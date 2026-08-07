# Using lider from another session

Copy-paste prompts to drive the `lider` plugin (marketplace `t50`) from **Grok Build**
or **Claude Code**. See [../README.md](../README.md) and [../ARCHITECTURE.md](../ARCHITECTURE.md)
for how it works.

## Full pipeline

Replace `[TASK]` with what you want built:

```text
Usa el plugin `lider` (marketplace t50) para esta tarea: [TASK].

Corre `/pipeline [descripción breve]` — si no fijas `--impl opus|sonnet|fable|grok`, el
pipeline te pregunta qué motor implementa. El flujo: el arquitecto escribe un spec
cerrado → el implementador ejecuta con supervisión Lider → el motor de la OTRA familia
revisa (regla cross-engine) → adjudicación contra el spec → verificación.

Mientras corre un implementador externo, NO adivines si está vivo: lee `<log>.status.json`
— el campo `activity` narra qué hace ahora (`exec: <cmd>`, `edit: <archivo>`,
`(running Ns)`), y `state`/`idle_s`/`exit` dan salud. Los watchdogs hacen fast-fail
(exit 125) sin matar comandos largos sanos; los fallos transitorios se auto-recuperan
desde un checkpoint git limpio.

Al terminar, pásalo por `/pair-review` (segunda familia, con fallback al host) antes
de commitear. Cuando esté verificado, promuévelo con `/promote`.

Docs: README.md y ARCHITECTURE.md en el repo.
```

## Short variants

Review the current diff with the second engine family:

```text
Corre /pair-review sobre el diff actual (segunda familia de motor; bajo Grok → claude,
bajo Claude Code → grok; fallback al host si el segundo no responde). Devuelve
findings estructurados y un veredicto.
```

Promote verified work to production:

```text
Corre /promote para subir el trabajo verificado (PR a dev, merge, gate a main).
```

Preflight before touching shared state:

```text
Corre /preflight antes de tocar estado compartido (deploy, merge a rama compartida,
migración). Solo da veredicto de lo que pudo establecer.
```

## Install check (Grok)

```bash
grok plugin list
grok plugin details lider
# expect: 5 skills, 1 agent, enabled
```
