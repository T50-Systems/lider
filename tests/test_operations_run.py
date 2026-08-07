"""Operations as a separate run: target, preflight/act/prove/close, strict mode.

Mirrors inception: recommended by default (with warnings), required under strict.
How to check lives in /preflight and /verify; the ledger only records ternary
verdicts and refuses undetermined-as-GO when strict.
"""
import json

import pytest

from models import Run

OK, REFUSED, UNDETERMINED, USAGE = 0, 1, 2, 3


@pytest.fixture
def ops(cli, tmp_path):
    def run(rid, *args):
        return cli("rungraph.py", "--dir", tmp_path, "--run", rid, *args)

    run.tmp = tmp_path
    return run


def state(ops, rid):
    return json.loads((ops.tmp / ".lider" / "runs" / rid / "run.json")
                      .read_text(encoding="utf-8"))


class TestOperationsHappyPath:
    def test_full_non_strict_path_warns_then_closes(self, ops):
        init = ops("o", "init", "--title", "deploy prod", "--kind", "operations")
        assert init.returncode == OK
        assert "RECOMMENDED" in init.stdout

        assert ops("o", "target", "--env", "prod", "--ref", "abc1234",
                   "--url", "https://example.test", "--surfaces", "api,web").returncode == OK
        assert ops("o", "enter", "scope").returncode == OK
        # preflight without recorded check: allowed non-strict after target
        assert ops("o", "enter", "preflight").returncode == OK
        act = ops("o", "enter", "act")
        assert act.returncode == OK
        assert "WARNING" in act.stderr and "preflight" in act.stderr
        assert ops("o", "check", "--name", "effect", "--verdict", "ok",
                   "--evidence", "version endpoint serves abc1234").returncode == OK
        assert ops("o", "enter", "prove").returncode == OK
        closed = ops("o", "enter", "closed")
        assert closed.returncode == OK
        assert "WARNING" not in closed.stderr  # effect already recorded
        st = state(ops, "o")
        Run.model_validate(st)
        assert st["node"] == "closed"
        assert st["kind"] == "operations"
        assert st["target"]["env"] == "prod"

    def test_preflight_without_target_is_undetermined(self, ops):
        ops("o", "init", "--title", "x", "--kind", "operations")
        ops("o", "enter", "scope")
        assert ops("o", "enter", "preflight").returncode == UNDETERMINED

    def test_show_lists_operations_artifacts(self, ops):
        ops("o", "init", "--title", "deploy", "--kind", "operations")
        out = ops("o", "show").stdout
        assert "artifacts:" in out
        assert "target pinned" in out
        ops("o", "target", "--env", "prod", "--ref", "abc1234", "--previous-ref", "old")
        ops("o", "check", "--name", "preflight", "--verdict", "ok", "--evidence", "go")
        out = ops("o", "show").stdout
        assert "ok   target pinned" in out or "ok  target pinned" in out
        assert "preflight check ok" in out


class TestOperationsStrict:
    def test_strict_blocks_act_without_preflight_ok(self, ops):
        ops("o", "init", "--title", "x", "--kind", "operations", "--strict")
        ops("o", "target", "--env", "prod", "--ref", "deadbeef")
        ops("o", "enter", "scope")
        ops("o", "enter", "preflight")
        proc = ops("o", "enter", "act")
        assert proc.returncode == REFUSED
        assert "STRICT" in proc.stderr

    def test_strict_allows_act_after_preflight_ok(self, ops):
        ops("o", "init", "--title", "x", "--kind", "operations", "--strict")
        ops("o", "target", "--env", "prod", "--ref", "deadbeef")
        ops("o", "enter", "scope")
        ops("o", "check", "--name", "preflight", "--verdict", "ok",
            "--evidence", "lock held, both deltas empty")
        ops("o", "enter", "preflight")
        assert ops("o", "enter", "act").returncode == OK

    def test_strict_blocks_closed_without_effect(self, ops):
        ops("o", "init", "--title", "x", "--kind", "operations", "--strict")
        ops("o", "target", "--env", "prod", "--ref", "deadbeef")
        ops("o", "enter", "scope")
        ops("o", "check", "--name", "preflight", "--verdict", "ok", "--evidence", "go")
        ops("o", "enter", "preflight")
        ops("o", "enter", "act")
        ops("o", "enter", "prove")
        proc = ops("o", "enter", "closed")
        assert proc.returncode == REFUSED
        assert "STRICT" in proc.stderr

    def test_strict_closes_after_effect_ok(self, ops):
        ops("o", "init", "--title", "x", "--kind", "operations", "--strict")
        ops("o", "target", "--env", "prod", "--ref", "deadbeef")
        ops("o", "enter", "scope")
        ops("o", "check", "--name", "preflight", "--verdict", "ok", "--evidence", "go")
        ops("o", "enter", "preflight")
        ops("o", "enter", "act")
        ops("o", "check", "--name", "effect", "--verdict", "ok",
            "--evidence", "prod /api/version = deadbeef")
        ops("o", "enter", "prove")
        assert ops("o", "enter", "closed").returncode == OK


