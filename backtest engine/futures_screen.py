"""Which CME contracts are worth carrying, and which are the same bet twice.

`universe_screen.py` is to `us_etfs` and `crypto` what this is to `cme_futures`: it turns
a pool of plausible symbols into the traded universe, and writes down why each rejection
happened so the decision survives the person who made it.

The gates are the same three questions the ETF screen asks, plus one this class forces:

**Can it be traded?** Median daily notional turnover, computed properly — a futures
volume is a contract count, and 1.4M ZN contracts is a different amount of money from
141k GC contracts by a factor no ranking on raw volume can see. `futures_specs` supplies
the multiplier.

**Is there enough of it?** Tradable years, measured on bars actually present. Nothing
here can reach further back than 2010-06-06 (`futures_specs.GLBX_START`), so the gate is
set against that ceiling rather than the 20 years the ETF screen can demand.

**Is the price a market or a grid?** The exchange's tick as a fraction of a typical day's
range. A rule that mean-reverts on a coarse grid is harvesting the rounding, which is
exactly how a recycled penny stock compounded to 6.4e17% in this repo before `check_data`
learned to quarantine it.

**And: is it a different asset, or the same one wearing a different symbol?** This is the
gate the equity classes never needed and this class cannot do without. Six points on the
Treasury curve are not six assets; ES and YM are the same index to two decimal places.
`metrics.se_ir` assumes its assets are independent and has no way to notice that they are
not, so a universe of correlated roots quietly claims statistical weight it does not
have. The ETF screen learned this the expensive way — ten funds picked on listing date
came out at mean pairwise correlation 0.72, and picking on tradable years instead got it
to 0.44 and roughly doubled the real weight of the same ten names.

So the last gate is **greedy, in liquidity order**: take the deepest contract first, then
accept a candidate only if its return correlation against everything already accepted
stays under `MAX_CORR`. That keeps the most tradable member of each cluster and drops the
echoes, and because the order is liquidity there is nothing arbitrary in which survives.

**`ALWAYS_KEEP` overrides that last gate and the history one, by name.** Some contracts
are wanted in the book for reasons a screen cannot see — a forward test on a specific
index, say — and the honest way to carry them is an explicit list that records what it
cost, not a loosened threshold that quietly changes every other verdict too. It cannot
override liquidity, the price grid or the volatility floor: those decide whether an
instrument can be filled, and a book holding something it cannot fill is a different
and much worse mistake than a book holding two things that move together.

Run::

    python futures_screen.py                  # the table, and what it would keep
    python futures_screen.py --write          # ...and commit it to universes_futures.py
    python futures_screen.py --tf 1d --max-corr 0.80
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import config
import futures_specs
import td_loader

# Every threshold that decides a name, in one place.
#
# `MIN_ADV_USD` at $1B/day sounds enormous next to the ETF screen's $20M, and it is: the
# smallest thing here still turns over more than the largest thing there. Futures are
# where the institutional flow is, and a $1B floor is roughly the point below which a CME
# product stops quoting one tick wide all session.
MIN_ADV_USD = 1_000_000_000.0
# 12 years, against a hard ceiling of ~16.2. The ETF screen asks for 20 tradable years;
# it can, because ETFs go back to 1993. Asking for 20 here would empty the class.
MIN_TRADABLE_YEARS = 12.0
# One tick may not be more than this share of a typical session's range — so at 5%, a
# day's move is at least twenty ticks wide.
#
# The number to reason from is what it does to `IBS`, which is `(C-L)/(H-L)` and is the
# most grid-sensitive statistic in the repo. With N ticks in a typical range, IBS can only
# take about N values; below roughly twenty it stops describing where the close sat and
# starts describing the rounding. That is the failure a recycled penny stock's 1-cent grid
# produced, and it is the thing this gate exists to catch.
#
# **It was 2% first, and 2% was wrong.** Fifty ticks per day's range is more resolution
# than most real instruments have, and it rejected the ENTIRE rates sector — ZN at 3.4%,
# ZF 2.8%, ZT 4.8%, ZB 3.4%, TN 2.6%, UB 2.5%. A 10-year Treasury future ticks in 1/64ths
# on a ~43bp daily range, which is about thirty ticks: coarse next to an equity index, and
# nowhere near a grid. Losing the whole sector would also have cost the most independent
# block in the universe, since rates correlate ~0.1-0.3 with everything else here — the
# exact quantity the correlation gate below is trying to buy.
#
# At 5% the gate still fires, and on the right name: SR3 at 11.1%, whose median daily
# range is 0.047% and therefore about nine ticks wide.
MAX_TICK_OF_RANGE = 0.05
# Annualised volatility floor. Below this the contract is not worth a slot.
#
# **State the counter-argument first, because it is strong.** Volatility is not
# opportunity, and this project already neutralises it: `riskmatch_wf` sizes every rule to
# the benchmark's risk and `portfolio_wf` scores the book, so a 5%-vol Treasury note is
# simply held in larger size. Futures are margin instruments — leverage is free here in a
# way it is not for cash equities — so "too slow" is usually a sizing question, not a
# selection one. Trend-following has historically made much of its money in exactly the
# rates and FX contracts this gate removes.
#
# What survives that argument, and is the actual reason for the floor: **cost and the tick
# grid do not scale down with volatility.** A round trip costs the same fraction of
# notional on ZT as on CL, but ZT's median daily range is 0.08% against CL's 2.73% — so
# the same fee eats ~34x more of the available move, and ZT's tick alone is 4.8% of a
# typical day where CL's is 0.4%. A rule on ZT is trading a handful of ticks, which is
# where `MAX_TICK_OF_RANGE` and this floor are really the same objection measured twice.
#
# 15% was chosen against the measured distribution, not picked round. The class splits
# cleanly: two rates and eight FX sit between 1.3% and 11.9%, then there is a gap to LE at
# 15.8% and everything else above. And it is nearly free — mean pairwise correlation among
# the kept names stays at **0.20** whether the floor is 0% or 15%, because the FX block
# was correlated with itself rather than adding independence. Above 25% it does start to
# cost: 7 names, 3 sectors, correlation 0.25.
MIN_ANN_VOL_PCT = 15.0
# Two roots correlating above this are one asset. 0.85 is deliberately looser than the
# 0.72 the ETF screen ended up at, because the sectors here are genuinely distinct and a
# tighter bar would start cutting real diversification (GC and SI run ~0.8 and are not
# the same trade).
MAX_CORR = 0.85

# Roots carried WHATEVER the correlation and history gates say (2026-08-28, by request).
#
# This is an override, not a re-tuning, and it is written as a named list rather than as a
# looser `MAX_CORR` on purpose: raising the bar to 0.96 would have admitted `EMD` too and
# quietly reshuffled the class, where this admits exactly the four contracts that were
# asked for and leaves every other verdict on the sheet untouched.
#
# `ES` is not here — it passes on merit and leads the liquidity ranking. The other three
# each fail exactly one gate, and the CSV keeps saying which:
#
#   NQ   corr +0.93 with ES        RTY  only 9.1 tradable years (needs 12)
#   YM   corr +0.95 with ES
#
# **What it costs, stated where it is done rather than in a commit message.** The
# correlation gate exists because `metrics.se_ir` assumes its assets are independent and
# has no way to notice that they are not. Four US equity index contracts are close to one
# bet, so a breadth figure or a `t` on this class now counts that bet up to four times and
# is optimistic by roughly the amount that redundancy is worth. Read `n_assets` on this
# class as "contracts", never as "independent bets", and prefer the book-level numbers
# (`portfolio_wf`), which hold the four in one account and therefore price the redundancy
# instead of assuming it away.
#
# RTY brings a second, smaller cost: it starts 2017-07-10, so it is ~9 years against the
# class's ~16, and any statistic pooled across names is now pooled over unequal spans.
ALWAYS_KEEP = ["NQ.v.0", "YM.v.0", "RTY.v.0"]

# ...but only the two gates that are about REDUNDANCY and SAMPLE LENGTH. Liquidity, the
# price grid and the volatility floor are about whether the instrument can be traded at
# all, and an override there would be a different and much worse claim — it would put a
# name in the book that the desk cannot fill. All three clear those three gates already.
OVERRIDABLE = ("corr", "tradable years")

OUT_CSV = config.RESULTS_DIR / "universe_screen_cme_futures.csv"
GENERATED = Path(__file__).resolve().parent / "universes_futures.py"


def measure(timeframe: str = "1d") -> pd.DataFrame:
    """One row per pooled root: how much money, how many years, how coarse the grid."""
    bars = td_loader.load("cme_futures", timeframe, config.CME_POOL)
    rows = []
    for symbol in config.CME_POOL:
        df = bars.get(symbol)
        if df is None or df.empty:
            rows.append({"symbol": symbol, "root": symbol.split(".")[0], "bars": 0})
            continue
        root = symbol.split(".")[0]
        s = futures_specs.spec(root)
        # Notional is computed on the LAST close, the one bar of a back-adjusted series
        # that is still a real quote. Using the adjusted history would price 2010 corn at
        # today's contract level and inflate the turnover of every long-dated root.
        per_contract = futures_specs.notional_usd(root, float(df["Close"].iloc[-1]))
        rng = (df["High"] - df["Low"]) / df["Close"]
        rows.append({
            "symbol": symbol,
            "root": root,
            "sector": s["sector"],
            "exchange": s["exchange"],
            "desc": s["desc"],
            "bars": len(df),
            "first": df.index[0].date().isoformat(),
            "last": df.index[-1].date().isoformat(),
            "years": (df.index[-1] - df.index[0]).days / 365.25,
            "notional_per_contract": per_contract,
            "median_volume": float(df["Volume"].median()),
            "adv_usd": float(df["Volume"].median()) * per_contract,
            "daily_range_pct": 100.0 * float(rng.median()),
            "tick_of_range": (s["tick"] / float(df["Close"].iloc[-1])) / float(rng.median()),
            "ann_vol_pct": 100.0 * float(df["Close"].pct_change().std() * np.sqrt(252)),
        })
    return pd.DataFrame(rows)


def correlations(timeframe: str, symbols: list[str]) -> pd.DataFrame:
    """Pairwise correlation of daily returns over the window every name shares."""
    bars = td_loader.load("cme_futures", timeframe, symbols)
    rets = pd.DataFrame({s: bars[s]["Close"].pct_change() for s in symbols if s in bars})
    return rets.corr()


def screen(timeframe: str = "1d", max_corr: float = MAX_CORR,
           min_vol: float = MIN_ANN_VOL_PCT) -> tuple[pd.DataFrame, list[str], pd.DataFrame]:
    """Returns (the ranked table, the accepted symbols in order, the correlation matrix).

    Three return values rather than one frame carrying `.attrs`: pandas does not promise
    to carry `attrs` through a `sort_values`, and a screen that silently forgets which
    names it accepted is worse than one that is verbose about it.
    """
    table = measure(timeframe)
    if table.empty or "adv_usd" not in table:
        return table, [], pd.DataFrame()

    forced = set(ALWAYS_KEEP)
    reasons: dict[str, str] = {}
    # Why an OVERRIDDEN name is in, which is a different column's worth of meaning from
    # why a rejected one is out — but it belongs in the same column, because `reason` is
    # "why is this row what it is" and a kept-despite is exactly that. Losing it would
    # leave a universe whose most questionable members look like its most ordinary ones.
    notes: dict[str, str] = {}
    for _, r in table.iterrows():
        if not r.get("bars"):
            # Not overridable, and it is the one rejection that cannot be: there is
            # nothing to trade, score or correlate.
            reasons[r["symbol"]] = "no bars cached"
        elif r["years"] < MIN_TRADABLE_YEARS:
            short = (f"only {r['years']:.1f} tradable years "
                     f"(need {MIN_TRADABLE_YEARS:.0f})")
            if r["symbol"] in forced:
                notes[r["symbol"]] = f"ALWAYS_KEEP: {short}"
            else:
                reasons[r["symbol"]] = short
        elif r["adv_usd"] < MIN_ADV_USD:
            reasons[r["symbol"]] = (f"${r['adv_usd'] / 1e6:,.0f}M/day "
                                    f"(need ${MIN_ADV_USD / 1e6:,.0f}M)")
        elif r["tick_of_range"] > MAX_TICK_OF_RANGE:
            reasons[r["symbol"]] = (f"one tick is {100 * r['tick_of_range']:.1f}% of a "
                                    f"day's range -- a grid, not a market")
        elif r["ann_vol_pct"] < min_vol:
            reasons[r["symbol"]] = (f"{r['ann_vol_pct']:.1f}% annual vol, daily range "
                                    f"{r['daily_range_pct']:.2f}% -- costs eat the move")

    # Correlation runs last and only over what survived, because a name rejected for
    # liquidity must not be able to knock out a name that would have been kept.
    survivors = [s for s in table["symbol"] if s not in reasons]
    survivors = sorted(survivors,
                       key=lambda s: -table.set_index("symbol").loc[s, "adv_usd"])
    corr = correlations(timeframe, survivors) if survivors else pd.DataFrame()

    def _worst(symbol: str, against: list[str]):
        """The strongest correlation `symbol` has with anything already accepted."""
        hits = [(k, corr.loc[symbol, k]) for k in against
                if symbol in corr.index and k in corr.columns
                and pd.notna(corr.loc[symbol, k])]
        return max(hits, key=lambda kv: abs(kv[1])) if hits else None

    # `comparators` is NOT `accepted`, and the split is the whole behaviour of the
    # override. An ALWAYS_KEEP name is added to the universe but never becomes something
    # later candidates are measured against, so admitting one can only ADD a contract —
    # never silently evict a different one. "Also carry NQ" must not turn into "…and drop
    # whatever now looks like NQ", which is what reusing one list would have done.
    accepted: list[str] = []
    comparators: list[str] = []
    for symbol in survivors:
        if symbol in forced:
            accepted.append(symbol)
            worst = _worst(symbol, comparators)
            if worst and abs(worst[1]) > max_corr:
                said = f"corr {worst[1]:+.2f} with {worst[0]} -- the same asset"
                notes[symbol] = (f"{notes[symbol]}; {said}" if symbol in notes
                                 else f"ALWAYS_KEEP: {said}")
            elif symbol not in notes:
                notes[symbol] = "ALWAYS_KEEP (clears every gate on its own anyway)"
            continue
        clash = None
        for kept in comparators:
            if symbol in corr.index and kept in corr.columns:
                c = corr.loc[symbol, kept]
                if pd.notna(c) and abs(c) > max_corr:
                    clash = (kept, c)
                    break
        if clash:
            reasons[symbol] = f"corr {clash[1]:+.2f} with {clash[0]} -- the same asset"
        else:
            accepted.append(symbol)
            comparators.append(symbol)

    table["kept"] = ~table["symbol"].isin(reasons)
    table["reason"] = (table["symbol"].map(reasons)
                       .fillna(table["symbol"].map(notes)).fillna(""))
    ranked = table.sort_values("adv_usd", ascending=False, na_position="last")
    return ranked, accepted, corr


def write_module(accepted: list[str], table: pd.DataFrame) -> None:
    """Emit `universes_futures.py`, the generated list `config.CME_FUTURES` reads."""
    kept = table[table["kept"]].set_index("symbol")
    lines = [
        '"""GENERATED by `futures_screen.py --write`. Do not edit.',
        "",
        "The CME roots that survived the liquidity, history, price-grid and correlation",
        "gates. `config.CME_POOL` is what was ranked; this is what is traded.",
        "",
        "Rows marked (ALWAYS_KEEP) did NOT survive a gate -- they are carried by the",
        "override list in `futures_screen.py`, which says what that costs. A name marked",
        "there is redundant with one above it, so `n_assets` on this class counts",
        "contracts rather than independent bets and `metrics.se_ir` is optimistic by",
        "whatever that redundancy is worth.",
        '"""',
        "",
        "CME_SCREENED = [",
    ]
    for symbol in accepted:
        r = kept.loc[symbol]
        flag = " (ALWAYS_KEEP)" if symbol in ALWAYS_KEEP else ""
        lines.append(f'    "{symbol}",'.ljust(16)
                     + f"  # {r['desc']}, ${r['adv_usd'] / 1e9:,.1f}B/day{flag}")
    lines += ["]", ""]
    GENERATED.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--max-corr", type=float, default=MAX_CORR)
    ap.add_argument("--min-vol", type=float, default=MIN_ANN_VOL_PCT,
                    help="annualised volatility floor, in percent")
    ap.add_argument("--write", action="store_true",
                    help="commit the survivors to universes_futures.py")
    args = ap.parse_args()

    table, accepted, corr = screen(args.tf, args.max_corr, args.min_vol)
    if table.empty or "adv_usd" not in table:
        raise SystemExit("nothing cached — run `python db_loader.py` first")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT_CSV, index=False)

    show = ["root", "sector", "years", "adv_usd", "daily_range_pct", "tick_of_range",
            "ann_vol_pct", "kept", "reason"]
    view = table[[c for c in show if c in table.columns]].copy()
    if "adv_usd" in view:
        view["adv_usd"] = (view["adv_usd"] / 1e9).round(2)
        view = view.rename(columns={"adv_usd": "adv_$B"})
    if "tick_of_range" in view:
        view["tick_of_range"] = (100 * view["tick_of_range"]).round(2)
        view = view.rename(columns={"tick_of_range": "tick_%range"})
    print(view.to_string(index=False))

    print(f"\nkept {len(accepted)} of {len(config.CME_POOL)}: {' '.join(accepted)}")
    if len(accepted) > 1 and not corr.empty:
        block = corr.loc[accepted, accepted].to_numpy()
        off = np.abs(block[np.triu_indices(len(accepted), k=1)])
        # The one number to quote about a universe whose noise ceiling assumes its assets
        # are independent. The ETF class sits at 0.44 after its screen and was at 0.72
        # before it; anything approaching that is a universe pretending to be wider than
        # it is.
        print(f"mean |pairwise correlation| among the kept: {np.nanmean(off):.2f} "
              f"(max {np.nanmax(off):.2f})")
    by_sector = table[table["kept"]].groupby("sector").size().to_dict()
    print(f"sectors: {by_sector}")
    print(f"-> {OUT_CSV}")

    if args.write:
        write_module(accepted, table)
        print(f"-> {GENERATED}\n   now point `config.CME_FUTURES` at "
              f"`universes_futures.CME_SCREENED`.")


if __name__ == "__main__":
    main()
