"""Deterministic audit checks: one invocation, one compact receipt each.

WHY THIS EXISTS. The CAND-3a gate's four structural checks were hand-rolled
roughly a dozen times across four review rounds, as a fresh throwaway script
each time. Two properties were lost by doing that:

1. **Reproducibility.** The width-hash assertion depended on a script in
   ``/tmp`` that is not in this repository, so nobody could re-derive the
   gate's central invariant. (That script is now ``tools/width_check.py``.)
2. **Receipt shape.** Raw output ran to a couple hundred lines and had to be
   re-read and re-summarised by hand every time, which is where the pass's
   one genuinely expensive mistake happened: the lead summarised a check's
   *intent* rather than its output, and a reviewer caught the file saying
   something different from what the summary claimed.

So each subcommand below prints a few lines, each line one assertion, with
the evidence inline. Every check is read-only. Nothing here commits.

Subcommands
-----------
``verify``     the four structural gate checks, against a reference commit
``evidence``   flags evidence in ``.data-audit/`` that cannot be re-derived
``findings``   finding <-> disposition parity between VALIDATION and DECISION
``closeout``   the deterministic close-out order, with preconditions checked
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AUDIT_DIR = REPO / ".data-audit"

#: The estimator + pre-registered threshold must never move. This is checked
#: as BYTE IDENTITY, which is the strongest available statement and is only
#: available because the cost-aware driver lives in its own file rather than
#: as a new subcommand of ``model_matrix``.
MEASUREMENT_TRACK = "tools/model_matrix.py"

#: Scratch scripts cited in the audit record that were later replaced by a
#: committed equivalent. Declared here rather than inferred from prose, so the
#: mapping is reviewable in one place and survives a rewrite of any artifact.
#: Each entry: legacy scratch name -> committed successor.
SUPERSEDED_SCRIPTS = {
    "score.py": "tools/cost_aware_gate.py",
    "dump_feat.py": "tools/width_check.py",
}

PY_FILES = [
    "kraken_trading_bot/rl/features.py",
    "kraken_trading_bot/rl/data_window.py",
    "kraken_trading_bot/rl/data.py",
    "kraken_trading_bot/rl/backtest.py",
    MEASUREMENT_TRACK,
    "tests/test_feature_nonfinite_guards.py",
]


# ── shared helpers ──────────────────────────────────────────────────────────
def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    ).stdout.strip()


def _blob(ref: str, rel: str) -> str | None:
    p = subprocess.run(
        ["git", "show", f"{ref}:{rel}"], cwd=REPO, capture_output=True, text=True, check=False
    )
    return p.stdout if p.returncode == 0 else None


class _StripDocsAndStrings(ast.NodeTransformer):
    """Blank every docstring AND every other string constant.

    Stripping only docstrings is what a first attempt did, and it was not
    enough: an f-string or error-message edit still shows up as an AST
    difference. Blanking all string constants makes the comparison a
    statement about executable structure alone. (Two bugs in the first
    version of this -- a visit method that skipped recursion, and a missing
    ``visit_Expr`` -- were caught only by the self-test below, which is why
    the self-test is not optional.)
    """

    def _blank(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return self.generic_visit(node)

    visit_Module = _blank
    visit_ClassDef = _blank
    visit_FunctionDef = _blank
    visit_AsyncFunctionDef = _blank
    visit_Expr = _blank

    def visit_Constant(self, node):  # noqa: N802
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return node


def executable_ast(src: str) -> str:
    return ast.dump(_StripDocsAndStrings().visit(ast.parse(src)), include_attributes=False)


def ast_selftest() -> list[tuple[str, bool]]:
    """The proof must be able to FAIL. Verified against known mutants."""
    base = "def f():\n    '''doc'''\n    return 1 + 1\n"
    with_str = 'def f():\n    """doc"""\n    return "boom" + 1\n'
    mutants = [
        # (label, base, mutant, should_the_two_differ)
        ("docstring changed", base, "def f():\n    '''other'''\n    return 1 + 1\n", False),
        ("comment added", base, "def f():\n    '''doc'''\n    # c\n    return 1 + 1\n", False),
        # A message-only edit: identical structure, different literal. This is
        # the case that proves strings are actually blanked rather than only
        # docstrings stripped -- the weaker proof called this "detected".
        ("message-only edit", with_str, 'def f():\n    """doc"""\n    return "other" + 1\n', False),
        ("constant changed", base, "def f():\n    '''doc'''\n    return 1 + 2\n", True),
        ("statement removed", base, "def f():\n    '''doc'''\n    return 1\n", True),
        ("call added", base, "def f():\n    '''doc'''\n    g()\n    return 1\n", True),
        ("arg default", base, "def f(a=2):\n    '''doc'''\n    return 1\n", True),
        ("f-string added", base, 'def f():\n    """doc"""\n    return f"{x}"\n', True),
    ]
    return [
        (label, (executable_ast(b) != executable_ast(m)) == should_differ)
        for label, b, m, should_differ in mutants
    ]


# ── verify ──────────────────────────────────────────────────────────────────
def cmd_verify(a: argparse.Namespace) -> int:
    # TWO references, because they answer two different questions and
    # conflating them produces a confident false alarm:
    #
    #   --prereg  has the estimator/threshold moved since the rule was
    #             pre-registered?  (the pre-registration commit)
    #   --since   has anything EXECUTABLE changed since this point?
    #             (the last commit you believe is code-identical)
    #
    # They are usually different commits. Pointing --since at a
    # pre-registration commit reports CHANGED for a file that a later
    # review round legitimately rewrote, which looks exactly like a
    # regression and is not one.
    ref = a.since or a.prereg
    ok = True
    print(
        f"audit-verify  HEAD={git('rev-parse', '--short', 'HEAD')}"
        f"  prereg={a.prereg}  since={a.since or a.prereg}"
    )

    # 1. measurement track byte-identity
    cur = (REPO / MEASUREMENT_TRACK).read_text()
    old = _blob(a.prereg, MEASUREMENT_TRACK)
    if old is None:
        print(f"  measurement-track  SKIP  {MEASUREMENT_TRACK} absent at {a.prereg}")
    else:
        same = old == cur
        ok = ok and same
        print(
            f"  measurement-track  {'MATCH' if same else 'CHANGED'}  "
            f"{MEASUREMENT_TRACK} byte-identical to prereg {a.prereg}"
        )

    # 2. executable AST identity, with a self-test first
    st = ast_selftest()
    if not all(okk for _, okk in st):
        print("  ast-proof          BROKEN  self-test failed: "
              + ", ".join(l for l, o in st if not o))
        return 2
    print(f"  ast-proof          SELF-TEST OK  ({len(st)} mutants detected correctly)")

    diffs, same_ct = [], 0
    for rel in PY_FILES:
        old_src = _blob(ref, rel)
        if old_src is None:
            continue
        same_ct += 1
        try:
            if executable_ast(old_src) != executable_ast((REPO / rel).read_text()):
                diffs.append(rel)
        except SyntaxError as e:
            diffs.append(f"{rel} (parse error: {e})")
    if same_ct:
        clean = not diffs
        ok = ok and clean
        print(
            f"  executable-ast     {'MATCH' if clean else 'CHANGED'}  "
            f"{same_ct - len(diffs)}/{same_ct} files unchanged since {ref} "
            f"(docs+strings blanked)"
        )
        for d in diffs:
            print(f"                     differs: {d}")

    # 3. suite
    if a.skip_suite:
        print("  suite              SKIP  (--skip-suite)")
    else:
        p = subprocess.run(
            ["pytest", "tests/", "-q"], cwd=REPO, capture_output=True, text=True, check=False
        )
        tail = (p.stdout or "") + (p.stderr or "")
        m = re.search(r"(\d+) passed", tail) or re.search(r"(\d+) failed", tail)
        summary = m.group(0) if m else "NO SUMMARY"
        passed = bool(re.search(r"\d+ passed", tail)) and p.returncode == 0
        ok = ok and passed
        print(f"  suite              {'PASS' if passed else 'FAIL'}  {summary}")

    # 4. width hash
    if a.frame:
        sys.path.insert(0, str(REPO / "tools"))
        from width_check import fingerprint, render  # noqa: PLC0415

        fp = fingerprint(Path(a.frame))
        expected = {k: v for k, v in (("obs", a.expect_obs), ("arr", a.expect_arr)) if v}
        _, match = render(fp, expected)
        ok = ok and match
        name = Path(a.frame).name
        print(
            f"  width              {'MATCH' if match else 'MISMATCH'}  {name} "
            f"features={fp['n_features']} start_index={fp['start_index']} "
            f"usable={fp['usable_bars']} nonfinite={fp['n_nonfinite_obs_cells']}"
        )
        if not expected:
            print(
                "                     (no --expect-obs/--expect-arr given: "
                "fingerprint only, not an assertion)"
            )
    else:
        print("  width              SKIP  (no --frame)")

    print(f"  RESULT             {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


# ── evidence ────────────────────────────────────────────────────────────────
#: Documents that make CLAIMS. A research note recording "the store was at
#: /tmp/x" is a true statement about where work happened; a gate record citing
#: /tmp as the source of a published figure is unreproducible. Flagging every
#: /tmp mention in every file produced 34 findings, almost all benign, which
#: is how a check gets ignored -- so scope to the claim-bearing docs and split
#: errors from warnings.
CLAIM_DOCS = (
    "DECISION.md",
    "VALIDATION.md",
    "PLAN.md",
    "RUN-LOG.md",
    "AUDIT.md",
    # FIXPASS.md is evidence for a docs pass, and it cites the same
    # things the decision cites. An evidence file that is exempt from
    # the evidence check is a hole shaped exactly like the one this
    # check exists to close.
    "FIXPASS.md",
)

#: A cross-repo citation is ``repo:path`` or ``repo:path#symbol``, backticked.
#:
#: WHY THE FORM EXISTS. This audit's decisions routinely turn on code that
#: lives in a SIBLING checkout -- `ticker-news-signals`, `kraken-social-
#: signals` -- not here. Before this form, the only way to cite such a file
#: was to write a bare basename, and ``find_in_repo`` then resolved it by
#: rglob: if two siblings had same-named files it picked whichever came
#: first alphabetically, and a file that existed in NO checkout was reported
#: as an error indistinguishable from one that exists in the wrong place.
#: That is how ``tests/test_gc_producer_append.py`` came to be cited as
#: evidence: it never existed in this repo, but the two tests it stood for
#: are real and live in the siblings. The citation was wrong about the
#: repo, not about the evidence.
#:
#: The repo token is ``[\w-]+`` -- no slashes -- so it can only ever name a
#: SIBLING DIRECTORY, never a path. A single-token ``foo:bar.py`` in prose
#: would be read as a cross-repo citation, so docs that need to cite a
#: local path keep using the plain backticked form.
CROSS_REPO_CITE = re.compile(
    r"`(?P<repo>[\w-]+):(?P<path>[\w./-]+\.(?:py|sh))(?:#(?P<symbol>\w+))?`"
)

#: A Markdown code span holding another code span: ``` ``foo.py`` ```.
#: Masked out before any citation is collected, because a citation written
#: that way is a QUOTATION -- a correction note naming the exact string that
#: was wrong -- and not a claim that the file exists. Without this, writing
#: down the false citation you are correcting re-raises it as a live finding,
#: which is the one thing a correction note must not do: it makes the honest
#: act of recording the mistake unrepresentable, so the alternative is to
#: stop naming the mistake, and then nobody can check the correction.
_QUOTED_SPAN = re.compile(r"``.+?``", re.S)


def _claims(line: str) -> str:
    """The part of ``line`` in which a citation is a CLAIM rather than a quote."""
    return _QUOTED_SPAN.sub(" ", line)

#: Repo token -> directory name to look for. Declared rather than globbed so
#: a typo in a citation is reported as a typo instead of silently becoming
#: "sibling absent", which is a non-failing state by design (see
#: :func:`classify_cross_repo`). ``.`` is this repo, which is legal to cite
#: but must still resolve.
CROSS_REPO_DIRS = {
    "ticker-news-signals": "ticker-news-signals",
    "kraken-social-signals": "kraken-social-signals",
    "kraken-python": "kraken-python",
    "kraken-market-data": "kraken-market-data",
    "kraken-deep-history": "kraken-deep-history",
    "kraken-funding-rates": "kraken-funding-rates",
    "kraken-trading-bot": ".",
}

#: Reported when a sibling is not on disk. NOT a failure, and that is the
#: whole point: a checkout that does not carry its siblings is not a broken
#: audit, it is an audit that cannot reach one file. Failing here would
#: punish every fresh clone. What must NOT be tolerated is the sibling being
#: PRESENT and the citation still not resolving -- that is provably a false
#: citation and stays an error.
CROSS_REPO_ABSENT = "UNVERIFIED (sibling absent)"
CROSS_REPO_OK = "VERIFIED (cross-repo)"


def sibling_roots() -> list[Path]:
    """Directories that may hold this repo's sibling checkouts.

    ``REPO.parent`` alone is wrong in exactly the place the audit runs. The
    primary checkout is ``~/Projects/kraken-trading-bot``, so its parent is
    ``~/Projects`` and the glob finds every sibling. An Ensemble/git worktree
    lives at ``~/.local/share/opencode/worktree/<sha>/<name>``, so its
    parent is the worktree directory and its grandparent is ``<sha>``: the
    glob finds nothing and every cross-repo citation degrades to ABSENT. A
    resolver that only worked in the primary checkout would report
    ``UNVERIFIED (sibling absent)`` for a citation that is perfectly valid,
    which is the false-negative shape that gets a check switched off.
    """
    cands = [REPO.parent, REPO.parent.parent, Path.home() / "Projects"]
    out: list[Path] = []
    for c in cands:
        try:
            if c.is_dir() and c not in out:
                out.append(c)
        except OSError:  # unreadable parent: a broken guess, not an answer
            continue
    return out


def _defines(path: Path, symbol: str) -> bool:
    """Does ``path`` define ``symbol`` at module level?

    AST first so a mention inside a docstring or a string cannot satisfy it;
    a regex fallback because a sibling file that does not parse must not
    turn the whole check into a traceback (F-16's lesson, applied forward).
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return re.search(rf"^\s*(?:def|class)\s+{re.escape(symbol)}\b", path.read_text(
            encoding="utf-8", errors="replace"), re.M) is not None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                return True
        # a decorated test is still a module-level def, so no recursion into
        # nested scopes is wanted here: a symbol found only inside another
        # function is not importable by name and does not count.
    return False


def _sibling_checkouts() -> list[Path]:
    """Every declared sibling checkout that is actually on disk.

    Derived from :data:`CROSS_REPO_DIRS` rather than a ``kraken-*`` glob so
    the set the resolver searches and the set the docs are allowed to name
    cannot drift apart. Cached per process: it is a handful of ``is_dir``
    calls and both call sites run inside one loop.
    """
    global _SIBLINGS
    if _SIBLINGS is None:
        names = {n for n in CROSS_REPO_DIRS.values() if n != "."}
        seen: list[Path] = []
        for root in sibling_roots():
            for name in sorted(names):
                cand = root / name
                if cand.is_dir() and cand not in seen:
                    seen.append(cand)
        _SIBLINGS = seen
    return _SIBLINGS


_SIBLINGS: list[Path] | None = None


def classify_cross_repo(repo: str, rel: str, symbol: str | None) -> tuple[str, str]:
    """``(state, detail)`` for one cross-repo citation.

    Three states, and the middle one is the one that is easy to get wrong:

    ``VERIFIED``            the sibling is here AND the file (and named
                            symbol) resolved. This is the only green.
    ``UNVERIFIED``          the sibling is NOT on disk. Non-failing by
                            design; see ``CROSS_REPO_ABSENT``.
    ``ERROR``               the sibling IS on disk and the citation still
                            does not resolve. Provably a false citation.
    """
    dirname = CROSS_REPO_DIRS.get(repo)
    if dirname is None:
        return "ERROR", f"{repo!r} is not a declared sibling repo (declare it in CROSS_REPO_DIRS)"
    if dirname == ".":
        root = REPO
    else:
        found = [p for p in _sibling_checkouts() if p.name == dirname]
        if not found:
            return CROSS_REPO_ABSENT, f"no checkout of {repo} under {len(sibling_roots())} candidate root(s)"
        root = found[0]
    target = root / rel
    if not target.is_file():
        return "ERROR", f"{root.name}/{rel} does not exist"
    if symbol and not _defines(target, symbol):
        return "ERROR", f"{root.name}/{rel} exists but defines no {symbol!r}"
    return CROSS_REPO_OK, f"{root.name}/{rel}" + (f"::{symbol}" if symbol else "")


def cmd_evidence(a: argparse.Namespace) -> int:
    if not AUDIT_DIR.exists():
        print(f"evidence: no {AUDIT_DIR}")
        return 0
    files = [md for md in sorted(AUDIT_DIR.glob("*.md")) if md.name in CLAIM_DOCS]
    files += sorted(AUDIT_DIR.glob("REVIEW*.md"))

    errors, warnings = [], []

    # 1. A cited script that is not in the repo cannot be re-run by anyone.
    print(f"evidence  scanned {len(files)} claim-bearing file(s) of .data-audit/")
    scripts: dict[str, list[str]] = {}
    tmps: list[tuple[str, str, int]] = []
    # A cross-repo citation is collected separately and NEVER fed to the
    # in-repo resolver: `ticker-news-signals:tests/test_export_append.py`
    # would otherwise be rglob'd by basename and could resolve to some other
    # repo's same-named file, which is the false-green this form removes.
    xrepo: dict[tuple[str, str, str | None], list[str]] = {}
    for md in files:
        for i, raw in enumerate(md.read_text().splitlines(), 1):
            line = _claims(raw)
            for m in re.finditer(r"`([\w./-]+\.(?:py|sh))`", line):
                scripts.setdefault(m.group(1), []).append(f"{md.name}:{i}")
            for m in CROSS_REPO_CITE.finditer(line):
                xrepo.setdefault((m.group("repo"), m.group("path"), m.group("symbol")), []).append(
                    f"{md.name}:{i}"
                )
            for m in re.finditer(r"/tmp/[A-Za-z0-9_./-]+", line):
                tmps.append((md.name, m.group(0).rstrip("`.,)"), i))

    print("\n  scripts cited as evidence:")
    if not scripts:
        print("    (none)")
    # Prose cites files by BASENAME ("features.py:1091"), and most of those
    # live under kraken_trading_bot/rl/ or tools/. Resolving against the repo
    # root only produced 15 false positives; resolve by basename anywhere
    # instead, which is what "can a reader find this?" actually means.
    def find_in_repo(name: str) -> str | None:
        direct = REPO / name
        if direct.exists():
            # `REPO / name` is the ABSOLUTE path when `name` is absolute, and
            # this audit legitimately cites a sibling checkout
            # (kraken-python/kraken_api/transport.py, kraken-market-data's
            # client.py). `relative_to(REPO)` then raises ValueError and
            # crashes the whole check on a VALID citation. A crash asserts
            # nothing, so degrade to the absolute path -- still findable by
            # the reader, which is the only thing this function decides.
            try:
                return str(direct.relative_to(REPO))
            except ValueError:
                return str(direct)
        # Prose cites by BASENAME ("features.py:1091"), and the audit also
        # legitimately cites SIBLING-repo scripts (kraken-market-data's
        # client.py, kraken-deep-history's seeder.py). Resolving against this
        # repo root alone produced 15 false positives; resolve by basename
        # across the repo and its sibling checkouts.
        #
        # `sibling_roots()` rather than `REPO.parent.glob("kraken-*")`: the
        # glob is empty inside a git worktree, which is where every audit
        # run happens, so it turned two valid citations (this audit's
        # RUN-LOG.md:628 references to kraken-python's transport.py and
        # kraken-market-data's client.py) into hard errors on every worktree
        # run while the primary checkout reported neither.
        for root in (REPO, *_sibling_checkouts()):
            for hit in root.rglob(Path(name).name):
                rel = hit.relative_to(root)
                if rel.parts[0] in (".venv", ".git", "node_modules") or "__pycache__" in rel.parts:
                    continue
                return str(rel) if root == REPO else f"{root.name}/{rel}"
        return None

    for path, cites in sorted(scripts.items()):
        if path in SUPERSEDED_SCRIPTS:
            successor = SUPERSEDED_SCRIPTS[path]
            ok = (REPO / successor).exists()
            if ok:
                print(f"    {path:32s} SUPERSEDED -> {successor}  ({len(cites)}x, resolved)")
            else:
                errors.append(
                    f"{path} is declared superseded by {successor}, which is missing"
                )
                print(f"    {path:32s} SUPERSEDED -> {successor}  MISSING SUCCESSOR")
            continue
        where = find_in_repo(path)
        if where:
            print(f"    {path:32s} in repo ({where})  cited {len(cites)}x")
        elif not _sibling_checkouts():
            # A bare basename that resolves nowhere, in an environment with
            # NO sibling checkout to resolve it in, is not a false citation
            # that has been proven -- it is a citation that cannot be
            # checked. Same rule as CROSS_REPO_ABSENT: report the distinct
            # non-failing state rather than failing a bare clone.
            warnings.append(
                f"{path} cited {len(cites)}x but no sibling checkout is on "
                f"disk to resolve a bare basename against (e.g. {cites[0]})"
            )
            print(
                f"    {path:32s} {CROSS_REPO_ABSENT}  bare basename, "
                f"{len(cites)} cite(s)  e.g. {cites[0]}"
            )
        else:
            errors.append(
                f"{path} cited {len(cites)}x as evidence but is NOT in this "
                f"repo or any of {len(_sibling_checkouts())} sibling checkout(s) "
                f"(e.g. {cites[0]}) -- a reader cannot re-run it"
            )
            print(f"    {path:32s} NOT IN REPO   cited {len(cites)}x  e.g. {cites[0]}")

    # 1b. Cross-repo citations, resolved against the sibling they name.
    if not xrepo:
        print("\n  cross-repo citations: (none)")
    else:
        n_ok = n_abs = 0
        print("\n  cross-repo citations (`repo:path`, `repo:path#symbol`):")
        for (repo, rel, symbol), cites in sorted(
            xrepo.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "")
        ):
            label = f"{repo}:{rel}" + (f"#{symbol}" if symbol else "")
            state, detail = classify_cross_repo(repo, rel, symbol)
            if state == CROSS_REPO_OK:
                n_ok += 1
            elif state == CROSS_REPO_ABSENT:
                n_abs += 1
                warnings.append(f"{label}: {detail}")
            else:
                errors.append(
                    f"cross-repo citation {label} is FALSE: {detail} "
                    f"(cited {len(cites)}x, e.g. {cites[0]}) -- the sibling is "
                    f"on disk, so this is provably a wrong citation"
                )
            print(f"    {label:58s} {state}  {detail}  cited {len(cites)}x")
        print(
            f"    -> {n_ok} VERIFIED, {n_abs} {CROSS_REPO_ABSENT} "
            f"(non-failing by design)"
        )

    # 2. /tmp paths in a claim-bearing doc: a warning, not an error. Most are
    #    legitimate ("the store was seeded here"); the ones that matter are
    #    where a PUBLISHED figure traces only to /tmp.
    print(f"\n  /tmp paths in claim-bearing docs: {len(tmps)}  (warnings, not errors)")
    for f, path, line in tmps[:10]:
        warnings.append(f"{f}:{line} {path}")
        print(f"    {f}:{line}  {path}")
    if len(tmps) > 10:
        print(f"    ... and {len(tmps) - 10} more")

    # 3. A recorded falsification attempt. This is the one that matters: a
    #    green check proves nothing until someone has tried to break it.
    corpus = "\n".join(md.read_text() for md in AUDIT_DIR.glob("*.md")).lower()
    falsified = any(
        k in corpus
        for k in ("inert by mutation", "proved inert", "mutation test", "mutants detected")
    )
    print(f"\n  a recorded falsification attempt exists: {falsified}")
    if not falsified:
        errors.append(
            "no mutation/falsification record anywhere in .data-audit/ -- a green "
            "check proves nothing until someone tries to break it"
        )

    print(f"\n  RESULT  {'PASS' if not errors else f'{len(errors)} ERROR(S), {len(warnings)} warning(s)'}")
    for e in errors:
        print(f"    ERROR  {e}")
    return 0 if not errors else 1


