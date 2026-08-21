"""Stage 1h: score the product, not the parts.

Every sheet before this one reports a **median across per-symbol backtests**. Nobody
trades one symbol, so that statistic describes a thing that does not exist — and it is
not merely cosmetic, it is biased downward. Measured on `ibs` across the 20 mega-caps at
1d, the per-symbol median excess Sharpe is **+0.072** while the equal-weight portfolio of
the same trades scores **+0.156**. The median throws away half the signal, because the
idiosyncratic noise that swamps any single name diversifies away in a book and a median
cannot see it.

The same construction cuts the other way on risk, and that is the more important half:
diversification took the portfolio's volatility from 18.8% to 12.0% and its maximum
drawdown from **-52.4% to -53.6%** — that is, not at all. The de-risking `ibs` provides
is entirely idiosyncratic; the systematic crash it is implicitly sold as protection
against is untouched. No per-symbol sheet in this repo could have shown that.

Four things are computed here that no other stage can compute, each answering a question
the leaderboard raises and cannot settle:

    portfolio Sharpe        the number an investor experiences, PIT-membership aware
    block-bootstrap t       significance without a Memmel formula. A stationary block
                            bootstrap over 1-year blocks preserves the autocorrelation
                            and the pairing with the benchmark; this repo has already
                            been burned once by an analytic dSharpe z that was wrong by
                            ~15x, so the resampled version is the one that gets quoted.
    deflated Sharpe         `metrics.deflated_sharpe` — trial count AND the skew and
                            kurtosis of the actual series. On the ibs portfolio the
                            excess kurtosis is 66.6, which no 1/sqrt(T) error bar sees.
    factor alpha            `factors.regress` — is this an edge, or short-term reversal
                            wearing a different name? This is the question that decides
                            whether the project has found anything.

**Point-in-time membership.** For `us_stocks` the book holds a name only on the dates it
was actually in the S&P 500, per `sp500_membership`. Names are equal-weighted across
whoever is a member that bar, so the portfolio's composition changes underneath it
exactly as a real index-tracking book's would. Two caveats travel with every number this
produces and belong in any writeup:

  * membership is only trustworthy from 2007 (`sp500_membership.RELIABLE_FROM`) — before
    that Wikipedia's changelog is a highlight reel and the reconstruction drifts back
    toward today's index, which is the bias it exists to remove;
  * 107 departed names cannot be priced at all, so their final descent is invisible.
    68 of those were acquisitions, which are benign; 1 is a labelled bankruptcy.

Run::

    python portfolio_wf.py --tf 1d --rules ibs
    python portfolio_wf.py --class us_stocks --tf 1d --pit           # PIT membership
    python portfolio_wf.py --tf 1d --rules ibs volregime:hi:0.5:ibs volregime:lo:0.5:ibs
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from wfo_paths import RESULTS_DIR
from config import (CLASSES, EDGE_MIN_FOLDS, MIN_BARS, headline_scenario, scenario,
                    scenarios_for)
from engines import vector
import factors as ff
import metrics
import td_loader
import riskmatch_wf as rm
import walkforward as wfmod

from stockhunt import stats
from strategies.catalog import BASELINE, CONTROLS, build, cells

CAPITAL = 10_000.0
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 20260809
FINANCING_SPREAD = rm.FINANCING_SPREAD


# One implementation, in `stockhunt.stats`; the arguments below are what this module did
# before and are what keep its published numbers unchanged.
def _sharpe(r: np.ndarray, rf: np.ndarray, bpy: float) -> float:
    # `min_obs=30`, not the 3 `riskmatch_wf` uses. This is the defensible number — a
    # Sharpe from a handful of bars has an error bar wider than any value it reports —
    # and book-level series here are always long enough to clear it.
    return stats.sharpe(r, rf, bpy, min_obs=30)


def _max_dd(r: np.ndarray) -> float:
    # `dropna=False`: this sheet's published numbers were computed without the filter.
    return stats.max_drawdown(r, dropna=False)


def block_bootstrap_dsharpe(strat: np.ndarray, bench: np.ndarray, rf: np.ndarray,
                            bpy: float, draws: int = BOOTSTRAP_DRAWS) -> dict:
    """Resampled sampling distribution of (strategy Sharpe - benchmark Sharpe).

    Circular block bootstrap with one-year blocks, resampling the strategy, benchmark and
    cash legs *together* at the same indices. The pairing is the point: the two series
    share their market shocks, and breaking that link would inflate the spread of the
    difference and make every edge look less significant than it is.
    """
    n = strat.size
    d0 = _sharpe(strat, rf, bpy) - _sharpe(bench, rf, bpy)
    blk = max(2, int(bpy))
    nb = int(np.ceil(n / blk))
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    out = np.empty(draws)
    offsets = np.arange(blk)
    for i in range(draws):
        starts = rng.integers(0, n, nb)
        idx = (starts[:, None] + offsets[None, :]).ravel()[:n] % n
        out[i] = _sharpe(strat[idx], rf[idx], bpy) - _sharpe(bench[idx], rf[idx], bpy)

    # Degenerate draws are dropped, not propagated. A single NaN makes `out.std()` NaN,
    # and that is exactly what happened: a low-exposure rule — a regime gate at 8%
    # exposure, say — can be resampled into a set of blocks where it is flat throughout,
    # so its excess-of-cash series is all zeros, its Sharpe is 0/0, and one such draw in
    # 2,000 silently voided the t-stat for 690 of 1,840 regime rows. The rows looked as
    # though significance had not been computed; in fact it had been computed and thrown
    # away by one pathological resample.
    ok = out[np.isfinite(out)]
    if ok.size < draws // 10:
        # Too few usable draws to describe a distribution. Say so rather than quote a
        # number derived from the handful that happened to be well-behaved.
        return {"dsharpe": d0, "boot_se": float("nan"), "boot_t": float("nan"),
                "boot_p_gt0": float("nan"), "boot_ci_lo": float("nan"),
                "boot_ci_hi": float("nan"), "boot_draws": int(ok.size)}
    se = float(ok.std(ddof=1))
    return {
        "dsharpe": d0,
        "boot_se": se,
        "boot_t": d0 / se if se > 0 else float("nan"),
        "boot_p_gt0": float((ok > 0).mean()),
        "boot_ci_lo": float(np.percentile(ok, 2.5)),
        "boot_ci_hi": float(np.percentile(ok, 97.5)),
        "boot_draws": int(ok.size),
    }


def trade_stats(pos: np.ndarray, net: np.ndarray) -> tuple[int, int, float, float]:
    """(wins, trades, gross win, gross loss), counted PER TRADE not per bar.

    A trade is a maximal run of constant non-zero target — the same unit `curves.py` and
    `riskmatch_wf.profit_factor` use, so the three files quote the same statistic.

    Per-bar accounting would be a different and much less useful number: a rule that sat
    long through a rising year would score several hundred separate "wins", which
    collapses every long-biased rule onto a win rate near the market's own up-day
    frequency and tells you nothing about the rule.

    `held` is the position carried INTO each bar, matching the engine's one-bar shift, so
    a trade's return is compounded over exactly the bars its capital was at risk.
    """
    n = len(pos)
    if n == 0:
        return 0, 0, 0.0, 0.0
    held = np.empty_like(pos)
    held[0] = 0.0
    held[1:] = pos[:-1]
    held = np.nan_to_num(held, nan=0.0, posinf=0.0, neginf=0.0)

    # Segment boundaries: a trade is a maximal run of constant non-zero `held`.
    #
    # The bar-by-bar version of this was 17.7 ms per (symbol, rule) on a 14k-bar series,
    # which is 74 MINUTES of pure overhead on us_stocks/1d alone (704 x 358 calls) — more
    # than a third of the whole stage. `np.multiply.reduceat` reduces each run in the same
    # sequential order the loop did, so the products are bit-for-bit what they were; this
    # is a speed change, not an accuracy trade.
    starts = np.concatenate(([0], np.flatnonzero(np.diff(held) != 0) + 1))
    finite = np.isfinite(net)
    growth = 1.0 + np.where(finite, net, 0.0)
    prod = np.multiply.reduceat(growth, starts)
    # A run whose every bar is unpriced is not a trade. Substituting 0.0 for those bars
    # would score it as a flat trade and count it — inflating the trade count and, since
    # a zero return is not a win, quietly dragging the win rate down.
    n_finite = np.add.reduceat(finite.astype(np.int64), starts)

    active = (held[starts] != 0.0) & (n_finite > 0)
    r = prod[active] - 1.0
    trades = int(r.size)
    if not trades:
        return 0, 0, 0.0, 0.0
    win = r > 0
    return (int(win.sum()), trades,
            float(r[win].sum()), float(-r[~win].sum()))


def membership_mask(index: pd.DatetimeIndex, symbol: str, intervals) -> np.ndarray:
    """True on bars where `symbol` was an index member. All-True when not applying PIT."""
    if intervals is None:
        return np.ones(len(index), dtype=bool)
    spells = intervals[intervals["symbol"] == symbol]
    if spells.empty:
        return np.zeros(len(index), dtype=bool)
    m = np.zeros(len(index), dtype=bool)
    for row in spells.itertuples():
        lo = pd.Timestamp(row.start)
        hi = pd.Timestamp(row.end) if pd.notna(row.end) else index.max() + pd.Timedelta(days=1)
        m |= (index >= lo) & (index < hi)
    return m


def leg_resolver(asset_class: str, timeframe: str):
    """A `build` resolver that can also produce the 231 TA-Lib singles.

    Without this, any combo label resolves to None and the row is skipped — silently,
    because `registry.build` returns None rather than raising. On the us_stocks 1d
    leaderboard 22 of the top 25 rows are pairs, so a portfolio stage that cannot resolve
    them is reporting on a different board than the one being read.

    Published strategies are tried first and TA-Lib singles second, because a name that
    exists in both should mean the same thing here as it does in `cells()`.
    """
    import signals

    def resolve(label, df, close, bpy, symbol):
        pos = build(label, df, close, bpy, symbol)
        if pos is not None:
            return pos
        # `signals.position_for` owns the benchmark plumbing and end-of-day flattening,
        # so legs come back already flattened — the same order `position_for_row` uses.
        return signals.position_for(label, df, asset_class, timeframe,
                                    baseline_name=BASELINE)

    return resolve



_UNION_CACHE: dict = {}


def _union_index(keys: tuple, data: dict) -> pd.DatetimeIndex:
    """Sorted union of every symbol's index, computed once per (panel, symbol set).

    Cached because it is identical for every rule in a panel and for all six frames
    within a book — recomputing it was the single largest cost in the stage.

    The cache key MUST identify the bars, not just the symbols. Keyed on the symbol
    tuple alone, `us_etfs/4h` reused `us_etfs/1d`'s index — same 65 names, completely
    different timestamps (8,438 daily bars vs 3,274 four-hourly) — so every 4h stamp was
    absent from the cached index, `reindex` produced NaN, and a float array was used as
    an integer index. The fingerprint below adds bar count and endpoints so two panels
    over the same names cannot collide.
    """
    fp = (keys, sum(len(data[k].index) for k in keys),
          min(data[k].index[0] for k in keys),
          max(data[k].index[-1] for k in keys))
    hit = _UNION_CACHE.get(fp)
    if hit is not None:
        return hit
    idx = pd.DatetimeIndex([])
    for k in keys:
        idx = idx.union(data[k].index)
    idx = idx.sort_values()
    _UNION_CACHE[fp] = idx
    return idx


def _frame(series_map: dict, index: pd.DatetimeIndex) -> pd.DataFrame:
    """A DataFrame on a KNOWN index — no alignment, no frequency inference.

    Each column is scattered into a NaN-filled array by position, which is what pandas
    would have produced anyway, minus the union and the `infer_freq` call per column.
    """
    if not series_map:
        return pd.DataFrame(index=index)
    n = len(index)
    pos_of = pd.Series(np.arange(n), index=index)
    out = {}
    for name, ser in series_map.items():
        col = np.full(n, np.nan)
        pos = pos_of.reindex(ser.index).to_numpy()
        if np.isnan(pos).any():
            # Every series must lie inside the union index by construction. A NaN here
            # means the index does not belong to this panel at all — raise with the
            # mismatch named rather than letting numpy report an opaque dtype error.
            raise ValueError(
                f"{name}: {int(np.isnan(pos).sum())} of {len(pos)} timestamps are absent "
                f"from the union index ({index[0]}..{index[-1]}, {n} bars). The cached "
                f"index belongs to a different panel.")
        col[pos.astype(np.int64)] = ser.to_numpy()
        out[name] = col
    return pd.DataFrame(out, index=index)


_INDEX_CACHE: dict = {}


# Purchasable comparison lines drawn beside the class's own benchmark, never instead of
# it. `CLASSES[...]["benchmark"]` feeds the BETA and CORREL rules, the fee grid and the
# `idx_cashmatch_*` columns, so it is not the place to express "and also show me this
# one" — that is what this is. Presentation only: nothing scored reads it.
#
# QQQ on `us_stocks` because the universe IS a point-in-time top-100 US list and the index
# a reader means by "the US 100" is the Nasdaq-100. It is emphatically **not the same
# hundred names** — tech-heavy, no financials, no energy — so it is a second line to
# compare against rather than a truer version of the basket.
#
# `us_etfs` deliberately gets nothing: QQQ is a *member* of `ETF_TOP10`, and drawing one
# of the traded names as though it were the yardstick would be a category error.
EXTRA_INDEXES: dict[str, list[str]] = {"us_stocks": ["QQQ"]}


def index_symbols(asset_class: str) -> list[str]:
    """Instruments to plot beside the book: the class's benchmark first, then the extras."""
    out: list[str] = []
    for s in [CLASSES[asset_class].get("benchmark"), *EXTRA_INDEXES.get(asset_class, [])]:
        if s and s not in out:
            out.append(s)
    return out


