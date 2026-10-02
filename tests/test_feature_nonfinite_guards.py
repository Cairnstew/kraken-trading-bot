"""Non-finite feature guards: one bad bar must not poison the episode.

The measured failure this pins, from the deep-history store arm of the
2026-10-02 data-pipeline pass. The seeded Binance archive carries
**4 zero-volume bars** in 2018-2026 (``2019-06-07 21:00``,
``2020-12-21 14:00``, ``2021-02-11 03:00``, ``2023-03-24 12:00``).
``volume_change_1`` was ``volume.pct_change()``, which divides by the
PRIOR bar's volume, so a zero prior bar yields ``inf`` -- and ``inf`` is
the one value the ``compute -> ffill -> fillna(0)`` observation policy
cannot repair.  ``FeaturePipeline.fit`` then recorded that column's stats
as ``(mean=inf, std=nan)``; ``NormalizationStats.normalize`` subtracts the
mean from every row, so EVERY observation row became ``-inf``/NaN and PPO
died on its own distribution constraint:

    Expected parameter loc (Tensor of shape (64, 3)) of distribution
    Normal(loc: torch.Size([64, 3]), scale: torch.Size([64, 3])) to
    satisfy the constraint Real(), but found invalid values

The live arm (721 Kraken bars) had no zero-volume bar and trained fine,
so the defect was invisible until the store arm was switched on.

**Correction (2026-10-02, Phase 6 finding F4).** These bars were
originally described here, and in ``DECISION.md`` §9A.3, as sitting "at
each partial-month boundary".  That was **factually wrong** and this
description of the cause has been replaced.  All 4 are **mid-month**;
**0 of the 106 month-first bars** in the archive are zero-volume; and 2 of
the 4 (2020-12-21 14:00, 2021-02-11 03:00) sit *immediately before* a
missing-bar gap.  They are **exchange-outage no-trade bars**, not
archive-boundary artifacts -- a materially different diagnosis, pointing
at exchange data integrity rather than at the Binance monthly file format.
(The zero-volume *symptom* and the fix below are unaffected; only the
attributed cause was wrong.)

Two things are pinned here, and they are deliberately separate:

1. the builders never EMIT a non-finite value for a zero bar -- they emit
   NaN, matching the ``.replace(0, np.nan)`` convention every other
   division in this module already used; and
2. ``fit`` refuses to persist a non-finite stat even if some future or
   exogenous column still manages to produce one, so the
   "transform output is an affine image of the observation" invariant is
   enforced rather than assumed.

Read the module-level seam note in ``kraken_trading_bot/rl/features.py``
before adding a guard: ``rolling`` **masks** a non-finite input to NaN
while ``ewm`` **skips** it and returns a finite-but-wrong value, and that
difference is what the last three tests in this file exist to pin.

These tests need no network, no store and no sibling package.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from kraken_trading_bot.rl.features import FeaturePipeline


def _frame(n: int = 400, zero_volume_at: tuple[int, ...] = (137,)) -> pd.DataFrame:
    """A healthy OHLCV frame with a zero-volume bar at each index given.

    Zero volume is what the seeded archive's exchange-outage no-trade bars
    carry; everything else is a clean geometric ramp so no indicator is
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


# ----------------------------------------------------------------------
# Part 2 -- added by the 2026-10-02 finishing pass.
#
# The tests above pin the ORIGINAL bug (one zero-volume bar poisoning the
# episode).  The tests below pin the whole guard surface: every division,
# log and ratio the module computes, the two ``_require_finite`` gates, the
# error type's place in the hierarchy, and the one site where zero is DATA
# rather than a denominator.
# ----------------------------------------------------------------------
from kraken_trading_bot.rl.features import (  # noqa: E402
    FeaturePipeline,
    NonFiniteFeatureError,
    NormalizationStats,
    _bollinger,
    _rsi,
)


def _observation(df: pd.DataFrame, ticker_id: str = "ETH_USD") -> np.ndarray:
    """fit + transform on a private pipeline; the matrix the policy sees."""
    pipe = _pipeline()
    pipe.fit(df, ticker_id=ticker_id)
    return pipe.transform(df, ticker_id=ticker_id)


