# Closed spec — the checkable half of Inception, and no scheduler

## How this was decided

A blind panel: **Fable**, **Opus** and **Grok** were given the same problem statement
and no sight of each other. Grok did not deliver — it cancelled itself twice
(`stopReason: "cancelled"`, ~$0.17 each) on this task, though a control test proved
it can read files fine under the same permission rules. **A panelist that did not
answer is a gap, not a vote**, so this was adjudicated on two plans, and that is
stated rather than glossed.

Fable and Opus converged independently on all three of:

1. **`inception_only`** — build the Inception half, reject the scheduler.
2. **The same single checkable gap**: units of work shipped with their most
   checkable property unbuilt — nothing correlates the spec's content with the
   units declared to implement it.
3. **The same objection to their own recommendation**, which is the most valuable
   thing the panel produced (see *Honesty constraint* below).

## Decision A — no scheduler. Build the instrument that would justify one.

Both deferred. The arguments compose:

- **rungraph is not the bottleneck.** Wall-clock goes into engine subprocesses, and
  `fanout.py` already runs those concurrently. (Opus)
- **Fanning units out means N implementers editing one working tree**, which needs
  per-unit git worktrees and a merge story — machinery with nothing to do with the
  ledger. (Opus)
- **Making rungraph execute drags `runtime.py`, the adapters and subprocess
  supervision into the one file that imports nothing but stdlib.** That purity is
  why its tests run without an engine ever starting. It is an asset. (Opus)
- **"Edge predicates as data" needs a DSL with no evidence of which predicates are
  needed** — and the guards that matter (defect identity, the family rule) are
  exactly the kind no predicate DSL expresses cleanly. That is evidence against the
  DSL, not for it. (Fable)

**Build instead:** `rungraph.py next` — read-only, no save, no mutation. It reports
what is eligible now, what is blocked and by what, and the run's legal moves, and it
records the concurrency width to `metrics.jsonl`. Nobody has yet looked at whether
real runs even have units eligible concurrently. **Revisit the scheduler against
that recorded data, never against intuition.**

## Decision B — build the Inception half, and only its checkable parts

Most of AI-DLC's Inception is unfalsifiable by machine: personas, component design,
requirements narrative. Forcing a model to emit them is instruction, not
enforcement, and this plugin's whole thesis is that those differ. Those stay prose
the architect writes.

### Honesty constraint (from the panel's shared objection)

Both engines raised it independently and neither could fully answer it:

> Every guard lider is proud of checks a declaration against something **outside**
> it. Reviewer-differs-in-family checks against a table the model does not control.
> Convergence-by-defect-identity checks this round's findings against earlier
> rounds'. **Coverage is self-attestation**: the orchestrator that writes the
> criteria also declares which unit covers them, so the gate verifies bookkeeping
> consistency — not that anything was implemented.

This does not kill the feature: dropping a requirement by never declaring a unit for
it is a real, common, and currently undetectable error. But it bounds the claim, and
the bound is **binding on the implementation**: the refusal text and the docs must
say that the ledger checks the mapping, not the substance. A form check must never
read as a substance check.

## Scope

May be touched:
- `plugins/lider/scripts/rungraph.py`
- `plugins/lider/skills/pipeline/SKILL.md`, `README.md`, `ARCHITECTURE.md`
- `tests/test_rungraph.py`, `tests/test_units.py`, and a new test module
- both version manifests (they must stay in sync)

Must NOT be touched: `runtime.py`, the adapters, `fanout.py`, `reduce-findings.py`,
`verify-findings.py`, `metrics.py`, any schema, any wrapper.

## Hard constraints

- An existing flat run, and an existing run with units, must keep working untouched.
  Every new guard is a no-op when its new state is empty.
- The three-outcome doctrine: an *unestablished* input is `undetermined` (exit 2),
  not `not-ok` (exit 1). Do not blur them to make a gate feel stronger.
- `rungraph.py` keeps importing nothing but stdlib and `lider.findings`.
- ASCII-only in anything printed to a console.
- All 133 existing tests keep passing; every new guard gets tests.

