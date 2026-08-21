"""Build the dashboard payload: one document, both emitters.

`build_dashboard.py` turns this into either `web/data.js` (the served SPA) or a
self-contained `dist/dashboard.html`. Both read the same payload, which is the point --
they used to be two independent builders over two different subsets of the results, and
drifted a day apart in practice.

What is real here and what is not:

* **Backtest** — every figure comes from `walk-forward optimization/results/
  wf_summary_*.csv` and `wf_per_asset_*.csv`, which are the walk-forward out-of-sample
  sweeps. Nothing is synthesised.
* **Equity curves** — not persisted by the sweep, so rule detail pages omit the chart
  rather than draw an invented one. Adding them means writing the stitched net-return
  series per rule, which is a change to `walkforward.py`, not to this file.
* **Paper trading** — empty until the Nautilus node runs long enough to fill an order and
  write `results/paper_state.json`. An empty list renders as "nothing running yet", which
  is the truth; it must never be padded with placeholders.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist

import pandas as pd

import dash_config
from dash_config import (BRIEF_EQUITIES, GROUPS, HEADLINE, TIMEFRAMES, TOP_N,
                         WFO_RESULTS)
from stockhunt.artifacts import read_bulk

# The timeframes the DESK offers, taken from the desk rather than restated. The paper
# page's filter strip was a hard-coded `1d / 4h` while `MEMBER_TIMEFRAMES` had grown to
# six, so a member's strategy registered at 1h or 5m ran, published, and could not be
# reached from this board at all -- the same failure the class strip had, one axis over.
#
# `paper_config` is safe to import here and is the ONLY module in that folder that is: it
# imports the backtest engine's `config` and nothing heavier, where `run_paper` and
# `backtest_paper` would drag `nautilus_trader` into a page builder.
if str(dash_config.PAPER) not in sys.path:
    sys.path.insert(0, str(dash_config.PAPER))
import paper_config                                                     # noqa: E402

HERE = dash_config.HERE
WEB = dash_config.WEB
# Kept as `BM` because ~20 call sites below read it; it now points at the walk-forward
# results rather than the engine's, which is where wf_summary_* moved.
BM = WFO_RESULTS


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


def text(v) -> str:
    """A CSV cell as a string, with pandas' float NaN rendered as empty rather than "nan"."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v)
    return "" if s in ("nan", "None") else s


def num(v, d=3):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else round(f, d)


def excess_cagr(r: dict) -> float | None:
    """A row's annualised rate minus its benchmark's. `None` if either side is missing."""
    a, b = r.get("net_cagr"), r.get("bh_cagr")
    return None if a is None or b is None else round(a - b, 2)


def excess_pnl(r: dict) -> float | None:
    """A row's total return minus its benchmark's, in percentage points.

    The page renders this as money — a fixed $10k stake under each, differenced — and that
    is a strictly increasing function of this number, so ordering on either is the same
    ordering. `None` if either side is missing.
    """
    a, b = r.get("net_pct"), r.get("bh_pct")
    return None if a is None or b is None else round(a - b, 1)


def _rank_assets(rows: list[dict]) -> tuple[list[dict], dict]:
    """EVERY name the rule was run on, ranked by P&L against buy-and-hold.

    **Nothing is cut.** This used to ship the best 10 and worst 5 by excess CAGR, which
    made the table a selection: the middle was invisible, the ends had to be defended
    against short-window rates with a span floor, and the caption had to keep saying the
    panel was not a sample anyone could average. Showing all of them removes the selection
    and every hazard that came with it — a reader can now see the whole distribution, count
    the winners themselves, and find any name they care about.

    The order is the money column, `P&L vs B&H`, and that is a change of key as well as of
    length. While this was a selection the key had to be the excess *rate*: `years` differs
    per asset by a factor of twenty, so ranking the ends on dollars ranked them partly on
    holding period — KEY led on span alone at 38.0%/yr while GGP's 94.5%/yr placed 4th. As
    a pure sort order that bias is no longer a distortion of *which* names are shown, only
    of which appear first, and the rate is still on the row (`vs B&H / yr`) for a reader who
    wants the other reading. The span floor goes with the selection it existed to protect.

    Returns `(rows, meta)`. Callers still take breadth from `_asset_stats`, because
    `unranked` names — no total return on one side — carry no comparison to count.
    """
    for r in rows:
        r["xcagr"] = excess_cagr(r)
        r["xpnl"] = excess_pnl(r)
    # Nulls sink, so a name with nothing to compare cannot win the sort by being blank.
    ranked = sorted(rows, key=lambda r: (r["xpnl"] is None, -(r["xpnl"] or 0.0)))
    return ranked, {"unranked": sum(1 for r in rows if r["xpnl"] is None)}


def _asset_stats(rows: list[dict]) -> dict:
    """Breadth and a MEDIAN-asset P&L, over the whole list.

    The median exists because the alternative was producing a wrong number on the page.
    `wf_summary.net_return_pct` is a mean of per-asset *total* returns, which `backtest
    engine/CLAUDE.md` warns about explicitly: annualise first, average second. Across 751
    names over 30.6 years the mean reached 996,990,228,818,076% and the detail page
    rendered "$10k became $99,699,022,881,817,632" at 166%/yr. One asset compounding
    1e15x drags a mean anywhere; it cannot drag a median.
    """
    irs = [r["ir"] for r in rows if r.get("ir") is not None]

    def med(key):
        v = sorted(r[key] for r in rows if r.get(key) is not None)
        if not v:
            return None
        n = len(v)
        return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2

    return {"n": len(rows), "pos": sum(1 for v in irs if v > 0), "scored": len(irs),
            "net_pct": med("net_pct"), "bh_pct": med("bh_pct"), "years": med("years")}


