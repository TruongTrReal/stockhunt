r"""Follow the leaderboard: which rules each sheet's portfolio should hold today.

One portfolio per leaderboard sheet — one (asset class, timeframe) pair — holding that
sheet's top N tradable rules. Membership is re-checked once a day: a rule that has dropped
out of the top N is retired and whoever replaced it is started.

**This module decides; it does not act.** Everything here is a pure function over a
catalog document and a list of rule labels, and `plan()` returns the whole daily reconcile
as data. Applying it is `stockhunt.portfolios.apply_membership`'s job. The split is not
tidiness: a `--dry-run` that shares no code with the real path is a dry run of something
else, and a reconcile that computes and writes in one pass cannot be tested without a
database to write to.

    python portfolio_follow.py                       # the plan, as a table
    python portfolio_follow.py --cls crypto --tf 1d
    python portfolio_follow.py --current members.json --json

**Ranking is not passing.** Nothing on any of these sheets clears an acceptance gate. A
place in a sheet's top five is the least-bad candidate on that sheet, not a good one, and
no string this module produces may be written as though it were otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import paper_config

# `board_rank` is THE ranking, and `is_idle` / `is_closet_bh` are its definitions of the
# two ways a book reaches a leaderboard without being a strategy. They are imported rather
# than restated: a second copy of those thresholds would drift from the board, and the
# failure that produces is a desk holding a book the page has already stopped showing.
# The path hop is the one `catalog.py` already makes for `payload`; the module imports
# pandas and `stockhunt.resultsdb`, and opens nothing.
if str(paper_config.DASHBOARD) not in sys.path:
    sys.path.insert(0, str(paper_config.DASHBOARD))
import board_rank                                            # noqa: E402

DEFAULT_N = 5
"""How many rules a follow-portfolio holds. Five, and it is a decision about noise rather
than a round number: on every sheet here the gap between rank 1 and rank 8 is smaller than
one standard error of a single IR, so a deeper portfolio is not a broader bet — it is the
same bet with more of the sheet's own noise in it."""

NOT_PASSING = ("Ranking is not passing — nothing on this sheet clears an acceptance gate. "
               "A top place is the least-bad candidate here, not a good one.")


def catalog(path=None) -> dict:
    """The published menu, read from disk.

    Deliberately its own four lines rather than `promote_top.catalog`: importing that
    module drags `stockhunt.deskdb` in behind it, and nothing that only computes a plan
    should have the order ledger on its import path.
    """
    if path is None:
        if paper_config.PUBLISH_DIR is None:
            raise SystemExit("publishing is switched off — there is no catalog.json to read")
        path = paper_config.PUBLISH_DIR / "catalog.json"
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"no catalog at {path} — run `python catalog.py` first")
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- the sheets

def sheets(doc: dict | None = None, timeframes=None) -> list[tuple[str, str]]:
    """Which leaderboard sheets exist AND may carry a book, as (class, timeframe).

    **The truth is `catalog.json`**, published by `catalog.py`. It carries a key per sheet
    the board could actually rank, which is a stronger statement than "the CSV is on
    disk": a `wf_summary_*.csv` whose riskmatch and book stages have never run ranks
    nothing, and a portfolio built on it would hold rules no verdict has looked at. The
    same document is what `promote_top.py` and the backtest page's switch read, so the
    portfolio, the switch and the desk cannot disagree about what is on offer.

    Nothing is hard-coded, so a sheet that lands next month appears here on its own the
    first time `catalog.py` runs after it — which is already a step of the pipeline.

    **When that source is stale, a portfolio is MISSING, never wrong.** An un-republished
    catalog is a sheet this function does not return, so the cell simply has no portfolio
    until somebody re-runs `catalog.py`; it can never produce a portfolio holding rules
    from a sheet that no longer exists. `unfollowed()` names those cells so a dry run can
    say which sheets are on disk and not yet on the menu, and every plan row carries the
    catalog's `generated_at` and its `stale` flag so the age travels with the decision.

    `timeframes` defaults to `paper_config.BOOK_TIMEFRAMES` — the sizes the desk can
    actually run a book at — and is a parameter so that widening that list is the only
    change needed when 1h and 15m are measured. It is read from `paper_config` rather than
    from the document's own `book.timeframes` copy on purpose: the copy is a snapshot, and
    a stale one would keep a widened list from ever taking effect.
    """
    doc = catalog() if doc is None else doc
    tfs = list(paper_config.BOOK_TIMEFRAMES if timeframes is None else timeframes)
    names = (doc.get("book") or {}).get("names") or {}
    out: list[tuple[str, str]] = []
    for key in (doc.get("sheets") or {}):
        cls, _, tf = str(key).rpartition("_")
        if not cls or tf not in tfs:
            continue
        # A class with no live names is a book of nothing. `promote_top.picks` skips it for
        # the same reason: the registration would succeed and the book would hold air.
        if not names.get(cls):
            continue
        out.append((cls, tf))
    return out


