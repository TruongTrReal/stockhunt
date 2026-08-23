"""`strategies._indicators` and the regime overlay — causality above all.

**Causality is tested by truncation, not by reading the code.** Build the primitive on the
full series and on the series minus the last N bars, then require the overlap to be
identical. A value at bar *t* that depends on bars after *t* cannot pass, and no amount of
staring at `rolling()` calls substitutes — that is exactly how `np.nanmedian(whole_series)`
survived in `prereg.volmanaged` and `variants._vol_scale` long enough to contaminate two
published stages.

So the leak is tested in both directions: `_causal_median` must survive truncation, and
the `np.nanmedian` form it replaced must be shown to fail the same test. A causality test
that cannot fail is not testing causality.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies import _indicators as ind
from strategies.overlays import regime

TRUNCATE = 200


@pytest.fixture
def series() -> np.ndarray:
    rng = np.random.default_rng(12)
    return 100.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, 1200)))


def assert_causal(fn, x, cut: int = TRUNCATE):
    """The overlap of `fn(x)` and `fn(x[:-cut])` must be identical, NaNs included."""
    full = np.asarray(fn(x), dtype="float64")
    short = np.asarray(fn(x[:-cut]), dtype="float64")
    np.testing.assert_array_equal(full[:len(short)], short)


# ------------------------------------------------------------------------ _bars

def test_bars_converts_a_calendar_span_on_the_sheets_own_grid():
    """A US equity 4h 'day' is one 4h bar plus a 2.5h stub, so '50 days' is a different
    window on every sheet. Never hardcode 252."""
    assert ind._bars(252.0, 50 * ind.D) == 50
    assert ind._bars(1638.0, 50 * ind.D) == 325
    assert ind._bars(252.0, ind.M) == 21


def test_bars_enforces_a_minimum_window():
    assert ind._bars(252.0, 0.0) == 2
    assert ind._bars(252.0, ind.D, minimum=10) == 10


def test_bars_matches_the_shared_core_definition():
    """`stockhunt.stats.bars` is the same arithmetic; `strategies/` keeps its own copy
    only because it must not depend on anything outside itself."""
    from stockhunt import stats
    for bpy in (252.0, 1638.0, 8760.0):
        for years in (0.02, 0.2, 1.0, 3.0):
            assert ind._bars(bpy, years) == stats.bars(bpy, years)


# --------------------------------------------------------------- _state_machine

def test_the_state_machine_starts_flat_and_holds_between_signals():
    entry = np.array([False, True, False, False, False, False])
    exit_ = np.array([False, False, False, True, False, False])
    np.testing.assert_array_equal(ind._state_machine(entry, exit_),
                                  [0.0, 1.0, 1.0, 0.0, 0.0, 0.0])


def test_entry_wins_a_same_bar_tie():
    """You do not enter and exit on the same close — the convention every published
    version of these rules uses."""
    both = np.array([False, True, False])
    np.testing.assert_array_equal(ind._state_machine(both, both), [0.0, 1.0, 1.0])


def test_the_state_machine_is_causal(series):
    up = np.append(False, series[1:] > series[:-1])
    down = np.append(False, series[1:] < series[:-1])
    full = ind._state_machine(up, down)
    short = ind._state_machine(up[:-TRUNCATE], down[:-TRUNCATE])
    np.testing.assert_array_equal(full[:len(short)], short)


# ----------------------------------------------------------------------- _streak

def test_streak_counts_signed_runs_of_consecutive_closes():
    close = np.array([10.0, 11.0, 12.0, 13.0, 12.0, 11.0, 11.0])
    np.testing.assert_array_equal(ind._streak(close),
                                  [0.0, 1.0, 2.0, 3.0, -1.0, -2.0, 0.0])


def test_streak_is_causal(series):
    assert_causal(ind._streak, series)


# ------------------------------------------------------------------------- _flip

def test_flip_holds_its_side_and_never_goes_flat_after_the_first_signal():
    """The distinction that matters against `_state_machine`: a Pine `strategy.entry`
    on the opposing side REVERSES, it does not close to cash. A converted rule that
    goes flat instead is out of the market for the whole of every downtrend."""
    long_ = np.array([False, True, False, False, False, False])
    short = np.array([False, False, False, True, False, False])
    np.testing.assert_array_equal(ind._flip(long_, short),
                                  [0.0, 1.0, 1.0, -1.0, -1.0, -1.0])


def test_flip_starts_flat_before_any_signal():
    none = np.zeros(4, dtype=bool)
    np.testing.assert_array_equal(ind._flip(none, none), np.zeros(4))


def test_flip_gives_a_same_bar_tie_to_the_long_side():
    """Same convention as `_state_machine`, so the two agree on what a tie means."""
    both = np.array([False, True, False])
    np.testing.assert_array_equal(ind._flip(both, both), [0.0, 1.0, 1.0])


def test_flip_is_causal(series):
    up = np.append(False, series[1:] > series[:-1])
    down = np.append(False, series[1:] < series[:-1])
    full = ind._flip(up, down)
    short = ind._flip(up[:-TRUNCATE], down[:-TRUNCATE])
    np.testing.assert_array_equal(full[:len(short)], short)


# ------------------------------------------------------ _cross_over / _cross_under

def test_a_cross_fires_only_on_the_bar_the_lines_swap():
    a = np.array([1.0, 1.0, 3.0, 4.0, 4.0])
    b = np.array([2.0, 2.0, 2.0, 2.0, 5.0])
    np.testing.assert_array_equal(ind._cross_over(a, b),
                                  [False, False, True, False, False])
    np.testing.assert_array_equal(ind._cross_under(a, b),
                                  [False, False, False, False, True])


def test_touching_without_crossing_is_not_a_cross():
    """Pine's `crossover` needs `a[1] <= b[1]` AND `a > b`, so equality on the previous
    bar arms it and equality on this one does not fire it."""
    a = np.array([1.0, 2.0, 2.0, 3.0])
    b = np.array([2.0, 2.0, 2.0, 2.0])
    np.testing.assert_array_equal(ind._cross_over(a, b),
                                  [False, False, False, True])


def test_the_first_bar_can_never_be_a_cross():
    a = np.array([5.0, 5.0])
    b = np.array([1.0, 1.0])
    assert not ind._cross_over(a, b)[0]


def test_a_nan_bar_neither_opens_nor_closes_a_cross():
    """Warmup bars compare false in both directions, which is what Pine's `na` gives:
    the bar AFTER the NaN cannot cross, because its predecessor was undefined, and the
    first fully-defined pair can."""
    a = np.array([np.nan, np.nan, 3.0, 1.0, 3.0])
    b = np.array([2.0, 2.0, 2.0, 2.0, 2.0])
    np.testing.assert_array_equal(ind._cross_over(a, b),
                                  [False, False, False, False, True])


def test_cross_is_causal(series):
    other = np.roll(series, 3)
    full = ind._cross_over(series, other)
    short = ind._cross_over(series[:-TRUNCATE], other[:-TRUNCATE])
    np.testing.assert_array_equal(full[:len(short)], short)


# -------------------------------------------------------------------------- _hma

def test_hma_lags_a_straight_line_far_less_than_an_sma_of_the_same_length():
    """The point of a Hull average. On a ramp of slope s an SMA(16) sits 7.5s behind;
    this construction sits 0.67s behind — not zero, because Pine's truncated half
    length breaks the exact cancellation, which is the whole reason `_hma` reproduces
    that truncation instead of tidying it."""
    import talib
    slope = 2.0
    x = np.arange(200, dtype="float64") * slope + 10.0
    hma_lag = (x[100:] - ind._hma(x, 16)[100:]) / slope
    sma_lag = (x[100:] - talib.SMA(x, 16)[100:]) / slope
    np.testing.assert_allclose(hma_lag, 2.0 / 3.0, atol=1e-9)
    np.testing.assert_allclose(sma_lag, 7.5, atol=1e-9)


def test_hma_uses_pines_truncated_half_length():
    """Pine truncates the `n/2` it passes as a length, so an odd `n` is not symmetric.
    Reproduced, because the published settings were fitted against that arithmetic."""
    x = np.arange(200, dtype="float64") ** 1.5
    import talib
    expect = talib.WMA(np.ascontiguousarray(
        2.0 * talib.WMA(x, 12) - talib.WMA(x, 25)), 5)
    np.testing.assert_allclose(ind._hma(x, 25)[50:], expect[50:], rtol=1e-12)


def test_hma_is_causal(series):
    assert_causal(lambda x: ind._hma(x, 16), series)


# ------------------------------------------------------------------- _pct_rank

def test_pct_rank_ranks_the_newest_value_within_its_window():
    x = np.arange(10.0)
    out = ind._pct_rank(x, 5)
    assert np.isnan(out[:4]).all()                          # window not yet full
    assert out[4] == pytest.approx(100.0)                   # a rising series is always top
    assert out[-1] == pytest.approx(100.0)


def test_pct_rank_is_causal(series):
    assert_causal(lambda x: ind._pct_rank(x, 100), series)


# --------------------------------------------------- rolling extremes exclude t

def test_rolling_max_excludes_the_current_bar():
    """The whole point of a breakout rule. Comparing close[t] against a window that
    already contains close[t] makes 'a new 20-day high' nearly unreachable."""
    x = np.array([1.0, 5.0, 3.0, 9.0, 2.0])
    out = ind._rolling_max(x, 2)
    assert np.isnan(out[:2]).all()
    assert out[2] == 5.0                                    # max(x[0], x[1]), not x[2]
    assert out[3] == 5.0                                    # max(x[1], x[2]), not the 9
    assert out[4] == 9.0


def test_rolling_min_excludes_the_current_bar():
    x = np.array([9.0, 5.0, 7.0, 1.0, 8.0])
    out = ind._rolling_min(x, 2)
    assert out[2] == 5.0
    assert out[4] == 1.0


def test_a_breakout_on_the_current_bar_is_reachable(series):
    """The consequence of the exclusion: new highs actually happen."""
    hi = ind._rolling_max(series, 20)
    assert int(np.nansum(series > hi)) > 0


@pytest.mark.parametrize("fn", [ind._rolling_max, ind._rolling_min])
def test_rolling_extremes_are_causal(series, fn):
    assert_causal(lambda x: fn(x, 50), series)


# ------------------------------------------------------- _causal_median, the leak

def test_causal_median_is_an_expanding_median(series):
    out = ind._causal_median(series, 10)
    assert np.isnan(out[:9]).all()
    assert out[99] == pytest.approx(float(np.median(series[:100])))
    assert out[-1] == pytest.approx(float(np.median(series)))


def test_causal_median_survives_truncation(series):
    assert_causal(lambda x: ind._causal_median(x, 50), series)


def test_the_nanmedian_form_it_replaced_FAILS_the_same_test(series):
    """A causality test that cannot fail is not testing causality.

    This is the exact construction still live in `prereg.volmanaged` and
    `variants._vol_scale`: one scalar, but computed from bars that had not happened yet.
    Truncating the series changes the *past* values.
    """
    def leaky(x):
        return np.full(len(x), np.nanmedian(x))

    with pytest.raises(AssertionError):
        assert_causal(leaky, series)


def test_vol_scale_is_causal(series):
    assert_causal(lambda x: ind._vol_scale(x, 60), series)


def test_vol_scale_is_capped_at_one(series):
    """Levering to hit a target is a different strategy with a different risk profile, and
    this project's gates are stated for an unlevered book."""
    scale = ind._vol_scale(series, 60)
    assert np.isfinite(scale).all()
    assert scale.max() <= 1.0
    assert scale.min() >= 0.0


