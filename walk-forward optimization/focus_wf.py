"""Stage 1f: the two survivors of stage 1e, examined properly.

`strat_wf.py` scored 25 published strategies and none cleared the gates. Two of them
were nonetheless different in kind from the rest, because their consistency was across
**assets** rather than selected per asset:

    ibs         best rule on 11 of 20 US stocks, and +0.334 above its exposure-matched
                control on the only statistically coherent sheet
    macd_cross  beats buy-and-hold on 7 of 10 crypto pairs by compounded return,
                +16.8 points a year on average

Everything else on that leaderboard was either worse than a coin flip at its own
exposure, or looked good on one name because it was chosen after looking. These two were
not, so they get the follow-up. Treating them as a pre-committed pair collapses the
multiple-testing correction: `metrics.noise_ceiling(2, 41)` is **+0.067**, against
+0.360 for the 93 cells stage 1e searched.

Four tests, each answering something the sheet-level tables structurally cannot:

**1. The conditional-return test, which is immune to time-in-market.** Every other number
in this project is confounded by exposure — `corr(IR, long_frac) = 0.881` — so a rule
that is flat half the time is punished for that alone. This asks a question exposure
cannot reach: on the bars the rule says hold, is the *next* bar's return larger than on
the bars it says stay out? That is signal or it is nothing, and no amount of being out of
a rising market can fake it. Reported as a per-bar spread in basis points, with the
t-statistic taken **across calendar years, not across bars or assets** — 11,500 daily
bars are not 11,500 independent observations, and 20 mega-caps in the same month are
close to one observation, so pooling either way manufactures significance.

**2. Era stability.** `ibs` has 41 out-of-sample years, which is the single most valuable
asset in this repo, and it is the only strategy here with enough history to ask whether
its edge *decayed*. Short-term mean reversion in US equities is widely reported to have
faded with decimalisation and the rise of electronic market making. If the conditional
spread is large before 2000 and near zero after 2010, `ibs` is a historical fact rather
than a tradeable one, and a 41-year average would be hiding exactly that.

**3. The cost curve.** `ibs` turns over ~58 times a year. A mean-reversion rule holding
for a day or two is the shape most exposed to fees, so the honest question is not "does
it survive the retail schedule" but "how many multiples of it does it survive", and where
the crossing is.

**4. Risk decomposition for `macd_cross`.** It wins on compounded return and loses on
information ratio, which is the SOXL pattern: arithmetic mean down, variance down more.
Crypto buy-and-hold carries 80-90% drawdowns, so the drag avoided can exceed the return
given up. Reported as max drawdown, MAR and the explicit arithmetic-versus-geometric
split, because on that sheet the two metrics genuinely disagree and picking one silently
would decide the answer.

Run::

    python focus_wf.py                       # everything
    python focus_wf.py --tf 1d               # daily only
"""

from __future__ import annotations

import argparse
import time

import numpy as np
import pandas as pd

from wfo_paths import RESULTS_DIR           # noqa: F401  (wires sys.path first)
from config import CLASSES, HEADLINE_SCENARIO, MIN_BARS, WF_MIN_FOLDS, scenario
from engines import vector
import metrics
import td_loader
import walkforward as wfmod

from stockhunt import stats

from strategies.catalog import CATALOG, build

# The whole point of this stage is that these two were named before the run, not picked
# out of it. Adding a third here silently raises the ceiling for the other two.
FOCUS = ("ibs", "macd_cross")

# Where each one earned its place, and therefore where its result counts as a follow-up
# rather than a fresh search on a new sheet.
HOME = {"ibs": "us_stocks", "macd_cross": "crypto"}

FREE = {"key": "gross", "commission_bps": 0.0, "half_spread_bps": 0.0,
        "sell_fee_bps": 0.0, "borrow_annual": 0.0}

COST_MULTIPLES = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0)
ERA_EDGES = (1970, 1980, 1990, 2000, 2010, 2020, 2030)


