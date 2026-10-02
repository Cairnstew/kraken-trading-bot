"""Gap accounting for the seeded market-data store (Phase 6 finding F6).

The store reader globs ``{PAIR_ID}/{interval_min}/{YYYY-MM}.parquet`` and
concatenates, so a hole in the series is invisible downstream: a consumer
sees a shorter, contiguous-looking frame and a feature window of N rows
that silently spans more than N hours.

The shipped store measures **157 missing hourly bars across 28 gaps**
(measured 2026-10-02), largest single gap spanning 39h / **38 missing
bars**, at the **seed/live-append seam**: ``2026-08-31 23:00`` to
``2026-09-02 14:00``, the month-file boundary where the archive seed ends
and this repo's live append leg takes over.

These tests DETECT AND LABEL. They deliberately assert nothing about a
repair, because the repair is CAND-3b and out of scope.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.store_gap_scan import (  # noqa: E402
    SEAM_GAP_NAME,
    BarGap,
    find_gaps,
    format_report,
    main,
    month_files,
    scan,
)

HOUR = pd.Timedelta(hours=1)


def _write_month(root: Path, pair_id: str, month: str, stamps) -> None:
    """Write one store month in the store's own shape: int64 epoch seconds."""
    directory = root / pair_id / "60"
    directory.mkdir(parents=True, exist_ok=True)
    seconds = [int(pd.Timestamp(s).timestamp()) for s in stamps]
    pd.DataFrame(
        {
            "time": seconds,
            "open": [1.0] * len(seconds),
            "high": [1.0] * len(seconds),
            "low": [1.0] * len(seconds),
            "close": [1.0] * len(seconds),
            "vwap": [1.0] * len(seconds),
            "volume": [1.0] * len(seconds),
            "count": [1] * len(seconds),
        }
    ).to_parquet(directory / f"{month}.parquet")


def _hourly(start: str, n: int):
    return pd.date_range(start, periods=n, freq="1h", tz="UTC")


# ── the arithmetic ─────────────────────────────────────────────────────────


def test_a_contiguous_run_has_no_gaps():
    assert find_gaps(_hourly("2026-01-01", 50)) == []


def test_a_single_missing_bar_is_counted_not_guessed():
    stamps = [s for s in _hourly("2026-01-01", 10) if s != pd.Timestamp("2026-01-01 03:00", tz="UTC")]
    gaps = find_gaps(stamps)
    assert len(gaps) == 1
    assert gaps[0].missing == 1
    assert gaps[0].after == pd.Timestamp("2026-01-01 02:00", tz="UTC")
    assert gaps[0].before == pd.Timestamp("2026-01-01 04:00", tz="UTC")


def test_missing_bars_and_elapsed_hours_are_different_numbers():
    """The unit confusion this finding was first reported with.

    The seam hole was quoted as both "39h" and "38 bars"; both are right,
    because N missing bars means the surviving bars are N+1 steps apart.
    """
    stamps = [
        pd.Timestamp("2026-08-31 23:00", tz="UTC"),
        pd.Timestamp("2026-09-02 14:00", tz="UTC"),
    ]
    gap = find_gaps(stamps)[0]
    assert gap.missing == 38
    assert gap.elapsed_hours == 39.0


def test_gaps_come_back_largest_first():
    stamps = [
        pd.Timestamp("2026-01-01 00:00", tz="UTC"),
        pd.Timestamp("2026-01-01 01:00", tz="UTC"),
        pd.Timestamp("2026-01-01 02:00", tz="UTC"),
        pd.Timestamp("2026-01-01 07:00", tz="UTC"),  # 4-bar hole before this
        pd.Timestamp("2026-01-01 08:00", tz="UTC"),
        pd.Timestamp("2026-01-01 09:00", tz="UTC"),
        pd.Timestamp("2026-01-01 09:30", tz="UTC"),  # fractional step, smaller
    ]
    gaps = find_gaps(stamps)
    assert [g.missing for g in gaps] == sorted((g.missing for g in gaps), reverse=True)
    assert gaps[0].missing == 4


def test_duplicate_bars_do_not_invent_a_gap():
    stamps = list(_hourly("2026-01-01", 10)) * 2
    assert find_gaps(stamps) == []


