"""Patch report_data.json without re-running the full (3.5min) pass 1:
adds a `tldr` field to every leaderboard row, and upgrades `trades[name][ticker]`
from a flat trade list to {stats, trades} so the report can show per-ticker
PF/WR/PnL/Sharpe instead of always repeating the indicator-wide aggregate.
"""

import json

import numpy as np
import pandas as pd

from build_report_data import (
    CAPITAL_PER_TICKER,
    MAX_TRADES_PER_TICKER,
    NEEDS_BENCHMARK,
    OUT_PATH,
    _load_benchmark_close,
    _sanitize_nans,
    compute_trades,
    ticker_equity_stats,
)
from data_loader import load_universe
from sp500_tickers import get_sp500_tickers
from talib_signals import describe_signal, generate_position


def patch():
    with open(OUT_PATH) as f:
        report = json.load(f)

    for row in report["leaderboard"]:
        row["tldr"] = describe_signal(row["indicator"])

    tickers_needed = sorted({t for by_ticker in report["trades"].values() for t in by_ticker})
    print(f"Recomputing per-ticker stats for {len(report['trades'])} indicators x tickers: {tickers_needed}")
    data = load_universe(tickers_needed)
    benchmark_close = _load_benchmark_close()

    new_trades_out = {}
    for name, by_ticker in report["trades"].items():
        new_trades_out[name] = {}
        for ticker in by_ticker:
            df = data[ticker]
            kwargs = {"benchmark_close": benchmark_close} if name in NEEDS_BENCHMARK else {}
            position = generate_position(name, df, **kwargs)
            trades = compute_trades(df, position)
            stats = ticker_equity_stats(df, position)

            trade_pnls = trades["pnl_dollars"]
            wins = trade_pnls[trade_pnls > 0]
            losses = trade_pnls[trade_pnls < 0]
            gross_profit = float(wins.sum()) if wins.size else 0.0
            gross_loss = float(abs(losses.sum())) if losses.size else 0.0
            pf = (gross_profit / gross_loss) if gross_loss > 0 else (np.inf if gross_profit > 0 else np.nan)

            tail = slice(-MAX_TRADES_PER_TICKER, None)
            new_trades_out[name][ticker] = {
                "stats": {
                    "total_pnl_dollars": float(stats["pnl_dollars"]) if stats else 0.0,
                    "cagr": float(stats["cagr"]) if stats else None,
                    "sharpe": float(stats["sharpe"]) if stats else None,
                    "max_drawdown": float(stats["max_drawdown"]) if stats else None,
                    "n_trades": int(len(trade_pnls)),
                    "win_rate": float(np.mean(trade_pnls > 0)) if len(trade_pnls) else None,
                    "profit_factor": None if not np.isfinite(pf) else float(pf),
                },
                "trades": [
                    {
                        "entry_date": entry_date.strftime("%Y-%m-%d"),
                        "exit_date": exit_date.strftime("%Y-%m-%d"),
                        "direction": "long" if direction > 0 else "short",
                        "entry_price": round(float(entry_price), 2),
                        "exit_price": round(float(exit_price), 2),
                        "pnl_pct": round(float(pnl_pct) * 100, 2),
                        "pnl_dollars": round(float(pnl_dollars), 2),
                        "holding_days": int(holding_days),
                    }
                    for entry_date, exit_date, direction, entry_price, exit_price, pnl_pct, pnl_dollars, holding_days in zip(
                        trades["entry_date"][tail], trades["exit_date"][tail], trades["direction"][tail],
                        trades["entry_price"][tail], trades["exit_price"][tail], trades["pnl_pct"][tail],
                        trades["pnl_dollars"][tail], trades["holding_days"][tail],
                    )
                ],
            }

    report["trades"] = new_trades_out

    with open(OUT_PATH, "w") as f:
        json.dump(_sanitize_nans(report), f, separators=(",", ":"), allow_nan=False)
    print(f"Patched {OUT_PATH}")


if __name__ == "__main__":
    patch()
