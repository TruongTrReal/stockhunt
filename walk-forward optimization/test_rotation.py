"""Gate for `rotation.py`. A `__main__` script that exits nonzero, like `test_t_bar.py`.

Not collected by pytest. Run it directly from this folder:

    ..\\.venv\\Scripts\\python test_rotation.py

Four properties, and each one guards a mistake that would have made the replication say
something it has not measured:

1. **Causality, by truncation, not by reading the code.** Rebuild the weights on a series
   with the last 500 sessions removed and require every surviving weight to be identical.
   This is the same test `strategies/tests/test_causality.py` applies to the published
   rules, and it is here for the same reason: a look-ahead in a rotation is invisible in
   the equity curve and obvious under truncation.

2. **Fill timing.** The weight decided on a session must earn nothing on that session.
   Checked against a hand-built two-asset series where the answer is arithmetic rather
   than a fixture.

3. **The month-end defect is real and is what it is claimed to be.** The post's
   `date.day == monthrange(...)[1]` test must fire on strictly fewer sessions than one
   per month, and every session it does fire on must be a genuine last-trading-day. If
   this ever stops holding, the "the post skips three months in ten" claim is wrong.

4. **The published metric definitions are reproduced.** `portwine_stats` must return
   CAGR/vol/Sharpe under portwine's own conventions -- bars/252 for years, no risk-free
   rate, Sharpe as CAGR over vol -- because those conventions are how the post's 0.49 was
   produced and the whole comparison rests on matching them.
"""
from __future__ import annotations

import calendar as _calendar
import sys

import numpy as np
import pandas as pd

import rotation as R

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def test_causality() -> None:
    print("causality by truncation (broad basket, 500 bars removed)")
    closes, idx = R.prepare(R.BASKETS["broad"], False, "2000-04-04", "2025-04-04")
    for month_end in ("calendar", "trading"):
        full = R.rotate(R.scores(closes, idx, 63, 0.0),
                        R.rebalance_days(idx, month_end), "best")
        cut = idx[:-500]
        short = R.rotate(R.scores(closes, cut, 63, 0.0),
                         R.rebalance_days(cut, month_end), "best")
        same = np.array_equal(full[:len(cut)], short)
        bad = int((full[:len(cut)] != short).any(axis=1).sum())
        check(f"{month_end}: weights on the overlap are identical", same,
              "" if same else f"{bad} sessions differ")


def test_fill_timing() -> None:
    print("fill timing: the deciding close is not the filling close")
    idx = pd.date_range("2020-01-01", periods=6, freq="D")
    closes = pd.DataFrame({"A": [1.0, 1.0, 1.0, 2.0, 2.0, 2.0],
                           "B": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]}, index=idx)
    w = np.zeros((6, 2))
    w[2:, 0] = 1.0                      # decided at bar 2, so it must earn from bar 3
    net = R.book(w, closes, idx, None)
    check("no return on the deciding bar", abs(net[2]) < 1e-12, f"net[2]={net[2]:.6g}")
    check("the +100% move lands on the next bar", abs(net[3] - 1.0) < 1e-12,
          f"net[3]={net[3]:.6g}")


def test_month_end_defect() -> None:
    print("the post's calendar month-end test skips months")
    _, idx = R.prepare(["SPY"], False, "2000-04-04", "2025-04-04")
    cal = R.rebalance_days(idx, "calendar")
    trd = R.rebalance_days(idx, "trading")
    months = len(pd.PeriodIndex(idx, freq="M").unique())
    check("calendar fires on fewer sessions than there are months",
          cal.sum() < months, f"{cal.sum()} of {months} months")
    check("every calendar firing is also a last trading day",
          bool((trd | ~cal).all()), "a calendar month-end that is not a session boundary")
    skipped = 1 - cal.sum() / months
    check("the skip rate is between a fifth and a half", 0.20 < skipped < 0.50,
          f"{skipped:.1%} of months never rebalance")
    # And the last calendar day really is absent from the session index on those months,
    # which is the mechanism rather than the symptom.
    miss = [p for p in pd.PeriodIndex(idx, freq="M").unique()
            if pd.Timestamp(year=p.year, month=p.month,
                            day=_calendar.monthrange(p.year, p.month)[1]) not in idx]
    check("the skipped months are exactly the ones whose last calendar day is not a "
          "session", len(miss) == months - int(cal.sum()),
          f"{len(miss)} vs {months - int(cal.sum())}")


def test_metric_definitions() -> None:
    print("portwine's metric conventions are reproduced")
    r = np.full(504, 0.001)                       # two 252-bar years, no variance
    s = R.portwine_stats(r)
    want_total = 1.001 ** 504 - 1.0
    check("total return compounds", abs(s["total_return"] - want_total) < 1e-12)
    check("years is bars/252, not elapsed time",
          abs(s["cagr"] - ((1 + want_total) ** 0.5 - 1.0)) < 1e-12)
    check("a constant series has zero vol and Sharpe defined as 0",
          s["ann_vol"] < 1e-12 and s["sharpe"] == 0.0)
    rng = np.random.default_rng(0)
    x = rng.normal(0.0004, 0.01, 5000)
    s = R.portwine_stats(x)
    check("Sharpe is CAGR over vol, with no risk-free rate",
          abs(s["sharpe"] - s["cagr"] / s["ann_vol"]) < 1e-12)
    check("max drawdown is negative and bounded by -1",
          -1.0 < s["max_dd"] <= 0.0, f"{s['max_dd']:.4f}")


def test_controls_are_matched() -> None:
    print("the controls hold everything but the ranking fixed")
    closes, idx = R.prepare(R.BASKETS["broad"], False, "2000-04-04", "2025-04-04")
    sc = R.scores(closes, idx, 63, 0.0)
    rb = R.rebalance_days(idx, "trading")
    best = R.rotate(sc, rb, "best")
    rng = np.random.default_rng(1)
    rand = R.rotate(sc, rb, "random", rng)
    picks = best[rb].sum(axis=0)
    mix = R.rotate(sc, rb, "freq", rng, picks / picks.sum())
    for name, w in (("random", rand), ("freq", mix)):
        check(f"{name}: fully invested whenever the rule is",
              np.allclose(w.sum(axis=1), best.sum(axis=1)))
        check(f"{name}: one name at a time", bool(((w > 0).sum(axis=1) <= 1).all()))
        check(f"{name}: never holds an ineligible name",
              not bool(((w > 0) & ~np.isfinite(sc)).any()))


def main() -> int:
    for t in (test_causality, test_fill_timing, test_month_end_defect,
              test_metric_definitions, test_controls_are_matched):
        t()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("rotation.py is causal, fills a bar late, and reproduces the post's metrics")
    return 0


if __name__ == "__main__":
    sys.exit(main())
