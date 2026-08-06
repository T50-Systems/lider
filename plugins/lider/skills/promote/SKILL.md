---
name: promote
description: "Promote the current work through the PR flow - branch, PR to dev, merge, PR dev to main, merge, local sync. Use when the work is verified and ready for production."
argument-hint: "[--yes] [change title]"
---

0. **Detect the topology — do NOT assume it.** This flow used to hardcode `origin` + `gh` +
   a `dev` branch. That is correct in many repos and **actively wrong in others**: a repo was
   measured whose `origin` is a read-only GitHub mirror with its push URL deliberately set to
   `DISABLED-PUSH-TO-GITHUB--use-forgejo`, whose real forge is self-hosted Forgejo reached
   through a tunnel, and whose `dev` branch still exists but sits **337 commits and 17 days
   behind `main`**. Running the old step 0 there would push to a dead remote and merge a
   resurrected branch. So resolve, in order, and **stop rather than improvise**:

   - **Canonical remote.** `git remote -v`. A remote whose push URL is disabled or a
     placeholder is a **mirror** — never the promotion target, and its failure to push is by
     design. If exactly one remote is pushable, that is the canonical one; if several are,
     ask.
   - **Forge API.** GitHub → `gh`. Anything else (Forgejo/Gitea/GitLab) → that forge's REST
     API, typically `curl` with a token. **`gh` always targets GitHub**, so on a non-GitHub
     canonical remote `gh` is not a fallback, it is the wrong repo.
   - **Integration branch.** Does the repo actually promote through an intermediate branch?
     `git ls-remote --heads <remote> dev`, and if it exists, how stale:
     `git rev-list --count <remote>/dev..<remote>/<default>`. If `dev` is absent, or lags the
     default branch by a large margin, or the repo's own agent instructions say PRs go
     **straight to the default branch** — then this is a **single-hop** repo: skip the
     dev→main leg entirely and treat steps 2–3 as the promotion. Say which mode you picked
     and why, before acting.
   - **Repo's own doctrine wins.** If the repo documents its flow (agent instructions, a
     coordination doc), that document overrides this skill's defaults.

   Then the ordinary preconditions, expressed against what you just resolved:
   - Auth to the canonical forge works.
   - `git fetch <remote>` and there is work to promote: commits ahead of the target branch, or
     uncommitted changes. If nothing, report and stop.
   - No equivalent promotion PR is already open. If one exists, stop — do not duplicate an
     in-flight promotion.

   **Run `preflight` first when the promotion reaches production or a shared environment**
   (locks, both directions of the delta, nothing in flight). This step establishes *where* to
   promote; `preflight` establishes *whether you may right now*.

1. **Pin the work branch.** Resolve `WORK_BRANCH=$(git branch --show-current)` ONCE, at the start, and use that literal value in every following step (push, PR, delete) — never re-derive "the current branch" mid-flow.
   - If `WORK_BRANCH` is `main` or `dev` and there are uncommitted changes: create a branch `type/short-slug` from `dev` (type = `feat`|`fix`|`chore`), commit there with a conventional message, and that becomes `WORK_BRANCH`. NEVER commit directly to `main`/`dev`.
   - If `WORK_BRANCH` is a work branch with uncommitted changes: commit them on it before continuing (nothing is promoted without a commit).
   - If `WORK_BRANCH` is a work branch with commits and a clean tree: use it as-is.

2. **PR to the first target.** Push the branch to the **canonical remote** and open the PR
   against the **first target** resolved in step 0 (`dev` in two-hop repos, the default branch
   in single-hop ones). Use `gh` only if the canonical forge is GitHub; otherwise use that
   forge's REST API. Body includes "## Summary" and "## Validation" — a real checklist of what
   was verified this session (tests/typecheck/browser). **Do not invent checks that were not
   run.** Save the PR number.

   **Build API bodies from a file, never inline in the shell.** A shell eats backticks and
   quotes even inside a heredoc, which silently mangles the body. Write the markdown to a
   file, build the JSON with a real JSON serializer, and post `--data-binary @file`.

   **If the repo auto-closes issues from PR bodies, check the exact form it accepts.** Several
   forges only match a bare English keyword followed immediately by a bare `#123`; a localized
   keyword, or the number wrapped in a markdown link, is a plain mention and the issue survives
   its own fix.

3. **Merge.** Merge the PR through the resolved forge and **verify the result**, then verify
   the **content**: the API's merged flag is intent, not effect. Two PRs merged seconds apart
   were measured to report `merged: true` with a `merge_commit_sha` unreachable from the
   branch — one PR's content vanished. **Merge one at a time**, wait for the remote branch to
   advance, and confirm the change is reachable (`verify` skill). If the merge is blocked
   (checks/protection/conflicts), report the reason and stop — do not bypass it.

4. **GATE toward the default branch.** *Two-hop repos only; in single-hop repos the gate was
   step 3 and you skip to 6.* Continue without asking ONLY if the arguments contain the exact
   token `--yes` as a separate word. In any other case (including doubt), STOP and ask for
   explicit confirmation, showing what already merged and what is about to reach production.

5. **PR from the integration branch to the default branch.** Same mechanics as step 2, titled
   as a promotion and referencing the previous PR; merge and verify as in step 3.

6. **Local sync and cleanup.** Requires a clean `git status --porcelain` (if not clean, stop and
   report). Pull each branch you touched `--ff-only` from the canonical remote, and delete the
   local work branch only if it is not an integration/default branch: `git branch -d
   "$WORK_BRANCH"`. If `-d` refuses, do NOT use `-D`: report the pending branch. Close by
   reporting the last 2 commits of the default branch.

   **If the promotion reaches a deployed environment, finish with `verify`** — content on the
   branch *and* what the environment is actually serving. A green pipeline is not evidence that
   production changed.

7. **Hard rules.** Never `push --force`. Never merge locally into `main`/`dev`. After each `gh`/`git` operation, verify its result before the next step; on any unexpected state, stop and report instead of improvising.
