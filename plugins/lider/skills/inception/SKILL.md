---
name: inception
description: >
  Run a separate Inception (discovery) ledger: pin a frame, declare checkable
  criteria/questions/units, optionally challenge, seal an operational handoff
  under .lider/handoffs/. RECOMMENDED before /pipeline construction; required
  only in strict mode. Use when starting non-trivial work, clarifying scope, or
  /inception.
argument-hint: "<theme or feature> [--strict]"
---

# Inception — discovery as its own run

**Recommended, not required.** Construction (`/pipeline`) still has a flat path
(`init → spec → implement`) for small tickets. Use this skill when the work needs
a sealed handoff: criteria, open questions, unit mapping. Say that recommendation
aloud when the user skips it on a large feature.

**Strict mode** (`--strict` on `init`, or `LIDER_STRICT=1`):
- Inception **must** perform a challenge before `enter sealed`
- Construction **must** `import --handoff` before `enter implement`

## Harness root

```bash
LIDER="${LIDER_PLUGIN_ROOT:-${GROK_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}}"
RG=(python "${LIDER}/scripts/rungraph.py")
```

## Flow

1. **Start an inception run** (not construction):

```bash
"${RG[@]}" init --title "<theme>" --kind inception [--strict] --run <id>
```

2. **Write a frame doc** (discovery: scope, constraints, risks, open design). Pin it:

```bash
"${RG[@]}" spec --file <frame.md> --run <id>
"${RG[@]}" enter spec --run <id>
```

Frame is not the build spec. Construction will pin its own how-to-build file later.

3. **Checkable only** (substance stays prose in the frame):

```bash
"${RG[@]}" criterion add --id AC1 --text "..." --run <id>
"${RG[@]}" question add --text "..." --run <id>
"${RG[@]}" question resolve --id q1 --status answered --answer "..." --run <id>
# assumed requires --answer too
"${RG[@]}" unit add --id auth --covers AC1 [--depends-on other] --run <id>
```

Coverage is a **mapping** check (criteria ↔ units), not proof the design is good.
Say that when you report.

4. **Challenge (optional by default):** other-family pressure test for high-risk work.
   Prompt content: `rungraph template --role challenger` (G2 — wording, not a new edge).

```bash
"${RG[@]}" assign --role challenger --engine grok --run <id>   # example
"${RG[@]}" enter challenge --run <id>
# or: check --name challenge --verdict ok --evidence "..."
```

Non-strict seal **warns** if this was skipped. Strict seal **refuses**.

5. **Seal** — writes `.lider/handoffs/<run-id>.json` (operational, under `.lider/`):

```bash
"${RG[@]}" enter sealed --run <id>
"${RG[@]}" show --run <id>
```

## From a session log (trace → graph)

When discovery already happened in a chat/notes dump, **do not retype the ledger
by hand**. Reify the log into a plan, then seed inception:

```bash
# 1) parse only → .lider/plans/<title>.plan.json (no ledger writes)
"${RG[@]}" extract --file session.md [--out plan.json]

# 2) seed an inception run (frame pin + criteria + questions + units). Does NOT seal.
"${RG[@]}" --run <id> apply-plan --plan plan.json --init --enter-spec --title "<theme>"

# or one shot:
"${RG[@]}" --run <id> extract --file session.md --apply --init --enter-spec --title "<theme>"
```

Heuristic extract is **deterministic** (no LLM): it reads markdown sections
(`## Scope`, `## Hard constraints`, `## Acceptance criteria`, `## Open questions`,
`## Units`) and lines like `AC1: …`, `unit auth: …`, `covers:`, `depends on:`.
You can also hand-author a JSON plan (`kind: lider.session.plan`) and pass that
to `extract` / `apply-plan`.

**Still required after apply:** review the frame, fix mapping, resolve open
questions (or assume with `--answer`), optional challenge, then `enter sealed`.
Coverage of criteria by units remains a **mapping** check only.

## Hand off to construction

```bash
"${RG[@]}" init --title "<build>" --kind construction [--strict] --run <build-id>
"${RG[@]}" import --handoff .lider/handoffs/<inception-id>.json --run <build-id>
"${RG[@]}" spec --file <build-spec.md> --run <build-id>   # how to implement
# then /pipeline from enter spec onward, or the usual construction graph
```

## Artifacts (what seal checks)

| Step | Consumes | Produces |
|---|---|---|
| `spec` | discovery brief | frame file + hash |
| criteria / questions / units | frame | ledger objects (mapping only) |
| `challenge` | frame (optional; **required in strict**) | path includes challenge |
| `sealed` | closed questions, covered criteria | `.lider/handoffs/<id>.json` + sha256 |

`rungraph.py show` lists these under **artifacts:** before you hit a seal refusal.

**Loop vs graph:** challenge/re-spec is a small loop on the discovery graph — do not add persona or “component” nodes. If it is not checkable, keep it prose in the frame.

## Standing rules

- Three outcomes: `ok` / `not-ok` / `undetermined` (exit 2). Do not round down.
- No implement, review, promote in this run — edges refuse them.
- Do not invent persona/component gates; only checkable ledger objects above.
