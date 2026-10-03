"""G1 order-book DEPTH RECORDER — append-only JSONL, own clock, gap counter.

WHY THIS EXISTS (the whole reason it is urgent)
-----------------------------------------------
Kraken's order book is a **live snapshot with no historical endpoint**.
Kraken's public archive is OHLCVT-only; Binance Vision's ``bookDepth`` was
falsified directly against the S3 bucket (``KeyCount=0`` against a klines
control that returned 2).  A ``count=10`` snapshot at *t* does not encode
*t-1h*.  **Every hour without this recorder is data that cannot be
recovered later.**  Nothing downstream can backfill it.

WHAT IT DOES
------------
One keyless ``/0/public/Depth`` call per fire, one JSON object appended to
one file.  Deliberately:

* **"log, not state"** — the same convention the funding signal seam already
  uses.  The file is only ever appended to; nothing is rewritten, and no
  prior record is ever corrected in place.  A re-fire inside the same hour
  (``Persistent=true`` catch-up after a lid-close) therefore appends a
  second, harmless line rather than clobbering a good reading.
* **its own clock** — a book snapshot carries **no timestamp of its own**:
  each level's ``ts`` is *that order's* placement time, so all levels in a
  snapshot share an age and carry no clock at all.  ``recorded_at`` is this
  process's own UTC stamp, taken *after* the response lands.  Without it two
  snapshots cannot be told apart in time at all.
* **its own depth** — ``count`` is a request, not a guarantee, and depth is
  **not comparable across a ``count`` change**.  Every record carries
  ``depth.requested_count`` *and* the counts actually returned, plus
  ``depth.truncated``.  This is not hypothetical: measured on 2026-10-03,
  ``count=1000`` returns **100** levels per side, i.e. a requested depth
  four times the served one with **no error and no warning from Kraken** —
  a silent depth *reduction*.  Without ``truncated`` in the record, that
  reads as a depth-100 book that happened to have 100 levels.
* **a gap counter** — F-6 showed this repo's producers can stop for a week
  and append happily afterwards, leaving a hole indistinguishable from a run
  that never stopped.  Every record therefore carries the expected-vs-actual
  interval that produced it (``interval.delta_seconds`` vs
  ``interval.expected_seconds``, and ``interval.status``), and the hole is
  **visible in the artifact**, not only in stdout.

The gap counter is a pure function (:func:`scan_gaps`) over the timestamps in
the file, so it can be re-run against history at any time and — importantly —
can be shown to go RED.  A counter that has only ever printed ``0 gaps`` is
indistinguishable from a counter that cannot count.

SCOPE
-----
This module **writes data and reads none of it back**.  There is deliberately
no imbalance feature, no ``_SIGNAL_COLUMNS`` entry, no observation-width
change and no merge into the observation.  The recorder must run unattended
for weeks before anything depends on its output; consuming it in the same
slice would create a reason to change its schema the first time an awkward
column appeared, and the history already written would become incompatible
with itself.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

_LOGGER = logging.getLogger(__name__)

#: Bumped only on an incompatible change to the record shape.  Records
#: written under a different value are still readable — the reader only ever
#: needs ``recorded_at`` — but a consumer that wants more must branch.
SCHEMA_VERSION = 1

#: Provenance string written into every record.  The Depth endpoint is
#: keyless, so this needs no credential and no rate-limit tier.
SOURCE = "kraken.public.Depth"

#: Kraken's own served default, and this recorder's default ``count``.
#:
#: MEASURED 2026-10-03 against ``/0/public/Depth?pair=ETHUSD``:
#:   no ``count`` -> 100 bids / 100 asks   (7,476 B)
#:   ``count=100`` -> 100 / 100             (7,476 B)
#:   ``count=500`` -> 500 / 500            (37,238 B)
#:   ``count=1000`` -> 100 / 100   <-- SILENT TRUNCATION, no error
#:
#: So 100 is the largest depth Kraken serves by default and the largest that
#: is actually honoured without asking: 7.5 KB per record is ~180 kB/day,
#: ~66 MB/year, which is a log.  ``count=500`` would be ~325 MB/year and buys
#: a deeper imbalance basis nobody has measured yet.
DEFAULT_COUNT = 100

#: Hourly, matching the ``ohlcv_interval_minutes: 60`` hour-floor in
#: ``configs/default.yaml``.  One row per bar-hour means anything finer pays
#: 60x the calls to keep the same rows.
DEFAULT_EXPECTED_INTERVAL_SECONDS = 3600.0

#: A hole is an interval longer than ``expected * gap_factor``.  1.5 is set
#: against the timer's ``RandomizedDelaySec=120`` plus ``Persistent=true``
#: catch-up jitter, which can legitimately stretch one interval to ~1h4m
#: without anything having been missed.  1.5x puts the threshold at 1h30m,
#: comfortably above that jitter and comfortably below the 2h that losing a
#: single fire produces.
DEFAULT_GAP_FACTOR = 1.5

#: Keys the reader needs from each line.  Kept to one function
#: (:func:`read_recorded_at`) so the gap scan does not depend on the rest of
#: the schema and keeps working across ``SCHEMA_VERSION`` bumps.
_RECORDED_AT_KEY = "recorded_at"

STATUS_FIRST = "FIRST"
STATUS_OK = "OK"
STATUS_SHORT = "SHORT"
STATUS_GAP = "GAP"


# ─────────────────────────────────────────────────────────────────────────────
# Clock helpers
# ─────────────────────────────────────────────────────────────────────────────


def utc_now() -> datetime:
    """The recorder's own clock: timezone-aware UTC, microsecond precision."""
    return datetime.now(timezone.utc)


