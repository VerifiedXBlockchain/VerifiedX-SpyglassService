# Migration — V1 → V2 Transition

## Current State

- V1 vBTC is broken/deprecated on butterfly
- A few hundred users, some with V1 balances
- V1 uses a shared treasury model with single SC
- V1 balances are event-sourced from VbtcTransfer records in butterfly DB

## Strategy: Clean Break

V1 and V2 are separate systems. No on-chain migration of balances. V1 is frozen and phased out.

## Phase 1: Freeze V1

1. Disable V1 vBTC operations in butterfly backend:
   - Remove V1 transfer endpoints
   - Remove V1 withdrawal endpoints
   - Remove V1 deposit flow
2. Keep V1 balance read endpoints temporarily (so users can see their V1 balance)
3. Show banner: "vBTC has been upgraded. Activate your new wallet to continue."

## Phase 2: V2 Goes Live

1. New users get V2 automatically (on-demand activation)
2. Existing users see prompt to activate V2
3. V1 balance displayed separately: "Legacy balance: X BTC (read-only)"

## V1 Balance Snapshot (before any cleanup)

Before deleting any V1 code or data, capture the authoritative V1 balance snapshot. This is the input for the migration step below and the only source of truth once `VbtcContract` (event-sourced) is removed.

**Script:** `scripts/v1_balance_snapshot.py` — uses the legacy `VbtcContract.get_all_balances()` to dump `{vfx_address: balance}` JSON to `docs/v1-balance-snapshot-{YYYY-MM-DD}.json`. Run against mainnet read replica. Today's CLAUDE.md says "a few hundred users, some with V1 balances"; confirm the actual number during execution before committing to the migration mechanics below.

The legacy `VbtcContract` class is preserved as `blockchain/vbtc_legacy.py` (or marked as `_legacy` within the existing module) for the duration of the migration window so this script can run. Phase 6b removes it once the snapshot is committed and the per-user custody migration has settled.

## Phase 3: V1 Balance Migration — Per-User Custody (TC-7 Option A)

Each user with a V1 balance gets their BTC custody-held by butterfly until they activate their V2 token. **Do not** send BTC to user-controlled addresses before they have somewhere to receive it; we lose visibility into completion otherwise.

**Mechanics:**
1. From the snapshot, butterfly identifies users with non-zero V1 balances.
2. Butterfly converts each user's V1 vBTC obligation into actual BTC held at a butterfly-controlled BTC custody address (one per user, derived from a deterministic HD path off the treasury seed for auditability — the address never moves once recorded).
3. The amount sits in custody. The user is notified by email (see Communication Plan below).
4. When the user next logs in and activates their V2 token, a Celery task (`migrate_v1_balance_to_v2_token`) sweeps the per-user custody BTC to the butterfly V2 treasury deposit address, then calls `VbtcTreasurySendService.transfer(user_vfx_address, snapshot_amount)` to credit them with vBTC in the V2 treasury token.
5. The user can then withdraw normally (D3 — any holder withdraws from any token).

This avoids the "user never activates → BTC sits in user-owned address indefinitely" problem because the BTC stays in butterfly custody until activation. It also keeps cleanup deterministic: every cleared user reduces the custody-held BTC by their snapshot amount; reconciliation at the end is straightforward.

**Cost:** Total V1 balances + per-user BTC sweep fees at the time of activation. Expected to be small per CLAUDE.md.

**Failure modes:**
- User never activates → BTC stays in butterfly custody indefinitely. Track in `docs/todos.md`; periodic ops review to write down to zero after N years.
- Snapshot drift → snapshot is taken once, before V1 freeze. If V1 code is fully frozen, no drift. If anything still emits Type 18 events on the V1 treasury after the snapshot, re-snapshot.

## Data Cleanup

### Remove from butterfly DB:
- V1 VbtcWithdrawal records — **no**, keep them. The model is preserved (per TC-6 in the gap analysis) for activity history and frontend reads against existing read endpoints.
- V1 BTCDepositRequest records and table.
- V1 VbtcTreasuryState records and table.
- Escrow fields on `PaymentLink`: `btc_escrow_private_key`, `btc_escrow_address`, `btc_sweep_status`, `btc_sweep_tx_hash`, `btc_sweep_fee_sats`. Column drops happen in a separate deferred Phase 6b after operational sign-off.

