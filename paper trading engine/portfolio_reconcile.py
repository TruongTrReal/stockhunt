r"""Make every follow-portfolio hold its sheet's top N. Run it once a day.

The other half of `portfolio_follow.py`. That module decides and this one acts, and the
split is the point: a plan you can print is a plan you can check before it moves money,
and `--dry-run` is only honest if the deciding half genuinely cannot write.

    python portfolio_reconcile.py                  # what would change. Writes NOTHING
    python portfolio_reconcile.py --apply          # ...do it
    python portfolio_reconcile.py --apply --cls us_stocks --tf 1d
    python portfolio_reconcile.py --apply --top 5

**A separate process, like `rotation_manager.py`, and for the same reason.** It writes to
`stockhunt.deskdb` and the desk applies what it finds on its next control tick, within
thirty seconds and with no restart — `desk_control` attaches and retires books on a
running node. So this can be edited, re-run and rescheduled without touching the trading
process, and if it never runs the desk simply keeps holding what it already holds.

**It creates the portfolio a sheet does not have yet.** The plan is driven by the
catalog's menu rather than by the portfolios that exist, so a sheet scored next month gets
its basket the first time this runs after `catalog.py` publishes it. That is the whole
reason the 5m sheets need no special handling: they are absent today and will be ordinary
tomorrow.

**Two states change nothing, deliberately.** A portfolio whose sheet has left the menu
(`orphan`) and a sheet that ranks nothing holdable (`empty`) both look exactly like a
catalog mid-rebuild, and only one of them would be a reason to liquidate a live basket.
Both are printed and skipped; a human decides.

**Ranking is not passing.** Nothing on any sheet clears this project's acceptance gates,
so the top N of a sheet are the least-bad N. This script deploys paper capital against
that ordering to keep a forward record; it is not a recommendation, and the total it
prints before writing is there to be read.
"""

from __future__ import annotations

import argparse
import sys

import paper_config
import portfolio_follow
from stockhunt import portfolios as pf

# One basket per sheet, funded like a promoted book so that a portfolio's curve and a
# single book's curve are read on the same axis.
CAPITAL = paper_config.BOOK_CAPITAL

# Whose portfolios these are. The house account, the same one `/v1/house/strategies`
# promotes onto — a member's follow-portfolios are reconciled by the same code through
# `--account`, but the desk's own are the default because they are what the board shows.
HOUSE = "00"


def name_for(cls: str, tf: str, n: int) -> str:
    """What a follow-portfolio is called, and it has to be derived rather than typed.

    `deskdb` is `UNIQUE(account, name)` and `portfolios.create` refuses a duplicate, so
    this string is the key that makes re-running safe: the same sheet must produce the
    same name on every pass or the second run creates a second basket holding the same
    five rules on another $100,000.

    **ASCII, lower case, no spaces, and that is not cosmetic.** A leg's name reaches
    Nautilus through `StrategyId`, whose Rust constructor PANICS on a non-ASCII character
    -- not an exception a Python `try` can catch, a process abort. Naming these baskets
    with an em-dash took the live desk down eleven times in a restart loop before it was
    identified. `portfolios._leg_name` folds the name too, so a portfolio named by hand
    cannot repeat it; this keeps the generated ones clean at the source.
    """
    return f"top{n}-{cls}-{tf}"


def existing(account: str) -> list[dict]:
    """The follow-portfolios this account already has, in the shape `plan()` accepts.

    Retired baskets are left out: a portfolio somebody switched off must not be quietly
    refilled by the nightly pass, which would be the desk overruling its owner.
    """
    rows = []
    for row in pf.listing(account):
        if row.get("kind") != "follow" or row.get("want") == "retired":
            continue
        rows.append({
            "cls": row.get("source_cls"), "tf": row.get("source_tf"),
            "name": row.get("name"),
            "members": [leg["rule"] for leg in (row.get("legs") or [])
                        if leg.get("rule")],
            "portfolio_id": row["portfolio_id"],
        })
    return rows


def benchmarks(doc: dict) -> dict:
    """Each class's declared baseline, straight off the catalog.

    Passed into `apply_membership` rather than left to be inferred from a book's
    holdings — a benchmark that differs from the strategy in more than the signal is the
    error this repo warns about most, and a hundred-name book has no single instrument to
    infer one from anyway.
    """
    return dict(((doc.get("book") or {}).get("benchmark") or {}))


