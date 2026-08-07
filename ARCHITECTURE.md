# Lider — Architecture

This document describes how the `lider` plugin is built and, more importantly, **why**. The design target is an engine-agnostic engineering flow that is **resilient, observable, self-recovering, and zombie-free** — safe enough to treat as mission-critical.

- [1. Overview](#1-overview)
- [2. Layered design](#2-layered-design)
- [3. Isolation](#3-isolation-adapterisolate)
- [4. Supervision layer](#4-supervision-layer-liderruntimepy)
- [5. The two wrappers](#5-the-two-wrappers)
- [6. Engine allocation (the pipeline)](#6-engine-allocation-the-pipeline)
- [7. Design decisions & rationale](#7-design-decisions--rationale)
- [8. Failure modes & guarantees](#8-failure-modes--guarantees)
- [9. Testing](#9-testing)

---

## 1. Overview

Lider orchestrates a phase of work across engines: **Fable** (architect) specs and adjudicates, a Claude model (**Opus/Sonnet/Haiku**) implements, and an engine from a *different family* reviews - **Grok** on this install, since Codex is not reachable here. The user-facing skills are `/pipeline`, `/pair-review`, `/preflight`, `/promote` and `/verify`.

The interesting engineering is not the orchestration prose (that lives in the `SKILL.md` files) but the **runtime that drives an engine CLI**. A bare `<engine> exec` call is a black box: you cannot see what it is doing, a hang costs you the whole timeout, a killed call can orphan child processes, and it inherits the user's entire personal install (plugins, skills, hooks, a multi-GB log DB) on every invocation. Lider wraps that call in a supervision layer that fixes all of the above.

That runtime is **engine-agnostic**. It supervises a process; what any particular CLI contributes — how to launch it, how to read its stream, what its errors mean — lives in a single adapter file. Adding an engine is writing one adapter; nothing else changes.

## 2. Layered design

Python does the supervising and computing; shell survives only as compatibility shims:

```
skills/{pipeline,pair-review,preflight,promote,verify}/SKILL.md   ← orchestration (model-driven)
        │  invoke
        ▼
scripts/agent-exec.py        scripts/agent-implement.py   ← the two wrappers
   (review, read-only)          (implement, writes)          (*.sh remain as shims)
        │  use                        │  use
        └───────────► scripts/lider/runtime.py ◄──────────┘   ← supervision (engine-neutral)
                              │  loads one
                              ▼
     scripts/lider/adapters/{codex,claude,grok,calvoproxy,generic}.py  ← everything engine-specific
```

- **Adapters** (`lider/adapters/<id>.py`) — the only place an engine is named. Contract in that package's `README.md`.
- **Supervision** (`lider/runtime.py`) — `Supervisor.run`: observability, watchdogs, tree-kill, classified retry, backoff, checkpoint hook.
- **Wrappers** — `agent-exec.py` (review, read-only, structured output) and `agent-implement.py` (implementer, write access). Thin: parse args, ask the adapter for the command line, hand it to the supervisor. The `.sh` names survive as six-line shims so existing callers keep working.
- **Skills** — the model-driven layer that decides *which* engine does *what* and reads the supervision signals to react.

### 2.1 The rule that shapes the contract

**An adapter that cannot report in-flight state disarms the stall watchdog.**

The watchdog kills a hung engine quickly by noticing the log stopped growing — but a healthy engine running an 8-minute test suite also stops writing. The only thing separating the two is knowing whether a command is currently open, which is what `adapter_inflight` reports. An adapter without that grammar leaves `ADAPTER_HAS_INFLIGHT=0`, the runtime sets `stall_s=0`, and the hard `timeout` becomes the only bound.

The run is slower to fail, and that is the correct trade: *"I cannot tell whether it is stalled"* is not *"it is stalled"* — the same three-outcome rule `preflight` and `verify` apply to evidence, applied to our own supervision. `status.json` carries `"stall_watchdog": 0` so a reader never mistakes an unwatched run for a watched one.

## 3. Isolation (`adapter.isolate`)

Each adapter decides what isolation means for its engine; most need none. The Codex adapter points `CODEX_HOME` at a throwaway directory containing only:

- a copy of the user's `auth.json` (so Codex can authenticate), and
- a minimal `config.toml` carrying over the user's `model` / `model_reasoning_effort` / `service_tier`, forcing `approval_policy = "never"`, and disabling heavy features.

**Why:** the user's real `~/.codex` can carry broken skills, failing hooks, and large state that load on *every* `codex exec` — adding latency, thousands of tokens of noise, and per-turn hook failures that push real calls toward the timeout. The isolated home makes each invocation fast, deterministic, and independent of the user's global setup. The temp home is removed on process exit.

The Claude adapter has the same intent and a **conditional** implementation: `--bare` strips the user's hooks, LSP, plugin sync and auto-memory, but it also restricts auth to `ANTHROPIC_API_KEY` and never reads OAuth — measured, it exits 1 with *"Not logged in"* under a normal login. So it is requested only when a key exists. A degraded reviewer beats one that cannot authenticate.

## 4. Supervision layer (`lider/runtime.py`)

`Supervisor.run(argv, timeout_s, stall_s, startup_s, retries, backoff_s)` is the heart of the system. It launches the engine as a child process and the wrapper's own foreground becomes the monitor loop — **there is no separate monitor process to orphan.**

```
Supervisor.run
 ├─ disarm the stall watchdog if the adapter has no in-flight grammar
 └─ retry loop (bounded):
      ├─ _once ── Popen(argv) + a daemon deadline thread, then poll:
      │     • sample log size → detect growth (idle clock)
      │     • track in-flight command state incrementally (see below)
      │     • write status.json + emit heartbeat
      │     • watchdogs: startup-fail / stall (command-aware) → exit 125
      │     • reap the child; the deadline surfaces as exit 124
      ├─ classify outcome: done | retry | auth | fatal
      ├─ if retry & attempts remain: run retry_hook (e.g. reset to a clean
      │     checkpoint); refuse the retry if it cannot be satisfied
      └─ exponential backoff + jitter (capped), then loop
```

**The hard deadline is a daemon thread, not a wrapping `timeout` binary.** The shell version used `timeout -k 10` so the run was bounded even if the supervisor misbehaved. Resolving a coreutils binary from Windows Python is exactly the trap that bit us twice (`bash` resolving to the WSL shim; `subprocess` unable to exec a shebang at all), so the deadline lives here instead — on a thread independent of the poll loop, which preserves the property that a wedged supervisor still gets the tree killed.

### 4.1 Observability

Two synchronized outputs, updated every poll:

- **Heartbeat → stdout** (the background panel), e.g.
  `[21:11:44] codex/implement attempt=1/2 elapsed=20s idle=10s log=2287B | exec: pwsh -Command 'Start-Sleep -Seconds 40' (running 10s)`
- **`<log>.status.json`** (atomic write via temp+rename), e.g.
  ```json
  {"engine":"codex","tool":"implement","state":"running","attempt":1,"max_attempts":2,
   "pid":12345,"elapsed_s":20,"idle_s":10,"log_bytes":2287,"exit":null,
   "reason":"","activity":"exec: … (running 10s)","stall_watchdog":1,
   "started_at":…,"updated_at":…}
  ```

The `activity` field is a human read of what the engine is doing *right now*, parsed by the adapter from its own stream markers (`exec`, `+++ b/<file>`, `codex`, `succeeded/failed/exited in Nms`, `tokens used`). Both stdout and status are kept **out of `<log>`** so they cannot fool the inactivity watchdog, and the document is produced by a real serialiser rather than assembled with `printf`, so no engine message can make the status file unparseable. `started_at`/`updated_at` let a restarted orchestrator distinguish a live run from an orphaned status left by a dead wrapper.

### 4.2 Command-aware watchdog

> Both watchdogs are adapter-disarmable, for the same reason at two moments. `has_inflight = False` disarms the **stall** watchdog (mid-run silence is unreadable); `streams = False` disarms the **startup** watchdog (early silence is unreadable). Measured: Grok writes nothing until it finishes, so an armed startup watchdog killed a real review at 129 s with an empty log. The hard timeout remains the bound in both cases, and `status.json` publishes `stall_watchdog` / `startup_watchdog` so an unwatched run is never mistaken for a watched one.

The naive "abort if the log hasn't grown in N seconds" watchdog has a fatal flaw: a healthy engine running a long, silent shell command (a build, a test suite, a `sleep`) produces no output, so it looks identical to a hang and gets **falsely killed**.

The fix distinguishes the two states by asking the adapter to read its engine's own markers (`exec` / `succeeded|failed|exited` for Codex; `tool_use` / `tool_result` events for Claude). While a command is **in flight**, the stall clock is suspended (silence is expected); the hard `timeout` remains the bound for a runaway command. A stall only fires when *the engine itself* is idle between steps - and an adapter with no in-flight grammar disarms the stall watchdog entirely rather than guess.

This in-flight state is tracked **incrementally** over newly-appended log bytes, advancing only through complete lines — not recomputed from a bounded tail. That matters because a verbose command could push its opening `exec` marker out of any fixed window, flipping the state and re-introducing the false-kill; incremental tracking cannot lose it, and line-alignment means a marker split across two polls is never dropped.

### 4.3 Process-tree teardown (no zombies)

`timeout -k 10` is the **hard backstop**: even if the supervisor misbehaves, zombie behavior is never worse than a bare `timeout` call (empirically zero orphans on clean exit and on timeout).

On a watchdog abort or a signal, `kill_tree` takes the whole tree down exactly: on Windows `taskkill /F /T` against the child's **native** PID (Python holds it directly, so there is no `ps` parsing and no MSYS-to-Windows PID mapping to get wrong), and on POSIX `killpg` against a process group the child was started in via `start_new_session`. A `KeyboardInterrupt` handler runs this before the wrapper exits, so killing the wrapper never leaves an engine running. Verified: after a hard timeout on a hung engine, zero survivors.

> This is one of the two places the Python port is strictly stronger than the shell original, which had to walk PPIDs and could in principle miss a grandchild reparented at the instant its parent died. Owning the native PID removes that failure class rather than mitigating it.

### 4.4 Classified retry & backoff

Not every non-zero exit should retry. `_retry_class` inspects the exit code and **only the current attempt's error tail** (recorded by byte offset, so a previous attempt's `429` in the cumulative log can't misclassify a new failure):

- `124`/`125` (timeout / watchdog) → **retry** (always transient).
- `2`/`127` (bad usage / engine missing) → **fatal**.
- the **adapter** speaks first (it knows its own error strings), then the generic vocabulary below.
- auth signatures (`401`, `unauthorized`, `token expired`, …) → **auth**: reported as actionable with the adapter's own remediation hint, *not* retried — retrying a 401 just burns attempts.
- transient signatures (`429`, `5xx`, `ECONNRESET`, `stream disconnected`, …) → **retry**.
- anything else → **fatal** (a deterministic error recurs).

Retries use exponential backoff with jitter — `min(60, base·2^n) + rand(base)`, saturated at 60s, with the exponent and retry count bounded so nothing overflows.

### 4.5 Preflight

Before launch, `agent_preflight` fails fast (`exit 2`) on a missing review schema, then delegates to `adapter_preflight` for engine-specific checks (missing credentials, an unreachable local service) — surfacing setup problems as clear messages instead of confusing mid-run failures.

## 5. The two wrappers

Both are thin: load the adapter, locate the binary, run preflight, isolate, write a log header, ask the adapter to build the command line, and call `run_supervised`. Both take `--engine <id>`. They differ in access level and recovery:

### `agent-exec.sh` — review (read-only)
The adapter builds a read-only invocation bound to `findings.schema.json`. Engines that enforce the schema themselves (`adapter_native_schema`) are only checked for parseability; engines that merely print JSON get their payload lifted out of the log (`adapter_extract`) and **validated locally** against the schema (`validate-json.py`) — otherwise everything downstream would trust an unvalidated blob. `exit 3` on missing / malformed / non-conformant output. Used by `/pair-review` and the pipeline's cross-engine reviewer path. Retries are safe (read-only), so `RETRIES` defaults to 1.

### `agent-implement.sh` — implementer (write access)
The adapter builds a full-access invocation. On Codex that is `--sandbox danger-full-access` (read/write anywhere, network on, no approvals), lifting the `workspace-write` cap of the Codex plugin's app-server path. Designed to run in the background: a watcher polls `git status`, `<log>.status.json`, and a `<done>` marker. An adapter may **refuse** the mode outright (`calvoproxy` does — it has no filesystem), which is reported as `exit 2` rather than a run that silently does nothing.

**Safe auto-recovery.** Blindly re-running an implementer that died mid-write is unsafe. So auto-retry is enabled **only when the working tree is clean in a git repo at launch** — then the checkpoint is exactly `HEAD`, and recovery is a precise `reset --hard <HEAD> && clean -ffd` (+ recursive submodule restore). The reset **refuses if HEAD moved to a different branch** since launch (never destroy another branch's commits), preserves ignored inputs like `.local/` (no `-x`), and only reports success if the tree is verifiably clean again. A dirty tree or non-repo disables auto-retry and leaves recovery to the orchestrator.

## 5b. The run ledger (`rungraph.py`)

The supervision layer makes a single *process* observable and bounded. The ledger does the same for the *flow*.

`pipeline/SKILL.md` describes a graph — nodes, loop-backs, invariants — in prose. Prose is honoured, not enforced: nothing counted adjudication rounds, nothing checked that the reviewer differed from the implementer, and the closed spec lived only in the conversation that produced it. `rungraph.py` turns that graph into data and its rules into guards.

```
rungraph.py init | spec | assign | check | findings | adjudicate | enter | gate | show
                                                                    ▲
                                              every transition goes through this guard
```

- **The graph is data.** `GRAPH` is a node → allowed-successors table. A transition that is not an edge is refused, naming the legal ones. Changing the flow means editing that table.
- **`undetermined` is a type, not a paragraph.** `check --verdict ok|not-ok|undetermined`; the third blocks forward edges exactly like a failure. The standing rule repeated across three skills is implemented once, here.
- **The engine invariant is checked, not honoured.** A same-family reviewer is refused at `assign` — before the tokens are spent. An *unknown* family is refused as `undetermined`, because not knowing is not the same as being different.
- **The adjudication loop is bounded AND must converge.** Each return-to-implementer opens a round; rounds are capped, and each must leave strictly fewer open BLOCKER/MAJOR findings than the last. A round counter alone never notices a loop that stops shrinking.
- **It survives the session.** State is written atomically to `<repo>/.lider/runs/<id>/run.json` (self-gitignored). A resumed orchestrator runs `show` and learns the node, the spec hash, the roles, the open findings, and every forced override.

Exit codes match the rest of the plugin: `0` ok, `1` refused, `2` undetermined. `--force` overrides one guard and is recorded as forced in the ledger.

## 6. Engine allocation (the pipeline)

`/pipeline` spends engines by **decision density**, not size:

| Role | Engine | Rationale |
|---|---|---|
| Architect (spec, adjudication) | **Fable** | Highest judgment, low output volume |
| Mechanical implementation | **Luna** (Codex) | Executes patterns, doesn't design them |
| Default implementation | **Terra** (Codex) | Normal features, several files |
| Open decisions / hard debugging | **Sol** (Codex) | Where judgment under uncertainty matters |
| Review | **≠ implementer** | Same-engine review shares blind spots |

The reviewer table enforces cross-engine review (Opus reviews Codex work via read-yourself; Codex reviews Claude work via `codex-exec.sh --model`). A **manual override** (`--impl codex|opus|fable`) lets the user pin the implementer and have the *opposite* engine auto-assigned as reviewer, preserving the cross-engine rule. When no engine is pinned, the pipeline asks which one to use before implementing.

## 7. Design decisions & rationale

Several of these were hardened through adversarial pair-review (Codex/Sol) — the "why nots" are as important as the "whys":

- **Keep `timeout -k 10` even with a custom supervisor.** It is the guaranteed backstop; the supervisor only ever makes teardown *better*, never worse.
- **Heartbeat to stdout, status to a side file — never to `<log>`.** Writing to the measured log would make the inactivity watchdog blind to its own writes.
- **Command-aware stall via incremental parsing, not a tail window.** A window loses the `exec` marker for verbose commands and re-introduces the false-kill.
- **Auto-retry the implementer only from a clean checkpoint, gated on same-branch.** A reset that could move another branch's ref (destroying commits) is unacceptable in mission-critical; the clean-tree precondition makes the checkpoint exact and the recovery precise.
- **Do NOT re-copy `auth.json` between attempts.** OAuth refresh tokens rotate inside the isolated home during an attempt; copying the older token back could itself induce a 401. Copy once; classify auth failures as actionable instead.
- **Classify retries from the current attempt's error tail only.** The cumulative log means a prior attempt's transient signature (or prompt/code text) could otherwise misclassify a deterministic failure.
- **Bound everything.** Retry counts, backoff, and all numeric config are coerced to bounded integers (rejecting overflow-length values) so arithmetic can't wrap or fail under `set -u`.

## 8. Failure modes & guarantees

| Failure | What happens | Guarantee |
|---|---|---|
| Codex hangs (model idle) | stall watchdog → `125` | detected in minutes, not at timeout |
| Codex runs a long silent command | in-flight → stall suspended | healthy command **not** killed; hard timeout bounds a runaway |
| Codex dies at launch | startup watchdog → `125` | detected within `CODEX_STARTUP_S` |
| Transient API/network error (`429`/`5xx`) | classified → retry w/ backoff | auto-recovers, bounded |
| Auth failure (`401`) | classified → actionable message | not retried; user told to `codex login` |
| Wrapper killed (orchestrator abort) | `INT`/`TERM` trap → tree-kill + terminal status/`<done>` | **no orphaned `codex.exe`** |
| Timeout kill | `timeout -k 10` process-tree kill | **no orphans** (empirically verified) |
| Implementer fails mid-write, tree clean | reset to checkpoint → retry | never a half-written re-run |
| Implementer fails, tree dirty / switched branch | auto-retry disabled / reset refused | never destroys pre-existing or other-branch work |
| Orchestrator restarts | reads `state` + `updated_at` + `<done>` | live run re-attached; orphaned status treated as failed |

## 9. Testing

The runtime is exercised end-to-end against the real Codex CLI (Windows Git Bash) and with deterministic synthetic harnesses for the watchdog/kill/retry logic. Covered: happy path (review + implement), hard timeout (`124`), stall and startup-fail (`125`), **the false-stall regression** (a healthy long command must complete — the primary acceptance criterion), in-flight tracking across a verbose→silent transition and across split lines, external-kill leaving zero `codex.exe`, cancellation writing `<done>`/status, the classifier (transient/auth/fatal, per-attempt), bounded backoff, the clean-vs-dirty checkpoint gate, the branch-switch reset refusal, JSON validity of the status file under adversarial output, and full backward compatibility of the wrapper interfaces.

The layer went through multiple adversarial Codex/Sol pair-review passes; every confirmed finding was fixed and re-verified.
