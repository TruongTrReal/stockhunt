"""The arithmetic between the desk's book and Alpaca's, exhaustively and offline.

No network, no credentials, no `paper_state.json`. Everything here is a synthetic snapshot
of the shape `paper_state.snapshot()` actually emits — which is why the fixtures carry the
fields they do, including the ones that look redundant: a book's top-level `units` is a
NAME COUNT, and a mirror that adds it to a share quantity is wrong in a way no smaller
fixture would reveal.
"""

from __future__ import annotations

import pytest

import alpaca_map as m


def book(cls, tf, holdings, *, equity=100_000.0, status="running", capital=100_000.0):
    """One book row, shaped like the real document."""
    return {
        "id": f"00:{cls}-{tf}-rule", "account": "00", "kind": "book",
        "symbol": f"{len(holdings)} names", "cls": cls, "tf": tf, "status": status,
        "capital": capital, "cash": 50_000.0, "equity": equity,
        # A NAME count, never a share count. If this is ever summed the tests below fail.
        "units": sum(1 for _sym, _u, _mk in holdings if _u),
        "holdings": [
            {"symbol": s, "units": u, "entry": None, "mark": mk, "state": "long"}
            for s, u, mk in holdings
        ],
    }


def snap(*strategies):
    return {"generated_at": "2026-08-27 12:00 UTC", "strategies": list(strategies)}


# ------------------------------------------------------------------ symbols

def test_norm_key_joins_both_crypto_spellings():
    assert m.norm_key("BTC/USD") == m.norm_key("BTCUSD") == "BTCUSD"
    assert m.norm_key("btc-usd") == "BTCUSD"
    assert m.norm_key(" spy ") == "SPY"


def test_alpaca_symbol_keeps_the_slash():
    assert m.alpaca_symbol("BTC/USD") == "BTC/USD"
    assert m.alpaca_symbol("spy") == "SPY"


def test_asset_index_drops_inactive_and_untradable():
    index = m.asset_index([
        {"symbol": "SPY", "status": "active", "tradable": True},
        {"symbol": "DEAD", "status": "inactive", "tradable": True},
        {"symbol": "HALTED", "status": "active", "tradable": False},
        {"symbol": "BTC/USD", "status": "active", "tradable": True},
    ])
    assert set(index) == {"SPY", "BTCUSD"}


# ------------------------------------------------------------------ the desk side

def test_desk_targets_sums_books_and_ignores_the_name_count():
    s = snap(
        book("us_etfs", "1d", [("SPY", 100.0, 500.0), ("QQQ", 50.0, 400.0)]),
        book("us_etfs", "4h", [("SPY", 25.0, 500.0)]),
    )
    assert m.desk_targets(s, "us_etfs") == {"SPY": 125.0, "QQQ": 50.0}


def test_desk_targets_skips_other_classes_and_stopped_books():
    s = snap(
        book("us_etfs", "1d", [("SPY", 100.0, 500.0)]),
        book("crypto", "1d", [("BTC/USD", 1.0, 90_000.0)]),
        book("us_etfs", "1d", [("QQQ", 10.0, 400.0)], status="paused"),
    )
    assert m.desk_targets(s, "us_etfs") == {"SPY": 100.0}
    assert m.desk_targets(s, "crypto") == {"BTC/USD": 1.0}


def test_desk_targets_reads_a_single_instrument_system():
    s = snap({"kind": "leg", "cls": "us_stocks", "status": "running",
              "symbol": "SOXL", "units": 12.5, "equity": 10_000.0})
    assert m.desk_targets(s, "us_stocks") == {"SOXL": 12.5}


def test_a_multi_ticker_member_is_not_summed_into_a_fake_symbol():
    """The live ETF desk carries exactly this row.

    A member registers with whatever they typed in `symbol`, and one on the running desk
    reads `"QQQ, IWM, XLK, TLT"` with `units` the SUM across those four names. Summing it
    invents a target for a ticker nobody lists, which Alpaca then refuses -- and the
    refusal reads as a BROKER coverage gap for a shape this desk chose.
    """
    s = snap({"kind": "member", "cls": "us_etfs", "status": "running",
              "symbol": "QQQ, IWM, XLK, TLT", "units": 329.0, "equity": 100_289.52})
    assert m.desk_targets(s, "us_etfs") == {}
    assert m.desk_marks(s, "us_etfs") == {}


