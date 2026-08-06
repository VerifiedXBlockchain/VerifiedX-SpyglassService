# CLI Change Proposal: Remote Address Support for vBTC V2 Operations

> For: Aaron/Jay
> From: Tyler
> Date: 2026-05-25

## Problem

Multiple vBTC V2 CLI endpoints require `AccountData.GetSingleAccount(address)` — meaning the address must have a private key in the CLI's local wallet database. This blocks **all** web wallet V2 operations that go through the CLI, since web wallet users' keys live in the browser.

### Affected Endpoints

| Endpoint | Where it fails | What it needs the key for |
|----------|---------------|--------------------------|
| `InitiateMPCCeremony` | `FrostMPCService.cs:280` via `SignatureService.AddressSignature()` | Leader signature for DKG broadcast |
| `CompleteWithdrawal` | `VBTCService.cs:916` via `GetSingleAccount(fromAddress)` | Leader signature for FROST signing + building the VFX completion TX (Type 28) |
| `CancelWithdrawal` | `VBTCService.cs:1021` via `GetSingleAccount(requestorAddress)` | Building the cancellation TX |

### NOT affected (web wallet handles these as raw TXs, no CLI involvement)

| Operation | TX Type | How web wallet handles it |
|-----------|---------|--------------------------|
| Transfer vBTC | 26 | Raw TX built + signed in browser, broadcast via `/api/raw/send/` |
| Withdrawal Request | 27 | Raw TX built + signed in browser, broadcast via `/api/raw/send/` |

## Root Cause

The common constraint is `AccountData.GetSingleAccount(address)` which only returns accounts with private keys in the local wallet DB. This is called in two contexts:

1. **Signing messages** (leader signatures for FROST/DKG coordination):
   - `SignatureService.AddressSignature()` at `SignatureService.cs:111-128`
   - Used by `FrostMPCService.CoordinateDKGCeremony()` and `CompleteWithdrawal()`

2. **Building transactions** (VFX chain TXs like Type 28 withdrawal complete):
   - `VBTCService.cs:916` builds a Type 28 TX using the requestor's account
   - `VBTCService.cs:1021` builds a cancellation TX using the requestor's account

## Proposed Fix: Accept Pre-Signed Signature

Allow the API caller to provide a signature, bypassing the local signing step. The web wallet signs the message in-browser (secp256k1) and passes it through the Spyglass API.

### Flow

```
1. Web wallet calls: POST /InitiateMPCCeremony/{ownerAddress}
   Body: { "Signature": "<signed_message>" }

2. CLI generates sessionId + timestamp internally
3. CLI constructs the expected message: "{sessionId}.{ownerAddress}.{timestamp}"
4. CLI verifies the provided signature against the ownerAddress
5. If valid → proceed with ceremony using that signature as the leaderSignature
6. Validators verify as normal (unchanged)
```

Wait — the signature needs to match the message format `{sessionId}.{ownerAddress}.{timestamp}`, but sessionId and timestamp are generated server-side. So the caller can't pre-sign the exact message.

### Revised approach: CLI generates message, caller signs a simpler proof

The signature just needs to prove address ownership. The CLI can accept a generic ownership proof and then generate the actual leader signature internally using a **delegated signing model**:

**Option A — Ownership proof + CLI re-signs with a coordinator key:**

```
1. Caller provides: ownerAddress + ownership proof (sign a fixed message like "VBTC_MPC_AUTH.{ownerAddress}")
2. CLI verifies the ownership proof against ownerAddress
3. CLI generates sessionId + timestamp
4. CLI signs the leader message with its own validator/coordinator key
5. Validators verify against the coordinator address instead of owner address
```

This requires validators to accept the coordinator's signature rather than the owner's.

**Option B (simpler) — Pre-generate ceremony ID, return message to sign, then start:**

Split into two sub-steps within the same endpoint:

```
1. POST /InitiateMPCCeremony/{ownerAddress}
   - CLI generates ceremonyId + timestamp
   - CLI constructs message: "{ceremonyId}.{ownerAddress}.{timestamp}"
   - Returns: { "CeremonyId": "...", "MessageToSign": "...", "Status": "AwaitingSignature" }

2. POST /ConfirmMPCCeremony/{ceremonyId}
   Body: { "Signature": "<owner_signs_MessageToSign>" }
   - CLI verifies signature against ownerAddress
   - Proceeds with DKG broadcast using this signature as leaderSignature
   - Returns: { "Status": "ValidatingValidators" }
```

**Option C (simplest, recommended) — Accept signature of a deterministic message:**

The message to sign is deterministic from the caller's perspective:

```
Message format: "MPC_CEREMONY.{ownerAddress}.{ceremonyId}"
```

