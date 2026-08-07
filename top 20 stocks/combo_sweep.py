"""2- and 3-way indicator combos on 1h and 5m bars, ranked beside the singles.

Mirrors `../test research/src/combo_backtest.py`: take the top TOP_N single
indicators, form every 2- and 3-way combination, and resolve each combo to one
-1/0/1 position per bar by majority vote (a 2-member combo whose members disagree,
or a 3-member combo split 1/1/1, goes flat).

Three things differ from the daily study, and all three are deliberate:

  * **Costs are charged.** The daily combo run was gross-only. At 5m a rule can turn
    over hundreds of times a year, so gross numbers are not evidence of anything. Every
    row is scored across COST_BPS_GRID and the headline uses HEADLINE_COST_BPS.
  * **The benchmark is never flattened.** At 5m the rules are forced flat at each
    session close but BUYHOLD is not, because flattening the benchmark turns it into a
    different strategy and hands the rules the overnight drift buy-and-hold already
    earns. That single defect is what made the old 5m sheet show 51 "winners".
  * **The multiple-testing bar is reported.** 231 singles + 1330 combos is 1561
    candidates; the best of 1561 draws from pure noise looks good by construction. The
    payload carries the excess-Sharpe threshold a candidate must clear to mean anything.

Trade-level stats are net: each round trip is charged entry and exit cost, so profit
factor and win rate describe what the trade actually returned rather than what the
price did.

    python combo_sweep.py            # 1h and 5m
    python combo_sweep.py 5m         # one timeframe
"""

from __future__ import annotations

import itertools
import json
import math
import sys
import time
from statistics import NormalDist

import numpy as np
import pandas as pd
from tqdm import tqdm

from config import (BASELINE_NAME, CAPITAL_PER_TICKER, COST_BPS_GRID, HEADLINE_COST_BPS,
                    MIN_BARS, MIN_SHARPE_COVERAGE, TIMEFRAMES, UNIVERSE)
import td_loader
from sweep import bars_per_year, flatten_eod
from talib_signals import (BETA_NAME, CORREL_NAME, GENERIC_FALLBACK_FUNCTIONS,
                           describe_signal, generate_position, get_all_indicator_names)

NEEDS_BENCHMARK = {BETA_NAME, CORREL_NAME}
TOP_N = 20
COMBO_SIZES = (2, 3)
SELECT_METRIC = "avg_excess_sharpe"
TOP_N_DETAIL = 10
TOP_TICKERS_PER_INDICATOR = 20
MAX_TRADES_PER_TICKER = 40
CURVE_POINTS = 700          # resampled chart resolution, keeps the payload small
CURVES_FOR_TOP = 40
OUT_JSON = "report_data_combo.json"


# --------------------------------------------------------------------- mechanics

def net_returns(pos: np.ndarray, ret: np.ndarray, cost_bps: float) -> np.ndarray:
    """Position is shifted one bar before earning; cost is charged on |change|."""
    held = np.empty_like(pos)
    held[..., 0] = 0.0
    held[..., 1:] = pos[..., :-1]
    turn = np.abs(np.diff(pos, axis=-1, prepend=0.0))
    return np.clip(held * ret - turn * (cost_bps / 1e4), -0.999, None)


def equity_stats(net: np.ndarray, bpy: float) -> dict:
    """Vectorised over the leading axis: net may be (n_bars,) or (n_rows, n_bars)."""
    eq = np.cumprod(1.0 + net, axis=-1)
    total = eq[..., -1] - 1.0
    n = net.shape[-1]
    yrs = n / bpy if bpy and bpy > 0 else np.nan
    with np.errstate(all="ignore"):
        cagr = np.where(eq[..., -1] > 0, np.power(np.abs(eq[..., -1]), 1.0 / yrs) - 1.0, np.nan)
        sd = net.std(axis=-1)
        sharpe = np.where(sd > 0, net.mean(axis=-1) / np.where(sd > 0, sd, 1) * math.sqrt(bpy), np.nan)
        dd = (eq / np.maximum.accumulate(eq, axis=-1) - 1.0).min(axis=-1)
    return {"total_return": total, "cagr": cagr, "sharpe": sharpe, "max_drawdown": dd,
            "equity": eq}