def floor_hour(dt: datetime) -> datetime:
    """Floor to the hour, in whatever tz ``dt`` carries.

    The funding sibling stores hour-floored timestamps so a once-per-hour
    pull naturally produces one record per hour and a re-fire inside the hour
    is resolvable by the reader.  This recorder keeps the **full-precision**
    ``recorded_at`` for the gap arithmetic (a floored clock cannot see a hole
    shorter than an hour) and adds a floored ``hour`` key alongside it for the
    same reader-side de-duplication the seam already does.
    """
    return dt.replace(minute=0, second=0, microsecond=0)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 stamp, accepting a trailing ``Z``.  ``None`` if unusable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        # A naive stamp is ambiguous.  Treating it as UTC is the only
        # interpretation consistent with everything this module writes; the
        # alternative (dropping the record) would silently shorten the file.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ─────────────────────────────────────────────────────────────────────────────
# Gap counting — the guard.  Pure functions over timestamps.
# ─────────────────────────────────────────────────────────────────────────────


#: sha256 of the two RAW LINES of ``signals/eth_usd_orderbook.jsonl`` that were
#: **seeded** — copied byte-for-byte out of the aborted recorder worktree via
#: ``/tmp/g1-rescued/`` on 2026-10-03, not appended by a run in this checkout.
#:
#: They are real Kraken readings (real prices, the recorder's own clock, full
#: 100-level books) but they are NOT on the hourly cadence and NOT unattended
#: fires, so they are excluded from the depth count.  The log is append-only and
#: rewriting it to tag them is forbidden, and a future consumer reading the file
#: will never see EVIDENCE §6 — so the exclusion is pinned BY HASH here, where
#: the code that does the excluding can enforce it.
#:
#: Hashing the raw line (not the parsed record) means the pin identifies the exact
#: bytes on disk: a re-serialisation that changed a float, a key order, or a
#: timestamp would not match, which is the point — it must be the same reading.
SEEDED_RECORD_SHA256: frozenset[str] = frozenset(
    {
        # 2026-10-03T19:10:45.752567+00:00  bid=2683.52000
        "392e06b46fc9597cc4ed30220e2a5217b4fc82654336cd53bdebaa751099ff0f",
        # 2026-10-03T19:12:55.755671+00:00  bid=2683.51000
        "7d7b62cc62390fcbeb24ce65a09ec7ce02e2e5375f9b94aacf498f6a6f4aaacd",
    }
)

#: Target depth N, counted in DISTINCT HOURLY BUCKETS holding a valid record —
#: never in rows.  Counting rows reaches N early the moment two records share an
#: hour, which is exactly what happened here: records #3 (19:37) and #4 (19:42)
#: are five minutes apart in the same bucket, so 7 rows cover only 4 hours.
#: Re-derived in RUN-LOG §22.8; 365 days at one snapshot per hour.
TARGET_DEPTH_HOURS: int = 8760


@dataclass(frozen=True, slots=True)
class Interval:
    """One consecutive pair of records, classified against the expectation."""

    after: str
    before: str
    delta_seconds: float
    expected_seconds: float
    factor: float
    status: str
    missing_snapshots: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "after": self.after,
            "before": self.before,
            "delta_seconds": round(self.delta_seconds, 3),
            "expected_seconds": self.expected_seconds,
            "factor": round(self.factor, 3),
            "status": self.status,
            "missing_snapshots": self.missing_snapshots,
        }


