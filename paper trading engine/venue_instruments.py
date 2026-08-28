"""Make an instrument known to a venue that is ALREADY RUNNING.

The desk seeds every instrument it might ever need into the Nautilus cache at build time
(`run_paper.build_node`), which works because the universe is known then. A symbol
admitted by `symbol_resolve` is not: the registration naming it arrives over the ledger,
minutes or days after the node came up, and there is no restart in that path by design.

**The failure this exists to prevent is silent, and it was measured rather than assumed.**
`SimulatedExchange.process_bar` looks the instrument up in the CACHE and creates a
matching engine for it on the spot; if the cache has never heard of it, the call raises
`RuntimeError: No matching engine found for ZZTEST.SANDBOX`. That raise happens inside
`run_paper.route_bars_to_sandbox`'s forwarding handler, which catches every exception on
purpose so one bad bar cannot kill the feed — so the symptom is not an error. The symptom
is a book that receives bars, marks nothing, fills nothing and reads healthy in every log
line, which is this folder's worst failure mode and it has happened before.

So two things are done, in this order, before a strategy for an open symbol is attached:

    cache.add_instrument(inst)      what `process_bar` reads to build a matching engine
    exchange.add_instrument(inst)   builds it now, rather than on the first bar

The second is not redundant with the first. Relying on the lazy path means relying on a
`RuntimeError` raised inside a handler that swallows it, which is a guarantee held by
nothing; building the engine at attach makes the failure loud and immediate, at the moment
somebody is looking at the registration.

**The exchanges are registered here rather than reached for**, because there is no route
from a Nautilus `Controller` to the execution engine — a Controller is an Actor, and an
Actor has a cache and a message bus and no exec clients. `run_paper.route_bars_to_sandbox`
already walks exactly the clients that own one, so it registers them on the way past. This
module imports neither `nautilus_trader` nor `run_paper`: the first would put the trading
stack behind a plain dict, and the second would close the import ring, since `run_paper`
imports `desk_control` which imports this.
"""

from __future__ import annotations

# Venue name -> a callable taking one instrument. In the live desk that is
# `SandboxExecutionClient.exchange.add_instrument`; a test may register anything with the
# same shape, which is the point of holding a callable rather than an exchange object.
_ADDERS: dict[str, object] = {}


def register(venue: str, add_instrument) -> None:
    """Record how to give VENUE a new instrument while it is running."""
    _ADDERS[str(venue)] = add_instrument


def clear() -> None:
    """Forget every venue. For tests, and for a second node in one process."""
    _ADDERS.clear()


def venues() -> list[str]:
    return sorted(_ADDERS)


def publish(instrument) -> bool:
    """Give a running venue an instrument it has never seen. True if a venue took it.

    False — not an exception — when no venue is registered, because that is the ordinary
    state of every process that is not the live desk: `backtest_paper.py`, the pytest
    suite, `catalog.py`. The caller has already put the instrument in the cache, which is
    what the exchange reads anyway, so a False is a missed optimisation and never a
    missing instrument.

    A venue that REFUSES the instrument is a different matter and is raised: an exchange
    rejecting an instrument is telling the desk this symbol cannot trade there, and
    swallowing that would attach a strategy whose every order will be refused.
    """
    add = _ADDERS.get(str(instrument.id.venue))
    if add is None:
        return False
    add(instrument)
    return True
