"""What do the four gates say about buy-and-hold itself?

Strictly: nothing. IR is the Sharpe of (strategy - benchmark), and when the strategy IS
the benchmark that difference series is identically zero — mean 0, std 0, so IR is 0/0,
undefined rather than zero. Hit rate, t and breakeven inherit the same problem. The gates
measure edge RELATIVE to a reference, and the reference has no edge over itself by
construction.

The question only becomes answerable by naming a different benchmark, and the choice
changes the answer completely:

  vs CASH   the difference series is just the return series, so IR collapses to the
            plain Sharpe ratio. This is what "Sharpe" always was — an IR against cash.
  vs SPY    the genuinely interesting one: is holding these 20 names better than holding
            the index? Answerable, because SPY is cached alongside them.

The SPY comparison carries a bias that has to be stated with the number, not after it:
the 20 names were chosen for being the largest companies in 2026. Asking whether they
beat SPY since 2015 is asking whether the winners won. The result is guaranteed positive
and is NOT evidence that a top-20 tilt is a strategy. It is reported here to size the
bias, which is a real and useful thing to know.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from common import STOCKHUNT, TRADING_DAYS, UNIVERSE, sharpe, summarise

# This project's own Twelve Data 1d cache — it is the source the report is built from,
# and the only one of the two that carries SPY (the sibling cache_td has the 501 names
# but no index proxy).
CACHE_1D = STOCKHUNT / "top 20 stocks" / "data" / "cache_1d"


def load_daily(tickers):
    out = {}
    for t in tickers:
        p = CACHE_1D / f"{t}.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            df.index = pd.DatetimeIndex(df.index).tz_localize(None)
            out[t] = df.sort_index()
    return out


def ir(strat: pd.Series, bench: pd.Series) -> float:
    d = (strat - bench).dropna()
    return float(d.mean() / d.std() * math.sqrt(TRADING_DAYS)) if len(d) > 2 and d.std() > 0 else np.nan


def main() -> None:
    data = load_daily(UNIVERSE + ["SPY"])
    close = pd.DataFrame({t: d["Close"] for t, d in data.items()})
    rets = close.pct_change().dropna(how="all")
    spy = rets["SPY"]
    names = [t for t in UNIVERSE if t in rets.columns]
    ew = rets[names].mean(axis=1)
    yrs = len(ew) / TRADING_DAYS

    print(f"{close.index[0].date()} -> {close.index[-1].date()}  ({yrs:.1f} years, {len(names)} names)\n")

    s_ew, s_spy = summarise(ew), summarise(spy)
    print(f"{'':22} {'Sharpe':>7} {'CAGR':>8} {'maxDD':>8} {'Calmar':>7}")
    for lbl, s in (("top-20 equal weight", s_ew), ("SPY", s_spy)):
        print(f"{lbl:22} {s['sharpe']:>7.3f} {s['cagr']:>8.2%} {s['max_dd']:>8.1%} "
              f"{s['cagr'] / abs(s['max_dd']):>7.2f}")

    print("\n--- the four gates, applied to buy-and-hold under three benchmarks ---")
    print(f"{'benchmark':>22} {'IR':>9} {'t':>8} {'hit rate':>10} {'BE bps':>10}")
    print(f"{'itself':>22} {'undefined':>9} {'undef':>8} {'undefined':>10} {'undef':>10}"
          "   <- 0/0: the difference series is identically zero")

    ir_cash = sharpe(ew)
    print(f"{'cash (0%)':>22} {ir_cash:>+9.3f} {ir_cash * math.sqrt(yrs):>+8.2f} "
          f"{(ew > 0).mean():>10.0%} {'infinite':>10}   <- IR vs cash IS the Sharpe ratio")

    ir_spy = ir(ew, spy)
    per_name = {t: ir(rets[t], spy) for t in names}
    hit = float(np.mean([v > 0 for v in per_name.values()]))
    print(f"{'SPY':>22} {ir_spy:>+9.3f} {ir_spy * math.sqrt(yrs):>+8.2f} "
          f"{hit:>10.0%} {'infinite':>10}   <- survivorship-inflated, see below")

    print("\nper-name IR vs SPY (the hit-rate detail):")
    srt = sorted(per_name.items(), key=lambda kv: kv[1], reverse=True)
    for i in range(0, len(srt), 5):
        print("   " + "  ".join(f"{t:>5} {v:>+5.2f}" for t, v in srt[i:i + 5]))

    # Why buy-and-hold trivially passes the cost gate.
    turn_bh = 1.0 / yrs
    print(f"\ncost gate: buy-and-hold turns over {turn_bh:.3f} units/yr (one entry, ever).")
    for bps in (5.0, 20.0, 100.0):
        print(f"   at {bps:>5.0f}bps its lifetime cost is {turn_bh * yrs * bps / 1e4:.4%} of capital "
              f"= {turn_bh * bps / 1e4:.5%}/yr")
    print("   -> breakeven cost is effectively unbounded. Nothing can out-wait it.")


if __name__ == "__main__":
    main()
