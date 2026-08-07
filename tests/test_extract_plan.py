"""Session log → plan → ledger (trace-to-graph without an LLM)."""
import json

import pytest

from models import InceptionHandoff, Run

OK, REFUSED, UNDETERMINED, USAGE = 0, 1, 2, 3

SESSION = """# Auth discovery

## Scope
Login and session cookies for the web app. No SSO yet.

## Hard constraints
- no new dependencies
- do NOT commit from implementer

## Acceptance criteria
- AC1: user can log in with email/password
- AC2: session survives browser refresh

## Open questions
- which session store?
- OAuth later?

## Units
- unit auth: login flow
  covers: AC1, AC2
- unit sessions: cookie hardening
  covers: AC2
  depends on: auth
"""


@pytest.fixture
def root(cli, tmp_path):
    log = tmp_path / "session.md"
    log.write_text(SESSION, encoding="utf-8")

    def run(*args):
        return cli("rungraph.py", "--dir", tmp_path, *args)

    run.tmp = tmp_path
    run.log = log
    return run


class TestExtract:
    def test_extract_writes_plan_with_criteria_and_units(self, root):
        out = root.tmp / "plan.json"
        proc = root("extract", "--file", root.log, "--out", out)
        assert proc.returncode == OK
        assert out.is_file()
        plan = json.loads(out.read_text(encoding="utf-8"))
        assert plan["kind"] == "lider.session.plan"
        assert plan["coverage"] in ("full", "partial")
        ids = {c["id"] for c in plan["criteria"]}
        assert "AC1" in ids and "AC2" in ids
        unit_ids = {u["id"] for u in plan["units"]}
        assert "auth" in unit_ids
        assert "scope" in plan["frame_markdown"].lower()
        assert "constraint" in plan["frame_markdown"].lower()

    def test_extract_structured_json_roundtrip(self, root):
        structured = {
            "kind": "lider.session.plan",
            "version": 1,
            "title": "prebuilt",
            "frame_markdown": "## Scope\nx\n\n## Hard constraints\ny\n",
            "criteria": [{"id": "AC1", "text": "works"}],
            "questions": [],
            "units": [{"id": "core", "title": "core", "covers": ["AC1"], "depends_on": []}],
        }
        src = root.tmp / "given.json"
        src.write_text(json.dumps(structured), encoding="utf-8")
        out = root.tmp / "out.plan.json"
        assert root("extract", "--file", src, "--out", out).returncode == OK
        plan = json.loads(out.read_text(encoding="utf-8"))
        assert plan["mode"] == "structured"
        assert plan["criteria"][0]["id"] == "AC1"
        assert plan["units"][0]["id"] == "core"

    def test_empty_log_is_undetermined(self, root):
        empty = root.tmp / "empty.md"
        empty.write_text("", encoding="utf-8")
        out = root.tmp / "empty.plan.json"
        proc = root("extract", "--file", empty, "--out", out)
        assert proc.returncode == UNDETERMINED
        plan = json.loads(out.read_text(encoding="utf-8"))
        assert plan["coverage"] == "undetermined"


class TestApplyPlan:
    def test_apply_seeds_inception_and_can_seal(self, root):
        plan_path = root.tmp / "p.json"
        assert root("extract", "--file", root.log, "--out", plan_path).returncode == OK
        proc = root("--run", "inc", "apply-plan", "--plan", plan_path,
                    "--init", "--enter-spec", "--title", "auth")
        assert proc.returncode == OK
        st = json.loads((root.tmp / ".lider" / "runs" / "inc" / "run.json")
                        .read_text(encoding="utf-8"))
        Run.model_validate(st)
        assert st["kind"] == "inception"
        assert st["node"] == "spec"
        assert st["spec"] is not None
        assert st["session_plan"]["path"].endswith("p.json")
        assert any(c["id"] == "AC1" for c in st["criteria"])
        assert any(u["id"] == "auth" for u in st["units"])
        # resolve open questions so seal can succeed
        for q in st["questions"]:
            if q["status"] == "open":
                assert root("--run", "inc", "question", "resolve",
                            "--id", q["id"], "--status", "assumed",
                            "--answer", "defer").returncode == OK
        seal = root("--run", "inc", "enter", "sealed")
        assert seal.returncode == OK
        handoff = root.tmp / ".lider" / "handoffs" / "inc.json"
        assert handoff.is_file()
        InceptionHandoff.model_validate(json.loads(handoff.read_text(encoding="utf-8")))

    def test_extract_apply_one_shot(self, root):
        proc = root("--run", "shot", "extract", "--file", root.log, "--apply",
                    "--init", "--enter-spec", "--title", "one-shot")
        assert proc.returncode == OK
        st = json.loads((root.tmp / ".lider" / "runs" / "shot" / "run.json")
                        .read_text(encoding="utf-8"))
        assert st["node"] == "spec"
        assert len(st["criteria"]) >= 2
        assert len(st["units"]) >= 1

    def test_apply_refuses_operations_kind(self, root):
        plan_path = root.tmp / "p.json"
        root("extract", "--file", root.log, "--out", plan_path)
        root("--run", "ops", "init", "--title", "x", "--kind", "operations")
        proc = root("--run", "ops", "apply-plan", "--plan", plan_path)
        assert proc.returncode == REFUSED
