# Privacy Web Wallet — Implementation Plan

> Internal implementation plan. Assumes the architecture doc has been reviewed and approved by the CLI team.
> Date: 2026-05-26

---

## Prerequisites (Verify Before Starting)

Before writing any code, confirm these with the CLI team:

1. **`GetPlonkStatus` check:** Hit the headless CLI's `/privacyapi/PrivacyV1/GetPlonkStatus` and confirm `CapV1Prove` (bit 4) is set. If not, prover keys need to be deployed.

2. **`SendRawTransaction` accepts privacy TXs:** Confirm that submitting a TX with type 31-36 via `txapi/txV1/SendRawTransaction` routes to `PrivateTransactionValidatorService` (not just `TransactionValidatorService`). If not, we need a different broadcast path.

3. **TX hash compatibility:** Confirm whether `txapi/txV1/GetTxHash` handles privacy TX types correctly (uses `BuildPrivate()` internally). If not, the web wallet will compute hashes locally (double-SHA256, trivial).

4. **Existing shielded test data:** Identify a zfx_ address with known shielded notes on mainnet or testnet for Phase 1 testing.

---

## Phase 1: Read-Only Privacy Endpoints (Spyglass)

**Goal:** Web wallet can display shielded balances, commitment lists, and pool state.

**Depends on:** Nothing (existing CLI endpoints, no new CLI work).

### Spyglass Changes

#### New file: `api/privacy/views.py`

Proxy views to CLI privacy endpoints:

| View | Method | Spyglass Path | CLI Path |
|------|--------|---------------|----------|
| `PlonkStatusView` | GET | `/api/privacy/plonk-status/` | `GET /privacyapi/PrivacyV1/GetPlonkStatus` |
| `ShieldedBalanceView` | GET | `/api/privacy/balance/{zfx_address}/` | `GET /privacyapi/PrivacyV1/GetShieldedBalance?zfxAddress={}&includeCommitments=true` |
| `ShieldedPoolStateView` | GET | `/api/privacy/pool-state/` | `GET /privacyapi/PrivacyV1/GetShieldedPoolState?asset=VFX` |
| `ShieldedVbtcBalanceView` | GET | `/api/privacy/vbtc/balance/{zfx_address}/{sc_uid}/` | `GET /privacyapi/PrivacyV1/GetShieldedVbtcBalance?zfxAddress={}&vbtcContractUid={}` |
| `ImportViewingKeyView` | POST | `/api/privacy/import-viewing-key/` | `POST /privacyapi/PrivacyV1/ImportViewingKey` |
| `ScanShieldedView` | POST | `/api/privacy/scan/` | `POST /privacyapi/PrivacyV1/ScanShielded` |
| `ScanShieldedVbtcView` | POST | `/api/privacy/vbtc/scan/` | `POST /privacyapi/PrivacyV1/ScanShieldedVBTC` |

#### New file: `api/privacy/urls.py`

Route registrations. Register in `api/urls.py` as `path("privacy/", include("api.privacy.urls"))`.

#### Add to: `rbx/client.py`

CLI client functions:
```python
def get_plonk_status()
def get_shielded_balance(zfx_address, include_commitments=True)
def get_shielded_pool_state(asset="VFX")
def get_shielded_vbtc_balance(zfx_address, sc_uid, include_commitments=True)
def import_viewing_key(zfx_address, viewing_key_b64, transparent_address=None)
def scan_shielded(zfx_address, from_height=0, to_height=0)
def scan_shielded_vbtc(zfx_address, sc_uid, from_height=0, to_height=0)
```

All follow existing patterns in `rbx/client.py` — `requests.get/post` to `join_url(BASE_URL, path)`.

### Testing Phase 1

1. Verify `GET /api/privacy/plonk-status/` returns capability flags from CLI
2. Import a viewing key via `POST /api/privacy/import-viewing-key/` using a known zfx_ address
3. Trigger scan via `POST /api/privacy/scan/`
4. Verify `GET /api/privacy/balance/{zfx_address}/` returns correct balance + commitments
5. Cross-check balance against desktop GUI for the same zfx_ address
6. Verify pool state endpoint returns Merkle root + commitment count

