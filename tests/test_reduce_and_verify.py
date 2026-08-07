"""Folding a fan-out into a round, and putting its claims to skeptics.

The rule both halves share, and the reason they exist: an absence is an absence.
A lens that crashed is not a lens that found nothing, and a ballot that never
came back is not a vote to drop the claim.
"""
import pytest

from conftest import findings_doc, read_json, write_json

OK, UNDETERMINED = 0, 2


def manifest(tmp_path, *lenses):
    """lenses: (name, engine, out_path_or_None, exit_code)"""
    return write_json(tmp_path / "manifest.json", {"lenses": [
        {"lens": name, "engine": engine, "model": None,
         "out": str(out) if out else str(tmp_path / "missing.json"),
         "exit": code, "reason": None if code == 0 else "exit %s" % code}
        for name, engine, out, code in lenses]})


class TestReduce:
    def test_two_lenses_on_one_line_collapse_and_corroborate(self, cli, tmp_path):
        a = write_json(tmp_path / "a.json", findings_doc(
            ("BLOCKER", "race on the cache map", "cache.ts:40"), engine="claude"))
        b = write_json(tmp_path / "b.json", findings_doc(
            ("MAJOR", "data race on the cache map", "cache.ts:40"), engine="grok"))
        out = tmp_path / "round.json"
        proc = cli("reduce-findings.py", "--manifest",
                   manifest(tmp_path, ("correctness", "claude", a, 0),
                            ("regression", "grok", b, 0)),
                   "--out", out)
        assert proc.returncode == OK
        doc = read_json(out)
        assert len(doc["findings"]) == 1
        corr = doc["findings"][0]["corroboration"]
        assert corr["engines"] == 2 and corr["lenses"] == 2

    def test_the_worst_severity_survives_a_disagreement(self, cli, tmp_path):
        a = write_json(tmp_path / "a.json", findings_doc(("NIT", "same spot", "x.ts:3")))
        b = write_json(tmp_path / "b.json", findings_doc(("BLOCKER", "same spot", "x.ts:3")))
        out = tmp_path / "round.json"
        cli("reduce-findings.py", "--manifest",
            manifest(tmp_path, ("a", "claude", a, 0), ("b", "grok", b, 0)), "--out", out)
        finding = read_json(out)["findings"][0]
        assert finding["severity"] == "BLOCKER"
        assert set(finding["corroboration"]["severity_spread"]) == {"BLOCKER", "NIT"}

    def test_two_lenses_on_ONE_engine_are_one_engine_twice(self, cli, tmp_path):
        """Same training, same blind spots. Corroboration must not inflate."""
        a = write_json(tmp_path / "a.json", findings_doc(("MAJOR", "same spot", "x.ts:3")))
        b = write_json(tmp_path / "b.json", findings_doc(("MAJOR", "same spot", "x.ts:3")))
        out = tmp_path / "round.json"
        cli("reduce-findings.py", "--manifest",
            manifest(tmp_path, ("correctness", "claude", a, 0), ("tests", "claude", b, 0)),
            "--out", out)
        corr = read_json(out)["findings"][0]["corroboration"]
        assert corr["engines"] == 1 and corr["lenses"] == 2

    def test_a_dead_lens_makes_coverage_undetermined(self, cli, tmp_path):
        """The whole reason the manifest exists."""
        a = write_json(tmp_path / "a.json", findings_doc(("MINOR", "nit", "x.ts:1")))
        out = tmp_path / "round.json"
        proc = cli("reduce-findings.py", "--manifest",
                   manifest(tmp_path, ("correctness", "claude", a, 0),
                            ("security", "grok", None, 124)),
                   "--out", out)
        assert proc.returncode == UNDETERMINED
        doc = read_json(out)
        assert doc["coverage"] == "undetermined"
        assert [m["lens"] for m in doc["missing"]] == ["security"]

    def test_a_lens_that_found_nothing_is_a_reviewer_not_a_gap(self, cli, tmp_path):
        """Empty findings and no answer are opposite facts."""
        a = write_json(tmp_path / "a.json", findings_doc(engine="claude", verdict="approve"))
        out = tmp_path / "round.json"
        proc = cli("reduce-findings.py", "--manifest",
                   manifest(tmp_path, ("security", "claude", a, 0)), "--out", out)
        assert proc.returncode == OK
        doc = read_json(out)
        assert doc["coverage"] == "complete" and doc["missing"] == []
        assert doc["reviewers"][0]["lens"] == "security"

    def test_unique_contribution_is_counted_per_lens(self, cli, tmp_path):
        """The number that decides whether a lens earns its slot."""
        a = write_json(tmp_path / "a.json", findings_doc(
            ("MAJOR", "shared defect here", "x.ts:1"),
            ("MINOR", "only this lens saw this", "z.ts:9")))
        b = write_json(tmp_path / "b.json", findings_doc(("MAJOR", "shared defect here", "x.ts:1")))
        out = tmp_path / "round.json"
        cli("reduce-findings.py", "--manifest",
            manifest(tmp_path, ("correctness", "claude", a, 0), ("echo", "grok", b, 0)),
            "--out", out)
        by = {r["lens"]: r for r in read_json(out)["reviewers"]}
        assert by["correctness"]["unique"] == 1
        assert by["echo"]["unique"] == 0        # contributed nothing of its own


