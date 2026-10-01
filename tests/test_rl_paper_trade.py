"""Tests for the live paper-trading runner.

All tests are hermetic: the Kraken manager is a mock (or a tiny in-test
double) and every model artifact lives under pytest ``tmp_path``.  No
network, no credentials, no real orders.  One real PPO model is trained
module-wide so `model.zip` loading is exercised end to end.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import numpy as np
import pandas as pd
import pytest
from kraken_api.models import Candle

from kraken_trading_bot.cli import _build_parser, main
from kraken_trading_bot.rl import train_ticker
from kraken_trading_bot.rl.data import add_derived_ohlcv_features
from kraken_trading_bot.rl.features import (
    FeatureWidthMismatchError,
    check_feature_width,
)
from kraken_trading_bot.rl.paper_trade import PaperSignal, PaperTrader, run_paper_trader


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
def _candle(ts: int, close: float, volume: str = "100.0", count: int = 5) -> Candle:
    """One flat OHLC candle at ``close`` (passes through candles_to_dataframe)."""
    return Candle(
        pair="ETH/USD",
        time=ts,
        open=str(close),
        high=str(close),
        low=str(close),
        close=str(close),
        vwap="",
        volume=volume,
        count=count,
    )


def _candle_window(n: int = 60, base: float = 2000.0, seed: int = 3) -> list[Candle]:
    """A deterministic random-walk candle sequence for the mock OHLC endpoint."""
    rng = np.random.default_rng(seed)
    closes = base * np.exp(np.cumsum(rng.normal(0.0, 0.005, n)))
    start = int(pd.Timestamp("2024-06-01T00:00:00Z").timestamp())
    return [_candle(start + i * 3600, float(c)) for i, c in enumerate(closes)]


def _synthetic_ohlcv(n: int = 60, base: float = 2000.0, seed: int = 9) -> pd.DataFrame:
    """OHLCV frame (DataFrame-like response for observation building)."""
    rng = np.random.default_rng(seed)
    close = base * np.exp(np.cumsum(rng.normal(0.0, 0.005, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1.0 + rng.uniform(0.0, 0.005, n))
    low = np.minimum(open_, close) * (1.0 - rng.uniform(0.0, 0.005, n))
    volume = rng.uniform(50.0, 200.0, n)
    idx = pd.date_range("2024-06-01", periods=n, freq="h")
    # vwap/count too: the read seam derives the three OHLCV-only scalars
    # from them, so a frame without them is a genuinely narrower one.
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "vwap": (high + low + close) / 3.0,
            "volume": volume,
            "count": rng.integers(10, 100, n),
        },
        index=idx,
    )


class MockManager:
    """Hermetic KrakenManager stand-in: synthetic candles + order recorders."""

    def __init__(self, candles: list[Candle]) -> None:
        self._candles = list(candles)
        self.ohlc_calls: list[int | None] = []
        self.buy = mock.MagicMock(return_value={"txid": ["MOCK-BUY"]})
        self.sell = mock.MagicMock(return_value={"txid": ["MOCK-SELL"]})
        # No `paper_account` attribute on purpose: PaperTrader must handle
        # managers without a paper simulation (falls back to internal state).

    def ohlc(self, pair, interval, since=None):
        self.ohlc_calls.append(since)
        return self._candles, 0  # exchange says history exhausted after page 1


class OneShotManager:
    """Returns a fixed candle list once (used to train the fixture model)."""

    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles

    def ohlc(self, pair, interval, since=None):
        return self.candles, 0


@pytest.fixture(scope="module")
def trained_model(tmp_path_factory):
    """A real, tiny trained PPO model + config.yaml + normalization.npz."""
    root = tmp_path_factory.mktemp("rl-paper")
    candles = _candle_window(n=90, seed=11)
    record = train_ticker(
        "ETH/USD",
        "ppo_paper",
        manager=OneShotManager(candles),
        pages=2,
        total_timesteps=150,
        seed=7,
        models_root=root,
    )
    assert record.is_trained()
    return root, record


# ---------------------------------------------------------------------------
# PaperTrader construction
# ---------------------------------------------------------------------------
def test_paper_trader_init_loads_model_env_config(trained_model):
    root, record = trained_model
    mgr = MockManager(_candle_window())

    trader = PaperTrader("ETH/USD", "ppo_paper", manager=mgr, models_root=root)

    # Config read from models/{TICKER}/{MODEL}/config.yaml.
    assert trader.config.get("ticker") == "ETH/USD"
    assert trader.config.get("model_name") == "ppo_paper"
    assert trader.config.get("action_space") == "continuous"
    # Model policy loaded through RLAgent.load.
    assert trader.agent.model is not None
    # Environment built with the same pipeline as training.
    assert trader.env is not None
    assert trader.pipeline is not None
    assert trader.action_space_name == "continuous"
    assert trader.pair == "ETH/USD"
    assert trader.base_asset == "ETH" and trader.quote_asset == "USD"
    # Init fetched an OHLC window through the (mock) manager: no network.
    assert mgr.ohlc_calls
    # Paper account is absent -> internal tracker fallback is used.
    assert trader.paper_account is None


def test_paper_trader_missing_model_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="No trained model"):
        PaperTrader("ETH/USD", "missing", manager=mock.MagicMock(), models_root=tmp_path)


# ---------------------------------------------------------------------------
# one-tick flow + action mapping (through the mock OHLC endpoint)
# ---------------------------------------------------------------------------
def test_one_tick_buy_action_fires_paper_order(trained_model):
    root, _ = trained_model
    mgr = MockManager(_candle_window())
    trader = PaperTrader("ETH/USD", "ppo_paper", manager=mgr, models_root=root)
    trader.agent = mock.MagicMock()
    trader.agent.predict.return_value = np.array([0.8, 0.0, 0.1], dtype=np.float32)

    info = trader.step()

    assert trader.agent.predict.call_count == 1
    assert mgr.buy.call_count == 1
    assert mgr.sell.call_count == 0
    # buy(pair, volume_str) with a positive Decimal volume.
    pair_arg, volume_arg = mgr.buy.call_args.args
    assert pair_arg == "ETH/USD"
    assert Decimal(volume_arg) > 0
    assert info["side"] == "buy"
    assert info["tick"] == 1


def test_strong_hold_action_no_order(trained_model):
    root, _ = trained_model
    mgr = MockManager(_candle_window())
    trader = PaperTrader("ETH/USD", "ppo_paper", manager=mgr, models_root=root)
    trader.agent = mock.MagicMock()
    trader.agent.predict.return_value = np.array([0.0, 0.0, 1.0], dtype=np.float32)

    info = trader.step()

    assert info["side"] == "hold"
    assert mgr.buy.call_count == 0
    assert mgr.sell.call_count == 0
    assert trader._holds == 1


def test_dry_run_logs_without_touching_manager(trained_model):
    root, _ = trained_model
    mgr = MockManager(_candle_window())
    trader = PaperTrader(
        "ETH/USD", "ppo_paper", manager=mgr, models_root=root, dry_run=True
    )
    trader.agent = mock.MagicMock()
    trader.agent.predict.return_value = np.array([0.8, 0.0, 0.1], dtype=np.float32)

    info = trader.step()

    assert info["side"] == "buy"
    assert mgr.buy.call_count == 0
    assert mgr.sell.call_count == 0
    # Dry-run still records the would-be order.
    assert trader._buys == 1


def test_discrete_action_mapping(trained_model):
    root, _ = trained_model
    mgr = MockManager(_candle_window())
    trader = PaperTrader("ETH/USD", "ppo_paper", manager=mgr, models_root=root)
    trader.action_space_name = "discrete"
    trader.agent = mock.MagicMock()

    trader.agent.predict.return_value = np.array([0])  # discrete buy
    info_buy = trader.step()
    assert info_buy["side"] == "buy"
    assert mgr.buy.call_count == 1

    trader.agent.predict.return_value = np.array([1])  # discrete hold
    info_hold = trader.step()
    assert info_hold["side"] == "hold"
    assert mgr.buy.call_count == 1  # unchanged
    assert mgr.sell.call_count == 0


def test_sell_fires_when_position_held(trained_model):
    root, _ = trained_model
    mgr = MockManager(_candle_window())
    trader = PaperTrader("ETH/USD", "ppo_paper", manager=mgr, models_root=root)
    # Fake an open base position through the internal tracker.
    trader._internal_position = Decimal("1.5")
    trader.agent = mock.MagicMock()
    trader.agent.predict.return_value = np.array([0.0, 0.5, 0.0], dtype=np.float32)

    info = trader.step()

    assert info["side"] == "sell"
    assert mgr.sell.call_count == 1
    assert float(Decimal(mgr.sell.call_args.args[1])) == pytest.approx(0.75, rel=1e-9)


# ---------------------------------------------------------------------------
# observation correctness
# ---------------------------------------------------------------------------
def test_observation_width_mismatch_raises_clear_error(trained_model):
    """A live frame narrower than the model was fitted on is rejected.

    The guard is non-self-referential: the expected names come from the
    model's own ``normalization.npz``, the actual columns from the live
    ``compute``.  Dropping one column therefore fails *before*
    ``normalize`` could silently shrink the row back to the model's
    width, which is the tautology this replaced.
    """
    root, _ = trained_model
    mgr = MockManager(_candle_window())
    trader = PaperTrader("ETH/USD", "ppo_paper", manager=mgr, models_root=root)

    orig_compute = trader.pipeline.compute

    def broken(df):
        out = orig_compute(df)
        return out.loc[:, out.columns[1:]]  # one feature too few

    trader.pipeline.compute = broken
    with pytest.raises(FeatureWidthMismatchError, match="Feature-width mismatch"):
        trader._build_observation(_synthetic_ohlcv(60))


def test_observation_width_guard_is_not_self_referential(trained_model):
    """The tautology: normalize() drops unknown columns, so a shape check
    against ``env.observation_space`` always passes for a stale model.

    Proven here directly: normalizing the live frame with the loaded
    stats produces exactly the model's width again even when the live
    pipeline had a whole extra column — which is precisely why the guard
    must compare *names*, not the resulting row length.
    """
    root, _ = trained_model
    mgr = MockManager(_candle_window())
    trader = PaperTrader("ETH/USD", "ppo_paper", manager=mgr, models_root=root)

    stats = trader.pipeline.stats_for(trader.ticker_key)
    assert stats is not None
    df = add_derived_ohlcv_features(_synthetic_ohlcv(60))
    computed = trader.pipeline.compute(df)

    # Simulate a widened live pipeline the model never saw.
    computed = computed.copy()
    computed["some_future_feature"] = 1.0

    normalized = stats.normalize(computed.ffill().fillna(0.0))
    assert normalized.shape[1] == len(stats.feature_names)
    assert "some_future_feature" not in normalized.columns
    # ...yet the name check still sees the truth.
    with pytest.raises(FeatureWidthMismatchError, match="some_future_feature"):
        check_feature_width(
            stats.feature_names, list(computed.columns), context="unit"
        )


def test_built_observation_matches_env_space(trained_model):
    root, _ = trained_model
    mgr = MockManager(_candle_window())
    trader = PaperTrader("ETH/USD", "ppo_paper", manager=mgr, models_root=root)

    obs = trader._build_observation(_synthetic_ohlcv(60))
    assert obs.shape == trader.env.observation_space.shape
    assert obs.dtype == np.float32


def test_built_observation_is_z_scored_by_saved_stats(trained_model):
    """Paper inference applies the saved npz stats, not the raw row.

    The train/infer-skew spot: ``_build_observation`` rebuilds the row per
    60 s tick from a *freshly fetched* window, so if it returned the raw
    ffilled features the live policy would be reading a different scale
    than the one it was trained on.  It must apply the same
    ``compute -> ffill -> fillna(0) -> (x - mean) / std`` the training
    environment applies, using the stats loaded from
    ``models/{TICKER}/{MODEL}/normalization.npz``.
    """
    root, record = trained_model
    mgr = MockManager(_candle_window())
    trader = PaperTrader("ETH/USD", "ppo_paper", manager=mgr, models_root=root)

    # The npz guard is unchanged and the saved stats are what gets used.
    assert record.normalization_path is not None
    stats = trader.pipeline.stats_for(trader.ticker_key)
    assert stats is not None
    assert stats.feature_names

    df = _synthetic_ohlcv(60)
    obs = trader._build_observation(df)

    # Identical transform to the training env's _raw_feature_array.  Both
    # sides derive the OHLCV-only scalars from the raw frame first, so a
    # caller-supplied frame is on the same footing as a fetched one.
    expected = trader.pipeline.transform(
        add_derived_ohlcv_features(df), ticker_id=trader.ticker_key
    )
    np.testing.assert_allclose(obs, expected[-1], rtol=1e-5, atol=1e-6)

    # ... and specifically not the raw ffilled row.
    raw = (
        trader.pipeline.compute(add_derived_ohlcv_features(df))
        .ffill()
        .fillna(0.0)
        .to_numpy(dtype=np.float32)
    )
    assert not np.allclose(obs, raw[-1])


# ---------------------------------------------------------------------------
# run_paper_trader convenience
# ---------------------------------------------------------------------------
def test_run_paper_trader_fixed_iterations(trained_model, capsys):
    root, _ = trained_model
    mgr = MockManager(_candle_window())

    trader = run_paper_trader(
        "ETH/USD",
        "ppo_paper",
        manager=mgr,
        models_root=root,
        iterations=1,
        interval=0,
        dry_run=True,
    )

    assert trader._ticks == 1
    out = capsys.readouterr().out
    assert "Paper trade summary" in out


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------
def test_cli_paper_trade_parser_flags():
    parser = _build_parser()
    args = parser.parse_args(
        ["paper-trade", "--ticker", "ETH_USD", "--model", "ppo_eth_01",
         "--interval", "30", "--iterations", "5", "--dry-run"]
    )
    assert args.command == "paper-trade"
    assert args.ticker == "ETH_USD"
    assert args.model == "ppo_eth_01"
    assert args.interval == 30
    assert args.iterations == 5
    assert args.dry_run is True
    assert args.models_root == "models"


def test_cli_paper_trade_missing_model_errors(tmp_path, capsys):
    rc = main(
        [
            "paper-trade",
            "--ticker", "ETH_USD",
            "--model", "missing",
            "--models-root", str(tmp_path),
        ]
    )
    assert rc == 1
    assert "not trained" in capsys.readouterr().err.lower()


def test_cli_paper_trade_dispatches_to_runner(trained_model):
    root, _ = trained_model
    with mock.patch("kraken_trading_bot.rl.paper_trade.run_paper_trader") as rpt:
        rc = main(
            [
                "paper-trade",
                "--ticker", "ETH_USD",
                "--model", "ppo_paper",
                "--models-root", str(root),
                "--iterations", "1",
                "--dry-run",
            ]
        )
    assert rc == 0
    rpt.assert_called_once()

# ---------------------------------------------------------------------------
# Gap-1 widening: a caller-supplied frame is derived like a fetched one
# ---------------------------------------------------------------------------
def test_step_supplied_frame_gets_the_same_derivation(trained_model):
    """A frame handed to step() is on the same footing as a fetched one.

    step(df) skips the read seam, so the vwap/count derivation is applied
    there too — otherwise a supplied frame would trip the width guard
    rather than simply working.
    """
    root, _ = trained_model
    mgr = MockManager(_candle_window())
    trader = PaperTrader("ETH/USD", "ppo_paper", manager=mgr, models_root=root)

    result = trader.step(_synthetic_ohlcv(60))
    assert result["tick"] == 1
    assert result["side"] in {"buy", "sell", "hold"}


def test_paper_trader_observation_is_55_wide_with_a_funding_frame(trained_model):
    """The gate number at the paper-trade boundary, with funding present.

    49 base + three vwap/count scalars + funding_rate_prediction + vol24h
    + spread.
    """
    root, record = trained_model
    mgr = MockManager(_candle_window())
    trader = PaperTrader("ETH/USD", "ppo_paper", manager=mgr, models_root=root)

    df = add_derived_ohlcv_features(_synthetic_ohlcv(60))
    df["funding_rate_prediction"] = 0.0003
    df["vol24h"] = 42000.0
    df["bid"] = df["close"] - 0.4
    df["ask"] = df["close"] + 0.4

    stats = trader.pipeline.stats_for(trader.ticker_key)
    assert stats is not None
    live = trader.pipeline.compute(add_derived_ohlcv_features(df))
    assert live.shape[1] == 55

    # Without the funding columns the live frame is the three-scalar-wider
    # base, i.e. the trained model's own width.
    plain = trader.pipeline.compute(add_derived_ohlcv_features(_synthetic_ohlcv(60)))
    assert plain.shape[1] == len(stats.feature_names)