def index_returns(asset_class: str, timeframe: str, index: pd.DatetimeIndex,
                  fill: str = "close", symbol: str | None = None):
    """Per-bar return of the class's REAL, PURCHASABLE benchmark over the book's bars.

    This answers a different question from `book["bench"]` and both belong on the row.

    `bench` is the self-built equal-weight basket of this sheet's own universe. It is the
    right control for "does the signal add value", because the strategy and the basket
    share a universe, a weighting and a rebalancing schedule, so the only thing separating
    them is the signal. It is NOT purchasable, and it is not close: over 2003-05-01..
    2026-08-07 it compounds 23.1%/yr against SPY's 11.5%, and the gap is survivorship plus
    the Blume-Stambaugh bounce a daily-rebalanced average harvests. Measured on this
    repo's own bars a real equal-weight S&P ETF (RSP, 11.38%/yr) and the cap-weighted one
    (SPY, 11.54%/yr) are the SAME, so none of that excess is the equal-weighting a reader
    would assume it was.

    So a book that beats `bench` has cleared a hard unbuyable yardstick, and a book that
    beats SPY may have beaten nothing but the universe it was handed. Reporting one
    without the other has to mislead somebody.

    Symbol defaults to `config.CLASSES[...]["benchmark"]`, the same place the BETA/CORREL
    plumbing reads it, so the scored columns cannot drift from it. `symbol` overrides that
    for the extra chart-only lines in `EXTRA_INDEXES`; nothing scored passes it.
    """
    sym = symbol or CLASSES[asset_class].get("benchmark")
    if not sym:
        return None, None
    key = (asset_class, timeframe, sym)
    if key not in _INDEX_CACHE:
        got = None
        for src in (asset_class, "us_etfs", "us_stocks"):
            try:
                d = td_loader.load(src, timeframe, symbols=[sym])
            except Exception:
                d = {}
            if d.get(sym) is not None and len(d[sym]) > 10:
                got = d[sym]
                break
        _INDEX_CACHE[key] = got
    df = _INDEX_CACHE[key]
    if df is None:
        return None, None
    # The index is priced on the same leg the book is. Comparing an open-to-open book
    # against a close-to-close index would put the bounce back on one side only, which is
    # the exact confound `fill` exists to remove.
    col = "Open" if (fill == "open" and "Open" in df.columns) else "Close"
    c = df[col].astype(float).reindex(index).ffill()
    r = c.pct_change().fillna(0.0).to_numpy()
    if not np.isfinite(r).any() or float(np.nanstd(r)) == 0.0:
        return None, None
    return r, sym


def apply_flatten(pos, df, rule: str, asset_class: str):
    """Force flat on each session's last bar — the ONE copy of the two exemptions.

    `signals.position_for` carries the same two carve-outs and they must not drift, so
    both delegate to `vector.flatten_eod` and both spell the exemptions the same way:

      * the BASELINE is never flattened. Flattening the benchmark turns it into a
        different strategy, and that is exactly what made the old 5-minute "beat" an
        artifact;
      * a class with `flatten_eod: False` — crypto — is never flattened at all. A 24/7
        market has no session to flatten into, so forcing a daily flat would invent an
        exposure gap no trader has.

    The RANDOM_* and ALWAYS_* controls are deliberately NOT exempt. They exist to price
    exactly this handicap — a no-signal always-long book scores IR -0.59 to -0.84 once
    flattened, with no signal involved — so exempting them would hand every real rule a
    cost its own control never pays, and the comparison would read as failed signal.

    Idempotent: flattening an already-flattened series is the same series, so it is safe
    on a position that arrived through `signals.position_for`.
    """
    if rule == BASELINE or not CLASSES[asset_class]["flatten_eod"]:
        return pos
    return vector.flatten_eod(pos, df.index)


def build_book(data: dict, rule: str, fee: dict, free: dict, intervals,
               cash_override: float | None = None, resolve=None,
               fill: str = "close", bench_fee: dict | None = None,
               stress_frac: float = 0.0, stress_intervals=None,
               flatten_eod: bool = False, asset_class: str = "") -> dict | None:
    """Equal-weight the rule across whoever is investable on each bar.

    Weights are recomputed every bar from the live membership, so a name entering the
    index dilutes the others rather than being bolted on at a fixed share. That is what a
    tracking book does and it is also the only version that has a defined return on the
    days the roster changes.

    ``fill`` decides WHICH PRICE THE MONEY IS EARNED ON, and it is the control for
    Blume & Stambaugh (1983): returns computed from closing prices are biased upward
    because a close is a transaction print that lands at the bid or the ask, and the
    arithmetic of a daily-rebalanced portfolio harvests that bounce as though it were
    return. Their measured gap is the whole size effect, halved.

    That bias is not neutral here. ``ibs`` is ``(C-L)/(H-L)`` — it buys when the close
    sits at the LOW of the bar and sells when it sits at the HIGH, so a close near the
    low is disproportionately a bid print and a close near the high an ask print. The
    rule therefore books its entries at the bid and its exits at the ask, which is the
    reverse of what anyone gets filled at. No fee model can see this: the bias lives in
    the price series, not in the cost applied to it.

      * ``close`` — signal at the close of bar t, money earned close(t) -> close(t+1).
        The published convention; unchanged.
      * ``open``  — signal at the close of bar t, filled at the open of bar t+1, money
        earned open(t+1) -> open(t+2). One extra bar of lag, and the entry price is no
        longer the same print the signal was computed from.
      * ``close_lag`` — the CONTROL for ``open``. Same extra bar of lag, but still priced
        on closes. ``open`` changes two things at once, so on its own it cannot say
        whether a collapse is the bounce or simply a signal that decays within a day.
        Read the three together: if ``close_lag`` holds up and ``open`` does not, the
        difference is the price the fill happened at, which is the bounce.

    The benchmark switches with it, so the comparison stays a controlled experiment: if
    the rule's edge is bounce, the rule falls and a long-only baseline barely moves.

    ``bench_fee`` charges the baseline. It defaults to ``free``, which is what every
    published number used and is defensible for a position that is bought once and held —
    but this book rebalances its weights every bar, so at book level "never charged" is a
    real subsidy to the baseline. Passing the strategy's own scenario makes the two sides
    pay the same schedule.

    ``cash_override`` is the annual rate idle capital earns. **The dashboard's books are
    built with 0** (``run_book.sh`` passes ``--cash-rate 0``): a part-time rule is credited
    with nothing for the bars it sits out. That is a modelling choice and it is not free —
    bills really did pay ~1.8% a year over 2003-2026, so a rule invested half the time
    gives up roughly 0.9%/yr it would actually have collected, and its reported CAGR is
    that much below what the account would have made. It applies to **both sides**: the
    cash-matched benchmark blend and the constant-weight control hold their idle share at
    0% too, so nothing is compared against a version of itself on a different cash
    convention. What it removes is a return that is not the signal's — over 1970-2026 the
    same series pays 15% in 1981, and an edge that leans on that is not an edge available
    now. ``None`` restores the historical path.
    """
    bench_fee = free if bench_fee is None else bench_fee
    S, B, RFm, MASK, POS, BPY = {}, {}, {}, {}, {}, []
    G: dict = {}                     # the same books at zero cost, for headroom
    TRADES = {"wins": 0, "n": 0, "gw": 0.0, "gl": 0.0}
    SPOS: dict = {}
    for symbol, df in data.items():
        if len(df) < MIN_BARS:
            continue
        close = df["Close"].to_numpy("float64")
        bpy = vector.bars_per_year(df.index)
        # `resolve` is threaded through so a combo's TA-Lib legs can be built; without it
        # every `A~B|op` row would be skipped here rather than scored.
        pos = build(rule, df, close, bpy, symbol, resolve=resolve)
        if pos is None and resolve is not None:
            # `build` only consults the resolver for combo LEGS, so a bare TA-Lib single
            # like `BOP` fell through it and was skipped — silently, as always. The 231
            # singles are the bulk of the leaderboard, so a portfolio stage that scores
            # only the published catalog is answering a much smaller question than the
            # one the dashboard is asking.
            pos = resolve(rule, df, close, bpy, symbol)
        if pos is None:
            continue
        # END-OF-DAY FLATTENING, and the reason it is a FLAG rather than a config lookup.
        #
        # `signals.position_for` flattens intraday rules from `FLATTEN_EOD_TIMEFRAMES`,
        # but `registry.build` — the path every PUBLISHED strategy and every control
        # takes — does not, so a sheet mixing the two would flatten half its rows and
        # not the other half. On 1d and 4h that is invisible because neither is in the
        # set; on the 1m/2m/3m sheets it decides the answer.
        #
        # Neither convention is free, and this repo does not get to pick quietly:
        #   * NOT flattening lets a "day-trading" rule collect the overnight drift that
        #     is 65-95% of US equity return, which is not what it claims to do — but it
        #     IS the faithful reading of a Pine `strategy()`, which holds through the
        #     close unless coded otherwise;
        #   * flattening charges a handicap the author never took: a no-signal
        #     always-long control scores IR -0.59 to -0.84 once flattened, with no
        #     signal involved at all.
        # So it is a BOUND, reported both ways, exactly as `--fill` is. Default off,
        # which is the faithful reading and what the published intraday sheets used.
        #
        # The baseline is exempt — flattening the benchmark makes it a different
        # strategy, which is what made the old 5-minute "beat" an artifact — and crypto
        # is exempt by class, having no session to flatten into. `apply_flatten` owns
        # both exemptions so they cannot drift from `signals.position_for`'s copy.
        if flatten_eod:
            pos = apply_flatten(pos, df, rule, asset_class)
        rf = rm.bar_rates(df.index, bpy, cash_override)
        # The signal is ALWAYS built on closes — that is what the rule is. Only the price
        # the resulting position is paid on moves, and with it one extra bar of lag, so
        # the position cannot be earning a return over a bar whose open it was filled at.
        # `riskmatch_wf.apply_fill` is THE definition, imported rather than reimplemented:
        # this repo has already been burned by two functions with the same name and
        # different behaviour in two files, and a fill convention that differs between the
        # verdict stage and the book stage would be exactly that failure again.
        epos, px = rm.apply_fill(pos, df, close, fill)
        if epos is None:
            continue
        POS[symbol] = pd.Series(np.abs(epos), index=df.index)
        SPOS[symbol] = pd.Series(epos, index=df.index)
        w, t, gw, gl = trade_stats(epos, S_sym := rm.levered_net(epos, px, fee, bpy, rf, 1.0))
        TRADES["wins"] += w; TRADES["n"] += t
        TRADES["gw"] += gw; TRADES["gl"] += gl
        S[symbol] = pd.Series(S_sym, index=df.index)
        # The same position at ZERO cost, kept so cost headroom can be priced without
        # rebuilding the book once per fee multiple. `vector.net_returns` charges
        # `turnover x per_side + sells x sell_fee + short x borrow/bpy` — every term
        # linear in its rate — so the drag at k times the schedule is exactly k times the
        # drag at 1x, and `gross - k*(gross - net)` is the book at that schedule rather
        # than an approximation of it. Aggregation is a weighted sum, which preserves it.
        G[symbol] = pd.Series(rm.levered_net(epos, px, free, bpy, rf, 1.0), index=df.index)
        B[symbol] = pd.Series(vector.net_returns(np.ones(len(df)), px, bench_fee, bpy),
                              index=df.index)
        RFm[symbol] = pd.Series(rf, index=df.index)
        MASK[symbol] = pd.Series(membership_mask(df.index, symbol, intervals), index=df.index)
        BPY.append(bpy)
    if len(S) < 2:
        return None

    # Build against ONE precomputed union index instead of letting pandas derive it
    # separately for each of the six frames.
    #
    # `pd.DataFrame(dict_of_series)` unions 722 DatetimeIndexes per frame, and pandas
    # runs `infer_freq` on every union — 4,219 calls per rule, 4.1s of a 9.3s book, 44%
    # of the panel's runtime spent guessing a calendar frequency nobody asked for
    # (`_get_wom_rule`, "week of month", was 2.1s of it alone). The union is identical
    # for all six frames and for every rule in the panel, so it is computed once and
    # reused.
    union_idx = _union_index(tuple(sorted(S)), data)
    Sd = _frame(S, union_idx)
    Bd = _frame(B, union_idx)
    Rd = _frame(RFm, union_idx)
    Md = _frame(MASK, union_idx).fillna(False).astype(bool)
    # A bar counts for a name only if it is both priced and a member.
    live = Md & Sd.notna() & Bd.notna()
    n_live = live.sum(axis=1)
    keep = n_live >= 2
    if keep.sum() < MIN_BARS:
        return None

    w = live.where(keep).astype("float64")
    w = w.div(n_live.where(n_live > 0), axis=0)
    ps = (Sd.fillna(0.0) * w).sum(axis=1)[keep]
    pb = (Bd.fillna(0.0) * w).sum(axis=1)[keep]
    prf = (Rd.fillna(0.0) * w).sum(axis=1)[keep]
    # Gross exposure of the BOOK, not "fraction of non-zero returns" — at portfolio level
    # the return series is non-zero on essentially every bar (cash accrues, and some name
    # is always moving), so the per-symbol definition silently reads 1.00 for everything
    # and hides the one variable this repo insists on checking first.
    exposure = (_frame(POS, union_idx).reindex(columns=Sd.columns).fillna(0.0)
                * w).sum(axis=1)[keep]
    out = {"strat": ps, "bench": pb, "rf": prf, "bpy": float(np.median(BPY)),
           # Aggregated with the identical weights, so `gross - strat` is this book's
           # own cost drag and nothing else.
           "gross": (_frame(G, union_idx).reindex(columns=Sd.columns).fillna(0.0)
                     * w).sum(axis=1)[keep],
           "exposure": exposure, "trades": TRADES,
           "signed": (_frame(SPOS, union_idx).reindex(columns=Sd.columns)
                      .fillna(0.0) * w).sum(axis=1)[keep],
           "n_names": int(live.any().sum()), "avg_breadth": float(n_live[keep].mean())}
    # The stress pool comes from the S&P table, never from `intervals`. Every name in a
    # top-100 table earned its slot by being RANKED on bars, so a table built that way
    # cannot contain a name with no bars — which is precisely the pool the stress needs.
    stress_iv = stress_intervals if stress_intervals is not None else intervals
    if stress_frac and stress_iv is not None:
        out.update(_stress_delisted(stress_iv, set(S), union_idx, keep, n_live,
                                    ps, pb, exposure, stress_frac,
                                    n_slots=int(np.median(n_live[keep]))))
    return out


