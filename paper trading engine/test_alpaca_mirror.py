"""The mirror's loop, against a stubbed broker.

No network and no credentials. `StubAlpaca` is a book that fills market orders instantly
at a stated price, which is enough to prove the four properties that matter: it converges,
it is idempotent, it refuses to trade a closed market, and it records what a fill cost
against the mark the desk assumed.
"""

from __future__ import annotations

import json
import os
import time

import pytest

import alpaca_client
import alpaca_map
import alpaca_mirror
import alpaca_store


# ------------------------------------------------------------------ the stub

class StubAlpaca:
    """A paper account that fills every market order at `prices[symbol]`."""

    def __init__(self, equity=100_000.0, prices=None, assets=None, is_open=True):
        self._equity = equity
        self.prices = prices or {"SPY": 500.0, "QQQ": 400.0}
        self._assets = assets if assets is not None else [
            {"symbol": s, "status": "active", "tradable": True, "fractionable": True}
            for s in ("SPY", "QQQ")
        ]
        self.is_open = is_open
        self.book: dict[str, float] = {}
        self.submitted: list[dict] = []
        self._closed: list[dict] = []
        self._seen: set[str] = set()

    def account(self):
        return {"equity": str(self._equity), "account_number": "PA-TEST",
                "status": "ACTIVE", "buying_power": str(self._equity)}

    def clock(self):
        return {"is_open": self.is_open}

    def positions(self):
        return [{"symbol": s, "qty": str(q)} for s, q in self.book.items() if q]

    def assets(self, asset_class=None, status="active"):
        return list(self._assets)

    def orders(self, status="open", limit=200, after=None):
        return list(self._closed)

    def submit(self, *, symbol, side, qty, client_order_id, time_in_force="day",
               order_type="market"):
        if client_order_id in self._seen:
            # Alpaca refuses a duplicate client_order_id. So does this.
            raise alpaca_client.AlpacaError(422, "client_order_id must be unique",
                                            "POST", "/v2/orders")
        self._seen.add(client_order_id)
        key = alpaca_map.norm_key(symbol)
        px = self.prices[key if key in self.prices else symbol]
        self.book[key] = self.book.get(key, 0.0) + (qty if side == "buy" else -qty)
        order = {"id": f"srv-{len(self.submitted)}", "client_order_id": client_order_id,
                 "symbol": symbol, "side": side, "filled_qty": str(qty),
                 "filled_avg_price": str(px), "filled_at": "2026-08-27T14:00:00Z"}
        self.submitted.append(order)
        self._closed.append(order)
        return order


def book_row(cls, tf, holdings, *, equity=100_000.0, status="running"):
    return {"id": f"00:{cls}-{tf}-r", "kind": "book", "cls": cls, "tf": tf,
            "status": status, "capital": 100_000.0, "cash": 0.0, "equity": equity,
            "symbol": f"{len(holdings)} names", "units": len(holdings),
            "holdings": [{"symbol": s, "units": u, "mark": mk, "entry": None}
                         for s, u, mk in holdings]}


def snapshot(*rows):
    return {"generated_at": "2026-08-27 12:00 UTC", "strategies": list(rows)}


@pytest.fixture(autouse=True)
def store(tmp_path):
    alpaca_store.use(tmp_path / "alpaca.db")
    yield
    alpaca_store.close()


def assets_for(stub):
    return alpaca_map.asset_index(stub.assets())


# ------------------------------------------------------------------ convergence

def test_a_flat_account_is_driven_to_the_scaled_target():
    """Six $100k books hold 600 SPY between them; a $100k account should hold 100."""
    stub = StubAlpaca(equity=100_000.0)
    snap = snapshot(book_row("us_etfs", "1d", [("SPY", 600.0, 500.0)], equity=600_000.0))

    plan = alpaca_mirror.run_cycle("us_etfs", stub, snap, assets_for(stub),
                                   dry_run=False, force_hours=True)

    assert plan.ratio == pytest.approx(1 / 6)
    assert len(stub.submitted) == 1
    assert stub.submitted[0]["side"] == "buy"
    assert stub.book["SPY"] == pytest.approx(100.0)


