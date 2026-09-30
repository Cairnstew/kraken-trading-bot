"""Tests for the data-pipeline CSV export (``export-data``).

The export composes the same stages ``train_ticker`` runs — config
resolution, OHLC read, signal merges, feature fit and episode slice —
so these tests prove that composition against a fake Kraken manager:
no network, no credentials, and no writes outside pytest's ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from kraken_api.models import Candle

from kraken_trading_bot.rl import NotEnoughDataError
from kraken_trading_bot.rl.export import (
    build_export_frame,
    default_export_path,
    write_export_csv,
)

# 2026-08-01 12:00 UTC — the anchor the store tests already use.
_BASE = 1785585600
_HOUR = 3600


def _candles(n: int = 120, seed: int = 7) -> list[Candle]:
    """Hourly bars with a wandering close, so indicators actually fill."""
    rng = np.random.default_rng(seed)
    closes = 100.0 + np.cumsum(rng.normal(0.0, 0.7, size=n))
    return [
        Candle(
            pair="SOL/USD",
            time=_BASE + i * _HOUR,
            open=f"{close:.4f}",
            high=f"{close * 1.002:.4f}",
            low=f"{close * 0.998:.4f}",
            close=f"{close:.4f}",
            vwap=f"{close:.4f}",
            volume=f"{10.0 + i % 5}",
            count=5,
        )
        for i, close in enumerate(closes)
    ]


class FakeManager:
    """Kraken-compatible manager returning one canned page.

    ``last = 0`` tells the paginator history is exhausted, so ``pages``
    never fetches twice.
    """

    def __init__(self, candles: list[Candle]) -> None:
        self.candles = list(candles)
        self.calls = 0

    def ohlc(self, pair: str, interval: int = 60, since: int | None = None):
        self.calls += 1
        return self.candles, 0


@pytest.fixture()
def manager() -> FakeManager:
    return FakeManager(_candles())


def test_build_export_frame_stage_column_order(manager):
    """Columns arrive in pipeline order, with the stage split recorded."""
    frame = build_export_frame("SOL_USD", manager=manager, pages=1)

    stages = frame.attrs["stages"]
    assert stages["ohlcv"] == [
        "time", "open", "high", "low", "close", "vwap", "volume", "count",
    ]
    assert stages["signals"] == []  # no exogenous signals configured
    assert stages["normalized"] == []
    assert len(stages["features"]) == 49  # default windows/groups

    assert list(frame.columns) == [
        "timestamp",
        *stages["ohlcv"],
        *stages["signals"],
        *stages["features"],
        "warmup",
    ]
    assert len(frame) == 120
    assert manager.calls == 1  # single page, no live re-fetch


def test_build_export_frame_observations_are_filled(manager):
    """The observation block is the env's ffill/fillna(0) matrix, finite."""
    frame = build_export_frame("SOL_USD", manager=manager, pages=1)
    features = frame[frame.attrs["stages"]["features"]]

    assert not features.isna().any().any()
    assert np.isfinite(features.to_numpy(dtype=np.float64)).all()


def test_build_export_frame_timestamps_are_iso_utc(manager):
    frame = build_export_frame("SOL_USD", manager=manager, pages=1)

    assert frame["timestamp"].iloc[0] == "2026-08-01T12:00:00Z"
    assert frame["timestamp"].iloc[-1] == "2026-08-06T11:00:00Z"  # 120 hourly bars
    # The frame's own epoch column travels alongside, unrenamed.
    assert frame["time"].iloc[0] == _BASE


def test_build_export_frame_flags_warmup_rows(manager):
    """Look-back rows carry warmup=True; the traded region does not."""
    frame = build_export_frame("SOL_USD", manager=manager, pages=1)

    assert frame["warmup"].dtype == bool
    assert bool(frame["warmup"].iloc[0]) is True
    assert bool(frame["warmup"].iloc[-1]) is False
    # Trading starts at the first fully-populated row, as the env does.
    first_trade = int(np.argmax(~frame["warmup"].to_numpy()))
    assert first_trade > 0
    assert not frame["warmup"].iloc[first_trade]


def test_build_export_frame_episode_bars_keeps_tail(manager):
    full = build_export_frame("SOL_USD", manager=manager, pages=1)
    sliced = build_export_frame("SOL_USD", manager=manager, pages=1, episode_bars=50)

    assert len(sliced) == 50
    assert list(sliced["timestamp"]) == list(full["timestamp"])[-50:]


