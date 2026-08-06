# vBTC V2 GUI Changes — CLI Alignment

## 1. Ceremony execute (already fixed)

The ceremony execute fix from the previous round is already applied. No action needed.

## 2. Withdrawal Complete — New Two-Step Pattern with signOnly

Aaron implemented withdrawal complete differently than the other operations. The CLI uses `signOnly: true` which means it does the FROST signing but returns the **signed BTC transaction hex** instead of broadcasting. The web wallet is responsible for broadcasting the BTC TX.

### What changed

**Old flow (single step):**
```
POST withdraw/complete/ → { vfx_transaction_hash, btc_transaction_hash }
```

**New flow (two-step, same pattern as ceremony):**
```
POST withdraw/complete/prepare/
  Request:  { sc_identifier, withdrawal_request_hash, owner_address }
  Response: { SessionId, StartMessage, StartTimestamp, ShareDistributionMessage, ShareDistributionTimestamp, Amount, BTCDestination, FeeRate }

POST withdraw/complete/execute/
  Request:  { sc_identifier, withdrawal_request_hash, owner_address, session_id,
              start_signature, start_timestamp, share_distribution_signature, share_distribution_timestamp,
              amount, btc_destination, fee_rate }
  Response: { SignedBTCTxHex }
```

### Key difference: SignedBTCTxHex

The execute response returns `SignedBTCTxHex` — a raw signed Bitcoin transaction hex string. The web wallet needs to:
1. Broadcast this hex to the Bitcoin network (via a BTC node API or mempool.space)
2. The VFX completion TX (Type 28) will be created by validators once they see the BTC TX confirmed

This means `completeV2Withdrawal` in `web_token_actions_manager.dart` needs to be updated to:
- Call prepare → sign two messages → execute (same pattern as ceremony)
- Handle `SignedBTCTxHex` in the response instead of `BTCTransactionHash`/`VFXTransactionHash`
- Broadcast the BTC TX (new step)

### explorer_service.dart changes

Replace `prepareV2WithdrawalComplete` and `executeV2WithdrawalComplete` with:

```dart
Future<Map<String, dynamic>> prepareV2WithdrawalComplete({
  required String scIdentifier,
  required String withdrawalRequestHash,
  required String ownerAddress,
}) async {
  final response = await postJson(
    '/btc/vbtc-v2/withdraw/complete/prepare/',
    params: {
      'sc_identifier': scIdentifier,
      'withdrawal_request_hash': withdrawalRequestHash,
      'owner_address': ownerAddress,
    },
  );
  return response['data'];
}

Future<Map<String, dynamic>> executeV2WithdrawalComplete({
  required String scIdentifier,
  required String withdrawalRequestHash,
  required String ownerAddress,
  required String sessionId,
  required String startSignature,
  required int startTimestamp,
  required String shareDistributionSignature,
  required int shareDistributionTimestamp,
  double amount = 0,
  String btcDestination = '',
  int feeRate = 0,
}) async {
  final response = await postJson(
    '/btc/vbtc-v2/withdraw/complete/execute/',
    params: {
      'sc_identifier': scIdentifier,
      'withdrawal_request_hash': withdrawalRequestHash,
      'owner_address': ownerAddress,
      'session_id': sessionId,
      'start_signature': startSignature,
      'start_timestamp': startTimestamp,
      'share_distribution_signature': shareDistributionSignature,
      'share_distribution_timestamp': shareDistributionTimestamp,
      'amount': amount,
      'btc_destination': btcDestination,
      'fee_rate': feeRate,
    },
    timeout: 180000,
  );
  return response['data'];
}
```

### web_token_actions_manager.dart changes

`completeV2Withdrawal` needs the same prepare→sign→execute pattern as the ceremony:

```dart
// 1. Prepare — get messages to sign
final prepared = await ExplorerService().prepareV2WithdrawalComplete(
  scIdentifier: scIdentifier,
  withdrawalRequestHash: requestHash,
  ownerAddress: keypair.address,
);

// 2. Sign both messages (same as ceremony)
final startSig = await RawTransaction.getSignature(
  message: prepared['StartMessage'],
  privateKey: keypair.private,
  publicKey: keypair.public,
);
final shareSig = await RawTransaction.getSignature(
  message: prepared['ShareDistributionMessage'],
  privateKey: keypair.private,
  publicKey: keypair.public,
);

// 3. Execute — triggers FROST, returns signed BTC TX hex
final result = await ExplorerService().executeV2WithdrawalComplete(
  scIdentifier: scIdentifier,
  withdrawalRequestHash: requestHash,
  ownerAddress: keypair.address,
  sessionId: prepared['SessionId'],
  startSignature: startSig,
  startTimestamp: prepared['StartTimestamp'],
  shareDistributionSignature: shareSig,
  shareDistributionTimestamp: prepared['ShareDistributionTimestamp'],
  amount: prepared['Amount']?.toDouble() ?? 0,
  btcDestination: prepared['BTCDestination'] ?? '',
  feeRate: prepared['FeeRate'] ?? 0,
);

// 4. Result contains SignedBTCTxHex — broadcast to Bitcoin network
final signedBtcTxHex = result['SignedBTCTxHex'];
```

### Open question: BTC broadcast

The web wallet needs to broadcast `SignedBTCTxHex` to the Bitcoin network. Options:
- Direct to a BTC node API (blockstream.info, mempool.space)
- Through a Spyglass proxy endpoint
- Through the CLI (if there's a broadcast endpoint)

This needs to be decided.
