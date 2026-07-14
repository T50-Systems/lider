# lider

A Claude Code plugin that orchestrates our workflow, **engine-agnostic**: the architect specs and adjudicates, an implementer executes, a second engine reviews (Codex today, with a fallback to Claude when it is unavailable), and the work is promoted through PRs. Distributed via the `t50` marketplace.

## Skills

- `/pair-review [scope]` — independent review of the current diff with a second engine; no zombie processes (hard timeout), structured output, and a fallback to reviewing it ourselves if the second engine does not respond.
- `/pipeline <description>` — a full phase: closed spec → background implementer → pair-review → finding-by-finding adjudication → verification → architect commit → promotion.
- `/promote [--yes] [title]` — PR promotion: branch → PR to `dev` → merge → production gate → PR `dev`→`main` → merge → local sync.

## Pieces

- `scripts/codex-exec.sh` — hardened wrapper for the second engine (timeout with escalation to SIGKILL, config isolated per invocation, validated JSON).
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
