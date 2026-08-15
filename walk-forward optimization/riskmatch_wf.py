"""Stage 1g: match the risk, then count the money.

Every number in this project so far compares a rule that is in the market part of the
time against a benchmark that is in it all the time, and calls the difference the
result. That comparison confounds two different things: **how good the signal is**, and
**how much capital was deployed**. Only the first is skill. The second is a dial.

The fix is standard and the whole stage is built on it:

    return = skill x leverage

Skill is scale-invariant — Sharpe, or information ratio, or MAR, none of which change
when you double the position. Return is not. So a rule that earns 12% at half the
volatility of buy-and-hold has not lost to a benchmark earning 19%; it has been measured
at the wrong size. **Size it up until the risk matches, then compare the money.** That is
the only comparison in which "beats buy-and-hold" means something a trader can act on.

Two corrections make this honest, and the first of them fixes a real defect in every
earlier stage:

**Idle capital earns interest, and this project has been crediting it zero.** A rule long
46% of the time held cash for the other 54% — across 41 years that include Treasury bills
at 15% in 1981. Charging that rule 0% on its cash is not conservative, it is wrong, and
it penalised precisely the rules that sit out. Actual daily 3-month T-bill rates are used
(`../data/rates/DTB3.csv`, FRED series DTB3, 1954 to date), so the cash leg is worth what
it was actually worth at the time.

**Leverage is not free and not unlimited.** Borrowed capital pays the benchmark rate plus
a spread — Interactive Brokers quotes benchmark + 1.5% for accounts over $100k — and
Reg-T caps overnight equity leverage at 2:1. Both are applied. Where the cap binds, the
row says so rather than quietly reporting an unreachable number.

Three benchmarks come out, in increasing order of how understandable they are:

    Sharpe (excess of cash)   the scale-free skill number. At matched risk, whoever has
                              the higher one ends with more money. Everything else here
                              is this number restated.
    equal-volatility wealth   scale the rule until its realised volatility equals
                              buy-and-hold's, then compare what $10,000 becomes.
    equal-drawdown wealth     scale until the worst peak-to-trough loss matches instead.
                              Volatility is what quants match; drawdown is what a person
                              actually lives through.

Run::

    python riskmatch_wf.py --tf 1d 4h               # every rule the leaderboard shows
    python riskmatch_wf.py --tf 1d --rules ibs macd_cross     # just these
    python riskmatch_wf.py --tf 1d --side long                # skip the long/short pass
"""

from __future__ import annotations

import argparse
import time

from statistics import NormalDist

import numpy as np
import pandas as pd

from wfo_paths import RESULTS_DIR, write_bulk    # noqa: F401  (wires sys.path first)
from config import (headline_key,  # noqa: F401
                    CLASSES, DATA_DIR, HEADLINE_SCENARIO, MIN_BARS, WF_MIN_FOLDS,
                    scenario)
from engines import vector
import metrics
import td_loader
import walkforward as wfmod

from stockhunt import stats

import signals
from strategies.catalog import BASELINE, CATALOG, CONTROLS, RANDOM_DRAWS, build

CAPITAL = 10_000.0

# Set by `--n-trials`. None means "count the rules actually in this run", which is right
# for a full sweep and WRONG for a shortlist.
#
# A shortlist is drawn from a search that already happened. Re-scoring 107 survivors of
# ~1,300 candidates while telling `apply_edge_standard` that 107 things were tried lowers
# both the corrected t bar and the noise ceiling, so rules can "pass" for no reason other
# than being asked about in a smaller group. That is selection on the test set wearing a
# re-run's clothes, and this repo has already retracted findings for it.
#
# The honest figure is the size of the population the shortlist came OUT of, not the size
# of the shortlist. `--n-trials` is how a caller declares it.
_N_TRIALS_OVERRIDE: int | None = None


def _n_trials(t) -> int:
    """Candidates this sheet searched: the declared population, else what is in hand."""
    return int(_N_TRIALS_OVERRIDE or t.rule.nunique())


def to_long_short(pos: np.ndarray) -> np.ndarray:
    """Long/flat -> long/short: `2p - 1`, so "stay out" becomes "sell it".

    The catalog's rules are long/flat, and the whole reason `ibs` loses to buy-and-hold is
    that it sits in cash 54% of the time while those bars still drift up. Shorting them
    removes the cash leg entirely — the rule is always in the market, so the exposure
    handicap that dominates every earlier stage largely disappears.

    It is not free, and the prior is bad. The conditional-return test measured the skipped
    bars at **+3.4 bps**, positive, so a short leg is expected to *lose* about that much
    per bar on equities before costs. It also doubles turnover, because every signal
    change is now a reversal paying two sides rather than one, and it pays borrow for
    every bar held short. Whether the risk reduction outweighs all three is exactly what
    the risk-matched benchmark is for.
    """
    return 2.0 * np.clip(pos, 0.0, 1.0) - 1.0


def endorsed_sides(asset_class: str, timeframe: str) -> dict[str, str]:
    """`rule -> the side this module endorsed`, read back off `edge_standard.csv`.

    Every consumer of a rule's identity needs to know whether "the rule" means long/flat or
    long/short, because a leaderboard row scored one way and a chart drawn the other are
    two different strategies wearing one name. This module makes that decision — it is the
    only thing that scores both sides — so it is the only thing that should answer the
    question, and the answer lives here rather than being re-derived by each caller.

    It was re-derived by each caller, and they disagreed. `curves.py` had no concept of a
    side at all and drew long/flat unconditionally, which put a long/flat equity curve and
    its whole metrics table under 45 of 272 charted rows whose verdict was computed on the
    short side — 19 of 44 on crypto 4h alone. The dashboard meanwhile picked the better
    side per *symbol*, which is a third answer and a biased one.

    Ranked on `edge_dsharpe`, the same key the payload's `_edge_index` uses, and
    `unrankable` rows are skipped rather than beating a real one on a NaN: those are rows
    that cannot support a ratio at all (near-zero exposure, too few folds), so a rule whose
    long side is unrankable is legitimately represented by its short side.

    TIES GO TO LONG, and they are not rare: 112 of 534 (rule, sheet) pairs carry a byte-
    identical `edge_dsharpe` on both sides — degenerate rows sharing one sentinel value —
    and three more differ only in the 6th decimal. Left to positional order the winner is
    whichever row the CSV happened to list first, which is not a decision. "Short is
    optional", so when the two sides cannot be told apart the rule is shown as published.
    Compared at `round(..., 3)` to match what `payload.num` stores, so the two
    implementations cannot disagree on a difference neither of them can display.
    """
    path = RESULTS_DIR / "edge_standard.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    col_tf = "timeframe" if "timeframe" in df.columns else "tf"
    g = df[(df["class"] == asset_class) & (df[col_tf] == timeframe)]
    if "edge_verdict" in g.columns:
        g = g[g["edge_verdict"].astype(str) != "unrankable"]
    if g.empty:
        return {}
    g = g.assign(_d=pd.to_numeric(g["edge_dsharpe"], errors="coerce").round(3),
                 _long=(g["side"].astype(str) == "long").astype(int))
    g = g.sort_values(["_d", "_long"], na_position="first").drop_duplicates(
        "rule", keep="last")
    return {str(r.rule): str(r.side) for r in g.itertuples()}


