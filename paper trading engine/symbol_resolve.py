"""Is this string an instrument, and is it the one the registration means?

The desk used to answer that by containment: a symbol was tradable if
`paper_config.CLASS_OF` held it, and refused otherwise. That is a correct answer to a
question nobody asked — a pinned roster says which names the desk was *configured* with,
not which names exist — and it is why a member could not trade `ARKK`.

Removing the containment check without putting anything in its place would be much worse
than leaving it, and this module is what goes in its place.

**A bare ticker is not an identity.** Twelve Data resolves an unqualified symbol against
every venue it carries, and where it has no US listing it does not answer "no" — it
answers with somebody else, as a full, internally consistent, structurally perfect series
that passes every bar-level check this repo has:

    CTRA -> Ciputra Development Tbk PT   Indonesia Stock Exchange, rupiah
    STJ  -> St. James's Place Plc        LSE, pence
    K    -> Kinross Gold Corporation     TSX, Canadian dollars

85 of 739 cached `us_stocks` series were a foreign namesake for their whole length before
`td_loader.US_LISTED_CLASSES` pinned `country=United States` at the source, and `CTRA`
had ranked as the 3rd largest US stock of 2026 on the strength of rupiah dollar volume.
An open symbol path that hands a string to the vendor reopens exactly that hole, one
registration at a time, with no sweep to notice it afterwards.

So resolution here is three things in order, and the first two cost nothing:

    1. SHAPE.   A class has a spelling. `cme_futures` is `ROOT.v.N` and its root must be a
                real CME contract; the pair classes carry exactly one `/`; the equity
                classes carry neither. Shape alone refuses `CL` as a future (it is
                Colgate-Palmolive at Twelve Data) and `ES.v.0` as an equity.
    2. VENDOR.  The class decides which vendor may be asked and how. Equities and ETFs are
                asked with `country=United States` pinned, exactly as `td_loader` does, so
                the vendor errors instead of substituting. **`cme_futures` is never asked
                Twelve Data at all** — it has no CME contract, and `ES` there is Eversource
                Energy.
    3. CACHE.   The verdict is written to `state/symbol_probe.json` with a timestamp. The
                probe runs inside `desk_control.tick()`, which is the desk's whole control
                plane at a one-second cadence, so a network round trip per attach is
                acceptable and one per bar is not.

`check_data.py --probe-listing` is the same idea one pipeline over: ask the vendor whether
a ticker is even the US company, and cache the verdict so the offline path can apply it.
This is that question asked at registration time instead of at sweep time.

**A refusal is a sentence, never a bare False.** It lands in the registration's `reason`
column, which is what its owner is looking at. A wrong instrument that trades is far worse
than a registration that was declined, and a decline nobody can act on is barely better.

Run::

    python symbol_resolve.py us_etfs ARKK              # one verdict, live, with detail
    python symbol_resolve.py us_stocks CTRA PLTR K     # the identity guard, demonstrated
    python symbol_resolve.py --cached crypto LTC/USD   # offline: what is already known
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import paper_config

# Where the verdicts live. Beside `alpaca.db` and `desk.db` rather than in
# `data/reference/`, and the difference is what the file IS: the reference tables are the
# one part of `data/` that no re-run reconstructs, while this is a cache of answers the
# vendor will give again. Losing it costs one credit per symbol.
CACHE_PATH = paper_config.HERE / "state" / "symbol_probe.json"

# How long a verdict is reused before the vendor is asked again.
#
# Both directions go stale and neither is safe to pin forever. A "no" outlives the listing
# that would have made it a "yes" — a symbol that IPO'd yesterday would be refused for the
# life of the desk. A "yes" outlives a delisting, and a stale yes is the worse of the two,
# because it admits a name whose bars have stopped and leaves the silence for
# `DeskController._watch_feeds` to notice hours later.
#
# One day, for both. It costs one credit per open symbol per day against a 610/minute
# budget, which is not a number worth optimising, and it bounds how long either mistake
# can stand.
PROBE_TTL_SECONDS = 24 * 60 * 60

# Twelve Data's own timeout for this probe, and it is shorter than `td_live`'s 30s on
# purpose. `resolve` is called from `desk_control._launch`, which runs inside `tick()` —
# the desk's control plane, at a one-second cadence, draining every member's orders. A
# vendor that has stopped answering must cost the desk one slow tick, not thirty seconds
# of every registration and order in the queue waiting behind a socket.
PROBE_TIMEOUT = 10

# Databento's continuous-contract symbology: root, roll rule, rank. Restated rather than
# imported from `paper api/api_symbols.py`, which is the same expression for the same
# reason — that process imports no trading code, and this module is trading code.
CONTINUOUS = re.compile(r"^([A-Z0-9]{1,6})\.([a-z])\.(\d{1,2})$")

EQUITY_CLASSES = ("us_stocks", "us_etfs")
PAIR_CLASSES = ("crypto", "commodities")

# What Twelve Data calls the venue for a spot metal, an energy spot or an FX cross. It is
# the field that separates the two pair classes, and it has to be checked rather than
# assumed from the separator: `XAU/USD` and `BTC/USD` are spelled alike, and routing on
# the `/` alone is what once priced a metal against the Binance book. Measured
# 2026-08-28 — XAU, XAG, WTI and EUR all answer `Forex`; BTC, ETH, LTC and DOT answer
# `Binance`.
FOREX_EXCHANGE = "Forex"


@dataclass
class Resolution:
    """What the desk decided about one symbol, and everything it decided it from."""
    symbol: str
    asset_class: str
    ok: bool
    reason: str = ""
    # The vendor's own words for what this ticker is. Kept on a SUCCESS as well as on a
    # failure: "ARKK resolved to ARK Innovation ETF on CBOE" is the sentence that lets
    # somebody confirm the desk admitted the instrument they meant.
    detail: dict[str, Any] = field(default_factory=dict)
    # Whether this came off the cache rather than off the wire, so a caller reporting a
    # verdict can say how old it is.
    cached: bool = False

    def __str__(self) -> str:
        head = "OK " if self.ok else "REFUSED"
        return f"{head} {self.symbol} ({self.asset_class}): {self.reason}"


# ------------------------------------------------------------------------ the cache
_cache: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:            # noqa: BLE001 - absence and corruption are both "empty"
            _cache = {}
    return _cache


def _save() -> None:
    """Write the cache, and never let failing to do so refuse a symbol.

    A read-only state directory is a reason to re-probe next time, not a reason to
    decline a registration that the vendor has just confirmed.
    """
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(_load(), indent=1, sort_keys=True),
                              encoding="utf-8")
    except Exception:                # noqa: BLE001 - a cache that cannot be written is a cache
        pass


def cached(symbol: str, asset_class: str) -> Resolution | None:
    """The stored verdict for this (symbol, class), if it is still inside the TTL."""
    row = _load().get(f"{asset_class}|{symbol}")
    if not row:
        return None
    if time.time() - float(row.get("at") or 0) > PROBE_TTL_SECONDS:
        return None
    return Resolution(symbol=symbol, asset_class=asset_class, ok=bool(row.get("ok")),
                      reason=str(row.get("reason") or ""),
                      detail=dict(row.get("detail") or {}), cached=True)


def _remember(r: Resolution) -> Resolution:
    _load()[f"{r.asset_class}|{r.symbol}"] = {
        "ok": r.ok, "reason": r.reason, "detail": r.detail, "at": time.time()}
    _save()
    return r


# ------------------------------------------------------------------------ the vendor
def _vendor_quote(symbol: str, country: str | None = None) -> dict:
    """One Twelve Data `/quote`, as a dict. The one place this module touches a network.

    Factored out so a test can replace it. Everything above it is arithmetic on strings
    and everything below it is a vendor's answer, and a unit suite must be able to assert
    on the join without a key or a socket.

    `country` is passed through rather than decided here, because the caller is the only
    thing that knows whether pinning it is right: on an equity it is the whole defence,
    and on `LTC/USD` it returns nothing at all.

    **`raise_for_status` is deliberately not called, and leaving it in got the reason
    wrong on the first run.** Twelve Data answers a ticker it has no listing for with
    **HTTP 404 and a JSON body** — which is the ANSWER, not a transport failure. Raising
    on the status turned the identity guard's whole finding into "could not check CTRA,
    try again once the vendor answers", so the desk would have told a member its symbol
    was unverifiable when the vendor had in fact said, clearly, that no US listing exists.
    A non-JSON body is the real transport failure and is reported as one.
    """
    import requests
    import td_live

    params = {"symbol": symbol, "apikey": td_live.api_key()}
    if country:
        params["country"] = country
    r = requests.get(f"{td_live.BASE_URL}/quote", params=params, timeout=PROBE_TIMEOUT)
    try:
        payload = r.json()
    except ValueError:
        return {"status": "error",
                "message": f"the vendor answered HTTP {r.status_code} with no JSON"}
    return payload if isinstance(payload, dict) else {}


def _scrub(text: str) -> str:
    """Take the API key out of anything about to be shown to a person.

    `requests` puts the full request URL in its exception messages, and this module's
    URLs carry `apikey=` in the query string — so an unscrubbed `TimeoutError` published
    the desk's Twelve Data credential into a registration's `reason` column, onto the
    manager console and into the desk log. Caught on the first live run of the CLI below.

    Best effort by design: if the key cannot be read there is nothing in the text to
    remove, and failing to scrub must not turn a refusal into a crash.
    """
    try:
        import td_live
        key = td_live.api_key()
    except Exception:                # noqa: BLE001 - nothing to hide is a valid outcome
        return text
    return text.replace(key, "***") if key else text


def _vendor_error(q: dict) -> str:
    """The vendor's own complaint, trimmed to something a person will read."""
    msg = str(q.get("message") or "").split(". Please provide")[0]
    return msg or "the vendor returned nothing usable"


