"""Shared configuration for the top-20 multi-timeframe indicator study.

The universe is deliberately small and fixed. The 501-ticker sweep already told us
what happens at breadth (nothing beats buy-and-hold); this study trades breadth for
depth — three timeframes on twenty highly liquid names — to see whether an edge
exists somewhere the daily S&P-wide sweep could not look.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DATA_DIR = HERE / "data"
RESULTS_DIR = HERE / "results"

# talib_signals.py and its 231-variant indicator table are the whole point of
# reusing the existing project rather than re-deriving rules here.
SRC_DIR = REPO_ROOT / "test research" / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

UNIVERSE = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "JPM", "JNJ", "XOM",
    "UNH", "V", "PG", "HD", "MA", "CVX", "ABBV", "PEP", "KO", "WMT",
]

# Requested separately (MK, 2026-08-05): 3x leveraged sector/index ETFs, 1d and 4h.
#
# These are NOT small mega-caps and must not be pooled with `UNIVERSE` on a leaderboard.
# A 3x daily-reset ETF compounds the *daily* return three times, so in a choppy market it
# bleeds relative to 3x the underlying's cumulative move — the standard volatility-decay
# result. That makes buy-and-hold a **structurally weaker benchmark here than anywhere
# else in this repo**, and this project ranks everything against buy-and-hold. A rule can
# therefore post a positive excess on SOXL/TQQQ purely by sitting out drawdowns, with no
# forecasting skill whatsoever. Read any positive result against `../backtest master/`'s
# unlevered equity sheets before calling it an edge.
ETF_UNIVERSE = ["SOXL", "TQQQ"]

# Twelve Data `/earliest_timestamp`, probed 2026-08-05. Daily reaches back to the funds'
# 2010 inception; 4h is capped at 2020-02-10 for every US equity symbol, ETF or not.
ETF_START = {"1d": "2010-01-01", "4h": "2020-02-10"}

# Each timeframe gets the deepest history Twelve Data actually serves for it, not a
# uniform window: 5-minute bars only go back to ~2020 and 3.5 years of them is
# already ~70k bars per ticker, which is more statistical weight than 11 years of
# daily bars provides.
TIMEFRAMES = {
    "1d":  {"interval": "1day",  "start": "2015-01-01", "window_days": 4000,
            "bars_per_day": 1,  "intraday": False, "flatten_eod": False},
    # Added 2026-08-05 for the 1d/4h scope MK asked for. A US equity 4h "day" is one 4h
    # bar plus a ~2.5h stub, so bars_per_day is 2 rather than a clean session divisor —
    # annualisation is measured from the index anyway, this value is only for sizing
    # requests. Not flattened: a position across a session boundary is ordinary at 4h,
    # and flattening would strip the overnight drift that is 65-95% of equity return.
    "4h":  {"interval": "4h",    "start": "2020-02-10", "window_days": 1250,
            "bars_per_day": 2,  "intraday": True,  "flatten_eod": False},
    "1h":  {"interval": "1h",    "start": "2020-01-01", "window_days": 900,
            "bars_per_day": 7,  "intraday": True,  "flatten_eod": False},
    "5m":  {"interval": "5min",  "start": "2023-01-01", "window_days": 80,
            "bars_per_day": 78, "intraday": True,  "flatten_eod": True},
}

BENCHMARK_TICKER = "SPY"
BASELINE_NAME = "BUYHOLD"
CAPITAL_PER_TICKER = 10_000.0

# A rule must produce a valid Sharpe on at least this fraction of the tickers it ran
# on to be ranked. Rules that sit flat almost always otherwise win on a couple of
# tickers' worth of noise.
MIN_SHARPE_COVERAGE = 0.8
MIN_BARS = 500

# Round-trip cost in basis points, charged on |change in position| so a flat->long
# entry costs one side and a long->short reversal costs two. Zero is reported as the
# gross case; the rest is what decides whether an intraday "edge" is real, since a
# 5-minute rule can turn over hundreds of times a year.
COST_BPS_GRID = [0.0, 1.0, 5.0, 10.0]
HEADLINE_COST_BPS = 5.0


def cache_dir(timeframe: str) -> Path:
    return DATA_DIR / f"cache_{timeframe}"