def test_vol_scale_shrinks_when_volatility_rises():
    calm = np.full(600, 0.0)
    rng = np.random.default_rng(2)
    ret = np.concatenate([rng.normal(0, 0.002, 300), rng.normal(0, 0.05, 300)])
    close = 100.0 * np.exp(np.cumsum(ret))
    scale = ind._vol_scale(close, 60)
    assert float(np.nanmean(scale[-200:])) < float(np.nanmean(scale[100:300]))
    assert calm.sum() == 0.0


# ---------------------------------------------------------------- _day_ordinals

def test_day_ordinals_number_the_sessions_within_each_month():
    index = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04",
                              "2024-02-01", "2024-02-02"])
    pos, size = ind._day_ordinals(index)
    np.testing.assert_array_equal(pos, [1, 2, 3, 1, 2])
    np.testing.assert_array_equal(size, [3, 3, 3, 2, 2])


def test_day_ordinals_give_every_bar_of_a_session_the_same_ordinal():
    """Computed on unique dates then broadcast back, so an intraday sheet agrees with a
    daily one: every bar of the third trading day IS the third trading day."""
    index = pd.DatetimeIndex(
        ["2024-01-02 09:30", "2024-01-02 15:30", "2024-01-03 09:30", "2024-01-03 15:30"])
    pos, size = ind._day_ordinals(index)
    np.testing.assert_array_equal(pos, [1, 1, 2, 2])
    np.testing.assert_array_equal(size, [2, 2, 2, 2])