# ------------------------------------------------------------------- the shape rules
def _shape_problem(symbol: str, asset_class: str) -> str:
    """Why this string cannot be an instrument of this class, before anybody is asked.

    Free, and it catches the two confusions that matter most. `CL` registered as
    `cme_futures` is refused here rather than sent to a vendor that would answer
    Colgate-Palmolive; `ES.v.0` registered as `us_stocks` is refused before it can be
    built as an `Equity` on the SANDBOX venue and marked from the wrong feed.
    """
    if asset_class == "cme_futures":
        m = CONTINUOUS.match(symbol)
        if not m:
            return (f"{symbol!r} is not a CME continuous contract. This class spells its "
                    f"names ROOT.v.RANK — `ES.v.0`, `CL.v.0` — because that is Databento's "
                    f"symbology and the desk holds it verbatim. A bare root is not the "
                    f"same instrument: Twelve Data answers `CL` with Colgate-Palmolive "
                    f"and `ES` with Eversource Energy")
        root = m.group(1)
        if root not in _cme_roots():
            return (f"{root} is not a CME root this repo knows. The contracts are listed "
                    f"in `backtest engine/futures_specs.py`; a root missing from it has "
                    f"no multiplier, no tick and no evidence anybody has ever fetched it")
        return ""
    if asset_class in PAIR_CLASSES:
        if symbol.count("/") != 1 or not all(symbol.split("/")):
            return (f"{symbol!r} is not a quoted pair. {asset_class} settles one asset "
                    f"into another and is spelled BASE/QUOTE — `BTC/USD`, `XAU/USD`")
        return ""
    if "/" in symbol or CONTINUOUS.match(symbol):
        return (f"{symbol!r} is not an equity ticker. A `/` means a settled pair and a "
                f"`ROOT.v.RANK` means a continuous futures contract, and neither trades "
                f"as whole shares on the SANDBOX venue")
    return ""


