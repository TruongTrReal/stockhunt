"""Plan 9 - re-test the survivors on 20 years of data the search has never seen.

Everything in P0-P8 was developed on 2015-2022 and checked, once, against 2023-2026.
The 1995-2014 window is untouched by any of it: no signal was chosen there, no parameter
fitted there, no leaderboard sorted there. That makes it a genuine out-of-sample test,
and a long one - 20 years against the 3.6 the original holdout offered, spanning the
dot-com bust and the GFC, which neither earlier window contains.

It also fixes what P4 identified as the binding constraint. Significance is
t = IR * sqrt(years), so the 8-year sample capped every t at IR * 2.83. The full
1995-2026 span takes that to ~5.6, nearly doubling the power of the same edge.

Judged against the criteria agreed for this project:
    IR >= 0.5 | breadth >= 70% | breakeven cost 3-5x real | t = IR*sqrt(years) >= 2

Parameters are FROZEN at what P7 used. Nothing is refitted here — a hypothesis that
needs new parameters to survive a new sample has already failed.

Scope limit, restated because it is easy to forget: the universe is today's top 20, so
ABSOLUTE returns from this cache are meaningless (see fetch_long_history.py). Only the
difference series against buy-and-hold on the same names is interpretable, which is
exactly what IR measures.
"""

from __future__ import annotations

import glob
import math
import os

import numpy as np
import pandas as pd

from common import TRADING_DAYS, block_bootstrap_se, log_trial, sharpe, summarise

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "cache_1d_long")
TARGET_VOL, LOOKBACKS, CAPS = 0.15, [20, 60], [1.0, 2.0]
COSTS = [0.0, 1.0, 5.0, 10.0, 20.0, 50.0]
SAMPLES = {
    "1995-2014 (NEW, unsearched)": ("1995-01-01", "2014-12-31"),
    "2015-2022 (original train)":  ("2015-01-01", "2022-12-31"),
    "2023-2026 (original holdout)": ("2023-01-01", "2026-12-31"),
    "1995-2026 (full)":            ("1995-01-01", "2026-12-31"),
}


def load():
    close, vol = {}, {}
    for p in sorted(glob.glob(os.path.join(CACHE, "*.parquet"))):
        t = os.path.basename(p)[:-8]
        df = pd.read_parquet(p)
        df.index = pd.DatetimeIndex(df.index).tz_localize(None)
        close[t], vol[t] = df["Close"], df["Volume"]
    return pd.DataFrame(close).sort_index(), pd.DataFrame(vol).sort_index()


def ir_of(strat: pd.Series, bench: pd.Series) -> float:
    """Sharpe of the difference series — the metric this project ranks on."""
    d = (strat - bench).dropna()
    return float(d.mean() / d.std() * math.sqrt(TRADING_DAYS)) if len(d) > 2 and d.std() > 0 else np.nan


def breakeven_bps(gross: pd.Series, turn: pd.Series, bench: pd.Series) -> float:
    """Cost level at which the IR crosses zero. Linear in bps, so two points locate it."""
    a = ir_of(gross, bench)
    b = ir_of(gross - turn * 1e-4, bench)       # 1bps
    if not np.isfinite(a) or not np.isfinite(b) or a <= 0:
        return 0.0
    slope = a - b
    return float(a / slope) if slope > 1e-12 else float("inf")


def vol_target(base: pd.Series, lookback: int, cap: float):
    rv = base.rolling(lookback).std() * math.sqrt(TRADING_DAYS)
    exp = (TARGET_VOL / rv).clip(upper=cap).shift(1).fillna(0.0)
    return exp * base, exp.diff().abs().fillna(exp.abs()), exp


def year_hit_rate(strat: pd.Series, bench: pd.Series) -> float:
    """Share of calendar years the overlay beat the book. Breadth for a portfolio
    overlay, where 'fraction of names positive' does not apply."""
    d = (strat - bench).dropna()
    if d.empty:
        return np.nan
    by = d.groupby(d.index.year).sum()
    return float((by > 0).mean())


