r"""Reconstruct a book's record over a past window, through the code that trades it live.

One leg per process, written into whatever `STOCKHUNT_PAPER_DB` names. Run it against a
COPY of the record first; there is no undo, and a reconstruction that turns out wrong has
already been mixed into the thing it would have been checked against.

    STOCKHUNT_PAPER_DB=/tmp/probe.db python backfill_books.py --leg str_00_pf-... \
        --from 2026-08-01
    python backfill_books.py --list                      # the legs, and what they need

**It runs `BookStrategy` itself.** Not a re-implementation of what a book does, and not the
research book out of `portfolio_wf` — the same class the live desk attaches, inside a
`BacktestEngine`, with the clock driven by cached bars instead of the wall. Every fill goes
through the same `_rebalance`, the same slice arithmetic, the same deadband, and lands in
`store` through the same `paper_state` calls. That is the only way the reconstructed rows
and the live rows are the same measurement rather than two things that resemble each other.

**A backfilled row is indistinguishable from a live one, by request.** Nothing marks it in
the database or on the board. Note for whoever maintains this: the record before the day a
book was actually promoted is reconstructed here, and the rules it holds were selected from
sheets covering that same period — so the backfilled stretch is in-sample for the selection
in a way the live stretch is not. Read the two halves differently even though the page does
not.

**One engine per process, and that is not tidiness.** Nautilus initialises its Rust logger
once per process and a second `BacktestEngine` panics with *attempted to set a logger after
the logging system was already initialized*. `run_backfill.sh` is the loop.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import paper_config                  # noqa: F401  (wires sys.path)
import store
import td_nautilus
from book_strategy import BookStrategy, BookStrategyConfig
from stockhunt import deskdb

import td_loader

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money, Price, Quantity

# How much history to feed BEFORE the window, so the rule is warm when the window opens.
# A book refuses to trade under `min_warmup_bars` (250), and a book that spends the first
# half of the reconstructed period warming is a flat line with an explanation nobody can
# see. Anything this produces before `--from` is deleted afterwards.
WARMUP_BARS = 400


def legs(account: str = "00") -> list[dict]:
    """Every live portfolio leg, as the ledger holds it."""
    rows = [r for r in deskdb.registrations(account)
            if r.get("portfolio_id") and r["want"] != "retired" and r.get("rule")]
    return sorted(rows, key=lambda r: r["strategy_id"])


def bars_for(cls: str, tf: str, symbols: list[str]) -> dict:
    frames = td_loader.load(cls, tf, symbols)
    out = {}
    for sym, df in frames.items():
        if df is None or df.empty:
            continue
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True)
        out[sym] = df.sort_index()
    return out


def to_bars(df: pd.DataFrame, bar_type: BarType, price_prec: int,
            size_prec: int) -> list[Bar]:
    idx = pd.to_datetime(df.index, utc=True).as_unit("ns")
    ts = idx.astype("int64").to_numpy()
    vol = df["Volume"].to_numpy() if "Volume" in df else np.zeros(len(df))
    vol = np.where(np.isfinite(vol), vol, 0.0)
    return [
        Bar(bar_type=bar_type,
            open=Price(o, price_prec), high=Price(h, price_prec),
            low=Price(lo, price_prec), close=Price(c, price_prec),
            volume=Quantity(v, size_prec), ts_event=int(t), ts_init=int(t))
        for o, h, lo, c, v, t in zip(
            df["Open"].to_numpy(), df["High"].to_numpy(), df["Low"].to_numpy(),
            df["Close"].to_numpy(), vol, ts)
    ]


def trim_before(sid: str, cutoff: str) -> dict:
    """Drop everything the warm-up produced before the window opened.

    The strategy has to see hundreds of bars before `--from` to be warm at it, and it
    trades on them as it goes. Those are real rows about a period this run was not asked
    to reconstruct, so they go — otherwise the record silently begins wherever the warm-up
    happened to start.
    """
    conn = store.connect()
    with store._lock if hasattr(store, "_lock") else _null():
        c1 = conn.execute("DELETE FROM curve WHERE sid = ? AND ts < ?", (sid, cutoff))
        c2 = conn.execute("DELETE FROM fills WHERE sid = ? AND ts < ?", (sid, cutoff))
        conn.commit()
    return {"curve": c1.rowcount, "fills": c2.rowcount}


class _null:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def run_leg(reg: dict, start: str, end: str | None, log_level: str) -> dict:
    cls, tf, rule = reg["cls"], reg["tf"], reg["rule"]
    venue = paper_config.VENUES[cls]
    universe = paper_config.book_universe(cls)

    frames = bars_for(cls, tf, universe)
    if not frames:
        return {"leg": reg["strategy_id"], "skipped": f"no cached {cls} {tf} bars"}

    lo = pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(end, tz="UTC") if end else max(df.index[-1] for df in frames.values())

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("BFILL-001"),
        logging=LoggingConfig(bypass_logging=log_level == "OFF", log_level=log_level),
    ))
    # Multi-currency for the pair classes, exactly as `run_paper` funds them: a CurrencyPair
    # trade converts USD into the base asset, and an account that cannot hold it fills one
    # size increment and stops.
    engine.add_venue(
        venue=Venue(venue), oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN, base_currency=None if cls in
        paper_config.PAIR_CLASSES else USD,
        starting_balances=[Money(reg["capital"] * 2, USD)])

    held = []
    for sym in universe:
        df = frames.get(sym)
        if df is None:
            continue
        inst = td_nautilus.instrument_for(sym, cls, venue)
        engine.add_instrument(inst)
        bar_type = BarType.from_str(f"{inst.id}-{paper_config.BAR_SPEC[tf]}")
        window = df[df.index <= hi]
        # Enough history in front of the window for the rule to be warm at its first bar.
        pre = window[window.index < lo]
        keep = pd.concat([pre.tail(WARMUP_BARS), window[window.index >= lo]])
        if len(keep) < 30:
            continue
        engine.add_data(to_bars(keep, bar_type, inst.price_precision,
                                inst.size_precision))
        held.append(sym)

    if not held:
        engine.dispose()
        return {"leg": reg["strategy_id"], "skipped": "no symbol had bars in the window"}

    strat = BookStrategy(config=BookStrategyConfig(
        order_id_tag=reg["strategy_id"][-30:],
        rule=rule, account=reg["account"], name=reg["name"], cls=cls, tf=tf,
        symbols=tuple(held), venue=venue, capital=float(reg["capital"]),
        allow_short=bool(reg["allow_short"]), benchmark=reg["benchmark"],
        export_state=True,
        note=f"{rule} as one book of ${float(reg['capital']):,.0f}"))
    engine.add_strategy(strat)
    engine.run()
    engine.dispose()

    sid = store.sid_for(reg["account"], reg["name"])
    removed = trim_before(sid, lo.isoformat(timespec="seconds"))
    conn = store.connect()
    points = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM curve WHERE sid = ?",
                          (sid,)).fetchone()
    fills = conn.execute("SELECT COUNT(*) FROM fills WHERE sid = ?", (sid,)).fetchone()
    return {"leg": reg["strategy_id"], "rule": rule, "cls": cls, "tf": tf,
            "names": len(held), "curve_points": points[0], "first": points[1],
            "last": points[2], "fills": fills[0], "trimmed": removed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--leg", help="one strategy_id; omit with --list")
    ap.add_argument("--list", action="store_true", help="every leg, one per line")
    ap.add_argument("--from", dest="start", default="2026-08-01")
    ap.add_argument("--to", dest="end", default=None)
    ap.add_argument("--account", default="00")
    ap.add_argument("--log-level", default="OFF")
    args = ap.parse_args(argv)

    if args.list:
        for r in legs(args.account):
            print(r["strategy_id"])
        return 0

    if not args.leg:
        ap.error("--leg or --list")

    if not os.environ.get("STOCKHUNT_PAPER_DB"):
        print("refusing to write the LIVE record. Set STOCKHUNT_PAPER_DB to a copy "
              "first — a reconstruction has no undo, and one that turns out wrong has "
              "already been mixed into the thing it would have been checked against.")
        return 2

    reg = deskdb.registration(args.leg, account=args.account)
    if reg is None:
        print(f"no such registration: {args.leg}")
        return 2

    store.start_session()
    out = run_leg(reg, args.start, args.end, args.log_level)
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
