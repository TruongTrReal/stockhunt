"""Correct the combo rows' explanation in report_data.json.

patch_combos_into_report.py wrote a tldr claiming the combos are volatility
timing rules that "hold when volatility is below its own average". combo_
diagnostics.py subsequently disproved that: exposure is flat at ~0.63 across
every volatility quintile, because the inverted volatility leg compares variance
to its own trailing average and therefore reacts to volatility acceleration
rather than volatility level. What the rules actually do is buy dips.

The report is a published artifact, so the wrong mechanism has to be corrected
in place rather than only in the analysis notes.

    python fix_combo_tldr.py
"""

import json

from build_report_data import _sanitize_nans

REPORT_PATH = "../data/report_data.json"

WRONG = (" Selected on 2015-2021 and validated on unseen 2022-2026; the inverted volatility "
         "leg makes this a volatility-timing rule - it holds when volatility is below its "
         "own average, which is what lifts risk-adjusted return above buy-and-hold.")

RIGHT = (" Selected on 2015-2021 and validated on unseen 2022-2026. Measured behaviour: this "
         "is a DIP BUYER, not a volatility timer. Exposure stays flat near 0.63 across every "
         "volatility quintile, because the inverted volatility leg compares variance to its "
         "own trailing average and so reacts to volatility acceleration rather than level. "
         "It raises exposure after down days (0.70 after a bottom-decile day vs 0.57 after a "
         "top-decile one) and was near fully invested at the 2018 and 2022 lows. That is why "
         "it beats buy-and-hold on risk-adjusted return while still falling in sustained "
         "selloffs - and why filters that merely cut exposure make its drawdown worse, since "
         "they sell exactly when the rebound it is positioned for arrives.")

HEDGE_EXTRA = (" Attribution: about 57% of the hedge's benefit is plain beta reduction (a "
               "constant short with no timing captures most of it) and 43% is trend timing. "
               "It improves Sharpe in 12 of 12 leave-one-year-out samples, but removing 2020 "
               "cuts the gain from +0.141 to +0.056, so the effect is consistently signed "
               "with crash-dependent magnitude. On the worst 10% of days the hedge earns "
               "+10bp while the combo loses 123bp; on the best 10% it costs 23bp.")


def main():
    report = json.load(open(REPORT_PATH))
    fixed = hedged = 0
    for row in report["leaderboard"]:
        if WRONG in row.get("tldr", ""):
            row["tldr"] = row["tldr"].replace(WRONG, RIGHT)
            fixed += 1
        if "hedge" in row["indicator"] and HEDGE_EXTRA not in row.get("tldr", ""):
            row["tldr"] += HEDGE_EXTRA
            hedged += 1

    with open(REPORT_PATH, "w") as f:
        json.dump(_sanitize_nans(report), f, separators=(",", ":"), allow_nan=False)
    print(f"corrected {fixed} combo tldrs; appended attribution to {hedged} hedged row(s)")
    remaining = sum(1 for r in report["leaderboard"] if "volatility-timing rule" in r.get("tldr", ""))
    print(f"rows still claiming 'volatility-timing rule': {remaining}")


if __name__ == "__main__":
    main()
