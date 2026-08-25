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
            if not book or bench is None:
                continue
            ekey = f"{key}_{tf}"
            envs.append({"key": ekey, "cls": key, "tf": tf,
                         "bench": {"sharpe": bench.get("sharpe"),
                                   "cagr": bench.get("cagr"),
                                   "dd": bench.get("dd"),
                                   "wealth": bench.get("wealth")},
                         "years": bench.get("years"),
                         "n_names": bench.get("n_names")})
            for rule, rec in book.items():
                rules.setdefault(str(rule), {})[ekey] = [rec.get(f) for f in fields]
            # The pessimistic bound, where it has been run. Absent is a real state —
            # a cell scored at close fill only — and the view says so rather than
            # falling back to the close number under an "open" label.
            ob, obench = _book_open_index(cls, tf)
            if ob and obench is not None:
                for rule, rec in ob.items():
                    rules_open.setdefault(str(rule), {})[ekey] = [rec.get(f)
                                                                  for f in fields]
                for e in envs:
                    if e["key"] == ekey:
                        e["bench_open"] = {"sharpe": obench.get("sharpe")}
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
            # The `_flat` sheets carry the SAME rule labels as their held-overnight
            # twins -- the variant is a run flag (`--flatten-eod`), not a different
            # label -- so the label alone is not an identity. Two rows sharing one
            # would collide in the URL slug and in the curve lookup, and `.find()`
            # would hand both detail pages the first of the pair.
            key = f"{label}|flat" if eod == "flat" else label
            if key not in rows:
                rows[key] = {"rule": label, "key": key, "source": name, "eod": eod,
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


def _conv_curve_json(cls: str, tf: str) -> dict:
    """Both overnight variants' curves, merged under the ROW's identity.

    `--flatten-eod` re-runs the same labels, so its curves land in their own file under
    their own stem and would otherwise collide key-for-key with the held-overnight ones.
    Suffixing `|flat` here is what makes one published file per (group, timeframe) able to
    carry both, and it is the same key `_conv_rows` puts on the row.
    """
    out: dict = {}
    for stem, suffix in (("convert_curves", ""), ("convert_curves_flat", "|flat")):
        p = BM / f"{stem}_{cls}_{tf}.json"
        try:
            for rule, payload_ in json.loads(p.read_text(encoding="utf-8")).items():
                out[f"{rule}{suffix}"] = payload_
        except (OSError, ValueError):
            continue
    return out


def _conv_curve_rules(cls: str, tf: str) -> set:
    """Which rows `run_convert_curves.sh` has written an equity series for."""
    key = (cls, tf)
    if key not in _CONV_CURVE_CACHE:
        _CONV_CURVE_CACHE[key] = set(_conv_curve_json(cls, tf))
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
            if not (BM / f"convert_curves_{cls}_{tf}.json").exists()                and not (BM / f"convert_curves_flat_{cls}_{tf}.json").exists():
                continue
            k = f"conv_{key}_{tf}"
            all_rules = _conv_curve_json(cls, tf)
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
        flat = "_flat" in m.group("tail")
        # A rebuilt cell supersedes the sheets it re-scored -- but ONLY the held-overnight
        # ones. `--flatten-eod` is a separate run of the same labels and the rebuild does
        # not carry it, so dropping these on the strength of the rebuild would quietly
        # shrink the sheet by 28 cells. They keep their old rows and simply have no curve,
        # which is a state the board already renders.
        if (cls, tf) in rebuilt and not flat:
            continue
        add(cls, tf, [path.name], eod="flat" if flat else "hold")

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
                # Keyed on the row's identity, so a flattened twin cannot inherit its
                # held-overnight partner's chart. The rebuild carries the held variant
                # only, so `|flat` rows are correctly chartless until a flattened pass
                # is run.
                e["curve"] = e["key"] in have
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
    shown = {f"{key}_{s['timeframe']}": {r["rule"] for r in s["rows"]}
             for key, g in backtest.items() for s in g["sheets"]}
    curves_index = (copy_curves(shown) if copy_curve_files
                    else _curves_index_only(shown))
    # The second board's curves, in their own `conv_` namespace and cut to the rows it
    # ships, exactly like the first board's. Built here rather than inside
    # `conversion_sheets` so both boards' publishing happens in one place and neither can
    # quietly stop writing while the index still claims the files exist.
    conversions = conversion_sheets()
    conv_shown = {f"conv_{key}_{s['timeframe']}": {r.get("key") or r["rule"]
                                                    for r in s["rows"]}
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
        # The full-population robustness index — see `robustness_index` for why it is
        # not derived from the shipped rows.
        "robust": robust,
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
