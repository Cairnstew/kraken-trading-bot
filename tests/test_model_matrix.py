"""Tests for the benchmark-matrix harness (``tools/model_matrix.py``).

Entirely offline: no network, no Kraken, no RL import. The ``--json``
backtest/train output the harness codes against is SYNTHESISED here from
the documented contract, so these tests stay green whether or not the
parallel RL builder's changes have landed.

Covered: spec loading, cartesian expansion, deterministic cell ids,
per-cell config materialisation (including deep merge of data_window and
leak prevention between cells), incremental/resumable JSONL, degenerate-cell
detection (the 1-bar/0-trades case), and median/IQR aggregation.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.model_matrix import (  # noqa: E402
    BACKTEST_EXPRESSIBLE_KEYS,
    RESERVED_AXES,
    REQUIRED_BACKTEST_FIELDS,
    Cell,
    assess_cell,
    build_warnings,
    cell_is_out_of_sample,
    classify_process_failure,
    cell_id,
    cmd_report,
    cmd_run,
    deep_merge,
    expand_cells,
    expected_bars_for,
    is_valid,
    load_records,
    load_spec,
    materialize_configs,
    median,
    quartile,
    split_cell_overrides,
    span_bars_for,
    window_has_split,
    window_is_pinned,
    summarize,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "model_matrix.py"


# ── fixtures ────────────────────────────────────────────────────────────


def _backtest_json(**overrides):
    """A healthy cell's backtest --json output (the documented contract)."""
    payload = {
        "ticker_id": "ETH_USD",
        "model_name": "mtx_abc",
        "action_space": "discrete",
        "total_return": 0.12,
        "sharpe": 1.4,
        "max_drawdown": 0.08,
        "num_trades": 42,
        "win_rate": 0.55,
        "equity_curve": [10000.0, 10100.0, 11200.0],
        "n_steps": 480,
        "final_equity": 11200.0,
        "seed": 42,
        "buy_hold_return": 0.05,
        "excess_return": 0.07,
        "buy_hold_max_drawdown": 0.11,
        "n_bars": 504,
        "fee_rate": 0.0,
        "slippage": 0.0,
    }
    payload.update(overrides)
    return payload


def _record(cell_id="abc123", **backtest_overrides):
    return {
        "cell_id": cell_id,
        "index": 0,
        "status": "ok",
        "params": {"ticker": "ETH_USD", "seed": 42},
        "cli_params": {"ticker": "ETH_USD", "seed": 42},
        "config_overrides": {},
        "expected_bars": 504,
        "backtest": _backtest_json(**backtest_overrides),
    }


def _write_spec(path: Path, **overrides) -> Path:
    spec = {
        "name": "t",
        "run": {
            "base_config": str(REPO_ROOT / "configs" / "default.yaml"),
            "results": str(path.parent / "cells.jsonl"),
            "models_root": "/tmp/ktb-test/models",
            "min_bar_ratio": 0.5,
            "min_trades": 1,
        },
        "axes": {
            "ticker": ["ETH_USD", "SOL_USD"],
            "seed": [42, 43, 44],
            "action_space": ["discrete"],
        },
    }
    spec.update(overrides)
    path.write_text(yaml.safe_dump(spec))
    return path


# ── spec loading ────────────────────────────────────────────────────────


def test_load_spec_reads_axes_and_run_block(tmp_path):
    spec_path = _write_spec(tmp_path / "m.yaml")
    spec = load_spec(spec_path)
    assert spec.name == "t"
    assert spec.axes["ticker"] == ["ETH_USD", "SOL_USD"]
    assert spec.min_bar_ratio == 0.5
    assert spec.models_root is not None
    assert spec.base_config is not None and spec.base_config.is_file()


def test_load_spec_rejects_empty_axes(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"name": "x", "axes": {}}))
    with pytest.raises(ValueError, match="non-empty mapping"):
        load_spec(bad)


def test_load_spec_scalar_axis_becomes_single_level(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {"results": str(tmp_path / "c.jsonl")},
                "axes": {"ticker": "ETH_USD", "seed": [1, 2]},
            }
        )
    )
    spec = load_spec(path)
    assert spec.levels("ticker") == ["ETH_USD"]


def test_load_spec_tolerates_missing_base_config(tmp_path):
    """The parallel builder may land configs later; plan must not crash."""
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {
                    "base_config": "configs/not-here-yet.yaml",
                    "results": str(tmp_path / "c.jsonl"),
                },
                "axes": {"ticker": ["ETH_USD"], "seed": [1, 2]},
            }
        )
    )
    spec = load_spec(path)
    assert spec.base_config is not None
    assert not spec.base_config.is_file()
    assert spec.base_config_present == {}


# ── expansion ───────────────────────────────────────────────────────────


def test_expand_cells_is_the_cartesian_product(tmp_path):
    spec = load_spec(_write_spec(tmp_path / "m.yaml"))
    cells = expand_cells(spec)
    assert len(cells) == 2 * 3  # tickers x seeds
    assert len({c.cell_id for c in cells}) == len(cells)


def test_expand_cells_separates_cli_flags_from_config_keys(tmp_path):
    spec = load_spec(_write_spec(tmp_path / "m.yaml"))
    cell = expand_cells(spec)[0]
    assert cell.cli_params["ticker"] in {"ETH_USD", "SOL_USD"}
    assert "seed" in cell.cli_params
    # action_space is NOT a CLI flag; it belongs in the config.
    assert "action_space" not in RESERVED_AXES
    assert cell.config_overrides["action_space"] == "discrete"
    assert "action_space" not in cell.cli_params


def test_expand_cells_folds_multiple_dict_axes_into_overrides(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {"results": str(tmp_path / "c.jsonl")},
                "axes": {
                    "ticker": ["ETH_USD"],
                    "seed": [1],
                    "friction": [
                        {"fee_rate": 0.0, "slippage": 0.0},
                        {"fee_rate": 0.0026, "slippage": 0.0005},
                    ],
                    ".data_window": [
                        {"since": "2026-09-01T00:00:00Z",
                         "until": "2026-09-20T00:00:00Z",
                         "eval_split": 0.7}
                    ],
                },
            }
        )
    )
    cells = expand_cells(load_spec(path))
    assert len(cells) == 2
    fees = sorted(c.config_overrides["fee_rate"] for c in cells)
    assert fees == [0.0, 0.0026]
    for cell in cells:
        # The window survives BOTH friction levels (dict-in-dict merge).
        assert cell.config_overrides["data_window"]["eval_split"] == 0.7


def test_expand_cells_orders_axes_deterministically(tmp_path):
    """Same spec -> same order and ids, regardless of YAML key order."""
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    _write_spec(a)
    b.write_text(
        yaml.safe_dump(
            {
                "name": "t",
                "run": {
                    "base_config": str(REPO_ROOT / "configs" / "default.yaml"),
                    "results": str(tmp_path / "cells.jsonl"),
                },
                "axes": {
                    "seed": [42, 43, 44],
                    "action_space": ["discrete"],
                    "ticker": ["ETH_USD", "SOL_USD"],
                },
            }
        )
    )
    ids_a = [c.cell_id for c in expand_cells(load_spec(a))]
    ids_b = [c.cell_id for c in expand_cells(load_spec(b))]
    assert ids_a == ids_b


# ── cell ids ────────────────────────────────────────────────────────────


def test_cell_id_is_stable_across_calls_and_processes():
    params = {"ticker": "ETH_USD", "seed": 42, "action_space": "discrete"}
    assert cell_id(params) == cell_id(dict(reversed(list(params.items()))))
    # Hardcoded so a change to the hash scheme is caught: ids key the
    # resume logic, and silently changing them would re-run every cell.
    assert cell_id(params) == "615a186d892c"


