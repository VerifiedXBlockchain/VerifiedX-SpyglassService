# Balance & Transfers — Aggregation, Sending, Receiving, Consolidation

## Balance Model

A user's total vBTC balance = sum of their balance across ALL tokens they appear in.

```
User A owns Token X (deposit address: bc1p_A)
User A also received 0.001 vBTC from User B (stored in Token Y)
User A also received 0.0005 vBTC from User C (stored in Token Z)

Total balance for A = Token_X.addresses[A] + Token_Y.addresses[A] + Token_Z.addresses[A]
```

**Source of truth**: Spyglass API
- `GET /btc/vbtc-v2/{vfx_address}/` returns all tokens the address is associated with
- Each token's `addresses` map has the per-address balance

## Backend Balance Service

```python
class VbtcBalanceService:
    @cached(ttl=30)  # 30-second cache
    def get_balance(self, vfx_address: str) -> VbtcBalance:
        """Get aggregated vBTC balance for a user."""
        tokens = spyglass.get_vbtc_tokens(vfx_address)
        
        total = Decimal(0)
        breakdown = []
        
        for token in tokens:
            address_balance = token['addresses'].get(vfx_address, 0)
            if address_balance > 0:
                total += Decimal(str(address_balance))
                breakdown.append({
                    'sc_identifier': token['sc_identifier'],
                    'balance': address_balance,
                    'is_primary': token['owner_address'] == vfx_address,
                    'deposit_address': token['deposit_address'] if token['owner_address'] == vfx_address else None,
                })
        
        return VbtcBalance(total=total, breakdown=breakdown)
```

**API endpoint** for frontend:
```
GET /api/butterfly/vbtc/balance/
Response: {
    "total_balance": 0.0035,
    "deposit_address": "bc1p...",  // null if not activated
    "is_activated": true,
    "breakdown": [
        { "sc_identifier": "abc:123", "balance": 0.002, "is_primary": true },
        { "sc_identifier": "def:456", "balance": 0.001, "is_primary": false },
        { "sc_identifier": "ghi:789", "balance": 0.0005, "is_primary": false }
    ],
    "needs_consolidation": true  // has balance in non-primary tokens
}
```

Frontend only shows `total_balance`. Breakdown is internal.

## Receiving vBTC

### From another butterfly user (VFX transfer)
- Sender's frontend calls `transferVbtc()` from SDK
- Transfer TX lands on VFX chain
- Recipient's balance updates on next Spyglass sync (within seconds)
- Recipient sees updated balance on next API call or page refresh
- SSE event triggers real-time update (existing butterfly infrastructure)

### From BTC deposit (to own deposit address)
- User sends BTC to their deposit address
- Spyglass balance sync picks it up (every 10 min, or live on detail view)
- Butterfly polls Spyglass for balance changes (or uses webhook if available)
- User sees updated balance

## Sending vBTC

### Simple case (enough balance in one token)
```
User A sends 0.001 to User B
A has 0.002 in Token X (primary)
→ Single TransferVBTCV2 from Token X
```

### Multi-token case (balance split across tokens)
```
User A sends 0.003 to User B
A has 0.002 in Token X (primary) and 0.001 in Token Y (received from C)
→ Two sequential transfers:
  1. TransferVBTCV2 from Token X: 0.002
  2. TransferVBTCV2 from Token Y: 0.001
```

### Backend send planner (no signing)

The backend computes which tokens to draw from, in what amount. It returns the plan to the frontend, which executes each `transferVbtc` SDK call individually with the user's `privateKey` held in `VfxWalletContext`. The backend never sees the private key for user-initiated operations.

```python
class VbtcSendService:
    def prepare_send(
        self,
        from_address: str,
        to_address: str,
        amount: Decimal,
        exclude_sc_identifiers: list[str] | None = None,
    ) -> list[dict]:
        """Return [{sc_identifier, amount}, ...] for the frontend to iterate.

        `exclude_sc_identifiers` lets the frontend retry partial-failure resumes
        without double-sending the tokens it has already settled (the planner
        drops these before coin selection).
        """
        balance = self.balance_service.get_balance(from_address)

        if Decimal(str(balance['total_balance'])) < amount:
            raise InsufficientBalance(available=balance['total_balance'], requested=amount)

        excluded = set(exclude_sc_identifiers or [])
        candidates = [
            entry for entry in balance['breakdown']
            if entry['sc_identifier'] not in excluded
        ]

        # Coin selection: empty smaller non-primary balances first, then drain primary.
        # See "Dust Prevention" below — Option B is the chosen rule.
        candidates.sort(key=lambda x: (x['is_primary'], Decimal(str(x['balance']))))

        selected: list[dict] = []
        remaining = amount
        for entry in candidates:
            if remaining <= 0:
                break
            send_amount = min(Decimal(str(entry['balance'])), remaining)
            selected.append({'sc_identifier': entry['sc_identifier'], 'amount': send_amount})
            remaining -= send_amount

        return selected
```

