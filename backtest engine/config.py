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

from universes import SP500_ALL, SP500_CURRENT, SP500_DEPARTED   # noqa: E402
from universes_top100 import TOP100_ALL, TOP100_CURRENT           # noqa: E402

# The 20 mega-caps this study ran on from its start until 2026-08-09, kept as a named
# constant so the three earlier null results stay reproducible. Nothing selects on it by
# default any more; pass `--symbols` or point a script at `MEGA20` to regenerate an old
# sheet. Archived outputs are under `results/_archive/mega20/`.
#
# It was frozen deliberately, and unfreezing it was a considered trade: every number
# computed before that date was measured on a universe chosen for being large *today*,
# which flatters buy-and-hold (~4.85pp of CAGR, measured elsewhere in this repo) and,
# less obviously, flatters every mean-reversion rule — a sample containing no failures is
# exactly where buying dips is safest. Comparability with the old sheets was worth less
# than removing that.
MEGA20 = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "JPM", "JNJ", "XOM",
    "UNH", "V", "PG", "HD", "MA", "CVX", "ABBV", "PEP", "KO", "WMT",
]

# The full point-in-time S&P 500: 503 current members plus the 248 departures Twelve Data
# will still price. This was `US_STOCKS` from 2026-08-09 until 2026-08-12 and every sheet
# under `results/_archive/sp500_500/` was measured on it. Kept named so those numbers stay
# reproducible — pass `--symbols` or point a script at it to regenerate one.
SP500_UNIVERSE = SP500_ALL

# ---------------------------------------------------------------- the live universe
#
# **The point-in-time TOP 100 US stocks.** 216 names ever held a slot; ~100 are held at
# once, decided per-bar by `top100_membership.load()` rather than by this list. Fetching
# the union rather than the current 100 is the same argument that applies one level up:
# a universe chosen for being large *today* is survivorship bias, and it flatters both
# buy-and-hold and every dip-buying rule.
#
# Selection is by trailing 252-day median dollar volume among the S&P 500 members on each
# rebalance date, annually, with a 120-rank buffer on incumbents. **It is not market cap**
# — this repo has no historical shares-outstanding series and cannot compute one — so it
# is a liquidity ranking that favours high-turnover names and penalises quiet giants.
# `top100_membership.py`'s docstring states the full cost of that substitution, and it
# belongs beside any result computed here.
US_STOCKS = TOP100_ALL

# Crypto, widened from 10 to 34, and since 2026-08-12 that 34 is the CANDIDATE POOL
# `universe_screen.py` ranks over rather than the traded universe — `CRYPTO` is
# `CRYPTO_TOP20`. The original ten were the only pairs with 1-minute
# history deep enough for the finest grid; that constraint binds at 1m and 5m and nowhere
# else, so the wider list is used at 1d and 4h while the finest timeframes still run on
# the ten. `CRYPTO_DEEP` is that subset.
CRYPTO_DEEP = [
    "BTC/USD", "ETH/USD", "XRP/USD", "BNB/USD", "SOL/USD",
    "DOGE/USD", "ADA/USD", "TRX/USD", "AVAX/USD", "LINK/USD",
]
CRYPTO_ALL34 = CRYPTO_DEEP + [
    "XLM/USD", "DOT/USD", "LTC/USD", "BCH/USD", "ATOM/USD", "ETC/USD",
    "FIL/USD", "ICP/USD", "NEAR/USD", "APT/USD", "ARB/USD", "OP/USD",
    "INJ/USD", "IMX/USD", "VET/USD", "ALGO/USD", "HBAR/USD", "XMR/USD",
    "AAVE/USD", "UNI/USD", "SAND/USD", "GRT/USD", "SHIB/USD", "XTZ/USD",
]

# **The screened basket: the 20 most tradable pairs of the 34.** Chosen by
# `universe_screen.py --class crypto`, whose sheet
# (`results/universe_screen_crypto.csv`) carries every measurement behind this list.
#
# The vendor serves NO VOLUME for this class, so there is no turnover ranking to be had
# and the screen ranks on what OHLC alone can say about the cost of trading: length of
# history, the Corwin-Schultz spread estimate, and relative tick size. Five pairs were
# rejected outright -- IMX/ARB/APT for having under four years, OP and SHIB for printing
# on a price grid several basis points wide. The grid test is the one that matters most
# here and it is not hypothetical: a coarse grid is what let a mean-reversion rule
# compound a recycled penny stock to 6.4e17% before `check_data` learned to quarantine
# it, and SHIB/USD quotes on a grid **nine basis points** wide.
#
# All ten of `CRYPTO_DEEP` survive, so the finest-timeframe subset is unaffected.
CRYPTO_TOP20 = [
    "BTC/USD", "ETH/USD", "BNB/USD", "XRP/USD", "SOL/USD", "DOGE/USD", "ADA/USD",
    "TRX/USD", "AVAX/USD", "LINK/USD", "XMR/USD", "BCH/USD", "XLM/USD", "VET/USD",
    "DOT/USD", "ATOM/USD", "HBAR/USD", "UNI/USD", "XTZ/USD", "LTC/USD",
]

CRYPTO = CRYPTO_TOP20

# US-listed ETFs, widened from 3 to 65 and grouped by what each group is *for*. Since
# 2026-08-12 these groups are the CANDIDATE POOL that `universe_screen.py` ranks over
# rather than the traded universe — see `ETF_TOP10` below, which is what `US_ETFS` is.
# Keeping the pool wide is what lets the screen pick a basket that is not all one trend;
# it can only choose among what is fetched. This is
# no longer a "third asset class" of index proxies — the sector, factor, bond and
# international blocks exist so a rule can be tested against return streams that are not
# all the same US-equity-uptrend in different clothing. The 0.881 correlation between IR
# and time-in-market on mega-caps is an artifact of that single shared trend; TLT and
# EWJ do not share it.
#
# Commodity ETFs live HERE and not in COMMODITIES: they are US-listed funds trading on
# the same venues under the same fee schedule as any other ETF. The spot pairs are the
# separate class, because their costs and their history are both different.
ETF_BROAD = ["SPY", "QQQ", "DIA", "IWM", "VTI", "RSP", "MDY"]
ETF_SECTOR = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB",
              "XLRE", "XLC"]
ETF_FACTOR = ["MTUM", "VLUE", "QUAL", "USMV", "SPHB", "SPLV", "VTV", "VUG"]
ETF_INTL = ["EFA", "EEM", "VGK", "EWJ", "FXI", "INDA", "EWZ"]
ETF_BOND = ["TLT", "IEF", "SHY", "LQD", "HYG", "AGG", "TIP"]
# A 3x leveraged ETF's buy-and-hold is a different kind of benchmark: TQQQ compounds
# daily 3x QQQ moves, so its own B&H carries ~-80% drawdowns and a volatility no
# mega-cap has. Kept in the class but flagged, because averaging its IR into a breadth
# statistic with SHY's blends two different questions.
ETF_LEVERAGED = ["TQQQ", "SOXL", "SQQQ", "SPXL", "UPRO", "SVXY", "VXX"]
ETF_THEME = ["VNQ", "GDX", "XBI", "SMH", "ITB"]
ETF_COMMODITY = ["GLD", "IAU", "SLV", "USO", "UNG", "DBC", "PPLT", "PALL",
                 "COPX", "DBA", "CORN", "WEAT", "UGA"]

