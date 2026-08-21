"""Replicate the "Simple SeekingAlpha Rotational Strategy" post, then price what it claims.

The claim, in one line: hold whichever ETF in a small basket had the best 3-month total
return, re-picked monthly, and you beat SPY roughly 2x since 2000.

Source: papertoprofit.substack.com/p/simple-seekingalpha-rotational-strategy (2025-04-05),
which is itself a replication of a 2013 Seeking Alpha article by "Logical-Trader" claiming
41.4% a year since 2003. The blogger's own code is on screen in the post and in
`examples/SimpleSeekingAlphaStrategy.ipynb` of github.com/StuartFarmer/portwine; the class
is `GlobalMarketRotationStrategy` and the harness is `portwine`. Both were read before this
file was written, so what follows is a port of an implementation, not of a description.

TWO MODES, and the difference between them is the whole point of the file.

`--mode faithful` reproduces the post's arithmetic including its defects, so that a
disagreement with the published numbers is attributable to data rather than to method:

  * the rebalance test is `date.day == calendar.monthrange(y, m)[1]` -- the last CALENDAR
    day, not the last trading day. The docstring above it says "last trading day". Roughly
    three months in ten end on a weekend or a holiday, and in those months the backtest
    never rebalances at all. Reproduced here as `--month-end calendar`;
  * no costs of any kind;
  * the benchmark is SPY, which is not in most of the baskets and is never the thing the
    rotation is choosing between;
  * Sharpe is `CAGR / annual vol` with no risk-free rate, and "years" is `bars / 252`.
    That is `portwine.analyzers.equitydrawdown.analyze_returns`, and it is reproduced
    exactly, because 10.12 / 20.49 = 0.49 is how the post's headline Sharpe was made.

`--mode honest` changes five things, each of which is a rule this repo already applies to
its own work and none of which is a matter of taste:

  * rebalance on the last TRADING day of the month;
  * charge the ETF fee schedule from `config.FEE_SCENARIOS`;
  * benchmark against an EQUAL-WEIGHT BOOK OF THE SAME BASKET, rebalanced on the same
    days. That benchmark differs from the strategy in exactly one thing -- the signal --
    which is what makes the difference attributable to skill. SPY is still reported, as
    the second reading, because both are purchasable;
  * a RANDOM-PICK control on the same basket and the same dates. A rotation is a bet that
    the RANKING is informative; a control that rotates on a coin toss holds the basket's
    concentration and turnover constant and prices everything except the ranking. This is
    the same idea as `ir_vs_random` in the sweep stages, moved from exposure to selection;
  * significance, as `t = IR * sqrt(years)` against the matched benchmark, plus a
    Newey-West t on the monthly excess series.

THE DATA PROBLEM, WHICH IS ONE TICKER AND IS FIXABLE:

**Twelve Data's EEM begins 2008-01-02 and its first year is wrong.** The fund listed
2003-04-14, so 1,188 sessions and +371.8% of total return are missing outright; and on
2008 the cached bars disagree with Yahoo's by a mean of 166 bp PER DAY, against 0.0-0.6
bp/day for every year from 2009 on. The post's headline basket (`broad`: SPY QQQ IWM EFA
EEM TLT SHV) is the one whose result the post attributes mostly to EEM -- "EEM is the
winner" -- so this is not a marginal series.

It is also the only bad one. Comparing both vendors across all fifteen tickers these
baskets use, EEM's mean |daily return difference| is 9.02 bp and every other name --
SPY, QQQ, IWM, EFA, TLT, SHV, MDY, IEV, ILF, EPP, EDV, SHY, GLD, VWO -- lands between
0.01 and 0.54 bp. That scan is what licenses `--alt-source` (see `ALT_SOURCE` below): a
second vendor is usable for the span the first one lacks only once it has been shown to
agree with the first on the span they share.

Both readings are kept. The default is the Twelve Data cache, because one named vendor
per asset class is this project's rule and the results on disk were produced under it;
`--alt-source` opts in and every table carries a `source` column saying which. Every
table also reports the 2008-onward sub-period, where both vendors are whole and agree.

Run from this folder. `python rotation.py --help`.
"""
from __future__ import annotations