def scaled_fee(base: dict, k: float) -> dict:
    """The real fee schedule, every component multiplied by `k`.

    Scaling the whole schedule rather than only commission keeps the *shape* of what a
    trader pays — spread, regulatory fees and borrow all move together when execution
    gets worse — so `k = 3` reads as "three times the real cost of doing this", which is
    exactly what the headroom gate is stated in.
    """
    out = dict(base)
    for f in ("commission_bps", "half_spread_bps", "sell_fee_bps", "borrow_annual"):
        out[f] = base.get(f, 0.0) * k
    out["key"] = f"{k:g}x"
    return out


# Masked wrappers over the one implementation in `stockhunt.stats`. The `** (1/years)`
# spelling this module used and the `** (bpy/n)` one `riskmatch_wf` used are the same
# number in exact arithmetic, since `years = n / bpy` — but not quite in float64: the
# first divides twice and so carries one extra rounding. The canonical single-division
# form is kept, so this function's output can move in the last mantissa bit, worst case
# **9.75e-16 relative** as measured by `tools/test_stats_equivalence.py`. Nothing this
# stage prints has more than three significant figures, so no reported digit can change.
# It is a real difference and it is recorded rather than waved through.
def cagr(net: np.ndarray, mask: np.ndarray, bpy: float) -> float:
    return stats.cagr(net[mask], bpy)


def drawdown(net: np.ndarray, mask: np.ndarray) -> float:
    # `dropna=True`: this module filtered before compounding, and it was the one that had
    # it right — a single non-finite bar otherwise poisons the whole cumulative product.
    return stats.max_drawdown(net[mask], dropna=True)


def conditional_spread(pos: np.ndarray, close: np.ndarray, mask: np.ndarray,
                       index: pd.DatetimeIndex) -> dict:
    """Next-bar return when the rule holds, minus when it does not. Exposure-free.

    `pos` is the signal at bar t; the engine trades it at t+1, so the return that signal
    earns is `ret[t+1]`. Aligning them the same way here is what makes this comparable to
    the backtest rather than a different, luckier question.

    The t-statistic is computed on the **yearly** series of spreads. Per-bar t would
    treat 11,500 correlated daily observations as independent and inflate it by roughly
    the square root of that; per-asset t would do the same across 20 names that move
    together. Years are the coarsest unit that still leaves a usable sample, and it is
    the conservative choice.
    """
    ret = np.empty_like(close)
    ret[0] = 0.0
    ret[1:] = close[1:] / close[:-1] - 1.0

    held = np.empty_like(pos)
    held[0] = 0.0
    held[1:] = pos[:-1]

    ok = mask & np.isfinite(ret)
    long_m, flat_m = ok & (held > 0), ok & (held == 0)
    if long_m.sum() < 30 or flat_m.sum() < 30:
        return {"spread_bps": np.nan, "t_years": np.nan, "n_years": 0,
                "years_positive": np.nan, "in_bps": np.nan, "out_bps": np.nan}

    years = index.year.to_numpy()
    per_year = []
    for y in np.unique(years[ok]):
        a = ret[long_m & (years == y)]
        b = ret[flat_m & (years == y)]
        if a.size >= 5 and b.size >= 5:
            per_year.append((a.mean() - b.mean()) * 1e4)
    per_year = np.array(per_year)
    t = (float(per_year.mean() / (per_year.std(ddof=1) / np.sqrt(per_year.size)))
         if per_year.size > 2 and per_year.std(ddof=1) > 0 else np.nan)
    return {
        "spread_bps": float(ret[long_m].mean() - ret[flat_m].mean()) * 1e4,
        "in_bps": float(ret[long_m].mean()) * 1e4,
        "out_bps": float(ret[flat_m].mean()) * 1e4,
        "t_years": t, "n_years": int(per_year.size),
        "years_positive": float(np.mean(per_year > 0)) if per_year.size else np.nan,
    }


def load_sheet(asset_class: str, timeframe: str):
    data = td_loader.load(asset_class, timeframe)
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
    if not data:
        return None
    start = min(df.index[0] for df in data.values())
    end = max(df.index[-1] for df in data.values())
    folds = wfmod.generate_folds(start, end)
    if len(folds) < WF_MIN_FOLDS:
        return None
    masks = {s: wfmod.fold_masks(df.index, folds) for s, df in data.items()}
    union = {s: np.logical_or.reduce([m[1] for m in ms if m is not None])
             for s, ms in masks.items() if any(m is not None for m in ms)}
    return data, folds, union


