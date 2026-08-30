"""Which rules on a sheet have no verdict row yet, one per line.

The verdict cells are not all complete, and the reason is not one thing. `cme_futures 4h`
was scored before the conversions and megacellar rules were promoted, so its cell holds 171
of a 406-rule population; `commodities 5m` lost chunks to the memory ceiling. Either way the
consequence on the board is identical and silent -- `board_rank` drops a rule with no
verdict, so the cell ranks the fraction that happens to have one.

This is the input to a TOP-UP run: score only what is missing, bank it as another slice, and
let `stitch_slices.py` union it with the cell that already exists. Scoring the whole cell
again would be correct too, and on `cme_futures 4h` it would cost four times as much for the
same sheet.

    python gap_rules.py cme_futures 4h        # the missing labels
    python gap_rules.py cme_futures 4h --pop  # ...and the population size, for --n-trials
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfo_paths  # noqa: F401,E402
from riskmatch_wf import leaderboard_universe  # noqa: E402


def main() -> int:
    cls, tf = sys.argv[1], sys.argv[2]
    pop = list(leaderboard_universe(cls, tf))
    cell = Path(wfo_paths.RESULTS_DIR) / "edge_cells" / f"edge_{cls}_{tf}.csv"
    have: set[str] = set()
    if cell.exists():
        try:
            have = set(pd.read_csv(cell)["rule"].astype(str))
        except Exception:
            have = set()
    missing = [r for r in pop if r not in have]
    if "--pop" in sys.argv:
        print(len(pop))
        return 0
    for r in missing:
        print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
