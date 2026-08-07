"""Why are the intraday sheets so much worse than daily?

Tests one hypothesis with a decisive control: **the end-of-day flattening handicap**.

`config.CLASSES["us_stocks"]["flatten_eod"]` is True, and `signals.position_for` applies
it at every intraday timeframe (4h down to 1m). The buy-and-hold benchmark is never
flattened — flattening it would make it a different strategy. So every intraday equity
rule is a day-trading strategy being scored against a hold-overnight benchmark.

US equities earn much of their drift overnight. If that is where the return lives, then
an intraday rule is denied it *structurally*, before signal quality is even in question.

The control that settles it: an **always-long rule that is EOD-flattened**. It has no
signal at all — it is pure day-session beta. Its IR against buy-and-hold is the floor
every EOD-flattened rule starts from. If that floor is around the -0.75 the leaderboards
show, then the intraday "result" is measuring the flattening convention, not the rules.

Run::

    python diagnose_intraday.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import CAPITAL_PER_TICKER, CLASSES, MIN_BARS, TIMEFRAMES, headline_scenario
from engines import vector
import metrics
import td_loader

TF_ORDER = ["1d", "4h", "2h", "1h", "15m", "5m", "1m"]


def overnight_split(asset_class: str, timeframe: str) -> dict:
    """Decompose total log return into overnight-gap and intraday-session components."""
    data = td_loader.load(asset_class, timeframe)
    data = {s: d for s, d in data.items() if len(d) >= MIN_BARS}
    rows = []
    for sym, df in data.items():
        close = df["Close"].to_numpy(dtype="float64")
        lr = np.diff(np.log(close))
        days = df.index.normalize().to_numpy()
        # A bar is an overnight gap if the previous bar belongs to an earlier session.
        gap = days[1:] != days[:-1]
        rows.append({
            "symbol": sym,
            "total_log": float(lr.sum()),
            "overnight_log": float(lr[gap].sum()),
            "intraday_log": float(lr[~gap].sum()),
            "n_gaps": int(gap.sum()),
        })
    d = pd.DataFrame(rows)
    tot = d["total_log"].sum()
    return {
        "timeframe": timeframe,
        "assets": len(d),
        "overnight_share": float(d["overnight_log"].sum() / tot) if tot else np.nan,
        "intraday_share": float(d["intraday_log"].sum() / tot) if tot else np.nan,
    }


def flatten_floor(asset_class: str, timeframe: str) -> dict:
    """IR of a no-signal, always-long rule with and without EOD flattening.

    `always_long` reproduces buy-and-hold exactly, so its IR is 0 by construction and
    serves as a sanity check on the harness. `flattened` is the same rule forced flat at
    each session close — the structural floor.
    """
    spec = CLASSES[asset_class]
    data = td_loader.load(asset_class, timeframe)
    data = {s: d for s, d in data.items() if len(d) >= MIN_BARS}
    if not data:
        return {}

    out = {"timeframe": timeframe, "assets": len(data)}
    FREE = {"commission_bps": 0.0, "half_spread_bps": 0.0, "sell_fee_bps": 0.0,
            "borrow_annual": 0.0}
    for fee in (FREE, headline_scenario(asset_class)):
        tag = fee.get("key", "gross")
        irs_flat, irs_long, expo = [], [], []
        for sym, df in data.items():
            close = df["Close"].to_numpy(dtype="float64")
            bpy = vector.bars_per_year(df.index)
            ones = np.ones(len(df), dtype="float64")
            bench = vector.net_returns(ones, close, FREE, bpy)

            long_net = vector.net_returns(ones, close, fee, bpy)
            irs_long.append(metrics.information_ratio(long_net, bench, bpy))

            if TIMEFRAMES[timeframe]["intraday"]:
                flat = vector.flatten_eod(ones, df.index)
            else:
                flat = ones
            flat_net = vector.net_returns(flat, close, fee, bpy)
            irs_flat.append(metrics.information_ratio(flat_net, bench, bpy))
            expo.append(float(np.mean(flat != 0)))

        out[f"always_long_ir[{tag}]"] = float(np.nanmean(irs_long))
        out[f"eod_flat_ir[{tag}]"] = float(np.nanmean(irs_flat))
        out["flat_exposure"] = float(np.mean(expo))
    return out


def main() -> None:
    pd.set_option("display.width", 200)

    print("=== 1. Where does the equity drift live? ===")
    print("(share of total log return earned in overnight gaps vs during the session)")
    rows = [overnight_split("us_stocks", tf) for tf in TF_ORDER if tf != "1d"]
    print(pd.DataFrame([r for r in rows if r])
          .to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    print("\n=== 2. The structural floor: a no-signal always-long rule ===")
    print("(always_long must be ~0 by construction - it IS buy-and-hold.")
    print(" eod_flat is the same rule forced flat overnight: no signal, only the handicap)")
    rows = [flatten_floor("us_stocks", tf) for tf in TF_ORDER]
    df = pd.DataFrame([r for r in rows if r])
    print(df.to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    print("\n=== 3. Same, for crypto (never flattened - 24/7 market) ===")
    rows = [flatten_floor("crypto", tf) for tf in TF_ORDER]
    dfc = pd.DataFrame([r for r in rows if r])
    print(dfc.to_string(index=False, float_format=lambda v: f"{v:.3f}"))


if __name__ == "__main__":
    main()
