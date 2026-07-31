"""Is the trend hedge a real diversifier, or just 2020?

The hedge leg earns 4.10% CAGR standalone and still adds ~0.17 to the blend's
Sharpe, which is either textbook diversification or a story told about one
crash. The year table already showed +10.7pp in 2020 and losses in 2019 and
2022, so this pins down which.

Controls, because "it helped" is not a mechanism:
  const_long / const_short  Replace the timed hedge with a permanently long or
      permanently short leg at the same weight. If a constant short does just as
      well, the trend timing is irrelevant and the benefit was only beta
      reduction - which risk-matching is supposed to have already neutralised.
  long_leg / short_leg      Split the hedge into its two halves (+1 above the
      average only, -1 below the average only). Isolates which side pays.
  jackknife                 Drop each calendar year in turn and re-measure. If
      the improvement only exists with 2020 in the sample, it is one event.
  conditional correlation   Correlation with the combo in falling vs rising
      markets. A hedge that is uncorrelated on average but strongly negative in
      selloffs is worth far more than the -0.06 headline suggests.

    python hedge_attribution.py
"""

import numpy as np
import pandas as pd

from beat_buyhold_search import Evaluator, TRADING_DAYS
from beat_buyhold_search2 import Space2
from sharpe_search import SharpeEvaluator, memmel_z, sharpe_of
from signal_tensor import CACHE_PATH, load

COST_BPS = 5.0
TARGET_VOL = 0.12
COMBO = "~ADOSC + ~CDLCLOSINGMARUBOZU + ~VAR"
LOOKBACK, WEIGHT = 100, 0.30


def stats(r):
    eq = np.cumprod(1 + np.clip(r, -0.999, None))
    yrs = len(r) / TRADING_DAYS
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    cagr = float(eq[-1] ** (1 / yrs) - 1) if eq[-1] > 0 else np.nan
    return {"cagr": cagr, "sharpe": sharpe_of(r), "maxdd": dd,
            "calmar": cagr / abs(dd) if dd < 0 else np.nan}


