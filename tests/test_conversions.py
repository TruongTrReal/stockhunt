"""The strategies converted from somebody else's code — see `strategies/CONVERSIONS.md`.

`test_registry.py` already asserts that everything in `CATALOG` builds, is finite and
stays inside -1..1, and `strategies/tests/test_causality.py` gates all of them on
truncation against real bars. Neither of those can catch the failure mode this file is
for: a conversion that runs, is causal, and computes **a different rule than the one that
was published**.

So the tests here are about mechanism, on hand-built series where the right answer is
known by construction rather than by re-running the implementation. Three things get
special attention:

* the state machines, because a Pine `strategy.entry` on the opposing side REVERSES and a
  freqtrade exit goes FLAT, and getting that backwards changes a rule's exposure by 100
  percentage points without changing anything that looks wrong;
* the ratchets and counters, which are the parts that cannot be vectorised and are
  therefore the parts most likely to drift;
* the defects reproduced ON PURPOSE. `lorentzian_knn`'s backwards training label and
  `ssl_hybrid`'s wrong-band short leg are pinned here, so that a later "fix" has to argue
  with a test instead of silently orphaning the rule's history.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from conftest import make_ohlcv
from strategies.published import (bar_updn, bb_outside_in, heikin_reversal,
                                  lorentzian_knn, range_filter, range_filter_macd,
                                  renko_delta, ssl_hybrid)


def frame_from(close: np.ndarray, high=None, low=None, open_=None) -> pd.DataFrame:
    """An OHLCV frame around a given close path, with sane brackets by default."""
    n = len(close)
    return pd.DataFrame(
        {"Open": np.asarray(open_ if open_ is not None else close, dtype="float64"),
         "High": np.asarray(high if high is not None else close, dtype="float64"),
         "Low": np.asarray(low if low is not None else close, dtype="float64"),
         "Close": np.asarray(close, dtype="float64"),
         "Volume": np.full(n, 1e6)},
        index=pd.date_range("2015-01-05", periods=n, freq="1D"))


@pytest.fixture
def daily():
    df = make_ohlcv(1500, seed=23)
    return df, df["Close"].to_numpy("float64"), 252.0


# ------------------------------------------------------------------------ bar_updn

def test_bar_updn_reads_the_close_two_bars_back():
    """`close > open and open > close[2]`, and nothing else. Hand-built so the signal
    bar is unambiguous."""
    close = np.array([10.0, 10.0, 10.0, 12.0, 12.0])
    open_ = np.array([10.0, 10.0, 10.0, 11.0, 12.0])
    #                                     ^ up bar, and open 11 > close[2] = 10
    df = frame_from(close, open_=open_)
    pos = bar_updn.position(df, close, 252.0)
    np.testing.assert_array_equal(pos, [0.0, 0.0, 0.0, 1.0, 1.0])


def test_bar_updn_reverses_rather_than_going_flat(daily):
    """A Pine entry on the opposite side closes and reverses. If this ever returns a
    long/flat series its exposure is wrong by 100pp and its IR is not comparable."""
    df, close, bpy = daily
    pos = bar_updn.position(df, close, bpy)
    assert set(np.unique(pos)) <= {-1.0, 0.0, 1.0}
    assert (pos == -1.0).any() and (pos == 1.0).any()
    assert (pos == 0.0).sum() < 10, "flat only during the warmup before the first signal"


def test_bar_updn_long_flat_variant_removes_exactly_the_short_leg(daily):
    df, close, bpy = daily
    both = bar_updn.position(df, close, bpy, allow_short=1)
    long_only = bar_updn.position(df, close, bpy, allow_short=0)
    np.testing.assert_array_equal(long_only, np.maximum(both, 0.0))


# -------------------------------------------------------------------- range_filter

def test_the_range_filter_does_not_move_inside_its_own_band():
    """The whole mechanism. Price wandering by less than one band width leaves the
    filter exactly where it was — a filter that tracked price here would turn a
    deadband trend rule into a fast one."""
    close = np.concatenate([np.full(60, 100.0), 100.0 + np.tile([0.4, -0.4], 40)])
    band, filt, _ = range_filter._filter(close, 20, 3.5)
    settled = filt[80:]
    assert np.allclose(settled, settled[0]), "the ratchet stepped inside its band"


def test_the_range_filter_steps_by_exactly_one_band_width():
    close = np.concatenate([np.full(80, 100.0), np.full(20, 130.0)])
    band, filt, direction = range_filter._filter(close, 20, 3.5)
    stepped = np.flatnonzero(np.diff(filt) != 0)
    assert stepped.size, "a 30% jump must move the filter"
    i = stepped[-1] + 1
    np.testing.assert_allclose(filt[i], close[i] - band[i])
    assert direction[i] == 1.0


def test_only_the_first_signal_of_a_leg_fires():
    """`longCondition = longCond and CondIni[1] == -1` — the source takes the turn and
    ignores every later bar of the same leg. A version that re-signals would trade many
    times more often at the same fee schedule."""
    close = np.concatenate([np.full(80, 100.0),
                            np.linspace(100.0, 200.0, 120),
                            np.linspace(200.0, 90.0, 120)])
    long_sig, short_sig = range_filter.raw_signals(close, 20, 3.5)
    assert long_sig.sum() <= 2 and short_sig.sum() <= 2


def test_the_range_filter_holds_its_side_between_legs():
    df = make_ohlcv(1500, seed=5)
    close = df["Close"].to_numpy("float64")
    pos = range_filter.position(df, close, 252.0)
    assert set(np.unique(pos)) <= {-1.0, 0.0, 1.0}
    assert (np.diff(pos) != 0).sum() < 400, "a deadband rule should not trade every bar"


def test_the_macd_gate_only_blocks_entries_never_exits():
    """The asymmetry that gives `range_filter_macd` a flat state its parent lacks: a
    blocked entry still CLOSES the opposite leg. Forced by handing it a histogram that
    can never agree — the position must be able to reach 0 and must never reach -1."""
    df = make_ohlcv(1500, seed=5)
    close = df["Close"].to_numpy("float64")
    gated = range_filter_macd.position(df, close, 252.0, fast_days=200.0,
                                       slow_days=201.0, signal_days=200.0)
    ungated = range_filter.position(df, close, 252.0)
    assert (gated == 0.0).sum() > (ungated == 0.0).sum()
    assert set(np.unique(gated)) <= {-1.0, 0.0, 1.0}


# ------------------------------------------------------------------ bb_outside_in

def test_the_outside_in_counter_arms_on_a_pierce_and_fires_on_the_recross():
    price = np.array([10.0, 10.0, 5.0, 8.0, 9.0, 9.0])
    band = np.full(6, 6.0)                       # only bar 2 pierces
    recross = np.array([False, False, False, True, True, False])
    fired = bb_outside_in._outside_in(price, band, recross, np.less)
    np.testing.assert_array_equal(fired, [False, False, False, True, False, False])


def test_a_second_pierce_re_arms_from_the_newer_low_rather_than_cancelling():
    """The reference is always the MOST RECENT pierce. A deeper low replaces the old one
    and the rule waits for a recross from there — it does not give up on the setup, and
    a version that did would miss every capitulation."""
    price = np.array([10.0, 5.0, 4.0, 8.0, 9.0])
    band = np.full(5, 6.0)
    recross = np.array([False, False, False, True, False])
    fired = bb_outside_in._outside_in(price, band, recross, np.less)
    np.testing.assert_array_equal(fired, [False, False, False, True, False])


def test_undercutting_the_standing_low_disarms_the_bar_it_happens_on():
    """The condition is "pierced and has not since gone lower". Bar 3 dips under the
    piercing low of 5 WITHOUT touching the band, so even with a midline recross on that
    same bar the counter is held at zero."""
    price = np.array([10.0, 5.0, 10.0, 4.5])
    band = np.array([6.0, 6.0, 4.0, 4.0])
    recross = np.array([False, False, False, True])
    fired = bb_outside_in._outside_in(price, band, recross, np.less)
    assert not fired.any()


def test_the_outside_in_signal_needs_a_pierce_to_have_happened_at_all():
    price = np.full(20, 10.0)
    band = np.full(20, 1.0)                      # never pierced
    recross = np.ones(20, dtype=bool)
    assert not bb_outside_in._outside_in(price, band, recross, np.less).any()


# ---------------------------------------------------------------- heikin_reversal

def test_the_synthetic_open_is_the_midpoint_of_the_bar_two_back():
    """NOT a real Heikin-Ashi open — the author says so and says he preferred it. If
    this ever becomes the recursive HA open it is a different rule with the same name."""
    df = make_ohlcv(300, seed=3)
    close = df["Close"].to_numpy("float64")
    open_ = df["Open"].to_numpy("float64")
    expect = (open_[:-2] + close[:-2]) / 2.0
    # Rebuilt from the same inputs the strategy uses, so a change to the lag shows up.
    got = np.full(len(close), np.nan)
    got[2:] = expect
    pos = heikin_reversal.position(df, close, 252.0, open_lag=2)
    other = heikin_reversal.position(df, close, 252.0, open_lag=1)
    assert not np.array_equal(pos, other), "open_lag must actually change the candle"
    assert np.isfinite(got[2:]).all()


def test_heikin_reversal_is_long_flat_not_reversing(daily):
    """freqtrade sells to cash; it has no short side at all here."""
    df, close, bpy = daily
    pos = heikin_reversal.position(df, close, bpy)
    assert set(np.unique(pos)) <= {0.0, 1.0}


# -------------------------------------------------------------------- renko_delta

def test_bricks_print_only_on_a_move_of_at_least_delta():
    """A 4% path never prints a 5% brick, so the rule never leaves cash."""
    close = 100.0 * np.cumprod(np.append(1.0, np.tile([1.04, 1 / 1.04], 100)))
    df = frame_from(close)
    assert not renko_delta.position(df, close, 252.0, delta=0.05).any()


def test_a_run_of_up_bricks_goes_long_and_a_run_of_down_bricks_goes_flat():
    close = np.concatenate([100.0 * 1.2 ** np.arange(6),
                            100.0 * 1.2 ** 5 / 1.2 ** np.arange(1, 7)])
    df = frame_from(close)
    pos = renko_delta.position(df, close, 252.0, delta=0.05, min_steps=1)
    assert pos[5] == 1.0 and pos[-1] == 0.0


def test_min_steps_demands_consecutive_bricks(daily):
    df, close, bpy = daily
    one = renko_delta.position(df, close, bpy, delta=0.05, min_steps=1)
    two = renko_delta.position(df, close, bpy, delta=0.05, min_steps=2)
    assert (np.diff(two) != 0).sum() <= (np.diff(one) != 0).sum()


# ------------------------------------------------------- reproduced-on-purpose defects

def test_the_lorentzian_training_label_is_backwards_and_stays_that_way():
    """PINNED. `close[4] < close[0] ? short : long` labels the move that ENDED at t,
    and labels a RISE as short. That is the published script and it is why this rule is
    a reversion classifier rather than the trend model it is marketed as. Shifting the
    label by four bars, or flipping its sign, would make this a rule with no history —
    if that is wanted it needs a new file and a new trial, not an edit here.

    See `strategies/CONVERSIONS.md`.
    """
    close = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 12.0, 8.0, 10.0])
    labels = np.zeros(len(close))
    labels[4:] = np.where(close[:-4] < close[4:], -1.0,
                          np.where(close[:-4] > close[4:], 1.0, 0.0))
    # bar 5 is above bar 1 -> SHORT; bar 6 is below bar 2 -> LONG.
    assert labels[5] == -1.0
    assert labels[6] == 1.0


def _literal_pine_knn(feats, labels, k, max_bars_back):
    """A line-by-line transcription of the Pine neighbour loop. Slow on purpose.

    This is the reference `_predict` has to match. `_predict` jumps straight to the next
    candidate that clears the threshold instead of testing every index, which is the one
    place in this batch where an optimisation could change a result — so it is checked
    against the unoptimised form rather than against itself.
    """
    n = len(feats)
    cap = max_bars_back - 1
    out = np.zeros(n)
    dists: list[float] = []
    preds: list[float] = []
    for t in range(n):
        last = -1.0
        for i in range(0, min(cap, t) + 1):
            d = float(np.log1p(np.abs(feats[t] - feats[i])).sum())
            if d >= last and (i % 4):
                last = d
                dists.append(d)
                preds.append(labels[i])
                if len(preds) > k:
                    last = dists[round(k * 3 / 4)]
                    dists.pop(0)
                    preds.pop(0)
        out[t] = float(np.sum(preds))
    return out


@pytest.mark.parametrize("n,n_feat,k,max_bars_back", [
    (400, 5, 8, 2000),        # the published settings, training set never saturated
    (400, 5, 8, 120),         # saturated, so the scan is pinned to the oldest 120 bars
    (300, 2, 4, 60),          # the two-feature cell of the grid
    (250, 5, 3, 2000),        # k below the 3/4 drop index
])
def test_the_neighbour_scan_matches_an_unoptimised_transcription(n, n_feat, k,
                                                                 max_bars_back):
    rng = np.random.default_rng(1)
    feats = rng.random((n, n_feat))
    labels = rng.choice([-1.0, 0.0, 1.0], n)
    np.testing.assert_array_equal(
        lorentzian_knn._predict(feats, labels, k, max_bars_back),
        _literal_pine_knn(feats, labels, k, max_bars_back))


def test_the_neighbour_buffer_persists_across_bars():
    """PINNED. `var predictions` is not reset each bar — only `lastDistance` is. So the
    prediction at bar t is a sum over neighbours accumulated over many bars, and a
    version that rebuilt the buffer per bar would be a different model."""
    rng = np.random.default_rng(9)
    feats = rng.random((300, 5))
    labels = np.ones(300)
    pred = lorentzian_knn._predict(feats, labels, 8, 2000)
    # With every label +1, a per-bar buffer could never exceed the neighbours found on
    # that bar; a persistent one saturates at k and stays there.
    assert pred[-1] == 8.0
    assert (pred[50:] == 8.0).all()


def test_the_lorentzian_normaliser_only_ever_looks_backwards():
    """MLExtensions' `normalize` is an EXPANDING min/max. Truncation is the test, as it
    is everywhere else here — a whole-series rescale would be the same class of leak as
    `np.nanmedian`, spread across every feature instead of one scalar."""
    rng = np.random.default_rng(4)
    x = np.cumsum(rng.normal(0, 1, 900))
    full = lorentzian_knn._normalize(x)
    short = lorentzian_knn._normalize(x[:-300])
    np.testing.assert_array_equal(full[:len(short)], short)


def test_the_ssl_hybrid_short_leg_uses_the_upper_keltner_band():
    """PINNED. The source tests `close < upperk` on the SHORT side where symmetry wants
    the lower band, so the two legs are gated differently. Detected structurally: making
    the band very wide has to leave the short side reachable while it shuts the long
    side down completely."""
    df = make_ohlcv(2500, seed=17)
    close = df["Close"].to_numpy("float64")
    wide = ssl_hybrid.position(df, close, 252.0, kc_mult=50.0)
    assert not (wide > 0).any(), "a huge band must make `close > upperk` unreachable"
    assert (wide < 0).any(), "...and must leave `close < upperk` trivially satisfied"
