"""Collect every stage's results into one JSON payload for the tracking dashboard.

Reads the research CSVs across all three studies plus the forward-test artefacts, and
snapshots live prices. The dashboard is a *published snapshot*, not a live feed: a
claude.ai artifact cannot reach Twelve Data (no such runtime capability, and the page CSP
blocks external hosts), so freshness comes from re-running this and redeploying.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

import fwd_config
import td_live

BM = fwd_config.BACKTEST_MASTER
TOP20 = fwd_config.REPO / "top 20 stocks"

HEADLINE = {"us_stocks": "retail", "crypto": "binance"}


def _read(path):
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def research_sheets() -> list[dict]:
    """One row per (class, timeframe): best fixed rule, honest IS#1, gates, ceiling."""
    out = []
    meta = _read(BM / "results" / "wf_meta.csv")
    for _, m in meta.iterrows():
        tag = f"{m['class']}_{m['timeframe']}"
        s = _read(BM / "results" / f"wf_summary_{tag}.csv")
        if s.empty:
            continue
        scen = HEADLINE[m["class"]]
        h = s[(s.scenario == scen) & s.rankable & ~s.is_baseline]
        if h.empty:
            continue
        is1 = h[h.wf_mode == "is1_selection"]["ir_net"]
        fixed = h[h.wf_mode == "fixed"]
        best = fixed.nlargest(1, "ir_net").iloc[0] if not fixed.empty else None
        out.append({
            "sheet": tag,
            "asset_class": m["class"],
            "timeframe": m["timeframe"],
            "folds": int(m["n_folds"]),
            "oos_years": round(float(h["years"].median()), 1),
            "best_rule": None if best is None else str(best["rule"]),
            "best_ir": None if best is None else round(float(best["ir_net"]), 3),
            "is1_ir": round(float(is1.iloc[0]), 3) if len(is1) else None,
            "ranking_stability": round(float(m["ranking_stability_spearman"]), 3)
            if pd.notna(m.get("ranking_stability_spearman")) else None,
            "gates_cleared": int((h["gates_passed"] == 4).sum()),
            "n_rankable": int(len(h)),
        })
    return sorted(out, key=lambda r: (r["asset_class"], r["timeframe"]))


def gate_power() -> list[dict]:
    """Whether each sheet can even prove the IR gate, per search size."""
    import numpy as np
    from statistics import NormalDist

    def ceiling(n, years):
        if n < 2 or years <= 0:
            return None
        return NormalDist().inv_cdf(1.0 - 1.0 / (n + 1)) / (years ** 0.5)

    rows = []
    for r in research_sheets():
        y = r["oos_years"]
        rows.append({
            "sheet": r["sheet"], "oos_years": y,
            "t_gate_implies": round(2.0 / (y ** 0.5), 3),
            "ceiling_5": round(ceiling(5, y), 3),
            "ceiling_96": round(ceiling(96, y), 3),
            "ceiling_327": round(ceiling(327, y), 3),
            "coherent": bool(0.50 >= ceiling(327, y)),
        })
    return rows


def etf_sheets() -> list[dict]:
    out = []
    for tf in ("1d", "4h"):
        s = _read(TOP20 / "results" / f"etf_wf_summary_{tf}.csv")
        bh = _read(TOP20 / "results" / f"etf_buyhold_{tf}.csv")
        if s.empty:
            continue
        h = s[(s.cost_bps == 5.0) & ~s.is_baseline]
        best = h.nlargest(1, "ir_net").iloc[0] if not h.empty else None
        is1 = h[h.rule == "IS#1"]["ir_net"]
        out.append({
            "timeframe": tf,
            "best_rule": None if best is None else str(best["rule"]),
            "best_ir": None if best is None else round(float(best["ir_net"]), 3),
            "is1_ir": round(float(is1.iloc[0]), 3) if len(is1) else None,
            "gates_cleared": int((h["gates_passed"] == 4).sum()),
            "n_rules": int(len(h)),
            "buyhold": [{"symbol": r["symbol"], "cagr": round(float(r["cagr"]), 4),
                         "sharpe": round(float(r["sharpe"]), 2),
                         "max_drawdown": round(float(r["max_drawdown"]), 4)}
                        for _, r in bh.iterrows()],
        })
    return out


def prereg() -> list[dict]:
    s = _read(BM / "results" / "prereg_us_stocks_1d.csv")
    if s.empty:
        return []
    h = s[s.scenario == "retail"]
    return [{"rule": str(r["rule"]), "ir": round(float(r["ir_net"]), 3),
             "prior": str(r.get("prior", "")), "contaminated": bool(r.get("contaminated"))}
            for _, r in h.sort_values("ir_net", ascending=False).iterrows()]


def parity() -> list[dict]:
    s = _read(fwd_config.RESULTS_DIR / "parity_live_1d.csv")
    if s.empty:
        return []
    agg = s.groupby("rule")["min_window"].max().sort_values(ascending=False)
    return [{"rule": str(k), "min_window": int(v) if pd.notna(v) else None}
            for k, v in agg.items()]


def live_prices() -> list[dict]:
    rows = []
    for sym in ["SOXL", "TQQQ", "SPY", "BTC/USD", "ETH/USD"]:
        try:
            df = td_live.fetch_bars(sym, "1d", n=2)
            last, prev = df.iloc[-1], df.iloc[-2]
            rows.append({
                "symbol": sym,
                "close": round(float(last["Close"]), 4),
                "change_pct": round(float(last["Close"] / prev["Close"] - 1) * 100, 2),
                "bar_date": str(df.index[-1].date()),
            })
        except Exception as exc:
            rows.append({"symbol": sym, "error": str(exc)[:80]})
    return rows


def main() -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "research": research_sheets(),
        "gate_power": gate_power(),
        "etf": etf_sheets(),
        "prereg": prereg(),
        "parity": parity(),
        "prices": live_prices(),
        "pipeline": {
            "warmup_default": fwd_config.DEFAULT_WINDOW_BARS,
            "warmup_measured": fwd_config.MEASURED_WINDOW_BARS,
            "universe_equity": fwd_config.EQUITY_SYMBOLS,
            "universe_crypto": fwd_config.CRYPTO_SYMBOLS,
            "timeframes": fwd_config.FORWARD_TIMEFRAMES,
        },
    }
    dest = fwd_config.RESULTS_DIR / "dashboard_data.json"
    dest.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"wrote {dest}")
    print(f"  research sheets : {len(payload['research'])}")
    print(f"  etf sheets      : {len(payload['etf'])}")
    print(f"  prereg rules    : {len(payload['prereg'])}")
    print(f"  parity rules    : {len(payload['parity'])}")
    print(f"  live prices     : {len(payload['prices'])}")


if __name__ == "__main__":
    main()
