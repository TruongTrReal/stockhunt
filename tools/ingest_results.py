"""Load the result sheets into `results.db`, so the board can be queried instead of baked.

Re-runnable and idempotent: every write is an upsert on the row's natural key, so running
this after any stage picks up what changed and leaves the rest alone.

**No stage is modified.** `walkforward.py`, `combo_wf.py`, `strat_wf.py`, `riskmatch_wf.py`
and `portfolio_wf.py` keep writing their CSVs exactly as they always have; this reads them
and inserts rows. That is deliberate and it is what makes the switch to a live board safe
to make at all — the same numbers reach the page by a second route, and
`tools/test_board_equivalence.py` asserts the two routes produce an identical document. If
the sheets were rewritten to target the store instead, there would be nothing left to
check the store against.

What is deliberately NOT done here
-----------------------------------
**No filtering and no ranking.** The rows land as the stage wrote them, including the ones
the board drops — unrankable rows, baselines, `IS#1`, every fee scenario rather than the
headline one. Selecting is the ranker's job, and a store that had already made those
choices could not answer a question the board does not currently ask.

The two exceptions are the per-asset fallback tables, which are filtered to the headline
scenario on the way in. They exist only to fill gaps for rules the standard never scored,
the board reads no other scenario from them, and keeping four would quadruple the largest
table in the store to serve nothing.

Run::

    python tools/ingest_results.py                    # every sheet
    python tools/ingest_results.py --class crypto     # one class
    python tools/ingest_results.py --skip-per-asset   # the fast pass, ~5s
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
for p in (str(REPO), str(REPO / "Stockhunt Dashboard")):
    if p not in sys.path:
        sys.path.insert(0, p)

import dash_config                                              # noqa: E402
from dash_config import GROUPS, HEADLINE, TIMEFRAMES, TOP_N, WFO_RESULTS  # noqa: E402

from stockhunt import resultsdb                                 # noqa: E402
from stockhunt.artifacts import read_bulk                       # noqa: E402

BM = WFO_RESULTS
CAP = 10_000.0                   # the stake `riskmatch.parquet` scores wealth on


def _read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def _records(df: pd.DataFrame) -> list[dict]:
    return df.to_dict("records")


# ============================================================ meta

def ingest_meta() -> None:
    """Everything the ranker needs that is not a rule.

    The gate definitions are here rather than imported at query time, and that is the
    dependency decision the whole layout rests on: `api_paths` is the one bootstrap that
    pulls in no trading code, so the HTTP layer starts and tests without the engine
    installed. A ranker that imported `backtest engine/config.py` for `GATES` would end
    that property for the sake of a static list of six dicts.
    """
    resultsdb.set_meta("gates", list(dash_config.bt_config.GATES))
    resultsdb.set_meta("headline", dict(HEADLINE))
    resultsdb.set_meta("timeframes", list(TIMEFRAMES))
    resultsdb.set_meta("top_n", TOP_N)
    resultsdb.set_meta("groups", [{"key": k, "cls": c, "label": lbl,
                                   "universe": list(u)}
                                  for k, c, lbl, u in GROUPS])


# ============================================================ the leaderboard sources

def ingest_wf(cls: str, tf: str) -> dict:
    """The three summary sheets that supply leaderboard rows.

    They are three files and one table because a pair is a strategy in exactly the sense a
    single rule is, and nobody choosing what to trade cares which sweep produced it. `kind`
    is what remembers the difference, which the page prints and the trial count needs.
    """
    counts = {}
    for stem, kind in (("wf_summary", "single"),
                       ("cwf_summary", "pair"),
                       ("strat_summary", "published")):
        df = _read(BM / f"{stem}_{cls}_{tf}.csv")
        if df.empty:
            continue
        rows = _records(df)
        counts[kind] = resultsdb.put_wf(rows, kind)
        # Provenance travels with the published rows only: `family`, `source` and
        # `strategy` are columns `strat_wf.py` emits from the registry and the TA-Lib
        # sweeps have no equivalent.
        resultsdb.put_rules([
            {"cls": cls, "tf": tf, "rule": str(r["rule"]), "kind": kind,
             "family": r.get("family"), "source": r.get("source"),
             "strategy": r.get("strategy")}
            for r in rows])
    return counts


def ingest_book(cls: str, tf: str) -> int:
    """`book_<cls>_<tf>.csv` -- one account holding the whole universe.

    The leaderboard's ranking tiebreak lives here (`cashmatch_excess_cagr`) and so does the
    filter that keeps rules which never open a position off the board (`n_trades`).
    """
    # FALL BACK TO THE OPEN-FILL BOOK WHERE NO CLOSE-FILL ONE EXISTS, which is every 5m
    # sheet and only 5m sheets.
    #
    # 5m is open fill by design, not by omission: at 78 bars a day a rule computed from a
    # bar's own close and filled at that same close measures the look-ahead rather than the
    # rule -- `ibs` on commodities reads 5.5%/yr at 1d and 1,970%/yr at 15m on close fill.
    # So `book_<cls>_5m.csv` does not exist and must not be created.
    #
    # Reading only the close-fill name therefore left every 5m row on the board carrying its
    # verdict and NOTHING ELSE: no CAGR, no IR, no turnover, `pnl_basis: none`. The rules had
    # been scored; the board simply had no book to print. `payload.py` already accepts either
    # fill for the robustness matrix for exactly this reason, and this is the same fix one
    # layer down.
    #
    # The store has no `fill` column and does not need one: a sheet has at most one book here,
    # and which fill produced it is a property of the timeframe rather than a choice.
    path = BM / f"book_{cls}_{tf}.csv"
    if not path.exists():
        alt = BM / f"book_{cls}_{tf}_open.csv"
        if alt.exists():
            path = alt
    df = _read(path)
    if df.empty:
        return 0
    return resultsdb.put_book(_records(df))


def ingest_edge() -> int:
    """`edge_standard.csv` -- every sheet in one file, so it is loaded once.

    Both sides of every rule are stored. Which one the board shows is `board_rank`'s
    decision and it is made on delta-Sharpe; collapsing here would move it somewhere it
    cannot be revisited.
    """
    df = _read(BM / "edge_standard.csv")
    if df.empty:
        return 0
    return resultsdb.put_edge(_records(df))


# ============================================================ per-asset

def ingest_riskmatch() -> int:
    """`riskmatch.parquet` -- the per-symbol layer of the measurement the verdict came from.

    `riskmatch_wf.py` scores every (rule, symbol, side) individually and only then
    aggregates to the sheet row. Reading that layer means a rule's detail page and the
    leaderboard row above it are the same measurement rather than two stages computing the
    same quantity independently — which is what once put a 53.6-year MNST at 2.78e12% on
    the `ibs` page while the row above it was scored over 23.6 years.

    Percentages, not dollars, because the site's formatter turns a percent into "what
    $10,000 became". **Unrounded**, though: rounding is a presentation decision and belongs
    in `board_rank`, which is where it was before this table existed.

    That is not fastidiousness. Rounding here loses the SIGN of a negative zero — a rule
    that lost four thousandths of a percent a year annualises to `-0.0`, which the page
    prints as "−0.0%" and which `round()` in the store turns into a plain `0.0`. It is the
    only difference the equivalence test found across all twenty sheets, and the lesson is
    the general one: a store that rounds has decided something a reader can see.
    """
    path = BM / "riskmatch.parquet"
    if not path.exists():
        return 0
    df = pd.read_parquet(path, columns=[
        "class", "tf", "rule", "symbol", "side", "years", "sharpe_edge",
        "causal_wealth", "bench_wealth", "causal_cagr", "bench_cagr"])
    # Vectorised, not `itertuples`: this is the largest table in the store and a Python
    # loop over 297k rows costs more than every other stage of the ingest put together.
    out = pd.DataFrame({
        "cls": df["class"].astype(str), "tf": df["tf"].astype(str),
        "rule": df["rule"].astype(str), "side": df["side"].astype(str),
        "symbol": df["symbol"].astype(str), "src": "riskmatch",
        "ir": df["sharpe_edge"], "years": df["years"],
        "net_cagr": df["causal_cagr"] * 100.0,
        "bh_cagr": df["bench_cagr"] * 100.0,
        "net_pct": (df["causal_wealth"] / CAP - 1.0) * 100.0,
        "bench_pct": (df["bench_wealth"] / CAP - 1.0) * 100.0})
    return resultsdb.put_per_asset(out.to_dict("records"))


def ingest_per_asset_fallback(cls: str, tf: str) -> int:
    """`wf_per_asset_*` and `strat_per_asset_*` -- the gap-filler.

    A SEPARATE computation of the same quantity, on an IR basis rather than a risk-matched
    one, used only for rules `riskmatch.parquet` never scored. The precedence is the
    ranker's to apply, so both are stored and `src` records which is which.

    Filtered to the headline scenario on the way in — the only one the board reads off
    these tables, and four would quadruple the largest table in the store for nothing.
    """
    scen = HEADLINE[cls]
    total = 0
    for stem, ir_col, src in ((f"wf_per_asset_{cls}_{tf}", "ir_union", "wf"),
                              (f"strat_per_asset_{cls}_{tf}", "ir_wf", "strat")):
        df = read_bulk(BM / stem)
        if df is None or ir_col not in df.columns:
            continue
        if "scenario" in df.columns:
            present = set(df.scenario.unique())
            df = df[df.scenario == (scen if scen in present else sorted(present)[0])]
        if df.empty:
            continue
        has_yrs = "years_oos" in df.columns
        rows = []
        for r in df.itertuples():
            y = float(getattr(r, "years_oos", float("nan"))) if has_yrs else float("nan")
            rows.append({
                "cls": cls, "tf": tf, "rule": str(r.rule), "symbol": str(r.symbol),
                # No side: these stages score the rule as published and never split it.
                # The ranker treats "" as "whatever side this rule is shown on".
                "side": "", "src": src,
                "ir": getattr(r, ir_col), "years": y if y == y else None,
                "net_pct": getattr(r, "ret_pct", None),
                "bench_pct": getattr(r, "bench_pct", None),
                # These stages report totals, not annualised rates. The ranker annualises
                # them, because it is the half that knows each name's own span.
                "net_cagr": None, "bh_cagr": None})
        total += resultsdb.put_per_asset(rows)
    return total


# ============================================================ driver

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--class", dest="classes", nargs="+", default=None,
                    help="limit to these asset classes (default: every group)")
    ap.add_argument("--tf", dest="timeframes", nargs="+", default=None,
                    help=f"limit to these timeframes (default: {' '.join(TIMEFRAMES)})")
    ap.add_argument("--skip-per-asset", action="store_true",
                    help="leaderboard rows only. ~5s instead of ~90s, and the rule "
                         "detail pages will have no asset table until a full run")
    args = ap.parse_args()

    classes = args.classes or [c for _k, c, _l, _u in GROUPS]
    timeframes = args.timeframes or list(TIMEFRAMES)

    t0 = time.time()
    print(f"ingesting into {resultsdb.DB_PATH}")
    ingest_meta()

    n_edge = ingest_edge()
    print(f"  edge_standard.csv           {n_edge:>8,} rows")

    for cls in classes:
        for tf in timeframes:
            counts = ingest_wf(cls, tf)
            n_book = ingest_book(cls, tf)
            if not counts and not n_book:
                continue
            parts = ", ".join(f"{k} {v:,}" for k, v in counts.items())
            print(f"  {cls:<12} {tf:<4} {parts or 'no summary'}"
                  f"{f', book {n_book:,}' if n_book else ''}")

    if not args.skip_per_asset:
        n_rm = ingest_riskmatch()
        print(f"  riskmatch.parquet           {n_rm:>8,} rows")
        n_fb = 0
        for cls in classes:
            for tf in timeframes:
                n_fb += ingest_per_asset_fallback(cls, tf)
        print(f"  per-asset fallback          {n_fb:>8,} rows")

    print(f"\n{time.time() - t0:.1f}s")
    for k, v in resultsdb.summary().items():
        print(f"  {k:<12} {v:,}" if isinstance(v, int) else f"  {k:<12} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
