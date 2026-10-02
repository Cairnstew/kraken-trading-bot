#!/usr/bin/env python3
"""Refuse a seed report that would leave an unreadable market-data store.

``kraken-deep-history`` can write two things and looks equally happy
about both:

* ``store_mode: "market-data"``  -> ``{PAIR_ID}/{interval_min}/{YYYY-MM}.parquet``
  month files, which is what ``MarketDataStore.read`` globs.
* ``store_mode: "fallback-csv"``  -> ``.csv`` files, written when
  ``_open_store`` could not import the store package (its own dev shell
  carries only pytest -- no pandas, no pyarrow).

Only the first is readable by this repo.  A fallback-csv seed reports
success, leaves a directory that *looks* seeded, and the bot then reads
~721 live bars while the run log claims deep history -- the exact failure
the store recipes exist to remove.  The seed recipe therefore pipes its
report through this guard, and a ``fallback-csv`` verdict is a refusal,
not a warning.

Usage::

    kraken_deep_history seed ... | python tools/store_guard.py -
    python tools/store_guard.py /tmp/seed-report.json

Exit codes: ``0`` the store is readable, ``1`` it is not (with the reason
on stderr), ``2`` the report could not be read at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

#: The only store mode whose files ``MarketDataStore.read`` can see.
REQUIRED_STORE_MODE = "market-data"


class StoreModeError(RuntimeError):
    """A seed report names a store mode the reader cannot use."""


def require_market_data_store(report: Mapping[str, Any]) -> str:
    """Assert the seeder wrote a ``market-data`` (parquet) store.

    Args:
        report: The seed report ``kraken_deep_history seed`` prints.

    Returns:
        The confirmed ``store_mode``, for callers that want to echo it.

    Raises:
        StoreModeError: If ``store_mode`` is absent, is not
            ``"market-data"``, or the report is otherwise not a seed
            report.
    """
    mode = (report or {}).get("store_mode")
    if mode == REQUIRED_STORE_MODE:
        return str(mode)
    found = repr(mode) if mode is not None else "no store_mode field"
    raise StoreModeError(
        f"seed report says store_mode={found}, not "
        f"\"{REQUIRED_STORE_MODE}\": the seed did not write parquet months "
        f"into the store, so MarketDataStore.read (which globs "
        f"{{PAIR_ID}}/{{interval_min}}/{{YYYY-MM}}.parquet) will find "
        "nothing and the run will silently read only Kraken's ~721 "
        "recent bars. Cause: the seeder ran where the store package or "
        "pyarrow is not importable, which is what kraken-deep-history's "
        "own dev shell does (pytest only). Fix: run the seed under THIS "
        "repo's dev shell, i.e. `just store-seed` rather than a bare "
        "`python cli.py seed`."
    )


def _load(raw: str) -> Mapping[str, Any]:
    if raw == "-":
        return json.load(sys.stdin)
    return json.loads(Path(raw).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: store_guard.py <report.json|->", file=sys.stderr)
        return 2
    try:
        report = _load(args[0])
    except (OSError, json.JSONDecodeError) as exc:
        print(f"store_guard: cannot read the seed report: {exc}", file=sys.stderr)
        return 2
    try:
        mode = require_market_data_store(report)
    except StoreModeError as exc:
        print(f"store_guard: REFUSING the seed. {exc}", file=sys.stderr)
        return 1
    print(
        "store_guard: store_mode={} confirmed for {} at {} — {} bars "
        "added, months {}/{} downloaded{}".format(
            mode,
            report.get("pair_id", "?"),
            report.get("ticker", "?"),
            report.get("bars_added", "?"),
            report.get("months_downloaded", "?"),
            report.get("months_requested", "?"),
            f", skipped: {', '.join(report.get('skipped') or [])}"
            if report.get("skipped")
            else "",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())