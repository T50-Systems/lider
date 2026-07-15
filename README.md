# lider

A Claude Code plugin that orchestrates the T50 engineering flow, **engine-agnostic**: an architect specs and adjudicates, an implementer executes, a *different* engine reviews, and the work is promoted through pull requests. Codex (GPT) is the second engine today, with a mandatory fallback to Claude when it is unavailable. Distributed via the `t50` marketplace.

The design goal is a flow that is **resilient, observable, and self-recovering**: you always know what each engine is doing, failures surface in minutes (not at a timeout), transient errors recover automatically and safely, and no orphaned processes are ever left behind.

For the full design and rationale, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Skills

- **`/pair-review [scope]`** — independent review of the current diff with the second engine. Structured findings, hard timeout (no zombies), and a mandatory fallback to reviewing it ourselves if the second engine does not respond.
- **`/pipeline <description> [--impl codex|opus]`** — a full phase: closed architect spec → decision-density-routed background implementer → cross-engine pair-review → finding-by-finding adjudication → verification → commit → promotion. `--impl` pins the implementer and auto-assigns the *opposite* engine as reviewer.
- **`/promote [--yes] [title]`** — PR promotion: branch → PR to `dev` → merge → production gate → PR `dev`→`main` → merge → local sync.

## How it works

`/pipeline` routes work by **decision density**, not size — frontier engines are spent on judgment, mechanical engines on volume:

- **Fable** — architect: writes the closed spec and adjudicates findings against contracts/invariants.
- **Terra / Sol / Luna** (Codex) — implementers: Terra by default, Sol for open decisions/hard debugging, Luna for mechanical work.
- **Reviewer ≠ implementer** — same-engine review shares blind spots, so the reviewer is always a different engine family (Opus reviews Codex work; Codex reviews Claude work).

Every Codex invocation runs through Lider's own wrappers, which add a supervision layer around the `codex` CLI (see below). This keeps the flow fast (an isolated, minimal Codex environment), observable (live narration of what Codex is doing), and robust (watchdogs, safe auto-recovery, clean process teardown) — independent of the user's personal Codex install.

## Supervision guarantees

Both wrappers source `codex-runtime.sh`, which wraps every `codex exec` in `run_supervised`. It provides:

- **Deep observability** — a stdout heartbeat and a live `<log>.status.json` narrate what Codex is doing *right now* (`exec: <cmd>`, `edit: <file>`, `say: <message>`, `(running Ns)` for an in-flight command), plus `started_at`/`updated_at` for crash-resume. Not just a pulse.
- **Command-aware fast-fail** — inactivity and startup watchdogs abort a genuine hang as **exit 125** in minutes. The stall clock is *suspended* while a shell command runs (a build or test suite is never mistaken for a stall); the hard `timeout -k 10` bounds a runaway.
- **No zombies** — a hardened process-tree kill (`taskkill //T` on the native tree + an MSYS walk, re-enumerated) plus a `SIGINT`/`SIGTERM` trap, so killing the wrapper takes Codex — and its children — down with it.
- **Safe auto-recovery** — transient outcomes (timeouts, stalls, `429`/`5xx`/network) retry with exponential backoff + jitter. The implementer only auto-retries from a **clean-tree git checkpoint** (it resets to that checkpoint first — never a half-written re-run, never resetting a branch it did not launch on). An auth failure is reported as **actionable, not retried**.
- **Isolation** — each invocation runs against a throwaway `CODEX_HOME` (no user plugins/skills/hooks/memories/logs), so it is fast, deterministic, and unaffected by the user's global Codex setup.

## Pieces

| Path | Role |
|---|---|
| `scripts/codex-runtime.sh` | Shared supervision layer: `preflight` + `run_supervised` (heartbeat, status file, command-aware watchdog, tree-kill, classified retry, backoff, checkpoint hook). |
| `scripts/codex-home-iso.sh` | Isolation helper (sourced by the runtime): throwaway `CODEX_HOME` with only credentials + a minimal config. |
| `scripts/codex-exec.sh` | **Review** wrapper: `codex exec --sandbox read-only`, optional `--model <slug>`, `--output-schema`, validated findings JSON. |
| `scripts/codex-implement.sh` | **Implementer** wrapper: `codex exec --sandbox danger-full-access` (full read/write + network, no approvals), background-friendly (`<done>` marker), safe checkpoint auto-retry. |
| `agents/pair-reviewer.md` | Reviewer agent with a mandatory Claude fallback. |
| `schemas/findings.schema.json` | Review output contract (engine, verdict, findings). |
| `skills/{pair-review,pipeline,promote}/SKILL.md` | The three skills. |

## Exit codes (both wrappers)

| Code | Meaning | Retryable |
|---|---|---|
| `0` | ok | — |
| `124` | hard timeout (process tree killed) | yes (transient) |
| `125` | watchdog abort (stalled / died at startup) | yes (transient) |
| `127` | `codex` binary not found | no |
| `2` | bad usage / missing schema | no |
| `130` | cancelled by signal (wrapper was killed) | — |
| `3` | (review only) output JSON missing/invalid | — |
| other | codex's exit code, classified from the log tail (transient → retry; auth → actionable; else fatal) | depends |

## Configuration

Behavior is tunable via environment variables (sane defaults; all validated):

| Var | Default | Meaning |
|---|---|---|
| `CODEX_STALL_S` | 300 (implement) / 180 (review) | idle seconds (Codex not in a command) before a stall abort |
| `CODEX_STARTUP_S` | 60 | seconds with no output before "died at launch" |
| `CODEX_POLL_S` | 5 | supervisor sampling interval |
| `CODEX_HEARTBEAT_S` | 10 | heartbeat emission interval |
| `CODEX_RETRIES` | 1 (review) / 1 when safe, else 0 (implement) | retry attempts on transient failures |
| `CODEX_BACKOFF_S` | 5 | base backoff (exponential + jitter, capped at 60s) |

## Requirements

- Second review engine: **Codex CLI ≥ 0.144.1** on `PATH` (`codex --version`). Without it, `/pair-review` falls back to Claude.
- Bash (Git Bash on Windows). Shell scripts are pinned to LF via `.gitattributes`.

## Installation

From Claude Code:

```
/plugin marketplace add C:\dev\lider
/plugin install lider@t50
```
