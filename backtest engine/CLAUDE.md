# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this directory. Read `../CLAUDE.md`
first. **No results here** — the `results/` CSVs and the dashboard own those.

## What this is

The machinery: universes, data fetching and integrity, the signal layer, three backtest
engines and the parity harness that makes them check each other, plus the single-split
sweeps and the static HTML report.

Everything that prices **selection** lives next door in `../walk-forward optimization/` —
stages 1b through 2b, and the book run that draws the dashboard's curves. That is where the
headline numbers come from.

## Setup and commands

The venv is one level up and shared with the sibling studies:

```powershell
..\.venv\Scripts\Activate.ps1
```

TA-Lib's Python wrapper needs the compiled TA-Lib C library already on the system. The
Twelve Data key comes from `TWELVEDATA_API_KEY`, falling back to `../.env.local`.

Run everything **from this directory** — modules import each other by bare name:

```powershell
python td_loader.py                       # fetch everything (~5h, ~13k credits)
python td_loader.py --class crypto --tf 1d
python db_loader.py --check               # CME futures: price the pull, download nothing
python db_loader.py                       # ...and fetch it (daily, 2010-06-06 on)
python futures_screen.py                  # which CME roots are tradable and independent
python futures_screen.py --write          # ...and commit that to universes_futures.py
python sp500_membership.py --probe        # point-in-time membership + priceability
python top100_membership.py               # the top 100 of it, point-in-time
python top100_membership.py --show 2008   # ...and who that was in a given year
python universe_screen.py                 # which ETFs and pairs are tradable, and when
python universe_screen.py --write         # ...and commit that to etf_entry.csv
python factors.py                         # Fama-French daily -> ../data/reference/
python check_data.py --fix                # OHLC integrity scan + repair
python check_data.py --probe-listing      # is each ticker even the US company? (network)
python check_data.py --class us_stocks --tf 1d
python parity.py --n 3                    # three-engine cross-check (gate on this)
python sweep.py --class us_stocks         # stage 1: singles
python combo_sweep.py                     # stage 2/3: single-split pairs (legacy)
python validate.py                        # Nautilus on survivors (if any)
python build_payload.py                   # results -> report/report_payload.json
python build_report.py                    # -> report/index.html
python build_report.py --demo             # synthetic payload, layout check only
```

**Background jobs get killed at 10 minutes** by the harness timeout. Long fetches and sweeps
must be launched detached (`Start-Process ... -WindowStyle Hidden`) with output redirected
to `logs/*.log`.

## Architecture