def test_build_export_frame_normalized_block_is_z_scored(manager):
    """--normalized adds z_-columns distinct from the raw observation."""
    raw = build_export_frame("SOL_USD", manager=manager, pages=1)
    zframe = build_export_frame(
        "SOL_USD", manager=manager, pages=1, include_normalized=True
    )
    stages = zframe.attrs["stages"]

    assert len(stages["normalized"]) == len(stages["features"])
    assert all(name.startswith("z_") for name in stages["normalized"])
    assert list(zframe.columns) == [*raw.columns, *stages["normalized"]]

    zblock = zframe[stages["normalized"]]
    assert not zblock.isna().any().any()
    assert not np.allclose(zblock.to_numpy(), zframe[stages["features"]].to_numpy())

    # Each z-column equals (raw - mean) / std over that column's non-NaN
    # rows, which is what FeaturePipeline.fit records — so the block is a
    # faithful render of normalization.npz, not a second normalization.
    raw_feat = zframe[stages["features"]]
    series = raw_feat["return_1"]
    mean, std = float(series.mean()), float(series.std(ddof=0))
    expected = (series - mean) / (std if std > 1e-12 else 1.0)
    present = series.notna()
    assert np.allclose(
        zframe.loc[present, "z_return_1"].to_numpy(),
        expected[present].to_numpy(),
        atol=1e-4,
    )


def test_build_export_frame_honours_config(tmp_path, manager):
    """Config knobs reach the pipeline, not just the fetch."""
    config = tmp_path / "cfg.yaml"
    config.write_text("feature_windows: [1, 4]\nfeature_groups: [price, volume]\n")

    frame = build_export_frame(
        "SOL_USD", config_path=config, manager=manager, pages=1
    )
    names = frame.attrs["stages"]["features"]

    assert not any(name.endswith("_24") for name in names)  # windows
    assert not any(name.startswith("sma_") for name in names)  # groups
    assert "return_4" in names and "volume_zscore_20" in names


def test_build_export_frame_rejects_too_little_data():
    short = FakeManager(_candles(10))
    with pytest.raises(NotEnoughDataError):
        build_export_frame("SOL_USD", manager=short, pages=1)


def test_default_export_path():
    assert default_export_path("SOL_USD") == Path("exports/SOL_USD.csv")
    assert default_export_path("SOL_USD", root="out") == Path("out/SOL_USD.csv")


def test_write_export_csv_creates_dirs_and_roundtrips(tmp_path, manager):
    frame = build_export_frame(
        "SOL_USD", manager=manager, pages=1, episode_bars=40
    )
    destination = write_export_csv(frame, tmp_path / "nested" / "SOL_USD.csv")

    assert destination.exists()
    back = pd.read_csv(destination)
    assert list(back.columns) == list(frame.columns)
    assert len(back) == 40
    assert back["timestamp"].iloc[-1] == frame["timestamp"].iloc[-1]
    assert back["close"].iloc[-1] == pytest.approx(float(frame["close"].iloc[-1]))


def test_normalized_block_equals_environment_observation(manager, monkeypatch):
    """The ``z_`` block IS the environment's observation, row for row.

    Fourth leg of the normalization contract: the other three consumers
    (``TradingEnvironment._raw_feature_array``,
    ``PaperTrader._build_observation``, and the backtest env) are pinned to
    ``FeaturePipeline.transform``.  Here we capture the exact episode and
    pipeline ``build_export_frame`` hands the environment, build that
    environment, and assert its observation matrix equals the exported
    ``z_*`` block -- so ``export-data --normalized`` is a faithful render
    of what the policy actually sees, not a second, independent
    normalization.
    """
    import kraken_trading_bot.rl.export as export_mod
    from kraken_trading_bot.rl import TradingEnvironment

    captured: dict[str, object] = {}
    real_prepare = export_mod.prepare_episode

    def spy_prepare(df, features, **kwargs):
        episode = real_prepare(df, features, **kwargs)
        captured["episode"] = episode
        captured["pipeline"] = features
        return episode

    monkeypatch.setattr(export_mod, "prepare_episode", spy_prepare)

    frame = build_export_frame("SOL_USD", manager=manager, pages=1, include_normalized=True)
    stages = frame.attrs["stages"]

    env = TradingEnvironment(
        "SOL_USD",
        data=captured["episode"],
        feature_pipeline=captured["pipeline"],
    )
    matrix = env._raw_feature_array()
    zblock = frame[stages["normalized"]].to_numpy(dtype=np.float32)

    assert matrix.shape == zblock.shape
    np.testing.assert_allclose(matrix, zblock, rtol=1e-5, atol=1e-5)

    # The pre-transform feature block is genuinely different.
    assert not np.allclose(matrix, frame[stages["features"]].to_numpy(dtype=np.float32))