# ----------------------------------------------------------------------
# 2a. one test per division / log / ratio site
# ----------------------------------------------------------------------
def test_pct_change_sites_stay_finite_on_a_zero_denominator():
    """``pct_change`` divides by the PRIOR bar -- three sites use it.

    ``return_1`` and ``return_{w}`` divide by a prior *close*;
    ``volume_change_1`` divides by a prior *volume*.  A zero at the
    denominator bar is the trigger for all three.
    """
    df = _frame(zero_volume_at=(137,))
    df.loc[df.index[200], "close"] = 0.0

    raw = _pipeline().compute(df)
    for col in ("return_1", "return_4", "return_24", "volume_change_1"):
        values = pd.to_numeric(raw[col], errors="coerce").to_numpy(dtype=np.float64)
        assert not np.isinf(values).any(), f"{col} emitted inf (0 denominator)"
    assert np.isfinite(_observation(df)).all()


def test_log_return_numerator_is_guarded_log_zero_is_minus_inf():
    """The specific omission the earlier note flagged.

    ``np.log(x)`` is ``-inf`` at ``x == 0``, so guarding only the
    DENOMINATOR of ``log(c_t / c_{t-w})`` is not enough: a zero *current*
    close makes the numerator zero and ``log(0)`` poisons the column
    exactly as a zero prior close poisons the denominator.  Both sides
    must be guarded, and ``log_return_*`` must come back finite.
    """
    df = _frame()
    # A zero close at a bar that is also the base of a 4-bar lookback.
    df.loc[df.index[200], "close"] = 0.0
    df.loc[df.index[200], "open"] = 0.0
    df.loc[df.index[200], "high"] = 0.0
    df.loc[df.index[200], "low"] = 0.0

    raw = _pipeline().compute(df)
    for col in ("log_return_1", "log_return_4", "log_return_24"):
        values = pd.to_numeric(raw[col], errors="coerce").to_numpy(dtype=np.float64)
        assert not np.isinf(values).any(), (
            f"{col} emitted inf: the log NUMERATOR is unguarded, so log(0) = -inf"
        )
    assert np.isfinite(_observation(df)).all()


def test_log_return_numerator_guards_an_infinite_close():
    """Same numerator, infinite rather than zero: ``log(inf)`` is ``+inf``."""
    df = _frame()
    df.loc[df.index[200], "close"] = np.inf

    raw = _pipeline().compute(df)
    values = pd.to_numeric(raw["log_return_1"], errors="coerce").to_numpy(
        dtype=np.float64
    )
    assert not np.isinf(values).any(), "log_return_1 emitted inf from an inf close"
    assert np.isfinite(_observation(df)).all()


def test_ratio_sites_stay_finite_on_a_zero_denominator():
    """The ratio family: ``range_1`` and ``price_ratio_sma_{w}``.

    ``range_1`` is a ratio of two series (so BOTH sides need the guard) and
    ``price_ratio_sma_{w}`` divides by a rolling mean.  A zero close
    degenerates the ``range_1`` DENOMINATOR and the ``price_ratio``
    NUMERATOR -- the latter yielding a finite but meaningless ``-1.0``
    that reads as a 100% discount to the moving average.
    """
    df = _frame()
    df.loc[df.index[200], "close"] = 0.0
    df.loc[df.index[200], "open"] = 0.0
    df.loc[df.index[200], "high"] = 0.0
    df.loc[df.index[200], "low"] = 0.0

    raw = _pipeline().compute(df)
    assert "range_1" in raw.columns and "price_ratio_sma_4" in raw.columns
    for col in ("range_1", "price_ratio_sma_1", "price_ratio_sma_4"):
        values = pd.to_numeric(raw[col], errors="coerce").to_numpy(dtype=np.float64)
        assert not np.isinf(values).any(), f"{col} emitted inf"
    assert np.isfinite(_observation(df)).all()


def test_microstructure_ratios_stay_finite_on_a_zero_bid():
    """``spread`` (bid/ask) and ``order_book_imbalance`` are both ratios.

    A zero bid is the shared zero denominator; an infinite ask or volume
    is the infinite-NUMERATOR case, since ``inf - bid`` is still inf even
    with a healthy denominator.
    """
    df = _frame()
    df["bid"] = 100.0
    df["ask"] = 100.5
    df["bid_vol"] = 10.0
    df["ask_vol"] = 12.0
    df.loc[df.index[300], "bid"] = 0.0
    df.loc[df.index[301], "ask"] = np.inf
    df.loc[df.index[302], "bid_vol"] = np.inf

    raw = _pipeline().compute(df)
    for col in ("spread", "order_book_imbalance"):
        values = pd.to_numeric(raw[col], errors="coerce").to_numpy(dtype=np.float64)
        assert not np.isinf(values).any(), f"{col} emitted inf"
    assert np.isfinite(_observation(df)).all()


