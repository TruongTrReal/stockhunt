"""How much does the price source itself move the backtest result?

`validate_data.py` answers "do the rules take different positions" (barely).
This answers the question that actually decides whether a source swap is safe:
having taken almost the same positions, do you end up with a different P&L?

Both are needed. A 0.05% signal difference sounds negligible until you notice
that dividend-adjustment methodology shifts the whole price *level* of a
high-yield name over eleven years, which moves returns without moving a single
position.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import HERE

KEY = ["convention", "ticker", "rule"]


def main() -> None:
    td = pd.read_csv(HERE / "results" / "twelvedata" / "reference.csv")
    yf = pd.read_csv(HERE / "results" / "yfinance" / "reference.csv")

    merged = td.merge(yf, on=KEY, suffixes=("_td", "_yf"))
    merged["ret_diff_pp"] = 100.0 * (merged["total_return_td"] - merged["total_return_yf"])
    merged["rel_equity_diff"] = np.abs(
        merged["final_equity_td"] / merged["final_equity_yf"] - 1.0)
    merged["trade_diff"] = merged["n_trades_td"] - merged["n_trades_yf"]

    print("=" * 78)
    print("SAME RULES, SAME ENGINE, DIFFERENT PRICE SOURCE")
    print("=" * 78)
    print(f"  runs compared            : {len(merged)}")
    print(f"  median |equity diff|     : {merged['rel_equity_diff'].median():.3e}")
    print(f"  90th pct |equity diff|   : {merged['rel_equity_diff'].quantile(0.9):.3e}")
    print(f"  max |equity diff|        : {merged['rel_equity_diff'].max():.3e}")
    print(f"  runs with trade-count gap: "
          f"{int((merged['trade_diff'] != 0).sum())} of {len(merged)}")
    print(f"  total-return spread      : "
          f"{merged['ret_diff_pp'].min():+.1f} pp to {merged['ret_diff_pp'].max():+.1f} pp")

    print("\n  largest divergences:")
    worst = merged.reindex(
        merged["rel_equity_diff"].sort_values(ascending=False).index).head(8)
    print(worst[["convention", "ticker", "rule", "final_equity_td",
                 "final_equity_yf", "rel_equity_diff", "trade_diff"]].to_string(
        index=False, float_format=lambda v: f"{v:,.4f}"))

    print("\n  by rule (median relative equity difference):")
    by_rule = merged.groupby("rule")["rel_equity_diff"].agg(["median", "max"])
    print(by_rule.to_string(float_format=lambda v: f"{v:.3e}"))

    print("\n  by ticker, worst 6:")
    by_ticker = (merged.groupby("ticker")["rel_equity_diff"]
                 .median().sort_values(ascending=False).head(6))
    print(by_ticker.to_string(float_format=lambda v: f"{v:.3e}"))

    # Would either source change which rule you would have picked?
    print()
    print("=" * 78)
    print("DOES THE SOURCE CHANGE THE RANKING?")
    print("=" * 78)
    for convention in sorted(merged["convention"].unique()):
        sub = merged[merged["convention"] == convention]
        rank_td = (sub.groupby("rule")["total_return_td"].mean()
                   .sort_values(ascending=False))
        rank_yf = (sub.groupby("rule")["total_return_yf"].mean()
                   .sort_values(ascending=False))
        same = list(rank_td.index) == list(rank_yf.index)
        print(f"\n  [{convention}] rule ranking by mean total return — "
              f"{'IDENTICAL' if same else 'DIFFERENT'}")
        print(f"    twelvedata: {' > '.join(rank_td.index)}")
        print(f"    yfinance  : {' > '.join(rank_yf.index)}")


if __name__ == "__main__":
    main()
