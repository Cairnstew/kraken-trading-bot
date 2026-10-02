#!/usr/bin/env python3
"""Count and list the MISSING bars in a seeded market-data store.

Phase 6 finding F6. The store reader globs
``{PAIR_ID}/{interval_min}/{YYYY-MM}.parquet`` and concatenates, so a hole
in the series is invisible downstream: every consumer sees a shorter,
contiguous-looking frame. The seeded ETH/USD store measures
**157 missing hourly bars across 28 gaps**, largest single gap **39h**,
including a **38-bar hole at the seed/live-append seam** in the most
recent month.

That matters because **features are computed on bar counts, not
wall-clock**. ``return_1`` reports the return across whatever two rows
happen to be adjacent, so across a 39-hour jump it reports a 39-hour
return as though it were a 1-hour return, and ``start_index = 24``
consumes 24 *rows* that may span more than a day. No guard in this repo
can catch that: a z-scored 1-bar step looks exactly like a z-scored
39-bar step.

This tool **detects and labels only**. It does not repair, interpolate or
reindex anything, and it does not fail the run -- it reports. The real fix
is to make the feature windows **time-aware** -- compute them over a
reindexed, gap-filled bar grid so a window means N *hours* rather than N
*rows*. That item is not yet registered; see DECISION.md section 14 for
why it is deliberately NOT called CAND-3b (that label is already taken).

Usage::

    python tools/store_gap_scan.py --store ~/Projects/kraken-market-data/store \
        --ticker ETH/USD --interval 60
    python tools/store_gap_scan.py --store ROOT --pair-id ETH_USD \
        --interval-min 60

Exit codes: ``0`` the scan ran (gaps or no gaps -- this is a report, not
a gate), ``2`` the store or its months could not be read.

The ``just store-verify`` recipe runs this after the sibling seeder's own
``verify`` so the gap accounting is available in this repo, where it can
be tested, without modifying the sibling.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

#: The name given to a gap that straddles the boundary between two month
#: files. That boundary is where the Binance-archive **seed** ends and the
#: **live append** (this repo's own fetch -> upsert leg) begins, which is
#: exactly the region a live deployment trades. Measured on the seeded
#: ETH/USD store: 2026-08 ends 2026-08-31 23:00, 2026-09 starts
#: 2026-09-02 14:00, leaving 38 bars missing.
SEAM_GAP_NAME = "seed/live-append seam"

#: The recorded shape of the shipped store, quoted in the report so a
#: reader can tell "new problem" from "the known one". Phase 6, F6.
#:
#: Units are stated explicitly because this exact gap got quoted two ways
#: and read as a contradiction: the seam hole is **38 missing bars** but
#: spans **39 elapsed hours** (38 bars sit *between* the two surviving
#: bars, which are themselves 39h apart). Both are correct.
#:
#: The total drifts by ~1 bar per run and that is itself expected: the
#: live append leg (`data.py` fetch -> upsert) extends the series, and a
#: scan taken later legitimately reports one more bar and one more missing
#: bar than a scan taken earlier. Verified: re-scanning the reviewer's
#: store copy gives 28 gaps and the same 38-bar seam hole, with 158
#: missing against the 157 recorded at review time.
MEASURED_ETH_USD = {
    "bars": 76_563,
    "missing": 157,
    "gaps": 28,
    "max_gap_bars": 39,
    "seam_gap_bars": 38,
}


@dataclass(frozen=True)
class BarGap:
    """One run of absent bars.

    Attributes:
        after: The last bar that IS present, before the hole.
        before: The first bar that IS present, after the hole. Equal to
            ``after`` when the hole runs to the end of the series.
        missing: How many bars are absent. Note this is NOT the same
            number as the elapsed hours between ``after`` and ``before``:
            N missing bars means the two surviving bars are N+1 steps
            apart. Both are reported, because conflating them is how this
            gap got reported as both "39h" and "38 bars".
        name: ``SEAM_GAP_NAME`` when this is the **latest** hole to straddle
            a month-file boundary -- the seed/live-append seam -- else
            ``""``. An earlier month-boundary hole is an ordinary archive
            gap and is deliberately not labelled the seam.
        misaligned: True when ``before`` does not sit on the interval grid
            (e.g. ``09:27:14`` for hourly bars). The archive has at least
            one such bar; ``missing`` is then an under-count, so it is
            flagged rather than presented as exact.
    """

    after: pd.Timestamp
    before: pd.Timestamp
    missing: int
    name: str = ""
    misaligned: bool = False

    @property
    def is_seam(self) -> bool:
        """True for a gap at the seed/live-append seam, by name."""
        return self.name == SEAM_GAP_NAME

    @property
    def elapsed_hours(self) -> float:
        """Wall-clock hours between the two surviving bars."""
        return (self.before - self.after) / pd.Timedelta(hours=1)

    def describe(self) -> str:
        end = self.before if self.before == self.after else self.before - pd.Timedelta(1, "min")
        label = f"  <-- {self.name}" if self.name else ""
        flag = "  [!] boundary off-grid, count is a lower bound" if self.misaligned else ""
        return (
            f"    {self.missing:>4d} bar(s)  "
            f"{self.after.isoformat()} -> {end.isoformat()}  "
            f"({self.elapsed_hours:g}h elapsed){label}{flag}"
        )


@dataclass
class GapReport:
    """The result of scanning one (pair, interval) source.

    Attributes:
        n_bars: Rows actually present.
        first: Earliest bar, ``None`` when the source is empty.
        last: Latest bar, ``None`` when the source is empty.
        interval: Bar width in minutes.
        gaps: Every run of absent bars, largest first.
    """

    n_bars: int = 0
    first: pd.Timestamp | None = None
    last: pd.Timestamp | None = None
    interval: int = 60
    gaps: list[BarGap] = field(default_factory=list)

    @property
    def n_missing(self) -> int:
        """Total absent bars across every gap."""
        return sum(g.missing for g in self.gaps)

    @property
    def n_gaps(self) -> int:
        """Number of distinct holes."""
        return len(self.gaps)

    @property
    def max_gap(self) -> BarGap | None:
        """The largest single hole, or ``None`` when contiguous."""
        return max(self.gaps, key=lambda g: g.missing, default=None)

    @property
    def seam_gaps(self) -> list[BarGap]:
        """Every hole that sits at the seed/live-append seam, by name."""
        return [g for g in self.gaps if g.is_seam]

    @property
    def expected_bars(self) -> int:
        """How many bars a gap-free span of this length would hold."""
        if self.first is None or self.last is None:
            return 0
        span = int((self.last - self.first) / pd.Timedelta(minutes=self.interval))
        return span + 1

    def caveat(self) -> str:
        """The sentence every result computed across this source carries.

        Names the measured figures explicitly so a reader cannot take a
        store-arm number as a clean one without seeing what is missing.
        """
        if not self.gaps:
            return (
                "Caveat: this source is contiguous, so bar-count windows and "
                "wall-clock windows agree here."
            )
        seam = self.seam_gaps
        parts = [
            f"{self.n_missing} missing bar(s) across {self.n_gaps} gap(s) "
            f"(largest {max(g.missing for g in self.gaps)} bars)"
        ]
        if seam:
            parts.append(
                "a "
                + ", ".join(f"{g.missing}-bar hole at the {g.name}" for g in seam)
            )
        return (
            "CAVEAT: this result was computed across a source with "
            + "; ".join(parts)
            + ". Features here are computed on BAR COUNTS, not wall-clock, so "
            "a gap of N bars is z-scored as a 1-bar step and the 24-bar "
            "warm-up may span more than a day. The real fix is time-aware "
            "feature windows over a reindexed bar grid (DECISION.md 14)."
        )


def _read_month(path: Path) -> pd.Series:
    """Return one month file's bar timestamps as a sorted UTC Series.

    The store writes a ``time`` column of **epoch seconds** (``int64``),
    verified against a real seeded month -- so a naive
    ``pd.to_datetime(..., utc=True)`` silently yields 1970 and every gap
    then reads as zero. Both that and a genuine datetime column (and a
    datetime index) are accepted.
    """
    frame = pd.read_parquet(path)
    if isinstance(frame.index, pd.DatetimeIndex):
        stamps = pd.Series(frame.index, index=frame.index)
    elif "time" in frame.columns:
        raw = frame["time"]
        if pd.api.types.is_numeric_dtype(raw):
            # Epoch SECONDS (the store's own format), not nanoseconds.
            stamps = pd.Series(pd.to_datetime(raw.astype("int64"), unit="s", utc=True))
        else:
            stamps = pd.Series(pd.to_datetime(raw, utc=True))
    else:  # pragma: no cover - defensive: a store month always has one
        raise ValueError(f"{path} has neither a DatetimeIndex nor a 'time' column")
    stamps = pd.to_datetime(stamps, utc=True)
    return stamps.sort_values()


def month_files(store_root: Path, pair_id: str, interval_min: int) -> list[Path]:
    """Every ``{YYYY-MM}.parquet`` for one source, oldest first."""
    directory = Path(store_root) / pair_id / str(interval_min)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.parquet"))


def find_gaps(
    stamps: Sequence[pd.Timestamp], interval: int = 60
) -> list[BarGap]:
    """Every run of absent bars in a sorted, de-duplicated stamp sequence.

    Args:
        stamps: Bar timestamps, any order; duplicates are collapsed.
        interval: Bar width in minutes.

    Returns:
        Gaps largest first. An empty list means the source is contiguous.
    """
    if len(stamps) == 0:
        return []
    index = pd.DatetimeIndex(sorted(set(pd.to_datetime(list(stamps), utc=True))))
    if len(index) < 2:
        return []
    step = pd.Timedelta(minutes=interval)
    epoch = pd.Timestamp(0, tz="UTC")
    deltas = index[1:] - index[:-1]
    # A bar is absent when consecutive stamps are more than one step apart.
    # `n_missing` is the number of whole steps in between, minus one.
    breaks = deltas > step
    gaps: list[BarGap] = []
    boundary_crossers: list[int] = []
    for i in np_where(breaks):
        after = index[i]
        before = index[i + 1]
        missing = int(deltas[i] / step) - 1
        # A hole straddling a month-file boundary is a candidate for the
        # seed/live-append seam; the LATEST such hole is the seam proper.
        # Month boundaries matter because the store is month-sliced: the
        # archive seed writes whole months and this repo's live append leg
        # adds at the very end of the series, so the newest boundary is
        # where seed hands over to live. Any *earlier* boundary hole is an
        # ordinary archive gap and must not be labelled the seam.
        crosses_month = after.month != before.month
        if crosses_month:
            boundary_crossers.append(len(gaps))
        # A surviving bar off the interval grid makes `missing` a truncation
        # of a fractional step, i.e. a lower bound. Flag it rather than
        # report an exact-looking number we do not have.
        misaligned = bool((before - epoch) % step)
        gaps.append(
            BarGap(
                after=after,
                before=before,
                missing=missing,
                name="",
                misaligned=misaligned,
            )
        )
    if boundary_crossers:
        # `after` increases with insertion order, so the last crosser is the
        # newest month boundary present.
        seam = gaps[boundary_crossers[-1]]
        gaps[boundary_crossers[-1]] = BarGap(
            after=seam.after,
            before=seam.before,
            missing=seam.missing,
            name=SEAM_GAP_NAME,
            misaligned=seam.misaligned,
        )
    gaps.sort(key=lambda g: g.missing, reverse=True)
    return gaps


def np_where(mask: Iterable[bool]) -> list[int]:
    """Indices where ``mask`` is true (a tiny local helper, no numpy import)."""
    return [i for i, flag in enumerate(mask) if flag]


def scan(
    store_root: Path, pair_id: str, interval_min: int = 60
) -> GapReport:
    """Scan one source and return its :class:`GapReport`.

    Args:
        store_root: The store root directory.
        pair_id: Kraken-style pair id, e.g. ``"ETH_USD"``.
        interval_min: Bar width in minutes, e.g. ``60``.

    Raises:
        FileNotFoundError: If the source directory holds no month files.
    """
    files = month_files(Path(store_root), pair_id, interval_min)
    if not files:
        raise FileNotFoundError(
            f"no parquet months at {Path(store_root) / pair_id / str(interval_min)} "
            f"-- is the store seeded? (`just store-plan` then `just store-seed`)"
        )
    stamps: list[pd.Timestamp] = []
    for path in files:
        stamps.extend(_read_month(path).tolist())
    stamps = sorted(set(stamps))
    return GapReport(
        n_bars=len(stamps),
        first=stamps[0],
        last=stamps[-1],
        interval=interval_min,
        gaps=find_gaps(stamps, interval_min),
    )


def format_report(report: GapReport, pair_id: str, show: int = 10) -> str:
    """A human-readable report that **names** any seed/live-append seam gap."""
    lines = [
        f"gap scan: {pair_id} @ {report.interval}m",
        f"  bars present   {report.n_bars}",
    ]
    if report.first is not None:
        lines.append(
            f"  span           {report.first.isoformat()} -> {report.last.isoformat()}"
        )
        lines.append(f"  span hours     {report.expected_bars - 1}")
    if not report.gaps:
        lines.append("  gaps           0 -- source is contiguous")
    else:
        lines.append(
            f"  MISSING        {report.n_missing} bar(s) across "
            f"{report.n_gaps} gap(s); largest {report.max_gap.missing}"
        )
        for gap in report.gaps[:show]:
            lines.append(gap.describe())
        if len(report.gaps) > show:
            lines.append(f"    ... and {len(report.gaps) - show} more gap(s)")
    for gap in report.seam_gaps:
        lines.append(
            f"  ** {gap.missing}-bar hole at the {gap.name} ** "
            f"({gap.after.isoformat()} -> {(gap.before - pd.Timedelta(1, 'min')).isoformat()})"
            f" -- the boundary where the archive seed ends and this repo's"
            f" live append leg takes over, i.e. the region a live deployment"
            f" trades."
        )
    lines.append("")
    lines.append("  " + report.caveat())
    lines.append(
        "  (recorded at Phase 6 review of the shipped ETH/USD store: "
        f"{MEASURED_ETH_USD['missing']} missing bars across "
        f"{MEASURED_ETH_USD['gaps']} gaps, largest gap spanning "
        f"{MEASURED_ETH_USD['max_gap_bars']}h / "
        f"{MEASURED_ETH_USD['seam_gap_bars']} missing bars at the "
        f"{SEAM_GAP_NAME}. The total drifts by ~1 bar per run as the live "
        f"append leg extends the series, so a later scan legitimately "
        f"reports one more bar and one more missing bar.)"
    )
    return "\n".join(lines)


def _pair_id(ticker: str) -> str:
    """``"ETH/USD"`` -> ``"ETH_USD"``, matching the store's directory name."""
    return ticker.replace("/", "_").replace("-", "_").upper()