def test_market_microstructure_pass_through_cannot_leak_inf():
    """The pre-computed ``spread`` column is copied verbatim.

    ``merge_extra_features`` and the read seam can hand this module a
    ``spread`` that is already inf.  Nothing computed it, so no other
    guard in this module has run before it arrives.
    """
    df = _frame()
    df["spread"] = 0.5
    df.loc[df.index[300], "spread"] = np.inf

    raw = _pipeline().compute(df)
    values = pd.to_numeric(raw["spread"], errors="coerce").to_numpy(dtype=np.float64)
    assert not np.isinf(values).any(), "a pre-computed inf spread leaked through"
    assert np.isfinite(_observation(df)).all()


def test_window_features_survive_an_all_constant_window_0_over_0():
    """The window/rolling family: a constant window is a real reading.

    ``bb_pct_b``'s ``upper - lower`` is identically zero on every
    constant window (and on every period-1 roll), so the 0/0 there is the
    COMMON case, not an edge case -- it maps to the neutral 0.5 midpoint.
    ``volume_zscore_20``'s rolling std is 0 on a constant-volume window,
    floored to 1.0 so the feature reads 0 rather than NaN.
    """
    df = _frame()
    df["volume"] = 7.0  # constant volume for the whole frame
    df["close"] = 50.0  # constant price: every Bollinger window is flat
    df["open"] = 50.0
    df["high"] = 50.0
    df["low"] = 50.0

    raw = _pipeline().compute(df)
    assert np.isfinite(_observation(df)).all()

    # Direct on the helpers, so the test does not depend on group wiring.
    flat = pd.Series([50.0] * 30)
    _, _, width, pct_b = _bollinger(flat, 20, 2.0)
    # The first period-1 entries are NaN warm-up, which is expected and is
    # exactly what ffill/fillna exist to repair.  What must never appear is
    # an infinity from the 0/0.
    assert not np.isinf(width.to_numpy(dtype=np.float64)).any()
    assert np.isfinite(width.dropna().to_numpy(dtype=np.float64)).all()
    assert np.isfinite(pct_b.to_numpy(dtype=np.float64)).all()
    # period-1 rolls have zero width by construction; %B is the neutral 0.5.
    assert (pct_b.dropna() == 0.5).all()
    assert (width.dropna() == 0.0).all()


def test_rsi_0_over_0_on_a_flat_window_resolves_to_the_neutral_50():
    """``_rsi``'s ``avg_gain / avg_loss`` is 0/0 when a window has no moves."""
    flat = pd.Series([50.0] * 40)
    rsi = _rsi(flat, 14)
    values = rsi.to_numpy(dtype=np.float64)
    assert np.isfinite(values).all()
    # A window with no gains AND no losses is neither -- the neutral 50.
    assert (values == 50.0).all()


def test_every_raw_column_contaminated_with_inf_leaves_the_observation_finite():
    """The sweep, executed: contaminate each raw column one at a time.

    This is the compact form of the per-site table.  For every column the
    module reads, injecting a single ``inf`` (and separately a ``NaN``)
    must not yield a silently non-finite observation.  There are exactly
    two acceptable outcomes, and both are the guard working:

    * the observation comes back finite -- the site guarded the value, or
      the value is safe by construction; or
    * ``NonFiniteFeatureError`` is raised, naming the column -- the
      backstop gate caught what the per-site guard could not.

    The unacceptable outcome, and the one this pins, is a *silent* leak:
    an observation full of inf handed to the policy as if it were data.
    """
    extra = {
        "bid": 100.0,
        "ask": 100.5,
        "bid_vol": 10.0,
        "ask_vol": 12.0,
        "spread": 0.5,
        "signal_observed": 1.0,
        "signal_age_hours": 0.0,
        "funding_rate": 0.0001,
        "sentiment_score": 0.4,
    }
    for contaminant in (np.inf, np.nan):
        for column in ("open", "high", "low", "close", "volume", *extra):
            df = _frame(n=200)
            for col, val in extra.items():
                df[col] = val
            df.loc[df.index[100], column] = contaminant

            try:
                obs = _observation(df)
            except NonFiniteFeatureError as err:
                # The backstop fired. That is a pass, not a leak -- but it
                # must name a column so the offending site is findable.
                assert err.columns, "the guard raised without naming a column"
                continue
            assert np.isfinite(obs).all(), (
                f"{column}={contaminant} silently left "
                f"{int((~np.isfinite(obs)).sum())} non-finite cells in the observation"
            )