def test_a_multi_ticker_member_is_reported_as_unmirrorable():
    """Skipping it silently would leave a whole $100,000 book missing from the second
    record with nothing saying so."""
    s = snap({"kind": "member", "cls": "us_etfs", "status": "running",
              "symbol": "QQQ, IWM, XLK, TLT", "units": 329.0, "equity": 100_289.52})
    (symbol, reason), = m.unmirrorable(s, "us_etfs")
    assert symbol == "QQQ, IWM, XLK, TLT"
    assert "4 instruments" in reason and "329" in reason


def test_a_single_ticker_member_is_mirrored_normally():
    """The repair must not swallow the ordinary case it sits next to."""
    s = snap({"kind": "member", "cls": "us_etfs", "status": "running",
              "symbol": "SPY", "units": 40.0, "mark_price": 500.0, "equity": 100_000.0})
    assert m.desk_targets(s, "us_etfs") == {"SPY": 40.0}
    assert m.desk_marks(s, "us_etfs") == {"SPY": 500.0}
    assert m.unmirrorable(s, "us_etfs") == []


def test_a_book_is_never_reported_unmirrorable_for_its_label():
    """A book's `symbol` is a LABEL ("5 names") and its units are a NAME COUNT, but its
    per-name quantities are published in `holdings`, so it mirrors fine."""
    s = snap(book("us_etfs", "1d", [("SPY", 10.0, 500.0), ("QQQ", 5.0, 400.0)]))
    assert m.unmirrorable(s, "us_etfs") == []
    assert m.desk_targets(s, "us_etfs") == {"SPY": 10.0, "QQQ": 5.0}


def test_a_stopped_multi_ticker_member_is_not_reported():
    s = snap({"kind": "member", "cls": "us_etfs", "status": "stopped",
              "symbol": "QQQ, IWM", "units": 100.0, "equity": 100_000.0})
    assert m.unmirrorable(s, "us_etfs") == []


def test_desk_equity_falls_back_to_capital_when_unmarked():
    s = snap(
        book("us_etfs", "1d", [("SPY", 1.0, 500.0)], equity=110_000.0),
        {"kind": "book", "cls": "us_etfs", "status": "running", "equity": None,
         "capital": 100_000.0, "holdings": []},
    )
    assert m.desk_equity(s, "us_etfs") == pytest.approx(210_000.0)


def test_desk_marks_are_collected_per_symbol():
    s = snap(book("us_etfs", "1d", [("SPY", 1.0, 501.5), ("QQQ", 0.0, 400.0)]))
    assert m.desk_marks(s, "us_etfs") == {"SPY": 501.5, "QQQ": 400.0}


# ------------------------------------------------------------------ the Alpaca side

def test_held_units_parses_strings_and_drops_dust():
    held = m.held_units([{"symbol": "SPY", "qty": "10.5"},
                         {"symbol": "BTC/USD", "qty": "0.25"},
                         {"symbol": "DUST", "qty": "0.0000000001"},
                         {"symbol": "BAD", "qty": None}])
    assert held == {"SPY": 10.5, "BTCUSD": 0.25}


def test_account_equity_prefers_equity_then_falls_back():
    assert m.account_equity({"equity": "100000.5"}) == pytest.approx(100_000.5)
    assert m.account_equity({"equity": "0", "portfolio_value": "99"}) == pytest.approx(99)
    assert m.account_equity({}) == 0.0


def test_scale_ratio_is_zero_when_either_side_is_unknown():
    assert m.scale_ratio(600_000.0, 100_000.0) == pytest.approx(1 / 6)
    assert m.scale_ratio(0.0, 100_000.0) == 0.0
    assert m.scale_ratio(600_000.0, 0.0) == 0.0


# ------------------------------------------------------------------ the plan

EQUITY_ASSETS = m.asset_index([
    {"symbol": "SPY", "status": "active", "tradable": True, "fractionable": True},
    {"symbol": "QQQ", "status": "active", "tradable": True, "fractionable": True},
    {"symbol": "BRKA", "status": "active", "tradable": True, "fractionable": False},
])
CRYPTO_ASSETS = m.asset_index([
    {"symbol": "BTC/USD", "status": "active", "tradable": True, "fractionable": True,
     "min_trade_increment": "0.0001"},
])


