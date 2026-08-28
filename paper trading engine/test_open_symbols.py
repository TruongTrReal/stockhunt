"""The widened universe, and the door that lets a symbol in from outside it.

Two things are under test and they fail in opposite directions, which is why they are in
one file:

* **Stage 1, the roster.** Each leg now holds its whole research class. The risk is
  quiet — a leg that drifts from the sheet it selects from, or two legs claiming one
  instrument, and neither shows up as an error anywhere.
* **Stage 2, the door.** A symbol outside those legs is resolved rather than refused. The
  risk here is loud in one direction and catastrophic in the other: refusing a real
  instrument is an inconvenience, and admitting somebody else's is the failure the root
  `CLAUDE.md` devotes a section to.

Nothing here reaches a vendor. `symbol_resolve._vendor_quote` is the one function that
does, and it exists as a separate function so this suite can replace it — everything above
it is arithmetic on strings and everything below it is a vendor's answer, and the join
between them is what has to be tested.

    ..\\.venv\\Scripts\\python -m pytest test_open_symbols.py -q
"""

from __future__ import annotations

import pytest

import paper_config
import symbol_resolve
import td_live
import venue_instruments

import config as bt_config


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def clean_registry():
    """Undo anything a test admits.

    `paper_config.CLASS_OF` is process-wide mutable state now — that is the whole point of
    `admit` — so a test that leaves a symbol behind changes what the next one is testing.
    `SAFE_TO_VENDOR` is restored with it, because the two are written together and a
    half-restored pair is worse than neither.
    """
    classes = dict(paper_config.CLASS_OF)
    vendor = dict(paper_config.SAFE_TO_VENDOR)
    yield
    paper_config.CLASS_OF.clear()
    paper_config.CLASS_OF.update(classes)
    paper_config.SAFE_TO_VENDOR.clear()
    paper_config.SAFE_TO_VENDOR.update(vendor)


@pytest.fixture
def live_node():
    """A built (never connected) `TradingNode`, with an event loop it can actually find.

    `TradingNode.__init__` calls `asyncio.get_event_loop()`, and nautilus refuses to
    CREATE one when `pytest` is in `sys.modules` — a deliberate guard against leaking a
    loop per test. Whether one already exists then depends on which tests ran before this
    file, so without this fixture the node tests pass alone and fail in the folder suite,
    which is the worst way for a test to be wrong.

    `build_node` connects to nothing — it constructs the clients and their exchanges and
    stops, which is exactly what `run_paper.py --dry-run` relies on.
    """
    import asyncio
    import run_paper

    previous = None
    try:
        previous = asyncio.get_event_loop_policy().get_event_loop()
    except Exception:                     # noqa: BLE001 - "there was none" is the answer
        previous = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    venue_instruments.clear()
    node, _ = run_paper.build_node([], allow_short=False, log_level="ERROR")
    try:
        yield node
    finally:
        node.dispose()
        venue_instruments.clear()
        loop.close()
        asyncio.set_event_loop(previous if previous and not previous.is_closed() else None)


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """A recording stand-in for the vendor, and a cache that starts empty.

    Returns a list the test appends answers to and reads calls from, so both halves of a
    probe — what was asked and what was answered — are assertable.
    """
    monkeypatch.setattr(symbol_resolve, "CACHE_PATH", tmp_path / "probe.json")
    monkeypatch.setattr(symbol_resolve, "_cache", None)
    calls: list[tuple[str, str | None]] = []
    answers: dict[str, dict] = {}

    def fake(symbol, country=None):
        calls.append((symbol, country))
        if symbol not in answers:
            raise AssertionError(f"the test did not stage an answer for {symbol}")
        return answers[symbol]

    monkeypatch.setattr(symbol_resolve, "_vendor_quote", fake)
    return type("Probe", (), {"calls": calls, "answers": answers})()


