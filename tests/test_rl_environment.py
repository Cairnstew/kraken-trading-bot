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
    prepare_episode,
)
from kraken_trading_bot.rl.data import add_derived_ohlcv_features
from kraken_trading_bot.rl.features import (
    FeatureWidthMismatchError,
    check_feature_width,
)


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


def test_environment_buy_hold_sell_confidence():
    """hold_confidence >= threshold suppresses the trade."""
    df = _synthetic_ohlcv()
    env = TradingEnvironment("ETH/USD", df, reward_spec={"hold_confidence_threshold": 0.5})

    env.reset(seed=0)
    env.step(np.array([0.9, 0.0, 0.6], dtype=np.float32))  # holds -> no position
    assert env.position == 0.0

    env.step(np.array([0.9, 0.0, 0.2], dtype=np.float32))  # buys
    assert env.position > 0.0

# ---------------------------------------------------------------------------
# normalization wiring: env observation == FeaturePipeline.transform
# ---------------------------------------------------------------------------
def test_raw_feature_array_is_the_transformed_frame():
    """The env's observation matrix IS ``FeaturePipeline.transform``.

    Regression contract for the Candidate-1 normalization wiring: the
    environment applies the ticker's fitted ``NormalizationStats`` to the
    ffilled matrix (``compute`` -> ``ffill`` -> ``fillna(0)`` ->
    ``(x - mean) / std``), so the row handed to the policy is the exact
    affine image ``export.py --normalized`` renders as its ``z_*`` block
    and ``PaperTrader._build_observation`` re-derives live.  Pre-fix this
    returned the *raw* ffilled matrix, which no other consumer produced.
    """
    df = _synthetic_ohlcv(n=320)
    pipe = FeaturePipeline(windows=(1, 4, 24))
    # Mirror the training path exactly: prepare_episode slices, then fits.
    episode = prepare_episode(df, pipe, ticker_id="ETH/USD", episode_bars=200)
    env = TradingEnvironment("ETH/USD", data=episode, feature_pipeline=pipe)

    expected = pipe.transform(episode, ticker_id=normalize_ticker_id("ETH/USD"))
    matrix = env._raw_feature_array()

    assert matrix.shape == expected.shape
    np.testing.assert_allclose(matrix, expected, rtol=1e-5, atol=1e-6)

    # And it is genuinely z-scored, not the raw heteroscaled row.
    raw = pipe.compute(episode).ffill().fillna(0.0).to_numpy(dtype=np.float32)
    assert not np.allclose(matrix, raw)

    # _observe() is a positional slice of that matrix, so the first traded
    # bar is transform[start_index] -- i.e. z-scored too.
    obs, _ = env.reset(seed=0)
    np.testing.assert_allclose(
        obs, expected[env._start_index], rtol=1e-5, atol=1e-6
    )


def test_observation_uses_saved_stats_not_a_refit(tmp_path):
    """A pipeline carrying loaded npz stats scales by *those*, not a refit.

    The backtest/paper call sites build a pipeline and load
    ``normalization.npz``; the env must then use the loaded stats instead
    of re-fitting on the replay frame (which would make backtest and paper
    in-sample normalized and silently skew against the trained policy).
    """
    df = _synthetic_ohlcv(n=320)
    train_pipe = FeaturePipeline(windows=(1, 4, 24))
    episode = prepare_episode(df, train_pipe, ticker_id="ETH/USD", episode_bars=200)

    path = tmp_path / "normalization.npz"
    train_pipe.save_normalization("ETH_USD", path)

    # Fresh pipeline + loaded stats, exactly what backtest.py/paper_trade.py do.
    live_pipe = FeaturePipeline(windows=(1, 4, 24))
    live_pipe.load_normalization("ETH_USD", path)
    env = TradingEnvironment("ETH/USD", data=episode, feature_pipeline=live_pipe)

    np.testing.assert_allclose(
        env._raw_feature_array(),
        train_pipe.transform(episode, ticker_id="ETH_USD"),
        rtol=1e-5,
        atol=1e-6,
    )
    # A refit would have overwritten the loaded stats; it did not.
    loaded = live_pipe.stats_for("ETH_USD")
    saved = train_pipe.stats_for("ETH_USD")
    for name in saved.feature_names:
        assert loaded.stats[name] == pytest.approx(saved.stats[name], rel=1e-9)