```
config.py        universes, 7 timeframes, per-class cost grids, gates, sys.path
   |             US_STOCKS is the POINT-IN-TIME TOP 100 (the 751-name S&P universe is
   |             kept as SP500_UNIVERSE, the 20 mega-caps as MEGA20)
   |             US_ETFS is ETF_TOP10 and CRYPTO is CRYPTO_TOP20 (the 65- and 34-name
   |             lists are kept as ETF_ALL65 and CRYPTO_ALL34)
   |             BACKTEST_START, MIN_PRICE_USD, FEE_SCENARIOS, HEADLINE_SCENARIO
universes.py     GENERATED: 503 current + 248 priceable departed S&P names
sp500_membership.py  point-in-time index membership from the Wikipedia changelog
universes_top100.py  GENERATED: the 216 names that ever held a top-100 slot
top100_membership.py the top 100 OF that index, ranked per date on trailing dollar
   |             volume. Annual re-rank, 120-rank buffer. -> top100_membership.csv
universe_screen.py   the ETF and crypto equivalent: which of the 65 funds and 34 pairs
   |             can actually be TRADED, and from when. -> universe_screen_<class>.csv
   |             and ../data/reference/etf_entry.csv
factors.py       Fama-French daily + momentum + short-term reversal, Newey-West OLS
   |
../strategies/   talib_signals.py (231 rules) | registry.py (31 published strategies)
   |             overlays/regime.py: trailing-vol conditioning over any base rule
td_loader.py     Twelve Data -> ../data/<stocks|crypto|etfs|commodities>/<tf>/*.parquet
   |             applies BACKTEST_START and the quarantine, once, for the whole repo
   |             span_for(class): the head cut. us_stocks -> membership_span (top-100
   |             record), us_etfs -> etf_entry_span (the liquidity screen), others none
   |             `load` reads EVERY class, whatever the vendor. `fetch` refuses any class
   |             whose spec names another `source`
db_loader.py     Databento GLBX.MDP3 -> ../data/futures/1d/*.parquet. THE SECOND VENDOR,
   |             and the only one. Continuous contracts, Sunday stubs merged into the
   |             session they open, ratio back-adjusted across rolls, every request
   |             costed before it is made -> ../data/reference/futures_rolls.csv
futures_specs.py what one CME contract is WORTH: multiplier, quote scale, tick, sector.
   |             A price alone says nothing here -- ZC at 438 is $21,912
futures_screen.py the CME equivalent of universe_screen: liquidity, tradable years,
   |             price grid, and a correlation gate no other class needs
check_data.py    OHLC integrity scan, repair, and the quarantine rules
   |
signals.py       the ONE way a rule name becomes a position series
   |
engines/         vector.py | reference.py | nautilus.py
parity.py        samples cells, runs all three, FAILS on disagreement
   |
sweep.py         stage 1: 231 singles x assets x timeframes x costs
combo_sweep.py   stage 2/3: pairs of train-shortlisted singles, 4 operators (legacy split)
metrics.py       IR, the edge standard, deflated Sharpe, breakeven, leave-one-out
   |
   +--> ../walk-forward optimization/   stages 1b-2b: everything that prices SELECTION
   |
validate.py      Nautilus on survivors: whole shares, commission, slippage
build_payload.py results -> report/report_payload.json
build_report.py  template.html + report.js + payload -> report/index.html
```

## Three modules are load-bearing beyond what they look like

**`config.py` is a path bootstrap.** It prepends the **repo root** to `sys.path`, which is
the only reason `from strategies.talib_signals import ...` resolves. The signal layer and
the published-strategy catalog live in `../strategies/`, a real package shared with the
walk-forward stage, the paper desk and the dashboard.

It used to prepend `../test research/src` instead, which made a frozen study a runtime
dependency of live code. `strategies/talib_signals.py` is now a copy of that file and the
original stays where it is because 17 modules inside the locked folder still import it. See
`../LOCKED.md`.

**`signals.py` is the single source of positions.** `parity.py`, `sweep.py`,
`combo_sweep.py` and `validate.py` must all produce byte-identical positions for the same
cell, or the parity harness is checking the wrong thing. Benchmark plumbing (BETA, CORREL),
the NaN policy and end-of-day flattening live there and nowhere else.

**`td_loader.load` is where the sample is defined.** `config.BACKTEST_START` and the
quarantine list are applied there and nowhere else, so one cut is the whole pipeline
agreeing on a window. `check_data.py` is the deliberate exception — it passes
`skip_quarantined=False` because it has to see the bars it is judging.

## Where a verdict may be computed

Nowhere in this folder. `metrics.aggregate` emits diagnostics only. The six criteria need
matched sizing, per-fold Sharpe and two signal-free controls, none of which the IR sweeps
have.

`metrics.apply_edge_standard` is the single **definition** — thresholds, the Bonferroni
correction on `t`, the rankability preconditions — and it lives here so that both stages
that can supply those inputs call the same function rather than two copies of it. Two do:

| who | over what | writes |
|---|---|---|
| `../walk-forward optimization/riskmatch_wf.py` | the **median asset**, risk-matched | `edge_standard.csv` |
| `../walk-forward optimization/portfolio_wf.py` | the **book**, one account | `book_<class>_<tf>.csv` |

They disagree on rows, legitimately — a book is steadier than any of its names. The
dashboard shows the book's. Changing a threshold means changing `config.EDGE_STANDARD`
once; adding a third caller means feeding it the same six inputs, never re-deriving them.

## Mechanics that will bite you

- **Twelve Data serves no volume for crypto** — the field is absent, not zero. AD, ADOSC,
  MFI and OBV cannot be evaluated on that class. `signals.usable_rules` skips and counts
  them; they are never fed NaN, because a volume rule on NaN produces a flat position that is
  indistinguishable on a leaderboard from a rule that does nothing.
