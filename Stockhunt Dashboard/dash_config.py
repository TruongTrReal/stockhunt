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
# Asset class is the only split on the backtest page. Single rules and pairs used to be two
# separate lists with two separate tables; they are one leaderboard now, because a pair is
# a strategy in exactly the sense a single rule is and nobody choosing what to trade cares
# which sweep produced it. What does differ between these four groups is the price series,
# the cost grid and the benchmark -- so that is what the tabs are.
#
# Commodities were scored from the start and shown nowhere: the walk-forward, combo, book
# and curve files have existed for every stage since 2026-08-10, and the paper page has
# carried the class since 2026-08-11, but this list had three entries so `payload.build`
# never asked for a fourth and the tab strip -- which is built from its keys -- could not
# offer one. Read the tab knowing the cross-section is FIVE contracts against 100 equities:
# breadth-based figures on it carry a fraction of the weight.
GROUPS = [("stocks", "us_stocks", "Top 100 US stocks, point-in-time", bt_config.US_STOCKS),
          ("crypto", "crypto", "20 pairs, screened on spread and price grid", bt_config.CRYPTO),
          ("etf", "us_etfs", "10 ETFs, each held only while liquid", bt_config.US_ETFS),
          ("commodities", "commodities", "5 spot metals and oil, quoted as FX pairs",
           bt_config.COMMODITIES),
          # The fifth tab, and the one whose column headings mean something different.
          # Prices are ratio back-adjusted across contract rolls, so a level on this sheet
          # is not a price anybody paid; the sample starts 2010-06-06 because that is the
          # first day of the vendor's CME archive, which makes it the SHORTEST sheet here
          # at ~13 out-of-sample years against us_stocks' 24; and it has **no 4h sheet**,
          # because the vendor's hourly archive is holed before 2013. The 4h tab is
          # therefore permanently empty for this class, which is the empty state's job.
          ("futures", "cme_futures", "26 CME contracts, screened on turnover and "
           "correlation", bt_config.CME_FUTURES)]
TIMEFRAMES = ["1d", "4h"]
BRIEF_EQUITIES = ["SPY", "SOXL", "TQQQ"]
TOP_N = 30                       # leaderboard depth; the tail is all worse, not different
