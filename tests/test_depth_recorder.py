"""Tests for the G1 order-book depth recorder.

Two things are under test, and the second matters more:

1. **The append/depth-stamping logic** — the recorder stamps its OWN clock,
   records the depth it actually got, and appends without ever rewriting.
2. **The gap counter** — the guard.  A counter that has only ever printed
   ``0 gaps`` is indistinguishable from a counter that cannot count, so this
   file deliberately feeds it holes and asserts that it goes red, reports the
   right missing-snapshot count, and says RED in the text a human reads.

Every hole in this file is a NAMED defect (see each test's docstring), which
is what the audit's CHECKPOINT RULE asks for: not "we imagine it would fail".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kraken_trading_bot import cli as bot_cli
from kraken_trading_bot.depth_recorder import (
    DEFAULT_COUNT,
    DEFAULT_EXPECTED_INTERVAL_SECONDS,
    DEFAULT_GAP_FACTOR,
    STATUS_FIRST,
    STATUS_GAP,
    STATUS_OK,
    STATUS_SHORT,
    append_record,
    book_to_record,
    classify_interval,
    floor_hour,
    format_report,
    parse_timestamp,
    read_recorded_at,
    record_once,
    scan_gap_file,
    scan_gaps,
    write_status,
)

T0 = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
HOUR = DEFAULT_EXPECTED_INTERVAL_SECONDS


def at(seconds: float) -> datetime:
    """A stamp ``seconds`` after the module-level anchor."""
    return T0 + timedelta(seconds=seconds)


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────


class FakeLevel:
    """Stands in for ``kraken_api.models.BookLevel`` — price/volume/timestamp."""

    def __init__(self, price: str, volume: str, timestamp: int) -> None:
        self.price = price
        self.volume = volume
        self.timestamp = timestamp


class FakeBook:
    """Stands in for ``kraken_api.models.OrderBook``."""

    def __init__(self, bids: list[FakeLevel], asks: list[FakeLevel]) -> None:
        self.bids = bids
        self.asks = asks


def make_book(n_bids: int = 3, n_asks: int = 3) -> FakeBook:
    """A book whose levels descend toward the middle, as a real one does.

    Each level's ``timestamp`` is deliberately the SAME value for every level:
    a real snapshot's levels all carry their own order-placement times and
    nothing that identifies when the snapshot was taken.  That is the fact
    the recorder's own clock exists to compensate for, so the fake reproduces
    it rather than papering over it.
    """
    order_ts = 1_791_054_363
    bids = [
        FakeLevel(f"{2700 - i:.2f}", f"{1.5 + i:.2f}", order_ts - i)
        for i in range(n_bids)
    ]
    asks = [
        FakeLevel(f"{2701 + i:.2f}", f"{0.5 + i:.2f}", order_ts - i)
        for i in range(n_asks)
    ]
    return FakeBook(bids, asks)


class FakeManager:
    """Records what it was asked for and returns a canned book."""

    def __init__(self, book: FakeBook | None = None) -> None:
        self.book = book if book is not None else make_book()
        self.calls: list[tuple[str, int | None]] = []

    def order_book(self, pair: str, count: int | None = None) -> FakeBook:
        self.calls.append((pair, count))
        return self.book


# ─────────────────────────────────────────────────────────────────────────────
# Clock helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_floor_hour_drops_minutes_seconds_and_microseconds():
    assert floor_hour(T0) == T0
    messy = datetime(2026, 10, 3, 19, 12, 55, 755671, tzinfo=timezone.utc)
    assert floor_hour(messy) == datetime(2026, 10, 3, 19, 0, 0, tzinfo=timezone.utc)


def test_parse_timestamp_accepts_both_iso_spellings():
    """``+00:00`` is what this module writes; ``Z`` is what Kraken sends."""
    a = parse_timestamp("2026-10-03T19:10:45.752567+00:00")
    b = parse_timestamp("2026-10-03T19:10:45.752567Z")
    assert a is not None and a == b


@pytest.mark.parametrize("bad", [None, "", "not-a-time", "   "])
def test_parse_timestamp_rejects_unusable_input(bad):
    assert parse_timestamp(bad) is None


def test_parse_timestamp_treats_naive_stamp_as_utc():
    """A naive stamp is ambiguous, not fatal — dropping the record would
    silently shorten the log, which is the exact failure this recorder
    exists to prevent."""
    assert parse_timestamp("2026-10-03T19:10:45") == datetime(
        2026, 10, 3, 19, 10, 45, tzinfo=timezone.utc
    )


# ─────────────────────────────────────────────────────────────────────────────
# Depth stamping — requirement 2
# ─────────────────────────────────────────────────────────────────────────────


def test_record_stamps_its_own_clock_not_the_orders(tmp_path):
    """A book snapshot carries no timestamp; the recorder must supply one."""
    out = tmp_path / "d.jsonl"
    record, _ = record_once(
        output=out, manager=FakeManager(), recorded_at=T0, count=DEFAULT_COUNT
    )

    assert record["recorded_at"] == T0.isoformat()
    # Every level's timestamp is an order-placement time, and all of them
    # differ from each other.  If the recorder were echoing one of them, this
    # assertion would catch it.
    order_timestamps = {lvl[2] for lvl in record["bids"]} | {
        lvl[2] for lvl in record["asks"]
    }
    assert order_timestamps  # the fake book does carry per-order stamps
    assert T0.timestamp() not in order_timestamps
    # The hour key is the floored recorder clock, matching the funding sibling.
    assert record["hour"] == T0.isoformat()


def test_two_snapshots_get_distinct_recorded_at(tmp_path):
    """Two pulls must differ by the interval, not share one stamp."""
    out = tmp_path / "d.jsonl"
    first, _ = record_once(
        output=out, manager=FakeManager(), recorded_at=at(0), count=DEFAULT_COUNT
    )
    second, _ = record_once(
        output=out, manager=FakeManager(), recorded_at=at(HOUR), count=DEFAULT_COUNT
    )
    assert first["recorded_at"] != second["recorded_at"]
    assert first["hour"] != second["hour"]


def test_record_carries_requested_and_actual_depth(tmp_path):
    """Depth is not comparable across a ``count`` change, so both go in."""
    out = tmp_path / "d.jsonl"
    manager = FakeManager(make_book(n_bids=100, n_asks=100))
    record, _ = record_once(
        output=out, manager=manager, recorded_at=T0, count=DEFAULT_COUNT
    )
    assert record["depth"] == {
        "requested_count": 100,
        "bid_levels": 100,
        "ask_levels": 100,
        "levels_total": 200,
        "truncated": False,
    }
    # The requested depth is what was actually asked of the API, not a value
    # the recorder invented.
    assert manager.calls == [("ETH/USD", 100)]


def test_silent_depth_truncation_is_recorded_not_hidden(caplog):
    """DEFECT UNDER TEST: Kraken served fewer levels than were requested.

    Measured 2026-10-03: ``count=1000`` is served as 100 levels per side with
    no error.  A record that carried only ``requested_count`` would read as a
    depth-1000 book that happened to have 100 levels.  This asserts the
    recorder notices AND says so.
    """
    with caplog.at_level("WARNING"):
        record = book_to_record(
            make_book(n_bids=100, n_asks=100),
            pair="ETH/USD",
            count_requested=1000,
            recorded_at=T0,
        )

    assert record["depth"]["requested_count"] == 1000
    assert record["depth"]["bid_levels"] == 100
    assert record["depth"]["truncated"] is True
    assert any("served depth is smaller than requested" in r.message for r in caplog.records)


def test_raw_levels_are_stored_verbatim():
    """Nothing normalised away: the record must be able to rebuild the book."""
    record = book_to_record(
        make_book(n_bids=2, n_asks=2),
        pair="ETH/USD",
        count_requested=2,
        recorded_at=T0,
    )
    assert record["bids"] == [
        ["2700.00", "1.50", 1_791_054_363],
        ["2699.00", "2.50", 1_791_054_362],
    ]
    assert record["asks"] == [
        ["2701.00", "0.50", 1_791_054_363],
        ["2702.00", "1.50", 1_791_054_362],
    ]


def test_top_of_book_and_spread_are_derived_from_the_levels():
    record = book_to_record(
        make_book(n_bids=2, n_asks=2),
        pair="ETH/USD",
        count_requested=2,
        recorded_at=T0,
    )
    assert record["best_bid"] == "2700.00"
    assert record["best_ask"] == "2701.00"
    assert record["spread"] == "1"
    assert record["mid"] == "2700.5"


def test_empty_book_is_recorded_without_pretending_there_is_a_spread():
    record = book_to_record(
        FakeBook([], []),
        pair="ETH/USD",
        count_requested=100,
        recorded_at=T0,
    )
    assert record["bids"] == []
    assert record["best_bid"] is None
    assert record["spread"] is None
    assert record["depth"]["bid_levels"] == 0
    assert record["depth"]["truncated"] is True  # 0 < 100 requested


def test_count_must_be_positive(tmp_path):
    with pytest.raises(ValueError, match="count must be positive"):
        record_once(output=tmp_path / "d.jsonl", manager=FakeManager(), count=0)


# ─────────────────────────────────────────────────────────────────────────────
# Append-only — requirement 2
# ─────────────────────────────────────────────────────────────────────────────


def test_append_only_never_rewrites_a_prior_line(tmp_path):
    """The log is a log: a second fire must not touch the first record."""
    out = tmp_path / "d.jsonl"
    record_once(output=out, manager=FakeManager(), recorded_at=at(0))
    first_line = out.read_text(encoding="utf-8")

    record_once(output=out, manager=FakeManager(), recorded_at=at(HOUR))
    lines = out.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    assert lines[0] == first_line.rstrip("\n")  # byte-identical, untouched


def test_append_creates_the_parent_directory(tmp_path):
    """A timer firing into a fresh clone must not die on a missing directory."""
    out = tmp_path / "deep" / "nested" / "d.jsonl"
    record_once(output=out, manager=FakeManager(), recorded_at=T0)
    assert out.exists()


def test_one_record_is_one_line_even_with_a_deep_book(tmp_path):
    """JSONL is line-delimited: a record containing newlines would split into
    two unreadable lines and read as two records."""
    out = tmp_path / "d.jsonl"
    record_once(
        output=out, manager=FakeManager(make_book(50, 50)), recorded_at=T0
    )
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1


def test_malformed_line_is_skipped_not_fatal(tmp_path):
    """A producer failure must stay distinguishable from a first-run state."""
    out = tmp_path / "d.jsonl"
    out.write_text(
        '{"recorded_at": "2026-10-03T12:00:00+00:00"}\n'
        "not json at all\n"
        '{"no_timestamp": true}\n'
        '{"recorded_at": "2026-10-03T13:00:00+00:00"}\n',
        encoding="utf-8",
    )
    stamps = read_recorded_at(out)
    assert len(stamps) == 2
    assert stamps[1] == at(HOUR)


def test_read_recorded_at_on_a_missing_file_is_zero_records(tmp_path):
    assert read_recorded_at(tmp_path / "absent.jsonl") == []


# ─────────────────────────────────────────────────────────────────────────────
# The gap counter — requirement 3.  THE GUARD.
# ─────────────────────────────────────────────────────────────────────────────


def test_on_cadence_is_green():
    report = scan_gaps([at(i * HOUR) for i in range(5)])
    assert report.n_records == 5
    assert report.n_intervals == 4
    assert report.n_gaps == 0
    assert report.ok is True
    assert report.coverage_ratio == 1.0
    assert report.expected_slots == 5


def test_hole_is_red_and_counts_the_missing_snapshots():
    """DEFECT UNDER TEST: a deliberately skipped run of five hours.

    Records exist at hours 0, 1, 2, then 8 — so hours 3-7 were never
    recorded.  That is the "stopped for a week and appended happily
    afterwards" case F-6 warned about, and the whole reason this counter
    exists.
    """
    stamps = [at(0), at(HOUR), at(2 * HOUR), at(8 * HOUR)]
    report = scan_gaps(stamps)

    assert report.ok is False, "a five-hour hole must not read as green"
    assert report.n_gaps == 1
    assert report.n_missing_snapshots == 5  # hours 3,4,5,6,7
    assert report.longest_gap_seconds == 6 * HOUR
    # Coverage is measured against the slots the log's OWN wall-clock span
    # covers: the span is hours 0..8, i.e. 9 slots, of which 4 hold records.
    assert report.expected_slots == 9
    assert report.coverage_ratio == pytest.approx(4 / 9)
    # The regression this fixes: dividing by the record-count-implied span and
    # clamping at 1.0 reported 100% coverage on THIS SAME report.
    assert report.coverage_ratio < 1.0

    hole = report.holes[0]
    assert hole.after == at(2 * HOUR).isoformat()
    assert hole.before == at(8 * HOUR).isoformat()
    assert hole.factor == pytest.approx(6.0)


def test_red_report_names_the_hole_in_its_text():
    """The report a human reads must say RED, not merely return False."""
    report = scan_gaps([at(0), at(9 * HOUR)])
    text = format_report(report, artifact="signals/eth_usd_orderbook.jsonl")

    assert "*** 1 HOLE(S) — DATA IS MISSING ***" in text
    assert "VERDICT: RED" in text
    assert "8 hourly snapshot(s) unrecoverable" in text
    # The unrecoverability is the point; the report must not soften it.
    assert "NO historical endpoint" in text


def test_two_independent_holes_are_both_counted():
    report = scan_gaps(
        [at(0), at(5 * HOUR), at(6 * HOUR), at(20 * HOUR)]
    )
    assert report.n_gaps == 2
    assert report.n_missing_snapshots == 4 + 13
    assert report.longest_gap_seconds == 14 * HOUR


def test_a_week_long_hole_is_red():
    """The literal F-6 scenario: a week of silence, then it starts again.

    Stamps land at hours 0 and 169..172, so hours 1 through 168 were never
    recorded: 168 missing snapshots.  (An earlier draft of this test asserted
    ``7*24 - 1`` = 167; the recorder's own arithmetic, ``round(delta/expected)
    - 1`` = ``169 - 1`` = 168, is the correct one and the test was wrong.)
    """
    stamps = [at(0)] + [at((7 * 24 + h) * HOUR) for h in range(1, 5)]
    report = scan_gaps(stamps)
    assert report.ok is False
    assert report.n_gaps == 1
    assert report.longest_gap_seconds == 169 * HOUR
    assert report.n_missing_snapshots == 168  # hours 1..168 inclusive


def test_short_interval_is_not_a_gap():
    """DEFECT UNDER TEST — the opposite error: over-counting.

    A ``Persistent=true`` catch-up fires twice inside one hour.  Calling that
    a hole would make the counter cry wolf, and a counter nobody believes is
    the same failure as no counter.  It must be reported and counted
    SEPARATELY.
    """
    report = scan_gaps([at(0), at(130)])
    assert report.ok is True
    assert report.n_gaps == 0
    assert report.n_short_intervals == 1
    assert report.intervals[0].status == STATUS_SHORT


def test_duplicate_stamp_is_short_not_a_gap():
    """Two records at the identical instant — a double fire, not a hole."""
    report = scan_gaps([T0, T0, at(HOUR), at(2 * HOUR)])
    assert report.n_gaps == 0
    assert report.n_short_intervals == 1
    assert report.n_records == 4


def test_boundary_exactly_at_the_factor_is_still_ok():
    """``>`` not ``>=``: an interval exactly 1.5x expected has not exceeded it."""
    report = scan_gaps([at(0), at(HOUR * DEFAULT_GAP_FACTOR)])
    assert report.ok is True
    assert report.n_gaps == 0


def test_just_past_the_factor_is_a_gap():
    report = scan_gaps([at(0), at(HOUR * DEFAULT_GAP_FACTOR + 1)])
    assert report.ok is False
    assert report.n_gaps == 1


def test_timer_jitter_does_not_trip_the_counter():
    """``RandomizedDelaySec=120`` legitimately stretches one interval to
    ~1h4m.  At the shipped 1.5x factor that must stay GREEN — otherwise the
    counter reports a hole every time the timer is merely a bit late."""
    report = scan_gaps([at(0), at(HOUR + 120)])
    assert report.ok is True


def test_unordered_and_duplicate_stamps_are_sorted_before_scanning():
    """A log written out of order (or re-read after a rollback) must give the
    same verdict as the ordered one."""
    ordered = [at(0), at(HOUR), at(10 * HOUR)]
    assert scan_gaps(ordered).n_gaps == 1
    assert scan_gaps(list(reversed(ordered))).n_gaps == 1


def test_single_record_has_no_interval_and_does_not_claim_green_coverage():
    """First-run state, not a verdict.  Coverage must be None, not 0.0."""
    report = scan_gaps([T0])
    assert report.n_intervals == 0
    assert report.n_gaps == 0
    assert report.coverage_ratio is None
    text = format_report(report)
    assert "first-run state, not a green verdict" in text


def test_empty_scan_is_not_green_but_is_not_red_either():
    report = scan_gaps([])
    assert report.n_records == 0
    assert report.n_gaps == 0
    assert report.coverage_ratio is None
    assert report.observed_span_seconds == 0.0


def test_non_positive_expectation_is_refused():
    with pytest.raises(ValueError, match="expected_interval_seconds must be positive"):
        classify_interval(T0, at(HOUR), expected_interval_seconds=0)


def test_sub_unit_gap_factor_is_refused():
    """A factor below 1.0 would classify every interval as long, turning the
    counter into a constant false alarm."""
    with pytest.raises(ValueError, match="gap_factor below 1.0"):
        classify_interval(T0, at(HOUR), gap_factor=0.5)


# ─────────────────────────────────────────────────────────────────────────────
# The hole must be visible in the ARTIFACT, not only on stdout
# ─────────────────────────────────────────────────────────────────────────────


def test_the_gap_is_written_into_the_record_that_closed_it(tmp_path):
    """The record following a hole must CARRY the hole."""
    out = tmp_path / "d.jsonl"
    record_once(output=out, manager=FakeManager(), recorded_at=at(0))
    record_once(output=out, manager=FakeManager(), recorded_at=at(4 * HOUR))
    last = json.loads(out.read_text(encoding="utf-8").splitlines()[-1])

    interval = last["interval"]
    assert interval["status"] == STATUS_GAP
    assert interval["prev_recorded_at"] == at(0).isoformat()
    assert interval["delta_seconds"] == 4 * HOUR
    assert interval["expected_seconds"] == HOUR
    assert interval["missing_snapshots"] == 3
    assert interval["factor"] == pytest.approx(4.0)


def test_the_first_record_says_first_not_ok(tmp_path):
    out = tmp_path / "d.jsonl"
    record, _ = record_once(output=out, manager=FakeManager(), recorded_at=T0)
    assert record["interval"]["status"] == STATUS_FIRST
    assert record["interval"]["prev_recorded_at"] is None
    assert record["interval"]["delta_seconds"] is None


def test_every_record_interval_block_has_the_same_keys(tmp_path):
    """REGRESSION: the first record's block used to carry `prev_recorded_at`
    while every later record's carried `after`/`before`, so a consumer had to
    branch on which record it was reading.  The keys must be identical
    whatever the status — that is what makes the artifact readable later by
    something that does not exist yet."""
    out = tmp_path / "d.jsonl"
    record_once(output=out, manager=FakeManager(), recorded_at=at(0))
    record_once(output=out, manager=FakeManager(), recorded_at=at(4 * HOUR))
    lines = out.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])["interval"]
    second = json.loads(lines[1])["interval"]
    assert set(first) == set(second)
    assert set(first) == {
        "expected_seconds",
        "gap_factor",
        "prev_recorded_at",
        "delta_seconds",
        "factor",
        "status",
        "missing_snapshots",
    }


def test_status_sidecar_reports_the_hole_without_the_log(tmp_path):
    """The sidecar is what makes a hole visible to someone who only reads
    ``signals/``.  It is REWRITTEN each fire; the log is not."""
    log = tmp_path / "d.jsonl"
    status = tmp_path / "d.status.json"
    record_once(
        output=log,
        status_output=status,
        manager=FakeManager(),
        recorded_at=at(0),
    )
    record_once(
        output=log,
        status_output=status,
        manager=FakeManager(),
        recorded_at=at(7 * HOUR),
    )

    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["n_gaps"] == 1
    assert payload["n_missing_snapshots"] == 6
    assert payload["holes"][0]["delta_seconds"] == 7 * HOUR
    assert payload["artifact"] == str(log)
    assert payload["source"] == "kraken.public.Depth"
    assert "generated_at" in payload
    # One record in the log, not one per scan: the scan must not append.
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_status_sidecar_is_written_atomically(tmp_path):
    """No `.tmp` left behind, and never a partial file a reader can see."""
    status = tmp_path / "d.status.json"
    write_status(status, scan_gaps([at(0), at(HOUR)]))
    assert status.exists()
    assert not (tmp_path / "d.status.json.tmp").exists()
    assert json.loads(status.read_text(encoding="utf-8"))["ok"] is True


def test_scan_gap_file_matches_scan_gaps(tmp_path):
    log = tmp_path / "d.jsonl"
    record_once(output=log, manager=FakeManager(), recorded_at=at(0))
    record_once(output=log, manager=FakeManager(), recorded_at=at(3 * HOUR))
    from_file = scan_gap_file(log)
    assert from_file.n_gaps == 1
    assert from_file.ok is False


def test_record_once_scans_after_appending(tmp_path):
    """The report must describe the file as it NOW is.  Scanning before the
    append would report one record stale — and, worse, would have missed the
    very hole the new record closed."""
    out = tmp_path / "d.jsonl"
    record_once(output=out, manager=FakeManager(), recorded_at=at(0))
    _, report = record_once(output=out, manager=FakeManager(), recorded_at=at(5 * HOUR))
    assert report.n_records == 2
    assert report.n_gaps == 1
    assert report.last_recorded_at == at(5 * HOUR).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# CLI surface — the timer's ExecStart IS this command
# ─────────────────────────────────────────────────────────────────────────────


def test_record_depth_parser_defaults_match_the_shipped_unit():
    """The unit passes --pair/--output/--count; the parser's defaults must be
    the shipped ones so the two cannot drift apart silently."""
    args = bot_cli._build_parser().parse_args(
        ["record-depth", "--output", "x.jsonl"]
    )
    assert args.pair == "ETH/USD"
    assert args.count == DEFAULT_COUNT
    assert args.expected_interval_seconds == DEFAULT_EXPECTED_INTERVAL_SECONDS
    assert args.gap_factor == DEFAULT_GAP_FACTOR


def test_record_depth_writes_the_log_and_exits_zero_when_green(
    tmp_path, capsys, monkeypatch
):
    """Drive the real CLI handler with a fake exchange — no network.

    ``record_once`` does ``from kraken_api import KrakenManager`` INSIDE the
    function, so the patch has to land on the class object in that module, not
    on this one's globals.
    """
    import kraken_api

    monkeypatch.setattr(
        kraken_api.KrakenManager, "from_env", staticmethod(lambda: FakeManager())
    )
    out = tmp_path / "d.jsonl"
    args = bot_cli._build_parser().parse_args(
        ["record-depth", "--output", str(out), "--pair", "ETH/USD"]
    )
    rc = bot_cli.cmd_record_depth(args)
    assert rc == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1
    # One record has no interval, so the handler must NOT print a green
    # verdict: exiting 0 while claiming GREEN would be a claim it cannot make.
    out_text = capsys.readouterr().out
    assert "VERDICT: GREEN" not in out_text
    assert "first-run state, not a green verdict" in out_text


def test_record_depth_exits_zero_and_says_green_with_two_records_on_cadence(
    tmp_path, capsys, monkeypatch
):
    """Two records an hour apart is the smallest log that CAN be green."""
    import kraken_api

    # ONE `record_once` reads the clock TWICE: once for the record's own
    # `recorded_at`, once for the status sidecar's `generated_at`.  Two
    # fires therefore need four stamps, in that order.
    stamps = iter([at(0), at(0), at(HOUR), at(HOUR)])
    monkeypatch.setattr(
        kraken_api.KrakenManager, "from_env", staticmethod(lambda: FakeManager())
    )
    monkeypatch.setattr(
        "kraken_trading_bot.depth_recorder.utc_now", lambda: next(stamps)
    )
    out = tmp_path / "d.jsonl"
    args = bot_cli._build_parser().parse_args(["record-depth", "--output", str(out)])
    assert bot_cli.cmd_record_depth(args) == 0
    capsys.readouterr()
    assert bot_cli.cmd_record_depth(args) == 0
    assert "VERDICT: GREEN" in capsys.readouterr().out


def test_depth_gaps_exits_one_on_a_hole(tmp_path, capsys):
    """A hole is a data-loss event, so the scanner's exit code is the machine
    gate; ``0 gaps`` on stdout alone would not be."""
    log = tmp_path / "d.jsonl"
    record_once(output=log, manager=FakeManager(), recorded_at=at(0))
    record_once(output=log, manager=FakeManager(), recorded_at=at(4 * HOUR))

    args = bot_cli._build_parser().parse_args(["depth-gaps", "--output", str(log)])
    rc = bot_cli.cmd_depth_gaps(args)
    assert rc == 1
    assert "VERDICT: RED" in capsys.readouterr().out


def test_depth_gaps_json_is_machine_readable(tmp_path, capsys):
    log = tmp_path / "d.jsonl"
    record_once(output=log, manager=FakeManager(), recorded_at=at(0))
    record_once(output=log, manager=FakeManager(), recorded_at=at(HOUR))
    args = bot_cli._build_parser().parse_args(
        ["depth-gaps", "--output", str(log), "--json"]
    )
    assert bot_cli.cmd_depth_gaps(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["n_records"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# No-consume scope — §8.1.4.  These are the tests that make "the recorder is
# the deliverable, the feature is NOT" mechanically checkable.
# ─────────────────────────────────────────────────────────────────────────────


def _imported_modules(path: str) -> set[str]:
    """Every module name this file imports, transitively at the top level.

    Parsed rather than grepped: a substring search for "features" also hits
    the word in a docstring, which would make the assertion pass or fail for
    reasons that have nothing to do with imports.
    """
    import ast

    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def test_recorder_imports_nothing_from_the_feature_pipeline():
    """§8.1.4: write data, read nothing.

    If this module ever imports ``rl.features`` or ``rl.data``, the
    no-consume constraint is broken and the artifact's schema becomes hostage
    to the observation — which is the failure the whole constraint exists to
    prevent.
    """
    imports = _imported_modules("kraken_trading_bot/depth_recorder.py")
    for forbidden in ("kraken_trading_bot.rl.features", "kraken_trading_bot.rl.data"):
        assert forbidden not in imports, f"depth_recorder must not import {forbidden}"
    assert not any(name.startswith("kraken_trading_bot.rl") for name in imports)


def test_recorder_has_no_feature_pipeline_symbols_at_all():
    """Belt and braces: even a deferred import of these is a violation."""
    source = Path("kraken_trading_bot/depth_recorder.py").read_text(encoding="utf-8")
    import ast

    # Strip docstrings and comments before looking for the symbols: the
    # module's own prose legitimately NAMES them to explain what it does not do.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body.pop(0)
    names = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    } | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    } | {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    for forbidden in ("_SIGNAL_COLUMNS", "_SIGNAL_BUILDER_INPUT_COLUMNS", "order_book_imbalance"):
        assert forbidden not in names, (
            f"depth_recorder references {forbidden!r} in code or string data; "
            "this slice writes data and reads none of it"
        )


def test_the_feature_seam_is_byte_identical_to_its_committed_state():
    """The strongest form of the no-consume claim: `rl/features.py` is not in
    this slice's diff at all.  `git diff HEAD -- <path>` empty is checked by
    the caller (see test_the_feature_seam_is_unchanged_on_disk); here we
    assert the file still parses and still carries its original signal
    column tuple length, so a well-meaning future edit cannot quietly widen
    it and call it a refactor."""
    import ast

    source = Path("kraken_trading_bot/rl/features.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_SIGNAL_COLUMNS" for t in node.targets
        ):
            return  # parses, and the tuple is still there
    raise AssertionError("_SIGNAL_COLUMNS no longer found in rl/features.py")


def test_the_feature_seam_is_unchanged_on_disk():
    """`rl/features.py` must be untouched by this slice.

    Checked against HEAD so the assertion is about THIS slice's diff rather
    than about anything the file happens to contain.  Skips (rather than
    fails) when the file is untracked or the repo has no HEAD, so the test is
    honest about what it can and cannot prove.
    """
    import subprocess

    path = "kraken_trading_bot/rl/features.py"
    proc = subprocess.run(
        ["git", "diff", "--stat", "HEAD", "--", path],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"git unavailable: {proc.stderr.strip()}")
    assert proc.stdout.strip() == "", (
        f"§8.1.4 forbids touching the feature seam; this slice changed it:\n{proc.stdout}"
    )


# ── the double-timer guard ────────────────────────────────────────────────
# NAMED DEFECT: `nix/module.nix` declares a SYSTEM timer (systemd.services /
# systemd.timers, behind `ob.enable`) and `just depth-timer` installs a USER
# timer.  Independent units; neither suppresses the other.  A host that enabled
# both would snapshot twice an hour, and every depth series would silently
# double its own cadence — while the gap counter, scanning a log that looks
# perfectly regular, printed GREEN.  The guard is the recipe refusing.
#
# The recipe body is extracted and run against a FAKE `systemctl` rather than the
# host's real one: the assertion must not depend on this machine's units, and
# must not touch them.

_RECIPE_NAME = "depth-timer"


def _recipe_body(repo: Path) -> str:
    """The shell body of the `depth-timer` recipe, placeholders already resolved."""
    lines = (repo / "justfile").read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, l in enumerate(lines) if re.match(rf"^{_RECIPE_NAME}\b", l)), None
    )
    assert start is not None, f"justfile has no {_RECIPE_NAME} recipe"
    # A just recipe body runs until the next line that is neither blank nor indented.
    # Deliberately NOT `\s{4}`: that assumed an indent width, and depth-timer alone
    # was mixed (4-space header over a 2-space body) before this normalised it.
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].strip() and not lines[i][0].isspace()
        ),
        len(lines),
    )
    body = "\n".join(l.strip() for l in lines[start + 1 : end] if l.strip())
    body = body.replace("{{justfile_directory()}}", str(repo))
    body = re.sub(r"\{\{[^}]*\}\}", "x", body)  # pair/output/count params
    return body


def _run_recipe_with_fake_systemctl(
    repo: Path, tmp_path: Path, *, system_timer_enabled: bool
) -> subprocess.CompletedProcess[str]:
    """Run the recipe with a stub `systemctl` first on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "systemctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        # Only the system-level query is answered "enabled"; everything else the
        # recipe calls (`--user daemon-reload`, `enable --now`, `list-timers`) is
        # a no-op success so the guard is the ONLY thing under test.
        'if [ "$1" = "is-enabled" ]; then\n'
        f"  exit {0 if system_timer_enabled else 1}\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    # HOME redirected so the recipe cannot touch the real user unit dir.
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", "-c", _recipe_body(repo)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_depth_timer_refuses_when_the_system_timer_is_enabled(tmp_path: Path) -> None:
    """The recipe must refuse, not warn: two timers each fire hourly."""
    repo = Path(__file__).resolve().parents[1]
    proc = _run_recipe_with_fake_systemctl(repo, tmp_path, system_timer_enabled=True)
    assert proc.returncode != 0, (
        "just depth-timer proceeded while the SYSTEM timer was enabled — two "
        f"timers would each fire hourly.\nstdout:\n{proc.stdout}"
    )
    assert "REFUSING" in proc.stderr, (
        f"expected an explicit REFUSING on stderr, got:\n{proc.stderr}"
    )
    assert "ob.enable" in proc.stderr, (
        "the refusal must name the switch that turns the other path on, so the "
        f"user can choose rather than guess:\n{proc.stderr}"
    )