def unfollowed(doc: dict | None = None, timeframes=None) -> list[tuple[str, str]]:
    """Sheets on disk that the catalog does not carry — i.e. the catalog needs rebuilding.

    The one function here that reads the filesystem, and it decides nothing: it exists so
    that "this cell has no portfolio" can be told apart from "this cell has no sheet". A
    dry run that cannot make that distinction reports a missing portfolio as normal.
    """
    doc = catalog() if doc is None else doc
    tfs = list(paper_config.BOOK_TIMEFRAMES if timeframes is None else timeframes)
    have = set(doc.get("sheets") or {})
    return [(cls, tf) for cls in paper_config.UNIVERSE for tf in tfs
            if f"{cls}_{tf}" not in have and paper_config.has_sheet(cls, tf)]


# ------------------------------------------------------------------- what a sheet offers

def _book_of(cell: dict) -> dict:
    """The book record `board_rank`'s two filters read, from whatever the cell carries.

    A row straight off `board_rank.build_sheet` has a `book` blob; a `catalog.json` cell
    has been flattened and keeps only some of those columns. Both are answered here so the
    filters below have one input shape, and a column the cell does not carry stays absent
    rather than becoming a zero — `is_idle({})` is False, and a rule the book stage has not
    reached must not be cut as though it had been measured and found empty.
    """
    book = cell.get("book")
    if isinstance(book, dict) and book:
        return book
    keep = ("exposure", "n_trades", "r2_vs_bh", "beta_bh", "n_names", "years")
    return {k: cell[k] for k in keep if cell.get(k) is not None}


def excluded_because(cell: dict) -> str:
    """Why this cell may not go into a portfolio, or "" if it may.

    Three exclusions, and two of them had been in somebody's top ten:

      untradable  no signal dispatcher can build the label, so the desk cannot run it
      idle        the book is in cash — a T-bill account with a signal attached
      closet      the book IS buy-and-hold, so the market's own return arrives on the page
                  under a rule's name

    A duplicate idea is the fourth and is not here, because it depends on what has already
    been picked. `top()` owns that one.
    """
    if not cell.get("tradable"):
        return str(cell.get("not_tradable_because")
                   or "no signal dispatcher can build this label")
    book = _book_of(cell)
    if board_rank.is_idle(book):
        return "the book holds almost nothing — cash with a signal attached, not a strategy"
    if board_rank.is_closet_bh(book):
        return "the book is buy-and-hold wearing a rule's name, not a strategy"
    return ""


