"""Prove the live board and the baked board are the same document.

The leaderboard used to be a snapshot: `payload.py` read 131 CSVs, joined and ranked them,
and wrote `web/data.js`. It now reads `results.db`, which `tools/ingest_results.py` fills
from those same CSVs. Two routes to one number, and the only thing that makes the second
one safe to switch on is being able to show it agrees with the first.

This is `tools/golden.py`'s contract, one level up. That harness hashes position series
across a refactor of the rule loop; this one hashes the **rendered leaderboard** across a
refactor of where its rows come from. Same rule: capture before, verify after, nonzero exit
on any drift, and a failure that names the sheet rather than saying something moved.

What is compared, per (class, timeframe): the whole sheet dict `board_rank.build_sheet`
returns — every ranked row, every per-asset table under it, and every header field
including the population statistics (`noise_ceiling`, `exposure_corr`, `n_rules`) that are
recomputed on each call rather than stored.

**Comparison is on the JSON text, not on the parsed values, and that is deliberate.** A
negative zero is equal to zero in Python and prints as "−0.0%" on the page, so a
value-wise diff cannot see the one class of drift this has already caught: rounding a
metric inside the store instead of inside the ranker silently turned every `-0.0` into
`0.0` on three of twenty sheets. `json.dumps` keeps the sign, so the text comparison
catches it.

The baseline is regenerated, never hand-edited. If a stage legitimately changes a number,
re-run the pipeline, re-ingest, eyeball the diff this prints, and capture again.

Run::

    python tools/ingest_results.py
    python tools/test_board_equivalence.py capture     # freeze today's board
    python tools/test_board_equivalence.py verify      # exits nonzero on any drift
    python tools/test_board_equivalence.py verify --sheets us_stocks/1d
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for p in (str(REPO), str(REPO / "Stockhunt Dashboard")):
    if p not in sys.path:
        sys.path.insert(0, p)

import board_rank                                                  # noqa: E402
from dash_config import GROUPS, TIMEFRAMES                         # noqa: E402

GOLDEN = REPO / "tools" / "golden" / "board.json"


def snapshot(sheets: list[tuple[str, str]] | None = None) -> dict:
    """`"<class>/<tf>" -> the sheet dict, as JSON text.`

    Stored as text rather than as nested objects so the file itself is the comparison —
    see the module docstring on negative zero.
    """
    universes = {cls: list(u) for _k, cls, _l, u in GROUPS}
    wanted = sheets or [(cls, tf) for _k, cls, _l, _u in GROUPS for tf in TIMEFRAMES]
    out = {}
    for cls, tf in wanted:
        sheet = board_rank.build_sheet(cls, tf, universes.get(cls, []))
        out[f"{cls}/{tf}"] = json.dumps(sheet, sort_keys=True)
    return out


def _describe(a: str | None, b: str | None) -> str:
    """Say WHERE two sheets differ, at the coarsest useful grain.

    A sheet is ~200 kB of JSON and printing the diff is not reading it. Naming the top-level
    key, then the row, then the field is what turns a failure into a thing to go and look
    at — the same reason `golden.py` digests per (sheet, rule) instead of per run.
    """
    if a is None:
        return "missing from the baseline (a new sheet — capture again)"
    if b is None:
        return "gone: the store has no rows for it"
    da, db = json.loads(a), json.loads(b)
    if da is None or db is None:
        return f"one side is empty: baseline {da is not None}, now {db is not None}"
    notes = []
    for k in sorted(set(da) | set(db)):
        ja = json.dumps(da.get(k), sort_keys=True)
        jb = json.dumps(db.get(k), sort_keys=True)
        if ja == jb:
            continue
        if k != "rows":
            notes.append(f"      {k}: {ja[:60]} -> {jb[:60]}")
            continue
        ra, rb = da["rows"], db["rows"]
        if len(ra) != len(rb):
            notes.append(f"      rows: {len(ra)} -> {len(rb)}")
            continue
        for i, (xa, xb) in enumerate(zip(ra, rb)):
            if json.dumps(xa, sort_keys=True) == json.dumps(xb, sort_keys=True):
                continue
            fields = [f for f in sorted(set(xa) | set(xb))
                      if json.dumps(xa.get(f), sort_keys=True)
                      != json.dumps(xb.get(f), sort_keys=True)]
            notes.append(f"      row {i} ({xa.get('rule')}): {', '.join(fields)}")
    return "\n".join(notes) or "identical text, differing bytes"


def parse_sheets(items: list[str] | None):
    if not items:
        return None
    out = []
    for item in items:
        if "/" not in item:
            raise SystemExit(f"bad sheet {item!r}; expected <class>/<tf>")
        cls, tf = item.split("/", 1)
        out.append((cls, tf))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["capture", "verify"])
    ap.add_argument("--sheets", nargs="+", default=None,
                    help="limit to these, as <class>/<tf>. Verify only compares what it "
                         "builds, so a narrowed run proves less")
    args = ap.parse_args()

    sheets = parse_sheets(args.sheets)
    now = snapshot(sheets)

    if args.mode == "capture":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(now, indent=1, sort_keys=True), encoding="utf-8")
        rows = sum(len(json.loads(v)["rows"]) for v in now.values() if json.loads(v))
        print(f"captured {len(now)} sheets, {rows} ranked rows -> "
              f"{GOLDEN.relative_to(REPO)}")
        return 0

    if not GOLDEN.exists():
        raise SystemExit(f"no baseline at {GOLDEN} — run `capture` first")
    old = json.loads(GOLDEN.read_text(encoding="utf-8"))

    drift = 0
    for key in sorted(set(old) | set(now)):
        if key not in now:
            continue                       # narrowed run; it proves nothing about this one
        a, b = old.get(key), now[key]
        if a == b:
            n = json.loads(b)
            print(f"  {key:<20} ok   ({len(n['rows']) if n else 0} rows)")
            continue
        drift += 1
        print(f"  {key:<20} DRIFT")
        print(_describe(a, b))

    if drift:
        print(f"\n{drift} sheet(s) differ from the baseline. Either a stage changed a "
              f"number — re-capture — or the store and the CSVs disagree, which is a bug.")
        return 1
    print(f"\n{len(now)} sheets identical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