import argparse
import calendar as _calendar
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import wfo_paths                                    # noqa: F401  (path bootstrap first)
from wfo_paths import RESULTS_DIR

import config                                       # noqa: E402
from engines.vector import bars_per_year            # noqa: E402
from stockhunt import rotation as sh_rotation        # noqa: E402
from stockhunt import stats                          # noqa: E402

REPO = Path(wfo_paths.REPO)
ETF_DIR = REPO / "data" / "etfs" / "1d"
STOCK_DIR = REPO / "data" / "stocks" / "1d"
# Spot metals and oil, quoted FX-style. Reachable so a basket can carry gold from 2000
# instead of from GLD's 2004 listing -- the ETF's absence in the dot-com bust is a real
# handicap when testing "always hold gold", and `XAU/USD` starts 1979. Their cost
# schedule is a dealer spread rather than the ETF one, so a basket mixing them is
# charged too cheaply here; treat those rows as an upper bound on gold's contribution.
COMMODITY_DIR = REPO / "data" / "commodities" / "1d"
RATES = REPO / "data" / "rates" / "DTB3.csv"

# ---------------------------------------------------------------- the second source
#
# `data/_alt_source/` is a SECOND VENDOR'S copy of a series, kept deliberately outside
# `data/etfs/1d/` so that nothing else in this repo can read it by accident. One ticker
# is in it, and it is here because Twelve Data's copy is measurably wrong rather than
# merely short:
#
#   * Their EEM begins 2008-01-02. The fund listed 2003-04-14, so 1,188 sessions and
#     +371.8% of total return are simply absent -- and 2003-2007 is the emerging-market
#     boom that is the whole reason this basket held EEM.
#   * Their 2008 bars disagree with Yahoo's by a mean of 166 bp PER DAY. From 2009 to
#     2026 the same two series agree to 0.0-0.6 bp/day, so this is not an adjustment
#     convention, it is a bad first year on a truncated series.
#
# Both were established by comparing the two vendors across all fifteen tickers these
# baskets use. EEM is the only outlier in the set: mean |daily return difference| of
# 9.02 bp against 0.01-0.54 bp for every other name, SPY through EDV. That measurement is
# what licenses the substitution -- a second source is only usable when it has been shown
# to agree with the first everywhere it can be checked.
#
# `check_data.py` cannot find this. The bars are not malformed; they are the right
# instrument with the wrong numbers, which is the same shape of defect as the foreign
# namesakes in `td_loader.US_LISTED_CLASSES` and needs the same kind of answer: an
# external cross-check, not a bar-level test.
#
# OFF by default. This project's rule is one named vendor per asset class and results on
# disk were produced under it; `--alt-source` opts in, and every table says which.
ALT_DIR = REPO / "data" / "_alt_source"
ALT_SOURCE = {"EEM": "Twelve Data starts 2008-01-02 (fund listed 2003-04-14) and its "
                     "2008 bars disagree with Yahoo by 166 bp/day; 2009-2026 agree to "
                     "under 1 bp/day"}

# The seven baskets the notebook runs, verbatim from cells 2 and 11. Order preserved
# because "how many baskets were tried" is itself a result later in this file.
#
# ITE (SPDR Barclays Intermediate Term Treasury) is the one substitution: the ticker was
# retired in the 2018 SPDR renaming and the vendor serves nothing under it, so SPTI --
# the same fund under its current ticker -- stands in. Recorded rather than silently
# swapped, because a reader comparing basket lists will otherwise find a name that is
# not in the post.
BASKETS = {
    "base":      ["MDY", "IEV", "EEM", "ILF", "EPP", "EDV", "SHY"],
    "sector":    ["XLP", "XLY", "XLF", "XLV", "XLK", "TLT", "SHY"],
    "broad":     ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "SHV"],
    "commodity": ["GLD", "SLV", "USO", "DBA", "TLT", "SHY"],
    "stocks":    ["AAPL", "TSLA", "XOM", "JNJ", "TLT", "BIL"],
    "extended":  ["XLF", "XLE", "XLK", "XLY", "XLP", "XLV", "XLI", "XLB", "XLU",
                  "TLT", "SHY"],
    "vanguard":  ["VTI", "VEA", "VWO", "BND", "BIV", "VGSH"],
    "spdrgold":  ["SPY", "MDY", "SPEM", "SPDW", "GLD", "SPTI", "BIL"],
    # NOT one of the source's. This is the basket the LIVE DESK trades, and it exists
    # because `paper_config.UNIVERSE["us_etfs"]` is five names and the desk refuses any
    # symbol outside it -- trading `broad` would mean adding four ETFs and restarting the
    # desk, which flattens every open book and re-warms 1,500 bars.
    #
    # **It was chosen after seeing results**, from the four desk-tradable variants, and
    # that is a search of four that nothing pre-registered. It scores a shade better than
    # `broad` on return and Sharpe and a shade worse on t, so the choice is close to a
    # coin toss and should be read as "what the desk could trade without disruption",
    # never as "the best basket". GLD is out on the measurement in `--add GLD`, not on
    # taste.
    "desk4":     ["QQQ", "IWM", "XLK", "TLT"],
}
SUBSTITUTIONS = {"SPTI": "ITE (retired ticker; same fund)"}

