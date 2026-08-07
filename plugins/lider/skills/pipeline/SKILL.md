---
name: pipeline
description: "Run a full phase of the T50 flow - closed architect spec, decision-density-routed implementer, independent review by a different engine, adjudication, verification, PR promotion, with cost-aware engine allocation. Use for scoped features with an optional final human sign-off."
argument-hint: "<phase or feature description> [--impl opus|sonnet|fable|grok]"
---

You act as the architect. Follow the flow in order; do not skip steps.

## Standing rule for every step: "I could not check" is NOT "it is fine"

Every check in this flow — yours, the implementer's, the reviewer's — has **three** outcomes,
never two: `ok`, `not ok`, and **`could not determine`**. The third stops the flow. It does not
pass it, and it is never rounded down to the second.

This is not pedantry; it is the failure mode that gets *believed*. Measured in one session:
a lock reader that could not launch its CLI returned empty and a dashboard drew **"all clear"
while another session held the production deploy lock mid-deploy**; a reachability probe used
an exit code that is also returned for "empty", so it would have failed open almost always; and
a CI watcher read an API error as "not pending" and reported **"CI finished"**.

Applied concretely: a verifier that answers "not present" when it actually failed to look is
worse than no verifier. Prefer tools that reserve a distinct exit code (commonly `2`) for
"I could not look", and treat that code as **stop**. When you write one, give it that code.

**When the work touches production or a shared environment, run `preflight` before step 2 and
`verify` after step 5.** `pipeline` establishes that you built the right thing; those two
establish that you were allowed to ship it and that it actually arrived.

## The run ledger — this flow is enforced, not remembered

Everything below used to be prose you were trusted to follow. It is now a state machine you
move through, so the rules hold even when the session that read them is gone:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/rungraph.py" <command> [--run <id>]
```

| Command | When |
|---|---|
| `init --title "<phase>"` | before step 1 |
| `spec --file <path>` | step 1, once the closed spec is written to a file (it is hashed and pinned) |
| `assign --role implementer\|reviewer\|architect\|challenger --engine <e> [--model <m>]` | steps 1B/2/3 |
| `check --name <n> --verdict ok\|not-ok\|undetermined --evidence "<line>"` | every check anywhere in the flow |
| `findings --file <findings.json>` | step 3, on the reviewer's output |
| `adjudicate --finding <id> --decision accept\|fix\|return\|respec\|reject\|escalate` | step 4, one per finding |
| `enter <node>` | **every transition** — the guard |
| `gate <node>` | "would this be allowed?" without moving |
| `criterion add --id AC1 --text "..."` / `criterion defer --id AC1 --reason "..."` | step 1, the acceptance criteria as ledger objects |
| `question add --text "..." [--unit X]` / `question resolve --id q1 --status answered\|assumed --answer "..."` | any time an input is not established |
| `unit add --id X --covers AC1,AC3 [--depends-on Y]` | step 1, once the spec is split |
| `next` | **read-only** — what could run right now, and how wide the concurrency is |
| `show` | **first thing to run when resuming** — node, spec text, roles, criteria, questions, units, open findings |

Nodes: `spec → challenge? → preflight? → implement → review → adjudicate → verify → commit →
promote → effect → done`, plus the loop-backs `adjudicate → implement` / `adjudicate → spec`
and the exits `blocked` / `escalated`.

**Exit codes are the same three outcomes as everything else in this plugin: `0` ok, `1`
refused, `2` undetermined.** Do not paper over `1` or `2` — they mean a rule said no.

Four rules it enforces so you do not have to remember them:

- **Illegal transitions are refused**, and it names the legal ones. You cannot reach `commit`
  without passing through verification.
- **`undetermined` blocks exactly like `not-ok`.** The standing rule at the top of this file
  is implemented here once instead of being restated at every step.
- **Reviewer ≠ implementer family.** A same-family review is refused; an *unknown* family is
  refused as `undetermined`, because not knowing is not the same as being different.
- **The adjudication loop is bounded AND must converge — by identity, not by count.** Each
  return-to-implementer opens a round. A defect keeps ONE identity across rounds, so the guard
  refuses when **the same BLOCKER survives three rounds** (the implementer is not fixing it)
  or when **every severe defect open last round is still open** — even if the raw count fell
  because two were fixed and one was introduced. Counting cannot tell those apart. `show`
  lists them under `STUCK`. Escalate instead (`enter escalated`).

### The checkable half of Inception

Most of what an upstream design phase produces — personas, component narratives, requirements
prose — has **no checkable predicate**, so it stays prose you write. Three things do, and only
those are machinery:

- **The spec is re-verified, not just pinned.** `enter implement` re-hashes the file: changed
  since you pinned it → refused (re-pin, or `--force`); unreadable → **`undetermined`**. This is
  the only new guard that checks a declaration against a fact from *outside* the ledger.
- **Open questions block as `undetermined`, not as failures.** An unanswered input is literally
  an unestablished one. You may proceed on an assumption — but `--status assumed` **requires
  `--answer`**: an assumption nobody recorded is indistinguishable from a fact nobody checked.
- **Coverage is a checked relation.** `enter plan` refuses while a required criterion is covered
  by no unit; a unit that covers nothing (once criteria exist) is unplanned scope; deferring a
  criterion **requires a reason**, the same way a dropped unit makes a descope visible.

**Read the limit, and repeat it when you report:** coverage is *self-attestation*. The same
orchestrator writes the criteria and declares which unit covers them, so the gate verifies
**bookkeeping consistency, not that anything was implemented**. It catches a requirement dropped
by never declaring a unit for it — a real and otherwise invisible error — and nothing more. Do
not let a form check read as a substance check.

### `next` — advisory, never authority

`rungraph.py next` reports what is eligible now and what blocks the rest. It **decides nothing
and acts on nothing**; the model still launches the work and every transition still passes the
guard. Its other job is to record how many units were eligible *concurrently*, so the question
"should the ledger become a scheduler?" gets answered against measured parallelism rather than
assumed parallelism.

`--force` overrides any single guard and is **recorded in the ledger as forced**. Use it when
you have a real reason and say what it was; never to make a refusal go away quietly.

### Units of work — when a phase is more than one thing

Step 1 tells you to *split the feature into implementable units*. That sentence now has a
representation: **each unit is its own subgraph**, with its own implementer, reviewer,
findings, rounds and convergence. Without it a three-unit phase is one flat run and the
ledger cannot say which unit is stuck.

```bash
rungraph.py unit add --id auth --title "login flow"
rungraph.py unit add --id api  --title "endpoints" --depends-on auth
rungraph.py enter plan