def build_sheet(cls: str, tf: str, universe: list[str]) -> dict | None:
    """One leaderboard per (class, timeframe), singles and pairs ranked together.

    They were two lists until they were not. A pair of rules joined by `or` is a strategy
    in the same sense a single rule is — the same walk-forward calendar, the same folds,
    the same gates — and splitting the page by which sweep emitted a row asks the reader to
    care about a detail of this repo's plumbing. What genuinely differs between sheets is
    the price series, the cost grid and the benchmark, so class and timeframe stay as the
    two axes and nothing else does.

    The cost of merging is that pairs then take most of the top of the equity sheets — 22 of
    the top 25 on 1d stocks — and they do it on exposure, not skill. That is why every row
    now carries `long_frac`, singles included (`walkforward.py` emits it as of this change),
    and why the trial count and the noise ceiling are computed over the merged population
    rather than one list at a time: ranking 385 candidates and reporting the ceiling for 245
    of them would understate what luck alone reaches.
    """
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

    per = _per_asset_from_riskmatch(cls, tf)
    # `riskmatch.parquet` is the SAME backtest the verdict came from, kept at per-symbol
    # grain — 263,974 rows of (class, tf, side, rule, symbol) carrying wealth, CAGR,
    # benchmark and years. Reading it means the detail page and the row above it are the
    # same measurement, and it costs a file read rather than a re-run.
    #
    # `wf_per_asset_*` and `strat_per_asset_*` are the fallback, used only for rules the
    # standard never scored. They are a SEPARATE computation of the same quantity by
    # `walkforward.py` and `strat_wf.py`, on an IR basis rather than a risk-matched one,
    # and keeping them as the primary source is what put a 53.6-year MNST at 2.78e12% on
    # the `ibs` page while the leaderboard row above it was scored over 23.6 years. Two
    # stages computing per-asset backtests independently is the disconnect; this makes the
    # cheap one authoritative and leaves the expensive one as a gap-filler.
    # `read_bulk` prefers the Parquet and falls back to the CSV of the same stem. These
    # two tables were 77 MB and 53 MB of CSV on us_stocks 1d; they are now written as
    # Parquet by `walkforward.py` and `strat_wf.py`. The fallback is what lets this keep
    # working against sheets written before that change, so the dashboard never has to be
    # re-run in lockstep with the stage that feeds it.
    for pa_path, ir_col in ((BM / f"wf_per_asset_{cls}_{tf}", "ir_union"),
                            (BM / f"strat_per_asset_{cls}_{tf}", "ir_wf")):
        pa = read_bulk(pa_path)
        if pa is None or ir_col not in pa.columns:
            continue
        # Same fallback as `catalog_frame`: sheets written during the gross-only window
        # carry no `retail` rows, and filtering them to nothing is silent.
        if "scenario" in pa.columns:
            present = set(pa.scenario.unique())
            pa = pa[pa.scenario == (scen if scen in present else sorted(present)[0])]
        # Each asset annualises over *its own* out-of-sample span. The sheet median is
        # 41 years on 1d equities, but META listed in 2012 and NVDA in 1999 — using the
        # median would understate the newer names by a factor of three.
        has_yrs = "years_oos" in pa.columns
        for rule, g in pa.groupby("rule"):
            if str(rule) in per:            # riskmatch already supplied it, and fresher
                continue
            rows = per.setdefault(str(rule), [])
            for r in g.itertuples():
                y = float(getattr(r, "years_oos", float("nan"))) if has_yrs else float("nan")
                if not (y and y == y):                      # NaN or 0 -> fall back
                    y = years
                net_pct = num(getattr(r, "ret_pct", None), 1)
                bh_pct = num(getattr(r, "bench_pct", None), 1)
                rows.append({
                    "symbol": str(r.symbol), "ir": num(getattr(r, ir_col)), "years": round(y, 1),
                    "net_cagr": cagr(net_pct, y), "bh_cagr": cagr(bh_pct, y),
                    # Raw compounded return as well as the annualised rate. The site turns
                    # these into what a fixed stake became, which is the form a P&L
                    # question is actually asked in.
                    "net_pct": net_pct, "bh_pct": bh_pct})
    # Breadth and the median asset come off the same full list the page now renders; the
    # ranking pass only orders it, and reports how many names had nothing to compare.
    per_stats = {k: _asset_stats(v) for k, v in per.items()}
    ranked = {}
    for k, v in per.items():
        ranked[k], meta = _rank_assets(v)
        per_stats[k].update(meta)
    per = ranked

    pairs = pair_frame(cls, tf, scen)
    cat = catalog_frame(cls, tf, scen)
    n_singles, n_pairs, n_cat = int(len(h)), int(len(pairs)), int(len(cat))
    n_all = n_singles + n_pairs + n_cat

    parts = [h.assign(kind="single")]
    if n_pairs:
        parts.append(pairs.assign(kind="pair"))
    if n_cat:
        parts.append(cat.assign(kind="published"))
    merged = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]

    # Computed over everything ranked, not over the 25 rows shown: the question the reader
    # needs answered is whether this leaderboard is a ranking of skill or of time-in-market,
    # and the top of a list sorted by IR is the least representative slice of it.
    corr = None
    if "long_frac" in merged and merged["long_frac"].notna().any():
        c = merged[["ir_net", "long_frac"]].corr().iloc[0, 1]
        corr = None if pd.isna(c) else round(float(c), 3)

    # The acceptance standard, merged onto the same rows. `edge_standard.csv` scores every
    # rule twice — as published (long/flat) and with "stay out" turned into "sell it" — and
    # only the better side is carried here: short is optional, so a rule is represented by
    # whichever version of itself is stronger, with the side named on the row.
    edge = _edge_index(cls, tf)
    # NOT `_edge`: `DataFrame.itertuples` renames any column starting with an
    # underscore to a positional `_1`, `_2`, so `getattr(r, "_edge")` silently returns the
    # default and every row loses its verdict.
    merged = merged.assign(edge_rec=[edge.get(str(r)) for r in merged["rule"]])
    book, book_bench = _book_index(cls, tf)
    merged = merged.assign(book_rec=[book.get(str(r)) for r in merged["rule"]])
    # Ranked on the BOOK's cash-matched excess CAGR: what one account holding the whole
    # equal-weighted, point-in-time universe earned per year above that same universe held
    # passively, with the passive side scaled DOWN with T-bills to the rule's own
    # volatility. It replaced raw per-asset Sharpe on 2026-08-13.
    #
    # Why the book and not the median asset. Both are on the table and both are honest, but
    # they answer different questions and only one of them is a portfolio. The median-asset
    # columns say what a typical stock did; an account does not hold the median stock, it
    # holds all of them at once, and diversification, rebalancing and membership churn all
    # land in the gap. On us_stocks 1d that gap is the difference between $26k and a figure
    # an order of magnitude larger for the SAME buy-and-hold.
    #
    # Why cash-matched and not CAGR. `corr(IR, long_frac)` is 0.881 on daily equities: an
    # account-level return ranking is substantially a ranking of who stayed invested
    # longest. Scaling the baseline down to the rule's volatility prices that away, so a
    # rule holding 47% of the time is compared with holding 47% of the market and the rest
    # in bills rather than with the whole market.
    #
    # Why not Sharpe, which this used to rank on. Sharpe is blind to variance drag — on
    # SOXL, `ibs` scores IR −0.13 while compounding +25pp/yr over buy-and-hold — and the
    # money column is what a reader is actually trying to read off a leaderboard.
    #
    # Expectancy is still NOT the tiebreak. It is measured per TRADE, so it rewards trading
    # rarely: `RANDOM_50` scores +3.1%/trade against `ibs`'s +0.68% purely by opening 72
    # positions instead of 642, and buy-and-hold scores +894% on the single position it
    # holds for 23 years.
    #
    # Since 2026-08-13 the six acceptance criteria ARE the ordering, and the money above is
    # the tiebreak. The page has exactly one verdict on it and it was not the thing that
    # placed a row: a rule clearing 5 of 6 could sit below one clearing 2 because it earned
    # less at equal risk, which asked the reader to rank on one column while judging on
    # another. Sorting on the standard puts the strongest evidence at the top and leaves the
    # money to separate rows the standard cannot.
    #
    # It is a coarse key — six integer tiers, and nothing has ever cleared all six — so it
    # decides little on its own and everything below it is still ordered by the basis
    # described under `edge_tie`. That is the point: the tiers say how much of the standard
    # was cleared, the tiebreak orders within a tier.
    if book:
        merged["edge_tie"] = [(b or {}).get("cm_excess_cagr") if b else None
                              for b in merged["book_rec"]]
        merged["edge_tie"] = [float("-inf") if v is None else v
                              for v in merged["edge_tie"]]
    else:
        # No book sheet for this class/tf — fall back to the previous basis rather than
        # ranking every row identically at -inf and letting row order decide the board.
        merged["edge_tie"] = [(e or {}).get("sharpe", float("-inf"))
                              if e else float("-inf") for e in merged["edge_rec"]]
    # An unscored row has no count. It sorts below every scored one rather than at zero,
    # because "the standard never looked at this" is not "it cleared nothing" — and those
    # rows are dropped a few lines down anyway.
    merged["edge_rank"] = [(e or {}).get("passed", -1) if e else -1
                           for e in merged["edge_rec"]]
    # DROPPED, not sorted last. `edge_standard.csv` and the `wf_*`/`cwf_*` leaderboards are
    # written by different stages and can be from different studies, and after the
    # 2026-08-11 shortlist re-run they were: the standard covered 89 rules over 23.6 years
    # on 642 quarantined names, while `wf_summary_us_stocks_1d` still held 247 rules over
    # 30.6 years on the old 704 including the recycled tickers.
    #
    # Carrying the unscored rows meant one printed row read left-to-right across two
    # universes — stale IR, Long % and CAGR beside a fresh Sharpe and verdict — which is
    # exactly the comparison the repo forbids. A blank verdict column looks like a gap in
    # the data; a stale diagnostic beside a fresh one looks like a measurement.
    #
    # So the page shows the study the standard actually ran. Rows it never scored are not
    # on it, and `n_rules` still reports the full population underneath so the reader can
    # see how much was cut.
    scoped = merged[[e is not None for e in merged["edge_rec"]]]
    dropped = len(merged) - len(scoped)
    # A rule that never opens a position is not a strategy, and it was reaching the board.
    #
    # `BBANDS`, `T3_1000` and `CDL3STARSINSOUTH` sat 5th, 6th and 7th on us_stocks 1d
    # holding NOTHING for 23.6 years — 0% exposure, 0 trades, $14,875 of pure cash — and
    # they ranked there because the per-asset standard scored their SHORT side, which is
    # "always sell everything" and a different strategy from the one every other column
    # described. The ranking is the book's now, so the fix is to require the book to have
    # traded: no positions opened, no row. They are still in `book_*.csv` for anyone
    # asking what a rule that never fires does.
    #
    # Filtered on the trade count rather than on exposure because that is the column the
    # page prints. A rule with a whisper of exposure and one trade is a bad strategy and
    # stays; a rule with none is not one.
    before_flat = len(scoped)
    scoped = scoped[[not (b and b.get("n_trades") == 0) for b in scoped["book_rec"]]]
    n_flat = before_flat - len(scoped)
    # Two keys, stable: criteria cleared first, then the money at equal risk. `nlargest`
    # takes one column, and a stable sort is what makes the second key the tiebreak rather
    # than an accident of row order.
    top = scoped.sort_values(["edge_rank", "edge_tie"], ascending=False,
                             kind="stable").head(TOP_N)
    rows = [leaderboard_entry(r, per, per_stats) for r in top.itertuples()]

    scored = [r for r in rows if r.get("edge")]
    # The header describes the sheet the VERDICT was computed on, not the one the stale
    # leaderboard was. `years` off `wf_summary` printed 30.6 above rows scored over 23.6.
    edge_years = _edge_years(cls, tf)
    return {"timeframe": tf, "years": round(edge_years or years, 1),
            # The book's own benchmark and its own span. Carried at sheet level because it
            # is identical on every row — one universe held passively — and because the
            # page has to be able to print the span beside the money rather than let a
            # reader assume it shares the header's per-asset `years`.
            "book_bench": book_bench,
            "n_book": sum(1 for r in rows if r.get("book")),
            # Two fields because the ranking has two keys, and the note above the table
            # names both. Letting the page infer the tiebreak from `ranked_on` is how a
            # caption ends up describing a basis the sheet does not use: `us_etfs 4h` has
            # no book run, so its ties break on Sharpe while `us_stocks 1d`'s break on the
            # book's excess.
            "ranked_on": "edge_passed",
            "ranked_tiebreak": "book_cm_excess_cagr" if book else "edge_sharpe",
            "n_unscored_dropped": dropped,
            "n_flat_dropped": n_flat,
            "folds": int(h["n_folds"].median()) if "n_folds" in h else None,
            "universe": universe, "rows": rows,
            "n_rules": n_all, "n_singles": n_singles, "n_pairs": n_pairs,
            "n_catalog": n_cat,
            "n_shown_pairs": sum(1 for r in rows if r["kind"] == "pair"),
            "exposure_corr": corr,
            "n_scored": len(edge), "n_shown_scored": len(scored),
            # How many names the SCORED columns actually ran on, which is not the size of
            # the universe list beside it. `universe` is every symbol the sheet knows;
            # `edge_standard` drops the quarantined impostors and anything without enough
            # post-2000 history, and every figure in the table rests on what survived.
            # Printing only the larger number made the table look broader than it is.
            "n_assets_scored": _edge_assets(cls, tf),
            "n_pass": sum(1 for r in book.values()
                          if (r.get("standard") or {}).get("verdict") == "PASS"),
            # Powered is now a statement about the BOOK, because the book is what the
            # Standard column scores. The two disagree on the intraday sheets and the
            # book is the stricter reading: `us_stocks 4h` gives a name ~4 years of
            # out-of-sample history, which is 2 walk-forward folds, and a t across 2
            # folds resolves nothing. Reporting "0 of 30 passed" there would be
            # manufacturing a null out of a sheet that could not have detected anything.
            "powered": (bool(next(iter(book.values()))["standard"]["powered"])
                        if book and next(iter(book.values())).get("standard") else None),
            "book_folds": (next(iter(book.values())).get("n_folds")
                           if book else None),
            "noise_ceiling": round(noise_ceiling(n_all, years), 2)}


_EDGE_CACHE: dict[tuple, dict] = {}


_RM_CACHE: dict = {}