# LEVERAGE IS OFF. Set 2026-08-11, and it is a decision about what the study measures, not
# a tuning knob.
#
# Risk-matching by borrowing was doing more of the work than the rules were. `ibs` sits in
# cash ~54% of the time and is therefore less volatile than buy-and-hold, so `causal_k`
# levered every one of 614 us_stocks assets above 1x, median **1.45x** — and the money
# columns on the dashboard were reporting the levered result. `MNST` showed $36.7M against
# a buy-and-hold of $21.3M and looked like a win; unlevered it is **$4.0M**, which loses.
# The ranking was substantially a ranking of who could be sized up the most.
#
# It also contradicted the repo's own standard, which is explicit: "match it with CASH, not
# leverage... The fix is not to lever the rule up to the benchmark's volatility — that pays
# a financing spread and runs into the Reg-T 2x cap, which then does part of the ranking for
# you. Scale the benchmark down instead."
#
# So every position is now carried at exactly 1x: the rule trades its own signal with the
# money it has and sits in cash otherwise. `levered_net` still runs — it is what credits the
# bill rate on idle capital, which is not optional — but `k` is 1.0 everywhere, the borrow
# leg is identically zero, and `causal_wealth` now equals `wealth_1x` by construction.
#
# Restoring it means setting this True; the cap and the financing spread below are kept for
# that reason and are dead while it is False.
LEVERAGE_ENABLED = False

# Reg-T overnight maintenance for equities is 2:1. Crypto venues offer more, but a number
# nobody could hold through a weekend is not a benchmark; the same cap is applied to both
# and every row records whether it bound.
MAX_LEVERAGE = 2.0 if LEVERAGE_ENABLED else 1.0
# Interactive Brokers, benchmark + 1.5% for balances over $100k (quoted May 2026). Retail
# brokers outside IBKR typically charge 8-13% all-in, so this is the favourable end.
FINANCING_SPREAD = 0.015
VOL_TOL = 1e-4

FREE = {"key": "gross", "commission_bps": 0.0, "half_spread_bps": 0.0,
        "sell_fee_bps": 0.0, "borrow_annual": 0.0}

_RATES: pd.Series | None = None


def cash_rates() -> pd.Series:
    """Daily 3-month T-bill, as a decimal annual rate, forward-filled.

    FRED publishes '.' for market holidays; those become NaN and inherit the previous
    session's rate, which is what a cash balance would actually have earned.
    """
    global _RATES
    if _RATES is None:
        path = DATA_DIR / "rates" / "DTB3.csv"
        df = pd.read_csv(path, parse_dates=["observation_date"])
        s = pd.to_numeric(df["DTB3"], errors="coerce") / 100.0
        _RATES = s.set_axis(df["observation_date"]).ffill().dropna()
    return _RATES


def bar_rates(index: pd.DatetimeIndex, bpy: float,
              override: float | None = None) -> np.ndarray:
    """Per-bar cash return implied by the annual rate in force on that bar.

    `override` replaces the historical path with a constant. It exists for one specific
    question and it is not decoration: a rule that sits in cash half the time across
    1970-2026 collected Treasury bills at 15% in 1981, and an edge that depends on that
    is not an edge available in 2026. Running at 0% and at today's rate separates
    "the signal works" from "the 1980s paid well".
    """
    if override is not None:
        return np.full(len(index), (1.0 + override) ** (1.0 / bpy) - 1.0)
    r = cash_rates().reindex(index.normalize(), method="ffill").to_numpy()
    # Before 1954 or after the last publication there is nothing to inherit; 0% there is
    # the conservative choice and affects no bar in this project's windows.
    r = np.nan_to_num(r, nan=0.0)
    return (1.0 + r) ** (1.0 / bpy) - 1.0


VOL_WINDOW = 60          # bars of trailing volatility used to size causally


def causal_leverage(strat: np.ndarray, bench: np.ndarray, bpy: float) -> np.ndarray:
    """Per-bar leverage from TRAILING volatility only — the sizing a trader could run.

    Solving one leverage over the whole out-of-sample window and then scoring that same
    window is in-sample sizing: it uses the realised volatility of returns that had not
    happened yet. The effect is mild, because volatility is far more forecastable than
    return, but "mild look-ahead" is how this project has produced retractions before.

    Here both legs are trailing 60-bar estimates, shifted one bar, so the size carried
    into bar *t* is decided by data through *t-1*. Where the estimate is not yet
    available the size is 1.0 — unlevered, which cannot flatter the result.
    """
    if not LEVERAGE_ENABLED:
        # Flat 1x, not `clip(k, 0, 1)`. Clipping would still SHRINK any rule more volatile
        # than the benchmark, which is a sizing intervention in the other direction and
        # would quietly flatter the de-risking rules that already top these sheets.
        return np.ones_like(np.asarray(strat, dtype="float64"))

    def trail(x):
        s = pd.Series(x).rolling(VOL_WINDOW).std(ddof=1).shift(1)
        return (s * np.sqrt(bpy)).to_numpy()

    sv, bv = trail(strat), trail(bench)
    k = np.divide(bv, sv, out=np.ones_like(sv), where=np.isfinite(sv) & (sv > 1e-9))
    return np.clip(np.nan_to_num(k, nan=1.0), 0.0, MAX_LEVERAGE)


def levered_net(pos: np.ndarray, close: np.ndarray, fee: dict, bpy: float,
                rf_bar: np.ndarray, k) -> np.ndarray:
    """Net per-bar return of `k` times the position, with cash interest and financing.

    Reuses `vector.net_returns` for the price and cost legs so this shares the arithmetic
    the parity harness gates on, and adds only what that function does not model:

      * capital not in the market earns the bill rate;
      * capital borrowed to exceed 1.0 pays that rate plus the broker spread.

    Both fall out of the same term. With `held` the position actually carried into the
    bar, `(1 - held)` is positive when under-invested (interest received) and negative
    when levered (interest paid), so one expression covers both and cannot disagree with
    itself at the boundary.

    `k` may be a scalar or a per-bar array. The array form is the causal one and it costs
    more, correctly: re-sizing every bar is itself turnover, and `vector.net_returns`
    charges the whole position change including the part the sizing caused.
    """
    p = k * pos
    base = vector.net_returns(p, close, fee, bpy)
    held = np.empty_like(p)
    held[0] = 0.0
    held[1:] = p[:-1]
    spread_bar = (1.0 + FINANCING_SPREAD) ** (1.0 / bpy) - 1.0
    # Three legs, and they must be separate. Writing this as one `(1 - held) * rf` term
    # is wrong at BOTH ends once shorts and leverage are both in play:
    #
    #   * at held = -1 it credits 2x the bill rate, as though the short-sale proceeds were
    #     yours to lend out. A retail short earns essentially no rebate, so interest is
    #     capped at your own capital.
    #   * at held = 1.4 it must charge the base rate PLUS the spread on the borrowed 0.4.
    #     Capping the credit at zero without adding the base rate back into the financing
    #     leg silently lends you 40% of the book at 1.5% instead of ~6%.
    #
    # Short borrow itself is charged inside `vector.net_returns` via `borrow_annual`.
    cash_leg = np.clip(1.0 - held, 0.0, 1.0) * rf_bar
    borrow_leg = np.maximum(held - 1.0, 0.0) * (rf_bar + spread_bar)
    return base + cash_leg - borrow_leg


FILLS = ("close", "open", "close_lag")