# each unit walks its own cycle, independently
rungraph.py enter implement --unit auth
rungraph.py enter review    --unit auth
rungraph.py findings --unit auth --file <round.json>
rungraph.py enter adjudicate --unit auth
rungraph.py enter done      --unit auth      # refused while its BLOCKERs are undecided

rungraph.py enter join                        # the barrier
```

Unit nodes: `pending → implement → review → adjudicate → done`, plus the loop-back
`adjudicate → implement` and the exits `escalated` / `dropped`.

Three rules it enforces that prose could not:

- **A unit may not start before what it depends on has finished.** Otherwise the dependency
  is a comment and the work lands in an order nobody chose.
- **`join` will not open while any unit is open**, and it names them with their node.
  `dropped` counts as terminal — descoping is a legitimate decision, and one that must be
  visible rather than silent.
- **A dropped unit does not hide its findings.** `verify` still refuses over an undecided
  BLOCKER inside it, reported as `unit/finding-id`.

The convergence rules are the same ones, applied per unit: a defect surviving three rounds
*inside a unit* stops that unit, not the whole phase.

**A phase that is genuinely one unit does not declare any** — the flat path
(`spec → implement`) is unchanged.

## Fan-out — many lenses, then many skeptics

One reviewer is one opinion. For anything beyond a routine ticket, step 3 should be a fan-out:

```bash
# 1. N lenses at once, ideally across engine families
python "${CLAUDE_PLUGIN_ROOT}/scripts/fanout.py" --out <dir> review \
  --scope "<the diff / what to review>" \
  --lens correctness:claude:opus --lens security:grok --lens regression:claude:sonnet

# 2. then put every BLOCKER/MAJOR to independent skeptics
python "${CLAUDE_PLUGIN_ROOT}/scripts/fanout.py" --out <dir> refute \
  --round <dir>/round.json --votes 3 --engines claude,grok
