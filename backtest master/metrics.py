"""Information ratio, the four acceptance gates, and the leave-one-out check.

Ranking is on **information ratio against buy-and-hold on the same asset**, not raw
Sharpe and not excess Sharpe. Raw Sharpe in a rising market mostly measures how
long-biased a rule is — that is how the first sweep in this repo manufactured "winners"
that were really just beta. Excess Sharpe fixes the direction but discards the
benchmark correlation that decides whether a difference is detectable at all:
`SE(dSharpe) = SE(SR) * sqrt(2(1-rho))`, so a rule that hugs its benchmark can show a
small edge at high confidence while a rule that wanders shows a large one at none.
The IR keeps that information because it is computed on the *difference* series.

The governing constraint, and the reason most of this file exists to report rather than
to optimise: **t = IR x sqrt(years)**. On an 11-year daily sample sqrt(11) = 3.3, so
even a genuinely good IR of 0.5 reaches only t = 1.7. A system can be real and still be
unprovable here. Reporting that honestly is the job.
"""

from __future__ import annotations

import numpy as np

from config import GATES, LOO_MIN_RETENTION


def information_ratio(net: np.ndarray, bench_net: np.ndarray, bpy: float) -> float:
    """Annualised IR of `net` against `bench_net`, both bar-level return series."""
    diff = np.asarray(net, dtype="float64") - np.asarray(bench_net, dtype="float64")
    diff = diff[np.isfinite(diff)]
    if diff.size < 3:
        return float("nan")
    sd = float(np.std(diff, ddof=1))          # ddof=1 everywhere; numpy defaults to 0
    if not np.isfinite(sd) or sd <= 0:
        return float("nan")
    return float(np.mean(diff) / sd * np.sqrt(bpy))


def cost_headroom(ir_gross: float, ir_headline: float) -> float:
    """How many multiples of the *real* fee schedule the edge survives.

    IR falls essentially linearly in cost — the charge enters the mean of the difference
    series linearly and barely touches its variance — so two points locate the crossing.
    Taking those two points as "no fees" and "the actual venue fee schedule" makes the
    answer directly meaningful: **1.0 means the edge dies exactly at real cost, 3.0 means
    it survives three times what you actually pay.**

    That is a better quantity than a breakeven expressed in basis points, because the fee
    schedule is no longer a single bps number — it is commission plus half-spread plus
    sell-side regulatory fees plus short borrow, each charged on something different.

    Returns 0.0 when the rule is unprofitable before any fees at all: there is no cost it
    could have survived.
    """
    if not np.isfinite(ir_gross) or not np.isfinite(ir_headline):
        return float("nan")
    if ir_gross <= 0:
        return 0.0
    drop = ir_gross - ir_headline
    if drop <= 0:
        return float("inf")                   # fees did not hurt it — inspect the rule
    return float(ir_gross / drop)


def leave_one_out(per_asset_ir: dict[str, float]) -> tuple[float, str | None]:
    """(retained fraction of mean IR after dropping the best asset, that asset).

    A strong average carried by one or two names is a fitted result, not a broad one.
    The top-20 study measured exactly this: dropping NVDA cost 34% of the IR.
    """
    vals = {k: v for k, v in per_asset_ir.items() if np.isfinite(v)}
    if len(vals) < 3:
        return float("nan"), None
    best = max(vals, key=vals.get)
    full = float(np.mean(list(vals.values())))
    without = float(np.mean([v for k, v in vals.items() if k != best]))
    if full <= 0:
        return float("nan"), best
    return without / full, best


