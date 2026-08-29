r"""Drive `backfill_books.py` across every leg: one subprocess each, one database each.

    python run_backfill.py --desk-db /path/desk.db --out /path/bf --from 2026-08-01

Python rather than a shell loop, and that is a decision made twice over. One process per
leg is forced — Nautilus initialises its Rust logger once per process and a second
`BacktestEngine` panics — and one database per leg keeps forty-five concurrent writers off
one SQLite file.

**The shell version of this failed three times for reasons that had nothing to do with the
work**, which is the argument for building the loop where the arguments are a list rather
than a string: the interpreter lives in the main checkout because a worktree has no venv of
its own; the ledger path did not survive `nohup env ... &` into a subshell; and `mapfile -t`
strips the newline but not the carriage return python prints on Windows, so every leg id
carried a trailing `\r` and failed the lookup as "no such registration" while looking
correct in the log. Here the environment is a dict and the id never becomes a token.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# A worktree has no `.venv`, and `pyvenv.cfg` bakes absolute paths so one cannot be
# junctioned in either — so the interpreter is the MAIN checkout's, found by walking up
# until a venv appears rather than assumed to be one level away.
def _default_python() -> str:
    for base in (HERE.parent, *HERE.parents):
        cand = base / ".venv" / "Scripts" / "python.exe"
        if cand.exists():
            return str(cand)
        cand = base / ".venv" / "bin" / "python"
        if cand.exists():
            return str(cand)
    return sys.executable


def legs(python: str, env: dict) -> list[str]:
    out = subprocess.run([python, "backfill_books.py", "--list"], cwd=HERE, env=env,
                         capture_output=True, text=True, check=True).stdout
    # `.split()` and not `.splitlines()`: a strategy id has no whitespace in it, so this
    # cannot be fooled by the carriage return python prints on Windows.
    return out.split()


def one(python: str, env: dict, out_dir: Path, leg: str, start: str,
        log_dir: Path) -> dict:
    db = out_dir / f"{leg}.db"
    try:
        db.unlink(missing_ok=True)
    except PermissionError:
        # Windows refuses to unlink a file another process still holds — an orphan from an
        # interrupted run. One stuck leg must not abort the other forty-four, so it is
        # reported as skipped and the rest continue.
        return {"leg": leg, "skipped": "its database is locked by another process"}
    child = dict(env, STOCKHUNT_PAPER_DB=str(db))
    log = log_dir / f"backfill_{leg}.log"
    proc = subprocess.run(
        [python, "-u", "backfill_books.py", "--leg", leg, "--from", start],
        cwd=HERE, env=child, capture_output=True, text=True)
    log.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    return {"leg": leg, "skipped": (proc.stdout + proc.stderr).strip().splitlines()[-1]
            if (proc.stdout + proc.stderr).strip() else f"exit {proc.returncode}"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--desk-db", required=True, help="the ledger holding the legs")
    ap.add_argument("--out", required=True, help="directory for the per-leg databases")
    ap.add_argument("--from", dest="start", default="2026-08-01")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--python", default=None)
    # Resume. A leg already written is left alone, so a run interrupted by a harness
    # timeout continues instead of starting the finished ones again.
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args(argv)

    python = args.python or _default_python()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir = HERE / "logs"
    log_dir.mkdir(exist_ok=True)

    env = dict(os.environ, STOCKHUNT_DESK_DB=str(Path(args.desk_db).resolve()),
               STOCKHUNT_PAPER_DB=str(out_dir / "_list.db"))
    ids = legs(python, env)
    stamp = lambda: datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"{stamp()}  {len(ids)} legs from {args.start}, {args.jobs} at a time")

    done, failed = [], []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        wanted = [l for l in ids
                  if not (args.skip_existing and (out_dir / f"{l}.db").exists())]
        if len(wanted) != len(ids):
            print(f"  {len(ids) - len(wanted)} already written, resuming the rest")
        futs = {pool.submit(one, python, env, out_dir, leg, args.start, log_dir): leg
                for leg in wanted}
        for fut in cf.as_completed(futs):
            res = fut.result()
            (failed if res.get("skipped") else done).append(res)
            tag = "SKIP" if res.get("skipped") else "ok  "
            extra = res.get("skipped") or (f"{res['curve_points']:>4} points "
                                           f"{res['fills']:>4} fills")
            print(f"{stamp()}  {tag} {futs[fut][-46:]:<48} {extra}")

    print(f"\n{len(done)} reconstructed, {len(failed)} skipped")
    if done:
        print(f"{sum(d['curve_points'] for d in done)} curve points, "
              f"{sum(d['fills'] for d in done)} fills")
        print(f"window {min(str(d['first'])[:10] for d in done)} .. "
              f"{max(str(d['last'])[:10] for d in done)}")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
