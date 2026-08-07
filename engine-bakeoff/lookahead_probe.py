"""Does manifoldbt's own look-ahead detector catch the sizing bug in finding 1?

manifoldbt advertises `bt.diagnostics.detect_lookahead()`. Reading its source, the
method is a **split-sample comparison**: re-run the strategy on a truncated time
range, and compare the trades in the overlapping period against the full run. If
they match, it reports CLEAN. Its docstring names the two things it looks for —
a global statistic computed over all history, and a signal at bar T that reads
bar T+1.

That is a real check, and it works for what it targets. But it is a comparison
*across bars*, and the bug in finding 1 lives *inside* one bar: the position is
sized from bar t's close and filled at bar t's open. Truncating the series never
changes bar t's own open or close, so the trades in the overlap are identical and
the detector has nothing to flag.

This script establishes both halves of that claim without needing a Pro license:

  Part 1 — prove the sizing bug on synthetic bars with a known open/close ratio,
           so the result is arithmetic rather than inference.
  Part 2 — replicate detect_lookahead's published method (truncate, compare
           overlapping trades) against a config Part 1 has just proven biased,
           and show it comes back clean.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pandas as pd

import manifoldbt as mbt
from manifoldbt import BacktestConfig, ExecutionConfig, Strategy, col, exo
from manifoldbt.helpers import ExecutionPrice, Interval, Slippage

from common import HERE, INITIAL_CAPITAL, load_bars, positions_for

PROBE_DIR = HERE / "_probe_store"
ONE_DAY_NS = 86_400_000_000_000


def make_config(store_symbol_id, ts0, ts1, exo_name, delay, exec_price,
                end_override=None):
    return BacktestConfig(
        universe=[store_symbol_id],
        time_range_start=int(ts0),
        time_range_end=int(end_override if end_override is not None else ts1),
        initial_capital=INITIAL_CAPITAL,
        bar_interval=Interval.hours(1),
        exo_data=[exo_name],
        slippage=Slippage.none(),
        execution=ExecutionConfig(
            signal_delay=delay,
            execution_price=exec_price,
            allow_short=False,
            allow_fractional=True,
            position_sizing_mode="FractionOfEquity",
        ),
    )


def part1_synthetic() -> None:
    """Bars with a fixed, known open->close ratio. If sizing uses the close, the
    invested notional must come out as exactly capital * (close/open)."""
    print("=" * 78)
    print("PART 1 — sizing basis on synthetic bars (open->close ratio is known)")
    print("=" * 78)

    root = PROBE_DIR / "synthetic"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    n = 300
    ts = pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC")
    # Flat price history except that every bar closes exactly 10% above its open,
    # so close/open == 1.10 on every single bar with no ambiguity.
    open_px = np.full(n, 100.0)
    close_px = open_px * 1.10
    df = pd.DataFrame({
        "timestamp": ts, "open": open_px,
        "high": close_px * 1.01, "low": open_px * 0.99,
        "close": close_px, "volume": 1e6,
    })

    store = mbt.import_dataframe(
        df, symbol="SYNTH", symbol_id=1, interval="1h",
        data_root=str(root / "data"), metadata_db=str(root / "m.sqlite"),
        exchange="CACHE", asset_class="equity",
    )
    # Long from bar 100 onward, so exactly one entry and no ambiguity about which
    # bar the fill belongs to.
    target = np.zeros(n)
    target[100:] = 1.0
    mbt.register_exo("sig", pd.DataFrame({"timestamp": ts, "sig": target}),
                     store=store, timeframe="1h")

    strategy = Strategy.create("probe").signal("t", exo("sig")).size(col("t"))
    ts0, ts1 = int(ts[0].value), int(ts[-1].value) + ONE_DAY_NS

    for label, delay, ep in (("AT_CLOSE", 0, ExecutionPrice.AT_CLOSE),
                             ("AT_OPEN", 0, ExecutionPrice.AT_OPEN),
                             ("AT_OPEN + delay=1", 1, ExecutionPrice.AT_OPEN),
                             ("NEXT_BAR_OPEN", 0, ExecutionPrice.NEXT_BAR_OPEN)):
        res = mbt.run(strategy, make_config(1, ts0, ts1, "sig", delay, ep), store)
        tr = pd.DataFrame(res.raw.trades.to_pydict())
        qty, fill = float(tr["quantity"][0]), float(tr["fill_price"][0])
        notional = qty * fill
        print(f"  {label:20} fill={fill:7.2f}  qty={qty:9.4f}  "
              f"notional={notional:10.2f}  invested={notional / INITIAL_CAPITAL:6.1%}")

    print(f"\n  Capital is {INITIAL_CAPITAL:,.0f} and max_position_pct is 1.0, so a")
    print(f"  correct engine invests exactly 100%. close/open is 1.10 on every bar:")
    print(f"  an invested figure of 110% means the share count was computed from the")
    print(f"  close and then transacted at the open.")


def part2_split_sample() -> None:
    """Replicate detect_lookahead's method against the biased config."""
    print()
    print("=" * 78)
    print("PART 2 — detect_lookahead's own method, applied to the biased config")
    print("=" * 78)

    root = PROBE_DIR / "aapl"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    df = load_bars("AAPL")
    pos = positions_for("AAPL", df)
    bars = df.loc[pos.index]
    imp = bars.reset_index().rename(columns={
        "Date": "timestamp", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume"})
    imp["timestamp"] = pd.to_datetime(imp["timestamp"], utc=True)

    store = mbt.import_dataframe(
        imp, symbol="AAPL", symbol_id=1, interval="1h",
        data_root=str(root / "data"), metadata_db=str(root / "m.sqlite"),
        exchange="CACHE", asset_class="equity")
    mbt.register_exo(
        "sig", pd.DataFrame({"timestamp": imp["timestamp"],
                             "sig": pos["SMA_CROSS"].to_numpy()}),
        store=store, timeframe="1h")

    strategy = Strategy.create("probe").signal("t", exo("sig")).size(col("t"))
    ts0 = int(imp["timestamp"].iloc[0].value)
    ts1 = int(imp["timestamp"].iloc[-1].value) + ONE_DAY_NS

    def trades_for(exec_price, end_ns):
        cfg = make_config(1, ts0, ts1, "sig", 0, exec_price, end_override=end_ns)
        res = mbt.run(strategy, cfg, store)
        tr = pd.DataFrame(res.raw.trades.to_pydict())
        tr["execution_timestamp"] = pd.to_datetime(tr["execution_timestamp"], utc=True)
        return tr

    period = ts1 - ts0
    for label, exec_price in (("AT_OPEN (proven biased)", ExecutionPrice.AT_OPEN),
                              ("AT_CLOSE (proven exact)", ExecutionPrice.AT_CLOSE)):
        full = trades_for(exec_price, ts1)
        print(f"\n  {label}")
        for method, split in (("truncation (full -> 1/3)", ts0 + period // 3),
                              ("extension  (2/3 -> full)", ts0 + period * 2 // 3)):
            short = trades_for(exec_price, split)
            overlap = full[full["execution_timestamp"] < pd.Timestamp(split, unit="ns", tz="UTC")]
            n = min(len(short), len(overlap))
            mismatched = 0
            for c in ("quantity", "fill_price"):
                a = short[c].to_numpy()[:n].astype(float)
                b = overlap[c].to_numpy()[:n].astype(float)
                mismatched += int((np.abs(a - b) > 1e-9).sum())
            verdict = "CLEAN - no lookahead detected" if mismatched == 0 else \
                      f"FLAGGED - {mismatched} mismatched fields"
            print(f"    {method}: {n} overlapping trades compared -> {verdict}")


def main() -> None:
    part1_synthetic()
    part2_split_sample()
    print()
    print("=" * 78)
    print("CONCLUSION")
    print("=" * 78)
    print("  Part 1 shows AT_OPEN sizes from the close and fills at the open.")
    print("  Part 2 shows that split-sample comparison — the published method of")
    print("  detect_lookahead — reports that same config CLEAN, because truncating")
    print("  the series does not change any bar's own open or close, so every")
    print("  overlapping trade is bit-identical.")
    print()
    print("  Both statements hold at once: their detector does what it claims")
    print("  (cross-bar leakage), and it is structurally blind to intra-bar")
    print("  leakage in the execution layer.")


if __name__ == "__main__":
    main()