def apply_fill(pos: np.ndarray, df, close: np.ndarray, fill: str):
    """Return `(pos, price)` for a fill convention. THE one definition; `portfolio_wf`
    imports this rather than keeping a second copy under the same name.

    The signal is always built on closes — that is what the rule is. This decides only
    which price the resulting position is paid on, and whether it is paid a bar later.

    ``close`` is the published convention and it contains a real look-ahead: the position
    from bar t earns close(t) -> close(t+1), i.e. it is entered at the very close whose
    high, low and close produced the signal. Nobody knows that print until it has printed.
    Measured on the 5m equity cache, recomputing IBS from the session up to 15:55 — an
    honest market-on-close order — agrees with the full-bar signal on 86% of symbol-days
    and costs ~0.11 of Sharpe, so `close` overstates but not enormously.

    ``open`` removes the look-ahead by filling at the next bar's open, and overshoots in
    the other direction: it also charges a full session of delay that a closing-auction
    order does not pay. On the same sample that second effect is worth ~0.27 of Sharpe,
    more than twice the look-ahead it is correcting.

    So the pair brackets the truth rather than locating it. **Quote the range.** Closing
    it needs a session-so-far signal, which needs intraday bars for the whole universe.

    ``close_lag`` is a diagnostic only — same delay as ``open`` but still on closes, so
    the two together separate a signal that decays within a bar from Blume & Stambaugh
    (1983) bid-ask bounce. Never quote it as performance.
    """
    if fill == "close":
        return pos, close
    if fill not in FILLS:
        raise ValueError(f"unknown fill {fill!r}; expected one of {FILLS}")
    if fill == "open":
        if "Open" not in getattr(df, "columns", ()):
            return None, None                      # caller skips the symbol
        price = df["Open"].to_numpy("float64")
    else:
        price = close
    lagged = np.empty_like(pos, dtype="float64")
    lagged[0] = 0.0
    lagged[1:] = pos[:-1]
    return lagged, price


# These four were defined here, and two of them were defined *differently* in
# `portfolio_wf.py` and `focus_wf.py` under the same names. They are now one
# implementation in `stockhunt.stats`, and this module's behaviour is pinned by the
# arguments rather than by which file happened to be imported — see that module's table.
def _ann_vol(r: np.ndarray, bpy: float) -> float:
    return stats.ann_vol(r, bpy)


def _cagr(r: np.ndarray, bpy: float) -> float:
    return stats.cagr(r, bpy)


def _max_dd(r: np.ndarray) -> float:
    # `dropna=False`: this sheet's published numbers were computed without the filter.
    return stats.max_drawdown(r, dropna=False)


def solve_leverage(pos, close, fee, bpy, rf_bar, mask, target, kind) -> tuple[float, bool]:
    """Smallest `k` whose realised vol (or drawdown) matches the benchmark's.

    Bisection rather than a closed form: `k` enters the price leg linearly but the cash
    and financing legs are piecewise, and drawdown is not analytic in `k` at all. Both
    targets are monotone in `k` over [0, MAX_LEVERAGE], which is all bisection needs.

    Returns 1.0 flat while `LEVERAGE_ENABLED` is False, so the `volmatch_*` and `ddmatch_*`
    columns collapse onto the unlevered result rather than disappearing. They are kept
    rather than deleted because the CSV schema is read in several places, and a column that
    silently changes meaning is worse than one that visibly stops varying.
    """
    if not LEVERAGE_ENABLED:
        return 1.0, False

    def measure(k):
        r = levered_net(pos, close, fee, bpy, rf_bar, k)[mask]
        r = r[np.isfinite(r)]
        if r.size < 3:
            return np.inf
        return _ann_vol(r, bpy) if kind == "vol" else abs(_max_dd(r))

    hi_val = measure(MAX_LEVERAGE)
    if hi_val <= target:
        return MAX_LEVERAGE, True          # cap binds before the risk is matched
    lo, hi = 0.0, MAX_LEVERAGE
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if measure(mid) < target:
            lo = mid
        else:
            hi = mid
        if hi - lo < VOL_TOL:
            break
    return (lo + hi) / 2.0, False


def trade_returns(pos: np.ndarray, r: np.ndarray) -> np.ndarray:
    """Compound return of every trade — a maximal run of constant non-zero target.

    The one segmentation. `profit_factor`, `trade_count` and `expectancy` all read it, so
    the three cannot drift into describing different units, and `curves.py` counts a trade
    the same way. Bar-level accounting would score a rule that sat long through a rising
    year as several hundred separate "wins" and collapse every long-biased rule onto the
    same number.

    Pass the position **held**, not the target. Segmenting on the unshifted target charges
    each bar's return to the wrong state, and on a rule that flips daily that inverts the
    result rather than nudging it: `ibs` scored a profit factor of 0.22 while compounding
    +27%/yr. Correct is 1.57.

    Compounding goes through a cumulative sum of log returns, because a running product
    over 11,500 bars underflows to zero long before the window ends. -1 is total loss and
    log1p is undefined at or below it, so the clip is the floor of what can happen.

    Vectorised deliberately: the obvious bar-by-bar walk is called once per
    (rule, symbol, side) across six sheets — order 1e9 iterations — which turned a
    two-minute run into one that had not finished a single sheet in nine.
    """
    p = np.nan_to_num(np.asarray(pos, dtype=float), nan=0.0)
    n = p.size
    if n == 0:
        return np.empty(0, dtype=float)
    lr = np.log1p(np.clip(np.asarray(r, dtype=float), -0.999999, None))
    lr[~np.isfinite(lr)] = 0.0
    csum = np.concatenate(([0.0], np.cumsum(lr)))
    edges = np.flatnonzero(np.diff(p) != 0) + 1
    starts = np.concatenate(([0], edges))
    ends = np.concatenate((edges, [n]))          # exclusive
    held = p[starts] != 0
    if not held.any():
        return np.empty(0, dtype=float)
    return np.expm1(csum[ends[held]] - csum[starts[held]])


def expectancy(pos: np.ndarray, r: np.ndarray) -> dict:
    """What one trade is worth on average, and the pieces that make it up.

    `expectancy` is the mean per-trade return, which is identically
    `win_rate * avg_win - (1 - win_rate) * avg_loss` — the textbook form. Reporting the
    mean directly avoids the rounding drift of reassembling it from three rounded parts.

    `expectancy_r` divides by the average loss, giving the R-multiple: how much is won per
    unit of what a losing trade costs. It is the scale-free version and the one that
    survives comparison across assets of different volatility. Undefined, and returned as
    NaN, when a rule never lost — no denominator exists, and a stand-in constant would
    sort to the top of any table it appeared in as though it were a measurement.

    Note this is expectancy per TRADE, not per bar and not per year. A rule with a fine
    expectancy that trades twice a decade is not thereby a good rule, which is why
    `trades_per_asset` is reported beside it and never dropped.
    """
    t = trade_returns(pos, r)
    nan = float("nan")
    if t.size == 0:
        return {"expectancy": nan, "expectancy_r": nan, "win_rate": nan,
                "avg_win": nan, "avg_loss": nan}
    wins, losses = t[t > 0], t[t < 0]
    avg_win = float(wins.mean()) if wins.size else 0.0
    avg_loss = float(-losses.mean()) if losses.size else 0.0     # positive magnitude
    exp_trade = float(t.mean())
    return {"expectancy": exp_trade,
            "expectancy_r": exp_trade / avg_loss if avg_loss > 0 else nan,
            "win_rate": float(wins.size / t.size),
            "avg_win": avg_win, "avg_loss": avg_loss}


def roe_annual(pos: np.ndarray, r: np.ndarray, bpy: float) -> float:
    """Annualised return on capital **while it was actually deployed**.

    ROI answers "what did the account earn"; this answers "what did the money earn when it
    was at work". The two differ by exactly the idle time, and separating them is the point
    on this project: `corr(IR, long_frac)` is 0.881 on daily equities, so a leaderboard of
    account-level returns is substantially a ranking of who stayed invested longest. A rule
    holding 46% of the time and a buy-and-hold holding 100% are not comparable on ROI and
    are comparable on ROE.

    Compounded over held bars only, then annualised by how many YEARS of held bars there
    were — not by calendar years, which would silently re-introduce the idle time this
    exists to remove.
    """
    p = np.nan_to_num(np.asarray(pos, dtype=float), nan=0.0)
    held = p != 0
    if not held.any() or bpy <= 0:
        return float("nan")
    lr = np.log1p(np.clip(np.asarray(r, dtype=float), -0.999999, None))[held]
    lr = lr[np.isfinite(lr)]
    yrs_held = lr.size / bpy
    if yrs_held <= 0:
        return float("nan")
    return float(np.expm1(lr.sum() / yrs_held))


