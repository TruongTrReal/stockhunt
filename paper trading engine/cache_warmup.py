"""Warm a live book from the bars on disk instead of from the vendor's history window.

A live book recomputes its rule over a rolling buffer, so the buffer's DEPTH decides
whether the rule it computes is the rule that was scored. Until now that depth came
entirely from one REST call, and `td_live.OUTPUT_SIZE` caps a single call at 5,000 bars.
At `1d` that is twenty years and the cap never binds. At `15m` it is ten months, and a
rule whose lookback is expressed in DAYS — every published strategy takes its periods from
`_bars(bpy, days)` — simply does not fit: `tsmom12` wants twelve months, which on the
equity 15m grid is ~6,500 bars, so live it computed nothing while the backtest traded.

**The history was never missing; it was never asked for.** `data/` holds 41,773 fifteen
minute bars for AAPL back to 2020 and 227,766 for BTC/USD. This module hands them to the
book, and the vendor then supplies only the recent tail — which is what a vendor is
actually for.

    cache -----------------------------------|
                                             |---- vendor request ---- live bars
                            [ overlap: CHECKED, never assumed ]

**The seam is the whole risk and it is checked, not trusted.** Two series of the same
instrument from two routes agree or they do not, and if they do not the buffer is a
splice of two different histories with a step in it that no indicator can see. The cache
is written by `td_loader` with `adjust=all` and a corporate action lands in it at the next
fetch, so a cache that predates a split disagrees with the live feed by the split ratio.
`seam_report` compares every overlapping bar and the book refuses the cache — falling back
to the vendor-only path it has always used — rather than trading a spliced buffer.

**`cme_futures` is deliberately excluded.** Its bars are ratio back-adjusted, and the
anchor is whichever bar was newest when the series was written. The cache's anchor is the
last fetch; `db_live.fetch_bars` re-anchors its warm-up at the newest bar it returns. Those
are different constants, so splicing the two puts a roll-sized step in the middle of the
buffer — the exact defect back-adjustment exists to remove. The futures leg does not need
this anyway: its sheets are led by TA-Lib rules whose periods are integer literals, and
those reproduce at the buffer the desk already had.
"""

from __future__ import annotations

import os

import pandas as pd

import paper_config

# The switch that turns this off, in the shape the rest of the repo already uses
# (`STOCKHUNT_NO_POSCACHE`, `STOCKHUNT_WORKERS`). A warm-up path that cannot be disabled is
# one nobody can bisect against when a book starts behaving differently, and this one
# changes what a rule SEES — which is the most consequential thing a desk can change
# quietly.
DISABLED = bool(os.environ.get("STOCKHUNT_NO_CACHE_WARMUP"))

# How far the RATIO between the two series may wander, once its own median is divided out.
#
# **The test is not that the prices agree — it is that they agree up to ONE CONSTANT.**
# Both series are dividend-adjusted (`adjust=all`), and each is adjusted as of the day it
# was fetched, so a cache written three weeks ago and a live series adjusted today differ
# by every distribution in between. Over six years that compounds to whole percent, and
# comparing levels rejects the cache on every dividend-paying name — measured on the
# deployed desk, 134 rejections out of 141 symbols, all of them healthy series.
#
# Rescaling is exactly what back-adjustment is, and every price indicator here is
# equivariant under a common scale, so a spliced buffer is correct provided the constant is
# ONE constant. That is what this tolerance bounds: after dividing by the median ratio, no
# bar may disagree by more than this. A genuinely different history — the wrong instrument,
# a missing split, a truncated series — does not have a constant ratio and fails.
SEAM_TOLERANCE = 2e-3

# How many overlapping bars the check needs before it will decide anything.
#
# The first version judged on whatever the first callback delivered, which on the live desk
# was ONE bar. One bar always has a constant ratio, so the check could neither pass nor fail
# honestly — it was a coin toss wearing a measurement.
#
# 250 rather than a token handful, because the classes most likely to carry a revised print
# are the aggregated ones and a small sample cannot tell a revision from a divergence: at 30
# bars `XTZ/USD` read as 1.30% off and was refused, and over 250 the same series reconciles
# exactly. The caller waits for the FIRST LIVE BAR before asking, so a full vendor window is
# in hand and this floor costs nothing.
MIN_OVERLAP = 250

