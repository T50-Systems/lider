---
name: audit
description: "Multi-engine consensus audit - run the SAME read-only analysis across >=4 engines of different model families (Claude-Sonnet, Claude-Opus, Grok, GPT-Codex), map consensus vs contrarian, VERIFY contrarian findings against the code, and produce severity-ranked findings + tracked issues. Use for reliability / security / performance / availability / flakiness / compliance audits where cross-engine agreement = confidence and a single engine's contrarian finding catches what the others miss. Findings feed `pipeline` for the fix."
argument-hint: "<dimension(s)> [target path/area] - e.g. 'security,reliability' or 'audit the payment module for performance'"
---

You act as the architect and adjudicator. You do NOT do the analysis yourself — you run a **battery of independent engines** over the same closed brief, then consolidate, verify, and rank. Follow the flow in order; do not skip the verification step.

## Why multi-engine (the whole point)

One engine is one blind spot. Value comes from **two forces**:
- **Consensus** — when >=3 engines independently flag the same thing, it is almost certainly real. Rank it high, act on it.
- **Contrarian** — when ONE engine flags something the others *praised as a strength* or missed entirely, that is often the highest-value catch — but also the most likely to be wrong. **Never act on a contrarian finding without verifying it against the code yourself.** (Real case: Codex flagged an advisory-lock leak that two other engines had explicitly listed as a strength; reading the code confirmed Codex was right — `client.release()` returns a lock-holding connection to the pool. Two engines were confidently wrong.)

Model-family **diversity** is what makes the battery work. Four Claude models share Claude's blind spots. Aim for >=4 engines spanning **>=3 families**: Claude (Sonnet + Opus), xAI (Grok), OpenAI (GPT-Codex).

## Engine battery (>=4, diverse families) — all READ-ONLY

| Engine | Family | Mechanism | Read-only lockdown |
|---|---|---|---|
| **Claude-Sonnet** | Claude | `Explore` subagent (NOT `general-purpose` — see gotcha #1) | Explore has no write/Agent tools |
| **Claude-Opus** | Claude | `Explore` subagent, `model: opus` | same |
| **Grok** | xAI | `invoke-grok` skill / `grok` CLI, `--effort high` | `--deny Edit --deny Write --deny Bash --tools "read_file,grep,list_dir" --no-subagents` |
| **GPT-Codex** | OpenAI | `pair-reviewer` agent, or `codex-exec.sh --model gpt-5.6-sol` | codex-exec is Lider-owned, read-only, findings schema |

Scale UP for `--thorough` / high-stakes: add a second Grok pass or a 5th engine. Scale DOWN only for a quick single-dimension check (but never below 3 families, or "consensus" is meaningless).

**All engine calls in this skill are analysis-only. Never let an audit engine edit, commit, or open issues** — that is the architect's job after adjudication, and the fix is a separate `pipeline` run.

## Flow

1. **Closed brief per dimension (architect seat).** For each dimension (reliability, security, performance, availability, flakiness, compliance, clinical-safety, ...), write ONE standalone brief — every engine gets the *same* one, so their outputs are comparable. A good brief: the target paths, the concrete sub-areas to probe (with known-suspect file:line leads to verify, not re-derive), "verify EVERY premise before asserting", "distinguish a REAL defect with a concrete failure/attack scenario from style", and the exact deliverable shape (verdict N/5 + findings ranked by severity, each with file:line + scenario + confidence + one-line fix). **Write the brief to a FILE** (gotcha #2). Reuse the same brief across all engines of that dimension.

2. **Fan out in parallel — all engines, all dimensions.** Launch every engine on its brief as a background task, in waves by dimension (the user's sequence, e.g. reliability+flakiness, then performance, then availability+security). Do NOT serialize engines. Each returns a structured report. Harvest as they complete; hold synthesis until a dimension's full battery is in.

3. **Consolidate cross-engine (architect seat).** Per dimension, build the map:
   - **Consensus tier** — findings >=2-3 engines agree on. Note the count (e.g. "5x across reliability+availability" is your strongest signal). High confidence.
   - **Contrarian tier** — single-engine findings, ESPECIALLY any that contradict another engine's "strength". Flag each for step 4.
   - **Per-engine unique** — sharp catches only one engine made (each family sees different things: one finds the KBA takeover, another the unaudited MCP query, another the sync-crypto blocker).
   - Record scores per cell (dimension x engine) and the consensus score.

4. **VERIFY the contrarian findings against code (MANDATORY — do not skip).** For every contrarian / strength-contradicting finding, READ the cited code yourself and adjudicate: CONFIRMED (real, with the concrete mechanism), or REFUTED (the engine misread). Also **adjudicate engine false-positives**: an engine can reason off stale or wrong context (real case: Codex declared the deploy pipeline "disabled" because it read the read-only GitHub mirror, not the canonical Forgejo workflow that was live). When engines disagree on a fact, the code / live state wins — verify, don't average.

5. **Rank + file issues (architect seat).** Rank by severity x cross-engine agreement. For each surviving finding write a tracked issue: title with severity emoji + one-line claim, body with the evidence (file:line, failure/attack scenario, which engines + confidence, "verified against code" when you did step 4, one-line fix). File issues to the repo's tracker via its API — **build request bodies with a file + a JSON-serializer, never inline in shell** (gotcha #3). Nothing gets a code change here.

6. **Executive summary + hand-off to `pipeline`.** Deliver: the dimension x engine score matrix, the consensus findings (high-confidence, act first), the multi-engine wins (what a single engine caught that others didn't), your adjudications (confirmed contrarian + refuted false-positives), and the filed issue list. Then the fix is a **separate `pipeline` run per finding** — where the same >=4-engine discipline applies across spec / implement / review / verify (architect Fable specs, an implementer builds, a *different* family reviews, you verify). Do not fix inside the audit.

## Gotchas (read `references/gotchas.md` before the first fan-out)

1. **A `general-purpose` subagent given a broad analysis task DELEGATES and returns nothing** — it spawns its own sub-agents and hands back "3 agents are running, I'll report when done." Use **`Explore`** for Claude-side analysis engines (no Agent tool → cannot delegate), or explicitly forbid delegation in the prompt.
2. **Grok's CLI breaks on an apostrophe inside a single-quoted `-p`** ("won't" ends the quote). Always write the brief to a FILE and pass `-p "$(cat file)"`.
3. **Bash eats backticks/quotes in inline API bodies** (even inside a Python `-c`). Write the issue/PR body to a file, then `python -c` `json.dump` it; POST with `--data-binary @file`.
4. **Re-pass every read-only lockdown flag on each engine call** — flags do not carry across invocations, and a bare re-run inherits the engine's default (which may be write-enabled).

## Rules

- Analysis-only end to end. The audit never writes code, never commits, never fixes. Its output is findings + issues; the fix is `pipeline`.
- Never average away a disagreement — verify it. A confident-but-wrong engine is the failure mode this skill exists to catch.
- Every filed issue must be independently actionable (file:line + scenario + fix) so `pipeline` can pick it up cold.
- Scale the battery to the stakes: >=4 engines / >=3 families is the floor; add passes for money / PHI / security / irreversible surfaces.