STRESS_SEED = 20260812
STRESS_FAIL_BARS = 252


def _stress_delisted(intervals, priced: set, union_idx, keep, n_live,
                     ps, pb, exposure, frac: float, n_slots: int | None = None) -> dict:
    """Put the names the vendor cannot price back into the book, and fail some of them.

    120 of the 858 S&P members in `sp500_membership.csv` have no parquet at all — Twelve
    Data serves no delisted equities — so the book silently holds only the 738 that
    survived to be priceable. This is the one bias in the stack that **does not cancel**
    between the strategy and its benchmark, and the asymmetry runs against a dip-buyer:
    the basket loses a name that fell, while `ibs` loses a name it would have bought all
    the way down. A sample containing no failures is exactly where buying the dip is
    safest, and this book has never once been asked to buy a dip that did not recover.

    The model, deliberately crude, because the honest output is a BOUND and not an
    estimate:

      * a missing name is held at 1/(n_live + n_missing) on every bar it was a member,
        which is what dilutes the surviving names — that alone is a real effect even with
        no failures at all;
      * on `frac` of them, chosen by a fixed seed, the final `STRESS_FAIL_BARS` bars of
        membership compound to -100%;
      * elsewhere a missing name earns the basket's own return, i.e. it behaves like an
        average member. Acquisitions are the majority of these removals and a shareholder
        was paid for those, so treating them as average is right and treating them all as
        failures would be theatre.

    The strategy holds a failing name at the book's own exposure that bar. That is the
    NEUTRAL assumption and it understates the damage: a mean-reversion rule buys more of
    something the further it falls, so the true `ibs` exposure to a name on its way to
    zero is above its average, not at it. Read the result as a floor.
    """
    miss = sorted(set(intervals["symbol"].astype(str)) - priced)
    if not miss:
        return {}
    # Only a name that actually LEFT the index is eligible to have failed. A member with
    # an open-ended spell is still in the S&P 500 today and demonstrably did not go to
    # zero, so failing it would not be conservatism, it would be fiction — and an
    # open-ended spell's "last 252 bars" land in 2026, putting an invented wipeout in the
    # most recent window on the sheet.
    departed = set(intervals.loc[intervals["end"].notna(), "symbol"].astype(str))
    pool = sorted(set(miss) & departed)
    rng = np.random.default_rng(STRESS_SEED)

    # ADMISSION, and it is new with the top-100 universe. The pool above is every
    # unpriceable name that ever left the S&P 500 — ~120 of them. Restoring all 120 to a
    # book that holds ~100 names at a time would put the majority of the roster on names
    # nobody can see, drop every real name's weight to 1/220, and produce a "bound" that
    # is mostly an artifact of the restoration. On the 500-name universe this did not
    # bite, because 120 against ~500 live names is a fifth rather than a majority.
    #
    # An unpriceable name cannot be ranked, so whether it would have been top-100 is
    # genuinely unknowable. Admit it with probability `n_slots / eligible`, i.e. treat it
    # as a RANDOM member of the index — the same neutrality this function already applies
    # to a missing name's returns, applied one level up to its membership.
    #
    # **This is neutral, not conservative, and the difference matters here.** Large caps
    # are over-represented among the spectacular failures: Lehman, Bear Stearns, Worldcom,
    # Enron, AIG and GM were all top-100 by dollar volume before they went. Treating them
    # as random members therefore UNDERSTATES how much of this universe's survivorship is
    # missing. Read the result as a floor on a floor.
    n_eligible = intervals["symbol"].nunique()
    if n_slots and n_eligible and n_slots < n_eligible:
        share = float(n_slots) / float(n_eligible)
        n_admit = int(round(len(pool) * share))
        admitted = set(rng.choice(np.array(pool, dtype=object), size=n_admit,
                                  replace=False)) if n_admit else set()
        miss = [m for m in miss if m in admitted]
        pool = sorted(admitted)
        if not miss:
            return {}

    n_fail = int(round(len(pool) * float(frac)))
    failing = set(rng.choice(np.array(pool, dtype=object), size=n_fail, replace=False)
                  ) if n_fail else set()

    # A missing name that did NOT fail must be EXACTLY NEUTRAL for both sides, or the
    # stress stops measuring survivorship and starts measuring the modelling choice. So
    # the benchmark earns its own basket return on it and the strategy earns its own book
    # return on it — each side assumed to do on an unseen name what it demonstrably does
    # on the seen ones. The first version scaled the strategy's leg by exposure while
    # leaving the benchmark's at 1.0, which handed `ibs` a -2.1pp/yr penalty on 24% of the
    # roster before a single company had gone bankrupt, and that is not survivorship.
    normal_b = pb.reindex(union_idx).fillna(0.0).to_numpy()
    normal_s = ps.reindex(union_idx).fillna(0.0).to_numpy()
    expo = exposure.reindex(union_idx).fillna(0.0).to_numpy()
    wipe = 0.001 ** (1.0 / STRESS_FAIL_BARS) - 1.0                 # -99.9% over the window

    n_miss = np.zeros(len(union_idx))
    add_b = np.zeros(len(union_idx))
    add_s = np.zeros(len(union_idx))
    for sym in miss:
        m = membership_mask(union_idx, sym, intervals)
        if not m.any():
            continue
        rb = np.where(m, normal_b, 0.0)
        rs = np.where(m, normal_s, 0.0)
        if sym in failing:
            at = np.flatnonzero(m)[-STRESS_FAIL_BARS:]
            rb[at] = wipe
            # The rule is only in the market `expo` of the time, so it eats that share of
            # the collapse. Understates it: a dip-buyer is MORE invested the further a
            # name falls, not averagely invested.
            rs[at] = wipe * expo[at]
        n_miss += m
        add_b += rb
        add_s += rs
    # Re-weight. The book currently divides by `n_live`; with the missing names restored
    # it divides by `n_live + n_miss`, so every surviving name's share shrinks by exactly
    # the fraction of the roster that was invisible. `ps * n_live` and `pb * n_live`
    # recover the unweighted sums the book was built from.
    nl = n_live.to_numpy().astype("float64")
    psf = ps.reindex(union_idx).fillna(0.0).to_numpy()
    pbf = pb.reindex(union_idx).fillna(0.0).to_numpy()
    n_tot = nl + n_miss
    safe = np.where(n_tot > 0, n_tot, 1.0)
    sb = (nl * pbf + add_b) / safe
    ss = (nl * psf + add_s) / safe
    return {"strat_stress": pd.Series(ss, index=union_idx)[keep],
            "bench_stress": pd.Series(sb, index=union_idx)[keep],
            "stress_n_missing": len(miss), "stress_n_departed": len(pool),
            "stress_n_failing": n_fail,
            "stress_share": float(np.nanmean((n_miss / safe)[keep.to_numpy()]))}


def fold_edges(ps: np.ndarray, pb: np.ndarray, prf: np.ndarray, bpy: float,
               index: pd.DatetimeIndex, folds) -> dict:
    """Delta-Sharpe fold by fold, and the t across those folds.

    This is criterion S and criterion T **as `config.EDGE_STANDARD` defines them** — "mean
    of per-fold values" and "t on the per-fold delta-Sharpe, across FOLDS" — applied to the
    book instead of to a single name. `riskmatch_wf` computes the same two quantities per
    asset and then takes a median across names; here the book is already the aggregate, so
    the per-fold values ARE the sheet's values and there is no median to take.

    Why not the block bootstrap sitting next to it in `boot_t`: it is a looser test, and
    the difference decides rows. `ibs` on us_stocks 1d bootstraps to t = 3.87 against a
    multiplicity-corrected bar of 3.84 — a pass by 0.03 — because a bootstrap over 5,936
    bars treats a long autocorrelated history as far more evidence than 54 quarterly
    out-of-sample windows do. The threshold in `config` was calibrated on the fold-to-fold
    sd (0.258 over 54 folds on this very sheet), so a bootstrap t scored against it is not
    the test that was validated. `boot_t` stays on the row as a second opinion; the
    standard reads this one.

    A fold contributes only if both series have enough bars in it to have a Sharpe at all;
    `n_folds_scored` counts those, and `fold_coverage` is that against the sheet's total,
    so a rule that could only be measured on half the calendar is caught by the same
    rankability check that catches it per asset.
    """
    edges = []
    for f in folds:
        # Half-open [is_end, oos_end), matching `walkforward.fold_masks` exactly — a fold
        # boundary counted twice would put one bar in two folds and correlate them.
        m = (index >= f.is_end) & (index < f.oos_end)
        n = int(m.sum())
        if n < 60:
            continue
        a = stats.sharpe(ps[m], prf[m], bpy, min_obs=30)
        b = stats.sharpe(pb[m], prf[m], bpy, min_obs=30)
        if np.isfinite(a) and np.isfinite(b):
            edges.append(a - b)
    e = np.asarray(edges, dtype="float64")
    n_f = e.size
    sd = float(e.std(ddof=1)) if n_f > 2 else float("nan")
    mean = float(e.mean()) if n_f else float("nan")
    return {
        "fold_dsharpe": mean,
        "fold_sd": sd,
        "fold_t": (mean / (sd / np.sqrt(n_f))
                   if n_f > 2 and np.isfinite(sd) and sd > 0 else float("nan")),
        "n_folds_scored": n_f,
        # The series itself, because the panel's significance bar is computed FROM it —
        # see `_t_bar`. Stored the way `riskmatch_wf` stores its own: one string, so a
        # variable-length vector survives a CSV round trip.
        "fold_edges": ";".join(f"{v:.6f}" for v in e),
    }


