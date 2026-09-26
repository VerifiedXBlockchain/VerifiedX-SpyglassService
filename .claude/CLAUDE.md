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
3. **Run the backfill once**, on the app, not locally:
   ```bash
   python3 -c 'import pty,sys; sys.exit(pty.spawn(["porter","app","run","rbx-explorer-<network>","--wait","--","python","manage.py","reprocess_vbtc_v2"]))'
   ```
   (`porter app run` needs a pseudo-terminal; macOS `script` fails when stdin is a socket, the Python pty module does not.) It reprocesses types 25-30 plus the legacy envelope transactions that touch vBTC V2 contracts, in chain order, idempotently. Expect one `Found N transaction(s)` line and `Done. Processed: N, Errors: 0`.
4. **Verify against a Core node**, not against Spyglass itself: for every token and address in `https://<network>-data.rbx.network/api/btc/vbtc-v2/`, compare `addresses[address]` with `GET http://<node>:17292/vbtcapi/VBTC/GetVBTCBalance/{address}/{scUID}` → `Balance`. Known pre-existing owner-formula divergences on Core's side are listed in the 2026-09-25 notes (`reviews/remediation-audit-2026-09-25` in the platform-context repo).

**Mainnet specifics.** `VBTC_NETWORK` defaults to the mainnet height table whenever `ENVIRONMENT` is not `testnet` (`project/settings/rbx.py`), so mainnet needs no new env var. The escrow gate on mainnet is block 7,296,200 and the multi-transfer gate 7,281,000; both are already below the tip, so the backfill will re-derive every escrowed request. **Do not skip step 3 on mainnet**: until it runs, every holder with a stalled or cancelled-but-unapproved withdrawal request is overstated, which is the SG-01 defect this code exists to fix. The mainnet backfill has not been run as of 2026-09-25.

**Testnet specifics.** The escrow binary reached the testnet fleet between blocks 926,211 and 927,986, so `VBTC_WITHDRAWAL_ESCROW_HEIGHT=927986` is pinned in the testnet Porter app env to match the live nodes' history (the code's testnet default is 1). Remove the pin only after the testnet nodes have resynced from genesis on a binary that escrows from height 1.