@dataclass(frozen=True, slots=True)
class GapReport:
    """The result of scanning a recorded file's timestamps.

    ``ok`` is the whole point of the report.  ``n_gaps == 0`` and ``ok`` are
    the same claim, stated once as a boolean a caller can gate on and once as
    the number a human reads.
    """

    expected_interval_seconds: float
    gap_factor: float
    n_records: int
    n_intervals: int
    n_gaps: int
    n_short_intervals: int
    n_missing_snapshots: int
    longest_gap_seconds: float
    first_recorded_at: str | None
    last_recorded_at: str | None
    observed_span_seconds: float
    #: DISTINCT floored hours holding at least one record.  This — not
    #: ``n_records`` — is the unit depth N is counted in, so two records in one
    #: hour (the bring-up pair, or a double-firing timer) cannot inflate it.
    n_hours_covered: int = 0
    #: Rows present but excluded by :data:`SEEDED_RECORD_SHA256` — real readings
    #: that are not on the cadence, so not part of the depth count.
    n_seeded_excluded: int = 0
    #: The GAP-classified intervals, verbatim — same objects as ``intervals``.
    holes: tuple[Interval, ...] = ()
    intervals: tuple[Interval, ...] = ()

    @property
    def ok(self) -> bool:
        """True when no interval exceeded ``expected * gap_factor`` AND the log
        is not running a sustained double cadence.

        Gaps alone are not sufficient.  A double timer produces two records per
        hour, ~1800 s apart: no interval is long, so ``n_gaps == 0``, and the
        old verdict read ``GREEN`` on a misconfigured recorder.  ``>= 2`` SHORT
        intervals is the sustained-pattern threshold — a single SHORT is the
        expected ``Persistent=true`` catch-up at enablement and stays green.
        """
        return self.n_gaps == 0 and self.n_short_intervals < 2

    @property
    def expected_span_seconds(self) -> float:
        """How long ``n_records`` on-cadence records *would* span.

        ``n_records - 1`` intervals, because one record has no interval.  A
        two-record file has an expected span of one interval, which is the
        arithmetic that makes a first-ever pair of records read as ``OK``
        rather than as unexplained time.

        Note this is NOT the denominator of :attr:`coverage_ratio`: a log with
        a hole spans MORE wall-clock time than its record count implies, so
        dividing by this would report the hole as surplus rather than as loss.
        """
        if self.n_records < 2:
            return 0.0
        return (self.n_records - 1) * self.expected_interval_seconds

    @property
    def expected_slots(self) -> int:
        """How many records an on-cadence run WOULD hold over the observed span.

        Derived from the wall-clock span the log actually covers, so it goes
        UP when a hole appears.  That direction is the whole point: coverage
        has to fall when hours are missing, not stay at 100% because the file
        grew long enough to hide the loss.
        """
        if self.n_records < 2:
            return self.n_records
        slots = int(round(self.observed_span_seconds / self.expected_interval_seconds))
        return max(self.n_records, slots + 1)

    @property
    def coverage_ratio(self) -> float | None:
        """Fraction of the hourly buckets the log's span covers that hold a
        record.

        Counted in **distinct floored hours**, not rows.  A row is not a unit of
        depth: records #3 (19:37) and #4 (19:42) sit five minutes apart in the
        same hour, so counting rows would reach a target N early and would let a
        double-firing timer inflate progress.  Two records in one hour cover that
        hour once.

        ``None`` when there are not yet two records, because "coverage" of a
        single record is not a number, and a fabricated ``0.0`` would read as
        "nothing recorded".

        An earlier revision divided observed by the record-count-implied span
        and clamped at 1.0, which reported **100% coverage on a log with a
        five-hour hole** — the hole made the file longer, and the clamp hid it.
        Measured on that revision: 4 records spanning 8 hours read as
        ``coverage_ratio == 1.0`` with ``n_gaps == 1``.  Hence the observed-span
        denominator, and hence hours rather than rows.
        """
        hours = self.hours_in_span
        if hours <= 0 or self.n_records < 2:
            # One record spans exactly one hour, so the hour-based denominator
            # would read 1/1 = 100% — a green coverage claim off a single
            # reading.  Coverage of one record is not a number.
            return None
        return min(1.0, self.n_hours_covered / hours)

    @property
    def hours_in_span(self) -> int:
        """Distinct hourly buckets between the first and last record inclusive.

        The denominator coverage is measured against.  Hour-based rather than
        record-based for the same reason :attr:`coverage_ratio` is.
        """
        first = parse_timestamp(self.first_recorded_at)
        last = parse_timestamp(self.last_recorded_at)
        if first is None or last is None:
            return 0
        return int((floor_hour(last) - floor_hour(first)).total_seconds() // 3600) + 1

    @property
    def depth_fraction(self) -> float:
        """Progress toward :data:`TARGET_DEPTH_HOURS`, in hours covered."""
        return self.n_hours_covered / TARGET_DEPTH_HOURS if TARGET_DEPTH_HOURS else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_interval_seconds": self.expected_interval_seconds,
            "gap_factor": self.gap_factor,
            "n_records": self.n_records,
            "n_intervals": self.n_intervals,
            "n_gaps": self.n_gaps,
            "n_short_intervals": self.n_short_intervals,
            "n_missing_snapshots": self.n_missing_snapshots,
            "longest_gap_seconds": round(self.longest_gap_seconds, 3),
            "first_recorded_at": self.first_recorded_at,
            "last_recorded_at": self.last_recorded_at,
"observed_span_seconds": round(self.observed_span_seconds, 3),
                "n_hours_covered": self.n_hours_covered,
                "hours_in_span": self.hours_in_span,
                "n_seeded_excluded": self.n_seeded_excluded,
                "target_depth_hours": TARGET_DEPTH_HOURS,
                "depth_fraction": round(self.depth_fraction, 9),
                "expected_span_seconds": round(self.expected_span_seconds, 3),
                "expected_slots": self.expected_slots,
            "coverage_ratio": (
                None if self.coverage_ratio is None else round(self.coverage_ratio, 6)
            ),
            "ok": self.ok,
            "holes": [h.to_dict() for h in self.holes],
        }


