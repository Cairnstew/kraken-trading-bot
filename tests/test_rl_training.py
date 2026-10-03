"""Tests for the RL training pipeline.

Covers the model registry, OHLC data loading/pagination, the
stable-baselines3 PPO agent wrapper, the ``train_ticker`` orchestrator,
and the backtest evaluator.  Model artifacts are written under pytest's
``tmp_path`` so the real ``models/`` tree is never touched.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from kraken_api.models import Candle

from kraken_trading_bot.rl import (
    BacktestResult,
    FeaturePipeline,
    ModelRecord,
    NotEnoughDataError,
    RLAgent,
    TradingEnvironment,
    backtest_model,
    candles_to_dataframe,
    fetch_ohlc_dataframe,
    list_models,
    normalize_ticker_id,
    pair_from_ticker_id,
    prepare_episode,
    register_model,
    train_ticker,
)
from kraken_trading_bot.rl.features import FeatureWidthMismatchError


def _synthetic_ohlcv(n: int = 300, base: float = 2000.0, seed: int = 42) -> pd.DataFrame:
    """Deterministic OHLCV fixture (random-walk close prices).

    Carries ``vwap`` and ``count`` as well, because that is what a real
    frame holds: the read seam (``data.read_ohlc_dataframe``) derives
    ``vwap_dev``/``trade_count_zscore_20``/``volume_per_trade`` from them
    (Gap-1 widening, 49 -> 52 features here), so a hand-built frame
    without them would compute at the narrower width and trip the width
    guard.  Use ``add_derived_ohlcv_features`` to stand in for the seam.
    """
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
            "vwap": (high + low + close) / 3.0,
            "volume": volume,
            "count": rng.integers(10, 100, n),
        },
        index=idx,
    )


def _candle(ts: int, close: float, volume: str = "100.0", count: int = 5) -> Candle:
    """One flat OHLC candle at ``close`` with given volume/count."""
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


def _sample_config() -> dict:
    return {
        "ticker": "ETH/USD",
        "ohlcv_interval_minutes": 60,
        "initial_balance": 10000.0,
        "fee_rate": 0.0,
        "slippage": 0.0,
        "allow_short": False,
        "action_space": "continuous",
        "reward": {"mode": "pnl", "pnl": 1.0},
        "feature_windows": [1, 4, 24],
        "feature_groups": ["price", "technical", "volume", "microstructure"],
        "model_name": "ppo_test",
    }


# ---------------------------------------------------------------------------
# model registry
# ---------------------------------------------------------------------------
def test_registry_register_list_roundtrip(tmp_path):
    cfg = _sample_config()
    record = register_model("ETH/USD", "ppo_test", cfg, root=tmp_path)

    assert isinstance(record, ModelRecord)
    assert record.ticker_id == "ETH_USD"
    assert record.model_name == "ppo_test"
    assert record.config_path is not None and record.config_path.is_file()
    assert record.model_path is None  # not trained yet
    assert record.normalization_path is None

    # config.yaml round-trips to exactly the dict we registered
    on_disk = yaml.safe_load(record.config_path.read_text(encoding="utf-8"))
    assert on_disk == cfg

    records = list_models(root=tmp_path)
    assert "ETH_USD" in records
    rec = records["ETH_USD"][0]
    assert rec.model_name == "ppo_test"
    assert rec.created is not None
    assert rec.config == cfg


def test_registry_per_ticker_independence(tmp_path):
    register_model("ETH/USD", "eth_01", _sample_config(), root=tmp_path)
    register_model(
        "XBT/USD",
        "xbt_01",
        {**_sample_config(), "ticker": "XBT/USD"},
        root=tmp_path,
    )

    records = list_models(root=tmp_path)
    assert set(records) == {"ETH_USD", "XBT_USD"}
    assert [r.model_name for r in records["ETH_USD"]] == ["eth_01"]
    assert [r.model_name for r in records["XBT_USD"]] == ["xbt_01"]
    assert records["ETH_USD"][0].config["ticker"] == "ETH/USD"
    assert records["XBT_USD"][0].config["ticker"] == "XBT/USD"


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------
def test_candles_to_dataframe_shape_and_dtypes():
    candles = [
        _candle(1_700_000_000, 2000.5, "150.25", 7),
        _candle(1_700_003_600, 2010.0, "160.5", 9),
    ]
    df = candles_to_dataframe(candles)

    assert list(df.columns) == [
        "time",
        "open",
        "high",
        "low",
        "close",
        "vwap",
        "volume",
        "count",
    ]
    assert len(df) == 2
    assert df["close"].dtype == np.float64
    assert df["open"].dtype == np.float64
    assert df["volume"].dtype == np.float64
    assert df["count"].dtype == np.int64
    assert df.index.is_monotonic_increasing


def test_fetch_ohlc_dataframe_pagination_two_pages_then_end():
    page1 = [_candle(1_700_000_000, 1.0), _candle(1_700_003_600, 2.0)]
    page2 = [_candle(1_700_007_200, 3.0)]

    class MockManager:
        def __init__(self):
            self.calls: list[int | None] = []

        def ohlc(self, pair, interval, since=None):
            self.calls.append(since)
            if len(self.calls) == 1:
                return page1, 11111
            return page2, 0  # exchange says history exhausted

    mgr = MockManager()
    df = fetch_ohlc_dataframe("ETH/USD", 60, pages=5, manager=mgr)

    assert len(df) == 3
    # Cursor sequence: first call with no `since`, second with page1's `last`.
    assert mgr.calls == [None, 11111]


def test_fetch_ohlc_dataframe_not_enough_data():
    class EmptyManager:
        def ohlc(self, pair, interval, since=None):
            return [], 0

    with pytest.raises(NotEnoughDataError, match="Not enough"):
        fetch_ohlc_dataframe("ETH/USD", 60, pages=2, manager=EmptyManager())


def test_prepare_episode_slices_window_and_fits_stats():
    df = _synthetic_ohlcv(200)
    features = FeaturePipeline(windows=[1, 4, 24])
    window = prepare_episode(df, features, ticker_id="ETH/USD", episode_bars=100)

    assert len(window) == 100
    # Per-ticker stats keyed by the normalized id the env will look up.
    assert features.stats_for("ETH_USD") is not None
    assert normalize_ticker_id("ETH/USD") == "ETH_USD"


def test_prepare_episode_not_enough_data():
    features = FeaturePipeline(windows=[1, 4, 24])
    small = _synthetic_ohlcv(5)
    with pytest.raises(NotEnoughDataError, match="Not enough"):
        prepare_episode(small, features, ticker_id="ETH/USD")


def test_pair_from_ticker_id():
    assert pair_from_ticker_id("ETH_USD") == "ETH/USD"
    assert pair_from_ticker_id("ETH/USD") == "ETH/USD"


# ---------------------------------------------------------------------------
# PPO agent
# ---------------------------------------------------------------------------
def test_agent_train_save_load_predict(tmp_path):
    df = _synthetic_ohlcv(60)
    # Builtin fallback features: no FeaturePipeline needed at construct.
    env = TradingEnvironment("ETH/USD", df)

    agent = RLAgent("ETH/USD", "ppo_agent", models_root=tmp_path, seed=1)
    path = agent.train(env, total_timesteps=200)

    assert path.exists()
    assert path.name == "model.zip"
    assert path == tmp_path / "ETH_USD" / "ppo_agent" / "model.zip"

    # Reload into a fresh env with the same spaces and predict a valid action.
    env2 = TradingEnvironment("ETH/USD", df)
    loaded = RLAgent.load("ETH/USD", "ppo_agent", env2, models_root=tmp_path)
    obs, _ = env2.reset()
    action = loaded.predict(obs)
    assert env2.action_space.contains(action)


def test_agent_predict_without_policy_raises():
    agent = RLAgent("ETH/USD", "untrained")
    with pytest.raises(RuntimeError):
        agent.predict(np.zeros(8, dtype=np.float32))


# ---------------------------------------------------------------------------
# training orchestrator + backtest
# ---------------------------------------------------------------------------
def test_train_ticker_end_to_end(tmp_path):
    base = int(pd.Timestamp("2024-01-01T00:00:00Z").timestamp())
    n = 120
    closes = 2000.0 * np.exp(
        np.cumsum(np.random.default_rng(0).normal(0.0, 0.01, n))
    )
    candles = [_candle(base + i * 3600, float(closes[i]), "150.0", 5) for i in range(n)]

    class OneShotManager:
        def __init__(self):
            self.calls: list[int | None] = []

        def ohlc(self, pair, interval, since=None):
            self.calls.append(since)
            return candles, 0

    record = train_ticker(
        "ETH/USD",
        "ppo_train",
        manager=OneShotManager(),
        config_path=_default_config_without_signal_files(
            tmp_path / "default-no-signals.yaml"
        ),
        pages=2,
        total_timesteps=150,
        seed=7,
        models_root=tmp_path,
    )

    # All three artifacts persisted under the per-ticker model dir.
    assert record.model_path is not None and record.model_path.is_file()
    assert record.normalization_path is not None and record.normalization_path.is_file()
    assert record.config_path is not None

    cfg = yaml.safe_load(record.config_path.read_text(encoding="utf-8"))
    assert cfg["ticker"] == "ETH/USD"
    assert cfg["model_name"] == "ppo_train"

    records = list_models(root=tmp_path)
    assert "ETH_USD" in records
    assert records["ETH_USD"][0].is_trained()

    # Backtest the registry-loaded model over fresh data (pipeline path;
    # normalization.npz present -> environment features rebuilt from config).
    fresh = _synthetic_ohlcv(120, seed=99)
    result = backtest_model("ETH/USD", "ppo_train", data=fresh, models_root=tmp_path)
    assert isinstance(result, BacktestResult)
    assert len(result.equity_curve) >= 2


def test_backtest_model_synthetic_walk(tmp_path):
    df = _synthetic_ohlcv(120)
    env = TradingEnvironment("ETH/USD", df)
    agent = RLAgent("ETH/USD", "ppo_bt", models_root=tmp_path, seed=3)
    agent.train(env, total_timesteps=200)

    result = backtest_model(
        "ETH/USD", "ppo_bt", data=df, agent=agent, models_root=tmp_path, seed=42
    )

    assert isinstance(result, BacktestResult)
    d = result.to_dict()
    for key in (
        "total_return",
        "sharpe",
        "max_drawdown",
        "num_trades",
        "win_rate",
        "equity_curve",
    ):
        assert key in d
    assert len(result.equity_curve) >= 2
    assert result.n_steps >= 1
    # Return/drawdown are sane bounded values.
    assert -1.0 <= result.total_return < 10.0
    assert 0.0 <= result.max_drawdown <= 1.0


def test_backtest_env_applies_loaded_normalization(tmp_path, monkeypatch):
    """The backtest call site hands the env the *loaded* npz stats.

    ``backtest_model`` builds a ``FeaturePipeline`` from the saved config
    and calls ``load_normalization`` before constructing the environment.
    This spies on the environment the call site actually builds and
    asserts its observation matrix is the z-scored transform — not the raw
    ffilled features, and not a fresh in-sample refit over the replay
    frame.
    """
    import kraken_trading_bot.rl.backtest as bt_mod

    base = int(pd.Timestamp("2024-01-01T00:00:00Z").timestamp())
    n = 300
    closes = 2000.0 * np.exp(np.cumsum(np.random.default_rng(0).normal(0.0, 0.01, n)))
    candles = [_candle(base + i * 3600, float(closes[i]), "150.0", 5) for i in range(n)]

    class OneShotManager:
        def ohlc(self, pair, interval, since=None):
            return candles, 0

    train = train_ticker(
        "ETH/USD",
        "ppo_norm",
        manager=OneShotManager(),
        config_path=_default_config_without_signal_files(
            tmp_path / "default-no-signals.yaml"
        ),
        pages=2,
        total_timesteps=200,
        seed=5,
        models_root=tmp_path,
    )
    assert train.is_trained()

    built: list[TradingEnvironment] = []
    real_env_cls = bt_mod.TradingEnvironment

    def spy(*args, **kwargs):
        env = real_env_cls(*args, **kwargs)
        built.append(env)
        return env

    monkeypatch.setattr(bt_mod, "TradingEnvironment", spy)

    fresh = _synthetic_ohlcv(150, seed=77)
    backtest_model("ETH/USD", "ppo_norm", data=fresh, models_root=tmp_path)

    assert len(built) == 1
    env = built[0]
    stats = env.normalization_stats()
    assert stats is not None
    # Stats came off disk, keyed by the normalized ticker id.
    assert stats.ticker_id == "ETH_USD"
    assert stats.feature_names

    # The env's matrix is exactly the saved stats applied to its own frame.
    expected = env.pipeline.transform(env.data, ticker_id="ETH_USD")
    np.testing.assert_allclose(
        env._raw_feature_array(), expected, rtol=1e-5, atol=1e-6
    )
    raw = env.pipeline.compute(env.data).ffill().fillna(0.0).to_numpy(dtype=np.float32)
    assert not np.allclose(env._raw_feature_array(), raw)


def test_load_train_config_falls_back_to_cwd_when_store_path_missing(
    tmp_path, monkeypatch
):
    """The packaged console script imports this module from the Nix store,
    where ``configs/`` is not installed, so the module-relative default
    path does not exist.  :func:`load_train_config` must then fall back to
    ``./configs/default.yaml`` under the current working directory (the
    justfile and ``nix develop`` both run from the repo root) instead of
    silently returning ``{}``.
    """
    import kraken_trading_bot.rl.train as train_module

    monkeypatch.setattr(
        train_module, "_DEFAULT_CONFIG_PATH", tmp_path / "absent" / "default.yaml"
    )
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    cfg = train_module.load_train_config()
    assert cfg, "expected the repo-root configs/default.yaml to be picked up"
    assert cfg["ticker"] == "ETH/USD"
    assert cfg["action_space"] == "continuous"
    assert cfg["reward"]["mode"] == "pnl"


def test_load_train_config_empty_when_no_candidate_exists(tmp_path, monkeypatch):
    """With neither the module-relative nor the CWD config present the
    loader stays backward-compatible and returns an empty dict."""
    import kraken_trading_bot.rl.train as train_module

    monkeypatch.setattr(
        train_module, "_DEFAULT_CONFIG_PATH", tmp_path / "absent" / "default.yaml"
    )
    monkeypatch.chdir(tmp_path)

    assert train_module.load_train_config() == {}


# ---------------------------------------------------------------------------
# the shipped default's signal channels
# ---------------------------------------------------------------------------
# `configs/default.yaml` configures three signal files under
# `~/Projects/kraken-trading-bot/signals/`, and the merge seam refuses a
# *configured* path it cannot resolve — naming the key, the value as written
# and the expanded path.  That is the intended behaviour (and is itself
# pinned, by `tests/test_gc_channel_activation.py::
# test_non_null_key_without_a_file_refuses`), but these tests are about the
# training orchestration, not about the channels, so they are handed a copy of
# the shipped config with those keys nulled (`null` = off = silent).  The
# channels are covered for real — fake `HOME`, a `~`-spelled path, and the
# seam asserted to have *consumed* the file — in
# `tests/test_rl_signal_config_wiring.py` and `tests/test_rl_data_store.py`.
def _default_config_without_signal_files(dest: Path) -> Path:
    """Write a copy of ``configs/default.yaml`` with all three channel keys null.

    The shipped config configures three signal files under
    `~/Projects/kraken-trading-bot/signals/`, and that directory is
    gitignored (.gitignore:65), so on any checkout without the timers
    installed the merge seam refuses a *configured* path it cannot resolve
    (naming the key, the raw value and the expanded path).  That refusal is
    the intended behaviour — it is what
    ``tests/test_gc_channel_activation.py::test_non_null_key_without_a_file_refuses``
    pins — but these tests are about the training orchestration, not about the signal
    channels, so they run against the shipped config with those keys nulled.
    ``null`` means off and is silent, which is precisely the distinction the
    non-null branch exists to make.  All three channels are covered for real
    (fake ``HOME``, a ``~``-spelled path, and the seam asserted to have
    *consumed* the file) in ``tests/test_rl_signal_config_wiring.py`` and
    ``tests/test_rl_data_store.py``.
    """
    repo = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load(
        (repo / "configs" / "default.yaml").read_text(encoding="utf-8")
    )
    for key in (
        "extra_features_file",
        "funding_features_file",
        "social_features_file",
    ):
        cfg[key] = None
    dest.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# Gap-1 widening: n_features provenance, written and surfaced
# ---------------------------------------------------------------------------
def test_train_ticker_records_n_features_in_config(tmp_path):
    """Width provenance lands in config.yaml, so a stale artifact is detectable.

    The six on-disk 49-wide artifacts are exactly what this makes visible:
    a model with no ``n_features`` key cannot be compared against the live
    pipeline's width at all.
    """
    base = int(pd.Timestamp("2024-01-01T00:00:00Z").timestamp())
    n = 120
    closes = 2000.0 * np.exp(
        np.cumsum(np.random.default_rng(0).normal(0.0, 0.01, n))
    )
    candles = [_candle(base + i * 3600, float(closes[i]), "150.0", 5) for i in range(n)]

    class OneShotManager:
        def ohlc(self, pair, interval=60, since=None):
            return candles, 0

    record = train_ticker(
        "ETH/USD",
        "ppo_width",
        manager=OneShotManager(),
        config_path=_default_config_without_signal_files(
            tmp_path / "default-no-signals.yaml"
        ),
        pages=2,
        total_timesteps=150,
        seed=7,
        models_root=tmp_path,
    )

    cfg = yaml.safe_load(record.config_path.read_text(encoding="utf-8"))
    assert cfg["n_features"] == 52  # 49 base + the three vwap/count scalars

    # Surfaced by the registry summary, and not stale against the live width.
    assert record.config_summary()["n_features"] == 52
    assert record.n_features == 52
    assert record.is_stale_width(52) is False
    assert record.is_stale_width(49) is True


def test_registry_reports_a_pre_provenance_artifact_as_stale(tmp_path):
    """A config without ``n_features`` is untrustworthy, not known-good."""
    record = register_model(
        "ETH/USD", "ppo_legacy", {"ticker": "ETH/USD"}, root=tmp_path
    )
    assert record.n_features is None
    assert record.config_summary()["n_features"] is None
    # Unknown width counts as stale: it can never be shown to match.
    assert record.is_stale_width(52) is True


def test_backtest_refuses_a_stale_width(tmp_path, monkeypatch):
    """backtest_model now has a guard (it previously had none at all).

    The mismatch is a widened *live* pipeline against the model's own
    52-feature npz, which is the direction that used to surface late and
    silently -- ``normalize`` dropped the extra column and the policy ran
    happily.
    """
    import kraken_trading_bot.rl.backtest as bt_mod

    base = int(pd.Timestamp("2024-01-01T00:00:00Z").timestamp())
    n = 200
    closes = 2000.0 * np.exp(np.cumsum(np.random.default_rng(1).normal(0.0, 0.01, n)))
    candles = [_candle(base + i * 3600, float(closes[i]), "150.0", 5) for i in range(n)]

    class OneShotManager:
        def ohlc(self, pair, interval=60, since=None):
            return candles, 0

    train = train_ticker(
        "ETH/USD",
        "ppo_stale",
        manager=OneShotManager(),
        config_path=_default_config_without_signal_files(
            tmp_path / "default-no-signals.yaml"
        ),
        pages=2,
        total_timesteps=150,
        seed=5,
        models_root=tmp_path,
    )
    assert train.is_trained()

    # Simulate a *widened* pipeline: a feature the model never saw.
    real_compute = FeaturePipeline.compute

    def widened(self, df):
        out = real_compute(self, df)
        out["a_feature_from_the_future"] = 1.0
        return out

    monkeypatch.setattr(FeaturePipeline, "compute", widened)

    with pytest.raises(FeatureWidthMismatchError, match="a_feature_from_the_future"):
        bt_mod.backtest_model(
            "ETH/USD",
            "ppo_stale",
            data=_synthetic_ohlcv(150, seed=3),
            models_root=tmp_path,
        )
