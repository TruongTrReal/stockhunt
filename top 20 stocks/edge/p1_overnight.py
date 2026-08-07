"""Plan 1 - decompose the daily return into its overnight and intraday legs.

Hypothesis: essentially all of the equity return on these 20 names accrues between
the previous close and the next open, and the 09:30-16:00 session contributes little
return but most of the variance. If so, holding only the overnight leg earns close to
the full return on a fraction of the risk - a Sharpe gain with no directional forecast
anywhere in it, hence orthogonal to all 304 close-to-close signals already tested.

Provenance: this is not a fresh guess. Validating the old 5m sheet showed an
EOD-flattened book earned +0.23% over a window in which true buy-and-hold earned
+5.80% - i.e. the intraday session contributed ~nothing. This plan asks whether that
holds over 8 years rather than one quarter.

The catch, stated before running: overnight-only trades one round trip every day
(504 turnover units/yr). At 5bps that is a 2.52%/yr drag, so the leg has to win by a
wide margin gross to survive net. Costs decide this, as always.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import (COST_BPS_GRID, HEADLINE_COST_BPS, TRADING_DAYS, block_bootstrap_se,
                    deflated_threshold, load_daily, log_trial, sharpe, split, summarise)


def legs(df: pd.DataFrame) -> pd.DataFrame:
    """Split close-to-close into close->open (overnight) and open->close (intraday).

    Uses adjusted OHLC from one source: Open and Close carry the same adjustment
    factor, so their ratio is clean even though the levels are back-adjusted.
    """
    o, c = df["Open"], df["Close"]
    overnight = o / c.shift(1) - 1.0
    intraday = c / o - 1.0
    total = c.pct_change()
    return pd.DataFrame({"overnight": overnight, "intraday": intraday, "total": total}).dropna()


def main() -> None:
    data = load_daily()
    per_leg = {t: legs(df) for t, df in data.items()}

    on = pd.DataFrame({t: v["overnight"] for t, v in per_leg.items()})
    idd = pd.DataFrame({t: v["intraday"] for t, v in per_leg.items()})
    tot = pd.DataFrame({t: v["total"] for t, v in per_leg.items()})

    on_tr, on_ho = split(on)
    id_tr, id_ho = split(idd)
    tot_tr, tot_ho = split(tot)

    print(f"TRAIN {on_tr.index[0].date()} -> {on_tr.index[-1].date()} "
          f"({len(on_tr)} days x {on.shape[1]} tickers)\n")

    # ---- per-ticker decomposition, gross -------------------------------------
    print(f"{'ticker':>7} | {'overnight':>22} | {'intraday':>22} | {'buy&hold':>14}")
    print(f"{'':>7} | {'CAGR':>8}{'vol':>7}{'SR':>7} | {'CAGR':>8}{'vol':>7}{'SR':>7} | {'CAGR':>7}{'SR':>7}")
    rows = []
    for t in on.columns:
        a, b, c = summarise(on_tr[t]), summarise(id_tr[t]), summarise(tot_tr[t])
        rows.append({"ticker": t, "on_cagr": a["cagr"], "on_sr": a["sharpe"], "on_vol": a["vol"],
                     "id_cagr": b["cagr"], "id_sr": b["sharpe"], "id_vol": b["vol"],
                     "bh_cagr": c["cagr"], "bh_sr": c["sharpe"]})
        print(f"{t:>7} | {a['cagr']:>8.2%}{a['vol']:>7.1%}{a['sharpe']:>7.2f} | "
              f"{b['cagr']:>8.2%}{b['vol']:>7.1%}{b['sharpe']:>7.2f} | {c['cagr']:>7.2%}{c['sharpe']:>7.2f}")

    r = pd.DataFrame(rows)
    print(f"\n{'MEAN':>7} | {r.on_cagr.mean():>8.2%}{r.on_vol.mean():>7.1%}{r.on_sr.mean():>7.2f} | "
          f"{r.id_cagr.mean():>8.2%}{r.id_vol.mean():>7.1%}{r.id_sr.mean():>7.2f} | "
          f"{r.bh_cagr.mean():>7.2%}{r.bh_sr.mean():>7.2f}")
    print(f"overnight leg wins on Sharpe for {int((r.on_sr > r.bh_sr).sum())}/{len(r)} tickers; "
          f"on CAGR for {int((r.on_cagr > r.bh_cagr).sum())}/{len(r)}")

    # ---- equal-weight portfolios, net of cost --------------------------------
    # Overnight-only and intraday-only both trade one round trip per day = 2 units.
    # Buy-and-hold trades once, ever.
    ew_on, ew_id, ew_bh = on_tr.mean(axis=1), id_tr.mean(axis=1), tot_tr.mean(axis=1)
    print("\n=== equal-weight portfolio, TRAIN, net of cost ===")
    print(f"{'bps':>5} | {'overnight-only':>26} | {'intraday-only':>26} | {'buy&hold':>16}")
    print(f"{'':>5} | {'CAGR':>9}{'SR':>8}{'dSR':>9} | {'CAGR':>9}{'SR':>8}{'dSR':>9} | {'CAGR':>8}{'SR':>8}")
    net = {}
    bh_sr = sharpe(ew_bh)
    for bps in COST_BPS_GRID:
        drag = 2 * bps / 1e4                     # buy at close, sell at open -> 2 units/day
        n_on, n_id = ew_on - drag, ew_id - drag
        s_on, s_id = summarise(n_on), summarise(n_id)
        net[bps] = {"on_sharpe": s_on["sharpe"], "on_cagr": s_on["cagr"],
                    "on_excess_sharpe": s_on["sharpe"] - bh_sr}
        print(f"{bps:>5.0f} | {s_on['cagr']:>9.2%}{s_on['sharpe']:>8.2f}{s_on['sharpe'] - bh_sr:>+9.2f} | "
              f"{s_id['cagr']:>9.2%}{s_id['sharpe']:>8.2f}{s_id['sharpe'] - bh_sr:>+9.2f} | "
              f"{summarise(ew_bh)['cagr']:>8.2%}{bh_sr:>8.2f}")

    # ---- is the headline result bigger than the noise floor? -----------------
    diff = (ew_on - 2 * HEADLINE_COST_BPS / 1e4) - ew_bh
    rho = float(pd.concat([ew_on, ew_bh], axis=1).dropna().corr().iloc[0, 1])
    se_sr = block_bootstrap_se(ew_bh, sharpe, block=21, n_boot=3000)
    se_d = se_sr * np.sqrt(2 * (1 - rho))
    obs = net[HEADLINE_COST_BPS]["on_excess_sharpe"]
    thr = deflated_threshold(100, se_d)
    print(f"\ncorrelation(overnight, buy&hold) = {rho:.3f}")
    print(f"observed excess Sharpe @{HEADLINE_COST_BPS:g}bps = {obs:+.3f}")
    print(f"detection floor (100 trials, 5%)  = {thr:+.3f}   -> "
          f"{'CLEARS' if obs > thr else 'FAILS'}")

    r.to_csv("edge/p1_overnight_per_ticker.csv", index=False)
    log_trial("P1", "overnight leg carries the return at a fraction of the variance",
              "TRAIN", "see metrics",
              {"mean_on_sharpe": float(r.on_sr.mean()), "mean_id_sharpe": float(r.id_sr.mean()),
               "mean_on_cagr": float(r.on_cagr.mean()), "mean_id_cagr": float(r.id_cagr.mean()),
               "ew_excess_sharpe_5bps": obs, "threshold_100trials": thr, "rho": rho},
              "CLEARS floor" if obs > thr else "FAILS floor")


if __name__ == "__main__":
    main()
