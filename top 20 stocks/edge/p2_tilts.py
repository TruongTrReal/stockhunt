"""Plan 2 - low-turnover weight tilts on a book that is always fully invested.

P0 settled the shape of anything that can work here. With SE(Sharpe)=0.366 on this
sample, a standalone strategy uncorrelated with buy-and-hold needs an excess Sharpe
above 2.1 to clear 100 trials - unreachable. An overlay correlated 0.95+ with the
benchmark needs only ~0.48, because the shared market variance cancels out of the
difference. So the search space is not "when to be in the market" (P0 says that is
unprovable, and buyhold-is-cagr-optimum says it is also wrong) - it is "which of
the 20 to overweight, while always holding all of them".

P1 supplies one of the candidate tilts. It found the overnight leg earns 12.78% CAGR
at 18.2% vol versus the intraday leg's 4.52% at 22.7%, and that the split varies
enormously by name (AMZN +35.9% overnight, META -2.1%). Trading that daily is
impossible - 504 turnover units/yr - but if overnight-share is *persistent per name*
it becomes a monthly stock-selection signal, which costs almost nothing to run.

Every tilt is long-only, fully invested, and rebalanced on a fixed calendar. Turnover
and net excess Sharpe across the cost grid are reported for all of them, because a
tilt that needs weekly rebalancing to work has to pay for it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import (COST_BPS_GRID, HEADLINE_COST_BPS, TRADING_DAYS, block_bootstrap_se,
                    deflated_threshold, load_daily, log_trial, sharpe, split, summarise)

LOOKBACK_VOL = 60
LOOKBACK_MOM = 252
REVERSAL_DAYS = 5


def rebalance_dates(idx: pd.DatetimeIndex, freq: str) -> pd.DatetimeIndex:
    """Last trading day of each period — weights computed there, traded next day."""
    return pd.DatetimeIndex(pd.Series(idx, index=idx).resample(freq).last().dropna().values)


def build_weights(signal: pd.DataFrame, rebal: pd.DatetimeIndex,
                  idx: pd.DatetimeIndex) -> pd.DataFrame:
    """Normalise a per-name score into long-only weights, held between rebalances."""
    w = {}
    for d in rebal:
        s = signal.loc[:d].iloc[-1] if len(signal.loc[:d]) else None
        if s is None or s.isna().all():
            continue
        s = s.fillna(s.median())
        s = s - s.min() + 1e-9          # long-only: shift to non-negative
        w[d] = s / s.sum()
    if not w:
        return pd.DataFrame(index=idx)
    return pd.DataFrame(w).T.reindex(idx).ffill()


def drifted_turnover(target: pd.DataFrame, rets: pd.DataFrame,
                     rebal: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.Series]:
    """Apply weights with drift: between rebalances weights move with returns, so the
    trade at each rebalance is |target - drifted|, not |target - previous target|.
    Ignoring drift would overstate turnover and unfairly penalise slow tilts.

    Rebalances fire on `rebal` dates, NOT on "the target changed". A constant-signal
    tilt still has to trade back to its target, because drift has moved it away — the
    first version of this inferred rebalances from target changes, so the equal-weight
    control silently never traded and degenerated into a drifting buy-and-hold.
    """
    cols = target.columns
    held = pd.DataFrame(0.0, index=target.index, columns=cols)
    turn = pd.Series(0.0, index=target.index)
    rebal_set = set(pd.DatetimeIndex(rebal))
    cur = None
    for i, d in enumerate(target.index):
        tgt = target.loc[d]
        if cur is None:
            cur = tgt.copy()
            turn.iloc[i] = float(tgt.abs().sum())
        elif d in rebal_set:
            turn.iloc[i] = float((tgt - cur).abs().sum())
            cur = tgt.copy()
        held.loc[d] = cur
        r = rets.loc[d].fillna(0.0)
        grown = cur * (1 + r)
        tot = grown.sum()
        cur = grown / tot if tot > 0 else cur
    return held, turn


def portfolio(signal: pd.DataFrame, rets: pd.DataFrame, freq: str):
    """(gross daily return, turnover series) for a tilt — the one path all books use,
    benchmark included, so nothing wins on a construction difference."""
    rebal = rebalance_dates(rets.index, freq)
    target = build_weights(signal, rebal, rets.index).fillna(1.0 / rets.shape[1])
    held, turn = drifted_turnover(target, rets, rebal)
    return (held.shift(1) * rets).sum(axis=1), turn


def evaluate(name: str, signal: pd.DataFrame, rets: pd.DataFrame, freq: str,
             ew_ret: pd.Series, bh_sr: float, se_sr: float, n_trials: int = 100) -> dict:
    gross, turn = portfolio(signal, rets, freq)
    yrs = len(rets) / TRADING_DAYS
    tpy = float(turn.sum() / yrs)

    rho = float(pd.concat([gross, ew_ret], axis=1).dropna().corr().iloc[0, 1])
    se_d = se_sr * np.sqrt(max(2 * (1 - rho), 1e-9))
    out = {"tilt": name, "freq": freq, "turnover_per_year": tpy, "rho": rho,
           "gross_sharpe": sharpe(gross), "gross_excess": sharpe(gross) - bh_sr,
           "threshold": deflated_threshold(n_trials, se_d),
           "threshold_1trial": deflated_threshold(1, se_d)}
    for bps in COST_BPS_GRID:
        net = gross - turn * bps / 1e4
        out[f"sr_{bps:g}"] = sharpe(net)
        out[f"ex_{bps:g}"] = sharpe(net) - bh_sr
        out[f"cagr_{bps:g}"] = summarise(net)["cagr"]
    return out


def main() -> None:
    data = load_daily()
    close = pd.DataFrame({t: df["Close"] for t, df in data.items()})
    opn = pd.DataFrame({t: df["Open"] for t, df in data.items()})
    rets_all = close.pct_change()
    overnight_all = (opn / close.shift(1) - 1.0)

    rets, _ = split(rets_all)
    overnight, _ = split(overnight_all)
    rets = rets.dropna(how="all").fillna(0.0)
    overnight = overnight.reindex(rets.index).fillna(0.0)

    # Benchmark = equal weight rebalanced on the SAME calendar as the tilt, built by
    # the same portfolio() path. Comparing a tilt against a daily-rebalanced EW would
    # hand it (or cost it) the rebalancing premium for free.
    flat = pd.DataFrame(1.0, index=rets.index, columns=rets.columns)
    bench = {f: portfolio(flat, rets, f)[0] for f in ("ME", "W-FRI")}
    ew = bench["ME"]
    se_sr = block_bootstrap_se(ew, sharpe, block=21, n_boot=3000)
    print(f"TRAIN {rets.index[0].date()} -> {rets.index[-1].date()}  SE(SR) {se_sr:.3f}")
    for f, b in bench.items():
        print(f"  EW benchmark rebalanced {f}: Sharpe {sharpe(b):.3f}  CAGR {summarise(b)['cagr']:.2%}")
    print()

    # Candidate tilts. Each is one trial and is logged as such.
    signals = {
        "inverse_vol_60d":    1.0 / rets.rolling(LOOKBACK_VOL).std(),
        "inverse_vol_252d":   1.0 / rets.rolling(LOOKBACK_MOM).std(),
        "reversal_5d":       -rets.rolling(REVERSAL_DAYS).sum(),
        "momentum_12_1":      close.shift(21) / close.shift(LOOKBACK_MOM) - 1.0,
        "overnight_share":    overnight.rolling(LOOKBACK_MOM).sum(),
        "overnight_sharpe":  (overnight.rolling(LOOKBACK_MOM).mean()
                              / overnight.rolling(LOOKBACK_MOM).std()),
        "low_beta_proxy":    -rets.rolling(LOOKBACK_MOM).corr(ew).mul(
                                 rets.rolling(LOOKBACK_MOM).std()),
        "equal_weight_ctrl":  pd.DataFrame(1.0, index=rets.index, columns=rets.columns),
    }

    n_tr = 2 * len(signals)
    rows = []
    for freq in ("ME", "W-FRI"):
        b = bench[freq]
        for name, sig in signals.items():
            rows.append(evaluate(name, sig, rets, freq, b, sharpe(b), se_sr, n_trials=n_tr))

    r = pd.DataFrame(rows).sort_values("ex_5", ascending=False)
    pd.set_option("display.width", 200)
    print(f"{'tilt':>19} {'freq':>6} {'turn/yr':>8} {'rho':>6} {'grossEx':>8} "
          f"{'ex@1':>7} {'ex@5':>7} {'ex@10':>7} {'1trial':>7} {f'x{n_tr}':>7} {'verdict':>8}")
    for _, x in r.iterrows():
        if x["ex_5"] > x["threshold"]:
            verdict = "PASS"
        elif x["ex_5"] > x["threshold_1trial"]:
            verdict = "marginal"
        else:
            verdict = "fail"
        print(f"{x['tilt']:>19} {x['freq']:>6} {x['turnover_per_year']:>8.2f} {x['rho']:>6.3f} "
              f"{x['gross_excess']:>+8.3f} {x['ex_1']:>+7.3f} {x['ex_5']:>+7.3f} "
              f"{x['ex_10']:>+7.3f} {x['threshold_1trial']:>7.3f} {x['threshold']:>7.3f} {verdict:>8}")

    r.to_csv("edge/p2_tilts.csv", index=False)
    for _, x in r.iterrows():
        log_trial("P2", f"tilt={x['tilt']} rebal={x['freq']}", "TRAIN",
                  f"excess Sharpe @5bps = {x['ex_5']:+.3f}",
                  {"gross_excess": x["gross_excess"], "ex_5": x["ex_5"],
                   "rho": x["rho"], "turnover": x["turnover_per_year"],
                   "threshold": x["threshold"]},
                  "PASS" if x["ex_5"] > x["threshold"] else "fail")


if __name__ == "__main__":
    main()
