# Phase 0 — Blueprint Corrections — Verification Report

**Reviewer:** Opus 4.7 (1M context), read-only verification against the executor's output.
**Phase plan:** `/Users/tyler/.claude/plans/synthetic-riding-frog-phases.md` — Phase 0.
**Gap analysis:** `/Users/tyler/.claude/plans/synthetic-riding-frog.md`.
**Phase-plan review:** `/Users/tyler/.claude/plans/synthetic-riding-frog-phase-plan-review.md`.
**Files under review:** `/Users/tyler/prj/vfx/vfx-explorer/docs/butterfly-vbtc-v2/{00..07}*.md` (8 files).

---

## Verdict

**PASS WITH WARNINGS.**

All Phase 0 acceptance criteria are met. The 8 blueprint files exist, the gap-analysis corrections (TC-1 through TC-10, PY-1, ENV-1) and phase-plan-review corrections (items 1–16 plus the relevant recommendations) are reflected accurately. No internal contradictions remain on the four high-risk axes (signing model, withdrawal source, env vars, treasury concept).

Three minor warnings keep this from being a clean PASS — all are documentation polish rather than load-bearing issues, and none should block the Phase 0 commit. Each is enumerated under Findings.

---

## Acceptance criteria checklist

### From phase plan Phase 0 acceptance criteria

| Criterion | Verdict | Evidence |
|---|---|---|
| All 8 files exist with the changes described | ✓ | `ls` returns `00..07-*.md`, 8 files, all non-trivial size (7K–20K) |
| No internal contradictions on signing model | ✓ | Frontend-only for user ops, treasury-key-backend-only for server ops via Spyglass HTTP — consistent in 00 (D8 + D9), 02 (planner-only + "no private key"), 05 (planner + treasury services side-by-side), 07 (PY-1 + treasury service) |
| No internal contradictions on withdrawal source | ✓ | "any token user holds balance in" framing — 00 (D3), 03 (top heading "Any Holder Can Withdraw"), 05 (`VbtcWithdrawalService.validate_withdrawal`). "Only from user's own primary token" wording is grep-clean across all 8 files |
| No internal contradictions on env vars | ✓ | ENV-1 in 05 (lines 372–390) matches 07 (lines 95–105) matches 04 (lines 90–109). KEEP/ADD/REMOVE structure consistent |
| No internal contradictions on treasury concept | ✓ | D9 in 00 (line 34–37), referenced as "butterfly's V2 token" / "butterfly-owned V2 token" consistently in 04 ("V2 treasury deposit address"), 05 ("ButterflyTreasuryToken"), 07 (whole file) |
| Reviewer can read 00 + 07 cold and understand the full architecture | ✓ | 00 establishes D1–D9 + sub-plan table including 07; 07 is self-contained on the treasury concept + activation + service + monitoring + runbook pointer. The cross-reference in 00 line 37 explicitly points to 07 |

### From the per-file checks in the verification prompt

**`00-master-plan.md`**

| Check | Verdict | Evidence |
|---|---|---|
| D9 added with butterfly-treasury-token wording | ✓ | Lines 34–37: "D9: Butterfly operates its own V2 treasury token" |
| Sub-plans table includes `07-treasury-token.md` | ✓ | Line 49: "Treasury V2 Token | 07-treasury-token.md | Butterfly's own V2 token: activation, treasury send service, monitoring" |
| Phase 0.5 row is in the timeline | ✓ | Line 56: "0.5 | Activate butterfly's V2 treasury token (one-time, blocks Phase 4+) | Phase 0" |
| "Treasury V2 SPoF" appears in Key Risks | ✓ | Line 84: "5. Treasury V2 token is an operational SPoF…" |

**`01-token-lifecycle.md`**

| Check | Verdict | Evidence |
|---|---|---|
| "Butterfly treasury activation" section exists | ✓ | Line 57: "## Butterfly Treasury Activation (Phase 0.5)" |
| Multi-tab race note exists in edge cases | ✓ | Lines 142, 144: "Multi-tab race during pre-activation (Phase 6)" and "Multi-tab race during on-demand activation" |
| Token metadata is `vBTC`/`vBTC Token`/`vBTC` (or TBD with next step) | ✓ | Lines 40–42 set vBTC defaults; line 44 has explicit TODO with three candidates and "lock in before mainnet activation" next step |

**`02-balance-and-transfers.md`**