def _per_asset_from_riskmatch(cls: str, tf: str) -> dict:
    """`rule -> per-asset rows`, taken from the file the VERDICT was computed in.

    `riskmatch_wf.py` already scores every (rule, symbol, side) individually and only then
    aggregates to the sheet row shown on the leaderboard. That per-symbol layer is written
    whole to `riskmatch.parquet`, so the asset-by-asset table is a groupby away and needs
    no backtest at all — where regenerating `strat_per_asset_*` means re-running the entire
    113-cell catalogue across eight sheets, roughly two hours, to recover numbers that were
    already computed ten minutes earlier by a different stage.

    Sides are collapsed to the ONE side `_edge_index` already endorsed for that rule, so
    the detail page describes the version of the rule the leaderboard is ranking. It is
    not a decision made here; it is a lookup of a decision made in `edge_standard.csv`.

    It used to be `drop_duplicates(["rule", "symbol"])` on `causal_wealth`, which is a
    different thing entirely: the better of long/short chosen **per symbol**, after seeing
    how both turned out. That is selection on the test set, and it is not free — on `ibs`
    at us_stocks 1d it mixed 96 short rows into 518 long ones and moved the median asset
    from $99,735 to $103,670, so the detail page's "$10k became" disagreed with the
    leaderboard's "$10k at equal risk" on the same row. Breadth was counted on the same
    cherry-picked table.

    The docstring that shipped with that code claimed it collapsed "one row per rule" on
    "the same quantity `_edge_index` ranks sides by, so the two cannot disagree". All three
    clauses were false — per `(rule, symbol)`, on `causal_wealth` against `_edge_index`'s
    `dsharpe` — which is why the disagreement went unnoticed.

    Where a rule has no rankable `edge_standard` row the side cannot be looked up, so it
    is chosen at RULE level on median risk-matched wealth. Still one side for the whole
    table, still never per symbol; it is a weaker tie-break, not a different policy.

    Percentages, not dollars, because the site's own formatter turns a percent into "what
    $10,000 became". `causal_wealth` is already money on a $10,000 stake, so it converts
    back rather than being passed through and double-counted.
    """
    key = (cls, tf)
    if key in _RM_CACHE:
        return _RM_CACHE[key]
    path = BM / "riskmatch.parquet"
    out: dict[str, list] = {}
    if not path.exists():
        _RM_CACHE[key] = out
        return out
    df = pd.read_parquet(path)
    g = df[(df["class"] == cls) & (df["tf"] == tf)]
    if g.empty:
        _RM_CACHE[key] = out
        return out
    # The side the leaderboard is showing, looked up rather than re-derived. `_edge_index`
    # is the only place that decision is made, and it makes it on `dsharpe`.
    endorsed = {k: v.get("side") for k, v in _edge_index(cls, tf).items()}
    cap = 10_000.0
    for rule, grp in g.groupby("rule"):
        side = endorsed.get(str(rule))
        if side is None:
            # No rankable edge row for this rule. Fall back to a RULE-level choice on
            # median risk-matched wealth — one side for the whole table either way.
            med = grp.groupby("side")["causal_wealth"].median()
            side = str(med.idxmax()) if len(med) else None
        if side is not None:
            picked = grp[grp["side"] == side]
            # An endorsed side that is absent from `riskmatch.parquet` means the two
            # stages were run against different universes. Showing the other side under
            # this rule's name would be exactly the mislabelling this function exists to
            # stop, so drop the rule from the table and let the page render "no per-asset
            # rows" rather than a confident wrong one.
            if picked.empty:
                continue
            grp = picked
        # Defensive only: one side per rule should already be one row per symbol.
        grp = grp.drop_duplicates(["symbol"])
        rows = []
        for r in grp.itertuples():
            y = float(r.years) if r.years == r.years else float("nan")
            # Four decimals, not one. These are percentages of a $10,000 stake, so one
            # decimal is $10 of granularity — invisible on a winner and the whole number on
            # a wipeout, where the leaderboard's dollar-rounded $16 met a detail page
            # rounding -99.8% to $20. Same measurement, 25% apart, purely from precision.
            net_pct = num((float(r.causal_wealth) / cap - 1.0) * 100.0, 4)
            bh_pct = num((float(r.bench_wealth) / cap - 1.0) * 100.0, 4)
            rows.append({
                "symbol": str(r.symbol),
                # `sharpe_edge` is this row's Sharpe minus its benchmark's, which is what
                # the leaderboard now ranks on. The column is still labelled IR on the
                # page; it has never been an information ratio here.
                "ir": num(float(r.sharpe_edge)),
                "years": round(y, 1) if y == y else None,
                "net_cagr": num(float(r.causal_cagr) * 100.0, 1),
                "bh_cagr": num(float(r.bench_cagr) * 100.0, 1),
                "net_pct": net_pct, "bh_pct": bh_pct})
        out[str(rule)] = rows
    _RM_CACHE[key] = out
    return out


def _edge_years(cls: str, tf: str) -> float | None:
    """Out-of-sample years the STANDARD scored, for the sheet header.

    Separate from `wf_summary`'s `years` because the two stages are re-run independently
    and drifted apart the moment `config.BACKTEST_START` moved: the header said 30.6 years
    over every row scored on 23.6.
    """
    df = _read(BM / "edge_standard.csv")
    if df.empty:
        return None
    col_tf = "timeframe" if "timeframe" in df.columns else "tf"
    g = df[(df["class"] == cls) & (df[col_tf] == tf)]
    return None if g.empty else float(g["years"].median())


def _edge_assets(cls: str, tf: str) -> int | None:
    """How many names the standard actually ran on. Same drift as `_edge_years`.

    The universe list is every symbol the sheet knows about; this is what survived the
    quarantine and the history floor and therefore what every scored column rests on.
    On us_stocks 1d that is 614 against a 751-name universe, and the header used to
    advertise only the larger one.
    """
    df = _read(BM / "edge_standard.csv")
    if df.empty or "n_assets" not in df.columns:
        return None
    col_tf = "timeframe" if "timeframe" in df.columns else "tf"
    g = df[(df["class"] == cls) & (df[col_tf] == tf)]
    return None if g.empty else int(g["n_assets"].median())


_BOOK_CACHE: dict[tuple, tuple] = {}


def _book_standard(r) -> dict | None:
    """The six-criteria verdict off a `book_*.csv` row.

    The gate order is `config.EDGE_STANDARD`'s and is imported rather than typed out, so
    the booleans line up with the labels the page renders beside them (`D.edge_criteria`).
    Getting that order wrong would tick the wrong criterion's name in every tooltip on the
    site and never raise anything.

    Returns `None` for a CSV written before the standard moved to the book, so the column
    prints an em-dash instead of `0/6`, which would read as a measured failure.
    """
    passed = getattr(r, "edge_passed", None)
    if passed is None or passed != passed:
        return None
    return {
        "passed": int(passed),
        "n": int(getattr(r, "edge_n", 6) or 6),
        "gates": [bool(getattr(r, f"edge_gate_{c['key']}", False))
                  for c in dash_config.bt_config.GATES],
        "verdict": text(getattr(r, "edge_verdict", None)),
        "powered": bool(getattr(r, "edge_powered", False)),
        "rankable": bool(getattr(r, "edge_rankable", False)),
        "t_bar": num(getattr(r, "edge_t_bar_corrected", None), 2),
        # How the bar was set, and what the naive correction would have said. Both on the
        # row because "t = 2.81 failed" is unreadable without the number it failed against
        # and where that number came from.
        "t_bar_source": text(getattr(r, "edge_t_bar_source", None)),
        "t_bar_bonferroni": num(getattr(r, "t_bar_bonferroni", None), 2),
        "n_candidates": int(getattr(r, "n_candidates_maxt", 0) or 0),
    }


def _per_asset(total, n_names) -> float | None:
    """A pooled book count spread over the names that produced it."""
    try:
        n = float(n_names or 0)
        return round(float(total) / n, 1) if n > 0 and total is not None else None
    except (TypeError, ValueError):
        return None


def _expectancy(r) -> float | None:
    """What one trade was worth on average, as a fraction: `p*W - (1-p)*L`.

    Derived rather than stored, because it is exactly the three columns beside it and a
    fourth CSV column that can disagree with its own inputs is worse than an expression.
    `avg_loss` is already signed negative in `portfolio_wf._trade_columns`, so the two
    terms ADD.
    """
    p, w, l = (getattr(r, "win_rate", None), getattr(r, "avg_win", None),
               getattr(r, "avg_loss", None))
    try:
        if p is None or w is None or l is None or any(v != v for v in (p, w, l)):
            return None
        return round(float(p) * float(w) + (1.0 - float(p)) * float(l), 5)
    except (TypeError, ValueError):
        return None


def _book_record(r) -> dict:
    """One row of a `portfolio_wf` book sheet, as the leaderboard reads it.

    Lifted out of `_book_index` so that the converted-strategy board can read the SAME
    record from `convert_*.csv`. Those sheets are `portfolio_wf.py` output too -- same
    stage, same columns, same conventions -- and the alternative was a second, drifting
    definition of what a book row is on a page whose whole point is that there is one
    measurement. If a field moves here it moves on both boards at once, which is the
    property worth having.
    """
    return {
        "wealth": num(getattr(r, "wealth", None), 0),
        "cagr": num(getattr(r, "cagr", None), 4),
        # Carried per row, like `edge.bench_wealth`, so the money cell can colour
        # itself against the book's OWN baseline without the sheet being threaded
        # into every cell renderer. It is the same figure on every row.
        "bench_wealth": num(getattr(r, "bench_wealth", None), 0),
        # The cash-matched pair is the one to rank on: the benchmark is scaled
        # DOWN with T-bills to the rule's own volatility, never levered up, so a
        # rule that simply held less of a rising market cannot win on exposure.
        "cm_excess_cagr": num(getattr(r, "cashmatch_excess_cagr", None), 4),
        "cm_bench_cagr": num(getattr(r, "cashmatch_bench_cagr", None), 4),
        "cm_ratio": num(getattr(r, "cashmatch_ratio", None), 2),
        "sharpe": num(getattr(r, "sharpe", None)),
        # The PER-FOLD pair is what the leaderboard shows and what the Standard
        # judges — `config.EDGE_STANDARD` defines both criteria that way. The
        # pooled difference and the block bootstrap ride along for the tooltip:
        # they are looser, and putting either under the header `t` beside a
        # verdict computed from the other is the two-statistics-one-name bug this
        # whole change exists to remove.
        "dsharpe": num(getattr(r, "fold_dsharpe", None)),
        "t": num(getattr(r, "fold_t", None), 2),
        "n_folds": int(getattr(r, "n_folds_scored", 0) or 0),
        "dsharpe_pooled": num(getattr(r, "dsharpe", None)),
        "boot_t": num(getattr(r, "boot_t", None), 2),
        "dsr": num(getattr(r, "dsr", None), 3),
        "dsr_pass": bool(getattr(r, "dsr_pass", False)),
        "vol": num(getattr(r, "vol", None), 3),
        "dd": num((getattr(r, "dd", None) or 0) * 100.0, 1),
        "exposure": num(getattr(r, "exposure", None), 3),
        "alpha_t": num(getattr(r, "alpha_vs_bh_t", None), 2),
        # The trade block, pooled across the book's names. These replace the
        # `edge_standard` versions on the leaderboard, which were the MEDIAN
        # ASSET's — a different portfolio from the one every other column now
        # describes. `trades_per_asset` is derived here rather than in the page so
        # the divisor is the book's own name count and not the sheet's universe.
        "sharpe_bench": num(getattr(r, "bench_sharpe", None)),
        "win_rate": num(getattr(r, "win_rate", None), 4),
        "profit_factor": num(getattr(r, "profit_factor", None), 2),
        "n_trades": int(getattr(r, "n_trades", 0) or 0),
        "trades_per_asset": _per_asset(getattr(r, "n_trades", None),
                                       getattr(r, "n_names", None)),
        "avg_win": num(getattr(r, "avg_win", None), 4),
        "avg_loss": num(getattr(r, "avg_loss", None), 4),
        "expectancy": _expectancy(r),
        "n_names": int(getattr(r, "n_names", 0) or 0),
        "years": num(getattr(r, "years", None), 1),
        "roe_ann": num(getattr(r, "roe_ann", None), 4),
        # The two signal-free controls, at book level. `vs_random` is a second
        # pass in `portfolio_wf` over the RANDOM_* books in the same panel, so it
        # is absent from any CSV written without them — a `--rules` shortlist.
        "vs_random": num(getattr(r, "vs_random", None), 3),
        "vs_constant": num(getattr(r, "vs_constant", None), 3),
        # The verdict, computed on the book by `portfolio_wf._standard` through
        # `metrics.apply_edge_standard` — the same six criteria and the same
        # thresholds the per-asset stage uses, fed the account's own numbers.
        # Shaped like the old `edge` record so `app.edgeCount` reads either.
        "standard": _book_standard(r),
        "headroom": num(getattr(r, "edge_headroom", None), 1),
    }


