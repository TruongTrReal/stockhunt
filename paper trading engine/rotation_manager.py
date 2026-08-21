"""The monthly ETF rotation, traded on the live desk through the public API.

This is a **manager**, not a desk component. It holds no positions, imports no Nautilus,
and touches no database: it computes a pick and posts orders to `/v1/orders`, exactly as
an outside manager's code would. `desk_control` drains the ledger and does the trading.
That separation is the whole design of the member path and it is why this file can be
edited and restarted without the desk noticing.

WHAT IT TRADES

`QQQ IWM XLK TLT`, the basket named `desk4` in `walk-forward optimization/rotation.py`.
Once a month it ranks the four on their trailing 63-session total return and puts
everything into the winner. Research, controls and caveats are in that module and in the
report it links; the honest summary is that this beats a coin toss that keeps the same
asset mix (p = 0.003 on the sibling basket) and does NOT clear this project's own
significance bar, so it is deployed as a forward test rather than as a conclusion.

The basket is what the desk can already trade. `paper_config.UNIVERSE["us_etfs"]` is five
names and `desk_control` refuses anything outside it, so the better-evidenced `broad`
basket would have cost a desk restart and a re-warm of every open book. That trade was
made deliberately and it is recorded in `rotation.py`'s `BASKETS["desk4"]`.

WHEN IT DECIDES, AND WHY NOT AT THE CLOSE

A rotation scored on a session's own close and filled at that same close assumes a print
nobody could have known until it happened. The fix this repo uses is not to delay the fill
but to **decide earlier and still trade the close**: at `DECIDE_LEAD_MIN` before the bell
on the last trading day of the month, this builds each session from the part of it that
had already happened, ranks on that, and sends market orders into the closing auction.
`stockhunt.sessions` is the arithmetic and `book_strategy.py` already does the same thing
for the house's IBS books.

IDEMPOTENCY, WHICH IS THE ONLY WAY THIS CAN LOSE MONEY BY ACCIDENT

`client_order_id` is derived from the strategy, the symbol and the SESSION DATE -- never
from a clock, a counter or a random value. Run this twice in the same session, or retry
after a timeout, and the desk returns the first order rather than opening a second
position. The timer that runs it fires several times inside the decision window on
purpose, because a single fire that hits a network error would silently skip a month;
with a stable key, extra fires are free.

Run::

    python rotation_manager.py --dry-run        # decide and print, post nothing
    python rotation_manager.py --status         # what the desk thinks we hold
    python rotation_manager.py                  # the real thing, gated on the window
    python rotation_manager.py --force          # decide now, whatever day it is

`STOCKHUNT_API_KEY` and `STOCKHUNT_STRATEGY_ID` come from the environment, falling back to
`.env.local` at the repo root. **The key is minted behind a browser login and the
registration is made by a human in the console** -- this file never attempts either, per
rule 3 of `/desk/agent.md`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pandas as pd

import paper_config                                   # noqa: F401  (path bootstrap)
import td_live
from stockhunt import rotation as sh_rotation
from stockhunt.sessions import fold_sessions, session_of

BASKET = ["QQQ", "IWM", "XLK", "TLT"]
ASSET_CLASS = "us_etfs"
SIGNAL_TF = "5m"            # a `td_live.INTERVALS` key, not the vendor string ("5min")
DECIDE_LEAD_MIN = 15        # act this many minutes before the bell
LOOKBACK = sh_rotation.LOOKBACK

# Enough 5-minute bars to rebuild 64 partial sessions with room for holidays and gaps.
# 78 bars a session x ~90 sessions. Vendor caps a single request well above this.
SIGNAL_BARS = 5000

API_BASE = os.environ.get("STOCKHUNT_API", "https://srv1903626.hstgr.cloud")
TIMEOUT = 30


# --------------------------------------------------------------------- credentials


def _env_file() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".env.local")
    out = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def credentials() -> tuple[str, str]:
    env = _env_file()
    key = os.environ.get("STOCKHUNT_API_KEY") or env.get("STOCKHUNT_API_KEY")
    sid = os.environ.get("STOCKHUNT_STRATEGY_ID") or env.get("STOCKHUNT_STRATEGY_ID")
    if not key or not sid:
        raise SystemExit(
            "STOCKHUNT_API_KEY and STOCKHUNT_STRATEGY_ID must be set (environment or "
            ".env.local at the repo root).\n"
            "Both come from the console: register the strategy at "
            f"{API_BASE}/console and mint a key there. Neither can be created from a "
            "script -- see rule 3 and rule 5 of /desk/agent.md.")
    return key, sid


# ----------------------------------------------------------------------- the api


def call(method: str, path: str, key: str, body: dict | None = None) -> tuple[int, dict]:
    """One HTTP call. Returns `(status, payload)` and raises only on transport failure.

    A non-2xx is returned rather than raised because the contract puts meaning in the
    body: `409` carries the existing order, `422` carries the reason a registration does
    not cover a symbol. Turning those into exceptions loses the half that explains itself.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API_BASE}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": "stockhunt-rotation-manager/1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode() or "{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() or "{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw[:400]}