def main(argv: Sequence[str] | None = None) -> int:
    # parse_known_args, not parse_args: `just store-verify` forwards the SAME
    # argument list to the sibling seeder's `verify`, which also accepts
    # `--since`/`--until`. Tolerating (and ignoring) the flags this tool has
    # no opinion about is what lets one recipe drive both tools.
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--store", required=True, help="store root directory")
    parser.add_argument("--ticker", help='Kraken pair, e.g. "ETH/USD"')
    parser.add_argument("--pair-id", help="store directory id, e.g. ETH_USD")
    parser.add_argument(
        "--interval", "--interval-min", dest="interval_min", type=int, default=60
    )
    parser.add_argument(
        "--show", type=int, default=10, help="how many gaps to list (default 10)"
    )
    args, ignored = parser.parse_known_args(argv)
    if ignored:
        print(
            f"store-gap-scan: ignoring flags meant for the seeder's own "
            f"verify: {' '.join(ignored)}",
            file=sys.stderr,
        )

    pair_id = args.pair_id or (_pair_id(args.ticker) if args.ticker else None)
    if not pair_id:
        parser.error("one of --ticker or --pair-id is required")
    try:
        report = scan(Path(args.store), pair_id, args.interval_min)
    except FileNotFoundError as exc:
        print(f"store-gap-scan: {exc}", file=sys.stderr)
        return 2
    print(format_report(report, pair_id, show=args.show))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
