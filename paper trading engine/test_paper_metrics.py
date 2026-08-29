"""The figures the board prints: marking, age and turnover.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_paper_metrics.py -q

Every case here is a number that was on the dashboard and was wrong. None of them needs a
bar, a vendor or the real `paper.db` — they are arithmetic over a registered strategy, so
they run in milliseconds and cannot go stale when somebody refetches a ticker.

The thread joining them: **the desk is books end to end, and three of these figures were
written for a system that holds one instrument.** A book registers with `symbol` set to a
label ("5 names"), keeps its quantities in `holdings`, and resumes an inception date from
the store — so a price lookup by `symbol`, a fill counter scoped to the process and an age
measured from the first bar of this session all silently produced nothing.

The last section is the same thread one kind of strategy over: a MEMBER also holds several
instruments under one `symbol` — the names they registered, joined with commas — and
published no breakdown at all, so the board drew three names as one row. Those cases build
a real `MemberStrategy`, which needs `nautilus_trader` on the path but no node, no venue
and no bar: the strategy object is constructed and handed a fill, which is all the
published shape is made of.
"""

from __future__ import annotations

import sqlite3
import pytest

import paper_config                                                     # noqa: F401
import store
import paper_state


@pytest.fixture()
def desk(tmp_path):
    """A registry backed by a throwaway store. The real `paper.db` is never opened."""
    store.close() if hasattr(store, "close") else None
    store._conn = None
    store.DB_PATH = tmp_path / "paper.db"
    paper_state.STATE_PATH = tmp_path / "paper_state.json"
    paper_state.MIRROR_PATH = None
    paper_state._strategies.clear()
    yield paper_state
    try:
        store._conn.close()
    except Exception:
        pass
    store._conn = None


def book(ps, sid="00:us_stocks-1d-ibs", capital=100_000.0, **kw):
    """Register a book the way `book_strategy.on_start` does."""
    fields = dict(
        account="00", kind="book", symbol="3 names", venue="SANDBOX", cls="us_stocks",
        tf="1d", rule="ibs", benchmark=None, state="flat", status="warming",
        since="2026-08-14", days=0, paper_pnl_pct=0.0, paper_trades=0,
        position_units=0, entry=None, capital=capital, cash=capital, units=0.0,
        equity=capital, turnover=0.0, note="", held=0, names=3,
        holdings=[{"symbol": "AAPL", "state": "flat", "units": 0.0, "entry": None,
                   "mark": None, "value": 0.0, "pnl_pct": None, "trades": 0},
                  {"symbol": "MSFT", "state": "flat", "units": 0.0, "entry": None,
                   "mark": None, "value": 0.0, "pnl_pct": None, "trades": 0},
                  {"symbol": "NVDA", "state": "flat", "units": 0.0, "entry": None,
                   "mark": None, "value": 0.0, "pnl_pct": None, "trades": 0}])
    fields.update(kw)
    ps.register(sid, **fields)
    return ps._strategies[sid]


def backdate(ps, sid, iso):
    """Age a system by moving the STORE's inception, then re-registering.

    `store.upsert_strategy` stamps `first_seen` from the wall clock and ignores whatever
    `since` the caller passes — correctly, since inception is when the desk first saw the
    system, not what a config says. So an old system can only be modelled by writing the
    store row, which is also exactly what a restart reads back.
    """
    conn = store.connect()
    conn.execute("UPDATE strategies SET first_seen = ? WHERE sid = ?", (iso, sid))
    conn.commit()
    fields = dict(ps._strategies[sid])
    fields.pop("id", None)
    ps.register(sid, **fields)
    return ps._strategies[sid]


# --------------------------------------------------------------------------- marking

def test_a_book_is_revalued_between_bars(desk):
    """The headline number on the whole desk, and it never moved.

    `mark()` looked the strategy's `symbol` up in the price dict. A book's is the string
    "3 names", so the lookup missed, the book was skipped, and P&L only changed when a bar
    closed — on a daily system, once every 24 hours. Every system on this desk is a book,
    so `mark()` returned 0 on every tick and the board sat at 0.000% exactly as its own
    docstring warns.
    """
    s = book(desk)
    s.update(cash=40_000.0, held=2, state="long")
    s["holdings"][0].update(units=100.0, entry=500.0)      # $50,000 in at 500
    s["holdings"][1].update(units=50.0, entry=200.0)       # $10,000 in at 200

    marked = desk.mark({"AAPL": 550.0, "MSFT": 200.0, "NVDA": 900.0})

    assert marked == 1, "the book was skipped entirely"
    # 40,000 cash + 100x550 + 50x200 = 105,000
    assert s["equity"] == pytest.approx(105_000.0)
    assert s["paper_pnl_pct"] == pytest.approx(5.0)