def _book_bench(r) -> dict:
    """The sheet's buy-and-hold, off any scored row -- it does not depend on the rule."""
    return {
        "wealth": num(getattr(r, "bench_wealth", None), 0),
        "cagr": num(getattr(r, "bench_cagr", None), 4),
        "sharpe": num(getattr(r, "bench_sharpe", None)),
        "vol": num(getattr(r, "bench_vol", None), 3),
        "dd": num((getattr(r, "bench_dd", None) or 0) * 100.0, 1),
        "years": num(getattr(r, "years", None), 1),
        "n_names": int(getattr(r, "n_names", 0) or 0),
        "start": text(getattr(r, "start", None)),
        "end": text(getattr(r, "end", None)),
        "index_wealth": num(getattr(r, "index_wealth", None), 0),
        "index_symbol": text(getattr(r, "index_symbol", None)),
    }


def _book_index(cls: str, tf: str) -> tuple[dict, dict | None]:
    """`(rule -> book record, the sheet's book benchmark)` from `book_<cls>_<tf>.csv`.

    A SECOND, DIFFERENT MEASUREMENT from everything else on the leaderboard, and the two
    must never be read as versions of one number:

    * every `edge_standard` column is a **median across assets**, each name scored over
      its own out-of-sample bars. On us_stocks 1d that median name is in the top 100 for
      11.99 years, so `$10k at equal risk` answers "what did a typical stock do".
    * this is **one account** holding the whole book — ~100 names, equal-weighted,
      rebalanced every bar, point-in-time membership — over the sheet's full
      out-of-sample span. On the same sheet that is 23.6 years.

    Both are honest and they are not comparable, which is why the buy-and-hold figure
    differs by an order of magnitude between them ($26k against $186k-scale) and why the
    columns are labelled with their own span rather than sharing the header's.

    `portfolio_wf.py` was run with `--start` at fold 0's `is_end`, so these bars are the
    same out-of-sample union the standard scored. Without that the book would cover the
    bars the rules were selected on and ranking on it would be ranking on in-sample fit.
    """
    key = (cls, tf)
    if key in _BOOK_CACHE:
        return _BOOK_CACHE[key]
    df = _read(BM / f"book_{cls}_{tf}.csv")
    out: dict[str, dict] = {}
    bench: dict | None = None
    if not df.empty:
        for r in df.itertuples():
            out[str(r.rule)] = _book_record(r)
            if bench is None:
                bench = _book_bench(r)
    _BOOK_CACHE[key] = (out, bench)
    return out, bench


def _edge_index(cls: str, tf: str) -> dict:
    """`rule -> the better of its two sides`, from `edge_standard.csv`.

    Keyed on rule name so it can be joined straight onto a leaderboard row. Where a rule
    was scored both long/flat and long/short, the higher delta-Sharpe wins — that is the
    "short is optional" rule made concrete, and the surviving row records which side it is
    so a reader is never left guessing whether shorting was involved.
    """
    key = (cls, tf)
    if key in _EDGE_CACHE:
        return _EDGE_CACHE[key]
    df = _read(BM / "edge_standard.csv")
    out: dict[str, dict] = {}
    if not df.empty:
        col_tf = "timeframe" if "timeframe" in df.columns else "tf"
        g = df[(df["class"] == cls) & (df[col_tf] == tf)]
        crit = dash_config.bt_config.GATES
        for r in g.itertuples():
            rec = {
                "side": str(r.side), "dsharpe": num(r.edge_dsharpe),
                "t": num(r.edge_t, 2), "vs_random": num(r.edge_vs_random),
                "vs_constant": num(r.edge_vs_constant),
                "wealth": num(r.wealth, 0), "bench_wealth": num(r.bench_wealth, 0),
                "edge_wealth": num(r.edge_wealth, 0),
                "headroom": num(r.edge_headroom, 1),
                "sharpe": num(r.sharpe), "bench_sharpe": num(r.bench_sharpe),
                # Drawdown is stored as a negative fraction and shown as a percentage.
                # Profit factor has no benchmark counterpart on purpose: buy-and-hold
                # holds one position for the whole window, so it has no losing trade to
                # divide by and its "profit factor" would be a category error, not a
                # number worth printing beside the rule's.
                "max_dd": num(r.max_dd * 100.0, 1),
                "bench_max_dd": num(r.bench_max_dd * 100.0, 1),
                "profit_factor": num(r.profit_factor, 2),
                "trades": num(r.trades_per_asset, 0),
                # Ranking metrics, kept separate from the six acceptance criteria above.
                # Sharpe and expectancy say how good a rule looks; STRCWH says whether the
                # sheet can support the claim. Sorting by the second answers a different
                # question from the one a reader scanning a leaderboard is asking.
                #
                # `expectancy` is per TRADE, so it is only readable beside `trades`: a
                # buy-and-hold that opens one position in 23 years shows an expectancy of
                # +894% and is not thereby a good rule. `expectancy_r` divides by the
                # average loss and is the scale-free version, but it explodes on rules with
                # a handful of trades — `MAXINDEX~MININDEX|or` reaches 10.35 on 5 trades.
                "expectancy": num(getattr(r, "expectancy", None), 4),
                "expectancy_r": num(getattr(r, "expectancy_r", None), 3),
                "win_rate": num(getattr(r, "win_rate", None), 3),
                # ROI is what the ACCOUNT earned; ROE is what the money earned while it was
                # actually deployed. They differ by exactly the idle time, and on this
                # project that gap is the whole argument: corr(IR, long_frac) is 0.881 on
                # daily equities, so an account-level ranking is substantially a ranking of
                # who stayed invested longest. `ibs` earns 14.1%/yr on the account and
                # 31.6%/yr on capital at work, at 46% exposure.
                "roi_ann": num(getattr(r, "roi_ann", None), 4),
                "roe_ann": num(getattr(r, "roe_ann", None), 4),
                # `avg_pnl_per_asset` / `avg_bench_pnl_per_asset` stay in the CSV but are
                # deliberately NOT published. They are means across assets, and the money
                # columns here are medians; put the two on one page and the mean reads as a
                # bigger version of the same number rather than a differently-skewed one.
                # `breadth` already answers the concentration question without inviting
                # that comparison.
                "ceiling": num(r.noise_ceiling),
                "passed": int(r.edge_passed), "n": int(r.edge_n),
                "verdict": str(r.edge_verdict), "powered": bool(r.edge_powered),
                "exposure": num(r.exposure, 3),
                "gates": [bool(getattr(r, f"edge_gate_{c['key']}")) for c in crit],
            }
            # `unrankable` means the row cannot support a ratio at all — near-zero
            # exposure, or scored on too few folds. Excluded here rather than shown with a
            # bad score, because a strategy that never trades is not a bad strategy.
            if str(r.edge_verdict) == "unrankable":
                continue
            prev = out.get(str(r.rule))
            # Ties go to LONG, and they decide 115 of 534 (rule, sheet) pairs — degenerate
            # rows where both sides carry one sentinel `edge_dsharpe`, plus a few that
            # differ below the third decimal this rounds to. Positional order was picking
            # those, which is not a decision and is not stable across a re-sort.
            # `riskmatch_wf.endorsed_sides` breaks them the same way, deliberately: it is
            # the same question and two answers is what put a long/flat curve under a
            # short-side row.
            #
            # `or -9` was also wrong on its own terms — a genuine dsharpe of exactly 0.0 is
            # falsy, so it was read as -9 and lost to anything.
            a = rec["dsharpe"] if rec["dsharpe"] is not None else -9e99
            b = prev["dsharpe"] if prev and prev["dsharpe"] is not None else -9e99
            if prev is None or a > b or (a == b and rec["side"] == "long"):
                out[str(r.rule)] = rec
    _EDGE_CACHE[key] = out
    return out


def leaderboard_entry(r, per: dict, per_stats: dict | None = None) -> dict:
    """One row of the merged leaderboard, singles and pairs on identical keys.

    `kind` is the only thing that separates them and it exists for two honest reasons: a
    pair has an operator worth naming, and it has no asset-by-asset breakdown — `combo_wf.py`
    records leg-correlation diagnostics instead of per-symbol rows — so its detail page has
    to say so rather than render an empty table.
    """
    kind = str(getattr(r, "kind", "single"))
    st = (per_stats or {}).get(str(r.rule)) or {}
    yrs = float(getattr(r, "years", 0) or 0)
    # Median asset where we have per-asset rows; otherwise NOTHING. The old path annualised
    # `net_return_pct` — a mean of per-asset totals — and printed $9.97e16 as if it were a
    # result. A blank is a true statement about a figure this sheet cannot support; a
    # number that large is a false one, and it is the kind this repo has shipped before.
    if st.get("net_pct") is not None:
        net, bh = num(st["net_pct"], 1), num(st.get("bh_pct"), 1)
        yrs = float(st.get("years") or yrs)
        basis = "median"
    else:
        net = bh = None
        basis = "none"
    net_cagr, bh_cagr = cagr(net, yrs), cagr(bh, yrs)
    row = {
        "kind": kind, "rule": str(r.rule),
        # A single has no operator and a pair has no `wf_mode`, so after the concat each
        # carries the other's column as a float NaN that would print as the literal "nan".
        "op": text(getattr(r, "op", None)),
        "net_cagr": net_cagr, "bh_cagr": bh_cagr,
        "cagr_gap": None if net_cagr is None or bh_cagr is None
                    else round(net_cagr - bh_cagr, 2),
        "ir_net": num(r.ir_net), "ir_hit_rate": num(r.ir_hit_rate, 2),
        "t_stat": num(r.t_stat, 2), "turnover": num(getattr(r, "turnover_per_year", None), 1),
        # Emitted by both sweeps as of this change; `walkforward.py` did not carry it while
        # the singles had a list of their own, which is exactly why they could not be merged.
        "long_frac": num(getattr(r, "long_frac", None), 3),
        "short_frac": num(getattr(r, "short_frac", None), 3),
        "exposure": num(getattr(r, "exposure", None), 3),
        "net_pct": net, "bh_pct": bh,
        "excess_pct": num(getattr(r, "excess_return_pct", None), 1),
        # The retired four, kept as a DIAGNOSTIC strip and named so. The verdict a reader
        # should act on is the edge standard, which is a different computation on a
        # different metric and lives in `payload["edge"]` — a leaderboard row cannot carry
        # it, because `wf_summary_*` has none of the inputs it needs.
        "legacy": [int(bool(getattr(r, f"legacy_gate_{k}", False)))
                   for k in ("ir", "breadth", "headroom", "t")],
        # The verdict. None where the standard has not scored this rule.
        "edge": getattr(r, "edge_rec", None),
        # The BOOK: one account holding the whole universe, not the median asset. Its own
        # span, its own benchmark — see `_book_index`. None where no book run covers it.
        "book": getattr(r, "book_rec", None),
        "wf_mode": text(getattr(r, "wf_mode", None)) or "fixed",
        "per_asset": per.get(str(r.rule), []) if kind != "pair" else [],
        # Breadth over the FULL universe, and `per_asset` is now that same full list. It
        # still comes off `_asset_stats` rather than being counted on the site, because the
        # denominator there is the names that were *scored* — a rule with no per-asset
        # source shows "—" instead of "0 / 751", which reads as a measured zero.
        "asset_n": st.get("n"), "asset_pos": st.get("pos"),
        # Names carrying no total return on one side of the comparison. They are still in
        # the table, printing em-dashes at the bottom of the sort; this says how many.
        "asset_unranked": st.get("unranked"),
        "pnl_basis": basis,
    }
    return _drop_offside_diagnostics(row)