def score(book: dict, rule: str, trial_sharpes, fac: pd.DataFrame | None) -> dict:
    ps = book["strat"].to_numpy()
    pb = book["bench"].to_numpy()
    prf = book["rf"].to_numpy()
    bpy = book["bpy"]
    idx = book["strat"].index

    s_sh, b_sh = _sharpe(ps, prf, bpy), _sharpe(pb, prf, bpy)
    s_vol, b_vol = float(ps.std(ddof=1) * np.sqrt(bpy)), float(pb.std(ddof=1) * np.sqrt(bpy))
    eq_s, eq_b = float(np.prod(1 + ps)), float(np.prod(1 + pb))
    years = len(ps) / bpy

    row = {
        "rule": rule, "n_names": book["n_names"], "avg_breadth": book["avg_breadth"],
        "bars": len(ps), "years": years,
        "start": idx.min().date(), "end": idx.max().date(),
        "sharpe": s_sh, "bench_sharpe": b_sh, "dsharpe": s_sh - b_sh,
        "vol": s_vol, "bench_vol": b_vol,
        "dd": _max_dd(ps), "bench_dd": _max_dd(pb),
        "cagr": eq_s ** (bpy / len(ps)) - 1.0, "bench_cagr": eq_b ** (bpy / len(pb)) - 1.0,
        "wealth": CAPITAL * eq_s, "bench_wealth": CAPITAL * eq_b,
        "exposure": float(book["exposure"].mean()),
        **_trade_columns(book.get("trades")),
    }
    if rule == BASELINE:
        # The baseline against the benchmark is the same trades twice, so the bootstrap
        # SE collapses to ~0 and the t-stat is whatever rounding survives — on the mega20
        # PIT book that produced boot_t +2.04 off a ~1e-5 Sharpe difference, which reads
        # as significance and is nothing of the kind. (The residual is the risk-free
        # credit `levered_net` pays on each symbol's first bar, before its position is
        # entered: 9 bars in 12,626, +0.021% over 50 years.) Report the descriptive
        # columns for this row and refuse the inferential ones.
        row.update({"dsharpe": row["dsharpe"], "boot_se": float("nan"),
                    "boot_t": float("nan"), "boot_p_gt0": float("nan"),
                    "boot_ci_lo": float("nan"), "boot_ci_hi": float("nan")})
    else:
        row.update(block_bootstrap_dsharpe(ps, pb, prf, bpy))

    # Criteria S and T, on the sheet's own walk-forward calendar. Attached here rather
    # than in `_standard` because only this function holds the series.
    if book.get("folds"):
        fe = fold_edges(ps, pb, prf, bpy, idx, book["folds"])
        if rule == BASELINE:
            # Same refusal as the bootstrap above, for the same reason: the baseline
            # against the benchmark is the same trades twice, so the per-fold differences
            # are rounding — 1e-5 with an sd of 1e-5, which is t = -4.75 and reads as a
            # significant result about nothing.
            fe = {**fe, "fold_dsharpe": float("nan"), "fold_sd": float("nan"),
                  "fold_t": float("nan")}
        row.update(fe)

    # ---- the headline money number: matched risk with NO LEVERAGE ANYWHERE.
    #
    # Rather than levering the rule UP to buy-and-hold's volatility, hold buy-and-hold
    # blended with T-bills so the BENCHMARK comes DOWN to the rule's. `w <= 1` always, so
    # this is a cash blend and never margin, and it is strictly better than the levered
    # version on three counts:
    #
    #   * no financing spread and no borrow — the levered comparison pays both;
    #   * no Reg-T cap, which was silently doing part of the ranking, since a rule at 17%
    #     exposure cannot be levered to 2x to match anything;
    #   * anyone can run it. "Hold 56% SPY and 44% T-bills" needs no margin account.
    #
    # The controls confirm it measures skill and not deployment: on us_stocks 1d, ALWAYS
    # -LONG and buy-and-hold score +0.00%/yr, RANDOM_50 +0.07% and RANDOM_90 +0.20%,
    # while `ibs` scores +4.40%/yr AND carries a shallower drawdown than the blend it is
    # measured against.
    w = min(s_vol / b_vol, 1.0) if b_vol > 0 else np.nan
    if np.isfinite(w):
        blend = w * pb + (1.0 - w) * prf
        eq_blend = float(np.prod(1.0 + blend))
        row.update({
            "cash_w": w,
            "cashmatch_bench_cagr": eq_blend ** (bpy / len(blend)) - 1.0,
            "cashmatch_bench_dd": _max_dd(blend),
            "cashmatch_bench_wealth": CAPITAL * eq_blend,
            "cashmatch_excess_cagr": row["cagr"] - (eq_blend ** (bpy / len(blend)) - 1.0),
            "cashmatch_ratio": eq_s / eq_blend if eq_blend > 0 else np.nan,
            "cashmatch_beats": bool(eq_s > eq_blend),
            # Criterion W at book level: the money the account made ABOVE the same basket
            # held at its own volatility. The per-asset stage calls this `edge_wealth`
            # and takes the median of it across names; here there is one account, so
            # there is one number and no median to take.
            "edge_wealth": CAPITAL * (eq_s - eq_blend),
        })

        # ---- criterion H at book level: multiples of the fee schedule the advantage
        # survives.
        #
        # The per-asset stage re-runs the backtest once per multiple. That is unnecessary:
        # every term in `vector.net_returns`' cost is linear in its own rate, so the book
        # at k times the schedule is `gross - k*(gross - net)` exactly — one subtraction
        # per multiple instead of one backtest. `build_book` carries the zero-cost book
        # for this and nothing else.
        #
        # The comparison, the ladder and the break-on-first-failure are `riskmatch_wf`'s,
        # so the two numbers mean the same thing: the equal-risk wealth advantage, not IR
        # (`metrics.cost_headroom` reports 0.00x whenever gross IR <= 0, which reads as a
        # measurement and means undefined). The blend is held at its 1x weight, matching
        # the per-asset stage holding `k_causal` fixed — fees erode the rule, they do not
        # move the yardstick.
        gross = book.get("gross")
        if gross is not None and len(gross) == len(ps):
            drag = np.asarray(gross, dtype="float64") - ps
            head = 0.0
            for mult in (0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0):
                if float(np.prod(1.0 + (gross - mult * drag))) > eq_blend:
                    head = mult
                else:
                    break
            row["edge_headroom"] = head
            row["cost_drag_ann"] = float(np.mean(drag)) * bpy

    # ---- what the capital earned WHILE IT WAS AT WORK, at book level.
    #
    # `cagr` above is the account: idle capital sits in T-bills and drags the annual rate
    # down in exact proportion to the time it was idle. On a sheet where exposure and
    # return correlate at 0.881 that makes an account-level ranking substantially a
    # ranking of who stayed invested longest, which is the whole reason this repo reports
    # both.
    #
    # Two adjustments, and both are needed or the number flatters:
    #   * the interest earned on the IDLE fraction is removed, so this is what the
    #     invested part did rather than what the invested part plus a bill ladder did;
    #   * it is annualised over DEPLOYED years — calendar years x mean exposure — not
    #     calendar years.
    # At full exposure both vanish and `roe_ann` collapses to `cagr`, which is the right
    # behaviour: buy-and-hold is never idle, so it has one number, not two.
    expo = float(book["exposure"].mean())
    if expo > 0:
        deployed = ps - (1.0 - book["exposure"].to_numpy()) * prf
        eq_d = float(np.prod(1.0 + deployed))
        dep_years = len(ps) * expo / bpy
        row["roe_ann"] = eq_d ** (1.0 / dep_years) - 1.0 if dep_years > 0 else np.nan
    else:
        row["roe_ann"] = np.nan

    # ---- criterion C at book level: beats simply owning LESS, with no signal at all.
    #
    # The per-asset stage asks this of each name; this asks it of the account. Hold the
    # same passive basket at the rule's own average weight and the rest in bills — no
    # timing, no signal, the same average market exposure — and compare return per unit of
    # drawdown (MAR). A rule whose timing is worth nothing scores ~0 here, and one that is
    # merely invested less than the market cannot win by being cautious.
    #
    # Weight is mean GROSS exposure, matching `exposure` on the row. On a long/short book
    # that overstates the control's market exposure, which makes this conservative rather
    # than wrong — `vs_random` carries the load on that side, as it does per asset.
    if np.isfinite(expo) and expo > 0:
        const = expo * pb + (1.0 - expo) * prf
        c_dd, s_dd = _max_dd(const), row["dd"]
        c_cagr = float(np.prod(1.0 + const)) ** (bpy / len(const)) - 1.0
        c_mar = c_cagr / abs(c_dd) if c_dd else np.nan
        s_mar = row["cagr"] / abs(s_dd) if s_dd else np.nan
        row.update({"const_weight": expo, "const_mar": c_mar, "mar": s_mar,
                    "vs_constant": s_mar - c_mar})

    # ---- the same money question against something a person can actually buy.
    #
    # `cashmatch_*` above is measured against the self-built basket, which is the correct
    # control for the SIGNAL and is not an investable alternative. `idx_*` repeats the
    # identical construction against the class's real index ETF. The two disagree by a
    # lot on us_stocks and the disagreement is the point: it is the size of the universe
    # subsidy every absolute figure on this sheet is carrying.
    ixr = book.get("index_ret")
    if ixr is not None and len(ixr) == len(ps):
        ixr = np.asarray(ixr, dtype="float64")
        i_vol = float(ixr.std(ddof=1) * np.sqrt(bpy))
        eq_i = float(np.prod(1.0 + ixr))
        row.update({
            "index_symbol": book.get("index_symbol"),
            "index_cagr": eq_i ** (bpy / len(ixr)) - 1.0,
            "index_wealth": CAPITAL * eq_i,
            "index_sharpe": _sharpe(ixr, prf, bpy),
            "index_vol": i_vol,
            "index_dd": _max_dd(ixr),
        })
        wi = min(s_vol / i_vol, 1.0) if i_vol > 0 else np.nan
        if np.isfinite(wi):
            iblend = wi * ixr + (1.0 - wi) * prf
            eq_ib = float(np.prod(1.0 + iblend))
            row.update({
                "idx_cash_w": wi,
                "idx_cashmatch_bench_cagr": eq_ib ** (bpy / len(iblend)) - 1.0,
                "idx_cashmatch_bench_dd": _max_dd(iblend),
                "idx_cashmatch_excess_cagr": row["cagr"] - (eq_ib ** (bpy / len(iblend)) - 1.0),
                "idx_cashmatch_ratio": eq_s / eq_ib if eq_ib > 0 else np.nan,
                "idx_cashmatch_beats": bool(eq_s > eq_ib),
            })

    # Vol-matched wealth, kept for continuity and now UNLEVERED: `rm.MAX_LEVERAGE` is 1.0
    # while `rm.LEVERAGE_ENABLED` is False, so `k` can only shrink a position, never borrow
    # to enlarge one. The headline money column on this sheet is `cashmatch_*`, which never
    # borrowed anything — it holds `w` in buy-and-hold and `1-w` in T-bills and matches risk
    # by scaling the BENCHMARK down. That construction is unaffected by any of this.
    k = b_vol / s_vol if s_vol > 0 else np.nan
    if np.isfinite(k):
        k = min(k, rm.MAX_LEVERAGE)
        spread_bar = (1.0 + FINANCING_SPREAD) ** (1.0 / bpy) - 1.0
        lev = ps * k - max(k - 1.0, 0.0) * (prf + spread_bar)
        row.update({"volmatch_k": k, "volmatch_wealth": CAPITAL * float(np.prod(1 + lev)),
                    "volmatch_dd": _max_dd(lev),
                    "volmatch_beats": bool(np.prod(1 + lev) > eq_b)})

    if trial_sharpes is not None:
        d = metrics.dsr_from_leaderboard(ps - prf, trial_sharpes, bpy=bpy)
        row.update({k2: d.get(k2) for k2 in
                    ("skew", "kurtosis", "n_trials", "sr_star_ann", "psr",
                     "dsr", "dsr_pass")})
    else:
        # Deflated below, once the panel's own trial set exists.
        row["_excess"] = ps - prf
        row["_bpy"] = bpy

    if fac is not None:
        # Regress the strategy's excess-of-cash return, daily-aligned to the factor file.
        y = pd.Series(ps - prf, index=idx).rename("y")
        bh = pd.Series(pb - prf, index=idx).rename("BH")
        y.index, bh.index = y.index.normalize(), bh.index.normalize()
        y = y[~y.index.duplicated(keep="first")]
        bh = bh[~bh.index.duplicated(keep="first")]

        res = ff.regress(y, fac, bpy=bpy)
        if "alpha_ann" in res:
            row.update({"alpha_ann": res["alpha_ann"], "alpha_t": res["alpha_t"],
                        "alpha_p": res["alpha_p"], "r2": res["r2"],
                        "factor_n": res["n"]})
            row.update({f"beta_{nm}": v for nm, v in res["loadings"].items()})
            row.update({f"t_{nm}": v for nm, v in res["loading_t"].items()})

        # ...and again with this class's OWN passive book as an extra factor. This is the
        # spec that answers the question actually being asked, because alpha against the
        # market prices the *names* as well as the timing. On the mega20 the passive book
        # itself scores alpha +7.3%/yr at t=6.70 against Fama-French — that is the
        # survivorship premium of holding twenty companies selected for being large
        # today, and a timing rule trading those same names inherits every basis point of
        # it for free. `alpha_vs_bh` is what the rule adds on top of simply holding them,
        # and it is the only one of the two that can be called skill.
        fac2 = fac.join(bh, how="inner")
        res2 = ff.regress(y, fac2, names=list(res.get("names", [])) + ["BH"], bpy=bpy)
        if "alpha_ann" in res2:
            row.update({"alpha_vs_bh_ann": res2["alpha_ann"],
                        "alpha_vs_bh_t": res2["alpha_t"],
                        "alpha_vs_bh_p": res2["alpha_p"],
                        "beta_BH": res2["loadings"].get("BH"),
                        "r2_vs_bh": res2["r2"]})
    return row




