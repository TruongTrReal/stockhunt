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

from config import LEGACY_GATES, LOO_MIN_RETENTION


def information_ratio(net: np.ndarray, bench_net: np.ndarray, bpy: float) -> float:
    """Annualised IR of `net` against `bench_net`, both bar-level return series."""
    diff = np.asarray(net, dtype="float64") - np.asarray(bench_net, dtype="float64")
    diff = diff[np.isfinite(diff)]
    if diff.size < 3:
        return float("nan")
    sd = float(np.std(diff, ddof=1))          # ddof=1 everywhere; numpy defaults to 0
    mean = float(np.mean(diff))
    # A `sd <= 0` test is not enough, and the difference is not academic.
    #
    # When a rule holds exactly the benchmark position — always long — its excess series
    # is the cost charged each bar, a CONSTANT. In exact arithmetic that has sd 0 and the
    # guard fires. In float64 the cost terms accumulate ~1e-16 of noise, so `sd` is a
    # denormal-ish positive number, the guard passes, and `mean / sd` returns an IR of
    # order 1e15.
    #
    # It stayed hidden for three studies because 20 assets x 24 folds rarely produced a
    # degenerate cell. At 704 assets x 54 folds it fired 1,600 times in 49,950 and
    # poisoned `mean_is_ir` to -2.1e13 — a mean, so a single row was enough. Only the
    # cost-bearing scenarios were affected; at `gross` the excess is identically zero and
    # 0/0 was already caught.
    #
    # The test is therefore relative: if the tracking error is negligible next to the
    # size of the differences themselves, the series is constant and its IR is undefined.
    scale = float(np.max(np.abs(diff))) if diff.size else 0.0
    if not np.isfinite(sd) or sd <= 0 or sd <= 1e-10 * max(scale, abs(mean)):
        return float("nan")
    return float(mean / sd * np.sqrt(bpy))


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
    # Both points identical means the sheet ran ONE cost scenario, so there is no second
    # point to locate a crossing with and the answer is unknown — not infinite.
    #
    # This matters since 1d and 4h collapsed to `gross`: the naive arithmetic returns
    # `inf` on 17% of rows, and `inf` reads as "survives unlimited cost" when the truth is
    # "cost was never charged". A gate on it would then pass trivially, on exactly the
    # rows with positive gross IR. `cost_drag` is where the fee information lives now.
    if ir_gross == ir_headline:
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
    # Diagnostics, not a verdict. `aggregate` used to call `apply_gates` here, which is
    # how every IR-based sweep came to stamp a pass/fail on rows whose inputs cannot
    # support one. The only place a verdict exists now is
    # `walk-forward optimization/results/edge_standard.csv`.
    row.update(apply_legacy_gates(row))
    return row