ETF_ALL65 = (ETF_BROAD + ETF_SECTOR + ETF_FACTOR + ETF_INTL + ETF_BOND
             + ETF_LEVERAGED + ETF_THEME + ETF_COMMODITY)

# **The screened basket: the 10 most tradable of the 65.** Chosen by
# `universe_screen.py --class us_etfs`; `results/universe_screen_us_etfs.csv` carries the
# measurement and the rejection reason for all 65.
#
# Two gates and a rank, and the ORDER of those is the whole design:
#
# * A name enters on the first date its trailing-252-bar median dollar volume clears $20M
#   and never falls back below. This is a head cut in the exact shape of the one
#   `td_loader.membership_span` applies to `us_stocks`, and it is applied here for the
#   same reason. **These funds existing is not the same as these funds being buyable**:
#   the nine original sector SPDRs listed in 1998 and then traded under $2M/day until
#   roughly 2004, XLU's worst year at $0.1M/day. A rule scored on those bars is a rule
#   scored on a market that would not have filled it.
# * A name must then still carry 20 of the study's 26.5 years. This replaced a "must have
#   listed by 2000" test, which was incoherent next to the entry cut: it admitted XLV,
#   unbuyable until 2006, and rejected TLT, buyable within a year of listing. That swap
#   is not cosmetic -- ranked on listing date the ten are all US equity beta at mean
#   pairwise correlation **0.72**; ranked on tradable years, four of the ten are not
#   equities at all and the correlation is **0.44**. Same window, same floor, same cap,
#   roughly twice the independent information.
# * Rank on median dollar volume over the last ten years -- a window every survivor covers
#   in full, which "median over the span it was held" is not: that measure pays a fund for
#   having been untradeable longer, since a late entry averages only over the modern
#   high-turnover era.
#
# The leveraged, inverse and commodity-roll block is excluded by name as well as by the
# history gate. A 3x fund or VXX or UNG is a path-dependent derivative of an index, not an
# asset, and its buy-and-hold is a mechanical loss with no view in it -- VXX is down 99.5%
# over its life and UNG 99.9%. "Beat buy-and-hold" against a benchmark engineered to decay
# is not a measurement, and `HEADLINE_SCENARIO` would have been reading one.
#
# What this basket still is not: it holds no fund that stopped trading, because the vendor
# serves none. Every survivorship caveat in `../CLAUDE.md` applies here unchanged.
ETF_TOP10 = ["SPY", "QQQ", "IWM", "TLT", "XLF", "GLD", "EFA", "DIA", "XLV", "XLE"]

US_ETFS = ETF_TOP10

# Spot metals and oil, quoted as FX-style pairs. Their own class for two reasons:
#
# * History. `XAU/USD` starts 1979-12-26, `XAG/USD` 1982-07-01, `WTI/USD` 1983-03-30 —
#   against 2004/2006/2006 for GLD/SLV/USO. History is the only lever on the noise
#   ceiling (`metrics.se_ir` falls as 1/sqrt(years) and ignores bar count), so 44 years
#   of gold is worth more here than any number of extra tickers.
# * Costs. There is no commission and no SEC fee; the whole cost is the dealer spread,
#   which is wider than an ETF's and asymmetric between metals and energy.
#
# Twelve Data serves no volume for these, exactly as for crypto, so AD/ADOSC/MFI/OBV are
# skipped and counted by `signals.usable_rules`. `BRENT/USD` does not exist on the
# vendor; `XPT/XPD` start only in 2012 and carry the class's thinnest quotes.
COMMODITIES = ["XAU/USD", "XAG/USD", "XPT/USD", "XPD/USD", "WTI/USD"]

# ------------------------------------------------------- CME futures
#
# Exchange-listed futures on CME Globex — CME, CBOT, NYMEX and COMEX — from Databento's
# `GLBX.MDP3`, fetched by `db_loader.py`. Four things separate this class from every
# other one here, and each of them changes how a result on it must be read.
#
# **A symbol here is `ES.v.0`, not `ES`.** The suffix is the vendor's continuous
# symbology: root, roll rule (`v` = roll on volume), rank (0 = front month). It is
# carried into the project's own spelling on purpose. `CL` is Colgate-Palmolive in
# `US_STOCKS` and WTI crude here, and `config.class_of` returns the first class that
# claims a symbol — so a bare root would have silently resolved crude oil to a toothpaste
# company. It also states the thing that is easiest to forget about a futures series:
# there is no such instrument as "ES", only a rule for choosing which contract to hold.
#
# **The bars are ratio back-adjusted, so a price here is not a price.** Only the most
# recent bars are real quotes; everything earlier is scaled so that returns across a roll
# are the returns an actual roll would have earned. Levels are therefore not comparable
# to a chart, and `MIN_PRICE_USD` is meaningless on this class.
#
# **History starts 2010-06-06 and cannot be extended.** That is the first day of
# Databento's CME archive, not a choice. `BACKTEST_START` never binds here; the class
# gets ~16 years where the us_stocks daily sheet gets ~26, and since `metrics.se_ir`
# falls as 1/sqrt(years) every gate on this sheet is roughly 1.3x harder to clear.
#
# **Buy-and-hold means something different.** A future has no cost basis: holding one is
# a fully collateralised position whose return is the roll-adjusted price change *plus*
# the bill rate on the collateral. The cash treatment in `riskmatch_wf.levered_net` is
# what makes that comparable to the other classes, and it must be on for this one.
#
# This is the FETCH POOL, not the universe: every root liquid enough to be worth pulling.
# `futures_screen.py` ranks it and writes the traded subset below, the same way
# `universe_screen.py` stands to `US_ETFS`. Micros (MES, MNQ, MGC, MCL, ...) are absent
# by construction — they are the same underlying as their parent at a fraction of the
# size, so holding both would double the apparent breadth of a class whose noise ceiling
# already assumes its assets are independent.
CME_POOL = [
    # equity index
    "ES.v.0", "NQ.v.0", "YM.v.0", "RTY.v.0", "EMD.v.0", "NKD.v.0",
    # rates
    "ZT.v.0", "ZF.v.0", "ZN.v.0", "TN.v.0", "ZB.v.0", "UB.v.0", "SR3.v.0",
    # FX
    "6E.v.0", "6J.v.0", "6B.v.0", "6A.v.0", "6C.v.0", "6S.v.0", "6N.v.0", "6M.v.0",
    "6L.v.0",
    # energy
    "CL.v.0", "BZ.v.0", "NG.v.0", "RB.v.0", "HO.v.0",
    # metals
    "GC.v.0", "SI.v.0", "HG.v.0", "PL.v.0", "PA.v.0",
    # grains and oilseeds
    "ZC.v.0", "ZS.v.0", "ZW.v.0", "KE.v.0", "ZL.v.0", "ZM.v.0",
    # livestock
    "LE.v.0", "GF.v.0", "HE.v.0",
    # crypto — listed 2017-12 (BTC) and 2021-02 (ETH), so both are short-history
    # candidates that the screen's tradable-years gate is expected to reject. They are
    # in the pool so that rejection is measured rather than assumed.
    "BTC.v.0", "ETH.v.0",
]

