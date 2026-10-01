"""Integration tests for the exogenous-signal config wiring.

Phase 4B made the merge seam sound (A1 ticker filter, A2 hour de-dup,
A3 bounded carry + freshness columns, A4 absence-is-not-neutral) and
threaded two new config keys — ``signal_max_age_hours`` and
``signal_require_ticker`` — through train/backtest/paper/export.

``tests/test_rl_data_store.py`` covers the *seam* by calling
``merge_extra_features`` / ``read_ohlc_dataframe`` with explicit
keyword arguments.  What it cannot cover is the half that decides
whether any of it reaches a real run: the ``config -> consumer ->
read_ohlc_dataframe`` hop.  Those two lines per consumer are the only
thing standing between a YAML key and the behaviour, so a dropped
keyword there would be invisible to the seam tests while silently
disabling A1–A4 for every user of that consumer.

These tests therefore drive the hop from a real YAML config through a
real consumer and assert on the frame and on the z-scored observation,
plus one structural guard across all four consumers.  Offline
throughout: a canned Kraken-shaped page, no network, no credentials,
and nothing written outside pytest's ``tmp_path``.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml
from kraken_api.models import Candle

from kraken_trading_bot.rl import SignalTickerMismatchError, read_ohlc_dataframe
from kraken_trading_bot.rl.data import (
    add_derived_ohlcv_features,
    merge_extra_features,
)
from kraken_trading_bot.rl.export import build_export_frame
from kraken_trading_bot.rl.features import FeaturePipeline

# 2026-08-01 12:00 UTC — the anchor the store and export tests already use.
_BASE = 1785585600
_HOUR = 3600
_PAIR = "ETH/USD"
_TICKER_ID = "ETH_USD"

# The five keys a user sets to switch the seam on and bound it, and the
# three file keys they point at their sibling projects' output.
_SIGNAL_FILE_KEYS = (
    "extra_features_file",
    "funding_features_file",
    "social_features_file",
)
_SIGNAL_CONTROL_KEYS = ("signal_max_age_hours", "signal_require_ticker")
_SIGNAL_KEYS = _SIGNAL_FILE_KEYS + _SIGNAL_CONTROL_KEYS

# Consumers that must forward every signal key, and the module each one
# imports ``read_ohlc_dataframe`` into (each does ``from .data import
# read_ohlc_dataframe``, so the binding is per-module and the structural
# guard below has to check each module separately).
_CONSUMERS = (
    "train",
    "backtest",
    "export",
    "paper_trade",
)


# ---------------------------------------------------------------------------
# fakes — same shape as tests/test_rl_data_store.py's FakeSource
# ---------------------------------------------------------------------------
def _candles(n: int = 60, seed: int = 7) -> list[Candle]:
    """Hourly ``ETH/USD`` bars from 2026-08-01T12:00Z with a wandering close.

    The wandering close matters: flat closes make the technical
    indicators degenerate and the episode a constant frame, which would
    let a broken wiring pass unnoticed.
    """
    rng = np.random.default_rng(seed)
    closes = 2000.0 + np.cumsum(rng.normal(0.0, 0.7, size=n))
    return [
        Candle(
            pair=_PAIR,
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


class FakeSource:
    """Kraken-compatible source: one canned page, ``last=0`` ends paging."""

    def __init__(self, candles: list[Candle]) -> None:
        self.candles = list(candles)
        self.calls: list[int | None] = []

    def ohlc(
        self, pair: str, interval: int = 60, since: int | None = None
    ) -> tuple[list[Candle], int]:
        self.calls.append(since)
        return self.candles, 0


def _write_signals(path: Path, records: list[dict[str, Any]]) -> Path:
    """Write JSONL signal records, one JSON object per line."""
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    return path


def _write_config(tmp_path: Path, name: str = "config.yaml", **keys: Any) -> Path:
    """Write a runnable YAML config carrying the signal keys under test.

    ``load_train_config`` reads exactly the file it is handed (no merge
    with ``configs/default.yaml``), so the feature config has to be
    spelled out here or the ``signals`` group would never run.
    """
    config: dict[str, Any] = {
        "feature_windows": [1, 4, 24],
        "feature_groups": [
            "price",
            "technical",
            "volume",
            "microstructure",
            "signals",
        ],
        "extra_features_file": None,
        "funding_features_file": None,
        "social_features_file": None,
        "signal_max_age_hours": None,
        "signal_require_ticker": True,
    }
    config.update(keys)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _by_hour(frame: pd.DataFrame) -> dict[str, Any]:
    """Map an export frame's ISO timestamps to their row, for assertions."""
    return {str(ts): frame.iloc[i] for i, ts in enumerate(frame["timestamp"])}


