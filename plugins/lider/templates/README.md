# Templates (G2 — structure vs content)

**Structure** lives in `scripts/lider/rungraph/constants.py` and guards.  
**Content** for role prompts lives here. Change wording without touching edges.

| Path | Role |
|---|---|
| `roles/architect.md` | Spec + adjudication seat |
| `roles/implementer.md` | Build from closed spec; no commit |
| `roles/reviewer.md` | Cross-family review → findings schema |
| `roles/challenger.md` | Optional inception / high-risk pressure test |

Skills (`pipeline`, `inception`, …) **must** load or point at these files rather
than inventing a parallel prompt in prose only. The ledger still requires
`assign` + `enter` — templates are content, not control flow.

List / print via CLI:

```bash
python scripts/rungraph.py template --list
python scripts/rungraph.py template --role implementer
```
