# Audit gotchas — operational lessons

Every one of these cost a wasted run or a false conclusion. Read before the first fan-out.

## 1. `general-purpose` subagents delegate instead of analyzing

Given a broad, open-ended analysis task ("audit performance"), a `general-purpose` subagent tends to **spawn its own sub-agents and return a non-answer** — literally "3 research agents are now running, I'll report back when they complete" — then exits, having produced nothing. The parent (you) waits on a result that never comes with content.

- **Fix:** use the **`Explore`** subagent type for Claude-side analysis engines. Explore's tool set excludes the Agent/Task tool, so it *cannot* delegate — it reads and reports itself.
- If you must use `general-purpose`, put an explicit line in the prompt: *"Do it YOURSELF. Do NOT delegate, do NOT spawn sub-agents, do NOT wait for anyone — read the code with your tools and compile the report now."*
- Symptom to catch early: a subagent returns fast with a "launched N agents" message and no findings. That run is dead; re-dispatch as Explore.
- Silver lining observed: the orphaned leaf sub-agents sometimes DID produce real findings (the middle-manager returned nothing, the leaves worked). Harvest anything useful, but don't rely on it.

## 2. Grok CLI: apostrophes and inline prompts

`grok -p 'text with won't in it'` — the apostrophe in "won't" closes the single quote and the rest is mis-parsed; Grok reports an empty prompt or garbles the task.

- **Fix:** write the brief to a file and pass `-p "$(cat brief.txt)"`.
- Standard read-only invocation:
  ```
  grok -p "$(cat brief.txt)" --cwd <repo> --output-format json --effort high \
    --deny "Edit" --deny "Write" --deny "Bash" --tools "read_file,grep,list_dir" \
    --no-subagents --max-turns 45
  ```
- `--effort high` always (contrast is the reason to pay the process hop).
- Parse the JSON with UTF-8 explicitly (`io.open(path, encoding='utf-8')`) — Windows `cp1252` chokes on the em-dashes/emoji in Grok's output.
- Grok's JSON endpoints for CI/actions are flaky; for run status prefer `commits/<sha>/status`.

## 3. Never build API bodies inline in shell

Constructing an issue/PR/comment body inline in bash — even inside a `python -c "..."` — gets mangled: bash consumes backticks and unbalanced quotes *before* Python sees them, and a stray apostrophe in the body aborts the whole compound command ("unexpected EOF while looking for matching `'`").

- **Fix:** write the body with the Write tool (or a heredoc with a quoted delimiter) to a `.md` file, then:
  ```
  python -c "import json,sys; json.dump({'title': T, 'body': open(sys.argv[1],encoding='utf-8').read()}, open(sys.argv[2],'w',encoding='utf-8'))" body.md payload.json
  curl ... --data-binary @payload.json <api>/issues
  ```
- For a batch, drive the whole thing from a small Python script (Write it, then run it) — no bash loop over strings with special chars.

## 4. Flags do not carry across engine invocations

Every `grok`/`codex` call re-reads config. A follow-up call without the read-only denies inherits the engine's *default* permissions (which may be write-enabled / always-approve). **Re-pass the full lockdown on every call, including resumes.**

## 5. Adjudicate engine false-positives — models reason off wrong context

An engine can be confidently wrong because it read the wrong thing:
- Codex declared the deploy pipeline "disabled / Cloud Run TF commented" — it had read the **read-only GitHub mirror** (`.github/workflows-disabled/`), not the canonical **Forgejo** workflow that was live and had deployed four times that night.
- Another engine called prod "paused/deleted" from a stale ADR, when prod was live.
- Conversely, one engine correctly caught that prod scales to zero (no `--min-instances`) when the others trusted the docs.

**When engines disagree on a fact, the live state / code wins.** Verify against ground truth; never average two engines' guesses into a "consensus" that is really just shared ignorance.

## 6. Consensus counting

- A finding is only "consensus" if the engines reached it **independently** (same brief, separate runs) — not if one engine's report leaked into another's context.
- Track the count and *where* it recurred. A reliability finding that also surfaces in the availability audit ("/api/health doesn't reflect the worker") — appearing 5x across two dimensions and three engines — is your single strongest signal.
- A lone contrarian is NOT weak by default. Rank it by *verified* severity (step 4), not by vote count. The best catches of a multi-engine audit are usually 1-of-4, then confirmed against code.
