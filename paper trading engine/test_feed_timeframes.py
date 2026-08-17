"""Every timeframe the desk OFFERS is a timeframe it can actually subscribe to.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_feed_timeframes.py -q

This is the regression test for a silent failure that cost fifteen hours of a forward
test. `td_nautilus.timeframe_of` had two hardcoded branches, `1d` and `4h`, while
`paper_config.MEMBER_TIMEFRAMES` offered six and `/v1/limits` advertised all six. A member
registering at `5m` therefore got:

    201 from the API              the timeframe IS in MEMBER_TIMEFRAMES
    the strategy attached         DeskController accepted it for the same reason
    "RUNNING" in the log          Nautilus started it happily
    state = live in the console   nothing had raised where anyone could see
    ValueError, in a task         `_subscribe_bars` failed, logged ERROR, went nowhere

...and then every order that strategy ever sent was refused with *"no price for BTC/USD
yet — try again after the next 5m close"*, which is advice that cannot come true, because
no 5m close was ever going to be delivered to it.

The check that was supposed to prevent exactly this compared `MEMBER_TIMEFRAMES` against
`BAR_SPEC` — which is derived from the BACKTEST engine's timeframe list and says nothing
whatever about what the live vendor client implements. Two lists that both look
authoritative, neither of which was the capability.

So: assert against the feed, and assert the round trip, not just the membership. A
timeframe that maps to the WRONG key polls on the wrong cadence, which is a quieter bug
than the one this file is named for.
"""

from __future__ import annotations

import pytest

import paper_config                                                     # noqa: F401
import td_live
import td_nautilus

from nautilus_trader.model.data import BarType


def _bar_type(tf: str, symbol: str = "BTCUSD.BINANCE") -> BarType:
    return BarType.from_str(f"{symbol}-{paper_config.BAR_SPEC[tf]}")


@pytest.mark.parametrize("tf", paper_config.MEMBER_TIMEFRAMES)
def test_every_offered_timeframe_can_be_fed(tf):
    """The vendor client knows how to ask for it."""
    assert tf in td_live.INTERVALS, (
        f"{tf} is offered to members but td_live cannot fetch it")


@pytest.mark.parametrize("tf", paper_config.MEMBER_TIMEFRAMES)
def test_every_offered_timeframe_round_trips(tf):
    """tf -> Nautilus bar spec -> tf, unchanged.

    The direction that matters is the second one: `_poll` calls `timeframe_of` on the bar
    type it was handed and uses the answer to pick both the vendor interval and the sleep
    to the next close. A spec that maps to a different key polls on the wrong cadence and
    fetches the wrong series, with nothing malformed anywhere to notice.
    """
    assert td_nautilus.timeframe_of(_bar_type(tf)) == tf


@pytest.mark.parametrize("tf", paper_config.BOOK_TIMEFRAMES)
def test_book_timeframes_are_feedable_too(tf):
    """The house's own books run on the same client and must not be forgotten."""
    assert td_nautilus.timeframe_of(_bar_type(tf)) == tf


def test_an_unsupported_spec_still_raises():
    """The mapping widened; it did not become permissive.

    `3m` is a spec Nautilus will happily parse and the vendor does not serve, so it must
    still fail loudly rather than fall through to a neighbouring interval.
    """
    with pytest.raises(ValueError):
        td_nautilus.timeframe_of(BarType.from_str("BTCUSD.BINANCE-3-MINUTE-LAST-EXTERNAL"))


def test_equities_map_the_same_way():
    """The venue is not part of the timeframe, and a regression that made it so would
    show up only on one asset class."""
    for tf in paper_config.MEMBER_TIMEFRAMES:
        assert td_nautilus.timeframe_of(_bar_type(tf, "QQQ.SANDBOX")) == tf