def test_cell_id_differs_on_any_parameter():
    base = {"ticker": "ETH_USD", "seed": 42}
    assert cell_id(base) != cell_id({"ticker": "ETH_USD", "seed": 43})
    assert cell_id(base) != cell_id({"ticker": "SOL_USD", "seed": 42})


def test_cell_id_is_not_the_index(tmp_path):
    """Two specs differing only in one axis must not collide."""
    spec = load_spec(_write_spec(tmp_path / "m.yaml"))
    cells = expand_cells(spec)
    assert len({c.index for c in cells}) == len(cells)
    assert cells[0].index == 0


# ── expected bars ───────────────────────────────────────────────────────


def _round_clamped(n_bars: int, split: float) -> int:
    """``rl.data_window.split_index`` arithmetic, restated locally.

    Duplicated rather than imported so this module keeps its promise of
    never importing the RL package. The equivalence is asserted against
    the real function in ``tests/test_matrix_rl_contract.py``.
    """
    return min(max(int(round(n_bars * split)), 1), n_bars - 1)


def test_expected_bars_is_the_eval_slice_not_the_whole_span():
    """With a split active, the denominator is the EVAL slice.

    ``n_bars`` counts bars a backtest REPLAYED, and the RL side gives
    training the leading ``eval_split`` of a pinned window. So a healthy
    out-of-sample cell returns ~(1 - eval_split) x the span. Denominating
    by the whole span marked every out-of-sample cell INVALID: measured
    against the real CLI on 2026-10-01, a 672-bar window logged "202
    replayable bars" and the guard demanded 336, so a correct 178-bar
    replay was reported as `n_bars:178<0.50x672`.
    """
    overrides = {"data_window": {
        "since": "2026-09-01T00:00:00Z",
        "until": "2026-09-20T00:00:00Z",
        "eval_split": 0.7,
    }}
    assert span_bars_for(overrides, interval_minutes=60) == 456  # 19d * 24h
    # round(456 * 0.7) = 319 training bars, so 137 remain to be replayed.
    assert expected_bars_for(overrides, interval_minutes=60) == 137


def test_expected_bars_matches_the_rl_side_split_boundary():
    """The harness and the RL side must not disagree by a bar.

    The harness rounds and clamps the split boundary the same way the RL
    side does (``rl.data_window.split_index``); a harness-side ``floor()``
    would silently shift the guard's denominator by one bar. The
    arithmetic is pinned here to the values the RL side logs; the
    cross-check against the real module lives in
    ``tests/test_matrix_rl_contract.py``.
    """
    # round(456 * 0.5)=228 -> 228 eval bars; 0.7 -> 319/137; 0.8 -> 365/91.
    for split, train, eval_ in ((0.5, 228, 228), (0.7, 319, 137), (0.8, 365, 91)):
        overrides = {"data_window": {
            "since": "2026-09-01T00:00:00Z",
            "until": "2026-09-20T00:00:00Z",
            "eval_split": split,
        }}
        assert expected_bars_for(overrides, interval_minutes=60) == eval_
        assert _round_clamped(456, split) == train


def test_expected_bars_is_full_span_when_no_split_applies():
    """No split -> the backtest replays the whole window, as before."""
    pinned_no_split = {
        "data_window": {"since": "2026-09-01T00:00:00Z", "until": "2026-09-20T00:00:00Z"}
    }
    assert expected_bars_for(pinned_no_split, interval_minutes=60) == 456


def test_expected_bars_treats_eval_split_one_as_no_split():
    """``eval_split: 1.0`` is the RL side's "no split" switch.

    ``DataWindow.has_split`` requires ``< 1.0``, so the backtest replays
    the whole window. Sizing the denominator at 1 bar here would let a
    near-empty replay pass the width guard.
    """
    pinned_full = {"data_window": {
        "since": "2026-09-01T00:00:00Z",
        "until": "2026-09-20T00:00:00Z",
        "eval_split": 1.0,
    }}
    assert expected_bars_for(pinned_full, interval_minutes=60) == 456


def test_a_correct_out_of_sample_replay_passes_the_width_guard():
    """The regression this whole fix exists for.

    A real run replayed 178 of the 202-bar eval slice of a 672-bar window.
    That is a measurement, not a degenerate cell, and must not be INVALID.
    """
    overrides = {"data_window": {
        "since": "2026-09-02T00:00:00Z",
        "until": "2026-09-30T00:00:00Z",
        "eval_split": 0.7,
    }}
    denominator = expected_bars_for(overrides, interval_minutes=60)
    assert denominator == 202
    assert is_valid(_record(n_bars=178, num_trades=154), expected_bars=denominator)
    # ...and the guard still catches a genuinely degenerate replay.
    assert not is_valid(_record(n_bars=1, num_trades=5), expected_bars=denominator)


def test_expected_bars_is_none_without_a_pinned_window():
    """pages must NOT be used as a denominator.

    --pages 2 has been measured returning 721 bars, not 1440, so
    pages*720 would flag healthy cells as truncated.
    """
    assert expected_bars_for({}, pages=2) is None
    assert expected_bars_for(
        {"data_window": {"since": "2026-09-01", "until": "2026-09-02"}},
        interval_minutes=1440,
    ) == 1


def test_expected_bars_ignores_half_open_window():
    assert expected_bars_for(
        {"data_window": {"since": "2026-09-01T00:00:00Z", "until": None}},
        pages=6,
    ) is None


# ── config materialisation ──────────────────────────────────────────────


def test_materialize_config_merges_overrides_onto_base(tmp_path):
    spec = load_spec(_write_spec(tmp_path / "m.yaml"))
    cell = expand_cells(spec)[0]
    out = materialize_configs(cell, spec, tmp_path / "configs")[0]
    data = yaml.safe_load(out.read_text())
    # from configs/default.yaml
    assert "feature_windows" in data
    # from the cell's axis
    assert data["action_space"] == "discrete"
    assert data["model_name"] == cell.model_name
    assert out.name == f"{cell.cell_id}.train.yaml"


def test_materialize_config_does_not_leak_between_cells(tmp_path):
    """A friction override in one cell must not reach the next."""
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {
                    "base_config": str(REPO_ROOT / "configs" / "default.yaml"),
                    "results": str(tmp_path / "c.jsonl"),
                },
                "axes": {
                    "ticker": ["ETH_USD"],
                    "seed": [1],
                    "friction": [
                        {"fee_rate": 0.0026, "slippage": 0.0005},
                        {"fee_rate": 0.0, "slippage": 0.0},
                    ],
                },
            }
        )
    )
    spec = load_spec(path)
    cells = expand_cells(spec)
    dest = tmp_path / "configs"
    a = yaml.safe_load(materialize_configs(cells[0], spec, dest)[0].read_text())
    b = yaml.safe_load(materialize_configs(cells[1], spec, dest)[0].read_text())
    assert a["fee_rate"] == 0.0026
    assert b["fee_rate"] == 0.0
    # Both keep the untouched base value they did not override.
    assert a["initial_balance"] == b["initial_balance"]


def test_materialize_config_cell_ticker_beats_the_base_config(tmp_path):
    """`configs/default.yaml` ships `ticker: "ETH/USD"`.

    A matrix cell for SOL_USD must still train and backtest SOL_USD --
    otherwise every cell in the matrix silently tests the same asset and
    the cross-section axis is a lie.
    """
    spec = load_spec(_write_spec(tmp_path / "m.yaml"))
    sol = next(c for c in expand_cells(spec) if c.ticker == "SOL_USD")
    data = yaml.safe_load(
        materialize_configs(sol, spec, tmp_path / "configs")[0].read_text()
    )
    assert data["ticker"] == "SOL/USD"


