---
name: preflight
description: "Establish and PROVE the operating conditions before touching shared state - deploys, migrations, infra, merges into a shared branch. Detects the repo's forge and conventions instead of assuming them, claims the advisory lock if there is one, measures BOTH directions of the delta, checks nothing is already in flight, and refuses to give a verdict it could not actually establish. Use before `promote`, before any deploy dispatch, and as step 0 of `pipeline` when the work touches production or a shared environment."
argument-hint: "<what you are about to do> - e.g. 'deploy main to prod', 'apply terraform to prod', 'merge #1320'"
---

`pipeline` answers *"did I build what I said?"*. This answers the question next to it:
**"under what conditions may I touch this, and can I prove those conditions hold?"**

Those are different failure modes. In one measured 26.8 h window of concurrent multi-agent
operation, **seven incidents were logged and six were operational** — not one was a spec
failure. The change was correct every time; the way it was delivered was not.

## The rule that outranks everything here

**"I could not ask" is NOT "there is nothing there."**

This is the single most expensive habit in this file, because it fails *silently and
confidently*. Measured instances, all in one session:

- A lock reader could not launch `gcloud`, returned empty, and the dashboard drew
  **"all clear" while another session held the production deploy lock and was mid-deploy.**
- A network probe used `gcloud storage ls` exit status to mean "reachable" — but an **empty
  prefix also exits 1**, and empty is the normal case. The guard would have failed open
  almost always.
- A CI watcher read an API error as `state != "pending"` and reported **"CI finished"**.

So: every check in this file has **three** outcomes, never two — `ok`, `not ok`, and
**`could not determine`**. The third must stop the flow, not pass it. When a repo ships a
checker that already does this (exit 0 / exit 1 / **exit 2 = "I could not look"**), use that
checker instead of a bare shell one-liner.

## 1. Detect the repo's conventions — never assume them

Assumed conventions are how tooling silently targets the wrong place. Establish, in this
order, and **stop if any answer is surprising**:

| Question | How | Trap it avoids |
|---|---|---|
| Which remote is canonical? | `git remote -v` — look for a **disabled push URL**; a remote can be fetch-only on purpose | A repo whose `origin` is a read-only mirror and whose real forge is elsewhere. Pushing there fails *by design* — do not "fix" it |
| Which forge API? | GitHub → `gh`. Anything else (Forgejo/Gitea/GitLab) → its own REST API, usually `curl`. `gh` always targets GitHub | Opening the PR on the mirror nobody reads |
| What is the integration branch? | `git ls-remote --heads <remote>` **and** how far behind it is: `git rev-list --count <remote>/<branch>..<remote>/<default>` | A `dev` branch that exists but is **337 commits and 17 days stale** — merging through it is not a promotion, it is a resurrection |
| Is there a lock / coordination board? | Look for `scripts/session-lock.sh`, a coordination dir, or a board named in the repo's agent instructions | Two sessions moving the same shared resource |

If the repo documents its own flow (agent instructions, a coordination doc), that document
wins over this skill's defaults.

## 2. Claim the lock — and prove it is yours

If the repo has an advisory lock, take it **before** measuring anything, and re-read it after:
another session can claim it in the seconds between your check and your action (measured: a
panel read "free" **one second** before another session's claim landed).

Identity trap: a lock is only useful if you can tell *your* lock from someone else's. Comparing
by **branch name** breaks the moment you switch branches between claiming and acting — the
holder then reads their own lock as foreign. Comparing by a **machine-local ledger** is worse:
it is per-machine, so a *different* session on the same box reads your lock as its own and
sails through. Prefer an identity that is stable under branch switches and distinct per
session (the worktree path is usually both).

## 3. Measure BOTH directions — deploying also REMOVES

The direction nobody looks at is what leaves.

```bash
git log --oneline <what-is-live>..<remote>/<branch>   # what ENTERS
git log --oneline <remote>/<branch>..<what-is-live>   # what LEAVES  <-- this one
```

If the second is not empty, there is a live fix that the branch does not have, and shipping
the branch is a **regression**. Measured: a hotfix that never got a PR back to the integration
branch was silently reverted by the next ordinary deploy and stayed reverted for **17 hours**.

Use a **two-dot** diff only for this "what leaves" question. For "what does my branch change",
always measure from the **merge base** — `git diff $(git merge-base HEAD <remote>/<branch>)..HEAD`.
A two-dot `<branch>..HEAD` shows your branch *reverting* everything the branch gained after
your base. Measured: a PR was closed as "it deletes a workflow and touches a component" when
the branch touched those files in **zero** of its commits.

## 4. Is there something in flight?

Check the queue before adding to it. Two traps, both measured:

- **A dispatch API can return success and enqueue nothing visible yet.** One forge returned
  `204` and the run took **5–20 minutes** to appear in the task list. Reading that as "it did
  not enqueue" and retrying enqueues a *second* full run.
- **`concurrency` groups serialize, they do not protect.** Two dispatches do not collide; they
  queue, and **the last one to RUN wins — not the newest commit.** A redundant dispatch of an
  older SHA ran after the good one and reverted a clinical fix in production.

Corollary: if the forge cannot cancel a run through its API (many cannot), a surplus dispatch
**cannot be taken back**. Never dispatch "just in case."

## 5. Would this change anything at all?

Measure which files enter *the artifact*, not how many commits enter the branch. A batch of
only tests, CI scripts and coordination docs produces a **byte-identical** artifact — the
deploy costs a cold start and changes nothing but a version string. Measured: one session
ceded its turn for exactly this reason.

```bash
git diff --name-only <live>..<remote>/<branch> -- . \
  ':(exclude)tests/' ':(exclude)docs/' ':(exclude)scripts/' ':(exclude).claude/'
```

Empty output → say so and let the batch accumulate.

## 6. Branch from the fetched remote, not from local

`git fetch <remote> <branch>` first, always. A local integration branch goes stale **silently**,
and a comment on an issue is not the current state. Measured: an entire PR was built on a
base that had moved.

## Output

Report a short table — condition, verdict, evidence — and one of three overall verdicts:

- **GO** — every condition established, lock held, nothing in flight.
- **NO-GO** — a condition failed. Say which, and what would clear it.
- **UNDETERMINED** — a check could not run. **This is not GO.** Say what you could not
  establish and why.

Never soften UNDETERMINED into GO because the rest looked fine. That specific softening is
what put a dashboard's "all clear" on screen during someone else's production deploy.