# ── findings ────────────────────────────────────────────────────────────────
#: The finding identities ``VALIDATION.md`` §8 is REQUIRED to carry.
#:
#: WHY A LITERAL PIN. ``cmd_findings`` reads the finding set *out of the
#: table it is meant to police*. A rewrite that drops a row therefore deletes
#: the very evidence the check would have compared against, and the check
#: reports a clean bill of health for a table that no longer says what it
#: used to. This is not hypothetical in this repo: ``bbdbe56`` rewrote
#: ``DECISION.md`` 526 insertions / 760 deletions and took §7.7/§7.8 — the
#: whole disposition table — with it, and ``just audit-findings`` went red
#: for the right reason by luck, because the *other* half of the check (does
#: each VALIDATION id appear in DECISION) reads DECISION for the ids. Had
#: the rewrite dropped the §8 rows instead, the same rewrite would have
#: gone green. A count is not the protection either: ``F-2`` and ``F-3``
#: dropped for ``F-17`` and ``F-18`` added is 17 either way.
#:
#: So the identities live HERE, in code, where a change to them is a visible
#: diff on a file that is not the table. Bumping the pin is deliberate work:
#: it must be its own commit with a stated reason, which is what makes the
#: drop reviewable rather than invisible.
#:
#: ``pinned_against`` is the tree the pin was read off — the commit whose
#: §8 index this set was transcribed from, not a commit that "endorses" it.
FINDINGS_PIN: dict[str, object] = {
    "pinned_against": "5eb1776",
    "count": 16,
    "ids": frozenset(range(1, 17)),
    # Disposition cells that mean "nothing happened", refused by name. A
    # bulk "deferred" is not a disposition: it is a finding about the audit,
    # and recording it as a disposition hides that. These four are the
    # spellings that would otherwise pass a length check while carrying no
    # evidence.
    "vacuous_dispositions": frozenset(
        {"deferred", "out of scope", "not addressed", "tbd", "todo", "n/a", "-", "—", "— —"}
    ),
}

