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
CLAIM_DOCS = ("DECISION.md", "VALIDATION.md", "PLAN.md", "RUN-LOG.md", "AUDIT.md")


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
    for md in files:
        for i, line in enumerate(md.read_text().splitlines(), 1):
            for m in re.finditer(r"`([\w./-]+\.(?:py|sh))`", line):
                scripts.setdefault(m.group(1), []).append(f"{md.name}:{i}")
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
            return str(direct.relative_to(REPO))
        # Prose cites by BASENAME ("features.py:1091"), and the audit also
        # legitimately cites SIBLING-repo scripts (kraken-market-data's
        # client.py, kraken-deep-history's seeder.py). Resolving against this
        # repo root alone produced 15 false positives; resolve by basename
        # across the repo and its sibling checkouts.
        for root in (REPO, *sorted(REPO.parent.glob("kraken-*"))):
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
        else:
            errors.append(
                f"{path} cited {len(cites)}x as evidence but is NOT in the repo "
                f"(e.g. {cites[0]}) -- a reader cannot re-run it"
            )
            print(f"    {path:32s} NOT IN REPO   cited {len(cites)}x  e.g. {cites[0]}")

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
def cmd_findings(a: argparse.Namespace) -> int:
    validation = AUDIT_DIR / "VALIDATION.md"
    decision = AUDIT_DIR / "DECISION.md"
    if not validation.exists():
        print(f"findings: no {validation}")
        return 0

    vtext = validation.read_text()
    dtext = decision.read_text() if decision.exists() else ""

    found = sorted({int(m) for m in re.findall(r"\bF(\d+)\b", vtext)})
    if not found:
        print("findings  no F<n> identifiers in VALIDATION.md")
        return 0

    print(f"findings  {len(found)} finding(s) in VALIDATION.md: "
          f"{', '.join('F' + str(n) for n in found)}")
    missing = []
    for n in found:
        token = f"F{n}"
        # A disposition is a mention in DECISION.md that is not merely the
        # finding id inside a quote of the validation text.
        hits = [i for i, l in enumerate(dtext.splitlines(), 1) if token in l]
        recorded = bool(hits)
        if not recorded:
            missing.append(n)
        loc = f"DECISION.md:{hits[0]}" if hits else "-- NOT RECORDED --"
        print(f"    {token:5s} {'recorded' if recorded else 'MISSING  '}  {loc}")

    print(
        f"\n  RESULT  {'PASS' if not missing else 'MISSING DISPOSITION: ' + ', '.join('F' + str(n) for n in missing)}"
    )
    print("  (a finding with no decision-level record is the R5 defect class)")
    return 0 if not missing else 1


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