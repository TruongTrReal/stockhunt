"""`stockhunt.stats` — the one definition of each summary statistic.

`tools/test_stats_equivalence.py` already proves these reproduce the four implementations
they replaced, bit for bit. It does not pin down what they should *do*, so a change that
moved both the new and the old definition together would pass it. These tests state the
contract directly, and they concentrate on the two parameters that exist only because the
originals disagreed — `min_obs` and `dropna` — since those are where a caller silently
picking the wrong default moves a published number.
"""

from __future__ import annotations

import numpy as np
import pytest

from stockhunt import stats

DAILY = 252.0


def test_ann_vol_uses_ddof_1():
    r = np.array([0.01, -0.02, 0.03, 0.0, -0.01])
    assert stats.ann_vol(r, DAILY) == pytest.approx(
        float(np.std(r, ddof=1) * np.sqrt(DAILY)))
    # ddof=0 is numpy's default and would be a smaller number. Mixing the two is how two
    # backtests become silently incomparable, so the difference must be real, not rounding.
    assert stats.ann_vol(r, DAILY) > float(np.std(r, ddof=0) * np.sqrt(DAILY))


def test_cagr_on_a_constant_return_series_is_exact():
    # 252 bars of +0.1%/bar at 252 bpy is exactly one year, so the CAGR is the total return.
    r = np.full(252, 0.001)
    assert stats.cagr(r, DAILY) == pytest.approx(1.001 ** 252 - 1.0, rel=1e-12)


def test_cagr_matches_the_one_over_years_spelling():
    """`prod^(bpy/n)` and `prod^(1/years)` are the same number — both were in the repo."""
    r = np.random.default_rng(0).normal(0.0004, 0.01, 900)
    years = r.size / DAILY
    assert stats.cagr(r, DAILY) == pytest.approx(
        float(np.prod(1.0 + r) ** (1.0 / years) - 1.0), rel=1e-12)


def test_cagr_filters_non_finite_before_compounding():
    r = np.array([0.01, np.nan, 0.02, np.inf, 0.03])
    clean = np.array([0.01, 0.02, 0.03])
    assert stats.cagr(r, DAILY) == pytest.approx(stats.cagr(clean, DAILY))


@pytest.mark.parametrize("r,bpy", [
    (np.array([0.01]), DAILY),            # one observation
    (np.array([]), DAILY),                # none at all
    (np.array([0.01, 0.02]), 0.0),        # bpy <= 0 cannot annualise
    (np.array([0.01, 0.02]), -1.0),
])
def test_cagr_returns_nan_when_undefined(r, bpy):
    assert np.isnan(stats.cagr(r, bpy))


def test_max_drawdown_finds_the_worst_peak_to_trough():
    # 1.0 -> 1.10 -> 0.88 -> 0.968: peak 1.10, trough 0.88, so -20%.
    r = np.array([0.10, -0.20, 0.10])
    assert stats.max_drawdown(r) == pytest.approx(-0.20, rel=1e-12)


def test_max_drawdown_is_zero_ish_on_a_monotonic_curve():
    assert stats.max_drawdown(np.full(50, 0.01)) == pytest.approx(0.0)


def test_max_drawdown_default_is_nan_poisoned_by_one_bad_bar():
    """The documented bug, pinned so it cannot change by accident.

    `dropna=False` reproduces `riskmatch_wf._max_dd` / `portfolio_wf._max_dd`, which pass
    the raw array to `cumprod`: one non-finite bar propagates through the whole cumulative
    product and the answer is NaN for the entire series, silently. It is the default
    anyway, because switching it moves published numbers on any sheet carrying a
    non-finite bar. This test exists so that stays a decision rather than a surprise.
    """
    r = np.array([0.10, np.nan, -0.20, 0.10])
    assert np.isnan(stats.max_drawdown(r))
    assert np.isnan(stats.max_drawdown(r, dropna=False))
    # `focus_wf.drawdown` filtered first and was right.
    assert stats.max_drawdown(r, dropna=True) == pytest.approx(-0.20, rel=1e-12)


def test_max_drawdown_needs_two_observations():
    assert np.isnan(stats.max_drawdown(np.array([0.01])))


def test_sharpe_subtracts_the_risk_free_path():
    """Idle capital earns the T-bill rate, so `rf` is a vector in every real caller."""
    rng = np.random.default_rng(4)
    r = rng.normal(0.0005, 0.01, 400)
    rf = np.full(400, 0.0001)
    assert (stats.sharpe(r, rf, DAILY)
            == pytest.approx(stats.sharpe(r - rf, 0.0, DAILY)))
    # Subtracting a positive risk-free path can only lower a positive Sharpe.
    assert stats.sharpe(r, rf, DAILY) < stats.sharpe(r, 0.0, DAILY)