def test_materialize_config_works_without_a_base_file(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {
                    "base_config": "configs/not-here-yet.yaml",
                    "results": str(tmp_path / "c.jsonl"),
                },
                "axes": {"ticker": ["ETH_USD"], "seed": [1]},
            }
        )
    )
    spec = load_spec(path)
    cell = expand_cells(spec)[0]
    data = yaml.safe_load(materialize_configs(cell, spec, tmp_path / "cfg")[0].read_text())
    assert data["ticker"] == "ETH/USD"
    assert data["model_name"] == cell.model_name


def test_deep_merge_is_recursive_and_non_destructive():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    out = deep_merge(base, {"a": {"y": 9}})
    assert out == {"a": {"x": 1, "y": 9}, "b": 3}
    assert base == {"a": {"x": 1, "y": 2}, "b": 3}


# ── degenerate-cell detection ───────────────────────────────────────────


def test_healthy_cell_is_valid():
    assert is_valid(_record(), expected_bars=504) is True
    assert assess_cell(_record(), expected_bars=504) == []


def test_the_one_bar_zero_trade_cell_is_invalid():
    """The measured silent failure: 1 bar replayed of 721, 0 trades.

    It exits 0, the width guard passes, and tabulating it as "0% return"
    would look exactly like a result.
    """
    record = _record(n_bars=1, num_trades=0, total_return=0.0, sharpe=0.0)
    reasons = assess_cell(record, expected_bars=721)
    assert "zero_trades:0" in reasons
    assert any(r.startswith("n_bars:1<") for r in reasons)
    assert is_valid(record, expected_bars=721) is False


def test_narrow_window_alone_is_enough_to_invalidate():
    """0 trades but a real width still fails the trade rule."""
    reasons = assess_cell(_record(n_bars=3, num_trades=0), expected_bars=721)
    assert "zero_trades:0" in reasons
    assert any(r.startswith("n_bars:3<") for r in reasons)


def test_zero_trades_alone_is_enough_to_invalidate():
    record = _record(num_trades=0)
    assert "zero_trades:0" in assess_cell(record, expected_bars=504)


def test_min_trades_is_configurable():
    record = _record(num_trades=3)
    assert assess_cell(record, min_trades=5) == ["zero_trades:3"]
    assert assess_cell(record, min_trades=3) == []


@pytest.mark.parametrize("metric", ["sharpe", "total_return", "max_drawdown"])
def test_nan_metric_is_invalid(metric):
    record = _record(**{metric: float("nan")})
    reasons = assess_cell(record, expected_bars=504)
    assert any(r.startswith("nan_metric:") for r in reasons)
    assert metric in " ".join(reasons)


def test_infinite_metric_is_invalid():
    record = _record(final_equity=float("inf"))
    assert any(
        r.startswith("nan_metric:") for r in assess_cell(record, expected_bars=504)
    )


def test_missing_contract_field_is_invalid_not_assumed_zero():
    """An absent field must never look like a healthy 0.0."""
    payload = _backtest_json()
    del payload["n_bars"]
    record = _record()
    record["backtest"] = payload
    reasons = assess_cell(record, expected_bars=504)
    assert any(r.startswith("missing_field:") for r in reasons)
    assert "n_bars" in " ".join(reasons)


def test_unparseable_or_absent_backtest_is_invalid():
    assert "no_backtest_json" in assess_cell({"cell_id": "x", "status": "ok"})
    errored = {"cell_id": "x", "status": "error", "backtest": None}
    assert "process_failed" in assess_cell(errored)


def test_nonzero_exit_is_invalid_even_with_parsable_json():
    record = _record()
    record["status"] = "error"
    assert "process_failed" in assess_cell(record, expected_bars=504)


def test_peer_reference_catches_degenerate_cell_without_a_pinned_window():
    """With no window pinned, sibling cells on the same ticker are the
    only trustworthy denominator: 1 bar next to 697 is degenerate."""
    record = _record(n_bars=1, num_trades=5, expected_bars=None)
    assert assess_cell(record, reference_bars=697)
    assert assess_cell(record, reference_bars=697) == [
        "n_bars:1<0.50x697"
    ]
    # With the peer set small, a healthy cell passes.
    assert assess_cell(_record(n_bars=504, expected_bars=None), reference_bars=697) == []


def test_invalid_reasons_are_sorted_and_deduplicated():
    record = _record(n_bars=1, num_trades=0, sharpe=float("nan"))
    reasons = assess_cell(record, expected_bars=721)
    assert reasons == sorted(set(reasons))


# ── statistics ──────────────────────────────────────────────────────────


def test_median_and_quartiles():
    assert median([]) is None
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 3, 2]) == 2.5
    assert quartile([1, 2, 3, 4], 0.25) == 1.75
    assert quartile([1, 2, 3, 4], 0.75) == 3.25
    assert quartile([5], 0.25) == 5


def test_summarize_reports_median_iqr_and_spread():
    # The 576-vs-374 shape: a wide spread must be visible, not averaged away.
    stat = summarize([0.07, -0.02, 0.15])
    assert stat["n"] == 3
    assert stat["median"] == 0.07
    assert stat["q1"] < stat["median"] < stat["q3"]
    assert stat["min"] == -0.02
    assert stat["max"] == 0.15


def test_summarize_drops_non_finite_values():
    stat = summarize([0.1, float("nan"), 0.3, float("inf")])
    assert stat["n"] == 2
    assert stat["median"] == 0.2


# ── warnings ────────────────────────────────────────────────────────────


def _codes(spec_path: Path) -> dict[str, str]:
    spec = load_spec(spec_path)
    cells = expand_cells(spec)
    return {w["code"]: w["severity"] for w in build_warnings(spec, cells)}


def test_missing_data_window_is_a_blocker(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {"results": str(tmp_path / "c.jsonl")},
                "axes": {
                    "ticker": ["ETH_USD", "SOL_USD"],
                    "seed": [1, 2, 3],
                    "friction": [{"fee_rate": 0.0026, "slippage": 0.0005}],
                },
            }
        )
    )
    assert _codes(path)["no_data_window"] == "BLOCKER"


def test_frictionless_matrix_is_a_blocker(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {"results": str(tmp_path / "c.jsonl")},
                "axes": {
                    "ticker": ["ETH_USD", "SOL_USD"],
                    "seed": [1, 2, 3],
                    "friction": [{"fee_rate": 0.0, "slippage": 0.0}],
                    ".data_window": [{
                        "since": "2026-09-01T00:00:00Z",
                        "until": "2026-09-20T00:00:00Z",
                    }],
                },
            }
        )
    )
    assert _codes(path)["frictionless"] == "BLOCKER"


def test_single_seed_and_single_ticker_warn(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {"results": str(tmp_path / "c.jsonl")},
                "axes": {
                    "ticker": ["ETH_USD"],
                    "seed": [42],
                    "friction": [{"fee_rate": 0.0026, "slippage": 0.0005}],
                    ".data_window": [{
                        "since": "2026-09-01T00:00:00Z",
                        "until": "2026-09-20T00:00:00Z",
                    }],
                },
            }
        )
    )
    codes = _codes(path)
    assert codes["few_seeds"] == "WARN"
    assert codes["single_ticker"] == "WARN"


def test_no_seed_axis_is_a_blocker(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {"results": str(tmp_path / "c.jsonl")},
                "axes": {"ticker": ["ETH_USD", "SOL_USD"]},
            }
        )
    )
    assert _codes(path)["no_seed_axis"] == "BLOCKER"


