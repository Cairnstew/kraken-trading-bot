"""Null-policy baselines for a model-matrix run.

**A negative return is not a finding until you know what doing nothing, or
doing something arbitrary, would have produced on the same bars.**  A
backtest that reports "-2.3% excess" invites the reading "the model lost",
which is not yet a statement about the *model* — it is a statement about
the model *relative to a benchmark that may itself be negative*.  This
module supplies the missing reference points:

``flat``
    Never trade.  Worth exactly the benchmark, minus the cost of whatever
    the environment charges to open and close a position.  This is the
    floor: any policy that cannot beat it has no edge at all.

``random``
    A random policy *matched on trade count* to the model being tested.
    Matching matters: turnover drives cost, so an unmatched random policy
    that trades 5 times is not a fair null for one that trades 50.  Run
    many draws and the result is a DISTRIBUTION, which is the only thing
    that can answer "is -2.3% distinguishable from luck?"

``momentum``
    A simple, declared rule: buy when the short moving average is above
    the long one.  Not a strawman — it is the rule the shipped SMA
    strategy implements, so it is the honest "did the RL policy beat the
    obvious thing" comparison.

Why this is a separate tool and not a matrix axis
    Every matrix cell trains a PPO model. A null policy has no parameters,
    so expressing it as an axis would burn a full training run per cell to
    compute something that does not depend on training.  This replays the
    SAME pinned window and the SAME fee/slippage accounting the matrix
    used, reading the bars once and running every policy over them, so the
    comparison is like-for-like by construction rather than by assertion.

Usage
    From the repo root, inside the dev shell::

        just baselines --spec configs/matrix.eth-highseed.yaml --draws 200

    or directly::

        python tools/null_baselines.py \\
            --results /tmp/ktb-matrix/eth-highseed/cells.jsonl --draws 200
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools"))

# Statuses written by tools/model_matrix.py that mean there is no usable
# measurement to compare against. "invalid" is deliberately NOT here: it is a
# verdict under the spec in force at execution time, and the caller owns the
# comparison being asked for now. See _load_cells.
NO_MEASUREMENT_STATUSES = frozenset({"error", "dry_run"})


@dataclass
class BaselineResult:
    """One policy's replay over one window, in the matrix's own units."""

    name: str
    total_return: float
    buy_hold_return: float
    excess_return: float
    max_drawdown: float
    num_trades: int
    n_bars: int
    fee_rate: float
    slippage: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.name,
            "total_return": self.total_return,
            "buy_hold_return": self.buy_hold_return,
            "excess_return": self.excess_return,
            "max_drawdown": self.max_drawdown,
            "num_trades": self.num_trades,
            "n_bars": self.n_bars,
            "fee_rate": self.fee_rate,
            "slippage": self.slippage,
        }


@dataclass
class RandomDraws:
    """The random-policy null as a DISTRIBUTION, not a single number."""

    draws: list[float] = field(default_factory=list)
    matched_trades: int = 0

    def summary(self) -> dict[str, Any]:
        if not self.draws:
            return {"n_draws": 0}
        s = sorted(self.draws)
        return {
            "n_draws": len(s),
            "matched_trades": self.matched_trades,
            "mean": statistics.fmean(s),
            "median": statistics.median(s),
            "sd": statistics.stdev(s) if len(s) > 1 else 0.0,
            "min": s[0],
            "max": s[-1],
            "p05": _quantile(s, 0.05),
            "p95": _quantile(s, 0.95),
        }


def _quantile(sorted_values: Sequence[float], p: float) -> float:
    """Linear-interpolation quantile over an already-sorted sequence."""
    if not sorted_values:
        raise ValueError("empty sequence")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    k = (len(sorted_values) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(sorted_values[int(k)])
    return float(
        sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)
    )


# ── execution accounting ─────────────────────────────────────────────────
#
# Deliberately re-implemented here rather than imported from the RL package
# so this tool keeps working if the environment's internals move, and so it
# can be read side by side with the numbers it is comparing. It mirrors
# `TradingEnvironment._execute`: a fee per executed order and a fractional
# adverse move on the fill, applied on the side being traded.


def _fill_price(price: float, side: int, slippage: float) -> float:
    """Price actually traded at: adverse by `slippage` on the traded side.

    Matches `TradingEnvironment._execute` exactly, in both directions:

        buy  -> fill = price * (1 + slippage)   # pay MORE
        sell -> fill = price * (1 - slippage)   # receive LESS

    `side > 0` is a **buy** and `side < 0` a **sell**; both call sites
    pass `+1` to enter and `-1` to exit.

    FIXED 2026-10-05 (was: `1 - slippage` for a buy, `1 + slippage` for a
    sell -- the wrong way round).  The old version combined with those
    call sites to give every long-only null a CHEAP entry and a RICH exit,
    crediting each null roughly +0.001 (two sides x 5 bp) against the
    models it is compared with: the nulls were mildly **optimistic**, and
    the bias is in the one direction that flatters a null.  On the deep-ab
    window the nulls scored -23% to -92% against models at -9.7% to
    +28.6%, so no verdict there moved; on a flat window it would have.

    The direction is now load-bearing rather than incidental, so it is
    pinned by ``tests/test_null_baseline_fill_prices.py``: an entry must
    cost MORE than the bar price and an exit must fetch LESS.
    """
    return price * (1.0 + slippage) if side > 0 else price * (1.0 - slippage)


def _simulate_long_only(
    closes: Sequence[float],
    actions: Sequence[int],
    *,
    initial_balance: float,
    fee_rate: float,
    slippage: float,
    name: str,
    buy_hold_return: float,
) -> BaselineResult:
    """Replay `actions` over `closes` with the environment's cost model.

    Long-only, which is the shipped default (`allow_short: false`), so a
    null and the matrix's own cells are on the same footing. `actions` is
    1-indexed against `closes` by position: action[i] is executed at
    closes[i].
    """
    cash = float(initial_balance)
    units = 0.0
    in_position = False
    entry = 0.0
    trades = 0
    equity_curve: list[float] = [cash]

    for i, action in enumerate(actions):
        price = float(closes[i])
        if action > 0 and not in_position:
            fill = _fill_price(price, +1, slippage)
            spend = cash / (1.0 + fee_rate)
            if spend > 0 and fill > 0:
                units = spend / fill
                cash = 0.0
                entry = fill
                in_position = True
                trades += 1
        elif action < 0 and in_position:
            fill = _fill_price(price, -1, slippage)
            cash = units * fill * (1.0 - fee_rate)
            units = 0.0
            in_position = False
            trades += 1
        equity_curve.append(cash + units * price)

    eq = equity_curve
    final = eq[-1]
    total_return = final / initial_balance - 1.0
    peak = -math.inf
    max_dd = 0.0
    for value in eq:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return BaselineResult(
        name=name,
        total_return=total_return,
        buy_hold_return=buy_hold_return,
        excess_return=total_return - buy_hold_return,
        max_drawdown=max_dd,
        num_trades=trades,
        n_bars=len(actions),
        fee_rate=fee_rate,
        slippage=slippage,
    )


def flat_policy(n: int) -> list[int]:
    """Never trade. The floor every other policy has to clear."""
    return [0] * n


# ── exposure accounting ────────────────────────────────────────────────
#
# Exposure is defined EXACTLY as the environment defines it and as the
# 0c-2 measurement defined it: `abs(position * price) / equity` at that
# bar's close, after that bar's order. Re-implemented here (like
# `_simulate_long_only`) so this tool stays independent of the RL package.


def _exposure_curve(
    closes: Sequence[float],
    actions: Sequence[int],
    *,
    initial_balance: float,
    fee_rate: float,
    slippage: float,
) -> list[float]:
    """Per-bar ``|position| * price / equity`` under the same cost model.

    Mirrors `_simulate_long_only`'s accounting bar for bar, so the
    exposure of a null and the exposure of a model are measured the same
    way. Needed because `excess_return` alone cannot distinguish skill
    from de-risking: on a falling window a policy that simply holds less
    beats buy-and-hold without being right about anything.
    """
    cash, units, in_position = float(initial_balance), 0.0, False
    out: list[float] = []
    n = min(len(closes), len(actions))
    for i in range(n):
        price = float(closes[i])
        action = actions[i]
        if action > 0 and not in_position:
            fill = _fill_price(price, +1, slippage)
            spend = cash / (1.0 + fee_rate)
            if spend > 0 and fill > 0:
                units, cash, in_position = spend / fill, 0.0, True
        elif action < 0 and in_position:
            cash = units * _fill_price(price, -1, slippage) * (1.0 - fee_rate)
            units, in_position = 0.0, False
        equity = cash + units * price
        out.append(abs(units * price) / equity if equity > 0.0 else 0.0)
    return out


def exposure_matched_baseline(
    closes: Sequence[float],
    x: float,
    *,
    initial_balance: float,
    fee_rate: float,
    slippage: float,
    buy_hold_return: float,
) -> dict[str, Any]:
    """Constant fractional exposure ``x`` of buy-and-hold. One entry fill.

    THE SEMANTICS, stated exactly because the number is easy to
    misread:

    - One fill, on the FIRST bar, buying fraction ``x`` of the initial
      balance, using the environment's own convention (fee taken in units,
      slippage adverse on the traded side). Then it holds to the last bar
      and never trades again.
    - ``x`` is the MODEL'S OWN realised ex-post mean exposure over the
      same bars, taken from the backtest record. **This makes the
      comparison a DECOMPOSITION, not an achievable strategy.** A live
      trader cannot know in advance the mean exposure their own policy
      will realise, so `alpha_vs_x` is not a target anyone could have hit;
      it answers only the narrower question "given how exposed this
      policy actually was, was its SELECTION of when to be exposed any
      better than being exposed constantly at that same level?".
    - It is therefore the metric that cannot be won by de-risking and
      cannot be won by beta: exposure is matched by construction, so a
      positive `alpha_vs_x` is evidence about timing alone.
    - `alpha_vs_x_adj` additionally adds back the model's own realised
      friction (`total_notional * (fee_rate + slippage)`), because the
      model pays for hundreds of fills while this baseline pays for one.
      That gap is churn cost, not timing error, so `alpha_vs_x` alone
      understates timing quality. `alpha_vs_x` stays PRIMARY; the
      adjusted figure is a diagnostic that credits the model with a
      frictionless version of its own turnover.

    Measured cost/notional on the deep-ab window is 0.003103-0.003104
    across all five models against a modelled fee+slippage of 0.0031, so
    `fee_rate + slippage` is the right per-unit rate here.
    """
    if not closes:
        return {
            "policy": "exposure-matched",
            "x": x,
            "total_return": 0.0,
            "buy_hold_return": buy_hold_return,
            "excess_return": 0.0,
            "max_drawdown": 0.0,
            "num_trades": 0,
            "mean_exposure": 0.0,
            "n_bars": 0,
        }
    frac = max(0.0, min(float(x), 1.0))
    spend = initial_balance * frac
    # Buy fill, ENVIRONMENT convention: a buy pays price * (1 + slippage).
    # Spelled out here rather than routed through `_fill_price` because this
    # is the PRIMARY metric: when `_fill_price` had its two directions
    # swapped (fixed 2026-10-05, see its docstring) this line was the one
    # place still charging the environment's way, and keeping it explicit is
    # what kept `alpha_vs_x` from being a decomposition against a null
    # nobody could trade.
    fill = float(closes[0]) * (1.0 + slippage)
    units = (spend / fill) * (1.0 - fee_rate) if fill > 0 else 0.0
    cash = initial_balance - spend
    peak, max_dd = -math.inf, 0.0
    exposure_sum = 0.0
    for price in closes:
        equity = cash + units * float(price)
        if equity > 0.0:
            exposure_sum += abs(units * float(price)) / equity
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    final = cash + units * float(closes[-1])
    total_return = final / initial_balance - 1.0
    return {
        "policy": "exposure-matched (constant x of buy&hold, one entry fill)",
        "x": frac,
        "total_return": total_return,
        "buy_hold_return": buy_hold_return,
        "excess_return": total_return - buy_hold_return,
        "max_drawdown": max_dd,
        "num_trades": 1 if frac > 0.0 else 0,
        # Realised, not assumed: exposure drifts as price moves, so the
        # mean over the slice is not exactly the x that was bought.
        "mean_exposure": exposure_sum / len(closes) if closes else 0.0,
        "n_bars": len(closes),
        "fee_rate": fee_rate,
        "slippage": slippage,
    }


def momentum_policy(closes: Sequence[float], fast: int = 6,
                    slow: int = 24) -> list[int]:
    """Buy while the fast SMA is above the slow SMA, else flat.

    A crossover rule with no exit other than the signal flipping — the same
    shape as the shipped SMA strategy, so it is the "did the RL policy beat
    the obvious thing" reference rather than a strawman.
    """
    n = len(closes)
    out: list[int] = []
    for i in range(n):
        if i < slow:
            out.append(0)
            continue
        fast_ma = sum(closes[i - fast + 1 : i + 1]) / fast
        slow_ma = sum(closes[i - slow + 1 : i + 1]) / slow
        out.append(1 if fast_ma > slow_ma else 0)
    return out


def momentum_stateful_policy(closes: Sequence[float], fast: int = 3,
                             slow: int = 12) -> list[int]:
    """Long while the fast SMA exceeds the slow SMA, re-entering on a flip.

    `momentum_policy` above is the pure crossover and, measured on a 127-bar
    eval slice, took ONE trade — the signal essentially never flips, so it
    compares a near-flat policy against the model rather than a trend
    follower. This variant is stateful: it stays long while the condition
    holds and re-enters when it returns, so it expresses the same idea at a
    cadence the window can actually resolve. Defaults are half the crossover
    pair for the same reason.
    """
    n = len(closes)
    out: list[int] = [0] * n
    for i in range(n):
        if i < slow:
            continue
        fast_ma = sum(closes[i - fast + 1 : i + 1]) / fast
        slow_ma = sum(closes[i - slow + 1 : i + 1]) / slow
        # 0 flat, +1 enter long, -1 exit long. The caller tracks the state,
        # so a run of 1s is ONE position, not one entry per bar.
        out[i] = 1 if fast_ma > slow_ma else -1
    return out


def random_policy_matched(n: int, trades: int, rng) -> list[int]:
    """A random long/flat policy executing approximately `trades` round trips.

    Entry points are drawn WITHOUT replacement from the interior bars, so
    each draw is a genuinely different schedule — an earlier version spaced
    entries on a fixed stride and took the `rng` argument without using it,
    which made all 200 draws identical and reported sd 0.00% for a
    "distribution". The realised trade count is reported per draw rather
    than assumed, so a draw that could not fit the requested count says so.
    """
    if trades <= 0 or n < 4:
        return [0] * n
    # Each round trip consumes an entry bar and at least one exit bar, so the
    # most that can fit is n // 2.
    want = min(trades, n // 2)
    # Candidate entry bars leave room for an exit.
    interior = list(range(1, n - 1))
    if not interior:
        return [0] * n
    chosen = sorted(rng.sample(interior, k=min(want, len(interior))))
    actions = [0] * n
    for pos in chosen:
        actions[pos] = 1
        # Hold length is itself random, so the draw varies the exposure too.
        hold = rng.randint(1, max(2, n // max(want, 1)))
        actions[min(n - 1, pos + hold)] = -1
    return actions


def timing_matched_shuffled(
    model_actions: Sequence[int], closes: Sequence[float], rng
) -> list[int]:
    """The model's OWN entry and exit bars, with the DECISIONS shuffled.

    SECONDARY DIAGNOSTIC. Not a skill bar. Read
    :func:`exposure_matched_baseline` for the primary comparison.

    What it is for: a random policy matched only on trade COUNT is a weak
    test under costs, because ANY uninformed turnover loses money.
    Matching on count holds turnover roughly fixed, so what that null
    measures is "how bad is trading this much at random" -- which the cost
    model already answers. This variant also fixes the TIMING skeleton: it
    enters and exits on exactly the bars the model did, and randomises only
    whether each entry is taken.

    WHAT IT PRESERVES, precisely:
      - the bar positions of the model's entry signals (each retained
        independently with p=0.5);
      - the bar positions of the model's exit signals (retained in full --
        an exit with no position is a no-op, so it just closes whatever the
        shuffled entry opened);
      - therefore the holding-period SKELETON, and -- measured -- roughly
        the aggregate time-in-market (2026-10-05, 5 models on the deep-ab
        window: model 82.4% vs null median 81.6%).

    WHAT IT DOES NOT PRESERVE. An earlier version of this docstring
    claimed "the trade count, the holding periods and the exposure
    schedule are all identical". That was wrong, and the error matters,
    so it is corrected here rather than left to be re-read:

      - TRADE COUNT. The p=0.5 coin fires on an entry stream of 258-5,501
        signals, while `_simulate_long_only` can only enter when flat.
        Measured 2026-10-05: null median trades 98 vs the models' 902 --
        and per model the null traded 14x to 312x FEWER fills (seed 45:
        3,433 vs 11). The null therefore pays almost no friction while the
        models pay 2.5-51pp of it, so a large part of "beating this null"
        is "not paying your own bill". Measured cost/notional is 0.0031,
        so the two bills are directly comparable.
      - EXPOSURE LEVEL. This null is BINARY -- 0% or 100%, because entry
        invests `cash / (1 + fee_rate)` all-in. The models are FRACTIONAL:
        the five deep-ab models realised mean exposures of 0.027, 0.046,
        0.758, 0.800 and 0.834. For the two near-flat models the null ran
        about 2x too exposed (0.049 / 0.059 vs 0.027 / 0.046).
      - POSITION SIZE / NOTIONAL. All-in per round trip, versus the
        models' 20%-of-balance accumulation.

    So it tests one thing -- given the same bar skeleton, was taking those
    particular entries better than taking them at random? -- and does not
    control for exposure level, turnover or size. Its output therefore
    reports the null's own realised trade count and mean exposure next to
    the model's, so a reader can see the mismatch rather than infer it.

    Args:
        model_actions: the model's own action stream (1 enter, -1 exit).
        closes: the replayed closes (unused today, kept so a future
            direction-shuffle can use them without changing the signature).
        rng: seeded RNG, so the draw is reproducible.
    """
    n = min(len(model_actions), len(closes))
    out = [0] * n
    for i in range(n):
        action = model_actions[i]
        if action > 0:
            # Randomise the DECISION, not the schedule.
            out[i] = 1 if rng.random() < 0.5 else 0
        elif action < 0:
            # An exit with no position is a no-op, so it stays an exit: it
            # closes whatever the shuffled entry opened.
            out[i] = -1
    return out


def _pinned_read_kwargs(
    window: Any,
    *,
    pair: str,
    interval: int,
    store_keys: Mapping[str, Any],
) -> dict[str, Any]:
    """``{"refresh": ..., "coverage": ...}`` for a pinned-window baselines read.

    Phase 1.5 taught ``backtest_model`` and ``train_ticker`` to skip the
    fetch leg when the window is pinned, ends at or before the store's own
    tail, and a store is configured -- all three, via
    :func:`~kraken_trading_bot.rl.data.refresh_for_window`.  This tool was
    missed: both of its reads passed no ``refresh`` at all, so they
    defaulted to ``refresh=True`` and did a live fetch plus upsert for a
    2020-2022 window the store already holds completely.  Measured 2026-10-05
    (Phase 3): the store grew 76,677 -> 76,679 bars during a diagnostics
    phase, from two baselines runs, entirely outside the replayed windows.

    Delegates to the SAME decision function rather than restating it, so
    the three consumers cannot drift.  An unpinned window, or one with no
    store behind it, still gets ``refresh=True`` -- and then no
    ``coverage``, because ``read_ohlc_dataframe(refresh=False)`` refuses a
    read it cannot verify, while ``refresh=True`` does not need it.
    """
    from kraken_trading_bot.rl.data import refresh_for_window  # noqa: PLC0415

    return refresh_for_window(
        since=getattr(window, "since", None),
        until=getattr(window, "until", None),
        is_pinned=bool(getattr(window, "is_pinned", False)),
        window_label=getattr(window, "describe", lambda: "unknown")(),
        pair=pair,
        interval=interval,
        store_setting=store_keys.get("market_data_store"),
    )


def training_seed_of(cell: Mapping[str, Any]) -> int | None:
    """The cell's TRAINING seed, or None if no layer records one.

    NOT `(cell["backtest"])["seed"]`. The matrix runs its backtest leg with
    a fixed CLI `--seed 42` for every cell, so that field is the backtest
    CLI's default and is identical across all eight Phase 2 control cells.
    The cell's own training seed lives in `params["seed"]` (mirrored in
    `train["seed"]`), and it is the number a reader needs in order to find
    and re-run that model.

    Measured 2026-10-05: the old expression printed "representative seed
    42" for mtx_0d102cc8689d, whose training seed is 48. Nothing numeric
    depends on this -- it is a label -- but a label that names the wrong
    seed sends the reader to a model they did not mean to run.
    """
    for layer in ("train", "params"):
        raw = (cell.get(layer) or {}).get("seed")
        if raw is not None:
            return int(raw)
    return None


def _timing_matched_null(
    pool: Sequence[Mapping[str, Any]],
    replay: Sequence[float],
    args: Any,
    buy_hold: float,
    rng,
) -> dict[str, Any] | None:
    """Shuffle the representative model's decisions on its own timing.

    Picks the cell whose excess sits at the MEDIAN of the friction-bearing
    arm (a representative model, not the luckiest one), reloads it through
    the real environment to capture its action stream, then replays that
    stream with each entry skipped at random.
    """
    import math as _math  # noqa: PLC0415

    scored = [
        c for c in pool
        if (c.get("backtest") or {}).get("n_bars")
    ]
    if not scored:
        return None
    med = statistics.median(
        float((c.get("backtest") or {}).get("excess_return") or 0.0)
        for c in scored
    )
    rep = min(
        scored,
        key=lambda c: abs(
            float((c.get("backtest") or {}).get("excess_return") or 0.0) - med
        ),
    )
    model_name = rep.get("model_name")
    models_root = getattr(args, "models_root", None) or "models"
    if not model_name:
        return {"error": "representative cell carries no model_name"}

    from kraken_trading_bot.rl.agent import RLAgent  # noqa: PLC0415
    from kraken_trading_bot.rl.data import (  # noqa: PLC0415
        add_derived_ohlcv_features,
    )
    from kraken_trading_bot.rl.environment import (  # noqa: PLC0415
        TradingEnvironment,
    )
    from kraken_trading_bot.rl.features import FeaturePipeline  # noqa: PLC0415
    from kraken_trading_bot.rl.registry import scan_model  # noqa: PLC0415

    record = scan_model("ETH_USD", model_name, root=models_root)
    if record.normalization_path is None:
        return {"error": f"{model_name} carries no normalization.npz"}
    pipeline = FeaturePipeline(
        windows=(record.config or {}).get("feature_windows", [1, 4, 24]),
        feature_groups=(record.config or {}).get("feature_groups")
        or ["price", "technical", "volume", "microstructure", "signals"],
    )
    pipeline.load_normalization("ETH_USD", record.normalization_path)

    from kraken_trading_bot.rl.data import read_ohlc_dataframe  # noqa: PLC0415
    from kraken_trading_bot.rl.data_window import (  # noqa: PLC0415
        evaluation_frame,
        resolve_data_window,
    )

    window = resolve_data_window(
        {"data_window": (rep.get("config_overrides") or {}).get("data_window")
         or {}}
    )
    # The signal channels MUST be passed here: the model was trained on a
    # 66-column observation that includes the funding/news/social columns,
    # and a plain read composes 52. Loading the env at the wrong width
    # raises rather than silently truncating, which is correct but means
    # this call has to mirror `backtest_model`'s read exactly.
    cfg = record.config or {}
    # The store keys travel with the model's own saved config, and they are
    # load-bearing here: this read MUST mirror `backtest_model`'s, and on a
    # store-backed matrix a read without them is a ~721-bar live fetch that
    # cannot overlap the pinned window (the RL side raises rather than
    # quietly replaying different bars than the cells did).
    # Named, never a `**` splat: `tests/test_rl_signal_config_wiring.py::
    # test_every_read_ohlc_dataframe_call_site_states_refresh_explicitly`
    # cannot prove a keyword arrived through a splat, and `refresh=` is
    # exactly the keyword this read has to state.
    store_kwargs: dict[str, Any] = {
        key: cfg[key]
        for key in ("market_data_store", "market_data_store_venue")
        if cfg.get(key)
    }
    cli_pages = (rep.get("cli_params") or {}).get("pages")
    # Same pinned-window skip as the other read in this file, and for the
    # same reason: with no `refresh=` this defaulted to True, so a
    # diagnostics run against an already-seeded store spent rate limit and
    # appended trailing bars it then discarded. `refresh_for_window` decides;
    # this call only forwards the answer, as named keywords.
    p3_pair = str(cfg.get("ticker", "ETH/USD"))
    p3_interval = int(cfg.get("ohlcv_interval_minutes", 60))
    p3_refresh = _pinned_read_kwargs(
        window, pair=p3_pair, interval=p3_interval, store_keys=store_kwargs
    )
    frame = add_derived_ohlcv_features(
        read_ohlc_dataframe(
            p3_pair,
            interval=p3_interval,
            pages=int(cli_pages) if cli_pages is not None else 6,
            extra_features_file=cfg.get("extra_features_file"),
            funding_features_file=cfg.get("funding_features_file"),
            social_features_file=cfg.get("social_features_file"),
            signal_max_age_hours=cfg.get("signal_max_age_hours"),
            signal_require_ticker=cfg.get("signal_require_ticker", True),
            refresh=bool(p3_refresh["refresh"]),
            coverage=p3_refresh.get("coverage"),
            market_data_store=store_kwargs.get("market_data_store"),
            market_data_store_venue=store_kwargs.get(
                "market_data_store_venue"
            ),
        )
    )
    env_frame = evaluation_frame(frame, window)
    fee_rate = float((rep.get("config_overrides") or {}).get("fee_rate") or 0.0)
    slip = float((rep.get("config_overrides") or {}).get("slippage") or 0.0)
    env = TradingEnvironment(
        ticker_id="ETH_USD",
        data=env_frame,
        feature_pipeline=pipeline,
        action_space=str((rep.get("config_overrides") or {}).get(
            "action_space", "discrete")),
        initial_balance=args.initial_balance,
        fee_rate=fee_rate,
        slippage=slip,
    )
    agent = RLAgent.load("ETH_USD", model_name, env, models_root=models_root)
    obs, _ = env.reset(seed=int((rep.get("backtest") or {}).get("seed") or 42))
    model_actions: list[int] = []
    closes = [float(v) for v in env_frame["close"].astype(float).tolist()]
    i = 0
    while True:
        action = agent.predict(obs, deterministic=True)
        # SB3 returns a SCALAR int for Discrete(3) and a 1-element array for
        # Box. `np.asarray(action).argmax()` on a 0-d array collapses to 0 —
        # i.e. every step read as "buy" — which silently produced a capture
        # that never held a position (measured: trades_median 1 instead of
        # ~half the model's entries). Take the scalar path first.
        if np.isscalar(action) or getattr(action, "ndim", 0) == 0:
            a = int(action)
        else:
            a = int(np.asarray(action).argmax())
        # discrete: 0 buy, 1 hold, 2 sell -> sign for the simulator
        model_actions.append(1 if a == 0 else (-1 if a == 2 else 0))
        obs, _r, term, trunc, _info = env.step(action)
        i += 1
        if term or trunc or i >= env.n_bars:
            break
    replay_n = min(len(replay), len(model_actions))
    sub_actions = model_actions[:replay_n]
    prices = list(replay[:replay_n])

    draws: list[float] = []
    trades: list[int] = []
    exposures: list[float] = []
    for _ in range(getattr(args, "draws", 200)):
        shuffled = timing_matched_shuffled(sub_actions, prices, rng)
        res = _simulate_long_only(
            prices, shuffled,
            initial_balance=args.initial_balance, fee_rate=fee_rate,
            slippage=slip, name="timing-matched-shuffled",
            buy_hold_return=buy_hold,
        )
        draws.append(res.excess_return)
        trades.append(res.num_trades)
        exposures.append(
            statistics.fmean(
                _exposure_curve(
                    prices, shuffled,
                    initial_balance=args.initial_balance, fee_rate=fee_rate,
                    slippage=slip,
                )
            )
        )
    model_abs = float((rep.get("backtest") or {}).get("total_return") or 0.0)
    model_exc = float((rep.get("backtest") or {}).get("excess_return") or 0.0)
    s = sorted(draws)

    

    # Percentile DIRECTION, stated explicitly because getting it backwards
    # inverts the entire conclusion. `frac_beaten_by_draws` = the share of
    # draws that return MORE than this model. A HIGH value means the model is
    # near the BOTTOM of the shuffled distribution (it loses to most draws);
    # 50% means its timing is indistinguishable from random.
    #
    # This field was previously called "model_percentile" with no direction
    # stated, and was reported as "the model sits at its 92nd percentile" --
    # which reads as the model BEATING the null. The number is the share of
    # draws beating the model, so 92% says the opposite. Measured 2026-10-04.
    def frac_beaten_by_draws(value: float) -> float | None:
        if not draws:
            return None
        return sum(1 for d in draws if d >= value) / len(draws)

    def percentile_of(value: float) -> float | None:
        """Share of draws the model EQUALS OR BEATS (conventional reading)."""
        if not draws:
            return None
        return sum(1 for d in draws if d <= value) / len(draws)

    # The null is one model's decision stream, but the claim under test is
    # about the POLICY across seeds. Compare the whole distribution: for each
    # seed in the friction-bearing pool, how much of the shuffled
    # distribution does it beat?
    per_seed = [
        float((c.get("backtest") or {}).get("excess_return") or 0.0) for c in scored
    ]
    seed_fracs = [f for f in (frac_beaten_by_draws(v) for v in per_seed) if f is not None]

    return {
        "representative_model": model_name,
        "representative_seed": training_seed_of(rep),
        # Kept, and named as the backtest leg's own seed, so the mislabel
        # this field used to be cannot come back unnoticed: if it ever
        # differs from `representative_seed`, that difference IS the bug.
        "backtest_seed": (rep.get("backtest") or {}).get("seed"),
        "friction": {"fee_rate": fee_rate, "slippage": slip},
        "model_excess": model_exc,
        "model_absolute_return": model_abs,
        "n_draws": len(draws),
        "mean": statistics.fmean(draws) if draws else None,
        "median": statistics.median(draws) if draws else None,
        "sd": statistics.stdev(draws) if len(draws) > 1 else 0.0,
        "p05": _quantile(s, 0.05) if s else None,
        "p95": _quantile(s, 0.95) if s else None,
        # Unambiguous, both directions:
        "frac_of_draws_beating_representative": frac_beaten_by_draws(model_exc),
        "representative_percentile_beat_by_draws": percentile_of(model_exc),
        # Whole 30-seed distribution against the same null:
        "n_seeds_compared": len(seed_fracs),
        "seed_frac_beaten_by_draws_median": (
            statistics.median(seed_fracs) if seed_fracs else None
        ),
        "seed_frac_beaten_by_draws_min": min(seed_fracs) if seed_fracs else None,
        "seed_frac_beaten_by_draws_max": max(seed_fracs) if seed_fracs else None,
        "seeds_beating_null_median_draw": sum(
            1 for v in per_seed if v >= (statistics.median(draws) if draws else 0.0)
        ),
        "trades_median": statistics.median(trades) if trades else 0,
        # What the null ACTUALLY realised, beside what the model realised,
        # so the mismatch is visible instead of inferable. See
        # `timing_matched_shuffled`'s docstring: this null does NOT match
        # exposure level, trade count or position size.
        "null_mean_exposure_median": (
            statistics.median(exposures) if exposures else None
        ),
        "model_mean_exposure": (rep.get("backtest") or {}).get("mean_exposure"),
        "model_trades": (rep.get("backtest") or {}).get("num_trades"),
        "exposure_level_matched": False,
        "trade_count_matched": False,
        "position_size_matched": False,
        "role": "secondary diagnostic; not a skill bar",
        "interpretation": (
            "SECONDARY DIAGNOSTIC, not a skill bar. Same entry and exit BARS "
            "as the representative model, each entry taken at random "
            "(p=0.5), exits kept in full. It preserves the bar skeleton and "
            "roughly the time-in-market. It does NOT match exposure level "
            "(this null is binary 0%/100% and invests all-in; the model is "
            "fractional), trade count (measured median "
            f"{statistics.median(trades) if trades else 0} vs the model's "
            f"{(rep.get('backtest') or {}).get('num_trades')}), or position "
            "size -- so the null pays almost no friction while the model "
            "pays for its whole turnover, and 'beating this null' is "
            "substantially 'not paying your own bill'. Use "
            "exposure_matched_baseline for the timing question. "
            "DIRECTION: 'frac_of_draws_beating_*' is the share of draws that "
            "return MORE than the model, so a HIGH value means the model "
            "sits near the BOTTOM of the shuffled distribution -- it loses "
            "to most draws. 50% means its timing is indistinguishable from "
            "random; above 50% means its timing is WORSE than random."
        ),
    }


def _benchmark(closes: Sequence[float], start: int, n_replayed: int
               ) -> tuple[float, float]:
    """Buy-and-hold return and drawdown over the bars actually replayed.

    Same convention as `kraken_trading_bot.rl.backtest._benchmark`, so the
    nulls and the matrix cells are measured against the same reference.
    """
    segment = list(closes[start : start + n_replayed])
    if len(segment) < 2:
        return 0.0, 0.0
    first = float(segment[0])
    if not math.isfinite(first) or first <= 0.0:
        return 0.0, 0.0
    equity = [value / first for value in segment]
    total_return = equity[-1] - 1.0
    peak = -math.inf
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return total_return, max_dd


def _select_arm(
    cells: Sequence[Mapping[str, Any]],
    fee_override: float | None,
) -> tuple[list[Mapping[str, Any]], float]:
    """Pick the cells to measure, and the fee rate to measure them at.

    Default is the friction-BEARING arm: the whole point of this tool is
    whether the policy survives realistic costs. ``fee_override`` selects a
    specific arm instead, so ``--fee 0`` produces the frictionless baseline
    set that the frictionless model row needs beside it.

    Selection is from the full cell list and never falls back silently. The
    first version filtered the already-narrowed friction-bearing pool by
    ``fee == 0``, which matched nothing, and an ``or pool`` fallback handed
    back the Kraken cells — so ``--fee 0`` printed the Kraken median in a
    frictionless-looking header. Measured 2026-10-04.
    """
    def fee_of(cell: Mapping[str, Any]) -> float:
        try:
            return float((cell.get("config_overrides") or {}).get("fee_rate"))
        except (TypeError, ValueError):
            return 0.0

    if fee_override is None:
        with_cost = [c for c in cells if fee_of(c) > 0]
        pool = with_cost or list(cells)
        return pool, max((fee_of(c) for c in pool), default=0.0)

    selected = [c for c in cells if fee_of(c) == fee_override]
    if not selected:
        available = sorted({fee_of(c) for c in cells})
        raise SystemExit(
            f"--fee {fee_override} matches no cells; available fee rates: "
            f"{available}"
        )
    return selected, fee_override


def _load_cells(results: Path) -> list[dict[str, Any]]:
    """Valid out-of-sample cells, last-wins per cell_id (the run's own rule).

    The gate is the RECOMPUTED validity, not the ``status`` recorded at
    execution time, and mixing the two was silently re-introducing the
    zero-trade exclusion this tool was written to avoid.

    Measured 2026-10-04. A cell that replayed 127 bars and took 0 trades was
    written with ``status: "invalid"`` under an earlier spec whose
    ``min_trades`` was 1. Re-classifying with ``min_trades: 0`` correctly
    returns ``valid=True`` with no ``invalid_reasons`` -- but filtering on
    ``status == "ok"`` threw those cells away anyway, so this function
    returned 28 of 30 friction-bearing cells and reported the *excluding*
    median (-1.78%) next to a *including* one (-1.55%) elsewhere. Same
    exclusion, one layer down, and invisible without counting.

    So: ``status`` is honoured only where it means there is no measurement to
    use at all (``error``, ``dry_run``). ``invalid`` is a judgement about the
    spec in force when the cell ran; ``min_trades`` is a judgement about the
    comparison being asked for now, and that is the one the caller controls.
    """
    import model_matrix as mm  # noqa: PLC0415 — optional dependency boundary

    records = mm.load_records(results)
    views = mm._build_views(records, min_bar_ratio=0.0, min_trades=0)
    return [
        v.record
        for v in views
        if v.valid and v.record.get("status") not in NO_MEASUREMENT_STATUSES
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results",
        help="matrix cells.jsonl",
    )
    parser.add_argument(
        "--spec",
        help="a matrix spec YAML; its run.results is used when --results "
             "is omitted. Reading it here rather than in the recipe keeps "
             "the recipe free of a YAML dependency it does not otherwise "
             "have (plain python3 outside the dev shell has no yaml, which "
             "silently produced an empty path).",
    )
    parser.add_argument("--draws", type=int, default=200,
                        help="random-policy draws (default 200)")
    parser.add_argument(
        "--fee",
        type=float,
        default=None,
        help=(
            "override the measured fee rate and select the arm whose cells "
            "match it. `--fee 0 --slippage 0` measures the FRICTIONLESS "
            "baseline arm against the frictionless cells, so the frictionless "
            "model row is not left without baselines beside it (default: the "
            "matrix's own friction)"
        ),
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=None,
        help="override the measured slippage (default: the matrix's own)",
    )
    parser.add_argument("--initial-balance", type=float, default=10000.0)
    parser.add_argument("--seed", type=int, default=20261004,
                        help="RNG seed, so the null distribution is "
                             "reproducible")
    parser.add_argument(
        "--models-root",
        help="registry root holding the matrix's trained models "
             "(default: read from the spec's run.models_root)",
    )
    parser.add_argument(
        "--no-timing-null",
        action="store_true",
        help="skip the timing-matched shuffled null (it reloads a model)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    results_arg = args.results
    if not results_arg and args.spec:
        import yaml  # noqa: PLC0415 — only needed on this path

        spec_path = Path(args.spec).expanduser()
        if not spec_path.is_file():
            print(f"error: no such spec: {spec_path}", file=sys.stderr)
            return 2
        loaded = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        run_block = loaded.get("run") or {}
        results_arg = run_block.get("results") or ""
        # The trained models live under the spec's own models_root, not the
        # repo's models/ — the harness points both legs at scratch space.
        # Without this the timing null reloads a model that does not exist
        # and reports "carries no normalization.npz".
        if not args.models_root and run_block.get("models_root"):
            args.models_root = str(run_block["models_root"])
    if not results_arg:
        print("error: pass --results <cells.jsonl> or --spec <matrix.yaml>",
              file=sys.stderr)
        return 2

    results = Path(results_arg).expanduser()
    if not results.is_file():
        print(f"error: no such results file: {results}", file=sys.stderr)
        return 2
    cells = _load_cells(results)
    if not cells:
        print("error: no valid cells in that results file", file=sys.stderr)
        return 1

    # The friction level to characterise: the realistic one if present,
    # because "is this distinguishable from chance" is a question about the
    # arm someone would actually trade. --fee overrides it.
    pool, fee_rate = _select_arm(cells, args.fee)
    slip_rate = (
        float(args.slippage)
        if args.slippage is not None
        else max(
            float((c.get("config_overrides") or {}).get("slippage") or 0.0)
            for c in pool
        )
    )
    n_bars = int((pool[0].get("backtest") or {}).get("n_bars") or 0)
    model_excess = [
        float((c.get("backtest") or {}).get("excess_return") or 0.0) for c in pool
    ]
    model_trades = [
        int((c.get("backtest") or {}).get("num_trades") or 0) for c in pool
    ]
    median_model_trades = int(statistics.median(model_trades)) if model_trades else 0

    # The bars themselves come from the matrix cells' own frames; the report
    # carries a summary, not prices, so re-fetch once through the RL reader
    # using the same pinned window the cells used.
    from kraken_trading_bot.rl.data import read_ohlc_dataframe  # noqa: PLC0415
    from kraken_trading_bot.rl.data_window import (  # noqa: PLC0415
        evaluation_frame,
        resolve_data_window,
    )

    window_cfg = (pool[0].get("config_overrides") or {}).get("data_window") or {}
    window = resolve_data_window({"data_window": window_cfg})

    # The bars must come from the SAME source the cells replayed, or the
    # comparison is against a different experiment. A hardcoded
    # `read_ohlc_dataframe("ETH/USD", interval=60, pages=6)` is a LIVE
    # fetch: Kraken's REST ceiling is ~721 recent bars, so on a store-backed
    # matrix the pinned window lands entirely outside them and
    # `evaluation_frame` raises PinnedWindowUnavailableError rather than
    # quietly benchmarking the wrong bars.
    #
    # `config_overrides` holds only the AXIS merge, so the store keys live
    # in the base config, not there. The cell's own
    # `backtest_config_path` is the fully-merged config the backtest leg
    # actually ran with — the correct source of truth — and it carries
    # `market_data_store`/`market_data_store_venue` because both are in
    # BACKTEST_EXPRESSIBLE_KEYS. Falls back to the previous hardcoded call
    # when the path is absent (an older results file).
    pair = "ETH/USD"
    interval = 60
    pages = 6
    store_keys: dict[str, Any] = {}
    cell_cfg_path = pool[0].get("backtest_config_path")
    if cell_cfg_path and Path(cell_cfg_path).is_file():
        import yaml  # noqa: PLC0415 — lazy, as on the --spec path above

        cell_cfg = yaml.safe_load(Path(cell_cfg_path).read_text(encoding="utf-8")) or {}
        pair = str(cell_cfg.get("ticker") or pair)
        interval = int(cell_cfg.get("ohlcv_interval_minutes") or interval)
        for key in ("market_data_store", "market_data_store_venue"):
            if cell_cfg.get(key):
                store_keys[key] = cell_cfg[key]
    cli_pages = (pool[0].get("cli_params") or {}).get("pages")
    if cli_pages is not None:
        pages = int(cli_pages)

    # The window is pinned, so ask `refresh_for_window` -- the same decision
    # `backtest_model` and `train_ticker` make -- whether the fetch leg can
    # be skipped, and pass the answer as NAMED keywords. Never a `**` splat:
    # `tests/test_rl_signal_config_wiring.py::
    # test_every_read_ohlc_dataframe_call_site_forwards_venue` refuses to
    # prove a splatted call forwards anything, and it is right not to.
    refresh_kwargs = _pinned_read_kwargs(
        window, pair=pair, interval=interval, store_keys=store_keys
    )
    frame = read_ohlc_dataframe(
        pair,
        interval=interval,
        pages=pages,
        refresh=bool(refresh_kwargs["refresh"]),
        coverage=refresh_kwargs.get("coverage"),
        market_data_store=store_keys.get("market_data_store"),
        market_data_store_venue=store_keys.get("market_data_store_venue"),
    )
    eval_frame = evaluation_frame(frame, window)
    closes_all = [float(v) for v in eval_frame["close"].astype(float).tolist()]
    # Match the cells' realized replay length, offset past the warm-up the
    # environment skips.
    warmup = 24
    replay = closes_all[warmup : warmup + n_bars] if n_bars else closes_all
    buy_hold, buy_hold_dd = _benchmark(replay, 0, len(replay))

    out: dict[str, Any] = {
        "source_results": str(results),
        "fee_rate": fee_rate,
        "slippage": slip_rate,
        "n_bars": len(replay),
        "buy_hold_return": buy_hold,
        "buy_hold_max_drawdown": buy_hold_dd,
        "model_excess_median": statistics.median(model_excess)
        if model_excess
        else None,
        "model_trades_median": median_model_trades,
        "model_cells": len(pool),
        "baselines": {},
    }

    out["baselines"]["flat"] = _simulate_long_only(
        replay, flat_policy(len(replay)),
        initial_balance=args.initial_balance, fee_rate=fee_rate,
        slippage=slip_rate, name="flat (never trade)",
        buy_hold_return=buy_hold,
    ).to_dict()

    out["baselines"]["momentum"] = _simulate_long_only(
        replay, momentum_policy(replay),
        initial_balance=args.initial_balance, fee_rate=fee_rate,
        slippage=slip_rate, name="momentum (6/24 SMA crossover)",
        buy_hold_return=buy_hold,
    ).to_dict()

    out["baselines"]["momentum_stateful"] = _simulate_long_only(
        replay, momentum_stateful_policy(replay),
        initial_balance=args.initial_balance, fee_rate=fee_rate,
        slippage=slip_rate, name="momentum-stateful (3/12 SMA, re-entering)",
        buy_hold_return=buy_hold,
    ).to_dict()

    import random as _random  # noqa: PLC0415

    rng = _random.Random(args.seed)
    draws = RandomDraws(matched_trades=median_model_trades)
    realised_trades: list[int] = []
    for _ in range(args.draws):
        actions = random_policy_matched(len(replay), median_model_trades, rng)
        res = _simulate_long_only(
            replay, actions,
            initial_balance=args.initial_balance, fee_rate=fee_rate,
            slippage=slip_rate, name="random",
            buy_hold_return=buy_hold,
        )
        draws.draws.append(res.excess_return)
        realised_trades.append(res.num_trades)
    summary = draws.summary()
    summary["trades_median"] = (
        statistics.median(realised_trades) if realised_trades else 0
    )
    summary["trades_min"] = min(realised_trades) if realised_trades else 0
    summary["trades_max"] = max(realised_trades) if realised_trades else 0
    summary["friction"] = {"fee_rate": fee_rate, "slippage": slip_rate}

    # ── the STRONGER null: the model's own timing, decisions shuffled ──────
    #
    # The count-matched random above fixes turnover but still lets the draw
    # choose *when* to trade, so part of what it measures is "trading this
    # much loses money" — which the cost model already says. Replaying the
    # model's own entries and exits and shuffling only the enter/skip
    # decision removes that: same bars, same holding periods, same exposure,
    # and the only thing left to be wrong is the model's timing.
    timing = None
    if not args.no_timing_null:
        try:
            timing = _timing_matched_null(
                pool, replay, args, buy_hold, rng,
            )
        except Exception as exc:  # noqa: BLE001 — reported, never fatal
            timing = {"error": f"{type(exc).__name__}: {exc}"}
    out["baselines"]["timing_matched_shuffled"] = timing

    # ── PRIMARY comparison: exposure-matched, per cell ────────────────────
    #
    # Each cell gets its OWN x (its own realised mean exposure), because x
    # is a property of that policy, not of the run. alpha_vs_x is the
    # headline: it cannot be won by de-risking or by beta.
    #
    # Cells whose backtest record predates the mean_exposure field have no
    # x to match on. They are reported as unavailable rather than silently
    # dropped or given a pooled x, which would compare a policy against
    # somebody else's exposure.
    exposure_rows: list[dict[str, Any]] = []
    missing_x = 0
    for cell in pool:
        bt = cell.get("backtest") or {}
        x = bt.get("mean_exposure")
        model_total = bt.get("total_return")
        if x is None or model_total is None:
            missing_x += 1
            continue
        x = float(x)
        base = exposure_matched_baseline(
            replay, x,
            initial_balance=args.initial_balance, fee_rate=fee_rate,
            slippage=slip_rate, buy_hold_return=buy_hold,
        )
        notional = bt.get("total_notional")
        # Measured cost/notional is 0.0031 = fee + slippage, so that is the
        # per-unit rate; applied to the model's realised notional.
        friction = (
            float(notional) * (fee_rate + slip_rate) / args.initial_balance
            if notional is not None
            else 0.0
        )
        alpha = float(model_total) - base["total_return"]
        exposure_rows.append({
            "cell_id": cell.get("cell_id"),
            "model_name": cell.get("model_name"),
            "seed": bt.get("seed"),
            "x": x,
            "x_matched_total_return": base["total_return"],
            "x_matched_mean_exposure": base["mean_exposure"],
            "model_total_return": float(model_total),
            "alpha_vs_x": alpha,
            "alpha_vs_x_adj": alpha + friction,
            "model_friction_as_fraction_of_initial": friction,
            "model_mean_exposure_field": x,
        })

    alphas = [r["alpha_vs_x"] for r in exposure_rows]
    alphas_adj = [r["alpha_vs_x_adj"] for r in exposure_rows]
    out["baselines"]["exposure_matched"] = {
        "role": "PRIMARY verdict metric",
        "x_source": (
            "each cell's own realised ex-post mean exposure from its backtest "
            "record; a DECOMPOSITION, not an achievable strategy"
        ),
        "alpha_vs_x_definition": (
            "model total_return minus the x-matched baseline's total_return. "
            "Exposure is matched by construction, so it isolates the "
            "SELECTION of when to be exposed from how exposed the policy was."
        ),
        "alpha_vs_x_adj_definition": (
            "alpha_vs_x plus the model's own realised friction "
            "(total_notional * (fee_rate + slippage)), because the model "
            "pays for hundreds of fills while the baseline pays for one. "
            "Diagnostic only."
        ),
        "n_cells": len(exposure_rows),
        "n_cells_without_mean_exposure": missing_x,
        "per_cell": exposure_rows,
        "alpha_vs_x_median": statistics.median(alphas) if alphas else None,
        "alpha_vs_x_adj_median": (
            statistics.median(alphas_adj) if alphas_adj else None
        ),
        "alpha_vs_x_n_positive": sum(1 for a in alphas if a > 0.0),
        "alpha_vs_x_adj_n_positive": sum(1 for a in alphas_adj if a > 0.0),
        "note": (
            "An older cells.jsonl has no mean_exposure and cannot be scored "
            "here; re-run those cells (or train fresh) to populate it."
        ),
    }

    summary["model_median_percentile"] = (
        sum(1 for d in draws.draws if d <= (out["model_excess_median"] or 0.0))
        / len(draws.draws)
        if draws.draws and out["model_excess_median"] is not None
        else None
    )
    out["baselines"]["random"] = summary

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print("NULL-POLICY BASELINES")
    print(f"  source: {results}")
    arm = "frictionless" if fee_rate == 0 and slip_rate == 0 else "Kraken-cost"
    print(f"  arm: {arm} — fee {fee_rate}, slippage {slip_rate}")
    print(f"  ALL rows below, and every model figure in this output, are at "
          f"that friction. For the other arm pass --fee/--slippage "
          f"(e.g. --fee 0 --slippage 0).")
    print(f"  bars replayed: {out['n_bars']}   "
          f"buy&hold: {buy_hold:+.2%} absolute, 0.00% excess by definition")
    print(f"  model median excess: {out['model_excess_median']:+.2%}"
          f"   median trades: {median_model_trades}")
    print()
    print("  policy                      excess    absolute     trades")
    for key in ("flat", "momentum", "momentum_stateful"):
        b = out["baselines"][key]
        print(f"  {b['policy']:<24s} {b['excess_return']:+9.2%} "
              f"{b['total_return']:+10.2%} {b['num_trades']:>8d}")
    r = out["baselines"]["random"]
    print(f"  random (count-matched)      {r['mean']:+9.2%}        —     "
          f"{r['matched_trades']:>8d}")
    print(f"    n={r['n_draws']}  sd {r['sd']:.2%}  "
          f"5th-95th pct [{r['p05']:+.2%}, {r['p95']:+.2%}]")
    print(f"    realised trades per draw: median {r['trades_median']:.0f} "
          f"(range {r['trades_min']}-{r['trades_max']}), "
          f"target {r['matched_trades']}")
    print(f"    this null fixes TRADE COUNT but lets each draw choose WHEN, so "
          f"it partly measures 'trading this much loses money' — which the "
          f"cost result already says.")

    e = out["baselines"].get("exposure_matched") or {}
    print()
    print("  EXPOSURE-MATCHED BASELINE — PRIMARY verdict metric")
    print("    one entry fill for fraction x of equity, then hold; x is each")
    print("    cell's OWN realised mean exposure, so this is a DECOMPOSITION")
    print("    (a decomposition of the model's own exposure, not a strategy")
    print("    anyone could have run), and alpha_vs_x isolates the CHOICE of")
    print("    when to be exposed from how exposed the policy was.")
    if e.get("n_cells"):
        print(f"    {'seed':>5} {'x':>7} {'x-matched':>11} {'model':>9} "
              f"{'alpha_vs_x':>12} {'adj':>9} {'friction':>10}")
        for row in e["per_cell"]:
            print(f"    {str(row['seed']):>5} {row['x']:>7.4f} "
                  f"{row['x_matched_total_return']:>+10.2%} "
                  f"{row['model_total_return']:>+8.2%} "
                  f"{row['alpha_vs_x']:>+11.2%} {row['alpha_vs_x_adj']:>+8.2%} "
                  f"{row['model_friction_as_fraction_of_initial']:>+9.2%}")
        print(f"    MEDIAN alpha_vs_x     {e['alpha_vs_x_median']:+.2%}"
              f"   ({e['alpha_vs_x_n_positive']}/{e['n_cells']} cells positive)"
              f"   <- PRIMARY")
        print(f"    MEDIAN alpha_vs_x_adj {e['alpha_vs_x_adj_median']:+.2%}"
              f"   ({e['alpha_vs_x_adj_n_positive']}/{e['n_cells']} cells positive)"
              f"   <- diagnostic (adds back the model's own friction)")
    else:
        print("    no cell carried a mean_exposure, so no alpha could be computed.")
    if e.get("n_cells_without_mean_exposure"):
        print(f"    NOTE: {e['n_cells_without_mean_exposure']} cell(s) predate the "
              f"mean_exposure field and were NOT scored. Re-run them to populate it.")

    t = out["baselines"].get("timing_matched_shuffled")
    if not t:
        print()
        print("  TIMING-MATCHED SHUFFLED NULL: not run "
              f"({(t or {}).get('error', 'disabled with --no-timing-null')})")
    elif t.get("error"):
        print()
        print(f"  TIMING-MATCHED SHUFFLED NULL: unavailable — {t['error']}")
    else:
        print()
        print("  TIMING-MATCHED SHUFFLED NULL — SECONDARY diagnostic, not a "
              "skill bar")
        print("    same entry/exit BARS as the model, each entry taken at "
              "random (p=0.5),")
        print("    exits kept in full. It does NOT match exposure level, trade "
              "count or size:")
        print(f"      trades  model {t.get('model_trades')}  vs  null median "
              f"{t['trades_median']}")
        print(f"      exposure  model {t.get('model_mean_exposure')}  vs  null "
              f"median {t.get('null_mean_exposure_median')}  "
              f"(the null is binary 0/100% and invests all-in)")
        print(f"    so the null pays almost no friction while the model pays for "
              f"its whole")
        print(f"    turnover: 'beating this null' is substantially 'not paying "
              f"your own bill'.")
        print(f"    shuffled excess: mean {t['mean']:+.2%}  median "
              f"{t['median']:+.2%}  sd {t['sd']:.2%}  n={t['n_draws']}")
        print(f"    shuffled 5th-95th pct [{t['p05']:+.2%}, {t['p95']:+.2%}]")
        print(f"    representative seed {t['representative_seed']} "
              f"(model {t['representative_model']}):")
        print(f"      absolute {t['model_absolute_return']:+.2%}   excess "
              f"{t['model_excess']:+.2%}")
        rep_f = t["frac_of_draws_beating_representative"]
        print(f"      BEATEN BY {rep_f:.0%} of the shuffled draws"
              f"   <- HIGH = model near the BOTTOM of this distribution")
        if t.get("seed_frac_beaten_by_draws_median") is not None:
            print(f"    across all {t['n_seeds_compared']} seeds: beaten by a "
                  f"median {t['seed_frac_beaten_by_draws_median']:.0%} of draws "
                  f"(range {t['seed_frac_beaten_by_draws_min']:.0%}-"
                  f"{t['seed_frac_beaten_by_draws_max']:.0%})")
            print(f"    seeds whose excess beat the shuffled median: "
                  f"{t['seeds_beating_null_median_draw']}/{t['n_seeds_compared']}")
        print("    DIRECTION: these fractions are the share of DRAWS THAT BEAT "
              "THE MODEL.")
        print("    50% would mean timing indistinguishable from random; above "
              "50% means timing is WORSE than random.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())