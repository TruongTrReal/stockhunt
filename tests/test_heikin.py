"""The `ha:` overlay: correct recursion, causal by construction, and signal-only.

The three ways a Heikin-Ashi backtest goes wrong, each pinned by a test here:

  1. the recursion is computed wrong (vectorised form drifts from the definition),
  2. the transform peeks (an anchored or centred variant would),
  3. the synthetic prices leak into the P&L (the TradingView chart-fill trap).

The third cannot be tested from inside `strategies/` alone — settlement happens in the
caller — so what is pinned instead is the contract that makes it impossible: `apply`
returns the base rule's EXPOSURE series unchanged in length and scale, and never
returns prices.
"""

import numpy as np
import pandas as pd

from conftest import make_ohlcv
from strategies.overlays import heikin
from strategies import registry


def _ha_loop(df):
    """The definition, bar by bar. The vectorised transform must match this exactly."""
    o = df["Open"].to_numpy("float64")
    h = df["High"].to_numpy("float64")
    l = df["Low"].to_numpy("float64")
    c = df["Close"].to_numpy("float64")
    n = len(c)
    hc = (o + h + l + c) / 4.0
    ho = np.empty(n)
    ho[0] = (o[0] + c[0]) / 2.0
    for t in range(1, n):
        ho[t] = (ho[t - 1] + hc[t - 1]) / 2.0
    return ho, hc


def test_ha_bars_matches_the_recurrence():
    df = make_ohlcv(800, seed=11)
    ha = heikin.ha_bars(df)
    ho, hc = _ha_loop(df)
    np.testing.assert_allclose(ha["Open"].to_numpy(), ho, rtol=0, atol=1e-12)
    np.testing.assert_allclose(ha["Close"].to_numpy(), hc, rtol=0, atol=0)


def test_ha_high_low_bracket_open_close():
    df = make_ohlcv(500, seed=3)
    ha = heikin.ha_bars(df)
    assert (ha["High"] >= ha[["Open", "Close"]].max(axis=1) - 1e-12).all()
    assert (ha["Low"] <= ha[["Open", "Close"]].min(axis=1) + 1e-12).all()
    # And the synthetic range contains the real one's extremes.
    assert (ha["High"] >= df["High"] - 1e-12).all()
    assert (ha["Low"] <= df["Low"] + 1e-12).all()


def test_ha_bars_is_tail_truncation_invariant():
    """Cutting future bars must not change a single earlier synthetic bar."""
    df = make_ohlcv(600, seed=5)
    full = heikin.ha_bars(df)
    cut = heikin.ha_bars(df.iloc[:400])
    pd.testing.assert_frame_equal(full.iloc[:400], cut)


def test_ha_volume_passes_through_untouched():
    df = make_ohlcv(300, seed=9)
    ha = heikin.ha_bars(df)
    np.testing.assert_array_equal(ha["Volume"].to_numpy(), df["Volume"].to_numpy())
    assert list(ha.columns) == list(df.columns)
    assert ha.index.equals(df.index)


def test_ha_label_builds_and_differs_from_the_bare_rule():
    df = make_ohlcv(2000, seed=21)
    close = df["Close"].to_numpy("float64")
    bare = registry.build("ema_cross_sniper", df, close, 252.0, "T")
    ha = registry.build("ha:ema_cross_sniper", df, close, 252.0, "T")
    assert bare is not None and ha is not None
    assert len(ha) == len(df)
    assert set(np.unique(ha)) <= {-1.0, 0.0, 1.0}
    assert not np.array_equal(bare, ha)


def test_ha_label_wraps_params_and_bad_labels_return_none():
    df = make_ohlcv(1500, seed=2)
    close = df["Close"].to_numpy("float64")
    assert registry.build("ha:ema_cross_sniper@allow_short=0", df, close,
                          252.0, "T") is not None
    assert registry.build("ha:", df, close, 252.0, "T") is None
    assert registry.build("ha:no_such_rule", df, close, 252.0, "T") is None
