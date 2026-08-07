---
name: operate
description: >
  Run an Operations ledger for shared or deployed state: pin target, preflight
  (ternary), act, prove effect, optional soak, close. RECOMMENDED around deploys
  and merges to shared envs; required under strict mode for preflight before act
  and effect before close. Use before/after promote, deploy, or /operate.
argument-hint: "<what you will touch> [--strict]  e.g. 'deploy main to prod'"
---

# Operations — prove conditions, then prove arrival

`/pipeline` (construction) answers *"did we build the right thing?"*  
`/inception` answers *"what are we building?"*  
This answers *"may I touch shared state, and did the change arrive?"*

**Recommended, not required** for pure local work. **Say so** when you skip it on a
deploy to a shared environment. **Strict** (`init --strict` / `LIDER_STRICT=1`):
preflight `ok` before `act`; `effect` or `prove` `ok` before `closed`.

How to check is still in **`/preflight`** and **`/verify`** — this skill is the
**ledger** that records those ternary results so a resumed session cannot round
`undetermined` down to GO.

## Harness root

```bash
LIDER="${LIDER_PLUGIN_ROOT:-${GROK_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}}"
RG=(python "${LIDER}/scripts/rungraph.py")
```

## Flow

1. **Start an operations run**

```bash
"${RG[@]}" init --title "<action>" --kind operations [--strict] --run <id>
```

2. **Pin the target** (checkable declaration — not a live probe by itself)

```bash
"${RG[@]}" target --env prod --ref <sha|tag> [--url https://...] \
  [--surfaces api,web] [--construction-run <build-run-id>] --run <id>
"${RG[@]}" enter scope --run <id>
```

3. **Preflight** — follow `/preflight` (locks, both deltas, in-flight, forge).  
   Then record the overall verdict (never invent GO):

```bash
"${RG[@]}" check --name preflight --verdict ok|not-ok|undetermined \
  --evidence "<one line>" --run <id>
# undetermined or not-ok: enter blocked, do not act
"${RG[@]}" enter preflight --run <id>
"${RG[@]}" enter act --run <id>    # STRICT refuses without preflight ok
```

4. **Act** — deploy, merge, dispatch (use `/promote` when that is the act).  
   Do not act twice "just in case."

5. **Prove effect** — follow `/verify` (content on branch + what the env serves).  
   Record:

```bash
"${RG[@]}" check --name effect --verdict ok|not-ok|undetermined \
  --evidence "<sha on /api/version, path present, ...>" --run <id>
"${RG[@]}" enter prove --run <id>
```

6. **Optional soak** then **close**

```bash
"${RG[@]}" enter soak --run <id>     # optional observe window
"${RG[@]}" enter closed --run <id>   # STRICT needs effect|prove ok
"${RG[@]}" show --run <id>
```

## Standing rules

- Three outcomes everywhere: `ok` / `not-ok` / **`undetermined` (exit 2)**.  
  Undetermined is **not** GO. Measured incidents: lock "all clear" during someone
  else's deploy; CI "finished" on API error; merge `true` with content unreachable.
- Prefer checkers that exit **2** for "could not look."
- Operations does not replace construction `promote → effect` for a feature ship;
  it is the ledger when the work *is* operating on shared state (including after
  another session's promote).
- No substance gates (no "ops review narrative required"). Only target + ternary checks.

## Relation to other skills

| Skill | Role |
|---|---|
| `/preflight` | How to establish GO/NO-GO/UNDETERMINED before touching shared state |
| `/verify` | How to prove effect, not intent |
| `/promote` | Often the **act** step for git promotion |
| `/operate` | Ledger that records those results and refuses silent skips in strict mode |
