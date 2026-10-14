#!/usr/bin/env bash
# golden-lock.sh <cmd...>
# Exclusive lock for golden-master bless/check ONLY (crucible_check.py, --update). Nothing else
# should take it, so ordinary heavy work (heavy-run.sh) is never serialized behind a proof sweep.
# Lock: ${SQUID_BOT_LOCK_DIR:-/tmp/squid-bot/locks}/golden.lock. Exit status is the command's.
set -u
[ $# -ge 1 ] || { sed -n '2,5p' "$0" >&2; exit 2; }
dir="${SQUID_BOT_LOCK_DIR:-${GG_LOCK_DIR:-/tmp/squid-bot/locks}}"
mkdir -p "$dir"
exec flock "$dir/golden.lock" "$@"