# The post is dated 2025-04-05 and its charts stop there. `--end` defaults to this so the
# replication is scored over the window the published numbers were scored over; the cache
# runs a further sixteen months, which `--end ""` uses.
POST_END = "2025-04-04"

ANN = 252.0          # portwine's fixed annualisation factor, kept for its metrics only


# --------------------------------------------------------------------------- data


def refresh_alt(symbols: list[str] | None = None) -> None:
    """Refetch the second-source series and re-prove they agree where they overlap.

    The proof is the point. A second vendor is not evidence because it is a second
    vendor; it is evidence because on the span where both have data they agree to within
    a basis point a day. This prints that comparison every time and refuses to write a
    series that fails it, so the substitution can never quietly become a guess.
    """
    import yfinance as yf
    ALT_DIR.mkdir(parents=True, exist_ok=True)
    (ALT_DIR / "README.md").write_text(
        "# Second-source price series\n\n"
        "Not the vendor cache. Written by `walk-forward optimization/rotation.py "
        "--refresh-alt` and read by nothing else in this repo, only under its explicit "
        "`--alt-source` flag. Source: Yahoo Finance via yfinance, `auto_adjust=True`.\n\n"
        "A series lands here only when it has been shown to track the Twelve Data copy "
        "to under 1 bp/day on their overlap. Rerun `--refresh-alt` to re-prove it.\n\n"
        + "".join(f"* **{s}** -- {why}\n" for s, why in ALT_SOURCE.items()),
        encoding="utf-8")
    for s in (symbols or ALT_SOURCE):
        y = yf.download(s, start="1990-01-01", auto_adjust=True, progress=False,
                        actions=False)
        y = y["Close"][s] if isinstance(y.columns, pd.MultiIndex) else y["Close"]
        y.index = pd.to_datetime(y.index).tz_localize(None).normalize()
        y = y.dropna().astype("float64")
        t = pd.read_parquet(ETF_DIR / f"{s}.parquet")["Close"]
        t.index = t.index.normalize()
        j = pd.concat({"y": y, "t": t}, axis=1).dropna()
        d = (j["y"].pct_change() - j["t"].pct_change()).abs()
        by_year = d.groupby(j.index.year).mean() * 1e4
        clean = by_year.drop(index=by_year.idxmax())      # the known-bad first year
        print(f"{s}: yahoo {y.index[0].date()}..{y.index[-1].date()} ({len(y)} bars) | "
              f"overlap {len(j)} bars | worst year {by_year.idxmax()} at "
              f"{by_year.max():.1f} bp/day | every other year <= {clean.max():.2f} bp/day")
        if clean.max() > 1.0:
            raise SystemExit(f"{s}: the two vendors disagree by {clean.max():.2f} bp/day "
                             f"outside {by_year.idxmax()} -- too much to substitute on. "
                             f"Nothing written.")
        y.to_frame("Close").to_parquet(ALT_DIR / f"{s}.parquet")
        print(f"   wrote {ALT_DIR / f'{s}.parquet'}")


