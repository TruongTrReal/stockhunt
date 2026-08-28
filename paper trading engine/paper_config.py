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

# The engine's own answers to "which class is this symbol" and "what clock does the vendor
# stamp that class's intraday bars in". Re-exported rather than restated for the reason
# every other re-export in this file exists: a live desk that disagrees with the backtest
# about a symbol's timezone produces a forward test of a different measurement, and nothing
# says so. `td_live` reads both.
#
# `research_class_of` is NOT `class_of`, which this file defines further down and which
# answers a narrower question — which *leg of this desk* a symbol trades on, and therefore
# which walk-forward sheet it selects from. That one raises `SystemExit` for anything
# outside the forward-test universe, which is right where it is used and wrong here: a
# member may register any symbol the vendor prices, and asking what timezone it is in must
# not be able to kill the process.
research_class_of = bt_config.class_of
vendor_tz = bt_config.vendor_tz
cache_tz = bt_config.cache_tz
to_cache_clock = bt_config.to_cache_clock

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
# `1m` joined on 2026-08-28, and the reason it was out is worth keeping because it is the
# reason the cap below exists. `td_nautilus` runs ONE POLL TASK PER SUBSCRIPTION, aligned
# to the bar close, so a minute book is one vendor request per symbol PER MINUTE — a
# different Twelve Data credit regime from the rest of this list, not merely a faster
# version of it. `td_live` is quoted against a 610/minute budget.
#
# What changed is the arithmetic, not the budget. This list governs MEMBER registrations,
# which name at most `SYMBOLS_MAX` (20) symbols each, and subscriptions are shared by
# (symbol, timeframe) — so the cost is the count of DISTINCT symbols at 1m, not the count
# of strategies. A handful of member books is tens of requests a minute against 610.
#
# The worst case is still real, which is why `MAX_1M_SYMBOLS` is enforced rather than
# assumed: `desk_control.MAX_MEMBER_STRATEGIES` is 60, and sixty strategies naming twenty
# distinct symbols each would be 1,200 requests a minute and would take the feed down for
# every other book on the desk, including the house's own.
#
# **`1m` is deliberately NOT in `BOOK_TIMEFRAMES`.** A book holds the whole class, and
# `book_universe("us_stocks")` is the live top 100 — 100 requests a minute from one
# promotion, which is the regime this note has always been about.
# `2h` came off on 2026-08-28. It was feedable — Twelve Data sells the interval and
# `BAR_SPEC` could spell it — but nothing in this repo has ever scored a 2h sheet
# (`dash_config.TIMEFRAMES` is 1d/4h/1h/15m/5m) and no registration has ever named it
# in the ledger's history: 1d 23, 4h 12, 5m 9, 1m 1, 2h **0**. So it was a pill on
# the board that could only ever answer "nothing matches this filter", and an option
# in the wizard leading somewhere with no research behind it.
#
# Removed from the LIST rather than hidden on the board, because the board strip is
# derived from this list on purpose — "what the desk CAN run, not what it happens to
# be running" — and special-casing the display would have made the two disagree,
# which is the drift that derivation exists to prevent.
MEMBER_TIMEFRAMES = [tf for tf in ("1d", "4h", "1h", "15m", "5m", "1m")]

# How many DISTINCT symbols the desk will carry at 1m, across every member registration.
#
# 120 is ~20% of the 610/minute budget `td_live` is quoted against, which leaves the house
# books, the mark-to-market poller and the warm-up requests the rest. It is a count of
# symbols and not of strategies on purpose: three members trading the same twenty tickers
# cost twenty polls between them, because `td_nautilus` keys its poll tasks on the BAR
# TYPE and books sharing one share the subscription.
#
# `cme_futures` cannot reach this at all — `db_live.SCHEMA` is 1d and 1h, so `_feedable`
# refuses a futures registration at 1m before this is ever consulted.
MAX_1M_SYMBOLS = 120

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

