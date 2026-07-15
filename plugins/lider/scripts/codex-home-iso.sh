#!/usr/bin/env bash
# codex-home-iso.sh — shared helper. Source it, then call
#   setup_isolated_codex_home <anchor_dir>
#
# It points CODEX_HOME at a throwaway home so a Codex invocation does NOT inherit
# the user's personal install (plugins, skills, hooks, memories, multi-GB logs) —
# those add latency, token noise, and per-turn hook failures on every `codex exec`.
# Only credentials and a minimal config are carried over. The temp home is removed
# on process exit.
#
# After the call these globals are set for logging: REAL_CODEX_HOME, ISO_CODEX_HOME.
setup_isolated_codex_home() {
  local anchor_dir="$1"   # where to place the temp home (e.g. the session temp dir)
  REAL_CODEX_HOME="${CODEX_HOME:-${HOME:-}/.codex}"
  if [ "$REAL_CODEX_HOME" = "/.codex" ]; then
    echo "codex-home-iso: neither CODEX_HOME nor HOME is set; cannot locate credentials." >&2
    return 2
  fi
  ISO_CODEX_HOME="$anchor_dir/.codex-iso-$$"
  mkdir -p "$ISO_CODEX_HOME"
  trap 'rm -rf "$ISO_CODEX_HOME"' EXIT

  # Carry over credentials so the isolated home can still authenticate.
  cp "$REAL_CODEX_HOME/auth.json" "$ISO_CODEX_HOME/auth.json" 2>/dev/null || true

  # Minimal config: keep the user's model/tier scalars, force a non-interactive
  # policy, disable heavy features. Nothing else (plugins, hooks, memories,
  # notify, projects) is inherited.
  {
    grep -E '^(model|model_reasoning_effort|service_tier)[[:space:]]*=' \
      "$REAL_CODEX_HOME/config.toml" 2>/dev/null
    echo 'approval_policy = "never"'
    echo '[features]'
    echo 'memories = false'
    echo 'multi_agent = false'
  } >"$ISO_CODEX_HOME/config.toml"

  export CODEX_HOME="$ISO_CODEX_HOME"
}
