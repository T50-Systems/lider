"""Local CalvoProxy as a *chat-completion* engine (not an agent).

This adapter is deliberately thin: one HTTP completion through the proxy
(OpenRouter free/cheap profiles). It has **no tools and no filesystem** — that
is a property of *this adapter*, not of free models in general.

If you want a cheap model *with* tools (read/edit/bash), do not use
`--engine calvoproxy`. Use an agentic adapter and pin the model there, e.g.:

  agent-exec.py --engine opencode --model openrouter/<free-model> ...
  agent-implement.py --engine opencode --model openrouter/<free-model> ...

CalvoProxy stays the path for contrast/bulk text when you explicitly do not
want tools.
"""
import os

from . import Adapter, AdapterRefused
from ..extract import extract_to
from ..runtime import interpreter_for


class CalvoProxyAdapter(Adapter):
    id = "calvoproxy"
    has_inflight = False      # one request either returns or does not
    streams = False           # a single HTTP response: nothing arrives until it does
    native_schema = False     # weak free models: validate the result locally

    def locate(self):
        self.bin = os.environ.get("LIDER_CALVOPROXY_ASK") or os.path.join(
            os.environ.get("HOME", ""), ".claude", "skills", "invoke-calvoproxy", "ask.sh")
        return os.path.isfile(self.bin)

    def auth_hint(self):
        return "check that CalvoProxy is running on :8080 with a valid OpenRouter key"

    def classify_tail(self, tail):
        if "no/invalid response from calvoproxy" in tail or "connection refused" in tail:
            return "retry"
        return ""

    def extract(self, log_path, out_path):
        return extract_to(log_path, out_path) == 0

    def argv(self, mode, model, prompt, schema, out):
        if mode == "implement":
            raise AdapterRefused(
                "calvoproxy: this adapter is chat-only (no tools/filesystem). "
                "For a free/cheap model WITH tools use --engine opencode "
                "(or pi/claude/codex) and pin the model there.")
        if mode != "review":
            raise AdapterRefused("calvoproxy: unknown mode %r" % mode)
        # The model argument maps to the proxy PROFILE, not a model name.
        return interpreter_for(self.bin) + [self.bin, model or "coding", prompt]