# ------------------------------------------------------------------ stage 1: the roster
def test_each_leg_holds_its_research_class():
    """A leg selects rules from `wf_summary_<cls>_<tf>.csv`, which ranks over that class's
    research universe. A leg narrower than its sheet is a forward test of a sample the
    sheet never scored, and nothing anywhere records the difference."""
    assert set(bt_config.US_STOCKS) <= set(paper_config.UNIVERSE["us_stocks"])
    assert set(bt_config.ETF_TOP10) <= set(paper_config.UNIVERSE["us_etfs"])
    assert set(bt_config.CRYPTO_TOP20) == set(paper_config.UNIVERSE["crypto"])
    assert set(bt_config.CLASSES["commodities"]["symbols"]) == set(
        paper_config.UNIVERSE["commodities"])
    assert len(paper_config.UNIVERSE["cme_futures"]) == len(bt_config.CME_FUTURES)


def test_the_legs_are_disjoint():
    """`CLASS_OF` is a reverse lookup and decides the venue, the instrument shape and the
    sheet. A symbol on two legs resolves to whichever was declared last and reads on the
    board as two systems agreeing when it is one asset counted twice.

    `paper_config` raises at import if this is violated, so this asserts the property
    rather than the guard — a future refactor could drop the loop and keep the file
    importable."""
    seen: dict[str, str] = {}
    for cls, syms in paper_config.UNIVERSE.items():
        for s in syms:
            assert s not in seen, f"{s} is in both {seen.get(s)} and {cls}"
            seen[s] = cls


def test_spy_is_on_the_etf_leg_and_only_there():
    """The one collision the widening forced. SPY was a `BRIEF_EQUITY` on the us_stocks
    leg and is in `ETF_TOP10`; taking both classes whole put it on two. It is settled to
    the ETF leg because that is the sheet that scores it — `wf_summary_us_etfs_*` ranks
    SPY and `wf_summary_us_stocks_*` does not."""
    assert paper_config.CLASS_OF["SPY"] == "us_etfs"
    assert "SPY" not in paper_config.RESEARCH_EQUITIES
    assert "SPY" not in paper_config.BRIEF_EQUITIES


def test_the_leveraged_transfer_test_survived_the_widening():
    """SOXL and TQQQ are in no research universe — they are path-dependent derivatives
    that `universe_screen.py` excludes, and they are on the desk as a TRANSFER test. A
    widening that swept them away would have ended a live forward record silently."""
    assert paper_config.CLASS_OF["SOXL"] == "us_stocks"
    assert paper_config.CLASS_OF["TQQQ"] == "us_stocks"


def test_xlk_is_kept_although_the_screen_dropped_it():
    """`universe_screen.py` cut XLK at 19.8 tradable years against a 20-year gate, and the
    desk holds a forward record on it. Retiring an instrument ends that record rather than
    pausing it, which is worse than the defect keeping it preserves."""
    assert paper_config.CLASS_OF["XLK"] == "us_etfs"
    assert "XLK" not in bt_config.ETF_TOP10


def test_a_bare_futures_root_is_still_an_equity():
    """`CL` is Colgate-Palmolive on the us_stocks leg and `CL.v.0` is WTI on GLBX. They
    are different strings and must stay different symbols; the day they collide, one of
    them is marked from the wrong vendor."""
    assert paper_config.CLASS_OF["CL"] == "us_stocks"
    assert paper_config.CLASS_OF["CL.v.0"] == "cme_futures"


# ------------------------------------------------------------------- stage 2: admitting
def test_admit_puts_a_symbol_on_every_map_that_reads_one(clean_registry):
    """`CLASS_OF` decides the venue and the vendor split; `SAFE_TO_VENDOR` turns the
    Nautilus symbol back into the vendor's spelling. A pair admitted without the second
    is asked of Twelve Data as `LTCUSD`, which is not an instrument — and the vendor
    answers an empty frame rather than an error, so the book warms up forever."""
    paper_config.admit("ARKK", "us_etfs")
    paper_config.admit("SHIB/USD", "crypto")
    assert paper_config.CLASS_OF["ARKK"] == "us_etfs"
    assert paper_config.SAFE_TO_VENDOR["SHIBUSD"] == "SHIB/USD"
    assert paper_config.open_symbols() == {"ARKK": "us_etfs", "SHIB/USD": "crypto"}


