"""lider.adapters - everything engine-specific, one module per engine.

`lider.runtime` supervises a process and names no engine. What a particular CLI
contributes - how to launch it, how to read its stream, what its errors mean -
lives in exactly one file here. Adding an engine is writing one module and
nothing else.

Subclass `Adapter`, set `id`, define `argv`, and stop. Everything else has a
conservative default, so an adapter that defines only `argv` still runs.

THE RULE THAT SHAPES THE DEFAULTS: `has_inflight` stays False unless `inflight`
can genuinely tell a running command from an idle engine. When it is False the
runtime DISARMS the stall watchdog and the hard timeout becomes the only bound.
The run is slower to fail, and that is correct - "I cannot tell whether it is
stalled" is not "it is stalled". A grammar you have not checked against real
output is worse than none: it reports idle while a build runs, and the watchdog
kills healthy work.
"""
import importlib
import os
import shutil
import sys


# The engine used when nothing is specified. This is claude and not codex because
# codex requires a paid account that this install does not have: its binary is on
# PATH, so `locate()` succeeds, and every default invocation would burn a launch
# only to fail on "You've hit your usage limit". Point LIDER_ENGINE elsewhere, or
# change this line, if that stops being true.
DEFAULT_ENGINE = "claude"


class Adapter(object):
    id = "generic"
    has_inflight = False          # see the module docstring before setting this
    native_schema = False         # True when the engine enforces the schema itself

    # Does the engine emit output PROGRESSIVELY, or all at once when it finishes?
    #
    # MEASURED: `grok --output-format json` emits a single object at the very end
    # and writes nothing before it. The startup watchdog - which aborts a run whose
    # log has not grown after `startup_s` - then stops being a health check and
    # becomes a guarantee that any run longer than that window is killed. A real
    # review died at 129s with exit 125 and log_bytes 0.
    #
    # None means "infer from has_inflight": an adapter that can parse a live stream
    # is by definition an adapter whose engine produces one. Set it explicitly when
    # the two differ.
    streams = None

    def __init__(self):
        self.bin = None

    def streams_output(self):
        """Resolved answer to `streams`, with the has_inflight fallback applied."""
        return self.has_inflight if self.streams is None else bool(self.streams)

    # -- discovery ---------------------------------------------------------
    def locate(self):
        """Set self.bin. Return False when the engine is not installed."""
        self.bin = shutil.which(self.id)
        return bool(self.bin)

    def isolate(self, anchor_dir):
        """Keep the run out of the user's personal config for this engine."""

    def preflight(self, schema):
        """0 ok, 2 stop. Credentials, reachable services, required inputs."""
        return 0

    def auth_hint(self):
        return "re-authenticate the engine CLI"

    # -- stream reading ----------------------------------------------------
    def activity(self, tail):
        """A short 'what it is doing right now' line, from the cleaned log tail."""
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if line:
                return line[:140]
        return ""

    def inflight(self, chunk):
        """True (a command opened) / False (closed) / None (no transition here)."""
        return None

    def classify_tail(self, tail):
        """'auth' | 'retry' | 'fatal' | '' - engine vocabulary only.

        The generic HTTP/network vocabulary is already handled by the runtime.
        """
        return ""

    # -- results -----------------------------------------------------------
    def extract(self, log_path, out_path):
        """Produce out_path. The default assumes the engine wrote it itself."""
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0

    def usage(self, log_path):
        """What the run consumed, read from the engine's own report.

        Returns a dict with any of: cost_usd, input_tokens, output_tokens,
        cache_read_tokens, cache_write_tokens, model_billed, turns - or None when
        the engine reports nothing.

        A field the engine did not report must be ABSENT or None, never 0. An
        unknown cost and a zero cost are opposite facts, and conflating them makes
        an expensive engine look free in every average downstream.
        """
        return None

    def argv(self, mode, model, prompt, schema, out):
        """REQUIRED. mode is 'review' (read-only, structured) or 'implement'."""
        raise NotImplementedError("adapter '%s' does not define argv()" % self.id)


class AdapterRefused(Exception):
    """An adapter cannot serve this mode at all (not a failure - a refusal)."""


def available():
    here = os.path.dirname(os.path.abspath(__file__))
    return sorted(f[:-3] for f in os.listdir(here)
                  if f.endswith(".py") and not f.startswith("_"))


def load(engine_id):
    """Instantiate an adapter by id.

    An unknown id is NOT an error: it falls back to `generic`, which runs any CLI
    with the stall watchdog disarmed. Silently guessing an unknown engine's log
    grammar would be the worse outcome.
    """
    engine_id = (engine_id or "generic").strip()
    if not engine_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("invalid adapter id %r" % engine_id)
    try:
        module = importlib.import_module("%s.%s" % (__name__, engine_id))
    except ImportError:
        print("adapters: no adapter for '%s'; falling back to 'generic' "
              "(hard timeout only)." % engine_id, file=sys.stderr)
        module = importlib.import_module("%s.generic" % __name__)
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, Adapter) and value is not Adapter:
            return value()
    raise ImportError("adapter module '%s' defines no Adapter subclass" % engine_id)
