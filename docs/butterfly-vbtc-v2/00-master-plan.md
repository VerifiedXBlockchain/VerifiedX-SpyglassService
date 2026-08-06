# Butterfly vBTC V2 Refactor — Master Plan

## Goal

Replace butterfly's V1 vBTC system (shared treasury, custodial) with V2 per-user tokens (self-custodial, FROST-based). Each user gets their own vBTC token with a unique BTC deposit address. Users see a single consolidated BTC balance — all multi-token complexity is hidden.

## Architecture Decisions

### D1: Per-user token model
Each butterfly user has at most one "primary" vBTC token that they own. They can also hold balance in other users' tokens (from receiving transfers). Their visible balance is the sum across all tokens.

### D2: Lazy minting with pre-activation option
- **On-demand**: User clicks "Activate" in the app → frontend runs MPC ceremony (user signs in browser) → token created directly with user as owner. ~30-90 seconds.
- **Pre-activation**: Butterfly backend mints with its own address → stores pending token → transfers ownership when user next logs in. Requires Raw transfer ownership endpoint from Aaron (pending).

### D3: Any holder can withdraw from any token
The `OwnerAddress` param in FROST signing is really "requestor address" — any address holding vBTC in a token can initiate a withdrawal from that token. The FROST signing uses the requestor's leader auth. This means users can withdraw received vBTC directly — no need to consolidate first.

### D4: No consolidation (removed)
Cross-token consolidation was considered but is not practical. TransferVBTCV2 only moves balance within a token, not between tokens. Moving balance from Token Y to Token X would require withdrawing to BTC and re-depositing — too expensive and slow. Instead, multi-token sends handle fragmented balances transparently.

### D5: Multi-token sends via sequential TXs
When sending more vBTC than available in any single token, butterfly executes N sequential transfers (one per source token, largest-first). Each is atomic. Partial sends are valid if one fails.

### D6: V1 is fully replaced, not coexisted
V1 vBTC code is removed. Existing V1 balances are not migrated on-chain — they're frozen. Users with V1 balances can withdraw via the legacy flow or are notified of the transition. No new V1 operations.

### D7: Balance reads from Spyglass
Butterfly does not maintain its own event-sourced balance ledger for V2. Spyglass is the source of truth (aggregates from blockchain). Butterfly calls Spyglass APIs and caches server-side.

### D8: Frontend uses JS SDK
The butterfly web app uses the vfx-web-sdk for all V2 operations. Backend orchestrates multi-token logic and provides consolidated balance APIs. **User-initiated operations sign in the browser only — the private key never leaves `VfxWalletContext`.** The backend planner computes which tokens to send from; the frontend iterates the SDK call per token.

### D9: Butterfly operates its own V2 treasury token
PaymentLink claims, MoonPay/CDC/Stripe BTC onramps, USDC→vBTC swap deliveries, vBTC Purchase fulfillment, and FundRequest fulfillment continue to flow from a butterfly-owned vBTC V2 token (one per network). The treasury private key (already held by the backend in `VBTC_TREASURY_PRIVATE_KEY_*`) signs `transferVbtc` calls from this token to recipients via Spyglass HTTP, using the backend's existing Python signing helper (`blockchain/vfx/utils.py:get_signature`) — no JS SDK on the backend. End users never see the treasury — they receive vBTC at their own VFX address (which lands as balance inside the treasury token; they can then withdraw it directly under D3).

See `07-treasury-token.md` for the full treasury concept, activation procedure, and operational rules.

## Sub-Plans

| Plan | File | Focus |
|------|------|-------|
| Token Lifecycle | [01-token-lifecycle.md](01-token-lifecycle.md) | Minting, activation, pre-activation, ownership transfer |
| Balance & Transfers | [02-balance-and-transfers.md](02-balance-and-transfers.md) | Aggregation, sending, receiving, consolidation |
| Withdrawals | [03-withdrawals.md](03-withdrawals.md) | 4-step flow, UTXO constraints, monitoring |
| Migration | [04-migration.md](04-migration.md) | V1 → V2 transition, existing users, data cleanup |
| Backend Implementation | [05-backend.md](05-backend.md) | Models, services, APIs, Celery tasks |
| Frontend Implementation | [06-frontend.md](06-frontend.md) | SDK integration, UI/UX, activation flow |
| Treasury V2 Token | [07-treasury-token.md](07-treasury-token.md) | Butterfly's own V2 token: activation, treasury send service, monitoring |

## Phase Timeline

| Phase | What | Blocked On |
|-------|------|------------|
| 0 | Architecture review (this plan) | Nothing |
| 0.5 | Activate butterfly's V2 treasury token (one-time, blocks Phase 4+) | Phase 0 |
| 1 | Backend: balance service + read APIs | Phase 0.5 |
| 2 | Backend: on-demand activation (minting) | Nothing |
| 3 | Frontend: activation flow + balance display | Phase 1-2 |
| 4 | Backend + Frontend: send/receive | Phase 3 |
| 5 | Backend + Frontend: withdrawal | Phase 4 |
| 6 | Backend: pre-activation service | Transfer ownership Raw endpoint (Aaron) |
| 7 | Migration: V1 sunset | Phase 5 |
| 8 | Backend: multi-token sends | TransferVBTCV2Multi endpoint (Aaron, optional) |

Phases 1-5 are the MVP. Phases 6-8 are optimizations. Phase 0.5 is a one-time operational gate — without an activated treasury token, the server-initiated paths (Phase 5 cutover) have nothing to send from.

> The phase numbering above is the architecture-level timeline. The executable phase plan (`/Users/tyler/.claude/plans/synthetic-riding-frog-phases.md`) re-numbers these around the actual repos and commits — both views are valid; Phase 0.5 is the same milestone in both.

## Dependencies on Aaron (CLI)

| Item | Status | Needed For |
|------|--------|------------|
| Raw transfer ownership endpoint | Pending | Phase 6 (pre-activation) |
| TransferVBTCV2Multi | Pending | Phase 9 (single-TX multi-token sends) |
| FROST unconfirmed UTXO handling | Nice-to-have | Shared token withdrawals (not MVP) |

## Key Risks

1. **MPC ceremony reliability** — 93 validators need to be online. If the network is degraded, activation fails. Mitigation: retry logic, clear error messaging.
2. **VFX gas for minting** — each mint costs ~0.00013 VFX. Butterfly needs to fund this. At scale (1000 users), that's ~0.13 VFX. Trivial.
3. **Consolidation requires user online** — if user only receives vBTC and never logs in, their balance stays fragmented. Mitigation: multi-token send handles it server-side.
4. **UTXO blocking on withdrawals** — each withdrawal needs previous BTC TX confirmed (~10 min). Per-user tokens mitigate this since each user has their own UTXO set.
5. **Treasury V2 token is an operational SPoF.** PaymentLink claims, MoonPay BTC onramp delivery, swap delivery, and Purchase fulfillment all flow from this single butterfly-owned V2 token. If the token's BTC backing drops to zero, every server-initiated vBTC path queues until ops refill. Mitigation: `monitor_v2_treasury_balance` Celery beat task (15-min cadence) Sentry-warns when BTC backing < `VBTC_V2_TREASURY_LOW_BALANCE_THRESHOLD_BTC` (default `0.001`). Cold-key offline backup of treasury private key (already done for V1; reused for V2). Per-treasury single-flight lock in `VbtcTreasurySendService` prevents concurrent prepare/send races.
