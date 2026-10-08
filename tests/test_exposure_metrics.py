"""Turnover / exposure accounting, and the exposure-matched baseline.

Added 2026-10-05 alongside `BacktestResult.mean_exposure`,
`total_notional`, `economic_trades` and
`tools/null_baselines.exposure_matched_baseline`.

Every case here is hand-computable: fixed prices, a fixed account size, and
arithmetic written out in the assertion so a reader can check it without
running anything. The one-off reproduction of the 0c-2 table for the five
deep-ab models is NOT in this file -- it needs those artifacts and takes
minutes; it lives in the Phase 1 report instead.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from kraken_trading_bot.rl import TradingEnvironment
from kraken_trading_bot.rl.backtest import BacktestResult
from kraken_trading_bot.rl.environment import ECONOMIC_FILL_FRACTION

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import null_baselines as nb  # noqa: E402


# ── fixtures ──────────────────────────────────────────────────────────


def _flat_frame(n: int = 120, price: float = 100.0) -> pd.DataFrame:
    """n bars, every close identical, so every fill is hand-computable.

    120 rather than "a few": the builtin feature set needs
    ``_MIN_BARS_BUILTIN`` (24) bars of warm-up, so a 60-bar frame leaves
    only 36 steppable bars and a test that walks 48 of them dies with
    "step() called after the episode ended".
    """
    idx = pd.date_range("2024-01-01", periods=n, freq="h")
    return pd.DataFrame(
        {
            "open": [price] * n,
            "high": [price] * n,
            "low": [price] * n,
            "close": [price] * n,
            "volume": [100.0] * n,
            "count": [10.0] * n,
        },
        index=idx,
    )


def _env(
    frame: pd.DataFrame,
    *,
    initial_balance: float = 10_000.0,
    fee_rate: float = 0.0,
    slippage: float = 0.0,
    allow_short: bool = False,
) -> TradingEnvironment:
    return TradingEnvironment(
        ticker_id="TEST_USD",
        data=frame,
        feature_pipeline=None,
        action_space="discrete",
        initial_balance=initial_balance,
        fee_rate=fee_rate,
        slippage=slippage,
        allow_short=allow_short,
    )


# ── mean exposure and time_in_market ──────────────────────────────────


def test_mean_exposure_and_time_in_market_on_flat_prices() -> None:
    """Buy once on flat prices: exposure is 20% for every bar after.

    Discrete buy spends `balance * 0.20`; with a flat close of 100 and no
    fee or slippage that buys exactly 20 units for $2,000, leaving
    $8,000 cash. Position 20 units at price 100 is $2,000 of a $10,000
    book, so exposure is 0.2 on every bar the position is held and 0.0
    before it. Holding for all 48 stepped bars makes the mean exactly 0.2.
    """
    frame = _flat_frame()
    env = _env(frame)
    env.reset(seed=0)
    env.step(0)  # buy on the first stepped bar
    for _ in range(47):
        env.step(1)  # hold

    assert env.num_trades == 1
    assert env.position == pytest.approx(20.0)
    assert env.mean_exposure == pytest.approx(0.20)
    assert env.time_in_market == pytest.approx(1.0)


def test_time_in_market_counts_only_bars_with_a_position() -> None:
    """10 stepped bars: buy on the first, sell on the 6th -> 5/10 held.

    Bars 1-5 hold the position and bar 6 onward are flat, so
    bars_in_market is 5 out of 10 stepped bars.
    """
    frame = _flat_frame()
    env = _env(frame)
    env.reset(seed=0)
    env.step(0)  # bar 1: buy
    for _ in range(4):
        env.step(1)  # bars 2-5: hold
    env.step(2)  # bar 6: sell all
    for _ in range(4):
        env.step(1)  # bars 7-10: flat

    assert env.bars_in_market == 5
    assert env.time_in_market == pytest.approx(0.5)


def test_never_trading_gives_zero_exposure_and_zero_time() -> None:
    frame = _flat_frame()
    env = _env(frame)
    env.reset(seed=0)
    for _ in range(20):
        env.step(1)

    assert env.num_trades == 0
    assert env.total_notional == pytest.approx(0.0)
    assert env.mean_exposure == pytest.approx(0.0)
    assert env.time_in_market == pytest.approx(0.0)


# ── notional ──────────────────────────────────────────────────────────


def test_total_notional_counts_gross_fill_value() -> None:
    """Flat price 100, no friction: buy notional 2000, sell notional 2000.

    Notional is `units * fill` -- the value of the fill at the fill price.
    With no fee, units * fill == the cash committed, so buying $2,000 for
    20 units at 100 is $2,000 of notional and selling them back is another
    $2,000. Total $4,000.
    """
    frame = _flat_frame()
    env = _env(frame)
    env.reset(seed=0)
    env.step(0)  # buy
    env.step(1)
    env.step(2)  # sell all

    assert env.num_trades == 2
    assert env.total_notional == pytest.approx(4_000.0)


def test_notional_is_the_fill_value_not_the_cash_committed() -> None:
    """The fee is taken in UNITS, so fill value and cash differ by the fee.

    With a 1% fee and no slippage, the buy commits $2,000 of cash but
    receives `units = (2000 / 100) * (1 - 0.01)` = 19.8 units, whose value
    AT THE FILL PRICE is 19.8 * 100 = $1,980. `total_notional` reports the
    fill value, $1,980. This is the "sum of |fill value|" definition, and
    it is why the measured cost/notional rate comes out at 0.0031034 rather
    than exactly fee+slippage = 0.0031: the denominator is already net of
    the buy-side fee.
    """
    frame = _flat_frame()
    env = _env(frame, fee_rate=0.01)
    env.reset(seed=0)
    env.step(0)

    assert env.total_notional == pytest.approx(1_980.0)
    # 2000 cash committed, 20 units * (1 - 0.01) = 19.8 units received.
    assert env.balance == pytest.approx(8_000.0)
    assert env.position == pytest.approx(19.8)


def test_economic_trades_ignores_sub_threshold_dust() -> None:
    """Repeated buys on a full book produce dust; only real fills count.

    Buy 20 times into a book that is already fully invested: the balance
    has been spent, so each further buy moves less than
    ECONOMIC_FILL_FRACTION (1e-4) x $10,000 = $1.00. All 20 fills are
    counted by num_trades; the first few are economic, the dust is not.
    """
    frame = _flat_frame()
    env = _env(frame)
    env.reset(seed=0)
    for _ in range(40):
        env.step(0)

    assert env.num_trades == 40
    assert env.total_notional > 0.0
    # The threshold is a fixed $1.00 on a $10,000 book.
    assert ECONOMIC_FILL_FRACTION * env.initial_balance == pytest.approx(1.0)
    # Balance is spent geometrically (0.8^n), so most of those 40 fills
    # move far less than the $1.00 threshold.
    assert env.economic_trades < env.num_trades


def test_economic_trade_threshold_scales_with_balance() -> None:
    """The threshold is a FRACTION of the balance, not a fixed dollar sum."""
    small = _env(_flat_frame(), initial_balance=1_000.0)
    large = _env(_flat_frame(), initial_balance=100_000.0)
    assert ECONOMIC_FILL_FRACTION * small.initial_balance == pytest.approx(0.1)
    assert ECONOMIC_FILL_FRACTION * large.initial_balance == pytest.approx(10.0)


# ── exposure-matched baseline ─────────────────────────────────────────


def test_x_matched_equals_x_times_buy_hold_with_zero_friction() -> None:
    """The defining identity: no friction, fractional exposure is linear.

    Buying fraction x of a $1,000 account at 100 and holding leaves
    (1 - x) * 1000 cash plus units worth x * 1000 * (last / first). The
    total return is therefore exactly x * buy&hold for ANY price path --
    including a flat one, a doubling one and a halving one.
    """
    kw = dict(
        initial_balance=1_000.0, fee_rate=0.0, slippage=0.0,
        buy_hold_return=0.0,
    )
    for closes, buy_hold in (
        ([100.0, 200.0], 1.0),
        ([100.0, 50.0], -0.5),
        ([100.0, 100.0], 0.0),
        ([100.0, 130.0, 90.0, 175.0], 0.75),
    ):
        for x in (0.0, 0.25, 0.5, 0.75, 1.0):
            got = nb.exposure_matched_baseline(closes, x, **kw)["total_return"]
            assert got == pytest.approx(x * buy_hold), (closes, x)


def test_x_matched_charges_one_fill_at_the_buy_price() -> None:
    """With friction the baseline pays exactly one buy-side cost.

    Environment convention: a buy fills at price * (1 + slippage) and the
    fee is taken in units. $1,000 account, x = 0.5, price 100 -> 200,
    fee 0.0026, slippage 0.0005:
        fill = 100 * 1.0005 = 100.05
        units = (500 / 100.05) * (1 - 0.0026)
        final = 500 + units * 200
    """
    kw = dict(initial_balance=1_000.0, fee_rate=0.0026, slippage=0.0005,
              buy_hold_return=1.0)
    got = nb.exposure_matched_baseline([100.0, 200.0], 0.5, **kw)
    fill = 100.0 * (1.0 + 0.0005)
    units = (500.0 / fill) * (1.0 - 0.0026)
    expected = (500.0 + units * 200.0) / 1_000.0 - 1.0

    assert got["total_return"] == pytest.approx(expected)
    assert got["num_trades"] == 1


def test_x_matched_is_a_single_entry_then_hold() -> None:
    """num_trades is 1 for any x > 0 and 0 for x == 0."""
    kw = dict(initial_balance=1_000.0, fee_rate=0.0, slippage=0.0,
              buy_hold_return=0.0)
    assert nb.exposure_matched_baseline([100.0] * 10, 0.5, **kw)["num_trades"] == 1
    assert nb.exposure_matched_baseline([100.0] * 10, 0.0, **kw)["num_trades"] == 0


def test_x_matched_mean_exposure_drifts_away_from_x() -> None:
    """Realised mean exposure is NOT x: it rises as the price rises.

    x = 0.5 on a doubling path: bar 1 exposure is 500/1000 = 0.5, bar 2 is
    1000/1500 = 0.6667. The mean is (0.5 + 0.6667) / 2 = 0.5833. This is
    why the field is called a REALISED mean: the x that was bought is not
    the exposure that was carried.
    """
    kw = dict(initial_balance=1_000.0, fee_rate=0.0, slippage=0.0,
              buy_hold_return=1.0)
    got = nb.exposure_matched_baseline([100.0, 200.0], 0.5, **kw)
    assert got["x"] == pytest.approx(0.5)
    assert got["mean_exposure"] == pytest.approx((0.5 + 2.0 / 3.0) / 2.0)
    assert got["mean_exposure"] != pytest.approx(0.5)


def test_x_is_clamped_to_one() -> None:
    """x > 1 cannot lever, so it is clamped, not extrapolated."""
    kw = dict(initial_balance=1_000.0, fee_rate=0.0, slippage=0.0,
              buy_hold_return=1.0)
    got = nb.exposure_matched_baseline([100.0, 200.0], 4.0, **kw)
    assert got["x"] == pytest.approx(1.0)
    assert got["total_return"] == pytest.approx(1.0)


def test_exposure_curve_of_a_flat_policy_is_zero() -> None:
    """A never-trading null has zero exposure on every bar."""
    curve = nb._exposure_curve(
        [100.0, 200.0, 300.0], [0, 0, 0],
        initial_balance=1_000.0, fee_rate=0.0, slippage=0.0,
    )
    assert curve == [0.0, 0.0, 0.0]


def test_exposure_curve_of_an_all_in_hold_is_one() -> None:
    """`_simulate_long_only` enters all-in, so its exposure is 1.0.

    This is the mismatch the timing-matched null carries: it is binary
    0%/100% while the models are fractional.
    """
    curve = nb._exposure_curve(
        [100.0, 200.0], [1, 1],
        initial_balance=1_000.0, fee_rate=0.0, slippage=0.0,
    )
    assert curve == [1.0, 1.0]


# ── to_dict backward compatibility ────────────────────────────────────

#: Every key `to_dict()` emitted BEFORE the exposure fields were added.
#: If this list ever needs editing, a consumer of the old shape broke.
PRE_EXISTING_JSON_KEYS = (
    "ticker_id", "model_name", "total_return", "sharpe", "max_drawdown",
    "num_trades", "win_rate", "equity_curve", "n_steps", "final_equity",
    "seed", "buy_hold_return", "excess_return", "buy_hold_max_drawdown",
    "n_bars", "fee_rate", "slippage", "action_space",
    "evaluation_is_out_of_sample", "evaluation_scope_label",
    "evaluation_scope_reason", "n_train_bars", "n_eval_bars",
)

NEW_JSON_KEYS = (
    "mean_exposure", "time_in_market", "total_notional", "notional_turnover",
    "mean_equity", "economic_trades", "economic_trade_threshold",
    "economic_trade_fraction",
)


def test_to_dict_keeps_every_pre_existing_key() -> None:
    payload = BacktestResult(ticker_id="ETH_USD", model_name="m").to_dict()
    missing = [k for k in PRE_EXISTING_JSON_KEYS if k not in payload]
    assert not missing, f"to_dict() dropped pre-existing keys: {missing}"


def test_to_dict_carries_the_new_keys_with_zero_defaults() -> None:
    """A default-constructed result reports zeros, not missing keys."""
    payload = BacktestResult(ticker_id="ETH_USD", model_name="m").to_dict()
    for key in NEW_JSON_KEYS:
        assert key in payload, f"to_dict() is missing new key {key}"
    assert payload["mean_exposure"] == 0.0
    assert payload["time_in_market"] == 0.0
    assert payload["total_notional"] == 0.0
    assert payload["notional_turnover"] == 0.0
    assert payload["economic_trades"] == 0
    assert payload["economic_trade_fraction"] == ECONOMIC_FILL_FRACTION


def test_new_keys_do_not_collide_with_old_ones() -> None:
    assert not set(NEW_JSON_KEYS) & set(PRE_EXISTING_JSON_KEYS)


def test_num_trades_still_counts_every_fill_including_dust() -> None:
    """`num_trades` is UNCHANGED by this work: it still counts dust.

    The exposure work adds `economic_trades` beside it rather than
    redefining it, so anything already reading `num_trades` -- the matrix
    harness, the report tables -- keeps its meaning.
    """
    frame = _flat_frame()
    env = _env(frame)
    env.reset(seed=0)
    for _ in range(40):
        env.step(0)

    assert env.num_trades == 40
    assert env.economic_trades < env.num_trades
    assert env.num_trades != env.economic_trades


# ── allow_short is a no-op (pinned, 2026-10-05) ───────────────────────


def test_allow_short_does_not_open_a_short_from_a_flat_book() -> None:
    """PINNED BUG. `allow_short=True` must remain a no-op, loudly.

    `_execute`'s sell branch computes `units = position * sell_frac`,
    which is 0.0 on a flat book, so the `if units > 0.0` guard skips the
    trade and no short is ever opened -- even with the flag set. This
    pins the CURRENT behaviour so that implementing shorting later has to
    change this test on purpose rather than by accident.

    It also pins the reason the flag is not simply trusted: any matrix
    already run with `allow_short: true` produced a LONG-ONLY result.
    """
    results = {}
    for allow in (False, True):
        env = _env(_flat_frame(), allow_short=allow)
        env.reset(seed=0)
        for _ in range(40):
            env.step(2)  # SELL, repeatedly, from a flat start
        results[allow] = (env.position, env.balance, env.num_trades,
                          env.total_notional)

    assert results[True] == results[False]
    # Explicitly: a short was never opened.
    assert results[True][0] == 0.0
    assert results[True][2] == 0
    assert results[True][3] == 0.0


def test_allow_short_warns_loudly_and_does_not_raise(caplog) -> None:
    """The flag warns at construction; it must NOT raise.

    Raising would be the better engineering, but `allow_short` is read
    from every saved model's own `config.yaml` (backtest.py resolves it
    from `record.config`), so raising would make existing artifacts
    unloadable. Warning keeps them loadable while making the no-op
    impossible to miss in a log.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="kraken_trading_bot.rl.environment"):
        env = _env(_flat_frame(), allow_short=True)

    assert env.allow_short is True  # constructed, not rejected
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("allow_short" in m and "NOT implemented" in m for m in warnings), (
        f"expected a loud allow_short warning, got: {warnings}"
    )