# ---------------------------------------------------------------- the drawable book
#
# The dashboard's equity chart used to come from `curves.py`, which built its own
# equal-weight portfolio from `signals` and `engines.vector`. That was a SECOND book, and
# it disagreed with this one — visibly, on the same page:
#
#   `ibs`, us_stocks 1d   this stage   curves.py
#   $10k became           $308,442     $270,661
#   CAGR                    15.65%       15.01%
#   Sharpe                   1.108        1.208
#
# Three conventions, none of them cosmetic. Idle cash earns the T-bill path here
# (`levered_net` takes `rf`) and earned zero there, which on a rule invested 47% of the
# time is most of the money gap. This book holds a name only while it was a MEMBER
# (`membership_mask`, `--pit`); that one held it on every bar it had a price for. And
# Sharpe here is excess of the bill rate, there it was raw — which is why the weaker book
# carried the HIGHER Sharpe, an inversion nobody could have reasoned their way out of
# from the page.
#
# So the chart is now drawn from the same series this stage scores. Not "reconciled with"
# — the same array. A number on the chart and the same number on the leaderboard row are
# now the same computation by construction, and the only way they can drift is if this
# file is edited to make them.

CURVE_POINTS = 320


def curve_points(ret: np.ndarray, index: pd.DatetimeIndex,
                 points: int = CURVE_POINTS) -> tuple[list, list]:
    """Cumulative growth of 100, downsampled by stride.

    Strided rather than resampled so the first and last points are real observations —
    the endpoint has to equal the total return quoted beside it, or the chart and the
    table disagree and nobody knows which to believe.
    """
    r = np.nan_to_num(np.asarray(ret, dtype="float64"), nan=0.0)
    eq = 100.0 * np.cumprod(1.0 + r)
    n = eq.size
    if n == 0:
        return [], []
    step = max(1, n // points)
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return ([round(float(eq[i]), 2) for i in idx],
            [index[i].strftime("%Y-%m-%d") for i in idx])


def _sortino(r: np.ndarray, rf: np.ndarray, bpy: float) -> float:
    """Sharpe counting only downside deviation, on the same excess series as `_sharpe`.

    Excess of the bill rate, not raw, because it sits in a table beside a Sharpe that is
    excess of the bill rate. Two ratios over the same numerator that disagree about what
    the numerator is would be the exact confusion this whole change exists to remove.
    """
    x = np.asarray(r, dtype="float64") - np.asarray(rf, dtype="float64")
    x = x[np.isfinite(x)]
    down = x[x < 0]
    if x.size < 2 or down.size < 2:
        return float("nan")
    sd = float(down.std(ddof=1))
    return float(x.mean() / sd * np.sqrt(bpy)) if sd > 0 else float("nan")


def _pct(v, dp=2):
    """A fraction as a display percentage, or None when it is not a number."""
    return None if v is None or not np.isfinite(v) else round(float(v) * 100.0, dp)


def _num(v, dp=3):
    return None if v is None or not np.isfinite(v) else round(float(v), dp)


def _line_metrics(r: np.ndarray, prf: np.ndarray, bpy: float) -> dict:
    """The display set for one plotted line, from its own return series.

    `_sharpe`, `_max_dd` and the CAGR expression are the ones `score` uses on the very
    same arrays, so the book's row in this dict is identical to the book's row in the CSV
    — verified in `main` rather than assumed, because "identical by inspection" is how the
    two books drifted apart in the first place.
    """
    r = np.asarray(r, dtype="float64")
    if r.size < 2:
        return {}
    eq = float(np.prod(1.0 + r))
    years = r.size / bpy if bpy > 0 else float("nan")
    cagr = eq ** (bpy / r.size) - 1.0 if r.size else float("nan")
    dd = _max_dd(r)
    return {
        "total_pct": _pct(eq - 1.0),
        "cagr_pct": _pct(cagr),
        "sharpe": _num(_sharpe(r, prf, bpy)),
        "sortino": _num(_sortino(r, prf, bpy)),
        "max_dd_pct": _pct(dd, 1),
        "calmar": _num(cagr / abs(dd)) if dd and np.isfinite(dd) and dd < 0 else None,
        "vol_pct": _pct(float(r.std(ddof=1)) * np.sqrt(bpy), 1),
        "bars": int(r.size),
        "years": _num(years, 2),
    }


def curve_payload(book: dict, row: dict, asset_class: str, timeframe: str,
                  fill: str = "close", side: str = "long") -> dict:
    """Everything the detail page draws for one rule, off this book's own series."""
    ps = book["strat"].to_numpy()
    pb = book["bench"].to_numpy()
    prf = book["rf"].to_numpy()
    bpy, index = book["bpy"], book["strat"].index

    eq, dates = curve_points(ps, index)
    beq, _ = curve_points(pb, index)

    indexes = []
    for sym in index_symbols(asset_class):
        ixr, _ = index_returns(asset_class, timeframe, index, fill, symbol=sym)
        if ixr is None or len(ixr) != len(ps):
            continue
        ieq, _ = curve_points(np.asarray(ixr, dtype="float64"), index)
        # `_ret` is carried only as far as the risk-match below, then dropped: the page
        # gets a 331-point curve, never a 5,936-bar series it has no use for.
        indexes.append({"symbol": sym, "curve": ieq, "_ret": ixr,
                        "metrics": _line_metrics(ixr, prf, bpy)})

    # ---- every benchmark scaled DOWN to the book's own volatility.
    #
    # The money columns answer "who ended with more" and the answer is often "whoever took
    # more risk". This answers the other half: hold each benchmark at the weight that gives
    # it the BOOK's volatility, the rest in cash, and compare terminal wealth. Scaled down,
    # never levered up — no margin, no borrow, nothing anyone would need an account upgrade
    # to run.
    #
    # It is computed here for every line the page draws, not just the class benchmark,
    # because the interesting case is the one the leaderboard cannot show: on us_stocks 1d
    # QQQ ends with 26% more money than `ibs` and 35% less once both carry the same risk.
    # A reader looking at the equity chart sees the first fact only.
    s_vol = float(np.std(ps, ddof=1) * np.sqrt(bpy))

    def _matched(r: np.ndarray, label: str) -> dict | None:
        v = float(np.std(r, ddof=1) * np.sqrt(bpy))
        if not (np.isfinite(v) and v > 0 and np.isfinite(s_vol) and s_vol > 0):
            return None
        w = min(s_vol / v, 1.0)
        blend = w * r + (1.0 - w) * prf
        eq = float(np.prod(1.0 + blend))
        bq, _ = curve_points(blend, index)
        return {"label": label, "weight": _num(w, 3),
                # The blended series itself, so the equity chart can draw the comparison
                # rather than describe it underneath. It cannot be reconstructed from what
                # else is published: `w*curve + (1-w)` is wrong — the weighting applies to
                # the RETURNS, bar by bar, before they compound.
                "curve": bq,
                # The full display set on the blend, because the detail page's metrics
                # table sits under that chart and must be on its basis. Note what this
                # does NOT change: Sharpe and Sortino are scale-invariant when idle cash
                # earns nothing, so a matched benchmark keeps the Sharpe it had at full
                # size. Volatility, drawdown, CAGR and terminal wealth all move.
                "metrics": _line_metrics(blend, prf, bpy),
                "wealth": round(CAPITAL * eq),
                "cagr_pct": _pct(eq ** (bpy / len(blend)) - 1.0),
                "max_dd_pct": _pct(_max_dd(blend), 1),
                "raw_wealth": round(CAPITAL * float(np.prod(1.0 + r)))}

    matched = {
        "vol_pct": _pct(s_vol, 1),
        "strategy": {"label": "the strategy", "weight": 1.0,
                     "wealth": round(CAPITAL * float(np.prod(1.0 + ps))),
                     "cagr_pct": _pct(float(np.prod(1.0 + ps)) ** (bpy / len(ps)) - 1.0),
                     "max_dd_pct": _pct(_max_dd(ps), 1)},
        "lines": [x for x in
                  ([_matched(np.asarray(i["_ret"], dtype="float64"), i["symbol"])
                    for i in indexes if "_ret" in i]
                   + [_matched(pb, "this universe, held")]) if x],
    }
    for i in indexes:
        i.pop("_ret", None)

    m = _line_metrics(ps, prf, bpy)
    # Trade statistics are the book's pooled trades, straight off the scored row rather
    # than recounted here — `trade_stats` segments them per symbol on the position that
    # was HELD over each bar, which no portfolio-level return series can reconstruct.
    m.update({
        "trades": None if not row.get("n_trades") else int(row["n_trades"]),
        "win_rate_pct": _pct(row.get("win_rate"), 1),
        "profit_factor": _num(row.get("profit_factor"), 2),
        "avg_win_pct": _pct(row.get("avg_win")),
        "avg_loss_pct": _pct(row.get("avg_loss")),
        "exposure_pct": _pct(row.get("exposure"), 1),
        "turnover_per_year": _num(row.get("n_trades") / row["n_names"] / row["years"], 2)
        if row.get("n_trades") and row.get("years") else None,
    })
    return {
        "curve": eq, "bench": beq, "dates": dates,
        "indexes": indexes,
        "matched": matched,
        "metrics": m,
        "bench_metrics": _line_metrics(pb, prf, bpy),
        # The book's OWN count of names it ever held, which is `n_names` on the row and
        # not the size of the universe: a name that never cleared membership inside the
        # scored window is in neither.
        "n_assets": int(row.get("n_names") or 0),
        "pit": bool(row.get("pit")),
        "side": side,
    }


def _trade_columns(t: dict | None) -> dict:
    """Win rate, profit factor and trade count, pooled across every name in the book."""
    if not t or not t["n"]:
        return {"win_rate": float("nan"), "profit_factor": float("nan"),
                "n_trades": 0, "avg_win": float("nan"), "avg_loss": float("nan")}
    wins, n = t["wins"], t["n"]
    losses = n - wins
    return {
        "win_rate": wins / n,
        # Gross winnings / gross losses. Undefined rather than infinite when a rule never
        # lost a trade — reporting `inf` on a leaderboard sorts it to the top.
        "profit_factor": (t["gw"] / t["gl"]) if t["gl"] > 0 else float("nan"),
        "n_trades": n,
        "avg_win": (t["gw"] / wins) if wins else float("nan"),
        "avg_loss": (-t["gl"] / losses) if losses else float("nan"),
    }


def _vs_random(rows: list[dict]) -> list[dict]:
    """Book Sharpe above a signal-free control invested exactly as often, per sheet.

    The mirror of `riskmatch_wf`'s criterion R, asked of the account instead of the median
    name. Being in the market pays in a rising market whether or not you were right, so
    the control prices that and what is left is what the timing did.

    The controls are already in the panel — `RANDOM_25/50/75/90` are scored as rules by
    the same run, on the same bars, with the same fees — so this is a second pass over
    `rows` rather than new backtesting. Their exposures land near but not on their names
    (a 50% coin flip does not hold exactly 50% of bars), so the curve is built from what
    they MEASURED and read by interpolation at the rule's own exposure, clamped at both
    ends. `np.interp` needs its x ascending, hence the sort.

    Needs at least two controls to interpolate between; a panel run without them (a
    `--rules` shortlist, say) leaves the column absent rather than guessed.
    """
    by_sheet: dict = {}
    for r in rows:
        by_sheet.setdefault((r.get("class"), r.get("tf")), []).append(r)
    for panel in by_sheet.values():
        pts = sorted((r["exposure"], r["sharpe"]) for r in panel
                     if str(r.get("rule", "")).startswith("RANDOM_")
                     and np.isfinite(r.get("exposure", np.nan))
                     and np.isfinite(r.get("sharpe", np.nan)))
        if len(pts) < 2:
            continue
        cx = np.array([p[0] for p in pts], dtype="float64")
        cy = np.array([p[1] for p in pts], dtype="float64")
        for r in panel:
            e, s = r.get("exposure"), r.get("sharpe")
            if e is None or s is None or not np.isfinite(e) or not np.isfinite(s):
                continue
            r["rand_sharpe"] = float(np.interp(e, cx, cy, left=cy[0], right=cy[-1]))
            r["vs_random"] = s - r["rand_sharpe"]
    return rows


# Family-wise error rate the significance bar is set to hold. 0.05 means: if nothing on
# the sheet has an edge, there is a 5% chance ANY row clears the bar — not a 5% chance per
# row, which is the mistake that turns 400 candidates into 20 discoveries.
FWER_ALPHA = 0.05
N_PERMUTATIONS = 20_000
PERM_SEED = 20260813


def _t_bar(rows: list[dict]) -> list[dict]:
    """The t a rule must clear, learned from the search instead of assumed.

    **Why Bonferroni was the wrong bar.** It divides alpha by the number of candidates,
    which is exactly right when those candidates are independent tests and far too strict
    when they are not. This panel is about as far from independent as a search gets: 231
    TA-Lib rules over the same 189 names and the same bars, 140 pairs built from those same
    legs, dozens of near-identical candle patterns. At 405 trials Bonferroni asks for
    t >= 3.84 — the bar for 405 genuinely separate coin flips — when the panel does not
    contain anything like 405 separate coin flips.

    **What replaces it: Westfall & Young's max-T, by sign-flipping.** Under the null a
    rule's per-fold edge is symmetric about zero, so flipping the sign of a fold is a draw
    from the null. The same sign vector is applied to EVERY rule at once, which is the
    whole point: it destroys the edge while preserving the correlation between rules, so
    the null distribution of the *best rule on the sheet* is built from this panel's own
    redundancy rather than from an assumption about it. The bar is the 95th percentile of
    that maximum. It is exact for any correlation structure, needs no model of it, and it
    collapses to Bonferroni's answer if the rules really are independent.

    The family is the CANDIDATES — the rules a search could have returned. Controls
    (`RANDOM_*`, `ALWAYS_*`) and the baseline are excluded: nobody was choosing between
    `ibs` and a coin flip they built on purpose, and padding the family with rows nobody
    would trade makes the bar move for the wrong reason.

    Rules are matched on a common fold count so the permuted t-statistics are comparable;
    that is every rule the sheet scored on its full calendar. `t_bar_maxt` goes on every
    row of the panel, with `t_bar_bonferroni` beside it for comparison.

    **This can move the bar UP.** It is a correction, not a discount. Bonferroni takes a
    normal quantile, and a mean over 21 folds is a t with fatter tails, so on a panel that
    really is independent the honest bar is higher than Bonferroni's — measured at 4.42
    against 3.84 for 387 rules over 21 folds. Whether the redundancy or the tails wins is
    a property of the panel and the point is that it is now measured rather than assumed.
    """
    by_sheet: dict = {}
    for r in rows:
        by_sheet.setdefault((r.get("class"), r.get("tf")), []).append(r)

    for panel in by_sheet.values():
        cand = [r for r in panel
                if not str(r.get("rule", "")).startswith(("RANDOM_", "ALWAYS_"))
                and r.get("rule") != BASELINE and r.get("fold_edges")]
        mats = {}
        for r in cand:
            e = np.array([float(x) for x in str(r["fold_edges"]).split(";") if x],
                         dtype="float64")
            mats.setdefault(e.size, []).append(e)
        if not mats:
            continue
        n_f = max(mats, key=lambda k: len(mats[k]))       # the sheet's full calendar
        # Below the power floor there is no bar worth quoting, and the arithmetic stops
        # meaning anything before it stops producing numbers. At 3 folds a sign flip has
        # only 2^3 = 8 distinct patterns, and the max over 400 rules reliably finds one
        # where the permuted spread collapses and t explodes — crypto 1d measured a
        # "bar" of 50.89 that way. Those sheets are `underpowered` on the fold count
        # regardless, so the verdict does not change; what changes is that the CSV and
        # the tooltip no longer carry a number nobody should read.
        E = np.vstack(mats[n_f]) if n_f >= EDGE_MIN_FOLDS else None
        if E is None or E.shape[0] < 2:
            continue

        rng = np.random.default_rng(PERM_SEED)
        # Sign-flips as a (B, n_f) matrix of +/-1; one row is one draw from the null, and
        # it is applied to all R rules at once. `E @ s` is then every rule's permuted mean
        # in a single product.
        S = rng.choice(np.array([-1.0, 1.0]), size=(N_PERMUTATIONS, n_f))
        means = S @ E.T / n_f                              # (B, R)
        # Flipping signs cannot change a rule's spread around ITS OWN permuted mean by
        # much, but computing it per draw is exact and costs one more product.
        sq = (S ** 2) @ (E ** 2).T / n_f                   # E[x^2] per draw
        var = (sq - means ** 2) * n_f / (n_f - 1)
        se = np.sqrt(np.maximum(var, 1e-24) / n_f)
        max_t = np.max(means / se, axis=1)
        bar = float(np.quantile(max_t, 1.0 - FWER_ALPHA))

        # The bar Bonferroni would have set, beside the measured one, so the size of the
        # correction is on the row rather than in a commit message.
        #
        # **Do not read the difference as the redundancy.** Two errors are in flight and
        # they partly cancel: Bonferroni assumes independence, which is too strict here,
        # but it also takes a NORMAL quantile when 21 folds is a t with fat tails, which
        # is too lenient. On us_stocks 1d they nearly annihilate — 3.84 against a measured
        # 3.76 — and reading that as "barely any redundancy" would be wrong. Calibrated
        # against simulated independent panels at the same fold count, 387 independent
        # rules would need 4.42, so this panel behaves like roughly 85 of them. That
        # calibration is in `test_t_bar.py`; it is not run here because it is a property
        # of the shape of the search, not of tonight's numbers.
        import metrics as _m
        for r in panel:
            r["t_bar_maxt"] = bar
            r["t_bar_bonferroni"] = _m.bonferroni_t(int(E.shape[0]))
            r["n_candidates_maxt"] = int(E.shape[0])
    return rows


def _standard(rows: list[dict], n_folds: dict) -> list[dict]:
    """The six acceptance criteria, computed on the BOOK.

    `metrics.apply_edge_standard` is imported rather than reimplemented: it owns the
    thresholds, the Bonferroni correction on `t`, the rankability preconditions and the
    `underpowered` verdict, and a second copy of that logic here would be free to drift
    from `config.EDGE_STANDARD` — which is the single definition of what an edge is in
    this repo. All this does is name the book's own columns as the six inputs:

        S  dsharpe      book Sharpe minus the passive book's, both over the cash rate
        T  boot_t       block bootstrap over the book's bars
        R  vs_random    against RANDOM_* books at the same exposure
        C  vs_constant  against the same basket held flat at the book's own weight
        W  edge_wealth  money above the volatility-matched blend
        H  edge_headroom  multiples of the fee schedule that advantage survives

    **Two of the six are not the same statistic as the per-asset stage's**, and the
    difference is stated rather than hidden:

    * `S` here is the difference of two pooled Sharpes; per asset it is the mean of
      per-fold differences, then a median across names. Pooled weights every bar equally,
      per-fold weights every fold equally.
    * `T` here is a block bootstrap over bars; per asset it is a t across folds. Both are
      "across time, never across assets", which is the property that matters — the book
      IS every asset at once, so no version of this can be inflated by breadth.

    `n_folds` supplies the fold count per (class, tf) so `underpowered` keeps meaning what
    it meant: a sheet whose walk-forward calendar is too short to resolve the effect
    cannot deliver a verdict, however many bars its book has.
    """
    import metrics
    for r in rows:
        cls_tf = (r.get("class"), r.get("tf"))
        r.update(metrics.apply_edge_standard({
            # The per-fold pair, not the pooled Sharpe difference and not the bootstrap:
            # `config.EDGE_STANDARD` defines S as the mean of per-fold values and T as the
            # t across folds, and its 0.10 threshold was calibrated against the fold-to-fold
            # sd. `dsharpe` and `boot_t` remain on the row as the pooled second opinion.
            "edge_dsharpe": r.get("fold_dsharpe"),
            "edge_t": r.get("fold_t"),
            "edge_vs_random": r.get("vs_random"),
            "edge_vs_constant": r.get("vs_constant"),
            "edge_wealth": r.get("edge_wealth"),
            "edge_headroom": r.get("edge_headroom"),
            "n_trials": r.get("n_trials"),
            # The measured family-wise bar, where the panel was big enough to measure one.
            # Absent -> `apply_edge_standard` falls back to Bonferroni on `n_trials`.
            "t_bar_override": r.get("t_bar_maxt"),
            "t_bar_source": "maxT",
            # How many folds the BOOK could actually be scored on, against how many the
            # sheet has. A rule measurable on half the calendar is caught here by the same
            # precondition that catches it per asset.
            "n_folds_scored": r.get("n_folds_scored"),
            "exposure": r.get("exposure"),
            "fold_coverage": (r.get("n_folds_scored", 0) / n_folds[cls_tf]
                              if n_folds.get(cls_tf) else None),
        }))
    return rows


def _deflate(rows: list[dict], n_trials_override: int | None = None,
             dispersion_override: float | None = None) -> list[dict]:
    """Deflate each panel against the trials THAT PANEL actually consumed.

    Two sources, added because either alone understates the bar:

      * the rules scored in this run — the search that produced these numbers;
      * `strategies.trials`, the append-only ledger, which remembers candidates that were
        registered and then abandoned. Those consumed a look at the data and leave no
        other trace, and excluding them is the precise selection effect the deflation
        exists to price.

    **Both are currently wrong in the flattering direction, and by a lot.** The ledger is
    EMPTY — `data/reference/trials.csv` has a header and no rows — so `extra` is 0, and
    `len(sharpes)` counts only the rules named on this command line. A run invoked as
    `--rules ibs BUYHOLD RANDOM_50 ...` therefore deflates against **6** trials and
    reports DSR ~1.0, when the search that surfaced `ibs` as a candidate at all was the
    whole leaderboard: `edge_standard.csv` puts us_stocks 1d at **1,273**. Deflating a
    hand-picked winner against the six rules you hand-picked it into is not a haircut.

    `n_trials_override` is the honest N until the ledger is populated. It replaces the
    COUNT only — the dispersion still comes from the Sharpes actually measured here, so
    run it over `--catalog` rather than a shortlist or the spread is estimated from a
    handful of rows and is itself meaningless.

    `dispersion_override` exists for the case where you deliberately run only a shortlist.
    Deflation needs **two** numbers about the search — how many candidates it looked at
    and how far apart their Sharpes fell — and a shortlist run can supply neither. The
    count comes from `edge_standard.csv`'s `n_trials`; the spread can come from the same
    file's `sharpe` column, which is the actual measured dispersion across the actual
    search. Pass it ANNUALISED; this divides by sqrt(bpy) to match the per-bar convention
    `metrics.deflated_sharpe` expects.

    One caveat travels with that substitution and belongs in any writeup using it:
    `edge_standard`'s Sharpes are MEDIAN-ASSET Sharpes and the rows being deflated here
    are BOOK Sharpes. A book diversifies, so its cross-rule spread is not identical to the
    median asset's. It is the only broad measured spread that exists, and it is far better
    than a spread computed from the two rules you already believe in — that one is
    guaranteed to be too small, which makes the bar too low, which is the exact direction
    the deflation is supposed to protect against.
    """
    try:
        from strategies import trials as _trials
    except Exception:
        _trials = None

    by_panel: dict[tuple, list[dict]] = {}
    for r in rows:
        by_panel.setdefault((r["class"], r["tf"]), []).append(r)

    for (cls, tf), group in by_panel.items():
        sharpes = np.array([r["sharpe"] for r in group
                            if r.get("sharpe") is not None
                            and np.isfinite(r.get("sharpe", np.nan))])
        extra = _trials.count(f"{cls}/{tf}") if _trials else 0
        n_trials = (int(n_trials_override) if n_trials_override
                    else max(len(sharpes) + extra, 2))
        n_trials = max(n_trials, 2)
        for r in group:
            ex, bpy = r.pop("_excess", None), r.pop("_bpy", None)
            if ex is None or bpy is None:
                continue
            if dispersion_override:
                disp_ann = float(dispersion_override)
            elif sharpes.size >= 2:
                disp_ann = float(sharpes.std(ddof=1))
            else:
                continue
            d = metrics.deflated_sharpe(ex, n_trials, disp_ann / np.sqrt(bpy), bpy)
            r.update({k: d.get(k) for k in
                      ("skew", "kurtosis", "n_trials", "sr_star_ann", "psr", "dsr",
                       "dsr_pass")})
            r["n_trials_ledger"] = extra
            r["n_trials_measured"] = int(len(sharpes))
            r["n_trials_source"] = "override" if n_trials_override else "run+ledger"
            r["trial_dispersion"] = disp_ann
            r["dispersion_source"] = "override" if dispersion_override else "this run"
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", default=list(CLASSES),
                    choices=list(CLASSES))
    ap.add_argument("--tf", nargs="+", default=["1d"])
    ap.add_argument("--rules", nargs="+", default=None,
                    help="labels to score; default is the baseline plus the controls")
    ap.add_argument("--rules-file", dest="rules_file", default=None, metavar="PATH",
                    help="read labels from a file, one per line; '#' comments and blank "
                         "lines ignored. Exists because 872 of the leaderboard's labels "
                         "contain a shell metacharacter -- every pair is "
                         "'LEG_A~LEG_B|operator' -- so passing a whole sheet through "
                         "--rules means quoting each one and getting it right 402 times. "
                         "Merged with --rules when both are given, first occurrence wins.")
    ap.add_argument("--catalog", action="store_true",
                    help="score every published-strategy cell runnable on the class")
    ap.add_argument("--regime", action="store_true",
                    help="also score hi/lo trailing-vol gates over each cell. Both halves "
                         "always, never one: picking the half that worked after seeing "
                         "both is selection on the test set in a regime filter's clothes")
    ap.add_argument("--pit", action="store_true",
                    help="apply point-in-time TOP-100 membership (us_stocks only): the "
                         "book holds a name only on the bars it held a top-100 slot, "
                         "~100 of the 216 names at a time")
    ap.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                    help="drop bars before this date, as if less history had been "
                         "fetched. Exists because IBS's edge is dated: see below")
    ap.add_argument("--fill", choices=("close", "open", "close_lag"), default="close",
                    help="which price the money is earned on. 'open' fills at the bar "
                         "AFTER the signal and earns open-to-open, which is the control "
                         "for Blume-Stambaugh bid-ask bounce. See build_book")
    ap.add_argument("--flatten-eod", action="store_true",
                    help="Force every non-baseline rule flat on each session's last "
                         "bar. A BOUND for the intraday sheets, not a correction: off "
                         "is the faithful reading of a Pine strategy (which holds "
                         "through the close), on is the true day-trading reading. "
                         "Report both, as with --fill. No effect on crypto, which has "
                         "no session, or on 1d/4h, which have nothing to flatten.")
    ap.add_argument("--charge-bench", action="store_true",
                    help="charge the baseline the same fee scenario as the strategy. "
                         "Published numbers ran it free")
    ap.add_argument("--stress-delisted", type=float, default=0.0, metavar="FRAC",
                    help="put the unpriceable S&P members back into the book and send "
                         "FRAC of them to -100%% over their last 252 bars of membership. "
                         "On the top-100 universe they are admitted at the rate a random "
                         "member holds a slot, since an unpriceable name cannot be "
                         "ranked. Requires --pit. 0 disables")
    ap.add_argument("--n-trials", type=int, default=None, metavar="N",
                    help="deflate against N trials instead of counting the rules on this "
                         "command line. The ledger is empty, so the default flatters a "
                         "shortlist run badly — us_stocks 1d consumed 1273. See _deflate")
    ap.add_argument("--trial-dispersion", type=float, default=None, metavar="SD",
                    help="annualised Sharpe spread ACROSS the search, for deflation. "
                         "Needed whenever only a shortlist is run, because the spread "
                         "cannot be measured from the rules you already believe in. "
                         "us_stocks 1d edge_standard.csv: 0.2769 over 178 rows")
    ap.add_argument("--cash-rate", type=float, default=None, metavar="RATE",
                    help="replace the historical T-bill path with a constant annual "
                         "rate. 0 means idle capital earns NOTHING, which is what the "
                         "dashboard's books are built with — see the note in build_book. "
                         "Omit for the historical path")
    ap.add_argument("--no-factors", action="store_true")
    ap.add_argument("--walkforward", action="store_true",
                    help="select each fold on IN-SAMPLE cash-matched excess "
                         "CAGR and score the stitched out-of-sample")
    ap.add_argument("--out", default="portfolio.csv")
    ap.add_argument("--curves", action="store_true",
                    help="also write book_curves_<class>_<tf>.json — the equity series "
                         "behind every scored row, which is what the dashboard draws. "
                         "One file per sheet regardless of how many --out holds")
    args = ap.parse_args()

    fac = None
    if not args.no_factors:
        try:
            fac = ff.load()
        except Exception as exc:
            print(f"factor file unavailable ({exc}); running without alpha columns")

    # `intervals` decides who is in the BOOK on each bar; `sp_intervals` is a different
    # table answering a different question, and conflating them is how the survivorship
    # stress silently reports "no stress".
    #
    #   intervals    -> top-100 membership. ~100 names live per bar out of 216 ever. This
    #                   is the universe being studied and the one the weights come from.
    #   sp_intervals -> S&P 500 membership. Used ONLY by `--stress-delisted`, whose pool is
    #                   the members the vendor cannot price at all — and those names have
    #                   no bars, so they can never appear in a table built by ranking bars.
    #                   Read the top-100 table for that pool and it is empty by
    #                   construction, which reads as a clean bill of health.
    intervals = sp_intervals = None
    if args.pit:
        import sp500_membership
        import top100_membership
        intervals = top100_membership.load()
        sp_intervals = sp500_membership.load()
        live = len(top100_membership.current(intervals))
        print(f"point-in-time membership: {len(intervals)} top-100 spells over "
              f"{intervals['symbol'].nunique()} names ({live} live at the last rebalance), "
              f"ranked on trailing dollar volume -- NOT market cap")
        print(f"                          S&P table carried for --stress-delisted only: "
              f"{len(sp_intervals)} spells, trustworthy from "
              f"{sp500_membership.RELIABLE_FROM.date()}")

    # Read once, not per class: the same sheet of labels is scored on every class named on
    # the command line, and a label that no class can resolve is a typo worth seeing now
    # rather than as a quietly shorter output file.
    file_rules: list[str] = []
    if args.rules_file:
        path = Path(args.rules_file)
        for line in path.read_text(encoding="utf-8").splitlines():
            label = line.strip()
            # Whole-line comments only. A trailing-comment rule would have to strip '#'
            # out of the middle of a label, and nothing guarantees no label ever uses one.
            if not label or label.startswith("#") or label in file_rules:
                continue
            file_rules.append(label)
        print(f"--rules-file {path}: {len(file_rules)} labels")

    rows = []
    curve_out: dict = {}
    n_folds: dict = {}
    for asset_class in args.classes:
        free = scenario(asset_class, "gross")
        iv = intervals if asset_class == "us_stocks" else None
        sp_iv = sp_intervals if asset_class == "us_stocks" else None
        for tf in args.tf:
            # The scenario to CHARGE, which is the headline schedule — not
            # `scenarios_for(...)[0]`.
            #
            # That indexing worked only while 1d/4h were collapsed to a single
            # `gross` entry. Once the full grid came back, `[0]` was still `gross`,
            # so a run explicitly labelled "net of fees" charged nothing: the fee
            # was zero and the fold-switch penalty, being `delta * per_side`, was
            # zero times something. It produced numbers identical to the gross run
            # to four decimals and looked like a successful re-run.
            fee = headline_scenario(asset_class)
            t0 = time.time()
            try:
                data = td_loader.load(asset_class, tf)
            except Exception as exc:
                print(f"{asset_class}/{tf}: no data ({exc})")
                continue
            if not data:
                continue
            # Truncating right here -- at the single point every book is built from -- is
            # exactly equivalent to having fetched less history, so nothing downstream
            # needs to know the window moved.
            #
            # It exists because `ibs` on this class earns its money in a window whose data
            # cannot support the statistic. IBS is `(C-L)/(H-L)`, which on a bar where
            # High == Low is 0/0 -- undefined, not zero -- and the vendor's early equity
            # history is largely stale single-price quotes: 2.5% of pre-2005 bars have no
            # intraday range at all, reaching 98% on SEE, 82% on AET, 69% on HUBB. The
            # same era is quantized, MNST's split-adjusted 1989 price being $0.028 where
            # one tick is 2.27%. Per-asset the split is stark -- MNST compounds x8.07e7 to
            # 2009 and x1.63 after it, SIG x1.19e8 then x1.14 -- so a 51-year book is
            # mostly reporting what a rounding grid did before 2005.
            #
            # Not a default. Shortening the sample raises every noise ceiling
            # (`metrics.se_ir` falls as 1/sqrt(years)) and history is this repo's only
            # lever on them, so throwing away 35 years is a real cost that has to be paid
            # deliberately and quoted alongside the result.
            if args.start:
                cut = pd.Timestamp(args.start)
                data = {s: df.loc[df.index >= cut] for s, df in data.items()}
                data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
                if not data:
                    print(f"{asset_class}/{tf}: nothing survives --start {args.start}")
                    continue
                print(f"{asset_class}/{tf}: --start {args.start} -> {len(data)} names")
            # The sheet's own walk-forward calendar, counted over the SAME span the books
            # are scored on, so `underpowered` keeps meaning what it means everywhere
            # else: too few folds to resolve the effect being claimed. A book has plenty
            # of bars on every sheet here; bars are not the scarce thing.
            _span = ([d.index.min() for d in data.values()],
                     [d.index.max() for d in data.values()])
            sheet_folds = wfmod.generate_folds(min(_span[0]), max(_span[1]))
            n_folds[(asset_class, tf)] = len(sheet_folds)
            rules = list(args.rules or [])
            rules += [r for r in file_rules if r not in rules]
            rules = rules or ([BASELINE] + list(CONTROLS))
            if args.catalog:
                rules = [BASELINE] + list(CONTROLS) + cells(asset_class)
            if args.regime:
                base = [r for r in rules if r not in (BASELINE,) and
                        not r.startswith(("ALWAYS_", "RANDOM_"))]
                rules += [f"volregime:{side}:0.5:{r}" for r in base for side in ("hi", "lo")]
            # DSR is deflated in a SECOND PASS, after this panel's rules are scored,
            # against this panel's own Sharpe dispersion.
            #
            # It used to read `riskmatch.csv`, and that was wrong in a way that inverted
            # conclusions: that file was computed on the OLD universe, so a us_etfs panel
            # was deflated against a 3-name sheet containing TQQQ and SOXL, whose
            # leveraged Sharpes give a huge dispersion and therefore an unreachable bar.
            # `ibs` on us_etfs 1d scored DSR 0.007 while simultaneously beating its own
            # panel's luck threshold — the two numbers disagreed because one of them was
            # measuring a different study.
            trial_sharpes = None

            if args.walkforward:
                books, idx, bpy = {}, None, None
                for rule in rules:
                    bk = build_book(data, rule, fee, free, iv,
                                    cash_override=args.cash_rate,
                                    resolve=leg_resolver(asset_class, tf),
                                    fill=args.fill,
                                    bench_fee=fee if args.charge_bench else None,
                                    flatten_eod=args.flatten_eod,
                                    asset_class=asset_class)
                    if bk is None:
                        continue
                    books[rule] = (bk["strat"].to_numpy(), bk["bench"].to_numpy(),
                                   bk["rf"].to_numpy(), bk["signed"].to_numpy())
                    idx, bpy = bk["strat"].index, bk["bpy"]
                if len(books) < 2:
                    print(f"{asset_class}/{tf}: too few books for walk-forward")
                    continue
                from config import per_side_bps
                res = walkforward_books(books, bpy, idx,
                                        per_side=per_side_bps(fee) / 10000.0)
                picks = res.pop("picks", [])
                fixed = res.pop("fixed", {})
                rows.append({**res, "class": asset_class, "tf": tf,
                             "rule": "IS#1[cashmatch]", "pit": bool(iv is not None),
                             "n_candidates": len(books)})
                pd.DataFrame(picks).to_csv(
                    RESULTS_DIR / f"pwf_picks_{asset_class}_{tf}.csv", index=False)
                pd.Series(fixed, name="excess_cagr").sort_values(ascending=False).to_csv(
                    RESULTS_DIR / f"pwf_fixed_{asset_class}_{tf}.csv")
                print(f"{asset_class}/{tf}: {res.get('n_folds')} folds, "
                      f"{len(books)} candidates, IS#1 "
                      f"{res.get('is1_excess_cagr', float('nan')):+.4f}/yr vs best fixed "
                      f"{res.get('best_fixed_excess_cagr', float('nan')):+.4f} "
                      f"[{res.get('best_fixed_rule')}] "
                      f"-> selection cost {res.get('selection_cost', float('nan')):+.4f}")
                continue

            for rule in rules:
                book = build_book(data, rule, fee, free, iv,
                                  cash_override=args.cash_rate,
                                  fill=args.fill,
                                  bench_fee=fee if args.charge_bench else None,
                                  stress_frac=args.stress_delisted,
                                  stress_intervals=sp_iv,
                                  resolve=leg_resolver(asset_class, tf),
                                  flatten_eod=args.flatten_eod,
                                  asset_class=asset_class)
                if book is None:
                    continue
                # The stressed pair REPLACES the book's series rather than sitting beside
                # it, so every downstream statistic — Sharpe, bootstrap, cash-match,
                # factor alpha — is computed on the stressed universe without `score`
                # needing to know the stress exists. Run it as a separate --out and diff.
                if args.stress_delisted and "strat_stress" in book:
                    book["strat"] = book["strat_stress"]
                    book["bench"] = book["bench_stress"]
                # Attached here rather than inside `build_book`, which does not know which
                # class it is building and should not have to: the index is a property of
                # the sheet, not of the rule. Cached across rules by `index_returns`.
                ixr, ixs = index_returns(asset_class, tf, book["strat"].index, args.fill)
                book["index_ret"], book["index_symbol"] = ixr, ixs
                # The sheet's fold calendar, for criteria S and T. A property of the
                # sheet, not of the rule, so it is attached here like the index is.
                book["folds"] = sheet_folds
                row = score(book, rule, trial_sharpes, fac)
                row.update({"class": asset_class, "tf": tf, "pit": bool(iv is not None),
                            "fill": args.fill})
                if args.curves:
                    # Built from the SAME `book` the row above was scored on, in the same
                    # iteration, so there is no window in which one could be regenerated
                    # without the other.
                    curve_out.setdefault((asset_class, tf), {})[rule] = curve_payload(
                        book, row, asset_class, tf, args.fill)
                for k in ("stress_n_missing", "stress_n_departed",
                          "stress_n_failing", "stress_share"):
                    if k in book:
                        row[k] = book[k]
                rows.append(row)
            print(f"{asset_class}/{tf}: {len(rules)} rules in {time.time()-t0:.0f}s")

    if not rows:
        print("nothing scored")
        return

    # Order matters and is not arbitrary: `_deflate` supplies `n_trials`, which the
    # standard's t-criterion needs for its Bonferroni bar, and `_vs_random` supplies
    # criterion R. Both are panel-wide passes — they need every rule scored first — so
    # the standard, which reads their output, has to run last.
    rows = _standard(
        _t_bar(_vs_random(_deflate(rows, args.n_trials, args.trial_dispersion))),
        n_folds)
    df = pd.DataFrame(rows)
    lead = ["class", "tf", "rule", "pit", "n_names", "years", "exposure",
            "cagr", "cashmatch_bench_cagr", "cashmatch_excess_cagr",
            "cashmatch_ratio", "win_rate", "profit_factor", "n_trades",
            "sharpe", "bench_sharpe", "dsharpe", "boot_t", "dsr",
            "alpha_vs_bh_ann", "alpha_vs_bh_t"]
    df = df[[c for c in lead if c in df.columns]
            + [c for c in df.columns if c not in lead]]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / args.out
    # Walk-forward rows carry different columns from per-rule rows, so sort on
    # whichever ranking column this run actually produced.
    sort_col = next((c for c in ("cashmatch_excess_cagr", "is1_excess_cagr",
                                 "dsharpe") if c in df.columns), None)
    keys = ["class", "tf"] + ([sort_col] if sort_col else [])
    df.sort_values(keys, ascending=[True, True] + ([False] if sort_col else [])
                   ).to_csv(out, index=False)
    print(f"\nwrote {out}  ({len(df)} rows)")

    for (cls, tf), payloads in curve_out.items():
        # The whole point of this file is that it cannot disagree with the CSV, so check
        # it rather than trust it. A mismatch means someone changed one of the two
        # computations, which is precisely the failure that made this stage own the
        # curves; it is loud and it does not stop the write, because a curve file that
        # exists and is wrong is easier to diagnose than one that silently never appeared.
        g = df[(df["class"] == cls) & (df.tf == tf)].set_index("rule")
        bad = []
        for rule, p in payloads.items():
            if rule not in g.index:
                continue
            r, m = g.loc[rule], p["metrics"]
            for key, col, scale in (("cagr_pct", "cagr", 100.0),
                                    ("sharpe", "sharpe", 1.0),
                                    ("max_dd_pct", "dd", 100.0)):
                a, b = m.get(key), r[col] * scale
                # BOTH undefined is agreement, not drift. `ALWAYS_FLAT` and the candle
                # rules that never fire have zero variance, so their Sharpe is genuinely
                # undefined — the JSON writes `null` and the CSV writes `nan`, which is
                # the same statement in two encodings. Flagging that pair produced a
                # dozen false alarms per sheet and would have buried a real one.
                b_ok = np.isfinite(b)
                if (a is None) != (not b_ok):
                    bad.append(f"{rule}.{key} curve={a} csv={b}")
                elif a is not None and b_ok and abs(a - b) > 0.05:
                    bad.append(f"{rule}.{key} curve={a} csv={b:.4f}")
        p = RESULTS_DIR / f"book_curves_{cls}_{tf}.json"
        p.write_text(json.dumps(payloads, separators=(",", ":")), encoding="utf-8")
        note = f"  MISMATCH vs csv: {len(bad)} ({'; '.join(bad[:3])})" if bad else ""
        print(f"wrote {p}  ({len(payloads)} rules, {p.stat().st_size / 1e6:.1f} MB)"
              f"{note}")

    show = ["rule", "exposure", "cagr", "cashmatch_bench_cagr",
            "cashmatch_excess_cagr", "cashmatch_ratio", "dd",
            "cashmatch_bench_dd", "sharpe", "dsharpe", "boot_t", "dsr",
            "win_rate", "profit_factor"]
    show += [c for c in ("alpha_ann", "alpha_t", "alpha_vs_bh_ann", "alpha_vs_bh_t")
             if c in df.columns]
    for (cls, tf), g in df.groupby(["class", "tf"]):
        print(f"\n=== {cls} / {tf} ===")
        key = next((c for c in ("cashmatch_excess_cagr", "is1_excess_cagr",
                                "dsharpe") if c in g.columns), None)
        cols = [c for c in show if c in g.columns]
        skey = key if key in g.columns else cols[0]
        # Sort the frame, THEN select display columns — the sort key is not
        # always one of the displayed ones.
        print(g.sort_values(skey, ascending=False)[cols].to_string(
            index=False, float_format=lambda v: f"{v:.3f}"))


