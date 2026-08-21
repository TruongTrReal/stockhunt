"""Read every `convert_ha_*` sheet and answer the four questions the study asked.

    python summarize_intraday_ha.py                 # everything written so far
    python summarize_intraday_ha.py --class crypto  # one class

It reads only files this folder wrote and computes nothing a sheet does not already
carry, so it can be run mid-study; sheets that do not exist yet are simply absent.

**The gross column is guarded, and that guard is the point.** "Excess before costs" is
reconstructed as `cashmatch_excess_cagr + cost_drag_ann`, which is exact arithmetic only
while the drag is small — every cost term is linear in its rate, so the book at zero cost
is `net + drag`. Once a book approaches total loss the identity stops meaning anything:
on crypto 5m a rule losing 100% of capital reports a drag of 3,782%/yr, and adding that
back yields a large positive number describing an account that no longer exists. The
no-signal control proves it rather than argument doing so — RANDOM_50 posts a +144%
"gross excess" on the same sheet. So gross is printed only where the drag is under
`MAX_HONEST_DRAG`, and suppressed with a reason everywhere else.
"""

from __future__ import annotations

import argparse
import glob
import os
import re

import numpy as np
import pandas as pd

from wfo_paths import RESULTS_DIR


CONTROLS = ("BUYHOLD", "RANDOM_50", "RANDOM_75", "ALWAYS_LONG", "ALWAYS_FLAT")

# Above this annual cost drag the gross reconstruction is not arithmetic any more.
# 0.50 is well inside the regime where it still holds and well below crypto's 1.0+.
MAX_HONEST_DRAG = 0.50

SHEET_RE = re.compile(r"convert_ha_(?P<tag>.*?)(?P<cls>us_stocks|crypto|us_etfs|commodities)"
                      r"_(?P<tf>\d+m)(?P<suffix>_flat|_knn)?\.csv$")


def load_sheets(only_class: str | None = None) -> dict:
    out = {}
    for path in sorted(glob.glob(str(RESULTS_DIR / "convert_ha_*.csv"))):
        m = SHEET_RE.search(os.path.basename(path))
        if not m or (only_class and m["cls"] != only_class):
            continue
        d = pd.read_csv(path).set_index("rule")
        # `gross` is NaN wherever the reconstruction is not arithmetic any more, so a
        # meaningless number cannot reach a mean, a t-statistic or a leaderboard by
        # being merely large. The guard is PER CELL: a sheet whose median drag is
        # modest can still hold one rule that flips position every bar and pays 250%,
        # and that rule's "gross" is exactly as fictional as crypto's.
        gross = d["cashmatch_excess_cagr"] + d["cost_drag_ann"]
        d = d.assign(gross=gross.where(d["cost_drag_ann"] < MAX_HONEST_DRAG))
        tag = (m["tag"] or "").rstrip("_") + (m["suffix"] or "")
        label = f'{m["cls"]}/{m["tf"]}' + (f" [{tag}]" if tag else "")
        out[(m["cls"], m["tf"], label)] = d
    return out


def rules_only(d: pd.DataFrame) -> pd.DataFrame:
    return d.drop(index=[c for c in CONTROLS if c in d.index])


def ha_pairs(d: pd.DataFrame) -> pd.DataFrame:
    """Every `ha:chart:X` beside its `chart:X` twin. The study's actual experiment."""
    rows = []
    for r in d.index:
        if not r.startswith("ha:chart:"):
            continue
        plain = r.replace("ha:chart:", "chart:", 1)
        if plain in d.index:
            rows.append({"cell": r[len("ha:chart:"):],
                         "d_net": d.loc[r, "cashmatch_excess_cagr"]
                                  - d.loc[plain, "cashmatch_excess_cagr"],
                         "d_gross": d.loc[r, "gross"] - d.loc[plain, "gross"]})
    return pd.DataFrame(rows)


