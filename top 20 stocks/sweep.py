"""Backtest all 231 TA-Lib indicator variants across the top-20 universe, per timeframe.

Method follows the main project's conventions, because departing from them would make
these results incomparable with everything already learned:

  * positions are 1 / 0 / -1 and shifted one bar before multiplying by returns, so a
    signal computed on bar t's close only trades bar t+1 — never look-ahead;
  * returns are floored at -99.9% before compounding, so a short that loses more than
    100% in one bar cannot drive equity negative and flip back positive;
  * ranking is on **excess over buy-and-hold**, not raw Sharpe. In a rising market raw
    Sharpe mostly measures how long-biased a rule is, which is how the earlier sweep
    manufactured "winners" that were really just beta.

Two things this study adds over the daily S&P-wide sweep:

  * **Transaction costs.** A 5-minute rule can turn over hundreds of times a year, so a
    gross-only result is not evidence of anything. Every rule is scored at 0/1/5/10 bps
    per unit of position change, and the headline ranking uses a non-zero cost.
  * **End-of-day flattening at 5m**, so the intraday result is genuine day-trading and
    is not quietly collecting the overnight drift that buy-and-hold already earns.

Run::

    python sweep.py            # all timeframes
    python sweep.py 1d 1h      # a subset
"""

from __future__ import annotations

import sys
import time

import numpy as np
import pandas as pd
from tqdm import tqdm

from config import (
    BASELINE_NAME, CAPITAL_PER_TICKER, COST_BPS_GRID, HEADLINE_COST_BPS,
    MIN_BARS, MIN_SHARPE_COVERAGE, RESULTS_DIR, TIMEFRAMES, UNIVERSE,
)
import td_loader
from talib_signals import (
    BETA_NAME, CORREL_NAME, GENERIC_FALLBACK_FUNCTIONS,
    generate_position, get_all_indicator_names,
)

NEEDS_BENCHMARK = {BETA_NAME, CORREL_NAME}
SECONDS_PER_YEAR = 365.25 * 24 * 3600


def bars_per_year(index: pd.DatetimeIndex) -> float:
    """Measure annualisation empirically rather than assuming 252 / 1638 / 19656.

    Yahoo-style and vendor intraday grids are not perfectly uniform (holidays, half
    days, and in this data two different hourly grids), so a theoretical constant
    would quietly mis-annualise every Sharpe and CAGR on the intraday sheets.
    """
    span = (index[-1] - index[0]).total_seconds()
    if span <= 0:
        return float("nan")
    return len(index) * SECONDS_PER_YEAR / span


def flatten_eod(position: pd.Series) -> pd.Series:
    """Force flat on the last bar of each session (5m day-trading, no overnight)."""
    dates = position.index.normalize()
    last_of_day = ~dates.duplicated(keep="last")
    out = position.copy()
    out[last_of_day] = 0.0
    return out


def stats_for(position: pd.Series, close: pd.Series, bpy: float,
              cost_bps: float) -> dict | None:
    """Trade-level and equity stats for one rule on one ticker at one cost level."""
    ret = close.pct_change()
    held = position.shift(1)
    gross = held * ret

    # Cost is charged on |change in position|, so flat->long costs one side and a
    # long->short reversal costs two. Charged on the bar the change happens.
    turnover = position.diff().abs().fillna(position.abs())
    cost = turnover * (cost_bps / 10_000.0)

    net = (gross - cost).dropna()
    if net.empty:
        return None
    net = net.clip(lower=-0.999)

    equity = (1.0 + net).cumprod()
    total_return = float(equity.iloc[-1] - 1.0)
    n_years = len(net) / bpy if bpy and bpy > 0 else np.nan
    cagr = float(equity.iloc[-1] ** (1.0 / n_years) - 1.0) if n_years and n_years > 0 else np.nan
    std = float(net.std())
    sharpe = float(net.mean() / std * np.sqrt(bpy)) if std > 0 else np.nan
    drawdown = float((equity / equity.cummax() - 1.0).min())

    n_trades = int((position.diff().fillna(position) != 0).sum())
    return {
        "total_return": total_return,
        "pnl_dollars": CAPITAL_PER_TICKER * total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": drawdown,
        "exposure": float((position != 0).mean()),
        "n_trades": n_trades,
        "turnover_per_year": float(turnover.sum() / n_years) if n_years and n_years > 0 else np.nan,
    }