# How many hours a session actually lasts. Only used to size the WARM-UP REQUEST, and it
# is load-bearing there: a warmup window is asked for as a wall-clock range, and an
# intraday equity bar only exists for 6.5 of every 24 hours and only on weekdays. Sized as
# though bars were continuous — which is true of crypto and false here — a request for
# 3,000 five-minute bars reaches back 10 days and returns 1,118 of them, and the book then
# sits warming for weeks with nothing in the log to say why.
SESSION_SPAN_HOURS = {
    "us_stocks": 6.5,
    "us_etfs":   6.5,
}

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
# **The legs now hold their whole research class, and the widening is the deliberate act
# this paragraph has always demanded rather than an abandonment of it.**
#
# It used to be `MEGA20`, 20 of the 216 names the research ranks, and the reason was a
# real cost: `run_paper.py --symbols` defaults to this list, so every name in it was a
# subscription and a warm-up the moment anybody passed `--top`. That is no longer what a
# name here costs. The desk runs `--top 0` by default and trades what is REGISTERED, and
# `run_paper.build_node` seeds the whole universe into the Nautilus cache while
# subscribing to nothing — measured, and the reason it is safe to widen: an entry here
# buys a value object, and a *subscription* is still created per bar type on demand by
# whichever strategy asks for one. Nothing in this block reaches a vendor.
#
# What the old narrow list actually cost was refusals. A registration naming a symbol
# outside it was rejected by `desk_control._launch`, so a member could not trade a name
# the research itself ranks — and the ceiling that binds a live desk is the vendor's
# request budget, which subscriptions spend and rosters do not.
#
# **The impostor risk the old note names is answered elsewhere now, and had to be.**
# `td_loader.US_LISTED_CLASSES` pins `country=United States` at the source and
# `check_data.py --probe-listing` quarantines what is already cached, so the 216 are the
# point-in-time top 100's union rather than a list a third of which is somebody else's
# penny stock. `symbol_resolve.py` applies the same pin to anything registered from
# OUTSIDE these lists. A roster is not where that defence belongs.
#
# The equity side is still two groups with different evidential status:
#
#   RESEARCH_EQUITIES  the 216 names that have held a point-in-time top-100 slot — the
#                      universe the sweeps rank. Paper trading these is a LIKE-FOR-LIKE
#                      forward test.
#   BRIEF_EQUITIES     SOXL and TQQQ. A TRANSFER test: they are 3x leveraged and decay
#                      against their index in chop, so whatever a rule scored on AAPL has
#                      no claim on them, and the research has never held either.
#
# **SPY left this list, and it is the one collision the widening forced.** It was here as
# the third BRIEF_EQUITY and it is also in `bt_config.ETF_TOP10`, so taking both classes
# whole would have put one instrument on two legs — which `_seen` below refuses at import,
# and rightly: two rule lists on one asset reads on the dashboard as two systems agreeing
# when it is one asset counted twice. It is settled in favour of the ETF leg, where the
# research actually ranks it: `wf_summary_us_etfs_*` scores SPY and
# `wf_summary_us_stocks_*` does not, so leaving it here kept it selecting rules from a
# sheet its own instrument is absent from. See ETF_SYMBOLS for what that costs.
RESEARCH_EQUITIES = list(bt_config.US_STOCKS)
BRIEF_EQUITIES = ["SOXL", "TQQQ"]
EQUITY_SYMBOLS = RESEARCH_EQUITIES + BRIEF_EQUITIES

# The whole screened class, for the reason above. `CRYPTO_TOP20` is what
# `universe_screen.py` kept after the tradability gates — the 34 raw pairs are NOT what
# this reads, so a pair rejected on price grid (SHIB, OP) or on history cannot reach the
# desk by this door.
#
# **It is still not `CLASSES["crypto"]["symbols"]`, and that distinction is the one the
# old note was really about.** A slice of a live list — this was
# `CLASSES["crypto"]["symbols"][:10]` once — changes under a reordering with no diff to
# review. A stable NAMED list does not: `CRYPTO_TOP20` gains or loses a name only when
# somebody re-runs the screen and commits the result, which is a reviewable act.
# `CRYPTO_DEEP` (the ten with 1-minute history deep enough for the finest grid) stays
# named in `config` and is a subset of this, so nothing the desk has been trading moves.
CRYPTO_SYMBOLS = list(bt_config.CRYPTO_TOP20)

# `ETF_TOP10`, which is the whole class after `universe_screen.py`, PLUS `XLK`.
#
# Two things about that composition:
#
# **SPY arrives here from the equity leg.** It is in `ETF_TOP10`, and the legs must stay
# disjoint, so it can be on exactly one of them — see RESEARCH_EQUITIES above for why this
# is the side that wins. The forward record does not move: a `sid` is
# `{symbol}-{tf}-{rule}` and carries no class, so SPY's existing fills and curve points
# reattach unchanged. What changes is which sheet a HOUSE rule on SPY selects from, and
# the ETF sheet is the one that has ever scored it.
#
# **XLK is kept although the screen dropped it**, at 19.8 tradable years against a 20-year
# gate. Dropping it is not free: the desk has a forward record on XLK and retiring an
# instrument ends that record rather than pausing it, which is a worse outcome than the
# defect keeping it preserves — that it is ranked on a sheet its own instrument is absent
# from. `XLF`, `XLV` and `XLE` are all in `ETF_TOP10` and any of them is the like-for-like
# swap whenever somebody decides the record is worth ending.
ETF_SYMBOLS = list(bt_config.ETF_TOP10) + ["XLK"]

