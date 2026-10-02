"""Tests for the deep-history store: seeding guard, config keys, window.

Three things are pinned here, each of which was a way this change could
have quietly shipped a wrong number:

1. **The finding the pass exists to close.** With ``since``/``until``
   null -- the shipped ``configs/default.yaml`` -- ``training_frame`` and
   ``evaluation_frame`` return the *same object*, so every default
   train/backtest pair is in-sample; and a pinned multi-year window on a
   ~721-bar frame clips to nothing and must raise
   ``NotEnoughDataError`` rather than quietly shortening. Both are
   identity/exception assertions, not value comparisons, so "fixing"
   ``data_window`` into always-splitting behaviour breaks them.
2. **The refusal.** A configured ``market_data_store`` that cannot serve
   bars must fail with instructions, not with a bare ``ValueError``.
3. **The seed guard.** ``tools/store_guard.py`` is what stops a
   ``fallback-csv`` seed from being mistaken for a seeded store.

Everything here is offline and hermetic: no network, no ``just``, no
training, no writes outside ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from kraken_trading_bot.rl import (  # noqa: E402
    DEFAULT_STORE_VENUE,
    FeaturePipeline,
    MarketDataStoreUnavailableError,
    NotEnoughDataError,
    clip_to_window,
    evaluation_frame,
    prepare_episode,
    read_ohlc_dataframe,
    resolve_data_window,
    training_frame,
)
from kraken_trading_bot.rl.data import _resolve_store  # noqa: E402
from tools.store_guard import (  # noqa: E402
    REQUIRED_STORE_MODE,
    StoreModeError,
    require_market_data_store,
)

DEFAULT_CONFIG = REPO_ROOT / "configs" / "default.yaml"
EXAMPLE_CONFIG = REPO_ROOT / "configs" / "deep-history.example.yaml"
JUSTFILE = REPO_ROOT / "justfile"

#: Kraken's REST ceiling, measured repeatedly: a paged live fetch returns
#: 721 bars and older data is not retrievable at all.
LIVE_CEILING = 721


class _RecordingStore:
    """Stand-in for ``market_data.store.MarketDataStore``.

    Installed as ``sys.modules["market_data.store"]`` by the tests that
    need the *path* branch of ``_resolve_store`` to reach the store rather
    than the sibling-import branch, so those tests never import the real
    sibling package (which would be a plain, un-undoable import).
    """

    def __init__(self, root):
        self.root = Path(root)

    def upsert(self, *args, **kwargs):
        return None

    def read(self, *args, **kwargs):
        return pd.DataFrame()


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _hourly(n: int, start: str = "2026-08-01T00:00:00Z") -> pd.DataFrame:
    """An ``n``-bar hourly OHLCV frame, the shape the read seam returns."""
    idx = pd.date_range(start, periods=n, freq="h", tz="UTC")
    close = 2000.0 + np.arange(n, dtype=float)
    return pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "vwap": close,
            "volume": np.full(n, 100.0),
            "count": np.full(n, 50.0),
        },
        index=idx,
    )


# ── 1. the finding: an unpinned default is in-sample, a too-old pin is an error


def test_shipped_default_window_is_unpinned_and_in_sample_by_identity():
    """The shipped config cannot produce an out-of-sample measurement.

    Read from ``configs/default.yaml`` itself rather than from a literal,
    so pointing the default at a pinned window fails here.  ``is`` rather
    than ``==``: an unpinned run must not even build a split, or
    "backtest == training bars" stops being a property of the code path
    and becomes a coincidence of values.
    """
    config = _load(DEFAULT_CONFIG)
    window = resolve_data_window(config)
    assert window.since is None and window.until is None
    assert window.is_pinned is False
    assert window.has_split is False

    frame = _hourly(LIVE_CEILING)
    training = training_frame(frame, window)
    evaluation = evaluation_frame(frame, window)
    assert training is evaluation
    assert training is frame
    # The consequence, stated as an assertion: 721 bars in, 721 bars
    # replayed, zero held out.
    assert len(evaluation) == len(frame) == LIVE_CEILING
    assert set(evaluation.index) & set(training.index) == set(frame.index)


def test_a_pinned_window_actually_splits_and_the_halves_are_disjoint():
    """The positive control for the identity test above.

    Without this, "training is evaluation" would also pass if the split
    had been deleted outright -- an identity assertion on its own cannot
    tell 'never splits' from 'always returns the input'.
    """
    frame = _hourly(1000, start="2020-01-01T00:00:00Z")
    window = resolve_data_window(
        {
            "data_window": {
                "since": "2020-01-01T00:00:00Z",
                "until": "2020-03-01T00:00:00Z",
                "eval_split": 0.7,
            }
        }
    )
    training = training_frame(frame, window)
    evaluation = evaluation_frame(frame, window)
    assert window.is_pinned and window.has_split
    assert training is not evaluation
    assert len(training) == 700 and len(evaluation) == 300
    assert not set(training.index) & set(evaluation.index)
    assert evaluation.index[0] > training.index[-1]


def test_pinned_multi_year_window_on_a_short_frame_raises_not_enough_data():
    """Pinning past the data is an ERROR, not a silently shorter window.

    The exact shape of the CAND-3a finding: a 2020->2026 pin is the
    documented way to get an out-of-sample backtest, and against the
    live ~721-bar frame it clips to zero bars.  ``prepare_episode`` must
    raise rather than hand the environment an empty episode.
    """
    frame = _hourly(LIVE_CEILING)  # 2026-08-01 .. 2026-09-01
    window = resolve_data_window(
        {
            "data_window": {
                "since": "2020-01-01T00:00:00Z",
                "until": "2026-01-01T00:00:00Z",
                "eval_split": 0.7,
            }
        }
    )
    clipped = clip_to_window(frame, window)
    assert len(clipped) == 0  # the pin, not the frame, emptied it

    pipeline = FeaturePipeline(windows=[1, 4, 24])
    with pytest.raises(NotEnoughDataError):
        prepare_episode(clipped, pipeline, ticker_id="ETH/USD")
    # Same error class as a too-short frame, so callers that already
    # handle it keep working.
    with pytest.raises(ValueError):
        prepare_episode(clipped, pipeline, ticker_id="ETH/USD")


def test_unpinned_frame_of_the_same_length_still_trains():
    """Non-vacuity control for the error test.

    The same 721 bars train fine unpinned, so the raise above is caused by
    the PIN and not by the bar count.  (The 24-bar warm-up is the
    environment's ``first_tradable_index``, not a slice out of
    ``prepare_episode``: the episode keeps every bar it is given.)
    """
    frame = _hourly(LIVE_CEILING)
    pipeline = FeaturePipeline(windows=[1, 4, 24])
    episode = prepare_episode(
        clip_to_window(frame, resolve_data_window({"data_window": {}})),
        pipeline,
        ticker_id="ETH/USD",
    )
    assert len(episode) == LIVE_CEILING


# ── 2. the refusal: a configured store that cannot serve must say what to do


def _seed_root(tmp_path: Path, name: str = "store") -> Path:
    root = tmp_path / name
    (root / "ETH_USD" / "60").mkdir(parents=True)
    (root / "ETH_USD" / "60" / "2026-08.parquet").write_bytes(b"")
    return root


def test_missing_store_root_refuses_with_the_seed_recipe_and_its_cost(tmp_path):
    """The bare ``ValueError`` is gone; the error teaches the fix."""
    root = tmp_path / "not-seeded"
    with pytest.raises(MarketDataStoreUnavailableError) as excinfo:
        _resolve_store(str(root))

    message = str(excinfo.value)
    # What to run, what it costs, and the store root it applies to.
    assert "just store-plan" in message
    assert "just store-seed" in message
    assert "~158s" in message and "~13MB" in message
    assert str(root) in message
    # And the way out for a host that has no store at all.
    assert "market_data_store: null" in message
    # The typing contract is unchanged: still a ValueError, so every
    # existing caller and test that catches one keeps working.
    assert isinstance(excinfo.value, ValueError)


def test_empty_store_root_is_refused_the_same_way(tmp_path):
    """A directory that exists but holds no months is just as unusable."""
    root = tmp_path / "empty-store"
    root.mkdir()
    with pytest.raises(MarketDataStoreUnavailableError, match="empty"):
        _resolve_store(str(root))


def test_csv_only_store_root_names_the_fallback_csv_failure(tmp_path):
    """A fallback-csv seed leaves .csv the reader cannot see -- say so.

    This is the shape a seed run in the wrong dev shell produces: exit 0,
    a directory that looks populated, and a bot that then reads ~721 live
    bars.  Naming it is the difference between a legible error and a
    mystery.
    """
    root = tmp_path / "csv-store"
    (root / "ETH_USD" / "60").mkdir(parents=True)
    (root / "ETH_USD" / "60" / "2026-08.csv").write_text("time,close\n", encoding="utf-8")

    with pytest.raises(MarketDataStoreUnavailableError) as excinfo:
        _resolve_store(str(root))
    message = str(excinfo.value)
    assert ".csv" in message and "fallback-csv" in message
    assert "store_seed" in message or "just store-seed" in message


def test_a_seeded_store_root_resolves(tmp_path, monkeypatch):
    """The positive control: parquet months present -> resolves, no raise."""
    import types

    seen: list[Path] = []

    class FakeStore:
        def __init__(self, root):
            seen.append(Path(root))

        def upsert(self, *args, **kwargs):
            return None

        def read(self, *args, **kwargs):
            return pd.DataFrame()

    module = types.ModuleType("market_data.store")
    module.MarketDataStore = FakeStore
    package = types.ModuleType("market_data")
    package.store = module
    monkeypatch.setitem(sys.modules, "market_data", package)
    monkeypatch.setitem(sys.modules, "market_data.store", module)

    root = _seed_root(tmp_path)
    assert _resolve_store(str(root)) is not None
    assert seen == [root]


def test_a_store_like_object_is_still_accepted_without_a_path(monkeypatch):
    """Duck-typed stores (tests, embedded callers) bypass the path check."""

    class Injected:
        def upsert(self, *args, **kwargs):
            return None

        def read(self, *args, **kwargs):
            return pd.DataFrame()

    assert isinstance(_resolve_store(Injected()), Injected)


def test_unreadable_store_error_reaches_the_read_seam(tmp_path, monkeypatch):
    """The refusal fires through ``read_ohlc_dataframe``, not only directly.

    ``train``/``backtest``/``export`` all reach the store through this
    function, so a check that only ``_resolve_store``'s own callers see
    would not protect them.
    """
    # A stand-in sibling module, so reaching the store seam here never
    # imports the REAL ``market_data.store``.  It would succeed in this
    # repo's dev shell and, being a plain import rather than a
    # monkeypatch, could not be undone -- leaving ``market_data.store``
    # cached in ``sys.modules`` for the rest of the session and silently
    # disarming any later test that hides the sibling to exercise the
    # ImportError branch.  The root-status refusal is checked *before* the
    # store is constructed, so a stand-in exercises exactly the same
    # assertion without the leak.
    module = types.ModuleType("market_data.store")
    module.MarketDataStore = _RecordingStore
    package = types.ModuleType("market_data")
    package.store = module
    monkeypatch.setitem(sys.modules, "market_data", package)
    monkeypatch.setitem(sys.modules, "market_data.store", module)

    class Source:
        def ohlc(self, pair, interval=60, since=None):
            return [], 0

    with pytest.raises(MarketDataStoreUnavailableError, match="just store-seed"):
        read_ohlc_dataframe(
            "ETH/USD",
            60,
            pages=1,
            manager=Source(),
            market_data_store=str(tmp_path / "absent"),
        )


# ── 3. the seed guard: store_mode is asserted, not printed


def test_guard_accepts_a_market_data_report():
    report = {
        "ticker": "ETH/USD",
        "pair_id": "ETH_USD",
        "interval_min": 60,
        "months_requested": 100,
        "months_downloaded": 100,
        "bars_added": 76561,
        "store_mode": REQUIRED_STORE_MODE,
        "skipped": [],
    }
    assert require_market_data_store(report) == "market-data"


def test_guard_refuses_a_fallback_csv_report():
    """The mode that writes files the reader cannot see."""
    with pytest.raises(StoreModeError) as excinfo:
        require_market_data_store(
            {"store_mode": "fallback-csv", "bars_added": 76561}
        )
    message = str(excinfo.value)
    assert "market-data" in message
    assert ".parquet" in message
    assert "just store-seed" in message  # the fix, not just the complaint
    assert "721" in message  # what the silent outcome would have been


def test_guard_refuses_a_report_with_no_store_mode_field():
    with pytest.raises(StoreModeError, match="no store_mode"):
        require_market_data_store({"bars_added": 10})


def test_guard_cli_exit_codes(tmp_path):
    """0 for a readable store, 1 for a refusal, 2 for an unreadable report."""
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"store_mode": "market-data", "bars_added": 5}))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"store_mode": "fallback-csv"}))
    broken = tmp_path / "broken.json"
    broken.write_text("not json at all")

    def run(path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "store_guard.py"), str(path)],
            capture_output=True,
            text=True,
        )

    assert run(good).returncode == 0
    assert "market-data" in run(good).stdout
    refused = run(bad)
    assert refused.returncode == 1
    assert "REFUSING" in refused.stderr
    assert run(broken).returncode == 2


def test_guard_reads_stdin_for_a_piped_report(tmp_path):
    """The recipe pipes stdout, so '-' has to work."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "store_guard.py"), "-"],
        input=json.dumps({"store_mode": "market-data", "bars_added": 7}),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "market-data" in result.stdout