# Columns sourced from `wf_summary_*` / `strat_summary_*` / `cwf_summary_*`. NONE of those
# files has a `side` column — they are long/flat, always and only.
_LONGFLAT_ONLY = ("net_cagr", "cagr_gap", "ir_net", "ir_hit_rate", "t_stat", "turnover",
                  "long_frac", "short_frac", "exposure", "net_pct", "excess_pct", "legacy")


def _drop_offside_diagnostics(row: dict) -> dict:
    """Blank the long/flat diagnostics on a row whose VERDICT was scored long/short.

    A leaderboard row is assembled from two sources that do not agree on which strategy
    they describe. `Side`, `Sharpe`, `t`, `Max DD`, `Trades` and `Standard` come from
    `edge_standard.csv` on the endorsed side; `IR`, `Long %`, `CAGR` and `$10k became`
    come from the walk-forward sheets, which have no `side` column at all. Where the
    endorsed side is short, one printed row was therefore half one strategy and half a
    different one — a `Long %` of 44% sitting beside a verdict computed on something in
    the market on every bar.

    Nothing here can repair that: the short-side IR and CAGR were never computed by any
    stage, so there is no value to substitute. Printing an em-dash is the only honest
    option, and it is strictly better than printing a confident wrong number. The detail
    page's per-asset table and its stored curve already follow the endorsed side, so this
    also stops the top of that page contradicting the rest of it.

    `bh_cagr` / `bh_pct` survive deliberately — buy-and-hold has no side.
    """
    e = row.get("edge") or {}
    if e.get("side") != "short":
        return row
    for k in _LONGFLAT_ONLY:
        row[k] = None
    row["diag_side"] = "long"      # what the blanked columns WOULD have described
    row["diag_missing"] = True
    return row


def pair_frame(cls: str, tf: str, scen: str) -> pd.DataFrame:
    """Walk-forward pairs from `combo_wf.py`, filtered the same way the singles are.

    Read the exposure column before the IR column. On equities a pair's IR is very largely
    a measure of how much of the time it is invested: `corr(IR, long_frac)` is +0.88 on 1d
    stocks, the `or` operator wins because it is the one that spends the most time in the
    market, and `MININDEX~MAXINDEX|or` is long 100% of the time, which is to say it *is*
    buy-and-hold. IR against buy-and-hold approaches 0 from below as a rule approaches
    always-long, so pairs filling the top of a merged list is the metric behaving correctly,
    not an edge — and it is why `long_frac` sits beside the IR on every row of the table.
    """
    p = BM / f"cwf_summary_{cls}_{tf}.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    h = df[(df.scenario == scen) & df.rankable & ~df.is_baseline]
    return drop_selection_rows(h)


def catalog_frame(cls: str, tf: str, scen: str) -> pd.DataFrame:
    """The published strategies from `strat_wf.py`, so they appear on the same leaderboard.

    Without this the page was missing the best row on it. `ibs` scores the highest
    delta-Sharpe on us_stocks 1d of anything scored, and it is a `strategies/catalog.py`
    entry — it lives in `strat_summary_*`, not in the TA-Lib sweep, so a leaderboard built
    only from `wf_summary` + `cwf_summary` could never show it.

    Only the published-parameter rows are taken. The grid cells (`ibs@buy=0.1`) are a
    parameter search and belong to the multiplicity count, not to a list a reader picks
    from; `WFO[...]` and `IS#1` are the act of choosing, which is a different question.
    """
    p = BM / f"strat_summary_{cls}_{tf}.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    if "wf_mode" not in df.columns:
        return pd.DataFrame()
    # Fall back to whatever scenario the sheet ACTUALLY has.
    #
    # A sheet written while 1d/4h were collapsed to `gross` contains only `gross`, and
    # once the fee grid was restored `scen` became `retail` — so this filter matched zero
    # rows and returned empty. That does not fail: it silently drops all 31 published
    # strategies from the leaderboard, `ibs` among them, and the page just shows 385 rows
    # where it showed 416. The count is the only visible symptom.
    present = set(df.scenario.unique())
    use = scen if scen in present else (sorted(present)[0] if present else scen)
    if use != scen:
        print(f"  note: {p.name} has no {scen!r} rows; using {use!r} "
              f"(sheet predates the fee-grid change)")
    h = df[(df.scenario == use) & df.rankable & (df.wf_mode == "published")]
    return drop_selection_rows(h)


def copy_curves(shown: dict | None = None) -> dict:
    """Publish `book_curves_*.json` beside the site, one file per sheet.

    **These are the series `portfolio_wf.py` scored**, not a redraw of them. The chart used
    to come from `curves.py`, which built its own equal-weight portfolio and disagreed with
    the book columns on the same row — $270,661 against $308,442 for `ibs` on us_stocks 1d,
    because that stage paid no interest on idle cash, ignored point-in-time membership and
    measured Sharpe raw instead of over the bill rate. The chart is now the row.

    Not inlined into `data.js`: every visitor would parse all six sheets before the
    leaderboard could render, to draw a chart most of them never open. The detail view
    fetches its sheet on demand instead, so the landing page stays small.

    `shown` maps `<key>_<tf>` to the rules that sheet actually ships, and the published
    file is cut to them. The source keeps every rule the book run scored.
    """
    out = WEB / "curves"
    out.mkdir(exist_ok=True)
    index = {}
    for key, cls, _label, _u in GROUPS:
        for tf in TIMEFRAMES:
            src = BM / f"book_curves_{cls}_{tf}.json"
            if not src.exists():
                continue
            k = f"{key}_{tf}"
            all_rules = json.loads(src.read_text(encoding="utf-8"))
            keep = _reachable(all_rules, shown.get(k) if shown else None)
            dst = out / f"{k}.json"
            dst.write_text(json.dumps(keep, separators=(",", ":")), encoding="utf-8")
            index[k] = {"file": f"curves/{k}.json", "bytes": dst.stat().st_size,
                        "rules": list(keep), "n_scored": len(all_rules)}
    return index


def _reachable(all_rules: dict, shown: set | None) -> dict:
    """The curves a reader can open, out of every curve the book run wrote.

    `None` means "no row list available" — publish everything rather than silently
    shipping an empty file, because an unfiltered chart is a waste of bytes and a missing
    one is a broken page.
    """
    if not shown:
        return all_rules
    return {r: c for r, c in all_rules.items() if r in shown}


def _curves_index_only(shown: dict | None = None) -> dict:
    """The same index `copy_curves` returns, without writing anything into `web/`.

    The single-file build embeds the curve JSONs directly, so it wants the index but has
    no use for the copies -- and must not touch the served site's files as a side effect
    of building something else. It therefore embeds whatever a previous `--serve` build
    published, which is why `bytes` is read from `web/` when that file exists.
    """
    index = {}
    for key, cls, _label, _u in GROUPS:
        for tf in TIMEFRAMES:
            src = BM / f"book_curves_{cls}_{tf}.json"
            if not src.exists():
                continue
            k = f"{key}_{tf}"
            all_rules = json.loads(src.read_text(encoding="utf-8"))
            keep = _reachable(all_rules, shown.get(k) if shown else None)
            published = WEB / "curves" / f"{k}.json"
            index[k] = {"file": f"curves/{k}.json",
                        "bytes": (published.stat().st_size if published.exists()
                                  else src.stat().st_size),
                        "rules": list(keep), "n_scored": len(all_rules)}
    return index


# Five groups, because they answer five different questions and lumping the desk into one
# list hides that. The mega-caps are the only equity leg that can confirm the research (same
# universe it was ranked on); SPY/SOXL/TQQQ are a transfer test onto instruments the study
# never held; the ETF, crypto and commodity legs each have their own sheet and cost grid.
#
# The symbol lists come from `paper_config`, which is what the node itself reads, rather
# than from `bt_config`. That was the same list until the research universe grew on
# 2026-08-09 — after which `bt_config.US_STOCKS` became 751 point-in-time S&P names, and
# then on 2026-08-12 the 216-name point-in-time top 100 — and this group would have claimed
# every one of them as a mega-cap the desk trades.
#
# Note the mega-cap leg is no longer a like-for-like forward test of the equity research:
# the rules are now ranked on the top-100 universe, and these 20 names are a subset of it
# chosen for being large today. Confirmation on them is weaker evidence than it was.
sys.path.insert(0, str(dash_config.PAPER))
import paper_config                                            # noqa: E402

PAPER_GROUPS = [
    {"key": "book", "label": "One book, the whole class",
     "note": "A single account holding every name in the class at once — $100,000 split "
             "equally, a name the rule is out of holding cash, and the slice growing with "
             "the book. This is the shape the research scored: `ir_net` is a mean across "
             "the class, so a one-symbol forward test measures something the backtest "
             "never reported.",
     "symbols": []},
    {"key": "megacap", "label": "20 US mega-caps",
     "note": "The same universe the equity rules were ranked on — the only like-for-like "
             "forward test here.",
     "symbols": list(paper_config.RESEARCH_EQUITIES)},
    {"key": "etf", "label": "SPY · SOXL · TQQQ",
     "note": "MK's brief. A transfer test: the research never held an ETF, and the two 3x "
             "funds decay against their index in chop. Traded off the EQUITY sheet, which "
             "is the transfer: these rules were never ranked on a fund.",
     "symbols": list(paper_config.BRIEF_EQUITIES)},
    {"key": "us_etfs", "label": "ETF sheet",
     "note": "Ranked on the us_etfs sheet and traded off it — index, small caps, a sector, "
             "duration and gold. Deliberately excludes SPY/SOXL/TQQQ, which are above on "
             "the equity sheet's rules; one instrument under two rule lists would read as "
             "two systems agreeing when it is one asset counted twice.",
     "symbols": list(paper_config.ETF_SYMBOLS)},
    {"key": "crypto", "label": "Top 10 crypto",
     "note": "Ranked on the crypto sheet, which has its own rules and its own cost grid.",
     "symbols": list(paper_config.CRYPTO_SYMBOLS)},
    {"key": "commodities", "label": "Spot metals · WTI",
     "note": "The deepest history in the repo — XAU/USD starts 1979 — and the shallowest "
             "cross-section, five contracts. Read the breadth figures accordingly.",
     "symbols": list(paper_config.COMMODITY_SYMBOLS)},
]

_GROUP_KEYS = {g["key"] for g in PAPER_GROUPS}


def group_of(symbol: str, cls: str) -> str:
    for g in PAPER_GROUPS:
        if symbol in g["symbols"]:
            return g["key"]
    # A symbol outside every leg — a hand-run `--symbols` session, or a record written
    # before the universe changed. Fall back to its own class where that names a group,
    # so it lands somewhere honest rather than being filed under ETFs by default.
    return cls if cls in _GROUP_KEYS else "etf"


