"""Stage 2: how many INDEPENDENT tests were there, and do the leaders survive at
proper permutation resolution?

Stage 1 reported two things that cannot both be taken at face value. The top-20 book
showed |t|>2 on 15.1% of tests against 4.5% expected, which looks like a large excess;
and nothing survived FDR, which looks like nothing is there. Both readings are
artefacts of how that run was built, and this script repairs each:

  * RESOLUTION.  24 permutations floor the p-value at 1/25 = 0.04. Benjamini-Hochberg
    over 252 tests needs roughly 0.0004 at the top rank, so no result could have
    survived regardless of how strong it was. The leaders are re-run here with 2000
    shifts each, which is the resolution the correction actually requires.

  * DEPENDENCE.  The 84 signals are not 84 questions. C_lowvol, C_downside_vol,
    C_idio_vol and C_vol_of_vol are four encodings of one volatility effect, and the
    stage-1 leaderboard is mostly that cluster repeating itself. The effective number
    of independent tests is estimated from the eigenvalue spectrum of the IC
    correlation matrix - the same diagnostic weekly_variants.py used to find that 304
    daily signals were worth 1.35 independent bets. An excess tail means nothing until
    it is compared against the number of genuinely distinct questions asked.

If the fat tail on top-20 is one volatility cluster measured eight ways, it is one
finding with t~2.8 out of a handful of real tests, not 38 findings out of 252.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import UNIVERSE, log_trial, split
from hypotheses import build
from mass_ic import HORIZONS, load, pit_mask, row_ic, tstat, bh_fdr

N_PERM = 2000
TOP_K = 15


def ic_panel(sig: pd.DataFrame, fwd_rank: np.ndarray, idx, cols, mask, h):
    s = sig.reindex(index=idx, columns=cols).where(pd.DataFrame(mask, index=idx, columns=cols))
    sr = s.rank(axis=1).to_numpy()[::h]
    n = min(len(sr), len(fwd_rank))
    return row_ic(sr[:n], fwd_rank[:n]), sr[:n]


def effective_tests(ic_matrix: pd.DataFrame) -> float:
    """Effective independent tests from the eigenvalue spectrum of the IC correlation
    matrix. Sum(lambda)^2 / Sum(lambda^2) — 1.0 when everything is one factor, N when
    all N are orthogonal."""
    c = ic_matrix.corr().to_numpy()
    c = np.nan_to_num(c, nan=0.0)
    ev = np.linalg.eigvalsh(c)
    ev = ev[ev > 0]
    return float(ev.sum() ** 2 / (ev ** 2).sum())


def main() -> None:
    close, opn, high, low, vol = load()
    bench = close["SPY"] if "SPY" in close.columns else None
    sig_all = build(close, opn, high, low, vol, bench)
    prior = pd.read_csv("edge/mass_ic.csv")

    cols_all = list(close.columns)
    books = {"top-20": [t for t in UNIVERSE if t in cols_all],
             "S&P-PIT": [t for t in cols_all if t != "SPY"]}

    rng = np.random.default_rng(999)
    out_rows = []
    for bname, bcols in books.items():
        c = close[bcols]
        rets_idx, _ = split(c.pct_change().dropna(how="all"))
        idx = rets_idx.index
        cc = c.reindex(idx)
        mask = (pit_mask(idx, bcols) if bname == "S&P-PIT"
                else np.ones((len(idx), len(bcols)), bool))
        maskdf = pd.DataFrame(mask, index=idx, columns=bcols)

        fwd_rank = {h: (cc.shift(-h) / cc - 1.0).where(maskdf).rank(axis=1).to_numpy()[::h]
                    for h in HORIZONS}

        # --- dependence: IC series for every signal at h=21, then effective N -----
        panels = {}
        for sname, sfull in sig_all.items():
            ic, _ = ic_panel(sfull, fwd_rank[21], idx, bcols, mask, 21)
            if np.isfinite(ic).sum() > 20:
                panels[sname] = ic
        L = min(len(v) for v in panels.values())
        icdf = pd.DataFrame({k: v[:L] for k, v in panels.items()})
        n_eff = effective_tests(icdf)
        print(f"\n### {bname}: {len(panels)} signals -> effective independent tests "
              f"= {n_eff:.2f}  ({n_eff / len(panels):.1%} of nominal)")

        # correlation inside the volatility cluster, to name the redundancy explicitly
        cfam = [k for k in icdf.columns if k.startswith("C_")]
        if len(cfam) > 1:
            cc_ = icdf[cfam].corr().to_numpy()
            iu = np.triu_indices(len(cfam), 1)
            print(f"    C-family (volatility, {len(cfam)} signals): "
                  f"mean pairwise IC corr {np.nanmean(cc_[iu]):+.3f}, "
                  f"effective tests {effective_tests(icdf[cfam]):.2f}")
        afam = [k for k in icdf.columns if k.startswith("A_")]
        if len(afam) > 1:
            print(f"    A-family (momentum, {len(afam)} signals): "
                  f"effective tests {effective_tests(icdf[afam]):.2f}")

        # --- resolution: re-run the leaders with 2000 shifts ---------------------
        lead = (prior[prior.book == bname]
                .reindex(prior[prior.book == bname]["t"].abs().sort_values(
                    ascending=False).index).head(TOP_K))
        print(f"    re-running top {len(lead)} at {N_PERM} permutations "
              f"(BH needs p<={0.10 / n_eff:.4f} at rank 1 given {n_eff:.1f} effective tests)")
        for _, row in lead.iterrows():
            sname, h = row["signal"], int(row["horizon"])
            ic, sr = ic_panel(sig_all[sname], fwd_rank[h], idx, bcols, mask, h)
            t_obs = tstat(ic)
            fr = fwd_rank[h][:len(sr)]
            nulls = np.empty(N_PERM)
            lo = max(252 // h, 5)
            for i in range(N_PERM):
                off = int(rng.integers(lo, max(len(sr) - lo, lo + 1)))
                nulls[i] = tstat(row_ic(np.roll(sr, off, axis=0), fr))
            nulls = nulls[np.isfinite(nulls)]
            p = float((np.abs(nulls) >= abs(t_obs)).mean())
            out_rows.append({"book": bname, "signal": sname, "horizon": h, "t": t_obs,
                             "p_perm": max(p, 1.0 / (len(nulls) + 1)),
                             "null_sd": float(nulls.std()),
                             "z_vs_null": t_obs / nulls.std() if nulls.std() > 0 else np.nan,
                             "n_eff": n_eff})

    r = pd.DataFrame(out_rows)
    r.to_csv("edge/mass_ic2.csv", index=False)
    print("\n" + "=" * 78)
    for bname in books:
        sub = r[r.book == bname].copy()
        if sub.empty:
            continue
        n_eff = float(sub["n_eff"].iloc[0])
        sub["keep_fdr"] = bh_fdr(sub["p_perm"].to_numpy(), q=0.10)
        # Bonferroni on the EFFECTIVE number of tests, not the nominal one.
        sub["keep_bonf_eff"] = sub["p_perm"] <= 0.05 / max(n_eff, 1.0)
        print(f"\n### {bname} (effective tests {n_eff:.1f})")
        print(sub[["signal", "horizon", "t", "z_vs_null", "p_perm",
                   "keep_fdr", "keep_bonf_eff"]].to_string(
            index=False, float_format=lambda v: f"{v:.4f}"))
        print(f"  survive BH-FDR q=0.10: {int(sub['keep_fdr'].sum())}   "
              f"survive Bonferroni on {n_eff:.1f} effective tests: "
              f"{int(sub['keep_bonf_eff'].sum())}")

    log_trial("MASS-IC2", "dependence + high-resolution permutation", "TRAIN",
              f"{len(r)} leaders re-tested at {N_PERM} shifts",
              {"n_eff_top20": float(r[r.book == 'top-20']['n_eff'].iloc[0]) if (r.book == 'top-20').any() else np.nan,
               "min_p": float(r["p_perm"].min())}, "see report")


if __name__ == "__main__":
    main()
