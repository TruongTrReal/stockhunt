"""Forward-test configuration and the sys.path wiring that makes sharing possible.

Named `fwd_config` and NOT `config` on purpose. `../backtest master/` puts itself on
sys.path and its modules import each other by bare name, so a `config.py` sitting in this
directory would shadow `backtest master/config.py` the moment anything ran from here —
`signals.py` would do `from config import CLASSES` and silently get the wrong file.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BACKTEST_MASTER = REPO / "backtest master"

# Order matters. `backtest master/config.py` prepends `../test research/src` to sys.path
# on import, which is the only reason `talib_signals` resolves anywhere in this repo.
# So: put that directory on the path, import its config, and the rest follows.
if str(BACKTEST_MASTER) not in sys.path:
    sys.path.insert(0, str(BACKTEST_MASTER))

import config as bt_config          # noqa: E402  (side effect: wires talib_signals)

CLASSES = bt_config.CLASSES
TIMEFRAMES = bt_config.TIMEFRAMES
FEE_SCENARIOS = bt_config.FEE_SCENARIOS

RESULTS_DIR = HERE / "results"
LOG_DIR = HERE / "logs"
for _d in (RESULTS_DIR, LOG_DIR):
    _d.mkdir(exist_ok=True)

# --------------------------------------------------------------- forward-test universe
#
# MK's brief, taken in full: SPY, the two leveraged ETFs, and the top-10 crypto by market
# cap, on 1d and 4h.
#
# SPY and the ETFs are a DIFFERENT universe from the research study, which deliberately
# excluded SPY (it is only the BETA/CORREL benchmark input there) and never held an ETF at
# all. SOXL and TQQQ are 3x leveraged and decay against their index in chop - whatever a
# rule scored on a mega-cap does not transfer to them, so they are forward-tested as new
# instruments, not as confirmations.
#
# The equity side is two groups with different evidential status, and they are named
# separately so the distinction survives:
#
#   RESEARCH_EQUITIES  the same 20 mega-caps the rules were ranked on. Paper trading these
#                      is a LIKE-FOR-LIKE forward test — the only equity leg that can
#                      confirm or refute what the sweep measured.
#   BRIEF_EQUITIES     SPY and the two 3x leveraged ETFs. A TRANSFER test: the research
#                      never held an ETF, and SOXL/TQQQ decay against their index in chop,
#                      so whatever a rule scored on AAPL has no claim on them. They are
#                      here because MK asked for them, and they are new instruments rather
#                      than confirmations.
#
# Keeping both means a rule that works on the mega-caps but not the ETFs is visibly a
# transfer failure rather than a mystery.
RESEARCH_EQUITIES = list(bt_config.US_STOCKS)
BRIEF_EQUITIES = ["SPY", "SOXL", "TQQQ"]
EQUITY_SYMBOLS = RESEARCH_EQUITIES + BRIEF_EQUITIES
CRYPTO_SYMBOLS = list(bt_config.CLASSES["crypto"]["symbols"])   # the same top-10

FORWARD_TIMEFRAMES = ["1d", "4h"]

UNIVERSE = {
    "us_stocks": EQUITY_SYMBOLS,
    "crypto": CRYPTO_SYMBOLS,
}


def all_cells():
    """(asset_class, symbol, timeframe) for everything in the forward test."""
    for asset_class, symbols in UNIVERSE.items():
        for symbol in symbols:
            for timeframe in FORWARD_TIMEFRAMES:
                yield asset_class, symbol, timeframe


# ------------------------------------------------------------------------- warmup
#
# The live strategy recomputes its indicator over a rolling window of the last N bars,
# because a live feed has no "full series" to hand it. A recursive indicator (EMA, RSI and
# anything Wilder-smoothed, ADX) technically depends on ALL prior history and only
# converges to the full-series value asymptotically, so N is a correctness parameter, not
# a performance one.
#
# It is therefore MEASURED, not assumed - `parity_live.py` reports the window each rule
# actually needs to reproduce the backtest position exactly. These are the defaults it
# checks against, not a claim.
# MEASURED 2026-08-06 by `parity_live.py --tf 1d` on SOXL and TQQQ: the smallest trailing
# window that reproduces the full-series position on 200/200 recent bars, exactly.
#
#   SMA_200 / MA_200 / MIDPRICE_200   250   pure windowed, converge at lookback + slack
#   HT_TRENDMODE / ADXR                250
#   RSI / ADX / ATR / NATR / MACD      120   Wilder seeds decay fast at timeperiod=14
#   EMA_200                            500
#   DEMA_200                           750
#   TEMA_200                          1000   <- the binding rule
#
# 750 was the old default and it was WRONG for TEMA_200: a triple-smoothed EMA carries its
# seed three times, so the live buffer would have traded a different signal from the
# backtest with nothing to indicate it. 1500 is the measured worst case plus 50% headroom,
# because the cost of extra warmup is one larger REST page at startup and the cost of too
# little is a silently different strategy.
DEFAULT_WINDOW_BARS = 1500
MEASURED_WINDOW_BARS = 1000
MIN_WARMUP_BARS = 250

# Live venues. `sandbox` is Nautilus's own paper venue: real market data in, simulated
# fills out. That is what makes "same framework, backtest -> paper -> live" true rather
# than aspirational - only the adapter changes.
VENUES = {
    "crypto": {"name": "BINANCE", "sandbox": True},
    "us_stocks": {"name": "IB", "sandbox": True},
}