def run_sheet(asset_class: str, timeframe: str) -> tuple[pd.DataFrame, ...]:
    got = load_sheet(asset_class, timeframe)
    if got is None:
        return None
    data, folds, union = got
    fee = scenario(asset_class, HEADLINE_SCENARIO[asset_class])

    per_asset, eras, costs = [], [], []
    for name in FOCUS:
        for symbol, df in data.items():
            if symbol not in union:
                continue
            close = df["Close"].to_numpy("float64")
            bpy = vector.bars_per_year(df.index)
            pos = build(name, df, close, bpy, symbol)
            if pos is None:
                continue
            u = union[symbol]
            bench = vector.net_returns(np.ones(len(df)), close, FREE, bpy)
            net = vector.net_returns(pos, close, fee, bpy)
            gross = vector.net_returns(pos, close, FREE, bpy)
            cond = conditional_spread(pos, close, u, df.index)

            s_cagr, b_cagr = cagr(net, u, bpy), cagr(bench, u, bpy)
            per_asset.append({
                "class": asset_class, "tf": timeframe, "rule": name, "symbol": symbol,
                "ir": metrics.information_ratio(net[u], bench[u], bpy),
                "ir_gross": metrics.information_ratio(gross[u], bench[u], bpy),
                "cagr": s_cagr, "bench_cagr": b_cagr, "excess_cagr": s_cagr - b_cagr,
                "max_dd": drawdown(net, u), "bench_dd": drawdown(bench, u),
                "mar": s_cagr / abs(drawdown(net, u)) if drawdown(net, u) else np.nan,
                "bench_mar": (b_cagr / abs(drawdown(bench, u))
                              if drawdown(bench, u) else np.nan),
                "vol": float(np.std(net[u], ddof=1) * np.sqrt(bpy)),
                "bench_vol": float(np.std(bench[u], ddof=1) * np.sqrt(bpy)),
                "long_frac": float(np.mean(pos[u] > 0)),
                "turnover_yr": float(np.abs(np.diff(pos[u])).sum() / (u.sum() / bpy)),
                "years": float(u.sum() / bpy), **cond,
            })

            # Era split: same rule, same asset, one decade at a time.
            yrs = df.index.year.to_numpy()
            for lo, hi in zip(ERA_EDGES, ERA_EDGES[1:]):
                m = u & (yrs >= lo) & (yrs < hi)
                if m.sum() < 250:
                    continue
                c = conditional_spread(pos, close, m, df.index)
                eras.append({
                    "class": asset_class, "tf": timeframe, "rule": name,
                    "symbol": symbol, "era": f"{lo}s", "bars": int(m.sum()),
                    "ir": metrics.information_ratio(net[m], bench[m], bpy),
                    "excess_cagr": cagr(net, m, bpy) - cagr(bench, m, bpy),
                    "spread_bps": c["spread_bps"],
                })

            for k in COST_MULTIPLES:
                n_k = vector.net_returns(pos, close, scaled_fee(fee, k), bpy)
                costs.append({
                    "class": asset_class, "tf": timeframe, "rule": name,
                    "symbol": symbol, "mult": k,
                    "excess_cagr": cagr(n_k, u, bpy) - b_cagr,
                    "ir": metrics.information_ratio(n_k[u], bench[u], bpy),
                })
    return (pd.DataFrame(per_asset), pd.DataFrame(eras), pd.DataFrame(costs))