def apply_legacy_gates(row: dict) -> dict:
    """The retired four IR gates, as **diagnostics only**.

    Renamed from `apply_gates` and no longer called by `aggregate`, because it was the
    thing that let every IR-based sweep stamp a verdict on rows it could not really judge.
    The four are wrong about what they measure — IR against buy-and-hold scores capital
    deployment as much as skill — so the numbers stay useful and the pass/fail does not.

    Output keys are prefixed `legacy_` so they can never be mistaken for the standard, and
    so a stale `gates_passed` column in an old CSV is visibly a different thing.
    """
    value_of = {
        "ir": row.get("ir_net"),
        "breadth": row.get("ir_hit_rate"),
        "headroom": row.get("headroom"),
        "t": row.get("t_stat"),
    }
    out = {}
    passed = 0
    for gate in LEGACY_GATES:
        v = value_of[gate["key"]]
        ok = bool(np.isfinite(v) and v >= gate["min"]) if v is not None else False
        out[f"legacy_gate_{gate['key']}"] = ok
        passed += int(ok)
    out["legacy_passed"] = passed
    loo = row.get("loo_retention")
    out["legacy_gate_loo"] = bool(loo is not None and np.isfinite(loo)
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


# ------------------------------------------------------- deflated Sharpe ratio
#
# `noise_ceiling` above answers "how high would the best of N worthless rules score?" and
# it answers it under two assumptions that this project's own data violates:
#
#   * returns are normal, so the Sharpe estimator has SE ~= 1/sqrt(T);
#   * the N trials are independent draws from one distribution.
#
# The first is the expensive one. Every rule on the leaderboard that de-risks — `ibs`,
# `rsi2`, every mean-reversion entry — is short volatility by construction: it holds
# through calm and sits out crashes, which produces a return series with negative skew
# and fat tails. The Sharpe of such a series is *harder* to estimate than a normal one,
# so the honest error bar is wider than 1/sqrt(T) and the naive t-stat is overstated in
# exactly the place this repo keeps finding its most promising rows. A metric that cannot
# see that is a metric that will eventually promote one of them.
#
# The Bailey / Lopez de Prado deflated Sharpe ratio fixes both at once, and it returns a
# probability rather than a threshold, which is what makes it directly reportable.


def sharpe_se(sharpe: float, skew: float, kurt: float, n_obs: int) -> float:
    """Mertens' standard error of a Sharpe estimate under non-normal returns.

    `sharpe` is per-observation, NOT annualised — mixing the two is the single easiest
    way to get this wrong by a factor of sqrt(bars per year). `kurt` is raw kurtosis
    (3.0 for a normal), not excess.

        SE = sqrt( (1 - g3*SR + (g4-1)/4 * SR^2) / (T-1) )

    Under normality (g3 = 0, g4 = 3) this collapses to the familiar sqrt(1/(T-1)). The
    skew term is the one that bites: negative skew *raises* the SE, so a strategy that
    wins small and often and loses big and rarely is measured less precisely than its bar
    count suggests. That is the correct penalty for selling insurance.
    """
    if n_obs < 3 or not np.isfinite(sharpe):
        return float("nan")
    var = (1.0 - skew * sharpe + (kurt - 1.0) / 4.0 * sharpe ** 2) / (n_obs - 1)
    return float(np.sqrt(var)) if var > 0 else float("nan")


def probabilistic_sharpe(sharpe: float, skew: float, kurt: float, n_obs: int,
                         benchmark: float = 0.0) -> float:
    """P(true Sharpe > `benchmark`) given the observed one. All per-observation.

    This is one test, honestly conducted. It becomes the deflated ratio below only once
    `benchmark` is raised from 0 to what the best of N trials reaches by luck.
    """
    from statistics import NormalDist
    se = sharpe_se(sharpe, skew, kurt, n_obs)
    if not np.isfinite(se) or se <= 0:
        return float("nan")
    return float(NormalDist().cdf((sharpe - benchmark) / se))


def expected_max_sharpe(n_trials: int, sharpe_std: float) -> float:
    """Sharpe the best of `n_trials` genuinely worthless rules reaches by luck.

    The Gumbel approximation to the expected maximum of `n_trials` independent normals:

        E[max] ~= sigma * ( (1 - g) * Phi^-1(1 - 1/N) + g * Phi^-1(1 - 1/(N*e)) )

    with `g` the Euler-Mascheroni constant. It is more accurate at small N than the
    single-term `Phi^-1(1 - 1/(N+1))` that `noise_ceiling` uses.

    `sharpe_std` is the **cross-sectional standard deviation of the trial Sharpes you
    actually ran**, not an assumed 1.0, and this is where the estimate earns its keep.
    A leaderboard of 416 near-identical always-long variants has a tiny spread and a
    correspondingly low bar; 416 genuinely different rules have a wide spread and a high
    one. Feeding it the real dispersion is how the correction adapts to the fact that
    this project's trials are heavily correlated rather than independent.
    """
    from math import e
    from statistics import NormalDist
    if n_trials < 2 or not np.isfinite(sharpe_std) or sharpe_std <= 0:
        return float("nan")
    g = 0.5772156649015329
    nd = NormalDist()
    z1 = nd.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = nd.inv_cdf(1.0 - 1.0 / (n_trials * e))
    return float(sharpe_std * ((1.0 - g) * z1 + g * z2))


def deflated_sharpe(returns: np.ndarray, n_trials: int, sharpe_std: float,
                    bpy: float | None = None) -> dict:
    """The full DSR for one return series, given the search that produced it.

    `returns` is the bar-level series of the strategy (in excess of cash if you want the
    Sharpe to mean what it usually means — this function does not subtract anything).
    `n_trials` is how many candidates the search examined, and `sharpe_std` their
    cross-sectional Sharpe dispersion; `dsr_from_leaderboard` derives both for you.

    Returns per-observation and annualised Sharpes, the deflated benchmark, and `dsr` —
    the probability the true Sharpe exceeds what the search would have produced from
    noise alone. **Read it as: below 0.95, the row is not evidence.** Unlike a t-stat it
    already contains the trial count and the shape of the distribution, so it needs no
    further correction and none should be applied on top.
    """
    r = np.asarray(returns, dtype="float64")
    r = r[np.isfinite(r)]
    n = r.size
    if n < 30:
        return {"dsr": float("nan"), "n_obs": n}

    sd = r.std(ddof=1)
    sr = float(r.mean() / sd) if sd > 0 else float("nan")
    z = (r - r.mean()) / sd if sd > 0 else r * np.nan
    skew = float((z ** 3).mean())
    kurt = float((z ** 4).mean())          # raw, not excess: 3.0 is normal

    sr_star = expected_max_sharpe(n_trials, sharpe_std)
    out = {
        "n_obs": n,
        "sharpe_bar": sr,
        "sharpe_ann": sr * np.sqrt(bpy) if bpy else float("nan"),
        "skew": skew,
        "kurtosis": kurt,
        "sharpe_se": sharpe_se(sr, skew, kurt, n),
        "n_trials": n_trials,
        "sharpe_std": sharpe_std,
        "sr_star_bar": sr_star,
        "sr_star_ann": sr_star * np.sqrt(bpy) if bpy else float("nan"),
        "psr": probabilistic_sharpe(sr, skew, kurt, n, 0.0),
        "dsr": probabilistic_sharpe(sr, skew, kurt, n, sr_star),
    }
    out["dsr_pass"] = bool(np.isfinite(out["dsr"]) and out["dsr"] >= 0.95)
    return out


def dsr_from_leaderboard(returns: np.ndarray, trial_sharpes, bpy: float | None = None
                         ) -> dict:
    """`deflated_sharpe` with the trial count and dispersion read off the search itself.

    `trial_sharpes` is every candidate's **annualised** Sharpe from the same search — the
    whole column, not the survivors. Filtering it to the good rows before calling this
    understates the dispersion and therefore understates the bar, which is the precise
    mistake the deflation exists to prevent.
    """
    s = np.asarray(list(trial_sharpes), dtype="float64")
    s = s[np.isfinite(s)]
    if s.size < 2 or not bpy:
        return {"dsr": float("nan"), "n_trials": int(s.size)}
    # Dispersion must be in the same per-observation units as the series' own Sharpe.
    return deflated_sharpe(returns, int(s.size), float(s.std(ddof=1) / np.sqrt(bpy)), bpy)


def apply_edge_standard(row: dict) -> dict:
    """The 2026-08-08 standard: booleans per criterion, plus the count and a verdict.

    Deliberately separate from `apply_gates` rather than replacing it. Every
    `gates_passed` column already on disk was computed against `config.GATES`, and
    rewriting that in place would change the meaning of results nobody re-ran. A row can
    carry both and they can disagree — for `ibs` they do, and the disagreement is the
    finding.

    `n_folds` gates the verdict, not the criteria. A sheet with six folds can still be
    scored; it just cannot be *believed*, so it returns `verdict="underpowered"` instead
    of a pass or a fail. Reporting "0 passed" on a sheet that could not have detected the
    effect is how a null gets manufactured.
    """
    from config import (EDGE_MIN_EXPOSURE, EDGE_MIN_FOLDS,
                        EDGE_MIN_FOLD_FRAC, EDGE_STANDARD)

    out, passed = {}, 0
    for c in EDGE_STANDARD:
        v = row.get(f"edge_{c['key']}", row.get(c["key"]))
        ok = bool(v is not None and np.isfinite(v) and v >= c["min"])
        # The T criterion's 2.0 is a SINGLE-TEST threshold, and nothing else in this
        # function knows how many candidates were searched. That is how, on 2026-08-10,
        # one row out of 1,974 rankable trials satisfied it at t = 3.014 and came back
        # PASS — the first in this repo's history — when the Bonferroni bar for that many
        # trials is 4.21 and the noise ceiling at 20.5 years is IR +0.726 against an
        # observed +0.201.
        #
        # A standard that cannot see its own trial count will keep producing one winner
        # per few thousand rows, forever, and every one of them will look like a finding.
        # `n_trials` is passed in by the caller; without it the bar stays at 2.0 and the
        # row is flagged so nobody mistakes an uncorrected pass for a corrected one.
        if c["key"] == "t" and ok:
            # An explicit bar wins, and it is the better one wherever it exists.
            # `bonferroni_t` divides alpha by the trial COUNT, which is the right
            # correction only if those trials are independent — and a panel of 231
            # indicators over one universe, plus pairs built from the same legs, is
            # nothing like independent. `portfolio_wf._t_bar` measures the bar instead,
            # by sign-flip permutation of the per-fold edges across the whole panel at
            # once (Westfall & Young max-T), which reads the correlation off the data.
            # Bonferroni remains the fallback for callers that cannot supply one; it errs
            # strict, so a pass under it is a pass under anything.
            explicit = row.get("t_bar_override")
            if explicit is not None and np.isfinite(explicit) and explicit > 0:
                ok = bool(v >= float(explicit))
                out["edge_t_bar_corrected"] = float(explicit)
                out["edge_t_bar_source"] = row.get("t_bar_source", "explicit")
                out["edge_t_uncorrected_pass"] = True
            elif (n_trials := row.get("n_trials")) is not None and np.isfinite(
                    n_trials) and n_trials > 1:
                bar = bonferroni_t(int(n_trials))
                ok = bool(v >= bar)
                out["edge_t_bar_corrected"] = bar
                out["edge_t_bar_source"] = "bonferroni"
                out["edge_t_uncorrected_pass"] = True
            else:
                out["edge_t_bar_corrected"] = float("nan")
                out["edge_t_bar_source"] = "uncorrected"
                out["edge_t_uncorrected_pass"] = True
        out[f"edge_gate_{c['key']}"] = ok
        passed += int(ok)
    out["edge_passed"] = passed
    out["edge_n"] = len(EDGE_STANDARD)

    folds = row.get("n_folds_scored", row.get("n_folds"))
    powered = bool(folds is not None and np.isfinite(folds) and folds >= EDGE_MIN_FOLDS)
    out["edge_powered"] = powered

    # Rankability first. A rule that holds a position on almost no bars produces a
    # near-zero-variance difference series, and a ratio over that is noise with a large
    # magnitude — `CDLKICKING` topped an 804-row sheet on exactly this. Such a row is
    # excluded from the ranking rather than given a verdict it cannot support.
    exp = row.get("exposure")
    ffrac = row.get("fold_coverage")
    rankable_edge = bool(
        exp is not None and np.isfinite(exp) and exp >= EDGE_MIN_EXPOSURE
        and (ffrac is None or (np.isfinite(ffrac) and ffrac >= EDGE_MIN_FOLD_FRAC)))
    out["edge_rankable"] = rankable_edge

    out["edge_verdict"] = ("unrankable" if not rankable_edge
                           else "underpowered" if not powered
                           else "PASS" if passed == len(EDGE_STANDARD)
                           else "fail")
    return out


def rankable(row: dict, min_coverage: float) -> bool:
    """A rule must produce a valid IR on enough of its assets to be ranked.

    Without this, rules that sit flat on most names win on a couple of assets' worth of
    noise — and a ratio objective rewards doing nothing, so this is a constraint on the
    objective rather than a cosmetic filter.
    """
    return row["n_assets"] > 0 and row["n_ir"] >= min_coverage * row["n_assets"]