def paper_state() -> dict:
    """Whatever the live node has written. Absent until it has actually traded."""
    p = dash_config.PAPER_RESULTS / "paper_state.json"
    if not p.exists():
        return {"strategies": [], "venue": {"name": "Nautilus sandbox",
                                            "balance": 100000, "equity": 100000},
                "feed": {"source": "Twelve Data", "plan": "pro",
                         "status": "not running", "last_bar": "—"},
                "timeframes": list(paper_config.MEMBER_TIMEFRAMES)}
    state = json.loads(p.read_text(encoding="utf-8"))
    # Every timeframe the desk CAN run, not the ones something happens to be deployed on
    # today -- the same rule the class strip follows, and for the same reason: a filter
    # derived only from the live rows cannot show you that nothing is running at 1h.
    state["timeframes"] = list(paper_config.MEMBER_TIMEFRAMES)
    # Tag membership here rather than in the node: it is a presentation concern, and doing
    # it at build time means the grouping can change without restarting a running desk.
    for s in state.get("strategies", []):
        # A BOOK holds a whole class, so it has no symbol to file under and
        # `group_of` would drop it through to the `etf` default — which is labelled
        # "SPY · SOXL · TQQQ". A $100,000 book over the top 100 appeared on the board as
        # one asset in the leveraged-ETF group, which is not a small mislabel.
        s["group"] = ("book" if s.get("kind") == "book"
                      else group_of(s.get("symbol", ""), s.get("cls", "")))
    state["groups"] = [{k: g[k] for k in ("key", "label", "note")} for g in PAPER_GROUPS]
    return state




# ---------------------------------------------------------------------------------------
# Snapshot sections
#
# These came from `build_dashboard_data.py`, which fed the single-file board while
# `build_web_data.py` fed the served site. Two builders over two different subsets of the
# same CSVs meant the two outputs disagreed by a day in practice; they are one payload now.
# ---------------------------------------------------------------------------------------

def _read(path):
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def research_sheets() -> list[dict]:
    """One row per (class, timeframe): best fixed rule, honest IS#1, gates, ceiling."""
    out = []
    meta = _read(BM / "wf_meta.csv")
    for _, m in meta.iterrows():
        tag = f"{m['class']}_{m['timeframe']}"
        s = _read(BM / f"wf_summary_{tag}.csv")
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
            # Legacy 4-gate count, kept as a diagnostic only. The verdict is in `edge`.
            "legacy_cleared": int((h["legacy_passed"] == 4).sum())
            if "legacy_passed" in h else None,
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
        s = _read(dash_config.TOP20_RESULTS / f"etf_wf_summary_{tf}.csv")
        bh = _read(dash_config.TOP20_RESULTS / f"etf_buyhold_{tf}.csv")
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
            # Legacy 4-gate count, kept as a diagnostic only. The verdict is in `edge`.
            "legacy_cleared": int((h["legacy_passed"] == 4).sum())
            if "legacy_passed" in h else None,
            "n_rules": int(len(h)),
            "buyhold": [{"symbol": r["symbol"], "cagr": round(float(r["cagr"]), 4),
                         "sharpe": round(float(r["sharpe"]), 2),
                         "max_drawdown": round(float(r["max_drawdown"]), 4)}
                        for _, r in bh.iterrows()],
        })
    return out


def prereg() -> list[dict]:
    s = _read(BM / "prereg_us_stocks_1d.csv")
    if s.empty:
        return []
    h = s[s.scenario == "retail"]
    return [{"rule": str(r["rule"]), "ir": round(float(r["ir_net"]), 3),
             "prior": str(r.get("prior", "")), "contaminated": bool(r.get("contaminated"))}
            for _, r in h.sort_values("ir_net", ascending=False).iterrows()]


def parity() -> list[dict]:
    s = _read(dash_config.PAPER_RESULTS / "parity_live_1d.csv")
    if s.empty:
        return []
    agg = s.groupby("rule")["min_window"].max().sort_values(ascending=False)
    return [{"rule": str(k), "min_window": int(v) if pd.notna(v) else None}
            for k, v in agg.items()]


def live_prices() -> list[dict]:
    """A price snapshot, taken at build time.

    The only network call in the dashboard, and it is optional -- `--offline` skips it.
    A published single-file build cannot fetch anything at view time, so a stamped
    snapshot is the honest version of "latest price" there.
    """
    # `td_live` belongs to the paper desk. Imported here rather than at module scope, and
    # with the path insert kept local, so the backtest sections build with no network path
    # and `--offline` never touches the desk's directory at all.
    import sys
    if str(dash_config.PAPER) not in sys.path:
        sys.path.insert(0, str(dash_config.PAPER))
    import td_live

    rows = []
    for sym in BRIEF_EQUITIES + ["BTC/USD", "ETH/USD"]:
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


def strategy_logic() -> dict:
    """What each strategy actually DOES, keyed by the name the leaderboard shows.

    Two sources, because the board merges two populations. The published catalogue carries
    a hand-written `LOGIC` block per file in `strategies/published/*.py`; the 231 TA-Lib
    rules have no prose anywhere, so `backtest engine/rule_logic.py` derives one from the
    indicator family and flags the ~36 that fall back to a generic entry rather than a
    documented rule.

    A leg of a pair resolves through the same table: `MAXINDEX~MININDEX|or` is two rules
    the reader can look up individually, which is the only way that row is interpretable.
    """
    out: dict[str, dict] = {}
    try:
        from strategies.registry import CATALOG
        for name, s in CATALOG.items():
            if s.logic or s.note:
                out[name] = {"logic": s.logic, "note": s.note, "rule": s.rule,
                             "source": s.source, "family": s.family, "kind": "published"}
    except Exception as e:                                     # noqa: BLE001
        print(f"  note: published logic unavailable ({e})")
    try:
        import rule_logic
        from strategies.talib_signals import get_all_indicator_names
        for name in get_all_indicator_names():
            if name in out:
                continue
            text = rule_logic.explain(name)
            if text:
                out[name] = {"logic": text, "family": rule_logic.family_of(name),
                             "kind": "talib"}
    except Exception as e:                                     # noqa: BLE001
        print(f"  note: TA-Lib logic unavailable ({e})")
    print(f"  strategy logic: {len(out)} entries")
    return out


# ---------------------------------------------------------------------------------------
# The converted strategies -- the SECOND board on the backtest page
#
# Thirteen third-party rules (eight TradingView Pine scripts, four freqtrade strategies and
# one pair of notebooks) arrived as `Strategies to convert.zip` on 2026-08-18 and were run
# through the same machinery as everything else. They get their own board rather than rows
# on the house one, for reasons about measurement rather than tidiness:
#
# * **They were tested on timeframes the house board has no sheets for.** The research
#   catalogue runs 1d and 4h; these were written for minute charts, so they were scored at
#   1m/2m/3m/5m as well. Two different timeframe axes cannot share one filter strip.
# * **They carry facets no house rule has** -- a short side that REVERSES rather than
#   selling to cash, a Heikin-Ashi signal variant, and an overnight-flat variant. Those
#   would be three columns empty on every house row.
# * **They were pre-registered as their own family** (1,247 cells in
#   `data/reference/trials.csv`), so the trial count that deflates them is theirs. Mixing
#   the two populations would deflate each against the other's search.
#
# Everything else is deliberately identical. The same `portfolio_wf.py` stage wrote these
# sheets, `_book_record` reads them, and the ranking key is the house key -- the standard's
# own count, ties on the book's risk-matched excess CAGR.
# ---------------------------------------------------------------------------------------

# Timeframe order for this board, coarsest first, so the sheet with the most history and
# the fewest cost artefacts is the one a reader lands on.
_CONV_TF_ORDER = ["1d", "5m", "3m", "2m", "1m"]

# Provenance, from `strategies/CONVERSIONS.md`. Kept here rather than parsed out of that
# file: it is prose with tables in it, and a regex over somebody's documentation is a
# silent failure waiting for the day they reformat it.
CONVERSIONS = [
    ("bar_updn", "TradingView sample", "pine",
     "A three-bar up/down pattern — TradingView's own BarUpDn demo."),
    ("pivot_center", "Pine script", "pine",
     "The centre line between two confirmed pivots, crossed one bar late."),
    ("range_filter", "Pine — DonovanWall", "pine",
     "A ratcheting trend line that only moves when price pushes far enough."),
    ("range_filter_macd", "Pine script", "pine",
     "The same range filter, with a slow MACD gate on entry."),
    ("ema_cross_sniper", "Pine — TradersPost", "pine",
     "An 8-over-21 exponential moving average cross."),
    ("bb_outside_in", "Pine script", "pine",
     "Price pierces a Bollinger band, then re-crosses the midline."),
    ("ssl_hybrid", "Pine script", "pine",
     "An SSL channel with a Keltner baseline and two QQE lines."),
    ("lorentzian_knn", "Pine — jdehorty", "pine",
     "A nearest-neighbour classifier over the shape of past bars."),
    ("heikin_reversal", "freqtrade", "freqtrade",
     "The first green synthetic candle after a bearish stretch."),
    ("sma_fan_dip", "freqtrade", "freqtrade",
     "A 5/10/25/60 moving-average fan break, bought at a discount."),
    ("vwma_offset_dip", "freqtrade", "freqtrade",
     "Percentage offsets under a volume-weighted average and two EMAs."),
    ("ema_fan_align", "freqtrade", "freqtrade",
     "A seven-deep EMA fan that is still widening."),
    ("renko_delta", "two notebooks", "notebook",
     "Percentage renko bricks, counted in runs."),
]
CONVERSION_NAMES = {c[0] for c in CONVERSIONS}
# The eight that came from Pine, and therefore the eight that HAVE a short side.
# `strategy.entry(short)` reverses the position rather than closing it to cash, so those
# eight ship as long/short with an `allow_short=0` cell appended. The other five come from
# freqtrade and notebooks, which sell to cash and have no short leg at all -- for them the
# absence of `allow_short=0` on the label means nothing, and reading it as "reverses" put a
# chip on five rows that do not and cannot.
CONVERSION_REVERSING = {c[0] for c in CONVERSIONS if c[2] == "pine"}

# The signal-free controls. They are NOT ranked rows here, exactly as on the house board:
# `BUYHOLD` is spliced in as the benchmark line and the random books are what the
# `vs random` column is measured against, so listing them as candidates would put the bar
# into the ranking it defines. They stay in the CSVs.
_CONV_BENCH = "BUYHOLD"
_CONV_CONTROLS = ("BUYHOLD", "RANDOM_25", "RANDOM_50", "RANDOM_75", "RANDOM_90",
                  "ALWAYS_FLAT", "ALWAYS_LONG")

# `run_convert_curves.sh` re-scores a whole (class, timeframe) in one run and writes both
# the sheet and its curves, so where that file exists it is the ONE source for that cell.
# It has to be: `--curves` writes one JSON per sheet, so three runs over the same cell
# would each overwrite the other two's curves and the last one would win silently.
_CONV_BOOK = "convert_book_{cls}_{tf}.csv"