def test_no_algo_axis_is_a_note_not_a_failure(tmp_path):
    codes = _codes(_write_spec(tmp_path / "m.yaml"))
    assert codes["no_algo_axis"] == "NOTE"


def test_well_formed_matrix_has_no_blockers(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {
                    "base_config": str(REPO_ROOT / "configs" / "default.yaml"),
                    "results": str(tmp_path / "c.jsonl"),
                },
                "axes": {
                    "ticker": ["ETH_USD", "SOL_USD"],
                    "seed": [1, 2, 3, 4, 5],
                    "friction": [{"fee_rate": 0.0026, "slippage": 0.0005}],
                    ".data_window": [{
                        "since": "2026-09-01T00:00:00Z",
                        "until": "2026-09-20T00:00:00Z",
                    }],
                },
            }
        )
    )
    codes = _codes(path)
    assert "no_data_window" not in codes
    assert "frictionless" not in codes
    assert "no_seed_axis" not in codes
    assert "single_ticker" not in codes
    assert "thin_replication" not in codes


def test_shipped_example_config_loads_and_expands():
    spec = load_spec(REPO_ROOT / "configs" / "matrix.example.yaml")
    cells = expand_cells(spec)
    assert spec.axes["seed"] == [42, 43, 44]
    assert len(cells) == 2 * 3 * 2  # tickers x seeds x friction
    assert all(c.expected_bars for c in cells)


def test_example_base_config_resolves_from_the_repo_root(tmp_path):
    """`configs/matrix.example.yaml` says `configs/default.yaml`.

    Resolving that against the spec's own directory would produce
    `configs/configs/default.yaml`, so the CWD must win.
    """
    from tools.model_matrix import _as_path

    resolved = _as_path(
        "configs/default.yaml",
        REPO_ROOT / "configs",
        existing=True,
    )
    assert resolved == REPO_ROOT / "configs" / "default.yaml"
    assert resolved.is_file()


def test_plan_reports_absent_base_config_keys_without_failing(tmp_path):
    """A base config missing a key the harness wants must NOT crash plan, and
    must be reported as a NOTE rather than a BLOCKER.

    Uses a SYNTHETIC base config rather than configs/default.yaml, so the
    assertion is about the harness's behaviour and not about which keys the
    shipped default happens to carry today. (It previously read the real
    default.yaml and asserted data_window was absent -- true when this test
    was written, false as soon as the RL side landed the data_window block.)
    """
    base = tmp_path / "base.yaml"
    base.write_text(
        yaml.safe_dump(
            {
                "ticker": "ETH/USD",
                "fee_rate": 0.0,
                "slippage": 0.0,
                # deliberately NO data_window, NO market_data_store
            }
        )
    )
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {
                    "base_config": str(base),
                    "results": str(tmp_path / "c.jsonl"),
                },
                "axes": {
                    "ticker": ["ETH_USD", "SOL_USD"],
                    "seed": [1, 2, 3],
                    "friction": [{"fee_rate": 0.0026, "slippage": 0.0005}],
                },
            }
        )
    )
    codes = _codes(path)
    assert codes["base_config_missing_keys"] == "NOTE"
    assert "base_config_missing" not in codes
    # And the run is still refused as a comparison, because no window is pinned.
    assert codes["no_data_window"] == "BLOCKER"


# ── run / resume via a FAKE CLI ─────────────────────────────────────────


def _fake_cli(tmp_path: Path, script: str) -> str:
    path = tmp_path / "fake-bot"
    path.write_text("#!/usr/bin/env python3\n" + script)
    path.chmod(0o755)
    return str(path)


FAKE_OK = """
import json, sys
args = sys.argv[1:]
if args[0] == "train":
    print(json.dumps({
        "ticker_id": "ETH_USD", "model_name": "m", "n_features": 60,
        "n_bars": 504, "timesteps": 3000, "seed": 42,
        "model_path": "/tmp/x/model.zip",
    }))
    sys.exit(0)
payload = {
    "ticker_id": "ETH_USD", "model_name": "m", "action_space": "discrete",
    "total_return": 0.10,
    "sharpe": 1.1, "max_drawdown": 0.09, "num_trades": 40,
    "win_rate": 0.5, "equity_curve": [10000.0, 11000.0], "n_steps": 504,
    "final_equity": 11000.0, "seed": 42, "buy_hold_return": 0.04,
    "excess_return": 0.06, "buy_hold_max_drawdown": 0.12, "n_bars": 504,
    "fee_rate": 0.0026, "slippage": 0.0005,
}
print(json.dumps(payload))
sys.exit(0)
"""


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_run_writes_one_jsonl_record_per_cell(tmp_path):
    spec_path = _write_spec(tmp_path / "m.yaml")
    cli = _fake_cli(tmp_path, FAKE_OK)
    rc = cmd_run(
        _Args(spec=spec_path, force=False, limit=0, dry_run=False, cli=cli,
              work_dir=str(tmp_path / "work"))
    )
    assert rc == 0
    records = load_records(tmp_path / "cells.jsonl")
    assert len(records) == 6  # 2 tickers x 3 seeds
    assert all(r["status"] in {"ok", "invalid"} for r in records)
    assert len({r["cell_id"] for r in records}) == 6


def test_run_is_resumable_and_skips_completed_cells(tmp_path):
    spec_path = _write_spec(tmp_path / "m.yaml")
    cli = _fake_cli(tmp_path, FAKE_OK)
    args = lambda **kw: _Args(
        spec=spec_path, force=False, limit=0, dry_run=False, cli=cli,
        work_dir=str(tmp_path / "work"), **kw
    )
    cmd_run(args())
    first = len(load_records(tmp_path / "cells.jsonl"))
    # Second pass: nothing left to do, and nothing duplicated.
    cmd_run(args())
    assert len(load_records(tmp_path / "cells.jsonl")) == first == 6


def test_run_force_reruns_every_cell(tmp_path):
    spec_path = _write_spec(tmp_path / "m.yaml")
    cli = _fake_cli(tmp_path, FAKE_OK)
    args = lambda **kw: _Args(
        spec=spec_path, force=True, limit=0, dry_run=False, cli=cli,
        work_dir=str(tmp_path / "work"), **kw
    )
    cmd_run(args())
    cmd_run(args())
    # Last-write-wins on cell_id, so 6 unique ids but 12 lines.
    records = load_records(tmp_path / "cells.jsonl")
    assert len(records) == 12
    assert len({r["cell_id"] for r in records}) == 6


def test_run_limit_stops_early(tmp_path):
    spec_path = _write_spec(tmp_path / "m.yaml")
    cli = _fake_cli(tmp_path, FAKE_OK)
    cmd_run(
        _Args(spec=spec_path, force=False, limit=2, dry_run=False, cli=cli,
              work_dir=str(tmp_path / "work"))
    )
    assert len(load_records(tmp_path / "cells.jsonl")) == 2


def test_run_dry_run_records_commands_without_executing(tmp_path):
    spec_path = _write_spec(tmp_path / "m.yaml")
    cli = _fake_cli(tmp_path, FAKE_OK)
    cmd_run(
        _Args(spec=spec_path, force=False, limit=1, dry_run=True, cli=cli,
              work_dir=str(tmp_path / "work"))
    )
    record = load_records(tmp_path / "cells.jsonl")[0]
    assert record["status"] == "dry_run"
    assert record["commands"]["train"][1] == "train"
    assert "--json" in record["commands"]["backtest"]


