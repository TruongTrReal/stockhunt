"""Same daily-bar backtest as build_report_data.py, but on the 100 most
volatile S&P 500 stocks (by annualized realized volatility since 2020) and
sliced to 2020-01-01 onward - a different, more recent, higher-volatility
regime than the full 501-stock, 2015+ backtest. Reuses cached daily data;
no new downloads needed.
"""

import json
import time

import numpy as np
import pandas as pd

from build_report_data import (
    BASELINE_NAME,
    CAPITAL_PER_TICKER,
    MAX_TRADES_PER_TICKER,
    MIN_ROWS,
    NEEDS_BENCHMARK,
    TOP_N_DETAIL,
    TOP_TICKERS_PER_INDICATOR,
    TRADING_DAYS_PER_YEAR,
    _load_benchmark_close,
    _position_for,
    _resample_curve,
    _sanitize_nans,
    compute_trades,
    ticker_equity_stats,
)
from data_loader import load_universe
from sp500_tickers import get_sp500_tickers
from talib_signals import GENERIC_FALLBACK_FUNCTIONS, describe_signal, get_all_indicator_names

N_TICKERS = 100
START_DATE = "2020-01-01"
OUT_PATH = "../data/report_data_hv.json"


def select_top_volatility_tickers(data: dict[str, pd.DataFrame], n: int, start_date: str) -> list[str]:
    vol = {}
    for ticker, df in data.items():
        sliced = df[df.index >= start_date]
        if len(sliced) < MIN_ROWS:
            continue
        returns = sliced["Close"].pct_change().dropna()
        if len(returns) < 50:
            continue
        vol[ticker] = returns.std() * np.sqrt(TRADING_DAYS_PER_YEAR)
    return sorted(vol, key=vol.get, reverse=True)[:n]


