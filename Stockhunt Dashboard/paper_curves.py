"""P&L history for every system and asset on the paper desk, YTD and last three months.

**These are simulated, not paper-traded.** The desk started today; it has hours of live
P&L, so a year-to-date chart of it would be a single point. What can honestly fill those
windows is the same rule, from the same signal layer, run over the same instrument's recent
history — which is a backtest of a live system, and is labelled that way everywhere it
appears. It answers "how would this system have done this year", not "how has it done since
we started it".

Everything is computed the way the research computes it, so the numbers reconcile: position
from `talib_signals.generate_position` via `signals.position_for`, net returns from
`engines.vector` at the class's headline fee scenario. A curve here that disagreed with the
sweep would be a bug, not a second opinion.

Two windows, because they answer different questions. **YTD** shows the regime the system
is walking into; **3M** shows whether it has been working lately, which is the one people
actually look at before letting something trade.

Run::

    python paper_curves.py                 # every system on the desk, both windows
    python paper_curves.py --top 5         # matches run_paper.py --top 5
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

import dash_config

# `paper_config` supplies the universe, the warm-up constant and `top_rules`; the engine
# supplies the fee model and the vectorised backtest. Deliberately NOT `run_paper` or
# `backtest_paper` -- both import nautilus_trader at module scope, and this is a page
# builder that has no business dragging a live trading stack in behind it.
import sys                                                # noqa: E402
sys.path.insert(0, str(dash_config.PAPER))
import paper_config                                        # noqa: E402
from config import scenarios                              # noqa: E402
from engines import vector                                # noqa: E402
import signals                                             # noqa: E402
import td_loader                                           # noqa: E402


def load_bars(symbol: str, timeframe: str):
    """Cached bars for one cell, from the shared ../data/ tree."""
    for asset_class in ("us_stocks", "crypto", "us_etfs"):
        try:
            got = td_loader.load(asset_class, timeframe, [symbol])
        except (KeyError, FileNotFoundError):
            continue
        df = got.get(symbol)
        if df is not None and len(df):
            return df
    raise FileNotFoundError(f"no cached {timeframe} bars for {symbol}")

HEADLINE = {"us_stocks": "retail", "crypto": "binance"}
POINTS = 180                 # what a panel-width chart resolves
WARMUP = paper_config.DEFAULT_WINDOW_BARS


def windows(index: pd.DatetimeIndex) -> dict:
    """(label -> first timestamp) for the two ranges, from the data's own end date rather
    than the wall clock — a sheet that stops on Friday should not show an empty weekend."""
    end = index[-1]
    return {"ytd": pd.Timestamp(year=end.year, month=1, day=1, tz="UTC"),
            "3m": end - pd.Timedelta(days=91)}


def curve(net: np.ndarray, index: pd.DatetimeIndex, start) -> tuple[list, list, float]:
    """Growth of 100 across one window, downsampled by stride.

    Rebased at the window's first bar, so YTD starts at 100 on 1 January and the 3-month
    chart starts at 100 three months ago — each answers its own question rather than being
    two crops of one line.
    """
    mask = index >= start
    r = np.nan_to_num(net[mask], nan=0.0)
    idx = index[mask]
    if r.size < 2:
        return [], [], float("nan")
    eq = 100.0 * np.cumprod(1.0 + r)
    step = max(1, eq.size // POINTS)
    pick = list(range(0, eq.size, step))
    if pick[-1] != eq.size - 1:
        pick.append(eq.size - 1)
    return ([round(float(eq[i]), 3) for i in pick],
            [idx[i].strftime("%Y-%m-%d") for i in pick],
            round(float(eq[-1] - 100.0), 3))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--timeframes", nargs="+", default=paper_config.FORWARD_TIMEFRAMES)
    args = ap.parse_args()

    universe = {"us_stocks": paper_config.EQUITY_SYMBOLS,
                "crypto": paper_config.CRYPTO_SYMBOLS}
    out = {}
    for tf in args.timeframes:
        for cls, symbols in universe.items():
            fee = next(f for f in scenarios(cls) if f["key"] == HEADLINE[cls])
            for rule in paper_config.top_rules(cls, args.top, tf):
                key = f"{'crypto' if cls == 'crypto' else 'equity'}|{tf}|{rule}"
                assets, frames, bframes = {}, {}, {}
                for symbol in symbols:
                    try:
                        df = load_bars(symbol, tf)
                    except FileNotFoundError:
                        continue
                    if len(df) < WARMUP // 2:
                        continue
                    pos = signals.position_for(rule, df, cls, tf)
                    if pos is None:
                        continue
                    close = df["Close"].to_numpy(dtype="float64")
                    bpy = vector.bars_per_year(df.index)
                    net = vector.net_returns(pos, close, fee, bpy)
                    bench = vector.net_returns(np.ones(len(df)), close,
                                               {"key": "gross", "commission_bps": 0.0,
                                                "half_spread_bps": 0.0,
                                                "sell_fee_bps": 0.0, "borrow_annual": 0.0},
                                               bpy)
                    w = windows(df.index)
                    entry = {}
                    for label, start in w.items():
                        eq, dates, pnl = curve(net, df.index, start)
                        beq, _, bpnl = curve(bench, df.index, start)
                        if eq:
                            entry[label] = {"curve": eq, "bench": beq, "dates": dates,
                                            "pnl_pct": pnl, "bench_pnl_pct": bpnl}
                    if entry:
                        assets[symbol] = entry
                        frames[symbol] = pd.DataFrame({"net": net}, index=df.index)
                        bframes[symbol] = pd.DataFrame({"net": bench}, index=df.index)

                if not assets:
                    continue
                # System-level line: equal weight across the assets it trades, rebalanced
                # each bar — the same construction the research uses for a universe view.
                port = pd.concat(frames.values()).groupby(level=0).mean().sort_index()
                # The system's benchmark is the same basket held rather than traded, so the
                # two lines differ only by the rule's decisions — not by which assets or
                # which weights, which would otherwise be doing the comparing for it.
                bport = pd.concat(bframes.values()).groupby(level=0).mean().sort_index()
                w = windows(port.index)
                sys_entry = {}
                for label, start in w.items():
                    eq, dates, pnl = curve(port["net"].to_numpy(), port.index, start)
                    beq, _, bpnl = curve(bport["net"].to_numpy(), bport.index, start)
                    if eq:
                        sys_entry[label] = {"curve": eq, "bench": beq, "dates": dates,
                                            "pnl_pct": pnl, "bench_pnl_pct": bpnl}
                out[key] = {"system": sys_entry, "assets": assets}
                print(f"  {key:<34} {len(assets):>2} assets  "
                      f"YTD {sys_entry.get('ytd', {}).get('pnl_pct')} vs "
                      f"{sys_entry.get('ytd', {}).get('bench_pnl_pct')}  "
                      f"3M {sys_entry.get('3m', {}).get('pnl_pct')} vs "
                      f"{sys_entry.get('3m', {}).get('bench_pnl_pct')}")

    p = dash_config.WEB / "paper_curves.json"
    p.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"\nwrote {p}  ({p.stat().st_size / 1e6:.1f} MB, {len(out)} systems)")


if __name__ == "__main__":
    main()
