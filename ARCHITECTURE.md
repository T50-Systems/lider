# Lider — Architecture

This document describes how the `lider` plugin is built and, more importantly, **why**. The design target is a Codex-backed engineering flow that is **resilient, observable, self-recovering, and zombie-free** — safe enough to treat as mission-critical.

- [1. Overview](#1-overview)
- [2. Layered design](#2-layered-design)
- [3. Isolation layer](#3-isolation-layer-codex-home-isosh)
- [4. Supervision layer](#4-supervision-layer-codex-runtimesh)
- [5. The two wrappers](#5-the-two-wrappers)
- [6. Engine allocation (the pipeline)](#6-engine-allocation-the-pipeline)
- [7. Design decisions & rationale](#7-design-decisions--rationale)
- [8. Failure modes & guarantees](#8-failure-modes--guarantees)
- [9. Testing](#9-testing)

---

## 1. Overview

Lider orchestrates a phase of work across engines: **Fable** (architect) specs and adjudicates, a Codex engine (**Terra/Sol/Luna**) or **Opus** implements, and a *different* engine reviews. The three user-facing skills are `/pipeline`, `/pair-review`, and `/promote`.

The interesting engineering is not the orchestration prose (that lives in the `SKILL.md` files) but the **runtime that drives the `codex` CLI**. A bare `codex exec` call is a black box: you cannot see what it is doing, a hang costs you the whole timeout, a killed call can orphan child processes, and it inherits the user's entire personal Codex install (plugins, skills, hooks, a multi-GB log DB) on every invocation. Lider wraps that call in a supervision layer that fixes all of the above.

## 2. Layered design

Everything is plain Bash (Git Bash on Windows + POSIX), sourced bottom-up so each wrapper pulls in one file:

```
skills/{pipeline,pair-review,promote}/SKILL.md   ← orchestration (model-driven)
        │  invoke
        ▼
scripts/codex-exec.sh        scripts/codex-implement.sh   ← the two wrappers
        │  source                     │  source
        └───────────► scripts/codex-runtime.sh ◄──────────┘   ← supervision
                              │  sources
                              ▼
                      scripts/codex-home-iso.sh              ← isolation
```

- **Isolation** (`codex-home-iso.sh`) — run Codex against a throwaway home.
- **Supervision** (`codex-runtime.sh`) — `preflight` + `run_supervised`: observability, watchdogs, tree-kill, classified retry, backoff, checkpoint hook. Re-exports the isolation helper so wrappers source only this file.
- **Wrappers** — `codex-exec.sh` (review, read-only) and `codex-implement.sh` (implementer, full-access). Thin: parse args, build the `codex` command, hand it to `run_supervised`.
- **Skills** — the model-driven layer that decides *which* engine does *what* and reads the supervision signals to react.

## 3. Isolation layer (`codex-home-iso.sh`)

`setup_isolated_codex_home <anchor_dir>` points `CODEX_HOME` at a throwaway directory (`<anchor>/.codex-iso-$$`) containing only:

- a copy of the user's `auth.json` (so Codex can authenticate), and
- a minimal `config.toml` carrying over the user's `model` / `model_reasoning_effort` / `service_tier`, forcing `approval_policy = "never"`, and disabling heavy features.

**Why:** the user's real `~/.codex` can carry broken skills, failing hooks, and large state that load on *every* `codex exec` — adding latency, thousands of tokens of noise, and per-turn hook failures that push real calls toward the timeout. The isolated home makes each invocation fast, deterministic, and independent of the user's global setup. The temp home is removed on process exit (`trap … EXIT`).

## 4. Supervision layer (`codex-runtime.sh`)

`run_supervised <tool> <timeout_s> <log> <status> <stall_s> <startup_s> <retries> <backoff_s> -- <cmd…>` is the heart of the system. It launches the command in the background under a hard `timeout`, and the wrapper's own foreground becomes the monitor loop — **there is no separate monitor process to orphan.**

```
run_supervised
 ├─ validate inputs (timeouts/counts coerced to bounded ints)
 ├─ trap INT/TERM → tear down the Codex tree, write a terminal status/<done>
 └─ retry loop (bounded):
      ├─ _supervise_once  ── launch `timeout -k 10 <t> <cmd> &`, then poll:
      │     • sample log size → detect growth (idle clock)
      │     • track in-flight command state incrementally (see below)
      │     • write status.json + emit heartbeat
      │     • watchdogs: startup-fail / stall (command-aware) → exit 125
      │     • reap the child; hard timeout surfaces as exit 124
      ├─ classify outcome (_retry_class): done | retry | auth | fatal
      ├─ if retry & attempts remain: run CODEX_RETRY_HOOK (e.g. reset to
      │     a clean checkpoint); refuse retry if it can't be satisfied
      └─ exponential backoff + jitter (capped), then loop
```

### 4.1 Observability

Two synchronized outputs, updated every poll:

- **Heartbeat → stdout** (the background panel), e.g.
  `[21:11:44] codex/implement attempt=1/2 elapsed=20s idle=10s log=2287B | exec: pwsh -Command 'Start-Sleep -Seconds 40' (running 10s)`
- **`<log>.status.json`** (atomic write via temp+rename), e.g.
  ```json
  {"tool":"implement","state":"running","attempt":1,"max_attempts":2,
   "pid":12345,"elapsed_s":20,"idle_s":10,"log_bytes":2287,"exit":null,
   "reason":"","activity":"exec: … (running 10s)","started_at":…,"updated_at":…}
  ```

The `activity` field is a human read of what Codex is doing *right now*, parsed from Codex's own stream markers (`exec`, `+++ b/<file>`, `codex`, `succeeded/failed/exited in Nms`, `tokens used`). Both stdout and status are kept **out of `<log>`** so they cannot fool the inactivity watchdog, and every string is JSON-escaped (reduced to printable ASCII, so a split multibyte char or an embedded quote/backslash can never make the status file invalid JSON). `started_at`/`updated_at` let a restarted orchestrator distinguish a live run from an orphaned status left by a dead wrapper.

### 4.2 Command-aware watchdog

The naive "abort if the log hasn't grown in N seconds" watchdog has a fatal flaw: a healthy Codex running a long, silent shell command (a build, a test suite, a `sleep`) produces no output, so it looks identical to a hang and gets **falsely killed**.

The fix distinguishes the two states by reading Codex's `exec` / `succeeded|failed|exited` markers. While a command is **in flight**, the stall clock is suspended (silence is expected); the hard `timeout` remains the bound for a runaway command. A stall only fires when *Codex itself* is idle between steps.

This in-flight state is tracked **incrementally** over newly-appended log bytes, advancing only through complete lines — not recomputed from a bounded tail. That matters because a verbose command could push its opening `exec` marker out of any fixed window, flipping the state and re-introducing the false-kill; incremental tracking cannot lose it, and line-alignment means a marker split across two polls is never dropped.

### 4.3 Process-tree teardown (no zombies)

`timeout -k 10` is the **hard backstop**: even if the supervisor misbehaves, zombie behavior is never worse than a bare `timeout` call (empirically zero orphans on clean exit and on timeout).

On a watchdog abort or a signal, `_kill_tree` does more: it re-enumerates the process tree from the root on each pass (never signalling a stale, possibly-reused PID), resolves each node's Windows PID *before* signalling, and issues `taskkill //F //T` on the native tree (which the empirical external-kill test reduces to zero `codex.exe`) plus an MSYS `kill`. An `INT`/`TERM` trap runs this before the wrapper exits, so killing the wrapper never leaves Codex running.

> Accepted residual: a *pure-MSYS* grandchild reparented at the instant its parent dies can escape the PPID walk. The wrappers run `codex.exe` (native), which `taskkill //T` tears down atomically, so this does not apply to them. A full fix (process groups / job objects) is not cleanly supported on Git Bash and would risk more than it fixes.

### 4.4 Classified retry & backoff

Not every non-zero exit should retry. `_retry_class` inspects the exit code and **only the current attempt's error tail** (recorded by byte offset, so a previous attempt's `429` in the cumulative log can't misclassify a new failure):

- `124`/`125` (timeout / watchdog) → **retry** (always transient).
- `2`/`127` (bad usage / codex missing) → **fatal**.
- auth signatures (`401`, `unauthorized`, `token expired`, …) → **auth**: reported as actionable (`run codex login`), *not* retried — retrying a 401 just burns attempts.
- transient signatures (`429`, `5xx`, `ECONNRESET`, `stream disconnected`, …) → **retry**.
- anything else → **fatal** (a deterministic error recurs).

Retries use exponential backoff with jitter — `min(60, base·2^n) + rand(base)`, saturated at 60s, with the exponent and retry count bounded so nothing overflows.

### 4.5 Preflight

Before launch, `preflight` warns on a missing `auth.json` and fails fast (`exit 2`) on a missing review schema — surfacing setup problems as clear messages instead of confusing mid-run failures.

## 5. The two wrappers

Both are thin: locate `codex`, source the runtime, run preflight, set up the isolated home, write a log header, and call `run_supervised`. They differ in sandbox and recovery:

### `codex-exec.sh` — review (read-only)
`codex exec --sandbox read-only --output-schema findings.schema.json`, optional `--model <slug>`. The output is validated as JSON (`exit 3` on a malformed/missing findings file). Used by `/pair-review` and by the pipeline's Codex-reviewer path. Retries are safe (read-only), so `RETRIES` defaults to 1.

### `codex-implement.sh` — implementer (full access)
`codex exec --sandbox danger-full-access` (read/write anywhere, network on, no approvals), which lifts the `workspace-write` cap of the Codex plugin's app-server path. Designed to run in the background: a watcher polls `git status`, `<log>.status.json`, and a `<done>` marker.

**Safe auto-recovery.** Blindly re-running an implementer that died mid-write is unsafe. So auto-retry is enabled **only when the working tree is clean in a git repo at launch** — then the checkpoint is exactly `HEAD`, and recovery is a precise `reset --hard <HEAD> && clean -ffd` (+ recursive submodule restore). The reset **refuses if HEAD moved to a different branch** since launch (never destroy another branch's commits), preserves ignored inputs like `.local/` (no `-x`), and only reports success if the tree is verifiably clean again. A dirty tree or non-repo disables auto-retry and leaves recovery to the orchestrator.

## 6. Engine allocation (the pipeline)

`/pipeline` spends engines by **decision density**, not size:

| Role | Engine | Rationale |
|---|---|---|
| Architect (spec, adjudication) | **Fable** | Highest judgment, low output volume |
| Mechanical implementation | **Luna** (Codex) | Executes patterns, doesn't design them |
| Default implementation | **Terra** (Codex) | Normal features, several files |
| Open decisions / hard debugging | **Sol** (Codex) | Where judgment under uncertainty matters |
| Review | **≠ implementer** | Same-engine review shares blind spots |

The reviewer table enforces cross-engine review (Opus reviews Codex work via read-yourself; Codex reviews Claude work via `codex-exec.sh --model`). A **manual override** (`--impl codex|opus`) lets the user pin the implementer and have the *opposite* engine auto-assigned as reviewer, preserving the cross-engine rule.

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
