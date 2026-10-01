"""Tests for the validity plumbing: benchmark, friction and data window.

Four measured problems this file pins down, each of which previously made
a "is this model useful?" verdict unavailable:

1. ``backtest`` had no ``--config``, so ``fee_rate``/``slippage`` (both
   ``0.0`` in ``configs/default.yaml``) could never be applied and every
   backtest was frictionless.
2. There was no benchmark, so a ``+4%`` return had no reference point.
3. ``since``/``until`` were dropped, so ``train`` and ``backtest`` each
   fetched FRESH data — two fetches of the same pair have been measured
   to disagree 16% on ``rsi_24``'s fitted std, so a matrix compared data
   instead of configs.
4. A single run is not a measurement, and a run can complete with no
   error while replaying one bar of 721.  ``n_bars`` is the magnitude
   guard for that, and the sparse-funding test below pins the fix
   (``b45a7a4``) it reads.

Everything here is offline and hermetic: no network, no training, no
``models/`` writes.  ``backtest_model`` accepts a caller-supplied frame
and a duck-typed agent, so the whole evaluator is driven without PPO.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pytest
import yaml

from kraken_trading_bot.cli import _build_parser, main
from kraken_trading_bot.rl import (
    ActionSpaceMismatchError,
    BacktestResult,
    DataWindow,
    FeaturePipeline,
    ModelRecord,
    TradingEnvironment,
    backtest_model,
    clip_to_window,
    evaluation_frame,
    resolve_data_window,
    training_frame,
)
from kraken_trading_bot.rl.backtest import _benchmark
from kraken_trading_bot.rl.data import add_derived_ohlcv_features, merge_extra_features
from kraken_trading_bot.rl.data_window import split_index
from kraken_trading_bot.rl.train import train_ticker

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
# The builtin (no-pipeline) warm-up boundary: the 20-bar rolling window
# plus a 5-bar return.  Pinned so a change to the warm-up rule has to be
# deliberate — the buy-and-hold hand computations below depend on it.
_BUILTIN_WARMUP = 24

# A linear ramp: close[i] == 100 + i, so buy-and-hold over a known bar
# range is arithmetic a reader can check by hand rather than a number the
# code computes for itself.
_RAMP_BASE = 100.0


def _ramp_ohlcv(n: int = 200, start: str = "2024-01-01") -> pd.DataFrame:
    """OHLCV frame whose close is exactly ``100 + i`` for bar ``i``.

    Deliberately degenerate (constant returns, no wicks): it makes
    ``buy_hold_return`` an exactly hand-computable quantity, which is the
    only way a benchmark assertion is worth anything.
    """
    idx = pd.date_range(start, periods=n, freq="h", tz="UTC")
    close = _RAMP_BASE + np.arange(n, dtype=float)
    open_ = close - 1.0
    return pd.DataFrame(
        {
            "open": open_,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "vwap": close,
            "volume": np.full(n, 100.0),
            "count": np.full(n, 50.0),
        },
        index=idx,
    )


def _walk_ohlcv(n: int = 240, seed: int = 11) -> pd.DataFrame:
    """A rising random walk (so buy-and-hold is clearly positive)."""
    rng = np.random.default_rng(seed)
    close = 2000.0 * np.exp(np.cumsum(rng.normal(0.001, 0.01, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.002,
            "low": np.minimum(open_, close) * 0.998,
            "close": close,
            "vwap": close,
            "volume": rng.uniform(50.0, 200.0, n),
            "count": rng.integers(10, 100, n).astype(float),
        },
        index=idx,
    )


class _BuyEveryBar:
    """Duck-typed agent that buys on every bar (``model`` non-None).

    ``backtest_model`` only requires that ``agent.model`` is truthy before
    replaying, so this stands in for a trained policy and makes the
    friction tests *deterministic*: a PPO agent might not trade at all in
    150 timesteps, and a fee that changes nothing proves nothing.
    """

    model = object()

    def __init__(self, buy: float = 1.0) -> None:
        self.buy = buy
        self.calls = 0

    def predict(self, observation, deterministic: bool = True):
        self.calls += 1
        return np.array([self.buy, 0.0, 0.0], dtype=np.float32)


class _HoldEveryBar(_BuyEveryBar):
    """Never trades — the flat baseline a friction test is compared against."""

    def predict(self, observation, deterministic: bool = True):
        self.calls += 1
        return np.array([0.0, 0.0, 1.0], dtype=np.float32)


class _DiscreteHold:
    """Duck-typed agent for a ``discrete`` environment (action 1 = hold)."""

    model = object()

    def predict(self, observation, deterministic: bool = True):
        return 1


def _register(root: Path, config: dict | None = None, name: str = "ppo_v") -> None:
    """Register a config.yaml-only model (no model.zip / normalization)."""
    from kraken_trading_bot.rl.registry import register_model

    register_model(
        "ETH_USD",
        name,
        {"ticker": "ETH/USD", "action_space": "continuous", **(config or {})},
        root=root,
    )


def _register_with_normalization(
    root: Path, frame: pd.DataFrame, config: dict | None = None, name: str = "ppo_v"
) -> None:
    """Register a model whose normalization.npz was fitted on ``frame``.

    This is what puts ``backtest_model`` on the *pipeline* path — the one
    where a sparsely covered exogenous column can move the warm-up
    boundary — without paying for a PPO run.  The stats are fitted on the
    same frame the backtest replays, so the width guard genuinely
    compares an artifact against a live frame.
    """
    from kraken_trading_bot.rl.registry import model_dir, register_model

    features = FeaturePipeline()
    features.fit(frame, ticker_id="ETH_USD")
    directory = model_dir("ETH_USD", name, root=root)
    features.save_normalization("ETH_USD", directory / "normalization.npz")
    register_model(
        "ETH_USD",
        name,
        {
            "ticker": "ETH/USD",
            "action_space": "continuous",
            "feature_windows": [1, 4, 24],
            "feature_groups": [
                "price",
                "technical",
                "volume",
                "microstructure",
                "signals",
            ],
            **(config or {}),
        },
        root=root,
    )


def _write_config(tmp_path: Path, body: dict, name: str = "run.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


# ===========================================================================
# 1. data_window: resolution, clipping, split
# ===========================================================================
def test_unpinned_window_is_inert_and_returns_the_same_object():
    """No bounds -> the frame is returned untouched, by identity.

    Identity rather than equality: an unpinned run must not even build a
    masked copy, or "byte-identical to today" becomes a promise about
    values rather than about the code path.
    """
    frame = _ramp_ohlcv(50)
    for config in (None, {}, {"data_window": None}, {"data_window": {}}):
        window = resolve_data_window(config)
        assert window.is_pinned is False
        assert clip_to_window(frame, window) is frame
        assert training_frame(frame, window) is frame
        assert evaluation_frame(frame, window) is frame


def test_unpinned_window_ignores_eval_split():
    """`eval_split` is inert until a bound is pinned.

    Shipping ``eval_split: 0.7`` as the default while applying it to an
    open-ended window would silently train every existing run on 70% of
    the bars it used to get.
    """
    window = resolve_data_window({"data_window": {"eval_split": 0.7}})
    assert window.eval_split == pytest.approx(0.7)
    assert window.is_pinned is False
    assert window.has_split is False
    frame = _ramp_ohlcv(100)
    assert evaluation_frame(frame, window) is frame
    assert training_frame(frame, window) is frame


def test_clip_is_inclusive_on_since_and_exclusive_on_until():
    frame = _ramp_ohlcv(100)
    window = resolve_data_window(
        {
            "data_window": {
                "since": frame.index[10].isoformat(),
                "until": frame.index[20].isoformat(),
            }
        }
    )
    assert window.is_pinned is True
    clipped = clip_to_window(frame, window)
    # 10 inclusive .. 19 inclusive.
    assert len(clipped) == 10
    assert clipped.index[0] == frame.index[10]
    assert clipped.index[-1] == frame.index[19]


def test_clip_accepts_only_one_bound():
    frame = _ramp_ohlcv(100)
    lower = clip_to_window(
        frame, resolve_data_window({"data_window": {"since": frame.index[80]}})
    )
    upper = clip_to_window(
        frame, resolve_data_window({"data_window": {"until": frame.index[20]}})
    )
    assert len(lower) == 20 and lower.index[0] == frame.index[80]
    assert len(upper) == 20 and upper.index[-1] == frame.index[19]


def test_naive_bounds_are_read_as_utc():
    frame = _ramp_ohlcv(100)
    naive = resolve_data_window({"data_window": {"since": "2024-01-01T00:00:00"}})
    explicit = resolve_data_window(
        {"data_window": {"since": "2024-01-01T00:00:00Z"}}
    )
    assert naive.since == explicit.since
    assert naive.since.tzinfo is not None
    assert len(clip_to_window(frame, naive)) == 100


def test_unparseable_bound_raises_rather_than_widening_the_window():
    """A mis-typed bound must not silently widen the window.

    Widening is the failure mode that matters: an unparsed ``since``
    would turn a pinned comparison into another fresh fetch.
    """
    with pytest.raises(ValueError, match="since"):
        resolve_data_window({"data_window": {"since": "not-a-date"}})


def test_until_at_or_before_since_raises():
    frame = _ramp_ohlcv(100)
    with pytest.raises(ValueError, match="not after since"):
        resolve_data_window(
            {
                "data_window": {
                    "since": frame.index[20].isoformat(),
                    "until": frame.index[20].isoformat(),
                }
            }
        )


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5, "half", True])
def test_eval_split_out_of_range_raises(bad):
    with pytest.raises(ValueError, match="eval_split"):
        resolve_data_window(
            {"data_window": {"since": "2024-01-01T00:00:00Z", "eval_split": bad}}
        )


def test_eval_split_one_means_no_split():
    window = resolve_data_window(
        {"data_window": {"since": "2024-01-01T00:00:00Z", "eval_split": 1.0}}
    )
    assert window.is_pinned is True
    assert window.has_split is False
    frame = _ramp_ohlcv(100)
    pinned = clip_to_window(frame, window)
    evaluation = evaluation_frame(frame, window)
    assert list(evaluation.index) == list(pinned.index)
    assert len(evaluation) == len(pinned) == 100


def test_split_slices_are_disjoint_and_reconstruct_the_window():
    """train[:cut] and eval[cut:] must partition the clipped window.

    Anything else (overlap, or a gap) would mean the reported
    "out-of-sample" return was measured over bars the model saw.
    """
    frame = _walk_ohlcv(240)
    window = resolve_data_window(
        {
            "data_window": {
                "since": frame.index[20].isoformat(),
                "until": frame.index[220].isoformat(),
                "eval_split": 0.7,
            }
        }
    )
    pinned = clip_to_window(frame, window)
    train = training_frame(frame, window)
    evaluation = evaluation_frame(frame, window)

    assert len(pinned) == 200
    assert len(train) == 140
    assert len(evaluation) == 60
    assert list(train.index) + list(evaluation.index) == list(pinned.index)
    assert evaluation.index[0] > train.index[-1]
    # And the tail really is the tail: eval ends on the window's last bar.
    assert evaluation.index[-1] == pinned.index[-1]


def test_split_index_keeps_a_bar_on_each_side():
    window = DataWindow(
        since=pd.Timestamp("2024-01-01", tz="UTC"), until=None, eval_split=0.999
    )
    assert split_index(2, window) == 1
    assert split_index(1, window) == 1  # degenerate: no split
    assert split_index(0, window) == 0


# ===========================================================================
# 2. buy-and-hold benchmark and excess return
# ===========================================================================
def test_benchmark_helper_matches_hand_computation():
    """`close[i] = 100 + i`, bars 24..99 -> (199/124) - 1."""
    closes = _ramp_ohlcv(100)["close"].to_numpy()
    expected = (100.0 + 99.0) / (100.0 + 24.0) - 1.0
    total, drawdown = _benchmark(closes, 24, 76)
    assert total == pytest.approx(expected)
    # A monotonic ramp never falls below its peak.
    assert drawdown == pytest.approx(0.0)


def test_buy_hold_return_matches_hand_computed_value(tmp_path):
    """The headline benchmark is arithmetic, not a re-derivation.

    No model, no policy, no fitted stats: the builtin feature path sets
    the warm-up boundary at exactly 24, so the replayed range is
    bars 24..199 of a ``close == 100 + i`` ramp.
    """
    frame = _ramp_ohlcv(200)
    result = backtest_model(
        "ETH_USD",
        "ppo_none",
        data=frame,
        agent=_HoldEveryBar(),
        models_root=tmp_path,
        seed=7,
    )

    assert result.n_bars == 200 - _BUILTIN_WARMUP
    assert result.buy_hold_return == pytest.approx(
        (100.0 + 199.0) / (100.0 + _BUILTIN_WARMUP) - 1.0
    )
    # Holding the asset is what a flat strategy does, so the excess
    # return must be ~0 here — not the strategy "beating" the benchmark.
    assert result.total_return == pytest.approx(0.0, abs=1e-12)
    assert result.excess_return == pytest.approx(
        result.total_return - result.buy_hold_return
    )


def test_buy_hold_uses_the_realized_bar_range_not_the_requested_frame(tmp_path):
    """Benchmarking from the frame's first close would compare two periods.

    The episode warms up on its leading bars, so a benchmark measured
    from bar 0 covers 24 bars the strategy never traded. On a rising walk
    that inflates the reference point and can flip the sign of the excess
    return, which is the one number the verdict rests on.
    """
    frame = _walk_ohlcv(240, seed=3)
    result = backtest_model(
        "ETH_USD", "ppo_none", data=frame, agent=_HoldEveryBar(), models_root=tmp_path
    )
    closes = frame["close"].to_numpy(dtype=float)
    realized = closes[_BUILTIN_WARMUP : _BUILTIN_WARMUP + result.n_bars]
    naive = closes[: len(realized)]

    assert result.buy_hold_return == pytest.approx(realized[-1] / realized[0] - 1.0)
    assert result.buy_hold_return != pytest.approx(naive[-1] / naive[0] - 1.0)


def test_excess_return_is_total_minus_buy_hold(tmp_path):
    frame = _walk_ohlcv(240, seed=17)
    result = backtest_model(
        "ETH_USD", "ppo_none", data=frame, agent=_BuyEveryBar(), models_root=tmp_path
    )
    assert result.excess_return == pytest.approx(
        result.total_return - result.buy_hold_return
    )
    # The whole point: a strategy that buys a rising asset every bar
    # returns *less* than the asset did, and the excess says so.
    assert result.total_return < result.buy_hold_return
    assert result.excess_return < 0.0


def test_buy_hold_drawdown_is_positive_on_a_falling_walk(tmp_path):
    rng = np.random.default_rng(5)
    close = 2000.0 * np.exp(np.cumsum(rng.normal(-0.001, 0.01, 240)))
    frame = _walk_ohlcv(240, seed=1)
    frame["close"] = close
    frame["open"] = np.concatenate([[close[0]], close[:-1]])
    frame["high"] = np.maximum(frame["open"], frame["close"]) * 1.002
    frame["low"] = np.minimum(frame["open"], frame["close"]) * 0.998
    frame["vwap"] = frame["close"]

    result = backtest_model(
        "ETH_USD", "ppo_none", data=frame, agent=_HoldEveryBar(), models_root=tmp_path
    )
    assert result.buy_hold_max_drawdown > 0.0
    assert 0.0 <= result.buy_hold_max_drawdown <= 1.0


def test_result_dict_carries_every_new_field(tmp_path):
    frame = _walk_ohlcv(120)
    result = backtest_model(
        "ETH_USD", "ppo_none", data=frame, agent=_BuyEveryBar(), models_root=tmp_path
    )
    d = result.to_dict()
    for key in (
        "buy_hold_return",
        "excess_return",
        "buy_hold_max_drawdown",
        "n_bars",
        "fee_rate",
        "slippage",
        "action_space",
    ):
        assert key in d, key
    assert d["n_bars"] == result.n_steps


def test_degenerate_replay_benchmarks_to_zero():
    """One replayed bar has no buy-and-hold span to measure."""
    closes = _ramp_ohlcv(100)["close"].to_numpy()
    assert _benchmark(closes, 24, 1) == (0.0, 0.0)
    assert _benchmark(closes, 24, 0) == (0.0, 0.0)


# ===========================================================================
# 3. friction: fee_rate / slippage actually applied
# ===========================================================================
def test_fee_rate_reduces_return(tmp_path):
    """Direction, not an exact number: a cost can only subtract.

    Asserting a precise value would pin the fill arithmetic, not the
    plumbing; what must hold is that the cost is actually reaching the
    environment and moving the outcome.
    """
    frame = _walk_ohlcv(240, seed=23)
    free = backtest_model(
        "ETH_USD",
        "ppo_none",
        data=frame,
        agent=_BuyEveryBar(),
        models_root=tmp_path,
        env_kwargs={"fee_rate": 0.0},
    )
    costed = backtest_model(
        "ETH_USD",
        "ppo_none",
        data=frame,
        agent=_BuyEveryBar(),
        models_root=tmp_path,
        env_kwargs={"fee_rate": 0.0026},
    )
    assert free.num_trades > 0, "the fixture must actually trade"
    assert free.fee_rate == 0.0
    assert costed.fee_rate == pytest.approx(0.0026)
    assert costed.total_return < free.total_return


def test_slippage_reduces_return(tmp_path):
    frame = _walk_ohlcv(240, seed=29)
    free = backtest_model(
        "ETH_USD",
        "ppo_none",
        data=frame,
        agent=_BuyEveryBar(),
        models_root=tmp_path,
        env_kwargs={"slippage": 0.0},
    )
    costed = backtest_model(
        "ETH_USD",
        "ppo_none",
        data=frame,
        agent=_BuyEveryBar(),
        models_root=tmp_path,
        env_kwargs={"slippage": 0.005},
    )
    assert free.num_trades > 0
    assert costed.slippage == pytest.approx(0.005)
    assert costed.total_return < free.total_return


def test_zero_cost_on_a_holding_strategy_is_a_no_op(tmp_path):
    """A guard on the tests above: the agent must be trading for them to mean
    anything."""
    frame = _walk_ohlcv(120)
    held = backtest_model(
        "ETH_USD", "ppo_none", data=frame, agent=_HoldEveryBar(), models_root=tmp_path
    )
    assert held.num_trades == 0
    assert held.total_return == pytest.approx(0.0, abs=1e-12)


def test_backtest_config_supplies_friction(tmp_path):
    """The blocker this change exists for: `--config` reaching the env."""
    frame = _walk_ohlcv(240, seed=31)
    config = _write_config(
        tmp_path,
        {
            "fee_rate": 0.004,
            "slippage": 0.001,
            "initial_balance": 5000.0,
            "action_space": "continuous",
            "data_window": {"since": None, "until": None, "eval_split": 0.7},
        },
    )
    costed = backtest_model(
        "ETH_USD",
        "ppo_none",
        data=frame,
        agent=_BuyEveryBar(),
        models_root=tmp_path,
        config_path=config,
    )
    free = backtest_model(
        "ETH_USD",
        "ppo_none",
        data=frame,
        agent=_BuyEveryBar(),
        models_root=tmp_path,
    )

    assert costed.fee_rate == pytest.approx(0.004)
    assert costed.slippage == pytest.approx(0.001)
    # initial_balance came from the run config too, so the curve is
    # scaled: equity is the return applied to 5 000, not to 10 000.
    assert costed.equity_curve[0] == pytest.approx(5000.0)
    assert free.equity_curve[0] == pytest.approx(10_000.0)
    assert costed.total_return < free.total_return


def test_run_config_beats_the_models_own_config(tmp_path):
    """The model's config is what it TRAINED with, not what to evaluate under.

    A matrix row says "evaluate this model under these costs"; if the
    training-time zero could not be overridden, friction would stay
    unreachable — which was the original blocker.
    """
    frame = _walk_ohlcv(200, seed=37)
    _register(tmp_path, {"fee_rate": 0.05, "slippage": 0.02})
    config = _write_config(tmp_path, {"fee_rate": 0.0, "slippage": 0.0})

    result = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=frame,
        agent=_BuyEveryBar(),
        models_root=tmp_path,
        config_path=config,
    )
    assert result.fee_rate == 0.0
    assert result.slippage == 0.0


def test_env_kwargs_beat_the_run_config(tmp_path):
    frame = _walk_ohlcv(200, seed=41)
    _register(tmp_path)
    config = _write_config(tmp_path, {"fee_rate": 0.01})
    result = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=frame,
        agent=_BuyEveryBar(),
        models_root=tmp_path,
        config_path=config,
        env_kwargs={"fee_rate": 0.02},
    )
    assert result.fee_rate == pytest.approx(0.02)


def test_missing_config_path_raises_instead_of_running_frictionless(tmp_path):
    """A typo in --config must not silently fall back to zero costs."""
    frame = _walk_ohlcv(120)
    with pytest.raises(FileNotFoundError, match="Backtest config not found"):
        backtest_model(
            "ETH_USD",
            "ppo_none",
            data=frame,
            agent=_HoldEveryBar(),
            models_root=tmp_path,
            config_path=tmp_path / "typo.yaml",
        )


# ===========================================================================
# 4. action-space guard
# ===========================================================================
def test_action_space_mismatch_is_reported_clearly(tmp_path):
    frame = _walk_ohlcv(120)
    _register(tmp_path, {"action_space": "continuous"})
    with pytest.raises(ActionSpaceMismatchError) as excinfo:
        backtest_model(
            "ETH_USD",
            "ppo_v",
            data=frame,
            agent=_HoldEveryBar(),
            models_root=tmp_path,
            env_kwargs={"action_space": "discrete"},
        )
    message = str(excinfo.value)
    assert "continuous" in message and "discrete" in message
    assert excinfo.value.trained_as == "continuous"
    assert excinfo.value.requested == "discrete"


def test_action_space_mismatch_via_run_config(tmp_path):
    frame = _walk_ohlcv(120)
    _register(tmp_path, {"action_space": "continuous"})
    config = _write_config(tmp_path, {"action_space": "discrete"})
    with pytest.raises(ActionSpaceMismatchError):
        backtest_model(
            "ETH_USD",
            "ppo_v",
            data=frame,
            agent=_HoldEveryBar(),
            models_root=tmp_path,
            config_path=config,
        )


def test_matching_action_space_passes(tmp_path):
    frame = _walk_ohlcv(120)
    _register(tmp_path, {"action_space": "continuous"})
    config = _write_config(tmp_path, {"action_space": "continuous"})
    result = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=frame,
        agent=_HoldEveryBar(),
        models_root=tmp_path,
        config_path=config,
    )
    assert result.action_space == "continuous"


def test_unrecorded_action_space_is_not_a_mismatch(tmp_path):
    """An artifact without provenance is unproven, not wrong.

    Refusing every pre-provenance model would be a louder break than the
    failure this guard prevents.
    """
    frame = _walk_ohlcv(120)
    _register(tmp_path, {"action_space": None})
    result = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=frame,
        agent=_DiscreteHold(),
        models_root=tmp_path,
        env_kwargs={"action_space": "discrete"},
    )
    assert result.action_space == "discrete"


# ===========================================================================
# 5. data window end-to-end through backtest
# ===========================================================================
def test_pinned_window_drops_out_of_window_bars(tmp_path):
    frame = _walk_ohlcv(240, seed=43)
    _register(tmp_path)
    config = _write_config(
        tmp_path,
        {
            "data_window": {
                "since": frame.index[40].isoformat(),
                "until": frame.index[160].isoformat(),
                "eval_split": 1.0,
            }
        },
    )
    result = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=frame,
        agent=_HoldEveryBar(),
        models_root=tmp_path,
        config_path=config,
    )
    # 120 bars pinned, minus the warm-up boundary.
    assert result.n_bars == 120 - _BUILTIN_WARMUP
    # And the benchmark is measured inside that window, not the whole frame.
    pinned_closes = frame["close"].to_numpy()[40:160]
    realized = pinned_closes[_BUILTIN_WARMUP:]
    assert result.buy_hold_return == pytest.approx(realized[-1] / realized[0] - 1.0)


def test_eval_split_gives_an_out_of_sample_backtest_shorter_than_train(tmp_path):
    """The headline requirement of the split: eval is a strict tail."""
    frame = _walk_ohlcv(240, seed=47)
    _register(tmp_path)
    # `until` is exclusive, so the whole frame needs a bound one bar past
    # its last timestamp.
    config = _write_config(
        tmp_path,
        {
            "data_window": {
                "since": frame.index[0].isoformat(),
                "until": (frame.index[-1] + pd.Timedelta(hours=1)).isoformat(),
                "eval_split": 0.7,
            }
        },
    )
    result = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=frame,
        agent=_HoldEveryBar(),
        models_root=tmp_path,
        config_path=config,
    )
    train_bars = 168  # round(240 * 0.7)
    eval_bars = 240 - train_bars
    assert result.n_bars == eval_bars - _BUILTIN_WARMUP
    assert result.n_bars < train_bars

    # The replayed bars are the TAIL of the window: the benchmark is
    # computed from close[168 + 24], i.e. past every training bar.
    closes = frame["close"].to_numpy()
    eval_slice = closes[train_bars:]
    assert result.buy_hold_return == pytest.approx(
        eval_slice[-1] / eval_slice[_BUILTIN_WARMUP] - 1.0
    )


def test_window_with_no_tradable_bar_names_the_knob(tmp_path):
    """An eval slice shorter than the warm-up has no honest result."""
    frame = _walk_ohlcv(240, seed=53)
    _register(tmp_path)
    config = _write_config(
        tmp_path,
        {
            "data_window": {
                "since": frame.index[0].isoformat(),
                "until": frame.index[20].isoformat(),
                "eval_split": 0.1,
            }
        },
    )
    with pytest.raises(ValueError, match="No tradable bar"):
        backtest_model(
            "ETH_USD",
            "ppo_v",
            data=frame,
            agent=_HoldEveryBar(),
            models_root=tmp_path,
            config_path=config,
        )


def test_explicit_data_window_argument_beats_the_config(tmp_path):
    frame = _walk_ohlcv(240, seed=59)
    _register(tmp_path)
    config = _write_config(
        tmp_path,
        {
            "data_window": {
                "since": frame.index[0].isoformat(),
                "until": (frame.index[-1] + pd.Timedelta(hours=1)).isoformat(),
                "eval_split": 1.0,
            }
        },
    )
    pinned = {
        "since": frame.index[0].isoformat(),
        "until": frame.index[120].isoformat(),
        "eval_split": 1.0,
    }
    result = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=frame,
        agent=_HoldEveryBar(),
        models_root=tmp_path,
        config_path=config,
        data_window=pinned,
    )
    assert result.n_bars == 120 - _BUILTIN_WARMUP


# ===========================================================================
# 6. the unpinned default path is unchanged
# ===========================================================================
def test_unpinned_backtest_replays_the_whole_frame(tmp_path, monkeypatch):
    """No --config -> the whole freshly-read frame, exactly as before.

    Pinned by identity and by count: the frame handed to the environment
    is the derived frame, un-clipped, and every bar after the warm-up is
    replayed.
    """
    import kraken_trading_bot.rl.backtest as bt_mod

    frame = _walk_ohlcv(240, seed=61)
    derived = add_derived_ohlcv_features(frame)

    built: list[TradingEnvironment] = []
    real_env_cls = bt_mod.TradingEnvironment

    def spy(*args, **kwargs):
        env = real_env_cls(*args, **kwargs)
        built.append(env)
        return env

    monkeypatch.setattr(bt_mod, "TradingEnvironment", spy)

    result = backtest_model(
        "ETH_USD",
        "ppo_none",
        data=frame,
        agent=_HoldEveryBar(),
        models_root=tmp_path,
        seed=42,
    )

    assert len(built) == 1
    env = built[0]
    assert env.n_bars == len(derived)
    assert env.start_index == _BUILTIN_WARMUP
    assert result.n_bars == len(derived) - _BUILTIN_WARMUP
    assert result.seed == 42
    # Costs stay at their pre-existing zero defaults.
    assert result.fee_rate == 0.0 and result.slippage == 0.0
    assert result.action_space == "continuous"


def test_unpinned_default_config_block_is_inert():
    """The shipped `configs/default.yaml` must not change any run.

    Resolving the real file's block gives an unpinned window, which means
    the two null defaults every existing command relies on still hold.
    """
    shipped = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "configs" / "default.yaml").read_text(
            encoding="utf-8"
        )
    )
    window = resolve_data_window(shipped)
    assert "data_window" in shipped
    assert window.is_pinned is False
    assert window.eval_split == pytest.approx(0.7)
    assert window.has_split is False

    frame = _walk_ohlcv(120)
    assert clip_to_window(frame, window) is frame


# ===========================================================================
# 7. n_bars is the magnitude guard (b45a7a4 must not regress)
# ===========================================================================
_SPARSE_BARS = 721
# The indicator warm-up boundary on these frames, pinned exactly as
# tests/test_rl_environment.py pins it.
_PIPELINE_WARMUP = 24
# Widths at the shipped defaults.
_NULL_FUNDING_WIDTH = 52
_FUNDING_WIDTH = 60


def _funding_ready_ohlcv(n: int = _SPARSE_BARS, seed: int = 5) -> pd.DataFrame:
    """vwap/count present, no funding columns (the shipped frame)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    close = 2000.0 * np.exp(np.cumsum(rng.normal(0.00005, 0.01, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    df = pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.002,
            "low": np.minimum(open_, close) * 0.998,
            "close": close,
            "vwap": close * (1.0 - rng.uniform(0.0, 0.002, n)),
            "volume": rng.uniform(50.0, 200.0, n),
            "count": rng.integers(5, 80, n).astype(float),
        },
        index=idx,
    )
    return add_derived_ohlcv_features(df)


def _one_record_funding_file(tmp_path: Path, index: pd.DatetimeIndex) -> Path:
    """A funding JSONL with a SINGLE record, on the window's last bar.

    This is the shape `kraken-funding-rates pull --append` produces on its
    first pull: funding settles ~8-hourly, so the shipped config produces a
    column that is NaN on all but a handful of bars.
    """
    path = tmp_path / "eth_usd_funding.jsonl"
    record = {
        "spot_pair": "ETH/USD",
        "timestamp": index[-1].strftime("%Y-%m-%dT%H:%M:%S%z"),
        "funding_rate": 0.025276074734625897,
        "funding_rate_prediction": -0.0024999392825,
        "mark_price": 2685.01548089456,
        "index_price": 2684.89,
        "basis": 4.673595363684979e-05,
        "open_interest": 26523.041,
        "bid": 2685.1,
        "ask": 2685.2,
        "vol24h": 37891.356,
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _merge_one_record_funding(tmp_path: Path, frame: pd.DataFrame) -> pd.DataFrame:
    return merge_extra_features(
        frame,
        extra_features_file=str(_one_record_funding_file(tmp_path, frame.index)),
        ticker="ETH/USD",
        max_age_hours=12,  # the value configs/default.yaml ships
        require_ticker=False,
    )


def test_sparse_funding_keeps_n_bars_healthy(tmp_path):
    """`n_bars` is the guard that would have caught the 1-of-721 defect.

    The measured failure this pins: a one-record funding file left `spread`
    NaN on 720 of 721 rows, the environment's start index moved 24 -> 720,
    and backtest replayed ONE bar with no error and a passing width guard.
    Now that `n_bars` is on the result, the magnitude of a run is
    reportable; this asserts the magnitude stays healthy.
    """
    frame = _merge_one_record_funding(tmp_path, _funding_ready_ohlcv())
    _register_with_normalization(tmp_path, frame)

    # Preconditions: this really is the sparse shape on the pipeline path.
    pipeline = FeaturePipeline()
    features = pipeline.compute(frame)
    assert features.shape[1] == _FUNDING_WIDTH
    assert features["spread"].notna().sum() == 1

    result = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=frame,
        agent=_BuyEveryBar(),
        models_root=tmp_path,
        seed=42,
    )

    assert result.n_bars == _SPARSE_BARS - _PIPELINE_WARMUP
    assert result.n_bars > 0.9 * _SPARSE_BARS, (
        f"a one-record funding file left only {result.n_bars} of "
        f"{_SPARSE_BARS} bars replayable"
    )


def test_sparse_funding_does_not_disturb_the_benchmark(tmp_path):
    """The benchmark must span the same healthy window the strategy did.

    If `n_bars` collapsed, `buy_hold_return` would silently become the
    return of a handful of bars and `excess_return` would be meaningless
    with no error anywhere.
    """
    frame = _merge_one_record_funding(tmp_path, _funding_ready_ohlcv())
    _register_with_normalization(tmp_path, frame)
    result = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=frame,
        agent=_HoldEveryBar(),
        models_root=tmp_path,
    )
    closes = frame["close"].to_numpy(dtype=float)
    realized = closes[_PIPELINE_WARMUP : _PIPELINE_WARMUP + result.n_bars]
    assert result.buy_hold_return == pytest.approx(realized[-1] / realized[0] - 1.0)


