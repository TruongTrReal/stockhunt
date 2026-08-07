"""Diagnose WHERE the validated combos lose money, before trying to fix it.

The combos beat buy-and-hold on Sharpe and cut drawdown, but they still fall in
market selloffs. Adding another indicator layer blindly is how the earlier
searches produced degenerate winners; this establishes the actual failure mode
first so any added layer has something specific to attack.

Questions this answers:
  1. What are the worst drawdown episodes, and how do they line up with the
     market's own? A strategy that simply tracks the market down is a different
     problem from one that bleeds in chop.
  2. What is exposure DOING during those episodes? The volatility leg is
     supposed to step aside when volatility rises - does it, and how late?
  3. Is the loss coming from being long into the fall, or from whipsaw
     (re-entering too early and getting hit again)?
  4. How do trades differ in up vs down markets?

    python combo_diagnostics.py
"""

import numpy as np
import pandas as pd

from beat_buyhold_search import Evaluator, TRADING_DAYS
from beat_buyhold_search2 import Space2
from sharpe_search import SharpeEvaluator, sharpe_of
from signal_tensor import CACHE_PATH, load

COMBO = "~ADOSC + ~CDLCLOSINGMARUBOZU + ~VAR"
TRANSFORM = "defensive"
COST_BPS = 5.0


def drawdown_episodes(eq, dates, top=6, min_depth=0.05):
    """Peak-to-trough episodes, deepest first."""
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    episodes, i, n = [], 0, len(eq)
    while i < n:
        if dd[i] >= -1e-12:
            i += 1
            continue
        start = i
        while i < n and dd[i] < -1e-12:
            i += 1
        seg = dd[start:i]
        j = int(np.argmin(seg))
        episodes.append({"peak": dates[start - 1] if start else dates[0],
                         "trough": dates[start + j], "depth": float(seg[j]),
                         "recovered": dates[i - 1] if i < n else None,
                         "len_days": i - start, "i0": start, "i1": i})
    episodes = [e for e in episodes if e["depth"] <= -min_depth]
    return sorted(episodes, key=lambda e: e["depth"])[:top]