# ----------------------------------------------------------------------
# 2b. the two _require_finite gates
# ----------------------------------------------------------------------
def test_fit_refuses_to_persist_a_non_finite_stat_and_names_the_column():
    """``fit`` must not persist an inf stat, and must say which column.

    ``normalize`` subtracts the stored mean from EVERY row, so one inf
    stat turns the entire episode non-finite.  The reachable trigger is
    a genuine float64 overflow: ``obv`` is a cumulative sum, so a frame
    whose volume alone approaches the float64 ceiling overflows it to
    ``inf`` -- and inf survives ``ffill().fillna(0)`` untouched, being
    the one value that policy cannot repair.
    """
    df = _frame(n=200)
    df["volume"] = 1e308  # a strictly rising close makes obv cumsum overflow
    pipe = _pipeline()

    with pytest.raises(NonFiniteFeatureError) as excinfo:
        pipe.fit(df, ticker_id="ETH_USD")

    err = excinfo.value
    assert "obv" in err.columns, f"the offending column was not named: {err.columns}"
    assert err.stage == "fit"
    assert err.ticker_id == "ETH_USD"
    # Transactional: a rejected frame leaves the previous stats in place.
    assert pipe.stats_for("ETH_USD") is None


def test_transform_fails_loudly_on_a_poisoned_stats_artifact():
    """The path that actually reached the policy, and was SILENT.

    ``NormalizationStats.load`` reads whatever ``mean``/``std`` a
    ``normalization.npz`` on disk carries, with no validation.  An
    artifact written before this guard existed reloads as
    ``(mean=inf, std=nan)``, and ``(x - inf) / 1.0`` is ``-inf`` on every
    single row.  This is reachable through the model directory rather
    than through the data, so no builder-side guard can prevent it -- the
    check has to be on the way out.
    """
    df = _frame(n=200)
    pipe = _pipeline()
    clean = pipe.compute(df).ffill().fillna(0.0)
    poisoned = {
        name: (np.inf, np.nan) if name == "sma_4" else (0.0, 1.0)
        for name in clean.columns
    }
    stats = NormalizationStats(
        ticker_id="ETH_USD", stats=poisoned, feature_names=list(clean.columns)
    )
    pipe.set_stats("ETH_USD", stats)

    with pytest.raises(NonFiniteFeatureError) as excinfo:
        pipe.transform(df, ticker_id="ETH_USD")

    err = excinfo.value
    assert err.stage == "transform"
    assert "sma_4" in err.columns, (
        f"the poisoned column was not named: {err.columns}"
    )


def test_transform_in_sample_fallback_also_refuses_to_return_non_finite():
    """The fallback branch (no stored stats) is checked too.

    ``.replace(0, 1.0)`` floors a zero std, but a column containing one
    infinity has an *infinite mean*, and subtracting it takes the whole
    frame with it -- so the floor is not enough on its own.
    """
    df = _frame(n=200)
    df["volume"] = 1e308  # overflow obv via the fallback branch
    pipe = _pipeline()
    assert pipe.stats_for("ETH_USD") is None, "the fallback branch must have no stats"

    with pytest.raises(NonFiniteFeatureError):
        pipe.transform(df, ticker_id="ETH_USD")


def test_non_finite_feature_error_is_a_value_error_subclass():
    """Existing catchers must keep working: it is a ``ValueError``."""
    assert issubclass(NonFiniteFeatureError, ValueError)

    df = _frame(n=200)
    df["volume"] = 1e308
    with pytest.raises(ValueError):  # the broad catcher, unchanged
        _pipeline().fit(df, ticker_id="ETH_USD")


