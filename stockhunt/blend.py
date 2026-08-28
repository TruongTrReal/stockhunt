"""Combine N strategy legs into one book, and price it against a matched benchmark.

A *portfolio* here is N legs, each of which is one `(asset class, timeframe, rule)` whose
full-history book curve has already been computed by `walk-forward optimization/
portfolio_wf.py` and written to `results/book_curves_<cls>_<tf>.json`. Nothing is
re-simulated. One pot of capital is split equally across the legs at inception, each
slice compounds on its own leg's curve, and the split is reset to equal at the first bar
of each calendar month.

Costs are already inside every leg's curve; this module charges nothing further, and
rebalancing is value-neutral here. That is a real understatement of the truth -- a monthly
reset moves money between legs and somebody pays a spread for it -- and it is called out
in `blend()`'s result as a warning rather than buried, because the alternative is a
rebalance premium that looks free.

--------------------------------------------------------------- the alignment problem

Legs may come from different classes and different timeframes, so their date axes differ
in span *and* in frequency. Two facts about the source decide how they are aligned, and
both are properties of the files rather than choices made here:

1. **The stored curves are stride-decimated, not daily.** `portfolio_wf.curve_points`
   keeps ~320 points per rule whatever the underlying bar count, so one point of a
   `us_stocks 1d` curve stands for ~18 sessions (~4 calendar weeks) while one point of a
   `crypto 4h` curve stands for ~4 days. **There is no daily series in these files and no
   way to recover one from them.** Any "daily" axis built here would be interpolation
   wearing a daily label.
2. **Sampling a coarse leg onto a fine axis is the one thing that must not happen.**
   Measured on `us_stocks 1d` + `crypto 4h`: the union of their two axes over the overlap
   is 371 points, on 318 of which the equity leg has not moved. Forward-filling produces
   86% flat bars followed by catch-up jumps -- which inflates volatility, wrecks the
   correlation estimate, and biases it toward zero. Toward zero is the flattering
   direction: it makes five picks off one sheet look like diversification.

So the common axis is **the coarsest leg's own dates, clipped to the intersection of every
leg's history**, and the finer legs are projected onto it by log-linear interpolation.
Consequences, stated plainly because each one changes how a number here reads:

* When every leg comes from the same sheet -- the common case, and the one where "are
  these really one bet?" matters most -- the grids are identical and the projection is the
  identity. The blend is then exact to the file's two-decimal rounding.
* A leg finer than the grid is interpolated only across a fraction of one of its own
  strides at each interval endpoint, so its return over a grid interval is very nearly its
  real one. `interp_ratio` on each leg reports native stride / grid stride: near 0 is
  negligible, near 1 means that leg is being smoothed across a whole interval and its
  contribution to volatility is understated.
* **The span is the intersection, never the union.** A book of a 2010-2026 leg and a
  2000-2026 leg is a 16-year measurement, and `axis["years"]` says 16. The legs' own spans
  travel alongside in `legs[i]["own_*"]` so the discarded history is visible rather than
  merely gone.
* **Sharpe, volatility and drawdown are grid-frequency statistics.** On a 1d sheet the
  grid bar is ~4 weeks, so `max_drawdown` cannot see a trough that opened and closed
  inside one bar and is a **lower bound** on the real one. It is not the daily-bar
  drawdown quoted on the same rule's dashboard row and should never be compared to it.
* Monthly rebalancing is applied at the resolution the source has. On a ~4-week grid most
  bars begin a new calendar month, so the reset fires nearly every bar; on a 4h sheet it
  fires ~12 times a year. `axis["rebalances"]` reports how many actually fired.

--------------------------------------------------------------- the benchmark

Each leg carries its own `bench`: the matched, cash-matched basket `portfolio_wf` built
for it, differing from the leg in the signal and nothing else. The blended benchmark is
those benches put through **the same weights, the same rebalance schedule and the same
axis** as the legs, which is the only construction that keeps that property one level up.

The tempting mistake is to drive the benchmark with the *strategy's* drifted weights so
the two sides "hold the same amounts". That would make the benchmark a function of the
signal, which is exactly the failure `../CLAUDE.md` spends a section on: whatever the two
sides differ in beyond the signal is attributed to skill, silently and flatteringly. Both
sides here start equal-weight and both reset to equal weight monthly; each drifts on its
own returns in between, because that is what "same rebalancing schedule" means.

A leg with no `bench` therefore poisons the whole comparison rather than one term of it,
and `blend()` returns `bench=None` in that case instead of a benchmark averaged over
whichever legs happened to have one.

--------------------------------------------------------------- running it

    python -m stockhunt.blend

from the repo root blends three real legs off disk and prints the result. It reads
`walk-forward optimization/results/` and is the one thing in this module that touches it.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from stockhunt import paths, stats

__all__ = ["sheet_path", "load_leg", "make_leg", "blend", "BlendError"]

# The Sharpe floor `portfolio_wf._sharpe` uses. A Sharpe off a handful of bars has an
# error bar wider than any value it could report, and a portfolio built on a short
# intersection is precisely the case that produces a handful of bars.
MIN_SHARPE_OBS = 30

# Above this, a leg's own sampling stride is a large enough fraction of a grid interval
# that interpolating it across the interval smooths away real movement. Chosen as "the
# leg's native points are sparser than roughly three per grid bar", which is where the
# interpolation error stops being confined to the ends of an interval.
INTERP_RATIO_WARN = 0.35

# ...and only once that interpolation covers a real share of the axis. Pinning the two
# intersection endpoints onto the grid leg's own dates makes one or two of its bars
# interpolated, which is a boundary effect and not worth a warning that then appears on
# every portfolio ever built.
INTERP_COVERAGE_WARN = 0.10

DAYS_PER_YEAR = 365.25

SECONDS_PER_DAY = 86_400.0


def _epoch_seconds(idx: pd.DatetimeIndex) -> np.ndarray:
    """A date axis as float seconds since the epoch.

    Explicitly `datetime64[s]` rather than `DatetimeIndex.asi8`, which returns the index's
    *own* unit. That is not a stable ns: pandas 3 resolves a list of date strings to
    `datetime64[us]`, so an `asi8` read scaled by nanoseconds-per-day reported every bar
    as zero days wide. Two indexes with different units would be worse than wrong-by-a-
    constant -- interpolating one against the other would silently place a leg a thousand
    years away from its own dates.
    """
    return idx.to_numpy(dtype="datetime64[s]").astype("float64")


def _median_bar_days(idx: pd.DatetimeIndex) -> float:
    """Typical spacing of a date axis, in days. Median, because these axes are strided in
    BARS and calendars are not: a holiday week and a crypto weekend are both outliers."""
    if idx.size < 2:
        return float("nan")
    return float(np.median(np.diff(_epoch_seconds(idx))) / SECONDS_PER_DAY)


class BlendError(ValueError):
    """A portfolio that cannot be priced, as opposed to one that prices badly.

    Its own type because the caller in front of this -- an API route, a dashboard panel --
    has to tell "your five legs never traded on the same day" apart from a bug, and
    catching bare `ValueError` around a numpy call cannot.
    """


# ============================================================ reading a leg off disk

def sheet_path(cls: str, tf: str, results_dir: Path | None = None) -> Path:
    """Where `run_book.sh --curves` put one sheet's per-rule curves."""
    root = Path(results_dir) if results_dir is not None else paths.WFO_RESULTS
    return root / f"book_curves_{cls}_{tf}.json"


