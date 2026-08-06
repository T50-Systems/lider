"""Free OpenRouter models via the local CalvoProxy.

Not an agent: a single chat completion over HTTP. No tools, no filesystem, no
turns. It is here because the contract should hold at the cheap end of the range
too - offloading bulk or contrast work - and because an adapter that cannot
implement should say so rather than pretend.
"""
import os

from . import Adapter, AdapterRefused
from ..extract import extract_to
from ..runtime import interpreter_for


class CalvoProxyAdapter(Adapter):
    id = "calvoproxy"
    has_inflight = False      # one request either returns or does not
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
                "calvoproxy: implement mode is not supported - the proxy is a chat completion "
                "with no tools or filesystem access. Pick another engine.")
        if mode != "review":
            raise AdapterRefused("calvoproxy: unknown mode %r" % mode)
        # The model argument maps to the proxy PROFILE, not a model name.
        return interpreter_for(self.bin) + [self.bin, model or "coding", prompt]