def test_the_second_cycle_sends_nothing():
    """Idempotency is the property the whole reconcile-don't-forward design buys."""
    stub = StubAlpaca()
    snap = snapshot(book_row("us_etfs", "1d", [("SPY", 100.0, 500.0)]))

    alpaca_mirror.run_cycle("us_etfs", stub, snap, assets_for(stub),
                            dry_run=False, force_hours=True)
    sent_after_first = len(stub.submitted)
    alpaca_mirror.run_cycle("us_etfs", stub, snap, assets_for(stub),
                            dry_run=False, force_hours=True)

    assert sent_after_first == 1
    assert len(stub.submitted) == 1


def test_a_reduced_target_sells_the_difference():
    stub = StubAlpaca()
    held = snapshot(book_row("us_etfs", "1d", [("SPY", 100.0, 500.0)]))
    alpaca_mirror.run_cycle("us_etfs", stub, held, assets_for(stub),
                            dry_run=False, force_hours=True)

    halved = snapshot(book_row("us_etfs", "1d", [("SPY", 50.0, 500.0)]))
    alpaca_mirror.run_cycle("us_etfs", stub, halved, assets_for(stub),
                            dry_run=False, force_hours=True)

    assert stub.submitted[-1]["side"] == "sell"
    assert stub.book["SPY"] == pytest.approx(50.0)


def test_a_retired_book_is_closed_out():
    stub = StubAlpaca()
    alpaca_mirror.run_cycle(
        "us_etfs", stub, snapshot(book_row("us_etfs", "1d", [("SPY", 100.0, 500.0)])),
        assets_for(stub), dry_run=False, force_hours=True)

    # The desk stops running the book entirely: no rows left for the class.
    alpaca_mirror.run_cycle("us_etfs", stub, snapshot(), assets_for(stub),
                            dry_run=False, force_hours=True)

    assert stub.submitted[-1]["side"] == "sell"
    assert stub.book.get("SPY", 0.0) == pytest.approx(0.0)


# ------------------------------------------------------------------ refusals

def test_a_closed_market_records_the_plan_and_sends_nothing():
    stub = StubAlpaca(is_open=False)
    snap = snapshot(book_row("us_etfs", "1d", [("SPY", 100.0, 500.0)]))

    plan = alpaca_mirror.run_cycle("us_etfs", stub, snap, assets_for(stub),
                                   dry_run=False, force_hours=False)

    assert plan.orders                      # it knew what it wanted
    assert stub.submitted == []             # ...and did not send it
    assert alpaca_store.summary()["cycles"] == 1


def test_crypto_ignores_the_market_clock():
    stub = StubAlpaca(is_open=False, prices={"BTCUSD": 90_000.0},
                      assets=[{"symbol": "BTC/USD", "status": "active",
                               "tradable": True, "fractionable": True}])
    snap = snapshot(book_row("crypto", "1d", [("BTC/USD", 1.0, 90_000.0)]))

    alpaca_mirror.run_cycle("crypto", stub, snap, assets_for(stub),
                            dry_run=False, force_hours=False)

    assert len(stub.submitted) == 1
    assert stub.submitted[0]["symbol"] == "BTC/USD"


def test_a_symbol_alpaca_will_not_trade_is_named_not_sent():
    stub = StubAlpaca(assets=[{"symbol": "SPY", "status": "active",
                               "tradable": True, "fractionable": True}])
    snap = snapshot(book_row("us_etfs", "1d",
                             [("SPY", 100.0, 500.0), ("QQQ", 50.0, 400.0)]))

    plan = alpaca_mirror.run_cycle("us_etfs", stub, snap, assets_for(stub),
                                   dry_run=False, force_hours=True)

    assert plan.untradable == ["QQQ"]
    assert [o["symbol"] for o in stub.submitted] == ["SPY"]


def test_dry_run_records_but_submits_nothing():
    stub = StubAlpaca()
    snap = snapshot(book_row("us_etfs", "1d", [("SPY", 100.0, 500.0)]))

    alpaca_mirror.run_cycle("us_etfs", stub, snap, assets_for(stub),
                            dry_run=True, force_hours=True)

    assert stub.submitted == []
    assert alpaca_store.summary()["orders"] == 1


