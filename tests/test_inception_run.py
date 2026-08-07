"""Inception as a separate run: seal handoff, import into construction, strict mode.

Decisions locked with the user:
  - Handoff is OPERATIONAL: .lider/handoffs/<id>.json
  - Inception is RECOMMENDED (and said so), not required - unless STRICT
  - Challenge is optional with warning; STRICT requires it at seal
  - STRICT construction requires import before implement
"""
import json
import os

import pytest

from models import InceptionHandoff, Run

FRAME = "## Scope\nauth\n## Hard constraints\nno new deps\n"
BUILD = FRAME + "## Mandatory verification\npytest -q\n"
OK, REFUSED, UNDETERMINED, USAGE = 0, 1, 2, 3


@pytest.fixture
def root(cli, tmp_path):
    frame = tmp_path / "frame.md"
    frame.write_text(FRAME, encoding="utf-8")
    build = tmp_path / "build.md"
    build.write_text(BUILD, encoding="utf-8")

    def run(run_id, *args):
        return cli("rungraph.py", "--dir", tmp_path, "--run", run_id, *args)

    run.tmp = tmp_path
    run.frame = frame
    run.build = build
    return run


def state(root, rid):
    return json.loads((root.tmp / ".lider" / "runs" / rid / "run.json")
                      .read_text(encoding="utf-8"))


def handoff_file(root, rid):
    return root.tmp / ".lider" / "handoffs" / ("%s.json" % rid)


class TestInceptionSeal:
    def _ready_to_seal(self, root, rid="inc"):
        assert root(rid, "init", "--title", "auth", "--kind", "inception").returncode == OK
        assert root(rid, "spec", "--file", root.frame).returncode == OK
        assert root(rid, "enter", "spec").returncode == OK
        assert root(rid, "criterion", "add", "--id", "AC1",
                    "--text", "user can log in").returncode == OK
        assert root(rid, "unit", "add", "--id", "auth", "--covers", "AC1").returncode == OK

    def test_show_lists_inception_artifacts(self, root):
        assert root("show-a", "init", "--title", "auth",
                    "--kind", "inception").returncode == OK
        out = root("show-a", "show").stdout
        assert "artifacts:" in out
        assert "frame pinned" in out
        assert " --  frame pinned" in out

        self._ready_to_seal(root, rid="show-b")
        out = root("show-b", "show").stdout
        assert "ok   frame pinned" in out or "ok  frame pinned" in out
        assert "handoff sealed" in out

    def test_seal_writes_operational_handoff_under_lider(self, root):
        self._ready_to_seal(root)
        proc = root("inc", "enter", "sealed")
        assert proc.returncode == OK
        assert "WARNING" in (proc.stderr or "")  # no challenge
        path = handoff_file(root, "inc")
        assert path.is_file()
        doc = json.loads(path.read_text(encoding="utf-8"))
        InceptionHandoff.model_validate(doc)
        assert doc["kind"] == "lider.inception.handoff"
        st = state(root, "inc")
        Run.model_validate(st)
        assert st["node"] == "sealed"
        assert st["handoff_out"]["path"] == str(path)
        show = root("inc", "show").stdout
        assert "ok   handoff sealed" in show or "ok  handoff sealed" in show

    def test_seal_refuses_open_questions(self, root):
        self._ready_to_seal(root)
        root("inc", "question", "add", "--text", "which IdP?")
        assert root("inc", "enter", "sealed").returncode == UNDETERMINED

    def test_seal_refuses_uncovered_criteria(self, root):
        root("inc", "init", "--title", "x", "--kind", "inception")
        root("inc", "spec", "--file", root.frame)
        root("inc", "enter", "spec")
        root("inc", "criterion", "add", "--id", "AC1", "--text", "login")
        # no unit covering AC1
        assert root("inc", "enter", "sealed").returncode == REFUSED

    def test_non_strict_warns_without_challenge_but_seals(self, root):
        self._ready_to_seal(root)
        proc = root("inc", "enter", "sealed")
        assert proc.returncode == OK
        assert "WARNING" in proc.stderr
        assert "challenge" in proc.stderr.lower()

    def test_strict_refuses_seal_without_challenge(self, root):
        root("inc", "init", "--title", "x", "--kind", "inception", "--strict")
        root("inc", "spec", "--file", root.frame)
        root("inc", "enter", "spec")
        root("inc", "criterion", "add", "--id", "AC1", "--text", "login")
        root("inc", "unit", "add", "--id", "auth", "--covers", "AC1")
        proc = root("inc", "enter", "sealed")
        assert proc.returncode == REFUSED
        assert "STRICT" in proc.stderr

    def test_strict_seal_ok_after_challenge_node(self, root):
        root("inc", "init", "--title", "x", "--kind", "inception", "--strict")
        root("inc", "spec", "--file", root.frame)
        root("inc", "enter", "spec")
        root("inc", "criterion", "add", "--id", "AC1", "--text", "login")
        root("inc", "unit", "add", "--id", "auth", "--covers", "AC1")
        assert root("inc", "enter", "challenge").returncode == OK
        assert root("inc", "enter", "sealed").returncode == OK
        assert handoff_file(root, "inc").is_file()

    def test_tampered_handoff_fails_import(self, root):
        self._ready_to_seal(root)
        root("inc", "enter", "sealed")
        path = handoff_file(root, "inc")
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["criteria"][0]["text"] = "TAMPERED"
        path.write_text(json.dumps(doc), encoding="utf-8")
        root("c", "init", "--title", "build")
        assert root("c", "import", "--handoff", path).returncode == UNDETERMINED