# ---------------------------------------------------------------- walk-forward
#
# The stage above scores a rule over its whole history. That measures a rule; it does not
# measure *choosing* one, and on this very dataset the difference decided everything:
# `walkforward.py` found crypto 4h's best fixed rule at IR +0.647 while the rule a
# researcher could actually have picked in real time scored -0.029. The entire edge was
# the privilege of hindsight.
#
# `strat_wf.py` already runs the catalog through the fold machinery, but it selects
# `IS#1` on in-sample **IR** — and IR pays a rule for time-in-market, so a 46%-invested
# rule like `ibs` is close to unselectable by it no matter how good it is. That answers
# "would an IR-driven researcher have found this?", not "does the edge survive selection
# on the measure we now believe is correct?".
#
# So selection here happens on in-sample **cash-matched excess CAGR** — the same quantity
# the leaderboard is ranked on, chosen on the in-sample window only and applied blind to
# the next one.


def _cagr_of(r: np.ndarray, bpy: float) -> float:
    # The canonical form additionally guards `bpy <= 0`, which this one did not. It is
    # unreachable either way: `vector.bars_per_year` returns NaN for a non-positive span
    # and a strictly positive number otherwise, never exactly zero.
    return stats.cagr(r, bpy)


def cashmatch_excess(strat: np.ndarray, bench: np.ndarray, rf: np.ndarray,
                     bpy: float) -> float:
    """Excess CAGR against buy-and-hold de-levered to the strategy's own volatility."""
    ok = np.isfinite(strat) & np.isfinite(bench) & np.isfinite(rf)
    s, b, f = strat[ok], bench[ok], rf[ok]
    if s.size < 30:
        return float("nan")
    vs, vb = s.std(ddof=1), b.std(ddof=1)
    if not np.isfinite(vs) or not np.isfinite(vb) or vb <= 0:
        return float("nan")
    w = min(vs / vb, 1.0)
    return _cagr_of(s, bpy) - _cagr_of(w * b + (1.0 - w) * f, bpy)