def test_an_empty_or_single_stamp_series_has_no_gaps():
    assert find_gaps([]) == []
    assert find_gaps([pd.Timestamp("2026-01-01", tz="UTC")]) == []


def test_an_off_grid_boundary_is_flagged_as_a_lower_bound():
    """The archive has at least one 09:27:14 bar; say so, do not fake precision."""
    stamps = [
        pd.Timestamp("2026-01-01 00:00", tz="UTC"),
        pd.Timestamp("2026-01-01 09:27:14", tz="UTC"),
    ]
    gap = find_gaps(stamps)[0]
    assert gap.misaligned is True
    assert "lower bound" in gap.describe()


# ── the seam label ─────────────────────────────────────────────────────────


def test_a_gap_straddling_a_month_boundary_is_named_the_seed_live_append_seam():
    """The whole point of F6: name the gap that matters, by name."""
    stamps = [
        pd.Timestamp("2026-08-31 23:00", tz="UTC"),
        pd.Timestamp("2026-09-02 14:00", tz="UTC"),
    ]
    gap = find_gaps(stamps)[0]
    assert gap.missing == 38
    assert gap.is_seam is True
    assert gap.name == SEAM_GAP_NAME
    assert SEAM_GAP_NAME in gap.describe()


def test_a_gap_inside_one_month_is_not_called_the_seam():
    stamps = [
        pd.Timestamp("2026-05-01 00:00", tz="UTC"),
        pd.Timestamp("2026-05-01 04:00", tz="UTC"),
    ]
    gap = find_gaps(stamps)[0]
    assert gap.is_seam is False
    assert gap.name == ""
    assert SEAM_GAP_NAME not in gap.describe()


def test_only_the_LATEST_month_boundary_hole_is_the_seam():
    """An earlier month-boundary hole is an archive gap, not the seam.

    Without this rule a store with holes at several month boundaries would
    label every one of them "the seed/live-append seam", which would make
    the label meaningless. The seam is where the seed hands over to the
    live append leg, i.e. the newest boundary in the series.
    """
    stamps = (
        list(_hourly("2026-06-30 20:00", 4))       # ends 2026-06-30 23:00
        + list(_hourly("2026-07-02", 720))         # contiguous July, ends 07-31 23:00
        + list(_hourly("2026-08-02 00:00", 4))     # hole, July->August
    )
    gaps = find_gaps(stamps)
    assert len(gaps) == 2, [f"{g.after}->{g.before} ({g.missing})" for g in gaps]
    named = [g for g in gaps if g.is_seam]
    assert len(named) == 1, "exactly one gap may carry the seam label"
    assert named[0].after == pd.Timestamp("2026-07-31 23:00", tz="UTC")
    assert named[0].before == pd.Timestamp("2026-08-02 00:00", tz="UTC")


# ── reading a real store tree ──────────────────────────────────────────────


def test_scan_reads_a_seeded_store_and_finds_the_seam_hole(tmp_path):
    """End-to-end over real parquet, in the store's own layout and schema."""
    # August runs to the end of its month; September resumes 38 bars later
    # (that is the seam) and then carries on with a mid-month hole of its own.
    _write_month(tmp_path, "ETH_USD", "2026-08", _hourly("2026-08-31 00:00", 24))
    _write_month(
        tmp_path,
        "ETH_USD",
        "2026-09",
        list(_hourly("2026-09-02 14:00", 2))
        + list(_hourly("2026-09-02 20:00", 5)),
    )

    report = scan(tmp_path, "ETH_USD", 60)
    assert report.n_bars == 24 + 7
    assert report.n_gaps == 2
    # 38 at the seam + 4 mid-September (16,17,18,19 are absent).
    assert report.n_missing == 38 + 4
    assert report.max_gap.missing == 38

    seam = report.seam_gaps
    assert len(seam) == 1
    assert seam[0].is_seam
    assert seam[0].after == pd.Timestamp("2026-08-31 23:00", tz="UTC")
    assert seam[0].before == pd.Timestamp("2026-09-02 14:00", tz="UTC")
    assert seam[0].elapsed_hours == 39.0
    # The mid-September hole is an ordinary archive gap, NOT the seam.
    assert sorted(g.name for g in report.gaps) == ["", SEAM_GAP_NAME]


