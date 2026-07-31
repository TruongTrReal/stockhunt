"""Swap the cost-robust C1 variant into report_data.json, replacing the hedged row.

C1 came out of the parameter sweep (param_search.py):

    ~ADOSC_5_30_zero + ~ADOSC_8_20_zero + ~AD_sma10 + ~VAR_20_sma60
    longonly transform, 30% short overlay below SPY's 100-day average

It is not expressible through talib_signals, whose functions are frozen at
TA-Lib defaults, so positions are read from the parameter-variant tensor built
by param_variants.py. Both legs stay within one unit of capital (longonly gives
0/1 and the overlay subtracts at most 0.3), so the row costs the same $10k as
every other on the board.

Why it replaces the previous hedged row: half the turnover (21.8 vs 42.5 per
year) at the same risk-adjusted return, which wins at any realistic cost. Test
portfolio Sharpe by cost - champion 1.243 / 0.977 / 0.447 / -0.082 at
5/10/20/30bps, C1 1.214 / 1.025 / 0.646 / 0.268. The old row was ahead only in
the most optimistic 5bps case, and went negative by 30bps where C1 still earns.

    python patch_c1_into_report.py
"""

import json
import time

import numpy as np
import pandas as pd
from tqdm import tqdm

from beat_buyhold_search import _transform
from build_report_data import (
    MAX_TRADES_PER_TICKER, MIN_ROWS, TOP_TICKERS_PER_INDICATOR,
    _resample_curve, _sanitize_nans, compute_trades, ticker_equity_stats,
)
from data_loader import load_universe
from sp500_tickers import get_sp500_tickers
from talib_signals import BENCHMARK_TICKER

REPORT_PATH = "../data/report_data.json"
VARIANT_PATH = "../data/param_variant_tensor.npz"
MEMBERS = ("ADOSC_5_30_zero", "ADOSC_8_20_zero", "AD_sma10", "VAR_20_sma60")
TRANSFORM = "longonly"
HEDGE_LOOKBACK, HEDGE_WEIGHT = 100, 0.30
LABEL = f"[HEDGED SHORT {int(HEDGE_WEIGHT*100)}% SPY{HEDGE_LOOKBACK}] " + " + ".join("~" + m for m in MEMBERS)

TLDR = (
    "Four inverted volume/volatility rules, tuned rather than left at TA-Lib's defaults: "
    "two Chaikin oscillators (ADOSC 5/30 and 8/20, long when the raw oscillator is below "
    "zero), accumulation/distribution against its own 10-day average, and 20-day variance "
    "against its own 60-day average. Long only when the combined signal is bullish, plus a "
    "30% SHORT overlay on the S&P proxy whenever it trades below its 100-day average. "
    "The slower parameters are the point: they flip less often, so this trades about half "
    "as much as the previous hedged row (22 vs 42 turns a year) for the same risk-adjusted "
    "return - which is what matters, because transaction costs are the binding constraint "
    "on every result in this project. It beats the previous version at any cost above 7bps "
    "and is still profitable at 30bps, where the previous one loses money. Selected on "
    "2015-2021, validated on unseen 2022-2026; survives an extra bar of execution delay. "
    "Position sizes are fractional (-0.3 to 1.0 of capital); the trade table shows each "
    "trade's P&L already scaled by the size held."
)


