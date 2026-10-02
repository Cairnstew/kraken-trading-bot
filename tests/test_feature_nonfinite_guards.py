"""Non-finite feature guards: one bad bar must not poison the episode.

The measured failure this pins, from the deep-history store arm of the
2026-10-02 data-pipeline pass. The seeded Binance archive carries a
zero-volume bar at each partial-month boundary (4 across 2018-2026: the
2019-06, 2020-12, 2021-02 and 2023-03 files). ``volume_change_1`` was
``volume.pct_change()``, which divides by the PRIOR bar's volume, so a
zero prior bar yields ``inf`` -- and ``inf`` is the one value the
``compute -> ffill -> fillna(0)`` observation policy cannot repair.
``FeaturePipeline.fit`` then recorded that column's stats as
``(mean=inf, std=nan)``; ``NormalizationStats.normalize`` subtracts the
mean from every row, so EVERY observation row became ``-inf``/NaN and PPO
died on its own distribution constraint:

    Expected parameter loc (Tensor of shape (64, 3)) of distribution
    Normal(loc: torch.Size([64, 3]), scale: torch.Size([64, 3])) to
    satisfy the constraint Real(), but found invalid values

The live arm (721 Kraken bars) had no zero-volume bar and trained fine,
so the defect was invisible until the store arm was switched on.

Two things are pinned here, and they are deliberately separate:

1. the builders never EMIT a non-finite value for a zero bar -- they emit
   NaN, matching the ``.replace(0, np.nan)`` convention every other
   division in this module already used; and
2. ``fit`` refuses to persist a non-finite stat even if some future or
   exogenous column still manages to produce one, so the
   "transform output is an affine image of the observation" invariant is
   enforced rather than assumed.

These tests need no network, no store and no sibling package.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from kraken_trading_bot.rl.features import FeaturePipeline


def _frame(n: int = 400, zero_volume_at: tuple[int, ...] = (137,)) -> pd.DataFrame:
    """A healthy OHLCV frame with a zero-volume bar at each index given.

    Zero volume is what the seeded archive's partial-month rows carry;
    everything else is a clean geometric ramp so no indicator is
    degenerate for an unrelated reason.
    """
    idx = pd.date_range("2021-01-01", periods=n, freq="1h", tz="UTC")
    close = 100.0 * (1.001 ** np.arange(n))
    vol = np.full(n, 1000.0)
    for i in zero_volume_at:
        vol[i] = 0.0
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "vwap": close,
            "volume": vol,
            "count": 42.0,
        },
        index=idx,
    )


def _pipeline() -> FeaturePipeline:
    return FeaturePipeline(
        windows=[1, 4, 24],
        feature_groups=["price", "technical", "volume", "microstructure", "signals"],
    )


def test_zero_volume_bar_does_not_emit_inf_from_volume_change():
    """The trigger: pct_change over a zero prior bar is inf, not NaN."""
    df = _frame(zero_volume_at=(137,))
    raw = _pipeline().compute(df)

    assert "volume_change_1" in raw.columns
    values = pd.to_numeric(raw["volume_change_1"], errors="coerce").to_numpy(
        dtype=np.float64
    )
    assert not np.isinf(values).any(), (
        "volume_change_1 emitted inf; the observation fill policy "
        "(compute -> ffill -> fillna(0)) cannot repair inf"
    )


def test_zero_close_bar_does_not_emit_inf_from_returns():
    """Same guard on the price group, which divides by close the same way."""
    df = _frame()
    df.loc[df.index[200], "close"] = 0.0
    df.loc[df.index[200], "open"] = 0.0
    df.loc[df.index[200], "high"] = 0.0
    df.loc[df.index[200], "low"] = 0.0

    raw = _pipeline().compute(df)
    for col in ("return_1", "return_4", "return_24", "log_return_1"):
        if col not in raw.columns:
            continue
        values = pd.to_numeric(raw[col], errors="coerce").to_numpy(dtype=np.float64)
        assert not np.isinf(values).any(), f"{col} emitted inf from a zero close"


def test_fitted_stats_are_finite_with_a_zero_volume_bar():
    """The invariant: no column's stats may be inf/nan, ever."""
    df = _frame(zero_volume_at=(137, 250))
    pipe = _pipeline()
    pipe.fit(df, ticker_id="ETH_USD")

    stats = pipe.stats_for("ETH_USD")
    assert stats is not None
    bad = {
        name: pair
        for name, pair in stats.stats.items()
        if not np.isfinite(pair[0]) or not np.isfinite(pair[1])
    }
    assert not bad, f"non-finite normalization stats persisted: {bad}"


def test_every_observation_row_is_finite_with_a_zero_volume_bar():
    """The end-to-end claim: one bad bar must not cost the whole episode.

    This is the assertion whose absence let PPO train on 36 804 rows of
    all-inf observations and then die on them.
    """
    df = _frame(zero_volume_at=(137, 250))
    pipe = _pipeline()
    pipe.fit(df, ticker_id="ETH_USD")
    obs = pipe.transform(df, ticker_id="ETH_USD")

    assert obs.shape[0] == len(df)
    assert np.isfinite(obs).all(), (
        f"{int((~np.isfinite(obs)).sum())} non-finite cells in the observation"
    )


def test_a_constant_column_normalizes_to_zero_not_to_inf():
    """A column with no usable scale is pinned flat, per the std floor."""
    df = _frame()
    df["volume"] = 7.0  # constant volume: pct_change and zscore degenerate
    pipe = _pipeline()
    pipe.fit(df, ticker_id="ETH_USD")
    obs = pipe.transform(df, ticker_id="ETH_USD")
    assert np.isfinite(obs).all()


def test_fit_rejects_a_frame_with_no_usable_scale_at_all():
    """Even an all-NaN raw column must not yield a non-finite stat."""
    df = _frame()
    df["volume"] = np.nan
    pipe = _pipeline()
    pipe.fit(df, ticker_id="ETH_USD")
    stats = pipe.stats_for("ETH_USD")
    assert stats is not None
    for name, (mean, std) in stats.stats.items():
        assert np.isfinite(mean), f"{name} mean is not finite"
        assert np.isfinite(std), f"{name} std is not finite"


@pytest.mark.parametrize("zero_at", [(0,), (1,), (137,)])
def test_zero_volume_at_any_position_stays_finite(zero_at):
    """Position-independent: the guard is in the builder, not in indexing."""
    df = _frame(zero_volume_at=zero_at)
    pipe = _pipeline()
    pipe.fit(df, ticker_id="ETH_USD")
    assert np.isfinite(pipe.transform(df, ticker_id="ETH_USD")).all()
