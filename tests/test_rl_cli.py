"""Tests for the RL CLI subcommands (train / backtest / models).

Covers argument parsing, dispatch into the training/backtesting/registry
functions, and the printed output of each command.  All tests are
hermetic: the RL pipeline functions are patched with mocks (or real
in-memory :class:`BacktestResult` / :class:`ModelRecord` values), so no
network traffic, training runs, credentials, or writes outside pytest's
``tmp_path`` happen here.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest import mock

import pandas as pd
import pytest

from kraken_trading_bot.cli import _build_parser, main
from kraken_trading_bot.rl.backtest import BacktestResult
from kraken_trading_bot.rl.registry import ModelRecord, register_model


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _sample_config() -> dict:
    """A config dict shaped like configs/default.yaml (used for records)."""
    return {
        "ticker": "ETH/USD",
        "model_name": "ppo_eth_01",
        "action_space": "continuous",
        "reward": {"mode": "pnl", "pnl": 1.0},
        "feature_windows": [1, 4, 24],
        "feature_groups": ["price", "technical", "volume", "microstructure"],
    }


def _record(
    ticker_id: str = "ETH_USD",
    model_name: str = "ppo_eth_01",
    trained: bool = True,
    tmp_path=None,
) -> ModelRecord:
    """A synthetic ModelRecord; optional paths live under tmp_path."""
    config = {**_sample_config(), "ticker": ticker_id.replace("_", "/")}
    model_path = None
    normalization_path = None
    if trained:
        assert tmp_path is not None
        model_path = tmp_path / "model.zip"
        normalization_path = tmp_path / "normalization.npz"
        model_path.write_bytes(b"pk")
        normalization_path.write_bytes(b"npz")
    return ModelRecord(
        ticker_id=ticker_id,
        model_name=model_name,
        config=config,
        created=datetime(2025, 1, 2, 3, 4, 5).timestamp(),
        config_path=tmp_path / "config.yaml" if tmp_path is not None else None,
        model_path=model_path,
        normalization_path=normalization_path,
        root=tmp_path,
    )


def _grouped(records: list[ModelRecord]) -> dict[str, list[ModelRecord]]:
    """Group records by ticker_id the way list_models does."""
    grouped: dict[str, list[ModelRecord]] = {}
    for record in records:
        grouped.setdefault(record.ticker_id, []).append(record)
    return grouped


# ---------------------------------------------------------------------------
# parser smoke
# ---------------------------------------------------------------------------
def test_cli_train_parser_flags():
    args = _build_parser().parse_args(
        [
            "train",
            "--ticker", "ETH_USD",
            "--model", "ppo_eth_01",
            "--config", "configs/default.yaml",
            "--pages", "8",
            "--episode-bars", "500",
            "--timesteps", "20000",
            "--seed", "7",
            "--interval-minutes", "30",
            "--action-space", "discrete",
            "--initial-balance", "5000.5",
            "--fee-rate", "0.0026",
            "--slippage", "0.0005",
        ]
    )
    assert args.command == "train"
    assert args.ticker == "ETH_USD"
    assert args.model == "ppo_eth_01"
    assert args.config == "configs/default.yaml"
    assert args.pages == 8
    assert args.episode_bars == 500
    assert args.timesteps == 20000
    assert args.seed == 7
    assert args.interval_minutes == 30
    assert args.action_space == "discrete"
    assert args.initial_balance == pytest.approx(5000.5)
    assert args.fee_rate == pytest.approx(0.0026)
    assert args.slippage == pytest.approx(0.0005)


def test_cli_train_parser_defaults():
    args = _build_parser().parse_args(["train", "--ticker", "ETH_USD", "--model", "m"])
    assert args.pages == 6
    assert args.episode_bars is None
    assert args.timesteps == 10_000
    assert args.seed == 42
    assert args.config is None
    assert args.interval_minutes is None
    assert args.action_space is None
    assert args.initial_balance is None
    assert args.fee_rate is None
    assert args.slippage is None
    assert args.models_root == "models"


def test_cli_backtest_parser_flags():
    args = _build_parser().parse_args(
        ["backtest", "--ticker", "ETH_USD", "--model", "ppo_eth_01", "--pages", "3", "--seed", "9"]
    )
    assert args.command == "backtest"
    assert args.ticker == "ETH_USD"
    assert args.model == "ppo_eth_01"
    assert args.pages == 3
    assert args.seed == 9
    assert args.models_root == "models"


def test_cli_models_parser_flags():
    args = _build_parser().parse_args(["models", "--ticker", "ETH_USD", "--json"])
    assert args.command == "models"
    assert args.ticker == "ETH_USD"
    assert args.json is True
    assert args.models_root == "models"


# ---------------------------------------------------------------------------
# train dispatch
# ---------------------------------------------------------------------------
def test_cli_train_dispatch_maps_flags_to_train_ticker():
    with mock.patch("kraken_trading_bot.rl.train.train_ticker") as train:
        train.return_value = _record(trained=False)
        rc = main(
            [
                "train",
                "--ticker", "ETH_USD",
                "--model", "ppo_eth_01",
                "--pages", "8",
                "--timesteps", "20000",
                "--seed", "7",
                "--models-root", "tmp_models",
            ]
        )
    assert rc == 0
    train.assert_called_once()
    call = train.call_args
    # Positional ticker + model.
    assert call.args == ("ETH_USD", "ppo_eth_01")
    # Named pipeline args are passed through.
    kwargs = call.kwargs
    assert kwargs["config_path"] is None
    assert kwargs["pages"] == 8
    assert kwargs["episode_bars"] is None
    assert kwargs["total_timesteps"] == 20000
    assert kwargs["seed"] == 7
    assert kwargs["models_root"] == "tmp_models"
    # No override flags given -> no config overrides are forwarded.
    assert "ohlcv_interval_minutes" not in kwargs
    assert "action_space" not in kwargs
    assert "initial_balance" not in kwargs
    assert "fee_rate" not in kwargs
    assert "slippage" not in kwargs


def test_cli_train_dispatch_forwards_overrides():
    with mock.patch("kraken_trading_bot.rl.train.train_ticker") as train:
        train.return_value = _record(trained=False)
        rc = main(
            [
                "train",
                "--ticker", "ETH_USD",
                "--model", "ppo_eth_01",
                "--interval-minutes", "30",
                "--action-space", "discrete",
                "--initial-balance", "5000.5",
                "--fee-rate", "0.0026",
                "--slippage", "0.0005",
            ]
        )
    assert rc == 0
    kwargs = train.call_args.kwargs
    assert kwargs["ohlcv_interval_minutes"] == 30
    assert kwargs["action_space"] == "discrete"
    assert kwargs["initial_balance"] == pytest.approx(5000.5)
    assert kwargs["fee_rate"] == pytest.approx(0.0026)
    assert kwargs["slippage"] == pytest.approx(0.0005)


def test_cli_train_dispatch_prints_record_output(tmp_path, capsys):
    with mock.patch("kraken_trading_bot.rl.train.train_ticker") as train:
        train.return_value = _record(tmp_path=tmp_path)
        rc = main(
            [
                "train",
                "--ticker", "ETH_USD",
                "--model", "ppo_eth_01",
                "--models-root", str(tmp_path),
            ]
        )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Trained model ETH_USD/ppo_eth_01" in out
    assert f"{tmp_path}/model.zip" in out
    assert "normalization.npz" in out
    assert "config.yaml" in out
    assert "action_space=continuous" in out
    assert "reward=pnl" in out
    assert "Trained:         True" in out


def test_cli_train_dispatch_error_returns_1():
    with mock.patch("kraken_trading_bot.rl.train.train_ticker") as train:
        train.side_effect = RuntimeError("boom")
        rc = main(["train", "--ticker", "ETH_USD", "--model", "ppo_eth_01"])
    assert rc == 1


# ---------------------------------------------------------------------------
# backtest dispatch
# ---------------------------------------------------------------------------
def test_cli_backtest_dispatch_prints_summary(capsys):
    result = BacktestResult(
        ticker_id="ETH_USD",
        model_name="ppo_eth_01",
        total_return=0.1234,
        sharpe=1.2345,
        max_drawdown=0.0456,
        num_trades=42,
        win_rate=0.556,
        equity_curve=[10_000.0, 11_123.0, 11_234.0],
        n_steps=2,
        final_equity=11_234.0,
        seed=42,
    )
    with mock.patch("kraken_trading_bot.rl.backtest.backtest_model") as bt:
        bt.return_value = result
        rc = main(
            [
                "backtest",
                "--ticker", "ETH_USD",
                "--model", "ppo_eth_01",
                "--pages", "3",
                "--seed", "9",
            ]
        )
    out = capsys.readouterr().out
    assert rc == 0
    bt.assert_called_once()
    call = bt.call_args
    assert call.args == ("ETH_USD", "ppo_eth_01")
    assert call.kwargs["pages"] == 3
    assert call.kwargs["seed"] == 9
    assert call.kwargs["models_root"] == "models"
    # Readable summary contains every headline metric.
    assert "Total return:   12.34%" in out
    assert "Sharpe:         1.234" in out
    assert "Max drawdown:   4.56%" in out
    assert "Trades:         42" in out
    assert "Win rate:       55.60%" in out
    assert "Final equity:   11,234.00" in out


def test_cli_backtest_dispatch_error_returns_1():
    with mock.patch("kraken_trading_bot.rl.backtest.backtest_model") as bt:
        bt.side_effect = FileNotFoundError("no model.zip")
        rc = main(["backtest", "--ticker", "ETH_USD", "--model", "missing"])
    assert rc == 1


# ---------------------------------------------------------------------------
# models dispatch
# ---------------------------------------------------------------------------
def test_cli_models_dispatch_plain_table(tmp_path, capsys):
    records = [
        _record("ETH_USD", "ppo_eth_01", trained=True, tmp_path=tmp_path),
        _record("ETH_USD", "ppo_eth_02", trained=False, tmp_path=tmp_path),
        _record("XBT_USD", "ppo_xbt_01", trained=True, tmp_path=tmp_path),
    ]
    with mock.patch("kraken_trading_bot.rl.registry.list_models") as lm:
        lm.return_value = _grouped(records)
        rc = main(["models", "--models-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    # Header + one row per record.
    assert "Ticker" in out and "Model" in out and "Created" in out
    assert "Trained" in out and "Action" in out and "Reward" in out
    assert "ETH_USD" in out and "XBT_USD" in out
    assert "ppo_eth_01" in out and "ppo_eth_02" in out and "ppo_xbt_01" in out
    # Trained flag column reflects is_trained().
    row_eth02 = out.split("ppo_eth_02")[1].split("\n")[0]
    assert "no" in row_eth02


def test_cli_models_dispatch_json(tmp_path, capsys):
    records = [
        _record("ETH_USD", "ppo_eth_01", trained=True, tmp_path=tmp_path),
        _record("XBT_USD", "ppo_xbt_01", trained=False, tmp_path=tmp_path),
    ]
    with mock.patch("kraken_trading_bot.rl.registry.list_models") as lm:
        lm.return_value = _grouped(records)
        rc = main(["models", "--json", "--models-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert set(payload) == {"ETH_USD", "XBT_USD"}
    eth = payload["ETH_USD"][0]
    assert eth["ticker_id"] == "ETH_USD"
    assert eth["model_name"] == "ppo_eth_01"
    assert eth["is_trained"] is True
    assert eth["created"] == records[0].created
    assert eth["config_summary"]["action_space"] == "continuous"
    assert eth["config_summary"]["reward_mode"] == "pnl"
    assert eth["model_path"] is not None and eth["model_path"].endswith("model.zip")
    xbt = payload["XBT_USD"][0]
    assert xbt["is_trained"] is False
    assert xbt["model_path"] is None
    assert xbt["normalization_path"] is None


def test_cli_models_dispatch_ticker_filter(tmp_path, capsys):
    records = [
        _record("ETH_USD", "ppo_eth_01", trained=True, tmp_path=tmp_path),
        _record("XBT_USD", "ppo_xbt_01", trained=True, tmp_path=tmp_path),
    ]
    with mock.patch("kraken_trading_bot.rl.registry.list_models") as lm:
        lm.return_value = _grouped(records)
        rc = main(["models", "--ticker", "ETH_USD", "--json", "--models-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert set(payload) == {"ETH_USD"}
    assert payload["ETH_USD"][0]["model_name"] == "ppo_eth_01"


def test_cli_models_dispatch_empty(tmp_path, capsys):
    with mock.patch("kraken_trading_bot.rl.registry.list_models") as lm:
        lm.return_value = {}
        rc = main(["models", "--json", "--models-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No models registered" in out


# ---------------------------------------------------------------------------
# export-data
# ---------------------------------------------------------------------------
def _export_frame() -> pd.DataFrame:
    """A two-row staged frame shaped like build_export_frame's output."""
    frame = pd.DataFrame(
        {
            "timestamp": ["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"],
            "time": [0, 3600],
            "close": [1.0, 1.5],
            "return_1": [0.0, 0.5],
            "warmup": [True, False],
        }
    )
    frame.attrs["stages"] = {
        "timestamp": ["timestamp"],
        "ohlcv": ["time", "close"],
        "signals": [],
        "features": ["return_1"],
        "normalized": [],
        "warmup": ["warmup"],
    }
    return frame