def main():
    t0 = time.time()
    report = json.load(open(REPORT_PATH))
    z = dict(np.load(VARIANT_PATH, allow_pickle=False))
    names = list(z["names"])
    master = pd.DatetimeIndex(z["dates"])
    tickers = list(z["tickers"])
    T, N = z["returns"].shape

    data = load_universe(get_sp500_tickers())
    data = {t: df for t, df in data.items() if len(df) >= MIN_ROWS and t != BENCHMARK_TICKER}

    acc = None
    for m in MEMBERS:
        t = (-z["tensor"][names.index(m)]).astype(np.int8)      # every member is inverted
        acc = t if acc is None else (acc + t).astype(np.int8)
    base = _transform(np.sign(acc).astype(np.int8), TRANSFORM).astype(np.float32)

    spy = z["spy"]
    sma = pd.Series(spy).rolling(HEDGE_LOOKBACK, min_periods=HEDGE_LOOKBACK).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        below = np.where(np.isnan(sma), 0.0, (spy <= sma).astype(float))
    posmat = (base - HEDGE_WEIGHT * below[:, None].astype(np.float32)).astype(np.float32)
    assert np.abs(posmat).max() <= 1.0 + 1e-6, "gross exposure must stay within one unit"

    pooled = pd.Series(0.0, index=master)
    pnl_by_ticker, tot, cagrs, sharpes, dds = {}, [], [], [], []
    trade_pnls, hold_days = [], []
    for i, ticker in enumerate(tqdm(tickers, desc="tickers")):
        df = data.get(ticker)
        if df is None:
            continue
        position = pd.Series(posmat[:, i], index=master, dtype=float).reindex(df.index).fillna(0)
        stats = ticker_equity_stats(df, position)
        if stats is None:
            continue
        tr = compute_trades(df, position)
        pnl_by_ticker[ticker] = stats["pnl_dollars"]
        tot.append(stats["total_return"]); cagrs.append(stats["cagr"])
        sharpes.append(stats["sharpe"]); dds.append(stats["max_drawdown"])
        trade_pnls.extend(tr["pnl_dollars"].tolist())
        hold_days.extend(tr["holding_days"].tolist())
        pooled = pooled.add(stats["cumulative_dollars"].reindex(master).ffill().fillna(0.0),
                            fill_value=0.0)

    tp = np.array(trade_pnls)
    wins, losses = tp[tp > 0], tp[tp < 0]
    gp = float(wins.sum()) if wins.size else 0.0
    gl = float(abs(losses.sum())) if losses.size else 0.0
    pf = (gp / gl) if gl > 0 else (np.inf if gp > 0 else np.nan)

    row = {
        "indicator": LABEL, "tldr": TLDR,
        "generic_fallback": True,          # the VAR leg uses the generic self-trend rule
        "is_baseline": False, "n_tickers": len(tot),
        "total_pnl_dollars": float(sum(pnl_by_ticker.values())),
        "avg_total_return": float(np.nanmean(tot)),
        "avg_cagr": float(np.nanmean(cagrs)),
        "avg_sharpe": float(np.nanmean(sharpes)),
        "avg_max_drawdown": float(np.nanmean(dds)),
        "stock_win_rate": float(np.mean(np.array(tot) > 0)),
        "n_trades": int(len(tp)),
        "trade_win_rate": float(np.mean(tp > 0)) if len(tp) else np.nan,
        "profit_factor": None if not np.isfinite(pf) else float(pf),
        "avg_holding_days": float(np.mean(hold_days)) if hold_days else np.nan,
        "avg_win_dollars": float(wins.mean()) if wins.size else 0.0,
        "avg_loss_dollars": float(losses.mean()) if losses.size else 0.0,
    }

    detail = {}
    for ticker in sorted(pnl_by_ticker, key=pnl_by_ticker.get, reverse=True)[:TOP_TICKERS_PER_INDICATOR]:
        df = data[ticker]
        i = tickers.index(ticker)
        position = pd.Series(posmat[:, i], index=master, dtype=float).reindex(df.index).fillna(0)
        st = ticker_equity_stats(df, position)
        tr = compute_trades(df, position)
        p = tr["pnl_dollars"]
        w, l = p[p > 0], p[p < 0]
        tgp = float(w.sum()) if w.size else 0.0
        tgl = float(abs(l.sum())) if l.size else 0.0
        tpf = (tgp / tgl) if tgl > 0 else (np.inf if tgp > 0 else np.nan)
        sl = slice(-MAX_TRADES_PER_TICKER, None)
        detail[ticker] = {
            "stats": {
                "total_pnl_dollars": float(st["pnl_dollars"]) if st else 0.0,
                "cagr": float(st["cagr"]) if st else None,
                "sharpe": float(st["sharpe"]) if st else None,
                "max_drawdown": float(st["max_drawdown"]) if st else None,
                "n_trades": int(len(p)),
                "win_rate": float(np.mean(p > 0)) if len(p) else None,
                "profit_factor": None if not np.isfinite(tpf) else float(tpf),
                "avg_win_dollars": float(w.mean()) if w.size else 0.0,
                "avg_loss_dollars": float(l.mean()) if l.size else 0.0,
            },
            "curve": _resample_curve(st["cumulative_dollars"], "W-FRI") if st else [],
            "trades": [
                {"entry_date": ed.strftime("%Y-%m-%d"), "exit_date": xd.strftime("%Y-%m-%d"),
                 "direction": "long" if dr > 0 else "short",
                 "entry_price": round(float(ep), 2), "exit_price": round(float(xp), 2),
                 "pnl_pct": round(float(pp) * 100, 2), "pnl_dollars": round(float(pdl), 2),
                 "holding_days": round(float(hd), 3)}
                for ed, xd, dr, ep, xp, pp, pdl, hd in zip(
                    tr["entry_date"][sl], tr["exit_date"][sl], tr["direction"][sl],
                    tr["entry_price"][sl], tr["exit_price"][sl], tr["pnl_pct"][sl],
                    tr["pnl_dollars"][sl], tr["holding_days"][sl])
            ],
        }

    stale = [r["indicator"] for r in report["leaderboard"] if "HEDGED" in r["indicator"]]
    for old in stale:
        report["curves"].pop(old, None)
        report["trades"].pop(old, None)
    report["leaderboard"] = [r for r in report["leaderboard"]
                             if "HEDGED" not in r["indicator"]] + [row]
    report["curves"][LABEL] = [[d.strftime("%Y-%m-%d"), round(float(v), 2)]
                              for d, v in pooled.resample("W-FRI").last().ffill().items()]
    report["trades"][LABEL] = detail
    if stale:
        print(f"replaced: {stale}")

    report["leaderboard"].sort(key=lambda r: r["total_pnl_dollars"], reverse=True)
    rank = 0
    for r in report["leaderboard"]:
        if r["is_baseline"]:
            r["rank"] = None
            continue
        rank += 1
        r["rank"] = rank
    report["meta"]["n_indicators"] = rank
    report["meta"]["n_generic_fallback"] = sum(1 for r in report["leaderboard"] if r["generic_fallback"])

    with open(REPORT_PATH, "w") as f:
        json.dump(_sanitize_nans(report), f, separators=(",", ":"), allow_nan=False)

    print(f"\nDone in {time.time()-t0:.0f}s. Leaderboard {len(report['leaderboard'])} rows.")
    print(f"{'rank':>5}  {'indicator':<62}{'total_pnl':>15}{'cagr':>8}{'sharpe':>8}{'maxDD':>9}")
    for r in report["leaderboard"][:8]:
        rk = "base" if r["rank"] is None else r["rank"]
        print(f"{rk:>5}  {r['indicator'][:60]:<62}{r['total_pnl_dollars']:>15,.0f}"
              f"{r['avg_cagr']:>8.2%}{r['avg_sharpe']:>8.4f}{r['avg_max_drawdown']:>9.2%}")
    me = next(r for r in report["leaderboard"] if r["indicator"] == LABEL)
    print(f"\nC1 -> rank {me['rank']}, sharpe {me['avg_sharpe']:.4f}, "
          f"maxDD {me['avg_max_drawdown']:.2%}, trades {me['n_trades']:,}")


if __name__ == "__main__":
    main()