def classify_interval(
    prev: datetime,
    current: datetime,
    *,
    expected_interval_seconds: float = DEFAULT_EXPECTED_INTERVAL_SECONDS,
    gap_factor: float = DEFAULT_GAP_FACTOR,
) -> Interval:
    """Classify one consecutive pair of stamps against the expectation.

    Three outcomes, all of them reportable:

    ``OK``    within ``expected * gap_factor``.
    ``SHORT`` shorter than ``expected / gap_factor`` — a ``Persistent=true``
              catch-up re-fire, or two manual pulls in one hour.  Reported and
              counted **separately from gaps** so a hole cannot hide among
              them and a benign duplicate cannot be read as one.
    ``GAP``   longer than ``expected * gap_factor``.  ``missing_snapshots`` is
              ``round(delta / expected) - 1``: how many hourly records the
              hole swallowed.
    """
    if expected_interval_seconds <= 0:
        raise ValueError(
            f"expected_interval_seconds must be positive, got {expected_interval_seconds!r}"
        )
    if gap_factor <= 0:
        raise ValueError(f"gap_factor must be positive, got {gap_factor!r}")
    if gap_factor < 1.0:
        raise ValueError(
            f"gap_factor below 1.0 would call every interval long, got {gap_factor!r}"
        )

    delta = (current - prev).total_seconds()
    factor = delta / expected_interval_seconds

    if delta > expected_interval_seconds * gap_factor:
        status = STATUS_GAP
        missing = max(1, int(round(delta / expected_interval_seconds)) - 1)
    elif delta < expected_interval_seconds / gap_factor:
        status = STATUS_SHORT
        missing = 0
    else:
        status = STATUS_OK
        missing = 0

    return Interval(
        after=prev.isoformat(),
        before=current.isoformat(),
        delta_seconds=delta,
        expected_seconds=expected_interval_seconds,
        factor=factor,
        status=status,
        missing_snapshots=missing,
    )


def scan_gaps(
    stamps: Sequence[datetime],
    *,
    expected_interval_seconds: float = DEFAULT_EXPECTED_INTERVAL_SECONDS,
    gap_factor: float = DEFAULT_GAP_FACTOR,
) -> GapReport:
    """Scan an ordered list of stamps and report every hole between them.

    ``stamps`` may be unsorted and may contain duplicates: both are real
    states of a log that a ``Persistent=true`` timer produces, so both are
    sorted here and then scanned.  A duplicate shows up as a ``SHORT``
    interval, not as a ``GAP``.
    """
    ordered = sorted(stamps)
    intervals = tuple(
        classify_interval(
            prev,
            current,
            expected_interval_seconds=expected_interval_seconds,
            gap_factor=gap_factor,
        )
        for prev, current in zip(ordered, ordered[1:])
    )
    holes = tuple(i for i in intervals if i.status == STATUS_GAP)
    shorts = tuple(i for i in intervals if i.status == STATUS_SHORT)
    longest = max((h.delta_seconds for h in holes), default=0.0)
    span = (
        (ordered[-1] - ordered[0]).total_seconds() if len(ordered) >= 2 else 0.0
    )
    return GapReport(
        expected_interval_seconds=expected_interval_seconds,
        gap_factor=gap_factor,
        n_records=len(ordered),
        n_intervals=len(intervals),
        n_gaps=len(holes),
        n_short_intervals=len(shorts),
        n_missing_snapshots=sum(h.missing_snapshots for h in holes),
        longest_gap_seconds=longest,
        first_recorded_at=ordered[0].isoformat() if ordered else None,
        last_recorded_at=ordered[-1].isoformat() if ordered else None,
        observed_span_seconds=span,
        n_hours_covered=len({floor_hour(s) for s in ordered}),
        holes=holes,
        intervals=intervals,
    )


