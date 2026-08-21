"""Chart-length conditioning: day-denominated windows become Pine bar counts."""

from __future__ import annotations


CHART_PREFIX = "chart"


CHART_SEP = ":"


# `_bars(bpy, days / 252)` returns `round(bpy * days / 252)`, so at exactly 252 bars a
# year every day-denominated parameter degenerates to its own number *as a bar count* —
# which is precisely what a Pine `ta.ema(close, 21)` means on ANY chart timeframe.
CHART_BPY = 252.0


def apply(label, df, close, bpy, symbol, build):
    """Decode `chart:<base label>` and build the base rule at Pine chart lengths.

    The repo's convention is horizon-preserving: `ema_cross_sniper` means "an 8-day
    against a 21-day EMA" on every sheet, so at 1m those windows are thousands of bars.
    That is the right convention for comparing a rule across timeframes, and it is NOT
    what anyone running the script on a TradingView chart ever saw — there the lengths
    are bars, so the same label on a 1m chart is an 8-minute against a 21-minute EMA.
    This overlay prices the chart experience; the bare label prices the horizon. They
    are different strategies and the trial ledger must carry them as different trials.

    It is exact, not approximate: every published strategy reaches `bpy` only through
    `_bars` (asserted by grep, pinned by `tests/test_chart_overlay.py`), so forcing
    `CHART_BPY` rescales every day-window at once and touches nothing else. Parameters
    that are already bar counts (`lookback`, `period`, `max_bars_back`) never see `bpy`
    and pass through unchanged, exactly as they should.

    `build` arrives as an argument rather than as an import: the registry owns label
    resolution and would be a circular import from here.
    """
    prefix = CHART_PREFIX + CHART_SEP
    if not label.startswith(prefix):
        return None
    base_label = label[len(prefix):]
    if not base_label:
        return None
    return build(base_label, df, close, CHART_BPY, symbol)