def tilt_and_regime(asset_class: str, timeframe: str, name: str
                    ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Two follow-ups the conditional-return result forces.

    **The floor sweep.** `ibs` picks better bars than average — +16.8bps against +3.4bps
    on the bars it skips, on 20 of 20 names — and still loses to buy-and-hold at *zero*
    cost. That is not a contradiction and not a cost problem: the bars it skips still
    earn +3.4bps, and forgoing 54% of them costs more than the selectivity is worth. So
    the rule's weakness is the cash leg, not the signal. Replace cash with a floor
    weight `w` and the question becomes whether selectivity is worth anything once the
    risk premium is no longer being thrown away. `w=0` is the published rule, `w=1` is
    buy-and-hold, and nothing exceeds 1.0 — a levered version would buy its result with
    borrowed money and is not what these gates are stated for.

    Arithmetically no floor can win, because the skipped bars have positive expected
    return and any weight below 1.0 gives some of it up. Geometrically it can, if the
    skipped bars are also the volatile ones: `ibs` runs 24.2% annualised volatility
    against buy-and-hold's 33.8%. That is the same arithmetic-versus-geometric split
    that makes `ibs` on SOXL compound 25 points a year ahead while its IR is negative.

    **The regime panel.** Per (asset, calendar year), the benchmark's return against the
    strategy's excess return. If a rule only wins when the market is falling it is a
    defensive overlay whose value depends on a forecast nobody has, not an alpha — and
    the era table already hints at exactly that.
    """
    got = load_sheet(asset_class, timeframe)
    if got is None:
        return pd.DataFrame(), pd.DataFrame()
    data, _folds, union = got
    fee = scenario(asset_class, HEADLINE_SCENARIO[asset_class])

    tilts, panel = [], []
    for symbol, df in data.items():
        if symbol not in union:
            continue
        close = df["Close"].to_numpy("float64")
        bpy = vector.bars_per_year(df.index)
        pos = build(name, df, close, bpy, symbol)
        if pos is None:
            continue
        u = union[symbol]
        bench = vector.net_returns(np.ones(len(df)), close, FREE, bpy)
        b_cagr = cagr(bench, u, bpy)

        long_pos = np.clip(pos, 0.0, 1.0)
        exposure = float(np.mean(long_pos[u]))
        variants = [(w, w + (1.0 - w) * long_pos, "floor") for w in
                    (0.0, 0.25, 0.5, 0.75, 1.0)]
        # The control that settles it. A constant weight equal to the rule's own average
        # exposure holds the same amount of the asset, all the time, with no signal and
        # essentially no turnover. If the rule cannot beat *that*, its lower drawdown is
        # not being bought by timing — it is being bought by simply owning less, which
        # anyone can do without a strategy. `RANDOM_50` in stage 1e randomises the
        # timing at matched exposure; this removes timing altogether.
        variants.append((exposure, np.full(len(df), exposure), "const"))
        for w, p, mode in variants:
            net = vector.net_returns(p, close, fee, bpy)
            c = cagr(net, u, bpy)
            dd = drawdown(net, u)
            tilts.append({
                "class": asset_class, "tf": timeframe, "rule": name, "symbol": symbol,
                "floor": w, "mode": mode, "cagr": c, "excess_cagr": c - b_cagr,
                "ir": metrics.information_ratio(net[u], bench[u], bpy),
                "vol": float(np.std(net[u], ddof=1) * np.sqrt(bpy)),
                "max_dd": dd, "mar": c / abs(dd) if dd else np.nan,
                "turnover_yr": float(np.abs(np.diff(p[u])).sum() / (u.sum() / bpy)),
                # Mean WEIGHT, not the fraction of bars with a non-zero position. Once a
                # floor is applied the position is never zero, so a hit-rate reads 100%
                # for every variant and hides the very thing being swept.
                "weight": float(np.mean(p[u])),
            })

        net0 = vector.net_returns(pos, close, fee, bpy)
        years = df.index.year.to_numpy()
        for y in np.unique(years[u]):
            m = u & (years == y)
            if m.sum() < 100:
                continue
            panel.append({
                "class": asset_class, "tf": timeframe, "rule": name, "symbol": symbol,
                "year": int(y),
                "bench_ret": float(np.prod(1.0 + bench[m]) - 1.0),
                "excess": float(np.prod(1.0 + net0[m]) - np.prod(1.0 + bench[m])),
            })
    return pd.DataFrame(tilts), pd.DataFrame(panel)


def report_tilt(tilts: pd.DataFrame, panel: pd.DataFrame, name: str,
                asset_class: str) -> None:
    t = tilts[tilts.rule == name]
    if t.empty:
        return
    print(f"\n  floor sweep on {asset_class} 1d "
          f"— what if it held the asset instead of cash?")
    print(f"    {'variant':<22}{'exposure':>9}{'CAGR':>9}{'vs B&H':>9}{'IR':>8}"
          f"{'vol':>8}{'maxDD':>8}{'MAR':>7}{'turns/yr':>10}")
    fl = t[t["mode"] == "floor"]
    rows = [(f"floor {w:.2f}", g) for w, g in fl.groupby("floor")]
    rows.append(("constant weight", t[t["mode"] == "const"]))
    for label, g in rows:
        w = g.floor.iloc[0] if len(g) else np.nan
        tag = ("  <- published" if label == "floor 0.00"
               else "  <- buy & hold" if label == "floor 1.00"
               else "  <- NO SIGNAL, same exposure" if label == "constant weight" else "")
        print(f"    {label:<22}{g.weight.mean():>9.0%}{g.cagr.mean():>9.2%}"
              f"{g.excess_cagr.mean():>+9.2%}{g.ir.mean():>+8.3f}{g.vol.mean():>8.1%}"
              f"{g.max_dd.mean():>8.1%}{g.mar.mean():>7.2f}"
              f"{g.turnover_yr.mean():>10.0f}{tag}")
    pub = fl[fl.floor == 0.0]
    con = t[t["mode"] == "const"]
    # IR is identical for every floor and that is arithmetic, not a bug: with
    # p = w + (1-w)q the difference series against an always-long benchmark is exactly
    # (1-w) times the published rule's, and a ratio is invariant to positive scaling. So
    # IR cannot rank these variants at all, while CAGR and MAR separate them clearly.
    print(f"    -> IR is identical across floors by construction "
          f"(the difference series is scaled, and a ratio does not see scale); "
          f"read CAGR and MAR instead")
    print(f"    -> published MAR {pub.mar.mean():.2f} vs constant-weight "
          f"{con.mar.mean():.2f} at the same {con.weight.mean():.0%} exposure "
          f"({'timing adds' if pub.mar.mean() > con.mar.mean() else 'timing SUBTRACTS'} "
          f"{pub.mar.mean() - con.mar.mean():+.2f} MAR)")

    p = panel[panel.rule == name]
    if len(p) > 20:
        lo = p[p.bench_ret < p.bench_ret.quantile(0.25)]
        hi = p[p.bench_ret > p.bench_ret.quantile(0.75)]
        print(f"\n  is it defensive? ({len(p)} asset-years)")
        print(f"    correlation(benchmark year return, strategy excess) = "
              f"{p.bench_ret.corr(p.excess):+.3f}")
        print(f"    worst quartile of market years  (mean B&H {lo.bench_ret.mean():+.1%})"
              f":  excess {lo.excess.mean():+.2%}  "
              f"({float((lo.excess > 0).mean()):.0%} of years positive)")
        print(f"    best quartile of market years   (mean B&H {hi.bench_ret.mean():+.1%})"
              f":  excess {hi.excess.mean():+.2%}  "
              f"({float((hi.excess > 0).mean()):.0%} of years positive)")


def breakeven(costs: pd.DataFrame) -> float:
    """Fee multiple at which mean excess CAGR crosses zero, linearly interpolated."""
    g = costs.groupby("mult")["excess_cagr"].mean().sort_index()
    if g.empty or g.iloc[0] <= 0:
        return 0.0
    below = g[g <= 0]
    if below.empty:
        return float("inf")
    hi = below.index[0]
    lo = g.index[g.index < hi].max()
    y0, y1 = g.loc[lo], g.loc[hi]
    return float(lo + (hi - lo) * y0 / (y0 - y1))


def report(per_asset, eras, costs) -> None:
    for name in FOCUS:
        pa = per_asset[per_asset.rule == name]
        if pa.empty:
            continue
        print(f"\n{'=' * 100}\n{name.upper()}   (home sheet: {HOME[name]})\n{'=' * 100}")
        for (cls_, tf), g in pa.groupby(["class", "tf"]):
            home = " <- home" if cls_ == HOME[name] else ""
            yrs = g["years"].median()
            ceil2 = metrics.noise_ceiling(2, yrs)
            print(f"\n  {cls_} {tf}  ({len(g)} assets, {yrs:.1f}y OOS, "
                  f"ceiling at N=2 is {ceil2:+.3f}){home}")
            print(f"    IR              {g.ir.mean():+.3f}   "
                  f"(gross {g.ir_gross.mean():+.3f}, "
                  f"{int((g.ir > 0).sum())}/{len(g)} assets positive)")
            print(f"    excess CAGR     {g.excess_cagr.mean():+.2%}   "
                  f"worst {g.excess_cagr.min():+.2%}, "
                  f"{int((g.excess_cagr > 0).sum())}/{len(g)} assets positive")
            print(f"    conditional     {g.spread_bps.mean():+.1f} bps/bar  "
                  f"(in {g.in_bps.mean():+.1f} vs out {g.out_bps.mean():+.1f})  "
                  f"t={g.t_years.mean():+.2f} across years, "
                  f"{int((g.spread_bps > 0).sum())}/{len(g)} assets positive")
            print(f"    risk            vol {g.vol.mean():.1%} vs {g.bench_vol.mean():.1%}"
                  f"   maxDD {g.max_dd.mean():.1%} vs {g.bench_dd.mean():.1%}"
                  f"   MAR {g.mar.mean():.2f} vs {g.bench_mar.mean():.2f}")
            c = costs[(costs.rule == name) & (costs["class"] == cls_) & (costs.tf == tf)]
            be = breakeven(c)
            gross_x = c[c.mult == 0].excess_cagr.mean()
            print(f"    cost            gross excess CAGR {gross_x:+.2%}, "
                  f"breakeven at {be:.2f}x the real fee schedule "
                  f"(gate wants 3x) | {g.turnover_yr.mean():.0f} turns/yr, "
                  f"long {g.long_frac.mean():.0%}")

        e = eras[(eras.rule == name) & (eras["class"] == HOME[name]) & (eras.tf == "1d")]
        if not e.empty:
            print(f"\n  era decomposition on {HOME[name]} 1d "
                  f"(the reason this sheet is worth having):")
            agg = e.groupby("era").agg(
                assets=("symbol", "nunique"), ir=("ir", "mean"),
                xcagr=("excess_cagr", "mean"), spread=("spread_bps", "mean"),
                pos=("spread_bps", lambda x: float((x > 0).mean())))
            for era, r in agg.iterrows():
                print(f"    {era}   {int(r.assets):>2} assets   IR {r.ir:+.3f}   "
                      f"excess CAGR {r.xcagr:+6.2%}   conditional {r.spread:+7.1f} bps"
                      f"   ({r.pos:.0%} of assets positive)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+", default=["1d", "4h"])
    args = ap.parse_args()

    t0 = time.time()
    pa, er, co = [], [], []
    for asset_class in args.classes:
        for timeframe in args.timeframes:
            got = run_sheet(asset_class, timeframe)
            if got is None:
                print(f"{asset_class}/{timeframe}: no cached data or too few folds")
                continue
            pa.append(got[0])
            er.append(got[1])
            co.append(got[2])
    if not pa:
        return
    per_asset = pd.concat(pa, ignore_index=True)
    eras = pd.concat([e for e in er if not e.empty], ignore_index=True)
    costs = pd.concat(co, ignore_index=True)

    report(per_asset, eras, costs)

    # The follow-ups run on each rule's HOME sheet only. Running them everywhere would
    # be a fresh search across sheets, which is the thing this stage exists to avoid.
    tilts, panels = [], []
    for name in FOCUS:
        home = HOME[name]
        if home not in args.classes or "1d" not in args.timeframes:
            continue
        ti, pn = tilt_and_regime(home, "1d", name)
        if ti.empty:
            continue
        print(f"\n{'=' * 100}\n{name.upper()} — follow-ups on {home} 1d\n{'=' * 100}")
        report_tilt(ti, pn, name, home)
        tilts.append(ti)
        panels.append(pn)

    per_asset.to_csv(RESULTS_DIR / "focus_per_asset.csv", index=False)
    eras.to_csv(RESULTS_DIR / "focus_eras.csv", index=False)
    costs.to_csv(RESULTS_DIR / "focus_costs.csv", index=False)
    if tilts:
        pd.concat(tilts, ignore_index=True).to_csv(
            RESULTS_DIR / "focus_tilt.csv", index=False)
        pd.concat(panels, ignore_index=True).to_csv(
            RESULTS_DIR / "focus_regime.csv", index=False)
    print(f"\nwrote focus_*.csv to {RESULTS_DIR}  ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
