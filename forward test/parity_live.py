"""How much history does a live strategy need to reproduce the backtest exactly?

The backtest hands every indicator the *entire* price series. A live strategy cannot: it
keeps a rolling buffer of the last N bars and recomputes on each close. For a windowed
indicator (SMA_200, Donchian) those two agree as soon as N exceeds the lookback. For a
**recursive** one they never agree exactly — an EMA at bar *t* depends on every bar before
it, and Wilder-smoothed rules (RSI, ADX, ATR, ADXR) are worse, because TA-Lib seeds them
with a simple average of the first `timeperiod` bars and then decays that seed forever.

So N is a *correctness* parameter, not a performance knob. Pick it too small and the live
system trades a different signal from the one that was tested, silently, and the forward
test measures nothing. `fwd_config.DEFAULT_WINDOW_BARS` is a default, not a claim; this
module measures what each rule actually needs.

Method: for each rule and each candidate window N, recompute the position at a sample of
recent bars using only the trailing N bars, and compare with the full-series position at
the same bar. Report the smallest N that reproduces it on **every** sampled bar — an exact
match, because a position is a discrete 1/0/-1 and "nearly the same signal" means "a
different trade".

Run::

    python parity_live.py                       # SOXL/TQQQ 1d, the default rule set
    python parity_live.py --tf 4h --rules RSI ADX SMA_200
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

import fwd_config                                # noqa: F401  (wires sys.path)
from talib_signals import generate_position

# Windows to try, smallest first. 4000 is "effectively the whole daily series" for the
# ETFs (~4100 bars) and is the honest answer for a rule that never converges.
WINDOWS = [120, 250, 500, 750, 1000, 1500, 2000, 3000, 4000]

# A deliberately mixed set: windowed rules that should converge immediately, recursive
# rules that should not, and the ETF sheet's actual leaders.
DEFAULT_RULES = [
    "SMA_200", "MA_200", "MIDPRICE_200",          # pure windowed
    "EMA_200", "DEMA_200", "TEMA_200",            # recursive
    "RSI", "ADX", "ADXR", "ATR", "NATR",          # Wilder-smoothed
    "MACD", "HT_TRENDMODE", "MAXINDEX", "MININDEX",
]

N_EVAL_BARS = 200        # how many recent bars to check parity on


def full_series_positions(rule: str, df: pd.DataFrame) -> np.ndarray | None:
    try:
        raw = generate_position(rule, df)
    except Exception:
        return None
    pos = np.nan_to_num(np.asarray(raw, dtype="float64"), nan=0.0,
                        posinf=0.0, neginf=0.0)
    return pos if pos.size == len(df) else None


def rolling_position_at(rule: str, df: pd.DataFrame, end: int, window: int) -> float | None:
    """Position at bar `end` computed from only the trailing `window` bars."""
    lo = max(0, end - window + 1)
    sub = df.iloc[lo:end + 1]
    try:
        raw = generate_position(rule, sub)
    except Exception:
        return None
    arr = np.nan_to_num(np.asarray(raw, dtype="float64"), nan=0.0,
                        posinf=0.0, neginf=0.0)
    return float(arr[-1]) if arr.size == len(sub) else None


def measure(rule: str, df: pd.DataFrame) -> dict:
    truth = full_series_positions(rule, df)
    if truth is None:
        return {"rule": rule, "min_window": None, "note": "rule failed to build"}

    ends = np.linspace(len(df) - N_EVAL_BARS, len(df) - 1, N_EVAL_BARS).astype(int)
    ends = np.unique(ends[ends > 0])

    row = {"rule": rule}
    min_window = None
    for w in WINDOWS:
        mismatches = 0
        for e in ends:
            got = rolling_position_at(rule, df, int(e), w)
            if got is None or got != truth[e]:
                mismatches += 1
        row[f"w{w}"] = len(ends) - mismatches
        if mismatches == 0 and min_window is None:
            min_window = w
    row["n_eval"] = len(ends)
    row["min_window"] = min_window
    row["converges"] = min_window is not None
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--symbols", nargs="+", default=["SOXL", "TQQQ"])
    ap.add_argument("--rules", nargs="+", default=DEFAULT_RULES)
    args = ap.parse_args()

    # The ETF cache lives with the study that fetched it.
    import sys
    from pathlib import Path
    top20 = Path(fwd_config.REPO) / "top 20 stocks"
    if str(top20) not in sys.path:
        sys.path.insert(0, str(top20))
    cache = top20 / "data" / f"cache_{args.tf}"

    frames = {}
    for sym in args.symbols:
        p = cache / f"{sym}.parquet"
        if p.exists():
            frames[sym] = pd.read_parquet(p)
    if not frames:
        print(f"no cached {args.tf} data under {cache}")
        return

    all_rows = []
    for sym, df in frames.items():
        print(f"\n=== {sym} {args.tf} — {len(df)} bars, "
              f"{df.index[0].date()} -> {df.index[-1].date()} ===")
        rows = [measure(r, df) for r in args.rules]
        for r in rows:
            r["symbol"] = sym
        table = pd.DataFrame(rows)
        cols = ["rule", "min_window", "converges"] + [f"w{w}" for w in WINDOWS]
        cols = [c for c in cols if c in table]
        print(table[cols].to_string(index=False))
        all_rows += rows

    out = pd.DataFrame(all_rows)
    dest = fwd_config.RESULTS_DIR / f"parity_live_{args.tf}.csv"
    out.to_csv(dest, index=False)

    bad = out[~out["converges"].fillna(False)]
    worst = out["min_window"].dropna()
    print(f"\nwrote {dest}")
    if len(worst):
        print(f"  safe window for the rules that converge: "
              f"{int(worst.max())} bars (max over rules and symbols)")
    if not bad.empty:
        names = sorted(set(bad["rule"]))
        print(f"  DID NOT converge within {WINDOWS[-1]} bars: {names}")
        print("  -> these cannot be reproduced live from a rolling buffer. Either seed "
              "the strategy with the full cached history at startup, or do not deploy "
              "them.")


if __name__ == "__main__":
    main()