def load_closes(symbols: list[str], alt: bool = False) -> pd.DataFrame:
    """Adjusted closes, one column per symbol, read straight from the parquet cache.

    Deliberately NOT `td_loader.load`. That function applies `config.BACKTEST_START` and
    the ETF liquidity entry cut, and both would change the answer: the entry cut alone
    moves TLT from 2002 to 2003-10 and EFA from 2001 to 2003-12, so a "faithful"
    replication routed through it would be scored on a shorter history than the post's
    and the disagreement could not be attributed. The cuts are real and this repo applies
    them everywhere else; `--screened` turns them on here so both readings exist.
    """
    out = {}
    for s in symbols:
        dirs = ((ALT_DIR,) if alt and s in ALT_SOURCE else ()) + (ETF_DIR, STOCK_DIR,
                                                                 COMMODITY_DIR)
        for d in dirs:
            p = d / f"{s.replace('/', '_')}.parquet"
            if p.exists():
                out[s] = pd.read_parquet(p)["Close"].astype("float64")
                break
        else:
            raise SystemExit(f"{s}: no cached bars. Fetch it first with "
                             f"`python td_loader.py --class us_etfs --tf 1d "
                             f"--symbols {s}` from `backtest engine/`"
                             + (f", or `python rotation.py --refresh-alt` for the "
                                f"second-source copy" if s in ALT_SOURCE else "") + ".")
    return pd.DataFrame(out)


def screened_closes(symbols: list[str]) -> pd.DataFrame:
    """The same closes with this repo's own cuts applied -- 2000 start, ETF entry dates."""
    sys.path.insert(0, str(REPO / "backtest engine"))
    import td_loader
    frames = {}
    for cls in ("us_etfs", "us_stocks"):
        got = td_loader.load(cls, "1d", symbols)
        for s, df in got.items():
            frames.setdefault(s, df["Close"].astype("float64"))
    missing = [s for s in symbols if s not in frames]
    if missing:
        raise SystemExit(f"screened load dropped {missing} entirely")
    return pd.DataFrame(frames)


def trading_calendar(closes: pd.DataFrame) -> pd.DatetimeIndex:
    """Every NYSE session in the study window, taken from SPY's own index.

    portwine builds this from `pandas_market_calendars` and then reads each ticker on
    every session, writing NaN where a ticker has no bar. SPY has traded every session
    since 1993, so its index IS that calendar and needs no extra dependency. The
    distinction matters because a session with no bar for a ticker is what makes that
    ticker ineligible, and a session missing from the calendar is what makes a
    calendar-month-end rebalance silently not happen.
    """
    spy = ETF_DIR / "SPY.parquet"
    idx = pd.read_parquet(spy).index
    lo, hi = closes.index.min(), closes.index.max()
    return idx[(idx >= lo) & (idx <= hi)]


def cash_per_bar(index: pd.DatetimeIndex, bpy: float) -> np.ndarray:
    """Per-bar 3-month T-bill return. Same series and same reading as `riskmatch_wf`."""
    df = pd.read_csv(RATES, parse_dates=["observation_date"])
    s = (pd.to_numeric(df["DTB3"], errors="coerce") / 100.0)
    s = s.set_axis(df["observation_date"]).ffill().dropna()
    r = np.nan_to_num(s.reindex(index.normalize(), method="ffill").to_numpy(), nan=0.0)
    return (1.0 + r) ** (1.0 / bpy) - 1.0


# ----------------------------------------------------------------------- the rule


def rebalance_days(index: pd.DatetimeIndex, mode: str) -> np.ndarray:
    """Boolean mask of the sessions on which the rotation re-picks.

    `calendar` is the post's own test and it is a defect: `date.day == monthrange(...)[1]`
    only fires when the last calendar day of the month IS a session. When the 31st is a
    Saturday there is no such session, the condition never fires, and that month's
    rotation does not happen -- the previous month's pick simply rides. `--month-end
    trading` is the correction, and the two are reported side by side rather than one
    being quietly preferred, because the difference between them is a measurement of how
    much of the published result is a scheduling accident.
    """
    if mode == "calendar":
        last = np.array([_calendar.monthrange(d.year, d.month)[1] for d in index])
        return index.day.to_numpy() == last
    if mode == "trading":
        month = index.year * 12 + index.month
        return np.r_[month[:-1] != month[1:], True]
    if mode == "first":
        # The FIRST session of each month rather than the last. One session later, and
        # the reason it exists is that it is the only one of the three a live process can
        # determine EXACTLY without a holiday calendar: "was the previous session in a
        # different month" is answerable from bars already printed, where "is today the
        # last session of this month" needs to know the future. `rotation_manager.py`
        # trades this schedule; see the note there.
        month = index.year * 12 + index.month
        return np.r_[True, month[1:] != month[:-1]]
    raise ValueError(mode)


