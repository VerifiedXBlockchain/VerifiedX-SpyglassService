# Treasury V2 Token — Butterfly's Own V2 Token

## Concept

Butterfly operates its own V2 vBTC token, one per network (testnet + mainnet). This is the "treasury": the source of all server-initiated vBTC transfers for PaymentLink claims, MoonPay/CDC/Stripe BTC onramp deliveries, USDC→vBTC swap deliveries, vBTC Purchase fulfillment, and FundRequest fulfillment. End users never see the treasury — they receive vBTC at their own VFX address, which lands as balance inside the treasury token's per-address map. They can then withdraw it directly under D3 (any holder withdraws from any token).

The treasury keypair already exists in env (`VBTC_TREASURY_PRIVATE_KEY_{TESTNET,MAINNET}`) — V2 reuses it. The treasury VFX address (`VBTC_TREASURY_ADDRESS_*`) becomes the `ownerAddress` of the V2 treasury token.

This entire concept exists because user-initiated operations are frontend-signed (D8), but server-initiated operations have no user in the loop. The treasury token is the system-owned signing source that fills that gap.

## Model

```python
class ButterflyTreasuryToken(AbstractModel):
    is_testnet = models.BooleanField(unique=True)               # singleton per network
    sc_identifier = models.CharField(max_length=128, unique=True)
    owner_vfx_address = models.CharField(max_length=64)         # butterfly's treasury VFX address
    deposit_address = models.CharField(max_length=128)          # BTC deposit address (MoonPay et al. send here)
    frost_group_public_key = models.CharField(max_length=256, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
```

`settings.BUTTERFLY_VBTC_V2_TREASURY_SC_ID_{TESTNET,MAINNET}` is a cached read of `sc_identifier`. The model row is the source of truth; the env var is the fast-path cache. Same for `BUTTERFLY_VBTC_V2_TREASURY_DEPOSIT_ADDRESS_{TESTNET,MAINNET}`.

## One-Time Activation Procedure

A Django management command, run once per network. **Idempotent against both successful and partial-failure prior runs.**

```bash
# Testnet
make manage ARGS="activate_butterfly_treasury_token --testnet"

# Mainnet (Tyler runs manually; double-gated)
make manage ARGS="activate_butterfly_treasury_token --mainnet --confirm-mainnet"
```

### Idempotency check (run before any signing or HTTP)

1. **Row exists?** `ButterflyTreasuryToken.objects.filter(is_testnet=...).first()` — if present, print the existing row's env vars to stdout, exit 0. No-op.
2. **Token exists on chain but row missing?** Call `SpyglassVbtcClient.get_tokens_for_address(treasury_vfx_address)`. If a token already exists with `owner_address == treasury_vfx_address` (and the expected ticker), recover its `sc_identifier` + `deposit_address` from Spyglass, write the row, exit 0. This recovers the partial-failure case where ceremony+create succeeded on chain but the row insert never ran.

Only if both checks fail does the command proceed with the full ceremony + create flow.

### Ceremony + create flow

Same sequence as `createVbtcToken` in the SDK (`vfx-web-sdk/src/client/vfx-client.ts`), reimplemented in Python using `requests` + the existing signing helper.

1. **Prepare ceremony**:
   ```
   POST /btc/vbtc-v2/ceremony/prepare/
   body: { owner_address: <treasury VFX address> }
   ```
   Response includes `ceremony_id`, `session_id`, `messages_to_sign.start_message`, `messages_to_sign.start_timestamp`, `messages_to_sign.share_distribution_message`, `messages_to_sign.share_distribution_timestamp`.
2. **Sign both messages** with the treasury private key via the existing `Keypair.vbtc_treasury(is_testnet)` helper and `blockchain/vfx/utils.py:get_signature`.
3. **Execute ceremony** — must send all seven fields:
   ```
   POST /btc/vbtc-v2/ceremony/execute/
   body: {
     ceremony_id, session_id, owner_address,
     start_signature, start_timestamp,
     share_distribution_signature, share_distribution_timestamp
   }
   ```
4. **Poll** `GET /btc/vbtc-v2/ceremony/{ceremony_id}/` every 4 seconds until `status == "Completed"`. Hard timeout 3 minutes.
5. **Build ownership proof message** exactly as the SDK does (no separators between fields):
   ```
   message = f"{owner_address}{name}{description}{ticker}{ceremony_id}{timestamp}{unique_id}"
   ```
   - `name = 'vBTC'`
   - `description = 'vBTC Token'`
   - `ticker = 'vBTC'`
   - `timestamp = int(time.time())` (integer seconds)
   - `unique_id` = 16 characters drawn from the exact SDK charset: `'AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTtUuVvWwXxYyZz0123456789'`. Use Python's `random.choices(charset, k=16)`.
   Sign the resulting string with the treasury private key → `owner_signature`.
