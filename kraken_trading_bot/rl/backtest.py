"""Backtesting evaluation for the RL pipeline.

Replays a trained PPO agent over OHLCV data in a
:class:`TradingEnvironment` and produces standard performance metrics:
total return, per-bar Sharpe, max drawdown, trade count and win rate,
plus the full equity curve.

Reuses the environment's fee/slippage/reward configuration: when the
model directory contains ``normalization.npz`` the pipeline path is
reconstructed exactly as at training time; otherwise the environment's
builtin fallback features are used.

**A return is not a verdict.**  A ``+4%`` on a rising asset may be
nothing at all, and a frictionless backtest (``fee_rate``/``slippage``
both ``0.0`` in ``configs/default.yaml``) makes every number look better
than it deserves.  Three things here exist to make the headline number
mean something, and all three are measured, not assumed:

``fee_rate`` / ``slippage``
    Echoed on the result, so a report cannot silently imply a frictionless
    result.  A backtest with no ``--config`` and a model trained without
    friction still runs frictionless — but now says so in its own output.

``buy_hold_return`` / ``excess_return`` / ``buy_hold_max_drawdown``
    Buy-and-hold over **the same bars actually replayed** (same first and
    last close, same initial balance), so ``excess_return`` is the
    strategy's return minus the reference point.  The realized range is
    used rather than the requested one: an episode that warms up on the
    first bars must not be compared against a benchmark measured from the
    frame's very first close.

``n_bars``
    Bars actually replayed.  This is the *magnitude* guard.  A run can
    complete with no error, no warning and a passing width guard while
    replaying a single bar of 721 — a real defect this repository found
    and fixed (``b45a7a4``), where a sparsely-covered exogenous funding
    file collapsed the tradable window.  A small ``n_bars`` says the
    number above it is noise.

Two pre-flight guards run before any replay: :func:`~kraken_trading_bot.
rl.features.check_feature_width` (observation width, unchanged and not
weakened) and :class:`ActionSpaceMismatchError` (the action space the
model was trained with, against the one this run would use).

Data-window pinning (``data_window`` in the config, see
:mod:`kraken_trading_bot.rl.data_window`) lets a caller pin the window
and take the out-of-sample tail, so a run stops comparing data against
data.  With the window unpinned — the default — nothing here changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .agent import RLAgent
from .data import add_derived_ohlcv_features, read_ohlc_dataframe
from .data_window import DataWindow, evaluation_frame, resolve_data_window
from .environment import TradingEnvironment
from .features import FeaturePipeline, check_feature_width, normalize_ticker_id
from .registry import ModelRecord, scan_model
from .train import load_train_config

_LOGGER = logging.getLogger(__name__)

# Environment settings this call site resolves explicitly, with the
# hard-coded fallback used when no source names them.  Resolved by
# :func:`_resolve_env_setting` and then removed from the ``**env_kwargs``
# pass-through, so they cannot arrive twice.
_ENV_SETTINGS = (
    ("action_space", "continuous"),
    ("initial_balance", 10_000.0),
    ("fee_rate", 0.0),
    ("slippage", 0.0),
)


class ActionSpaceMismatchError(ValueError):
    """The model's trained action space differs from the one being replayed.

    A ``continuous`` policy fed a ``Discrete(3)`` action space (or the
    reverse) does not fail loudly at load time — stable-baselines3 will
    happily bind the policy to the new space and then emit actions the
    environment cannot interpret, or actions it can interpret as
    something the policy never learned to emit.  The result is a return
    number with no meaning, which is worse than an error.

    The model's ``action_space`` is read from its own ``config.yaml``
    (``None`` for a pre-provenance artifact, which is not treated as a
    mismatch — an unrecorded value is unproven, not wrong).

    Attributes:
        trained_as: Action space the model recorded, or ``None``.
        requested: Action space this run would use.
        context: Human-readable description of the check.
    """

    def __init__(
        self, trained_as: str | None, requested: str, *, context: str
    ) -> None:
        self.trained_as = trained_as
        self.requested = requested
        super().__init__(
            f"Action-space mismatch ({context}): the model was trained as "
            f"{trained_as!r} but this backtest would replay it as "
            f"{requested!r}. A policy bound to the wrong action space "
            f"emits actions it never learned to emit, so its return is "
            f"meaningless. Replay with the action space the model was "
            f"trained with, or retrain for {requested!r}."
        )


@dataclass
class BacktestResult:
    """Performance summary of one backtest run.

    Attributes:
        ticker_id: Normalized ticker backtested (e.g. ``"ETH_USD"``).
        model_name: Model name backtested.
        total_return: Final equity / initial balance - 1.
        sharpe: Per-bar Sharpe ratio (mean / std of per-bar equity
            returns scaled by sqrt(n)); 0 when returns are flat.
        max_drawdown: Largest peak-to-trough equity loss as a positive
            fraction of the running peak.
        num_trades: Number of executed orders.
        win_rate: Fraction of fully-closed round trips with positive
            realized profit; 0.0 when no position was fully closed.
        equity_curve: Per-bar equity values, starting at the initial
            balance (length n_steps + 1).
        n_steps: Number of environment steps executed.
        final_equity: Equity after the final step.
        seed: Seed used for the deterministic replay.
        buy_hold_return: Buy-and-hold return over the SAME bars replayed
            (same first and last close, same initial balance).  0.0 when
            fewer than two bars were replayed.
        excess_return: ``total_return - buy_hold_return`` — the headline
            number: what the strategy returned *beyond* doing nothing but
            holding the asset.
        buy_hold_max_drawdown: Peak-to-trough drawdown of that same
            buy-and-hold equity path, as a positive fraction of its
            running peak (the same convention as ``max_drawdown``).
        n_bars: Bars **actually replayed**.  The magnitude guard: a run
            can finish with no error while replaying a handful of bars of
            a much larger frame, so this is what tells a reader whether
            ``total_return`` describes a period or an instant.
        fee_rate: Fractional fee per executed order that was actually
            applied; echoed so a frictionless result cannot be mistaken
            for a realistic one.
        slippage: Fractional adverse fill move actually applied.
        action_space: Action space the replay actually used.
    """

    ticker_id: str
    model_name: str
    total_return: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    num_trades: int = 0
    win_rate: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    n_steps: int = 0
    final_equity: float = 0.0
    seed: int | None = None
    buy_hold_return: float = 0.0
    excess_return: float = 0.0
    buy_hold_max_drawdown: float = 0.0
    n_bars: int = 0
    fee_rate: float = 0.0
    slippage: float = 0.0
    action_space: str = "continuous"

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable dict view of the result."""
        return {
            "ticker_id": self.ticker_id,
            "model_name": self.model_name,
            "total_return": self.total_return,
            "sharpe": self.sharpe,
            "max_drawdown": self.max_drawdown,
            "num_trades": self.num_trades,
            "win_rate": self.win_rate,
            "equity_curve": list(self.equity_curve),
            "n_steps": self.n_steps,
            "final_equity": self.final_equity,
            "seed": self.seed,
            "buy_hold_return": self.buy_hold_return,
            "excess_return": self.excess_return,
            "buy_hold_max_drawdown": self.buy_hold_max_drawdown,
            "n_bars": self.n_bars,
            "fee_rate": self.fee_rate,
            "slippage": self.slippage,
            "action_space": self.action_space,
        }


