"""Add risk layers on top of the validated combos to cut drawdown.

combo_diagnostics.py established the failure mode: the combos are dip buyers.
They raise exposure after down days (0.70 after a bottom-decile day vs 0.57
after a top-decile one) and were near fully invested at the 2018 and 2022
troughs. Their `~VAR` leg does NOT time volatility - exposure is flat at ~0.63
across every volatility quintile, because `~VAR` compares variance to its own
trailing average and so responds to vol acceleration rather than vol level.

That leaves the actual Moreira-Muir lever untouched, so three layers are tested
here, each attacking the diagnosed problem from a different side:

  volscale   Scale the whole book by target_vol / realised_vol, capped. This is
             real volatility targeting: it cuts exposure in high-vol regimes,
             which is precisely what the combo currently fails to do.
  regime     Cut exposure while the market is below its 200-day average. Trend
             gates destroyed CAGR in every earlier test, so the prior is that
             this hurts - but the combo is a dip buyer, and "don't buy dips in a
             confirmed downtrend" is a different claim than "don't hold stocks".
  ddcontrol  De-risk after the book's own drawdown passes a threshold, restoring
             exposure as it recovers. Attacks drawdown directly rather than
             through a market proxy.

All parameters are chosen on TRAIN (2015-2021) and reported on TEST (2022-2026).
Realised volatility and drawdown are computed from data through t-1 only and
applied to the position held into t, so no layer can see the bar it trades.

    python combo_layers.py
"""

import numpy as np
import pandas as pd

from beat_buyhold_search import Evaluator, TRADING_DAYS
from beat_buyhold_search2 import Space2
from sharpe_search import SharpeEvaluator, sharpe_of
from signal_tensor import CACHE_PATH, load

OUT = "../data/combo_layers_results.csv"
COST_BPS = 5.0
COMBOS = [("~ADOSC + ~CDLCLOSINGMARUBOZU + ~VAR", "defensive"),
          ("~AD + ~CDLCLOSINGMARUBOZU + ~STDDEV", "defensive")]

VOL_WINDOWS = (20, 40, 60)
VOL_TARGETS = (0.08, 0.10, 0.12, 0.14)
VOL_CAPS = (1.5, 2.0, 3.0)
REGIME_CUTS = (0.0, 0.25, 0.5)          # exposure multiplier while below SMA200
DD_TRIGGERS = (0.05, 0.08, 0.12)        # book drawdown at which de-risking starts
DD_FLOORS = (0.25, 0.5)                 # minimum multiplier at full de-risk

# Risk-matching target: every variant is levered to this annualised volatility
# using TRAIN data only, so configurations are compared at equal risk instead of
# being rewarded or punished for how much exposure they happen to carry.
TARGET_VOL = 0.12
MAX_LEVERAGE = 3.0


def curve_stats(r):
    eq = np.cumprod(1 + np.clip(r, -0.999, None))
    yrs = len(r) / TRADING_DAYS
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    cagr = float(eq[-1] ** (1 / yrs) - 1) if eq[-1] > 0 else np.nan
    return {"cagr": cagr, "vol": float(r.std(ddof=1) * np.sqrt(TRADING_DAYS)),
            "sharpe": sharpe_of(r), "maxdd": dd,
            "calmar": cagr / abs(dd) if dd < 0 else np.nan}


class Book:
    def __init__(self, z, se):
        self.se = se
        self.T, self.N = z["returns"].shape
        self.m_all = se.valid

    def returns(self, w, s, e, costs=True):
        """w is a (T,N) float weight matrix; returns the daily portfolio series."""
        held = w[s - 1:e - 1] if s > 0 else np.vstack([np.zeros((1, self.N), w.dtype), w[:e - 1]])
        m, R = self.m_all[s:e], self.se.R[s:e]
        n = np.maximum(m.sum(axis=1), 1)
        out = ((held * R) * m).sum(axis=1) / n
        if not costs:
            return out, held, m, n
        first = w[s - 2:s - 1] if s >= 2 else np.zeros((1, self.N), w.dtype)
        tr = (np.abs(np.diff(np.vstack([first, held]), axis=0)) * m).sum(axis=1) / n
        return out - tr * COST_BPS / 1e4, held, m, n


def dd_multiplier(daily, trigger, floor):
    """Sequential: multiplier for bar t depends only on the book's equity
    through t-1, so it cannot see the return it is scaling."""
    n = len(daily)
    mult = np.ones(n)
    eq, peak = 1.0, 1.0
    for t in range(n):
        dd = eq / peak - 1
        if dd < -trigger:
            frac = min((-dd - trigger) / trigger, 1.0)
            mult[t] = 1.0 - (1.0 - floor) * frac
        eq *= 1 + daily[t] * mult[t]
        peak = max(peak, eq)
    return mult


