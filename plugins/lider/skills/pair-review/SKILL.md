---
name: pair-review
description: "Pair-review the current diff with Codex as a second engine (with fallback). Use after implementing changes, before committing."
argument-hint: "[base-ref | scope description]"
---

1. **Capture the diff.** Use `git diff` (working tree). If it is clean, use `git diff <base-ref>...HEAD` with the ref passed as argument, or the last commit if none was given. Also add `git diff --stat`.

2. **If the diff exceeds ~400 lines, do not paste it whole.** Pass only the `--stat` and the list of changed files, and instruct Codex to read those files directly from the repo.

3. **Launch the `pair-reviewer` agent** (Agent tool, the plugin's `subagent_type`), passing it the diff (or the scope) and the repo working directory (cwd).

4. **On return:** present the findings grouped by severity and the final verdict. If there are BLOCKERs, do not repeat the NITs — prioritize what blocks.