def test_width_guard_still_fires_and_n_bars_does_not_replace_it(tmp_path):
    """`n_bars` is a magnitude guard, not a replacement for the width guard."""
    from kraken_trading_bot.rl.features import FeatureWidthMismatchError

    frame = _walk_ohlcv(200, seed=67)
    _register_with_normalization(
        tmp_path,
        frame,
        config={"feature_groups": ["price", "technical", "volume", "microstructure"]},
    )
    # Re-fit and re-register at a narrower width than the live frame.
    narrow = FeaturePipeline(feature_groups=["price", "technical"])
    narrow.fit(frame, ticker_id="ETH_USD")
    narrow.save_normalization("ETH_USD", tmp_path / "ETH_USD" / "ppo_v" / "normalization.npz")

    with pytest.raises(FeatureWidthMismatchError):
        backtest_model(
            "ETH_USD",
            "ppo_v",
            data=frame,
            agent=_HoldEveryBar(),
            models_root=tmp_path,
        )


def test_presence_gating_still_yields_a_narrow_frame(tmp_path):
    """No vwap/count and no funding: 49 features, replay still healthy.

    Nothing in the validity plumbing may make a column required.
    """
    rng = np.random.default_rng(71)
    n = 300
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    close = 2000.0 * np.exp(np.cumsum(rng.normal(0.00005, 0.01, n)))
    plain = pd.DataFrame(
        {
            "open": np.concatenate([[close[0]], close[:-1]]),
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "volume": rng.uniform(50.0, 200.0, n),
        },
        index=idx,
    )
    _register_with_normalization(tmp_path, plain)
    env = TradingEnvironment("ETH_USD", plain, feature_pipeline=FeaturePipeline())
    assert env.observation_space.shape[0] == 49

    result = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=plain,
        agent=_HoldEveryBar(),
        models_root=tmp_path,
    )
    assert result.n_bars == n - _PIPELINE_WARMUP