# All five. The class is small enough to trade whole, and `XAU/USD` carries the deepest
# history in the repo (1979-12-26), which is the only lever there is on a noise ceiling.
COMMODITY_SYMBOLS = list(bt_config.CLASSES["commodities"]["symbols"])

# The CME roots, written out rather than read from `bt_config.CME_FUTURES`, and this is
# the pin rule from the crypto note above applied one class over.
#
# `bt_config.CME_FUTURES` is `universes_futures.CME_SCREENED`, which is **GENERATED** —
# `futures_screen.py --write` rewrites that file from the liquidity, tradable-years,
# price-grid and correlation gates. So it is exactly the kind of thing the desk must not
# read live: re-running the screen on a fresh fetch would silently add or drop a live
# instrument, with no diff to review and a forward record that changes what it is measuring
# halfway through. `CRYPTO_SYMBOLS` was one list-reordering away from that failure and
# survived by luck.
#
# These nineteen are what the screen selected as of 2026-08-28, grouped by sector rather
# than by turnover so the equity-index block reads as the one bet it mostly is. After
# re-running `futures_screen.py --write`, compare it against this list and change it
# deliberately or not at all.
#
# **The history of this class begins 2010-06-06 and cannot be extended** — that is the
# first day of Databento's CME archive, and there is nothing before it at any price. ~16
# years against the equity sheet's ~26, and `metrics.se_ir` falls as 1/sqrt(years), so
# every gate on this class is about 1.3x harder to clear than the same gate elsewhere.
FUTURES_SYMBOLS = [
    # The equity-index block. Five contracts, and close to ONE BET: NQ is 0.93 correlated
    # with ES, YM 0.95, RTY 0.86. Three of them are in because they were asked for, not
    # because the screen kept them — `futures_screen.ALWAYS_KEEP` carries them and says
    # what that costs. On the desk the cost is concrete rather than statistical: an
    # equal-weight book puts ~26% of its capital into five versions of the same move, so
    # this leg's drawdown is a US-equity drawdown far more than the contract count
    # suggests. Read it that way, not as five diversified positions.
    "ES.v.0",     # E-mini S&P 500
    "NQ.v.0",     # E-mini Nasdaq-100          (ALWAYS_KEEP: corr +0.93 with ES)
    "YM.v.0",     # E-mini Dow                 (ALWAYS_KEEP: corr +0.95 with ES)
    "RTY.v.0",    # E-mini Russell 2000        (ALWAYS_KEEP: corr +0.86, and 9.1y history)
    "NKD.v.0",    # Nikkei 225 (USD)           — kept on merit; a different index entirely
    # Everything below cleared every gate on its own.
    "GC.v.0",     # Gold
    "CL.v.0",     # WTI Crude Oil
    "SI.v.0",     # Silver
    "HG.v.0",     # Copper
    "HO.v.0",     # NY Harbor ULSD
    "ZS.v.0",     # Soybeans
    "RB.v.0",     # RBOB Gasoline
    "ZC.v.0",     # Corn
    "NG.v.0",     # Henry Hub Natural Gas
    "ZL.v.0",     # Soybean Oil
    "ZW.v.0",     # Chicago SRW Wheat
    "LE.v.0",     # Live Cattle
    "PL.v.0",     # Platinum
    "ZM.v.0",     # Soybean Meal
]

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
#
# `5m` added 2026-08-21 for the manager's forward test of the converted TradingView
# strategies. Three things were checked before widening this, and all three are the
# reason the list is short rather than a formality:
#
# * **The feed can subscribe to it.** `td_live.INTERVALS["5m"]` is a real vendor interval,
#   not a spelling — the distinction that cost this desk fifteen hours when six member
#   timeframes were offered and two were feedable. See the note on MEMBER_TIMEFRAMES.
# * **The credit regime carries it.** `td_nautilus` runs one poll per subscription aligned
#   to the bar close, and books sharing a (symbol, timeframe) share the subscription — so
#   the cost is set by the UNIVERSE, not by how many books are promoted. Measured on the
#   first deploy: `book_universe("us_stocks")` is the live top 100, not the 23-name
#   `UNIVERSE` roster, so five 5m books cost 100 requests every five minutes (~20/min)
#   rather than the ~5/min a reading of `UNIVERSE` suggests. Still far inside the key's
#   budget, but size a 5m promotion off `book_universe`, which is what the desk actually
#   subscribes to. `1m` would be 100/min on the same universe and stays out.
# * **The rules reproduce at the default window.** Every strategy promoted at 5m was
#   checked against its full-series position over a rolling `DEFAULT_WINDOW_BARS` buffer
#   and matched on 100% of sampled bars. `lorentzian_knn` did NOT (67-83%) — its
#   neighbour set is anchored at bar 0, so it slides with the buffer — and is therefore
#   not promotable here whatever a leaderboard says about it.
BOOK_TIMEFRAMES = ["1d", "4h", "5m"]


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
    # The fifth leg, and the only one whose bars do not come from Twelve Data. A leg is a
    # feed, an instrument, a venue and a sheet: `db_live`/`db_nautilus` are the feed,
    # `td_nautilus.futures_instrument` is the instrument, `GLBX` is the venue, and
    # `wf_summary_cme_futures_1d` is the sheet. There is no 4h sheet for this class and
    # there cannot be one from the vendor's ohlcv archive — see `has_sheet` below and
    # `db_live.SCHEMA`.
    "cme_futures": FUTURES_SYMBOLS,
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

