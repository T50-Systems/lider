#!/usr/bin/env bash
# codex-exec.sh — wrapper endurecido para invocar Codex CLI sin dejar procesos zombie.
#
# Uso: codex-exec.sh <timeout_s> <out_json> <log_file> <prompt>
#
# Códigos de salida:
#   0   codex terminó ok Y <out_json> existe y es JSON parseable (la conformidad
#       con el schema la garantiza el servidor de OpenAI vía --output-schema)
#   124 timeout (el árbol de procesos de codex fue matado por `timeout`)
#   3   codex terminó con éxito pero <out_json> no existe o no es JSON válido
#   127 no se encontró el binario `codex` en PATH
#   N   cualquier otro código: se propaga el exit code de codex
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCHEMA="$SCRIPT_DIR/../schemas/findings.schema.json"

if [ "$#" -lt 4 ]; then
  echo "Uso: codex-exec.sh <timeout_s> <out_json> <log_file> <prompt>" >&2
  exit 2
fi

TIMEOUT_S="$1"
OUT_JSON="$2"
LOG_FILE="$3"
shift 3
PROMPT="$*"

# --- 1. Localizar codex ------------------------------------------------
if ! command -v codex >/dev/null 2>&1; then
  export PATH="$PATH:${APPDATA:-}/npm:${HOME:-}/AppData/Roaming/npm"
fi

if ! command -v codex >/dev/null 2>&1; then
  echo "codex-exec.sh: no se encontró el binario 'codex' en PATH (probado también \$APPDATA/npm y \$HOME/AppData/Roaming/npm)." >&2
  exit 127
fi

CODEX_BIN="$(command -v codex)"

# --- 2. Log de cabecera --------------------------------------------------
{
  echo "=== codex-exec.sh $(date '+%Y-%m-%d %H:%M:%S') ==="
  echo "codex bin: $CODEX_BIN"
  echo "codex --version: $("$CODEX_BIN" --version 2>&1)"
  echo "timeout usado: ${TIMEOUT_S}s"
  echo "schema: $SCHEMA"
  echo "out_json: $OUT_JSON"
} >>"$LOG_FILE" 2>&1

# --- 3. Ejecutar codex -----------------------------------------------------
# -k 10: si tras el timeout el proceso ignora el TERM, escala a KILL a los 10s.
timeout -k 10 "$TIMEOUT_S" "$CODEX_BIN" exec --sandbox read-only --skip-git-repo-check \
  -c "mcp_servers={}" \
  --output-schema "$SCHEMA" \
  -o "$OUT_JSON" \
  "$PROMPT" >>"$LOG_FILE" 2>&1
CODEX_EXIT=$?

# --- 4. Filtro de ruido conocido + resumen de fallo ------------------------
tail_filtered() {
  grep -v "caveman\|hook: .*Failed\|Skill descriptions were shortened" "$LOG_FILE" | tail -n 5
}

fail_summary() {
  local msg="$1"
  echo "codex-exec.sh: $msg" >&2
  tail_filtered >&2
}

if [ "$CODEX_EXIT" -eq 124 ]; then
  fail_summary "timeout tras ${TIMEOUT_S}s, árbol de procesos terminado."
  exit 124
fi

if [ "$CODEX_EXIT" -ne 0 ]; then
  fail_summary "codex terminó con exit code $CODEX_EXIT."
  exit "$CODEX_EXIT"
fi

# codex salió 0: validar que OUT_JSON existe y es JSON parseable
if [ ! -s "$OUT_JSON" ]; then
  fail_summary "codex terminó ok pero '$OUT_JSON' no existe o está vacío."
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
    fail_summary "codex terminó ok pero '$OUT_JSON' no es JSON válido."
    exit 3
  fi
elif command -v node >/dev/null 2>&1; then
  # Fallback: node está garantizado allí donde corre Claude Code.
  if ! node -e "JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'))" "$OUT_JSON" >>"$LOG_FILE" 2>&1; then
    fail_summary "codex terminó ok pero '$OUT_JSON' no es JSON válido."
    exit 3
  fi
fi

echo "codex-exec.sh: ok ($OUT_JSON válido)." >>"$LOG_FILE" 2>&1
exit 0
