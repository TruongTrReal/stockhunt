"""The trial ledger: every strategy ever evaluated, recorded before it is scored.

Why a ledger exists at all
--------------------------
This project's binding constraint is not finding a rule that looks good — it is knowing
how many rules were looked at. The best of N worthless candidates reaches roughly
`sigma * sqrt(2 ln N)` by luck, so a result is only interpretable against an honest N.
`metrics.deflated_sharpe` takes `n_trials` for exactly this reason, and the number it is
given decides whether a row is evidence.

Counting trials from whatever CSV happens to be on disk gets that number wrong in the
flattering direction, every time. Rules that were tried and abandoned leave no file. A
grid that was narrowed after a first look leaves only its narrowed self. A strategy
someone wrote, ran once and deleted leaves nothing at all — and it still consumed a
trial. **The ledger is append-only and records intent before results exist**, which is
the only construction under which the count cannot quietly shrink.

    register(...)  ->  writes the row, refuses to overwrite an existing one
    count(...)     ->  how many trials a given scope has consumed, for DSR
    pending(...)   ->  registered but never scored: exactly the rows a naive count misses

This is pre-registration in the clinical-trials sense and it is deliberately mildly
inconvenient. Two findings in this repo have already been retracted; both would have been
caught by an honest N.

Run::

    python strategies/trials.py --list
    python strategies/trials.py --register ibs --scope us_stocks/1d --why "published rule"
    python strategies/trials.py --count --scope us_stocks/1d
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER = REPO_ROOT / "data" / "reference" / "trials.csv"

FIELDS = ("label", "scope", "registered_on", "author", "why", "hypothesis", "scored_on",
          "outcome")


def _read() -> list[dict]:
    if not LEDGER.exists():
        return []
    with open(LEDGER, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _write(rows: list[dict]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def register(label: str, scope: str, why: str = "", hypothesis: str = "",
             author: str = "", on: str | None = None) -> bool:
    """Record a trial. Returns False if this (label, scope) is already registered.

    Registering is idempotent and never overwrites: the original registration date is the
    one that matters, because it is what establishes the claim was made before the result
    was seen.
    """
    rows = _read()
    if any(r["label"] == label and r["scope"] == scope for r in rows):
        return False
    rows.append({
        "label": label, "scope": scope,
        "registered_on": on or date.today().isoformat(),
        "author": author, "why": why, "hypothesis": hypothesis,
        "scored_on": "", "outcome": "",
    })
    _write(rows)
    return True


def register_many(labels, scope: str, why: str = "", hypothesis: str = "",
                  author: str = "") -> tuple[int, int]:
    """Bulk registration. Returns (added, already_present).

    One read and one write for the whole batch, not one of each per label. The obvious
    implementation -- `sum(register(l, ...) for l in labels)` -- rewrites the entire
    ledger once per label, so registering a 1,000-cell grid across nine scopes rewrote a
    multi-megabyte file nine thousand times and took longer than the backtest it was
    registering. Same semantics: still idempotent, still never overwrites an existing
    registration date, and duplicates WITHIN the batch are collapsed too.
    """
    labels = list(labels)
    rows = _read()
    seen = {(r["label"], r["scope"]) for r in rows}
    today = date.today().isoformat()
    added = 0
    for label in labels:
        if (label, scope) in seen:
            continue
        seen.add((label, scope))
        rows.append({
            "label": label, "scope": scope, "registered_on": today,
            "author": author, "why": why, "hypothesis": hypothesis,
            "scored_on": "", "outcome": "",
        })
        added += 1
    if added:
        _write(rows)
    return added, len(labels) - added


def mark_scored(label: str, scope: str, outcome: str = "", on: str | None = None) -> bool:
    rows = _read()
    hit = False
    for r in rows:
        if r["label"] == label and r["scope"] == scope:
            r["scored_on"] = on or date.today().isoformat()
            r["outcome"] = outcome
            hit = True
    if hit:
        _write(rows)
    return hit


def mark_scored_many(labels, scope: str, outcome: str = "",
                     on: str | None = None) -> int:
    """Bulk `mark_scored`. Returns how many rows were marked.

    One read and one write for the batch, same reason as `register_many`: the per-label
    form rewrites the whole ledger once per label, and this batch marks thousands.
    """
    labels = set(labels)
    rows = _read()
    when = on or date.today().isoformat()
    hit = 0
    for r in rows:
        if r["scope"] == scope and r["label"] in labels:
            r["scored_on"] = when
            r["outcome"] = outcome
            hit += 1
    if hit:
        _write(rows)
    return hit


def count(scope: str | None = None) -> int:
    """Trials consumed, for `metrics.deflated_sharpe(n_trials=...)`.

    Counts *registered* trials, not scored ones. A candidate that was registered and then
    abandoned still consumed a look at the data, and excluding it is precisely the
    selection effect the deflation exists to price.
    """
    rows = _read()
    if scope is None:
        return len(rows)
    return sum(1 for r in rows if r["scope"] == scope)


def pending(scope: str | None = None) -> list[dict]:
    return [r for r in _read()
            if not r["scored_on"] and (scope is None or r["scope"] == scope)]


def scopes() -> dict[str, int]:
    out: dict[str, int] = {}
    for r in _read():
        out[r["scope"]] = out.get(r["scope"], 0) + 1
    return dict(sorted(out.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--count", action="store_true")
    ap.add_argument("--pending", action="store_true")
    ap.add_argument("--register", nargs="+", metavar="LABEL")
    ap.add_argument("--scope", default=None, help="e.g. us_stocks/1d")
    ap.add_argument("--why", default="")
    ap.add_argument("--hypothesis", default="")
    ap.add_argument("--author", default="")
    args = ap.parse_args()

    if args.register:
        if not args.scope:
            ap.error("--register needs --scope (e.g. us_stocks/1d)")
        added, dup = register_many(args.register, args.scope, args.why,
                                   args.hypothesis, args.author)
        print(f"registered {added}, already present {dup} -> {LEDGER}")
        print(f"{args.scope} has now consumed {count(args.scope)} trials")
        return 0

    if args.count:
        if args.scope:
            print(f"{args.scope}: {count(args.scope)} trials")
        else:
            for s, n in scopes().items():
                print(f"  {s:24} {n:5d}")
            print(f"  {'TOTAL':24} {count():5d}")
        return 0

    if args.pending:
        rows = pending(args.scope)
        print(f"{len(rows)} registered but never scored:")
        for r in rows[:40]:
            print(f"  {r['scope']:18} {r['label']:32} registered {r['registered_on']}")
        return 0

    rows = _read()
    print(f"{len(rows)} trials in {LEDGER}")
    for r in rows[-30:]:
        state = f"scored {r['scored_on']} {r['outcome']}" if r["scored_on"] else "PENDING"
        print(f"  {r['scope']:18} {r['label']:32} {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
