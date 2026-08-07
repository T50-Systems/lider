---
name: schedule
description: >
  Plan parallel unit waves from the construction ledger deps. Use when a phase
  has multiple units and you want to know what can run together, print worktree
  commands, or cap concurrency. Does not execute engines — ledger stays arbiter.
  Triggers: /schedule, parallel units, worktrees, fan-out implement.
argument-hint: "[--max-width N] [--format text|json|commands]"
---

# Schedule — parallel plan without holding the graph in your head

`next` = who is READY **right now**.  
`schedule` = **all waves** given `depends_on`, assuming earlier waves finish.

Neither command runs implementers. You (or host agents) still launch work and
every `enter` still goes through the guard. That is intentional: the ledger
refuses illegal edges; the schedule only removes the mental dependency chart.

## When to use

- Phase declared several units and sequential pipeline feels too slow.
- You have multiple agents/worktrees (Claude, Grok, OpenCode, Pi, Codex) and need
  a clear wave list.
- You want a hard cap (`--max-width 2`) because only two worktrees are comfortable.

## Commands

```bash
LIDER="${LIDER_PLUGIN_ROOT:-${GROK_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}}"
RG=(python "${LIDER}/scripts/rungraph.py")

"${RG[@]}" schedule --run <id>
"${RG[@]}" schedule --run <id> --format json
"${RG[@]}" schedule --run <id> --format commands --max-width 2
```

### Reading the plan

| Field | Meaning |
|---|---|
| wave 0 | Ready **now** (same idea as `next` READY set) |
| wave N>0 | Becomes ready after previous waves' units are `done`/`dropped` |
| in flight | Already past `pending` — do not re-schedule |
| stuck | Deps missing or cyclic — fix `unit add --depends-on` |

### How to actually parallelize (recommended)

1. `schedule --format commands` → copy the worktree + `enter implement --unit` lines.
2. **One git worktree per unit in the current wave** (never two implementers on one tree).
3. Launch one host/engine per worktree (`agent-implement` or that host's agent).
4. When a unit hits `done`, re-run `schedule` (or trust the next wave) and open the next set.
5. `enter join` only when schedule shows no unfinished units.

### What this is not

- Not a process supervisor (that is `agent-implement` / runtime).
- Not auto-merge of worktrees (you merge when units are done).
- Not a replacement for `enter` guards.

If `metrics-report --section parallelism` shows `max_width` always 1, you never had
independent units — schedule will also be sequential; that is data, not a bug.