6. **Prepare create**:
   ```
   POST /btc/vbtc-v2/create/prepare/
   body: { owner_address, name, description, ticker, ceremony_id, timestamp, unique_id, owner_signature }
   ```
   **The response includes `SmartContractUID` and `DepositAddress` — capture both here.** It also includes `Hash` for signing.
7. **Sign `Hash`** with the treasury private key.
8. **Send create**:
   ```
   POST /btc/vbtc-v2/create/send/
   body: { hash, signature, public_key }
   ```
   Response includes only the broadcast tx hash; ignore.
9. **Persist** the `ButterflyTreasuryToken` row.
10. **Write env vars to stdout AND `scratch.txt`** for Tyler to paste into `.env.local` / Porter:
    ```
    BUTTERFLY_VBTC_V2_TREASURY_SC_ID_TESTNET=<sc_id>
    BUTTERFLY_VBTC_V2_TREASURY_DEPOSIT_ADDRESS_TESTNET=<deposit_addr>
    ```

## Env Vars

```
BUTTERFLY_VBTC_V2_TREASURY_SC_ID_TESTNET=<populated by Phase 0.5>
BUTTERFLY_VBTC_V2_TREASURY_SC_ID_MAINNET=<populated by mainnet runbook>
BUTTERFLY_VBTC_V2_TREASURY_DEPOSIT_ADDRESS_TESTNET=<populated by Phase 0.5>
BUTTERFLY_VBTC_V2_TREASURY_DEPOSIT_ADDRESS_MAINNET=<populated by mainnet runbook>
VBTC_V2_TREASURY_LOW_BALANCE_THRESHOLD_BTC=0.001    # Sentry warn threshold
```

The `VBTC_TREASURY_PRIVATE_KEY_*` / `VBTC_TREASURY_ADDRESS_*` / `VBTC_TREASURY_PUBLIC_KEY_*` env vars are reused from V1 — no change.

## Python Signing Parity (PY-1)

The SDK at `vfx-web-sdk` is TypeScript-only and cannot be imported in Python. For treasury operations the backend reimplements the SDK's prepare→sign→send sequence using two HTTP calls plus one signing step.

**The good news:** signing parity is already a solved problem in butterfly. `blockchain/vfx/utils.py:get_signature(hash, private_key, public_key)` produces the same `base64(DER(ecdsa-sha256)) + '.' + base58(pubkey)` composite the SDK emits as `signature`. The `public_key` field on the Spyglass send payload is the hex pubkey with the leading `04` byte stripped — `get_signature` already handles that.

The call sequence for a treasury-initiated transfer is exactly:

```
POST /btc/vbtc-v2/transfer/prepare/   {sc_identifier, from_address, to_address, amount}
  → returns Hash + Fee
sign Hash with treasury private key via get_signature(...)
POST /btc/vbtc-v2/transfer/send/      {hash, signature, public_key}
  → returns { Hash: tx_hash }
```

### Parity gate (one-time, before Phase 1)

A throwaway parity script (`scripts/verify_v2_signing_parity.py`) **MUST run successfully against testnet Spyglass before any other Phase 1 code lands**:

1. Load `Keypair.vbtc_treasury(is_testnet=True)`.
2. Call testnet Spyglass `POST /btc/vbtc-v2/transfer/prepare/` for a 0.00001 BTC self-transfer (treasury → treasury). No balance is required for prepare; only `send` would fail.
3. Sign the returned `Hash` using `get_signature(hash, private_key, public_key)`.
4. Call `POST /btc/vbtc-v2/transfer/send/` — assert HTTP 200 with `success: true`.
5. Log the resulting tx hash.

If this fails, the signing model is broken and a Node sidecar would be required (NOT in scope for this plan — stop and escalate). If it succeeds, Phase 1 proceeds.

A JS cross-check unit test in `vfx-web-sdk/src/__tests__/parity.test.ts` runs as part of Phase 1's automated test suite — it imports the SDK's `signAndSend` machinery and, given a fixed input hash + private key, asserts the resulting `{signature, public_key}` body exactly matches what Python `get_signature` produces. This catches drift even when Spyglass is unavailable.

## Backend Service

