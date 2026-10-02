"""Data loading for the RL training pipeline.

Turns Kraken OHLCV market data into the pandas DataFrames consumed by
the :class:`~kraken_trading_bot.rl.environment.TradingEnvironment`.

Pagination is implemented client-side here: :meth:`KrakenManager.ohlc`
already returns a ``(candles, last)`` cursor pair, so we loop on the
``since`` cursor until we have ``pages`` pages or the exchange says the
history is exhausted (``last == 0``).  This intentionally does not add
pagination to kraken-python itself; it lives in this package.

Exogenous signal merging (:func:`merge_extra_features`) left-joins
per-(ticker, hour) signal vectors produced by the sibling
``ticker-news-signals`` project onto the OHLCV frame by floored
bar timestamp.  That single seam is the only door every exogenous
column travels through, so it also carries the three properties the
join needs to be trustworthy: the records are filtered to the
requested ticker, each floored hour keeps exactly one record (last
write wins), and a reading is only carried forward for a *bounded*
number of hours — with ``signal_age_hours`` / ``signal_observed``
recorded next to it so "no record within the window" is a value the
agent can see rather than a silent ``0.0``.

Local market-data store integration (:func:`read_ohlc_dataframe`): when the
``market_data_store`` config key points at the sibling ``kraken-market-data``
store root, loading becomes fetch -> ``upsert`` -> ``read`` — every window is
also appended to the store, and later reads can return deeper history than
Kraken's ~720-bar REST ceiling.  The store is imported lazily and only when
configured, so ``kraken-market-data`` stays an optional, duck-typed accent of
this package.  See ``kraken-market-data/INTEGRATION.md`` for the full
consumer contract.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

import numpy as np
import pandas as pd

from .features import (
    FeaturePipeline,
    _SIGNAL_COLUMNS,
    _SIGNAL_FRESHNESS_COLUMNS,
    normalize_ticker_id,
)

if TYPE_CHECKING:
    from kraken_api import KrakenManager

_LOGGER = logging.getLogger(__name__)

_OHLCV_COLUMNS = ("time", "open", "high", "low", "close", "vwap", "volume", "count")

# Rolling window, in bars, of the trade-count activity z-score derived at
# the read seam from the ``count`` column every OHLCV frame already carries.
_TRADE_COUNT_ZSCORE_WINDOW = 20

# Columns expected in signal JSONL records from sibling projects; the
# canonical definition lives in ``features._SIGNAL_COLUMNS`` (imported
# here so the merge seam and the observation allow-list share one source).
# News signals (ticker-news-signals): sentiment_score, article_count, novelty_flag
# Funding signals (kraken-funding-rates): funding_rate, basis, open_interest
# Social signals (kraken-social-signals): stt_mention_count, stt_tilt, fng_index

# Bars a 24-window feature pipeline needs before any indicator fills its
# look-back window (max window + a small return/rolling cushion).
_WARMUP_PAD = 6

# Freshness/provenance columns written by :func:`merge_extra_features`
# beside the signal values themselves.  They are part of the canonical
# allow-list in ``features._SIGNAL_COLUMNS`` (imported above), so they ride
# the existing ``signals`` feature group into the observation with no new
# plumbing: ``signal_observed`` says "a record for this ticker landed
# inside the freshness window" and ``signal_age_hours`` says how old the
# stalest live reading is.  The names come from the features module so
# the seam and the observation cannot drift.
_SIGNAL_AGE_COLUMN, _SIGNAL_OBSERVED_COLUMN = _SIGNAL_FRESHNESS_COLUMNS

# Default staleness bound in hours, used when the ``signal_max_age_hours``
# config key is null.  One hour of signal time — i.e. exactly one hourly
# bar, the seam's own resolution — is enough to bridge a single missed
# pull without letting a stale reading impersonate a current one for days.
# Set the key to widen it (funding settles ~8-hourly, so an 8-hourly
# source wants a bound above 1) or to 0 for a strict per-hour match.
_DEFAULT_SIGNAL_MAX_AGE_HOURS = 1.0

# ``signal_age_hours`` value for a bar with no live reading.  Ages are
# always >= 0, so -1.0 is an unambiguous "nothing within the window"
# marker, and it stays a finite float on purpose: a NaN here would flow
# into ``NormalizationStats.normalize`` and poison the whole z-scored
# observation vector.  Both columns are combined across the three merged
# sources with ``max`` (stalest live reading wins; the sentinel loses to
# any real age), which is why it must sort below 0.
_NO_SIGNAL_AGE = -1.0

# Kraken quotes Bitcoin as XBT while configs, model ids and the other
# signal projects use BTC, so both spellings must fold to the same key or
# a genuine BTC file would be rejected against a BTC/USD model.
_TICKER_ALIASES = {"XBT": "BTC"}

# Ticker field name carried by every signal record shape
# (``ticker-news-signals``, ``kraken-funding-rates``, ``kraken-social-
# signals``).  Records without it are treated as an explicitly one-ticker
# file (WARNING, still merged) for backwards compatibility.
_TICKER_FIELD = "ticker"

# The three exogenous channels in merge order, each paired with the command
# that produces its file.  Both the fetch leg and the store leg iterate this
# one list, so the two can never disagree about which key feeds which merge
# or about the wording of a refusal — a disagreement that would show up as
# one branch reading a file the other silently skipped.
_SIGNAL_CHANNELS: tuple[tuple[str, str], ...] = (
    (
        "extra_features_file",
        "python ~/Projects/ticker-news-signals/cli.py pull --ticker <PAIR> "
        "--output <path>",
    ),
    ("funding_features_file", "just funding-pull"),
    (
        "social_features_file",
        "python ~/Projects/kraken-social-signals/cli.py pull --ticker <PAIR> "
        "--output <path>",
    ),
)


def _resolve_config_path(value: Any) -> Path | None:
    """Expand a config-supplied path; ``None`` means "not configured".

    ``yaml.safe_load`` preserves the ``~`` of a value like
    ``~/Projects/kraken-trading-bot/signals/eth_usd_funding.jsonl``, and
    ``Path("~/x")`` looks for a directory *literally named* ``~`` under the
    current working directory — so the shipped spelling resolved to nothing
    from every CWD, and the consumer could not tell "not configured" from
    "configured and broken".  ``backtest.py`` already got this right for
    ``--config``; this helper is the single definition so the next instance
    is a review question rather than an audit finding.

    Args:
        value: The raw config value: a path string, an ``os.PathLike``, or
            a null-ish value meaning "off".

    Returns:
        The expanded ``Path``, or ``None`` for a null/empty value (an
        ``expanduser()`` no-op on any value without a ``~`` prefix).
    """
    if not value:
        return None
    return Path(value).expanduser()


def _signal_channels(*values: Any) -> list[tuple[str, Any, str]]:
    """Pair each exogenous channel's config value with its key and producer.

    Args:
        *values: The three channel values **in merge order** — news,
            funding, social — matching :data:`_SIGNAL_CHANNELS`.

    Returns:
        ``(config_key, value, producer_command)`` triples in merge order.
        The value is passed through untouched, so a null channel stays
        null here and is skipped downstream.
    """
    return [
        (key, value, producer)
        for (key, producer), value in zip(_SIGNAL_CHANNELS, values)
    ]


def _channel_producer(config_key: str | None) -> str | None:
    """The command that produces ``config_key``'s file, for a refusal.

    Naming the producer turns "your path is wrong" into a copy-pasteable
    fix, which is the whole point of failing loudly instead of skipping.

    Args:
        config_key: A key from :data:`_SIGNAL_CHANNELS`, or ``None``.

    Returns:
        The producer command, or ``None`` for an unrecognised/absent key.
    """
    for key, producer in _SIGNAL_CHANNELS:
        if key == config_key:
            return producer
    return None


class NotEnoughDataError(ValueError):
    """Raised when the available OHLCV bars are too few to train/backtest.

    Attributes:
        needed: Minimum number of bars required.
        available: Number of bars actually available.
    """

    def __init__(self, needed: int, available: int, what: str = "data") -> None:
        self.needed = needed
        self.available = available
        super().__init__(
            f"Not enough {what}: need at least {needed} bars "
            f"(max feature window + warmup), got {available}"
        )


class SignalTickerMismatchError(ValueError):
    """Raised when a signal file holds no record for the requested pair.

    The sibling signal projects write a ``ticker`` field on every record,
    so a file whose tickers exclude the pair being loaded is a
    mis-pointed config (``extra_features_file`` still holding the BTC
    file while ETH is being trained), not a data gap.  Merging it anyway
    is the silent-corruption failure this class exists to stop: BTC
    sentiment landing on ETH bars with no error and no log.

    Attributes:
        requested: The pair the caller asked for, e.g. ``"ETH/USD"``.
        found: Sorted canonical tickers the file actually contains.
        path: The signal file that was rejected.
    """

    def __init__(self, requested: str, found: Sequence[str], path: "Path | str") -> None:
        self.requested = requested
        self.found = list(found)
        self.path = str(path)
        super().__init__(
            f"Signal file {self.path} holds no records for {requested} "
            f"(contains: {', '.join(self.found) or 'none'}); point the "
            f"signal file config key at a {requested} file or set "
            f"signal_require_ticker: false to merge it unfiltered."
        )


class SignalFileNotFoundError(ValueError):
    """Raised when a *configured* exogenous-signal file cannot be used.

    The three channel keys (``extra_features_file`` /
    ``funding_features_file`` / ``social_features_file``) are two-state by
    design.  A ``null`` or empty value means **off**: the merge does
    nothing at all and says nothing at all, so a fresh clone with no sibling
    projects cloned still runs cleanly on price alone.  A non-null value is
    a *declared intent to use that channel*, which makes an unusable path a
    configuration error rather than a first-run state.

    Silently skipping it was the defect this class exists to stop.  The
    skipped merge does not merely drop the source's own columns — it drops
    ``signal_observed`` and ``signal_age_hours`` too, because those are
    written by the same seam, so the diagnostic that would have made the
    absence auditable *inside* the observation was itself absent.

    Attributes:
        config_key: The config key that named the file (e.g.
            ``"funding_features_file"``), or ``None`` for a direct seam
            call that passed no key.
        raw_value: The value exactly as the config spells it, so a ``~``
            that was never expanded stays legible in the message.
        path: The expanded path that was checked.
        reason: ``"missing"`` (no file at the path) or ``"empty"`` (the
            file exists but parsed to zero records).
        producer: The command that produces the file, when known.
    """

    def __init__(
        self,
        config_key: str | None,
        raw_value: Any,
        path: "Path | str",
        *,
        reason: str = "missing",
        producer: str | None = None,
    ) -> None:
        self.config_key = config_key
        self.raw_value = raw_value
        self.path = str(path)
        self.reason = reason
        self.producer = producer

        key = config_key or "the signal-file config key"
        # Naming both spellings keeps a recurring tilde defect legible: the
        # raw value is what the user wrote, the path is what was checked.
        spelled = str(raw_value)
        where = (
            f"{self.path} (expanded from {spelled!r})"
            if spelled != self.path
            else self.path
        )
        if reason == "empty":
            head = (
                f"{key} is set to {spelled!r} and {where} holds no records — "
                f"an empty log is a producer failure, not a first-run state"
            )
        else:
            head = (
                f"{key} is set to {spelled!r} but no file exists at {where}"
            )
        fix = f" Produce it with: {producer}." if producer else ""
        super().__init__(
            f"{head}.{fix} Or set {key} to null to run without this "
            f"channel: a null key means off and is silent, a set key is a "
            f"declared intent to use it."
        )


def _canonical_ticker(value: Any) -> str:
    """Fold a pair/ticker string to a separator-free comparison key.

    ``"ETH/USD"``, ``"ETH_USD"``, ``"eth/usd"`` and ``"ETHUSD"`` all fold
    to ``"ETHUSD"`` so a signal file and a model config can be compared
    without depending on one spelling.  Kraken's ``XBT`` is aliased to
    ``BTC``.  Returns ``""`` for an empty/None value.

    Args:
        value: Raw pair or ticker string from a record or a config.

    Returns:
        The canonical uppercase key, or ``""`` when there is nothing to
        fold.
    """
    text = "" if value is None else str(value).strip().upper()
    if not text:
        return ""
    parts = [part for part in re.split(r"[^A-Z0-9]+", text) if part]
    if not parts:
        return ""
    parts[0] = _TICKER_ALIASES.get(parts[0], parts[0])
    return "".join(parts)


def _bar_hours(index: pd.DatetimeIndex) -> float:
    """Median spacing of ``index`` in hours (the environment bar interval).

    Args:
        index: UTC ``DatetimeIndex`` of the OHLCV frame.

    Returns:
        The median gap between consecutive bars in hours, clamped to
        ``[1/60, 24]``; ``1.0`` when the index has fewer than two bars or
        its spacing is unusable.
    """
    if len(index) < 2:
        return 1.0
    gaps = pd.Series(index).diff().dropna().dt.total_seconds()
    step = float(gaps.median()) if len(gaps) else 0.0
    if not step > 0:
        return 1.0
    return min(max(step / 3600.0, 1.0 / 60.0), 24.0)


def _resolve_max_age_hours(max_age_hours: float | int | None) -> float:
    """Resolve the configured staleness bound in hours.

    Args:
        max_age_hours: The ``signal_max_age_hours`` config value.  ``None``
            (the config default) means "derive it": one hour of signal
            time, i.e. exactly one bar at the seam's hourly resolution.

    Returns:
        A non-negative hour count.  A non-positive configured value is
        clamped to ``0.0``, which disables forward-fill entirely (a
        reading then only applies to the bar whose hour it belongs to).
    """
    if max_age_hours is None:
        return _DEFAULT_SIGNAL_MAX_AGE_HOURS
    try:
        hours = float(max_age_hours)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "signal_max_age_hours=%r is not a number — using the derived "
            "default of %.1fh",
            max_age_hours,
            _DEFAULT_SIGNAL_MAX_AGE_HOURS,
        )
        return _DEFAULT_SIGNAL_MAX_AGE_HOURS
    if hours < 0.0:
        _LOGGER.warning(
            "signal_max_age_hours=%r is negative — clamping to 0 (no carry)",
            max_age_hours,
        )
        return 0.0
    return hours


def _signal_ages(
    raw: pd.DataFrame,
    filled: pd.DataFrame,
    columns: Sequence[str],
    bar_index: pd.DatetimeIndex | None = None,
) -> pd.DataFrame:
    """Hours since the record that produced each (carried) value.

    Args:
        raw: The reindexed signal frame *before* the bounded forward-fill,
            where a non-null cell means "this bar has its own record".
        filled: The forward-filled frame; a non-null cell means "this
            bar's value came from a record inside the freshness window".
        columns: The signal columns present in both frames.
        bar_index: The frame's *unfloored* bar timestamps, used to measure
            the age.  ``filled`` is indexed on hour-truncated stamps, so on
            sub-hourly bars (15m, 5m) every bar inside a record's hour
            shares one floored label and would otherwise all report an age
            of 0.  Defaults to ``filled.index`` (identical for hourly
            bars, where flooring is a no-op).

    Returns:
        Float frame aligned to ``filled``; ``0.0`` on a bar that carries
        its own record, growing by the bar interval while a value is
        carried forward, and ``NaN`` where no record is live.
    """
    hours = pd.Timedelta(hours=1)
    index = filled.index if bar_index is None else bar_index
    out = pd.DataFrame(index=filled.index, columns=list(columns), dtype=float)
    for col in columns:
        # Hour of the record a bar is reading, carried forward without
        # limit purely to find it.  Positionally paired with ``raw`` /
        # ``filled``, which are reindexed onto the *floored* index — so
        # this tracks the record's hour (all sub-hourly bars of one hour
        # read the same record, which is correct for an hourly source),
        # while the subtraction below measures the age against the real
        # bar time.  Using the floored stamp for both would report every
        # bar of the record's hour as 0 hours old.
        own = raw[col].notna().to_numpy()
        seen = pd.Series(filled.index, index=filled.index).where(own).ffill()
        age = (index - seen) / hours
        # Mask to the live rows: a bar with no live reading is unobserved,
        # not stale-but-usable, and gets no age.
        out[col] = age.where(filled[col].notna()).to_numpy(dtype=float)
    return out


def merge_extra_features(
    df: pd.DataFrame,
    extra_features_file: str | None = None,
    *,
    ticker: str | None = None,
    max_age_hours: float | int | None = None,
    require_ticker: bool = True,
    config_key: str | None = None,
) -> pd.DataFrame:
    """Merge exogenous per-(ticker, hour) signal vectors onto an OHLCV frame.

    Reads a JSONL file produced by the sibling signal projects
    (``ticker-news-signals``, ``kraken-funding-rates``,
    ``kraken-social-signals``) — each record has a ``timestamp`` (ISO 8601,
    UTC), a ``ticker`` and that source's signal columns, e.g. news writes
    ``sentiment_score`` (float in [-1, 1]), ``article_count`` (int) and
    ``novelty_flag`` (bool).

    This is the single seam every exogenous column travels through, so it
    is also where four properties the join depends on are enforced:

    1. **Ticker filter** — records are filtered to ``ticker`` before the
       join, so one ticker's signals can never annotate another's bars.  A
       file whose tickers do not include ``ticker`` raises
       :class:`SignalTickerMismatchError` (a mis-pointed config key, not a
       data gap).  A file with *no* ``ticker`` field is treated as an
       explicitly one-ticker file: it is merged unchanged at WARNING, which
       keeps pre-ticker-tagged files and the documented one-ticker cron
       working.  ``require_ticker=False`` opts out of the filter entirely.
    2. **De-duplicated hours** — the sibling's documented hourly cron
       re-appends the current hour on every pull, so the floored index
       repeats.  Records are de-duplicated on the raw timestamp and then
       collapsed to one per floored hour (last write wins, i.e. the most
       recent pull), which is what stops the reindex from raising
       ``ValueError: cannot reindex on an axis with duplicate labels``.
    3. **Bounded carry + freshness** — a reading is forward-filled for at
       most ``max_age_hours`` (default: one hour, derived from the bar
       interval), never unboundedly, and ``signal_age_hours`` /
       ``signal_observed`` record how old the reading is and whether there
       was one at all.
    4. **Absence is not neutral** — the value columns keep their historical
       zero-fill (so no ``NaN`` can reach the z-scored observation), but a
       row with no live reading is now marked ``signal_observed=False``
       with ``signal_age_hours=-1.0``; "no record" and "a genuine
       ``0.0``/balanced reading" are therefore distinguishable.
    5. **Off is not the same as broken** — a null/empty
       ``extra_features_file`` means "off" and returns ``df`` untouched and
       *silently*, so a fresh clone with no sibling projects cloned still
       runs.  A non-null value that does not resolve, or that resolves to a
       file holding zero records, raises
       :class:`SignalFileNotFoundError` naming the config key, the value as
       written and the expanded path.  Skipping it silently was the defect:
       the skipped merge drops the freshness pair too, so the diagnostic
       that would have made the absence auditable inside the observation was
       itself absent.  "Records present but none overlapping this ticker"
       stays a WARNING — the expected state of a young forward-only log is
       not a fault.

    Both the OHLCV bar index and the signal timestamps are floor-truncated
    to the hour before joining, so the merge is robust to minor timestamp
    offsets (e.g. 1-minute candles with non-zero minutes).

    Args:
        df: OHLCV DataFrame indexed by UTC ``DatetimeIndex`` (``time``).
        extra_features_file: Path to the signal JSONL.  ``None`` or empty
            returns ``df`` unchanged; a ``~``-spelled value is expanded
            against ``$HOME`` (see :func:`_resolve_config_path`).
        ticker: The pair the frame is for, e.g. ``"ETH/USD"``; used to
            filter records.  ``None`` (a direct call that does not know
            the pair) skips the filter with a WARNING.
        max_age_hours: Staleness bound in hours; ``None`` (the
            ``signal_max_age_hours`` config default) derives one hour of
            carry from the bar interval.  ``0`` disables forward-fill.
        require_ticker: Fail loudly on a ticker mismatch (default).  When
            ``False`` every record is merged and a mismatch only logs a
            WARNING.
        config_key: The config key ``extra_features_file`` came from, used
            only to name the key in a refusal.  The fetch and store legs
            pass it via :func:`_signal_channels`; a direct call may omit it.

    Returns:
        The input DataFrame with the source's signal columns plus
        ``signal_age_hours`` and ``signal_observed`` added (when a file is
        provided and readable).  The freshness columns are combined across
        repeated calls: ``signal_observed`` is true if *any* merged source
        had a live reading and ``signal_age_hours`` is the age of the
        stalest one.

    Raises:
        SignalFileNotFoundError: If a configured path does not resolve, or
            resolves to a file holding zero records.
        SignalTickerMismatchError: If the file is ticker-tagged but holds
            no record for ``ticker`` and ``require_ticker`` is true.
    """
    path = _resolve_config_path(extra_features_file)
    if path is None:
        return df
    if not path.is_file():
        raise SignalFileNotFoundError(
            config_key,
            extra_features_file,
            path,
            producer=_channel_producer(config_key),
        )

    # ── read JSONL ────────────────────────────────────────────────────
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                _LOGGER.warning("Skipping malformed JSONL line %d: %s", lineno, exc)

    if not records:
        # A file that exists but parses to nothing is a *producer* failure,
        # not a first-run state, and it must not be indistinguishable from a
        # null key: the same columns (including the freshness pair) are
        # missing from the observation either way.  Logged at WARNING before
        # raising so the diagnosis survives a caller that catches.
        _LOGGER.warning(
            "Signal file %s (%s) exists but holds no records — skipping merge",
            path,
            config_key or "unlabelled channel",
        )
        raise SignalFileNotFoundError(
            config_key,
            extra_features_file,
            path,
            reason="empty",
            producer=_channel_producer(config_key),
        )

    # ── build a small DataFrame from the signals ──────────────────────
    signal_df = pd.DataFrame(records)
    if "timestamp" not in signal_df.columns:
        _LOGGER.warning("No 'timestamp' column in %s — skipping merge", path)
        return df

    # ── 1. ticker filter ──────────────────────────────────────────────
    # Runs before the join, on the raw records: without it a BTC file
    # annotates ETH bars with BTC sentiment, silently.
    signal_df = _filter_ticker(signal_df, ticker, path, require_ticker=require_ticker)

    signal_df["timestamp"] = pd.to_datetime(signal_df["timestamp"], utc=True)
    undated = signal_df["timestamp"].isna()
    if undated.any():
        _LOGGER.warning(
            "Dropping %d record(s) with an unparseable 'timestamp' in %s",
            int(undated.sum()),
            path,
        )
        signal_df = signal_df[~undated]
    signal_df = signal_df.set_index("timestamp")

    # ── 2. de-duplicate the floored hour ───────────────────────────────
    # Exact-duplicate timestamps first (keep the later line), then a stable
    # sort so "the most recent pull" is the last row of its floored hour,
    # then one row per hour.  `last()` is per column and skips nulls, so a
    # record that updates only some fields keeps the last known value of
    # the others.
    signal_df = signal_df[~signal_df.index.duplicated(keep="last")]
    signal_df = signal_df.sort_index(kind="stable")
    # Floor both indices to the hour so 60-min candles align cleanly
    # even if bar timestamps land at e.g. 14:01 due to exchange quirks.
    signal_df.index = signal_df.index.floor("h")
    signal_df = signal_df.groupby(level=0).last()

    # Keep only the columns on the canonical allow-list; ignore the rest
    # (the raw ticker, ids, any future sibling extras).  The freshness pair
    # is derived here, never read from the file.
    available_cols = [
        c
        for c in _SIGNAL_COLUMNS
        if c in signal_df.columns and c not in _SIGNAL_FRESHNESS_COLUMNS
    ]
    if not available_cols:
        _LOGGER.warning(
            "No signal columns found in %s — expected %s",
            path,
            _SIGNAL_COLUMNS,
        )
        return df

    signal_df = signal_df[available_cols]

    # ── 3. bounded forward-fill + freshness ────────────────────────────
    ohlc_index = df.index.floor("h")
    # Bar spacing is measured on the *unfloored* index: flooring collapses
    # every bar of an hour onto one label, so on 15m/5m bars the median
    # gap of the floored index is 0 and `_bar_hours` would fall back to
    # 1.0.  On hourly bars the two are identical.
    bar_hours = _bar_hours(df.index)
    bound_hours = _resolve_max_age_hours(max_age_hours)

    merged = signal_df.reindex(ohlc_index)
    filled = merged[available_cols].ffill()
    # The bound is expressed in *hours*, so it is applied by masking on the
    # measured age rather than with `ffill(limit=N)`.  A row-count limit
    # only equals an hour-count on bars of an hour or longer: several
    # sub-hourly bars share one floored record-hour, so `limit=N` rows
    # would carry a reading N bars *past* its whole hour.  Masking on age
    # keeps the bound exact on every interval and is identical to the old
    # `ffill(limit=...)` on hourly bars.
    ages = _signal_ages(merged, filled, available_cols, df.index)
    for col in available_cols:
        filled[col] = filled[col].where(ages[col] <= bound_hours)
    # Ages for the rows the mask just retired.  `filled` is the masked
    # frame, so masking the ages by it reproduces exactly the ages of a
    # forward-fill that had never carried past the bound.
    ages = ages.where(filled.notna())

    # ── 4. absence is not neutral ─────────────────────────────────────
    # A row is "observed" when at least one of this source's columns holds
    # a live reading there; `signal_age_hours` is the age of its stalest
    # live column.  The value columns keep the historical zero-fill so no
    # NaN reaches `FeaturePipeline`/`NormalizationStats` — the flag pair is
    # what makes the zero-fill unambiguous.
    observed = filled.notna().any(axis=1)
    source_age = ages.max(axis=1).fillna(_NO_SIGNAL_AGE)

    for col in available_cols:
        df[col] = filled[col].fillna(0.0).to_numpy()

    if not bool(observed.any()):
        _LOGGER.warning(
            "No %s signal record overlaps the %d-bar read window from %s — "
            "every %s value is zero-filled with signal_observed=False "
            "(widen signal_max_age_hours if the records are simply older)",
            ticker or "any",
            len(df),
            path,
            "/".join(available_cols),
        )
    _combine_freshness(df, observed, source_age)

    _LOGGER.debug(
        "Merged %d signal record(s) (%d hour(s)) from %s onto %d OHLCV bars "
        "(columns: %s, ticker: %s, carry bound: %.2fh on %.2fh bars)",
        len(signal_df),
        len(signal_df.index),
        path,
        len(df),
        available_cols,
        ticker or "unfiltered",
        bound_hours,
        bar_hours,
    )
    return df


def _filter_ticker(
    signal_df: pd.DataFrame,
    ticker: str | None,
    path: "Path | str",
    *,
    require_ticker: bool = True,
) -> pd.DataFrame:
    """Keep only the records written for ``ticker``.

    A file without a usable ``ticker`` field is an explicitly one-ticker
    file: it is returned unchanged with a WARNING, so pre-ticker-tagged
    files (and the documented one-ticker cron) keep working.

    Args:
        signal_df: Raw signal records, one row per JSONL line.
        ticker: The pair the frame is for, e.g. ``"ETH/USD"``.
        path: The signal file, for logging.
        require_ticker: Fail loudly on a mismatch instead of merging the
            other tickers' records.

    Returns:
        The filtered records.

    Raises:
        SignalTickerMismatchError: If the file is ticker-tagged, holds no
            record for ``ticker``, and ``require_ticker`` is true.
    """
    # A config key that is present but null must not silently disable the
    # filter, so only an explicit False opts out.
    if require_ticker is None:
        require_ticker = True

    if _TICKER_FIELD not in signal_df.columns:
        _LOGGER.warning(
            "Signal file %s has no '%s' field — treating it as a one-ticker "
            "file and merging every record (set the field on the producer "
            "side to get the per-ticker filter)",
            path,
            _TICKER_FIELD,
        )
        return signal_df

    keys = signal_df[_TICKER_FIELD].map(_canonical_ticker)
    present = sorted({key for key in keys if key})
    if not present:
        _LOGGER.warning(
            "Signal file %s carries an empty '%s' field on every record — "
            "treating it as a one-ticker file and merging every record",
            path,
            _TICKER_FIELD,
        )
        return signal_df

    if ticker is None:
        _LOGGER.warning(
            "No ticker requested for signal file %s (contains: %s) — merging "
            "every record unfiltered",
            path,
            ", ".join(present),
        )
        return signal_df

    wanted = _canonical_ticker(ticker)
    mask = keys == wanted
    if not mask.any():
        if require_ticker:
            raise SignalTickerMismatchError(ticker, present, path)
        _LOGGER.warning(
            "Signal file %s holds no %s record (contains: %s) and "
            "signal_require_ticker is false — merging every record unfiltered",
            path,
            ticker,
            ", ".join(present),
        )
        return signal_df

    kept = int(mask.sum())
    if kept < len(signal_df):
        _LOGGER.debug(
            "Filtered %d/%d signal record(s) from %s to ticker %s "
            "(dropped: %s)",
            kept,
            len(signal_df),
            path,
            ticker,
            ", ".join(key for key in present if key != wanted),
        )
    return signal_df[mask]


def _combine_freshness(
    df: pd.DataFrame,
    observed: pd.Series,
    ages: pd.Series,
) -> None:
    """Fold one source's freshness into the frame's ``signal_*`` pair.

    The three merged sources share one pair of columns, so they are
    combined rather than overwritten: ``signal_observed`` is true if any
    source had a live reading and ``signal_age_hours`` is the age of the
    stalest one (the ``-1.0`` "nothing live" sentinel loses to any real
    age, which is always >= 0).  A source that is silent for the whole
    window therefore does not inflate the age, but it is logged as a
    WARNING by the caller.

    Args:
        df: Frame being merged into; updated in place.
        observed: Per-bar bool, "this source had a live reading here".
        ages: Per-bar float age in hours for this source.
    """
    new_observed = observed.to_numpy(dtype=bool)
    new_age = ages.to_numpy(dtype=float)

    if _SIGNAL_OBSERVED_COLUMN in df.columns:
        previous_observed = df[_SIGNAL_OBSERVED_COLUMN].to_numpy(dtype=bool)
        previous_age = df[_SIGNAL_AGE_COLUMN].to_numpy(dtype=float)
        df[_SIGNAL_OBSERVED_COLUMN] = previous_observed | new_observed
        df[_SIGNAL_AGE_COLUMN] = np.maximum(previous_age, new_age)
    else:
        df[_SIGNAL_OBSERVED_COLUMN] = new_observed
        df[_SIGNAL_AGE_COLUMN] = new_age


def add_derived_ohlcv_features(df: pd.DataFrame) -> pd.DataFrame:
    """Derive the OHLCV-only scalars the observation was missing.

    Every OHLCV frame this module produces already carries ``vwap`` and
    ``count`` — the live path parses them in :func:`candles_to_dataframe`
    and the market-data store persists them — but no feature builder ever
    read either, so both were parsed, stored, exported and then dropped on
    the floor.  This function derives three columns from them and the
    ``signals`` feature group passes them through:

    * ``vwap_dev`` = ``close / vwap - 1`` — the signed deviation of the
      bar close from the volume-weighted average price (buy pressure when
      positive).  The standard intraday "VWAP dev" feature.
    * ``trade_count_zscore_20`` — a 20-bar rolling z-score of the bar's
      trade-execution count, i.e. an *activity surprise*: a big move with
      an unusual number of prints is a participation event, which the
      ``volume`` group cannot express (one huge print and a thousand tiny
      prints look identical there).
    * ``volume_per_trade`` = ``volume / count`` — the bar-scale analogue
      of a tick-level mean trade size, i.e. a persistent fill-composition
      reading (institutional-size vs retail-size prints).

    **Presence-gated, not required-column**, exactly like the
    microstructure builder in ``features.py``: a frame from a source that
    does not carry ``vwap``/``count`` still computes, at the narrower
    width.  Nothing here raises or warns when the inputs are absent —
    silently narrower is the honest answer, and the width guard
    (``features.check_feature_width``) is what makes a *mismatch* loud.

    The three columns are written on a **copy** of ``df`` (leaving the
    caller's frame untouched) and only for the inputs actually present, so
    a frame with ``vwap`` but no ``count`` still gains ``vwap_dev``.

    Args:
        df: Frame indexed by UTC ``DatetimeIndex``, sorted oldest first.

    Returns:
        A new frame with whichever derived columns the inputs support.
    """
    out = df.copy()

    if "close" in df.columns and "vwap" in df.columns:
        close = df["close"].astype(float)
        vwap = df["vwap"].astype(float)
        # A zero/NaN VWAP (empty field on the wire) yields NaN here, which
        # the observation's ffill/fillna handles like every other warmup
        # row rather than poisoning the z-scoring with an infinity.
        out["vwap_dev"] = close / vwap.replace(0.0, np.nan) - 1.0

    if "count" in df.columns:
        count = df["count"].astype(float)
        roll = count.rolling(_TRADE_COUNT_ZSCORE_WINDOW)
        # ddof=0 matches the volume z-score's own convention, and the
        # zero-std guard keeps a constant-count window at 0.0.
        out["trade_count_zscore_20"] = (count - roll.mean()) / roll.std(
            ddof=0
        ).replace(0.0, 1.0)
        if "volume" in df.columns:
            volume = df["volume"].astype(float)
            out["volume_per_trade"] = volume / count.replace(0.0, np.nan)

    return out


def candles_to_dataframe(candles: Sequence[Any]) -> pd.DataFrame:
    """Convert a list of :class:`kraken_api.models.Candle` to a DataFrame.

    Args:
        candles: Candle objects with ``time`` (epoch seconds),
            ``open/high/low/close/vwap/volume`` (string prices) and
            ``count`` fields.

    Returns:
        DataFrame with columns ``time, open, high, low, close, vwap,
        volume, count`` (prices as float, ``time``/``count`` as int),
        indexed by ``pd.to_datetime(time, unit="s")`` and sorted oldest
        first.

    Raises:
        NotEnoughDataError: If ``candles`` is empty.
    """
    if not candles:
        raise NotEnoughDataError(1, 0, what="OHLC candles")
    rows = [
        {
            "time": int(c.time),
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "vwap": float(c.vwap) if c.vwap else float("nan"),
            "volume": float(c.volume) if c.volume else 0.0,
            "count": int(c.count),
        }
        for c in candles
    ]
    df = pd.DataFrame(rows, columns=list(_OHLCV_COLUMNS))
    df = df.sort_values("time").reset_index(drop=True)
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    df.index.name = "time"
    return df


def _page_candles(
    pair: str,
    interval: int,
    pages: int,
    manager: Any,
    since: int | None,
) -> list[Any]:
    """Page through a Kraken-compatible source ``ohlc`` and collect candles.

    Follows the exchange's ``last`` cursor exactly as the loop in
    :func:`fetch_ohlc_dataframe` always has: each call to
    ``manager.ohlc(pair, interval=..., since=cursor)`` returns
    ``(candles, last)``, the next call passes ``since=last``, and the loop
    stops after ``pages`` pages, when the source reports ``last == 0``
    (history exhausted), or when a page returns no candles.  This is shared
    by the live fetch and the ``market-data`` store read-through adapter so
    the two paths can never drift apart on pagination semantics.

    Args:
        pair: Kraken pair notation, e.g. ``"ETH/USD"``.
        interval: Candle interval in minutes.
        pages: Maximum number of OHLC pages to fetch.
        manager: Anything exposing ``ohlc(pair, interval, since) ->
            (candles, last)`` (a ``KrakenManager`` or a duck-typed source).
        since: Optional start cursor for the first page (epoch seconds).

    Returns:
        The collected Candle-like objects (may be empty).
    """
    if pages < 1:
        raise ValueError(f"`pages` must be >= 1, got {pages}")
    collected: list[Any] = []
    cursor: int | None = since
    for _ in range(pages):
        batch, last = manager.ohlc(pair, interval=interval, since=cursor)
        if batch:
            collected.extend(batch)
        if last == 0 or not batch:
            _LOGGER.debug("OHLC history exhausted at cursor %s", cursor)
            break
        cursor = last
    return collected


def fetch_ohlc_dataframe(
    pair: str,
    interval: int,
    pages: int = 6,
    manager: "KrakenManager | None" = None,
    since: int | None = None,
    extra_features_file: str | None = None,
    funding_features_file: str | None = None,
    social_features_file: str | None = None,
    signal_max_age_hours: int | None = None,
    signal_require_ticker: bool = True,
) -> pd.DataFrame:
    """Page through Kraken OHLCV history into a training DataFrame.

    The loop follows the exchange's ``last`` cursor: each call to
    :meth:`KrakenManager.ohlc` returns ``(candles, last)``, and the next
    call passes ``since=last``.  The loop stops after ``pages`` pages,
    when the exchange reports ``last == 0`` (history exhausted), or when
    a page returns no candles.

    Args:
        pair: Kraken pair notation, e.g. ``"ETH/USD"``.
        interval: Candle interval in minutes (see
            :data:`kraken_api.manager.OHLC_INTERVALS`).
        pages: Maximum number of OHLC pages to fetch.
        manager: A :class:`kraken_api.KrakenManager`; defaults to
            ``KrakenManager.from_env()`` when omitted.
        since: Optional start cursor for the first page (epoch seconds).
        extra_features_file: Optional path to a JSONL of per-(ticker,
            hour) signal vectors (see :func:`merge_extra_features`); a
            feature-engineering seam joins them onto the OHLCV frame by
            floored bar timestamp. ``None`` (config default) disables it.
        funding_features_file: Optional path to a JSONL of per-(ticker,
            hour) funding-rate/basis vectors from the sibling
            ``kraken-funding-rates`` project.  Merged after news signals
            via the same timestamp-floor left-join.
        social_features_file: Optional path to a JSONL of per-(ticker,
            hour) social/search-trend vectors from the sibling
            ``kraken-social-signals`` project.  Merged after funding
            signals via the same timestamp-floor left-join.
        signal_max_age_hours: Staleness bound applied to every merged
            signal file, in hours (the ``signal_max_age_hours`` config
            key).  ``None`` (the config default) derives one hour of
            carry from the bar interval; ``0`` disables forward-fill.
        signal_require_ticker: Fail loudly when a ticker-tagged signal
            file holds no record for ``pair`` (the
            ``signal_require_ticker`` config key, default true).

    Returns:
        DataFrame as produced by :func:`candles_to_dataframe` with
        ``open/high/low/close/volume`` columns suitable for
        :class:`~kraken_trading_bot.rl.environment.TradingEnvironment`.

    Raises:
        NotEnoughDataError: If no candles could be fetched at all.
        SignalTickerMismatchError: Propagated from
            :func:`merge_extra_features` when a ticker-tagged signal file
            holds no record for ``pair``.
    """
    if manager is None:  # deferred import: only needed for live fetching
        from kraken_api import KrakenManager

        manager = KrakenManager.from_env()

    collected = _page_candles(pair, interval, pages, manager, since)

    if not collected:
        raise NotEnoughDataError(1, 0, what="OHLC candles")

    df = candles_to_dataframe(collected)
    # The exchange may return overlapping boundary candles across pages;
    # keep the first occurrence per timestamp.
    df = df[~df.index.duplicated(keep="first")].sort_index()
    # OHLCV-derived scalars (vwap_dev, trade_count_zscore_20,
    # volume_per_trade) are derived here, on the assembled frame rather
    # than per page, so the rolling window sees real consecutive bars
    # across page boundaries.  Presence-gated: a source without vwap/count
    # simply comes back at the narrower width.
    df = add_derived_ohlcv_features(df)
    for config_key, signal_file, _producer in _signal_channels(
        extra_features_file, funding_features_file, social_features_file
    ):
        df = merge_extra_features(
            df,
            signal_file,
            ticker=pair,
            max_age_hours=signal_max_age_hours,
            require_ticker=signal_require_ticker,
            config_key=config_key,
        )
    return df


def read_ohlc_dataframe(
    pair: str,
    interval: int,
    *,
    pages: int = 6,
    manager: "KrakenManager | None" = None,
    since: int | None = None,
    until: int | None = None,
    extra_features_file: str | None = None,
    funding_features_file: str | None = None,
    social_features_file: str | None = None,
    signal_max_age_hours: int | None = None,
    signal_require_ticker: bool = True,
    market_data_store: Any = None,
    market_data_source: Any = None,
) -> pd.DataFrame:
    """Read OHLCV through the local market-data store, or fall back to live.

    The integration seam for the sibling ``kraken-market-data`` project.
    When ``market_data_store`` is set, this performs **fetch -> upsert ->
    read**: the window is paged from Kraken exactly as
    :func:`fetch_ohlc_dataframe` does, appended into the store, and then
    read back — so the requested window is also persisted and later reads
    can return deeper history than Kraken's ~720-bar REST ceiling.  When
    ``market_data_store`` is unset (the config default, ``null``), it is a
    pass-through to :func:`fetch_ohlc_dataframe` and behaviour is
    byte-identical, which keeps the existing train/backtest/paper call
    sites working unchanged.

    Args:
        pair: Kraken pair notation, e.g. ``"ETH/USD"``.
        interval: Candle interval in minutes.
        pages: Maximum number of OHLC pages to fetch before the read.
        manager: A :class:`kraken_api.KrakenManager` for the fetch leg;
            defaults to ``KrakenManager.from_env()`` when omitted.
        since: Inclusive lower bound on the returned window (epoch seconds
            or ``None`` for the whole store).
        until: Exclusive upper bound on the returned window (epoch seconds
            or ``None`` for no bound).
        extra_features_file: Passed to :func:`merge_extra_features` on the
            read frame, exactly as in :func:`fetch_ohlc_dataframe`.
        funding_features_file: Optional path to a JSONL of per-(ticker,
            hour) funding-rate/basis vectors from the sibling
            ``kraken-funding-rates`` project.  Merged after news signals
            via the same timestamp-floor left-join.
        social_features_file: Optional path to a JSONL of per-(ticker,
            hour) social/search-trend vectors from the sibling
            ``kraken-social-signals`` project.  Merged after funding
            signals via the same timestamp-floor left-join.
        signal_max_age_hours: Staleness bound applied to every merged
            signal file, in hours (the ``signal_max_age_hours`` config
            key).  ``None`` (the config default) derives one hour of
            carry from the bar interval; ``0`` disables forward-fill.
        signal_require_ticker: Fail loudly when a ticker-tagged signal
            file holds no record for ``pair`` (the
            ``signal_require_ticker`` config key, default true).
        market_data_store: ``null`` (live fetch), a store root path, or a
            store-like object exposing ``upsert``/``read`` (see
            :func:`_resolve_store`).
        market_data_source: Fetch source for the upsert leg; defaults to
            ``manager``.  Anything exposing ``ohlc(pair, interval, since)
            -> (candles, last)`` works (a live ``KrakenManager``, the
            store's own thin client, or a fake in tests).

    Returns:
        The same DataFrame shape :func:`fetch_ohlc_dataframe` returns
        (UTC ``DatetimeIndex``, columns ``time, open, high, low, close,
        vwap, volume, count``).

    Raises:
        ValueError: If a store *path* is configured but the sibling
            package is not importable.
        TypeError: If ``market_data_store`` is neither null, a path, nor a
            store-like object.
        NotEnoughDataError: If the read window is empty.
        SignalTickerMismatchError: Propagated from
            :func:`merge_extra_features` when a ticker-tagged signal file
            holds no record for ``pair``.

    .. todo:: Follow-up integration pass (see ``kraken-market-data/
       INTEGRATION.md``): honour ``since``/``until`` from config; drive
       train/eval split and walk-forward through the currently-unused
       ``TradingEnvironment.reset(options=...)``; let backtest skip the
       re-fetch and paper trade collapse to append + tail read.
    """
    if market_data_store is None:
        return fetch_ohlc_dataframe(
            pair,
            interval=interval,
            pages=pages,
            manager=manager,
            since=since,
            extra_features_file=extra_features_file,
            funding_features_file=funding_features_file,
            social_features_file=social_features_file,
            signal_max_age_hours=signal_max_age_hours,
            signal_require_ticker=signal_require_ticker,
        )

    # DEEP-HISTORY (kraken-deep-history): with a store seeded by the
    # sibling `kraken-deep-history` project (Binance-archive OHLCV -> the
    # `kraken-market-data` store root), this branch serves years of bars
    # instead of Kraken's ~720-bar REST ceiling: since/until default to
    # None so `store.read` returns the whole store, and `pages` only bounds
    # the live append below. Turn it on per-model by setting
    # `market_data_store` in models/{TICKER}/{NAME}/config.yaml (or via
    # `configs/deep-history.example.yaml`). A seeded store makes since/until
    # slicing, train/eval split, and walk-forward through the currently-
    # unused `TradingEnvironment.reset(options=...)` possible — a follow-up
    # pass, not a feature-engineering rewrite (see
    # kraken-deep-history/INTEGRATION.md §5).
    store = _resolve_store(market_data_store)

    source = market_data_source if market_data_source is not None else manager
    if source is None:  # deferred import: only needed for live fetching
        from kraken_api import KrakenManager

        source = KrakenManager.from_env()

    # fetch -> upsert: page the window from the source and append it to the
    # store, so the read below covers both pre-existing history and the
    # just-fetched bars.
    candles = _page_candles(pair, interval, pages, source, since)
    if candles:
        store.upsert(pair, interval, candles)
        _LOGGER.info(
            "Upserted %d %d-minute %s bars into market-data store",
            len(candles),
            interval,
            pair,
        )
    else:
        _LOGGER.warning(
            "market-data store fetch returned no candles for %s/%d — "
            "reading whatever the store already has",
            pair,
            interval,
        )

    df = store.read(pair, interval, since=since, until=until)
    if isinstance(df, pd.DataFrame) and df.empty:
        raise NotEnoughDataError(1, 0, what="OHLC candles")
    # Same derivation as the live leg above, so the store and the direct
    # fetch produce the identical column set — the two must never drift.
    df = add_derived_ohlcv_features(df)
    for config_key, signal_file, _producer in _signal_channels(
        extra_features_file, funding_features_file, social_features_file
    ):
        df = merge_extra_features(
            df,
            signal_file,
            ticker=pair,
            max_age_hours=signal_max_age_hours,
            require_ticker=signal_require_ticker,
            config_key=config_key,
        )
    return df


def _resolve_store(market_data_store: Any) -> Any:
    """Resolve the ``market_data_store`` config value to a usable store.

    Two shapes are accepted, both duck-typed against the sibling
    ``kraken-market-data`` contract (``upsert(pair, interval, candles)``
    plus ``read(pair, interval, since, until) -> DataFrame``):

    * a **path** (``str``/``os.PathLike``) to a store root — the sibling
      package is imported lazily and ``MarketDataStore(path)`` is built
      (raises a clear error if it is not installed).  A ``~``-spelled path
      is expanded against ``$HOME``, which is how
      ``configs/deep-history.example.yaml`` spells it;
    * a **store-like object** — used directly, which is what lets tests
      (and embedded callers) drive the seam without the sibling package.

    Returns:
        An object exposing the store's ``upsert``/``read`` contract.

    Raises:
        ValueError: If the sibling package is missing for a path config.
        TypeError: If the value is neither a path nor a store-like object.
    """
    upsert = getattr(market_data_store, "upsert", None)
    read = getattr(market_data_store, "read", None)
    if callable(upsert) and callable(read):
        return market_data_store
    if isinstance(market_data_store, (str, os.PathLike)):
        try:  # deferred import: optional accent, not a hard dependency
            from market_data.store import MarketDataStore
        except ImportError as exc:  # pragma: no cover - env dependent
            raise ValueError(
                "market_data_store is configured but the sibling "
                "`kraken-market-data` package is not importable; install it "
                "or set market_data_store: null in config to use the live "
                "paginated fetch."
            ) from exc
        return MarketDataStore(_resolve_config_path(market_data_store) or Path())
    raise TypeError(
        "market_data_store must be a store root path, a MarketDataStore-like "
        "object with upsert()/read(), or None (live fetch); got "
        f"{type(market_data_store).__name__}"
    )


def prepare_episode(
    df: pd.DataFrame,
    features: FeaturePipeline,
    ticker_id: str | None = None,
    episode_bars: int | None = None,
) -> pd.DataFrame:
    """Fit features on OHLCV data and slice an episode-sized window.

    The :class:`TradingEnvironment` treats the DataFrame it is given as
    one episode (its ``reset(options=...)`` protocol is currently
    unused), so bounding episode length means bounding the DataFrame.
    This helper slices the trailing ``episode_bars`` bars **first**, then
    fits the pipeline's per-ticker normalization stats on that window
    only — never on the held-out tail of the fetched frame, which would
    leak future bars into the stats.  With ``episode_bars`` None the
    window is the full frame and nothing changes.  It validates that
    enough data is present, then returns the window.

    Args:
        df: Raw OHLCV DataFrame (``open/high/low/close/volume``).
        features: Feature pipeline to fit; its per-ticker stats are
            registered under ``ticker_id``.
        ticker_id: Ticker the stats belong to (e.g. ``"ETH/USD"``).
        episode_bars: Number of trailing bars to slice into one episode;
            None (default) keeps the full frame.

    Returns:
        The episode window DataFrame to pass as ``data`` to a
        :class:`TradingEnvironment`.

    Raises:
        NotEnoughDataError: If the frame is empty/shorter than the
            pipeline's minimum length, or ``episode_bars`` would slice
            below that minimum.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise NotEnoughDataError(1, 0, what="OHLC bars")

    ticker_key = normalize_ticker_id(ticker_id) if ticker_id else ""

    needed = _minimum_bars(features)
    available = len(df)
    if available < needed:
        raise NotEnoughDataError(needed, available, what="OHLC bars")

    if episode_bars is not None:
        if int(episode_bars) < needed:
            raise NotEnoughDataError(int(episode_bars), available, what="episode bars")
        window = df.tail(int(episode_bars))
    else:
        window = df

    # Slice-first-then-fit: the stats now cover exactly the bars the
    # episode trades, never bars outside the trailing window.
    features.fit(window, ticker_id=ticker_key)

    _LOGGER.debug(
        "Prepared %s-bar episode for %s (full frame %d bars)",
        len(window),
        ticker_key or "unknown",
        available,
    )
    return window


def _minimum_bars(features: FeaturePipeline) -> int:
    """Minimum OHLC bars a pipeline can produce a useful episode from."""
    max_window = max(features.windows) if features.windows else 1
    return int(max_window) + _WARMUP_PAD


__all__ = [
    "NotEnoughDataError",
    "SignalFileNotFoundError",
    "SignalTickerMismatchError",
    "candles_to_dataframe",
    "add_derived_ohlcv_features",
    "merge_extra_features",
    "fetch_ohlc_dataframe",
    "read_ohlc_dataframe",
    "prepare_episode",
    "_minimum_bars",
    "_page_candles",
    "_resolve_store",
]