# ------------------------------------------------------------------ supertrend

def test_supertrend_is_plus_or_minus_one_after_warmup(series):
    n = len(series)
    df = pd.DataFrame({"High": series * 1.01, "Low": series * 0.99, "Close": series},
                      index=pd.date_range("2018-01-01", periods=n, freq="D"))
    trend = ind._supertrend_trend(df, series, 252.0, 14.0, 3.0)
    assert set(np.unique(trend)) <= {-1.0, 0.0, 1.0}
    assert trend[0] == 0.0                                  # before the first valid ATR
    assert set(np.unique(trend[-200:])) <= {-1.0, 1.0}


def test_supertrend_is_causal(series):
    n = len(series)
    index = pd.date_range("2018-01-01", periods=n, freq="D")

    def build(x):
        k = len(x)
        df = pd.DataFrame({"High": x * 1.01, "Low": x * 0.99, "Close": x},
                          index=index[:k])
        return ind._supertrend_trend(df, x, 252.0, 14.0, 3.0)

    assert_causal(build, series)


# --------------------------------------------------------------- the regime gate

def test_causal_quantile_survives_truncation(series):
    assert_causal(lambda x: regime._causal_quantile(x, 0.5, 100), series)


def test_the_whole_series_quantile_it_replaced_FAILS(series):
    def leaky(x):
        return np.full(len(x), np.nanquantile(x, 0.5))

    with pytest.raises(AssertionError):
        assert_causal(leaky, series)


