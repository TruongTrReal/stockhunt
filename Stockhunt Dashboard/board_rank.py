"""The leaderboard: joining five measurements into one ranked sheet, from the store.

Moved out of `payload.py` whole, and the reason is that two processes now need it. The
dashboard builder still bakes a snapshot (`dist/dashboard.html` has to be frozen -- it is
one file with no server behind it), while `paper api` answers `/v1/research/leaderboard`
per request against `results.db`. One ranking, two callers; a second implementation of
this join is the thing most worth not having.

**The code here was moved, not rewritten.** Every filter, every tiebreak and every comment
below came across unchanged from `payload.build_sheet`, because the reasoning in them is
the product of corrections that each cost a wrong number on a published page -- why the
ranking is the book's and not the median asset's, why unscored rows are dropped rather
than sorted last, why a rule that never opens a position is filtered on trade count, why a
side is chosen at rule level and never per symbol. The only lines that changed are the
half-dozen that used to open a CSV and now read a table.

Two things this module must keep being able to say about itself
----------------------------------------------------------------
**It imports pandas and `stockhunt.resultsdb`, and nothing else from this repo.** Not
`dash_config`, not `paper_config`, not the engine's `config`. `paper api/api_paths.py` is
the one bootstrap that pulls in no trading code, which is what lets the HTTP layer start
and its tests run without `nautilus_trader` or a TA-Lib build present. A ranker that
imported the engine for a list of six gate definitions would end that property, so the
gates travel in the store's `meta` table instead -- written there by
`tools/ingest_results.py`, which is a tool and may import whatever it likes.

**The population statistics are computed here, per call, over whatever is in the store.**
`noise_ceiling(n_all, years)`, the trial count, `exposure_corr` -- each is defined over the
whole candidate population, so adding one rule changes it for every existing row. Baked
into a payload they go stale silently the moment a strategy lands and nobody rebuilds;
recomputed on read they cannot.
"""

from __future__ import annotations

import logging
import threading
from statistics import NormalDist

import pandas as pd

from stockhunt import resultsdb

log = logging.getLogger("stockhunt.board_rank")


# ---------------------------------------------------------------- the store as a source
#
# `meta` is written by `tools/ingest_results.py` and holds the few constants the ranking
# needs that are not rules: the six gate definitions, each class's headline fee scenario,
# the leaderboard depth, and each group's universe. See the module docstring for why they
# are read from a table rather than imported.

def gates() -> list[dict]:
    """`config.EDGE_STANDARD`, in order. The order is load-bearing: the booleans on a row
    line up with the labels the page prints beside them, and getting it wrong would tick
    the wrong criterion's name in every tooltip and never raise anything."""
    return resultsdb.get_meta("gates", [])


def headline() -> dict:
    """`class -> the fee scenario the board reads`. `config.HEADLINE_SCENARIO`."""
    return resultsdb.get_meta("headline", {})


def top_n() -> int:
    """Leaderboard depth. The tail is all worse, not different."""
    return int(resultsdb.get_meta("top_n", 30))


def universes() -> dict:
    """`class -> the symbol list the sheet knows about`, for the header count."""
    return {g["cls"]: g["universe"] for g in resultsdb.get_meta("groups", [])}


_BOARD_CACHE: dict[int, dict] = {}

# Held across a rebuild so that N readers arriving on a cold cache pay for ONE build
# rather than N. Two tabs reloading together used to run the whole join twice on a
# two-core box and throw one of the answers away; the second now waits for the first.
_BOARD_LOCK = threading.Lock()


