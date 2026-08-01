# VFX Web SDK — vBTC V2 Integration Spec

> For the agent working on /Users/tyler/prj/vfx/vfx-web-sdk
> Date: 2026-05-29

## Overview

Replace all V1 vBTC methods with V2. The V1 methods (`sendVbtc`, `withdrawVbtc`, `completeVbtcWithdrawal`) should be removed. V2 uses a two-step prepare/sign/send pattern for all operations, talking to the Spyglass API.

## Spyglass Base URLs

- Mainnet: `https://data.verifiedx.io/api`
- Testnet: `https://data-testnet.verifiedx.io/api`

## Existing SDK Patterns to Follow

The SDK already has the building blocks:
- `KeypairService.getSignature(message, privateKeyHex)` — secp256k1 ECDSA signing, returns `${signatureBase64}.${publicKeyBase58}`
- `VfxClient` exposes all methods as instance methods
- `RawTransactionApiClient` handles `/raw/*` endpoints
- Network is set at client construction: `new VfxClient('mainnet')`

## New API Client

Create a `VbtcV2ApiClient` (or add to existing `RawTransactionApiClient`) with methods for all V2 Spyglass endpoints.

### Read Endpoints

```
GET /btc/vbtc-v2/                              → list all V2 tokens
GET /btc/vbtc-v2/{vfx_address}/                → list tokens for address
GET /btc/vbtc-v2/detail/{sc_identifier}/        → token detail (live BTC balance)
GET /btc/vbtc-v2/transfers/{sc_identifier}/     → transfer history
GET /btc/vbtc-v2/withdrawals/{sc_identifier}/   → withdrawal history
```

### Two-Step Write Pattern

Every write operation follows: prepare (Spyglass builds unsigned TX via CLI) → sign locally → send (Spyglass broadcasts).

The "prepare" step returns a `Hash` field. The SDK signs this hash with `getSignature(hash, privateKey)`. The "send" step accepts `{ hash, signature, public_key }`.

Exception: MPC ceremony and FROST signing use message signing instead of TX hash signing.

## Operations

### 1. List Tokens

```typescript
async getVbtcTokens(address: string): Promise<VbtcV2Token[]>
// GET /btc/vbtc-v2/{address}/
// Response: { results: VbtcV2Token[] }
```

### 2. Token Detail

```typescript
async getVbtcTokenDetail(scIdentifier: string): Promise<VbtcV2Token>
// GET /btc/vbtc-v2/detail/{scIdentifier}/
// Response: VbtcV2Token (with live BTC balance, addresses map, withdrawal_requests)
```

### 3. Create Token (MPC Ceremony)

Three-phase flow: ceremony → contract creation.

```typescript
async createVbtcToken(params: {
  ownerAddress: string;
  privateKey: string;
  name: string;
  description: string;
  ticker: string;
}): Promise<CreateVbtcResult>
```

**Phase 1: MPC Ceremony**

```
POST /btc/vbtc-v2/ceremony/prepare/
Body: { owner_address }
Response: {
  success, ceremony_id, session_id,
  messages_to_sign: {
    start_message, start_timestamp,
    share_distribution_message, share_distribution_timestamp
  },
  validator_count, threshold
}
```

Sign both `start_message` and `share_distribution_message` with `getSignature()`.

```
POST /btc/vbtc-v2/ceremony/execute/
Body: {
  ceremony_id, session_id, owner_address,
  start_signature, start_timestamp,
  share_distribution_signature, share_distribution_timestamp
}
Response: { success }
```

Poll until complete:
```
GET /btc/vbtc-v2/ceremony/{ceremony_id}/
Response: { success, status, progress, message }
// Poll every 3-5s. Status: "Completed" | "Failed" | "Round1InProgress" etc.
```

**Phase 2: Contract Creation**

Generate a timestamp (unix seconds), a unique ID (16 random alphanumeric chars), and sign the ownership proof:

```
Ownership proof message = "{ownerAddress}{name}{description}{ticker}{ceremonyId}{timestamp}{uniqueId}"
(string concatenation, no separators)
```

Sign this with `getSignature()`.

```
POST /btc/vbtc-v2/create/prepare/
Body: {
  owner_address, name, description, ticker, ceremony_id,
  timestamp, unique_id, owner_signature
}
Response: { success, Hash, SmartContractUID, DepositAddress, Fee }
```

Sign the `Hash` with `getSignature()`.

```
POST /btc/vbtc-v2/create/send/
Body: { hash, signature, public_key }
Response: { success, Hash }
```

### 4. Transfer vBTC

```typescript
async transferVbtc(params: {
  scIdentifier: string;
  fromAddress: string;
  toAddress: string;
  amount: number;
  privateKey: string;
}): Promise<TransferResult>
```

```
POST /btc/vbtc-v2/transfer/prepare/
Body: { sc_identifier, from_address, to_address, amount }
Response: { success, Hash, Fee, ... }
```

Sign `Hash` with `getSignature()`.

```
POST /btc/vbtc-v2/transfer/send/
Body: { hash, signature, public_key }
Response: { success, Hash }
```

### 5. Withdrawal (4-Step Flow)

```typescript
async requestWithdrawal(params: {
  scIdentifier: string;
  requestorAddress: string;
  btcAddress: string;
  amount: number;
  feeRate: number;
  privateKey: string;
}): Promise<WithdrawalResult>
```

**Step 1: Request (Type 27 TX)**

```
POST /btc/vbtc-v2/withdraw/request/prepare/
Body: { sc_identifier, requestor_address, btc_address, amount, fee_rate }
Response: { success, Hash, Fee, ... }
```

Sign `Hash`, send via:
```
POST /btc/vbtc-v2/withdraw/request/send/
Body: { hash, signature, public_key }
Response: { success, Hash }
// Save this Hash — it's the withdrawal_request_hash
```

