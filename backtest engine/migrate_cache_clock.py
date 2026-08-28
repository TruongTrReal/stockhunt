"""Restamp an intraday cache that was written before its class's clock was declared.

    python migrate_cache_clock.py --class commodities            # what it would do
    python migrate_cache_clock.py --class commodities --write    # do it

`config.INTRADAY_CLOCK` says what wall clock each class's bars are stamped in and what
this repo's parquet is supposed to hold. `td_loader.fetch` has applied that on every fetch
since 2026-08-28. This script is the one-off for everything fetched **before** that, and
it exists because `data/` is gitignored: a cache is not recoverable from history, only
from the vendor, and a commodity refetch is ~13k credits and hours.

**The bug it repairs.** Twelve Data returned commodity intraday bars stamped in
`Australia/Sydney` and declared nothing — `meta.exchange_timezone` is `null` for the
class, while crypto says `UTC` and equities correctly say `America/New_York`. The repo's
docs said UTC. See `config.INTRADAY_CLOCK` for the measurement that settled it.

Three rules decide what happens to each timeframe, and the third is the one that is easy
to get wrong:

* **`1d` is never touched.** A daily commodity bar is on a third convention again — the
  vendor's own roll-up on a fixed 21:00 UTC boundary, not the Sydney clock — and a date is
  not a time. Relabelling it would move real bars between real days.
* **A size that divides one hour is RELABELLED.** The offset is a whole number of hours
  (+10 AEST, +11 AEDT), so `1m`, `5m`, `15m`, `30m` and `1h` bars cover exactly the same
  windows before and after; only the label moves. This is not an approximation — a 1m bar
  stamped 08:00 Sydney *is* the minute beginning 22:00 UTC.
* **Everything else is RE-DERIVED, because a whole-hour shift does not preserve its
  grid.** `4h` is the case: `10 % 4 == 2` and `11 % 4 == 3`, so relabelled 4h bars would
  sit at 02:00/06:00/... UTC in summer and 01:00/05:00/... in winter — not the windows any
  other class's 4h bars cover, and not even the same windows as themselves across a DST
  change. `2m` and `3m` are re-derived too, though they would survive a relabel, because
  they are derived bars and there should be one path that builds them.

The re-derivations run through `resample_intraday.py`, which is the only place 2m/3m/4h
are ever built. `4h` is rebuilt from the corrected `1h` and not from `1m`: the 1h cache
holds all five commodity symbols from 2020-01-20, the 1m cache three from 2020-10-01.

**Idempotent by measurement, not by a marker.** Before writing anything it asks
`check_data.clock_verdict` whether the cache on disk already agrees with the declared
clock, and refuses if it does. A marker file can be deleted, copied or missed; a
double-shifted cache is silent, and this is the one operation in the repo that cannot be
undone by re-running it.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

import check_data
from config import CLASSES, DATA_DIR, TIMEFRAMES, cache_dir, cache_tz, vendor_tz
from td_loader import to_cache_clock_frame

# Where the pre-migration copy goes. Under `data/` so it sits beside what it backs up and
# inherits the gitignore, and named with a leading underscore so it is not a class
# directory — `config.CLASS_DIR` maps class names to folders and nothing globs `data/*`.
BACKUP_DIR = DATA_DIR / "_pre_clock_fix"


def _minutes(timeframe: str) -> int:
    return int(timeframe[:-1]) * (60 if timeframe.endswith("h") else 1)


def relabel_safe(timeframe: str) -> bool:
    """Does a whole-hour shift leave this size on the same grid?

    True exactly when the bar width divides an hour. The offsets in
    `config.INTRADAY_CLOCK` are all whole hours, so this is the complete condition — and
    it is arithmetic rather than a list, so a new timeframe answers for itself.
    """
    return TIMEFRAMES[timeframe]["intraday"] and 60 % _minutes(timeframe) == 0


# What to rebuild once the relabelling is done, as (target, source) through
# `resample_intraday.py`. Ordered so `4h` reads a `1h` that has already been corrected.
DERIVE = [("2m", "1m"), ("3m", "1m"), ("4h", "1h")]


def plan(asset_class: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Which cached timeframes get relabelled, and which get rebuilt."""
    cached = [tf for tf in TIMEFRAMES
              if any(cache_dir(asset_class, tf).glob("*.parquet"))]
    derived = {tf for tf, _ in DERIVE}
    shift = [tf for tf in cached if tf not in derived and relabel_safe(tf)]
    rebuild = [(tf, src) for tf, src in DERIVE
               if tf in cached and any(cache_dir(asset_class, src).glob("*.parquet"))]
    return shift, rebuild


