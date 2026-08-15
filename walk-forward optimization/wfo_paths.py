"""Paths and the sys.path wiring for the walk-forward stage.

Named `wfo_paths` and NOT `config` on purpose. `../backtest engine/` goes on sys.path and
its modules import each other by bare name, so a `config.py` sitting in this directory
would shadow the engine's the moment anything ran from here -- `signals.py` would do
`from config import CLASSES` and silently get the wrong file. The same trap is why the
paper desk calls its own file `paper_config.py`.

Import this **before** `config`. It is what puts the engine on the path, and the engine's
config.py in turn puts the repo root on the path, which is the only reason `strategies`
resolves. Three hops, in that order.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ENGINE = REPO / "backtest engine"

if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

# This stage keeps its own results directory. The engine's holds what a single split
# produces -- the sweep leaderboards, the combo grid, parity and validation. Everything
# that prices *selection* lands here: wf_*, cwf_*, var_*, prereg_*, strat_* and the
# curve JSONs built from wf_summary.
#
# The split is clean in one direction: every read inside this folder is of a file this
# folder wrote. `wf_vs_split.py` is the sole exception -- comparing walk-forward against
# the single split is the whole point of it -- and it reaches across via ENGINE_RESULTS.
RESULTS_DIR = HERE / "results"
ENGINE_RESULTS = ENGINE / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def write_bulk(frame, path) -> None:
    """Write a per-fold / per-cell diagnostic table as Parquet, not CSV.

    Delegates to `stockhunt.artifacts.write_bulk`. It moved there because `backtest
    engine/` could not import this module, so `sweep.py` went on writing a 166 MB
    `per_asset_us_stocks_4h.csv` that nothing in the repo reads. Kept here as the name
    every writer in this folder already calls.

    These three tables are the long ones -- `rule x symbol x scenario x fold`, four cost
    scenarios and up to 54 folds for every rule-symbol pair. On us_stocks 1d that is
    **10,420,048 rows**, and as CSV it was **809 MiB** at 81 bytes a row, roughly half of
    them the full float64 repr of two information ratios that mean nothing past three
    significant figures: `-0.7822978734787989`. Together `strat_folds`, `var_folds` and
    `riskmatch` were 2.7 GB of a 3.6 GB results directory.

    Parquet suits the shape rather than merely shrinking it. `rule`, `symbol` and
    `scenario` are three low-cardinality strings repeating ten million times and
    dictionary encoding stores each distinct value once, where CSV re-spells every one.
    Measured on the real table it is **5.3x** smaller and **19x** faster to read back
    (7.6s -> 0.4s), bit-exact including the float64 mantissas -- the decimal spelling goes,
    the precision does not.

    Not 20x, which is what an estimate from the string columns alone suggests: once those
    collapse, two high-entropy float columns are most of what is left and they barely
    compress. Worth knowing before promising a ratio on the next table.

    The superseded CSV is removed rather than left beside the Parquet. A stale twin of a
    generated file is the failure this repo already documents for `index.html` and
    `data.js` -- a reader picking the wrong one gets last month's numbers and nothing
    anywhere says so.

    Reserved for tables NOTHING reads. `edge_standard.csv` (read by `validate.py` and the
    dashboard) and `var_summary_*.csv` (globbed by `gate_calibration.py`) stay CSV; a
    reader resolves those by literal name and would not survive the extension changing.
    """
    from stockhunt.artifacts import write_bulk as _write_bulk
    _write_bulk(frame, path)