def run_timeframe(timeframe: str) -> pd.DataFrame:
    spec = TIMEFRAMES[timeframe]
    data = td_loader.load(timeframe)
    data = {t: df for t, df in data.items() if len(df) >= MIN_BARS}
    if not data:
        raise SystemExit(f"no cached data for {timeframe} — run td_loader.py {timeframe}")

    benchmark = td_loader.load(timeframe, ["SPY"]).get("SPY")
    benchmark_close = benchmark["Close"] if benchmark is not None else None

    names = get_all_indicator_names()
    if BASELINE_NAME not in names:
        names = names + [BASELINE_NAME]

    rows: list[dict] = []
    for ticker, df in tqdm(data.items(), desc=f"{timeframe} tickers"):
        close = df["Close"]
        bpy = bars_per_year(df.index)

        for name in names:
            try:
                if name == BASELINE_NAME:
                    position = pd.Series(1.0, index=df.index)
                else:
                    kwargs = {}
                    if name in NEEDS_BENCHMARK:
                        if benchmark_close is None:
                            continue
                        kwargs["benchmark_close"] = benchmark_close.reindex(
                            df.index).ffill()
                    position = generate_position(name, df, **kwargs)
                position = pd.Series(position, index=df.index).fillna(0.0)
                # Buy-and-hold is the thing being beaten; flattening it every night
                # would turn the benchmark into a different strategy.
                if spec["flatten_eod"] and name != BASELINE_NAME:
                    position = flatten_eod(position)
            except Exception:
                continue

            for cost_bps in COST_BPS_GRID:
                stats = stats_for(position, close, bpy, cost_bps)
                if stats is None:
                    continue
                rows.append({"timeframe": timeframe, "ticker": ticker,
                             "indicator": name, "cost_bps": cost_bps,
                             "n_bars": len(df), "bars_per_year": bpy, **stats})

    return pd.DataFrame(rows)


def summarise(per_ticker: pd.DataFrame) -> pd.DataFrame:
    """Per-indicator leaderboard, ranked on excess over buy-and-hold."""
    baseline = per_ticker[per_ticker["indicator"] == BASELINE_NAME]
    key = ["timeframe", "ticker", "cost_bps"]
    base = baseline.set_index(key)[["total_return", "cagr", "sharpe"]]
    base.columns = ["base_total_return", "base_cagr", "base_sharpe"]

    merged = per_ticker.merge(base, left_on=key, right_index=True, how="left")
    merged["excess_total_return"] = merged["total_return"] - merged["base_total_return"]
    merged["excess_cagr"] = merged["cagr"] - merged["base_cagr"]
    merged["excess_sharpe"] = merged["sharpe"] - merged["base_sharpe"]
    merged["beat_buyhold"] = merged["total_return"] > merged["base_total_return"]

    grouped = merged.groupby(["timeframe", "cost_bps", "indicator"])
    summary = grouped.agg(
        n_tickers=("ticker", "size"),
        total_pnl_dollars=("pnl_dollars", "sum"),
        avg_total_return=("total_return", "mean"),
        median_total_return=("total_return", "median"),
        avg_cagr=("cagr", "mean"),
        avg_sharpe=("sharpe", "mean"),
        avg_max_drawdown=("max_drawdown", "mean"),
        avg_excess_sharpe=("excess_sharpe", "mean"),
        avg_excess_cagr=("excess_cagr", "mean"),
        beat_buyhold_rate=("beat_buyhold", "mean"),
        win_rate=("total_return", lambda s: float((s > 0).mean())),
        avg_exposure=("exposure", "mean"),
        avg_turnover=("turnover_per_year", "mean"),
        avg_trades=("n_trades", "mean"),
        n_sharpe=("sharpe", lambda s: int(s.notna().sum())),
    ).reset_index()

    summary["rankable"] = summary["n_sharpe"] >= MIN_SHARPE_COVERAGE * summary["n_tickers"]
    summary["generic_fallback"] = summary["indicator"].isin(GENERIC_FALLBACK_FUNCTIONS)
    summary["is_baseline"] = summary["indicator"] == BASELINE_NAME
    return summary, merged


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    requested = [a for a in sys.argv[1:] if a in TIMEFRAMES] or list(TIMEFRAMES)

    all_summary, all_detail = [], []
    for timeframe in requested:
        start = time.time()
        per_ticker = run_timeframe(timeframe)
        summary, detail = summarise(per_ticker)
        all_summary.append(summary)
        all_detail.append(detail)

        detail.to_csv(RESULTS_DIR / f"per_ticker_{timeframe}.csv", index=False)
        summary.to_csv(RESULTS_DIR / f"summary_{timeframe}.csv", index=False)

        head = summary[(summary["cost_bps"] == HEADLINE_COST_BPS)
                       & summary["rankable"] & ~summary["is_baseline"]]
        head = head.sort_values("avg_excess_sharpe", ascending=False)
        n_beat = int((head["avg_excess_sharpe"] > 0).sum())
        print(f"\n=== {timeframe} ({time.time() - start:.0f}s) ===")
        print(f"  {len(head)} rankable rules @ {HEADLINE_COST_BPS:g}bps; "
              f"{n_beat} beat buy-and-hold on avg excess Sharpe")
        print(head.head(8)[["indicator", "avg_excess_sharpe", "avg_sharpe",
                            "avg_cagr", "beat_buyhold_rate", "avg_turnover"]]
              .to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    pd.concat(all_summary).to_csv(RESULTS_DIR / "summary_all.csv", index=False)
    pd.concat(all_detail).to_csv(RESULTS_DIR / "per_ticker_all.csv", index=False)
    print(f"\nwrote {RESULTS_DIR}")


if __name__ == "__main__":
    main()
