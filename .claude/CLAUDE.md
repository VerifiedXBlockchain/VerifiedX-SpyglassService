# VFX Explorer (RBX Explorer)

Blockchain explorer for the VFX/RBX network. Django + PostgreSQL + Celery. Syncs blocks, indexes transactions, and provides APIs for wallet/transaction data.

## Quick Commands

```bash
make up           # Start docker services
make migrate      # Apply migrations
make shell        # Django shell
make test         # Run tests
make celery       # Start celery worker
```

---

## Mainnet & Testnet Databases

Both connection URLs are in `.env.local` (gitignored). `.mcp.json` picks them up via `${VAR}` substitution when Claude launches via `./scripts/launch-claude.sh`.

| MCP Server | Network | Database | Read-only |
|---|---|---|---|
| `postgres-mainnet` | Mainnet (production) | `rbxexplorermainnet` | `default_transaction_read_only=on` session guard |
| `postgres-testnet` | Testnet | `rbxtestnet` | `default_transaction_read_only=on` session guard |

Both use the admin `postgres` user with a session-level read-only guard. **Do not attempt to disable it or issue INSERT/UPDATE/DELETE.**

### Two ways to query

1. **Postgres MCP (preferred):**
   - `mcp__postgres-mainnet__pg_execute_query` / `mcp__postgres-testnet__pg_execute_query`
2. **Raw psql:**
   ```bash
   psql "$VFX_EXPLORER_MAINNET_DB_URL" -c "SELECT ..."
   psql "$VFX_EXPLORER_TESTNET_DB_URL" -c "SELECT ..."
   ```

Always `LIMIT` queries on large tables.

---

## Porter Production Logs

Porter logs are fetched via `./scripts/fetch-logs.sh`:

- `./scripts/fetch-logs.sh mainnet [porter flags...]` → `porter app logs rbx-explorer-mainnet`
- `./scripts/fetch-logs.sh testnet [porter flags...]` → `porter app logs rbx-explorer-testnet`

Defaults: `--since 30m --limit 500`. Secret scrubbing is applied in the output pipe.

### Porter CLI context

Tyler uses `porter-switch vfx` / `porter-switch surf` to switch between Porter projects. If log fetches error with "app not found" or auth errors, run `porter-switch vfx` first.

### Log investigator subagent

For log investigation, dispatch the `log-investigator` subagent (`subagent_type: "log-investigator"`) instead of calling `fetch-logs.sh` directly. This keeps raw log output out of the main context window. Pass: **symptom**, **time window**, **network** (mainnet/testnet/both), and **identifiers** to grep for.

Use the log-investigator when:
- Sentry doesn't have a matching error signature
- A Celery task is stuck with no exception (block sync, vBTC processing)
- A startup error occurred before Sentry initialized
- A print-style debug line is the only trace

Skip it when:
- The symptom is purely a DB state issue (use the postgres MCPs)
- Sentry already has the exact error

### Porter services

| Service | Role |
|---|---|
| `web` | Gunicorn HTTP server (API, admin, wallet/transaction queries) |
| `default-worker` | Celery worker for default queue |
| `blocks-worker` | Celery worker for block sync (concurrency=1) |
| `vbtc-worker` | Celery worker for vBTC operations (concurrency=1) |
| `runner` | Celery beat scheduler |

---

## Sentry

- **Org:** verifiedx (`https://verifiedx.sentry.io`)
- **Project:** `python-django`

Use `mcp__sentry__*` tools to search issues, get details, analyze root causes. Filter by project `python-django`.

---

## Hard Rules

1. **Never write to mainnet or testnet databases.** The session-level read-only guard is the primary defense. Do not attempt to circumvent it.
2. **Never run migrations from Claude.** Migrations go through the normal deploy pipeline.
3. **Don't commit secrets.** `.env.local` is gitignored. Keep it that way.
4. **Use `./scripts/launch-claude.sh` to start sessions.** It sources the DB URLs that the MCP servers need.

---

## vBTC V2 indexer: rollout checklist (read before any mainnet deploy)

The vBTC V2 indexer (`rbx/vbtc_dispatch.py`, `rbx/vbtc_gates.py`, balance math in `rbx/models.py`) mirrors the node's state apply. Rows indexed by older code are wrong until they are re-derived, so a deploy of indexer changes is not finished until the backfill has run.

1. **Deploy** (push to `main` for mainnet, `testnet` for testnet). Porter's predeploy runs migrations; the workers restart.
2. **Wait for quiescence**: no fresh `ready.` lines in `./scripts/fetch-logs.sh <network> --since 8m` for a few minutes (Porter can roll a second revision after the Action goes green).
3. **Preview the run.** `--dry-run` lists the transactions, every settlement row the run would write, change or remove, and every one it would leave because the order inside the block is not stored:
   ```bash
   python3 -c 'import pty,sys; sys.exit(pty.spawn(["porter","app","run","rbx-explorer-<network>","--wait","--","python","manage.py","reprocess_vbtc_v2","--dry-run"]))'
   ```
   `Settlement rows that would change: 0; left as stored: 0` is what a chain that was indexed correctly gives when no transfer shares its block with other activity. Account for every row it names before going on. A row "left as stored" is not checked by the run at all: compare that holder with the node by hand (step 7). The preview reads each transfer against the table as it is, so where one settlement feeds the next on the same contract the run can end on a different row; steps 4 and 6 are the check on the result.