_roots: frozenset[str] | None = None


def _cme_roots() -> frozenset[str]:
    """Every CME root this repo has a contract specification for.

    **This is the futures probe, and it is deliberately offline.** Databento would answer
    the same question, and asking it costs ~25 seconds of server-side symbology
    resolution — inside `tick()`, which is the desk's control plane. `futures_specs` is a
    curated table of real CME contracts, so a root in it is a real contract and a root
    outside it is one nobody here has evidence for; combined with the `ROOT.v.RANK` shape
    above, that is a confident answer at no cost. What it does not prove is that the
    ARCHIVE holds bars for the root, and nothing pretends otherwise — `db_live.have_key`
    gates the feed and `DeskController._watch_feeds` reports the silence if it is empty.
    """
    global _roots
    if _roots is None:
        import futures_specs
        _roots = frozenset(futures_specs.CME_CONTRACTS)
    return _roots


# ------------------------------------------------------------------------ the verdict
def resolve(symbol: str, asset_class: str, *, use_cache: bool = True) -> Resolution:
    """Can the desk trade this symbol as this class, and what is it?

    Returns a `Resolution` rather than raising, because both answers are ordinary and the
    caller has somewhere to put either: `desk_control._launch` turns a refusal into the
    registration's `reason` and an acceptance into `paper_config.admit`.
    """
    symbol = (symbol or "").strip()
    if not symbol:
        return Resolution(symbol=symbol, asset_class=asset_class, ok=False,
                          reason="no symbol given")
    if asset_class not in paper_config.UNIVERSE:
        return Resolution(symbol=symbol, asset_class=asset_class, ok=False,
                          reason=f"unknown asset class {asset_class!r}")

    # Already on a leg, pinned or admitted. Answered before the shape rules and before the
    # cache: a name the desk is configured with has been decided by a human editing
    # `UNIVERSE`, and re-litigating that against a vendor at registration time would let
    # one bad `/quote` refuse a symbol the desk is holding a position in.
    held = paper_config.CLASS_OF.get(symbol)
    if held == asset_class:
        return Resolution(symbol=symbol, asset_class=asset_class, ok=True,
                          reason="already on this desk's universe")
    if held is not None:
        return Resolution(
            symbol=symbol, asset_class=asset_class, ok=False,
            reason=(f"{symbol} trades on this desk as {held}, not {asset_class}. One "
                    f"instrument cannot be on two legs — it would run against two rule "
                    f"lists and read on the board as two systems agreeing"))

    problem = _shape_problem(symbol, asset_class)
    if problem:
        return Resolution(symbol=symbol, asset_class=asset_class, ok=False,
                          reason=problem)

    # Futures never reach a vendor here; the shape check above IS the probe. See
    # `_cme_roots`.
    if asset_class == "cme_futures":
        return Resolution(symbol=symbol, asset_class=asset_class, ok=True,
                          reason=f"{symbol.split('.')[0]} is a CME contract this repo "
                                 f"has a specification for",
                          detail={"vendor": "databento", "root": symbol.split(".")[0]})

    if use_cache:
        hit = cached(symbol, asset_class)
        if hit is not None:
            return hit
    return _remember(_probe(symbol, asset_class))


