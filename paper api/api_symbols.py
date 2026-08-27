"""How this desk spells an instrument, in the one place all three doors read.

A symbol reaches this process typed into the console's picker, written into a deploy
script, and copied off a TradingView chart. Every one of those doors folds it to a single
spelling before anything is compared, because the desk compares them as plain strings and
nothing downstream is forgiving: `desk_control` checks each registered name against
`paper_config.CLASS_OF`, and `desk_orders` checks `symbol not in registration["symbols"]`.
One letter in the wrong case is therefore a `201` here and a refusal there, minutes later,
in a `reason` on a table nobody is still watching.

**It is a module of its own because three callers need the identical answer and the
imports already run one way**: `api_strategies` -> `api_webhook` -> `api_orders`. Putting
the fold in `api_strategies` and importing it from `api_orders` would close that ring.
Restated here rather than imported from the engine, like the class list and the
timeframes, for the reason `api_paths` exists: this process imports no trading code.
"""

from __future__ import annotations

import re

# Databento's continuous-contract symbology: root, roll rule, rank. `ES.v.0` is the front
# E-mini S&P 500 rolled on volume, and the roll rule is a LOWER-case letter there (`v`
# volume, `c` calendar, `n` open interest). The desk holds the vendor's own spelling
# verbatim -- `config.CME_FUTURES` is `ES.v.0`, `GC.v.0`, `CL.v.0` -- so `cme_futures` is
# the one class on this desk whose symbols are not all capitals.
#
# Which is what makes the ordinary fold dangerous rather than merely wrong. Every other
# class spells its names in caps (`SPY`, `BTC/USD`, `XAU/USD`), so a symbol is
# upper-cased on the way in: somebody typing `spy` means `SPY`. That same fold turns
# `ES.v.0` into `ES.V.0`, which is in no universe and belongs to no class -- the console
# would offer the symbol from the desk's own published list and then register a name the
# desk cannot match.
#
# Anchored and bounded on purpose. `BRK.B` carries one dot and no trailing rank, so it
# does not match and survives the fold unchanged, which is the property that lets this run
# on every class instead of only on futures.
CONTINUOUS = re.compile(r"^([A-Z0-9]{1,6})\.([A-Z])\.(\d{1,2})$")


def canonical(symbol: str) -> str:
    """The desk's spelling of what somebody typed.

    `es.v.0`, `ES.V.0` and `ES.v.0` are one symbol; `spy` and ` SPY ` are one symbol.
    Nothing here decides whether the instrument exists — the published universe answers
    that in `/v1/limits`, and the desk's check is the one that binds.
    """
    s = symbol.strip().upper()
    m = CONTINUOUS.match(s)
    return f"{m.group(1)}.{m.group(2).lower()}.{m.group(3)}" if m else s