def test_the_regime_gate_stands_aside_through_its_warmup(series):
    """A gate that trades its warmup is a gate whose earliest years are unconditional,
    which is exactly what it claims not to be."""
    hi = regime.regime_gate(series, 252.0, "hi", 0.5)
    lo = regime.regime_gate(series, 252.0, "lo", 0.5)
    warmup = ind._bars(252.0, regime.REGIME_WARMUP_YEARS)
    assert np.all(hi[:warmup] == 0.0)
    assert np.all(lo[:warmup] == 0.0)


def test_hi_and_lo_partition_the_scored_bars(series):
    """`side='lo'` is not optional decoration: reporting only the half that worked, after
    seeing both, is selection on the test set wearing a regime filter's clothes."""
    hi = regime.regime_gate(series, 252.0, "hi", 0.5)
    lo = regime.regime_gate(series, 252.0, "lo", 0.5)
    assert np.all(hi * lo == 0.0)                           # never both
    scored = (hi + lo) > 0
    assert scored.sum() > 0
    np.testing.assert_array_equal((hi + lo)[scored], np.ones(int(scored.sum())))


def test_the_regime_gate_is_causal(series):
    assert_causal(lambda x: regime.regime_gate(x, 252.0, "hi", 0.5), series)
    assert_causal(lambda x: regime.regime_gate(x, 252.0, "lo", 0.5), series)


def test_a_higher_quantile_gates_more_aggressively(series):
    loose = regime.regime_gate(series, 252.0, "hi", 0.2)
    tight = regime.regime_gate(series, 252.0, "hi", 0.8)
    assert tight.sum() < loose.sum()