def test_sharpe_is_nan_when_the_excess_is_exactly_zero():
    """A strategy that sits in cash all fold earns exactly `rf`. 0/0 is undefined."""
    r = np.full(100, 0.002)
    assert np.isnan(stats.sharpe(r, 0.002, DAILY))


def test_sharpe_is_nan_on_a_constant_series():
    """FIXED. The guard is relative, matching `metrics.information_ratio`.

    An absolute `sd > 0` test passes here: np.std(np.full(100, 0.001), ddof=1) is
    2.18e-19, not 0, so the old code divided by dust and returned 7.28e16. It was
    value-dependent — 0.0001 lands on a true 0.0 and behaved — which is why it survived.
    """
    for value in (0.001, 0.01, 0.1, 1.0, -0.002, 0.0025):
        assert np.isnan(stats.sharpe(np.full(100, value), 0.0, DAILY)), value


def test_sharpe_is_nan_on_a_constant_EXCESS_over_a_varying_series():
    """The reachable shape: a fold spent entirely in cash earns exactly the bill rate,
    so `r - rf` is constant even though neither `r` nor `rf` is."""
    rf = np.random.default_rng(20).normal(0.0002, 0.00005, 500)
    assert np.isnan(stats.sharpe(rf + 0.001, rf, DAILY))


def test_the_guard_does_not_swallow_a_genuinely_small_edge():
    """Relative to the differences' own scale, not an absolute floor on variance."""
    rng = np.random.default_rng(21)
    tiny = rng.normal(1e-9, 1e-8, 5000)
    assert np.isfinite(stats.sharpe(tiny, 0.0, DAILY))


def test_the_fix_matches_the_information_ratio_guard_exactly():
    """One definition of 'this series is constant', not two that can drift."""
    import metrics
    rng = np.random.default_rng(22)
    bench = rng.normal(0.0004, 0.01, 2000)
    for offset in (0.001, 0.01, 1e-6):
        strat = bench + offset
        assert np.isnan(metrics.information_ratio(strat, bench, DAILY))
        assert np.isnan(stats.sharpe(strat, bench, DAILY))


def test_sharpe_min_obs_is_the_preserved_divergence():
    """`riskmatch_wf` used 3 and `portfolio_wf` used 30; each caller keeps its own."""
    r = np.random.default_rng(1).normal(0.001, 0.01, 10)
    assert np.isfinite(stats.sharpe(r, 0.0, DAILY))                 # default 3
    assert np.isfinite(stats.sharpe(r, 0.0, DAILY, min_obs=3))
    assert np.isnan(stats.sharpe(r, 0.0, DAILY, min_obs=30))        # portfolio_wf's floor


def test_sharpe_counts_finite_observations_not_array_length():
    """A 40-long array that is mostly NaN must not clear a 30-observation floor."""
    r = np.full(40, np.nan)
    r[:5] = np.random.default_rng(2).normal(0.001, 0.01, 5)
    assert np.isnan(stats.sharpe(r, 0.0, DAILY, min_obs=30))
    assert np.isfinite(stats.sharpe(r, 0.0, DAILY, min_obs=3))


def test_sharpe_is_nan_on_exactly_zero_variance():
    """The values where float64 happens to land on a true 0.0 std are handled today."""
    assert np.isnan(stats.sharpe(np.full(100, 0.0001), 0.0, DAILY))
    assert np.isnan(stats.sharpe(np.zeros(100), 0.0, DAILY))


def test_sharpe_annualises_by_sqrt_bpy():
    r = np.random.default_rng(3).normal(0.001, 0.01, 500)
    assert (stats.sharpe(r, 0.0, 252.0)
            == pytest.approx(stats.sharpe(r, 0.0, 1.0) * np.sqrt(252.0)))


def test_bars_never_hardcodes_252():
    """A US equity 4h 'day' is one 4h bar plus a 2.5h stub — `bpy` is measured."""
    assert stats.bars(252.0, 1.0) == 252
    assert stats.bars(1638.0, 50.0 / 252.0) == 325      # 50 "days" on a 4h sheet
    assert stats.bars(252.0, 50.0 / 252.0) == 50        # ...and on a daily one


def test_bars_enforces_its_minimum():
    assert stats.bars(252.0, 0.0) == 2
    assert stats.bars(252.0, 1.0 / 252.0, minimum=5) == 5


def test_bars_rounds_rather_than_truncating():
    assert stats.bars(100.0, 0.026) == 3        # 2.6 -> 3, not 2
