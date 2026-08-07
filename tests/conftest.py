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


@pytest.fixture
def cli(scripts_dir, tmp_path):
    """Run one of the plugin's CLI scripts and return the CompletedProcess."""
    def run(script, *args, **kwargs):
        return subprocess.run(
            [sys.executable, os.path.join(scripts_dir, script)] + [str(a) for a in args],
            capture_output=True, text=True, cwd=kwargs.get("cwd", str(tmp_path)))
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
