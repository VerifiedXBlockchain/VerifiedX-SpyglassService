# New Transaction Types Reference

Context document for the explorer frontend and wallet (web + GUI) teams. All data sourced from mainnet and testnet databases as of 2026-05-25.

---

## Complete Type Map

| Type ID | Enum Name | Label | Status |
|---------|-----------|-------|--------|
| 0 | `TX` | Tx | Existing |
| 1 | `NODE` | Node | Existing |
| 2 | `NFT_MINT` | NFT Mint | Existing |
| 3 | `NFT_TX` | NFT Tx | Existing |
| 4 | `NFT_BURN` | NFT Burn | Existing |
| 5 | `NFT_SALE` | NFT Sale | Existing |
| 6 | `ADDRESS` | Address | Existing |
| 7 | `DST_REGISTRATION` | DST Registration | Existing |
| 8 | `VOTE_TOPIC` | Vote Topic | Existing |
| 9 | `VOTE` | Vote | Existing |
| 10 | `RESERVE` | Reserve | Existing |
| 11 | `SC_MINT` | Smart Contract Mint | Existing |
| 12 | `SC_TX` | Smart Contract Tx | Existing |
| 13 | `SC_BURN` | Smart Contract Burn | Existing |
| 14 | `FTKN_MINT` | Fungible Token Mint | Existing |
| 15 | `FTKN_TX` | Fungible Token Tx | Existing |
| 16 | `FTKN_BURN` | Fungible Token Burn | Existing |
| 17 | `TKNZ_MINT` | Tokenization Mint | Existing |
| 18 | `TKNZ_TX` | Tokenization Tx | Existing |
| 19 | `TKNZ_BURN` | Tokenization Burn | Existing |
| **20** | `TKNZ_WITHDRAWAL_REQUEST` | Tokenization Withdrawal Request | **New** |
| **21** | `TKNZ_WITHDRAWAL_COMPLETE` | Tokenization Withdrawal Complete | **New** |
| **22** | `VALIDATOR_REGISTRATION` | Validator Registration | **New** |
| **23** | `VALIDATOR_HEARTBEAT` | Validator Heartbeat | **New** |
| 24 | — | (unused) | — |
| 25 | `VBTC_V2_MINT` | vBTC V2 Mint | Existing |
| 26 | `VBTC_V2_TRANSFER` | vBTC V2 Transfer | Existing |
| 27 | `VBTC_V2_WITHDRAWAL_REQUEST` | vBTC V2 Withdrawal Request | Existing |
| 28 | `VBTC_V2_WITHDRAWAL_COMPLETE` | vBTC V2 Withdrawal Complete | Existing |
| 29-30 | — | (unused) | — |
| **31** | `VFX_SHIELD` | VFX Shield | **New** |
| **32** | `VFX_UNSHIELD` | VFX Unshield | **New** |
| **33** | `VFX_PRIVATE_TRANSFER` | VFX Private Transfer | **New** |
| **34** | `VBTC_SHIELD` | vBTC Shield | **New** |
| **35** | `VBTC_UNSHIELD` | vBTC Unshield | **New** |
| **36** | `VBTC_PRIVATE_TRANSFER` | vBTC Private Transfer | **New** (reserved, no on-chain data yet) |
| **37** | `VBTC_BRIDGE_LOCK` | vBTC Bridge Lock | **New** |

---

## Detailed Reference: New Types

### Tokenization V1 Withdrawal (Types 20-21)

These complete the vBTC v1 (tokenization) lifecycle. They were present on-chain but not labeled in the explorer.

#### Type 20 — Tokenization Withdrawal Request

A vBTC v1 holder requests withdrawal of their tokenized BTC back to the Bitcoin chain.

- **to_address**: Requestor's VFX address
- **from_address**: Arbiter address
- **total_amount**: `0`
- **total_fee**: `0`

**Data payload:**
```json
{
  "Function": "TokenizedWithdrawalRequest()",
  "ContractUID": "10bd52d2bdbd4a90a70d8641d4fbb07a:1762788962",
  "TokenizedWithdrawal": {
    "RequestorAddress": "RRyPZMPmDjZtRn1iZR2uDWuwBZDXAimX1e",
    "OriginalRequestTime": 1768798318,
    "OriginalSignature": "MEQCICbn/kJRfHqdAN5j...",
    "OriginalUniqueId": "OklSWOdrOcypIFAO",
    "Timestamp": 1768798319,
    "SmartContractUID": "10bd52d2bdbd4a90a70d8641d4fbb07a:1762788962",
    "Amount": 0.0001,
    "WithdrawalRequestType": 0,
    "TransactionHash": "0",
    "ArbiterUniqueId": "aTfsQOtPBqTGNZYo",
    "IsCompleted": false
  }
}
```

