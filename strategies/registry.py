"""Discovers the published strategies and turns a label into a position series.

One file per strategy, discovered by import rather than listed here. Adding a strategy is
adding a file to `published/`; there is no registration step to forget, and no central
list that can disagree with what is on disk.

The label grammar is unchanged and must stay that way — every CSV in this repo is keyed
on it, going back three studies:

    ibs                      the published parameter set
    ibs@buy=0.3              a variant, written as a diff against the published one
    volregime:hi:0.5:ibs     an overlay wrapping any of the above
    BUYHOLD, ALWAYS_FLAT     the controls, which are not strategies

A module in `published/` is a strategy if it defines `position` and `GRID`. Everything
else it declares is metadata that used to live in one central dataclass literal:

    RULE     what it does, in English
    SOURCE   where it was published — this is a replication study, so provenance is data
    FAMILY   trend | reversion | calendar | volatility | regime
    ANCHOR   already run by prereg.py on 2026-08-05
    CLASSES  None for every asset class, else a tuple
    NOTE     anything a reader must know before quoting a result, including known defects
    LOGIC    the long-form explanation: what the indicator measures, the claim being
             tested, what the position is REALLY exposed to, and how it fails. A one-line
             RULE tells you what the code does; it does not let you read a result.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from strategies import published as _published_pkg
from strategies.controls import (BASELINE, RANDOM_BLOCK, RANDOM_SEED,  # noqa: F401
                                 random_control)
from strategies.overlays import combo, regime

SEP = "@"                      # `rsi2_connors@buy=5,exit_ma=10`


@dataclass(frozen=True)
class Strategy:
    fn: Callable
    rule: str                          # the rule in plain English
    source: str                        # where it was published
    family: str
    grid: tuple                        # dicts of params; grid[0] IS the published one
    anchor: bool = False               # already run by prereg.py on 2026-08-05
    classes: tuple | None = None       # None = every class
    note: str = ""
    logic: str = ""                    # the long-form explanation
    module: str = ""                   # where it lives, for anyone chasing a number

    @property
    def published(self) -> dict:
        return self.grid[0]


DRAFTS: list[str] = []


def _discover() -> dict[str, Strategy]:
    """Import every module in `published/` and build the catalog from what they declare.

    Sorted by module name so `CATALOG` has a stable order regardless of filesystem
    enumeration — an unstable order would silently reshuffle `cells()`, and this project
    charges itself for the number of trials it runs.
    """
    out: dict[str, Strategy] = {}
    for info in sorted(pkgutil.iter_modules(_published_pkg.__path__),
                       key=lambda i: i.name):
        mod = importlib.import_module(f"{_published_pkg.__name__}.{info.name}")
        fn, grid = getattr(mod, "position", None), getattr(mod, "GRID", None)
        if fn is None or not grid:
            continue
        # A scaffolded file whose position() still raises NotImplementedError would
        # be swallowed by `build`'s except clause and appear as a strategy that
        # simply never trades — indistinguishable on a leaderboard from a real rule
        # that does nothing, which is the same failure `usable_rules` exists to avoid.
        if getattr(mod, "DRAFT", False):
            DRAFTS.append(info.name)
            continue
        out[info.name] = Strategy(
            fn=fn, rule=getattr(mod, "RULE", ""), source=getattr(mod, "SOURCE", ""),
            family=getattr(mod, "FAMILY", ""), grid=tuple(grid),
            anchor=bool(getattr(mod, "ANCHOR", False)),
            classes=getattr(mod, "CLASSES", None), note=getattr(mod, "NOTE", ""),
            logic=(getattr(mod, "LOGIC", "") or "").strip(),
            module=mod.__name__)
    return out


CATALOG: dict[str, Strategy] = _discover()


def encode(name: str, params: dict, strategy: Strategy) -> str:
    """`rsi2_connors` for the published cell, `rsi2_connors@buy=5.0` for the rest.

    Only parameters that DIFFER from the published values are written, so the published
    cell keeps a bare name and every other label reads as a diff against it.
    """
    diff = {k: v for k, v in params.items() if strategy.published.get(k) != v}
    if not diff:
        return name
    return name + SEP + ",".join(f"{k}={v}" for k, v in sorted(diff.items()))


def decode(label: str) -> tuple[str, dict]:
    """Label -> (strategy name, full parameter dict), published values filling the rest."""
    name, _, tail = label.partition(SEP)
    strategy = CATALOG[name]
    params = dict(strategy.published)
    if tail:
        for piece in tail.split(","):
            k, _, v = piece.partition("=")
            params[k] = type(strategy.published[k])(float(v))
    return name, params


def cells(asset_class: str) -> list[str]:
    """Every (strategy, parameter) label runnable on this class, published cells first."""
    out = []
    for name, s in CATALOG.items():
        if s.classes is not None and asset_class not in s.classes:
            continue
        out.extend(encode(name, p, s) for p in s.grid)
    return out


def skipped_for(asset_class: str) -> list[str]:
    """Strategies this class cannot express. Counted and reported, never run as flat.

    A rule that is undefined on a class and gets scored anyway produces a flat position,
    which on a leaderboard is indistinguishable from a rule that simply does nothing —
    the same reasoning as `signals.usable_rules` and the crypto volume rules.
    """
    return [n for n, s in CATALOG.items()
            if s.classes is not None and asset_class not in s.classes]


def build(label: str, df: pd.DataFrame, close: np.ndarray, bpy: float,
          symbol: str = "", draw: int = 0, resolve=None) -> np.ndarray | None:
    """Label -> position series, or None if this label cannot be built here.

    `resolve` is how a caller supplies legs this module cannot build itself. The 231
    TA-Lib singles need an asset class and timeframe to pick a benchmark and to decide
    end-of-day flattening, both of which `signals.py` owns, so a combo of two TA-Lib
    rules is only buildable by a caller that has that context. Pass
    `resolve=lambda lab, df, close, bpy, sym: signals.position_for(...)` and combos
    resolve; omit it and only combos of published strategies and controls will.

    Returning None rather than raising is deliberate and load-bearing elsewhere, but it
    is also why the combo branch below matters: before it existed every `A~B|op` label
    returned None *silently*, so combos vanished from any sheet built through here
    instead of failing loudly.
    """
    def _self(lab, d, c, b, s):
        return build(lab, d, c, b, s, draw, resolve)

    leg_resolver = resolve or _self

    if label in (BASELINE, "ALWAYS_LONG"):
        return np.ones(len(df), dtype="float64")
    if label == "ALWAYS_FLAT":
        return np.zeros(len(df), dtype="float64")
    if label.startswith("RANDOM_"):
        return random_control(len(df), int(label.split("_")[1]) / 100.0, symbol, draw)
    if label.startswith(regime.REGIME_PREFIX + regime.REGIME_SEP):
        # `build` is handed to the overlay rather than imported by it: the overlay wraps
        # arbitrary labels, so importing this module from there would be circular. The
        # base may itself be a combo, which is why this branch is checked first.
        return regime.apply(label, df, close, bpy, symbol, _self)
    if combo.is_combo(label):
        return combo.apply(label, df, close, bpy, symbol, leg_resolver)
    try:
        name, params = decode(label)
        pos = CATALOG[name].fn(df, close, bpy, **params)
    except Exception:
        return None
    pos = np.nan_to_num(np.asarray(pos, dtype="float64"), nan=0.0,
                        posinf=0.0, neginf=0.0)
    return pos if pos.size == len(df) else None