def test_cli_export_data_parser_flags():
    args = _build_parser().parse_args(
        [
            "export-data",
            "--ticker", "SOL_USD",
            "--output", "out/data.csv",
            "--config", "configs/default.yaml",
            "--pages", "3",
            "--interval-minutes", "30",
            "--episode-bars", "500",
            "--normalized",
        ]
    )
    assert args.command == "export-data"
    assert args.ticker == "SOL_USD"
    assert args.output == "out/data.csv"
    assert args.config == "configs/default.yaml"
    assert args.pages == 3
    assert args.interval_minutes == 30
    assert args.episode_bars == 500
    assert args.normalized is True


def test_cli_export_data_parser_defaults():
    args = _build_parser().parse_args(["export-data", "--ticker", "SOL_USD"])
    assert args.output is None
    assert args.config is None
    assert args.pages == 6
    assert args.interval_minutes is None
    assert args.episode_bars is None
    assert args.normalized is False


def test_cli_export_data_dispatch_writes_csv(tmp_path, capsys):
    destination = tmp_path / "out" / "SOL_USD.csv"
    with mock.patch(
        "kraken_trading_bot.rl.export.build_export_frame",
        return_value=_export_frame(),
    ):
        rc = main(
            [
                "export-data",
                "--ticker", "SOL_USD",
                "--output", str(destination),
                "--pages", "2",
            ]
        )
    out = capsys.readouterr().out
    assert rc == 0
    assert destination.exists()
    assert "Exported 2 bars x 5 columns" in out
    assert "2026-01-01T00:00:00Z -> 2026-01-01T01:00:00Z" in out
    # Stage counts come from frame.attrs, not a re-derivation.
    assert "1 timestamp, 2 OHLCV, 0 signal, 1 observation (raw)" in out


