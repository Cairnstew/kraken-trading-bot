"""``_fill_price`` must charge the environment's cost model, both ways.

The null baselines are compared against models replayed through
``TradingEnvironment._execute``, which charges:

    buy  -> price * (1 + slippage)   # pay MORE
    sell -> price * (1 - slippage)   # receive LESS

``tools/null_baselines.py`` shipped the two the wrong way round, and its
call sites pass ``+1`` to ENTER and ``-1`` to EXIT -- so every long-only
null got a cheap entry and a rich exit, i.e. the nulls were credited
roughly +0.001 per round trip against the models they are compared with.
That bias flatters the null, and on a flat window it is the same order as
the differences being measured.

The direction is asserted against the environment's own source rather
than against a restatement of it, so the two cannot drift apart again.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import null_baselines as nb  # noqa: E402


def test_a_buy_pays_more_and_a_sell_receives_less() -> None:
    """The two directions, which is the whole content of the fix."""
    price, slip = 100.0, 0.005

    assert nb._fill_price(price, +1, slip) == pytest.approx(100.5)
    assert nb._fill_price(price, -1, slip) == pytest.approx(99.5)


def test_a_round_trip_costs_money_rather_than_making_it() -> None:
    """Enter then exit at the same price must LOSE the round trip.

    The old direction made this *gain* about +0.1% before fees, which is
    the clearest single statement of why the bug flattered the nulls.
    """
    price, slip = 100.0, 0.005
    entry = nb._fill_price(price, +1, slip)
    exit_ = nb._fill_price(price, -1, slip)

    assert exit_ < entry
    assert exit_ / entry - 1.0 < 0.0


def test_zero_slippage_is_the_identity_on_both_sides() -> None:
    """Frictionless means unchanged, not mirrored."""
    assert nb._fill_price(100.0, +1, 0.0) == 100.0
    assert nb._fill_price(100.0, -1, 0.0) == 100.0


def test_the_environment_charges_the_same_two_directions() -> None:
    """Pin the cost model this tool claims to mirror, at the source.

    Parsed out of ``TradingEnvironment._execute`` rather than restated, so
    if the environment ever changes which side pays up, this fails instead
    of the two drifting apart in silence.
    """
    from kraken_trading_bot.rl.environment import TradingEnvironment

    source = inspect.getsource(TradingEnvironment._execute)
    fills = re.findall(r"fill\s*=\s*price\s*\*\s*\(1(?:\.0+)?\s*([+-])\s*self\.slippage\)", source)
    assert fills, (
        "could not find the fill-price expressions in _execute; the cost "
        "model moved and this pin must be rewritten to match"
    )
    # First fill is the buy (pay up), second is the sell (receive less).
    assert fills[0] == "+", "the environment must charge MORE on a buy"
    assert fills[1] == "-", "the environment must charge LESS on a sell"


def test_a_null_that_trades_pays_costs_not_collects_them() -> None:
    """End to end: a null that enters AND exits pays both sides.

    Buy on the flat stretch, sell into the jump.  Both fills are exercised
    -- a buy-and-hold null would only ever price the entry, so a sell that
    wrongly charged *up* would slip through.  Under the old direction this
    same script netted roughly +0.1% before fees: the null was collecting
    the spread instead of paying it.
    """
    closes = [100.0] * 40 + [110.0] * 40  # flat then a jump
    # action > 0 buys, < 0 sells, and 0 does nothing -- so this enters on
    # bar 0, holds through the flat stretch and the jump, and exits on the
    # last bar: one entry fill AND one exit fill, both exercised.
    actions = [1] + [0] * 78 + [-1]

    base = dict(
        closes=closes,
        actions=actions,
        initial_balance=10_000.0,
        fee_rate=0.0,
        slippage=0.005,
        name="probe",
        buy_hold_return=0.10,
    )
    with_cost = nb._simulate_long_only(**base)
    free = nb._simulate_long_only(**{**base, "slippage": 0.0})

    assert with_cost.total_return < free.total_return, (
        "a null must pay costs, not collect them"
    )


def test_the_exposure_matched_baseline_was_already_correct() -> None:
    """``exposure_matched_baseline`` builds its own fill, correctly.

    It was written to the environment's convention from the start, which is
    why ``alpha_vs_x`` was never built on the inverted baseline.  Pinned so
    a future "simplification" that routes it through ``_fill_price`` is
    caught only if it also inverts.
    """
    source = inspect.getsource(nb.exposure_matched_baseline)
    assert "(1.0 + slippage)" in source, (
        "the x-matched buy must pay UP, like the environment"
    )
    # It is allowed to route through _fill_price now that the two agree;
    # what it must never do is disagree with the environment again.
    if "_fill_price(" in source:
        assert "(1.0 + slippage)" in inspect.getsource(nb._fill_price)