# ── 4. the recipes and the config keys


def test_the_justfile_seeds_under_this_repos_shell_and_guards_the_mode():
    """R2: the recipes must not seed where the store package is missing."""
    text = JUSTFILE.read_text(encoding="utf-8")
    for recipe in ("store-plan", "store-seed", "store-stats", "store-verify"):
        assert f"\n{recipe} " in text, f"{recipe} recipe missing"
    # Every seeder invocation names the sibling checkout on PYTHONPATH and
    # this repo's dev shell; the seeding one is the load-bearing one.
    seed_body = text.split("\nstore-seed ", 1)[1].split("\n# ", 1)[0]
    assert "{{dev}}" in seed_body  # nix develop --command bash -c
    assert "PYTHONPATH=" in seed_body
    assert "kraken_deep_history.cli seed" in seed_body
    assert "tools/store_guard.py" in seed_body
    # And the refusal is explicit in the help text, not merely in the guard.
    assert 'REFUSES store_mode != "market-data"' in text or (
        "REFUSES" in text and "market-data" in text
    )


def test_no_106x_claim_anywhere_in_the_docs_or_recipes():
    """The '106x more training' framing is false and must not reappear.

    10,000 PPO steps over a deep store is ~13% of the store with resets:
    the bars buy normalization-sample size and regime diversity, not
    gradient steps.
    """
    for path in (JUSTFILE, DEFAULT_CONFIG, EXAMPLE_CONFIG, REPO_ROOT / "README.md"):
        text = path.read_text(encoding="utf-8")
        assert "106" not in text, f"{path.name} mentions 106"


