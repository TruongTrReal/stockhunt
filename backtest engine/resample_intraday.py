"""Materialise 2m/3m bars from the cached 1m — the vendor serves no such interval.

    python resample_intraday.py --class crypto --tf 2m 3m
    python resample_intraday.py                              # every class with a 1m cache

Derived, never fetched: `td_loader.fetch` refuses `2m`/`3m` outright (Twelve Data has no
2min/3min product), and `td_loader.load` reads whatever this writes, so the whole
pipeline sees these bars through the same single door as every other timeframe.

Three properties of the aggregation are load-bearing:

* **Windows are anchored at midnight and labelled by their OPEN**, matching the vendor's
  own convention in the 1m cache (the first stock bar of a day is 09:30). 09:30 is 570
  minutes past midnight, which divides by both 2 and 3, so a midnight anchor aligns
  every equity session's first 2m/3m bar exactly at the open — nothing straddles 09:29.
* **Empty windows are dropped, not filled.** A US equity session therefore ends cleanly
  at 15:58 (2m) / 15:57 (3m), and no synthetic bar bridges the overnight gap or a
  weekend. Bars-per-year is *measured* downstream (`vector.bars_per_year`), so the
  ragged last window of a session costs nothing.
* **OHLC aggregates as first/max/min/last and Volume as a sum that keeps NaN NaN.**
  Crypto volume is all-NaN in the 1m cache (the vendor serves none for the class);
  `sum(min_count=1)` preserves that fact instead of laundering it into a zero, which
  would silently turn "no volume data" into "zero turnover" for every volume-gated rule.

**A vendor interval may be built here too, and since 2026-08-23 `15m` is** — not because
Twelve Data cannot sell it, but because a fetched sheet and the 1m cache beside it are
adjusted on **different days and therefore to different bases**. `adjust=all` back-adjusts
for splits AND dividends, so a 15m file pulled in July and a 1m file pulled in August
differ by every distribution in between: measured on AAPL, our 15m from 1m matches the
vendor's stored 15m at a **constant ratio of 0.999128 in every year from 2020 to 2026** —
one dividend, applied to one file and not the other. A constant ratio is the proof that
the aggregation itself is exact; it is also the proof that the two files cannot be mixed.
Deriving every intraday size from one 1m cache gives the whole class one basis.

The aggregation was checked against the vendor's own bars before it was trusted: on the
symbols holding both, `ABBV`, `AMZN`, `ADA/USD`, `AVAX/USD` and `BNB/USD` agree to
**0.00 bp on open, high, low and close** over 41,000-221,000 bars each.

**Not every size may be derived, and the guard is arithmetic.** Windows anchor at midnight,
so a US equity session aligns only when the target divides the open's offset: 09:30 is
**570** minutes past midnight, and 570 is divisible by 2, 3, 5, 15 and 30 but **not by 60**
— a 1h grid cut this way would put every equity session's first bar 30 minutes before the
open, straddling it. So `main` refuses a size that does not divide 570 on the exchange-local
classes; 1h and 4h are fetched, never resampled. The 24-hour classes have no such
constraint, and the check is skipped for them.

Superseded output is overwritten whole. Rerun after any 1m refetch.
"""

from __future__ import annotations

import argparse

import pandas as pd

from config import CLASSES, TIMEFRAMES
from td_loader import cache_dir, safe_symbol


AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}

# Which classes carry EXCHANGE-LOCAL timestamps in the 1m cache rather than UTC. Twelve
# Data returns the exchange's own clock and Databento returns UTC, and neither stamps the
# zone on the parquet, so this is the only place the difference is written down outside
# `CLAUDE.md`. It matters here and nowhere else in this file: the 570 rule below applies
# to a 09:30 open, which is a fact about ET, not about the calendar day.
EXCHANGE_LOCAL = {"us_stocks": True, "us_etfs": True}


def resample_frame(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """One symbol's 1m frame -> its N-minute frame. Pure; tested on synthetic bars."""
    rule = f"{minutes}min"
    out = df.resample(rule, label="left", closed="left", origin="start_day").agg(
        AGG | ({"Volume": "sum"} if "Volume" in df.columns else {}))
    if "Volume" in df.columns:
        out["Volume"] = df["Volume"].resample(
            rule, label="left", closed="left", origin="start_day").sum(min_count=1)
    out = out.dropna(subset=["Close"])
    out.index.name = df.index.name
    return out[df.columns.tolist()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", default=None,
                    help="default: every class whose 1m cache is non-empty")
    # Derivable sizes: everything the vendor does not sell (2m/3m), plus the intraday
    # sizes it does sell that we would rather build than buy so the whole class shares one
    # adjustment basis. `1h`/`4h` are deliberately absent -- see the docstring's 570 rule.
    # `4h` is offered but the 570 rule below refuses it on the exchange-local classes
    # (570 % 240 = 90), so it is reachable only for the 24-hour ones -- which is exactly
    # where it is wanted: `cme_futures` has no 4h sheet because the vendor's hourly
    # archive is holed before 2013, and the 1m cache this builds from starts in 2016,
    # after that defect ends.
    ap.add_argument("--tf", nargs="+", default=["2m", "3m"],
                    choices=sorted({t for t, s in TIMEFRAMES.items()
                                    if s["interval"] is None}
                                   | {"5m", "15m", "30m", "4h"}))
    args = ap.parse_args()

    classes = args.classes or [c for c in CLASSES
                               if any(cache_dir(c, "1m").glob("*.parquet"))]
    for asset_class in classes:
        src = cache_dir(asset_class, "1m")
        files = sorted(src.glob("*.parquet"))
        if not files:
            print(f"{asset_class}: no 1m cache, skipped")
            continue
        for tf in args.tf:
            minutes = int(tf[:-1]) * (60 if tf.endswith("h") else 1)
            # The 570 rule. A US equity session opens 570 minutes past midnight and these
            # windows anchor at midnight, so a size that does not divide 570 puts the
            # session's first bar astride the open -- half of it pre-market, and every
            # later bar off the vendor's own grid by the remainder. Refused rather than
            # warned about: the output would be well formed and silently misaligned,
            # which is the failure this repo keeps paying for.
            if EXCHANGE_LOCAL.get(asset_class) and 570 % minutes:
                print(f"{asset_class}/{tf}: refused - {minutes}m does not divide the "
                      f"570-minute open offset, so every session would straddle 09:30. "
                      f"Fetch this size instead.")
                continue
            out_dir = cache_dir(asset_class, tf)
            out_dir.mkdir(parents=True, exist_ok=True)
            rows = 0
            for path in files:
                df = pd.read_parquet(path)
                res = resample_frame(df, minutes)
                res.to_parquet(out_dir / f"{safe_symbol(path.stem)}.parquet")
                rows += len(res)
            print(f"{asset_class}/{tf}: {len(files)} symbols, {rows:,} bars -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
