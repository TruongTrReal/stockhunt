"""Paper-desk configuration and the sys.path wiring that makes sharing possible.

Named `paper_config` and NOT `config` on purpose. `../backtest engine/` puts itself on
sys.path and its modules import each other by bare name, so a `config.py` sitting in this
directory would shadow `backtest engine/config.py` the moment anything ran from here —
`signals.py` would do `from config import CLASSES` and silently get the wrong file. The
walk-forward stage avoids the same trap by calling its file `wfo_paths.py`.

Order matters. Putting the engine on the path makes its `config` importable, and that
module in turn puts the **repo root** on the path, which is the only reason
`strategies.talib_signals` resolves. Three hops, in that order.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BACKTEST_ENGINE = REPO / "backtest engine"
WFO = REPO / "walk-forward optimization"
DASHBOARD = REPO / "Stockhunt Dashboard"

if str(BACKTEST_ENGINE) not in sys.path:
    sys.path.insert(0, str(BACKTEST_ENGINE))

import config as bt_config          # noqa: E402  (side effect: wires strategies)

CLASSES = bt_config.CLASSES
TIMEFRAMES = bt_config.TIMEFRAMES
FEE_SCENARIOS = bt_config.FEE_SCENARIOS

RESULTS_DIR = HERE / "results"
LOG_DIR = HERE / "logs"
for _d in (RESULTS_DIR, LOG_DIR):
    _d.mkdir(exist_ok=True)

# Where the desk publishes live state for the dashboard to read.
#
# This is the one place the trading engine writes outside its own folder, and it is
# declared here rather than buried in `paper_state.py` so the coupling is visible and
# overridable. Set STOCKHUNT_PUBLISH_DIR to redirect it, or to an empty string to
# publish nothing at all — the desk keeps trading either way, since nothing downstream
# of this file can place an order.
_publish = os.environ.get("STOCKHUNT_PUBLISH_DIR")
if _publish is None:
    PUBLISH_DIR = DASHBOARD / "web"
elif _publish.strip():
    PUBLISH_DIR = Path(_publish)
else:
    PUBLISH_DIR = None

# The cost scenario each class is quoted at. Taken from the engine rather than restated,
# because a desk trading against numbers produced under a different fee assumption than
# the leaderboard it selected from is a silent, invisible mismatch.
HEADLINE = bt_config.HEADLINE_SCENARIO

# Nautilus bar specs for the timeframes this desk trades. Both `run_paper.py` (live) and
# `backtest_paper.py` (the same strategy on cached bars) need it, and they must agree or
# the smoke test stops testing the live path.
#
# Derived from `bt_config.TIMEFRAMES` rather than listed, because the engine already
# declares every timeframe and its vendor interval. Two hand-written subsets of one list
# is how the desk ended up able to accept a registration at a timeframe it could not build
# a bar type for — a `KeyError` inside `on_start`, minutes after the API said 201.
_NAUTILUS_UNIT = {"m": "MINUTE", "h": "HOUR", "d": "DAY"}
BAR_SPEC = {
    tf: f"{tf[:-1]}-{_NAUTILUS_UNIT[tf[-1]]}-LAST-EXTERNAL"
    for tf in bt_config.TIMEFRAMES
}

# Timeframes a MEMBER may register at. Wider than the house's own books, and deliberately
# so: the desk's books are a research artifact that follows the walk-forward sheets, while
# a member's strategy decides for itself and only needs the desk to mark a book and fill an
# order. Neither of those needs a leaderboard behind it.
#
# `1m` is excluded, and it is the one exclusion worth stating. `td_nautilus` runs ONE POLL
# TASK PER SUBSCRIPTION, aligned to the bar close — so a minute book of ten symbols is ten
# vendor requests every minute, which is a different Twelve Data credit regime from the one
# this desk was sized for, not merely a faster version of it. Add it here when the plan
# behind `TWELVEDATA_API_KEY` can carry it; nothing else has to change.
MEMBER_TIMEFRAMES = [tf for tf in ("1d", "4h", "2h", "1h", "15m", "5m")]

# A timeframe on offer that the desk cannot subscribe to is a registration that is accepted
# and then rejected minutes later, which is exactly the confusion the console was rebuilt
# to remove. Checked at import so it fails on start rather than on somebody's first order.
#
# **This check is necessary and it is NOT sufficient, and the difference cost fifteen
# hours.** `BAR_SPEC` comes from the BACKTEST engine's `TIMEFRAMES`, so all this proves is
# that a bar type can be *spelled*. Whether the live vendor client can *subscribe* to it is
# a fact about `td_nautilus.timeframe_of` and `td_live.INTERVALS`, and for a long time it
# could not: six timeframes were offered here, two were feedable, and a member registering
# at 5m got a strategy that read `live` in the console and had every order it ever sent
# refused for want of a price. The capability check lives in `td_nautilus`, next to the
# capability — this module is imported by `Stockhunt Dashboard/` and may not import the
# trading stack to do it. Do not widen this list without checking that one too.
_missing = [tf for tf in MEMBER_TIMEFRAMES if tf not in BAR_SPEC]
if _missing:
    raise SystemExit(f"{', '.join(_missing)} in MEMBER_TIMEFRAMES has no BAR_SPEC; "
                     f"add it to TIMEFRAMES in `backtest engine/config.py` first")

# --------------------------------------------------------------- when a session ends
#
# Needed by the DECIDE-EARLY signal mode (`BookStrategyConfig.signal_tf`), which computes
# a rule from the session **so far** a few minutes before the bell instead of from the
# finished bar. That is the whole point of the mode: a rule keyed on the current bar's
# close — IBS, and the reversion family generally — cannot be traded at that close in real
# life, because the close is not known until it has happened. Deciding at 15:55 and
# trading then costs the last five minutes of the range and removes the look-ahead.
#
# Only the classes with a BELL are here. Crypto and spot commodities trade around the
# clock, so "five minutes before the close" names no instant and the mode does not apply.
SESSION_CLOSE = {
    "us_stocks": ("America/New_York", (16, 0)),
    "us_etfs":   ("America/New_York", (16, 0)),
}

# How long before the bell to decide, in minutes. 5 is what was measured: at a 5-minute
# lead the signal agrees with the finished-bar signal on 86% of stock-days, and the rule's
# Sharpe gives back about half of what a next-open fill costs. Longer leads have not been
# measured and should be before being used.
DEFAULT_DECIDE_LEAD_MIN = 5

# Warmup for the decide-early mode, counted in SESSIONS, not raw bars — the rule sees one
# folded row per session, so that is the unit its lookback is in. 30 is the measured worst
# case plus 50% headroom: truncation-tested on the 5m cache, every one of the 21 symbols
# reproduces the full-history IBS state from 20 sessions or fewer. Re-measure before
# running a rule here whose lookback is longer than IBS's (which is zero — its only memory
# is the entry/exit state machine).
MIN_WARMUP_SESSIONS = 30

# The raw 5m buffer behind those sessions. A US equity session is 78 five-minute bars, so
# 3,000 is ~38 sessions — the warmup above with room for holidays and half-days.
DECIDE_EARLY_WINDOW_BARS = 3000


# --------------------------------------------------------------- forward-test universe
#
# MK's brief, taken in full: SPY, the two leveraged ETFs, and the top-10 crypto by market
# cap, on 1d and 4h. The desk now also runs the two asset classes the research gained on
# 2026-08-09 — ETFs and commodities — so that every sheet with a walk-forward leaderboard
# has a forward test attached to it rather than two of the four being scored and never
# traded.
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
#
# **`MEGA20`, not `bt_config.US_STOCKS`.** This used to read the live class list, which was
# right when that list WAS the 20 mega-caps and became wrong the moment the universe grew
# to the point-in-time S&P 500 on 2026-08-09: `run_paper.py --symbols` would have defaulted
# to 751 tickers, and a third of the departed ones are impostor penny stocks that Twelve
# Data resolves from a recycled ticker. The desk's job is a continuous forward record on a
# fixed set of instruments, so the set is pinned. Widening it is a deliberate act, not a
# side effect of re-running `sp500_membership.py`.
RESEARCH_EQUITIES = list(bt_config.MEGA20)
BRIEF_EQUITIES = ["SPY", "SOXL", "TQQQ"]
EQUITY_SYMBOLS = RESEARCH_EQUITIES + BRIEF_EQUITIES

# Pinned for the same reason, and to the same 10 the desk has been trading since it
# started: the research class grew to 34 pairs and was then screened back to 20, and
# swapping this roster would reset nothing but would multiply the credit cost and bury the
# existing record in new sids.
#
# **This was `CLASSES["crypto"]["symbols"][:10]`, which is not a pin.** It reads the live
# research universe and takes whatever ten names happen to sit at the front of it, so the
# desk's roster was one list reordering away from silently changing — exactly the failure
# the paragraph above claims to prevent, and the 2026-08-12 screen came within a couple of
# positions of triggering it. `CRYPTO_DEEP` is a stable named list and is already exactly
# these ten: the pairs with 1-minute history deep enough for the finest grid.
CRYPTO_SYMBOLS = list(bt_config.CRYPTO_DEEP)

# The ETF leg deliberately holds NONE of `BRIEF_EQUITIES`. SPY, SOXL and TQQQ are already
# traded above under the us_stocks leaderboard, and listing them here too would run one
# instrument against two different rule lists — which reads on the dashboard as two
# independent systems agreeing or disagreeing when it is really one asset counted twice.
# These five are the breadth the equity leg has not got: an index that is not the S&P, small
# caps, a sector, long duration, and gold.
ETF_SYMBOLS = ["QQQ", "IWM", "XLK", "TLT", "GLD"]

# All five. The class is small enough to trade whole, and `XAU/USD` carries the deepest
# history in the repo (1979-12-26), which is the only lever there is on a noise ceiling.
COMMODITY_SYMBOLS = list(bt_config.CLASSES["commodities"]["symbols"])

FORWARD_TIMEFRAMES = ["1d", "4h"]

# ------------------------------------------------------------------ class-wide books
#
# What one promoted strategy is given, whatever the class. The slice is this divided by
# however many names are live, so 100 US stocks get $1,000 each and 5 commodities get
# $20,000 — one number to reason about instead of a per-class table.
BOOK_CAPITAL = 100_000.0

# Timeframes a book may run at. Daily led, and 4h followed once the daily books had been
# watched — the desk runs one accounting model now, so there is no longer a second shape
# on the board for a second horizon to be confused with.
BOOK_TIMEFRAMES = ["1d", "4h"]


def live_top100(on=None) -> list[str]:
    """The US stocks in the point-in-time top 100 *now*.

    Read live rather than pinned, because that is the whole idea: the book holds the
    hundred largest by turnover, and when January's re-rank moves somebody out the desk
    sells them and buys whoever replaced them. `top100_membership` is the same table the
    research ranks on, so the forward test holds the same names the backtest did.

    216 symbols have held a slot; about 100 are live on any date.
    """
    import pandas as pd
    import top100_membership
    frame = top100_membership.load()
    when = pd.Timestamp(on) if on is not None else pd.Timestamp.utcnow().tz_localize(None)
    start = pd.to_datetime(frame["start"], errors="coerce")
    end = pd.to_datetime(frame["end"], errors="coerce")
    live = frame[(start <= when) & (end.isna() | (end > when))]
    return sorted(live["symbol"].astype(str).unique())


def book_universe(asset_class: str) -> list[str]:
    """Who a class-wide book holds right now.

    `us_stocks` is the live top 100 and moves under the book; the other three classes are
    the desk's pinned legs, which are already the whole tradable class after
    `universe_screen.py` cut them down.
    """
    if asset_class == "us_stocks":
        return live_top100()
    return list(UNIVERSE.get(asset_class, []))

UNIVERSE = {
    "us_stocks": EQUITY_SYMBOLS,
    "us_etfs": ETF_SYMBOLS,
    "crypto": CRYPTO_SYMBOLS,
    "commodities": COMMODITY_SYMBOLS,
}

# The legs must stay disjoint: `class_of` is a reverse lookup, and it is what decides which
# leaderboard a symbol's rules come from and which venue it trades on. A symbol in two legs
# would silently resolve to whichever was declared first.
_seen: dict[str, str] = {}
for _cls, _syms in UNIVERSE.items():
    for _s in _syms:
        if _s in _seen:
            raise SystemExit(f"{_s} is in both {_seen[_s]} and {_cls}; the legs must be "
                             f"disjoint — see UNIVERSE in paper_config.py")
        _seen[_s] = _cls
CLASS_OF = _seen


def class_of(symbol: str) -> str:
    """Which research class a desk symbol belongs to.

    Not inferable from the ticker: `SPY` and `QQQ` are both ETFs but only one of them is
    traded off the ETF sheet here, and `XAU/USD` carries the same separator as `BTC/USD`
    while belonging to a different class, a different venue and a different leaderboard.
    """
    try:
        return CLASS_OF[symbol]
    except KeyError:
        raise SystemExit(
            f"{symbol} is not in the forward-test universe — add it to a leg of "
            f"UNIVERSE in paper_config.py, which is also what tells the desk which "
            f"walk-forward sheet to select its rules from") from None


# `BTC/USD` -> `BTCUSD`. A Nautilus `Symbol` cannot carry a separator, so the instrument
# wears the stripped form and the vendor still has to be asked for the original. Held as a
# map rather than re-derived, because the inverse is genuinely ambiguous — `XAUUSD` could
# be `XAU/USD` or a ticker called XAUUSD, and guessing it wrong asks Twelve Data for an
# instrument that does not exist and gets an empty frame back rather than an error.
SAFE_TO_VENDOR = {s.replace("/", ""): s for s in CLASS_OF if "/" in s}

# Which sandbox venue each class trades on. `sandbox` is Nautilus's own paper venue: real
# market data in, simulated fills out, which is what makes "same framework, backtest ->
# paper -> live" true rather than aspirational — only the execution client changes.
#
# Equities and ETFs share a venue because they are the same instrument shape (whole shares,
# USD settlement); the pair-quoted classes each get their own so a `SPOT` bar can never be
# priced against the `BINANCE` book — `run_paper.route_bars_to_sandbox` filters by venue and
# that filter is only as good as the venues being distinct — and so the accounts' balances
# stay separable on the dashboard.
VENUES = {
    "us_stocks": "SANDBOX",
    "us_etfs": "SANDBOX",
    "crypto": "BINANCE",
    "commodities": "SPOT",
}

# Classes whose instrument is a fractional-quantity pair rather than a whole-share equity.
PAIR_CLASSES = {"crypto", "commodities"}


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

# --------------------------------------------------------------- rule selection
#
# Here rather than in `run_paper.py` because the dashboard picks the same rules to draw
# the same systems, and importing `run_paper` to get one function would pull the whole
# nautilus_trader stack into a page builder.

# The sheets this desk selects from are walk-forward output, so they live with the
# walk-forward stage, not with the engine that produced the single-split leaderboards.
WFO_RESULTS = WFO / "results"

# How many rules per class per timeframe the desk trades. Three rather than five: the
# universe went from two classes to four, so the same number per sheet would have doubled
# the system count, the credit spend and the log volume for no extra evidence. Three also
# sits further above the point where the leaderboard is indistinguishable from its own noise
# — on every sheet here the gap between rank 1 and rank 8 is smaller than one standard error
# of a single IR, so taking more of the list is taking more noise, not more signal.
TOP_N_RULES = 3


def top_rules(asset_class: str, n: int = TOP_N_RULES,
              timeframe: str = "1d") -> list[str]:
    """The n best rules on a sheet, by walk-forward out-of-sample IR.

    Read from `wf_summary_*.csv` rather than hard-coded, so the paper desk always reflects
    the current sweep instead of a list that silently goes stale the next time the research
    is re-run. Re-running `walkforward.py` re-picks the desk on the next start; there is no
    list to remember to update. Restricted to `wf_mode == "fixed"`: the re-selected rows
    (`IS#1`, the `[WF]` families) are a different rule in every fold and have no single
    definition to trade live.

    **Ranking is not passing.** Nothing on any of the four sheets clears a single acceptance
    gate. Three of them are led by `MAXINDEX`/`MININDEX` at `long_frac` ~ 0.86, which is the
    leaderboard ranking time-in-market rather than skill — see the `ir_vs_random` note in
    `../CLAUDE.md`. These are the least-bad candidates, which is not the same as good ones;
    they are here to exercise the pipeline.
    """
    import pandas as pd
    p = WFO_RESULTS / f"wf_summary_{asset_class}_{timeframe}.csv"
    if not p.exists():
        raise SystemExit(f"no sweep results at {p} — run walkforward.py first")
    _warn_if_stale(p)
    df = pd.read_csv(p)
    h = df[(df.scenario == HEADLINE[asset_class]) & df.rankable & ~df.is_baseline]
    h = h[(h.wf_mode == "fixed") & ~h.rule.astype(str).str.startswith("IS#1")]
    out: list[str] = []
    for r in h.nlargest(n * 3, "ir_net").itertuples():
        # MA_50 and SMA_50 are the same series (TA-Lib's MA defaults to a simple average)
        # and score identically. Trading both would double the exposure to one idea while
        # looking like diversification.
        if any(_same_idea(r.rule, k) for k in out):
            continue
        out.append(str(r.rule))
        if len(out) == n:
            break
    return out


_warned_stale: set = set()


def _warn_if_stale(sheet) -> bool:
    """Say so, once per sheet, when the leaderboard predates the data it was computed from.

    `td_loader.load` applies corrections that change WHICH bars a sweep sees, not just how
    they are scored: the liquidity quarantine and `config.BACKTEST_START`. A sheet written
    before the last of those landed ranked rules over a different sample from the one this
    desk would trade, and nothing in the CSV records it — the file looks exactly as valid as
    a fresh one. `quarantine.csv`'s mtime is the timestamp used because it is the youngest
    artifact the loader consults; it is a marker for "the input changed", not a claim that
    quarantining is the only thing that changed.

    The severity differs by class and the message says so. On `us_stocks` a stale sheet was
    ranked with the recycled-ticker impostors still in the sample — `ibs` reached 6.4e17%
    on one of them — so the ordering is not trustworthy at all. Elsewhere the quarantine
    holds nothing and the cost is narrower: the sample still runs back through the coarsely
    quantized pre-2000 bars that `BACKTEST_START` now cuts.

    A warning rather than a refusal. The desk is plumbing; a stale ranking still exercises
    the order path, and stopping the forward record because a research artifact is a day old
    would trade one problem for a worse one. But it is printed at selection time so the
    provenance is in the run's own log, next to the rules it chose.
    """
    try:
        quarantine = bt_config.DATA_DIR / "reference" / "quarantine.csv"
        if not quarantine.exists():
            return False
        if sheet.stat().st_mtime >= quarantine.stat().st_mtime:
            return False
    except OSError:
        return False
    if sheet.name in _warned_stale:
        return True
    _warned_stale.add(sheet.name)
    import datetime as _dt
    stamp = lambda p: _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    why = ("the recycled-ticker impostors were still in the sample"
           if sheet.name.startswith("wf_summary_us_stocks")
           else f"the sample still predates BACKTEST_START={bt_config.BACKTEST_START}")
    print(f"  ! {sheet.name} ({stamp(sheet)}) predates the data corrections of "
          f"{stamp(quarantine)}: {why}. Re-run walkforward.py to re-pick.")
    return True


# Rules that are one idea under two names. Each entry is MEASURED — the fraction of bars on
# which the two rules hold the identical position — not assumed from the indicator's
# description, because that is how the pair below got through in the first place.
#
#   MA_n / SMA_n   100%   TA-Lib's MA defaults to a simple average, so they are one series
#   SAR / SAREXT   100%   on XAU/USD 1d, 7,068 bars. SAREXT exposes more parameters and at
#                         their defaults produces the same flips as SAR, bar for bar
#
# Two near-misses, measured and deliberately NOT collapsed, so the next reader has the
# numbers rather than re-deriving them:
#
#   MAXINDEX / MININDEX    74%   which is what any two long-biased rules do on a rising
#                                series; they flip on different events
#   LINEARREG_n / TSF_n    95%   close, and genuinely different — TSF projects the
#                                regression one bar forward, LINEARREG reads its endpoint
#
# Collapsing those would be an opinion about the indicators. The two above are an
# observation about the output.
_ALIASES = {"SMA_": "MA_", "SAREXT": "SAR"}


def _same_idea(a: str, b: str) -> bool:
    def norm(s: str) -> str:
        for src, dst in _ALIASES.items():
            s = s.replace(src, dst)
        return s
    return norm(a) == norm(b)
