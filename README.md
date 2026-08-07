# lider

A Claude Code plugin that orchestrates the T50 engineering flow, **engine-agnostic**: an architect specs and adjudicates, an implementer executes, a *different* engine reviews, and the work is promoted through pull requests. The engines available on this install are **claude** (default) and **grok**; `calvoproxy` serves as a cheap third opinion. **Codex is not reachable on this account** - its binary is on `PATH` but every run fails on the usage limit, so its adapter is kept ready rather than routed to. Distributed via the `t50` marketplace.

The design goal is a flow that is **resilient, observable, and self-recovering**: you always know what each engine is doing, failures surface in minutes (not at a timeout), transient errors recover automatically and safely, and no orphaned processes are ever left behind.

For the full design and rationale, see [ARCHITECTURE.md](ARCHITECTURE.md). For copy-paste prompts to drive it from another session, see [docs/USAGE.md](docs/USAGE.md).

## Skills

- **`/pair-review [scope]`** — independent review of the current diff with the second engine. Structured findings, hard timeout (no zombies), and a mandatory fallback to reviewing it ourselves if the second engine does not respond.
- **`/pipeline <description> [--impl codex|opus|fable]`** — a full phase: closed architect spec → decision-density-routed background implementer → cross-engine pair-review → finding-by-finding adjudication → verification → commit → promotion. `--impl` pins the implementer and auto-assigns the *opposite* engine as reviewer; if you don't pin one, the pipeline asks which engine should implement.
- **`/promote [--yes] [title]`** — PR promotion: branch → PR to `dev` → merge → production gate → PR `dev`→`main` → merge → local sync.

## How it works

`/pipeline` routes work by **decision density**, not size — frontier engines are spent on judgment, mechanical engines on volume:

- **Fable** — architect: writes the closed spec and adjudicates findings against contracts/invariants.
- **Opus / Sonnet / Haiku** — implementers: Sonnet by default, Opus for open decisions and hard debugging, Haiku for mechanical work.
- **Reviewer ≠ implementer** — same-family review shares blind spots, so the reviewer is always a different engine family. With this roster that means **Claude implements, Grok reviews**, and `rungraph.py assign` refuses the pairing if it is not.

Every engine invocation runs through Lider's own wrappers, which add a supervision layer around whichever CLI is driving (see below). This keeps the flow fast (an isolated, minimal Codex environment), observable (live narration of what Codex is doing), and robust (watchdogs, safe auto-recovery, clean process teardown) — independent of the user's personal Codex install.

## Supervision guarantees

Both wrappers drive `lider/runtime.py`, which supervises every engine invocation. The runtime is **engine-agnostic** — everything CLI-specific lives in one adapter module. It provides:

- **Deep observability** — a stdout heartbeat and a live `<log>.status.json` narrate what the engine is doing *right now* (`exec: <cmd>`, `edit: <file>`, `tool: Bash`, `(running Ns)` for an in-flight command), plus `started_at`/`updated_at` for crash-resume. Not just a pulse.
- **Two watchdogs, each disarmable by the adapter** — the **stall** watchdog is disarmed when the adapter has no in-flight grammar; the **startup** watchdog is disarmed when the engine does not stream at all (measured: Grok emits one object at the end, so an armed startup watchdog killed every run longer than its window). Both states are published in `status.json`. Otherwise they abort a genuine hang as **exit 125** in minutes. The stall clock is *suspended* while a shell command runs (a build or test suite is never mistaken for a stall); a hard deadline on a daemon thread, independent of the poll loop, bounds a runaway even if the supervisor itself wedges. An adapter that **cannot** report in-flight state disarms the stall watchdog instead of guessing, and says so in `status.json`.
- **No zombies** — `taskkill /F /T` on the native pid (Windows) or `killpg` on a fresh session (POSIX), so the whole tree goes down exactly, with no pid-table walking to miss a reparented grandchild.
- **Safe auto-recovery** — transient outcomes (timeouts, stalls, `429`/`5xx`/network) retry with exponential backoff + jitter. The implementer only auto-retries from a **clean-tree git checkpoint** (it resets to that checkpoint first — never a half-written re-run, never resetting a branch it did not launch on). An auth failure is reported as **actionable, not retried**.
- **Isolation** — an invocation runs against a throwaway engine home where the adapter supports it (no user plugins/skills/hooks/memories/logs), so it is fast, deterministic, and unaffected by the user's global setup.
- **Verified output** — engines that enforce the schema natively are trusted; engines that merely print JSON have their payload extracted and validated locally before anything downstream reads it.

