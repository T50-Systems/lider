# Engine adapters

`lider/runtime.py` supervises a process and names no engine. Everything
CLI-specific lives in one module here, `<id>.py`, selected with `--engine <id>`
(or `LIDER_ENGINE`).

Adding an engine means writing one of these files. Nothing else changes.

## Contract

Subclass `Adapter`, set `id`, define `argv`, and stop. Everything else has a
conservative default.

| Member | Contract | Default |
|---|---|---|
| `id` | module name, and the value of `--engine` | required |
| `has_inflight` | `True` only if `inflight` can really distinguish a running command from an idle engine | `False` |
| `streams` | `True` if the engine emits output **progressively**. `None` infers it from `has_inflight` | `None` |
| `native_schema` | `True` if the engine enforces the JSON schema itself | `False` (validate locally) |
| `locate()` | set `self.bin`; return `False` if the engine is absent | look up `id` on `PATH` |
| `isolate(anchor_dir)` | keep the run out of the user's personal config for this engine | no-op |
| `preflight(schema)` | `0` ok / `2` stop — credentials, reachable services | `0` |
| `activity(tail)` | cleaned log tail → one short "what it is doing now" line | last non-empty line |
| `inflight(chunk)` | newly appended complete lines → `True` opened / `False` closed / `None` no change | `None` |
| `classify_tail(tail)` | this attempt's lowercased error tail → `auth` / `retry` / `fatal` / `""` | `""` |
| `auth_hint()` | what the operator must do to fix auth | generic |
| `extract(log, out)` | produce `out` for engines that print instead of writing a file | assume `out` exists |
| `argv(mode, model, prompt, schema, out)` | **required** — the command line. `mode` is `review` (read-only, structured) or `implement` (writes) | raises |

Raise `AdapterRefused` when a mode is impossible for the engine (see
`calvoproxy`, which has no filesystem). The wrapper reports that as `exit 2`
rather than running something that would quietly do nothing.

## Two watchdogs, two disarms

`has_inflight` and `streams` answer different questions, and each disarms a different watchdog:

| Adapter says | Runtime disarms | Because |
|---|---|---|
| `has_inflight = False` | the **stall** watchdog | mid-run silence cannot be told from a long command |
| `streams = False` | the **startup** watchdog | early silence cannot be told from a dead launch |

**MEASURED:** `grok --output-format json` emits one object at the very end and writes nothing
before it. With the startup watchdog armed, that is not a health check — it is a guarantee that
any Grok run longer than the startup window is killed. A real review died at 129 s with
`exit 125` and an empty log.

`status.json` publishes both as `stall_watchdog` and `startup_watchdog`, so a reader never
mistakes an unwatched run for a watched one.

**Known limit** (raised by a cross-family review of this very change): `streams = None` infers
from `has_inflight`, so an engine that *does* stream but never got an in-flight grammar written
also loses its startup watchdog. Declare `streams = True` explicitly on such an adapter.

## The rule that shapes the defaults

**An adapter that cannot report in-flight state disarms the stall watchdog.**

The watchdog kills a hung engine quickly by noticing the log stopped growing —
but a healthy engine running an 8-minute test suite also stops writing. The only
thing separating the two is knowing whether a command is currently open, which is
what `inflight` reports.

So an adapter with no grammar leaves `has_inflight = False`, the runtime sets
`stall_s = 0`, and the hard timeout becomes the only bound. The run is slower to
fail, and that is the correct trade: *"I cannot tell whether it is stalled"* is
not *"it is stalled"* — the same three-outcome rule `preflight` and `verify` apply
to evidence, applied to our own supervision. `status.json` carries
`"stall_watchdog": 0` so a reader never mistakes an unwatched run for a watched
one.

**Do not set `has_inflight = True` against a grammar you have not checked against
real output.** A wrong grammar is worse than none: it reports idle while a build
runs, and the watchdog kills healthy work. Both shipped grammars were verified
against captured transcripts.

## Shipped adapters

| id | Kind | In-flight | Implement | Notes |
|---|---|---|---|---|
| `codex` | agentic CLI | ✅ text grammar | ✅ full access | isolated `CODEX_HOME`; native `--output-schema` |
| `claude` | agentic CLI | ✅ stream-json | ✅ | native `--json-schema` (inline, not a path); `--bare` only when `ANTHROPIC_API_KEY` is set |
| `grok` | agentic CLI | ❌ (final JSON only) | ✅ `--yolo` | **does not stream** — startup watchdog disarmed; review locks down with permission **rules**, its tool denylist fails open |
| `opencode` | agentic CLI | ❌ | ✅ `--auto` | `opencode run --format json`; review sets `OPENCODE_PERMISSION` deny write/edit |
| `pi` | agentic CLI | ❌ | ✅ | `pi -p --mode json --no-session`; review uses `--tools read,grep,find,ls` only |
| `calvoproxy` | HTTP chat (CalvoProxy) | ❌ | ⛔ refused | One OpenRouter completion via local proxy — **no tools**. Not “free models can’t use tools”: for tools + a cheap model use `--engine opencode` (or another agentic adapter) with that model pinned |
| `generic` | any CLI | ❌ | ✅ | `LIDER_BIN` / `LIDER_ARGS_*`; the fallback for unknown ids |

## Measured traps worth not re-learning

Each of these cost a debugging cycle and is now encoded in the adapter it belongs to.

- **`claude --bare` breaks OAuth.** It restricts auth to `ANTHROPIC_API_KEY` and
  never reads the keychain, so under a normal login it exits 1 with
  *"Not logged in"*. Isolation is conditional on a key being present.
- **`claude --json-schema` takes the schema inline**, not a path. A filename is
  parsed as JSON and fails with `Unexpected identifier "C"`.
- **`grok --disallowed-tools` fails open.** An adversarial prompt with every write
  tool denylisted still overwrote its target. Only `--deny` rules hold.
  `--read-only` does not exist.
- **`subprocess` cannot exec a shebang script on Windows**, and plain `"bash"`
  resolves to the WSL shim in `System32`, which cannot see Windows paths. Use
  `runtime.interpreter_for()` for any script-based engine.
- **Grok wraps its answer in prose**: `{"text": "<prose> ```json {...} ``` "}`. The
  extractor now finds a fenced block anywhere inside a string, not only one that
  spans the whole of it.

## Writing a new one

Start from `generic.py`, define `argv`, and stop. Add `locate` if the binary is
not simply the id on `PATH`. Add a grammar only once you have a real transcript
in front of you to write it against.
