"""What one fill realised, and the cost basis it is measured against.

Every strategy here used to publish `equity() - capital` on each fill — the **whole
book's** mark at that instant — and the board printed it under the heading *Realised
P&L*. Those are different quantities and the gap between them is not small: on
`00:us_stocks-1d-ibs` all eight completed round trips made money, +$188.43 together, and
the page reported 20 losing trades, a 13% win rate and a profit factor of 0.04. Two names
filling in the same second got the same book snapshot stamped on both, so one number was
counted twice; a BUY, which cannot realise anything, was counted as a closed trade
whenever some unrelated name in the book had moved since its last mark.

So the two are now separate columns with separate meanings, and this module owns the one
that was missing:

    book_pnl        equity - capital at the moment of the fill. A snapshot of the BOOK.
    realised_pnl    what THIS fill closed, against the position's average cost. NULL on a
                    fill that opened or added, because such a fill realises nothing —
                    which is not the same fact as realising zero.

**Average cost, not the opening price.** A position built over three bars has three prices
behind it and only their weighted average answers "what did the part I just sold cost".
`_entry` used to hold the opening fill's price and ignore every add, which also made the
per-name `pnl_pct` on the board wrong on any name the rule added to; the strategies now
keep this basis instead, so there is one definition and the two figures agree.

Pure arithmetic over floats — no Nautilus, no database, no strategy. That is what lets the
same function serve the live path, the migration that backfills the existing record, and a
test suite that runs in milliseconds.
"""

from __future__ import annotations

# A position within this of zero is flat. Matches the tolerance the strategies already use
# for the same question; a venue's rounded quantity never lands exactly on 0.0.
FLAT = 1e-12


def apply_fill(units: float, cost: float | None, signed: float, price: float
               ) -> tuple[float | None, float | None]:
    """One fill against one position. Returns `(realised, cost_after)`.

    `units` is the position BEFORE the fill; `signed` is the fill itself, positive for a
    buy and negative for a sell. `cost` is the position's average cost, or None when flat.

    `realised` is **None when the fill closed nothing** and a float — possibly 0.0 — when
    it did. The distinction carries the whole point of this module: a scratch trade that
    closes exactly at cost realised zero and is a closed trade; an opening buy realised
    nothing and is not one. Collapsing those two into 0.0 is how the old column came to
    report 23 closed trades from 8 sells.

    A reversal is handled as what it is — a close of the whole old position followed by an
    open of the remainder at this price — so a rule that flips from long to short in one
    order books its long P&L rather than carrying it silently into the short.
    """
    if abs(signed) <= FLAT:
        return None, cost

    # Opening from flat: nothing to realise, and this price is the basis.
    if abs(units) <= FLAT:
        return None, price

    base = cost if cost is not None else price
    after = units + signed

    # Adding in the direction already held: still nothing realised, and the basis is the
    # weighted average of what was there and what just arrived.
    if (units > 0) == (signed > 0):
        total = abs(units) + abs(signed)
        return None, (base * abs(units) + price * abs(signed)) / total

    # Opposing the position, so some of it closes. A long realises price - cost; a short
    # realises the negative of that.
    closed = min(abs(signed), abs(units))
    realised = closed * (price - base) * (1.0 if units > 0 else -1.0)

    if abs(after) <= FLAT:
        return realised, None            # flat again — no basis to carry
    if (after > 0) == (units > 0):
        return realised, base            # partial close leaves the basis alone
    return realised, price               # reversed — the remainder opened here


def replay(fills) -> list[float | None]:
    """The realised P&L of each of ONE symbol's fills, in order.

    `fills` is a sequence of `(side, qty, price)`. Used by the store's v2 -> v3 migration
    to recover the column for a record written before it existed: the fills are the whole
    input, so the value is recomputable exactly rather than lost.
    """
    units, cost, out = 0.0, None, []
    for side, qty, price in fills:
        signed = float(qty) if str(side).upper() == "BUY" else -float(qty)
        realised, cost = apply_fill(units, cost, signed, float(price))
        units += signed
        out.append(realised)
    return out