def _t(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    return np.nan if len(x) < 2 or x.std(ddof=1) == 0 else \
        float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="cls", default=None)
    args = ap.parse_args()
    sheets = load_sheets(args.cls)
    if not sheets:
        print("no convert_ha_* sheets yet")
        return 0

    print("=" * 108)
    print("1. DID ANYTHING BEAT BUY-AND-HOLD?   (cash-matched, so risk is already equalised)")
    print("=" * 108)
    print(f"{'sheet':26s} {'yrs':>5s} {'B&H':>7s} {'beat':>8s} {'tie':>5s} "
          f"{'best net':>9s} {'best cell':34s} {'med cost':>9s}")
    for (c, tf, label), d in sorted(sheets.items()):
        r = rules_only(d)
        if r.empty:
            continue
        tie = int((r["boot_t"].abs() < 2).sum()) if "boot_t" in r else -1
        best = r["cashmatch_excess_cagr"].idxmax()
        bh = d.loc["BUYHOLD", "cagr"] if "BUYHOLD" in d.index else np.nan
        print(f"{label:26s} {d['years'].iloc[0]:5.1f} {bh:6.1%} "
              f"{int((r['cashmatch_excess_cagr'] > 0).sum()):3d}/{len(r):<4d} {tie:5d} "
              f"{r['cashmatch_excess_cagr'].max():+9.4f} {best[:34]:34s} "
              f"{r['cost_drag_ann'].median():8.1%}")
    print("  beat = cells with positive excess.  tie = cells a bootstrap cannot separate "
          "from B&H (|t|<2).")

    print()
    print("=" * 108)
    print("2. HEIKIN-ASHI vs PLAIN CANDLES   (paired on the same rule, same fills)")
    print("=" * 108)
    print(f"{'sheet':26s} {'n':>3s} {'net diff':>10s} {'t':>7s} {'wins':>8s} "
          f"{'gross diff':>11s} {'t':>7s} {'wins':>8s}")
    for (c, tf, label), d in sorted(sheets.items()):
        p = ha_pairs(d)
        if p.empty:
            continue
        # A difference is only computable where BOTH sides survived the per-cell guard.
        # Dropping them rather than zero-filling matters: a suppressed cell is one whose
        # gross is unknown, not one whose gross is zero, and averaging in a zero would
        # drag every mean toward "no effect" for a reason that is not evidence.
        g = p.dropna(subset=["d_gross"])
        gross = (f"{g.d_gross.mean():+11.4f} {_t(g.d_gross):+7.2f} "
                 f"{int((g.d_gross > 0).sum()):4d}/{len(g):<3d}") if len(g) >= 2 else \
                f"{f'{len(p) - len(g)}/{len(p)} pairs too costly':>28s}"
        print(f"{label:26s} {len(p):3d} {p.d_net.mean():+10.4f} {_t(p.d_net):+7.2f} "
              f"{int((p.d_net > 0).sum()):4d}/{len(p):<3d} {gross}")
    print(f"  |t| > ~2.2 is the bar at n=14. A sign that flips between sheets is noise, "
          f"not a small effect.")

    print()
    print("=" * 108)
    print("3. WHAT DOES SPEED COST?   (median across the cells on each sheet)")
    print("=" * 108)
    print(f"{'class':14s} " + "".join(f"{tf:>22s}" for tf in ("1m", "2m", "3m", "5m")))
    for c in sorted({k[0] for k in sheets}):
        line = f"{c:14s} "
        for tf in ("1m", "2m", "3m", "5m"):
            d = sheets.get((c, tf, f"{c}/{tf}"))       # the untagged sheet only
            if d is None:
                line += f"{'-':>22s}"
            else:
                r = rules_only(d)
                tpy = (r["n_trades"] / r["n_names"] / r["years"]).median()
                line += f"{r['cost_drag_ann'].median():>13.1%}{tpy:>9,.0f}"
        print(line)
    print("  each cell: median annual cost drag, then median trades per name per year.")

    print()
    print("=" * 108)
    print("4. ANYTHING POSITIVE BEFORE COSTS?   (suppressed where the drag makes it meaningless)")
    print("=" * 108)
    for (c, tf, label), d in sorted(sheets.items()):
        r = rules_only(d)
        if r.empty:
            continue
        keep = r.dropna(subset=["gross"])
        dropped = len(r) - len(keep)
        ctrl = d.loc["RANDOM_50", "gross"] if "RANDOM_50" in d.index else np.nan
        if keep.empty:
            print(f"{label:26s} ALL {len(r)} cells suppressed - every one pays over "
                  f"{MAX_HONEST_DRAG:.0%}/yr, so `net + drag` describes no real account.")
            if np.isfinite(ctrl):
                print(f"{'':26s} the no-signal RANDOM_50 control would read {ctrl:+.2f} "
                      f"here, which is the proof rather than the argument.")
            continue
        pos = keep[keep["gross"] > 0]
        drop_note = f", {dropped} suppressed as too costly to reconstruct" if dropped else ""
        base = ctrl if np.isfinite(ctrl) else 0.0
        print(f"{label:26s} {len(pos):2d}/{len(keep):<3d} positive gross{drop_note}")
        print(f"{'':26s} read against the no-signal control at {base:+.4f}, not against zero")
        for lab, row in pos.nlargest(3, "gross").iterrows():
            note = "  <- inside control noise" if row["gross"] <= max(base, 0.0) else ""
            print(f"{'':28s}{lab[:44]:44s} gross {row['gross']:+.4f} "
                  f"net {row['cashmatch_excess_cagr']:+.4f} drag {row['cost_drag_ann']:.1%}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
