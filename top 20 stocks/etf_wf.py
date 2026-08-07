"""Which TA-Lib indicators work on SOXL and TQQQ at 1d and 4h? (MK, 2026-08-05)

Walk-forward, because a single split scores *a rule* and cannot score *choosing* one —
the choice in a finished leaderboard is made with the test column already visible.
Parameters and rule identity are re-selected on each in-sample window and applied to the
next out-of-sample window; only the stitched out-of-sample path is ranked.

**Read the leveraged-ETF warning before reading the results.** SOXL and TQQQ reset their
3x exposure daily, so they compound the daily return three times rather than tripling the
cumulative move. In a choppy market that bleeds — the standard volatility-decay result —
and it makes buy-and-hold a *structurally weak benchmark*. Every number in this repo is an
information ratio against buy-and-hold on the same asset, so on a decaying benchmark a
rule can post a positive excess purely by being out of the market during drawdowns, with
no forecasting skill at all. Two diagnostics are printed to separate the two cases:

* `corr(IR, long_frac)` — if IR is mostly a function of time spent long, the leaderboard
  is ranking exposure, not skill. On unlevered daily equities this ran +0.88, and there it
  meant "the winner has become buy-and-hold". On a *decaying* benchmark the sign flips:
  a strong **negative** correlation means the winners are winning by staying out, which is
  decay avoidance, not prediction.
* the buy-and-hold reference block — SOXL/TQQQ drawdown and CAGR against SPY. If the
  benchmark itself is down 90% peak-to-trough, beating it is a low bar and should be
  described that way.

A rule that only avoids decay is still tradeable, but it is a *risk-management* result and
must be pitched as one. It will not survive being ported to an unlevered instrument.

Run::

    python etf_wf.py                 # 1d and 4h
    python etf_wf.py --tf 1d
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
from tqdm import tqdm

from config import (BASELINE_NAME, COST_BPS_GRID, ETF_START, ETF_UNIVERSE,
                    HEADLINE_COST_BPS, MIN_BARS, RESULTS_DIR, TIMEFRAMES)
from sweep import bars_per_year, flatten_eod
import td_loader

from talib_signals import GENERIC_FALLBACK_FUNCTIONS, generate_position, get_all_indicator_names

# Same rolling geometry as `../backtest master/walkforward.py`, so the ETF sheet can be
# read directly against the unlevered equity sheets rather than needing a translation.
IS_YEARS, OOS_YEARS, STEP_YEARS = 3, 1, 1
MIN_FOLDS = 3
MIN_IS_BARS, MIN_OOS_BARS = 50, 10

# The four acceptance gates, copied from `../backtest master/config.py:GATES` rather than
# imported: that module pulls in a different universe and cost model on import.
GATES = {"ir": 0.50, "breadth": 0.70, "headroom": 3.0, "t": 2.0}
EULER = 0.5772156649015329


@dataclass(frozen=True)
class Fold:
    index: int
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    oos_end: pd.Timestamp


def generate_folds(start: pd.Timestamp, end: pd.Timestamp) -> list[Fold]:
    folds, is_start = [], start
    while True:
        is_end = is_start + pd.DateOffset(years=IS_YEARS)
        oos_end = is_end + pd.DateOffset(years=OOS_YEARS)
        if is_end >= end:
            break
        folds.append(Fold(len(folds), is_start, is_end, min(oos_end, end)))
        if oos_end >= end:
            break
        is_start = is_start + pd.DateOffset(years=STEP_YEARS)
    return folds


def fold_masks(index: pd.DatetimeIndex, folds: list[Fold]) -> list[tuple | None]:
    idx = index.to_numpy()
    out = []
    for f in folds:
        ism = (idx >= np.datetime64(f.is_start)) & (idx < np.datetime64(f.is_end))
        oosm = (idx >= np.datetime64(f.is_end)) & (idx < np.datetime64(f.oos_end))
        ok = ism.sum() >= MIN_IS_BARS and oosm.sum() >= MIN_OOS_BARS
        out.append((ism, oosm) if ok else None)
    return out


def net_returns(pos: np.ndarray, close: np.ndarray, cost_bps: float) -> np.ndarray:
    """Bar-level net return. Convention is identical to `sweep.stats_for`: signal at t
    trades t+1, cost on |change in position|, clipped at -0.999 before compounding."""
    ret = np.empty_like(close)
    ret[0] = 0.0
    ret[1:] = close[1:] / close[:-1] - 1.0
    held = np.empty_like(pos)
    held[0] = 0.0
    held[1:] = pos[:-1]
    delta = np.empty_like(pos)
    delta[0] = pos[0]
    delta[1:] = np.diff(pos)
    net = held * ret - np.abs(delta) * (cost_bps / 10_000.0)
    return np.clip(net, -0.999, None)


def information_ratio(net: np.ndarray, bench: np.ndarray, bpy: float) -> float:
    diff = net - bench
    diff = diff[np.isfinite(diff)]
    if diff.size < 3:
        return float("nan")
    sd = float(np.std(diff, ddof=1))
    if not np.isfinite(sd) or sd <= 0:
        return float("nan")
    return float(np.mean(diff) / sd * np.sqrt(bpy))


def noise_ceiling(n_candidates: int, years: float) -> float:
    """IR the best of `n_candidates` worthless rules reaches by luck."""
    from statistics import NormalDist
    if n_candidates < 2 or not years or years <= 0:
        return float("nan")
    z = NormalDist().inv_cdf(1.0 - 1.0 / (n_candidates + 1))
    return float(z / np.sqrt(years))


def position_for(rule: str, df: pd.DataFrame, timeframe: str,
                 bench_close: pd.Series | None) -> np.ndarray | None:
    if rule == BASELINE_NAME:
        return np.ones(len(df), dtype="float64")
    kwargs = {}
    if rule in ("BETA", "CORREL"):
        if bench_close is None:
            return None
        kwargs["benchmark_close"] = bench_close.reindex(df.index).ffill()
    try:
        raw = generate_position(rule, df, **kwargs)
    except Exception:
        return None
    pos = np.nan_to_num(np.asarray(raw, dtype="float64"), nan=0.0, posinf=0.0, neginf=0.0)
    if pos.size != len(df):
        return None
    if TIMEFRAMES[timeframe]["flatten_eod"]:
        pos = flatten_eod(pd.Series(pos, index=df.index)).to_numpy()
    return pos


def buyhold_reference(data: dict, bench: pd.DataFrame | None,
                      union: dict, bpys: dict) -> pd.DataFrame:
    """What the benchmark itself does. On a 3x ETF this is the whole interpretation."""
    rows = []
    frames = dict(data)
    if bench is not None:
        frames["SPY"] = bench
    for sym, df in frames.items():
        close = df["Close"].to_numpy(dtype="float64")
        bpy = bpys.get(sym) or bars_per_year(df.index)
        u = union.get(sym)
        if u is None:
            u = np.ones(len(close), dtype=bool)
        net = net_returns(np.ones(len(close)), close, 0.0)[u]
        eq = np.cumprod(1.0 + net)
        yrs = u.sum() / bpy
        rows.append({
            "symbol": sym, "years": yrs,
            "cagr": float(eq[-1] ** (1.0 / yrs) - 1.0) if yrs > 0 else np.nan,
            "sharpe": float(np.mean(net) / np.std(net, ddof=1) * np.sqrt(bpy)),
            "max_drawdown": float(np.min(eq / np.maximum.accumulate(eq) - 1.0)),
        })
    return pd.DataFrame(rows)


def run_timeframe(timeframe: str) -> tuple[dict, dict]:
    data = td_loader.load(timeframe, ETF_UNIVERSE)
    data = {s: d for s, d in data.items() if len(d) >= MIN_BARS}
    if not data:
        return {}, {"skipped": "no cached data"}
    bench_frames = td_loader.load(timeframe, ["SPY"])
    bench_close = bench_frames["SPY"]["Close"] if "SPY" in bench_frames else None

    start = min(d.index[0] for d in data.values())
    end = max(d.index[-1] for d in data.values())
    folds = generate_folds(start, end)
    if len(folds) < MIN_FOLDS:
        return {}, {"skipped": f"only {len(folds)} folds"}

    masks = {s: fold_masks(d.index, folds) for s, d in data.items()}
    union = {s: np.logical_or.reduce([m[1] for m in ms if m is not None])
             for s, ms in masks.items() if any(m is not None for m in ms)}
    bpys = {s: bars_per_year(d.index) for s, d in data.items()}
    closes = {s: d["Close"].to_numpy(dtype="float64") for s, d in data.items()}
    benches = {s: net_returns(np.ones(len(closes[s])), closes[s], 0.0) for s in data}

    names = list(get_all_indicator_names())
    if BASELINE_NAME not in names:
        names.append(BASELINE_NAME)

    fold_rows, union_rows, expo_rows = [], [], []
    for rule in tqdm(names, desc=f"ETF WF {timeframe}"):
        for sym, df in data.items():
            if sym not in union:
                continue
            pos = position_for(rule, df, timeframe, bench_close)
            if pos is None:
                continue
            u, bpy, b = union[sym], bpys[sym], benches[sym]
            expo_rows.append({"rule": rule, "symbol": sym,
                              "long_frac": float(np.mean(pos[u] > 0)),
                              "short_frac": float(np.mean(pos[u] < 0)),
                              "exposure": float(np.mean(pos[u] != 0))})
            for cost in COST_BPS_GRID:
                net = net_returns(pos, closes[sym], cost)
                union_rows.append((rule, sym, cost,
                                   information_ratio(net[u], b[u], bpy),
                                   float(u.sum() / bpy)))
                for f, m in zip(folds, masks[sym]):
                    if m is None:
                        continue
                    fold_rows.append((
                        rule, sym, cost, f.index,
                        information_ratio(net[m[0]], b[m[0]], bpy),
                        information_ratio(net[m[1]], b[m[1]], bpy),
                    ))

    folds_df = pd.DataFrame(
        fold_rows, columns=["rule", "symbol", "cost_bps", "fold", "ir_is", "ir_oos"])
    fixed = pd.DataFrame(
        union_rows, columns=["rule", "symbol", "cost_bps", "ir_wf", "years"])
    expo = pd.DataFrame(expo_rows).groupby("rule")[
        ["long_frac", "short_frac", "exposure"]].mean()

    # IS#1: per (symbol, cost, fold) take the highest in-sample IR, then stitch that
    # rule's positions into one array and backtest once, so the fold-boundary switch is
    # charged like any other trade.
    cand = folds_df[(folds_df.rule != BASELINE_NAME) & np.isfinite(folds_df.ir_is)]
    is1_rows = []
    if not cand.empty:
        picks = cand.loc[cand.groupby(["symbol", "cost_bps", "fold"])["ir_is"].idxmax()]
        for (sym, cost), grp in picks.groupby(["symbol", "cost_bps"]):
            df = data[sym]
            stitched = np.zeros(len(df), dtype="float64")
            used = np.zeros(len(df), dtype=bool)
            cache = {}
            for r in grp.itertuples():
                m = masks[sym][r.fold]
                if m is None:
                    continue
                if r.rule not in cache:
                    cache[r.rule] = position_for(r.rule, df, timeframe, bench_close)
                p = cache[r.rule]
                if p is None:
                    continue
                stitched[m[1]] = p[m[1]]
                used |= m[1]
            if not used.any():
                continue
            net = net_returns(stitched, closes[sym], cost)
            chosen = grp.sort_values("fold")["rule"]
            is1_rows.append({"rule": "IS#1", "symbol": sym, "cost_bps": cost,
                             "ir_wf": information_ratio(net[used], benches[sym][used],
                                                        bpys[sym]),
                             "years": float(used.sum() / bpys[sym]),
                             "n_switches": int((chosen != chosen.shift()).sum() - 1)})
    allrows = pd.concat([fixed, pd.DataFrame(is1_rows)], ignore_index=True)

    out = []
    for (rule, cost), grp in allrows.groupby(["rule", "cost_bps"]):
        irs = grp["ir_wf"].to_numpy()
        finite = irs[np.isfinite(irs)]
        if finite.size == 0:
            continue
        years = float(grp["years"].median())
        ir = float(np.mean(finite))
        gross = allrows[(allrows.rule == rule) & (allrows.cost_bps == 0.0)]["ir_wf"].mean()
        drop = gross - ir
        headroom = (0.0 if not np.isfinite(gross) or gross <= 0
                    else float("inf") if drop <= 0 else float(gross / drop))
        row = {"timeframe": timeframe, "rule": rule, "cost_bps": cost,
               "ir_net": ir, "ir_gross": float(gross),
               "ir_hit_rate": float(np.mean(finite > 0)),
               "headroom": headroom, "t_stat": ir * np.sqrt(years) if years > 0 else np.nan,
               "years": years, "n_ir": int(finite.size), "n_assets": int(len(grp)),
               "is_baseline": rule == BASELINE_NAME,
               "generic_fallback": rule in GENERIC_FALLBACK_FUNCTIONS,
               "n_switches": float(grp["n_switches"].median())
               if "n_switches" in grp else 0.0}
        for col in ("long_frac", "short_frac", "exposure"):
            row[col] = float(expo.loc[rule, col]) if rule in expo.index else np.nan
        row["gate_ir"] = bool(row["ir_net"] >= GATES["ir"])
        row["gate_breadth"] = bool(row["ir_hit_rate"] >= GATES["breadth"])
        row["gate_headroom"] = bool(row["headroom"] >= GATES["headroom"])
        row["gate_t"] = bool(np.isfinite(row["t_stat"]) and row["t_stat"] >= GATES["t"])
        row["gates_passed"] = sum(row[f"gate_{g}"] for g in GATES)
        out.append(row)
    summary = pd.DataFrame(out).sort_values(["cost_bps", "ir_net"],
                                            ascending=[True, False])

    years = float(fixed["years"].median())
    meta = {
        "timeframe": timeframe, "n_assets": len(union), "n_rules": len(names),
        "n_folds": len(folds), "years_oos": years,
        "noise_ceiling": noise_ceiling(len(names), years),
        "oos_start": str(folds[0].is_end.date()),
        "oos_end": str(folds[-1].oos_end.date()),
    }
    ref = buyhold_reference(data, bench_frames.get("SPY"), union, bpys)
    return {"summary": summary, "folds": folds_df, "buyhold": ref}, meta


def report(tables: dict, meta: dict) -> None:
    s = tables["summary"]
    h = s[(s.cost_bps == HEADLINE_COST_BPS) & ~s.is_baseline]
    print(f"\n=== SOXL / TQQQ  {meta['timeframe']}  ({meta['seconds']:.0f}s) ===")
    print(f"  {meta['n_folds']} folds ({IS_YEARS}y IS / {OOS_YEARS}y OOS) | "
          f"OOS {meta['oos_start']} -> {meta['oos_end']} ({meta['years_oos']:.1f}y) | "
          f"{meta['n_rules']} rules @ {HEADLINE_COST_BPS:.0f}bps")
    print(f"  noise ceiling for {meta['n_rules']} rules: IR "
          f"{meta['noise_ceiling']:+.3f}  (below this is luck)")

    print("\n  buy-and-hold reference — the benchmark these IRs are measured against:")
    for r in tables["buyhold"].itertuples():
        print(f"    {r.symbol:<5} CAGR {r.cagr:+7.1%}  Sharpe {r.sharpe:+.2f}  "
              f"max DD {r.max_drawdown:+.1%}  over {r.years:.1f}y")

    top = h.nlargest(8, "ir_net")
    print("\n  top 8 rules by walk-forward OOS information ratio:")
    for r in top.itertuples():
        flag = " [generic fallback]" if r.generic_fallback else ""
        print(f"    {r.rule:<24} IR {r.ir_net:+.3f}  breadth {r.ir_hit_rate:.0%}  "
              f"t {r.t_stat:+.2f}  long {r.long_frac:.0%}  "
              f"{r.gates_passed}/4{flag}")

    is1 = s[(s.rule == "IS#1") & (s.cost_bps == HEADLINE_COST_BPS)]
    if not is1.empty:
        r = is1.iloc[0]
        print(f"\n  IS#1 (re-selected each fold — the honest number): "
              f"IR {r['ir_net']:+.3f}, breadth {r['ir_hit_rate']:.0%}, "
              f"t {r['t_stat']:+.2f}, {r['gates_passed']}/4 gates")

    real = h[np.isfinite(h.long_frac)]
    if len(real) > 2:
        c = real["ir_net"].corr(real["long_frac"])
        note = ("winners are winning by staying OUT -> decay avoidance, not prediction"
                if c < -0.4 else
                "winners are winning by staying IN -> converging on buy-and-hold"
                if c > 0.4 else "no simple exposure story")
        print(f"\n  corr(IR, time-long) = {c:+.3f}  <- {note}")

    best = h.nlargest(1, "ir_net")
    if not best.empty:
        r = best.iloc[0]
        print(f"  best rule {r['rule']}: IR {r['ir_net']:+.3f} "
              f"{'ABOVE' if r['ir_net'] > meta['noise_ceiling'] else 'below'} "
              f"the noise ceiling")
    print(f"  rules clearing all four gates: {int((h.gates_passed == 4).sum())} "
          f"of {len(h)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tf", dest="timeframes", nargs="+",
                    default=list(ETF_START), choices=list(TIMEFRAMES))
    args = ap.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    metas = []
    for tf in args.timeframes:
        t0 = time.time()
        tables, meta = run_timeframe(tf)
        if not tables:
            print(f"{tf}: {meta.get('skipped', 'no data')}, skipped")
            continue
        tables["summary"].to_csv(RESULTS_DIR / f"etf_wf_summary_{tf}.csv", index=False)
        tables["folds"].to_csv(RESULTS_DIR / f"etf_wf_folds_{tf}.csv", index=False)
        tables["buyhold"].to_csv(RESULTS_DIR / f"etf_buyhold_{tf}.csv", index=False)
        meta["seconds"] = time.time() - t0
        metas.append(meta)
        report(tables, meta)

    if metas:
        pd.DataFrame(metas).to_csv(RESULTS_DIR / "etf_wf_meta.csv", index=False)
        print(f"\nwrote {RESULTS_DIR}")


if __name__ == "__main__":
    main()
