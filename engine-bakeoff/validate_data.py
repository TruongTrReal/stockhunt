"""Compare the Twelve Data pull against the project's existing yfinance cache.

Switching price source is only safe if you know what it changes. This checks
three things, in increasing order of how much they matter:

1. **Calendar** — do the two sources agree on which days are trading days?
2. **Prices** — how far apart are OHLC on the days they share?
3. **Signals** — and, the only question that really counts, do the five TA-Lib
   rules actually produce different positions because of it?

A price difference of 1e-4 is irrelevant if no rule ever crosses a threshold
because of it, and very relevant if one does.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import RULES, UNIVERSE, load_bars

FIELDS = ["Open", "High", "Low", "Close"]


def main() -> None:
    cal_rows, px_rows, sig_rows = [], [], []

    for ticker in UNIVERSE:
        td = load_bars(ticker, source="twelvedata")
        yf = load_bars(ticker, source="yfinance")

        shared = td.index.intersection(yf.index)
        cal_rows.append({
            "ticker": ticker,
            "td_bars": len(td),
            "yf_bars": len(yf),
            "shared": len(shared),
            "td_only": len(td.index.difference(yf.index)),
            "yf_only": len(yf.index.difference(td.index)),
        })

        a, b = td.loc[shared], yf.loc[shared]
        row = {"ticker": ticker}
        for field in FIELDS:
            rel = np.abs(a[field].to_numpy() / b[field].to_numpy() - 1.0)
            row[f"{field}_median"] = float(np.median(rel))
            row[f"{field}_max"] = float(np.max(rel))
        px_rows.append(row)

        # Signals are recomputed on each source's own full series, then compared
        # only where the calendars overlap — the honest question is "would my
        # backtest have taken a different position on this day".
        for rule, fn in RULES.items():
            sa = pd.Series(fn(td), index=td.index).reindex(shared)
            sb = pd.Series(fn(yf), index=yf.index).reindex(shared)
            both = sa.notna() & sb.notna()
            differ = int((sa[both] != sb[both]).sum())
            sig_rows.append({
                "ticker": ticker, "rule": rule,
                "compared": int(both.sum()), "differing_bars": differ,
                "pct_differing": 100.0 * differ / max(1, int(both.sum())),
            })

    cal = pd.DataFrame(cal_rows)
    px = pd.DataFrame(px_rows)
    sig = pd.DataFrame(sig_rows)

    print("=" * 72)
    print("CALENDAR")
    print("=" * 72)
    print(f"  bars per ticker : twelvedata {cal['td_bars'].min()}-{cal['td_bars'].max()}, "
          f"yfinance {cal['yf_bars'].min()}-{cal['yf_bars'].max()}")
    print(f"  shared dates    : {cal['shared'].min()}-{cal['shared'].max()}")
    print(f"  twelvedata-only : {cal['td_only'].sum()} bars across all tickers")
    print(f"  yfinance-only   : {cal['yf_only'].sum()} bars across all tickers")

    print()
    print("=" * 72)
    print("PRICE AGREEMENT on shared dates (relative difference)")
    print("=" * 72)
    for field in FIELDS:
        print(f"  {field:6} median {px[f'{field}_median'].median():.3e}   "
              f"worst {px[f'{field}_max'].max():.3e} "
              f"({px.loc[px[f'{field}_max'].idxmax(), 'ticker']})")

    print()
    print("=" * 72)
    print("SIGNAL AGREEMENT — do the rules actually disagree?")
    print("=" * 72)
    by_rule = sig.groupby("rule").agg(
        compared=("compared", "sum"),
        differing=("differing_bars", "sum"),
    ).reset_index()
    by_rule["pct"] = 100.0 * by_rule["differing"] / by_rule["compared"]
    print(by_rule.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print(f"\n  total: {by_rule['differing'].sum()} differing position-days of "
          f"{by_rule['compared'].sum()} "
          f"({100.0 * by_rule['differing'].sum() / by_rule['compared'].sum():.4f}%)")

    worst = sig.sort_values("pct_differing", ascending=False).head(5)
    print("\n  worst (ticker, rule) pairs:")
    print(worst.to_string(index=False, float_format=lambda v: f"{v:.4f}"))


if __name__ == "__main__":
    main()