def test_cli_export_data_dispatch_forwards_overrides():
    with mock.patch(
        "kraken_trading_bot.rl.export.build_export_frame",
        return_value=_export_frame(),
    ) as build:
        rc = main(
            [
                "export-data",
                "--ticker", "SOL_USD",
                "--config", "configs/default.yaml",
                "--pages", "4",
                "--interval-minutes", "30",
                "--episode-bars", "100",
                "--normalized",
            ]
        )
    assert rc == 0
    kwargs = build.call_args.kwargs
    assert build.call_args.args == ("SOL_USD",)
    assert kwargs["config_path"] == "configs/default.yaml"
    assert kwargs["pages"] == 4
    assert kwargs["episode_bars"] == 100
    assert kwargs["include_normalized"] is True
    assert kwargs["ohlcv_interval_minutes"] == 30


def test_cli_export_data_dispatch_error_returns_1(capsys):
    with mock.patch(
        "kraken_trading_bot.rl.export.build_export_frame",
        side_effect=RuntimeError("boom"),
    ):
        rc = main(["export-data", "--ticker", "SOL_USD", "--output", "x.csv"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Error exporting SOL_USD: boom" in captured.err

# ---------------------------------------------------------------------------
# Gap-1 widening: the CLI surfaces the observation width
# ---------------------------------------------------------------------------
def test_models_table_shows_a_width_column(tmp_path, capsys):
    """`models` prints the width, or `?` for a pre-provenance artifact.

    The whole point of recording `n_features` is that a human (or a
    script) can tell a 49-wide artifact from a 55-wide one; the table is
    where that shows up.
    """
    register_model(
        "ETH_USD",
        "ppo_wide",
        {
            "ticker": "ETH/USD",
            "action_space": "continuous",
            "reward": {"mode": "pnl"},
            "feature_windows": [1, 4, 24],
            "n_features": 55,
        },
        root=tmp_path,
    )
    register_model(
        "SOL_USD",
        "ppo_legacy",
        {"ticker": "SOL/USD", "action_space": "discrete", "reward": {"mode": "pnl"}},
        root=tmp_path,
    )

    assert main(["models", "--models-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    header = [line for line in out.splitlines() if line.startswith("Ticker")][0]
    assert "Width" in header
    rows = {
        line.split()[0]: line for line in out.splitlines() if line.startswith(("ETH", "SOL"))
    }
    assert rows["ETH_USD"].split()[-1] == "55"
    # No n_features recorded -> cannot be shown to match anything.
    assert rows["SOL_USD"].split()[-1] == "?"


def test_models_json_carries_n_features(tmp_path, capsys):
    register_model(
        "ETH_USD",
        "ppo_wide",
        {"ticker": "ETH/USD", "action_space": "continuous", "n_features": 55},
        root=tmp_path,
    )
    assert main(["models", "--models-root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ETH_USD"][0]["config_summary"]["n_features"] == 55