**Test data:** Use the existing shielded transactions on mainnet (types 31-35 we already found in the DB). If we have a zfx_ address that owns some of those notes, we can verify balances match.

### Viewing Key Resilience

The CLI stores imported viewing keys in LiteDB (`PRIV_WALLETS`). They persist across restarts but could be lost if the DB is wiped (redeployment, migration). The web wallet should:
- On session start, call `GET /api/privacy/balance/{zfx_address}/`
- If CLI returns "unknown address" or empty result, auto re-import the viewing key
- Trigger a rescan from block 0 (or last known good block)
- Show "syncing privacy wallet..." with progress during initial scan

### Initial Scan Delay

After importing a viewing key, the CLI scans the blockchain for existing notes. If the user has been shielding on desktop, the initial scan could take minutes. The web wallet should:
- Show a progress indicator ("Scanning blocks... X of Y")
- Allow the user to use other wallet features while scanning
- Poll `GET /api/privacy/balance/{zfx_address}/` until `LastScannedBlock` catches up to chain tip

---

## Phase 2: Poseidon WASM Build

**Goal:** Ship a minimal WASM module (~100-200 KB) containing Poseidon hash from the same Rust source as `plonk_ffi`, guaranteeing byte-identical outputs.

**Depends on:** Aaron creating a `plonk-wasm` crate (or us doing it in the plonk repo).

### Why WASM Instead of JS

We reviewed the `plonk` Rust repo (github.com/VerifiedXBlockchain/plonk). The entire crypto stack is pure Rust (arkworks BLS12-381, no C bindings). A Poseidon-only WASM build is small and eliminates the #1 risk: field parameter mismatches between a JS reimplementation and the native library.

We evaluated a **full WASM build** (all proving + verifying) but rejected it for v1:
- Full binary: 8-15 MB + 10-50 MB prover params
- Proof generation: 10-30s in WASM vs 1-3s native
- The server-side proving tradeoff (server sees amounts during proving) is acceptable

### What's Needed in the Plonk Repo

1. New crate: `plonk-wasm/` (or a `wasm` feature on `plonk-ffi`)
2. Exposes via `wasm-bindgen`:
   - `poseidon_hash(inputs: &[u8]) -> Vec<u8>`
   - `poseidon_note_hash(amount_scaled: u64, randomness: &[u8]) -> Vec<u8>`
   - `nullifier_derive_v1(viewing_key: &[u8], note_hash: &[u8], tree_position: u64) -> Vec<u8>`
3. Changes to support `wasm32-unknown-unknown`:
   - Add `plonk_load_params_from_bytes(ptr, len)` (current uses `std::fs::read`)
   - Disable `getrandom` and `std_rng` features on `rand`/`rand_core`
   - Make `std` feature optional in `plonk-ffi`

### Build & Ship

```bash
wasm-pack build plonk-wasm --target web --release
# Output: plonk_wasm_bg.wasm (~100-200 KB) + plonk_wasm.js (bindings)
```

Host on CDN, lazy-load when user activates privacy features.

### Testing Phase 2

1. Build WASM module, call `poseidon_note_hash` with known inputs
2. Call same function via native CLI (`plonk_ffi`)
3. Verify outputs are byte-identical
4. Test `nullifier_derive_v1` with known viewing key + note hash + position
5. Verify nullifier matches CLI output
6. Test in Chrome, Firefox, Safari, mobile Safari

---

## Phase 3: Shield Operations (T→Z)

**Goal:** Web wallet can shield VFX (move from transparent to shielded pool).

**Depends on:** CLI `PedersenCommit` endpoint. Note hash computed client-side via Poseidon WASM (Phase 2).

### What the Browser Does

1. User enters amount to shield and selects transparent account
2. Browser generates 32 bytes of randomness (`crypto.getRandomValues`)
3. Browser requests Pedersen commitment + note hash from server:
   ```
   POST /api/privacy/compute/commit/
   Body: { amount_scaled: 150000000, randomness_b64: "..." }
   Response: { commitment_b64: "...", note_hash_b64: "..." }
   ```
