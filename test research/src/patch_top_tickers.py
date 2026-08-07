"""Regenerate just the `trades` section of an existing report_data*.json with
TOP_TICKERS_PER_INDICATOR tickers per indicator (now 20, was 3) and the new
avg_win_dollars/avg_loss_dollars stats - without re-running the expensive
full pass 1 (231 indicators x every ticker). Only recomputes per-ticker PnL
for the top-10 ranked indicators + baseline, across the whole universe, to
find each one's top-20 contributing tickers.
"""

import json

import numpy as np

from build_report_data import (
    MAX_TRADES_PER_TICKER,
    TOP_N_DETAIL,
    TOP_TICKERS_PER_INDICATOR,
    _resample_curve,
    _sanitize_nans,
    compute_trades,
    ticker_equity_stats,
)


def build_trades_section(out_path, data, position_fn, benchmark_close, periods_per_year=252, resample_freq="W-FRI"):
    """`periods_per_year` MUST match what the original backtest used to
    annualize CAGR/Sharpe (252 for daily bars, but ~19,626 for 5-minute bars
    and ~1,751 for hourly - see each dataset's meta.periods_per_year) or
    those two stats will be silently wrong for every ticker patched here.
    `resample_freq` should match the granularity already used for that
    dataset's aggregate curves ("W-FRI" for 1D/1H, "B" for 5m)."""
    with open(out_path) as f:
        report = json.load(f)

    leaderboard = report["leaderboard"]
    ranked_only = sorted((r for r in leaderboard if not r["is_baseline"]), key=lambda r: r["rank"])
    baseline_rows = [r for r in leaderboard if r["is_baseline"]]
    target_rows = ranked_only[:TOP_N_DETAIL] + baseline_rows

    trades_out = {}
    for row in target_rows:
        name = row["indicator"]
        pnl_by_ticker = {}
        for ticker, df in data.items():
            try:
                position = position_fn(name, df, benchmark_close)
                stats = ticker_equity_stats(df, position, periods_per_year=periods_per_year)
            except Exception:
                continue
            if stats is not None:
                pnl_by_ticker[ticker] = stats["pnl_dollars"]

        top_tickers = sorted(pnl_by_ticker, key=pnl_by_ticker.get, reverse=True)[:TOP_TICKERS_PER_INDICATOR]
        trades_out[name] = {}
        for ticker in top_tickers:
            df = data[ticker]
            position = position_fn(name, df, benchmark_close)
            trades = compute_trades(df, position)
            ticker_stats = ticker_equity_stats(df, position, periods_per_year=periods_per_year)

            trade_pnls = trades["pnl_dollars"]
            wins = trade_pnls[trade_pnls > 0]
            losses = trade_pnls[trade_pnls < 0]
            gross_profit = float(wins.sum()) if wins.size else 0.0
            gross_loss = float(abs(losses.sum())) if losses.size else 0.0
            pf = (gross_profit / gross_loss) if gross_loss > 0 else (np.inf if gross_profit > 0 else np.nan)

            tail = slice(-MAX_TRADES_PER_TICKER, None)
            trades_out[name][ticker] = {
                "stats": {
                    "total_pnl_dollars": float(ticker_stats["pnl_dollars"]) if ticker_stats else 0.0,
                    "cagr": float(ticker_stats["cagr"]) if ticker_stats else None,
                    "sharpe": float(ticker_stats["sharpe"]) if ticker_stats else None,
                    "max_drawdown": float(ticker_stats["max_drawdown"]) if ticker_stats else None,
                    "n_trades": int(len(trade_pnls)),
                    "win_rate": float(np.mean(trade_pnls > 0)) if len(trade_pnls) else None,
                    "profit_factor": None if not np.isfinite(pf) else float(pf),
                    "avg_win_dollars": float(wins.mean()) if wins.size else 0.0,
                    "avg_loss_dollars": float(losses.mean()) if losses.size else 0.0,
                },
                "curve": _resample_curve(ticker_stats["cumulative_dollars"], resample_freq) if ticker_stats else [],
                "trades": [
                    {
                        "entry_date": entry_date.strftime("%Y-%m-%d %H:%M") if hasattr(entry_date, "hour") else entry_date.strftime("%Y-%m-%d"),
                        "exit_date": exit_date.strftime("%Y-%m-%d %H:%M") if hasattr(exit_date, "hour") else exit_date.strftime("%Y-%m-%d"),
                        "direction": "long" if direction > 0 else "short",
                        "entry_price": round(float(entry_price), 2),
                        "exit_price": round(float(exit_price), 2),
                        "pnl_pct": round(float(pnl_pct) * 100, 2),
                        "pnl_dollars": round(float(pnl_dollars), 2),
                        "holding_days": round(float(holding_days), 3),
                    }
                    for entry_date, exit_date, direction, entry_price, exit_price, pnl_pct, pnl_dollars, holding_days in zip(
                        trades["entry_date"][tail], trades["exit_date"][tail], trades["direction"][tail],
                        trades["entry_price"][tail], trades["exit_price"][tail], trades["pnl_pct"][tail],
                        trades["pnl_dollars"][tail], trades["holding_days"][tail],
                    )
                ],
            }
        print(f"  {name}: {len(top_tickers)} tickers")

    report["trades"] = trades_out
    with open(out_path, "w") as f:
        json.dump(_sanitize_nans(report), f, separators=(",", ":"), allow_nan=False)
    print(f"Patched {out_path}")