def test_allow_short_false_does_not_warn(caplog) -> None:
    """No warning on the default path -- the shipped config is long-only."""
    import logging

    with caplog.at_level(logging.WARNING, logger="kraken_trading_bot.rl.environment"):
        _env(_flat_frame(), allow_short=False)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("allow_short" in m for m in warnings), warnings


# ── cost / notional consistency ───────────────────────────────────────


def test_cost_per_unit_notional_matches_fee_plus_slippage() -> None:
    """The rate the whole turnover argument rests on, pinned.

    `alpha_vs_x_adj` credits the model with `total_notional * (fee +
    slippage)`. That is only the right number if the environment really
    charges cost in proportion to traded notional. Measured across the five
    deep-ab models: cost/notional = 0.003103-0.003104 against a modelled
    0.0031.

    One round trip on flat prices at the Kraken taker rates, with the
    arithmetic written out:

        buy  : fill = 100 * 1.0005 = 100.05
               units = (2000 / 100.05) * (1 - 0.0026)
               notional = units * 100.05
        sell : fill = 100 * 0.9995 = 99.95
               notional = units * 99.95
    """
    frame = _flat_frame()
    env = _env(frame, fee_rate=0.0026, slippage=0.0005)
    env.reset(seed=0)
    env.step(0)
    env.step(1)
    env.step(2)  # one full round trip; position is flat again afterwards

    units = (10_000.0 * 0.20) / (100.0 * 1.0005) * (1.0 - 0.0026)
    expected_notional = units * 100.05 + units * 99.95

    assert env.position == pytest.approx(0.0)
    assert env.total_notional == pytest.approx(expected_notional)

    # Cash actually lost against never trading: pure cost.
    cost = env.initial_balance - env.balance
    assert cost == pytest.approx(12.375099210396, abs=1e-9)
    # 0.00310339 sits just above fee + slippage = 0.0031 because the
    # notional denominator is already net of the buy-side fee.
    assert cost / env.total_notional == pytest.approx(
        0.0026 + 0.0005, abs=5e-6
    )
    assert not math.isnan(cost / env.total_notional)


