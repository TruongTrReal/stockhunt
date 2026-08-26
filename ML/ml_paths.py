"""Paths and the sys.path wiring for the ML stage.

Named `ml_paths` and NOT `config` on purpose. `../backtest engine/` goes on sys.path and
its modules import each other by bare name, so a `config.py` sitting in this directory
would shadow the engine's the moment anything ran from here -- `signals.py` would do
`from config import CLASSES` and silently get the wrong file. `wfo_paths.py`,
`paper_config.py`, `dash_config.py` and `api_paths.py` all dodge the same trap the same
way, and every module basename in this folder has to stay globally unique for it.

Import this **before** `config`. It is what puts the engine on the path; the engine's
`config.py` in turn puts the repo root on the path, which is the only reason
`strategies.*` and `stockhunt.*` resolve. Three hops, in that order.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ENGINE = REPO / "backtest engine"
WFO = REPO / "walk-forward optimization"

if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

# The walk-forward folder is a LIBRARY to this one, not merely a neighbour: `alpha101`,
# `riskmatch_wf` and `portfolio_wf` are how anything built here gets scored, and
# `walkforward.generate_folds` is the one definition of a fold. Appended rather than
# inserted so the engine keeps priority on a basename collision.
if str(WFO) not in sys.path:
    sys.path.append(str(WFO))

# --- where things land -----------------------------------------------------------------
# Sheets: small, tracked, one row per (model, cell) -- the same shape every other stage
# writes. Anything large or regenerable goes to the cache instead, which is gitignored
# whole, so the split is enforced by where you write rather than by a .gitignore rule
# somebody has to remember to add.
RESULTS_DIR = HERE / "results"
LOGS_DIR = HERE / "logs"

CACHE = REPO / ".cache" / "ml"
FEATURES = CACHE / "features"     # feature panels, keyed by fingerprint
MODELS = CACHE / "models"         # fitted estimators, keyed by (fold, fingerprint)
BOOKS = CACHE / "books"           # model -> position panel, what riskmatch_wf reads back

RESULTS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
