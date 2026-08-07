"""Ad-hoc: does the leaderboard actually move once ranking is walk-forward?

Compares `summary_<tag>.csv` (single split, IR on the last 40% of the series) against the
`fixed` rows of `wf_summary_<tag>.csv` (same rules, IR on the stitched 24-fold OOS path).
Both are out-of-sample; they differ only in *which* out-of-sample and in whether the
choice of rule was priced.
"""

from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

from config import HEADLINE_SCENARIO


def spearman(a: pd.Series, b: pd.Series) -> float:
    return float(a.rank().corr(b.rank()))


rows = []
detail = {}
for wf_path in sorted(glob.glob("results/wf_summary_*.csv")):
    tag = os.path.basename(wf_path)[len("wf_summary_"):-4]
    old_path = f"results/summary_{tag}.csv"
    if not os.path.exists(old_path):
        continue
    wf = pd.read_csv(wf_path)
    old = pd.read_csv(old_path)
    scen = HEADLINE_SCENARIO[wf["class"].iloc[0]]

    w = wf[(wf.scenario == scen) & wf.rankable & ~wf.is_baseline]
    o = old[(old.scenario == scen) & old.rankable & ~old.is_baseline]
    wfix = w[w.wf_mode == "fixed"][["rule", "ir_net"]].rename(columns={"ir_net": "ir_wf"})
    osub = o[["rule", "ir_net"]].rename(columns={"ir_net": "ir_split"})
    m = wfix.merge(osub, on="rule").dropna()
    if len(m) < 10:
        continue

    top_old = osub.nlargest(10, "ir_split")["rule"].tolist()
    top_wf = wfix.nlargest(10, "ir_wf")["rule"].tolist()
    is1 = w[w.wf_mode == "is1_selection"]["ir_net"]

    # Where the honest selection rule would sit if dropped into the old leaderboard.
    is1_rank = (int((osub["ir_split"] > is1.iloc[0]).sum()) + 1) if len(is1) else None

    rows.append({
        "sheet": tag,
        "n_rules": len(m),
        "spearman_split_vs_wf": spearman(m["ir_split"], m["ir_wf"]),
        "top1_split": top_old[0],
        "top1_wf": top_wf[0],
        "top1_same": top_old[0] == top_wf[0],
        "top10_overlap": len(set(top_old) & set(top_wf)),
        "is1_ir": float(is1.iloc[0]) if len(is1) else np.nan,
        "is1_rank_of": f"{is1_rank}/{len(osub)}" if is1_rank else "",
    })
    detail[tag] = (osub.nlargest(8, "ir_split").reset_index(drop=True),
                   wfix.nlargest(8, "ir_wf").reset_index(drop=True))

df = pd.DataFrame(rows).sort_values("sheet")
print("=" * 104)
print("SINGLE SPLIT vs WALK-FORWARD — does the ranking move?")
print("=" * 104)
print(df.round(3).to_string(index=False))

for tag in ("us_stocks_1d", "crypto_1d"):
    if tag not in detail:
        continue
    o, w = detail[tag]
    print(f"\n--- {tag}: top 8 side by side ---")
    side = pd.concat([o.add_prefix("split_"), w.add_prefix("wf_")], axis=1)
    print(side.round(3).to_string(index=False))