#### Type 21 — Tokenization Withdrawal Complete

Confirms a v1 tokenized withdrawal was executed on-chain (BTC side).

- **to_address**: `"TW_Base"` (special sentinel)
- **from_address**: Requestor's VFX address
- **total_amount**: `0`
- **total_fee**: `~0.00001`

**Data payload:**
```json
{
  "Function": "TokenizedWithdrawalComplete()",
  "ContractUID": "aa314a95ce474cffa8448b0af1efabbd:1736752601",
  "UniqueId": "QiSqdTuVUasVgjKd",
  "TransactionHash": "a8c2a9e55d0b85f435afd994783c6d387cf678115f094f69a416435f686304c4"
}
```

---

### Validator Registration & Heartbeat (Types 22-23)

#### Type 22 — Validator Registration

Sent once when a validator first registers on the network.

- **to_address / from_address**: Validator's VFX address (same)
- **total_amount**: `0`
- **total_fee**: `0`
- **Mainnet count**: 56 | **Testnet count**: 32

**Data payload:**
```json
{
  "ValidatorAddress": "RNLsAwj8pn6EXbHv1LYokuFGHXTVnxcwfr",
  "IPAddress": "93.127.132.150",
  "FrostPublicKey": "04e956c4345ae487a077fb9f49955cb027...",
  "BaseAddress": "0x4b2191F227E854D065Cd179DC2055FFf4461ad2d",
  "RegistrationBlockHeight": 6441514,
  "Signature": "MEUCIQC+YEJbC2HkLZfJFQ..."
}
```

**Key differences from heartbeat**: Has `RegistrationBlockHeight` (not `ReactivationBlockHeight`), no `PreviousIPAddress`.

#### Type 23 — Validator Heartbeat

High-volume periodic "I'm still alive" signal from validators. Also serves as a reactivation mechanism.

- **to_address / from_address**: Validator's VFX address (same)
- **total_amount**: `0`
- **total_fee**: `0`
- **Mainnet count**: 16,690 | **Testnet count**: 974

**Data payload:**
```json
{
  "ValidatorAddress": "RBsK9qCJh2CJUm29WfxExLgeqYkNJ9C6bc",
  "IPAddress": "91.98.147.204",
  "FrostPublicKey": "0407ec966bef091c230241dd2d51b16c498f8032...",
  "BaseAddress": "0xeDAdEb41E2398a34104463D95C7240b9813BFC10",
  "ReactivationBlockHeight": 6534734,
  "PreviousIPAddress": "91.98.147.204",
  "Signature": "MEUCIGr2hnfT4Dfx/fcAE5m/L4u..."
}
```

**Display considerations**: These are very high-volume. Frontends should consider filtering them from default transaction lists or providing a toggle. They are useful in validator detail views.

---

### vBTC V2 (Types 25-28)

All vBTC V2 transactions reference a smart contract via `ContractUID` in the format `{guid}:{timestamp}`.

#### Type 25 — vBTC V2 Mint

Mints a new vBTC V2 token (deploys the smart contract).

- **to_address / from_address**: Minter's VFX address (same)
- **total_amount**: `0`
- **total_fee**: `~0.00013`
- **Mainnet count**: 6

**Data payload:**
```json
[{
  "Function": "Mint()",
  "ContractUID": "871de5dff3f3461c9b5bf785f7933aa2:1779378609",
  "Data": "<gzip+base64 encoded smart contract state>",
  "MD5List": "NA"
}]
```

Note: The data payload is wrapped in an array (unlike other types). The `Data` field is a compressed blob containing the full SC definition (deposit address, FROST group keys, validator snapshot, thresholds, etc.).

#### Type 26 — vBTC V2 Transfer

Transfers vBTC amount from one VFX address to another within the same SC.

- **from_address**: Sender VFX address
- **to_address**: Recipient VFX address
- **total_amount**: `0` (actual BTC amount is in the data payload `Amount` field)
- **total_fee**: `~0.00001`
- **Mainnet count**: 1