@lru_cache(maxsize=2)
def _sheet(path: str, mtime: float) -> dict:
    """One sheet's curve file, parsed once and held.

    Keyed on MTIME as well as path, so a re-run of `run_book.sh` invalidates this without
    anybody remembering to -- the same arrangement `paper api/api_research.py::_curves`
    uses, and for the same reason.

    `maxsize=2` is a memory bound rather than a tuning guess: `book_curves_us_stocks_1d`
    is 12 MB on disk and several times that parsed, so holding all twenty sheets would
    cost more than the process has. A portfolio drawn from one or two sheets -- which is
    most of them -- never misses.
    """
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_leg(cls: str, tf: str, rule: str, results_dir: Path | None = None) -> dict:
    """One leg: its curve, its matched benchmark and its dates, out of the sheet file.

    Raises `BlendError` for a missing sheet or a rule that is not in it, with the sheet
    named. A rule quietly resolving to nothing is how a portfolio ends up quoting the
    performance of its other four legs under a five-leg label.
    """
    path = sheet_path(cls, tf, results_dir)
    if not path.exists():
        raise BlendError(
            f"No curve file for {cls}/{tf} at {path}. That sheet's book run has not been "
            f"done with --curves.")
    sheet = _sheet(str(path), path.stat().st_mtime)
    entry = sheet.get(rule)
    if entry is None:
        raise BlendError(
            f"{rule!r} is not in the curve file for {cls}/{tf} ({len(sheet)} rules in it).")
    return make_leg(
        dates=entry["dates"], curve=entry["curve"], bench=entry.get("bench"),
        cls=cls, tf=tf, rule=rule,
        n_assets=entry.get("n_assets"), side=entry.get("side"), pit=entry.get("pit"))