class TestConstructionImport:
    def _sealed(self, root):
        root("inc", "init", "--title", "auth", "--kind", "inception")
        root("inc", "spec", "--file", root.frame)
        root("inc", "enter", "spec")
        root("inc", "criterion", "add", "--id", "AC1", "--text", "login")
        root("inc", "unit", "add", "--id", "auth", "--covers", "AC1")
        root("inc", "enter", "sealed")
        return handoff_file(root, "inc")

    def test_import_loads_criteria_and_units(self, root):
        path = self._sealed(root)
        assert root("c", "init", "--title", "build").returncode == OK
        assert root("c", "import", "--handoff", path).returncode == OK
        st = state(root, "c")
        assert st["handoff"]["sha256"]
        assert [c["id"] for c in st["criteria"]] == ["AC1"]
        assert st["units"][0]["id"] == "auth"
        assert st["units"][0]["node"] == "pending"
        Run.model_validate(st)

    def test_flat_path_still_works_without_import(self, root):
        """Recommended, not required - the whole point of non-strict."""
        assert root("c", "init", "--title", "quick").returncode == OK
        root("c", "spec", "--file", root.build)
        root("c", "enter", "spec")
        proc = root("c", "enter", "implement")
        assert proc.returncode == OK
        assert "RECOMMENDED" in proc.stderr or "handoff" in proc.stderr.lower()

    def test_strict_blocks_implement_without_import(self, root):
        root("c", "init", "--title", "build", "--strict")
        root("c", "spec", "--file", root.build)
        root("c", "enter", "spec")
        proc = root("c", "enter", "implement")
        assert proc.returncode == REFUSED
        assert "STRICT" in proc.stderr

    def test_strict_allows_implement_after_import(self, root):
        path = self._sealed(root)
        root("c", "init", "--title", "build", "--strict")
        root("c", "import", "--handoff", path)
        root("c", "spec", "--file", root.build)
        root("c", "enter", "spec")
        assert root("c", "enter", "implement").returncode == OK

    def test_init_messages_say_recommended(self, root):
        proc = root("c", "init", "--title", "t")
        assert proc.returncode == OK
        assert "RECOMMENDED" in proc.stdout


class TestInceptionDoesNotBuild:
    def test_cannot_enter_implement_on_inception_run(self, root):
        root("inc", "init", "--title", "x", "--kind", "inception")
        root("inc", "spec", "--file", root.frame)
        root("inc", "enter", "spec")
        proc = root("inc", "enter", "implement")
        assert proc.returncode == REFUSED
        assert "discovery only" in proc.stderr
