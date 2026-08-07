# Prompt graph engineering (G1–G4) in Lider

Lider is **prompt graph engineering** in the sense of Macedo (arXiv:2607.27578):
an explicit, executable, improvable graph of prompt-mediated work. This note is
the product vocabulary — use it in design reviews and PRs.

## The four conditions

| Id | Condition | Lider |
|---|---|---|
| **G1** | Explicit structure | Edge tables in `lider/rungraph/constants.py` (`GRAPH`, `UNIT_GRAPH`, inception, operations). `enter` / `gate` refuse non-edges. |
| **G2** | Separation of structure and content | **Structure** = tables + guards only. **Content** = pinned specs, findings JSON, and **role templates** under `plugins/lider/templates/roles/`. Tune wording without editing edges. |
| **G3** | Executable semantics | **Ledger-as-arbiter:** who may `enter`, what blocks, ternary checks. **Not** auto-launch of LLM nodes. `next` / `schedule` are advisory. The host agent still launches engines. |
| **G4** | First-class artifact | `run.json`, sealed handoffs, session plans, metrics rows. `rungraph snapshot` exports structure + content pins for audit. |

### G3 honesty (do not “fix” with a scheduler)

| What G3 means here | What it does **not** mean |
|---|---|
| Transition semantics: legal edges, undetermined-as-block, convergence | A runtime that fires implement/review by itself |
| `schedule` prints waves / worktree commands | Engines executed by the ledger |

Converting `schedule` into an engine executor would fight the design. Keep it.

## Design checklist (PR / new skill / new node)

Answer before merging:

1. **Node or loop?**  
   Retry/converge on one act → loop *inside* a node (adjudication rounds, fanout).  
   Multi-role edge, barrier, or resume-across-sessions → graph edge/node.
2. **Structure or content?**  
   Edge/guard change → `constants.py` / `guards.py`.  
   Wording for a role → `templates/roles/*.md` or a pinned spec — **not** a new edge.
3. **Artifact or transcript?**  
   Must survive session death → ledger pin / handoff / plan / `check` / findings.  
   Narrative only → prose in frame/spec; do not invent a node.
4. **Explicit or emergent?**  
   Next step requires `enter` / `assign` / guard.  
   “The model decides the flow without the ledger” is a **G1/G4 regression**.

## Explicit vs emergent (boundary)

| Explicit (Lider) | Emergent (excluded) |
|---|---|
| `assign` + cross-family review | Free multi-agent chat as the only control plane |
| `enter` every transition | Host invents the next phase from vibes |
| Fan-out / refute as tools feeding `findings` | Unstructured “committee” with no schema |
| `extract` / `apply-plan` reifies a session log | Leaving discovery only in chat history |

Skills **guide** with prose; the **ledger decides**. A skill that says “skip `enter` and just continue” is wrong.

## Snapshot for audit

```bash
python "${LIDER}/scripts/rungraph.py" snapshot
python "${LIDER}/scripts/rungraph.py" snapshot --json
python "${LIDER}/scripts/rungraph.py" snapshot --out .lider/runs/<id>/snapshot.json
```

Separates **structure** (kind, legal edges, plugin version) from **content pins**
(spec hash, roles, open severe findings, artifact checklist).

## Role templates (G2)

```bash
python "${LIDER}/scripts/rungraph.py" template --role implementer
python "${LIDER}/scripts/rungraph.py" template --role reviewer --path
```

Paths: `plugins/lider/templates/roles/{architect,implementer,reviewer,challenger}.md`.  
Edit templates to change node *content*; leave graph tables alone.
