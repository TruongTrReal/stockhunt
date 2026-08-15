"""Create new strategy files — one, or a batch — already wired into every gate.

The point of the split into `published/<name>.py` is that adding a strategy is adding a
file: `registry._discover()` imports the folder and picks up anything defining
`position` and `GRID`, so there is no list to update and no registration call to forget.
This module is the front door to that, and it exists to make the *disciplined* path the
easy one.

What it does beyond writing a file
----------------------------------
A new strategy is a new trial, and an untracked trial is how a search silently inflates
its own significance. So scaffolding also **pre-registers** the strategy in
`strategies/trials.py` before any result exists. That ordering is the whole value: the
ledger records what you intended to test, on what scope, and why — written at a moment
when you cannot yet know whether it worked.

Batch mode takes a CSV so a sweep of fifty ideas is one command and fifty ledger rows,
rather than fifty chances to skip the bookkeeping.

Run::

    # one strategy
    python strategies/scaffold.py new rsi_divergence \\
        --family reversion --source "Author (2019), 'Title'" \\
        --rule "Long when price makes a lower low and RSI does not." \\
        --scope us_stocks/1d --why "classic divergence, never tested here"

    # a batch: CSV with columns name,family,source,rule[,params]
    python strategies/scaffold.py batch ideas.csv --scope us_stocks/1d

    # then, always
    python strategies/tests/test_causality.py --rules rsi_divergence
    cd "walk-forward optimization" && python portfolio_wf.py --rules rsi_divergence --pit
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategies import trials                                   # noqa: E402

PUBLISHED = REPO_ROOT / "strategies" / "published"

FAMILIES = ("trend", "reversion", "calendar", "volatility", "regime", "breadth", "other")

TEMPLATE = '''"""{rule}"""

from __future__ import annotations

import numpy as np
{extra_imports}
from strategies._indicators import _state_machine

RULE = {rule!r}
SOURCE = {source!r}
FAMILY = {family!r}
DRAFT = True            # flip to False when position() is implemented.
                        # Drafts are EXCLUDED from CATALOG so a half-written
                        # file cannot quietly enter cells() and the trial count.
ANCHOR = False
CLASSES = None          # None = every asset class; else e.g. ("us_stocks", "us_etfs")
NOTE = ""               # anything a reader must know before quoting a result

# grid[0] IS the published parameter set; everything after it is a variant.
# Every extra cell is another trial and is paid for in the deflated Sharpe, so add
# variants because the source specifies them, not to widen the search.
GRID = (
{grid}
)


def position(df, close, bpy, {signature}):
    """{rule}

    Contract:
      * return one float64 value per bar: 1.0 long, 0.0 flat, -1.0 short
      * bar t may use information from bars <= t ONLY. Test this by truncation, never by
        reading the code — `strategies/tests/test_causality.py` is the gate, and it is
        what caught `np.nanmedian(whole_series)` after review had missed it twice
      * derive every lookback from `bpy` via `_bars(bpy, years)`, so the rule means the
        same thing on daily and 4-hour bars
      * no NaN policy needed here; `registry.build` maps NaN/inf to 0.0
    """
    raise NotImplementedError("implement {name}")
'''


def _grid_literal(params: dict, variants: list[dict]) -> str:
    rows = [params] + variants
    if not rows or not params:
        return "        {},"
    return "\n".join(f"        {r!r}," for r in rows)


def _signature(params: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in params.items()) if params else ""


def create(name: str, rule: str, source: str, family: str,
           params: dict | None = None, variants: list[dict] | None = None,
           extra_imports: str = "", force: bool = False) -> Path:
    if family not in FAMILIES:
        raise SystemExit(f"family must be one of {FAMILIES}, got {family!r}")
    path = PUBLISHED / f"{name}.py"
    if path.exists() and not force:
        raise SystemExit(f"{path} exists — the filename IS the strategy's identity and "
                         f"every result CSV is keyed on it. Pass --force only if you "
                         f"mean to redefine it, and expect its history to be orphaned.")
    params = params or {}
    path.write_text(TEMPLATE.format(
        name=name, rule=rule, source=source, family=family,
        grid=_grid_literal(params, variants or []),
        signature=_signature(params),
        extra_imports=extra_imports,
    ), encoding="utf-8")
    return path


def _register(names, scope, why, hypothesis, author):
    if not scope:
        print("  ! not registered — pass --scope to pre-register these as trials")
        return
    added, dup = trials.register_many(names, scope, why, hypothesis, author)
    print(f"  pre-registered {added} trial(s) on {scope} "
          f"({dup} already present); {scope} has consumed {trials.count(scope)}")


def cmd_new(args) -> int:
    params = {}
    for kv in args.param or []:
        k, _, v = kv.partition("=")
        try:
            params[k] = float(v)
        except ValueError:
            params[k] = v
    path = create(args.name, args.rule or f"TODO: describe {args.name}",
                  args.source or "TODO: citation", args.family, params,
                  force=args.force)
    print(f"wrote {path}")
    _register([args.name], args.scope, args.why, args.hypothesis, args.author)
    print(f"\nnext:\n  1. implement position() in {path.name}, then flip DRAFT = False\n"
          f"  2. python strategies/tests/test_causality.py --rules {args.name}\n"
          f"  3. cd 'walk-forward optimization' && python portfolio_wf.py "
          f"--rules {args.name} --pit\n"
          f"  4. python strat_wf.py --tf 1d --rules {args.name}   # scoped -> *.partial\n"
          f"  5. python merge_book.py --class us_stocks --tf 1d --rules {args.name}\n"
          f"\n  3 and 4 are the cheap look. 5 puts it on the BOOK leaderboard without "
          f"re-scoring\n  the ~400 rules already there — only the panel columns are "
          f"relative, and those are\n  re-derived from the sheet. A place on "
          f"edge_standard.csv still needs riskmatch_wf.py.")
    return 0


def cmd_batch(args) -> int:
    """Scaffold many strategies from a CSV: name,family,source,rule."""
    with open(args.csv, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit(f"{args.csv} has no rows")
    missing = {"name", "family"} - set(rows[0])
    if missing:
        raise SystemExit(f"{args.csv} needs columns {sorted(missing)}")

    made = []
    for r in rows:
        name = r["name"].strip()
        if not name:
            continue
        try:
            create(name, r.get("rule") or f"TODO: describe {name}",
                   r.get("source") or "TODO: citation", r.get("family", "other").strip(),
                   force=args.force)
            made.append(name)
            print(f"  wrote published/{name}.py")
        except SystemExit as exc:
            print(f"  skipped {name}: {exc}")
    print(f"\n{len(made)} of {len(rows)} scaffolded")
    _register(made, args.scope, args.why, args.hypothesis, args.author)
    if made:
        print(f"\nnext:\n  1. implement position() in each\n"
              f"  2. python strategies/tests/test_causality.py\n"
              f"  3. cd 'walk-forward optimization' && python portfolio_wf.py "
              f"--rules {' '.join(made[:4])}{' ...' if len(made) > 4 else ''} --pit")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--scope", help="e.g. us_stocks/1d — pre-registers the trial")
    common.add_argument("--why", default="", help="why this is worth a trial")
    common.add_argument("--hypothesis", default="", help="what you expect, before running")
    common.add_argument("--author", default="")
    common.add_argument("--force", action="store_true")

    n = sub.add_parser("new", parents=[common], help="one strategy")
    n.add_argument("name")
    n.add_argument("--family", default="other", choices=FAMILIES)
    n.add_argument("--source", default="")
    n.add_argument("--rule", default="")
    n.add_argument("--param", action="append", metavar="k=v")
    n.set_defaults(func=cmd_new)

    b = sub.add_parser("batch", parents=[common], help="many, from a CSV")
    b.add_argument("csv")
    b.set_defaults(func=cmd_batch)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