| Check | Verdict | Evidence |
|---|---|---|
| `VbtcSendService.send(... private_key)` backend-signing snippet is GONE | ✓ | `grep "VbtcSendService.send"` returns nothing. Only `prepare_send` snippet remains (lines 100–140) |
| Planner-only `prepare_send` is present | ✓ | Lines 100–140 show `prepare_send(from_address, to_address, amount, exclude_sc_identifiers)` returning list of `{sc_identifier, amount}` |
| "Private key handling" paragraph mentioning backend signing is GONE | ✓ | `grep "private_key"` returns nothing — the section is replaced by line 96–98 "The backend never sees the private key…" |
| Cache invalidation subsection exists | ✓ | Lines 165–175: "### Cache invalidation" with code example |
| "Receiving from butterfly treasury" subsection exists | ✓ | Lines 177–185: "### Receiving from butterfly treasury" |

**`03-withdrawals.md`**

| Check | Verdict | Evidence |
|---|---|---|
| "Only from user's own primary token" is GONE (heading + prose) | ✓ | `grep "Only from user"` returns nothing across all 8 files |
| UX validates against total_balance | ✓ | Line 130: "Amount field validates against `total_balance` (sum across all held tokens)" |
| Cancel UX section exists with phase-gating rule | ✓ | Lines 132–134: pre-FROST shows Cancel; FROST-started "Cancel button is hidden" |
| SSE-based status section exists | ✓ | Lines 100–122: "Withdrawal Monitoring (SSE, not polling)" with `vbtc:withdrawal_status` payload |
| SDK-one-call clarification exists | ✓ | Line 36: "From the frontend's perspective, these 4 steps happen inside a single SDK call: `client.requestWithdrawal(...)`…" |

**`04-migration.md`**

| Check | Verdict | Evidence |
|---|---|---|
| TC-7 Option A (custody-then-vBTC) is in place | ✓ | Line 37 heading "Phase 3: V1 Balance Migration — Per-User Custody (TC-7 Option A)"; lines 39–48 describe the mechanics |
| V1 balance snapshot step is mentioned | ✓ | Lines 29–35: "V1 Balance Snapshot (before any cleanup)" |
| "Remove VbtcTransfer" line is GONE | ✓ | `grep "Remove VbtcTransfer\|Remove from butterfly DB: VbtcTransfer"` returns nothing. Line 59 explicitly says "no, keep them" |
| BTCDepositRequest + escrow tasks + PaymentLink escrow fields in removal list | ⚠ | BTCDepositRequest ✓ (line 60), escrow fields on PaymentLink ✓ (line 62). Escrow Celery tasks themselves are not enumerated in 04's removal list — but they are in 05's "Deleted" section (lines 364–370). Minor finding #1 below. |
| Personalized-email line is present | ✓ | Line 114: "**Personalized email to users with V1 balances**: include the snapshot amount in BTC…" |
| Column-drop is noted as deferred to Phase 6b | ✓ | Line 62: "Column drops happen in a separate deferred Phase 6b after operational sign-off"; lines 79–88 dedicate a "Deferred to Phase 6b" section with gating query |

**`05-backend.md`**

| Check | Verdict | Evidence |
|---|---|---|
| `ButterflyTreasuryToken` model is specified | ✓ | Lines 26–40 |
| VbtcTransfer is explicitly kept | ✓ | Lines 52–54: "### VbtcTransfer (kept; not new)" + "NOT removed in V2" |
| Orphan `VbtcSendService.send(private_key)` is GONE | ✓ | Only `VbtcSendService.prepare_send` (lines 144–182) remains |
| `VbtcTreasurySendService` is specified with single-flight lock | ✓ | Lines 184–217 with `with redis_lock(f'vbtc_v2:treasury_send:{treasury.sc_identifier}', ttl=300)` |
| SpyglassVbtcClient has prepare/send/cancel method stubs | ✓ | Lines 219–255: `prepare_transfer`, `send_transfer`, `prepare_withdraw_request`, `send_withdraw_request`, `prepare_withdraw_cancel`, `send_withdraw_cancel` |
| New API endpoints listed | ✓ | Lines 261–338: balance, activation status, record activation, prepare send, validate withdrawal — all with `VfxSignatureRequired` |
| Celery tasks updated per spec (kept/new/deleted) | ✓ | Lines 340–370 with clear "Kept (rewritten)", "New", and "Deleted (replaced by V2)" subsections |
| ENV-1 env var diff in place with KEEP/ADD/REMOVE | ✓ | Lines 372–390: "## Environment Variables (ENV-1)" with Keep/Add/Remove sub-headings; includes `VBTC_V2_ACTIVE` per phase-plan-review correction #8 |
| Removed Code section updated | ✓ | Lines 392–412: Delete (Phase 6a), Do NOT delete, Keep — all three subsections present and consistent |

**`06-frontend.md`**

