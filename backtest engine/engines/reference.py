"""Ground truth: an explicit share-level simulation, one bar at a time.

Ported from `../engine-bakeoff/common.py:simulate`, which is the arithmetic both
NautilusTrader and manifoldbt were scored against in that bake-off. It is deliberately a
plain Python loop rather than the `cumprod` the fast path uses: the two are only equal
when fills happen at the same close the return is measured from, and having an
independent derivation is the entire point. If this and `vector.py` ever disagree, one
of them has a bug — which is a thing a single engine can never tell you.

Extended here in one way the bake-off did not need: **shorting and transaction costs**.
The bake-off was long/flat with no fees, so a faithful port would not exercise the paths
this project actually uses.

Slow by construction — a Python loop over bars. `parity.py` runs it on sampled cells,
never on the sweep.
"""

from __future__ import annotations

import numpy as np


def simulate(close: np.ndarray, target: np.ndarray, fee,
             initial_capital: float, bpy: float = None) -> dict:
    """Mark-to-market a 1/0/-1 target series, trading at each bar's close.

    `target[i]` is the exposure established at bar *i*'s close, so it earns the return
    over (i, i+1]. That is the raw rule output — the shift to "signal at t trades t+1"
    is inherent in trading at the close of the signal bar, which is why the caller
    passes the position series unshifted. Shifting it here as well would delay every
    trade by an extra bar.

    **Three conventions are aligned with `vector.py` on purpose**, because a comparison
    between the two is only meaningful if they are modelling the same thing:

    1. **The position is re-sized to the target *fraction* of equity on every bar**, not
       held as a fixed share count until the signal changes. This is what the project's
       `(1 + pos.shift(1) * ret).cumprod()` has always meant, and the difference is not
       cosmetic: a static -1 short through four +10% bars ends at 0.536x capital, while
       a constantly-re-sized one ends at 0.9^4 = 0.656x. They agree exactly for long and
       flat — which is why the long/flat bake-off in `../engine-bakeoff/` never surfaced
       it. Rebalancing is not charged a fee; only a change in *target* is, matching the
       cost model in `vector.net_returns`.
    2. the fee is charged against the *previous* mark-to-market equity, not the
       post-growth value — otherwise the two differ by `gross x cost`, a second-order
       term that would sit right at the parity tolerance and mask real bugs;
    3. the opening trade is charged.

    Everything else — the share accounting, the loop, the cash ledger — is derived
    independently. That is what makes disagreement informative.

    Returns final equity, trade count, and the equity curve.
    """
    if not isinstance(fee, dict):
        fee = {"commission_bps": float(fee), "half_spread_bps": 0.0,
               "sell_fee_bps": 0.0, "borrow_annual": 0.0}
    per_side = (fee["commission_bps"] + fee["half_spread_bps"]) / 10_000.0
    sell_fee = fee["sell_fee_bps"] / 10_000.0
    borrow = fee.get("borrow_annual", 0.0)
    borrow_per_bar = (borrow / bpy) if (borrow and bpy and bpy > 0) else 0.0

    close = np.asarray(close, dtype="float64")
    target = np.asarray(target, dtype="float64")
    n = close.size

    cash = float(initial_capital)
    shares = 0.0
    equity = np.empty(n, dtype="float64")
    trades = 0
    prev = 0.0
    prev_equity = float(initial_capital)

    for i in range(n):
        price = close[i]
        want = target[i]
        if not np.isfinite(price) or price <= 0:
            equity[i] = prev_equity
            continue

        value = cash + shares * price          # mark to market at this bar's close
        # Borrow accrues on the exposure held *through* this bar, i.e. the previous
        # target, and only while it was short.
        if borrow_per_bar and prev < 0.0:
            value -= (-prev) * borrow_per_bar * prev_equity
        if want != prev:
            delta = want - prev
            value -= abs(delta) * per_side * prev_equity
            if delta < 0.0:                    # a sale: regulatory fee, one direction
                value -= (-delta) * sell_fee * prev_equity
            trades += 1
            prev = want

        # Re-size to the target fraction every bar, fee or no fee. For want in {0, 1}
        # this is a no-op on the share count; for a short it is the whole difference.
        shares = want * value / price
        cash = value - shares * price

        equity[i] = value
        prev_equity = value

    return {
        "final_equity": float(equity[-1]),
        "total_return": float(equity[-1] / initial_capital - 1.0),
        "n_trades": trades,
        "equity": equity,
    }


def final_equity(position: np.ndarray, close: np.ndarray, fee,
                 capital: float, bpy: float = None) -> float:
    """Same signature as `vector.final_equity`, for the parity harness.

    `position` is passed through unshifted — see `simulate`: trading at the close of the
    signal bar *is* the one-bar delay.
    """
    return simulate(close, position, fee, capital, bpy)["final_equity"]
