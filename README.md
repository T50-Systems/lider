# lider

A Claude Code plugin that orchestrates our workflow, **engine-agnostic**: the architect specs and adjudicates, an implementer executes, a second engine reviews (Codex today, with a fallback to Claude when it is unavailable), and the work is promoted through PRs. Distributed via the `t50` marketplace.

## Skills

- `/pair-review [scope]` — independent review of the current diff with a second engine; no zombie processes (hard timeout), structured output, and a fallback to reviewing it ourselves if the second engine does not respond.
- `/pipeline <description>` — a full phase: closed spec → background implementer → pair-review → finding-by-finding adjudication → verification → architect commit → promotion.
- `/promote [--yes] [title]` — PR promotion: branch → PR to `dev` → merge → production gate → PR `dev`→`main` → merge → local sync.

## Pieces

- `scripts/codex-exec.sh` — hardened **review** wrapper for the second engine (read-only sandbox, optional `--model <slug>`, timeout with escalation to SIGKILL, isolated per invocation, validated JSON).
- `scripts/codex-implement.sh` — Lider-owned **implementer** wrapper: `codex exec` with full access (`danger-full-access`, no approvals), isolated `CODEX_HOME`, background-friendly (writes its exit code to a `<done>` file for a watcher). Used by `/pipeline` so the implementer is not capped at the Codex plugin's `workspace-write`.
- `scripts/codex-runtime.sh` — shared supervision layer both wrappers source: preflight, plus `run_supervised` (stdout heartbeat + live `<log>.status.json` that narrate what Codex is doing right now — `exec:`/`edit:`/`say:` extracted from its stream — not just a pulse; inactivity/startup watchdog → fast-fail as exit 125; hardened process-tree kill with a SIGTERM trap so no orphans; bounded retry). Keeps `timeout -k 10` as the hard backstop.
- `scripts/codex-home-iso.sh` — shared helper (sourced by the runtime) to run Codex against a throwaway `CODEX_HOME` (no user plugins/skills/hooks/memories), so invocations are fast and deterministic.
- `agents/pair-reviewer.md` — reviewer agent with a mandatory fallback.
- `schemas/findings.schema.json` — output contract (engine, verdict, findings).

## Requirements

- Second review engine: Codex CLI >= 0.144.1 on PATH (`codex --version`). Without it, `/pair-review` falls back to Claude.

## Installation

From Claude Code:

```
/plugin marketplace add C:\dev\lider
/plugin install lider@t50
```