def scan_gap_file(
    path: str | Path,
    *,
    expected_interval_seconds: float = DEFAULT_EXPECTED_INTERVAL_SECONDS,
    gap_factor: float = DEFAULT_GAP_FACTOR,
) -> GapReport:
    """Scan a recorded JSONL file's timestamps.  A missing file is zero records.

    Seeded rows are counted and reported but **excluded from the depth count**.
    They are real readings that are not on the cadence (see
    :data:`SEEDED_RECORD_SHA256`), and the exclusion has to happen HERE, where
    the file is read — a consumer opening the raw file will not know.
    """
    p = Path(path)
    raw = read_recorded_at(p)
    report = scan_gaps(
        raw,
        expected_interval_seconds=expected_interval_seconds,
        gap_factor=gap_factor,
    )
    seeded = seeded_record_hashes(p)
    if not seeded:
        return report
    seeded_stamps = {
        ts for ts, h in _stamps_with_hashes(p) if h in SEEDED_RECORD_SHA256
    }
    kept = [ts for ts in raw if ts not in seeded_stamps]
    # Intervals are re-derived over the kept rows only, so a seeded row cannot
    # manufacture a SHORT interval or a hole either.
    rescanned = scan_gaps(
        kept,
        expected_interval_seconds=expected_interval_seconds,
        gap_factor=gap_factor,
    )
    from dataclasses import replace

    return replace(
        rescanned,
        n_records=len(raw),
        n_seeded_excluded=len(raw) - len(kept),
    )