# ------------------------------------------------------------------ the record

def test_a_fill_is_recorded_against_the_desks_own_mark():
    """The number this whole process exists to produce."""
    stub = StubAlpaca(prices={"SPY": 505.0})          # the venue fills 1% above the mark
    snap = snapshot(book_row("us_etfs", "1d", [("SPY", 100.0, 500.0)]))

    alpaca_mirror.run_cycle("us_etfs", stub, snap, assets_for(stub),
                            dry_run=False, force_hours=True)
    # The next cycle is what harvests the fill, exactly as the loop does.
    alpaca_mirror.run_cycle("us_etfs", stub, snap, assets_for(stub),
                            dry_run=False, force_hours=True)

    stats = alpaca_store.slippage("us_etfs")
    assert stats["fills"] == 1
    # Bought 5 above a 500 mark: 100 bp worse than the sandbox assumed.
    assert stats["mean_slip_bp"] == pytest.approx(100.0, abs=0.01)


def test_slippage_is_signed_from_the_desks_point_of_view():
    """A sell filled BELOW the mark also costs money, so it is positive too."""
    alpaca_store.record_fill("a", cls="us_etfs", symbol="SPY", side="sell", qty=1.0,
                             price=495.0, desk_mark=500.0)
    assert alpaca_store.slippage()["mean_slip_bp"] == pytest.approx(100.0, abs=0.01)


# ------------------------------------------------------------------ the staleness gate

def test_a_stale_snapshot_holds_everything(tmp_path, monkeypatch):
    state = tmp_path / "paper_state.json"
    state.write_text(json.dumps(
        snapshot(book_row("us_etfs", "1d", [("SPY", 100.0, 500.0)]))), encoding="utf-8")
    old = time.time() - 3600
    os.utime(state, (old, old))

    stub = StubAlpaca()
    monkeypatch.setattr(alpaca_client, "configured_classes", lambda: ["us_etfs"])
    monkeypatch.setattr(alpaca_client.AlpacaClient, "for_class",
                        classmethod(lambda cls, name, **kw: stub))

    rc = alpaca_mirror.main(["--once", "--state", str(state), "--max-age", "15",
                             "--db", str(tmp_path / "a.db"), "--ignore-hours"])

    assert rc == 0
    assert stub.submitted == []


def test_a_fresh_snapshot_is_acted_on(tmp_path, monkeypatch):
    state = tmp_path / "paper_state.json"
    state.write_text(json.dumps(
        snapshot(book_row("us_etfs", "1d", [("SPY", 100.0, 500.0)]))), encoding="utf-8")

    stub = StubAlpaca()
    monkeypatch.setattr(alpaca_client, "configured_classes", lambda: ["us_etfs"])
    monkeypatch.setattr(alpaca_client.AlpacaClient, "for_class",
                        classmethod(lambda cls, name, **kw: stub))

    rc = alpaca_mirror.main(["--once", "--state", str(state), "--max-age", "15",
                             "--db", str(tmp_path / "b.db"), "--ignore-hours"])

    assert rc == 0
    assert len(stub.submitted) == 1


# ------------------------------------------------------------------ safety

def test_the_endpoint_is_the_paper_one_and_nothing_reads_it_from_the_environment(
        monkeypatch):
    monkeypatch.setenv("ALPACA_BASE_URL", alpaca_client.LIVE_URL)
    monkeypatch.setenv("APCA_API_BASE_URL", alpaca_client.LIVE_URL)
    assert alpaca_client.PAPER_URL == "https://paper-api.alpaca.markets"
    source = (alpaca_client.__file__)
    with open(source, encoding="utf-8") as fh:
        body = fh.read()
    # The live host may be NAMED, but never handed to a request.
    assert 'url = f"{PAPER_URL}{path}"' in body
    assert "LIVE_URL}" not in body


def test_unsupported_classes_refuse_with_a_reason():
    for cls in ("commodities", "cme_futures"):
        with pytest.raises(KeyError) as exc:
            alpaca_client.credentials(cls)
        assert cls in str(exc.value)
