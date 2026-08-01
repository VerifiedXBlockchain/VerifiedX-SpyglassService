# VFX Explorer Deep Review — June 10–11, 2026

## Executive summary

Nine review agents produced 97 findings (96 after dedup) across vBTC v2 and the explorer's general machinery; medium-and-up correctness/security/operational findings went to adversarial verification against live mainnet/testnet data. Final tally: **5 critical** (each manually re-verified against mainnet on 2026-06-11), **29 confirmed by independent verifiers** (10 high, 15 medium, 4 low), 8 code-confirmed with the final live-data check interrupted, 22 marked unverified because the verification phase stalled (reviewer evidence included), 15 low-severity observations, 8 architecture and 7 docs notes, and 2 findings refuted and dismissed.

The themes that matter most:

1. **vBTC v2 balance accounting is wrong on mainnet right now.** `VbtcV2Token.addresses` pins the token's whole `global_balance` (plus completed withdrawals) on the *current* owner with no ledger compensation at ownership transfer, duplicate `VbtcV2TokenTransfer` rows double-count transfers, and the `> 0` filter hides the resulting negative residuals instead of alarming on them. Net effect on token 7 today: the API reports 0.00118859 vBTC of claims against 0.00078859 BTC of backing — ~51% unbacked.

2. **Chain data is silently lost.** Two of 13 mainnet V2 mints have no token row (mint dropped when the CLI lacked smart-contract state — all later transfers/withdrawals for those tokens are silently skipped); mainnet has 5 permanently missing block heights because `sync_block` returns silently on bad node data and the cursor never looks back; a mid-block exception drops the rest of the block's transactions; there is no reorg handling.

3. **The withdrawal state machine can't cancel or recover.** Transaction type 29 (VBTC_V2_WITHDRAWAL_CANCEL) has no handler; two mainnet withdrawals have been stuck `requested` for 12 days, locking tokens 7 and 8 in `is_pending_withdrawal`; completion clears that flag even while other withdrawals are still open, and the withdrawal add-back opens a balance-inflation window until the next BTC sync.

4. **Writes happen where they shouldn't.** The public, unauthenticated `VbtcV2DetailView` GET performs an external blockchain.info call and a stale full-row `token.save()` on every request — it can race with and revert an ownership transfer; `reprocess_vbtc_v2` (full run) reverts V2 ownership to the original minter; three different workers full-row-write the same token rows; DRF auth is globally `AllowAny`, leaving mutating proxy endpoints (`api/raw/*`, `masternodes/send`) open.

5. **Operations are flying blind.** No time limits on the block-sync pipeline (the concurrency-1 blocks worker can stall forever on untimed HTTP calls — there is no recovery path); the health check monitors the CLI node's height rather than the explorer's own sync, so a dead blocks-worker raises no alert; alerting shares its failure domain with the things it monitors; and the incremental `Address.balance` system has drifted (899 addresses) from the transaction ledger.

**Reading note:** several issues were independently rediscovered by multiple reviewers and are intentionally kept as separate entries (same file/line, different angle) — treat those as corroboration, not noise.

## Methodology

Nine parallel review agents covered the codebase — five focused on vBTC v2 (token model & balance math, TKNZ_TX processing & ownership transfer, API security, design-doc conformance, and live-data invariant probing) and four on general subsystems (block sync & indexing, Celery topology & health monitoring, the web API layer, and architecture). Correctness/security/operational findings of medium severity or higher were then handed to independent, adversarial verifier agents instructed to refute them using the actual code, read-only mainnet/testnet queries, and Sentry. Databases were accessed strictly read-only throughout.

**Caveats:** the verification phase was cut short by a tooling failure (verifier DB connections hung). 8 findings had their verification interrupted after the code-level confirmation but before final live-data checks, and a handful of verifiers (including those for the four critical findings) never launched — the criticals were instead manually re-verified against mainnet on 2026-06-11, and each is corroborated by independently verified findings at the same locations. Low-severity, architecture, and docs findings were not adversarially verified by design. All such cases are marked inline.


## Critical

### Duplicate VbtcV2TokenTransfer rows on mainnet double-count live wallet balances

**Severity:** critical · **Category:** correctness · **Location:** `rbx/management/commands/reprocess_vbtc_v2.py:59` · **Found by:** vbtc-data-invariants

Mainnet has duplicate VbtcV2TokenTransfer rows for the same chain transaction: rows (1,4) both record tx dd9d567a243e10dbf34779508febd171301d272154e8fe835dd6346dc989ec72 for token 2, and rows (2,5) both record tx 9fe258120a42a9bdbe03772eaa72049ac7a211263e51942df4e12e40697e3a52 for token 7. The duplicates were created by rerunning reprocess_vbtc_v2 before commit 727978b added get_or_create at rbx/tasks.py:913; the commit prevented NEW duplicates but never cleaned existing rows (row id 6 for token 8 is missing from the id sequence, evidence of a manual partial cleanup that skipped tokens 2 and 7). Concrete failure: VbtcV2Token.addresses (served by VbtcV2TokenSerializer field 'addresses' to the web wallet) reports RNiQrW3aBUWZhfadqKxPuN46iGaR13ox7P as holding 0.0002 vBTC in token 2 when the on-chain transfer was only 0.0001 — the user sees 2x their real balance and can craft a transfer/withdrawal for funds they do not have. There is also no DB unique constraint on (token_id, transaction_id), so get_or_create is the only guard and it is race-prone under concurrent processing. Related latent bug folded in: the withdrawal-complete handler looks up VbtcV2WithdrawalRequest.objects.get(request_transaction__hash=...) at rbx/tasks.py:978 — if the same reprocessing path ever duplicates a withdrawal row, .get() raises MultipleObjectsReturned and the withdrawal stays 'requested' forever.

**Reviewer evidence:**

SQL: SELECT token_id, transaction_id, count(*), array_agg(id) FROM rbx_vbtcv2tokentransfer GROUP BY token_id, transaction_id HAVING count(*)>1 → token 2 tx dd9d567a... rows {1,4}; token 7 tx 9fe25812... rows {2,5}. Mainnet has only 4 type-26 transactions but 6 transfer rows. Git: commit 727978b 'Use get_or_create for V2 transfer and withdrawal records to prevent duplicates' (no data cleanup).

**Manually re-verified 2026-06-11:** psql against mainnet returned exactly the claimed duplicates — token 2 tx `dd9d567a…` rows {1,4}, token 7 tx `9fe25812…` rows {2,5} — and `VbtcV2TokenTransfer` (`rbx/models.py:1205`) has no unique constraint on (token, transaction). Corroborated by independently verified high-severity findings at `rbx/models.py:1172` and `rbx/models.py:1205`.


### V2 ownership transfer breaks addresses accounting: claims exceed BTC backing on token 7

**Severity:** critical · **Category:** correctness · **Location:** `rbx/models.py:1189` · **Found by:** vbtc-data-invariants

VbtcV2Token.addresses credits the CURRENT owner with global_balance + total of ALL completed withdrawals (models.py:1187-1193), assuming the owner was constant for the token's whole history. The TKNZ_TX Transfer() handler (rbx/tasks.py:846-848, commit 3d83402) just flips owner_address with no compensating ledger entry. Live failure on mainnet token 7 (d11a9ef3f98d4723849d5463596a6c21:1779979897): RNiQ... minted it, withdrew 0.0002 BTC (completed withdrawal id 3), transferred 0.0001 vBTC to RPKx..., then transferred OWNERSHIP to RPKx on 2026-06-02 (tx 4221154...). The addresses property now computes RPKx = 0.0011885900 while the deposit address holds only 0.0007885900 BTC (global_balance) — 0.0004 BTC of unbacked claims served to the wallet (≈51% inflation; 0.0001 of it from the duplicate-row finding, 0.0003 purely from this bug because the old owner's withdrawal compensation is credited to the new owner and the old owner's resulting −0.0004 entry is silently dropped by the >0 filter). A withdrawal attempt for the displayed amount would either fail FROST signing or consume other holders' backing. Related: the handler also swallows NFT-owner update failures with a bare 'except Exception: pass' at tasks.py:854-855.

**Reviewer evidence:**

SQL replication of the property on mainnet returned RPKx=0.0011885900, RNiQ=-0.0004000000 for token 7 whose global_balance=0.00078859. Ownership-transfer tx confirmed: SELECT ... FROM rbx_transaction WHERE type=18 AND data::text ILIKE '%d11a9ef3%' → hash 422115482c56955faa9127109bdf2947e0a0b9bdb1c0865554d7e88dde97e91c, RNiQ→RPKx, 2026-06-02.

**Manually re-verified 2026-06-11:** `rbx/models.py:1189-1193` credits `global_balance + total_withdrawn` to the *current* `owner_address` with no compensating ledger entry at ownership transfer, and the `bal > 0` filter at line 1202 silently drops the prior owner's negative residual. Corroborated by two independently verified high-severity findings at the same location.


### V2 mints silently dropped when CLI has no SC data — 2 of 13 mainnet V2 tokens missing

**Severity:** critical · **Category:** correctness · **Location:** `rbx/tasks.py:336` · **Found by:** vbtc-tknz-processing

In the mint branch (NFT_MINT/TKNZ_MINT/VBTC_V2_MINT), if get_nft(identifier) returns None the handler does `logging.error('No SC data found.'); return` — no retry, no raise, no marker row. get_nft (rbx/client.py:466) swallows all exceptions and returns None after 5 tries, and the CLI is known to fail its first request on cold start and to lag materializing smart-contract state right after a block is crafted. Result: the Nft and VbtcV2Token rows are never created, and every subsequent TKNZ_TX Transfer(), VBTC_V2_TRANSFER, and withdrawal for that sc_identifier is silently skipped by the DoesNotExist guards (tasks.py:846-859, 908-911, 937-940). Confirmed live: mainnet has 13 type-25 VBTC_V2_MINT transactions but only 11 VbtcV2Token rows; ContractUIDs 672076ec1b164936819663e867f8a1f4:1779846489 (tx 3060143c..., height 6544281) and 847c505ea6264e81913cdcbe2566e94b:1779900857 (tx 67d6c68e..., height 6548606) have neither an Nft row nor a VbtcV2Token row. Those two tokenized-BTC vaults are invisible to the explorer API; any future ownership transfer or withdrawal against them will be dropped without alerting.

**Reviewer evidence:**

Mainnet: SELECT count(*) FROM rbx_transaction WHERE type=25 → 13; SELECT count(*) FROM rbx_vbtcv2token → 11. SELECT ... FROM rbx_nft WHERE identifier IN ('672076ec...:1779846489','847c505e...:1779900857') → 0 rows. Code: tasks.py lines 334-339 returns on falsy get_nft; client.py get_nft returns None on persistent failure instead of raising (so the task's autoretry_for=[RBXException] never fires).

**Manually re-verified 2026-06-11:** psql against mainnet shows 13 type-25 mint transactions vs 11 `VbtcV2Token` rows, and `rbx/tasks.py:336-339` returns on missing SC data with the recovery path commented out (`# handle_unavailable_nft(tx, parsed)`). Corroborated by the independently verified high-severity finding at the same location.


### addresses property inflates displayed vBTC claims after ownership transfer (live on mainnet token 7)

**Severity:** critical · **Category:** correctness · **Location:** `rbx/models.py:1189` · **Found by:** vbtc-token-model

VbtcV2Token.addresses attributes the entire global_balance (plus the total_withdrawn add-back) to the CURRENT owner_address, while the PREVIOUS owner's net-negative ledger history (transfers out + withdrawals made while they were owner) is silently discarded by the `bal > 0` filter at line 1202. Before filtering, the entries always sum exactly to global_balance (transfers and withdrawals net to zero); every negative entry that gets clamped therefore inflates the displayed total above the actual BTC backing. Concrete live failure: mainnet token id 7 (sc d11a9ef3f98d4723849d5463596a6c21:1779979897). History: RNiQ minted, transferred 0.0001 to RPKx, withdrew 0.0002 (completed), then ownership-transferred the token to RPKx (TKNZ_TX type 18, hash 422115482c56955faa9127109bdf2947e0a0b9bdb1c0865554d7e88dde97e91c, 2026-06-02). Computed addresses today: {RPKx: 0.00118859} while global_balance is 0.00078859 — the API (VbtcV2TokenSerializer exposes 'addresses') reports 51% more vBTC claims than BTC actually held; RNiQ's hidden entry is -0.0004. Commit 0732472's own message admits the filter is a display patch 'while a more robust ownership-aware calculation is considered'. The withdrawal add-back at lines 1187-1193 compounds this: withdrawals completed by a PRIOR owner are credited to the NEW owner. Fix direction: record an ownership-transfer ledger event (move the old owner's residual claim to the new owner at transfer time) instead of pinning global_balance to whoever currently owns the token, and surface (not hide) negative balances as an integrity alarm.

**Reviewer evidence:**

Mainnet rbx_vbtcv2token id 7: owner_address=RPKxShZhfytjfffG8sQZG5ZTj9qqcAPgvJ, global_balance=0.00078859. rbx_vbtcv2tokentransfer rows (token 7): RNiQ→RPKx 0.0001 (x2, one is a duplicate). rbx_vbtcv2withdrawalrequest id 3: RNiQ 0.0002 completed. Reproduced property math: raw entries {RPKx: 0.00118859, RNiQ: -0.0004}; displayed {RPKx: 0.00118859}; displayed sum 0.00118859 vs gb 0.00078859. Ownership transfer tx type 18 at height 6589566 confirmed in rbx_transaction.

**Manually re-verified 2026-06-11:** `rbx/models.py:1189-1193` credits `global_balance + total_withdrawn` to the *current* `owner_address` with no compensating ledger entry at ownership transfer, and the `bal > 0` filter at line 1202 silently drops the prior owner's negative residual. Corroborated by two independently verified high-severity findings at the same location.


### sync_block silently skips a block when the node returns unparseable/null data, permanently losing the block

**Severity:** critical · **Category:** correctness · **Location:** `rbx/tasks.py:150` · **Found by:** block-sync

sync_block does `data = get_block(height); if not data: return` with no log, no retry, no marker. get_block (rbx/client.py:69-79) returns None whenever the node responds 200 with a body that isn't valid JSON (or a literal `null`, which response.json() decodes to None — exactly what the node returns when it does not yet have the block, e.g. a tip-of-chain race or a momentarily inconsistent node). Because sync_blocks (rbx/management/commands/sync_blocks.py:35-39) keeps iterating the rest of the range, later heights in the same run still get created, so the cursor (Max(height)) jumps past the skipped height and the next run starts at local_max+1. The block is never fetched again. This is confirmed in production: mainnet has 5 permanently missing block heights, including 6638787 which sits between two synced blocks (6638786 and 6638788 both exist), i.e. it was skipped mid-run and never repaired. Any transactions in those blocks were never indexed and never applied to address balances.

**Reviewer evidence:**

Mainnet: SELECT (max(height)-min(height)+1)-count(*) FROM rbx_block → 5 missing. Gap heights: 6638787, 1781020, 1617991, 1561322, 1441747. Neighbors 6638786 and 6638788 exist with 1 tx each. Code: rbx/tasks.py:150-152 returns silently; rbx/client.py:76-79 returns None on JSONDecodeError.

**Manually re-verified 2026-06-11:** psql against mainnet confirms exactly 5 missing heights (min 0, max 6,651,306, 6,651,302 rows), and `rbx/tasks.py:150-152` reads `data = get_block(height); if not data: return` — a silent return that also bypasses the task's `autoretry_for=[RBXException]` (no exception is ever raised). Corroborated by the independently verified high-severity gap-detection finding at `rbx/management/commands/sync_blocks.py:32`.



## High

### VbtcV2Token.addresses inflates balances after ownership transfer — live on mainnet

**Severity:** high · **Category:** correctness · **Location:** `rbx/models.py:1189` · **Found by:** vbtc-docs-conformance

The addresses property credits the CURRENT owner with global_balance + total_withdrawn, applies net transfer ledger entries per address, then filters out negative balances. When ownership is reassigned by the TKNZ_TX Transfer() handler (rbx/tasks.py:844-856, commit 3d83402), the OLD owner's transfer-out and withdrawal debits become orphaned negatives (silently dropped by the bal > 0 filter at models.py:1202) while the NEW owner receives the full global_balance credit on top of any transfer credits they already had — the same backing BTC is counted twice. Concrete failure on mainnet token d11a9ef3f98d4723849d5463596a6c21:1779979897: original owner RNiQ transferred 0.0001 vBTC to RPKx, completed a 0.0002 withdrawal, then transferred ownership to RPKx on 2026-06-02 (tx 422115482c56955faa9127109bdf2947e0a0b9bdb1c0865554d7e88dde97e91c). addresses now reports RPKx = 0.00118859 while the token's actual BTC backing (global_balance) is 0.00078859 — over-reported by ~51% (0.0003 from the ownership-transfer orphaning + 0.0001 from a duplicate ledger row, see separate finding). Butterfly's documented balance aggregation (docs/butterfly-vbtc-v2-architecture.md:40) sums exactly these values, so users will see phantom BTC and butterfly's multi-token coin selection will attempt sends that the CLI rejects. Honorable mention: the bal > 0 filter (commit 0732472) masks every such conservation violation, making sum(addresses) != global_balance undetectable from API responses.

**Reviewer evidence:**

Mainnet SQL: rbx_vbtcv2token d11a9ef3 has owner_address=RPKxShZhfytjfffG8sQZG5ZTj9qqcAPgvJ, global_balance=0.00078859. rbx_vbtcv2tokentransfer for that token: RNiQ->RPKx 0.0001 (x2 dup rows). rbx_vbtcv2withdrawalrequest id=3: 0.0002 COMPLETED, requestor RNiQ. rbx_transaction type=18 hash 4221154... from RNiQ to RPKx dated 2026-06-02. Code path: models.py:1180-1202 (owner credit + negative filter), tasks.py:844-856 (owner_address reassignment with no ledger compensation).

**Verification (high confidence):** Confirmed by direct code reading and live mainnet data. The addresses property (rbx/models.py:1189-1202) credits the CURRENT owner with global_balance + total_withdrawn and filters out negative balances, while the TKNZ_TX Transfer() handler in rbx/tasks.py reassigns owner_address with no ledger compensation. For mainnet token d11a9ef3...:1779979897 (id=7): owner=RPKx, global_balance=0.00078859, two duplicate transfer rows RNiQ->RPKx 0.0001 (ids 2 and 5, same tx hash 9fe25812...), completed withdrawal 0.0002 by RNiQ, and a type-18 ownership transfer tx 4221154... RNiQ->RPKx on 2026-06-02. Hand-evaluating the property: RPKx = 0.0002 (transfers) + 0.00078859 (global) + 0.0002 (total_withdrawn) = 0.00118859; RNiQ = -0.0004, silently dropped by the bal > 0 filter. Reported balance exceeds actual BTC backing by 0.0004 (~51% over-report). No guard exists elsewhere: VbtcV2TokenSerializer (api/btc/serializers.py) exposes addresses verbatim, and docs/butterfly-vbtc-v2-architecture.md documents butterfly summing addresses[user] across tokens for coin selection. Severity remains high (not critical) because the CLI is the settlement authority and will reject over-spends, so impact is phantom displayed balances and failed sends, not fund loss.

**Verification evidence:**

rbx/models.py:1189-1193 "entries[owner_address] = (entries.get(owner_address, Decimal(0)) + self.global_balance + total_withdrawn)" plus models.py:1202 "return {addr: bal for addr, bal in entries.items() if bal > 0}"; rbx/tasks.py Transfer() handler: "v2_token.owner_address = tx.to_address; v2_token.save()" with no ledger compensation. Mainnet SQL: rbx_vbtcv2token id=7 sc_identifier=d11a9ef3f98d4723849d5463596a6c21:1779979897 owner_address=RPKxShZhfytjfffG8sQZG5ZTj9qqcAPgvJ global_balance=0.00078859; rbx_vbtcv2tokentransfer token_id=7: two rows (ids 2,5) RNiQ->RPKx 0.0001 each, both transaction_id=9fe258120a42a9bdbe03772eaa72049ac7a211263e51942df4e12e40697e3a52; rbx_vbtcv2withdrawalrequest id=3 requestor=RNiQ amount=0.0002 status=completed; rbx_transaction hash=422115482c56955faa9127109bdf2947e0a0b9bdb1c0865554d7e88dde97e91c type=18 RNiQ->RPKx 2026-06-02. Hand evaluation: addresses returns {RPKx: 0.00118859} vs actual backing 0.00078859 (RNiQ at -0.0004 filtered out).


### Duplicate VbtcV2TokenTransfer ledger rows in production; no uniqueness constraint

**Severity:** high · **Category:** correctness · **Location:** `rbx/models.py:1205` · **Found by:** vbtc-docs-conformance