| Check | Verdict | Evidence |
|---|---|---|
| SDK is `v3.0.0 (file-linked until published)` | ✓ | Line 8: "**vfx-web-sdk** **v3.0.0 (file-linked until published)**. `package.json` carries `\"vfx-web-sdk\": \"file:../../vfx/vfx-web-sdk\"`" + feature-branch note |
| `vbtcActivationStatus` union has `'unknown'` default | ✓ | Lines 42–45: "Default 'unknown' so cold load does NOT briefly show the wrong CTA" |
| Deposit + Receive page spec uses `react-qr-code` (NOT `qrcode.react`) | ✓ | Lines 11 + 224: `react-qr-code` ^2.0.18. `grep qrcode` returns nothing |
| Withdrawal Status via SSE subsection exists | ✓ | Lines 253–272: "## Withdrawal Status via SSE" with on-mount rehydrate + SSE subscribe |
| Multi-token Send UI subsection exists with `exclude_sc_identifiers` partial-failure resume | ✓ | Lines 230–251: "## Multi-token Send UI" + lines 248–251 "Partial-failure resume" calls `prepareVbtcV2Send` with `exclude_sc_identifiers: completed` |
| PasswordPromptModal unchanged | ✓ | Line 102 ("No changes needed") + line 274 ("`PasswordPromptModal` continues to work unchanged") |

**`07-treasury-token.md`**

| Check | Verdict | Evidence |
|---|---|---|
| File exists | ✓ | 13550 bytes, all required sections present |
| Concept | ✓ | Lines 3–10 |
| Model | ✓ | Lines 14–23 |
| Activation procedure with idempotency (incl. Spyglass recovery against partial failures) | ✓ | Lines 25–93; idempotency steps 1+2 at lines 37–42 explicitly cover "Token exists on chain but row missing" via `get_tokens_for_address` recovery |
| Env vars (additive, matches ENV-1) | ✓ | Lines 95–105 list `BUTTERFLY_VBTC_V2_TREASURY_SC_ID_{TESTNET,MAINNET}`, `BUTTERFLY_VBTC_V2_TREASURY_DEPOSIT_ADDRESS_{TESTNET,MAINNET}`, `VBTC_V2_TREASURY_LOW_BALANCE_THRESHOLD_BTC` — exact match with 05's ENV-1 Add block |
| Python signing parity (PY-1) with throwaway script gate | ✓ | Lines 107–135 with explicit gate "MUST run successfully against testnet Spyglass before any other Phase 1 code lands" + JS cross-check unit test reference |
| VbtcTreasurySendService with Redis single-flight lock | ✓ | Lines 139–165 with `lock_key = f'vbtc_v2:treasury_send:{treasury.sc_identifier}'` + 5-minute TTL |
| `monitor_v2_treasury_balance` task with threshold env var | ✓ | Lines 169–194 with `VBTC_V2_TREASURY_LOW_BALANCE_THRESHOLD_BTC` reference and 15-min cadence |
| Operational rules | ✓ | Lines 196–200 |
| Mainnet runbook pointer | ✓ | Lines 202–214 referencing `docs/runbooks/activate-mainnet-vbtc-v2-treasury.md` (in butterfly-service repo, not blueprint) |

### Cross-file consistency checks

| Check | Verdict | Evidence |
|---|---|---|
| Signing model identical across all files | ✓ | Frontend-only for user ops in 00 (D8), 02 (lines 96–98, 159–162), 05 (line 141); treasury-key-backend-only in 00 (D9), 05 (VbtcTreasurySendService), 07 (whole file). No contradictions found |
| Withdrawal source = "any token user holds balance in" everywhere | ✓ | 00 (D3), 03 (line 4, line 130), 05 (validate_withdrawal). Grep for "Only from user" returns nothing |
| Env var list in 05 (ENV-1) matches 07 matches 04 | ✓ | All three list the same Add block: `BUTTERFLY_VBTC_V2_TREASURY_SC_ID_*`, `BUTTERFLY_VBTC_V2_TREASURY_DEPOSIT_ADDRESS_*`, `VBTC_V2_TREASURY_LOW_BALANCE_THRESHOLD_BTC`. 05 additionally lists `VBTC_V2_ACTIVE` (cutover flag) — 04/07 don't need to mention this since it's a Phase 5 concept. 04 line 92 explicitly says "The authoritative env-var diff lives in `05-backend.md`" |
| Removal list consistency | ✓ | `VbtcTransfer` is NOT removed anywhere (04 line 59, 05 line 404); `BTCDepositRequest` + escrow tasks + PaymentLink escrow fields ARE in removal list (04 lines 60–62 + 05 lines 396–399) |
| D9 referenced consistently across 00, 04, 05, 07 | ✓ | 00 line 34 (D9 definition), 04 (treasury concept via Phase 0.5 references), 05 (`ButterflyTreasuryToken` model), 07 (whole file). All four use "butterfly's V2 [treasury] token" framing |
| SDK version is v3.0.0 (file-linked) in 06; 07 doesn't contradict | ✓ | 06 line 8 has v3.0.0 file-link; 07 references the SDK only as a source code path (line 46, 109, 135) — no version contradiction |

