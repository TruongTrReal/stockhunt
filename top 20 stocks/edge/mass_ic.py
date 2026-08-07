"""Mass test: every hypothesis vs a permutation null, with FDR control.

With a search this large the question "did the winner clear the bar?" is the wrong one,
because the bar itself moves with the number of tests. Two better questions are asked
here instead:

  1. IS THE DISTRIBUTION FATTER THAN CHANCE?  Run every signal, then run every signal
     again against a null built by circularly shifting the signal panel in time by a
     random offset of at least a year. That destroys any predictive link to returns
     while preserving BOTH the cross-sectional structure and the per-name time-series
     persistence of the signal - which a naive shuffle-the-names null would wreck, and
     which matters because persistent signals produce autocorrelated IC series and
     therefore inflated t-statistics. If the observed |t| distribution is no fatter
     than the null's, the whole search found nothing and no individual "winner" is real.

  2. WHICH SURVIVE FDR?  Benjamini-Hochberg on the p-values, which controls the expected
     proportion of false discoveries among the things called significant. That is the
     right correction for a large screen; family-wise methods at this scale would reject
     everything including real effects.

Forward windows are sampled non-overlapping. Ranks are computed once per panel and the
null reuses them, so the approximation (ranking over the full row rather than the
finite intersection) is applied identically to real and null and cannot bias the
comparison in either direction.
"""

from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

from common import STOCKHUNT, UNIVERSE, log_trial, split
from hypotheses import build, families

CACHE = STOCKHUNT / "test research" / "data" / "cache"
PIT = STOCKHUNT / "test research" / "data" / "sp500_pit_membership.csv"
HORIZONS = [1, 5, 21]
N_SHIFT = 24          # null draws per signal-horizon
MIN_NAMES = 20


def load():
    c, o, h, l, v = {}, {}, {}, {}, {}
    for p in sorted(glob.glob(str(CACHE / "*.parquet"))):
        t = os.path.basename(p)[:-8]
        df = pd.read_parquet(p)
        if len(df) < 250:
            continue
        df.index = pd.DatetimeIndex(df.index).tz_localize(None)
        c[t], o[t], h[t], l[t], v[t] = (df["Close"], df["Open"], df["High"],
                                        df["Low"], df["Volume"])
    f = lambda d: pd.DataFrame(d).sort_index()
    return f(c), f(o), f(h), f(l), f(v)


def pit_mask(idx, cols):
    m = pd.read_csv(PIT)
    m["date"] = pd.to_datetime(m["date"])
    m = m[m.ticker.isin(cols)]
    wide = (m.assign(v=True).pivot_table(index="date", columns="ticker", values="v",
                                         aggfunc="first")
            .reindex(columns=cols).reindex(idx.union(pd.DatetimeIndex(m.date.unique()))).ffill())
    return wide.reindex(idx).fillna(False).to_numpy()


