---
name: operate
description: >
  Operations ledger for shared/deployed state: target, preflight, act, prove,
  soak, close — plus incident → rollback (or forward fix) when effect fails.
  RECOMMENDED around deploys; strict requires preflight before act, effect before
  close, incident signal, and previous_ref for rollback. Use /operate, deploy,
  rollback, incident.
argument-hint: "<action> [--strict]  e.g. 'deploy main to prod' | 'rollback prod'"
---

# Operations — conditions, arrival, incident, rollback

`/pipeline` = did we build the right thing?  
`/inception` = what are we building?  
This = **may I touch shared state, did it arrive, and if not, how do we recover?**

How to check remains **`/preflight`** and **`/verify`**. This skill is the **ledger**
that records ternary results and refuses silent skips in strict mode.

## Graph

```
init → scope → preflight → act → prove → soak? → closed
                      ↘                ↘ incident ⇄ rollback → prove → …
                         blocked / escalated
```

| Node | Meaning |
|---|---|
| scope / preflight / act / prove / soak / closed | Happy path (ship + prove) |
| **incident** | Declared failure/uncertainty (check signal, not a war-room essay) |
| **rollback** | Revert toward `previous_ref` (must **prove** again after) |
| act from incident | Forward fix (hot patch) instead of revert |

## Harness root

```bash
LIDER="${LIDER_PLUGIN_ROOT:-${GROK_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}}"
RG=(python "${LIDER}/scripts/rungraph.py")
```

## Happy path

```bash
"${RG[@]}" init --title "deploy prod" --kind operations [--strict] --run <id>
"${RG[@]}" target --env prod --ref <new> --previous-ref <old> \
  [--url https://...] [--surfaces api,web] --run <id>
"${RG[@]}" enter scope --run <id>
# /preflight →
"${RG[@]}" check --name preflight --verdict ok --evidence "..." --run <id>
"${RG[@]}" enter preflight --run <id>
"${RG[@]}" enter act --run <id>          # deploy / promote
# /verify →
"${RG[@]}" check --name effect --verdict ok --evidence "..." --run <id>
"${RG[@]}" enter prove --run <id>
"${RG[@]}" enter soak --run <id>         # optional
"${RG[@]}" enter closed --run <id>
```

## Incident + rollback (checkable)

When `/verify` fails or health degrades:

```bash
"${RG[@]}" check --name effect --verdict not-ok \
  --evidence "prod /api/version still old OR 5xx on /health" --run <id>
# or: check --name incident --verdict not-ok --evidence "..."
"${RG[@]}" enter incident --run <id>     # STRICT needs that signal

# Re-preflight the REVERT (locks, in-flight, both deltas):
"${RG[@]}" check --name rollback-preflight --verdict ok \
  --evidence "lock held; rolling back to previous_ref" --run <id>
"${RG[@]}" enter rollback --run <id>     # STRICT needs previous_ref + preflight ok

# After revert, prove the GOOD ref is live:
"${RG[@]}" check --name effect --verdict ok \
  --evidence "prod serves previous_ref <old>" --run <id>
"${RG[@]}" enter prove --run <id>
"${RG[@]}" enter closed --run <id>
```

Forward fix instead of rollback: from `incident` use `enter act` (hot patch), then prove.

Reopen after close: `enter incident` from `closed` if discovery is late.

## Recommended vs strict

| Gate | Default | Strict |
|---|---|---|
| act without preflight ok | WARNING | REFUSED |
| closed without effect\|prove ok | WARNING | REFUSED |
| incident without failure signal | WARNING | REFUSED |
| rollback without preflight + previous_ref | WARNING | REFUSED |

## Standing rules

- Three outcomes: `ok` / `not-ok` / **`undetermined`**. Undetermined is not GO.
- **not-ok effect does not block `enter incident`** — it is the signal to go there.
- Rollback without a second **prove** is incomplete; closed still wants effect ok.
- No substance gates (no required “RCA narrative” node). Evidence is one-line checks.
- Prefer checkers that exit **2** for “could not look.”

## Relation to other skills

| Skill | Role |
|---|---|
| `/preflight` | How to establish GO before act **or** rollback |
| `/verify` | How to prove effect (post-act or post-rollback) |
| `/promote` | Often the **act** for git promotion |
| `/operate` | Ledger + incident/rollback graph |