def top(cls: str, tf: str, n: int = DEFAULT_N, doc: dict | None = None) -> list[dict]:
    """The sheet's top `n` holdable rules, in the board's own order.

    Generalised from `promote_top.picks`, and the one property worth keeping from it: a
    row that cannot be held is **skipped, not counted**, so `n=5` means five holdings and
    never "five rows, of which two were combos we could not build". Skipped rows are still
    ranked ahead — the skip is about what can run, never about what scored.

    Each row carries `rank` (its place in the portfolio), `board_pos` (its place in the
    sheet's own order, counting everything skipped ahead of it) and the two keys the board
    ranks on, so a caller can say WHY a rule is in without re-reading the leaderboard. A
    change log that records only a label cannot answer that question a month later.

    A sheet the catalog does not carry returns `[]`, and a sheet with fewer than `n`
    holdable rules returns what it has. Neither is an error: one is a cell the research has
    not reached, the other is a thin sheet, and raising on either would take a daily
    reconcile down over a cell that is merely empty.
    """
    doc = catalog() if doc is None else doc
    key = f"{cls}_{tf}"
    sheet = (doc.get("sheets") or {}).get(key)
    if not sheet:
        return []
    stale = key in ((doc.get("health") or {}).get("stale_sheets") or [])
    ranked_on = str(sheet.get("ranked_on") or "the board's own order")

    out: list[dict] = []
    for pos, cell in enumerate(sheet.get("cells") or [], start=1):
        if len(out) >= n:
            break
        rule = str(cell.get("rule") or "")
        if not rule or excluded_because(cell):
            continue
        # Two names for one idea double an exposure while reading as diversification.
        # `_same_idea` is measured, not assumed — see `paper_config._ALIASES`.
        if any(paper_config._same_idea(rule, picked["rule"]) for picked in out):
            continue
        out.append({
            "rule": rule, "cls": cls, "tf": tf, "sheet": key,
            "rank": len(out) + 1,
            "board_pos": pos,
            "ranked_on": ranked_on,
            # The ordering keys, carried so a change log can be read without the sheet.
            "edge_passed": cell.get("edge_passed"),
            "book_cm_excess_cagr": cell.get("book_cm_excess_cagr"),
            # The columns that stop the two above from being quoted alone. A rule holding a
            # position 86% of the time is this leaderboard measuring time in the market.
            "long_frac": cell.get("long_frac"),
            "exposure": cell.get("exposure"),
            "family": cell.get("family"), "kind": cell.get("kind"),
            "stale_sheet": stale,
        })
    return out


def why(row: dict) -> str:
    """One sentence for the change log: where this rule sits, and on what."""
    return (f"rank {row['rank']} on {row['sheet']} ({row['board_pos']} in the board's own "
            f"order, ranked on {row['ranked_on']}); "
            f"edge_passed={row.get('edge_passed')}, "
            f"book_cm_excess_cagr={row.get('book_cm_excess_cagr')}, "
            f"long_frac={row.get('long_frac')}. {NOT_PASSING}")


# ---------------------------------------------------------------------------- the change

def diff(current: list[str], target: list[dict], *, sheet: str | None = None) -> dict:
    """What to start and what to retire, with a reason per change.

    `current` is labels — what the portfolio holds — and `target` is `top()`'s rows. Every
    change carries a sentence fit to be stored in the change log and shown to a person,
    because a log that records only "retired MACD" cannot later answer why, and a portfolio
    nobody can audit is one nobody will believe.

    Order is not arbitrary: additions come in the target's order (rank 1 first, so a
    partial application still holds the head of the sheet) and retirements in the order the
    portfolio held them.
    """
    if sheet is None and target:
        sheet = target[0].get("sheet")
    where = sheet or "this sheet"
    held = list(dict.fromkeys(str(r) for r in (current or [])))
    want = [str(r["rule"]) for r in target]
    want_set, held_set = set(want), set(held)

    add = [{"rule": str(r["rule"]), "rank": r["rank"], "board_pos": r["board_pos"],
            "reason": f"joined the top {len(want)} on {where}: {why(r)}"}
           for r in target if str(r["rule"]) not in held_set]

    if want:
        gone = (f"dropped out of the top {len(want)} on {where} — the rules ahead of it "
                f"today are {', '.join(want)}")
    else:
        # An empty target is not "retire everything because the sheet went bad". It is a
        # sheet that offered nothing holdable at all, which is a different fact about a
        # different thing, and it is said as one.
        gone = (f"{where} offers no holdable rule today — every ranked row was untradable, "
                f"idle, a closet tracker, or a duplicate of one already picked")
    retire = [{"rule": r, "reason": gone} for r in held if r not in want_set]

    hold = [r for r in held if r in want_set]
    return {"sheet": sheet, "target": want, "add": add, "retire": retire,
            "hold": hold, "changed": bool(add or retire)}


