"""The three exogenous channels, end to end, on all four legs.

Every other guard in this repo about the signal seam is either structural
or single-channel:

* ``tests/test_rl_signal_config_wiring.py::test_every_consumer_threads_both_signal_keys``
  and ``::test_every_read_ohlc_dataframe_call_site_forwards_venue`` read the
  **AST** of the call sites.  That proves a keyword is *written*.  It cannot
  prove the file behind the keyword is readable, cannot prove the three files
  land in one frame together, and cannot prove the frame the leg went on to
  use carries the columns.
* ``tests/test_rl_signal_config_wiring.py::test_yaml_config_keys_reach_the_merge_through_export``
  is behavioural, but drives **one channel at a time** through **one leg**
  (``export``).
* ``tests/test_gc_channel_activation.py::test_every_consumer_threads_all_three_signal_file_keys``
  names the three keys specifically and is likewise structural.

So the runtime claim this pass actually rests on — *all three channels reach
the observation on every leg* — had no test behind it.  This file is that
test.  It is deliberately behavioural and hermetic: a mock Kraken source,
synthetic candles, and model artifacts under pytest ``tmp_path``.  No
network, no credentials, no orders, nothing committed.

Merge order is a separate matter and gets its own treatment below, because
no observable value can reveal it: the three channels share no column name
(see :data:`kraken_trading_bot.rl.features._SIGNAL_COLUMNS`), and the two
names they *do* share — ``signal_age_hours`` / ``signal_observed`` — are
combined order-insensitively by design (max / any).  Order is therefore a
property of the call, not of the output, and is pinned structurally.
"""

from __future__ import annotations

import ast
import json
import inspect
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml
from kraken_api.models import Candle

from kraken_trading_bot.rl.backtest import backtest_model
from kraken_trading_bot.rl.data import (
    SignalFileNotFoundError,
    _SIGNAL_CHANNELS,
    read_ohlc_dataframe,
)
from kraken_trading_bot.rl.export import build_export_frame
from kraken_trading_bot.rl.paper_trade import PaperTrader
from kraken_trading_bot.rl.train import train_ticker

_REPO = Path(__file__).resolve().parents[1]

_PAIR = "ETH/USD"
_TICKER_ID = "ETH_USD"
_MODEL = "ppo_three_channel"

# 2026-01-01T00:00:00Z.  Fixed so a signal record can be placed at a known
# age relative to the first bar.
_BASE = 1767225600
_HOUR = 3600
_T0 = pd.Timestamp("2026-01-01T00:00:00Z")

# One column per channel that exists ONLY because that channel's file was
# merged.  These are written by ``merge_extra_features``, so they are present
# on the raw merged OHLCV frame every leg reads -- which is what makes them
# usable as the assertion on ``backtest``/``paper_trade``, whose reads return
# before any feature stage has run.
_CHANNEL_COLUMNS = {
    "extra_features_file": ("sentiment_score", "article_count", "novelty_flag"),
    "funding_features_file": (
        "funding_rate",
        "basis",
        "open_interest",
        "funding_rate_prediction",
        "vol24h",
    ),
    "social_features_file": ("stt_mention_count", "stt_tilt", "fng_index"),
}
# ``bid``/``ask`` ride the frame as BUILDER INPUTS and never reach the
# observation raw (features.py _SIGNAL_BUILDER_INPUT_COLUMNS); ``spread`` is
# not a merge column at all but the microstructure builder's single-writer
# derivation of them.  So all three are asserted separately, on the feature
# frame, and ``spread`` is the proof that bid/ask actually arrived.
_FRAME_ONLY = ("bid", "ask")
_DERIVED_FROM_FUNDING = ("spread",)
_FRESHNESS = ("signal_age_hours", "signal_observed")

# Every column a merge is expected to write, on a frame that went no further.
_MERGE_FRAME_COLUMNS = tuple(
    col for cols in _CHANNEL_COLUMNS.values() for col in cols
) + _FRAME_ONLY


