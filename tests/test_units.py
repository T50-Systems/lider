"""Units of work: subgraphs, dependency edges, and the join barrier.

A spec is required to "split the feature into implementable units". Until units
existed that sentence had no representation: a phase with three units was one
flat run, and the ledger could not say which unit was stuck.

A unit carries the same shape as the run - findings, rounds, max_rounds - so
every convergence rule applies to it unchanged. These tests hold that equivalence
and the two things it adds: dependencies that are enforced, and a barrier that
will not open early.
"""
import json

import pytest

from conftest import findings_doc, write_json

SPEC = "## Scope\nx\n## Hard constraints\ny\n## Mandatory verification\nnpm test\n"
OK, REFUSED, UNDETERMINED, USAGE = 0, 1, 2, 3


@pytest.fixture
def run(cli, tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")

    def go(*args):
        return cli("rungraph.py", "--dir", tmp_path, "--run", "p", *args)

    go("init", "--title", "phase", "--max-rounds", "9")
    go("spec", "--file", spec)
    go("enter", "spec")
    go("assign", "--role", "implementer", "--engine", "claude")
    go("assign", "--role", "reviewer", "--engine", "grok")
    go.tmp = tmp_path
    return go


def state_of(run):
    return json.loads((run.tmp / ".lider" / "runs" / "p" / "run.json")
                      .read_text(encoding="utf-8"))


def finish(run, unit, tmp_path, findings=()):
    """Walk a unit all the way to done."""
    run("enter", "implement", "--unit", unit)
    run("enter", "review", "--unit", unit)
    if findings:
        run("findings", "--unit", unit,
            "--file", write_json(tmp_path / ("%s.json" % unit), findings_doc(*findings)))
    run("enter", "adjudicate", "--unit", unit)
    return run("enter", "done", "--unit", unit)


class TestDeclaring:
    def test_units_are_declared_and_listed(self, run):
        assert run("unit", "add", "--id", "auth", "--title", "login").returncode == OK
        assert run("unit", "add", "--id", "api", "--title", "endpoints").returncode == OK
        out = run("unit", "list").stdout
        assert "auth" in out and "api" in out and "pending" in out

    def test_a_run_with_no_units_says_so(self, run):
        assert "single flat unit" in run("unit", "list").stdout

    def test_a_duplicate_id_is_refused(self, run):
        run("unit", "add", "--id", "auth")
        assert run("unit", "add", "--id", "auth").returncode == REFUSED

    def test_depending_on_an_undeclared_unit_is_refused(self, run):
        """An unsatisfiable barrier that nobody notices until the end."""
        proc = run("unit", "add", "--id", "api", "--depends-on", "auth")
        assert proc.returncode == REFUSED
        assert "undeclared" in proc.stderr

    def test_a_unit_cannot_depend_on_itself(self, run):
        proc = run("unit", "add", "--id", "auth", "--depends-on", "auth")
        assert proc.returncode == REFUSED


class TestTheSubgraph:
    def test_a_unit_walks_its_own_cycle(self, run, tmp_path):
        run("unit", "add", "--id", "auth")
        run("enter", "plan")
        assert finish(run, "auth", tmp_path).returncode == OK
        assert [u["node"] for u in state_of(run)["units"]] == ["done"]

    def test_an_illegal_unit_edge_is_refused(self, run):
        run("unit", "add", "--id", "auth")
        proc = run("enter", "done", "--unit", "auth")
        assert proc.returncode == REFUSED
        assert "[auth]" in proc.stderr and "not an edge" in proc.stderr

    def test_an_unknown_unit_is_a_usage_error(self, run):
        proc = run("enter", "implement", "--unit", "nope")
        assert proc.returncode == USAGE
        assert "no unit 'nope'" in proc.stderr

    def test_a_unit_cannot_finish_with_undecided_severe_findings(self, run, tmp_path):
        run("unit", "add", "--id", "auth")
        run("enter", "implement", "--unit", "auth")
        run("enter", "review", "--unit", "auth")
        run("findings", "--unit", "auth",
            "--file", write_json(tmp_path / "f.json", findings_doc(
                ("BLOCKER", "token is never checked", "auth.ts:9"))))
        run("enter", "adjudicate", "--unit", "auth")
        proc = run("enter", "done", "--unit", "auth")
        assert proc.returncode == REFUSED
        assert "undecided" in proc.stderr

    def test_the_convergence_rules_apply_per_unit(self, run, tmp_path):
        """The same defect surviving three rounds, inside one unit."""
        run("unit", "add", "--id", "auth")
        run("enter", "implement", "--unit", "auth")
        for i in (1, 2, 3):
            run("enter", "review", "--unit", "auth")
            run("findings", "--unit", "auth", "--file", write_json(
                tmp_path / ("r%d.json" % i),
                findings_doc(("BLOCKER", "race on the session cache", "s.ts:12"))))
            run("enter", "adjudicate", "--unit", "auth")
            run("adjudicate", "--unit", "auth", "--finding", "r%d-1" % i,
                "--decision", "return")
            proc = run("enter", "implement", "--unit", "auth")
        assert proc.returncode == REFUSED
        assert "[auth]" in proc.stderr and "survived 3 rounds" in proc.stderr

    def test_a_units_findings_do_not_leak_into_the_run(self, run, tmp_path):
        run("unit", "add", "--id", "auth")
        run("enter", "implement", "--unit", "auth")
        run("enter", "review", "--unit", "auth")
        run("findings", "--unit", "auth", "--file", write_json(
            tmp_path / "f.json", findings_doc(("MINOR", "naming", "a.ts:1"))))
        state = state_of(run)
        assert state["findings"] == []
        assert len(state["units"][0]["findings"]) == 1


class TestDependencies:
    def test_a_unit_may_not_start_before_what_it_depends_on(self, run):
        """Otherwise the dependency is a comment and the work lands in any order."""
        run("unit", "add", "--id", "auth")
        run("unit", "add", "--id", "api", "--depends-on", "auth")
        proc = run("enter", "implement", "--unit", "api")
        assert proc.returncode == REFUSED
        assert "depends on unfinished unit(s): auth" in proc.stderr

    def test_finishing_the_dependency_unblocks_it(self, run, tmp_path):
        run("unit", "add", "--id", "auth")
        run("unit", "add", "--id", "api", "--depends-on", "auth")
        finish(run, "auth", tmp_path)
        assert run("enter", "implement", "--unit", "api").returncode == OK

    def test_a_dropped_dependency_also_unblocks(self, run):
        """Descoping is a legitimate decision; it must not deadlock the rest."""
        run("unit", "add", "--id", "auth")
        run("unit", "add", "--id", "api", "--depends-on", "auth")
        run("enter", "dropped", "--unit", "auth")
        assert run("enter", "implement", "--unit", "api").returncode == OK

    def test_list_shows_what_is_blocking_what(self, run):
        run("unit", "add", "--id", "auth")
        run("unit", "add", "--id", "api", "--depends-on", "auth")
        assert "blocked by auth" in run("unit", "list").stdout


class TestJoinBarrier:
    def test_join_refuses_while_any_unit_is_open_and_names_it(self, run, tmp_path):
        run("unit", "add", "--id", "auth")
        run("unit", "add", "--id", "api")
        run("enter", "plan")
        finish(run, "auth", tmp_path)
        proc = run("enter", "join")
        assert proc.returncode == REFUSED
        assert "1 unit(s) still open" in proc.stderr
        assert "api(pending)" in proc.stderr

    def test_join_opens_once_every_unit_is_terminal(self, run, tmp_path):
        run("unit", "add", "--id", "auth")
        run("unit", "add", "--id", "api")
        run("enter", "plan")
        finish(run, "auth", tmp_path)
        finish(run, "api", tmp_path)
        assert run("enter", "join").returncode == OK
        assert run("enter", "verify").returncode == OK

    def test_a_dropped_unit_counts_as_terminal(self, run, tmp_path):
        run("unit", "add", "--id", "auth")
        run("unit", "add", "--id", "later")
        run("enter", "plan")
        finish(run, "auth", tmp_path)
        run("enter", "dropped", "--unit", "later")
        assert run("enter", "join").returncode == OK

    def test_joining_with_no_units_declared_is_refused(self, run):
        run("enter", "plan")
        proc = run("enter", "join")
        assert proc.returncode == REFUSED
        assert "no units were declared" in proc.stderr

    def test_verify_refuses_over_a_units_unread_findings(self, run, tmp_path):
        """A phase must not verify while a unit's worst findings are undecided."""
        run("unit", "add", "--id", "auth")
        run("enter", "plan")
        run("enter", "implement", "--unit", "auth")
        run("enter", "review", "--unit", "auth")
        run("findings", "--unit", "auth", "--file", write_json(
            tmp_path / "f.json", findings_doc(("BLOCKER", "boom", "a.ts:1"))))
        run("enter", "adjudicate", "--unit", "auth")
        run("enter", "escalated", "--unit", "auth")     # terminal? no - still open
        run("enter", "dropped", "--unit", "auth")       # now terminal
        run("enter", "join")
        proc = run("enter", "verify")
        assert proc.returncode == REFUSED
        assert "auth/r1-1" in proc.stderr


class TestBackwardsCompatibility:
    def test_the_flat_path_still_works_untouched(self, run, tmp_path):
        """A phase that is one unit should not have to declare one."""
        assert run("enter", "implement").returncode == OK
        assert run("enter", "review").returncode == OK
        run("findings", "--file", write_json(tmp_path / "f.json", findings_doc()))
        assert run("enter", "adjudicate").returncode == OK
        assert run("enter", "verify").returncode == OK

    def test_show_reports_units_when_there_are_any(self, run, tmp_path):
        run("unit", "add", "--id", "auth", "--title", "login flow")
        run("unit", "add", "--id", "api", "--depends-on", "auth")
        out = run("show").stdout
        assert "units: 2, 2 still open" in out
        assert "blocked by auth" in out