# The traded universe: 16 of the 43, from `futures_screen.py --write`. What the gates
# removed, and why it is the right removal:
#
# * **NQ, YM, EMD** -- 0.91 to 0.95 correlated with ES. Four US equity index contracts are
#   one bet, and `metrics.se_ir` would have been told they were four.
# * **ZT, ZN and the whole FX block** -- under the 15% volatility floor. A round trip costs
#   the same fraction of notional whatever the contract, but ZT's median daily range is
#   0.08% against CL's 2.73%, so the same fee eats ~34x more of the available move.
# * **RTY, BTC, ETH, SR3** -- too short. The E-mini Russell only moved to CME in 2017 and
#   SOFR did not exist before 2018, so none has the 12 tradable years the class floor
#   demands against its own 16.2-year ceiling.
# * **BZ, GF, KE, HE, PA, 6L** -- under $1B a day.
#
# **Dropping the slow half cost nothing in breadth, and that is the measured part.** Mean
# |pairwise correlation| among the kept names is **0.20 either way** -- the eight FX
# contracts were correlated with each other rather than adding independence, so removing
# them concentrates nothing. It is 0.20 across five sectors here against 0.44 for the ETF
# class, which is still the widest universe in the repo and the whole argument for
# carrying it. Above a 25% floor it would start to cost: 7 names, 3 sectors, 0.25.
from universes_futures import CME_SCREENED