def build_board() -> dict:
    """Every sheet, shaped like the dashboard's `backtest` section. Memoised per revision.

    Keyed on the dashboard's group keys rather than on class names, because that is what
    `app.js` reads and the tab strip is `Object.keys(D.backtest)`.

    **The memo is not optional at this size.** Building every sheet takes tens of
    seconds — 20.7s measured on the deployed two-core box — which is a fine price for a
    builder that runs once and an unacceptable one on every page load. Most of it is the per-asset layer: `_per_asset_from_riskmatch`
    ranks every name for every rule on the sheet, ~400 rules x ~600 symbols on us_stocks
    1d, and only the `TOP_N` rows that ship ever carry the result.

    So it is computed once per store revision and handed out until something is written.
    The first request after the worker inserts a rule pays the rebuild; every other one is
    a dictionary lookup. That is the honest trade — freshness is exact, latency is
    amortised — and it is only safe because the key is the revision rather than a clock.

    Two things follow from the memo outliving a single call, and both are stated below
    rather than here: the rebuild is serialised on `_BOARD_LOCK`, so concurrent readers
    share one build instead of racing several, and `_evict` sweeps the previous
    revision's sheet memos so that they stay a cache rather than becoming a leak. A
    server that would rather nobody paid the first build at all calls `start_warmer`.

    The obvious next optimisation is to rank the sheet first and build per-asset rows only
    for the rows that ship. It is a real change to moved code rather than a cache, so it
    wants `tools/test_board_equivalence.py` either side of it and its own commit.
    """
    rev = resultsdb.revision()
    hit = _BOARD_CACHE.get(rev)
    if hit is not None:
        return hit
    with _BOARD_LOCK:
        # Re-read under the lock. Whoever held it may have been building exactly this
        # revision, in which case the work is already done and this is a dictionary
        # lookup after all.
        hit = _BOARD_CACHE.get(rev)
        if hit is not None:
            return hit
        _evict(rev)
        tfs = resultsdb.get_meta("timeframes", [])
        out = {}
        for g in resultsdb.get_meta("groups", []):
            sheets = [sh for sh in (build_sheet(g["cls"], tf, g["universe"]) for tf in tfs)
                      if sh]
            if sheets:
                out[g["key"]] = {"label": g["label"], "n": len(g["universe"]),
                                 "universe": g["universe"], "sheets": sheets}
        # One revision's worth. Superseded entries are unreachable and holding them would
        # keep a few MB per write alive for the life of the process.
        _BOARD_CACHE.clear()
        _BOARD_CACHE[rev] = out
        return out


def _evict(rev: int) -> None:
    """Drop every sheet-level memo left over from a superseded revision.

    `_BOARD_CACHE` has always cleared itself; the three caches below never did, and under
    a builder that runs once and exits that was invisible. Behind an endpoint it is a leak
    rather than a cache: `_RM_CACHE` holds the per-asset rows for every sheet on the board
    -- 435,012 of them on the deployed store -- so each write to the store used to add a
    second full copy and keep the first one for the life of the process.

    Called from inside the lock, before a rebuild, which is the one moment every one of
    them is about to be repopulated anyway.
    """
    for cache in (_RM_CACHE, _EDGE_CACHE, _BOOK_CACHE):
        for key in [k for k in cache if k[-1] != rev]:
            del cache[key]


# ------------------------------------------------------------------ keeping it warm
#
# The memo above makes a warm board free and leaves the FIRST reader after any restart
# paying for all of it -- 20.7s on the deployed two-core box, measured, with the page
# rendering nothing until it lands because `app.js` awaits `/v1/research/board` before its
# first `render()`. That is the wrong person to charge. A restart is a deploy, a deploy is
# a push, and a push is followed within seconds by somebody opening the board to look at
# what they just shipped.
#
# So a server may ask for the build to happen on a thread it owns instead. Nothing about
# the freshness contract changes -- this calls the same `build_board()` every reader
# calls, and the revision is still what decides whether a rebuild happens. It only moves
# the cost off the critical path of whoever asked first.
#
# The poll is what catches a write from ANOTHER PROCESS: `research_worker.py` scores a
# submitted rule in its own interpreter, and this one learns about it exactly the way a
# reader would -- by noticing the revision moved. Without the poll, a warmer would keep
# the board hot only until the first submission and then hand the cost straight back.
_WARM_STOP = threading.Event()
_WARM_THREAD: threading.Thread | None = None


def start_warmer(interval: float = 30.0) -> threading.Thread | None:
    """Build the board off the request path, now and whenever the store moves.

    Idempotent, and a no-op for `interval <= 0` -- that is how a caller turns it off
    without branching, and how the test suites keep a pandas job out of their fixtures.

    A daemon thread: it holds no resource whose loss matters and must never be the reason
    a process refuses to exit. `stop_warmer` is still worth calling, because a build in
    flight at shutdown otherwise runs on into the next test's store.
    """
    global _WARM_THREAD
    if interval <= 0:
        return None
    if _WARM_THREAD is not None and _WARM_THREAD.is_alive():
        return _WARM_THREAD
    _WARM_STOP.clear()

    def loop() -> None:
        while not _WARM_STOP.is_set():
            try:
                build_board()
            except Exception:
                # A store that is missing, empty or mid-ingest must not take the process
                # down from a background thread. The endpoint degrades on its own -- a
                # non-200 leaves `app.js` on the baked payload -- and the next tick
                # retries, so there is nothing to do here but say so once.
                log.warning("board warm-up failed; will retry", exc_info=True)
            _WARM_STOP.wait(interval)

    _WARM_THREAD = threading.Thread(target=loop, name="board-warmer", daemon=True)
    _WARM_THREAD.start()
    return _WARM_THREAD


