"""Universe, timeframes, cost grids and paths for the master backtest.

Load-bearing beyond configuration: this module prepends the repo root to `sys.path`,
which is the only reason `from strategies.talib_signals import ...` resolves anywhere in
this project. The signal layer and the published-strategy catalog live in `../strategies/`
and are shared with the walk-forward stage, the paper desk and the dashboard.

Two things here differ from the earlier studies and both are deliberate:

* **Cost grids are per asset class.** Crypto taker fees run ~10bps a side before spread;
  charging it the equity grid would manufacture survivors.
* **Intraday history windows are per (class, timeframe).** Crypto trades 24/7, so a
  5-minute window that is safely under Twelve Data's 5000-bar response cap for a US
  equity is ~3.7x too long for a crypto pair.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
# Price data is repo-wide, not per-study: the paper desk and the dashboard read the same
# bars this engine sweeps, and three copies of the same Twelve Data pull is how the
# earlier studies ended up 80MB apart on identical numbers.
DATA_DIR = REPO_ROOT / "data"
RESULTS_DIR = HERE / "results"
REPORT_DIR = HERE / "report"
ENV_FILE = REPO_ROOT / ".env.local"

# The repo root goes on sys.path so `import strategies` resolves. That package holds the
# signal layer (`strategies.talib_signals`) and the published-strategy catalog. It used
# to be reached by putting `../test research/src` on the path instead, which made a
# frozen study a runtime dependency of live code; see `../LOCKED.md`.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CAPITAL_PER_TICKER = 10_000.0
BASELINE_NAME = "BUYHOLD"

# ---------------------------------------------------------------- universes

# Deliberately the same 20 as `../top 20 stocks/` so this study's numbers can be read
# against that null result rather than against a fresh universe with fresh biases.
US_STOCKS = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "JPM", "JNJ", "XOM",
    "UNH", "V", "PG", "HD", "MA", "CVX", "ABBV", "PEP", "KO", "WMT",
]

# Top 10 by market cap that also carry enough 1-minute history to be testable at every
# timeframe. DOT and LTC were dropped for exactly that reason: their 1m series start
# 2023-05 and 2021-05, too short for the finest grid.
CRYPTO = [
    "BTC/USD", "ETH/USD", "XRP/USD", "BNB/USD", "SOL/USD",
    "DOGE/USD", "ADA/USD", "TRX/USD", "AVAX/USD", "LINK/USD",
]

# Index and leveraged sector ETFs, kept as their OWN class rather than folded into
# US_STOCKS. Two reasons, both load-bearing:
#
# * The 20 mega-caps above are deliberately frozen so this study's numbers read against
#   the earlier null results. Adding names to that list silently changes what every
#   previous sheet was measuring.
# * A 3x leveraged ETF's buy-and-hold is a different kind of benchmark. TQQQ compounds
#   daily 3x QQQ moves, so its own B&H carries ~-80% drawdowns and a volatility no
#   mega-cap has. An IR against *that* is not comparable to an IR against AAPL's B&H,
#   and averaging the two into one breadth statistic would blend two different questions.
#
# Breadth over three names is a weak gate by construction (it can only take the values
# 0, 1/3, 2/3, 1). The per-asset table, not the breadth column, is what this sheet is for.
US_ETFS = ["SPY", "TQQQ", "SOXL"]

CLASSES = {
    "us_stocks": {
        "label": "US stocks",
        "noun": "stocks",
        "symbols": US_STOCKS,
        # Fetched and cached but NOT part of the universe — it exists only as the
        # benchmark input for the BETA and CORREL rules.
        "benchmark": "SPY",
        "flatten_eod": True,       # only at FLATTEN_EOD_TIMEFRAMES, never for BUYHOLD
    },
    # Must stay AFTER us_stocks: `class_of` returns the first class that claims a symbol,
    # and SPY is us_stocks' benchmark. Ordering it second keeps `class_of("SPY")` ==
    # "us_stocks", which is what the BETA/CORREL plumbing already assumes.
    "us_etfs": {
        "label": "US ETFs",
        "noun": "ETFs",
        "symbols": US_ETFS,
        # SPY is both a universe member here and this class's own benchmark input. That
        # is harmless: it means BETA/CORREL on SPY are computed against itself, and both
        # rules are outside the stage-1e catalog anyway.
        "benchmark": "SPY",
        "flatten_eod": True,
    },
    "crypto": {
        "label": "Crypto",
        "noun": "pairs",
        "symbols": CRYPTO,
        "benchmark": "BTC/USD",
        # 24/7 market: there is no session to flatten into, and forcing a daily flat
        # would invent an exposure gap the asset class does not have.
        "flatten_eod": False,
    },
}

# ---------------------------------------------------------------- fees
#
# Actual venue fees, itemised, rather than a round "5bps" abstraction. Four components,
# because they are charged on different things:
#
#   commission_bps    per side, on notional traded
#   half_spread_bps   per side — you cross half the quoted spread to take liquidity
#   sell_fee_bps      on SELLS ONLY (US regulatory fees are one-directional)
#   borrow_annual     accrued per bar while the position is SHORT, as an annual rate
#
# The first two are what "cost per side" used to mean. The last two were previously not
# charged at all, and the short-borrow omission was the material one: the leading rules
# hold shorts 8% of bars on daily equities and up to 50% on hourly.

FEE_SCENARIOS = {
    "us_stocks": [
        {"key": "gross", "label": "gross",
         "commission_bps": 0.0, "half_spread_bps": 0.0, "sell_fee_bps": 0.0,
         "borrow_annual": 0.0,
         "note": "No costs at all. Kept only to locate the breakeven crossing and to "
                 "show what a rule looks like before reality — never evidence."},
        {"key": "retail", "label": "retail",
         "commission_bps": 0.0, "half_spread_bps": 0.5, "sell_fee_bps": 0.29,
         "borrow_annual": 0.0030,
         "note": "Zero-commission US broker (Schwab/Fidelity/IBKR Lite). Half the "
                 "NBBO spread on a mega-cap is ~0.2bps (1 cent on a $250 stock); 0.5 "
                 "allows for imperfect fills. Sell fees are SEC Section 31 "
                 "(~$27.80/$1M) plus FINRA TAF. Borrow 0.30%/yr, easy-to-borrow."},
        {"key": "wide", "label": "wide spread",
         "commission_bps": 0.0, "half_spread_bps": 1.5, "sell_fee_bps": 0.29,
         "borrow_annual": 0.0050,
         "note": "Same broker, worse execution: trading into opens, closes and news "
                 "when the book is thin, and a harder borrow."},
        {"key": "pessimistic", "label": "pessimistic",
         "commission_bps": 1.0, "half_spread_bps": 3.0, "sell_fee_bps": 0.29,
         "borrow_annual": 0.0100,
         "note": "A per-share commission broker, poor fills and expensive borrow. The "
                 "stress case, not the expectation."},
    ],
    "crypto": [
        {"key": "gross", "label": "gross",
         "commission_bps": 0.0, "half_spread_bps": 0.0, "sell_fee_bps": 0.0,
         "borrow_annual": 0.0,
         "note": "No costs at all — never evidence."},
        {"key": "binance", "label": "Binance",
         "commission_bps": 10.0, "half_spread_bps": 1.0, "sell_fee_bps": 0.0,
         "borrow_annual": 0.0,
         "note": "Base-tier spot taker fee 0.10%. Majors quote ~1-2bps wide. Shorting "
                 "spot is not available retail; via perpetuals funding has historically "
                 "flowed from longs to shorts, so charging zero borrow is the "
                 "conservative choice for a short — it does not flatter the rule."},
        {"key": "kraken", "label": "Kraken",
         "commission_bps": 26.0, "half_spread_bps": 1.5, "sell_fee_bps": 0.0,
         "borrow_annual": 0.0,
         "note": "Base-tier taker fee 0.26%."},
        {"key": "coinbase", "label": "Coinbase",
         "commission_bps": 60.0, "half_spread_bps": 2.0, "sell_fee_bps": 0.0,
         "borrow_annual": 0.0,
         "note": "Coinbase Advanced base-tier taker fee 0.60% — what a low-volume "
                 "retail account actually pays."},
    ],
}

# ETFs trade on the same venues under the same fee schedule as the equities above, so
# they take the identical grid rather than a second hand-written copy that could drift.
# SPY, TQQQ and SOXL are among the most liquid instruments listed — SPY quotes a 1-cent
# spread on a ~$600 price, roughly 0.08bps — so the 0.5bps `retail` half-spread is if
# anything conservative here. Copied, not aliased: a shared list object would make an
# edit to one class silently change the other.
FEE_SCENARIOS["us_etfs"] = [dict(s) for s in FEE_SCENARIOS["us_stocks"]]

HEADLINE_SCENARIO = {"us_stocks": "retail", "us_etfs": "retail", "crypto": "binance"}


def scenarios(asset_class: str) -> list[dict]:
    return FEE_SCENARIOS[asset_class]


def scenario(asset_class: str, key: str) -> dict:
    for s in FEE_SCENARIOS[asset_class]:
        if s["key"] == key:
            return s
    raise KeyError(f"no fee scenario {key!r} for {asset_class}")


def headline_scenario(asset_class: str) -> dict:
    return scenario(asset_class, HEADLINE_SCENARIO[asset_class])


def per_side_bps(fee: dict) -> float:
    """What one unit of position change costs, ignoring the sell-only component."""
    return fee["commission_bps"] + fee["half_spread_bps"]

# ---------------------------------------------------------------- timeframes

# End-of-day flattening applies ONLY at these timeframes, not to every intraday sheet.
#
# Measured on this data: 65-95% of US equity return is earned in overnight gaps (95.3% at
# 4h). Flattening a rule at the close while buy-and-hold holds through therefore removes
# most of the drift the rule is being scored on — `diagnose_intraday.py` shows a
# *no-signal* always-long rule scoring IR -0.59 to -0.84 once flattened, which is the
# whole of the apparent "intraday is worse" effect and more. Against that floor the real
# rules were adding value at every timeframe.
#
# So flattening is reserved for horizons where holding overnight is not what the rule
# means: at 5m and 1m the intent is genuine day-trading, and letting those collect the
# overnight drift would hand them buy-and-hold's return for free. At 4h/2h/1h/15m a
# position across a session boundary is ordinary, and this matches `../top 20 stocks/`,
# which flattened 5m only.
FLATTEN_EOD_TIMEFRAMES = frozenset({"5m", "1m"})

TIMEFRAMES = {
    "1d":  {"interval": "1day",  "intraday": False},
    "4h":  {"interval": "4h",    "intraday": True},
    "2h":  {"interval": "2h",    "intraday": True},
    "1h":  {"interval": "1h",    "intraday": True},
    "15m": {"interval": "15min", "intraday": True},
    "5m":  {"interval": "5min",  "intraday": True},
    "1m":  {"interval": "1min",  "intraday": True},
}

# `start` is the vendor's own earliest_timestamp for that (class, interval), measured
# 2026-08-03, rounded up to a clean date. `window_days` is sized so one request stays
# comfortably under the 5000-bar response cap given that class's bars-per-calendar-day
# — but the loader also splits any window that comes back at exactly 5000 rows, so a
# wrong value here costs requests, not correctness.
WINDOWS = {
    # 1970, not 2000. The old value was recorded as "the vendor's earliest timestamp" and
    # was simply wrong: `/earliest_timestamp` returns 1970-01-02 for JNJ/KO/XOM/PG,
    # 1980-12-12 for AAPL and 1986-03-13 for MSFT. That error cost the study half its
    # daily history, and history is the only thing that lowers the noise ceiling —
    # `metrics.se_ir` falls as 1/sqrt(years) and is indifferent to how many assets or
    # bars those years contain.
    ("us_stocks", "1d"):  {"start": "1970-01-01", "window_days": 4000},
    ("us_stocks", "4h"):  {"start": "2019-06-21", "window_days": 2500},
    ("us_stocks", "2h"):  {"start": "2019-06-21", "window_days": 1250},
    ("us_stocks", "1h"):  {"start": "2019-01-08", "window_days": 700},
    ("us_stocks", "15m"): {"start": "2019-09-17", "window_days": 190},
    ("us_stocks", "5m"):  {"start": "2020-01-09", "window_days": 64},
    ("us_stocks", "1m"):  {"start": "2020-03-25", "window_days": 12},

    # Probed 2026-08-07 via `/earliest_timestamp`: TQQQ 1day 2010-02-11, SOXL 2010-03-11,
    # SPY 1993-01-29. Started at the *latest* of the three so all three names share a fold
    # calendar; SPY's extra 17 years would otherwise give it folds the leveraged pair
    # cannot be scored on, and this sheet exists to compare them. On 4h the vendor has
    # nothing before 2020-02-10 for any of the three — note that this is later than the
    # 2019-06 the us_stocks 4h sheet starts from, so the two 4h sheets are not the same span.
    ("us_etfs", "1d"):  {"start": "2010-02-11", "window_days": 4000},
    ("us_etfs", "4h"):  {"start": "2020-02-10", "window_days": 2500},

    ("crypto", "1d"):  {"start": "2017-08-29", "window_days": 4000},
    ("crypto", "4h"):  {"start": "2020-01-07", "window_days": 580},
    ("crypto", "2h"):  {"start": "2020-01-07", "window_days": 290},
    ("crypto", "1h"):  {"start": "2020-01-07", "window_days": 145},
    ("crypto", "15m"): {"start": "2020-02-20", "window_days": 36},
    ("crypto", "5m"):  {"start": "2020-03-26", "window_days": 12},
    ("crypto", "1m"):  {"start": "2020-04-08", "window_days": 2},
}

# A rule must produce a valid IR on at least this fraction of its assets to be ranked.
# Rules that sit flat on most names otherwise win on a couple of assets' worth of noise.
MIN_IR_COVERAGE = 0.8
MIN_BARS = 500

# Fraction of each series used for selection. Everything reported as out-of-sample —
# including every gate — is measured on the remainder, and the shortlist never sees it.
TRAIN_FRACTION = 0.60

# ---------------------------------------------------------------- walk-forward
#
# The single split above answers "does this rule work on unseen data". It cannot answer
# "does *choosing* a rule work", because the choice is made once, by us, with the whole
# test segment already visible in the leaderboard. `walkforward.py` closes that gap:
# parameters and rule identity are re-selected on each in-sample window and applied to
# the next out-of-sample window, and only the stitched out-of-sample path is scored.
#
# Windows are calendar years, not bar counts, so a fold means the same thing at 1d and
# at 1m and the two sheets stay comparable. Rolling (not expanding) in-sample, matching
# both external submissions this was checked against.
WF_IS_YEARS = 3
WF_OOS_YEARS = 1
WF_STEP_YEARS = 1

# Below this many folds the stitched path is too short for its IR to mean anything, and
# a two-fold "walk-forward" is a single split wearing a better name. Skip the sheet.
WF_MIN_FOLDS = 3

# A fold is scored for an asset only if both windows carry this many bars. Assets that
# listed late (ABBV in 2013) simply miss the early folds rather than contributing an IR
# estimated from a handful of bars.
WF_MIN_IS_BARS = 50
WF_MIN_OOS_BARS = 10

# ---------------------------------------------------------------- gates
# The four acceptance gates. `target` is what the report prints; `test` decides.
GATES = [
    {"key": "ir",       "letter": "I", "label": "Information ratio, net and out of sample",
     "target": "0.50-1.00", "min": 0.50},
    {"key": "breadth",  "letter": "B", "label": "Breadth - share of assets with positive IR",
     "target": "70-80%",    "min": 0.70},
    {"key": "headroom", "letter": "H", "label": "Multiples of real fees the edge survives",
     "target": "3-5x",      "min": 3.0},
    {"key": "t",        "letter": "T", "label": "t = IR x sqrt(years)",
     "target": "2-3",       "min": 2.0},
]
LOO_MIN_RETENTION = 0.80          # dropping the best asset must cost < 20% of the IR


def volume_dependent_rules() -> frozenset[str]:
    """Rule names that consume volume, derived from TA-Lib rather than hardcoded.

    Twelve Data serves no volume for crypto pairs — the field is absent, not zero — so
    these cannot be evaluated on that class at all. Measured 2026-08-03 this is exactly
    4 of 231: AD, ADOSC, MFI, OBV. That list is small but not harmless: ADOSC and AD
    were the headline names in the earlier `sharpe_validated.csv` survivors, so the
    crypto sheet is structurally unable to reproduce that particular finding, and the
    report has to say so rather than showing them as absent.
    """
    from talib import abstract, get_functions
    base = set()
    for name in get_functions():
        try:
            if "volume" in abstract.Function(name).input_names.get("prices", []):
                base.add(name)
        except Exception:
            continue
    return frozenset(base)


def rule_needs_volume(rule: str, volume_funcs: frozenset[str]) -> bool:
    """True if `rule` (possibly a period variant like `ADOSC_3_10`) needs volume."""
    return any(rule == v or rule.startswith(v + "_") for v in volume_funcs)


# Directory name per asset class under `DATA_DIR`. The keys are this project's internal
# class names; the values are what the shared `data/` tree is organised by, so a human
# browsing it sees `data/stocks/1d/` rather than `data/cache_us_stocks_1d/`.
CLASS_DIR = {"us_stocks": "stocks", "crypto": "crypto", "us_etfs": "etfs"}


def cache_dir(asset_class: str, timeframe: str) -> Path:
    return DATA_DIR / CLASS_DIR[asset_class] / timeframe


def safe_symbol(symbol: str) -> str:
    """`BTC/USD` -> `BTC_USD`. Crypto pairs carry a slash, which is a path separator."""
    return symbol.replace("/", "_")


def class_of(symbol: str) -> str:
    for key, spec in CLASSES.items():
        if symbol in spec["symbols"] or symbol == spec["benchmark"]:
            return key
    raise KeyError(f"{symbol!r} is not in any configured class")


def window_spec(asset_class: str, timeframe: str) -> dict:
    return WINDOWS[(asset_class, timeframe)]
