"""Tests for the RL trading environment and feature pipeline."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from gymnasium import spaces

from kraken_trading_bot.rl import (
    FeaturePipeline,
    NormalizationStats,
    RewardSpec,
    TradingEnvironment,
    normalize_ticker_id,
)
from kraken_trading_bot.rl.features import observation_frame


def _synthetic_ohlcv(n: int = 300, base: float = 2000.0, seed: int = 42) -> pd.DataFrame:
    """Deterministic OHLCV fixture (random-walk close prices)."""
    rng = np.random.default_rng(seed)
    close = base * np.exp(np.cumsum(rng.normal(0.00005, 0.01, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1.0 + rng.uniform(0.0, 0.005, n))
    low = np.minimum(open_, close) * (1.0 - rng.uniform(0.0, 0.005, n))
    volume = rng.uniform(50.0, 200.0, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="h")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# environment basics
# ---------------------------------------------------------------------------
def test_environment_creation_multiple_tickers():
    df = _synthetic_ohlcv()
    eth = TradingEnvironment("ETH/USD", df)
    btc = TradingEnvironment("XBT/USD", df)

    assert eth.ticker_id == "ETH_USD"
    assert btc.ticker_id == "XBT_USD"
    # Same data, same feature set -> identical observation shapes.
    assert eth.observation_space.shape == btc.observation_space.shape
    # Independent instances must not share mutable state.
    eth.step(np.array([0.5, 0.0, 0.0], dtype=np.float32))
    assert eth._position > 0
    assert btc._position == 0
    assert eth is not btc


def test_environment_action_spaces():
    df = _synthetic_ohlcv()
    cont = TradingEnvironment("ETH/USD", df, action_space="continuous")
    disc = TradingEnvironment("ETH/USD", df, action_space="discrete")

    assert isinstance(cont.action_space, spaces.Box)
    assert cont.action_space.shape == (3,)
    assert cont.action_space.low.min() == 0.0 and cont.action_space.high.max() == 1.0

    assert isinstance(disc.action_space, spaces.Discrete)
    assert disc.action_space.n == 3

    with pytest.raises(ValueError):
        TradingEnvironment("ETH/USD", df, action_space="bogus")


def test_environment_requires_ohlcv():
    df = pd.DataFrame({"close": [1.0, 2.0]})
    with pytest.raises(ValueError, match="OHLCV"):
        TradingEnvironment("ETH/USD", df)


def test_reset_returns_observation_and_info():
    df = _synthetic_ohlcv()
    env = TradingEnvironment("ETH/USD", df)

    obs, info = env.reset(seed=1)

    assert obs.shape == env.observation_space.shape
    assert obs.dtype == np.float32
    assert info["ticker_id"] == "ETH_USD"
    assert np.isclose(info["balance"], env.initial_balance)
    assert env.position == 0.0
    assert env.entry_price is None


def test_reset_step_cycle_runs_full_episode():
    df = _synthetic_ohlcv(n=120)
    env = TradingEnvironment("ETH/USD", df, fee_rate=0.001)

    obs, _ = env.reset(seed=3)
    assert obs.shape == env.observation_space.shape

    n_steps = 0
    truncated = terminated = False
    while not (truncated or terminated):
        action = np.array([0.1, 0.0, 0.0], dtype=np.float32)
        obs, reward, terminated, truncated, info = env.step(action)
        n_steps += 1
        assert obs.shape == env.observation_space.shape
        assert isinstance(reward, float)
        assert set(info) >= {"balance", "position", "entry_price", "equity"}

    # Episode covers every usable bar; ep ends by truncation.
    assert truncated is True
    assert n_steps == env.n_bars - env._start_index
    assert env.equity_curve.shape[0] == n_steps
    assert env.num_trades > 0


def test_discrete_action_spaces_execute():
    df = _synthetic_ohlcv(n=80)
    env = TradingEnvironment("ETH/USD", df, action_space="discrete")

    env.reset(seed=0)
    # buy, then sell-all paths should be exercised without error
    for action in (0, 1, 2, 0, 2):
        obs, _, terminated, truncated, _ = env.step(action)
        assert obs.shape == env.observation_space.shape
        if terminated or truncated:
            break


def test_reward_modes_differ():
    df = _synthetic_ohlcv(n=80)
    buy = np.array([0.5, 0.0, 0.0], dtype=np.float32)

    env_pnl = TradingEnvironment(
        "ETH/USD", df,
        reward_spec=RewardSpec(pnl=1.0, risk_adjusted=0.0),
        fee_rate=0.001,
    )
    env_sharpe = TradingEnvironment(
        "ETH/USD", df,
        reward_spec=RewardSpec(pnl=0.0, risk_adjusted=1.0),
        fee_rate=0.001,
    )

    obs_p, _ = env_pnl.reset(seed=0)
    obs_s, _ = env_sharpe.reset(seed=0)
    assert np.array_equal(obs_p, obs_s)

    r_pnl = [env_pnl.step(buy)[1] for _ in range(10)][-1]
    r_sharpe = [env_sharpe.step(buy)[1] for _ in range(10)][-1]
    # Different reward configs generally disagree in magnitude.
    assert r_pnl != pytest.approx(r_sharpe)


def test_reward_spec_from_dict():
    spec = RewardSpec.from_dict({"mode": "sharpe", "holding_penalty": 0.01})
    assert spec.pnl == 0.0
    assert spec.risk_adjusted == 1.0
    assert spec.holding_penalty == 0.01
    with pytest.raises(ValueError):
        RewardSpec.from_dict({"mode": "nope"})


def test_normalize_ticker_id():
    assert normalize_ticker_id("ETH/USD") == "ETH_USD"
    assert normalize_ticker_id("btc-usd") == "BTC_USD"


# ---------------------------------------------------------------------------
# feature pipeline
# ---------------------------------------------------------------------------
def test_feature_pipeline_shapes():
    df = _synthetic_ohlcv()
    pipe = FeaturePipeline(windows=(1, 4, 24))
    features = pipe.compute(df)

    assert isinstance(features, pd.DataFrame)
    assert len(features) == len(df)
    assert pipe.n_features() == features.shape[1]

    arr = pipe.fit_transform(df, ticker_id="ETH_USD")
    assert arr.shape == (len(df), features.shape[1])
    assert arr.dtype == np.float32
    # Normalized arrays are finite (NaNs filled).
    assert np.isfinite(arr).all()


def test_feature_pipeline_windows_and_groups():
    df = _synthetic_ohlcv()
    n_wide = FeaturePipeline(windows=(1, 4, 24)).compute(df).shape[1]
    n_narrow = FeaturePipeline(windows=(1,)).compute(df).shape[1]
    assert n_wide > n_narrow

    all_groups = FeaturePipeline(feature_groups=("technical",)).compute(df).shape[1]
    assert 0 < all_groups < n_wide
    # An excluded group contributes no columns.
    price_only = FeaturePipeline(feature_groups=("price",)).compute(df)
    assert "sma_1" not in price_only.columns
    assert "rsi_14" not in price_only.columns


def test_feature_pipeline_microstructure_optional():
    df = _synthetic_ohlcv()
    plain = FeaturePipeline(feature_groups=("microstructure",)).compute(df)
    assert plain.shape[1] == 0  # no spread/imbalance columns on the frame

    df_micro = df.copy()
    df_micro["spread"] = 0.001
    df_micro["bid_vol"] = 100.0
    df_micro["ask_vol"] = 80.0
    with_micro = FeaturePipeline(feature_groups=("microstructure",)).compute(df_micro)
    assert set(with_micro.columns) == {"spread", "order_book_imbalance"}


def test_normalization_stats_roundtrip(tmp_path):
    df = _synthetic_ohlcv()
    pipe = FeaturePipeline(windows=(1, 4))
    pipe.fit(df, ticker_id="ETH_USD")

    stats = pipe.stats_for("ETH_USD")
    assert isinstance(stats, NormalizationStats)
    assert stats.ticker_id == "ETH_USD"

    path = tmp_path / "models" / "ETH_USD" / "ppo_test" / "normalization.npz"
    pipe.save_normalization("ETH_USD", path)
    assert path.exists()

    pipe2 = FeaturePipeline(windows=(1, 4))
    pipe2.load_normalization("ETH_USD", path)
    stats2 = pipe2.stats_for("ETH_USD")
    assert stats2.feature_names == stats.feature_names
    for name in stats.feature_names:
        assert stats2.stats[name] == pytest.approx(stats.stats[name])

    # Transforms agree after save/load round-trip.
    arr_a = pipe.transform(df, ticker_id="ETH_USD")
    arr_b = pipe2.transform(df, ticker_id="ETH_USD")
    np.testing.assert_allclose(arr_a, arr_b, rtol=1e-6)


def test_normalization_stats_isolated_per_ticker():
    df_eth = _synthetic_ohlcv(base=2000.0, seed=1)
    df_btc = _synthetic_ohlcv(base=40000.0, seed=2)

    pipe = FeaturePipeline(windows=(1,))
    pipe.fit(df_eth, ticker_id="ETH_USD")
    pipe.fit(df_btc, ticker_id="XBT_USD")

    eth_stats = pipe.stats_for("ETH_USD")
    btc_stats = pipe.stats_for("XBT_USD")
    assert eth_stats.ticker_id == "ETH_USD"
    assert btc_stats.ticker_id == "XBT_USD"
    # Different price levels -> different normalization.
    assert eth_stats.stats["sma_1"][0] != pytest.approx(btc_stats.stats["sma_1"][0])


# ---------------------------------------------------------------------------
# environment + pipeline integration
# ---------------------------------------------------------------------------
def test_environment_with_pipeline():
    df = _synthetic_ohlcv()
    pipe = FeaturePipeline(windows=(1, 4, 24))
    env = TradingEnvironment("ETH/USD", df, feature_pipeline=pipe)

    assert env.observation_space.shape == (pipe.n_features(),)
    assert env.feature_names == pipe.compute(df).columns.tolist()
    assert env.normalization_stats() is not None

    obs, _ = env.reset(seed=7)
    assert obs.shape == env.observation_space.shape

    for _ in range(5):
        obs, reward, terminated, truncated, info = env.step(
            np.array([0.25, 0.0, 0.0], dtype=np.float32)
        )
        assert obs.shape == env.observation_space.shape
        if terminated or truncated:
            break
    assert info["ticker_id"] == "ETH_USD"


def test_environment_observation_is_z_scored_affine_image():
    """The policy sees the features z-scored with the ticker's stats.

    This is the environment half of the normalization contract: the row
    handed to the agent must be ``(observed - mean) / std`` on the
    observation frame, exactly what ``normalization.npz`` records and
    exactly what the export's ``z_`` block renders. The map is affine, so
    the observation width is unchanged and a policy trained against these
    stats still loads.
    """
    df = _synthetic_ohlcv()
    pipe = FeaturePipeline(windows=(1, 4, 24))
    env = TradingEnvironment("ETH/USD", df, feature_pipeline=pipe)

    observed = observation_frame(pipe.compute(df))
    stats = env.normalization_stats()
    assert stats is not None

    expected = stats.normalize(observed).to_numpy(dtype=np.float32)

    # Width is unchanged: an affine map over the same feature set.
    assert env.observation_space.shape == (observed.shape[1],)
    np.testing.assert_allclose(env._feature_matrix, expected, rtol=1e-5, atol=1e-5)

    # Every column is centred on zero and unit-scaled (the constant-column
    # guard makes a zero-variance feature land on exactly 0).
    np.testing.assert_allclose(expected.mean(axis=0), 0.0, atol=1e-4)
    unit = (observed.std(ddof=0) > 1e-12).to_numpy()
    np.testing.assert_allclose(expected.std(axis=0)[unit], 1.0, rtol=1e-3)

    # And what reset() returns is a row of that matrix.
    obs, _ = env.reset(seed=3)
    np.testing.assert_allclose(obs, expected[env._step_idx], rtol=1e-5, atol=1e-5)


def test_environment_builtin_features_unscaled():
    """Without a pipeline there are no stats, so nothing is z-scored."""
    df = _synthetic_ohlcv()
    env = TradingEnvironment("ETH/USD", df)

    assert env.pipeline is None
    assert env.normalization_stats() is None
    assert env._feature_matrix.shape == (
        len(df),
        env.observation_space.shape[0],
    )
    assert np.isfinite(env._feature_matrix).all()

    obs, _ = env.reset(seed=1)
    assert obs.shape == env.observation_space.shape
    assert np.isfinite(obs).all()


def test_environment_buy_hold_sell_confidence():
    """hold_confidence >= threshold suppresses the trade."""
    df = _synthetic_ohlcv()
    env = TradingEnvironment("ETH/USD", df, reward_spec={"hold_confidence_threshold": 0.5})

    env.reset(seed=0)
    env.step(np.array([0.9, 0.0, 0.6], dtype=np.float32))  # holds -> no position
    assert env.position == 0.0

    env.step(np.array([0.9, 0.0, 0.2], dtype=np.float32))  # buys
    assert env.position > 0.0