def main() -> None:
    close, _ = load()
    close = close.drop(columns=[c for c in ("SPY",) if c in close.columns])
    rets_all = close.pct_change()
    print(f"loaded {close.shape[1]} names, {close.index[0].date()} -> {close.index[-1].date()}\n")

    rows = []
    for label, (a, b) in SAMPLES.items():
        r = rets_all.loc[a:b].dropna(how="all")
        if r.empty:
            continue
        # Equal weight across whatever exists on each date - names join as they list.
        base = r.mean(axis=1, skipna=True).fillna(0.0)
        yrs = len(base) / TRADING_DAYS
        se = block_bootstrap_se(base, sharpe, block=21, n_boot=2000)
        s = summarise(base)
        n_live = r.notna().sum(axis=1)
        print(f"=== {label} ===")
        print(f"  {len(base):,} days = {yrs:.1f}y | names {int(n_live.min())}-{int(n_live.max())} "
              f"| book Sharpe {s['sharpe']:.3f} maxDD {s['max_dd']:.1%} | SE(SR) {se:.3f} "
              f"| sqrt(years) {math.sqrt(yrs):.2f}")
        print(f"  {'overlay':>16} {'IR':>7} {'t':>6} {'yrHit':>6} {'BE bps':>8} "
              f"{'CAGR@5':>8} {'maxDD@5':>8} {'verdict':>10}")
        for lb in LOOKBACKS:
            for cap in CAPS:
                gross, turn, exp = vol_target(base, lb, cap)
                ir = ir_of(gross - turn * 5e-4, base)
                be = breakeven_bps(gross, turn, base)
                t = ir * math.sqrt(yrs)
                hit = year_hit_rate(gross - turn * 5e-4, base)
                st = summarise((gross - turn * 5e-4).clip(lower=-0.999))
                gates = (ir >= 0.5) + (hit >= 0.70) + (be >= 15.0) + (t >= 2.0)
                verdict = {4: "PASS", 3: "3/4", 2: "2/4", 1: "1/4", 0: "fail"}[gates]
                rows.append({"sample": label, "lookback": lb, "cap": cap, "ir": ir, "t": t,
                             "year_hit": hit, "breakeven_bps": be, "cagr_5": st["cagr"],
                             "dd_5": st["max_dd"], "gates": gates, "years": yrs})
                print(f"  {f'volt {lb}d x{cap:g}':>16} {ir:>+7.3f} {t:>+6.2f} {hit:>6.0%} "
                      f"{be:>8.1f} {st['cagr']:>8.2%} {st['max_dd']:>8.1%} {verdict:>10}")
        print()

    r = pd.DataFrame(rows)
    r.to_csv("edge/p9_long_history.csv", index=False)

    print("=" * 78)
    print("Does the sign hold on the 20 years the search never saw?")
    new = r[r["sample"].str.startswith("1995-2014")].set_index(["lookback", "cap"])
    old = r[r["sample"].str.startswith("2015-2022")].set_index(["lookback", "cap"])
    for k in new.index:
        n, o = new.loc[k], old.loc[k]
        agree = "holds" if np.sign(n["ir"]) == np.sign(o["ir"]) and n["ir"] > 0 else \
                "SIGN FLIPS" if np.sign(n["ir"]) != np.sign(o["ir"]) else "both negative"
        print(f"  volt {k[0]}d x{k[1]:g}: train IR {o['ir']:+.3f} -> unsearched IR {n['ir']:+.3f}   {agree}")

    best = r.loc[r["gates"].idxmax()]
    log_trial("P9", "frozen vol-target on 1995-2014, a 20y unsearched sample", "NEW-OOS",
              f"best {best['gates']}/4 gates ({best['sample']}, {best['lookback']}d x{best['cap']})",
              {k: float(best[k]) for k in ("ir", "t", "year_hit", "breakeven_bps", "years")},
              "PASS" if best["gates"] == 4 else f"{int(best['gates'])}/4 gates")


if __name__ == "__main__":
    main()
