"""Gate: a book that decides BEFORE the bell trades what the backtest said it would.

    ..\\.venv\\Scripts\\python test_decide_early.py

`BookStrategyConfig.signal_tf` makes a book read 5-minute bars, fold each day into the
range **so far**, and act once — `decide_lead_min` minutes before the close. It exists
because a rule keyed on the current bar's own high, low and close cannot honestly be
filled at that close: the close is not known until it has printed. Deciding five minutes
early removes the look-ahead without paying for a whole session of delay.

That is only true if the live path computes the same thing the measurement did, so this
asserts it directly rather than by reading the code.

1. **The folded session frame never contains a price from after the cutoff.** The whole
   claim rests on this one, and it is checked against the raw bars rather than against
   the fold itself.
2. **The signal equals the reference.** IBS rebuilt from session-so-far ranges offline,
   compared decision for decision with what the running strategy did. An exact match,
   because a position is discrete and "nearly the same signal" is a different trade.
   There are TWO references and the difference between them is a real one: the sheet's
   prices are `adjust=all` floats and the desk's are quantised to the instrument's own
   precision, so a threshold rule can land on opposite sides of 0.2. Such a decision is
   named and counted rather than tolerated — see the note beside `flips` in `main`.
3. **One decision per name per session** — 78 five-minute bars a day must not become 78
   rebalances, and the record must stay one curve point a session.
4. **The book identity still holds**: cash plus every slice equals equity, to the cent.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

import paper_config                                                     # noqa: F401

_TMP = Path(os.environ.setdefault(
    "STOCKHUNT_EARLY_TMP", tempfile.mkdtemp(prefix="stockhunt-early-")))

import store                                                            # noqa: E402
store.DB_PATH = _TMP / "paper.db"
import paper_state                                                      # noqa: E402
paper_state.STATE_PATH = _TMP / "paper_state.json"
paper_state.MIRROR_PATH = None

import config as bt                                                     # noqa: E402
import td_nautilus                                                      # noqa: E402
import book_strategy                                                    # noqa: E402
from backtest_paper import build_bars                                   # noqa: E402
from book_strategy import BookStrategy, BookStrategyConfig              # noqa: E402
from strategies._indicators import _state_machine                       # noqa: E402

# THE CACHE THIS GATE REPLAYS IS THE CACHE A LIVE BOOK WARMS FROM, and offline they are one
# file. `book_strategy.on_start` seeds `_bars` from `cache_warmup.load`, whose newest bar is
# the newest bar in `data/stocks/5m` — which is also the LAST bar this gate feeds through
# the engine. `_append` refuses any bar at or before the buffer's last timestamp, so every
# replayed bar was rejected as already seen, the buffer never advanced, and all 200 sessions
# were decided on one folded frame. The gate went on printing 1,200 decisions and went on
# matching its own reference, because both sides had collapsed onto the final session:
# nothing failed loudly, the gate simply stopped testing anything.
#
# **Live this cannot happen and the strategy is not the thing to change.** A live book's
# cache is by construction older than its feed — that is what warming from it is for. A
# replay of the cache against itself is a property of running offline, so the seed is
# switched off HERE. `test_book.py` covers the warm-up path itself.
book_strategy.cache_warmup.load = lambda *a, **k: []

from nautilus_trader.backtest.engine import BacktestEngine              # noqa: E402
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig  # noqa: E402
from nautilus_trader.model.currencies import USD                        # noqa: E402
from nautilus_trader.model.data import BarType                          # noqa: E402
from nautilus_trader.model.enums import AccountType, OmsType            # noqa: E402
from nautilus_trader.model.identifiers import TraderId, Venue           # noqa: E402
from nautilus_trader.model.objects import Money                         # noqa: E402

VENUE = "SANDBOX"
CAPITAL = 100_000.0
MARKET_TZ = "America/New_York"
FIVE_M = pd.Timedelta(minutes=5)
BELL_HOUR = 16


def load_5m(symbol: str) -> pd.DataFrame:
    """Cached 5m bars, stamped at the bar CLOSE in UTC.

    The cache is indexed by the bar's OPEN in market local time — the vendor's convention
    — while `td_nautilus._to_bar` hands Nautilus the close. Getting this wrong shifts
    every session by one bar and the gate would be comparing the wrong days.
    """
    path = bt.DATA_DIR / "stocks" / "5m" / f"{symbol}.parquet"
    df = pd.read_parquet(path).dropna()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize(MARKET_TZ, ambiguous="NaT", nonexistent="NaT")
    df.index = (idx + FIVE_M).tz_convert("UTC")
    return df[df.index.notna()].sort_index()


def _decide_index(idx: pd.DatetimeIndex, lead: int) -> pd.DatetimeIndex:
    """Decision instant per bar, from the WALL CLOCK.

    Deliberately spelled out here rather than imported from `book_strategy`: a reference
    that calls the implementation cannot disagree with it, which is the one thing this
    gate exists to find out. Local midnight plus sixteen hours is 17:00 on the
    spring-forward day, so the arithmetic has to go through a naive clock and localise.
    """
    naive = idx.tz_convert(MARKET_TZ).tz_localize(None).normalize()
    naive = naive + pd.Timedelta(hours=BELL_HOUR) - pd.Timedelta(minutes=lead)
    return naive.tz_localize(MARKET_TZ)


def reference_positions(df: pd.DataFrame, lead: int,
                        price_precision: int | None = None) -> pd.Series:
    """IBS on the session SO FAR, offline. The thing the live path must reproduce.

    `price_precision` rounds the bars first, which is not a tolerance and not an
    approximation — it is the SECOND reference, computed on the prices the desk actually
    sees. See the note beside `flips` in `main` for why there have to be two.
    """
    local = df.index.tz_convert(MARKET_TZ)
    keep = local <= _decide_index(df.index, lead)
    e = df[keep]
    if price_precision is not None:
        e = e.round(price_precision)
    day = e.index.tz_convert(MARKET_TZ).date
    g = e.groupby(day)
    s = pd.DataFrame({"High": g["High"].max(), "Low": g["Low"].min(),
                      "Close": g["Close"].last()}).dropna()
    rng = (s.High - s.Low).to_numpy()
    val = np.divide(s.Close - s.Low, rng, out=np.full(len(s), 0.5), where=rng > 0)
    return pd.Series(_state_machine(val < 0.2, val > 0.8), index=s.index)


def session_ibs(df: pd.DataFrame, lead: int, day, price_precision: int | None = None):
    """The IBS one session was decided on, for reporting a disagreement in numbers."""
    local = df.index.tz_convert(MARKET_TZ)
    e = df[(local <= _decide_index(df.index, lead))
           & (df.index.tz_convert(MARKET_TZ).date == day)]
    if not len(e):
        return None
    if price_precision is not None:
        e = e.round(price_precision)
    hi, lo, close = e["High"].max(), e["Low"].min(), e["Close"].iloc[-1]
    return 0.5 if hi <= lo else float((close - lo) / (hi - lo))


def _full_sessions(df: pd.DataFrame, sessions: int) -> pd.DataFrame:
    """The last `sessions` days that actually have a whole session of bars."""
    day = df.index.tz_convert(MARKET_TZ).date
    counts = pd.Series(np.ones(len(df)), index=day).groupby(level=0).size()
    full = counts[counts >= 70].index
    df = df[np.isin(day, full)]
    day = df.index.tz_convert(MARKET_TZ).date
    keep = sorted(set(day))[-sessions:]
    return df[np.isin(day, keep)]


def _leaked(strat, symbol: str, lead: int) -> list:
    """Does the session just decided use only prices knowable by its cutoff?

    Only the NEWEST folded row is checked, which is the one the decision was actually
    made on. Every row is the newest exactly once over a run, so the whole history still
    gets tested — without rebuilding the fold from scratch on every decision.
    """
    folded = strat._sessions(symbol)
    if folded is None or not len(folded):
        return []
    row_ts = folded.index[-1]
    day = row_ts.date()
    vis = [b for b in strat._bars[symbol]
           if b["ts"].tz_convert(MARKET_TZ).date() == day and b["ts"] <= row_ts]
    if not vis:
        return [(symbol, str(row_ts), "empty")]
    out = []
    if folded["High"].iloc[-1] > max(b["High"] for b in vis) + 1e-9:
        out.append((symbol, str(row_ts), "high"))
    if folded["Low"].iloc[-1] < min(b["Low"] for b in vis) - 1e-9:
        out.append((symbol, str(row_ts), "low"))
    if abs(folded["Close"].iloc[-1] - vis[-1]["Close"]) > 1e-9:
        out.append((symbol, str(row_ts), "close"))
    return out


def run(names: list[str], sessions: int, lead: int, log_level: str) -> dict:
    frames = {}
    for s in names:
        path = bt.DATA_DIR / "stocks" / "5m" / f"{s}.parquet"
        if not path.exists():
            continue
        frames[s] = _full_sessions(load_5m(s), sessions)
    names = sorted(frames)
    if not names:
        raise SystemExit("no 5m bars cached; this gate needs data/stocks/5m/")

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("EARLY-01"),
        logging=LoggingConfig(bypass_logging=log_level == "OFF", log_level=log_level)))
    engine.add_venue(venue=Venue(VENUE), oms_type=OmsType.NETTING,
                     account_type=AccountType.CASH, base_currency=USD,
                     starting_balances=[Money(CAPITAL * 5, USD)])
    for symbol in names:
        inst = td_nautilus.equity_instrument(symbol, VENUE)
        engine.add_instrument(inst)
        bar_type = BarType.from_str(f"{inst.id}-{paper_config.BAR_SPEC['5m']}")
        engine.add_data(build_bars(frames[symbol], bar_type,
                                   price_precision=2, size_precision=0))

    strat = BookStrategy(config=BookStrategyConfig(
        order_id_tag="EARLY-1", rule="ibs", name="early-gate",
        cls="us_stocks", tf="1d", signal_tf="5m", decide_lead_min=lead,
        symbols=tuple(names), venue=VENUE, capital=CAPITAL,
        window_bars=paper_config.DECIDE_EARLY_WINDOW_BARS,
        min_warmup_sessions=paper_config.MIN_WARMUP_SESSIONS,
        export_state=True, benchmark=None, note="decide-early gate"))
    engine.add_strategy(strat)

    decisions: list[tuple] = []
    leaks: list[tuple] = []
    breaches: list[tuple] = []
    original = strat._signal

    def watched(symbol):
        out = original(symbol)
        leaks.extend(_leaked(strat, symbol, lead))
        if out is not None:
            day, _ = strat._session_of(strat._bars[symbol][-1]["ts"])
            decisions.append((symbol, day, out))
        equity = strat.equity()
        rebuilt = strat._cash + sum(u * strat._last_price.get(s, 0.0)
                                    for s, u in strat._units.items())
        if abs(equity - rebuilt) > 0.01:
            breaches.append((symbol, equity, rebuilt))
        return out

    strat._signal = watched
    engine.run()
    fills, equity = strat._n_fills, strat.equity()
    engine.dispose()
    return {"names": names, "frames": frames, "decisions": decisions,
            "leaks": leaks, "breaches": breaches, "fills": fills,
            "equity": equity}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--names", nargs="+",
                    default=["AAPL", "MSFT", "NVDA", "JPM", "KO", "XOM"])
    ap.add_argument("--sessions", type=int, default=200)
    ap.add_argument("--lead", type=int, default=paper_config.DEFAULT_DECIDE_LEAD_MIN)
    ap.add_argument("--log-level", default="OFF")
    a = ap.parse_args()

    r = run(a.names, a.sessions, a.lead, a.log_level)
    problems = []

    print(f"  names        : {', '.join(r['names'])}")
    print(f"  decisions    : {len(r['decisions'])}   fills: {r['fills']}")
    print(f"  equity       : ${r['equity']:,.2f}")

    print(f"  cutoff leaks : {len(r['leaks'])}")
    if r["leaks"]:
        for leak in r["leaks"][:5]:
            print(f"                 {leak}")
        problems.append(f"{len(r['leaks'])} folded sessions used a post-cutoff price")

    mismatch = checked = 0
    flips = []
    for symbol in r["names"]:
        frame = r["frames"][symbol]
        prec = td_nautilus.equity_instrument(symbol, VENUE).price_precision
        ref = reference_positions(frame, a.lead)
        ref_q = reference_positions(frame, a.lead, price_precision=prec)
        for sym, day, got in r["decisions"]:
            if sym != symbol or day not in ref.index:
                continue
            checked += 1
            if abs(float(ref.loc[day]) - float(got)) <= 1e-9:
                continue
            if (day in ref_q.index
                    and abs(float(ref_q.loc[day]) - float(got)) <= 1e-9):
                flips.append((symbol, day, prec,
                              session_ibs(frame, a.lead, day),
                              session_ibs(frame, a.lead, day, price_precision=prec),
                              float(ref.loc[day]), float(got)))
                continue
            mismatch += 1
    print(f"  signal parity: {checked - mismatch - len(flips)}/{checked} decisions match "
          f"the sheet's prices, {checked - mismatch}/{checked} match the desk's")
    if checked == 0:
        problems.append("no decision could be compared with the reference")
    if mismatch:
        problems.append(f"{mismatch} of {checked} decisions differ from the backtest")

    # A DECISION THE DESK AND THE SHEET DISAGREE ABOUT BECAUSE THEY SEE DIFFERENT PRICES.
    #
    # A Nautilus `Price` carries the instrument's own precision — two decimals on an equity
    # — so `td_nautilus._to_bar` quantises every bar to the cent before a rule ever sees it.
    # The research does not: `data/stocks/5m` holds `adjust=all` back-adjusted floats, and a
    # split or a dividend leaves a name quoting 88.019997 rather than 88.02. That is ~1bp,
    # invisible in any P&L, and IBS is a THRESHOLD rule — so on a session whose IBS lands
    # within a cent's worth of 0.2 or 0.8 the two round to opposite sides of it and the desk
    # takes a position the sheet does not.
    #
    # **Reported by name, not tolerated and not fixed here.** Widening the tolerance would
    # hide it; raising the instrument's precision would change every live fill price and
    # every number already in `results/paper.db`, which is a decision with a record behind
    # it and not a line in a gate. What this must never do is fail for a DIFFERENT reason
    # and be read as "oh, the rounding thing again" — hence the second reference, which
    # only excuses a decision the desk's own prices actually produce.
    if flips:
        print(f"  price rounding: {len(flips)} decision(s) flipped by the cent grid")
        for sym, day, prec, raw, quant, want, got in flips:
            print(f"                 {sym} {day}: IBS {raw:.6f} on the sheet's prices, "
                  f"{quant:.6f} at {prec}dp — sheet {want:.0f}, desk {got:.0f}")

    seen, dupes = set(), 0
    for sym, day, _ in r["decisions"]:
        if (sym, day) in seen:
            dupes += 1
        seen.add((sym, day))
    print(f"  one a session: {dupes} repeat decisions")
    if dupes:
        problems.append(f"{dupes} sessions were decided more than once")

    print(f"  identity     : {len(r['breaches'])} breaches")
    if r["breaches"]:
        problems.append(f"{len(r['breaches'])} bars where cash + slices != equity")

    print()
    if problems:
        for p in problems:
            print(f"  FAIL  {p}")
        return 1
    if flips:
        print(f"  PASS  the early-deciding book reproduces the backtest signal on every "
              f"decision but {len(flips)}, which the cent grid flipped (named above)")
    else:
        print("  PASS  the early-deciding book reproduces the backtest signal exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
