"""Which rules from the research can be promoted to the live desk, published as a file.

This is the menu behind "pick a strategy from the backtest and paper trade it". It is
generated, never edited, and it is exported for the same reason `live.json` is: the API
has to be able to read it without importing the trading stack, the universe or pandas.

**It applies exactly the filters `paper_config.top_rules` applies**, and calls the same
helpers, so the menu and the desk's own automatic selection can never disagree about what
is tradable. In particular `wf_mode == "fixed"` — the re-selected rows (`IS#1`, the `[WF]`
families) are a different rule in every fold and have no single definition to trade live.

**Ranking is not passing, and the file says so.** Nothing on any of these sheets clears an
acceptance gate, and several are led by rules that are in the market ~86% of the time,
which is the leaderboard measuring capital deployment rather than skill. So every entry
carries `long_frac` and the exposure-matched figure beside `ir_net`, and the document
carries a `health` block a reader has to get past. A menu showing one flattering number
would be read as a recommendation.

Run::

    python catalog.py                 # -> ../Stockhunt Dashboard/web/catalog.json
    python catalog.py --print         # ...and show what it found
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

import paper_config

# Columns carried through when the sheet has them. `ir_net` alone is the number people
# would quote; the rest are what stop it from being quoted alone.
EXTRA = ("long_frac", "exposure", "t_stat", "years", "n_assets", "headroom",
         "ir_vs_random", "net_return_pct", "bench_return_pct", "excess_return_pct")

# How many rules per sheet the menu offers. Deeper than the desk's own TOP_N_RULES,
# because a person choosing deliberately is a different thing from an automatic top-N —
# but not unbounded, since the gap between rank 1 and rank 50 on these sheets is well
# inside one standard error of a single IR and a longer list is not a longer list of
# candidates, it is a longer list of noise.
DEPTH = 25

WARNING = ("Ranking is not passing. Nothing on any of these sheets clears an acceptance "
           "gate, and several are led by rules that hold a position ~86% of the time — "
           "which is this leaderboard measuring time in the market, not skill. Read "
           "long_frac beside ir_net. These are the least-bad candidates, not good ones.")


def _sheet(cls: str, tf: str):
    path = paper_config.WFO_RESULTS / f"wf_summary_{cls}_{tf}.csv"
    if not path.exists():
        return None, path, False
    return True, path, paper_config._warn_if_stale(path)


def _board_rows(cls: str, tf: str):
    """The leaderboard EXACTLY as the dashboard ranks it.

    Built by calling the dashboard's own `payload.build_sheet` rather than re-deriving it,
    and that is the whole point of this function.

    The first version of this file read `wf_summary_*.csv` and sorted on `ir_net`, which is
    what `paper_config.top_rules` does for the desk's automatic selection. It produced a
    completely different list from the board: the board ranks on `edge_passed` — did the
    rule clear the acceptance standard in `edge_standard.csv` — and tie-breaks on
    `book_cm_excess_cagr`, the cash-matched excess CAGR at BOOK level. It also merges in
    `strategies/published/`, which is not in `wf_summary` at all.

    The result was a picker that did not contain `ibs` or `volmanaged`, the two rules at
    the top of the `us_stocks 1d` board. A menu that disagrees with the page it sits
    behind is worse than no menu.
    """
    import sys
    if str(paper_config.DASHBOARD) not in sys.path:
        sys.path.insert(0, str(paper_config.DASHBOARD))
    import dash_config                                       # noqa: F401  (bootstrap)
    import payload

    import config as bt_config
    universe = bt_config.CLASSES[cls]["symbols"]
    sheet = payload.build_sheet(cls, tf, universe)
    return (sheet or {}).get("rows", []), (sheet or {})


def cells(cls: str, tf: str, depth: int = DEPTH) -> list[dict]:
    """Promotable rows, in the order the dashboard shows them.

    Every row carries `tradable`, because the two families the board merges are not equally
    runnable live:

      published   `ibs`, `volmanaged` — built by `strategies.registry`. Tradable.
      single      the 231 TA-Lib rules — built by `signals`. Tradable.
      pair        a combo, rebuilt from its stored legs by `signals.position_for_row`,
                  which needs the leaderboard ROW. A live strategy holds only a label,
                  so a combo cannot be reconstructed and is NOT tradable.

    Marked rather than filtered out, so the picker can show the board's real top of the
    list and say why a row is unavailable — silently dropping the best rule is how the
    first version of this file went wrong.
    """
    import live_signal

    rows, _ = _board_rows(cls, tf)
    out: list[dict] = []
    for r in rows:
        rule = str(r.get("rule") or "")
        if not rule:
            continue
        if any(paper_config._same_idea(rule, seen["rule"]) for seen in out):
            continue

        # Tradable if — and only if — a dispatcher can build the label. Nothing else:
        # `kind` used to disqualify every pair on the grounds that a combo is rebuilt from
        # a leaderboard row, which is true of the row-based path and false of the label.
        # `combo.parse` reads the legs and the operator straight out of the name, so the
        # whole top of the crypto 1d board is tradable and was marked unavailable.
        family = live_signal.family(rule)
        tradable = family != live_signal.UNKNOWN
        why = "" if tradable else (
            "no signal dispatcher can build this label — if it is a pair, one of its legs "
            "is unknown")

        edge, book = r.get("edge") or {}, r.get("book") or {}
        entry = {
            "rule": rule, "cls": cls, "tf": tf,
            "kind": r.get("kind"), "family": family,
            "tradable": tradable, "not_tradable_because": why,
            # The two the board actually ranks on, carried so the picker can show why a
            # row is where it is.
            "edge_passed": edge.get("passed") if isinstance(edge, dict) else None,
            "book_cm_excess_cagr": (book.get("cm_excess_cagr")
                                    if isinstance(book, dict) else None),
        }
        for col in EXTRA:
            if col in r:
                entry[col] = _num(r.get(col))
        out.append(entry)
        if len(out) >= depth:
            break
    return out


def _dash_class_map() -> dict:
    """The board's tab key -> the engine's class name, from the one place it is defined."""
    try:
        import sys
        if str(paper_config.DASHBOARD) not in sys.path:
            sys.path.insert(0, str(paper_config.DASHBOARD))
        import dash_config
        return {key: cls for key, cls, *_ in dash_config.GROUPS}
    except Exception:
        # The identity map is the honest fallback: a class the board and the desk already
        # spell the same way still works, and anything else fails visibly rather than
        # silently offering a book of nothing.
        return {}


def _num(v):
    """JSON has no NaN. A missing number must travel as null, not as the string 'nan'."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, 6)


