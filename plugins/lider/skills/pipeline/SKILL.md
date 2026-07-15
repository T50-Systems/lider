---
name: pipeline
description: "Run a full phase of the T50 flow - closed architect spec, decision-density-routed implementer, independent review by a different engine, adjudication, verification, PR promotion, with cost-aware engine allocation. Use for scoped features with an optional final human sign-off."
argument-hint: "<phase or feature description> [--impl codex|opus]"
---

You act as the architect. Follow the flow in order; do not skip steps.

## Engine & model allocation

Core idea: **Fable decides direction; Terra builds; Sol resolves uncertainty; Luna mechanizes; Opus and GPT-5.3-Codex review; Fable adjudicates.**

Frontier models are expensive on OUTPUT — spend them on judgment, not volume. Route implementation by **decision density, not size**. **Never use any Fast mode.** If a step's engine is unavailable, fall back one tier and note it.

**Allowed Codex models (this roster only):** `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`. Do NOT use `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, or `gpt-5.3-codex-spark` (Spark) — Spark is interactive/supervised and does not fit this skill's autonomous background-implementer flow. `codex-auto-review` is the review-only model reached through `pair-review` — never a selectable implementer.

**Architect seat (steps 1 & 4 — spec, adjudication): Fable.** Low output, highest judgment. Adjudicate against contracts / invariants / acceptance criteria / authorized risks / scope — not "who seems right."

**Step 1B challenger (optional): GPT-5.6 Sol** (`--model gpt-5.6-sol`). Activate ONLY for high-risk features — security/authorization, concurrency, transactions, data migrations, architectural changes, external contracts, financial logic, high ambiguity, large blast radius. Sol tries to break the plan (false assumptions, unhandled states, races, incompatibilities, rollback difficulty, missing observability). Skip for routine tickets.

**Manual engine override (when the user asks — takes precedence over the routing below).** The user may pin the implementer for a run: `--impl codex` / `--impl opus`, or in words ("implementa con opus", "que codex implemente"). When set, it overrides decision-density routing for step 2 and **forces the reviewer to the opposite engine** in step 3 — the cross-engine rule (reviewer ≠ implementer) is preserved automatically:

| Requested implementer | Step 2 implementer | Step 3 reviewer (opposite) |
|---|---|---|
| **codex** | Codex via `scripts/codex-implement.sh` — Terra by default; Luna/Sol still allowed by decision density *within the Codex family* | **Opus** — review the diff yourself as Opus (NOT `pair-review`) |
| **opus** | **Opus** — a background `general-purpose` subagent (`model: opus`) implementing from the closed spec (does NOT commit; reports deviations) | **Codex** — `codex-exec.sh --model gpt-5.6-terra` (or `gpt-5.6-sol`): Lider-owned, read-only, findings schema |

Only `codex` and `opus` are selectable as the pinned implementer. If the requested engine is unavailable, say so and fall back per the tier rules rather than silently swapping the reviewer to the same family. Without an override, use the decision-density routing below.

**Step 2 implementer — route by decision density (OpenAI models via the `codex` plugin):**

| Task shape | Engine | Codex `--model` slug |
|---|---|---|
| Mechanical / repetitive with a defined pattern (renames, scaffolding, fixtures, config, docs, lint/type fixes, tests from a case table) | **GPT-5.6 Luna** — executes patterns, does not design them | `gpt-5.6-luna` |
| Normal feature, several files, clear-enough requirements | **GPT-5.6 Terra** (DEFAULT implementer) | `gpt-5.6-terra` |
| Open decisions, high impact, hard debugging, unknown root cause, repeated Terra failures | **GPT-5.6 Sol** | `gpt-5.6-sol` |
| OpenAI unavailable, or to offload OpenAI quota | **Sonnet** (Claude fallback, `general-purpose` background subagent) | — |

Do not escalate by size — escalate by decision density. The implementer does not decide architecture and does NOT commit; it reports deviations with a reason.

**Invoking Codex (Lider-owned, full access).** Run the implementer through this plugin's `scripts/codex-implement.sh`. Do NOT route the implementer through `codex:codex-rescue` — that path uses the Codex plugin's app-server, which caps at `workspace-write` (no writes outside the repo, no network) and inherits the user's personal Codex config. The wrapper instead runs `codex exec --sandbox danger-full-access --model <slug>` in a throwaway `CODEX_HOME`: full read/write across the filesystem, network on, no approvals, and none of the user's plugins/skills/hooks/memories bloating the run.

```
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-implement.sh" <timeout_s> <log> <done> <model_slug> "<prompt>"
```
- **Launch it with the Bash tool's background mode** — it is the long-running implementer. `<log>` and `<done>` are temp files in the session temp dir; the wrapper writes the task's final exit code to `<done>` when it finishes.
- The wrapper runs in the **current working directory** — `cd` into the intended repo/worktree first (the isolated `CODEX_HOME` does not change where the task writes).
- **Always pass an explicit `<model_slug>`** — the Codex default is `gpt-5.5` (disallowed). Terra/Sol/Luna use their full slug above. Never a Fast/priority tier.

**⚠️ Full access is real.** `danger-full-access` lets the implementer write anywhere on disk and use the network with no confirmation. That is intended (it removes the `workspace-write` confinement below), but scope the task prompt tightly and keep the watcher armed.

**Execution constraints (plan around them):**
- **No filesystem confinement.** With full access the implementer can read/write outside the repo and hit the network — the old `workspace-write` limitation (out-of-repo specs unreadable, task silently doing zero work) NO LONGER applies. Keeping inputs in `<repo>/.local/` (gitignored) is still tidy, but not required for the task to see them.
- **The task can start in the wrong checkout** when multiple checkouts/worktrees of the repo exist (observed: a review task began in a sibling worktree and had to self-correct). State the exact working directory in the prompt AND require the task to verify `git branch --show-current` matches the intended branch before touching anything.

**Operational rules — the wrapper self-supervises (heartbeat + watchdog + status file); your job is to read it and react:**
1. **Read `<log>.status.json`** for live state at any moment — `{state, elapsed_s, idle_s, log_bytes, exit, reason, activity}`. `state` moves `starting → running → done|failed`; **`activity`** is a short human read of what Codex is doing *right now*, extracted from its own stream (`exec: <cmd>`, `edit: <file>`, `say: <message>`, `finalizing`). The heartbeat line on stdout (~10s) shows the same `activity`, so the background panel narrates the run — not just a pulse. For the full trace, tail `<log>` itself. The wrapper self-aborts a stall (no `<log>` growth for ~5 min on implement) or a dead-at-launch (no output in 60s) as **exit 125**, so you learn about a hang in minutes, not at the full timeout.
2. **React to `<done>` / the exit code** (mirrored in `status.json`): `0` = ok; `124` = hard timeout; `125` = watchdog abort (stalled or died at launch); `127` = codex missing; other = codex's exit. On `124`/`125` inspect the working tree (`git status --short`) and decide recovery — the implement wrapper does NOT auto-retry (a half-written tree is unsafe to blindly re-run); reset/branch or relaunch with a resume instruction as the spec dictates.
3. **Still cross-check `git status --short`** for actual file activity — it is the ground truth that the task is producing work, complementing the status file.

**Step 3 reviewer — MUST differ from the implementer (same-engine review shares blind spots):**

| Implementer | Reviewer | Mechanism |
|---|---|---|
| Luna | **GPT-5.3-Codex** | `pair-review` skill |
| Terra / Sol | **Opus** — for Sol on critical code: Opus + focused human review | review yourself as Claude/Opus (NOT `pair-review`) |
| Claude — Sonnet | **GPT-5.3-Codex** | `pair-review` skill |
| Claude — Opus | **GPT-5.6 Terra or Sol** | `codex-exec.sh --model gpt-5.6-terra`/`gpt-5.6-sol` (Lider-owned, read-only, findings schema) |
| Claude — Fable | **GPT-5.6 Sol** | `codex-exec.sh --model gpt-5.6-sol` (Lider-owned, read-only, findings schema) |

The GPT-5.3-Codex reviewer is realized by the Codex code-review path (model `codex-auto-review`), invoked through this plugin's `pair-review` skill — there is no bare `gpt-5.3-codex` slug in the install. When the reviewer is Opus, review the diff yourself and do NOT route it back through the Codex-backed `pair-review` (that would collapse to the same engine family the implementer used). GPT-5.3-Codex is the code/review specialist (diffs, cross-file coherence, regressions, coverage gaps, convention compliance), not a general implementer.

**Steps 5–8 (verify, commit, promote, close-out): direct tools first.** Run tests / lint / typecheck / build / migrations / coverage with tools, never with a model. Use **Luna** (`--model gpt-5.6-luna`) for mechanical follow-up (interpret simple results, commit message, changelog, decision log, docs); **Haiku** as the Claude-side equivalent when preserving OpenAI quota or Codex is unavailable (commit messages, changelog, simple result interpretation, close-out summary, cheap read-only repo searches via `Explore` subagents); **Sonnet** only when the mechanical work needs multi-step coordination or light judgment. Never spend frontier tokens here.

## Flow

1. **Closed spec.** Architect seat (Fable) — the most important deliverable. If the user's description is ambiguous in scope, ask BEFORE launching anything. Identify decisions, define limits, establish contracts and invariants, specify acceptance criteria, split the feature into implementable units, and flag reversible vs irreversible risks. Fill in this template:
   - **Scope:** exact files/packages that may be touched; what NOT to touch.
   - **Hard constraints:** repo conventions (typing, style, testids, i18n...), "do NOT commit".
   - **Design:** decisions already made, with concrete values (the implementer does not decide architecture; it does report deviations with a reason).
   - **Mandatory verification:** exact commands (typecheck/build/tests) that must pass before finishing.

   Classify risk. For high-risk features only, run **step 1B** — have GPT-5.6 Sol pressure-test the plan (see allocation) before implementing.

2. **Implementer.** If the user pinned an implementer (`--impl codex|opus`), use it per *Manual engine override* — otherwise route per *Engine & model allocation* by decision density (Luna mechanical / Terra default / Sol open decisions / Sonnet fallback). Launch in the background with the full spec. The implementer does not decide architecture and does NOT commit; it reports deviations with a reason.

   **Background visibility rule.** EVERY background task in this flow (implementer runs, status-polling loops, monitors, QA servers) must emit periodic visible output — at minimum one heartbeat line per poll iteration with timestamp, phase, and elapsed time (e.g. `[14:32:01] Phase: running | Elapsed: 12m | last: <event>`). Never launch a silent `while` loop that only prints on exit: the user sees the task panel, and a mute loop is indistinguishable from a hang.

3. **Pair-review.** When the implementer finishes, review the resulting diff (the uncommitted working tree; if the implementer worked on a branch, that branch's diff against `origin/dev`) with an engine **different from the implementer**. If an implementer was pinned, the reviewer is the opposite engine per the override table (**codex→Opus** review yourself; **opus→Codex** via `pair-review`). Otherwise use the reviewer table: invoke this plugin's `pair-review` skill when Claude implemented; review with Opus yourself (or GPT-5.3-Codex for Luna) when OpenAI implemented.

4. **Adjudication.** Architect seat (Fable), against the spec — contracts, invariants, acceptance criteria, authorized risks, scope. For each finding, decide and record it: ACCEPT / accept with small fixes / return to the implementer / change the spec / reject and reimplement / escalate to human review. Do not adjudicate by "who seems right"; do not apply findings blindly.

5. **Final verification.** Run the spec's verification commands YOURSELF with direct tools — do not rely on the implementer's report alone. If there is observable surface (UI/API), verify it for real.

6. **Architect commit.** The implementer does NOT commit (the spec forbids it): after adjudicating and verifying, review `git status` and `git diff --stat` YOURSELF, and commit the result on the work branch with a conventional message. Nothing reaches `promote` without a deliberate commit from you.

7. **Promotion.** Invoke this plugin's `promote` skill (without `--yes`: the gate to `main` stays in the user's hands, unless they asked otherwise).

8. **Close-out.** Summarize the phase, the adjudicated findings, and the final state in 5-8 lines.
