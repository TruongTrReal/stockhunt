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

import pandas as pd

import dash_config
from dash_config import (BRIEF_EQUITIES, GROUPS, HEADLINE, TIMEFRAMES, TOP_N,
                         WFO_RESULTS)

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


# The leaderboard itself — the join across five measurements, the ranking, and every
# per-row shape the page reads — lives in `board_rank.py` now, sourced from `results.db`
# instead of from the CSVs. It moved because `paper api` answers
# `/v1/research/leaderboard` out of the same code, and a second implementation of this
# join is the thing most worth not having. See that module's docstring.
#
# `_read` comes back with it: the sections below that still open a file directly (the
# research summary, prereg, parity, the conversion board) use it, and it has no business
# being defined twice either.
from board_rank import (_book_bench, _book_index, _book_record, _read,  # noqa: F401
                        build_sheet, num, text)


# The board's header chart is a THIRD reader of the same series, and it needs almost none
# of what the other two need. `curves/<key>_<tf>.json` is 300-650 kB per sheet because a
# detail page reads `matched`, `metrics`, `bench_metrics`, the index comparisons and every
# point of every series. A chart above the leaderboard reads one line per ticked row and
# nothing else, so fetching the detail file to draw it would put half a megabyte on the
# wire before the reader has opened a single detail page.
#
# 200 points is where the line stops gaining anything a reader can see: the chart shares a
# 1240px rail with its axis, so a finer series is bytes nobody can resolve. It is a cap,
# not a target -- a sheet with fewer points ships all of them untouched.
BOARD_CURVE_POINTS = 200


