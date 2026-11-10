# Spyglass (VerifiedX explorer service)

Django REST service behind the VerifiedX explorer, the wallets (Flutter GUI, web wallet, browser extension), the TypeScript SDK and the payments service. Postgres, Redis (cache) and Celery workers that index blocks from a Core node (`rbx/tasks.py`, `sync_block`). Operations notes (databases, Porter logs, Sentry, the vBTC V2 rollout checklist) live in `.claude/CLAUDE.md`.

## Layout

- `api/<area>/`: views, serializers and urls per area, mounted under `/api/` (`api/urls.py`).
- `rbx/`: models, the block indexer and the vBTC logic. `rbx/migrations/` holds the schema; an index on a large table is built concurrently in a non-atomic migration (see `0086`).
- `project/settings/`: one module per concern, read from the environment.

## Tests

- `make test` runs `python manage.py test` inside the `web` container of `docker compose` (project `vfx-explorer`).
- The settings need the variables named in `.env.template` (and `BROKER_URL`), with `API_AUTH_REQUIRED=False` and `SECURE_SSL_REDIRECT=False` for the test client. Point `DATABASE_URL` and `REDIS_URL` at a throwaway Postgres 15 and Redis 7 only; the test runner creates its own `test_` database.

## Compatibility

Clients that cannot be updated read every existing endpoint. Additions only: new endpoints, optional query parameters, headers, indexes, caching. No renamed, removed or retyped field, and no change of default ordering, page size, payload or status code.

## Branches

- `testnet` deploys the testnet stack (`rbx-explorer-testnet`) through the Porter GitHub Action on push.
- `main` deploys mainnet (`rbx-explorer-mainnet`) on push.
- `devnet` deploys `vfx-explorer-devnet`.

## Ship

- **Non-prod:** the `testnet` branch.
- **Prod:** `main` (mainnet). Merging or pushing to `main` needs the owner's sign-off.
