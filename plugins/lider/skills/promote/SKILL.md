---
name: promote
description: Promociona el trabajo actual por el flujo de PRs: rama → PR a dev → merge → PR dev→main → merge → sync local. Úsalo cuando el trabajo esté verificado y listo para producción.
argument-hint: "[--yes] [titulo del cambio]"
---

0. **Precondiciones.** Verifica antes de nada; si falla alguna, informa al usuario y detente:
   - `git remote get-url origin` existe y `gh auth status` está ok.
   - Existe rama `dev` en origin (`git ls-remote --heads origin dev`). Si NO existe: dile al usuario que este flujo requiere `dev` y ofrece crearla — no la crees sin confirmación.
   - `git fetch origin` y comprueba que hay trabajo que promover: `git log --oneline origin/dev..HEAD` (commits por delante) o `git status --porcelain` (cambios sin commitear). Si no hay nada, repórtalo y para.
   - No existe ya un PR abierto `base:main head:dev` (`gh pr list --base main --head dev --state open`). Si existe, detente y repórtalo — no dupliques promociones en curso.

1. **Fija la rama de trabajo.** Resuelve `WORK_BRANCH=$(git branch --show-current)` UNA vez, al inicio, y usa ese valor literal en todos los pasos siguientes (push, PR, borrado) — nunca vuelvas a deducir "la rama actual" a mitad de flujo.
   - Si `WORK_BRANCH` es `main` o `dev` y hay cambios sin commitear: crea una rama `tipo/slug-corto` desde `dev` (tipo = `feat`|`fix`|`chore`), commitea ahí con mensaje convencional, y esa pasa a ser `WORK_BRANCH`. NUNCA commitees directo en `main`/`dev`.
   - Si `WORK_BRANCH` es una rama de trabajo con cambios sin commitear: commitéalos en ella antes de continuar (nada se promociona sin commit).
   - Si `WORK_BRANCH` es una rama de trabajo con commits y árbol limpio: úsala tal cual.

2. **PR a dev.** `git push -u origin "$WORK_BRANCH"` y `gh pr create --base dev --head "$WORK_BRANCH"` con cuerpo que incluya "## Summary" y "## Validation" (checklist real de lo verificado en la sesión: tests/typecheck/navegador — no inventes checks que no se ejecutaron). Guarda el número de PR que devuelve.

3. **Merge a dev.** `gh pr merge <n> --merge --delete-branch` y verifica que quedó fusionado (`gh pr view <n> --json state` → `MERGED`). Si el merge falla o queda bloqueado (checks/protección/conflictos), reporta el motivo y detente — no lo puentees.

4. **GATE hacia main.** Continúa sin preguntar SOLO si los argumentos contienen el token exacto `--yes` como palabra separada. En cualquier otro caso (incluida la duda), PARA aquí y pide confirmación explícita mostrando: el PR ya fusionado en dev y qué se va a promover a producción.

5. **PR dev→main.** `gh pr create --base main --head dev --title "Promoción a producción: <resumen>"` con Summary/Validation referenciando el PR anterior; guarda el número, `gh pr merge <n> --merge`, y verifica estado `MERGED` igual que en el paso 3.

6. **Sync local y limpieza.** Requiere `git status --porcelain` limpio (si no lo está, detente y repórtalo). Después: `git checkout dev && git pull --ff-only origin dev`, `git checkout main && git pull --ff-only origin main`, y borra la rama local solo si `WORK_BRANCH` no es `main`/`dev`: `git branch -d "$WORK_BRANCH"` (la remota ya la borró el paso 3). Si `-d` se niega, NO uses `-D`: reporta la rama pendiente. Cierra reportando los últimos 2 commits de `main`.

7. **Reglas duras.** Nunca `push --force`. Nunca merge local a `main`/`dev`. Tras cada operación `gh`/`git`, verifica su resultado antes del paso siguiente; ante cualquier estado inesperado, detente y reporta en vez de improvisar.
