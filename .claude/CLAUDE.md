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
3. **Preview the run.** `--dry-run` lists the transactions, the settlement rows the run would write, change or remove, and the ones it would leave because the order inside the block is not stored, all read from the table as it is:
   ```bash
   python3 -c 'import pty,sys; sys.exit(pty.spawn(["porter","app","run","rbx-explorer-<network>","--wait","--","python","manage.py","reprocess_vbtc_v2","--dry-run"]))'
   ```
   `Settlement rows that would change: 0; left as stored: 0` is what a chain that was indexed correctly gives when no transfer shares its block with other activity. Account for every row it names before going on. A row "left as stored" is not checked by the run at all: compare that holder with the node by hand (step 7). The run rebuilds the table as it goes, so it can end on a different row than the preview names: where one settlement feeds the next on the same contract, and where a row of the transfer's block is missing until the run writes it. Steps 4 and 6 are the check on the result.
4. **Snapshot the balances** the API serves, before anything is rewritten:
   ```bash
   ./scripts/vbtc-v2-balances.py snapshot <network> before.json
   ```
5. **Run the backfill once**, on the app, not locally:
   ```bash
   python3 -c 'import pty,sys; sys.exit(pty.spawn(["porter","app","run","rbx-explorer-<network>","--wait","--","python","manage.py","reprocess_vbtc_v2"]))'
   ```
   (`porter app run` needs a pseudo-terminal; macOS `script` fails when stdin is a socket, the Python pty module does not.) It reprocesses types 25-30 plus the legacy envelope transactions that touch vBTC V2 contracts, block by block, idempotently. Inside a block it goes in hash order, which is not the chain's, with ownership transfers last so that the rest of the block is back in the table when they are reached. Run it whole: `--type 25` and `--skip-envelopes` replay mints without the transfers that follow them and leave `Nft.owner_address` on the minter. Expect one `Found N transaction(s)` line and `Done. Processed: N, Errors: 0`. A clean exit says the transactions were processed, not that the ledger is right; steps 6 and 7 say that.
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
   It compares every holder balance Spyglass lists with the node's `vbtcapi/VBTC/GetVBTCBalance/{address}/{scUID}`. A balance that matched the node before the backfill and does not after it is a defect in the backfill. A holder the backfill dropped is not in `after.json` and is not asked about; step 6 is what shows it. Known owner-formula differences on Core's side are listed in the 2026-09-25 notes (`reviews/remediation-audit-2026-09-25` in the platform-context repo); on mainnet they are the owners of `4bb6f099`, `76c995d5`, `8234b371` and `d11a9ef3`, and holder `RNiQ` on `d11a9ef3`.

**What went wrong on 2026-09-28.** The first mainnet backfill replayed each ownership transfer against the present-day ledger and wrote three settlement rows that handed the former owner's current balance to the new owner (contracts `3de79275`, `6fc51819`, `b8f0d376`). The rows were deleted and the settlement now comes from the ledger as it stood at the transfer (`planned_settlement`, `before=tx`). Anything else that derives a row from the ledger during a replay needs the same cut-off. The order of transactions inside a block is not stored, so when the transfer's block holds other activity on the contract a replay keeps the row that is stored and logs a warning.

**What a replay cannot order.** `sync_block` stamps every transaction with its block's time, so nothing stored says which of two transactions in one block the chain applied first. `VbtcV2Token.has_unordered_activity` names the cases: a row or a request on the contract in the transfer's block, a refund decided in that block or with no time recorded, and a reserve send that unlocked between the block before and the transfer's block. In each a replay, or a second pass over a transfer that already has its row, leaves the settlement as it is stored and logs a warning. `--dry-run` lists it as left when the rows of the block are in the table; when one is missing until the run writes it, the preview names a change the run will not make. Nothing checks such a settlement: compare the holders of that contract with the node by hand. It does not matter whose row it is, because a row stored under the wrong parties is corrected by the same replay and can be reached after the transfer. A completion or a cancellation request does not hold a settlement, because an open request and a completed one settle the same. Neither network holds any of these cases as of 2026-09-29 (59 transfers on mainnet, 2 on testnet).

A block cannot hold two `Transfer()` of one contract: the node checks each against the owner before the block and rejects a block where one sender touches a contract twice (`TransactionValidatorService.cs:981`, `BlockValidatorService.cs:1066`).

A reserve send that a recovery redirected keeps the recovery's address on every later pass (`Recovery.outstanding_transactions`).

Known limits, with what each network holds on 2026-09-29:

- The backfill does not replay callbacks, recoveries (type RESERVE) or sales. It resets `Nft.owner_address` to what the mint and the transfers give, so a change a `Recover()`, a `CallBack()` or a `Sale_Complete()` made to it is lost. Neither branch moves `VbtcV2Token.owner_address` at all, in live indexing or in a replay. No recovery on either network involves a vBTC V2 party, and every contract's two owner fields agree.
- A request that the indexer before `faae121` marked CANCELLED at the cancel transaction, without a vote, stays CANCELLED through a backfill, because a cancel on a closed request is ignored. Such a row has `cancelled_at` equal to its cancel transaction's time. Mainnet holds no cancelled request; testnet holds one, cancelled by vote.
- `validate_transactions --fix` deletes a block's transactions and re-syncs it. The delete cascades to every ledger row and withdrawal request that points at one of them, including a request from an earlier block whose completion or cancel is in this one. After using it on a block with vBTC V2 activity, run the backfill by this checklist. The backfill brings back rows and requests, not what a callback, a recovery or a FROST signing wrote on them.
- A second pass outside `replaying()` over a transfer with no row settles against the table as it is. No caller does this today. Wrap any new replay in `replaying()`.
- When a transfer is first indexed, a reserve send counts as applied if its unlock time is at or before the transfer's block time. Core applies a send at the end of the first block stamped after the unlock (`ReserveService.Run`), so a send that unlocked since the block before has not been applied yet. `Recover()` has the same window. Read from Core's code, not confirmed against a node. No vBTC V2 contract on either network has a reserve send.
- A `Transfer()` from a reserve address moves the owner on Core when it unlocks; Spyglass moves it when it is mined. One mainnet contract is owned by a reserve address.
- `Recover()` to an address that already has an `Address` row raises and the whole recovery is skipped.

Storing each transaction's index inside its block at sync would remove the cases a replay cannot order. That is a schema change and its own piece of work.

**Mainnet specifics.** `VBTC_NETWORK` defaults to the mainnet height table whenever `ENVIRONMENT` is not `testnet` (`project/settings/rbx.py`), so mainnet needs no new env var. The escrow gate on mainnet is block 7,296,200 and the multi-transfer gate 7,281,000. The mainnet backfill ran on 2026-09-28 and, once the three settlement rows were removed, left every balance as it was. A deploy that changes how rows are derived needs the backfill again; one that does not, such as the fix to the backfill itself, needs none.

**Testnet specifics.** The escrow binary reached the testnet fleet between blocks 926,211 and 927,986, so `VBTC_WITHDRAWAL_ESCROW_HEIGHT=927986` is pinned in the testnet Porter app env to match the live nodes' history (the code's testnet default is 1). Remove the pin only after the testnet nodes have resynced from genesis on a binary that escrows from height 1.