def test_admit_is_idempotent_but_refuses_a_second_class(clean_registry):
    """Two members naming the same symbol is ordinary; the same symbol on two legs is the
    disjointness failure, arriving at runtime instead of at import."""
    paper_config.admit("ARKK", "us_etfs")
    paper_config.admit("ARKK", "us_etfs")          # no-op, not an error
    with pytest.raises(RuntimeError, match="already trades on this desk"):
        paper_config.admit("ARKK", "us_stocks")


def test_admit_refuses_a_pinned_symbol_for_another_class(clean_registry):
    with pytest.raises(RuntimeError, match="already trades on this desk"):
        paper_config.admit("SPY", "us_stocks")


def test_the_open_ceiling_is_a_feed_budget(clean_registry, monkeypatch):
    """Each open symbol is one vendor request per bar. Sixty member strategies naming
    twenty symbols each is 1,200 names nobody decided on, which at 5m is 240 requests a
    minute against a 610/minute budget — so the ceiling is enforced rather than assumed,
    and it counts SYMBOLS because that is what a subscription is keyed on."""
    monkeypatch.setattr(paper_config, "MAX_OPEN_SYMBOLS", 2)
    paper_config.admit("ARKK", "us_etfs")
    paper_config.admit("ARKQ", "us_etfs")
    with pytest.raises(RuntimeError, match="pinned universe"):
        paper_config.admit("ARKW", "us_etfs")
    # A symbol already admitted still attaches at the ceiling — the cost was paid once.
    paper_config.admit("ARKK", "us_etfs")


def test_admit_refuses_an_unknown_class(clean_registry):
    with pytest.raises(RuntimeError, match="unknown asset class"):
        paper_config.admit("ARKK", "us_bonds")


# ------------------------------------------------------------------- stage 2: the shape
def test_a_bare_root_is_not_a_futures_contract():
    """The single most dangerous confusion on this desk. `CL` at Twelve Data is
    Colgate-Palmolive and `ES` is Eversource Energy, returned as clean, plausible,
    entirely wrong series — so the shape is checked before anybody is asked anything."""
    r = symbol_resolve.resolve("HE", "cme_futures")
    assert not r.ok
    assert "not a CME continuous contract" in r.reason


def test_an_unknown_root_is_refused():
    r = symbol_resolve.resolve("ZZ.v.0", "cme_futures")
    assert not r.ok
    assert "not a CME root" in r.reason


def test_a_real_root_outside_the_desk_resolves_without_a_vendor(probe):
    """Futures are resolved offline against `futures_specs`, and the assertion that
    matters is `probe.calls == []`: Twelve Data carries no CME contract at all and must
    never be asked about one."""
    r = symbol_resolve.resolve("MES.v.0", "cme_futures")
    assert r.ok
    assert r.detail["vendor"] == "databento"
    assert probe.calls == []


def test_a_pair_cannot_be_registered_as_an_equity():
    # A pair the desk does NOT already hold, so the shape rule is what refuses it. Using a
    # pinned name like `DOGE/USD` would pass for the wrong reason — the disjointness check
    # fires first and this test would keep passing with the shape rule deleted.
    r = symbol_resolve.resolve("SHIB/USD", "us_stocks")
    assert not r.ok
    assert "not an equity ticker" in r.reason


def test_an_equity_cannot_be_registered_as_a_pair():
    r = symbol_resolve.resolve("ARKK", "crypto")
    assert not r.ok
    assert "not a quoted pair" in r.reason