def extract_trades(pos: np.ndarray, close: np.ndarray, times: np.ndarray,
                   cost_bps: float) -> dict:
    """Runs of constant non-zero position, priced entry-to-exit and charged both sides."""
    p = pos.astype(np.int8)
    change = np.flatnonzero(np.diff(p, prepend=np.int8(0)) != 0)
    if change.size == 0:
        return {k: np.array([]) for k in
                ("pnl_dollars", "pnl_pct", "holding_days", "direction",
                 "entry_price", "exit_price", "entry_i", "exit_i")}
    ends = np.append(change[1:], p.size) - 1
    keep = p[change] != 0
    starts, ends = change[keep], ends[keep]
    # Entry fills on the bar after the signal (positions are shifted before earning).
    ent = np.minimum(starts + 1, p.size - 1)
    ext = np.minimum(ends + 1, p.size - 1)
    direction = p[starts].astype(float)
    ep, xp = close[ent], close[ext]
    gross = direction * (xp / ep - 1.0)
    pnl_pct = gross - 2.0 * cost_bps / 1e4
    secs = (times[ext] - times[ent]) / np.timedelta64(1, "s")
    return {"pnl_dollars": CAPITAL_PER_TICKER * pnl_pct, "pnl_pct": pnl_pct,
            "holding_days": secs / 86400.0, "direction": direction,
            "entry_price": ep, "exit_price": xp, "entry_i": ent, "exit_i": ext}


def trade_aggregate(pnl: np.ndarray, hold: np.ndarray) -> dict:
    n = int(pnl.size)
    if n == 0:
        return {"n_trades": 0, "trade_win_rate": np.nan, "profit_factor": None,
                "avg_holding_days": np.nan, "avg_win_dollars": 0.0, "avg_loss_dollars": 0.0}
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    gp, gl = float(wins.sum()), float(abs(losses.sum()))
    pf = (gp / gl) if gl > 0 else (None if gp > 0 else np.nan)
    return {"n_trades": n, "trade_win_rate": float((pnl > 0).mean()),
            "profit_factor": pf, "avg_holding_days": float(hold.mean()),
            "avg_win_dollars": float(wins.mean()) if wins.size else 0.0,
            "avg_loss_dollars": float(losses.mean()) if losses.size else 0.0}


def majority(counts_long: np.ndarray, counts_short: np.ndarray, size: int) -> np.ndarray:
    """Strict majority of MEMBERS, so a 3-way split 1/1/1 and a disagreeing pair go flat."""
    out = np.zeros(counts_long.shape, dtype=np.float64)
    out[counts_long * 2 > size] = 1.0
    out[counts_short * 2 > size] = -1.0
    return out


def deflated_bar(n_candidates: int, se: float, alpha: float = 0.05) -> float:
    ppf = NormalDist().inv_cdf
    g = 0.5772156649
    e_max = (1 - g) * ppf(1 - 1 / n_candidates) + g * ppf(1 - 1 / (n_candidates * math.e))
    return float((e_max + ppf(1 - alpha)) * se)


# ------------------------------------------------------------------------ driver

def positions_for(names, df, benchmark_close, flatten) -> dict[str, np.ndarray]:
    out = {}
    for name in names:
        try:
            if name == BASELINE_NAME:
                pos = pd.Series(1.0, index=df.index)
            else:
                kw = {}
                if name in NEEDS_BENCHMARK:
                    if benchmark_close is None:
                        continue
                    kw["benchmark_close"] = benchmark_close.reindex(df.index).ffill()
                pos = generate_position(name, df, **kw)
            pos = pd.Series(pos, index=df.index).fillna(0.0)
            if flatten and name != BASELINE_NAME:
                pos = flatten_eod(pos)
            out[name] = pos.to_numpy(dtype=np.float64)
        except Exception:
            continue
    return out


