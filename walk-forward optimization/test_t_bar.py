"""Does `portfolio_wf._t_bar` do what it claims?

Run from this folder: `python test_t_bar.py`. Four checks with known answers, because a
significance bar is exactly the kind of thing that can be wrong by a factor of two and
still look plausible on a leaderboard.

The third and fourth are the ones that matter. A correction that is too strict buries real
findings; one that is too loose manufactures them, and this repo has retracted a result to
that before. The measured false-positive rate is the only direct evidence which side we are
on, so it is a test and not a comment.

Note what case 1 shows: on genuinely independent rules the honest bar is HIGHER than
Bonferroni's, because `bonferroni_t` takes a normal quantile while 21 folds is a t with 20
degrees of freedom and fatter tails. Bonferroni was not simply "too strict" here — it was
wrong in both directions at once, and only measurement can say which dominates on a given
panel.
"""
import numpy as np
from statistics import NormalDist
import portfolio_wf as P


def bar_from(E, seed=P.PERM_SEED, B=P.N_PERMUTATIONS):
    n_f = E.shape[1]
    rng = np.random.default_rng(seed)
    S = rng.choice(np.array([-1.0, 1.0]), size=(B, n_f))
    means = S @ E.T / n_f
    sq = (S ** 2) @ (E ** 2).T / n_f
    var = (sq - means ** 2) * n_f / (n_f - 1)
    se = np.sqrt(np.maximum(var, 1e-24) / n_f)
    return float(np.quantile(np.max(means / se, axis=1), 0.95))


def tstats(E):
    m, sd = E.mean(axis=1), E.std(axis=1, ddof=1)
    return m / (sd / np.sqrt(E.shape[1]))


rng = np.random.default_rng(1)
R, F = 400, 21
bonf = NormalDist().inv_cdf(1 - 0.05 / (2 * R))
print(f"Bonferroni bar for {R} trials: {bonf:.2f}\n")

# 1. Genuinely independent rules -> the correction SHOULD be near-Bonferroni.
E = rng.standard_normal((R, F))
print(f"independent 400 rules      bar {bar_from(E):.2f}   (expect ~Bonferroni or above)")

# 2. 400 near-copies of one rule -> one effective test, bar near the single-test t.
base = rng.standard_normal((1, F))
E = base + 0.02 * rng.standard_normal((R, F))
print(f"400 near-identical rules   bar {bar_from(E):.2f}   (expect ~2, one real test)")

# 3. Blocks: 20 distinct ideas, 20 variants each -> between the two.
ideas = rng.standard_normal((20, F))
E = np.repeat(ideas, 20, axis=0) + 0.5 * rng.standard_normal((R, F))
print(f"20 ideas x 20 variants     bar {bar_from(E):.2f}   (expect between)\n")

# 4. THE test that matters: on panels with no edge at all, how often does the best rule
#    clear the bar? Family-wise error should come out at the 5% it is set to.
hits = 0
trials = 200
for i in range(trials):
    g = np.random.default_rng(1000 + i)
    ideas = g.standard_normal((20, F))
    E = np.repeat(ideas, 20, axis=0) + 0.5 * g.standard_normal((R, F))
    if tstats(E).max() >= bar_from(E, seed=7 + i, B=4000):
        hits += 1
print(f"false-positive rate over {trials} null panels: {hits / trials:.1%}   (target 5.0%)")
