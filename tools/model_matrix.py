#!/usr/bin/env python3
"""Benchmark-matrix harness for the RL pipeline.

A *matrix* is a YAML spec describing a cartesian product of axes
(tickers x seeds x friction x ...). This tool turns it into evidence:

    model_matrix.py plan   SPEC   expand the design, warn when it cannot
                                 support the comparison being implied
    model_matrix.py run    SPEC   execute the cells, one JSONL record each
    model_matrix.py report SPEC   aggregate into per-axis medians + IQR

Why it exists: a single PPO run is not a measurement. The same config has
been measured replaying 576 trades and 374 (see .data-audit/VALIDATION.md
§2.1), so a naive "run N configs, tabulate returns" script reports noise
as a result. The three things this tool refuses to do:

1. **Reduce silently.** A multi-seed cell group is reported as
   ``median [q1, q3]``, never as a mean and never as one run.
2. **Tabulate a degenerate cell.** A run can exit 0 having replayed ONE
   bar out of 721 with ZERO trades. That is a pass-shaped failure: the
   feature-width guard passes, and "0% return" looks like a real result.
   Such cells are marked INVALID, listed, and excluded from every
   aggregate. See :func:`assess_cell`.
3. **Compare incomparable cells.** ``train`` and ``backtest`` each fetch
   data; two fetches have been measured to disagree 16% on a fitted
   feature std. Unless the spec pins ``data_window``, the cells are not
   comparable and ``plan`` says so loudly rather than letting ``report``
   print a comparison it cannot support.

Nothing here imports the RL package: the harness drives the documented
``kraken-trading-bot train/backtest --json`` CLI contract, so it keeps
working when the RL internals move.

The report ADJUDICATES those comparisons, not just tabulates them: the
CAND-5 dispersion gate scores each adjacent arm-pair by
``|median_A - median_B|`` over the pooled within-group IQR and prints a
RATIO.  ``NOT SEPARATED`` — the gap is smaller than the seed noise
inside an arm — is a normal, non-failing outcome; see
:func:`dispersion_verdict`.

Spec shape (see configs/matrix.example.yaml for the worked example):

    name: my-matrix
    base_config: configs/default.yaml
    results: /tmp/ktb-matrix/cells.jsonl
    models_root: /tmp/ktb-matrix/models
    min_bar_ratio: 0.5
    axes:
      ticker: [ETH_USD, SOL_USD]
      seed: [42, 43, 44]
      friction:
        - {fee_rate: 0.0, slippage: 0.0}
        - {fee_rate: 0.0026, slippage: 0.0005}
      action_space: [discrete]
      data_window:
        - {since: "2026-09-01T00:00:00Z", until: "2026-09-15T00:00:00Z", eval_split: 0.7}

Reserved axes (``ticker``, ``seed``, ``pages``, ``timesteps``) become CLI
flags; every other axis is deep-merged into the generated per-cell config.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "BACKTEST_EXPRESSIBLE_KEYS",
    "DISPERSION_NOT_SEPARATED",
    "DISPERSION_RATIO_THRESHOLD",
    "DISPERSION_RESOLVED",
    "DISPERSION_UNDEFINED",
    "TRAIN_ONLY_CONFIG_KEYS",
    "Cell",
    "MatrixSpec",
    "append_record",
    "assess_cell",
    "cell_id",
    "cell_is_out_of_sample",
    "classify_process_failure",
    "cmd_plan",
    "cmd_report",
    "cmd_run",
    "deep_merge",
    "describe_dispersion",
    "dispersion_verdict",
    "done_cell_ids",
    "expected_bars_for",
    "expand_cells",
    "is_valid",
    "load_records",
    "load_spec",
    "main",
    "materialize_configs",
    "pooled_within_spread",
    "replicated_groups",
    "split_cell_overrides",
    "span_bars_for",
    "summarize",
    "window_has_split",
    "window_is_pinned",
]

# ── Contract constants ──────────────────────────────────────────────────

#: Axis names that map onto a CLI flag rather than a config-file key.
#: Everything else in ``axes:`` is deep-merged into the per-cell config.
RESERVED_AXES: frozenset[str] = frozenset(
    {"ticker", "seed", "pages", "timesteps"}
)

#: Train fraction the RL side applies when a config names ``data_window``
#: without an ``eval_split``. Mirrors ``rl.data_window.DEFAULT_EVAL_SPLIT``
#: and ``configs/default.yaml``; used only to size the width denominator.
DEFAULT_EVAL_SPLIT = 0.7

#: The backtest JSON contract this tool codes against. A record missing any
#: of these cannot be assessed, so it is INVALID rather than silently passed:
#: "the field is absent" and "the field is zero" must not look alike.
#:
#: 18 keys, per the RL side's ``BacktestResult.to_dict()``: the original 12
#: plus ``buy_hold_return``, ``excess_return``, ``buy_hold_max_drawdown``,
#: ``n_bars``, ``fee_rate``, ``slippage`` and ``action_space``.
REQUIRED_BACKTEST_FIELDS: tuple[str, ...] = (
    "ticker_id",
    "model_name",
    "action_space",
    "total_return",
    "sharpe",
    "max_drawdown",
    "num_trades",
    "win_rate",
    "equity_curve",
    "n_steps",
    "final_equity",
    "seed",
    "buy_hold_return",
    "excess_return",
    "buy_hold_max_drawdown",
    "n_bars",
    "fee_rate",
    "slippage",
)

#: Fields that may legitimately be ``null`` for a pre-provenance artifact
#: (a model trained before widths were recorded). ``null`` means UNKNOWN,
#: never 0 — so a null here is INVALID rather than a pass, but it is
#: reported as its own reason code so the cause is legible.
NULLABLE_BACKTEST_FIELDS: frozenset[str] = frozenset({"n_bars"})

#: The RL config keys that name an exogenous-signal file.  Mirrors
#: ``kraken_trading_bot.rl.data._SIGNAL_CHANNELS``, which is the definition
#: on the producing side; a refusal from that seam always opens with one of
#: these keys followed by " is set to" (see ``classify_process_failure``).
#: Spelled out here rather than imported because this harness deliberately
#: matches the *text the CLI prints*, not Python internals — the RL package
#: is not a dependency of ``tools/model_matrix.py``.
_SIGNAL_CONFIG_KEYS: tuple[str, ...] = (
    "extra_features_file",
    "funding_features_file",
    "social_features_file",
)

#: Metrics that must be finite for a cell to count as a measurement.
NUMERIC_METRICS: tuple[str, ...] = (
    "total_return",
    "sharpe",
    "max_drawdown",
    "win_rate",
    "final_equity",
    "buy_hold_return",
    "excess_return",
    "buy_hold_max_drawdown",
)

#: How many equity-curve points to KEEP per cell. The full curve is ~4 KB
#: of floats; retaining all of it across a matrix would bloat the JSONL
#: without informing any aggregate this tool computes.
EQUITY_CURVE_POINTS = 16

#: Exactly the config keys a ``backtest --config PATH`` honours. Anything
#: outside this set cannot be expressed to a backtest at all.
BACKTEST_EXPRESSIBLE_KEYS: frozenset[str] = frozenset(
    {
        "fee_rate",
        "slippage",
        "action_space",
        "initial_balance",
        "market_data_store",
        "data_window",
    }
)

#: Config keys that are TRAIN-SIDE ONLY. A backtest re-reads these from the
#: MODEL's own training config, because a mismatch there is
#: silent-corruption class: the policy was fitted against one feature
#: layout and replaying against another produces numbers that mean
#: nothing. An axis touching any of these therefore cannot be expressed as
#: a backtest config at all -- the harness must write a per-cell TRAIN
#: config and retrain for every level.
TRAIN_ONLY_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "reward",
        "allow_short",
        "feature_groups",
        "feature_windows",
        "extra_features_file",
        "funding_features_file",
        "social_features_file",
        "signal_max_age_hours",
        "signal_require_ticker",
        "ohlcv_interval_minutes",
    }
)

#: How each reported metric is formatted. ``count`` metrics are whole
#: numbers (a trade count printed as ``42.000`` reads like a bug).
METRIC_KINDS: dict[str, str] = {
    "excess_return": "pct",
    "buy_hold_return": "pct",
    "total_return": "pct",
    "max_drawdown": "pct",
    "sharpe": "num",
    "num_trades": "count",
    "n_bars": "count",
    "win_rate": "pct",
}

#: The metric the report leads with. A raw return is meaningless without
#: the buy-and-hold reference it has to beat.
HEADLINE = "excess_return"

#: Kraken's REST ceiling for a pair/interval is ~720 candles, and it does
#: NOT scale with ``--pages``: ``--pages 2`` has been measured returning
#: 721 bars, not 1440. So "expected bars = pages * 720" is a wrong
#: denominator that would make a healthy cell look truncated. When no
#: window is pinned the harness says "width unchecked" rather than invent
#: one.
KRAKEN_REST_BAR_CEILING = 720

#: Below this many valid replicates a median is reported but explicitly
#: labelled as not separating from noise.
MIN_REPLICATES_FOR_A_CLAIM = 3


# ── Small helpers ───────────────────────────────────────────────────────


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Any) -> str:
    """Stable JSON text for hashing/grouping arbitrary YAML values."""
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``.

    Nested dicts (``data_window``) merge key-wise so a friction override
    of ``{fee_rate: 0.0026}`` does not wipe a pinned ``data_window``.
    """
    out = dict(base)
    for key, value in override.items():
        existing = out.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            out[key] = deep_merge(dict(existing), value)
        else:
            out[key] = value
    return out


def label(value: Any) -> str:
    """Short human label for an axis level (dict levels become k=v pairs)."""
    if isinstance(value, Mapping):
        inner = ",".join(
            f"{k}={_fmt_scalar(value[k])}" for k in sorted(value, key=str)
        )
        return "{" + inner + "}"
    return _fmt_scalar(value)


