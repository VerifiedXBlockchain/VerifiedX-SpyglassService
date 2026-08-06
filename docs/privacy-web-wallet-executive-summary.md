# Privacy on the Web Wallet — Executive Summary

> For Jay
> Date: 2026-05-26

## What

Bring the same privacy features that exist on the desktop wallet to the web wallet. This lets users shield their VFX and vBTC balances so transactions are private — amounts and participants are hidden on-chain.

Desktop has had this for a while. Web doesn't have it yet. Web is the most-used wallet, so this is a gap.

## Why It's Not Trivial

Privacy transactions aren't like normal transactions. A normal transfer is basically "sign a message and broadcast it." Privacy transactions involve zero-knowledge cryptography — the wallet has to generate mathematical proofs that a transaction is valid without revealing the amounts or who's involved.

On the desktop, this is straightforward because the wallet talks directly to a local node that handles all the heavy crypto. On the web, the user's keys are in the browser and the node is on a remote server. We need to split the work between browser and server without compromising self-custody (the server should never be able to spend a user's funds).

## The Approach

We're doing a hybrid model:

- **Browser handles keys and encryption** — the user's private keys never leave the browser. Standard web crypto (the same kind banks use) handles the encryption parts.
- **Server handles proof generation** — the heavy math (PLONK zero-knowledge proofs) runs on our server using the same native code the desktop uses. The server gets the math inputs but never the keys.
- **A small WASM module** (~200 KB, runs in browser) handles one specific hash function (Poseidon) that needs to match the server exactly. This avoids compatibility bugs.

The user's funds remain fully self-custodial. The server acts as a calculator — it does math on numbers you give it and hands back a result. It can't sign transactions or move funds.

## What's Needed

### From Aaron (CLI / Core)
- 3-4 new API endpoints on the headless node (proof generation, Pedersen commitment, Merkle proof retrieval)
- A small WASM build of the Poseidon hash function from the existing Rust code
- Confirm a couple of existing endpoints handle privacy transaction types correctly
- Estimated effort: ~1 week

### From Us (Spyglass / Explorer API)
- Proxy endpoints to route web wallet requests to the CLI
- Rate limiting on the proof generation endpoint (it's CPU-heavy)
- Estimated effort: ~1 week

### From Web Wallet Team
- Key derivation and note encryption in JavaScript (using standard browser crypto APIs)
- Building and signing privacy transactions client-side
- Witness assembly (packing the inputs for proof generation)
- UI for shielding, unshielding, private transfers, and balance display
- Edge case handling (multi-device conflicts, note consolidation, network errors)
- Estimated effort: ~7-10 weeks (this is the bulk of the work)

## Timeline

| Phase | What | Time |
|-------|------|------|
| 1 | Read-only (show balances, pool state) | 1-2 weeks |
| 2 | Poseidon WASM build + browser integration | 1-2 weeks |
| 3 | Shield (deposit VFX into privacy pool) | 2-3 weeks |
| 4 | Unshield + Private Transfer (full spending) | 3-4 weeks |
| 5 | vBTC privacy (same ops for vBTC) | 1 week |
| 6 | Polish (consolidation, recovery, edge cases) | 1-2 weeks |
| **Total** | | **~9-12 weeks** |

Phases 1-2 can run in parallel. CLI and Spyglass work can overlap with web wallet development. The critical path is the web wallet JavaScript crypto implementation (Phases 3-4).

## What Users Get

- Shield VFX or vBTC to make their balance private
- Send private transfers where amount and participants are hidden
- Unshield back to a normal transparent balance at any time
- Same privacy guarantees as the desktop wallet
- Self-custodial — server never holds keys

## Risks

- **Poseidon hash compatibility** — if the browser's hash doesn't match the server's exactly, transactions fail. Mitigated by using a WASM build of the same Rust code (not a separate reimplementation).
- **Proof server availability** — if the server goes down, users can't generate proofs (can't spend shielded funds). They can still shield and view balances. Mitigated by monitoring and redundancy.
- **Browser performance** — the crypto operations in the browser (encryption, hashing, witness assembly) should be fast on modern devices. Proof generation stays server-side so there's no multi-second wait in the browser.
