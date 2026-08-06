#!/usr/bin/env bash
# agent-exec.sh - compatibility shim. The wrapper is now agent-exec.py; this name is kept so existing
# callers (and installed plugin versions) keep working.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python || command -v python3)"
[ -n "$PY" ] || { echo "$0: python not found on PATH" >&2; exit 2; }
exec "$PY" "$DIR/agent-exec.py"  "$@"