def main():
    z = load(CACHE_PATH)
    ev = Evaluator(z)
    se = SharpeEvaluator(z, ev)
    sp = Space2(z)
    names = list(z["names"])
    dates = pd.DatetimeIndex(z["dates"])
    T, N = z["returns"].shape
    ones = np.ones((T, N), np.int8)

    atoms = tuple(sorted((names.index(t.lstrip("~")), -1 if t.startswith("~") else 1)
                         for t in COMBO.split(" + ")))
    pos = sp.build2(atoms, None, 1, TRANSFORM, "none")

    s, e = 0, T
    m = se.valid[s:e]
    n_tick = np.maximum(m.sum(axis=1), 1)

    def book(p, lev=1.0, costs=True):
        held = se._held(p, s, e)
        pnl = lev * ((held * se.R[s:e]) * m).sum(axis=1) / n_tick
        if not costs:
            return pnl
        first = np.zeros((1, N), p.dtype)
        tr = lev * (np.abs(np.diff(np.vstack([first, held]), axis=0)) * m).sum(axis=1) / n_tick
        return pnl - tr * COST_BPS / 1e4

    strat = book(pos)
    mkt = book(ones, costs=False)
    expo = (np.abs(se._held(pos, s, e)) * m).sum(axis=1) / n_tick

    eq_s = np.cumprod(1 + strat)
    eq_m = np.cumprod(1 + mkt)
    d = dates[s:e]

    print(f"Combo: {COMBO} [{TRANSFORM}]   full period {d[0]:%Y-%m-%d} to {d[-1]:%Y-%m-%d}")
    print(f"  strategy  CAGR {eq_s[-1]**(TRADING_DAYS/len(eq_s))-1:>7.2%}  "
          f"vol {strat.std(ddof=1)*np.sqrt(TRADING_DAYS):>6.2%}  "
          f"Sharpe {sharpe_of(strat):.3f}  maxDD {(eq_s/np.maximum.accumulate(eq_s)-1).min():>7.2%}")
    print(f"  market    CAGR {eq_m[-1]**(TRADING_DAYS/len(eq_m))-1:>7.2%}  "
          f"vol {mkt.std(ddof=1)*np.sqrt(TRADING_DAYS):>6.2%}  "
          f"Sharpe {sharpe_of(mkt):.3f}  maxDD {(eq_m/np.maximum.accumulate(eq_m)-1).min():>7.2%}")
    print(f"  average exposure {expo.mean():.3f}\n")

    print("=== 1. Worst drawdowns of the MARKET, and what the combo did ===")
    print(f"{'peak':<12}{'trough':<12}{'mkt dd':>9}{'combo dd':>10}{'capture':>9}"
          f"{'expo@pk':>9}{'expo@tr':>9}{'expo min':>9}")
    for ep in drawdown_episodes(eq_m, d):
        i0, i1 = ep["i0"], ep["i1"]
        j = int(np.argmin(eq_m[i0:i1] / np.maximum.accumulate(eq_m)[i0:i1] - 1))
        seg = slice(i0, i0 + j + 1)
        cdd = float(eq_s[i0 + j] / eq_s[max(i0 - 1, 0)] - 1)
        print(f"{ep['peak']:%Y-%m-%d}  {ep['trough']:%Y-%m-%d}  {ep['depth']:>8.2%}{cdd:>10.2%}"
              f"{cdd/ep['depth']:>9.2f}{expo[max(i0-1,0)]:>9.3f}{expo[i0+j]:>9.3f}"
              f"{expo[seg].min():>9.3f}")

    print("\n=== 2. Does exposure fall when the market falls? (by market-return decile) ===")
    fwd = np.r_[mkt[1:], 0.0]
    q = pd.qcut(pd.Series(mkt), 10, labels=False, duplicates="drop")
    tab = pd.DataFrame({"mkt": mkt, "expo_same_day": expo, "expo_next_day": np.r_[expo[1:], expo[-1]],
                        "q": q}).groupby("q").mean()
    print(tab.to_string(float_format=lambda x: f"{x:.4f}"))

    print("\n=== 3. Where does the strategy's loss come from? ===")
    down = mkt < 0
    big_down = mkt < np.quantile(mkt, 0.05)
    for label, mask in (("all days", np.ones(len(mkt), bool)), ("market down", down),
                        ("worst 5% days", big_down)):
        print(f"  {label:<16} n={mask.sum():>5}  mkt {mkt[mask].mean()*1e4:>8.2f}bp   "
              f"strat {strat[mask].mean()*1e4:>8.2f}bp   expo {expo[mask].mean():.3f}   "
              f"capture {strat[mask].mean()/mkt[mask].mean():>6.2f}")

    print("\n=== 4. Volatility-leg lag: exposure vs realised vol regime ===")
    rv = pd.Series(mkt).rolling(20).std().to_numpy() * np.sqrt(TRADING_DAYS)
    ok = ~np.isnan(rv)
    qv = pd.qcut(pd.Series(rv[ok]), 5, labels=False, duplicates="drop")
    t2 = pd.DataFrame({"realised_vol": rv[ok], "exposure": expo[ok],
                       "strat_bp": strat[ok] * 1e4, "mkt_bp": mkt[ok] * 1e4,
                       "q": qv}).groupby("q").mean()
    print(t2.to_string(float_format=lambda x: f"{x:.4f}"))

    print("\n=== 5. Calendar-year breakdown ===")
    yr = pd.DataFrame({"strat": strat, "mkt": mkt, "expo": expo}, index=d)
    g = yr.groupby(yr.index.year).agg(
        strat=("strat", lambda x: (1 + x).prod() - 1),
        mkt=("mkt", lambda x: (1 + x).prod() - 1),
        expo=("expo", "mean"))
    g["diff"] = g.strat - g.mkt
    print(g.to_string(float_format=lambda x: f"{x:>8.2%}"))


if __name__ == "__main__":
    main()