def test_marking_a_book_updates_the_rows_it_is_made_of(desk):
    """The total and the expanded table are drawn from the same prices, or they disagree
    on screen and the reader cannot tell which is stale."""
    s = book(desk)
    s.update(cash=40_000.0, held=1)
    s["holdings"][0].update(units=100.0, entry=500.0)

    desk.mark({"AAPL": 550.0, "MSFT": 200.0, "NVDA": 900.0})

    held = s["holdings"][0]
    assert held["mark"] == 550.0
    assert held["value"] == pytest.approx(55_000.0)
    assert held["pnl_pct"] == pytest.approx(10.0)
    assert s["holdings"][2]["pnl_pct"] is None, "a flat name has no entry to measure from"


def test_a_held_name_with_no_price_marks_nothing(desk):
    """Half a book is worse than none of it: the total would be short by a whole position
    and would read as a loss the desk did not take."""
    s = book(desk)
    s.update(cash=40_000.0, held=2)
    s["holdings"][0].update(units=100.0, entry=500.0)
    s["holdings"][1].update(units=50.0, entry=200.0)

    assert desk.mark({"AAPL": 550.0}) == 0
    assert s["equity"] == 100_000.0, "left at its last honest value"


def test_a_single_instrument_system_still_marks(desk):
    """The path that always worked, kept working."""
    s = book(desk, sid="00:crypto-1d-sma", kind="rule", symbol="BTC/USD", names=1,
             holdings=None)
    s.update(cash=50_000.0, units=2.0)

    assert desk.mark({"BTC/USD": 30_000.0}) == 1
    assert s["equity"] == pytest.approx(110_000.0)
    assert s["mark_price"] == 30_000.0


# ------------------------------------------------------------------------------- age

def test_days_comes_from_inception_not_from_this_process(desk, monkeypatch):
    """`since` is resumed from the store; `days` was measured from the first bar of the
    session. The board printed both, side by side: "since 2026-08-14 · 1 day in", days
    later. The correction existed but was guarded on the caller passing a LATER `since` —
    and `_export`, the path that runs every bar, passes no `since` at all.
    """
    from datetime import datetime, timezone

    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(paper_state, "datetime", Now)
    s = book(desk)
    s = backdate(desk, s["id"], "2026-08-14T00:00:00+00:00")
    desk.update(s["id"], days=1, paper_pnl_pct=0.0)     # what the strategy reports

    assert s["days"] == 10, "ten days since 2026-08-14, not one since the restart"
    assert s["since"] == "2026-08-14"


# -------------------------------------------------------------------------- turnover

def test_turnover_is_round_trips_per_name_per_year(desk, monkeypatch):
    """`book_strategy` set it to 0.0 at registration and never touched it again, so a desk
    with 1,389 fills on it reported "turnover 0.0/yr" on every row."""
    from datetime import datetime, timezone

    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 8, 14, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(paper_state, "datetime", Now)
    s = book(desk)
    s = backdate(desk, s["id"], "2026-08-14T00:00:00+00:00")   # exactly one year old
    assert s["days"] == 365
    for i in range(60):                              # 60 fills = 30 round trips
        desk.push_trade(s["id"], f"2026-09-{i % 28 + 1:02d} 00:00", "BUY", 1.0, 100.0,
                        symbol="AAPL", ref=f"f{i}")

    assert s["lifetime_trades"] == 60
    # 60 fills / 2 = 30 round trips, over one year, across 3 names
    assert s["turnover"] == pytest.approx(10.0, abs=0.05)


def test_turnover_is_zero_without_fills(desk):
    s = book(desk)
    assert s["turnover"] == 0.0


def test_turnover_is_withheld_until_there_is_enough_record(desk):
    """A rate annualised from two days is arithmetic, not a measurement.

    The desk was two days old with its opening positions behind it, and the honest
    division reported 493 round trips per name per year. The board prints nothing rather
    than a figure whose dominant term is the divisor.
    """
    s = book(desk)                                   # registered today
    for i in range(60):
        desk.push_trade(s["id"], f"2026-08-{i % 28 + 1:02d} 00:00", "BUY", 1.0, 100.0,
                        symbol="AAPL", ref=f"f{i}")

    assert s["days"] < paper_state.MIN_TURNOVER_DAYS
    assert s["turnover"] is None, "a two-day-old record cannot carry an annual rate"