# ------------------------------------------------------------------ stage 2: the vendor
def test_an_equity_probe_pins_the_country(probe):
    """The pin is the whole defence, so the test is on the REQUEST and not only on the
    verdict. Without `country=United States` the vendor does not fail — it returns a
    different company that shares the ticker, as a full and structurally perfect series
    that every bar-level check in this repo passes."""
    probe.answers["ARKK"] = {"symbol": "ARKK", "name": "ARK Innovation ETF",
                             "exchange": "CBOE", "currency": "USD"}
    r = symbol_resolve.resolve("ARKK", "us_etfs")
    assert r.ok
    assert probe.calls == [("ARKK", "United States")]
    assert "ARK Innovation ETF" in r.reason


def test_no_us_listing_is_refused_with_the_reason(probe):
    """CTRA is the case the guard is named after: no US listing, and unpinned the vendor
    answers Ciputra Development Tbk PT on the Indonesia Stock Exchange."""
    probe.answers["CTRA"] = {"status": "error", "code": 404,
                             "message": "**symbol** or **figi** parameter is missing."}
    r = symbol_resolve.resolve("CTRA", "us_stocks")
    assert not r.ok
    assert "no US listing" in r.reason


def test_a_foreign_namesake_is_refused_on_its_currency(probe):
    """A second lock on the same door, and it fires only if the pin is ever dropped or
    ignored: a namesake resolved on a foreign venue is quoted in that venue's money, and
    STJ in pence and K in Canadian dollars are what the cache was full of."""
    probe.answers["STJ"] = {"symbol": "STJ", "name": "St. James's Place Plc",
                            "exchange": "LSE", "currency": "GBp"}
    r = symbol_resolve.resolve("STJ", "us_stocks")
    assert not r.ok
    assert "foreign namesake" in r.reason


def test_a_pair_is_not_asked_for_a_country(probe):
    """Twelve Data returns nothing at all for a country-pinned FX or crypto pair, so the
    pin is per class rather than global — the same split `td_loader.US_LISTED_CLASSES`
    makes."""
    probe.answers["SHIB/USD"] = {"symbol": "SHIB/USD", "name": "SHIBA INU US Dollar",
                                 "exchange": "Coinbase Pro"}
    assert symbol_resolve.resolve("SHIB/USD", "crypto").ok
    assert probe.calls == [("SHIB/USD", None)]


def test_a_metal_cannot_be_registered_as_crypto(probe):
    """`XAU/USD` and `BTC/USD` are spelled alike and settle on different venues. Routing
    on the separator once priced a metal against the Binance book; the vendor's own
    `exchange` field is what separates them, and it is checked rather than inferred."""
    probe.answers["XPD/EUR"] = {"symbol": "XPD/EUR", "name": "Palladium Spot / Euro",
                                "exchange": "Forex"}
    r = symbol_resolve.resolve("XPD/EUR", "crypto")
    assert not r.ok
    assert "spot/FX pair rather than a coin" in r.reason


def test_a_coin_cannot_be_registered_as_a_commodity(probe):
    probe.answers["SHIB/USD"] = {"symbol": "SHIB/USD", "name": "SHIBA INU US Dollar",
                                 "exchange": "Coinbase Pro"}
    r = symbol_resolve.resolve("SHIB/USD", "commodities")
    assert not r.ok
    assert "rather than as a spot/FX pair" in r.reason


def test_an_unreachable_vendor_fails_closed(probe, monkeypatch):
    """An unreachable vendor is not evidence that a ticker is real. Admitting one on that
    basis is how a symbol nobody verified ends up with an instrument, a subscription and
    a position."""
    def boom(symbol, country=None):
        raise TimeoutError("read timed out")
    monkeypatch.setattr(symbol_resolve, "_vendor_quote", boom)
    r = symbol_resolve.resolve("ARKK", "us_etfs")
    assert not r.ok
    assert "refused rather than guessed" in r.reason


