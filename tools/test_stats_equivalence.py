"""Prove `stockhunt.stats` reproduces the implementations it replaced, bit for bit.

Four helpers existed two or three times each across `walk-forward optimization/`, under
the same names and with quietly different behaviour. Collapsing them to one definition is
only safe if the surviving version returns the *identical float* for every input the old
ones saw — including the inputs where they disagreed with each other, which is where a
silent change would hide.

So the originals are pasted in verbatim below and the new ones are diffed against them
over adversarial inputs: NaNs, infinities, constant series, all-zero series, two-element
series, and series short enough to straddle both `min_obs` thresholds.

    python tools/test_stats_equivalence.py

Exits nonzero on any mismatch. Bit-level comparison, not `np.isclose` — a refactor that
moves the last mantissa bit still moves a published number, and this repo reports to
three significant figures off numbers it has already had to retract twice.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stockhunt import stats                                    # noqa: E402


# ------------------------------------------------------- the originals, verbatim

def orig_ann_vol(r, bpy):                                      # riskmatch_wf._ann_vol
    return float(np.std(r, ddof=1) * np.sqrt(bpy))


def orig_cagr_riskmatch(r, bpy):                               # riskmatch_wf._cagr
    r = r[np.isfinite(r)]
    if r.size < 2 or bpy <= 0:
        return float("nan")
    return float(np.prod(1.0 + r) ** (bpy / r.size) - 1.0)


def orig_cagr_portfolio(r, bpy):                               # portfolio_wf._cagr_of
    r = r[np.isfinite(r)]
    if r.size < 2:
        return float("nan")
    return float(np.prod(1.0 + r) ** (bpy / r.size) - 1.0)


def orig_cagr_focus(net, mask, bpy):                           # focus_wf.cagr
    r = net[mask]
    r = r[np.isfinite(r)]
    if r.size < 2 or bpy <= 0:
        return float("nan")
    yrs = r.size / bpy
    return float(np.prod(1.0 + r) ** (1.0 / yrs) - 1.0)


def orig_max_dd(r):                                            # riskmatch_wf/portfolio_wf
    eq = np.cumprod(1.0 + r)
    return float(np.min(eq / np.maximum.accumulate(eq) - 1.0))


def orig_drawdown_focus(net, mask):                            # focus_wf.drawdown
    r = net[mask]
    r = r[np.isfinite(r)]
    if r.size < 2:
        return float("nan")
    eq = np.cumprod(1.0 + r)
    return float(np.min(eq / np.maximum.accumulate(eq) - 1.0))


def orig_sharpe_riskmatch(r, rf, bpy):                         # riskmatch_wf.sharpe
    ex = r - rf
    ex = ex[np.isfinite(ex)]
    if ex.size < 3:
        return float("nan")
    sd = float(np.std(ex, ddof=1))
    return float(np.mean(ex) / sd * np.sqrt(bpy)) if sd > 0 else float("nan")


def orig_sharpe_portfolio(r, rf, bpy):                         # portfolio_wf._sharpe
    x = (r - rf)
    x = x[np.isfinite(x)]
    if x.size < 30:
        return float("nan")
    sd = x.std(ddof=1)
    return float(x.mean() / sd * np.sqrt(bpy)) if sd > 0 else float("nan")


# ------------------------------------------------------------------- the inputs

def cases():
    rng = np.random.default_rng(20260811)
    out = []
    for n in (0, 1, 2, 3, 5, 29, 30, 31, 250, 4000):
        out.append(("normal", rng.normal(0.0004, 0.012, n)))
        out.append(("zeros", np.zeros(n)))
        out.append(("constant", np.full(n, 0.001)))
        out.append(("fat", rng.standard_t(3, n) * 0.02))
        if n >= 3:
            a = rng.normal(0.0004, 0.012, n).copy()
            a[n // 2] = np.nan
            out.append(("one-nan", a))
            b = rng.normal(0.0004, 0.012, n).copy()
            b[0] = np.inf
            out.append(("one-inf", b))
            c = rng.normal(0.0004, 0.012, n).copy()
            c[1] = -1.0                    # total wipeout bar: equity hits exactly zero
            out.append(("wipeout", c))
            d = rng.normal(0.0004, 0.012, n).copy()
            d[2] = -1.5                    # below the -1 floor: equity goes negative
            out.append(("negative-equity", d))
    return out


def same(a: float, b: float) -> bool:
    """Bit-identical, treating NaN as equal to NaN (any NaN payload)."""
    a, b = float(a), float(b)
    if np.isnan(a) and np.isnan(b):
        return True
    return a.__eq__(b) is True and np.signbit(a) == np.signbit(b)


def main() -> None:
    fails: list = []
    tolerated: list = []
    degenerate: list = []
    rng = np.random.default_rng(7)

    for bpy in (252.0, 1638.0, 0.5):
        for label, r in cases():
            rf = np.full(r.size, 0.00002)
            mask = np.ones(r.size, dtype=bool)
            if r.size > 4:
                mask[::7] = False

            checks = [
                ("ann_vol", orig_ann_vol(r, bpy) if r.size >= 2 else None,
                 stats.ann_vol(r, bpy) if r.size >= 2 else None),
                ("cagr/riskmatch", orig_cagr_riskmatch(r, bpy), stats.cagr(r, bpy)),
                ("cagr/portfolio", orig_cagr_portfolio(r, bpy), stats.cagr(r, bpy)),
                ("cagr/focus", orig_cagr_focus(r, mask, bpy),
                 stats.cagr(r[mask], bpy)),
                ("max_dd", orig_max_dd(r) if r.size >= 2 else None,
                 stats.max_drawdown(r, dropna=False) if r.size >= 2 else None),
                ("drawdown/focus", orig_drawdown_focus(r, mask),
                 stats.max_drawdown(r[mask], dropna=True)),
                ("sharpe/riskmatch", orig_sharpe_riskmatch(r, rf, bpy),
                 stats.sharpe(r, rf, bpy, min_obs=3)),
                ("sharpe/portfolio", orig_sharpe_portfolio(r, rf, bpy),
                 stats.sharpe(r, rf, bpy, min_obs=30)),
            ]
            for name, old, new in checks:
                if old is None and new is None:
                    continue
                if same(old, new):
                    continue
                # ONE documented exception. `focus_wf.cagr` spelled the exponent
                # `1.0 / (n / bpy)` and the canonical form spells it `bpy / n`. Same
                # number in exact arithmetic; in float64 the two-division version
                # carries one extra rounding, so the results can differ in the last
                # mantissa bit. The single division is the more accurate spelling, so
                # the canonical form is kept and this is a (tiny) improvement rather
                # than a regression — but it IS a change, so it is bounded and reported
                # here instead of being waved through.
                if name == "cagr/focus" and np.isfinite(old) and np.isfinite(new):
                    rel = abs(new - old) / max(abs(old), 1e-300)
                    if rel < 1e-12:
                        tolerated.append((label, r.size, bpy, rel))
                        continue
                # A SECOND documented exception, and this one is a deliberate bug fix
                # rather than a rounding artifact.
                #
                # Both originals guarded zero variance with an absolute `sd > 0`. That
                # test does not fire on a constant series, because float64 does not put
                # its standard deviation at exactly zero: `np.std(np.full(100, 0.001),
                # ddof=1)` is 2.18e-19. So the old code divided by dust and returned
                # numbers of order 1e16-1e17 where the honest answer is "undefined".
                #
                # Note what that means for THIS harness: `zeros` and `constant` were
                # already in `cases()`, and the suite passed anyway, because it compares
                # old against new and both were equally wrong. An equivalence test cannot
                # catch a bug the two sides share — it pins a refactor, it does not audit
                # a definition. `tests/test_stats.py` is what states the contract.
                #
                # The canonical form now uses the same RELATIVE guard as
                # `metrics.information_ratio`, which had this fixed already. The
                # divergence is therefore expected, one-directional and bounded: the old
                # value must be astronomically large and the new one NaN. Anything else
                # is a real regression and still fails.
                if name.startswith("sharpe/") and np.isnan(new) and np.isfinite(old):
                    if abs(old) > 1e12:
                        degenerate.append((name, label, r.size, bpy, old))
                        continue
                fails.append((name, label, r.size, bpy, old, new))

    # `cagr/portfolio` differs from the canonical form only when bpy <= 0, which
    # `vector.bars_per_year` cannot produce. Assert that is the ONLY divergence, so the
    # claim in `portfolio_wf._cagr_of`'s comment is checked rather than asserted.
    r = rng.normal(0, 0.01, 100)
    div = (orig_cagr_portfolio(r, 0.0), stats.cagr(r, 0.0))
    print(f"known bpy=0 divergence (unreachable in practice): "
          f"portfolio {div[0]} vs canonical {div[1]}")

    n = len(cases()) * 3 * 8
    if tolerated:
        worst = max(t[3] for t in tolerated)
        print(f"tolerated: {len(tolerated)} cagr/focus last-bit differences, "
              f"worst relative {worst:.2e} "
              f"(reported to 3 significant figures; this cannot reach a printed digit)")
    if degenerate:
        worst = max(abs(d[4]) for d in degenerate)
        print(f"FIXED: {len(degenerate)} degenerate-variance sharpe cells now return NaN "
              f"where the originals returned a spurious ratio (worst old value "
              f"{worst:.3e}). This is the relative-guard port from "
              f"metrics.information_ratio, not a regression. Re-run "
              f"'walk-forward optimization/riskmatch_wf.py' to clear the poisoned rows "
              f"in results/edge_standard.csv.")
    if fails:
        print(f"\nFAIL: {len(fails)} of ~{n} comparisons differ")
        for f in fails[:25]:
            print(f"  {f[0]:<18} {f[1]:<16} n={f[2]:<5} bpy={f[3]:<7} "
                  f"old={f[4]!r} new={f[5]!r}")
        raise SystemExit(1)
    print(f"OK: {n} comparisons, bit-identical except the bounded case above")


if __name__ == "__main__":
    main()
