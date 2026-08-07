# Closed spec — the startup watchdog must not kill a non-streaming engine

## Problem (measured, not theorised)

A real fan-out run killed Grok at 129 s with `exit 125`, `log_bytes: 0`, while the
stall watchdog was already **disarmed** for that adapter. The abort came from the
**startup** watchdog.

Root cause: `grok --output-format json` emits **one object at the end of the run**.
It writes nothing at all until it finishes. `Supervisor._once` decides an engine
"died at launch" when the log has not grown after `startup_s` (default 60 s):

```python
if not grew and elapsed >= startup_s:
    reason = "startup-failed"
```

For a batch-output engine that condition is not a health check — it is a guarantee
that any run longer than `startup_s` is killed. **Grok is unusable today.**

This is the same flaw the stall watchdog had before it was made command-aware: it
cannot tell "dead" from "working silently". The fix must follow the same doctrine
already established in this codebase — *"I cannot tell whether it died" is not
"it died"* — and disarm rather than guess.

## Scope

May be touched:

- `plugins/lider/scripts/lider/adapters/__init__.py` — the adapter contract
- `plugins/lider/scripts/lider/adapters/{grok,calvoproxy,generic,codex,claude}.py`
- `plugins/lider/scripts/lider/runtime.py` — `Supervisor.run` / `_once` / `Status`
- `plugins/lider/scripts/lider/adapters/README.md`, `README.md`, `ARCHITECTURE.md`
- version in `plugins/lider/.claude-plugin/plugin.json` **and**
  `.claude-plugin/marketplace.json` (they must stay in sync)

Must NOT be touched: `rungraph.py`, `fanout.py`, `reduce-findings.py`,
`verify-findings.py`, `metrics*.py`, any skill, any schema.

## Hard constraints

- No behaviour change for adapters that DO stream (`codex`, `claude`): their
  startup watchdog stays armed at the current default.
- The hard timeout remains the bound in every case. Nothing here may weaken it.
- Exit codes stay as they are: `124` timeout, `125` watchdog abort.
- Comments explain WHY in the existing style, and cite the measurement.
- ASCII-only in anything printed to a console (the Windows console mangles
  em-dashes).
- The implementer does NOT commit.

## Design (decided; the implementer does not re-decide these)

1. **New adapter property `streams`.** True when the engine emits output
   progressively, so an absence of output early is genuinely suspicious.
   Default in the base class: `streams = True` is WRONG — an unknown engine's
   behaviour is unknown. Default it to the adapter's own `has_inflight`, because
   an adapter that can parse a live stream is by definition an adapter whose
   engine produces one.

2. **`Supervisor.run` disarms the startup watchdog when `adapter.streams` is
   False**, exactly as it already disarms the stall watchdog when
   `has_inflight` is False: set `startup_s = 0`, announce it on our own channel
   (never into `<log>`), and record it.

3. **`_once` treats `startup_s == 0` as disarmed** — the `not grew` branch must
   not fire at all.

4. **`Status` gains `startup_watchdog`** alongside `stall_watchdog`, so a reader
   can tell an unwatched run from a watched one. Same reason as before: an
   unarmed watchdog must never be mistaken for a healthy run.

5. **Per adapter:**
   - `codex`, `claude` → `streams = True` (they have verified stream grammars)
   - `grok` → `streams = False` (one JSON object at the end — measured)
   - `calvoproxy` → `streams = False` (a single HTTP response)
   - `generic` → inherits False (unknown engine, conservative)

## Acceptance criteria

1. A non-streaming adapter that produces nothing for longer than the default
   startup window **completes normally** instead of aborting with `125`.
2. A streaming adapter that dies at launch **still** aborts with `125` inside the
   startup window — no regression in fast-fail.
3. A non-streaming adapter that genuinely hangs is still bounded by the hard
   timeout (`124`), with the process tree torn down and zero survivors.
4. `status.json` reports `startup_watchdog: 0` for a disarmed run and `1` for an
   armed one.
5. **Grok completes a real review through `agent-exec.py`** and returns
   schema-conformant findings.

## Mandatory verification (run these, do not report them unread)

```bash
python -m py_compile plugins/lider/scripts/lider/*.py plugins/lider/scripts/lider/adapters/*.py
```

- A fake non-streaming engine (silent > 90 s, then valid JSON) with
  `LIDER_STARTUP_S=60` through the `generic` adapter -> exit `0`.
- A fake engine that exits immediately with no output through a STREAMING
  adapter -> exit `125` within the startup window.
- A fake engine that never returns, non-streaming -> exit `124`, zero survivors.
- The existing regression set: happy path `0`, bad schema `3`, hang `124`,
  missing binary `127`, refused mode `2`.
- A real `agent-exec.py --engine grok` review that returns findings.

## Risk

Reversible. The change is additive (one property, one guard) and touches no
persisted format except an added status field, which readers may ignore.
The authorised risk: a dead non-streaming engine now runs to the hard timeout
instead of failing in 60 s. That is the intended trade and is stated in the docs.

---

## Scope change 1 (architect decision, mid-phase)

Discovered while running the phase, not predicted by it: making a non-streaming
engine survive is not enough to make it USABLE. Grok returns
`{"text": "<prose> ```json {...} ``` "}`, and `lider/extract.py:parse_maybe`
only recognises a fenced block that spans the ENTIRE string. The payload of a
chatty engine is therefore unreachable and the run fails with exit 3.

Added to scope: `plugins/lider/scripts/lider/extract.py`.

Design: `parse_maybe` gains two fallbacks, in order - (1) the whole string is a
fence, as today; (2) a fenced ```json block found ANYWHERE in the string;
(3) the last balanced JSON object embedded in the string. Ordered most-specific
first so a fenced answer always wins over an object the engine merely quoted
while reasoning.

Acceptance: the findings grok already produced (its log is on disk) extract and
validate against findings.schema.json, WITHOUT paying for a second run.
