"""Remove vendor bad ticks from an intraday cache, one BAR at a time, with a ledger.

    python repair_spikes.py --class crypto --tf 1m 15m 1h 4h --dry-run
    python repair_spikes.py --class crypto --tf 1m 15m 1h 4h

**Why this exists.** Twelve Data's intraday crypto carries bad ticks that the repo's other
checks structurally cannot see. Measured 2026-08-23: 8 of 20 pairs, clustered in one week
of December 2023, where a coin "crashes" 78-99% and recovers within minutes -- SOL prints
a 4h low of 0.000002 against neighbours near 65, TRX drops 0.10434 -> 0.001105 and back.
The DAILY bars are clean; every intraday timeframe is not.

Nothing already here catches them:

* `check_data`'s OHLC scan passes: High >= Low and the body is inside the range. The bars
  are internally consistent and completely wrong, which is the `CTRA`/`EEM` signature.
* `SPIKE_QUARANTINE` fires at +900% in ONE bar and its remedy is to drop the whole symbol.
  A 78% crash is under the line, and 8 of 20 pairs is not a universe you can tidy away.
* `truncate_after_hole` is the closest precedent and the right instinct -- surgical, not
  wholesale -- but it cuts a series at a data outage, not a handful of bars inside one.

**Why it matters more here than a wrong price usually would.** IBS, this repo's headline
signal, is `(Close - Low) / (High - Low)`. A bogus Low pins it near 1.0 no matter where
the bar really closed: measured on SOL 2023-12-12, IBS reads 0.983 where a neighbour-
bounded Low gives 0.683. The defect does not add noise, it manufactures signal.

**The test is spike-AND-REVERT, never size alone.** A crypto pair can genuinely move 50%,
and a rule that only asked "is this bar far from its neighbours" would delete the real
ones -- LUNA, the 2025-10-10 flash crash. So a bar is condemned only when it is far from a
CENTRED rolling median (which follows a sustained move, and which one outlier cannot drag)
*and* the bars either side of it sit close to that same median. A real move takes its
neighbours with it; a bad tick does not.

Two repairs, because two things break:

* **body** -- Open or Close is itself wrong (BCH 2023-12-13 closes at 50.9 between two
  232s). The bar is DROPPED. Nothing is invented: a dropped bar is a bar the engine never
  sees, exactly as with a folded futures session, and `vector.bars_per_year` is measured
  rather than assumed so a hole costs nothing.
* **wick** -- the body is fine and only High or Low is absurd. The offending bound is
  CLAMPED to the body's own extreme, which is a price that certainly traded.

Every change is written to `data/reference/spike_repairs.csv` before the parquet is
touched, so the edit is auditable and reversible symbol by symbol.
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from config import CLASSES, DATA_DIR
from td_loader import cache_dir, safe_symbol

LEDGER = DATA_DIR / "reference" / "spike_repairs.csv"

# Centred, so a sustained move carries the median with it and only an isolated bar stands
# out. Odd, so the window has a true centre. 21 bars is ~5h at 15m and ~20m at 1m -- long
# enough to be robust, short enough to track a real trend.
WINDOW = 21
# How far from the local median a body must sit to be called wrong. Deliberately blunt:
# crypto moves, and anything under this is a market, not a fault.
BODY_TOL = 0.50
# ...and how close the neighbours must sit for it to count as a revert rather than a move.
NEIGHBOUR_TOL = 0.20
# A wick is judged against the same median, wider, because a real wick is a real trade.
WICK_LO, WICK_HI = 0.50, 2.00
# How close the DAILY bar must come to an intraday extreme for it to count as corroborated.
DAILY_TOL = 0.01


def find_faults(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """`(drop_bar, clamp_wick)` masks over `df`'s index."""
    c = df["Close"].astype("float64")
    med = c.rolling(WINDOW, center=True, min_periods=WINDOW // 2).median()
    ok = med > 0
    dev = (c - med).abs() / med.where(ok)
    prev_dev = (c.shift(1) - med).abs() / med.where(ok)
    next_dev = (c.shift(-1) - med).abs() / med.where(ok)
    o_dev = (df["Open"].astype("float64") - med).abs() / med.where(ok)

    # Spike AND revert: the bar is far out and both neighbours are not. A sustained move
    # fails this on the second condition, which is the whole point.
    reverts = (prev_dev < NEIGHBOUR_TOL) & (next_dev < NEIGHBOUR_TOL)
    drop = (((dev > BODY_TOL) | (o_dev > BODY_TOL)) & reverts).fillna(False)

    # **Per FIELD, not per bar.** A single mask over the row, clamping both bounds, throws
    # away the good half: AAVE 2023-12-13 has a Low of 0.000001 (bogus) and a High of
    # 95.27 (real, 0.5% above the body), and a row-level rule trimmed the High too. It
    # also ate the genuine 2025-10-10 flash-crash wick, 280 -> 258. Only the bound that is
    # impossible may be touched.
    # A bound must be absurd against the local median AND against its OWN body. The median
    # test alone condemns the wick of a genuine crash: a bar that really fell 60% has a
    # body at 0.4x the median and a Low just under it, which trips `lo < med/2` while
    # being an entirely real print. Requiring the wick to sit a further factor away from
    # the body means only a bound that doubles the bar's own range can be touched -- a
    # 0.000001 Low under a 94.44 body is 94-million-fold, and nothing real looks like that.
    lo = df["Low"].astype("float64")
    hi = df["High"].astype("float64")
    body_lo = df[["Open", "Close"]].min(axis=1).astype("float64")
    body_hi = df[["Open", "Close"]].max(axis=1).astype("float64")
    low_bad = ((lo < med * WICK_LO) & (lo < body_lo * WICK_LO) & ~drop).fillna(False)
    high_bad = ((hi > med * WICK_HI) & (hi > body_hi * WICK_HI) & ~drop).fillna(False)
    return drop, (low_bad, high_bad)


def daily_veto(df: pd.DataFrame, daily: pd.DataFrame | None, drop: pd.Series,
               low_bad: pd.Series, high_bad: pd.Series
               ) -> tuple[pd.Series, pd.Series, pd.Series, int]:
    """Drop any repair the DAILY bar for that date corroborates.

    **This is the check that was missing, and its absence cost real data.** Every test in
    `find_faults` is local: it asks whether a bar disagrees with its intraday neighbours.
    That cannot separate "the vendor printed a number nobody traded at" from "the whole
    market gapped down for ninety seconds", because both look identical inside a 21-bar
    window. On 2025-10-10 an earlier version of this tool decided a -78% wick was
    impossible and clamped it out of crypto 1h, 15m and 5m -- and it was the real
    liquidation cascade, still sitting in the daily bars, where DOT's low is 0.633 against
    an intraday low of 2.452 afterwards. The finer the bars, the more of the crash was
    erased, so the timeframes disagreed with each other about the largest move in the
    sample, in the direction that flatters every dip-buying rule in the catalogue.

    The daily bar is the right adjudicator because it comes from the same vendor and the
    same instrument but is aggregated independently, so a bad tick has to appear in BOTH
    to survive -- and the December 2023 ticks this tool exists for do not: BCH prints an
    intraday 50.9 while its daily low that session is 227.6.

    Corroboration is one-sided by construction. A Low may only be clamped if the daily bar
    never went that low; a High only if the daily never went that high; a bar may only be
    dropped if its Close sits outside the daily range entirely. Absent daily data vetoes
    nothing -- this narrows what may be repaired and must never widen it.
    """
    if daily is None or daily.empty:
        return drop, low_bad, high_bad, 0
    d = daily.copy()
    d.index = pd.to_datetime(d.index).normalize()
    d = d[~d.index.duplicated(keep="first")]
    day = pd.to_datetime(df.index).normalize()
    dlo = pd.Series(d["Low"].reindex(day).to_numpy(), index=df.index, dtype="float64")
    dhi = pd.Series(d["High"].reindex(day).to_numpy(), index=df.index, dtype="float64")
    lo = df["Low"].astype("float64")
    hi = df["High"].astype("float64")
    c = df["Close"].astype("float64")

    low_ok = (dlo <= lo * (1.0 + DAILY_TOL)).fillna(False)
    high_ok = (dhi >= hi * (1.0 - DAILY_TOL)).fillna(False)
    close_ok = ((c >= dlo * (1.0 - DAILY_TOL))
                & (c <= dhi * (1.0 + DAILY_TOL))).fillna(False)

    vetoed = int((low_bad & low_ok).sum() + (high_bad & high_ok).sum()
                 + (drop & close_ok).sum())
    return drop & ~close_ok, low_bad & ~low_ok, high_bad & ~high_ok, vetoed


def repair_frame(df: pd.DataFrame,
                 daily: pd.DataFrame | None = None) -> tuple[pd.DataFrame, list[dict], int]:
    drop, (low_bad, high_bad) = find_faults(df)
    drop, low_bad, high_bad, vetoed = daily_veto(df, daily, drop, low_bad, high_bad)
    notes: list[dict] = []
    out = df.copy()
    for ts in df.index[drop]:
        notes.append({"ts": ts, "action": "drop_bar", "field": "",
                      "was": float(df.loc[ts, "Close"]), "now": np.nan})
    body_lo = out[["Open", "Close"]].min(axis=1)
    body_hi = out[["Open", "Close"]].max(axis=1)
    for ts in df.index[low_bad]:
        notes.append({"ts": ts, "action": "clamp_wick", "field": "Low",
                      "was": float(df.loc[ts, "Low"]), "now": float(body_lo.loc[ts])})
    for ts in df.index[high_bad]:
        notes.append({"ts": ts, "action": "clamp_wick", "field": "High",
                      "was": float(df.loc[ts, "High"]), "now": float(body_hi.loc[ts])})
    if low_bad.any():
        out.loc[low_bad, "Low"] = body_lo[low_bad]
    if high_bad.any():
        out.loc[high_bad, "High"] = body_hi[high_bad]
    out = out[~drop]
    return out, notes, vetoed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--class", dest="asset_class", required=True, choices=list(CLASSES))
    ap.add_argument("--tf", nargs="+", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="report and write the ledger; leave the parquet alone")
    a = ap.parse_args()

    rows = []
    for tf in a.tf:
        d = cache_dir(a.asset_class, tf)
        files = sorted(d.glob("*.parquet"))
        if not files:
            print(f"{a.asset_class}/{tf}: no cache")
            continue
        touched = dropped = clamped = vetoed = 0
        daily_dir = cache_dir(a.asset_class, "1d")
        for path in files:
            df = pd.read_parquet(path)
            # The same symbol's DAILY bars, read straight off the cache rather than through
            # `td_loader.load`, which applies BACKTEST_START and the quarantine — neither
            # belongs in a decision about whether a printed price was real.
            dpath = daily_dir / path.name
            daily = pd.read_parquet(dpath) if dpath.exists() else None
            fixed, notes, n_vetoed = repair_frame(df, daily)
            vetoed += n_vetoed
            if not notes:
                continue
            touched += 1
            dropped += sum(1 for n in notes if n["action"] == "drop_bar")
            clamped += sum(1 for n in notes if n["action"] == "clamp_wick")
            for n in notes:
                rows.append({"class": a.asset_class, "tf": tf,
                             "symbol": safe_symbol(path.stem), **n})
            if not a.dry_run:
                fixed.to_parquet(path)
        print(f"{a.asset_class}/{tf}: {touched} of {len(files)} symbols, "
              f"{dropped} bars dropped, {clamped} wicks clamped, "
              f"{vetoed} spared by the daily bar"
              f"{' (dry run)' if a.dry_run else ''}")

    if rows:
        led = pd.DataFrame(rows)
        if LEDGER.exists():
            led = pd.concat([pd.read_csv(LEDGER), led], ignore_index=True)
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        led.to_csv(LEDGER, index=False)
        print(f"ledger -> {LEDGER} ({len(rows)} new entries)")
    else:
        print("nothing to repair")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
