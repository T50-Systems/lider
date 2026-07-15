# Using lider from another session

Copy-paste prompts to drive the `lider` plugin (marketplace `t50`) from any Claude Code session. See [../README.md](../README.md) and [../ARCHITECTURE.md](../ARCHITECTURE.md) for how it works.

## Full pipeline

Replace `[TASK]` with what you want built:

```text
Usa el plugin `lider` (marketplace t50) para esta tarea: [TASK].

Corre `/pipeline [descripción breve] --impl codex` (o `--impl opus` si quieres que
implemente Claude). El flujo: Fable escribe un spec cerrado → el implementador
elegido ejecuta en background con acceso total y auto-recuperación → el motor
OPUESTO revisa (regla cross-engine) → adjudicación contra el spec → verificación.

Mientras corre el implementador, NO adivines si está vivo: lee `<log>.status.json`
— el campo `activity` te narra qué hace Codex ahora mismo (`exec: <cmd>`,
`edit: <archivo>`, `say: <mensaje>`, `(running Ns)`), y `state`/`idle_s`/`exit` te
dan salud. Los watchdogs hacen fast-fail (exit 125) sin matar comandos largos
sanos; los fallos transitorios (timeout/429/5xx) se auto-recuperan desde un
checkpoint git limpio; un fallo de auth te avisa que corras `codex login`.

Al terminar, pásalo por `/pair-review` (segundo motor, con fallback a Claude)
antes de commitear. Cuando esté verificado, promuévelo con `/promote`.

Docs del plugin: README.md y ARCHITECTURE.md en el repo.
```

## Short variants

Review the current diff with the second engine:

```text
Corre /pair-review sobre el diff actual (segundo motor Codex, con fallback a Claude
si no responde). Devuelve findings estructurados y un veredicto.
```

Promote verified work to production:

```text
Corre /promote para llevar esto por PRs: branch → PR a dev → merge → gate → PR
dev→main → merge → sync local.
```

Pin the implementer/reviewer engines explicitly:

```text
/pipeline <descripción> --impl codex   # Codex implementa, Opus revisa
/pipeline <descripción> --impl opus    # Opus implementa, Codex/Sol revisa
```

## Monitoring a background implementer

The implement wrapper writes three things next to the log you pass it:

| Artifact | Use |
|---|---|
| `<log>` | full Codex stream (tail for detail) |
| `<log>.status.json` | live `{state, activity, elapsed_s, idle_s, exit, reason, started_at, updated_at}` — read this at a glance |
| `<done>` | final exit code once finished |

**Exit codes:** `0` ok · `124` hard timeout · `125` watchdog abort (stall / died at launch) · `127` codex missing · `130` cancelled by signal · other = codex's exit (classified transient → auto-retried, auth → actionable).

**Resume after a restart:** if you find a `status.json` you did not launch — `state=done|failed` is terminal; `state=running` with a fresh `updated_at` is still alive (re-attach); `running` with a stale `updated_at` and no `<done>` is an orphaned run (treat as failed and recover).
