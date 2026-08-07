"""Defect identity - the question three subsystems must answer the same way.

The reducer folds parallel reviewers into one round, the ledger tracks a defect
across adjudication rounds, and loop-until-dry decides whether a new wave found
anything new. All three call `same_defect`, so its edges are load-bearing.
"""
from lider import findings as fx


def key(summary, location):
    return fx.key({"summary": summary, "location": location})


class TestSameDefect:
    def test_same_line_beats_different_wording(self):
        """The case a count-based convergence check could never see.

        Two reviewers pointing at one line are describing one defect however
        differently they word it. Measured: 'race on the shared cache map' and
        'data race on the shared cache map' had to merge for the ledger to notice
        a BLOCKER surviving three rounds.
        """
        a = key("race on the shared cache map", "cache.ts:40")
        b = key("data race condition on the shared cache map", "cache.ts:40")
        assert fx.same_defect(a, b)

    def test_identical_text_different_file_stays_separate(self):
        """Merging these would hide one of two real defects."""
        a = key("add() returns a - b instead of a + b", "math.js:1")
        b = key("add() returns a - b instead of a + b", "other.js:1")
        assert not fx.same_defect(a, b)

    def test_close_paraphrase_merges_within_line_drift(self):
        a = key("unvalidated user input reaches the query builder", "db.ts:10")
        b = key("user input reaches the query builder unvalidated", "db.ts:14")
        assert fx.same_defect(a, b)

    def test_far_apart_lines_do_not_merge(self):
        a = key("missing null check", "a.ts:5")
        b = key("missing null check", "a.ts:400")
        assert not fx.same_defect(a, b)

    def test_unrelated_summaries_on_one_file_stay_separate(self):
        a = key("unused import of the path module", "a.ts:1")
        b = key("the retry loop never resets its backoff", "a.ts:9")
        assert not fx.same_defect(a, b)

    def test_documented_limit_paraphrase_without_a_line_match(self):
        """The known limit, asserted so it is a decision and not a surprise.

        Lexical measures do not recognise paraphrase. Without a line match these
        two stay separate, which is the direction we deliberately err in: a
        duplicate costs a moment, a wrongly merged finding is a defect nobody
        sees again.
        """
        a = key("add() returns a - b", "math.js:1")
        b = key("the add function subtracts its arguments", "math.js:30")
        assert not fx.same_defect(a, b)


class TestNormalisation:
    def test_windows_and_posix_paths_are_the_same_file(self):
        assert fx.norm_file(r"src\auth.ts:42") == fx.norm_file("src/auth.ts:42")

    def test_location_without_a_line_is_tolerated(self):
        assert fx.norm_file("src/auth.ts") == ("auth.ts", None)

    def test_missing_location_does_not_explode(self):
        assert fx.norm_file(None) == ("", None)


class TestMatch:
    def test_returns_the_matching_entry(self):
        known = [{"summary": "race on the cache map", "location": "cache.ts:40", "id": "r1-1"}]
        hit = fx.match({"summary": "data race on the cache map", "location": "cache.ts:40"}, known)
        assert hit is not None and hit["id"] == "r1-1"

    def test_returns_none_when_nothing_matches(self):
        known = [{"summary": "race on the cache map", "location": "cache.ts:40"}]
        assert fx.match({"summary": "unused import", "location": "z.ts:1"}, known) is None

    def test_empty_history_is_not_an_error(self):
        assert fx.match({"summary": "x", "location": "a:1"}, []) is None


def test_worst_severity_wins_a_disagreement():
    """A BLOCKER one reviewer called a NIT is still a BLOCKER worth reading."""
    assert fx.worst(["NIT", "BLOCKER", "MINOR"]) == "BLOCKER"
    assert fx.worst(["MINOR", "MAJOR"]) == "MAJOR"
