---
name: promote
description: "Promote the current work through the PR flow - branch, PR to dev, merge, PR dev to main, merge, local sync. Use when the work is verified and ready for production."
argument-hint: "[--yes] [change title]"
---

0. **Preconditions.** Verify before anything; if any fails, inform the user and stop:
   - `git remote get-url origin` exists and `gh auth status` is ok.
   - A `dev` branch exists on origin (`git ls-remote --heads origin dev`). If it does NOT: tell the user this flow requires `dev` and offer to create it — do not create it without confirmation.
   - `git fetch origin` and check there is work to promote: `git log --oneline origin/dev..HEAD` (commits ahead) or `git status --porcelain` (uncommitted changes). If there is nothing, report it and stop.
   - There is no open PR already `base:main head:dev` (`gh pr list --base main --head dev --state open`). If one exists, stop and report it — do not duplicate in-flight promotions.

1. **Pin the work branch.** Resolve `WORK_BRANCH=$(git branch --show-current)` ONCE, at the start, and use that literal value in every following step (push, PR, delete) — never re-derive "the current branch" mid-flow.
   - If `WORK_BRANCH` is `main` or `dev` and there are uncommitted changes: create a branch `type/short-slug` from `dev` (type = `feat`|`fix`|`chore`), commit there with a conventional message, and that becomes `WORK_BRANCH`. NEVER commit directly to `main`/`dev`.
   - If `WORK_BRANCH` is a work branch with uncommitted changes: commit them on it before continuing (nothing is promoted without a commit).
   - If `WORK_BRANCH` is a work branch with commits and a clean tree: use it as-is.

2. **PR to dev.** `git push -u origin "$WORK_BRANCH"` and `gh pr create --base dev --head "$WORK_BRANCH"` with a body that includes "## Summary" and "## Validation" (a real checklist of what was verified in the session: tests/typecheck/browser — do not invent checks that were not run). Save the PR number it returns.

3. **Merge to dev.** `gh pr merge <n> --merge --delete-branch` and verify it merged (`gh pr view <n> --json state` → `MERGED`). If the merge fails or is blocked (checks/protection/conflicts), report the reason and stop — do not bypass it.

4. **GATE toward main.** Continue without asking ONLY if the arguments contain the exact token `--yes` as a separate word. In any other case (including doubt), STOP here and ask for explicit confirmation, showing: the PR already merged into dev and what is about to be promoted to production.

5. **PR dev→main.** `gh pr create --base main --head dev --title "Promote to production: <summary>"` with Summary/Validation referencing the previous PR; save the number, `gh pr merge <n> --merge`, and verify `MERGED` state as in step 3.

6. **Local sync and cleanup.** Requires a clean `git status --porcelain` (if not clean, stop and report). Then: `git checkout dev && git pull --ff-only origin dev`, `git checkout main && git pull --ff-only origin main`, and delete the local branch only if `WORK_BRANCH` is not `main`/`dev`: `git branch -d "$WORK_BRANCH"` (the remote one was already deleted in step 3). If `-d` refuses, do NOT use `-D`: report the pending branch. Close by reporting the last 2 commits of `main`.

7. **Hard rules.** Never `push --force`. Never merge locally into `main`/`dev`. After each `gh`/`git` operation, verify its result before the next step; on any unexpected state, stop and report instead of improvising.
