# Privacy Transactions on the Web Wallet — Architecture Proposal

> For team review before implementation. Looking for feedback on approach, especially from Aaron/Jay on the CLI side.
> Date: 2026-05-26

## Goal

Bring full privacy transaction support (Shield, Unshield, Private Transfer, Consolidate) for VFX and vBTC to the web wallet. Currently desktop-only.

## Constraint

The web wallet is non-custodial. Users' spending keys live in the browser, not on any server. The architecture must preserve this while still leveraging the CLI's native crypto capabilities.

## Proposed Architecture: Hybrid Split

Split responsibilities between browser and server based on what requires key material vs what's pure computation.

### Browser (Key Operations — Self-Custodial)
- Key derivation from seed (BIP32 → spending key → viewing key → encryption key → zfx_ address)
- Note decryption (secp256k1 ECDH + AES-256-GCM — standard WebCrypto)
- Nullifier computation (Poseidon hash — JS implementation needed)
- Witness assembly (selecting inputs, packing circuit inputs as bytes)
- TX payload construction and signing
- Broadcast via existing `/api/raw/send/`

### Server / CLI (Stateless Computation — No Key Material)
- PLONK proof generation (accepts witness bytes, returns proof bytes)
- Pedersen commitments (accepts amount + randomness, returns commitment)
- Poseidon hashing (optional proxy if JS implementation has issues)
- Merkle proof retrieval (commitment tree lives on CLI)
- Note scanning via imported viewing key (read-only, no spending capability)
- Pool state queries (Merkle root, total supply, commitment count)

### What the Server Never Sees
- Spending key
- Encryption private key
- Wallet seed / mnemonic

### What the Server Does See
- Viewing key (imported with user consent — enables automatic note scanning)
- Witness data during proof generation (amounts, randomness, Merkle paths — ephemeral, not stored)
- zfx_ address (public shielded address)

This is the same trust model as Zcash light wallets sharing viewing keys with a lightwalletd server.

## Data Flow: Unshield Example (Z→T)

```
Browser                          Spyglass                         CLI
  |                                 |                               |
  |-- 1. GET /privacy/balance/ ---->|-- proxy ---------------------->|
  |<--- unspent notes + amounts ----|<-------------------------------|
  |                                 |                               |
  | 2. Select inputs (locally)      |                               |
  | 3. Compute nullifiers (locally) |                               |
  | 4. Build witness bytes          |                               |
  |                                 |                               |
  |-- 5. POST /privacy/prove/ ----->|-- proxy ---------------------->|
  |    { circuit: "unshield",       |    (witness bytes only,        |
  |      witness_b64: "..." }       |     no keys)                   |
  |<--- { proof_b64: "..." } -------|<-------------------------------|
  |                                 |                               |
  | 6. Encrypt change note (locally)|                               |
  | 7. Assemble TX payload          |                               |
  | 8. Sign TX                      |                               |
  |                                 |                               |
  |-- 9. POST /raw/send/ ---------->|-- broadcast to network ------>|
  |<--- tx_hash --------------------|                               |
```

Steps 1-4, 6-8 use only browser-local keys and standard web crypto.
Step 5 is a stateless proof computation — the CLI gets circuit inputs, not keys.

## What's Needed from the CLI (Aaron)

### 1. GenerateProof Endpoint (New)

A stateless endpoint that accepts circuit witness bytes and returns a PLONK proof. No key material, no wallet state — pure math.

```
POST /privacyapi/PrivacyV1/GenerateProof
Body: {
  "CircuitType": "shield" | "transfer" | "unshield" | "fee",
  "WitnessDataBase64": "<base64 encoded witness bytes>"
}
Response: {
  "Success": true,
  "ProofBase64": "<PLONK proof bytes>",
  "PublicInputsBase64": "<circuit public inputs>"
}
```

The witness format per circuit is already defined in `PlonkProverV1.cs`. This endpoint would call the same `plonk_prove_*` FFI functions but accept pre-built witness data from an external caller instead of building it internally.

### 2. PedersenCommit Endpoint (New)

Compute a Pedersen commitment from amount + randomness. Alternatively, if we get a WASM build of the Poseidon/Pedersen primitives, this can move client-side.

```
POST /privacyapi/PrivacyV1/PedersenCommit
Body: {
  "AmountScaled": 150000000,
  "RandomnessBase64": "<32 bytes base64>"
}
Response: {
  "Success": true,
  "CommitmentBase64": "<48 byte G1 compressed>",
  "NoteHashBase64": "<32 byte Poseidon hash>"
}
```

### 3. MerkleProof Endpoint (New)

Return the Merkle proof (sibling path) for a commitment at a given tree position.

