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

import pandas as pd

import paper_config

# How closely a cached bar and a vendor bar must agree on the overlap, as a fraction of
# price. 5 bp is far wider than floating-point noise and far tighter than any real defect:
# an unapplied split is tens of percent, a stale adjustment percent-sized, a vendor
# revision of a settled bar a handful of basis points at worst. It is a tripwire for "these
# are two different series", not a tolerance on quoting.
SEAM_TOLERANCE = 5e-4

# How many overlapping bars to compare. Enough that one revised print cannot pass the
# check on its own, few enough that the comparison costs nothing.
SEAM_BARS = 20

# The classes whose cache may be spliced onto a live feed. `cme_futures` is out for the
# reason in the module docstring; it is named by exclusion rather than by listing the
# other four so that a sixth class arriving is included by default and its exclusion has
# to be an argued decision rather than an omission.
EXCLUDED_CLASSES = {"cme_futures"}


def usable(asset_class: str) -> bool:
    return asset_class not in EXCLUDED_CLASSES


def load(asset_class: str, timeframe: str, symbol: str,
         limit: int) -> list[dict]:
    """The newest `limit` cached bars for one symbol, in `book_strategy._bars` shape.

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
    out: list[dict] = []
    for ts, row in df.iterrows():
        stamp = pd.Timestamp(ts)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        out.append({"Open": float(row["Open"]), "High": float(row["High"]),
                    "Low": float(row["Low"]), "Close": float(row["Close"]),
                    "Volume": float(row.get("Volume", 0.0) or 0.0),
                    "ts": stamp})
    return out


def seam_report(cached: list[dict], arriving: list[dict],
                tolerance: float = SEAM_TOLERANCE) -> dict:
    """Do the two routes agree where they overlap?

    Returns `{"overlap": n, "worst": frac, "symbol_ok": bool, "reason": str}`. An overlap
    of zero is **not** agreement — it means the vendor window starts after the cache ends,
    so there is a hole between them and the buffer would be two series with a gap rather
    than one series. That is reported as a failure, because a gap in the middle of a
    rolling buffer is exactly as wrong as a step and much easier to miss.
    """
    if not cached or not arriving:
        return {"overlap": 0, "worst": 0.0, "ok": False,
                "reason": "one of the two routes returned nothing"}

    by_ts = {bar["ts"]: bar for bar in cached}
    pairs = [(bar, by_ts[bar["ts"]]) for bar in arriving if bar["ts"] in by_ts]
    pairs = pairs[:SEAM_BARS]
    if not pairs:
        first, last = arriving[0]["ts"], cached[-1]["ts"]
        if first > last:
            return {"overlap": 0, "worst": 0.0, "ok": False,
                    "reason": (f"no overlap — the cache ends {last} and the vendor window "
                               f"starts {first}, so there is a hole between them")}
        return {"overlap": 0, "worst": 0.0, "ok": False,
                "reason": "no overlapping bars to compare"}

    worst, where = 0.0, None
    for live_bar, cached_bar in pairs:
        base = abs(cached_bar["Close"]) or 1.0
        diff = abs(live_bar["Close"] - cached_bar["Close"]) / base
        if diff > worst:
            worst, where = diff, live_bar["ts"]
    ok = worst <= tolerance
    return {
        "overlap": len(pairs), "worst": worst, "ok": ok,
        "reason": "" if ok else (
            f"the cached and live bars disagree by {worst * 100:.2f}% at {where}, over "
            f"{len(pairs)} overlapping bars. That is a different series, not a revision — "
            f"most likely a corporate action the cache predates. Re-fetch this symbol."),
    }


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