```

Built-in lenses: `correctness`, `security`, `regression`, `concurrency`, `performance`,
`conventions`, `tests`. Any other name works as a free-form lens.

**Let discovery converge, do not guess at N.** One wave of lenses is an arbitrary amount of
looking. `--until-dry K` keeps running waves until **K consecutive rounds turn up nothing the
earlier rounds had not already reported** — and each wave tells its lenses what is already
known, so a second round looks somewhere new instead of restating the first.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/fanout.py" --out <dir> review   --scope "<the diff>" --until-dry 2 --max-rounds 5   --lens correctness:claude:opus --lens security:grok
```

If the `--max-rounds` cap is hit while findings are still arriving, the round is marked
**incomplete** (`coverage: undetermined`, exit `2`) and says so. A bounded search reported
without its bound reads as an exhaustive one.

**Let the record pick the lenses.** `--prune-lenses` consults `.lider/metrics.jsonl` and skips
lenses with a sustained zero unique-findings history — announced, never silent, and never on
fewer than `--prune-min-runs` (default 3) recorded runs. Too little data is not evidence of
uselessness.

**Do not skip the refute pass.** A fan-out of finders raises recall and *lowers* precision:
more eyes produce more plausible-but-wrong findings, and those cost exactly the judgment this
flow is built around. The skeptics are told to refute, a claim a majority refutes is dropped,
and a low-confidence "I could not tell" never counts as a refutation.

**Read `coverage` before you read the findings.** Both commands exit `2` when a lens or a
ballot produced nothing, and mark the round `undetermined`. That is the fan-out version of the
standing rule: five lenses of which two crashed is not broad coverage, it is three lenses and
two unanswered questions. Say which lenses did not run; do not present the round as complete.

Feed the reduced round into the ledger with `rungraph.py findings --file <round>.verified.json`.
Corroboration counts (`engines` vs `lenses`) travel with each finding — a defect two engine
families found independently is stronger evidence than one found twice by one family.

## Engine & model allocation

### What this install can actually reach

**Codex is NOT available on this account.** Its binary is on `PATH`, so it *looks*
installed and `locate()` succeeds — but every run fails with `You've hit your usage limit`.
Do not route to it, and do not read that failure as a bug in the wrapper. The default engine
is therefore **claude**; the Codex adapter is kept for the day access returns.

That leaves two engine families for the cross-engine rule, which is what matters:

| Family | Engine | Role it can play |
|---|---|---|
| **anthropic** | `claude` (Fable / Opus / Sonnet / Haiku) | architect, implementer, reviewer |
| **xai** | `grok` | reviewer (verified lockdown), challenger; implementer only with explicit approval |
| **openrouter** | `calvoproxy` | contrast and bulk only — free models, no tools, **cannot implement** |

**Reviewer ≠ implementer family is still the rule**, and with this roster it has exactly one
natural shape: **Claude implements, Grok reviews.** `rungraph.py assign` enforces it and will
refuse a same-family pairing before the tokens are spent.

Core idea, restated for this roster: **Fable decides direction; Opus or Sonnet builds; Grok
challenges and reviews; Fable adjudicates.**

Frontier models are expensive on OUTPUT — spend them on judgment, not volume. Route implementation by **decision density, not size**. **Never use any Fast mode.** If a step's engine is unavailable, say so and fall back rather than silently pairing same-family.

**Architect seat (steps 1 & 4 — spec, adjudication): Fable.** Low output, highest judgment. Adjudicate against contracts / invariants / acceptance criteria / authorized risks / scope — not "who seems right."

**Step 1B challenger (optional): Grok** (`agent-exec.py --engine grok`, effort pinned high by the adapter). Activate ONLY for high-risk features — security/authorization, concurrency, transactions, data migrations, architectural changes, external contracts, financial logic, high ambiguity, large blast radius. A different family is the point: it tries to break the plan (false assumptions, unhandled states, races, incompatibilities, rollback difficulty, missing observability). Skip for routine tickets.

**Manual engine override (takes precedence over the routing below).** The user may pin the implementer: `--impl opus` / `--impl fable` / `--impl sonnet`, or `--impl grok`, or in words ("implementa con opus"). When set, it overrides decision-density routing for step 2 and **forces the reviewer to the other family** in step 3:

| Requested implementer | Step 2 implementer | Step 3 reviewer (other family) |
|---|---|---|
| **opus** / **fable** / **sonnet** | a background `general-purpose` subagent at that model, implementing from the closed spec (does NOT commit; reports deviations) | **Grok** — `agent-exec.py --engine grok`: Lider-owned, read-only by permission RULES, findings schema |
| **grok** | `agent-implement.py --engine grok` (`--yolo`). **Requires explicit user approval** — letting Grok write is a separate boundary, not implied by picking it as an engine. Prefer a dedicated worktree. | **Opus** — review the diff yourself as Opus (NOT `pair-review`) |