**Data payload:**
```json
{
  "Function": "TransferVBTCV2()",
  "ContractUID": "4bb6f0991eda4c63b89d129514b149e2:1779157633",
  "FromAddress": "RKuUHeM13zJ3cTh69TB5EKT8gUFCrXQtR5",
  "ToAddress": "RNiQrW3aBUWZhfadqKxPuN46iGaR13ox7P",
  "Amount": 0.0001
}
```

**Important for wallets**: The `Amount` field in the data payload is the actual vBTC (BTC-denominated) transfer amount. The top-level `total_amount` is always `0`.

#### Type 27 — vBTC V2 Withdrawal Request

Requests withdrawal of vBTC back to a real BTC address.

- **to_address / from_address**: Requestor's VFX address (same)
- **total_amount**: `0`
- **total_fee**: `~0.000011`
- **Mainnet count**: 2

**Data payload:**
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

**Key fields for display**: `BTCAddress` (destination on Bitcoin), `Amount` (BTC amount), `FeeRate` (sat/vbyte fee rate for the BTC tx).

#### Type 28 — vBTC V2 Withdrawal Complete

Validator-issued confirmation that the BTC withdrawal was executed. Links back to the original request.

- **to_address / from_address**: Requestor's VFX address (same)
- **total_amount**: `0`
- **total_fee**: `0` (no fee — this is a validator-generated tx)
- **Mainnet count**: 2

**Data payload:**
```json
{
  "Function": "VBTCWithdrawalComplete()",
  "ContractUID": "76c995d525ab49a8956241a0e85db35a:1779140646",
  "WithdrawalRequestHash": "758ac79e33f97aeff0f53366811c483c79aac30d7de6fa9c2a12b4fe95e2726e",
  "BTCTransactionHash": "21463f551e41b88b63755234728b8a5df7d23ce9bd69dfe355de6cedf6cc2fb5",
  "Amount": 0.001,
  "Destination": "bc1qjzgqf9377zd0ts0x3z3sgq4e588txrwaqeca2w"
}
```

**Key fields for display**: `BTCTransactionHash` (can link to a BTC block explorer), `WithdrawalRequestHash` (links to the type 27 tx), `Destination` (BTC address).

#### SC Ownership Transfer

There is no dedicated vBTC V2 type for transferring smart contract ownership. This uses the existing `SC_TX` (type 12) mechanism, same as any NFT/SC transfer.

---

### Shielded / Privacy Transactions (Types 31-37)

These implement the privacy layer for VFX and vBTC. Funds move between transparent addresses and a `"Shielded_Pool"` address.

All shielded tx data payloads share a common structure:

```json
{
  "v": 1,
  "kind": "shield|unshield|private_transfer",
  "sub_type": "Shield|Unshield|PrivateTransfer",
  "asset": "VFX" or "VBTC:{contract_uid}",
  "outs": [{ "i": 0, "c": "<commitment>", "nh": "<note_hash>", "note": "<encrypted_note>" }],
  "nulls": ["<nullifier>"],
  "spent_tree_positions": [<int>],
  "spent_commitments": ["<commitment>"],
  "merkle_root": "<root_hash>",
  "fee": 0.000003
}
```

#### Type 31 — VFX Shield

Moves VFX from a transparent address into the shielded pool.

- **to_address**: `"Shielded_Pool"`
- **from_address**: Sender's VFX address
- **total_amount**: The shielded amount (e.g. `1.5`, `2.0`)
- **total_fee**: `~0.00002`
- **Mainnet count**: 2

Extra data fields: `transparent_input` (source address), `transparent_amount`.

#### Type 32 — VFX Unshield

Moves VFX from the shielded pool back to a transparent address.

- **to_address**: Recipient's VFX address
- **from_address**: `"Shielded_Pool"`
- **total_amount**: The unshielded amount (e.g. `0.4`)
- **total_fee**: `0`
- **Mainnet count**: 1

Extra data fields: `transparent_output` (destination address), `transparent_amount`.

#### Type 33 — VFX Private Transfer

Transfers VFX entirely within the shielded pool. Both sender and recipient are hidden.

- **to_address**: `"Shielded_Pool"`
- **from_address**: `"Shielded_Pool"`
- **total_amount**: `0` (amount is hidden)
- **total_fee**: `0`
- **Mainnet count**: 4

No transparent input/output fields. The transfer amount is encrypted in the note data.

#### Type 34 — vBTC Shield

Moves vBTC from a transparent address into the shielded pool.

