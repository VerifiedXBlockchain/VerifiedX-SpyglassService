# vBTC V2 — Web Wallet Integration Guide

> For the web wallet and explorer frontend teams.
> Last updated: 2026-05-25

## Overview

vBTC V2 replaces v1 tokenization with FROST-based threshold signing and MPC key generation. The desktop wallet talks directly to a local CLI. The web wallet goes through the Spyglass API (this explorer backend), which proxies CLI operations that need server-side coordination.

**Key principle**: Transfer and withdrawal request are **raw transactions** signed client-side. Contract creation (MPC) and withdrawal completion (FROST) require **CLI proxy endpoints**.

---

## API Endpoints

Base URL: `/api/btc/`

### Read Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `vbtc-v2/` | List all V2 tokens |
| GET | `vbtc-v2/{vfx_address}/` | List V2 tokens for a specific address |
| GET | `vbtc-v2/detail/{sc_identifier}/` | Token detail (live BTC balance refresh) |
| GET | `vbtc-v2/transfers/{sc_identifier}/` | Transfer history for a token |
| GET | `vbtc-v2/withdrawals/{sc_identifier}/` | Withdrawal history for a token |

#### Token Detail Response

```json
{
  "sc_identifier": "4bb6f0991eda4c63b89d129514b149e2:1779157633",
  "name": "My vBTC Token",
  "description": "...",
  "owner_address": "RKuUHeM13zJ3cTh69TB5EKT8gUFCrXQtR5",
  "image_url": "https://...",
  "deposit_address": "bc1p...",
  "frost_group_public_key": "04...",
  "required_threshold": 3,
  "proof_block_height": 6489000,
  "global_balance": "0.0012000000000000",
  "total_received": "0.0020000000000000",
  "total_sent": "0.0008000000000000",
  "tx_count": 4,
  "is_pending_withdrawal": false,
  "addresses": {
    "RKuUHeM13zJ3cTh69TB5EKT8gUFCrXQtR5": "0.0010000000000000",
    "RNiQrW3aBUWZhfadqKxPuN46iGaR13ox7P": "0.0002000000000000"
  },
  "nft": { "identifier": "...", "name": "...", "..." : "..." },
  "withdrawal_requests": [
    {
      "id": 1,
      "requestor_address": "R...",
      "btc_address": "bc1q...",
      "amount": "0.001",
      "fee_rate": "10.00000000",
      "btc_transaction_hash": "21463f...",
      "status": "completed",
      "request_transaction_hash": "758ac7...",
      "completion_transaction_hash": "1109602b...",
      "created_at": "2026-05-19T01:38:00.000Z",
      "completed_at": "2026-05-19T01:50:39.000Z"
    }
  ],
  "created_at": "2026-05-19T02:27:16.000Z"
}
```

**Notes:**
- `global_balance` is refreshed live from the Bitcoin chain on every detail request.
- `addresses` is a computed map of VFX address → vBTC balance (accounts for transfers and withdrawals).
- `deposit_address` is the BTC address users send Bitcoin to for depositing into this token.

---

## Operations

### 1. Transfer vBTC (Raw Transaction)

User builds and signs a Type 26 raw transaction, then broadcasts via the existing raw TX endpoint.

**Endpoint**: `POST /api/raw/send/`

**Transaction data payload:**
```json
{
  "Function": "TransferVBTCV2()",
  "ContractUID": "4bb6f0991eda4c63b89d129514b149e2:1779157633",
  "FromAddress": "RKuUHeM13zJ3cTh69TB5EKT8gUFCrXQtR5",
  "ToAddress": "RNiQrW3aBUWZhfadqKxPuN46iGaR13ox7P",
  "Amount": 0.0001
}
```

**TX fields:**
- `type`: 26
- `to_address`: recipient VFX address
- `from_address`: sender VFX address
- `total_amount`: 0 (the actual amount is in the data payload)
- `total_fee`: calculated via `POST /api/raw/fee/`

**Flow:**
1. Build raw TX with the data payload above
2. Get fee via `POST /api/raw/fee/`
3. Get hash via `POST /api/raw/hash/`
4. Sign the hash with sender's private key (secp256k1)
5. Broadcast via `POST /api/raw/send/`