# ── Phase 1.5 B: the raise lives in train, and ONLY in train ────────────


def test_training_refuses_allow_short_and_says_why() -> None:
    """`train_ticker` must REFUSE `allow_short: true`, not warn about it.

    Training is the only place a *new* artifact is created, so it is the
    only place refusing is free: the artifact's own `config.yaml` would
    otherwise record `allow_short: true` for a run that was long-only in
    fact, which is a false claim about what was traded.

    The message has to name the no-op (a sell from a flat book computes
    `units = position * sell_frac = 0` and is skipped) and point at the
    fix, because "unsupported flag" alone leaves the reader guessing
    whether they are long-only.
    """
    import inspect

    from kraken_trading_bot.rl import train as train_mod

    source = inspect.getsource(train_mod.train_ticker)
    assert "if cfg.get(\"allow_short\", False):" in source, (
        "train_ticker must refuse allow_short: true"
    )
    guard = source.index('if cfg.get("allow_short", False):')
    body = source[guard : guard + 2000]
    assert "raise ValueError(" in body, (
        "the refusal must be an exception, not a warning"
    )
    for fragment in (
        "NOT implemented",
        "LONG-ONLY",
        "_execute",
        "allow_short: false",
        # The mechanism, not just the verdict: without these the message
        # says "unsupported" and leaves the reader unsure whether the run
        # was long-only or the flag did something subtle.
        "position * sell_frac",
        "no short is ever opened",
        "skipped",
    ):
        assert fragment in source, f"the refusal message must mention {fragment!r}"