def already_migrated(asset_class: str, shift: list[str]) -> bool:
    """Does the cache on disk already show the declared clock's session boundary?

    Asked of every relabel-safe timeframe rather than one, because a partial migration is
    the state this most needs to refuse: a run interrupted between `1h` and `15m` leaves
    half the class on each clock, and "the first file I looked at was fine" is exactly the
    answer that would let the other half be shifted twice.
    """
    verdicts = []
    for tf in shift:
        for path in sorted(cache_dir(asset_class, tf).glob("*.parquet")):
            v = check_data.clock_verdict(pd.read_parquet(path, columns=[]).index,
                                         asset_class)
            if v:
                verdicts.append(v["ok"])
    return bool(verdicts) and all(verdicts)


def backup(asset_class: str) -> Path:
    """Copy the class's whole cache aside. Refuses to overwrite an existing backup."""
    from config import CLASS_DIR
    dest = BACKUP_DIR / CLASS_DIR[asset_class]
    if dest.exists():
        print(f"  backup already at {dest} - kept, NOT overwritten")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(cache_dir(asset_class, "1d").parent, dest)
    print(f"  backed up -> {dest}")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=[c for c in CLASSES if vendor_tz(c) != cache_tz(c)],
                    help="default: every class whose vendor and cache clocks differ")
    ap.add_argument("--write", action="store_true",
                    help="actually restamp. Without it this prints the plan and stops.")
    args = ap.parse_args()

    for asset_class in args.classes:
        src, dst = vendor_tz(asset_class), cache_tz(asset_class)
        print(f"\n=== {asset_class}: {src} -> {dst} ===")
        if src == dst:
            print("  vendor and cache clocks already agree, nothing to do")
            continue
        shift, rebuild = plan(asset_class)
        skipped = [tf for tf in TIMEFRAMES
                   if any(cache_dir(asset_class, tf).glob("*.parquet"))
                   and tf not in shift and tf not in {t for t, _ in rebuild}]
        print(f"  relabel : {shift}")
        print(f"  rebuild : {[f'{t}<-{s}' for t, s in rebuild]}")
        print(f"  untouched: {skipped}")

        if already_migrated(asset_class, shift):
            print("  REFUSED: this cache already shows the declared clock's session "
                  "boundary. Shifting it again would be silent corruption.")
            continue
        if not args.write:
            print("  dry run - pass --write to apply")
            continue

        backup(asset_class)
        hazard = {"ambiguous": 0, "nonexistent": 0}
        for tf in shift:
            per_tf = {"ambiguous": 0, "nonexistent": 0}
            n = 0
            for path in sorted(cache_dir(asset_class, tf).glob("*.parquet")):
                df = pd.read_parquet(path)
                before = len(df)
                out = to_cache_clock_frame(df, asset_class, per_tf)
                out.to_parquet(path)
                n += 1
                if len(out) != before:
                    # A whole-hour shift cannot lose a bar except where two vendor stamps
                    # collapse onto one instant, which only a DST transition can do. Say
                    # which symbol and how many, rather than letting a row count drift.
                    print(f"    {path.stem}: {before} -> {len(out)} bars "
                          f"({before - len(out)} collided across a DST boundary)")
            print(f"  {tf}: {n} symbols relabelled  "
                  f"(ambiguous {per_tf['ambiguous']}, "
                  f"nonexistent {per_tf['nonexistent']})")
            for k in hazard:
                hazard[k] += per_tf[k]

        for target, source in rebuild:
            cmd = [sys.executable, "-u", str(Path(__file__).parent / "resample_intraday.py"),
                   "--class", asset_class, "--from", source, "--tf", target]
            print(f"  rebuilding {target} from {source}...")
            rc = subprocess.run(cmd, cwd=str(Path(__file__).parent)).returncode
            if rc:
                print(f"  FAILED to rebuild {target} (exit {rc})")
                return rc

        print(f"  DST hazard across the class: {hazard['ambiguous']} ambiguous, "
              f"{hazard['nonexistent']} nonexistent stamps. These are instants the "
              f"vendor destroyed when it stamped a naive local time; see "
              f"config.AMBIGUOUS_POLICY for which reading was taken.")
        print(f"  now run: python check_data.py --check-clock --class {asset_class}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
