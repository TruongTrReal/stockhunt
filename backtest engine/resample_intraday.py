"""Materialise 2m/3m bars from the cached 1m — the vendor serves no such interval.

    python resample_intraday.py --class crypto --tf 2m 3m
    python resample_intraday.py                              # every class with a 1m cache
    python resample_intraday.py --class commodities --from 1h --tf 4h

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

**The source timeframe is an argument, and `4h` on commodities is why.** That class's bars
were stamped in `Australia/Sydney` and are now restamped to UTC (see
`config.INTRADAY_CLOCK`); the offset is a whole number of hours, so `1m`/`5m`/`15m`/`1h`
stay exactly on the UTC grid under it and only their labels move. **`4h` does not**: the
offset is 10 or 11 hours, `10 % 4 == 2` and `11 % 4 == 3`, so a relabelled 4h bar would sit
at 02:00/06:00/... UTC in summer and 01:00/05:00/... in winter — not the same windows any
other class's 4h bars cover, and not even the same windows as itself across a DST change.
It has to be rebuilt on the real grid, and it is rebuilt from **`1h`, not `1m`**: the 1h
cache holds all five commodity symbols from 2020-01-20 while the 1m cache holds three from
2020-10-01, so deriving from 1m would silently delete two metals and nine months.

Superseded output is overwritten whole. Rerun after any refetch of the source timeframe.
"""

from __future__ import annotations

import argparse

import pandas as pd

import config
from config import CLASSES, TIMEFRAMES
from td_loader import cache_dir, safe_symbol


AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}

# Which classes carry EXCHANGE-LOCAL timestamps in the cache rather than UTC. It matters
# here and nowhere else in this file: the 570 rule below is about a 09:30 open, which is a
# fact about ET and not about the calendar day.
#
# **Derived from `config.INTRADAY_CLOCK`, not restated.** This used to be a hand-written
# `{"us_stocks": True, "us_etfs": True}` and it was the only place outside `CLAUDE.md` the
# difference was written down — which is how the docs came to claim commodities were UTC
# while the vendor was stamping them in `Australia/Sydney`. One declaration, read from it.
EXCHANGE_LOCAL = {c: config.cache_tz(c) != "UTC" for c in CLASSES}


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
    # Which cached size to aggregate FROM. Defaults to 1m, which is every case but one:
    # commodity `4h` is rebuilt from `1h` because the 1m cache is missing two of the five
    # symbols and nine months of the other three. See the docstring.
    ap.add_argument("--from", dest="source_tf", default="1m",
                    choices=[t for t, s in TIMEFRAMES.items() if s["intraday"]])
    args = ap.parse_args()

    src_minutes = int(args.source_tf[:-1]) * (60 if args.source_tf.endswith("h") else 1)
    classes = args.classes or [c for c in CLASSES
                               if any(cache_dir(c, args.source_tf).glob("*.parquet"))]
    for asset_class in classes:
        src = cache_dir(asset_class, args.source_tf)
        files = sorted(src.glob("*.parquet"))
        if not files:
            print(f"{asset_class}: no {args.source_tf} cache, skipped")
            continue
        for tf in args.tf:
            minutes = int(tf[:-1]) * (60 if tf.endswith("h") else 1)
            # An aggregation can only ever be exact when the source bar tiles the target
            # window. Refused rather than warned about, for the same reason as the 570
            # rule below: the output would be well formed and quietly wrong.
            if minutes % src_minutes or minutes == src_minutes:
                print(f"{asset_class}/{tf}: refused - cannot build a {minutes}m bar out "
                      f"of {src_minutes}m bars")
                continue
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