# ---------------------------------------------------------------------------
# 1. config -> consumer -> merge: the freshness columns arrive with values
# ---------------------------------------------------------------------------
def test_yaml_config_keys_reach_the_merge_through_export(tmp_path) -> None:
    """Setting the five keys in YAML must switch A1–A4 on for ``export-data``.

    ``export`` is the cheapest consumer to drive end-to-end (no policy,
    no registry), and it is a real consumer that composes the same
    stages ``train_ticker`` runs — config -> OHLC read -> signal merges
    -> feature fit -> episode slice -> compute.  So this one test covers
    the whole hop for the export path, including the thing the seam
    tests cannot see: whether ``signal_max_age_hours`` /
    ``signal_require_ticker`` are actually forwarded out of the config.

    One ticker-tagged record at 12:00 against hourly bars gives the
    default bound's exact signature: observed at 12:00 (age 0) and
    13:00 (age 1, the one bar of carry), unobserved from 14:00 on.
    """
    signals = _write_signals(
        tmp_path / "news.jsonl",
        [
            {
                "ticker": _PAIR,
                "timestamp": "2026-08-01T12:00:00Z",
                "sentiment_score": 0.4,
            }
        ],
    )
    config = _write_config(
        tmp_path,
        extra_features_file=str(signals),
        signal_max_age_hours=None,  # the documented default
        signal_require_ticker=True,
    )

    frame = build_export_frame(
        _TICKER_ID,
        config_path=config,
        manager=FakeSource(_candles(60)),
        pages=1,
    )

    # The freshness pair rides the `signals` feature group into the frame.
    assert "signal_age_hours" in frame.columns
    assert "signal_observed" in frame.columns
    # ...and the filtered value itself arrived (A1 kept our ticker).
    assert "sentiment_score" in frame.columns

    rows = _by_hour(frame)
    observed = rows["2026-08-01T12:00:00Z"]
    carried = rows["2026-08-01T13:00:00Z"]
    stale = rows["2026-08-01T14:00:00Z"]

    assert observed["sentiment_score"] == pytest.approx(0.4)
    assert observed["signal_age_hours"] == pytest.approx(0.0)
    assert observed["signal_observed"] == pytest.approx(1.0)

    # One bar of carry by default, with the age growing as it is carried.
    assert carried["sentiment_score"] == pytest.approx(0.4)
    assert carried["signal_age_hours"] == pytest.approx(1.0)
    assert carried["signal_observed"] == pytest.approx(1.0)

    # Past the bound the value is gone and the bar is explicitly
    # unobserved — never a stale reading wearing a current value.
    assert stale["sentiment_score"] == pytest.approx(0.0)
    assert stale["signal_age_hours"] == pytest.approx(-1.0)
    assert stale["signal_observed"] == pytest.approx(0.0)