def make_leg(dates, curve, bench=None, *, cls: str = "", tf: str = "", rule: str = "",
             **extra) -> dict:
    """A leg dict from raw series -- the constructor `load_leg` goes through.

    Public because the unit suite has to build legs without a result file (`tests/` reads
    no sheet, by the rule in `../CLAUDE.md`), and because a caller holding a curve from
    somewhere else should not have to reverse-engineer the dict's shape.

    Validates rather than trusts: a non-monotonic or duplicated date axis makes the
    interpolation below wrong in a way that shows up as a plausible-looking curve, not as
    an exception.
    """
    idx = pd.DatetimeIndex(pd.to_datetime(list(dates)))
    eq = np.asarray(curve, dtype="float64")
    label = "/".join(p for p in (cls, tf, rule) if p) or "leg"
    if idx.size != eq.size:
        raise BlendError(f"{label}: {idx.size} dates against {eq.size} curve points.")
    if idx.size < 2:
        raise BlendError(f"{label}: a curve of {idx.size} point(s) has no return in it.")
    if not idx.is_monotonic_increasing or not idx.is_unique:
        raise BlendError(f"{label}: dates are not strictly increasing.")
    if not np.all(np.isfinite(eq)) or np.any(eq <= 0.0):
        raise BlendError(f"{label}: curve has a non-finite or non-positive level, which "
                         f"no equity curve of a funded book can have.")
    bq = None
    if bench is not None:
        bq = np.asarray(bench, dtype="float64")
        if bq.size != eq.size:
            raise BlendError(f"{label}: benchmark has {bq.size} points against the "
                             f"curve's {eq.size}.")
        if not np.all(np.isfinite(bq)) or np.any(bq <= 0.0):
            raise BlendError(f"{label}: benchmark has a non-finite or non-positive level.")
    leg = {"cls": cls, "tf": tf, "rule": rule, "dates": idx, "curve": eq, "bench": bq}
    leg.update(extra)
    return leg


def _label(leg: dict) -> str:
    parts = [str(leg.get(k) or "") for k in ("cls", "tf", "rule")]
    return "/".join(p for p in parts if p) or "leg"


# ============================================================ the common axis

def _to_leg(item) -> dict:
    """Accept a leg dict, or a `(cls, tf, rule)` triple to be loaded."""
    if isinstance(item, dict) and "dates" in item:
        return item
    if isinstance(item, dict):
        return load_leg(item["cls"], item["tf"], item["rule"], item.get("results_dir"))
    cls, tf, rule = item
    return load_leg(cls, tf, rule)


def _n_inside(leg: dict, lo: pd.Timestamp, hi: pd.Timestamp) -> int:
    d = leg["dates"]
    return int(((d >= lo) & (d <= hi)).sum())