# ===========================================================================
# 8. train-side window + provenance
# ===========================================================================
def test_train_clips_to_the_pinned_window_and_records_provenance(tmp_path):
    """Train takes the leading `eval_split`; the artifact says so.

    Without this, a backtest reading the same config would replay bars
    the normalization stats were fitted on and call the result
    out-of-sample.
    """
    from kraken_trading_bot.rl.data import candles_to_dataframe

    n = 240
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    rng = np.random.default_rng(73)
    close = 2000.0 * np.exp(np.cumsum(rng.normal(0.00005, 0.01, n)))

    class OneShotManager:
        def ohlc(self, pair, interval, since=None):
            return _candles(idx, close), 0

    config = _write_config(
        tmp_path,
        {
            "ticker": "ETH/USD",
            "action_space": "continuous",
            "reward": {"mode": "pnl"},
            "feature_windows": [1, 4, 24],
            "feature_groups": [
                "price",
                "technical",
                "volume",
                "microstructure",
                "signals",
            ],
            "market_data_store": None,
            "extra_features_file": None,
            "social_features_file": None,
            "data_window": {
                "since": idx[20].isoformat(),
                "until": idx[220].isoformat(),
                "eval_split": 0.75,
            },
        },
        name="train.yaml",
    )

    record = train_ticker(
        "ETH_USD",
        "ppo_win",
        config_path=config,
        manager=OneShotManager(),
        pages=1,
        total_timesteps=120,
        seed=3,
        models_root=tmp_path,
    )

    pinned = 200
    assert record.config["n_bars"] == int(round(pinned * 0.75))
    # The window it was trained on travels with the artifact, so a report
    # can read back what it saw instead of re-deriving it.
    assert record.config["data_window"]["since"] == idx[20].isoformat()
    assert record.config["data_window"]["until"] == idx[220].isoformat()
    assert record.config["n_features"] > 0


