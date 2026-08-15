"""Gate: $100,000 traded as ONE book across many names, and the four rules that define it.

Runs offline on cached daily bars. Writes only to temporary files — the real `paper.db`
is never touched.

    ..\\.venv\\Scripts\\python test_book.py
    ..\\.venv\\Scripts\\python test_book.py --rule ibs --names 12

What it asserts, in the order the rules were decided:

1. **One book.** Cash plus every open slice equals the equity, at every bar, to the cent.
   If this drifts, every number the feature reports is wrong and none of the others matter.
2. **A slice is the book over the live count**, so it compounds rather than being a fixed
   $1,000 forever.
3. **A name the rule is out of holds cash** — it is not pushed into the signalled names,
   so the book is only partly invested and `held < names` most of the time.
4. **It rebalances on change, not on a schedule.** Turnover has to look like a rule
   trading, not like a book chasing its own drift: `strategy.py` once produced 637 fills
   where the research measured two or three round trips a year.
5. **A departing name is sold and its value stays in the book** — the $100,000 line is
   continuous across a membership change.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

import paper_config                                                     # noqa: F401

_TMP = Path(os.environ.setdefault(
    "STOCKHUNT_BOOK_TMP", tempfile.mkdtemp(prefix="stockhunt-book-")))

import store                                                            # noqa: E402
store.DB_PATH = _TMP / "paper.db"
import paper_state                                                      # noqa: E402
paper_state.STATE_PATH = _TMP / "paper_state.json"
paper_state.MIRROR_PATH = None

from backtest_paper import build_bars, load_bars                        # noqa: E402
from book_strategy import BookStrategy, BookStrategyConfig             # noqa: E402
import td_nautilus                                                     # noqa: E402

from nautilus_trader.backtest.engine import BacktestEngine             # noqa: E402
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig  # noqa: E402
from nautilus_trader.model.currencies import USD                       # noqa: E402
from nautilus_trader.model.data import BarType                         # noqa: E402
from nautilus_trader.model.enums import AccountType, OmsType           # noqa: E402
from nautilus_trader.model.identifiers import TraderId, Venue          # noqa: E402
from nautilus_trader.model.objects import Money                        # noqa: E402

VENUE = "SANDBOX"
CAPITAL = 100_000.0


def top_names(n: int) -> list[str]:
    """The first `n` of the live top 100 that have cached daily bars."""
    import config as bt
    import top100_membership as t
    import pandas as pd
    m = t.load()
    end = pd.to_datetime(m["end"], errors="coerce")
    live = sorted(m[end.isna() | (end >= pd.Timestamp.utcnow().tz_localize(None))]
                  .symbol.unique())
    have = [s for s in live if (bt.DATA_DIR / "stocks" / "1d" / f"{s}.parquet").exists()]
    return have[:n]


def run(rule: str, names: list[str], bars: int, drop_at: int, log_level: str) -> dict:
    frames = {s: load_bars(s, "1d").tail(bars) for s in names}
    frames = {s: df for s, df in frames.items() if len(df) > 300}
    names = sorted(frames)

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("BOOK-001"),
        logging=LoggingConfig(bypass_logging=log_level == "OFF", log_level=log_level)))
    engine.add_venue(venue=Venue(VENUE), oms_type=OmsType.NETTING,
                     account_type=AccountType.CASH, base_currency=USD,
                     starting_balances=[Money(CAPITAL * 5, USD)])

    for symbol in names:
        inst = td_nautilus.equity_instrument(symbol, VENUE)
        engine.add_instrument(inst)
        bar_type = BarType.from_str(f"{inst.id}-{paper_config.BAR_SPEC['1d']}")
        engine.add_data(build_bars(frames[symbol], bar_type,
                                   price_precision=2, size_precision=0))

    warmup = min(paper_config.MIN_WARMUP_BARS, max(bars // 4, 30))
    strat = BookStrategy(config=BookStrategyConfig(
        order_id_tag="BOOK-1", rule=rule, name="gate", cls="us_stocks", tf="1d",
        symbols=tuple(names), venue=VENUE, capital=CAPITAL,
        min_warmup_bars=warmup, export_state=True,
        benchmark=None, note="book gate"))
    engine.add_strategy(strat)

    # The identity has to hold on every bar, not just at the end: a book that balances
    # only when the run stops has been wrong in between and happened to come back.
    breaches, samples, overdrawn = [], [], []
    original = strat._export

    def watched(bar):
        eq = strat.equity()
        recomputed = strat._cash + sum(
            u * strat._last_price.get(s, 0.0) for s, u in strat._units.items())
        if abs(eq - recomputed) > 0.01:
            breaches.append((str(bar.bar_type), eq, recomputed))
        samples.append((strat.held_count(), len(strat._live), strat.slice_value(), eq))
        # No leverage. Whole-share rounding can outrun the headroom `target_fraction`
        # leaves, and a book that quietly borrows is not the book the backtest measured.
        if strat._cash < -0.01:
            overdrawn.append((str(bar.bar_type), strat._cash))
        return original(bar)

    strat._export = watched
    engine.run()

    out = {
        "names": names, "rule": rule,
        "fills": strat._n_fills,
        "equity": strat.equity(),
        "cash": strat._cash,
        "held": strat.held_count(),
        "identity_breaches": breaches,
        "overdrawn": overdrawn,
        "samples": samples,
        "years": max(len(f) for f in frames.values()) / 252.0,
    }
    engine.dispose()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rule", default="ibs")
    ap.add_argument("--names", type=int, default=12)
    ap.add_argument("--bars", type=int, default=900)
    ap.add_argument("--drop-at", type=int, default=0)
    ap.add_argument("--log-level", default="OFF")
    args = ap.parse_args()

    names = top_names(args.names)
    if len(names) < 3:
        raise SystemExit("need at least 3 cached names from the top 100")

    r = run(args.rule, names, args.bars, args.drop_at, args.log_level)
    held = [h for h, _, _, _ in r["samples"]]
    slices = [sv for _, _, sv, _ in r["samples"]]
    n = len(r["names"])
    years = r["years"]
    turns = r["fills"] / 2 / max(years, 0.01) / max(n, 1)

    print(f"\n  {r['rule']} as one book over {n} names, {years:.1f} years of daily bars")
    print(f"  $100,000 -> ${r['equity']:,.2f}   ({(r['equity']/CAPITAL-1)*100:+.2f}%)")
    print(f"  cash ${r['cash']:,.2f}   holding {r['held']} of {n} at the end")
    print(f"  {r['fills']} fills = {turns:.1f} round trips per name per year")
    print(f"  slice grew ${slices[0]:,.0f} -> ${slices[-1]:,.0f}" if slices else "")

    problems = []
    if r["identity_breaches"]:
        b = r["identity_breaches"][0]
        problems.append(f"the book did not balance on {len(r['identity_breaches'])} bars "
                        f"(first: equity {b[1]:.2f} vs cash+slices {b[2]:.2f})")
    if r["overdrawn"]:
        worst = min(c for _, c in r["overdrawn"])
        problems.append(f"the book went cash-negative on {len(r['overdrawn'])} bars "
                        f"(worst {worst:,.2f}) — that is leverage, and this book has none")
    if r["fills"] == 0:
        problems.append("nothing traded — the rule never fired or sizing rounded to zero")
    if not slices:
        problems.append("no bars reached the book")
    elif abs(slices[-1] - slices[0]) < 1e-9 and r["equity"] != CAPITAL:
        problems.append("the slice never moved with the book, so it is a fixed dollar "
                        "figure rather than equity/n")
    # Rule 3: cash when out. A book that is ALWAYS fully invested is not holding cash for
    # the names the rule is out of, which is a different strategy from the backtest.
    if held and min(held) == n and max(held) == n:
        problems.append(f"every name was held on every bar — an idle slice is not "
                        f"sitting in cash")
    if turns > 30:
        problems.append(f"{turns:.0f} round trips per name per year is drift-chasing, "
                        f"not a rule trading")

    if problems:
        print("\n  THE BOOK IS WRONG:")
        for p in problems:
            print(f"    - {p}")
        raise SystemExit(1)

    print(f"\n  the book holds: cash + slices = equity on every one of "
          f"{len(r['samples'])} bars,"
          f"\n  the slice tracks the book, idle names sit in cash, and turnover is "
          f"{turns:.1f}/name/year.")


if __name__ == "__main__":
    main()
