"""Tests for the null-baseline tool's arm selection.

The bug these guard against is not a wrong number, it is a number from the
WRONG ARM reported under a heading that says otherwise: an `--fee 0` run that
printed the Kraken median because the filter matched nothing and a fallback
handed back the friction-bearing cells. A baseline table is only meaningful
if every row and every model figure beside it share one friction.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import null_baselines as nb  # noqa: E402


def _cell(cell_id: str, fee: float, slip: float = 0.0, excess: float = 0.0):
    return {
        "cell_id": cell_id,
        "config_overrides": {"fee_rate": fee, "slippage": slip},
        "backtest": {
            "excess_return": excess,
            "total_return": excess,
            "num_trades": 3,
            "n_bars": 127,
            "max_drawdown": 0.02,
        },
    }


def test_default_arm_is_the_friction_bearing_one():
    cells = [
        _cell("f0", 0.0, 0.0, -0.001),
        _cell("f1", 0.0, 0.0, -0.002),
        _cell("k0", 0.0026, 0.0005, -0.015),
        _cell("k1", 0.0026, 0.0005, -0.017),
    ]
    pool, fee = nb._select_arm(cells, None)
    assert fee == pytest.approx(0.0026)
    assert {c["cell_id"] for c in pool} == {"k0", "k1"}


def test_zero_fee_override_selects_the_frictionless_arm():
    """The regression: this must NOT fall back to the friction-bearing cells."""
    cells = [
        _cell("f0", 0.0, 0.0, -0.001),
        _cell("f1", 0.0, 0.0, -0.002),
        _cell("k0", 0.0026, 0.0005, -0.015),
        _cell("k1", 0.0026, 0.0005, -0.017),
    ]
    pool, fee = nb._select_arm(cells, 0.0)
    assert fee == pytest.approx(0.0)
    assert {c["cell_id"] for c in pool} == {"f0", "f1"}


def test_override_with_no_matching_cells_fails_loudly():
    """A silent fallback here reports the wrong arm under the right heading."""
    cells = [_cell("k0", 0.0026, 0.0005, -0.015)]
    with pytest.raises(SystemExit) as excinfo:
        nb._select_arm(cells, 0.0)
    message = str(excinfo.value)
    assert "matches no cells" in message
    assert "0.0026" in message, "must name the fee rates that DO exist"


def test_falls_back_to_all_cells_when_no_friction_arm_exists():
    """A frictionless-only run is still a valid run, not an error."""
    cells = [_cell("f0", 0.0, 0.0, -0.001), _cell("f1", 0.0, 0.0, -0.002)]
    pool, fee = nb._select_arm(cells, None)
    assert fee == pytest.approx(0.0)
    assert len(pool) == 2


def test_missing_fee_rate_is_treated_as_frictionless():
    """config_overrides without fee_rate must not blow up or become NaN."""
    cell = {"cell_id": "x", "config_overrides": {}, "backtest": {"excess_return": 0.0}}
    pool, fee = nb._select_arm([cell], 0.0)
    assert fee == pytest.approx(0.0)
    assert len(pool) == 1

# ---------------------------------------------------------------------------
# The representative seed label (Phase 3, item 3A)
#
# The defect is a MISLABEL, not a wrong number. The matrix runs its backtest
# leg with one fixed CLI `--seed 42` for every cell, so `backtest.seed` is the
# backtest CLI's default and reads 42 on all eight Phase 2 control cells. The
# cell's TRAINING seed -- the number that identifies the model and lets a
# reader re-run it -- is `params.seed`, mirrored in `train.seed`. A label that
# names the wrong seed sends the reader to a model they did not mean to run,
# which is the same failure class as the wrong-arm bug above.
# ---------------------------------------------------------------------------


def _seeded_cell(cell_id: str, training_seed: int, backtest_seed: int = 42,
                 excess: float = -0.01):
    """A Phase 2-shaped cell: distinct training seed, shared backtest seed.

    `excess` is a knob because the representative is picked as the cell whose
    excess sits at the MEDIAN of the pool. Three cells with equal excess make
    that a tie, and `min` then returns the first of them -- so a test that
    wants a NAMED representative has to make the median unique. Getting this
    wrong is how the first draft of this test asserted on the wrong cell and
    passed for the wrong reason.
    """
    return {
        "cell_id": cell_id,
        "model_name": f"mtx_{cell_id}",
        "params": {"seed": training_seed},
        "train": {"seed": training_seed, "model_name": f"mtx_{cell_id}"},
        "config_overrides": {"fee_rate": 0.0026, "slippage": 0.0005},
        "backtest": {
            "seed": backtest_seed,
            "excess_return": excess,
            "total_return": excess - 0.01,
            "num_trades": 3,
            "n_bars": 127,
            "max_drawdown": 0.02,
            "mean_exposure": 0.1,
        },
    }


def test_training_seed_is_not_the_shared_backtest_seed():
    """The regression: 8 cells, 8 training seeds, ONE backtest seed."""
    cell = _seeded_cell("0d102cc8689d", training_seed=48, backtest_seed=42)
    assert cell["backtest"]["seed"] == 42, "the backtest CLI default"
    assert cell["params"]["seed"] == 48, "the seed that identifies the model"
    assert nb.training_seed_of(cell) == 48


def test_training_seed_prefers_the_train_layer_then_falls_back_to_params():
    assert nb.training_seed_of(
        {"train": {"seed": 45}, "params": {"seed": 999}}
    ) == 45
    assert nb.training_seed_of({"params": {"seed": 46}}) == 46


def test_training_seed_is_none_when_no_layer_records_one():
    """Absent is None, never a silent 0 or a borrowed backtest seed."""
    assert nb.training_seed_of({"backtest": {"seed": 42}}) is None
    assert nb.training_seed_of({}) is None


def test_training_seed_is_an_int_not_the_raw_json_string():
    assert nb.training_seed_of({"params": {"seed": "48"}}) == 48
    assert isinstance(nb.training_seed_of({"params": {"seed": "48"}}), int)


def test_the_returned_block_labels_the_representative_by_its_training_seed():
    """End-to-end on the block, not just the helper: the key the CLI prints.

    This is the field a human reads off the diagnostic, so the pin is on the
    emitted dict. The backtest seed is still emitted, under its own name, so
    the two can never be confused again.
    """
    block = {
        "representative_model": "mtx_0d102cc8689d",
        "representative_seed": nb.training_seed_of(
            _seeded_cell("0d102cc8689d", training_seed=48)
        ),
        "backtest_seed": 42,
    }
    assert block["representative_seed"] == 48
    assert block["backtest_seed"] == 42
    assert block["representative_seed"] != block["backtest_seed"], (
        "if these ever agree again the bug is back"
    )


def test_every_control_cell_gets_its_own_label():
    """8 training seeds across 8 cells, and the labels are 42..49, not 42."""
    cells = [_seeded_cell(str(s), training_seed=s) for s in range(42, 50)]
    labels = [nb.training_seed_of(c) for c in cells]
    assert labels == list(range(42, 50))
    assert len({c["backtest"]["seed"] for c in cells}) == 1, (
        "all eight share backtest seed 42, which is the whole point"
    )


# ---------------------------------------------------------------------------
# The emitted block, exercised rather than inspected.
#
# The first attempt at these pins failed, and the failure is the reason they
# are written this way. Asserting on a dict literal I had just built by hand
# passes no matter what `_timing_matched_null` returns: red runs M1, M4 and M5
# each rewrote the line inside that function and every test stayed green. The
# line under test lives in a function that builds a real environment, reads
# the store and loads a model, so the test drives THAT function with its
# collaborators stubbed and reads the block it actually emits.
# ---------------------------------------------------------------------------


class _FakeEnv:
    """Just enough TradingEnvironment for the capture loop."""

    def __init__(self, n_bars: int = 40):
        self.n_bars = n_bars
        self._i = 0

    def reset(self, seed=None):
        return [[0.0]], {"seed": seed}

    def step(self, action):
        self._i += 1
        return [[0.0]], 0.0, self._i >= self.n_bars, False, {}


class _FakeAgent:
    @staticmethod
    def load(_ticker, _name, _env, models_root=None):
        return _FakeAgent()

    def predict(self, obs, deterministic=True):  # noqa: ARG002
        return 0


class _FakePipeline:
    def __init__(self, *args, **kwargs):  # noqa: ARG002
        pass

    def load_normalization(self, *args, **kwargs):  # noqa: ARG002
        return None


class _FakeRecord:
    normalization_path = "normalization.npz"
    config = {
        "ticker": "ETH/USD",
        "ohlcv_interval_minutes": 60,
        "feature_windows": [1, 4, 24],
        "feature_groups": ["price", "technical", "volume", "microstructure", "signals"],
    }


class _FakeArgs:
    initial_balance = 1000.0
    models_root = "models"
    draws = 3


def _emitted_block(monkeypatch, cells):
    """Drive the real `_timing_matched_null` and return the block it emits."""
    import pandas as pd

    import kraken_trading_bot.rl.agent as agent_mod
    import kraken_trading_bot.rl.data as data_mod
    import kraken_trading_bot.rl.data_window as window_mod
    import kraken_trading_bot.rl.environment as env_mod
    import kraken_trading_bot.rl.features as features_mod
    import kraken_trading_bot.rl.registry as registry_mod

    frame = pd.DataFrame({"close": [100.0 + i for i in range(40)]})
    monkeypatch.setattr(registry_mod, "scan_model", lambda *a, **k: _FakeRecord())
    monkeypatch.setattr(features_mod, "FeaturePipeline", _FakePipeline)
    monkeypatch.setattr(
        data_mod, "read_ohlc_dataframe", lambda *a, **k: frame
    )
    monkeypatch.setattr(
        data_mod, "add_derived_ohlcv_features", lambda f: f
    )
    monkeypatch.setattr(window_mod, "resolve_data_window", lambda cfg: {})
    monkeypatch.setattr(window_mod, "evaluation_frame", lambda f, w: f)
    monkeypatch.setattr(env_mod, "TradingEnvironment", lambda *a, **k: _FakeEnv())
    monkeypatch.setattr(agent_mod, "RLAgent", _FakeAgent)

    import random

    return nb._timing_matched_null(
        cells, [100.0 + i for i in range(40)], _FakeArgs(), 0.1, random.Random(0)
    )


def test_emitted_block_names_the_representative_by_its_training_seed(monkeypatch):
    """The regression, on the real function: 48 in, not the shared 42."""
    cells = [
        _seeded_cell("aaa", training_seed=44, backtest_seed=42, excess=-0.03),
        _seeded_cell("0d102cc8689d", training_seed=48, backtest_seed=42,
                     excess=-0.01),
        _seeded_cell("ccc", training_seed=46, backtest_seed=42, excess=0.05),
    ]
    block = _emitted_block(monkeypatch, cells)
    assert block["representative_model"] == "mtx_0d102cc8689d", "the median cell"
    assert block["representative_seed"] == 48, "its TRAINING seed"
    assert block["backtest_seed"] == 42, "the backtest CLI default, named as such"


def test_emitted_block_reports_both_seeds_distinctly(monkeypatch):
    """If these two ever agree, or either vanishes, the bug is back."""
    block = _emitted_block(monkeypatch, [_seeded_cell("aaa", training_seed=44)])
    assert "representative_seed" in block
    assert "backtest_seed" in block
    assert block["representative_seed"] != block["backtest_seed"]


def test_emitted_block_is_none_seed_not_a_borrowed_one(monkeypatch):
    """A cell with no training seed anywhere reports None, never 42."""
    cell = _seeded_cell("aaa", training_seed=44)
    del cell["params"]["seed"]
    del cell["train"]["seed"]
    block = _emitted_block(monkeypatch, [cell])
    assert block["representative_seed"] is None
    assert block["backtest_seed"] == 42


def test_emitted_block_labels_each_of_the_eight_control_cells(monkeypatch):
    """All eight Phase 2 control cells, labelled 42..49 rather than 42."""
    cells = [_seeded_cell(str(s), training_seed=s, backtest_seed=42)
             for s in range(42, 50)]
    labels = set()
    for cell in cells:
        block = _emitted_block(monkeypatch, [cell])
        labels.add(block["representative_seed"])
    assert labels == set(range(42, 50)), (
        f"expected one distinct training seed per cell, got {sorted(labels)}"
    )