This is the same pattern V1 uses for `TransferCoin()`, just with a different function name and type number.

---

### 2. Withdrawal Request (Raw Transaction)

User builds and signs a Type 27 raw transaction.

**Endpoint**: `POST /api/raw/send/`

**Transaction data payload:**
```json
{
  "Function": "VBTCWithdrawalRequest()",
  "ContractUID": "76c995d525ab49a8956241a0e85db35a:1779140646",
  "RequestorAddress": "RTC7uEaVWVakHwYQMhMDAyNkxYgjzV9WZq",
  "BTCAddress": "bc1qjzgqf9377zd0ts0x3z3sgq4e588txrwaqeca2w",
  "Amount": 0.001,
  "FeeRate": 10
}
```

**TX fields:**
- `type`: 27
- `to_address`: requestor's VFX address (same as from)
- `from_address`: requestor's VFX address
- `total_amount`: 0
- `total_fee`: calculated via `POST /api/raw/fee/`

**Flow:**
1. Build raw TX with the data payload above
2. Fee → hash → sign → broadcast (same as transfer)
3. After broadcast, the tx hash becomes the `withdrawal_request_hash`
4. Call the withdrawal complete endpoint (below) to trigger FROST signing

---

### 3. Withdrawal Complete (CLI Proxy — FROST Signing)

After the withdrawal request TX is confirmed on-chain, call this endpoint to trigger FROST threshold signing, which creates the actual BTC transaction.

**Endpoint**: `POST /api/btc/vbtc-v2/withdraw/complete/`

**Request:**
```json
{
  "sc_identifier": "76c995d525ab49a8956241a0e85db35a:1779140646",
  "withdrawal_request_hash": "758ac79e33f97aeff0f53366811c483c79aac30d7de6fa9c2a12b4fe95e2726e"
}
```

**Response (success):**
```json
{
  "success": true,
  "vfx_transaction_hash": "1109602b4af0ba5c...",
  "btc_transaction_hash": "21463f551e41b88b...",
  "status": "Completed"
}
```

**Important:**
- This endpoint has a **120s+ timeout**. The FROST signing ceremony takes time.
- The web wallet should show a progress/waiting UI during this call.
- If it times out, the withdrawal may still complete — poll the withdrawals endpoint to check.

---

### 4. Withdrawal Cancel (CLI Proxy)

Cancels a pending withdrawal request.

**Endpoint**: `POST /api/btc/vbtc-v2/withdraw/cancel/`

**Request:**
```json
{
  "sc_identifier": "76c995d525ab49a8956241a0e85db35a:1779140646",
  "owner_address": "RTC7uEaVWVakHwYQMhMDAyNkxYgjzV9WZq",
  "withdrawal_request_hash": "758ac79e33f97aeff0f53366811c483c79aac30d7de6fa9c2a12b4fe95e2726e",
  "btc_tx_hash": "",
  "failure_proof": ""
}
```

**Response:**
```json
{
  "success": true
}
```

---

### 5. Create vBTC V2 Token (CLI Proxy — MPC Ceremony)

Creating a new vBTC V2 token requires a multi-party computation ceremony to generate the threshold signing keys. This is a 3-step process.

#### Step 1: Initiate MPC Ceremony

**Endpoint**: `POST /api/btc/vbtc-v2/ceremony/initiate/`

**Request:**
```json
{
  "owner_address": "RKuUHeM13zJ3cTh69TB5EKT8gUFCrXQtR5"
}
```

**Response:**
```json
{
  "success": true,
  "ceremony_id": "abc123...",
  "message": ""
}
```

#### Step 2: Poll Ceremony Status

Poll until `status` indicates completion.

**Endpoint**: `GET /api/btc/vbtc-v2/ceremony/{ceremony_id}/`

**Response:**
```json
{
  "success": true,
  "status": "InProgress",
  "progress": 65,
  "message": "Collecting key shares..."
}
```

