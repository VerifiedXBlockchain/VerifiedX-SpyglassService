# vBTC V2 API Changelog — Two-Step Endpoints

> Breaking change to all vBTC V2 write endpoints. Read endpoints unchanged.

## What Changed

All write operations now follow a **two-step pattern**: prepare (CLI builds unsigned TX, returns hash) → sign in browser → send (CLI accepts signature, broadcasts).

The old single-step endpoints are removed.

## Removed Endpoints

```
POST /api/btc/vbtc-v2/ceremony/initiate/
POST /api/btc/vbtc-v2/create/
POST /api/btc/vbtc-v2/withdraw/complete/
POST /api/btc/vbtc-v2/withdraw/cancel/
```

## New Endpoints

### MPC Ceremony (Contract Creation — Step 1)

**Prepare:** `POST /api/btc/vbtc-v2/ceremony/prepare/`
```json
// Request
{ "owner_address": "R..." }

// Response
{
  "success": true,
  "ceremony_id": "guid-1",
  "session_id": "guid-2",
  "messages_to_sign": {
    "start_message": "guid-2.Rxxxx....1779825120",
    "start_timestamp": 1779825120,
    "share_distribution_message": "guid-2.Rxxxx....1779825121",
    "share_distribution_timestamp": 1779825121
  },
  "validator_count": 93,
  "threshold": 51
}
```

**Execute:** `POST /api/btc/vbtc-v2/ceremony/execute/`
```json
// Request — sign both messages with secp256k1
{
  "ceremony_id": "guid-1",
  "start_signature": "<sign start_message>",
  "share_distribution_signature": "<sign share_distribution_message>"
}

// Response
{ "success": true, ... }
```

**Poll status:** `GET /api/btc/vbtc-v2/ceremony/{ceremony_id}/` — **unchanged**

### Contract Creation (Step 2 — after ceremony completes)

**Prepare:** `POST /api/btc/vbtc-v2/create/prepare/`
```json
// Request
{
  "owner_address": "R...",
  "name": "My vBTC Token",
  "description": "...",
  "ticker": "vBTC",
  "ceremony_id": "guid-1"
}

// Response — Hash is what the browser signs
{ "success": true, "Hash": "abc123...", ... }
```

**Send:** `POST /api/btc/vbtc-v2/create/send/`
```json
// Request
{
  "hash": "abc123...",
  "signature": "<sign the Hash>",
  "public_key": "<signer's compressed public key>"
}

// Response
{ "success": true, "TransactionHash": "...", "SmartContractUID": "..." }
```

### Transfer

**Prepare:** `POST /api/btc/vbtc-v2/transfer/prepare/`
```json
// Request
{
  "sc_identifier": "guid:timestamp",
  "from_address": "R...",
  "to_address": "R...",
  "amount": 0.0001
}

// Response
{ "success": true, "Hash": "...", ... }
```

**Send:** `POST /api/btc/vbtc-v2/transfer/send/`
```json
{ "hash": "...", "signature": "...", "public_key": "..." }
```

### Withdrawal Request

**Prepare:** `POST /api/btc/vbtc-v2/withdraw/request/prepare/`
```json
// Request
{
  "sc_identifier": "guid:timestamp",
  "requestor_address": "R...",
  "btc_address": "bc1q...",
  "amount": 0.001,
  "fee_rate": 10
}

// Response
{ "success": true, "Hash": "...", ... }
```

**Send:** `POST /api/btc/vbtc-v2/withdraw/request/send/`
```json
{ "hash": "...", "signature": "...", "public_key": "..." }
```

### Withdrawal Complete (FROST)

**Prepare:** `POST /api/btc/vbtc-v2/withdraw/complete/prepare/`
```json
// Request
{
  "sc_identifier": "guid:timestamp",
  "withdrawal_request_hash": "..."
}

// Response — message_to_sign is what the browser signs for FROST leader auth
{ "success": true, "SessionId": "...", "message_to_sign": "...", "Timestamp": ... }
```

**Execute:** `POST /api/btc/vbtc-v2/withdraw/complete/execute/`
```json
// Request
{
  "sc_identifier": "guid:timestamp",
  "withdrawal_request_hash": "...",
  "signature": "<sign message_to_sign>",
  "timestamp": 1779825120,
  "unique_id": "<client-generated UUID>",
  "owner_address": "R..."
}

// Response (long timeout — FROST signing takes up to 120s)
{
  "success": true,
  "VFXTransactionHash": "...",
  "BTCTransactionHash": "..."
}
```

### Withdrawal Cancel

**Prepare:** `POST /api/btc/vbtc-v2/withdraw/cancel/prepare/`
```json
{
  "sc_identifier": "guid:timestamp",
  "owner_address": "R...",
  "withdrawal_request_hash": "..."
}

// Response
{ "success": true, "Hash": "...", ... }
```

**Send:** `POST /api/btc/vbtc-v2/withdraw/cancel/send/`
```json
{ "hash": "...", "signature": "...", "public_key": "..." }
```

## Signing

All signing uses **secp256k1 ECDSA** — same as regular VFX transaction signing.

Two types of things to sign:
1. **TX hashes** (from prepare endpoints) — sign the `Hash` field, same as any raw TX
2. **Ceremony messages** (MPC/FROST) — sign the plain UTF-8 string in `"{sessionId}.{ownerAddress}.{timestamp}"` format

Signature format: `sig.pubkey` (base64) or DER — CLI accepts both.

## Unchanged Endpoints

```
GET  /api/btc/vbtc-v2/                              — list all tokens
GET  /api/btc/vbtc-v2/{vfx_address}/                — list tokens for address
GET  /api/btc/vbtc-v2/detail/{sc_identifier}/        — token detail (live balance)
GET  /api/btc/vbtc-v2/transfers/{sc_identifier}/     — transfer history
GET  /api/btc/vbtc-v2/withdrawals/{sc_identifier}/   — withdrawal history
GET  /api/btc/vbtc-v2/ceremony/{ceremony_id}/        — poll ceremony status
```