def apply(rows: list[dict], account: str, doc: dict, capital: float,
          verbose: bool = True) -> dict:
    """Write the plan. Only rows that actually change anything are touched.

    `apply_membership` is idempotent, so running this twice in a night is free; the skip
    below is about the LOG rather than about correctness — a pass that rewrote every
    basket every night would bury the four real changes among a hundred that said nothing.
    """
    marks = benchmarks(doc)
    made = changed = 0
    for row in rows:
        if row["action"] in ("orphan", "empty"):
            continue
        cls, tf = row["cls"], row["tf"]
        portfolio_id = row.get("portfolio_id")

        if row["create"]:
            try:
                created = pf.create(
                    account, name_for(cls, tf, row["n"]), "follow",
                    capital=capital, source_cls=cls, source_tf=tf, top_n=row["n"])
            except ValueError:
                # The name is taken, and the only way to get here is a basket that was
                # RETIRED — `existing()` leaves those out, so the plan cannot see it and
                # asks for a create. Skipping is the right answer twice over: refilling it
                # would be the nightly pass overruling somebody who switched it off, and
                # crashing would let one retired basket stop every other sheet from
                # reconciling for as long as it existed.
                if verbose:
                    print(f"  . {name_for(cls, tf, row['n'])} was retired — left alone. "
                          f"Resume it to have this sheet followed again.")
                continue
            portfolio_id = created["portfolio_id"]
            made += 1
            if verbose:
                print(f"  + {created['name']}")

        if not row["changed"] and not row["create"]:
            continue

        target = portfolio_follow.target_cells(row)
        result = pf.apply_membership(
            portfolio_id, target,
            reason=f"{row['sheet']} top {row['n']}, as ranked on "
                   f"{row.get('catalog_generated_at') or 'the current catalog'}",
            account=account, benchmarks=marks)
        if result["added"] or result["removed"]:
            changed += 1
            if verbose:
                print(f"    {row['sheet']}: +{len(result['added'])} "
                      f"-{len(result['removed'])}, "
                      f"{result['n_legs']} legs at ${result['leg_capital']:,.0f}")
    return {"created": made, "changed": changed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the changes. Without it, nothing is written")
    ap.add_argument("--top", type=int, default=portfolio_follow.DEFAULT_N,
                    help="how many rules a basket holds")
    ap.add_argument("--account", default=HOUSE)
    ap.add_argument("--capital", type=float, default=CAPITAL,
                    help="the pot per portfolio, split equally across its legs")
    ap.add_argument("--cls")
    ap.add_argument("--tf")
    args = ap.parse_args(argv)

    try:
        doc = portfolio_follow.catalog()
    except SystemExit as exc:
        print(exc)
        return 2

    have = existing(args.account)
    by_cell = {(r["cls"], r["tf"]): r for r in have}
    rows = portfolio_follow.plan(have, n=args.top, doc=doc,
                                 only_cls=args.cls, only_tf=args.tf)
    for row in rows:
        held = by_cell.get((row["cls"], row["tf"]))
        if held:
            row["portfolio_id"] = held["portfolio_id"]

    portfolio_follow._print(rows, portfolio_follow.unfollowed(doc))

    live = [r for r in rows if r["action"] not in ("orphan", "empty")]
    new = [r for r in live if r["create"]]
    moving = [r for r in live if r["changed"] and not r["create"]]

    # Printed before anything is written, the way `promote_top.py` prints its total. A
    # basket is $100,000 and there is one per sheet, so "reconcile everything" is a much
    # larger commitment than the word suggests.
    print(f"\n  {len(live)} follow-portfolios, {len(new)} of them new, "
          f"{len(moving)} with membership changes")
    print(f"  ${(len(new) + len(have)) * args.capital:,.0f} of paper capital in total "
          f"if this is applied")
    print(f"  {portfolio_follow.NOT_PASSING}")

    if not args.apply:
        print("\n  dry run — nothing written. Add --apply to write it.")
        return 0

    result = apply(rows, args.account, doc, args.capital)
    print(f"\n  wrote {result['created']} new portfolios, "
          f"{result['changed']} membership changes. The desk applies them on its next "
          f"control tick; nothing needs restarting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
