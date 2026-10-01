"""Composition contract: the matrix harness against the REAL RL modules.

``tests/test_model_matrix.py`` is deliberately offline and RL-free — it
drives a synthesised fake CLI so it stays green while the RL side moves.
That property has a cost, and this file is where the cost is paid: it
imports the real modules and asserts that the two sides agree.

The bug this exists for was found on 2026-10-01, running the harness
against the real ``kraken-trading-bot`` binary for the first time. The
synthetic fake cannot express it, because it returns a fixed
``n_bars: 504`` for *every* spec — including specs whose pinned window
plus ``eval_split: 0.7`` means the backtest can only ever replay ~30% of
the span. A fake that claims a physically impossible bar count hides the
whole class of "the harness and the CLI disagree about the experiment".

Nothing here fetches anything. ``rl.data_window`` is pure pandas slicing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kraken_trading_bot.rl.backtest import BacktestResult  # noqa: E402
from kraken_trading_bot.rl.data_window import (  # noqa: E402
    resolve_data_window,
    split_index,
)
from tools.model_matrix import (  # noqa: E402
    BACKTEST_EXPRESSIBLE_KEYS,
    REQUIRED_BACKTEST_FIELDS,
    expected_bars_for,
    span_bars_for,
)


# The window the real 2026-10-01 run used, and the numbers it logged:
#   "kept 672 of 721 bars"
#   "-> 202 replayable bars (training took the leading 70%...)"
#   "178/202 bars replayed"
REAL_WINDOW = {
    "since": "2026-09-02T00:00:00Z",
    "until": "2026-09-30T00:00:00Z",
    "eval_split": 0.7,
}


def test_backtest_result_emits_every_field_the_harness_requires():
    """The 18-key contract, asserted against the real ``to_dict()``.

    The harness codes against ``REQUIRED_BACKTEST_FIELDS`` and marks a cell
    INVALID when one is absent. If the RL side ever drops or renames a
    key, every matrix cell becomes INVALID at once — and nothing in the
    synthetic suite would notice.
    """
    result = BacktestResult(
        ticker_id="ETH_USD",
        model_name="mtx_probe",
        total_return=0.05,
        sharpe=0.04,
        max_drawdown=0.013,
        num_trades=154,
        win_rate=0.0,
        equity_curve=[10000.0, 10005.1],
        n_steps=178,
        final_equity=10005.1,
        seed=42,
        buy_hold_return=-0.024,
        excess_return=0.0245,
        buy_hold_max_drawdown=0.03,
        n_bars=178,
        fee_rate=0.0026,
        slippage=0.0005,
        action_space="continuous",
    )
    payload = result.to_dict()
    missing = [f for f in REQUIRED_BACKTEST_FIELDS if f not in payload]
    assert missing == [], f"harness requires these, to_dict() omits: {missing}"
    assert len(REQUIRED_BACKTEST_FIELDS) == 18


def test_span_bars_for_matches_a_28_day_hourly_window():
    assert span_bars_for({"data_window": REAL_WINDOW}, 60) == 672


@pytest.mark.parametrize("split", [0.3, 0.5, 0.7, 0.8, 0.9, 0.95])
def test_width_denominator_equals_the_real_eval_slice(split):
    """The harness's denominator must equal the RL side's eval slice.

    This is the exact disagreement found on 2026-10-01. The harness
    denominated ``n_bars`` by the whole 672-bar span while the backtest
    can only replay the 202-bar eval remainder, so the width guard
    demanded 336 bars and marked a correct 178-bar out-of-sample replay
    ``INVALID: n_bars:178<0.50x672``.
    """
    overrides = {"data_window": {**REAL_WINDOW, "eval_split": split}}
    span = span_bars_for(overrides, 60)
    window = resolve_data_window(overrides)
    rl_eval_slice = span - split_index(span, window)
    assert expected_bars_for(overrides, 60) == rl_eval_slice


def test_reproduces_the_real_2026_10_01_run():
    """The exact numbers the real binary logged, as an assertion.

    If the RL side's split arithmetic or the harness's denominator moves,
    this is the test that says so.
    """
    overrides = {"data_window": REAL_WINDOW}
    assert expected_bars_for(overrides, 60) == 202


def test_harness_rounding_matches_the_rl_side_exactly():
    """No off-by-one between the two implementations of the split."""
    for span in (2, 3, 7, 202, 456, 672, 721):
        for split in (0.3, 0.5, 0.7, 0.9):
            overrides = {"data_window": {**REAL_WINDOW, "eval_split": split}}
            window = resolve_data_window(overrides)
            assert split_index(span, window) == min(
                max(int(round(span * split)), 1), span - 1
            ) or span < 2


def test_unpinned_window_leaves_the_denominator_unset():
    """Both sides agree an unpinned window derives no denominator."""
    inert = {"data_window": {"since": None, "until": None, "eval_split": 0.7}}
    assert resolve_data_window(inert).is_pinned is False
    assert resolve_data_window(inert).has_split is False
    assert expected_bars_for(inert, 60) is None


def test_eval_split_one_is_no_split_on_both_sides():
    """``eval_split: 1.0`` must not shrink the denominator to 1 bar.

    ``DataWindow.has_split`` requires ``< 1.0``, so a pinned window with
    ``eval_split: 1.0`` replays in full. A harness that honoured 1.0 as a
    split would demand ~0 bars and let a near-empty replay pass.
    """
    overrides = {"data_window": {**REAL_WINDOW, "eval_split": 1.0}}
    assert resolve_data_window(overrides).has_split is False
    assert expected_bars_for(overrides, 60) == span_bars_for(overrides, 60)


def test_six_expressible_backtest_keys_still_match_the_contract():
    """The documented six keys are exactly what the harness narrows to.

    ``backtest --config`` reads ``fee_rate``, ``slippage``,
    ``action_space``, ``initial_balance``, ``market_data_store`` and
    ``data_window``; everything else comes from the model's own training
    config. If that set drifts, the harness starts writing backtest
    configs it believes are honoured and are not.
    """
    assert BACKTEST_EXPRESSIBLE_KEYS == frozenset(
        {
            "fee_rate",
            "slippage",
            "action_space",
            "initial_balance",
            "market_data_store",
            "data_window",
        }
    )


def test_data_window_bounds_are_half_open():
    """``since`` inclusive, ``until`` exclusive — as documented."""
    import pandas as pd

    window = resolve_data_window({"data_window": REAL_WINDOW})
    assert window.since == pd.Timestamp("2026-09-02T00:00:00Z")
    assert window.until == pd.Timestamp("2026-09-30T00:00:00Z")
    index = pd.date_range("2026-09-01", "2026-10-01", freq="h", tz="UTC")
    from kraken_trading_bot.rl.data_window import clip_to_window

    clipped = clip_to_window(pd.DataFrame(index=index), window)
    # The lower bound is in, the upper bound is out.
    assert clipped.index[0] == pd.Timestamp("2026-09-02T00:00:00Z")
    assert clipped.index[-1] == pd.Timestamp("2026-09-29T23:00:00Z")
    assert len(clipped) == 672