# ------------------------------------------------------------- the two P&L columns

def test_an_opening_fill_records_no_realised_pnl(desk):
    """The property every trade statistic on the board rests on.

    A buy that opened a position closed nothing, so `realised` is **None** — not 0.0, and
    emphatically not the book's mark. The board filters closed trades on `realised != null`
    and would count this fill as a closed trade the moment that null became a number.
    """
    s = book(desk)
    desk.push_trade(s["id"], "2026-08-14 00:00", "BUY", 2.0, 483.01,
                    pnl=-59.33, symbol="AMD", realised=None)

    t, = s["trades"]
    assert t["realised"] is None
    assert t["pnl"] == -59.33, "the book snapshot is still recorded, under its own name"


def test_the_two_columns_are_stored_separately(desk):
    """`book_pnl` and `realised_pnl` are different questions and the record answers both.

    This is the AMD round trip off `00:us_stocks-1d-ibs`, with the numbers that were on the
    board: a +$62.72 sell that the page printed as **-$59.33**, because it was publishing
    the whole book's mark under the heading "Realised P&L". Two names filling in the same
    second got the same value, so one book snapshot became two "trades".
    """
    s = book(desk)
    desk.push_trade(s["id"], "2026-08-14 00:00", "BUY", 2.0, 483.01,
                    pnl=0.0, symbol="AMD", realised=None)
    desk.push_trade(s["id"], "2026-08-15 00:00", "SELL", 2.0, 514.37,
                    pnl=-59.33, symbol="AMD", realised=62.72)

    opened, closed = s["trades"]
    assert (opened["realised"], opened["pnl"]) == (None, 0.0)
    assert (closed["realised"], closed["pnl"]) == (62.72, -59.33)
    # What the metrics table computes: one closed trade, and it won.
    real = [t["realised"] for t in s["trades"] if t["realised"] is not None]
    assert real == [62.72]


def test_a_scratch_trade_is_closed_and_realised_zero(desk):
    """0.0 and None are different facts and the store must not collapse them: a trade
    closed exactly at cost IS a closed trade, with a realised P&L of nothing."""
    s = book(desk)
    desk.push_trade(s["id"], "2026-08-14 00:00", "BUY", 1.0, 100.0,
                    symbol="AAPL", realised=None)
    desk.push_trade(s["id"], "2026-08-15 00:00", "SELL", 1.0, 100.0,
                    symbol="AAPL", realised=0.0)

    assert [t["realised"] for t in s["trades"]] == [None, 0.0]


def test_realised_pnl_is_not_part_of_the_fill_key(desk):
    """A warm-up replay must still collapse.

    `realised` is a consequence of a fill, not part of its identity — and a replayed fill
    computed against a re-warmed book can legitimately carry a different one. Putting it in
    the natural key would switch off the deduplication `test_store.py` protects.
    """
    s = book(desk)
    desk.push_trade(s["id"], "2026-08-14 00:00", "SELL", 1.0, 100.0,
                    symbol="AAPL", realised=5.0)
    desk.push_trade(s["id"], "2026-08-14 00:00", "SELL", 1.0, 100.0,
                    symbol="AAPL", realised=7.0)

    assert s["lifetime_trades"] == 1, "the same fill twice is one fill"


def test_the_migration_recovers_realised_pnl_for_a_record_written_without_it(desk):
    """A v2 database has the fills but not the column, and the fills ARE the input — so
    the value is recomputable exactly rather than lost. The eight completed IBS round
    trips were all winners; the board reported twenty losses."""
    conn = store.connect()
    s = book(desk)
    for ts, side, qty, price, sym in [
            ("2026-08-14 00:00", "BUY", 2.0, 483.01, "AMD"),
            ("2026-08-14 00:00", "BUY", 15.0, 64.09, "BAC"),
            ("2026-08-15 00:00", "SELL", 2.0, 514.37, "AMD"),
            ("2026-08-15 00:00", "SELL", 15.0, 64.49, "BAC")]:
        desk.push_trade(s["id"], ts, side, qty, price, symbol=sym, realised=None)

    # Rewind to v2: drop what the live path just wrote and re-run the migration.
    conn.execute("UPDATE fills SET realised_pnl = NULL")
    conn.execute("PRAGMA user_version = 2")
    conn.execute("ALTER TABLE fills DROP COLUMN realised_pnl")
    conn.commit()
    assert store._migrate(conn) is True

    got = dict(conn.execute(
        "SELECT symbol, realised_pnl FROM fills WHERE side = 'SELL'").fetchall())
    assert got["AMD"] == pytest.approx(62.72)
    assert got["BAC"] == pytest.approx(6.00)
    assert all(r[0] is None for r in conn.execute(
        "SELECT realised_pnl FROM fills WHERE side = 'BUY'")), "a buy closed nothing"