VbtcV2TokenTransfer has no unique constraint on (token, transaction). The indexer uses get_or_create(token=token, transaction=tx) (rbx/tasks.py:913), which is not race-safe without a DB constraint and does not retroactively fix rows created before that change. Mainnet currently contains duplicate ledger rows: tx dd9d567a243e10dbf34779508febd171301d272154e8fe835dd6346dc989ec72 appears as transfer rows id 1 AND 4, and tx 9fe258120a42a9bdbe03772eaa72049ac7a211263e51942df4e12e40697e3a52 appears as rows id 2 AND 5 — i.e. 2 of the 4 distinct V2 transfers ever made are double-counted. Every duplicate doubles the credit/debit in VbtcV2Token.addresses, so per-address balances served by /api/btc/vbtc-v2/* are wrong today (e.g. token 4bb6f099: RNiQ shown holding 0.0002 instead of 0.0001). Failure scenario: butterfly or the web wallet trusts addresses for spendable balance, prepares a transfer for the inflated amount, and the CLI rejects it — or worse, a UI shows the recipient vBTC they do not have. Fix needs both a data dedupe and a UniqueConstraint(fields=['token','transaction']); same gap exists on VbtcV2WithdrawalRequest (token, request_transaction).

**Reviewer evidence:**

Mainnet SQL result: SELECT tr.id, tr.transaction_id FROM rbx_vbtcv2tokentransfer tr WHERE tr.id IN (1,2,4,5) shows rows 1 and 4 share transaction_id dd9d567a..., rows 2 and 5 share 9fe25812.... Both pairs have identical from/to/amount/created_at. Model definition rbx/models.py:1205-1211 has no Meta.constraints.

**Verification (high confidence):** Every element of the claim verified against code and live mainnet data. (1) Model gap: rbx/models.py:1205-1214 defines VbtcV2TokenTransfer with no Meta class at all — no UniqueConstraint on (token, transaction). Live pg_constraint for rbx_vbtcv2tokentransfer shows only the PK and two FKs, no unique index. Same gap on VbtcV2WithdrawalRequest (rbx/models.py:1217-1244, no Meta). (2) Duplicates exist in production: the GROUP BY/HAVING query returns exactly the 2 claimed transaction hashes, each with count=2 (rows id 1&4 for tx dd9d567a... on token 2, rows id 2&5 for tx 9fe25812... on token 7) — identical from/to/amount/created_at, so 2 of the 4 distinct V2 transfers ever made are double-counted. (3) Origin: git -L history shows the code was VbtcV2TokenTransfer.objects.create() in 0d28768 and was changed to get_or_create in 727978b ("Use get_or_create ... to prevent duplicates") with no data cleanup, so pre-existing duplicates persist; get_or_create at rbx/tasks.py:913 is also not race-safe without a DB constraint (though blocks-worker concurrency=1 makes a future race unlikely). (4) Impact confirmed: VbtcV2Token.addresses (rbx/models.py:1164-1202) iterates raw transfer rows with no dedup, so each duplicate doubles the credit/debit. For token 2 (4bb6f0991eda..., owner RKuU, global_balance 0.0008): duplicated RKuU->RNiQ 0.0001 transfer yields RNiQ=0.0002 (should be 0.0001) and owner RKuU=0.0006 (should be 0.0007) — exactly as claimed. Token 7 (owner RPKx) is overstated by 0.0001 for RPKx, and RNiQ's residual balance is hidden by the bal>0 filter. addresses is serialized in VbtcV2TokenSerializer (api/btc/serializers.py:93) and served by the /api/btc/vbtc-v2/* endpoints, so wrong per-address balances are live in production today. Mitigations on severity: amounts are tiny (0.0001 BTC), the CLI is the authority and would reject an overdrawn transfer, and rbx_vbtcv2withdrawalrequest currently has zero duplicates. Severity stays high (live financial data served wrong on production mainnet) but not critical (no fund loss; CLI rejects inflated spends). Fix as claimed: dedupe rows 4 and 5, add UniqueConstraint(fields=['token','transaction']) and the analogous constraint on VbtcV2WithdrawalRequest (token, request_transaction).

**Verification evidence:**

Mainnet SQL: SELECT transaction_id, count(*) FROM rbx_vbtcv2tokentransfer GROUP BY transaction_id HAVING count(*) > 1 → [{"transaction_id":"9fe258120a42a9bdbe03772eaa72049ac7a211263e51942df4e12e40697e3a52","count":"2"},{"transaction_id":"dd9d567a243e10dbf34779508febd171301d272154e8fe835dd6346dc989ec72","count":"2"}]. pg_constraint on rbx_vbtcv2tokentransfer lists only PK(id) + 2 FKs — no unique on (token_id, transaction_id). Code: rbx/models.py:1205-1214 (no Meta/constraints); rbx/models.py:1172-1174 sums transfers without dedup ("entries[t.to_address] = entries.get(t.to_address, Decimal(0)) + t.amount"); rbx/tasks.py:913 get_or_create (was .create() until commit 727978b "Use get_or_create for V2 transfer and withdrawal records to prevent duplicates"); exposed via "addresses" in VbtcV2TokenSerializer, api/btc/serializers.py:93.


### Type 29 (VBTC_V2_WITHDRAWAL_CANCEL) transactions are never processed; withdrawal state machine has no cancel/expiry path

**Severity:** high · **Category:** correctness · **Location:** `rbx/tasks.py:958` · **Found by:** vbtc-docs-conformance

The transaction processor handles types 25/26/27/28 but has no branch for type 29 (cancel) — the elif chain ends at VBTC_V2_WITHDRAWAL_COMPLETE (tasks.py:958-996). Repo-wide grep shows VBTC_V2_WITHDRAWAL_CANCEL appears only in the enum (models.py:177) and name mapping. Yet cancel is a fully documented, production-exposed operation: /api/btc/vbtc-v2/withdraw/cancel/prepare|send/ exist (api/btc/urls.py:65-66) and the SDK doc specifies cancelWithdrawal(). Failure scenario: a user cancels a withdrawal via the live endpoints; the chain accepts the type-29 TX, but the explorer leaves the VbtcV2WithdrawalRequest status='requested' and token.is_pending_withdrawal=True forever — the API then reports a phantom pending withdrawal, GUIs block new withdrawals, and butterfly retries completion of a cancelled request. The model has no CANCELLED status to even record it (models.py:1218-1220), matching the SDK doc's 'requested'|'completed' enum — so the doc and code share the same hole. Related operational evidence: mainnet already has 2 requests stuck status='requested' since 2026-05-29 (ids 4 and 8) with is_pending_withdrawal=true on both tokens for 12 days; nothing in the codebase can ever clear that flag except a type-28 completion, which for id 4 may now be impossible because the token's ownership was transferred away from a state where completion was being driven (complete/prepare requires owner_address while the requestor RNiQ is no longer the owner).

**Reviewer evidence:**

grep -rn WITHDRAWAL_CANCEL over *.py returns only rbx/models.py:177 and :300 (enum + label). Mainnet: SELECT type,count(*) FROM rbx_transaction WHERE type IN (25,26,27,28,29,30) GROUP BY type → no type-29 rows yet (zero cancels so far, so unbroken only by luck). Stuck rows: withdrawal ids 4 and 8 status='requested', created 2026-05-29, tokens d11a9ef3/6d893dce both is_pending_withdrawal=true today (2026-06-10).

**Verification (high confidence):** Every checkable element of the finding holds. The process_transaction elif chain in rbx/tasks.py ends at VBTC_V2_WITHDRAWAL_COMPLETE (lines 958-996) with no branch for type 29; sync_block stores all TX types and dispatches unconditionally, so a type-29 cancel TX would be indexed but silently ignored. VBTC_V2_WITHDRAWAL_CANCEL appears only in the enum (models.py:177) and display label (models.py:300). The cancel endpoints (api/btc/urls.py:65-66 → views.py:635-662) are live, production-exposed CLI proxies that perform no local DB update, so explorer state after a cancel can only be fixed by TX processing that does not exist. VbtcV2WithdrawalRequest.Status has only REQUESTED/COMPLETED — no CANCELLED value to record one. is_pending_withdrawal is written in exactly two places (set at tasks.py:955, cleared at tasks.py:995); no reconciliation task exists. Live mainnet confirms zero type-29 transactions to date (bug is latent, unbroken only because no one has cancelled yet) and confirms the operational corollary: withdrawal ids 4 and 8 stuck status='requested' since 2026-05-29 with is_pending_withdrawal=true for 12 days, and id 4's token ownership has moved to RPKxShZ... away from requestor RNiQrW3..., consistent with the claim that completion may now be unreachable. Refutation attempts (alternate handlers, function-name dispatch under TKNZ_TX, periodic sync, view-side writes) all came up empty. Severity high stands: a documented, SDK-specified, production-live operation will permanently corrupt withdrawal state on first use, with no status value or repair path, and the stuck-flag failure mode is already manifest on mainnet.

**Verification evidence:**

rbx/tasks.py:958-996 — final branch of process_transaction is `elif tx.type == Transaction.Type.VBTC_V2_WITHDRAWAL_COMPLETE:` ending with `token.is_pending_withdrawal = False; token.save()`; grep -rn WITHDRAWAL_CANCEL over *.py returns only rbx/models.py:177 and :300. Mainnet SQL: SELECT type,count(*) FROM rbx_transaction WHERE type IN (25,26,27,28,29,30) GROUP BY type → {25:13, 26:4, 27:8, 28:6} (no type 29). SELECT w.id,w.status,w.created_at,t.owner_address,t.is_pending_withdrawal ... → id 4: status='requested', created 2026-05-29T18:15:36Z, token d11a9ef3 owner RPKxShZhfytjfffG8sQZG5ZTj9qqcAPgvJ (requestor RNiQrW3aBUWZhfadqKxPuN46iGaR13ox7P), is_pending_withdrawal=true; id 8: status='requested', created 2026-05-29T20:27:49Z, token 6d893dce, is_pending_withdrawal=true. Cancel endpoints live at api/btc/urls.py:65-66; views.py:647/:662 proxy to CLI with no DB write. models.py Status choices: REQUESTED/COMPLETED only.


### Two on-chain V2 mints silently dropped — tokens invisible to explorer and wallet

**Severity:** high · **Category:** correctness · **Location:** `rbx/tasks.py:336` · **Found by:** vbtc-data-invariants

process_transaction bails with only logging.error('No SC data found.') and returns when get_nft() fails during mint processing (rbx/tasks.py:336-339). There is no retry, dead-letter queue, or reconciliation job. On mainnet, 13 VBTC_V2_MINT (type 25) transactions exist but only 11 VbtcV2Token rows: sc 672076ec1b164936819663e867f8a1f4:1779846489 (mint tx 3060143c0495882bf9a12f5e7ef9f1de1bbb5fda1f7fab69f0b21add06099fe0) and sc 847c505ea6264e81913cdcbe2566e94b:1779900857 (mint tx 67d6c68eed6a7ac75b0717b0111dd92308b6eeff9d827ff7f8c92edc2789e811), both minted 2026-05-27 by RNiQrW3aBUWZhfadqKxPuN46iGaR13ox7P, have NO VbtcV2Token row and NO rbx_nft row, and no burn tx exists for either. Concrete failure: these tokens (and their BTC deposit addresses) do not appear in any wallet/API response; any BTC deposited to them is invisible, and any future transfer/withdrawal tx referencing them will also be dropped ('VbtcV2Token ... not found'). reprocess_vbtc_v2 cannot fix them either, because get_nft must succeed at reprocess time and nobody is alerted to run it.

**Reviewer evidence:**

SQL: type-25 mint count = 13 vs SELECT count(*) FROM rbx_vbtcv2token = 11. Per-mint join showed token_exists=0 for the two sc_uids above; rbx_nft lookup for both identifiers returned 0 rows; full-text search of rbx_transaction.data found no burn/other txs for these UIDs.

**Verification (high confidence):** Code and live mainnet data both confirm the finding. rbx/tasks.py:336-339 silently returns after logging.error('No SC data found.') when get_nft fails, with the recovery handler handle_unavailable_nft commented out; VbtcV2Token creation (tasks.py:455-477) is only reachable past that gate. get_nft (rbx/client.py:466-482) retries only on request exceptions, not on an empty/null JSON body, so a CLI that responds without SC data (cold start, SC not yet synced) causes a permanent silent drop. Live mainnet query confirms 13 type-25 mint txs vs 11 rbx_vbtcv2token rows, and the exact two txs/sc_uids cited by the reviewer have token_exists=false and nft_exists=false; a full-text search of rbx_transaction.data for both UIDs returns only the mint txs (no burns/transfers), so both tokens are live on-chain but invisible to the explorer. Both mints were on-chain successes (status-999 txs are filtered before insert at tasks.py:192-193). No reconciliation exists: process_transaction is called inline from block sync (tasks.py:216) with no retry, and health_check.py has zero vbtc/mint/token checks, so nobody is alerted. One overstatement: 'reprocess_vbtc_v2 cannot fix them' is too strong — the command re-runs process_transaction and get_nft would likely succeed now since the SCs still exist on-chain; recovery is a one-command manual fix, but nothing detects the gap or triggers it. Severity stays high (not critical): 2 of 13 V2 mints invisible to wallet/APIs and future txs for them would be dropped, but no on-chain funds are lost and manual recovery is straightforward.

**Verification evidence:**

Mainnet SQL (per-mint join): WITH m AS (SELECT hash, from_address, date_crafted, (regexp_match(data::text, '"ContractUID\\?":\\?"([^"\\]+)'))[1] AS sc FROM rbx_transaction WHERE type=25) SELECT ... LEFT JOIN rbx_vbtcv2token v ON v.sc_identifier=m.sc LEFT JOIN rbx_nft n ON n.identifier=m.sc → 13 rows; rows 7-8: {hash: 3060143c0495882bf9a12f5e7ef9f1de1bbb5fda1f7fab69f0b21add06099fe0, sc: 672076ec1b164936819663e867f8a1f4:1779846489, token_exists: false, nft_exists: false} and {hash: 67d6c68eed6a7ac75b0717b0111dd92308b6eeff9d827ff7f8c92edc2789e811, sc: 847c505ea6264e81913cdcbe2566e94b:1779900857, token_exists: false, nft_exists: false}, both from_address RNiQrW3aBUWZhfadqKxPuN46iGaR13ox7P, 2026-05-27. Counts: mint_tx_count=13, token_count=11. Full-text search for both UIDs in rbx_transaction.data returned only the 2 mint txs (no burn/transfer). Code: rbx/tasks.py:336-339 'if not data: # handle_unavailable_nft(tx, parsed) / logging.error("No SC data found.") / return'.


### GET endpoint and balance sweep do stale full-row token.save(), can silently revert ownership transfer and is_pending_withdrawal

**Severity:** high · **Category:** correctness · **Location:** `api/btc/views.py:235` · **Found by:** vbtc-tknz-processing

VbtcV2DetailView.get (a read endpoint the wallet GUI polls) loads the token, makes an external HTTP call to BtcClient.get_balance, then calls token.save() — Django saves ALL fields, not just the balance fields. The same pattern exists in btc/management/commands/update_vbtc_balances.py:18-27, which runs every 10 min on the vbtc-worker and materializes the entire token list up front (len(tokens)), then sleeps 0.5s + does one BTC API call per token, so the last token's in-memory snapshot can be many seconds to minutes stale. Meanwhile TKNZ_TX Transfer() ownership updates run on the blocks-worker (tasks.py:846-848). Lost-update scenario: web/vbtc-worker loads token (owner=A) → blocks-worker processes the ownership transfer, sets owner=B, saves → web/vbtc-worker saves its stale copy → owner reverted to A with no error anywhere, and there is no later event that re-corrects it (the TKNZ_TX is already processed). Same race clobbers is_pending_withdrawal set at tasks.py:955/995. Because every detail-page view triggers a write, the race window is hit in normal operation, not just during the periodic sweep. Fix shape: save(update_fields=[...]) everywhere balances are refreshed, and update ownership via queryset .update().

**Reviewer evidence:**

api/btc/views.py:226-236 (get_object → external HTTP → token.save()); btc/management/commands/update_vbtc_balances.py:18-27 (full queryset materialized, save() per token after sleep); project/celery.py:101 routes update_vbtc_balances to vbtc_queue while block sync (and thus process_transaction) runs on blocks_queue via project/celery.py:49-53 — two different workers plus gunicorn web all write the same rows with full-row saves.

**Verification (high confidence):** Every element of the claim checks out in code. (1) VbtcV2DetailView.get (api/btc/views.py:226-236) loads the token, makes an external BtcClient.get_balance HTTP call, then calls token.save() with no update_fields — Django writes ALL columns, so a concurrent ownership change or is_pending_withdrawal flip committed during the external-call window is silently reverted. (2) update_vbtc_balances (btc/management/commands/update_vbtc_balances.py:18-29) materializes the whole queryset up front via tqdm(total=len(tokens)) and then saves each cached instance after 0.5s sleeps + per-token BTC API calls — full-row saves of snapshots that are seconds-to-tens-of-seconds stale; it runs every 10 minutes on vbtc_queue (project/celery.py:29-31, 101). (3) The competing writers are on different processes: Transfer() ownership update and is_pending_withdrawal writes happen in rbx/tasks.py on blocks_queue (sync_the_blocks at project/celery.py:49), the sweep on the vbtc-worker, and the GET under gunicorn web. (4) VbtcV2Token (rbx/models.py:1129) has no save() override, no select_for_update, no refresh_from_db, no update_fields anywhere in the three files (grep confirmed empty), and owner_address/is_pending_withdrawal share the row with the balance fields. The lost-update scenario therefore exists exactly as described and a hit is silent and permanent — the TKNZ_TX is already processed, so nothing re-corrects the row, and VbtcV2Token.addresses derives per-address balances from owner_address, so a reverted owner also corrupts balance attribution. Mitigating evidence from live mainnet: only 11 tokens exist, token.owner_address currently matches nft.owner_address on all rows (the race has not demonstrably fired yet), and ownership transfers are rare, so the per-event probability is modest. Aggravating: the wallet GUI polls the detail endpoint precisely during transfer/withdrawal flows, correlating the write window with the events it can clobber. Keeping severity high because the failure mode is silent ownership/state corruption of a Bitcoin-backed token in live production with no self-healing path; the fix (update_fields=[...] / queryset .update()) is trivial.

**Verification evidence:**

api/btc/views.py:226-236: "def get(self, request, *args, **kwargs): token = self.get_object(); client = BtcClient(); balance_info = client.get_balance(token.deposit_address); if balance_info: token.global_balance = ...; token.save()" — full-row save after an external HTTP call on a read endpoint, no update_fields. grep for update_fields/select_for_update/refresh_from_db across api/btc/views.py, btc/management/commands/update_vbtc_balances.py, rbx/tasks.py returns zero matches, and rbx/models.py:1129 VbtcV2Token defines no save() override. Competing full-row writers confirmed: rbx/tasks.py Transfer() handler "v2_token.owner_address = tx.to_address; v2_token.save()" on blocks_queue, and "token.is_pending_withdrawal = True; token.save()" in VBTCWithdrawalRequest() handler; update_vbtc_balances scheduled every 10 min on vbtc_queue (project/celery.py:29-31,101). Live mainnet: SELECT count(*) FROM rbx_vbtcv2token → 11 tokens (2 pending withdrawal); token.owner_address == nft.owner_address on all 11 rows, so no observed corruption to date.


### reprocess_vbtc_v2 full run reverts V2 ownership to the original minter (transfer rides on type 18, which is not reprocessed)

**Severity:** high · **Category:** correctness · **Location:** `rbx/management/commands/reprocess_vbtc_v2.py:6` · **Found by:** vbtc-tknz-processing

VBTC_V2_TYPES only covers types 25-28, but V2 ownership transfer is a TKNZ_TX (type 18, Function 'Transfer()') per commit 3d83402. Reprocessing a type-25 mint re-executes the mint branch, which sets nft.owner_address = minter_address (tasks.py:373) and v2_token.owner_address = nft.owner_address (tasks.py:462) — clobbering any ownership transfer that happened after the mint, and nothing in the command replays the type-18 transfer to restore it. Concrete live scenario: token d11a9ef3f98d4723849d5463596a6c21:1779979897 was minted by RNiQrW3aBUWZhfadqKxPuN46iGaR13ox7P (tx 035a82ff..., height 6554877) and ownership-transferred to RPKxShZhfytjfffG8sQZG5ZTj9qqcAPgvJ (TKNZ_TX 42211548..., height 6589566; DB owner currently RPKx, correct). Running `python manage.py reprocess_vbtc_v2` (the natural remediation for the two dropped mints in finding 1) reverts that token's owner_address AND its NFT owner to RNiQ, with a pending 0.0003 withdrawal attached. The same hazard applies to oct_2025_reprocess.py (reprocesses NFT_MINT/TKNZ_MINT, resetting nft.owner_address to minter, discarding later NFT_TX transfers).

**Reviewer evidence:**

reprocess_vbtc_v2.py lines 6-11 (types 25-28 only); tasks.py:373 `nft.owner_address = minter_address`, tasks.py:462 `v2_token.owner_address = nft.owner_address`. Mainnet: mint tx 035a82ff from_address=RNiQ...; rbx_vbtcv2token owner_address=RPKx... for sc d11a9ef3...:1779979897; only TKNZ_TX since V2 launch is hash 42211548... height 6589566 RNiQ→RPKx.

**Verification (high confidence):** Every element of the claim checks out against code and live mainnet data. reprocess_vbtc_v2.py replays only types 25-28 through process_transaction(). The mint branch (tasks.py:324-382) get-or-creates the existing Nft and unconditionally sets nft.owner_address = tx.from_address (line 373) and the existing VbtcV2Token's owner_address = nft.owner_address (line 462), with no check for a later ownership transfer. V2 ownership transfer is processed exclusively under TKNZ_TX type 18 / Function "Transfer()" (tasks.py:833-856, commit 3d83402), which the command never replays; the type-26 VBTC_V2_TRANSFER handler (tasks.py:892-922) only records amount transfers and never touches owner_address. Live mainnet confirms the concrete scenario: token d11a9ef3f98d4723849d5463596a6c21:1779979897 was minted by RNiQ... (tx 035a82ff, type 25, height 6554877) and its owner is currently RPKx... via type-18 tx 42211548 (height 6589566, payload Function "Transfer()" with matching ContractUID). A full reprocess run would therefore revert the token and NFT owner to the original minter with nothing restoring the transfer. Only escape hatch is the early return when get_nft() fails, which does not apply to this live (non-burned) contract. The bug requires deliberate operator action and is manually recoverable, but since the command is the natural remediation for the dropped-mint finding, high severity is appropriate.

**Verification evidence:**

tasks.py:373 `nft.owner_address = minter_address` and tasks.py:462 `v2_token.owner_address = nft.owner_address` execute unconditionally on replay of type-25 mints (reprocess_vbtc_v2.py:6-11 replays only types 25-28); ownership transfer lives only in the type-18 branch tasks.py:846-848 `v2_token.owner_address = tx.to_address`. Mainnet: SELECT data FROM rbx_transaction WHERE hash='422115482c56955faa9127109bdf2947e0a0b9bdb1c0865554d7e88dde97e91c' -> type 18, height 6589566, '[{"Function":"Transfer()","ContractUID":"d11a9ef3f98d4723849d5463596a6c21:1779979897","ToAddress":"RPKxShZhfytjfffG8sQZG5ZTj9qqcAPgvJ",...}]'; mint tx 035a82ff... type 25 from_address=RNiQrW3aBUWZhfadqKxPuN46iGaR13ox7P; rbx_vbtcv2token.owner_address for that sc_identifier is currently RPKxShZhfytjfffG8sQZG5ZTj9qqcAPgvJ — a full reprocess would revert it to RNiQ.


### VbtcV2Token.addresses orphans the old owner's debits after ownership transfer — total claims exceed BTC backing

**Severity:** high · **Category:** correctness · **Location:** `rbx/models.py:1189` · **Found by:** vbtc-tknz-processing

addresses credits global_balance + total_withdrawn to the CURRENT owner_address and debits each completed withdrawal from its requestor, then filters out negative balances (line 1202). When ownership transfers, the previous owner's accumulated debits (outgoing amount transfers, past withdrawals) go negative and are silently dropped, while the new owner is credited total_withdrawn for withdrawals that happened before they owned the token. Live example, token d11a9ef3...:1779979897 (owner now RPKx after the height-6589566 transfer from RNiQ): transfers give RPKx +0.0002 (includes the dup from finding 4) and RNiQ -0.0002; completed withdrawal of 0.0002 was requested by RNiQ. addresses computes RPKx = 0.0002 + 0.00078859 (global) + 0.0002 (withdrawn) = 0.00118859, and RNiQ = -0.0004 → filtered. Total claims 0.00118859 vBTC vs 0.00078859 BTC actually on the deposit address — a 51% overstatement served to wallets. 0.0002 of that is the orphaned-debit mechanism (independent of the duplicate-row bug). Any token that has amount transfers or withdrawals followed by an ownership transfer will misreport this way.

**Reviewer evidence:**

models.py:1180-1202 (owner credited global_balance + total_withdrawn; >0 filter drops old owner's net debit). Mainnet rows: rbx_vbtcv2tokentransfer ids 2,5 (RNiQ→RPKx 0.0001 each); rbx_vbtcv2withdrawalrequest id 3 (completed, 0.0002, requestor RNiQ); rbx_vbtcv2token global_balance 0.00078859, owner RPKx.

**Verification (high confidence):** Confirmed on all three axes: code, live data, and absence of any compensating guard. (1) rbx/models.py:1189-1202 does exactly what the reviewer claims: it credits self.global_balance + total_withdrawn to the CURRENT owner_address, debits each completed withdrawal from its requestor, and drops negative entries via `if bal > 0`. (2) The ownership-transfer path (commit 3d83402, rbx/tasks.py) only sets v2_token.owner_address = tx.to_address — it creates no VbtcV2TokenTransfer row or any other ledger adjustment, so the old owner's accumulated debits are orphaned. Mainnet tx 422115482c56... at height 6589566 (type 18, Transfer(), ContractUID d11a9ef3f98d4723849d5463596a6c21:1779979897, RNiQ→RPKx) confirms the transfer happened. (3) Live mainnet token id 7: transfers ids 2 and 5 (RNiQ→RPKx, 0.0001 each, same transaction_id — the duplicate from the other finding), completed withdrawal id 3 (0.0002, requestor RNiQ), global_balance 0.00078859, owner RPKx. Hand evaluation of the property: RPKx = 0.0002 (transfers) + 0.00078859 (global) + 0.0002 (total_withdrawn) = 0.00118859; RNiQ = -0.0002 - 0.0002 = -0.0004, filtered out. Served claims total 0.00118859 vBTC against 0.00078859 BTC backing — 50.7% overstatement. Sanity check: with no ownership transfer the same arithmetic nets to exactly global_balance (RNiQ 0.00058859 + RPKx 0.0002), proving the overstatement is caused entirely by the ownership transfer orphaning the old owner's debits (0.0001 of the 0.0004 additionally attributable to the duplicate row). The value is wallet-facing: VbtcV2TokenSerializer (api/btc/serializers.py:93) includes "addresses" with no correction. No guard found anywhere. Severity stays high (not critical): it misreports balances to wallet clients but cannot move funds — the chain/CLI remains authoritative for actual spends.

**Verification evidence:**

rbx/models.py:1189-1202: `entries[owner_address] = (entries.get(owner_address, Decimal(0)) + self.global_balance + total_withdrawn)` ... `return {addr: bal for addr, bal in entries.items() if bal > 0}`. Mainnet (read-only): rbx_vbtcv2token id=7 → global_balance 0.00078859, owner RPKxShZhfytjfffG8sQZG5ZTj9qqcAPgvJ; rbx_vbtcv2tokentransfer token_id=7 → ids 2,5 both RNiQ→RPKx 0.0001 (same transaction_id 9fe25812...); rbx_vbtcv2withdrawalrequest token_id=7 → id 3 completed 0.0002 requestor RNiQ. Hand-eval: RPKx = 0.0002+0.00078859+0.0002 = 0.00118859 served; RNiQ = -0.0004 dropped; 0.00118859 > 0.00078859 backing. Ownership transfer confirmed: rbx_transaction hash 422115482c56955faa9127109bdf2947e0a0b9bdb1c0865554d7e88dde97e91c, height 6589566, type 18, Function "Transfer()", ContractUID d11a9ef3f98d4723849d5463596a6c21:1779979897. Commit 3d83402 adds only `v2_token.owner_address = tx.to_address` — no ledger compensation.


### Mid-block exception permanently drops the rest of a block's transactions; retry path dies on Transaction PK IntegrityError

**Severity:** high · **Category:** correctness · **Location:** `rbx/tasks.py:201` · **Found by:** vbtc-tknz-processing

sync_block creates the Block row first (get_or_create, line 165), then loops transactions with Transaction.objects.create (hash is the primary key, models.py:188) and calls process_transaction inline with no per-tx try/except. Any uncaught exception in process_transaction — KeyError on an unexpected TKNZ/V2 payload shape (e.g. parsed['RequestorAddress'] at line 946, parsed['BTCTransactionHash'] at line 988), json.JSONDecodeError, or MultipleObjectsReturned from the non-unique sc_identifier .get()s (only DoesNotExist is caught at 846/908/937/972) — aborts the task mid-block. The Block row already exists, so the next sync_blocks run computes start_height = local_max_height + 1 (sync_blocks.py:32, utils.py:27) and skips the failed block entirely: the remaining transactions of that block are never indexed (missing vBTC transfers/withdrawals, wrong balances), with only a dead Celery task as evidence. If the exception happens to be RBXException, autoretry re-runs sync_block, which then crashes on the first already-inserted tx with IntegrityError (create with duplicate PK), and the partial balance updates from the first attempt (lines 219-241) are also re-applied for any tx that did get re-created before the crash. The 999-status skip (line 192) plus this means TKNZ_TX correctness depends on every payload field existing forever.

**Reviewer evidence:**

tasks.py:165 (block created before tx loop), 201-216 (create + inline process_transaction, no try/except), models.py:188 (hash primary_key), sync_blocks.py:29-39 + utils.py:27-28 (resume from max Block.height, skipping partially-processed blocks). Withdrawal handlers access parsed['RequestorAddress'], parsed['FeeRate'], parsed['BTCTransactionHash'] with no .get() fallback (tasks.py:946-949, 988).

**Verification (high confidence):** The structural claim is fully verified in code, and the failure mode has demonstrably occurred in production. (1) /Users/tyler/prj/vfx/vfx-explorer/rbx/tasks.py:165 creates the Block row via get_or_create BEFORE the tx loop; lines 191-241 do Transaction.objects.create (201) + inline process_transaction(216) with no per-tx try/except and no transaction.atomic anywhere in tasks.py. (2) models.py:188 confirms hash is primary_key. (3) Production sync runs every 10s via project/celery.py sync_the_blocks -> call_command("sync_blocks") synchronously (management/commands/sync_blocks.py:39, no --async), and start_height = local_max_height + 1 (sync_blocks.py:32, utils.py:27-28 Max(height)) — so a mid-block exception leaves the Block row in place and the next run permanently skips the remaining txs. (4) KeyError surface confirmed: tasks.py:813 parsed["Function"], 946 parsed["RequestorAddress"], 949 parsed["FeeRate"], 988 parsed["BTCTransactionHash"] are direct indexing; only DoesNotExist is caught (820/841/857/874/909/938/973). sc_identifier is NOT unique on VbtcToken (models.py:1063) and VbtcV2Token (models.py:1130), so MultipleObjectsReturned is possible — though live mainnet currently has zero duplicate sc_identifiers, so that vector is theoretical today. (5) The RBXException retry path is real (autoretry_for=[RBXException], tasks.py:146; process_transaction calls get_nft at tasks.py:334/502 which raises RBXException per client.py) and the retry would die on IntegrityError at the FIRST already-inserted tx — but this means the reviewer's sub-claim that partial balance updates get re-applied on retry is overstated: the retry crashes before re-creating any tx (only master_node.block_count at tasks.py:160-161 double-increments). Also note the dominant production path calls sync_block synchronously, where autoretry never engages. (6) Empirical confirmation: exactly one block in all 6.64M mainnet blocks has zero indexed transactions (height 1065562); the CLI shows that block contains 1 on-chain tx (hash b0b40e29ed84...45fd) which is entirely absent from rbx_transaction — a permanent silent drop matching the claimed mechanism. (7) Refuting/limiting evidence: both reviewer-suggested heights (6544281, 6548606) and 10 sampled recent multi-tx blocks match chain tx counts exactly; all 21 recent vBTC-V2-era blocks look intact; a manual heal exists (validate_transactions --fix) but is unscheduled and itself re-applies balance deltas without resetting balances. Severity: incidence is historically tiny (1 in 6.6M), but the mode is silent, unrecoverable without manual audit, corrupts incrementally-maintained Address balances permanently, and the newly-live V2 withdrawal handlers hard-index CLI-controlled payload fields — one malformed payload from the CLI team silently drops the rest of a block of financial data. High stands.

**Verification evidence:**

Live mainnet: SELECT b.height FROM rbx_block b WHERE NOT EXISTS (SELECT 1 FROM rbx_transaction t WHERE t.block_id=b.height) -> exactly 1 row: height 1065562. CLI api/V1/SendBlock/1065562 -> chain_tx_count=1 (type 0, hash b0b40e29ed84b7e7d1c1293a2c0ec6812ebe205f713532cd52c30419a8aa45fd); SELECT ... FROM rbx_transaction WHERE hash='b0b40e29...45fd' -> 0 rows. Code: rbx/tasks.py:165 Block.objects.get_or_create(...) precedes the tx loop at 191; tasks.py:201-216 Transaction.objects.create(hash=...PK) + process_transaction(tx) with no try/except; management/commands/sync_blocks.py:32 "start_height = 0 if sync_all or not local_max_height else local_max_height + 1". Counter-evidence checked: blocks 6544281/6548606 and 10 recent multi-tx blocks all match chain counts; zero duplicate sc_identifiers in rbx_vbtctoken/rbx_vbtcv2token.


### Duplicate VbtcV2TokenTransfer rows in production double-count transfers in balances

**Severity:** high · **Category:** correctness · **Location:** `rbx/models.py:1172` · **Found by:** vbtc-token-model

The addresses property sums all VbtcV2TokenTransfer rows with no dedup, and production contains duplicate rows: ids 1 & 4 (token 2) and ids 2 & 5 (token 7) reference the SAME chain transaction hash with identical from/to/amount. There are 6 transfer rows but only 4 type-26 transactions on chain. Commit 727978b (2026-05-29) switched processing to get_or_create, which prevents NEW duplicates, but the pre-existing rows were never cleaned (the id-6 gap suggests a partial manual cleanup that missed these). Concrete live failure: token 2 (sc 4bb6f0991eda4c63b89d129514b149e2:1779157633) — addresses shows RNiQ holding 0.0002 when the on-chain transfer was 0.0001; the duplicate also silently shifts 0.0001 away from owner RKuU (0.0006 displayed vs 0.0007 actual). On token 7 the duplicate contributes 0.0001 of the inflation described in the ownership-transfer finding. Additionally there is no DB unique constraint on (token_id, transaction_id) — only PK and FKs exist — so get_or_create remains racy if two workers process the same tx concurrently. Fix: delete rows 4 and 5 via a vetted migration/operation, and add a unique constraint.

**Reviewer evidence:**

SQL: SELECT t.id,t.token_id,t.transaction_id FROM rbx_vbtcv2tokentransfer t ORDER BY t.id → ids 1/4 share hash dd9d567a243e10dbf34779508febd171301d272154e8fe835dd6346dc989ec72 (token 2), ids 2/5 share hash 9fe258120a42a9bdbe03772eaa72049ac7a211263e51942df4e12e40697e3a52 (token 7). SELECT type,COUNT(*) FROM rbx_transaction WHERE type=26 GROUP BY type → 4 transactions vs 6 transfer rows. pg_constraint on rbx_vbtcv2tokentransfer shows only PK + 2 FKs, no unique constraint.

**Verification (high confidence):** Every factual element of the finding was independently confirmed. (1) rbx/models.py:1172-1174 sums all VbtcV2TokenTransfer rows with no dedup. (2) Live mainnet query shows 6 transfer rows (ids 1,2,3,4,5,7 — id-6 gap present) where ids 1&4 are byte-identical duplicates for token 2 (tx hash dd9d567a..., RKuU->RNiQ 0.0001) and ids 2&5 for token 7 (tx hash 9fe25812..., RNiQ->RPKx 0.0001); the FK targets rbx_transaction(hash) so transaction_id is the chain hash itself. Only 4 type-26 transactions exist on chain vs 6 rows. (3) Recomputing the addresses property from live data (token 2: owner RKuU, global_balance 0.0008, completed withdrawal 0.0007 by RKuU) reproduces the claimed wrong output exactly: RNiQ 0.0002 (actual 0.0001), RKuU 0.0006 (actual 0.0007); token 7's duplicate adds +0.0001 to owner RPKx. (4) pg_constraint confirms only PK(id) + 2 FKs — no unique on (token_id, transaction_id), so the get_or_create added in commit 727978b (2026-05-29, after both duplicates were created) has no DB backstop and is racy without it. (5) The addresses property is exposed via api/btc/serializers.py (fields lines 23 and 93), so wrong balances are served by the live wallet API. No guard elsewhere dedups; nothing refutes the claim. Severity high is appropriate: live wrong financial balances on production, though the absolute amounts are tiny (0.0001 vBTC per duplicate, 2 tokens affected).

**Verification evidence:**

Live mainnet SQL: SELECT id,token_id,transaction_id,from_address,to_address,amount FROM rbx_vbtcv2tokentransfer ORDER BY id → rows: (1, 2, dd9d567a243e10dbf34779508febd171301d272154e8fe835dd6346dc989ec72, RKuU...→RNiQ..., 0.0001) and (4, 2, dd9d567a243e...same hash, same from/to/amount); (2, 7, 9fe258120a42a9bdbe03772eaa72049ac7a211263e51942df4e12e40697e3a52, ...0.0001) and (5, 7, 9fe25812...same); id 6 missing. SELECT COUNT(*) FROM rbx_transaction WHERE type=26 → 4 (vs 6 transfer rows). pg_constraint on rbx_vbtcv2tokentransfer → only rbx_vbtcv2tokentransfer_pkey PRIMARY KEY (id) plus FKs to rbx_transaction(hash) and rbx_vbtcv2token(id); no unique constraint. Code: rbx/models.py:1172-1174 `for t in transfers: entries[t.to_address] = entries.get(t.to_address, Decimal(0)) + t.amount; entries[t.from_address] = ... - t.amount` (no dedup); recomputation with token 2 live values (owner RKuU, global_balance 0.0008, completed withdrawal 0.0007) yields RNiQ=0.0002 / RKuU=0.0006 vs correct 0.0001 / 0.0007.


### No gap detection or automatic backfill; sync cursor is Max(height)+1 so any skipped block is unrecoverable without manual intervention

**Severity:** high · **Category:** correctness · **Location:** `rbx/management/commands/sync_blocks.py:32` · **Found by:** block-sync

start_height = local_max_height + 1 where local_max_height = Block.objects.aggregate(Max(height)) (rbx/utils.py:27-28). There is no scheduled job that scans for holes. The only repair tool is the manual `validate_blocks --sync_missing` management command (rbx/management/commands/validate_blocks.py), which itself is O(n) Block.objects.get calls per height and requires someone to notice the gap. The gap at 6638787 (created within the last ~2 weeks of chain history) is still unrepaired on production mainnet, proving nothing automated exists. Concrete failure: any of the silent-skip scenarios in the previous finding, or an operator killing the worker mid-run after a partial range, leaves holes that persist forever; explorer APIs (balances, tx history, circulation) silently omit that data.

**Reviewer evidence:**

rbx/utils.py:27-32 (get_local_max_height / get_remote_max_height); validate_blocks.py is manual-only; project/celery.py:22 only schedules sync_the_blocks every 10s with no validation pass. Mainnet still shows the 6638787 hole.

**Verification (high confidence):** Every element of the finding checks out against code and live production data. (1) The sync cursor is exactly as claimed: rbx/management/commands/sync_blocks.py:32 `start_height = 0 if sync_all or not local_max_height else local_max_height + 1`, with local_max_height from rbx/utils.py:27-28 `Block.objects.aggregate(value=Max("height"))["value"]`. Once any block past a hole is inserted, the cursor never revisits the hole. (2) No automated gap detection exists: project/celery.py:22 schedules only `sync_the_blocks` every 10s; the other periodic tasks (CMC prices, master nodes, vBTC balances, health_check, shop crawlers) do nothing with gaps. health_check.py (recently improved in e21b3e2) only compares the REMOTE chain tip's timestamp to now and sends SMS — it never inspects local block continuity. validate_blocks.py is manual-only (requires --height arg, loops `Block.objects.get(height=i)` per height) and is referenced nowhere except itself. Grep for validate_blocks/backfill/gap across the repo finds no scheduled caller. (3) The silent-skip mechanism is concrete: rbx/tasks.py:147-152 `data = get_block(height); if not data: return` — a transient empty/failed CLI response drops the block while the loop continues inserting later heights, advancing Max(height) past the hole. (4) Live mainnet proves it: block 6638787 is missing (6638786 and 6638788 both present), it is the only gap above height 6,000,000, current max height is 6,644,948 with 6,161 blocks synced past the hole, and it remains unrepaired ~4 days later (block timestamp 1781034982 ≈ 2026-06-06). I fetched the block from the headless CLI: it has NumOfTx=2 — a coinbase and a TransactionType 23 tx (hash d24fcc...) — and a DB query confirms neither hash exists in rbx_transaction. So the explorer is verifiably and silently missing real transaction data with no automated recovery path. Severity: I keep high rather than critical because observed frequency is low (1 gap in the last ~645k blocks) and this particular block's txs are 0-amount/0-fee, so no balance corruption occurred this time. But the same silent-skip applied to a transfer or vBTC v2 TKNZ_TX would permanently corrupt balances/tx history with zero detection, and vBTC v2 is production-active — so the missing safety net is a genuine high-severity correctness gap, not theoretical.

**Verification evidence:**

Code: rbx/management/commands/sync_blocks.py:32 `start_height = 0 if sync_all or not local_max_height else local_max_height + 1`; rbx/tasks.py:150-152 `data = get_block(height); if not data: return` (silent skip); project/celery.py:22 schedules only sync_the_blocks every 10s, no validate_blocks anywhere. Live mainnet SQL (gap scan with LEAD over rbx_block WHERE height > 6000000): exactly one row {gap_start: 6638787, gap_end: 6638787, missing_count: 1}; SELECT MAX(height), COUNT(*) WHERE height > 6638787 → max_height=6644948, cnt_above=6161 (cursor moved on, never repaired). CLI fetch of the missing block (GET /api/V1/SendBlock/6638787) shows NumOfTx=2 including tx d24fccedcec39d8a81c5056f3c03e7f42b511bc8259679429d6e36dfbbcdfdf2 (TransactionType 23); DB query `SELECT hash FROM rbx_transaction WHERE hash IN (...)` → 0 rows, confirming the explorer silently omits this block's transactions.


### Block processing is non-atomic and non-idempotent: a mid-block failure leaves the block partially indexed with no recovery path

**Severity:** high · **Category:** correctness · **Location:** `rbx/tasks.py:191` · **Found by:** block-sync

The Block row is committed (get_or_create, line 165) before the transaction loop runs, and nothing wraps the loop in a DB transaction — `atomic` is imported at line 13 (atomic_transaction) but never used anywhere in the file. If any tx in the loop raises (KeyError on an unexpected payload shape in process_transaction — most handlers do unguarded parsed["Function"]/parsed["ContractUID"] lookups; json.loads failure; IntegrityError if a tx hash re-appears after a reorg), the command aborts with the block partially indexed: some transactions and balance updates exist, the rest never will, because the next run starts at Max(height)+1. Worse, re-processing is impossible: Transaction.hash is the primary key (rbx/models.py:188) and the loop uses Transaction.objects.create (line 201), so running sync_block again on the same height (e.g. validate_blocks --sync_missing, or the task's autoretry_for=[RBXException]) raises IntegrityError on the first already-existing tx — manual repair of a partial block fails too. Additional non-idempotent side effects on any re-run: MasterNode.block_count is incremented unconditionally at lines 158-161 even when the block already exists, and sync_block(0) wipes the entire Address table (line 154-155), so a stray backfill of height 0 destroys all balances.

**Reviewer evidence:**

rbx/tasks.py:13 imports atomic as atomic_transaction, zero usages (grep). Lines 165-241: Block committed first, Transaction.objects.create (PK=hash), balance saves interleaved per-tx. Lines 154-155: `if height == 0: Address.objects.all().delete()`. Lines 158-161: block_count += 1 before get_or_create.

> ⚠️ _Adversarial verification was interrupted by a tooling failure during the final live-data check; the verifier had already confirmed the code-level claim. Last verifier note: "The cited code matches the claim so far. Let me check the callers, the Transaction model, and process_transaction."_


### VBTC_V2_MINT indexing silently drops the token when the node's smart-contract state isn't available yet — 2 of 13 mainnet mints have no VbtcV2Token row

**Severity:** high · **Category:** correctness · **Location:** `rbx/tasks.py:336` · **Found by:** block-sync

process_transaction for mint types calls get_nft(identifier) against the live node at index time. If the node returns no data (`if not data: logging.error("No SC data found."); return`), the tx is marked processed and never revisited: no Nft row, no VbtcV2Token row. This is a race — the block can be indexed seconds after crafting (sync runs every 10s) while the node's SC state isn't queryable yet, or while the CLI is cold-starting (its first request after restart fails and the validator registry takes ~30s — exactly the window where get_nft's own retry loop, 5 tries x 5s, can exhaust). Every downstream tx for that contract is then permanently dropped too: VBTC_V2_TRANSFER (line 909-911), withdrawal request (938-940), withdrawal complete (973-975), and TKNZ_TX Transfer() ownership change (857-859) all do `VbtcV2Token.DoesNotExist → log → return`. Confirmed on mainnet: 13 type-25 mint txs but only 11 VbtcV2Token rows; mint txs 3060143c0495... (height 6544281) and 67d6c68eed6a... (height 6548606) match no token's sc_identifier. The manual reprocess_vbtc_v2 command exists but nothing detects the condition.

**Reviewer evidence:**

Mainnet: SELECT count(*) FROM rbx_transaction WHERE type=25 → 13; SELECT count(*) FROM rbx_vbtcv2token → 11. LEFT JOIN of mint tx data text against token sc_identifiers shows NULL for hashes 3060143c... and 67d6c68e... Code: rbx/tasks.py:334-339, client.py get_nft retries 5x then returns None.

> ⚠️ _Adversarial verification was interrupted by a tooling failure during the final live-data check; the verifier had already confirmed the code-level claim. Last verifier note: "TKNZ_TX is 18. Let me re-check downstream txs with the correct types."_


### No handler for VBTC_V2_WITHDRAWAL_CANCEL (type 29) and no expiry: tokens stuck with is_pending_withdrawal=True — 2 stuck on mainnet for 12 days

**Severity:** high · **Category:** correctness · **Location:** `rbx/tasks.py:958` · **Found by:** block-sync

process_transaction's elif chain handles types 25-28 but nothing for Transaction.Type.VBTC_V2_WITHDRAWAL_CANCEL (29, defined in rbx/models.py). The API layer (api/btc/views.py:647-662) only proxies the cancel TX to the CLI and updates nothing locally. So when a withdrawal is cancelled on-chain (or simply never completes), VbtcV2WithdrawalRequest stays status=REQUESTED and token.is_pending_withdrawal stays True forever — there is no code path that ever resets it except a completion tx. is_pending_withdrawal is exposed via the API serializer (api/btc/serializers.py:92), so wallet frontends show these tokens as locked in a withdrawal indefinitely, blocking users from initiating new withdrawals. Confirmed live: mainnet has 8 withdrawal requests, 6 completed, and 2 stuck in 'requested' since 2026-05-29 (12 days, amounts 0.0003 and 0.0002 BTC, tokens d11a9ef3...:1779979897 and 6d893dce...:1780080357), with exactly 2 tokens flagged is_pending_withdrawal=true.

**Reviewer evidence:**

Mainnet: SELECT status, count(*) FROM rbx_vbtcv2withdrawalrequest GROUP BY status → requested:2, completed:6; both requested rows created_at 2026-05-29; SELECT count(*) FROM rbx_vbtcv2token WHERE is_pending_withdrawal → 2. Code: tasks.py elif chain ends at type 28 (lines 958-996); grep is_pending_withdrawal shows only set-True (955) and set-False-on-complete (995).

> ⚠️ _Adversarial verification was interrupted by a tooling failure during the final live-data check; the verifier had already confirmed the code-level claim. Last verifier note: "Code confirms the claim. Now let me verify live mainnet data."_


### Block sync pipeline can freeze indefinitely: no time limits on sync_the_blocks and untimed HTTP calls in the block-processing path

**Severity:** high · **Category:** operational · **Location:** `project/celery.py:49` · **Found by:** celery-ops

sync_the_blocks (project/celery.py:49) has no soft_time_limit/time_limit (only health_check got limits in e21b3e2). It runs on blocks-worker with concurrency=1 and synchronously executes sync_block -> process_transaction, which makes HTTP calls with NO timeout: get_nft (rbx/client.py:477, called from process_transaction at rbx/tasks.py:334 and 502 for every NFT/TKNZ/VBTC_V2 mint), get_master_nodes (rbx/client.py:58), get_smart_contract (rbx/client.py:145), and notify_socket_service's requests.post (rbx/tasks.py:1452, fired for EVERY new block). A single hung TCP connection (CLI wallet or socket service accepts but never responds) blocks the only worker slot forever: block sync stops, the explorer DB goes permanently stale, and beat keeps enqueueing sync_the_blocks every 10 seconds (project/celery.py:22) with no `expires` option, so blocks_queue grows unboundedly in the broker. get_nft is worse: it catches the hang's eventual exception, sleeps 5s and recurses 5x, multiplying stall time. e21b3e2 added timeout=15 only to get_info/get_block; the rest of rbx/client.py (40+ requests calls) is untimed. Honorable mention: the other celery.py wrapper tasks (update_cmc_prices, sync_master_nodes, shop crawlers, update_vbtc_balances) also have no time limits and can similarly wedge default-worker/vbtc-worker slots.

**Reviewer evidence:**

project/celery.py:49 `@app.task(queue="blocks_queue")` with no limits; rbx/client.py:477 `response = requests.get(url)` inside get_nft retry loop; rbx/tasks.py:1452 `requests.post(f"{settings.SOCKET_BASE_URL}/event/", ...)` with no timeout; porter.yaml:12 blocks-worker `--concurrency=1`; git show e21b3e2 confirms only get_info/get_block were given timeouts.

> ⚠️ _Adversarial verification was interrupted by a tooling failure during the final live-data check; the verifier had already confirmed the code-level claim. Last verifier note: "watch_worker is dev-only (DEBUG guard). Now confirming the process_transaction -> get_nft call sites and checking live mainnet sync freshness."_


### sync_block is neither atomic nor idempotent: a crash mid-block permanently loses transactions and corrupts incremental balances

**Severity:** high · **Category:** correctness · **Location:** `rbx/tasks.py:147` · **Found by:** celery-ops

sync_block creates the Block row (get_or_create, rbx/tasks.py:164), then loops creating Transaction rows and incrementally mutating Address.balance (lines ~200-245) with no transaction.atomic wrapper. If the worker dies mid-loop (deploy SIGTERM — Celery acks early since acks_late is not set — OOM, or an exception from process_transaction's network calls), the block row exists but only some transactions/balance deltas were applied. The next beat run computes start_height = local_max_height + 1 (rbx/management/commands/sync_blocks.py:32), so the partially-synced block is skipped forever: its remaining transactions are never indexed and balances are permanently wrong. Re-running the same height manually doesn't help either: Transaction.hash is the primary key (rbx/models.py:188) so Transaction.objects.create raises IntegrityError on the already-inserted rows, aborting before the missing ones. There is no automatic repair; the repo's history of manual fix commands (resync_balances, fix_adnr_balances, sale_complete_balance_fix, oct_2025_reprocess) shows this class of drift recurs.

**Reviewer evidence:**

Live mainnet: SELECT count(*) FROM rbx_address WHERE balance < 0 returns 899 addresses; ordinary user addresses like RBC6PazBtj1W1Z2PrXDmf9bGV2RcT7grcg sit at -128,032 RBX (genesis/coinbase pseudo-addresses are legitimately negative, but plain R-addresses at large negative values indicate applied-debit-without-credit drift consistent with partial block processing).

> ⚠️ _Adversarial verification was interrupted by a tooling failure during the final live-data check; the verifier had already confirmed the code-level claim. Last verifier note: "n/a"_


### No handler for VBTC_V2_WITHDRAWAL_CANCEL — cancelled withdrawals will stay 'requested' and lock tokens forever

**Severity:** high · **Category:** correctness · **Location:** `rbx/tasks.py:958` · **Found by:** vbtc-data-invariants,vbtc-token-model

Transaction.Type defines VBTC_V2_WITHDRAWAL_CANCEL = 29 (rbx/models.py:177) and the web API exposes a full cancel flow (api/btc/views.py:635-662, VbtcV2WithdrawCancelPrepareView/SendView), but process_transaction's elif chain handles only types 18/26/27/28 and ends at line 996 with no branch for type 29 (or 30). The first time a user cancels a withdrawal via the live cancel endpoints, the cancel tx will be indexed but ignored: the VbtcV2WithdrawalRequest stays status='requested' and token.is_pending_withdrawal stays True permanently, blocking the token in the wallet UI and making VbtcV2Token.addresses inconsistent with chain state. Related latent bug folded in: the WITHDRAWAL_COMPLETE handler unconditionally sets is_pending_withdrawal=False (tasks.py:995) even when OTHER withdrawal requests for the same token are still outstanding, so completing withdrawal A hides pending withdrawal B (has not fired yet on mainnet only because no token has had overlapping requests).

**Reviewer evidence:**

grep: VBTC_V2_WITHDRAWAL_CANCEL appears only in rbx/models.py (enum + type_label), never in rbx/tasks.py. Mainnet: SELECT type, count(*) FROM rbx_transaction WHERE type IN (29,30) GROUP BY type → 0 rows so far, meaning the gap has not yet fired but the cancel API is deployed and reachable.
grep of rbx/tasks.py shows no WITHDRAWAL_CANCEL / VBTCWithdrawalCancel handling; only types 26/27/28 branches at lines 892/924/958. Mainnet: SELECT type, COUNT(*) FROM rbx_transaction WHERE type IN (25,26,27,28,29,30) GROUP BY type → types 25:13, 26:4, 27:8, 28:6; no rows for 29. Cancel proxy endpoints exist at api/btc/views.py:635-662.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### Top-holders endpoint runs an uncached ~61M-cost aggregate over 7.4M transactions on every request

**Severity:** high · **Category:** operational · **Location:** `api/address/views.py:34` · **Found by:** web-api

AddressTopHoldersListView.get() builds a correlated-subquery aggregate: a parallel seq scan over the entire rbx_transaction table grouped by to_address, plus a per-group correlated subquery summing sent amounts. It has NO @cache_request decorator (unlike block/transaction/adnr list views) and no auth gate. Each call is a full recompute. A handful of concurrent hits to GET /api/addresses/top-holders/ can saturate DB CPU and stall block-sync/wallet queries. Failure scenario: a scraper or dashboard polling this endpoint degrades the whole explorer + API.

**Reviewer evidence:**

Live mainnet pg_class reltuples: rbx_transaction=7,375,062 rows. EXPLAIN of the equivalent query on mainnet: top-level Limit cost 246608..1156326, Result node cost up to 61,716,289; Parallel Seq Scan on rbx_transaction (rows=3,077,068) + SubPlan 1 Aggregate (cost 9097 each) executed per group (~6757 groups). No cache decorator present on the view (contrast TransactionListView/BlockListView which use cache_request).

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### API auth disabled globally makes every endpoint AllowAny, including mutating proxy endpoints

**Severity:** high · **Category:** security · **Location:** `project/settings/api.py:14` · **Found by:** web-api

DEFAULT_PERMISSION_CLASSES resolves to AllowAny whenever API_AUTH_REQUIRED is False. The local .env sets API_AUTH_REQUIRED=False (and the explorer must serve public read APIs, so prod almost certainly runs the same). Consequence: the per-view permission_classes=[AllowAny] scattered around the codebase is redundant and there is effectively NO authenticated tier — every route is anonymous, including the mutating proxies under api/raw/ (send/, withdraw-vbtc/, beacon/*) and api/masternodes/send/. The only real auth in the layer is the custom address_permission used by exactly one endpoint (email-subscribe). This means hardening any single endpoint by 'requiring auth' silently does nothing.

**Reviewer evidence:**

.env line 66: API_AUTH_REQUIRED=False. project/settings/api.py: DEFAULT_PERMISSION_CLASSES = [IsAuthenticated if API_AUTH_REQUIRED else AllowAny]. raw/master_node views set no permission_classes, so they inherit the global AllowAny.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### Unauthenticated masternodes/send writes arbitrary rows, forwards attacker payload to an external URL, and leaks exceptions

**Severity:** high · **Category:** security · **Location:** `api/master_node/views.py:96` · **Found by:** web-api

SendMasterNodesView.post is unauthenticated (no signature, no token — unlike raw/send which at least requires CLI-validated signatures) and creates/updates SentMasterNode rows directly from request.data keys (Address, UniqueName, IpAddress, WalletVersion, ...). An anonymous caller can inject/overwrite arbitrary masternode records (18,826 live rows) and, because RBX_FORWARD_SEND_MASTER_NODES is configured, the view re-POSTs the unvalidated attacker payload to that external endpoint — a server-side request amplification/forwarding primitive. On any error it returns the raw exception string (f"{e}") to the client, leaking internal field names/stack context.

**Reviewer evidence:**

api/master_node/views.py: post() loops request.data writing SentMasterNode fields; lines 142-150 forward self.request.data to settings.RBX_FORWARD_SEND_MASTER_NODES; line 152-154 `except Exception as e: print(e); return Response({"success": False, "message": f"{e}"}, status=400)`. Live rbx_sentmasternode=18,826 rows.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### Lost-update race on VbtcV2Token rows: three different workers write the same rows with full-instance save()

**Severity:** high · **Category:** correctness · **Location:** `btc/management/commands/update_vbtc_balances.py:18` · **Found by:** celery-ops

update_vbtc_balances (vbtc-worker, every 10 min) materializes ALL VbtcV2Token instances up front (`tokens = VbtcV2Token.objects.all()` evaluated by len() at line 19), then iterates with sleep(0.5) per token and calls token.save() with no update_fields (line 27) — writing back EVERY field from an in-memory snapshot that is up to ~30s stale (and staler as token count grows). Meanwhile blocks-worker concurrently writes the same rows during TKNZ_TX/withdrawal processing: owner_address on V2 ownership transfer (rbx/tasks.py:846-848, commit 3d83402), is_pending_withdrawal=True on withdrawal request (~rbx/tasks.py:953) and =False on completion; and default-worker writes image_base64_url in handle_vbtc_v2_icon_upload (rbx/tasks.py:1419-1446), also via full save(). Concrete failure: an ownership transfer or withdrawal-request lands on-chain while the balance refresher is mid-loop; the refresher's save() reverts owner_address to the previous owner or clears/sets is_pending_withdrawal incorrectly. The API serializes these fields to wallets (api/btc/serializers.py:92), so a reverted owner or stale pending flag directly breaks wallet behavior. Fix direction: save(update_fields=[balance fields]) everywhere and refetch per token.

**Reviewer evidence:**

btc/management/commands/update_vbtc_balances.py:18-27 (snapshot + bare save()); rbx/tasks.py:846-848 `v2_token.owner_address = tx.to_address; v2_token.save()`; mainnet has 11 live V2 tokens and ownership transfer shipped this week, so the 10-minute refresher overlaps real transfer activity.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### Health check false-passes: it monitors the CLI wallet's chain height, not the explorer's own sync pipeline or workers

**Severity:** high · **Category:** operational · **Location:** `rbx/management/commands/health_check.py:27` · **Found by:** celery-ops

health_check calls get_info()/get_block() against the RBX CLI wallet and alerts only when the network's latest block timestamp is >250s old. It never compares against the explorer DB (max(rbx_block.height) / date_crafted lag), and never checks worker liveness. Concrete false-pass: blocks-worker crashes or wedges (see the untimed-HTTP finding) while the chain keeps producing blocks — health_check reports 'All is well' every 3 minutes indefinitely while the explorer API serves increasingly stale data and blocks_queue backlogs. There is NO monitoring at all for vbtc-worker (vBTC balances silently freeze) or default-worker. A DB-lag check (now() - max(date_crafted) on the explorer's own table) would catch both network stalls AND pipeline stalls with one query.

**Reviewer evidence:**

health_check.py:28-41 only uses rbx.client get_info/get_block (CLI wallet HTTP API); no import of Block model lag, no Celery inspect/ping. Mainnet currently healthy (max date_crafted lag was 10s at check time), so no live incident — but the blind spot is structural.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### Block sync silently skips heights, leaving permanent gaps (5 confirmed on mainnet, one recent)

**Severity:** high · **Category:** correctness · **Location:** `rbx/tasks.py:150` · **Found by:** architecture

sync_block() returns silently when get_block(height) yields no data (lines 150-152), and the resume logic in rbx/management/commands/sync_blocks.py:32 always restarts from get_local_max_height()+1. If height H is skipped but H+1 succeeds, local max advances past H and H is never fetched again — no gap detection, no alert, no backfill task exists. Additionally sync_block is not wrapped in a DB transaction: Block row is created first, then Transaction rows one-by-one with balance side effects; a crash mid-block commits the Block (advancing local max) while the remaining transactions of that block are never indexed, since Transaction.hash is the PK and re-running raises IntegrityError on the already-inserted rows. Net effect: explorer (which backs wallet APIs) silently under-reports transactions and misstates balances. The gap at 6638787 is from within the last days, proving this is an active failure mode, not legacy.

**Reviewer evidence:**

Mainnet: SELECT max(height), count(*) FROM rbx_block → max_h=6644878, cnt=6644874 (5 missing). Gap heights via LAG window: 1441747, 1561322, 1617991, 1781020, 6638787. Code: rbx/tasks.py:150-152 `if not data: return`; no atomic_transaction use in sync_block despite the import at line 13; sync_blocks.py:32 `start_height = ... local_max_height + 1`.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### Two divergent balance systems; incremental Address.balance has drifted — 899 mainnet addresses negative

**Severity:** high · **Category:** correctness · **Location:** `rbx/models.py:483` · **Found by:** architecture

Balances are computed two independent ways: (1) an incremental running column Address.balance mutated inline during block sync (rbx/tasks.py:218-241) with read-modify-write and special cases (ADNR burn via float-constructed Decimal(5.0) at tasks.py:225), and (2) a 150-line full recompute Address.get_balance() (models.py:483-629) with different rules (excludes NFT_SALE, handles callbacks, recoveries, vault locks). The API serves get_balance() (api/address/views.py:101,192) while the column is what sync maintains and what resync_balances rebuilds — the two are never reconciled and encode different domain rules (get_balance subtracts ADNR fees from the RECEIVER per tasks.py:223 but counts per to_address in models.py:553-559). Any new transaction type with non-standard balance semantics (shield/bridge types 31-38 already live) must be implemented twice or the systems diverge further. The 899 negative balances (3.8% of 23,856 addresses) prove the incremental model is already wrong at scale; commit 0732472 papering over negatives in VbtcV2Token.addresses shows the same pattern repeating in vBTC v2.

**Reviewer evidence:**

Mainnet: SELECT count(*) FILTER (WHERE balance < 0), count(*) FROM rbx_address → 899 negative of 23856. Code: rbx/tasks.py:218-241 (incremental), rbx/models.py:483-629 (recompute), api/address/views.py:101 calls get_balance().

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### Blocks worker (concurrency=1) can stall indefinitely on no-timeout HTTP calls inside block sync

**Severity:** high · **Category:** operational · **Location:** `rbx/tasks.py:1448` · **Found by:** architecture

Block ingestion runs on the single-concurrency blocks-worker via a 10s beat task with no time limit (project/celery.py:22,49-53). Inside that path, notify_socket_service() does requests.post with NO timeout (rbx/tasks.py:1452-1458) for every new block, and process_transaction calls get_nft() (rbx/client.py:477, no timeout) for every mint. 46 of the HTTP calls in rbx/client.py still lack timeouts — commit e21b3e2 (yesterday) only fixed get_info and get_block, confirming this class of bug is real and being fixed piecemeal. If the socket service or CLI accepts a connection but never responds, the blocks-worker hangs forever, block sync stops network-wide for the explorer, and beat piles up redundant sync_the_blocks tasks. Crucially, health_check (rbx/management/commands/health_check.py:24-43) only monitors the CLI's chain tip freshness, NOT explorer sync lag (local max vs remote max), so a stalled blocks-worker raises no alert while wallet APIs serve stale data. Honorable mention: alert phone numbers are hardcoded in source (health_check.py:14-21).

**Reviewer evidence:**

rbx/tasks.py:1452 `requests.post(f"{settings.SOCKET_BASE_URL}/event/", ...)` with no timeout, called from sync_block (line 261). `grep -c` shows 46 requests calls without timeout in rbx/client.py. git show e21b3e2 adds timeout=15 to only get_info/get_block. project/celery.py:49 sync_the_blocks has no time_limit while health_check got soft_time_limit=60 in the same commit.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._



## Medium

### FROST signing runs in a daemon thread inside the gunicorn web process; one-shot status read can permanently lose the signed BTC transaction

**Severity:** medium · **Category:** operational · **Location:** `api/btc/views.py:549` · **Found by:** vbtc-docs-conformance

VbtcV2WithdrawCompleteExecuteView spawns threading.Thread(daemon=True) in the web worker to run the 180s FROST CLI call, storing state in Redis with a 300s TTL (views.py:494-549). Three concrete failure modes: (1) Porter deploy/restart or worker recycling of the `web` service mid-job kills the daemon thread silently; the job stays 'pending' until the 300s TTL expires, after which polls get 404 'Job not found' — indistinguishable from a bad job_id, and the SDK doc tells clients to poll 'up to 3 minutes', exactly the window where this ambiguity bites. The validators may have completed the FROST round (CLI-side), so the user has a half-done withdrawal with no signed_btc_tx_hex and must restart the whole FROST round. (2) The status view DELETES the cache entry on the first successful read of 'complete' (views.py:576) and returns signed_btc_tx_hex once; if that single HTTP response is lost (mobile network blip, proxy timeout), the signed BTC transaction hex is gone forever — there is no redundant store and no idempotent re-read. (3) The repo runs a dedicated `vbtc-worker` Celery queue with concurrency=1 for exactly this kind of long CLI operation, but this code path bypasses it. Moving the job to Celery on the vbtc queue and making status reads non-destructive (delete on TTL only) removes all three.

**Reviewer evidence:**

views.py:523-549 (thread + cache), :576 and :586 (_cache.delete on first read), :495 FROST_JOB_TTL=300; rbx/client.py:1177 timeout=180 for execute_complete_withdrawal; project/settings/cache.py confirms Redis backing (so cross-worker polling works — restart loss and one-shot read are the real risks). CLAUDE.md documents the vbtc-worker service.

**Verification (high confidence):** Every element of the finding is confirmed by code. (1) The FROST signing job runs as a daemon thread inside the gunicorn web process (api/btc/views.py:549) calling execute_complete_withdrawal with a 180s HTTP timeout (rbx/client.py:1177); porter.yaml runs the web service as 'gunicorn --workers 3 project.wsgi' with sync workers, so a Porter deploy/restart/crash of the web service mid-job kills the thread silently, leaving the Redis entry at 'pending' until FROST_JOB_TTL=300 (views.py:495) expires into a 404 'Job not found' (views.py:564-567) indistinguishable from a bad job_id. (2) The status view deletes the cache entry on the first read of a 'complete' result (views.py:576) before returning signed_btc_tx_hex; a repo-wide grep shows SignedBTCTxHex/signed_btc_tx_hex exists ONLY at views.py:580 — no DB field, no redundant store, no idempotent re-read, so one lost HTTP response permanently loses the signed BTC transaction (user must restart the FROST round). (3) The dedicated vbtc-worker (vbtc_queue, concurrency=1) exists in porter.yaml, but the only task on that queue is update_vbtc_balances (project/celery.py:101); no Celery task wraps execute_complete_withdrawal — only the view calls it. The Redis cache backend (project/settings/cache.py, django_redis) is also confirmed, so cross-worker polling works and the reviewer correctly narrowed the risk to restart-loss and one-shot reads. Minor nuance: gunicorn has no --max-requests, so workers are not routinely recycled — failure mode (1) only triggers on deploys/restarts/crashes during the up-to-180s window. The impact is a stuck/half-done withdrawal requiring a FROST round restart, not fund loss, so medium operational severity is correct. The suggested fix (Celery task on vbtc_queue + non-destructive status reads, delete on TTL only) addresses all three modes.

**Verification evidence:**

api/btc/views.py:549 'threading.Thread(target=_run_frost, daemon=True).start()' inside VbtcV2WithdrawCompleteExecuteView.post (web process), with views.py:576 '_cache.delete(f"{FROST_JOB_PREFIX}{job_id}")' executed before returning signed_btc_tx_hex at :580 — and grep for SignedBTCTxHex across the entire repo returns exactly one hit (views.py:580), proving there is no secondary store of the signed transaction. porter.yaml: web = 'gunicorn --workers 3 project.wsgi' (no max_requests); vbtc-worker = 'celery ... --queues=vbtc_queue --concurrency=1' but project/celery.py:101 shows the only vbtc_queue task is update_vbtc_balances; grep 'execute_complete_withdrawal' hits only api/btc/views.py:28,528 and its definition rbx/client.py:1172 (timeout=180 at :1177).


### Two withdrawals stuck 'requested' for 12 days; tokens 7 and 8 locked is_pending_withdrawal=true with no reconciliation path

**Severity:** medium · **Category:** operational · **Location:** `rbx/tasks.py:955` · **Found by:** vbtc-data-invariants

Mainnet withdrawal id 4 (token 7, requestor RNiQ..., 0.0003 BTC, request tx 95a68f88be59ff38adfd88061ecbceee8392ef7639f620d47852046c8c26fbaa) and id 8 (token 8, requestor RPKx..., 0.0002 BTC, request tx 98d16460ac60e14f9c7706f8b5799aa830b8a4e4b6f313509be0eeed96a7823b) have been status='requested' since 2026-05-29 — 12 days as of 2026-06-10 — and both tokens carry is_pending_withdrawal=true. No completion (type 28) or cancel (type 29) tx exists for either, and the explorer has no expiry/timeout/reconciliation job, so these tokens stay locked in the wallet UI indefinitely. Worse, token 7's ownership was transferred to RPKx on 2026-06-02 WHILE this withdrawal by the previous owner was pending — nothing in the explorer (or apparently the flow) blocks ownership transfer with an open withdrawal, leaving a pending withdrawal whose requestor no longer owns the token. Note: Sentry could not be checked for corroborating error signatures in this session (the sentry MCP server requires an interactive OAuth flow a subagent cannot complete); recommend a manual scan of project python-django for 'VbtcV2' signatures over the last 60 days.

**Reviewer evidence:**

SQL: SELECT id, token_id, requestor_address, amount::text, status, created_at FROM rbx_vbtcv2withdrawalrequest WHERE status='requested' → ids 4 and 8, both created 2026-05-29; rbx_vbtcv2token ids 7 and 8 show is_pending_withdrawal=true; SELECT count(*) FROM rbx_transaction WHERE type IN (28,29) AND date_crafted > '2026-05-29 20:30' → no matching completion/cancel for these requests.

**Verification (high confidence):** Every factual claim verified against live mainnet data and code. Withdrawal requests 4 and 8 have been status='requested' since 2026-05-29 (~12 days) with tokens 7 and 8 locked is_pending_withdrawal=true. No type-28 completion exists after 2026-05-29 20:17:52 and zero type-29 cancel txs have ever occurred on mainnet. The explorer's only state transitions are rbx/tasks.py:955 (set true on request) and :995 (set false on completion); there is no handler for VBTC_V2_WITHDRAWAL_CANCEL at all and no periodic reconciliation (celery beat only runs update_vbtc_balances, which never touches the flag) — so the finding slightly understates the gap: even an on-chain cancel would not unstick these tokens in the explorer. The ownership-transfer-during-open-withdrawal claim is also confirmed: TKNZ_TX 422115482c... on 2026-06-02 15:17:29 moved token 7 from RNiQ to RPKx via the Transfer() handler (tasks.py:844-848), which has no pending-withdrawal guard, leaving an open withdrawal whose requestor no longer owns the token. Impact is limited to stale locked state for 2 tokens with dust amounts (0.0003/0.0002 BTC) on what appear to be team test wallets, so medium severity stands; the fix needs both a type-29 cancel handler and a reconciliation/expiry path, plus a decision on whether ownership transfer should be blocked or should reassign/void open withdrawals.

**Verification evidence:**

Live mainnet SQL: SELECT w.id, w.token_id, w.requestor_address, w.amount::text, w.status, w.created_at, now()-w.created_at AS age, t.is_pending_withdrawal FROM rbx_vbtcv2withdrawalrequest w JOIN rbx_vbtcv2token t ON t.id=w.token_id WHERE w.status='requested' → id 4 (token 7, RNiQrW3a..., 0.0003, created 2026-05-29T18:15:36Z, age 12 days, is_pending_withdrawal=true) and id 8 (token 8, RPKxShZh..., 0.0002, created 2026-05-29T20:27:49Z, age ~12 days, is_pending_withdrawal=true). Zero type-29 txs exist (SELECT ... WHERE type IN (28,29,30) → only six type-28 rows, latest 2026-05-29T20:17:52Z). Code: rbx/tasks.py:955 'token.is_pending_withdrawal = True' and rbx/tasks.py:995 'token.is_pending_withdrawal = False' are the only writes; grep for VBTC_V2_WITHDRAWAL_CANCEL in rbx/tasks.py returns nothing. Ownership transfer during open withdrawal: mainnet tx 422115482c56955faa9127109bdf2947e0a0b9bdb1c0865554d7e88dde97e91c, type 18, 2026-06-02T15:17:29Z, Function Transfer(), ContractUID d11a9ef3f98d4723849d5463596a6c21:1779979897 (token 7), RNiQ → RPKx; handler rbx/tasks.py:846-848 sets v2_token.owner_address = tx.to_address with no pending-withdrawal check.


### addresses property's '> 0' filter masks negative-balance ledger corruption and inflates displayed totals

**Severity:** medium · **Category:** correctness · **Location:** `rbx/models.py:1202` · **Found by:** vbtc-data-invariants

Commit 0732472 made VbtcV2Token.addresses return only entries with bal > 0. A negative computed balance is always evidence of ledger corruption (duplicate rows, ownership-transfer accounting, missed transactions) — the invariant is that all entries sum exactly to global_balance, so dropping negatives guarantees the displayed entries sum to MORE than the BTC actually backing the token whenever corruption exists, silently converting a detectable inconsistency into unbacked user-visible balances. Live instance: token 7 has RNiQ at −0.0004 (dropped), which is exactly why the wallet shows RPKx holding 0.00118859 against 0.00078859 of real BTC (see ownership-transfer finding). The filter also drops legitimately-zero addresses indistinguishably from corrupt-negative ones, so there is no signal anywhere (no log, no metric) when this fires. Honorable mention: the V1 VbtcToken.addresses (models.py:1094-1113) has no filter at all and would expose raw negative balances, an inconsistent behavior between V1 and V2 of the same API field.

**Reviewer evidence:**

SQL ledger replication for token 7 returned RNiQrW3aBUWZhfadqKxPuN46iGaR13ox7P = -0.0004000000000000, which the property silently drops; sum of positive entries (0.00118859) != global_balance (0.00078859).

**Verification (high confidence):** Confirmed at every level. The code at rbx/models.py:1202 filters entries to bal > 0, the property is exposed unguarded through VbtcV2TokenSerializer (api/btc/serializers.py:93), and there is no log/metric/sum-check anywhere when an entry is dropped (grep found no other consumers or validators). The live mainnet reproduction matches the reviewer's numbers exactly: replaying the property's algorithm for token 7 yields RPKx = 0.00118859 and RNiQ = -0.0004; the filter drops RNiQ, so displayed entries sum to 0.00118859 against global_balance (actual BTC backing) of 0.00078859. The negative arises from two real masked defects: (1) a genuine duplicate ledger row — VbtcV2TokenTransfer ids 2 and 5 both reference the same transaction hash 9fe25812...697e3a52 — and (2) the ownership-transfer accounting gap (on-chain Transfer() TKNZ_TX 42211548...e91c moved ownership RNiQ->RPKx on 2026-06-02; commit 3d83402 only updates owner_address, writing no ledger row, so global_balance is credited wholly to the new owner while the old owner's debits remain). Commit 0732472's own message admits the filter is a stopgap clamp. V1 VbtcToken.addresses (models.py:1094-1113) confirmed to have no filter, so V1/V2 behavior is inconsistent. Minor correction to the reviewer's framing: a negative is not always 'corruption' — the ownership-transfer accounting model was a known deliberate deferral — but the masking and inflated user-visible total are real and reproduce on production today. Severity remains medium: it inflates displayed/API balances (bad for a financial product UI) but does not itself move funds; withdrawals are validated by the CLI/network, so the worst direct outcome is a user attempting an over-backed withdrawal that fails.

**Verification evidence:**

rbx/models.py:1202: `return {addr: bal for addr, bal in entries.items() if bal > 0}`. Live mainnet, token 7 (global_balance 0.00078859, owner RPKxShZhfytjfffG8sQZG5ZTj9qqcAPgvJ): SELECT t.id, t.transaction_id, t.amount FROM rbx_vbtcv2tokentransfer t WHERE token_id=7 returned ids 2 and 5 BOTH with transaction hash 9fe258120a42a9bdbe03772eaa72049ac7a211263e51942df4e12e40697e3a52, amount 0.0001 each (duplicate row); completed withdrawal RNiQ 0.0002. Replaying the property: RPKx = 0.0002 + 0.00078859 + 0.0002 = 0.00118859 displayed; RNiQ = -0.0004 silently dropped; displayed sum exceeds BTC backing by 0.0004. Ownership transfer confirmed: type-18 tx 422115482c56955faa9127109bdf2947e0a0b9bdb1c0865554d7e88dde97e91c from RNiQ to RPKx (2026-06-02). Property exposed via api/btc/serializers.py:93 ("addresses" in VbtcV2TokenSerializer.fields) with no validation.


### VbtcV2DetailView mutates token balance rows on every uncached public GET via blockchain.info

**Severity:** medium · **Category:** operational · **Location:** `api/btc/views.py:226` · **Found by:** vbtc-data-invariants

VbtcV2DetailView.get() performs a synchronous third-party HTTP call (BtcClient.get_balance → blockchain.info with a 5/10s timeout) and then token.save() on EVERY request, with no cache decorator (unlike BtcAddressView which has @cache_request) and no rate limiting. Failure scenarios: (a) any anonymous client polling the detail endpoint drives one blockchain.info request per hit — blockchain.info rate-limits aggressively, which then starves the legitimate 10-minute celery update_vbtc_balances task using the same unauthenticated API and User-Agent; (b) the save() writes all token fields and races with the celery task and with TKNZ ownership-transfer processing (last-writer-wins, no select_for_update), so a slow GET can clobber a concurrent owner_address/is_pending_withdrawal update with stale in-memory values; (c) write load on a read path. Honorable mention folded in: BtcClient.satoshi_to_btc_multiplier = 0.00000001 is a binary float and conversions go int*float→Decimal (btc/btc_client.py:53-67); at current sub-0.01 BTC magnitudes the 16-dp DecimalField quantization happens to round clean (verified: all mainnet global_balance values have clean tails), but the construction is precision-unsound for larger balances — should be Decimal(sats) / Decimal(100000000).

**Reviewer evidence:**

api/btc/views.py:226-236 (no @method_decorator(cache_request...) on the class, contrast line 51); btc/btc_client.py:16 'satoshi_to_btc_multiplier = 0.00000001' (float); mainnet check of stored values: all 11 rbx_vbtcv2token.global_balance values currently quantize clean at 16dp.

**Verification (high confidence):** The finding is confirmed with one correction. (a) Confirmed: VbtcV2DetailView.get() (api/btc/views.py:226-236) performs a synchronous blockchain.info HTTP call (btc/btc_client.py:40-44, timeout=(5,10)) and a full-row token.save() on every GET, with no cache decorator (contrast @method_decorator(cache_request(...)) on BtcAddressView at line 51). The endpoint is publicly reachable anonymously in production: unauthenticated curl to https://data.verifiedx.io/api/btc/vbtc-v2/ returned 200, and api/btc/views.py has zero permission_classes overrides, so API_AUTH_REQUIRED=False in prod. CORRECTION to the claim: 'no rate limiting' is overstated — DRF defaults apply api.throttling.AnonRateThrottle at 300/min per IP (project/settings/api.py:27-33, API_THROTTLE_ENABLED default True). However 300/min/IP (~5 req/s) still far exceeds blockchain.info's unauthenticated tolerance, and the web pods likely share egress IP with the celery update_vbtc_balances task (project/celery.py:30, every 10 min, same BtcClient/User-Agent), so the starvation scenario stands. (b) Race confirmed structurally: rbx/tasks.py:846-848 sets owner_address via full save(), :955-956/:995-996 set is_pending_withdrawal via full save(); the view's save() has no update_fields and no select_for_update exists anywhere in the repo (grep returned nothing), so a GET in flight across a TKNZ transfer/withdrawal event clobbers those fields with stale values (last-writer-wins). Mitigant: get_balance returns None on any HTTP error, so the view degrades gracefully and the race requires a slow-but-successful GET — narrow window, low probability, but real. (c) Float precision confirmed latent: satoshi_to_btc_multiplier = 0.00000001 is a binary float (btc_client.py:16) and conversions go int*float→Decimal (:53-57); mainnet query shows all 11 rbx_vbtcv2token.global_balance values are currently exact satoshi multiples (0 dirty rows), so it is latent-only at current sub-0.01 BTC magnitudes but unsound for larger balances. Severity medium is correct: production-active anonymous write-on-read with third-party dependency, partially mitigated by the 300/min/IP throttle and graceful failure handling.

**Verification evidence:**

api/btc/views.py:226-236: "def get(self, request, *args, **kwargs): token = self.get_object(); client = BtcClient(); balance_info = client.get_balance(token.deposit_address); if balance_info: token.global_balance = ...; token.save(); return Response(...)" — no cache decorator (vs line 51 @method_decorator(cache_request(settings.CACHE_TIMEOUT_LONG), name="get") on BtcAddressView), no update_fields. Public access verified live: unauthenticated `curl https://data.verifiedx.io/api/btc/vbtc-v2/` → HTTP 200 with token JSON. Race counterpart: rbx/tasks.py:846-848 "v2_token.owner_address = tx.to_address; v2_token.save()"; grep for select_for_update across repo: 0 hits. Float: btc/btc_client.py:16 "satoshi_to_btc_multiplier = 0.00000001"; mainnet SQL: SELECT count(*) FILTER (WHERE (global_balance*100000000) <> round(global_balance*100000000)) FROM rbx_vbtcv2token → total=11, dirty=0 (latent only). Correction: DRF AnonRateThrottle 300/min per IP IS active by default (project/settings/api.py:27-33, api/throttling.py BypassThrottleMixin gated on API_THROTTLE_ENABLED default True), so "no rate limiting" is inaccurate, though insufficient to protect blockchain.info quota.


### Duplicate VbtcV2TokenTransfer rows live on mainnet inflate per-address balances served by the API

**Severity:** medium · **Category:** correctness · **Location:** `rbx/models.py:1205` · **Found by:** vbtc-tknz-processing

rbx_vbtcv2tokentransfer currently contains two duplicate pairs: ids 1 & 4 (same transaction dd9d567a..., token 4bb6f099...) and ids 2 & 5 (same transaction 9fe25812..., token d11a9ef3...). They were created before commit 727978b added get_or_create to the VBTC_V2_TRANSFER handler (i.e., a reprocess double-inserted them) and were never cleaned up. VbtcV2TokenTransfer has no unique constraint on (token, transaction), so get_or_create is also race-prone (concurrent reprocess vs block sync can still double-insert). Impact: VbtcV2Token.addresses (models.py:1164-1202) sums these rows, so RPKx shows +0.0002 instead of +0.0001 on token d11a9ef3 and RNiQ shows +0.0002 instead of +0.0001 on token 4bb6f099; `addresses` is exposed through VbtcV2TokenSerializer (api/btc/serializers.py:93) and consumed by the wallet/Butterfly coin-selection, so spendable vBTC is overstated against real BTC backing. The V1 paths are worse: TKNZ_TX TransferCoin()/TransferCoinMulti() (tasks.py:824-831, 879-887) still use plain creates with no idempotency at all, so any reprocess of type-18 txs duplicates VbtcTokenAmountTransfer rows.

**Reviewer evidence:**

Mainnet: SELECT id, token_id, transaction_id, amount FROM rbx_vbtcv2tokentransfer ORDER BY id → ids 1/4 share transaction dd9d567a243e10..., ids 2/5 share 9fe258120a42a9bd.... git log -S 'VbtcV2TokenTransfer.objects.get_or_create' → 727978b 'Use get_or_create ... to prevent duplicates' (fix was forward-only). models.py:1205-1211 shows no Meta unique_together.

**Verification (high confidence):** Every factual claim verified against live mainnet data and code. (1) The duplicate-pair query returns exactly the two claimed pairs; full rows show ids 1/4 and 2/5 are byte-identical duplicates created 2026-05-19 and 2026-05-28, predating the 2026-05-29 forward-only get_or_create fix (commit 727978b, confirmed via git show). (2) pg_constraint confirms rbx_vbtcv2tokentransfer has only a PK and two FKs — no unique constraint on (token, transaction) — so get_or_create remains race-prone in principle. (3) tasks.py V1 TransferCoin()/TransferCoinMulti() handlers still do plain VbtcTokenAmountTransfer(...).save() with no idempotency. (4) VbtcV2Token.addresses (models.py:1164-1202) sums all transfer rows and is exposed via VbtcV2TokenSerializer's "addresses" field, so RNiQ shows 0.0002 instead of 0.0001 on token 4bb6f099 and RPKx is overstated by 0.0001 on token d11a9ef3 — exactly as claimed. One nuance the reviewer overstated: for token 2 the sender is the owner, so the duplicate misallocates (owner understated by the same 0.0001) and the address total still equals global_balance — net inflation vs BTC backing only occurs for token 7, where the non-owner sender's resulting negative balance is filtered out at models.py:1202. Severity adjusted high → medium: the live damage is 0.0001 BTC misallocated per token across two addresses, the chain/CLI is the authoritative spend validator (worst case is a wallet crafting a transfer the chain rejects), and one token's total is fully conserved. Still a real production data-integrity bug needing dedupe of ids 4 and 5, a unique constraint, and V1 idempotency.

**Verification evidence:**

Mainnet: SELECT token_id, transaction_id, count(*) FROM rbx_vbtcv2tokentransfer GROUP BY 1,2 HAVING count(*)>1 → [{token_id: 2, transaction_id: "dd9d567a243e10dbf34779508febd171301d272154e8fe835dd6346dc989ec72", count: 2}, {token_id: 7, transaction_id: "9fe258120a42a9bdbe03772eaa72049ac7a211263e51942df4e12e40697e3a52", count: 2}]; ids 1/4 and 2/5 are identical rows (0.0001 each). pg_constraint on rbx_vbtcv2tokentransfer: only PRIMARY KEY (id) plus two FKs — no unique on (token_id, transaction_id). Model at /Users/tyler/prj/vfx/vfx-explorer/rbx/models.py:1205-1214 has no Meta uniqueness; addresses property at models.py:1164-1202 sums these rows and filters negatives at line 1202. Commit 727978b ("Use get_or_create for V2 transfer and withdrawal records to prevent duplicates", 2026-05-29) is forward-only; duplicate rows dated 2026-05-19/2026-05-28. V1 TKNZ_TX handlers in /Users/tyler/prj/vfx/vfx-explorer/rbx/tasks.py still call VbtcTokenAmountTransfer(...).save() with no get_or_create.


### VBTCWithdrawalComplete clears is_pending_withdrawal even when other withdrawals on the token are still REQUESTED

**Severity:** medium · **Category:** correctness · **Location:** `rbx/tasks.py:995` · **Found by:** vbtc-tknz-processing

The VBTC_V2_WITHDRAWAL_COMPLETE handler sets token.is_pending_withdrawal = False unconditionally after completing one request, without checking for other rows still in REQUESTED status. Mainnet shows tokens routinely carry multiple withdrawal requests: token 6d893dce...:1780080357 has had four (ids 5,6,7 completed; id 8 REQUESTED since 2026-05-29), token d11a9ef3... has id 4 REQUESTED since 2026-05-29. If a new request+complete cycle runs on either token, the complete will flip is_pending_withdrawal to False while the stale REQUESTED row is still outstanding — the flag the wallet uses to gate further withdrawals/transfers goes wrong. Mirror image: the request handler (line 955) unconditionally sets it True, so `reprocess_vbtc_v2 --type 27` re-marks every token with any historical withdrawal as pending even when all are completed. The flag should be derived: is_pending = token.withdrawal_requests.filter(status=REQUESTED).exists().

**Reviewer evidence:**

Mainnet rbx_vbtcv2withdrawalrequest: ids 4 and 8 status='requested' created 2026-05-29 (12 days stale), on tokens that also have completed requests; both tokens currently is_pending_withdrawal=true. tasks.py:955 (set True on request) and 995-996 (set False on any complete).

**Verification (high confidence):** Attempted to refute and failed on every axis. (1) Code: rbx/tasks.py:995-996 clears is_pending_withdrawal unconditionally after completing the one request matched by WithdrawalRequestHash, with no exists() check on other REQUESTED rows; line 955 unconditionally sets it True on request. (2) No guard elsewhere: grep shows these are the ONLY two writes to the field in the codebase — no periodic task re-derives it, and it is wallet-facing via api/btc/serializers.py:92. (3) Live mainnet confirms the trigger condition exists today: tokens 7 and 8 both carry a stale REQUESTED row (ids 4 and 8, created 2026-05-29) alongside completed rows, both with is_pending_withdrawal=true. With withdrawals now live, the next request+complete cycle on either token flips the flag False while the stale REQUESTED row remains outstanding. (4) The reprocess mirror claim also holds: reprocess_vbtc_v2.py with --type 27 replays only WITHDRAWAL_REQUEST txs, which would incorrectly set the flag True on tokens 1 and 2 (all withdrawals completed, currently correctly False). Severity stays medium: real wallet-facing gating-flag corruption with the precondition standing in prod, but it needs another withdrawal cycle to manifest, the flag is currently consistent, and the fix is the trivial derived-flag change suggested. Caveat for the fixer: the 12-day-stale REQUESTED rows show there is no expiry/cancel path, so a pure exists() derivation would pin tokens 7/8 pending forever unless stale requests are also resolved.

**Verification evidence:**

rbx/tasks.py:993-996: "# Balance fields (global_balance, total_sent) are updated by the / # periodic BTC chain sync (update_vbtc_balances), not here. / token.is_pending_withdrawal = False / token.save()" — no check of other REQUESTED rows. Mainnet query (rbx_vbtcv2withdrawalrequest JOIN rbx_vbtcv2token): token_id=8 (6d893dce1c244ad5a98b3981a63dcd2a:1780080357) rows id=5,6,7 status=completed and id=8 status=requested created 2026-05-29T20:27:49Z, is_pending_withdrawal=true; token_id=7 (d11a9ef3f98d4723849d5463596a6c21:1779979897) id=3 completed, id=4 requested created 2026-05-29T18:15:36Z, is_pending_withdrawal=true. Grep confirms tasks.py:955 and :995 are the only writes to is_pending_withdrawal; reprocess_vbtc_v2.py:34-35 allows --type 27 request-only replay.


### Transaction types 29 (VBTC_V2_WITHDRAWAL_CANCEL) and 30 (VOTE) have no handler — cancelled withdrawals stay REQUESTED forever

**Severity:** medium · **Category:** correctness · **Location:** `rbx/tasks.py:958` · **Found by:** vbtc-tknz-processing

process_transaction handles types 25-28 but has no branch for VBTC_V2_WITHDRAWAL_CANCEL (29) or VBTC_V2_WITHDRAWAL_VOTE (30), both defined in Transaction.Type (rbx/models.py:177-178). When the first cancel lands on chain, the explorer will keep the VbtcV2WithdrawalRequest in REQUESTED and the token in is_pending_withdrawal=True permanently — the token appears locked for withdrawal in every wallet even though the chain released it. This is plausibly already user-visible: requests id 4 (token d11a9ef3) and id 8 (token 6d893dce) have sat in REQUESTED for 12 days with no completion; if the CLI cancelled or expired them, the explorer has no code path to ever reflect that (mainnet currently has zero type-29/30 txs, so either they are genuinely outstanding or cancellation isn't a chain tx yet — either way the handler gap is real and the model has no CANCELLED status to transition to).

**Reviewer evidence:**

grep: only models.py mentions WITHDRAWAL_CANCEL/VOTE (enum + label), no occurrence in rbx/tasks.py. Mainnet: SELECT type, count(*) FROM rbx_transaction WHERE type IN (29,30) GROUP BY 1 → 0 rows. VbtcV2WithdrawalRequest.Status has only REQUESTED/COMPLETED (models.py:1218-1220). Stale REQUESTED rows: ids 4, 8 created 2026-05-29.

**Verification (high confidence):** The handler gap is real and unguarded. rbx/tasks.py process_transaction branches only on types 25-28; types 29 (VBTC_V2_WITHDRAWAL_CANCEL) and 30 (VOTE) exist in the Transaction.Type enum but have no processing branch anywhere in the codebase. The cancel flow is production-exposed: the explorer ships POST /api/btc/vbtc-v2/withdraw/cancel/prepare|send/ endpoints (api/btc/urls.py:65-66) that are pure CLI proxies (rbx/client.py:1199-1209, SendRawCancelWithdrawalTx) and write nothing to the local DB, and the SDK integration doc maps cancel to on-chain tx type 29. So when the first cancel TX lands, block sync will index it but never update VbtcV2WithdrawalRequest.status (which has no CANCELLED choice — only REQUESTED/COMPLETED at models.py:1218-1220) or clear token.is_pending_withdrawal — only tasks.py:955/995 ever touch that flag, and no periodic reconciliation exists. The reprocess_vbtc_v2 management command also omits types 29/30. However, the reviewer's stale-row evidence is over-attributed: mainnet has ZERO type-29/30 transactions (and testnet has zero type 27-30 at all), so requests ids 4 and 8 (REQUESTED since 2026-05-29, both tokens is_pending_withdrawal=true) cannot have been cancelled on-chain — they are genuinely outstanding/abandoned, not victims of the gap. The bug is latent, never yet triggered. Also, type 30 (VOTE) is a validator vote with no explorer state to mutate; its missing handler is benign. Severity stays medium: latent but live-exposed, user-visible lockup when triggered (token permanently excluded from withdrawal coin selection and shown locked in wallets), and the fix needs a migration (new CANCELLED status) plus a reprocess-list update.

**Verification evidence:**

Code: rbx/tasks.py has only `elif tx.type == Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST:` (line 924) and `elif tx.type == Transaction.Type.VBTC_V2_WITHDRAWAL_COMPLETE:` (line 958) — grep for VBTC_V2_WITHDRAWAL_CANCEL across *.py hits only rbx/models.py:177 (enum) and models.py:300 (label); cancel views at api/btc/views.py:650-662 only call `_vbtc_v2_proxy(send_raw_cancel_withdrawal_tx, ...)` with no local writes. Mainnet SQL: SELECT type, count(*) FROM rbx_transaction WHERE type IN (27,28,29,30) GROUP BY 1 → {27: 8, 28: 6} (no 29/30 rows); SELECT id,status,created_at FROM rbx_vbtcv2withdrawalrequest → ids 4 and 8 status='requested' since 2026-05-29, joined tokens d11a9ef3.../6d893dce... both is_pending_withdrawal=true. Testnet: same type query → 0 rows. Zero type-29 txs on either network means the stale rows were never cancelled on-chain — the gap is real but has not yet fired.


### V2 token lookups .get() on non-unique sc_identifier catch only DoesNotExist — MultipleObjectsReturned crashes block sync

**Severity:** medium · **Category:** correctness · **Location:** `rbx/models.py:1130` · **Found by:** vbtc-tknz-processing

VbtcV2Token.sc_identifier (and VbtcToken.sc_identifier, line 1063) is db_index=True but NOT unique. The mint branch does a non-atomic get-then-create (tasks.py:454-457), so a reprocess_vbtc_v2 run on the web pod concurrent with blocks-worker mint processing (or a double-delivered task) can insert two token rows for the same sc_identifier — nothing in the DB prevents it. Once a duplicate exists, every handler that does VbtcV2Token.objects.get(sc_identifier=...) catching only DoesNotExist (tasks.py:846, 908, 937, 972) raises MultipleObjectsReturned, which is uncaught and triggers the mid-block abort in finding 6 — i.e., one duplicate token row poisons processing of every subsequent transaction touching that token AND the remainder of any block containing one. Currently mainnet has no duplicates (11 rows, 11 distinct sc_identifiers), so this is a latent constraint gap, not live corruption — but the same constraint gap already let the transfer-row duplicates of finding 4 happen.

**Reviewer evidence:**

models.py:1130 `sc_identifier = models.CharField(max_length=64, db_index=True)` (no unique=True); tasks.py:454-457 get/create race; tasks.py:846/908/937/972 catch only VbtcV2Token.DoesNotExist. Mainnet: SELECT count(*), count(DISTINCT sc_identifier) FROM rbx_vbtcv2token → 11/11.

**Verification (high confidence):** Every cited element verifies against code and live mainnet. VbtcV2Token.sc_identifier (models.py:1130) and VbtcToken.sc_identifier (models.py:1063) have db_index=True but no unique=True, while FungibleToken (models.py:885) does have unique=True — confirming this is an omission, not a project convention. Live mainnet pg_indexes shows only non-unique btree indexes on rbx_vbtcv2token.sc_identifier; nothing in the DB prevents duplicate rows. The mint branch (tasks.py:454-457) is a non-atomic get-then-create with no lock or get_or_create, and a real concurrent-writer path exists: rbx/management/commands/reprocess_vbtc_v2.py calls process_transaction() directly (web pod) and includes VBTC_V2_MINT, while blocks-worker processes the same branch; sync_block's autoretry plus Transaction.objects.create can also re-process mints. Once a duplicate exists, tasks.py:846/908/937/972 each do .get(sc_identifier=...) catching only DoesNotExist, so MultipleObjectsReturned propagates uncaught through process_transaction (called bare at tasks.py:216 inside the per-transaction block loop), aborting the rest of the block — every subsequent tx touching that token then fails the same way. Mainnet currently has 11/11 distinct sc_identifiers, so this is a latent constraint gap, not active corruption. Severity stays medium: trigger probability is low (manual reprocess concurrent with rare mint processing, or task double-delivery), but blast radius once triggered is significant (persistent block-sync poisoning until manual dedup).

**Verification evidence:**

models.py:1130 `sc_identifier = models.CharField(max_length=64, db_index=True)` (no unique=True; contrast models.py:885 FungibleToken `unique=True, db_index=True`). Live mainnet: SELECT indexname, indexdef FROM pg_indexes WHERE tablename='rbx_vbtcv2token' → only `CREATE INDEX rbx_vbtcv2token_sc_identifier_631fa332 ... USING btree (sc_identifier)` plus _like variant — no unique index. SELECT count(*), count(DISTINCT sc_identifier) FROM rbx_vbtcv2token → 11 / 11 (no duplicates today). tasks.py:455-457 get-then-create; tasks.py:846/908/937/972 catch only VbtcV2Token.DoesNotExist; tasks.py:216 calls process_transaction(tx) bare inside sync_block's per-transaction loop; rbx/management/commands/reprocess_vbtc_v2.py:59 calls process_transaction(tx) directly, enabling the concurrent-writer race.


### Decimal-from-float conversions corrupt BTC amounts above ~1.23 BTC

**Severity:** medium · **Category:** correctness · **Location:** `btc/btc_client.py:16` · **Found by:** vbtc-token-model

BtcClient converts satoshis with `Decimal(int(sats) * 0.00000001)` — an int*float multiply followed by Decimal(float). Binary float artifacts survive quantization to the 16-decimal-place columns once amounts reach ~1.23 BTC: verified locally, 123456789 sats produces 1.2345678900000001 (wrong last digit) instead of 1.23456789, and larger holdings get proportionally larger errors (20999999.9769 BTC → off by 1.25e-8 BTC). These values feed global_balance/total_received/total_sent (update_vbtc_balances command line 23-26 and VbtcV2DetailView api/btc/views.py:231-234), and since addresses computes owner = global_balance + ... and filters `bal > 0`, a 1-ulp residue makes should-be-zero balances appear as 1e-16 dust entries or flips them slightly negative (hidden). The same anti-pattern exists in rbx/tasks.py where chain amounts come from json.loads as Python floats: `Decimal(parsed["Amount"])` at tasks.py:905 (transfers) and tasks.py:948 (withdrawal amounts). Current mainnet amounts are all < 0.005 BTC so artifacts are quantized away today, but any token holding > ~1.23 BTC gets a corrupted stored balance. Fix: `Decimal(sats) / Decimal(100_000_000)` in btc_client, and `json.loads(tx.data, parse_float=Decimal)` (or Decimal(str(x))) in tasks.py.

**Reviewer evidence:**

python3 test: Decimal(123456789 * 0.00000001) = 1.23456789000000011213... → quantized at 1e-16 = 1.2345678900000001 != exact 1.23456789; Decimal(2099999997690000 * 1e-8) off by >1e-9 BTC. btc/btc_client.py:16 `satoshi_to_btc_multiplier = 0.00000001` (float), used at lines 53-66. rbx/tasks.py:905 `amount = Decimal(parsed["Amount"])` where parsed comes from json.loads (float).

**Verification (high confidence):** Every element of the finding verified against actual code, local reproduction, and live mainnet data. btc/btc_client.py:16 defines the satoshi multiplier as a float and lines 53-66 wrap int*float in Decimal(), producing binary-float artifacts that survive quantization to the decimal_places=16 columns once amounts exceed ~1 BTC (reproduced: 123456789 sats -> 1.2345678900000001 instead of 1.23456789). These values are persisted via update_vbtc_balances.py:23-26 and VbtcV2DetailView (api/btc/views.py:231-234). The same Decimal(float) pattern exists at rbx/tasks.py:905 and ~947, and live mainnet rbx_transaction.data confirms Amount arrives as a bare JSON number ("Amount":0.0008), so json.loads yields a Python float. The addresses property (rbx/models.py:1187-1203) sums global_balance with exact transfer Decimals and filters bal > 0, so 1-ulp residues create 1e-16 dust entries or hide should-be-positive balances. No rounding guard exists anywhere downstream (serializers expose raw fields). Live mainnet confirms the bug is latent today: all 11 tokens hold <= 0.0022 BTC, where float error falls below the 1e-16 column precision. Severity adjusted high -> medium: the bug is deterministic above ~1 BTC but error magnitude is ~1e-8 satoshi; the explorer is a read model so impact is incorrect displayed balances, phantom dust entries in the addresses API, and reconciliation residue rather than fund loss, and there is zero impact at current production amounts. Would escalate if wallet clients use explorer balances to craft exact-amount transactions.

**Verification evidence:**

btc/btc_client.py:16 `satoshi_to_btc_multiplier = 0.00000001` with lines 53-55 `total_received = Decimal(int(data.get("total_received", 0)) * self.satoshi_to_btc_multiplier)`; reproduced locally: python3 `Decimal(123456789 * 0.00000001).quantize(Decimal('1e-16'))` -> 1.2345678900000001 (!= 1.23456789). Live mainnet SQL: `SELECT type, left(data::text,500) FROM rbx_transaction WHERE type IN (26,27) ORDER BY date_crafted DESC LIMIT 4` -> `..."Amount":0.0008}` (bare JSON float feeding `Decimal(parsed["Amount"])` at rbx/tasks.py:905); `SELECT sc_identifier, global_balance FROM rbx_vbtcv2token ORDER BY global_balance DESC LIMIT 20` -> max 0.0022000000000000 BTC across all 11 tokens, confirming artifacts are currently quantized away.


### Withdrawal completion creates a balance-inflation window until the 10-minute BTC sync catches up

**Severity:** medium · **Category:** correctness · **Location:** `rbx/tasks.py:993` · **Found by:** vbtc-token-model

When a VBTC_V2_WITHDRAWAL_COMPLETE tx is processed, the withdrawal flips to COMPLETED immediately, but global_balance is intentionally left to the periodic sync (comment at tasks.py:993-994; celery beat runs update_vbtc_balances every 10 minutes, project/celery.py:30). In addresses (rbx/models.py:1180-1199), total_withdrawn includes the new completion right away while global_balance still includes the not-yet-synced (or not-yet-confirmed — blockchain.info total_sent reflects confirmed spends) BTC. During that window the owner's displayed balance = stale_gb + w.amount + net_transfers, i.e. the owner is inflated by exactly the withdrawal amount while the requestor is already debited, so the sum of displayed claims exceeds the real BTC backing by w.amount for up to (BTC confirmation time + 10 min). The mirror-image staleness also exists for deposits. Related smaller gap folded in here: REQUESTED (pending) withdrawals are never deducted in addresses — live token 8 shows RPKx with 0.0009 while their 0.0002 withdrawal (request id 8) is in flight; if the CLI escrows on request, the explorer overstates the requestor's spendable balance until completion.

**Reviewer evidence:**

tasks.py:993-996 comment: 'Balance fields (global_balance, total_sent) are updated by the periodic BTC chain sync (update_vbtc_balances), not here.' project/celery.py:30 schedules every 600s. Mainnet token 8: is_pending_withdrawal=true, rbx_vbtcv2withdrawalrequest id 8 (RPKx, 0.0002, status=requested) not reflected in the addresses formula (models.py:1180-1182 filters status=COMPLETED only).

**Verification (high confidence):** Confirmed by code and live mainnet data. rbx/tasks.py:987-996 flips the withdrawal to COMPLETED immediately and explicitly defers global_balance/total_sent to the 10-minute beat task (project/celery.py:29-31; btc/management/commands/update_vbtc_balances.py is the only periodic writer). rbx/models.py:1180-1199 then immediately includes the new completion in total_withdrawn (credited to the owner on top of the stale global_balance) and debits the requestor, so during the window the owner is inflated by exactly w.amount and the sum of displayed claims exceeds real BTC backing by w.amount. I verified the formula empirically: token 8's computed addresses (RNiQ=0.00007083, RPKx=0.0009) sum exactly to global_balance=0.00097083. The folded-in pending-withdrawal gap is also real and live: rbx_vbtcv2withdrawalrequest id 8 (token 8, RPKx, 0.0002, status='requested' since 2026-05-29 — 12 days) is not deducted from RPKx's displayed 0.0009, and request id 4 (token 7, 0.0003) is similarly 12 days in 'requested'. One partial mitigation the reviewer missed: VbtcV2DetailView (api/btc/views.py:226-236) refreshes global_balance live from blockchain.info on every detail GET, so the detail endpoint self-heals on access — but the list endpoints (VbtcV2ListAllView/VbtcV2ListView, api/btc/views.py:189-218) serve the stale denormalized value, and even a live refresh cannot see the outgoing BTC before blockchain.info does, so a window always exists. Severity stays medium: addresses is display/API-only in the explorer (only consumed by api/btc/serializers.py); the CLI/network enforces actual balances, so this is an API-correctness/display issue, not a fund-safety issue — transient for completions, persistent (12 days observed) for pending requests if the CLI escrows on request.

**Verification evidence:**

rbx/tasks.py:989-996: "withdrawal.status = VbtcV2WithdrawalRequest.Status.COMPLETED ... # Balance fields (global_balance, total_sent) are updated by the / # periodic BTC chain sync (update_vbtc_balances), not here." + rbx/models.py:1180-1182 filters status=COMPLETED only. Live mainnet SQL: rbx_vbtcv2withdrawalrequest id=8 → {token_id:8, requestor:"RPKxShZhfytjfffG8sQZG5ZTj9qqcAPgvJ", amount:0.0002, status:"requested", created_at:2026-05-29, btc_transaction_hash:""}; token 8 → {global_balance:0.00097083, is_pending_withdrawal:true}; transfers to RPKx total 0.0009, so addresses shows RPKx=0.0009 with the 0.0002 pending withdrawal undeducted. Computed addresses sum (0.00007083+0.0009) equals global_balance exactly, confirming the formula. Mitigation reviewer missed: api/btc/views.py:226-236 (VbtcV2DetailView.get) refreshes global_balance from BtcClient on every detail request, but list views at api/btc/views.py:189-218 serve the stale value.


### VbtcV2DetailView performs an external API call and DB write on every unauthenticated GET

**Severity:** medium · **Category:** operational · **Location:** `api/btc/views.py:226` · **Found by:** vbtc-token-model

VbtcV2DetailView.get() synchronously calls blockchain.info (BtcClient.get_balance) and then writes global_balance/total_received/total_sent/tx_count to the token row on every request, with no cache decorator (unlike BtcAddressView which is cached) and no authentication. Failure scenarios: (1) blockchain.info rate-limits (it throttles aggressively per-IP); get_balance returns None and the view silently serves stale data — and because the celery beat sync shares the same source IP, heavy detail-page traffic can starve the beat sync too, leaving ALL token balances stale; (2) a GET that mutates state breaks HTTP semantics and lets any anonymous client drive write load and last-writer-wins races against the 10-minute update_vbtc_balances task; (3) the write path stores the Decimal-from-float artifacts described in the precision finding on every page view. The 16dp float artifact and the per-GET write together mean two consecutive GETs can flip the stored balance's last digit back and forth.

**Reviewer evidence:**

api/btc/views.py:221-236: get_object → BtcClient().get_balance(token.deposit_address) → token.save() inside a GET handler, no @cache_request decorator (contrast line 51 BtcAddressView). BtcClient.get_balance returns None on any exception (btc/btc_client.py:48-50), which the view treats as 'serve stale silently'.

**Verification (high confidence):** The core finding is real and verified at every layer. api/btc/views.py:226-235 synchronously calls BtcClient().get_balance (blockchain.info on mainnet per btc/btc_client.py:29) and performs token.save() inside an unauthenticated GET handler, with no cache decorator — in direct contrast to BtcAddressView at line 51 which is wrapped in cache_request(CACHE_TIMEOUT_LONG). get_balance returns None on any exception (btc_client.py:48-50) and the view silently serves stale data. The 10-minute celery beat task (project/celery.py:29-30) uses the same BtcClient and also silently skips failed lookups, so sustained upstream rate-limiting would leave all token balances stale. Anonymous access confirmed live: GET to a sibling /api/btc/vbtc-v2/ endpoint on data.verifiedx.io returned 200 with no auth. Mitigating factors the reviewer omitted: DRF AnonRateThrottle at 300/min per IP is active (project/settings/api.py:27-34), bounding single-client abuse, and mainnet currently has only 11 VbtcV2Token rows with presumably light detail-page traffic, so present-day operational impact is small. One sub-claim is refuted: the "two consecutive GETs flip the last digit back and forth" is impossible — both write paths compute the identical deterministic Decimal(int * 1e-8) expression, and live DB rows show clean 16dp values (e.g., 0.0009708300000000) with no artifact patterns. The writes are last-writer-wins but carry same-source idempotent data, so the race is benign. Net: a real design flaw (GET with side effects, uncached external dependency, anonymous write-load driver, shared rate-limit budget with the beat sync) whose realistic worst case is stale balances and tied-up gunicorn workers (up to 15s timeout each), not data corruption or fund loss. Severity medium stands.

**Verification evidence:**

api/btc/views.py:226-235: "def get(self, request, *args, **kwargs): token = self.get_object(); client = BtcClient(); balance_info = client.get_balance(token.deposit_address); if balance_info: token.global_balance = ...; token.save()" — uncached, unauthenticated (no permission_classes in api/btc/; live check: curl https://data.verifiedx.io/api/btc/vbtc-v2/transfers/nonexistent-sc-id/ → HTTP 200 {"results":[]} with no token), vs BtcAddressView at views.py:51 wrapped in @method_decorator(cache_request(settings.CACHE_TIMEOUT_LONG)). Same BtcClient is used by the 10-min beat task (project/celery.py:30, btc/management/commands/update_vbtc_balances.py:16-27). Refuting the digit-flip sub-claim, mainnet SQL "SELECT sc_identifier, global_balance ... FROM rbx_vbtcv2token LIMIT 11" returned 11 rows of clean values (e.g., 0.0009708300000000), 0 rows matching float-artifact patterns.


### All vBTC v2 mint/transfer/withdraw endpoints are unauthenticated in production

**Severity:** medium · **Category:** security · **Location:** `project/settings/api.py:15` · **Found by:** vbtc-api-security

DEFAULT_PERMISSION_CLASSES resolves to AllowAny when API_AUTH_REQUIRED is false, and .env sets API_AUTH_REQUIRED=False. None of the btc/raw views override permission_classes, so every prepare/send/execute/broadcast endpoint is fully public (confirmed by scripts/sign_and_send_completion.js POSTing to the live /btc/vbtc-v2/.../send/ with no auth header). This is acceptable for the signature-bound 'send' steps (the blockchain rejects bad signatures), but it means the entire security boundary is the downstream CLI. Any endpoint that performs a sensitive action without a signature being verified downstream (e.g. the FROST Amount/BTCDestination issue, the DoS/side-effect issues below) is directly reachable by anyone on the internet with no rate-limiting beyond 1000/min per IP.

**Reviewer evidence:**

project/settings/api.py:11-19 permission classes; .env:66 API_AUTH_REQUIRED=False; .env:63 API_THROTTLE_RATE=1000/min; scripts/sign_and_send_completion.js:38-46 calls send endpoint with only Content-Type header.

**Verification (high confidence):** All cited evidence is confirmed. project/settings/api.py:15-19 sets DEFAULT_PERMISSION_CLASSES to AllowAny when API_AUTH_REQUIRED is false; .env:66 sets API_AUTH_REQUIRED=False; .env:63 sets API_THROTTLE_RATE=1000/min. A grep across api/btc/views.py for permission_classes/authentication_classes returned no matches (exit 1) — every VbtcV2 view (prepare/send/execute/broadcast/list/detail) is a plain GenericAPIView/RetrieveAPIView subclass inheriting the global AllowAny default, with no per-view override. I confirmed public access empirically with a read-only GET (no state change): GET https://data.verifiedx.io/api/btc/vbtc-v2/ with no Authorization header returned HTTP 200 and live token data. The completion script (scripts/sign_and_send_completion.js:14,38-46) POSTs to the production send endpoint with only a Content-Type header, matching the claim. The only protection on sensitive endpoints is the per-IP throttle (1000/min) plus downstream CLI signature verification — exactly as the reviewer states. Severity medium is appropriate: the signature-bound send steps are protected by blockchain signature checks, so this is an accurate architectural-boundary observation rather than a standalone critical exploit; its impact depends on the companion non-signature-verified findings.

**Verification evidence:**

project/settings/api.py:15-19: "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated" if API_AUTH_REQUIRED else "rest_framework.permissions.AllowAny"]; .env:66 API_AUTH_REQUIRED=False; grep -n "permission_classes\|authentication_classes" api/btc/views.py → no matches (exit 1); live GET https://data.verifiedx.io/api/btc/vbtc-v2/ with no auth header → HTTP 200 {"results":[{"sc_identifier":"68dbc155e07c42eaad9aa4a0c37657a8:1781020123","name":"vBTC",...,"deposit_address":"bc1p0d74995wr45qrtqepxpuaq6hz73el0xjlf07ggktgy3ss6vte2ps5ufu9k",...}]}


### FROST execute spawns an unbounded daemon thread per request with no dedicated throttle

**Severity:** medium · **Category:** operational · **Location:** `api/btc/views.py:526` · **Found by:** vbtc-api-security

VbtcV2WithdrawCompleteExecuteView starts a new daemon thread on every POST (threading.Thread(target=_run_frost).start()) that makes a CLI call with a 180s timeout, then returns immediately. The endpoint is public and only covered by the global 1000/min anon throttle. An attacker can fire ~1000 requests/min/IP (more across IPs), each spawning a thread that holds a connection to the single-concurrency vbtc-worker/CLI for up to 3 minutes, exhausting the gunicorn worker's thread pool and the CLI's capacity and blocking legitimate withdrawals. There is no cap on concurrent FROST jobs and no per-endpoint throttle class.

**Reviewer evidence:**

views.py:523-549 (uuid job, thread spawn, 180s execute_complete_withdrawal at client.py:1172-1178). No throttle_classes override on the view; global rate 1000/min from .env:63.

**Verification (high confidence):** The cited code behaves as claimed: VbtcV2WithdrawCompleteExecuteView spawns a new daemon thread per POST with no concurrency cap, no dedup, and no per-endpoint throttle; the thread makes a blocking 180s HTTP call directly to the single CLI node; the endpoint is public (API_AUTH_REQUIRED=False -> AllowAny) and covered only by the global 1000/min anon throttle. Only field-presence validation runs before the thread spawn, so invalid payloads still trigger CLI calls. Two reviewer mechanism details are wrong but don't change the verdict: (1) the call goes directly to the CLI HTTP API, not through the concurrency=1 celery vbtc-worker; (2) gunicorn runs 3 sync workers with no thread pool, so the threat is unbounded OS-thread/socket/memory growth in the web process (~3000 live threads worst case at 1000/min x 180s) plus flooding the CLI FROST endpoint, not thread-pool exhaustion. Severity stays medium: real operational DoS surface on a live withdrawal path, but no integrity/funds risk, attack impact depends on CLI rejection speed for invalid signatures (untestable read-only), and adjacent synchronous proxy endpoints share the same throttle exposure.

**Verification evidence:**

api/btc/views.py:549 `threading.Thread(target=_run_frost, daemon=True).start()` executed unconditionally per POST after only `_require_fields` presence checks (views.py:501-507); rbx/client.py:1172-1178 `execute_complete_withdrawal` -> `_vbtc_v2_request(..., timeout=180)` which is a direct blocking `requests.post` to the CLI (client.py:1065-1078); no `throttle_classes`/`permission_classes` overrides anywhere in api/btc/views.py (grep: zero hits); .env:63 `API_THROTTLE_RATE=1000/min`, .env:66 `API_AUTH_REQUIRED=False` -> AllowAny via project/settings/api.py:15-19; route exposed at api/btc/urls.py:59. Correction evidence: porter.yaml:6 `run: "gunicorn --workers 3 project.wsgi --log-file -"` (sync workers, no thread pool to exhaust), and client.py shows the call bypasses the celery vbtc-worker entirely.


### VbtcV2DetailView GET mutates the DB and calls an external BTC API on every uncached request

**Severity:** medium · **Category:** operational · **Location:** `api/btc/views.py:226` · **Found by:** vbtc-api-security

VbtcV2DetailView.get() performs a write (token.save()) and a synchronous external BtcClient.get_balance(token.deposit_address) call on every GET, with no cache_request decorator (unlike BtcAddressView) and no auth. A GET endpoint with side effects violates HTTP semantics and, more importantly, lets any anonymous caller amplify load: at 1000/min/IP this drives 1000 external BTC-explorer calls and 1000 DB writes per minute per token id, which can exhaust the upstream BTC API rate limit (breaking balance refresh for everyone) and generate write contention.

**Reviewer evidence:**

views.py:226-236 get_object -> BtcClient().get_balance -> token.save(); contrast BtcAddressView which is wrapped in cache_request (views.py:51). No throttle override.

**Verification (high confidence):** Confirmed at the cited location. api/btc/views.py:226-236: VbtcV2DetailView.get() calls BtcClient().get_balance(token.deposit_address) — a synchronous, uncached HTTP call to blockchain.info /rawaddr/<addr> on mainnet (btc/btc_client.py:29,36-44, no API key, timeout=(5,10)) — then token.save() (full-row UPDATE, no update_fields). There is no @cache_request on this view, in direct contrast to BtcAddressView at views.py:51, and no per-view permission/throttle overrides anywhere in api/btc/views.py. The endpoint is publicly routed at api/btc/urls.py:71 (vbtc-v2/detail/<sc_identifier>/). Repo .env shows API_AUTH_REQUIRED=False and API_THROTTLE_RATE=1000/min, and the vBTC v2 web-wallet integration doc points anonymous SDK clients at https://data.verifiedx.io/api, so anonymous access in production is consistent with the reviewer's claim.

Two corrections to the claim: (1) token.save() is NOT unconditional — it only runs when get_balance() succeeds (balance_info truthy); on upstream failure the client swallows the exception, logs, returns None, and the view serves stale data with no write. (2) "No throttle" is wrong: a global DRF AnonRateThrottle applies (project/settings/api.py:27-34, api/throttling.py), capping at API_THROTTLE_RATE per IP — but that cap is 1000/min in .env, which is exactly the reviewer's amplification figure, and it is per-IP so multi-IP abuse scales further.

Impact calibration: mainnet has only 11 VbtcV2Token rows (live read-only query), so DB write contention is negligible today. The real risks are (a) burning blockchain.info's unauthenticated rate limit from the server's egress IP — which is shared with api/galxe/views.py and the update_vbtc_balances management command, so a ban breaks balance refresh for the live vBTC v2 wallet — and (b) gunicorn worker starvation, since every uncached GET blocks up to ~15s on the external call. Medium severity stands: real, anonymously triggerable operational risk, but no data corruption or auth bypass, and failures degrade to stale data rather than errors. Fix is cheap: add cache_request (as BtcAddressView does) and/or move balance refresh out of the GET path.

**Verification evidence:**

api/btc/views.py:226-236:
    def get(self, request, *args, **kwargs):
        token = self.get_object()
        client = BtcClient()
        balance_info = client.get_balance(token.deposit_address)
        if balance_info:
            token.global_balance = balance_info["balance"]
            ...
            token.save()
        return Response(self.get_serializer(token).data)
— no @cache_request (contrast views.py:51 @method_decorator(cache_request(settings.CACHE_TIMEOUT_LONG)) on BtcAddressView); BtcClient.get_balance (btc/btc_client.py:32-50) does a raw requests.get to https://blockchain.info/rawaddr/<addr> with no caching/API key; routed at api/btc/urls.py:71. .env: API_AUTH_REQUIRED=False, API_THROTTLE_RATE=1000/min. Mainnet DB (read-only): SELECT count(*) FROM rbx_vbtcv2token → 11.


### No reorg handling: blocks are keyed by height via get_or_create and the stored hash is never re-checked — confirmed stale orphaned block on mainnet

**Severity:** medium · **Category:** correctness · **Location:** `rbx/tasks.py:165` · **Found by:** block-sync

Block.objects.get_or_create(height=height, defaults={... hash ...}) means if the explorer indexed a block that was later orphaned by the chain, re-syncing the height is a no-op: the hash, transactions, and balance effects of the orphaned block are kept forever and the canonical block's transactions are never indexed. previous_hash is stored but never validated against the prior block's hash, so the corruption is invisible. This is not hypothetical: on mainnet, block 6610259's previous_hash is 3c097df4...88aa1c but the stored block 6610258 has hash 22e3749c...7756d2 — the DB holds an orphaned version of 6610258, and its 3 transactions (vs 1 in each neighbor) were applied to address balances. Address balances and tx history derived from that block are permanently wrong.

**Reviewer evidence:**

Mainnet SQL over last 100k blocks found exactly 1 broken parent link: height 6610259 previous_hash=3c097df46625...aa1c, but stored 6610258.hash=22e3749c72ae...56d2. rbx_transaction has 3 rows for block 6610258. Code: rbx/tasks.py:165-189 never compares data['Hash'] to an existing block's hash.

**Verification (high confidence):** The core claim is confirmed: rbx/tasks.py:165 uses Block.objects.get_or_create(height=...) with hash only in defaults, the stored hash is never re-checked, previous_hash is stored but validated nowhere (grep: only models.py:106, tasks.py:170, admin.py:146), and the validate_blocks management command only detects missing heights, not hash mismatches — so there is no reorg detection or repair path. Live mainnet data confirms real orphaned blocks, and the problem is 4x more widespread than the reviewer found: a lag(hash) scan over heights > 5,610,000 found 4 broken parent links (6469032, 6474731, 6493976, 6610259), and read-only node API fetches confirm the DB-stored hash is non-canonical at all 4 parent heights (e.g. canonical 6610258 = 3c097df4...88aa1c vs stored 22e3749c...7756d2). HOWEVER, the claimed impact ('address balances permanently wrong') is refuted for every observed instance: all transactions in all 4 orphaned blocks and all 4 canonical replacements are zero-amount/zero-fee (type-0 coinbase with 0 reward, type-23 VALIDATOR_HEARTBEAT self-sends), so the applied balance deltas were exactly zero. Observed damage is limited to non-canonical block hashes / tx history at 4 heights (~4 per 1M blocks, apparently 1-block tip reorgs). Severity adjusted from high to medium: the bug is real, silent, and undetectable, and a future micro-reorg over a block containing a value transfer or vBTC v2 tx would permanently corrupt balances/token state with no alarm — but no monetary corruption has actually occurred, and observed reorgs only clipped heartbeat-only blocks.

**Verification evidence:**

Mainnet SQL (lag scan, heights > 5610000) returned 4 broken parent links, including {"height": 6610259, "previous_hash": "3c097df46625f051cead24c4cef12d2d06d13d95f756d863b5f2f8a88188aa1c", "parent_hash": "22e3749c72ae065a9d2b6957a4723ab3e7f7ef19a1388c831fa337577e7756d2"} plus 6469032, 6474731, 6493976. Node API GET api/V1/SendBlock/6610258 returns canonical Hash: 3c097df46625...88aa1c (PrevHash matches stored 6610257), proving the DB block 22e3749c... is orphaned. Code: rbx/tasks.py:165-169 'block, block_created = Block.objects.get_or_create(height=height, defaults={..."hash": data["Hash"],...})' — hash never compared on existing rows. Impact refutation: all txs at the 4 orphaned heights (DB and canonical, e.g. rbx_transaction rows for 6610258: type 0 Coinbase_BlkRwd amount 0.0000000000000000 fee 0, and two type-23 self-sends amount 0 fee 0) carry zero value, so no address balance was corrupted.


### Blocking calls without timeouts inside the single-threaded block-sync path can stall all block indexing indefinitely

**Severity:** medium · **Category:** operational · **Location:** `rbx/tasks.py:1452` · **Found by:** block-sync

Block sync runs synchronously on blocks-worker (concurrency=1). Inside sync_block: (a) notify_socket_service does requests.post with no timeout (tasks.py:1448-1458) — if the socket service hangs accepting but not responding, the worker blocks forever and block sync stops network-wide with no error; (b) get_nft (client.py:466-482) has no request timeout and on failure sleeps 5s x 5 attempts = 25s of blocking inside indexing for every unavailable smart contract (each NFT/TKNZ/VBTC mint); (c) handle_auction_sale_complete_tx is called synchronously from process_transaction (tasks.py:516). The 10-second beat (project/celery.py:22) keeps enqueueing sync_the_blocks while the worker is stuck, building queue backlog. requests has no default timeout, so a single TCP black-hole turns into a permanent sync outage that the health_check would not even detect (see separate finding). Honorable mentions in the same class: get_status, get_master_nodes, get_topics, get_network_metrics, all tx_* functions in client.py also lack timeouts (only get_info/get_block have timeout=15).

**Reviewer evidence:**

tasks.py:1452 requests.post(...) no timeout; client.py:477 requests.get(url) no timeout inside get_nft retry loop with time.sleep(5); CLAUDE.md confirms blocks-worker concurrency=1; mainnet tip is currently fresh (13s behind) so this is a latent risk, not an active incident.

> ⚠️ _Adversarial verification was interrupted by a tooling failure during the final live-data check; the verifier had already confirmed the code-level claim. Last verifier note: "n/a"_


### Out-of-order and async processing breaks cross-transaction lookups (withdrawal complete, callbacks); --async path scatters blocks across concurrent workers

**Severity:** medium · **Category:** correctness · **Location:** `rbx/management/commands/sync_blocks.py:37` · **Found by:** block-sync

Several process_transaction handlers assume strictly in-order indexing: VBTCWithdrawalComplete looks up the prior request by hash (tasks.py:977-985) and silently returns if absent; RESERVE CallBack() looks up the original tx (tasks.py:583-587); NFT/token transfers assume the mint was already indexed. The normal path preserves order only because the command loop is synchronous on one worker. But sync_blocks --async does sync_block.apply_async(args=[height]) with no queue routing — the task lands on the default queue served by default-worker with concurrency > 1, so blocks process in parallel and out of order: balance read-modify-write on Address (tasks.py:219-241, get_or_create + save with no locking) races and corrupts balances, and a withdrawal-complete can be processed before its request, leaving the request permanently REQUESTED (the handler logs and returns, never retried). Any operator backfill using --all/--async (the documented flags) silently triggers this.

**Reviewer evidence:**

sync_blocks.py:35-39; rbx/tasks.py:146 sync_block has no queue= and project/celery.py:16 sets default queue; tasks.py:977-985 'Withdrawal request ... not found' → return (no retry); tasks.py:219-241 non-atomic balance updates.

> ⚠️ _Adversarial verification was interrupted by a tooling failure during the final live-data check; the verifier had already confirmed the code-level claim. Last verifier note: "All cited code matches. Now let me check worker configurations to verify the default-worker concurrency claim and whether the periodic path uses `--async`."_


### Health check monitors the chain node, not the explorer's own sync — a dead blocks-worker raises no alert

**Severity:** medium · **Category:** operational · **Location:** `rbx/management/commands/health_check.py:30` · **Found by:** block-sync

health_check (every 3 min, project/celery.py:34) fetches get_info()/get_block() from the node and alerts when the chain itself stops producing blocks. It never compares the node height to the explorer DB's Max(Block.height). Failure scenario: blocks-worker crashes or is wedged on a no-timeout call (see related finding) — the chain keeps producing, get_info stays fresh, health_check reports 'All is well' while the explorer DB falls hours behind and every wallet/API consumer serves stale balances. Given the silent-skip and partial-block findings, DB-side freshness/gap monitoring is the missing safety net. Secondary: it SMSes ALERT_NUMBERS on every 3-min run while a condition persists (no dedupe/cooldown), and any transient exception (e.g. the CLI cold-start first-request failure) immediately sends an 'Explorer Wallet is Unreachable' SMS false positive (line 42-43).

**Reviewer evidence:**

health_check.py:25-43 only touches node APIs; no Block model import for freshness. Recent commit e21b3e2 ('improvements to health monitoring checks') did not add DB-side checks.

> ⚠️ _Adversarial verification was interrupted by a tooling failure during the final live-data check; the verifier had already confirmed the code-level claim. Last verifier note: "The code fully confirms the claim. Let me do one live sanity check on the mainnet DB to see current sync freshness (context for impact)."_


### Sentry send_default_pii=True captures auth tokens, signatures, and phone numbers; 100% trace sampling

**Severity:** medium · **Category:** security · **Location:** `project/settings/logging.py:36` · **Found by:** web-api

sentry_sdk.init is configured with send_default_pii=True, so request headers (including the Authorization token used by address_permission), request bodies (signed-message signatures in SignTokenView, phone numbers in the faucet RequestFundsSerializer), and user data are shipped to Sentry on every captured event. traces_sample_rate defaults to 1.0, meaning 100% of transactions are sampled — both a privacy exposure (auth tokens + PII persisted in a third-party system) and an operational cost/volume concern. Failure scenario: a leaked or over-broadly-shared Sentry project grants replay of valid AuthToken values and user phone numbers.

**Reviewer evidence:**

project/settings/logging.py:35 traces_sample_rate default 1.0; :36 send_default_pii=True. AuthToken is read from the Authorization header (api/permissions.py:13-23); faucet collects phone (api/faucet/serializers.py).

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### stats_view crashes on empty aggregates and runs multiple full-table scans of 7.4M rows

**Severity:** medium · **Category:** correctness · **Location:** `rbx/views.py:33` · **Found by:** web-api

stats_view wraps aggregate sums in Decimal(...) directly: Decimal(query['total_fee__sum']) (line 33), Decimal(query['total_amount__sum']) (lines 53, 78). When the filtered queryset is empty the SUM is None and Decimal(None) raises TypeError -> uncaught 500. It also issues many unfiltered COUNT/SUM/values-annotate passes over rbx_transaction (7.4M rows) plus ADNR/shop groupings; it is only protected by the default 60s cache, so each cache miss is a multi-second multi-scan. Reachable unauthenticated at /vfx/stats/.

**Reviewer evidence:**

rbx/views.py:33,53,78 Decimal(query[...]) with no None guard; multiple Transaction.objects.filter(...).count()/aggregate over the full table. Live rbx_transaction=7,375,062 rows.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### WithdrawVbtcView failure branch uses a variable as a dict key, producing TypeError/garbage on failed withdrawals

**Severity:** medium · **Category:** correctness · **Location:** `api/raw/views.py:198` · **Found by:** web-api

On a failed vBTC withdrawal the view returns Response({"success": False, result: None}, status=500). `result` here is the (falsy) return value of client.withdraw_btc — withdraw_btc returns response.json(), which is typically a dict. Using a dict as a key raises 'TypeError: unhashable type: dict' (itself a 500 masking the real failure); if it returns None the response gets a literal null key. Either way the error path is broken and never returns the intended structured failure. Since vBTC v2 withdrawal is live on mainnet, this is hit whenever the CLI node rejects/empties a withdrawal.

**Reviewer evidence:**

api/raw/views.py:198 `{"success": False, result: None}` (should be a string key like "result"). client.withdraw_btc (rbx/client.py ~1046) returns response.json().

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### api/raw/* is an unauthenticated open relay to the internal wallet/CLI node

**Severity:** medium · **Category:** security · **Location:** `api/raw/views.py:1` · **Found by:** web-api

All raw views proxy directly to the internal RBX wallet node (BASE_URL=RBX_WALLET_ADDRESS) with no auth (inherited AllowAny), no allowlist, and minimal validation (RawTransactionSerializer is just a JSONField passthrough). Endpoints include send/, verify/, fee/, hash/, nonce/<address>, validate-signature/<message>/<address>/<path:signature>, beacon upload/assets, smart-contract-data, and withdraw-vbtc. While fund-moving calls require CLI-validated signatures, the relay lets anonymous callers drive the internal node's API surface for free (reconnaissance, nonce enumeration, beacon side effects that enqueue remote_nft_media_to_urls Celery tasks). Signatures and messages are also embedded in URL paths (validate-signature/beacon), so they land in access logs and any short-lived caches.

**Reviewer evidence:**

api/raw/views.py: TransactionView.post passes serializer JSONField straight to client.tx_send/tx_verify; BeaconAssetsView triggers remote_nft_media_to_urls.apply_async; api/raw/urls.py routes <path:signature> path params. No permission_classes anywhere in the module.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### Multi-address transaction/NFT endpoints build unbounded IN() lists against the 7.4M-row transaction table

**Severity:** medium · **Category:** operational · **Location:** `api/transaction/views.py:71` · **Found by:** web-api

TransactionMultiAddressListView splits a comma-separated path segment into address_list with no cap and filters Q(to_address__in=list)|Q(from_address__in=list) over rbx_transaction (7.4M rows). NftMultipleAddressesListView does the same against rbx_nft. A caller can pass thousands of addresses in one request, producing a giant IN()-on-OR query and a large planner/scan cost; the short cache keys on the full (huge) URL so cache hit rate is near zero for abusive inputs. Failure scenario: a single crafted long-URL request forces an expensive scan repeatedly.

**Reviewer evidence:**

api/transaction/views.py:71 addresses.split(',') with no length guard; api/nft/views.py:38 same pattern. Live rbx_transaction=7,375,062.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### Alerting shares its failure domain with the system it monitors: beat + default-worker + broker must all be healthy for any alert to fire

**Severity:** medium · **Category:** operational · **Location:** `project/celery.py:33` · **Found by:** celery-ops

health_check is a periodic Celery task scheduled by beat (runner service) and executed on the default queue by default-worker (project/celery.py:33-34, task at :56 has no queue= so it routes to default). If the runner pod dies, the broker is unreachable, or the default queue is clogged (shop_online_crawler/crawl_online_shops run every 5-10 min on the same queue and contain many time.sleep()s and unbounded retries — e.g. connect_to_shop retries with sleeps in rbx/client.py:676-711), health checks silently stop running. There is no dead-man's switch / external heartbeat (e.g. healthchecks.io ping on success), so 'no SMS' is indistinguishable from 'all healthy'. Concrete scenario: broker outage takes down ALL queues including the health check itself — total monitoring blackout during exactly the incident class it exists for.

**Reviewer evidence:**

project/celery.py:33-34 schedules health_check via beat; :56-60 task has no queue routing -> default queue; porter.yaml:16-18 single `runner` beat instance; no external heartbeat call anywhere (grep for healthchecks/heartbeat/deadman returns nothing).

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### Stale vBTC V2 withdrawal state is never reconciled: live tokens stuck in is_pending_withdrawal=true for 12 days

**Severity:** medium · **Category:** operational · **Location:** `rbx/tasks.py:953` · **Found by:** celery-ops

process_transaction sets token.is_pending_withdrawal=True on VBTCWithdrawalRequest() (~rbx/tasks.py:953) and only clears it when a VBTCWithdrawalComplete() or cancel TX is observed on-chain. There is no periodic reconciliation/expiry task for withdrawal requests that never complete (e.g. requests made during the CLI withdrawal blocker, abandoned FROST ceremonies, or a completion TX missed due to the non-atomic sync_block issue). The flag is served to wallets via the API (api/btc/serializers.py:92), so an orphaned request leaves the token flagged pending-withdrawal indefinitely, blocking/confusing wallet UX with no automated path out. Live mainnet confirms: 2 of 11 tokens have been stuck with is_pending_withdrawal=true and latest request status='requested' since 2026-05-29 (12 days, predating today's withdrawal go-live).

**Reviewer evidence:**

Mainnet query: tokens 6d893dce1c244ad5a98b3981a63dcd2a:1780080357 and d11a9ef3f98d4723849d5463596a6c21:1779979897 have is_pending_withdrawal=true with newest VbtcV2WithdrawalRequest in status 'requested' created 2026-05-29T20:27:49Z and 2026-05-29T18:15:36Z respectively.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### SMS alerting has no dedup/throttle and swallows the underlying exception

**Severity:** medium · **Category:** operational · **Location:** `rbx/management/commands/health_check.py:42` · **Found by:** celery-ops

health_check runs every 3 minutes. During a chain stall, handle_problem fires SMS to 2 numbers every run (40 messages/hour, hundreds during a multi-hour incident — alert fatigue plus Twilio cost). During a wallet outage, handle_exception does the same to WARNING_NUMBERS. Worse, handle_exception (line 62) receives the exception but never logs it or re-raises, so the root cause never reaches Sentry or logs — every failure mode (timeout, JSON decode, get_block returning None -> TypeError on block["Timestamp"], even SoftTimeLimitExceeded from the new 60s soft limit) is flattened into the same 'Explorer Wallet is Unreachable' SMS with zero diagnostic trail. Also note ALERT/WARNING phone numbers are hardcoded in source (lines 14-21).

**Reviewer evidence:**

health_check.py:42-43 `except Exception as e: self.handle_exception(e)` — `e` is never used beyond the parameter; :62-70 sends SMS only; project/celery.py:34 3-minute cadence; no cache/state to suppress repeats.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### update_vbtc_balances has a scaling cliff: serial full-table refresh with mandatory sleeps on a concurrency=1 queue, no lock/expires/time limit

**Severity:** medium · **Category:** operational · **Location:** `btc/management/commands/update_vbtc_balances.py:18` · **Found by:** celery-ops

The task runs every 10 minutes on vbtc_queue (project/celery.py:101, vbtc-worker concurrency=1) and iterates EVERY VbtcV2Token serially with sleep(0.5) plus an external BTC API call (BtcClient.get_balance, timeout up to 15s) per token. The Butterfly architecture creates per-user tokens, so token count grows linearly with users. At a realistic ~2s/token, ~300 tokens exceed the 10-minute interval; since beat enqueues unconditionally (no lock, no `expires`, no time_limit), runs then permanently overlap-in-queue and vbtc_queue backlogs without bound while the worker never idles. Additionally `VbtcV2Token.objects.all()` is fully materialized by len() at line 19 including the image_base64 column (full base64 images in-row), so memory per run also grows with token count. Today's 11 tokens are fine (~30s/run); this fails silently and progressively as vBTC V2 adoption grows, with no monitoring on the vbtc-worker to notice (see health-check finding).

**Reviewer evidence:**

btc/management/commands/update_vbtc_balances.py:18-30; project/celery.py:29-31 (10-min schedule) and :101-105 (no time limits, no lock); porter.yaml:15 `--concurrency=1`; mainnet rbx_vbtcv2token count = 11 with image_base64 stored in-row (information_schema confirms column).

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### Retry coverage is inverted: transient network errors don't retry, and the block path bypasses retries entirely

**Severity:** medium · **Category:** operational · **Location:** `rbx/tasks.py:51` · **Found by:** celery-ops

All rbx/shop tasks declare autoretry_for=[RBXException] only (e.g. rbx/tasks.py:51, 1239, 1272; shop/tasks.py:23). RBXException is raised only on non-200 responses; the far more common transient failures — requests.exceptions.ConnectionError/Timeout (CLI cold-start: per project memory the first request to the CLI fails and the validator registry takes ~30s to load) and json.JSONDecodeError — are NOT retried, so e.g. sync_topics or sync_master_nodes fails outright and waits up to 10 minutes for the next beat tick. Conversely, the one path where retries would matter most — sync_block — never gets them: it is invoked as a plain function call `sync_block(height)` (rbx/management/commands/sync_blocks.py:39) inside the sync_the_blocks task, so its autoretry/max_retries/retry_backoff (settings CELERY_TASK_ANNOTATIONS, project/settings/worker.py:13-18) are all dead config in production; any exception aborts the whole catch-up loop, and combined with the non-atomic sync_block this is the trigger for permanently skipped partial blocks. The wrapper tasks in project/celery.py (sync_the_blocks, update_vbtc_balances, etc.) declare no autoretry at all.

**Reviewer evidence:**

rbx/client.py raises RBXException only on `status_code != 200`; requests exceptions propagate raw. rbx/management/commands/sync_blocks.py:36-39 shows the synchronous call. project/settings/worker.py:13-18 annotations only affect .retry()/autoretry, which never engage on this path.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### vBTC v2 owner balance window: addresses property adds back withdrawals before the BTC chain sync reduces global_balance

**Severity:** medium · **Category:** correctness · **Location:** `rbx/models.py:1180` · **Found by:** architecture

VbtcV2Token.addresses computes owner balance = global_balance + total_withdrawn(COMPLETED) + net transfers, then subtracts each withdrawal from the requestor (models.py:1180-1199). This is only consistent when global_balance already reflects the BTC leaving the deposit address. But status flips to COMPLETED the moment the VFX completion tx is indexed (tasks.py:987-991), while global_balance is refreshed at most every 10 minutes by update_vbtc_balances — and blockchain.info's total_sent typically reflects only confirmed BTC txs (~10+ min). In that window the owner's displayed balance is overstated by the full withdrawal amount (the requestor is debited but the owner is credited with BTC that has already left). The reverse skew exists for non-owner withdrawals. This is derived-state money display whose correctness depends on the relative timing of two unsynchronized pipelines (VFX block sync on blocks-worker vs BTC polling on vbtc-worker). Also each serializer access re-runs 2+ queries per token; VbtcV2ListAllView serializes addresses for every token (api/btc/views.py:191-194), and VbtcV2ListView has an N+1 on transfer.token (views.py:207-208).

**Reviewer evidence:**

models.py:1183-1193 comment admits 'global_balance already reflects the BTC leaving the deposit address' — an assumption, not an invariant; tasks.py:993-994 comment defers balance update to 'the periodic BTC chain sync'; celery.py:29-31 schedules update_vbtc_balances every 10*60s.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### sc_identifier not unique on VbtcToken/VbtcV2Token; non-idempotent get-or-new pattern can create duplicates that then crash all processing via .get()

**Severity:** medium · **Category:** correctness · **Location:** `rbx/models.py:1130` · **Found by:** architecture

VbtcV2Token.sc_identifier (models.py:1130) and VbtcToken.sc_identifier (1063) are db_index but NOT unique, unlike FungibleToken (885, unique=True). All processing does try/get-except-DoesNotExist/create (tasks.py:453-457) with no DB constraint backstop. If a mint tx is processed twice concurrently — entirely plausible since reprocess_vbtc_v2/oct_2025_reprocess are run by hand while the blocks-worker processes live blocks, and there is no locking — two rows for the same sc_identifier are inserted. From then on, every `VbtcV2Token.objects.get(sc_identifier=...)` (10+ call sites: tasks.py:846, 908, 937, 972; views and serializers) raises MultipleObjectsReturned, which no caller catches (only DoesNotExist is handled), so processing of EVERY subsequent transfer/withdrawal for that token hard-fails and the token's V2 state freezes. Currently 0 duplicates on mainnet, but the only thing preventing this failure class is luck and low volume; a unique constraint is a one-line migration.

**Reviewer evidence:**

models.py:1130 `sc_identifier = models.CharField(max_length=64, db_index=True)` (no unique); tasks.py:453-457 except VbtcV2Token.DoesNotExist only; mainnet GROUP BY sc_identifier HAVING count>1 → 0 rows today.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._


### Destructive full-wipe operations registered as ordinary Celery tasks / hidden in sync paths

**Severity:** medium · **Category:** operational · **Location:** `rbx/tasks.py:154` · **Found by:** architecture

sync_block(0) executes `Address.objects.all().delete()` (tasks.py:154-155) — any reprocessing of genesis (sync_blocks --all, or a stray apply_async(args=[0])) wipes the production Address table whose balances cannot be correctly rebuilt by the also-registered resync_balances task (tasks.py:267-290 — it replays only raw amounts, omitting ADNR/callback/recovery rules, guaranteeing the 899-negative drift pattern). sync_adnrs deletes all Adnr rows then rebuilds from a live loop (tasks.py:1274-1285); sync_blocks --wipe deletes all Blocks/Callbacks/Recoveries (sync_blocks.py:19-27). None of these have confirmation, dry-run, or environment guards, and being @app.task means a single queued message triggers them in prod. The architecture conflates 'initial backfill', 'disaster repair', and 'steady-state sync' in the same functions, which is exactly how a routine reprocess turns into hours of API downtime serving empty balances while a rebuild runs (rebuild of 7.4M txs row-by-row with per-row save() would take many hours).

**Reviewer evidence:**

tasks.py:154-155 inside the hot sync path; tasks.py:267-290 resync_balances lacks the ADNR/callback rules present in get_balance; 7,429,601 transactions on mainnet would be replayed one ORM save at a time.

> ⚠️ _The verification agent for this finding never launched (the review workflow stalled first). The reviewer evidence above stands unreviewed._



## Low

### is_pending_withdrawal cleared unconditionally on any completion, even with other pending requests

**Severity:** low · **Category:** correctness · **Location:** `rbx/tasks.py:995` · **Found by:** vbtc-token-model

The VBTC_V2_WITHDRAWAL_COMPLETE handler sets `token.is_pending_withdrawal = False` without checking whether OTHER VbtcV2WithdrawalRequest rows for the same token are still in REQUESTED status. Failure scenario: requests A and B are both pending (the model and live data show multiple concurrent requests per token are normal — token 8 has had 4 requests); A completes → flag flips False while B is still outstanding → API consumers (serializer exposes is_pending_withdrawal) believe the token is free, and a second withdrawal/transfer flow can be initiated in the GUI against funds already committed to B. Also folded in: the completion lookup at tasks.py:978 fetches the withdrawal by request_transaction__hash alone without scoping to the token resolved from ContractUID, so the `token` whose flag gets cleared is taken from the completion tx's ContractUID while the withdrawal row could in principle belong to a different token — harmless today but an unvalidated cross-reference. Fix: `token.is_pending_withdrawal = token.withdrawal_requests.filter(status=Status.REQUESTED).exists()` after saving the completion, and filter the lookup by token.

**Reviewer evidence:**

tasks.py:977-996. Live data shows interleaving is real: token 7 had request id 3 completed 2026-05-29 18:12 and request id 4 created 18:15; token 8 has completed ids 5,6,7 plus pending id 8. If id 4 had been created before id 3 completed, the flag would now be wrong.

**Verification (high confidence):** The code-level claim is verified exactly as stated: rbx/tasks.py:995 sets is_pending_withdrawal = False unconditionally on any VBTC_V2_WITHDRAWAL_COMPLETE, and a repo-wide grep confirms nothing else recomputes the flag (only tasks.py:955 sets it True). The request handler at tasks.py:942-956 will happily create a second REQUESTED row for the same token with no guard, so the explorer's own data model permits concurrent pending requests. Live mainnet data makes the precondition realistic: token 8 has 4 requests from two DIFFERENT requestor addresses, proving independent wallets withdraw against the same token and could race. The unscoped lookup at tasks.py:978 (by request_transaction__hash only, FK not unique, no token filter) is also confirmed, though harmless given tx-hash uniqueness. However, the impact is smaller than the reviewer claims: (1) a flag-consistency SQL check across all 11 mainnet tokens shows zero corruption today — every interleaving has been sequential, so the bug has never fired in production; (2) completion typically lands 30-60s after request, so the race window is narrow; (3) the explorer is a read model — fund safety is enforced on-chain by validators, so a stale-False flag misleads the GUI/API token-selection (docs/butterfly-vbtc-v2/03-withdrawals.md:48 uses it as a hard filter) but cannot itself release committed funds. The suggested fix (recompute via withdrawal_requests.filter(status=REQUESTED).exists()) is correct and cheap. Severity adjusted from medium to low: real latent logic bug, never manifested, chain-backstopped impact.

**Verification evidence:**

rbx/tasks.py:993-996: "# Balance fields (global_balance, total_sent) are updated by the / # periodic BTC chain sync (update_vbtc_balances), not here. / token.is_pending_withdrawal = False / token.save()" — unconditional, and grep shows the only other writer is tasks.py:955 (set True). Live mainnet SQL (SELECT id, token_id, requestor_address, status, created_at, completed_at FROM rbx_vbtcv2withdrawalrequest ORDER BY id) returned 8 rows: token 8 has requests id 5,6,7 (completed, requestor RNiQrW3aBUWZhfadqKxPuN46iGaR13ox7P) and id 8 (requested, DIFFERENT requestor RPKxShZhfytjfffG8sQZG5ZTj9qqcAPgvJ) — multi-requestor concurrency is real. But the overlap check shows no completion ever fell inside another request's pending window, and a per-token consistency query (pending_count vs is_pending_withdrawal across all 11 tokens) shows the flag is currently correct everywhere — the bug is latent, never triggered.


### V1 vBTC transfer processing is not idempotent (plain save, no get_or_create)

**Severity:** low · **Category:** correctness · **Location:** `rbx/tasks.py:824` · **Found by:** vbtc-token-model

Commit 727978b fixed duplicate-record creation for V2 transfers and withdrawals by switching to get_or_create, but the V1 paths were left as plain constructor + save(): TransferCoin() at tasks.py:824-831 and TransferCoinMulti() at tasks.py:879-887 both create a new VbtcTokenAmountTransfer every time process_transaction runs for the same tx. Any block re-processing/resync (which evidently happened for V2 — that is exactly how the V2 duplicates at transfer ids 1/4 and 2/5 were created) will duplicate V1 transfer rows, and VbtcToken.addresses (rbx/models.py:1094-1113) sums them without dedup, double-counting recipient balances. Honorable mentions on the same V1 code: tasks.py:870 parses input['FromAddress'] for multi-transfers but never uses it (the transfer is debited to tx.from_address via the addresses property), and V1 addresses applies no negative filtering at all, so it exposes raw negative balances the V2 property hides.

**Reviewer evidence:**

tasks.py:824-831 and 879-887 use VbtcTokenAmountTransfer(...).save() with no existence check, in the same process_transaction function where the V2 branches (lines 913, 942) use get_or_create — the asymmetry is the bug. The V2 duplicates in production (rbx_vbtcv2tokentransfer ids 1/4, 2/5) prove that this code path does get re-executed for the same transaction in production.

**Verification (high confidence):** The code defect is exactly as claimed: in rbx/tasks.py process_transaction, the V1 TKNZ_TX branches TransferCoin() (~lines 824-831) and TransferCoinMulti() (~878-886) create VbtcTokenAmountTransfer with plain constructor + save(), while the V2 branches in the same function use get_or_create (added in commit 727978b, which explicitly fixed reprocessing duplicates but only for V2). VbtcTokenAmountTransfer has no unique constraint on (token, transaction), so nothing at the DB level prevents duplicates, and VbtcToken.addresses (models.py ~1095-1113) sums transfers with no dedup, so duplicates would double-count balances. The honorable mentions also check out (unused from_address at ~870; no negative filtering in V1 addresses). HOWEVER, the claimed real-world impact is overstated: (1) the reviewer's suggested duplicate query returns 0 rows on mainnet (117 V1 transfers total) and testnet has 0 V1 transfers at all — no duplication has ever occurred; (2) the mechanism that created the V2 duplicates (ids 1/4, 2/5, still present on mainnet) was the reprocess_vbtc_v2 management command, which filters to types 25-28 and can never re-run the V1 TKNZ_TX path; (3) no other current code path re-executes process_transaction for an existing TKNZ_TX row — normal sync_blocks starts at local_max_height+1, validate_transactions --fix deletes Transaction rows first (CASCADE wipes the transfer rows), nft_fixer covers NFT_MINT only, oct_2025_reprocess covers NFT_MINT/TKNZ_MINT only. So this is a real latent non-idempotency (and the team has twice written ad-hoc reprocess commands, so a future TKNZ_TX reprocess is plausible), but there is no active duplication and no current trigger. Severity adjusted from medium to low.

**Verification evidence:**

Code (rbx/tasks.py, TransferCoin() branch): "transfer = VbtcTokenAmountTransfer(token=token, transaction=tx, address=tx.to_address, amount=amount, created_at=tx.date_crafted,) / transfer.save()" — plain save, vs the V2 branch in the same function: "VbtcV2TokenTransfer.objects.get_or_create(token=token, transaction=tx, defaults={...})". Refuting live data (mainnet): SELECT transaction_id, token_id, COUNT(*) FROM rbx_vbtctokenamounttransfer GROUP BY transaction_id, token_id HAVING COUNT(*) > 1 LIMIT 10 → 0 rows (117 total rows); testnet → table has 0 rows. Reviewer's V2-duplicate premise confirmed (mainnet rbx_vbtcv2tokentransfer ids 1/4 share transaction dd9d567a..., ids 2/5 share 9fe25812...), but the cause was rbx/management/commands/reprocess_vbtc_v2.py whose VBTC_V2_TYPES = [VBTC_V2_MINT, VBTC_V2_TRANSFER, VBTC_V2_WITHDRAWAL_REQUEST, VBTC_V2_WITHDRAWAL_COMPLETE] — it never touches TKNZ_TX, so that reprocessing vector cannot duplicate V1 rows.


### Input validation only checks truthiness — negative amounts and zero pass/are mis-handled

**Severity:** low · **Category:** correctness · **Location:** `api/btc/views.py:273` · **Found by:** vbtc-api-security

_require_fields uses `if not data.get(field)`, which only rejects falsy values. For transfer (views.py:407) and withdrawal request (views.py:441) this means: (a) a legitimate amount of 0 is rejected as 'amount required' (wrong error), and (b) negative amounts, non-numeric strings, and absurdly large values pass the proxy unchecked and are forwarded to the CLI. There is no numeric/range/format validation of amount, fee_rate, addresses, or sc_identifier anywhere in the proxy — it relies entirely on the CLI to reject them. A negative amount or malformed address that the CLI handles loosely could produce unexpected ledger effects.

**Reviewer evidence:**

views.py:273-278 truthiness check; used by transfer (407), withdraw request (441), create (366), complete-tx (599).

**Verification (high confidence):** The factual claims are fully confirmed in code: _require_fields (api/btc/views.py:273-278) is truthiness-only, so amount=0/fee_rate=0 get a misleading 400 'X required', and negative/non-numeric/oversized values pass straight through. Transfer prepare (views.py:407, 411-417) and withdrawal request prepare (views.py:441, 445-451) build payloads directly from request.data, and rbx/client.py:1065-1079 (_vbtc_v2_request) posts them to the CLI verbatim — no serializer, permission class, or any other guard exists anywhere in the path (the only serializer_class declarations in the file are on read-only views at lines 176/222). However, the claimed impact is overstated. These are prepare-raw-TX endpoints whose output is unsigned TX data requiring the owner's private-key signature; ledger acceptance is gated by the CLI and VFX network consensus, not the proxy — the entire file is a thin passthrough that relies on CLI validation by design. The 'unexpected ledger effects' scenario requires a CLI/consensus validation bug for which there is no evidence (no Sentry signature, no DB anomaly cited). amount=0 is not a legitimate transfer/withdrawal amount, so rejecting it is correct in effect; only the error message is wrong. Realistic impact: misleading 400 messages and garbage forwarded to the CLI, whose error the proxy already wraps as a 500. That is a defense-in-depth/UX gap, so severity is adjusted from medium to low. I did not run the suggested live amount=-1 test: the only documented CLI host is mainnet and GetRawTransferVBTCData cannot be guaranteed non-state-changing, so the rule against state-changing production requests applies; the code evidence is decisive on the facts regardless.

**Verification evidence:**

api/btc/views.py:273-278: def _require_fields(data, fields): """Validate required fields, return error Response or None.""" for field in fields: if not data.get(field): return Response({"success": False, "message": f"{field} required"}, status=400) — combined with views.py:411-417 building the CLI payload directly from request.data ("Amount": request.data["amount"]) and rbx/client.py:1065-1079 _vbtc_v2_request doing requests.post(url, json=payload) with no validation. No serializer_class or permission_classes on any vBTC v2 POST view (grep shows serializer_class only at lines 176 and 222, both GET views).


### WithdrawVbtcView passes raw request body to the BTC withdrawal CLI and has a crashing error path

**Severity:** low · **Category:** correctness · **Location:** `api/raw/views.py:185` · **Found by:** vbtc-api-security

WithdrawVbtcView.post forwards request.data verbatim to client.withdraw_btc (which POSTs to btcapi/btcv2/WithdrawalCoinRawTX) with zero field validation or signature check at the proxy layer — this v1 withdrawal path is public (AllowAny) and entirely trusts the CLI. Additionally the failure branch returns Response({'success': False, result: None}) — it uses the variable `result` as a dict KEY. On failure `result` is falsy: if it is an empty dict {} this raises TypeError (unhashable type) yielding an opaque 500 with a traceback; if None it silently builds {None: None}. The intended key was almost certainly the string 'result'.

**Reviewer evidence:**

raw/views.py:188-200; client.withdraw_btc at rbx/client.py:1046-1059 posts payload straight to CLI.

**Verification (high confidence):** Both code claims are verbatim accurate. api/raw/views.py:197-200 returns Response({'success': False, result: None}) using the local variable `result` as the dict key; in that branch `result` is falsy, so result=={} raises TypeError (unhashable type: 'dict') producing a 500, and result==None builds {None: None}. The view forwards request.data with no validation to client.withdraw_btc (rbx/client.py:1046-1059), which POSTs it straight to the CLI's btcapi/btcv2/WithdrawalCoinRawTX. The endpoint is routed at api/raw/urls.py:53 and is effectively public: no permission_classes on the view, and .env sets API_AUTH_REQUIRED=False so DRF default is AllowAny (anon throttle 300-1000/min). However, the impact is smaller than claimed: (1) this is the V1 legacy withdrawal path — docs/vfx-web-sdk-vbtc-v2-integration.md:353 explicitly lists /raw/withdraw-vbtc/ under "What to Remove"; live V2 withdrawals use dedicated api/btc views; (2) all /raw/ endpoints are intentionally thin unauthenticated proxies — WithdrawalCoinRawTX builds a raw tx that still requires the owner's signature, so the missing proxy-layer validation does not enable unauthorized fund movement; (3) the typo only matters in the narrow case where the CLI returns falsy JSON ({} or null) — exceptions (connection errors, non-JSON) already propagate as 500s — so the client sees a 500 either way; the bug just swaps a clean error body for a TypeError. Real bug, but severity low: a one-character fix ('result') on a deprecated endpoint whose failure mode is already a 500.

**Verification evidence:**

api/raw/views.py:197-200:
        return Response(
            {"success": False, result: None},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
— variable `result` used as dict key, confirmed verbatim. Supporting: rbx/client.py:1055 `response = requests.post(url, json=payload)` with no validation; .env:66 `API_AUTH_REQUIRED=False` (AllowAny default per project/settings/api.py:15-19); docs/vfx-web-sdk-vbtc-v2-integration.md:353 marks `/raw/withdraw-vbtc/` as the V1 path slated for removal.


### TKNZ_TX ownership Transfer() trusts tx.to_address with no current-owner check and swallows NFT sync failures

**Severity:** low · **Category:** correctness · **Location:** `rbx/tasks.py:846` · **Found by:** vbtc-data-invariants

The V2 ownership-transfer branch (commit 3d83402) sets v2_token.owner_address = tx.to_address without verifying tx.from_address equals the token's current owner_address, and wraps the companion NFT owner update in a bare 'except Exception: pass' (tasks.py:850-855). Failure scenarios: (1) if the explorer ever ingests a TKNZ_TX Transfer() that chain consensus rejected late, or processes ownership transfers out of order during a resync (reprocess_vbtc_v2 orders by height but TKNZ_TX type 18 is NOT in its VBTC_V2_TYPES list, so a full-chain resync is the only way to replay it and ordering with other writers is unguarded), the owner silently flips to a wrong address with no log; (2) when the NFT update fails, token.owner_address and nft.owner_address diverge permanently and invisibly — VbtcV2ListView keys off token.owner_address while NFT-based views key off nft.owner_address, so the same asset appears owned by two different addresses. Currently consistent on mainnet (0 mismatched rows), but the only ownership transfer so far (token 7) was applied retroactively, and the except-pass guarantees future divergence will be silent.

**Reviewer evidence:**

rbx/tasks.py:844-856: no 'from_address == token.owner_address' guard; bare 'except Exception: pass'. Mainnet consistency check currently passes: SELECT v.id FROM rbx_vbtcv2token v JOIN rbx_nft n ON n.identifier = v.nft_id WHERE v.owner_address <> n.owner_address → 0 rows. reprocess_vbtc_v2.py:6-11 omits type 18.

> _Not adversarially verified (below the verification threshold by design)._


### V2 ownership transfer leaves no transfer record and swallows NFT-owner update failures

**Severity:** low · **Category:** correctness · **Location:** `rbx/tasks.py:850` · **Found by:** vbtc-tknz-processing

The TKNZ_TX Transfer() V2 path (commit 3d83402) updates v2_token.owner_address and nft.owner_address but, unlike the V1 NFT path (nft.transfer_transactions.add(tx), line 497) and the V2 amount-transfer path (VbtcV2TokenTransfer row), records nothing: no transfer event row, no link from the tx to the token/NFT. The explorer therefore cannot show ownership history, and a re-derivation of ownership from recorded events (e.g. after restoring from the reprocess command) has no source of truth — compounding finding 3. The inner `except Exception: pass` around the NFT owner update means a failure there silently leaves VbtcV2Token.owner_address and Nft.owner_address pointing at different parties (token owned by B, NFT still owned by A), with zero log output. Current mainnet state is consistent (both rows say RPKx for token d11a9ef3), but only because the happy path ran.

**Reviewer evidence:**

tasks.py:844-859 (no event row created, bare except Exception: pass); contrast with line 497 (NFT_TX adds transfer_transactions) and line 913 (V2 amount transfer creates a row). Mainnet: rbx_nft.owner_address = rbx_vbtcv2token.owner_address = RPKx... for d11a9ef3...:1779979897.

> _Not adversarially verified (below the verification threshold by design)._


### V1 TKNZ_TX coin-transfer paths are non-idempotent and TransferCoinMulti swallows all exceptions

**Severity:** low · **Category:** correctness · **Location:** `rbx/tasks.py:824` · **Found by:** vbtc-tknz-processing

TransferCoin() (line 824) and TransferCoinMulti() (line 879) create VbtcTokenAmountTransfer rows with plain constructor+save — no get_or_create, no unique constraint — so any reprocessing of type-18 transactions (the obvious next step after finding 3, since type 18 carries V2 ownership transfers and is missing from reprocess_vbtc_v2) double-counts every V1 coin transfer in VbtcToken.addresses (models.py:1095-1113). This is exactly the mechanism that produced the live V2 duplicates in finding 4 before commit 727978b, and the V1 path was never given the same fix. Additionally, TransferCoinMulti wraps each input in `except Exception as e: print('ERROR'); print(e)` (lines 888-890), so a malformed input is dropped with only a container-stdout print — no Sentry, no retry — leaving partial multi-transfer state (some inputs recorded, others silently missing).

**Reviewer evidence:**

tasks.py:824-831 (plain save), 861-890 (broad except with print). git show 727978b touched only VbtcV2TokenTransfer/VbtcV2WithdrawalRequest get_or_create. Mainnet precedent: duplicate V2 rows ids 1/4 and 2/5 created by pre-fix reprocessing.

> _Not adversarially verified (below the verification threshold by design)._


### update_vbtc_balances scales linearly with token count inside a 10-minute beat interval

**Severity:** low · **Category:** operational · **Location:** `btc/management/commands/update_vbtc_balances.py:18` · **Found by:** vbtc-token-model

The beat task (project/celery.py:30, every 600s) shells into a management command that iterates VbtcV2Token.objects.all() with a 0.5s sleep and one blockchain.info request per token. With 11 tokens today that's ~10s, but the butterfly architecture is per-user tokens (lazy minting), so token count grows with users; at ~600+ tokens the run exceeds the 10-minute interval and overlapping invocations stack while hammering an unauthenticated rate-limited API. Failures are silent: get_balance returns None on any error and the token is skipped with no logging or metrics, so under sustained rate-limiting all balances quietly go stale — which directly feeds the addresses inflation window described in the withdrawal-sync finding. Also `total=len(tokens)` forces a full list materialization instead of .count(), trivial today but same scaling concern.

**Reviewer evidence:**

btc/management/commands/update_vbtc_balances.py:18-30; project/celery.py:30 (10*60 schedule) and :102-105 (synchronous call_command). Mainnet currently has 11 rows in rbx_vbtcv2token. btc/btc_client.py:48-50 returns None on error with only a log line in the command's silent `if balance_info:` skip.

> _Not adversarially verified (below the verification threshold by design)._


### BtcTx.timestamp is assigned the transaction hash

**Severity:** low · **Category:** correctness · **Location:** `btc/models.py:45` · **Found by:** vbtc-token-model

BtcTx.__init__ sets `self.timestamp = data["transactionHash"]` instead of data["timestamp"]. Every BTC transaction serialized through BtcAddressView (api/btc/views.py:65-75, via BtcExplorerClient.get_confirmed_transactions) returns the tx hash string in the 'timestamp' field, so wallet/GUI consumers rendering transaction times for BTC addresses get garbage. Copy-paste bug from the line above; minor related note: senders are constructed with the BtcTxRecipient class (line 44), harmless since both subclasses are identical.

**Reviewer evidence:**

btc/models.py:42-45: `self.hash = data["transactionHash"]` followed by `self.timestamp = data["transactionHash"]` while the class annotates `timestamp: int` (line 35) and Utxo correctly uses data["timestamp"] (line 79).

> _Not adversarially verified (below the verification threshold by design)._


### Signatures and signed messages are logged and passed in URL paths

**Severity:** low · **Category:** security · **Location:** `rbx/client.py:1069` · **Found by:** vbtc-api-security

_vbtc_v2_request and beacon helpers log the full outbound URL (logger.info VBTC_V2: ... url), and several routes carry the signature in the URL path itself: api/raw/urls.py:28 (validate-signature/<message>/<address>/<path:signature>) and api/btc/urls.py:68 (beacon-upload/.../<path:signature>). Signatures embedded in URLs land in application logs, proxy/access logs, and any intermediary, where they are retained and may be broadly readable. Because the beacon-upload signature authorizes an ownership transfer to a specific to_address, leaking it is lower-impact than a bare signing key, but it is still sensitive auth material that should be in the request body, not the path/logs.

**Reviewer evidence:**

rbx/client.py:1068-1069 logs url; rbx/client.py:1219-1227 beacon upload builds URL with signature; raw/urls.py:28 and btc/urls.py:68 route signatures via <path:signature>.

> _Not adversarially verified (below the verification threshold by design)._


### FROST signed BTC tx hex retrievable by job_id alone; broadcast endpoint is an open relay

**Severity:** low · **Category:** security · **Location:** `api/btc/views.py:558` · **Found by:** vbtc-api-security

VbtcV2WithdrawCompleteStatusView returns the fully signed BTC transaction hex to anyone presenting the job_id, with no ownership check; the only protection is that job_id is a uuid4 (122 bits, not practically brute-forceable) and the result is deleted on first read. This is acceptable in isolation, but combined with BtcBroadcastView (views.py:711-732), which broadcasts any client-supplied raw_tx_hex with no auth, the server acts as an open BTC broadcast relay. Neither is a direct theft vector (signed txs and BTC broadcasting are inherently public), but the status endpoint should still scope the result to the requesting owner, and the job cache key should not be the sole gate on returning a signed transaction.

**Reviewer evidence:**

views.py:560-583 returns signed_btc_tx_hex on job_id only; views.py:711-732 BtcBroadcastView broadcasts arbitrary hex unauthenticated.

> _Not adversarially verified (below the verification threshold by design)._


### address_permission token check: wrong auth scheme handling, non-constant-time compare, and info-leak prints

**Severity:** low · **Category:** security · **Location:** `api/permissions.py:20` · **Found by:** web-api

address_permission strips only a lowercase 'basic ' prefix (token_value = authorization.replace('basic ', '')) — inconsistent with DRF's TokenAuthentication 'Token ' scheme and case-sensitive, so a correctly-cased or differently-cased header silently fails or leaves the scheme word embedded. It looks AuthToken up by exact equality (no constant-time compare, minor) and prints 'Token not found'/'Token invalid'/'Incorrect address' to stdout on every failure (log noise + reveals which check failed). Only email-subscribe relies on this, so blast radius is small, but the bound-to-address gate is the single real authz check in the layer and should be hardened.

**Reviewer evidence:**

api/permissions.py:20 replace('basic ', ''); :25,29,33 print() debug lines; :23 AuthToken.objects.get(token=token_value).

> _Not adversarially verified (below the verification threshold by design)._


### MasterNode list/map pages serialize up to 15,000 rows each with geo data

**Severity:** low · **Category:** operational · **Location:** `api/pagination.py:23` · **Found by:** web-api

MasterNodePagination sets page_size=max_page_size=15000. MasterNodeListView and MasterNodeMapView use it, so a single page serializes up to 15k masternode records (live rbx_masternode=29,191 rows -> ~2 max pages) including map/geo fields. Cached short (DEFAULT/SHORT), but each cache miss serializes a very large payload in one response, increasing memory and latency. Lower priority because it is cached and bounded, but the page size defeats the purpose of pagination.

**Reviewer evidence:**

api/pagination.py:23-25 MasterNodePagination page_size=15000; api/master_node/views.py MasterNodeListView/MapView use it. Live rbx_masternode=29,191.

> _Not adversarially verified (below the verification threshold by design)._


### FungibleToken detail computes holder balances with per-address queries (N+1) inside the request

**Severity:** low · **Category:** operational · **Location:** `api/fungible_token/views.py:33` · **Found by:** web-api

FungibleTokenRetrieveView.get gathers every distinct sending/receiving address from FungibleTokenTx for the token, then calls token.get_address_balance(address) in a Python loop — one (or more) query per holder, synchronously, with no pagination and no cache. Today FungibleTokenTx is tiny (1,175 rows total mainnet) so impact is negligible, but this scales linearly with token adoption and will become a slow uncached endpoint as fungible tokens grow; flagging now since vBTC v2 / token usage is expanding.

**Reviewer evidence:**

api/fungible_token/views.py:33-60 builds addresses set then loops holders[address]=token.get_address_balance(address). Live rbx_fungibletokentx=1,175 rows (low today).

> _Not adversarially verified (below the verification threshold by design)._


### client.py has no handling for the CLI cold-start quirk (first request fails, ~30s validator-registry load)

**Severity:** low · **Category:** operational · **Location:** `rbx/client.py:33` · **Found by:** block-sync

Documented CLI behavior: the first request after a cold start fails and the validator registry takes ~30s to load. No function in client.py implements a warm-up, retry-once, or backoff for this (only get_nft and shop ping_check have ad-hoc retries; everything else raises RBXException or returns None on the first failure). Consequences: (a) block sync self-heals via the 10s beat, but each CLI restart deterministically produces one failed sync_the_blocks run and, if it happens mid-range, can trigger the partial-block scenario; (b) health_check fires a false 'Explorer Wallet is Unreachable' SMS; (c) user-facing endpoints that proxy the CLI (tx_send, validate_signature, vBTC v2 _vbtc_v2_request which converts any exception into {'Success': False}) return hard failures to wallets for the first request(s) after every CLI deploy/restart. A single shared retry-on-first-failure wrapper would eliminate the whole class.

**Reviewer evidence:**

client.py: get_status/get_info/get_block raise RBXException immediately on non-200 (lines 33-79); _vbtc_v2_request (1065-1078) catches everything and returns Success:False with no retry. Memory note 'project_cli_cold_start.md' documents the quirk.

> _Not adversarially verified (below the verification threshold by design)._


### Address balance accounting bug: `to_address != "Adnr_Base"` compares a model instance to a string and is always True

**Severity:** low · **Category:** correctness · **Location:** `rbx/tasks.py:223` · **Found by:** block-sync

In sync_block's balance section, to_address is an Address model instance (from get_or_create at line 219), so `to_address != "Adnr_Base"` is always True (Django Model.__eq__ against a str is False). The guard intended to exclude the Adnr_Base burn address from the 5-RBX (or 1-RBX pre-832000) ADNR deduction is dead code: the deduction applies unconditionally to every Type.ADDRESS tx recipient, including Adnr_Base itself. Explorer-tracked balance for Adnr_Base (currently 541 on mainnet) is therefore skewed by 5/1 RBX per ADNR tx relative to whatever the intended semantics were; any other intended-exempt recipient is similarly mis-deducted. Should be `tx.to_address != "Adnr_Base"`.

**Reviewer evidence:**

tasks.py:219-231: `to_address, _ = Address.objects.get_or_create(...)` then `if tx.type == Transaction.Type.ADDRESS and to_address != "Adnr_Base":` — instance-vs-str comparison. Mainnet: SELECT balance FROM rbx_address WHERE address='Adnr_Base' → 541.0.

> _Not adversarially verified (below the verification threshold by design)._


### TKNZ/FTKN handlers swallow errors and create rows non-idempotently, so reprocessing duplicates transfer records

**Severity:** low · **Category:** correctness · **Location:** `rbx/tasks.py:888` · **Found by:** block-sync

TransferCoinMulti() wraps each input in a bare `except Exception: print("ERROR")` (tasks.py:888-890), so malformed inputs are dropped with no Sentry signal and no record — a vBTC amount transfer can vanish from the explorer silently. Separately, VbtcTokenAmountTransfer (lines 824-831, 879-887) and FungibleTokenTx (711-735) use plain .save()/.create() with no uniqueness key on (transaction, token), unlike the V2 handlers which correctly use get_or_create keyed on (token, transaction) (lines 913, 942). Any reprocessing path — process_transaction re-run via reprocess_vbtc_v2/oct_2025_reprocess-style management commands, or a partially-failed block re-driven manually — creates duplicate transfer rows, double-counting token movement in wallet histories and balance computations derived from these tables. The newer V2 code shows the team knows the right pattern; the V1/FTKN paths were never brought up to it.

**Reviewer evidence:**

tasks.py:888-890 bare except printing 'ERROR'; tasks.py:824-831 and 727-735 unconditional saves vs tasks.py:913-922 and 942-953 get_or_create. No unique_together on VbtcTokenAmountTransfer/FungibleTokenTx in rbx/models.py.

> _Not adversarially verified (below the verification threshold by design)._


### Production workers run at --loglevel=DEBUG, and client code logs sensitive payloads

**Severity:** low · **Category:** security · **Location:** `porter.yaml:9` · **Found by:** celery-ops

All four Celery services (default-worker, blocks-worker, vbtc-worker, runner/beat) run with --loglevel=DEBUG in production (porter.yaml:9,12,15,18). Celery DEBUG logs every task body and result, and rbx/client.py deliberately logs full request/response payloads at info/debug/error level — e.g. withdraw_btc logs the entire withdrawal payload and response (rbx/client.py:1050-1058), beacon_upload_request logs URL+signature at logger.error (:353-355), get_address_nonce logs responses (:391). Failure scenario: signatures, proofs, and withdrawal payloads land in Porter log aggregation where anyone with log access can read them (signatures are single-use on-chain but proofs/payload structure aid attackers), and DEBUG volume inflates log costs and buries real errors — directly degrading the log-investigator workflow this project relies on.

**Reviewer evidence:**

porter.yaml lines 9/12/15/18 all end in `--loglevel=DEBUG`; rbx/client.py:1050 `logger.info(f"PAYLOAD: {json.dumps(payload)}")` inside withdraw_btc.

> _Not adversarially verified (below the verification threshold by design)._


### watch_worker autoreload never kills the old worker: pkill pattern does not match the actual command line

**Severity:** low · **Category:** correctness · **Location:** `rbx/management/commands/watch_worker.py:10` · **Found by:** celery-ops

restart_celery runs `pkill -f "celery worker"` but then starts the worker as `celery --app=project worker ...` — the process command line contains 'celery --app=project worker', not the adjacent substring 'celery worker', so pkill -f never matches. On every code change the autoreloader spawns an additional worker while old ones keep running; after a dev session you have N workers consuming the same queues, causing duplicate task execution and confusing local debugging of exactly the serialization behaviors (blocks/vbtc concurrency=1) this topology depends on. Dev-only (guarded by settings.DEBUG at line 18), so low severity. Related dev-tooling rot: Makefile target `celery` (Makefile:58) uses `-A config`, which is not this project's app module (`project`), so it fails outright.

**Reviewer evidence:**

watch_worker.py:10-12: pattern 'celery worker' vs spawned cmd 'celery --app=project worker --without-heartbeat ...'.

> _Not adversarially verified (below the verification threshold by design)._



## Architecture observations

_Assessments, not bugs — not adversarially verified._

### process_transaction: 675-line if/elif dispatcher with no handlers for 6 transaction types already live on mainnet

**Severity:** high · **Category:** architecture · **Location:** `rbx/tasks.py:322` · **Found by:** architecture

All domain logic for every transaction type lives in one 675-line function (tasks.py:322-996) inside the tasks module. Adding a type means editing this monolith; the enum already proves the lag: types 29 (VBTC_V2_WITHDRAWAL_CANCEL), 30, and 31-38 (VFX_SHIELD/UNSHIELD/PRIVATE_TRANSFER, VBTC shield/bridge) are declared in rbx/models.py:177-186 and 13 such transactions exist on mainnet TODAY (types 31:2, 32:1, 33:4, 34:1, 35:2, 37:3), but process_transaction falls through silently for all of them — no logging, no record, and the generic balance arithmetic in sync_block is still applied to them whether or not that is correct for shield/bridge semantics. Concrete consequence for vBTC v2: the API ships withdraw/cancel endpoints (api/btc/urls.py:66-67) but there is no type-29 handler, so the first on-chain cancel will leave VbtcV2WithdrawalRequest stuck in 'requested' and token.is_pending_withdrawal=True forever (set at tasks.py:955, only cleared by type-28 complete at line 995). Two mainnet requests (ids 4, 8) have already sat in 'requested' since 2026-05-29. A vBTC v3 would mean a third copy of the model/serializer/view/icon-task/feature-branch stack (VbtcToken vs VbtcV2Token are near-identical parallel hierarchies: models.py:1062 vs 1129, handle_vbtc_icon_upload vs handle_vbtc_v2_icon_upload tasks.py:1389/1419, FeatureName 3 vs 14 branches tasks.py:427/451, try-V1-then-V2 fallback in TKNZ_TX Transfer() tasks.py:833-859).

**Reviewer evidence:**

Mainnet: SELECT type, count(*) FROM rbx_transaction WHERE type >= 25 GROUP BY type → rows for 31,32,33,34,35,37 totaling 13 txs with no handler in process_transaction. Withdrawals: ids 8 and 4 status='requested', created 2026-05-29, token.is_pending_withdrawal=true.


### Zero automated tests in the entire repository — money-math iterated directly against production

**Severity:** high · **Category:** architecture · **Location:** `btc/tests.py:1` · **Found by:** architecture

Every tests.py is a 3-line Django stub; `grep -rl 'def test'` over the repo (excluding venv) returns nothing. The riskiest logic — process_transaction dispatch, Address.get_balance (150 lines of ledger math), VbtcV2Token.addresses (per-address BTC attribution incl. withdrawal compensation) — is untestable as structured: it lives in task functions and model properties with synchronous external calls embedded (get_nft inside the mint branch, BtcClient inside views), so there is no seam to test the pure logic. The cost is visible in git history: VbtcV2Token.addresses semantics were rewritten three times in three consecutive commits (0732472, eb3d1a6, 3083c0e) — each iteration validated only by observing production mainnet. Concrete consequence: any regression in transfer/withdrawal balance attribution (the numbers wallets display for users' BTC) ships undetected and is only caught by users or manual SQL.

**Reviewer evidence:**

find . -name 'tests.py' → btc/tests.py, payment/tests.py, shop/tests.py, price/tests.py, all 3 lines; no test_*.py anywhere; no rbx/tests.py or api tests at all. Recent commits 0732472/eb3d1a6/3083c0e all rework the same addresses property.


### vBTC mutations are not routed through the single-concurrency vbtc-worker; sync_blocks --async fans block processing onto the parallel default queue

**Severity:** medium · **Category:** architecture · **Location:** `project/celery.py:49` · **Found by:** vbtc-tknz-processing

All TKNZ_TX / vBTC V2 state mutations execute inside process_transaction, which runs inline in sync_block on the blocks-worker (sync_the_blocks → call_command('sync_blocks') → sync_block(height) synchronously, sync_blocks.py:39). The vbtc-worker (concurrency=1) only runs update_vbtc_balances — so the 'vBTC mutations are serialized on vbtc-worker' assumption is false: token rows are written from blocks-worker, vbtc-worker, gunicorn web (VbtcV2DetailView), and ad-hoc management commands simultaneously (the enabling condition for the lost-update race in finding 2). Worse, sync_block itself declares no queue (rbx/tasks.py:146-147), so `manage.py sync_blocks --async` (sync_blocks.py:36-37) dispatches every height to the DEFAULT queue, where default-worker processes blocks in parallel and out of order: a V2 mint at height H and its TKNZ_TX ownership transfer at H+k can process transfer-before-mint (transfer silently dropped per the DoesNotExist guard), and the per-address balance arithmetic in sync_block interleaves non-atomically. Anyone running --async to catch up after downtime corrupts ordering guarantees that the rest of the pipeline assumes.

**Reviewer evidence:**

project/celery.py:49-53 (blocks_queue task calls command synchronously); rbx/management/commands/sync_blocks.py:35-39 (--async → sync_block.apply_async with no queue, falls to task_default_queue='default', celery.py:16); rbx/tasks.py:146 @app.task(autoretry_for=[RBXException]) with no queue= on sync_block; porter service table: vbtc-worker concurrency=1, blocks-worker concurrency=1, default-worker not concurrency-limited.


### fix_vbtc duplicates 120 lines of process_transaction mint logic and has already drifted (no V2 support, loop-aborting return)

**Severity:** medium · **Category:** architecture · **Location:** `rbx/management/commands/fix_vbtc.py:117` · **Found by:** architecture

fix_vbtc.py reimplements the TKNZ_MINT branch of process_transaction by copy-paste (NFT creation + FeatureName 13 + FeatureName 3) instead of calling it like reprocess_vbtc_v2 and oct_2025_reprocess do. It has already drifted: it lacks the FeatureName 14 (vBTC V2) branch added in tasks.py:451-477, so running it on a TKNZ_MINT that carries a V2 feature creates the Nft but silently never creates the VbtcV2Token — a 'repair' tool that corrupts V2 state by half-processing. It also uses `return` instead of `continue` at lines 37 and 46: the first transaction whose SC data is unavailable from the CLI aborts the entire repair run, skipping all remaining transactions without any error summary. Underlying driver: process_transaction mint handling depends on a live synchronous get_nft() CLI call (tasks.py:334-339) that silently drops the token when the CLI lacks data (e.g., the documented ~30s cold-start registry load), which is why this family of after-the-fact repair commands keeps multiplying (fix_vbtc, reprocess_vbtc_v2, oct_2025_reprocess, nft_fixer, resync_balances, sale_complete_balance_fix...). Each new one is another drift surface.

**Reviewer evidence:**

fix_vbtc.py:34-37 `if not data: ... return` inside the for-loop; fix_vbtc.py:117-146 handles only FeatureName 3; tasks.py:451-477 handles FeatureName 14. reprocess_vbtc_v2.py:59 correctly reuses process_transaction. 13 VBTC_V2_MINT txs vs 11 VbtcV2Token rows on mainnet (2 mints did not yield tokens — consistent with silent drop or re-mints).


### VbtcV2DetailView mutates token balance on GET via a third-party API call in the web process

**Severity:** medium · **Category:** architecture · **Location:** `api/btc/views.py:226` · **Found by:** architecture

VbtcV2DetailView.get() (views.py:226-236) calls blockchain.info synchronously (5-10s timeouts in BtcClient) and writes global_balance/total_received/total_sent/tx_count to the DB on every GET. This creates: (1) a second writer for fields the vbtc-worker's update_vbtc_balances also owns, with full-object token.save() (not update_fields) so concurrent saves can clobber other fields (e.g., is_pending_withdrawal being toggled by TKNZ processing on the blocks-worker — last-writer-wins on the whole row); (2) request latency and gunicorn worker occupancy coupled to a rate-limited third-party (blockchain.info 429s make the wallet detail page slow or stale with only a logger.error trace, BtcClient.get_balance returns None silently); (3) an unauthenticated-or-token-gated cache-busting path where polling clients hammer blockchain.info from the web tier. Detail GETs should read indexed state; freshness belongs to the worker pipeline alone.

**Reviewer evidence:**

api/btc/views.py:228-235: `client.get_balance(...); token.global_balance = ...; token.save()` inside RetrieveAPIView.get. btc/btc_client.py:40-50 returns None on any error. celery.py:101-105 schedules the same write from vbtc_queue.


### update_vbtc_balances scales O(tokens) with serial HTTP + sleep — collides with butterfly per-user-token architecture

**Severity:** medium · **Category:** architecture · **Location:** `btc/management/commands/update_vbtc_balances.py:18` · **Found by:** architecture

The 10-minute beat task iterates VbtcV2Token.objects.all(), making one blockchain.info request plus a 0.5s sleep per token, serially, on the concurrency-1 vbtc-worker. At ~1.5s/token (HTTP + sleep), 400 tokens exceed the 10-minute schedule, after which runs permanently overlap-queue and balance freshness degrades unboundedly. The butterfly v2 architecture (per-user tokens with lazy minting, per docs/butterfly-vbtc-v2-architecture.md and project memory) makes token count proportional to user count, so this is a designed-in scaling cliff for the system's core balance-freshness mechanism — and the addresses property (user-visible BTC attribution) plus the withdrawal add-back logic both depend on this freshness. Currently 11 tokens (~17s/run), so it is invisible today and will degrade silently: there is no run-duration metric, no staleness alert, and get_balance failures are swallowed (returns None → token silently skipped). Needs batched/parallel fetch, an updated_at staleness marker, or event-driven refresh before user growth.

**Reviewer evidence:**

update_vbtc_balances.py:18-30: `VbtcV2Token.objects.all()` loop with `sleep(0.5)` and silent skip on None; mainnet count: 11 tokens today. project/celery.py:29-31 schedules every 10*60s on vbtc_queue.


### addresses property is O(N) Python-side aggregation executed per serialized token in list endpoints

**Severity:** low · **Category:** architecture · **Location:** `rbx/models.py:1163` · **Found by:** vbtc-token-model

VbtcV2TokenSerializer includes 'addresses', so VbtcV2ListAllView/VbtcV2ListView (api/btc/views.py:189-218) trigger, per token, two extra queries (transfers + completed withdrawals) and Python-side Decimal summation — a classic N+1 that compounds with the per-user-token butterfly model. More importantly for correctness review: because the aggregation lives only in a Python property, there is no single source of truth the DB can enforce or that other consumers (admin, future endpoints) can reuse consistently — the V1 VbtcToken.addresses property already implements a subtly different formula (no negative filter, no withdrawal handling), and the three rapid-fire reworks on 2026-06-02 (0732472 → eb3d1a6 → 3083c0e, all same afternoon) show the formula is being patched against display symptoms rather than reconciled against a ledger invariant (sum(addresses) == global_balance) that could be asserted/tested. Recommend materializing per-address balances as rows updated transactionally in process_transaction with an invariant check, which would have surfaced both the duplicate rows and the ownership-transfer inflation immediately.

**Reviewer evidence:**

rbx/models.py:1163-1202 (property, two queries per call); api/btc/serializers.py:93 includes 'addresses'; api/btc/views.py:192-194 serializes all tokens. Git log: three formula rewrites within 15 minutes on 2026-06-02 (commits 0732472 13:14, eb3d1a6 13:25, 3083c0e 13:29).


### Config and composition sprawl: 30 star-imported settings modules, ENVIRONMENT defaulting to 'undefined' gating chain rules, celery↔management-command indirection

**Severity:** low · **Category:** architecture · **Location:** `project/settings/environment.py:14` · **Found by:** architecture

Settings are 30 modules star-imported in project/settings/__init__.py with implicit ordering (worker.py imported last; any name collision silently wins by import order — already bit once judging by socket.py being appended out of alphabetical order). ENVIRONMENT defaults to the string 'undefined' (environment.py:14), and consensus-relevant logic branches on `== "testnet"` (rbx/tasks.py:224 ADNR burn amount, models.py:550 balance rules, btc/btc_client.py:26 which BTC chain to query): a deployment missing the env var silently applies MAINNET rules — including pointing BtcClient at mainnet blockchain.info — instead of failing fast. Periodic scheduling is likewise gated on IS_DEVNET/MINIMAL_CRON_JOBS/HEALTH_CHECK_ENABLED env flags spread across celery.py, so what runs where (web vs 4 worker roles) is reconstructable only by reading code. Celery tasks shell into management commands (project/celery.py:49-105) and management commands import task functions back (sync_blocks.py:3), so the call graph for 'a block got synced' crosses 4 files in 2 directions. Consequence: a misconfigured or missing env var degrades to mainnet-on-testnet (or vice-versa) behavior with no error, and operational changes (e.g., moving a task to another queue) require touching the celery/command indirection chain.

**Reviewer evidence:**

project/settings/__init__.py: 29 `from .x import *` lines; environment.py:14 default='undefined'; tasks.py:224 and btc_client.py:26 branch on the string; celery.py task bodies are all management.call_command wrappers.



## Docs issues

_Issues in the design docs themselves (stale claims, gaps, helper-script problems)._

### Ownership-transfer docs describe the wrong response shape: endpoint wraps payload in {success, tx_data} but docs show a bare JSON array

**Severity:** high · **Category:** docs · **Location:** `docs/vbtc-v2-ownership-transfer-gui.md:36` · **Found by:** vbtc-docs-conformance

Both ownership-transfer docs state Step 2 (GET /btc/vbtc-v2/ownership-transfer/...) 'Returns: JSON array with the TX data payload' and show sample code that uses the raw response body directly as the raw-TX Data field (gui doc lines 36-46 and 168-179; butterfly doc lines 36-38 where tx_data = response.json() is passed straight into the TX, and again at lines 104-118 in the service class). The actual view wraps the array: api/btc/views.py:696-697 returns {"success": true, "tx_data": [...]} — a deliberate change in commit bd8b5af ('Wrap ownership transfer data in {success, tx_data} object') made AFTER these docs were written and never backported. Concrete failure: an integrator following either doc builds a TKNZ_TX whose Data is the wrapper dict; the CLI's TX verification rejects it (or, if it ever got mined, the explorer's TKNZ_TX handler at rbx/tasks.py:813 would KeyError on 'Function' since the wrapper has no Function key). The error-shape documentation is also stale: docs say errors come back as {Success: false, Message} (PascalCase), the view normalizes to lowercase {success, message} with HTTP 500. Every code sample in both docs needs the .tx_data unwrap added.

**Reviewer evidence:**

api/btc/views.py:696-697: `if isinstance(result, list): return Response({"success": True, "tx_data": result})`. git log api/btc/views.py shows bd8b5af 'Wrap ownership transfer data in {success, tx_data} object'. docs/vbtc-v2-ownership-transfer-gui.md:36-48 and :168-179; docs/vbtc-v2-ownership-transfer-butterfly.md:34-39 and :104-118 all consume the bare array.


### Ownership-transfer docs route beacon upload to the SHOP CLI endpoint; the purpose-built V2 endpoint is undocumented, and the documented method (POST) is wrong

**Severity:** high · **Category:** docs · **Location:** `docs/vbtc-v2-ownership-transfer-butterfly.md:25` · **Found by:** vbtc-docs-conformance

Both ownership-transfer docs tell integrators to call /raw/beacon/upload/{sc}/{to}/{sig}/ for Step 1 (butterfly doc lines 25-30 and 94-99; gui doc line 19 and the Dart beaconUpload at line 124, also listed as a dependency 'deployed ✓' at butterfly doc line 180). That route proxies CreateBeaconUploadRequest on the SHOP CLI (rbx/client.py:338-345 uses SHOP_BASE_URL). But the V2 flow has its own endpoint /api/btc/vbtc-v2/beacon-upload/... (api/btc/urls.py:68) whose client function is explicitly commented 'Beacon upload via the V2 CLI (BASE_URL), not the shop CLI' (rbx/client.py:1219-1224) — added in commit 35685e6 precisely because the shop CLI is the wrong CLI for V2 contracts. An integrator following the docs uploads via a CLI that does not hold the V2 smart contract assets, getting 'Failed to talk to beacon'/asset-not-found failures (compare docs/cli-beacon-issues.md, written while debugging exactly this path) or a locator pointing at the wrong node — Step 2 then fails. Additionally, the gui doc's Step 1 heading says 'POST /api/raw/beacon/upload/...' but both that view and the V2 view are GET-only (api/raw/views.py:146-147, api/btc/views.py:668-670); a POST returns 405. The gui doc's own Dart sample contradicts its heading by using getJson. Neither doc mentions /btc/vbtc-v2/beacon-upload/ at all. Also note the raw endpoint's failure shape uses key 'error' (api/raw/views.py:161) while the V2 one uses 'message' — docs show neither.

**Reviewer evidence:**

rbx/client.py:343 SHOP_BASE_URL for beacon_upload_request vs rbx/client.py:1220 docstring 'Beacon upload via the V2 CLI (BASE_URL), not the shop CLI'. api/btc/urls.py:68 vbtc-v2/beacon-upload route. git log: 35685e6 'Add V2 beacon upload endpoint using BASE_URL instead of SHOP_BASE_URL'. docs reference only /raw/beacon/upload/.


### vbtc-v2-api-changelog.md documents a withdrawal-complete and ceremony API that does not exist

**Severity:** high · **Category:** docs · **Location:** `docs/vbtc-v2-api-changelog.md:133` · **Found by:** vbtc-docs-conformance

The changelog — the canonical 'breaking change' reference for client teams — is wrong about most write flows. (1) Withdrawal Complete (lines 133-165): documents prepare returning {SessionId, message_to_sign, Timestamp} and a synchronous execute taking {signature, timestamp, unique_id} that blocks up to 120s and returns {VFXTransactionHash, BTCTransactionHash}. Actual code: prepare additionally REQUIRES owner_address and returns StartMessage/StartTimestamp/ShareDistributionMessage/ShareDistributionTimestamp (api/btc/views.py:473-485); execute requires 8 fields including session_id and TWO signatures (views.py:501-505), returns {job_id} immediately, and the client must poll GET /withdraw/complete/status/{job_id}/ (views.py:558) — an endpoint the changelog never mentions — then broadcast via POST /btc/broadcast/ and finish with the Step-4 endpoints /withdraw/complete/tx/prepare|send/ (urls.py:62-63), none of which appear in the changelog. A client built from the changelog gets HTTP 400 'owner_address required' at prepare and has no path to the signed BTC tx. (2) Ceremony execute (lines 45-56): body shown with 3 fields; code requires 7 (session_id, owner_address, start_timestamp, share_distribution_timestamp also mandatory, views.py:318-322) → immediate 400. (3) create/prepare (lines 62-75): body shown without timestamp, unique_id, owner_signature; all three are required (views.py:366-369) → 400. The companion doc vfx-web-sdk-vbtc-v2-integration.md has the correct flow, so the fix is to rewrite the changelog's stale sections or point at the SDK spec.

**Reviewer evidence:**

api/btc/views.py:_require_fields lists for each view vs changelog request bodies; api/btc/urls.py:58-63 shows execute→status→tx/prepare→tx/send endpoints absent from the changelog. Mainnet has 6 type-28 completion TXs, proving the 4-step flow (with Step-4 TX) is the production reality.


### vbtc-v2-web-wallet-integration.md is stale end-to-end: removed endpoints, and 'ownership transfer = Type 3 / SC_TX' contradicts the implemented Type 18 flow

**Severity:** medium · **Category:** docs · **Location:** `docs/vbtc-v2-web-wallet-integration.md:281` · **Found by:** vbtc-docs-conformance

This doc (dated 2026-05-25, addressed to web wallet/frontend teams) still documents the removed single-step endpoints as live: POST /api/btc/vbtc-v2/withdraw/complete/ (line 151), /withdraw/cancel/ (line 182), /ceremony/initiate/ (line 210), /create/ (line 253), and the end-to-end flows at lines 285-324 are built on them. None of these routes exist in api/btc/urls.py — every call 404s; the changelog itself lists them under 'Removed Endpoints'. Worst concrete trap is section 6 (line 279-282): 'Smart contract ownership transfer uses the standard NFT transfer mechanism (Type 3 / SC_TX). No special vBTC V2 endpoint needed.' The shipped design (both ownership-transfer docs, commit 3d83402, and the live mainnet transfer tx 4221154... of 2026-06-02) uses Type 18 TKNZ_TX plus the dedicated /btc/vbtc-v2/ownership-transfer/ endpoint. If a wallet follows this doc and sends a Type 3 Transfer(), the explorer's NFT_TX handler (rbx/tasks.py:481-506) updates only Nft.owner_address — VbtcV2Token.owner_address stays stale, so the addresses balance map credits the WRONG owner with global_balance indefinitely. The doc needs a prominent superseded banner or deletion; teams have two contradictory 'integration guides' in the same directory.

**Reviewer evidence:**

api/btc/urls.py contains no ceremony/initiate, create/, withdraw/complete/ (bare), withdraw/cancel/ (bare) routes; docs/vbtc-v2-api-changelog.md:13-18 lists exactly these as removed. tasks.py:481-506 (type 3 handler touches only Nft) vs tasks.py:844-856 (type 18 handler updates VbtcV2Token.owner_address + Nft). Mainnet ownership transfer used type 18.


### Butterfly pre-activation flow contradicts the documented zero-balance transfer restriction

**Severity:** medium · **Category:** docs · **Location:** `docs/vbtc-v2-ownership-transfer-butterfly.md:9` · **Found by:** vbtc-docs-conformance

The butterfly docs build the entire pre-activation design on transferring ownership of a freshly minted token: 'Celery task mints tokens for active users. When user logs in, butterfly transfers ownership' (this doc line 9) and butterfly-vbtc-v2-architecture.md:21-26 ('Token created, SC ownership transferred to user's VFX address' immediately after the MPC ceremony, BEFORE the deposit address is even shown to the user — i.e. global_balance is necessarily 0 at transfer time). But the GUI ownership-transfer doc lists a CLI-enforced error for exactly this: 'Cannot transfer a token with zero balance — empty token' (docs/vbtc-v2-ownership-transfer-gui.md:54). Both cannot be true: either the CLI rejects every butterfly pre-activation transfer (the documented lazy-mint flow is dead on arrival and butterfly burns a ~30-90s MPC ceremony + mint fee per user for an untransferable token), or the zero-balance restriction doesn't exist/was lifted and the GUI doc's error list is wrong. Neither doc acknowledges the other. This needs an explicit resolution before the butterfly server work starts (it is item 3 in the architecture doc's implementation order). The contradiction is checkable today on testnet with a zero-balance token but was not resolved in any of the docs.

**Reviewer evidence:**

docs/butterfly-vbtc-v2-architecture.md:21-26 (transfer at step 4, deposit address shown at step 6); docs/vbtc-v2-ownership-transfer-butterfly.md:5-9 (mint then transfer on login, pre-deposit); docs/vbtc-v2-ownership-transfer-gui.md:50-54 (zero-balance error). All three docs are untracked working-copy files of the same vintage.


### Documented addresses-map semantics will break butterfly's aggregation: zero/negative balances are silently dropped, values are decimal strings not numbers

**Severity:** medium · **Category:** docs · **Location:** `docs/butterfly-vbtc-v2-architecture.md:40` · **Found by:** vbtc-docs-conformance

The architecture doc specifies butterfly's core balance computation as total_vbtc = sum(token.addresses[user_address] for token in user_associated_tokens) over the tokens returned by GET /btc/vbtc-v2/{address}/. Two undocumented behaviors break this as written: (1) VbtcV2Token.addresses filters out all entries with balance <= 0 (rbx/models.py:1202, commit 0732472), while the list endpoint returns tokens the address is merely ASSOCIATED with via any historical transfer (api/btc/views.py:204-211, matches on from_address OR to_address). So a user who received vBTC in a token and later sent it all away still gets that token in their list but has NO key in its addresses map — the doc's direct indexing raises KeyError (Python) / yields undefined (JS), crashing or NaN-ing the consolidated balance. Must be .get(addr, 0). The SDK doc carries the same trap: vfx-web-sdk-vbtc-v2-integration.md:298 types addresses as Record<string, number> with comment 'vfx_address → vbtc_balance' and no mention that absent keys are normal. (2) The SDK doc types global_balance/total_received/etc. and the addresses values as number, but DRF serializes DecimalFields as JSON strings ('0.0009708300000000' — confirmed in live API and shown correctly as strings in vbtc-v2-web-wallet-integration.md:41-49); naive arithmetic on them concatenates instead of adding. Honorable mentions folded in: the SDK doc's WithdrawalRequest.status union 'requested'|'completed' omits any cancelled/expired state (consistent with the code gap reported separately), and amount/fee_rate are also stringly-typed.

**Reviewer evidence:**

rbx/models.py:1202 `return {addr: bal for addr, bal in entries.items() if bal > 0}`; api/btc/views.py:204-211 association by transfer history; serializer uses default DecimalField rendering (api/btc/serializers.py:71-97, no coerce_to_string=False); mainnet API values observed as strings.


### Withdrawal completion authority is ambiguous across docs: requestor vs owner, and ownership transfer mid-withdrawal is unspecified

**Severity:** medium · **Category:** docs · **Location:** `docs/vfx-web-sdk-vbtc-v2-integration.md:199` · **Found by:** vbtc-docs-conformance

The withdrawal request step is keyed on requestor_address (any holder, per the multi-address balance model), but the completion FROST step requires owner_address (POST /withdraw/complete/prepare/ body, SDK doc line 199; enforced as required by api/btc/views.py:476). No doc states whose key must sign the FROST StartMessage/ShareDistributionMessage when requestor != owner: does the token OWNER complete a withdrawal requested by another holder, or must owner_address actually be the requestor's address (making the parameter misnamed)? This is not hypothetical on mainnet: withdrawal id 8 (token 6d893dce, requestor RPKx) is pending on a token owned by RNiQ, and token d11a9ef3 had its ownership transferred (2026-06-02) WHILE withdrawal id 4 (requestor RNiQ, the now-former owner) was pending — that request has been unfinishable-looking for 12 days. The Step-4 completion TX similarly takes from_address with no statement of who may send it (SDK doc line 243). An SDK or butterfly implementation must guess, and a wrong guess fails only at FROST-time after the user has already burned a Type-27 request TX, leaving exactly the stuck 'requested' rows observed in prod. The docs also never specify what happens to pending withdrawal_requests when ownership transfers (forbidden? inherited by new owner? auto-cancelled?) — neither ownership-transfer doc mentions withdrawals at all.

**Reviewer evidence:**

Mainnet rows: withdrawal id 8 requestor RPKxShZ... on token owned by RNiQrW3... (status requested since 2026-05-29); token d11a9ef3 ownership transferred 2026-06-02 with withdrawal id 4 still status='requested'. Code: views.py:476 requires owner_address for complete/prepare; views.py:441 requires requestor_address for request/prepare; cancel/prepare maps owner_address into the CLI's RequestorAddress field (views.py:644) — even the explorer's own field mapping conflates the two.



## Appendix: findings checked and dismissed

- **sign_and_send_completion.js: divergent key normalization, no HTTP error handling, exits 0 on failure, hardcoded hash and machine-local SDK path** (`scripts/sign_and_send_completion.js`) — refuted: The finding's central claim (1) is empirically false: stripping a leading '00' from a 64-char private key does NOT corrupt public-key derivation. The SDK's publicFromPrivate (vfx-web-sdk/lib/cjs/services/keypair-service.js:92-98) converts the hex to a Buffer and passes it to elliptic's keyFromPrivat

- **FROST withdrawal-complete forwards client-supplied Amount/BTCDestination not bound by any owner signature** (`api/btc/views.py`) — refuted: The cited explorer code is accurate: api/btc/views.py:509-521 forwards client-supplied Amount/BTCDestination/FeeRate (with 0/'' defaults) to the CLI, and the FROST authorizing signatures (prepare step, views.py:476-485) only ever cover sc_identifier+withdrawal_request_hash+owner_address+session/time