# ---------------------------------------------------------------------------
# fakes and builders
# ---------------------------------------------------------------------------
def _candle(ts: int, close: float, volume: str = "100.0", count: int = 5) -> Candle:
    return Candle(
        pair=_PAIR,
        time=ts,
        open=str(close),
        high=str(close),
        low=str(close),
        close=str(close),
        vwap="",
        volume=volume,
        count=count,
    )


def _candles(n: int = 90, base: float = 2000.0, seed: int = 11) -> list[Candle]:
    """A deterministic random-walk candle sequence for the mock OHLC endpoint."""
    rng = np.random.default_rng(seed)
    closes = base * np.exp(np.cumsum(rng.normal(0.0, 0.005, n)))
    return [_candle(_BASE + i * _HOUR, float(c)) for i, c in enumerate(closes)]


class MockManager:
    """Hermetic ``KrakenManager`` stand-in: canned candles, recorded orders."""

    def __init__(self, candles: list[Candle]) -> None:
        self._candles = list(candles)
        self.ohlc_calls: list[int | None] = []

    def ohlc(self, pair: str, interval: int = 60, since: int | None = None):
        self.ohlc_calls.append(since)
        return self._candles, 0

    # PaperTrader probes for these; the absence of `paper_account` is the
    # signal it uses for its internal tracker.
    def buy(self, *args: Any, **kwargs: Any):  # pragma: no cover - unused
        return {"txid": ["MOCK-BUY"]}

    def sell(self, *args: Any, **kwargs: Any):  # pragma: no cover - unused
        return {"txid": ["MOCK-SELL"]}


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    return path


def _stamp(hours_after_t0: int) -> str:
    """ISO stamp ``hours_after_t0`` hours after the first bar.

    Offsets are POSITIVE and the assertion is made on a later bar, so every
    record sits inside the read window.  A record placed before the first
    bar would be outside it, which the seam reports as a WARNING and a
    zero-fill -- correct behaviour, and useless as a merge proof.
    """
    ts = _T0 + pd.Timedelta(hours=hours_after_t0)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# The bar the freshness-combine assertion reads: 8h after the first bar, with
# records at +0h (news), +4h (funding) and +6h (social), so the three ages
# there are 8 / 4 / 2 and the combined age must be the largest.
_COMBINE_BAR_HOURS = 8
_COMBINE_RECORD_OFFSETS = (0, 4, 6)
_COMBINE_AGES = (8, 4, 2)


def _three_channel_files(
    tmp_path: Path, *, offsets: tuple[int, int, int] = (0, 0, 0)
) -> dict[str, Path]:
    """One file per channel, each carrying a reading of a DIFFERENT age.

    Distinct ages matter: the freshness pair combines across channels by
    ``max`` (stalest live reading wins), so three different ages are what
    makes the combined value proof that all three merges happened rather
    than one merge happening three times.  With three identical ages a leg
    that merged only one channel would report the same number.
    """
    news_off, funding_off, social_off = offsets
    return {
        "extra_features_file": _write_jsonl(
            tmp_path / "news.jsonl",
            [
                {
                    "ticker": _PAIR,
                    "timestamp": _stamp(news_off),
                    "sentiment_score": 0.4,
                    "article_count": 7,
                    "novelty_flag": True,
                }
            ],
        ),
        "funding_features_file": _write_jsonl(
            tmp_path / "funding.jsonl",
            [
                {
                    "ticker": _PAIR,
                    "timestamp": _stamp(funding_off),
                    "funding_rate": 0.0001,
                    "basis": 0.5,
                    "open_interest": 10.0,
                    # The two funding-record columns the merge allow-list
                    # used to drop (features.py _SIGNAL_COLUMNS).
                    "funding_rate_prediction": 0.0002,
                    "vol24h": 123.0,
                    # bid/ask are BUILDER INPUTS: the micro builder turns
                    # them into the single `spread` column, and they must
                    # NOT reach the observation raw (features.py
                    # _SIGNAL_BUILDER_INPUT_COLUMNS).
                    "bid": 1999.0,
                    "ask": 2001.0,
                }
            ],
        ),
        "social_features_file": _write_jsonl(
            tmp_path / "social.jsonl",
            [
                {
                    "ticker": _PAIR,
                    "timestamp": _stamp(social_off),
                    "stt_mention_count": 30,
                    "stt_tilt": 0.25,
                    "fng_index": 42.0,
                }
            ],
        ),
    }