def _load_run_config(config_path: str | Path | None) -> dict[str, Any]:
    """Load the backtest's own run config (``--config``), if any.

    Absent ``config_path`` loads *nothing* on purpose: a backtest without
    ``--config`` must keep reading its parameters from the model's own
    training config, never from ``configs/default.yaml``, so it behaves
    exactly as it did before ``--config`` existed.

    A path that does not exist raises instead of degrading to the model's
    config.  Silently running frictionless because of a typo in the config
    path is precisely the failure the ``fee_rate``/``slippage`` echo on
    the result exists to make visible — better to refuse the run.

    Raises:
        FileNotFoundError: If ``config_path`` is given but absent.
    """
    if config_path is None:
        return {}
    path = Path(config_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"Backtest config not found: {path}. Pass --config with an "
            f"existing YAML file (e.g. configs/default.yaml), or omit it "
            f"to replay with the model's own training config."
        )
    return load_train_config(path)


def _resolve_env_setting(
    name: str,
    env_kwargs: dict[str, Any],
    run_cfg: dict[str, Any],
    record_cfg: dict[str, Any],
    default: Any,
) -> Any:
    """Resolve one environment setting from its four sources, in order.

    Precedence, strongest first:

    1. ``env_kwargs`` — an explicit programmatic argument from the caller.
    2. the run config (``--config``) — what this run was asked to apply.
    3. the model's own ``config.yaml`` — what it was trained with.
    4. ``default`` — the pre-existing hard-coded fallback.

    A source that *has* the key set to ``None`` is skipped rather than
    honoured, because YAML ``key: null`` is how a config says "unset",
    not "set to nothing".
    """
    if name in env_kwargs:
        return env_kwargs[name]
    if run_cfg.get(name) is not None:
        return run_cfg[name]
    if record_cfg.get(name) is not None:
        return record_cfg[name]
    return default