def test_mispointed_config_raises_through_the_consumer(tmp_path) -> None:
    """A BTC file left configured while ETH exports must raise, not merge.

    This is A1 seen from the user's side: the failure only exists if the
    ``signal_require_ticker`` key survives the config hop.  Drop that
    keyword from ``export.py`` and this test fails, where the seam tests
    (which pass the flag directly) would still pass.

    ``signal_require_ticker: false`` is the documented escape hatch and
    must still merge — the config key that is *present but null* must not
    silently behave like ``false`` either, so that is checked too.
    """
    btc_file = _write_signals(
        tmp_path / "btc.jsonl",
        [
            {
                "ticker": "BTC/USD",
                "timestamp": "2026-08-01T12:00:00Z",
                "sentiment_score": -0.9,
            }
        ],
    )

    strict = _write_config(
        tmp_path,
        "strict.yaml",
        extra_features_file=str(btc_file),
        signal_require_ticker=True,
    )
    with pytest.raises(SignalTickerMismatchError) as excinfo:
        build_export_frame(
            _TICKER_ID,
            config_path=strict,
            manager=FakeSource(_candles(60)),
            pages=1,
        )
    assert _PAIR in str(excinfo.value)
    assert "BTCUSD" in str(excinfo.value)

    # Opting out merges BTC's numbers onto ETH deliberately, not silently.
    opted_out = _write_config(
        tmp_path,
        "opted_out.yaml",
        extra_features_file=str(btc_file),
        signal_require_ticker=False,
    )
    frame = build_export_frame(
        _TICKER_ID,
        config_path=opted_out,
        manager=FakeSource(_candles(60)),
        pages=1,
    )
    assert _by_hour(frame)["2026-08-01T12:00:00Z"]["sentiment_score"] == (
        pytest.approx(-0.9)
    )

    # A null in the file is not an opt-out: only an explicit false is.
    nulled = _write_config(
        tmp_path,
        "nulled.yaml",
        extra_features_file=str(btc_file),
        signal_require_ticker=None,
    )
    with pytest.raises(SignalTickerMismatchError):
        build_export_frame(
            _TICKER_ID,
            config_path=nulled,
            manager=FakeSource(_candles(60)),
            pages=1,
        )