def aggregate(per_asset: dict[str, dict], years: float,
              ir_gross: float, ir_headline: float) -> dict:
    """Roll per-asset results into the leaderboard row for one rule under one scenario.

    `per_asset` maps symbol -> {"ir": float, ...}. `ir_gross` and `ir_headline` are that
    rule's mean out-of-sample IR with no fees and with the real fee schedule; they locate
    the cost headroom and are the same for every scenario row of a given rule.
    """
    irs = {s: r["ir"] for s, r in per_asset.items()}
    finite = [v for v in irs.values() if np.isfinite(v)]
    n_assets = len(per_asset)

    ir_mean = float(np.mean(finite)) if finite else float("nan")
    hit_rate = float(np.mean([v > 0 for v in finite])) if finite else float("nan")
    t_stat = ir_mean * np.sqrt(years) if np.isfinite(ir_mean) and years > 0 else float("nan")

    headroom = cost_headroom(ir_gross, ir_headline)
    loo_ret, loo_asset = leave_one_out(irs)

    row = {
        "ir_net": ir_mean,
        "ir_hit_rate": hit_rate,
        "ir_gross": ir_gross,
        "headroom": headroom,
        "t_stat": t_stat,
        "loo_retention": loo_ret,
        "loo_dropped": loo_asset,
        "n_assets": n_assets,
        "n_ir": len(finite),
        "years": years,
    }
    row.update(apply_gates(row))
    return row


def apply_gates(row: dict) -> dict:
    """Boolean per gate, plus the count. Decided in Python, never in the browser."""
    value_of = {
        "ir": row.get("ir_net"),
        "breadth": row.get("ir_hit_rate"),
        "headroom": row.get("headroom"),
        "t": row.get("t_stat"),
    }
    out = {}
    passed = 0
    for gate in GATES:
        v = value_of[gate["key"]]
        ok = bool(np.isfinite(v) and v >= gate["min"]) if v is not None else False
        out[f"gate_{gate['key']}"] = ok
        passed += int(ok)
    out["gates_passed"] = passed
    loo = row.get("loo_retention")
    out["gate_loo"] = bool(loo is not None and np.isfinite(loo)
                           and loo >= LOO_MIN_RETENTION)
    return out


def se_ir(years: float) -> float:
    """Standard error of an annualised IR estimated over `years` of data.

    For a difference series with T observations per year, the annualised IR has
    SE ~= 1/sqrt(years) — it depends on the *length* of the sample, not on how many bars
    that length is chopped into. This is why running the same span at 1-minute instead
    of daily buys no significance: 390x the bars, identical sqrt(years).
    """
    return float("nan") if not years or years <= 0 else 1.0 / np.sqrt(years)


def noise_ceiling(n_candidates: int, years: float) -> float:
    """The IR the *best* of `n_candidates` worthless rules would reach by luck.

    Searching N candidates and reporting the winner is an extreme-value problem, not a
    single test: the maximum of N standard normals sits near Phi^-1(1 - 1/(N+1)). Any
    reported best-IR below this line is indistinguishable from noise no matter how
    healthy it looks, and any comparison that omits it is quietly cheating.

    Concretely, on this project's data: 231 candidates over 10.6 equity test-years gives
    a ceiling near +0.9, while 227 candidates over 2.6 crypto test-years gives near +1.8
    — so a crypto rule needs to look twice as good as an equity rule to mean the same
    thing. Reporting the raw maximum without this correction would make the shorter,
    noisier sample look like the more promising one.
    """
    from statistics import NormalDist
    se = se_ir(years)
    if not np.isfinite(se) or n_candidates < 2:
        return float("nan")
    z = NormalDist().inv_cdf(1.0 - 1.0 / (n_candidates + 1))
    return float(se * z)


def bonferroni_t(n_candidates: int, alpha: float = 0.05) -> float:
    """Two-sided t threshold for `n_candidates` simultaneous tests."""
    from statistics import NormalDist
    if n_candidates < 1:
        return float("nan")
    return float(NormalDist().inv_cdf(1.0 - alpha / (2.0 * n_candidates)))


def rankable(row: dict, min_coverage: float) -> bool:
    """A rule must produce a valid IR on enough of its assets to be ranked.

    Without this, rules that sit flat on most names win on a couple of assets' worth of
    noise — and a ratio objective rewards doing nothing, so this is a constraint on the
    objective rather than a cosmetic filter.
    """
    return row["n_assets"] > 0 and row["n_ir"] >= min_coverage * row["n_assets"]