# ------------------------------------------------------------- what the feed subscribes to

def test_the_feed_follows_the_running_book(desk):
    """The symbol list the price feed uses comes from what is REGISTERED.

    It used to come from `run_paper.build_plan()`, the automatic per-symbol legs — and that
    list is empty in the configuration the desk actually runs in ("no automatic legs; the
    desk runs what is registered"). So the tick socket subscribed to the empty string and
    the REST poller polled nothing, while the hub cheerfully reported `upstream=live`. The
    status described the socket, not the subscription, and P&L could not move.
    """
    book(desk, sid="00:us_stocks-1d-ibs")
    book(desk, sid="00:crypto-1d-sma", kind="rule", symbol="BTC/USD", names=1,
         holdings=None)

    got = desk.marked_symbols()
    assert got == ["AAPL", "BTC/USD", "MSFT", "NVDA"], got


def test_a_books_label_is_never_asked_of_the_vendor(desk):
    """"3 names" is not an instrument. Asking for it is the same mistake that stopped
    books being marked in the first place."""
    book(desk)
    assert not any("names" in s for s in desk.marked_symbols())


def test_a_members_multi_symbol_field_becomes_several_tickers(desk):
    """A member types the field; it is not always one ticker.

    Two live registrations carry `"AAPL, MSFT, AMZN"` and a seven-name version of it, and
    the whole string was going to the vendor as one instrument -- a guaranteed miss, and
    one more oversized entry in a `/price` URL that was already over the limit.
    """
    book(desk, sid="01:test", kind="member", names=3, holdings=None,
         symbol="AAPL, MSFT, AMZN")
    got = desk.marked_symbols()
    assert got == ["AAPL", "AMZN", "MSFT"], got
    assert not any("," in s for s in got), got


def test_no_symbol_carries_whitespace_or_empties(desk):
    """`"AAPL,,  MSFT "` is three fields and two tickers."""
    book(desk, sid="01:sloppy", kind="member", names=2, holdings=None,
         symbol="AAPL,,  MSFT ")
    assert desk.marked_symbols() == ["AAPL", "MSFT"]


# --------------------------------------------------------------- the /price URL has a size

def test_price_requests_are_chunked_to_fit_a_url():
    """The vendor batches `/price` in the QUERY STRING, so the batch has a ceiling.

    At ~125 symbols the desk's request was `414 URI Too Long` on every pass: 1,035
    failures and no successes in a day, while it went on filling orders against prices it
    could not refresh. Nothing was bounding the size.
    """
    import td_live
    symbols = [f"SYM{i:03d}" for i in range(125)]
    chunks = td_live._price_chunks(symbols)

    assert len(chunks) > 1, "125 symbols must not go out as one request"
    assert [s for c in chunks for s in c] == symbols, "every symbol, in order, exactly once"
    for c in chunks:
        assert len(c) <= td_live.MAX_PRICE_SYMBOLS
        assert len(",".join(c)) <= td_live.MAX_PRICE_CHARS


def test_chunking_counts_percent_encoding_of_crypto_pairs():
    """`BTC/USD` costs 11 characters in a URL, not 7. A count-only guard overflows on a
    book of pairs even while it reports itself within budget."""
    import td_live
    pairs = [f"CO{i:02d}/USD" for i in range(60)]
    for c in td_live._price_chunks(pairs):
        encoded = ",".join(c).replace("/", "%2F")
        assert len(encoded) <= td_live.MAX_PRICE_CHARS * 3, len(encoded)
        assert len(c) <= td_live.MAX_PRICE_SYMBOLS


def test_one_bad_chunk_does_not_unprice_the_whole_book(monkeypatch):
    """Partial marks are the point. One unpriceable symbol used to mean nothing marked."""
    import td_live

    class Resp:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    calls = []

    def fake_get(url, timeout=None, params=None):
        chunk = params["symbol"].split(",")
        calls.append(chunk)
        if "BAD" in chunk:
            raise RuntimeError("414 Client Error: URI Too Long")
        return Resp({s: {"price": "10.0"} for s in chunk})

    monkeypatch.setattr(td_live.requests, "get", fake_get)
    monkeypatch.setattr(td_live, "api_key", lambda: "k")
    monkeypatch.setattr(td_live, "MAX_PRICE_SYMBOLS", 2)

    out = td_live.fetch_prices(["AAPL", "MSFT", "BAD", "NVDA"])
    assert len(calls) == 2, calls
    assert out == {"AAPL": 10.0, "MSFT": 10.0}, out


