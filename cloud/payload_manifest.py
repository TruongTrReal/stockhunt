"""Print the exact file list to upload, one path per line, for `rsync --files-from`.

**The grid needs 2.45 GB of the 9.6 GB cache, and naming the difference is the whole point
of this file.** The scored timeframes are 1d/4h/1h/15m/5m, and 5m and 15m are already
materialised as their own parquets rather than derived at run time -- so the 1m cache, and
the 2m/3m resampled from it, are 5.5 GB that no stage on the box will open. Sending the
whole of `data/` would quadruple the upload for files nothing reads.

Two things travel besides bars, and both are small and load-bearing:

* `data/reference/` -- the quarantine, the point-in-time membership, the ETF and commodity
  entry dates, the futures roll ledger. Without these `td_loader.load` silently returns
  series it should have cut: the gold and silver Open is fabricated before 2006, and three
  recycled tickers are somebody else entirely.
* `data/rates/` -- the DTB3 T-bill path, which is what `riskmatch_wf` credits idle capital
  at. Absent, every part-time rule is scored as though cash earned nothing, which is the
  defect that stage exists to fix.

The repo itself is NOT here: the box clones it from git. Only files git does not carry.

With `--class NAME` it prints one class only, which is what the fleet uses: five boxes each
take their own bars, so the biggest upload is us_stocks at 1.6 GB rather than the whole
2.43 GB, and four of the five boxes start work within a minute. The reference tier and the
T-bill path go to EVERY box regardless -- they are 13 MB and nothing scores correctly
without them.
"""
from __future__ import annotations

import sys
from pathlib import Path

# LF, NOT CRLF. This list is consumed by `tar -T` on the far side of a pipe, and on Windows
# Python would otherwise end each line with a carriage return -- tar then treats that CR as
# part of the FILENAME, every path fails to stat, and the upload "succeeds" with zero files
# while the box goes on to score an empty cache.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(newline=chr(10))

ROOT = Path(__file__).resolve().parent.parent

# What the grid actually reads. `1m`, `2m` and `3m` are deliberately absent.
CLASS_DIRS = ["stocks", "etfs", "crypto", "commodities", "futures"]
TIMEFRAMES = ["1d", "4h", "1h", "15m", "5m"]


CLASS_OF = {"us_stocks": "stocks", "us_etfs": "etfs", "crypto": "crypto",
            "commodities": "commodities", "cme_futures": "futures"}


def main() -> int:
    only = None
    if "--class" in sys.argv:
        name = sys.argv[sys.argv.index("--class") + 1]
        only = CLASS_OF.get(name, name)
    out: list[str] = []
    total = 0
    for cls in ([only] if only else CLASS_DIRS):
        for tf in TIMEFRAMES:
            d = ROOT / "data" / cls / tf
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.parquet")):
                out.append(f.relative_to(ROOT).as_posix())
                total += f.stat().st_size
    for extra in ("data/reference", "data/rates"):
        d = ROOT / extra
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if f.is_file():
                out.append(f.relative_to(ROOT).as_posix())
                total += f.stat().st_size

    print("\n".join(out))
    print(f"{len(out)} files, {total/1024**3:.2f} GB", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
