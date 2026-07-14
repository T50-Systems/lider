---
name: pipeline
description: "Run a full phase of the T50 flow - closed architect spec, background implementer, pair-review with a second engine, adjudication, verification, PR promotion. Use for scoped features with an optional final human sign-off."
argument-hint: "<phase or feature description>"
---

You act as the architect. Follow the flow in order; do not skip steps.

1. **Closed spec.** This is the most important deliverable. If the user's description is ambiguous in scope, ask BEFORE launching anything. Fill in this template:
   - **Scope:** exact files/packages that may be touched; what NOT to touch.
   - **Hard constraints:** repo conventions (typing, style, testids, i18n...), "do NOT commit".
   - **Design:** decisions already made, with concrete values (the implementer does not decide architecture; it does report deviations with a reason).
   - **Mandatory verification:** exact commands (typecheck/build/tests) that must pass before finishing.

2. **Implementer.** Launch an agent (Agent tool, `general-purpose`, in the background) with the full spec. Model: `sonnet` by default; use a stronger model only if the phase requires ambiguous decisions the spec could not close.

3. **Pair-review.** When the implementer finishes, invoke this plugin's `pair-review` skill on the resulting diff (the uncommitted working tree; if the implementer worked on a branch, that branch's diff against `origin/dev`).

4. **Adjudication.** For each finding, decide and record it: APPLIED (and apply the fix — directly if trivial, or via the implementer if substantial) or REJECTED with a one-line reason. Do not apply findings blindly.

5. **Final verification.** Run the spec's verification commands YOURSELF — do not rely on the implementer's report alone. If there is observable surface (UI/API), verify it for real.

6. **Architect commit.** The implementer does NOT commit (the spec forbids it): after adjudicating and verifying, review `git status` and `git diff --stat` YOURSELF, and commit the result on the work branch with a conventional message. Nothing reaches `promote` without a deliberate commit from you.

7. **Promotion.** Invoke this plugin's `promote` skill (without `--yes`: the gate to `main` stays in the user's hands, unless they asked otherwise).

8. **Close-out.** Summarize the phase, the adjudicated findings, and the final state in 5-8 lines.