def plan(cls="us_etfs", *, targets, held=None, marks=None, ratio=1.0,
         assets=EQUITY_ASSETS, **kw):
    return m.plan_orders(cls, targets=targets, held=held or {},
                         marks=marks or {"SPY": 500.0, "QQQ": 400.0, "BRKA": 700_000.0,
                                         "BTC/USD": 90_000.0},
                         ratio=ratio, assets=assets, **kw)


def test_buy_from_flat_is_scaled_by_the_ratio():
    p = plan(targets={"SPY": 600.0}, ratio=1 / 6)
    assert [(o["symbol"], o["side"], o["qty"]) for o in p.orders] == \
        [("SPY", "buy", pytest.approx(100.0))]


def test_reconciled_book_sends_nothing():
    p = plan(targets={"SPY": 600.0}, held={"SPY": 100.0}, ratio=1 / 6)
    assert p.orders == []


def test_a_target_of_zero_sells_the_whole_position():
    p = plan(targets={"SPY": 0.0}, held={"SPY": 100.0})
    assert [(o["side"], o["qty"]) for o in p.orders] == [("sell", pytest.approx(100.0))]


def test_a_name_the_desk_no_longer_holds_is_still_closed():
    """A retired book leaves shares behind unless every held symbol is considered, not
    only the ones the desk named."""
    p = plan(targets={"SPY": 100.0}, held={"SPY": 100.0, "QQQ": 40.0})
    assert [(o["symbol"], o["side"], o["qty"]) for o in p.orders] == \
        [("QQQ", "sell", pytest.approx(40.0))]


def test_negative_target_is_clamped_and_reported():
    p = plan(targets={"SPY": -50.0}, held={"SPY": 10.0})
    assert p.clamped == ["SPY"]
    assert [(o["side"], o["qty"]) for o in p.orders] == [("sell", pytest.approx(10.0))]
    assert p.targets["SPY"] == 0.0


def test_never_sells_into_a_short():
    p = plan(targets={"SPY": 0.0}, held={"SPY": 5.0})
    assert p.orders[0]["qty"] == pytest.approx(5.0)


def test_an_untradable_symbol_is_named_not_ordered():
    p = plan(targets={"SPY": 10.0, "XMR/USD": 3.0})
    assert p.untradable == ["XMR/USD"]
    assert [o["symbol"] for o in p.orders] == ["SPY"]


def test_a_non_fractionable_name_rounds_toward_zero():
    p = plan(targets={"BRKA": 2.9}, marks={"BRKA": 700_000.0})
    assert p.orders[0]["qty"] == pytest.approx(2.0)


def test_a_full_exit_of_a_non_fractionable_name_leaves_no_stub():
    p = plan(targets={"BRKA": 0.0}, held={"BRKA": 2.5}, marks={"BRKA": 700_000.0})
    assert p.orders[0]["qty"] == pytest.approx(2.5)


def test_crypto_rounds_to_the_trade_increment():
    p = plan("crypto", targets={"BTC/USD": 0.123456789}, assets=CRYPTO_ASSETS,
             marks={"BTC/USD": 90_000.0})
    assert p.orders[0]["qty"] == pytest.approx(0.1234, abs=1e-9)
    assert p.orders[0]["alpaca_symbol"] == "BTC/USD"


def test_a_tiny_rebalance_is_skipped_on_notional():
    p = plan(targets={"SPY": 100.004}, held={"SPY": 100.0}, min_drift=0.0)
    assert p.orders == []
    assert any("floor" in reason for _, reason in p.skipped)


def test_a_small_rebalance_is_skipped_on_drift():
    p = plan(targets={"SPY": 101.0}, held={"SPY": 100.0}, min_notional=0.0)
    assert p.orders == []
    assert any("drift" in reason for _, reason in p.skipped)


def test_sells_come_before_buys_and_the_cycle_is_capped():
    p = plan(targets={"SPY": 200.0, "QQQ": 0.0},
             held={"SPY": 100.0, "QQQ": 50.0}, max_orders=1)
    assert [(o["symbol"], o["side"]) for o in p.orders] == [("QQQ", "sell")]
    assert any("cycle cap" in reason for _, reason in p.skipped)


def test_a_zero_ratio_flattens_everything():
    """If either equity is unknown the mirror must go flat, not guess a size."""
    p = plan(targets={"SPY": 600.0}, held={"SPY": 100.0}, ratio=0.0)
    assert [(o["side"], o["qty"]) for o in p.orders] == [("sell", pytest.approx(100.0))]