4. Browser encrypts the output note:
   - Derive ephemeral secp256k1 keypair
   - ECDH shared secret with recipient's zfx_ encryption pubkey
   - AES-256-GCM encrypt the `ShieldedPlainNote` JSON
   - Assemble sealed blob: `0x01 | ephPub33 | nonce12 | tag16 | ciphertext`
5. Browser builds the `PrivateTxPayload` JSON:
   ```json
   {
     "v": 1,
     "kind": "shield",
     "sub_type": "Shield",
     "asset": "VFX",
     "outs": [{ "i": 0, "c": "<commitment_b64>", "nh": "<note_hash_b64>", "note": "<encrypted_note_b64>" }],
     "nulls": [],
     "spent_tree_positions": [],
     "transparent_input": "<from_address>",
     "transparent_amount": 1.5
   }
   ```
6. Browser builds the VFX transaction (Type 31):
   - FromAddress: transparent address
   - ToAddress: "Shielded_Pool"
   - Amount: shield amount
   - Fee: from `/api/raw/fee/`
   - Data: the payload JSON above
   - Nonce: from `/api/raw/nonce/{address}/`
7. Get hash → sign with transparent key → broadcast via `/api/raw/send/`

### Spyglass Changes

#### Add to: `api/privacy/views.py`

| View | Method | Path | Purpose |
|------|--------|------|---------|
| `PedersenCommitView` | POST | `/api/privacy/compute/commit/` | Proxy to CLI PedersenCommit |
| `PoseidonHashView` | POST | `/api/privacy/compute/poseidon/` | Proxy to CLI PoseidonHash (fallback) |

#### Add to: `rbx/client.py`

```python
def privacy_pedersen_commit(amount_scaled, randomness_b64)
def privacy_poseidon_hash(inputs_b64)
```

### Note Encryption in Browser (JS)

The encryption scheme is well-defined and uses standard primitives:
- secp256k1 ECDH → `noble-secp256k1` or `@noble/secp256k1`
- SHA-256 for key derivation → `WebCrypto.subtle.digest`
- AES-256-GCM → `WebCrypto.subtle.encrypt`
- Domain strings match CLI: `"VFX/shielded/note-aes/v1"`, `"VFX/shielded/note-aad/v1"`

Need to implement `ShieldedNoteEncryption.SealPlainNote()` equivalent in JS. The wire format is:
```
0x01 | ephPub33(33) | nonce12(12) | tag16(16) | ciphertext(var)
```

### TX Hash Computation (Privacy-Specific)

Privacy TXs use `BuildPrivate()` not `Build()` for hash computation. The hash differs by TX type:

**Shield (T→Z) — types 31, 34:**
```
Hash = DoubleSHA256(Timestamp + TransactionType + Data + FromAddress + Amount + Nonce)
```

**Spend (Z→T, Z→Z) — types 32, 33, 35, 36:**
```
Hash = DoubleSHA256(Timestamp + TransactionType + Data)
```

Where `+` is string concatenation and `DoubleSHA256 = SHA256(SHA256(input))`.

The web wallet must compute this locally (double-SHA256 is trivial via WebCrypto). Do NOT use `/api/raw/hash/` for privacy TXs — it may not route to `BuildPrivate()`.

### Spend TX Special Fields

For spend TXs (unshield, private transfer), the TX has non-standard fields:
- `Signature`: literal string `"PLONK"` (not a real signature)
- `Nonce`: `0`
- `Fee`: `0`
- `Amount`: `0`
- `FromAddress`: `"Shielded_Pool"` (sentinel)

The CLI's `PrivateTransactionValidatorService` checks for the `"PLONK"` sentinel and validates via PLONK proof instead of signature verification.

### Amount Scaling Factor

All amounts in witness bytes use a scaling factor of **10^8** (100,000,000). Defined in `GlobalsPrivacy.cs:27`.

Example: 1.5 VFX → `150000000` as u64 little-endian (8 bytes).

Getting this wrong means invalid proofs. The browser must use this exact factor.

### Password Not Needed

The desktop uses a `WalletPassword` to decrypt the spending key stored on the CLI's disk (AES-256-GCM + PBKDF2, 120k iterations). In the web wallet, the spending key is derived from the seed already in browser memory — **no password flow is needed**. This simplifies the UX significantly compared to the desktop.

