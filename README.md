# stockhunt

A quant research pipeline, end to end: fetch bars, score technical rules against
buy-and-hold, price the *selection* of those rules by walk-forward, paper-trade the
survivors through NautilusTrader, and watch the result on a dashboard.

It is built around one idea: **a backtest is a claim, and most claims are the benchmark
in disguise.** Nearly every mechanism here exists to take something away from a result —
a look-ahead, a survivorship gap, a flattering universe, a fill nobody could have got —
and see whether anything is left.

---

## What is in here

| folder | what it does |
|---|---|
| `stockhunt/` | the shared core, and the only real Python package. One definition of Sharpe/CAGR/drawdown, the on-disk position cache, the parallel rule loop, the order ledger, the results store. numpy + pandas only |
| `backtest engine/` | data fetch, integrity checks, the engines, and single-split sweeps. Two vendors live here |
| `walk-forward optimization/` | what prices *selection*: rolling re-fits, variants, pre-registration, the per-asset verdict and the book-level one |
| `strategies/` | 231 TA-Lib rules behind one dispatcher, plus 176 published strategies discovered by a registry. Signal overlays compose on top of any of them |
| `paper trading engine/` | the live desk — a Nautilus sandbox on live bars, plus an optional Alpaca paper mirror so real fills can be compared against sandbox ones |
| `paper api/` | the invitation-only HTTP layer in front of that desk, and the manager desk other people register strategies into |
| `Stockhunt Dashboard/` | the monitor. One builder, two outputs: a served SPA and a single self-contained file |
| `ML/` | machine-learning studies. It owns no scorer — a model's output becomes a position panel and is judged by the same walk-forward machinery as everything else |
| `tests/`, `tools/` | the unit suite (synthetic bars only) and the gates that prove a refactor changed no number |

`CLAUDE.md` at the root is the working guide to all of it, and each folder carries its own
for the structural detail of that stage.

---

## The method, in five rules

**1. A benchmark is valid only if it differs from the strategy in exactly one thing: the
signal.** Universe, membership dates, weighting, rebalancing schedule, fee schedule, fill
timing and cash treatment must be identical on both sides. Whatever is left different gets
attributed to skill, silently, and always in the flattering direction.

**2. Report three numbers, never one.** Does the signal add value, against a matched
basket? Is it worth running, against a real purchasable index ETF? And how much of the
first number is survivorship, measured by putting the delisted names back?

**3. Fill timing is a control, not a detail.** A rule computed from a bar's own close and
then filled at that same close assumes a print you could not have known. The optimistic
and pessimistic bounds are both reported; a result is only safe if it survives the
pessimistic one.

**4. The universe is point-in-time or it is nothing.** Index membership and top-100
membership are two different questions and both are reconstructed per bar. Tradability
screens decide the ETF and crypto sets, because a fund existing is not a fund being
buyable.

**5. Deflate against the search, not against the winner.** A trial ledger records what was
looked at *before* it was scored, and every deflated statistic reports where its trial
count came from.

---

## The vendors, and why there are two

Price data comes from **Twelve Data** for equities, ETFs, crypto and commodities, and from
**Databento** (`GLBX.MDP3`) for CME futures — Twelve Data carries no CME contract at all,
and does not say so: it returns a different instrument that shares the letters.

That failure mode is the recurring theme of the data layer here. A bare ticker is not an
identity; a series can be the right instrument with the wrong history; an intraday bar can
be the right price on the wrong clock. **None of these are findable by a bar-level test** —
the bars are well formed and internally consistent in every case. Each one needed an
external fact to check against, and each of those checks now lives in `check_data.py`.

---

## Running it

Python 3.11+. TA-Lib's Python wrapper needs the compiled TA-Lib C library on the system.

```bash
pip install -e ".[pipeline]"        # the shared core plus what the stages need
pip install -e ".[api]"             # ...and the HTTP layer, if you want the board
```

The unit suite is fast and reads nothing outside itself:

```bash
python -m pytest -q
```

Beyond it are **gates** — `__main__` scripts that exit nonzero, run directly rather than
collected. They prove things a synthetic-bar unit test structurally cannot: that every
published strategy is causal *by truncation* rather than by reading the code, that three
engines agree on the same rule, that a refactor moved no position and no score, that the
significance bar's false-positive rate is actually 5%.

Two rules will bite anyone working in the tree:

- **Run each folder's scripts from that folder.** Folder names contain spaces, so none of
  them can be a Python package, so cross-folder imports are bare-name-on-`sys.path`.
- **Module basenames must be globally unique** across folders that land on `sys.path`
  together. There is exactly one `config.py` in the repo, and each folder's path bootstrap
  is named distinctly for that reason.

---

## Where the results are

**Not in this file, and not in any Markdown here.** Findings live in the `results/` CSVs,
in `results.db`, and on the dashboard, which is where they are read. Anything that would
change if a backtest were re-run tonight belongs there — a number written into
documentation is a number that goes stale silently and gets quoted anyway.

Build and open the dashboard to see what the pipeline has concluded.