# ---------------------------------------------------------------------------
# 2. the freshness columns must reach the z-scored observation
# ---------------------------------------------------------------------------
def test_freshness_columns_reach_the_z_scored_observation(tmp_path) -> None:
    """A4's distinction is only real once it is inside the observation.

    ``features._add_signals_features`` forwards the pair verbatim, and
    the environment then z-scores the ffilled frame
    (``TradingEnvironment._raw_feature_array``).  This mirrors that
    composition, so it fails if the freshness columns are ever dropped
    from ``_SIGNAL_COLUMNS``, are dropped from the ``signals`` group, or
    reintroduce a NaN (which would spread through the whole z-scored
    vector rather than costing one bad bar).
    """
    frame = pd.DataFrame(
        {
            "open": 2000.0,
            "high": 2000.0,
            "low": 2000.0,
            "close": 2000.0,
            "volume": 10.0,
        },
        index=pd.to_datetime(
            [f"2026-08-01T{hour:02d}:00:00Z" for hour in range(12, 18)],
            utc=True,
        ),
    )
    signals = _write_signals(
        tmp_path / "sig.jsonl",
        [
            {
                "ticker": _PAIR,
                "timestamp": "2026-08-01T12:00:00Z",
                "fng_index": 0,
            }
        ],
    )
    merged = merge_extra_features(frame, str(signals), ticker=_PAIR)

    # A genuine reading of 0 ("extreme fear") on 12:00/13:00, nothing after.
    assert list(merged["fng_index"]) == [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert list(merged["signal_observed"]) == [
        True,
        True,
        False,
        False,
        False,
        False,
    ]

    pipeline = FeaturePipeline(windows=[1, 4], feature_groups=["signals"])
    pipeline.fit(merged, ticker_id=_TICKER_ID)
    stats = pipeline.stats_for(_TICKER_ID)
    assert stats is not None

    # The pair is part of what the policy is fitted and observed on.
    assert "signal_age_hours" in stats.feature_names
    assert "signal_observed" in stats.feature_names

    # Exactly what TradingEnvironment._raw_feature_array computes.
    filled = pipeline.compute(merged).ffill().fillna(0.0)
    normalized = stats.normalize(filled)

    # A NaN here would poison every downstream affine image.  (Checked
    # with isfinite, not ``.all()``: a legitimately zero z-score is
    # falsy and would make ``.all()`` false.)
    assert np.isfinite(normalized.to_numpy(dtype=float)).all()

    # The z-scored flag really does separate observed from unobserved —
    # i.e. "no record" stays distinguishable from a genuine 0.
    assert normalized["signal_observed"].nunique() == 2
    assert normalized["signal_age_hours"].nunique() > 1


# ---------------------------------------------------------------------------
# 3. the shape paper trade reads in (no `since`) still carries the pair
# ---------------------------------------------------------------------------
def test_freshness_columns_survive_the_no_since_read(tmp_path) -> None:
    """Paper trade deliberately passes no ``since``; that read still works.

    ``PaperTrader._fetch_data`` calls ``read_ohlc_dataframe`` without
    ``since`` and lets the paginator take the tail.  Nothing about the
    freshness pair depends on ``since`` — the ages are computed against
    the frame's own bar index — but that is exactly the kind of claim
    worth pinning at the level the consumer actually uses, since the
    merge reindexes onto whatever bars it was handed.
    """
    signals = _write_signals(
        tmp_path / "news.jsonl",
        [
            {
                "ticker": _PAIR,
                "timestamp": "2026-08-01T12:00:00Z",
                "sentiment_score": 0.4,
            }
        ],
    )

    df = read_ohlc_dataframe(
        _PAIR,
        60,
        pages=2,
        manager=FakeSource(_candles(6)),
        extra_features_file=str(signals),
        signal_max_age_hours=None,
        signal_require_ticker=True,
    )

    assert "signal_age_hours" in df.columns
    assert "signal_observed" in df.columns
    assert list(df["sentiment_score"]) == [0.4, 0.4, 0.0, 0.0, 0.0, 0.0]
    assert list(df["signal_age_hours"]) == [0.0, 1.0, -1.0, -1.0, -1.0, -1.0]
    assert list(df["signal_observed"]) == [True, True, False, False, False, False]


# ---------------------------------------------------------------------------
# 4. the documented default: null is ONE HOUR of carry, on any bar interval
# ---------------------------------------------------------------------------
def test_null_max_age_hours_is_one_hour_of_carry_on_any_bar_interval(
    tmp_path,
) -> None:
    """Pin the shipped meaning of ``signal_max_age_hours: null``.

    Phase 4B shipped the null default as *derived* — one hour of carry,
    the seam's own hourly resolution — not as *unbounded*.  The config
    comment promises it "derives the bound from the bar interval", so
    this checks both halves of that promise on two bar intervals:

    * hourly bars    -> 1 bar of carry;
    * 15-minute bars -> 4 bars of carry, and ``signal_age_hours``
      advances by 0.25 per bar, because the column is in *hours* and not
      in bars.

    The 15-minute case is the load-bearing one: a bound implemented as
    "carry N bars" instead of "carry N hours" would pass an hourly-only
    test and then carry four times too long on finer bars.
    """
    hourly = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
        index=pd.date_range("2026-08-01T12:00:00Z", periods=6, freq="h"),
    )
    quarter = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
        index=pd.date_range("2026-08-01T12:00:00Z", periods=8, freq="15min"),
    )
    signals = _write_signals(
        tmp_path / "sig.jsonl",
        [
            {
                "ticker": _PAIR,
                "timestamp": "2026-08-01T12:00:00Z",
                "funding_rate": 0.0001,
            }
        ],
    )

    # Hourly bars: the reading is observed for one full hour of ages —
    # its own bar plus one carried bar.
    h = merge_extra_features(
        hourly, str(signals), ticker=_PAIR, max_age_hours=None
    )
    assert list(h["funding_rate"]) == [0.0001, 0.0001, 0.0, 0.0, 0.0, 0.0]
    assert list(h["signal_age_hours"]) == [0.0, 1.0, -1.0, -1.0, -1.0, -1.0]

    # 15-minute bars: the same one *hour* of ages, i.e. the own bar plus
    # four carried 15-minute bars, aged in hours at 0.25 per bar.
    q = merge_extra_features(
        quarter, str(signals), ticker=_PAIR, max_age_hours=None
    )
    assert list(q["funding_rate"]) == [0.0001] * 5 + [0.0] * 3
    assert list(q["signal_age_hours"]) == [
        0.0,
        0.25,
        0.5,
        0.75,
        1.0,
        -1.0,
        -1.0,
        -1.0,
    ]