def test_default_config_keeps_the_store_null_and_declares_the_venue():
    """A fresh clone must not error on its first run (DECISION DEVIATION 3)."""
    config = _load(DEFAULT_CONFIG)
    assert "market_data_store" in config
    assert config["market_data_store"] is None
    assert config["market_data_store_venue"] == DEFAULT_STORE_VENUE
    # The default window is deliberately open: a pinned default would make
    # every unpinned run out-of-sample on ~721 live bars, which is a
    # different (and unfalsifiable) claim. It is pinned in the example.
    assert config["data_window"]["since"] is None
    assert config["data_window"]["until"] is None
    assert config["data_window"]["eval_split"] == pytest.approx(0.7)


def test_example_config_pins_a_window_and_labels_the_seeded_venue():
    config = _load(EXAMPLE_CONFIG)
    # Store ON here (the opt-in), and it says so in its header.
    assert config["market_data_store"]
    assert config["market_data_store_venue"] != DEFAULT_STORE_VENUE
    window = config["data_window"]
    assert window["since"] and window["until"]
    assert window["until"] > window["since"]
    assert 0.0 < window["eval_split"] <= 1.0
    assert resolve_data_window(config).has_split is True

    header = EXAMPLE_CONFIG.read_text(encoding="utf-8").split("ticker:", 1)[0]
    # WHY the default stays null, and how to seed before using this file.
    assert "ValueError" in header or "MarketDataStoreUnavailableError" in header
    assert "just store-seed" in header


def test_example_config_pins_a_window_the_seeded_store_can_actually_serve():
    """The example's dates have to be inside the seeded span.

    The store covers 2018-01-01 onward for the four mapped USDT pairs;
    a `since` before that clips to empty and raises NotEnoughDataError,
    which would make the copy-paste example fail on its first run.
    """
    config = _load(EXAMPLE_CONFIG)
    window = resolve_data_window(config)
    # The seeder's earliest mapped month, to the month before the pin.
    assert window.since >= pd.Timestamp("2018-01-01", tz="UTC")
    assert window.until <= pd.Timestamp("2026-10-01", tz="UTC")