# How many overlapping bars to actually compare, once there are enough.
SEAM_BARS = 250

# Which quantile of the per-bar disagreement is the verdict. See `seam_report`: the maximum
# is hostage to one revised print, and the classes most likely to carry one are exactly the
# ones with no single venue behind them.
PERCENTILE = 0.95

# The classes whose cache may be spliced onto a live feed. `cme_futures` is out for the
# reason in the module docstring; it is named by exclusion rather than by listing the
# other four so that a sixth class arriving is included by default and its exclusion has
# to be an argued decision rather than an omission.
EXCLUDED_CLASSES = {"cme_futures"}


def usable(asset_class: str) -> bool:
    return not DISABLED and asset_class not in EXCLUDED_CLASSES


def _interval(timeframe: str) -> pd.Timedelta:
    n, unit = int(timeframe[:-1]), timeframe[-1]
    return pd.Timedelta(**{{"d": "days", "h": "hours", "m": "minutes"}[unit]: n})


def load(asset_class: str, timeframe: str, symbol: str,
         limit: int) -> list[dict]:
    """The newest `limit` cached bars for one symbol, in `book_strategy._bars` shape.

    **Stamped on the bar's CLOSE, because that is what a Nautilus bar carries.**
    `td_nautilus._to_bar` sets `ts_event = <vendor stamp> + <one interval>`, while the
    parquet on disk holds the vendor's OPEN stamp. Handing the buffer the open stamp put
    the two conventions one interval apart, so out of ~1,490 genuinely shared daily bars
    exactly ONE matched — by coincidence, at a boundary — and every symbol was judged on
    that single accident. The convention is converted here, at the one place that reads
    the file, rather than compared for at each call site.

    Empty when there is no cache for the cell, which is the normal state on a box that
    carries only `data/reference/` — the desk then warms from the vendor exactly as it
    always has. **A missing cache must never be an error**: this is an improvement to the
    warm-up, not a new dependency of the desk, and a box without the bars has to keep
    trading.
    """
    if not usable(asset_class) or limit <= 0:
        return []
    try:
        import td_loader
        frames = td_loader.load(asset_class, timeframe, [symbol])
    except Exception:
        # A missing folder, an unreadable parquet, a class the loader does not know: all of
        # them mean "no cache", and none of them is a reason to stop a book from starting.
        return []

    df = frames.get(symbol)
    if df is None or df.empty:
        return []
    df = df.tail(limit)
    step = _interval(timeframe)
    out: list[dict] = []
    for ts, row in df.iterrows():
        stamp = pd.Timestamp(ts)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        stamp = stamp + step
        out.append({"Open": float(row["Open"]), "High": float(row["High"]),
                    "Low": float(row["Low"]), "Close": float(row["Close"]),
                    "Volume": float(row.get("Volume", 0.0) or 0.0),
                    "ts": stamp})
    return out