def test_run_marks_a_degenerate_cell_invalid(tmp_path):
    """The fake bot reports the measured failure: 1 bar of 721, 0 trades."""
    degenerate = (
        FAKE_OK.replace('"num_trades": 40', '"num_trades": 0')
        .replace('"sharpe": 1.1', '"sharpe": 0.0')
        .replace('"total_return": 0.10', '"total_return": 0.0')
        .replace('"excess_return": 0.06', '"excess_return": 0.0')
        .replace(
            '"excess_return": 0.06, "buy_hold_max_drawdown": 0.12, "n_bars": 504',
            '"excess_return": 0.0, "buy_hold_max_drawdown": 0.12, "n_bars": 1',
        )
    )
    spec_path = _write_spec(tmp_path / "m.yaml")
    cli = _fake_cli(tmp_path, degenerate)
    cmd_run(
        _Args(spec=spec_path, force=False, limit=1, dry_run=False, cli=cli,
              work_dir=str(tmp_path / "work"))
    )
    record = load_records(tmp_path / "cells.jsonl")[0]
    assert record["status"] == "invalid"
    assert "zero_trades:0" in record["invalid_reasons"]


def test_run_records_a_nonzero_exit_as_an_error(tmp_path):
    failing = FAKE_OK.replace("sys.exit(0)", "sys.exit(3)")
    spec_path = _write_spec(tmp_path / "m.yaml")
    cli = _fake_cli(tmp_path, failing)
    rc = cmd_run(
        _Args(spec=spec_path, force=False, limit=1, dry_run=False, cli=cli,
              work_dir=str(tmp_path / "work"))
    )
    assert rc == 1
    record = load_records(tmp_path / "cells.jsonl")[0]
    assert record["status"] == "error"
    assert "returncodes" in record


# ── report ──────────────────────────────────────────────────────────────


def _write_records(path: Path, records) -> Path:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def _seeded_records():
    """2 tickers x 3 seeds x 2 friction, with one degenerate cell."""
    records = []
    idx = 0
    for ticker in ("ETH_USD", "SOL_USD"):
        for friction, base in ((0.0, 0.090), (0.0026, 0.011)):
            for i, seed in enumerate((42, 43, 44)):
                excess = base + (i - 1) * 0.02  # spread across seeds
                degenerate = ticker == "SOL_USD" and friction == 0.0 and seed == 44
                payload = _backtest_json(
                    ticker_id=ticker,
                    seed=seed,
                    excess_return=0.0 if degenerate else excess,
                    total_return=0.04 if degenerate else excess + 0.05,
                    buy_hold_return=0.05,
                    num_trades=0 if degenerate else 40 + i,
                    n_bars=1 if degenerate else 504,
                    sharpe=0.0 if degenerate else 1.0 + i,
                    fee_rate=friction,
                    slippage=friction,
                )
                records.append(
                    {
                        "cell_id": f"c{idx:02d}",
                        "index": idx,
                        "status": "ok",
                        "params": {
                            "ticker": ticker,
                            "seed": seed,
                            "friction": {"fee_rate": friction,
                                         "slippage": friction},
                        },
                        "config_overrides": {
                            "fee_rate": friction,
                            "slippage": friction,
                            "data_window": {
                                "since": "2026-09-01T00:00:00Z",
                                "until": "2026-09-20T00:00:00Z",
                                "eval_split": 0.7,
                            },
                        },
                        "expected_bars": 456,
                        "backtest": payload,
                    }
                )
                idx += 1
    return records


def test_report_excludes_invalid_cells_from_aggregates(tmp_path, capsys):
    path = _write_records(tmp_path / "cells.jsonl", _seeded_records())
    assert cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                            min_trades=1, json=False)) == 0
    out = capsys.readouterr().out
    assert "11 valid" in out and "1 INVALID" in out
    assert "zero_trades:0" in out
    assert "n_bars:1<" in out


def test_report_medians_use_the_replicates_not_one_run(tmp_path, capsys):
    path = _write_records(tmp_path / "cells.jsonl", _seeded_records())
    cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                     min_trades=1, json=False))
    out = capsys.readouterr().out
    # ETH frictionless group: excess 0.07/0.09/0.11 -> median 0.09.
    assert "+9.00%" in out
    assert "PER-AXIS MARGINALS" in out
    assert "axis: seed" in out


def test_report_states_what_the_sample_supports(tmp_path, capsys):
    path = _write_records(tmp_path / "cells.jsonl", _seeded_records())
    cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                     min_trades=1, json=False))
    out = capsys.readouterr().out
    assert "WHAT THIS SAMPLE SUPPORTS" in out
    assert "576 trades and 374" in out  # the multi-seed justification
    assert "excluded as degenerate" in out


def test_report_json_is_machine_readable(tmp_path, capsys):
    path = _write_records(tmp_path / "cells.jsonl", _seeded_records())
    cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                     min_trades=1, json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["cells_valid"] == 11
    assert payload["cells_invalid"] == 1
    assert payload["headline_metric"] == "excess_return"
    assert payload["invalid"][0]["reasons"]
    assert payload["claims"]


def test_report_never_claims_a_tradeable_result_when_all_cells_are_frictionless(
    tmp_path, capsys
):
    records = _seeded_records()
    for record in records:
        record["backtest"]["fee_rate"] = 0.0
        record["backtest"]["slippage"] = 0.0
    path = _write_records(tmp_path / "cells.jsonl", records)
    cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                     min_trades=1, json=False))
    out = capsys.readouterr().out
    assert "FRICTIONLESS" in out
    assert "supports no tradeable claim" in out


def test_report_refuses_to_compare_unpinned_cells(tmp_path, capsys):
    records = _seeded_records()
    for record in records:
        # An unpinned cell has no window in its config and no expected
        # bar count -- that is the shape a real unpinned run produces.
        record["expected_bars"] = None
        record["config_overrides"].pop("data_window", None)
        record["out_of_sample"] = False
    path = _write_records(tmp_path / "cells.jsonl", records)
    cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                     min_trades=1, json=False))
    out = capsys.readouterr().out
    assert "NO OUT-OF-SAMPLE CELLS" in out
    assert "empty BY DESIGN" in out


def test_report_with_zero_valid_cells_says_nothing(tmp_path, capsys):
    records = _seeded_records()
    for record in records:
        # every cell degenerate -> nothing left to aggregate
        record["backtest"]["n_bars"] = 1
        record["backtest"]["num_trades"] = 0
    path = _write_records(tmp_path / "cells.jsonl", records)
    cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                     min_trades=1, json=False))
    out = capsys.readouterr().out
    assert "NOTHING" in out


def test_report_skips_unparseable_jsonl_lines(tmp_path, capsys):
    path = tmp_path / "cells.jsonl"
    path.write_text(
        json.dumps(_seeded_records()[0]) + "\nnot json at all\n",
        encoding="utf-8",
    )
    cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                     min_trades=1, json=False))
    assert "unparseable JSONL line skipped" in capsys.readouterr().err


def test_report_empty_results_file_exits_nonzero(tmp_path, capsys):
    path = tmp_path / "cells.jsonl"
    path.write_text("", encoding="utf-8")
    assert cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                            min_trades=1, json=False)) == 1
    assert "No records" in capsys.readouterr().out


# ── CLI surface ─────────────────────────────────────────────────────────


def _cli(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )


def test_cli_plan_on_the_shipped_example(tmp_path):
    proc = _cli("plan", str(REPO_ROOT / "configs" / "matrix.example.yaml"))
    assert proc.returncode == 0, proc.stderr
    assert "MODEL MATRIX PLAN" in proc.stdout
    assert "CELLS       12" in proc.stdout
    assert "VALIDITY WARNINGS" in proc.stdout


