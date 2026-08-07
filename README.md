# lider

A **multi-harness** workflow plugin for **Claude Code**, **Grok Build**, **OpenCode**, **Codex**, and **Pi**. Engine-agnostic runtime: an architect specs and adjudicates, an implementer executes, a *different* engine family reviews, and work is promoted through PRs. Shipped engine adapters: `claude` (default), `grok`, `codex`, `opencode`, `pi`, `calvoproxy` (contrast only), `generic`. Distributed via the `t50` marketplace (Claude/Grok) and `install-skills.py` for OpenCode/Pi/Codex discovery paths.

The design goal is a flow that is **resilient, observable, and self-recovering**: you always know what each engine is doing, failures surface in minutes (not at a timeout), transient errors recover automatically and safely, and no orphaned processes are ever left behind.

For the full design and rationale, see [ARCHITECTURE.md](ARCHITECTURE.md). For copy-paste prompts to drive it from another session, see [docs/USAGE.md](docs/USAGE.md).

## Skills

- **`/inception <theme> [--strict]`** — **recommended** discovery run: frame, criteria, questions, units, optional challenge, seal to `.lider/handoffs/`. Strict: challenge at seal + handoff import before implement.
- **`/pipeline <description> [--impl opus|sonnet|fable|grok]`** — construction: build spec → implement → cross-engine review → adjudicate → commit → promote. Prefer `/inception` first on non-trivial work.
- **`/schedule [--max-width N]`** — plan **parallel unit waves** from deps (`rungraph schedule`); prints worktree commands. Does not run engines; use when multi-unit sequential feels too heavy.
- **`/operate <action> [--strict]`** — **recommended** operations ledger: target → preflight → act → prove → close, plus **incident → rollback** (or forward fix) when effect fails. How to check: `/preflight` + `/verify`.
- **`/pair-review [scope]`** — independent review with the second engine family (fallback to host).
- **`/promote [--yes] [title]`** — PR promotion (often the **act** inside `/operate`).
- **`/preflight`**, **`/verify`** — establish conditions before shared-state changes; prove effect after.

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
| `claude` | agentic CLI | ✅ | ✅ | **default**; native `--json-schema`; `--bare` only with `ANTHROPIC_API_KEY` |
| `grok` | agentic CLI | ❌ | ✅ | permission *rules* for review; `--yolo` for implement |
| `codex` | agentic CLI | ✅ | ✅ full access | isolated `CODEX_HOME`; may be usage-limited on some accounts |
| `opencode` | agentic CLI | ❌ | ✅ `--auto` | `opencode run --format json` |
| `pi` | agentic CLI | ❌ | ✅ | `pi -p --mode json`; review = read-only tools |
| `calvoproxy` | chat completion | ❌ | ⛔ refused | free models, no tools; contrast/bulk only |
| `generic` | any CLI | ❌ | ✅ | `LIDER_BIN` / `LIDER_ARGS_*`; fallback for unknown ids |

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
| `scripts/rungraph.py` | **The run ledger**: the flow as an enforced state machine — legal edges, three-valued checks, reviewer≠implementer, a bounded *and converging* adjudication loop, units of work as subgraphs with dependency edges and a join barrier, spec-drift re-verification, open questions that block as `undetermined`, criterion→unit coverage, and a read-only `next`. Resumable across sessions. |
| `scripts/fanout.py` | **Fan-out**: N lenses reviewed concurrently, then N skeptics per severe claim. Counts absences as absences. |
| `scripts/reduce-findings.py` | Merges a fan-out into one round: dedup, corroboration by engine and by lens, missing-lens accounting. |
| `scripts/verify-findings.py` | Applies refutation ballots: majority rule, quorum, low-confidence discounting. |
| `scripts/lider/metrics.py` | Append-only run record (`.lider/metrics.jsonl`) — cost, tokens, outcomes, per-lens contribution. Unmeasured values stay `null`, never `0`. |
| `scripts/metrics-report.py` | Turns that record into the answers: routing, reviewer precision, which lenses earn their slot, vote count, timeouts, model drift. |
| `scripts/lider/log.py` | The three output destinations and their rules (live stdout, stderr + `LIDER_DEBUG`, and the engine-only run log). |
| `scripts/lider/extract.py` | Recovers a result payload from an engine that prints instead of writing a file (envelopes, fences, ANSI). |
| `scripts/lider/validate.py` | Local schema validation for engines with no server-side guarantee. |
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