def row_ic(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Per-row correlation of two rank panels, NaN-masked, fully vectorised."""
    m = np.isfinite(a) & np.isfinite(b)
    n = m.sum(1).astype(float)
    a = np.where(m, a, 0.0)
    b = np.where(m, b, 0.0)
    sa, sb = a.sum(1), b.sum(1)
    cov = (a * b).sum(1) - sa * sb / np.where(n > 0, n, np.nan)
    va = (a * a).sum(1) - sa * sa / np.where(n > 0, n, np.nan)
    vb = (b * b).sum(1) - sb * sb / np.where(n > 0, n, np.nan)
    ic = cov / np.sqrt(np.where(va > 0, va, np.nan) * np.where(vb > 0, vb, np.nan))
    return np.where(n >= MIN_NAMES, ic, np.nan)


def tstat(ic: np.ndarray) -> float:
    ic = ic[np.isfinite(ic)]
    if len(ic) < 20:
        return np.nan
    s = ic.std(ddof=1)
    return float(ic.mean() / s * np.sqrt(len(ic))) if s > 0 else np.nan


def bh_fdr(p: np.ndarray, q: float = 0.10):
    """Benjamini-Hochberg. Returns boolean keep-mask at false discovery rate q."""
    ok = np.isfinite(p)
    idx = np.where(ok)[0]
    order = idx[np.argsort(p[idx])]
    m = len(order)
    keep = np.zeros_like(p, dtype=bool)
    thresh = 0
    for rank, i in enumerate(order, start=1):
        if p[i] <= q * rank / m:
            thresh = rank
    for rank, i in enumerate(order, start=1):
        if rank <= thresh:
            keep[i] = True
    return keep


def main() -> None:
    close, opn, high, low, vol = load()
    print(f"loaded {close.shape[1]} tickers x {close.shape[0]} days (yfinance)")
    bench = close["SPY"] if "SPY" in close.columns else None

    sig_all = build(close, opn, high, low, vol, bench)
    fam = families(sig_all)
    print(f"hypothesis space: {len(sig_all)} signals x {len(HORIZONS)} horizons "
          f"= {len(sig_all) * len(HORIZONS)} tests per book")
    print("families: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(fam.items())) + "\n")

    cols = list(close.columns)
    books = {"top-20": [t for t in UNIVERSE if t in cols],
             "S&P-PIT": [t for t in cols if t != "SPY"]}

    rng = np.random.default_rng(12345)
    rows = []
    for bname, bcols in books.items():
        c = close[bcols]
        rets_idx, _ = split(c.pct_change().dropna(how="all"))
        idx = rets_idx.index
        cc = c.reindex(idx)
        mask = pit_mask(idx, bcols) if bname == "S&P-PIT" else np.ones((len(idx), len(bcols)), bool)

        fwd_rank = {}
        for h in HORIZONS:
            f = (cc.shift(-h) / cc - 1.0).where(pd.DataFrame(mask, index=idx, columns=bcols))
            fwd_rank[h] = f.rank(axis=1).to_numpy()[::h]

        print(f"=== {bname}: {len(bcols)} names, {len(idx)} days ===")
        for sname, sfull in sig_all.items():
            s = sfull.reindex(index=idx, columns=bcols).where(
                pd.DataFrame(mask, index=idx, columns=bcols))
            sr_full = s.rank(axis=1).to_numpy()
            for h in HORIZONS:
                sr = sr_full[::h]
                fr = fwd_rank[h]
                n = min(len(sr), len(fr))
                t_obs = tstat(row_ic(sr[:n], fr[:n]))
                nulls = []
                for _ in range(N_SHIFT):
                    off = int(rng.integers(252 // h, max(n - 252 // h, 252 // h + 1)))
                    nulls.append(tstat(row_ic(np.roll(sr[:n], off, axis=0), fr[:n])))
                nulls = np.array([x for x in nulls if np.isfinite(x)])
                if not np.isfinite(t_obs) or len(nulls) < 5:
                    continue
                p = float((np.abs(nulls) >= abs(t_obs)).mean())
                rows.append({"book": bname, "signal": sname, "family": sname.split("_")[0],
                             "horizon": h, "t": t_obs, "null_sd": float(nulls.std()),
                             "p_perm": max(p, 1.0 / (len(nulls) + 1))})
        print(f"  done ({sum(1 for r in rows if r['book'] == bname)} tests)")

    r = pd.DataFrame(rows)
    r.to_csv("edge/mass_ic.csv", index=False)

    print("\n" + "=" * 78)
    for bname in books:
        sub = r[r.book == bname].copy()
        if sub.empty:
            continue
        sub["keep"] = bh_fdr(sub["p_perm"].to_numpy(), q=0.10)
        obs_tail = float((sub["t"].abs() > 2).mean())
        exp_tail = 0.0455
        print(f"\n### {bname}: {len(sub)} tests")
        print(f"  |t|>2: observed {obs_tail:.1%} of tests vs {exp_tail:.1%} expected by chance "
              f"({obs_tail / exp_tail:.2f}x)")
        print(f"  max |t| observed {sub['t'].abs().max():.2f}; "
              f"mean null sd {sub['null_sd'].mean():.2f} (=1.0 if IC were iid)")
        print(f"  survive BH-FDR at q=0.10: {int(sub['keep'].sum())}")
        top = sub.reindex(sub["t"].abs().sort_values(ascending=False).index).head(10)
        print(top[["signal", "horizon", "t", "null_sd", "p_perm", "keep"]].to_string(
            index=False, float_format=lambda v: f"{v:.3f}"))

    log_trial("MASS-IC", f"{len(sig_all)} signals x {len(HORIZONS)} horizons x {len(books)} books",
              "TRAIN", f"{len(r)} tests",
              {"n_tests": len(r), "n_signals": len(sig_all),
               "max_abs_t": float(r["t"].abs().max())}, "see report")


if __name__ == "__main__":
    main()