**If the user did NOT pin an implementer, ASK before launching step 2** — offer **opus** (judgment-heavy), **sonnet** (default workhorse), or **fable** (highest judgment, lowest volume), with the one-line trade-off. Grok as implementer is offered only if the user raises it, and then only with the write-approval question attached. Skip the question only if the request already makes the engine unambiguous.

<details>
<summary>If Codex access is restored — the roster and its traps, kept rather than relearned</summary>

**Allowed Codex models (this roster only):** `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`. Do NOT use `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, or `gpt-5.3-codex-spark` (Spark) — Spark is interactive/supervised and does not fit this skill's autonomous background-implementer flow. `codex-auto-review` is the review-only model reached through `pair-review` — never a selectable implementer.

Routing within the family: **Luna** mechanical, **Terra** default, **Sol** open decisions / hard debugging / repeated Terra failures. With Codex back, `codex` and `claude` become the natural cross-family pair and Grok returns to being the third opinion.
</details>

**Step 2 implementer — route by decision density:**

| Task shape | Model | How |
|---|---|---|
| Mechanical / repetitive with a defined pattern (renames, scaffolding, fixtures, config, docs, lint/type fixes, tests from a case table) | **Haiku** — executes patterns, does not design them | `general-purpose` background subagent |
| Normal feature, several files, clear-enough requirements | **Sonnet** (DEFAULT implementer) | `general-purpose` background subagent |
| Open decisions, high impact, hard debugging, unknown root cause, repeated Sonnet failures | **Opus** | `general-purpose` background subagent |
| A deliberately different family is wanted, and the user approved writes | **Grok** | `agent-implement.py --engine grok` |

Do not escalate by size — escalate by decision density. The implementer does not decide architecture and does NOT commit; it reports deviations with a reason.

**Invoking an external implementer (Lider-owned, full access).** When the implementer is an external CLI rather than a Claude subagent, run it through this plugin's wrapper — never through another plugin's app-server path, which typically caps at `workspace-write` (no writes outside the repo, no network) and inherits the user's personal config. The wrapper runs the engine with full access in an isolated home where the adapter supports it: read/write across the filesystem, network on, no approvals, and none of the user's plugins/skills/hooks/memories bloating the run.

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/agent-implement.py" --engine <id> <timeout_s> <log> <done> <model> "<prompt>"
```
- **Launch it DIRECTLY with the Bash tool's background mode — do NOT redirect or swallow its stdout.** The wrapper streams the engine's own output to `<log>` internally and prints a heartbeat to ITS stdout every ~10s; as a background task that heartbeat is what narrates the run in the panel. `<log>`/`<done>` are temp files in the session temp dir; the wrapper writes the final exit code to `<done>` when done.
- The wrapper runs in the **current working directory** — `cd` into the intended repo/worktree first (an isolated engine home does not change where the task writes).
- **Pass an explicit model** unless the adapter documents a safe default. Never a Fast/priority tier.

**VISIBILITY IS MANDATORY — the user must be able to see the run at all times. This is where past runs failed; it is not optional:**
- ✅ **Launch `agent-implement.py` directly in background** and leave its stdout alone → its ~10s heartbeat (`<engine>/implement … | exec: … (running Ns)`) streams live into the task panel.
- ❌ **NEVER wrap it in "run, then dump the log at the end"** (e.g. capturing stdout to a file and `cat`-ing it on exit). Lider's own fan-out shipped this bug once and was silent for its whole duration. That hides everything until the task finishes — the exact failure to avoid.
- ❌ **NEVER build a separate blind anti-hang loop** (`Start-Sleep 780; check …`). The wrapper already self-supervises — its command-aware watchdog fast-fails a real hang as exit 125. A manual long sleep is both redundant AND blind. Forbidden.
- ✅ **Surface progress to the MAIN thread every ~1–2 min:** read `<log>.status.json` and report ONE line to the user — `state` + `activity` + `idle_s` (e.g. `Terra: running · editing stat-tile.tsx · idle 3s`). The status file is rewritten every poll and is always readable, so the user sees progress without expanding the panel.
- If you cannot see live output, the launch is wrong (stdout swallowed, or the wrapper not used) — **fix the launch; do not proceed blind.**