The web wallet derives keys on login:
1. User provides seed/mnemonic → browser derives BIP32 master key
2. Browser derives shielded keys: `m/44'/{coinType}'/0'/1'/{addressIndex}'`
3. Spending key, viewing key, encryption key all in memory
4. No password prompt for privacy operations

### Testing Phase 3

1. Generate randomness in browser, get commitment from server
2. Verify commitment matches what CLI would produce for same inputs (test vectors)
3. Encrypt a note in JS, decrypt it on CLI (or vice versa) — verify interoperability
4. Build a complete Shield TX payload with correct `BuildPrivate()` hash
5. Submit to testnet via `/api/raw/send/` — verify CLI accepts it
6. Verify the TX appears on-chain with correct type (31)
7. Verify the shielded balance updates after the TX is indexed
8. Verify the desktop GUI can see/decrypt the note created by the web wallet

---

## Phase 4: Spend Operations (Z→T, Z→Z)

**Goal:** Web wallet can unshield VFX and send private transfers.

**Depends on:** CLI `GenerateProof` + `GetMerkleProof` endpoints. Poseidon WASM (Phase 2) for nullifier derivation.

### What the Browser Does

1. Fetch unspent notes via `GET /api/privacy/balance/{zfx_address}/`
2. Select 1-2 inputs that cover the amount + fee
3. For each input, compute nullifier locally:
   - `nullifier = Poseidon(viewingKey32, noteHash32, treePosition)`
   - This is where Poseidon JS compatibility is critical
4. Fetch Merkle proofs for selected inputs:
   ```
   GET /api/privacy/merkle-proof/?asset=VFX&position=42
   Response: { proof_b64, root_b64, leaf_count }
   ```
5. Build witness bytes (per PlonkProverV1.cs format):
   - Transfer: 4384 bytes (2 inputs × 2128B + 2 outputs × 40B + fee 8B + root 32B)
   - Unshield: 4344 bytes
6. Send witness to proof server:
   ```
   POST /api/privacy/compute/prove/
   Body: { circuit_type: "unshield", witness_data_b64: "..." }
   Response: { proof_b64: "...", public_inputs_b64: "..." }
   ```
7. Build output notes (change output for self, payment output for recipient)
   - Encrypt each note with the recipient's zfx_ encryption pubkey
8. Assemble the full `PrivateTxPayload`:
   ```json
   {
     "v": 1,
     "kind": "unshield",
     "asset": "VFX",
     "outs": [{ change_note }],
     "nulls": ["nullifier1_b64", "nullifier2_b64"],
     "spent_tree_positions": [42, 58],
     "spent_commitments": ["commitment1_b64", "commitment2_b64"],
     "merkle_root": "root_b64",
     "proof_b64": "proof_from_server",
     "transparent_output": "RBx...",
     "transparent_amount": 1.0,
     "fee": 0.000003
   }
   ```
9. Build TX (Type 32 for unshield, 33 for private transfer):
   - FromAddress: "Shielded_Pool"
   - ToAddress: destination (transparent or "Shielded_Pool")
   - Amount: 0
   - Fee: 0
   - Signature: "PLONK" (sentinel value)
   - Nonce: 0
10. Compute TX hash and broadcast via `/api/raw/send/`

### Spyglass Changes

| View | Method | Path | Purpose |
|------|--------|------|---------|
| `GenerateProofView` | POST | `/api/privacy/compute/prove/` | Proxy to CLI GenerateProof |
| `MerkleProofView` | GET | `/api/privacy/merkle-proof/` | Proxy to CLI GetMerkleProof |

### Witness Byte Format (Must Match PlonkProverV1.cs Exactly)

**Transfer/Unshield Input (2128 bytes per input):**
```
amount:       8 bytes (u64 LE, scaled by 10^8)
randomness:   32 bytes
nullifier:    32 bytes
treePosition: 8 bytes (u64 LE)
merklePath:   2048 bytes (32 siblings × (32 bytes value + 32 bytes direction))
```

**Output (40 bytes per output):**
```
amount:     8 bytes (u64 LE, scaled)
randomness: 32 bytes
```