- **to_address**: `"Shielded_Pool"`
- **from_address**: Owner's VFX address
- **total_amount**: `0`
- **total_fee**: `~0.00002`
- **Mainnet count**: 1

Extra data fields: `vbtc_uid` (the vBTC contract UID), `vbtc_amt` (BTC-denominated amount), `transparent_input`, `transparent_amount`.

**Data `asset` format**: `"VBTC:{contract_uid}"` (e.g. `"VBTC:4bb6f0991eda4c63b89d129514b149e2:1779157633"`)

#### Type 35 — vBTC Unshield

Moves vBTC from the shielded pool back to a transparent address.

- **to_address**: Recipient's VFX address
- **from_address**: `"Shielded_Pool"`
- **total_amount**: `0`
- **total_fee**: `0`
- **Mainnet count**: 2

Extra data fields: `vbtc_uid`, `vbtc_amt`, `transparent_output`, `transparent_amount`, plus fee-related nullifier/commitment fields for the VFX fee payment within the shielded pool.

#### Type 36 — vBTC Private Transfer

Transfers vBTC entirely within the shielded pool. Reserved but **no on-chain transactions exist yet**.

Expected to follow the same pattern as type 33 but with `asset: "VBTC:{contract_uid}"`.

#### Type 37 — vBTC Bridge Lock

Locks vBTC on the VFX chain for bridging to an EVM chain.

- **to_address / from_address**: Lock initiator's VFX address (same)
- **total_amount**: `0`
- **total_fee**: `~0.000011`
- **Mainnet count**: 3

**Data payload:**
```json
{
  "Function": "VBTCBridgeLock()",
  "ContractUID": "8234b371e58c4edda2543ad40c69bf84:1779197803",
  "LockId": "5425a29161cc46618015f283071f5bd9",
  "Amount": 0.0001,
  "AmountSats": 10000,
  "EvmDestination": "0xa3979bC430c5fE371894F231A4bBD38D175bF930"
}
```

**Key fields for display**: `LockId` (unique lock identifier), `Amount` (BTC-denominated), `AmountSats` (satoshi amount), `EvmDestination` (EVM address receiving the bridged asset).

---

## Frontend Display Recommendations

### Transaction List Views

- **Validator Heartbeat (23)**: High volume (16k+ on mainnet). Consider hiding from default tx lists or showing behind a filter toggle. Useful on validator detail pages.
- **Validator Registration (22)**: Low volume but important. Show with a distinct badge/icon.
- **Shielded Pool txs (31-37)**: The `"Shielded_Pool"` address is a sentinel, not a real wallet. Don't link it as a normal address. Consider a privacy/shield icon.

### Amount Display

| Type | Where the amount lives |
|------|----------------------|
| 25 (vBTC Mint) | No displayable amount (compressed blob) |
| 26 (vBTC Transfer) | `data.Amount` (BTC-denominated) |
| 27 (vBTC Withdrawal Req) | `data.Amount` (BTC-denominated) |
| 28 (vBTC Withdrawal Complete) | `data.Amount` (BTC-denominated) |
| 31 (VFX Shield) | `total_amount` on the tx |
| 32 (VFX Unshield) | `total_amount` on the tx |
| 33 (VFX Private Transfer) | Hidden (encrypted) |
| 34 (vBTC Shield) | `data.vbtc_amt` (BTC-denominated) |
| 35 (vBTC Unshield) | `data.vbtc_amt` (BTC-denominated) |
| 37 (vBTC Bridge Lock) | `data.Amount` / `data.AmountSats` |

### Linking

- **Type 28 `BTCTransactionHash`** can link to a Bitcoin block explorer (e.g. `mempool.space/tx/{hash}`)
- **Type 28 `WithdrawalRequestHash`** links to the corresponding type 27 tx in the VFX explorer
- **Type 37 `EvmDestination`** can link to an EVM block explorer

---

## API

The explorer API already serves `type_label` as a string field on all transaction serializers. The new labels are:

```
20 -> "Tokenization Withdrawal Request"
21 -> "Tokenization Withdrawal Complete"
22 -> "Validator Registration"
23 -> "Validator Heartbeat"
31 -> "VFX Shield"
32 -> "VFX Unshield"
33 -> "VFX Private Transfer"
34 -> "vBTC Shield"
35 -> "vBTC Unshield"
36 -> "vBTC Private Transfer"
37 -> "vBTC Bridge Lock"
```

These are returned alongside the integer `type` field, so frontends can use either for display/filtering.