def test_cli_plan_json_and_strict(tmp_path):
    example = str(REPO_ROOT / "configs" / "matrix.example.yaml")
    ok = _cli("plan", example, "--json")
    payload = json.loads(ok.stdout)
    assert payload["cells"] == 12
    assert payload["axes"]["seed"] == 3

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {"results": str(tmp_path / "c.jsonl")},
                "axes": {"ticker": ["ETH_USD"], "seed": [42]},
            }
        )
    )
    strict = _cli("plan", str(bad), "--strict")
    assert strict.returncode == 1
    assert "BLOCKER" in strict.stdout
    # Non-strict warns but does not fail.
    assert _cli("plan", str(bad)).returncode == 0


def test_cli_help_lists_all_three_subcommands():
    proc = _cli("--help")
    for name in ("plan", "run", "report"):
        assert name in proc.stdout

# ── RL-side contract clarifications (18-key JSON, window/split) ──────────


def test_required_fields_include_action_space():
    """to_dict() carries 18 keys, not 12 -- action_space was added."""
    assert "action_space" in REQUIRED_BACKTEST_FIELDS
    assert len(REQUIRED_BACKTEST_FIELDS) == 18


def test_missing_action_space_is_invalid():
    payload = _backtest_json()
    del payload["action_space"]
    record = _record()
    record["backtest"] = payload
    reasons = assess_cell(record, expected_bars=504)
    assert any(r.startswith("missing_field:") for r in reasons)


def test_null_n_bars_is_invalid_not_zero():
    """A pre-provenance artifact reports n_bars: null, meaning UNKNOWN.

    Treating it as 0 would be a false claim, and treating it as "present"
    would let the width guard pass on a cell we know nothing about.
    """
    reasons = assess_cell(_record(n_bars=None), expected_bars=504)
    assert "null_field:n_bars" in reasons
    assert is_valid(_record(n_bars=None), expected_bars=504) is False


def test_window_pinned_requires_both_bounds():
    assert window_is_pinned(
        {"data_window": {"since": "2026-09-01", "until": "2026-09-20"}}
    )
    # A key present but null counts as UNSET.
    assert not window_is_pinned(
        {"data_window": {"since": "2026-09-01", "until": None}}
    )
    assert not window_is_pinned({"data_window": {"since": None, "until": None}})
    assert not window_is_pinned({})


def test_eval_split_is_inert_without_a_pinned_window():
    """The critical one: eval_split set against a null window is INERT."""
    inert = {"data_window": {"since": None, "until": None, "eval_split": 0.7}}
    assert not window_has_split(inert)
    assert not cell_is_out_of_sample(inert)

    live = {
        "data_window": {
            "since": "2026-09-01", "until": "2026-09-20", "eval_split": 0.7
        }
    }
    assert window_has_split(live)
    assert cell_is_out_of_sample(live)


def test_pinned_window_without_split_is_still_in_sample():
    """Pinned but no eval_split -> the whole window is in-sample."""
    pinned_no_split = {"data_window": {"since": "2026-09-01", "until": "2026-09-20"}}
    assert window_is_pinned(pinned_no_split)
    assert not window_has_split(pinned_no_split)
    assert not cell_is_out_of_sample(pinned_no_split)


def test_plan_flags_inert_eval_split_as_a_blocker(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {"results": str(tmp_path / "c.jsonl")},
                "axes": {
                    "ticker": ["ETH_USD", "SOL_USD"],
                    "seed": [1, 2, 3],
                    "friction": [{"fee_rate": 0.0026, "slippage": 0.0005}],
                    ".data_window": [
                        {"since": None, "until": None, "eval_split": 0.7}
                    ],
                },
            }
        )
    )
    assert _codes(path)["eval_split_inert"] == "BLOCKER"


def test_report_separates_in_sample_from_out_of_sample(tmp_path, capsys):
    """In-sample cells must never be averaged with out-of-sample ones."""
    records = _seeded_records()
    # Make every cell in-sample by unpinning the window.
    for record in records:
        record["out_of_sample"] = False
    path = _write_records(tmp_path / "cells.jsonl", records)
    cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                     min_trades=1, json=False))
    out = capsys.readouterr().out
    assert "IN-SAMPLE CELLS" in out
    assert "NO OUT-OF-SAMPLE CELLS" in out
    assert "EXCLUDED from every aggregate" in out


def test_report_counts_out_of_sample_cells(tmp_path, capsys):
    path = _write_records(tmp_path / "cells.jsonl", _seeded_records())
    cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                     min_trades=1, json=False))
    out = capsys.readouterr().out
    assert "out-of-sample" in out
    assert "IN-SAMPLE" in out


def test_report_json_reports_both_cohorts(tmp_path, capsys):
    path = _write_records(tmp_path / "cells.jsonl", _seeded_records())
    cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                     min_trades=1, json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["cells_out_of_sample"] == 11
    assert payload["cells_in_sample"] == 0


# ── train-side vs backtest-side config split ────────────────────────────


def test_split_cell_overrides_separates_the_six_backtest_keys():
    overrides = {
        "fee_rate": 0.0026,
        "slippage": 0.0005,
        "action_space": "discrete",
        "initial_balance": 10_000.0,
        "market_data_store": "/tmp/store",
        "data_window": {"since": "2026-09-01", "until": "2026-09-20"},
        "reward": {"mode": "sharpe"},
        "feature_windows": [1, 4],
    }
    backtest_side, train_only = split_cell_overrides(overrides)
    assert set(backtest_side) == BACKTEST_EXPRESSIBLE_KEYS
    assert train_only == ["feature_windows", "reward"]


def test_train_only_axis_is_reported_by_plan(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {
                    "base_config": str(REPO_ROOT / "configs" / "default.yaml"),
                    "results": str(tmp_path / "c.jsonl"),
                },
                "axes": {
                    "ticker": ["ETH_USD", "SOL_USD"],
                    "seed": [1, 2, 3],
                    "reward": [
                        {"mode": "pnl", "drawdown_penalty": 0.0},
                        {"mode": "sharpe", "risk_adjusted": 1.0},
                    ],
                    ".data_window": [
                        {"since": "2026-09-01", "until": "2026-09-20",
                         "eval_split": 0.7}
                    ],
                },
            }
        )
    )
    codes = _codes(path)
    assert codes["train_only_axis"] == "NOTE"


def test_materialize_configs_writes_a_narrower_backtest_config(tmp_path):
    """A backtest --config must not carry train-only keys.

    The RL side reads reward/feature_* from the MODEL's own config
    because a mismatch there is silent-corruption class; shipping them in
    a backtest config would either be ignored or silently misalign the
    replay.
    """
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {
                    "base_config": str(REPO_ROOT / "configs" / "default.yaml"),
                    "results": str(tmp_path / "c.jsonl"),
                },
                "axes": {
                    "ticker": ["ETH_USD"],
                    "seed": [1],
                    ".reward": [{"mode": "sharpe", "risk_adjusted": 1.0}],
                    "friction": [{"fee_rate": 0.0026, "slippage": 0.0005}],
                },
            }
        )
    )
    spec = load_spec(path)
    cell = expand_cells(spec)[0]
    train_path, backtest_path = materialize_configs(cell, spec, tmp_path / "cfg")
    train_data = yaml.safe_load(train_path.read_text())
    backtest_data = yaml.safe_load(backtest_path.read_text())
    # The reward axis reaches the TRAIN config only.
    assert train_data["reward"]["mode"] == "sharpe"
    assert "reward" not in backtest_data
    assert "feature_windows" not in backtest_data
    assert "signal_require_ticker" not in backtest_data
    # Both still carry the six expressible keys.
    assert backtest_data["fee_rate"] == 0.0026
    assert backtest_data["ticker"] == "ETH/USD"


# ── RL-side refusal classification ──────────────────────────────────────


