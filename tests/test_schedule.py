"""Parallel schedule from unit deps — plan only, no engine execution."""
import json

import pytest

SPEC = "## Scope\nx\n## Hard constraints\ny\n## Mandatory verification\npytest -q\n"
OK, REFUSED = 0, 1


@pytest.fixture
def led(cli, tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")

    def run(*args):
        return cli("rungraph.py", "--dir", tmp_path, "--run", "s", *args)

    run.tmp = tmp_path
    run("init", "--title", "multi")
    run("spec", "--file", spec)
    run("enter", "spec")
    return run


class TestScheduleWaves:
    def test_independent_units_are_one_wave(self, led):
        led("unit", "add", "--id", "a", "--title", "one")
        led("unit", "add", "--id", "b", "--title", "two")
        led("enter", "plan")
        proc = led("schedule", "--format", "json")
        assert proc.returncode == OK
        plan = json.loads(proc.stdout)
        assert plan["wave_count"] == 1
        assert plan["width_now"] == 2
        ids = sorted(u["id"] for u in plan["waves"][0])
        assert ids == ["a", "b"]

    def test_deps_make_sequential_waves(self, led):
        led("unit", "add", "--id", "auth", "--title", "auth")
        led("unit", "add", "--id", "api", "--title", "api", "--depends-on", "auth")
        led("unit", "add", "--id", "ui", "--title", "ui", "--depends-on", "api")
        led("enter", "plan")
        plan = json.loads(led("schedule", "--format", "json").stdout)
        assert plan["wave_count"] == 3
        assert [u["id"] for u in plan["waves"][0]] == ["auth"]
        assert [u["id"] for u in plan["waves"][1]] == ["api"]
        assert [u["id"] for u in plan["waves"][2]] == ["ui"]
        assert plan["width_now"] == 1

    def test_diamond_deps_parallelize_middle(self, led):
        """auth first; api+web parallel; join last."""
        led("unit", "add", "--id", "auth")
        led("unit", "add", "--id", "api", "--depends-on", "auth")
        led("unit", "add", "--id", "web", "--depends-on", "auth")
        led("unit", "add", "--id", "ship", "--depends-on", "api,web")
        led("enter", "plan")
        plan = json.loads(led("schedule", "--format", "json").stdout)
        assert plan["wave_count"] == 3
        assert [u["id"] for u in plan["waves"][0]] == ["auth"]
        mid = sorted(u["id"] for u in plan["waves"][1])
        assert mid == ["api", "web"]
        assert [u["id"] for u in plan["waves"][2]] == ["ship"]
        assert plan["max_wave_width"] == 2

    def test_max_width_caps_a_wave(self, led):
        for i in range(4):
            led("unit", "add", "--id", "u%d" % i)
        led("enter", "plan")
        plan = json.loads(led("schedule", "--format", "json", "--max-width", "2").stdout)
        assert plan["wave_count"] == 2
        assert all(len(w) <= 2 for w in plan["waves"])
        assert plan["max_wave_width"] == 2

    def test_commands_format_mentions_worktrees_and_enter(self, led):
        led("unit", "add", "--id", "auth")
        led("unit", "add", "--id", "api", "--depends-on", "auth")
        led("enter", "plan")
        proc = led("schedule", "--format", "commands")
        assert proc.returncode == OK
        assert "worktree" in proc.stdout
        assert "enter implement --unit auth" in proc.stdout
        assert "enter implement --unit api" in proc.stdout
        assert "wave 0" in proc.stdout and "wave 1" in proc.stdout

    def test_flat_run_refuses_schedule(self, led):
        assert led("schedule").returncode == REFUSED

    def test_finished_unit_not_rescheduled(self, led):
        led("unit", "add", "--id", "auth")
        led("unit", "add", "--id", "api", "--depends-on", "auth")
        led("enter", "plan")
        # complete auth unit through its subgraph
        led("enter", "implement", "--unit", "auth")
        led("enter", "review", "--unit", "auth")
        led("enter", "adjudicate", "--unit", "auth")
        led("enter", "done", "--unit", "auth")
        plan = json.loads(led("schedule", "--format", "json").stdout)
        assert plan["finished"] == ["auth"]
        assert plan["wave_count"] == 1
        assert [u["id"] for u in plan["waves"][0]] == ["api"]