def _fmt_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def median(values: Sequence[float]) -> float | None:
    """Median of ``values`` (None when empty)."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def quartile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation quantile (the numpy/'inclusive' method).

    Kept local so ``tools/`` imports on stdlib + pyyaml alone.
    """
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[int(pos)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def summarize(values: Sequence[float]) -> dict[str, float | int | None]:
    """Median + IQR + range for one group of replicates."""
    finite = [float(v) for v in values if _finite(v)]
    return {
        "n": len(finite),
        "median": median(finite),
        "q1": quartile(finite, 0.25),
        "q3": quartile(finite, 0.75),
        "min": min(finite) if finite else None,
        "max": max(finite) if finite else None,
    }


# ── CAND-5: the dispersion gate ────────────────────────────────────────────
#
# A median difference between two arms is not a finding.  What makes one
# a finding is that the gap between the arms is large *relative to the
# spread inside* an arm — the seed noise the replicates measure.  The gate
# therefore compares a RATIO, never a boolean: ``gap / pooled``, where the
# yardstick is the median within-group IQR over the groups that have
# enough replicates to have an IQR worth quoting.
#
# Why the median and not the max:  with three replicates per arm, one arm
# whose seeds happen to disagree badly (a crash-recovery run, a lucky
# trade) drags a ``max`` yardstick past the gap and flips every verdict to
# NOT SEPARATED.  That was measured, not hypothesised — the worked case 1
# below (gap 4pp, pooled IQR 0.075pp, 53x) collapses to 0.21x under the
# same data with a max.  The median is robust to that one group; ``mean``
# is not.

#: Ratio at or above which a pair is reported RESOLVED.  This is a
#: JUDGEMENT CALL, not a fact: the ratio is the fact, and a reader who
#: disagrees with the threshold can read the ratio off the same output.
DISPERSION_RATIO_THRESHOLD = 1.0

#: Verdict labels.  ``NOT_SEPARATED`` is a first-class, NON-failing
#: outcome: it says the between-arm gap is smaller than the noise inside
#: an arm, which is a measurement, not an error.  Nothing in this module
#: exits non-zero because of it.
DISPERSION_RESOLVED = "RESOLVED"
DISPERSION_NOT_SEPARATED = "NOT SEPARATED"
DISPERSION_UNDEFINED = "UNDEFINED"


def replicated_groups(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Rows whose replicate count clears :data:`MIN_REPLICATES_FOR_A_CLAIM`.

    The keep-list for every dispersion number: a group below the count
    gate has no IQR worth quoting (at n=2 the "IQR" is just the gap
    between the two seeds), so counting it would let thin groups set the
    yardstick that thick groups are then judged against.
    """
    kept = []
    for row in rows or ():
        summary = (row.get("summary") or {}) if isinstance(row, Mapping) else {}
        try:
            n = int(summary.get("n") or 0)
        except (TypeError, ValueError):
            n = 0
        if n >= MIN_REPLICATES_FOR_A_CLAIM:
            kept.append(row)
    return kept


def _group_iqr(row: Mapping[str, Any]) -> float | None:
    """One group's ``q3 - q1``, or None when either quartile is absent."""
    summary = (row.get("summary") or {}) if isinstance(row, Mapping) else {}
    q1, q3 = summary.get("q1"), summary.get("q3")
    if not _finite(q1) or not _finite(q3):
        return None
    return float(q3) - float(q1)


def pooled_within_spread(rows: Sequence[Mapping[str, Any]]) -> float | None:
    """The dispersion-gate yardstick: MEDIAN within-group ``q3 - q1``.

    Taken over the groups that clear :data:`MIN_REPLICATES_FOR_A_CLAIM`,
    because that is the same set the gate is allowed to speak about.

    Args:
        rows: Group rows as produced by :func:`_group_summaries` (each
            with a ``summary`` mapping holding ``n``/``q1``/``q3``).

    Returns:
        The pooled spread, or ``None`` when fewer than two such groups
        exist (one arm has no yardstick to be measured against) or when
        the median comes out at exactly ``0`` — a zero denominator would
        make every ratio infinite, i.e. would bless whatever gap it was
        handed.
    """
    spreads = [
        iqr for iqr in (_group_iqr(row) for row in replicated_groups(rows)) if iqr is not None
    ]
    if len(spreads) < 2:
        return None
    pooled = median(spreads)
    if pooled is None or not pooled > 0.0:
        return None
    return pooled


def dispersion_verdict(
    gap: float | None,
    pooled: float | None,
    *,
    replicates: int | None = None,
    threshold: float = DISPERSION_RATIO_THRESHOLD,
) -> dict[str, Any]:
    """Adjudicate one arm-pair against the pooled within-arm spread.

    The gate sits BEHIND the count gate, and that placement is the whole
    point.  At one replicate per arm a group has ``q3 - q1 == 0``, so a
    ratio-of-gap-to-spread gate would report ``5pp / 0 = inf`` and call
    the pair RESOLVED — blessing precisely the anecdote the surrounding
    prose exists to kill.  So nothing is emitted until BOTH hold: the
    contributing groups have ``n >= MIN_REPLICATES_FOR_A_CLAIM`` **and**
    the pooled spread is positive.

    Args:
        gap: ``abs(median_A - median_B)`` between two adjacent arms.
        pooled: :func:`pooled_within_spread` over the cohort, or None.
        replicates: Smallest replicate count among the contributing
            groups; ``None`` when unknown.
        threshold: Ratio at/above which the pair reads RESOLVED.

    Returns:
        ``{"verdict", "ratio", "gap", "pooled", "replicates", "reason"}``.
        ``ratio`` is ``None`` for UNDEFINED — no number is invented when
        there is no yardstick.
    """
    reason = ""
    if replicates is None or int(replicates) < MIN_REPLICATES_FOR_A_CLAIM:
        reason = (
            f"fewer than {MIN_REPLICATES_FOR_A_CLAIM} replicates in the "
            "contributing groups"
        )
    elif pooled is None:
        reason = (
            "no positive pooled spread (needs >= 2 groups with "
            f"n >= {MIN_REPLICATES_FOR_A_CLAIM}, and a non-zero IQR)"
        )
    elif not _finite(gap):
        reason = "a missing arm median"

    if reason:
        return {
            "verdict": DISPERSION_UNDEFINED,
            "ratio": None,
            "gap": float(gap) if _finite(gap) else None,
            "pooled": pooled,
            "replicates": replicates,
            "reason": reason,
        }

    ratio = abs(float(gap)) / float(pooled)  # type: ignore[arg-type]
    return {
        "verdict": (
            DISPERSION_RESOLVED if ratio >= threshold else DISPERSION_NOT_SEPARATED
        ),
        "ratio": ratio,
        "gap": abs(float(gap)),
        "pooled": float(pooled),  # type: ignore[arg-type]
        "replicates": replicates,
        "reason": "",
    }


def describe_dispersion(
    lower: Mapping[str, Any],
    upper: Mapping[str, Any],
    pooled: float | None,
    *,
    replicates: int | None = None,
    threshold: float = DISPERSION_RATIO_THRESHOLD,
    kind: str = "pct",
) -> str:
    """One human line for an adjacent arm-pair, ratio first."""
    gap = None
    med_a = (lower.get("summary") or {}).get("median")
    med_b = (upper.get("summary") or {}).get("median")
    if _finite(med_a) and _finite(med_b):
        gap = abs(float(med_a) - float(med_b))
    result = dispersion_verdict(
        gap, pooled, replicates=replicates, threshold=threshold
    )
    fmt = _fmt_for(kind)
    pair = f"{lower.get('label')} vs {upper.get('label')}"
    ratio = result["ratio"]
    ratio_text = "ratio n/a" if ratio is None else f"ratio {ratio:.2f}x"
    if result["verdict"] == DISPERSION_UNDEFINED:
        return (
            f"{pair}: UNDEFINED ({result['reason']}) — {ratio_text}; "
            f"gap {fmt(gap)}, pooled within-group IQR {fmt(pooled)}"
        )
    return (
        f"{pair}: {result['verdict']} ({ratio_text}; gap {fmt(gap)} vs "
        f"pooled within-group IQR {fmt(pooled)})"
    )


def fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.{digits}f}%"


def fmt_num(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def fmt_count(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.0f}"


def _fmt_for(kind: str):
    return {"pct": fmt_pct, "num": fmt_num, "count": fmt_count}.get(kind, fmt_num)


def fmt_iqr(stat: Mapping[str, Any], kind: str = "pct") -> str:
    fmt = _fmt_for(kind)
    return f"{fmt(stat.get('median'))} [{fmt(stat.get('q1'))}, {fmt(stat.get('q3'))}]"


def parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` suffix allowed)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# ── Spec ────────────────────────────────────────────────────────────────


@dataclass
class MatrixSpec:
    """A parsed matrix specification."""

    name: str
    path: Path
    base_config: Path | None = None
    models_root: Path | None = None
    results: Path = Path("cells.jsonl")
    axes: dict[str, list[Any]] = field(default_factory=dict)
    min_bar_ratio: float = 0.5
    min_trades: int = 1
    interval_minutes: int = 60
    timeout: float | None = None
    train_seconds: float = 60.0
    backtest_seconds: float = 15.0
    question: str = ""
    base_config_present: dict[str, bool] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def axis_names(self) -> list[str]:
        return list(self.axes)

    @property
    def config_axes(self) -> list[str]:
        return [a for a in self.axes if a not in RESERVED_AXES]

    def has_axis(self, *names: str) -> bool:
        return any(n in self.axes for n in names)

    def levels(self, axis: str) -> list[Any]:
        return list(self.axes.get(axis, []))


def _as_path(value: Any, base: Path, *, existing: bool = False) -> Path | None:
    """Resolve a spec path value.

    Absolute paths are used verbatim. Relative paths resolve against the
    CWD first, then the spec's own directory. CWD-first matters because
    repo-relative paths are the norm here: a spec under ``configs/``
    saying ``base_config: configs/default.yaml`` would otherwise resolve
    to ``configs/configs/default.yaml``.

    ``existing=True`` marks an INPUT path: when neither candidate exists
    the CWD one is kept, so ``plan`` can report the path it actually
    probed. Outputs (``results``, ``models_root``) do not exist yet, so
    they always resolve against the CWD — that is where a run writes.
    """
    if value in (None, ""):
        return None
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    from_cwd = Path.cwd() / path
    if not existing or from_cwd.exists():
        return from_cwd
    return base / path


def load_spec(path: str | Path) -> MatrixSpec:
    """Read and validate a matrix spec.

    Raises:
        ValueError: the file is unparseable, has no axes, or an axis value
            list is empty.
    """
    spec_path = Path(path).expanduser().resolve()
    with spec_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{spec_path}: top level must be a mapping")

    base_dir = spec_path.parent
    axes_raw = raw.get("axes") or {}
    if not isinstance(axes_raw, Mapping) or not axes_raw:
        raise ValueError(f"{spec_path}: 'axes' must be a non-empty mapping")

    axes: dict[str, list[Any]] = {}
    for axis in axes_raw:
        values = axes_raw[axis]
        if isinstance(values, (str, int, float, bool, Mapping)):
            values = [values]
        if not isinstance(values, list) or not values:
            raise ValueError(
                f"{spec_path}: axis {axis!r} must list at least one value"
            )
        axes[str(axis)] = list(values)

    run = raw.get("run") or {}
    if not isinstance(run, Mapping):
        raise ValueError(f"{spec_path}: 'run' must be a mapping")

    base_config = _as_path(
        run.get("base_config", raw.get("base_config")), base_dir, existing=True
    )
    results = _as_path(
        run.get("results", raw.get("results")), base_dir
    ) or Path("cells.jsonl")
    models_root = _as_path(
        run.get("models_root", raw.get("models_root")), base_dir
    )

    present: dict[str, bool] = {}
    base_raw: dict[str, Any] = {}
    if base_config is not None and base_config.is_file():
        with base_config.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        base_raw = dict(loaded) if isinstance(loaded, Mapping) else {}
        for key in (
            "fee_rate",
            "slippage",
            "action_space",
            "data_window",
            "market_data_store",
            "ohlcv_interval_minutes",
        ):
            present[key] = key in base_raw
        interval = base_raw.get("ohlcv_interval_minutes", 60)
    else:
        # A missing base config is reported in plan output, not a crash:
        # the harness may be pointed at a config the other builder lands
        # later. Cells then run against the CLI's own defaults.
        interval = 60

    return MatrixSpec(
        name=str(raw.get("name") or spec_path.stem),
        path=spec_path,
        base_config=base_config,
        models_root=models_root,
        results=results,
        axes=axes,
        min_bar_ratio=float(run.get("min_bar_ratio", 0.5)),
        min_trades=int(run.get("min_trades", 1)),
        interval_minutes=int(run.get("interval_minutes", interval) or 60),
        timeout=(float(run["timeout"]) if run.get("timeout") else None),
        train_seconds=float(run.get("train_seconds", 60.0)),
        backtest_seconds=float(run.get("backtest_seconds", 15.0)),
        question=str(raw.get("question") or raw.get("description") or ""),
        base_config_present=present,
        raw=raw,
    )


# ── Expansion ───────────────────────────────────────────────────────────


def cell_id(params: Mapping[str, Any]) -> str:
    """Deterministic 12-hex id for a cell's parameter tuple.

    Stable across runs and across machines: the id is a hash of the
    canonical JSON of the parameters, never of the index or the timestamp,
    so a re-run after a crash recognises and skips finished cells.
    """
    return hashlib.sha256(_canonical(params).encode("utf-8")).hexdigest()[:12]


def expected_bars_for(
    config_overrides: Mapping[str, Any],
    pages: Any = None,
    interval_minutes: int = 60,
) -> int | None:
    """Bars the cell ASKED FOR, or None when that cannot be derived.

    A pinned ``data_window`` (since + until) is the only trustworthy
    denominator: it states the span. ``pages`` is deliberately NOT used --
    ``--pages 2`` has been measured returning 721 bars, not 1440, so a
    ``pages * 720`` guess would flag healthy cells as truncated. Returns
    None instead, which makes the width check explicitly skipped rather
    than silently wrong.

    With an active ``eval_split`` the denominator is the **eval slice**,
    not the whole span, because that is what the backtest is asked to
    replay. ``n_bars`` counts bars REPLAYED, and with ``eval_split: 0.7``
    the RL side gives training the leading 70% and replays only the
    remainder -- so a healthy cell returns ~0.3x the span, not 1.0x.

    Measured against the real CLI (2026-10-01, ETH_USD, pages 4):
    a 672-bar window logged "202 replayable bars", of which 178 were
    replayed after feature warm-up. Denominating by the full 672 demanded
    336 and marked every out-of-sample cell INVALID, so a correctly
    out-of-sample matrix reported zero usable cells. The split boundary is
    taken with the same ``round(n * split)`` clamped to ``[1, n - 1]`` the
    RL side uses, so the two sides cannot disagree by a bar.

    This narrows the denominator to the segment actually under test; it
    does NOT relax the guard -- a cell replaying 1 bar of 202 is still
    INVALID, which is exactly the failure the guard exists to catch.
    """
    window = config_overrides.get("data_window")
    bars = span_bars_for(config_overrides, interval_minutes)
    if bars:
        cut = _split_boundary(config_overrides, bars)
        if cut is not None:
            return max(1, bars - cut)
        return bars
    return None


def span_bars_for(
    config_overrides: Mapping[str, Any], interval_minutes: int = 60
) -> int | None:
    """Bars in the WHOLE pinned ``data_window`` span, or None.

    Distinct from :func:`expected_bars_for`, which narrows that span to the
    eval slice when a split is active. This one answers "how far must the
    fetch reach to cover the window", which is what the ``--pages``
    undershoot warning is about -- so it must stay the full span.
    """
    window = config_overrides.get("data_window")
    if not isinstance(window, Mapping):
        return None
    since = parse_iso(window.get("since"))
    until = parse_iso(window.get("until"))
    if not (since and until):
        return None
    span = (until - since).total_seconds()
    seconds_per_bar = max(1, int(interval_minutes)) * 60
    bars = int(span // seconds_per_bar)
    return bars if bars > 0 else None


def _split_boundary(
    config_overrides: Mapping[str, Any], n_bars: int
) -> int | None:
    """Number of leading TRAINING bars, or None when no split applies.

    Mirrors the RL side's ``rl.data_window.split_index``: ``round(n *
    eval_split)`` clamped to ``[1, n - 1]``, and inert unless the window is
    actually pinned (``eval_split`` below 1.0 on a null window is
    deliberately ignored there, and is ignored here for the same reason).
    """
    if not window_has_split(config_overrides):
        return None
    window = config_overrides.get("data_window") or {}
    raw = window.get("eval_split")
    if isinstance(raw, bool):
        return None
    try:
        split = float(raw) if raw is not None else DEFAULT_EVAL_SPLIT
    except (TypeError, ValueError):
        return None
    # ``eval_split: 1.0`` means NO split on the RL side (DataWindow.has_split
    # requires < 1.0), so the backtest replays the whole window. Honouring it
    # as a split here would size the denominator at 1 bar and let a
    # near-empty replay pass the width guard.
    if not (0.0 < split < 1.0) or n_bars < 2:
        return None
    return min(max(int(round(n_bars * split)), 1), n_bars - 1)


@dataclass(frozen=True)
class Cell:
    """One point in the matrix."""

    index: int
    cell_id: str
    params: dict[str, Any]
    cli_params: dict[str, Any]
    config_overrides: dict[str, Any]
    expected_bars: int | None
    model_name: str

    @property
    def ticker(self) -> str:
        return str(self.params.get("ticker", ""))


def _merge_axis_levels(
    cli: dict[str, Any], config: dict[str, Any], axis: str, value: Any
) -> None:
    """Place one axis level into either the CLI flags or the config.

    Rules, in order:

    1. A reserved axis name (``ticker``/``seed``/``pages``/``timesteps``)
       becomes a CLI flag.
    2. An axis named ``.key`` nests its dict level under ``key`` — the
       leading dot says "this axis IS a config key". So
       ``.data_window: [{since, until, eval_split}]`` writes
       ``data_window: {since, until, eval_split}``.
    3. Any other dict level merges FLAT, because an axis whose name is a
       label rather than a key must be able to set several keys at once:
       ``friction: [{fee_rate, slippage}]`` writes top-level ``fee_rate``
       and ``slippage``.
    4. A scalar level writes ``{axis: value}``.
    """
    if axis in RESERVED_AXES:
        cli[axis] = value
        return
    if axis.startswith(".") and len(axis) > 1:
        payload = {axis[1:]: value}
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        payload = {axis: value}
    config.update(deep_merge(config, payload))


def expand_cells(spec: MatrixSpec) -> list[Cell]:
    """Cartesian product of every axis, in sorted-axis-name order.

    Sorted so the enumeration (and therefore the cell ids) is stable no
    matter how the YAML happens to be ordered.
    """
    names = sorted(spec.axes)
    combos: list[dict[str, Any]] = [{}]
    for axis in names:
        nxt: list[dict[str, Any]] = []
        for prefix in combos:
            for value in spec.axes[axis]:
                merged = dict(prefix)
                merged[axis] = value
                nxt.append(merged)
        combos = nxt

    cells: list[Cell] = []
    for index, params in enumerate(combos):
        cli_params: dict[str, Any] = {}
        config_overrides: dict[str, Any] = {}
        for axis in sorted(params):
            _merge_axis_levels(cli_params, config_overrides, axis, params[axis])
        cid = cell_id(params)
        model_name = str(
            config_overrides.pop("model_name", None)
            or cli_params.get("model_name")
            or f"mtx_{cid}"
        )
        cells.append(
            Cell(
                index=index,
                cell_id=cid,
                params=params,
                cli_params=cli_params,
                config_overrides=config_overrides,
                expected_bars=expected_bars_for(
                    config_overrides, cli_params.get("pages"), spec.interval_minutes
                ),
                model_name=model_name,
            )
        )
    return cells


# ── Degenerate-cell detection ───────────────────────────────────────────


def window_is_pinned(config_overrides: Mapping[str, Any]) -> bool:
    """True only when BOTH ``data_window.since`` and ``.until`` are set.

    This is the RL side's ``DataWindow.is_pinned``. It gates everything
    downstream: with a null window the run behaves exactly as it did
    before windowing existed.
    """
    window = config_overrides.get("data_window")
    if not isinstance(window, Mapping):
        return False
    since = window.get("since")
    until = window.get("until")
    # A key present but null counts as UNSET (RL-side precedence rule).
    return bool(since) and bool(until)


def window_has_split(config_overrides: Mapping[str, Any]) -> bool:
    """True when an ``eval_split`` is actually in play.

    CRITICAL: ``eval_split`` is INERT unless the window is pinned. With
    ``since``/``until`` null the run trains and backtests on 100% of the
    frame, so a cell that "sets eval_split" is still fully in-sample. This
    is the RL side's ``DataWindow.has_split``.
    """
    if not window_is_pinned(config_overrides):
        return False
    window = config_overrides.get("data_window") or {}
    split = window.get("eval_split")
    return split is not None


def cell_is_out_of_sample(config_overrides: Mapping[str, Any]) -> bool:
    """A cell is out-of-sample ONLY when pinned AND split.

    Anything else — including a cell that sets ``eval_split`` against an
    unpinned window — replayed the bars the model was fitted on, and its
    return describes the fit rather than a prediction.
    """
    return window_is_pinned(config_overrides) and window_has_split(
        config_overrides
    )


def assess_cell(
    record: Mapping[str, Any],
    *,
    min_bar_ratio: float = 0.5,
    min_trades: int = 1,
    expected_bars: int | None = None,
    reference_bars: int | None = None,
) -> list[str]:
    """Reasons this cell is INVALID (empty list means it is a measurement).

    A cell that exits 0 is not automatically evidence. The worked example
    from .data-audit/VALIDATION.md §2.2 is a run that replayed **1 bar out
    of 721 with 0 trades**, silently, while the feature-width guard
    reported PASS -- tabulating that as "0% return" would look exactly
    like a real result.

    Args:
        record: One JSONL record written by ``run``.
        min_bar_ratio: Fraction of the requested window a cell must replay.
        min_trades: Minimum executed trades for the cell to be informative.
        expected_bars: What the cell asked for -- the pinned window's span
            narrowed to the eval slice when ``eval_split`` is active, since
            that (not the whole window) is what a backtest replays. See
            :func:`expected_bars_for`.
        reference_bars: Fallback denominator -- typically the largest
            ``n_bars`` seen for the same ticker in this results file. Every
            cell of one ticker requests the same window, so 1 bar next to
            697 is degenerate even with no pinned window.

    Returns:
        Sorted list of reason codes; empty when the cell is valid.
    """
    reasons: list[str] = []

    # RL-side refusals are failures, not results. Classified by name so a
    # matrix run surfaces WHY rather than an opaque rc=1.
    for code in classify_process_failure(record):
        reasons.append(code)

    backtest = record.get("backtest")
    if not isinstance(backtest, Mapping):
        if not reasons:
            reasons.append("no_backtest_json")
        return sorted(set(reasons))

    missing = [
        f for f in REQUIRED_BACKTEST_FIELDS if f not in backtest
    ]
    if missing:
        reasons.append("missing_field:" + ",".join(missing))
        # Without the contract's fields there is nothing left to check.
        return sorted(set(reasons))

    nulls = [
        f
        for f in NULLABLE_BACKTEST_FIELDS
        if backtest.get(f) is None
    ]
    if nulls:
        # null is UNKNOWN (pre-provenance artifact), never 0.
        reasons.append("null_field:" + ",".join(sorted(nulls)))

    n_bars = backtest.get("n_bars")
    try:
        n_bars_value = int(n_bars)
    except (TypeError, ValueError):
        n_bars_value = None

    # Width: the cell must have replayed what it asked for. The window
    # bounds are a CLIP on the read, not a push-down, so a too-small
    # `--pages` yields a silently SHORT window that only `n_bars` reveals.
    denominator = expected_bars if expected_bars else reference_bars
    if denominator and n_bars_value is not None:
        if n_bars_value < min_bar_ratio * denominator:
            reasons.append(
                f"n_bars:{n_bars_value}<{min_bar_ratio:.2f}x{denominator}"
            )

    trades = backtest.get("num_trades")
    try:
        trades_value = int(trades)
    except (TypeError, ValueError):
        trades_value = None
    if trades_value is None:
        reasons.append("no_trade_count")
    elif trades_value < min_trades:
        reasons.append(f"zero_trades:{trades_value}")

    bad = [m for m in NUMERIC_METRICS if not _finite(backtest.get(m))]
    if bad:
        reasons.append("nan_metric:" + ",".join(bad))

    return sorted(set(reasons))


def classify_process_failure(record: Mapping[str, Any]) -> list[str]:
    """Classify an RL-side refusal by name, so `run` reports WHY.

    ``--config`` pointing at a nonexistent path deliberately RAISES (so
    nobody silently falls back to a zero-cost model config); that plus the
    domain refusals below are failures to be surfaced, never retried
    around.

    Matching is on the message the real ``kraken-trading-bot`` CLI
    actually prints, not on the Python exception class name. Found on
    2026-10-01 against the real binary: ``cmd_backtest`` catches
    ``Exception`` and prints ``Error backtesting <t>/<m>: {e}``, so the
    class name never reaches stdout or stderr. Against the real CLI both
    refusals degraded to a bare ``process_failed`` — the exact opaque
    ``rc=1`` this function exists to avoid — because the synthetic fake
    had been feeding text containing ``FileNotFoundError`` and
    ``ActionSpaceMismatchError``. Both spellings are accepted here so a
    future traceback (or an uncaught raise) still classifies.

    ``signal_file_not_found`` is the one that matters at scale. Before the
    2026-10-02 signal-seam fix, a configured-but-unreadable signal file was
    a WARNING and the run *succeeded* on narrower features; that fix made it
    raise by name instead (see ``SignalFileNotFoundError``). Because the
    shipped ``configs/default.yaml`` sets ``funding_features_file``
    non-null, every cell of a matrix on a checkout without that file now
    fails the same way — which is precisely the whole-matrix "why is
    nothing working" shape that a bare ``process_failed`` cannot explain.
    """
    reasons: list[str] = []
    rc = (record.get("returncodes") or {}).get("backtest")
    status = record.get("status")
    if status != "error" and rc in (None, 0):
        return reasons

    text = " ".join(
        str(part)
        for part in (
            record.get("error"),
            " ".join(record.get("stderr_tail") or []),
        )
    )
    if status == "error" or rc not in (None, 0):
        reasons.append("process_failed")
    # Real CLI text: "Backtest config not found: <path>". Traceback text
    # would carry the class name instead.
    if (
        "FileNotFoundError" in text
        or "config not found" in text.lower()
        or "No such file or directory" in text
    ):
        reasons.append("config_not_found")
    # Real CLI text: "Action-space mismatch (<t>/<m> backtest): the model
    # was trained as 'continuous' but ...".
    if "ActionSpaceMismatchError" in text or "action-space mismatch" in text.lower():
        # The model's recorded action_space disagrees with the run's. An
        # UNRECORDED action_space (pre-provenance) is unproven, not a
        # mismatch, and the RL side treats it as such.
        reasons.append("action_space_mismatch")
    # PinnedWindowUnavailableError's real CLI text opens "Pinned
    # data_window [...] does not overlap the N bar(s) that were actually
    # read" -- it carries no class name (cli.py prints str(e) only) and
    # no "tradable bar" phrasing, so without this clause a pinned window
    # that missed the live ceiling is reported as an opaque process_failed
    # rather than the diagnosable data failure it is.
    if "does not overlap" in text.lower() or "PinnedWindowUnavailableError" in text:
        reasons.append("no_tradable_bar")
    if "NotEnoughDataError" in text or (
        "ValueError" in text and "tradable bar" in text
    ) or "no tradable bar" in text.lower():
        # A pinned window left no tradable bar (eval slice shorter than
        # the warm-up). It RAISES rather than returning n_bars: 0.
        reasons.append("no_tradable_bar")
    # Real CLI text: "funding_features_file is set to '~/...' but no file
    # exists at /home/... (expanded from '~/...').  Produce it with: just
    # funding-pull."  -- the seam's own two-state refusal (a null key is
    # silent, a SET key that cannot be used raises by name).  Matched on
    # "<config_key> is set to", which is how every message from
    # SignalFileNotFoundError opens, because that prefix is the one part
    # guaranteed to carry the key; the traceback spelling carries the class
    # name instead.  Deliberately NOT folded into config_not_found: a
    # missing --config and a missing signal file are different fixes (write
    # a YAML file vs run the producer), and conflating them would send a
    # reader to the wrong one.
    if "SignalFileNotFoundError" in text or any(
        f"{key} is set to" in text for key in _SIGNAL_CONFIG_KEYS
    ):
        reasons.append("signal_file_not_found")
    # SignalTickerMismatchError: the file EXISTS and parses, but every record
    # in it is tagged for a different pair — the mis-pointed-config case the
    # seam exists to refuse.  Its real-CLI text carries NO class name (cli.py
    # prints str(e) only), so it is matched on the message's stable fragments
    # rather than the spelling: "holds no records for" is the opener of
    # SignalTickerMismatchError's message and appears in no other refusal,
    # and the trailing `signal_require_ticker: false` is part of its remedy.
    #
    # Why it needs its own code rather than joining signal_file_not_found:
    # the two send the reader to OPPOSITE fixes.  A missing file means "run
    # the producer" (just news-pull).  A mismatch means the file is fine and
    # the *pointing* is wrong — you need that ticker's file, or
    # signal_require_ticker: false.  Conflating them would have a reader run
    # a producer that already ran and produce nothing.
    #
    # Measured red run before this clause (RED-GRUN §8, RG6 precedent — the
    # guard must be shown returning the WRONG answer first): a SOL_USD cell
    # against the ETH-only news file reported `process_failed` alone, with
    # the real cause visible in the same stderr_tail.
    if "SignalTickerMismatchError" in text or "holds no records for" in text:
        reasons.append("signal_ticker_mismatch")
    return reasons


def is_valid(
    record: Mapping[str, Any],
    *,
    min_bar_ratio: float = 0.5,
    min_trades: int = 1,
    expected_bars: int | None = None,
    reference_bars: int | None = None,
) -> bool:
    return not assess_cell(
        record,
        min_bar_ratio=min_bar_ratio,
        min_trades=min_trades,
        expected_bars=expected_bars,
        reference_bars=reference_bars,
    )


# ── Results file ────────────────────────────────────────────────────────


def load_records(path: str | Path) -> list[dict[str, Any]]:
    """Read the JSONL results file, skipping unparseable lines (loudly)."""
    p = Path(path)
    if not p.is_file():
        return []
    records: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(
                    f"warning: {p}:{lineno}: unparseable JSONL line skipped "
                    f"({exc})",
                    file=sys.stderr,
                )
    return records


def append_record(path: str | Path, record: Mapping[str, Any]) -> None:
    """Append one record and flush it to disk.

    One line per cell, fsync'd, so an interrupted run loses at most the
    cell in flight and the file is always resumable.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as handle:
        handle.write(_canonical(record) + "\n")
        handle.flush()
        import os as _os

        _os.fsync(handle.fileno())


def done_cell_ids(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map cell_id -> last record for that id (later lines win on rerun)."""
    out: dict[str, dict[str, Any]] = {}
    for record in records:
        cid = record.get("cell_id")
        if cid:
            out[str(cid)] = dict(record)
    return out


# ── Command construction ────────────────────────────────────────────────


def _resolve_cli(explicit: str | None) -> list[str]:
    """Locate the bot CLI: the console script, or this interpreter."""
    if explicit:
        return [explicit]
    found = shutil.which("kraken-trading-bot")
    if found:
        return [found]
    return [sys.executable, "-m", "kraken_trading_bot.cli"]


def split_cell_overrides(
    config_overrides: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Split a cell's config overrides into backtest- and train-side.

    ``backtest --config`` honours EXACTLY six keys (``fee_rate``,
    ``slippage``, ``action_space``, ``initial_balance``,
    ``market_data_store``, ``data_window``). Everything else comes from
    the MODEL's own training config, because a mismatch there is
    silent-corruption class.

    So an axis touching ``reward``/``feature_*``/``ohlcv_interval_minutes``
    cannot be expressed to a backtest at all. Rather than silently
    applying a backtest config that cannot express the axis (which would
    produce two cells that differ only in training, mislabelled as a
    backtest comparison), we return the train-only keys so the caller
    knows the cell MUST be retrained and the train config MUST carry them.

    Returns:
        ``(backtest_overrides, train_only_keys)``.
    """
    backtest_side: dict[str, Any] = {}
    train_only: list[str] = []
    for key, value in config_overrides.items():
        if key in BACKTEST_EXPRESSIBLE_KEYS:
            backtest_side[key] = value
        else:
            train_only.append(key)
    return backtest_side, sorted(train_only)


def materialize_configs(
    cell: Cell, spec: MatrixSpec, dest_dir: Path
) -> tuple[Path, Path]:
    """Write this cell's TRAIN config and BACKTEST config.

    Materialising rather than mutating the shared base is what keeps cells
    independent: a friction override in one cell cannot leak into the next.

    Two files, deliberately. The train config carries the FULL merge
    (including reward/feature keys a backtest cannot express), so each
    axis level is genuinely retrained. The backtest config carries only
    the six expressible keys — passing train-only keys to a backtest is
    at best ignored and at worst silently misaligns the replay.

    Returns:
        ``(train_config_path, backtest_config_path)``.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    base: dict[str, Any] = {}
    if spec.base_config is not None and spec.base_config.is_file():
        with spec.base_config.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if isinstance(loaded, Mapping):
            base = dict(loaded)

    # The CELL's ticker and model name always win over the base config's:
    # `configs/default.yaml` ships `ticker: "ETH/USD"`, and a matrix cell
    # for SOL_USD would otherwise silently train and backtest ETH.
    base["ticker"] = cell.ticker.replace("_", "/")
    base["model_name"] = cell.model_name

    backtest_overrides, train_only = split_cell_overrides(cell.config_overrides)

    train_path = dest_dir / f"{cell.cell_id}.train.yaml"
    backtest_path = dest_dir / f"{cell.cell_id}.backtest.yaml"

    train_merged = deep_merge(base, cell.config_overrides)
    train_merged["model_name"] = cell.model_name
    train_merged["ticker"] = cell.ticker.replace("_", "/")
    with train_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(train_merged, handle, sort_keys=True)

    backtest_merged = deep_merge(base, backtest_overrides)
    backtest_merged["model_name"] = cell.model_name
    backtest_merged["ticker"] = cell.ticker.replace("_", "/")
    # Strip train-only keys the base may carry, so the backtest genuinely
    # defers to the model's own config rather than to ours.
    for key in TRAIN_ONLY_CONFIG_KEYS:
        backtest_merged.pop(key, None)
    with backtest_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(backtest_merged, handle, sort_keys=True)

    if train_only:
        _LOGGER.debug(
            "cell %s: train-only axis keys %s -> retrained per level",
            cell.cell_id,
            ",".join(train_only),
        )
    return train_path, backtest_path


def train_command(
    cell: Cell, spec: MatrixSpec, cli: Sequence[str], config_path: Path
) -> list[str]:
    cmd = [
        *cli,
        "train",
        "--ticker",
        cell.ticker,
        "--model",
        cell.model_name,
        "--config",
        str(config_path),
    ]
    pages = cell.cli_params.get("pages")
    if pages is not None:
        cmd += ["--pages", str(pages)]
    timesteps = cell.cli_params.get("timesteps")
    if timesteps is not None:
        cmd += ["--timesteps", str(timesteps)]
    seed = cell.cli_params.get("seed")
    if seed is not None:
        cmd += ["--seed", str(seed)]
    if spec.models_root is not None:
        cmd += ["--models-root", str(spec.models_root)]
    cmd.append("--json")
    return cmd


def backtest_command(
    cell: Cell, spec: MatrixSpec, cli: Sequence[str], config_path: Path
) -> list[str]:
    """The contract-shaped backtest command.

    Note what is deliberately NOT passed: ``--pages``/``--seed``. Both
    re-fetch a window, which is exactly the comparability bug that
    ``data_window`` exists to fix -- the pinned window in the per-cell
    config is the single source of the bars under test.

    ``--models-root`` IS passed (when the spec sets one) because the
    contract's ``train`` line takes it and ``backtest`` has to look in the
    same place to find the artifact. This is a documented gap in the
    stated contract; see the report to the lead.
    """
    cmd = [
        *cli,
        "backtest",
        "--ticker",
        cell.ticker,
        "--model",
        cell.model_name,
        "--config",
        str(config_path),
    ]
    if spec.models_root is not None:
        cmd += ["--models-root", str(spec.models_root)]
    cmd.append("--json")
    return cmd


def _run_json(
    cmd: Sequence[str], timeout: float | None, cwd: Path | None = None
) -> tuple[int, dict[str, Any] | None, str, str]:
    """Run a command and parse its stdout as one JSON object.

    Returns ``(returncode, parsed, stdout, stderr)``; ``parsed`` is None
    when the process failed or stdout was not a single JSON object.
    """
    try:
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired:
        return 124, None, "", f"timeout after {timeout}s"
    except FileNotFoundError as exc:
        return 127, None, "", str(exc)

    parsed: dict[str, Any] | None = None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed = candidate
            break
    return proc.returncode, parsed, proc.stdout, proc.stderr


# ── plan ────────────────────────────────────────────────────────────────


#: Ticker-field name carried by every signal record shape. Mirrors
#: ``_TICKER_FIELD`` in ``kraken_trading_bot.rl.data``.
_SIGNAL_TICKER_FIELD = "ticker"

#: Kraken spells bitcoin ``XBT``; every producer and config folds it to
#: ``BTC``. Mirrors ``_TICKER_ALIASES`` in the RL data module.
_SIGNAL_TICKER_ALIASES = {"XBT": "BTC"}


def canonical_pair(value: Any) -> str:
    """Fold a pair/ticker string to a separator-free comparison key.

    ``"ETH/USD"``, ``"ETH_USD"``, ``"eth/usd"`` and ``"ETHUSD"`` all fold
    to ``"ETHUSD"``, so a signal file's records and a matrix cell's ticker
    can be compared without either side depending on a spelling.

    Deliberately re-implemented here rather than imported: this harness
    never imports the RL package, so that it keeps driving the documented
    CLI contract when the internals move. The folding rule is duplicated
    on purpose; :func:`_signal_file_tickers` is written to be
    CONSERVATIVE (see there) so a drift between the two copies downgrades
    a BLOCKER to a NOTE rather than inventing one.
    """
    text = "" if value is None else str(value).strip().upper()
    if not text:
        return ""
    parts = [part for part in re.split(r"[^A-Z0-9]+", text) if part]
    if not parts:
        return ""
    parts[0] = _SIGNAL_TICKER_ALIASES.get(parts[0], parts[0])
    return "".join(parts)


def _signal_file_tickers(path: Path) -> tuple[frozenset[str], bool]:
    """The canonical tickers a signal file carries, and whether it is tagged.

    Returns ``(tickers, tagged)``. ``tagged`` is False when the file has no
    usable ``ticker`` field at all, or the field is empty on every record —
    the two cases the RL seam treats as an explicitly one-ticker file and
    MERGES anyway, with a WARNING. Those must never produce a BLOCKER here,
    or this preflight would fail a spec the run itself accepts.

    Only raises on an unreadable/unparseable file, which the caller
    downgrades: the point of a plan-time preflight is to catch the
    guaranteed failures cheaply, not to be the authority that decides.
    """
    tickers: set[str] = set()
    tagged = False
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, Mapping):
                continue
            raw = record.get(_SIGNAL_TICKER_FIELD)
            key = canonical_pair(raw)
            if key:
                tagged = True
                tickers.add(key)
    return frozenset(tickers), tagged


def build_warnings(spec: MatrixSpec, cells: Sequence[Cell]) -> list[dict[str, str]]:
    """Validity warnings for the design. WARN, never fail."""
    out: list[dict[str, str]] = []

    def warn(code: str, severity: str, message: str) -> None:
        out.append({"code": code, "severity": severity, "message": message})

    # 1. Window pinning -- the most important one. Two fetches have been
    #    measured disagreeing 16% on a fitted feature std, so unpinned
    #    cells are not comparable with each other at all.
    windowed = [c for c in cells if c.expected_bars]
    if not windowed:
        warn(
            "no_data_window",
            "BLOCKER",
            "No cell pins data_window.{since,until}. train and backtest each "
            "fetch FRESH data, and two fetches have been measured to "
            "disagree 16% on a fitted feature std -- so these cells are not "
            "comparable with each other. Add a data_window axis with since "
            "+ until before reading any number this run produces.",
        )
    elif len(windowed) < len(cells):
        warn(
            "partial_data_window",
            "BLOCKER",
            f"Only {len(windowed)}/{len(cells)} cells pin data_window; the "
            "rest re-fetch live bars and cannot be compared against the "
            "pinned ones.",
        )

    # 2. Multi-seed discipline.
    seeds = spec.levels("seed") if spec.has_axis("seed") else []
    if not seeds:
        warn(
            "no_seed_axis",
            "BLOCKER",
            "No 'seed' axis. PPO on an identical config has been measured "
            "replaying 576 trades and 374 (AUDIT/VALIDATION 2026-10-01), so "
            "one run per configuration is an anecdote, not a measurement. "
            "Add at least 3 seeds.",
        )
    elif len(seeds) < MIN_REPLICATES_FOR_A_CLAIM:
        warn(
            "few_seeds",
            "WARN",
            f"Only {len(seeds)} seed(s). Below {MIN_REPLICATES_FOR_A_CLAIM} "
            "replicates a median cannot be separated from seed noise -- "
            "prefer 3, and 5+ before calling any difference real.",
        )

    # 3. Friction.
    frictionless = []
    for cell in cells:
        fee = cell.config_overrides.get("fee_rate")
        slip = cell.config_overrides.get("slippage")
        fee = 0.0 if fee is None else float(fee)
        slip = 0.0 if slip is None else float(slip)
        if fee == 0.0 and slip == 0.0:
            frictionless.append(cell)
    if frictionless and len(frictionless) == len(cells):
        warn(
            "frictionless",
            "BLOCKER",
            "Every cell is frictionless (fee_rate == slippage == 0). A "
            "frictionless Sharpe is not a Sharpe: the backtest books every "
            "fill at the mid, so any strategy that trades at all looks "
            "profitable. Add a realistic friction axis.",
        )
    elif frictionless:
        warn(
            "partial_friction",
            "WARN",
            f"{len(frictionless)}/{len(cells)} cells are frictionless. Their "
            "returns are an upper bound no trader could achieve.",
        )

    # 4. Cross-section.
    tickers = spec.levels("ticker") if spec.has_axis("ticker") else []
    if len(tickers) < 2:
        warn(
            "single_ticker",
            "WARN",
            "Exactly one ticker. There is no cross-sectional view, so "
            "'does this generalize across assets?' cannot be answered -- a "
            "result here is one asset's story. Add a second ticker.",
        )

    # 4b. Signal/ticker compatibility -- a cell that CANNOT succeed, named
    # before the run rather than after it. Measured: a SOL_USD cell against
    # the ETH-only news file raised SignalTickerMismatchError on the train
    # leg and reported a bare `process_failed`, and plan had said nothing,
    # because plan never looked. Silence on a spec that is guaranteed to
    # fail is the defect; the classifier only improves the message once the
    # cell has already burned its train+backtest budget.
    #
    # A configured (non-null) signal key whose file carries no record for a
    # cell's ticker is a BLOCKER, because signal_require_ticker defaults
    # true and the seam raises by name. Every escape hatch the seam itself
    # honours is honoured here too, so this cannot BLOCK a cell the run
    # would accept: a null/empty key is OFF; an untagged or empty-tagged
    # file is a one-ticker file the seam merges with a WARNING; and
    # signal_require_ticker: false turns the whole thing into a log line.
    # An unreadable file downgrades to a NOTE — that is a different defect
    # (signal_file_not_found) and not this check's to assert.
    base_cfg: dict[str, Any] = {}
    if spec.base_config is not None and spec.base_config.is_file():
        try:
            loaded = yaml.safe_load(spec.base_config.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                base_cfg = dict(loaded)
        except (OSError, yaml.YAMLError):
            base_cfg = {}
    # A cell-level axis may point the key somewhere else entirely; the
    # effective value is the cell override when present, else the base.
    require_ticker = base_cfg.get("signal_require_ticker", True)
    coverage: dict[str, tuple[Path, frozenset[str], bool]] = {}
    for key in _SIGNAL_CONFIG_KEYS:
        raw = None
        for cell in cells:
            if key in cell.config_overrides:
                raw = cell.config_overrides[key]
                break
        if raw is None:
            raw = base_cfg.get(key)
        if not raw:  # null / empty -> the channel is OFF
            continue
        path = Path(str(raw)).expanduser()
        if not path.is_file():
            continue
        try:
            tickers_in_file, tagged = _signal_file_tickers(path)
        except (OSError, json.JSONDecodeError):
            continue
        coverage[key] = (path, tickers_in_file, tagged)
    if coverage and require_ticker is not False:
        mismatched: dict[str, set[str]] = {}
        for cell in cells:
            wanted = canonical_pair(cell.ticker)
            if not wanted:
                continue
            for key, (path, tickers_in_file, tagged) in coverage.items():
                # An untagged/empty-tagged file is merged unfiltered by the
                # seam, so it constrains nothing. Same for a match.
                if not tagged or wanted in tickers_in_file:
                    continue
                mismatched.setdefault(cell.ticker, set()).add(key)
        for ticker, keys in sorted(mismatched.items()):
            warn(
                "signal_ticker_mismatch",
                "BLOCKER",
                f"Cell ticker {ticker} has no record in the configured "
                f"signal file(s) {sorted(keys)}, which carry "
                f"{sorted(set().union(*[coverage[k][1] for k in keys])) or ['none']}. "
                "With signal_require_ticker true the seam raises "
                "SignalTickerMismatchError on the train leg, so every such "
                "cell fails before it can produce a number. Point those keys "
                "at this ticker's file, or set signal_require_ticker: false "
                "to merge unfiltered.",
            )

    # 5. Only PPO exists.
    if not spec.has_axis("algo", "algorithm"):
        warn(
            "no_algo_axis",
            "NOTE",
            "No algo axis, and there cannot be one yet: rl/agent.py is a "
            "thin SB3 PPO wrapper with no other backend. 'Which algorithm "
            "is better?' is a documented FUTURE axis, not a live one.",
        )

    # 6. Sample size per configuration.
    per_config: dict[str, int] = {}
    for cell in cells:
        key = _canonical(
            {"ticker": cell.ticker, "cfg": cell.config_overrides}
        )
        per_config[key] = per_config.get(key, 0) + 1
    thin = {k: v for k, v in per_config.items() if v < MIN_REPLICATES_FOR_A_CLAIM}
    if thin:
        warn(
            "thin_replication",
            "WARN",
            f"{len(thin)}/{len(per_config)} (ticker, config) groups have "
            f"fewer than {MIN_REPLICATES_FOR_A_CLAIM} cells. Those medians "
            "are anecdotes; they are labelled as such in the report.",
        )

    # 7. Config keys the harness would like but the base config may lack
    #    (the parallel builder may land them later). Reported, not fatal.
    if spec.base_config is not None and not spec.base_config.is_file():
        warn(
            "base_config_missing",
            "WARN",
            f"base_config {spec.base_config} does not exist. Cells will run "
            "against the CLI's own defaults; copy configs/default.yaml and "
            "point at it before trusting the numbers.",
        )
    elif spec.base_config_present:
        absent = [
            key
            for key in ("data_window", "fee_rate", "slippage")
            if not spec.base_config_present.get(key, False)
        ]
        if absent:
            warn(
                "base_config_missing_keys",
                "NOTE",
                "base_config has no "
                + ", ".join(absent)
                + ". Those keys are supplied per-cell by this harness, so "
                "the run proceeds -- but the shipped default config is not "
                "yet able to express them on its own.",
            )

    # 8. Width check would be skipped.
    if not any(c.expected_bars for c in cells):
        warn(
            "width_unchecked",
            "NOTE",
            "No cell has a derivable expected bar count, so the per-cell "
            "width guard is skipped (the report falls back to each ticker's "
            "largest observed n_bars as the denominator). pages is NOT used "
            "as a denominator on purpose: --pages 2 returned 721 bars, not "
            "1440, so pages*720 would flag healthy cells as truncated.",
        )

    # 9. eval_split is INERT unless the window is pinned. A cell that
    #    "sets eval_split" against a null window still trains and
    #    backtests on 100% of the frame, i.e. it is IN-SAMPLE and its
    #    return describes the fit rather than a prediction.
    inert_split = [
        c
        for c in cells
        if (c.config_overrides.get("data_window") or {}).get("eval_split")
        is not None
        and not window_is_pinned(c.config_overrides)
    ]
    if inert_split:
        warn(
            "eval_split_inert",
            "BLOCKER",
            f"{len(inert_split)} cell(s) set data_window.eval_split but did "
            "NOT pin since/until. eval_split is inert in that case: the run "
            "trains and backtests on 100% of the frame, so those cells are "
            "IN-SAMPLE and their returns describe the fit, not a prediction. "
            "Pin both bounds, or drop eval_split so nothing reads as a "
            "holdout.",
        )

    # 10. --pages must be sized to REACH `until`. The window bounds are a
    #     CLIP on the read, not a push-down, so a too-small --pages
    #     silently yields a short window that only n_bars reveals.
    sized = [
        c
        for c in cells
        if span_bars_for(c.config_overrides) and c.cli_params.get("pages")
    ]
    undersized = [
        c
        for c in sized
        if int(c.cli_params["pages"]) * 300 < span_bars_for(c.config_overrides)
    ]
    if undersized:
        warn(
            "pages_may_undershoot",
            "WARN",
            f"{len(undersized)} cell(s) request a window of up to "
            f"{span_bars_for(undersized[0].config_overrides)} bars with only "
            f"{undersized[0].cli_params['pages']} page(s). Window bounds are "
            "a CLIP, not a push-down, so too few pages yields a silently "
            "short window. Cells below min_bar_ratio of the requested span "
            "are marked INVALID on n_bars, but size pages generously.",
        )

    # 11. An axis a backtest config cannot express forces a retrain.
    train_only_axes = sorted(
        {
            key
            for c in cells
            for key in split_cell_overrides(c.config_overrides)[1]
        }
    )
    if train_only_axes:
        warn(
            "train_only_axis",
            "NOTE",
            "Axis key(s) "
            + ", ".join(train_only_axes)
            + " cannot be expressed to a backtest (--config honours exactly "
            "fee_rate, slippage, action_space, initial_balance, "
            "market_data_store, data_window). The harness writes them to a "
            "per-cell TRAIN config and retrains every level, which is the "
            "only correct reading: a backtest would otherwise read them "
            "from the model config and the comparison would be meaningless.",
        )

    # 12. A dict axis named after a config key but WITHOUT the leading dot
    #     FLATTENS, so `data_window:` writes since/until/eval_split at the
    #     TOP level and the window silently never happens. Silent no-ops are
    #     the whole failure mode this tool exists to catch, so name them.
    flattened_keys = (
        BACKTEST_EXPRESSIBLE_KEYS | TRAIN_ONLY_CONFIG_KEYS | {"data_window"}
    )
    mis_dotted = sorted(
        axis
        for axis in spec.axes
        if axis in flattened_keys
        and any(isinstance(v, Mapping) for v in spec.levels(axis))
    )
    if mis_dotted:
        warn(
            "axis_needs_dot",
            "WARN",
            "Axis name(s) "
            + ", ".join(mis_dotted)
            + " take a dict level, but without a leading dot a dict level "
            "FLATTENS -- its keys are written at the top level, so the axis "
            "does not reach the key you named and takes effect nowhere. "
            "Write '."
            + mis_dotted[0]
            + ":' to nest it.",
        )
    return out


def estimate_seconds(spec: MatrixSpec, cells: Sequence[Cell]) -> float:
    per_cell = spec.train_seconds + spec.backtest_seconds
    return per_cell * len(cells)


def cmd_plan(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec)
    cells = expand_cells(spec)
    warnings = build_warnings(spec, cells)

    blockers = [w for w in warnings if w["severity"] == "BLOCKER"]
    seconds = estimate_seconds(spec, cells)

    if args.json:
        print(
            json.dumps(
                {
                    "name": spec.name,
                    "question": spec.question,
                    "spec": str(spec.path),
                    "cells": len(cells),
                    "axes": {a: len(spec.axes[a]) for a in sorted(spec.axes)},
                    "estimated_seconds": seconds,
                    "results": str(spec.results),
                    "warnings": warnings,
                },
                indent=2,
            )
        )
        return 1 if (args.strict and blockers) else 0

    print(f"MODEL MATRIX PLAN — {spec.name}")
    if spec.question:
        print(f"  question:   {spec.question}")
    print(f"  spec:       {spec.path}")
    print(f"  base config:{spec.base_config or '(none — CLI defaults)'}")
    print(f"  results:    {spec.results}")
    if spec.models_root:
        print(f"  models root:{spec.models_root}")
    print()

    print(f"AXES ({len(spec.axes)})")
    for axis in sorted(spec.axes):
        values = spec.axes[axis]
        kind = "CLI flag" if axis in RESERVED_AXES else "config key"
        shown = ", ".join(label(v) for v in values)
        if len(shown) > 66:
            shown = shown[:63] + "..."
        print(f"  {axis:<16} {len(values):>3} level(s)  [{kind}]  {shown}")
    print()

    print(f"CELLS       {len(cells)}")
    print(
        f"ESTIMATED   {format_duration(seconds)} "
        f"(~{spec.train_seconds:.0f}s train + ~{spec.backtest_seconds:.0f}s "
        f"backtest per cell; a real PPO run is slower)"
    )
    pinned = sum(1 for c in cells if c.expected_bars)
    print(f"WINDOWED    {pinned}/{len(cells)} cells pin a data_window")
    print()

    print("VALIDITY WARNINGS")
    if not warnings:
        print("  none — every precondition for a comparison holds.")
    for warn_ in sorted(
        warnings, key=lambda w: {"BLOCKER": 0, "WARN": 1, "NOTE": 2}[w["severity"]]
    ):
        print(f"  [{warn_['severity']}] {warn_['code']}")
        for line in _wrap(warn_["message"], 72):
            print(f"      {line}")
    print()

    if args.explain:
        print("CELLS (first 12)")
        for cell in cells[:12]:
            params = ", ".join(
                f"{k}={label(v)}" for k, v in sorted(cell.params.items())
            )
            print(f"  {cell.cell_id}  {params}")
        if len(cells) > 12:
            print(f"  ... {len(cells) - 12} more")
        print()

    if blockers:
        print(
            f"REFUSING TO IMPLY A COMPARISON: {len(blockers)} blocking "
            "condition(s) above. Run anyway with --force if you accept that "
            "the result is not a comparison."
        )
    return 1 if (args.strict and blockers) else 0


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
        add = len(word) + (1 if current else 0)
        if current and length + add > width:
            lines.append(" ".join(current))
            current = [word]
            length = len(word)
        else:
            current.append(word)
            length += add
    if current:
        lines.append(" ".join(current))
    return lines or [""]


# ── run ─────────────────────────────────────────────────────────────────


def trim_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Shrink a parsed ``--json`` payload for the results file.

    ``equity_curve`` is ~4 KB of floats per cell; across a matrix that is
    pure bloat, since no aggregate here reads it. It is replaced by
    ``equity_curve_summary`` (length + endpoints), which is enough to spot
    a flat or truncated replay. Everything else is kept verbatim — the
    contract fields are what the validity checks run on.
    """
    if payload is None:
        return None
    curve = payload.get("equity_curve")
    if isinstance(curve, list) and curve:
        numeric = [float(v) for v in curve if _finite(v)]
        payload["equity_curve_summary"] = {
            "n_points": len(curve),
            "first": numeric[0] if numeric else None,
            "last": numeric[-1] if numeric else None,
            "min": min(numeric) if numeric else None,
            "max": max(numeric) if numeric else None,
        }
    payload["equity_curve"] = []
    return payload


def cmd_run(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec)
    cells = expand_cells(spec)
    cli = _resolve_cli(args.cli)
    workdir = Path(args.work_dir).expanduser() if args.work_dir else None

    known = done_cell_ids(load_records(spec.results))
    pending = [c for c in cells if args.force or c.cell_id not in known]
    if not pending:
        print(
            f"{len(cells)} cell(s) already recorded in {spec.results}. "
            "Nothing to do (use --force to re-run)."
        )
        return 0

    print(
        f"MODEL MATRIX RUN — {spec.name}: {len(pending)} cell(s) to run "
        f"({len(cells) - len(pending)} already recorded), "
        f"{format_duration(estimate_seconds(spec, pending))} estimated."
    )
    if workdir is None and spec.models_root:
        workdir = spec.models_root.parent
    print(f"  results:    {spec.results}")
    print(f"  models root:{spec.models_root or '(repo default)'}")
    print()

    executed = 0
    failures = 0
    invalid = 0
    for cell in pending:
        if args.limit and executed >= args.limit:
            print(f"  (--limit {args.limit} reached; stopping)")
            break
        cell_work = workdir or Path.cwd()
        config_dir = cell_work / "configs"
        train_cfg, backtest_cfg = materialize_configs(cell, spec, config_dir)
        backtest_overrides, train_only = split_cell_overrides(
            cell.config_overrides
        )

        cmd_tr = train_command(cell, spec, cli, train_cfg)
        cmd_bt = backtest_command(cell, spec, cli, backtest_cfg)
        params_text = ", ".join(
            f"{k}={label(v)}" for k, v in sorted(cell.params.items())
        )
        print(f"[{cell.index + 1}/{len(cells)}] {cell.cell_id}  {params_text}")
        if train_only:
            # Not an error: an axis a backtest cannot express. Say so,
            # because it means this level cost a full retrain.
            print(
                f"    train-only axis keys ({', '.join(train_only)}): "
                "written to the TRAIN config; this level is retrained"
            )

        record: dict[str, Any] = {
            "cell_id": cell.cell_id,
            "index": cell.index,
            "name": spec.name,
            "params": cell.params,
            "cli_params": cell.cli_params,
            "config_overrides": cell.config_overrides,
            "backtest_overrides": backtest_overrides,
            "train_only_keys": train_only,
            "train_config_path": str(train_cfg),
            "backtest_config_path": str(backtest_cfg),
            "config_path": str(backtest_cfg),
            "expected_bars": cell.expected_bars,
            "model_name": cell.model_name,
            "window_pinned": window_is_pinned(cell.config_overrides),
            "eval_split_active": window_has_split(cell.config_overrides),
            "out_of_sample": cell_is_out_of_sample(cell.config_overrides),
            "started_at": _utcnow(),
        }

        if args.dry_run:
            record["commands"] = {"train": cmd_tr, "backtest": cmd_bt}
            record["status"] = "dry_run"
            append_record(spec.results, record)
            print(f"    train:    {' '.join(cmd_tr)}")
            print(f"    backtest: {' '.join(cmd_bt)}")
            executed += 1
            continue

        t0 = time.monotonic()
        rc_tr, train_json, _out_tr, err_tr = _run_json(
            cmd_tr, spec.timeout, config_dir
        )
        t1 = time.monotonic()
        rc_bt, backtest_json, _out_bt, err_bt = _run_json(
            cmd_bt, spec.timeout, config_dir
        )
        elapsed = time.monotonic() - t0

        record["commands"] = {"train": cmd_tr, "backtest": cmd_bt}
        record["train"] = trim_payload(train_json)
        record["backtest"] = trim_payload(backtest_json)
        record["returncodes"] = {"train": rc_tr, "backtest": rc_bt}
        record["duration_seconds"] = round(elapsed, 2)
        record["finished_at"] = _utcnow()

        # Tail is taken PER LEG, not over the concatenation. When train
        # fails the backtest leg still runs and still fails, and its
        # output — the downstream "No trained model" consequence — is
        # last, so a single concatenated slice kept only that and
        # truncated away the train leg's refusal, the one message that
        # names the fix. That made classify_process_failure's
        # `signal_file_not_found` unfireable on the live cell: the
        # classifier was right, the capture was losing the text. Measured
        # on the real pair: refusal at line 8 of a 10-line err_tr, 19
        # concatenated lines, last-4 keeps 4, refusal gone.
        #
        # train gets the larger tail because it is the ROOT-CAUSE leg;
        # backtest only ever echoes the consequence, so two lines is
        # enough for it and keeps the record small. Taking the legs
        # separately also stops a leg that ends without a newline from
        # splicing its last line onto the next leg's first.
        stderr_tail = (
            err_tr.strip().splitlines()[-4:] + err_bt.strip().splitlines()[-2:]
        )
        if stderr_tail:
            record["stderr_tail"] = stderr_tail

        if rc_tr != 0 or rc_bt != 0:
            failures += 1
            record["status"] = "error"
            record["error"] = (
                f"train rc={rc_tr}, backtest rc={rc_bt}; "
                f"{'; '.join(stderr_tail)[:400]}"
            )
        elif backtest_json is None:
            failures += 1
            record["status"] = "error"
            record["error"] = "backtest --json printed no parseable JSON object"

        reasons = assess_cell(
            record,
            min_bar_ratio=spec.min_bar_ratio,
            min_trades=spec.min_trades,
            expected_bars=cell.expected_bars,
        )
        record["invalid_reasons"] = reasons
        if reasons and record.get("status") != "error":
            invalid += 1
            record["status"] = "invalid"
        elif record.get("status") != "error":
            record["status"] = "ok"

        append_record(spec.results, record)
        executed += 1

        if backtest_json:
            print(
                f"    return={fmt_pct(backtest_json.get('total_return'))} "
                f"excess={fmt_pct(backtest_json.get('excess_return'))} "
                f"sharpe={fmt_num(backtest_json.get('sharpe'))} "
                f"trades={backtest_json.get('num_trades')} "
                f"bars={backtest_json.get('n_bars')} "
                f"({elapsed:.0f}s)"
            )
        if reasons:
            print(f"    INVALID: {', '.join(reasons)}")
        if record.get("error"):
            print(f"    ERROR: {record['error']}")

    print()
    print(
        f"ran {executed} cell(s): {invalid} invalid, {failures} errored. "
        f"Results: {spec.results}"
    )
    if invalid:
        print(
            f"  {invalid} cell(s) completed but produced no measurement — "
            "they will be EXCLUDED from every aggregate until you look at "
            "why."
        )
    return 1 if failures else 0


# ── report ──────────────────────────────────────────────────────────────


@dataclass
class _CellView:
    """A record plus everything the report needs, derived once."""

    record: dict[str, Any]
    reasons: list[str]
    params: dict[str, Any]
    backtest: dict[str, Any] | None
    config_key: str

    @property
    def valid(self) -> bool:
        return not self.reasons

    def metric(self, name: str) -> float | None:
        if not self.backtest:
            return None
        value = self.backtest.get(name)
        return float(value) if _finite(value) else None


def _build_views(
    records: Sequence[Mapping[str, Any]],
    *,
    min_bar_ratio: float,
    min_trades: int,
) -> list[_CellView]:
    """Classify every record, with a per-ticker peer reference.

    The peer reference is the point: with no pinned window the only
    trustworthy denominator is what the ticker's other cells actually
    replayed. A cell that replayed 1 bar while its siblings replayed 697
    is degenerate regardless of what it asked for.
    """
    peer_max: dict[str, int] = {}
    for record in records:
        backtest = record.get("backtest")
        if not isinstance(backtest, Mapping):
            continue
        try:
            n_bars = int(backtest.get("n_bars"))
        except (TypeError, ValueError):
            continue
        key = str(backtest.get("ticker_id") or record.get("params", {}).get("ticker"))
        peer_max[key] = max(peer_max.get(key, 0), n_bars)

    views: list[_CellView] = []
    for record in records:
        params = dict(record.get("params") or {})
        backtest = record.get("backtest")
        ticker_key = str(
            (backtest or {}).get("ticker_id") or params.get("ticker") or "?"
        )
        reasons = assess_cell(
            record,
            min_bar_ratio=min_bar_ratio,
            min_trades=min_trades,
            expected_bars=record.get("expected_bars"),
            reference_bars=peer_max.get(ticker_key),
        )
        views.append(
            _CellView(
                record=dict(record),
                reasons=reasons,
                params=params,
                backtest=dict(backtest) if isinstance(backtest, Mapping) else None,
                config_key=_canonical(
                    {
                        "ticker": params.get("ticker"),
                        "cfg": record.get("config_overrides") or {},
                    }
                ),
            )
        )
    return views


def _view_is_oos(view: "_CellView") -> bool:
    """Was this cell actually evaluated out-of-sample?

    Prefers the value `run` recorded at execution time; falls back to
    re-deriving it from the cell's config overrides, so a hand-written or
    older results file is still classified correctly.
    """
    recorded = view.record.get("out_of_sample")
    if isinstance(recorded, bool):
        return recorded
    return cell_is_out_of_sample(view.record.get("config_overrides") or {})


def _group_summaries(
    views: Sequence[_CellView],
    key_fn,
    label_fn,
    metric: str,
    kind: str,
    requested: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    groups: dict[str, list[_CellView]] = {}
    labels: dict[str, str] = {}
    for view in views:
        if not view.valid:
            continue
        key = key_fn(view)
        groups.setdefault(key, []).append(view)
        labels[key] = label_fn(view)

    rows: list[dict[str, Any]] = []
    for key, members in sorted(groups.items()):
        values = [v.metric(metric) for v in members]
        values = [v for v in values if v is not None]
        row: dict[str, Any] = {
            "key": key,
            "label": labels[key],
            "n_valid": len(values),
            "n_requested": (requested or {}).get(key, len(members)),
            "summary": summarize(values),
        }
        rows.append(row)
    return rows


def _print_group_table(
    rows: Sequence[Mapping[str, Any]], metric_label: str, kind: str, indent: str = "  "
) -> None:
    if not rows:
        print(f"{indent}(no valid cells)")
        return
    width = max(len(str(r["label"])) for r in rows)
    width = min(width, 34)
    print(
        f"{indent}{'group':<{width}}  {'n':>5}  {metric_label}"
    )
    print(f"{indent}{'-' * width}  {'-' * 5}  {'-' * len(metric_label)}")
    for row in sorted(
        rows, key=lambda r: (r["summary"]["median"] is None, -(r["summary"]["median"] or 0))
    ):
        text = str(row["label"])
        if len(text) > width:
            text = text[: width - 3] + "..."
        n_cell = str(row["n_valid"])
        if row["n_requested"] > row["n_valid"]:
            n_cell = f"{row['n_valid']}/{row['n_requested']}"
        print(
            f"{indent}{text:<{width}}  {n_cell:>5}  "
            f"{fmt_iqr(row['summary'], kind)}"
        )
    # The yardstick the dispersion verdicts used, printed with the table
    # it was measured from: a reader comparing two rows above needs the
    # within-group noise next to the between-group gap.
    thick = replicated_groups(rows)
    pooled = pooled_within_spread(rows)
    fmt = _fmt_for(kind)
    if pooled is not None:
        print(
            f"{indent}pooled within-group IQR (median over {len(thick)} group(s) "
            f"with n >= {MIN_REPLICATES_FOR_A_CLAIM}): {fmt(pooled)} — the "
            "dispersion gate's yardstick (its ratio threshold is a judgement "
            "call, the ratio itself is a fact)"
        )
    else:
        why = (
            f"needs >= 2 groups with n >= {MIN_REPLICATES_FOR_A_CLAIM} and a "
            f"non-zero IQR (have {len(thick)})"
        )
        print(
            f"{indent}pooled within-group IQR: n/a ({why}) — the dispersion "
            "gate is UNDEFINED for this table, so no pair is adjudicated"
        )


def cmd_report(args: argparse.Namespace) -> int:
    spec = None
    min_bar_ratio = args.min_bar_ratio
    min_trades = args.min_trades
    if args.spec:
        spec = load_spec(args.spec)
        min_bar_ratio = spec.min_bar_ratio
        min_trades = spec.min_trades
    results = Path(args.results) if args.results else (spec.results if spec else None)
    if results is None:
        print("error: pass --results or a --spec", file=sys.stderr)
        return 2

    records = load_records(results)
    if not records:
        print(f"No records in {results}. Run the matrix first.")
        return 1

    views = _build_views(
        records, min_bar_ratio=min_bar_ratio, min_trades=min_trades
    )
    invalid = [v for v in views if not v.valid]
    # Out-of-sample and in-sample cells are NEVER averaged together: an
    # in-sample return describes the fit, an out-of-sample return is a
    # prediction, and the difference between them is the whole point.
    # A cell is out-of-sample only when its window is pinned AND a split
    # is active -- eval_split against a null window is inert.
    oos = [v for v in views if v.valid and _view_is_oos(v)]
    insample = [v for v in views if v.valid and not _view_is_oos(v)]
    valid = oos + insample
    _compute_varying_keys(valid)

    # Seed axis vs config axes.
    config_axes = sorted(
        {
            axis
            for v in views
            for axis in v.params
            if axis not in RESERVED_AXES
        }
    )
    has_seeds = any("seed" in v.params for v in views)

    def metric_row(cohort, metric: str) -> dict[str, Any]:
        return {
            "metric": metric,
            "groups": _group_summaries(
                cohort,
                lambda v: _canonical(v.params.get("ticker")),
                lambda v: label(v.params.get("ticker")),
                metric,
                METRIC_KINDS.get(metric, "num"),
            ),
        }

    METRICS = (
        "excess_return",
        "buy_hold_return",
        "total_return",
        "sharpe",
        "max_drawdown",
        "num_trades",
    )

    per_ticker = [metric_row(oos, m) for m in METRICS]

    # Per-config = seed replicates of the same (ticker, config) setup.
    per_config: list[dict[str, Any]] = []
    for metric in ("excess_return", "sharpe", "num_trades"):
        per_config.append(
            {
                "metric": metric,
                "groups": _group_summaries(
                    oos,
                    lambda v: v.config_key,
                    _config_label,
                    metric,
                    METRIC_KINDS.get(metric, "num"),
                ),
            }
        )

    per_axis: list[dict[str, Any]] = []
    for axis in config_axes + (["seed"] if has_seeds else []):
        per_axis.append(
            {
                "axis": axis,
                "groups": _group_summaries(
                    oos,
                    lambda v, a=axis: _canonical(v.params.get(a)),
                    lambda v, a=axis: label(v.params.get(a)),
                    HEADLINE,
                    "pct",
                ),
            }
        )

    claims = _build_claims(
        views, oos, insample, invalid, config_axes, has_seeds, spec
    )

    if args.json:
        print(
            json.dumps(
                {
                    "results": str(results),
                    "cells_recorded": len(views),
                    "cells_valid": len(valid),
                    "cells_out_of_sample": len(oos),
                    "cells_in_sample": len(insample),
                    "cells_invalid": len(invalid),
                    "invalid": [
                        {
                            "cell_id": v.record.get("cell_id"),
                            "params": v.params,
                            "reasons": v.reasons,
                        }
                        for v in invalid
                    ],
                    "headline_metric": HEADLINE,
                    "per_ticker": per_ticker,
                    "per_config": per_config,
                    "per_axis": per_axis,
                    "claims": claims,
                },
                indent=2,
                default=str,
            )
        )
        return 0

    print(f"MODEL MATRIX REPORT — {spec.name if spec else results}")
    print(f"  results:  {results}")
    print(
        f"  cells:    {len(views)} recorded, {len(valid)} valid "
        f"({len(oos)} out-of-sample, {len(insample)} IN-SAMPLE), "
        f"{len(invalid)} INVALID"
    )
    print(f"  headline: {HEADLINE} (return over buy-and-hold), median [q1, q3]")
    print()

    if insample:
        print(
            f"IN-SAMPLE CELLS ({len(insample)}) — replayed the bars the model "
            "was FITTED on"
        )
        print(
            "  eval_split is inert without a pinned window, so these cells "
            "train and backtest on 100% of the frame. Their returns describe"
        )
        print(
            "  the fit, not a prediction, and they are EXCLUDED from every "
            "aggregate below. They are listed so the run is not silently"
        )
        print("  thinner than it looks — pin data_window.{since,until} to get holdout cells.")
        for view in insample:
            params = ", ".join(
                f"{k}={label(v)}" for k, v in sorted(view.params.items())
            )
            print(f"    {view.record.get('cell_id', '?')}  {params}")
        print()

    if invalid:
        print(f"INVALID CELLS ({len(invalid)}) — excluded from every aggregate")
        for view in invalid:
            params = ", ".join(
                f"{k}={label(v)}" for k, v in sorted(view.params.items())
            )
            bar = ""
            if view.backtest:
                bar = (
                    f"  [n_bars={view.backtest.get('n_bars')} "
                    f"trades={view.backtest.get('num_trades')}]"
                )
            print(f"  {view.record.get('cell_id', '?')}  {params}{bar}")
            for reason in view.reasons:
                print(f"      - {reason}")
        print()

    print("PER-TICKER SUMMARY")
    for row in per_ticker:
        kind = METRIC_KINDS.get(row["metric"], "num")
        if any(g["n_valid"] for g in row["groups"]):
            print(f"  {row['metric']}")
            _print_group_table(row["groups"], row["metric"], kind, indent="    ")
    print()

    print("PER-CONFIG SUMMARY  (one row = one (ticker, config); n = seeds behind it)")
    for row in per_config:
        kind = METRIC_KINDS.get(row["metric"], "num")
        if any(g["n_valid"] for g in row["groups"]):
            print(f"  {row['metric']}")
            _print_group_table(row["groups"], row["metric"], kind, indent="    ")
    print()

    if per_axis:
        print("PER-AXIS MARGINALS  (median excess_return per axis level)")
        for entry in per_axis:
            print(f"  axis: {entry['axis']}")
            _print_group_table(
                entry["groups"], "excess_return", "pct", indent="    "
            )
        print()

    print("WHAT THIS SAMPLE SUPPORTS")
    for claim in claims:
        print(f"  - {claim}")
    print()

    if invalid:
        print(
            f"REMINDER: {len(invalid)} cell(s) were excluded above. An "
            "excluded cell is not a zero — it is an absent measurement, and "
            "a matrix that silently drops them understates its own noise."
        )
    return 0


def _config_label(view: _CellView) -> str:
    """Label a (ticker, config) group by what DISCRIMINATES it.

    Keys that are identical across every valid cell carry no information
    and are dropped, otherwise every row truncates to the same prefix and
    the one axis being compared gets cut off.
    """
    overrides = view.record.get("config_overrides") or {}
    ticker = label(view.params.get("ticker"))
    varying = {k: v for k, v in overrides.items() if _VARYING_KEYS.get(k, True)}
    if not varying:
        return f"{ticker} (base config)"
    inner = ",".join(f"{k}={label(varying[k])}" for k in sorted(varying, key=str))
    return f"{ticker} [{inner}]"


#: Config keys whose value is identical across every valid cell. Filled in
#: by the report; used only to shorten group labels, never to drop data.
_VARYING_KEYS: dict[str, bool] = {}


def _compute_varying_keys(views: Sequence["_CellView"]) -> None:
    """Record which config keys actually vary across the valid cells."""
    _VARYING_KEYS.clear()
    seen: dict[str, set[str]] = {}
    for view in views:
        if not view.valid:
            continue
        for key, value in (view.record.get("config_overrides") or {}).items():
            seen.setdefault(key, set()).add(_canonical(value))
    for key, values in seen.items():
        _VARYING_KEYS[key] = len(values) > 1


def _dispersion_claims(oos: Sequence[_CellView]) -> list[str]:
    """The CAND-5 block: adjacent arm-pairs scored against pooled noise.

    One arm is a row of the per-config headline table; a *pair* is two
    adjacent rows in that table's printed (median-descending) order,
    which is the comparison a reader makes when they look at the table.
    The gate reports a RATIO per pair and never fails the run.

    ``NOT SEPARATED`` is printed as a finding in its own right, worded
    as a measurement: the gap is smaller than the spread inside an arm,
    so the two medians are not ordered by the data.  It is the outcome a
    store-vs-live comparison is most likely to produce at n=3 seeds, and
    burying it under a failure would make the harness lie about its own
    result.
    """
    rows = _group_summaries(
        oos,
        lambda v: v.config_key,
        _config_label,
        HEADLINE,
        "pct",
    )
    ordered = sorted(
        (r for r in rows if _finite((r.get("summary") or {}).get("median"))),
        key=lambda r: float(r["summary"]["median"]),
        reverse=True,
    )
    thick = replicated_groups(rows)
    pooled = pooled_within_spread(rows)
    replicates = min(
        (int(r["summary"]["n"]) for r in thick), default=None
    )

    lines: list[str] = []
    if pooled is None:
        lines.append(
            "DISPERSION GATE: UNDEFINED — no positive pooled within-group "
            f"IQR over the groups with n >= {MIN_REPLICATES_FOR_A_CLAIM} "
            f"({len(thick)} such group(s)). Every pair below is left "
            "UNADJUDICATED rather than scored against a zero denominator, "
            "which would report every gap as infinite and call the "
            "thinnest group the winner."
        )
        return lines

    fmt = _fmt_for("pct")
    lines.append(
        f"DISPERSION GATE (CAND-5): pooled within-group IQR {fmt(pooled)} "
        f"over {len(thick)} group(s) with n >= {MIN_REPLICATES_FOR_A_CLAIM}; "
        "each pair below is |median gap| / that spread, so a ratio >= "
        f"{DISPERSION_RATIO_THRESHOLD:g} clears seed noise. The ratio is a "
        "FACT; the threshold is a JUDGEMENT CALL — read the ratio, not the "
        "verdict. NOT SEPARATED is a finding, not a failure: it means the "
        "gap is inside the noise, so those medians are not ordered by the "
        "data."
    )
    resolved = 0
    not_separated = 0
    pairs = list(zip(ordered, ordered[1:]))
    for lower, upper in pairs:
        result = dispersion_verdict(
            abs(float(lower["summary"]["median"]) - float(upper["summary"]["median"])),
            pooled,
            replicates=replicates,
        )
        if result["verdict"] == DISPERSION_RESOLVED:
            resolved += 1
        elif result["verdict"] == DISPERSION_NOT_SEPARATED:
            not_separated += 1
        lines.append(
            "  " + describe_dispersion(lower, upper, pooled, replicates=replicates)
        )
    lines.append(
        f"DISPERSION VERDICT: {resolved} RESOLVED, {not_separated} NOT "
        f"SEPARATED across {len(pairs)} adjacent pair(s). At n="
        f"{MIN_REPLICATES_FOR_A_CLAIM} a group IQR is the middle of two "
        "order statistics, so this gate is NECESSARY, NOT SUFFICIENT: it "
        "stops a noise-ranked difference being presented as a finding, it "
        "does not manufacture power. More power is more seeds."
    )
    return lines


def _build_claims(
    views: Sequence[_CellView],
    oos: Sequence[_CellView],
    insample: Sequence[_CellView],
    invalid: Sequence[_CellView],
    config_axes: Sequence[str],
    has_seeds: bool,
    spec: MatrixSpec | None,
) -> list[str]:
    """The explicit 'what the sample size supports' statement."""
    claims: list[str] = []
    valid = list(oos) + list(insample)
    if not valid:
        claims.append(
            "NOTHING. No valid cells: every recorded cell is degenerate or "
            "errored, so this results file supports no comparison at all."
        )
        return claims

    if insample:
        claims.append(
            f"{len(insample)} of {len(valid)} valid cells are IN-SAMPLE "
            "(window not pinned, so eval_split never activated) and are "
            "excluded from every aggregate above. A matrix with zero "
            "out-of-sample cells supports NO generalization claim at all — "
            "the numbers describe the fit."
        )
    if not oos:
        claims.append(
            "NO OUT-OF-SAMPLE CELLS. Every valid cell replayed the bars its "
            "model was fitted on, so nothing here can answer 'does this "
            "model work on data it has not seen?'. The aggregates below are "
            "empty BY DESIGN -- pinning data_window.{since,until} plus "
            "eval_split is what fills them. Re-run before drawing any "
            "conclusion."
        )
        if invalid:
            claims.append(
                f"{len(invalid)} cell(s) were also excluded as degenerate, "
                f"so {len(views)} recorded cells yielded 0 usable "
                "out-of-sample measurements."
            )
        return claims

    singles = [
        v
        for v in oos
        if 1
        == sum(1 for o in oos if o.config_key == v.config_key)
    ]
    if singles:
        claims.append(
            f"{len(singles)} cell(s) stand alone in their (ticker, config) "
            "group (n=1). Those are anecdotes, not measurements — PPO on one "
            "config has been measured replaying 576 trades and 374."
        )

    thin = [
        key
        for key in {v.config_key for v in oos}
        if sum(1 for v in oos if v.config_key == key) < MIN_REPLICATES_FOR_A_CLAIM
    ]
    if thin:
        claims.append(
            f"{len(thin)} group(s) have fewer than {MIN_REPLICATES_FOR_A_CLAIM} "
            "valid replicates. Differences inside them are within seed noise "
            "— PPO on one config has been measured replaying 576 trades and "
            "374 — so treat those medians as ordering hints, not evidence."
        )
    # CAND-5: the count gate above is NECESSARY, not sufficient.  "Every
    # group has >= 3 seeds" does not make a difference between two group
    # medians a finding — it only makes the IQRs printable.  So the old
    # "at least seed-stable, treat as a distribution" else-branch is
    # REPLACED by the dispersion gate: each adjacent arm-pair is scored by
    # |median_A - median_B| against the pooled within-group IQR.
    claims.extend(
        _dispersion_claims(oos)
    )

    friction_free = [
        v
        for v in oos
        if float((v.backtest or {}).get("fee_rate") or 0.0) == 0.0
        and float((v.backtest or {}).get("slippage") or 0.0) == 0.0
    ]
    if oos and len(friction_free) == len(oos):
        claims.append(
            "Every valid cell is FRICTIONLESS. A frictionless Sharpe is not a "
            "Sharpe — these returns are an upper bound no trader could "
            "achieve, so this report supports no tradeable claim at all."
        )
    elif friction_free:
        claims.append(
            f"{len(friction_free)}/{len(oos)} out-of-sample cells are frictionless. "
            "Read the friction-bearing rows; the others are ceilings."
        )

    unpinned = [
        v for v in oos
        if not window_is_pinned(v.record.get("config_overrides") or {})
    ]
    if oos and len(unpinned) == len(oos):
        claims.append(
            "No cell pins data_window, so bars under test were re-fetched "
            "per cell. Two fetches have been measured to disagree 16% on a "
            "fitted feature std — this file does NOT support cross-cell "
            "comparison."
        )
    elif unpinned:
        claims.append(
            f"{len(unpinned)}/{len(oos)} out-of-sample cells are window-pinned. "
            "The unpinned ones cannot be compared against them."
        )
    else:
        claims.append(
            "All valid cells share a pinned data_window, so they replayed "
            "the same bars — the cross-cell comparison is at least "
            "well-posed."
        )

    tickers = {v.params.get("ticker") for v in oos}
    if len(tickers) < 2:
        claims.append(
            "One ticker only: this says nothing about whether the signal "
            "generalises across assets."
        )
    else:
        claims.append(
            f"{len(tickers)} tickers — the cross-sectional read is real, but "
            "assets within a ticker are one market, not independent draws."
        )

    if invalid:
        claims.append(
            f"{len(invalid)} cell(s) excluded as degenerate. The remaining "
            f"{len(oos)} are not a random sample of {len(views)} — a cell "
            "that replays 1 bar and 0 trades fails for reasons likely "
            "correlated with the cells that failed, so the survivors may be "
            "flattering."
        )
    if not config_axes:
        claims.append(
            "No config axis varied, so this measures repetition, not "
            "sensitivity to anything."
        )
    return claims


# ── entry point ─────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="model_matrix.py",
        description=__doc__.splitlines()[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser(
        "plan", help="Expand the design, count cells, warn on validity gaps."
    )
    plan.add_argument("spec", help="Path to the matrix YAML spec.")
    plan.add_argument(
        "--json", action="store_true", help="Machine-readable plan output."
    )
    plan.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when a blocking validity condition is present.",
    )
    plan.add_argument(
        "--explain", action="store_true", help="List the first cells."
    )
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser(
        "run", help="Execute cells, appending one JSONL record per cell."
    )
    run.add_argument("spec", help="Path to the matrix YAML spec.")
    run.add_argument(
        "--force",
        action="store_true",
        help="Re-run cells already present in the results file.",
    )
    run.add_argument(
        "--limit", type=int, default=0, help="Stop after N cells (0 = all)."
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Record the commands without executing them.",
    )
    run.add_argument(
        "--cli",
        default=None,
        help="Path to the kraken-trading-bot binary (default: PATH, then -m).",
    )
    run.add_argument(
        "--work-dir",
        default=None,
        help="Where per-cell configs go (default: alongside models_root).",
    )
    run.set_defaults(func=cmd_run)

    report = sub.add_parser(
        "report", help="Aggregate the results file into medians and IQRs."
    )
    report.add_argument(
        "spec", nargs="?", default=None, help="Path to the matrix YAML spec."
    )
    report.add_argument("--results", default=None, help="Path to the JSONL file.")
    report.add_argument(
        "--min-bar-ratio", type=float, default=0.5, dest="min_bar_ratio"
    )
    report.add_argument(
        "--min-trades", type=int, default=1, dest="min_trades"
    )
    report.add_argument(
        "--json", action="store_true", help="Machine-readable report output."
    )
    report.set_defaults(func=cmd_report)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())