---
name: pair-review
description: Pair-review del diff actual con Codex como segundo motor (con fallback). Úsalo tras implementar cambios, antes de commitear.
argument-hint: "[base-ref | descripción del ámbito]"
---

1. **Captura el diff.** Usa `git diff` (working tree). Si está limpio, usa `git diff <base-ref>...HEAD` con el ref recibido como argumento, o el último commit si no se dio ninguno. Añade también `git diff --stat`.

2. **Si el diff supera ~400 líneas, no lo pegues entero.** Pasa solo el `--stat` y la lista de ficheros cambiados, y ordena a Codex que lea esos ficheros directamente del repo.

3. **Lanza el agente `pair-reviewer`** (herramienta Agent, `subagent_type` del plugin) pasándole el diff (o el ámbito) y el directorio de trabajo (cwd) del repo.

4. **Al volver:** presenta los findings agrupados por severidad y el veredicto final. Si hay BLOCKERs, no repitas los NITs — prioriza lo bloqueante.
