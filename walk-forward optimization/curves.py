"""Equity curves and conventional performance metrics for the leaderboard leaders.

The sweep answers "is this an edge?" and stores only what that needs — an IR, a breadth,
a t-stat. It never kept a bar-by-bar series, so the dashboard had no line to draw and no
drawdown to quote. This fills that gap for the top rules on each sheet.

**Scope is deliberate.** Recomputing curves for all 246 rules x 20 assets would be a
gigabyte of JSON nobody opens; the leaderboard shows 15, so 15 are computed. Everything
here is derived, never a second source of truth: the position comes from `signals`, the
net returns from `engines.vector` and the window from the same walk-forward folds, so a
curve that disagreed with `wf_summary` would be a bug, not a difference of opinion.

**Only `fixed` rules.** A `family_reopt` row (`SMA[WF]`) is a different rule in each fold
and its curve would need the champion schedule stitched in. Those rows are skipped rather
than approximated — a chart labelled `SMA[WF]` showing one fixed SMA would be a lie.

**Metrics are portfolio-level *and* per asset.** The per-asset pass is what answers "how
did this do on AAPL, against just holding AAPL", which is a different question from the
universe average and the one people actually ask.

Run::

    python curves.py                      # every class x 1d/4h in scope
    python curves.py --class crypto --tf 1d --top 10
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np
import pandas as pd
from tqdm import tqdm

from wfo_paths import RESULTS_DIR          # noqa: F401  (wires sys.path first)
from config import (BASELINE_NAME, CLASSES, HEADLINE_SCENARIO, MIN_BARS,
                    scenarios)
from engines import vector
import signals
import td_loader
import walkforward as wf

TOP_N = 15
# What each chart can actually resolve. The headline chart is full width; the per-asset
# ones are thumbnails in a grid, and at 20 assets x 15 rules the difference between 320
# and 140 points is several megabytes the browser has to parse before drawing anything.
CURVE_POINTS = 320
ASSET_CURVE_POINTS = 140
SCOPE_TIMEFRAMES = ["1d", "4h"]


# --------------------------------------------------------------------------- metrics
def _drawdown(equity: np.ndarray) -> tuple[float, int]:
    """Worst peak-to-trough fall, and how many bars the deepest one lasted."""
    if equity.size == 0:
        return float("nan"), 0
    peak = np.maximum.accumulate(equity)
    dd = equity / peak - 1.0
    i = int(np.argmin(dd))
    j = int(np.argmax(equity[:i + 1])) if i else 0
    return float(dd[i]), i - j


def _trades(pos: np.ndarray, ret: np.ndarray) -> list[float]:
    """P&L of each held position, as a fraction.

    A trade is a maximal run of constant non-zero target. That is the unit a win rate and
    a profit factor are normally quoted in — bar-level counts would report a rule that is
    long through a rising year as thousands of "wins" and make every long-biased rule look
    identical.
    """
    out, i, n = [], 0, len(pos)
    while i < n:
        if pos[i] == 0:
            i += 1
            continue
        j = i
        while j + 1 < n and pos[j + 1] == pos[i]:
            j += 1
        r = ret[i:j + 1]
        r = r[np.isfinite(r)]
        if r.size:
            out.append(float(np.prod(1.0 + r) - 1.0))
        i = j + 1
    return out


def trade_stats(trades: list[float], years: float | None = None) -> dict:
    """Win rate, profit factor and friends from a list of per-trade returns.

    Split out from `perf` so the universe view can pool every asset's trades. A basket of
    twenty assets has no single position series, so it has no trades of its own — but
    "across all twenty, 412 trades, 46% of them positive" is exactly what someone means by
    the win rate of a rule, and leaving the whole block empty at portfolio level was
    unhelpful when the answer was one concatenation away.
    """
    if not trades:
        return {}
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t < 0]
    gross_loss = -sum(losses)
    out = {
        "trades": len(trades),
        "win_rate_pct": round(100.0 * len(wins) / len(trades), 1),
        # A rule that never lost has no denominator. `None` renders as "—"; a large
        # sentinel would be read as a real, spectacular number.
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win_pct": round(100.0 * float(np.mean(wins)), 2) if wins else None,
        "avg_loss_pct": round(100.0 * float(np.mean(losses)), 2) if losses else None,
        "best_trade_pct": round(100.0 * max(trades), 2),
        "worst_trade_pct": round(100.0 * min(trades), 2),
    }
    if years and years > 0:
        out["turnover_per_year"] = round(len(trades) / years, 2)
    return out


def perf(ret: np.ndarray, bpy: float, pos: np.ndarray | None = None) -> dict:
    """The conventional set, on a net-return series."""
    r = ret[np.isfinite(ret)]
    if r.size < 2:
        return {}
    equity = np.cumprod(1.0 + r)
    years = r.size / bpy if bpy > 0 else float("nan")
    total = float(equity[-1] - 1.0)
    cagr = float(equity[-1] ** (1.0 / years) - 1.0) if years and years > 0 else float("nan")

    sd = float(np.std(r, ddof=1))
    sharpe = float(np.mean(r) / sd * np.sqrt(bpy)) if sd > 0 else float("nan")
    # Sortino divides by downside deviation only. Zero downside bars means no denominator,
    # not an infinite ratio — report it as undefined rather than a number that would sort
    # to the top of any table it appeared in.
    down = r[r < 0]
    dsd = float(np.std(down, ddof=1)) if down.size > 1 else float("nan")
    sortino = float(np.mean(r) / dsd * np.sqrt(bpy)) if dsd and dsd > 0 else float("nan")

    mdd, mdd_bars = _drawdown(equity)
    calmar = float(cagr / abs(mdd)) if mdd and mdd < 0 and np.isfinite(cagr) else float("nan")

    out = {
        "total_pct": round(total * 100.0, 2),
        "cagr_pct": round(cagr * 100.0, 2) if np.isfinite(cagr) else None,
        "sharpe": round(sharpe, 3) if np.isfinite(sharpe) else None,
        "sortino": round(sortino, 3) if np.isfinite(sortino) else None,
        "max_dd_pct": round(mdd * 100.0, 2) if np.isfinite(mdd) else None,
        "max_dd_bars": int(mdd_bars),
        "calmar": round(calmar, 3) if np.isfinite(calmar) else None,
        "vol_pct": round(sd * np.sqrt(bpy) * 100.0, 2) if sd > 0 else None,
        "bars": int(r.size), "years": round(years, 2) if np.isfinite(years) else None,
    }

    if pos is not None:
        p = np.nan_to_num(pos, nan=0.0)
        out.update(trade_stats(_trades(p, ret), out.get("years")))
        out["exposure_pct"] = round(100.0 * float(np.mean(p != 0)), 1)
        out["long_pct"] = round(100.0 * float(np.mean(p > 0)), 1)
    return out


# --------------------------------------------------------------------------- curves
def curve_points(ret: np.ndarray, index: pd.DatetimeIndex,
                 points: int = CURVE_POINTS) -> tuple[list, list]:
    """Cumulative growth of 100, downsampled by stride.

    Strided rather than resampled so the first and last points are real observations —
    the endpoint has to equal the total return quoted beside it, or the chart and the
    table disagree and nobody knows which to believe.
    """
    r = np.nan_to_num(ret, nan=0.0)
    eq = 100.0 * np.cumprod(1.0 + r)
    n = eq.size
    if n == 0:
        return [], []
    step = max(1, n // points)
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return ([round(float(eq[i]), 2) for i in idx],
            [index[i].strftime("%Y-%m-%d") for i in idx])


def run_sheet(asset_class: str, timeframe: str, top_n: int) -> dict | None:
    summ = RESULTS_DIR / f"wf_summary_{asset_class}_{timeframe}.csv"
    if not summ.exists():
        return None
    scen_key = HEADLINE_SCENARIO[asset_class]
    df = pd.read_csv(summ)
    h = df[(df.scenario == scen_key) & df.rankable & ~df.is_baseline]
    # `fixed` only — see the module docstring. IS#1 and the `[WF]` families are re-selected
    # per fold and have no single position series to draw.
    h = h[h.wf_mode == "fixed"]
    if h.empty:
        return None
    wanted = list(h.nlargest(top_n, "ir_net")["rule"].astype(str))

    data = td_loader.load(asset_class, timeframe)
    data = {s: d for s, d in data.items() if len(d) >= MIN_BARS}
    if not data:
        return None
    start = min(d.index[0] for d in data.values())
    end = max(d.index[-1] for d in data.values())
    folds = wf.generate_folds(start, end)

    free = {"key": "gross", "commission_bps": 0.0, "half_spread_bps": 0.0,
            "sell_fee_bps": 0.0, "borrow_annual": 0.0}
    fee = next(f for f in scenarios(asset_class) if f["key"] == scen_key)

    union, bench = {}, {}
    for symbol, d in data.items():
        ms = wf.fold_masks(d.index, folds)
        oos = [m[1] for m in ms if m is not None]
        if not oos:
            continue
        union[symbol] = np.logical_or.reduce(oos)
        close = d["Close"].to_numpy(dtype="float64")
        bpy = vector.bars_per_year(d.index)
        bench[symbol] = {"net": vector.net_returns(np.ones(len(d)), close, free, bpy),
                         "bpy": bpy, "close": close}

    out = {}
    for rule in tqdm(wanted, desc=f"curves {asset_class}/{timeframe}"):
        assets, port_frames, all_trades, expo = [], [], [], []
        for symbol, d in data.items():
            if symbol not in union:
                continue
            pos = signals.position_for(rule, d, asset_class, timeframe,
                                       baseline_name=BASELINE_NAME)
            if pos is None:
                continue
            b = bench[symbol]
            net = vector.net_returns(pos, b["close"], fee, b["bpy"])
            u = union[symbol]
            idx = d.index[u]
            rnet, rben, rpos = net[u], b["net"][u], np.asarray(pos)[u]

            eq, ts = curve_points(rnet, idx, ASSET_CURVE_POINTS)
            beq, _ = curve_points(rben, idx, ASSET_CURVE_POINTS)
            assets.append({
                "symbol": symbol,
                "curve": eq, "bench": beq, "dates": ts,
                "metrics": perf(rnet, b["bpy"], rpos),
                "bench_metrics": perf(rben, b["bpy"]),
                "ir": round(float(wf._ir(net, b["net"], u, b["bpy"])), 4),
            })
            port_frames.append(pd.DataFrame({"net": rnet, "bench": rben}, index=idx))
            all_trades.extend(_trades(np.nan_to_num(rpos, nan=0.0), rnet))
            expo.append(float(np.mean(np.nan_to_num(rpos, nan=0.0) != 0)))

        if not assets:
            continue
        # Equal weight, rebalanced every bar, across whichever assets are trading that
        # bar. Assets start decades apart (JNJ 1970, META 2012); an equal-weight mean over
        # the union index is the only aggregate that does not silently drop the young ones
        # or back-fill them with zeros.
        port = pd.concat(port_frames).groupby(level=0).mean().sort_index()
        bpy_med = float(np.median([bench[a["symbol"]]["bpy"] for a in assets]))
        peq, pts = curve_points(port["net"].to_numpy(), port.index)
        pbeq, _ = curve_points(port["bench"].to_numpy(), port.index)

        pm = perf(port["net"].to_numpy(), bpy_med)
        # Trade counts pool across assets; the *rate* stays per-asset-mean so the metric
        # is not dominated by whichever asset happens to have the longest history.
        pm.update(trade_stats(all_trades))
        pm["turnover_per_year"] = (round(len(all_trades) / len(assets) / pm["years"], 2)
                                   if pm.get("years") and assets else None)
        pm["exposure_pct"] = round(100.0 * float(np.mean(expo)), 1) if expo else None

        out[rule] = {
            "curve": peq, "bench": pbeq, "dates": pts,
            "metrics": pm,
            "bench_metrics": perf(port["bench"].to_numpy(), bpy_med),
            "assets": sorted(assets, key=lambda a: -a["ir"]),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+", default=SCOPE_TIMEFRAMES)
    ap.add_argument("--top", type=int, default=TOP_N)
    args = ap.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for asset_class in args.classes:
        for timeframe in args.timeframes:
            t0 = time.time()
            res = run_sheet(asset_class, timeframe, args.top)
            if not res:
                print(f"{asset_class}/{timeframe}: nothing to curve, skipped")
                continue
            p = RESULTS_DIR / f"curves_{asset_class}_{timeframe}.json"
            p.write_text(json.dumps(res, separators=(",", ":")), encoding="utf-8")
            first = next(iter(res.values()))
            m = first["metrics"]
            print(f"{asset_class}/{timeframe}: {len(res)} rules, "
                  f"{len(first['assets'])} assets each, {p.stat().st_size / 1e6:.1f} MB, "
                  f"{time.time() - t0:.0f}s")
            print(f"    top rule sharpe {m.get('sharpe')} maxDD {m.get('max_dd_pct')}% "
                  f"calmar {m.get('calmar')} vs bench sharpe "
                  f"{first['bench_metrics'].get('sharpe')}")


if __name__ == "__main__":
    main()