def test_a_total_failure_still_raises(monkeypatch):
    """Partial tolerance must not hide a real outage: the caller logs "will retry" off
    this exception, and swallowing it would make a dead vendor look like a quiet one."""
    import td_live

    def fake_get(url, timeout=None, params=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(td_live.requests, "get", fake_get)
    monkeypatch.setattr(td_live, "api_key", lambda: "k")
    with pytest.raises(RuntimeError):
        td_live.fetch_prices(["AAPL", "MSFT"])


def test_a_single_symbol_still_reads_the_bare_object(monkeypatch):
    """`/price` drops the outer dict for one symbol. That shape predates the chunking and
    must survive it."""
    import td_live

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"price": "42.5"}

    monkeypatch.setattr(td_live.requests, "get",
                        lambda url, timeout=None, params=None: Resp())
    monkeypatch.setattr(td_live, "api_key", lambda: "k")
    assert td_live.fetch_prices(["SOXL"]) == {"SOXL": 42.5}


# ------------------------------------------------------- what a MEMBER publishes per name
#
# The bug: a member strategy holding `BTC/USD`, `ETH/USD` and `BNB/USD` published its
# `symbol` as those three joined with commas and nothing else per name, so the board had
# no key to build a row on. It drew ONE row headed with all three tickers, one Units figure
# that was their sum, and an em-dash wherever a per-name cost, mark or value belonged —
# then counted the rows it had and printed "1 assets" over a book holding three.
#
# The state was never missing: `_units`, `_cost` and `_last_price` have always been keyed
# on the symbol. Only the publishing was.


def member(symbols, *, cls="crypto", symbol_classes=(), allow_short=False,
           capital=10_000.0):
    """A `MemberStrategy` with no node behind it.

    Constructing one takes a config and nothing else — no trader, no venue, no clock that
    has moved — which is what lets the published shape be asserted in milliseconds.
    `export_state=False` keeps the registry and the ledger out of it; those paths have
    their own tests above and are not what these cases are about.
    """
    from member_strategy import MemberStrategy, MemberStrategyConfig
    return MemberStrategy(MemberStrategyConfig(
        registration_id="str_01_test", account="01", name="test", cls=cls, tf="1h",
        symbols=tuple(symbols), symbol_classes=tuple(symbol_classes),
        capital=capital, allow_short=allow_short, export_state=False))


class _Fill:
    """The three things `on_order_filled` reads off a Nautilus event.

    Hand-built rather than minted by an engine, because an engine needs a venue, an
    instrument, a bar and a matching pass to produce one — all of which
    `test_member_desk.py` already drives end to end. What is under test here is the
    arithmetic and the shape it publishes.
    """

    def __init__(self, instrument_id, price, qty, side):
        self.instrument_id = instrument_id
        self.order_side = type("Side", (), {"name": side})()
        self.last_px = type("Px", (), {"as_double": lambda _s: price})()
        self.last_qty = type("Qty", (), {"as_double": lambda _s: qty})()
        self.client_order_id = "unmapped"      # no ledger seq, so `deskdb` is never called
        self.ts_event = 0
        self.trade_id = "t1"


def fill(strategy, symbol, price, qty, side="BUY"):
    strategy.on_order_filled(
        _Fill(strategy._instrument_id(symbol), price, qty, side))


def rows_by_symbol(strategy):
    return {h["symbol"]: h for h in strategy.holdings()}


def test_a_member_publishes_one_row_per_registered_symbol():
    """Three names is three rows, and the untraded one is a ROW and not an absence.

    "BNB/USD, flat" is a fact — the member registered it and the desk is watching it.
    Publishing only the names with a position would say this strategy holds one
    instrument when it holds three, which is the same silent narrowing that made the
    board print "1 assets" in the first place.
    """
    s = member(["BTC/USD", "ETH/USD", "BNB/USD"])
    fill(s, "ETH/USD", 2_000.0, 2.0)

    holdings = s.holdings()
    assert [h["symbol"] for h in holdings] == ["BTC/USD", "ETH/USD", "BNB/USD"]

    never = rows_by_symbol(s)["BNB/USD"]
    assert never["state"] == "flat" and never["units"] == 0.0
    assert never["entry"] is None and never["mark"] is None
    assert never["trades"] == 0
    assert never["pnl_pct"] is None, "a flat name has no return; 0.00% reads as 'tried'"
    assert never["warming"] is True, "the desk has seen no price for it"


