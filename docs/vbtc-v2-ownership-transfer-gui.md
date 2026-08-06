# vBTC V2 Ownership Transfer — GUI Web Wallet Integration

## Overview

Transfer ownership of a vBTC V2 smart contract to another VFX address. This is a 3-step flow using existing infrastructure (beacon upload + raw TX pipeline) plus one new Spyglass endpoint.

## When It's Used

- Butterfly pre-activation: mint token with service account → transfer to user on login
- User-initiated: transfer their vBTC token to another address (advanced operation)

## Flow

### Step 1: Beacon Upload

Upload SC assets to a beacon so the recipient can receive the contract data.

```
POST /api/raw/beacon/upload/{scIdentifier}/{toAddress}/{signature}/
```

- `signature`: sign the `scIdentifier` string with the owner's private key
- Returns: `{ "success": true, "locator": "beacon_locator_string" }`

This endpoint already exists and is used for NFT transfers.

### Step 2: Get Transfer TX Data

Get the pre-built TX data payload for the ownership transfer.

```
GET /api/btc/vbtc-v2/ownership-transfer/{scIdentifier}/{toAddress}/{locator}/
```

- `locator`: the beacon locator from Step 1
- Returns: JSON array with the TX data payload:
```json
[{
  "Function": "Transfer()",
  "ContractUID": "6d893dce1c244ad5a98b3981a63dcd2a:1780080357",
  "ToAddress": "RPKxShZhfytjfffG8sQZG5ZTj9qqcAPgvJ",
  "Data": "<gzip+base64 encoded SC code>",
  "Locators": "beacon_locator_string",
  "MD5List": "defaultvBTC.png::150b90aa9d06f7e4fc5703ca6d7f01db"
}]
```

On error: `{ "Success": false, "Message": "..." }`

Possible errors:
- "Smart contract state not found" — SC not in State Trei
- "vBTC V2 contract not found" — not a V2 token
- "Contract missing TokenizationV2 feature" — wrong contract type
- "Cannot transfer a token with zero balance" — empty token

### Step 3: Build, Sign, Send Raw TX

Use the standard raw TX pipeline with the data from Step 2.

```typescript
// Build transaction
const txData = step2Response; // The JSON array from Step 2

// Get fee
const feeResult = await postJson('/raw/fee/', {
  transaction: {
    FromAddress: ownerAddress,
    ToAddress: toAddress,
    TransactionType: 18, // TKNZ_TX
    Amount: 0,
    Data: txData,
  }
});

// Get hash
const hashResult = await postJson('/raw/hash/', {
  transaction: {
    FromAddress: ownerAddress,
    ToAddress: toAddress,
    TransactionType: 18, // TKNZ_TX
    Amount: 0,
    Fee: feeResult.Fee,
    Nonce: nonce,
    Timestamp: timestamp,
    Data: txData,
  }
});

// Sign
const signature = getSignature(hashResult.Hash, privateKey);

// Send
const sendResult = await postJson('/raw/send/', {
  transaction: {
    Hash: hashResult.Hash,
    FromAddress: ownerAddress,
    ToAddress: toAddress,
    TransactionType: 18, // TKNZ_TX
    Amount: 0,
    Fee: feeResult.Fee,
    Nonce: nonce,
    Timestamp: timestamp,
    Data: txData,
    Signature: signature,
    Height: 0,
    UnlockTime: null,
  }
});
```

**TX Type**: `18` (TKNZ_TX) — this is the same type used for V1 tokenization transfers and is what the CLI's VBTCService.TransferOwnership uses.

## Explorer Service Methods to Add

```dart
// In explorer_service.dart

Future<Map<String, dynamic>> beaconUpload({
  required String scIdentifier,
  required String toAddress,
  required String signature,
}) async {
  final response = await getJson(
    '/raw/beacon/upload/$scIdentifier/$toAddress/$signature/',
  );
  return response;
}

Future<dynamic> getVbtcOwnershipTransferData({
  required String scIdentifier,
  required String toAddress,
  required String locator,
}) async {
  final response = await getJson(
    '/btc/vbtc-v2/ownership-transfer/$scIdentifier/$toAddress/$locator/',
  );
  return response;
}
```

## Token Actions Manager Method

```dart
Future<bool> transferVbtcOwnership({
  required String scIdentifier,
  required String toAddress,
}) async {
  final keypair = ref.read(webSessionProvider).keypair;
  if (keypair == null) return false;

  // Step 1: Beacon upload
  final beaconSig = await RawTransaction.getSignature(
    message: scIdentifier,
    privateKey: keypair.private,
    publicKey: keypair.public,
  );
  if (beaconSig == null) return false;

  final beacon = await ExplorerService().beaconUpload(
    scIdentifier: scIdentifier,
    toAddress: toAddress,
    signature: beaconSig,
  );
  final locator = beacon['locator'];
  if (locator == null) return false;

  // Step 2: Get transfer data
  final txData = await ExplorerService().getVbtcOwnershipTransferData(
    scIdentifier: scIdentifier,
    toAddress: toAddress,
    locator: locator,
  );

  // Step 3: Build, sign, send via standard raw TX
  return await _verifyConfirmAndSendTx(
    toAddress: toAddress,
    data: txData,
    txType: 18, // TKNZ_TX
  );
}
```

Note: `_verifyConfirmAndSendTx` is the existing helper that handles fee/hash/sign/verify/send.