def scores(closes: pd.DataFrame, index: pd.DatetimeIndex, lookback: int,
           f_vol: float) -> np.ndarray:
    """(T, N) matrix of the rotation score, NaN where the ticker is not yet eligible.

    Delegates to `stockhunt.rotation.scores`, which is the ONE definition — the live
    desk's manager computes its monthly pick from that same function. A second copy here
    would be free to drift, and a live book drifting away from the research that justified
    it is invisible: every number still looks reasonable.

    Causality: row t uses closes at t and t-lookback and nothing later. The weights this
    produces are shifted one session before they earn anything (see `book`), so the close
    that decides is never the close that fills.
    """
    return sh_rotation.scores(closes.reindex(index), lookback, f_vol)


def rotate(sc: np.ndarray, rb: np.ndarray, mode: str = "best",
           rng: np.random.Generator | None = None,
           freq: np.ndarray | None = None) -> np.ndarray:
    """Score matrix -> (T, N) weights. 100% in one name, held until the next rebalance.

    `mode="best"` is the strategy. `mode="equal"` spreads across every eligible name and
    is the matched benchmark. The other two are controls, and they ask different
    questions -- a rotation can win for two quite separate reasons and only one of them
    is skill:

      `random`  uniform among the eligible names. Holds the basket, the one-name
                concentration, the turnover and the rebalance dates fixed, so the only
                thing removed is the ranking. Beating it means the rule did SOMETHING.
                But a momentum rule sits in equities most of the time and a uniform draw
                sits in bonds a seventh of the time, so part of any win here is the asset
                MIX rather than the timing.
      `freq`    draws from the strategy's OWN realised pick frequencies, renormalised
                over whoever is eligible that month. The mix is then held fixed too and
                only the timing is destroyed. This is the control that isolates "it knew
                WHEN to be in emerging markets" from "it was in equities a lot".

    Weights persist between rebalance days and are NOT drifted with prices, which is what
    the original does (`self.current_weights` is only written on a rebalance day and
    returned verbatim on every other). For a single name at 1.0 that is exact; for the
    equal-weight benchmark it means a daily reset to equal weight, and the benchmark has
    to be built the same way as the strategy or the comparison stops being about signal.
    """
    t, n = sc.shape
    w = np.zeros((t, n))
    cur = np.zeros(n)
    for i in np.flatnonzero(rb):
        valid = np.flatnonzero(np.isfinite(sc[i]))
        if valid.size:
            nxt = np.zeros(n)
            if mode == "best":
                nxt[valid[np.argmax(sc[i, valid])]] = 1.0
            elif mode == "random":
                nxt[rng.choice(valid)] = 1.0
            elif mode == "freq":
                p = freq[valid]
                # A month in which none of the eligible names was ever picked by the real
                # rule falls back to uniform rather than dividing by zero.
                p = p / p.sum() if p.sum() > 0 else np.full(valid.size, 1.0 / valid.size)
                nxt[rng.choice(valid, p=p)] = 1.0
            elif mode == "equal":
                nxt[valid] = 1.0 / valid.size
            else:
                raise ValueError(mode)
            cur = nxt
        w[i:] = cur          # persists forward until the next rebalance overwrites it
    return w


