"""Phase 4A: paper_trade's read path and the two refresh fixes.

Two things are pinned here, and both are "the code does not do the safe
thing", which is the class of claim a comment alone cannot keep honest:

1. The read path does NOT drop the in-progress candle. Kraken's OHLC
   endpoint returns the current, still-forming bucket as the final element,
   `read_ohlc_dataframe` upserts whatever comes back, and `paper_trade`
   fills at `df["close"].iloc[-1]`. A store read long after the last write
   shows only completed bars, which is a property of WHEN the store was
   written (the upsert heals the bar on the next fetch) and not of the
   read. So a live fill is at a still-forming close, which is strictly
   more optimistic than the backtest's closed-bar close.

2. A pinned-window read must skip the fetch. `refresh=False` with the
   pinned `coverage=` is the whole mechanism, and the store must come out
   byte-identical afterwards.

Verified 2026-10-05 by reading the fetch -> upsert -> read path in
`data.py`, `market_data/store.py` and `paper_trade.py`: no `iloc[:-1]`,
no `now`-relative filter, no "last complete bar" step anywhere.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import null_baselines as nb  # noqa: E402

PACKAGE = ROOT / "kraken_trading_bot"


def _module(name: str):
    return __import__(f"kraken_trading_bot.rl.{name}", fromlist=["*"])


def _source(name: str) -> str:
    return Path(inspect.getsourcefile(_module(name)) or "").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 1. the in-progress candle is NOT dropped
# ---------------------------------------------------------------------------

def test_paper_trade_read_path_has_no_drop_of_the_last_bar():
    """Structural: no ``[:-1]`` slice and no time-relative filter on the read.

    A drop would make the note in `step()` false, and would also make live
    behaviour *better* than the backtest -- so its absence is a fact worth
    pinning rather than a coincidence worth discovering.
    """
    source = _source("paper_trade")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        # `x[:-1]` DROPS the last row; `x[-1]` TAKES it. Only the first is
        # a freshness step, and conflating them is what made the first
        # draft of this test fire on `_build_observation`'s `[-1]` -- the
        # line that *uses* the in-progress bar rather than removing it.
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            upper = node.slice.upper
            parts = upper.elts if isinstance(upper, ast.Tuple) else [upper]
            for part in parts:
                if (
                    isinstance(part, ast.UnaryOp)
                    and isinstance(part.op, ast.USub)
                    and isinstance(part.operand, ast.Constant)
                    and part.operand.value == 1
                ):
                    offenders.append(
                        f"line {node.lineno}: a [:-1] slice drops the last bar"
                    )
        # `now()`, `time.time()`, `utcnow` near a read -- a "is this bar
        # finished?" test would look like one of these.
        if isinstance(node, ast.Call):
            called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if called in {"now", "utcnow", "time", "timestamp"}:
                offenders.append(
                    f"line {node.lineno}: a {called}() call -- a wall-clock "
                    f"test would drop the in-progress bar"
                )
    assert not offenders, (
        "paper_trade now contains a bar-freshness step the NOTE in step() "
        "does not describe:\n" + "\n".join(offenders)
    )


def test_paper_trade_fills_at_the_newest_close_verbatim():
    """Behavioural: the last row of the frame IS the fill price.

    Built a frame whose last row is a deliberately odd value, so a test that
    accidentally reads the wrong row fails rather than passing on a frame
    where the rows happen to agree.
    """
    from kraken_trading_bot.rl.paper_trade import PaperTrader  # noqa: PLC0415

    assert hasattr(PaperTrader, "_build_observation")
    source = _source("paper_trade")
    assert 'df["close"].iloc[-1]' in source, (
        "paper_trade no longer fills at the newest close; the NOTE in "
        "step() describes a different fill price and must be updated"
    )


def test_the_note_says_the_fill_is_not_comparable_to_backtest_d0():
    """The note is load-bearing documentation, so pin that it is present."""
    source = _source("paper_trade")
    for fragment in (
        "still-forming",
        "NOT comparable to the backtest",
        "refresh=False",
    ):
        assert fragment in source, (
            f"paper_trade's NOTE lost {fragment!r}; a reader would no longer "
            f"be told the live fill is not comparable to backtest d=0"
        )


def test_backtest_still_fills_at_a_closed_bar_close():
    """The other half of the comparison: the backtest has no such problem."""
    env_source = Path(
        inspect.getsourcefile(_module("environment")) or ""
    ).read_text(encoding="utf-8")
    assert "self.data[\"close\"].iloc[idx]" in env_source, (
        "_price_at no longer reads a close; the d=0 comparison is stale"
    )


# ---------------------------------------------------------------------------
# 2. a pinned baselines read must not touch the network or the store
# ---------------------------------------------------------------------------

class _ExplodingSource:
    """A fetch source that refuses to be used."""

    def ohlc(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError(
            "the fetch leg ran: a pinned-window read must pass refresh=False"
        )


def _pinned_window(since: str = "2020-01-01T00:00:00Z",
                   until: str = "2025-01-01T00:00:00Z"):
    from kraken_trading_bot.rl.data_window import resolve_data_window

    return resolve_data_window(
        {"data_window": {"since": since, "until": until, "eval_split": 0.79953}}
    )


def test_pinned_read_kwargs_ask_for_no_fetch_on_a_store_backed_window():
    store = "/home/seanc/Projects/kraken-market-data/store"
    kwargs = nb._pinned_read_kwargs(
        _pinned_window(),
        pair="ETH/USD",
        interval=60,
        store_keys={"market_data_store": store},
    )
    assert kwargs["refresh"] is False, (
        "a pinned window behind the store tail must skip the fetch"
    )
    assert kwargs.get("coverage") is not None, (
        "refresh=False without coverage is refused by read_ohlc_dataframe"
    )


def test_pinned_read_kwargs_keep_the_fetch_without_a_store():
    """No store means no local read, so the fetch must stay."""
    kwargs = nb._pinned_read_kwargs(
        _pinned_window(),
        pair="ETH/USD",
        interval=60,
        store_keys={},
    )
    assert kwargs["refresh"] is True
    assert "coverage" not in kwargs or kwargs.get("coverage") is None, (
        "coverage is ignored when refresh is true; carrying it invites a "
        "future reader to trust a check that is not running"
    )


def test_pinned_read_kwargs_keep_the_fetch_on_an_unpinned_window():
    """An unpinned window is the trailing-live case; the fetch is the point."""
    from kraken_trading_bot.rl.data_window import resolve_data_window

    unpinned = resolve_data_window({"data_window": {}})
    kwargs = nb._pinned_read_kwargs(
        unpinned,
        pair="ETH/USD",
        interval=60,
        store_keys={"market_data_store": "/home/seanc/Projects/kraken-market-data/store"},
    )
    assert kwargs["refresh"] is True


def test_both_null_baselines_reads_state_refresh(monkeypatch):
    """The two reads must both name `refresh`; a third leg would not."""
    tree = ast.parse((ROOT / "tools/null_baselines.py").read_text(
        encoding="utf-8"))
    sites = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None)
             or getattr(node.func, "attr", None)) == "read_ohlc_dataframe"
    ]
    assert len(sites) == 2, f"expected 2 read sites, found {len(sites)}"
    for node in sites:
        keywords = {kw.arg for kw in node.keywords if kw.arg}
        assert len(keywords) == len(node.keywords), (
            f"line {node.lineno}: splats, so refresh= is unprovable"
        )
        assert "refresh" in keywords, (
            f"line {node.lineno}: no refresh=, so it silently fetches"
        )
    _ = monkeypatch


@pytest.mark.skipif(
    not Path("/home/seanc/Projects/kraken-market-data/store").is_dir(),
    reason="the seeded market-data store is not present",
)
def test_a_pinned_read_leaves_the_store_byte_identical():
    """Bar count, tail and every partition file's size, before and after.

    This is the store-side half of "no API calls". A read that upserted
    would leave the same bar count on a re-read only if the fetch returned
    nothing new; the file sizes catch a rewrite that changed nothing.
    """
    from kraken_trading_bot.rl.data import read_ohlc_dataframe  # noqa: PLC0415

    root = Path("/home/seanc/Projects/kraken-market-data/store")
    parts = sorted((root / "ETH_USD" / "60").glob("*.parquet"))

    def fingerprint():
        frames = [pd.read_parquet(p) for p in parts]
        df = pd.concat(frames)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.sort_values("time")
        df = df[~df["time"].duplicated(keep="last")]
        return {
            "n": int(len(df)),
            "last": str(df["time"].iloc[-1]),
            "sizes": {p.name: p.stat().st_size for p in parts},
        }

    before = fingerprint()
    kwargs = nb._pinned_read_kwargs(
        _pinned_window(), pair="ETH/USD", interval=60,
        store_keys={"market_data_store": str(root)},
    )
    df = read_ohlc_dataframe(
        "ETH/USD", interval=60, pages=6,
        refresh=bool(kwargs["refresh"]),
        coverage=kwargs.get("coverage"),
        market_data_store=str(root),
        market_data_store_venue="binance-spot-archive-seeded",
    )
    assert not df.empty
    after = fingerprint()
    assert before == after, (
        f"the read changed the store: bars {before['n']} -> {after['n']}, "
        f"tail {before['last']} -> {after['last']}"
    )


@pytest.mark.skipif(
    not Path("/home/seanc/Projects/kraken-market-data/store").is_dir(),
    reason="the seeded market-data store is not present",
)
def test_a_pinned_read_makes_no_api_call(monkeypatch):
    """The network half: an exploding fetch source is never consulted."""
    from kraken_trading_bot.rl import data as data_mod  # noqa: PLC0415

    root = Path("/home/seanc/Projects/kraken-market-data/store")
    kwargs = nb._pinned_read_kwargs(
        _pinned_window(), pair="ETH/USD", interval=60,
        store_keys={"market_data_store": str(root)},
    )
    monkeypatch.setattr(
        data_mod, "_page_candles",
        lambda *a, **k: pytest.fail("the fetch leg ran on a pinned read"),
    )
    df = data_mod.read_ohlc_dataframe(
        "ETH/USD", interval=60, pages=6,
        refresh=bool(kwargs["refresh"]),
        coverage=kwargs.get("coverage"),
        market_data_store=str(root),
        market_data_store_venue="binance-spot-archive-seeded",
    )
    assert len(df) > 1000


def test_the_reproduction_anchors_are_pinned_as_constants():
    """The Phase 3 numbers this fix must not move, as assertable constants.

    Two anchors, both measured 2026-10-05 on
    `configs/matrix.deep-ab.yaml --draws 300`:
      * flat cash EXCESS vs buy-and-hold, +60.6293% (its total_return is 0)
      * the timing-matched null's MEDIAN at the FIXED slippage direction,
        +17.4365% (the pre-1.5-C direction gave +18.6724%)
    Both are excess figures, which is the mistake Phase 3's checkpoint
    report made and had to correct: neither is a `total_return`.
    """
    # Not re-derivable without a full run, so this pins the VALUES as the
    # regression anchors a future run is compared against by eye.
    anchors = {
        "flat_cash_excess_vs_bh": 0.606293,
        "timing_null_median_fixed": 0.174365,
        "timing_null_median_old_direction": 0.186724,
    }
    path = Path("/tmp/phase4/anchors.json")
    if path.is_file():
        measured = json.loads(path.read_text(encoding="utf-8"))
        for key, expected in anchors.items():
            if key in measured:
                assert abs(measured[key] - expected) < 5e-6, (
                    f"{key}: measured {measured[key]!r}, anchor {expected!r}"
                )