"""Scan the cache for OHLC bars that cannot be real, and other integrity faults.

Found because NautilusTrader refused to accept some Twelve Data 1-hour equity bars
("high was < close", "low was > open"). The vectorised sweep reads Close only, so it
would have consumed those bars in silence forever — which is precisely the class of
problem a second engine exists to catch.

An inconsistent bar is not merely cosmetic. Any rule reading High or Low — the
mean-reversion bands, ATR-scaled rules, the whole candlestick family, BETA/CORREL — is
computing on a bar that never existed.

Run::

    python check_data.py                # scan everything cached
    python check_data.py --fix          # additionally write repaired parquet
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from config import CLASSES, RESULTS_DIR, TIMEFRAMES, cache_dir, safe_symbol
import td_loader


# A bar whose close sits this far from the local median is a vendor misprint, not a
# move. Observed cases are decimal-point errors of 1000-10000x (BTC 28,100 -> 2.812 ->
# 28,118 in consecutive minutes), so the threshold is set far outside anything a real
# crypto minute does.
SPIKE_LO, SPIKE_HI = 0.2, 5.0
SPIKE_WINDOW = 5


def spike_mask(close: np.ndarray) -> np.ndarray:
    """Bars whose close is a wild outlier against a centred rolling median.

    Single-bar price spikes are invisible to the OHLC consistency checks — each bad bar
    is internally consistent (its own high/low bracket its own close). They only show up
    bar-to-bar, and they are devastating in combination with the -0.999 return floor:
    the crash leg gets clipped, the recovery leg does not, so each spike pair multiplies
    equity by ~10x. 125 of them on BTC 1-minute turned buy-and-hold into 1e125.
    """
    s = pd.Series(close)
    med = s.rolling(SPIKE_WINDOW, center=True, min_periods=2).median().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = np.where(med > 0, close / med, 1.0)
    return (ratio < SPIKE_LO) | (ratio > SPIKE_HI)


def faults(df: pd.DataFrame) -> dict[str, np.ndarray]:
    o, h, l, c = (df[k].to_numpy(dtype="float64") for k in ("Open", "High", "Low", "Close"))
    return {
        "high_lt_low": h < l,
        "high_lt_open": h < o,
        "high_lt_close": h < c,
        "low_gt_open": l > o,
        "low_gt_close": l > c,
        "nonpositive_close": ~(c > 0),
        "nonfinite": ~(np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c)),
        "price_spike": spike_mask(c),
    }


def repair(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop spike bars, then widen High/Low to contain Open and Close.

    Two different faults, repaired differently because they mean different things:

    * **Price spikes are removed, not adjusted.** A bar printing 1/10,000 of the true
      price is not a mispriced extreme, it is a bar that did not happen; there is no
      honest value to substitute, so the row is dropped and the series closes over the
      gap. Interpolating would invent a trade.
    * **High/Low are widened** to contain Open and Close. Those two are prices actually
      transacted at, so a High below either is a fault in the reported *extreme*. Widening
      to the minimum consistent range can only shrink an ATR or a band, never invent a
      move.
    """
    out = df.copy()
    spikes = spike_mask(out["Close"].to_numpy(dtype="float64"))
    n_spikes = int(spikes.sum())
    if n_spikes:
        out = out.loc[~spikes]

    o, c = out["Open"].to_numpy(), out["Close"].to_numpy()
    hi = np.maximum(out["High"].to_numpy(), np.maximum(o, c))
    lo = np.minimum(out["Low"].to_numpy(), np.minimum(o, c))
    n_hl = int((hi != out["High"].to_numpy()).sum() + (lo != out["Low"].to_numpy()).sum())
    out["High"], out["Low"] = hi, lo
    return out, n_spikes + n_hl


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fix", action="store_true")
    # Scoping exists because a full scan reads ~60M bars, most of them crypto 1-minute,
    # to re-check a cache that was already repaired. After fetching one new sheet the
    # only thing worth scanning is that sheet.
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+", choices=list(TIMEFRAMES),
                    default=list(TIMEFRAMES))
    args = ap.parse_args()
    full_scan = (len(args.classes) == len(CLASSES)
                 and len(args.timeframes) == len(TIMEFRAMES))

    rows = []
    for asset_class in args.classes:
        for timeframe in args.timeframes:
            data = td_loader.load(asset_class, timeframe)
            for symbol, df in data.items():
                f = faults(df)
                total = int(sum(int(v.sum()) for v in f.values()))
                if total:
                    rows.append({"class": asset_class, "timeframe": timeframe,
                                 "symbol": symbol, "n_bars": len(df),
                                 **{k: int(v.sum()) for k, v in f.items()},
                                 "total_faults": total,
                                 "pct": 100.0 * total / len(df)})
                if args.fix and total:
                    fixed, n = repair(df)
                    fixed.to_parquet(cache_dir(asset_class, timeframe)
                                     / f"{safe_symbol(symbol)}.parquet")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if not rows:
        print("no OHLC integrity faults found")
        # Only a full scan may declare the whole cache clean. A scoped run that truncated
        # this file would erase the record of faults in the sheets it never looked at.
        if full_scan:
            (RESULTS_DIR / "data_faults.csv").write_text("", encoding="utf-8")
        return

    df = pd.DataFrame(rows).sort_values("total_faults", ascending=False)
    df.to_csv(RESULTS_DIR / "data_faults.csv", index=False)

    by_tf = df.groupby(["class", "timeframe"]).agg(
        symbols_affected=("symbol", "size"),
        bars=("n_bars", "sum"),
        faults=("total_faults", "sum")).reset_index()
    by_tf["pct_of_bars"] = 100.0 * by_tf["faults"] / by_tf["bars"]
    print("=== OHLC integrity faults ===")
    print(by_tf.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print(f"\nworst symbols:\n{df.head(8).to_string(index=False)}")
    if args.fix:
        print("\nrepaired in place (High/Low widened to contain Open/Close)")
    else:
        print("\nrun with --fix to widen High/Low to contain Open and Close")


if __name__ == "__main__":
    main()
