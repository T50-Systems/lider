#!/usr/bin/env bash
# codex-exec.sh — hardened wrapper to invoke the Codex CLI without leaving zombie processes.
#
# Usage: codex-exec.sh [--model <slug>] <timeout_s> <out_json> <log_file> <prompt>
#
#   --model <slug>  optional: pin the Codex review model (e.g. gpt-5.6-terra).
#                   Omitted → the isolated config's default model is used.
#
# Exit codes:
#   0   codex finished ok AND <out_json> exists and is parseable JSON (schema
#       conformance is guaranteed by OpenAI's server via --output-schema)
#   124 timeout (the codex process tree was killed by `timeout`)
#   125 aborted early by a watchdog (stalled, or produced nothing at startup)
#   3   codex finished ok but <out_json> is missing or not valid JSON
#   127 the `codex` binary was not found on PATH
#   2   bad usage / required schema missing
#   N   any other code: codex's exit code is propagated
#
# A live status file is written to <log_file>.status.json throughout the run.
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCHEMA="$SCRIPT_DIR/../schemas/findings.schema.json"

# Optional leading flags (currently only --model), then positional args.
MODEL=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --model)
      if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
        echo "codex-exec.sh: --model requires a non-empty value" >&2
        exit 2
      fi
      MODEL="$2"; shift 2 ;;
    --model=*)
      MODEL="${1#--model=}"
      if [ -z "$MODEL" ]; then
        echo "codex-exec.sh: --model requires a non-empty value" >&2
        exit 2
      fi
      shift ;;
    --) shift; break ;;
    *) break ;;
  esac
done

if [ "$#" -lt 4 ]; then
  echo "Usage: codex-exec.sh [--model <slug>] <timeout_s> <out_json> <log_file> <prompt>" >&2
  exit 2
fi

TIMEOUT_S="$1"
OUT_JSON="$2"
LOG_FILE="$3"
shift 3
PROMPT="$*"
STATUS_FILE="$LOG_FILE.status.json"

# --- 1. Locate codex ---------------------------------------------------
if ! command -v codex >/dev/null 2>&1; then
  export PATH="$PATH:${APPDATA:-}/npm:${HOME:-}/AppData/Roaming/npm"
fi

if ! command -v codex >/dev/null 2>&1; then
  echo "codex-exec.sh: 'codex' binary not found on PATH (also tried \$APPDATA/npm and \$HOME/AppData/Roaming/npm)." >&2
  exit 127
fi

CODEX_BIN="$(command -v codex)"

# --- 1b. Supervision layer + isolated CODEX_HOME --------------------------
# codex-runtime.sh provides preflight/run_supervised and re-exports the
# isolation helper. A review must not inherit the user's personal Codex install.
# shellcheck source=codex-runtime.sh
. "$SCRIPT_DIR/codex-runtime.sh"
preflight "$SCHEMA" || exit $?
setup_isolated_codex_home "$(dirname "$OUT_JSON")"

# --- 2. Log header -------------------------------------------------------
{
  echo "=== codex-exec.sh $(date '+%Y-%m-%d %H:%M:%S') ==="
  echo "codex bin: $CODEX_BIN"
  echo "codex --version: $("$CODEX_BIN" --version 2>&1)"
  echo "isolated CODEX_HOME: $ISO_CODEX_HOME (from $REAL_CODEX_HOME)"
  echo "model: ${MODEL:-<config default>}"
  echo "timeout used: ${TIMEOUT_S}s"
  echo "schema: $SCHEMA"
  echo "out_json: $OUT_JSON"
  echo "status: $STATUS_FILE"
} >>"$LOG_FILE" 2>&1

# --- 3. Run codex (supervised) --------------------------------------------
# Optional model pin (empty → the isolated config's default model).
MODEL_ARGS=()
if [ -n "$MODEL" ]; then
  MODEL_ARGS=(--model "$MODEL")
fi

# run_supervised keeps `timeout -k 10` as the hard backstop and adds the
# heartbeat / status file / inactivity watchdog / tree-kill / retry on top.
run_supervised "review" "$TIMEOUT_S" "$LOG_FILE" "$STATUS_FILE" \
  "${CODEX_STALL_S:-180}" "${CODEX_STARTUP_S:-60}" "${CODEX_RETRIES:-1}" "${CODEX_BACKOFF_S:-5}" \
  -- "$CODEX_BIN" exec --sandbox read-only --skip-git-repo-check \
     -c "mcp_servers={}" --output-schema "$SCHEMA" -o "$OUT_JSON" \
     ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} "$PROMPT"
CODEX_EXIT=$?

# --- 4. Known-noise filter + failure summary -------------------------------
tail_filtered() {
  grep -v "caveman\|hook: .*Failed\|Skill descriptions were shortened" "$LOG_FILE" | tail -n 5
}

fail_summary() {
  local msg="$1"
  echo "codex-exec.sh: $msg" >&2
  tail_filtered >&2
}

if [ "$CODEX_EXIT" -eq 124 ]; then
  fail_summary "timeout after ${TIMEOUT_S}s, process tree terminated."
  exit 124
fi

if [ "$CODEX_EXIT" -eq 125 ]; then
  fail_summary "aborted early by watchdog (stalled or produced nothing at startup); see $STATUS_FILE."
  exit 125
fi

if [ "$CODEX_EXIT" -ne 0 ]; then
  fail_summary "codex finished with exit code $CODEX_EXIT."
  exit "$CODEX_EXIT"
fi

# codex exited 0: check that OUT_JSON exists and is parseable JSON
if [ ! -s "$OUT_JSON" ]; then
  fail_summary "codex finished ok but '$OUT_JSON' is missing or empty."
  exit 3
fi

if command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  PYTHON_BIN=""
fi

if [ -n "$PYTHON_BIN" ]; then
  if ! "$PYTHON_BIN" -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" "$OUT_JSON" >>"$LOG_FILE" 2>&1; then
    fail_summary "codex finished ok but '$OUT_JSON' is not valid JSON."
    exit 3
  fi
elif command -v node >/dev/null 2>&1; then
  # Fallback: node is guaranteed wherever Claude Code runs.
  if ! node -e "JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'))" "$OUT_JSON" >>"$LOG_FILE" 2>&1; then
    fail_summary "codex finished ok but '$OUT_JSON' is not valid JSON."
    exit 3
  fi
fi

echo "codex-exec.sh: ok ($OUT_JSON valid)." >>"$LOG_FILE" 2>&1
exit 0