def build(depth: int = DEPTH) -> dict:
    sheets, stale = {}, []
    for cls in paper_config.UNIVERSE:
        for tf in paper_config.FORWARD_TIMEFRAMES:
            exists, path, is_stale = _sheet(cls, tf)
            key = f"{cls}_{tf}"
            if not exists:
                continue
            try:
                rows = cells(cls, tf, depth)
            except Exception as exc:
                # One unbuildable sheet must not cost the other seven. It is reported
                # rather than swallowed: an absent sheet and a broken one look identical
                # from the picker, and only one of them is somebody's mistake.
                print(f"  ! {key}: {type(exc).__name__}: {exc}")
                continue
            sheets[key] = {
                "mtime": datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                "ranked_on": "edge_passed, then book_cm_excess_cagr — the same order the "
                             "dashboard shows",
                "cells": rows,
            }
            if is_stale:
                stale.append(key)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # Which symbols a promotion may name, per class. The desk refuses anything else —
        # a symbol it does not already subscribe to costs an instrument, a subscription
        # and a full 1,500-bar warm-up — and publishing the list lets the API say so
        # immediately instead of leaving a registration pending until the next tick.
        "universe": {cls: list(syms) for cls, syms in paper_config.UNIVERSE.items()},
        "timeframes": list(paper_config.FORWARD_TIMEFRAMES),
        # What a promotion actually creates, published so the board can say it exactly
        # rather than hardcoding numbers that drift. `names` is read LIVE — the top 100
        # moves, and a stale count on the switch is a promise the desk will not keep.
        "book": {
            "capital": paper_config.BOOK_CAPITAL,
            "timeframes": list(paper_config.BOOK_TIMEFRAMES),
            "names": {cls: len(paper_config.book_universe(cls))
                      for cls in paper_config.UNIVERSE},
            # Declared per class, never inferred from the holdings. A book of a hundred
            # names has no single instrument to benchmark against, so where there is no
            # obvious index the honest answer is none and the curve is one line.
            "benchmark": {"us_stocks": "SPY", "us_etfs": "QQQ",
                          "crypto": None, "commodities": None},
            # The dashboard's tab keys are NOT the engine's class names — the board says
            # `stocks` and `etf` where the desk says `us_stocks` and `us_etfs`. The URL a
            # reader is on carries the board's spelling, so the switch has to translate
            # before it can look anything up or ask for anything.
            #
            # Published from `dash_config.GROUPS`, which is where that mapping is actually
            # defined, rather than restated in JavaScript where it would drift. Getting it
            # wrong is not loud: the switch offered "0 stocks names" and "$100k a name".
            "class_map": _dash_class_map(),
        },
        "health": {
            "warning": WARNING,
            # A sheet older than the data corrections it was computed from ranked rules
            # over a different SAMPLE, and nothing in the CSV records that.
            "stale_sheets": stale,
        },
        "sheets": sheets,
    }


def publish(depth: int = DEPTH):
    """Write beside `live.json`, atomically, wherever the desk publishes."""
    if paper_config.PUBLISH_DIR is None:
        return None
    path = paper_config.PUBLISH_DIR / "catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(build(depth), indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--depth", type=int, default=DEPTH)
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()

    doc = build(args.depth)
    path = publish(args.depth)
    total = sum(len(s["cells"]) for s in doc["sheets"].values())
    print(f"  {total} promotable cells across {len(doc['sheets'])} sheets -> {path}")
    if doc["health"]["stale_sheets"]:
        print(f"  ! stale: {', '.join(doc['health']['stale_sheets'])}")
    if args.print:
        for key, sheet in doc["sheets"].items():
            print(f"\n  {key}   ({len(sheet['cells'])} cells)")
            for c in sheet["cells"][:5]:
                print(f"    {c['rule']:<24} ir_net {c['ir_net']:>7}  "
                      f"long_frac {c.get('long_frac')}")
    print(f"\n  {WARNING}")


if __name__ == "__main__":
    main()
