"""P&L history for every system and asset on the paper desk, YTD and last three months.

**These are simulated, not paper-traded.** The desk started today; it has hours of live
P&L, so a year-to-date chart of it would be a single point. What can honestly fill those
windows is the same rule, from the same signal layer, run over the same instrument's recent
history — which is a backtest of a live system, and is labelled that way everywhere it
appears. It answers "how would this system have done this year", not "how has it done since
we started it".

Everything is computed the way the desk computes it, so the numbers reconcile: position
from `live_signal.position_for` — the dispatcher the live strategy itself calls — and net
returns from `engines.vector` at the class's headline fee scenario. A curve here that
disagreed with the sweep would be a bug, not a second opinion.

Two windows, because they answer different questions. **YTD** shows the regime the system
is walking into; **3M** shows whether it has been working lately, which is the one people
actually look at before letting something trade.

**The list of systems comes from the desk, not from the sheet.** `run_paper.py` trades what
has been REGISTERED — books of combos and published strategies — and `paper_config.top_rules`
returns none of those, so selecting here off the sheet drew 24 charts for systems that were
not running while all 24 running ones had none. `desk_systems()` reads `paper_state.json`
instead, which is the same document the board renders.

Run::

    python paper_curves.py                 # every system the desk is running, both windows
    python paper_curves.py --top 3         # ...plus run_paper.py --top 3's automatic legs
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
#
# `live_signal` is the desk's OWN dispatcher and is nautilus-free, which is why it can be
# imported here. It is also the only one that can build what the desk actually trades:
# `signals.position_for` answers None for a combo label (`MININDEX~SAREXT|and`) and RAISES
# for a published strategy (`ibs`), and those two families are most of the current desk.
import sys                                                # noqa: E402
sys.path.insert(0, str(dash_config.PAPER))
import paper_config                                        # noqa: E402
import live_signal                                         # noqa: E402
from config import scenarios                              # noqa: E402
from engines import vector                                # noqa: E402
import td_loader                                           # noqa: E402


_BARS: dict[tuple, object] = {}


def load_bars(symbol: str, timeframe: str, asset_class: str | None = None):
    """Cached bars for one cell, from the shared ../data/ tree.

    Memoised, because the desk's systems overlap heavily: six us_stocks books at 1d hold
    the same hundred names, so without this the same CSV is parsed six hundred times for
    one sheet. The dictionary lives for one run of this script and nothing else.

    The desk's class is tried FIRST and the others remain as a fallback. Both halves matter.
    Trying them in a fixed order was fine for two classes and is not for four — `SPY` is
    cached under both `us_stocks` and `us_etfs`, and whichever was tried first decided which
    copy a curve was drawn from. But an exclusive lookup is wrong too: `SOXL` and `TQQQ` are
    traded on the equity leg (they are MK's transfer test, ranked off the us_stocks sheet)
    while their bars are cached under `us_etfs`, because that is the class the fetcher knows
    them by. Class-first, then fall back, resolves both without a special case.
    """
    hit = _BARS.get((symbol, timeframe, asset_class))
    if hit is not None:
        if isinstance(hit, FileNotFoundError):
            raise hit
        return hit

    rest = [c for c in ("us_stocks", "crypto", "us_etfs", "commodities")
            if c != asset_class]
    for cls in ([asset_class] + rest if asset_class else rest):
        try:
            got = td_loader.load(cls, timeframe, [symbol])
        except (KeyError, FileNotFoundError):
            continue
        df = got.get(symbol)
        if df is not None and len(df):
            _BARS[(symbol, timeframe, asset_class)] = df
            return df
    miss = FileNotFoundError(f"no cached {timeframe} bars for {symbol}")
    _BARS[(symbol, timeframe, asset_class)] = miss
    raise miss

# Taken from the engine rather than restated, so a curve here is quoted at the same cost
# assumption as the leaderboard the rule was selected off.
HEADLINE = paper_config.HEADLINE
POINTS = 180                 # what a panel-width chart resolves
WARMUP = paper_config.DEFAULT_WINDOW_BARS


def windows(index: pd.DatetimeIndex) -> dict:
    """(label -> first timestamp) for the two ranges, from the data's own end date rather
    than the wall clock — a sheet that stops on Friday should not show an empty weekend.

    `tz=index.tz` rather than a literal `tz="UTC"`. `td_loader.load` returns a tz-NAIVE
    index, and comparing one against a tz-aware Timestamp is a `TypeError` in pandas, not a
    coercion — so every curve raised the moment the year boundary was applied. The `3m`
    bound never showed it because it is derived from `index[-1]` and inherits whatever the
    index has. Taking the tz from the index does the same for both, and keeps working if
    the loader ever starts localising.
    """
    end = index[-1]
    return {"ytd": pd.Timestamp(year=end.year, month=1, day=1, tz=index.tz),
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


def desk_systems() -> dict:
    """What the desk is RUNNING, read from the state it publishes.

    This used to be `paper_config.top_rules` — the top three rules per class off the
    walk-forward sheet — and that selection stopped being what the desk runs when the desk
    moved to registered books. `run_paper.py` defaults to `--top 0`: it trades what has
    been registered, which on the current desk is books of combos (`MININDEX~SAREXT|and`)
    and published strategies (`ibs`, `volmanaged`). Not one of those is what `top_rules`
    returns, so every chart on the paper page was drawn for a system that is not running
    and every running system had none — 24 for 24, silently, for four days.

    So the list comes from the desk itself. `paper_state.json` is the same document the
    board reads, so a system on screen and a curve for it cannot disagree about what is
    deployed. A book publishes its `holdings`, which is where the names come from; a
    single-symbol system carries its own. Anything with neither falls back to the class's
    configured universe rather than being dropped.
    """
    p = dash_config.PAPER_RESULTS / "paper_state.json"
    if not p.exists():
        print(f"no desk state at {p} — nothing is running, so nothing to draw")
        return {}
    state = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for s in state.get("strategies", []):
        cls, tf, rule = s.get("cls"), s.get("tf"), s.get("rule")
        if not (cls and tf and rule):
            continue
        names = [h["symbol"] for h in (s.get("holdings") or []) if h.get("symbol")]
        if not names and s.get("kind") != "book" and s.get("symbol"):
            names = [s["symbol"]]
        entry = out.setdefault(f"{cls}|{tf}|{rule}",
                               {"cls": cls, "tf": tf, "rule": rule, "symbols": [],
                                "book": False})
        entry["book"] = entry["book"] or s.get("kind") == "book"
        for n in names:
            if n not in entry["symbols"]:
                entry["symbols"].append(n)
    for entry in out.values():
        if not entry["symbols"]:
            entry["symbols"] = list(paper_config.UNIVERSE.get(entry["cls"], []))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=0,
                    help="ALSO draw the top N rules per class off the walk-forward sheet "
                         "— the old automatic legs. 0 draws only what the desk runs.")
    ap.add_argument("--timeframes", nargs="+", default=paper_config.FORWARD_TIMEFRAMES)
    args = ap.parse_args()

    plan = desk_systems()
    # `--top` is additive now, and it is the only thing that still consults the sheet. The
    # legs it adds are single rules over the class's configured universe, exactly as before.
    for tf in args.timeframes:
        for cls, symbols in paper_config.UNIVERSE.items():
            for rule in (paper_config.top_rules(cls, args.top, tf) if args.top else []):
                plan.setdefault(f"{cls}|{tf}|{rule}",
                                {"cls": cls, "tf": tf, "rule": rule,
                                 "symbols": list(symbols), "book": False})
    if not plan:
        print("nothing to draw")

    out = {}
    for key, sysdef in plan.items():
        cls, tf, rule = sysdef["cls"], sysdef["tf"], sysdef["rule"]
        symbols = sysdef["symbols"]
        fee = next(f for f in scenarios(cls) if f["key"] == HEADLINE[cls])
        # Must match `systemKey` in app.js — `cls|tf|rule`, where `cls` is the
        # `asset_class` the strategy publishes. Both sides now use the research
        # class name, so the key is the same string on both ends instead of a
        # two-valued equity/crypto mapping that had to be kept in step by hand.
        assets, frames, bframes = {}, {}, {}
        for symbol in symbols:
            try:
                df = load_bars(symbol, tf, cls)
            except FileNotFoundError:
                continue
            if len(df) < WARMUP // 2:
                continue
            # The desk's dispatcher, not the research one. It is what `TalibRuleStrategy`
            # calls, so a curve here is of the same series the live system trades — and it
            # is the only one that resolves a combo label or a published strategy.
            pos = live_signal.position_for(rule, df, symbol)
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
        # A BOOK's per-asset curves are computed (the system line is their equal-weight
        # mean) and then thrown away, because nothing renders them: the page looks up
        # `assets[row.symbol]` and a book's symbol is the phrase "100 names", not a ticker.
        # Writing them anyway took this file from 2.6 MB to 6.3 MB of payload that every
        # reader downloads and no reader sees.
        out[key] = {"system": sys_entry,
                    "assets": {} if sysdef.get("book") else assets}
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