def _errored(stderr: str, rc: int = 1):
    return {
        "cell_id": "e1",
        "status": "error",
        "returncodes": {"train": 0, "backtest": rc},
        "stderr_tail": [stderr],
        "backtest": None,
    }


def test_missing_config_file_is_classified_not_retried():
    reasons = assess_cell(_errored("FileNotFoundError: configs/nope.yaml"))
    assert "config_not_found" in reasons
    assert "process_failed" in reasons


def test_action_space_mismatch_is_invalid():
    reasons = assess_cell(
        _errored("ActionSpaceMismatchError: model=continuous run=discrete")
    )
    assert "action_space_mismatch" in reasons


def test_no_tradable_bar_value_error_is_invalid():
    """A pinned window shorter than the warm-up RAISES, rather than
    returning n_bars: 0 with metrics divided by nothing."""
    reasons = assess_cell(
        _errored("ValueError: pinned window leaves no tradable bar")
    )
    assert "no_tradable_bar" in reasons


def test_healthy_record_produces_no_failure_reasons():
    assert classify_process_failure(_record()) == []


# ── Real CLI refusals, verbatim ────────────────────────────────────────
#
# Captured 2026-10-01 by running the real kraken-trading-bot binary. The
# point of these is the MESSAGE: cmd_backtest catches Exception and prints
# "Error backtesting <t>/<m>: {e}", so the Python class name never
# reaches stdout/stderr. Matching only on the class name silently
# degraded every real refusal to a bare process_failed -- the opaque rc=1
# classify_process_failure exists to prevent.

# The 2026-10-01 capture led with the seam's "Extra features file not found:
# ... -- skipping signal merge" WARNING.  That line cannot be printed any
# more: G1 made a CONFIGURED-but-unresolvable signal path raise by name
# instead of being logged and skipped (see SignalFileNotFoundError), so a run
# with a missing funding file now refuses before it can reach the
# action-space check.  The warning line is removed rather than reworded so
# this fixture stays a verbatim capture of a state that is still reachable
# (an explicit --config, or any run on a checkout whose funding file does
# resolve) — the refusal's own text is pinned separately below.
REAL_ACTION_SPACE_MISMATCH = [
    "2026-10-01 13:36:46 [ERROR] kraken_trading_bot.cli: Backtesting "
    "ETH_USD/mv_probe failed: Action-space mismatch (ETH_USD/mv_probe "
    "backtest): the model was trained as 'continuous' but this backtest "
    "would replay it as 'discrete'. A policy bound to the wrong action "
    "space emits actions it never learned to emit, so its return is "
    "meaningless.",
    "Error backtesting ETH_USD/mv_probe: Action-space mismatch "
    "(ETH_USD/mv_probe backtest): the model was trained as 'continuous' "
    "but this backtest would replay it as 'discrete'.",
]

REAL_CONFIG_NOT_FOUND = [
    "2026-10-01 13:36:00 [ERROR] kraken_trading_bot.cli: Backtesting "
    "ETH_USD/mv_probe failed: Backtest config not found: /tmp/mv/NOPE.yaml. "
    "Pass --config with an existing YAML file (e.g. configs/default.yaml), "
    "or omit it to replay with the model's own config.",
    "Error backtesting ETH_USD/mv_probe: Backtest config not found: "
    "/tmp/mv/NOPE.yaml. Pass --config with an existing YAML file (e.g. "
    "configs/default.yaml), or omit it to replay with the model's own "
    "training config.",
]


# The same CLI, same day, after G1: a run whose configured signal file does
# not resolve now refuses AT THE SEAM, by name.  Produced by
# `merge_extra_features(df, "~/signals/eth_usd_funding.jsonl",
# config_key="funding_features_file")` and pasted through the same
# "Error backtesting <t>/<m>: {e}" print the CLI actually uses.
REAL_SIGNAL_FILE_REFUSED = [
    "2026-10-02 09:14:02 [ERROR] kraken_trading_bot.cli: Backtesting "
    "ETH_USD/mv_probe failed: funding_features_file is set to "
    "'~/signals/eth_usd_funding.jsonl' but no file exists at "
    "/home/seanc/signals/eth_usd_funding.jsonl (expanded from "
    "'~/signals/eth_usd_funding.jsonl'). Produce it with: just funding-pull. "
    "Or set funding_features_file to null to run without this channel: a null "
    "key means off and is silent, a set key is a declared intent to use it.",
    "Error backtesting ETH_USD/mv_probe: funding_features_file is set to "
    "'~/signals/eth_usd_funding.jsonl' but no file exists at "
    "/home/seanc/signals/eth_usd_funding.jsonl (expanded from "
    "'~/signals/eth_usd_funding.jsonl'). Produce it with: just funding-pull. "
    "Or set funding_features_file to null to run without this channel: a null "
    "key means off and is silent, a set key is a declared intent to use it.",
]


def test_a_refused_signal_file_is_named_and_not_mistaken_for_another_cause():
    """The refusal gets its own reason, and never borrows another's.

    G1's message names a *config key* and a missing file, which is close
    enough to several things this classifier already matches on.  Pinning
    that it matches NONE of them is what stops a future rewording from
    silently reclassifying "your funding file is missing" as "your --config
    is missing" — two different fixes for the reader.

    It carries ``signal_file_not_found`` rather than a bare
    ``process_failed`` because that bare rc is the opaque case this
    function exists to prevent, and the state is the *shipped* one:
    ``configs/default.yaml`` sets ``funding_features_file`` non-null, so a
    checkout without that file turns every cell of a matrix into this same
    refusal.  One opaque code for the whole grid tells the reader nothing
    about the single fix that would make all of them pass.
    """
    reasons = classify_process_failure(_real_errored(REAL_SIGNAL_FILE_REFUSED))
    assert reasons == ["process_failed", "signal_file_not_found"]
    for other in ("config_not_found", "action_space_mismatch", "no_tradable_bar"):
        assert other not in reasons
    # And it reaches the user as a named reason rather than a bare rc.
    cell = assess_cell(_real_errored(REAL_SIGNAL_FILE_REFUSED))
    assert "signal_file_not_found" in cell
    assert "process_failed" in cell


def test_an_empty_signal_file_is_the_same_named_reason_not_a_different_one():
    """Zero records is the same reader-facing cause as no file.

    ``SignalFileNotFoundError`` words the two differently ("holds no
    records" rather than "no file exists at"), and both open with the config
    key.  They want the same action — run the producer — so they must not
    drift into two codes that a report would print as two unrelated faults.
    """
    empty = [
        "Error backtesting ETH_USD/mv_probe: funding_features_file is set to "
        "'~/signals/eth_usd_funding.jsonl' and /home/seanc/signals/"
        "eth_usd_funding.jsonl (expanded from '~/signals/eth_usd_funding.jsonl') "
        "holds no records — an empty log is a producer failure, not a "
        "first-run state. Produce it with: just funding-pull.",
    ]
    reasons = classify_process_failure(_real_errored(empty))
    assert "signal_file_not_found" in reasons
    assert "config_not_found" not in reasons


def test_a_missing_config_is_still_not_confused_with_a_missing_signal_file():
    """The two "your path is wrong" refusals must stay distinguishable.

    Both name a nonexistent path, so a loose matcher here would send a
    reader to write a YAML file when the actual fix is to run a producer
    (or to set the key back to ``null``).  Guards the boundary in both
    directions.
    """
    config_only = classify_process_failure(_real_errored(REAL_CONFIG_NOT_FOUND))
    assert "config_not_found" in config_only
    assert "signal_file_not_found" not in config_only

    # And a bad --config must not be dragged into the signal reason by a
    # model config that merely *mentions* a signal key somewhere.
    assert "signal_file_not_found" not in classify_process_failure(
        _real_errored(["Error backtesting ETH_USD/mv_probe: Backtest config "
                       "not found: /nope.yaml. funding_features_file was "
                       "never read."])
    )