def _stamps_with_hashes(path: str | Path) -> list[tuple[datetime, str]]:
    """``(recorded_at, sha256-of-raw-line)`` for every parseable line."""
    p = Path(path)
    if not p.exists():
        return []
    import hashlib
    import json as _json

    out: list[tuple[datetime, str]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = _json.loads(line)
            ts = parse_timestamp(rec.get("recorded_at"))
        except ValueError:
            continue
        if ts is not None:
            out.append((ts, hashlib.sha256(line.encode("utf-8")).hexdigest()))
    return out


def seeded_record_hashes(path: str | Path) -> set[str]:
    """The :data:`SEEDED_RECORD_SHA256` pins actually present in ``path``."""
    present = {h for _, h in _stamps_with_hashes(path)}
    return present & SEEDED_RECORD_SHA256


# ─────────────────────────────────────────────────────────────────────────────
# Reading and appending
# ─────────────────────────────────────────────────────────────────────────────


def read_recorded_at(path: str | Path) -> list[datetime]:
    """Every parseable ``recorded_at`` in a JSONL file, in file order.

    A malformed line is skipped with a WARNING rather than raising: the
    funding seam's precedent is explicit that a producer failure must be
    distinguishable from a first-run state, and a file that exists but cannot
    be scanned is a producer failure.  The count of skipped lines is reported
    by the caller's report, so skipping is visible rather than silent.
    """
    p = Path(path)
    if not p.exists():
        return []
    stamps: list[datetime] = []
    skipped = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(rec, dict):
            skipped += 1
            continue
        stamp = parse_timestamp(rec.get(_RECORDED_AT_KEY))
        if stamp is None:
            skipped += 1
            continue
        stamps.append(stamp)
    if skipped:
        _LOGGER.warning(
            "%d line(s) of %s carried no usable %s and were skipped by the "
            "gap scan",
            skipped,
            p,
            _RECORDED_AT_KEY,
        )
    return stamps


def append_record(path: str | Path, record: dict[str, Any]) -> None:
    """Append ONE record as ONE line, flushed and fsynced.

    Append-only and never rewriting: a hole in the file must stay a hole.  The
    ``fsync`` is load-bearing rather than defensive — this is an unattended
    timer whose whole value is what survives, and a buffered line lost to a
    power cut is a hole the gap counter would report as unexplained.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"), sort_keys=False)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def write_status(path: str | Path, report: GapReport, **extra: Any) -> Path:
    """Write (REWRITE) the sidecar status file for a scanned log.

    The JSONL log is append-only and never touched; this sidecar is the one
    file that is legitimately *state*, because it is state **about** the log
    rather than another sample of it.  It exists so a hole is visible in the
    artifact: someone reading only ``signals/`` sees the gap count next to
    the data, without having to re-run the scan or trust stdout.

    Rewritten atomically (temp file + ``os.replace``) so a reader can never
    observe a half-written status.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": utc_now().isoformat(), **report.to_dict(), **extra}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Recording
# ─────────────────────────────────────────────────────────────────────────────


def book_to_record(
    book: Any,
    *,
    pair: str,
    count_requested: int,
    recorded_at: datetime,
    expected_interval_seconds: float = DEFAULT_EXPECTED_INTERVAL_SECONDS,
    gap_factor: float = DEFAULT_GAP_FACTOR,
    prev_recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """Turn one live :class:`kraken_api.models.OrderBook` into one record.

    The raw levels are stored verbatim as ``[price, volume, order_ts]`` — the
    exact shape Kraken sent.  Nothing is normalised, rounded away or reduced
    to a derived feature: the only numbers computed here are the top of book
    (which the funding sibling also records, so the two channels look alike)
    and the interval arithmetic that makes a hole visible.
    """
    bids = [[lvl.price, lvl.volume, int(lvl.timestamp)] for lvl in book.bids]
    asks = [[lvl.price, lvl.volume, int(lvl.timestamp)] for lvl in book.asks]

    n_bids, n_asks = len(bids), len(asks)
    # A requested depth the exchange did not honour is a SILENT depth
    # reduction (measured: count=1000 -> 100 levels, no error).  Recorded as
    # a flag rather than left for a reader to infer from the counts.
    truncated = bool(count_requested) and (n_bids < count_requested or n_asks < count_requested)
    if truncated:
        _LOGGER.warning(
            "Requested depth %s but Kraken returned %d bids / %d asks — the "
            "served depth is smaller than requested, so this record is NOT "
            "comparable with a --count=%s record",
            count_requested,
            n_bids,
            n_asks,
            count_requested,
        )

    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    spread = mid = None
    if best_bid is not None and best_ask is not None:
        try:
            bid_f, ask_f = float(best_bid), float(best_ask)
            spread = f"{ask_f - bid_f:.10f}".rstrip("0").rstrip(".") or "0"
            mid = f"{(ask_f + bid_f) / 2.0:.10f}".rstrip("0").rstrip(".") or "0"
        except (TypeError, ValueError):  # pragma: no cover - defensive
            _LOGGER.warning("Non-numeric top of book: bid=%r ask=%r", best_bid, best_ask)

    # The SAME keys in both branches, always.  An earlier revision emitted
    # `after`/`before` here for every record but the first, so a consumer of
    # this artifact had to know which branch produced it — an inconsistency
    # baked into a file whose whole point is to be read for months.
    if prev_recorded_at is None:
        interval = {
            "expected_seconds": expected_interval_seconds,
            "gap_factor": gap_factor,
            "prev_recorded_at": None,
            "delta_seconds": None,
            "factor": None,
            "status": STATUS_FIRST,
            "missing_snapshots": 0,
        }
    else:
        classified = classify_interval(
            prev_recorded_at,
            recorded_at,
            expected_interval_seconds=expected_interval_seconds,
            gap_factor=gap_factor,
        )
        interval = {
            "expected_seconds": classified.expected_seconds,
            "gap_factor": gap_factor,
            "prev_recorded_at": classified.after,
            "delta_seconds": round(classified.delta_seconds, 3),
            "factor": round(classified.factor, 3),
            "status": classified.status,
            "missing_snapshots": classified.missing_snapshots,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "pair": pair,
        # The recorder's own clock.  A book snapshot has no timestamp of its
        # own: every level's `ts` is that order's placement time.
        "recorded_at": recorded_at.isoformat(),
        # Hour-floored key, matching the funding sibling, so the reader can
        # resolve a same-hour re-fire without giving up sub-hour precision in
        # `recorded_at`.
        "hour": floor_hour(recorded_at).isoformat(),
        "depth": {
            "requested_count": count_requested,
            "bid_levels": n_bids,
            "ask_levels": n_asks,
            "levels_total": n_bids + n_asks,
            "truncated": truncated,
        },
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "mid": mid,
        "bids": bids,
        "asks": asks,
        # The expected-vs-actual interval this record closes.  THIS is the
        # hole being visible in the artifact rather than only in stdout.
        "interval": interval,
    }


def record_once(
    *,
    pair: str = "ETH/USD",
    count: int = DEFAULT_COUNT,
    output: str | Path,
    status_output: str | Path | None = None,
    expected_interval_seconds: float = DEFAULT_EXPECTED_INTERVAL_SECONDS,
    gap_factor: float = DEFAULT_GAP_FACTOR,
    manager: Any = None,
    recorded_at: datetime | None = None,
) -> tuple[dict[str, Any], GapReport]:
    """Take one snapshot, append it, then re-scan and refresh the sidecar.

    Order matters: the snapshot is appended **before** the scan, so the
    report and the status file describe the file as it now is, hole included.
    Scanning first would report a state that is one record stale.

    ``manager`` is injectable so tests can drive the whole path without a
    network call; ``recorded_at`` likewise, so a test can place two records
    either side of a deliberate hole.
    """
    if count <= 0:
        raise ValueError(f"count must be positive, got {count!r}")

    stamp = recorded_at if recorded_at is not None else utc_now()

    # The PREVIOUS record's stamp, read before appending — this is what makes
    # the new record's interval field meaningful rather than always FIRST.
    prior = read_recorded_at(output)
    prev_stamp = max(prior) if prior else None

    if manager is None:
        from kraken_api import KrakenManager

        # Keyless: /0/public/Depth needs no credential, so this must not
        # depend on one being configured.  `from_env` does not require one.
        manager = KrakenManager.from_env()

    book = manager.order_book(pair, count=count)
    record = book_to_record(
        book,
        pair=pair,
        count_requested=count,
        recorded_at=stamp,
        expected_interval_seconds=expected_interval_seconds,
        gap_factor=gap_factor,
        prev_recorded_at=prev_stamp,
    )
    append_record(output, record)

    report = scan_gap_file(
        output,
        expected_interval_seconds=expected_interval_seconds,
        gap_factor=gap_factor,
    )
    if status_output is not None:
        write_status(
            status_output,
            report,
            artifact=str(output),
            pair=pair,
            count_requested=count,
            source=SOURCE,
        )
    return record, report


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────


def format_report(
    report: GapReport,
    *,
    artifact: str | Path | None = None,
    status_file: str | Path | None = None,
    max_intervals: int = 12,
) -> str:
    """Human-readable expected-vs-actual report.  This is stdout, not the artifact."""
    lines: list[str] = []
    lines.append(f"DEPTH RECORDER GAP REPORT — {SOURCE}")
    if artifact is not None:
        lines.append(f"  artifact      : {artifact}")
    if status_file is not None:
        lines.append(f"  status sidecar: {status_file}")
    lines.append(
        f"  expectation   : every {report.expected_interval_seconds:.0f}s "
        f"(hole if actual > expected x {report.gap_factor:g})"
    )
    lines.append(
        f"  records       : {report.n_records}   intervals: {report.n_intervals}"
    )
    if report.n_seeded_excluded:
        lines.append(
            f"                 ({report.n_seeded_excluded} SEEDED record(s) excluded —"
            " real readings, not on the cadence; RUN-LOG 22.1/22.8)"
        )
    lines.append(
        f"  first         : {report.first_recorded_at or '(none)'}"
    )
    lines.append(
        f"  last          : {report.last_recorded_at or '(none)'}"
    )
    lines.append(
        f"  hours covered : {report.n_hours_covered} distinct hour(s) of"
        f" {report.hours_in_span} in span"
    )
    lines.append(
        f"  depth N       : {report.n_hours_covered} / {TARGET_DEPTH_HOURS} hours"
        f" ({report.depth_fraction * 100:.3f}%)  <- COUNTED IN HOURS, not rows"
    )
    coverage = report.coverage_ratio
    lines.append(
        f"  span          : {report.observed_span_seconds:.0f}s observed"
        + (
            f"   hours in span: {report.hours_in_span}"
            f"   coverage: {coverage * 100:.1f}%"
            if coverage is not None
            else ""
        )
    )
    # The threshold is printed, not just the count: a bare nonzero counter beside a
    # VERDICT line reads as an unexplained defect (or, worse, gets dismissed as one).
    # `SHORT` is an INTERVAL status — any consecutive pair closer together than
    # expected/gap_factor — and is NOT a count of duplicate records, nor anything to
    # do with book depth.  Those are three different things a reader confuses.
    short_threshold = report.expected_interval_seconds / report.gap_factor
    lines.append(
        f"  SHORT         : {report.n_short_intervals} interval(s) "
        f"closer than {short_threshold:.0f}s "
        f"(= {report.expected_interval_seconds:.0f}s expected / "
        f"{report.gap_factor:g} factor)"
    )
    lines.append(
        "                   a sub-hour re-fire, timer catch-up, or two manual pulls "
        "in quick succession;"
    )
    lines.append(
        "                   counted separately from gaps, and NEITHER a duplicate-record "
        "count NOR a depth shortfall"
    )
    if report.n_short_intervals >= 2:
        # ONE short interval is a legitimate, documented catch-up:
        # `Persistent=true` fires immediately when the timer is enabled and the
        # slot was missed, which on this very log produced 19:37:04 -> 19:42:32
        # (327 s apart) from a SINGLE timer.  A second timer does not produce one
        # short interval, it produces a SUSTAINED pattern — two records every
        # hour, ~1800 s apart, repeating — so the hint waits for repetition
        # rather than firing on the catch-up every host sees once at enablement.
        lines.append(
            f"                   >> {report.n_short_intervals} SHORT interval(s) is a"
            " PATTERN, not a catch-up. If these are not your own manual pulls,"
        )
        lines.append(
            "                      CHECK FOR A SECOND TIMER — `systemctl "
            "is-enabled kraken-trading-bot-order-book.timer`"
        )
        lines.append(
            "                      (system) vs `systemctl --user is-active "
            "kraken-trading-bot-order-book.timer` (user). Both fire hourly;"
        )
        lines.append(
            "                      two timers put two records in one hour, ~1800s apart.")
    elif report.n_short_intervals == 1:
        lines.append(
            "                   (one SHORT interval is the expected `Persistent=true`"
            " catch-up at enablement; see RUN-LOG 22.9)"
        )
    lines.append(
        f"  GAPS          : {report.n_gaps}   "
        f"missing snapshots: {report.n_missing_snapshots}   "
        f"longest hole: {report.longest_gap_seconds:.0f}s"
    )
    lines.append("")

    if report.n_records < 2:
        lines.append(
            "  No interval yet — one record has nothing to compare against. "
            "This is a first-run state, not a green verdict."
        )
        return "\n".join(lines)

    shown = report.intervals[-max_intervals:] if max_intervals > 0 else report.intervals
    if len(report.intervals) > len(shown):
        lines.append(f"  (last {len(shown)} of {len(report.intervals)} intervals)")
    # 33 is the width of an offset-aware ISO-8601 stamp to the microsecond
    # ("2026-10-03T19:10:45.752567+00:00").  Under-sizing it silently ran the
    # two stamps together, which is exactly the report a reader must be able
    # to check a hole against — so the width is derived, not guessed.
    stamp_w = max(33, *(len(iv.after) for iv in shown), *(len(iv.before) for iv in shown))
    lines.append("  EXPECTED-VS-ACTUAL INTERVALS")
    lines.append(
        f"  {'STATUS':<7} {'AFTER':<{stamp_w}} {'BEFORE':<{stamp_w}} "
        f"{'ACTUAL':>9} {'EXPECT':>8} {'x':>6} {'MISS':>5}"
    )
    for iv in shown:
        lines.append(
            f"  {iv.status:<7} {iv.after:<{stamp_w}} {iv.before:<{stamp_w}} "
            f"{iv.delta_seconds:>9.0f} {iv.expected_seconds:>8.0f} "
            f"{iv.factor:>6.2f} {iv.missing_snapshots:>5d}"
        )
    lines.append("")

    if report.holes:
        lines.append(f"  *** {report.n_gaps} HOLE(S) — DATA IS MISSING ***")
        for i, hole in enumerate(report.holes, start=1):
            lines.append(
                f"    hole {i}: recorder did not fire between "
                f"{hole.after} and {hole.before}"
            )
            lines.append(
                f"            actual {hole.delta_seconds:.0f}s vs expected "
                f"{hole.expected_seconds:.0f}s  (x{hole.factor:.2f})  "
                f"-> {hole.missing_snapshots} hourly snapshot(s) unrecoverable"
            )
        lines.append(
            "  Kraken's book has NO historical endpoint: these hours cannot be "
            "backfilled by anything."
        )
        lines.append("  VERDICT: RED")
    elif report.n_short_intervals >= 2:
        # A sustained SHORT pattern is a DEFECT, so it must not sit under a GREEN
        # verdict.  Measured: a synthetic double-timer log (12 records, 6 hours,
        # 1800s apart) reported `GAPS 0` and therefore "GREEN — no interval
        # exceeded the expectation", while the recorder was running at double
        # cadence.  Gaps measure LOSS; a double timer is the opposite fault and
        # the old verdict was blind to it.  The hours-based depth count already
        # resists the inflation (12 rows -> 6 hours), but the verdict has to say
        # so too, or a reader trusts GREEN.
        lines.append(
            f"  VERDICT: RED — {report.n_short_intervals} SHORT intervals is a"
            " sustained pattern, not loss."
        )
        lines.append(
            "  No hour is missing, but the recorder is not on its cadence: the"
            " likely cause is a SECOND TIMER."
        )
        lines.append(
            "  Two timers put two records in every hour and duplicate the depth"
            " series' cadence."
        )
    else:
        lines.append("  VERDICT: GREEN (no interval exceeded the expectation)")
    return "\n".join(lines)
