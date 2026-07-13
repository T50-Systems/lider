# t50-flow

Marketplace local del plugin `t50-flow`: empaqueta nuestro flujo de trabajo (Fable planifica → implementador → pair-review con Codex CLI → verificación → promoción por PRs).

Este hito (M2) incluye solo el núcleo:
- `scripts/codex-exec.sh` — wrapper endurecido de Codex CLI (sin procesos zombie, timeout controlado, JSON validado).
- `agents/pair-reviewer.md` — agente que pide una segunda opinión adversarial a Codex, con fallback a revisión propia.
- `skills/pair-review/SKILL.md` — skill `/pair-review` para revisar el diff actual antes de commitear.

## Requisitos

- Codex CLI >= 0.144.1 disponible en PATH (verificar con `codex --version`).

## Instalación

Desde Claude Code:

```
/plugin marketplace add C:\dev\t50-flow
/plugin install t50-flow@t50-flow
```