def test_non_finite_feature_error_reports_stage_columns_and_count():
    """The message must name what failed, where, and how badly."""
    df = _frame(n=200)
    df["volume"] = 1e308
    with pytest.raises(NonFiniteFeatureError) as excinfo:
        _pipeline().fit(df, ticker_id="ETH_USD")

    err = excinfo.value
    assert err.columns and all(isinstance(c, str) for c in err.columns)
    assert err.stage in {"fit", "transform", "normalize"}
    assert err.count >= 1
    assert err.kind in {"inf", "nan"}
    assert "ETH_USD" in str(err), "the message must name the ticker"
    assert "obv" in str(err), "the message must name the column"


# ----------------------------------------------------------------------
# 2c. the one site where zero is DATA, not a denominator
# ----------------------------------------------------------------------
def test_exogenous_passthrough_preserves_zero_and_the_minus_one_sentinel():
    """REGRESSION: zero must NOT be mapped to NaN at the passthrough.

    ``signal_observed`` is 0.0/1.0 and ``signal_age_hours`` carries a
    ``-1.0`` "no reading" sentinel.  Those two columns exist precisely so
    a zero-filled cell can be told apart from a genuine 0, so mapping
    their 0.0/-1.0 to NaN lets ``ffill().fillna(0.0)`` re-flatten exactly
    the distinction they were added to keep.  This broke three
    pre-existing freshness/signal tests when the zero-is-a-denominator
    guard was first applied here.
    """
    df = _frame(n=200)
    df["signal_observed"] = 0.0
    df["signal_age_hours"] = -1.0  # data._NO_SIGNAL_AGE sentinel

    raw = _pipeline().compute(df)
    assert (raw["signal_observed"] == 0.0).all(), "the 0.0 'unobserved' flag was erased"
    assert (raw["signal_age_hours"] == -1.0).all(), "the -1.0 sentinel was erased"

    # And it must still be distinguishable after the observation policy.
    obs = _pipeline().compute(df).ffill().fillna(0.0)
    assert (obs["signal_observed"] == 0.0).all()
    assert (obs["signal_age_hours"] == -1.0).all()


def test_exogenous_passthrough_still_maps_infinity_to_nan():
    """The inf half of that guard is load-bearing and must survive.

    An exogenous column is the only observation column this module does
    NOT compute -- it arrives from a sibling signal file or from
    ``data.add_derived_ohlcv_features`` -- so no other guard in this
    module has run before it.  inf is still unrepairable by the fill.
    """
    df = _frame(n=200)
    df["signal_observed"] = 1.0
    df["signal_age_hours"] = 0.0
    df.loc[df.index[100], "signal_age_hours"] = np.inf
    df.loc[df.index[101], "sentiment_score"] = np.inf

    raw = _pipeline().compute(df)
    for col in ("signal_age_hours", "sentiment_score"):
        values = pd.to_numeric(raw[col], errors="coerce").to_numpy(dtype=np.float64)
        assert not np.isinf(values).any(), f"{col} leaked an inf from the passthrough"

    # Mapped to NaN, so the documented fill policy repairs it.
    assert np.isfinite(_observation(df)).all()


# ----------------------------------------------------------------------
# 2d. positive control
# ----------------------------------------------------------------------
def test_a_clean_frame_is_unaffected_by_every_guard():
    """The guard must not simply be rejecting everything.

    A healthy frame with no degeneracy whatsoever must still produce a
    full, finite, correctly-shaped observation -- and must still compute
    every feature group, so no guard has narrowed the column set.
    """
    df = _frame(n=400, zero_volume_at=())
    raw = _pipeline().compute(df)

    assert not raw.empty
    assert "return_1" in raw.columns
    assert "log_return_1" in raw.columns
    assert "volume_change_1" in raw.columns
    assert any(c.startswith("rsi_") for c in raw.columns)
    assert any(c.startswith("bb_width") for c in raw.columns)
    assert "volume_zscore_20" in raw.columns

    pipe = _pipeline()
    pipe.fit(df, ticker_id="ETH_USD")
    obs = pipe.transform(df, ticker_id="ETH_USD")
    assert obs.shape == (400, raw.shape[1])
    assert np.isfinite(obs).all()

    # The normalization stats are real, not the (0.0, 1.0) degenerate pair,
    # and they vary per column -- i.e. the pipeline actually fitted.
    stats = pipe.stats_for("ETH_USD")
    assert stats is not None
    means = {name: pair[0] for name, pair in stats.stats.items()}
    assert len(set(round(v, 9) for v in means.values())) > 1, (
        "every column normalized to the same mean: the guards are pinning "
        "everything to a constant"
    )
    # sma_4 tracks a strictly rising ramp here, so its normalized column
    # has real spread rather than sitting flat at zero.
    sma_idx = list(raw.columns).index("sma_4")
    assert obs[:, sma_idx].std() > 0.0