- **A bare symbol is not an identity, and the vendor substitutes rather than failing.**
  `td_loader._request` now pins `country=United States` for `US_LISTED_CLASSES`, which
  makes Twelve Data return `status: error` for a ticker it has no US listing for instead
  of serving a foreign namesake. Before that pin, **85 of 739** cached `us_stocks` series
  were a different company for their entire length — `CTRA` was Ciputra Development Tbk PT
  on the Indonesia Stock Exchange, and it ranked as the 3rd largest US stock of 2026.
  `check_data.py --probe-listing` is the check on data already on disk; it caches a
  verdict per symbol and `check_data.wrong_instrument_reason` quarantines from that cache
  offline. **No bar-level test can find these** — the series is internally consistent, its
  highs bracket its lows, it has no 10x bar, and it turns over billions a day. The
  liquidity floor catches an impostor that is THIN; this catches one that is FAT.
  `sp500_membership.probe_priceable` still sends a bare symbol, and its answer is
  therefore a claim about the ticker rather than about the company.
- **There are two vendors now, and only one of them may be asked for futures.** Twelve
  Data carries no CME contract at all, and — exactly as with the foreign namesakes above —
  it does not answer "no". `CL` there is Colgate-Palmolive and `ES` is Eversource Energy,
  returned as full, clean, plausible equity series. `CLASSES["cme_futures"]["source"]`
  says `databento` and `td_loader.fetch` raises on it rather than trusting anyone to
  remember. `td_loader.load` still reads the class, because the cache format is identical
  and the single-door rule matters more than which vendor filled it.
- **A CME symbol here is `ES.v.0`, not `ES`.** Root, roll rule, rank — the vendor's own
  continuous symbology, kept as the project spelling. `config.class_of` returns the first
  class that claims a symbol, and `CL` is already a member of `US_STOCKS`, so a bare root
  would have resolved crude oil to a toothpaste company silently. It also says the true
  thing about the series: there is no instrument called "ES", only a rule for picking
  which contract to hold.
- **The futures class is 1d only, and that is a vendor defect, not a choice.** Databento's
  hourly archive for GLBX collapses whole sessions into one or two bars before 2013 —
  2015-01-06 returns 2 hourly bars whose volume sums to the full day's 2,344,424, June
  2011 returns 230 where ~500 exist. `ohlcv-1d` over the same days is complete and ties
  out to the hourly sum exactly. Anything cut from hourly would be wrong over the first
  third of the sample; a 4h sheet needs bars rebuilt from `trades`, which is complete and
  is metered at ~$100 per root per year before 2026.
- **Run `check_data.py --fix` after any fetch, before sweeping.** A scoped
  `--class X --tf Y` merges into `quarantine.csv` rather than rewriting it; rows outside the
  scanned scope are preserved, rows inside are re-derived so a repaired symbol can leave.
- **Nautilus parity runs at zero cost only.** A venue fee is charged on traded notional;
  this project's cost model charges on target change. Under constant-fraction rebalancing
  those legitimately differ. Cost modelling is verified between `vector` and `reference`
  across the whole grid instead.
- **Nautilus parity tolerance scales with sqrt(fills), not with equity.** Every fill rounds
  cash to the cent and a short re-sizes every bar — one 7k-bar cell produced 5,355 fills. A
  flat tolerance produces false failures; see the note in `parity.py`.
- **`parity.py --n 5` currently exits nonzero**, and `vector-nautilus` is red on some cells.
  Both are known and deliberate — a real short-annihilation disagreement in the first case,
  fill-count rounding in the second. `--n 3`/`--n 4` do not sample the failing cell.
- `report/index.html` is generated. Edit `template.html`, `report.js` or `build_payload.py`
  — never `index.html`, which is overwritten every build.
- **The report is forced to pure ASCII** by `build_report.py`, with different escaping per
  region (character references in HTML, backslash escapes in JS, and CSS `content:` must be
  ASCII outright). A raw `->` in the source renders as mojibake in any context where the
  charset cannot be declared.