def _config(tmp_path: Path, files: dict[str, Path], **extra: Any) -> Path:
    """A runnable config with all three channels live, written to disk."""
    cfg: dict[str, Any] = {
        "ticker": _PAIR,
        "ohlcv_interval_minutes": 60,
        "feature_windows": [1, 4, 24],
        "feature_groups": [
            "price",
            "technical",
            "volume",
            "microstructure",
            "signals",
        ],
        "signal_max_age_hours": 12,
        "signal_require_ticker": True,
        # Set, so the train -> artifact hop can be asserted: `paper_trade`
        # reads this key off the ARTIFACT config, not off the config `train`
        # was handed, and a key dropped in between would silently put the
        # read back on data.DEFAULT_STORE_VENUE (G-B(i)).
        "market_data_store_venue": "binance-spot-archive-seeded",
        # str(), not Path: yaml.safe_dump cannot represent a PosixPath, and
        # the point of this config is that it round-trips through the loader.
        **{key: str(value) for key, value in files.items()},
        **extra,
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def _assert_all_three_channels_landed(
    frame: pd.DataFrame, where: str, *, features: bool
) -> None:
    """Every channel's own column is present AND carries its own value.

    ``features=True`` for a frame that has been through ``FeaturePipeline``
    (``export``'s output), ``False`` for the raw merged OHLCV frame a read
    returns.  The difference is not cosmetic: ``spread`` is derived by the
    microstructure builder, so demanding it of a raw frame would be asking
    for a stage that has not run.
    """
    wanted = _MERGE_FRAME_COLUMNS + (
        _DERIVED_FROM_FUNDING if features else ()
    )
    missing = [col for col in wanted if col not in frame.columns]
    assert not missing, f"{where}: channels did not reach the frame: {missing}"
    for col in _FRESHNESS:
        assert col in frame.columns, f"{where}: freshness column {col} absent"

    # Per-channel provenance: each channel carries numbers no other file
    # has, so a merge that silently skipped one shows up here.
    row = frame.iloc[0]
    expected = {
        # news
        "sentiment_score": 0.4,
        "article_count": 7.0,
        "novelty_flag": 1.0,
        # funding
        "funding_rate": 0.0001,
        "basis": 0.5,
        "open_interest": 10.0,
        "funding_rate_prediction": 0.0002,
        "vol24h": 123.0,
        # social
        "stt_mention_count": 30.0,
        "stt_tilt": 0.25,
        "fng_index": 42.0,
    }
    for col, want in expected.items():
        assert row[col] == pytest.approx(want), (
            f"{where}: {col}={row[col]!r}, expected {want} — a channel "
            "merged but its own values did not survive"
        )

    if features:
        # `spread` exists only because the funding file's bid/ask were
        # merged and the microstructure builder divided them, so it is the
        # strongest single proof that funding arrived on the raw scale.
        assert row["spread"] == pytest.approx(2.0 / 1999.0, rel=1e-6), (
            f"{where}: spread={row['spread']!r} — bid/ask did not arrive from "
            "the funding file"
        )


# ---------------------------------------------------------------------------
# 1. export-data: all three channels in ONE frame
# ---------------------------------------------------------------------------
def test_export_lands_all_three_channels_in_one_frame(tmp_path) -> None:
    """The cheap leg, driven end to end, carries news + funding + social.

    ``export`` composes the same stages ``train_ticker`` runs — config ->
    OHLC read -> three merges -> feature fit -> episode slice — so it is
    the cheapest place to observe the whole hop.  The three files are the
    real shapes the sibling CLIs write.
    """
    files = _three_channel_files(tmp_path)
    config = _config(tmp_path, files)

    frame = build_export_frame(
        _TICKER_ID,
        config_path=config,
        manager=MockManager(_candles()),
        pages=1,
    )

    _assert_all_three_channels_landed(frame, "export", features=True)


def test_the_freshness_pair_combines_across_all_three_channels(tmp_path) -> None:
    """``signal_age_hours`` is the STALEST of the three, not one of them.

    This is the assertion that distinguishes "three files were configured"
    from "three files were merged into one frame".  A leg that merged only
    one channel — or that merged the same channel three times — would
    report a different age.  ``signal_observed`` is the per-channel OR, so
    it is 1.0 here even though the stalest reading is three hours old, and
    that pairing (observed, yet three hours stale) is exactly the state an
    operator has to be able to see.
    """
    files = _three_channel_files(tmp_path, offsets=_COMBINE_RECORD_OFFSETS)
    config = _config(tmp_path, files)

    frame = build_export_frame(
        _TICKER_ID,
        config_path=config,
        manager=MockManager(_candles()),
        pages=1,
    )

    # ``timestamp`` is the export frame's ISO string; find the named bar
    # rather than trusting an index position.
    want_ts = _stamp(_COMBINE_BAR_HOURS)
    rows = frame[frame["timestamp"] == want_ts]
    assert len(rows) == 1, f"no single row at {want_ts}; got {len(rows)}"
    row = rows.iloc[0]

    oldest = max(_COMBINE_AGES)
    assert row["signal_age_hours"] == pytest.approx(float(oldest)), (
        f"expected the stalest of news/funding/social ages {_COMBINE_AGES} = "
        f"{oldest}, got {row['signal_age_hours']!r} — that is one channel's "
        "age, not the combination of three"
    )
    # Per-channel OR, so still observed despite the stalest being 8h.
    assert row["signal_observed"] == pytest.approx(1.0)

    # And the stalest channel is the one whose value is oldest, not the
    # newest: guards against the max being taken over a different axis.
    assert row["sentiment_score"] == pytest.approx(0.4), (
        "the oldest record is news\'s; its value must be the carried one"
    )


# ---------------------------------------------------------------------------
# 2. merge ORDER: a property of the call, not of the output
# ---------------------------------------------------------------------------
def _read_legs() -> dict[str, ast.FunctionDef]:
    """The two functions in data.py that iterate the channel list.

    ``fetch_ohlc_dataframe`` is the live-fetch leg; the store-backed leg
    merges inside ``read_ohlc_dataframe`` itself.  Two separate argument
    lists, which is why both are named here.
    """
    source = Path(inspect.getsourcefile(read_ohlc_dataframe) or "")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    legs = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not any(
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "_signal_channels"
            for sub in ast.walk(node)
        ):
            continue
        legs[node.name] = node
    return legs


def test_both_read_legs_pass_the_channel_values_in_channel_order() -> None:
    """Both legs hand ``_signal_channels`` news, funding, social, depth — in order.

    Order cannot be proved from the output and is not left to review: the
    channels share **no** column name, and the two they do share
    (``signal_age_hours`` / ``signal_observed``) combine by ``max``/``any``,
    so every ordering produces byte-identical output.  The order is carried
    entirely by the positional arguments of the one ``_signal_channels``
    call in each leg, which is why it needs pinning at the call.

    Two legs, not one: the live-fetch leg and the store-backed leg are
    separate functions with separate argument lists, and a store-backed run
    is the one that would merge in the wrong order without anything else
    noticing.
    """
    legs = _read_legs()
    # `fetch_ohlc_dataframe` is the live-fetch leg; `read_ohlc_dataframe`
    # holds the store-backed leg's own merge loop.
    assert set(legs) == {
        "fetch_ohlc_dataframe",
        "read_ohlc_dataframe",
    }, f"the two read legs moved or were renamed: {sorted(legs)}"

    expected = [key for key, _ in _SIGNAL_CHANNELS]
    assert expected == [
        "extra_features_file",
        "funding_features_file",
        "social_features_file",
        "orderbook_features_file",
    ], (
        "_SIGNAL_CHANNELS changed shape; this test and the two AST guards "
        "in test_gc_channel_activation.py / test_rl_signal_config_wiring.py "
        "must move together"
    )

    for name, node in sorted(legs.items()):
        calls = [
            sub
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "_signal_channels"
        ]
        assert len(calls) == 1, f"{name}: expected one _signal_channels call"
        got = [arg.id for arg in calls[0].args if isinstance(arg, ast.Name)]
        assert got == expected, (
            f"{name}: merges in order {got}, but _SIGNAL_CHANNELS declares "
            f"{expected}. The columns are disjoint, so nothing downstream can "
            "detect the swap."
        )


# ---------------------------------------------------------------------------
# 3. the legs that only exist behind a trained model
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def three_channel_model(tmp_path_factory):
    """A tiny real PPO model trained with all three channels live.

    Hermetic throughout: a mock Kraken source, synthetic candles, and every
    artifact under pytest ``tmp_path``.  The point is that ``backtest`` and
    ``paper_trade`` can only be reached through a model on disk, and
    asserting on two lines of plumbing should not cost a real training run.
    """
    root = tmp_path_factory.mktemp("rl-three-channel")
    files = _three_channel_files(root)
    config = _config(root, files)
    record = train_ticker(
        _PAIR,
        _MODEL,
        manager=MockManager(_candles(n=120)),
        pages=2,
        total_timesteps=150,
        seed=7,
        models_root=root,
        config_path=config,
    )
    assert record.is_trained()
    return root, record


def test_train_wrote_a_config_carrying_all_three_channels(three_channel_model) -> None:
    """The trained artifact's own config keeps all three keys (and the venue).

    ``backtest`` and ``paper_trade`` read the three keys off ``record.config``
    — the model's OWN config, not the shipped default — so if ``train`` had
    dropped a key on the way to disk the other two legs would have nothing to
    forward.  That hop is the one the AST guards cannot see.
    """
    root, _ = three_channel_model
    artifact_cfg = yaml.safe_load(
        (root / _TICKER_ID / _MODEL / "config.yaml").read_text(encoding="utf-8")
    )
    for key, cols in _CHANNEL_COLUMNS.items():
        assert artifact_cfg.get(key), f"train dropped {key} from the artifact config"
        assert Path(str(artifact_cfg[key])).exists(), f"{key} points nowhere"
    assert artifact_cfg.get("market_data_store_venue") == (
        "binance-spot-archive-seeded"
    ), (
        "train did not carry market_data_store_venue into the artifact "
        "config. paper_trade reads that key off the ARTIFACT config "
        "(self.config), not off the config train was handed, so a dropped "
        "key silently puts the venue read back on data.DEFAULT_STORE_VENUE "
        "-- the exact defect G-B(i) fixed on the call, reopened one hop "
        "earlier."
    )


def test_backtest_reads_all_three_channels_and_runs(three_channel_model, monkeypatch) -> None:
    """``backtest``'s own read returns a frame carrying all three channels.

    The seam is *wrapped*, not replaced, so the leg still runs its real
    stage decomposition, its width guard and its metrics on the frame that
    actually came back.  Recording the call's keywords AND the returned
    frame's columns covers both halves: the keyword says what the leg asked
    for, the columns say what it got.
    """
    root, _ = three_channel_model
    seen: dict[str, Any] = {}

    import kraken_trading_bot.rl.backtest as bt

    real = bt.read_ohlc_dataframe

    def spy(*args: Any, **kwargs: Any):
        frame = real(*args, **kwargs)
        seen["kwargs"] = dict(kwargs)
        seen["columns"] = list(frame.columns)
        return frame

    monkeypatch.setattr(bt, "read_ohlc_dataframe", spy)

    result = backtest_model(
        _TICKER_ID,
        _MODEL,
        models_root=root,
        manager=MockManager(_candles(n=120)),
        pages=2,
        seed=7,
    )

    assert seen, "backtest never called the read seam"
    for key in _CHANNEL_COLUMNS:
        assert seen["kwargs"].get(key), f"backtest forwarded no {key}"
    # G-B(i): the venue key travels with the rest.
    assert "market_data_store_venue" in seen["kwargs"], (
        "backtest stopped forwarding market_data_store_venue — the "
        "AST guard in test_rl_signal_config_wiring.py should have caught this"
    )

    # A RAW merged OHLCV frame: no feature stage has run, so `spread` is
    # legitimately absent here (the microstructure builder writes it) and
    # asking for it would be asking for the wrong stage.
    missing = [col for col in _MERGE_FRAME_COLUMNS if col not in seen["columns"]]
    assert not missing, f"backtest's frame lacked {missing}"
    assert not [c for c in _DERIVED_FROM_FUNDING if c in seen["columns"]], (
        "`spread` appeared on the raw frame; it is a feature, not a merge "
        "column, so something upstream is computing features twice"
    )
    assert result.n_steps > 0, "backtest replayed no bars"


def test_paper_trade_reads_all_three_channels(three_channel_model) -> None:
    """``paper_trade``'s own read returns a frame carrying all three channels.

    The leg that places orders reads its config off ``self.config``, which
    is a different object from the local ``cfg`` the other three legs use —
    so it is the one where a key can be present in the file and absent from
    the call.  ``_fetch_window`` returns the frame directly, so this asserts
    on the thing itself rather than on a call record.
    """
    root, _ = three_channel_model
    trader = PaperTrader(
        _PAIR,
        _MODEL,
        manager=MockManager(_candles()),
        models_root=root,
    )
    frame = trader._fetch_data()
    _assert_all_three_channels_landed(frame, "paper_trade", features=False)


# ---------------------------------------------------------------------------
# 4. the fresh-clone default, proved rather than asserted in a comment
# ---------------------------------------------------------------------------
def _shipped_default() -> dict[str, Any]:
    return yaml.safe_load(
        (_REPO / "configs" / "default.yaml").read_text(encoding="utf-8")
    )


def test_the_shipped_default_refuses_on_a_fresh_clone_and_names_the_fix(
    tmp_path, monkeypatch
) -> None:
    """A clone with no ``signals/`` raises, and the message is the whole fix.

    ``signals/`` is gitignored (``.gitignore``), so this is the DEFAULT state
    of every fresh clone.  ``configs/default.yaml`` documents the
    consequence in a FRESH CLONE block; this proves the documented behaviour
    is the actual behaviour, through a real consumer, on the real shipped
    config rather than on a hand-written fixture.

    ``HOME`` is redirected at a directory that does not exist, so the
    ``~``-spelled paths resolve to nothing no matter what the host happens
    to have — that is what makes this a fresh-clone simulation rather than a
    statement about this machine.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "no-such-home"))
    assert not (tmp_path / "no-such-home").exists()

    shipped = _shipped_default()
    for key in _CHANNEL_COLUMNS:
        assert shipped.get(key), f"{key} is null in the shipped default"
        assert str(shipped[key]).startswith("~/"), f"{key} is not ~-spelled"

    config = tmp_path / "shipped.yaml"
    config.write_text(yaml.safe_dump(shipped), encoding="utf-8")

    with pytest.raises(SignalFileNotFoundError) as excinfo:
        build_export_frame(
            _TICKER_ID, config_path=config, manager=MockManager(_candles()), pages=1
        )
    err = excinfo.value

    # The three things that make the refusal a fix rather than a complaint.
    assert err.config_key in _CHANNEL_COLUMNS, err.config_key
    assert err.reason == "missing", err.reason
    assert err.producer, "a refusal that names no producer is not actionable"
    message = str(err)
    assert err.config_key in message
    assert err.producer in message
    assert "set extra_features_file to null" in message, (
        "the refusal must name the escape hatch, or a reader who has no "
        "sibling repo cloned cannot proceed"
    )
    # The path is reported twice on purpose: as written, and expanded.
    assert str(shipped[err.config_key]) in message, "the raw value is missing"
    assert err.path.startswith(str(tmp_path)), err.path


def test_nulling_the_three_keys_is_the_documented_way_out(tmp_path, monkeypatch) -> None:
    """All three null: a fresh clone runs cleanly on price alone.

    This is the other half of the fresh-clone claim, and the half that makes
    the refusal above acceptable.  ``null`` means OFF: no merge, no
    freshness columns, no complaint.  If this stopped being true the shipped
    non-null default would be a hard block with no way past it short of
    cloning three sibling repos.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "no-such-home"))
    shipped = _shipped_default()
    for key in _CHANNEL_COLUMNS:
        shipped[key] = None
    config = tmp_path / "all-null.yaml"
    config.write_text(yaml.safe_dump(shipped), encoding="utf-8")

    frame = build_export_frame(
        _TICKER_ID, config_path=config, manager=MockManager(_candles()), pages=1
    )

    assert len(frame) > 0
    for key, cols in _CHANNEL_COLUMNS.items():
        for col in cols:
            assert col not in frame.columns, (
                f"{col} is present with {key} null — the null key did not "
                "switch the channel off"
            )
    for col in _FRESHNESS:
        assert col not in frame.columns, (
            f"{col} is present with all three keys null; it is written by "
            "the merge seam, so its presence means a merge happened"
        )


