"""Does each intraday cache still contain the days the DAILY cache says happened?

    python verify_intraday_vs_daily.py --class crypto --tf 4h 1h 15m 5m 1m
    python verify_intraday_vs_daily.py --class crypto --tf 1h --date 2025-10-10
    python verify_intraday_vs_daily.py --class crypto --tf 1h --against ../data/_crypto_prerefetch

**The defect this exists for is agreement between timeframes, which nothing else checks.**
Every integrity test in this folder reads one series at a time: `check_data` asks whether a
bar is well formed, `repair_spikes` asks whether a bar agrees with its neighbours. Neither
can see a series that is internally perfect and quietly missing an event its own daily bars
record. That is the `EEM` signature one level down, and it happened here: an early
`repair_spikes` pass clamped the real 2025-10-10 liquidation cascade out of crypto 1h, 15m
and 5m, leaving DOT with a daily low of 0.633 and an intraday low of 2.452 -- 19 of 34
cached pairs affected at 4h alone.

**The daily bar is the adjudicator, and the test is one-sided.** An intraday series must
reach AT LEAST as far as the daily bar for the same date: a day's low is the lowest price
of that day whichever way you slice it. Intraday going FURTHER is not an error -- a daily
bar can be built from a different session boundary, and `Close` legitimately differs
because the daily close is an auction print. So this flags only the direction that means
data was destroyed, never the direction that means the two were aggregated differently.

`--against` runs the same test over a second cache root and prints them side by side, which
is how a repair or a refetch is proved to have helped rather than assumed to have.

Symbols are read off `config`'s universe for the class, so files left behind by a previous,
wider universe are reported separately rather than counted as failures -- they are inert
(`td_loader.load` filters to the universe) but they are still damaged, and deleting price
data is not this tool's decision to make.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from config import CLASSES
from td_loader import cache_dir, safe_symbol

# How far short of the daily extreme an intraday series may fall before it is called
# destroyed. Generous: session boundaries and rounding move an extreme by basis points,
# and what this looks for moves it by half.
TOL = 0.05


def _extremes(df: pd.DataFrame, day: pd.Timestamp,
              back_hours: int = 0) -> tuple[float, float] | None:
    lo_edge = day - pd.Timedelta(hours=back_hours)
    w = df.loc[str(lo_edge):str((day + pd.Timedelta(days=1)).date())]
    if not len(w):
        return None
    return float(w["Low"].min()), float(w["High"].max())


# How far back a session may reach before the date its DAILY bar is stamped with.
#
# **A CME session opens the previous evening, and the daily bar carries the whole of it.**
# CL's daily bar for 2020-03-16 has a High of 32.9267 — which is in the intraday bars
# stamped 2020-03-15 22:00 and 23:00 UTC, the Sunday-evening open. Comparing UTC calendar
# day to UTC calendar day therefore reports the crash-day High as 6% missing on three roots
# at once, and it is not missing: the two UTC days together reproduce the daily High and Low
# EXACTLY. Twelve Data's crypto and commodities are stamped on the same UTC day the daily
# bar is, so they need none of this; equities are exchange-local and intraday, likewise.
#
# This is a reporting distinction, not a relaxation — a series that reconciles only once the
# previous evening is included is counted and named separately, never folded into "ok".
SESSION_BACK_HOURS = {"cme_futures": 6}


def scan(intr: Path, asset_class: str, dates: list[pd.Timestamp], live: set[str]) -> dict:
    """One directory of intraday parquets against the class's DAILY cache.

    Takes the directory rather than a cache root so the live tree and an `--against`
    snapshot go through exactly the same code — two scanners would be two definitions of
    "short", and the whole value here is that the comparison is like for like.
    """
    daily = cache_dir(asset_class, "1d")
    back = SESSION_BACK_HOURS.get(asset_class, 0)
    out: dict = {"short": [], "ok": 0, "stale": [], "nodata": 0, "session": []}
    if not intr.exists():
        return out
    for f in sorted(intr.glob("*.parquet")):
        dpath = daily / f.name
        if not dpath.exists():
            out["nodata"] += 1
            continue
        d, n = pd.read_parquet(dpath), pd.read_parquet(f)
        in_universe = any(safe_symbol(s) == f.stem for s in live)
        worst = None
        session_only = False
        # The comparison window spans TWO calendar days (see `_extremes`), so at the head and
        # tail of an intraday series the two sides stop covering the same time. On the first
        # day, the daily side contributes the PREVIOUS session's extremes while the intraday
        # side can only contribute the first — and the difference is reported as destroyed
        # data. It bit hard: `us_stocks` 1m begins 2020-03-25 because that is the vendor's 1m
        # depth, 15m and 5m are derived from it and inherit that start, and 2020-03-24 was
        # the +11% session after the COVID bottom. 80 of 216 names were flagged "short" for a
        # day their series does not claim to cover, which reads exactly like a real defect.
        #
        # So the daily bar only adjudicates days the intraday series actually spans.
        if not len(n):
            out["nodata"] += 1
            continue
        first_day, last_day = n.index.min().normalize(), n.index.max().normalize()
        for day in dates:
            if day.normalize() < first_day or day.normalize() >= last_day:
                continue
            de, ne = _extremes(d, day), _extremes(n, day)
            if de is None or ne is None:
                continue
            # Only "did not reach". Overshoot is a different session boundary, not lost
            # data, and flagging it would bury the failure that matters in noise.
            #
            # WHICH SIDE FAILED IS PART OF THE FINDING, not a detail. Taking the max of the
            # two gaps and then printing the LOW pair regardless produced lines reading
            # "daily low 0.3091 vs intraday 0.3091 (10% short)" — identical numbers under a
            # 10% gap — because the gap was on the High. That is unreadable, and worse, it
            # points an investigation at the wrong end of the bar.
            gap_lo = (ne[0] - de[0]) / de[0] if de[0] else 0.0
            gap_hi = (de[1] - ne[1]) / de[1] if de[1] else 0.0
            side, gap, dv, nv = (("Low", gap_lo, de[0], ne[0]) if gap_lo >= gap_hi
                                 else ("High", gap_hi, de[1], ne[1]))
            if gap <= TOL:
                continue
            # Before calling it lost, allow the session to have opened the previous evening.
            if back:
                we = _extremes(n, day, back_hours=back)
                if we is not None:
                    g2 = max((we[0] - de[0]) / de[0] if de[0] else 0.0,
                             (de[1] - we[1]) / de[1] if de[1] else 0.0)
                    if g2 <= TOL:
                        session_only = True
                        continue
            if worst is None or gap > worst[1]:
                worst = (day, gap, dv, nv, side)
        if worst is not None and in_universe:
            out["short"].append((f.stem, *worst))
        elif worst is not None:
            out["stale"].append(f.stem)
        elif session_only:
            out["session"].append(f.stem)
        else:
            out["ok"] += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--class", dest="asset_class", required=True, choices=list(CLASSES))
    ap.add_argument("--tf", nargs="+", required=True)
    ap.add_argument("--date", nargs="+", default=None,
                    help="days to check (default: every day the DAILY bars show a >25%% range)")
    ap.add_argument("--against", default=None,
                    help="a second cache root to compare with, e.g. ../data/_crypto_prerefetch")
    a = ap.parse_args()

    live = set(CLASSES[a.asset_class]["symbols"])
    if a.date:
        dates = [pd.Timestamp(d) for d in a.date]
    else:
        # The days worth checking are the violent ones: a bar cannot lose an event it never
        # had, and a quiet day's extremes agree trivially.
        dates = set()
        dd = cache_dir(a.asset_class, "1d")
        for s in live:
            f = dd / f"{safe_symbol(s)}.parquet"
            if not f.exists():
                continue
            d = pd.read_parquet(f)
            rng = (d["High"] - d["Low"]) / d["Low"].where(d["Low"] > 0)
            dates.update(d.index[rng > 0.25])
        dates = sorted(dates)
    print(f"{a.asset_class}: checking {len(dates)} high-range day(s) "
          f"against {len(live)} live symbols, tolerance {TOL:.0%}\n")

    bad_total = 0
    for tf in a.tf:
        now = scan(cache_dir(a.asset_class, tf), a.asset_class, dates, live)
        line = f"  {tf:4s}  now: {len(now['short'])} short / {now['ok']} ok"
        if a.against:
            was = scan(Path(a.against) / tf, a.asset_class, dates, live)
            line += f"   (was: {len(was['short'])} short / {was['ok']} ok)"
        if now.get("session"):
            line += f"   [{len(now['session'])} reconcile only with the prior evening]"
        if now["stale"]:
            line += f"  [+{len(now['stale'])} stale, outside universe]"
        print(line)
        for sym, day, gap, dv, nv, side in sorted(now["short"], key=lambda x: -x[2])[:8]:
            print(f"        {sym:10s} {str(day.date())}  daily {side} {dv:.4f} vs "
                  f"intraday {nv:.4f}  ({gap:.0%} short)")
        bad_total += len(now["short"])
    print()
    print("OK — every live series reaches its own daily extremes" if not bad_total
          else f"{bad_total} series-timeframe(s) fall short of the daily bars")
    return 1 if bad_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
