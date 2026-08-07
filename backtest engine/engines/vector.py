"""The fast path: a vectorised long/flat/short backtest.

Not an approximation of the event-driven engines — it is the arithmetic they were
scored against. In `../engine-bakeoff/`, a share-level simulation of this same model was
the ground truth and NautilusTrader matched it to 5.7e-6, that residual being Nautilus's
own quantisation (positions to 6dp, cash to the cent). What the vectorised form cannot
represent is whole-share rounding, real order types and partial fills; `validate.py`
re-runs survivors in Nautilus for exactly that.

Conventions, all carried from the two earlier studies because departing from any one of
them makes results incomparable with everything already learned:

* positions are 1 / 0 / -1 and shifted one bar before multiplying by returns, so a
  signal computed on bar *t*'s close only ever trades bar *t+1*;
* cost is charged on |change in position|, so flat->long costs one side and a
  long->short reversal costs two;
* net returns are clipped at -0.999 before compounding, so a short losing more than
  100% in a bar cannot drive equity negative and flip positive on the next multiply;
* annualisation is measured from the index span, never a theoretical constant;
* standard deviations use ddof=1 everywhere (pandas' default, not numpy's).

Everything here takes and returns numpy arrays. One rule on one asset at a time, so
peak memory is one int8 position series — a 3.35M-bar crypto 1-minute series is 3.3 MB,
against 774 MB for a materialised 231-rule tensor over the same span.
"""

from __future__ import annotations

import numpy as np

SECONDS_PER_YEAR = 365.25 * 24 * 3600
RETURN_FLOOR = -0.999


def bars_per_year(index) -> float:
    """Measure annualisation empirically.

    The vendor's intraday grid is not uniform — holidays, half days, and two different
    session-aligned hourly grids in this data — so 252/1638/19656 would quietly
    mis-annualise every Sharpe and IR on the intraday sheets. A US equity 4h "day" is
    one 4h bar plus a 2.5h stub, which no constant describes.
    """
    span = (index[-1] - index[0]).total_seconds()
    if span <= 0:
        return float("nan")
    return len(index) * SECONDS_PER_YEAR / span


def net_returns(position: np.ndarray, close: np.ndarray, fee, bpy: float = None
                ) -> np.ndarray:
    """Per-bar net return of holding `position`, after real fees.

    `fee` is either a scenario dict from `config.FEE_SCENARIOS` or a bare number, which
    is read as symmetric per-side basis points (kept so parity tests and ad-hoc callers
    can still pass a scalar).

    Four charges, applied to the three different things they are actually levied on:

    * ``commission_bps + half_spread_bps`` on ``|d position|`` — both sides of a trade
      pay to take liquidity;
    * ``sell_fee_bps`` on the **sell** portion only — US regulatory fees (SEC Section 31,
      FINRA TAF) are charged on sales, including short sales, and not on buys;
    * ``borrow_annual`` accrued per bar on the position **held short** during that bar.
      This one was previously missing entirely, and it is not decorative: the leading
      rules hold shorts for 8% of bars on daily equities and up to 50% on hourly.

    `bpy` (bars per year) is needed only for the borrow accrual; without it a borrow
    rate cannot be pro-rated to a bar and is ignored.
    """
    if not isinstance(fee, dict):
        fee = {"commission_bps": float(fee), "half_spread_bps": 0.0,
               "sell_fee_bps": 0.0, "borrow_annual": 0.0}
    close = np.asarray(close, dtype="float64")
    position = np.asarray(position, dtype="float64")

    ret = np.empty_like(close)
    ret[0] = 0.0                          # no prior bar to have earned a return over
    ret[1:] = close[1:] / close[:-1] - 1.0

    held = np.empty_like(position)
    held[0] = 0.0                         # flat before the first signal
    held[1:] = position[:-1]              # position.shift(1): signal at t trades t+1

    gross = held * ret

    # Turnover on the bar the change happens. Bar 0 carries the cost of *entering*
    # the opening position — dropping it would hand every rule one free side, which
    # matters most on the low-turnover rules that survive the cost gate longest.
    delta = np.empty_like(position)
    delta[0] = position[0]
    delta[1:] = np.diff(position)
    turnover = np.abs(delta)
    sells = np.maximum(-delta, 0.0)          # position decreasing = a sale

    per_side = (fee["commission_bps"] + fee["half_spread_bps"]) / 10_000.0
    cost = turnover * per_side + sells * (fee["sell_fee_bps"] / 10_000.0)

    borrow = fee.get("borrow_annual", 0.0)
    if borrow and bpy and bpy > 0:
        # Accrued on the position actually held during the bar, only while short.
        cost = cost + np.maximum(-held, 0.0) * (borrow / bpy)

    net = gross - cost
    return np.clip(net, RETURN_FLOOR, None)


def equity_curve(net: np.ndarray) -> np.ndarray:
    """Compounded equity from bar-level net returns, starting at 1.0."""
    return np.cumprod(1.0 + net)


def stats(position: np.ndarray, close: np.ndarray, index, fee,
          capital: float) -> dict | None:
    """Scalar stats for one rule on one asset under one fee scenario."""
    net = net_returns(position, close, fee, bars_per_year(index))
    if net.size == 0 or not np.isfinite(net).any():
        return None
    net = net[np.isfinite(net)]
    if net.size < 2:
        return None

    eq = equity_curve(net)
    bpy = bars_per_year(index)
    n_years = net.size / bpy if bpy and bpy > 0 else np.nan
    total_return = float(eq[-1] - 1.0)

    std = float(np.std(net, ddof=1))
    sharpe = float(np.mean(net) / std * np.sqrt(bpy)) if std > 0 and bpy > 0 else np.nan
    cagr = float(eq[-1] ** (1.0 / n_years) - 1.0) if n_years and n_years > 0 else np.nan
    drawdown = float(np.min(eq / np.maximum.accumulate(eq) - 1.0))

    pos = np.asarray(position, dtype="float64")
    turnover = float(np.abs(np.diff(pos)).sum() + abs(pos[0]))
    n_trades = int((np.diff(pos) != 0).sum() + (pos[0] != 0))

    return {
        "total_return": total_return,
        "pnl_dollars": capital * total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": drawdown,
        "exposure": float(np.mean(pos != 0)),
        "n_trades": n_trades,
        "turnover_per_year": turnover / n_years if n_years and n_years > 0 else np.nan,
        "n_bars": int(net.size),
        "years": float(n_years),
        "bars_per_year": float(bpy),
        "final_equity": float(eq[-1]),
    }


def final_equity(position: np.ndarray, close: np.ndarray, fee,
                 capital: float, bpy: float = None) -> float:
    """Just the number the parity harness compares. Kept separate and allocation-light."""
    net = net_returns(position, close, fee, bpy)
    net = net[np.isfinite(net)]
    if net.size == 0:
        return float(capital)
    return float(capital * np.prod(1.0 + net))


def flatten_eod(position: np.ndarray, index) -> np.ndarray:
    """Force flat on the last bar of each session.

    Applied to intraday *rules* only, never to the buy-and-hold baseline: flattening the
    benchmark turns it into a different strategy, which is precisely what made the old
    5-minute "beat" an artifact. Not applied to crypto at all — a 24/7 market has no
    session to flatten into, and forcing a daily flat would invent an exposure gap.
    """
    out = np.asarray(position, dtype="float64").copy()
    days = np.asarray(index.normalize())
    last_of_day = np.empty(len(days), dtype=bool)
    last_of_day[:-1] = days[1:] != days[:-1]
    last_of_day[-1] = True
    out[last_of_day] = 0.0
    return out
