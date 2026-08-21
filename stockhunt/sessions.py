"""Folding intraday bars into sessions that stop before the bell.

A rule computed from a session's own high, low and close and then filled at that same
close assumes a print nobody could have known until it happened. The repo's answer is not
to delay the fill by a bar but to **compute the signal earlier and still trade the
close**: a market-on-close order sent minutes before the bell fills in the deepest auction
of the day, and the only thing that changes is that the signal may not read the last few
minutes.

This module is the arithmetic of that, extracted so more than one caller can share it.
`paper trading engine/book_strategy.py` carries an equivalent private implementation that
predates this module and is deliberately NOT changed here — it is live code on a desk
holding open positions, and editing it would flag a restart that flattens every book. The
two are pinned together instead: `test_rotation_manager.py` asserts they agree bar for
bar, including on the two days a year where the naive arithmetic differs.

**The decision instant is built from the wall clock and then localised**, not by adding
sixteen hours to local midnight. On the spring-forward Sunday an hour does not exist, so
midnight plus sixteen hours of *elapsed* time is 17:00 rather than 16:00. That is right
for 363 days a year and silently trades the wrong bar on the other two.
"""
from __future__ import annotations

import pandas as pd

__all__ = ["decision_instants", "fold_sessions", "session_of"]


def decision_instants(idx: pd.DatetimeIndex, tz: str, hh: int, mm: int,
                      lead_min: int) -> pd.DatetimeIndex:
    """For each timestamp, the decision instant of the session it belongs to.

    `idx` must be tz-aware. The result is tz-aware in `tz` and has one entry per input, so
    it can be compared elementwise against `idx` or used as a grouping key.
    """
    naive = idx.tz_convert(tz).tz_localize(None).normalize()
    naive = naive + pd.Timedelta(hours=hh, minutes=mm) - pd.Timedelta(minutes=lead_min)
    return naive.tz_localize(tz)


def session_of(ts: pd.Timestamp, tz: str, hh: int, mm: int, lead_min: int):
    """`(session date, decision instant)` for one bar close, in market local time.

    A bar carries its CLOSE timestamp, so a 5-minute bar stamped 15:55 covers 15:50-15:55
    — the last bar whose contents are known five minutes before a 16:00 bell, and
    therefore the decision bar itself.
    """
    idx = pd.DatetimeIndex([ts])
    return (idx.tz_convert(tz)[0].date(),
            decision_instants(idx, tz, hh, mm, lead_min)[0])


def fold_sessions(bars: pd.DataFrame, tz: str, hh: int, mm: int,
                  lead_min: int) -> pd.DataFrame | None:
    """Intraday bars -> one OHLCV row per session, cut `lead_min` before the bell.

    `bars` is indexed by tz-aware bar-close timestamps and carries Open/High/Low/Close and
    optionally Volume.

    **Every row is a partial session, history included.** That is the point rather than an
    approximation: a rule whose past is full-day bars and whose newest row is a partial one
    is comparing two different statistics, and for anything with state that quietly changes
    which state it is in. The index is the decision instant, not the session date, so two
    sessions can never collide.
    """
    if bars is None or bars.empty:
        return None
    idx = pd.DatetimeIndex(bars.index)
    if idx.tz is None:
        raise ValueError("fold_sessions needs tz-aware bar timestamps")
    decide = decision_instants(idx, tz, hh, mm, lead_min)
    keep = idx.tz_convert(tz) <= decide
    if not keep.any():
        return None
    raw = bars[keep]
    g = raw.groupby(decide[keep])
    out = pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(),
                        "Low": g["Low"].min(), "Close": g["Close"].last()})
    if "Volume" in raw.columns:
        out["Volume"] = g["Volume"].sum()
    out.index.name = "ts"
    return out