Modify the flow:
1. Caller generates their own ceremonyId (UUID) or the CLI returns one from a lightweight pre-init call
2. Caller signs `"MPC_CEREMONY.{ownerAddress}.{ceremonyId}"` in-browser
3. Caller calls `POST /InitiateMPCCeremony/{ownerAddress}` with `{ "CeremonyId": "...", "Signature": "..." }`
4. CLI verifies signature, then uses it (or re-constructs the actual leader message and signs with the validated identity)

---

## Recommended Approach: Option B (Two-Step)

Most compatible with validator verification logic (no changes to `FrostStartup.cs`). The leader signature format stays the same — validators don't need updates.

### Changes Required

#### `VBTCController.cs`

**Modify `InitiateMPCCeremony` (~line 437):**
- Accept optional `Signature` field in request body
- If no signature provided AND address is not local → return ceremony ID + message to sign (status: `AwaitingSignature`)
- If signature provided → validate and proceed

**Add new endpoint `ConfirmMPCCeremony` (~line 512):**
```csharp
[HttpPost("ConfirmMPCCeremony/{ceremonyId}")]
public async Task<string> ConfirmMPCCeremony(string ceremonyId)
{
    var body = await ReadRequestBody();
    var signature = GetJsonValue(body, "Signature");
    
    var ceremony = MPCCeremonyManager.GetCeremony(ceremonyId);
    if (ceremony == null) return Error("Ceremony not found");
    
    // Verify signature matches the message we gave them
    var message = $"{ceremony.SessionId}.{ceremony.OwnerAddress}.{ceremony.Timestamp}";
    var valid = SignatureService.VerifySignature(ceremony.OwnerAddress, message, signature);
    if (!valid) return Error("Invalid signature");
    
    // Store signature and proceed with ceremony
    ceremony.LeaderSignature = signature;
    _ = Task.Run(() => ExecuteMPCCeremony(ceremonyId));
    
    return Success(ceremonyId);
}
```

#### `FrostMPCService.cs` (~line 280-297)

**Modify `CoordinateDKGCeremony`:**
```csharp
// Instead of:
var leaderSignature = SignatureService.AddressSignature(leaderAddress, leaderMessage);

// Do:
var leaderSignature = ceremony.LeaderSignature 
    ?? SignatureService.AddressSignature(leaderAddress, leaderMessage);
```

Falls back to local signing if no pre-provided signature (backward compatible for desktop/CLI usage).

#### `MPCCeremonyState.cs`

Add field:
```csharp
public string? LeaderSignature { get; set; }  // Pre-provided by external caller
public string? MessageToSign { get; set; }     // Returned to caller for signing
```

Add status:
```csharp
AwaitingSignature  // Waiting for external signature before starting DKG
```

### No changes to:
- `FrostStartup.cs` (validator verification unchanged)
- `SignatureService.cs` (just bypassed, not modified)
- Any validator-side code

---

## Spyglass (Explorer API) Changes

Once the CLI supports this, Spyglass ceremony initiate becomes a two-step flow:

```
1. POST /api/btc/vbtc-v2/ceremony/initiate/
   Body: { "owner_address": "R..." }
   Response: { "ceremony_id": "...", "message_to_sign": "...", "status": "awaiting_signature" }

2. POST /api/btc/vbtc-v2/ceremony/confirm/
   Body: { "ceremony_id": "...", "signature": "..." }
   Response: { "success": true, "status": "validating_validators" }

3. GET /api/btc/vbtc-v2/ceremony/{ceremony_id}/  (unchanged — poll status)
```

The web wallet handles step 1→2 by signing the message with the user's key in-browser.

---

## Summary

| What | Where | Effort |
|------|-------|--------|
| Add `AwaitingSignature` state | `MPCCeremonyState.cs` | 5 min |
| Add `LeaderSignature` field to state | `MPCCeremonyState.cs` | 5 min |
| Return message-to-sign when address not local | `VBTCController.cs` | 15 min |
| Add `ConfirmMPCCeremony` endpoint | `VBTCController.cs` | 20 min |
| Use pre-provided signature in DKG | `FrostMPCService.cs` | 5 min |
| **Total** | | **~50 min** |

No validator-side changes. Fully backward compatible (local addresses still auto-sign).

---

## Part 2: CompleteWithdrawal (FROST Signing)

### The Same Two Problems

`VBTCService.CompleteWithdrawal()` (`VBTCService.cs:649-960`) requires local key ownership for:

1. **FROST leader signature** (~line 658) — same `AddressSignature` pattern as MPC ceremony
2. **VFX completion TX** (~line 916) — builds a Type 28 TX using `GetSingleAccount(fromAddress)` to get the requestor's private key for signing the VFX transaction

### Proposed Fix: Same Two-Step Pattern

#### Problem 1: FROST Leader Signature

Same fix as MPC ceremony — accept a pre-provided signature.

The FROST coordination in `CompleteWithdrawal` calls `FrostMPCService` which uses the same `AddressSignature` for the leader role. Apply the same bypass:

