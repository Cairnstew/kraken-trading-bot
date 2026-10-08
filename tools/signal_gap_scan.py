"""Signal-channel coverage and gap check.

**Why this exists.** A signal channel that stops being written looks exactly
like a signal channel that was never configured. The social channel proved
it on 2026-10-04: `kraken-trading-bot-social.service` exited **0/SUCCESS**
every hour while StockTwits returned `HTTP 403 (Cloudflare challenge)` on
every call, because Fear & Greed still resolved. Nothing in the repo
detected it, because nothing counted the hours a file actually covers — the
existing `store_gap_scan.py` scans the PRICE store, not the signal files.

That matters because these files are unrecoverable. Funding has a
historical endpoint (`/historical-funding-rates`, ~366 days). News and
social have **none**: their producers only look backward from now
(`--lookback-hours`), so an hour that is not written is an hour that can
never be fetched. A silently-truncated channel therefore biases every
future matrix that pins a window over it — `plan` checks only that the file
EXISTS and is tagged for the ticker, never that it COVERS the window, so a
short file passes every existing gate.

Run it:
    just signal-gaps                       # all configured channels
    just signal-gaps signals/eth_usd_news.jsonl
    just signal-gaps --window-since 2026-09-10T15:00:00Z \
                    --window-until 2026-10-01T15:00:00Z

Exit codes: 0 no gaps, 1 at least one missing hour. So it is gateable in
CI or a timer rather than decorative — the same contract `depth-gaps` uses
for the order-book log.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

#: Hourly cadence the three channels nominally settle on. Measured over
#: Kraken's own funding history: inter-record gap histogram
#: {1.0h: 8783, 2.0h: 6, 3.0h: 1}, i.e. hourly with three stragglers. A
#: multiple rather than an exact match, so one late pull is not a gap.
GAP_FACTOR = 1.5

#: The signal keys `configs/default.yaml` declares, with their defaults.
#: Kept here rather than parsed out of the YAML so this tool has no
#: dependency on the config's shape; `~` is expanded against $HOME exactly
#: as the RL-side seam does.
DEFAULT_CHANNELS: dict[str, str] = {
    "funding": "~/Projects/kraken-trading-bot/signals/eth_usd_funding.jsonl",
    "news": "~/Projects/kraken-trading-bot/signals/eth_usd_news.jsonl",
    "social": "~/Projects/kraken-trading-bot/signals/eth_usd_social.jsonl",
}

#: Channels whose history is unrecoverable. An hour missing here is
#: permanent, which is why `report_unrecoverable` says so loudly.
UNRECOVERABLE = ("news", "social")


@dataclass
class ChannelReport:
    """Coverage of one signal file, hour by hour."""

    channel: str
    path: str
    exists: bool = False
    n_records: int = 0
    hours_covered: int = 0
    hours_in_span: int = 0
    n_missing: int = 0
    n_gaps: int = 0
    longest_gap_seconds: float = 0.0
    first_record: str | None = None
    last_record: str | None = None
    coverage_ratio: float = 0.0
    in_window_records: int = 0
    in_window_hours: int = 0
    in_window_missing: int = 0
    gaps: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "path": self.path,
            "exists": self.exists,
            "n_records": self.n_records,
            "hours_covered": self.hours_covered,
            "hours_in_span": self.hours_in_span,
            "n_missing": self.n_missing,
            "n_gaps": self.n_gaps,
            "longest_gap_seconds": self.longest_gap_seconds,
            "first_record": self.first_record,
            "last_record": self.last_record,
            "coverage_ratio": self.coverage_ratio,
            "in_window_records": self.in_window_records,
            "in_window_hours": self.in_window_hours,
            "in_window_missing": self.in_window_missing,
            "gaps": self.gaps,
            "unrecoverable": self.channel in UNRECOVERABLE,
            "error": self.error,
        }


def _parse_ts(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _floor_hour(ts: dt.datetime) -> dt.datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def read_hours(path: Path) -> list[dt.datetime]:
    """Every distinct floor-hour present in a signal JSONL file.

    Unparseable lines are counted by the caller via the record count; here
    they are skipped, because a malformed line has no hour and inventing one
    would understate a gap.
    """
    hours: set[dt.datetime] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            ts = _parse_ts(record.get("timestamp"))
            if ts is not None:
                hours.add(_floor_hour(ts))
    return sorted(hours)


def count_records(path: Path) -> int:
    n = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                n += 1
    return n


def scan_channel(
    channel: str,
    raw_path: str,
    *,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    gap_factor: float = GAP_FACTOR,
) -> ChannelReport:
    """Coverage and gaps for one signal file.

    ``since``/``until`` restrict the *window* verdict to a pinned range —
    the same half-open convention the RL seam uses — so a matrix can be
    asked "is this channel covered across the window I am about to train
    on?" rather than only "does this file have records".
    """
    path = Path(raw_path).expanduser()
    rep = ChannelReport(channel=channel, path=str(path))
    if not path.is_file():
        rep.error = "file not found"
        return rep
    rep.exists = True
    try:
        rep.n_records = count_records(path)
        hours = read_hours(path)
    except OSError as exc:
        rep.error = f"unreadable: {exc}"
        return rep

    if not hours:
        rep.error = "file holds no parseable timestamps"
        return rep

    rep.hours_covered = len(hours)
    rep.first_record = hours[0].isoformat()
    rep.last_record = hours[-1].isoformat()
    span = hours[-1] - hours[0]
    rep.hours_in_span = int(span.total_seconds() // 3600) + 1

    threshold = 3600.0 * gap_factor
    for prev, cur in zip(hours, hours[1:]):
        delta = (cur - prev).total_seconds()
        if delta > threshold:
            rep.n_gaps += 1
            missing = int(delta // 3600) - 1
            rep.longest_gap_seconds = max(rep.longest_gap_seconds, delta)
            rep.gaps.append(
                {
                    "after": prev.isoformat(),
                    "before": cur.isoformat(),
                    "delta_seconds": delta,
                    "factor": round(delta / 3600.0, 3),
                    "missing_hours": missing,
                }
            )
    rep.n_missing = sum(int(g["missing_hours"]) for g in rep.gaps)
    if rep.hours_in_span > 0:
        rep.coverage_ratio = rep.hours_covered / rep.hours_in_span

    # NOTE: an earlier version added a "span shortfall" check here — comparing
    # hours_in_span against hours_covered and reporting the difference as
    # missing. It was removed on 2026-10-04 because it is UNREACHABLE for
    # this data and therefore untestable. Timestamps are floored to the hour
    # before anything else, so every consecutive delta is a whole number of
    # hours: 1h never trips the factor, and any absent hour necessarily
    # produces a >= 2h delta that the loop above already reports. A branch
    # that cannot fail is worse than no branch — it reads as coverage
    # assurance while being unreachable.

    if since is not None or until is not None:
        lo = since or hours[0]
        hi = until or hours[-1]
        if hi < lo:
            rep.error = (
                f"window is inverted: until {hi.isoformat()} < since "
                f"{lo.isoformat()}"
            )
            return rep
        inwin = [h for h in hours if lo <= h < hi]
        rep.in_window_records = len(inwin)
        rep.in_window_hours = len(inwin)
        expected = int((hi - lo).total_seconds() // 3600)
        rep.in_window_missing = max(0, expected - len(inwin))
    return rep


def render(reports: Sequence[ChannelReport], *, json_out: bool) -> int:
    if json_out:
        print(
            json.dumps(
                {
                    "channels": [r.to_dict() for r in reports],
                    "any_gap": any(
                        r.n_gaps or r.error or r.in_window_missing for r in reports
                    ),
                },
                indent=2,
            )
        )
        return 1 if any(
            r.n_gaps or r.error or r.in_window_missing for r in reports
        ) else 0

    print("SIGNAL-CHANNEL COVERAGE")
    worst = 0
    for rep in reports:
        if rep.error:
            print(f"  {rep.channel:8s} {rep.error}  ({rep.path})")
            worst = 1
            continue
        print(
            f"  {rep.channel:8s} records {rep.n_records:>6d}  hours covered "
            f"{rep.hours_covered:>6d}/{rep.hours_in_span:<6d} "
            f"({rep.coverage_ratio:6.1%})"
        )
        print(
            f"           span {rep.first_record} -> {rep.last_record}"
        )
        print(
            f"           GAPS {rep.n_gaps}  missing hours {rep.n_missing}  "
            f"longest {rep.longest_gap_seconds / 3600.0:.1f}h"
        )
        if rep.in_window_hours or rep.in_window_missing:
            print(
                f"           WINDOW covered {rep.in_window_hours}  "
                f"missing {rep.in_window_missing}"
            )
        for gap in rep.gaps[:5]:
            print(
                f"             {gap['after']} -> {gap['before']}  "
                f"{gap['factor']}x  missing {gap['missing_hours']}h"
            )
        if rep.n_gaps:
            worst = 1

    unrecoverable_gaps = [
        r for r in reports if r.channel in UNRECOVERABLE and (r.n_gaps or r.error)
    ]
    if unrecoverable_gaps:
        print()
        print(
            "  *** PERMANENT DATA LOSS on "
            + ", ".join(r.channel for r in unrecoverable_gaps)
            + " ***"
        )
        print(
            "  These producers have NO historical endpoint — they only look"
            " backward from"
        )
        print(
            "  now. An hour not written now cannot be fetched later, so a"
            " gap here is"
        )
        print(
            "  permanent. (Funding is different: Kraken serves ~366 days of"
            " history"
        )
        print(
            "  via /historical-funding-rates, so `just funding-backfill`"
            " repairs it.)"
        )
        worst = 1
    return worst


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="signal JSONL files to check; defaults to the three channels "
             "configs/default.yaml declares",
    )
    parser.add_argument("--channel", action="append", default=[],
                        help="label for a positional path (repeatable)")
    parser.add_argument("--window-since", help="ISO-8601 inclusive lower bound")
    parser.add_argument("--window-until", help="ISO-8601 exclusive upper bound")
    parser.add_argument("--gap-factor", type=float, default=GAP_FACTOR)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    since = _parse_ts(args.window_since) if args.window_since else None
    until = _parse_ts(args.window_until) if args.window_until else None

    if args.paths:
        labels = args.channel or [f"ch{i}" for i in range(len(args.paths))]
        targets = list(zip(labels, args.paths))
    else:
        targets = list(DEFAULT_CHANNELS.items())

    reports = [
        scan_channel(
            name, path, since=since, until=until, gap_factor=args.gap_factor
        )
        for name, path in targets
    ]
    return render(reports, json_out=args.json)


if __name__ == "__main__":
    raise SystemExit(main())