# Token Lifecycle — Minting, Activation, Ownership

## User States

```
NEW_USER          → No vBTC token. Balance may be >0 (received from others).
ACTIVATION_READY  → Pre-activated by butterfly. Token exists, butterfly owns it.
ACTIVATED         → User owns their token. Has a deposit address. Full functionality.
```

## On-Demand Activation (Phase 2)

User is present in the browser. Frontend drives the MPC ceremony.

```
User clicks "Activate vBTC"
  ↓
Frontend: ceremony/prepare/ with user's VFX address
  ↓
Frontend: signs both messages with user's keypair (in browser)
  ↓
Frontend: ceremony/execute/ with signatures
  ↓
Frontend: polls ceremony/{id}/ every 3-5s (~30-90s)
  ↓
Frontend: generates ownership proof signature
  ↓
Frontend: create/prepare/ with ceremony_id + name/description/ticker
  ↓
Frontend: signs Hash, create/send/
  ↓
Backend: records user's token in DB (sc_identifier, deposit_address)
  ↓
User sees: "Your Bitcoin deposit address: bc1p..."
```

**UX**: Show progress bar during ceremony. "Setting up your Bitcoin wallet... (X% complete)". 30-90 seconds total.

**Token metadata**: Defaults (these are the values exercised in the SDK tests at `vfx-web-sdk/src/__tests__/vbtc-v2.test.ts`; we use them as the safe default):
- Name: `vBTC`
- Description: `vBTC Token`
- Ticker: `vBTC`

> TODO: confirm the desired user-facing naming convention before mainnet rollout. The candidates are: (a) keep `vBTC` for all tokens (simpler, ticker is shared anyway), (b) namespace by username e.g. `{username}:vBTC`, or (c) `BTFLY:vBTC` for the butterfly treasury token specifically. The defaults above match the SDK tests and are safe to ship for testnet; lock in before mainnet activation.

**Backend records on activation**:
```python
class UserVbtcToken(models.Model):
    user = models.OneToOneField(User, on_delete=CASCADE)
    sc_identifier = models.CharField(max_length=128, unique=True)
    deposit_address = models.CharField(max_length=128)
    vfx_address = models.CharField(max_length=64)
    status = models.CharField(choices=['pending_ownership', 'active'])
    created_at = models.DateTimeField(auto_now_add=True)
```

## Butterfly Treasury Activation (Phase 0.5)

Same `createVbtcToken` flow as user activation, but signed with butterfly's backend treasury keypair (`Keypair.vbtc_treasury(is_testnet)`) instead of a user keypair. Run as a one-shot Django management command — not Celery, not a request handler — so it's auditable and idempotent.

```
make manage ARGS="activate_butterfly_treasury_token --testnet"
# or for mainnet (Tyler runs manually):
make manage ARGS="activate_butterfly_treasury_token --mainnet --confirm-mainnet"
```

The ceremony + create sequence is identical to user activation:
1. `POST /btc/vbtc-v2/ceremony/prepare/` with `{owner_address: <treasury VFX address>}`.
2. Sign both `start_message` + `share_distribution_message` with the treasury private key.
3. `POST /btc/vbtc-v2/ceremony/execute/` with all seven fields (`ceremony_id`, `session_id`, `owner_address`, `start_signature`, `start_timestamp`, `share_distribution_signature`, `share_distribution_timestamp`).
4. Poll `GET /btc/vbtc-v2/ceremony/{ceremony_id}/` until `Completed` (3-min timeout, 4-second interval).
5. `POST /btc/vbtc-v2/create/prepare/` → response includes `SmartContractUID` + `DepositAddress`. **Capture these from the `prepare` response, not from `send`.**
6. Sign returned `Hash`, `POST /btc/vbtc-v2/create/send/`.
7. Persist a `ButterflyTreasuryToken(is_testnet, sc_identifier, owner_vfx_address, deposit_address, frost_group_public_key, activated_at)` row (singleton per network).