def _common_axis(legs: list[dict], start: pd.Timestamp | None) -> pd.DatetimeIndex:
    """The coarsest leg's dates, clipped to every leg's overlap.

    Not the union, and not a synthetic daily or monthly grid. See the module docstring:
    the union puts a coarse leg on a fine axis, which manufactures flat bars, and a
    synthetic grid manufactures points that no leg ever observed. The coarsest leg's own
    dates are the finest axis on which *every* leg has genuinely moved between bars.
    """
    lo = max(leg["dates"][0] for leg in legs)
    hi = min(leg["dates"][-1] for leg in legs)
    spans = "; ".join(f"{_label(l)} {l['dates'][0].date()}..{l['dates'][-1].date()}"
                      for l in legs)
    if start is not None:
        lo = max(lo, pd.Timestamp(start))
        if lo > hi:
            raise BlendError(
                f"No shared history at or after {pd.Timestamp(start).date()}. Legs: "
                f"{spans}.")
    if lo >= hi:
        raise BlendError(f"The legs share no overlapping history. Legs: {spans}.")

    # Coarsest = fewest of its own observations inside the overlap. Counting inside the
    # overlap rather than over the whole leg is the point: a leg can be dense over its own
    # long history and still contribute two points to a short intersection.
    coarsest = min(legs, key=lambda l: _n_inside(l, lo, hi))
    d = coarsest["dates"]
    axis = d[(d >= lo) & (d <= hi)]

    # Pin the endpoints to the intersection itself. Without this the reported span is the
    # coarsest leg's dates *near* the overlap, which silently drops up to one stride of
    # history at each end and quietly changes what `years` describes.
    axis = axis.union(pd.DatetimeIndex([lo, hi]))
    if axis.size < 2:
        raise BlendError(
            f"The legs overlap on {lo.date()}..{hi.date()}, which is too little for a "
            f"single return.")
    return axis


def _project(idx: pd.DatetimeIndex, eq: np.ndarray,
             axis: pd.DatetimeIndex) -> np.ndarray:
    """A leg's equity onto the common axis, log-linear between its own observations.

    Log-linear, not linear on the level, because an equity curve compounds: a straight
    line in log space is a constant return through the interval, which is the only
    assumption that leaves the interval's total return equal to what the two real
    endpoints say it was. Linear on the level puts a different total return in the gap
    than the observations bracketing it do.

    Where the axis *is* this leg's own dates -- every same-sheet portfolio -- this is the
    identity, to a float round-trip through log/exp.
    """
    return np.exp(np.interp(_epoch_seconds(axis), _epoch_seconds(idx), np.log(eq)))


def _rebalance_flags(axis: pd.DatetimeIndex, rebalance: str | None) -> np.ndarray:
    """Which bars reset the split back to equal weight.

    `monthly` fires on the first bar of each calendar month. Bar 0 is never flagged: the
    book is equal-weight at inception by construction, and flagging it would count a
    rebalance that never happened.
    """
    flags = np.zeros(axis.size, dtype=bool)
    if rebalance in (None, "none"):
        return flags
    if rebalance != "monthly":
        raise BlendError(f"Unknown rebalance schedule {rebalance!r}; expected 'monthly' "
                         f"or 'none'.")
    period = axis.to_period("M")
    flags[1:] = period[1:] != period[:-1]
    return flags