def test_a_failure_never_carries_the_api_key(probe, monkeypatch):
    """`requests` puts the full request URL in its exception messages and this module's
    URLs carry `apikey=` in the query string. Unscrubbed, a timeout published the desk's
    Twelve Data credential into a registration's `reason`, onto the manager console and
    into the desk log — caught on the first live run of `symbol_resolve.py`."""
    monkeypatch.setattr(td_live, "api_key", lambda: "SECRETKEY123")

    def boom(symbol, country=None):
        raise TimeoutError("GET https://api.twelvedata.com/quote?apikey=SECRETKEY123")
    monkeypatch.setattr(symbol_resolve, "_vendor_quote", boom)
    r = symbol_resolve.resolve("ARKK", "us_etfs")
    assert "SECRETKEY123" not in r.reason
    assert "***" in r.reason


# ------------------------------------------------------------------- stage 2: the cache
def test_a_verdict_is_cached_and_the_vendor_asked_once(probe):
    """`resolve` runs inside `desk_control.tick()`, the desk's control plane at a
    one-second cadence. One network round trip per attach is acceptable; one per tick is
    not."""
    probe.answers["ARKK"] = {"symbol": "ARKK", "name": "ARK Innovation ETF",
                             "exchange": "CBOE", "currency": "USD"}
    assert symbol_resolve.resolve("ARKK", "us_etfs").ok
    again = symbol_resolve.resolve("ARKK", "us_etfs")
    assert again.ok and again.cached
    assert len(probe.calls) == 1


def test_a_refusal_is_cached_too(probe):
    """A member who mistypes should not spend a credit on every control tick until they
    notice. Both verdicts expire on `PROBE_TTL_SECONDS` for the same reason: a "no"
    outlives the listing that would make it a "yes", and a "yes" outlives a delisting."""
    probe.answers["NOPE"] = {"status": "error", "message": "invalid"}
    assert not symbol_resolve.resolve("NOPE", "us_stocks").ok
    assert not symbol_resolve.resolve("NOPE", "us_stocks").ok
    assert len(probe.calls) == 1


def test_a_stale_verdict_is_re_probed(probe, monkeypatch):
    probe.answers["ARKK"] = {"symbol": "ARKK", "name": "ARK Innovation ETF",
                             "exchange": "CBOE", "currency": "USD"}
    symbol_resolve.resolve("ARKK", "us_etfs")
    monkeypatch.setattr(symbol_resolve, "PROBE_TTL_SECONDS", -1)
    symbol_resolve.resolve("ARKK", "us_etfs")
    assert len(probe.calls) == 2


def test_a_pinned_symbol_never_reaches_the_vendor(probe):
    """A name the desk was configured with was decided by a human editing `UNIVERSE`.
    Re-litigating that against a vendor at registration time would let one bad `/quote`
    refuse a symbol the desk is holding a position in."""
    r = symbol_resolve.resolve("SPY", "us_etfs")
    assert r.ok
    assert probe.calls == []


# ------------------------------------------------------- stage 2: reaching a live venue
def test_publish_is_a_no_op_without_a_running_venue():
    """Every process that is not the live desk — `backtest_paper.py`, this suite,
    `catalog.py` — has no exchange to tell. The instrument is in the cache either way,
    which is what the exchange reads, so a False is a missed optimisation and never a
    missing instrument."""
    venue_instruments.clear()
    inst = _instrument("ARKK", "us_etfs", "SANDBOX")
    assert venue_instruments.publish(inst) is False


def test_publish_reaches_the_right_venue_only():
    """One process runs SANDBOX, BINANCE, SPOT and GLBX. Handing a BINANCE instrument to
    the SANDBOX exchange would price it on a book it does not belong to — the same venue
    filter `run_paper.route_bars_to_sandbox` keeps on bars."""
    venue_instruments.clear()
    sandbox, binance = [], []
    venue_instruments.register("SANDBOX", sandbox.append)
    venue_instruments.register("BINANCE", binance.append)
    assert venue_instruments.publish(_instrument("ARKK", "us_etfs", "SANDBOX"))
    assert venue_instruments.publish(_instrument("SHIB/USD", "crypto", "BINANCE"))
    assert [str(i.id) for i in sandbox] == ["ARKK.SANDBOX"]
    assert [str(i.id) for i in binance] == ["SHIBUSD.BINANCE"]
    venue_instruments.clear()


