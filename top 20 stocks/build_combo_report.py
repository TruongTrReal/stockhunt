"""Pass 3 + payload assembly, in the exact schema `../test research/report/report.js` reads.

That schema is {timeframe: {meta, leaderboard, curves, benchmark_curve, trades}}, with
the leaderboard carrying rank/tldr/is_baseline and the columns report.js sorts on. It is
reproduced field for field so the existing template and renderer can be reused unchanged
rather than reimplemented.

Curves and trades are rebuilt here for only the names that reach the page — the top
CURVES_FOR_TOP by PnL for the overlay chart and the top TOP_N_DETAIL for the drill-down.
Carrying curves for all 1,561 candidates would be roughly 1.4M points per timeframe and
a payload nobody can open; carrying them for 40 is a few hundred KB.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

from combo_sweep import (CAPITAL_PER_TICKER, COST_BPS_GRID, CURVES_FOR_TOP, CURVE_POINTS,
                         HEADLINE_COST_BPS, MAX_TRADES_PER_TICKER, TOP_N_DETAIL,
                         TOP_TICKERS_PER_INDICATOR, deflated_bar, equity_stats,
                         extract_trades, majority, net_returns, positions_for, _ret_of)
from config import BASELINE_NAME
from talib_signals import describe_signal

TS_FMT = "%Y-%m-%d %H:%M"


def position_of(name: str, df, bench_close, flat) -> np.ndarray:
    """Single indicator, BUYHOLD, or a '+'-joined combo resolved by majority vote."""
    members = name.split("+")
    pm = positions_for(members, df, bench_close, flat)
    if len(members) == 1:
        return pm.get(members[0], np.zeros(len(df)))
    P = np.vstack([pm[m] for m in members if m in pm])
    if P.shape[0] < len(members):
        return np.zeros(len(df))
    return majority((P == 1).sum(0), (P == -1).sum(0), len(members))


def tldr_for(name: str, is_baseline: bool, flat: bool) -> str:
    if is_baseline:
        return ("Buy on the first bar and hold to the end — never traded again, and "
                "never flattened overnight. Not a TA-Lib rule; the benchmark every "
                "other row has to beat.")
    members = name.split("+")
    suffix = (" Every position is forced flat at the last bar of each session, so there "
              "is no overnight exposure." if flat else "")
    if len(members) == 1:
        return describe_signal(name) + suffix
    return (f"Majority vote of {len(members)} indicators ({', '.join(members)}). Long only "
            f"when a strict majority agree long, short when a strict majority agree short, "
            f"flat otherwise — so a disagreeing pair, or a three-way 1/1/1 split, sits out."
            + suffix)


def resample_curve(s: pd.Series, n_points: int = CURVE_POINTS) -> list:
    if s.empty:
        return []
    step = max(1, len(s) // n_points)
    t = s.iloc[::step]
    if t.index[-1] != s.index[-1]:
        t = pd.concat([t, s.iloc[[-1]]])
    return [[d.strftime(TS_FMT), round(float(v), 2)] for d, v in t.items()]


def build_payload(res: dict) -> dict:
    lb: pd.DataFrame = res["leaderboard"].copy()
    data, bench_close, flat = res["data"], res["bench_close"], res["flat"]

    lb = lb.sort_values("total_pnl_dollars", ascending=False).reset_index(drop=True)
    rank = 0
    ranks = []
    for _, r in lb.iterrows():
        if r["is_baseline"]:
            ranks.append(None)
        else:
            rank += 1
            ranks.append(rank)
    lb["rank"] = ranks
    lb["tldr"] = [tldr_for(n, b, flat) for n, b in zip(lb["indicator"], lb["is_baseline"])]

    ranked = lb[~lb["is_baseline"]]
    curve_names = ranked.head(CURVES_FOR_TOP)["indicator"].tolist() + [BASELINE_NAME]
    detail_names = ranked.head(TOP_N_DETAIL)["indicator"].tolist() + [BASELINE_NAME]
    need = sorted(set(curve_names) | set(detail_names))

    # ---- pass 3: rebuild only what the page shows ---------------------------
    agg: dict[str, pd.Series] = {}
    trades_out: dict[str, dict] = {n: {} for n in detail_names}
    for ticker, df in data.items():
        close, ret, bpy, times = _ret_of(df)
        for name in need:
            pos = position_of(name, df, bench_close, flat)
            net = net_returns(pos, ret, HEADLINE_COST_BPS)
            st = equity_stats(net, bpy)
            cur = pd.Series(CAPITAL_PER_TICKER * (st["equity"] - 1.0), index=df.index)
            if name in curve_names:
                agg[name] = cur if name not in agg else agg[name].add(cur, fill_value=0.0)
            if name in detail_names:
                tr = extract_trades(pos, close, times, HEADLINE_COST_BPS)
                pnl = tr["pnl_dollars"]
                wins, losses = pnl[pnl > 0], pnl[pnl < 0]
                gp, gl = float(wins.sum()), float(abs(losses.sum()))
                pf = (gp / gl) if gl > 0 else (None if gp > 0 else None)
                tail = slice(-MAX_TRADES_PER_TICKER, None)
                trades_out[name][ticker] = {
                    "stats": {"total_pnl_dollars": CAPITAL_PER_TICKER * float(st["total_return"]),
                              "cagr": _f(st["cagr"]), "sharpe": _f(st["sharpe"]),
                              "max_drawdown": _f(st["max_drawdown"]),
                              "n_trades": int(pnl.size),
                              "win_rate": float((pnl > 0).mean()) if pnl.size else None,
                              "profit_factor": pf,
                              "avg_win_dollars": float(wins.mean()) if wins.size else 0.0,
                              "avg_loss_dollars": float(losses.mean()) if losses.size else 0.0},
                    "curve": resample_curve(cur, 260),
                    "trades": [
                        {"entry_date": pd.Timestamp(times[ei]).strftime(TS_FMT),
                         "exit_date": pd.Timestamp(times[xi]).strftime(TS_FMT),
                         "direction": "long" if d > 0 else "short",
                         "entry_price": round(float(ep), 2), "exit_price": round(float(xp), 2),
                         "pnl_pct": round(float(pp) * 100, 2),
                         "pnl_dollars": round(float(pd_), 2),
                         "holding_days": round(float(hd), 4)}
                        for ei, xi, d, ep, xp, pp, pd_, hd in zip(
                            tr["entry_i"][tail], tr["exit_i"][tail], tr["direction"][tail],
                            tr["entry_price"][tail], tr["exit_price"][tail],
                            tr["pnl_pct"][tail], tr["pnl_dollars"][tail],
                            tr["holding_days"][tail])],
                }

    # keep only the strongest tickers per indicator, as the daily report does
    for name, by_t in trades_out.items():
        keep = sorted(by_t, key=lambda t: by_t[t]["stats"]["total_pnl_dollars"],
                      reverse=True)[:TOP_TICKERS_PER_INDICATOR]
        trades_out[name] = {t: by_t[t] for t in keep}

    curves = {n: resample_curve(s) for n, s in agg.items()}
    idx = next(iter(data.values())).index

    pool = lb[(~lb["is_baseline"]) & lb["rankable"]]
    noise_sd = float(pool["avg_excess_sharpe"].std())
    bar = deflated_bar(res["n_candidates"], noise_sd)
    best = float(pool["avg_excess_sharpe"].max())

    meta = {
        "interval": res["timeframe"],
        "start_date": idx[0].strftime(TS_FMT), "end_date": idx[-1].strftime(TS_FMT),
        "n_tickers": int(lb["n_tickers"].max()),
        "n_indicators": int((~lb["is_baseline"]).sum()),
        "n_generic_fallback": int(lb["generic_fallback"].sum()),
        "capital_per_ticker": CAPITAL_PER_TICKER,
        "total_capital": CAPITAL_PER_TICKER * int(lb["n_tickers"].max()),
        "n_singles": int(((~lb["is_baseline"]) & (lb["combo_size"] == 1)).sum()),
        "n_combos": int((lb["combo_size"] > 1).sum()),
        "cost_bps": HEADLINE_COST_BPS,
        "cost_grid": COST_BPS_GRID,
        "n_candidates": res["n_candidates"],
        "noise_bar_excess_sharpe": bar,
        "best_excess_sharpe": best,
        "beats_noise_bar": bool(best > bar),
        "n_beat_buyhold": int((pool["avg_excess_sharpe"] > 0).sum()),
        "baseline_sharpe": res["base_sharpe"],
    }

    cols = ["rank", "indicator", "tldr", "combo_size", "is_baseline", "generic_fallback",
            "n_tickers", "total_pnl_dollars", "avg_total_return", "avg_cagr", "avg_sharpe",
            "avg_max_drawdown", "avg_excess_sharpe", "avg_ir", "ir_hit_rate",
            "ir_t", "breakeven_bps", "avg_excess_cagr", "stock_win_rate",
            "n_trades", "trade_win_rate", "profit_factor", "avg_holding_days",
            "avg_win_dollars", "avg_loss_dollars", "avg_exposure", "rankable"] + \
           [f"sharpe_{c:g}bps" for c in COST_BPS_GRID]
    board = [{k: _clean(r[k]) for k in cols if k in lb.columns} for _, r in lb.iterrows()]

    return {"meta": meta, "leaderboard": board, "curves": curves,
            "benchmark_curve": curves.get(BASELINE_NAME, []), "trades": trades_out}


def _f(x):
    x = float(x)
    return None if not math.isfinite(x) else x


def _clean(v):
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if not math.isfinite(f) else f
    return v