class TestOperationsBoundaries:
    def test_cannot_enter_implement(self, ops):
        ops("o", "init", "--title", "x", "--kind", "operations")
        ops("o", "target", "--env", "prod", "--ref", "a")
        ops("o", "enter", "scope")
        proc = ops("o", "enter", "implement")
        assert proc.returncode == REFUSED
        assert "operations run cannot enter" in proc.stderr

    def test_spec_refused_on_operations(self, ops, tmp_path):
        ops("o", "init", "--title", "x", "--kind", "operations")
        f = tmp_path / "s.md"
        f.write_text("## Scope\nx\n## Hard constraints\ny\n## Mandatory verification\nz\n",
                     encoding="utf-8")
        proc = ops("o", "spec", "--file", f)
        assert proc.returncode == REFUSED
        assert "target" in proc.stderr

    def test_target_refused_on_construction(self, ops):
        ops("c", "init", "--title", "build")
        assert ops("c", "target", "--env", "prod", "--ref", "x").returncode == REFUSED

    def test_undetermined_preflight_is_not_ok_for_strict_act(self, ops):
        ops("o", "init", "--title", "x", "--kind", "operations", "--strict")
        ops("o", "target", "--env", "prod", "--ref", "a")
        ops("o", "enter", "scope")
        # check returns 2 for undetermined
        assert ops("o", "check", "--name", "preflight", "--verdict", "undetermined",
                   "--evidence", "could not read lock").returncode == UNDETERMINED
        ops("o", "enter", "preflight")
        assert ops("o", "enter", "act").returncode == REFUSED


class TestIncidentRollback:
    def _to_prove_failed(self, ops, strict=False):
        args = ["init", "--title", "deploy", "--kind", "operations"]
        if strict:
            args.append("--strict")
        ops("o", *args)
        ops("o", "target", "--env", "prod", "--ref", "badsha",
            "--previous-ref", "goodsha")
        ops("o", "enter", "scope")
        ops("o", "check", "--name", "preflight", "--verdict", "ok", "--evidence", "go")
        ops("o", "enter", "preflight")
        ops("o", "enter", "act")
        ops("o", "check", "--name", "effect", "--verdict", "not-ok",
            "--evidence", "5xx on /health")
        # not-ok must not block entering prove (signal for incident)
        assert ops("o", "enter", "prove").returncode == OK

    def test_failed_effect_can_enter_incident(self, ops):
        self._to_prove_failed(ops)
        proc = ops("o", "enter", "incident")
        assert proc.returncode == OK
        assert state(ops, "o")["node"] == "incident"

    def test_strict_incident_needs_signal(self, ops):
        ops("o", "init", "--title", "x", "--kind", "operations", "--strict")
        ops("o", "target", "--env", "prod", "--ref", "a", "--previous-ref", "b")
        ops("o", "enter", "scope")
        # jump-ish: no failure signal
        ops("o", "check", "--name", "preflight", "--verdict", "ok", "--evidence", "go")
        ops("o", "enter", "preflight")
        ops("o", "enter", "act")
        ops("o", "enter", "prove")
        proc = ops("o", "enter", "incident")
        assert proc.returncode == REFUSED
        assert "STRICT" in proc.stderr

    def test_strict_rollback_needs_previous_ref_and_preflight(self, ops):
        ops("o", "init", "--title", "x", "--kind", "operations", "--strict")
        ops("o", "target", "--env", "prod", "--ref", "bad")  # no previous_ref
        ops("o", "enter", "scope")
        ops("o", "check", "--name", "effect", "--verdict", "not-ok", "--evidence", "down")
        ops("o", "enter", "incident")
        proc = ops("o", "enter", "rollback")
        assert proc.returncode == REFUSED

    def test_rollback_then_prove_then_close(self, ops):
        self._to_prove_failed(ops, strict=True)
        assert ops("o", "enter", "incident").returncode == OK
        ops("o", "check", "--name", "rollback-preflight", "--verdict", "ok",
            "--evidence", "lock held for revert")
        assert ops("o", "enter", "rollback").returncode == OK
        # re-prove recovered state
        ops("o", "check", "--name", "effect", "--verdict", "ok",
            "--evidence", "prod serves goodsha")
        assert ops("o", "enter", "prove").returncode == OK
        assert ops("o", "enter", "closed").returncode == OK
        assert state(ops, "o")["node"] == "closed"
        assert state(ops, "o")["target"]["previous_ref"] == "goodsha"

    def test_closed_can_reopen_incident(self, ops):
        ops("o", "init", "--title", "x", "--kind", "operations")
        ops("o", "target", "--env", "prod", "--ref", "a", "--previous-ref", "b")
        ops("o", "enter", "scope")
        ops("o", "enter", "preflight")
        ops("o", "enter", "act")
        ops("o", "check", "--name", "effect", "--verdict", "ok", "--evidence", "ok")
        ops("o", "enter", "prove")
        ops("o", "enter", "closed")
        ops("o", "check", "--name", "health", "--verdict", "not-ok",
            "--evidence", "error rate spike post-close")
        assert ops("o", "enter", "incident").returncode == OK
