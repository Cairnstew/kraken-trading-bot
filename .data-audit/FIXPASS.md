# FIXPASS — the two reds from `bbdbe56`

**Pass:** 2026-10-03 · **Scope:** `.data-audit/`, `tools/audit_checks.py`, `tests/test_audit_checks.py`
**Base:** `5eb1776` · **Not merged to master by this pass.**

Evidence for the two fixes, kept out of `RUN-LOG.md` because the lead is
editing that file. Every red run below is **verbatim**; where a run was
broken it is labelled and discarded per `RUN-LOG.md` §8.1, not reported as a
result.

---

## 0. The two reds, confirmed first

Both reproduced at `5eb1776` **before** any change, so the "before" state is
not asserted from memory.

`just audit-evidence` (`.venv` absent in a worktree, so the same invocation
the recipe makes, via the dev shell's python):

```
  tests/test_gc_producer_append.py NOT IN REPO   cited 2x  e.g. DECISION.md:401
  ...
  RESULT  3 ERROR(S), 10 warning(s)
    ERROR  client.py cited 1x as evidence but is NOT in the repo (e.g. RUN-LOG.md:628) -- a reader cannot re-run it
    ERROR  kraken-python/kraken_api/transport.py cited 1x as evidence but is NOT in the repo (e.g. RUN-LOG.md:628) -- a reader cannot re-run it
    ERROR  tests/test_gc_producer_append.py cited 2x as evidence but is NOT in the repo (e.g. DECISION.md:401) -- a reader cannot re-run it
```

`just audit-findings`:

```
  RESULT  MISSING DISPOSITION: F-2, F-3, F-4, F-5, F-6, F-7, F-8, F-9, F-10, F-11, F-12, F-13, F-14, F-16
```

**A third fact established, not assumed: `audit-evidence` has never been green
in this chain, and it is not `bbdbe56`'s fault.** Two of those three errors
are *false* — `client.py` and `kraken-python/kraken_api/transport.py` are
valid citations of sibling files. They errored because
`find_in_repo` searched `REPO.parent.glob("kraken-*")`, and
`REPO.parent` inside an Ensemble worktree is the worktree directory, not
`~/Projects`. The check was structurally incapable of resolving a cross-repo
citation in the only place it ever runs. Fixing the citation alone would have
left `audit-evidence` red.

---

## 1. RED 1 — `audit-evidence`

### 1a. The citation

`DECISION.md:401` cited ``tests/test_gc_producer_append.py``. Verified absent
from every reachable state:

```
$ git log --all --diff-filter=A -- '*test_gc_producer_append.py'      # (no output)
$ git stash list                                                     # (no output)
```

The evidence it stood for is real, in the siblings:

```
$ grep -c "^def test_" ~/Projects/ticker-news-signals/tests/test_export_append.py   -> 6
$ grep -c "^def test_" ~/Projects/kraken-social-signals/tests/test_export_append.py -> 6
$ grep -n "test_two_appends_keep_the_first_file_intact" <both>                     -> defined in both
```

`DECISION.md` §6 now cites them in the new form, names all six-test files, and
calls out the sentinel **by function name**:

```
  cross-repo citations (`repo:path`, `repo:path#symbol`):
    kraken-social-signals:tests/test_export_append.py          VERIFIED (cross-repo)  kraken-social-signals/tests/test_export_append.py  cited 1x
    kraken-social-signals:tests/test_export_append.py#test_two_appends_keep_the_first_file_intact VERIFIED (cross-repo)  kraken-social-signals/tests/test_export_append.py::test_two_appends_keep_the_first_file_intact  cited 1x
    ticker-news-signals:tests/test_export_append.py            VERIFIED (cross-repo)  ticker-news-signals/tests/test_export_append.py  cited 1x
    ticker-news-signals:tests/test_export_append.py#test_two_appends_keep_the_first_file_intact VERIFIED (cross-repo)  ticker-news-signals/tests/test_export_append.py::test_two_appends_keep_the_first_file_intact  cited 1x
    -> 4 VERIFIED, 0 UNVERIFIED (sibling absent) (non-failing by design)
```

The `#symbol` is load-bearing, not decoration: it is the RG1/RG2 sentinel, and
if the sibling ever renames or deletes it, the citation goes red. §6 says so.

### 1b. The cross-repo form — semantics

Form: `` `repo:path` `` or `` `repo:path#symbol` ``, repo token `[\w-]+` (no
slashes, so it can only name a *sibling directory*). Allow-listed in
`CROSS_REPO_DIRS`, reviewable in one place, same as the existing
`SUPERSEDED_SCRIPTS`.

Three states, and the middle one is the whole design:

| state | when | exit-code effect |
|---|---|---|
| `VERIFIED (cross-repo)` | sibling on disk, file resolves, named symbol is defined at module level (AST, with a regex fallback so an unparseable file cannot crash the check) | none |
| `UNVERIFIED (sibling absent)` | **no** checkout of that sibling on disk | **none — non-failing** |
| `ERROR` | sibling **is** on disk and the citation still does not resolve; or the repo token is not in the allow-list | fails the check |

The `ERROR`-on-present-but-missing rule is what makes the non-failing state
honest rather than toothless: *absent* means "cannot check"; *present and
missing* means "provably false". Collapsing them would let a typo'd repo
token pass as "sibling absent", so an undeclared token is deliberately an
error naming the allow-list.

Sibling roots are `REPO.parent`, `REPO.parent.parent` and `$HOME/Projects`.
The first two cover a primary checkout and a worktree; the third is what makes
it work from an Ensemble worktree, where neither of the other two contains a
sibling.

**The ABSENT state, demonstrated** (all six declared siblings are checked out
here, so a bare checkout is simulated by pointing `HOME` at an empty dir —
the worktree's parent dirs hold no siblings either):

```
$ HOME=/tmp/opencode/barehome … tools/audit_checks.py evidence
    client.py                        UNVERIFIED (sibling absent)  bare basename, 1 cite(s)  e.g. RUN-LOG.md:628
    kraken-python/kraken_api/transport.py UNVERIFIED (sibling absent)  bare basename, 1 cite(s)  e.g. RUN-LOG.md:628
    tests/test_gc_producer_append.py UNVERIFIED (sibling absent)  bare basename, 2 cite(s)  e.g. DECISION.md:428
    kraken-social-signals:tests/test_export_append.py          UNVERIFIED (sibling absent)  no checkout of kraken-social-signals under 2 candidate root(s)  cited 1x
    kraken-social-signals:tests/test_export_append.py#test_two_appends_keep_the_first_file_intact UNVERIFIED (sibling absent)  no checkout of kraken-social-signals under 2 candidate root(s)  cited 1x
    ticker-news-signals:tests/test_export_append.py            UNVERIFIED (sibling absent)  no checkout of ticker-news-signals under 2 candidate root(s)  cited 1x
    ticker-news-signals:tests/test_export_append.py#test_two_appends_keep_the_first_file_intact UNVERIFIED (sibling absent)  no checkout of ticker-news-signals under 2 candidate root(s)  cited 1x
    -> 0 VERIFIED, 4 UNVERIFIED (sibling absent) (non-failing by design)
  RESULT  PASS
RAW EXIT CODE = 0
```

Note the third line. ``tests/test_gc_producer_append.py`` — the file this pass
exists to correct — is a **warning** with no siblings and an **error** with
them. That is the correct treatment: with the siblings present we can *prove*
the citation false; without them we can only say it is uncheckable.

The bare-basename `client.py` / `transport.py` cases get the same treatment,
which is what takes `audit-evidence` from "3 errors, 2 of them false" to
green.

### 1c. The DELIBERATE-WRONG-CITATION red runs

**One discarded run, stated because §8.1 requires it.** The first cross-repo
run raised `TypeError: '<' not supported between instances of 'str' and
'NoneType'` — `sorted()` on a tuple whose third element was an optional
symbol. **That output is discarded and was not used as evidence of anything**;
the sort key was made explicit and the run repeated. The three runs below are
from the fixed checker.

**Red run 1 — right repo, wrong file.** Citation mutated to
``ticker-news-signals:tests/test_gc_producer_append.py`` (the never-existed
name, now aimed at a sibling where it is provably absent):

```
    ticker-news-signals:tests/test_gc_producer_append.py       ERROR  ticker-news-signals/tests/test_gc_producer_append.py does not exist  cited 1x
    -> 3 VERIFIED, 0 UNVERIFIED (sibling absent) (non-failing by design)

  RESULT  2 ERROR(S), 10 warning(s)
    ERROR  tests/test_gc_producer_append.py cited 2x as evidence but is NOT in this repo or any of 6 sibling checkout(s) (e.g. DECISION.md:428) -- a reader cannot re-run it
    ERROR  cross-repo citation ticker-news-signals:tests/test_gc_producer_append.py is FALSE: ticker-news-signals/tests/test_gc_producer_append.py does not exist (cited 1x, e.g. DECISION.md:408) -- the sibling is on disk, so this is provably a wrong citation
EXIT=1
```

**Red run 2 — right file, wrong symbol** (the sentinel's name misspelled
`_intact` → `_intact_v2`, which is exactly what happens when someone renames
it):

```
    ticker-news-signals:tests/test_export_append.py#test_two_appends_keep_the_first_file_intact_v2 ERROR  ticker-news-signals/tests/test_export_append.py exists but defines no 'test_two_appends_keep_the_first_file_intact_v2'  cited 1x
    -> 3 VERIFIED, 0 UNVERIFIED (sibling absent) (non-failing by design)
EXIT=1
```

**Red run 3 — undeclared repo token** (`kraken-unbacked-sibling`, i.e. the
typo shape):

```
    kraken-unbacked-sibling:tests/test_export_append.py        ERROR  'kraken-unbacked-sibling' is not a declared sibling repo (declare it in CROSS_REPO_DIRS)  cited 1x
EXIT=1
```

All three reverted; §1a's output is the restored state.

**One behaviour added because red run 1 exposed a defect in my own fix.**
Writing the correction into `DECISION.md` re-raised the corrected citation:
the checker cannot tell a *quotation* of a wrong string from a live claim, so
the honest act of recording the mistake was unrepresentable and the only way
to make the check green would have been to stop naming the mistake. Fixed
deterministically — a Markdown code span containing a code span
(`` ``…`` ``) is masked before citations are collected, so a quote is not a
claim while a live claim **on the same line** still registers. Both directions
are tested.

### 1d. Known remaining red, in a file I must not touch

```
    ERROR  tests/test_gc_producer_append.py cited 1x as evidence but is NOT in this repo or any of 6 sibling checkout(s) (e.g. RUN-LOG.md:615) -- a reader cannot re-run it
```

`RUN-LOG.md:615` is the lead's own record of this defect, written as a live
single-backtick citation. **The one-line fix is to wrap it as
`` ``tests/test_gc_producer_append.py`` ``**, which marks it a quotation and
takes `audit-evidence` to green. I have not made it** — `RUN-LOG.md` is the
lead's file. Reported rather than silently worked around.

---

## 2. RED 2 — `audit-findings`

### 2a. Root cause, established rather than assumed

The dispositions existed. `bbdbe56` rewrote `DECISION.md` (526 insertions /
760 deletions) and took §7.7/§7.8 — the whole disposition table — with it,
while `VALIDATION.md` §8 (which says dispositions live in `DECISION.md`
§7.7/§7.8) survived. Restored at `bbdbe56^`:

```
$ git show bbdbe56^:.data-audit/DECISION.md | grep -n "^### 7\.[78]"
488:### 7.7 Finding dispositions
505:### 7.8 Phase 4/5 finding dispositions (lead-appended after `aba7b9b`)
```

### 2b. The dispositions are RE-DERIVED, and three changed

Each row was re-derived at `5eb1776` from the file the claim is about. **They
are not the pre-rewrite rows.** Three came out different, and those are
marked `[CORRECTED]` in the table with the reason. Full text is in
`DECISION.md` §7.7; the deltas:

| id | recorded before | re-derived, and why it differs |
|---|---|---|
| **F-1** | "Accepted, not fixed here" | **Closed.** It *was* fixed, in `aba7b9b`; `features.py:506-507` now reads "52 is in fact the *all-null* width, and the shipped default composes 60". |
| **F-3** | "Accepted, bounded, and **asserted**" | Bounded and measured, but **nothing asserts 366**. `VALIDATION.md:225-233` measures 8,792 records over the window and says the count must be dated. What is asserted is `tests/test_rl_signal_config_wiring.py:951` — a young log with no overlap stays a WARNING. No test pins a day count, and none should: it is recomputed per call. |
| **F-5** | "Override, measured equivalent — §6.1 shows…" | The override stands, **but the measurement was deleted by the same rewrite.** The three-row `null`/`0.0`/today table lived in the old `DECISION.md` §6.1 and is gone from every revision since. Behaviour is shipped; justification is nowhere. Re-measure before relying on it. |
| **F-6** | "Override: fix the comment, not the value" | Correct **and now asserted**: `configs/default.yaml:192` is `12`, and `tests/test_rl_signal_config_wiring.py:1149` asserts `== 12` in both shipped configs, carrying the measured gap histogram `{1.0h: 8783, 2.0h: 6, 3.0h: 1}`. Confirmed independently by `VALIDATION.md` §5.4 (baseline max 12.0). |
| **F-7** | "Corrected and acted on — `--append` **skips hours the file already holds**" | **That claim is false against the code.** `ticker-news-signals:ticker_news_signals/export.py:188` is `mode = "a" if append else "w"` — no hour-skip — and its docstring says "The consumer's de-duplication means a duplicate hour is harmless, so … appending is always safe"; `54c9d24` says the same. The dedup *direction* **was** fixed, on the consumer: `data.py:798-809` exact-dup `keep="last"` → **`sort_index(kind="stable")`** → `floor("h")` → `groupby(level=0).last()`, arriving in `5951f72` (A2). Resolved design is *append unconditionally, dedup on the consumer*. |
| **F-9** | "**Open** — reviewer must re-derive and name the frame" | **Resolved.** `VALIDATION.md` §5.1 across five frames: `computed` tradable slice (697 bars) = 684 baseline / 697 backfilled; `observed` = 0/0. `first_tradable_index = 24` ⇒ `721 − 24 = 697`, `697 − 13 = 684`, arithmetically exact. Also corrects the builder's "identical both arms". |
| **F-10** | "Restate per shape" (a prescription) | Resolved by measurement: baseline max **12.0** on 720 nonzero bars, backfilled 0.0 (`VALIDATION.md` §5.4). |
| **F-11** | "**Open** — reviewer re-derives; two agents measured different arms" | **Resolved**: `computed` 13 baseline / 0 backfilled, `observed` 24 / 0, same arm and frame length. Mechanism: record at `2026-10-02T00:00Z`, frame right edge `23:00Z`, so 23 bars back and the 12 h carry reaches forward. The integrator's "byte-identical both arms" is **wrong** — backfilled is 0/721. |
| **F-12** | "…which is what the builder's test asserts" | **Could not find that test.** `VALIDATION.md` §4 does measure **14 keys** and lists all fourteen, and `tests/test_rl_signal_config_wiring.py:1231` writes `relative_funding_rate: None` — but nothing asserts `live_keys ⊆ backfilled_keys`. Recorded as not-found with the test that would settle it. |
| **F-4** | "Correction recorded" | **Stronger than recorded**: the correction landed *in the artifact*. The 12 was never literal — `069a826^` `AUDIT.md:150-153` said "≈709 of 721 bars read these six columns as their historical `0.0` fill", which implies 12. After the `f6d9118` rewrite, `AUDIT.md:307` reads "2 distinct values on **13 of 721** bars", and the 709 arithmetic is in no revision from `f6d9118` on. |
| **F-13** | Closed | Confirmed: `aba7b9b` by `-S"52 is in fact the *all-null* width"`, read back at `features.py:506-507`. Visible instance of F-1's disposition working — docstring-only, so `executable_ast` never saw it. |
| **F-14** | Accepted, not a defect | Confirmed with the receipt (`VALIDATION.md` §7.2): `2 passed, 21 deselected in 0.56s` with `data.py`/`features.py` reverted to `069a826`, plus `executable_ast … -> True` for both. The same rule caught §7.1's pin as genuinely non-vacuous (`1 failed, 22 deselected`), so it discriminates. |
| **F-16** | Fixed (the `ValueError` degrade) | Fixed, **and a second defect in the same function**: the sibling search was `REPO.parent.glob("kraken-*")`, empty in a worktree. That is §0. Fixed here. |
| **F-15** | Accepted, docstring-only | **Unverified by me** whether the requested note in `width_check.py`'s module docstring landed; I did not read it against the request. Behaviour unchanged either way. |

**Nothing is bulk "deferred" or "out of scope".** Two rows say plainly that
something could not be established (F-12's missing assertion, F-15's
unverified docstring) and name what would settle it. A finding that was not
addressed is itself a finding about the audit, and recording it as a
disposition would hide that.

`DECISION.md` §7.8 also restores **F-17** and **F-18**, which the rewrite
dropped along with everything else. They are not in `VALIDATION.md` §8 and
so are not in the pin; they are recorded because dropping them would repeat
the defect they describe.

**Green:**

```
findings-table pin  pinned against 5eb1776: 16 ids, 16 distinct
    PIN  OK  16/16 rows, all ids pinned, every disposition cell non-vacuous

  RESULT  PASS
EXIT=0
```

### 2c. The pin, and its mutation runs

**Why the identities live in code.** `cmd_findings` read the finding set *out
of the table it polices*, so a rewrite dropping a row deletes the evidence the
check would have compared against. This is not hypothetical: `bbdbe56` went
red only because the rewrite dropped the `DECISION.md` half and the check
reads ids from `VALIDATION.md`. Had it dropped the §8 **rows** instead, the
same rewrite would have gone **green**. A count is no protection either:
`F-9` → `F-19` is 16 rows either way.

So the ids live in `FINDINGS_PIN` in the code, not in the table. Bumping the
pin is a visible diff on a file that is not the table, and must be its own
commit with a stated reason.

**Mutation A — delete the `F-9` row:**

```
    PIN  DROP-FROM-TABLE: F-9 -- pinned in FINDINGS_PIN but ABSENT from VALIDATION.md §8's table (15 row(s)). A findings table that loses rows is not a rewrite, it is a deletion; if the finding is genuinely retired, retire it in FINDINGS_PIN too, in its own commit.
    PIN  COUNT: table carries 15 row(s), pin requires 16

  RESULT  2 PIN PROBLEM(S)
RAW EXIT CODE = 1
```

**Mutation B — relabel `F-9` as `F-19`; the row count is UNCHANGED at 16,
which is the case a count-only check is blind to:**

```
    PIN  DROP-FROM-TABLE: F-9 -- pinned in FINDINGS_PIN but ABSENT from VALIDATION.md §8's table (16 row(s)). …
    PIN  ADDED-WITHOUT-PIN: F-19 -- present in VALIDATION.md §8's table but not in FINDINGS_PIN. …

  RESULT  MISSING DISPOSITION: F-19
RAW EXIT CODE = 1
```

No `COUNT:` line fires — by design, and the reason the identity pin exists.

**Mutation C — four dispositions blanked to `deferred`:**

```
    PIN  VACUOUS DISPOSITION: F-3, F-5, F-12, F-14 -- the disposition cell is empty or a bare 'deferred' / 'out of scope'. If a finding was not addressed that is itself a finding about the audit; write that, and say what evidence would settle it.

  RESULT  1 PIN PROBLEM(S)
RAW EXIT CODE = 1
```

All reverted; the green output in §2b is the restored state.

---

## 3. The new tests, and one mutation of the checker that DID NOT bite

`tests/test_audit_checks.py`, **32 passed**, whole suite
**551 passed, 24 warnings in 39.95s**.

Three mutations of `tools/audit_checks.py` itself:

| mutation | suite |
|---|---|
| ABSENT state swallowed into `OK` | `1 failed, 25 passed` — `test_absent_sibling_is_unverified_and_not_an_error` |
| `#symbol` verification removed (`if False:`) | `1 failed, 25 passed` — `test_present_sibling_with_missing_symbol_is_an_error` |
| `DROP-FROM-TABLE` pin disabled (`if False:`) | **`26 passed`** — **did not bite** |

The third one is the finding. Every test up to that point exercised
`parse_finding_table` *in isolation* and proved the parser could see a
difference; none drove the command, so disabling the check entirely left the
suite green. **Testing a helper and testing a check are different things.**

`TestFindingsPinEndToEnd` was added to close it: it points `AUDIT_DIR` at a
synthetic `.data-audit/`, calls `cmd_findings`, and asserts on the **exit
code and the printed message**, never on the repo's real `VALIDATION.md`.
Re-run of the same mutation afterwards: **`2 failed, 30 passed`**
(`…_drops_a_row_from_the_table_fails_the_command`,
`…_relabelling_a_row_fails_even_when_the_count_is_unchanged`).

Two test bugs of my own were also caught this way before commit: an
ABSENT-state test that skipped depending on the host, and a generator that
replaced the whole row instead of the disposition cell (which emptied the
table and exited early on "no `F<n>` identifiers" — a different exit with a
different meaning).

---

## 4. What I could not establish

1. **`audit-evidence` cannot reach PASS** while `RUN-LOG.md:615` holds a live
   single-backtick citation to ``tests/test_gc_producer_append.py``. That file
   is the lead's. Fix is one word of quoting (§1d).
2. **F-15**: whether the requested note in `tools/width_check.py`'s module
   docstring actually landed. Unverified; behaviour unchanged either way.
3. **F-12**: no test asserts `live_keys ⊆ backfilled_keys`. Recorded as
   not-found with the test that would settle it.
4. **`git stash list` was empty and `git log --all --diff-filter=A` found
   nothing** for ``test_gc_producer_append.py``, which supports "never existed".
   I did not search unreachable/dangling objects (`git fsck`); the lead's
   bisection already established this, and I did not re-derive it.