## Engines

Selected with `--engine <id>`, or `LIDER_ENGINE`. Contract and how to add one: [`scripts/lider/adapters/README.md`](plugins/lider/scripts/lider/adapters/README.md).

| id | Kind | In-flight | Implement | Notes |
|---|---|---|---|---|
| `codex` | agentic CLI | ✅ | ✅ full access | isolated `CODEX_HOME`; native `--output-schema`. **Needs a paid account this install does not have** — kept for when that changes |
| `claude` | agentic CLI | ✅ | ✅ | **default engine**; native `--json-schema`; `--bare` only when `ANTHROPIC_API_KEY` is set |
| `grok` | agentic CLI | ❌ | ✅ | review locks down with permission *rules* — its tool denylist fails open |
| `calvoproxy` | chat completion | ❌ | ⛔ refused | free models, no tools; contrast/bulk only |
| `generic` | any CLI | ❌ | ✅ | configured via `LIDER_BIN` / `LIDER_ARGS_*`; the fallback for unknown ids |

## Language split

Two languages, divided by job, not by habit:

- **Python supervises and computes.** The runtime, the adapters, the run ledger,
  the fan-out and every reduction. `subprocess` + `signal` give the process
  control directly, `taskkill /F /T` gets the *native* pid with no `ps` parsing,
  and status JSON is serialised rather than assembled with `printf`.
- **Shell is only a shim.** The `.sh` entry points exist so existing callers and
  installed plugin versions keep working; each is six lines that exec the Python.

## Pieces

| Path | Role |
|---|---|
| `scripts/lider/runtime.py` | Engine-neutral supervision: heartbeat, startup + command-aware stall watchdogs, hard-deadline thread, process-tree teardown, classified retry with backoff. |
| `scripts/lider/adapters/*.py` | One module per engine — the only place a CLI is named. `generic.py` is the reference and the fallback. |
| `scripts/agent-exec.py` | **Review** wrapper: read-only, `--engine`/`--model`, schema-bound, validated findings JSON. |
| `scripts/agent-implement.py` | **Implementer** wrapper: write access, background-friendly (`<done>` marker), safe checkpoint auto-retry. |
| `scripts/rungraph.py` | **The run ledger**: the flow as an enforced state machine — legal edges, three-valued checks, reviewer≠implementer, a bounded *and converging* adjudication loop, resumable across sessions. |
| `scripts/fanout.py` | **Fan-out**: N lenses reviewed concurrently, then N skeptics per severe claim. Counts absences as absences. |
| `scripts/reduce-findings.py` | Merges a fan-out into one round: dedup, corroboration by engine and by lens, missing-lens accounting. |
| `scripts/verify-findings.py` | Applies refutation ballots: majority rule, quorum, low-confidence discounting. |
| `scripts/lider/metrics.py` | Append-only run record (`.lider/metrics.jsonl`) — cost, tokens, outcomes, per-lens contribution. Unmeasured values stay `null`, never `0`. |
| `scripts/metrics-report.py` | Turns that record into the answers: routing, reviewer precision, which lenses earn their slot, vote count, timeouts, model drift. |
| `scripts/lider/log.py` | The three output destinations and their rules (live stdout, stderr + `LIDER_DEBUG`, and the engine-only run log). |
| `scripts/lider/extract.py` | Recovers a result payload from an engine that prints instead of writing a file (envelopes, fences, ANSI). |
| `scripts/lider/validate.py` | Local schema validation for engines with no server-side guarantee. |
| `scripts/{agent,codex}-{exec,implement}.sh` | Compatibility shims. |
| `agents/pair-reviewer.md` | Reviewer agent with a mandatory Claude fallback. |
| `schemas/findings.schema.json` | Review output contract (engine, verdict, findings). |
| `schemas/refutation.schema.json` | Refutation ballot contract. |
| `skills/{pair-review,pipeline,preflight,promote,verify}/SKILL.md` | The five skills. |

