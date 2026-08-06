#!/usr/bin/env bash
# fetch-logs.sh — fetch Porter logs for vfx-explorer (mainnet or testnet)
# with best-effort secret scrubbing. Emits to stdout; nothing written to disk.
#
# Primary consumer is the `log-investigator` subagent. The main debug agent
# should dispatch that subagent rather than calling this script directly, so
# raw logs never enter the main context window.
#
# Usage:
#   ./scripts/fetch-logs.sh <mainnet|testnet> [porter flags...]
#
# Targets:
#   mainnet  → porter app logs rbx-explorer-mainnet
#   testnet  → porter app logs rbx-explorer-testnet
#
# Defaults applied if you do not override them:
#   --since 30m
#   --limit 500
#
# Examples:
#   ./scripts/fetch-logs.sh mainnet --since 10m --search ERROR
#   ./scripts/fetch-logs.sh testnet --since 1h --service default-worker
#   ./scripts/fetch-logs.sh mainnet --from 2026-04-10T14:00:00Z --to 2026-04-10T14:30:00Z
#   ./scripts/fetch-logs.sh mainnet --since 2h --search block_sync
#
# Scrubbing: Bearer tokens, Authorization headers, postgres URLs, and common
# credential assignments (api_key, password, secret, token) are redacted in
# the pipe. This is defense in depth — do NOT treat the output as safe to
# paste into bug reports or shared docs without manual review.
#
# IMPORTANT: Porter CLI must be switched to the vfx context first.
# Run `porter-switch vfx` if you get auth or "app not found" errors.

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage: fetch-logs.sh <mainnet|testnet> [porter flags...]

Targets:
  mainnet  → porter app logs rbx-explorer-mainnet
  testnet  → porter app logs rbx-explorer-testnet

Defaults (applied if not overridden):
  --since 30m
  --limit 500

Porter flags you will commonly want to pass through:
  --since DURATION      e.g. --since 10m, --since 2h
  --from TIMESTAMP      UTC, e.g. --from 2026-04-10T14:00:00Z
  --to TIMESTAMP        UTC, e.g. --to 2026-04-10T14:30:00Z
  --limit N             number of lines to return
  --search STRING       server-side filter — use this for identifiers
  --service NAME        filter by service within the app (e.g. web, default-worker, blocks-worker)

Examples:
  fetch-logs.sh mainnet --since 10m --search ERROR
  fetch-logs.sh testnet --since 1h --service default-worker
  fetch-logs.sh mainnet --from 2026-04-10T14:00:00Z --to 2026-04-10T14:30:00Z

NOTE: Run `porter-switch vfx` first if Porter is set to a different project.
EOF
}

if [ $# -lt 1 ] || [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 1
fi

target="$1"
shift

case "$target" in
    mainnet) app="rbx-explorer-mainnet" ;;
    testnet) app="rbx-explorer-testnet" ;;
    *)
        echo "Error: target must be 'mainnet' or 'testnet' (got '$target')" >&2
        usage
        exit 1
        ;;
esac

# Apply defaults only if the user didn't already specify them
has_time_flag=false
has_limit_flag=false
for arg in "$@"; do
    case "$arg" in
        --since|--from|--to) has_time_flag=true ;;
        --limit)             has_limit_flag=true ;;
    esac
done

defaults=()
if [ "$has_time_flag" = false ]; then
    defaults+=(--since 30m)
fi
if [ "$has_limit_flag" = false ]; then
    defaults+=(--limit 500)
fi

# Sanity: is porter installed and callable?
if ! command -v porter >/dev/null 2>&1; then
    echo "Error: porter CLI not found in PATH. Install from https://docs.porter.run/cli/installation" >&2
    exit 127
fi

# Build the final command line. The ${defaults[@]+...} expansion is the
# safe idiom for "expand only if the array is non-empty" under `set -u`.
cmd=(porter app logs "$app")
cmd+=(${defaults[@]+"${defaults[@]}"})
cmd+=("$@")

# Fetch and scrub in a perl pipe. s{}{} syntax is used so the alternation
# `|` inside character groups does not collide with the regex delimiter.
"${cmd[@]}" 2>&1 \
    | perl -pe '
        s{(Bearer )[A-Za-z0-9._\-]+}{${1}<REDACTED>}g;
        s{(Authorization:\s*)\S+}{${1}<REDACTED>}gi;
        s{postgres(ql)?://[^@\s]+@}{postgres${1}://<REDACTED>@}g;
        s{((?i:api[_\-]?key|password|secret|token))(["\s]*[=:]["\s]*)[^\s,"\x27]+}{${1}${2}<REDACTED>}g;
    '
