"""Gate: the 101-alpha interpreter is causal, and its two axes are the right way round.

A `__main__` script that exits nonzero, like `test_t_bar.py` beside it and
`strategies/tests/test_causality.py`. Not collected by pytest: the nonzero exit is the
product.

Synthetic bars only. Nothing here reads `data/`, so it cannot start failing because
somebody refetched a ticker.

Four properties, and each of them has a specific way of being silently wrong:

1.  **Every published formula parses.** 101 of 101, including the ones this repo cannot
    evaluate -- a parse failure on Alpha#67 would otherwise be indistinguishable from
    Alpha#67 being excluded for missing an industry map.

2.  **Causality, proved by TRUNCATION.** Score the panel, then score a prefix of the same
    panel, and require every overlapping value to be identical. Reading the operators and
    satisfying yourself they only look backwards is exactly how `nanmedian` leaked into
    two stages of this pipeline; truncation cannot be talked round.

3.  **`rank` is cross-sectional and `ts_*` are not.** These are the two axes of the same
    DataFrame and swapping them produces a full, plausible, entirely wrong panel. Pinned
    here against hand-computed values rather than against the code's own behaviour.

4.  **An absent symbol never receives a score.** `np.argmax` of an all-NaN window returns
    0, `NaN < NaN` is False, and a ternary with a scalar arm falls through to it -- three
    separate routes by which a name that was not in the universe gets a real number and,
    because `rank` is cross-sectional, moves the percentile of every genuine name beside
    it.
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import alpha101 as A
from alpha101_formulas import FORMULAS

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(f"{name} {detail}".strip())


def synthetic(n_days: int = 900, n_sym: int = 24, seed: int = 7) -> tuple[dict, pd.DataFrame]:
    """A panel with the shape of the real thing: staggered listings and a dead name."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2010-01-04", periods=n_days)
    syms = [f"S{i:02d}" for i in range(n_sym)]
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0, 0.013, (n_days, n_sym)), axis=0)),
        index=idx, columns=syms)
    # Staggered entry, so most symbols are absent for part of the sample.
    for j, s in enumerate(syms):
        close.iloc[:j * 7, j] = np.nan
    close["S23"] = np.nan                      # a name with no bars at all, ever

    spread = 1 + rng.uniform(0.001, 0.02, close.shape)
    high = close * spread
    low = close / spread
    open_ = low + (high - low) * rng.uniform(0, 1, close.shape)
    vol = pd.DataFrame(rng.lognormal(13, 0.6, close.shape), index=idx, columns=syms)
    vol = vol.where(close.notna())

    env = {"open": open_.where(close.notna()), "high": high.where(close.notna()),
           "low": low.where(close.notna()), "close": close, "volume": vol,
           "returns": close.pct_change()}
    dollar = close * vol
    for d in A._adv_windows():
        env[f"adv{d}"] = dollar.rolling(d, min_periods=d).mean()
    env["vwap"] = (high + low + close) / 3.0
    return env, close


def test_parse_all() -> None:
    bad = []
    for n in sorted(FORMULAS):
        try:
            A.parse(FORMULAS[n])
        except Exception as exc:                                    # noqa: BLE001
            bad.append((n, type(exc).__name__, str(exc)[:60]))
    check("all 101 formulas parse", not bad, f"{len(FORMULAS)} parsed" if not bad
          else f"failed: {bad}")


def test_axes() -> None:
    """rank across symbols; ts_* down the index. Hand-computed, not self-referential."""
    idx = pd.bdate_range("2020-01-01", periods=4)
    x = pd.DataFrame({"A": [1.0, 4.0, 9.0, 2.0],
                      "B": [3.0, 2.0, 1.0, 8.0],
                      "C": [2.0, 6.0, 5.0, 4.0]}, index=idx)
    env = {"x": x}

    r = A.evaluate(A.parse("rank(x)"), env)
    # Row 0 is (1, 3, 2) -> percentile ranks (1/3, 3/3, 2/3), left to right.
    check("rank() is cross-sectional",
          np.allclose(r.iloc[0].to_numpy(), [1 / 3, 1.0, 2 / 3]),
          f"row0={np.round(r.iloc[0].to_numpy(), 3).tolist()}")

    s = A.evaluate(A.parse("sum(x, 2)"), env)
    check("sum() is a time series", np.allclose(s["A"].to_numpy()[1:], [5.0, 13.0, 11.0]),
          f"A={s['A'].to_numpy().tolist()}")

    am = A.evaluate(A.parse("ts_argmax(x, 3)"), env)
    # Window (1,4,9) peaks on the 3rd day of the window -> 3 under the +1 convention.
    check("ts_argmax counts from the oldest bar, 1-based",
          am["A"].to_numpy()[2] == 3.0 and am["B"].to_numpy()[2] == 1.0,
          f"A={am['A'].to_numpy()[2]}, B={am['B'].to_numpy()[2]}")

    d = A.evaluate(A.parse("decay_linear(x, 3)"), env)
    # Weights 1,2,3 over the window (1,4,9), normalised by 6.
    check("decay_linear weights the newest bar heaviest",
          np.isclose(d["A"].to_numpy()[2], (1 * 1 + 2 * 4 + 3 * 9) / 6),
          f"got {d['A'].to_numpy()[2]:.4f}")

    sc = A.evaluate(A.parse("scale(x)"), env)
    check("scale() normalises each day to sum |x| = 1",
          np.isclose(sc.iloc[0].abs().sum(), 1.0))

    mx = A.evaluate(A.parse("max(rank(x), sum(x, 2))"), env)
    check("max() of two expressions is elementwise, not ts_max",
          mx.shape == x.shape and np.isclose(
              mx["A"].to_numpy()[1], max(A.evaluate(A.parse("rank(x)"), env)["A"].to_numpy()[1],
                                         5.0)))