def _ret_of(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, float, np.ndarray]:
    close = df["Close"].to_numpy(float)
    ret = np.empty_like(close)
    ret[0] = 0.0
    ret[1:] = close[1:] / close[:-1] - 1.0
    return close, ret, bars_per_year(df.index), df.index.to_numpy()


def _blank(size: int) -> dict:
    return {"pnl": {}, "tr": [], "cagr": [], "sh": [], "dd": [], "expo": [],
            "tpnl": [], "thold": [], "n": 0, "size": size,
            "ir": [], "ir0": [], "ir1": [], "yrs": [],
            "grid": {c: [] for c in COST_BPS_GRID}}


def information_ratio(net: np.ndarray, base_net: np.ndarray, bpy: float) -> float:
    """Sharpe of the DIFFERENCE series — not the difference of two Sharpes.

    avg_excess_sharpe subtracts one Sharpe from another, which throws away the
    correlation between the strategy and its benchmark. The IR keeps it: a rule that
    tracks buy-&-hold closely and beats it by a hair has a small tracking error, so a
    modest edge still shows up as a large IR, while a rule that wanders far from the
    benchmark has to earn much more to score the same. That is the same correlation
    term that governs how detectable an edge is at all -
    SE(dSharpe) = SE(SR) * sqrt(2(1-rho)) - so ranking on IR ranks on the thing the
    sample can actually resolve.
    """
    d = net - base_net
    sd = d.std()
    return float(d.mean() / sd * math.sqrt(bpy)) if sd > 0 else np.nan


def _absorb(row: dict, ticker: str, pos: np.ndarray, close, ret, bpy, times,
            base_net: np.ndarray | None = None) -> None:
    net = net_returns(pos, ret, HEADLINE_COST_BPS)
    st = equity_stats(net, bpy)
    tr = extract_trades(pos, close, times, HEADLINE_COST_BPS)
    if base_net is not None:
        row["ir"].append(information_ratio(net, base_net, bpy))
        # IR at 0 and 1bps locates the breakeven cost: IR falls linearly in bps, so two
        # points fix the zero crossing. That level, against a realistic 2-5bps execution,
        # is the cost-headroom gate — the one every previous finding in this project died on.
        row["ir0"].append(information_ratio(net_returns(pos, ret, 0.0), base_net, bpy))
        row["ir1"].append(information_ratio(net_returns(pos, ret, 1.0), base_net, bpy))
        row["yrs"].append(len(ret) / bpy if bpy and bpy > 0 else np.nan)
    row["pnl"][ticker] = CAPITAL_PER_TICKER * float(st["total_return"])
    row["tr"].append(float(st["total_return"]))
    row["cagr"].append(float(st["cagr"]))
    row["sh"].append(float(st["sharpe"]))
    row["dd"].append(float(st["max_drawdown"]))
    row["expo"].append(float((pos != 0).mean()))
    row["tpnl"].append(tr["pnl_dollars"])
    row["thold"].append(tr["holding_days"])
    row["n"] += 1
    for c in COST_BPS_GRID:
        row["grid"][c].append(float(equity_stats(net_returns(pos, ret, c), bpy)["sharpe"]))