def _walk(levels: np.ndarray, capital: float,
          reb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Compound N equal slices along `levels`, resetting to equal on the flagged bars.

    `levels` is (n_legs, n_bars) of equity levels already on the common axis. Returns the
    total wealth path and the (n_legs, n_bars) per-leg dollar P&L.

    The reset happens *before* the bar's return, so the flagged bar itself is earned on
    equal weights -- that is what "rebalanced at the first bar of the month" means when
    holdings are set at the previous close. It is value-neutral, so the wealth path is
    continuous across it and the attribution still telescopes exactly to
    `final - capital`.
    """
    n_legs, n_bars = levels.shape
    r = np.zeros_like(levels)
    r[:, 1:] = levels[:, 1:] / levels[:, :-1] - 1.0

    v = np.full(n_legs, capital / n_legs, dtype="float64")
    total = np.empty(n_bars, dtype="float64")
    pnl = np.zeros((n_legs, n_bars), dtype="float64")
    total[0] = capital
    for t in range(1, n_bars):
        if reb[t]:
            v[:] = v.sum() / n_legs
        step = v * r[:, t]
        pnl[:, t] = step
        v = v + step
        total[t] = v.sum()
    return total, pnl


def _stat_block(total: np.ndarray, bpy: float, rf: float) -> dict:
    """The headline numbers, from `stockhunt.stats` and nowhere else.

    `dropna=True` on the drawdown: `../CLAUDE.md` records that the default `False` is a
    preserved bug (one non-finite bar poisons the whole series to NaN) kept only so that
    published sheets do not move. Nothing computed here is a published sheet.
    """
    r = total[1:] / total[:-1] - 1.0
    return {
        "final_value": float(total[-1]),
        "total_return": float(total[-1] / total[0] - 1.0),
        "cagr": stats.cagr(r, bpy),
        "sharpe": stats.sharpe(r, rf, bpy, min_obs=MIN_SHARPE_OBS),
        "ann_vol": stats.ann_vol(r, bpy),
        "max_drawdown": stats.max_drawdown(r, dropna=True),
        "bars": int(total.size),
    }


# ============================================================ the blend

def blend(legs, capital: float = 100_000.0, rebalance: str = "monthly",
          start=None, rf: float = 0.0) -> dict:
    """Blend N legs into one book and price it against the same blend of their benchmarks.

    `legs` is a sequence of leg dicts from `load_leg`/`make_leg`, or of `(cls, tf, rule)`
    triples to be loaded. One pot of `capital` is split equally at inception, each slice
    compounds on its leg, and `rebalance='monthly'` resets the split to equal at the first
    bar of each calendar month. `rebalance='none'` lets the split run.

    `start` moves inception later than the legs' histories allow for; the axis is clipped
    to it and the capital is deployed at the first shared bar at or after it.

    `rf` is the per-bar risk-free rate the Sharpes are taken in excess of, and it defaults
    to **zero**. The curve files carry no bill-rate series, so a real excess Sharpe is not
    computable from them: the number returned here is not the same statistic as
    `portfolio_wf`'s `sharpe`, which is excess of DTB3, and the two must not be put in one
    column. Both sides of this comparison use the same `rf`, so the portfolio-versus-
    benchmark reading is unaffected by the choice.

    Returns a dict -- see the module docstring for what the axis and the benchmark mean.
    Raises `BlendError` when the portfolio cannot be priced at all.
    """
    if isinstance(legs, dict):
        legs = [legs]
    legs = [_to_leg(x) for x in legs]
    if not legs:
        raise BlendError("A portfolio needs at least one leg.")
    if not np.isfinite(capital) or capital <= 0:
        raise BlendError(f"Capital must be a positive number, got {capital!r}.")

    axis = _common_axis(legs, None if start is None else pd.Timestamp(start))
    n_bars, n_legs = axis.size, len(legs)
    years = float((axis[-1] - axis[0]).days) / DAYS_PER_YEAR
    if years <= 0:
        raise BlendError(f"The legs overlap inside a single day ({axis[0].date()}), which "
                         f"cannot be annualised.")
    # Bars per year measured off this axis, never a hardcoded 252. The axis is
    # stride-decimated and non-uniform, so the only defensible annualiser is the one the
    # axis itself implies; anything else annualises a ~4-week bar as though it were a day.
    bpy = (n_bars - 1) / years

    levels = np.vstack([_project(l["dates"], l["curve"], axis) for l in legs])
    reb = _rebalance_flags(axis, rebalance)
    total, pnl = _walk(levels, capital, reb)

    have_bench = all(l.get("bench") is not None for l in legs)
    b_total = None
    if have_bench:
        b_levels = np.vstack([_project(l["dates"], l["bench"], axis) for l in legs])
        b_total, _ = _walk(b_levels, capital, reb)

    # Per-leg returns on the shared axis: the input to both the correlation matrix and
    # each leg's standalone statistics, so a leg's numbers here describe the intersection
    # rather than its own history. They will not match its dashboard row, and should not.
    leg_r = levels[:, 1:] / levels[:, :-1] - 1.0
    corr = pd.DataFrame(leg_r.T).corr()

    grid_days = _median_bar_days(axis)
    rows, warnings = [], []
    for i, leg in enumerate(legs):
        own = leg["dates"]
        own_days = _median_bar_days(own)
        ratio = own_days / grid_days if grid_days > 0 else float("nan")
        # How much of this leg is a real observation rather than an interpolated one. The
        # leg that supplied the grid scores 1.0 and is never interpolated at all; so does
        # any leg sharing that leg's dates, which is every same-sheet portfolio.
        on_grid = float(np.isin(axis.to_numpy(), own.to_numpy()).mean())
        rows.append({
            "cls": leg.get("cls"), "tf": leg.get("tf"), "rule": leg.get("rule"),
            "label": _label(leg),
            "weight_initial": 1.0 / n_legs,
            "pnl": float(pnl[i].sum()),
            # This leg's dollar P&L as a fraction of the whole pot. The legs' values sum
            # to the book's `total_return` exactly, which a share-of-profit ratio does
            # not: when the book loses money, dividing by a negative profit reports a leg
            # that MADE money as a negative contributor. Signed and additive beats
            # normalised and backwards.
            "contribution": float(pnl[i].sum() / capital),
            "cagr": stats.cagr(leg_r[i], bpy),
            "sharpe": stats.sharpe(leg_r[i], rf, bpy, min_obs=MIN_SHARPE_OBS),
            "ann_vol": stats.ann_vol(leg_r[i], bpy),
            "max_drawdown": stats.max_drawdown(leg_r[i], dropna=True),
            "own_start": own[0].strftime("%Y-%m-%d"),
            "own_end": own[-1].strftime("%Y-%m-%d"),
            "own_points": int(own.size),
            "own_years": round(float((own[-1] - own[0]).days) / DAYS_PER_YEAR, 2),
            "interp_ratio": round(ratio, 3),
            "on_grid_frac": round(on_grid, 3),
            "n_assets": leg.get("n_assets"),
            "side": leg.get("side"),
        })
        # The coverage test exempts the leg whose dates the grid IS, and every leg sharing
        # them. Warning on the ratio alone fired on the grid leg itself, whose stride
        # equals the grid's by construction and which is interpolated nowhere but at the
        # two pinned endpoints.
        if (1.0 - on_grid) > INTERP_COVERAGE_WARN and ratio > INTERP_RATIO_WARN:
            warnings.append(
                f"{_label(leg)} is sampled every ~{own_days:.0f}d against a "
                f"~{grid_days:.0f}d grid, so it is interpolated across a large part of "
                f"each bar: its volatility and drawdown here are understated.")

    # The warning that matters most, and the reason the correlation matrix is in the
    # result at all: five rules off one sheet trade one universe under one cost schedule,
    # and a book of them is closer to one bet than to five. `n_assets` cannot notice --
    # it counts holdings, exactly as it cannot notice that ES/NQ/YM/RTY are one bet.
    sheets: dict[tuple, list[str]] = {}
    for leg in legs:
        sheets.setdefault((leg.get("cls"), leg.get("tf")), []).append(_label(leg))
    for (cls, tf), names in sheets.items():
        if len(names) > 1:
            warnings.append(
                f"{len(names)} legs come from the same sheet ({cls}/{tf}) and trade the "
                f"same universe: {', '.join(names)}. Read their pairwise correlation "
                f"before counting them as separate bets.")

    if not have_bench:
        warnings.append(
            "At least one leg carries no matched benchmark, so no blended benchmark is "
            "reported. A benchmark averaged over only the legs that have one would differ "
            "from the book in more than the signal.")
    if n_bars < MIN_SHARPE_OBS:
        warnings.append(
            f"The shared history is {n_bars} bars; Sharpe needs {MIN_SHARPE_OBS} and is "
            f"NaN below that rather than a number with no error bar.")
    warnings.append(
        f"Statistics are computed on a ~{grid_days:.0f}-day grid, which is the resolution "
        f"the stored curves have. Drawdown is a lower bound: a trough that opened and "
        f"closed inside one bar is invisible.")
    warnings.append(
        "Rebalancing is charged nothing. Each leg's own costs are already inside its "
        "curve, but moving money between legs at the monthly reset is not free in life.")

    port = _stat_block(total, bpy, rf)
    bench_stats = _stat_block(b_total, bpy, rf) if have_bench else None

    return {
        "capital": float(capital),
        "rebalance": rebalance if rebalance else "none",
        "n_legs": n_legs,
        "axis": {
            "start": axis[0].strftime("%Y-%m-%d"),
            "end": axis[-1].strftime("%Y-%m-%d"),
            "years": round(years, 2),
            "bars": int(n_bars),
            "bars_per_year": round(float(bpy), 2),
            "median_bar_days": round(grid_days, 2),
            # Whose dates these are. Named so a reader can see which leg's resolution the
            # whole portfolio inherited.
            "grid_from": _label(min(legs,
                                    key=lambda l: _n_inside(l, axis[0], axis[-1]))),
            "rebalances": int(reb.sum()),
        },
        "dates": [d.strftime("%Y-%m-%d") for d in axis],
        "curve": [float(v) for v in total],
        "bench": [float(v) for v in b_total] if have_bench else None,
        "legs": rows,
        "corr": {"labels": [_label(l) for l in legs],
                 "matrix": [[float(v) for v in row] for row in corr.to_numpy()]},
        "metrics": port,
        "bench_metrics": bench_stats,
        # N1 in `../CLAUDE.md`'s three numbers: the book against its own matched basket.
        # N2 (against a purchasable index) and N3 (survivorship) are not computable from
        # these files, and are absent rather than approximated.
        "excess": ({k: port[k] - bench_stats[k]
                    for k in ("cagr", "sharpe", "total_return")}
                   if have_bench else None),
        "warnings": warnings,
    }


# ============================================================ sanity check

def _main() -> None:
    """Blend real legs off disk, so a human can eyeball the whole thing in one command.

        python -m stockhunt.blend

    run from the repo root, with no arguments. It is the only thing in this module that
    reads `walk-forward optimization/results/`, which is why the unit suite never calls it.

    Deliberately crosses sheets -- a daily equity leg, a daily ETF leg and a 4h crypto leg
    -- because a same-sheet blend exercises none of the alignment above.
    """
    picks = [("us_stocks", "1d", "RSI"), ("us_etfs", "1d", "MACD"),
             ("crypto", "4h", "WILLR")]
    loaded = []
    for cls, tf, rule in picks:
        try:
            loaded.append(load_leg(cls, tf, rule))
        except BlendError as exc:
            print(f"skipping {cls}/{tf}/{rule}: {exc}")
    if len(loaded) < 2:
        print("Not enough legs on disk to demonstrate a blend.")
        return

    out = blend(loaded)
    ax = out["axis"]
    print(f"\n{out['n_legs']} legs, ${out['capital']:,.0f}, rebalance {out['rebalance']}")
    print(f"axis {ax['start']} .. {ax['end']}  {ax['years']}y  {ax['bars']} bars "
          f"(~{ax['median_bar_days']:.0f}d each, grid from {ax['grid_from']}), "
          f"{ax['rebalances']} rebalances")
    print("\nlegs -- own history, then what it did inside this book:")
    for r in out["legs"]:
        print(f"  {r['label']:<24} own {r['own_start']}..{r['own_end']} "
              f"({r['own_years']:>5}y, {r['own_points']:>3}pts, {r['on_grid_frac']:.0%} "
              f"on grid, stride x{r['interp_ratio']:.2f})  pnl ${r['pnl']:>12,.0f}  "
              f"{r['contribution']:>+7.1%} of the pot")
    print("\ncorrelation between legs, on the shared axis:")
    labels = out["corr"]["labels"]
    print(" " * 26 + "".join(f"{l:>26}" for l in labels))
    for name, row in zip(labels, out["corr"]["matrix"]):
        print(f"  {name:<24}" + "".join(f"{v:>26.3f}" for v in row))
    m, b = out["metrics"], out["bench_metrics"]
    print(f"\n  {'':<14}{'portfolio':>16}{'matched bench':>16}")
    for key in ("final_value", "total_return", "cagr", "sharpe", "ann_vol",
                "max_drawdown"):
        bv = f"{b[key]:,.4f}" if b else "n/a"
        print(f"  {key:<14}{m[key]:>16,.4f}{bv:>16}")
    print("\nwarnings:")
    for w in out["warnings"]:
        print(f"  - {w}")


if __name__ == "__main__":
    _main()