## Design (decided; the implementer does not re-decide these)

**Part 0 — three defects found while evaluating, fixed first.**

- **0a. `gate` is not a dry run.** It snapshots state, lets `cmd_enter` commit, then
  restores — destroying any write that landed in between and bumping `updated_at` on
  a *query*, which perturbs `resolve_run`'s "most recently updated run". Split the
  guard chain into a pure `evaluate(state, dest, unit, force) -> (code, message)`
  that mutates nothing. `cmd_enter` calls it then saves; `cmd_gate` calls it and
  returns, with no `save` at all.
- **0b. `unit add --force` on an existing id appends a shadow unit.** `find_unit`
  returns the first match, so the second stays `pending` forever while
  `unfinished_units` still counts it — the join barrier becomes permanently
  unopenable except by force. With `--force`, replace in place.
- **0c. The pinned spec hash is never read again.** `cmd_spec` stores
  `path`/`sha256`/`text` and nothing ever re-checks it, so an implementer can work
  from a file that no longer matches what the ledger records was decided. Re-verify
  at `enter implement` (run and unit): file missing or unreadable → **UNDETERMINED**;
  present but different → REFUSED, telling the operator to re-pin. This is the one
  new guard that checks a declaration against an **external fact**, which is why it
  ranks first.

**Part 1 — open questions, three-valued.**
`state["questions"]` of `{id, text, status: open|answered|assumed, answer, unit, at}`.
`question add --text ... [--unit X]`; `question resolve --id q1 --status
answered|assumed --answer ...` where **`assumed` requires `--answer`**: you may
proceed on an assumption, but only one written down. Guard: `enter implement`
returns **UNDETERMINED** while any question is open — an unanswered input is
literally an unestablished one, so it reuses the exit-code semantics exactly rather
than inventing a new meaning. `assumed` does not block; it is surfaced in `show`.

**Part 2 — acceptance criteria and coverage as a checked relation.**
`state["criteria"]` of `{id, text, status: required|deferred, reason, at}`, declared
explicitly (`criterion add --id AC1 --text ...`), never parsed out of spec prose —
parsing is fragile and format-coupled. `criterion defer --id AC1 --reason ...` with
`--reason` **required**, mirroring how `dropped` makes a descope visible rather than
silent. `unit add --covers AC1,AC3` stores `unit["covers"]` and refuses an
undeclared id, reusing the shape of the existing undeclared-dependency refusal.

Two guards, both no-ops when no criteria are declared:
- `enter plan` refuses while any **required** criterion is covered by no unit, naming them.
- `unit add` refuses an empty `--covers` when criteria exist — a unit mapping to
  nothing is unplanned scope.

**Part 3 — `next`, read-only.** Reports the run node, its legal successors, and per
unit `{id, node, eligible, blocked_by}`, plus how many units could run concurrently
right now. No save, no mutation, `updated_at` untouched. Records the width to
`metrics.jsonl` as a new kind.

## Acceptance criteria

1. `gate` never writes: `updated_at` is byte-identical before and after, and a write
   from another process between load and return is not lost.
2. `unit add --force` on an existing id leaves exactly one unit with that id.
3. A spec file modified after pinning refuses `enter implement`; a deleted one
   returns UNDETERMINED; `--force` records the override.
4. An open question makes `enter implement` return **2**, not 1. `assumed` without
   `--answer` is refused. `answered`/`assumed` unblock.
5. A required criterion covered by no unit refuses `enter plan`, naming it;
   deferring it with a reason unblocks; the refusal text says the check is of the
   mapping, not of the implementation.
6. `next` mutates nothing and reports eligibility that matches `unblocked()`.
7. All 133 pre-existing tests pass untouched.

## Mandatory verification

```bash
python -m pytest -q            # everything, including the new guards
python -m pytest -q -m "not slow"
```

## Risk

Reversible and additive: every new guard is a no-op when its state key is empty, so
an existing run is unaffected. The authorised risk is the honesty constraint above —
a coverage gate that is bookkeeping-only could be mistaken for a substance check.
Mitigated in refusal text and docs, not by code.
