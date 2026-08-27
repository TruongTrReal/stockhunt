"""What the leaderboard refuses to rank, and what it ranks on.

Three decisions live in `board_rank` and none of them is visible to
`tools/test_board_equivalence.py`, which proves the rendered document did not move and is
therefore silent about whether the document was right. Each of the three was a rule
sitting in the top ten of a live sheet:

* **a book that never held anything.** `BBANDS` and `CDL3STARSINSOUTH` sat 6th and 5th on
  us_stocks 1d with one trade between them and 0.0% exposure. They ranked there because
  the money tiebreak is the book's cash-matched excess CAGR and a cash account's is a
  rounding error below zero, which beats every rule that took risk and lost;
* **a book that IS buy-and-hold.** `mc_williams_r_reactive` is 98.8% invested with an
  R-squared against buy-and-hold of 0.992 — whatever it computes, what it holds is the
  index;
* **which standard the sort key reads.** It used to be `edge_standard.csv`'s, which scores
  the MEDIAN ASSET, while the tiebreak and every column beside it are the BOOK's. The two
  disagreed on 29 of the 30 rows shipped for us_stocks 1d.

Dicts, not fixtures: these are pure predicates over the record `_book_record` builds, so
the test is the record's shape and nothing else. No store, no CSV, no bars.
"""

from __future__ import annotations

import board_rank as B


def book(**kw) -> dict:
    """A book record with plausible defaults — a real, mid-exposure, actively traded rule.

    Every test below overrides exactly the fields it is about, so a default that drifts
    into a filter's range would be visible as a whole file going red rather than as one
    assertion quietly testing nothing.
    """
    rec = {"n_trades": 5000, "exposure": 0.47, "r2_vs_bh": 0.77, "beta_bh": 0.57,
           "n_names": 187, "years": 23.6, "standard": {"passed": 3}}
    rec.update(kw)
    return rec


# ------------------------------------------------------------------ a book doing nothing

def test_a_book_that_never_opened_a_position_is_idle():
    assert B.is_idle(book(n_trades=0, exposure=0.0, r2_vs_bh=None))


def test_a_book_in_cash_is_idle_however_often_it_trades():
    """`CDLINNECK` opens 849 positions on us_stocks 1d and is invested 0.8% of the time.
    A trade count cannot separate it from a rule that opens one; exposure can."""
    assert B.is_idle(book(n_trades=849, exposure=0.008))


def test_a_rare_but_real_rule_is_not_idle():
    """`CDLINVERTEDHAMMER` holds 7.7% of the time and clears five of the six criteria as a
    book. Rare is not idle, and the floor has to leave room for it."""
    assert not B.is_idle(book(n_trades=7831, exposure=0.077))


def test_no_book_record_is_not_idle():
    """Unknown is not empty. A rule the book stage has not reached must not be cut as
    though it had been measured and found flat."""
    assert not B.is_idle(None)
    assert not B.is_idle({})


# --------------------------------------------------------------- a book that is the index

def test_a_closet_index_is_caught_on_the_outcome():
    assert B.is_closet_bh(book(exposure=0.988, r2_vs_bh=0.992, beta_bh=0.96, n_trades=53))


def test_a_de_levered_sleeve_is_kept():
    """High R-squared at beta 0.55 is half the market and half in bills. Cash-matching
    already prices that fairly, so cutting it would remove a real answer."""
    assert not B.is_closet_bh(book(exposure=0.5, r2_vs_bh=0.97, beta_bh=0.55))


def test_a_rule_that_dodges_something_is_kept():
    """`MININDEX~MA_50|or` on crypto 1d is 98.5% invested and still posts a large
    cash-matched excess: 10% of its variance is its own, and that 10% is the strategy.
    Exposure alone would have cut it, which is why the test is on the returns."""
    assert not B.is_closet_bh(book(exposure=0.985, r2_vs_bh=0.904, beta_bh=0.87))


def test_buy_and_hold_without_a_regression_falls_back_to_how_it_was_built():
    """One trade per name, held for the whole span, 99.8% invested. `mc_bollinger_bandwidth`
    is buy-and-hold whatever its docstring says, and a book row written before the
    benchmark regression existed still has to say so."""
    assert B.is_closet_bh(book(exposure=0.998, r2_vs_bh=None, beta_bh=None,
                               n_trades=187, n_names=187, years=23.6))


def test_the_fallback_does_not_cut_an_active_rule():
    assert not B.is_closet_bh(book(exposure=0.998, r2_vs_bh=None, beta_bh=None,
                                   n_trades=71065, n_names=187, years=23.6))


def test_no_book_record_is_not_a_closet_index():
    assert not B.is_closet_bh(None)
    assert not B.is_closet_bh({})


# ------------------------------------------------------------------- which standard sorts

def test_the_sort_key_is_the_books_criteria_count():
    assert B._criteria_passed(book(standard={"passed": 5})) == 5


def test_a_row_with_no_book_verdict_sorts_below_a_measured_failure():
    """-1, not 0. "Never looked at" and "cleared nothing" are different facts and sorting
    them level buries the rule that was actually measured."""
    assert B._criteria_passed(None) == -1
    assert B._criteria_passed({"standard": None}) == -1
    assert B._criteria_passed(book(standard={"passed": 0})) == 0
    assert B._criteria_passed(book(standard={"passed": 0})) > B._criteria_passed(None)