def test_scan_of_a_contiguous_store_reports_zero_gaps_and_says_so(tmp_path):
    _write_month(tmp_path, "ETH_USD", "2026-01", _hourly("2026-01-01", 100))
    report = scan(tmp_path, "ETH_USD", 60)
    assert report.n_gaps == 0
    assert report.n_missing == 0
    assert report.max_gap is None
    assert report.seam_gaps == []
    assert report.caveat().startswith("Caveat:")


def test_scan_raises_for_an_unseeded_source(tmp_path):
    with pytest.raises(FileNotFoundError, match="just store-plan"):
        scan(tmp_path, "ETH_USD", 60)


def test_month_files_ignores_non_parquet_and_orders_oldest_first(tmp_path):
    _write_month(tmp_path, "ETH_USD", "2026-10", _hourly("2026-10-01", 2))
    _write_month(tmp_path, "ETH_USD", "2026-02", _hourly("2026-02-01", 2))
    (tmp_path / "ETH_USD" / "60" / "notes.csv").write_text("x", encoding="utf-8")
    found = month_files(tmp_path, "ETH_USD", 60)
    assert [p.name for p in found] == ["2026-02.parquet", "2026-10.parquet"]


# ── the caveat every result carries ────────────────────────────────────────


def test_the_caveat_names_the_missing_bars_gaps_and_the_seam_hole(tmp_path):
    """F6 item 3: a result computed across a gap must say so, visibly."""
    _write_month(tmp_path, "ETH_USD", "2026-08", _hourly("2026-08-31 00:00", 24))
    _write_month(tmp_path, "ETH_USD", "2026-09", _hourly("2026-09-02 14:00", 24))
    report = scan(tmp_path, "ETH_USD", 60)

    caveat = report.caveat()
    assert "38 missing bar(s)" in caveat
    assert "1 gap(s)" in caveat
    assert SEAM_GAP_NAME in caveat
    # And it must point at the real fix rather than implying this is fine.
    assert "CAND-3b" in caveat
    assert "BAR COUNTS" in caveat


def test_format_report_prints_the_seam_warning_on_its_own_line(tmp_path):
    _write_month(tmp_path, "ETH_USD", "2026-08", _hourly("2026-08-31 00:00", 24))
    _write_month(tmp_path, "ETH_USD", "2026-09", _hourly("2026-09-02 14:00", 24))
    report = scan(tmp_path, "ETH_USD", 60)
    text = format_report(report, "ETH_USD")

    assert "MISSING" in text
    assert SEAM_GAP_NAME in text
    assert "**" in text  # the flagged line
    assert "CAND-3b" in text


# ── the CLI ────────────────────────────────────────────────────────────────


def test_cli_reports_a_gap_and_exits_zero_because_it_is_a_report(tmp_path):
    _write_month(tmp_path, "ETH_USD", "2026-08", _hourly("2026-08-31 00:00", 24))
    _write_month(tmp_path, "ETH_USD", "2026-09", _hourly("2026-09-02 14:00", 24))
    code = main(
        ["--store", str(tmp_path), "--ticker", "ETH/USD", "--interval", "60"]
    )
    assert code == 0


def test_cli_accepts_the_sibling_seeder_flags_it_has_no_opinion_on(tmp_path, capsys):
    """`just store-verify` forwards ONE ARGS list to both tools."""
    _write_month(tmp_path, "ETH_USD", "2026-08", _hourly("2026-08-31 00:00", 24))
    _write_month(tmp_path, "ETH_USD", "2026-09", _hourly("2026-09-02 14:00", 24))
    code = main(
        [
            "--store", str(tmp_path),
            "--ticker", "ETH/USD",
            "--since", "2020-01-01",
            "--until", "2026-01-01",
        ]
    )
    assert code == 0
    assert "ignoring flags meant for the seeder" in capsys.readouterr().err


def test_cli_exits_two_for_an_unseeded_store(tmp_path, capsys):
    assert main(["--store", str(tmp_path), "--ticker", "ETH/USD"]) == 2
    assert "just store-plan" in capsys.readouterr().err


def test_cli_requires_a_ticker_or_pair_id(tmp_path):
    with pytest.raises(SystemExit):
        main(["--store", str(tmp_path)])
