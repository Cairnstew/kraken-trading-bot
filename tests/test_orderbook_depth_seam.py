"""The order-book depth channel: flattener + seam wiring (G-1, 2026-10-08).

The recorder (``record-depth``, :mod:`kraken_trading_bot.depth_recorder`)
writes the raw book verbatim — ``bids``/``asks`` as lists of
``[price, volume, order_ts]`` — and reads none of it back.  This pass wires it
into the RL observation: :func:`_flatten_orderbook_records` reduces each nested
record to flat per-hour scalars, the seam carries ``bid_vol``/``ask_vol`` to
the frame, and the existing ``_add_microstructure_features`` builder turns
them into the single ``order_book_imbalance`` observation column.

These tests are offline and hermetic: a synthetic OHLCV frame, a synthetic
depth JSONL, no network, nothing committed.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from kraken_trading_bot.rl.data import (
    _flatten_orderbook_records,
    merge_extra_features,
)
from kraken_trading_bot.rl.features import (
    _SIGNAL_BUILDER_INPUT_COLUMNS,
    _SIGNAL_COLUMNS,
    FeaturePipeline,
)


# ---------------------------------------------------------------------------
# fixtures / builders
# ---------------------------------------------------------------------------
def _ohlc_frame(bars: int = 6, start_hour: int = 12) -> pd.DataFrame:
    """A flat OHLCV frame of ``bars`` hourly bars from 2026-08-01T12:00Z."""
    index = pd.to_datetime(
        [
            f"2026-08-01T{hour:02d}:00:00Z"
            for hour in range(start_hour, start_hour + bars)
        ],
        utc=True,
    )
    return pd.DataFrame(
        {
            "open": 1000.0,
            "high": 1000.0,
            "low": 1000.0,
            "close": 1000.0,
            "volume": 10.0,
        },
        index=index,
    )


def _levels(n: int, *, side: str, vol: float, base: float = 1000.0) -> list[list]:
    """``n`` depth levels ``[price_str, vol_str, ts]`` stepping away from base."""
    sign = -1.0 if side == "bid" else 1.0
    return [
        [f"{base + sign * 0.1 * i:.5f}", f"{vol:.3f}", 0]
        for i in range(1, n + 1)
    ]


def _depth_record(
    hour: int,
    *,
    pair: str = "ETH/USD",
    n_bid: int = 10,
    n_ask: int = 10,
    bid_vol: float = 1.0,
    ask_vol: float = 1.0,
    best_bid: float = 999.9,
    best_ask: float = 1000.1,
    truncated: bool = False,
) -> dict:
    """One depth record shaped exactly like ``depth_recorder.book_to_record``."""
    mid = (best_bid + best_ask) / 2.0
    return {
        "recorded_at": f"2026-08-01T{hour:02d}:10:00.000000+00:00",
        "hour": f"2026-08-01T{hour:02d}:00:00+00:00",
        "pair": pair,
        "bids": _levels(n_bid, side="bid", vol=bid_vol),
        "asks": _levels(n_ask, side="ask", vol=ask_vol),
        "best_bid": f"{best_bid:.5f}",
        "best_ask": f"{best_ask:.5f}",
        "mid": f"{mid:.5f}",
        "spread": f"{best_ask - best_bid:.5f}",
        "depth": {
            "requested_count": 100,
            "bid_levels": n_bid,
            "ask_levels": n_ask,
            "levels_total": n_bid + n_ask,
            "truncated": truncated,
        },
        "interval": 60,
        "schema_version": 1,
        "source": "kraken.public.Depth",
    }


def _depth_file(tmp_path, records: list[dict]) -> str:
    path = tmp_path / "eth_usd_orderbook.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return str(path)


# ---------------------------------------------------------------------------
# _flatten_orderbook_records — unit
# ---------------------------------------------------------------------------
def test_flattener_maps_timestamp_and_ticker_and_sums_top_n():
    rec = _depth_record(12, n_bid=10, n_ask=10, bid_vol=2.0, ask_vol=3.0)
    (flat,) = _flatten_orderbook_records([rec])
    # timestamp <- recorded_at; ticker <- pair (the record has no `ticker` key).
    assert flat["timestamp"] == rec["recorded_at"]
    assert flat["ticker"] == "ETH/USD"
    # 10 levels x vol per side.
    assert flat["bid_vol"] == pytest.approx(20.0)
    assert flat["ask_vol"] == pytest.approx(30.0)


def test_flattener_realized_spread_bps_and_never_spread():
    rec = _depth_record(12, best_bid=999.9, best_ask=1000.1)
    (flat,) = _flatten_orderbook_records([rec])
    mid = (999.9 + 1000.1) / 2.0
    assert flat["realized_spread_bps"] == pytest.approx((1000.1 - 999.9) / mid * 1e4)
    # `spread` is reserved for the funding channel; a depth `spread` merged
    # last would overwrite it, so the flattener must never emit it.
    assert "spread" not in flat


def test_flattener_top_n_levels_is_configurable():
    rec = _depth_record(12, n_bid=10, bid_vol=1.0)
    (flat,) = _flatten_orderbook_records([rec], levels=3)
    assert flat["bid_vol"] == pytest.approx(3.0)


def test_flattener_empty_sides_are_zero_not_an_error():
    rec = _depth_record(12)
    rec["bids"] = []
    rec["asks"] = []
    (flat,) = _flatten_orderbook_records([rec])
    assert flat["bid_vol"] == 0.0
    assert flat["ask_vol"] == 0.0


def test_flattener_refuses_truncated_records():
    ok = _depth_record(12)
    bad = _depth_record(13, truncated=True)
    flat = _flatten_orderbook_records([ok, bad])
    assert len(flat) == 1
    assert flat[0]["timestamp"] == ok["recorded_at"]


def test_flattener_passes_non_depth_records_through_unchanged():
    other = {
        "timestamp": "2026-08-01T12:00:00Z",
        "ticker": "ETH/USD",
        "sentiment_score": 0.5,
    }
    assert _flatten_orderbook_records([other]) == [other]


def test_flattener_empty_input_is_empty_output():
    assert _flatten_orderbook_records([]) == []


# ---------------------------------------------------------------------------
# merge_extra_features — the seam
# ---------------------------------------------------------------------------
def test_depth_channel_merges_bid_vol_and_ask_vol(tmp_path):
    path = _depth_file(
        tmp_path,
        [_depth_record(h, bid_vol=2.0, ask_vol=3.0) for h in (12, 13, 14)],
    )
    merged = merge_extra_features(_ohlc_frame(6), path, ticker="ETH/USD")
    assert "bid_vol" in merged.columns
    assert "ask_vol" in merged.columns
    row = merged.loc[merged.index.hour == 12]
    assert row["bid_vol"].iloc[0] == pytest.approx(20.0)
    assert row["ask_vol"].iloc[0] == pytest.approx(30.0)


def test_depth_channel_never_writes_spread(tmp_path):
    path = _depth_file(tmp_path, [_depth_record(12)])
    merged = merge_extra_features(_ohlc_frame(6), path, ticker="ETH/USD")
    # The depth record carries a `spread`; the flattener must not surface it.
    assert "spread" not in merged.columns
    # `realized_spread_bps` is emitted by the flattener but is not on the
    # observation allow-list, so the seam drops it too.
    assert "realized_spread_bps" not in merged.columns


def test_depth_pair_rename_keeps_the_ticker_filter_on(tmp_path):
    """A BTC/USD record must not annotate an ETH bar (pair -> ticker rename)."""
    path = _depth_file(
        tmp_path,
        [
            _depth_record(12, pair="ETH/USD", bid_vol=5.0),
            _depth_record(13, pair="BTC/USD", bid_vol=9.0),
        ],
    )
    # max_age_hours=0 disables the bounded carry, so an hour with no ETH
    # record is honestly absent rather than ffilled from hour 12.
    merged = merge_extra_features(
        _ohlc_frame(6), path, ticker="ETH/USD", max_age_hours=0
    )
    eth = merged.loc[merged.index.hour == 12, "bid_vol"].iloc[0]
    btc = merged.loc[merged.index.hour == 13, "bid_vol"].iloc[0]
    assert eth == pytest.approx(50.0)
    # The BTC hour must be absent (0.0 fill), never BTC's 90.0.
    assert btc == pytest.approx(0.0)
    assert not (merged["bid_vol"] == 90.0).any()


# ---------------------------------------------------------------------------
# the observation — order_book_imbalance lights up, raw volumes do not
# ---------------------------------------------------------------------------
def test_order_book_imbalance_reaches_the_feature_frame_non_constant(tmp_path):
    # Vary the bid/ask volume so the imbalance is non-constant across hours.
    path = _depth_file(
        tmp_path,
        [
            _depth_record(12, bid_vol=1.0, ask_vol=3.0),
            _depth_record(13, bid_vol=3.0, ask_vol=1.0),
            _depth_record(14, bid_vol=2.0, ask_vol=2.0),
        ],
    )
    merged = merge_extra_features(_ohlc_frame(6), path, ticker="ETH/USD")
    out = FeaturePipeline(windows=[1, 4, 24]).compute(merged)
    assert "order_book_imbalance" in out.columns
    values = out["order_book_imbalance"].dropna()
    assert values.nunique() > 1


def test_bid_vol_ask_vol_are_builder_inputs_not_observation_columns():
    # Listed on the seam allow-list so the frame carries them...
    assert "bid_vol" in _SIGNAL_COLUMNS
    assert "ask_vol" in _SIGNAL_COLUMNS
    # ...but marked as builder inputs, which `_add_signals_features` skips,
    # so only the derived `order_book_imbalance` reaches the observation.
    assert "bid_vol" in _SIGNAL_BUILDER_INPUT_COLUMNS
    assert "ask_vol" in _SIGNAL_BUILDER_INPUT_COLUMNS
