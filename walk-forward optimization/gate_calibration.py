"""Are the four gates internally coherent on the data we actually have?

The gates in `config.GATES` were written as a statement of what a tradeable edge looks
like. They were never checked against the *power* of the sample they are applied to, and
two of them turn out to be statements about sample length rather than about strategies:

* **t = IR x sqrt(years) >= 2** implies `IR >= 2/sqrt(years)`. On 41 out-of-sample years
  that is IR 0.31 and the IR gate binds; on 3.6 crypto-4h years it is **1.05**, so the
  t gate silently demands twice what the stated IR gate does.
* **The noise ceiling** (`metrics.noise_ceiling`) is the IR the best of N worthless
  configurations reaches by luck. A gate set below its own ceiling cannot be passed
  *meaningfully* — anything clearing it is indistinguishable from the best of N coin
  flips. Relaxing such a gate makes it easier and more worthless at the same time; the
  coherent move is the opposite, raise it to the ceiling or shrink N.

So the effective bar is `max(ir_gate, 2/sqrt(years), noise_ceiling(N, years))`, and this
module prints it per sheet for the three search sizes this project has actually run: 5
pre-registered hypotheses, 96 variants, and the full 327-configuration sweep.

Run::

    python gate_calibration.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from wfo_paths import RESULTS_DIR          # noqa: F401  (wires sys.path first)
from config import GATES, HEADLINE_SCENARIO
import metrics

TRIAL_SIZES = {"prereg (5)": 5, "variants (96)": 96, "full sweep (327)": 327}


def gate_min(key: str) -> float:
    """Minimum for a criterion, tolerant of the 2026-08-08 rename.

    This module was written against the legacy four (`ir`, `breadth`, `headroom`, `t`)
    and reads `config.GATES`, which is now EDGE_STANDARD (`dsharpe`, `t`, `vs_random`,
    `vs_constant`, `wealth`, `headroom`). `next()` with no default then raised
    StopIteration and the whole stage died — the effect-size gate is simply called
    `dsharpe` now, and it is the same quantity: an edge over buy-and-hold that the sample
    has to be able to resolve.
    """
    alias = {"ir": ("dsharpe", "ir")}.get(key, (key,))
    for want in alias:
        for g in GATES:
            if g["key"] == want:
                return float(g["min"])
    raise KeyError(f"no criterion {key!r} in GATES "
                   f"({[g['key'] for g in GATES]}) — has the standard been renamed?")


def sheet_years() -> dict[str, float]:
    """Median out-of-sample years per sheet, taken from whatever stage last ran."""
    years: dict[str, float] = {}
    for path in sorted(RESULTS_DIR.glob("wf_summary_*.csv")):
        tag = path.stem[len("wf_summary_"):]
        df = pd.read_csv(path)
        scen = HEADLINE_SCENARIO[df["class"].iloc[0]]
        sub = df[(df.scenario == scen) & df.rankable]
        if not sub.empty and "years" in sub:
            years[tag] = float(sub["years"].median())
    return years


def best_observed() -> dict[str, float]:
    """Best out-of-sample IR actually achieved per sheet, across every stage."""
    best: dict[str, float] = {}
    for pattern, _ in (("wf_summary_*.csv", "wf"),
                       ("var_summary_*.csv", "var"),
                       ("prereg_*.csv", "prereg")):
        for path in sorted(RESULTS_DIR.glob(pattern)):
            base = path.name
            if base.startswith("prereg_meta"):
                continue
            tag = base.split("_", 1)[1][:-4] if base.startswith("prereg_") else \
                base[base.index("_", base.index("_") + 1) + 1:-4]
            try:
                df = pd.read_csv(path)
                scen = HEADLINE_SCENARIO[df["class"].iloc[0]]
            except Exception:
                continue
            sub = df[(df.scenario == scen) & df.rankable]
            if sub.empty:
                continue
            v = float(sub["ir_net"].max())
            best[tag] = max(best.get(tag, -np.inf), v)
    return best


def main() -> None:
    years = sheet_years()
    best = best_observed()
    ir_gate, t_gate = gate_min("ir"), gate_min("t")

    rows = []
    for tag, y in sorted(years.items()):
        t_implied = t_gate / np.sqrt(y) if y > 0 else np.nan
        row = {"sheet": tag, "oos_years": y,
               "ir_gate": ir_gate, "t_gate_implies_IR": t_implied}
        for label, n in TRIAL_SIZES.items():
            row[f"ceiling@{n}"] = metrics.noise_ceiling(n, y)
        row["best_observed"] = best.get(tag, np.nan)
        rows.append(row)
    df = pd.DataFrame(rows)

    print("=" * 108)
    print("GATE CALIBRATION — what each gate really demands, per sheet")
    print("=" * 108)
    print(df.round(3).to_string(index=False))

    print("\nEffective bar = max(IR gate, t gate, noise ceiling). "
          "A gate below its own ceiling cannot be passed meaningfully.")
    out = []
    for row in df.to_dict("records"):
        for label, n in TRIAL_SIZES.items():
            ceiling = row[f"ceiling@{n}"]
            t_imp = row["t_gate_implies_IR"]
            bar = max(row["ir_gate"], t_imp, ceiling)
            binds = ("noise ceiling" if bar == ceiling
                     else "t gate" if bar == t_imp else "IR gate")
            out.append({"sheet": row["sheet"], "search": label,
                        "effective_IR_bar": bar, "binding_gate": binds,
                        "gate_coherent": bool(row["ir_gate"] >= ceiling),
                        "gap_to_best": row["best_observed"] - bar})
    o = pd.DataFrame(out)
    print()
    print(o.round(3).to_string(index=False))

    print("\nReading:")
    print("  gate_coherent=False means the stated IR gate of "
          f"{ir_gate:.2f} sits BELOW what luck alone produces at that search size.")
    print("  gap_to_best is how far the best result actually achieved is from the bar.")
    coherent = o[o.gate_coherent]
    print(f"\n  sheet/search combinations where the gates are coherent: "
          f"{len(coherent)} of {len(o)}")
    if len(coherent):
        print("  " + ", ".join(f"{r.sheet}@{r.search}" for r in coherent.itertuples()))


if __name__ == "__main__":
    main()
