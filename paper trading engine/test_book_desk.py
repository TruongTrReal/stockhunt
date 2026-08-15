r"""Gate: a registration becomes a live book, through the real DeskController.

`test_book.py` proves the accounting. This proves the PATH — the thing a click on the
backtest page actually triggers:

    a row in desk.db  ->  DeskController reconciles it
                      ->  resolves the class universe LIVE (the top 100 today)
                      ->  attaches a BookStrategy to a running trader
                      ->  it trades, and the registration is marked live

Offline, on cached daily bars, into temporary databases. Run from this directory::

    ..\.venv\Scripts\python test_book_desk.py
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import paper_config

_TMP = Path(os.environ.setdefault(
    "STOCKHUNT_BOOKDESK_TMP", tempfile.mkdtemp(prefix="stockhunt-bookdesk-")))

import store                                                            # noqa: E402
store.DB_PATH = _TMP / "paper.db"
import paper_state                                                      # noqa: E402
paper_state.STATE_PATH = _TMP / "paper_state.json"
paper_state.MIRROR_PATH = None
from stockhunt import deskdb                                            # noqa: E402
deskdb.use(_TMP / "desk.db")

from backtest_paper import build_bars, load_bars                        # noqa: E402
from book_strategy import BookStrategy                                  # noqa: E402
from desk_control import DeskController, DeskControllerConfig           # noqa: E402
import td_nautilus                                                      # noqa: E402

from nautilus_trader.backtest.engine import BacktestEngine              # noqa: E402
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig  # noqa: E402
from nautilus_trader.model.currencies import USD                        # noqa: E402
from nautilus_trader.model.data import Bar, BarType                     # noqa: E402
from nautilus_trader.model.enums import AccountType, OmsType            # noqa: E402
from nautilus_trader.model.identifiers import TraderId, Venue           # noqa: E402
from nautilus_trader.model.objects import Money                         # noqa: E402
from nautilus_trader.trading.config import ImportableControllerConfig   # noqa: E402

VENUE = "SANDBOX"
RULE = "ibs"
SID = f"str_00_us_stocks-1d-{RULE}"


class DeskCfg(DeskControllerConfig, frozen=True):
    tick_every_bars: int = 40
    # Carried in the CONFIG, not a module global. `ImportableControllerConfig` resolves
    # the controller by importing this file, so it runs as a SECOND module object while
    # `__main__` is still live — a global set in `main()` is empty over here. The config
    # is the one thing that crosses that boundary.
    bar_type: str = ""


class ScriptedDesk(DeskController):
    """The real controller, ticked by bars instead of a wall clock — a TestClock and a
    LiveClock disagree about timers, and a gate that depends on that tests Nautilus's
    scheduler rather than this desk."""

    def __init__(self, config: DeskCfg, trader) -> None:
        super().__init__(config=config, trader=trader)
        self.seen = 0

    def on_start(self) -> None:
        deskdb.connect()
        self.tick()
        self.subscribe_bars(BarType.from_str(self.config.bar_type))

    def on_bar(self, bar: Bar) -> None:
        self.seen += 1
        if self.seen % self.config.tick_every_bars == 0:
            self.tick()


def main() -> None:
    # Names the desk would really hold, intersected with what is cached.
    import config as bt
    live = paper_config.book_universe("us_stocks")
    names = [s for s in live
             if (bt.DATA_DIR / "stocks" / "1d" / f"{s}.parquet").exists()][:6]
    frames = {s: load_bars(s, "1d").tail(700) for s in names}
    frames = {s: f for s, f in frames.items() if len(f) > 300}
    names = sorted(frames)

    deskdb.register("00", f"us_stocks-1d-{RULE}", "us_stocks", [], "1d",
                    paper_config.BOOK_CAPITAL, kind="book", rule=RULE, benchmark=None)

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("BOOKDESK-1"),
        logging=LoggingConfig(bypass_logging=True),
        controller=ImportableControllerConfig(
            controller_path="test_book_desk:ScriptedDesk",
            config_path="test_book_desk:DeskCfg",
            config={"export_state": True, "tick_every_bars": 40,
                    "bar_type": f"{td_nautilus.equity_instrument(names[0], VENUE).id}"
                                f"-{paper_config.BAR_SPEC['1d']}"})))
    engine.add_venue(venue=Venue(VENUE), oms_type=OmsType.NETTING,
                     account_type=AccountType.CASH, base_currency=USD,
                     starting_balances=[Money(1_000_000, USD)])
    for s in names:
        inst = td_nautilus.equity_instrument(s, VENUE)
        engine.add_instrument(inst)
        bt_ = BarType.from_str(f"{inst.id}-{paper_config.BAR_SPEC['1d']}")
        engine.add_data(build_bars(frames[s], bt_, price_precision=2, size_precision=0))
    engine.run()

    ctl = engine.kernel._controller
    strat = ctl._running.get(SID) if ctl else None
    reg = deskdb.registration(SID)

    print(f"\n  registration : {reg['state']}   kind={reg['kind']}   "
          f"reason={reg['reason'] or '—'}")
    print(f"  attached     : {isinstance(strat, BookStrategy)}")
    problems = []
    if reg["state"] != "live":
        problems.append(f"the desk did not start it (state={reg['state']}, "
                        f"reason={reg['reason']})")
    if not isinstance(strat, BookStrategy):
        problems.append("no BookStrategy was attached to the running trader")
    else:
        print(f"  universe     : {len(strat._live)} names resolved live from the top 100")
        print(f"  traded       : {strat._n_fills} fills over {len(names)} cached names")
        print(f"  $100,000 ->   ${strat.equity():,.2f}   "
              f"holding {strat.held_count()} of {len(strat._live)}")
        if len(strat._live) != len(paper_config.book_universe("us_stocks")):
            problems.append("the book's universe is not the live class universe")
        if strat._n_fills == 0:
            problems.append("the book never traded")
        if abs(strat.equity() - (strat._cash + sum(
                u * strat._last_price.get(s, 0.0)
                for s, u in strat._units.items()))) > 0.01:
            problems.append("cash + slices does not equal equity")

    engine.dispose()
    if problems:
        print("\n  THE BOOK PATH IS BROKEN:")
        for p in problems:
            print(f"    - {p}")
        raise SystemExit(1)
    print("\n  a registration became a live book on a running desk, and it traded.")


if __name__ == "__main__":
    main()
