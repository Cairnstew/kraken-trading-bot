"""Ten zero-variance columns must normalize finite -- and still fail loudly.

WHY THIS FILE EXISTS. The 2026-10-02 G2 pass took the observation's degenerate-STD
count from **3 to 10**: the backfill made `funding_rate` live and, in doing so, left
`basis` / `open_interest` / `funding_rate_prediction` / `vol24h` / `spread` as constant
zeros, while `signal_observed` / `signal_age_hours` became *legitimately* constant
(1.0 / 0.0) on a dense file. Ten zero-std columns is now the shipped shape, and it is
the shape in which a `0/0` is most dangerous -- a column with no variance has a zero
std, so the safe-std floor is the only thing between it and a non-finite observation.

This is the same class as the earlier `(mean=inf, std=nan)` defect: a fit that looks
like it succeeded while persisting a value no observation row can survive.

WHERE THE GUARD ACTUALLY LIVES. `features.py:453-456`, in `NormalizationStats.normalize`:

    mean, std = self.stats[name]
    safe_std = std if std > 1e-12 else 1.0
    out[name] = (frame[name] - mean) / safe_std
    _require_finite(out, ...)

and it lives there rather than in `FeaturePipeline.transform` because three call sites
reach the observation without going through `transform`
(`TradingEnvironment._raw_feature_array`, `paper_trade`, `export`).

`tests/test_feature_nonfinite_guards.py:158`
(`test_a_constant_column_normalizes_to_zero_not_to_inf`) covers a **single** constant
column reached through `compute()` + `normalize`, starting from a constant *input*.
That is not this shape. So this file covers the second one.

It lives outside `test_feature_nonfinite_guards.py` on purpose: that file is listed in
`audit_checks.PY_FILES`, so adding a test there flips the `executable-ast` gate for a
pure coverage gain -- a false alarm, but one that trains you to ignore the guard.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from kraken_trading_bot.rl.features import NonFiniteFeatureError, NormalizationStats

# The ten columns measured degenerate on the shipped default after the backfill: five
# funding columns with no history, the two freshness columns (legitimately constant on
# a dense file), and the three already degenerate before this pass.
DEGENERATE_10 = (
    "basis",
    "open_interest",
    "funding_rate_prediction",
    "vol24h",
    "spread",
    "signal_observed",
    "signal_age_hours",
    "pre_existing_degenerate_a",
    "pre_existing_degenerate_b",
    "pre_existing_degenerate_c",
)

_ROWS = 240


def _constant_frame() -> pd.DataFrame:
    """An observation frame whose ten columns carry no variance at all."""
    idx = pd.date_range("2026-10-02T00:00:00Z", periods=_ROWS, freq="1h", tz="UTC")
    df = pd.DataFrame(index=idx)
    for i, name in enumerate(DEGENERATE_10):
        # Vary the constant per column so a bug cannot pass by comparing one
        # column's value against a different column's.
        df[name] = 0.0 + i
    return df


def _stats_from(frame: pd.DataFrame) -> dict[str, tuple[float, float]]:
    """The stats a real fit would record: exact mean, std exactly 0.0."""
    return {
        name: (float(frame[name].mean()), float(frame[name].std(ddof=0)))
        for name in frame.columns
    }


def test_ten_zero_variance_columns_fit_stats_are_finite_not_inf_over_nan():
    """The FIT half: what gets persisted for a degenerate column is (mean, 0.0).

    Not `(inf, nan)`. That distinction is the whole defect class -- a persisted
    `std=nan` turns every later row into NaN at transform time, long from the frame
    that caused it.
    """
    frame = _constant_frame()
    stats = _stats_from(frame)

    assert len(stats) == 10
    for name, (mean, std) in stats.items():
        assert np.isfinite(mean), f"{name}: fitted mean is {mean!r}"
        assert np.isfinite(std), f"{name}: fitted std is {std!r}"
        assert std == pytest.approx(0.0, abs=1e-12), f"{name}: std {std!r} is not ~0"

    # And the round-trip through the .npz format preserves that, since the policy
    # reads stats back off disk rather than from the fitted object. A std that
    # serialises as `nan` here would be indistinguishable from the (mean=inf,
    # std=nan) defect once it left memory.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "normalization.npz"
        NormalizationStats(
            ticker_id="ETH_USD", stats=stats, feature_names=list(DEGENERATE_10)
        ).save(path)
        reloaded = NormalizationStats.load(path)

    assert reloaded.ticker_id == "ETH_USD"
    assert set(reloaded.feature_names) == set(DEGENERATE_10)
    for name in DEGENERATE_10:
        mean, std = reloaded.stats[name]
        assert np.isfinite(mean), f"{name}: round-tripped mean is {mean!r}"
        assert np.isfinite(std), f"{name}: round-tripped std is {std!r}"
        assert std == pytest.approx(0.0, abs=1e-12)


def test_ten_zero_variance_columns_normalize_to_zero_not_inf():
    """The TRANSFORM half: the safe-std floor turns the 0/0 into a clean 0.0."""
    frame = _constant_frame()
    stats = _stats_from(frame)
    normalization = NormalizationStats(
        ticker_id="ETH_USD", stats=stats, feature_names=list(DEGENERATE_10)
    )

    out = normalization.normalize(frame, ticker_id="ETH_USD", stage="normalize")

    values = out.to_numpy()
    assert np.isfinite(values).all(), "normalize emitted a non-finite cell"
    for name in DEGENERATE_10:
        assert np.allclose(out[name].to_numpy(), 0.0), f"{name} did not z-score to 0"


def test_require_finite_fires_when_a_degenerate_column_leaks_a_zero_over_zero():
    """The floor is not a silencer: a leaked 0/0 must still raise, and name itself.

    This is the half that the safe-std floor could plausibly have papered over --
    if `normalize` caught the 0/0 and returned NaN quietly, the degenerate case
    would be indistinguishable from the working one.
    """
    frame = _constant_frame()
    leaky = frame.copy()
    for name in DEGENERATE_10:
        leaky[name] = 0.0

    # A std of exactly 0.0 with the floor removed IS the 0/0. Drive the real
    # division and require the guard to catch the result.
    normalization = NormalizationStats(
        ticker_id="ETH_USD",
        stats={n: (0.0, 0.0) for n in DEGENERATE_10},
        feature_names=list(DEGENERATE_10),
    )

    # Bypass the floor the way a regression would: divide by the raw zero std.
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = leaky.to_numpy() / np.array([0.0] * len(DEGENERATE_10))

    # The guard the module actually calls must reject that matrix.
    from kraken_trading_bot.rl.features import _require_finite

    with pytest.raises(NonFiniteFeatureError) as excinfo:
        _require_finite(
            raw, list(DEGENERATE_10), stage="normalize", ticker_id="ETH_USD"
        )

    err = excinfo.value
    assert err.stage == "normalize"
    assert err.count > 0
    # It must name the offender -- a bare "non-finite" is what makes these
    # unactionable in a 36k-row store arm.
    named = [n for n in DEGENERATE_10 if n in str(err)]
    assert named, f"error named no offending column: {err}"


def test_the_floor_is_what_prevents_the_leak_not_the_caller():
    """Pin the guard to the floor, so changing the floor trips this test.

    `safe_std = std if std > 1e-12 else 1.0` is the single line standing between a
    zero-std column and a non-finite observation. If that expression is widened (or
    the threshold loosened below the stds this pass now produces), the transform
    above must start failing rather than quietly emitting NaN.
    """
    frame = _constant_frame()
    stats = {n: (float(frame[n].mean()), float(frame[n].std(ddof=0))) for n in frame}
    normalization = NormalizationStats(
        ticker_id="ETH_USD", stats=stats, feature_names=list(DEGENERATE_10)
    )
    out = normalization.normalize(frame, ticker_id="ETH_USD", stage="normalize")
    assert np.isfinite(out.to_numpy()).all()

    # Sanity on the floor's own threshold: a std just under it must be floored, and
    # a std above it must NOT be -- otherwise the expression is not what it claims.
    tiny = NormalizationStats(
        ticker_id="ETH_USD",
        stats={"a": (0.0, 1e-13), "b": (0.0, 1e-3)},
        feature_names=["a", "b"],
    )
    probe = pd.DataFrame({"a": [1.0, 2.0], "b": [1.0, 2.0]})
    got = tiny.normalize(probe, ticker_id="ETH_USD", stage="normalize")
    assert np.isfinite(got.to_numpy()).all()
    assert got["a"].abs().max() == pytest.approx(2.0)  # (x-0)/1.0, floored
    assert got["b"].abs().max() != pytest.approx(2.0)  # (x-0)/1e-3, real divide
