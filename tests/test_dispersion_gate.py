"""Tests for the CAND-5 dispersion gate in ``tools/model_matrix.py``.

The gate answers one question the count gate cannot: is the difference
between two arms bigger than the seed noise *inside* an arm?  It answers
it with a RATIO, ``|median_A - median_B| / pooled within-group IQR``, and
``NOT SEPARATED`` is a first-class NON-failing outcome.

The four worked cases below are the regression fixtures (measured on the
2026-10-02 store-vs-live cohort, RESEARCH-3 §5.2/§5.3):

======  ==================  ============  ========  ===============
case    gap                 pooled IQR    ratio     verdict
======  ==================  ============  ========  ===============
1       4 pp                0.075 pp      53x       RESOLVED
2       3 pp                19 pp         0.16x    NOT SEPARATED
3       0.91x                            0.91x    NOT SEPARATED
4       n=1 per arm         0.00 pp       --        UNDEFINED
======  ==================  ============  ========  ===============

Case 2 is the one that matters: ``len(seeds) >= 3`` — the count gate the
report already applied — **passes** it, because both arms have three
seeds.  That is the whole reason this gate exists, so
``test_count_gate_does_not_catch_case_2`` pins that explicitly.

Case 4 is the degenerate one: at n=1 a group IQR is exactly 0, so a naive
gate would divide by zero and report ``inf``, blessing the very anecdote
the surrounding prose exists to kill.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.model_matrix import (  # noqa: E402
    DISPERSION_NOT_SEPARATED,
    DISPERSION_RATIO_THRESHOLD,
    DISPERSION_RESOLVED,
    DISPERSION_UNDEFINED,
    MIN_REPLICATES_FOR_A_CLAIM,
    cmd_report,
    dispersion_verdict,
    pooled_within_spread,
    replicated_groups,
    summarize,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── helpers ────────────────────────────────────────────────────────────


def _group(label: str, median_value: float, iqr: float, n: int) -> dict:
    """A group row whose ``summarize`` output has exactly this median/IQR.

    Built from explicit quartiles rather than from raw replicate values so
    the fixture cannot drift with the quartile implementation: q1 = med -
    iqr/2, q3 = med + iqr/2, and the counts are the ones the count gate
    reads.
    """
    return {
        "label": label,
        "n_valid": n,
        "n_requested": n,
        "summary": {
            "n": n,
            "median": median_value,
            "q1": median_value - iqr / 2.0,
            "q3": median_value + iqr / 2.0,
            "min": median_value - iqr,
            "max": median_value + iqr,
        },
    }


def _backtest_json(**overrides) -> dict:
    payload = {
        "ticker_id": "ETH_USD",
        "model_name": "mtx_abc",
        "action_space": "discrete",
        "total_return": 0.12,
        "sharpe": 1.4,
        "max_drawdown": 0.08,
        "num_trades": 42,
        "win_rate": 0.55,
        "equity_curve": [10000.0, 10100.0, 11200.0],
        "n_steps": 480,
        "final_equity": 11200.0,
        "seed": 42,
        "buy_hold_return": 0.05,
        "excess_return": 0.07,
        "buy_hold_max_drawdown": 0.11,
        "n_bars": 504,
        "fee_rate": 0.0026,
        "slippage": 0.0005,
    }
    payload.update(overrides)
    return payload


def _args(results: Path, as_json: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        spec=None,
        results=str(results),
        min_bar_ratio=0.5,
        min_trades=1,
        json=as_json,
    )


def _noisy_records(tmp_path: Path, *, arm_a: float, arm_b: float, iqr: float):
    """Two arms, five seeds each, pinned window (so the cells are OOS).

    ``iqr`` is the within-arm spread, built so that ``summarize`` measures
    EXACTLY that: five sorted replicates put ``q1`` and ``q3`` on the
    second and fourth values, so ``q3 - q1`` is ``iqr`` and nothing about
    the fixture depends on the quartile implementation.  Both arms carry
    the same noise, so the only thing the gate can be reacting to is the
    ``arm_a - arm_b`` gap.
    """
    spreads = (-3.0, -0.5, 0.0, 0.5, 3.0)  # x iqr, centred on the median
    records = []
    for index, (ticker, median_value) in enumerate(
        (("ETH_USD", arm_a), ("SOL_USD", arm_b))
    ):
        for offset, seed in enumerate((1, 2, 3, 4, 5)):
            excess = median_value + spreads[offset] * iqr
            records.append(
                {
                    "cell_id": f"c{index}{offset}",
                    "index": len(records),
                    "status": "ok",
                    "params": {"ticker": ticker, "seed": seed},
                    "config_overrides": {
                        "data_window": {
                            "since": "2026-09-01T00:00:00Z",
                            "until": "2026-09-20T00:00:00Z",
                            "eval_split": 0.7,
                        }
                    },
                    "out_of_sample": True,
                    "expected_bars": 456,
                    "backtest": _backtest_json(
                        ticker_id=ticker,
                        seed=seed,
                        excess_return=excess,
                        total_return=excess + 0.05,
                        buy_hold_return=0.05,
                        sharpe=1.0 + offset,
                        num_trades=40 + offset,
                        n_bars=456,
                    ),
                }
            )
    path = tmp_path / "cells.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


# ── the estimator ───────────────────────────────────────────────────────


def test_pooled_spread_is_the_median_of_group_iqrs_not_the_max():
    """One wild group must not set the yardstick for every comparison.

    The measured case: two groups at IQR 0.075pp and a third at 19pp.  A
    ``max`` yardstick reports 0.21x where the median reports the 53x the
    data actually shows, i.e. max lets a single noisy group condemn every
    other pair in the cohort.
    """
    rows = [
        _group("a", 0.10, 0.00075, 5),
        _group("b", 0.06, 0.00075, 5),
        _group("wild", 0.30, 0.19, 5),
    ]
    pooled = pooled_within_spread(rows)
    assert pooled == pytest.approx(0.00075)
    # Explicitly not the max, and the difference is what flips the verdict.
    assert pooled != pytest.approx(max(0.00075, 0.00075, 0.19))
    assert dispersion_verdict(0.04, pooled, replicates=5)["verdict"] == DISPERSION_RESOLVED
    assert (
        dispersion_verdict(0.04, max(0.00075, 0.00075, 0.19), replicates=5)["verdict"]
        == DISPERSION_NOT_SEPARATED
    )


def test_pooled_spread_ignores_groups_below_the_count_gate():
    """A thin group's IQR must not be allowed to set the yardstick.

    At n=2 the "IQR" is the distance between the two seeds; quoting it as
    the cohort's spread would let the thinnest group decide what counts
    as resolved.
    """
    rows = [
        _group("thick-a", 0.10, 0.001, 5),
        _group("thick-b", 0.09, 0.003, 4),
        _group("thin", 0.30, 0.40, MIN_REPLICATES_FOR_A_CLAIM - 1),
    ]
    assert [r["label"] for r in replicated_groups(rows)] == ["thick-a", "thick-b"]
    assert pooled_within_spread(rows) == pytest.approx(0.002)


def test_pooled_spread_is_undefined_below_two_replicated_groups():
    """One arm has nothing to be measured against; say so, do not guess."""
    rows = [_group("only", 0.10, 0.001, 5)]
    assert pooled_within_spread(rows) is None


def test_zero_iqr_groups_yield_no_yardstick_rather_than_infinite_ratios():
    """Three seeds that agree exactly give IQR 0 — not a free pass."""
    rows = [
        _group("a", 0.10, 0.0, 3),
        _group("b", 0.50, 0.0, 3),
    ]
    assert pooled_within_spread(rows) is None
    verdict = dispersion_verdict(0.40, pooled_within_spread(rows), replicates=3)
    assert verdict["verdict"] == DISPERSION_UNDEFINED
    assert verdict["ratio"] is None


# ── the four worked cases ───────────────────────────────────────────────


def test_worked_case_1_gap_4pp_over_pooled_075pp_is_resolved():
    rows = [_group("store", 0.100, 0.00075, 5), _group("live", 0.060, 0.00075, 5)]
    pooled = pooled_within_spread(rows)
    verdict = dispersion_verdict(0.04, pooled, replicates=5)
    assert verdict["ratio"] == pytest.approx(53.33, abs=0.01)
    assert verdict["verdict"] == DISPERSION_RESOLVED


def test_worked_case_2_gap_3pp_over_pooled_19pp_is_not_separated():
    rows = [_group("a", 0.100, 0.19, 5), _group("b", 0.130, 0.19, 5)]
    pooled = pooled_within_spread(rows)
    verdict = dispersion_verdict(0.03, pooled, replicates=5)
    assert verdict["ratio"] == pytest.approx(0.158, abs=0.001)
    assert verdict["verdict"] == DISPERSION_NOT_SEPARATED


def test_count_gate_does_not_catch_case_2():
    """Why this gate exists: the pre-existing count gate PASSES case 2.

    Both arms have three seeds, so ``len(seeds) >= 3`` is satisfied and
    the old report said "medians are at least seed-stable" — about a
    3pp gap sitting inside 19pp of seed noise.  This assertion is the
    non-vacuity of the gate: if it ever passes, the count gate has been
    re-adopted as sufficient.
    """
    rows = [_group("a", 0.100, 0.19, 5), _group("b", 0.130, 0.19, 5)]
    assert len(replicated_groups(rows)) == 2  # the count gate is satisfied...
    assert pooled_within_spread(rows) > 0  # ...and a yardstick exists...
    assert dispersion_verdict(0.03, pooled_within_spread(rows), replicates=5)[
        "verdict"
    ] == DISPERSION_NOT_SEPARATED  # ...and the gap still does not clear it.


def test_worked_case_3_ratio_091_is_not_separated():
    verdict = dispersion_verdict(0.0091, 0.01, replicates=4)
    assert verdict["ratio"] == pytest.approx(0.91)
    assert verdict["verdict"] == DISPERSION_NOT_SEPARATED


def test_worked_case_4_one_replicate_is_undefined_not_resolved():
    """The degenerate case: n=1 gives IQR 0, so `inf -> RESOLVED` must not fire."""
    rows = [
        summarize([0.100]) | {"label": "store"},
        summarize([0.150]) | {"label": "live"},
    ]
    assert replicated_groups(rows) == []
    assert pooled_within_spread(rows) is None
    verdict = dispersion_verdict(0.05, pooled_within_spread(rows), replicates=1)
    assert verdict["verdict"] == DISPERSION_UNDEFINED
    assert verdict["ratio"] is None
    assert "fewer than 3 replicates" in verdict["reason"]


def test_gate_requires_the_count_and_the_spread_together():
    """Neither condition alone is enough to emit a number."""
    # Count satisfied, spread missing -> no verdict.
    assert dispersion_verdict(0.04, None, replicates=5)["verdict"] == DISPERSION_UNDEFINED
    # Spread present, count missing -> no verdict either.
    assert (
        dispersion_verdict(0.04, 0.00075, replicates=MIN_REPLICATES_FOR_A_CLAIM - 1)[
            "verdict"
        ]
        == DISPERSION_UNDEFINED
    )
    # Unknown count -> no verdict.
    assert dispersion_verdict(0.04, 0.00075, replicates=None)["verdict"] == (
        DISPERSION_UNDEFINED
    )


def test_threshold_is_a_judgement_call_the_ratio_is_not():
    """The same fact, read at a stricter threshold, flips only the label."""
    kwargs = dict(replicates=5)
    strict = dispersion_verdict(0.0091, 0.01, threshold=0.5, **kwargs)
    assert strict["verdict"] == DISPERSION_RESOLVED
    assert strict["ratio"] == dispersion_verdict(0.0091, 0.01, **kwargs)["ratio"]
    assert DISPERSION_RATIO_THRESHOLD == 1.0


# ── the report ──────────────────────────────────────────────────────────


def test_report_prints_a_ratio_and_a_verdict_per_arm_pair(tmp_path, capsys):
    path = _noisy_records(tmp_path, arm_a=0.100, arm_b=0.060, iqr=0.00075)
    assert cmd_report(_args(path)) == 0
    out = capsys.readouterr().out
    assert "DISPERSION GATE (CAND-5)" in out
    assert "RESOLVED" in out
    assert "ratio 53.33x" in out
    assert "pooled within-group IQR" in out


def test_report_prints_not_separated_as_a_finding_not_a_failure(tmp_path, capsys):
    """NOT SEPARATED must never be worded or behaved as an error."""
    path = _noisy_records(tmp_path, arm_a=0.100, arm_b=0.130, iqr=0.19)
    rc = cmd_report(_args(path))
    out = capsys.readouterr().out
    assert rc == 0  # a non-failing outcome
    assert "NOT SEPARATED" in out
    assert "NOT SEPARATED is a finding, not a failure" in out
    assert "ratio 0.16x" in out
    assert "0 RESOLVED, 1 NOT SEPARATED" in out
    assert "Traceback" not in out and "error:" not in out


def test_report_says_the_ratio_is_a_fact_and_the_threshold_a_judgement_call(
    tmp_path, capsys
):
    path = _noisy_records(tmp_path, arm_a=0.100, arm_b=0.060, iqr=0.00075)
    cmd_report(_args(path))
    out = capsys.readouterr().out
    assert "The ratio is a FACT" in out
    assert "threshold is a JUDGEMENT CALL" in out


def test_report_states_the_n3_limitation_as_necessary_not_sufficient(
    tmp_path, capsys
):
    path = _noisy_records(tmp_path, arm_a=0.100, arm_b=0.060, iqr=0.00075)
    cmd_report(_args(path))
    out = capsys.readouterr().out
    assert "NECESSARY, NOT SUFFICIENT" in out
    assert "does not manufacture power" in out


def test_report_leaves_every_pair_undefined_without_a_yardstick(tmp_path, capsys):
    """Below the count gate the gate says UNDEFINED rather than scoring."""
    path = _noisy_records(tmp_path, arm_a=0.100, arm_b=0.060, iqr=0.00075)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    # One seed per arm: keep only the first cell of each (ticker, config).
    records = [r for r in records if r["params"]["seed"] == 1]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    assert cmd_report(_args(path)) == 0
    out = capsys.readouterr().out
    assert "DISPERSION GATE: UNDEFINED" in out
    assert "UNADJUDICATED" in out
    assert "infinite" in out


def test_report_json_carries_the_dispersion_claims(tmp_path, capsys):
    path = _noisy_records(tmp_path, arm_a=0.100, arm_b=0.130, iqr=0.19)
    assert cmd_report(_args(path, as_json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    joined = "\n".join(payload["claims"])
    assert "DISPERSION GATE (CAND-5)" in joined
    assert "NOT SEPARATED" in joined
    assert payload["cells_out_of_sample"] == 10


def test_group_table_prints_the_yardstick_and_its_reason_when_absent(
    tmp_path, capsys
):
    path = _noisy_records(tmp_path, arm_a=0.100, arm_b=0.060, iqr=0.00075)
    cmd_report(_args(path))
    out = capsys.readouterr().out
    assert "pooled within-group IQR (median over 2 group(s)" in out
    # The per-ticker table has one group per ticker -> no yardstick, and
    # the table must SAY so rather than print nothing.
    assert "the dispersion gate is UNDEFINED for this table" in out


def test_no_106x_language_anywhere_in_the_harness():
    """The '106x' framing is false; the harness must not assert it.

    10,000 PPO steps over a deep store buys normalization-sample size and
    regime diversity, not 106x more gradient steps.  Grep the source so
    the slogan cannot creep back in through a comment.
    """
    text = (REPO_ROOT / "tools" / "model_matrix.py").read_text(encoding="utf-8")
    assert "106" not in text