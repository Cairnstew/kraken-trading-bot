"""The audit checker's own guards, proved able to FAIL.

`tools/audit_checks.py` exists because two audit defects were expensive: a
script cited as evidence that was not in any repo, and a findings check that
had been passing *vacuously*. This file is the second half of that lesson
applied forward -- a checker that has only ever printed OK is
indistinguishable from a checker that cannot fail, so every state the
checker can report is asserted here by constructing the input that produces
it, not by reading the checker's source.

The tests are deliberately **state-transition** tests, not source-inspection
tests: they call the classifier and the parser on synthetic trees, and they
assert the *returned state*, so a rewrite that changes the strings but not the
semantics fails loudly.

Run with the repo's own venv:  `.venv/bin/pytest tests/test_audit_checks.py`
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import audit_checks as ac  # noqa: E402


# ── cross-repo citation form: three states, and the middle one matters ───────
class TestCrossRepoStates:
    """`classify_cross_repo` has three outcomes; two of them are the point.

    A cross-repo citation can be true, or it can be *unverifiable here*, and
    conflating those two is the bug the form exists to remove: a checkout
    without its siblings is not a broken audit, so the ABSENT state must not
    fail, while a sibling that IS present and still does not resolve is a
    provably false citation and must.
    """

    def test_absent_sibling_is_unverified_and_not_an_error(self, monkeypatch) -> None:
        # Forced, not discovered: whether a declared sibling happens to be
        # checked out on the machine running the suite is not this test's
        # subject, and a host-dependent assertion is a test that silently
        # stops testing. An empty sibling set IS the bare-clone case.
        monkeypatch.setattr(ac, "_SIBLINGS", [])
        state, detail = ac.classify_cross_repo(
            "ticker-news-signals", "tests/test_export_append.py", None
        )
        assert state == ac.CROSS_REPO_ABSENT, detail
        assert state != "ERROR"

    def test_absent_sibling_state_is_distinct_from_verified(self) -> None:
        """The three states must not collide, or the label is decoration."""
        assert ac.CROSS_REPO_ABSENT != ac.CROSS_REPO_OK
        assert ac.CROSS_REPO_ABSENT != "ERROR"
        assert ac.CROSS_REPO_OK != "ERROR"

    def test_present_sibling_with_missing_file_is_an_error(self, tmp_path, monkeypatch) -> None:
        """The sibling IS here and the file is not -> provably a false citation."""
        sib = tmp_path / "ticker-news-signals"
        sib.mkdir()
        monkeypatch.setattr(ac, "_SIBLINGS", [sib])
        state, detail = ac.classify_cross_repo(
            "ticker-news-signals", "tests/test_gc_producer_append.py", None
        )
        assert state == "ERROR"
        assert "does not exist" in detail

    def test_present_sibling_with_missing_symbol_is_an_error(self, tmp_path, monkeypatch) -> None:
        """Right file, wrong test name. This is the failure a bare basename
        cannot see, and the reason the form carries an optional ``#symbol``."""
        sib = tmp_path / "ticker-news-signals"
        (sib / "tests").mkdir(parents=True)
        (sib / "tests" / "test_export_append.py").write_text(
            "def test_two_appends_keep_the_first_file_intact():\n    pass\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(ac, "_SIBLINGS", [sib])
        state, detail = ac.classify_cross_repo(
            "ticker-news-signals",
            "tests/test_export_append.py",
            "test_two_appends_keep_the_first_file_intact_v2",
        )
        assert state == "ERROR"
        assert "defines no" in detail

    def test_resolved_citation_with_symbol_is_verified(self, tmp_path, monkeypatch) -> None:
        sib = tmp_path / "ticker-news-signals"
        (sib / "tests").mkdir(parents=True)
        (sib / "tests" / "test_export_append.py").write_text(
            "def test_two_appends_keep_the_first_file_intact():\n    pass\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(ac, "_SIBLINGS", [sib])
        state, detail = ac.classify_cross_repo(
            "ticker-news-signals",
            "tests/test_export_append.py",
            "test_two_appends_keep_the_first_file_intact",
        )
        assert state == ac.CROSS_REPO_OK
        assert detail.endswith("::test_two_appends_keep_the_first_file_intact")

    def test_undeclared_repo_token_is_an_error(self, tmp_path, monkeypatch) -> None:
        """A typo'd repo name must not read as 'sibling absent'.

        Absent is a legitimate, non-failing state precisely because it means
        "cannot check". If a typo also produced Absent, the non-failing state
        would swallow the failing one and the check would be toothless.
        """
        monkeypatch.setattr(ac, "_SIBLINGS", [])
        state, detail = ac.classify_cross_repo("no-such-sibling", "x.py", None)
        assert state == "ERROR"
        assert "not a declared sibling" in detail

    def test_this_repo_can_be_cited_cross_repo(self) -> None:
        state, detail = ac.classify_cross_repo(
            "kraken-trading-bot", "tools/audit_checks.py", None
        )
        assert state == ac.CROSS_REPO_OK
        # The prefix is the checkout's own directory name, which is the long
        # ensemble-worktree name here and `kraken-trading-bot` in the primary
        # checkout. Assert the suffix, which is the part that means something.
        assert detail.endswith("/tools/audit_checks.py")


# ── the regex: what counts as a cross-repo citation ─────────────────────────
class TestCrossRepoRegex:
    def test_matches_repo_colon_path(self) -> None:
        m = ac.CROSS_REPO_CITE.search("see `ticker-news-signals:tests/test_export_append.py`")
        assert m and m.group("repo") == "ticker-news-signals"
        assert m.group("path") == "tests/test_export_append.py"
        assert m.group("symbol") is None

    def test_matches_symbol_suffix(self) -> None:
        m = ac.CROSS_REPO_CITE.search(
            "`kraken-social-signals:tests/test_export_append.py#test_two_appends_keep_the_first_file_intact`"
        )
        assert m and m.group("repo") == "kraken-social-signals"
        assert m.group("symbol") == "test_two_appends_keep_the_first_file_intact"

    def test_plain_local_path_is_not_a_cross_repo_citation(self) -> None:
        """The in-repo form must be untouched by the new one.

        A regression here would silently reroute every existing citation
        through the sibling resolver, which is how a check stops meaning
        anything while still printing green.
        """
        for line in (
            "cites `tools/width_check.py:53-55` for the width ladder",
            "cites `kraken_trading_bot/rl/features.py:94-125` for the columns",
        ):
            assert ac.CROSS_REPO_CITE.search(line) is None, line

    def test_repo_token_cannot_be_a_path(self) -> None:
        """`a/b:c.py` is not a sibling citation -- the token is `\\w-` only.

        Without this a relative directory name would parse as a repo and the
        resolver would be asked for a checkout named `a/b`, which no glob
        would ever produce.
        """
        assert ac.CROSS_REPO_CITE.search("`some/dir:tests/test_x.py`") is None


# ── quotations vs claims ────────────────────────────────────────────────────
class TestQuotedSpans:
    """A correction note must be able to name the wrong string verbatim.

    DECISION.md records the false citation it is correcting. If quoting it
    re-raised it as a live finding, the honest act of recording the mistake
    becomes unrepresentable -- so a double-backtick span is masked out before
    citations are collected. Tested in both directions: the quote must not
    register, and a live claim on the SAME LINE must still register.
    """

    def test_double_backtick_quote_is_not_a_claim(self) -> None:
        line = "an earlier revision cited ``tests/test_gc_producer_append.py`` as a file here"
        assert ac.CROSS_REPO_CITE.search(ac._claims(line)) is None
        import re as _re

        assert _re.search(r"`([\w./-]+\.(?:py|sh))`", ac._claims(line)) is None

    def test_live_claim_on_the_same_line_still_registers(self) -> None:
        line = (
            "``tests/test_gc_producer_append.py`` was wrong; the real one is "
            "`ticker-news-signals:tests/test_export_append.py`"
        )
        m = ac.CROSS_REPO_CITE.search(ac._claims(line))
        assert m and m.group("repo") == "ticker-news-signals"

    def test_single_backtick_is_still_a_claim(self) -> None:
        assert "`tests/test_export_append.py`" in ac._claims("`tests/test_export_append.py`")


# ── sibling-root discovery: the worktree case ──────────────────────────────
class TestSiblingRoots:
    def test_includes_home_projects(self) -> None:
        """`REPO.parent` alone is empty in a worktree.

        An Ensemble worktree sits at
        ``~/.local/share/opencode/worktree/<sha>/<name>``, so its parent is
        the worktree dir and its grandparent a hash dir -- neither holds a
        sibling. `$HOME/Projects` is what makes cross-repo resolution work in
        the place the audit actually runs.
        """
        assert (Path.home() / "Projects") in ac.sibling_roots()

    def test_declaration_and_resolution_agree(self) -> None:
        """A repo may not be declared but unresolvable, or vice versa.

        The two sets are separate constants for a reason; this is the
        assertion that stops them drifting into disagreement.
        """
        for repo, dirname in ac.CROSS_REPO_DIRS.items():
            if dirname != ".":
                assert Path(dirname).name == dirname, repo

    def test_declared_repo_is_found_when_checked_out(self) -> None:
        """If a declared sibling IS on disk, the resolver must return it.

        This is the test that would have caught the `REPO.parent.glob`
        version, which returned nothing from a worktree and so reported
        every cross-repo citation as absent.
        """
        for dirname in {d for d in ac.CROSS_REPO_DIRS.values() if d != "."}:
            if any(p.name == dirname for p in ac._sibling_checkouts()):
                state, _ = ac.classify_cross_repo(
                    next(k for k, v in ac.CROSS_REPO_DIRS.items() if v == dirname),
                    "does-not-matter.py",
                    None,
                )
                # present-but-missing-file is an ERROR, which proves the
                # lookup happened at all -- UNVERIFIED would mean it did not.
                assert state == "ERROR"
                break
        else:
            pytest.skip("no declared sibling checkout on disk to test against")


# ── the findings table pin ──────────────────────────────────────────────────
class TestFindingsPin:
    def test_pin_ids_and_count_are_consistent(self) -> None:
        """`count` is redundant with `len(ids)` by construction.

        Kept anyway, and asserted equal, because the whole argument for the
        pin is that a count alone is blind: if they ever disagree, the
        `COUNT:` line reports a number nobody chose.
        """
        assert ac.FINDINGS_PIN["count"] == len(ac.FINDINGS_PIN["ids"])
        assert len(ac.FINDINGS_PIN["ids"]) == len(set(ac.FINDINGS_PIN["ids"]))

    def test_parse_finding_table_reads_the_real_table(self) -> None:
        text = (REPO / ".data-audit" / "VALIDATION.md").read_text(encoding="utf-8")
        rows = ac.parse_finding_table(text)
        pinned = ac.FINDINGS_PIN["ids"]
        assert set(rows) == set(pinned), (
            f"VALIDATION.md §8 table carries {sorted(set(rows))}, pin requires "
            f"{sorted(pinned)}"
        )
        assert len(rows) == ac.FINDINGS_PIN["count"]

    def test_parse_ignores_header_and_rule_rows(self) -> None:
        text = (
            "| id | one-line | disposition |\n"
            "|---|---|---|\n"
            "| **F-1** | a finding | **Closed** — because |\n"
        )
        assert set(ac.parse_finding_table(text)) == {1}

    def test_dropping_a_row_is_detectable_from_the_table_alone(self) -> None:
        """The drop must be visible as a SET difference, not a count.

        Relabelling F-9 as F-19 keeps the row count at 16, so anything that
        compares counts reports a clean table. This is the assertion that
        makes the identity pin load-bearing rather than decorative.
        """
        good = (
            "| **F-8** | eight | Closed |\n"
            "| **F-9** | nine | Resolved |\n"
            "| **F-10** | ten | Closed |\n"
        )
        assert set(ac.parse_finding_table(good)) == {8, 9, 10}
        dropped = good.replace("| **F-10** | ten | Closed |\n", "")
        assert set(ac.parse_finding_table(dropped)) == {8, 9}
        relabelled = good.replace("**F-9**", "**F-19**")
        assert len(ac.parse_finding_table(relabelled)) == 3  # count unchanged
        assert set(ac.parse_finding_table(relabelled)) == {8, 10, 19}  # set is not

    def test_vacuous_disposition_cells_are_detected(self) -> None:
        """A bulk 'deferred' carries no evidence and must not pass as one."""
        vac = ac.FINDINGS_PIN["vacuous_dispositions"]
        assert "deferred" in vac
        assert "out of scope" in vac
        for spelling in ("deferred", "Deferred.", "**out of scope**", "—", ""):
            norm = spelling.strip().strip("*_ ").strip().lower().rstrip(".")
            assert norm in vac or not norm, spelling

    def test_real_dispositions_are_not_flagged_as_vacuous(self) -> None:
        """The vacuity rule must not fire on this repo's own table.

        A check that rejects the correct answer is as broken as one that
        accepts the wrong one.
        """
        text = (REPO / ".data-audit" / "VALIDATION.md").read_text(encoding="utf-8")
        vac = ac.FINDINGS_PIN["vacuous_dispositions"]
        for n, (_, _, disp) in ac.parse_finding_table(text).items():
            norm = disp.strip().strip("*_ ").strip().lower().rstrip(".")
            assert norm not in vac, f"F-{n} disposition reads as vacuous: {disp!r}"


class TestFindingsPinEndToEnd:
    """`cmd_findings` must ACT on the pin, not merely be able to compute it.

    This class exists because a mutation of the checker proved the gap: with
    `if dropped:` rewritten to `if False:` the whole suite still reported
    26 passed, because every earlier test exercised `parse_finding_table`
    in isolation and none of them drove the command. Testing a helper and
    testing the check are different things, and only the second one catches a
    check that has stopped checking.

    Driven end-to-end over a synthetic `.data-audit/` so the repo's real
    VALIDATION.md is never the thing being mutated.
    """

    @staticmethod
    def _tree(tmp_path: Path, rows: str, decision_body: str = "") -> Path:
        audit = tmp_path / ".data-audit"
        audit.mkdir()
        (audit / "VALIDATION.md").write_text(
            "# V\n\n## 8. findings\n\n| id | one-line | disposition |\n|---|---|---|\n"
            + rows,
            encoding="utf-8",
        )
        (audit / "DECISION.md").write_text(decision_body or "# D\n", encoding="utf-8")
        return audit

    @staticmethod
    def _full_rows() -> str:
        return "".join(
            f"| **F-{n}** | finding {n} | **Closed** — evidence |\n"
            for n in sorted(ac.FINDINGS_PIN["ids"])
        )

    def _run(self, audit: Path, monkeypatch, capsys) -> tuple[int, str]:
        monkeypatch.setattr(ac, "AUDIT_DIR", audit)
        rc = ac.cmd_findings(argparse.Namespace())
        return rc, capsys.readouterr().out

    def test_the_real_tree_passes(self) -> None:
        rc = ac.cmd_findings(argparse.Namespace())
        assert rc == 0

    def test_dropping_a_row_from_the_table_fails_the_command(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        rows = "".join(
            line
            for line in self._full_rows().splitlines(keepends=True)
            if not line.startswith("| **F-9** |")
        )
        audit = self._tree(
            tmp_path,
            rows,
            decision_body="".join(f"F-{n} recorded\n" for n in sorted(ac.FINDINGS_PIN["ids"])),
        )
        rc, out = self._run(audit, monkeypatch, capsys)
        assert rc == 1
        assert "DROP-FROM-TABLE: F-9" in out, out
        assert "COUNT: table carries 15 row(s), pin requires 16" in out, out

    def test_relabelling_a_row_fails_even_when_the_count_is_unchanged(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """The case a count cannot see: F-9 -> F-19 is still 16 rows."""
        rows = self._full_rows().replace("| **F-9** |", "| **F-19** |", 1)
        audit = self._tree(tmp_path, rows, decision_body="F-19 recorded\n")
        rc, out = self._run(audit, monkeypatch, capsys)
        assert rc == 1
        assert "DROP-FROM-TABLE: F-9" in out, out
        assert "ADDED-WITHOUT-PIN: F-19" in out, out
        # and the count must NOT be among the complaints, or this test is
        # passing for the wrong reason
        assert "COUNT:" not in out, out

    def test_bulk_deferred_dispositions_fail_the_command(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        # Replace ONLY the third cell. Dropping the id as well would empty
        # the table and make the command exit early on "no F<n> identifiers",
        # which is a different exit with a different meaning.
        rows = re.sub(
            r"(\| \*\*F-\d+\*\* \| [^|]*\| )[^|]*\|",
            r"\1 deferred |",
            self._full_rows(),
        )
        assert rows.count("|  deferred |") == ac.FINDINGS_PIN["count"]
        audit = self._tree(tmp_path, rows, decision_body="F-n recorded\n")
        rc, out = self._run(audit, monkeypatch, capsys)
        assert rc == 1, out
        assert "VACUOUS DISPOSITION" in out, out
        assert "deferred" in out

    def test_a_missing_decision_record_still_fails(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """The pin must not have displaced the original half of the check."""
        audit = self._tree(tmp_path, self._full_rows(), decision_body="nothing here\n")
        rc, out = self._run(audit, monkeypatch, capsys)
        assert rc == 1
        assert "MISSING DISPOSITION" in out, out
        assert "PIN  OK" in out, out  # the pin half is still happy

    def test_a_complete_table_with_decisions_passes(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        audit = self._tree(
            tmp_path,
            self._full_rows(),
            decision_body="".join(f"F-{n} recorded\n" for n in sorted(ac.FINDINGS_PIN["ids"])),
        )
        rc, out = self._run(audit, monkeypatch, capsys)
        assert rc == 0, out
        assert "RESULT  PASS" in out


# ── the in-repo citation behaviour must be unchanged ────────────────────────
class TestInRepoBehaviourUnchanged:
    def test_superseded_script_map_still_resolves(self) -> None:
        for legacy, successor in ac.SUPERSEDED_SCRIPTS.items():
            assert legacy and successor
            assert (REPO / successor).exists(), successor

    def test_claim_docs_still_lists_the_audit_artifacts(self) -> None:
        for name in ("DECISION.md", "VALIDATION.md"):
            assert name in ac.CLAIM_DOCS

    def test_ast_selftest_still_detects_every_mutant(self) -> None:
        """The existing self-test must keep passing: it is the check that
        proves the executable-structure comparison can fail at all."""
        results = ac.ast_selftest()
        assert results, "self-test produced no cases"
        bad = [label for label, ok in results if not ok]
        assert not bad, f"ast self-test failed for: {bad}"