The frontend then iterates:

```typescript
for (const transfer of plan.transfers) {
  await client.transferVbtc({
    scIdentifier: transfer.sc_identifier,
    fromAddress: wallet.address,
    toAddress: recipient,
    amount: transfer.amount,
    privateKey: privateKey,   // held in VfxWalletContext only
  });
  completed.push(transfer.sc_identifier);
}
```

On partial failure (TX N succeeds, TX N+1 fails), the frontend re-calls `prepare_send` with `exclude_sc_identifiers = [...completed]` — this guards against Spyglass index lag so the planner can't accidentally re-issue an already-settled token.

**Key decisions:**
- Server plans (not signs). The backend never receives the private key for user-initiated operations.
- Sequential per TX on the frontend, not parallel (avoid nonce conflicts).
- If one TX fails mid-sequence, prior TXs are valid (partial send). The frontend resumes with `exclude_sc_identifiers`.
- Frontend shows single "Sending..." indicator throughout, with per-TX progress underneath.

### Cache invalidation

The 30-second `VbtcV2BalanceCache` covers steady-state read traffic, but a user who just initiated a transfer needs to see their balance update immediately on the next render — waiting 30s is a UX regression. The Type 18 indexer handler (`handle_vbtc_transfer`) must invalidate the cache for both sides as soon as the transfer is recorded:

```python
VbtcV2BalanceCache.objects.filter(
    vfx_address__in=[sender_address, recipient_address],
).delete()
```

This applies to V2 events only — the handler discriminates by the `sc_identifier` populated from the Type 18 contract UID. The cache also needs to be invalidated on `handle_vbtc_withdrawal` for the requestor's address.

### Receiving from butterfly treasury

A user does **not** need an activated token to *receive* vBTC at their VFX address. PaymentLink claims, MoonPay BTC onramp deliveries, Purchase fulfillment, and swap deliveries all transfer vBTC from butterfly's V2 treasury token to the user's VFX address — the balance lands inside the treasury token (per-address balance map within the token) and is visible to the user via `VbtcBalanceService.get_balance`.

Activation is required only to:
1. Obtain a personal BTC deposit address (so external BTC deposits can fund the user's own token).
2. Initiate a withdrawal (the FROST signing in `requestWithdrawal` requires the requestor to be a participant — any holder qualifies under D3, but you need a `sc_identifier` to point the SDK at, and unactivated users have nothing of their own; they can still withdraw from the treasury token they hold balance in, which uses the treasury sc_identifier).

This is what makes D9 work end to end: a brand-new user with no VFX wallet history can be sent vBTC immediately, claim it without any setup, and only activate when they want their own deposit address.

## Consolidation (Phase 7)

Move balance from non-primary tokens to user's primary token.

### When to consolidate
- User logs in and has balance in non-primary tokens
- User initiates a send and we detect fragmentation
- Background check on session start

### Consolidation flow
```
User B has:
  Token X (primary, owned): 0.002
  Token Y (received from A): 0.001
  Token Z (received from C): 0.0005

Consolidation:
  1. TransferVBTCV2 from Token Y → B's address in Token X: 0.001
     Wait... this doesn't work. You can't transfer INTO a specific token.
```

**Critical realization**: `TransferVBTCV2` transfers vBTC within a SINGLE token from one address to another. You can't move balance between tokens. To "consolidate", User B would need to:

1. Withdraw from Token Y (BTC withdrawal → receive BTC)
2. Deposit BTC to their own Token X (send BTC to their deposit address)

This is expensive (BTC fees twice) and slow (Bitcoin confirmations).

**Revised decision**: Consolidation via on-chain transfers is not practical. Instead:
- Accept that balance may be fragmented across tokens
- Multi-token send handles it transparently
- No background consolidation
- Users don't need to know or care

**Updated architecture**: Remove consolidation from the plan. Multi-token send handles fragmented balances for transfers. For withdrawals, users can withdraw directly from any token they hold balance in — no consolidation needed. The `TransferVBTCV2Multi` endpoint from Aaron (Phase 8) would make multi-token sends a single TX instead of sequential.

## Dust Prevention

When a user sends vBTC, prefer emptying smaller token balances completely rather than leaving dust:
```
Sending 0.0015 from [0.002 (primary), 0.001 (Token Y)]
Option A: Send 0.0015 from primary → leaves 0.0005 in primary, 0.001 in Token Y
Option B: Send 0.001 from Token Y + 0.0005 from primary → empties Token Y ✓
```

Option B is better — reduces fragmentation over time.
