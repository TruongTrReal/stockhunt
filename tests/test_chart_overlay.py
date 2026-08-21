"""The `chart:` overlay — Pine bar-count semantics, and nothing else.

The overlay's whole contract is a single substitution: the base rule is built with
`bpy = 252` so `_bars(bpy, days/252)` returns `days` as a bar count. What that must
mean in practice, and what is pinned here:

  * on a sheet whose measured bpy IS ~252, `chart:` changes nothing — identity;
  * on any other sheet it must equal calling the rule with bpy forced to 252;
  * bar-denominated parameters (`lookback`, `period`) never see bpy, so `chart:` is
    identity for a rule made only of those, at ANY bpy;
  * it composes under `ha:` in either order, because neither overlay reads what the
    other changes.
"""

import numpy as np
import pandas as pd

from conftest import make_ohlcv
from strategies import registry
from strategies.overlays import chart, heikin


def test_chart_is_identity_at_daily_bpy():
    df = make_ohlcv(2000, seed=13)
    close = df["Close"].to_numpy("float64")
    bare = registry.build("ema_cross_sniper", df, close, 252.0, "T")
    charted = registry.build("chart:ema_cross_sniper", df, close, 252.0, "T")
    np.testing.assert_array_equal(bare, charted)


def test_chart_forces_pine_lengths_at_intraday_bpy():
    """At bpy 98280 (a 1m equity session year) the bare rule and the charted rule must
    differ — and the charted one must equal the rule evaluated at bpy 252."""
    df = make_ohlcv(3000, freq="1min", seed=17)
    close = df["Close"].to_numpy("float64")
    intraday_bpy = 98280.0
    bare = registry.build("ema_cross_sniper", df, close, intraday_bpy, "T")
    charted = registry.build("chart:ema_cross_sniper", df, close, intraday_bpy, "T")
    pine = registry.build("ema_cross_sniper", df, close, chart.CHART_BPY, "T")
    assert not np.array_equal(bare, charted)
    np.testing.assert_array_equal(charted, pine)


def test_chart_is_identity_for_bar_denominated_rules():
    """`bar_updn`'s lookback is already a bar count, so chart: must change nothing
    even at an absurd bpy."""
    df = make_ohlcv(1500, freq="1min", seed=23)
    close = df["Close"].to_numpy("float64")
    bare = registry.build("bar_updn", df, close, 522634.0, "T")
    charted = registry.build("chart:bar_updn", df, close, 522634.0, "T")
    np.testing.assert_array_equal(bare, charted)


def test_chart_and_ha_compose_in_either_order():
    df = make_ohlcv(3000, freq="1min", seed=29)
    close = df["Close"].to_numpy("float64")
    a = registry.build("ha:chart:ema_cross_sniper", df, close, 98280.0, "T")
    b = registry.build("chart:ha:ema_cross_sniper", df, close, 98280.0, "T")
    assert a is not None
    np.testing.assert_array_equal(a, b)
    # And the composition really is the HA path: it must differ from chart: alone.
    c = registry.build("chart:ema_cross_sniper", df, close, 98280.0, "T")
    assert not np.array_equal(a, c)


def test_chart_bad_labels_return_none():
    df = make_ohlcv(500, seed=31)
    close = df["Close"].to_numpy("float64")
    assert registry.build("chart:", df, close, 252.0, "T") is None
    assert registry.build("chart:no_such_rule", df, close, 252.0, "T") is None