## Tests

```bash
python -m pytest -m "not slow"          # the fast suite - run this on every change
python -m pytest                        # + the supervision tests, which drive real processes
python -m pytest --cov                  # 91% - fails below 90, see .coveragerc
```

Most tests call a CLI's `main()` **in process**. That is deliberate: `main()` is the
boundary (only the `sys.exit` wrapper sits outside it), it is far faster than a
subprocess per assertion, and coverage cannot see into a subprocess - so the CLI surface,
which is most of this codebase, used to report 0% while being thoroughly exercised. A
number that wrong is worse than no number. The supervisor and fan-out suites still spawn
real processes, because there the process boundary *is* the thing under test.

No engine is ever called: every test drives a fake engine that is a small Python
script launched with `sys.executable`. The suite is free, deterministic, and clear of
the shebang/WSL-bash trap that bit the real code twice.

**Every test encodes a defect that actually happened**, or a rule the plugin refuses to
break — a non-streaming engine killed for early silence, a payload fenced inside prose,
our own notes landing in the engine's transcript, a grandchild outliving a teardown, an
unknown cost recorded as zero. The suite is the memory of what went wrong, not decoration.

Its first run found two live bugs in cost accounting: `grok.usage()` used a flat regex
against a nested envelope, so **every Grok run had been silently recorded as
cost-unmeasured**, and `claude.usage()` substring-matched compact JSON.

## Dependencies: none at runtime, on purpose

The plugin needs **nothing installed but Python**. `rungraph.py` states that as a
constraint, and it is why hundreds of its tests run without an engine, a network or a
package. `lider/validate.py` uses `jsonschema` when present and falls back to a built-in
checker when it is not; both paths are tested, because the fallback is the one most
people hit.

Development is where the tools live — see [`requirements-dev.txt`](requirements-dev.txt).
The most useful of them is **pydantic, used only in the tests**: the three durable
formats (`run.json`, `status.json`, `metrics.jsonl`) are written as plain dicts and
validated by nothing at runtime, so [`tests/models.py`](tests/models.py) declares them as
**strict** models and checks them against what the code actually wrote. `extra="forbid"`
is the mechanism — a field added, renamed or retyped fails a test instead of surfacing in
someone's resumed session a week later. The models never ship.

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

- **Python 3** on `PATH` (stdlib only at runtime).
- At least two engine families for cross-engine review. On this install: **claude** + **grok** CLIs. Codex is optional (adapter kept; account may be usage-limited).
- Git. Shell scripts, if any remain, are pinned to LF via `.gitattributes`.

## Installation

### Grok Build

```bash
grok plugin marketplace add C:\dev\lider
grok plugin install lider --trust
grok plugin enable lider
```

### Claude Code

```
/plugin marketplace add C:\dev\lider
/plugin install lider@t50
```

### OpenCode, Pi, Codex (skill discovery paths)

```bash
# From repo root — copies plugins/lider/skills into each host's layout
python plugins/lider/scripts/install-skills.py
python plugins/lider/scripts/install-skills.py --user   # also ~/.agents, ~/.pi, etc.
set LIDER_PLUGIN_ROOT=C:\dev\lider\plugins\lider      # required so scripts resolve
```

| Host | Discovers skills from |
|---|---|
| OpenCode | `.opencode/skills`, `.agents/skills`, `.claude/skills` |
| Pi | `.pi/skills`, `.agents/skills`, `~/.pi/agent/skills` |
| Codex | `.codex/skills`, `~/.codex/skills` |

### Plugin root in scripts

```bash
LIDER="${LIDER_PLUGIN_ROOT:-${GROK_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}}"
python "${LIDER}/scripts/rungraph.py" show
```

Engines are independent of host: a Grok session can still `agent-exec --engine pi`.
