"""Pi coding agent CLI (@mariozechner/pi-coding-agent / pi.dev)."""
import os
import shutil

from . import Adapter, AdapterRefused
from ..extract import extract_to


class PiAdapter(Adapter):
    id = "pi"
    # Print/json modes emit events; no verified command-inflight grammar yet.
    has_inflight = False
    streams = True

    def locate(self):
        self.bin = shutil.which("pi")
        if not self.bin:
            for extra in (os.path.join(os.environ.get("APPDATA", ""), "npm"),
                          os.path.join(os.environ.get("HOME", ""), "AppData", "Roaming", "npm")):
                if extra:
                    candidate = shutil.which("pi", path=extra)
                    if candidate:
                        self.bin = candidate
                        break
        if self.bin and self.bin.lower().endswith(".ps1"):
            base = self.bin[:-4]
            for alt in (base + ".cmd", base + ".exe", base):
                if os.path.isfile(alt):
                    self.bin = alt
                    break
        return bool(self.bin)

    def auth_hint(self):
        return "run 'pi /login' (or set a provider API key, e.g. ANTHROPIC_API_KEY)"

    def classify_tail(self, tail):
        if any(s in tail for s in ("not authenticated", "/login", "invalid api key",
                                   "unauthorized", "api key")):
            return "auth"
        if any(s in tail for s in ("rate limit", "overloaded", "429", "503")):
            return "retry"
        return ""

    def extract(self, log_path, out_path):
        return extract_to(log_path, out_path) == 0

    def argv(self, mode, model, prompt, schema, out):
        # -p / --print: non-interactive. --mode json: event stream. --no-session:
        # do not persist into the user's session tree (isolation-ish).
        cmd = [self.bin, "-p", "--mode", "json", "--no-session"]
        if model:
            # Accept bare ids or provider/model; optional :thinking handled by pi.
            if "/" in model:
                cmd += ["--model", model]
            else:
                cmd += ["--model", model]
        if mode == "review":
            # Read-only built-ins only - no write/edit/bash.
            cmd += ["--tools", "read,grep,find,ls"]
        elif mode == "implement":
            # Default tool set (read/bash/edit/write). Writes are real.
            pass
        else:
            raise AdapterRefused("pi: unknown mode %r" % mode)
        cmd.append(prompt)
        return cmd
