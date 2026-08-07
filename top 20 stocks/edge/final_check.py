"""Is the top-20 momentum long-short book a real effect or two stocks?

It is the strongest portfolio result in the entire project: Sharpe 0.889 net of 5bps,
0.805 even at 20bps, on only 8.5 turnover units a year. That profile - high Sharpe,
cost-insensitive, market-neutral - is exactly what a tradeable edge is supposed to look
like, so it deserves a real attempt at destruction rather than a hand-wave.

Two pieces of evidence already point at an artefact. P4 found the same momentum signal
decaying from +0.249 to +0.082 excess Sharpe when the book widened from 20 names to
472; and the mass IC screen found 12-month momentum has NO significant cross-sectional
IC on the survivorship-controlled S&P book (p=0.31 against the permutation null). Both
say the effect lives specifically in these 20 names over this period.

The direct test is leave-one-out. If the book's Sharpe is a diversified harvest across
20 names, removing any single name should barely move it. If it is a bet on NVDA and
TSLA trending for eight years, dropping those two should gut it. A concentration this
extreme would also mean the strategy is untradeable in any case: nobody runs a
"market-neutral" book whose entire P&L is two positions.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import TRADING_DAYS, UNIVERSE, log_trial, sharpe, split, summarise
from mass_ic import load
from mass_port import run_book, zscore
from hypotheses import build

SIGNAL, STEP = "A_mom_252_skip21", 5
COST = 5.0


def book_sharpe(sig, rets, mask, mode, cost=COST):
    g, turn = run_book(zscore(sig), rets, mask, STEP, mode)
    net = (g - turn * cost / 1e4).clip(lower=-0.999)
    yrs = len(rets) / TRADING_DAYS
    return sharpe(net), float(turn.sum() / yrs), summarise(net)


def main() -> None:
    close, opn, high, low, vol = load()
    bench_px = close["SPY"] if "SPY" in close.columns else None
    sig_all = build(close, opn, high, low, vol, bench_px)
    cols = [t for t in UNIVERSE if t in close.columns]

    rets_all, _ = split(close[cols].pct_change().dropna(how="all"))
    rets = rets_all.fillna(0.0)
    idx = rets.index
    mask = pd.DataFrame(True, index=idx, columns=cols)
    sig = sig_all[SIGNAL].reindex(index=idx, columns=cols)

    base_ls, turn_ls, stats_ls = book_sharpe(sig, rets, mask, "ls")
    print(f"full 20-name momentum long-short: Sharpe {base_ls:.3f} @{COST:g}bps, "
          f"turnover {turn_ls:.1f}/yr, CAGR {stats_ls['cagr']:.2%}, maxDD {stats_ls['max_dd']:.1%}\n")

    print("leave-one-out (Sharpe of the book with that name removed):")
    rows = []
    for drop in cols:
        keep = [c for c in cols if c != drop]
        m = pd.DataFrame(True, index=idx, columns=keep)
        s, _, _ = book_sharpe(sig[keep], rets[keep], m, "ls")
        rows.append({"dropped": drop, "sharpe": s, "delta": s - base_ls})
    r = pd.DataFrame(rows).sort_values("sharpe")
    for _, x in r.iterrows():
        bar = "#" * max(int(abs(x["delta"]) * 40), 0)
        print(f"  -{x['dropped']:<6} {x['sharpe']:>6.3f}  ({x['delta']:+.3f}) {bar}")

    worst = r.head(2)["dropped"].tolist()
    keep = [c for c in cols if c not in worst]
    m = pd.DataFrame(True, index=idx, columns=keep)
    s2, _, st2 = book_sharpe(sig[keep], rets[keep], m, "ls")
    print(f"\ndropping the two most load-bearing names ({', '.join(worst)}): "
          f"Sharpe {base_ls:.3f} -> {s2:.3f}  ({s2 - base_ls:+.3f}), "
          f"CAGR {stats_ls['cagr']:.2%} -> {st2['cagr']:.2%}")

    # Sub-period stability: an effect present in only one regime is not an effect.
    print("\nsub-period Sharpe of the full book:")
    halves = {"2015-2018": slice("2015", "2018"), "2019-2022": slice("2019", "2022")}
    for label, sl in halves.items():
        sub = rets.loc[sl]
        m = pd.DataFrame(True, index=sub.index, columns=cols)
        s, _, st = book_sharpe(sig.reindex(sub.index), sub, m, "ls")
        print(f"  {label}: Sharpe {s:>6.3f}  CAGR {st['cagr']:>7.2%}")

    r.to_csv("edge/final_check.csv", index=False)
    concentrated = (base_ls - s2) > 0.4 * abs(base_ls)
    log_trial("FINAL", "leave-one-out on top-20 momentum long-short", "TRAIN",
              f"full {base_ls:.3f} -> drop2 {s2:.3f}",
              {"full_sharpe": base_ls, "drop_two_sharpe": s2,
               "most_load_bearing": ",".join(worst)},
              "ARTEFACT - concentrated in a few names" if concentrated
              else "robust to leave-one-out")


if __name__ == "__main__":
    main()
