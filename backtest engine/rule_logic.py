"""Long-form explanation for a TA-Lib rule: what it measures, and how to read its result.

`strategies.talib_signals.describe_signal` says what the *rule* does — "long when X is
above its 20-day average". That is the mechanics. It does not say what X measures, what
the resulting position is really exposed to, or why a whole family of rules should be
read with suspicion, and without those a leaderboard row is a number with no meaning
attached.

There are 231 rules over ~160 TA-Lib functions, so writing an essay each would be both
enormous and mostly repetition: rules within a family share their mechanism, their
exposure and their failure mode. The explanation is therefore assembled from three parts:

    1. the FAMILY — what this class of indicator measures and what it is exposed to;
    2. the MECHANICS — `describe_signal`, the exact entry and exit;
    3. a WARNING where one applies, most importantly the generic fallback.

**The generic fallback is the single most important thing on this page.** 36 of the 231
functions have no trading direction of their own — `ATR` measures range, `CORREL`
measures co-movement, `COS` is arithmetic. To score them at all, the harness wraps them
in a synthetic rule: long when the indicator sits above its own 20-day average. That is
not a strategy anybody proposed; it is a scaffold that makes the function *testable*. A
row like `COS` scoring well is a statement about that scaffold and about noise, never
about cosine as a trading signal, and any reader who does not know this will draw exactly
the wrong conclusion.
"""

from __future__ import annotations

import talib

FAMILY_LOGIC = {
    "Overlap Studies": (
        "Moving averages and bands — a smoothed version of price itself.",
        "Trend, and heavy market beta. A price-versus-average rule is long whenever the "
        "market is above its own recent level, which on a rising index is most of the "
        "time. Read `exposure` before crediting any of these with skill: much of what "
        "looks like signal is simply time in the market.",
        "They lag by construction, so they buy after the move and sell after the "
        "reversal, and they whipsaw in ranges. The lookback is the whole strategy."),
    "Momentum Indicators": (
        "Rate-of-change and oscillator measures — how fast price is moving and whether "
        "that pace is extreme.",
        "Either trend or short-term reversal depending on how the rule is read. Bounded "
        "oscillators (RSI, STOCH) used at extremes are reversal bets; unbounded ones "
        "(MOM, ROC) used by sign are trend bets. The family label does not tell you "
        "which — the rule does.",
        "Oscillators saturate: in a strong trend RSI can sit above 70 for months, so a "
        "rule that sells 'overbought' fights the trend the whole way up."),
    "Volatility Indicators": (
        "The size of recent price movement, without direction. ATR is average true "
        "range; NATR normalises it by price.",
        "Nothing directional at all — these have no inherent trading side, so they are "
        "scored through the generic fallback. The resulting position is a bet that "
        "volatility itself trends, which is a real effect but a different claim from "
        "anything about price.",
        "Volatility clusters and mean-reverts on different horizons, so the scaffold's "
        "20-day average picks one of those arbitrarily."),
    "Volume Indicators": (
        "Flow measures combining price and traded volume — accumulation, distribution, "
        "money flow.",
        "The claim is that volume confirms or contradicts price, so flow leads return.",
        "Twelve Data serves NO volume for crypto or spot commodities, so these cannot be "
        "evaluated there at all — they are skipped and counted, never fed zeros, because "
        "a volume rule on missing data produces a flat position that is indistinguishable "
        "on a leaderboard from a rule that does nothing."),
    "Cycle Indicators": (
        "Hilbert Transform measures — an attempt to decompose price into a dominant "
        "cycle and to say whether the market is trending or oscillating.",
        "A regime classifier more than a direction. `HT_TRENDMODE` is binary and only "
        "ever long or flat, so it collects market drift while it is switched on.",
        "The underlying signal-processing assumes a roughly stationary cycle, which "
        "market prices do not have. In practice these behave like slow trend filters."),
    "Pattern Recognition": (
        "Candlestick patterns — 61 named shapes over one to three bars, each returning "
        "+100 / 0 / -100.",
        "Japanese candlestick folklore: specific bar geometries encode the balance of "
        "buyers and sellers and therefore predict the next move.",
        "This is the largest family and the weakest prior — 61 functions is 61 trials, "
        "and it dominates the multiplicity correction. Most patterns fire rarely, so "
        "their statistics rest on few events, and a pattern that 'works' is the single "
        "most likely thing on the whole sheet to be luck."),
    "Statistic Functions": (
        "Regression and dispersion statistics over a rolling window — slope, intercept, "
        "standard deviation, correlation, beta.",
        "Linear-regression rules are trend measures in another coordinate system; "
        "dispersion measures have no direction and go through the fallback.",
        "`BETA` and `CORREL` need a benchmark series, so they measure a relationship "
        "with the index rather than anything about the asset alone."),
    "Price Transform": (
        "Arithmetic re-expressions of the bar — typical price, weighted close, median "
        "price.",
        "Nothing. These are close variants, so a rule on them is a rule on price with "
        "a slightly different smoothing.",
        "They are near-duplicates of each other and of the close, so several rows on the "
        "leaderboard are effectively the same trial counted many times — which inflates "
        "the apparent breadth of any result."),
    "Math Transform": (
        "Pure mathematics applied to the price series: cosine, log, square root, "
        "arctangent.",
        "Nothing whatsoever. There is no economic claim here, and none is intended.",
        "These exist to establish the null. They are scored through the generic fallback "
        "and their whole purpose is to show what a meaningless rule scores on this data. "
        "If one of them ranks highly, that is the measurement of noise — treat it as the "
        "scale against which everything else should be read."),
    "Math Operators": (
        "Elementwise arithmetic and rolling extremes — add, subtract, min, max, sum.",
        "Nothing directional. `MAXINDEX`/`MININDEX` return the POSITION of a recent "
        "extreme within the window, which through the fallback becomes a slow trend proxy.",
        "Same as Math Transform: these are null-establishing rows. `MAXINDEX` topping a "
        "leaderboard is a statement about the noise floor, not a discovery."),
}

