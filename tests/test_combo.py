"""`strategies.overlays.combo` — one operator definition, one label grammar.

The bug this module was extracted to fix is worth restating, because it is the reason
these tests are not cosmetic: `strategies.registry.build` returned `None` for every
`A~B|op` label, *silently*, because `build` swallows failures. On the dashboard
leaderboard 22 of the top 25 rows are pairs, so every portfolio, deflated-Sharpe and
factor-alpha number computed through the registry was quietly skipping most of the board.

So there is a test that a combo label resolves through the registry, not merely that the
parser returns a tuple.
"""

from __future__ import annotations

import numpy as np
import pytest

from strategies.overlays import combo

A = np.array([1.0, 1.0, 0.0, -1.0, -1.0, 0.0])
B = np.array([1.0, -1.0, 1.0, -1.0, 0.0, 0.0])


# --------------------------------------------------------------------- operators

def test_vote_takes_the_sign_of_the_sum():
    np.testing.assert_array_equal(combo.combine(A, B, "vote"),
                                  [1.0, 0.0, 1.0, -1.0, -1.0, 0.0])


def test_and_requires_the_legs_to_agree_in_sign():
    np.testing.assert_array_equal(combo.combine(A, B, "and"),
                                  [1.0, 0.0, 0.0, -1.0, 0.0, 0.0])


def test_or_prefers_the_first_leg_and_falls_back_to_the_second():
    np.testing.assert_array_equal(combo.combine(A, B, "or"),
                                  [1.0, 1.0, 1.0, -1.0, -1.0, 0.0])


def test_gate_takes_the_first_leg_only_where_the_second_is_active():
    np.testing.assert_array_equal(combo.combine(A, B, "gate"),
                                  [1.0, 1.0, 0.0, -1.0, 0.0, 0.0])


def test_or_is_the_operator_that_spends_the_most():
    """Documented: `or` wins the equity leaderboard because it is the most invested, and
    `corr(IR, long_frac)` is 0.881 there. That is a property of the operator, so it should
    be visible in the arithmetic."""
    for op in combo.OPERATORS:
        assert np.mean(combo.combine(A, B, "or") != 0) >= np.mean(
            combo.combine(A, B, op) != 0)


def test_an_unknown_operator_raises_rather_than_returning_flat():
    with pytest.raises(ValueError, match="unknown operator"):
        combo.combine(A, B, "xor")


def test_the_operator_list_is_the_grammar():
    assert combo.OPERATORS == ("vote", "and", "or", "gate")


# ------------------------------------------------------------------------ parsing

def test_the_canonical_form_parses():
    assert combo.parse("HT_TRENDMODE~MAXINDEX|or") == ("HT_TRENDMODE", "MAXINDEX", "or")


def test_the_legacy_form_still_parses():
    """`combo_sweep.py` wrote `A op B` and those sheets are on disk; a parser that cannot
    read them makes those results unrebuildable."""
    assert combo.parse("SMA_50 and RSI_14") == ("SMA_50", "RSI_14", "and")


def test_a_rule_name_containing_a_space_is_not_mistaken_for_an_operator():
    """Checked against the operator list, never by splitting on whitespace."""
    assert combo.parse("some rule name") is None


def test_an_operator_word_inside_a_leg_name_does_not_split_the_label():
    assert combo.parse("BAND_20~ORACLE|vote") == ("BAND_20", "ORACLE", "vote")


@pytest.mark.parametrize("label", [
    "SMA_50",                      # a plain single
    "ibs@buy=0.1",                 # a published-strategy variant
    "A~B|xor",                     # not an operator
    "A~B",                         # no operator at all
    "A|or",                        # no second leg
    "~B|or",                       # empty first leg
    "A~|or",                       # empty second leg
    "",
    None,
    123,
])
def test_non_combos_parse_to_none(label):
    assert combo.parse(label) is None
    assert combo.is_combo(label) is False


def test_encode_and_parse_round_trip():
    for op in combo.OPERATORS:
        label = combo.encode("HT_PHASOR", "ATR", op)
        assert combo.parse(label) == ("HT_PHASOR", "ATR", op)
        assert combo.is_combo(label)


def test_encode_refuses_an_unknown_operator():
    with pytest.raises(ValueError, match="unknown operator"):
        combo.encode("A", "B", "nand")


def test_the_operator_is_taken_from_the_RIGHTMOST_separator():
    """A leg that itself contains `|` must not steal the operator slot."""
    assert combo.parse("A|x~B|or") == ("A|x", "B", "or")


# ------------------------------------------------------------------------- apply

class FakeFrame:
    def __init__(self, n):
        self._n = n

    def __len__(self):
        return self._n


def resolver(mapping):
    def resolve(label, df, close, bpy, symbol):
        return mapping.get(label)
    return resolve


def test_apply_resolves_both_legs_and_combines_them():
    df = FakeFrame(6)
    out = combo.apply("X~Y|and", df, None, 252.0, "AAPL", resolver({"X": A, "Y": B}))
    np.testing.assert_array_equal(out, combo.combine(A, B, "and"))


def test_apply_returns_none_when_either_leg_is_unbuildable():
    df = FakeFrame(6)
    assert combo.apply("X~Z|or", df, None, 252.0, "AAPL",
                       resolver({"X": A})) is None
    assert combo.apply("Z~Y|or", df, None, 252.0, "AAPL",
                       resolver({"Y": B})) is None


def test_apply_returns_none_on_a_length_mismatch():
    df = FakeFrame(6)
    assert combo.apply("X~Y|or", df, None, 252.0, "AAPL",
                       resolver({"X": A, "Y": B[:3]})) is None


def test_apply_returns_none_for_a_label_that_is_not_a_combo():
    assert combo.apply("SMA_50", FakeFrame(6), None, 252.0, "AAPL",
                       resolver({"SMA_50": A})) is None


def test_apply_does_not_resolve_the_second_leg_when_the_first_fails():
    """Legs are expensive to build; a failed first leg must short-circuit."""
    seen = []

    def resolve(label, df, close, bpy, symbol):
        seen.append(label)
        return None

    combo.apply("X~Y|or", FakeFrame(6), None, 252.0, "AAPL", resolve)
    assert seen == ["X"]


def test_signals_reexports_the_one_definition():
    """`signals.combine` must BE this function, not a copy — the searcher and the report
    renderer cannot be allowed to drift on the meaning of `and`."""
    import signals
    assert signals.combine is combo.combine
    assert signals.OPERATORS is combo.OPERATORS


def test_the_registry_can_build_a_combo_label():
    """The silent-None regression, pinned. Combos are 22 of the dashboard's top 25 rows."""
    import pandas as pd

    from strategies.registry import build

    n = 400
    df = pd.DataFrame({"Close": np.linspace(100.0, 200.0, n)},
                      index=pd.date_range("2020-01-01", periods=n, freq="D"))
    close = df["Close"].to_numpy()

    label = combo.encode("ALWAYS_LONG", "RANDOM_50", "gate")
    pos = build(label, df, close, 252.0, "AAPL")
    assert pos is not None, "registry.build returned None for a combo label"
    assert pos.size == n
    # `gate` keeps leg A only where leg B is active, and leg A here is always long.
    assert set(np.unique(pos)) <= {0.0, 1.0}
    assert 0.0 < float(np.mean(pos)) < 1.0
