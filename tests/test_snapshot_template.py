"""G2 templates + G4 snapshot (structure vs content)."""
import json

import pytest

OK, USAGE = 0, 3

SPEC = "## Scope\nx\n## Hard constraints\ny\n## Mandatory verification\nnpm test\n"


@pytest.fixture
def led(cli, tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")

    def run(*args):
        return cli("rungraph.py", "--dir", tmp_path, "--run", "t", *args)

    run("init", "--title", "snap")
    run("spec", "--file", spec)
    run.tmp = tmp_path
    return run


class TestTemplate:
    def test_list_roles(self, cli, tmp_path):
        proc = cli("rungraph.py", "--dir", tmp_path, "template", "--list")
        assert proc.returncode == OK
        assert "implementer" in proc.stdout
        assert "templates" in proc.stdout.lower() or "roles" in proc.stdout

    def test_print_implementer_body(self, cli, tmp_path):
        proc = cli("rungraph.py", "--dir", tmp_path, "template", "--role", "implementer")
        assert proc.returncode == OK
        assert "Do NOT commit" in proc.stdout or "do NOT commit" in proc.stdout

    def test_path_only(self, cli, tmp_path):
        proc = cli("rungraph.py", "--dir", tmp_path, "template",
                   "--role", "reviewer", "--path")
        assert proc.returncode == OK
        assert proc.stdout.strip().endswith("reviewer.md")


class TestSnapshot:
    def test_json_separates_structure_and_content(self, led):
        led("enter", "spec")
        led("assign", "--role", "implementer", "--engine", "claude", "--model", "sonnet")
        proc = led("snapshot", "--json")
        assert proc.returncode == OK
        snap = json.loads(proc.stdout)
        assert snap["kind"] == "lider.run.snapshot"
        assert "structure" in snap and "content" in snap
        assert snap["structure"]["node"] == "spec"
        assert "legal_next" in snap["structure"]
        assert "edges" in snap["structure"]
        assert snap["content"]["spec_sha256"]
        assert "implementer" in snap["content"]["roles"]
        assert snap["content"]["roles"]["implementer"]["family"] == "anthropic"

    def test_out_writes_file(self, led):
        out = led.tmp / "snap.json"
        proc = led("snapshot", "--out", out)
        assert proc.returncode == OK
        assert out.is_file()
        snap = json.loads(out.read_text(encoding="utf-8"))
        assert snap["run_id"] == "t"
        assert "artifacts" in snap