**`VBTCService.cs` / `FrostMPCService.cs`:**
```csharp
// Use pre-provided signature if available, fall back to local signing
var leaderSignature = preProvidedSignature 
    ?? SignatureService.AddressSignature(leaderAddress, leaderMessage);
```

#### Problem 2: Building the VFX Completion TX (Type 28)

At `VBTCService.cs:916-945`, the CLI builds a Type 28 transaction locally using the requestor's private key. For remote addresses, we have two options:

**Option A (recommended): CLI signs with its own coordinator/validator key**

The Type 28 withdrawal complete TX is a validator-generated confirmation (note: `total_fee: 0` on-chain). It doesn't _need_ to be signed by the requestor — it's a record that the withdrawal was executed. The CLI could sign it with its own validator address:

```csharp
// Instead of:
var account = AccountData.GetSingleAccount(fromAddress);

// For remote addresses, use the validator/coordinator account:
var account = AccountData.GetSingleAccount(fromAddress) 
    ?? AccountData.GetSingleAccount(Globals.ValidatorAddress);

// Adjust fromAddress/toAddress to use validator if signing on behalf
```

This is already how validators work on the network — they create Type 28 TXs confirming withdrawals. The `fromAddress`/`toAddress` on the TX could remain the requestor's address (since it's informational), with the validator signing the TX.

**Option B: Return unsigned TX for client to sign**

The CLI could return the raw unsigned Type 28 TX, the web wallet signs it, and sends it back or broadcasts directly. More complex, less clean.

### Two-Step Flow for Web Wallet

```
1. POST /api/btc/vbtc-v2/withdraw/complete/
   Body: { "sc_identifier": "...", "withdrawal_request_hash": "..." }
   Response: { "message_to_sign": "...", "session_id": "..." }

2. POST /api/btc/vbtc-v2/withdraw/complete/confirm/
   Body: { "session_id": "...", "signature": "..." }
   Response: { "success": true, "vfx_transaction_hash": "...", "btc_transaction_hash": "..." }
```

Or, if using Option A (validator signs the VFX TX), it could be a single step — the web wallet provides the leader signature upfront, and the CLI handles the rest using its own validator key for the VFX TX.

---

## Part 3: CancelWithdrawal

### Same Constraint

`VBTCService.CancelWithdrawal()` at `VBTCService.cs:1016-1021` calls `GetSingleAccount(requestorAddress)` to build a cancellation TX.

### Proposed Fix

Same pattern — accept a pre-provided signature or have the CLI sign the cancellation TX with its coordinator key. Cancellation is simpler than withdrawal complete (no FROST ceremony needed), so Option A (coordinator signs) is cleanest.

---

## Summary: All Changes Needed

### CLI Changes

| What | Where | Pattern | Effort |
|------|-------|---------|--------|
| **MPC Ceremony** | | | |
| Add `AwaitingSignature` state | `MPCCeremonyState.cs` | New status | 5 min |
| Add `LeaderSignature` + `MessageToSign` fields | `MPCCeremonyState.cs` | New fields | 5 min |
| Return message-to-sign for remote addresses | `VBTCController.cs` InitiateMPCCeremony | Two-step flow | 15 min |
| Add `ConfirmMPCCeremony` endpoint | `VBTCController.cs` | New endpoint | 20 min |
| Use pre-provided signature in DKG | `FrostMPCService.cs:280` | Signature bypass | 5 min |
| **Withdrawal Complete** | | | |
| Accept pre-provided leader signature | `VBTCService.cs` CompleteWithdrawal | Same bypass pattern | 10 min |
| Sign VFX TX with validator key for remote addresses | `VBTCService.cs:916` | Coordinator fallback | 15 min |
| Add confirm endpoint or single-step with signature | `VBTCController.cs` | New endpoint or param | 15 min |
| **Withdrawal Cancel** | | | |
| Sign cancel TX with validator key for remote addresses | `VBTCService.cs:1021` | Coordinator fallback | 10 min |
| Accept signature param | `VBTCController.cs` CancelWithdrawal | New param | 10 min |
| **Total** | | | **~2 hours** |

### Spyglass (Explorer API) Changes

Once CLI is updated, Spyglass adds confirm endpoints:

```
POST /api/btc/vbtc-v2/ceremony/initiate/    → returns message_to_sign
POST /api/btc/vbtc-v2/ceremony/confirm/     → accepts signature, starts ceremony
POST /api/btc/vbtc-v2/withdraw/complete/    → may need two-step (depends on CLI approach)
POST /api/btc/vbtc-v2/withdraw/cancel/      → pass signature through
```

### No Changes To

- `FrostStartup.cs` — validator verification unchanged
- `SignatureService.cs` — just bypassed, not modified
- Any validator-side code
- Transfer (Type 26) or Withdrawal Request (Type 27) — these are raw TXs handled by the web wallet directly
