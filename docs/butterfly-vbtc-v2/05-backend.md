# Backend Implementation — Models, Services, APIs, Tasks

## New Models

### UserVbtcToken
```python
class UserVbtcToken(AbstractModel):
    user = models.OneToOneField(User, on_delete=CASCADE, related_name='vbtc_token')
    sc_identifier = models.CharField(max_length=128, unique=True, db_index=True)
    deposit_address = models.CharField(max_length=128)
    vfx_address = models.CharField(max_length=64, db_index=True)
    ceremony_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        max_length=20,
        choices=[
            ('pending_ceremony', 'Pending Ceremony'),    # ceremony in progress
            ('pending_ownership', 'Pending Ownership'),  # pre-activated, needs transfer
            ('active', 'Active'),                         # user owns it
        ],
        default='pending_ceremony',
    )
    frost_group_public_key = models.CharField(max_length=256, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
```

### ButterflyTreasuryToken

One row per network. Persists the result of the Phase 0.5 activation so the runtime never depends solely on env-var configuration. See `07-treasury-token.md` for the activation procedure.

```python
class ButterflyTreasuryToken(AbstractModel):
    is_testnet = models.BooleanField(unique=True)               # singleton per network
    sc_identifier = models.CharField(max_length=128, unique=True)
    owner_vfx_address = models.CharField(max_length=64)         # butterfly's treasury VFX address
    deposit_address = models.CharField(max_length=128)          # BTC deposit address for MoonPay et al.
    frost_group_public_key = models.CharField(max_length=256, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
```

`settings.BUTTERFLY_VBTC_V2_TREASURY_SC_ID_*` is a cached read of this row's `sc_identifier`; the model is the source of truth.

### VbtcV2BalanceCache
```python
class VbtcV2BalanceCache(AbstractModel):
    vfx_address = models.CharField(max_length=64, unique=True, db_index=True)
    total_balance = models.DecimalField(max_digits=32, decimal_places=16, default=0)
    primary_balance = models.DecimalField(max_digits=32, decimal_places=16, default=0)
    breakdown_json = models.JSONField(default=list)
    last_synced = models.DateTimeField(auto_now=True)
```

### VbtcTransfer (kept; not new)

`VbtcTransfer` is NOT removed in V2. It stays as the activity ledger that links Type 18 events to PaymentLink/Purchase/FundRequest/Offramp/Swap FKs, emits SSE, drives `fund_vfx_for_vbtc_recipient`, and feeds `FeeExpense`. The existing `sc_identifier` column (`blockchain/models.py:1913`, indexed) carries the V2 token ID for each event. Balance reads no longer come from `VbtcTransfer` — they come from `VbtcBalanceService` against Spyglass — but transfer records are still written by `handle_vbtc_transfer`.

## New Services

### VbtcTokenService
Handles token lifecycle (minting, activation).

```python
class VbtcTokenService:
    def __init__(self):
        self.spyglass = SpyglassVbtcClient()
    
    def get_or_create_token(self, user) -> UserVbtcToken:
        """Return existing token or raise NeedsActivation."""
        try:
            return UserVbtcToken.objects.get(user=user, status='active')
        except UserVbtcToken.DoesNotExist:
            raise NeedsActivation()
    
    def record_activation(self, user, sc_identifier, deposit_address, ceremony_id):
        """Called after frontend completes MPC ceremony + contract creation."""
        return UserVbtcToken.objects.create(
            user=user,
            sc_identifier=sc_identifier,
            deposit_address=deposit_address,
            vfx_address=user.vfx_address,
            ceremony_id=ceremony_id,
            status='active',
            activated_at=timezone.now(),
        )
```

### VbtcBalanceService
Handles balance aggregation from Spyglass.

