"""Mirror the desk's book into Alpaca's paper accounts, and record what the broker did.

The desk fills its own orders. `SandboxExecutionClient` prices a fill from the Twelve Data
bar that produced the signal, so every fill lands at that bar's close — the same price the
backtest assumed, which is exactly what makes the live record comparable to the sheet and
exactly what the live record therefore cannot check. Whether those fills exist at a real
venue, at a real spread, at a real time of day, is a question the sandbox is structurally
unable to answer about itself.

This process asks it. It reads `results/paper_state.json`, computes the desk's net
exposure per symbol, and drives an Alpaca paper account to a proportional copy of it. What
comes back — a real fill price, at a real time, or a refusal — goes into `state/alpaca.db`
beside the desk's own mark for the same decision.

    run_paper.py  (UNCHANGED)  ->  SandboxExecutionClient  ->  results/paper.db   [truth]
          |
          +-- paper_state._write --> results/paper_state.json
                                            |
                            alpaca_mirror.py (this process)
                                            |  target - held = delta
                                            v
                            POST /v2/orders  ->  paper-api.alpaca.markets
                                            |
                                            v
                            state/alpaca.db   [the second record]

**Out of process, and that is the design, not a convenience.** No HTTP client enters the
Nautilus node, nothing here can raise inside a strategy's `on_bar`, and an Alpaca outage,
a rate limit or a bad response cannot stop the desk trading. It is the same separation
`stockhunt/deskdb.py` buys between the API and the desk: one side writes a document, the
other reads it, and neither imports the other.

**`paper.db`, the board and every published number are untouched.** Alpaca is a second
record, not a replacement for the first.

Run::

    python alpaca_mirror.py --check                 # credentials, equity, coverage
    python alpaca_mirror.py --once --dry-run        # plan every class, send nothing
    python alpaca_mirror.py --once --class us_etfs  # one class, for real
    python alpaca_mirror.py                         # the loop
    python alpaca_mirror.py --report                # fill price vs the desk's mark

Long runs go through bash, detached, as everything else in this repo does::

    nohup python -u alpaca_mirror.py > logs/alpaca_mirror.log 2>&1 &
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import alpaca_client
import alpaca_map
import alpaca_store
import paper_config

STATE_PATH = paper_config.RESULTS_DIR / "paper_state.json"

# How stale the desk's snapshot may be before the mirror stops acting on it. Reconciling
# to a target the desk has already abandoned is worse than not reconciling at all: the
# desk's own record moves on and Alpaca's does not, and the divergence that produces looks
# exactly like execution slippage in the one table this process exists to fill.
#
# Fifteen minutes is loose enough for a 1d/4h desk that only writes when something changes,
# and tight enough that a node which died overnight is caught by the next cycle.
MAX_SNAPSHOT_AGE = timedelta(minutes=15)

DEFAULT_INTERVAL = 60.0

# Alpaca trades crypto around the clock; equities only in the session. Outside it a market
# order queues to the next open and fills at a price nobody decided on, which is the same
# mistake `desk_orders.STALE_BARS` refuses one process over.
ALWAYS_OPEN = {"crypto"}


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ------------------------------------------------------------------ the desk's document

def load_snapshot(path: Path) -> tuple[dict, datetime]:
    """The desk's state, and when it was last written.

    Age comes from the file's mtime rather than from `generated_at`, which is a formatted
    string for a human and a parsing liability for a gate. `paper_state._write` replaces
    the file atomically on every change, so the mtime is exactly what is wanted.
    """
    snap = json.loads(path.read_text(encoding="utf-8"))
    written = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return snap, written


def classes_in(snapshot: dict) -> list[str]:
    seen = {s.get("cls") for s in snapshot.get("strategies", [])}
    return [c for c in alpaca_client.CLASS_ENV if c in seen]


# ------------------------------------------------------------------ one class, one cycle

def build_plan(cls: str, client: alpaca_client.AlpacaClient, snapshot: dict,
               assets: dict[str, dict] | None) -> alpaca_map.Plan:
    targets = alpaca_map.desk_targets(snapshot, cls)
    marks = alpaca_map.desk_marks(snapshot, cls)
    desk_eq = alpaca_map.desk_equity(snapshot, cls)
    alpaca_eq = alpaca_map.account_equity(client.account())
    held = alpaca_map.held_units(client.positions())
    ratio = alpaca_map.scale_ratio(desk_eq, alpaca_eq)
    plan = alpaca_map.plan_orders(
        cls, targets=targets, held=held, marks=marks, ratio=ratio, assets=assets,
        desk_eq=desk_eq, alpaca_eq=alpaca_eq)
    plan.marks = marks
    return plan


def submit_plan(plan: alpaca_map.Plan, client: alpaca_client.AlpacaClient,
                cycle_id: int, cycle_token: str, *, dry_run: bool) -> tuple[int, int]:
    sent = failed = 0
    for order in plan.orders:
        coid = alpaca_map.client_order_id(plan.cls, order["symbol"], cycle_token)
        if dry_run:
            alpaca_store.record_order(cycle_id, coid, order, state="dry_run")
            log(f"  [dry] {order['side'].upper():4s} {order['qty']:.6f} "
                f"{order['symbol']:<10s} (~${order['notional']:,.0f})")
            continue
        try:
            resp = client.submit(
                symbol=order["alpaca_symbol"], side=order["side"], qty=order["qty"],
                client_order_id=coid,
                # Crypto refuses `day`; `gtc` is the venue's own recommendation for it.
                time_in_force="gtc" if plan.cls == "crypto" else "day")
        except alpaca_client.AlpacaError as exc:
            failed += 1
            alpaca_store.record_order(cycle_id, coid, order, state="rejected",
                                      reason=f"HTTP {exc.status}: {exc.body[:200]}")
            log(f"  REJECTED {order['side']} {order['symbol']}: {exc.body[:160]}")
            continue
        sent += 1
        alpaca_store.record_order(cycle_id, coid, order, state="submitted",
                                  alpaca_order_id=(resp or {}).get("id"))
        log(f"  {order['side'].upper():4s} {order['qty']:.6f} {order['symbol']:<10s} "
            f"(~${order['notional']:,.0f})  {(resp or {}).get('id', '?')}")
    return sent, failed


def collect_fills(cls: str, client: alpaca_client.AlpacaClient) -> int:
    """Match Alpaca's closed orders against what this store sent, and record the price.

    Driven off the store's own open orders rather than off a time window, so a fill that
    lands minutes after the cycle that requested it is still picked up — and so a restart
    picks up everything outstanding rather than only what it happens to have sent.
    """
    pending = {o["client_order_id"]: o for o in alpaca_store.open_orders(cls)}
    if not pending:
        return 0
    recorded = 0
    try:
        closed = client.orders(status="closed", limit=200)
    except alpaca_client.AlpacaError as exc:
        log(f"  could not read closed orders: {exc}")
        return 0
    for row in closed:
        coid = row.get("client_order_id")
        order = pending.get(coid)
        if order is None:
            continue
        try:
            qty = float(row.get("filled_qty") or 0.0)
            price = float(row.get("filled_avg_price") or 0.0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0:
            continue                      # cancelled or expired: no execution to record
        alpaca_store.record_fill(
            coid, cls=cls, symbol=order["symbol"], side=order["side"], qty=qty,
            price=price, desk_mark=order["desk_mark"], at=row.get("filled_at"),
            alpaca_order_id=row.get("id"))
        recorded += 1
    return recorded


def run_cycle(cls: str, client: alpaca_client.AlpacaClient, snapshot: dict,
              assets: dict[str, dict] | None, *,
              dry_run: bool, force_hours: bool) -> alpaca_map.Plan | None:
    filled = collect_fills(cls, client)
    if filled:
        log(f"[{cls}] recorded {filled} fill(s)")

    plan = build_plan(cls, client, snapshot, assets)
    note = None

    tradeable = True
    if cls not in ALWAYS_OPEN and not force_hours:
        try:
            tradeable = bool(client.clock().get("is_open"))
        except alpaca_client.AlpacaError as exc:
            log(f"[{cls}] clock unavailable ({exc.status}); holding orders")
            tradeable = False
        if not tradeable:
            note = "market closed; plan recorded, no orders sent"

    cycle_id = alpaca_store.start_cycle(
        cls, snapshot_at=snapshot.get("generated_at"), desk_equity=plan.desk_equity,
        alpaca_equity=plan.alpaca_equity, ratio=plan.ratio,
        dry_run=dry_run or not tradeable, note=note)
    alpaca_store.record_targets(cycle_id, plan.targets, plan.held, plan.marks)
    # The store's own row id is the cycle's identity: monotonic, and never twice in one
    # second the way a wall clock is. The timestamp in front of it keeps ids distinct
    # across a rebuilt database, whose autoincrement starts again at 1.
    cycle_token = f"{int(time.time())}-{cycle_id}"

    log(f"[{cls}] desk ${plan.desk_equity:,.0f} -> alpaca ${plan.alpaca_equity:,.0f} "
        f"(x{plan.ratio:.4f}); {len(plan.targets)} target(s), "
        f"{len(plan.orders)} order(s)"
        + (f"; {len(plan.untradable)} untradable" if plan.untradable else "")
        + (f"; {len(plan.clamped)} negative target(s) clamped" if plan.clamped else ""))
    if plan.untradable:
        log(f"[{cls}] Alpaca will not trade: {', '.join(sorted(plan.untradable))}")
    # Not an Alpaca coverage gap and must not be reported as one: this is a book on THIS
    # desk whose published shape has no per-name units to mirror. Its exposure is real and
    # unmirrored, so the second record is incomplete by exactly this much.
    for symbol, reason in alpaca_map.unmirrorable(snapshot, cls):
        log(f"[{cls}] NOT MIRRORED - book '{symbol}' {reason}")
    for symbol, reason in plan.skipped:
        log(f"[{cls}] skip {symbol}: {reason}")

    if not tradeable:
        log(f"[{cls}] market closed; {len(plan.orders)} order(s) held for the next open")
        return plan

    sent, failed = submit_plan(plan, client, cycle_id, cycle_token, dry_run=dry_run)
    if sent or failed:
        log(f"[{cls}] {sent} sent, {failed} rejected")
    return plan


# ------------------------------------------------------------------ modes

def check(state_path: Path) -> int:
    """Credentials, account equity, and which symbols Alpaca will actually trade.

    This is where the crypto intersection and any renamed equity ticker become visible,
    and it makes no order of any kind.
    """
    configured = alpaca_client.configured_classes()
    missing = [c for c in alpaca_client.CLASS_ENV if c not in configured]
    print(f"paper endpoint : {alpaca_client.PAPER_URL}")
    print(f"configured     : {', '.join(configured) or '(none)'}")
    if missing:
        for cls in missing:
            key, secret = alpaca_client.CLASS_ENV[cls]
            print(f"  {cls}: no credentials - set {key} and {secret}")
    for cls, reason in alpaca_client.UNSUPPORTED.items():
        print(f"  {cls}: not mirrored - {reason}")
    if not configured:
        return 1

    snapshot = None
    if state_path.exists():
        snapshot, written = load_snapshot(state_path)
        age = datetime.now(timezone.utc) - written
        print(f"\nsnapshot       : {state_path}")
        print(f"  generated_at : {snapshot.get('generated_at')} "
              f"(file written {int(age.total_seconds() // 60)} min ago)")
    else:
        print(f"\nsnapshot       : {state_path} does not exist - is the desk running here?")

    failures = 0
    for cls in configured:
        print(f"\n--- {cls} ---")
        try:
            client = alpaca_client.AlpacaClient.for_class(cls)
            acct = client.account()
        except (alpaca_client.AlpacaError, RuntimeError) as exc:
            print(f"  UNUSABLE: {exc}")
            failures += 1
            continue
        print(f"  account      : {acct.get('account_number')} ({acct.get('status')})")
        print(f"  equity       : ${alpaca_map.account_equity(acct):,.2f}")
        print(f"  buying power : ${float(acct.get('buying_power') or 0):,.2f}")
        print(f"  positions    : {len(client.positions())}")

        index = alpaca_map.asset_index(
            client.assets("crypto" if cls == "crypto" else "us_equity"))
        wanted = sorted(paper_config.CLASSES[cls]["symbols"])
        ok = [s for s in wanted if alpaca_map.norm_key(s) in index]
        no = [s for s in wanted if alpaca_map.norm_key(s) not in index]
        print(f"  tradable     : {len(ok)}/{len(wanted)}")
        if no:
            print(f"  NOT tradable : {', '.join(no)}")
        if snapshot is not None:
            targets = alpaca_map.desk_targets(snapshot, cls)
            desk_eq = alpaca_map.desk_equity(snapshot, cls)
            ratio = alpaca_map.scale_ratio(desk_eq, alpaca_map.account_equity(acct))
            print(f"  desk equity  : ${desk_eq:,.2f}  ->  scale x{ratio:.4f}")
            print(f"  desk holds   : {len(targets)} name(s) "
                  f"{', '.join(sorted(targets)[:8])}{' ...' if len(targets) > 8 else ''}")
            for symbol, reason in alpaca_map.unmirrorable(snapshot, cls):
                print(f"  NOT MIRRORED : '{symbol}' {reason}")
    return 1 if failures else 0


def report() -> int:
    print(json.dumps(alpaca_store.summary(), indent=2))
    print("\nfill price against the desk's own mark "
          "(positive = Alpaca filled worse than the sandbox assumed):")
    overall = alpaca_store.slippage()
    for cls in list(alpaca_client.CLASS_ENV) + [None]:
        stats = alpaca_store.slippage(cls) if cls else overall
        label = cls or "ALL"
        if not stats["fills"]:
            print(f"  {label:<10s} no fills recorded yet")
            continue
        print(f"  {label:<10s} {stats['fills']:4d} fills  "
              f"mean {stats['mean_slip_bp']:+.2f} bp  "
              f"on ${stats['notional']:,.0f}")
    return 0


# ------------------------------------------------------------------ entry point

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="credentials, equity and symbol coverage; sends nothing")
    ap.add_argument("--report", action="store_true",
                    help="fill price against the desk's mark, out of alpaca.db")
    ap.add_argument("--once", action="store_true", help="one cycle, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan and record, but submit no order")
    ap.add_argument("--class", dest="classes", action="append",
                    choices=sorted(alpaca_client.CLASS_ENV),
                    help="limit to this class (repeatable)")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help=f"seconds between cycles (default {DEFAULT_INTERVAL:.0f})")
    ap.add_argument("--state", type=Path, default=STATE_PATH,
                    help="the desk's paper_state.json")
    ap.add_argument("--max-age", type=float,
                    default=MAX_SNAPSHOT_AGE.total_seconds() / 60.0,
                    help="refuse a snapshot older than this many minutes")
    ap.add_argument("--db", type=Path, help="override state/alpaca.db")
    ap.add_argument("--ignore-hours", action="store_true",
                    help="skip the market-open gate (equities queue to the next open)")
    args = ap.parse_args(argv)

    if args.db:
        alpaca_store.use(args.db)

    if args.check:
        return check(args.state)
    if args.report:
        return report()

    configured = args.classes or alpaca_client.configured_classes()
    if not configured:
        log("no Alpaca credentials configured; run --check")
        return 1

    clients: dict[str, alpaca_client.AlpacaClient] = {}
    assets: dict[str, dict[str, dict]] = {}
    for cls in configured:
        try:
            clients[cls] = alpaca_client.AlpacaClient.for_class(cls)
        except RuntimeError as exc:
            log(f"[{cls}] {exc}")
            return 1

    max_age = timedelta(minutes=args.max_age)
    while True:
        try:
            if not args.state.exists():
                log(f"no snapshot at {args.state}; the desk has never run here")
            else:
                snapshot, written = load_snapshot(args.state)
                age = datetime.now(timezone.utc) - written
                if age > max_age:
                    log(f"snapshot is {age.total_seconds() / 60:.0f} min old "
                        f"(limit {args.max_age:.0f}); holding - the desk may be down")
                else:
                    live = [c for c in configured if c in classes_in(snapshot)]
                    for cls in live:
                        if cls not in assets:
                            kind = "crypto" if cls == "crypto" else "us_equity"
                            assets[cls] = alpaca_map.asset_index(
                                clients[cls].assets(kind))
                        run_cycle(cls, clients[cls], snapshot, assets[cls],
                                  dry_run=args.dry_run, force_hours=args.ignore_hours)
                    for cls in configured:
                        if cls not in live:
                            log(f"[{cls}] no running book in the snapshot; nothing to do")
        except alpaca_client.AlpacaError as exc:
            log(f"alpaca refused: {exc}")
        except Exception as exc:                       # never let one cycle kill the loop
            log(f"cycle failed: {type(exc).__name__}: {exc}")

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