**⚠️ Full access is real.** `danger-full-access` lets the implementer write anywhere on disk and use the network with no confirmation. That is intended (it removes the `workspace-write` confinement below), but scope the task prompt tightly and keep the watcher armed.

**Execution constraints (plan around them):**
- **No filesystem confinement.** With full access the implementer can read/write outside the repo and hit the network — the old `workspace-write` limitation (out-of-repo specs unreadable, task silently doing zero work) NO LONGER applies. Keeping inputs in `<repo>/.local/` (gitignored) is still tidy, but not required for the task to see them.
- **The task can start in the wrong checkout** when multiple checkouts/worktrees of the repo exist (observed: a review task began in a sibling worktree and had to self-correct). State the exact working directory in the prompt AND require the task to verify `git branch --show-current` matches the intended branch before touching anything.

**Operational rules — the wrapper self-supervises (heartbeat + watchdog + status file + safe auto-recovery); your job is to read it and react:**
1. **Read `<log>.status.json`** for live state — `{state, elapsed_s, idle_s, log_bytes, exit, reason, activity, started_at, updated_at}`. `state` moves `starting → running → done|failed`; **`activity`** narrates what the engine is doing *right now* (`exec: <cmd>`, `edit: <file>`, `say: <message>`, `finalizing`), and shows `(running Ns)` while a shell command is in flight. The stdout heartbeat (~10s) mirrors it, so the panel narrates the run. For the full trace, tail `<log>`.
2. **Fast-fail, without false alarms.** The stall watchdog is **command-aware**: while the engine runs a shell command (a build, a test suite) its silence is expected, so the stall clock is *suspended* — a healthy long command is never killed; the hard `timeout` bounds a runaway one. A true stall (the engine idle *between* steps) or dead-at-launch still aborts as **exit 125** in minutes.
3. **React to `<done>` / exit code** (mirrored in `status.json`): `0` ok; `124` hard timeout; `125` watchdog abort; `127` engine missing; other = the engine's exit. **Auto-recovery is built in:** transient outcomes (timeouts, stalls, `429`/`5xx`/network) retry with exponential backoff; the **implementer only auto-retries when the tree was clean at launch** (it resets to that checkpoint first — no half-written re-runs), and an **auth failure is reported as actionable, not retried**, with the adapter's own remediation hint. When auto-retry is exhausted or disabled, inspect `git status --short` and decide recovery per the spec.
4. **Resume after an orchestrator restart.** If you find a `status.json` you did not just launch: `state=done|failed` → terminal, act on `exit`. `state=running|starting` → check `updated_at`: fresh (within a few poll intervals) → still alive, re-attach; stale + no `<done>` → the wrapper died orphaned, treat as failed and recover.
5. **Cross-check `git status --short`** for real file activity — ground truth that work is landing, complementing the status file.

**Step 3 reviewer — MUST differ from the implementer (same-engine review shares blind spots):**

| Implementer | Reviewer | Mechanism |
|---|---|---|
| Claude — Haiku / Sonnet / Opus / Fable | **Grok** | `agent-exec.py --engine grok` (Lider-owned, read-only by permission RULES, findings schema) |
| Grok | **Opus** — on critical code: Opus + focused human review | review the diff yourself as Opus (NOT `pair-review`) |

Record the pairing with `rungraph.py assign` BEFORE launching the review: it refuses a same-family reviewer there, while the refusal is still free. An engine whose family it cannot identify is refused as `undetermined` — not knowing is not the same as being different.

For anything beyond a routine ticket, make step 3 a **fan-out** (see that section): several lenses at once across both families, then a refutation pass. One reviewer is one opinion. `calvoproxy` can join as a cheap third opinion on bulk lenses, but never as the deciding one — free models are for contrast, not authority.

**Steps 5–8 (verify, commit, promote, close-out): direct tools first.** Run tests / lint / typecheck / build / migrations / coverage with tools, never with a model. Use **Haiku** for mechanical follow-up (commit messages, changelog, decision log, docs, simple result interpretation, close-out summary, cheap read-only repo searches via `Explore` subagents); **Sonnet** only when the mechanical work needs multi-step coordination or light judgment. Never spend frontier tokens here.

## Flow

**Before step 1:** `rungraph.py init --title "<phase>"`. **On resuming any run:** `rungraph.py show`
first — it tells you the node, the pinned spec, who played each role, and what is still open.
Every step below ends with the corresponding `enter <node>`; if it refuses, that is the answer.