# The ad-hoc daily runs that came first, kept as the fallback for any cell
# `run_convert_curves.sh` has not reached. Order is load-bearing where two files carry the
# same label: the first wins, and the first is always the sheet that ran the whole family
# rather than a follow-up over a subset of it.
_CONV_1D = {
    ("us_stocks", "1d"): ["portfolio.csv", "convert_us_longflat.csv", "convert_us_lf2.csv"],
    ("us_etfs", "1d"): ["convert_etf_lf.csv"],
    ("crypto", "1d"): ["convert_crypto_longflat.csv", "convert_crypto_lf2.csv"],
    ("commodities", "1d"): ["convert_commodities_book.csv"],
    ("cme_futures", "1d"): ["convert_cme_futures_book.csv"],
}

# Re-runs of a sheet under one changed assumption. They are NOT merged into the ranking --
# a rule at Coinbase fees and the same rule at Binance fees is one strategy with two prices,
# not two candidates — so they render as their own small tables under the board, which is
# where a sensitivity belongs.
_CONV_CHECKS = [
    (("crypto", "1d"), "Trading venue",
     "The same rules re-priced on three real exchange fee schedules. Two of the three stop "
     "beating the benchmark on the move alone, so which venue you would actually trade at "
     "decides whether the result exists.",
     [("Binance", "convert_crypto_fee_binance.csv"),
      ("Coinbase", "convert_crypto_fee_coinbase.csv"),
      ("Kraken", "convert_crypto_fee_kraken.csv")]),
    (("crypto", "1d"), "Fill timing",
     "As published, the signal is computed from a bar's own close and filled at that same "
     "close — a price nobody had yet. These re-runs remove the assumption: fill at the "
     "next open, and delay the whole thing by a bar.",
     [("Next open", "convert_crypto_fill_open.csv"),
      ("One bar later", "convert_crypto_fill_close_lag.csv")]),
    (("crypto", "1d"), "Universe",
     "The board holds the 20 pairs that pass the tradability screen. This is the same run "
     "over all 34 the vendor serves: if a result exists only on the screened set, the "
     "screen is what produced it.",
     [("All 34 pairs", "convert_crypto_all34.csv")]),
    (("us_stocks", "3m"), "Fill timing",
     "The same rules at 3-minute bars, filled at the next open and delayed by a bar.",
     [("Next open", "convert_ha_fill_open_us_stocks_3m.csv"),
      ("One bar later", "convert_ha_fill_close_lag_us_stocks_3m.csv")]),
]

# `convert_ha_<class>_<tf>[_flat][_knn].csv`. Globbed rather than listed, so a sheet that
# finishes overnight joins the board at the next build with no edit here.
_CONV_HA_RE = re.compile(
    r"^convert_ha_(?P<cls>[a-z_]+?)_(?P<tf>\d+m)(?P<tail>(?:_flat|_knn)*)\.csv$")


def _conv_facets(rule: str) -> dict:
    """Split a rule label into the strategy and the things done to it.

    `ha:chart:ssl_hybrid@allow_short=0` is one strategy wearing three facets, not a fourth
    strategy. A board that printed the raw label would rank the same rule against itself
    four times under four names nobody can line up at a glance.
    """
    label = str(rule)
    base, _, param = label.partition("@")
    overlays = []
    while ":" in base:
        head, _, base = base.partition(":")
        overlays.append(head)
    can_short = base in CONVERSION_REVERSING
    return {
        "base": base,
        # As published, the eight Pine rules REVERSE on a short signal rather than selling
        # to cash. `allow_short=0` is the same signal with that half removed, and on this
        # repo's benchmark that single property dominates everything else the rule does --
        # so it is a chip on the row and is never folded into the name.
        #
        # `short_off` and `short` are NOT each other's negation, and collapsing them was a
        # bug on the page: a freqtrade rule has no short leg to switch off, so an unmarked
        # row would have meant two different things.
        "short": can_short and "allow_short=0" not in param,
        "short_off": can_short and "allow_short=0" in param,
        # The signal on synthetic candles; the money still settles on real closes. That is
        # the one thing separating this from an HA backtest run on a chart platform.
        "ha": "ha" in overlays,
        # Window lengths read as Pine BAR COUNTS rather than as calendar days.
        "chart": "chart" in overlays,
    }


def _conv_rows(files: list[str], eod: str | None = None) -> tuple[dict, dict | None]:
    """Merge one or more book sheets into `key -> row`, first file winning."""
    rows: dict[str, dict] = {}
    bench: dict | None = None
    for name in files:
        df = _read(BM / name)
        if df.empty or "rule" not in df.columns:
            continue
        first = None
        for r in df.itertuples():
            if first is None:
                first = r
            label = str(r.rule)
            if label in _CONV_CONTROLS:
                if label == _CONV_BENCH and bench is None:
                    bench = _book_bench(r)
                continue
            f = _conv_facets(label)
            if f["base"] not in CONVERSION_NAMES:
                continue
            key = f"{label}|{eod or ''}"
            if key not in rows:
                rows[key] = {"rule": label, "source": name, "eod": eod,
                             "book": _book_record(r), **f}
        # Every scored row carries the sheet's benchmark, so a sheet whose controls were
        # not re-run still has one. Leaving it null would blank the line the whole table is
        # read against.
        if bench is None and first is not None:
            bench = _book_bench(first)
    return rows, bench


def _conv_rank(rows: list[dict]) -> list[dict]:
    """Ranked on the book's risk-matched excess CAGR, and on nothing else.

    The house board ranks on the six-criteria count with this as the tiebreak. That key is
    wrong for this family and was dropped on request: **almost every sheet here is
    underpowered**, so the count is mostly reporting how much history a class has rather
    than how good a rule is. The crypto daily sheet has 6 folds and the minute sheets have
    about 4, against the 20 the thresholds were calibrated on -- so the tiers separated
    rows on evidence none of them had, and buried a rule earning +30 pp/yr under one
    earning +9 because the second happened to clear a gate the first missed.

    What is left is the question the board is actually for: at equal risk, how much did
    this rule make above holding the same universe. `edge_standard`'s verdict is still
    computed and still on every row in the payload; it is simply not rendered and not
    ranked on.
    """
    def key(e):
        ex = e["book"].get("cm_excess_cagr")
        return -(ex if ex is not None else float("-inf"))
    return sorted(rows, key=key)


def _conv_check_table(files: list[tuple[str, str]]) -> dict | None:
    """A sensitivity as a table: one row per rule, one column per variant of the run."""
    cols: list[str] = []
    per: dict[str, dict] = {}
    for label, name in files:
        df = _read(BM / name)
        if df.empty or "rule" not in df.columns:
            continue
        cols.append(label)
        for r in df.itertuples():
            rule = str(r.rule)
            if rule in _CONV_CONTROLS:
                continue
            if _conv_facets(rule)["base"] not in CONVERSION_NAMES:
                continue
            per.setdefault(rule, {})[label] = {
                "excess": num(getattr(r, "cashmatch_excess_cagr", None), 4),
                "t": num(getattr(r, "boot_t", None), 2)}
    if not cols or not per:
        return None
    rows = []
    for rule, cells in per.items():
        f = _conv_facets(rule)
        rows.append({"rule": rule, "base": f["base"], "short": f["short"],
                     "short_off": f["short_off"], "ha": f["ha"],
                     "cells": [cells.get(c) for c in cols]})
    # Ordered on the first variant, which is the one the prose above the table names first.
    def first_excess(e):
        c = e["cells"][0] if e["cells"] else None
        return c["excess"] if c and c["excess"] is not None else float("-inf")
    rows.sort(key=lambda e: -first_excess(e))
    return {"cols": cols, "rows": rows}


def _conv_selection() -> dict | None:
    """What choosing a rule each year, on prior data only, was worth.

    The one thing no leaderboard can show, because a leaderboard is by construction the
    view from the end. `convert_crypto_wf.csv` is `portfolio_wf --walkforward`: re-pick the
    best of the family on each in-sample window, trade it through the next, and score the
    SELECTION rather than the winner.
    """
    df = _read(BM / "convert_crypto_wf.csv")
    if df.empty:
        return None
    r = df.iloc[0]
    picks = _read(BM / "pwf_picks_crypto_1d.csv")
    return {
        "is1_excess": num(r.get("is1_excess_cagr"), 4),
        "is1_t": num(r.get("is1_boot_t"), 2),
        "best_fixed": text(r.get("best_fixed_rule")),
        "best_fixed_excess": num(r.get("best_fixed_excess_cagr"), 4),
        "selection_cost": num(r.get("selection_cost"), 4),
        "n_folds": int(r.get("n_folds") or 0),
        "n_switches": int(r.get("n_switches") or 0),
        "n_candidates": int(r.get("n_candidates") or 0),
        "verdict": text(r.get("edge_verdict")),
        "picks": [] if picks.empty else [
            {"fold": text(p.fold), "rule": text(p.rule),
             "is_excess": num(getattr(p, "is_excess_cagr", None), 4)}
            for p in picks.itertuples()],
    }


_CONV_CURVE_CACHE: dict[tuple, set] = {}