def stop_warmer(timeout: float = 5.0) -> None:
    """Ask the warmer to finish and wait briefly for it.

    The wait is bounded because a rebuild already in flight cannot be interrupted, and
    blocking a shutdown for the length of one is worse than letting a daemon thread die
    with the process.
    """
    global _WARM_THREAD
    _WARM_STOP.set()
    thread, _WARM_THREAD = _WARM_THREAD, None
    if thread is not None and thread.is_alive():
        thread.join(timeout)


def _frame(rows: list[dict]) -> pd.DataFrame:
    """Store rows as the DataFrame the CSV used to produce.

    `resultsdb._shape` returns each stage's row verbatim, so the columns and their dtypes
    are what `pd.read_csv` gave the code below -- which is why the filters it inherited
    (`df[df.rankable]`, `~df.is_baseline`) still mean what they meant. Column *order*
    differs and nothing downstream reads by position.
    """
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _wf_frame(cls: str, tf: str, kind: str) -> pd.DataFrame:
    """One summary sheet's rows: `single`, `pair` or `published`."""
    return _frame([r for r in resultsdb.wf_rows(cls, tf) if r.get("kind") == kind])


def _edge_frame(cls: str, tf: str) -> pd.DataFrame:
    return _frame(resultsdb.edge_rows(cls, tf))


def _book_frame(cls: str, tf: str) -> pd.DataFrame:
    return _frame(resultsdb.book_rows(cls, tf))