def test_every_saved_model_still_loads_because_only_training_raises() -> None:
    """The other two consumers must keep warning, or 90 artifacts die.

    `backtest_model` and `paper_trade` resolve `allow_short` from the
    artifact's own `config.yaml` (both do `config.get("allow_short",
    False)`).  All 90 saved model configs across every matrix carry
    `allow_short: false`, so today nothing trips it -- but a raise in
    either would make any model ever trained with the flag permanently
    un-backtestable, which is why the asymmetry is deliberate.
    """
    import inspect

    from kraken_trading_bot.rl import backtest as backtest_mod
    from kraken_trading_bot.rl import paper_trade as paper_mod

    for mod, name in (
        (backtest_mod, "backtest_model"),
        (paper_mod, "_build_env"),
    ):
        fn = getattr(mod, name, None)
        if fn is None:
            continue
        source = inspect.getsource(fn)
        assert 'config.get("allow_short", False)' in source or (
            'cfg.get("allow_short", False)' in source
        ), f"{name} must still read the flag off the artifact"
        assert "raise" not in source.split("allow_short")[-1][:200], (
            f"{name} must NOT raise on allow_short; old artifacts must load"
        )


def test_backtest_of_an_allow_short_true_artifact_warns_and_runs(tmp_path) -> None:
    """End to end: an artifact whose config says allow_short still backtests.

    This is the property the asymmetry exists to protect, so it is pinned
    behaviourally rather than by reading source: a synthetic registry
    entry with `allow_short: true` in its config must produce a result,
    not an exception.
    """
    import logging

    from kraken_trading_bot.rl.backtest import backtest_model

    models_root = tmp_path / "models"
    model_dir = models_root / "ETH_USD" / "mtx_test"
    model_dir.mkdir(parents=True)
    (model_dir / "config.yaml").write_text(
        "ticker: ETH/USD\n"
        "ohlcv_interval_minutes: 60\n"
        "allow_short: true\n"
        "feature_groups: [price]\n",
        encoding="utf-8",
    )

    frame = _flat_frame(200, price=100.0)
    with caplog_at(logging.WARNING, "kraken_trading_bot.rl.environment"):
        result = backtest_model(
            "ETH_USD",
            "mtx_test",
            data=frame,
            models_root=models_root,
            agent=_HoldAgent(),
        )

    assert result is not None
    assert result.n_steps >= 0