**Full witness:**
```
Transfer: 2 inputs (4256B) + 2 outputs (80B) + fee (8B) + root (32B) = 4376B padded to 4384B
Unshield: similar layout, 4344B
Fee:      1 input (2128B) + 1 output (40B) + fee (8B) + root (32B) = 2208B
```

### Poseidon (via WASM from Phase 2)

Nullifier derivation uses the Poseidon WASM module built in Phase 2. No JS reimplementation needed — same Rust code as the native CLI, guaranteed compatible.

### Multi-Device Conflict Handling

If a user spends a note from the desktop while the web wallet has it selected:
- The CLI rejects the TX with "nullifier already spent"
- Web wallet catches this error, refreshes the note set via `GET /api/privacy/balance/`
- Shows "Your balance has changed — a note was spent from another device. Please retry."
- Auto-reselects inputs and retries if the remaining balance covers the amount

### Proof Server Rate Limiting (Spyglass Side)

The `POST /api/privacy/compute/prove/` endpoint needs rate limiting since proof generation is CPU-heavy (1-3s) and the CLI's FFI serializes through a Mutex:
- Rate limit: max 4 concurrent proof requests per CLI instance
- Queue overflow: return HTTP 503 with `Retry-After` header
- Per-IP limit: max 2 concurrent proof requests (prevent one user from monopolizing)
- Timeout: 30s max wait in queue before returning 503

### Auto-Consolidation Logic

When a user tries to spend more than their largest 2 notes can cover:
1. Detect: `requested_amount > sum(top_2_notes)`
2. Prompt: "Your balance is spread across many small notes. Consolidate first?"
3. Auto-consolidate: merge 2 smallest notes → 1 (Z→Z to self)
4. Wait for confirmation (~1 block)
5. Repeat until spendable
6. Proceed with original operation

### Merkle Root Staleness Handling

The network allows roots within `MaxMerkleRootAge = 100 blocks` (~30 min).

- Fetch root when building TX
- If broadcast returns "stale root" error:
  - Re-fetch Merkle proofs with fresh root
  - Rebuild witness
  - Re-generate proof
  - Re-broadcast
- Show user "refreshing proof..." rather than an error

### Testing Phase 4

1. **Nullifier test:** Compute nullifiers via Poseidon WASM in browser, verify they match CLI output for same inputs
2. **Witness format test:** Build witness bytes in JS, send to CLI `GenerateProof`, verify proof is returned (not rejected for format error)
3. **Unshield end-to-end:** Shield some VFX via Phase 2, then unshield back to transparent address
4. **Private transfer end-to-end:** Shield VFX, send to a different zfx_ address, verify recipient can see the note
5. **Cross-wallet test:** Shield on web wallet, unshield on desktop GUI (and vice versa)
6. **Consolidation test:** Create 5+ small shielded notes, verify consolidation merges them
7. **Stale root test:** Build a TX, wait for a new block, verify the TX still broadcasts (within 100-block window)
8. **Double-spend test:** Try to spend the same note twice, verify second TX is rejected
9. **Multi-device test:** Spend a note from desktop while web wallet has it selected, verify web wallet handles "nullifier already spent" gracefully
10. **Proof server load test:** Submit 5+ concurrent proof requests, verify queuing/rate limiting works
11. **Amount scaling test:** Verify 1.5 VFX → 150000000 u64 LE → valid proof (scaling factor = 10^8)

---

## Phase 5: vBTC Privacy

**Goal:** Shield/Unshield/Transfer vBTC through the shielded pool.

**Depends on:** Phase 4 complete.

### Differences from VFX

- Asset key: `"VBTC:{contract_uid}"` instead of `"VFX"`
- TX types: 34 (shield), 35 (unshield), 36 (private transfer)
- Dual-fee model: vBTC operations pay a VFX fee from the shielded VFX pool
  - Requires a separate fee circuit proof (`PlonkCircuitType.Fee`)
  - Additional payload fields: `fee_proof_b64`, `fee_input_nullifier_b64`, `fee_output_commitment_b64`, `fee_tree_merkle_root`, etc.