1. **Closed spec.** Architect seat (Fable) — the most important deliverable. If the user's description is ambiguous in scope, ask BEFORE launching anything. Identify decisions, define limits, establish contracts and invariants, specify acceptance criteria, split the feature into implementable units, and flag reversible vs irreversible risks. Fill in this template:
   - **Scope:** exact files/packages that may be touched; what NOT to touch.
   - **Hard constraints:** repo conventions (typing, style, testids, i18n...), "do NOT commit".
   - **Design:** decisions already made, with concrete values (the implementer does not decide architecture; it does report deviations with a reason).
   - **Mandatory verification:** exact commands (typecheck/build/tests) that must pass before finishing.

   Write the finished spec to a file and pin it: `rungraph.py spec --file <path>`, then `rungraph.py enter spec`.

   Classify risk. For high-risk features only, run **step 1B** — have GPT-5.6 Sol pressure-test the plan (see allocation) before implementing.

2. **Implementer.** Record the engine (`rungraph.py assign --role implementer --engine <e> --model <m>`) and `enter implement` BEFORE launching — the guard refuses if a preflight check is failing or undetermined. If the user pinned an implementer (`--impl opus|sonnet|fable|grok`), use it per *Manual engine override*. If they did NOT pin one, **ask which model implements** (opus / sonnet / fable) per that section before launching — unless the request already makes it unambiguous, in which case route by decision density (Haiku mechanical / Sonnet default / Opus open decisions). Launch in the background with the full spec. The implementer does not decide architecture and does NOT commit; it reports deviations with a reason.

   **Background visibility rule (see *VISIBILITY IS MANDATORY* above — enforce it).** EVERY background task in this flow must stream visible output as it runs, never only on exit. For the implementer, that means launching `agent-implement.sh` directly so its heartbeat streams — NOT wrapping it in a run-then-`cat`-the-log command, and NOT pairing it with a blind `Start-Sleep <minutes>` anti-hang (the wrapper's watchdog already covers hangs). Any other background loop (QA servers, status pollers) must print one line per short iteration with timestamp/phase/elapsed. A mute task is indistinguishable from a hang.

3. **Pair-review.** `assign --role reviewer --engine <e>` first: a same-family reviewer is refused there, before the tokens are spent, not after. Then `enter review`, and feed the result in with `findings --file <out.json>`. When the implementer finishes, review the resulting diff (the uncommitted working tree; if the implementer worked on a branch, that branch's diff against `origin/dev`) with an engine **different from the implementer**. If an implementer was pinned, the reviewer is the other family per the override table (**any Claude model → Grok** via `agent-exec.py --engine grok`; **Grok → Opus**, reviewed by you). For anything beyond a routine ticket prefer the fan-out over a single reviewer.

4. **Adjudication.** `enter adjudicate`, then one `adjudicate --finding <id> --decision <d> --rationale "..."` per finding — the decision log is the ledger, not your message. Returning to the implementer is `enter implement`, which enforces the round cap and the convergence rule; if it refuses, `enter escalated`. Architect seat (Fable), against the spec — contracts, invariants, acceptance criteria, authorized risks, scope. For each finding, decide and record it: ACCEPT / accept with small fixes / return to the implementer / change the spec / reject and reimplement / escalate to human review. Do not adjudicate by "who seems right"; do not apply findings blindly.

5. **Final verification.** `enter verify` (refused while any BLOCKER/MAJOR is undecided). Record each command with `check --name <n> --verdict ok|not-ok|undetermined --evidence "<line>"` — a command you could not run is `undetermined`, and that blocks `commit` by itself. Run the spec's verification commands YOURSELF with direct tools — do not rely on the implementer's report alone. If there is observable surface (UI/API), verify it for real. Apply the standing rule above: a command you could not run is not a command that passed.

6. **Architect commit.** `enter commit`. The implementer does NOT commit (the spec forbids it): after adjudicating and verifying, review `git status` and `git diff --stat` YOURSELF, and commit the result on the work branch with a conventional message. Nothing reaches `promote` without a deliberate commit from you.

7. **Promotion.** `enter promote`, then invoke this plugin's `promote` skill (without `--yes`: the gate to `main` stays in the user's hands, unless they asked otherwise).

8. **Close-out.** `enter effect` (after `verify` proves the change arrived) and `enter done`. Summarize the phase, the adjudicated findings, and the final state in 5-8 lines - `rungraph.py show` has all of it.
