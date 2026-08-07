"""Score the validated combos over the FULL 2015-2026 period and merge them
into the existing indicator leaderboard.

The combos were selected on 2015-2021 and validated on 2022-2026. The report
(`report/index.html`) ranks 231 single indicators over the whole span on total
dollar P&L at $10k per ticker. To put the combos on that same board they have to
be scored the same way, so this calls `indicator_backtest._ticker_stats`
directly rather than reimplementing the metrics - identical clipping, identical
Sharpe convention (pandas ddof=1), identical $10k notional.

Positions come from the cached tensor and are mapped back onto each ticker's own
calendar, so a ticker listed partway through the period is treated exactly as
`indicator_backtest.py` treats it.

Caveat that belongs on any reading of the output: the full period INCLUDES the
2015-2021 window these combos were selected on, so their full-period rank is
partly in-sample. The honest out-of-sample number is the 2022-2026 test column,
reported alongside.

    python rank_combos_full_period.py
"""

import numpy as np
import pandas as pd
from tqdm import tqdm

from beat_buyhold_search2 import Space2
from data_loader import load_universe
from indicator_backtest import (
    BASELINE_NAME, CAPITAL_PER_TICKER, MIN_ROWS, MIN_SHARPE_COVERAGE, _ticker_stats,
)
from signal_tensor import CACHE_PATH, load
from sp500_tickers import get_sp500_tickers
from talib_signals import BENCHMARK_TICKER

OUT_CSV = "../data/leaderboard_with_validated_combos.csv"


def main():
    z = load(CACHE_PATH)
    sp = Space2(z)
    names = list(z["names"])
    master = pd.DatetimeIndex(z["dates"])
    tickers = list(z["tickers"])

    data = load_universe(get_sp500_tickers())
    data = {t: df for t, df in data.items() if len(df) >= MIN_ROWS and t != BENCHMARK_TICKER}

    fin = pd.read_csv("../data/sharpe_finalists.csv")
    combos = [(f"{r['combo']} [{r['transform']}]",
               tuple(sorted((names.index(t.lstrip("~")), -1 if t.startswith("~") else 1)
                            for t in r["combo"].split(" + "))),
               r["transform"]) for _, r in fin.iterrows()]

    rows = []
    for label, atoms, transform in tqdm(combos, desc="combos"):
        posmat = sp.build2(atoms, None, 1, transform, "none")
        per_ticker = []
        for i, ticker in enumerate(tickers):
            df = data.get(ticker)
            if df is None:
                continue
            pos = pd.Series(posmat[:, i], index=master, dtype=float).reindex(df.index)
            stats = _ticker_stats(df, pos.fillna(0))
            base = _ticker_stats(df, pd.Series(1.0, index=df.index))
            if stats is None or base is None:
                continue
            per_ticker.append({**stats,
                               "excess_sharpe": stats["sharpe"] - base["sharpe"],
                               "excess_cagr": stats["cagr"] - base["cagr"],
                               "beat_buyhold": stats["total_return"] > base["total_return"]})

        s = pd.DataFrame(per_ticker)
        rows.append({
            "indicator": label,
            "n_tickers": len(s),
            "total_pnl_dollars": s["pnl_dollars"].sum(),
            "avg_pnl_dollars": s["pnl_dollars"].mean(),
            "avg_total_return": s["total_return"].mean(),
            "median_total_return": s["total_return"].median(),
            "avg_cagr": s["cagr"].mean(),
            "avg_sharpe": s["sharpe"].mean(),
            "avg_max_drawdown": s["max_drawdown"].mean(),
            "win_rate": (s["total_return"] > 0).mean(),
            "avg_excess_sharpe": s["excess_sharpe"].mean(),
            "avg_excess_cagr": s["excess_cagr"].mean(),
            "beat_buyhold_rate": s["beat_buyhold"].mean(),
            "n_sharpe": int(s["sharpe"].notna().sum()),
            "avg_exposure": s["exposure"].mean(),
            "rankable": bool(s["sharpe"].notna().sum() >= MIN_SHARPE_COVERAGE * len(s)),
            "generic_fallback": False,
            "is_baseline": False,
            "is_combo": True,
        })

    combo_df = pd.DataFrame(rows).set_index("indicator")
    old = pd.read_csv("../data/indicator_backtest_results.csv", index_col=0)
    old["is_combo"] = False
    merged = pd.concat([old, combo_df], axis=0, sort=False)
    merged.to_csv(OUT_CSV)

    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.max_colwidth", 48)
    bh = merged.loc[BASELINE_NAME]

    cols = ["total_pnl_dollars", "avg_cagr", "avg_sharpe", "avg_max_drawdown",
            "avg_exposure", "win_rate", "beat_buyhold_rate"]

    print(f"\nFull period {master.min():%Y-%m-%d} to {master.max():%Y-%m-%d}, "
          f"{len(tickers)} tickers, ${CAPITAL_PER_TICKER:,}/ticker\n")

    # The report ranks by total dollar P&L, so reproduce that ordering.
    by_pnl = merged[~merged.is_baseline].sort_values("total_pnl_dollars", ascending=False)
    by_pnl.insert(0, "rank", range(1, len(by_pnl) + 1))
    print("=== RANKED BY TOTAL PnL (the report's ordering) ===")
    show = pd.concat([by_pnl.head(8), by_pnl[by_pnl.is_combo]]).drop_duplicates()
    print(show[["rank"] + cols].to_string(float_format=lambda x: f"{x:,.4f}"))
    print(f"\n{'BUYHOLD (baseline)':<50}"
          f"{bh['total_pnl_dollars']:>16,.0f}{bh['avg_cagr']:>10.4f}{bh['avg_sharpe']:>10.4f}"
          f"{bh['avg_max_drawdown']:>10.4f}")

    print("\n=== RANKED BY AVG SHARPE ===")
    by_sh = merged[~merged.is_baseline].sort_values("avg_sharpe", ascending=False)
    by_sh.insert(0, "rank", range(1, len(by_sh) + 1))
    print(pd.concat([by_sh.head(10), by_sh[by_sh.is_combo]]).drop_duplicates()[
        ["rank"] + cols].to_string(float_format=lambda x: f"{x:,.4f}"))

    n_beat_sharpe = int((by_sh[by_sh.is_combo].avg_sharpe > bh["avg_sharpe"]).sum())
    print(f"\ncombos beating BUYHOLD avg_sharpe ({bh['avg_sharpe']:.4f}): "
          f"{n_beat_sharpe} of {len(combo_df)}")
    print(f"combos beating BUYHOLD total PnL (${bh['total_pnl_dollars']:,.0f}): "
          f"{int((combo_df.total_pnl_dollars > bh['total_pnl_dollars']).sum())} of {len(combo_df)}")
    print(f"\nMerged leaderboard -> {OUT_CSV}")


if __name__ == "__main__":
    main()