**Polling recommendations:**
- Poll every 3-5 seconds
- Show a progress bar using the `progress` field (0-100)
- Typical ceremony takes 30-90 seconds

#### Step 3: Create Contract

Once the ceremony completes, create the contract.

**Endpoint**: `POST /api/btc/vbtc-v2/create/`

**Request:**
```json
{
  "owner_address": "RKuUHeM13zJ3cTh69TB5EKT8gUFCrXQtR5",
  "name": "My vBTC Token",
  "description": "A vBTC V2 token",
  "ticker": "vBTC",
  "ceremony_id": "abc123..."
}
```

**Response:**
```json
{
  "success": true,
  "transaction_hash": "3f225e54a1c25fdd...",
  "sc_identifier": "ba6351b3497d4326baf01a54df3b4d91:1779317679"
}
```

After creation, the token will appear in the list endpoints once the mint transaction is indexed by the explorer (typically within a few seconds).

---

### 6. Transfer SC Ownership

Smart contract ownership transfer uses the standard NFT transfer mechanism (Type 3 / `SC_TX`). No special vBTC V2 endpoint needed — build a raw TX with `Function: "Transfer()"` and `ContractUID`, same as any NFT transfer.

---

## Complete Withdrawal Flow (End to End)

```
1. User submits withdrawal request
   └─ Build Type 27 raw TX → POST /api/raw/send/
   └─ Returns: tx_hash (this is the withdrawal_request_hash)

2. Wait for tx confirmation (~1 block, ~20 seconds)
   └─ Poll GET /api/btc/vbtc-v2/withdrawals/{sc_identifier}/
   └─ Or check the tx appears in the explorer

3. Trigger FROST signing
   └─ POST /api/btc/vbtc-v2/withdraw/complete/
   └─ Body: { sc_identifier, withdrawal_request_hash }
   └─ Long timeout (120s+) — show waiting UI
   └─ Returns: vfx_transaction_hash + btc_transaction_hash

4. Done — BTC is sent to the destination address
   └─ btc_transaction_hash can be viewed on mempool.space
```

## Complete Create Flow (End to End)

```
1. Initiate MPC ceremony
   └─ POST /api/btc/vbtc-v2/ceremony/initiate/
   └─ Returns: ceremony_id

2. Poll until complete
   └─ GET /api/btc/vbtc-v2/ceremony/{ceremony_id}/
   └─ Poll every 3-5s, show progress bar

3. Create the contract
   └─ POST /api/btc/vbtc-v2/create/
   └─ Returns: transaction_hash, sc_identifier

4. Token appears in explorer once indexed
   └─ GET /api/btc/vbtc-v2/detail/{sc_identifier}/
   └─ deposit_address is the BTC address to fund the token
```

---

## Error Handling

All proxy endpoints return a consistent shape:

**Success:** `{ "success": true, ... }`
**Failure:** `{ "success": false, "message": "Human-readable error" }`

HTTP status codes:
- `200` — success
- `400` — missing required fields
- `500` — CLI error (message field has details)

---

## Differences from V1

| Aspect | V1 | V2 |
|--------|----|----|
| Transfer function | `TransferCoin()` | `TransferVBTCV2()` |
| Transfer TX type | 18 (`TKNZ_TX`) | 26 (`VBTC_V2_TRANSFER`) |
| Withdrawal request TX type | 20 (`TKNZ_WITHDRAWAL_REQUEST`) | 27 (`VBTC_V2_WITHDRAWAL_REQUEST`) |
| Withdrawal completion | Arbiter-based | FROST threshold signing (validator set) |
| Key management | Single arbiter holds BTC key | Distributed (MPC + FROST) |
| Contract creation | Standard SC mint | MPC ceremony required |
| `total_amount` on transfer TX | Contains the amount | Always `0` (amount in data payload) |
| Balance source | BlockCypher (deprecated) | blockchain.info / Blockbook |
| List endpoint | `GET /api/btc/vbtc/{address}/` | `GET /api/btc/vbtc-v2/{address}/` |
| Detail endpoint | `GET /api/btc/vbtc/detail/{id}/` | `GET /api/btc/vbtc-v2/detail/{id}/` |