def seam_report(cached: list[dict], arriving: list[dict],
                tolerance: float = SEAM_TOLERANCE) -> dict:
    """Are these two routes the same series, up to one constant?

    Returns `overlap`, `scale`, `worst`, `ok` and `reason`. `scale` is the constant that
    lifts the cached prices onto the live series' basis — the median of `live / cached`
    across the overlap — and `worst` is how far any single bar departs from it.

    **Levels are not compared and must not be.** Both sides carry `adjust=all`, applied as
    of the day each was fetched, so every dividend between the two fetches shifts the whole
    cached series by a constant. That is not a defect; it is the same history on a different
    basis, and back-adjustment is precisely multiplication by a constant. What would be a
    defect is the ratio not being constant — a missing split, a truncated history, the wrong
    instrument — and that is what this measures.

    An overlap below `MIN_OVERLAP` is **not** agreement. Too few bars always look
    consistent, and the first version of this decided on one bar and called it a
    measurement.
    """
    if not cached or not arriving:
        return {"overlap": 0, "scale": 1.0, "worst": 0.0, "ok": False,
                "reason": "one of the two routes returned nothing"}

    by_ts = {bar["ts"]: bar for bar in cached}
    pairs = [(live, by_ts[live["ts"]]) for live in arriving if live["ts"] in by_ts]
    if len(pairs) < MIN_OVERLAP:
        newest_cached, oldest_live = cached[-1]["ts"], arriving[0]["ts"]
        if oldest_live > newest_cached:
            why = (f"the cache ends {newest_cached} and the vendor window starts "
                   f"{oldest_live}, so there is a hole between them")
        else:
            why = (f"only {len(pairs)} bars line up, and {MIN_OVERLAP} are needed before "
                   f"a ratio means anything")
        return {"overlap": len(pairs), "scale": 1.0, "worst": 0.0, "ok": False,
                "reason": why}

    pairs = pairs[-SEAM_BARS:]
    ratios = []
    for live, cached_bar in pairs:
        base = cached_bar["Close"]
        if base:
            ratios.append(live["Close"] / base)
    if not ratios:
        return {"overlap": len(pairs), "scale": 1.0, "worst": 0.0, "ok": False,
                "reason": "the cached bars carry no usable close"}

    ratios.sort()
    scale = ratios[len(ratios) // 2]
    if not scale:
        return {"overlap": len(pairs), "scale": 1.0, "worst": 1.0, "worst_bar": 1.0,
                "ok": False, "reason": "the cached bars price at zero"}

    devs = sorted(abs(r / scale - 1.0) for r in ratios)
    # The DECISION statistic is a high percentile, not the maximum, and the difference
    # matters on the pair-quoted classes. `BTC/USD` has no single venue behind it — the
    # vendor aggregates, and a settled bar can be revised afterwards — so one print out of
    # 250 legitimately differs by tens of basis points while the other 249 agree exactly.
    # Judging on the maximum lets a single revision veto a whole healthy history, which is
    # the same "one bar decided it" failure this check was rewritten to remove, wearing the
    # opposite sign. A SYSTEMATIC difference moves most of the distribution and still fails.
    worst = devs[int(len(devs) * PERCENTILE)] if devs else 0.0
    worst_bar = devs[-1] if devs else 0.0
    ok = worst <= tolerance
    return {
        "overlap": len(pairs), "scale": scale, "worst": worst,
        "worst_bar": worst_bar, "ok": ok,
        "reason": "" if ok else (
            f"the two series are not one series rescaled: after dividing out the median "
            f"ratio they still disagree by {worst * 100:.2f}% on {len(pairs)} overlapping "
            f"bars. A missing split, a truncated history or the wrong instrument looks "
            f"like this. Re-fetch this symbol."),
    }


def splice(cached: list[dict], live: list[dict], scale: float,
           limit: int) -> list[dict]:
    """The cached history, lifted onto the live basis, in front of the live bars.

    Only bars strictly OLDER than the live window are kept: where both routes have a bar
    the live one wins, because it is the series everything after it will extend. The
    result is one series on one basis, newest `limit` bars.

    `scale` is applied to the four prices and not to volume — volume is a count and does
    not rescale under a price adjustment.
    """
    if not live:
        return list(cached)[-limit:]
    oldest = live[0]["ts"]
    out = [{**bar,
            "Open": bar["Open"] * scale, "High": bar["High"] * scale,
            "Low": bar["Low"] * scale, "Close": bar["Close"] * scale}
           for bar in cached if bar["ts"] < oldest]
    out.extend(live)
    return out[-limit:]


def depth_note(asset_class: str, timeframe: str, symbol: str) -> str:
    """One line for the log saying what the cache actually offers. Diagnostics only."""
    bars = load(asset_class, timeframe, symbol, limit=10 ** 9)
    if not bars:
        return f"{symbol} {timeframe}: no cached bars"
    return (f"{symbol} {timeframe}: {len(bars):,} cached bars, "
            f"{bars[0]['ts'].date()} -> {bars[-1]['ts'].date()}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="What the bar cache offers a live book.")
    ap.add_argument("--cls", default="us_stocks")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--symbols", default="")
    args = ap.parse_args()

    names = ([s.strip() for s in args.symbols.split(",") if s.strip()]
             or paper_config.book_universe(args.cls)[:5])
    print(f"  {args.cls} {args.tf} — window "
          f"{paper_config.window_bars(args.tf):,} bars, cache "
          f"{'usable' if usable(args.cls) else 'EXCLUDED for this class'}")
    for name in names:
        print("   ", depth_note(args.cls, args.tf, name))
