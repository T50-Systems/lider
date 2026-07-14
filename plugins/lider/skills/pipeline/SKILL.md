---
name: pipeline
description: "Run a full phase of the T50 flow - closed architect spec, decision-density-routed implementer, independent review by a different engine, adjudication, verification, PR promotion, with cost-aware engine allocation. Use for scoped features with an optional final human sign-off."
argument-hint: "<phase or feature description>"
---

You act as the architect. Follow the flow in order; do not skip steps.

## Engine & model allocation

Core idea: **Fable decides direction; Terra builds; Sol resolves uncertainty; Luna mechanizes; Opus and GPT-5.3-Codex review; Fable adjudicates.**

Frontier models are expensive on OUTPUT — spend them on judgment, not volume. Route implementation by **decision density, not size**. **Never use any Fast mode.** If a step's engine is unavailable, fall back one tier and note it.

**Allowed Codex models (this roster only):** `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`. Do NOT use `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, or `gpt-5.3-codex-spark` (Spark) — Spark is interactive/supervised and does not fit this skill's autonomous background-implementer flow. `codex-auto-review` is the review-only model reached through `pair-review` — never a selectable implementer.

**Architect seat (steps 1 & 4 — spec, adjudication): Fable.** Low output, highest judgment. Adjudicate against contracts / invariants / acceptance criteria / authorized risks / scope — not "who seems right."

**Step 1B challenger (optional): GPT-5.6 Sol** (`--model gpt-5.6-sol`). Activate ONLY for high-risk features — security/authorization, concurrency, transactions, data migrations, architectural changes, external contracts, financial logic, high ambiguity, large blast radius. Sol tries to break the plan (false assumptions, unhandled states, races, incompatibilities, rollback difficulty, missing observability). Skip for routine tickets.

**Step 2 implementer — route by decision density (OpenAI models via the `codex` plugin):**

| Task shape | Engine | Codex `--model` slug |
|---|---|---|
| Mechanical / repetitive with a defined pattern (renames, scaffolding, fixtures, config, docs, lint/type fixes, tests from a case table) | **GPT-5.6 Luna** — executes patterns, does not design them | `gpt-5.6-luna` |
| Normal feature, several files, clear-enough requirements | **GPT-5.6 Terra** (DEFAULT implementer) | `gpt-5.6-terra` |
| Open decisions, high impact, hard debugging, unknown root cause, repeated Terra failures | **GPT-5.6 Sol** | `gpt-5.6-sol` |
| OpenAI unavailable, or to offload OpenAI quota | **Sonnet** (Claude fallback, `general-purpose` background subagent) | — |

Do not escalate by size — escalate by decision density. The implementer does not decide architecture and does NOT commit; it reports deviations with a reason.

**Invoking Codex.** Delegate to the `codex:codex-rescue` agent, which forwards `task --model <slug> [--effort <none|minimal|low|medium|high|xhigh>] --write` to the Codex runtime. **Always pass an explicit `--model <slug>`** — the Codex default is `gpt-5.5` (disallowed), so an unset model does NOT give you Terra. Terra/Sol/Luna use their full slug above. Never select a Fast/priority tier.

**Codex sandbox constraints (verified behaviors — plan around them):**
- **Filesystem access is confined to the task's working directory.** Reads outside it fail (observed: a spec in a temp/scratchpad dir was unreadable and the task completed with zero work). Assume writes are equally confined. EVERYTHING the task needs — spec, fixtures, reference docs — must live inside the repo (use `<repo>/.local/`, gitignored, for non-committable inputs).
- **The task can start in the wrong checkout** when multiple checkouts/worktrees of the repo exist (observed: a review task began in a sibling worktree and had to self-correct). Always state the exact working directory in the prompt AND require the task to verify `git branch --show-current` matches the intended branch before touching anything.

**Codex operational rules (learned 2026-07, HRH daily-schedule-email run):**
1. **The spec file MUST live inside the repo working directory** (e.g. `<repo>/.local/spec-<feature>.md`, gitignored) — see sandbox constraints above; an out-of-repo spec fails quietly: the task completes having done zero work.
2. **Arm your own working-tree watcher when you launch a `--write` task** (poll `git status --short` for changes/stability). Do not rely on the wrapper agent's reporting — it tends to yield with "watcher set, waiting" instead of blocking, and a dead task can sit invisible for an hour.
3. **Fast-fail: if no file has been touched within ~10 minutes of launching a `--write` task, interrogate it immediately** (message the wrapper: "inspect the task state NOW, no watchers"). A healthy implementer starts writing quickly; prolonged silence means the task died at launch.

**Step 3 reviewer — MUST differ from the implementer (same-engine review shares blind spots):**

| Implementer | Reviewer | Mechanism |
|---|---|---|
| Luna | **GPT-5.3-Codex** | `pair-review` skill |
| Terra / Sol | **Opus** — for Sol on critical code: Opus + focused human review | review yourself as Claude/Opus (NOT `pair-review`) |
| Claude — Sonnet | **GPT-5.3-Codex** | `pair-review` skill |
| Claude — Opus | **GPT-5.6 Terra or Sol** | Codex read-only review task, `--model gpt-5.6-terra`/`gpt-5.6-sol` |
| Claude — Fable | **GPT-5.6 Sol** | Codex read-only review task, `--model gpt-5.6-sol` |

The GPT-5.3-Codex reviewer is realized by the Codex code-review path (model `codex-auto-review`), invoked through this plugin's `pair-review` skill — there is no bare `gpt-5.3-codex` slug in the install. When the reviewer is Opus, review the diff yourself and do NOT route it back through the Codex-backed `pair-review` (that would collapse to the same engine family the implementer used). GPT-5.3-Codex is the code/review specialist (diffs, cross-file coherence, regressions, coverage gaps, convention compliance), not a general implementer.

**Steps 5–8 (verify, commit, promote, close-out): direct tools first.** Run tests / lint / typecheck / build / migrations / coverage with tools, never with a model. Use **Luna** (`--model gpt-5.6-luna`) for mechanical follow-up (interpret simple results, commit message, changelog, decision log, docs); **Haiku** as the Claude-side equivalent when preserving OpenAI quota or Codex is unavailable (commit messages, changelog, simple result interpretation, close-out summary, cheap read-only repo searches via `Explore` subagents); **Sonnet** only when the mechanical work needs multi-step coordination or light judgment. Never spend frontier tokens here.

## Flow

1. **Closed spec.** Architect seat (Fable) — the most important deliverable. If the user's description is ambiguous in scope, ask BEFORE launching anything. Identify decisions, define limits, establish contracts and invariants, specify acceptance criteria, split the feature into implementable units, and flag reversible vs irreversible risks. Fill in this template:
   - **Scope:** exact files/packages that may be touched; what NOT to touch.
   - **Hard constraints:** repo conventions (typing, style, testids, i18n...), "do NOT commit".
   - **Design:** decisions already made, with concrete values (the implementer does not decide architecture; it does report deviations with a reason).
   - **Mandatory verification:** exact commands (typecheck/build/tests) that must pass before finishing.

   Classify risk. For high-risk features only, run **step 1B** — have GPT-5.6 Sol pressure-test the plan (see allocation) before implementing.

2. **Implementer.** Route per *Engine & model allocation* by decision density — Luna (mechanical) / Terra (default) / Sol (open decisions) / Sonnet (fallback). Launch in the background with the full spec. The implementer does not decide architecture and does NOT commit; it reports deviations with a reason.

   **Background visibility rule.** EVERY background task in this flow (implementer runs, status-polling loops, monitors, QA servers) must emit periodic visible output — at minimum one heartbeat line per poll iteration with timestamp, phase, and elapsed time (e.g. `[14:32:01] Phase: running | Elapsed: 12m | last: <event>`). Never launch a silent `while` loop that only prints on exit: the user sees the task panel, and a mute loop is indistinguishable from a hang.

3. **Pair-review.** When the implementer finishes, review the resulting diff (the uncommitted working tree; if the implementer worked on a branch, that branch's diff against `origin/dev`) with an engine **different from the implementer** per the reviewer table: invoke this plugin's `pair-review` skill when Claude implemented; review with Opus yourself (or GPT-5.3-Codex for Luna) when OpenAI implemented.

4. **Adjudication.** Architect seat (Fable), against the spec — contracts, invariants, acceptance criteria, authorized risks, scope. For each finding, decide and record it: ACCEPT / accept with small fixes / return to the implementer / change the spec / reject and reimplement / escalate to human review. Do not adjudicate by "who seems right"; do not apply findings blindly.

5. **Final verification.** Run the spec's verification commands YOURSELF with direct tools — do not rely on the implementer's report alone. If there is observable surface (UI/API), verify it for real.

6. **Architect commit.** The implementer does NOT commit (the spec forbids it): after adjudicating and verifying, review `git status` and `git diff --stat` YOURSELF, and commit the result on the work branch with a conventional message. Nothing reaches `promote` without a deliberate commit from you.

7. **Promotion.** Invoke this plugin's `promote` skill (without `--yes`: the gate to `main` stays in the user's hands, unless they asked otherwise).

8. **Close-out.** Summarize the phase, the adjudicated findings, and the final state in 5-8 lines.