# ---------------------------------------------------------------------------
# Gap-1 widening: presence-gated builders and the width probe
# ---------------------------------------------------------------------------
# Base width with the default groups and a bare OHLCV frame, measured
# before the widening and re-pinned by the probe below.
_BASE_WIDTH = 49
# ...and with vwap/count/funding bid-ask present.
_WIDENED_WIDTH = 55


def _widened_ohlcv(n: int = 200, seed: int = 5) -> pd.DataFrame:
    """An OHLCV frame plus every input the six activated columns need."""
    rng = np.random.default_rng(seed)
    df = _synthetic_ohlcv(n=n, seed=seed)
    df["vwap"] = df["close"] * (1.0 - rng.uniform(0.0, 0.002, n))
    df["count"] = rng.integers(5, 80, n).astype(float)
    df["bid"] = df["close"] - 0.4
    df["ask"] = df["close"] + 0.4
    df["funding_rate_prediction"] = rng.normal(0.0, 0.0002, n)
    df["vol24h"] = rng.uniform(1000.0, 5000.0, n)
    return df


def test_vwap_and_count_produce_the_derived_columns():
    """{vwap, count, volume} -> {vwap_dev, trade_count_zscore_20, volume_per_trade}.

    The builder is presence-gated, so this asserts the *derivation*
    through the read seam (which is where the three live), not a required
    column on the frame.
    """
    df = _widened_ohlcv()
    out = add_derived_ohlcv_features(df)

    assert {"vwap_dev", "trade_count_zscore_20", "volume_per_trade"}.issubset(
        out.columns
    )
    expected = df["close"] / df["vwap"] - 1.0
    np.testing.assert_allclose(
        out["vwap_dev"].to_numpy(), expected.to_numpy(), rtol=1e-12
    )
    expected_vpt = df["volume"] / df["count"]
    np.testing.assert_allclose(
        out["volume_per_trade"].to_numpy(), expected_vpt.to_numpy(), rtol=1e-12
    )
    # A 20-bar rolling z-score: the warmup rows are NaN (the observation's
    # ffill/fillna handles them like every other indicator's) and later
    # rows are finite and vary with the count.
    z = out["trade_count_zscore_20"]
    assert z.iloc[:19].isna().all()
    assert np.isfinite(z.iloc[19:].to_numpy()).all()
    assert z.iloc[19:].nunique() > 1
    # The caller's frame is untouched (the seam derives on a copy).
    assert "vwap_dev" not in df.columns


def test_derived_columns_reach_the_observation_via_the_pipeline():
    """The three derived columns are pass-through members, so they land."""
    from kraken_trading_bot.rl.features import _SIGNAL_COLUMNS

    for name in ("vwap_dev", "trade_count_zscore_20", "volume_per_trade"):
        assert name in _SIGNAL_COLUMNS

    pipeline = FeaturePipeline(windows=[1, 4, 24], feature_groups=["signals"])
    computed = pipeline.compute(add_derived_ohlcv_features(_widened_ohlcv()))
    assert {"vwap_dev", "trade_count_zscore_20", "volume_per_trade"}.issubset(
        computed.columns
    )


def test_width_probe_55_with_all_inputs_and_49_without():
    """The gate number, both ways.

    All inputs present -> 55.  The same frame without vwap/count/bid/ask
    -> 49, i.e. **unchanged**: the builders are presence-gated, never
    required-column, so a source that lacks them still computes.

    Both sides go through ``add_derived_ohlcv_features`` first, because
    that is what the read seam does and therefore what every consumer
    actually sees.  It is a no-op on the bare frame (no vwap/count to
    derive from), which is exactly the 49 case: calling it there proves
    the presence-gating rather than skipping the helper to reach 49.
    """
    full = FeaturePipeline(windows=[1, 4, 24]).compute(
        add_derived_ohlcv_features(_widened_ohlcv())
    )
    assert full.shape[1] == _WIDENED_WIDTH, list(full.columns)

    plain = FeaturePipeline(windows=[1, 4, 24]).compute(
        add_derived_ohlcv_features(_synthetic_ohlcv())
    )
    assert plain.shape[1] == _BASE_WIDTH, list(plain.columns)

    # Exactly the six new names, nothing else.
    assert set(full.columns) - set(plain.columns) == {
        "vwap_dev",
        "trade_count_zscore_20",
        "volume_per_trade",
        "funding_rate_prediction",
        "vol24h",
        "spread",
    }