## Measuring itself

Every supervised run appends a row to `.lider/metrics.jsonl`, so the choices the skills
currently make from doctrine can be checked against what actually happened:

```bash
python plugins/lider/scripts/metrics-report.py --dir . 
```

| Section | Decision it settles |
|---|---|
| `drift` | did the engine bill the model we asked for? |
| `routing` | which engine to send work to (accept rate, cost, duration) |
| `reviewers` | whose findings are worth adjudicating (unique share) |
| `lenses` | which lenses earn their slot (unique findings per dollar) |
| `votes` | is the refutation vote count one higher than it needs to be |
| `timing` | do the timeouts and stall thresholds fit the real durations |
| `health` | how often the infrastructure, not the model, was the problem |

**A quantity that could not be measured is recorded as `null`, never `0`.** An unknown cost
and a zero cost are opposite facts; every aggregate reports how many inputs were unmeasured
rather than averaging the gap away.

The first thing this caught, on its first real run: a review launched with `--model haiku`
was billed as `claude-sonnet-5` — reproducibly, at ~$0.27 for a two-line file. The session
even reported `init.model: claude-haiku-4-5`; only the billed model exposed it.

## Exit codes (both wrappers)

| Code | Meaning | Retryable |
|---|---|---|
| `0` | ok | — |
| `124` | hard timeout (process tree killed) | yes (transient) |
| `125` | watchdog abort (stalled / died at startup) | yes (transient) |
| `127` | engine binary not found | no |
| `2` | bad usage / missing schema / adapter refused the mode | no |
| `130` | cancelled by signal (wrapper was killed) | — |
| `3` | (review only) output JSON missing, unparseable, or non-conformant | — |
| other | the engine's exit code, classified from the log tail (transient → retry; auth → actionable; else fatal) | depends |

## Configuration

Behavior is tunable via environment variables (sane defaults; all validated). The former `CODEX_*` names are still accepted as deprecated aliases.

| Var | Default | Meaning |
|---|---|---|
| `LIDER_ENGINE` | `codex` | which adapter to use when `--engine` is not passed |
| `LIDER_STALL_S` | 300 (implement) / 180 (review) | idle seconds (engine not in a command) before a stall abort; forced to 0 when the adapter has no in-flight grammar |
| `LIDER_STARTUP_S` | 60 | seconds with no output before "died at launch" |
| `LIDER_POLL_S` | 5 | supervisor sampling interval |
| `LIDER_HEARTBEAT_S` | 10 | heartbeat emission interval |
| `LIDER_RETRIES` | 1 (review) / 1 when safe, else 0 (implement) | retry attempts on transient failures |
| `LIDER_BACKOFF_S` | 5 | base backoff (exponential + jitter, capped at 60s) |
| `LIDER_SCHEMA` | `schemas/findings.schema.json` | review output contract |
| `LIDER_BIN`, `LIDER_ARGS_REVIEW`, `LIDER_ARGS_IMPLEMENT`, `LIDER_MODEL_FLAG`, `LIDER_EXTRACT_JSON` | — | `generic` adapter configuration |

## Requirements

- Second review engine: **Codex CLI ≥ 0.144.1** on `PATH` (`codex --version`). Without it, `/pair-review` falls back to Claude.
- Bash (Git Bash on Windows). Shell scripts are pinned to LF via `.gitattributes`.

## Installation

From Claude Code:

```
/plugin marketplace add C:\dev\lider
/plugin install lider@t50
```