def _read(path):
    """Kept so the moved code below reads unchanged. Nothing here calls it with a path any
    more; `payload.py` imports it for the sections that still open files directly."""
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


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
    df = _wf_frame(cls, tf, "single")
    if df.empty:
        return None
    scen = headline()[cls]
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
    # The store carries these under `src` in ('wf', 'strat'), filtered to the headline
    # scenario on the way in -- the only one the board ever read off them. The iteration
    # order below IS the precedence: `wf` first, `strat` second, and both skip a rule
    # riskmatch already supplied.
    fallback = [r for r in resultsdb.per_asset_rows(cls, tf) if r["src"] != "riskmatch"]
    for src in ("wf", "strat"):
        by_rule: dict[str, list] = {}
        for r in fallback:
            if r["src"] == src:
                by_rule.setdefault(str(r["rule"]), []).append(r)
        for rule, g in by_rule.items():
            if rule in per:                 # riskmatch already supplied it, and fresher
                continue
            rows = per.setdefault(rule, [])
            for r in g:
                # Each asset annualises over *its own* out-of-sample span. The sheet
                # median is 41 years on 1d equities, but META listed in 2012 and NVDA in
                # 1999 — using the median would understate the newer names by a factor of
                # three.
                y = r.get("years")
                y = float(y) if y is not None else float("nan")
                if not (y and y == y):                      # NaN or 0 -> fall back
                    y = years
                net_pct = num(r.get("net_pct"), 1)
                bh_pct = num(r.get("bench_pct"), 1)
                rows.append({
                    "symbol": str(r["symbol"]), "ir": num(r.get("ir")), "years": round(y, 1),
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
    # The masks are built as bool Series, not bare lists: `df[[]]` with an EMPTY list is
    # COLUMN selection, so on a sheet the standard has not scored yet (a fresh timeframe
    # whose `wf_summary` exists but whose riskmatch/book stages have not run — first hit
    # by us_etfs 1h on 2026-08-22) the second mask stripped every column and the sort on
    # `edge_rank` raised KeyError instead of shipping an empty sheet.
    scoped = merged[pd.Series([e is not None for e in merged["edge_rec"]],
                              index=merged.index, dtype=bool)]
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
    scoped = scoped[pd.Series([not (b and b.get("n_trades") == 0)
                               for b in scoped["book_rec"]],
                              index=scoped.index, dtype=bool)]
    n_flat = before_flat - len(scoped)
    # Two keys, stable: criteria cleared first, then the money at equal risk. `nlargest`
    # takes one column, and a stable sort is what makes the second key the tiebreak rather
    # than an accident of row order.
    top = scoped.sort_values(["edge_rank", "edge_tie"], ascending=False,
                             kind="stable").head(top_n())
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


# ---------------------------------------------------------------- memoisation
#
# A sheet's edge, book and per-asset tables are each read several times while one
# leaderboard is assembled, so they are memoised. Every key carries `resultsdb.revision()`,
# and that is not an optimisation detail — it is what makes these caches legal behind an
# endpoint at all.
#
# They were written for a builder that ran once and exited. Under a server they would be a
# board frozen at the first request: a rule gets scored, the store has it, and the page
# goes on showing what it showed an hour ago — which is precisely the failure this whole
# layer exists to remove, reintroduced one level down and much harder to see, because
# nothing is stale on disk.
#
# The revision is global rather than per sheet. A per-sheet counter is more precise and
# would have to be right about which sheets a write touched; being wrong about that is
# silent, and a re-read costs milliseconds.
def _ckey(cls: str, tf: str) -> tuple:
    return (cls, tf, resultsdb.revision())


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
    key = _ckey(cls, tf)
    if key in _RM_CACHE:
        return _RM_CACHE[key]
    out: dict[str, list] = {}
    g = _frame([r for r in resultsdb.per_asset_rows(cls, tf)
                if r["src"] == "riskmatch"])
    if g.empty:
        _RM_CACHE[key] = out
        return out
    # The side the leaderboard is showing, looked up rather than re-derived. `_edge_index`
    # is the only place that decision is made, and it makes it on `dsharpe`.
    endorsed = {k: v.get("side") for k, v in _edge_index(cls, tf).items()}
    for rule, grp in g.groupby("rule"):
        side = endorsed.get(str(rule))
        if side is None:
            # No rankable edge row for this rule. Fall back to a RULE-level choice on
            # median risk-matched wealth — one side for the whole table either way.
            # `net_pct` is a strictly increasing transform of the risk-matched wealth this
            # used to read off the parquet, so the side it picks is the same one.
            med = grp.groupby("side")["net_pct"].median()
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
            # Four decimals, not one, and `tools/ingest_results.py` is where that rounding
            # happens now. These are percentages of a $10,000 stake, so one decimal is $10
            # of granularity — invisible on a winner and the whole number on a wipeout,
            # where the leaderboard's dollar-rounded $16 met a detail page rounding -99.8%
            # to $20. Same measurement, 25% apart, purely from precision.
            rows.append({
                "symbol": str(r.symbol),
                # `ir` is this row's Sharpe minus its benchmark's (`sharpe_edge` in
                # `riskmatch.parquet`), which is what the leaderboard now ranks on. The
                # column is still labelled IR on the page; it has never been an
                # information ratio here.
                "ir": num(r.ir),
                "years": round(y, 1) if y == y else None,
                "net_cagr": num(r.net_cagr, 1),
                "bh_cagr": num(r.bh_cagr, 1),
                "net_pct": num(r.net_pct, 4), "bh_pct": num(r.bench_pct, 4)})
        out[str(rule)] = rows
    _RM_CACHE[key] = out
    return out


def _edge_years(cls: str, tf: str) -> float | None:
    """Out-of-sample years the STANDARD scored, for the sheet header.

    Separate from `wf_summary`'s `years` because the two stages are re-run independently
    and drifted apart the moment `config.BACKTEST_START` moved: the header said 30.6 years
    over every row scored on 23.6.
    """
    g = _edge_frame(cls, tf)
    if g.empty or "years" not in g.columns:
        return None
    return float(g["years"].median())


def _edge_assets(cls: str, tf: str) -> int | None:
    """How many names the standard actually ran on. Same drift as `_edge_years`.

    The universe list is every symbol the sheet knows about; this is what survived the
    quarantine and the history floor and therefore what every scored column rests on.
    On us_stocks 1d that is 614 against a 751-name universe, and the header used to
    advertise only the larger one.
    """
    g = _edge_frame(cls, tf)
    if g.empty or "n_assets" not in g.columns:
        return None
    return int(g["n_assets"].median())


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
                  for c in gates()],
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
    key = _ckey(cls, tf)
    if key in _BOOK_CACHE:
        return _BOOK_CACHE[key]
    df = _book_frame(cls, tf)
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
    key = _ckey(cls, tf)
    if key in _EDGE_CACHE:
        return _EDGE_CACHE[key]
    g = _edge_frame(cls, tf)
    out: dict[str, dict] = {}
    if not g.empty:
        crit = gates()
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
    df = _wf_frame(cls, tf, "pair")
    if df.empty:
        return pd.DataFrame()
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
    df = _wf_frame(cls, tf, "published")
    if df.empty:
        return pd.DataFrame()
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
        print(f"  note: strat_summary_{cls}_{tf} has no {scen!r} rows; using "
              f"{use!r} (sheet predates the fee-grid change)")
    h = df[(df.scenario == use) & df.rankable & (df.wf_mode == "published")]
    return drop_selection_rows(h)