```
GET /privacyapi/PrivacyV1/GetMerkleProof?asset=VFX&treePosition=42
Response: {
  "Success": true,
  "ProofBase64": "<sibling path bytes>",
  "RootBase64": "<current Merkle root>",
  "LeafCount": 150
}
```

### 4. Viewing Key Import (Existing — Already Works)

The CLI already supports `ImportViewingKey`. The web wallet would call this once during privacy activation to register its viewing key. The CLI then automatically scans incoming blocks for the user's notes.

### 5. Poseidon Hash Endpoint (Optional, Fallback)

If the JS Poseidon implementation has compatibility issues with the BLS12-381 field parameters used by `plonk_ffi`, a server-side fallback:

```
POST /privacyapi/PrivacyV1/PoseidonHash
Body: { "InputsBase64": "<concatenated 32-byte field elements>" }
Response: { "HashBase64": "<32 byte result>" }
```

Ideally we avoid this (latency per hash call adds up), but good to have as a safety net.

### 6. Verify Headless CLI Has Prover Keys

The headless CLI needs PLONK prover keys loaded (VXPLNK02/VXPLNK03 params). We can check via `GET /privacyapi/PrivacyV1/GetPlonkStatus` — want to confirm `CapV1Prove` (bit 4) is set.

### 7. Privacy TX Hash Endpoint (New, or confirm existing works)

Privacy transactions use a different hash computation than regular TXs. Regular TXs hash all fields; privacy TXs use `BuildPrivate()` which hashes `Timestamp + TransactionType + Data` (for spend TXs) or `Timestamp + TransactionType + Data + FromAddress + Amount + Nonce` (for shield TXs). Both use double-SHA256.

The existing `GetTxHash` endpoint may not handle this. Either:
- Confirm `GetTxHash` detects privacy TX types and routes to `BuildPrivate()` internally
- Or add a `GetPrivateTxHash` endpoint
- Or the web wallet computes the hash locally (double-SHA256 is trivial in JS)

**Ask for Aaron:** Does `txapi/txV1/GetTxHash` correctly compute hashes for privacy TX types (31-36)? If not, can it be updated to detect them and use `BuildPrivate()` instead of `Build()`?

### 8. Confirm SendRawTransaction Routes Privacy TXs Correctly

The CLI's `SendRawTransaction` calls `TransactionValidatorService.VerifyTX()`. For privacy TXs, validation needs to go through `PrivateTransactionValidatorService.VerifyPrivateTX()` instead. Need to confirm the routing is correct for TX types 31-36 when received via `SendRawTransaction`.

**Ask for Aaron:** When a privacy TX (type 31-36) is submitted via `SendRawTransaction`, does it correctly route to `PrivateTransactionValidatorService`? The web wallet will submit fully-built privacy TXs through this endpoint.

### 9. Rate Limiting on GenerateProof

Proof generation is CPU-intensive (1-3 seconds per proof) and the Rust FFI uses a `Mutex` on the prover params — concurrent requests serialize. Without rate limiting, the GenerateProof endpoint is both a DoS vector and a concurrency bottleneck.

**Recommendations:**
- Rate limit to N concurrent proof requests (suggest N=2-4)
- Queue additional requests with a timeout (30s max wait)
- Return 429/503 if queue is full rather than blocking indefinitely
- Consider: if proof demand grows, extracting to a dedicated proof service that can scale horizontally

## What We Don't Need from the CLI

- No changes to existing privacy endpoints (ShieldVFX, UnshieldVFX, etc.)
- No changes to consensus/validation
- No changes to the PLONK circuits themselves
- No changes to how the desktop GUI works

## Poseidon Hash — Solved via Mini WASM Build

The browser needs to compute Poseidon hashes matching the native `plonk_ffi` exactly (for nullifier derivation). Rather than reimplementing in JS and risking field parameter mismatches, we'll compile a **minimal WASM build of just the Poseidon hash** from the same `plonk_ffi` Rust source.

We reviewed the `plonk` Rust repo (github.com/VerifiedXBlockchain/plonk). Key findings:
- The entire crypto stack is **pure Rust** (arkworks BLS12-381, no C bindings)
- WASM compilation is feasible with minor changes (refactor `std::fs::read()` in param loading, disable `getrandom` feature)
- A Poseidon-only WASM build would be **~100-200 KB** — trivial to ship in the browser

This guarantees byte-identical hashes with zero compatibility risk.

**We evaluated a full WASM build** (proving + verifying + all circuits) but decided against it for v1:
- Full WASM binary: 8-15 MB + 10-50 MB prover params download
- Proof generation in WASM: 10-30s (vs 1-3s server-side)
- Significant UX friction on first load
- Server-side proving is acceptable given the privacy tradeoff (server sees amounts during proving but not keys)

