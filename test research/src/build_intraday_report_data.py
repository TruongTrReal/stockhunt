"""Same backtest as build_report_data.py, but on intraday bars (5m or 1h)
with every position forced flat at the last bar of each trading day - genuine
day-trading, zero overnight exposure. Reuses the daily pipeline's trade
extraction and equity-curve math unchanged (both are bar-size-agnostic);
only the EOD-flatten step, annualization factor, and data source are new.

Yahoo's free intraday history is hard-capped (5m: ~60 days, 1h: ~730 days),
so this runs on a 50-ticker liquidity-ranked subset of the S&P 500, not all
501 - the full download would be slow and unreliable at this granularity,
and the sample is already much smaller than the daily backtest's 10+ years.
"""

import json
import time

import numpy as np
import pandas as pd

from build_report_data import (
    BASELINE_NAME,
    CAPITAL_PER_TICKER,
    MAX_TRADES_PER_TICKER,
    NEEDS_BENCHMARK,
    TOP_N_DETAIL,
    TOP_TICKERS_PER_INDICATOR,
    _resample_curve,
    _sanitize_nans,
    compute_trades,
    ticker_equity_stats,
)
from intraday_loader import load_intraday_universe
from talib_signals import (
    BETA_NAME,
    CORREL_NAME,
    GENERIC_FALLBACK_FUNCTIONS,
    describe_signal,
    generate_position,
    get_all_indicator_names,
)

BENCHMARK_TICKER = "SPY"
MIN_ROWS = {"5m": 500, "1h": 500}
CURVE_RESAMPLE = {"5m": "B", "1h": "W-FRI"}  # "B" = business day, skips weekend buckets entirely
TRADING_DAYS_PER_YEAR = 252

# 5m stays genuine day-trading (flat every night, zero overnight exposure).
# 1h instead holds positions indefinitely across day/week boundaries, same
# as the 1D backtest - just on hourly bars instead of daily ones.
EOD_FLATTEN = {"5m": True, "1h": False}


def flatten_eod(position: pd.Series) -> pd.Series:
    """Force position to 0 at the last bar of each calendar day, so the
    overnight return (last bar of day D -> first bar of day D+1) always
    gets multiplied by zero once shifted for execution - no overnight risk."""
    dates = position.index.tz_localize(None).normalize() if position.index.tz is not None else position.index.normalize()
    dates = dates.to_numpy()
    is_last_of_day = np.r_[dates[:-1] != dates[1:], True]
    flat = position.copy()
    flat.iloc[is_last_of_day] = 0.0
    return flat


def _position_for(name: str, df: pd.DataFrame, benchmark_close: pd.Series, flatten: bool = True) -> pd.Series:
    if name == BASELINE_NAME:
        raw = pd.Series(1.0, index=df.index)
    else:
        kwargs = {"benchmark_close": benchmark_close} if name in NEEDS_BENCHMARK else {}
        raw = generate_position(name, df, **kwargs)
    return flatten_eod(raw) if flatten else raw


def _bars_per_year(data: dict[str, pd.DataFrame]) -> float:
    """Empirical annualization factor: (avg bars/ticker) / (avg trading days
    spanned) * 252, rather than assuming a theoretical session length."""
    ratios = []
    for df in data.values():
        n_days = len(pd.Index(df.index.date).unique())
        if n_days > 0:
            ratios.append(len(df) / n_days)
    return float(np.mean(ratios)) * TRADING_DAYS_PER_YEAR


def build(interval: str, out_path: str, tickers: list[str] | None = None):
    """`tickers` should be passed explicitly (not left as None/glob-all) once
    the cache directory has ever held more than one ticker universe - stale
    files from a previous selection (e.g. a different liquidity/volatility
    cut) would otherwise silently get mixed back in."""
    start_time = time.time()
    load_list = list(tickers) + [BENCHMARK_TICKER] if tickers is not None else None
    all_data = load_intraday_universe(load_list, interval)
    benchmark_close = all_data.pop(BENCHMARK_TICKER, None)
    if benchmark_close is not None:
        benchmark_close = benchmark_close["Close"]
    data = {t: df for t, df in all_data.items() if len(df) >= MIN_ROWS[interval]}
    n_skipped = len(all_data) - len(data)

    periods_per_year = _bars_per_year(data)
    flatten = EOD_FLATTEN[interval]
    print(f"{interval}: {len(data)} tickers, empirical periods_per_year={periods_per_year:.0f}, eod_flatten={flatten}")

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
                position = _position_for(name, df, benchmark_close, flatten=flatten)
                stats = ticker_equity_stats(df, position, periods_per_year=periods_per_year)
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

        if (i + 1) % 10 == 0:
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
            "tldr": ("Buy at the open of each day, sell at the close - zero overnight exposure, every day, for the whole window. Not a TA-Lib indicator; the baseline every intraday rule should beat."
                     if is_baseline else describe_signal(name) + " Flattened at the close of every trading day (no overnight positions)."),
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

    def to_points(series: pd.Series):
        # "B" skips weekends, but US market holidays (Thanksgiving, July 4th,
        # ...) can still land on a business day with zero trading - ffill so
        # those show "unchanged from last close", not a fake drop to zero.
        resampled = series.resample(CURVE_RESAMPLE[interval]).last().ffill()
        return [[d.strftime("%Y-%m-%d %H:%M"), round(float(v), 2)] for d, v in resampled.items()]

    curves = {name: to_points(curve) for name, curve in per_indicator_curve.items()}
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
            position = _position_for(name, df, benchmark_close, flatten=flatten)
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
                "curve": _resample_curve(ticker_stats["cumulative_dollars"], CURVE_RESAMPLE[interval]) if ticker_stats else [],
                "trades": [
                    {
                        "entry_date": entry_date.strftime("%Y-%m-%d %H:%M"),
                        "exit_date": exit_date.strftime("%Y-%m-%d %H:%M"),
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
            "interval": interval,
            "start_date": master_index.min().strftime("%Y-%m-%d %H:%M"),
            "end_date": master_index.max().strftime("%Y-%m-%d %H:%M"),
            "n_tickers": n_tested_tickers,
            "n_indicators": n_ranked_indicators,
            "n_generic_fallback": sum(1 for r in leaderboard if r["generic_fallback"]),
            "capital_per_ticker": CAPITAL_PER_TICKER,
            "total_capital": total_capital,
            "n_skipped_tickers": n_skipped,
            "periods_per_year": round(periods_per_year),
        },
        "leaderboard": leaderboard,
        "curves": curves,
        "benchmark_curve": benchmark_curve_points,
        "trades": trades_out,
    }

    with open(out_path, "w") as f:
        json.dump(_sanitize_nans(report), f, separators=(",", ":"), allow_nan=False)

    elapsed = time.time() - start_time
    print(f"Done in {elapsed:.0f}s. Wrote {out_path}")
    print(f"Top 5 (excl. baseline): {[r['indicator'] for r in ranked_only[:5]]}")
    baseline_row = next(r for r in leaderboard if r["is_baseline"])
    print(f"Buy & hold baseline total PnL: {baseline_row['total_pnl_dollars']:.0f}")


if __name__ == "__main__":
    import sys
    from sp500_tickers import get_sp500_tickers

    interval = sys.argv[1] if len(sys.argv) > 1 else "5m"
    out = f"../data/report_data_{interval}.json"
    build(interval, out, tickers=get_sp500_tickers())
