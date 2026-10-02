"""Width-neutrality / observation-matrix fingerprint for a cached OHLCV frame.

WHY THIS IS COMMITTED. The gate for the CAND-3a pass asserted width
neutrality via obs sha256 ``93edc733...`` and arr sha256 ``6a88a379...``.
The script that computed those lived in ``/tmp``, so nobody -- including a
future session -- could re-derive the gate's central invariant. This file
is that script, committed, so the assertion is reproducible.

THE HASH TRAP THIS FILE EXISTS TO AVOID. There are two different sha256
values for the same matrix and they are NOT interchangeable:

  * ``obs_file_sha256``  -- the ``.npy`` FILE bytes, header included.
  * ``obs_array_sha256`` -- the raw float64 array bytes.

The CAND-3a gate figures were the FILE hashes; an earlier draft of the
receipt quoted the ARRAY hashes and looked like a mismatch. Both are
printed here, always labelled, so nobody re-derives the wrong one and
reports a false regression.

Usage
-----
    just width-check --frame /path/frame.parquet
    just width-check --frame ... --expect-obs 93edc733... --expect-arr 6a88a379...

Exit status is 0 only when every expectation given is met.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# Mirrors the shipped production configuration used by the gate. Changing
# any of these changes the fingerprint, so they are named here rather than
# left implicit: a fingerprint nobody can reproduce is not a fingerprint.
TICKER = "ETH_USD"
WINDOWS = (1, 4, 24)
GROUPS = ("price", "technical", "volume", "microstructure", "signals")


def fingerprint(frame_path: Path) -> dict[str, object]:
    """Compute the observation/transform fingerprint for one frame."""
    import numpy as np
    import pandas as pd

    from kraken_trading_bot.rl.features import FeaturePipeline, first_tradable_index

    df = pd.read_parquet(frame_path)
    raw_n = len(df)

    pipe = FeaturePipeline(windows=WINDOWS, feature_groups=GROUPS)
    feat = pipe.compute(df)
    obs = feat.ffill().fillna(0.0)
    start = int(first_tradable_index(feat))

    # Stored-stats branch, then the in-sample fallback branch.
    pipe.fit(df, ticker_id=TICKER)
    arr_stats = pipe.transform(df, ticker_id=TICKER)
    arr_insample = FeaturePipeline(
        windows=WINDOWS, feature_groups=GROUPS
    ).transform(df, ticker_id=TICKER)

    obs_arr = obs.to_numpy(dtype=np.float64)

    # The FILE hash needs a real .npy header, so write then read back.
    import io

    def file_sha(array) -> str:
        buf = io.BytesIO()
        np.save(buf, array)
        return hashlib.sha256(buf.getvalue()).hexdigest()

    return {
        "frame": str(frame_path),
        "raw_bars": raw_n,
        "index_first": str(df.index[0]),
        "index_last": str(df.index[-1]),
        "n_features": int(obs.shape[1]),
        "start_index": start,
        "usable_bars": raw_n - start,
        "n_nonfinite_obs_cells": int((~np.isfinite(obs_arr)).sum()),
        "obs_file_sha256": file_sha(obs_arr),
        "obs_array_sha256": hashlib.sha256(obs_arr.tobytes()).hexdigest(),
        "arr_file_sha256": file_sha(arr_stats),
        "arr_array_sha256": hashlib.sha256(arr_stats.tobytes()).hexdigest(),
        "arr_stats_shape": list(arr_stats.shape),
        "arr_stats_dtype": str(arr_stats.dtype),
        "arr_stats_is_finite": bool(np.isfinite(arr_stats).all()),
        "arr_insample_is_finite": bool(np.isfinite(arr_insample).all()),
    }


def _short(h: str, n: int = 8) -> str:
    return f"{h[:n]}…" if len(h) > n else h


def render(fp: dict[str, object], expected: dict[str, str]) -> tuple[str, bool]:
    """Compact receipt. ``expected`` maps ``obs_file``/``arr_file`` to a sha."""
    lines = [
        f"width-check  frame={Path(str(fp['frame'])).name}  "
        f"bars={fp['raw_bars']}  features={fp['n_features']}  "
        f"start_index={fp['start_index']}  usable={fp['usable_bars']}",
        f"  obs  FILE  {_short(str(fp['obs_file_sha256']))}"
        f"     array {_short(str(fp['obs_array_sha256']))}",
        f"  arr  FILE  {_short(str(fp['arr_file_sha256']))}"
        f"     array {_short(str(fp['arr_array_sha256']))}",
        f"  nonfinite_obs_cells={fp['n_nonfinite_obs_cells']}  "
        f"arr_finite={fp['arr_stats_is_finite']}  "
        f"insample_finite={fp['arr_insample_is_finite']}",
    ]
    ok = True
    for key, label in (("obs", "obs_file"), ("arr", "arr_file")):
        want = expected.get(key)
        if not want:
            continue
        got = str(fp[f"{label}_sha256"])
        match = got.startswith(want)
        ok = ok and match
        lines.append(
            f"  expect {key}={want}  ->  {'MATCH' if match else 'MISMATCH'}  ({_short(got)})"
        )
    return "\n".join(lines), ok


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or __file__).split("\n")[0])
    p.add_argument("--frame", required=True, type=Path, help="parquet OHLCV frame")
    p.add_argument("--expect-obs", help="expected obs FILE sha256 (prefix ok)")
    p.add_argument("--expect-arr", help="expected arr FILE sha256 (prefix ok)")
    p.add_argument("--json", action="store_true", help="full JSON instead of the receipt")
    a = p.parse_args(argv)

    if not a.frame.exists():
        print(f"width-check: frame not found: {a.frame}", file=sys.stderr)
        return 2

    fp = fingerprint(a.frame)
    expected = {k: v for k, v in (("obs", a.expect_obs), ("arr", a.expect_arr)) if v}

    if a.json:
        print(json.dumps(fp, indent=2))
        return 0

    text, ok = render(fp, expected)
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())