def profit_factor(pos: np.ndarray, r: np.ndarray) -> float:
    """Gross winnings / gross losses, counted **per trade**, not per bar.

    Returns NaN, never a sentinel, when the rule never lost: a strategy with no losing
    trade has no denominator, and a large stand-in would sort to the top of any table it
    appeared in as though it were a measurement.
    """
    trades = trade_returns(pos, r)
    if trades.size == 0:
        return float("nan")
    gain = float(trades[trades > 0].sum())
    loss = float(-trades[trades < 0].sum())
    return gain / loss if loss > 0 else float("nan")


def trade_count(pos: np.ndarray) -> int:
    """How many positions the rule opened — maximal runs of constant non-zero target.

    Counted per asset, because the sheet aggregates hundreds of symbols and "10,024 trades"
    across all of them is a number nobody can size against a holding period.
    """
    p = np.nan_to_num(np.asarray(pos, dtype=float), nan=0.0)
    if p.size == 0:
        return 0
    starts = np.concatenate(([0], np.flatnonzero(np.diff(p) != 0) + 1))
    return int((p[starts] != 0).sum())


def wealth(r: np.ndarray) -> float:
    r = r[np.isfinite(r)]
    return float(CAPITAL * np.prod(1.0 + r))


def sharpe(r: np.ndarray, rf: np.ndarray, bpy: float) -> float:
    # `min_obs=3`, not the 30 `portfolio_wf` uses. Per-fold cells here legitimately run
    # short and raising the floor would turn scored cells into NaN.
    return stats.sharpe(r, rf, bpy, min_obs=3)


_CURVES: dict[tuple, tuple] = {}


def control_curve(df, close, bpy, fee, rf, mask, symbol, side, sheet="") -> tuple:
    """Sharpe against time-in-market for the six signal-free controls, on this asset.

    Same idea as `strat_wf`'s exposure control curve, restated in Sharpe so it composes
    with the rest of this stage. Against a rising benchmark, being out of the market costs
    Sharpe whether or not the rule knows anything, so a strategy is only informative if it
    sits above this line **at its own exposure**. Random controls are averaged over
    `RANDOM_DRAWS` draws — one draw carries as much sampling noise as the thing it is
    meant to calibrate.

    Under `side="short"` the controls are mapped through `to_long_short` too, so a
    long/short rule is compared against random long/short, not against random long/flat.

    Memoised on `(sheet, symbol, side)`: the curve depends on the asset and the side, not
    on the rule being scored, so computing it per rule would rebuild 6 controls x 12
    random draws x 20 assets for every one of 31 rules — about 97% wasted work.
    """
    ck = (sheet, symbol, side)
    if ck in _CURVES:
        return _CURVES[ck]
    xs, ys = [], []
    for ctrl in (BASELINE, *CONTROLS):
        n = RANDOM_DRAWS if ctrl.startswith("RANDOM_") else 1
        shp, lf = [], []
        for d in range(n):
            p = build(ctrl, df, close, bpy, symbol, d)
            if p is None:
                continue
            if side == "short":
                p = to_long_short(p)
            r = levered_net(p, close, fee, bpy, rf, 1.0)
            shp.append(sharpe(r[mask], rf[mask], bpy))
            lf.append(float(np.mean(p[mask] > 0)))
        shp = [v for v in shp if np.isfinite(v)]
        if not shp or not lf:
            continue
        xs.append(float(np.mean(lf)))
        ys.append(float(np.mean(shp)))
    if len(xs) < 2:
        _CURVES[ck] = (None, None)
        return _CURVES[ck]
    order = np.argsort(xs)
    _CURVES[ck] = (np.array(xs)[order], np.array(ys)[order])
    return _CURVES[ck]


PAIR_SEP, OP_SEP = "~", "|"


def leaderboard_universe(asset_class: str, timeframe: str) -> list[str]:
    """Exactly the rules the backtest leaderboard shows: TA-Lib singles AND pairs.

    The standard has to cover the same population as the page, or the page cannot be
    ranked on it. Reading the rule names straight out of `wf_summary_*` and
    `cwf_summary_*` guarantees that rather than approximating it — and it means a rule
    added to the sweep shows up here without a second list to maintain.

    `IS#1` rows are dropped: they are the act of *choosing* a rule scored as a strategy,
    not a rule, and a leaderboard sorted by a per-rule metric is the wrong place for them.
    """
    names: list[str] = []
    scen = headline_key(asset_class, timeframe)
    for prefix in ("wf_summary", "cwf_summary"):
        p = RESULTS_DIR / f"{prefix}_{asset_class}_{timeframe}.csv"
        if not p.exists():
            continue
        df = pd.read_csv(p)
        keep = df[(df.scenario == scen) & df.rankable & ~df.is_baseline]
        keep = keep[~keep["rule"].astype(str).str.startswith("IS#1")]
        names += [str(r) for r in keep["rule"].unique()]
    # Catalog rules too, so the published strategies stay comparable on one axis.
    names += [n for n in CATALOG if n not in names]
    return list(dict.fromkeys(names))


def resolve_position(name: str, df: pd.DataFrame, close: np.ndarray, bpy: float,
                     symbol: str, asset_class: str, timeframe: str) -> np.ndarray | None:
    """One name -> one position series, whichever of the three kinds of name it is.

    Three populations meet on this leaderboard and each builds differently: the published
    catalog (`strategies.catalog.build`), the 231 TA-Lib singles (`signals.position_for`)
    and the pairs, whose name `A~B|op` carries its own recipe. Parsing the pair out of its
    name rather than threading `leg_a`/`leg_b` through keeps this in step with whatever the
    sweep wrote, which is the same reason `leaderboard_universe` reads the CSVs.
    """
    if name in CATALOG or name == BASELINE or name in CONTROLS:
        return build(name, df, close, bpy, symbol)
    if PAIR_SEP in name and OP_SEP in name:
        legs, op = name.rsplit(OP_SEP, 1)
        a, _, b = legs.partition(PAIR_SEP)
        pa = signals.position_for(a, df, asset_class, timeframe, BASELINE)
        pb = signals.position_for(b, df, asset_class, timeframe, BASELINE)
        if pa is None or pb is None or op not in signals.OPERATORS:
            return None
        return signals.combine(pa, pb, op)
    return signals.position_for(name, df, asset_class, timeframe, BASELINE)



# ---------------------------------------------------------------- parallelism
#
# Each stage in this repo is single-threaded and uses exactly one of six physical cores,
# so most of the machine sits idle regardless of what else is running. Measured
# 2026-08-10: RAM was never the constraint (7.3 GB free, zero hard page faults) and CPU
# sat at ~55% — the limit was that one process cannot use more than one core.
#
# Rules are the grain. They are independent of each other, and they SHARE the expensive
# setup: 704 parquet loads, the fold masks, the union mask. Sending a rule name to a
# worker costs nothing; sending `data` would cost more than the work saved, because
# Windows spawns rather than forks and every argument is pickled per task.
#
# So each worker builds the shared context ONCE in its initialiser and then receives only
# strings. `_WORKER` is module-level because a spawned interpreter re-imports this module
# and an initialiser cannot return a value.

_WORKER: dict = {}