# ---------------------------------------------------------------------------
# 5. every consumer forwards the keys (structural guard)
# ---------------------------------------------------------------------------
def test_every_consumer_threads_both_signal_keys() -> None:
    """All four consumers must forward all five signal keys.

    ``export`` is proved behaviourally above.  ``backtest`` and
    ``paper_trade`` can only be reached through a trained model on disk,
    which would make this guard cost a training run per assertion for
    two lines of plumbing — so this checks the call sites directly, by
    parsing each consumer module and reading the keyword arguments of
    its ``read_ohlc_dataframe`` call.

    The trade-off is deliberate and worth stating: this proves the
    keywords are *present*, not that the consumer's runtime behaviour is
    correct.  Behaviour is covered for the config hop (above) and for
    the seam itself (``tests/test_rl_data_store.py``); what this adds is
    early, cheap failure when someone edits one of the four call sites.
    """
    missing: list[str] = []
    for name in _CONSUMERS:
        module = __import__(
            f"kraken_trading_bot.rl.{name}", fromlist=["*"]
        )
        source = Path(inspect.getsourcefile(module) or "")
        tree = ast.parse(source.read_text(encoding="utf-8"))

        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "read_ohlc_dataframe"
        ]
        if not calls:
            missing.append(f"{name}: no read_ohlc_dataframe call found")
            continue

        keywords = {kw.arg for kw in calls[0].keywords if kw.arg}
        for key in _SIGNAL_KEYS:
            if key not in keywords:
                missing.append(f"{name}: does not forward {key}")

    assert not missing, "signal config wiring gaps: " + "; ".join(missing)


def test_paper_trade_deliberately_passes_no_since() -> None:
    """``since`` stays unset on paper trade's read; the freshness pair is
    computed against the frame's own bars, so it does not depend on it.

    Pins the assumption the no-``since`` test above relies on: if paper
    trade ever starts slicing a window with ``since``, that test's
    fixture stops matching what the consumer does and has to be revisited.
    """
    module = __import__("kraken_trading_bot.rl.paper_trade", fromlist=["*"])
    source = Path(inspect.getsourcefile(module) or "")
    tree = ast.parse(source.read_text(encoding="utf-8"))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "read_ohlc_dataframe"
    ]
    assert calls, "paper_trade no longer calls read_ohlc_dataframe"
    assert "since" not in {kw.arg for kw in calls[0].keywords if kw.arg}# ---------------------------------------------------------------------------
# 4. Gap-1 widening: the activated columns reach the observation
# ---------------------------------------------------------------------------
def test_funding_signal_columns_round_trip_through_merge(tmp_path) -> None:
    """Every new ``_SIGNAL_COLUMNS`` member survives the merge allow-list.

    The allow-list is ``_SIGNAL_COLUMNS`` minus the freshness pair, so a
    name absent from that tuple is dropped by the seam *before* the
    feature pipeline ever sees the frame.  These three were exactly that:
    emitted by the sibling into every funding JSONL record, then
    discarded.
    """
    from kraken_trading_bot.rl.features import _SIGNAL_COLUMNS

    frame = pd.DataFrame(
        {
            "open": 2000.0,
            "high": 2001.0,
            "low": 1999.0,
            "close": 2000.0,
            "vwap": 2000.5,
            "volume": 10.0,
            "count": 7,
        },
        index=pd.date_range(
            "2026-08-01T12:00:00Z", periods=6, freq="h"
        ),
    )
    signals = _write_signals(
        tmp_path / "funding.jsonl",
        [
            {
                "ticker": _PAIR,
                "timestamp": "2026-08-01T12:00:00Z",
                "funding_rate": 0.0001,
                "basis": 0.0002,
                "open_interest": 1000.0,
                # The two newly-whitelisted funding columns:
                "funding_rate_prediction": 0.0003,
                "vol24h": 42000.5,
                # And the builder inputs for `spread`.
                "bid": 2000.0,
                "ask": 2000.4,
            }
        ],
    )

    merged = merge_extra_features(frame, str(signals), ticker=_PAIR)

    for name in ("funding_rate_prediction", "vol24h", "bid", "ask"):
        assert name in _SIGNAL_COLUMNS, f"{name} must be on the allow-list"
        assert name in merged.columns, f"{name} was dropped at the seam"
    assert merged["funding_rate_prediction"].iloc[0] == pytest.approx(0.0003)
    assert merged["vol24h"].iloc[0] == pytest.approx(42000.5)
    assert merged["bid"].iloc[0] == pytest.approx(2000.0)


