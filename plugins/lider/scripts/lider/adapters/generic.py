"""Any CLI - the floor of the contract, and the fallback for unknown engines.

Deliberately provides NO log grammar, so the stall watchdog stays disarmed and
the hard timeout is the only bound. That is the honest position: we cannot tell
this engine's "compiling for 8 minutes" from its "hung", and killing a healthy
build is the worse error.

Configure with environment variables:
  LIDER_BIN             command to run (required) - absolute path or on PATH
  LIDER_ARGS_REVIEW     extra args for review mode      (whitespace-split)
  LIDER_ARGS_IMPLEMENT  extra args for implement mode   (whitespace-split)
  LIDER_MODEL_FLAG      flag used to pin a model, e.g. --model
  LIDER_EXTRACT_JSON    1 to recover the result JSON from stdout (default 0)
"""
import os
import shlex
import shutil

from . import Adapter, AdapterRefused
from ..extract import extract_to
from ..runtime import interpreter_for


class GenericAdapter(Adapter):
    id = "generic"
    has_inflight = False

    def locate(self):
        target = os.environ.get("LIDER_BIN")
        if not target:
            return False
        self.bin = target if os.path.isfile(target) else shutil.which(target)
        return bool(self.bin)

    def auth_hint(self):
        return "authenticate '%s' however it expects" % (os.environ.get("LIDER_BIN") or "the engine")

    def extract(self, log_path, out_path):
        if os.environ.get("LIDER_EXTRACT_JSON") != "1":
            return os.path.exists(out_path) and os.path.getsize(out_path) > 0
        return extract_to(log_path, out_path) == 0

    def argv(self, mode, model, prompt, schema, out):
        # A shebang is not enough on Windows: subprocess needs the interpreter.
        cmd = interpreter_for(self.bin) + [self.bin]
        flag = os.environ.get("LIDER_MODEL_FLAG")
        if model and flag:
            cmd += [flag, model]
        if mode == "review":
            extra = os.environ.get("LIDER_ARGS_REVIEW", "")
        elif mode == "implement":
            extra = os.environ.get("LIDER_ARGS_IMPLEMENT", "")
        else:
            raise AdapterRefused("generic: unknown mode %r" % mode)
        if extra:
            cmd += shlex.split(extra)
        return cmd + [prompt]