```python
class VbtcBalanceService:
    CACHE_TTL = 30  # seconds
    
    def get_balance(self, vfx_address: str) -> dict:
        """Aggregated balance across all tokens."""
        # Check cache first
        cached = VbtcV2BalanceCache.objects.filter(
            vfx_address=vfx_address,
            last_synced__gte=timezone.now() - timedelta(seconds=self.CACHE_TTL)
        ).first()
        if cached:
            return cached.to_dict()
        
        # Fetch from Spyglass
        tokens = self.spyglass.get_tokens_for_address(vfx_address)
        total = Decimal(0)
        primary_balance = Decimal(0)
        breakdown = []
        
        for token in tokens:
            addr_balance = Decimal(str(token['addresses'].get(vfx_address, 0)))
            if addr_balance > 0:
                is_primary = token['owner_address'] == vfx_address
                if is_primary:
                    primary_balance = addr_balance
                total += addr_balance
                breakdown.append({
                    'sc_identifier': token['sc_identifier'],
                    'balance': float(addr_balance),
                    'is_primary': is_primary,
                })
        
        # Update cache
        VbtcV2BalanceCache.objects.update_or_create(
            vfx_address=vfx_address,
            defaults={
                'total_balance': total,
                'primary_balance': primary_balance,
                'breakdown_json': breakdown,
            }
        )
        
        return {
            'total_balance': float(total),
            'primary_balance': float(primary_balance),
            'breakdown': breakdown,
        }
```

### VbtcSendService — planner only (no backend signing)

The send service is a planner. It returns the list of `{sc_identifier, amount}` pairs for the frontend to iterate. The backend never sees the private key for user-initiated operations.

```python
class VbtcSendService:
    def prepare_send(
        self,
        from_address: str,
        to_address: str,
        amount: Decimal,
        exclude_sc_identifiers: list[str] | None = None,
    ) -> list[dict]:
        """
        Returns [{sc_identifier, amount}, ...] for the frontend to execute via SDK.
        `exclude_sc_identifiers` is used by frontend partial-failure resumes — the planner
        drops these before coin selection so Spyglass index lag can't cause double-sending.
        Dust-minimizing coin selection: empty smaller non-primary balances first, then drain primary.
        """
        balance = self.balance_service.get_balance(from_address)

        if Decimal(str(balance['total_balance'])) < amount:
            raise InsufficientBalance()

        excluded = set(exclude_sc_identifiers or [])
        candidates = [e for e in balance['breakdown'] if e['sc_identifier'] not in excluded]

        # Sort: smallest non-primary first (empty small balances), then primary.
        candidates.sort(key=lambda x: (x['is_primary'], Decimal(str(x['balance']))))

        selected = []
        remaining = amount
        for entry in candidates:
            if remaining <= 0:
                break
            send_amount = min(Decimal(str(entry['balance'])), remaining)
            selected.append({
                'sc_identifier': entry['sc_identifier'],
                'amount': str(send_amount),   # Decimal-as-string; see "Decimal precision" below
            })
            remaining -= send_amount

        return selected
```

### VbtcTreasurySendService — backend-signed, single-flight

Server-initiated transfers (PaymentLink claim, Purchase fulfillment, MoonPay BTC onramp delivery, swap delivery) sign with butterfly's treasury keypair via Spyglass HTTP. The SDK is JS-only and cannot be imported in Python; instead the backend reimplements the prepare→sign→send sequence using the existing `blockchain/vfx/utils.py:get_signature` helper, which already produces the same `base64(DER) + '.' + base58(pubkey)` composite the SDK emits. See `07-treasury-token.md` for the parity gate.

```python
class VbtcTreasurySendService:
    def transfer(self, to_address: str, amount: Decimal, is_testnet: bool) -> dict:
        """
        Returns {tx_hash, fee_native}. Routes through Spyglass HTTP.

        Concurrency: per-treasury single-flight via Redis lock keyed on
        `vbtc_v2:treasury_send:{sc_id}` (5-minute TTL) so concurrent claim/fulfillment
        tasks don't race the prepare→send sequence. Uses the existing Celery Redis broker.
        Raises VbtcTreasuryTransferError on any failure so callers can route retries
        and Sentry alerting independently of user-initiated send failures.
        """
        treasury = ButterflyTreasuryToken.objects.get(is_testnet=is_testnet)
        keypair = Keypair.vbtc_treasury(is_testnet=is_testnet)

        with redis_lock(f'vbtc_v2:treasury_send:{treasury.sc_identifier}', ttl=300):
            prep = self.spyglass.prepare_transfer(
                sc_identifier=treasury.sc_identifier,
                from_address=treasury.owner_vfx_address,
                to_address=to_address,
                amount=amount,
            )
            signature = get_signature(prep['Hash'], keypair.private_key, keypair.public_key)
            sent = self.spyglass.send_transfer(
                hash=prep['Hash'],
                signature=signature,
                public_key=keypair.public_key_hex_no_04,
            )
            return {'tx_hash': sent['Hash'], 'fee_native': prep['Fee']}
```