def test_order_book_imbalance_denominator_is_guarded_against_cancellation():
    """The one case where the imbalance denominator guard is load-bearing.

    ``bid_vol - ask_vol`` over ``bid_vol + ask_vol`` looks safe against an
    infinity on its own -- an infinite numerator meets an infinite
    denominator and yields ``NaN``, which the fill repairs.  But the
    denominator is a SUM, so two large volumes of OPPOSITE SIGN cancel it
    to exactly ``0`` while the numerator stays huge, and ``inf / 0`` is
    ``inf``: a value that survives the observation policy.  Negative
    volumes are representable (a signed net-flow feed), so this is
    reachable, not hypothetical.
    """
    df = _frame(n=200)
    df["bid_vol"] = 1e308
    df["ask_vol"] = -1e308  # denominator cancels to 0.0, numerator to inf
    df["bid"] = 100.0
    df["ask"] = 100.5

    raw = _pipeline().compute(df)
    values = pd.to_numeric(raw["order_book_imbalance"], errors="coerce").to_numpy(
        dtype=np.float64
    )
    assert not np.isinf(values).any(), (
        "order_book_imbalance emitted inf: the denominator cancelled to 0 "
        "while the numerator overflowed, and inf/0 is inf"
    )
    assert np.isfinite(_observation(df)).all()


def test_rsi_reports_the_neutral_50_when_a_window_has_no_losses():
    """Pins the value the ``avg_loss`` zero floor actually produces.

    Two different readings are possible for a window with no losses, and
    they are numerically distinguishable, so this pins the chosen one:
    ``avg_gain / avg_loss.replace(0, nan)`` is ``NaN`` and the trailing
    ``fillna(50.0)`` supplies the neutral 50, whereas the unfloored ``inf``
    would make ``100 - 100/(1 + inf)`` collapse to ``100.0``.  The module
    deliberately takes the neutral reading; this test is what makes that
    a decision rather than an accident.
    """
    rising = pd.Series(100.0 * (1.001 ** np.arange(40)))  # no down-bars at all
    values = _rsi(rising, 14).to_numpy(dtype=np.float64)

    assert np.isfinite(values).all()
    assert (values[14:] == 50.0).all(), (
        "a window with no losses resolved to the unfloored value rather "
        "than to the neutral 50"
    )


def test_log_return_loop_numerator_is_guarded_on_an_inf_close():
    """The LIVE log-return expression, which is the one in the window loop.

    ``return_1``/``log_return_1`` are assigned twice: once as standalone
    statements and once by ``for w in self.windows``.  With ``w=1`` in the
    window set -- the default -- the loop wins, so the loop's expression is
    the one that actually reaches the column, and it is the one whose
    numerator guard is load-bearing.  An infinite close makes the numerator
    ``inf`` and ``log(inf)`` is ``+inf``.
    """
    df = _frame()
    df.loc[df.index[200], "close"] = np.inf

    raw = _pipeline().compute(df)
    for col in ("log_return_1", "log_return_4", "log_return_24"):
        values = pd.to_numeric(raw[col], errors="coerce").to_numpy(dtype=np.float64)
        assert not np.isinf(values).any(), (
            f"{col} emitted inf: the window-loop numerator is unguarded"
        )
    assert np.isfinite(_observation(df)).all()


