"""Synthesise a report payload with the right *shape* so the page can be checked
before the sweep exists.

Every number here is fabricated. It exists only to exercise the layout: 2 classes x
7 timeframes x 4 cost levels, real TA-Lib indicator names, and the monotone
degradation with finer timeframes that both earlier studies measured. The page
built from this must be labelled as a layout check, never quoted.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

# Duplicates config.py's shim on purpose: this module must run standalone for a layout
# check, without importing the rest of the pipeline.
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CAPITAL = 10_000.0

CLASSES = [
    {"key": "us_stocks", "label": "US stocks", "noun": "stocks", "n_assets": 20,
     "cost_grid": [0, 1, 5, 10], "headline_cost": 5,
     "tldr": "20 US mega-caps, the same universe as the top-20 study so results stay "
             "comparable. Costs 0/1/5/10bps, headline 5bps. SPY is cached as the "
             "benchmark input for BETA and CORREL but is not in the universe."},
    {"key": "crypto", "label": "Crypto", "noun": "pairs", "n_assets": 10,
     "cost_grid": [0, 5, 10, 20], "headline_cost": 10,
     "tldr": "10 USD pairs by market cap. Charged 0/5/10/20bps with a 10bps headline, "
             "because major-exchange taker fees are ~10bps a side before spread — an "
             "equity cost grid here would manufacture survivors. Survivorship bias is "
             "severe (dead coins are missing) but largely cancels in the IR, since each "
             "asset is measured against buy-and-hold on itself."},
]

TIMEFRAMES = ["1d", "4h", "2h", "1h", "15m", "5m", "1m"]

TIMEFRAME_TLDR = {
    "1d": "Daily bars. Positions held for weeks to years, no forced exit. Deepest history "
          "of any timeframe — equities back to 1980, crypto to 2017 — which matters "
          "because t scales with the square root of the sample length.",
    "4h": "Four-hour bars. Session-aligned for equities, so a US trading day is one 4h bar "
          "plus a 2.5h stub — annualisation is measured from the index, never assumed. "
          "Crypto 4h is a uniform 24/7 grid. History from mid-2019 / 2020.",
    "2h": "Two-hour bars. Same session-alignment caveat as 4h. History from mid-2019 / 2020.",
    "1h": "Hourly bars. The only timeframe where the earlier top-20 study found anything "
          "above parity — 2 rules at 0bps, both gone by 1bp. History from 2019 / 2020.",
    "15m": "Fifteen-minute bars. History from late 2019 / early 2020.",
    "5m": "Five-minute bars, end-of-day flattened for equities so the result is genuine "
          "day-trading and does not quietly collect the overnight drift buy-and-hold "
          "already earns. The benchmark is never flattened. Crypto runs 24/7 and is not "
          "flattened at all.",
    "1m": "One-minute bars — ~3.3M per crypto pair. The finest grid the vendor serves and, "
          "on every prior measurement, the worst: turnover runs to thousands per year, so "
          "the rule that scores best at cost is usually the one that most nearly does "
          "nothing.",
}

GATES = [
    {"key": "ir", "label": "Information ratio, net and out of sample", "letter": "I", "target": "0.50–1.00"},
    {"key": "breadth", "label": "Breadth — share of assets with positive IR", "letter": "B", "target": "70–80%"},
    {"key": "headroom", "label": "Breakeven cost ÷ headline cost", "letter": "H", "target": "3–5×"},
    {"key": "t", "label": "t = IR × √years", "letter": "T", "target": "2–3"},
]

# Best achievable IR per (class, timeframe) at the headline cost — the monotone decay
# with finer bars that both earlier studies measured, and the shape this page has to
# render legibly.
# Tuned so the headline panels land just *under* the t gate — IR 0.52 on an 11.6-year
# daily sample gives t = 0.52 x sqrt(11.6) = 1.77, short of 2. That is the real shape of
# this problem (see the edge-acceptance-criteria note: a genuinely good system is not
# provable on a sample this short), and it means the default view exercises the
# zero-survivor path rather than the celebratory one.
BEST_IR = {
    "us_stocks": {"1d": 0.52, "4h": 0.44, "2h": 0.37, "1h": 0.30, "15m": 0.12, "5m": -0.18, "1m": -0.94},
    "crypto":    {"1d": 0.56, "4h": 0.51, "2h": 0.42, "1h": 0.34, "15m": 0.15, "5m": -0.22, "1m": -1.21},
}
YEARS = {"1d": 11.6, "4h": 7.1, "2h": 7.1, "1h": 7.6, "15m": 6.9, "5m": 6.6, "1m": 6.4}
BARS = {
    "us_stocks": {"1d": 2911, "4h": 3180, "2h": 6310, "1h": 13400, "15m": 45200, "5m": 129700, "1m": 629000},
    "crypto":    {"1d": 3270, "4h": 14500, "2h": 29000, "1h": 57800, "15m": 231000, "5m": 690000, "1m": 3350000},
}
START = {"1d": "2015-01-02", "4h": "2019-06-20", "2h": "2019-06-20", "1h": "2019-01-07",
         "15m": "2019-09-16", "5m": "2020-01-08", "1m": "2020-03-24"}
END = "2026-07-31"

TICKERS = {
    "us_stocks": ["AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "JPM", "JNJ", "XOM",
                  "UNH", "V", "PG", "HD", "MA", "CVX", "ABBV", "PEP", "KO", "WMT"],
    "crypto": ["BTC/USD", "ETH/USD", "XRP/USD", "BNB/USD", "SOL/USD",
               "DOGE/USD", "ADA/USD", "TRX/USD", "AVAX/USD", "LINK/USD"],
}

# 56 panels is the cost of the extra two dimensions, and it bounds how much
# per-candidate detail can be retained: curves and trades for every row would run to
# hundreds of MB. Pass 1 keeps scalars for all 231; pass 2 keeps curves for the top
# CURVES_KEPT and trade detail for the top TOP_N_TRADES — the same two-pass split
# build_report_data.py uses in `test research/`, just tightened for the panel count.
CURVE_POINTS = 80
CURVES_KEPT = 20
TOP_N_TRADES = 5           # candidates that get trade-level detail
TICKERS_PER_INDICATOR = 2


def indicator_names() -> tuple[list[str], set[str], dict]:
    """Real TA-Lib rule names, so the leaderboard is the size it will actually be."""
    try:
        from strategies.talib_signals import (GENERIC_FALLBACK_FUNCTIONS, describe_signal,
                                   get_all_indicator_names)
        names = list(get_all_indicator_names())
        tldr = {}
        for n in names:
            try:
                tldr[n] = describe_signal(n)
            except Exception:
                tldr[n] = "Rule description unavailable."
        return names, set(GENERIC_FALLBACK_FUNCTIONS), tldr
    except Exception as exc:                       # TA-Lib absent — shape still holds
        print(f"  (talib_signals unavailable: {exc}; using placeholder names)")
        names = [f"RULE_{i:03d}" for i in range(231)]
        return names, set(names[:40]), {n: "Placeholder rule." for n in names}


def dates_for(tf: str, n: int) -> list[str]:
    start = date.fromisoformat(START[tf])
    span = (date.fromisoformat(END) - start).days
    return [(start + timedelta(days=int(span * i / (n - 1)))).isoformat() for i in range(n)]


def curve(rng, n: int, final: float, vol: float) -> list[float]:
    """A cumulative-PnL path ending at `final`, with drift, not a straight line."""
    steps = rng.normal(0.0, vol, n)
    path = np.cumsum(steps)
    path = path - path[0]
    if abs(path[-1]) > 1e-9:
        path = path - np.linspace(0, path[-1], n)      # detrend, then impose the endpoint
    return list(np.round(path + np.linspace(0, final, n), 2))


def build() -> dict:
    names, fallback, tldr_map = indicator_names()
    rng = np.random.default_rng(20260803)
    panels = {}

    for cs in CLASSES:
        ckey, n_assets = cs["key"], cs["n_assets"]
        for tf in TIMEFRAMES:
            n_bars = BARS[ckey][tf]
            yrs = YEARS[tf]
            dts = dates_for(tf, CURVE_POINTS)
            # Buy-and-hold is the same series at every cost level — it is never charged
            # a cost and never flattened, so it must not move when the cost tab changes.
            bh_final = CAPITAL * n_assets * (2.4 if ckey == "us_stocks" else 3.1)
            bh_curve = curve(rng, CURVE_POINTS, bh_final, bh_final * 0.02)

            for cost in cs["cost_grid"]:
                head = cs["headline_cost"]
                # IR degrades roughly linearly in cost; at 0bps everything looks better.
                best = BEST_IR[ckey][tf] + (head - cost) * 0.045
                rows, curves, trades = [], {}, {}

                for i, name in enumerate(names):
                    decay = np.exp(-i / 34.0)
                    ir = best * decay + rng.normal(0, 0.05)
                    hit = float(np.clip(0.5 + ir * 0.32 + rng.normal(0, 0.05), 0.05, 0.95))
                    breakeven = max(0.2, (ir + 0.35) * 16.0 + rng.normal(0, 1.5))
                    headroom = breakeven / max(head, 1e-9) if head else float("nan")
                    t_stat = ir * np.sqrt(yrs)
                    loo = float(np.clip(0.62 + rng.normal(0, 0.16), 0.1, 0.99))
                    turnover = {"1d": 24, "4h": 61, "2h": 96, "1h": 152,
                                "15m": 430, "5m": 1180, "1m": 4900}[tf] * (0.4 + decay)
                    total_ret = ir * 0.22 + rng.normal(0, 0.08)
                    pnl = CAPITAL * n_assets * total_ret

                    g_ir = bool(ir >= 0.50)
                    g_breadth = bool(hit >= 0.70)
                    g_head = bool(head > 0 and headroom >= 3.0)
                    g_t = bool(t_stat >= 2.0)

                    rows.append({
                        "indicator": name,
                        "is_baseline": False,
                        "generic_fallback": name in fallback,
                        "tldr": tldr_map.get(name, ""),
                        "n_tickers": n_assets,
                        "ir_net": round(float(ir), 4),
                        "ir_hit_rate": round(hit, 4),
                        "headroom": round(float(headroom), 3) if head else None,
                        "t_stat": round(float(t_stat), 4),
                        "loo_retention": round(loo, 4),
                        "gate_ir": g_ir, "gate_breadth": g_breadth,
                        "gate_headroom": g_head, "gate_t": g_t,
                        "gates_passed": int(g_ir) + int(g_breadth) + int(g_head) + int(g_t),
                        "total_pnl_dollars": round(float(pnl), 2),
                        "avg_cagr": round(float(total_ret / yrs), 4),
                        "avg_sharpe": round(float(ir * 0.8 + 0.35 + rng.normal(0, 0.08)), 3),
                        "profit_factor": round(float(1.0 + ir * 0.35 + rng.normal(0, 0.06)), 3),
                        "trade_win_rate": round(float(np.clip(0.46 + ir * 0.08, 0.2, 0.8)), 4),
                        "avg_max_drawdown": round(float(-0.22 - abs(rng.normal(0, 0.09))), 4),
                        "turnover_per_year": round(float(turnover), 1),
                        "n_trades": int(turnover * yrs / 2),
                        "avg_holding_days": round(float(2 * 365.25 / max(turnover, 1e-6)), 4),
                    })

                rows.sort(key=lambda r: -r["ir_net"])
                for rank, r in enumerate(rows, start=1):
                    r["rank"] = rank

                for r in rows[:CURVES_KEPT]:
                    curves[r["indicator"]] = [
                        [d, v] for d, v in zip(dts, curve(rng, CURVE_POINTS,
                                                          r["total_pnl_dollars"],
                                                          abs(r["total_pnl_dollars"]) * 0.03 + 400))]

                for r in rows[:TOP_N_TRADES]:
                    per_ticker = {}
                    for tk in TICKERS[ckey][:TICKERS_PER_INDICATOR]:
                        tk_pnl = r["total_pnl_dollars"] / n_assets * (0.4 + rng.random() * 1.6)
                        n_tr = max(2, int(r["n_trades"] / n_assets))
                        tr_dates = dates_for(tf, min(n_tr, 24) + 1)
                        tlist = []
                        for k in range(len(tr_dates) - 1):
                            entry = 100 + rng.normal(0, 12)
                            pnl_pct = rng.normal(0.4, 4.0)
                            tlist.append({
                                "entry_date": tr_dates[k], "exit_date": tr_dates[k + 1],
                                "direction": "long" if rng.random() > 0.35 else "short",
                                "entry_price": round(float(entry), 2),
                                "exit_price": round(float(entry * (1 + pnl_pct / 100)), 2),
                                "pnl_pct": round(float(pnl_pct), 3),
                                "pnl_dollars": round(float(CAPITAL * pnl_pct / 100), 2),
                                "holding_days": round(float(2 * 365.25 / max(r["turnover_per_year"], 1e-6)), 4),
                            })
                        per_ticker[tk] = {
                            "stats": {
                                "total_pnl_dollars": round(float(tk_pnl), 2),
                                "cagr": round(float(tk_pnl / CAPITAL / yrs), 4),
                                "sharpe": round(float(r["avg_sharpe"] + rng.normal(0, 0.2)), 3),
                                "profit_factor": round(float(r["profit_factor"] + rng.normal(0, 0.1)), 3),
                                "win_rate": r["trade_win_rate"],
                                "avg_win_dollars": round(float(abs(rng.normal(340, 90))), 2),
                                "avg_loss_dollars": round(float(-abs(rng.normal(300, 80))), 2),
                                "max_drawdown": r["avg_max_drawdown"],
                                "n_trades": n_tr,
                            },
                            "curve": [[d, v] for d, v in zip(
                                dts, curve(rng, CURVE_POINTS, tk_pnl, abs(tk_pnl) * 0.05 + 120))],
                            "trades": tlist,
                        }
                    trades[r["indicator"]] = per_ticker

                baseline = {
                    "indicator": "BUYHOLD", "is_baseline": True, "generic_fallback": False,
                    "tldr": "Buy on the first bar, hold to the last. Never flattened, never "
                            "charged a cost — the thing every rule on this page is trying to beat.",
                    "rank": 0, "n_tickers": n_assets,
                    "ir_net": 0.0, "ir_hit_rate": None, "headroom": None,
                    "t_stat": 0.0, "loo_retention": None,
                    "gate_ir": False, "gate_breadth": False, "gate_headroom": False,
                    "gate_t": False, "gates_passed": 0,
                    "total_pnl_dollars": round(float(bh_final), 2),
                    "avg_cagr": round(float((bh_final / (CAPITAL * n_assets)) ** (1 / yrs) - 1), 4),
                    "avg_sharpe": 0.73 if ckey == "us_stocks" else 0.91,
                    "profit_factor": None, "trade_win_rate": None,
                    "avg_max_drawdown": -0.34 if ckey == "us_stocks" else -0.71,
                    "turnover_per_year": 0.0, "n_trades": n_assets,
                    "avg_holding_days": yrs * 365.25,
                }
                rows.append(baseline)
                curves["BUYHOLD"] = [[d, v] for d, v in zip(dts, bh_curve)]

                panels[f"{ckey}|{tf}|{cost}"] = {
                    "meta": {
                        "start_date": START[tf], "end_date": END,
                        "n_tickers": n_assets, "n_bars": n_bars,
                        "capital_per_ticker": int(CAPITAL),
                        "n_indicators": len(names),
                        "n_curves": CURVES_KEPT,
                        "n_generic_fallback": len([n for n in names if n in fallback]),
                    },
                    "leaderboard": rows,
                    "curves": curves,
                    "benchmark_curve": [[d, v] for d, v in zip(dts, bh_curve)],
                    "trades": trades,
                }

    return {"demo": True, "classes": CLASSES, "timeframes": TIMEFRAMES,
            "timeframe_tldr": TIMEFRAME_TLDR, "gates": GATES, "panels": panels}


if __name__ == "__main__":
    p = build()
    print(f"{len(p['panels'])} panels, "
          f"{len(next(iter(p['panels'].values()))['leaderboard'])} rows each")