### SpyglassVbtcClient — reads + writes
HTTP client for Spyglass V2 endpoints. Used by both `VbtcBalanceService` (reads) and `VbtcTreasurySendService` (treasury writes). All write methods accept a pre-signed `signature` + `public_key` from the caller — the client itself never signs.

```python
class SpyglassVbtcClient:
    def __init__(self):
        self.base_url = settings.SPYGLASS_API_URL  # https://data.verifiedx.io/api

    # Reads
    def get_tokens_for_address(self, address: str) -> list[dict]:
        response = requests.get(f"{self.base_url}/btc/vbtc-v2/{address}/")
        return response.json().get('results', [])

    def get_token_detail(self, sc_identifier: str) -> dict:
        response = requests.get(f"{self.base_url}/btc/vbtc-v2/detail/{sc_identifier}/")
        return response.json()

    # Writes (treasury-signed)
    def prepare_transfer(self, sc_identifier, from_address, to_address, amount) -> dict:
        """POST /btc/vbtc-v2/transfer/prepare/ → {Hash, Fee}"""

    def send_transfer(self, hash: str, signature: str, public_key: str) -> dict:
        """POST /btc/vbtc-v2/transfer/send/ → {Hash: tx_hash}"""

    # Withdrawal cancel (used by frontend via butterfly proxy if needed; primarily SDK-driven)
    def prepare_withdraw_request(self, sc_identifier, owner_address, btc_address, amount, fee_rate) -> dict:
        """POST /btc/vbtc-v2/withdraw/request/prepare/ → {Hash}"""

    def send_withdraw_request(self, hash: str, signature: str, public_key: str) -> dict:
        """POST /btc/vbtc-v2/withdraw/request/send/"""

    def prepare_withdraw_cancel(self, sc_identifier, owner_address, withdrawal_request_hash) -> dict:
        """POST /btc/vbtc-v2/withdraw/cancel/prepare/ → {Hash}"""

    def send_withdraw_cancel(self, hash: str, signature: str, public_key: str) -> dict:
        """POST /btc/vbtc-v2/withdraw/cancel/send/"""
```

## API Endpoints

All V2 endpoints use `authentication_classes = [VfxSignatureRequired]` (from `api/authentication.py:134`). All Decimal fields serialize as strings (`DecimalField(coerce_to_string=True)`) to avoid float drift on small amounts.

### Balance
```
GET /api/butterfly/vbtc/balance/
Auth: VfxSignatureRequired
Response: {
    "total_balance": "0.00350000",
    "primary_balance": "0.00200000",
    "deposit_address": "bc1p..." | null,
    "is_activated": true,
    "can_withdraw": true,
    "breakdown": [
        {"sc_identifier": "abc:123", "balance": "0.00200000", "is_primary": true}
    ]
}
```

### Activation Status
```
GET /api/butterfly/vbtc/activation/
Auth: VfxSignatureRequired
Response: {
    "status": "active" | "needs_activation" | "pending_ceremony" | "pending_ownership",
    "sc_identifier": "..." | null,
    "deposit_address": "..." | null
}
```

### Record Activation
```
POST /api/butterfly/vbtc/activate/
Auth: VfxSignatureRequired
Body: {
    "sc_identifier": "...",
    "deposit_address": "...",
    "ceremony_id": "...",
    "frost_group_public_key": "..."
}
Response: { "success": true, "status": "active", "sc_identifier": "...", "deposit_address": "..." }
```

Called by frontend after MPC ceremony + contract creation completes. Server validates via `SpyglassVbtcClient.get_token_detail(sc_identifier)` that `owner_address == request.user.vfx_address` before writing the row. Idempotent: returns 200 with the existing row if the user already has an active token; returns 409 with the existing token's info if the user attempts to record a *different* token (multi-tab race — see 01-token-lifecycle.md).

### Prepare Send
```
POST /api/butterfly/vbtc/send/prepare/
Auth: VfxSignatureRequired
Body: { "to_address": "...", "amount": "0.001", "exclude_sc_identifiers": [] }
Response: {
    "transfers": [
        { "sc_identifier": "abc:123", "amount": "0.001" }
    ]
}
```

Returns the list of transfers the frontend needs to execute via SDK. Frontend iterates and calls `transferVbtc()` for each. Frontend includes `exclude_sc_identifiers` on retry after partial failure (see plan 02).

