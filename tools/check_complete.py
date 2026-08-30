"""Is the grid finished? One answer, three conditions, no partial credit.

Written because "done" here has three separate meanings that can and did diverge:

* a VERDICT cell exists (`riskmatch_wf` ran)            -> edge_standard has rows
* the LEADERBOARD ranks something (`portfolio_wf` ran)  -> board_rank returns n_ranked > 0
* the ROBUSTNESS matrix has the environment             -> payload has a book to draw

A cell can have a verdict and rank nothing, which is exactly what every 5m sheet did until
the open-fill book was ingested. Reporting "22 of 25 cells" while four leaderboards printed
a verdict and no money was true and useless, so this checks all three and prints the gaps.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The repo root first: `board_rank` imports `stockhunt.resultsdb`, and the dashboard folder
# on its own does not put the package on the path.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Stockhunt Dashboard"))

import board_rank  # noqa: E402

CLASSES = ["us_stocks", "us_etfs", "crypto", "commodities", "cme_futures"]
TFS = ["1d", "4h", "1h", "15m", "5m"]


def main() -> int:
    cells = ROOT / "walk-forward optimization" / "results" / "edge_cells"
    gaps: list[str] = []
    print(f'{"":13s}' + "".join(f"{t:>12s}" for t in TFS))
    for c in CLASSES:
        row = f"{c:13s}"
        for t in TFS:
            has_cell = (cells / f"edge_{c}_{t}.csv").exists()
            try:
                s = board_rank.build_sheet(c, t, [], limit=1)
                ranked = (s or {}).get("n_ranked", 0)
                book = bool((s or {}).get("book_bench"))
            except Exception:
                ranked, book = -1, False
            if ranked == -1:
                mark, why = "CRASH", "sheet raises"
            elif not has_cell:
                mark, why = "no-cell", "no verdict"
            elif not ranked:
                mark, why = "0 ranked", "verdict but nothing ranked"
            elif not book:
                mark, why = f"{ranked} nobk", "ranked but no book -> robustness gap"
            else:
                mark, why = f"{ranked} ok", ""
            if why:
                gaps.append(f"  {c} {t}: {why}")
            row += f"{mark:>12s}"
        print(row)

    print()
    if gaps:
        print(f"NOT DONE -- {len(gaps)} gap(s):")
        print("\n".join(gaps))
        return 1
    print("ALL 25 CELLS: verdict scored, leaderboard ranked, book present for robustness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