```python
class VbtcTreasurySendService:
    def transfer(self, to_address: str, amount: Decimal, is_testnet: bool) -> dict:
        """Returns {tx_hash, fee_native}."""
        treasury = ButterflyTreasuryToken.objects.get(is_testnet=is_testnet)
        keypair = Keypair.vbtc_treasury(is_testnet=is_testnet)

        # Single-flight via Redis lock — concurrent claim/fulfillment tasks must not race
        # the prepare→send sequence against the same treasury sc_id.
        lock_key = f'vbtc_v2:treasury_send:{treasury.sc_identifier}'
        with redis_lock(lock_key, ttl=300):
            prep = spyglass.prepare_transfer(
                sc_identifier=treasury.sc_identifier,
                from_address=treasury.owner_vfx_address,
                to_address=to_address,
                amount=amount,
            )
            signature = get_signature(prep['Hash'], keypair.private_key, keypair.public_key)
            sent = spyglass.send_transfer(
                hash=prep['Hash'],
                signature=signature,
                public_key=keypair.public_key_hex_no_04,
            )
            return {'tx_hash': sent['Hash'], 'fee_native': prep['Fee']}
```

The single-flight lock uses the existing Celery Redis broker connection. The 5-minute TTL is a hard ceiling: if anything in the sequence stalls for more than 5 minutes we have a bigger problem than concurrent transfers. Wraps all errors in `VbtcTreasuryTransferError` so callers can route retries / Sentry alerting independently of user-initiated send failures.

## Monitoring Task

```python
@celery_task(queue='default')
def monitor_v2_treasury_balance():
    """Beat-scheduled every 15 minutes.

    Sentry-warns when BTC backing balance < VBTC_V2_TREASURY_LOW_BALANCE_THRESHOLD_BTC.
    The threshold is intentionally conservative — 0.001 BTC default — so we get
    ~24-48 hours of warning before claims start failing.
    """
    for is_testnet in [True, False]:
        try:
            treasury = ButterflyTreasuryToken.objects.get(is_testnet=is_testnet)
        except ButterflyTreasuryToken.DoesNotExist:
            continue   # mainnet not yet activated; that's fine
        detail = spyglass.get_token_detail(treasury.sc_identifier)
        backing_btc = Decimal(str(detail.get('total_btc_balance', 0)))
        if backing_btc < settings.VBTC_V2_TREASURY_LOW_BALANCE_THRESHOLD_BTC:
            sentry_sdk.capture_message(
                f'V2 treasury BTC backing low: {backing_btc} BTC '
                f'(threshold {settings.VBTC_V2_TREASURY_LOW_BALANCE_THRESHOLD_BTC}, '
                f'is_testnet={is_testnet})',
                level='warning',
            )
```

Add to the Celery beat schedule with `crontab(minute='*/15')`.

## Operational Rules

- **Only ops can withdraw from the treasury token.** There is no operational code path that triggers `requestWithdrawal` against the butterfly treasury sc_identifier. Withdrawals from the treasury are an ops-initiated event (e.g. rebalancing custody, sweeping operational floats) and happen via a separate ops tool, never via the user-facing flow. This is enforced by code review, not by the API — there is no scope where `request.user` equals the treasury's owner, but the ops tool is also out of scope for this plan.
- **`VbtcContract` (legacy) stays accessible only for the V1 snapshot script.** Phase 6a renames or moves `blockchain/vbtc.py` to `blockchain/vbtc_legacy.py` and removes all other imports. The one-shot `scripts/v1_balance_snapshot.py` imports the legacy class to dump `get_all_balances()` to JSON. Phase 6b removes the legacy class entirely after the snapshot is committed and the per-user custody migration has settled.
- **Treasury private key handling is unchanged from V1.** Cold-key backup, key rotation procedure, audit log — all of the V1 operational practices carry over. The same key signs V2 treasury transfers.

## Mainnet Activation Runbook

The mainnet activation is reserved for Tyler. The same command runs on mainnet with `--mainnet --confirm-mainnet`, but the operational risk is much higher (one-shot, irreversible, treasury key signs).

See `docs/runbooks/activate-mainnet-vbtc-v2-treasury.md` (in the **butterfly-service repo**, not the blueprint repo) for the auditable step-by-step. The runbook covers:
- Pre-flight checks (`VBTC_TREASURY_PRIVATE_KEY_MAINNET` present, `SPYGLASS_API_URL` pointing at mainnet, sufficient VFX gas on treasury for the create TX).
- Exact command invocation.
- Expected output and where to verify each milestone.
- Recovery procedure if the ceremony fails mid-way (the idempotency check handles partial-failure re-runs — see "One-Time Activation Procedure" above).
- Rollback note: **there is no rollback.** Once the token is created on mainnet, it exists on mainnet. The idempotency check ensures we don't accidentally create a *second* token, but we cannot un-create the first. This is by design — the treasury token is a long-lived operational artifact, not a deployable.
- Post-activation: paste the env vars from `scratch.txt` into Porter for both backend and Celery workers; redeploy; confirm `monitor_v2_treasury_balance` runs without error.

The runbook lives in butterfly-service rather than this blueprint because it's an operational artifact tied to the deploy surface — the blueprint references it but does not duplicate the contents.
