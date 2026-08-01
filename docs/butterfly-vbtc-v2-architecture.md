# Butterfly vBTC V2 — Per-User Token Architecture

> Internal planning doc
> Date: 2026-05-28

## Overview

Each butterfly user gets their own vBTC V2 token with a unique BTC deposit address. No shared treasury. Butterfly mints the token (it has CLI access for MPC ceremony) and transfers SC ownership to the user's VFX address. From the user's perspective, they see a single consolidated BTC balance — all multi-token complexity is hidden.

## Why Per-User Tokens

- Truly self-custodial — butterfly never holds BTC
- Each user owns their deposit address and signing threshold
- No escrow/sweep complexity on the backend
- Philosophically aligned with butterfly's self-custody model
- Eliminates the V1 treasury bottleneck

## Signup / First Deposit Flow

**Lazy minting** (recommended over signup-time minting):
1. User signs up → no vBTC token minted yet (saves ceremony load)
2. User clicks "Deposit BTC" for the first time
3. Butterfly runs MPC ceremony using its own keys (CLI access)
4. Token created, SC ownership transferred to user's VFX address
5. User sees "Setting up your Bitcoin address..." progress bar (~30-90 seconds)
6. Deposit address shown — user can now receive BTC

**Why lazy:** Most users may never use vBTC. MPC ceremonies are expensive (93 validators, 30-90s each). Lazy minting avoids unnecessary load and VFX fee spend.

**Cost:** ~0.00013 VFX per mint. Butterfly funds this from its gas wallet.

## Balance Aggregation

A user may hold vBTC across multiple tokens:
- Their own token (from BTC deposits)
- Other users' tokens (from receiving transfers)

**Server-side aggregation:**
```
total_vbtc = sum(token.addresses[user_address] for token in user_associated_tokens)
```

Spyglass already provides `GET /btc/vbtc-v2/{address}/` which returns all tokens the address is associated with, each with per-address balance in the `addresses` map. Butterfly sums across tokens and presents one number.

**Balance response to frontend:**
```json
{
  "btc_balance": 0.0053,
  "token_breakdown": [
    { "sc_identifier": "abc:123", "balance": 0.003, "is_primary": true },
    { "sc_identifier": "def:456", "balance": 0.0023, "is_primary": false }
  ]
}
```

Frontend only shows `btc_balance`. The breakdown is available for advanced/debug views but not surfaced in normal UX.

## Sending vBTC (Multi-Token Coin Selection)

When a user sends more vBTC than they have in any single token, butterfly splits the send across tokens. This is a UTXO-like coin selection problem at the token level.

**Algorithm:**
1. Fetch per-token balances for sender, sorted descending
2. Select tokens largest-first until send amount is covered
3. For each selected token, calculate the amount to send from it
4. Execute N transfers (one per source token), all to same recipient address
5. Each transfer is atomic — partial sends are valid if one fails

**Example:** User has 0.003 in Token A and 0.0023 in Token B. Sends 0.005.
- Token A: send 0.003 (full balance)
- Token B: send 0.002 (partial)
- Two prepare/sign/send cycles

**Performance:** Each transfer is a two-step prepare/sign/send (~2-3 seconds). For 2-3 tokens, total is ~5-10 seconds. Acceptable with a progress indicator.

**Edge case:** If a TX fails mid-sequence, the recipient has received a partial amount. Butterfly should:
- Track the multi-send as a single logical operation
- Retry failed TXs
- Show the user "X of Y BTC sent" if partial

## Background Consolidation

To minimize multi-token sends, proactively consolidate:
- When a user receives vBTC into a non-primary token, queue a background transfer to move it to their primary token
- Run as a Celery task with delay (wait for block confirmation before consolidating)
- User never sees this — it's like UTXO consolidation in Bitcoin wallets
- Keeps most balance in one place, reducing N in multi-token sends

**Consolidation task:**
```
1. Detect: user has balance in non-primary tokens
2. For each non-primary token with balance:
   - Prepare transfer from non-primary → primary
   - Sign with user's key (requires user's key or delegation)
   - Broadcast
3. Mark consolidated
```

**Key challenge:** Consolidation requires the user's private key to sign transfers FROM non-primary tokens. Options:
- Consolidate only when user is online (frontend triggers it in background)
- Accept fragmentation when user is offline (multi-TX send handles it)
- Butterfly holds a delegated signing capability (breaks self-custody)

**Recommendation:** Consolidate opportunistically when user is online. Accept fragmentation otherwise. The multi-TX send handles the worst case gracefully.

## What Butterfly Server Does (V2)

| Responsibility | V1 | V2 |
|---------------|----|----|
| Treasury management | Server holds treasury keys, moves vBTC | Eliminated — no treasury |
| BTC deposits | Escrow addresses, sweep, credit | User's own deposit address, balance auto-updates |
| Token minting | N/A | Server runs MPC ceremony on first deposit request |
| SC ownership transfer | N/A | Server transfers ownership to user after mint |
| Balance tracking | Event-sourced from indexed TXs | Aggregated from Spyglass across user's tokens |
| Withdrawal | Server prepares, client signs | Client does prepare/sign/send via Spyglass |
| VFX gas funding | Fund recipients for gas | Same — fund for gas on transfers |
| TX monitoring | Monitor BTC confirmations | Same — monitor withdrawal confirmations |
| Consolidation | N/A | Opportunistic background consolidation |

## Butterfly Server-Side Changes

### New
- **Minting service**: Runs MPC ceremony via Spyglass proxy, transfers SC ownership
- **Balance aggregation**: Sums vBTC across all tokens for a user address
- **Multi-token send orchestration**: Coin selection + sequential TX execution
- **Consolidation task**: Moves balance to primary token when user is online

### Updated
- **Transaction handlers**: Type 26 (V2 transfer) and Type 28 (V2 withdrawal complete) instead of 18/21
- **VfxClient**: Use V2 two-step endpoints instead of direct raw TX building
- **Settings**: Per-user SC IDs instead of single treasury SC ID

### Removed
- **Treasury keypairs and addresses** — no shared treasury
- **Escrow/sweep flow** — no escrow addresses needed
- **Event-sourced balance ledger** — Spyglass is source of truth

## JS SDK Changes

The SDK needs to support all V2 operations. Since butterfly's frontend uses the SDK, these are shared:
- `createVbtcToken()` — ceremony + contract creation
- `transferVbtc()` — two-step prepare/sign/send
- `requestWithdrawal()` — two-step
- `completeWithdrawal()` — FROST signing + BTC broadcast
- `cancelWithdrawal()` — two-step
- `getVbtcTokens(address)` — list all tokens for address
- `getVbtcTokenDetail(scIdentifier)` — token detail with live balance

## Implementation Order

1. **Finish GUI testing** — validates Spyglass endpoints work for transfer + withdrawal
2. **JS SDK V2 methods** — needed by both butterfly frontend and standalone users
3. **Butterfly server-side**: lazy minting service, balance aggregation, multi-token send
4. **Butterfly frontend**: consolidated balance, send flow hiding multi-TX complexity
5. **Consolidation task** — optimize after core flow works

## Open Questions

1. Should we set a max number of tokens per user to bound coin selection complexity?
2. For consolidation, do we accept that it only works when the user is online (has keys available)?
3. Should butterfly pre-fund the minting fee, or require the user to have VFX first?
4. When a user receives vBTC from another butterfly user, should we consolidate immediately or lazily?