def ballot(tmp_path, claim, vote, refuted, confidence="high", engine="claude"):
    return write_json(tmp_path / ("refute-%d-%d.json" % (claim, vote)),
                      {"engine": engine, "refuted": refuted, "confidence": confidence,
                       "reason": "because", "evidence": None})


def round_with(tmp_path, *items):
    return write_json(tmp_path / "round.json", findings_doc(*items))


class TestRefutation:
    def test_a_confident_majority_drops_the_claim(self, cli, tmp_path):
        rnd = round_with(tmp_path, ("BLOCKER", "claim A", "a.ts:1"))
        ballot(tmp_path, 0, 0, True)
        ballot(tmp_path, 0, 1, True, engine="grok")
        ballot(tmp_path, 0, 2, False)
        out = tmp_path / "v.json"
        proc = cli("verify-findings.py", "--round", rnd, "--dir", tmp_path,
                   "--votes", 3, "--out", out)
        assert proc.returncode == OK
        doc = read_json(out)
        assert doc["findings"] == [] and len(doc["dropped"]) == 1
        assert doc["dropped"][0]["refutation"]["status"] == "refuted"

    def test_a_low_confidence_refutation_does_not_count(self, cli, tmp_path):
        """Skeptics answer low-confidence when they could not establish either way.

        Letting that kill a real BLOCKER would make the adversarial pass a
        defect-hiding machine.
        """
        rnd = round_with(tmp_path, ("BLOCKER", "claim A", "a.ts:1"))
        ballot(tmp_path, 0, 0, True, confidence="high")
        ballot(tmp_path, 0, 1, True, confidence="low")
        ballot(tmp_path, 0, 2, False)
        out = tmp_path / "v.json"
        cli("verify-findings.py", "--round", rnd, "--dir", tmp_path, "--votes", 3, "--out", out)
        doc = read_json(out)
        assert len(doc["findings"]) == 1
        assert doc["findings"][0]["refutation"]["status"] == "upheld"

    def test_below_quorum_the_claim_is_KEPT_and_flagged(self, cli, tmp_path):
        """A verification we could not run is not one that passed."""
        rnd = round_with(tmp_path, ("BLOCKER", "claim A", "a.ts:1"))
        ballot(tmp_path, 0, 0, True)            # only one of three came back
        out = tmp_path / "v.json"
        proc = cli("verify-findings.py", "--round", rnd, "--dir", tmp_path,
                   "--votes", 3, "--out", out)
        assert proc.returncode == UNDETERMINED
        doc = read_json(out)
        assert len(doc["findings"]) == 1
        assert doc["findings"][0]["refutation"]["status"] == "undetermined"
        assert doc["dropped"] == []
        assert doc["coverage"] == "undetermined"

    def test_minor_and_nit_are_never_put_to_a_vote(self, cli, tmp_path):
        rnd = round_with(tmp_path, ("NIT", "naming", "a.ts:1"), ("MINOR", "unused", "a.ts:2"))
        out = tmp_path / "v.json"
        proc = cli("verify-findings.py", "--round", rnd, "--dir", tmp_path,
                   "--votes", 3, "--out", out)
        assert proc.returncode == OK
        doc = read_json(out)
        assert len(doc["findings"]) == 2
        assert all("refutation" not in f for f in doc["findings"])
        assert doc["refutation_summary"]["claims"] == 0

    def test_a_plurality_is_not_a_majority(self, cli, tmp_path):
        """One refuter out of three, with the rest confirming, does not disprove."""
        rnd = round_with(tmp_path, ("MAJOR", "claim A", "a.ts:1"))
        ballot(tmp_path, 0, 0, True)
        ballot(tmp_path, 0, 1, False)
        ballot(tmp_path, 0, 2, False)
        out = tmp_path / "v.json"
        cli("verify-findings.py", "--round", rnd, "--dir", tmp_path, "--votes", 3, "--out", out)
        assert read_json(out)["findings"][0]["refutation"]["status"] == "upheld"