### Do NOT remove:
- `VbtcTransfer` — kept. It's no longer balance-authoritative (Spyglass is), but it remains the activity ledger that links Type 18 events to PaymentLink/Purchase/FundRequest/OfframpTransaction/SwapTransaction FKs, drives SSE emit, runs the `fund_vfx_for_vbtc_recipient` heuristic, and feeds `FeeExpense`. The existing `sc_identifier` column (already present in schema) carries the V2 token ID for each event.

### Keep:
- User accounts (unchanged)
- VFX addresses (unchanged)
- Transaction history (generic, not vBTC-specific)
- `VbtcTransfer` and `VbtcWithdrawal` models (see above)
- `BtcApiClient` and BTC fee-estimate / confirmation helpers (still used for L1 BTC tracking by `monitor_v2_withdrawal_status`)

### Add:
- UserVbtcToken model (new, maps user → V2 token)
- ButterflyTreasuryToken model (new, one row per network — see 07-treasury-token.md)
- VbtcV2BalanceCache records

### Deferred to Phase 6b (separate PR, after operational sign-off)
Before scheduling Phase 6b, the gating verification query must return zero in-flight V1 PaymentLinks:

```sql
SELECT COUNT(*) FROM blockchain_paymentlink
WHERE btc_sweep_status IS NOT NULL
  AND btc_sweep_status NOT IN ('completed','failed','underfunded');
```

Then Phase 6b drops the escrow columns from `PaymentLink`, drops `BTCDepositRequest`, drops `VbtcTreasuryState`, and removes the V1 env vars. Splitting the column drop from "stop writing V1 paths" lets us deploy the V2 cutover without risking a migration race against in-flight V1 records.

## Configuration Changes

The authoritative env-var diff lives in `05-backend.md` under "Environment Variables (ENV-1)". Summary here:

### Keep (reused for V2 treasury operations):
- `VBTC_TREASURY_PRIVATE_KEY_{TESTNET,MAINNET}` — backend signing keypair, now used to sign V2 treasury transfers via Spyglass HTTP.
- `VBTC_TREASURY_ADDRESS_{TESTNET,MAINNET}` — butterfly's VFX address that owns the V2 treasury token.
- `VBTC_TREASURY_PUBLIC_KEY_{TESTNET,MAINNET}` — paired pubkey.
- `VBTC_MIN_PURCHASE_AMOUNT`, `VBTC_MIN_WITHDRAWAL_AMOUNT`, `VBTC_WITHDRAWAL_FEE_RATE_*`, `VBTC_WITHDRAWAL_ESTIMATED_TX_SIZE` — operational limits, still relevant.

### Add:
- `BUTTERFLY_VBTC_V2_TREASURY_SC_ID_{TESTNET,MAINNET}` — populated after Phase 0.5 activation; cached for fast read paths.
- `BUTTERFLY_VBTC_V2_TREASURY_DEPOSIT_ADDRESS_{TESTNET,MAINNET}` — the BTC address MoonPay/CDC/Stripe deliver to.
- `VBTC_V2_TREASURY_LOW_BALANCE_THRESHOLD_BTC` — default `0.001`; Sentry-warn threshold for `monitor_v2_treasury_balance`.
- `SPYGLASS_API_URL` — confirm reuses existing `VFX_EXPLORER_*` setting.

### Remove (in Phase 6b, after operational sign-off):
- `VBTC_TREASURY_SC_ID_{TESTNET,MAINNET}` — replaced by the V2 SC IDs above.
- `VBTC_TREASURY_BTC_DEPOSIT_ADDRESS_{TESTNET,MAINNET}` — replaced by the V2 deposit addresses above.
- `NEXT_PUBLIC_VBTC_CONTRACT_UID` (frontend).

## Communication Plan

1. In-app banner announcing V2 upgrade (2 weeks before)
2. **Personalized email to users with V1 balances**: include the snapshot amount in BTC, the activation instructions, and a clear statement that their balance is held in butterfly custody until they activate (no action by them is required to preserve the balance; activation is required to use it).
3. Activation prompt on first login after V2 launch
4. FAQ page explaining the change and self-custody benefits