def run(timeframe: str) -> dict:
    """Three passes, so nothing large is ever held.

    Keeping every indicator's position array for every ticker would be
    231 x 20 x 70,000 floats at 5m — about 2.6 GB — so pass 1 discards positions as it
    goes, pass 2 recomputes only the TOP_N members it needs for combos, and pass 3
    recomputes only the handful of names that actually reach the report.
    """
    spec = TIMEFRAMES[timeframe]
    flat = spec["flatten_eod"]
    data = {t: d for t, d in td_loader.load(timeframe).items() if len(d) >= MIN_BARS}
    if not data:
        raise SystemExit(f"no cached data for {timeframe}")
    bench = td_loader.load(timeframe, ["SPY"]).get("SPY")
    bench_close = bench["Close"] if bench is not None else None

    singles = [n for n in get_all_indicator_names() if n != BASELINE_NAME]
    t0 = time.time()

    # ---- pass 1: singles ----------------------------------------------------
    acc: dict[str, dict] = {}
    base_net_of: dict[str, np.ndarray] = {}
    for ticker, df in tqdm(data.items(), desc=f"{timeframe} singles"):
        close, ret, bpy, times = _ret_of(df)
        # Buy-&-hold on THIS ticker is the benchmark every IR on this ticker is measured
        # against, so it has to be built before anything else absorbs.
        base_net_of[ticker] = net_returns(np.ones(len(df)), ret, HEADLINE_COST_BPS)
        for name, pos in positions_for(singles + [BASELINE_NAME], df, bench_close, flat).items():
            _absorb(acc.setdefault(name, _blank(1)), ticker, pos, close, ret, bpy, times,
                    base_net_of[ticker])
    print(f"  singles done in {time.time() - t0:.0f}s")

    lb = pd.DataFrame([summarise_row(n, a, 1) for n, a in acc.items()])
    base_sh = float(lb.loc[lb.indicator == BASELINE_NAME, "avg_sharpe"].iloc[0])
    base_cagr = float(lb.loc[lb.indicator == BASELINE_NAME, "avg_cagr"].iloc[0])
    lb["avg_excess_sharpe"] = lb["avg_sharpe"] - base_sh
    lb["avg_excess_cagr"] = lb["avg_cagr"] - base_cagr

    pool = lb[(lb.indicator != BASELINE_NAME) & lb.rankable]
    top = pool.sort_values(SELECT_METRIC, ascending=False).head(TOP_N)["indicator"].tolist()
    print(f"  top {len(top)} by {SELECT_METRIC}: {', '.join(top[:6])} ...")

    # ---- pass 2: combos -----------------------------------------------------
    combos = [c for s in COMBO_SIZES for c in itertools.combinations(top, s)]
    names_c = ["+".join(c) for c in combos]
    idx_of = {n: i for i, n in enumerate(top)}
    M = np.zeros((len(combos), len(top)))
    sizes = np.array([len(c) for c in combos])
    for i, c in enumerate(combos):
        for m in c:
            M[i, idx_of[m]] = 1.0

    cacc = {n: _blank(int(sizes[i])) for i, n in enumerate(names_c)}
    for ticker, df in tqdm(data.items(), desc=f"{timeframe} combos"):
        close, ret, bpy, times = _ret_of(df)
        pm = positions_for(top, df, bench_close, flat)
        P = np.vstack([pm[n] for n in top])
        L, S = (P == 1).astype(float), (P == -1).astype(float)
        del pm, P
        for s0 in range(0, len(combos), 100):
            sl = slice(s0, min(s0 + 100, len(combos)))
            cp = majority(M[sl] @ L, M[sl] @ S, sizes[sl][:, None])
            for j in range(cp.shape[0]):
                _absorb(cacc[names_c[s0 + j]], ticker, cp[j], close, ret, bpy, times,
                        base_net_of[ticker])
            del cp
    print(f"  combos done in {time.time() - t0:.0f}s")

    full = pd.concat([lb, pd.DataFrame([summarise_row(n, a, a["size"])
                                        for n, a in cacc.items()])], ignore_index=True)
    full["avg_excess_sharpe"] = full["avg_sharpe"] - base_sh
    full["avg_excess_cagr"] = full["avg_cagr"] - base_cagr
    return {"leaderboard": full, "data": data, "top": top, "bench_close": bench_close,
            "flat": flat, "base_sharpe": base_sh, "timeframe": timeframe,
            "n_candidates": int(len(singles) + len(combos))}


