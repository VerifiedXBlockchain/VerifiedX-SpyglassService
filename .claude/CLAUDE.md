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