def test_bid_ask_reach_the_observation_as_spread_only(tmp_path) -> None:
    """Clean +1 accounting: `spread` yes, raw dollar `bid`/`ask` no.

    Whitelisting the funding bid/ask *naively* would put two more
    dollar-scale columns in the observation (net +3 for one ratio).  They
    are builder inputs: `_add_microstructure_features` owns `spread`.
    """
    frame = pd.DataFrame(
        {
            "open": 2000.0,
            "high": 2001.0,
            "low": 1999.0,
            "close": 2000.0,
            "vwap": 2000.5,
            "volume": 10.0,
            "count": 7,
        },
        index=pd.date_range(
            "2026-08-01T12:00:00Z", periods=6, freq="h"
        ),
    )
    signals = _write_signals(
        tmp_path / "funding.jsonl",
        [
            {
                "ticker": _PAIR,
                "timestamp": "2026-08-01T12:00:00Z",
                "bid": 2000.0,
                "ask": 2000.4,
            }
        ],
    )
    merged = merge_extra_features(frame, str(signals), ticker=_PAIR)
    assert {"bid", "ask"}.issubset(merged.columns)  # builder inputs present

    pipeline = FeaturePipeline(
        windows=[1, 4], feature_groups=["microstructure", "signals"]
    )
    computed = pipeline.compute(merged)

    assert "spread" in computed.columns
    assert "bid" not in computed.columns
    assert "ask" not in computed.columns
    # (ask - bid) / bid
    assert computed["spread"].iloc[0] == pytest.approx(0.4 / 2000.0)


def test_all_six_activated_columns_reach_the_observation(tmp_path) -> None:
    """End-to-end: the six names land in the fitted observation width.

    Asserts on ``stats.feature_names``, i.e. on what the policy is
    actually fitted and observed on, not on an intermediate frame.
    """
    n = 120
    idx = pd.date_range("2026-08-01T00:00:00Z", periods=n, freq="h")
    rng = np.random.default_rng(11)
    close = 2000.0 + np.cumsum(rng.normal(0.0, 0.7, n))
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "vwap": close - 0.5,
            "volume": rng.uniform(10.0, 50.0, n),
            "count": rng.integers(5, 50, n).astype(float),
        },
        index=idx,
    )
    signals = _write_signals(
        tmp_path / "funding.jsonl",
        [
            {
                "ticker": _PAIR,
                "timestamp": "2026-08-01T12:00:00Z",
                "funding_rate_prediction": 0.0003,
                "vol24h": 42000.5,
                "bid": 2000.0,
                "ask": 2000.4,
            }
        ],
    )
    # The read seam derives the three OHLCV-only scalars first, then the
    # signal merge adds the funding columns — the real order every
    # consumer goes through.
    merged = merge_extra_features(
        add_derived_ohlcv_features(frame), str(signals), ticker=_PAIR
    )

    pipeline = FeaturePipeline(windows=[1, 4, 24])
    pipeline.fit(merged, ticker_id=_TICKER_ID)
    stats = pipeline.stats_for(_TICKER_ID)
    assert stats is not None

    for name in (
        "vwap_dev",
        "trade_count_zscore_20",
        "volume_per_trade",
        "funding_rate_prediction",
        "vol24h",
        "spread",
    ):
        assert name in stats.feature_names, f"{name} missing from the observation"
    # Clean accounting: the builder inputs stay out.
    for name in ("bid", "ask"):
        assert name not in stats.feature_names


def test_config_points_at_a_real_funding_file_with_a_12h_bound() -> None:
    """The activation that keeps the three funding columns non-silent.

    A ``null`` ``funding_features_file`` means ``funding_rate_prediction``,
    ``vol24h`` and ``spread`` exist in code and produce zero in
    production.  The bound matters for the same reason: funding settles
    ~8-hourly, so the null->1h default would mark most bars unobserved.
    """
    repo = Path(__file__).resolve().parents[1]
    default_cfg = yaml.safe_load(
        (repo / "configs" / "default.yaml").read_text(encoding="utf-8")
    )
    funding = default_cfg["funding_features_file"]
    assert funding, "funding_features_file must name a real path"
    assert str(funding).endswith(".jsonl")
    assert default_cfg["signal_max_age_hours"] == 12

    deep_cfg = yaml.safe_load(
        (repo / "configs" / "deep-history.example.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert deep_cfg["funding_features_file"], "example config must agree"
    assert deep_cfg["signal_max_age_hours"] == 12