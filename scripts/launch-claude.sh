#!/usr/bin/env bash
# Launch Claude Code in this workspace with database URLs loaded from
# .env.local. The .mcp.json references these via ${VAR} substitution.
#
# Usage:
#   ./scripts/launch-claude.sh
#   ./scripts/launch-claude.sh --some-claude-flag

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env.local ]]; then
  echo "ERROR: .env.local is missing."
  echo "Create .env.local with VFX_EXPLORER_MAINNET_DB_URL and VFX_EXPLORER_TESTNET_DB_URL."
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env.local
set +a

# Sanity check: required vars are non-empty and not placeholders.
for var in VFX_EXPLORER_MAINNET_DB_URL VFX_EXPLORER_TESTNET_DB_URL; do
  val="${!var:-}"
  if [[ -z "$val" || "$val" == *"REPLACE_ME"* ]]; then
    echo "ERROR: $var is unset or still contains REPLACE_ME."
    echo "Edit .env.local and set a real value."
    exit 1
  fi
done

exec claude "$@"