# --------------------------------------------------------------------- the signal


def bell() -> tuple[str, int, int]:
    tz, (hh, mm) = paper_config.SESSION_CLOSE[ASSET_CLASS]
    return tz, hh, mm


def sessions_for(symbol: str) -> pd.DataFrame | None:
    """Trailing partial sessions for one name, each cut before the bell.

    Every row is a partial session, today's included. A rule whose history is full-day
    bars and whose newest row is partial is comparing two different statistics -- so the
    history is built the same way the live row is, and that is what makes the live signal
    comparable to the backtest rather than merely similar to it.
    """
    bars = td_live.fetch_bars(symbol, SIGNAL_TF, n=SIGNAL_BARS)
    if bars is None or len(bars) == 0:
        return None
    idx = pd.DatetimeIndex(bars.index)
    if idx.tz is None:
        bars = bars.tz_localize("UTC")
    tz, hh, mm = bell()
    return fold_sessions(bars, tz, hh, mm, DECIDE_LEAD_MIN)


def decide() -> tuple[str | None, pd.DataFrame]:
    """`(winning symbol, the score table)`. `None` when nothing has enough history."""
    frames, rows = {}, []
    for s in BASKET:
        df = sessions_for(s)
        if df is None or len(df) < LOOKBACK + 1:
            rows.append({"symbol": s, "sessions": 0 if df is None else len(df),
                         "score": float("nan"), "last": float("nan")})
            continue
        frames[s] = df["Close"]
        rows.append({"symbol": s, "sessions": len(df), "score": float("nan"),
                     "last": float(df["Close"].iloc[-1])})
    if not frames:
        return None, pd.DataFrame(rows)

    closes = pd.DataFrame(frames).sort_index()
    sc = sh_rotation.scores(closes, LOOKBACK)
    latest = sc[-1]
    table = pd.DataFrame(rows).set_index("symbol")
    for i, s in enumerate(closes.columns):
        table.loc[s, "score"] = latest[i]
    winner = sh_rotation.pick(latest, list(closes.columns))
    return winner, table.reset_index()


# ------------------------------------------------------------------- the schedule


