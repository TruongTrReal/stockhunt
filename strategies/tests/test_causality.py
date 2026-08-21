"""Gate every published strategy on causality, metadata and label round-tripping.

Exits nonzero on any failure, so it can be gated on the same way `parity.py` is. There is
no pytest in this project's venv and this deliberately does not add one.

**Causality is tested by truncation, not by reading the code.** Build positions on the
full series and on the series minus the last N bars, then assert the overlapping region
is identical. A rule whose value at bar *t* depends on bars after *t* cannot pass, and no
amount of staring at `rolling()` calls substitutes — that is exactly how
`np.nanmedian(whole_series)` survived in `prereg.volmanaged` and `variants._vol_scale`
long enough to contaminate two published stages. Truncation caught it; review had not.

The metadata check exists because the split into one-file-per-strategy made provenance a
per-file responsibility. A strategy with no `SOURCE` is not a replication of anything,
and this repo's entire premise is testing *published* rules rather than invented ones.

Run::

    python strategies/tests/test_causality.py                 # everything
    python strategies/tests/test_causality.py --rules ibs     # one strategy
    python strategies/tests/test_causality.py --truncate 750  # a deeper cut
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backtest engine")):
    if p not in sys.path:
        sys.path.insert(0, p)

from engines import vector                                  # noqa: E402
import td_loader                                            # noqa: E402
from strategies.registry import CATALOG, build, cells, decode, encode  # noqa: E402

# Two names per class, and they must differ in shape: a mean-reversion rule can look
# perfectly causal on a trending mega-cap and leak on a choppy one.
PROBES = {
    "us_stocks": ["AAPL", "JNJ"],
    "us_etfs": ["SPY", "TLT"],
    "crypto": ["BTC/USD"],
    "commodities": ["XAU/USD"],
}
TRUNCATE = 500


def load_probes(timeframe: str = "1d") -> list[tuple[str, str, object]]:
    out = []
    for asset_class, symbols in PROBES.items():
        try:
            data = td_loader.load(asset_class, timeframe, symbols=symbols)
        except Exception:
            continue
        for symbol, df in data.items():
            if len(df) > TRUNCATE * 3:
                out.append((asset_class, symbol, df))
    return out


# A disagreement confined to the last few bars of the truncated series is a different
# animal from one in the interior, and conflating them makes the gate useless.
#
# `turn_of_month` is the live example. `_day_ordinals` counts how many sessions a month
# contains *from the observed bars*, so cutting the series mid-month shrinks that count
# and flips `pos > size - before` for the final 1-3 bars. Economically it is not a leak —
# an exchange calendar is published years ahead, and a trader genuinely knows which
# session is January's last — but the implementation infers it from data, so any boundary
# is ambiguous. Positions are built on the whole series and sliced afterwards, so the
# affected bars are the last few of the entire dataset, not of every fold.
#
# An interior disagreement has no such excuse and fails.
EDGE_BARS = 5


def check_causality(label: str, probes, truncate: int) -> tuple[list[str], list[str]]:
    """A position at bar t must not change when bars after t are removed.

    Returns `(failures, edge_warnings)`.
    """
    failures, edges = [], []
    for asset_class, symbol, df in probes:
        close = df["Close"].to_numpy("float64")
        bpy = vector.bars_per_year(df.index)
        full = build(label, df, close, bpy, symbol)
        if full is None:
            continue
        n = len(df) - truncate
        short = build(label, df.iloc[:n], close[:n], bpy, symbol)
        if short is None:
            failures.append(f"{label} on {symbol}: builds on full series but not truncated")
            continue
        bad_mask = ~np.isclose(full[:n], short, equal_nan=True)
        if not bad_mask.any():
            continue
        idx = np.flatnonzero(bad_mask)
        where = (f"{int(bad_mask.sum())} of {n} past positions changed "
                 f"(bars {int(idx.min())}..{int(idx.max())} of {n})")
        if idx.min() >= n - EDGE_BARS:
            edges.append(f"{label} on {asset_class}/{symbol}: {where} — boundary only")
        else:
            failures.append(f"{label} on {asset_class}/{symbol}: {where} — LOOK-AHEAD")
    return failures, edges


def check_metadata() -> list[str]:
    failures = []
    for name, spec in CATALOG.items():
        if not spec.rule:
            failures.append(f"{name}: no RULE")
        if not spec.source:
            failures.append(f"{name}: no SOURCE — provenance is the point of this repo")
        if not spec.family:
            failures.append(f"{name}: no FAMILY")
        if not spec.grid:
            failures.append(f"{name}: empty GRID")
        elif not isinstance(spec.published, dict):
            failures.append(f"{name}: GRID[0] must be the published parameter dict")
    return failures


def check_labels() -> list[str]:
    """Every generated label must decode back to the parameters that produced it."""
    failures = []
    for name, spec in CATALOG.items():
        for params in spec.grid:
            label = encode(name, params, spec)
            try:
                back_name, back_params = decode(label)
            except Exception as exc:
                failures.append(f"{label}: does not decode ({exc})")
                continue
            if back_name != name:
                failures.append(f"{label}: decoded to {back_name}")
            if back_params != params:
                failures.append(f"{label}: round-trip changed params "
                                f"{params} -> {back_params}")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rules", nargs="+", default=None)
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--truncate", type=int, default=TRUNCATE)
    args = ap.parse_args()

    print(f"{len(CATALOG)} published strategies discovered from strategies/published/")
    failures = check_metadata() + check_labels()
    print(f"metadata + label round-trip: {'OK' if not failures else f'{len(failures)} FAIL'}")

    probes = load_probes(args.tf)
    if not probes:
        print("no probe data cached — run td_loader.py first")
        return 1
    print(f"causality probes: {', '.join(s for _, s, _ in probes)} "
          f"(truncating {args.truncate} bars)")

    labels = args.rules or [lab for c in PROBES for lab in cells(c)]
    labels = list(dict.fromkeys(labels))
    # Overlays wrap arbitrary labels, so they need testing too — the vol gate's threshold
    # is an expanding quantile precisely because a whole-series one would leak here.
    labels += [f"volregime:{side}:0.5:{lab}"
               for lab in (args.rules or ["ibs", "faber_gtaa", "volmanaged"])
               for side in ("hi", "lo")]
    # The Heikin-Ashi overlay's open is a forward recursion seeded at bar 0, so it is
    # causal by construction — but "by construction" is exactly the claim this gate
    # refuses to take on faith, for overlays as for rules. Wrap the eight Pine
    # conversions it exists for, at their published cells.
    pine = args.rules or [lab for name in
                          ("bar_updn", "pivot_center", "range_filter",
                           "range_filter_macd", "ema_cross_sniper", "bb_outside_in",
                           "ssl_hybrid", "lorentzian_knn")
                          for lab in (encode(name, CATALOG[name].grid[0],
                                             CATALOG[name]),
                                      f"{name}@allow_short=0")]
    labels += [f"{wrap}{lab}" for lab in pine
               for wrap in ("ha:", "chart:", "ha:chart:")]

    causal, edges = [], []
    for label in labels:
        f, e = check_causality(label, probes, args.truncate)
        causal += f
        edges += e
    print(f"causality ({len(labels)} labels x {len(probes)} symbols): "
          f"{'OK' if not causal else f'{len(causal)} FAIL'}"
          f"{f', {len(edges)} boundary-only' if edges else ''}")
    if edges:
        affected = sorted({e.split(" on ")[0].split("@")[0] for e in edges})
        print(f"  boundary-only (calendar inferred from data, last {EDGE_BARS} bars "
              f"of the series): {', '.join(affected)}")

    failures += causal
    if failures:
        print(f"\n=== {len(failures)} FAILURES ===")
        for f in failures[:40]:
            print(f"  {f}")
        if len(failures) > 40:
            print(f"  ... and {len(failures) - 40} more")
        return 1
    print("\nall published strategies are causal, documented and round-trip cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