def test_range_1_and_price_ratio_numerators_are_guarded_on_an_inf_close():
    """The numerator guards on the two ratio features, exercised by inf.

    ``range_1`` divides a DIFFERENCE of two series by ``close``, so both
    the numerator and the denominator need a guard; ``price_ratio_sma_{w}``
    divides ``close`` by a rolling mean, and its numerator is the ``close``
    that a zero/infinite bar sets.  An infinite close is the discriminating
    case: it leaves the denominator healthy, so only the numerator guard can
    save the column.
    """
    df = _frame()
    df.loc[df.index[200], "close"] = np.inf
    df.loc[df.index[200], "high"] = np.inf

    raw = _pipeline().compute(df)
    for col in ("range_1", "price_ratio_sma_4", "price_ratio_sma_24"):
        values = pd.to_numeric(raw[col], errors="coerce").to_numpy(dtype=np.float64)
        assert not np.isinf(values).any(), (
            f"{col} emitted inf: its numerator is unguarded, and an "
            f"infinite close leaves the denominator healthy"
        )
    assert np.isfinite(_observation(df)).all()


#: The rolling/technical columns that ``compute``'s seam is load-bearing
#: for, split by the mechanism that repairs them once the seam has mapped
#: the bad price to NaN.  See the two tests below for the measurements.
#:
#: FRAME-DEPENDENCY, recorded deliberately: the ``rsi_4`` entry here is NOT
#: the seam reporting an unknown window as its neutral constant.  ``_rsi``
#: reads the poisoned bar as a *stale carried value*, because ``ewm`` skips a
#: non-finite input instead of masking it (see ``_rsi``'s docstring).  The
#: 50.0 lands here only because ``_frame()`` is a monotonically rising ramp
#: with no down-bars, so ``avg_loss`` is ``-0.0`` everywhere and the
#: both-sides-zero guard -- not the seam -- feeds ``fillna(50.0)``.  On a
#: frame that has losses this entry would read as a carried value instead and
#: the assertion below would not hold; that assertion is about the seam, so
#: it is pinned on the frame where the seam is the only variable.
_ROLLING_NAN_COLUMNS = ("sma_4", "bb_upper_4", "bb_lower_4", "bb_width_4")
_ROLLING_NEUTRAL_COLUMNS = {"rsi_4": 50.0, "bb_pctb_4": 0.5}


def _poisoned_frame(bad: float, at: int = 200) -> pd.DataFrame:
    """A healthy frame with ``bad`` written into one bar's whole price row."""
    df = _frame()
    for col in ("close", "high", "low", "open"):
        df.loc[df.index[at], col] = bad
    return df


def test_compute_seam_makes_a_zero_price_bar_unknown_to_the_rolling_group():
    """Pins the seam by its *real* effect, which is not infinity suppression.

    This test previously claimed that an infinite close "comes straight back
    out as ``inf``" through ``rolling`` and that the seam was therefore the
    only thing stopping an infinity.  That claim was false and the test was
    vacuous: pandas masks ``inf`` to NaN inside ``rolling`` on its own, so
    deleting the seam left it green.  Measured on pandas 3.0.4:

    * ``rolling(w)`` **masks** a non-finite input -> NaN (0 infinities out).
    * ``ewm()`` **skips** the bar entirely -> finite, and *wrong*.

    Neither emits an infinity, so no ``isinf`` assertion can ever see the
    seam.  What the seam actually does is refuse the bad price *before* the
    rolling windows consume it, and because ``_NON_FINITE_INPUTS`` contains
    ``0.0`` as well as the infinities, the reachable trigger is a **zero**
    price -- far likelier in real OHLCV than an infinity.

    Without the seam a zero price enters the window as a real number and the
    rolling group reports a finite, plausible, completely wrong answer
    (measured at bar 200 of this exact frame): ``sma_4`` 91.41 instead of
    NaN, ``bb_upper_4`` 196.97, ``bb_lower_4`` **-14.14**, ``bb_width_4``
    **2.3094**, ``rsi_4`` **0.2979**, ``bb_pctb_4`` **0.0670**, ``atr_4``
    **30.87** against a healthy 0.487.  Every one of those is finite, so
    every one of them walks straight through ``_require_finite`` -- a
    -83% price print and a 63x range spike, silently, as model input.

    The contract pinned here is therefore: **the poisoned window reads as
    unknown (NaN), or as the module's documented neutral constant where a
    downstream guard owns one** -- never as a finite wrong number.
    """
    df = _poisoned_frame(0.0)
    raw = _pipeline().compute(df)

    # window=4 over a NaN at bar 200 -> bars 200..203 are all un-known.
    poisoned = slice(200, 204)
    for col in _ROLLING_NAN_COLUMNS:
        values = pd.to_numeric(raw[col], errors="coerce").to_numpy(dtype=np.float64)
        assert np.isnan(values[poisoned]).all(), (
            f"{col} reported a finite value across the poisoned window "
            f"({values[poisoned].tolist()}) instead of NaN: the compute() "
            f"seam is what stops a zero price entering the rolling window"
        )
        # and the window must recover once the bad bar has rolled out
        assert np.isfinite(values[204]), f"{col} never recovered after the gap"

    # These two have a *documented* neutral reading for an unknown window,
    # so NaN is not what they may report -- the neutral constant is.  For
    # `rsi_4` the route to 50.0 is the no-losses guard rather than the seam
    # (see `_ROLLING_NEUTRAL_COLUMNS` above); this loop pins the value, not
    # the mechanism that produced it.
    for col, neutral in _ROLLING_NEUTRAL_COLUMNS.items():
        values = pd.to_numeric(raw[col], errors="coerce").to_numpy(dtype=np.float64)
        assert np.allclose(values[poisoned], neutral), (
            f"{col} should fall back to its documented neutral {neutral} "
            f"across the poisoned window, got {values[poisoned].tolist()}"
        )

    # The neutral readings must not leak into the normalized observation
    # either -- this is the surface the environment actually sees.
    assert np.isfinite(_observation(df)).all()