4. **Snapshot the balances** the API serves, before anything is rewritten:
   ```bash
   ./scripts/vbtc-v2-balances.py snapshot <network> before.json
   ```
5. **Run the backfill once**, on the app, not locally:
   ```bash
   python3 -c 'import pty,sys; sys.exit(pty.spawn(["porter","app","run","rbx-explorer-<network>","--wait","--","python","manage.py","reprocess_vbtc_v2"]))'
   ```
   (`porter app run` needs a pseudo-terminal; macOS `script` fails when stdin is a socket, the Python pty module does not.) It reprocesses types 25-30 plus the legacy envelope transactions that touch vBTC V2 contracts, in chain order, idempotently. Expect one `Found N transaction(s)` line and `Done. Processed: N, Errors: 0`. A clean exit says the transactions were processed, not that the ledger is right; steps 6 and 7 say that.
6. **Read every balance the backfill changed**:
   ```bash
   ./scripts/vbtc-v2-balances.py snapshot <network> after.json
   ./scripts/vbtc-v2-balances.py diff before.json after.json
   ```
   Each line must be a change the deployed code was meant to make. A backfill over a chain that was indexed correctly changes nothing.
7. **Verify against a Core node**, not against Spyglass itself:
   ```bash
   VFX_NODE_TOKEN=<token> ./scripts/vbtc-v2-balances.py node after.json --node http://<node>:<port> --token-env VFX_NODE_TOKEN
   ```
   It compares every holder balance with the node's `vbtcapi/VBTC/GetVBTCBalance/{address}/{scUID}`. A balance that matched the node before the backfill and does not after it is a defect in the backfill. Known owner-formula differences on Core's side are listed in the 2026-09-25 notes (`reviews/remediation-audit-2026-09-25` in the platform-context repo); on mainnet they are the owners of `4bb6f099`, `76c995d5`, `8234b371` and `d11a9ef3`, and holder `RNiQ` on `d11a9ef3`.

**What went wrong on 2026-09-28.** The first mainnet backfill replayed each ownership transfer against the present-day ledger and wrote three settlement rows that handed the former owner's current balance to the new owner (contracts `3de79275`, `6fc51819`, `b8f0d376`). The rows were deleted and the settlement now comes from the ledger as it stood at the transfer (`planned_settlement`, `before=tx`). Anything else that derives a row from the ledger during a replay needs the same cut-off. The order of transactions inside a block is not stored, so when the transfer's block holds other activity on the contract a replay keeps the row that live indexing wrote and logs a warning.

**What a replay cannot order.** `sync_block` stamps every transaction with its block's time, so nothing stored says which of two transactions in one block the chain applied first. `planned_settlement` names the cases: anything else on the contract in the transfer's block (a row, a request, a completion, a cancel, a refund, a second `Transfer()`), a reserve send whose unlock time is the block's time, and a refund with no time recorded. In each a replay, or a second pass over a transfer that already has its row, leaves the settlement as it is stored and logs a warning. When one block holds several transfers of a contract, a replay sets the owner to the address the block ends on, which does not depend on their order. A replay also keeps the recipient of a reserve send it already holds, because a recovery moves it and recoveries are not replayed. Neither network holds any of these cases as of 2026-09-29 (59 transfers on mainnet, 2 on testnet).

Known limits, none of which a backfill triggers:

- Re-syncing a block outside a backfill (`validate_transactions --fix`) recomputes a transfer that has lost its row. If a refund followed the transfer in that block, the settlement is computed as if the refund came first.
- A second pass outside `replaying()` over a transfer with no row, in a block that holds other activity on the contract, settles against the table as it is. No caller does this today. Wrap any new replay in `replaying()`.
- A reserve send that unlocks exactly at the transfer's block time is counted as applied before the transfer when the block is first indexed. Core's code reads as applying it after the block. Not confirmed against a node.

The fix for all three is to store each transaction's index inside its block at sync and order by it. That is a schema change and a re-sync of the index column, so it is its own piece of work.

**Mainnet specifics.** `VBTC_NETWORK` defaults to the mainnet height table whenever `ENVIRONMENT` is not `testnet` (`project/settings/rbx.py`), so mainnet needs no new env var. The escrow gate on mainnet is block 7,296,200 and the multi-transfer gate 7,281,000; both are already below the tip, so the backfill will re-derive every escrowed request. **Do not skip step 5 on mainnet**: until it runs, every holder with a stalled or cancelled-but-unapproved withdrawal request is overstated, which is the SG-01 defect this code exists to fix. The mainnet backfill ran on 2026-09-28 and, once the three settlement rows were removed, left every balance as it was.

**Testnet specifics.** The escrow binary reached the testnet fleet between blocks 926,211 and 927,986, so `VBTC_WITHDRAWAL_ESCROW_HEIGHT=927986` is pinned in the testnet Porter app env to match the live nodes' history (the code's testnet default is 1). Remove the pin only after the testnet nodes have resynced from genesis on a binary that escrows from height 1.