def _candles(idx: pd.DatetimeIndex, close: np.ndarray):
    """Flat candles matching a frame's index/closes."""
    from kraken_api.models import Candle

    return [
        Candle(
            pair="ETH/USD",
            time=int(ts.timestamp()),
            open=str(float(close[i])),
            high=str(float(close[i])),
            low=str(float(close[i])),
            close=str(float(close[i])),
            vwap="",
            volume="100.0",
            count=5,
        )
        for i, ts in enumerate(idx)
    ]


def test_unpinned_training_uses_the_whole_frame(tmp_path):
    """The default path is untouched: whole frame, whole episode."""
    from kraken_trading_bot.rl.data import prepare_episode

    n = 200
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    close = 2000.0 * np.exp(np.cumsum(np.random.default_rng(79).normal(0.0, 0.01, n)))
    frame = pd.DataFrame(
        {
            "open": np.concatenate([[close[0]], close[:-1]]),
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "volume": np.full(n, 100.0),
        },
        index=idx,
    )
    window = resolve_data_window({"data_window": {"since": None, "until": None}})
    assert training_frame(frame, window) is frame
    episode = prepare_episode(frame, FeaturePipeline(), ticker_id="ETH_USD")
    assert len(episode) == n


# ===========================================================================
# 9. CLI surface
# ===========================================================================
def test_backtest_parser_accepts_config_and_json():
    args = _build_parser().parse_args(
        [
            "backtest",
            "--ticker", "ETH_USD",
            "--model", "ppo_v",
            "--pages", "3",
            "--seed", "9",
            "--config", "configs/default.yaml",
            "--json",
        ]
    )
    assert args.command == "backtest"
    assert args.config == "configs/default.yaml"
    assert args.json is True
    assert args.seed == 9
    assert args.models_root == "models"