def main():
    z = load(CACHE_PATH)
    ev = Evaluator(z)
    se = SharpeEvaluator(z, ev)
    sp = Space2(z)
    bk = Book(z, se)
    names = list(z["names"])
    T, N = z["returns"].shape
    ones = np.ones((T, N), np.int8)
    closes, spy = z["closes"], z["spy"]

    s_tr, e_tr = se.slices["train"]
    s_te, e_te = se.slices["test"]

    # Market series and the two lagged risk signals, built once.
    mkt, _, _, _ = bk.returns(ones.astype(np.float32), 1, T, costs=False)
    mkt_full = np.r_[0.0, mkt]
    rv = {w: pd.Series(mkt_full).rolling(w).std().shift(1).to_numpy() * np.sqrt(TRADING_DAYS)
          for w in VOL_WINDOWS}
    spy_sma = pd.Series(spy).rolling(200, min_periods=200).mean().to_numpy()
    below = np.nan_to_num(spy < spy_sma, nan=0.0).astype(bool)

    rows = []
    for combo, transform in COMBOS:
        atoms = tuple(sorted((names.index(t.lstrip("~")), -1 if t.startswith("~") else 1)
                             for t in combo.split(" + ")))
        base = sp.build2(atoms, None, 1, transform, "none").astype(np.float32)

        def record(tag, w, params):
            """Every layer here REDUCES risk, so raw CAGR/Calmar penalises it for
            holding less - the same trap the CAGR search fell into. Each variant
            is therefore also levered to a common target volatility, solved on
            TRAIN, and it is those risk-matched columns that are comparable."""
            rec = {"combo": combo, "layer": tag, **params}
            r_tr = bk.returns(w, s_tr, e_tr)[0]
            v_tr = float(r_tr.std(ddof=1) * np.sqrt(TRADING_DAYS))
            L = min(TARGET_VOL / max(v_tr, 1e-9), MAX_LEVERAGE)
            rec["leverage"] = L
            wl = (w * L).astype(np.float32)
            for win, (s, e) in (("train", (s_tr, e_tr)), ("test", (s_te, e_te))):
                r, held, m, n = bk.returns(w, s, e)
                rec.update({f"{win}_{k}": v for k, v in curve_stats(r).items()})
                rec[f"{win}_expo"] = float((np.abs(held) * m).sum() / m.sum())
                rl, heldl, ml, _ = bk.returns(wl, s, e)
                rec.update({f"m_{win}_{k}": v for k, v in curve_stats(rl).items()})
                rec[f"m_{win}_expo"] = float((np.abs(heldl) * ml).sum() / ml.sum())
            rows.append(rec)

        record("none", base, {})

        # --- volatility targeting -----------------------------------------
        for win in VOL_WINDOWS:
            v = rv[win]
            for tgt in VOL_TARGETS:
                for cap in VOL_CAPS:
                    mult = np.clip(np.where(np.isnan(v), 1.0, tgt / np.maximum(v, 1e-6)), 0.0, cap)
                    record("volscale", base * mult[:, None].astype(np.float32),
                           {"vol_win": win, "vol_target": tgt, "vol_cap": cap})

        # --- regime gate ---------------------------------------------------
        for cut in REGIME_CUTS:
            mult = np.where(below, cut, 1.0).astype(np.float32)
            record("regime", base * mult[:, None], {"regime_cut": cut})

        # --- drawdown control ----------------------------------------------
        r_base, _, _, _ = bk.returns(base, 1, T)
        for trig in DD_TRIGGERS:
            for floor in DD_FLOORS:
                mult = np.r_[1.0, dd_multiplier(r_base, trig, floor)].astype(np.float32)
                record("ddcontrol", base * mult[:, None], {"dd_trigger": trig, "dd_floor": floor})

        # --- best pairing: vol targeting + drawdown control -----------------
        for win in VOL_WINDOWS:
            v = rv[win]
            for tgt in VOL_TARGETS:
                mult_v = np.clip(np.where(np.isnan(v), 1.0, tgt / np.maximum(v, 1e-6)), 0.0, 2.0)
                w_v = base * mult_v[:, None].astype(np.float32)
                r_v, _, _, _ = bk.returns(w_v, 1, T)
                for trig in DD_TRIGGERS:
                    mult_d = np.r_[1.0, dd_multiplier(r_v, trig, 0.25)].astype(np.float32)
                    record("volscale+dd", w_v * mult_d[:, None],
                           {"vol_win": win, "vol_target": tgt, "vol_cap": 2.0,
                            "dd_trigger": trig, "dd_floor": 0.25})

    df = pd.DataFrame(rows)
    df.to_csv(OUT, index=False)

    bh_tr = curve_stats(bk.returns(np.ones((T, N), np.float32), s_tr, e_tr, costs=False)[0])
    bh_te = curve_stats(bk.returns(np.ones((T, N), np.float32), s_te, e_te, costs=False)[0])
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)

    print(f"Buy & hold   train: CAGR {bh_tr['cagr']:.2%} maxDD {bh_tr['maxdd']:.2%} "
          f"Sharpe {bh_tr['sharpe']:.3f} | test: CAGR {bh_te['cagr']:.2%} "
          f"maxDD {bh_te['maxdd']:.2%} Sharpe {bh_te['sharpe']:.3f}\n")

    cols = ["layer", "leverage", "m_train_cagr", "m_train_maxdd", "m_train_sharpe",
            "m_train_calmar", "m_test_cagr", "m_test_maxdd", "m_test_sharpe",
            "m_test_calmar", "m_test_vol"]
    par = ["vol_win", "vol_target", "vol_cap", "regime_cut", "dd_trigger", "dd_floor"]
    print(f"All figures below are RISK-MATCHED: levered on train to {TARGET_VOL:.0%} "
          f"annual volatility, so lower drawdown is a real gain, not less exposure.\n")
    for combo, _ in COMBOS:
        sub = df[df.combo == combo]
        print(f"=== {combo} ===")
        print("unlayered baseline:")
        print(sub[sub.layer == "none"][cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
        # Rank on TRAIN Calmar of the risk-matched book - picking on test is how
        # the earlier tilt work fooled itself.
        best = sub[sub.layer != "none"].sort_values("m_train_calmar", ascending=False).head(8)
        print("top 8 layered configs by RISK-MATCHED TRAIN Calmar:")
        print(best[cols + par].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
        print()

    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