def summarise_row(name: str, a: dict, size: int) -> dict:
    pnl = np.array(list(a["pnl"].values()))
    tp = np.concatenate(a["tpnl"]) if a["tpnl"] else np.array([])
    th = np.concatenate(a["thold"]) if a["thold"] else np.array([])
    sh = np.array(a["sh"], dtype=float)
    ir = np.array(a["ir"], dtype=float) if a["ir"] else np.array([np.nan])
    ir_ok = ir[np.isfinite(ir)]
    avg_ir = float(np.nanmean(ir)) if ir_ok.size else np.nan
    g0 = float(np.nanmean(a["ir0"])) if a["ir0"] else np.nan
    g1 = float(np.nanmean(a["ir1"])) if a["ir1"] else np.nan
    yrs = float(np.nanmean(a["yrs"])) if a["yrs"] else np.nan
    slope = g0 - g1
    be = (g0 / slope) if (np.isfinite(g0) and g0 > 0 and slope > 1e-12) else 0.0
    out = {"indicator": name, "combo_size": size, "n_tickers": a["n"],
           "avg_ir": avg_ir,
           # t = IR * sqrt(years) is the whole significance story: on an 8-year sample
           # sqrt(8)=2.83, so even a good IR of 0.5 only reaches t=1.41.
           "ir_t": float(avg_ir * math.sqrt(yrs)) if np.isfinite(avg_ir) and yrs > 0 else np.nan,
           "breakeven_bps": float(min(be, 9999.0)),
           # Breadth guard. final_check.py showed a book with Sharpe 0.889 collapsing to
           # 0.165 once two names were removed — a strong average IR carried by a couple
           # of tickers is the failure mode this column is here to expose.
           "ir_hit_rate": float((ir_ok > 0).mean()) if ir_ok.size else np.nan,
           "total_pnl_dollars": float(pnl.sum()),
           "avg_total_return": float(np.nanmean(a["tr"])),
           "median_total_return": float(np.nanmedian(a["tr"])),
           "avg_cagr": float(np.nanmean(a["cagr"])),
           "avg_sharpe": float(np.nanmean(sh)),
           "avg_max_drawdown": float(np.nanmean(a["dd"])),
           "avg_exposure": float(np.nanmean(a["expo"])),
           "stock_win_rate": float(np.mean(np.array(a["tr"]) > 0)),
           "n_sharpe": int(np.isfinite(sh).sum()),
           "rankable": bool(np.isfinite(sh).sum() >= MIN_SHARPE_COVERAGE * a["n"]),
           "generic_fallback": any(m in GENERIC_FALLBACK_FUNCTIONS for m in name.split("+")),
           "is_baseline": name == BASELINE_NAME}
    out.update(trade_aggregate(tp, th))
    for c in COST_BPS_GRID:
        out[f"sharpe_{c:g}bps"] = float(np.nanmean(a["grid"][c]))
    return out


if __name__ == "__main__":
    req = [a for a in sys.argv[1:] if a in TIMEFRAMES] or ["1d", "1h", "5m"]
    import build_combo_report
    import os

    # Merge, don't clobber: running one timeframe must not silently drop the tabs the
    # report already has — the same trap as results/summary_all.csv holding only the last
    # invocation's timeframes.
    payload = {}
    if os.path.exists(OUT_JSON):
        with open(OUT_JSON, encoding="utf-8") as f:
            payload = json.load(f)
        print(f"merging into existing {OUT_JSON}: {', '.join(payload)}")

    label = {"1d": "1D", "1h": "1H", "5m": "5m"}
    for tf in req:
        res = run(tf)
        payload[label[tf]] = build_combo_report.build_payload(res)

    order = [k for k in ("1D", "1H", "5m") if k in payload]
    payload = {k: payload[k] for k in order}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), allow_nan=False)
    print(f"wrote {OUT_JSON} ({', '.join(payload)})")
