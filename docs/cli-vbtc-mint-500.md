# CLI returns HTTP 500 for four vBTC V2 smart contracts

Investigated 2026-07-31. For Aaron.

## Summary

`GET /scapi/SCV1/GetSmartContractData/{ContractUID}` returns **HTTP 500 with a zero-length body** for four vBTC V2 contracts, while serving the other 21 normally. The node is healthy and fully synced, and **it still holds the contract data** — `GetSmartContractsState/{scUID}` returns it fine for all 25.

The failure is in materialising the stored contract text into an object. `GetSingleSmartContract/{id}` leaks the trace:

```
System.NullReferenceException: Object reference not set to an instance of an object.
   at ReserveBlockCore.Models.SmartContracts.SmartContractMain.GenerateSmartContractInMemory(String scText)
      in /home/ubuntu/vfx-cli/Res…
```

So nothing is lost on the node — `GenerateSmartContractInMemory` just cannot parse these four contract texts. There are **two distinct triggers**, one per pair, and they need two separate fixes:

| Pair | Trigger | Status |
|---|---|---|
| 2026-07-31 | Trillium rejects a digit in a method name (`GetIsS3C()`) | Root cause found by Aaron, confirmed against all 25 contracts |
| 2026-05-27 | `let SCVersion = ` emitted with no value | Still open — **not** covered by the method-name fix |

## Trigger 2 — the 2026-07-31 pair: digit in a method name — CONFIRMED

Aaron identified this: Trillium does not accept a digit in a method name, and the newer contract template emits `GetIsS3C()`.

Checked against all 25 mainnet V2 contracts, and it holds exactly:

- `GetIsS3C` is the **only** method name containing a digit anywhere in the set.
- It appears in exactly the two contracts that fail from 2026-07-31.
- The other 23 contracts have no digit-bearing method name, and 21 of them serve fine.

Both July contracts have 15 functions where all working ones have 13; the two extra are `GetIsS3C()` and `GetLinkedContractUID()`. Only the first carries a digit.

This is the trigger that matters going forward: it is a template-version issue, not a one-off, so **every future mint from the newer client hits it** until the fix ships. The two on 2026-07-31 were 8 minutes apart and both failed.

## Trigger 1 — the 2026-05-27 pair: a SEPARATE bug, not fixed by the above

The digit theory does **not** cover the May pair, and this is worth flagging before the Trillium fix is called done:

- Both May contracts have 13 functions, same as every working contract.
- Neither has any method name containing a digit.
- Both still return HTTP 500 today.

Their distinguishing feature is a `let` declaration emitted with no value at all:

```
let SCVersion = 
let FileSize = "0"
```

They carry 15 `let` declarations where every working contract has 16 — `SCVersion` is blank, where all 21 working contracts have `let SCVersion = 1`. That empty value is the remaining candidate for the null deref on those two.

**So there are two distinct fixes needed.** Shipping only the method-name fix will leave `672076ec…` and `847c505e…` still unreadable.

## The four affected contracts

| ContractUID | Name | Mint tx | Height | Minter | Minted (UTC) | Trigger |
|---|---|---|---|---|---|---|
| `672076ec1b164936819663e867f8a1f4:1779846489` | vbtcv2 web | `3060143c0495882bf9a12f5e7ef9f1de1bbb5fda1f7fab69f0b21add06099fe0` | 6544281 | RNiQrW3aBUWZhfadqKxPuN46iGaR13ox7P | 2026-05-27 01:48:18 | empty SCVersion |
| `847c505ea6264e81913cdcbe2566e94b:1779900857` | vbtc v2 web | `67d6c68eed6a7ac75b0717b0111dd92308b6eeff9d827ff7f8c92edc2789e811` | 6548606 | RNiQrW3aBUWZhfadqKxPuN46iGaR13ox7P | 2026-05-27 16:54:19 | empty SCVersion |
| `6cdfedf203c044028edc83eabe28b03d:1785522298` | Bfly vBTC @samcrocker | `c3fb367c626b7c57315aa65407c2a472a592920a2738dff0f6ef8225621fa8f0` | 6991104 | RU3XgUWc8M9vCVFzAw1ae9hjzYhgFmbGnj | 2026-07-31 18:25:04 | extra getters |
| `46a6b1698a8f443bb02fd3afad65ec96:1785522809` | SC1 | `e2860dacded7847dc4d7e508e81d3c018ed850ad7a9d20e151ace938012c2618` | 6991145 | RW8YA62c6z3kjDojN7VVH2RchAFm5feBHb | 2026-07-31 18:33:37 | extra getters |

Deposit addresses (these hold or will hold real BTC):