def _downsample(n: int, limit: int) -> list[int]:
    """Which index positions to keep out of `n`, at most `limit` of them, ends included.

    Even stride, and the LAST index is kept whatever the stride lands on. That last point
    is the terminal wealth the leaderboard's `$10k / book` column prints, so dropping it
    would hang a chart above the table that disagrees with the row underneath it — which
    is the exact class of disagreement `curves.py` was deleted for.

    The stride is a ceiling over `limit - 1` GAPS rather than `limit` points, so appending
    that final index can never push the result past the cap.
    """
    if n <= 0:
        return []
    if n <= limit:
        return list(range(n))
    step = -(-(n - 1) // (limit - 1))
    keep = list(range(0, n, step))
    if keep[-1] != n - 1:
        keep.append(n - 1)
    return keep


def _thin(series: list, keep: list[int]) -> list:
    """`series` at `keep`'s positions, rounded to 2dp, with nulls passed through.

    Two decimals because these are growth-of-100 series: the third one is a hundredth of a
    percent of the starting stake and costs a byte per point per line. `None` is left as
    `None` rather than coerced to zero — a gap in a book's history is a gap, and a chart
    that draws it as a fall to nothing invents a drawdown.

    Reads past the end as `None` so a short series cannot silently shift against the ones
    beside it: every line here is cut on the SAME positions, which is what keeps them
    aligned to one date axis.
    """
    out = []
    for i in keep:
        v = series[i] if i < len(series) else None
        out.append(None if v is None else round(v, 2))
    return out


def board_curves(shown_order: dict[str, list]) -> tuple[dict[str, str], dict]:
    """One small file per house sheet, backing the chart at the top of the leaderboard.

    Returns `(files, index)`: the files as JSON **text** keyed by the relative URL `app.js`
    fetches, for the payload's `_files` side channel, and index entries in the same shape
    `copy_curves` returns — so `D.curves[key].file` finds them with no further wiring.

    **Nothing is written here.** Going through the side channel is what lets `--dist` alone
    embed freshly built bytes instead of whatever a previous `--serve` happened to leave in
    `web/`, and it keeps the standing rule that `--dist` never touches the served site's
    files. `_curves_index_only` still lives with that drift for the detail-page curves; this
    path had no reason to inherit it.

    **Only the rows the sheet SHIPS get a line, and an empty list means no lines at all** —
    the opposite of `_reachable`'s "no list, publish everything". This file exists to back a
    chart over the shipped rows; a sheet that ships nothing has no chart to draw, and a
    409-line header chart is not a useful fallback for a missing 30-line one.
    """
    files: dict[str, str] = {}
    index: dict[str, dict] = {}
    total = 0
    for key, cls, _label, _u in GROUPS:
        for tf in TIMEFRAMES:
            src = BM / f"book_curves_{cls}_{tf}.json"
            if not src.exists():
                continue
            k = f"board_{key}_{tf}"
            all_rules = json.loads(src.read_text(encoding="utf-8"))
            # Shipped ORDER, not a set: the index's `rules` list is what the page draws its
            # legend from, and a set would hand it a different order every build.
            recs = [(r, all_rules[r]) for r in (shown_order.get(f"{key}_{tf}") or [])
                    if (all_rules.get(r) or {}).get("curve")]
            # Dates and the benchmark are ONE series for the whole sheet — every record on
            # it carries the same pair, and the page has always drawn them that way — so
            # the first shipped rule that has them speaks for all of them. Carrying them
            # per rule would triple the file for no information. If no rule has them the
            # key is omitted rather than nulled: absent means "this sheet has no benchmark
            # line", which is a state, where `null` reads as a build fault.
            dates = bench = None
            for _r, rec in recs:
                if dates is None and rec.get("dates"):
                    dates = rec["dates"]
                if bench is None and rec.get("bench"):
                    bench = rec["bench"]
            lengths = [len(rec["curve"]) for _r, rec in recs]
            lengths += [len(s) for s in (dates, bench) if s]
            keep = _downsample(max(lengths) if lengths else 0, BOARD_CURVE_POINTS)
            doc: dict = {}
            if dates:
                doc["dates"] = [dates[i] if i < len(dates) else None for i in keep]
            if bench:
                doc["bench"] = _thin(bench, keep)
            # A rule with no curve is skipped above rather than written as a null entry: a
            # missing series and a series of nulls draw differently, and only one of them
            # is true.
            doc["rules"] = {r: _thin(rec["curve"], keep) for r, rec in recs}
            blob = json.dumps(doc, separators=(",", ":"))
            n_bytes = len(blob.encode("utf-8"))
            files[f"curves/{k}.json"] = blob
            index[k] = {"file": f"curves/{k}.json", "bytes": n_bytes,
                        "rules": [r for r, _rec in recs]}
            total += n_bytes
    print(f"  board curves: {len(files)} sheets, {total / 1e3:.0f} kB total")
    return files, index


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


_OPEN_CACHE: dict[tuple, tuple] = {}


def _book_open_index(cls: str, tf: str) -> tuple[dict, dict | None]:
    """`(rule -> book record, benchmark)` from `book_<cls>_<tf>_open.csv`, off disk.

    Deliberately NOT through `resultsdb`. The store holds the board, and the board is the
    published close-fill convention; the pessimistic bound is an analysis artifact that
    sits beside it, produced by `run_open_fill.sh` for the robustness view alone. Giving
    it a place in the schema would make "which fill is this row?" a question every reader
    of the store has to ask, and every writer has to answer. Reading the CSV keeps the
    ambiguity out of the board entirely, at the cost of one file read per cell.

    Shares `_book_record` with the close-fill path, so the two are the same eight numbers
    computed the same way — the fill is the only thing that differs, which is the whole
    point of showing them together.
    """
    key = (cls, tf)
    if key in _OPEN_CACHE:
        return _OPEN_CACHE[key]
    df = _read(BM / f"book_{cls}_{tf}_open.csv")
    out: dict[str, dict] = {}
    bench: dict | None = None
    if not df.empty:
        for r in df.itertuples():
            out[str(r.rule)] = _book_record(r)
            if bench is None:
                bench = _book_bench(r)
    _OPEN_CACHE[key] = (out, bench)
    return out, bench


def robustness_index(backtest: dict) -> dict:
    """Every rule the book stage scored, on every (class, timeframe) it scored it.

    The robustness view asks the one question a leaderboard structurally cannot: not
    "what ranked here" but "does this signal survive everywhere else". A matrix built
    from the shipped `TOP_N` rows would answer it with survivorship — a rule appears
    exactly where it did well and its weak environments vanish, which inverts the point
    of the view. So this index is cut from the full `book_*.csv` sheets instead: ~400
    rules per sheet, eight numbers per cell, ~250 kB inlined. Curves stay fetched on
    demand; this is small enough to ride in `data.js` and the view needs all of it at
    once to draw a matrix.

    `fields` names the per-cell array order and `ROB_FIELDS` in `app.js` mirrors it —
    change one and change the other in the same commit.

    **Two fills, and the second one is the point.** `book_<cls>_<tf>.csv` is the
    published close-fill convention: the signal is computed from a bar's own high, low
    and close and then transacted at that same close, a price nobody knew when the
    decision was made. That has always been labelled an optimistic bound; measured
    2026-08-24 it stops being a nudge and becomes the whole result as bars get finer,
    because the bias is per-bar and compounds. `ibs` on commodities, same instruments
    and period, close fill:

        1d  6,511 bars      5.5%/yr  Sharpe  0.45
        4h  6,067 bars     37.9%/yr  Sharpe  2.13
        1h 23,249 bars    122.7%/yr  Sharpe  4.61
        15m 75,909 bars 1,970.1%/yr  Sharpe 13.04

    A matrix on close fill alone therefore ranks every reversion rule higher at finer
    timeframes for a reason that has nothing to do with the rule — which would make this
    view actively misleading, since comparing timeframes IS what it is for. So
    `book_<cls>_<tf>_open.csv` rides alongside under `open`, and the view can show
    either. Neither is the truth: `open` charges a full session of delay a
    market-on-close order would not pay. The honest read is the range.
    """
    fields = ["sharpe", "cm_excess_cagr", "cagr", "dd", "exposure", "n_trades",
              "profit_factor", "win_rate"]
    envs: list[dict] = []
    rules: dict[str, dict] = {}
    rules_open: dict[str, dict] = {}
    for key, cls, _label, _u in GROUPS:
        for tf in TIMEFRAMES:
            book, bench = _book_index(cls, tf)
            ob, obench = _book_open_index(cls, tf)
            has_close = bool(book) and bench is not None
            has_open = bool(ob) and obench is not None
            # EITHER fill makes the environment real, and neither implies the other.
            # `5m` was run at `open` only — deliberately, because a close-fill number on
            # 78 bars a day is the look-ahead and not the rule — so gating the whole
            # environment on the close sheet dropped five cells from the matrix without
            # a word. The reverse gap is the older one: every cell existed at `close`
            # before the pessimistic pass was run at all.
            if not has_close and not has_open:
                continue
            ekey = f"{key}_{tf}"
            # Years and the universe size are facts about the CELL, not about the fill,
            # so they come off whichever sheet is present. The benchmark's own scores
            # never cross: a close-fill Sharpe under an `open` label would charge the
            # delay to one side only, which is the thing this split exists to prevent.
            shape = bench if has_close else obench
            envs.append({"key": ekey, "cls": key, "tf": tf,
                         "bench": ({"sharpe": bench.get("sharpe"),
                                    "cagr": bench.get("cagr"),
                                    "dd": bench.get("dd"),
                                    "wealth": bench.get("wealth")}
                                   if has_close else None),
                         "years": shape.get("years"),
                         "n_names": shape.get("n_names")})
            if has_close:
                for rule, rec in book.items():
                    rules.setdefault(str(rule), {})[ekey] = [rec.get(f) for f in fields]
            # The pessimistic bound, where it has been run. Absent is a real state —
            # a cell scored at close fill only — and the view says so rather than
            # falling back to the close number under an "open" label.
            if has_open:
                for rule, rec in ob.items():
                    rules_open.setdefault(str(rule), {})[ekey] = [rec.get(f)
                                                                  for f in fields]
                envs[-1]["bench_open"] = {"sharpe": obench.get("sharpe")}
    # The leaderboard's Robustness column, attached here so the page never re-derives
    # the definition: environments where the book's Sharpe cleared the same universe
    # held passively, out of the environments the rule was scored on at all. A raw
    # count on purpose — a composite "robustness score" with an undefined formula is
    # exactly the kind of number this dashboard exists to not print.
    bench_sharpe = {e["key"]: (e["bench"] or {}).get("sharpe") for e in envs}
    # The open-fill benchmark is its OWN number, not the close-fill one: holding is also
    # filled a bar later, so comparing an open-fill rule against a close-fill benchmark
    # would charge the delay to one side only.
    bench_open = {e["key"]: (e.get("bench_open") or {}).get("sharpe") for e in envs}
    for g in backtest.values():
        for s in g["sheets"]:
            for r in s["rows"]:
                cells = rules.get(r["rule"])
                if not cells:
                    continue
                beat = sum(1 for ek, v in cells.items()
                           if v[0] is not None and bench_sharpe.get(ek) is not None
                           and v[0] > bench_sharpe[ek])
                # Both bounds on the row, because the drop between them IS the finding on
                # several rules: `ibs` beats holding in 8 of 20 environments at the
                # published fill and 5 of 20 once it can no longer transact at a close it
                # used to peek at. A single number here would have to pick one, and
                # whichever it picked would be quoted without the other.
                ocells = rules_open.get(r["rule"]) or {}
                obeat = sum(1 for ek, v in ocells.items()
                            if v[0] is not None
                            and (bench_open.get(ek) or bench_sharpe.get(ek)) is not None
                            and v[0] > (bench_open.get(ek) or bench_sharpe[ek]))
                r["rob"] = {"n": beat, "total": len(cells),
                            "n_open": obeat, "total_open": len(ocells)}
    return {"fields": fields, "envs": envs, "rules": rules, "open": rules_open}


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
             "the book. This is the shape the research scored — its figures are means "
             "across the whole class, so a forward test on a single symbol would measure "
             "something the backtest never reported.",
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



def build(copy_curve_files: bool = True, offline: bool = False) -> dict:
    """The whole payload, for either emitter.

    `copy_curve_files` mirrors the WFO stage's curve JSONs into `web/curves/` and is only
    wanted for the served build. `offline` skips the one network call (the price snapshot),
    which matters when rebuilding without an API key or when Twelve Data is down -- the
    rest of the payload comes off local CSVs and must not be held hostage to that.

    **`payload["_files"]` is a side channel, not a section of the document.** It maps the
    relative URL `app.js` will fetch to that file's JSON text, for generated files that
    must reach BOTH outputs without riding inside `window.DASH`: `emit_serve` writes them
    under `web/`, `emit_dist` folds them into the embedded map, and both strip the key
    before serialising. It exists so that neither emitter has to write into the other's
    output directory to hand a file over -- `--dist` alone must not touch the served site,
    and embedding what a previous `--serve` left on disk is a staleness this path does not
    need to inherit.
    """
    backtest = {}
    for key, cls, label, universe in GROUPS:
        sheets = [s for s in (build_sheet(cls, tf, universe) for tf in TIMEFRAMES) if s]
        if sheets:
            backtest[key] = {"label": label, "n": len(universe),
                             "universe": universe, "sheets": sheets}

    # Attaches `rob` to every shipped row as a side effect, so it runs after the sheets
    # are built and before the payload is assembled.
    robust = robustness_index(backtest)

    paper = paper_state()
    # Only the rules this payload can actually open. `run_book.sh` curves all ~409 labels
    # per sheet and `results/` keeps every one of them, but a leaderboard ships `TOP_N`
    # rows and a detail page is reachable only from a row -- so publishing the rest put
    # 13x the bytes into `web/curves/` and took the single-file build from 9 MB to 38 MB
    # to carry charts nothing links to. Raise `TOP_N` and they publish themselves; no
    # re-run is needed, because the source files already hold them.
    #
    # Kept in SHIPPED ORDER and reduced to a set for the membership tests. `board_curves`
    # needs the order — its index carries the legend the header chart draws — and deriving
    # both from one expression is what stops the two lists drifting apart.
    shown_order = {f"{key}_{s['timeframe']}": [r["rule"] for r in s["rows"]]
                   for key, g in backtest.items() for s in g["sheets"]}
    shown = {k: set(v) for k, v in shown_order.items()}
    curves_index = (copy_curves(shown) if copy_curve_files
                    else _curves_index_only(shown))
    # The compact per-sheet series behind the leaderboard's own chart. They ride the
    # `_files` channel rather than being written here, so `--dist` alone gets freshly built
    # bytes and the served site's files are only ever touched by `--serve`.
    board_files, board_index = board_curves(shown_order)
    curves_index.update(board_index)
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
        # **A stub, and the index itself is a file now.** `robustness_index` is ~900 kB of
        # the payload and, since the matrix moved onto each strategy's detail page, it is
        # read only when somebody opens one — so every visitor was parsing it to render a
        # leaderboard that does not use it. It goes out through `_files` as `robust.json`
        # and `app.js` fetches it on demand, treating the presence of `file` as "not loaded
        # yet". The stub carries nothing else on purpose: a half-filled `robust` would let
        # a caller read a partial matrix without knowing it was partial.
        "robust": {"file": "robust.json"},
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
    # The side channel. Assembled last so the sections above cannot accidentally read it,
    # and stripped by whichever emitter serialises the payload -- it must never appear in
    # `window.DASH`, which is the whole reason it is a separate key rather than a section.
    payload["_files"] = {
        # The robustness matrix, out of `data.js` and into a fetch a detail page makes.
        "robust.json": json.dumps(robust, separators=(",", ":")),
        **board_files,
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
    s = payload["summary"]
    print(f"  paper strategies: {len(payload['strategies'])}")
    print(f"  summary sections: {len(s['sheets'])} sheets, {len(s['etf_sheets'])} ETF, "
          f"{len(s['prereg'])} prereg, {len(s['parity'])} parity, "
          f"{len(s['prices'])} prices")


if __name__ == "__main__":
    raise SystemExit("payload.py is a library — run build_dashboard.py instead.")