def _worker_count(n_tasks: int) -> int:
    """Workers to use: never more than tasks, never the whole machine."""
    import os
    if n_tasks <= 1:
        return 1
    # Leave two cores so the box stays usable and so another project's jobs are not
    # starved — this machine has been shared all session.
    return max(1, min(n_tasks, (os.cpu_count() or 2) - 2))


def _init_worker(asset_class: str, timeframe: str, side: str,
                 cash_override: float | None, fill: str = "close") -> None:
    """Rebuild the shared context inside a spawned worker, once."""
    global _WORKER
    data = td_loader.load(asset_class, timeframe)
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
    start = min(df.index[0] for df in data.values())
    end = max(df.index[-1] for df in data.values())
    folds = wfmod.generate_folds(start, end)
    masks = {s: wfmod.fold_masks(df.index, folds) for s, df in data.items()}
    union = {s: np.logical_or.reduce([m[1] for m in ms if m is not None])
             for s, ms in masks.items() if any(m is not None for m in ms)}
    _WORKER = {"data": data, "masks": masks, "union": union, "folds": folds,
               "fee": scenario(asset_class, HEADLINE_SCENARIO[asset_class]),
               "asset_class": asset_class, "timeframe": timeframe, "side": side,
               "cash_override": cash_override, "fill": fill}


def _score_rule_worker(name: str) -> list:
    return _score_rule(name, _WORKER)


def _run_rules_parallel(rules: list, ctx: dict, workers: int) -> list:
    """Map rules across processes; fall back to serial if the pool cannot start.

    A failure here must not be silent. If the pool dies the answer would otherwise be a
    short results frame that looks like "those rules produced nothing" — the exact shape
    of every bug this pipeline has hidden behind today.
    """
    from concurrent.futures import ProcessPoolExecutor
    rows = []
    try:
        with ProcessPoolExecutor(
                max_workers=workers, initializer=_init_worker,
                initargs=(ctx["asset_class"], ctx["timeframe"], ctx["side"],
                          ctx["cash_override"], ctx.get("fill", "close"))) as ex:
            for out in ex.map(_score_rule_worker, rules, chunksize=1):
                rows.extend(out)
    except Exception as exc:
        print(f"  parallel pool failed ({type(exc).__name__}: {exc}); "
              f"falling back to serial")
        rows = []
        for name in rules:
            rows.extend(_score_rule(name, ctx))
    return rows


def run_sheet(asset_class: str, timeframe: str, rules: list[str],
              cash_override: float | None = None,
              side: str = "long", fill: str = "close") -> pd.DataFrame:
    data = td_loader.load(asset_class, timeframe)
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
    if not data:
        return pd.DataFrame()
    start = min(df.index[0] for df in data.values())
    end = max(df.index[-1] for df in data.values())
    folds = wfmod.generate_folds(start, end)
    if len(folds) < WF_MIN_FOLDS:
        return pd.DataFrame()
    masks = {s: wfmod.fold_masks(df.index, folds) for s, df in data.items()}
    union = {s: np.logical_or.reduce([m[1] for m in ms if m is not None])
             for s, ms in masks.items() if any(m is not None for m in ms)}
    fee = scenario(asset_class, HEADLINE_SCENARIO[asset_class])

    ctx = {"data": data, "masks": masks, "union": union, "folds": folds, "fee": fee,
           "asset_class": asset_class, "timeframe": timeframe, "side": side,
           "cash_override": cash_override, "fill": fill}

    workers = _worker_count(len(rules))
    if workers > 1:
        rows = _run_rules_parallel(rules, ctx, workers)
    else:
        rows = []
        for name in rules:
            rows.extend(_score_rule(name, ctx))
    # Deterministic order regardless of completion order — a parallel map returns
    # whenever each task happens to finish, and an unsorted frame would make two
    # identical runs differ by row order alone.
    rows.sort(key=lambda r: (str(r.get("rule")), str(r.get("symbol")), str(r.get("side"))))
    return pd.DataFrame(rows)