def _check_action_space(
    record_cfg: dict[str, Any], action_space: str, context: str
) -> None:
    """Fail loudly when the model's action space differs from this run's.

    An unrecorded ``action_space`` (a pre-provenance artifact) is not a
    mismatch: it is unproven, and refusing every old artifact would be a
    louder break than the failure this guards.
    """
    trained_as = record_cfg.get("action_space")
    if not isinstance(trained_as, str) or not trained_as:
        _LOGGER.debug(
            "%s records no action_space — skipping the action-space check",
            context,
        )
        return
    if trained_as != action_space:
        raise ActionSpaceMismatchError(trained_as, action_space, context=context)


def _benchmark(
    closes: np.ndarray, start: int, n_replayed: int
) -> tuple[float, float]:
    """Buy-and-hold return and drawdown over the bars actually replayed.

    Measured on the **realized** range ``closes[start : start + n]``, not
    the requested frame, for two reasons: an episode warms up on its
    leading bars, so benchmarking from the frame's first close would
    measure a different period than the strategy traded; and the
    drawdown must describe the same period as the strategy's own.

    The equity path is ``initial_balance * close / close[0]``, so the
    drawdown fraction is scale-invariant and matches the ``max_drawdown``
    convention used for the strategy curve (positive fraction of the
    running peak).

    Args:
        closes: Float close prices of the frame the environment replayed.
        start: First bar index the environment traded on.
        n_replayed: Number of bars replayed.

    Returns:
        ``(buy_hold_return, buy_hold_max_drawdown)``; both ``0.0`` when
        fewer than two bars were replayed or the first close is
        unusable.
    """
    segment = closes[start : start + n_replayed]
    if segment.size < 2:
        return 0.0, 0.0
    first = float(segment[0])
    if not np.isfinite(first) or first <= 0.0:
        return 0.0, 0.0
    equity = segment / first
    total_return = float(equity[-1] - 1.0)
    peak = np.maximum.accumulate(equity)
    with np.errstate(invalid="ignore", divide="ignore"):
        drawdowns = (peak - equity) / peak
    max_drawdown = float(np.nanmax(drawdowns)) if drawdowns.size else 0.0
    return total_return, max_drawdown


