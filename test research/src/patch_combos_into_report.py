"""Add the validated multi-indicator combos to an existing report_data.json.

A full rebuild re-runs 501 tickers x 231 indicators to regenerate rows that are
not changing. This follows the same pattern as patch_report_data.py: load the
built dataset, compute only the new rows, splice them in, and re-rank. Every
number is produced by build_report_data.py's own `ticker_equity_stats` /
`compute_trades`, so the combo rows are computed identically to the 231 rows
already on the board rather than merely similarly.

Combos are marked `generic_fallback` when any member uses the generic
"value vs. its own SMA" rule - the inverted volatility leg (~VAR / ~STDDEV) does,
and the report should keep flagging that even though here it is the deliberate
point of the rule rather than an accident.

    python patch_combos_into_report.py
"""

import json
import time

import numpy as np
import pandas as pd
from tqdm import tqdm

from beat_buyhold_search2 import Space2
from build_report_data import (
    BASELINE_NAME, CAPITAL_PER_TICKER, MAX_TRADES_PER_TICKER, MIN_ROWS,
    TOP_TICKERS_PER_INDICATOR, _resample_curve, _sanitize_nans, compute_trades,
    ticker_equity_stats,
)
from data_loader import load_universe
from signal_tensor import CACHE_PATH, load
from sp500_tickers import get_sp500_tickers
from talib_signals import BENCHMARK_TICKER, GENERIC_FALLBACK_FUNCTIONS, describe_signal

REPORT_PATH = "../data/report_data.json"

TRANSFORM_TLDR = {
    "defensive": ("Holds long by default and steps aside only when the combined signal turns "
                  "bearish, so it keeps most of buy-and-hold's exposure."),
    "longonly": ("Takes a long position only while the combined signal is bullish, and sits in "
                 "cash otherwise - roughly 40% of the time invested."),
}


def combo_tldr(members, transform):
    parts = []
    for name, sign in members:
        base = describe_signal(name).rstrip(".")
        parts.append(f"{'INVERTED ' if sign < 0 else ''}{name} ({base})")
    return (f"Combination of {len(members)} rules, summed and signed: " + "; ".join(parts) + ". "
            + TRANSFORM_TLDR.get(transform, "")
            + " Selected on 2015-2021 and validated on unseen 2022-2026; the inverted volatility "
              "leg makes this a volatility-timing rule - it holds when volatility is below its "
              "own average, which is what lifts risk-adjusted return above buy-and-hold.")