def _score_rule(name: str, ctx: dict) -> list:
    """Every (symbol) record for one rule. The unit of parallel work.

    Rules are independent of each other and share the expensive setup — the loaded bars,
    the fold masks, the union mask and the fee schedule — so the rule is the right grain:
    one task carries a string, not 704 DataFrames.
    """
    data, masks, union = ctx["data"], ctx["masks"], ctx["union"]
    folds, fee = ctx["folds"], ctx["fee"]
    asset_class, timeframe = ctx["asset_class"], ctx["timeframe"]
    side, cash_override = ctx["side"], ctx["cash_override"]
    fill = ctx.get("fill", "close")

    rows = []
    if True:
        spec = CATALOG.get(name)
        # Catalog entries can be undefined on a class (preholiday on crypto). Non-catalog
        # names have no such declaration and are filtered by `resolve_position` returning
        # None instead.
        if spec is not None and spec.classes and asset_class not in spec.classes:
            return rows
        for symbol, df in data.items():
            if symbol not in union:
                continue
            close = df["Close"].to_numpy("float64")
            bpy = vector.bars_per_year(df.index)
            pos = resolve_position(name, df, close, bpy, symbol,
                                   asset_class, timeframe)
            if pos is None:
                continue
            if side == "short":
                pos = to_long_short(pos)
            # The signal was built on closes above — that is the rule. `close` is rebound
            # here to whichever leg the money is earned on, so every pricing call below
            # (base, causal, the cost-headroom ladder, the leverage solve, and BOTH
            # signal-free controls) follows the convention without eight separate edits
            # and cannot be left half-converted. `pos` moves with it, so `long_frac`,
            # `exposure`, `turnover` and the trade slicing all describe what was actually
            # held rather than what was merely signalled.
            pos, close = apply_fill(pos, df, close, fill)
            if pos is None:
                continue
            u = union[symbol]
            rf = bar_rates(df.index, bpy, cash_override)
            yrs = u.sum() / bpy

            # Full-length series for the trailing-vol sizing (it needs the warmup that
            # sits before the first out-of-sample bar), masked copies for scoring.
            bench_full = vector.net_returns(np.ones(len(df)), close, FREE, bpy)
            base_full = levered_net(pos, close, fee, bpy, rf, 1.0)
            bench, base = bench_full[u], base_full[u]
            b_vol, b_dd = _ann_vol(bench, bpy), abs(_max_dd(bench))
            rf_u = rf[u]
            b_sharpe = float(np.mean(bench - rf_u) / np.std(bench - rf_u, ddof=1)
                             * np.sqrt(bpy))
            s_sharpe = float(np.mean(base - rf_u) / np.std(base - rf_u, ddof=1)
                             * np.sqrt(bpy))

            rec = {
                "class": asset_class, "tf": timeframe, "rule": name, "symbol": symbol,
                "side": side, "years": yrs,
                "long_frac": float(np.mean(pos[u] > 0)),
                "short_frac": float(np.mean(pos[u] < 0)),
                # Mean absolute weight — the quantity the exposure precondition tests.
                # A rule can be "long 0%" and still trade (a short-only rule), so a
                # long-fraction floor would reject the wrong things.
                "exposure": float(np.mean(np.abs(pos[u]))),
                "turnover_yr": float(np.abs(np.diff(pos[u])).sum() / yrs),
                "sharpe": s_sharpe, "bench_sharpe": b_sharpe,
                "sharpe_edge": s_sharpe - b_sharpe,
                "vol_1x": _ann_vol(base, bpy), "bench_vol": b_vol,
                "dd_1x": _max_dd(base), "bench_dd": -b_dd,
                "wealth_1x": wealth(base), "bench_wealth": wealth(bench),
            }
            # The honest one: sized bar by bar from trailing volatility only.
            k_causal = causal_leverage(base_full, bench_full, bpy)
            r_c_full = levered_net(pos, close, fee, bpy, rf, k_causal)
            r_c = r_c_full[u]
            w_c = wealth(r_c)

            # Cost headroom, measured on the RISK-MATCHED result. `metrics.cost_headroom`
            # is computed from IR and returns 0.0 whenever gross IR <= 0, so for every
            # rule that loses to buy-and-hold unsized it reports 0.00x — which reads like
            # a measurement and is really just "undefined". Asking instead how many
            # multiples of the real fee schedule the *equal-risk wealth advantage*
            # survives gives a number that exists whenever there is anything to erode.
            head = 0.0
            for mult in (0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0):
                f2 = dict(fee)
                for c in ("commission_bps", "half_spread_bps", "sell_fee_bps",
                          "borrow_annual"):
                    f2[c] = fee.get(c, 0.0) * mult
                if wealth(levered_net(pos, close, f2, bpy, rf, k_causal)[u]) > wealth(bench):
                    head = mult
                else:
                    break
            rec_head = head
            # The position *held* over each bar, which is the one that earned that bar's
            # return: `vector.net_returns` does `held[1:] = position[:-1]`. Shifted on the
            # full series before masking, because masking breaks bar adjacency. Everything
            # that slices returns by position run — trade count, profit factor — uses this
            # and not `pos`, or each trade is attributed its neighbour's return.
            _held = np.concatenate(([0.0], pos[:-1]))[u]
            rec.update({
                "causal_k": float(np.median(k_causal[u])),
                "causal_wealth": w_c,
                "causal_cagr": (w_c / CAPITAL) ** (1 / yrs) - 1 if yrs > 0 else np.nan,
                "causal_vol": _ann_vol(r_c, bpy), "causal_dd": _max_dd(r_c),
                # Both on the risk-matched series, so they sit on the same basis as the
                # wealth figures rather than describing an unsized rule nobody traded.
                # The benchmark is always in the market, so its "trade" is the whole
                # window and its profit factor is undefined — reported as such, not faked.
                # Segmenting these on the unshifted `pos` attributes each bar's return to
                # the wrong state, and on a rule that flips daily that is not a rounding
                # error — it inverts the result. `ibs` scored a profit factor of 0.22 while
                # compounding +27%/yr, because the held bars were being charged the flat
                # bars' returns and vice versa. Correct is 1.57.
                "causal_pf": profit_factor(_held, r_c),
                "causal_trades": trade_count(_held),
                # Expectancy, ROI and ROE ride on the SAME risk-matched series as the
                # wealth columns. Computing them on the unsized rule would describe a
                # strategy nobody traded and put them on a different basis from every
                # other number in the row.
                **{f"causal_{k}": v for k, v in expectancy(_held, r_c).items()},
                "causal_roi": w_c / CAPITAL - 1.0,
                "causal_roe": roe_annual(_held, r_c, bpy),
                "causal_sharpe": float(np.mean(r_c - rf_u)
                                       / np.std(r_c - rf_u, ddof=1) * np.sqrt(bpy)),
                "causal_beats": w_c > rec["bench_wealth"],
                "cost_headroom": rec_head,
            })

            for kind, target, tag in (("vol", b_vol, "volmatch"),
                                      ("dd", b_dd, "ddmatch")):
                k, capped = solve_leverage(pos, close, fee, bpy, rf, u, target, kind)
                r = levered_net(pos, close, fee, bpy, rf, k)[u]
                w = wealth(r)
                rec[f"{tag}_k"] = k
                rec[f"{tag}_capped"] = capped
                rec[f"{tag}_wealth"] = w
                rec[f"{tag}_cagr"] = (w / CAPITAL) ** (1 / yrs) - 1 if yrs > 0 else np.nan
                rec[f"{tag}_vol"] = _ann_vol(r, bpy)
                rec[f"{tag}_dd"] = _max_dd(r)
                rec[f"{tag}_beats"] = w > rec["bench_wealth"]
            rec["bench_cagr"] = ((rec["bench_wealth"] / CAPITAL) ** (1 / yrs) - 1
                                 if yrs > 0 else np.nan)

            # --- criterion R: beats a coin flip at its own time-in-market
            cx, cy = control_curve(df, close, bpy, fee, rf, u, symbol, side,
                                   f"{asset_class}_{timeframe}")
            rand_sharpe = (float(np.interp(rec["long_frac"], cx, cy,
                                           left=cy[0], right=cy[-1]))
                           if cx is not None else np.nan)
            rec["rand_sharpe"] = rand_sharpe
            rec["edge_vs_random"] = s_sharpe - rand_sharpe

            # --- criterion C: beats simply owning less, with no signal at all.
            # Weight is the rule's own MEAN NET position. For long/flat that is its average
            # exposure; for long/short it is close to zero, which makes this control weak
            # rather than wrong on that side — the random control carries the load there.
            cw = float(np.mean(pos[u]))
            cr = levered_net(np.full(len(df), cw), close, fee, bpy, rf, 1.0)[u]
            c_cagr, c_dd = _cagr(cr, bpy), _max_dd(cr)
            s_dd = rec["dd_1x"]
            rec["const_weight"] = cw
            rec["const_mar"] = c_cagr / abs(c_dd) if c_dd else np.nan
            rec["mar_1x"] = _cagr(base, bpy) / abs(s_dd) if s_dd else np.nan
            rec["edge_vs_constant"] = rec["mar_1x"] - rec["const_mar"]

            # --- criteria S and T: per-fold delta-Sharpe, the quantity the t-test needs
            fs = []
            for f, m in zip(folds, masks[symbol]):
                if m is None or m[1].sum() < 60:
                    continue
                o = m[1]
                a = sharpe(r_c_full[o], rf[o], bpy)
                b = sharpe(bench_full[o], rf[o], bpy)
                if np.isfinite(a) and np.isfinite(b):
                    fs.append((f.index, a - b))
            rec["fold_edges"] = ";".join(f"{i}:{v:.6f}" for i, v in fs)
            rec["n_folds_scored"] = len(fs)
            # The sheet's fold count, not this asset's. `fold_coverage` divides by it, and
            # dividing by a per-asset max would let a rule that is sparse on EVERY asset
            # look fully covered — which is exactly the CDLKICKING failure mode.
            rec["n_folds_sheet"] = len(folds)

            rec["edge_wealth"] = w_c - rec["bench_wealth"]
            rec["edge_headroom"] = rec_head
            rows.append(rec)
    return rows