def walkforward_books(books: dict, bpy: float, index: pd.DatetimeIndex,
                      per_side: float = 0.0) -> dict:
    """Pick each fold's rule on its in-sample window; score the stitched out-of-sample.

    `books` maps rule -> (strat, bench, rf) aligned to `index`. Positions were built on
    the full series and are masked per fold, which is sound for exactly the reason
    `walkforward.py` records: every rule here is causal, so a bar's position does not
    depend on which window asked for it.

    Note what is NOT modelled: switching rules at a fold boundary is a real trade and is
    not charged, because these sheets run at zero cost. At a non-zero fee schedule that
    would flatter re-selection, and the stitch would need to charge it.
    """
    folds = wfmod.generate_folds(index.min(), index.max())
    if len(folds) < 3:
        return {"n_folds": len(folds), "skipped": "too few folds"}

    rules = list(books)
    oos_parts, picks = [], []
    prev_rule = None
    for fold in folds:
        is_m = (index >= fold.is_start) & (index < fold.is_end)
        oos_m = (index >= fold.is_end) & (index < fold.oos_end)
        if is_m.sum() < 250 or oos_m.sum() < 30:
            continue
        best, best_score = None, -np.inf
        for rule in rules:
            s, b, f, _sg = books[rule]
            sc = cashmatch_excess(s[is_m], b[is_m], f[is_m], bpy)
            if np.isfinite(sc) and sc > best_score:
                best, best_score = rule, sc
        if best is None:
            continue
        s, b, f, sg = books[best]
        seg_s = s[oos_m].copy()
        # Charge the fold switch. Changing the selected rule at a boundary re-positions
        # the whole book, and leaving it free makes re-selection look costless — over 26
        # switches in 47 folds that is not a rounding error. `walkforward.stitch` charges
        # it for the IR path by writing one position series and backtesting it once; the
        # equivalent here is the portfolio's own change in signed exposure at the seam.
        #
        # This is a LOWER BOUND: symbol-level changes that offset each other net out in a
        # portfolio-level exposure difference, so the true turnover is at least this.
        if prev_rule is not None and best != prev_rule and seg_s.size:
            prev_sg = books[prev_rule][3]
            delta = float(abs(sg[oos_m][0] - prev_sg[oos_m][0]))
            seg_s[0] -= delta * per_side
        prev_rule = best
        oos_parts.append((seg_s, b[oos_m], f[oos_m]))
        picks.append({"fold": str(fold.is_end.date()), "rule": best,
                      "is_excess_cagr": best_score})

    if not oos_parts:
        return {"n_folds": len(folds), "skipped": "no scorable folds"}

    S = np.concatenate([p[0] for p in oos_parts])
    B = np.concatenate([p[1] for p in oos_parts])
    F = np.concatenate([p[2] for p in oos_parts])
    out = {
        "n_folds": len(oos_parts),
        "n_switches": sum(1 for a, b in zip(picks, picks[1:]) if a["rule"] != b["rule"]),
        "is1_excess_cagr": cashmatch_excess(S, B, F, bpy),
        "picks": picks,
    }
    out.update({f"is1_{k}": v for k, v in
                block_bootstrap_dsharpe(S, B, F, bpy).items()})

    # The same stitched out-of-sample window, scored for every rule held FIXED. The best
    # of these is the hindsight number, and the gap to IS#1 is what choosing costs.
    fixed = {}
    for rule in rules:
        s, b, f, _sg = books[rule]
        parts = [(s[(index >= fo.is_end) & (index < fo.oos_end)],
                  b[(index >= fo.is_end) & (index < fo.oos_end)],
                  f[(index >= fo.is_end) & (index < fo.oos_end)]) for fo in folds]
        parts = [p for p in parts if p[0].size]
        if not parts:
            continue
        fixed[rule] = cashmatch_excess(np.concatenate([p[0] for p in parts]),
                                       np.concatenate([p[1] for p in parts]),
                                       np.concatenate([p[2] for p in parts]), bpy)
    if fixed:
        best_rule = max(fixed, key=lambda r: (fixed[r] if np.isfinite(fixed[r]) else -np.inf))
        out["best_fixed_rule"] = best_rule
        out["best_fixed_excess_cagr"] = fixed[best_rule]
        out["selection_cost"] = out["is1_excess_cagr"] - fixed[best_rule]
        out["fixed"] = fixed
    return out


if __name__ == "__main__":
    main()