def book(weights: np.ndarray, closes: pd.DataFrame, index: pd.DatetimeIndex,
         fee: dict | None) -> np.ndarray:
    """Weights -> the book's net return per session.

    `held = weights.shift(1)`: the pick made at session t's close earns from t+1. That is
    `np.roll(sig_array, 1)` in portwine and `held[1:] = position[:-1]` in this repo's
    vector engine -- the two agree, so the post is not carrying a same-bar fill and this
    is not where its numbers come from.

    Costs use the same three charges as `engines/vector.py`, moved from one asset to the
    book: commission and half-spread on every unit of |dw| summed across names, and the
    US sell-side fee on the falling half only. There is no borrow term because the rule
    never goes short.
    """
    p = closes.reindex(index).to_numpy(dtype="float64")
    r = np.zeros_like(p)
    r[1:] = p[1:] / p[:-1] - 1.0
    r = np.nan_to_num(r, nan=0.0)          # portwine's own treatment of a missing bar

    held = np.zeros_like(weights)
    held[1:] = weights[:-1]
    gross = (held * r).sum(axis=1)
    if fee is None:
        return gross

    d = np.zeros_like(weights)
    d[0] = weights[0]
    d[1:] = np.diff(weights, axis=0)
    per_side = (fee["commission_bps"] + fee["half_spread_bps"]) / 10_000.0
    cost = (np.abs(d).sum(axis=1) * per_side
            + np.maximum(-d, 0.0).sum(axis=1) * fee["sell_fee_bps"] / 10_000.0)
    return gross - cost


# ------------------------------------------------------------------------ metrics


def portwine_stats(r: np.ndarray) -> dict:
    """The post's own definitions, reproduced exactly so its numbers can be checked.

    Two of them are not the conventional thing and both flatter a volatile book:
    "years" is bar count over 252 rather than elapsed calendar time, and Sharpe is
    geometric CAGR over arithmetic volatility with NO risk-free rate. On the broad basket
    the post reports 10.12 / 20.49 = 0.49, which is how these three lines are known to be
    the right ones. A cash rate of 3% over the window would take that 0.49 to about 0.34.
    """
    x = pd.Series(r).dropna()
    total = float((1 + x).prod() - 1.0)
    years = len(x) / ANN
    cagr = (1 + total) ** (1 / years) - 1.0
    vol = float(x.std() * np.sqrt(ANN))
    eq = (1 + x).cumprod()
    return {"total_return": total, "cagr": cagr, "ann_vol": vol,
            "sharpe": cagr / vol if vol > 1e-9 else 0.0,
            "max_dd": float(((eq - eq.cummax()) / eq.cummax()).min())}


def newey_west_t(x: np.ndarray, lags: int = 3) -> float:
    """t on the mean of `x` with a Newey-West correction for autocorrelation.

    Applied to MONTHLY excess returns. A rotation holds one name for a month at a time,
    so its excess series is autocorrelated by construction and a plain t over-states.
    """
    x = np.asarray(x, dtype="float64")
    x = x[np.isfinite(x)]
    n = x.size
    if n < 12:
        return float("nan")
    e = x - x.mean()
    v = float(e @ e) / n
    for L in range(1, min(lags, n - 1) + 1):
        c = float(e[L:] @ e[:-L]) / n
        v += 2.0 * (1.0 - L / (lags + 1.0)) * c
    if v <= 0:
        return float("nan")
    return float(x.mean() / np.sqrt(v / n))


def monthly(r: np.ndarray, index: pd.DatetimeIndex) -> pd.Series:
    return (1 + pd.Series(r, index=index)).resample("ME").prod() - 1.0


def repo_stats(net: np.ndarray, bench: np.ndarray, index: pd.DatetimeIndex,
               rf: np.ndarray, bpy: float) -> dict:
    """This repo's own reading of the same series, plus the comparison to the benchmark.

    `t_stat` is `IR * sqrt(years)`, the bar used by every gate in this project. `nw_t` is
    the same question asked of the monthly excess series with a lag correction, and the
    two are reported together because they disagree when the excess is autocorrelated,
    which for a monthly rotation it always is.
    """
    years = (index[-1] - index[0]).days / 365.25
    ex = net - bench
    sd = float(np.std(ex, ddof=1))
    ir = float(np.mean(ex) / sd * np.sqrt(bpy)) if sd > 1e-12 else float("nan")
    m = monthly(net, index).to_numpy() - monthly(bench, index).to_numpy()
    return {"years": years,
            "cagr_net": stats.cagr(net, bpy),
            "cagr_bench": stats.cagr(bench, bpy),
            "excess_cagr": stats.cagr(net, bpy) - stats.cagr(bench, bpy),
            "sharpe_rf": stats.sharpe(net, rf, bpy, min_obs=30),
            "sharpe_bench_rf": stats.sharpe(bench, rf, bpy, min_obs=30),
            "max_dd": stats.max_drawdown(net),
            "ir": ir,
            "t_stat": ir * np.sqrt(years),
            "nw_t": newey_west_t(m)}