def test_a_zero_price_bar_never_yields_an_infinity_in_the_rolling_group():
    """The narrow claim the old test made, kept because it is still true.

    Note what this does **not** establish: it is a property of pandas'
    own masking, not of this module.  It passes identically with the seam
    removed -- which is precisely why it could never prove the seam was
    load-bearing.  The test above is the one that does.
    """
    df = _poisoned_frame(0.0)
    raw = _pipeline().compute(df)
    for col in (
        "sma_4",
        "ema_4",
        "rsi_4",
        "macd_line_4",
        "macd_signal_4",
        "macd_hist_4",
        "bb_upper_4",
        "bb_lower_4",
        "bb_width_4",
        "bb_pctb_4",
        "atr_4",
    ):
        values = pd.to_numeric(raw[col], errors="coerce").to_numpy(dtype=np.float64)
        assert not np.isinf(values).any(), f"{col} emitted inf for a zero price"
    assert np.isfinite(_observation(df)).all()


def test_rolling_masks_but_ewm_skips_a_non_finite_price_this_guard_cannot_see():
    """Documents the known gap: pandas' ``ewm`` returns finite-but-wrong.

    The seam plus ``_require_finite`` close the *infinity* hole.  They
    cannot close this one, and this test exists to pin the hazard in place
    rather than let it be rediscovered as a bug:

    * ``rolling(w).mean()`` **masks** a non-finite input to NaN -- the bad
      bar contaminates nothing, and the NaN is visible to the guard.
    * ``ewm(...).mean()`` **skips** the bar and carries on, returning a
      value that is finite *and wrong* -- 0 infinities and 0 NaNs, so
      ``_require_finite`` passes it cleanly.  Nothing downstream can tell a
      skipped-bar EMA from a genuine one.

    Fixing the ``ewm`` families (``ema``/``macd``/``rsi``/``atr``) is out of
    scope for this pass; what is in scope is that the limitation is written
    down and pinned.  If this test ever starts failing because pandas
    changed the behaviour, that is *good* news -- re-check whether the
    finite-but-wrong hazard still exists.
    """
    close = pd.Series([1.0, 2.0, 3.0, np.inf, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    rolled = close.rolling(5).mean().to_numpy(dtype=np.float64)
    ewmmed = close.ewm(span=5, adjust=False).mean().to_numpy(dtype=np.float64)

    # rolling: masks.  No infinity, and the bar plus its window read NaN.
    assert not np.isinf(rolled).any()
    assert np.isnan(rolled[3:8]).all(), "rolling no longer masks a non-finite input"

    # ewm: skips.  No infinity, no NaN -- a clean-looking, wrong number.
    assert not np.isinf(ewmmed).any()
    assert not np.isnan(ewmmed).any(), "ewm no longer skips a non-finite input"
    # It literally repeats the previous bar's value: the inf never entered
    # the average at all, it was skipped.
    assert ewmmed[3] == pytest.approx(ewmmed[2]), (
        "ewm no longer skips the bad bar; the finite-but-wrong hazard it "
        "created may be gone -- re-check the seam and the ewm guards"
    )