def main():
    z = load(CACHE_PATH)
    ev = Evaluator(z)
    se = SharpeEvaluator(z, ev)
    sp = Space2(z)
    names = list(z["names"])
    T, N = z["returns"].shape
    d = pd.DatetimeIndex(z["dates"])
    valid = se.valid
    s_tr, e_tr = se.slices["train"]

    def book(w, s=1, e=T, costs=True):
        held = se._held(w, s, e)
        m, R = valid[s:e], se.R[s:e]
        n = np.maximum(m.sum(axis=1), 1)
        out = ((held * R) * m).sum(axis=1) / n
        if costs:
            first = w[s - 2:s - 1] if s >= 2 else np.zeros((1, N), w.dtype)
            out = out - (np.abs(np.diff(np.vstack([first, held]), axis=0)) * m).sum(axis=1) / n \
                * COST_BPS / 1e4
        return out

    atoms = tuple(sorted((names.index(t.lstrip("~")), -1 if t.startswith("~") else 1)
                         for t in COMBO.split(" + ")))
    base = sp.build2(atoms, None, 1, "defensive", "none").astype(np.float32)

    spy = z["spy"]
    sma = pd.Series(spy).rolling(LOOKBACK, min_periods=LOOKBACK).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        above = np.where(np.isnan(sma), 0.0, (spy > sma).astype(float))
        below = np.where(np.isnan(sma), 0.0, (spy <= sma).astype(float))
    def bc(v):
        return np.broadcast_to(v[:, None], (T, N)).astype(np.float32)

    legs = {
        "trend (as used)": bc(above - below),
        "long_leg only": bc(above),
        "short_leg only": bc(-below),
        "const_long": np.ones((T, N), np.float32),
        "const_short": -np.ones((T, N), np.float32),
    }

    uv = lambda w: w / max(float(book(w, s_tr, e_tr).std(ddof=1) * np.sqrt(TRADING_DAYS)), 1e-9)
    mt = lambda w: (w * min(TARGET_VOL / max(float(book(w, s_tr, e_tr).std(ddof=1)
                                                    * np.sqrt(TRADING_DAYS)), 1e-9), 3.0)).astype(np.float32)
    base_u = uv(base)
    combo = mt(base_u)
    r_combo = book(combo)
    mkt = book(np.ones((T, N), np.float32), costs=False)

    print("=== 1. Each leg STANDALONE (risk-matched, full period) ===")
    print(f"{'leg':<18}{'CAGR':>9}{'Sharpe':>9}{'maxDD':>9}{'corr w/combo':>14}")
    for nm, leg in legs.items():
        r = book(mt(uv(leg)))
        st = stats(r)
        print(f"{nm:<18}{st['cagr']:>9.2%}{st['sharpe']:>9.3f}{st['maxdd']:>9.2%}"
              f"{float(np.corrcoef(r, r_combo)[0, 1]):>14.3f}")

    print("\n=== 2. Blended 70/30 with each leg (risk-matched) ===")
    print(f"{'blend'   :<18}{'CAGR':>9}{'Sharpe':>9}{'maxDD':>9}{'Calmar':>9}{'z vs combo':>12}")
    st = stats(r_combo)
    print(f"{'combo alone':<18}{st['cagr']:>9.2%}{st['sharpe']:>9.3f}{st['maxdd']:>9.2%}"
          f"{st['calmar']:>9.3f}{'-':>12}")
    blends = {}
    for nm, leg in legs.items():
        w = mt(((1 - WEIGHT) * base_u + WEIGHT * uv(leg)).astype(np.float32))
        r = book(w)
        blends[nm] = r
        st = stats(r)
        print(f"{nm:<18}{st['cagr']:>9.2%}{st['sharpe']:>9.3f}{st['maxdd']:>9.2%}"
              f"{st['calmar']:>9.3f}{memmel_z(r, r_combo)[0]:>12.2f}")

    print("\n=== 3. Conditional correlation with the combo ===")
    trend_r = book(mt(uv(legs["trend (as used)"])))
    for label, mask in (("all days", np.ones(len(mkt), bool)),
                        ("market up", mkt > 0), ("market down", mkt < 0),
                        ("worst 10% days", mkt < np.quantile(mkt, 0.10)),
                        ("best 10% days", mkt > np.quantile(mkt, 0.90))):
        print(f"  {label:<16} n={int(mask.sum()):>5}  corr {float(np.corrcoef(trend_r[mask], r_combo[mask])[0,1]):>+7.3f}"
              f"   hedge mean {trend_r[mask].mean()*1e4:>8.2f}bp   combo mean {r_combo[mask].mean()*1e4:>8.2f}bp")

    print("\n=== 4. Jackknife: drop one calendar year, re-measure the hedge's gain ===")
    hedged = blends["trend (as used)"]
    yrs = d[1:1 + len(r_combo)].year if len(d) > len(r_combo) else d[:len(r_combo)].year
    yrs = np.asarray(yrs)
    print(f"{'dropped':<10}{'combo Sharpe':>14}{'hedged Sharpe':>15}{'delta':>9}"
          f"{'combo maxDD':>13}{'hedged maxDD':>14}")
    rows = []
    for y in ["none"] + sorted(set(yrs.tolist())):
        keep = np.ones(len(r_combo), bool) if y == "none" else (yrs != y)
        sc, sh = stats(r_combo[keep]), stats(hedged[keep])
        rows.append((y, sh["sharpe"] - sc["sharpe"]))
        print(f"{str(y):<10}{sc['sharpe']:>14.3f}{sh['sharpe']:>15.3f}"
              f"{sh['sharpe']-sc['sharpe']:>+9.3f}{sc['maxdd']:>13.2%}{sh['maxdd']:>14.2%}")
    deltas = [v for y, v in rows if y != "none"]
    print(f"\n  hedge improves Sharpe in {sum(1 for v in deltas if v > 0)} of {len(deltas)} "
          f"leave-one-year-out samples (min {min(deltas):+.3f}, max {max(deltas):+.3f})")


if __name__ == "__main__":
    main()
