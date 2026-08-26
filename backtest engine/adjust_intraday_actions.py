"""Put an intraday equity series on the same adjustment basis as its own daily bars.

    python adjust_intraday_actions.py --class us_stocks --tf 1h 15m 5m --dry-run
    python adjust_intraday_actions.py --class us_stocks --tf 1h 15m 5m

**Twelve Data adjusts daily and intraday equity bars differently, and the gap is a return
nobody earned.** GE spun off GE Vernova on 2024-04-02. Its DAILY series carries the
adjustment back through history and prints -2.49% that day; its INTRADAY series does not,
and prints **-22.14%**. Every other day of the overlap agrees to a fraction of a basis
point, so this is not noise or a different session boundary -- it is one fabricated crash
sitting in the middle of a series, and a dip-buying rule will buy it and wait for a
recovery that cannot come because nothing fell.

Refetching does not help: measured 2026-08-26, a fresh pull returns the same basis. It is
what the vendor serves.

**This is the futures-roll problem in a different asset class, and it takes the same
answer.** `db_loader` ratio back-adjusts continuous contracts because a roll otherwise
hands a rule a return nobody earned -- WTI closing at 18.12 and the next print being 24.76
-- and records every adjustment in `data/reference/futures_rolls.csv`. Same defect, same
remedy, same ledger discipline.

**The daily series is the reference, and the factor is measured rather than looked up.**
No corporate-action feed is needed: the ratio of daily close to the last intraday close of
the same day IS the adjustment, and where the two bases differ it is flat and obvious --
GE's is 0.798 for every day from 2019 to 2024-04-01 and 1.000 for every day after. So the
factor is read off the data, the break date is where it changes, and a series whose ratio
is not FLAT on both sides of the break is refused rather than guessed at: a drifting ratio
means the intraday series is a different instrument (see `GEN`, `BNY` in CLAUDE.md), and
scaling one of those would launder an impostor into looking consistent.

Only bars BEFORE the break are scaled, by the constant that makes them agree with the
daily bars. Volume is untouched -- a share count is not a price.
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from config import CLASSES, DATA_DIR
from td_loader import cache_dir, safe_symbol

LEDGER = DATA_DIR / "reference" / "intraday_action_adjustments.csv"

# A day's daily/intraday close ratio must sit this far from 1.0 to count as a different
# basis rather than as the ordinary difference between an auction print and the last
# continuous trade (which the repo measures at 0.9-2.2 bp).
MIN_FACTOR_GAP = 0.02
# How far the pre-break ratio may wander from its own median before the series is called
# two INSTRUMENTS rather than two bases — measured p5 to p95, NOT min to max.
#
# Min-to-max is decided by a handful of days on which the last intraday print and the
# closing auction simply disagreed, and it varies with how thin the bars are: GE's is 4.6%
# at 1h and 8.5% at 15m, on identical underlying data. Reading that as real spread led to
# a threshold that admitted the series at one timeframe and rejected it at another, which
# is a property of the tolerance rather than of the instrument.
#
# The percentile spread says what the min-max hid — GE's pre-break ratio is FLAT, 0.19% to
# 0.25% across all three timeframes, so the two bases really do differ by one constant.
# And it separates the cases by three orders of magnitude: BNY reads 184-194% and GEN
# 241-395%, because those series are a different company, not a different basis. Anything
# under a few percent is a rebasing; anything over is a quarantine.
MAX_SIDE_SPREAD = 0.05
# Below this many days on either side there is not enough evidence to call a break.
MIN_SIDE_DAYS = 20
# The factor is taken from the days IMMEDIATELY before the break, not from the whole span.
# The defect being removed is one discontinuity on one date, so the number that removes it
# cleanly is the ratio as it stood going into that date; a five-year median would leave a
# residual step behind of exactly the dividend drift.
FACTOR_WINDOW = 20


def measure(daily: pd.DataFrame, intra: pd.DataFrame) -> dict | None:
    """`{break_ts, factor, ...}` if this series has two adjustment bases, else None."""
    last = intra.groupby(intra.index.normalize())["Close"].last()
    j = pd.concat([daily["Close"].rename("d"), last.rename("h")], axis=1).dropna()
    if len(j) < 2 * MIN_SIDE_DAYS:
        return None
    r = (j["d"] / j["h"]).replace([np.inf, -np.inf], np.nan).dropna()
    if r.empty or (r - 1.0).abs().max() < MIN_FACTOR_GAP:
        return None
    # The break is where the ratio last differs from ~1.0. Everything after it agrees with
    # the daily basis; everything before it is on the old one.
    off = r[(r - 1.0).abs() >= MIN_FACTOR_GAP]
    on = r[(r - 1.0).abs() < MIN_FACTOR_GAP]
    if len(off) < MIN_SIDE_DAYS or len(on) < MIN_SIDE_DAYS:
        return None
    # Must be a clean split in TIME, not interleaved: every off-basis day before every
    # on-basis day. Interleaving means something else is wrong.
    if off.index.max() >= on.index.min():
        return None
    # ...and the pre-break side must be flat RELATIVE TO ITSELF. A ratio that drifts by a
    # dividend yield is one instrument on two bases; one that drifts by a factor of three
    # is two instruments, and scaling that would launder an impostor into looking
    # consistent instead of leaving it to be quarantined.
    spread_rel = float((off.quantile(0.95) - off.quantile(0.05)) / abs(off.median()))
    if spread_rel > MAX_SIDE_SPREAD:
        return None
    return {"break_ts": on.index.min(),
            "factor": float(off.tail(FACTOR_WINDOW).median()),
            "n_before": int(len(off)), "n_after": int(len(on)),
            "spread_rel": spread_rel}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--class", dest="asset_class", required=True, choices=list(CLASSES))
    ap.add_argument("--tf", nargs="+", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--symbols", nargs="+", default=None)
    a = ap.parse_args()

    syms = a.symbols or CLASSES[a.asset_class]["symbols"]
    rows = []
    for tf in a.tf:
        n_adj = 0
        for s in syms:
            dp = cache_dir(a.asset_class, "1d") / f"{safe_symbol(s)}.parquet"
            hp = cache_dir(a.asset_class, tf) / f"{safe_symbol(s)}.parquet"
            if not (dp.exists() and hp.exists()):
                continue
            d, h = pd.read_parquet(dp), pd.read_parquet(hp)
            m = measure(d, h)
            if m is None:
                continue
            n_adj += 1
            mask = h.index < m["break_ts"]
            print(f"  {a.asset_class}/{tf} {s}: x{m['factor']:.4f} on {int(mask.sum()):,} "
                  f"bars before {m['break_ts'].date()}  "
                  f"({m['n_before']} d before / {m['n_after']} after, "
                  f"spread {m['spread_rel']:.2%})")
            rows.append({"class": a.asset_class, "tf": tf, "symbol": s,
                         "break": str(m["break_ts"].date()), "factor": m["factor"],
                         "bars_scaled": int(mask.sum()), "n_before": m["n_before"],
                         "n_after": m["n_after"]})
            if not a.dry_run:
                out = h.copy()
                for col in ("Open", "High", "Low", "Close"):
                    out.loc[mask, col] = out.loc[mask, col] * m["factor"]
                out.to_parquet(hp)
        print(f"{a.asset_class}/{tf}: {n_adj} series rebased"
              f"{' (dry run)' if a.dry_run else ''}")

    if rows and not a.dry_run:
        led = pd.DataFrame(rows)
        if LEDGER.exists():
            led = pd.concat([pd.read_csv(LEDGER), led], ignore_index=True)
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        led.to_csv(LEDGER, index=False)
        print(f"ledger -> {LEDGER} ({len(rows)} entries)")
    elif not rows:
        print("nothing to rebase")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
