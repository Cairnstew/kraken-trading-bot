"""Which kind of measurement is this backtest? (commit ``c74e806``)

The shipped default has ``data_window.since``/``until`` **null**, so
:func:`training_frame` and :func:`evaluation_frame` hand back the *same
object* and no out-of-sample measurement is possible.  Unlabelled, that
result reads exactly like one -- which is the failure this file pins:

1. **The default is IN-SAMPLE.**  Not "the default happens not to claim
   OOS" -- the label is on the result, in ``to_dict()``, in ``--json``
   and in the human/log output, so a report cannot read the return as
   out-of-sample because the JSON it parsed happened to omit the field.
2. **A pinned run that really splits says OUT-OF-SAMPLE**, and only
   because both halves were *measured* non-empty and disjoint.
3. **A pinned window that covers nothing is a FAILED measurement, not an
   out-of-sample one.**  This is the negative case that matters most:
   ``has_split`` is ``True`` for such a window, so a label derived from
   ``has_split`` alone would bless a zero-bar run.
4. **A pin with nothing behind it raises a named error** carrying the
   remedy, and it is a **subclass** of ``NotEnoughDataError`` so every
   existing catcher keeps working.  It is deliberately *not* fired for an
   ordinary short frame -- "widen ``--pages``" is right there and "seed
   the store" is not.

Everything here is offline and hermetic: no network, no store, no
training, no ``models/`` writes.  ``backtest_model`` takes a
caller-supplied frame and a duck-typed agent, so the real evaluator and
the real label derivation are both driven directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pytest
import yaml

from kraken_trading_bot.cli import main
from kraken_trading_bot.rl import backtest_model
from kraken_trading_bot.rl.data import (
    NotEnoughDataError,
    PinnedWindowUnavailableError,
)
from kraken_trading_bot.rl.data_window import (
    IN_SAMPLE_LABEL,
    OUT_OF_SAMPLE_LABEL,
    evaluate_scope,
    evaluation_frame,
    resolve_data_window,
    training_frame,
)

# The builtin (no-pipeline) warm-up boundary: 20-bar rolling window plus a
# 5-bar return.  Same constant ``tests/test_rl_validity.py`` pins.
_BUILTIN_WARMUP = 24

# Kraken's REST OHLC leg serves this many bars and says outright that older
# data cannot be retrieved regardless of ``since``.
_LIVE_CEILING = 721


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _ramp_ohlcv(n: int = 240, start: str = "2024-01-01") -> pd.DataFrame:
    """OHLCV frame whose close is exactly ``100 + i`` for bar ``i``."""
    idx = pd.date_range(start, periods=n, freq="h", tz="UTC")
    close = 100.0 + np.arange(n, dtype=float)
    open_ = close - 1.0
    return pd.DataFrame(
        {
            "open": open_,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "vwap": close,
            "volume": np.full(n, 100.0),
            "count": np.full(n, 50.0),
        },
        index=idx,
    )


def _walk_ohlcv(n: int = 240, seed: int = 61) -> pd.DataFrame:
    """A rising random walk, so buy-and-hold is clearly positive."""
    rng = np.random.default_rng(seed)
    close = 2000.0 * np.exp(np.cumsum(rng.normal(0.001, 0.01, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.002,
            "low": np.minimum(open_, close) * 0.998,
            "close": close,
            "vwap": close,
            "volume": rng.uniform(50.0, 200.0, n),
            "count": rng.integers(10, 100, n).astype(float),
        },
        index=idx,
    )


class _HoldEveryBar:
    """Duck-typed agent that never trades -- the flat baseline."""

    model = object()

    def predict(self, observation, deterministic: bool = True):
        return np.array([0.0, 0.0, 1.0], dtype=np.float32)


def _register(root: Path, config: dict | None = None, name: str = "ppo_v") -> None:
    """Register a config.yaml-only model (no model.zip / normalization)."""
    from kraken_trading_bot.rl.registry import register_model

    register_model(
        "ETH_USD",
        name,
        {"ticker": "ETH/USD", "action_space": "continuous", **(config or {})},
        root=root,
    )


def _write_config(tmp_path: Path, body: dict, name: str = "run.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def _covers_whole_frame(frame: pd.DataFrame, split: float) -> dict:
    """A ``data_window`` block covering every bar of ``frame``.

    ``until`` is EXCLUSIVE, so the upper bound sits one bar past the last
    timestamp or the final bar is dropped.
    """
    return {
        "since": frame.index[0].isoformat(),
        "until": (frame.index[-1] + pd.Timedelta(hours=1)).isoformat(),
        "eval_split": split,
    }


# ===========================================================================
# 1. evaluate_scope: the derivation itself
# ===========================================================================
def test_default_window_hands_back_one_object_so_no_oos_is_possible():
    """The mechanical fact the label rests on, asserted directly.

    If this ever stops holding, the default run is measuring something
    new and every IN-SAMPLE default below is wrong.
    """
    frame = _ramp_ohlcv(120)
    window = resolve_data_window({})  # the shipped default
    assert window.is_pinned is False
    assert training_frame(frame, window) is evaluation_frame(frame, window)


def test_default_window_is_labelled_in_sample():
    frame = _ramp_ohlcv(120)
    scope = evaluate_scope(frame, resolve_data_window({}))

    assert scope.label == IN_SAMPLE_LABEL
    assert scope.is_out_of_sample is False
    # Fail closed on the structured field too, not just the word.
    assert scope.window_is_pinned is False
    # And it admits the two halves are the same bars -- that is the whole
    # reason the label is what it is.
    assert scope.n_train_bars == scope.n_eval_bars == 120
    assert scope.n_overlapping_bars == 120
    assert "SAME" in scope.reason


def test_pinned_window_with_disjoint_halves_is_labelled_out_of_sample():
    """A store-backed pin that really holds bars out earns the label."""
    frame = _walk_ohlcv(240, seed=62)
    window = resolve_data_window({"data_window": _covers_whole_frame(frame, 0.7)})
    assert window.is_pinned is True and window.has_split is True

    scope = evaluate_scope(frame, window)

    assert scope.label == OUT_OF_SAMPLE_LABEL
    assert scope.is_out_of_sample is True
    train_bars = 168  # round(240 * 0.7)
    assert scope.n_train_bars == train_bars
    assert scope.n_eval_bars == 240 - train_bars
    # Disjointness MEASURED, not assumed -- the load-bearing clause.
    assert scope.n_overlapping_bars == 0
    # Both halves non-empty: a zero-bar half is a failure, not a split.
    assert scope.n_train_bars > 0 and scope.n_eval_bars > 0


def test_pinned_window_with_eval_split_one_is_inert_and_labelled_in_sample():
    """``has_split`` is False at 1.0, so the pin buys nothing."""
    frame = _walk_ohlcv(240, seed=63)
    window = resolve_data_window({"data_window": _covers_whole_frame(frame, 1.0)})
    assert window.is_pinned is True
    assert window.has_split is False

    scope = evaluate_scope(frame, window)

    assert scope.label == IN_SAMPLE_LABEL
    assert scope.is_out_of_sample is False
    assert "eval_split=1" in scope.reason


def test_pinned_window_whose_halves_share_a_bar_is_in_sample():
    """A one-bar overlap must disqualify the out-of-sample label.

    Reached with a clip too short to split (the ``len(clipped) < 2``
    branch), which hands the same bar to both halves.  ``has_split`` is
    True here, so a label derived from ``has_split`` alone would wrongly
    bless it.
    """
    frame = _ramp_ohlcv(60)
    window = resolve_data_window(
        {
            "data_window": {
                "since": frame.index[0].isoformat(),
                "until": frame.index[1].isoformat(),  # exactly one bar
                "eval_split": 0.7,
            }
        }
    )
    assert window.is_pinned is True and window.has_split is True

    scope = evaluate_scope(frame, window)

    assert scope.label == IN_SAMPLE_LABEL
    assert scope.is_out_of_sample is False
    assert scope.n_overlapping_bars == 1
    assert "share 1 bar" in scope.reason


def test_pinned_window_covering_nothing_raises_and_never_claims_oos():
    """THE negative case: a nominal pin with 0/0 bars is not out-of-sample.

    ``has_split`` is True for this window, so a label derived from
    ``has_split`` alone would read OUT-OF-SAMPLE off a run that replayed
    nothing at all.  The derivation refuses instead -- and it refuses by
    raising, so no ``BacktestResult`` exists to carry a wrong label.
    """
    frame = _walk_ohlcv(240, seed=64)
    window = resolve_data_window(
        {
            "data_window": {
                "since": "2020-01-01T00:00:00+00:00",
                "until": "2020-06-01T00:00:00+00:00",
                "eval_split": 0.7,
            }
        }
    )
    # The premise the whole negative case rests on.
    assert window.is_pinned is True
    assert window.has_split is True

    with pytest.raises(PinnedWindowUnavailableError):
        evaluate_scope(frame, window)


def test_pinned_window_on_an_empty_frame_raises_the_same_error():
    """Nothing was read at all: a different clause, still a named refusal."""
    frame = _walk_ohlcv(240, seed=65)
    empty = frame.iloc[0:0]
    window = resolve_data_window({"data_window": _covers_whole_frame(frame, 0.7)})

    with pytest.raises(PinnedWindowUnavailableError) as excinfo:
        evaluate_scope(empty, window)
    assert "returned no bars at all" in str(excinfo.value)


def test_scope_to_dict_carries_every_field_it_claims():
    frame = _walk_ohlcv(120, seed=66)
    payload = evaluate_scope(frame, resolve_data_window({})).to_dict()
    for key in (
        "evaluation_is_out_of_sample",
        "evaluation_scope_label",
        "evaluation_scope_reason",
        "data_window_is_pinned",
        "data_window_has_split",
        "n_train_bars",
        "n_eval_bars",
        "n_overlapping_bars",
    ):
        assert key in payload, key
    # It has to survive json.dumps for --json.
    json.dumps(payload)


# ===========================================================================
# 2. PinnedWindowUnavailableError: the message and the hierarchy
# ===========================================================================
def _outside_frame_error(frame: pd.DataFrame) -> PinnedWindowUnavailableError:
    window = resolve_data_window(
        {
            "data_window": {
                "since": "2020-01-01T00:00:00+00:00",
                "until": "2020-06-01T00:00:00+00:00",
                "eval_split": 0.7,
            }
        }
    )
    with pytest.raises(PinnedWindowUnavailableError) as excinfo:
        training_frame(frame, window)
    return excinfo.value


def test_pinned_without_a_store_names_the_fix_and_its_cost():
    """The message must carry the recipe, or the error is only a complaint."""
    frame = _walk_ohlcv(240, seed=67)
    message = str(_outside_frame_error(frame))

    # The recipe a reader can actually run...
    assert "just store-seed" in message
    assert "just store-plan" in message
    # ...with its price quoted, because 158s is the difference between
    # trying it and not.
    assert "158s" in message
    assert "13MB" in message
    # The live ceiling is WHY the pin cannot reach, so it belongs here.
    assert str(_LIVE_CEILING) in message
    assert "regardless of" in message
    # Both the ask and what actually arrived.
    assert "2020-01-01T00:00:00+00:00" in message
    assert "2020-06-01T00:00:00+00:00" in message
    assert "240 bar(s) that were actually read" in message


def test_pinned_error_subclasses_not_enough_data_error_and_value_error():
    """The hierarchy is the compatibility contract.

    Every existing caller and test catches ``NotEnoughDataError`` or
    ``ValueError``; a new class that broke either would silently turn a
    diagnosed data failure into a crash.
    """
    frame = _walk_ohlcv(240, seed=68)
    error = _outside_frame_error(frame)

    assert isinstance(error, NotEnoughDataError)
    assert isinstance(error, ValueError)


def test_existing_catchers_of_not_enough_data_still_catch_it():
    """The regression test for the hierarchy, in the shape callers use."""
    frame = _walk_ohlcv(240, seed=69)
    with pytest.raises(NotEnoughDataError):
        training_frame(
            frame,
            resolve_data_window(
                {
                    "data_window": {
                        "since": "2020-01-01T00:00:00+00:00",
                        "until": "2020-06-01T00:00:00+00:00",
                    }
                }
            ),
        )
    with pytest.raises(ValueError):
        evaluation_frame(
            frame,
            resolve_data_window(
                {
                    "data_window": {
                        "since": "2020-01-01T00:00:00+00:00",
                        "until": "2020-06-01T00:00:00+00:00",
                    }
                }
            ),
        )


def test_pinned_error_keeps_the_inherited_count_attributes():
    """Readers of ``.needed``/``.available`` stay unaffected."""
    frame = _walk_ohlcv(240, seed=70)
    error = _outside_frame_error(frame)
    assert error.needed == 1
    assert error.available == 240
    assert error.n_available == 240
    assert error.reason
    assert "outside the available bars" in error.reason
    assert error.window
    assert "eval_split=0.7" in error.window


def test_both_halves_raise_the_named_error_on_a_missed_pin():
    """Neither half may quietly return an empty slice."""
    frame = _walk_ohlcv(240, seed=71)
    window = resolve_data_window(
        {
            "data_window": {
                "since": "2020-01-01T00:00:00+00:00",
                "until": "2020-06-01T00:00:00+00:00",
            }
        }
    )
    with pytest.raises(PinnedWindowUnavailableError, match="training"):
        training_frame(frame, window)
    with pytest.raises(PinnedWindowUnavailableError, match="evaluation"):
        evaluation_frame(frame, window)


def test_it_does_not_fire_for_an_unpinned_window_even_when_empty():
    """The unpinned default keeps its own pre-existing path, unchanged."""
    frame = _walk_ohlcv(240, seed=72)
    window = resolve_data_window({})
    assert window.is_pinned is False
    # An empty frame must NOT be dressed up as a pin problem.
    assert len(training_frame(frame.iloc[0:0], window)) == 0
    assert len(evaluation_frame(frame.iloc[0:0], window)) == 0


# ===========================================================================
# 3. backtest_model: the label on a real result
# ===========================================================================
def test_backtest_default_is_in_sample_on_the_result(tmp_path):
    frame = _walk_ohlcv(240, seed=73)
    _register(tmp_path)
    # No data_window block at all: the shipped default.
    config = _write_config(tmp_path, {})

    result = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=frame,
        agent=_HoldEveryBar(),
        models_root=tmp_path,
        config_path=config,
    )

    assert result.evaluation_is_out_of_sample is False
    assert result.evaluation_scope_label == IN_SAMPLE_LABEL
    assert result.evaluation_scope_reason
    assert result.n_train_bars == result.n_eval_bars == 240


def test_backtest_default_is_in_sample_in_to_dict(tmp_path):
    """The dict is what ``--json`` serialises and ``export-data`` reads."""
    frame = _walk_ohlcv(240, seed=74)
    _register(tmp_path)
    config = _write_config(tmp_path, {})

    payload = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=frame,
        agent=_HoldEveryBar(),
        models_root=tmp_path,
        config_path=config,
    ).to_dict()

    assert payload["evaluation_is_out_of_sample"] is False
    assert payload["evaluation_scope_label"] == IN_SAMPLE_LABEL
    assert payload["evaluation_scope_reason"]
    assert payload["n_train_bars"] == 240
    assert payload["n_eval_bars"] == 240
    # And it survives the JSON round-trip --json actually performs.
    assert json.loads(json.dumps(payload))["evaluation_scope_label"] == (
        IN_SAMPLE_LABEL
    )


def test_backtest_default_result_replays_the_whole_frame(tmp_path):
    """Positive control: the guard is not rejecting or truncating anything."""
    frame = _walk_ohlcv(240, seed=75)
    _register(tmp_path)
    config = _write_config(tmp_path, {})

    result = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=frame,
        agent=_HoldEveryBar(),
        models_root=tmp_path,
        config_path=config,
    )

    assert result.n_bars == 240 - _BUILTIN_WARMUP
    assert result.n_steps == 240 - _BUILTIN_WARMUP
    # A clean run is genuinely measurable, not refused.
    assert np.isfinite(result.total_return)
    assert np.isfinite(result.buy_hold_return)


def test_backtest_pinned_and_splitting_is_out_of_sample(tmp_path):
    """The store-backed shape: a real split, honestly labelled."""
    frame = _walk_ohlcv(240, seed=76)
    _register(tmp_path)
    config = _write_config(
        tmp_path, {"data_window": _covers_whole_frame(frame, 0.7)}
    )

    result = backtest_model(
        "ETH_USD",
        "ppo_v",
        data=frame,
        agent=_HoldEveryBar(),
        models_root=tmp_path,
        config_path=config,
    )

    assert result.evaluation_is_out_of_sample is True
    assert result.evaluation_scope_label == OUT_OF_SAMPLE_LABEL
    assert result.n_train_bars == 168
    assert result.n_eval_bars == 72
    # Non-empty disjoint halves, and the replay is the held-out tail only.
    assert result.n_train_bars > 0 and result.n_eval_bars > 0
    assert result.n_bars == result.n_eval_bars - _BUILTIN_WARMUP


def test_backtest_pinned_window_that_misses_everything_raises(tmp_path):
    """``backtest_model`` must not return a result here at all.

    The failure mode being closed: a zero-bar run that reports numbers and
    could be read as out-of-sample.  Raising means there is no result to
    misread.
    """
    frame = _walk_ohlcv(240, seed=77)
    _register(tmp_path)
    config = _write_config(
        tmp_path,
        {
            "data_window": {
                "since": "2020-01-01T00:00:00+00:00",
                "until": "2020-06-01T00:00:00+00:00",
                "eval_split": 0.7,
            }
        },
    )
    with pytest.raises(PinnedWindowUnavailableError) as excinfo:
        backtest_model(
            "ETH_USD",
            "ppo_v",
            data=frame,
            agent=_HoldEveryBar(),
            models_root=tmp_path,
            config_path=config,
        )
    assert "just store-seed" in str(excinfo.value)
    # Catchable as the class existing callers already catch.
    with pytest.raises(NotEnoughDataError):
        backtest_model(
            "ETH_USD",
            "ppo_v",
            data=frame,
            agent=_HoldEveryBar(),
            models_root=tmp_path,
            config_path=config,
        )


def test_backtest_does_not_fire_the_pinned_error_for_an_ordinary_short_frame(
    tmp_path,
):
    """The guard must not reject everything.

    A pin that DOES overlap the data but yields too few bars for the
    indicator warm-up is the "widen ``--pages``" case.  It keeps raising
    the plain pre-existing ``ValueError``, and specifically NOT
    ``PinnedWindowUnavailableError`` -- telling someone to seed a store
    when their window was merely short sends them the wrong way.
    """
    frame = _walk_ohlcv(240, seed=78)
    _register(tmp_path)
    config = _write_config(
        tmp_path,
        {
            "data_window": {
                "since": frame.index[0].isoformat(),
                "until": frame.index[20].isoformat(),  # 20 bars: overlaps
                "eval_split": 0.1,
            }
        },
    )
    with pytest.raises(ValueError) as excinfo:
        backtest_model(
            "ETH_USD",
            "ppo_v",
            data=frame,
            agent=_HoldEveryBar(),
            models_root=tmp_path,
            config_path=config,
        )
    message = str(excinfo.value)
    assert "No tradable bar" in message
    assert "just store-seed" not in message
    assert not isinstance(excinfo.value, PinnedWindowUnavailableError)


def test_ordinary_not_enough_bars_keeps_raising_plain_not_enough_data(tmp_path):
    """The pre-existing bare bar-count refusal is untouched by the guard.

    ``tests/test_rl_training.py`` already pins ``match="Not enough"`` on
    this; restated here so the guard's blast radius is visible in one
    place next to the error it must NOT swallow.
    """
    frame = _walk_ohlcv(240, seed=79)
    _register(tmp_path)
    config = _write_config(tmp_path, {})
    # 3 bars: no pin, so the guard is silent and the warm-up refusal owns it.
    with pytest.raises(ValueError) as excinfo:
        backtest_model(
            "ETH_USD",
            "ppo_v",
            data=frame.iloc[:3],
            agent=_HoldEveryBar(),
            models_root=tmp_path,
            config_path=config,
        )
    assert "No tradable bar" in str(excinfo.value)
    assert not isinstance(excinfo.value, PinnedWindowUnavailableError)


# ===========================================================================
# 4. The label reaches --json and the human output
# ===========================================================================
def test_json_output_carries_the_label_for_an_unpinned_run(tmp_path, capsys):
    """End-to-end through the CLI: the real evaluator, the real JSON."""
    frame = _walk_ohlcv(240, seed=80)
    _register(tmp_path)
    config = _write_config(tmp_path, {})

    # Drive the genuine backtest_model (label really derived), then stub
    # only the two network-facing seams the CLI path cannot have here.
    with mock.patch(
        "kraken_trading_bot.rl.backtest.read_ohlc_dataframe",
        return_value=frame,
    ), mock.patch.object(
        __import__(
            "kraken_trading_bot.rl.backtest", fromlist=["RLAgent"]
        ).RLAgent,
        "load",
        return_value=_HoldEveryBar(),
    ):
        rc = main(
            [
                "backtest",
                "--ticker",
                "ETH_USD",
                "--model",
                "ppo_v",
                "--models-root",
                str(tmp_path),
                "--config",
                str(config),
                "--json",
            ]
        )

    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)  # raises if anything else was printed
    assert payload["evaluation_is_out_of_sample"] is False
    assert payload["evaluation_scope_label"] == IN_SAMPLE_LABEL
    assert payload["evaluation_scope_reason"]
    # The human table is suppressed under --json, as documented.
    assert "Total return:" not in captured.out


def test_json_output_carries_the_label_for_a_pinned_run(tmp_path, capsys):
    frame = _walk_ohlcv(240, seed=81)
    _register(tmp_path)
    config = _write_config(
        tmp_path, {"data_window": _covers_whole_frame(frame, 0.7)}
    )

    with mock.patch(
        "kraken_trading_bot.rl.backtest.read_ohlc_dataframe",
        return_value=frame,
    ), mock.patch.object(
        __import__(
            "kraken_trading_bot.rl.backtest", fromlist=["RLAgent"]
        ).RLAgent,
        "load",
        return_value=_HoldEveryBar(),
    ):
        rc = main(
            [
                "backtest",
                "--ticker",
                "ETH_USD",
                "--model",
                "ppo_v",
                "--models-root",
                str(tmp_path),
                "--config",
                str(config),
                "--json",
            ]
        )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["evaluation_is_out_of_sample"] is True
    assert payload["evaluation_scope_label"] == OUT_OF_SAMPLE_LABEL
    assert payload["n_train_bars"] == 168
    assert payload["n_eval_bars"] == 72


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP in c74e806: the label reaches --json and the LOG stream "
        "but NOT the CLI's human stdout table. cli.py's cmd_backtest print "
        "block (lines ~593-614) prints Total return / Buy & hold / Bars "
        "replayed / Fee / Action space and never reads "
        "d['evaluation_scope_label'], so a reader of the human table sees "
        "an unlabelled return -- the exact failure this commit exists to "
        "close. cli.py is outside this pass's file ownership, so the gap "
        "is reported rather than fixed. strict=True so closing it turns "
        "this into a suite FAILURE, which is the intended signal."
    ),
)
def test_human_output_states_the_label_and_its_reason(tmp_path, capsys):
    """The label must be *printed* to stdout, with the reason."""
    frame = _walk_ohlcv(240, seed=82)
    _register(tmp_path)
    config = _write_config(tmp_path, {})

    with mock.patch(
        "kraken_trading_bot.rl.backtest.read_ohlc_dataframe",
        return_value=frame,
    ), mock.patch.object(
        __import__(
            "kraken_trading_bot.rl.backtest", fromlist=["RLAgent"]
        ).RLAgent,
        "load",
        return_value=_HoldEveryBar(),
    ):
        rc = main(
            [
                "backtest",
                "--ticker",
                "ETH_USD",
                "--model",
                "ppo_v",
                "--models-root",
                str(tmp_path),
                "--config",
                str(config),
            ]
        )

    captured = capsys.readouterr()
    assert rc == 0
    # The pre-existing lines must survive for existing readers...
    assert "Total return:" in captured.out
    assert "Bars replayed:" in captured.out
    # ...and the verdict must be printed rather than buried.
    assert IN_SAMPLE_LABEL in captured.out
    assert "SAME" in captured.out


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Same KNOWN GAP as "
        "test_human_output_states_the_label_and_its_reason: cmd_backtest "
        "never prints evaluation_scope_label, so even a genuine split is "
        "unlabelled in the human table."
    ),
)
def test_human_output_labels_a_pinned_run_out_of_sample(tmp_path, capsys):
    frame = _walk_ohlcv(240, seed=83)
    _register(tmp_path)
    config = _write_config(
        tmp_path, {"data_window": _covers_whole_frame(frame, 0.7)}
    )

    with mock.patch(
        "kraken_trading_bot.rl.backtest.read_ohlc_dataframe",
        return_value=frame,
    ), mock.patch.object(
        __import__(
            "kraken_trading_bot.rl.backtest", fromlist=["RLAgent"]
        ).RLAgent,
        "load",
        return_value=_HoldEveryBar(),
    ):
        rc = main(
            [
                "backtest",
                "--ticker",
                "ETH_USD",
                "--model",
                "ppo_v",
                "--models-root",
                str(tmp_path),
                "--config",
                str(config),
            ]
        )

    captured = capsys.readouterr()
    assert rc == 0
    assert OUT_OF_SAMPLE_LABEL in captured.out
    assert IN_SAMPLE_LABEL not in captured.out


def test_label_reaches_the_user_through_the_log_stream(tmp_path, caplog):
    """The mechanism c74e806 actually shipped, pinned.

    The default path warns at WARNING level, because an unlabelled
    IN-SAMPLE return is the failure this commit exists to close and the
    default is the case being misread.  The per-run summary line then
    carries the label in brackets, so the log stream alone is enough for
    a reader who never looks at the stdout table (see the xfail above).
    """
    import logging

    frame = _walk_ohlcv(240, seed=84)
    _register(tmp_path)
    config = _write_config(tmp_path, {})

    with caplog.at_level(logging.INFO, logger="kraken_trading_bot.rl.backtest"):
        backtest_model(
            "ETH_USD",
            "ppo_v",
            data=frame,
            agent=_HoldEveryBar(),
            models_root=tmp_path,
            config_path=config,
        )

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the default IN-SAMPLE path must warn"
    assert any(
        IN_SAMPLE_LABEL in r.getMessage()
        and "no out-of-sample claim is supported" in r.getMessage()
        for r in warnings
    )
    # And the reason travels with the label.
    assert any("SAME" in r.getMessage() for r in caplog.records)
    # The summary line carries the label too.
    assert any(
        f"[{IN_SAMPLE_LABEL}]" in r.getMessage() for r in caplog.records
    )


def test_pinned_run_logs_out_of_sample_without_the_warning(tmp_path, caplog):
    """The positive control on the log stream: a real split does not warn."""
    import logging

    frame = _walk_ohlcv(240, seed=86)
    _register(tmp_path)
    config = _write_config(
        tmp_path, {"data_window": _covers_whole_frame(frame, 0.7)}
    )

    with caplog.at_level(logging.INFO, logger="kraken_trading_bot.rl.backtest"):
        backtest_model(
            "ETH_USD",
            "ppo_v",
            data=frame,
            agent=_HoldEveryBar(),
            models_root=tmp_path,
            config_path=config,
        )

    # OUT-OF-SAMPLE runs at INFO, so no "no out-of-sample claim" warning.
    assert not any(
        "no out-of-sample claim is supported" in r.getMessage()
        for r in caplog.records
    )
    assert any(
        f"[{OUT_OF_SAMPLE_LABEL}]" in r.getMessage() for r in caplog.records
    )


# ===========================================================================
# 5. The second error site stays a plain ValueError (decision pinned)
# ===========================================================================
def test_no_tradable_bar_site_is_still_a_plain_value_error(tmp_path):
    """``backtest.py``'s warm-up refusal is deliberately NOT reclassified.

    Justification, from the consumers:

    * ``cli.py``'s ``cmd_backtest`` catches bare ``Exception`` and prints
      only ``str(e)``, so no consumer can tell the two sites apart.
    * ``tools/model_matrix.py`` classifies cells by *message text*, and
      keeps a compensating ``"ValueError" in text and "tradable bar" in
      text`` clause that exists precisely because this site is a plain
      ``ValueError``.  Its message still opens "No tradable bar", so the
      ``no_tradable_bar`` verdict survives either way.
    * The remedies differ: "seed the store" for a pin that missed
      everything, "raise ``--pages``" for a warm-up the window cannot
      fill.  ``PinnedWindowUnavailableError``'s own docstring promises it
      is "deliberately *not* fired for an ordinary short frame".  Merging
      them would contradict that contract.

    This test pins the decision so a future change has to make it
    deliberately.
    """
    frame = _walk_ohlcv(240, seed=85)
    _register(tmp_path)
    config = _write_config(
        tmp_path,
        {
            "data_window": {
                "since": frame.index[0].isoformat(),
                "until": frame.index[20].isoformat(),
                "eval_split": 0.1,
            }
        },
    )
    with pytest.raises(ValueError) as excinfo:
        backtest_model(
            "ETH_USD",
            "ppo_v",
            data=frame,
            agent=_HoldEveryBar(),
            models_root=tmp_path,
            config_path=config,
        )
    error = excinfo.value
    # A ValueError, and specifically NOT one of the data-error classes.
    assert type(error) is ValueError
    assert not isinstance(error, PinnedWindowUnavailableError)
    assert not isinstance(error, NotEnoughDataError)
    # It still names the knobs a reader can turn.
    assert "eval_split" in str(error)
    assert "--pages" in str(error)