def build():
    start_time = time.time()
    all_tickers = get_sp500_tickers()
    all_data = load_universe(all_tickers)
    benchmark_close = _load_benchmark_close()

    selected = select_top_volatility_tickers(all_data, N_TICKERS, START_DATE)
    data = {t: all_data[t][all_data[t].index >= START_DATE] for t in selected}
    data = {t: df for t, df in data.items() if len(df) >= MIN_ROWS}
    print(f"Selected {len(data)} of top {N_TICKERS} most-volatile tickers with enough post-{START_DATE} history")

    indicator_names = get_all_indicator_names() + [BASELINE_NAME]
    master_index = pd.DatetimeIndex(sorted(set().union(*[df.index for df in data.values()])))

    per_indicator_scalar = {
        name: {
            "pnl_dollars_by_ticker": {}, "total_returns": [], "cagrs": [], "sharpes": [], "max_drawdowns": [],
            "trade_pnls": [], "trade_holding_days": [], "n_tickers": 0,
        }
        for name in indicator_names
    }
    per_indicator_curve = {name: pd.Series(0.0, index=master_index) for name in indicator_names}

    for i, (ticker, df) in enumerate(data.items()):
        for name in indicator_names:
            try:
                position = _position_for(name, df, benchmark_close)
                stats = ticker_equity_stats(df, position)
                trades = compute_trades(df, position)
            except Exception:
                continue
            if stats is None:
                continue

            acc = per_indicator_scalar[name]
            acc["pnl_dollars_by_ticker"][ticker] = stats["pnl_dollars"]
            acc["total_returns"].append(stats["total_return"])
            acc["cagrs"].append(stats["cagr"])
            acc["sharpes"].append(stats["sharpe"])
            acc["max_drawdowns"].append(stats["max_drawdown"])
            acc["trade_pnls"].extend(trades["pnl_dollars"].tolist())
            acc["trade_holding_days"].extend(trades["holding_days"].tolist())
            acc["n_tickers"] += 1

            reindexed = stats["cumulative_dollars"].reindex(master_index).ffill().fillna(0.0)
            per_indicator_curve[name] = per_indicator_curve[name].add(reindexed, fill_value=0.0)

        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(data)} tickers ({time.time() - start_time:.0f}s)")

    print(f"Pass 1 done in {time.time() - start_time:.0f}s")
    n_tested_tickers = max(acc["n_tickers"] for acc in per_indicator_scalar.values())

    leaderboard = []
    for name, acc in per_indicator_scalar.items():
        if acc["n_tickers"] == 0:
            continue
        trade_pnls = np.array(acc["trade_pnls"])
        n_trades = len(trade_pnls)
        wins = trade_pnls[trade_pnls > 0]
        losses = trade_pnls[trade_pnls < 0]
        gross_profit = wins.sum() if wins.size else 0.0
        gross_loss = abs(losses.sum()) if losses.size else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (np.inf if gross_profit > 0 else np.nan)
        is_baseline = name == BASELINE_NAME

        leaderboard.append({
            "indicator": name,
            "tldr": ("Buy on day 1 and hold - never sells. Not a TA-Lib indicator; included purely as the baseline every other row should beat."
                     if is_baseline else describe_signal(name)),
            "generic_fallback": False if is_baseline else name in GENERIC_FALLBACK_FUNCTIONS,
            "is_baseline": is_baseline,
            "n_tickers": acc["n_tickers"],
            "total_pnl_dollars": float(sum(acc["pnl_dollars_by_ticker"].values())),
            "avg_total_return": float(np.nanmean(acc["total_returns"])),
            "avg_cagr": float(np.nanmean(acc["cagrs"])),
            "avg_sharpe": float(np.nanmean(acc["sharpes"])),
            "avg_max_drawdown": float(np.nanmean(acc["max_drawdowns"])),
            "stock_win_rate": float(np.mean(np.array(acc["total_returns"]) > 0)),
            "n_trades": int(n_trades),
            "trade_win_rate": float(np.mean(trade_pnls > 0)) if n_trades else np.nan,
            "profit_factor": None if not np.isfinite(profit_factor) else float(profit_factor),
            "avg_holding_days": float(np.mean(acc["trade_holding_days"])) if acc["trade_holding_days"] else np.nan,
            "avg_win_dollars": float(wins.mean()) if wins.size else 0.0,
            "avg_loss_dollars": float(losses.mean()) if losses.size else 0.0,
        })

    leaderboard.sort(key=lambda r: r["total_pnl_dollars"], reverse=True)
    rank = 0
    for row in leaderboard:
        if row["is_baseline"]:
            row["rank"] = None
            continue
        rank += 1
        row["rank"] = rank
    n_ranked_indicators = rank

    def to_weekly_points(series: pd.Series):
        weekly = series.resample("W-FRI").last().ffill()
        return [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in weekly.items()]

    curves = {name: to_weekly_points(curve) for name, curve in per_indicator_curve.items()}
    benchmark_curve_points = curves[BASELINE_NAME]

    ranked_only = [r for r in leaderboard if not r["is_baseline"]]
    baseline_rows = [r for r in leaderboard if r["is_baseline"]]
    trades_out = {}
    for row in ranked_only[:TOP_N_DETAIL] + baseline_rows:
        name = row["indicator"]
        by_ticker = per_indicator_scalar[name]["pnl_dollars_by_ticker"]
        top_tickers = sorted(by_ticker, key=by_ticker.get, reverse=True)[:TOP_TICKERS_PER_INDICATOR]
        trades_out[name] = {}
        for ticker in top_tickers:
            df = data[ticker]
            position = _position_for(name, df, benchmark_close)
            trades = compute_trades(df, position)
            ticker_stats = ticker_equity_stats(df, position)

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
                "curve": _resample_curve(ticker_stats["cumulative_dollars"], "W-FRI") if ticker_stats else [],
                "trades": [
                    {
                        "entry_date": entry_date.strftime("%Y-%m-%d"),
                        "exit_date": exit_date.strftime("%Y-%m-%d"),
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

    total_capital = CAPITAL_PER_TICKER * n_tested_tickers
    report = {
        "meta": {
            "start_date": master_index.min().strftime("%Y-%m-%d"),
            "end_date": master_index.max().strftime("%Y-%m-%d"),
            "n_tickers": n_tested_tickers,
            "n_indicators": n_ranked_indicators,
            "n_generic_fallback": sum(1 for r in leaderboard if r["generic_fallback"]),
            "capital_per_ticker": CAPITAL_PER_TICKER,
            "total_capital": total_capital,
            "n_skipped_tickers": N_TICKERS - len(data),
        },
        "leaderboard": leaderboard,
        "curves": curves,
        "benchmark_curve": benchmark_curve_points,
        "trades": trades_out,
    }

    with open(OUT_PATH, "w") as f:
        json.dump(_sanitize_nans(report), f, separators=(",", ":"), allow_nan=False)

    elapsed = time.time() - start_time
    print(f"Done in {elapsed:.0f}s. Wrote {OUT_PATH}")
    print(f"Top 5 (excl. baseline): {[r['indicator'] for r in ranked_only[:5]]}")
    baseline_row = next(r for r in leaderboard if r["is_baseline"])
    print(f"Buy & hold baseline total PnL: {baseline_row['total_pnl_dollars']:.0f}")


if __name__ == "__main__":
    build()