def target_cells(row: dict) -> list[tuple[str, str, str]]:
    """A plan row's target, in the shape `stockhunt.portfolios.apply_membership` takes.

    Here rather than at the call site because the order is load-bearing: that function
    records the rank of every leg it adds at the moment it adds it, and the sheet is
    re-ranked nightly, so a target rebuilt in any other order writes down a rank nobody
    can check afterwards.

    **An orphan row raises rather than returning `[]`.** Its sheet is off the menu, so it
    has no target — and an empty target handed to `apply_membership` does not mean "leave
    this alone", it means "retire every leg". A caller looping over `plan()` and applying
    each row must not be able to empty a live basket because `catalog.py` failed to
    rebuild, and a raise is the only version of this that cannot be missed.
    """
    if row.get("action") in ("orphan", "empty"):
        raise ValueError(f"{row.get('sheet')} has no target today — apply nothing to it: "
                         f"{row.get('note')}")
    return [(r["cls"], r["tf"], r["rule"]) for r in (row.get("rows") or [])]


def _members(portfolios) -> dict[tuple[str, str], dict]:
    """Accept the shapes a caller plausibly has, and normalise once.

    A mapping keyed by `"cls_tf"` or by `(cls, tf)`, or a list of rows carrying `cls`, `tf`
    and `members`. Guessing at the call site is how two callers end up disagreeing about
    what a portfolio record looks like.
    """
    out: dict[tuple[str, str], dict] = {}

    def put(cls, tf, members, name=None):
        out[(str(cls), str(tf))] = {
            "name": name or f"follow-{cls}-{tf}",
            "members": [str(m) for m in (members or [])]}

    if isinstance(portfolios, dict):
        for key, val in portfolios.items():
            if isinstance(key, (tuple, list)) and len(key) == 2:
                cls, tf = key
            else:
                cls, _, tf = str(key).rpartition("_")
            if isinstance(val, dict):
                put(val.get("cls", cls), val.get("tf", tf),
                    val.get("members"), val.get("name"))
            else:
                put(cls, tf, val)
    else:
        for row in (portfolios or []):
            put(row.get("cls"), row.get("tf"), row.get("members"), row.get("name"))
    return out


def plan(portfolios, n: int = DEFAULT_N, doc: dict | None = None, timeframes=None,
         only_cls: str | None = None, only_tf: str | None = None) -> list[dict]:
    """The whole daily reconcile, as data. One row per follow-portfolio.

    Driven by `sheets()` rather than by the portfolios that already exist, which is what
    makes a sheet landing later get a portfolio on its own: a cell with no portfolio yet
    comes back with `create` set and every target rule in `add`.

    **A portfolio whose sheet is no longer on the menu is reported, never emptied.** A
    withdrawn sheet and a catalog that failed to rebuild look identical from here, and only
    one of them is a reason to retire a live book — so those rows come back as `orphan`
    with no changes, and a human decides.

    Computes only. Nothing here opens a database; the reconcile that applies a row is
    `stockhunt.portfolios.apply_membership`.
    """
    doc = catalog() if doc is None else doc
    have = _members(portfolios)
    generated = doc.get("generated_at")
    stale = set((doc.get("health") or {}).get("stale_sheets") or [])

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for cls, tf in sheets(doc, timeframes):
        if (only_cls and cls != only_cls) or (only_tf and tf != only_tf):
            continue
        seen.add((cls, tf))
        key = f"{cls}_{tf}"
        held = have.get((cls, tf))
        target = top(cls, tf, n, doc)
        row = diff((held or {}).get("members") or [], target, sheet=key)
        row.update({
            "cls": cls, "tf": tf,
            "name": (held or {}).get("name") or f"follow-{cls}-{tf}",
            "action": "reconcile" if held else "create",
            "create": held is None,
            "rows": target,
            "n": n,
            "stale_sheet": key in stale,
            "catalog_generated_at": generated,
            "note": NOT_PASSING,
        })
        if not target:
            # A sheet on the menu that ranks nothing is the orphan case one level in.
            # `catalog.py` writes a key with an empty `cells` list whenever the board could
            # not rank the sheet — a cell whose riskmatch and book stages have not run yet
            # does exactly that — and `diff` correctly answers "retire everything", because
            # that is what was asked of it. Acting on that answer would liquidate a live
            # basket because a research stage is mid-rebuild, so the plan reports the state
            # and changes nothing. `diff` keeps the honest arithmetic; the reconcile
            # declines to run it.
            row.update({
                "action": "empty", "create": False, "add": [], "retire": [],
                "changed": False,
                "note": (f"{key} is on the menu but ranks nothing holdable today, so this "
                         f"portfolio is left exactly as it is. A sheet the board cannot "
                         f"rank yet and a sheet whose rules were all cut look the same "
                         f"from here, and neither is a reason to empty a basket."),
            })
        rows.append(row)

    for (cls, tf), held in have.items():
        if (cls, tf) in seen:
            continue
        if (only_cls and cls != only_cls) or (only_tf and tf != only_tf):
            continue
        rows.append({
            "cls": cls, "tf": tf, "sheet": f"{cls}_{tf}", "name": held["name"],
            "action": "orphan", "create": False,
            "target": [], "rows": [], "add": [], "retire": [], "hold": held["members"],
            "changed": False, "n": n,
            "stale_sheet": False, "catalog_generated_at": generated,
            "note": (f"{cls} {tf} is not on the catalog's menu today, so this portfolio is "
                     f"left exactly as it is. A withdrawn sheet and a catalog that failed "
                     f"to rebuild look the same from here, and only one of them is a "
                     f"reason to retire anything."),
        })
    return rows