def main():
    t0 = time.time()
    report = json.load(open(REPORT_PATH))
    z = load(CACHE_PATH)
    sp = Space2(z)
    names = list(z["names"])
    master = pd.DatetimeIndex(z["dates"])
    tickers = list(z["tickers"])

    data = load_universe(get_sp500_tickers())
    data = {t: df for t, df in data.items() if len(df) >= MIN_ROWS and t != BENCHMARK_TICKER}

    fin = pd.read_csv("../data/sharpe_finalists.csv")
    specs = []
    for _, r in fin.iterrows():
        members = [(t.lstrip("~"), -1 if t.startswith("~") else 1) for t in r["combo"].split(" + ")]
        atoms = tuple(sorted((names.index(n), s) for n, s in members))
        specs.append((f"{r['combo']} [{r['transform']}]", members, atoms, r["transform"]))

    existing = {row["indicator"] for row in report["leaderboard"]}
    new_rows, new_curves, new_trades = [], {}, {}

    for label, members, atoms, transform in tqdm(specs, desc="combos"):
        if label in existing:
            continue
        posmat = sp.build2(atoms, None, 1, transform, "none")
        pooled = pd.Series(0.0, index=master)
        pnl_by_ticker, tot, cagrs, sharpes, dds = {}, [], [], [], []
        trade_pnls, hold_days = [], []

        for i, ticker in enumerate(tickers):
            df = data.get(ticker)
            if df is None:
                continue
            position = pd.Series(posmat[:, i], index=master, dtype=float).reindex(df.index).fillna(0)
            stats = ticker_equity_stats(df, position)
            if stats is None:
                continue
            trades = compute_trades(df, position)
            pnl_by_ticker[ticker] = stats["pnl_dollars"]
            tot.append(stats["total_return"]); cagrs.append(stats["cagr"])
            sharpes.append(stats["sharpe"]); dds.append(stats["max_drawdown"])
            trade_pnls.extend(trades["pnl_dollars"].tolist())
            hold_days.extend(trades["holding_days"].tolist())
            pooled = pooled.add(
                stats["cumulative_dollars"].reindex(master).ffill().fillna(0.0), fill_value=0.0)

        tp = np.array(trade_pnls)
        wins, losses = tp[tp > 0], tp[tp < 0]
        gp = float(wins.sum()) if wins.size else 0.0
        gl = float(abs(losses.sum())) if losses.size else 0.0
        pf = (gp / gl) if gl > 0 else (np.inf if gp > 0 else np.nan)

        new_rows.append({
            "indicator": label,
            "tldr": combo_tldr(members, transform),
            "generic_fallback": any(n in GENERIC_FALLBACK_FUNCTIONS for n, _ in members),
            "is_baseline": False,
            "n_tickers": len(tot),
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
        })
        new_curves[label] = [[d.strftime("%Y-%m-%d"), round(float(v), 2)]
                             for d, v in pooled.resample("W-FRI").last().ffill().items()]

        # Drill-down detail for the best tickers, same shape as pass 2.
        top = sorted(pnl_by_ticker, key=pnl_by_ticker.get, reverse=True)[:TOP_TICKERS_PER_INDICATOR]
        detail = {}
        for ticker in top:
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
                     "pnl_pct": round(float(pp) * 100, 2), "pnl_dollars": round(float(pd_), 2),
                     "holding_days": round(float(hd), 3)}
                    for ed, xd, dr, ep, xp, pp, pd_, hd in zip(
                        tr["entry_date"][sl], tr["exit_date"][sl], tr["direction"][sl],
                        tr["entry_price"][sl], tr["exit_price"][sl], tr["pnl_pct"][sl],
                        tr["pnl_dollars"][sl], tr["holding_days"][sl])
                ],
            }
        new_trades[label] = detail

    report["leaderboard"] = [r for r in report["leaderboard"]
                             if r["indicator"] not in new_curves] + new_rows
    report["curves"].update(new_curves)
    report["trades"].update(new_trades)

    report["leaderboard"].sort(key=lambda r: r["total_pnl_dollars"], reverse=True)
    rank = 0
    for row in report["leaderboard"]:
        if row["is_baseline"]:
            row["rank"] = None
            continue
        rank += 1
        row["rank"] = rank
    report["meta"]["n_indicators"] = rank
    report["meta"]["n_generic_fallback"] = sum(1 for r in report["leaderboard"] if r["generic_fallback"])

    with open(REPORT_PATH, "w") as f:
        json.dump(_sanitize_nans(report), f, separators=(",", ":"), allow_nan=False)

    print(f"\nAdded {len(new_rows)} combos in {time.time()-t0:.0f}s -> {REPORT_PATH}")
    print(f"leaderboard now {len(report['leaderboard'])} rows, "
          f"{rank} ranked, {len(report['trades'])} with drill-down detail\n")
    print(f"{'rank':>5}  {'indicator':<50}{'total_pnl':>15}{'cagr':>9}{'sharpe':>9}")
    for row in report["leaderboard"][:10]:
        r = "base" if row["rank"] is None else row["rank"]
        print(f"{r:>5}  {row['indicator'][:48]:<50}{row['total_pnl_dollars']:>15,.0f}"
              f"{row['avg_cagr']:>9.2%}{row['avg_sharpe']:>9.4f}")


if __name__ == "__main__":
    main()