# The pinned legs as declared above, frozen. Everything that has to distinguish "a name
# the desk was configured with" from "a name a registration brought in" reads this;
# `CLASS_OF` below is the same map plus whatever `admit` has since added, and it is what
# every lookup goes through.
PINNED_CLASS_OF: dict[str, str] = dict(_seen)
CLASS_OF = _seen

# How many symbols from OUTSIDE the pinned legs the desk will carry at once.
#
# The pinned lists are bounded and reviewable — somebody edits this file and there is a
# diff. The open path is unbounded by construction: `desk_control.MAX_MEMBER_STRATEGIES`
# is 60 and each may name `SYMBOLS_MAX` (20), so 1,200 distinct names is reachable without
# anybody deciding it. That is not a roster problem, it is a FEED problem — every
# (symbol, timeframe) is one Twelve Data request per bar against the 610/minute budget
# `td_live` is quoted for, so 1,200 open names at 5m would be 240 requests a minute on
# their own, before the house books and the mark-to-market poll.
#
# 200 is sized off that arithmetic and not off taste: 200 names at 5m is ~40 requests a
# minute, which sits beside the ~20/min the class-wide 5m books already cost and leaves
# the budget's shoulder free. It is deliberately NOT a second 1m ceiling —
# `MAX_1M_SYMBOLS` (120) is finer and binds first at that size, and a symbol admitted here
# is still subject to it.
MAX_OPEN_SYMBOLS = 200


def open_symbols() -> dict[str, str]:
    """Symbol -> class for everything `admit` has let in beyond the pinned legs."""
    return {s: c for s, c in CLASS_OF.items() if s not in PINNED_CLASS_OF}