def test_one_live_channel_among_nulls_raises_naming_that_channel(
    tmp_path, monkeypatch
) -> None:
    """Nulling the OTHER two does not silence the one that is live.

    The obvious wrong "fix" for the fresh-clone state is to null two keys
    and leave the third, which reads as a partial opt-out.  Two-state by
    design means the surviving key still refuses — and the message names
    which one, so the reader knows which of the three to populate.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "no-such-home"))
    shipped = _shipped_default()

    for live in _CHANNEL_COLUMNS:
        cfg = dict(shipped)
        cfg[live] = shipped[live]
        for key in _CHANNEL_COLUMNS:
            if key != live:
                cfg[key] = None
        path = tmp_path / f"live-{live}.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

        with pytest.raises(SignalFileNotFoundError) as excinfo:
            build_export_frame(
                _TICKER_ID,
                config_path=path,
                manager=MockManager(_candles()),
                pages=1,
            )
        assert excinfo.value.config_key == live, (
            f"with only {live} live, the refusal named "
            f"{excinfo.value.config_key!r} instead"
        )


def test_the_store_key_and_the_signal_keys_ship_differently_on_purpose() -> None:
    """``market_data_store: null`` beside three non-null signal keys is the rule.

    ``configs/default.yaml`` argues each of these in its own block, three
    sections apart, and on its face they contradict:

    * the store block: a non-null value whose root does not exist "would
      turn every first run on a fresh clone into an error, for a store that
      clone does not have";
    * the signal block: all three keys are non-null, and a fresh clone has
      no ``signals/`` either — so every leg raises.

    They ship differently because the two keys are not the same kind of
    thing, and the discriminator is **who can produce the file**:

    * ``signals/*.jsonl`` is produced by recipes THIS repo ships —
      ``just news-pull``, ``just funding-backfill && just funding-pull``,
      ``just social-pull`` — plus three linkable timers, and the funding
      recipe is keyless.  A fresh clone can populate them.
    * ``~/Projects/kraken-market-data/store`` cannot be conjured by anything
      in this repo.  It needs a sibling clone plus a multi-minute seed, and
      the block immediately below documents the result: 158 missing bars
      including a 39-hour hole at the seed seam, across which ``return_1``
      reports a 39-hour return "as though it were 1-hour".

    The failure modes are therefore not symmetric either.  A mis-set signal
    key raises loudly, naming the key and a copy-pasteable producer, before
    any artifact exists.  A mis-set store key computes wrong numbers
    silently.  That is the whole argument for which of the two defaults to
    the safe side, and this test exists so the two blocks cannot drift into
    an unexamined contradiction.

    It pins the SHIPPED VALUES, so a future edit that flips either one has
    to come here and say why.
    """
    shipped = _shipped_default()

    assert shipped["market_data_store"] is None, (
        "the store key went non-null in the shipped default; the block's "
        "own WHY THIS STAYS null paragraph no longer describes the file"
    )
    for key in _CHANNEL_COLUMNS:
        assert shipped.get(key), (
            f"{key} went null in the shipped default. That is defensible on "
            "its own terms — it makes the shipped width 52 rather than 60 "
            "(features.py check_feature_width docstring) and inerts the "
            "widening this pass bought — but it must be a decision with the "
            "width claim and the README updated in the same commit, not a "
            "quiet edit."
        )

    text = (_REPO / "configs" / "default.yaml").read_text(encoding="utf-8")
    # The discriminator must be stated in the file, not only in a test: a
    # reader who lands on the store block and sees three non-null keys
    # above it is exactly the reader this guards.
    for needle in ("signals/", "fresh clone"):
        assert needle in text, f"the shipped config no longer mentions {needle!r}"
    assert "market_data_store" in text