Idempotency:
- If `ButterflyTreasuryToken.objects.filter(is_testnet=...).first()` exists, exit 0 — no-op.
- Else query `SpyglassVbtcClient.get_tokens_for_address(treasury_vfx_address)`. If a token already exists (owner matches treasury, expected ticker), recover the `sc_identifier` + `deposit_address` and write the row. This covers the partial-failure case where ceremony+create succeeded on chain but the row insert never ran.

The command writes the resulting env vars to stdout **and** to `scratch.txt` so Tyler can paste them into `.env.local` / Porter:

```
BUTTERFLY_VBTC_V2_TREASURY_SC_ID_TESTNET=<sc_id>
BUTTERFLY_VBTC_V2_TREASURY_DEPOSIT_ADDRESS_TESTNET=<deposit_addr>
```

See `07-treasury-token.md` for the full treasury token architecture, the Python signing parity gate (PY-1), and the monitoring task.

## Pre-Activation (Phase 6)

Butterfly mints tokens in advance for users likely to need them. No user interaction required.

**Selection criteria for pre-activation:**
- Users with recent login activity (last 30 days)
- Users with existing V1 vBTC balance
- Users who have received VFX recently
- Exclude: dormant accounts, test accounts

**Flow:**
```
Celery task: pre_activate_vbtc_tokens
  ↓
For each selected user:
  1. Check if user already has a token → skip
  2. Call ceremony/prepare/ with BUTTERFLY's VFX address (not user's)
  3. Sign with butterfly's keypair
  4. Execute ceremony, poll until complete
  5. Create contract with butterfly as owner
  6. Store in DB: status = 'pending_ownership'
  ↓
When user next logs in:
  1. Check if pending_ownership token exists
  2. Transfer ownership: butterfly → user (needs Raw endpoint from Aaron)
  3. Update status to 'active'
  4. Show deposit address
```

**Rate limiting**: Max 2-3 ceremonies at a time (validator load). Space out pre-activations over hours/days.

**Cost**: ~0.00013 VFX per mint. Butterfly's gas wallet funds this.

## Deposit Address Display

Once activated, user sees their deposit address everywhere:
- Dashboard: "Your BTC Deposit Address: bc1p..."
- QR code for mobile scanning
- Copy button
- "Send Bitcoin here to add to your balance"

Before activation:
- "Activate your BTC wallet to get a deposit address"
- Can still see received vBTC balance (from other users' transfers)

## Edge Cases

**User loses access**: Token is on-chain, tied to their VFX address. If they recover their VFX wallet, they recover their vBTC token.

**Ceremony fails**: Retry automatically. If persistent failure, show error and let user retry manually. Don't block the app — they can still receive vBTC from others.

**Duplicate activation**: Prevented at the API layer by `OneToOneField(User)` on `UserVbtcToken` — a second `POST /vbtc/activate/` for the same user 409s with the existing token's `sc_identifier` + `deposit_address` in the body so the frontend can hydrate context cleanly.

**Multi-tab race during pre-activation (Phase 6)**: When the pre-activation Celery task picks up a candidate and the user simultaneously initiates on-demand activation in a browser tab, two ceremonies could fire. The DB row uniqueness blocks the second insert, but the on-chain side may produce a stranded second token. Mitigation: the pre-activation task must take `select_for_update` on the `User` row (or a dedicated activation lock row) before calling `ceremony/prepare/`, and the on-demand activation endpoint must check the lock and 409 if held.

**Multi-tab race during on-demand activation**: Two browser tabs (or two devices) of the same logged-in user can both invoke `createVbtcToken` before the first `recordVbtcV2Activation` lands. The second `POST /vbtc/activate/` 409s — the frontend handles this by hydrating the existing token from the 409 body rather than surfacing an error. The user "loses" the gas spent on the duplicate token; the duplicate's balance still shows up via the multi-token planner naturally because the user still owns it.
