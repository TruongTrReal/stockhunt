"""Build the full dataset behind the TA-Lib indicator report: a leaderboard
with trade-level performance stats (PF, win rate, Sharpe, max DD, ...), a
portfolio-level cumulative $PnL curve per indicator, and sample trade
histories for the top indicators - all serialized to one JSON file that the
report page embeds directly (the page is a static artifact with no backend).

Two passes over the universe:
  1. Every (ticker, indicator) pair -> summary stats + trade P&L pooled into
     running per-indicator accumulators, and each ticker's cumulative-$PnL
     series summed into a per-indicator aggregate curve. Cheap: only scalars
     and one running array are kept per indicator, never every trade.
  2. Only for the top N indicators by total PnL: re-fetch full trade records
     (entry/exit dates & prices) for each indicator's top-K tickers, capped
     to the most recent MAX_TRADES_PER_TICKER trades.
"""

import json
import time

import numpy as np
import pandas as pd

from data_loader import load_universe
from sp500_tickers import get_sp500_tickers
from talib_signals import (
    BETA_NAME,
    CORREL_NAME,
    GENERIC_FALLBACK_FUNCTIONS,
    describe_signal,
    generate_position,
    get_all_indicator_names,
)

TRADING_DAYS_PER_YEAR = 252
CAPITAL_PER_TICKER = 10_000
MIN_ROWS = 100
NEEDS_BENCHMARK = {BETA_NAME, CORREL_NAME}
TOP_N_DETAIL = 10
TOP_TICKERS_PER_INDICATOR = 20
MAX_TRADES_PER_TICKER = 40
OUT_PATH = "../data/report_data.json"
BASELINE_NAME = "BUYHOLD"  # not a TA-Lib function - buy on day 1, hold, never trade again


