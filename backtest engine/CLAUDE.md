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
config.py        universes, 9 timeframes, per-class cost grids, gates, sys.path
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
   |             overlays/: regime (trailing vol), heikin (`ha:` synthetic candles,
   |             SIGNAL only), chart (`chart:` Pine bar-count windows)
td_loader.py     Twelve Data -> ../data/<stocks|crypto|etfs|commodities>/<tf>/*.parquet
   |             applies BACKTEST_START and the quarantine, once, for the whole repo
   |             span_for(class): the head cut. us_stocks -> membership_span (top-100
   |             record), us_etfs -> etf_entry_span (the liquidity screen), others none
   |             `load` reads EVERY class, whatever the vendor. `fetch` refuses any class
   |             whose spec names another `source`, and any timeframe the vendor has no
   |             product for (`interval: None`)
resample_intraday.py  2m and 3m bars, aggregated from the cached 1m. DERIVED, never
   |             fetched: Twelve Data sells no 2min/3min interval
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

## The 2m and 3m sheets are built here, not bought

Twelve Data serves 1min, 5min, 15min, 30min, 1h, 2h, 4h and 1day, and nothing between
them (30min confirmed by `earliest_timestamp` probe 2026-08-22 and now a `TIMEFRAMES`
row). So `TIMEFRAMES` carries `2m` and `3m` with `interval: None`, `td_loader.fetch`
refuses them by name, and `resample_intraday.py` aggregates them out of the cached 1m. `load` never
knows the difference — same directory layout, same parquet, same single door.

### Intraday timestamps are NOT on one clock across classes (measured 2026-08-23)

Every intraday parquet has a tz-**naive** index, and the wall clock it carries depends on
who sold the bars. On 2025-07-15:

| class | bars/day | first → last | clock |
|---|---|---|---|
| `us_stocks`, `us_etfs` | 390 | 09:30 → 15:59 | **exchange-local (ET)**, regular session only |
| `crypto` | 1440 | 00:00 → 23:59 | UTC |
| `commodities` | 1417 | 00:00 → 23:59 | UTC |
| `cme_futures` | 1380 | 00:00 → 23:59 | **UTC** (`db_intraday.py` writes it) |

Twelve Data returns exchange-local time and Databento returns UTC, and neither stamps the
zone, so nothing in the cache announces the difference. It is invisible at 1d — a daily
bar is a date — and decides the answer at every intraday size:

* **Resampling is per class, never a shared rule.** A 15m or 30m bar cut on UTC boundaries
  is correct for futures, crypto and commodities and wrong for equities, where the session
  starts at 09:30 local and the grid has to start there or every bar straddles two.
* **Slicing equities by a UTC window silently returns the wrong bars.** Checking the 1m
  cache against the daily one with `between_time('13:30','19:59')` — the correct UTC span
  for the US session in summer — put Open out by 60-130 bp while Close stayed at ~1.6 bp,
  which reads like bad data rather than a bad query. Aggregating the whole session instead
  reproduces the daily cache at **0.00 bp on open, high and low**.
* **Close lands 0.9-2.2 bp off the daily bar and that is correct, not drift.** The daily
  Close is the official closing-auction print; the last 1m bar is the last continuous
  trade before it. They are different prices and always will be.

Three properties of that aggregation are load-bearing, and each is pinned in
`tests/test_resample_intraday.py`:

* **windows anchor at midnight and are labelled by their OPEN**, matching the vendor's
  own 1m convention. 09:30 is 570 minutes past midnight and divides by both 2 and 3, so
  every US session's first 2m/3m bar starts exactly at the open;
* **empty windows are dropped, never filled**, so no synthetic bar bridges a weekend or
  an overnight gap. `vector.bars_per_year` measures, so a session's ragged last window
  costs nothing;
* **`sum(min_count=1)` keeps NaN volume NaN.** Crypto's 1m cache carries no volume at
  all, and a plain `.sum()` would launder that into a turnover of zero — which reads to
  every volume-gated rule as a real, quiet market rather than as missing data.

`FLATTEN_EOD_TIMEFRAMES` covers `1m`, `2m`, `3m` and `5m`: at those horizons a rule that
holds overnight is collecting the drift that is 65-95% of US equity return, which is not
what a day-trading rule is claiming to do. It does not apply to crypto, which has no
session to flatten into.

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
- **The GLBX intraday archive folds sessions, and a folded day reconciles perfectly.**
  Databento collapses whole sessions into a handful of bars on scattered days — 2015-01-06
  returns 114 one-minute bars where ~1,380 exist — and **the folded day's volume sums to
  EXACTLY the `ohlcv-1d` volume**. The minutes are not missing, they are folded into the
  bars that survive, so OHLC relations hold, volume ties out, and nothing but the bar
  COUNT can detect it. Treat it as the third member of the family with the foreign
  namesakes and EEM: well formed, and quietly the wrong measurement.

  Re-measured 2026-08-22 (five consecutive weekdays, mid-June, every year), the old
  "before 2013" boundary is wrong in both directions — 2010-06-07 is complete, 2015-01-06
  is not, and `ohlcv-1m` has the same defect as `ohlcv-1h`:

      2010-2012: 1 of 5 complete    2013: 5/5    2014: 3/5    2016-2026: 5/5, every year

  So **`db_loader` ships 1d, and `db_intraday.py` ships 1h and 1m from 2016** — free, and
  screened per session against **that root's own median day**, never an absolute count:
  one UTC day is 23 hourly bars on ES, 19 on the grains and **6** on live cattle, and an
  absolute floor tuned to the equity index silently deleted LE.v.0 in its entirety while
  reading as a clean run for the other fifteen roots. The pre-2016 sample still needs bars
  rebuilt from `trades` (~$100 per root per year); everything from 2016 is $0.00.

- **The intraday path is verified against the daily one, and the join is UTC calendar
  day.** Aggregating `db_intraday`'s hourly bars by UTC day reproduces `data/futures/1d`
  to **0.00 bp on open, high, low and close, volume ratio 1.0000**. Chicago-day grouping
  misses close by 13 bp — and screening on that wrong boundary split sessions across two
  buckets and threw away 16.8% of ZS.v.0. Roll adjustment reads the ratios the daily run
  already wrote to `data/reference/futures_rolls.csv` rather than re-deriving them from a
  second contract rank: half the requests, and one definition of the adjustment instead
  of two that can drift.
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
