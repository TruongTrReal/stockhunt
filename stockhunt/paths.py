"""Repo paths, resolved from this file's own location.

The four pipeline bootstraps (`config.py`, `wfo_paths.py`, `paper_config.py`,
`dash_config.py`) each rediscover these, and each also mutates `sys.path` as a side
effect of being imported — which is why their import *order* is load-bearing and why
every one of them carries a comment explaining what would break if it were named
`config.py`. This module resolves the same paths and mutates nothing, so importing it
can never reorder anything.

It is the seam for eventually deleting those bootstraps: once `pip install -e .` is run,
`stockhunt` resolves without the repo root being on `sys.path`, and the only remaining
job of the four bootstraps is the bare-name imports *between* modules inside each
folder. Nothing here depends on that migration having happened.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = REPO_ROOT / "data"
ENV_FILE = REPO_ROOT / ".env.local"

ENGINE = REPO_ROOT / "backtest engine"
WFO = REPO_ROOT / "walk-forward optimization"
PAPER = REPO_ROOT / "paper trading engine"
DASHBOARD = REPO_ROOT / "Stockhunt Dashboard"
STRATEGIES = REPO_ROOT / "strategies"

ENGINE_RESULTS = ENGINE / "results"
WFO_RESULTS = WFO / "results"

# Generated, disposable, never committed. Distinct from `data/`, which holds the price
# bars — those are expensive to refetch (~13k API credits) and are not a cache.
CACHE_DIR = REPO_ROOT / ".cache"
POSITION_CACHE = CACHE_DIR / "positions"

# Every module whose source can change what a position series *means*. Hashed into the
# position cache key so editing a rule invalidates every cached entry it could reach.
SIGNAL_SOURCES = (STRATEGIES, ENGINE / "signals.py", ENGINE / "engines" / "vector.py")
