"""`engines/vector.py` — the conventions every other engine is required to match.

`parity.py` checks that three engines agree on a number. It cannot check that the number
means what the project thinks it means, because all three would have to be wrong in the
same way for it to notice — and two of them were written from the same conventions. These
tests state the conventions directly, on hand-computable inputs:

* a signal on bar *t* trades bar *t+1*, never bar *t*;
* cost is charged on |change in position|, so a reversal costs two sides;
* positions re-size to the target FRACTION every bar (the documented 0.9^4 case);
* net returns are clipped at -0.999 before compounding;
* annualisation is measured from the index, never a constant;
* the baseline is never flattened.

Departing from any one of them makes results incomparable with everything already learned,
so each gets a test that fails loudly rather than a comment that can rot.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from engines import vector

FREE = {"commission_bps": 0.0, "half_spread_bps": 0.0,
        "sell_fee_bps": 0.0, "borrow_annual": 0.0}


def fee(**kw) -> dict:
    return {**FREE, **kw}


# ------------------------------------------------------------------ bars_per_year

def test_bars_per_year_is_measured_from_the_index():
    daily = pd.date_range("2020-01-01", periods=3653, freq="D")     # ten calendar years
    assert vector.bars_per_year(daily) == pytest.approx(365.25, rel=1e-3)


def test_bars_per_year_counts_bars_over_a_first_to_last_span():
    """A fencepost worth stating: `n` bars span `n-1` intervals, so a short series reads
    slightly high. It washes out over a real sheet and is exact for the ratio work the
    annualisation actually does."""
    daily = pd.date_range("2020-01-01", periods=366, freq="D")      # 365 days of span
    assert vector.bars_per_year(daily) == pytest.approx(366 * 365.25 / 365, rel=1e-9)


def test_bars_per_year_sees_a_thinner_grid_as_more_bars():
    hourly = pd.date_range("2020-01-01", periods=24 * 30, freq="h")
    daily = pd.date_range("2020-01-01", periods=30, freq="D")
    assert vector.bars_per_year(hourly) == pytest.approx(
        vector.bars_per_year(daily) * 24, rel=0.05)


def test_bars_per_year_is_nan_on_a_zero_span():
    idx = pd.DatetimeIndex(["2020-01-01", "2020-01-01"])
    assert np.isnan(vector.bars_per_year(idx))


# -------------------------------------------------------------------- the one-bar lag

def test_the_first_bar_earns_nothing():
    """There is no prior bar to have earned a return over."""
    close = np.array([100.0, 110.0, 121.0])
    net = vector.net_returns(np.ones(3), close, FREE)
    assert net[0] == 0.0


def test_a_signal_on_bar_t_trades_bar_t_plus_1():
    """The single most important convention here. A rule that goes long on the bar its
    signal fires would be reading its own future."""
    close = np.array([100.0, 100.0, 110.0, 110.0])
    pos = np.array([0.0, 1.0, 0.0, 0.0])       # long set at bar 1, held into bar 2
    net = vector.net_returns(pos, close, FREE)
    assert net[1] == pytest.approx(0.0)        # bar 1's return earned while flat
    assert net[2] == pytest.approx(0.10)       # bar 2's +10% collected by bar 1's signal
    assert net[3] == pytest.approx(0.0)


def test_the_last_signal_never_trades():
    """A position set on the final bar has no bar to be held into."""
    close = np.array([100.0, 101.0, 102.0])
    flat = vector.net_returns(np.array([0.0, 0.0, 0.0]), close, FREE)
    late = vector.net_returns(np.array([0.0, 0.0, 1.0]), close, FREE)
    np.testing.assert_allclose(late - flat, [0.0, 0.0, 0.0])


def test_a_short_earns_the_negative_of_the_move():
    close = np.array([100.0, 90.0])
    net = vector.net_returns(np.array([-1.0, -1.0]), close, FREE)
    assert net[1] == pytest.approx(0.10)


# --------------------------------------------------------------- constant fraction

def test_positions_resize_to_the_target_fraction_every_bar():
    """The documented case, and the one any new engine must match or parity fails:
    a static -1 short through four +10% bars ends at 0.536x if share count is held
    constant, and at 0.9^4 = 0.6561x if the *fraction* is re-sized each bar."""
    close = 100.0 * 1.10 ** np.arange(6)
    pos = np.full(6, -1.0)
    net = vector.net_returns(pos, close, FREE)
    equity = vector.equity_curve(net)
    # Bars 2..5 are held short (bar 1's return is earned flat-to-short at bar 0's signal).
    assert equity[-1] == pytest.approx(0.9 ** 5, rel=1e-12)
    assert equity[-1] > 0.536                  # emphatically NOT the static-share answer


def test_equity_curve_starts_at_one_times_the_first_return():
    net = np.array([0.0, 0.10, -0.10])
    np.testing.assert_allclose(vector.equity_curve(net), [1.0, 1.10, 0.99])


# ---------------------------------------------------------------------------- cost

def test_bar_zero_is_charged_for_entering_the_opening_position():
    """Dropping it would hand every rule one free side, which matters most on the
    low-turnover rules that survive the cost gate longest."""
    close = np.array([100.0, 100.0, 100.0])
    net = vector.net_returns(np.ones(3), close, fee(commission_bps=10.0))
    assert net[0] == pytest.approx(-0.001)     # 10bps on a full unit of position
    assert net[1] == pytest.approx(0.0)        # no change, no charge
    assert net[2] == pytest.approx(0.0)


def test_cost_is_charged_on_the_change_in_position():
    close = np.full(4, 100.0)
    pos = np.array([0.0, 1.0, 1.0, 0.0])
    net = vector.net_returns(pos, close, fee(commission_bps=10.0))
    np.testing.assert_allclose(net, [0.0, -0.001, 0.0, -0.001], atol=1e-15)


def test_a_reversal_costs_two_sides():
    """flat->long costs one side; long->short costs two."""
    close = np.full(3, 100.0)
    one = vector.net_returns(np.array([1.0, 1.0, 1.0]), close, fee(commission_bps=10.0))
    two = vector.net_returns(np.array([1.0, -1.0, -1.0]), close, fee(commission_bps=10.0))
    assert one[1] == pytest.approx(0.0)
    assert two[1] == pytest.approx(-0.002)     # |−1 − 1| = 2 units


def test_half_spread_adds_to_commission_on_both_sides():
    close = np.full(2, 100.0)
    net = vector.net_returns(np.ones(2), close,
                             fee(commission_bps=5.0, half_spread_bps=15.0))
    assert net[0] == pytest.approx(-0.002)


def test_sell_fees_are_charged_on_sales_only():
    """US regulatory fees (SEC Section 31, FINRA TAF) are levied on sales, never buys."""
    close = np.full(3, 100.0)
    pos = np.array([1.0, 0.0, 1.0])            # buy, sell, buy
    net = vector.net_returns(pos, close, fee(sell_fee_bps=20.0))
    assert net[0] == pytest.approx(0.0)        # the opening buy pays no sell fee
    assert net[1] == pytest.approx(-0.002)     # the sale does
    assert net[2] == pytest.approx(0.0)


def test_a_short_entry_is_a_sale_and_pays_the_sell_fee():
    close = np.full(2, 100.0)
    net = vector.net_returns(np.array([-1.0, -1.0]), close, fee(sell_fee_bps=20.0))
    assert net[0] == pytest.approx(-0.002)


def test_borrow_accrues_only_while_short_and_only_on_the_held_position():
    close = np.full(4, 100.0)
    pos = np.array([-1.0, -1.0, 1.0, 1.0])
    net = vector.net_returns(pos, close, fee(borrow_annual=0.0252), bpy=252.0)
    assert net[0] == pytest.approx(0.0)        # flat before the first signal
    assert net[1] == pytest.approx(-0.0001)    # held short: 2.52%/252 bars
    assert net[2] == pytest.approx(-0.0001)    # bar 1's short is what bar 2 held
    assert net[3] == pytest.approx(0.0)        # long: no borrow


def test_borrow_is_ignored_without_bars_per_year():
    """A borrow rate cannot be pro-rated to a bar without knowing how long a bar is."""
    close = np.full(3, 100.0)
    with_bpy = vector.net_returns(np.full(3, -1.0), close, fee(borrow_annual=0.05),
                                  bpy=252.0)
    without = vector.net_returns(np.full(3, -1.0), close, fee(borrow_annual=0.05))
    assert with_bpy[2] < without[2]
    assert without[2] == pytest.approx(0.0)


def test_a_bare_number_is_read_as_symmetric_per_side_bps():
    """Kept so parity tests and ad-hoc callers can still pass a scalar."""
    close = np.full(3, 100.0)
    scalar = vector.net_returns(np.ones(3), close, 10.0)
    spelled = vector.net_returns(np.ones(3), close, fee(commission_bps=10.0))
    np.testing.assert_allclose(scalar, spelled)


def test_the_baseline_is_never_charged():
    """An always-long position with a zero-fee scenario is the benchmark, and it pays
    nothing — flattening or charging it turns it into a different strategy."""
    close = np.array([100.0, 110.0, 121.0])
    net = vector.net_returns(np.ones(3), close, FREE)
    np.testing.assert_allclose(net, [0.0, 0.10, 0.10])


# ------------------------------------------------------------------- the return floor

def test_net_returns_are_clipped_at_the_return_floor():
    """A short losing more than 100% in a bar must not drive equity negative and flip
    positive on the next multiply."""
    close = np.array([100.0, 300.0])           # +200% against a short
    net = vector.net_returns(np.array([-1.0, -1.0]), close, FREE)
    assert net[1] == pytest.approx(vector.RETURN_FLOOR)
    assert vector.equity_curve(net)[-1] > 0.0


def test_the_floor_does_not_touch_ordinary_losses():
    close = np.array([100.0, 50.0])
    net = vector.net_returns(np.ones(2), close, FREE)
    assert net[1] == pytest.approx(-0.50)


# --------------------------------------------------------------------------- stats

def test_stats_reports_exposure_trades_and_turnover():
    close = np.full(6, 100.0)
    pos = np.array([0.0, 1.0, 1.0, 0.0, -1.0, -1.0])
    index = pd.date_range("2020-01-01", periods=6, freq="D")
    out = vector.stats(pos, close, index, FREE, capital=10_000.0)
    assert out["exposure"] == pytest.approx(4 / 6)
    assert out["n_trades"] == 3                # in, out, and into the short
    assert out["turnover_per_year"] > 0
    assert out["n_bars"] == 6


def test_stats_final_equity_matches_the_compounded_curve():
    close = np.array([100.0, 110.0, 99.0, 108.9])
    index = pd.date_range("2020-01-01", periods=4, freq="D")
    out = vector.stats(np.ones(4), close, index, FREE, capital=1_000.0)
    assert out["final_equity"] == pytest.approx(close[-1] / close[0])
    assert out["pnl_dollars"] == pytest.approx(1_000.0 * (out["final_equity"] - 1.0))


def test_stats_returns_none_when_there_is_nothing_to_score():
    index = pd.date_range("2020-01-01", periods=2, freq="D")
    assert vector.stats(np.ones(2), np.array([np.nan, np.nan]), index, FREE, 1.0) is None


def test_stats_accepts_a_precomputed_net_series_without_changing_the_answer():
    """Passing it in is worth ~15% of the scoring cost and must change nothing."""
    close = 100.0 * np.exp(np.cumsum(np.random.default_rng(0).normal(0, 0.01, 300)))
    index = pd.date_range("2020-01-01", periods=300, freq="D")
    pos = np.resize([1.0, 0.0, -1.0], 300)
    bpy = vector.bars_per_year(index)
    net = vector.net_returns(pos, close, FREE, bpy)
    a = vector.stats(pos, close, index, FREE, 1.0)
    b = vector.stats(pos, close, index, FREE, 1.0, net=net, bpy=bpy)
    assert a == b


def test_final_equity_agrees_with_stats():
    close = 100.0 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, 200)))
    index = pd.date_range("2020-01-01", periods=200, freq="D")
    pos = np.resize([1.0, 1.0, 0.0], 200)
    out = vector.stats(pos, close, index, FREE, capital=5_000.0)
    assert vector.final_equity(pos, close, FREE, 5_000.0) == pytest.approx(
        5_000.0 * out["final_equity"])


# ---------------------------------------------------------------------- flatten_eod

def test_flatten_eod_zeroes_the_last_bar_of_every_session(intraday_5m):
    pos = np.ones(len(intraday_5m))
    out = vector.flatten_eod(pos, intraday_5m.index)
    days = intraday_5m.index.normalize()
    last = np.flatnonzero(np.append(days[1:] != days[:-1], True))
    assert len(last) == 3                                   # three sessions
    assert np.all(out[last] == 0.0)
    assert np.all(np.delete(out, last) == 1.0)


def test_flatten_eod_always_closes_the_final_bar(intraday_5m):
    out = vector.flatten_eod(np.ones(len(intraday_5m)), intraday_5m.index)
    assert out[-1] == 0.0


def test_flatten_eod_does_not_mutate_its_input(intraday_5m):
    pos = np.ones(len(intraday_5m))
    vector.flatten_eod(pos, intraday_5m.index)
    assert np.all(pos == 1.0)


def test_flatten_eod_preserves_shorts_it_does_not_touch(intraday_5m):
    pos = np.full(len(intraday_5m), -1.0)
    out = vector.flatten_eod(pos, intraday_5m.index)
    assert set(np.unique(out)) == {-1.0, 0.0}