def test_a_venue_refusing_an_instrument_is_raised():
    """An exchange rejecting an instrument is telling the desk this symbol cannot trade
    there. Swallowing it would attach a strategy whose every order is refused."""
    venue_instruments.clear()

    def refuse(_inst):
        raise ValueError("invalid for this venue")
    venue_instruments.register("SANDBOX", refuse)
    with pytest.raises(ValueError):
        venue_instruments.publish(_instrument("ARKK", "us_etfs", "SANDBOX"))
    venue_instruments.clear()


def _instrument(symbol, cls, venue):
    import td_nautilus
    return td_nautilus.instrument_for(symbol, cls, venue)


def test_a_real_sandbox_exchange_needs_telling_and_says_nothing_when_it_was_not(live_node):
    """The measurement the whole runtime path rests on, against the real adapter.

    `SimulatedExchange.process_bar` builds its matching engine by looking the instrument
    up in the cache. For one it has never heard of it raises
    `RuntimeError: No matching engine found` — **inside**
    `run_paper.route_bars_to_sandbox`'s forwarding handler, which catches everything so
    that one malformed bar cannot kill the feed. So the symptom of getting this wrong is
    not an error: it is a book that receives bars, marks nothing, fills nothing, and reads
    healthy in every log line.

    This asserts both halves — that the raise happens, and that publishing removes it —
    because a test of only the second would pass with the whole mechanism deleted.

    """
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.objects import Price, Quantity

    exchange = next(
        c.exchange for c in live_node.kernel.exec_engine._clients.values()
        if getattr(c, "exchange", None) is not None and str(c.venue) == "SANDBOX")
    # A ticker in no universe, no cache and no research table, so nothing can have seeded
    # it by accident.
    inst = _instrument("ZZ-OPEN-TEST", "us_etfs", "SANDBOX")
    bar = Bar(BarType.from_str(f"{inst.id}-{paper_config.BAR_SPEC['1d']}"),
              Price.from_str("100.00"), Price.from_str("101.00"),
              Price.from_str("99.00"), Price.from_str("100.50"),
              Quantity.from_int(1_000_000), 0, 0)

    with pytest.raises(RuntimeError, match="No matching engine"):
        exchange.process_bar(bar)

    live_node.kernel.cache.add_instrument(inst)
    exchange.add_instrument(inst)
    exchange.process_bar(bar)                      # no raise: the engine exists now
    assert str(inst.id) in {str(k) for k in exchange.get_matching_engines()}


def test_building_a_node_registers_every_sandbox_venue(live_node):
    """`desk_control` cannot find these for itself — a Nautilus `Controller` is an `Actor`
    and has no route to the execution engine at all — so `route_bars_to_sandbox` registers
    them on its way past. If it stops, `publish` silently returns False for every open
    symbol and the failure above comes back."""
    assert set(venue_instruments.venues()) == set(paper_config.VENUES.values())


# ------------------------------------------------------------------ stage 2: the clocks
def test_class_of_answers_for_a_symbol_the_research_does_not_know(clean_registry):
    """`td_live.class_of` decides which TIMEZONE a bar is stamped in, and reading a
    Sydney-stamped commodity bar as UTC put that whole leg permanently one bar behind for
    as long as it existed. It consults the desk's legs first because they and the research
    disagree in both directions: XLK is on this desk and not in `bt_config.US_ETFS`, and a
    symbol admitted at runtime is in no research universe by construction."""
    assert td_live.class_of("XLK") == "us_etfs"
    paper_config.admit("ARKK", "us_etfs")
    assert td_live.class_of("ARKK") == "us_etfs"
    assert td_live.class_of("ZZ-NOT-A-SYMBOL") is None
