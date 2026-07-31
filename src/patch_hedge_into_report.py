"""Add the trend-hedged version of the best combo to report_data.json.

The validated hedge (combo_hedge.py) is a risk-matched book carrying leverage,
which would not be comparable on a leaderboard where every other row is a
$10k long/flat/short rule. This adds the UNLEVERED 70/30 form instead:

    position = 0.7 * combo + 0.3 * (SPY above/below its 100-day average)

Both legs are +/-1, so gross exposure per ticker never exceeds 1.0 and the row
costs the same $10k of capital as every other row on the board. Position values
are fractional (-0.3, 0.3, 0.4, 0.7, 1.0) rather than -1/0/1; `compute_trades`
handles that correctly because it scales each trade's P&L by the held position,
but the drill-down's long/short label only carries the sign, so the tldr says so.

    python patch_hedge_into_report.py
"""

import json
import time

import numpy as np
import pandas as pd
from tqdm import tqdm

from beat_buyhold_search2 import Space2
from build_report_data import (
    MAX_TRADES_PER_TICKER, MIN_ROWS, TOP_TICKERS_PER_INDICATOR,
    _resample_curve, _sanitize_nans, compute_trades, ticker_equity_stats,
)
from data_loader import load_universe
from signal_tensor import CACHE_PATH, load
from sp500_tickers import get_sp500_tickers
from talib_signals import BENCHMARK_TICKER, GENERIC_FALLBACK_FUNCTIONS

REPORT_PATH = "../data/report_data.json"
COMBO = "~ADOSC + ~CDLCLOSINGMARUBOZU + ~VAR"
TRANSFORM = "defensive"
HEDGE_LOOKBACK = 100
HEDGE_WEIGHT = 0.50
LABEL = f"[HEDGED SHORT {int(HEDGE_WEIGHT*100)}% SPY{HEDGE_LOOKBACK}] {COMBO}"

# Short-only overlay rather than a full trend leg. hedge_attribution.py showed
# the hedge's long leg is dead weight - it adds back the market beta the short
# leg just paid to remove - and dropping it raises Sharpe at every lookback
# tested. Applied as an OVERLAY on the full-size combo rather than blended
# 70/30, because the short leg is flat roughly half the time, so diluting the
# combo to make room for it would drop gross exposure to 0.41 and give up most
# of the return. Overlay keeps exposure near 0.58.
TLDR = (
    f"The best combo ({COMBO}, held long by default) at full size, plus a "
    f"{int(HEDGE_WEIGHT*100)}% SHORT overlay on the S&P proxy whenever it trades below its "
    f"{HEDGE_LOOKBACK}-day average (and nothing when it is above). The combo is a dip buyer "
    "- it raises exposure into declines and was near fully invested at the 2018 and 2022 "
    "lows - so filters that merely cut exposure made its drawdown worse; a leg that pays "
    "during sustained declines is what works. The hedge's long leg was dropped because it "
    "only added back the market beta the short leg removes. Weight 0.50 is an interior "
    "optimum on 2015-2021 (train Calmar falls off on both sides), applied unchanged to "
    "2022-2026. Position sizes are fractional (-0.5 to 1.0 of capital); the trade table "
    "shows each trade's direction and its P&L already scaled by the size held. Costs you "
    "return in choppy years and pays in crashes."
)


def main():
    t0 = time.time()
    report = json.load(open(REPORT_PATH))
    z = load(CACHE_PATH)
    sp = Space2(z)
    names = list(z["names"])
    master = pd.DatetimeIndex(z["dates"])
    tickers = list(z["tickers"])
    T, N = z["returns"].shape

    data = load_universe(get_sp500_tickers())
    data = {t: df for t, df in data.items() if len(df) >= MIN_ROWS and t != BENCHMARK_TICKER}

    atoms = tuple(sorted((names.index(t.lstrip("~")), -1 if t.startswith("~") else 1)
                         for t in COMBO.split(" + ")))
    base = sp.build2(atoms, None, 1, TRANSFORM, "none").astype(np.float32)
    spy = z["spy"]
    sma = pd.Series(spy).rolling(HEDGE_LOOKBACK, min_periods=HEDGE_LOOKBACK).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        below = np.where(np.isnan(sma), 0.0, (spy <= sma).astype(float))
    # Overlay, not a blend: the combo keeps full size and the short is added on
    # top. The two legs never both take a positive position, so gross exposure
    # still peaks at exactly 1.0 and the row costs the same $10k as any other.
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
        "generic_fallback": any(n.lstrip("~") in GENERIC_FALLBACK_FUNCTIONS
                                for n in COMBO.split(" + ")),
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

    # Drop any previously published hedged variant, not just this exact label,
    # so re-running with different hedge parameters replaces rather than stacks.
    stale = [r["indicator"] for r in report["leaderboard"] if "HEDGED" in r["indicator"]]
    for old in stale:
        report["curves"].pop(old, None)
        report["trades"].pop(old, None)
    report["leaderboard"] = [r for r in report["leaderboard"]
                             if "HEDGED" not in r["indicator"]] + [row]
    if stale:
        print(f"replaced stale hedged row(s): {stale}")
    report["curves"][LABEL] = [[d.strftime("%Y-%m-%d"), round(float(v), 2)]
                              for d, v in pooled.resample("W-FRI").last().ffill().items()]
    report["trades"][LABEL] = detail

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

    print(f"\nAdded in {time.time()-t0:.0f}s. Leaderboard now {len(report['leaderboard'])} rows.")
    print(f"{'rank':>5}  {'indicator':<58}{'total_pnl':>15}{'cagr':>9}{'sharpe':>9}{'maxDD':>9}")
    for r in report["leaderboard"][:8]:
        rk = "base" if r["rank"] is None else r["rank"]
        print(f"{rk:>5}  {r['indicator'][:56]:<58}{r['total_pnl_dollars']:>15,.0f}"
              f"{r['avg_cagr']:>9.2%}{r['avg_sharpe']:>9.4f}{r['avg_max_drawdown']:>9.2%}")
    hedged = next(r for r in report["leaderboard"] if r["indicator"] == LABEL)
    print(f"\nhedged row -> rank {hedged['rank']} by PnL, "
          f"avg_sharpe {hedged['avg_sharpe']:.4f}, avg_max_drawdown {hedged['avg_max_drawdown']:.2%}")


if __name__ == "__main__":
    main()
