"""Paths and the sys.path wiring for the dashboard.

Named `dash_config` and NOT `config` on purpose. `../backtest engine/` goes on sys.path
and its modules import each other by bare name, so a `config.py` sitting in this directory
would shadow the engine's the moment anything ran from here. The walk-forward stage
(`wfo_paths.py`) and the paper desk (`paper_config.py`) dodge the same trap the same way.

The dashboard is a **reader**. Nothing in this folder can place an order, fetch price data
for the desk, or write into another folder's results. It reads four sources and emits two
artifacts; that is the whole contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ENGINE = REPO / "backtest engine"
WFO = REPO / "walk-forward optimization"
PAPER = REPO / "paper trading engine"
TOP20 = REPO / "top 20 stocks"          # frozen; read-only, see ../LOCKED.md

if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

import config as bt_config              # noqa: E402  (side effect: wires strategies)

# --- where the numbers come from -------------------------------------------------------
# Two results directories, not one, since the engine/WFO split: the engine holds what a
# single split produces, the WFO folder holds everything that prices selection. Every
# leaderboard on this dashboard is walk-forward, so most reads land in WFO_RESULTS.
ENGINE_RESULTS = ENGINE / "results"
WFO_RESULTS = WFO / "results"
PAPER_RESULTS = PAPER / "results"
TOP20_RESULTS = TOP20 / "results"

# --- where the artifacts go ------------------------------------------------------------
WEB = HERE / "web"                      # the served SPA; `serve.py` is rooted here
DIST = HERE / "dist"                    # the self-contained single-file build

HEADLINE = bt_config.HEADLINE_SCENARIO
GROUPS = [("stocks", "us_stocks", "Top 20 US mega-caps", bt_config.US_STOCKS),
          ("crypto", "crypto", "Top 10 crypto by market cap", bt_config.CRYPTO)]
TIMEFRAMES = ["1d", "4h"]
BRIEF_EQUITIES = ["SPY", "SOXL", "TQQQ"]
TOP_N = 15                       # leaderboard depth; the tail is all worse, not different
