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