def _conv_curve_rules(cls: str, tf: str) -> set:
    """Which rules `run_convert_curves.sh` has written an equity series for."""
    key = (cls, tf)
    if key not in _CONV_CURVE_CACHE:
        p = BM / f"convert_curves_{cls}_{tf}.json"
        try:
            _CONV_CURVE_CACHE[key] = set(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _CONV_CURVE_CACHE[key] = set()
    return _CONV_CURVE_CACHE[key]


def copy_conv_curves(shown: dict, write: bool = True) -> dict:
    """Publish the converted board's curve files, in their own `conv_` namespace.

    Separate from `copy_curves` and separately keyed, because the file name over there is
    derived from (group, timeframe) alone -- `stocks_1d` -- and this board scores a
    different rule set on exactly those cells. One namespace would have meant the two
    boards fighting over one file, which is the same collision `--curves-out` exists to
    stop one level up.
    """
    out = WEB / "curves"
    if write:
        out.mkdir(exist_ok=True)
    index = {}
    for key, cls, _label, _u in GROUPS:
        for tf in _CONV_TF_ORDER:
            src = BM / f"convert_curves_{cls}_{tf}.json"
            if not src.exists():
                continue
            k = f"conv_{key}_{tf}"
            all_rules = json.loads(src.read_text(encoding="utf-8"))
            keep = _reachable(all_rules, shown.get(k))
            dst = out / f"{k}.json"
            if write:
                dst.write_text(json.dumps(keep, separators=(",", ":")), encoding="utf-8")
            index[k] = {"file": f"curves/{k}.json",
                        "bytes": dst.stat().st_size if write and dst.exists()
                                 else len(json.dumps(keep)),
                        "rules": list(keep), "n_scored": len(all_rules)}
    return index


def conversion_sheets() -> dict:
    """The whole second board: a sheet per (class, timeframe), plus the checks."""
    found: dict[tuple, list[dict]] = {}
    benches: dict[tuple, dict] = {}
    sources: dict[tuple, list[str]] = {}

    def add(cls, tf, files, eod=None):
        rows, bench = _conv_rows(files, eod)
        if not rows:
            return
        found.setdefault((cls, tf), []).extend(rows.values())
        sources.setdefault((cls, tf), []).extend(files)
        if bench and (cls, tf) not in benches:
            benches[(cls, tf)] = bench

    # Every cell the rebuilt run has reached, first: one file, one curve set, one run.
    rebuilt = set()
    for path in sorted(BM.glob("convert_book_*.csv")):
        df = _read(path)
        if df.empty or "class" not in df.columns:
            continue
        cls, tf = str(df["class"].iloc[0]), str(df["tf"].iloc[0])
        add(cls, tf, [path.name])
        rebuilt.add((cls, tf))

    for (cls, tf), files in _CONV_1D.items():
        if (cls, tf) in rebuilt:
            continue
        add(cls, tf, [f for f in files if (BM / f).exists()])

    for path in sorted(BM.glob("convert_ha_*.csv")):
        m = _CONV_HA_RE.match(path.name)
        if not m:                    # the fill / grid / pilot re-runs, handled as checks
            continue
        cls, tf = m.group("cls"), m.group("tf")
        if (cls, tf) in rebuilt:
            continue
        add(cls, tf, [path.name],
            eod="flat" if "_flat" in m.group("tail") else "hold")

    checks: dict[tuple, list[dict]] = {}
    for key, title, note, files in _CONV_CHECKS:
        tbl = _conv_check_table([(l, n) for l, n in files if (BM / n).exists()])
        if tbl:
            checks.setdefault(key, []).append({"title": title, "note": note, **tbl})

    sel = _conv_selection()
    by_group: dict[str, dict] = {}
    tfs: list[str] = []
    total = beat = strong = 0
    for gkey, cls, label, universe in GROUPS:
        sheets = []
        for tf in _CONV_TF_ORDER:
            rows = found.get((cls, tf))
            if not rows:
                continue
            ranked = _conv_rank(rows)
            bench = benches.get((cls, tf))
            head = ranked[0]["book"]
            # Which rows can draw a chart. Read off the curve file rather than assumed
            # from the sheet: the two are written by one run, but a cell whose re-score
            # has not happened yet has a sheet and no curves, and a row that navigates to
            # an empty chart is worse than one that says there is nothing to draw.
            have = _conv_curve_rules(cls, tf)
            for e in ranked:
                e["curve"] = e["rule"] in have
            n_beat = sum(1 for e in ranked if (e["book"].get("cm_excess_cagr") or 0) > 0)
            total += len(ranked)
            beat += n_beat
            strong += sum(1 for e in ranked
                          if (e["book"].get("cm_excess_cagr") or 0) > 0
                          and (e["book"].get("boot_t") or 0) > 2)
            sheets.append({
                "timeframe": tf, "cls": cls,
                "rows": ranked[:TOP_N],
                "n_rules": len(ranked),
                "n_beat": n_beat,
                "n_flat": sum(1 for e in ranked if e["eod"] == "flat"),
                "book_bench": bench,
                "years": head.get("years"),
                "n_names": head.get("n_names"),
                "ranked_on": "book_cm_excess_cagr",
                "ranked_tiebreak": None,
                "sources": sorted(set(sources.get((cls, tf), []))),
                "checks": checks.get((cls, tf), []),
                # Only the crypto daily sheet has a selection run, and it belongs to that
                # sheet rather than to the board: it prices choosing among THAT family.
                "selection": sel if (cls, tf) == ("crypto", "1d") else None,
            })
        if sheets:
            by_group[gkey] = {"label": label, "cls": cls, "sheets": sheets}
            for s in sheets:
                if s["timeframe"] not in tfs:
                    tfs.append(s["timeframe"])
    return {
        "groups": by_group,
        "timeframes": [t for t in _CONV_TF_ORDER if t in tfs],
        "roster": [{"name": n, "origin": o, "family": fam, "blurb": b,
                    "reverses": n in CONVERSION_REVERSING}
                   for n, o, fam, b in CONVERSIONS],
        "totals": {"cells": total, "beat": beat, "strong": strong,
                   "strategies": len(CONVERSIONS),
                   "sheets": sum(len(g["sheets"]) for g in by_group.values())},
    }


def build(copy_curve_files: bool = True, offline: bool = False) -> dict:
    """The whole payload, for either emitter.

    `copy_curve_files` mirrors the WFO stage's curve JSONs into `web/curves/` and is only
    wanted for the served build. `offline` skips the one network call (the price snapshot),
    which matters when rebuilding without an API key or when Twelve Data is down -- the
    rest of the payload comes off local CSVs and must not be held hostage to that.
    """
    backtest = {}
    for key, cls, label, universe in GROUPS:
        sheets = [s for s in (build_sheet(cls, tf, universe) for tf in TIMEFRAMES) if s]
        if sheets:
            backtest[key] = {"label": label, "n": len(universe),
                             "universe": universe, "sheets": sheets}

    paper = paper_state()
    # Only the rules this payload can actually open. `run_book.sh` curves all ~409 labels
    # per sheet and `results/` keeps every one of them, but a leaderboard ships `TOP_N`
    # rows and a detail page is reachable only from a row -- so publishing the rest put
    # 13x the bytes into `web/curves/` and took the single-file build from 9 MB to 38 MB
    # to carry charts nothing links to. Raise `TOP_N` and they publish themselves; no
    # re-run is needed, because the source files already hold them.
    shown = {f"{key}_{s['timeframe']}": {r["rule"] for r in s["rows"]}
             for key, g in backtest.items() for s in g["sheets"]}
    curves_index = (copy_curves(shown) if copy_curve_files
                    else _curves_index_only(shown))
    # The second board's curves, in their own `conv_` namespace and cut to the rows it
    # ships, exactly like the first board's. Built here rather than inside
    # `conversion_sheets` so both boards' publishing happens in one place and neither can
    # quietly stop writing while the index still claims the files exist.
    conversions = conversion_sheets()
    conv_shown = {f"conv_{key}_{s['timeframe']}": {r["rule"] for r in s["rows"]}
                  for key, g in conversions["groups"].items() for s in g["sheets"]}
    curves_index.update(copy_conv_curves(conv_shown, write=copy_curve_files))
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "feed": paper["feed"], "venue": paper["venue"],
        "strategies": paper["strategies"],
        "paper_groups": paper.get("groups", []),
        # Both filter strips read their options from here rather than carrying a literal
        # pair in the page. `timeframes` is what the RESEARCH has sheets for
        # (`dash_config.TIMEFRAMES`); `paper_timeframes` is what the DESK will accept a
        # registration at, which is the wider list.
        "timeframes": list(TIMEFRAMES),
        "paper_timeframes": paper.get("timeframes", []),
        "research": {
            # The retired four, so the diagnostic strip on each leaderboard row is
            # explicable rather than mysterious.
            "legacy_gates": [
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
            # The four gates above still label every leaderboard row, because that is what
            # the recorded `gates_passed` columns were computed against. The standard below
            # supersedes them for new work and is scored separately — see `edge`.
            "superseded": ("The four gates rank on information ratio against "
                           "buy-and-hold, which compares a rule that is in the market "
                           "part of the time against one that always is. That measures "
                           "capital deployment as much as skill. The edge standard "
                           "replaces it: size to equal risk first, then count the money."),
        },
        # The criteria themselves, for the Method page. The SCORES live on each
        # leaderboard row in `backtest`, not in a section of their own — a separate page
        # meant two places to look for one answer.
        "edge_criteria": [{"k": c["letter"], "name": c["label"],
                           "target": c["target"], "ask": c.get("note", "")}
                          for c in dash_config.bt_config.GATES],
        "backtest": backtest,
        # The second board on the same page. Its own timeframe axis (1d down to
        # 1m), its own trial family, and its own facets -- see `conversion_sheets`
        # for why it is not rows on the first one.
        "conversions": conversions,
        "logic": strategy_logic(),
        "curves": curves_index,
        # The sections the single-file board used to carry alone.
        "summary": {
            "sheets": research_sheets(),
            "gate_power": gate_power(),
            "etf_sheets": etf_sheets(),
            "prereg": prereg(),
            "parity": parity(),
            "prices": [] if offline else live_prices(),
        },
    }
    return payload


def report(payload: dict) -> None:
    """One line per sheet, so a build that quietly lost a sheet is visible."""
    backtest = payload["backtest"]
    for key, g in backtest.items():
        for s in g["sheets"]:
            best = s["rows"][0] if s["rows"] else {}
            # `-` rather than `None` where the top row is scored long/short: those columns
            # are long/flat-only and `_drop_offside_diagnostics` blanks them. Printing the
            # literal "None" made a deliberate blank look like a builder fault.
            d = lambda k, sfx="": ("-" if best.get(k) is None else f"{best[k]}{sfx}")
            print(f"  {key:7s} {s['timeframe']:3s} {s['n_rules']:3d} strategies "
                  f"({s['n_singles']} single + {s['n_pairs']} pair)  "
                  f"{s['years']:5.1f}y  best {best.get('rule','—'):<30} "
                  f"{'[short]' if best.get('diag_missing') else '':<8}"
                  f"IR {d('ir_net'):>7}  long {d('long_frac'):>5}  "
                  f"CAGR {d('net_cagr','%'):>7} vs B&H {d('bh_cagr','%')}")
            print(f"  {'':7s} {'':3s} top {len(s['rows'])} shown, "
                  f"{s['n_shown_pairs']} of them pairs  ·  corr(IR,long) "
                  f"{s['exposure_corr']}  ·  luck threshold +{s['noise_ceiling']}  ·  "
                  f"per-asset rows on best {len(best.get('per_asset', []))}")
    # The second board gets the same treatment: a build that silently drops a sheet --
    # a rename in `results/`, a run that has not finished -- is otherwise invisible until
    # somebody notices a missing tab.
    conv = payload.get("conversions") or {}
    t = conv.get("totals") or {}
    print(f"  conversions: {t.get('strategies', 0)} strategies, {t.get('sheets', 0)} sheets, "
          f"{t.get('cells', 0)} cells, {t.get('beat', 0)} beat B&H, "
          f"{t.get('strong', 0)} of those at t>2")
    for key, g in (conv.get("groups") or {}).items():
        for sh in g["sheets"]:
            best = sh["rows"][0] if sh["rows"] else {}
            bk = best.get("book") or {}
            ex = bk.get("cm_excess_cagr")
            st = (bk.get("standard") or {}).get("passed")
            print(f"  {key:7s} {sh['timeframe']:3s} {sh['n_rules']:3d} cells  "
                  f"{sh['n_beat']} beat  top {best.get('rule', '-'):<44} "
                  f"{'-' if st is None else f'{st}/6'}  "
                  f"vs B&H {'-' if ex is None else f'{ex * 100:+.2f}%/yr'}")
    s = payload["summary"]
    print(f"  paper strategies: {len(payload['strategies'])}")
    print(f"  summary sections: {len(s['sheets'])} sheets, {len(s['etf_sheets'])} ETF, "
          f"{len(s['prereg'])} prereg, {len(s['parity'])} parity, "
          f"{len(s['prices'])} prices")


if __name__ == "__main__":
    raise SystemExit("payload.py is a library — run build_dashboard.py instead.")
