"""The cost basis, and what each fill closes against it.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_fill_pnl.py -q

`fill_pnl` is pure arithmetic — no Nautilus, no database, no bars — so every case here is
a few floats and the whole file runs in milliseconds. That is deliberate: this is the
module the trade statistics on the board are built out of, and the failure it replaced was
invisible precisely because nothing anywhere asserted what a "closed trade" is.

**The distinction under test is None versus 0.0.** A fill that opened or added closed
nothing and realises None; a fill that closed exactly at cost realises 0.0 and IS a closed
trade. The board counts closed trades off that null. It used to count them off
`book_pnl != 0` — the whole book's mark at the fill — which made an opening buy a closed
trade whenever some unrelated name had moved, and reported 23 closed trades and a 13% win
rate for eight round trips that had every one of them made money.
"""

from __future__ import annotations

import pytest

import fill_pnl


# --------------------------------------------------------------- opening and adding
def test_opening_from_flat_realises_nothing_and_sets_the_basis():
    realised, cost = fill_pnl.apply_fill(0.0, None, 10.0, 100.0)
    assert realised is None, "an opening buy closed nothing — not zero, nothing"
    assert cost == 100.0


def test_adding_averages_the_basis_and_still_realises_nothing():
    realised, cost = fill_pnl.apply_fill(10.0, 100.0, 30.0, 120.0)
    assert realised is None
    assert cost == pytest.approx(115.0), "(10x100 + 30x120) / 40"


def test_adding_to_a_short_averages_the_same_way():
    realised, cost = fill_pnl.apply_fill(-10.0, 100.0, -10.0, 90.0)
    assert realised is None
    assert cost == pytest.approx(95.0)


# --------------------------------------------------------------- closing
def test_closing_a_long_realises_against_the_average_cost():
    realised, cost = fill_pnl.apply_fill(10.0, 100.0, -10.0, 110.0)
    assert realised == pytest.approx(100.0)
    assert cost is None, "flat again, so there is no basis to carry"


def test_closing_a_short_realises_the_other_way():
    realised, cost = fill_pnl.apply_fill(-10.0, 100.0, 10.0, 90.0)
    assert realised == pytest.approx(100.0), "a short that bought back cheaper made money"
    assert cost is None


def test_a_partial_close_prices_only_the_part_sold_and_keeps_the_basis():
    realised, cost = fill_pnl.apply_fill(10.0, 100.0, -4.0, 110.0)
    assert realised == pytest.approx(40.0)
    assert cost == 100.0, "the six still held cost what they always cost"


def test_closing_at_cost_realises_zero_and_is_still_a_closed_trade():
    realised, cost = fill_pnl.apply_fill(10.0, 100.0, -10.0, 100.0)
    assert realised == 0.0
    assert realised is not None, "0.0 is an answer; None is the absence of one"
    assert cost is None


def test_a_reversal_books_the_old_position_and_opens_the_rest_here():
    # Long 10 at 100, sell 25 at 110: the 10 close for +100 and 15 short open at 110.
    realised, cost = fill_pnl.apply_fill(10.0, 100.0, -25.0, 110.0)
    assert realised == pytest.approx(100.0), "only the 10 that existed can be closed"
    assert cost == 110.0, "the new short's basis is this price, not the old long's"


# --------------------------------------------------------------- degenerate input
def test_a_zero_size_fill_changes_nothing():
    assert fill_pnl.apply_fill(10.0, 100.0, 0.0, 999.0) == (None, 100.0)


def test_a_position_with_no_basis_realises_zero_rather_than_guessing():
    """Defensive: a close against a missing basis prices at the fill, so the trade shows
    as a scratch instead of inventing a P&L out of a None."""
    realised, _ = fill_pnl.apply_fill(10.0, None, -10.0, 110.0)
    assert realised == 0.0


# --------------------------------------------------------------- the replay
def test_replay_reproduces_the_ibs_round_trip_that_was_reported_as_a_loss():
    """AMD on `00:us_stocks-1d-ibs`: bought 2 at 483.01, sold 2 at 514.37.

    A +$62.72 round trip. The board printed it as **-$59.33** because it was showing the
    book's mark at that instant under the heading "Realised P&L". This is the exact case
    the whole change exists for, so it is pinned with the real numbers.
    """
    out = fill_pnl.replay([("BUY", 2.0, 483.01), ("SELL", 2.0, 514.37)])
    assert out[0] is None, "the opening buy closed nothing"
    assert out[1] == pytest.approx(62.72)


def test_replay_averages_a_name_bought_twice_before_it_sold():
    """IWM on `00:us_etfs-1d-ibs`: two buys of 65 at 303.50 across a restart, then a sell
    of 65 at 305.08. Only 65 close, at the average of what was held — +$102.70."""
    out = fill_pnl.replay([("BUY", 65.0, 303.5), ("BUY", 65.0, 303.5),
                           ("SELL", 65.0, 305.08)])
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(102.7)


def test_replay_is_case_insensitive_about_the_side():
    assert fill_pnl.replay([("buy", 1.0, 10.0), ("sell", 1.0, 12.0)])[1] == pytest.approx(2.0)