The full WASM build remains an option for a future "maximum privacy" mode.

**Ask for Aaron:** We'd like to create a `plonk-wasm` crate in the plonk repo that exposes just `poseidon_hash` and `poseidon_note_hash` for browser use. This requires:
- Adding a `plonk_load_params_from_bytes(ptr, len)` variant (current `plonk_load_params` uses `std::fs::read`)
- A `wasm` feature flag that disables `getrandom` and `std_rng`
- ~1-2 days of Rust work

Alternatively, if you can share test vectors (known inputs → expected Poseidon outputs), we can validate a JS implementation against them as a fallback.

## Note Consolidation

The PLONK circuits support max 2 inputs per transaction. Users with many small shielded notes need consolidation before larger spends. The web wallet will handle this by:
- Detecting when the requested amount exceeds the sum of the 2 largest notes
- Prompting the user to consolidate (merge 2 smallest notes → 1, Z→Z to self)
- Auto-repeating until the user has notes large enough for the spend
- Each consolidation requires ~1 block confirmation (~20s)

## Merkle Root Staleness

Between building a TX (fetching Merkle proofs) and broadcasting, the root can change as new blocks arrive. The network allows roots within 100 blocks (~30 min). The web wallet will:
- Fetch a fresh root when building each TX
- On "stale root" rejection: re-fetch proofs, rebuild witness, re-generate proof, re-broadcast
- Show "refreshing proof..." rather than an error to the user

## Trust Model & Security Considerations

### What the Server Learns

| Data | When | Risk |
|------|------|------|
| Viewing key | On privacy activation (one-time import) | Server can see all past and future shielded TXs for this address. Cannot spend. |
| Transaction amounts + randomness | During proof generation (ephemeral) | Server learns the specific amounts being transacted. Not stored. |
| zfx_ address | On balance queries | Server knows which shielded address is being queried. Public information. |

**If the server is compromised:** An attacker with the viewing key can surveil a user's shielded activity (amounts, timing) but cannot spend funds. An attacker who intercepts proof requests can learn transaction amounts for those requests. This is the same trust model as Zcash light wallets (lightwalletd).

**Mitigation:** The full WASM build (future "maximum privacy" mode) would eliminate all server-side data exposure by running proof generation in the browser.

### Multi-Device Conflicts

If a user operates from both the desktop GUI and web wallet, both can attempt to spend the same shielded note. The network rejects double-spends at the consensus level (nullifier already in the set), but the UX needs to handle this:
- On "nullifier already spent" error: refresh the note set from the server, show updated balance, let user retry
- The auto-scanner on the CLI updates note state in real-time, so refreshing balances after an error resolves the conflict

### Viewing Key Persistence on CLI

Imported viewing keys are stored in the CLI's LiteDB (`PRIV_WALLETS` collection). They persist across restarts. However, if the CLI's database is wiped (redeployment, migration), viewing keys are lost. The web wallet should:
- Re-import the viewing key on session start if the CLI reports no balance / unknown address
- Trigger a rescan after re-import

## Phased Rollout

| Phase | What | Depends On |
|-------|------|------------|
| 1. Read-only | Balance queries, pool state, commitment list | Viewing key import (existing CLI endpoint) |
| 2. Poseidon WASM | Mini WASM build of Poseidon hash for browser | `plonk-wasm` crate (Aaron, ~1-2 days) |
| 3. Shield (T→Z) | Transparent → shielded deposits | PedersenCommit + NoteHash endpoints |
| 4. Unshield + Transfer (Z→T, Z→Z) | Full spending operations | GenerateProof + MerkleProof endpoints |
| 5. vBTC privacy | Same ops for vBTC (dual-fee model) | Phase 4 complete |
| 6. Consolidation + Polish | Auto-merge small notes, recovery scanning, UX | Phase 4 complete |

Phase 1 needs no CLI changes. Phase 2 is a small Rust task. Phases 3-4 need the new CLI endpoints.

## Open Questions

1. Does this architecture make sense from the CLI perspective? Any concerns about exposing witness-based proof generation?
2. Can we create a `plonk-wasm` crate in the plonk repo for the Poseidon-only WASM build? Or would you prefer we extract test vectors and use a JS implementation?
3. Does `txapi/txV1/GetTxHash` handle privacy TX types (31-36) correctly, or does it only use `Build()` (not `BuildPrivate()`)?
4. Does `SendRawTransaction` correctly route privacy TX types to `PrivateTransactionValidatorService`?
5. Any other privacy features on the roadmap that might affect this architecture?
6. Is there any plan to support higher-input circuits (>2 inputs), or should we design around the 2-input constraint?
7. What's the concurrency story for proof generation? Can the FFI handle multiple simultaneous requests, or do they serialize through the Mutex?