# --------------------------------------------------------------------------- runs


def prepare(names: list[str], screened: bool, start: str | None, end: str | None,
            alt: bool = False):
    closes = screened_closes(names) if screened else load_closes(names, alt)
    idx = trading_calendar(closes)
    # portwine's default start is `earliest_any_date`: the first session on which ANY
    # basket member has a bar. Nothing is tradable before it and the calendar would
    # otherwise open years of empty rows.
    first = closes.dropna(how="all").index.min()
    if start:
        first = max(first, pd.Timestamp(start))
    idx = idx[idx >= first]
    if end:
        idx = idx[idx <= pd.Timestamp(end)]
    return closes, idx


def run_basket(name: str, mode: str, lookback: int, f_vol: float, month_end: str,
               fee_key: str, start: str | None, end: str | None, screened: bool,
               n_controls: int, seed: int, alt: bool = False) -> dict:
    names = BASKETS[name]
    closes, idx = prepare(names, screened, start, end, alt)
    bpy = bars_per_year(idx)
    rb = rebalance_days(idx, month_end)
    sc = scores(closes, idx, lookback, f_vol)

    fee = None
    if fee_key != "gross":
        fee = next(f for f in config.FEE_SCENARIOS["us_etfs"] if f["key"] == fee_key)

    w = rotate(sc, rb, "best")
    net = book(w, closes, idx, fee)

    eq = book(rotate(sc, rb, "equal"), closes, idx, fee)
    spy_closes = load_closes(["SPY"])
    spy = book(np.ones((len(idx), 1)), spy_closes, idx, fee)

    rf = cash_per_bar(idx, bpy)
    row = {"basket": name, "mode": mode, "tickers": " ".join(names),
           "start": str(idx[0].date()), "end": str(idx[-1].date()), "bars": len(idx),
           "rebalances": int(rb.sum()),
           "switches": int((np.abs(np.diff(w, axis=0)).sum(axis=1) > 1e-9).sum()),
           "lookback": lookback, "month_end": month_end, "fee": fee_key,
           "source": "alt" if alt and set(names) & set(ALT_SOURCE) else "twelvedata"}
    row.update({f"pw_{k}": v for k, v in portwine_stats(net).items()})
    row.update({f"spy_{k}": v for k, v in portwine_stats(spy).items()})
    row.update({f"eq_{k}": v for k, v in portwine_stats(eq).items()})
    row.update(repo_stats(net, eq, idx, rf, bpy))
    row.update({f"vs_spy_{k}": v for k, v in
                repo_stats(net, spy, idx, rf, bpy).items() if k in
                ("excess_cagr", "ir", "t_stat", "nw_t")})

    if n_controls:
        rng = np.random.default_rng(seed)
        # How often the real rule held each name, over rebalance days only. This is the
        # asset mix the `freq` control is asked to reproduce.
        picks = w[rb].sum(axis=0)
        picks = picks / picks.sum() if picks.sum() > 0 else np.ones(len(names))
        for tag, kind in (("ctl", "random"), ("mix", "freq")):
            draws = np.array([portwine_stats(
                book(rotate(sc, rb, kind, rng, picks), closes, idx, fee))["cagr"]
                for _ in range(n_controls)])
            row[f"{tag}_cagr_mean"] = float(draws.mean())
            # The share of control draws the real rule failed to beat -- a one-sided
            # p-value for "the ranking is informative", against a control that holds
            # everything except the ranking fixed.
            row[f"{tag}_pct_above"] = float((draws >= row["pw_cagr"]).mean())
    return row


# ---------------------------------------------------------------------------- cli