def backtest_model(
    ticker_id: str,
    model_name: str,
    data: pd.DataFrame | None = None,
    agent: RLAgent | None = None,
    models_root: str | Path = "models",
    manager: Any | None = None,
    pages: int = 6,
    env_kwargs: dict[str, Any] | None = None,
    seed: int | None = 42,
    deterministic: bool = True,
    config_path: str | Path | None = None,
    data_window: dict[str, Any] | DataWindow | None = None,
) -> BacktestResult:
    """Walk a trained agent forward over OHLC data and compute metrics.

    Args:
        ticker_id: Ticker the model was trained on (e.g. ``"ETH/USD"``).
        model_name: Name of the saved model.
        data: OHLCV DataFrame to replay over.  When None, fresh data is
            fetched via ``manager`` (``KrakenManager.from_env()`` when
            no manager is given).
        agent: Optional pre-loaded :class:`RLAgent`; when omitted the
            model is loaded from ``models/{TICKER}/{NAME}/model.zip``.
        models_root: Registry root; defaults to ``models/``.
        manager: Optional KrakenManager used only when ``data`` is None.
        pages: OHLC pages to fetch when ``data`` is None.
        env_kwargs: Extra kwargs passed to the
            :class:`TradingEnvironment` constructor.  The strongest source
            for every environment setting.
        seed: Seed for the deterministic replay (0 disables seeding).
        deterministic: Whether the policy acts greedily.
        config_path: Optional YAML run config (``--config``) supplying
            ``fee_rate``, ``slippage``, ``action_space``,
            ``initial_balance``, ``market_data_store`` and
            ``data_window``.  When omitted, nothing is read from
            ``configs/default.yaml`` and every setting comes from the
            model's own config — the pre-existing behaviour.
        data_window: Explicit ``data_window`` block (dict or resolved
            :class:`~kraken_trading_bot.rl.data_window.DataWindow`),
            overriding the one in ``config_path``.  Leave None to take it
            from the config; an explicit block wins so a caller can pin a
            window per call without writing a YAML file.

    Returns:
        The :class:`BacktestResult` for the run, including the
        buy-and-hold benchmark, ``excess_return``, the applied
        ``fee_rate``/``slippage`` and the realized ``n_bars``.

    Raises:
        ValueError: If neither ``data`` nor a way to fetch it exists, or
            if the resolved action space contradicts the model's.
        FileNotFoundError: If ``agent`` is omitted and ``model.zip`` is
            missing, or ``config_path`` is absent from disk.
        FeatureWidthMismatchError: If the model's fitted feature names do
            not match the live pipeline's columns.
        ActionSpaceMismatchError: If the model was trained under a
            different action space than this run would use.
        NotEnoughDataError: If the resolved window leaves no tradable bar.
    """
    ticker_key = normalize_ticker_id(ticker_id)
    env_kwargs = dict(env_kwargs or {})

    run_cfg = _load_run_config(config_path)
    if data_window is None:
        window = resolve_data_window(run_cfg)
    elif isinstance(data_window, DataWindow):
        window = data_window
    else:
        window = resolve_data_window({"data_window": data_window})

    # 1. Data
    if data is None:
        if manager is None:
            from kraken_api import KrakenManager

            manager = KrakenManager.from_env()
        record = scan_model(ticker_id, model_name, root=models_root)
        interval = int(
            (record.config or {}).get("ohlcv_interval_minutes", 60)
        )
        df = read_ohlc_dataframe(
            record.config.get("ticker", ticker_key.replace("_", "/")),
            interval=interval,
            pages=pages,
            manager=manager,
            extra_features_file=record.config.get("extra_features_file"),
            funding_features_file=record.config.get("funding_features_file"),
            social_features_file=record.config.get("social_features_file"),
            signal_max_age_hours=record.config.get("signal_max_age_hours"),
            signal_require_ticker=record.config.get("signal_require_ticker", True),
            # A run config may point the read at a different store; absent
            # that key the model's own source is used, unchanged.
            market_data_store=_resolve_env_setting(
                "market_data_store",
                env_kwargs,
                run_cfg,
                record.config or {},
                None,
            ),
            # Provenance label only, same four sources: the model's own
            # config is the record of what its training bars came from,
            # so a run config may not contradict it silently.
            market_data_store_venue=_resolve_env_setting(
                "market_data_store_venue",
                env_kwargs,
                run_cfg,
                record.config or {},
                None,
            ),
        )
    else:
        df = data

    # A caller-supplied frame bypasses the read seam, so apply the same
    # vwap/count derivation here; otherwise the width guard below would
    # (correctly) reject a frame that is merely missing the seam's step.
    df = add_derived_ohlcv_features(df)

    # Pinned window + train/eval split.  With an unpinned window this
    # returns the frame itself, so the default path is untouched.
    df = evaluation_frame(df, window)
    if window.is_pinned:
        _LOGGER.info(
            "Backtest window %s -> %d replayable bars (training took the "
            "leading %d%%, this replays the out-of-sample remainder)",
            window.describe(),
            len(df),
            int(window.eval_split * 100),
        )

    # 2. Environment: reconstruct the pipeline when normalization exists.
    record = scan_model(ticker_id, model_name, root=models_root)
    config = record.config or {}
    pipeline: FeaturePipeline | None = None
    if record.normalization_path is not None:
        pipeline = FeaturePipeline(
            windows=config.get("feature_windows", [1, 4, 24]),
            feature_groups=config.get("feature_groups", None)
            or ["price", "technical", "volume", "microstructure", "signals"],
        )
        pipeline.load_normalization(ticker_key, record.normalization_path)

    if pipeline is not None:
        # Width guard (the 49 -> 55 widening ships with it).  backtest
        # previously had no guard at all, so a stale artifact surfaced
        # late — and, because normalize() drops unknown columns,
        # silently — at agent.predict() instead of at a Dutch door.
        # Non-self-referential: the model's feature_names come from its
        # own normalization.npz, the columns from the live frame.
        stats = pipeline.stats_for(ticker_key)
        if stats is not None:
            check_feature_width(
                stats.feature_names,
                list(pipeline.compute(df).columns),
                context=f"{ticker_key}/{model_name} backtest",
            )

    # Resolve the four settings this call site owns, then drop them from
    # the pass-through bag so they cannot also arrive as **kwargs.
    settings = {
        name: _resolve_env_setting(name, env_kwargs, run_cfg, config, default)
        for name, default in _ENV_SETTINGS
    }
    for name, _default in _ENV_SETTINGS:
        env_kwargs.pop(name, None)

    action_space = str(settings["action_space"])
    _check_action_space(config, action_space, f"{ticker_key}/{model_name} backtest")

    initial_balance = float(settings["initial_balance"])
    fee_rate = float(settings["fee_rate"])
    slippage = float(settings["slippage"])

    env = TradingEnvironment(
        ticker_id=ticker_id,
        data=df,
        feature_pipeline=pipeline,
        action_space=action_space,
        initial_balance=initial_balance,
        fee_rate=fee_rate,
        slippage=slippage,
        reward_spec=env_kwargs.pop("reward", config.get("reward")),
        allow_short=bool(
            env_kwargs.pop("allow_short", config.get("allow_short", False))
        ),
        **env_kwargs,
    )

    # A window that cannot fill even the indicator warm-up has no honest
    # result to report: `n_bars` would be 0 and every metric below would be
    # a division of nothing.  Refuse, naming the knob that caused it.
    if env.start_index >= env.n_bars:
        raise ValueError(
            f"No tradable bar for {ticker_key}/{model_name}: the resolved "
            f"window {window.describe()} left {env.n_bars} bars but the "
            f"indicator warm-up ends at bar {env.start_index}. Widen "
            f"data_window.since/until, lower data_window.eval_split, or "
            f"raise --pages so more of the window is inside it."
        )

    # 3. Agent (load when not provided).
    if agent is None:
        agent = RLAgent.load(
            ticker_id, model_name, env, models_root=models_root
        )
    else:
        if agent.model is None:
            raise ValueError(
                "`agent` has no policy loaded; call RLAgent.load first."
            )

    # 4. Deterministic replay.
    obs, _ = env.reset(seed=seed)
    closes = df["close"].astype(float).to_numpy()
    initial_balance = float(env.initial_balance)
    equity_curve: list[float] = [initial_balance]
    wins = 0
    closes_completed = 0
    prev_trades = 0

    i = 0
    while True:
        price = float(closes[i])
        entry_before = env.entry_price
        action = agent.predict(obs, deterministic=deterministic)
        obs, _reward, terminated, truncated, info = env.step(action)

        # A full round trip completed this bar when a position that was
        # open before the step is fully closed afterwards (the env clears
        # entry_price only when the whole position liquidates).
        if env.entry_price is None and entry_before is not None:
            fill = price * (1.0 - env.slippage)
            wins += 1 if fill > entry_before else 0
            closes_completed += 1
        prev_trades = info["trades"]

        equity_curve.append(float(info["equity"]))
        i += 1
        if terminated or truncated or i >= len(closes):
            break

    # 5. Metrics.
    eq = np.asarray(equity_curve, dtype=float)
    final_equity = float(eq[-1])
    total_return = final_equity / initial_balance - 1.0

    rets = np.diff(eq) / eq[:-1]
    if rets.size >= 2 and float(np.std(rets)) > 1e-12:
        sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(rets.size))
    else:
        sharpe = 0.0

    peak = np.maximum.accumulate(eq)
    drawdowns = (peak - eq) / peak
    max_drawdown = float(np.max(drawdowns)) if drawdowns.size else 0.0

    num_trades = env.num_trades
    win_rate = wins / closes_completed if closes_completed else 0.0

    # Benchmark over the bars actually replayed, not the ones requested.
    n_replayed = int(i)
    buy_hold_return, buy_hold_max_drawdown = _benchmark(
        closes, env.start_index, n_replayed
    )

    result = BacktestResult(
        ticker_id=ticker_key,
        model_name=model_name,
        total_return=total_return,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        num_trades=num_trades,
        win_rate=win_rate,
        equity_curve=equity_curve,
        n_steps=i,
        final_equity=final_equity,
        seed=seed,
        buy_hold_return=buy_hold_return,
        excess_return=total_return - buy_hold_return,
        buy_hold_max_drawdown=buy_hold_max_drawdown,
        n_bars=n_replayed,
        fee_rate=fee_rate,
        slippage=slippage,
        action_space=action_space,
    )
    _LOGGER.info(
        "Backtest %s/%s: %d/%d bars replayed, return=%.2f%% "
        "buy&hold=%.2f%% excess=%.2f%% max_dd=%.2f%% trades=%d win=%.2f%% "
        "fee=%.4f%% slip=%.4f%%",
        ticker_key,
        model_name,
        n_replayed,
        len(closes),
        total_return * 100.0,
        buy_hold_return * 100.0,
        result.excess_return * 100.0,
        max_drawdown * 100.0,
        num_trades,
        win_rate * 100.0,
        fee_rate * 100.0,
        slippage * 100.0,
    )
    if buy_hold_return > 0.0 and result.excess_return <= 0.0:
        _LOGGER.warning(
            "%s/%s did not beat buy-and-hold over the %d bars it replayed "
            "(return %.2f%% vs %.2f%%): the excess return is the number "
            "that answers whether this was skill or a rising asset.",
            ticker_key,
            model_name,
            n_replayed,
            total_return * 100.0,
            buy_hold_return * 100.0,
        )
    return result


__all__ = ["ActionSpaceMismatchError", "BacktestResult", "backtest_model"]