def test_backtest_parser_defaults_leave_config_and_json_off():
    args = _build_parser().parse_args(["backtest", "--ticker", "ETH_USD", "--model", "m"])
    assert args.config is None
    assert args.json is False


def test_train_parser_accepts_json():
    args = _build_parser().parse_args(
        ["train", "--ticker", "ETH_USD", "--model", "m", "--json"]
    )
    assert args.json is True


def test_backtest_dispatch_forwards_config_seed_and_root():
    with mock.patch("kraken_trading_bot.rl.backtest.backtest_model") as bt:
        bt.return_value = BacktestResult(ticker_id="ETH_USD", model_name="ppo_v")
        rc = main(
            [
                "backtest",
                "--ticker", "ETH_USD",
                "--model", "ppo_v",
                "--seed", "9",
                "--config", "configs/default.yaml",
                "--models-root", "tmp_models",
            ]
        )
    assert rc == 0
    kwargs = bt.call_args.kwargs
    assert kwargs["config_path"] == "configs/default.yaml"
    # The explicit flags win: seed over the environment, models-root over
    # the default root.
    assert kwargs["seed"] == 9
    assert kwargs["models_root"] == "tmp_models"


def test_backtest_json_prints_one_parseable_object_and_nothing_else(capsys):
    result = BacktestResult(
        ticker_id="ETH_USD",
        model_name="ppo_v",
        total_return=0.1234,
        sharpe=1.2345,
        max_drawdown=0.0456,
        num_trades=42,
        win_rate=0.556,
        equity_curve=[10_000.0, 11_123.0],
        n_steps=1,
        final_equity=11_123.0,
        seed=42,
        buy_hold_return=0.20,
        excess_return=-0.0766,
        buy_hold_max_drawdown=0.03,
        n_bars=1,
        fee_rate=0.0026,
        slippage=0.0005,
        action_space="continuous",
    )
    with mock.patch("kraken_trading_bot.rl.backtest.backtest_model") as bt:
        bt.return_value = result
        rc = main(["backtest", "--ticker", "ETH_USD", "--model", "ppo_v", "--json"])

    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)  # would raise on any extra output
    for key in (
        "ticker_id",
        "model_name",
        "total_return",
        "buy_hold_return",
        "excess_return",
        "buy_hold_max_drawdown",
        "num_trades",
        "n_bars",
        "fee_rate",
        "slippage",
        "action_space",
    ):
        assert key in payload, key
    assert payload["buy_hold_return"] == pytest.approx(0.20)
    assert payload["n_bars"] == 1
    assert payload["fee_rate"] == pytest.approx(0.0026)
    # stdout is JSON only: the human table is gone.
    assert "Total return:" not in captured.out
    assert "Buy & hold:" not in captured.out