def score_standard(t: pd.DataFrame) -> pd.DataFrame:
    """One row per (sheet, side, rule), scored against `config.EDGE_STANDARD`.

    The per-fold delta-Sharpes are pooled the right way round: **average across assets
    within a fold first, then take the t across folds.** Doing it the other way — t across
    all (asset, fold) cells — would treat 20 co-moving mega-caps in the same year as 20
    independent observations and inflate the statistic by roughly sqrt(20).
    """
    from config import EDGE_STANDARD
    import metrics as M

    out = []
    for (cls_, tf, side, rule), g in t.groupby(["class", "tf", "side", "rule"]):
        # fold index -> list of per-asset edges
        pooled: dict[int, list[float]] = {}
        for s in g["fold_edges"].dropna():
            for piece in str(s).split(";"):
                if not piece:
                    continue
                i, v = piece.split(":")
                pooled.setdefault(int(i), []).append(float(v))
        per_fold = np.array([np.mean(v) for _, v in sorted(pooled.items())])
        n_f = per_fold.size
        sd = float(np.std(per_fold, ddof=1)) if n_f > 2 else np.nan
        mean_edge = float(np.mean(per_fold)) if n_f else np.nan
        tstat = (mean_edge / (sd / np.sqrt(n_f))
                 if n_f > 2 and np.isfinite(sd) and sd > 0 else np.nan)

        row = {
            "class": cls_, "tf": tf, "side": side, "rule": rule,
            "n_assets": int(g.symbol.nunique()), "years": float(g.years.median()),
            "edge_dsharpe": mean_edge, "fold_sd": sd, "edge_t": tstat,
            "n_folds_scored": n_f,
            "exposure": float(g.exposure.median()),
            "fold_coverage": (n_f / float(g.n_folds_sheet.max())
                              if float(g.n_folds_sheet.max()) > 0 else 0.0),
            "edge_vs_random": float(g.edge_vs_random.median()),
            "edge_vs_constant": float(g.edge_vs_constant.median()),
            "edge_wealth": float(g.edge_wealth.median()),
            "edge_headroom": float(g.edge_headroom.median()),
            # report-only
            "sharpe": float(g.sharpe.median()),
            "bench_sharpe": float(g.bench_sharpe.median()),
            # Report-only, and on the risk-matched series like the wealth columns. Median
            # across assets for the same reason every other figure here is: one blown-up
            # name should not set the number for the sheet.
            "max_dd": float(g.causal_dd.median()),
            "bench_max_dd": float(g.bench_dd.median()),
            "profit_factor": float(g.causal_pf.median()),
            "trades_per_asset": float(g.causal_trades.median()),
            # Median across assets, like every other aggregate here. A mean would let one
            # asset write the answer -- the failure `excess_return_pct` already walked into
            # by averaging per-asset totals when SOXL compounds 230x and SPY 6.4x.
            "expectancy": float(g.causal_expectancy.median()),
            "win_rate": float(g.causal_win_rate.median()),
            "avg_win": float(g.causal_avg_win.median()),
            "avg_loss": float(g.causal_avg_loss.median()),
            # Rebuilt from the sheet's own aggregates, NOT as the median of per-asset
            # ratios. `Series.median` skips NaN, and `expectancy_r` is NaN exactly where a
            # rule never lost — so the median of the ratio silently describes only the
            # assets that DID lose. Buy-and-hold showed `expectancy_r = -1.000` beside
            # `win_rate = 1.000` for that reason: the winning assets have no ratio, so the
            # aggregate was taken over the losers alone. Dividing the reported expectancy
            # by the reported average loss keeps the printed row self-consistent.
            "expectancy_r": (float(g.causal_expectancy.median()
                                   / g.causal_avg_loss.median())
                             if float(g.causal_avg_loss.median()) > 0 else float("nan")),
            "roi": float(g.causal_roi.median()),
            "roi_ann": float(g.causal_cagr.median()),
            "roe_ann": float(g.causal_roe.median()),
            "bench_roi": float((g.bench_wealth / CAPITAL - 1.0).median()),
            # The MEAN, deliberately, where `wealth` above is the median. Each asset is
            # funded with its own CAPITAL and traded independently, so this is what a
            # typical name returned — and the gap between it and the median is the
            # skew: a mean far above the median is one asset carrying the sheet, which
            # is exactly the failure `breadth` exists to catch.
            "avg_pnl_per_asset": float((g.causal_wealth - CAPITAL).mean()),
            "avg_bench_pnl_per_asset": float((g.bench_wealth - CAPITAL).mean()),
            "wealth": float(g.causal_wealth.median()),
            "bench_wealth": float(g.bench_wealth.median()),
            "breadth": float((g.sharpe_edge > 0).mean()),
            "long_frac": float(g.long_frac.median()),
            "short_frac": float(g.short_frac.median()),
            "turnover_yr": float(g.turnover_yr.median()),
            "max_leverage": float(g.causal_k.max()),
            # The bar the observed edge has to clear given how many things were tried.
            "noise_ceiling": (float(sd / np.sqrt(n_f)
                                    * NormalDist().inv_cdf(1 - 1 / (_n_trials(t) + 1)))
                              if n_f > 2 and np.isfinite(sd) else np.nan),
        }
        # How many candidates this sheet searched. Without it `apply_edge_standard`
        # applies a single-test t bar to the winner of a large search, which is exactly
        # how one row in 1,974 came back PASS at t=3.014 against a corrected bar of 4.21.
        row["n_trials"] = _n_trials(t)
        row.update(M.apply_edge_standard(row))
        row["above_ceiling"] = bool(np.isfinite(row["noise_ceiling"])
                                    and mean_edge > row["noise_ceiling"])
        out.append(row)
    cols = [c["key"] for c in EDGE_STANDARD]
    df = pd.DataFrame(out)
    return df.sort_values(["class", "tf", "edge_passed", "edge_dsharpe"],
                          ascending=[True, True, False, False])


def report_standard(s: pd.DataFrame) -> None:
    from config import EDGE_STANDARD
    letters = "".join(c["letter"] for c in EDGE_STANDARD)
    print(f"\n{'#' * 118}\nTHE EDGE STANDARD — {letters} = "
          + ", ".join(f"{c['letter']}:{c['label'].split(',')[0]}" for c in EDGE_STANDARD)
          + f"\n{'#' * 118}")
    for (cls_, tf), sheet in s.groupby(["class", "tf"]):
        pw = sheet.edge_powered.iloc[0]
        print(f"\n=== {cls_} {tf} — {sheet.years.iloc[0]:.1f}y, "
              f"{int(sheet.n_folds_scored.max())} folds"
              + ("" if pw else "  [UNDERPOWERED: verdicts are 'cannot tell', not 'no']")
              + " ===")
        print(f"  {'rule':<17}{'side':<7}{'dSharpe':>9}{'t':>7}{'vsRand':>8}{'vsConst':>9}"
              f"{'$ vs B&H':>14}{'fees':>7}  {letters}  verdict")
        for r in sheet.sort_values(["edge_passed", "edge_t"],
                                  ascending=[False, False]).itertuples():
            flags = "".join(
                c["letter"] if getattr(r, f"edge_gate_{c['key']}") else "."
                for c in EDGE_STANDARD)
            print(f"  {r.rule:<17}{r.side:<7}{r.edge_dsharpe:>+9.3f}{r.edge_t:>+7.2f}"
                  f"{r.edge_vs_random:>+8.3f}{r.edge_vs_constant:>+9.3f}"
                  f"${r.edge_wealth:>13,.0f}{r.edge_headroom:>6.1f}x  {flags}  "
                  f"{r.edge_verdict}")
        n_pass = int((sheet.edge_verdict == "PASS").sum())
        print(f"  -> {n_pass} of {len(sheet)} PASS all "
              f"{int(sheet.edge_n.iloc[0])} criteria")


