"""When does each commodity's Open become a price, rather than a placeholder?

    python commodity_entry.py            # measure and print
    python commodity_entry.py --write    # ...and write data/reference/commodity_entry.csv

**Twelve Data serves gold and silver with a fabricated Open for the first half of their
history.** On every bar from the start of each series to early 2006, the Open is exactly
equal to the High or exactly equal to the Low — 100% of bars, month after month, for 27
years of XAU and 24 of XAG. A real Open lands on an extreme sometimes: after the break the
rate settles at 0-15%, which is the honest base rate for a quiet or gapping session. Before
it, the field is not a measurement.

**`check_data --fix` makes this worse, not better, and that is why a cut is the answer.**
Its repair widens High/Low to contain Open, because an OHLC bar whose Open sits outside its
range is malformed. Applied to a fabricated Open that is the whole point of the defect, the
repair propagates it: the bar becomes self-consistent by moving the extremes to the bad
number. So after an integrity pass these years have a fabricated Open AND fabricated
extremes, and nothing downstream can tell.

**It is inside the backtest window.** `BACKTEST_START` is 2000-01-01, so six years of gold
and silver carry it, and `--fill open` prices entries at a number nobody traded — on the
one class where the pessimistic fill is most often quoted.

**The cut is measured, not chosen.** The entry date is the first bar after the last 21-bar
window in which at least half the Opens sit on an extreme. That is deliberately a REGIME
test rather than a last-bad-bar test: single fabricated-looking bars keep occurring after
the break at the base rate, and cutting to the last of those would delete good years to
chase noise. Gold enters 2006-02-03 and silver 2006-02-06, four sessions apart, which is
itself evidence the break is one vendor event and not two coincidences.

Roughly 55% of each series goes. What remains is ~20.6 years, which is more history than
`us_etfs` carries and about four years more than `cme_futures`.

Platinum and palladium are clean and uncut: both start 2012, well after the break.

The output follows `etf_entry.csv` exactly — `symbol,entry,reason`, head cut only, and a
name absent from the file means no cut — so `td_loader.commodity_entry_span` reads it the
same way `etf_entry_span` reads the ETF screen.
"""

from __future__ import annotations

import argparse

import pandas as pd

from config import CLASSES, DATA_DIR
from td_loader import cache_dir, safe_symbol

OUT = DATA_DIR / "reference" / "commodity_entry.csv"

# A bar whose Open is exactly its High or exactly its Low. Legitimate sometimes; universal
# only when the field is synthetic.
WINDOW = 21          # sessions in the regime test — one trading month
THRESHOLD = 0.50     # ...at or above which the window is still the fabricated regime


def entry_for(df: pd.DataFrame) -> tuple[pd.Timestamp | None, float]:
    """`(first trustworthy date, share of bars cut)`, or `(None, 0.0)` if never fabricated."""
    on_extreme = (df["Low"] == df["Open"]) | (df["High"] == df["Open"])
    roll = on_extreme.rolling(WINDOW).mean()
    bad = roll[roll >= THRESHOLD]
    if bad.empty:
        return None, 0.0
    after = df.index[df.index > bad.index.max()]
    if not len(after):
        return None, 0.0
    return after[0], float((df.index < after[0]).mean())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    rows = []
    for s in CLASSES["commodities"]["symbols"]:
        f = cache_dir("commodities", "1d") / f"{safe_symbol(s)}.parquet"
        if not f.exists():
            continue
        df = pd.read_parquet(f)
        entry, cut = entry_for(df)
        if entry is None:
            print(f"  {s:9s} clean, no cut          "
                  f"({df.index.min().date()}..{df.index.max().date()})")
            continue
        kept = df.index[df.index >= entry]
        print(f"  {s:9s} entry {entry.date()}  cuts {cut:.0%} of bars, "
              f"keeps {len(kept):,} ({(kept.max() - entry).days / 365.25:.1f} yrs)")
        rows.append({"symbol": s, "entry": entry.date(),
                     "reason": f"Open is exactly High or Low on >={THRESHOLD:.0%} of every "
                               f"{WINDOW}-bar window before this date -- the field is "
                               f"synthetic, and check_data's repair widens High/Low onto it"})

    if a.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(OUT, index=False)
        print(f"\nwrote {OUT} ({len(rows)} cut, "
              f"{len(CLASSES['commodities']['symbols']) - len(rows)} uncut)")
    else:
        print("\n(dry run — pass --write to record it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