---

## Findings

### Finding #1 (minor, polish) — `04-migration.md` does not enumerate the V1 escrow Celery tasks in its removal list

**File:** `/Users/tyler/prj/vfx/vfx-explorer/docs/butterfly-vbtc-v2/04-migration.md`
**Location:** "Data Cleanup → Remove from butterfly DB" section, lines 58–62.
**What's there:** BTCDepositRequest, VbtcTreasuryState, and PaymentLink escrow fields are listed for removal in 04, but the V1 escrow Celery tasks (`monitor_btc_escrow_deposit`, `execute_btc_sweep`, etc.) are not.
**What the gap analysis asks for:** TC-5 line ("BTCDepositRequest correctly deleted, but `PaymentLink.btc_escrow_*` fields and `monitor_btc_escrow_deposit` / `execute_btc_sweep` / `sweep_btc_deposit_escrow` / `monitor_btc_deposit_sweep` tasks need to be removed in `05-backend.md`'s Removed Code section") — these are required in 05, which is satisfied (05 lines 364–370). The gap analysis's verification prompt says "BTCDepositRequest + escrow tasks + PaymentLink escrow fields ARE in the removal list" — this is true if you read 04 + 05 together, but not from 04 alone.
**Severity:** Polish. The information is in 05; 04 covers the data-layer cleanup and points to 05 for the env-var/task surface. A reader following the cross-references will find it. Not a blocker.
**Optional fix:** Add a one-line callout in 04 line 56 area: "(See 05's 'Deleted' Celery tasks list for the V1 escrow tasks themselves.)"

### Finding #2 (minor, polish) — 04's Phase numbering ("Phase 1: Freeze V1", "Phase 2: V2 Goes Live", "Phase 3: V1 Balance Migration") overlaps with the executable phase plan's phase numbers

**File:** `/Users/tyler/prj/vfx/vfx-explorer/docs/butterfly-vbtc-v2/04-migration.md`
**Location:** Section headers "## Phase 1: Freeze V1" (line 14), "## Phase 2: V2 Goes Live" (line 23), "## Phase 3: V1 Balance Migration — Per-User Custody (TC-7 Option A)" (line 37).
**What's wrong:** These are migration-strategy phases that are unrelated to the executable plan's Phase 1, 2, 3 (which are Spyglass client, balance API, activation API). 00-master-plan.md already disambiguates with a footnote at line 68 ("The phase numbering above is the architecture-level timeline. The executable phase plan re-numbers these…"), but 04's internal phase numbering can still trip a cold reader.
**Severity:** Polish. A reader who has read 00 will understand. Not a blocker.
**Optional fix:** Rename 04's section headers to "Step 1: Freeze V1", "Step 2: V2 Goes Live", "Step 3: V1 Balance Migration" (or "Migration Strategy Phase 1/2/3" with the qualifier inline).

### Finding #3 (minor, polish) — `01-token-lifecycle.md` keeps Phase 2/Phase 6 phase numbering that does not match the executable plan

**File:** `/Users/tyler/prj/vfx/vfx-explorer/docs/butterfly-vbtc-v2/01-token-lifecycle.md`
**Location:** Line 11 ("## On-Demand Activation (Phase 2)"), line 89 ("## Pre-Activation (Phase 6)").
**What's wrong:** Same as Finding #2 — these refer to the architecture-level phase numbering in 00, not the executable plan. The executable plan's Phase 2 is the balance API; Phase 6 is V1 cleanup. 01's "Phase 2" is on-demand activation, which in the executable plan is Phase 3.
**Severity:** Polish. 00's line-68 footnote covers this. Not a blocker.
**Optional fix:** Same as Finding #2.

---

## Recommendation

**Commit Phase 0 as-is.** The acceptance criteria are met and there are no internal contradictions on the four high-risk axes. All three findings are documentation polish that does not affect the substance of the blueprint — they're cross-reference clarity, not correctness. Tyler can either:

(a) Merge Phase 0 now and address findings 1–3 in a follow-up touch-up commit, or
(b) Apply the three optional fixes inline before committing.

Either is fine. Findings 1–3 will not change the executor's ability to act on the blueprint in Phase 0.5 and beyond.

---

## Notes for the executor on what was NOT verified here

- The executor agent reported applying the corrections but the prompt did not ask me to verify the git diff. I verified the **end state** of the 8 files against the gap-analysis + phase-plan-review + phase-plan acceptance criteria. If the executor introduced new content that is correct but not in the acceptance criteria (e.g. additional clarifying paragraphs), that's fine and not flagged.
- I did not run the parity script, did not call Spyglass, did not query any DB, and did not touch any of the symlinked repos. Phase 0 is docs-only.
- I did not modify any blueprint files. Per the verification prompt instructions, only this report was written.
