r"""Put the top N rules from every sheet on the desk, as books.

The bulk form of the switch on the backtest page. It reads the same `catalog.json` that
switch reads — so the same ranking, the same tradability test, the same order the board
shows — and registers one $100,000 book per (class, timeframe, rule).

    python promote_top.py --dry-run          # what would be registered
    python promote_top.py --top 3            # every class, every timeframe
    python promote_top.py --top 3 --cls us_stocks --tf 1d
    python promote_top.py --retire-others    # ...and switch off anything else promoted

Registration is all this does. The desk applies it on its next control tick, within thirty
seconds, and it needs no restart — `desk_control` attaches books to a running node.

**Read the money before running it.** Each book is $100,000 and they are independent, so
`--top 3` over four classes and two timeframes deploys $2.4M of paper capital and holds
several hundred instruments. The summary prints the total before it writes anything.
"""

from __future__ import annotations

import argparse
import json

import paper_config
from stockhunt import deskdb


def catalog() -> dict:
    path = (paper_config.PUBLISH_DIR / "catalog.json"
            if paper_config.PUBLISH_DIR else None)
    if path is None or not path.exists():
        raise SystemExit("no catalog.json — run `python catalog.py` first")
    return json.loads(path.read_text(encoding="utf-8"))


def picks(doc: dict, top: int, only_cls: str | None,
          only_tf: str | None) -> list[dict]:
    """The first `top` TRADABLE cells on each sheet, in the board's own order.

    Untradable rows are skipped rather than counted, so `--top 3` means three books and
    not "three rows, of which two were combos we could not build". They are still ranked
    ahead — the skip is about what can run, never about what scored.
    """
    book = doc.get("book") or {}
    timeframes = book.get("timeframes") or ["1d"]
    out = []
    for key, sheet in (doc.get("sheets") or {}).items():
        cls, _, tf = key.rpartition("_")
        if tf not in timeframes:
            continue
        if only_cls and cls != only_cls:
            continue
        if only_tf and tf != only_tf:
            continue
        if not (book.get("names") or {}).get(cls):
            print(f"  ! {key}: no names live in {cls}; skipped")
            continue
        taken = 0
        for cell in sheet.get("cells", []):
            if taken >= top:
                break
            if not cell.get("tradable"):
                continue
            out.append({"cls": cls, "tf": tf, "rule": cell["rule"],
                        "names": book["names"][cls],
                        "capital": float(book.get("capital") or 100_000.0),
                        "benchmark": (book.get("benchmark") or {}).get(cls),
                        "stale": key in (doc.get("health") or {}).get("stale_sheets", []),
                        "family": cell.get("family")})
            taken += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--cls", default=None)
    ap.add_argument("--tf", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--retire-others", action="store_true",
                    help="switch off any house book not in this selection")
    args = ap.parse_args()

    doc = catalog()
    chosen = picks(doc, args.top, args.cls, args.tf)
    if not chosen:
        raise SystemExit("nothing to promote — check --cls/--tf against the catalog")

    total = sum(c["capital"] for c in chosen)
    print(f"\n  {len(chosen)} books, ${total:,.0f} of paper capital\n")
    by_sheet: dict[str, list[dict]] = {}
    for c in chosen:
        by_sheet.setdefault(f"{c['cls']}_{c['tf']}", []).append(c)
    for key, cells in by_sheet.items():
        flag = " (STALE SHEET)" if cells[0]["stale"] else ""
        print(f"  {key}{flag}  {cells[0]['names']} names, "
              f"${cells[0]['capital'] / cells[0]['names']:,.0f} a name")
        for c in cells:
            print(f"      {c['rule']:<28} {c['family']}")

    if args.dry_run:
        print("\n  dry run — nothing written")
        return

    deskdb.connect()
    wanted, created, revived = set(), 0, 0
    for c in chosen:
        name = f"{c['cls']}-{c['tf']}-{c['rule'].lower()}"
        wanted.add(name)
        before = deskdb.registration(f"str_00_{name}")
        row = deskdb.register("00", name, c["cls"], [], c["tf"], c["capital"],
                              kind="book", rule=c["rule"], benchmark=c["benchmark"])
        if before is None:
            created += 1
        elif before["want"] == "retired" or before["state"] in deskdb.REGISTRATION_DONE:
            revived += 1

    print(f"\n  {created} new, {revived} revived, "
          f"{len(chosen) - created - revived} already live")

    if args.retire_others:
        gone = 0
        for r in deskdb.registrations("00"):
            if r["kind"] == "book" and r["name"] not in wanted and r["want"] != "retired":
                deskdb.set_want("00", r["strategy_id"], "retired")
                gone += 1
        print(f"  {gone} retired for not being in this selection")

    print("\n  The desk applies these on its next tick — no restart needed.\n"
          "  Each book warms up 1,500 bars per name first, so give it a few minutes.")


if __name__ == "__main__":
    main()