- User needs shielded VFX balance to pay fees when doing vBTC privacy operations

### Additional Spyglass Endpoints

- `GET /api/privacy/vbtc/pool-state/{sc_uid}/` → proxy to CLI

### Testing Phase 5

1. Shield vBTC via web wallet, verify on desktop
2. Unshield vBTC, verify transparent balance updates
3. Verify fee deduction from shielded VFX pool
4. Test with insufficient shielded VFX for fees — verify clear error
5. Multi-device: shield vBTC on desktop, unshield on web (and vice versa)

---

## Phase 6: Consolidation + Polish

**Goal:** Auto-consolidation, recovery scanning, edge case handling.

### Features

- Auto-consolidation prompt when notes are fragmented
- Manual resync/recovery scanning (`POST /api/privacy/rescan/`)
- Privacy balance in wallet overview
- Transaction history for shielded operations
- zfx_ address display + copy + QR
- Viewing key auto-reimport on session start (resilience against CLI DB wipes)
- Multi-device conflict handling (nullifier-already-spent → refresh + retry)
- Graceful handling of proof server unavailability (503 → "service busy, try again")

---

## Summary: New Endpoints by Phase

### Phase 1 — Read-Only (no CLI changes)
```
GET  /api/privacy/plonk-status/
GET  /api/privacy/balance/{zfx_address}/
GET  /api/privacy/pool-state/
GET  /api/privacy/vbtc/balance/{zfx_address}/{sc_uid}/
POST /api/privacy/import-viewing-key/
POST /api/privacy/scan/
POST /api/privacy/vbtc/scan/
```

### Phase 2 — Poseidon WASM (plonk repo, Aaron or us)
```
No Spyglass endpoints. Ships a ~100-200 KB WASM module to the browser.
Exposes: poseidon_hash, poseidon_note_hash, nullifier_derive_v1
```

### Phase 3 — Shield (needs CLI: PedersenCommit)
```
POST /api/privacy/compute/commit/
```

### Phase 4 — Spend (needs CLI: GenerateProof, MerkleProof)
```
POST /api/privacy/compute/prove/
GET  /api/privacy/merkle-proof/
```

### Phase 5 — vBTC Privacy (no new endpoints, reuses Phase 1-4)
```
GET  /api/privacy/vbtc/pool-state/{sc_uid}/
```

## Timeline Estimate

| Phase | Spyglass Work | CLI / Rust Work (Aaron) | Web Wallet Work | Elapsed |
|-------|---------------|------------------------|-----------------|---------|
| 1 | 2-3 days | None | 3-5 days | 1-2 weeks |
| 2 | None | `plonk-wasm` crate (1-2 days) | WASM integration + testing (3-5 days) | 1-2 weeks |
| 3 | 1-2 days | PedersenCommit endpoint (1 day) | Note encryption, TX building (1-2 weeks) | 2-3 weeks |
| 4 | 1-2 days | GenerateProof + MerkleProof (2-3 days) | Witness building, spending flow (2-3 weeks) | 3-4 weeks |
| 5 | 1 day | None | Dual-fee model (1 week) | 1 week |
| 6 | — | — | UX polish (1 week) | 1 week |
| **Total** | **~1 week** | **~5-6 days** | **~7-10 weeks** | **~9-12 weeks** |

The critical path is the web wallet crypto implementation (Phases 3-4). The Poseidon WASM build (Phase 2) should happen early since it unblocks Phase 4 nullifier work. Spyglass and CLI work can proceed in parallel with web wallet development.

### Parallelization

```
Week 1-2:  Phase 1 (Spyglass + web wallet read-only)
           Phase 2 (Poseidon WASM build — Aaron or us in plonk repo)
Week 3-5:  Phase 3 (Shield — CLI PedersenCommit + web wallet TX building)
Week 5-9:  Phase 4 (Spend — CLI GenerateProof/MerkleProof + web wallet witness/spending)
Week 9-10: Phase 5 (vBTC privacy — web wallet only)
Week 10-12: Phase 6 (Consolidation, polish, edge cases)
```

Phases 1 and 2 can run fully in parallel. Phase 3's Spyglass work can overlap with Phase 2's WASM testing.