def report(t: pd.DataFrame) -> None:
    for (cls_, tf, side), sheet in t.groupby(["class", "tf", "side"]):
        yrs = sheet.years.median()
        n_assets = sheet.symbol.nunique()
        b = sheet.groupby("symbol").first()
        # MEDIAN wealth, never the mean. Across 20 mega-caps over 41 years terminal
        # wealth spans three orders of magnitude, so an average is written by whichever
        # name compounded hardest and says nothing about the typical outcome. Same
        # mistake as averaging per-asset total returns when SOXL does 230x and SPY 6x.
        label = ("long/flat as published" if side == "long"
                 else "LONG/SHORT — 'stay out' becomes 'sell it'")
        print(f"\n{'=' * 112}\n{cls_} {tf}  [{label}]   {n_assets} assets, {yrs:.1f}y   "
              f"buy & hold: Sharpe {b.bench_sharpe.median():.3f}, "
              f"vol {b.bench_vol.median():.1%}, maxDD {b.bench_dd.median():.1%}, "
              f"median ${b.bench_wealth.median():,.0f} from ${CAPITAL:,.0f}\n{'=' * 112}")
        g = (sheet.groupby("rule")
             .agg(sharpe=("sharpe", "median"), edge=("sharpe_edge", "median"),
                  beat_sh=("sharpe_edge", lambda x: float((x > 0).mean())),
                  k=("volmatch_k", "median"), capped=("volmatch_capped", "mean"),
                  w=("volmatch_wealth", "median"), bw=("bench_wealth", "median"),
                  beats=("volmatch_beats", lambda x: float(x.mean())),
                  ddw=("ddmatch_wealth", "median"),
                  ddbeats=("ddmatch_beats", lambda x: float(x.mean())),
                  cw=("causal_wealth", "median"),
                  cbeats=("causal_beats", lambda x: float(x.mean())),
                  ck=("causal_k", "median"), cvol=("causal_vol", "median"),
                  vol1=("vol_1x", "median"), lf=("long_frac", "median"),
                  # NOT `head=`: `Series.head` is a method, so `r.head` resolves to the
                  # bound method and formatting it raises. Same class of bug as naming a
                  # column `transform`.
                  feeok=("cost_headroom", "median"), tn=("turnover_yr", "median"))
             .sort_values("edge", ascending=False))
        print(f"  {'strategy':<17}{'Sharpe':>8}{'vs B&H':>8}{'assets':>8}"
              f"{'lever':>7}{'$ CAUSAL-sized':>16}{'beats':>7}"
              f"{'fees ok':>9}{'turns/yr':>10}   long")
        for name, r in g.iterrows():
            cap = "*" if r.capped > 0.5 else " "
            print(f"  {name:<17}{r.sharpe:>+8.3f}{r.edge:>+8.3f}"
                  f"{r.beat_sh:>7.0%} {r.k:>6.2f}{cap}${r.cw:>15,.0f}{r.cbeats:>7.0%}"
                  f"{r.feeok:>8.1f}x{r.tn:>10.0f}   {r.lf:>4.0%}")
        print(f"  {'-' * 110}")
        print(f"  {'BUY & HOLD':<17}{b.bench_sharpe.median():>+8.3f}{'':>8}{'':>8}"
              f"{1.0:>6.2f} ${b.bench_wealth.median():>14,.0f}"
              f"{'':>7}${b.bench_wealth.median():>15,.0f}{'':>7}   100%")
        n_beat = int((g.beats > 0.5).sum())
        print(f"\n  strategies with a higher Sharpe than buy-and-hold: "
              f"{int((g.edge > 0).sum())} of {len(g)}")
        print(f"  strategies that end with more money at EQUAL VOLATILITY on a "
              f"majority of assets: {n_beat} of {len(g)}")
        print(f"  strategies that end with more money at EQUAL DRAWDOWN on a "
              f"majority of assets: {int((g.ddbeats > 0.5).sum())} of {len(g)}")
        print(f"  ...and with CAUSAL sizing (trailing vol only, the tradeable version): "
              f"{int((g.cbeats > 0.5).sum())} of {len(g)}")
        if (g.capped > 0.5).any():
            print(f"  * leverage capped at {MAX_LEVERAGE:g}x — these could not be sized "
                  f"up to the benchmark's risk, so their wealth is understated")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+", default=["1d"])
    ap.add_argument("--rules", nargs="+", default=None,
                    help="specific rule names; default is the whole leaderboard universe "
                         "for each sheet (TA-Lib singles + pairs + the catalog)")
    ap.add_argument("--cash-rate", type=float, default=None,
                    help="override the historical T-bill path with a constant, e.g. 0 "
                         "for no interest on idle capital, 0.037 for today's rate")
    ap.add_argument("--side", nargs="+", choices=["long", "short"],
                    default=["long", "short"],
                    help="'long' = the published long/flat rule; 'short' = long/short, "
                         "where 'stay out' becomes 'sell it'. Both by default.")
    ap.add_argument("--fill", choices=list(FILLS), default="close",
                    help="which price the position is paid on. 'close' is the published "
                         "convention and carries a look-ahead — it enters at the same "
                         "close whose high/low/close made the signal. 'open' removes it "
                         "and overcharges for delay. The pair BRACKETS the truth; quote "
                         "the range. See apply_fill")
    ap.add_argument("--n-trials", type=int, default=None, metavar="N",
                    help="size of the search these rules were SELECTED FROM. Mandatory "
                         "in spirit whenever --rules is a shortlist: without it the "
                         "multiplicity correction is computed on the shortlist's own "
                         "length and every bar drops for free.")
    ap.add_argument("--promote", action="store_true",
                    help="write the real edge_standard.csv from a scoped run. Off by "
                         "default: a scoped run writes *.partial.csv because this file "
                         "was twice clobbered by narrow runs. Pass this only when the "
                         "scoped set IS the study you mean to publish.")
    args = ap.parse_args()

    global _N_TRIALS_OVERRIDE
    _N_TRIALS_OVERRIDE = args.n_trials
    if args.rules and args.n_trials is None:
        print(f"WARNING: --rules given without --n-trials, so the multiplicity correction "
              f"will use {len(args.rules)} — the length of this list, not the size of the "
              f"search it came from. Every t bar and noise ceiling is understated.")
    if args.n_trials:
        print(f"multiplicity: correcting for a search of {args.n_trials:,} candidates, "
              f"not the {len(args.rules or [])} rules in this run")

    t0 = time.time()
    if args.cash_rate is not None:
        print(f"cash on idle capital fixed at {args.cash_rate:.2%} "
              f"(historical T-bill path overridden)")
    out = []
    for asset_class in args.classes:
        for timeframe in args.timeframes:
            for side in args.side:
                rules = args.rules or leaderboard_universe(asset_class, timeframe)
                df = run_sheet(asset_class, timeframe, rules, args.cash_rate, side,
                               fill=args.fill)
                if not df.empty:
                    out.append(df)
    if not out:
        print("no cached data")
        return
    t = pd.concat(out, ignore_index=True)
    report(t)
    std = score_standard(t)
    report_standard(std)

    # A SCOPED run must not overwrite the study's verdict of record.
    #
    # `edge_standard.csv` is the single place a pass or fail exists in this project, and
    # `--rules ibs` or `--class commodities` produces a handful of rows that are correct
    # for what was asked and useless as a verdict. That file was clobbered twice in one
    # session by scoped runs — 4,800 rows replaced by 3, then by 16 — and only the
    # session-start archive made it recoverable. The dashboard reads this file directly,
    # so a scoped write also silently empties the verdict column for every asset class
    # that was not in scope.
    # `--promote` is the deliberate exception. The guard below exists to stop an ACCIDENTAL
    # narrow run from becoming the verdict; it was never meant to stop a shortlist study
    # that is the intended study. Requiring an explicit flag keeps the accident impossible
    # while letting the intent through, and the flag is loud in the log either way.
    scoped = (bool(args.rules) or len(args.classes) < len(CLASSES)) and not args.promote
    suffix = ".partial" if scoped else ""
    if args.promote:
        print(f"--promote: writing the REAL edge_standard.csv from a scoped run of "
              f"{len(args.rules or [])} rules. This REPLACES the verdict of record.")
    # `riskmatch` is the 1.3 GB working table and nothing reads it; `edge_standard` is the
    # single place a verdict exists and both `validate.py` and the dashboard open it by
    # literal name. Only the first moves to Parquet.
    write_bulk(t, RESULTS_DIR / f"riskmatch{suffix}.parquet")
    std.to_csv(RESULTS_DIR / f"edge_standard{suffix}.csv", index=False)
    if scoped:
        print("\n  SCOPED RUN — wrote *.partial.csv and left the full verdict intact.")
    print(f"\nwrote riskmatch{suffix}.csv + edge_standard{suffix}.csv to {RESULTS_DIR}  "
          f"({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
