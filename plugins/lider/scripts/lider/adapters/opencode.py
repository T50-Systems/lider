"""OpenCode CLI (opencode.ai) - headless via `opencode run`."""
import json
import os
import shutil

from . import Adapter, AdapterRefused
from ..extract import extract_to


class OpenCodeAdapter(Adapter):
    id = "opencode"
    # Event stream under --format json is progressive when tools run; we have not
    # verified a stable in-flight grammar against a production transcript, so the
    # stall watchdog stays disarmed. Flip has_inflight only after a measured log.
    has_inflight = False
    streams = True            # --format json emits events during the run

    def locate(self):
        self.bin = shutil.which("opencode")
        if not self.bin:
            for extra in (os.path.join(os.environ.get("APPDATA", ""), "npm"),
                          os.path.join(os.environ.get("HOME", ""), "AppData", "Roaming", "npm"),
                          os.path.join(os.environ.get("LOCALAPPDATA", ""), "opencode")):
                if extra:
                    candidate = shutil.which("opencode", path=extra)
                    if candidate:
                        self.bin = candidate
                        break
        # Prefer .cmd / .exe over .ps1 for subprocess on Windows.
        if self.bin and self.bin.lower().endswith(".ps1"):
            base = self.bin[:-4]
            for alt in (base + ".cmd", base + ".exe", base):
                if os.path.isfile(alt):
                    self.bin = alt
                    break
        return bool(self.bin)

    def auth_hint(self):
        return "run 'opencode auth login' (or configure a provider in opencode)"

    def classify_tail(self, tail):
        if any(s in tail for s in ("not authenticated", "unauthorized", "auth login",
                                   "invalid api key", "no api key")):
            return "auth"
        if any(s in tail for s in ("rate limit", "overloaded", "429", "503")):
            return "retry"
        return ""

    def extract(self, log_path, out_path):
        return extract_to(log_path, out_path) == 0

    def argv(self, mode, model, prompt, schema, out):
        # Non-interactive: opencode run <message> --format json
        cmd = [self.bin, "run", prompt, "--format", "json"]
        if model:
            cmd += ["--model", model]
        if mode == "review":
            # Prefer permission config over hope. OPENCODE_PERMISSION is json;
            # deny edit/write-class tools when the engine honours it. --auto is
            # intentionally OFF for review so it cannot free-write.
            os.environ.setdefault(
                "OPENCODE_PERMISSION",
                json.dumps({"edit": "deny", "write": "deny", "bash": "ask"}))
        elif mode == "implement":
            # Full agentic write path. Orchestrator must have approved writes.
            cmd.append("--auto")
        else:
            raise AdapterRefused("opencode: unknown mode %r" % mode)
        return cmd
