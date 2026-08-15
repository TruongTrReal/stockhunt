"""Pairs of rules joined by an operator — the stage-2 search space.

A combo is not a new tested thing; it is a **composition of two tested things**, which is
why it lives in `overlays/` beside the volatility gate rather than as a file in
`published/`. There is also nothing to put in such a file: combos are never enumerated.
`combo_sweep.py` shortlists singles on train-period IR and crosses that shortlist with
the four operators, so the set of combos is a function of the training window. Change the
window and you get a different set.

Two things this module fixes, both of which were real.

**One definition of what an operator means.** `combine` used to live in
`backtest engine/signals.py` with a comment explaining that it sat there so the searcher
and the report renderer could not drift on the meaning of `and`. That reasoning is right
and is preserved exactly — there is still one definition, it has simply moved to where the
rest of the composition logic now lives, and `signals.py` re-exports it.

**One grammar.** The two combo stages disagreed on how a pair is identified:

    combo_sweep.py   "SMA_50 and RSI_14"          plus separate op/leg_a/leg_b columns
    combo_wf.py      "HT_TRENDMODE~MAXINDEX|or"   label only, no leg columns

so a walk-forward combo could not be rebuilt from its own sheet by anything that did not
already know the grammar, and `strategies.registry.build` returned `None` for every one of
them — silently, because `build` swallows failures. On the dashboard leaderboard 22 of the
top 25 rows are pairs, so every portfolio, deflated-Sharpe and factor-alpha number
computed through the registry was quietly skipping most of the board. `A~B|op` is now
canonical and the legacy `A op B` form still parses, so old sheets stay readable.

**Legs are resolved by a callback, never built here.** `signals.py` owns the benchmark
plumbing, the NaN policy and end-of-day flattening, and duplicating any of that to make
this module self-contained is exactly how two engines stop agreeing. `apply` takes a
`resolve` function; callers that have an asset class and timeframe to hand pass one bound
to `signals.position_for`, and the legs are resolved — and therefore flattened — before
they are combined, which is the order `signals.position_for_row` has always used.
"""

from __future__ import annotations

import numpy as np

LEG = "~"                      # `HT_TRENDMODE~MAXINDEX|or`
OP = "|"
OPERATORS = ("vote", "and", "or", "gate")


def combine(a: np.ndarray, b: np.ndarray, op: str) -> np.ndarray:
    """Two 1/0/-1 position series -> one.

    The single definition of what an operator means. `combo_sweep.py` (which searches)
    and `build_payload.py` (which has to redraw the winner's curve) both reach it through
    `signals.combine`, so they cannot drift apart on the meaning of `and`.
    """
    if op == "vote":
        return np.sign(a + b)
    if op == "and":
        return np.where(np.sign(a) == np.sign(b), a, 0.0)
    if op == "or":
        return np.where(a != 0, a, b)
    if op == "gate":
        return np.where(b != 0, a, 0.0)
    raise ValueError(f"unknown operator {op!r}")


def parse(label: str) -> tuple[str, str, str] | None:
    """`A~B|op` -> (A, B, op). Returns None if this is not a combo label.

    The legacy `A op B` form written by `combo_sweep.py` is also accepted, because the
    single-split sheets already on disk use it and a parser that cannot read them would
    make those results unrebuildable.
    """
    if not isinstance(label, str):
        return None

    if OP in label and LEG in label:
        legs, _, op = label.rpartition(OP)
        a, _, b = legs.partition(LEG)
        if op in OPERATORS and a and b:
            return a.strip(), b.strip(), op
        return None

    # Legacy: "SMA_50 and RSI_14". Checked against the operator list rather than by
    # splitting on whitespace, so a rule name containing a space cannot be mistaken for
    # an operator.
    for op in OPERATORS:
        token = f" {op} "
        if token in label:
            a, _, b = label.partition(token)
            if a and b:
                return a.strip(), b.strip(), op
    return None


def encode(a: str, b: str, op: str) -> str:
    """Canonical label for a pair. Use this everywhere new labels are minted."""
    if op not in OPERATORS:
        raise ValueError(f"unknown operator {op!r}")
    return f"{a}{LEG}{b}{OP}{op}"


def is_combo(label: str) -> bool:
    return parse(label) is not None


def apply(label, df, close, bpy, symbol, resolve):
    """Build both legs with `resolve` and combine them. None if either leg is unbuildable.

    `resolve(label, df, close, bpy, symbol) -> np.ndarray | None`. Passing it in rather
    than importing a builder keeps this module free of any dependency on the engine, and
    lets a caller that knows the asset class supply legs that are correctly flattened.
    """
    parsed = parse(label)
    if parsed is None:
        return None
    a_label, b_label, op = parsed
    pa = resolve(a_label, df, close, bpy, symbol)
    if pa is None:
        return None
    pb = resolve(b_label, df, close, bpy, symbol)
    if pb is None:
        return None
    pa = np.asarray(pa, dtype="float64")
    pb = np.asarray(pb, dtype="float64")
    if pa.size != len(df) or pb.size != len(df):
        return None
    return combine(pa, pb, op)