def is_decision_time(now: datetime | None = None) -> tuple[bool, str]:
    """Is now inside the decision window of the last trading day of the month?

    Two conditions, and the second is the one that needs care. "Last trading day" cannot
    be read off a calendar -- the source's own code tested `date.day == monthrange(...)`
    and therefore skipped 30% of months, every one whose 31st fell on a weekend. It is
    asked of the SESSION INDEX instead: today is the last trading day if the next session
    the vendor knows about belongs to a different month.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tz, hh, mm = bell()
    local = pd.Timestamp(now).tz_convert(tz)
    _, decide_at = session_of(pd.Timestamp(now), tz, hh, mm, DECIDE_LEAD_MIN)
    if local < decide_at:
        return False, f"before the window ({local:%H:%M} < {decide_at:%H:%M} {tz})"
    close_at = decide_at + pd.Timedelta(minutes=DECIDE_LEAD_MIN)
    if local > close_at:
        return False, f"after the bell ({local:%H:%M} > {close_at:%H:%M} {tz})"

    # A calendar month-end that is not a session is exactly the source's bug, so the test
    # is "is there another session in this month", not "is today the 31st".
    nxt = _next_session(local)
    if nxt is None:
        return False, "no exchange calendar; refusing to guess the month end"
    if (nxt.year, nxt.month) == (local.year, local.month):
        return False, f"not the last trading day -- {nxt} is still this month"
    return True, f"last trading day, {local:%H:%M} {tz}, bell at {close_at:%H:%M}"


def _next_session(local: pd.Timestamp):
    """The next US equity session date after `local`, or None if it cannot be determined.

    Returns a plain `date`, and that is deliberate. `valid_days` hands back midnight UTC
    for each session, and converting that to New York lands on the PREVIOUS evening --
    2026-09-01 becomes 2026-08-31 20:00, so the last trading day of August reads as "there
    is another August session" and the month is never traded. The gate caught exactly
    that. A session is a calendar date on its own exchange; do not give it a time.

    None is a refusal to guess. The caller treats it as "not the last trading day" and
    skips, because trading a wrongly-inferred month end is worse than missing one.
    """
    try:
        import pandas_market_calendars as mcal
        cal = mcal.get_calendar("NYSE")
        days = cal.valid_days(start_date=(local + pd.Timedelta(days=1)).date(),
                              end_date=(local + pd.Timedelta(days=12)).date())
        if not len(days):
            return None
        return pd.Timestamp(days[0]).tz_localize(None).normalize().date()
    except Exception:
        return None


# --------------------------------------------------------------------- the orders


def holdings(key: str, sid: str) -> dict[str, float]:
    """What the desk believes this strategy holds, per symbol.

    Read back from the desk rather than remembered locally. A manager that keeps its own
    idea of the book will eventually disagree with the desk about it -- after a rejected
    order, a restart, or a partial fill -- and every subsequent order compounds the error.
    """
    status, body = call("GET", f"/v1/strategies/{sid}", key)
    if status != 200:
        raise SystemExit(f"GET /v1/strategies/{sid} -> {status}: "
                         f"{body.get('detail') or body}")
    out = {}
    for h in (body.get("holdings") or []):
        qty = float(h.get("qty") or h.get("units") or 0.0)
        if abs(qty) > 1e-9:
            out[h["symbol"]] = qty
    return out


def order_id(sid: str, symbol: str, session: str) -> str:
    """`rot-<strategy>-<symbol>-<session>`. Stable across every retry within a session."""
    return f"rot-{sid}-{symbol}-{session}"


def place(key: str, sid: str, symbol: str, side: str, qty: float,
          session: str, dry: bool) -> dict:
    body = {"strategy_id": sid, "client_order_id": order_id(sid, symbol, session),
            "symbol": symbol, "side": side, "qty": round(float(qty), 6),
            "type": "market", "tif": "day"}
    if dry:
        print(f"    DRY  {side:4s} {qty:>12.4f} {symbol}   "
              f"client_order_id={body['client_order_id']}")
        return {"state": "dry-run"}
    status, out = call("POST", "/v1/orders", key, body)
    tag = {200: "already sent", 202: "accepted"}.get(status, f"HTTP {status}")
    print(f"    {tag:12s} {side:4s} {qty:>12.4f} {symbol}"
          + (f"   reason={out.get('reason') or out.get('detail')}"
             if status not in (200, 202) else ""))
    return out


def rebalance(key: str, sid: str, winner: str, prices: dict[str, float],
              session: str, dry: bool) -> None:
    """Sell everything that is not the winner, then buy the winner with the proceeds.

    Sells first and in one pass, because the desk drains strictly in `seq` order: a buy
    queued ahead of the sell that funds it is refused for want of cash, and the month is
    then spent in the wrong name. Ordering here is cheaper than reconciling there.
    """
    held = holdings(key, sid)
    for symbol, qty in sorted(held.items()):
        if symbol != winner and qty > 0:
            place(key, sid, symbol, "sell", qty, session, dry)
    if winner in held and held[winner] > 0:
        print(f"    hold  {winner} -- already the pick, nothing to do")
        return
    px = prices.get(winner)
    if not px or px <= 0:
        print(f"    SKIP  no price for {winner}; not sizing an order blind")
        return
    equity = sum(prices.get(s, 0.0) * q for s, q in held.items())
    status, body = call("GET", f"/v1/strategies/{sid}", key)
    cash = float((body or {}).get("cash") or 0.0) if status == 200 else 0.0
    budget = (cash + equity) * 0.98        # the same 2% buffer the house books keep
    if budget <= 0:
        print("    SKIP  no equity reported yet; the desk may still be warming up")
        return
    place(key, sid, winner, "buy", budget / px, session, dry)


# ------------------------------------------------------------------------ cli


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="decide and print, post nothing")
    ap.add_argument("--force", action="store_true",
                    help="skip the last-trading-day and window checks")
    ap.add_argument("--status", action="store_true",
                    help="show the desk's view of this strategy and exit")
    args = ap.parse_args()

    if args.status:
        key, sid = credentials()
        status, body = call("GET", f"/v1/strategies/{sid}", key)
        print(json.dumps(body, indent=2)[:4000])
        return 0 if status == 200 else 1

    # The window is checked BEFORE the credentials, and the order matters operationally.
    # The timer fires ~36 times a day and all but a handful are outside the window; asking
    # for credentials first would turn every one of those into a unit failure on a box
    # where the key has not been pasted in yet, and a service that alerts constantly is a
    # service nobody reads. Missing credentials are only a failure when they actually
    # stopped a trade -- which is exactly when this returns nonzero.
    ok, why = is_decision_time()
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ}] {why}")
    if not ok and not args.force:
        return 0
    if not ok:
        print("  --force: deciding anyway")
    key, sid = credentials()

    winner, table = decide()
    print(table.to_string(index=False, float_format=lambda x: f"{x:10.4f}"))
    if winner is None:
        print("  no name has enough history yet; holding whatever is held")
        return 0

    tz, hh, mm = bell()
    session = pd.Timestamp.now(tz=tz).strftime("%Y%m%d")
    print(f"  pick: {winner}   session {session}")

    prices = td_live.fetch_prices(BASKET) or {}
    rebalance(key, sid, winner, prices, session, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