def _sanitize_nans(obj):
    """Python's json.dump writes bare NaN/Infinity tokens (a non-standard
    extension) that JS's JSON.parse rejects. Replace with None recursively."""
    if isinstance(obj, dict):
        return {k: _sanitize_nans(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_nans(v) for v in obj]
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return obj


def _load_benchmark_close():
    from data_loader import update_universe, DEFAULT_START
    from talib_signals import BENCHMARK_TICKER
    bench = load_universe([BENCHMARK_TICKER])
    if BENCHMARK_TICKER not in bench:
        update_universe(tickers=[BENCHMARK_TICKER], start=DEFAULT_START)
        bench = load_universe([BENCHMARK_TICKER])
    return bench[BENCHMARK_TICKER]["Close"]


_EMPTY_TRADES = {
    "direction": np.array([]), "entry_date": pd.DatetimeIndex([]), "exit_date": pd.DatetimeIndex([]),
    "entry_price": np.array([]), "exit_price": np.array([]), "holding_days": np.array([]),
    "pnl_pct": np.array([]), "pnl_dollars": np.array([]),
}


def compute_trades(df: pd.DataFrame, position: pd.Series) -> dict:
    """Pure-numpy trade extraction: one entry per contiguous held position (a
    discrete "trade"). Avoids pandas groupby, which is too slow to call ~80k
    times (501 tickers x 161 indicators)."""
    idx = df.index
    close = df["Close"].to_numpy(dtype=float)
    pos = position.shift(1).fillna(0).to_numpy()
    n = len(pos)
    if n == 0:
        return dict(_EMPTY_TRADES)

    boundary = np.empty(n, dtype=bool)
    boundary[0] = True
    boundary[1:] = pos[1:] != pos[:-1]
    seg_start = np.flatnonzero(boundary)
    seg_end = np.r_[seg_start[1:] - 1, n - 1]

    direction = pos[seg_start]
    keep = (direction != 0) & (seg_start > 0)
    seg_start, seg_end, direction = seg_start[keep], seg_end[keep], direction[keep]
    if len(seg_start) == 0:
        return dict(_EMPTY_TRADES)

    entry_price = close[seg_start - 1]
    exit_price = close[seg_end]
    keep_price = entry_price != 0
    seg_start, seg_end, direction = seg_start[keep_price], seg_end[keep_price], direction[keep_price]
    entry_price, exit_price = entry_price[keep_price], exit_price[keep_price]

    # Actual elapsed calendar time (entry timestamp to exit timestamp), not a
    # bar count - a bar count means something different on every timeframe
    # (2906 daily bars ~= 2906 days, but 76.9 5-minute bars ~= 6.4 hours, not
    # "76.9 days"), so it can't be compared or labeled consistently.
    holding_days = (idx[seg_end].values - idx[seg_start].values) / np.timedelta64(1, "D")
    pnl_pct = direction * (exit_price / entry_price - 1)
    pnl_dollars = CAPITAL_PER_TICKER * pnl_pct

    return {
        "direction": direction,
        "entry_date": idx[seg_start],
        "exit_date": idx[seg_end],
        "entry_price": entry_price,
        "exit_price": exit_price,
        "holding_days": holding_days,
        "pnl_pct": pnl_pct,
        "pnl_dollars": pnl_dollars,
    }


def ticker_equity_stats(df: pd.DataFrame, position: pd.Series, periods_per_year: float = TRADING_DAYS_PER_YEAR):
    """Compounding equity curve stats for one ticker (matches the original
    report's "Total PnL" definition - gains compound within a ticker's own
    history). `periods_per_year` annualizes CAGR/Sharpe correctly regardless
    of bar size - 252 for daily bars, ~19,656 for 5-minute bars, ~1,638 for
    hourly bars (see build_intraday_report_data.py)."""
    daily_return = df["Close"].pct_change()
    strategy_return = (position.shift(1) * daily_return).dropna()
    if strategy_return.empty:
        return None
    strategy_return = strategy_return.clip(lower=-0.999)
    equity_curve = (1 + strategy_return).cumprod()
    cumulative_dollars = CAPITAL_PER_TICKER * (equity_curve - 1)

    total_return = equity_curve.iloc[-1] - 1
    n_years = len(strategy_return) / periods_per_year
    cagr = equity_curve.iloc[-1] ** (1 / n_years) - 1 if n_years > 0 else np.nan
    std = strategy_return.std()
    sharpe = (strategy_return.mean() / std) * np.sqrt(periods_per_year) if std else np.nan
    running_max = equity_curve.cummax()
    max_drawdown = (equity_curve / running_max - 1).min()

    return {
        "total_return": total_return,
        "pnl_dollars": CAPITAL_PER_TICKER * total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "cumulative_dollars": cumulative_dollars,  # pd.Series, for curve aggregation
    }


def _resample_curve(series: pd.Series, resample_freq: str) -> list:
    """Downsample a cumulative-$ series to a small, chart-friendly point list.
    ffill guards against a bucket with no trading in it (a weekend if
    resampling calendar days, a market holiday if resampling "B"/"W-FRI")
    resampling to an empty bucket -> NaN -> None -> JS silently reads `null`
    as 0 in arithmetic, which reads as a fake crash to zero."""
    resampled = series.resample(resample_freq).last().ffill()
    return [[d.strftime("%Y-%m-%d %H:%M") if hasattr(d, "hour") and (d.hour or d.minute) else d.strftime("%Y-%m-%d"),
             round(float(v), 2)] for d, v in resampled.items()]


def _position_for(name: str, df: pd.DataFrame, benchmark_close: pd.Series) -> pd.Series:
    if name == BASELINE_NAME:
        return pd.Series(1.0, index=df.index)  # buy on day 1, hold forever, never trade again
    kwargs = {"benchmark_close": benchmark_close} if name in NEEDS_BENCHMARK else {}
    return generate_position(name, df, **kwargs)


def build():
    start_time = time.time()
    tickers = get_sp500_tickers()
    data = load_universe(tickers)
    indicator_names = get_all_indicator_names() + [BASELINE_NAME]
    benchmark_close = _load_benchmark_close()

    master_index = pd.DatetimeIndex(sorted(set().union(*[df.index for df in data.values()])))

    per_indicator_scalar = {
        name: {
            "pnl_dollars_by_ticker": {},  # ticker -> final $pnl (for ranking + top-K selection)
            "total_returns": [], "cagrs": [], "sharpes": [], "max_drawdowns": [],
            "trade_pnls": [], "trade_holding_days": [],
            "n_tickers": 0,
        }
        for name in indicator_names
    }
    per_indicator_curve = {name: pd.Series(0.0, index=master_index) for name in indicator_names}

    n_skipped = 0
    for i, (ticker, df) in enumerate(data.items()):
        if len(df) < MIN_ROWS:
            n_skipped += 1
            continue
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

        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(data)} tickers processed ({time.time() - start_time:.0f}s elapsed)")

    print(f"Pass 1 done in {time.time() - start_time:.0f}s. Skipped {n_skipped} thin-history tickers.")

    n_tested_tickers = max(acc["n_tickers"] for acc in per_indicator_scalar.values())

    # --- Leaderboard ----------------------------------------------------
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
            "tldr": "Buy on day 1 and hold - never sells. Not a TA-Lib indicator; included purely as the baseline every other row should beat." if is_baseline else describe_signal(name),
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

    # --- Curves (weekly-resampled for a small embeddable payload) --------
    def to_weekly_points(series: pd.Series):
        # ffill guards against a Friday that's a market holiday (no trading
        # that day), which would otherwise resample to a fake drop to zero.
        weekly = series.resample("W-FRI").last().ffill()
        return [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in weekly.items()]

    curves = {name: to_weekly_points(curve) for name, curve in per_indicator_curve.items()}
    benchmark_curve_points = curves[BASELINE_NAME]

    # --- Pass 2: full trade history + per-ticker stats for the top-N ranked
    # indicators' top-K tickers, plus the baseline (always included, doesn't
    # count against the top-N slots) --------------------------------------
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
            ticker_pf = (gross_profit / gross_loss) if gross_loss > 0 else (np.inf if gross_profit > 0 else np.nan)

            tail_slice = slice(-MAX_TRADES_PER_TICKER, None)
            trades_out[name][ticker] = {
                "stats": {
                    "total_pnl_dollars": float(ticker_stats["pnl_dollars"]) if ticker_stats else 0.0,
                    "cagr": float(ticker_stats["cagr"]) if ticker_stats else None,
                    "sharpe": float(ticker_stats["sharpe"]) if ticker_stats else None,
                    "max_drawdown": float(ticker_stats["max_drawdown"]) if ticker_stats else None,
                    "n_trades": int(len(trade_pnls)),
                    "win_rate": float(np.mean(trade_pnls > 0)) if len(trade_pnls) else None,
                    "profit_factor": None if not np.isfinite(ticker_pf) else float(ticker_pf),
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
                        trades["entry_date"][tail_slice], trades["exit_date"][tail_slice], trades["direction"][tail_slice],
                        trades["entry_price"][tail_slice], trades["exit_price"][tail_slice], trades["pnl_pct"][tail_slice],
                        trades["pnl_dollars"][tail_slice], trades["holding_days"][tail_slice],
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
            "n_skipped_tickers": n_skipped,
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
    print(f"Buy & hold baseline total PnL: {baseline_row['total_pnl_dollars']:.0f} "
          f"(would rank #{sum(1 for r in ranked_only if r['total_pnl_dollars'] > baseline_row['total_pnl_dollars']) + 1} if ranked)")


if __name__ == "__main__":
    build()