FALLBACK_WARNING = (
    "GENERIC FALLBACK — this indicator has no trading direction of its own, so the "
    "harness wraps it in a synthetic rule (long when the indicator is above its own "
    "20-day average). That scaffold is not a strategy anyone proposed; it exists so the "
    "function can be scored at all. Read any result as a statement about the scaffold "
    "and the noise floor, not about the indicator as a signal.")

_GROUP_OF: dict[str, str] = {}
for _group, _fns in talib.get_function_groups().items():
    for _fn in _fns:
        _GROUP_OF[_fn] = _group


def base_function(rule: str) -> str:
    """`EMA_200` -> `EMA`. Period variants share their parent's meaning."""
    if rule in _GROUP_OF:
        return rule
    head = rule.split("_")[0]
    return head if head in _GROUP_OF else rule


def family_of(rule: str) -> str:
    return _GROUP_OF.get(base_function(rule), "")


def explain(rule: str, describe_signal=None, fallback_functions=frozenset()) -> str:
    """Assemble the long-form explanation for one rule."""
    fn = base_function(rule)
    family = _GROUP_OF.get(fn, "")
    parts = []

    if describe_signal is not None:
        try:
            mech = describe_signal(rule)
        except Exception:
            mech = ""
        if mech:
            parts.append("How the rule trades\n    " + mech)

    if family and family in FAMILY_LOGIC:
        measures, exposed, fails = FAMILY_LOGIC[family]
        parts.append(f"What it measures ({family})\n    " + measures)
        parts.append("What the position is really exposed to\n    " + exposed)
        parts.append("How it fails\n    " + fails)

    if fn in fallback_functions:
        parts.append("Warning\n    " + FALLBACK_WARNING)

    return "\n\n".join(parts)