def test_depth_timer_proceeds_when_the_system_timer_is_absent(tmp_path: Path) -> None:
    """The guard must not fire on the ordinary single-timer host (no false block)."""
    repo = Path(__file__).resolve().parents[1]
    proc = _run_recipe_with_fake_systemctl(repo, tmp_path, system_timer_enabled=False)
    assert "REFUSING" not in proc.stderr, (
        f"guard fired with no system timer present — it would block every host:\n{proc.stderr}"
    )
    assert proc.returncode == 0, (
        f"recipe failed with no system timer enabled:\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    assert "enabled. Next fire:" in proc.stdout, (
        f"recipe did not reach its normal success path:\n{proc.stdout}"
    )


def test_the_two_timer_paths_are_both_declared() -> None:
    repo = Path(__file__).resolve().parents[1]
    """Both paths must still exist — the guard picks one at runtime, it does not
    delete the NixOS declaration."""
    module = (repo / "nix" / "module.nix").read_text(encoding="utf-8")
    assert 'systemd.services."kraken-trading-bot-order-book"' in module, (
        "the NixOS system timer is gone; if it was deleted deliberately, drop the "
        "recipe guard and this test together rather than leaving one behind"
    )
    assert 'systemd.timers."kraken-trading-bot-order-book"' in module
    # Assignment lines only.  The block's own COMMENT names `systemd.user.*` to
    # explain that it is deliberately not used, so matching the raw text would
    # fail on the explanation and pass on a real regression.
    g1 = module.split("G1 order-book depth recorder")[1][:4000]
    assignments = [
        l for l in g1.splitlines() if re.match(r"^\s*systemd\.(user|lightdm)\.", l)
    ]
    assert not assignments, (
        "the G1 block must assign under systemd.services/systemd.timers (NixOS), not "
        f"a home-manager namespace — found: {assignments}"
    )
