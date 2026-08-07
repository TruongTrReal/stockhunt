"""Generate `web/data.js` from the real research results.

Replaces `demo_data.js`. Same shape, so the views need no changes — swapping the script
tag in `index.html` is the whole migration.

What is real here and what is not:

* **Backtest** — every figure comes from `backtest engine/results/wf_summary_*.csv` and
  `wf_per_asset_*.csv`, which are the walk-forward out-of-sample sweeps. Nothing is
  synthesised.
* **Equity curves** — not persisted by the sweep, so rule detail pages omit the chart
  rather than draw an invented one. Adding them means writing the stitched net-return
  series per rule, which is a change to `walkforward.py`, not to this file.
* **Paper trading** — empty until the Nautilus node runs long enough to fill an order and
  write `results/paper_state.json`. An empty list renders as "nothing running yet", which
  is the truth; it must never be padded with placeholders.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist

import pandas as pd

HERE = Path(__file__).resolve().parent
FWD = HERE.parent
REPO = FWD.parent
BM = REPO / "backtest engine" / "results"
sys.path.insert(0, str(REPO / "backtest engine"))
import config as bt                                    # noqa: E402

HEADLINE = {"us_stocks": "retail", "crypto": "binance"}
GROUPS = [("stocks", "us_stocks", "Top 20 US mega-caps", bt.US_STOCKS),
          ("crypto", "crypto", "Top 10 crypto by market cap", bt.CRYPTO)]
TIMEFRAMES = ["1d", "4h"]
TOP_N = 15                       # leaderboard depth; the tail is all worse, not different


def drop_selection_rows(h: pd.DataFrame) -> pd.DataFrame:
    """Remove `IS#1` / `IS#1[combo]` from a leaderboard.

    These are not rules. They are the *act of choosing* a rule, scored as a strategy:
    re-rank everything on each in-sample window, trade whichever led, repeat. That makes
    them the most methodologically important number in the study — and the wrong thing to
    put in a list sorted by IR, where they read as one more candidate someone might pick
    up and trade. They stay in `wf_summary_*.csv` and `cwf_summary_*.csv`, which is where
    the selection-cost question belongs.
    """
    return h[~h["rule"].astype(str).str.startswith("IS#1")]


def noise_ceiling(n: int, years: float) -> float:
    if n < 2 or years <= 0:
        return float("nan")
    return NormalDist().inv_cdf(1.0 - 1.0 / (n + 1)) / (years ** 0.5)


def cagr(total_pct, years):
    """Annualise a compounded return. Over 41 years the raw figure is 74,735% against a
    164,378% benchmark — arithmetically right and completely unreadable. A manager needs
    "18.0%/yr against 20.0%/yr", and the gap in points is then a number that means
    something on sight."""
    if total_pct is None or years in (None, 0) or pd.isna(total_pct):
        return None
    try:
        return round(((1.0 + total_pct / 100.0) ** (1.0 / years) - 1.0) * 100.0, 2)
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


def num(v, d=3):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else round(f, d)


def build_sheet(cls: str, tf: str, universe: list[str]) -> dict | None:
    summ = BM / f"wf_summary_{cls}_{tf}.csv"
    if not summ.exists():
        return None
    df = pd.read_csv(summ)
    scen = HEADLINE[cls]
    h = df[(df.scenario == scen) & df.rankable & ~df.is_baseline]
    h = drop_selection_rows(h)
    if h.empty:
        return None

    years = float(h["years"].median())

    per = {}
    pa_path = BM / f"wf_per_asset_{cls}_{tf}.csv"
    if pa_path.exists():
        pa = pd.read_csv(pa_path)
        pa = pa[pa.scenario == scen]
        # Each asset annualises over *its own* out-of-sample span. The sheet median is
        # 41 years on 1d equities, but META listed in 2012 and NVDA in 1999 — using the
        # median would understate the newer names by a factor of three.
        has_yrs = "years_oos" in pa.columns
        for rule, g in pa.groupby("rule"):
            per[rule] = []
            for r in g.itertuples():
                y = float(getattr(r, "years_oos", float("nan"))) if has_yrs else float("nan")
                if not (y and y == y):                      # NaN or 0 -> fall back
                    y = years
                net_pct = num(getattr(r, "ret_pct", None), 1)
                bh_pct = num(getattr(r, "bench_pct", None), 1)
                per[rule].append({
                    "symbol": str(r.symbol), "ir": num(r.ir_union), "years": round(y, 1),
                    "net_cagr": cagr(net_pct, y), "bh_cagr": cagr(bh_pct, y),
                    # Raw compounded return as well as the annualised rate. The site turns
                    # these into what a fixed stake became, which is the form a P&L
                    # question is actually asked in.
                    "net_pct": net_pct, "bh_pct": bh_pct})

    rows = []
    for r in h.nlargest(TOP_N, "ir_net").itertuples():
        net = num(getattr(r, "net_return_pct", None), 1)
        bh = num(getattr(r, "bench_return_pct", None), 1)
        yrs = float(getattr(r, "years", 0) or 0)
        net_cagr, bh_cagr = cagr(net, yrs), cagr(bh, yrs)
        rows.append({
            "net_cagr": net_cagr, "bh_cagr": bh_cagr,
            "cagr_gap": None if net_cagr is None or bh_cagr is None
                        else round(net_cagr - bh_cagr, 2),
            "rule": str(r.rule),
            "ir_net": num(r.ir_net), "ir_hit_rate": num(r.ir_hit_rate, 2),
            "t_stat": num(r.t_stat, 2), "turnover": num(getattr(r, "turnover_per_year", None), 1),
            "net_pct": net, "bh_pct": bh,
            "excess_pct": num(getattr(r, "excess_return_pct", None), 1),
            "gates": [int(bool(getattr(r, f"gate_{k}"))) for k in ("ir", "breadth", "headroom", "t")],
            "wf_mode": str(getattr(r, "wf_mode", "fixed")),
            "per_asset": per.get(str(r.rule), []),
        })

    combos, combo_corr, n_combos = build_combos(cls, tf, scen)

    return {"timeframe": tf, "years": round(years, 1),
            "folds": int(h["n_folds"].median()) if "n_folds" in h else None,
            "universe": universe, "rows": rows,
            "n_rules": int(len(h)),
            "noise_ceiling": round(noise_ceiling(len(h), years), 2),
            "combos": combos, "n_combos": n_combos,
            "combo_corr": combo_corr,
            "combo_ceiling": round(noise_ceiling(n_combos, years), 2) if n_combos else None}


def build_combos(cls: str, tf: str, scen: str) -> tuple[list, float | None, int]:
    """Walk-forward pairs from `combo_wf.py`.

    Kept in their own list rather than merged into the singles leaderboard, and carrying
    `long_frac`, because on equities a combination's IR is very largely a measure of how
    much of the time it is invested. `corr(IR, long_frac)` is +0.88 on 1d stocks and the
    `or` operator wins because it is the one that spends the most time in the market —
    `MININDEX~MAXINDEX|or` is long 100% of the time, which is to say it *is* buy-and-hold.
    IR against buy-and-hold approaches 0 from below as a rule approaches always-long, so a
    combination sorting above every single is the metric behaving correctly, not an edge.
    Ranking these next to the singles without the exposure beside them would read as the
    best result on the page.
    """
    p = BM / f"cwf_summary_{cls}_{tf}.csv"
    if not p.exists():
        return [], None, 0
    df = pd.read_csv(p)
    h = df[(df.scenario == scen) & df.rankable & ~df.is_baseline]
    h = drop_selection_rows(h)
    if h.empty:
        return [], None, 0

    corr = None
    if "long_frac" in h and h["long_frac"].notna().any():
        c = h[["ir_net", "long_frac"]].corr().iloc[0, 1]
        corr = None if pd.isna(c) else round(float(c), 3)

    rows = []
    for r in h.nlargest(TOP_N, "ir_net").itertuples():
        yrs = float(getattr(r, "years", 0) or 0)
        net, bh = num(getattr(r, "net_return_pct", None), 1), num(getattr(r, "bench_return_pct", None), 1)
        net_cagr, bh_cagr = cagr(net, yrs), cagr(bh, yrs)
        rows.append({
            "rule": str(r.rule), "op": str(getattr(r, "op", "")),
            "ir_net": num(r.ir_net), "ir_hit_rate": num(r.ir_hit_rate, 2),
            "t_stat": num(r.t_stat, 2),
            "long_frac": num(getattr(r, "long_frac", None), 3),
            "short_frac": num(getattr(r, "short_frac", None), 3),
            "exposure": num(getattr(r, "exposure", None), 3),
            "net_cagr": net_cagr, "bh_cagr": bh_cagr,
            "cagr_gap": None if net_cagr is None or bh_cagr is None
                        else round(net_cagr - bh_cagr, 2),
            "net_pct": net, "bh_pct": bh,
            "gates": [int(bool(getattr(r, f"gate_{k}"))) for k in ("ir", "breadth", "headroom", "t")],
        })
    return rows, corr, int(len(h))


def copy_curves() -> dict:
    """Publish `curves_*.json` beside the site, one file per sheet.

    Not inlined into `data.js`. Fifteen rules x twenty assets x two series is several
    megabytes per sheet, and every visitor would parse all four sheets before the
    leaderboard could render — to draw a chart most of them never open. The detail view
    fetches its sheet on demand instead, so the landing page stays small.
    """
    out = HERE / "curves"
    out.mkdir(exist_ok=True)
    index = {}
    for key, cls, _label, _u in GROUPS:
        for tf in TIMEFRAMES:
            src = BM / f"curves_{cls}_{tf}.json"
            if not src.exists():
                continue
            dst = out / f"{key}_{tf}.json"
            dst.write_bytes(src.read_bytes())
            index[f"{key}_{tf}"] = {"file": f"curves/{key}_{tf}.json",
                                    "bytes": dst.stat().st_size,
                                    "rules": list(json.loads(src.read_text(encoding="utf-8")))}
    return index


# Three groups, because they answer three different questions and lumping 330 strategies
# into one list hides that. The mega-caps are the only equity leg that can confirm the
# research (same universe it was ranked on); the ETFs are a transfer test onto instruments
# the study never held; crypto is its own asset class with its own sheet and cost grid.
BRIEF_EQUITIES = ["SPY", "SOXL", "TQQQ"]
PAPER_GROUPS = [
    {"key": "megacap", "label": "20 US mega-caps",
     "note": "The same universe the equity rules were ranked on — the only like-for-like "
             "forward test here.",
     "symbols": list(bt.US_STOCKS)},
    {"key": "etf", "label": "SPY · SOXL · TQQQ",
     "note": "MK's brief. A transfer test: the research never held an ETF, and the two 3x "
             "funds decay against their index in chop.",
     "symbols": BRIEF_EQUITIES},
    {"key": "crypto", "label": "Top 10 crypto",
     "note": "Ranked on the crypto sheet, which has its own rules and its own cost grid.",
     "symbols": list(bt.CRYPTO)},
]


def group_of(symbol: str, cls: str) -> str:
    for g in PAPER_GROUPS:
        if symbol in g["symbols"]:
            return g["key"]
    return "crypto" if cls == "crypto" else "etf"


def paper_state() -> dict:
    """Whatever the live node has written. Absent until it has actually traded."""
    p = FWD / "results" / "paper_state.json"
    if not p.exists():
        return {"strategies": [], "venue": {"name": "Nautilus sandbox",
                                            "balance": 100000, "equity": 100000},
                "feed": {"source": "Twelve Data", "plan": "pro",
                         "status": "not running", "last_bar": "—"}}
    state = json.loads(p.read_text(encoding="utf-8"))
    # Tag membership here rather than in the node: it is a presentation concern, and doing
    # it at build time means the grouping can change without restarting a running desk.
    for s in state.get("strategies", []):
        s["group"] = group_of(s.get("symbol", ""), s.get("cls", ""))
    state["groups"] = [{k: g[k] for k in ("key", "label", "note")} for g in PAPER_GROUPS]
    return state


def main() -> None:
    backtest = {}
    for key, cls, label, universe in GROUPS:
        sheets = [s for s in (build_sheet(cls, tf, universe) for tf in TIMEFRAMES) if s]
        if sheets:
            backtest[key] = {"label": label, "n": len(universe),
                             "universe": universe, "sheets": sheets}

    paper = paper_state()
    curves_index = copy_curves()
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "feed": paper["feed"], "venue": paper["venue"],
        "strategies": paper["strategies"],
        "paper_groups": paper.get("groups", []),
        "research": {
            "gates": [
                {"k": "I", "name": "Information ratio", "target": "0.50 – 1.00",
                 "ask": "Does it beat buy-and-hold, per unit of tracking error?"},
                {"k": "B", "name": "Breadth", "target": "70 – 80%",
                 "ask": "Does it work on most assets, or is one name carrying it?"},
                {"k": "H", "name": "Cost headroom", "target": "3 – 5x",
                 "ask": "Does the edge survive several times the real fee schedule?"},
                {"k": "T", "name": "t-statistic", "target": "2 – 3",
                 "ask": "Is the sample long enough for the result to mean anything?"},
            ],
            "note": ("Across every sheet and every rule tested walk-forward, none has "
                     "cleared all four gates. Anything running in paper is there to "
                     "prove the pipeline, not because it is expected to make money."),
        },
        "backtest": backtest,
        "curves": curves_index,
    }

    out = HERE / "data.js"
    out.write_text("/* GENERATED by build_web_data.py — do not edit. */\n"
                   "window.DEMO = false;\nwindow.DASH = "
                   + json.dumps(payload, separators=(",", ":")) + ";\n",
                   encoding="utf-8")
    print(f"wrote {out}")
    for key, g in backtest.items():
        for s in g["sheets"]:
            best = s["rows"][0] if s["rows"] else {}
            print(f"  {key:7s} {s['timeframe']:3s} {s['n_rules']:3d} rules  "
                  f"{s['years']:5.1f}y  best {best.get('rule','—'):<22} "
                  f"IR {best.get('ir_net')}  CAGR {best.get('net_cagr')}% vs B&H "
                  f"{best.get('bh_cagr')}% (gap {best.get('cagr_gap')})  "
                  f"per-asset {len(best.get('per_asset', []))}")
            cb = s["combos"][0] if s["combos"] else {}
            print(f"  {'':7s} {'':3s} {s['n_combos']:3d} combos  corr(IR,long) "
                  f"{s['combo_corr']}  best {cb.get('rule','—'):<30} "
                  f"IR {cb.get('ir_net')}  long {cb.get('long_frac')}")
    print(f"  paper strategies: {len(payload['strategies'])}")


if __name__ == "__main__":
    main()