**Step 2: Prepare FROST**

```
POST /btc/vbtc-v2/withdraw/complete/prepare/
Body: { sc_identifier, withdrawal_request_hash, owner_address }
Response: {
  success, SessionId,
  StartMessage, StartTimestamp,
  ShareDistributionMessage, ShareDistributionTimestamp,
  Amount, BTCDestination, FeeRate
}
```

Sign both `StartMessage` and `ShareDistributionMessage` with `getSignature()`.

**Step 3: Execute FROST (Async)**

```
POST /btc/vbtc-v2/withdraw/complete/execute/
Body: {
  sc_identifier, withdrawal_request_hash, owner_address,
  session_id, start_signature, start_timestamp,
  share_distribution_signature, share_distribution_timestamp,
  amount, btc_destination, fee_rate
}
Response: { success, job_id }
```

Poll for result (every 5s, up to 3 minutes):
```
GET /btc/vbtc-v2/withdraw/complete/status/{job_id}/
Response:
  Pending: { success: true, status: "pending" }
  Complete: { success: true, status: "complete", signed_btc_tx_hex, sc_identifier, withdrawal_request_hash }
  Failed: { success: false, status: "failed", message }
```

When complete, broadcast the BTC transaction:
```
POST /btc/broadcast/
Body: { raw_tx_hex: signed_btc_tx_hex }
Response: { success, txid }
```

**Step 4: Record Completion (Type 28 TX)**

```
POST /btc/vbtc-v2/withdraw/complete/tx/prepare/
Body: {
  sc_identifier, from_address, withdrawal_request_hash,
  btc_transaction_hash (txid from broadcast), amount, btc_destination
}
Response: { success, Hash, Fee (0), ... }
```

Sign `Hash`, send via:
```
POST /btc/vbtc-v2/withdraw/complete/tx/send/
Body: { hash, signature, public_key }
Response: { success, Hash }
```

### 6. Cancel Withdrawal

```typescript
async cancelWithdrawal(params: {
  scIdentifier: string;
  ownerAddress: string;
  withdrawalRequestHash: string;
  privateKey: string;
}): Promise<CancelResult>
```

```
POST /btc/vbtc-v2/withdraw/cancel/prepare/
Body: { sc_identifier, owner_address, withdrawal_request_hash }
Response: { success, Hash, Fee, ... }
```

Sign `Hash`, send via:
```
POST /btc/vbtc-v2/withdraw/cancel/send/
Body: { hash, signature, public_key }
Response: { success, Hash }
```

## Types

```typescript
interface VbtcV2Token {
  sc_identifier: string;
  name: string;
  description: string;
  owner_address: string;
  image_url: string;
  deposit_address: string;
  frost_group_public_key: string;
  required_threshold: number;
  proof_block_height: number;
  global_balance: number;
  total_received: number;
  total_sent: number;
  tx_count: number;
  is_pending_withdrawal: boolean;
  addresses: Record<string, number>;  // vfx_address → vbtc_balance
  nft: any;
  withdrawal_requests: WithdrawalRequest[];
  created_at: string;
}

interface WithdrawalRequest {
  id: number;
  requestor_address: string;
  btc_address: string;
  amount: string;
  fee_rate: string;
  btc_transaction_hash: string;
  status: 'requested' | 'completed';
  request_transaction_hash: string;
  completion_transaction_hash: string | null;
  created_at: string;
  completed_at: string | null;
}

interface CreateVbtcResult {
  transactionHash: string;
  scIdentifier: string;
  depositAddress: string;
}

interface TransferResult {
  transactionHash: string;
}

interface WithdrawalResult {
  btcTransactionHash: string;
}

interface CancelResult {
  transactionHash: string;
}
```

## TxType Constants to Add

```typescript
enum TxType {
  // ... existing ...
  VbtcV2ContractCreate = 25,
  VbtcV2Transfer = 26,
  VbtcV2WithdrawalRequest = 27,
  VbtcV2WithdrawalComplete = 28,
  VbtcV2WithdrawalCancel = 29,
}
```

## What to Remove

- `sendVbtc()` — V1 transfer using `TransferCoin()` + Type 18
- `withdrawVbtc()` — V1 withdrawal using `/raw/withdraw-vbtc/`
- `completeVbtcWithdrawal()` — V1 completion using `TokenizedWithdrawalComplete()` + Type 21
- `VbtcWithdrawRequest` interface — V1 withdrawal payload
- `VbtcWithdrawalResult` interface — V1 withdrawal result
- `TxType.TokenizeTx` (18) and `TxType.TokenizedWithdrawal` (21) if only used for vBTC

## Signing Reference

Two types of things get signed:

1. **TX hashes** (from prepare endpoints) — sign the `Hash` string directly:
   ```typescript
   const signature = keypairService.getSignature(hash, privateKey);
   ```

2. **Ceremony/FROST messages** — sign the plain UTF-8 message string:
   ```typescript
   const signature = keypairService.getSignature(startMessage, privateKey);
   ```

Both use the same `getSignature()` method. The signature format is `${base64DERSignature}.${base58PublicKey}`.

For the "send" step, `public_key` is the full hex public key (not base58):
```typescript
const publicKey = keypairService.publicFromPrivate(normalizePrivateKey(privateKey));
```

## Testing

Each operation can be tested independently:
1. **List/Detail** — read-only, no signing needed
2. **Transfer** — needs a token with balance
3. **Create** — takes 30-90s for MPC ceremony
4. **Withdrawal** — full 4-step flow, needs BTC on deposit address, FROST signing takes 30-60s
5. **Cancel** — needs an active withdrawal to cancel