def admit(symbol: str, asset_class: str) -> None:
    """Let a symbol the pinned legs do not hold trade on this desk, from now on.

    **This is a registry entry, not a permission.** Whether the instrument EXISTS — that
    the vendor has a US listing for it, that a futures root is a real CME contract and not
    Colgate-Palmolive wearing the letters `CL` — is `symbol_resolve.resolve`'s question and
    it must be answered before this is called. This function only records the answer, and
    it is separate from the probe for the reason the probe is cached at all: the lookups
    below happen on every bar and must not reach a vendor.

    Four dictionaries key off this, which is why it exists rather than each caller
    remembering to update its own map:

      `CLASS_OF`        the venue, the instrument shape, and which sheet a house rule
                        would select from
      `SAFE_TO_VENDOR`  `LTCUSD` -> `LTC/USD`. Without it a pair admitted here is asked
                        of Twelve Data as `LTCUSD`, which is not an instrument — and the
                        vendor answers an empty frame rather than an error, so the book
                        warms up forever while the log reads healthy
      `run_paper._split_by_feed` / `live_ws.streamable`   both read `CLASS_OF` to keep
                        `cme_futures` away from Twelve Data. A futures symbol admitted
                        without an entry here defaults to the equity side of that split,
                        which is the one thing this desk may never do

    **Disjointness is enforced here too, not only at import.** `CLASS_OF` is a reverse
    lookup; a symbol claimed by two classes resolves to whichever was written last, which
    would silently move an instrument to another venue mid-session. Re-admitting a symbol
    to the class it already has is a no-op, because two registrations naming the same name
    is ordinary.
    """
    if asset_class not in UNIVERSE:
        raise RuntimeError(f"unknown asset class {asset_class!r}")
    held = CLASS_OF.get(symbol)
    if held == asset_class:
        return
    if held is not None:
        raise RuntimeError(
            f"{symbol} already trades on this desk as {held}, so it cannot also be "
            f"{asset_class} — one instrument on two legs would run it against two rule "
            f"lists and read on the board as two systems agreeing")
    if len(open_symbols()) >= MAX_OPEN_SYMBOLS:
        raise RuntimeError(
            f"the desk is already carrying {MAX_OPEN_SYMBOLS} symbols from outside its "
            f"pinned universe, which is the ceiling. Each one is a vendor request per "
            f"bar, so this is a feed budget and not a bookkeeping limit — retire "
            f"something, or name a symbol the desk already holds")
    CLASS_OF[symbol] = asset_class
    if "/" in symbol:
        SAFE_TO_VENDOR[symbol.replace("/", "")] = symbol


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
#
# Mutable, and `admit` is the only thing that adds to it. A pair let in at runtime whose
# stripped form is missing from here is asked of the vendor by the WRONG NAME.
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
    # Databento's own name for the CME Globex dataset, and it must be its own venue for a
    # stronger reason than tidiness: Nautilus routes DATA CLIENTS by venue.
    # `DataEngine.register_client` files a client with `venue=None` as the default and one
    # with a concrete venue into `_routing_map[venue]`, so `GLBX` is what makes
    # `db_nautilus` receive exactly the futures subscriptions — and what stops a futures
    # bar request reaching Twelve Data, where `ES` is Eversource Energy and comes back as
    # a clean, plausible, entirely wrong series.
    "cme_futures": "GLBX",
}

# Classes whose instrument is a fractional-quantity pair rather than a whole-share equity.
#
# `cme_futures` is here and its unit is NOT a CME contract. `FuturesContract` has no
# `size_increment`, so a whole-contract instrument against a $5,263 slice of a $100,000
# book rounds ES to zero and the whole leg sits flat. See
# `td_nautilus.futures_instrument`, which says at length what a unit on this leg is.
# Membership also decides the ACCOUNT shape in `run_paper`: a pair venue is left
# multi-currency, because a CurrencyPair trade converts USD into the base asset and an
# account that cannot hold it fills one size increment and stops.
PAIR_CLASSES = {"crypto", "commodities", "cme_futures"}


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


def sheet_path(asset_class: str, timeframe: str):
    return WFO_RESULTS / f"wf_summary_{asset_class}_{timeframe}.csv"


def has_sheet(asset_class: str, timeframe: str) -> bool:
    """Is there a walk-forward leaderboard for this cell at all?

    Asked before `top_rules`, which raises `SystemExit` on a missing sheet — right for a
    research script that has nothing else to do, and fatal for the desk. **This is not
    hypothetical and it is not temporary:** `FORWARD_TIMEFRAMES` is `1d, 4h` and there is
    no `wf_summary_cme_futures_4h.csv`, because the vendor's ohlcv archive has no 4h
    schema for CME and the 15m/1h sheets that do exist were cut from cached 1m bars, which
    a live poll cannot ask for. So one cell of the grid is permanently empty, and
    `run_paper --top N` must skip it rather than take the desk down over it.
    """
    return sheet_path(asset_class, timeframe).exists()


def top_rules(asset_class: str, n: int = TOP_N_RULES,
              timeframe: str = "1d") -> list[str]:
    """The n best rules on a sheet, by walk-forward out-of-sample IR.

    Read from `wf_summary_*.csv` rather than hard-coded, so the paper desk always reflects
    the current sweep instead of a list that silently goes stale the next time the research
    is re-run. Re-running `walkforward.py` re-picks the desk on the next start; there is no
    list to remember to update. Restricted to `wf_mode == "fixed"`: the re-selected rows
    (`IS#1`, the `[WF]` families) are a different rule in every fold and have no single
    definition to trade live.

    **Ranking is not passing.** Nothing on any of the five sheets clears a single acceptance
    gate. Three of them are led by `MAXINDEX`/`MININDEX` at `long_frac` ~ 0.86, which is the
    leaderboard ranking time-in-market rather than skill — see the `ir_vs_random` note in
    `../CLAUDE.md`. These are the least-bad candidates, which is not the same as good ones;
    they are here to exercise the pipeline.
    """
    import pandas as pd
    p = sheet_path(asset_class, timeframe)
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
