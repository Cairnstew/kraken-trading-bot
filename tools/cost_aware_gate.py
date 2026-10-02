"""Re-derive the cost-aware CAND-5 dispersion figures from per-seed records.

WHY THIS IS A SEPARATE FILE AND NOT A ``model_matrix.py`` SUBCOMMAND.
``tools/model_matrix.py`` holds the gate's *estimator* and its
pre-registered ``DISPERSION_RATIO_THRESHOLD``. The CAND-3a gate asserted,
repeatedly and correctly, that that file is **byte-identical** to the
commit that pre-registered the threshold. Adding a subcommand to it would
permanently weaken that assertion from "byte-identical" to "the estimator
and threshold are unchanged", which is strictly weaker evidence for a
pre-registration claim.

So this driver lives beside it and **imports** the shipped estimator.
Nothing here chooses a threshold, an estimator, or an arm assignment:
every one of those is an argument or comes from ``model_matrix``. That is
deliberate -- the CAND-3a scratch scorer that produced the published
figures also imported rather than reimplemented, and duplicating the
estimator would create a second source of truth that can drift.

THE INPUTS ARE NOT COMMITTED. Per-seed backtest records (``n_bars``,
``num_trades``, ``final_equity``, ``fee_rate``, ``excess_return``, ...)
are run artifacts and stay out of git, exactly like the store and the
trained models. This driver is what makes them *re-derivable* rather than
merely re-runnable: with the records regenerated into ``--records``, every
figure in the published result table comes back from committed code.

Usage
-----
    just cost-aware-replay \\
      --records /tmp/krb-cost-aware/cost \\
      --control-records /tmp/krb-cost-aware/ctrl \\
      --cells live=0396722e7a1a,d76622e63d7c,dfdd93543409 \\
      --cells store=0fed61e632ba,0498bcf75916,4630123dff45
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics as st
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.model_matrix import (  # noqa: E402  -- path set above, by design
    DISPERSION_RATIO_THRESHOLD,
    MIN_REPLICATES_FOR_A_CLAIM,
    dispersion_verdict,
    pooled_within_spread,
    summarize,
)

#: The four metrics the published result table reports, in its order.
DEFAULT_METRICS = ("excess_return", "total_return", "sharpe", "max_drawdown")

#: Absolute cost is NOT recomputable from these records -- they carry no
#: notional or position size. So the gate's cost claims are reported as two
#: named proxies and NEITHER is presented as the true ratio. See
#: DECISION.md section 14.0 / finding R3.
COST_PROXIES = ("trades", "equity_drag")


def _load(records: Path, cell: str) -> dict[str, Any] | None:
    p = records / f"{cell}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def _group_rows(
    records: Path, cells: Sequence[str], metric: str
) -> list[dict[str, Any]]:
    """Build the row shape ``pooled_within_spread`` expects.

    Same shape ``model_matrix._group_summaries`` produces: a ``summary``
    mapping per group holding ``n``/``q1``/``q3``. Built here rather than
    reimplemented in ``model_matrix`` so the estimator keeps one caller
    contract.
    """
    values = []
    for cell in cells:
        rec = _load(records, cell)
        if rec is not None and rec.get(metric) is not None:
            values.append(float(rec[metric]))
    summary = summarize(values)
    return [
        {
            "key": "arm",
            "label": "arm",
            "n_valid": summary.get("n", 0),
            "n_requested": len(cells),
            "summary": summary,
        }
    ]


def _n_valid(records: Path, cells: Sequence[str], metric: str) -> int:
    n = 0
    for c in cells:
        rec = _load(records, c)
        if rec is not None and rec.get(metric) is not None:
            n += 1
    return n


def _median(records: Path, cells: Sequence[str], metric: str) -> float | None:
    vals = []
    for cell in cells:
        rec = _load(records, cell)
        if rec is not None and rec.get(metric) is not None:
            vals.append(float(rec[metric]))
    return st.median(vals) if vals else None


def _index(
    records: Path, cells: Sequence[str], seeds: Sequence[int | None]
) -> list[tuple[str, dict[str, Any], int | None]]:
    """``(cell_id, record, seed)`` per cell, in the order given.

    MEASURED LIMITATION, and the reason ``--cells`` accepts ``id:seed``:
    these backtest records all carry the SAME ``seed`` field (the trainer's
    default), so the records cannot be used to pair arms by seed. Pairing
    them by list position alone is how a mis-ordered ``--cells`` silently
    computes a cost ratio across mismatched seeds. So: if seeds are
    annotated, pairing is verified and a mismatch is reported; if not, the
    pairing is caller-asserted and the receipt says so.
    """
    out = []
    for i, c in enumerate(cells):
        rec = _load(records, c)
        if rec is None:
            raise SystemExit(f"cost-aware-replay: no record for cell {c!r} in {records}")
        out.append((c, rec, seeds[i] if i < len(seeds) else None))
    return out


def _cost_proxies(
    cost: Path,
    control: Path,
    live_cells: Sequence[str],
    store_cells: Sequence[str],
    live_seeds: Sequence[int | None],
    store_seeds: Sequence[int | None],
) -> dict[str, Any]:
    """Both cost proxies, named, with neither declared correct."""
    out: dict[str, Any] = {}
    if not control.exists():
        return out

    live_idx = _index(cost, live_cells, live_seeds)
    store_idx = _index(cost, store_cells, store_seeds)

    live_seen: list[int] = [s for _, _, s in live_idx if s is not None]
    store_seen: list[int] = [s for _, _, s in store_idx if s is not None]
    annotated = len(live_seen) == len(live_idx) and len(store_seen) == len(store_idx)
    if annotated:
        if sorted(live_seen) != sorted(store_seen):
            raise SystemExit(
                "cost-aware-replay: --cells seeds differ across arms: "
                f"live={live_seen} store={store_seen}"
            )
        pairing = "verified from the :seed annotations"
    else:
        pairing = (
            "ASSERTED BY POSITION -- these records all carry the same 'seed' "
            "field, so annotate --cells with id:seed to verify it"
        )

    trades, drag = [], []
    for (l_cell, ls, _), (s_cell, ss, _) in zip(live_idx, store_idx, strict=False):
        if ls.get("num_trades") and ss.get("num_trades"):
            trades.append(ss["num_trades"] / ls["num_trades"])
        lc, sc = _load(control, l_cell), _load(control, s_cell)
        if lc and sc and None not in (lc.get("final_equity"), sc.get("final_equity")):
            l_drag = lc["final_equity"] - ls["final_equity"]
            s_drag = sc["final_equity"] - ss["final_equity"]
            if l_drag:
                drag.append(s_drag / l_drag)

    out["_pairing"] = pairing
    if trades:
        out["trades"] = {
            "median": st.median(trades),
            "per_pair": trades,
            "assumes": "fixed notional -- cost scales with trade count",
        }
    if drag:
        out["equity_drag"] = {
            "median": st.median(drag),
            "per_pair": drag,
            "assumes": "fractional equity on a compounding curve",
            "caveat": "seed 44's control equity is the +1800% seed, so its drag "
            "spans a ~19x notional; quote the median, not the mean",
        }
    return out


def _bars_per_reentry(records: Path, cells: Sequence[str]) -> dict[str, Any]:
    vals = []
    for c in cells:
        rec = _load(records, c)
        if rec and rec.get("num_trades"):
            vals.append(rec["n_bars"] / rec["num_trades"])
    return {"median": st.median(vals) if vals else None, "per_seed": vals}


def build(
    records: Path,
    control: Path | None,
    cells: Mapping[str, Sequence[str]],
    seeds: Mapping[str, Sequence[int | None]],
) -> dict:
    """The full result table, from the shipped estimator only."""
    arms = list(cells)
    if any(len(cells[a]) != len(seeds.get(a, ())) for a in arms):
        raise SystemExit("cost-aware-replay: seed list length must match --cells")
    if len(arms) != 2:
        raise SystemExit(f"cost-aware-replay: need exactly two arms, got {arms}")

    rows = []
    for metric in DEFAULT_METRICS:
        medians: dict[str, float | None] = {
            a: _median(records, cells[a], metric) for a in arms
        }
        first, second = medians[arms[0]], medians[arms[1]]
        if first is None or second is None:
            rows.append({"metric": metric, "error": "a median is missing"})
            continue
        group_rows = [r for a in arms for r in _group_rows(records, cells[a], metric)]
        pooled = pooled_within_spread(group_rows)
        gap = abs(second - first)
        ratio = (gap / pooled) if pooled else None
        # The gate sits behind the count gate: without the replicate count,
        # dispersion_verdict returns UNDEFINED by design (a 1-replicate group
        # has q3-q1 == 0 and would otherwise bless any gap as RESOLVED).
        replicates = min(
            (_n_valid(records, cells[a], metric) for a in arms), default=0
        )
        verdict = dispersion_verdict(gap, pooled, replicates=replicates)

        row = {
            "metric": metric,
            "medians": medians,
            "gap": gap,
            "pooled_iqr": pooled,
            "ratio": verdict.get("ratio"),
            "replicates": replicates,
            "verdict": verdict.get("verdict"),
            "verdict_reason": verdict.get("reason"),
        }
        if control is not None:
            row["control_medians"] = {
                a: _median(control, cells[a], metric) for a in arms
            }
        rows.append(row)

    payload: dict[str, Any] = {
        "threshold": DISPERSION_RATIO_THRESHOLD,
        "min_replicates": MIN_REPLICATES_FOR_A_CLAIM,
        "arms": arms,
        "cells": {a: list(cells[a]) for a in arms},
        "metrics": rows,
        "bars_per_reentry": {a: _bars_per_reentry(records, cells[a]) for a in arms},
    }
    if control is not None and control.exists():
        payload["cost_proxies"] = _cost_proxies(
            records,
            control,
            cells[arms[0]],
            cells[arms[1]],
            seeds[arms[0]],
            seeds[arms[1]],
        )
    return payload


def render(payload: dict) -> str:
    out = [
        f"cost-aware-replay  threshold={payload['threshold']}"
        f"  min_replicates={payload['min_replicates']}  (both from model_matrix)",
        f"  arms: {' vs '.join(payload['arms'])}"
        f"   cells: " + ", ".join(f"{a}={len(v)}" for a, v in payload["cells"].items()),
        "",
        f"  {'metric':16s} {'live':>12s} {'store':>12s} {'gap':>10s} "
        f"{'pooled':>10s} {'ratio':>9s}  verdict",
    ]
    for r in payload["metrics"]:
        if r.get("error"):
            out.append(f"  {r['metric']:16s} ERROR: {r['error']}")
            continue
        a, b = payload["arms"]
        out.append(
            f"  {r['metric']:16s} {r['medians'][a]:>12.4f} {r['medians'][b]:>12.4f} "
            f"{r['gap']:>10.4f} {r['pooled_iqr']:>10.4f} "
            f"{(r['ratio'] if r['ratio'] is not None else float('nan')):>9.4f}  "
            f"{r['verdict']}"
        )
    out.append("")
    for arm, v in payload["bars_per_reentry"].items():
        out.append(
            f"  bars-per-re-entry {arm:6s} median={v['median']:.3f}  "
            f"per-seed={[round(x, 3) for x in v['per_seed']]}"
        )
    if "cost_proxies" in payload:
        out.append("")
        out.append("  cost ratio -- NOT recomputable (no notional in the records); two proxies:")
        for name, p in payload["cost_proxies"].items():
            if name.startswith("_"):
                continue
            out.append(
                f"    {name:12s} median={p['median']:>7.1f}x  per-pair="
                f"{[round(x, 1) for x in p['per_pair']]}  assumes {p['assumes']}"
            )
            if "caveat" in p:
                out.append(f"    {'':12s} caveat: {p['caveat']}")
        out.append(f"    pairing: {payload['cost_proxies'].get('_pairing')}")
        out.append("    -> neither proxy is presented as the true ratio")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or __file__).split("\n")[0])
    p.add_argument("--records", required=True, type=Path, help="cost-basis record dir")
    p.add_argument("--control-records", type=Path, help="frictionless control dir")
    p.add_argument(
        "--cells",
        action="append",
        required=True,
        metavar="ARM=id1[:seed1],id2,id3",
        help=(
            "repeat once per arm; arm order sets the reported columns. "
            "Annotate id:seed when the records cannot prove their own pairing"
        ),
    )
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    cells: dict[str, list[str]] = {}
    seeds: dict[str, list[int | None]] = {}
    for spec in a.cells:
        if "=" not in spec:
            raise SystemExit(f"cost-aware-replay: --cells needs ARM=ids, got {spec!r}")
        arm, ids = spec.split("=", 1)
        ids = [x for x in ids.split(",") if x]
        parsed: list[int | None] = []
        for x in ids:
            cell, _, seed = x.partition(":")
            ids[ids.index(x)] = cell
            parsed.append(int(seed) if seed else None)
        cells[arm] = ids
        seeds[arm] = parsed

    if not a.records.exists():
        print(f"cost-aware-replay: records dir not found: {a.records}", file=sys.stderr)
        return 2

    payload = build(a.records, a.control_records, cells, seeds)
    if a.json:
        blob = json.dumps(payload, indent=2, sort_keys=True, default=str)
        print(json.dumps({"receipt_sha256": hashlib.sha256(blob.encode()).hexdigest()}))
        print(blob)
        return 0
    print(render(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())