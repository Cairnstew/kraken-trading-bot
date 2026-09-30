"""Live paper-trading runner for trained RL models.

Loads a trained per-ticker PPO model and steps it against live Kraken
market data, executing *paper* trades through
:meth:`kraken_api.KrakenManager.paper` so balances and orders are tracked
by kraken-python's in-memory simulated account.  No real orders are ever
placed, and no credentials are needed: market data comes from the public
(unauthenticated) OHLC endpoint.

Action mapping (mirrors ``TradingEnvironment._execute``):

- Continuous action ``[buy_frac, sell_frac, hold_conf]``:

  - ``hold_conf >= reward.hold_confidence_threshold`` -> hold (no order);
  - ``buy_frac > sell_frac`` and ``buy_frac >= 1e-3`` -> BUY with volume
    ``quote_balance * buy_frac * trade_fraction / last_price``;
  - ``sell_frac >= 1e-3`` and a base position is held -> SELL with volume
    ``base_position * sell_frac``.
- Discrete action: ``0`` BUY ``quote_balance * trade_fraction / price``,
  ``1`` hold, ``2`` SELL the full position.

Observations replicate the environment exactly, which is the load-bearing
detail for inference: :class:`TradingEnvironment` feeds the agent the
pipeline's computed features, forward-filled then zero-filled and z-scored
with the ticker's persisted ``normalization.npz`` stats, as a ``float32``
row (:meth:`_observe` / ``_raw_feature_array``).  ``PaperTrader.
_build_observation`` rebuilds that exact vector from the live window — same
fill, same stats — so inference sees the same scale as training.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from .agent import RLAgent
from .data import NotEnoughDataError, read_ohlc_dataframe
from .environment import TradingEnvironment
from .features import FeaturePipeline, normalize_ticker_id, observation_frame
from .registry import scan_model
from .train import pair_from_ticker_id

_LOGGER = logging.getLogger(__name__)

# Minimum action magnitude considered an actionable signal (continuous).
_SIGNAL_EPS = 1e-3

# OHLC pages fetched per tick (small: live public endpoint, no credentials).
_FETCH_PAGES = 2


@dataclass
class PaperSignal:
    """One paper order derived from an RL action.

    Attributes:
        side: ``"buy"``, ``"sell"`` or ``"hold"``.
        volume: Base-currency volume to trade (0 for hold).
        reason: Human-readable explanation of why this action fired.
    """

    side: str
    volume: Decimal
    reason: str


class PaperTrader:
    """Runs a trained RL model against live market data in paper-trade mode.

    One PaperTrader per ticker (per-ticker model specialization invariant).
    Executes through ``kraken_api.KrakenManager.paper()`` so paper balances
    and orders are tracked by the kraken-python paper account.

    Parameters:
        ticker_id: Ticker the model trades (e.g. ``"ETH/USD"`` or
            ``"ETH_USD"``; normalized on disk).
        model_name: Name of the trained model under the ticker.
        manager: A :class:`kraken_api.KrakenManager`; defaults to
            ``KrakenManager.paper()`` (paper trading, no real orders).
        models_root: Registry root; defaults to ``models/``.
        interval: Seconds between ticks (only used by
            :func:`run_paper_trader`; the OHLC candle interval used for
            observations comes from the model's ``config.yaml`` so live
            data matches training).
        context_bars: Optional cap on the trailing bars kept when building
            each observation (bounds feature look-back cost on very long
            windows).
        env_kwargs: Extra kwargs for the internal
            :class:`TradingEnvironment`; they override the per-model config
            (e.g. ``action_space``), mirroring :func:`backtest_model`.
        trade_fraction: Fraction of the quote balance spent per BUY signal
            (and the discrete BUY budget), default 0.2.
        dry_run: When True, print would-be orders and never call
            ``manager.buy``/``manager.sell``.
        data_fetcher: Optional ``callable(pair, interval_minutes) ->
            DataFrame`` overriding live OHLC fetching (used by tests and
            for wiring in alternate data sources).
        sleep_fn: Optional ``callable(seconds)`` used by
            :func:`run_paper_trader` between ticks (testable pacing).

    Raises:
        FileNotFoundError: If the model has no ``model.zip`` / ``config.yaml``
            under ``models/{TICKER_ID}/{model_name}/``.
        ValueError: If the live observation width does not match the model's
            observation space.
    """

    def __init__(
        self,
        ticker_id: str,
        model_name: str,
        manager: Any | None = None,
        models_root: str | Path = "models",
        interval: int = 60,
        context_bars: int | None = None,
        env_kwargs: dict[str, Any] | None = None,
        trade_fraction: float = 0.2,
        dry_run: bool = False,
        data_fetcher: Callable[[str, int], pd.DataFrame] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.ticker_key = normalize_ticker_id(ticker_id)
        self.model_name = model_name
        self.models_root = Path(models_root).expanduser()
        self.interval = float(interval)
        self.context_bars = int(context_bars) if context_bars is not None else None
        self.trade_fraction = float(trade_fraction)
        self.dry_run = bool(dry_run)
        self._data_fetcher = data_fetcher
        self.sleep_fn = sleep_fn

        if manager is None:  # deferred import: only needed for live trading
            from kraken_api import KrakenManager

            manager = KrakenManager.paper()
        self.manager = manager

        # ------------------------------------------------------------------
        # Load the model's per-ticker artifacts.
        record = scan_model(ticker_id, model_name, root=self.models_root)
        if record.model_path is None:
            raise FileNotFoundError(
                f"No trained model for {self.ticker_key}/{model_name} at "
                f"{record.root}; train it first (RLAgent.train or "
                f"rl.train_ticker)."
            )
        if record.config_path is None:
            raise FileNotFoundError(
                f"Missing config.yaml for {self.ticker_key}/{model_name} at "
                f"{record.root}; re-register the model."
            )
        self.record = record
        self.config: dict[str, Any] = record.config or {}

        # The pair/OHLC interval come from the exact config the model was
        # trained with, so live windows match training windows.
        self.pair = str(self.config.get("ticker") or pair_from_ticker_id(ticker_id))
        self.ohlcv_interval_minutes = int(
            self.config.get("ohlcv_interval_minutes", 60)
        )
        if "/" in self.pair:
            self.base_asset, self.quote_asset = self.pair.split("/")
        else:
            raise ValueError(
                f"Unrecognized pair {self.pair!r}; expected Kraken 'BASE/QUOTE' "
                f"notation (trained config ticker field)."
            )

        # ------------------------------------------------------------------
        # Rebuild the environment exactly like training so observation/action
        # spaces match the saved policy.  Normalization stats load when the
        # model directory has them (env then skips refitting).
        config = self.config
        env_kwargs = dict(env_kwargs or {})
        pipeline = FeaturePipeline(
            windows=config.get("feature_windows", [1, 4, 24]),
            feature_groups=config.get("feature_groups", None)
            or ["price", "technical", "volume", "microstructure", "signals"],
        )
        if record.normalization_path is not None:
            pipeline.load_normalization(self.ticker_key, record.normalization_path)
        self.pipeline = pipeline

        initial_df = self._fetch_data()
        if initial_df is None or initial_df.empty:
            raise NotEnoughDataError(1, 0, what="live OHLC bars")
        self.env = TradingEnvironment(
            ticker_id=ticker_id,
            data=initial_df,
            feature_pipeline=self.pipeline,
            action_space=env_kwargs.pop(
                "action_space", config.get("action_space", "continuous")
            ),
            initial_balance=float(
                env_kwargs.pop("initial_balance", config.get("initial_balance", 10_000.0))
            ),
            fee_rate=float(env_kwargs.pop("fee_rate", config.get("fee_rate", 0.0))),
            slippage=float(env_kwargs.pop("slippage", config.get("slippage", 0.0))),
            reward_spec=env_kwargs.pop("reward", config.get("reward")),
            allow_short=bool(
                env_kwargs.pop("allow_short", config.get("allow_short", False))
            ),
            **env_kwargs,
        )
        self.action_space_name = self.env.action_space_name
        self.reward_spec = self.env.reward_spec
        self.initial_balance = float(self.env.initial_balance)

        self.agent = RLAgent.load(
            ticker_id, model_name, self.env, models_root=self.models_root
        )

        # ------------------------------------------------------------------
        # Paper-trading state.
        self._ticks = 0
        self._buys = 0
        self._sells = 0
        self._holds = 0
        self._internal_position = Decimal("0")  # base units (fallback tracker)
        self._avg_cost = Decimal("0")
        self._realized_pnl = Decimal("0")
        self._last_close: float | None = None
        self._last_signal: PaperSignal | None = None

        if self.paper_account is not None:
            _LOGGER.debug(
                "%s paper account available: %s",
                self.ticker_key,
                dict(self.paper_account.balances),
            )

    # ------------------------------------------------------------------
    # paper account access
    # ------------------------------------------------------------------
    @property
    def paper_account(self) -> Any:
        """The kraken-python ``PaperAccount`` when the manager is paper."""
        try:
            return self.manager.paper_account
        except AttributeError:
            return None

    def _paper_balance(self, asset: str) -> Decimal | None:
        """Read one asset's balance from the paper account, if any."""
        acct = self.paper_account
        if acct is None:
            return None
        try:
            balances = getattr(acct, "balances", None)
            if isinstance(balances, dict) and asset in balances:
                return Decimal(str(balances[asset]))
        except Exception:  # pragma: no cover - defensive on mock managers
            return None
        return None

    def _quote_balance(self) -> Decimal:
        """Quote-currency balance: paper account when present, else env balance."""
        bal = self._paper_balance(self.quote_asset)
        if bal is not None:
            return bal
        return Decimal(str(self.env.balance))

    def _base_position(self) -> Decimal:
        """Base-currency position from the paper account, else internal tracker."""
        bal = self._paper_balance(self.base_asset)
        if bal is not None:
            return bal
        return self._internal_position

    # ------------------------------------------------------------------
    # data flow per tick
    # ------------------------------------------------------------------
    def _fetch_data(self) -> pd.DataFrame:
        """Return the latest OHLCV window (``pages=2`` public fetch).

        When the model config sets ``market_data_store``, the window is read
        through the local store (fetch -> upsert -> read), so each tick both
        appends to the store and reads the fresh tail back.
        """
        if self._data_fetcher is not None:
            return self._data_fetcher(self.pair, self.ohlcv_interval_minutes)
        return read_ohlc_dataframe(
            self.pair,
            interval=self.ohlcv_interval_minutes,
            pages=_FETCH_PAGES,
            manager=self.manager,
            extra_features_file=self.config.get("extra_features_file"),
            funding_features_file=self.config.get("funding_features_file"),
            social_features_file=self.config.get("social_features_file"),
            market_data_store=self.config.get("market_data_store"),
        )

    def _build_observation(self, df: pd.DataFrame) -> np.ndarray:
        """Build the observation vector exactly as the training env did.

        The environment feeds the agent the pipeline's computed features
        forward-filled then zero-filled, z-scored with the ticker's
        ``normalization.npz`` stats (see ``TradingEnvironment._observe`` /
        ``_raw_feature_array``), not the raw un-normalized row; using the
        same construction keeps live inference on the same scale as
        training.  Those stats are the ones loaded from the model
        directory — never refitted from the live window.

        Raises:
            ValueError: If the resulting width differs from the model's
                observation space (stale/mismatched feature config).
        """
        if self.context_bars is not None:
            df = df.tail(self.context_bars)
        observed = observation_frame(self.pipeline.compute(df))
        stats = self.pipeline.stats_for(self.ticker_key)
        matrix = observed if stats is None else stats.normalize(observed)
        obs = np.asarray(matrix.to_numpy(dtype=np.float32)[-1], dtype=np.float32)
        return self._validate_observation(obs)

    def _validate_observation(self, obs: np.ndarray) -> np.ndarray:
        """Raise a clear error when the live observation width mismatches."""
        expected = int(self.env.observation_space.shape[0])
        actual = int(obs.shape[0])
        if actual != expected:
            raise ValueError(
                f"Observation width mismatch for {self.ticker_key}/{self.model_name}: "
                f"live features have {actual} columns but the model was trained "
                f"with an observation space of {expected}. This usually means the "
                f"feature config (feature_windows/feature_groups) or "
                f"normalization.npz does not match the model; retrain or align "
                f"the model config before paper trading."
            )
        return obs

    # ------------------------------------------------------------------
    # tick loop
    # ------------------------------------------------------------------
    def step(self, df: pd.DataFrame | None = None) -> dict[str, Any]:
        """Run one tick: fetch, observe, predict, execute (paper).

        Args:
            df: Optional pre-fetched OHLCV frame (skips the live fetch path;
                used by tests and external schedulers).

        Returns:
            A dict with ``tick``, ``action``, ``side``, ``volume``, ``price``.

        Raises:
            NotEnoughDataError: If no OHLC bars are available this tick.
            ValueError: On observation width mismatch (see
                :meth:`_validate_observation`).
        """
        if df is None:
            df = self._fetch_data()
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            raise NotEnoughDataError(1, 0, what="live OHLC bars")

        self._last_close = float(df["close"].iloc[-1])
        obs = self._build_observation(df)
        action = self.agent.predict(obs, deterministic=True)
        self._ticks += 1

        signal = self._interpret_action(action)
        self._last_signal = signal
        self._execute_signal(signal)

        return {
            "tick": self._ticks,
            "action": action,
            "side": signal.side if signal is not None else "none",
            "volume": (
                float(signal.volume) if signal is not None and signal.volume else 0.0
            ),
            "price": self._last_close,
        }

    # ------------------------------------------------------------------
    # action -> paper order
    # ------------------------------------------------------------------
    def _interpret_action(self, action: Any) -> PaperSignal:
        """Map an RL action to a :class:`PaperSignal` (never None)."""
        hold_threshold = float(self.reward_spec.hold_confidence_threshold)

        if self.action_space_name == "discrete":
            idx = int(np.asarray(action).reshape(-1)[0])
            if idx == 0:
                volume = self._buy_volume(Decimal("1.0"))
                if volume <= 0:
                    return PaperSignal("hold", Decimal("0"), "no quote balance to buy")
                return PaperSignal("buy", volume, "discrete buy action")
            if idx == 2:
                volume = self._base_position()
                if volume <= 0:
                    return PaperSignal("hold", Decimal("0"), "no base position to sell")
                return PaperSignal("sell", volume, "discrete sell action")
            return PaperSignal("hold", Decimal("0"), "discrete hold action")

        arr = np.asarray(action, dtype=float).reshape(-1)
        if arr.size != 3:
            raise ValueError(
                f"Continuous action must have 3 components, got {arr.size}"
            )
        buy_frac = float(np.clip(arr[0], 0.0, 1.0))
        sell_frac = float(np.clip(arr[1], 0.0, 1.0))
        hold_conf = float(np.clip(arr[2], 0.0, 1.0))

        if hold_conf >= hold_threshold:
            return PaperSignal(
                "hold",
                Decimal("0"),
                f"hold_confidence {hold_conf:.3f} >= {hold_threshold:.3f}",
            )
        if buy_frac > sell_frac + _SIGNAL_EPS:
            volume = self._buy_volume(Decimal(str(buy_frac)))
            if volume <= 0:
                return PaperSignal("hold", Decimal("0"), "no quote balance to buy")
            return PaperSignal(
                "buy", volume, f"continuous buy_frac={buy_frac:.3f}"
            )
        if sell_frac >= _SIGNAL_EPS:
            volume = self._base_position() * Decimal(str(sell_frac))
            if volume <= 0:
                return PaperSignal("hold", Decimal("0"), "no base position to sell")
            return PaperSignal(
                "sell", volume, f"continuous sell_frac={sell_frac:.3f}"
            )
        return PaperSignal("hold", Decimal("0"), "no actionable signal")

    def _buy_volume(self, buy_frac: Decimal) -> Decimal:
        """Base volume for a buy: ``balance * buy_frac * trade_fraction / price``."""
        balance = self._quote_balance()
        price = Decimal(str(self._last_close or 0.0))
        if balance <= 0 or price <= 0:
            return Decimal("0")
        budget = balance * buy_frac * Decimal(str(self.trade_fraction))
        return min(budget, balance) / price

    def _execute_signal(self, signal: PaperSignal | None) -> bool:
        """Execute one paper signal (hold / order / dry-run log).

        Returns:
            True when handled (paper fill or hold); False on a failed order.
        """
        if signal is None or signal.side == "hold":
            self._holds += 1
            _LOGGER.debug(
                "[PAPER] %s/%s hold: %s",
                self.ticker_key,
                self.model_name,
                signal.reason if signal else "no signal",
            )
            return True

        volume = signal.volume
        price = self._last_close or 0.0
        if volume <= 0:
            self._holds += 1
            return True

        if self.dry_run:
            _LOGGER.info(
                "[DRY-RUN] would %s %s %s @ %.8f (%s)",
                signal.side.upper(),
                volume,
                self.pair,
                price,
                signal.reason,
            )
            self._buys += 1 if signal.side == "buy" else 0
            self._sells += 1 if signal.side == "sell" else 0
            return True

        try:
            if signal.side == "buy":
                result = self.manager.buy(self.pair, str(volume))
                self._buys += 1
            else:
                result = self.manager.sell(self.pair, str(volume))
                self._sells += 1
            self._track_fill(signal.side, volume, Decimal(str(price)))
            _LOGGER.info(
                "Paper order filled: %s %s %s @ %.8f -> %s",
                signal.side.upper(),
                volume,
                self.pair,
                price,
                result,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - surface any manager failure
            _LOGGER.error("Failed to place paper %s: %s", signal.side, exc)
            return False

    def _track_fill(self, side: str, volume: Decimal, price: Decimal) -> None:
        """Update the internal average-cost and realized-PnL tracker."""
        vol = Decimal(str(volume))
        if side == "buy":
            new_qty = self._internal_position + vol
            if new_qty > 0:
                self._avg_cost = (
                    self._avg_cost * self._internal_position + price * vol
                ) / new_qty
                self._internal_position = new_qty
        else:
            qty = min(vol, self._internal_position)
            self._realized_pnl += (price - self._avg_cost) * qty
            self._internal_position = max(
                self._internal_position - qty, Decimal("0")
            )
            if self._internal_position == 0:
                self._avg_cost = Decimal("0")

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """A JSON-serializable report of the paper-trading session."""
        quote = self._quote_balance()
        base = self._base_position()
        price = Decimal(str(self._last_close or 0.0))
        equity = quote + base * price
        return {
            "ticker_id": self.ticker_key,
            "model_name": self.model_name,
            "pair": self.pair,
            "dry_run": self.dry_run,
            "interval_seconds": self.interval,
            "ticks": self._ticks,
            "buys": self._buys,
            "sells": self._sells,
            "holds": self._holds,
            "quote_balance": str(quote),
            "base_position": str(base),
            "last_price": float(price),
            "equity_estimate": str(equity),
            "realized_pnl": str(self._realized_pnl),
        }

    def render_summary(self) -> None:
        """Print the session summary one line at a time."""
        s = self.summary()
        print("=" * 60)
        print(f"Paper trade summary: {s['ticker_id']}/{s['model_name']} ({s['pair']})")
        mode = "DRY-RUN (no orders executed)" if self.dry_run else "paper account"
        print(f"  mode                    : {mode}")
        print(f"  ticks                   : {s['ticks']}")
        print(f"  buys / sells / holds    : {s['buys']} / {s['sells']} / {s['holds']}")
        print(f"  quote balance           : {s['quote_balance']}")
        print(f"  base position           : {s['base_position']}")
        print(f"  last price              : {s['last_price']}")
        print(f"  equity estimate         : {s['equity_estimate']}")
        print(f"  realized PnL (internal) : {s['realized_pnl']}")
        print("=" * 60)

    def close(self) -> None:
        """Release resources (no-op for paper trading)."""
        pass


def run_paper_trader(
    ticker_id: str,
    model_name: str,
    *,
    iterations: int | None = None,
    interval: int = 60,
    dry_run: bool = False,
    models_root: str | Path = "models",
    manager: Any | None = None,
    context_bars: int | None = None,
    env_kwargs: dict[str, Any] | None = None,
    trade_fraction: float = 0.2,
    data_fetcher: Callable[[str, int], pd.DataFrame] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> PaperTrader:
    """Run a :class:`PaperTrader` for a fixed number of ticks (or until
    interrupted), printing a one-line per-tick log and a final summary.

    Args:
        ticker_id: Ticker the model trades (e.g. ``"ETH_USD"``).
        model_name: Name of the trained model under the ticker.
        iterations: Number of ticks to run; None (default) runs forever
            until :class:`KeyboardInterrupt`.
        interval: Seconds between ticks (default 60).
        dry_run: Print would-be orders without executing (see
            :class:`PaperTrader`).
        models_root: Registry root; defaults to ``models/``.
        manager: Optional KrakenManager; defaults to ``KrakenManager.paper()``.
        context_bars: Optional trailing-bar observation cap.
        env_kwargs: Extra kwargs for the internal environment.
        trade_fraction: Fraction of the quote balance spent per BUY.
        data_fetcher: Optional ``callable(pair, interval_minutes) -> DataFrame``.
        sleep_fn: Optional ``callable(seconds)`` between ticks (defaults to
            :func:`time.sleep`).

    Returns:
        The finished :class:`PaperTrader` (its :meth:`summary` holds results).
    """
    trader = PaperTrader(
        ticker_id=ticker_id,
        model_name=model_name,
        manager=manager,
        models_root=models_root,
        interval=interval,
        context_bars=context_bars,
        env_kwargs=env_kwargs,
        trade_fraction=trade_fraction,
        dry_run=dry_run,
        data_fetcher=data_fetcher,
        sleep_fn=sleep_fn,
    )
    sleeper: Callable[[float], None] = (
        sleep_fn if sleep_fn is not None else time.sleep
    )

    tick = 0
    try:
        while iterations is None or tick < iterations:
            info = trader.step()
            tick += 1
            _LOGGER.info(
                "[PAPER] tick=%d action=%s side=%s volume=%s price=%.8f",
                info["tick"],
                info["action"],
                info["side"],
                info["volume"],
                info["price"],
            )
            print(
                f"[PAPER] tick={info['tick']} side={info['side']} "
                f"volume={info['volume']:.8f} price={info['price']:.8f}"
            )
            if iterations is None or tick < iterations:
                sleeper(float(interval))
    except KeyboardInterrupt:
        _LOGGER.info("Paper trader interrupted by user")
        print("\nInterrupted.")
    finally:
        trader.render_summary()
    return trader


__all__ = ["PaperSignal", "PaperTrader", "run_paper_trader"]