# ------------------------------------------------------------------ orders in flight
#
# The defect these pin cost real money on 2026-08-27. `/v2/positions` does not know about
# an order that has not filled, so the first cycles after the US open sent 60 orders, then
# 60 more a minute later, then more -- and the excess had to be sold back the same morning.

def test_pending_units_signs_by_side_and_counts_only_the_remainder():
    pend = m.pending_units([
        {"symbol": "SPY", "side": "buy", "qty": "10", "filled_qty": "4"},
        {"symbol": "QQQ", "side": "sell", "qty": "8", "filled_qty": "0"},
    ])
    assert pend == {"SPY": pytest.approx(6.0), "QQQ": pytest.approx(-8.0)}


def test_pending_units_sums_several_orders_on_one_symbol():
    pend = m.pending_units([
        {"symbol": "SPY", "side": "buy", "qty": "10", "filled_qty": "0"},
        {"symbol": "SPY", "side": "buy", "qty": "5", "filled_qty": "0"},
    ])
    assert pend == {"SPY": pytest.approx(15.0)}


def test_pending_units_ignores_a_fully_filled_order():
    assert m.pending_units([
        {"symbol": "SPY", "side": "buy", "qty": "10", "filled_qty": "10"}]) == {}


def test_an_order_in_flight_is_not_sent_again():
    """THE regression. Position still zero, order already working: send nothing."""
    p = plan(targets={"SPY": 100.0}, held={}, pending={"SPY": 100.0})
    assert p.orders == []
    assert p.pending == {"SPY": pytest.approx(100.0)}
    assert any("in flight" in reason for _, reason in p.skipped)


def test_a_partial_fill_orders_only_the_remainder():
    p = plan(targets={"SPY": 100.0}, held={"SPY": 30.0}, pending={"SPY": 70.0})
    assert p.orders == []
    # ...and with only part of it working, the rest is still wanted.
    p2 = plan(targets={"SPY": 100.0}, held={"SPY": 30.0}, pending={"SPY": 20.0})
    assert [(o["side"], o["qty"]) for o in p2.orders] == [("buy", pytest.approx(50.0))]


def test_a_pending_sell_is_not_duplicated_into_a_double_exit():
    """A working sell must not be re-sent, or the account ends up short."""
    p = plan(targets={"SPY": 0.0}, held={"SPY": 80.0}, pending={"SPY": -80.0})
    assert p.orders == []


def test_a_partial_exit_in_flight_sells_only_what_is_left():
    p = plan(targets={"SPY": 0.0}, held={"SPY": 80.0}, pending={"SPY": -30.0})
    assert [(o["side"], o["qty"]) for o in p.orders] == [("sell", pytest.approx(50.0))]


def test_never_sells_more_than_is_actually_held_despite_a_pending_buy():
    """An in-flight BUY is not stock this account can deliver yet."""
    p = plan(targets={"SPY": 0.0}, held={"SPY": 10.0}, pending={"SPY": 40.0})
    assert all(o["qty"] <= 10.0 + 1e-9 for o in p.orders if o["side"] == "sell")


def test_a_symbol_only_in_flight_is_still_considered():
    """Target dropped to zero while the opening buy was still working."""
    p = plan(targets={}, held={}, pending={"SPY": 25.0})
    assert p.pending == {"SPY": pytest.approx(25.0)}
    assert p.orders == []          # nothing held yet, so there is nothing to sell


def test_no_pending_reproduces_the_previous_behaviour_exactly():
    """The fix must be inert when nothing is in flight."""
    a = plan(targets={"SPY": 100.0}, held={"SPY": 40.0})
    b = plan(targets={"SPY": 100.0}, held={"SPY": 40.0}, pending={})
    assert [(o["side"], o["qty"]) for o in a.orders] ==            [(o["side"], o["qty"]) for o in b.orders] == [("buy", pytest.approx(60.0))]


def test_client_order_id_is_deterministic_and_distinct_per_cycle():
    a = m.client_order_id("crypto", "BTC/USD", 1000)
    assert a == m.client_order_id("crypto", "BTC/USD", 1000)
    assert a != m.client_order_id("crypto", "BTC/USD", 1001)
    assert a != m.client_order_id("crypto", "ETH/USD", 1000)
    assert len(a) <= 128 and "/" not in a