def test_width_probe_partial_inputs_still_compute():
    """`vwap` without `count` (and vice versa) computes, just narrower."""
    df = _widened_ohlcv()
    only_vwap = add_derived_ohlcv_features(df.drop(columns=["count"]))
    assert "vwap_dev" in only_vwap.columns
    assert "trade_count_zscore_20" not in only_vwap.columns
    assert "volume_per_trade" not in only_vwap.columns

    only_count = add_derived_ohlcv_features(df.drop(columns=["vwap"]))
    assert "vwap_dev" not in only_count.columns
    assert {"trade_count_zscore_20", "volume_per_trade"}.issubset(
        only_count.columns
    )


def test_zero_vwap_and_zero_count_are_guarded_not_infinite():
    """The divide-by-zero paths produce NaN, never inf.

    An inf here would survive ffill/fillna(0) untouched and then be
    z-scored into a poisoned observation row.
    """
    df = _widened_ohlcv(n=60)
    df.loc[df.index[10], "vwap"] = 0.0
    df.loc[df.index[11], "count"] = 0.0
    out = add_derived_ohlcv_features(df)

    assert np.isnan(out["vwap_dev"].iloc[10])
    assert np.isnan(out["volume_per_trade"].iloc[11])
    filled = out.ffill().fillna(0.0)
    assert np.isfinite(filled.to_numpy(dtype=float)).all()


# ---------------------------------------------------------------------------
# The width guard (non-tautological by construction)
# ---------------------------------------------------------------------------
def test_width_guard_fires_on_a_mismatched_feature_name_set():
    """Positive case: a stale 49-wide artifact against a 55-wide pipeline.

    The live column set is taken after the read seam's derivation, which is
    what the paper-trade and backtest call sites hand ``compute``, so the
    55 here is the width a real consumer sees.
    """
    df = add_derived_ohlcv_features(_widened_ohlcv())
    pipeline = FeaturePipeline(windows=[1, 4, 24])
    live_columns = list(pipeline.compute(df).columns)
    assert len(live_columns) == _WIDENED_WIDTH

    stale = live_columns[:-1]  # a 54-wide artifact
    with pytest.raises(FeatureWidthMismatchError) as excinfo:
        check_feature_width(stale, live_columns, context="unit")
    message = str(excinfo.value)
    assert "Feature-width mismatch" in message
    # Names, not just counts: the offending column is named.
    assert live_columns[-1] in message


def test_width_guard_passes_on_a_matched_feature_name_set():
    """Negative case: identical names, in any order, do not raise."""
    df = _widened_ohlcv()
    pipeline = FeaturePipeline(windows=[1, 4, 24])
    live_columns = list(pipeline.compute(df).columns)

    check_feature_width(live_columns, live_columns, context="unit")
    # Column *order* is not part of the contract; the observation is
    # z-scored per name, so a reordered artifact is not a mismatch.
    check_feature_width(list(reversed(live_columns)), live_columns, context="unit")


def test_width_guard_is_not_defeated_by_normalize_dropping_columns():
    """Why the guard compares names and not the resulting row width.

    ``NormalizationStats.normalize`` silently drops any column without a
    registered stat, so a *row-width* check always passes for a stale
    artifact.  This pins that trap so the guard cannot be "simplified"
    back into the tautology.
    """
    df = _widened_ohlcv()
    pipeline = FeaturePipeline(windows=[1, 4, 24])
    pipeline.fit(df, ticker_id="ETH_USD")
    stats = pipeline.stats_for("ETH_USD")
    assert stats is not None

    widened = pipeline.compute(df).copy()
    widened["a_brand_new_feature"] = 1.0

    # The tautological version: row width after normalize == fitted width.
    normalized = stats.normalize(widened.ffill().fillna(0.0))
    assert normalized.shape[1] == len(stats.feature_names)

    # The real one: the names disagree, and that is caught.
    with pytest.raises(FeatureWidthMismatchError, match="a_brand_new_feature"):
        check_feature_width(
            stats.feature_names, list(widened.columns), context="unit"
        )