def _probe(symbol: str, asset_class: str) -> Resolution:
    """Ask Twelve Data what this ticker is, with the class's own pin applied."""
    country = "United States" if asset_class in EQUITY_CLASSES else None
    try:
        q = _vendor_quote(symbol, country)
    except Exception as exc:            # noqa: BLE001 - reported as a refusal, never raised
        # Fail CLOSED, and say which way. An unreachable vendor is not evidence that a
        # ticker is real, and admitting one on that basis is how a symbol nobody verified
        # ends up with an instrument, a subscription and a position.
        return Resolution(
            symbol=symbol, asset_class=asset_class, ok=False,
            reason=_scrub(f"could not check {symbol} against Twelve Data "
                          f"({type(exc).__name__}: {exc}), so it is refused rather than "
                          f"guessed. Try again once the vendor answers"))

    if str(q.get("status")) == "error" or not q.get("symbol"):
        if asset_class in EQUITY_CLASSES:
            return Resolution(
                symbol=symbol, asset_class=asset_class, ok=False,
                reason=(f"Twelve Data has no US listing for {symbol} "
                        f"({_vendor_error(q)}). The request pins "
                        f"country=United States on purpose: unpinned it would not fail, "
                        f"it would return a different company that happens to share the "
                        f"ticker — CTRA comes back as an Indonesian developer quoted in "
                        f"rupiah — and no check on the bars can tell the difference"),
                detail={"vendor": "twelvedata", "country": country})
        return Resolution(
            symbol=symbol, asset_class=asset_class, ok=False,
            reason=(f"Twelve Data does not price the pair {symbol} "
                    f"({_vendor_error(q)})"),
            detail={"vendor": "twelvedata"})

    detail = {"vendor": "twelvedata", "country": country,
              "resolves_to": q.get("name"), "exchange": q.get("exchange"),
              "mic_code": q.get("mic_code"), "currency": q.get("currency")}

    if asset_class in EQUITY_CLASSES:
        # The pin is what makes the vendor error rather than substitute, so this is a
        # second lock on the same door and not the door itself. It fires if the pin is
        # ever dropped or ignored: a namesake resolved on a foreign venue is quoted in
        # that venue's money, and STJ in pence and K in Canadian dollars are what the
        # cache was full of.
        currency = str(q.get("currency") or "USD")
        if currency.upper() != "USD":
            return Resolution(
                symbol=symbol, asset_class=asset_class, ok=False,
                reason=(f"{symbol} resolved to {q.get('name')} on {q.get('exchange')}, "
                        f"quoted in {currency} rather than USD. That is a foreign "
                        f"namesake, not the US listing"),
                detail=detail)
        return Resolution(
            symbol=symbol, asset_class=asset_class, ok=True,
            reason=(f"{symbol} is {q.get('name')} on {q.get('exchange')}, a US listing "
                    f"quoted in USD"),
            detail=detail)

    # The two pair classes are spelled alike and settle on different venues, so the
    # vendor's own answer decides which. Registering `XAU/USD` as crypto would price a
    # metal against the Binance book — the failure `td_nautilus.instrument_for` stopped
    # inferring from the separator to avoid — and registering `LTC/USD` as commodities
    # would put a coin on the SPOT venue.
    is_forex = str(q.get("exchange") or "") == FOREX_EXCHANGE
    if asset_class == "commodities" and not is_forex:
        return Resolution(
            symbol=symbol, asset_class=asset_class, ok=False,
            reason=(f"{symbol} is {q.get('name')}, which Twelve Data prices on "
                    f"{q.get('exchange')} rather than as a spot/FX pair. Register it as "
                    f"crypto — the commodities leg trades on its own SPOT venue and a "
                    f"coin priced there is on a book it does not belong to"),
            detail=detail)
    if asset_class == "crypto" and is_forex:
        return Resolution(
            symbol=symbol, asset_class=asset_class, ok=False,
            reason=(f"{symbol} is {q.get('name')}, a spot/FX pair rather than a coin. "
                    f"Register it as commodities — the crypto leg trades on the BINANCE "
                    f"venue and a metal priced there is on a book it does not belong to"),
            detail=detail)
    return Resolution(symbol=symbol, asset_class=asset_class, ok=True,
                      reason=f"{symbol} is {q.get('name')} on {q.get('exchange')}",
                      detail=detail)


def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("asset_class", choices=sorted(paper_config.UNIVERSE))
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--cached", action="store_true",
                    help="read the stored verdicts only; ask no vendor")
    args = ap.parse_args()

    worst = 0
    for symbol in args.symbols:
        if args.cached:
            r = cached(symbol, args.asset_class)
            if r is None:
                print(f"  ?       {symbol} ({args.asset_class}): nothing cached")
                continue
        else:
            r = resolve(symbol, args.asset_class, use_cache=False)
        mark = "ok " if r.ok else "NO "
        print(f"  {mark} {symbol}")
        print(f"        {r.reason}")
        if r.detail:
            print(f"        {r.detail}")
        worst = max(worst, 0 if r.ok else 1)
    raise SystemExit(worst)


if __name__ == "__main__":
    _main()