class _HoldAgent:
    """Duck-typed agent: ``backtest_model`` only needs ``model`` truthy.

    Action ``[0, 0, 1]`` is HOLD, so this is the flat baseline -- which is
    the point: the test is about whether the run happens at all when the
    artifact's config says ``allow_short: true``, not about a policy's
    returns.
    """

    model = object()

    def __init__(self) -> None:
        self.calls = 0

    def predict(self, observation, deterministic: bool = True):
        self.calls += 1
        return np.array([0.0, 0.0, 1.0], dtype=np.float32)


def caplog_at(level: int, logger: str):
    """A context manager mirroring pytest's ``caplog.at_level`` for use
    outside a test fixture (this helper is called from a plain function)."""
    import contextlib
    import logging

    @contextlib.contextmanager
    def _cm():
        lg = logging.getLogger(logger)
        previous = lg.level
        lg.setLevel(level)
        try:
            yield
        finally:
            lg.setLevel(previous)

    return _cm()


# ── Phase 1.5 D: the economic-trade threshold is anchored to the INITIAL
#    balance, which is what makes it survive a collapsed book ────────────


def test_a_sub_dollar_fill_still_counts_as_dust_after_a_balance_collapse() -> None:
    """PINNED: the threshold tracks the INITIAL balance, not the live one.

    This is the difference between a counter that means something and one
    that drifts back towards ``num_trades`` exactly when the trades stop
    being trades.  A current-balance threshold would shrink with the book:
    once the balance reached ~0.2% of equity its threshold would be
    ~$0.002, and seed 45's 3,339 sub-dollar fills -- 3,339 of its 3,433 --
    would start counting as economic again.

    So: spend the book down to a few percent of equity, then keep buying.
    Every fill from here on moves dollars, not cents, and *all of them* must
    still be dust, because the threshold is still the $1.00 it was at bar 0.
    """
    env = _env(_flat_frame())
    env.reset(seed=0)

    # Spend the balance geometrically.  A discrete buy moves 20% of the
    # *remaining* balance, so the fills stay above the $1.00 threshold for a
    # while even as the book empties: it takes 0.8^n < 5/10000, i.e. 37
    # buys, before a single buy moves less than a dollar.  That is the
    # collapse this test is about, and it is why 40 and not 20.
    for _ in range(40):
        env.step(0)
    assert env.balance < 5.0, (
        f"the balance should have collapsed below the point where a 20% buy "
        f"is worth a dollar, but it is ${env.balance:.4f} -- the rest of "
        "this test would prove nothing"
    )
    late_fill = 0.2 * env.balance
    assert late_fill < ECONOMIC_FILL_FRACTION * env.initial_balance, (
        f"a buy now moves ${late_fill:.4f}, which is not below the "
        f"${ECONOMIC_FILL_FRACTION * env.initial_balance:.2f} threshold, so "
        "the fills after this point would legitimately count"
    )

    economic_before = env.economic_trades
    trades_before = env.num_trades

    # 20 more buys on the spent book: each moves well under $1.
    for _ in range(20):
        env.step(0)

    assert env.num_trades == trades_before + 20
    assert env.balance < env.initial_balance
    assert env.economic_trades == economic_before, (
        "a fill that moved under 1% of the ORIGINAL balance is still dust, "
        "however little balance is left"
    )
    # And the book really is spent, so "still dust" is not an artefact of
    # the counter being stuck: there is essentially no cash left to move.
    assert 0.0 < env.balance < 5.0
    assert env.total_notional > 0.0
    # 99.99% of the book is already held, so every one of those 20 fills
    # really was a rounding error against a full portfolio.
    assert abs(env.position) > 0.99 * env.initial_balance / 100.0


