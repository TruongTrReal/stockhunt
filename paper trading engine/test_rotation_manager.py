"""Gate for `rotation_manager.py`. A `__main__` script that exits nonzero.

Not collected by pytest -- this folder also holds pytest suites and a nonzero exit is the
whole point. Run it directly:

    ..\\.venv\\Scripts\\python test_rotation_manager.py

Six properties. Each one guards a way the live book could quietly stop being the thing the
research scored, which is the failure this file exists for: every symptom of that failure
looks like ordinary underperformance.

1. **The schedule the desk trades is the schedule the backtest scored.** The NYSE calendar
   the manager asks must name the same last-trading-day as the real session index, or the
   live book rebalances on a different day from the one measured -- and one session of
   delay costs this strategy a third of its edge (t 1.58 -> 0.98, measured).
2. **The session fold agrees with the desk's own.** `book_strategy.py` has an equivalent
   private implementation that is deliberately not edited (live code, open positions), so
   the two are pinned together here instead -- including on the spring-forward Sunday,
   where the naive arithmetic is wrong.
3. **The signal is the shared one.** The manager and the backtest must call the same
   scorer, and a fold of full sessions must reproduce the daily answer exactly.
4. **The idempotency key is derived from the session, never from a clock.** This is the
   only defect here that can cost money silently: a retry after a timeout with an
   unstable key opens a second position and nothing surfaces it until somebody
   reconciles by hand.
5. **Sells are queued before the buy.** The desk drains in `seq` order, so a buy ahead of
   the sell that funds it is refused for want of cash and the month is spent in the wrong
   name.
6. **The window refuses everything outside it**, so a timer that fires every five minutes
   cannot trade at 10am, and a month whose end is a holiday is still caught.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pandas as pd

import rotation_manager as RM
from stockhunt import rotation as sh_rotation
from stockhunt.sessions import decision_instants, fold_sessions

FAILURES: list[str] = []
TZ, HH, MM = "America/New_York", 16, 0
LEAD = RM.DECIDE_LEAD_MIN


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def test_calendar_matches_real_sessions() -> None:
    print("the live calendar names the same month-ends as the real session index")
    try:
        import pandas_market_calendars as mcal
    except ImportError:
        check("pandas_market_calendars is installed", False,
              "the manager cannot find the last trading day without it and will "
              "skip every month rather than guess")
        return
    check("pandas_market_calendars is installed", True)
    spy = RM.paper_config.REPO / "data" / "etfs" / "1d" / "SPY.parquet" \
        if hasattr(RM.paper_config, "REPO") else None
    import pathlib
    here = pathlib.Path(__file__).resolve().parent.parent
    spy = here / "data" / "etfs" / "1d" / "SPY.parquet"
    if not spy.exists():
        check("SPY bars available to compare against", False, str(spy))
        return
    real = pd.Series(sorted(set(pd.read_parquet(spy).index.normalize())))
    cal = mcal.get_calendar("NYSE")
    days = pd.DatetimeIndex(cal.valid_days(str(real.iloc[0].date()),
                                           str(real.iloc[-1].date())))
    days = pd.Series(sorted(set(days.tz_localize(None).normalize())))
    lr = real.groupby([real.dt.year, real.dt.month]).max()
    lc = days.groupby([days.dt.year, days.dt.month]).max()
    j = pd.concat({"real": lr, "cal": lc}, axis=1).dropna()
    bad = j[j["real"] != j["cal"]]
    check(f"every last-trading-day agrees ({len(j)} months)", len(bad) == 0,
          "" if not len(bad) else f"{len(bad)} differ, first {bad.index[0]}")


def test_fold_matches_the_desk() -> None:
    print("the shared fold agrees with book_strategy's private one, DST included")
    try:
        from book_strategy import BookStrategy
    except Exception as exc:
        check("book_strategy importable", False, f"{type(exc).__name__}: {exc}")
        return
    # Two windows: an ordinary one, and the spring-forward Sunday, where "local midnight
    # plus sixteen hours" is 17:00 rather than 16:00.
    for label, start in (("ordinary week", "2026-02-02"), ("spring forward", "2026-03-06")):
        idx = pd.date_range(f"{start} 14:30", periods=8 * 78, freq="5min", tz="UTC")
        mine = decision_instants(idx, TZ, HH, MM, LEAD)
        # `_decision_instants` is a plain Python method that reads exactly one thing off
        # `self`. Calling it unbound against a stand-in is the only way to compare the two
        # implementations without standing up a Nautilus kernel -- `Actor.config` is a
        # read-only Cython attribute, so the strategy cannot be half-built.
        class _Cfg:
            decide_lead_min = LEAD

        class _Shim:
            config = _Cfg()

        theirs = BookStrategy._decision_instants(_Shim(), idx, TZ, HH, MM)
        check(f"{label}: decision instants identical",
              bool((mine == theirs).all()),
              "" if (mine == theirs).all() else f"{int((mine != theirs).sum())} differ")


def test_signal_is_the_shared_one() -> None:
    print("a fold of whole sessions reproduces the daily signal exactly")
    check("the manager calls stockhunt.rotation",
          RM.sh_rotation is sh_rotation)
    check("the manager uses the published lookback, unmodified",
          RM.LOOKBACK == sh_rotation.LOOKBACK == 63, f"{RM.LOOKBACK}")
    # Build 5-minute bars whose sessions have known closes, fold them, and require the
    # score to match the same closes scored daily.
    n = 80
    days = pd.bdate_range("2025-01-02", periods=n, tz=TZ)
    rows = []
    closes = {}
    for k, sym in enumerate(["A", "B"]):
        px = [100.0 * (1.0 + 0.01 * i * (1 if k == 0 else -1)) for i in range(n)]
        closes[sym] = px
    for i, d in enumerate(days):
        for sym in ("A", "B"):
            for j, minute in enumerate(("09:35", "12:00", "15:45")):
                ts = pd.Timestamp(f"{d.date()} {minute}", tz=TZ)
                p = closes[sym][i]
                rows.append({"sym": sym, "ts": ts, "Open": p, "High": p, "Low": p,
                             "Close": p, "Volume": 1})
    raw = pd.DataFrame(rows)
    folded = {}
    for sym in ("A", "B"):
        d = raw[raw["sym"] == sym].set_index("ts")[["Open", "High", "Low", "Close", "Volume"]]
        folded[sym] = fold_sessions(d, TZ, HH, MM, LEAD)["Close"]
    live = pd.DataFrame(folded)
    daily = pd.DataFrame({s: closes[s] for s in ("A", "B")})
    check("the fold produced one row per session", len(live) == n, f"{len(live)} vs {n}")
    a = sh_rotation.scores(live, 63)[-1]
    b = sh_rotation.scores(daily, 63)[-1]
    check("live score == daily score", bool((abs(a - b) < 1e-12).all()),
          f"{a} vs {b}")
    check("and it picks the riser", sh_rotation.pick(a, ["A", "B"]) == "A")


def test_idempotency_key() -> None:
    print("the order id is derived from the session, not from a clock")
    a = RM.order_id("QQQ", "20260831")
    check("same session -> same key", a == RM.order_id("QQQ", "20260831"), a)
    check("different session -> different key", a != RM.order_id("QQQ", "20260930"))
    check("different symbol -> different key", a != RM.order_id("IWM", "20260831"))
    check("no digits from the current time appear in it",
          pd.Timestamp.utcnow().strftime("%H%M") not in a, a)


def test_ledger_roundtrip() -> None:
    """Register and order against a THROWAWAY database, never the desk's own.

    This is the half the HTTP version could only fake. `deskdb` is the same module the
    API writes through, so exercising it here proves the whole path a firing takes --
    idempotent registration, ordered submission, retry collapsing -- rather than proving
    that a mock was called.
    """
    print("the ledger accepts the registration and collapses a retry")
    import tempfile
    from stockhunt import deskdb
    tmp = pathlib.Path(tempfile.mkdtemp()) / "desk.db"
    real = RM.DESK_DB
    RM.DESK_DB = tmp
    try:
        RM.open_ledger()
        a = RM.ensure_registered()
        b = RM.ensure_registered()
        check("registration is idempotent on (account, name)",
              a["strategy_id"] == b["strategy_id"] == RM.STRATEGY_ID, a["strategy_id"])
        check("it asks for exactly the basket", json.loads(a["symbols"]) == RM.BASKET
              if isinstance(a["symbols"], str) else list(a["symbols"]) == RM.BASKET,
              str(a["symbols"]))
        check("and starts pending, for the desk to pick up",
              a["state"] == "pending" and a["want"] == "live",
              f"{a['state']}/{a['want']}")
        first = RM.place("QQQ", "buy", 12.5, "20260831", dry=False)
        again = RM.place("QQQ", "buy", 12.5, "20260831", dry=False)
        check("the first order is created", first is True)
        check("the retry is collapsed, not duplicated", again is False)
        rows = deskdb.orders(RM.ACCOUNT, strategy_id=RM.STRATEGY_ID)
        check("exactly one row in the ledger", len(rows) == 1, f"{len(rows)} rows")

        # A REJECTION must not block the session. Without this a single refusal at 15:45
        # ("no price yet") costs the whole month, because every later firing in the window
        # sees the same client_order_id and reports "already sent".
        deskdb.mark_order(rows[0]["seq"], "rejected", reason="no price for QQQ yet")
        after = RM.place("QQQ", "buy", 12.5, "20260831", dry=False)
        rows2 = deskdb.orders(RM.ACCOUNT, strategy_id=RM.STRATEGY_ID)
        check("a rejected order is retried under a fresh id", after is True,
              f"{len(rows2)} rows now")
        check("and the retry is a second row, not an overwrite", len(rows2) == 2,
              f"{len(rows2)}")
        # A FILLED order must still block, which is the property that protects the book.
        deskdb.mark_order(rows2[-1]["seq"], "filled", filled_qty=12.5, avg_price=400.0)
        blocked = RM.place("QQQ", "buy", 12.5, "20260831", dry=False)
        check("a filled order still blocks", blocked is False,
              f"{len(deskdb.orders(RM.ACCOUNT, strategy_id=RM.STRATEGY_ID))} rows")
    finally:
        deskdb.close()
        RM.DESK_DB = real


def test_sells_precede_the_buy() -> None:
    print("sells are queued before the buy that spends their proceeds")
    sent = []

    def fake_place(symbol, side, qty, session, dry):
        sent.append((side, symbol, RM.order_id(symbol, session)))
        return True

    real_place, real_view = RM.place, RM.desk_view
    RM.place = fake_place
    RM.desk_view = lambda: {"cash": 1000.0, "equity": 3500.0, "seen": True,
                            "holdings": {"IWM": 10.0, "XLK": 5.0}}
    try:
        RM.rebalance("QQQ", {"QQQ": 400.0, "IWM": 200.0, "XLK": 100.0},
                     "20260831", dry=False)
    finally:
        RM.place, RM.desk_view = real_place, real_view
    sides = [s for s, _, _ in sent]
    check("something was sent", bool(sent), str(sent))
    check("every sell precedes every buy",
          "buy" not in sides or all(s == "sell" for s in sides[:sides.index("buy")]),
          str(sides))
    check("the winner is bought exactly once", sides.count("buy") == 1, str(sides))
    check("the names already held are sold",
          {sym for side, sym, _ in sent if side == "sell"} == {"IWM", "XLK"}, str(sent))
    check("every id carries the session", all("20260831" in cid for _, _, cid in sent))


def test_window_refuses_outside() -> None:
    print("the window refuses every instant that is not the decision instant")
    # 2026-08-31 is a Monday and the last trading day of August.
    def at(local: str):
        return pd.Timestamp(local, tz=TZ).tz_convert("UTC").to_pydatetime()
    cases = [("2026-08-31 10:00", False, "mid-session"),
             ("2026-08-31 15:44", False, "one minute early"),
             ("2026-08-31 15:45", True, "the decision instant"),
             ("2026-08-31 15:59", True, "inside the window"),
             ("2026-08-31 16:30", False, "after the bell"),
             ("2026-08-28 15:50", False, "a Friday that is not month end")]
    for local, want, why in cases:
        got, reason = RM.is_decision_time(at(local))
        check(f"{why} ({local}) -> {'fire' if want else 'skip'}", got == want, reason)


def main() -> int:
    for t in (test_calendar_matches_real_sessions, test_fold_matches_the_desk,
              test_signal_is_the_shared_one, test_idempotency_key,
              test_ledger_roundtrip, test_sells_precede_the_buy,
              test_window_refuses_outside):
        t()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("the live rotation trades the schedule, signal and fold the research scored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