#: A row of the §8 finding-index table: ``| **F-7** | finding | disposition |``
_FINDING_ROW = re.compile(r"^\|\s*\**\s*F-?(\d+)\s*\**\s*\|(.+)$")


def parse_finding_table(text: str) -> dict[int, tuple[int, str, str]]:
    """``{n: (line, finding_cell, disposition_cell)}`` from VALIDATION.md §8.

    Parsed from the TABLE, not from a free-text scan, because the table is
    what a rewrite silently loses: a finding mentioned in prose and absent
    from the table is exactly the drop this check exists to catch.
    """
    rows: dict[int, tuple[int, str, str]] = {}
    for i, line in enumerate(text.splitlines(), 1):
        m = _FINDING_ROW.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        # a header row (`| id | one-line | disposition |`) or a rule
        # (`|---|---|---|`) has no integer in cell 0, so it never reaches here
        rows[int(m.group(1))] = (i, cells[1], cells[2])
    return rows


def cmd_findings(a: argparse.Namespace) -> int:
    validation = AUDIT_DIR / "VALIDATION.md"
    decision = AUDIT_DIR / "DECISION.md"
    if not validation.exists():
        print(f"findings: no {validation}")
        return 0

    vtext = validation.read_text()
    dtext = decision.read_text() if decision.exists() else ""

    # The repo's finding convention is HYPHENATED -- `F-1`, `F-7` -- in both
    # DECISION.md and VALIDATION.md. This scan used `\bF(\d+)\b` and the
    # disposition lookup used the literal token `F{n}`, so neither matched the
    # convention the artifacts actually use: every check run reported "no
    # F<n> identifiers" and PASSED VACUOUSLY. Accept both spellings, and
    # match the disposition with a boundary so `F1` cannot match inside `F11`.
    found = sorted({int(m) for m in re.findall(r"\bF-?(\d+)\b", vtext)})
    if not found:
        print("findings  no F<n> identifiers in VALIDATION.md")
        return 0

    print(f"findings  {len(found)} finding(s) in VALIDATION.md: "
          f"{', '.join('F-' + str(n) for n in found)}")
    missing = []
    for n in found:
        pattern = re.compile(rf"\bF-?{n}\b")
        # A disposition is a mention in DECISION.md that is not merely the
        # finding id inside a quote of the validation text.
        hits = [i for i, l in enumerate(dtext.splitlines(), 1) if pattern.search(l)]
        recorded = bool(hits)
        if not recorded:
            missing.append(n)
        loc = f"DECISION.md:{hits[0]}" if hits else "-- NOT RECORDED --"
        print(f"    F-{n:<4d} {'recorded' if recorded else 'MISSING  '}  {loc}")

    # 2. THE PIN. The set of finding identities the §8 table must carry,
    #    held in FINDINGS_PIN rather than read back out of the table.
    pin_problems: list[str] = []
    rows = parse_finding_table(vtext)
    pinned: frozenset[int] = FINDINGS_PIN["ids"]  # type: ignore[assignment]
    want_count: int = FINDINGS_PIN["count"]  # type: ignore[assignment]
    vacuum = FINDINGS_PIN["vacuous_dispositions"]  # type: ignore[assignment]

    dropped = sorted(pinned - set(rows))
    added = sorted(set(rows) - pinned)
    print(
        f"\n  findings-table pin  pinned against {FINDINGS_PIN['pinned_against']}: "
        f"{want_count} ids, {len(pinned)} distinct"
    )
    if dropped:
        pin_problems.append(
            "DROP-FROM-TABLE: " + ", ".join(f"F-{n}" for n in dropped)
            + f" -- pinned in FINDINGS_PIN but ABSENT from VALIDATION.md §8's "
            f"table ({len(rows)} row(s)). A findings table that loses rows is "
            f"not a rewrite, it is a deletion; if the finding is genuinely "
            f"retired, retire it in FINDINGS_PIN too, in its own commit."
        )
    if added:
        pin_problems.append(
            "ADDED-WITHOUT-PIN: " + ", ".join(f"F-{n}" for n in added)
            + " -- present in VALIDATION.md §8's table but not in "
            "FINDINGS_PIN. A new finding must be added to the pin in the "
            "same commit, or the next table rewrite silently drops it."
        )
    if len(rows) != want_count:
        pin_problems.append(
            f"COUNT: table carries {len(rows)} row(s), pin requires "
            f"{want_count}"
        )
    vac: list[int] = []
    for n in sorted(rows):
        disp = rows[n][2].strip().strip("*_ ").strip()
        if disp.lower().rstrip(".") in vacuum or not disp:
            vac.append(n)
    if vac:
        pin_problems.append(
            "VACUOUS DISPOSITION: " + ", ".join(f"F-{n}" for n in vac)
            + " -- the disposition cell is empty or a bare 'deferred' / "
            "'out of scope'. If a finding was not addressed that is itself a "
            "finding about the audit; write that, and say what evidence "
            "would settle it."
        )
    for p in pin_problems:
        print(f"    PIN  {p}")
    if not pin_problems:
        print(
            f"    PIN  OK  {len(rows)}/{want_count} rows, all ids pinned, "
            f"every disposition cell non-vacuous"
        )

    ok = not missing and not pin_problems
    if missing:
        print(
            f"\n  RESULT  MISSING DISPOSITION: "
            + ", ".join(f"F-{n}" for n in missing)
        )
    elif pin_problems:
        print(f"\n  RESULT  {len(pin_problems)} PIN PROBLEM(S)")
    else:
        print("\n  RESULT  PASS")
    print("  (a finding with no decision-level record is the R5 defect class)")
    print("  (the pin is what stops a REWRITE of the table from deleting a row)")
    return 0 if ok else 1


