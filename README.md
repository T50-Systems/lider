# lider

Plugin de Claude Code que orquesta nuestro flujo de trabajo, **agnóstico de motor**: el arquitecto especifica y adjudica, un implementador ejecuta, un segundo motor revisa (Codex hoy, con fallback a Claude si no está), y el trabajo se promociona por PRs. Se distribuye en el marketplace `t50`.

## Skills

- `/pair-review [ámbito]` — revisión independiente del diff actual con un segundo motor; sin procesos zombie (timeout duro), salida estructurada, y fallback a revisión propia si el segundo motor no responde.
- `/pipeline <descripción>` — una fase completa: spec cerrada → implementador en background → pair-review → adjudicación finding a finding → verificación → commit del arquitecto → promoción.
- `/promote [--yes] [título]` — promoción por PRs: rama → PR a `dev` → merge → gate a producción → PR `dev`→`main` → merge → sync local.

## Piezas

- `scripts/codex-exec.sh` — wrapper endurecido del segundo motor (timeout con escalada a SIGKILL, config aislada por invocación, JSON validado).
- `agents/pair-reviewer.md` — agente revisor con fallback obligatorio.
- `schemas/findings.schema.json` — contrato de salida (engine, verdict, findings).

## Requisitos

- Segundo motor de revisión: Codex CLI >= 0.144.1 en PATH (`codex --version`). Sin él, `/pair-review` cae al fallback de Claude.

## Instalación

Desde Claude Code:

```
/plugin marketplace add C:\dev\lider
/plugin install lider@t50
```