# ------------------------------------------------------------------------------ the CLI

def _print(rows: list[dict], missing: list[tuple[str, str]]) -> None:
    if not rows:
        print("\n  no follow-portfolios — nothing on the menu is at a book timeframe")
    for r in rows:
        head = f"  {r['sheet']:<20} {r['action']}"
        if r["action"] in ("orphan", "empty"):
            print(f"\n{head}")
            print(f"      {r['note']}")
            continue
        flag = "  (STALE SHEET)" if r["stale_sheet"] else ""
        print(f"\n{head}  {len(r['target'])}/{r['n']} held, "
              f"+{len(r['add'])} -{len(r['retire'])}{flag}")
        added = {a["rule"] for a in r["add"]}
        for row in r["rows"]:
            print(f"      {'+' if row['rule'] in added else ' '} {row['rule']:<28} "
                  f"rank {row['rank']} (board {row['board_pos']})  "
                  f"passed={row.get('edge_passed')} "
                  f"cm_excess_cagr={row.get('book_cm_excess_cagr')} "
                  f"long_frac={row.get('long_frac')}")
        for gone in r["retire"]:
            print(f"      - {gone['rule']:<28} {gone['reason']}")
    if missing:
        print("\n  ! on disk but not on the menu — re-run `python catalog.py`: "
              + ", ".join(f"{c}_{t}" for c, t in missing))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=DEFAULT_N)
    ap.add_argument("--cls", default=None)
    ap.add_argument("--tf", default=None)
    ap.add_argument("--catalog", default=None, help="read a catalog.json from elsewhere")
    ap.add_argument("--current", default=None,
                    help='JSON file of current membership: {"cls_tf": [rules]}')
    ap.add_argument("--json", action="store_true", help="print the plan as JSON")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="the only mode: compute the plan and print it")
    ap.add_argument("--apply", action="store_true", help="refused — see below")
    args = ap.parse_args()

    if args.apply:
        # Refused rather than absent, so nobody concludes the default already applied
        # something. Deciding and acting are split on purpose, and the actor is elsewhere.
        raise SystemExit("this module only computes the plan — applying it is "
                         "`stockhunt.portfolios.apply_membership`, wired up separately")

    doc = catalog(args.catalog)
    current: dict = {}
    if args.current:
        current = json.loads(Path(args.current).read_text(encoding="utf-8"))
    else:
        print("  no --current given: showing the plan against empty portfolios")

    rows = plan(current, args.top, doc, only_cls=args.cls, only_tf=args.tf)
    if args.json:
        print(json.dumps(rows, indent=1))
    else:
        _print(rows, unfollowed(doc))
    print(f"\n  dry run — nothing written.\n  {NOT_PASSING}")


if __name__ == "__main__":
    main()