def test_truncation_causality(env: dict, close: pd.DataFrame) -> None:
    """Score the whole panel, then a prefix of it. Overlapping values must be identical.

    This is the only test in the file that could catch a look-ahead, and it catches every
    kind at once: a centred window, a whole-sample statistic, a negative shift, a
    `bfill`. If any operator reached forward, the prefix run -- which has no future to
    reach into -- would disagree.
    """
    cut = int(len(close) * 0.8)
    head_index = close.index[:cut]
    trunc = {k: v.loc[head_index] for k, v in env.items()}

    # `adv{d}` and `returns` are derived, so rebuild them inside the truncated world
    # rather than slicing the full-sample versions -- slicing would import the answer.
    tclose = trunc["close"]
    trunc["returns"] = tclose.pct_change()
    dollar = tclose * trunc["volume"]
    for d in A._adv_windows():
        trunc[f"adv{d}"] = dollar.rolling(d, min_periods=d).mean()

    worst, offenders = 0.0, []
    for n in A.runnable(vwap_proxy=True):
        full = A.score_panel(n, env).loc[head_index]
        part = A.score_panel(n, trunc)
        a, b = full.to_numpy("float64"), part.to_numpy("float64")
        both = ~np.isnan(a) & ~np.isnan(b)
        # A prefix run legitimately has MORE NaN (a 250-bar window near the cut cannot be
        # filled). It may never have FEWER, and where both are defined they must agree.
        extra = int((np.isnan(a) & ~np.isnan(b)).sum())
        gap = float(np.nanmax(np.abs(a[both] - b[both]))) if both.any() else 0.0
        if extra or not np.isfinite(gap) or gap > 1e-9:
            offenders.append((n, extra, gap))
        worst = max(worst, gap if np.isfinite(gap) else np.inf)

    check("truncation: 82 alphas identical on the overlap",
          not offenders, f"max drift {worst:.2e}" if not offenders
          else f"offenders {offenders[:6]}")


def test_absent_symbol_never_scored(env: dict, close: pd.DataFrame) -> None:
    live = close.notna()
    leaked = []
    for n in A.runnable(vwap_proxy=True):
        s = A.score_panel(n, env)
        cells = int(((s.notna()) & ~live).to_numpy().sum())
        if cells:
            leaked.append((n, cells))
    check("no alpha scores a symbol that had no bar that day",
          not leaked, "S23 never held" if not leaked else f"leaked {leaked[:6]}")


def test_positions_are_a_book(env: dict, close: pd.DataFrame) -> None:
    s = A.score_panel(101, env)
    pos = A.to_positions(s, close, quantile=0.2)
    held = pos.sum(axis=1)
    n_live = close.notna().sum(axis=1)
    active = n_live >= 10
    frac = (held[active] / n_live[active]).dropna()
    check("top-quintile book holds about a fifth of the live names",
          0.12 <= float(frac.mean()) <= 0.30, f"mean {float(frac.mean()):.3f}")
    check("book never holds an absent symbol",
          int(((pos > 0) & ~close.notna()).to_numpy().sum()) == 0)
    check("book is long/flat only",
          bool(((pos == 0) | (pos == 1)).to_numpy().all()))


def main() -> int:
    print(__doc__.split("\n")[0])
    print()
    env, close = synthetic()
    print(f"synthetic panel: {close.shape[0]} bars x {close.shape[1]} symbols\n")

    test_parse_all()
    test_axes()
    test_absent_symbol_never_scored(env, close)
    test_positions_are_a_book(env, close)
    test_truncation_causality(env, close)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