### Validate Withdrawal
```
POST /api/butterfly/vbtc/withdraw/validate/
Auth: VfxSignatureRequired
Body: { "amount": "0.001", "btc_address": "bc1q...", "fee_rate": 12 }
Response (success): {
    "valid": true,
    "sc_identifier": "...",
    "owner_address": "...",
    "available": "0.002",
    "fee_estimate_sats": 250,
    "largest_single_token": "0.001"  // optional hint when fragmented
}
Response (failure, HTTP 400): {
    "valid": false,
    "reason": "not_activated" | "insufficient_total" | "insufficient_single_token",
    "available": "0.0005",
    "largest_single_token": "0.0005"
}
```

Pre-validates before frontend starts the SDK's `requestWithdrawal` call.

## Celery Tasks

### Kept (rewritten to use V2 paths)
The following V1 tasks are kept; their vBTC-touching code paths are rewritten to use `VbtcTreasurySendService` (for transfers) or `VbtcBalanceService` (for balance reads) instead of `VbtcContract` / `VfxClient.transfer_vbtc`. The overall task shape, dispatch points, and retry behavior are unchanged.

- `fulfill_purchase` — Purchase fulfillment for vBTC onramp.
- `process_claim_transaction` — PaymentLink claims for vBTC/BTC chain.
- `monitor_moonpay_deposit` — MoonPay BTC onramp confirmation (bitcoin chain marks `TreasuryFundingTransaction` confirmed immediately; EVM still polls RPC).
- `credit_vbtc_to_user` — generic credit task used by Purchase + swap delivery.
- `monitor_vbtc_bridge` — SwapKit/THORChain polling for USDC→vBTC swaps. Polls SwapKit for `destination_tx_hash` AND polls Spyglass `get_token_detail(butterfly_sc_id)` for BTC arrival into the V2 treasury before forwarding vBTC to the user via `VbtcTreasurySendService`.
- `fund_vfx_for_vbtc_recipient` — ensures fresh recipients have enough VFX to cover their next outbound send.
- `handle_vbtc_transfer` — Type 18 indexer handler. Records `VbtcTransfer` with `sc_identifier` populated, emits SSE, runs FK linking. Also invalidates `VbtcV2BalanceCache` for sender + recipient (see plan 02 "Cache invalidation").

### New

- `monitor_v2_withdrawal_status(withdrawal_id)` — driven by Type 27 → FROST → BTC broadcast → BTC confirmation → Type 28 timeline. Emits SSE `vbtc:withdrawal_status` on each transition. Replaces the polling pattern from V1.
- `monitor_v2_treasury_balance` — Celery beat task on a 15-minute cadence. Calls `SpyglassVbtcClient.get_token_detail(butterfly_sc_id)`. If BTC backing balance < `VBTC_V2_TREASURY_LOW_BALANCE_THRESHOLD_BTC` (default `0.001`), emits a Sentry warning. See `07-treasury-token.md`.
- `fund_vfx_for_activation(user_id)` — ensures a user has enough VFX to cover the `createVbtcToken` ceremony (~0.00013 VFX). Same pattern as `fund_vfx_for_vbtc_recipient`.
- `sync_active_balances` — keeps the V2 balance cache warm for recently active users. Runs on a short cadence (~5 min).
- `pre_activate_tokens` (Phase 6, deferred) — mints tokens for active users who haven't activated. Blocked on Aaron's Raw transfer ownership endpoint. Takes `select_for_update` on the candidate user row before dispatching `ceremony/prepare/` to avoid the multi-tab race called out in 01-token-lifecycle.md.

### Deleted (replaced by V2)
The V1 escrow flow is fully removed. The corresponding `PaymentLink.btc_escrow_*` columns are dropped in deferred Phase 6b.

- `monitor_btc_escrow_deposit` — replaced by direct vBTC transfer from funder to treasury V2 deposit address.
- `execute_btc_sweep` — no escrow sweep under V2.
- `sweep_btc_deposit_escrow` — same.
- `monitor_btc_deposit_sweep` — same.
- `credit_vbtc_for_deposit` — replaced by `VbtcTreasurySendService.transfer` triggered from `monitor_moonpay_deposit`.
- `monitor_btc_deposit` — replaced by `monitor_moonpay_deposit` + Spyglass `get_token_detail` polling.
- `sync_btc_treasury_balances` — replaced by `monitor_v2_treasury_balance`.

## Environment Variables (ENV-1)

