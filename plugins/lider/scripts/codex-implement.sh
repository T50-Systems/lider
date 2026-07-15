#!/usr/bin/env bash
# codex-implement.sh — Lider-owned implementer path for the pipeline.
#
# Runs Codex with FULL ACCESS (--sandbox danger-full-access, approvals off) in an
# isolated CODEX_HOME, so the implementer can read/write across the filesystem and
# reach the network — lifting the `workspace-write` cap the codex plugin's
# app-server imposes (writes confined to the repo, no network). The task operates
# on the CURRENT working directory (the repo); the isolated home only strips the
# user's personal Codex config, it does not change where the task writes.
#
# Designed to be launched in the BACKGROUND: the orchestrator polls
# `git status --short` for progress, <log_file>.status.json for live state, and
# <done_file> for completion. A heartbeat is emitted to stdout while it runs.
#
# NOTE: this wrapper does NOT auto-retry (CODEX_RETRIES defaults to 0). A half-
# written working tree from a dead implementer is not safe to blindly re-run;
# recovery is the orchestrator's call (inspect the diff, reset, or resume).
#
# Usage: codex-implement.sh <timeout_s> <log_file> <done_file> <model_slug> <prompt>
#
# Exit codes (also written to <done_file> for the watcher):
#   0    codex finished ok
#   124  timeout (process tree killed)
#   125  aborted early by a watchdog (stalled, or produced nothing at startup)
#   127  codex binary not found
#   2    bad usage
#   N    any other code: codex's exit code is propagated
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$#" -lt 5 ]; then
  echo "Usage: codex-implement.sh <timeout_s> <log_file> <done_file> <model_slug> <prompt>" >&2
  exit 2
fi

TIMEOUT_S="$1"
LOG_FILE="$2"
DONE_FILE="$3"
MODEL="$4"
shift 4
PROMPT="$*"
STATUS_FILE="$LOG_FILE.status.json"

# --- 1. Locate codex -------------------------------------------------------
if ! command -v codex >/dev/null 2>&1; then
  export PATH="$PATH:${APPDATA:-}/npm:${HOME:-}/AppData/Roaming/npm"
fi
if ! command -v codex >/dev/null 2>&1; then
  echo "codex-implement.sh: 'codex' binary not found on PATH." >&2
  echo 127 >"$DONE_FILE"
  exit 127
fi
CODEX_BIN="$(command -v codex)"

# --- 2. Supervision layer + isolated CODEX_HOME ---------------------------
# codex-runtime.sh provides preflight/run_supervised and re-exports the
# isolation helper.
# shellcheck source=codex-runtime.sh
. "$SCRIPT_DIR/codex-runtime.sh"
preflight || true
setup_isolated_codex_home "$(dirname "$LOG_FILE")"

# --- 3. Log header ---------------------------------------------------------
{
  echo "=== codex-implement.sh $(date '+%Y-%m-%d %H:%M:%S') ==="
  echo "codex bin: $CODEX_BIN"
  echo "codex --version: $("$CODEX_BIN" --version 2>&1)"
  echo "model: $MODEL"
  echo "sandbox: danger-full-access (FULL ACCESS — writes anywhere, network on, no approvals)"
  echo "workdir: $(pwd)"
  echo "isolated CODEX_HOME: $ISO_CODEX_HOME (from $REAL_CODEX_HOME)"
  echo "timeout: ${TIMEOUT_S}s"
  echo "status: $STATUS_FILE"
} >>"$LOG_FILE" 2>&1

# --- 4. Run codex (full access, supervised) --------------------------------
# run_supervised keeps `timeout -k 10` as the hard backstop and adds the
# heartbeat / status file / inactivity watchdog / tree-kill on top. No retry
# here (RETRIES=0): a half-written tree must not be blindly re-run.
# Let the signal trap finalize <done> if the wrapper is killed mid-run.
_CODEX_DONE_FILE="$DONE_FILE"
run_supervised "implement" "$TIMEOUT_S" "$LOG_FILE" "$STATUS_FILE" \
  "${CODEX_STALL_S:-300}" "${CODEX_STARTUP_S:-60}" "${CODEX_RETRIES:-0}" "${CODEX_BACKOFF_S:-5}" \
  -- "$CODEX_BIN" exec \
     --sandbox danger-full-access --skip-git-repo-check \
     -c "mcp_servers={}" --model "$MODEL" "$PROMPT"
CODEX_EXIT=$?

# --- 5. Record outcome for the watcher -------------------------------------
echo "$CODEX_EXIT" >"$DONE_FILE"

if [ "$CODEX_EXIT" -eq 124 ]; then
  echo "codex-implement.sh: timeout after ${TIMEOUT_S}s, process tree terminated." >&2
elif [ "$CODEX_EXIT" -eq 125 ]; then
  echo "codex-implement.sh: aborted early by watchdog (stalled or no startup output); see $STATUS_FILE." >&2
elif [ "$CODEX_EXIT" -ne 0 ]; then
  echo "codex-implement.sh: codex finished with exit code $CODEX_EXIT." >&2
  grep -v "caveman\|hook: .*Failed\|Skill descriptions were shortened" "$LOG_FILE" | tail -n 5 >&2
else
  echo "codex-implement.sh: ok." >>"$LOG_FILE" 2>&1
fi

exit "$CODEX_EXIT"