COLS = ["basket", "source", "start", "end", "bars", "rebalances", "switches",
        "pw_total_return", "pw_cagr", "pw_ann_vol", "pw_sharpe", "pw_max_dd",
        "spy_cagr", "eq_cagr", "excess_cagr", "ir", "t_stat", "nw_t",
        "ctl_cagr_mean", "ctl_pct_above", "mix_cagr_mean", "mix_pct_above"]


def show(rows: list[dict], cols: list[str] = COLS) -> pd.DataFrame:
    d = pd.DataFrame(rows)
    keep = [c for c in cols if c in d.columns]
    print(d[keep].to_string(index=False, float_format=lambda x: f"{x:9.4f}"))
    return d


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baskets", nargs="+", default=list(BASKETS),
                    choices=list(BASKETS))
    ap.add_argument("--mode", choices=["faithful", "honest", "both"], default="both")
    ap.add_argument("--lookback", type=int, default=63)
    ap.add_argument("--lookbacks", nargs="+", type=int, default=None,
                    help="sweep the lookback on every basket instead of one run")
    ap.add_argument("--f-vol", type=float, default=0.0)
    ap.add_argument("--add", nargs="+", default=None, metavar="SYMBOL",
                    help="append these tickers to every basket that lacks them. The "
                         "controlled way to ask 'is this basket missing something' -- "
                         "the alternative, comparing two baskets that differ in several "
                         "names, cannot attribute the difference to any of them. "
                         "`--add GLD` is the worked example")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=POST_END,
                    help=f"default {POST_END}, the date the post's charts stop. "
                         f"Pass an empty string for the full cache.")
    ap.add_argument("--screened", action="store_true",
                    help="apply this repo's 2000 start and ETF liquidity entry cuts")
    ap.add_argument("--alt-source", action="store_true",
                    help=f"use data/_alt_source/ for {', '.join(ALT_SOURCE)} instead of "
                         f"the Twelve Data cache. See ALT_SOURCE for why")
    ap.add_argument("--refresh-alt", action="store_true",
                    help="refetch the second-source series, re-prove the overlap, exit")
    ap.add_argument("--controls", type=int, default=0,
                    help="random-pick control draws per basket (honest mode)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=None, help="write the full table to results/")
    args = ap.parse_args()
    if args.refresh_alt:
        refresh_alt()
        return
    end = args.end or None
    if args.add:
        for b, names in list(BASKETS.items()):
            extra = [s for s in args.add if s not in names]
            if extra:
                BASKETS[b] = names + extra
        print(f"added {args.add} to every basket that lacked them")
    if args.alt_source:
        missing = [s for s in ALT_SOURCE if not (ALT_DIR / f"{s}.parquet").exists()]
        if missing:
            raise SystemExit(f"--alt-source needs {missing}. Run "
                             f"`python rotation.py --refresh-alt` first.")
        print("second source in use: "
              + "; ".join(f"{s} ({why})" for s, why in ALT_SOURCE.items()))

    modes = ["faithful", "honest"] if args.mode == "both" else [args.mode]
    rows = []
    for mode in modes:
        cfg = dict(month_end="calendar", fee_key="gross", n_controls=0) \
            if mode == "faithful" else \
            dict(month_end="trading", fee_key="retail", n_controls=args.controls)
        print(f"\n=== {mode} === "
              + ("post's arithmetic, defects included, no costs, benchmark SPY"
                 if mode == "faithful" else
                 f"last trading day, {cfg['fee_key']} fees, benchmark = equal-weight "
                 f"basket"))
        for lb in (args.lookbacks or [args.lookback]):
            for b in args.baskets:
                rows.append(run_basket(b, mode, lb, args.f_vol, start=args.start,
                                       end=end, screened=args.screened,
                                       seed=args.seed, alt=args.alt_source, **cfg))
        cols = COLS if not args.lookbacks else ["lookback"] + COLS
        show([r for r in rows if r["mode"] == mode], cols)

    if args.out:
        p = RESULTS_DIR / args.out
        pd.DataFrame(rows).to_csv(p, index=False)
        print(f"\nwrote {p}")


if __name__ == "__main__":
    main()