def _real_errored(stderr_tail):
    record = {
        "cell_id": "abc123",
        "status": "error",
        "params": {"ticker": "ETH_USD"},
        "returncodes": {"train": 0, "backtest": 1},
        "error": "train rc=0, backtest rc=1; " + stderr_tail[-1][:400],
        "stderr_tail": stderr_tail,
        "backtest": None,
    }
    return record


def test_real_action_space_mismatch_is_classified_not_just_process_failed():
    reasons = classify_process_failure(
        _real_errored(REAL_ACTION_SPACE_MISMATCH)
    )
    assert "action_space_mismatch" in reasons
    assert "process_failed" in reasons


def test_real_config_not_found_is_classified_not_just_process_failed():
    reasons = classify_process_failure(_real_errored(REAL_CONFIG_NOT_FOUND))
    assert "config_not_found" in reasons
    assert "process_failed" in reasons


def test_real_refusals_reach_assess_cell_as_specific_reasons():
    """The reason must survive into the INVALID block the user reads."""
    assert "action_space_mismatch" in assess_cell(
        _real_errored(REAL_ACTION_SPACE_MISMATCH)
    )
    assert "config_not_found" in assess_cell(
        _real_errored(REAL_CONFIG_NOT_FOUND)
    )


def test_traceback_spelling_still_classifies():
    """An uncaught raise would carry the class name instead."""
    assert "action_space_mismatch" in classify_process_failure(
        _real_errored(["ActionSpaceMismatchError: trained continuous"])
    )
    assert "config_not_found" in classify_process_failure(
        _real_errored(["FileNotFoundError: no such config"])
    )


# ── --pages sizing and equity-curve trimming ───────────────────────────


def test_plan_warns_when_pages_may_undershoot_the_window(tmp_path):
    """Window bounds are a CLIP, not a push-down, so too few pages
    silently yields a short window visible only in n_bars."""
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {"results": str(tmp_path / "c.jsonl")},
                "axes": {
                    "ticker": ["ETH_USD", "SOL_USD"],
                    "seed": [1, 2, 3],
                    "pages": [1],
                    "friction": [{"fee_rate": 0.0026, "slippage": 0.0005}],
                    ".data_window": [
                        {"since": "2026-09-01", "until": "2026-09-20",
                         "eval_split": 0.7}
                    ],
                },
            }
        )
    )
    assert _codes(path)["pages_may_undershoot"] == "WARN"


def test_trim_payload_drops_equity_curve_but_keeps_a_summary():
    from tools.model_matrix import trim_payload

    payload = trim_payload(_backtest_json(equity_curve=[100.0, 50.0, 120.0]))
    assert payload["equity_curve"] == []
    assert payload["equity_curve_summary"]["n_points"] == 3
    assert payload["equity_curve_summary"]["first"] == 100.0
    assert payload["equity_curve_summary"]["last"] == 120.0
    assert payload["equity_curve_summary"]["min"] == 50.0


def test_run_records_stay_small_after_trimming(tmp_path):
    """~4 KB of floats per cell would bloat the JSONL across a matrix."""
    spec_path = _write_spec(tmp_path / "m.yaml")
    big = _backtest_json(equity_curve=[float(i) for i in range(720)])
    script = (
        "import json, sys\n"
        "a = sys.argv[1:]\n"
        "if a[0] == 'train':\n"
        "    print(json.dumps({'ticker_id': 'ETH_USD', 'model_name': 'm',"
        " 'n_features': 60, 'n_bars': 504, 'timesteps': 3000, 'seed': 42,"
        " 'model_path': '/tmp/x.zip'}))\n"
        "    sys.exit(0)\n"
        f"print(json.dumps({big!r}))\n"
    )
    cli = _fake_cli(tmp_path, script)
    cmd_run(
        _Args(spec=spec_path, force=False, limit=1, dry_run=False, cli=cli,
              work_dir=str(tmp_path / "work"))
    )
    record = load_records(tmp_path / "cells.jsonl")[0]
    assert record["backtest"]["equity_curve"] == []
    assert record["backtest"]["equity_curve_summary"]["n_points"] == 720
    assert record["backtest"]["total_return"] == 0.12


def test_run_records_the_out_of_sample_flag(tmp_path):
    spec_path = _write_spec(tmp_path / "m.yaml")
    cli = _fake_cli(tmp_path, FAKE_OK)
    cmd_run(
        _Args(spec=spec_path, force=False, limit=1, dry_run=False, cli=cli,
              work_dir=str(tmp_path / "work"))
    )
    record = load_records(tmp_path / "cells.jsonl")[0]
    # _write_spec has no data_window, so it must be flagged in-sample.
    assert record["window_pinned"] is False
    assert record["eval_split_active"] is False
    assert record["out_of_sample"] is False


# ── zero out-of-sample: no false reassurance ────────────────────────────


def test_zero_out_of_sample_cells_claim_no_false_reassurance(tmp_path, capsys):
    """With every cell in-sample, the claims must NOT say the sample is
    well-posed or that groups have enough replicates -- those branches
    read vacuously true on an empty cohort."""
    records = _seeded_records()
    for record in records:
        record["out_of_sample"] = False
    path = _write_records(tmp_path / "cells.jsonl", records)
    cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                     min_trades=1, json=False))
    out = capsys.readouterr().out
    assert "NO OUT-OF-SAMPLE CELLS" in out
    assert "empty BY DESIGN" in out
    # These would be FALSE here and must not be printed.
    assert "well-posed" not in out
    assert "at least seed-stable" not in out


def test_zero_out_of_sample_leaves_the_aggregates_empty(tmp_path, capsys):
    records = _seeded_records()
    for record in records:
        record["out_of_sample"] = False
    path = _write_records(tmp_path / "cells.jsonl", records)
    cmd_report(_Args(spec=None, results=str(path), min_bar_ratio=0.5,
                     min_trades=1, json=True))
    payload = json.loads(capsys.readouterr().out)
    # 12 recorded, 1 degenerate -> 11 valid, all of them in-sample.
    assert payload["cells_out_of_sample"] == 0
    assert payload["cells_in_sample"] == 11
    assert payload["per_ticker"][0]["groups"] == []


def test_mis_dotted_dict_axis_is_flagged(tmp_path):
    """`data_window:` without the dot FLATTENS, so the window silently
    never happens -- exactly the class of silent no-op this tool exists
    to catch."""
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {"results": str(tmp_path / "c.jsonl")},
                "axes": {
                    "ticker": ["ETH_USD", "SOL_USD"],
                    "seed": [1, 2, 3],
                    # NOTE: no leading dot -> flattens
                    "data_window": [
                        {"since": "2026-09-01", "until": "2026-09-20",
                         "eval_split": 0.7}
                    ],
                },
            }
        )
    )
    assert _codes(path)["axis_needs_dot"] == "WARN"


def test_correctly_dotted_axis_is_not_flagged(tmp_path):
    """The friction axis is SUPPOSED to flatten; only names that collide
    with a real config key are worth warning about."""
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "run": {"results": str(tmp_path / "c.jsonl")},
                "axes": {
                    "ticker": ["ETH_USD", "SOL_USD"],
                    "seed": [1, 2, 3],
                    "friction": [{"fee_rate": 0.0026, "slippage": 0.0005}],
                    ".data_window": [
                        {"since": "2026-09-01", "until": "2026-09-20",
                         "eval_split": 0.7}
                    ],
                },
            }
        )
    )
    assert "axis_needs_dot" not in _codes(path)
