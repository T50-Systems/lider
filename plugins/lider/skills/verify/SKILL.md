---
name: verify
description: "Prove the EFFECT of a change, not the intent. Verifies content actually reachable on the integration branch (not the PR's merged flag), and what the deployed environment is actually serving (not the colour of the CI job) - across every surface, at the moment you show it to someone. Refuses to give a verdict it could not establish. Use after `promote`, after any deploy, and as the closing step of `pipeline` when the work reaches a shared environment."
argument-hint: "<what should now be true> - e.g. 'PR #1320 landed', 'prod serves 2a6257dd'"
---

Tests prove the change is correct. This proves the change **arrived**. Those fail
independently, and the second failure mode is the quiet one: every status API in the chain
reports **intent**, and intent is not effect.

## The three lies to defeat

### 1. "The PR says merged"

A merge API can report `merged: true`, hand you a `merge_commit_sha`, and leave the content
**nowhere**. Measured: two PRs merged seconds apart, both green; both squashes were computed
against the same base, and the second to write the ref orphaned the first. The API returned
`merged: true` with a commit SHA that **was not reachable from the integration branch and did
not even fetch**. The PR sat closed-as-merged with its content gone: the file it added was
missing and the code it deleted was still there.

So verify **content**, not status:

```bash
git fetch <remote> <branch>
git show <remote>/<branch>:<path-the-change-added>     # must exist
git show <remote>/<branch>:<path> | grep -q '<pattern>' # must contain
```

Do **not** hand-roll this if the repo ships a checker for it — a raw `git show | grep -c` has
three ways to lie, and all three have been observed on the same day:

- path mangling (MSYS rewrites `remote/branch:path`) makes `git` die "ambiguous argument", and
  a `| grep -c` over the empty output prints `0` — **indistinguishable from "not present"**;
- `set -e` does not fire inside a pipeline, so the status you see is `grep`'s, not `git`'s;
- `git show` exits non-zero both when the file is missing and when the **fetch** failed —
  opposite conclusions, same exit code.

**Rule: merge one at a time**, wait for the remote branch to advance, then verify content.

### 2. "The job is green" / "the job is red"

Both directions lie:

- **A red run can be a correct deploy.** A post-deploy surface assertion that runs seconds
  after traffic switches reads a cold-start `503` as "route absent". Verify the routes by
  hand before reverting anything.
- **A green run can be a lost merge** (see lie 1): CI validated the tree that *did* land.
- Some forges map **cancelled → failure**, so a systematic red is really "superseded". Confirm
  which state your API actually returns before reclassifying anything — on at least one, the
  task API reports `cancelled` and `failure` as **distinct** states, so the folklore does not
  apply and the counts must not be merged.

The environment is the authority, not the job:

```bash
curl -s <env>/api/version   # or whatever reports the built SHA
curl -s <env>/api/health
```

### 3. "I verified it" (but earlier)

Verify at the **moment you show it to someone**, not before. Measured: a session verified a
shared environment at 05:46, showed it to the operator, another session deployed its own
branch at 05:48, and the operator was told **twice** to clear their browser cache for a
problem that was not theirs.

Two corollaries:

- **Check every surface.** If a deploy touches more than one service, they do not converge
  together — one was measured to report the new SHA **~2 minutes** after the other. Seeing
  one updated and one not is *not* a half-broken deploy; do not dispatch another.
- **Check the artifact, not just the version.** For a frontend, confirm the chunk that
  actually contains the change, not only the version string — a correct `/api/version` with a
  stale bundle is possible while a tab stays open.

## Cold starts are not incidents

An environment that scales to zero answers slowly on the first hit — measured up to ~80 s to
respond and ~250 s before reporting healthy. Poll before declaring anything broken. Equally:
do **not** add a periodic poll "to keep an eye on it" — that defeats the scale-to-zero the
environment was configured for and turns a cost saving into a bill.

## Refuse verdicts you could not establish

Same rule as `preflight`, restated because this is where it costs most: every check here has
**three** outcomes — confirmed, refuted, and **could not determine**. A checker that answers
"not present" when it actually failed to look is worse than no checker, because it is
believed. Prefer tools that exit with a distinct code for "I could not look" (commonly `2`)
and treat that code as **stop**, never as "no".

## Output

One table: claim → how it was checked → verdict (**confirmed** / **refuted** /
**undetermined**) → the raw evidence line. Then a single overall verdict.

If anything is undetermined, say what and why, and do not close the task. "Probably fine" is
the sentence that preceded every incident in this file.
