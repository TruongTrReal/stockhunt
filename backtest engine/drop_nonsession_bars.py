"""Remove daily bars stamped on days the exchange was shut.

    python drop_nonsession_bars.py --class us_stocks us_etfs --dry-run
    python drop_nonsession_bars.py --class us_stocks us_etfs

**A US equity does not trade on a Saturday or a Sunday, so a daily bar stamped with one is
not a thin session — it is the vendor putting a number on a day that did not happen.** The
worked example is `CVS` on 2009-07-26, a Sunday: Open, High, Low and Close all exactly
56.4167 on zero volume, while the stock closed at 23.07 the Friday before and opened at
23.03 the Monday after. Nothing about that bar is a price CVS ever traded at, and it sits
inside the backtest window with a +145% return into it and a -59% return out of it.

**No existing check can see this, and each for a structural reason.** `check_data` asks
whether a bar is well formed — this one is, perfectly. `repair_spikes` asks whether a bar
agrees with its neighbours — but its daily-side adjudicator is the intraday cache, which on
equities begins in 2019 and so spares 2009 by design. `verify_intraday_vs_daily` asks
whether intraday reaches the daily extremes, and it is the DAILY bar that is wrong here.
The defect is only visible against the exchange calendar, which nothing else consults.

**Weekend bars are an impostor tell, not just a data fault.** Ranked by count they are
`INFO` 364, `APC` 343 — recycled tickers that resolve to somebody else, exactly the family
`check_data --probe-listing` quarantines. A ticker whose series contains hundreds of
Sundays is being served from a venue with a different week. Dropping the bars does not make
such a series usable; quarantine does. This tool fixes the bars and leaves that judgement
where it belongs.

**Only weekends, and only the exchange-local classes.** `crypto` trades every day of the
week and `cme_futures` opens on Sunday evening, so both are refused outright rather than
silently skipped. Holidays are deliberately NOT handled: that needs a real exchange calendar
with its historical changes, and half a calendar would delete good sessions. A weekend is
the part that is certain, and this tool does only the certain part.
"""

from __future__ import annotations

import argparse

import pandas as pd

from config import CLASSES, DATA_DIR
from td_loader import cache_dir

LEDGER = DATA_DIR / "reference" / "nonsession_bars.csv"

# Classes whose bars carry an EXCHANGE-LOCAL calendar with a Mon-Fri week. `crypto` is 24/7
# and `cme_futures` sessions open Sunday evening; a weekend bar in either is legitimate.
WEEKDAY_ONLY = {"us_stocks", "us_etfs"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--class", dest="classes", nargs="+", required=True,
                    choices=sorted(WEEKDAY_ONLY))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    rows = []
    for cls in a.classes:
        if cls not in WEEKDAY_ONLY:
            raise SystemExit(f"{cls} does not have a Monday-to-Friday week; refusing.")
        d = cache_dir(cls, "1d")
        files = sorted(d.glob("*.parquet"))
        touched = total = 0
        for path in files:
            df = pd.read_parquet(path)
            we = df.index.dayofweek >= 5
            if not we.any():
                continue
            touched += 1
            total += int(we.sum())
            for ts in df.index[we]:
                r = df.loc[ts]
                rows.append({"class": cls, "symbol": path.stem, "ts": str(ts.date()),
                             "weekday": ts.day_name(), "open": float(r["Open"]),
                             "close": float(r["Close"]),
                             "volume": float(r["Volume"]) if "Volume" in df else float("nan")})
            if not a.dry_run:
                df[~we].to_parquet(path)
        print(f"{cls}/1d: {total} weekend bars on {touched} of {len(files)} symbols"
              f"{' (dry run)' if a.dry_run else ' dropped'}")

    if rows and not a.dry_run:
        led = pd.DataFrame(rows)
        if LEDGER.exists():
            led = pd.concat([pd.read_csv(LEDGER), led], ignore_index=True).drop_duplicates()
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        led.to_csv(LEDGER, index=False)
        print(f"ledger -> {LEDGER} ({len(rows)} entries)")
    elif not rows:
        print("nothing to drop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