- `672076ec…` → `bc1prjyl3snkmvpngqamp84xu96v3kgcwplvz4d9anykn70dre8hypqsqgflag`
- `847c505e…` → `bc1pd5axrfupv3au97gd849jf8a322g55n3gtkmruup8zzndgpnrsg5qqakm39`
- `6cdfedf2…` → `bc1p888eaz0vdqwj3yveyc6n2h0s7um2aud9jhve7dqrhde7t675cmgsc3ky54`
- `46a6b169…` → `bc1pge2jf6zmfhx4pc5h0et0dvmwmhntqmfutvehz09hwsy66srmtz0svfefsh`

## Endpoint behaviour, same node, same session

| Endpoint | Failing contract | Working contract |
|---|---|---|
| `scapi/SCV1/GetSmartContractData/{id}` | **500**, 0 bytes | 200, ~5KB |
| `scapi/SCV1/GetSingleSmartContract/{id}` | 200 carrying the .NET stack trace | 200 `null` |
| `scapi/SCV1/GetSmartContractsState/{scUID}` | **200**, full `ContractData` | 200, full `ContractData` |
| `scapi/SCV1/GetSmartContractsByAddress/{addr}` | 200, UID present in list | 200, UID present in list |

Note `GetSingleSmartContract` returns HTTP **200** with a raw exception string as the body — worth fixing separately, since a caller checking only the status code will treat a crash as success.

## Was there media attached?

No, and not on the working ones either. Every V2 mint on mainnet — all 25 — carries `FileName = "vbtc_v2_token"`, `FileSize = "0"`, and an empty `GetImageBase()`. Media is not involved in this at all.

## "Are we supposed to use the NFT data endpoints for these? I thought we made new ones."

Pulled the full route list from the node's own swagger (`/swagger/v1/swagger.json`, which is exposed). There is **no V2-specific endpoint that reads contract data by SC UID**. The newer namespaces are transaction-building and wallet operations:

- `vbtcapi/VBTC/*` — MPC ceremony, transfer, withdrawal request/complete/cancel, ownership transfer, validator list/status, shield/unshield. All raw-tx builders and ceremony ops.
- `btcapi/BTCV2/*` — tokenize, transfer, withdraw, balances, address/UTXO management. Closest reads are `GetTokenizedBTCList` and `GetvBTCBalance/{address}/{scUID}`, neither of which returns contract detail.

Everything that returns a contract by UID is still under `scapi/SCV1`. So yes, the explorer is using the right endpoint — there isn't a V2 replacement for it. If one is supposed to exist, it is not on this build.

The practical alternative today is **`GetSmartContractsState/{scUID}`**, which works for all 25 and returns the same `ContractData` blob.

## What we ruled out

- **Minter address.** `RNiQrW3a…` owns two failures *and* three successes; `RU3XgUWc…` owns one failure and nine successes.
- **Media / assets.** Identical across all 25, see above.
- **Compression.** The gzip OS byte varies (0x03, 0x0a, 0x13) without tracking failure; one failing contract is stored uncompressed.
- **Beacon locators.** `GetLastKnownLocators` returns `{"Result":"Success","Locators":null}` for both failing and working contracts.
- **URL form.** Fails with and without the trailing slash, and on both configured base URLs (same host).
- **Transience.** The May pair still fails two months and many restarts later.

## What the explorer is doing meanwhile

Not waiting. A V2 mint carries its entire contract in the mint transaction, so the explorer now rebuilds the contract from chain data when the CLI will not serve it (`rbx/chain_contract.py`). Verified against all four: names, owners, deposit addresses, FROST group keys, thresholds, proof heights, 432-char DKG proofs and 92–95 validator addresses all recovered. Cross-checked against a contract the CLI *does* serve — chain-derived and CLI-derived values match exactly.

That makes indexing independent of this bug, but it does not make the bug harmless: anything else reading `GetSmartContractData` still breaks, and trigger 2 suggests every future mint from the newer client will hit it.

## Reproducing

```bash
N=http://44.254.72.141:7292
BAD=6cdfedf203c044028edc83eabe28b03d:1785522298
OK=d77f9a312364497788a4ea1c359b4367:1782336901

curl -s -o /dev/null -w '%{http_code}\n' "$N/scapi/SCV1/GetSmartContractData/$BAD"   # 500
curl -s -o /dev/null -w '%{http_code}\n' "$N/scapi/SCV1/GetSmartContractData/$OK"    # 200
curl -s "$N/scapi/SCV1/GetSingleSmartContract/$BAD" | head -c 300                    # stack trace
curl -s "$N/scapi/SCV1/GetSmartContractsState/$BAD" | head -c 300                    # data is there
```

To read a contract yourself: `ContractData` is base64 → gzip → **UTF-16**.
