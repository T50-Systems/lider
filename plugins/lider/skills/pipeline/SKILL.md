---
name: pipeline
description: Ejecuta una fase completa del flujo T50: spec cerrada del arquitecto → implementador en background → pair-review con Codex → adjudicación → verificación → promoción. Úsalo para features acotadas con visto bueno final humano opcional.
argument-hint: "<descripción de la fase o feature>"
---

Actúas de arquitecto. Sigue el flujo en orden; no te saltes pasos.

1. **Spec cerrada.** Es el entregable más importante. Si la descripción del usuario es ambigua en ámbito, pregunta ANTES de lanzar nada. Rellena esta plantilla:
   - **Ámbito:** ficheros/paquetes exactos que se pueden tocar; qué NO tocar.
   - **Restricciones duras:** convenciones del repo (tipado, estilo, testids, i18n...), "NO hagas commit".
   - **Diseño:** decisiones ya tomadas con valores concretos (el implementador no decide arquitectura; sí reporta desviaciones con motivo).
   - **Verificación obligatoria:** comandos exactos (typecheck/build/tests) que deben salir verdes antes de terminar.

2. **Implementador.** Lanza un agente (herramienta Agent, `general-purpose`, en background) con la spec completa. Modelo: `sonnet` por defecto; usa un modelo superior solo si la fase exige decisiones ambiguas que la spec no pudo cerrar.

3. **Pair-review.** Al terminar el implementador, invoca la skill `pair-review` de este plugin sobre el diff resultante (el working tree sin commitear; si el implementador trabajó en rama, el diff de esa rama contra `origin/dev`).

4. **Adjudicación.** Por cada finding, decide y deja constancia: APLICADO (y aplica el fix — directamente si es trivial, o vía el implementador si es sustancial) o RECHAZADO con motivo de una línea. No apliques findings a ciegas.

5. **Verificación final.** Ejecuta TÚ los comandos de verificación de la spec — no te fíes solo del reporte del implementador. Si hay superficie observable (UI/API), verifícala de verdad.

6. **Commit del arquitecto.** El implementador NO commitea (lo prohíbe la spec): tras adjudicar y verificar, revisa TÚ `git status` y `git diff --stat`, y commitea el resultado en la rama de trabajo con mensaje convencional. Nada llega a `promote` sin un commit tuyo deliberado.

7. **Promoción.** Invoca la skill `promote` de este plugin (sin `--yes`: el gate a `main` queda en manos del usuario, salvo que él pidiera lo contrario).

8. **Cierre.** Resume la fase, los hallazgos adjudicados y el estado final en 5-8 líneas.