# ── closeout ────────────────────────────────────────────────────────────────
def cmd_closeout(a: argparse.Namespace) -> int:
    head = git("rev-parse", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    upstream = git("rev-parse", "--abbrev-ref", "@{u}") or "(none)"
    at = git("rev-parse", "@{u}") if upstream != "(none)" else ""
    dirty = [l for l in git("status", "--porcelain").splitlines() if l.strip()]

    print(f"closeout  branch={branch}  upstream={upstream}")
    print(f"  HEAD == upstream   {'YES' if at and at == head else 'NO'}  ({head[:8]})")
    print(f"  working tree       {'clean' if not dirty else f'{len(dirty)} dirty entry(ies)'}")
    for d in dirty[:8]:
        print(f"                     {d}")

    steps = [
        "1. just audit-verify --prereg <pre-reg> --since <last-code-identical>",
        "2. git push                                         # push, THEN re-check step 1",
        "3. just audit-verify --prereg <same> --since <same>  # in the PUSHED state",
        "4. just audit-findings                             # every F<n> has a disposition",
        "5. just audit-evidence                             # nothing cited is /tmp-only",
        "6. Phase 8 self-improvement -> append the RUN LOG entry",
        "7. shutdown every teammate, THEN team_cleanup      # order matters; see command file",
    ]
    print("\n  ordered steps:")
    for s in steps:
        print(f"    {s}")

    ok = bool(at and at == head and not dirty)
    print(f"\n  RESULT  {'READY' if ok else 'NOT READY — resolve the above first'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or __file__).split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="the four structural gate checks")
    v.add_argument(
        "--prereg",
        required=True,
        help="the pre-registration commit; the estimator+threshold must be "
        "byte-identical to it",
    )
    v.add_argument(
        "--since",
        help="commit to compare executable AST against (defaults to --prereg). "
        "Use the last commit you believe is code-identical, NOT the "
        "pre-registration commit -- they are usually different.",
    )
    v.add_argument("--frame", help="parquet frame for the width fingerprint")
    v.add_argument("--expect-obs", help="expected obs FILE sha256")
    v.add_argument("--expect-arr", help="expected arr FILE sha256")
    v.add_argument("--skip-suite", action="store_true")
    v.set_defaults(fn=cmd_verify)

    sub.add_parser("evidence", help="flag non-reproducible evidence").set_defaults(fn=cmd_evidence)
    sub.add_parser("findings", help="finding <-> disposition parity").set_defaults(fn=cmd_findings)
    sub.add_parser("closeout", help="deterministic close-out order").set_defaults(fn=cmd_closeout)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())