### Keep (reused for V2 treasury operations)
- `VBTC_TREASURY_PRIVATE_KEY_{TESTNET,MAINNET}` — butterfly's backend signing keypair; now signs V2 treasury transfers via Spyglass HTTP.
- `VBTC_TREASURY_ADDRESS_{TESTNET,MAINNET}` — butterfly's VFX address that owns the V2 treasury token.
- `VBTC_TREASURY_PUBLIC_KEY_{TESTNET,MAINNET}` — paired pubkey.
- `VBTC_MIN_PURCHASE_AMOUNT`, `VBTC_MIN_WITHDRAWAL_AMOUNT`, `VBTC_WITHDRAWAL_FEE_RATE_{TESTNET,MAINNET}`, `VBTC_WITHDRAWAL_ESTIMATED_TX_SIZE` — operational limits, still relevant.

### Add
- `BUTTERFLY_VBTC_V2_TREASURY_SC_ID_{TESTNET,MAINNET}` — populated after Phase 0.5 activation; cached read from env. Source of truth is `ButterflyTreasuryToken.sc_identifier`.
- `BUTTERFLY_VBTC_V2_TREASURY_DEPOSIT_ADDRESS_{TESTNET,MAINNET}` — BTC deposit address for MoonPay/CDC/Stripe. Same source-of-truth note.
- `VBTC_V2_TREASURY_LOW_BALANCE_THRESHOLD_BTC` — default `0.001`; threshold for `monitor_v2_treasury_balance` Sentry warning.
- `VBTC_V2_ACTIVE` — boolean cutover feature flag. When `False`, V1 paths run unchanged. When `True`, V1 transfer/withdrawal endpoints 503 and all server-initiated paths route through V2. Defaults to `False` in every environment; flipped per-env after verification.
- `SPYGLASS_API_URL` — confirm reuses existing `VFX_EXPLORER_*` setting.

### Remove (deferred to Phase 6b, after operational sign-off)
- `VBTC_TREASURY_SC_ID_{TESTNET,MAINNET}` — replaced by `BUTTERFLY_VBTC_V2_TREASURY_SC_ID_*`.
- `VBTC_TREASURY_BTC_DEPOSIT_ADDRESS_{TESTNET,MAINNET}` — replaced by `BUTTERFLY_VBTC_V2_TREASURY_DEPOSIT_ADDRESS_*`.
- `NEXT_PUBLIC_VBTC_CONTRACT_UID` (frontend env).

## Removed Code

### Delete (Phase 6a — stop writing V1 paths)
- `VbtcContract` class — preserved as `_legacy` only for the V1 balance snapshot script during the migration window; removed in Phase 6b.
- `BTCDepositRequest` model (V1 escrow flow); table dropped in Phase 6b.
- `VbtcTreasuryState` model (V1 treasury cache); table dropped in Phase 6b.
- `PaymentLink.btc_escrow_private_key`, `btc_escrow_address`, `btc_sweep_status`, `btc_sweep_tx_hash`, `btc_sweep_fee_sats` (escrow fields); columns dropped in Phase 6b after the gating verification query returns zero in-flight V1 rows.
- V1 vBTC escrow Celery tasks: `monitor_btc_escrow_deposit`, `execute_btc_sweep`, `sweep_btc_deposit_escrow`, `monitor_btc_deposit_sweep`, `credit_vbtc_for_deposit`, `monitor_btc_deposit`, `sync_btc_treasury_balances`.
- V1 vBTC API views: `PrepareVbtcTransferView`, `PrepareVbtcWithdrawalView` (V1), `BroadcastVbtcWithdrawalView` and their URL routes.
- `VfxClient.transfer_vbtc()` (V1 transfer builder).

### Do NOT delete
- `VbtcTransfer` — kept (see "VbtcTransfer (kept)" above and TC-6 in the gap analysis).
- `VbtcWithdrawal` — kept; existing `/api/butterfly/vbtc/withdrawals/` read endpoints continue to serve frontend history views.
- `handle_vbtc_transfer` / `handle_vbtc_withdrawal` handlers — kept; rewritten to populate V2 `sc_identifier`, invalidate `VbtcV2BalanceCache`, and emit SSE.

### Keep
- Bitcoin client (`BtcApiClient`) — still used for BTC L1 fee estimates and confirmation reads driven by `monitor_v2_withdrawal_status`.
- Existing user/auth infrastructure.
- SSE event system (extended with `vbtc:activation_complete`, `vbtc:withdrawal_status`).
- Treasury keypair config (`VBTC_TREASURY_PRIVATE_KEY_*` etc.) — same keypair, now signs V2 treasury transfers.
