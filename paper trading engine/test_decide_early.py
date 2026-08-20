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
from backtest_paper import build_bars                                   # noqa: E402
from book_strategy import BookStrategy, BookStrategyConfig              # noqa: E402
from strategies._indicators import _state_machine                       # noqa: E402

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


def reference_positions(df: pd.DataFrame, lead: int) -> pd.Series:
    """IBS on the session SO FAR, offline. The thing the live path must reproduce."""
    local = df.index.tz_convert(MARKET_TZ)
    keep = local <= _decide_index(df.index, lead)
    e = df[keep]
    day = e.index.tz_convert(MARKET_TZ).date
    g = e.groupby(day)
    s = pd.DataFrame({"High": g["High"].max(), "Low": g["Low"].min(),
                      "Close": g["Close"].last()}).dropna()
    rng = (s.High - s.Low).to_numpy()
    val = np.divide(s.Close - s.Low, rng, out=np.full(len(s), 0.5), where=rng > 0)
    return pd.Series(_state_machine(val < 0.2, val > 0.8), index=s.index)


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
    for symbol in r["names"]:
        ref = reference_positions(r["frames"][symbol], a.lead)
        for sym, day, got in r["decisions"]:
            if sym != symbol or day not in ref.index:
                continue
            checked += 1
            if abs(float(ref.loc[day]) - float(got)) > 1e-9:
                mismatch += 1
    print(f"  signal parity: {checked - mismatch}/{checked} decisions match the reference")
    if checked == 0:
        problems.append("no decision could be compared with the reference")
    if mismatch:
        problems.append(f"{mismatch} of {checked} decisions differ from the backtest")

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
    print("  PASS  the early-deciding book reproduces the backtest signal exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