def test_backtest_human_output_carries_the_benchmark_and_costs(capsys):
    result = BacktestResult(
        ticker_id="ETH_USD",
        model_name="ppo_v",
        total_return=0.10,
        buy_hold_return=0.25,
        excess_return=-0.15,
        buy_hold_max_drawdown=0.12,
        n_bars=697,
        fee_rate=0.0026,
        slippage=0.0005,
    )
    with mock.patch("kraken_trading_bot.rl.backtest.backtest_model") as bt:
        bt.return_value = result
        rc = main(["backtest", "--ticker", "ETH_USD", "--model", "ppo_v"])
    out = capsys.readouterr().out
    assert rc == 0
    # Pre-existing lines must survive for existing readers...
    assert "Total return:   10.00%" in out
    # ...and the new ones must be there, including the magnitude and the
    # costs that were actually applied.
    assert "Buy & hold:     25.00%" in out
    assert "Excess return:  -15.00%" in out
    assert "Bars replayed:  697" in out
    assert "0.2600% / 0.0500%" in out


def test_backtest_error_goes_to_stderr_leaving_stdout_clean(capsys):
    with mock.patch("kraken_trading_bot.rl.backtest.backtest_model") as bt:
        bt.side_effect = ActionSpaceMismatchError(
            "continuous", "discrete", context="ETH_USD/ppo_v backtest"
        )
        rc = main(["backtest", "--ticker", "ETH_USD", "--model", "ppo_v", "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "Action-space mismatch" in captured.err


def _train_record(n_bars: int = 168, n_features: int = 55) -> ModelRecord:
    return ModelRecord(
        ticker_id="ETH_USD",
        model_name="ppo_v",
        config={"ticker": "ETH/USD", "n_features": n_features, "n_bars": n_bars},
        model_path=Path("/tmp/models/ETH_USD/ppo_v/model.zip"),
        normalization_path=Path("/tmp/models/ETH_USD/ppo_v/normalization.npz"),
        config_path=Path("/tmp/models/ETH_USD/ppo_v/config.yaml"),
    )


def test_train_json_emits_the_agreed_fields(capsys):
    with mock.patch("kraken_trading_bot.rl.train.train_ticker") as train:
        train.return_value = _train_record()
        rc = main(
            [
                "train",
                "--ticker", "ETH_USD",
                "--model", "ppo_v",
                "--timesteps", "512",
                "--seed", "11",
                "--json",
            ]
        )
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload == {
        "ticker_id": "ETH_USD",
        "model_name": "ppo_v",
        "n_features": 55,
        "n_bars": 168,
        "timesteps": 512,
        "seed": 11,
        "model_path": "/tmp/models/ETH_USD/ppo_v/model.zip",
    }


def test_train_json_tolerates_a_pre_provenance_artifact(capsys):
    with mock.patch("kraken_trading_bot.rl.train.train_ticker") as train:
        record = _train_record()
        record.config = {}
        train.return_value = record
        rc = main(["train", "--ticker", "ETH_USD", "--model", "ppo_v", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["n_bars"] is None
    assert payload["n_features"] is None


def test_train_human_output_mentions_the_training_window(capsys):
    with mock.patch("kraken_trading_bot.rl.train.train_ticker") as train:
        train.return_value = _train_record()
        rc = main(["train", "--ticker", "ETH_USD", "--model", "ppo_v"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Training bars:   168" in out


def test_train_error_goes_to_stderr_leaving_stdout_clean(capsys):
    with mock.patch("kraken_trading_bot.rl.train.train_ticker") as train:
        train.side_effect = RuntimeError("boom")
        rc = main(["train", "--ticker", "ETH_USD", "--model", "ppo_v", "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "Error training ETH_USD/ppo_v: boom" in captured.err