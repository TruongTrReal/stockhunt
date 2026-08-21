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

Superseded output is overwritten whole. Rerun after any 1m refetch.
"""

from __future__ import annotations

import argparse

import pandas as pd

from config import CLASSES, TIMEFRAMES
from td_loader import cache_dir, safe_symbol


AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}


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
    ap.add_argument("--tf", nargs="+", default=["2m", "3m"],
                    choices=[t for t, s in TIMEFRAMES.items() if s["interval"] is None])
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
            minutes = int(tf.rstrip("m"))
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