def test_the_fill_lands_on_the_name_that_traded():
    """One fill on ETH is one trade on the ETH row and none on the other two.

    The old row carried the strategy's whole fill count against the joined symbol string,
    so the same `1` was reported for a book of three names whichever one had traded.
    """
    s = member(["BTC/USD", "ETH/USD", "BNB/USD"])
    fill(s, "ETH/USD", 2_000.0, 2.0)

    got = rows_by_symbol(s)
    assert [got[k]["trades"] for k in ("BTC/USD", "ETH/USD", "BNB/USD")] == [0, 1, 0]
    assert got["ETH/USD"]["units"] == 2.0
    assert got["ETH/USD"]["value"] == pytest.approx(4_000.0)
    assert s.held_count() == 1, "one name has a position, whatever the units sum to"


def test_avg_cost_is_the_average_and_not_the_opening_fill():
    """Scaled into over two fills, the row prices what is HELD.

    Same definition as `fill_pnl` and as `BookStrategy._entry`; an opening-price basis
    misreports every name the strategy added to.
    """
    s = member(["ETH/USD"])
    fill(s, "ETH/USD", 2_000.0, 1.0)
    fill(s, "ETH/USD", 3_000.0, 1.0)
    # The mark is set here rather than left to the fills. `on_order_filled` SEEDS
    # `_last_price` and does not overwrite it — marking is `on_bar`'s job — so a strategy
    # driven by fills alone is still marked at the first one.
    s._last_price["ETH/USD"] = 3_000.0

    row = rows_by_symbol(s)["ETH/USD"]
    assert row["entry"] == pytest.approx(2_500.0)
    assert row["trades"] == 2
    assert row["pnl_pct"] == pytest.approx(20.0), "+20% on a $2,500 basis, not on $2,000"


def test_a_short_that_fell_reads_as_a_gain():
    """Colour on this page means gained or lost and nothing else.

    A book is long/flat by construction, so `price / entry - 1` is unconditionally right
    for it. A member with `allow_short` can be the other way round, and the unsigned form
    would paint a profitable short red.
    """
    s = member(["ETH/USD"], allow_short=True)
    fill(s, "ETH/USD", 2_000.0, 1.0, side="SELL")
    s._last_price["ETH/USD"] = 1_800.0

    row = rows_by_symbol(s)["ETH/USD"]
    assert row["state"] == "short"
    assert row["pnl_pct"] == pytest.approx(10.0), "down 10% on a short is up 10%"


def test_a_member_holding_two_classes_still_publishes_one_row_per_name():
    """A registration may hold instruments from several asset classes since 2026-08-29.

    The two names settle on two venues and are fed by the same vendor split, and the row
    carries no class of its own on purpose — that disclosure travels once, on `classes`,
    rather than being copied onto every row where it can go stale separately.
    """
    s = member(["SPY", "BTC/USD"], cls="us_etfs",
               symbol_classes=(("SPY", "us_etfs"), ("BTC/USD", "crypto")))
    fill(s, "BTC/USD", 60_000.0, 0.1)

    got = rows_by_symbol(s)
    assert sorted(got) == ["BTC/USD", "SPY"]
    assert got["BTC/USD"]["units"] == pytest.approx(0.1)
    assert got["SPY"]["state"] == "flat" and got["SPY"]["trades"] == 0
    assert s.classes() == ["crypto", "us_etfs"], "both classes are disclosed on the row"
    assert not any("cls" in h for h in s.holdings())


def test_a_member_row_has_the_same_fields_a_book_row_has():
    """One shape, so the board needs ONE renderer.

    Both kinds are rows on one table with one header, and a field that exists on only one
    of them is a column that silently blanks for the other. Asserted against
    `BookStrategy.holdings()` itself rather than against a copied list, so the two cannot
    drift apart without this failing.
    """
    from book_strategy import BookStrategy, BookStrategyConfig

    b = BookStrategy(BookStrategyConfig(
        rule="ibs", symbols=("AAPL", "MSFT"), export_state=False))
    m = member(["BTC/USD", "ETH/USD"])

    assert set(m.holdings()[0]) == set(b.holdings()[0])