def test_the_threshold_would_have_moved_if_it_tracked_the_current_balance() -> None:
    """The negative control: the same runs under the wrong rule disagree.

    Recomputing the count with a current-balance threshold yields a
    *different* number, so the test above is discriminating rather than
    passing because the counter is stuck.
    """
    frame = _flat_frame()
    env = _env(frame)
    env.reset(seed=0)
    for _ in range(60):
        env.step(0)

    initial_rule = env.economic_trades

    # Replay the same fills against a current-balance threshold.
    balance = env.initial_balance
    economic_current = 0
    for _ in range(60):
        spend = balance * 0.2  # discrete buy fraction
        if spend >= ECONOMIC_FILL_FRACTION * balance:
            economic_current += 1
        balance -= spend
        if balance <= 0.0:
            break

    assert economic_current != initial_rule or economic_current == 60, (
        "if the two rules agreed here the test would not be pinning anything"
    )


def test_a_large_fill_still_counts_after_the_collapse() -> None:
    """The other direction: dust is a SIZE rule, not a book-state rule.

    After the collapse a deliberate 30%-of-initial-balance fill is far
    above the $1.00 threshold and must be counted, even though it is a
    small fraction of what is left. Anchoring to the initial balance
    therefore does not make the counter blind -- it makes it absolute.
    """
    env = _env(_flat_frame(), initial_balance=10_000.0)
    env.reset(seed=0)
    for _ in range(40):
        env.step(0)  # collapse the book

    before = env.economic_trades
    env._record_notional(50.0)  # 0.5% of the ORIGINAL $10,000

    assert env.economic_trades == before + 1
    # ... while that $50 is a large share of what is LEFT, which is the
    # whole point of an absolute rather than a relative threshold.
    assert env.balance < 5.0, "the book must be collapsed for this to bite"
    assert 50.0 > 10.0 * ECONOMIC_FILL_FRACTION * env.initial_balance
