"""One declaration per leaderboard metric — the wire format, labels and help text.

Why this file exists
--------------------
A leaderboard column used to be spread across four places that had to agree by hand:
`ROW_FIELDS` (a positional list), the array literal in `build_payload._row_for_report`
(positional, index-matched), the hydration in `report.js`, and whatever hardcoded the
column header. Adding a metric meant four edits; getting the *index* wrong in either of
the first two shifted every later column, so `ir_net` rendered `headroom`'s number on a
page that looked entirely plausible. `zip()` truncates silently, so nothing raised.

That is the same failure shape as the three bugs this pipeline hit on 2026-08-09 — an IR
divided by float noise, an aggregate over an empty scenario filter, and a shortlist
compared against a function object. None crashed. All three returned a believable wrong
answer. Declaring a metric once removes the whole class for this layer: the field order,
the extraction, the rounding and the column header are all derived from the same row of
this table, so they cannot disagree.

Adding a metric
---------------
Append a `Metric(...)` and nothing else. `ROW_FIELDS` and the row builder follow
automatically, and the payload ships `label`/`help` so the page renders the header and
tooltip without hardcoding either.

**Appending is safe; inserting or reordering is not.** The wire format is positional, so
a payload written by one version and read by another must agree on order. Append at the
end and old readers keep working.

`source` may be a column name in the stage's row dict, or a callable taking
`(row, ctx)` for anything derived — `ctx` carries per-panel facts the row does not, like
which operators count as a combo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np


@dataclass(frozen=True)
class Metric:
    key: str                       # wire name, and the key `report.js` hydrates to
    source: str | Callable         # row column, or fn(row, ctx) -> value
    kind: str = "num"              # num | int | bool | str
    dp: int | None = None          # rounding for `num`
    label: str = ""                # column header
    help: str = ""                 # tooltip / one-line meaning
    group: str = ""                # for grouping columns in the UI


def _num(v, dp=None):
    """Non-finite becomes None: JSON has no NaN, and `allow_nan=False` would raise."""
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return round(f, dp) if dp is not None else f


def _coerce(metric: Metric, value):
    if metric.kind == "num":
        return _num(value, metric.dp)
    if metric.kind == "int":
        return None if value is None else int(value)
    if metric.kind == "bool":
        return bool(value)
    return value


# ---- derived fields ---------------------------------------------------------
# Kept as callables so the extraction lives beside the declaration rather than in a
# parallel array whose indices have to match.

def _is_combo(row, ctx) -> bool:
    op = row.get("op")
    return isinstance(op, str) and op in ctx["operators"]


def _combo_field(name: str):
    def get(row, ctx):
        return row.get(name) if _is_combo(row, ctx) else None
    return get


REGISTRY: tuple[Metric, ...] = (
    Metric("indicator", "rule", "str", label="Strategy",
           help="Rule name, or `A~B|op` for a pair."),
    Metric("rank", lambda row, ctx: ctx["rank"], "int", label="#",
           help="Position within this panel, by the ranking column."),
    Metric("is_baseline", "is_baseline", "bool", label="Baseline",
           help="Buy-and-hold. Never charged a cost and never flattened."),
    Metric("generic_fallback", "generic_fallback", "bool", label="Generic",
           help="Scored by the generic fallback rather than a hand-written signal."),
    Metric("is_combo", _is_combo, "bool", label="Pair",
           help="Two rules joined by an operator."),
    # Carried so pass 2 can rebuild a combo's position from its legs rather than
    # parsing it back out of the label.
    Metric("op", _combo_field("op"), "str", label="Operator",
           help="vote / and / or / gate."),
    Metric("leg_a", _combo_field("leg_a"), "str", label="Leg A", help="First rule."),
    Metric("leg_b", _combo_field("leg_b"), "str", label="Leg B", help="Second rule."),
    Metric("n_tickers", "n_assets", "int", label="Assets",
           help="Assets with a valid result. Breadth is computed from all of them."),

    Metric("ir_net", "ir_net", "num", 4, label="IR", group="score",
           help="Information ratio against buy-and-hold on the same asset. Pays a rule "
                "for time-in-market, so read exposure beside it."),
    Metric("ir_hit_rate", "ir_hit_rate", "num", 4, label="Breadth", group="score",
           help="Share of assets with positive IR."),
    Metric("headroom", "headroom", "num", 3, label="Cost headroom", group="score",
           help="How many times current costs the rule survives before IR hits zero."),
    Metric("t_stat", "t_stat", "num", 4, label="t", group="score",
           help="IR x sqrt(years). Not corrected for the number of rules searched."),
    Metric("loo_retention", "loo_retention", "num", 4, label="LOO", group="score",
           help="IR retained when the single best asset is dropped."),

    Metric("legacy_gate_ir", "legacy_gate_ir", "bool", label="gate IR", group="legacy"),
    Metric("legacy_gate_breadth", "legacy_gate_breadth", "bool", label="gate breadth",
           group="legacy"),
    Metric("legacy_gate_headroom", "legacy_gate_headroom", "bool", label="gate headroom",
           group="legacy"),
    Metric("legacy_gate_t", "legacy_gate_t", "bool", label="gate t", group="legacy"),
    Metric("legacy_passed", "legacy_passed", "int", label="gates", group="legacy",
           help="Retired 2026-08-08. Kept so old CSVs stay interpretable; no verdict "
                "is rendered from it."),

    Metric("total_pnl_dollars", "total_pnl_dollars", "num", 2, label="PnL", group="money",
           help="Summed dollar PnL at $10k per ticker. Same information as percent "
                "gain, scaled by the starting stake."),
    Metric("avg_cagr", "avg_cagr", "num", 4, label="CAGR", group="money"),
    Metric("avg_sharpe", "avg_sharpe", "num", 3, label="Sharpe", group="money",
           help="Raw Sharpe measures long-bias in a rising market; it is not the "
                "ranking column."),
    Metric("avg_max_drawdown", "avg_max_drawdown", "num", 4, label="Max DD",
           group="money"),
    Metric("turnover_per_year", "turnover_per_year", "num", 1, label="Turns/yr",
           group="cost"),
    Metric("n_trades", "n_trades", "int", label="Trades", group="cost"),
)

ROW_FIELDS: list[str] = [m.key for m in REGISTRY]

# Shipped in the payload so the page renders headers and tooltips from the same
# declaration the numbers came from, instead of a second hardcoded list.
FIELD_META: dict[str, dict] = {
    m.key: {"label": m.label or m.key, "help": m.help, "group": m.group,
            "kind": m.kind}
    for m in REGISTRY
}


def build_row(row: dict, ctx: dict) -> list:
    """One leaderboard row, in wire order, derived from `REGISTRY`.

    The order cannot drift from `ROW_FIELDS` because both come from the same tuple —
    which is the entire point of the file.
    """
    out = []
    for m in REGISTRY:
        value = m.source(row, ctx) if callable(m.source) else row.get(m.source)
        out.append(_coerce(m, value))
    return out
