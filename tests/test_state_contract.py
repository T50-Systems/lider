"""The state the code actually writes, checked against the models in models.py.

The runtime writes plain dicts and validates nothing - it has to run with nothing
installed but Python. These tests are the other half of that bargain: they drive
real flows and then hold the produced files to a strict schema, so a field added,
renamed or retyped without updating the contract fails here rather than in
someone's resumed session a week later.

`extra="forbid"` throughout is the whole mechanism. A test that only checked the
fields it knew about would pass forever while the shape drifted underneath it.
"""
import json
import sys

import pytest

from models import MetricRow, Run, Status
from conftest import findings_doc, write_json

SPEC = "## Scope\nx\n## Hard constraints\ny\n## Mandatory verification\nnpm test\n"
FINDINGS_SCHEMA = __import__("conftest").FINDINGS_SCHEMA


def state_file(tmp_path, run="r"):
    return tmp_path / ".lider" / "runs" / run / "run.json"


def load_run(tmp_path, run="r"):
    """Parse the ledger through the strict model - the assertion IS the parse."""
    return Run.model_validate_json(state_file(tmp_path, run).read_text(encoding="utf-8"))


@pytest.fixture
def led(cli, tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")

    def run(*args):
        return cli("rungraph.py", "--dir", tmp_path, "--run", "r", *args)

    run("init", "--title", "contract phase", "--max-rounds", "9")
    run("spec", "--file", spec)
    run.tmp = tmp_path
    run.spec = spec
    return run


class TestTheLedgerMatchesItsContract:
    def test_a_fresh_run(self, led, tmp_path):
        model = load_run(tmp_path)
        assert model.schema_version == 1
        assert model.node == "init"
        assert model.spec is not None and model.spec.text == SPEC

    def test_a_run_with_every_kind_of_state_on_it(self, led, tmp_path):
        """One flow that touches every list and dict the model declares."""
        led("enter", "spec")
        led("assign", "--role", "architect", "--engine", "claude", "--model", "fable")
        led("assign", "--role", "implementer", "--engine", "claude", "--model", "opus")
        led("assign", "--role", "reviewer", "--engine", "grok")
        led("check", "--name", "lock", "--verdict", "ok", "--evidence", "held by me")
        led("criterion", "add", "--id", "AC1", "--text", "login works")
        led("criterion", "add", "--id", "AC2", "--text", "logout works")
        led("criterion", "defer", "--id", "AC2", "--reason", "next phase")
        led("question", "add", "--text", "which provider?")
        led("question", "resolve", "--id", "q1", "--status", "assumed",
            "--answer", "clerk, per the existing integration")
        led("unit", "add", "--id", "auth", "--title", "login", "--covers", "AC1")
        led("unit", "add", "--id", "api", "--depends-on", "auth", "--covers", "AC1")
        led("enter", "plan")
        led("enter", "implement", "--unit", "auth")
        led("enter", "review", "--unit", "auth")
        led("findings", "--unit", "auth", "--file", write_json(
            tmp_path / "u.json", findings_doc(
                ("BLOCKER", "token is never verified", "auth.ts:9"),
                ("NIT", "naming", "auth.ts:1"))))
        led("enter", "adjudicate", "--unit", "auth")
        led("adjudicate", "--unit", "auth", "--finding", "r1-1", "--decision", "fix",
            "--rationale", "real, and small")
        led("adjudicate", "--unit", "auth", "--finding", "r1-2", "--decision", "accept")
        led("enter", "done", "--unit", "auth")

        model = load_run(tmp_path)
        assert {u.id for u in model.units} == {"auth", "api"}
        auth = next(u for u in model.units if u.id == "auth")
        assert auth.node == "done" and auth.covers == ["AC1"]
        assert len(auth.findings) == 2 and len(auth.rounds) == 1
        assert auth.findings[0].defect_id                      # identity is always set
        assert {c.id: c.status for c in model.criteria} == {"AC1": "required",
                                                            "AC2": "deferred"}
        assert model.questions[0].status == "assumed" and model.questions[0].answer
        assert model.roles["reviewer"].family == "xai"
        assert model.checks["lock"].verdict == "ok"

    def test_a_run_that_looped_and_escalated(self, led, tmp_path):
        led("enter", "spec")
        led("assign", "--role", "implementer", "--engine", "claude")
        led("assign", "--role", "reviewer", "--engine", "grok")
        led("enter", "implement")
        for i in (1, 2):
            led("enter", "review")
            led("findings", "--file", write_json(tmp_path / ("r%d.json" % i), findings_doc(
                ("BLOCKER", "race on the shared cache", "s.ts:12"))))
            led("enter", "adjudicate")
            led("adjudicate", "--finding", "r%d-1" % i, "--decision", "return")
            led("enter", "implement", "--force")
        model = load_run(tmp_path)
        assert len(model.rounds) == 2
        assert model.rounds[1].recurring == 1
        assert model.rounds[1].severe_defects == model.rounds[0].severe_defects
        assert model.findings[1].recurrence_of == model.findings[0].id

    def test_a_forced_transition_is_recorded_in_the_events(self, led, tmp_path):
        led("enter", "spec")
        led.spec.write_text(SPEC + "\ndrifted\n", encoding="utf-8")
        led("enter", "implement", "--force")
        model = load_run(tmp_path)
        assert any(e.kind == "enter" and getattr(e, "forced", False) for e in model.events)

    def test_the_flat_path_produces_the_same_shape_as_the_unit_path(self, led, tmp_path):
        """Backwards compatibility is a shape claim, so it is checked as one."""
        led("enter", "spec")
        led("assign", "--role", "implementer", "--engine", "claude")
        led("assign", "--role", "reviewer", "--engine", "grok")
        led("enter", "implement")
        led("enter", "review")
        led("findings", "--file", write_json(tmp_path / "f.json", findings_doc(
            ("MINOR", "naming", "a.ts:1"))))
        model = load_run(tmp_path)
        assert model.units == [] and len(model.findings) == 1

    def test_a_ledger_written_by_an_older_version_still_parses(self, led, tmp_path):
        """Events are open on purpose: adding a field to one command's event must
        not invalidate a run recorded before that field existed."""
        raw = json.loads(state_file(tmp_path).read_text(encoding="utf-8"))
        raw["events"].append({"kind": "something-new", "at": 1, "node": "init",
                              "a_field_from_the_future": True})
        state_file(tmp_path).write_text(json.dumps(raw), encoding="utf-8")
        assert load_run(tmp_path).events[-1].kind == "something-new"


class TestTheContractIsStrictInBothDirections:
    def test_an_unknown_field_is_rejected(self, led, tmp_path):
        """The mechanism: if the code starts writing a key nobody declared, this
        test fails rather than the key going unnoticed for a release."""
        raw = json.loads(state_file(tmp_path).read_text(encoding="utf-8"))
        raw["a_key_nobody_declared"] = 1
        with pytest.raises(Exception):
            Run.model_validate(raw)

    def test_a_retyped_field_is_rejected(self, led, tmp_path):
        raw = json.loads(state_file(tmp_path).read_text(encoding="utf-8"))
        raw["max_rounds"] = "nine"
        with pytest.raises(Exception):
            Run.model_validate(raw)

    def test_an_invalid_verdict_is_rejected(self, led, tmp_path):
        led("check", "--name", "x", "--verdict", "ok", "--evidence", "e")
        raw = json.loads(state_file(tmp_path).read_text(encoding="utf-8"))
        raw["checks"]["x"]["verdict"] = "probably fine"
        with pytest.raises(Exception):
            Run.model_validate(raw)

    def test_an_invalid_unit_node_is_rejected(self, led, tmp_path):
        led("unit", "add", "--id", "auth")
        raw = json.loads(state_file(tmp_path).read_text(encoding="utf-8"))
        raw["units"][0]["node"] = "somewhere-else"
        with pytest.raises(Exception):
            Run.model_validate(raw)


class TestTheSupervisorStatusMatchesItsContract:
    """status.json crosses a process boundary: a watcher reads it live, so a
    silent change here breaks something that is not in this repository."""

    def _run(self, cli, tmp_path, monkeypatch, body, timeout=30):
        script = tmp_path / "engine.py"
        script.write_text("import sys, time\n" + body, encoding="utf-8")
        monkeypatch.setenv("LIDER_ENGINE", "generic")
        monkeypatch.setenv("LIDER_BIN", sys.executable)
        monkeypatch.setenv("LIDER_ARGS_REVIEW", str(script))
        monkeypatch.setenv("LIDER_EXTRACT_JSON", "1")
        monkeypatch.setenv("LIDER_RETRIES", "0")
        monkeypatch.setenv("LIDER_SCHEMA", FINDINGS_SCHEMA)
        monkeypatch.setenv("LIDER_METRICS_DIR", str(tmp_path))
        log = tmp_path / "run.log"
        cli("agent-exec.py", timeout, tmp_path / "out.json", log, "review")
        return Status.model_validate_json(
            (log.parent / (log.name + ".status.json")).read_text(encoding="utf-8"))

    def test_a_successful_run(self, cli, tmp_path, monkeypatch):
        status = self._run(cli, tmp_path, monkeypatch,
                           "print('{\"engine\":\"f\",\"verdict\":\"approve\",\"findings\":[]}')\n")
        assert status.state == "done" and status.exit == 0
        assert status.stall_watchdog == 0 and status.startup_watchdog == 0

    def test_a_timed_out_run(self, cli, tmp_path, monkeypatch):
        status = self._run(cli, tmp_path, monkeypatch, "time.sleep(120)\n", timeout=5)
        assert status.state == "failed" and status.exit == 124

    def test_an_unknown_state_would_be_caught(self, cli, tmp_path, monkeypatch):
        status = self._run(cli, tmp_path, monkeypatch, "print('{}')\n")
        raw = status.model_dump()
        raw["state"] = "vibing"
        with pytest.raises(Exception):
            Status.model_validate(raw)


class TestTheMetricsRowsMatchTheirContract:
    def test_every_row_any_tool_writes_parses(self, cli, tmp_path, monkeypatch):
        from lider import metrics
        metrics.record(tmp_path, "run", engine="claude", exit=0, cost_usd=1.0)
        metrics.record(tmp_path, "lens", lens="correctness", unique=2)
        metrics.record(tmp_path, "round", coverage="complete")
        metrics.record(tmp_path, "eligibility", width=2, units=3)
        rows = [MetricRow.model_validate(r) for r in metrics.read(tmp_path)]
        assert {r.kind for r in rows} == {"run", "lens", "round", "eligibility"}
        assert all(r.v == 1 for r in rows)

    def test_a_row_missing_the_version_is_rejected(self):
        with pytest.raises(Exception):
            MetricRow.model_validate({"kind": "run", "at": 1})

    def test_a_new_field_on_a_row_is_accepted(self):
        """Open by design: several tools append here and must be able to record
        something the reader does not know about yet."""
        row = MetricRow.model_validate({"v": 1, "kind": "run", "at": 1, "brand_new": 5})
        assert row.kind == "run"
