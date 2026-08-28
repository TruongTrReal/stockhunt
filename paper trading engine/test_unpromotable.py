"""Rules a book may never hold, and the two places that must both refuse them.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_unpromotable.py -q

A live book recomputes its rule over a ROLLING buffer. A rule whose value at bar t depends
on bar 0 slides underneath that buffer and trades something the backtest never scored —
with a healthy log, a filling order path and a curve that looks like any other. That is the
worst available failure mode, and it is why this is enforced in code rather than recorded
in a comment: `lorentzian_knn` was documented as "not promotable" for weeks with nothing
stopping it, and `volmanaged` was live at 1d and 4h the whole time.

The test that matters most is the last one: the catalog's mark is a courtesy to the picker
and `desk_control` is the bind, so a registration that reaches the node by any other route
must still be refused.
"""

from __future__ import annotations

import pytest

import paper_config


@pytest.mark.parametrize("rule", ["volmanaged", "voltgt_tsmom", "dyn_breakout2",
                                  "lorentzian_knn"])
def test_an_expanding_statistic_is_barred(rule):
    assert paper_config.unpromotable_reason(rule)


def test_the_reason_says_that_more_warmup_would_not_help(_reason=None):
    """The obvious fix is a bigger buffer, and for this family it is not a fix at all."""
    why = paper_config.unpromotable_reason("volmanaged")
    assert "No warm-up size fixes this." in why


@pytest.mark.parametrize("rule", ["ibs", "MAXINDEX", "MAXINDEX~MININDEX|or",
                                  "bb_outside_in@allow_short=0", "SMA_200"])
def test_a_rule_that_reproduces_is_not_barred(rule):
    """Every TA-Lib rule and `ibs` measured 100% agreement; none of them belong here."""
    assert paper_config.unpromotable_reason(rule) == ""


def test_a_parameterised_form_of_a_barred_rule_is_still_barred(self=None):
    """`volmanaged@window=40` is `volmanaged` with a knob turned, not a different rule."""
    assert paper_config.unpromotable_reason("volmanaged@window=40")


def test_the_regime_overlay_is_barred_on_any_base(self=None):
    """It composes with every label, so the names cannot be listed — only the prefix."""
    assert paper_config.unpromotable_reason("regime:ibs")
    assert paper_config.unpromotable_reason("regime:MAXINDEX~MININDEX|or")


def test_a_rule_merely_containing_the_word_is_not_barred(self=None):
    """The match is on the label, not on a substring of it."""
    assert paper_config.unpromotable_reason("my_regime_filter") == ""


def test_the_catalog_marks_it_untradable_with_the_reason(monkeypatch):
    """The picker has to say WHY, or a missing top row reads as missing data."""
    import catalog
    import live_signal

    monkeypatch.setattr(catalog, "_board_rows",
                        lambda cls, tf: ([{"rule": "volmanaged", "kind": "single"},
                                          {"rule": "ibs", "kind": "single"}], False))
    monkeypatch.setattr(live_signal, "family", lambda rule: "published")

    cells = {c["rule"]: c for c in catalog.cells("us_stocks", "1d", 10)}
    assert cells["volmanaged"]["tradable"] is False
    assert "expanding" in cells["volmanaged"]["not_tradable_because"].lower()
    assert cells["ibs"]["tradable"] is True


def test_the_desk_refuses_it_even_when_the_catalog_did_not(self=None):
    """The bind, not the courtesy.

    A registration can reach the node from an older `catalog.json`, a hand-written row or
    a member's API call, and none of those consulted this catalog build.
    """
    import desk_control

    assert "expanding" in desk_control.book_refusal(
        {"tf": "1d", "rule": "volmanaged"}).lower()
    assert desk_control.book_refusal({"tf": "1d", "rule": "ibs"}) == ""
    # The timeframe gate it shares the function with still answers first.
    assert "books run at" in desk_control.book_refusal({"tf": "2h", "rule": "ibs"})