CME_FUTURES = CME_SCREENED

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
    "commodities": {
        "label": "Commodities",
        "noun": "contracts",
        "symbols": COMMODITIES,
        # Gold is the class's own reference asset the way BTC is crypto's: the longest
        # series, and the one the others are read against. It is NOT a market portfolio,
        # so BETA/CORREL against it mean something narrower here than SPY does for
        # equities — a fact for whoever reads those two rules' rows, not a defect.
        "benchmark": "XAU/USD",
        # Spot metals trade nearly around the clock (a ~1h break at the CME daily
        # settlement), so there is no session close to flatten into.
        "flatten_eod": False,
    },
    "cme_futures": {
        "label": "CME futures",
        "noun": "contracts",
        "symbols": CME_FUTURES,
        # The equity index front month is this class's market portfolio the way SPY is
        # the equity classes'. Like SPY in `us_etfs` it is also a universe member, which
        # only means BETA and CORREL on `ES.v.0` are computed against itself.
        "benchmark": "ES.v.0",
        # Globex runs ~23 hours a day, so there is no close to flatten into. The bars are
        # already bucketed on the exchange's own 17:00->16:00 Chicago session by
        # `db_loader`, which is a stronger statement than flattening: a "day" here is a
        # trading day, not a timezone.
        "flatten_eod": False,
        # This class does NOT come from Twelve Data, which carries no futures at all —
        # every CME root there resolves to an equity wearing the same letters. Read by
        # `td_loader.main`, which would otherwise try to fetch it and cache 43 wrong
        # instruments. See `../CLAUDE.md` on why a bare ticker is not an identity.
        "source": "databento",
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

# ---------------------------------------------------------------- SOURCED RATES
#
# Every published figure below was checked against its primary source on 2026-08-11.
# The URL and the exact quoted number are recorded so a reader can re-derive the bps
# rather than trust a comment, and so the next person can see what has gone stale.
#
# US EQUITIES AND ETFs — the sell-side regulatory charges
#
#   SEC Section 31   $20.60 per $1,000,000 of covered SALES, effective 2026-04-04.
#                    FINRA Information Notice 3/17/26; SEC FY2026 rate advisory.
#                    -> 20.60 / 1e6 = 2.060e-5 of notional = 0.2060 bps.
#                    Note it replaced a $0.00/$1M window: the rate lapses to zero
#                    between appropriations and is reset annually, so this is the one
#                    number here most likely to move.
#
#   FINRA TAF        $0.000195 per share sold, capped $9.79 per trade, effective
#                    2026-01-01. FINRA's own SR-FINRA-2024-019 fee adjustment schedule
#                    (2025 $0.000166 -> 2026 $0.000195 -> 2027 $0.000232 -> 2028
#                    $0.000240 -> 2029 $0.000249). NOTE: FINRA's By-Laws Schedule A page
#                    still renders the superseded $0.000166/$8.30 text, so the adjustment
#                    schedule is the authority, not the rulebook page.
#
#                    This one is levied PER SHARE, and the model is in bps of notional,
#                    so it has to be converted at a price. Measured on the actual live
#                    universe (625 priced us_stocks names, latest close, 2026-08-11):
#                    p25 $47.47, median $114.02, p75 $246.21. At the median that is
#                    0.000195 / 114.02 * 1e4 = 0.0171 bps.
#
#                    The conversion barely matters, which is the point of recording it:
#                    total sell-side cost runs 0.2471 bps at p25, 0.2231 at the median
#                    and 0.2139 at p75. A 5x price range moves it by 0.03 bps.
#
#   -> sell_fee_bps = 0.2060 + 0.0171 = 0.223, carried as 0.22.
#      Was 0.29, which was correct for the older $27.80/$1M Section 31 rate.
#
#   Commission       $0 on online US stock and ETF trades at Schwab and Fidelity
#                    (both confirmed 2026). IBKR Lite likewise. The zero is real.
#
#   Borrow           General-collateral / easy-to-borrow large caps return the lender
#                    "up to 0.5% (50 basis points) annually", so the short pays inside
#                    that band. 0.30%/yr is kept for `retail` as a mid-band
#                    easy-to-borrow rate, 0.50% for `wide` at the top of the band, and
#                    1.00% for `pessimistic`, which is deliberately OUTSIDE general
#                    collateral — that is the point of the stress case.
#
# THERE IS NO US TRANSACTION TAX ON EQUITIES, and this is worth stating because it is
# the obvious thing to go looking for. No federal financial transaction tax and no stamp
# duty exists as of 2026; the federal stock transfer excise tax was repealed in 1966 and
# every revival since (the CBO budget option, the Sanders 0.5% proposal) remains a
# proposal. Section 31 is a fee, and the FINRA TAF is an SRO fee, not a tax.
#
# Capital gains tax IS real and is deliberately NOT modelled here, because it cannot be:
# it is charged on realised gains at a rate that depends on holding period, the filer's
# bracket and whether the account is taxable at all — none of which is a property of a
# trade. Putting a bps number on it would be an invention. A high-turnover rule is
# additionally penalised in a taxable account (short-term gains are taxed as ordinary
# income), so every net figure in this repo should be read as a pre-tax, IRA-equivalent
# result, and the turnover leaders are flattered by that omission rather than harmed.

FEE_SCENARIOS = {
    "us_stocks": [
        {"key": "gross", "label": "gross",
         "commission_bps": 0.0, "half_spread_bps": 0.0, "sell_fee_bps": 0.0,
         "borrow_annual": 0.0,
         "note": "No costs at all. Kept only to locate the breakeven crossing and to "
                 "show what a rule looks like before reality — never evidence."},
        {"key": "retail", "label": "retail",
         "commission_bps": 0.0, "half_spread_bps": 0.5, "sell_fee_bps": 0.22,
         "borrow_annual": 0.0030,
         "note": "Zero-commission US broker (Schwab/Fidelity/IBKR Lite), confirmed $0 "
                 "on online stock and ETF trades in 2026. Half the NBBO spread on a "
                 "mega-cap is ~0.2bps (1 cent on a $250 stock); 0.5 allows for "
                 "imperfect fills. Sell fee is SEC Section 31 at $20.60/$1M (0.206bps, "
                 "eff. 2026-04-04) plus FINRA TAF at $0.000195/share (0.017bps at the "
                 "universe's $114 median close, eff. 2026-01-01). Borrow 0.30%/yr, "
                 "inside the general-collateral band."},
        {"key": "wide", "label": "wide spread",
         "commission_bps": 0.0, "half_spread_bps": 1.5, "sell_fee_bps": 0.22,
         "borrow_annual": 0.0050,
         "note": "Same broker and the same statutory sell fee — those do not vary with "
                 "execution quality. Worse fills: trading into opens, closes and news "
                 "when the book is thin, and borrow at the top of general collateral "
                 "(0.50%/yr, the documented ceiling for easy-to-borrow names)."},
        {"key": "pessimistic", "label": "pessimistic",
         "commission_bps": 1.0, "half_spread_bps": 3.0, "sell_fee_bps": 0.22,
         "borrow_annual": 0.0100,
         "note": "A per-share commission broker, poor fills, and borrow at 1.00%/yr — "
                 "deliberately outside general collateral, i.e. a name that is not "
                 "comfortably available to short. The stress case, not the expectation."},
    ],
    "crypto": [
        {"key": "gross", "label": "gross",
         "commission_bps": 0.0, "half_spread_bps": 0.0, "sell_fee_bps": 0.0,
         "borrow_annual": 0.0,
         "note": "No costs at all — never evidence."},
        {"key": "binance", "label": "Binance",
         "commission_bps": 10.0, "half_spread_bps": 1.0, "sell_fee_bps": 0.0,
         "borrow_annual": 0.0,
         "note": "Spot taker 0.10% at VIP 0, the base tier, verified 2026. (Paying fees "
                 "in BNB cuts it 25% to 0.075%; not modelled, because it requires "
                 "holding an exchange token whose price risk is not in this backtest.) "
                 "Majors quote ~1-2bps wide. Shorting spot is not available retail; via "
                 "perpetuals funding has historically flowed from longs to shorts, so "
                 "charging zero borrow is the conservative choice for a short — it does "
                 "not flatter the rule."},
        {"key": "coinbase", "label": "Coinbase",
         "commission_bps": 60.0, "half_spread_bps": 2.0, "sell_fee_bps": 0.0,
         "borrow_annual": 0.0,
         "note": "Coinbase Advanced taker 0.60% below $10K of 30-day volume — what a "
                 "low-volume retail account actually pays. Verified 2026."},
        {"key": "kraken", "label": "Kraken",
         "commission_bps": 80.0, "half_spread_bps": 1.5, "sell_fee_bps": 0.0,
         "borrow_annual": 0.0,
         "note": "Kraken Pro spot taker 0.80% at Tier 1 ($0+ 30-day volume), from "
                 "Kraken's own fee schedule, 2026. This was 26.0bps here, which was the "
                 "old flat 0.26% base tier — Kraken has since restructured into a "
                 "12-tier ladder (0.80% taker at Tier 1, 0.38% at $10K, 0.30% at $50K, "
                 "0.10% at $10M). Tier 1 is the honest retail default and it is now the "
                 "MOST expensive venue on this grid, not the middle one, so the "
                 "scenarios are listed cheapest-first rather than by their old order."},
    ],
}

# ETFs trade on the same venues under the same fee schedule as the equities above, so
# they take the identical grid rather than a second hand-written copy that could drift.
# SPY, TQQQ and SOXL are among the most liquid instruments listed — SPY quotes a 1-cent
# spread on a ~$600 price, roughly 0.08bps — so the 0.5bps `retail` half-spread is if
# anything conservative here. Copied, not aliased: a shared list object would make an
# edit to one class silently change the other.
FEE_SCENARIOS["us_etfs"] = [dict(s) for s in FEE_SCENARIOS["us_stocks"]]

# Spot metals and oil. The whole cost is the dealer spread — no commission, no SEC fee,
# no stock borrow — but it is wider than an ETF's and it is not the same across the class.
#
# **These are the least well-sourced numbers in this file, and that is a property of the
# market rather than of the research.** Spot metals have no consolidated tape and no
# regulator publishing a rate: every figure is one dealer's quote on one account type, so
# a survey returns a range, not a number. Checked 2026-08-11, retail XAU/USD spreads run
# **15-50 pips typical, 6-10 pips on raw-spread accounts**, with 0.5-0.6 pips seen in the
# London-NY overlap on commission-bearing accounts.
#
# That range cannot be turned into bps without fixing a convention this repo does not
# control: brokers disagree on whether an XAU/USD "pip" is $0.01 or $0.10, which is a
# 10x difference in the answer. Under the $0.01 reading a 23-pip spread on ~$3,500 gold
# is ~0.33bps a half-turn; under $0.10 it is ~3.3bps. The ladder below spans that
# ambiguity deliberately rather than resolving it by picking the flattering end, and it
# is the reason `retail` sits at 2.0bps instead of the 0.6bps a single convention would
# suggest. Do not tighten these without a dealer quote in hand.
#
# One cost this grid does NOT charge is the overnight financing swap on a held spot
# position, which is real and is levied per calendar day rather than per trade. The
# engine's cost model charges on position *change*, so there is nowhere honest to put it;
# `riskmatch_wf.levered_net` already applies a bill-rate cash/financing leg, which is the
# closest thing this project has. A rule that holds spot commodities for long stretches is
# therefore scored slightly generously here, and that asymmetry favours the buy-and-hold
# baseline it is being compared against, not the rule.
FEE_SCENARIOS["commodities"] = [
    {"key": "gross", "label": "gross",
     "commission_bps": 0.0, "half_spread_bps": 0.0, "sell_fee_bps": 0.0,
     "borrow_annual": 0.0,
     "note": "No costs at all — never evidence."},
    {"key": "tight", "label": "tight (metals)",
     "commission_bps": 0.0, "half_spread_bps": 0.75, "sell_fee_bps": 0.0,
     "borrow_annual": 0.0,
     "note": "Gold and silver at a competitive broker in liquid hours."},
    {"key": "retail", "label": "retail",
     "commission_bps": 0.0, "half_spread_bps": 2.0, "sell_fee_bps": 0.0,
     "borrow_annual": 0.0,
     "note": "The headline. Covers WTI's ~2bps and leaves room for metals quoted "
             "outside London/NY overlap, where spreads widen several-fold."},
    {"key": "wide", "label": "wide",
     "commission_bps": 0.0, "half_spread_bps": 5.0, "sell_fee_bps": 0.0,
     "borrow_annual": 0.0,
     "note": "Platinum, palladium, and anything traded in the Asian session. The "
             "stress case."},
]

# Exchange-listed futures are the cheapest thing in this repo to trade, and by a wide
# margin: one ES contract is ~$320,000 of index exposure and costs about $2.25 all-in for
# the round turn, which is 0.035bps a side. The equity `retail` scenario charges 0.5bps of
# half-spread alone — fourteen times as much — so a rule that dies on the stock sheet
# purely on costs is not automatically dead here, and that asymmetry is the main reason
# this class is worth carrying at all.
#
# Three things about this grid are not like the others:
#
# **Commission is per contract, so a bps figure is an approximation that scales the wrong
# way.** The same $2.25 is 0.035bps on ES and 0.33bps on a $68,750 crude contract, an
# order of magnitude apart. The rates here are set from the *median* contract in the
# screened universe, so the largest contracts are charged slightly too much and the
# smallest slightly too little. It errs toward too much, which is the safe direction.
#
# **`borrow_annual` is zero and that is not a simplification.** A short future is not a
# borrowed asset — it is the other side of a contract — so there is no locate, no recall
# and no fee. This is a real structural advantage over the equity classes, where a short
# pays 30-100bps a year, and it is one of the few places in this repo where zero is the
# honest number rather than a missing one.
#
# **The roll is not charged here, and does not need to be.** Rolling a position costs a
# round turn every expiry — four a year on ES, twelve on CL — but `db_loader` back-adjusts
# the series so a roll is not a position change, and the engine charges on position
# change. That looks like an understatement until you notice the baseline rolls too: a
# buy-and-hold on a continuous futures series holds through every roll and pays exactly
# the same toll. It is identical on both sides of the comparison and cancels out of the
# excess, which is the one thing `../CLAUDE.md` requires of a benchmark. It does NOT
# cancel out of an absolute CAGR, so quote the roll count from
# `../data/reference/futures_rolls.csv` beside any absolute figure on this class.
FEE_SCENARIOS["cme_futures"] = [
    {"key": "gross", "label": "gross",
     "commission_bps": 0.0, "half_spread_bps": 0.0, "sell_fee_bps": 0.0,
     "borrow_annual": 0.0,
     "note": "No costs at all — never evidence."},
    {"key": "tight", "label": "tight (ES/NQ/ZN)",
     "commission_bps": 0.05, "half_spread_bps": 0.2, "sell_fee_bps": 0.0,
     "borrow_annual": 0.0,
     "note": "The top of the class. ES, NQ, ZN and ZF quote one tick wide essentially "
             "all session — 0.25 index points on 6,400 is 0.39bps, so half of it is "
             "0.2 — and a discount futures broker charges $0.25-$0.85 plus ~$1.40 of "
             "exchange and NFA fees per round turn."},
    {"key": "retail", "label": "retail",
     "commission_bps": 0.25, "half_spread_bps": 0.5, "sell_fee_bps": 0.0,
     "borrow_annual": 0.0,
     "note": "The headline. A one-tick spread on the median screened contract and a "
             "$2.25-$4.00 all-in round turn, which is what a retail futures account "
             "actually pays. Wide enough to cover the grains and livestock, whose ticks "
             "run 2-6bps, without pricing the whole class off the deepest contracts."},
    {"key": "wide", "label": "wide",
     "commission_bps": 0.5, "half_spread_bps": 2.0, "sell_fee_bps": 0.0,
     "borrow_annual": 0.0,
     "note": "The thin end and the bad hours: palladium, feeder cattle, KC wheat, and "
             "anything traded in the Asian session or into a limit move. The stress "
             "case, not the expectation."},
]

HEADLINE_SCENARIO = {"us_stocks": "retail", "us_etfs": "retail", "crypto": "binance",
                     "commodities": "retail", "cme_futures": "retail"}


# Timeframes that run a SINGLE cost scenario instead of the full grid.
#
# Set to {} on 2026-08-09, deliberately, after being {"1d": "gross", "4h": "gross"}.
#
# Collapsing to gross was justified on its own terms — the retail schedule costs the
# leading rules only 0.023-0.037 Sharpe at these horizons — but it broke the acceptance
# standard, which is the thing the collapse was not allowed to break. `config.GATES` is
# EDGE_STANDARD, and two of its six criteria need a real fee schedule:
#
#   H  cost headroom >= 3x the real schedule. With one scenario there is no second point
#      to locate the crossing, so it is undefined, not infinite.
#   W  more money at equal risk, measured net of the schedule being survived.
#
# A standard with two uncomputable criteria is not a standard. The fee panels come back.
#
# The tail is the reason it matters beyond bookkeeping: 22 of 231 rules turn over more
# than 100x a year and the worst (`BOP`, 2.5M trades) reach ~256, where the retail
# schedule costs ~2%/yr. Those are exactly the rules that look best gross.
SINGLE_SCENARIO_TIMEFRAMES: dict[str, str] = {}


def scenarios(asset_class: str) -> list[dict]:
    return FEE_SCENARIOS[asset_class]


def scenarios_for(asset_class: str, timeframe: str) -> list[dict]:
    """Cost scenarios to actually run for this (class, timeframe).

    Collapsing 4 scenarios to 1 at 1d and 4h also cuts the report from 56 panels to far
    fewer, which is the single biggest lever on the payload budget that limits how many
    candidates can carry drill-down detail.
    """
    key = SINGLE_SCENARIO_TIMEFRAMES.get(timeframe)
    if key is None:
        return FEE_SCENARIOS[asset_class]
    return [scenario(asset_class, key)]


def headline_key(asset_class: str, timeframe: str | None = None) -> str:
    """The scenario key a sheet is reported on — the one it RAN, not the one assumed.

    Every stage used to read `HEADLINE_SCENARIO[asset_class]` directly. That was correct
    while every sheet ran the full grid, and became a silent trap the moment 1d and 4h
    collapsed to `gross`: filtering rows on `retail` then matches nothing, and the
    aggregate becomes the mean of an empty set. It does not raise — it returns NaN, or
    prints "0 rankable", which reads as a null result rather than as a wrong question.
    That has now happened in `sweep.py` and `walkforward.py`; this function exists so it
    cannot happen in the other six call sites.

    **The collapse must be the reason, not the mere presence of a timeframe.** This read
    `scenarios_for(asset_class, timeframe)[0]["key"]`, which is only the collapsed
    scenario when the timeframe actually collapsed. Otherwise it is simply the first entry
    of the fee grid — and every grid in this file starts at `gross`. With
    `SINGLE_SCENARIO_TIMEFRAMES` set back to `{}` on 2026-08-09, *nothing* collapses, so
    every one of the 13 call sites that passes a timeframe was silently reporting the
    ZERO-COST scenario: walkforward, variants, prereg, strat_wf, combo_wf, curves,
    riskmatch_wf and combo_sweep. Nothing complained, because `gross` rows do exist — the
    failure mode is a flattering number, not an empty set.

    That is the precise thing the un-collapse was performed to prevent. Two of the six
    EDGE_STANDARD criteria (H, W) only mean something against a real fee schedule, and
    costs are what kill the candidates here: 22 of 231 rules turn over more than 100x a
    year, where the retail schedule costs ~2%/yr, and those are exactly the rules that
    look best gross.
    """
    if timeframe is not None and timeframe in SINGLE_SCENARIO_TIMEFRAMES:
        return SINGLE_SCENARIO_TIMEFRAMES[timeframe]
    return HEADLINE_SCENARIO[asset_class]


def reference_scenario(asset_class: str, timeframe: str) -> dict | None:
    """The paid schedule a single-scenario sheet is measured against for `cost_drag`."""
    if timeframe not in SINGLE_SCENARIO_TIMEFRAMES:
        return None
    return headline_scenario(asset_class)


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
FLATTEN_EOD_TIMEFRAMES = frozenset({"5m", "3m", "2m", "1m"})

# `interval: None` marks a timeframe the vendor does not serve: Twelve Data has no 2min
# or 3min product, so those bars exist only by resampling the cached 1m — see
# `resample_intraday.py`. `td_loader.fetch` refuses them outright rather than guessing,
# exactly as it refuses `cme_futures`; `load` reads whatever the resampler materialised.
TIMEFRAMES = {
    "1d":  {"interval": "1day",  "intraday": False},
    "4h":  {"interval": "4h",    "intraday": True},
    "2h":  {"interval": "2h",    "intraday": True},
    "1h":  {"interval": "1h",    "intraday": True},
    # Added 2026-08-22 for the full-timeframe robustness program. `30min` IS a vendor
    # product — the older comment above ("no 2min or 3min") is about the gaps around it,
    # not about this interval. Probed the same day: AAPL 30min from 2019-09-16, SPY from
    # 2020-02-10, BTC/USD from 2020-01-06, XAU/USD from 2020-01-24.
    "30m": {"interval": "30min", "intraday": True},
    "15m": {"interval": "15min", "intraday": True},
    "5m":  {"interval": "5min",  "intraday": True},
    "3m":  {"interval": None,    "intraday": True},
    "2m":  {"interval": None,    "intraday": True},
    "1m":  {"interval": "1min",  "intraday": True},
}

# `start` is the vendor's own earliest_timestamp for that (class, interval), measured
# 2026-08-03, rounded up to a clean date. `window_days` is sized so one request stays
# comfortably under the 5000-bar response cap given that class's bars-per-calendar-day
# — but the loader also splits any window that comes back at exactly 5000 rows, so a
# wrong value here costs requests, not correctness.
# Every backtest starts here, whatever the cache holds. Set 2026-08-11.
#
# The deep history is FETCHED and KEPT -- `WINDOWS` still reaches back to 1970 and the
# parquet on disk is untouched -- but nothing scores a bar before this date. Cutting at
# load time rather than at fetch time is the whole point: the decision is reversible with
# one edit and costs no credits to undo.
#
# It is here because the pre-2000 vendor history cannot support the statistics run on it.
# Those bars are largely stale single-price quotes -- 2.5% of pre-2005 bars have High ==
# Low against 0.3% after, reaching 98% on SEE, 82% on AET, 69% on HUBB -- and IBS is
# `(C-L)/(H-L)`, which on such a bar is 0/0. The same era is coarsely quantized: MNST's
# split-adjusted 1989 price is $0.028, where a single tick is 2.27%, so a mean-reversion
# rule harvests the rounding grid instead of a market. The effect is not subtle and the
# cut removes it outright:
#
#     ibs terminal wealth   full history -> 2000+      buy-and-hold 2000+
#     MNST                     1.31e8x -> 48.5x                    2,011x
#     SIG                      1.36e8x -> 20.7x                      7.4x
#     TYL                      1.16e7x -> 37.2x                     53.2x
#
# The cost is real and must be quoted beside any result: us_stocks 1d drops from ~41
# out-of-sample years to ~20, and `metrics.se_ir` falls as 1/sqrt(years), so every noise
# ceiling roughly doubles-in-difficulty. This sheet was the ONE whose gates were coherent
# and that was because of its length. Shorter sample, higher bar; that trade was made
# deliberately, on the grounds that 30 years of quotes that cannot support the statistic
# are worth less than 20 that can.
BACKTEST_START = "2000-01-01"

# A share that costs less than this is not backtested. See `check_data.quarantine_reason`.
#
# CAUTION, and it is the whole story for this constant: prices are stored `adjust=all`, so
# a historical price is today's share reflated backwards through every split. Applied to
# those stored numbers a $1 floor deletes **NVDA** (median $0.48 since 2000, 62% of bars
# under $1, $324M/day), **KLAC**, **NFLX** (34% of bars) and **MNST** -- none of which was
# ever a penny stock -- while catching nothing, because after the liquidity quarantine
# **zero** symbols have a latest close under $1.
#
# So it is applied to the LATEST close, the one price in an `adjust=all` series that is
# genuinely what a share costs. That makes it a real guard for future fetches rather than
# a retroactive filter on split history: it independently catches the recycled tickers
# (`FL` $0.44, `GR` $0.07, `TSG` $0.18, `LEH` $0.13) that the dollar-volume test found.
#
# A true point-in-time price floor needs UNADJUSTED prices, which this repo does not
# store; it would take a refetch at `adjust=none` and a per-symbol split-factor series.
MIN_PRICE_USD = 1.00

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
    # 30m probed 2026-08-22: AAPL earliest_timestamp 2019-09-16 — the same depth as
    # 15min, not 1h, so the two share a start. ~13 bars per US session keeps 380
    # calendar days near ~3,400 bars, comfortably under the 5000-bar response cap.
    ("us_stocks", "30m"): {"start": "2019-09-17", "window_days": 380},
    ("us_stocks", "15m"): {"start": "2019-09-17", "window_days": 190},
    ("us_stocks", "5m"):  {"start": "2020-01-09", "window_days": 64},
    ("us_stocks", "1m"):  {"start": "2020-03-25", "window_days": 12},

    # Was 2010-02-11, the latest of SPY/TQQQ/SOXL, so those three shared a fold calendar.
    # That reasoning does not survive a 65-name class: the block now spans SPY 1993 to
    # XLC 2018 and no single start makes them commensurate anyway. Take the earliest
    # instead and let each name carry the folds it can support — `MIN_BARS` and
    # `WF_MIN_FOLDS` already refuse the ones that cannot. History is the only lever on
    # the noise ceiling, and this recovers 17 years of it on the broad and sector blocks.
    # On 4h the vendor has nothing before 2020-02-10 for any ETF — later than the 2019-06
    # the us_stocks 4h sheet starts from, so the two 4h sheets are not the same span.
    ("us_etfs", "1d"):  {"start": "1993-01-01", "window_days": 4000},
    ("us_etfs", "4h"):  {"start": "2020-02-10", "window_days": 2500},
    # 1m and 5m added 2026-08-20 for the intraday Heikin-Ashi study. Their absence was
    # not a decision — no intraday sheet had ever been run on this class, so the gap sat
    # unnoticed until `fetch` raised a bare KeyError three hours into a batch. ETFs are
    # US-listed equities on the same venues as `us_stocks`, so they inherit that class's
    # vendor start dates and window sizes; the depth probe of 2026-08-03 measured the
    # limit per INTERVAL, not per symbol.
    ("us_etfs", "5m"):  {"start": "2020-01-09", "window_days": 64},
    ("us_etfs", "1m"):  {"start": "2020-03-25", "window_days": 12},
    # 1h and 30m added 2026-08-22 for the full-timeframe robustness program. The ETF
    # archive is NOT the equity archive at these intervals — SPY's earliest_timestamp is
    # 2020-02-10 for both 1h and 30min, a year later than us_stocks' 2019-01-08 at 1h —
    # the same one-class-later pattern the 4h row above already records. Probed, not
    # inherited.
    ("us_etfs", "1h"):  {"start": "2020-02-10", "window_days": 700},
    ("us_etfs", "30m"): {"start": "2020-02-10", "window_days": 380},
    # Probed 2026-08-22: SPY 15min also 2020-02-10 — the vendor's intraday floor for this
    # class is one date across 1h, 30m and 15m, not a per-interval ladder.
    ("us_etfs", "15m"): {"start": "2020-02-10", "window_days": 190},

    # Probed 2026-08-09: XAU/USD 1979-12-26, XAG/USD 1982-07-01, WTI/USD 1983-03-30,
    # XPT and XPD only 2012-09. Start from gold's own beginning — 46 years is the deepest
    # series in this repo by a decade and the reason the class is worth carrying at all.
    # 4h begins 2020-01 for the metals and 2020-10 for WTI. These pairs quote ~24/5 with
    # a short daily settlement break, so a 4h window holds ~6 bars a weekday.
    ("commodities", "1d"): {"start": "1979-12-01", "window_days": 4000},
    ("commodities", "4h"): {"start": "2020-01-20", "window_days": 1000},
    # Same addition, same reason. The start is LATER than the equity classes' because
    # this class's intraday archive is thinner: the 2026-08-03 probe found only gold,
    # silver and WTI carrying usable intraday at all, and WTI 1-minute beginning
    # 2020-10. Ask for the whole class at 1m and the platinum/palladium requests come
    # back empty rather than erroring, which is why `run_intraday_ha_5m.sh` names the
    # three symbols explicitly instead of taking the class default.
    ("commodities", "5m"): {"start": "2020-01-20", "window_days": 64},
    ("commodities", "1m"): {"start": "2020-10-01", "window_days": 12},
    # 1h and 30m probed 2026-08-22: XAU/USD serves both from 2020-01-24, WTI/USD only
    # from 2020-10-05 — the same three-symbol thinness as the rows above, so fetch these
    # with `--symbols XAU/USD XAG/USD WTI/USD` rather than the class default. ~46 bars
    # per ~23h weekday at 30m; the window sizes keep one request under the 5000-bar cap.
    ("commodities", "1h"):  {"start": "2020-01-20", "window_days": 280},
    ("commodities", "30m"): {"start": "2020-01-20", "window_days": 140},
    # Probed 2026-08-22 at 15min: XAG/USD 2020-01-22, XAU/USD 2020-01-24,
    # WTI/USD 2020-10-05. The 2020-01-20 start covers the earliest of the three and the
    # loader simply gets nothing before each symbol's own first bar.
    ("commodities", "15m"): {"start": "2020-01-20", "window_days": 70},

    # 2010-06-06 is the first day of Databento's CME archive and there is nothing before
    # it at any price, so unlike every other row here this is a hard floor rather than a
    # probe result. `window_days` is unused for this class: `db_loader` chunks by years
    # and is bounded by response size, not by a bar cap, because Databento streams.
    #
    # **1d and no intraday row, deliberately.** The vendor's hourly archive for this
    # dataset is incomplete before 2013 — on affected days it collapses a whole session
    # into one or two bars, so June 2011 returns 230 hourly bars where ~500 exist, while
    # `ohlcv-1d` over the same days is complete and its volumes tie out to the hourly sum
    # exactly. A 4h sheet cut from that would be silently wrong over the first third of
    # the sample. Rebuilding bars from the `trades` schema would fix it and is metered at
    # $28/GB before 2026, roughly $100 per root per year; see `db_loader`'s docstring.
    ("cme_futures", "1d"): {"start": "2010-06-06", "window_days": 4000},

    ("crypto", "1d"):  {"start": "2017-08-29", "window_days": 4000},
    ("crypto", "4h"):  {"start": "2020-01-07", "window_days": 580},
    ("crypto", "2h"):  {"start": "2020-01-07", "window_days": 290},
    ("crypto", "1h"):  {"start": "2020-01-07", "window_days": 145},
    # Probed 2026-08-22: BTC/USD 30min earliest_timestamp 2020-01-06, the same era as
    # 1h/2h/4h. 48 bars a day at 24/7, so 72 days is ~3,450 bars — half the 1h window,
    # same headroom under the cap.
    ("crypto", "30m"): {"start": "2020-01-07", "window_days": 72},
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
#
# RETIRED 2026-08-08. These four were the acceptance definition for three studies and
# they are wrong about what they measure, not merely strict: they rank on information
# ratio against buy-and-hold, which compares a rule in the market part of the time
# against one that always is. `return = skill x leverage` — skill is scale-invariant,
# leverage is a dial — so an IR gate scores capital deployment as much as skill. The
# same trades give `ibs` IR -0.170 ("fails all four") and Sharpe 0.658 vs 0.629 with
# ~7x the money at matched risk ("4 of 6"). `EDGE_STANDARD` below is the definition now.
#
# Kept, because the `gate_*` and `gates_passed` columns already written across both
# results directories were computed against this list and would otherwise be
# uninterpretable. Nothing renders a verdict from it any more.
LEGACY_GATES = [
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

# ---------------------------------------------------------------- THE edge standard
#
# `GATES` is an alias, so anything that imports `GATES` gets this. It is the project's
# single acceptance definition as of 2026-08-08.
#
# **A verdict is never stored in a sweep's CSV.** Only `walk-forward
# optimization/riskmatch_wf.py` can compute these six — they need risk-matched sizing,
# the T-bill path, per-fold Sharpe and two signal-free controls, none of which the
# IR-based sweeps have. So the sweeps emit *diagnostics* and
# `results/edge_standard.csv` is the one place a pass or fail exists. A pipeline that
# cannot compute the standard must not render a verdict against it.
#
# The threshold was validated by power analysis, not chosen: on us_stocks 1d the measured
# fold-to-fold sd of delta-Sharpe is 0.258 over 54 folds, so the minimum detectable effect
# is +0.070 and a true edge of +0.10 is caught 81% of the time — the conventional target.
# +0.15 was needlessly strict. For calibration, the S&P's own Sharpe is ~0.6 and
# practitioners call 1.0 good for a systematic strategy, so +0.10 (0.63 -> 0.73) sits well
# below anything anyone calls impressive.
GATES = EDGE_STANDARD = [
    {"key": "dsharpe", "letter": "S", "min": 0.10,
     "label": "delta-Sharpe vs buy-and-hold, mean of per-fold values",
     "target": ">= +0.10", "note": "about +3%/yr at matched risk on mega-caps"},
    {"key": "t", "letter": "T", "min": 2.0,
     "label": "t on the per-fold delta-Sharpe, across FOLDS",
     "target": ">= 2.0",
     "note": "across time, never across assets: 20 correlated mega-caps in one month are "
             "close to one observation, and 11,500 daily bars are not 11,500 draws"},
    {"key": "vs_random", "letter": "R", "min": 0.0,
     "label": "beats an exposure-matched random control",
     "target": "> 0",
     "note": "does the timing beat a coin flip at the same time-in-market? kills "
             "'it is just less beta'"},
    {"key": "vs_constant", "letter": "C", "min": 0.0,
     "label": "beats a constant weight at the same average exposure",
     "target": "> 0 MAR",
     "note": "does timing beat simply owning less, all the time, with no signal? kills "
             "'drawdown reduction anyone can buy'"},
    {"key": "wealth", "letter": "W", "min": 0.0,
     "label": "more money than buy-and-hold once sized to equal risk",
     "target": "> 0",
     "note": "causally sized from trailing vol, cash credited at T-bills, financing at "
             "benchmark+1.5%, capped 2x"},
    {"key": "headroom", "letter": "H", "min": 3.0,
     "label": "multiples of the real fee schedule the equal-risk advantage survives",
     "target": ">= 3x",
     "note": "measured on wealth, NOT on IR — metrics.cost_headroom returns 0.00x "
             "whenever gross IR <= 0, which reads like a measurement and means undefined"},
]

# Breadth across assets is deliberately NOT a gate any more. The old 70% bar treated 20
# co-moving mega-caps as 20 independent tests. Report it; do not decide on it.
EDGE_REPORT_ONLY = ("breadth", "loo_retention", "long_frac", "turnover_yr", "max_leverage")

# A sheet may only be judged if it could have detected the effect being claimed. On five
# of six sheets the noise ceiling sits above the threshold, so neither passing nor failing
# there means anything — see `walk-forward optimization/gate_calibration.py`.
EDGE_MIN_FOLDS = 20              # below this, the per-fold t has no resolution

# Rankability preconditions. Not gates — a row failing these is not a bad strategy, it is
# not a strategy, and it must be kept OUT of the ranking rather than scored badly.
#
# A ratio objective rewards doing nothing, and delta-Sharpe is a ratio. Measured directly:
# `CDLKICKING` never fires at all — 0% exposure, zero turnover, pure cash — and it topped
# the whole 804-row us_stocks 1d sheet at delta-Sharpe **+0.949**, because a cash series has
# almost no variance and most of its folds are 0/0 and drop out, leaving a "mean" over a
# couple of surviving folds. Exactly the trap `MIN_IR_COVERAGE` exists for one metric up.
EDGE_MIN_EXPOSURE = 0.10         # must hold a position on >=10% of the scored bars
EDGE_MIN_FOLD_FRAC = 0.50        # ...and be scoreable on >=half the sheet's folds


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
CLASS_DIR = {"us_stocks": "stocks", "crypto": "crypto", "us_etfs": "etfs",
             "commodities": "commodities", "cme_futures": "futures"}
# Adding a class to CLASSES without adding it here fetches seven stages and then dies on
# a bare KeyError in `cache_dir` at the eighth, after the in-memory frames for that class
# have already been downloaded and cannot be recovered without refetching.
assert set(CLASS_DIR) == set(CLASSES), (
    f"CLASS_DIR is missing {set(CLASSES) - set(CLASS_DIR)}")


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
    """The vendor start date and page size for one (class, timeframe).

    The error message is the whole reason this is not a bare subscript. `WINDOWS` is
    sparse — it carries only the pairs somebody has needed — and a missing pair is a gap
    in the table, never a statement that the vendor lacks the data. On 2026-08-21 that
    distinction cost a batch: `us_etfs` and `commodities` had no intraday rows, so a 1m
    fetch for both died on `KeyError: ('us_etfs', '1m')` and the shell's own summary line
    still reported the batch DONE. Nothing was written and nothing said so.
    """
    try:
        return WINDOWS[(asset_class, timeframe)]
    except KeyError:
        have = sorted(tf for cls, tf in WINDOWS if cls == asset_class)
        raise KeyError(
            f"config.WINDOWS has no entry for ({asset_class!r}, {timeframe!r}). "
            f"That class currently carries {have}. This is a GAP IN THE TABLE, not a "
            f"vendor limit — add a row with the interval's `earliest_timestamp` as "
            f"`start` and a `window_days` that keeps one request under the 5000-bar "
            f"response cap, then refetch."
        ) from None
