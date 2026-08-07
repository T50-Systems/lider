"""Shared fixtures.

Two rules this suite holds itself to:

* **No engine is ever called.** Every test drives a fake engine that is a small
  Python script, launched with `sys.executable`. That keeps the suite free, fast,
  deterministic, and clear of the shebang/WSL-bash trap on Windows that bit the
  real code twice.

* **Every test encodes a defect that actually happened**, or a rule the plugin
  refuses to break. A test here should be readable as "this is what went wrong
  once, and here is the shape it must never take again".
"""
import importlib.util
import io
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "plugins", "lider", "scripts")
SCHEMAS = os.path.join(REPO, "plugins", "lider", "schemas")
FINDINGS_SCHEMA = os.path.join(SCHEMAS, "findings.schema.json")

sys.path.insert(0, SCRIPTS)


@pytest.fixture(scope="session")
def scripts_dir():
    return SCRIPTS


@pytest.fixture(scope="session")
def findings_schema():
    return FINDINGS_SCHEMA


@pytest.fixture
def fake_engine(tmp_path):
    """Build a fake engine and return the env that points the generic adapter at it.

    The engine is a Python script run through `sys.executable`, so the adapter
    never has to exec a shebang - the exact thing `subprocess` cannot do on
    Windows.
    """
    def build(body, extract_json=True):
        script = tmp_path / "engine.py"
        script.write_text("import sys, time\n" + body, encoding="utf-8")
        env = dict(os.environ)
        env.update(
            LIDER_BIN=sys.executable,
            LIDER_ARGS_REVIEW=str(script),
            LIDER_ARGS_IMPLEMENT=str(script),
            LIDER_RETRIES="0",
            LIDER_METRICS_DIR=str(tmp_path),
        )
        if extract_json:
            env["LIDER_EXTRACT_JSON"] = "1"
        else:
            env.pop("LIDER_EXTRACT_JSON", None)
        return env
    return build


@pytest.fixture
def run_exec(tmp_path, scripts_dir):
    """Invoke agent-exec.py against the generic adapter; return (rc, out_path, log_path)."""
    def run(env, timeout_s=30, prompt="review", schema=None):
        out = tmp_path / "out.json"
        log = tmp_path / "run.log"
        if schema:
            env = dict(env, LIDER_SCHEMA=str(schema))
        proc = subprocess.run(
            [sys.executable, os.path.join(scripts_dir, "agent-exec.py"),
             "--engine", "generic", str(timeout_s), str(out), str(log), prompt],
            capture_output=True, text=True, env=env, cwd=str(tmp_path))
        return proc.returncode, out, log, proc
    return run


def _load_script(scripts_dir, script):
    """Import a CLI script by path, because most of them have a dash in the name."""
    name = "lidercli_" + script.replace("-", "_").replace(".py", "")
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(scripts_dir, script))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Result(object):
    """The CompletedProcess shape the tests already read, from an in-process call."""

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def cli(scripts_dir, tmp_path, monkeypatch):
    """Invoke a CLI script's `main()` IN PROCESS and return a CompletedProcess-alike.

    These used to be real subprocesses. Two reasons they are not any more, and the
    second is the one that mattered:

    * **Speed.** A subprocess per assertion made the ledger tests the slowest part
      of the suite for no extra signal - `main()` IS the boundary; only the
      `sys.exit()` wrapper around it is outside.
    * **Measurability.** Coverage cannot see into a subprocess without a
      sitecustomize hook that proved unreliable here, so the whole CLI surface -
      the majority of this codebase - reported as 0% while being thoroughly
      exercised. A number that wrong is worse than no number.

    A handful of tests still spawn real processes on purpose (the supervisor and
    fan-out suites), because there the process boundary IS the thing under test.
    """
    def run(script, *args, **kwargs):
        module = _load_script(scripts_dir, script)
        argv = [script] + [str(a) for a in args]
        out, err = io.StringIO(), io.StringIO()
        cwd = kwargs.get("cwd", str(tmp_path))
        with monkeypatch.context() as patch:
            patch.setattr(sys, "argv", argv)
            patch.setattr(sys, "stdout", out)
            patch.setattr(sys, "stderr", err)
            patch.chdir(cwd)
            try:
                code = module.main()
            except SystemExit as exc:          # argparse errors exit rather than return
                code = exc.code if isinstance(exc.code, int) else 2
        return Result(code if code is not None else 0, out.getvalue(), err.getvalue())
    return run


def write_json(path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def findings_doc(*items, **kw):
    """A findings report in the schema's shape."""
    return {
        "engine": kw.get("engine", "fake"),
        "verdict": kw.get("verdict", "request_changes"),
        "findings": [
            {"severity": sev, "summary": summary, "location": loc, "suggestion": None}
            for sev, summary, loc in items
        ],
    }
