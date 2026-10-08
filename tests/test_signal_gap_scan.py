"""Tests for tools/signal_gap_scan.py — the silent-coverage-loss detector.

The defect class these exist for: a signal channel that stops being written
looks exactly like one that was never configured. On 2026-10-04 the social
timer exited 0/SUCCESS hourly while StockTwits returned HTTP 403 on every
call, and nothing in the repo noticed, because nothing counted the hours a
signal file actually covers.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "tools" / "signal_gap_scan.py"


def _hourly(path: Path, start: str, n: int) -> None:
    """Write n hourly records beginning at `start` (ISO-8601)."""
    t0 = dt.datetime.fromisoformat(start)
    lines = []
    for i in range(n):
        ts = t0 + dt.timedelta(hours=i)
        lines.append(
            json.dumps({"ticker": "ETH_USD", "timestamp": ts.isoformat()})
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
    )


# ── coverage counting ────────────────────────────────────────────────────


def test_contiguous_channel_reports_no_gaps(tmp_path):
    path = tmp_path / "f.jsonl"
    _hourly(path, "2026-10-04T00:00:00+00:00", 24)
    out = _run(str(path), "--channel", "f", "--json")
    assert out.returncode == 0, out.stdout + out.stderr
    payload = json.loads(out.stdout)
    rep = payload["channels"][0]
    assert rep["n_records"] == 24
    assert rep["hours_covered"] == 24
    assert rep["n_gaps"] == 0
    assert rep["coverage_ratio"] == 1.0
    assert payload["any_gap"] is False


def test_a_missing_hour_is_reported_and_exits_nonzero(tmp_path):
    """The core case: an hour that is not there must be visible AND gateable."""
    path = tmp_path / "s.jsonl"
    # 00,01, then a 5-hour hole, then 06..12
    t0 = dt.datetime.fromisoformat("2026-10-04T00:00:00+00:00")
    keep = [0, 1] + list(range(6, 13))
    path.write_text(
        "".join(
            json.dumps(
                {
                    "ticker": "ETH_USD",
                    "timestamp": (t0 + dt.timedelta(hours=h)).isoformat(),
                }
            )
            + "\n"
            for h in keep
        ),
        encoding="utf-8",
    )
    out = _run(str(path), "--channel", "s", "--json")
    assert out.returncode == 1, "a hole must make this gateable"
    rep = json.loads(out.stdout)["channels"][0]
    assert rep["n_gaps"] == 1
    assert rep["n_missing"] == 4  # hours 02,03,04,05
    assert rep["gaps"][0]["missing_hours"] == 4
    assert rep["coverage_ratio"] < 1.0


def test_a_single_missing_hour_is_always_a_gap(tmp_path):
    """1.5x tolerates JITTER, not a missing hour.

    The threshold is 1.5 x 3600s = 5400s. Dropping one hour from an hourly
    series leaves a 7200s (2.0x) delta, which is over it — correctly, since
    an hour really is absent. What the factor buys is that a late pull
    (e.g. 1.4h) does not cry wolf, which is the property the next test pins.
    """
    path = tmp_path / "f.jsonl"
    t0 = dt.datetime.fromisoformat("2026-10-04T00:00:00+00:00")
    keep = [h for h in range(10) if h != 5]
    path.write_text(
        "".join(
            json.dumps(
                {"timestamp": (t0 + dt.timedelta(hours=h)).isoformat()}) + "\n"
            for h in keep
        ),
        encoding="utf-8",
    )
    out = _run(str(path), "--channel", "f", "--json")
    assert out.returncode == 1
    rep = json.loads(out.stdout)["channels"][0]
    assert rep["n_gaps"] == 1
    assert rep["n_missing"] == 1


def test_a_late_pull_within_the_factor_is_not_a_gap(tmp_path):
    """A record 1.4h after the previous one is jitter, not a hole."""
    path = tmp_path / "f.jsonl"
    t0 = dt.datetime.fromisoformat("2026-10-04T00:00:00+00:00")
    rows = []
    for h in range(6):
        rows.append(
            json.dumps(
                {"timestamp": (t0 + dt.timedelta(hours=h)).isoformat()}) + "\n"
        )
    # A straggler landing 1.4h after hour 5 — under the 1.5x threshold.
    rows.append(
        json.dumps({"timestamp": (t0 + dt.timedelta(hours=6, minutes=24)).isoformat()})
        + "\n"
    )
    path.write_text("".join(rows), encoding="utf-8")
    out = _run(str(path), "--channel", "f", "--json")
    assert out.returncode == 0, out.stdout
    assert json.loads(out.stdout)["channels"][0]["n_gaps"] == 0


def test_duplicate_hours_collapse_and_do_not_inflate_coverage(tmp_path):
    path = tmp_path / "f.jsonl"
    t0 = dt.datetime.fromisoformat("2026-10-04T00:00:00+00:00")
    rows = []
    for h in range(5):
        ts = (t0 + dt.timedelta(hours=h)).isoformat()
        rows.append(json.dumps({"timestamp": ts}) + "\n")
        rows.append(json.dumps({"timestamp": ts}) + "\n")  # duplicate hour
    path.write_text("".join(rows), encoding="utf-8")
    out = _run(str(path), "--channel", "f", "--json")
    assert out.returncode == 0
    rep = json.loads(out.stdout)["channels"][0]
    assert rep["n_records"] == 10
    assert rep["hours_covered"] == 5, "distinct hours, not records"


# ── the window question ──────────────────────────────────────────────────


def test_window_reports_missing_hours_even_when_the_file_is_contiguous(
    tmp_path,
):
    """A healthy file can still not COVER the window a matrix pins.

    This is the case that matters: `plan` only checks a signal file exists
    and is tagged for the ticker, so a short file passes every other gate.
    """
    path = tmp_path / "n.jsonl"
    _hourly(path, "2026-10-04T00:00:00+00:00", 24)  # no gaps at all
    out = _run(
        str(path), "--channel", "n",
        "--window-since", "2026-09-10T15:00:00+00:00",
        "--window-until", "2026-09-20T15:00:00+00:00",  # 240h, none covered
        "--json",
    )
    rep = json.loads(out.stdout)["channels"][0]
    assert rep["n_gaps"] == 0, "the file itself is contiguous"
    assert rep["in_window_hours"] == 0
    assert rep["in_window_missing"] == 240
    assert out.returncode == 1, "but the window verdict must still gate"


def test_window_fully_covered_is_not_a_gap(tmp_path):
    path = tmp_path / "f.jsonl"
    _hourly(path, "2026-10-04T00:00:00+00:00", 24)
    out = _run(
        str(path), "--channel", "f",
        "--window-since", "2026-10-04T00:00:00+00:00",
        "--window-until", "2026-10-05T00:00:00+00:00",  # exactly 24h
        "--json",
    )
    assert out.returncode == 0
    rep = json.loads(out.stdout)["channels"][0]
    assert rep["in_window_hours"] == 24
    assert rep["in_window_missing"] == 0


def test_inverted_window_is_an_error_not_a_silent_pass(tmp_path):
    path = tmp_path / "f.jsonl"
    _hourly(path, "2026-10-04T00:00:00+00:00", 5)
    out = _run(
        str(path), "--channel", "f",
        "--window-since", "2026-10-05T00:00:00+00:00",
        "--window-until", "2026-10-04T00:00:00+00:00",
        "--json",
    )
    rep = json.loads(out.stdout)["channels"][0]
    assert "inverted" in (rep["error"] or "")


# ── malformed input ──────────────────────────────────────────────────────


def test_a_missing_file_is_an_error_not_a_pass(tmp_path):
    out = _run(str(tmp_path / "nope.jsonl"), "--channel", "x", "--json")
    assert out.returncode == 1
    rep = json.loads(out.stdout)["channels"][0]
    assert rep["exists"] is False
    assert "not found" in rep["error"]


def test_an_empty_file_is_an_error_not_a_pass(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    out = _run(str(path), "--channel", "e", "--json")
    assert out.returncode == 1
    assert "no parseable timestamps" in json.loads(out.stdout)["channels"][0]["error"]


def test_unparseable_lines_are_skipped_not_counted_as_coverage(tmp_path):
    path = tmp_path / "f.jsonl"
    _hourly(path, "2026-10-04T00:00:00+00:00", 3)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write(json.dumps({"no_timestamp": True}) + "\n")
    out = _run(str(path), "--channel", "f", "--json")
    rep = json.loads(out.stdout)["channels"][0]
    assert rep["hours_covered"] == 3
    assert rep["n_records"] == 5


def test_unrecoverable_channels_are_labelled(tmp_path):
    """news and social have no historical endpoint, so a hole is permanent."""
    path = tmp_path / "s.jsonl"
    t0 = dt.datetime.fromisoformat("2026-10-04T00:00:00+00:00")
    # Three early records, then a 40-hour hole. `write_text` REPLACES, so the
    # full set is written here rather than layering on _hourly.
    hours = [0, 1, 2, 42]
    path.write_text(
        "".join(
            json.dumps(
                {"timestamp": (t0 + dt.timedelta(hours=h)).isoformat()}) + "\n"
            for h in hours
        ),
        encoding="utf-8",
    )
    out = _run(str(path), "--channel", "social", "--json")
    assert out.returncode == 1
    rep = json.loads(out.stdout)["channels"][0]
    assert rep["unrecoverable"] is True
    assert rep["n_gaps"] >= 1
    assert rep["n_missing"] >= 39


def test_human_output_names_the_permanent_loss(tmp_path):
    path = tmp_path / "s.jsonl"
    t0 = dt.datetime.fromisoformat("2026-10-04T00:00:00+00:00")
    path.write_text(
        "".join(
            json.dumps(
                {"timestamp": (t0 + dt.timedelta(hours=h)).isoformat()}) + "\n"
            for h in (0, 1, 2, 42)
        ),
        encoding="utf-8",
    )
    out = _run(str(path), "--channel", "social")
    assert out.returncode == 1
    assert "PERMANENT DATA LOSS" in out.stdout
    assert "backfill" in out.stdout, "names the one channel that IS repairable"