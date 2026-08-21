# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working anywhere in this repo.

**This file describes how the repo works, never what it found.** No results here — no
findings, no leaderboard numbers, no IR/Sharpe/CAGR figures, nothing that would change if a
backtest were re-run tonight. Those live in the `results/` CSVs, and the dashboard is where
they are read. Do not add a number here.

## What this is

A quant research pipeline: fetch bars, score technical rules against buy-and-hold, price
the *selection* of those rules by walk-forward, paper-trade the survivors through
NautilusTrader, and watch the result on a dashboard.

Build and open the dashboard to see what the pipeline has concluded.

## How to talk to me about it

After any long piece of work, **explain the outcome like I'm five.** Short, plain words, no
jargon. A few sentences is enough. If a number matters, say what it means rather than what
it is called. The detail belongs in the files and on the dashboard, not in the reply.

## Layout

```
stockhunt/                  THE SHARED CORE. a real package (no space in the name), so
   |                        it imports normally. numpy + pandas only, and it may never
   |                        import from a pipeline folder — the dependency runs one way
   |                        stats.py     one definition of sharpe/cagr/max_drawdown
   |                        poscache.py  on-disk position cache, fingerprinted
   |                        parallel.py  the rule loop across cores
   |                        paths.py     repo paths, mutates no sys.path
   |                        deskdb.py    THE ORDER LEDGER between `paper api/` and the
   |                                     desk. Both open it; stdlib only, so importing
   |                                     it cannot drag the trading stack into the API
   |                        pyproject.toml at the repo root packages THIS and nothing
   |                        else -- the pipeline folders have spaces and are scripts
tools/                      golden.py    hash positions before/after a refactor
   |                        test_stats_equivalence.py
tests/                      the unit suite. synthetic bars only -- no data/, no vendor,
   |                        no result CSV. conftest.py does the path bootstrap once
data/                       every price bar, shared. stocks/ crypto/ etfs/ commodities/
   |                        futures/ CME continuous contracts, the one class that does
   |                        NOT come from Twelve Data
   |                        2m/ and 3m/ are RESAMPLED from 1m by
   |                        `backtest engine/resample_intraday.py` -- the vendor sells
   |                        no such interval, so they are derived, never fetched
   |                        rates/ DTB3 T-bill path
   |                        reference/ sp500 membership, Fama-French factors, quarantine,
   |                        trials ledger, futures roll ledger
strategies/                 talib_signals.py (231 rules, ONE dispatcher) +
   |                        published/ ONE FILE PER STRATEGY (174), discovered by
   |                        registry.py. tests/test_causality.py gates them all
   |                        overlays/ wrap any base label and compose: regime
   |                        (trailing vol), heikin (`ha:`, synthetic candles for the
   |                        SIGNAL ONLY -- fills stay on real prices), chart
   |                        (`chart:`, Pine BAR-COUNT windows instead of day spans)
   |
backtest engine/            the machinery: engines, signals, metrics, td_loader, parity
   |                        db_loader.py + futures_specs.py + futures_screen.py: the CME
   |                        FUTURES class, and the second vendor. Databento GLBX.MDP3
   |                        universes.py + sp500_membership.py: who is in the INDEX
   |                        universes_top100.py + top100_membership.py: WHO IS IN THE
   |                        UNIVERSE -- the point-in-time top 100, which is what
   |                        `config.US_STOCKS` now is
   |                        factors.py: Fama-French daily + Newey-West OLS
   |                        -> results/ single-split sweeps, combos, parity, validation
walk-forward optimization/  what prices SELECTION: rolling re-fit, variants, prereg
   |                        riskmatch_wf.py: the verdict PER ASSET -> edge_standard.csv
   |                        portfolio_wf.py: the same six criteria on the BOOK, which
   |                        is what the dashboard shows -- the book, not the
   |                        median of its parts
   |                        -> results/ wf_* cwf_* var_* prereg_* strat_* book_*
   |                                    edge_standard.csv
paper trading engine/       the live desk: Nautilus sandbox on live Twelve Data bars
   |                        top 3 rules x 4 classes x 2 timeframes, each leg selecting
   |                        from its OWN wf_summary sheet -> publishes live.json
   |                        ALSO runs other people's strategies: desk_control is a
   |                        Nautilus Controller that attaches registrations to the
   |                        RUNNING node and drains their orders. catalog.py publishes
   |                        which backtested rules may be promoted onto the desk
paper api/                  the invitation-only HTTP layer in front of that desk. auth
   |                        (email + one-time code, allowlist only) + the DASHBOARD
   |                        behind it -- this is how the board is shared now, since
   |                        serve.py has no login and is loopback-only. now also the
   |                        MANAGER DESK: /v1/strategies, /v1/orders, /v1/house.
   |                        joining is a five-step wizard, and /desk/agent.md is the
   |                        ONE integration contract -- docs.html only renders it.
   |                        /v1/webhook/tradingview is the ONE route with no auth
   |                        HEADER: an alert cannot send one, so it carries a
   |                        per-strategy secret in the body instead
   |                        owns no trading and never will: it writes requests, the
   |                        desk acts on them
Stockhunt Dashboard/        the monitor. one builder, two outputs (served SPA + one file)

engine-bakeoff/             reference vs nautilus vs manifoldbt; yfinance vs Twelve Data
test research/              LOCKED - study 1, S&P-wide daily
top 20 stocks/              LOCKED - study 2, 20 mega-caps at 1d/1h/5m
AI generated strategies/    empty
```

`test research/` and `top 20 stocks/` are **frozen** — `.claude/settings.json` denies writes
to both. Read `LOCKED.md` before trying to change anything there.

## Environments

| venv | used by |
|---|---|
| `.venv` | everything, including the paper desk (it has `nautilus_trader` and `websockets`) |
| `.venv-bakeoff`, `.venv-nautilus` | `engine-bakeoff/` only, one per engine under test |

TA-Lib's Python wrapper needs the compiled TA-Lib C library on the system. The Twelve Data
key comes from `TWELVEDATA_API_KEY`, falling back to `.env.local` at the repo root.

## Two rules that will bite you

**1. Run each folder's scripts from that folder.** Folder names contain spaces, so none of
them can ever be a Python package, so cross-folder imports are bare-name-on-`sys.path`
forever.

**2. Module basenames must be globally unique** across every folder that lands on
`sys.path` together. Each folder's path bootstrap is therefore named distinctly, and there
is exactly one `config.py` in the repo:

| folder | bootstrap | why not `config.py` |
|---|---|---|
| `backtest engine/` | `config.py` | it *is* the one |
| `walk-forward optimization/` | `wfo_paths.py` | would shadow the engine's `config` |
| `paper trading engine/` | `paper_config.py` | same |
| `Stockhunt Dashboard/` | `dash_config.py` | same |
| `paper api/` | `api_paths.py` | same — and it is the one bootstrap that imports **no** trading code, so the HTTP layer starts and tests without the engine |

The chain is three hops and the order matters: a bootstrap puts `backtest engine/` on the
path, importing its `config` puts the **repo root** on the path, and that is the only reason
`strategies.talib_signals` resolves. Import the bootstrap first.

## Speed, and the two switches that turn it off

The rule loop runs across cores and generated positions are cached on disk. Both are
transparent — same numbers, verified — and both can be disabled:

```powershell
$env:STOCKHUNT_WORKERS = 1        # force the serial path
$env:STOCKHUNT_NO_POSCACHE = 1    # regenerate every position from the bars
```

| stage | was | now | why |
|---|---|---|---|
| `sweep.py` us_stocks 1d | 628s | **67s** cold, 52s warm | 10 workers + cache |
| `walkforward.py` us_stocks 1d | 1316s | **237s** | 10 workers + cache |
| `strat_wf.py` us_stocks 1d | 879s | **120s** cold, 96s warm | 10 workers + cache, and the stitching phase stopped generating every picked position twice |
| `strat_wf.py --rules ibs` us_stocks 1d | — | **32s** | one strategy's cells instead of 117 |

They bind only on the stages that go through the cache — `sweep.py`, `walkforward.py` and
`strat_wf.py`. `riskmatch_wf` and `portfolio_wf` call `signals.position_for` directly and
run the same code path either way.

Two things make the speedup safe, and neither is optional when changing this machinery:

**`tools/golden.py`.** Hash 1,840 (sheet, rule) position-and-score digests, refactor, then
require every one to be identical. Capture *before* touching anything:

```powershell
python tools\golden.py capture      # ~45s, before
python tools\golden.py verify       # after; exits nonzero on any drift
python tools\golden.py verify --per-symbol    # narrow a failure to one symbol
```

It covers the 231 TA-Lib rules; it does **not** cover `strategies/published/`.

**The cache is fingerprinted, not trusted.** Its key is a SHA-256 over the OHLCV bytes of
each symbol *and* over the source of `strategies/**`, `signals.py` and `engines/vector.py`.
Edit a rule and every entry it could reach is invalidated automatically; refetch one ticker
and only that ticker's entries go. There is no mtime path and no manual clear, because a
cache you have to remember to clear is one that will eventually be wrong.

**The code half of that key is coarse, and it costs.** It hashes `strategies/` whole, so
editing anything in that folder — a new file in `published/`, a docstring in
`scaffold.py`, which is not a rule at all — invalidates every entry for every class and
timeframe, including the 231 TA-Lib rules whose meaning cannot have changed. Adding a
strategy therefore makes the *next* `sweep.py` and `walkforward.py` run cold. Nothing is
wrong when that happens; it is over-invalidation, not staleness. Superseded fingerprint
trees under `.cache/positions/` are unreachable and can be deleted at will. It lives in
`.cache/` (gitignored) and is ~113 MB for the whole us_stocks 1d sheet.

`signals.rule_positions(rule, data, ...)` is the cached entry point — one rule across all
symbols, which is how every stage loops. `signals.position_for` is the single uncached
definition and remains the thing parity gates on.

## What the universe is, and the two layers that decide it

`config.US_STOCKS` is the **point-in-time top 100 US stocks**. Two layers stack, and they
answer different questions:

| layer | question | built by |
|---|---|---|
| `sp500_membership` | was this name in the INDEX on that date? | Wikipedia changelog, walked backwards |
| `top100_membership` | was it one of the hundred LARGEST on that date? | trailing 252-day median dollar volume among that date's members, re-ranked each January, 120-rank buffer on incumbents |

216 names have held a slot; ~100 are live on any bar. The union is what gets *fetched*;
who is *held* is decided per bar, by `top100_membership.load()` at book level and by
`td_loader.membership_span` everywhere else.

**It is ranked on dollar volume, not market cap, and that substitution is forced.** This
repo has no historical shares-outstanding series and the vendor does not sell one, so a
true point-in-time market cap is not computable here. Dollar volume is a liquidity ranking:
it favours high-turnover names and penalises quiet giants, and every result on this
universe should be read as "the 100 most heavily traded US listings", which is not the same
sentence as "the 100 biggest companies". Say the first one.

Three universes have now been live, and a number is only comparable inside one:

    until 2026-08-09   MEGA20, 20 names chosen for being large TODAY
    2026-08-09..08-12  SP500_UNIVERSE, 751 point-in-time S&P names
    from 2026-08-12    TOP100_ALL, the point-in-time top 100

All three are still named in `config`, so an old sheet is regenerable rather than merely
untrusted. The superseded `us_stocks` results live under `results/_archive/sp500_500/`
and `results/_archive/mega20/`; read the README in the first before quoting anything
from it.

### The other two classes are screened on whether they can be TRADED

`universe_screen.py` is to `us_etfs` and `crypto` what `top100_membership.py` is to
`us_stocks`: it decides who is in, and — for the ETFs — from when. `US_ETFS` is now
`ETF_TOP10` (from 65) and `CRYPTO` is `CRYPTO_TOP20` (from 34); `ETF_ALL65` and
`CRYPTO_ALL34` stay named, and the old sheets are under
`results/_archive/etf65_crypto34/` with a README.

    until 2026-08-12   ETF_ALL65, 65 funds held whole; CRYPTO_ALL34, 34 pairs
    from 2026-08-12    ETF_TOP10 with per-fund liquidity entry; CRYPTO_TOP20

Three things the screen is built around, each of which had been silently wrong:

**A fund existing is not a fund being buyable.** The nine original sector SPDRs listed in
December 1998 and then traded under $2M/day until roughly 2004 — XLU's worst year is
$0.1M/day. So an ETF now enters on the date its trailing-252-bar median dollar volume
first clears $20M and never falls back, and `td_loader.span_for` cuts its earlier bars.
That is the same head cut `membership_span` applies to `us_stocks`, for the same reason,
and it removes up to 6.5 years per name. **Crypto gets no such cut**: the vendor serves no
volume for the class, so there is no turnover series to gate on and the screen can only
keep a pair or drop it whole.

**"Listed by 2000" is the wrong history gate once that entry cut exists.** It admits XLV
— listed 1998, unbuyable until 2006 — and rejects TLT, buyable within a year of listing.
The gate is 20 tradable years, measured after the cut, and the swap is not cosmetic:
ranked on listing date the ten are all US equity beta at mean pairwise correlation 0.72;
ranked on tradable years, four of the ten are not equities and it is 0.44. `metrics.se_ir`
assumes independent assets, so that is close to twice the statistical weight from the same
ten names.

**A leveraged or roll-decay fund has no honest buy-and-hold.** TQQQ, SQQQ, SPXL, UPRO,
SOXL, SVXY, VXX, USO, UNG, DBC, DBA, CORN, WEAT and UGA are path-dependent derivatives of
an index, not assets. VXX is down 99.5% over its life and UNG 99.9% with no view expressed
either way, so "beat buy-and-hold" on those names measures the decay of the benchmark. All
fourteen are out.

For crypto the equivalent trap is the **price grid**: SHIB/USD quotes on an increment nine
basis points wide, and harvesting a coarse grid rather than an asset is exactly what let a
recycled penny stock compound to 6.4e17% before `check_data` learned to quarantine it.
SHIB and OP are rejected on grid, IMX/ARB/APT on history.

### A bare ticker is not an identity

Twelve Data resolves an unqualified symbol against every venue it carries. Where it has no
US listing it does not return nothing — it returns **somebody else**, as a full,
internally consistent, structurally perfect series that passes every bar-level check:

    CTRA -> Ciputra Development Tbk PT   Indonesia Stock Exchange, rupiah
    STJ  -> St. James's Place Plc        LSE, pence
    K    -> Kinross Gold Corporation     TSX

85 of the 739 cached `us_stocks` series were a foreign namesake for their entire length,
and `CTRA` ranked as the 3rd largest US stock of 2026 on the strength of rupiah dollar
volume before this was caught. Two defences, and both are load-bearing:

* `td_loader.US_LISTED_CLASSES` pins `country=United States` on every equity and ETF
  request, so the vendor errors instead of substituting. This is the fix at the source.
* `check_data.py --probe-listing` asks whether each cached ticker has a US listing at all
  and caches the verdict; `check_data.wrong_instrument_reason` then quarantines offline
  from that cache. This is the check on data already on disk.

No bar-level test can find these, because the bars are not malformed — they are somebody
else's. Re-probe after any fetch that adds symbols.

### There are two vendors, and the futures class may only ask the second one

Twelve Data carries **no CME contract at all**, and — exactly as above — it does not
answer "no". `CL` there is Colgate-Palmolive and `ES` is Eversource Energy. So
`cme_futures` names its own source and `td_loader.fetch` refuses it outright:

| class | vendor | what it writes |
|---|---|---|
| `us_stocks`, `us_etfs`, `crypto`, `commodities` | Twelve Data | `td_loader.py` |
| `cme_futures` | Databento `GLBX.MDP3` | `db_loader.py` |

`td_loader.load` still reads every class, whatever filled the cache. The single door
matters more than the vendor behind it, and the parquet on disk is identical either way.

Three things about the futures class differ from everything else here, and each of them
changes how a number on it must be read:

* **A symbol is `ES.v.0`** — root, roll rule, rank — not `ES`. `CL` is already a member
  of `US_STOCKS`, and `config.class_of` returns the first class that claims a symbol.
* **Prices are ratio back-adjusted, so only the newest bars are real quotes.** A roll
  otherwise hands a rule a return nobody earned: WTI's front month closed at 18.12 in
  April 2020 and the series' next print was 24.76. `data/reference/futures_rolls.csv`
  records every adjustment and whether it was exact.
* **History begins 2010-06-06 and cannot be extended** — that is the first day of the
  vendor's CME archive. ~16 years against the equity sheet's ~26, and `metrics.se_ir`
  falls as 1/sqrt(years), so every gate on this class is about 1.3x harder to clear.

And the class is **1d only**, which is a measured vendor defect rather than a choice: the
hourly archive collapses whole sessions into single bars before 2013. See
`backtest engine/CLAUDE.md`.

## How a strategy is compared to buy-and-hold

**A benchmark is valid only if it differs from the strategy in exactly one thing: the
signal.** Universe, membership dates, weighting, rebalancing schedule, fee schedule, fill
timing and cash treatment must all be identical on both sides. Whatever you leave
different is attributed to skill, silently and in the flattering direction.

Five things must match, and each has a way of not matching that has bitten this repo:

| must match | the failure it prevents |
|---|---|
| universe and membership dates | holding a name on days it was not in the universe. `--pit` and `top100_membership` |
| weighting and rebalance schedule | the book rebalances to equal weight every bar, so the baseline must too. A "buy once and hold forever" baseline is a *different portfolio*, and the gap between the two is not signal |
| fee schedule | `--charge-bench` puts the baseline on the strategy's grid |
| fill timing | see below. `--fill` |
| cash treatment | idle capital earns the bill rate on both sides, and the baseline is scaled DOWN with T-bills to the strategy's volatility rather than the strategy levered up |

Survivorship is the sixth and it **cannot be matched away**, because the vendor serves no
delisted equities — the names that failed are absent from both sides rather than
underweighted on both. `--stress-delisted FRAC` puts the unheld members back and sends
`FRAC` of the ones that actually departed to zero over their last year of membership. It
is a bound, not an estimate, and its floor/ceiling direction depends on the rule: a
part-time rule eats less of a bankruptcy than an always-invested baseline does, so the
stress can move a relative score either way. Run it before asserting which.

### Report three numbers, never one

* **N1 — does the signal add value?** The book against the matched basket, cash-matched.
  Survives a flattering universe, because both sides carry the same one. This is what
  `cashmatch_excess_cagr` and the block bootstrap answer.
* **N2 — is it worth running?** The book against the class's real index ETF, cash-matched
  — `idx_cashmatch_*`. Both sides purchasable. **Read the signal-free controls on this
  column before reading the rule's**: the self-built basket is not the index, so a
  no-signal control can post a large multiple here on universe alone, and if it does then
  no absolute figure on the sheet means what it appears to.
* **N3 — how much of N1 is survivorship?** The `--stress-delisted` re-run.

### Fill timing is a first-class control, not a detail

A rule computed from a bar's own close, high and low and then *filled at that same close*
assumes you transacted at a print you could not have known until it happened. That is a
real look-ahead — the same family as the `nanmedian` leak below, smaller and much better
hidden, and it flatters every rule keyed on the current bar's close, which is the whole
reversion family.

**The fix is not to delay the fill by a bar. It is to compute the signal earlier and
still trade the close.** A market-on-close order sent minutes before the bell fills in
the deepest auction of the day; the only thing that has to change is that the *signal*
may not use the last few minutes. For IBS that means a session-so-far IBS instead of a
full-day one. Measured on the 5m equity cache, the two agree on **86%** of symbol-days
and their raw values correlate 0.93-0.98, so this costs far less than the delay does.

`--fill` prices the alternatives, and the three separate cleanly:

* `close` — the published convention. **An optimistic bound, not a result**: it carries
  the look-ahead above.
* `open` — signal at a close, filled at the next open. **A pessimistic bound.** It removes
  the look-ahead but also charges a full session of delay you do not have to take if the
  auction is available to you, so it double-counts.
* `close_lag` — a diagnostic only. Same delay as `open` but still on closes, so it skips a
  whole session. It exists to separate a signal that decays within a bar from Blume &
  Stambaugh (1983) bid-ask bounce, because `open` moves the delay *and* the price at once
  and alone cannot say which collapsed a result. Never quote it as performance.

The truth is between `close` and `open`, and `--fill` cannot reach it because the
correction lives in the signal, not the fill. **A result is only safe if it survives at
the pessimistic bound; if it clears `close` but not `open`, the honest report is a range
and the reason for it, never the flattering end.** Closing that gap at book level needs
intraday bars for the whole universe — the cache holds only the old mega-20 — so for now
it is a bound, not a number.

### A Heikin-Ashi backtest has a fill trap, and it is not in the fill model

An HA close is `(O+H+L+C)/4` — an average of four prices, so it is not a price anybody
could have transacted at. A chart platform's broker emulator will fill at it anyway
unless told not to, and because the synthetic close is pulled toward the middle of the
bar, that single default buys below where the market was. It is enough on its own to make
most HA strategies look profitable, and it is invisible in any statistic computed after
the fill.

So `ha:` in this repo changes **the signal only**. The exposure it returns settles on
real closes through the same `apply_fill` path as every other rule. The consequence is
worth stating plainly: **a published HA result from TradingView and an HA result from
this repo are not the same measurement**, and the gap is not a rounding difference.

The same reasoning as the `--fill` section above, one level down — there the question is
which real print you are filled at, here it is whether the print is real at all.

### The trial ledger is only as complete as what was registered into it

`portfolio_wf._deflate` counts the rules named on the command line **plus** whatever
`data/reference/trials.csv` has registered for that sheet, and reports which in
`n_trials_source`. The ledger is no longer empty — the conversion batch and the intraday
Heikin-Ashi study both registered their cells before scoring — but it covers only the
work that bothered, so a sheet whose trials were never registered still deflates a
hand-picked winner against the handful of rules it was hand-picked into, which is not a
haircut. **Read `n_trials_source` before trusting `dsr` on any sheet.**

Deflation needs **two** facts about the search and a shortlist run can supply neither:
how many candidates it looked at, and how far apart their Sharpes fell. Both come off
`edge_standard.csv` — `n_trials` for the count, the spread of its `sharpe` column for the
dispersion — and both have an override, `--n-trials` and `--trial-dispersion`. **Pass
both or neither.** Supplying the honest count while letting the spread be estimated from
two rules you already believe in lowers the bar rather than raising it, which inverts the
correction. Running `--catalog` measures the spread properly and is the better option
when you can afford it. Registering trials up front is the actual fix.

## Tests, and why there are two kinds

**The unit suite is `tests/`, and it runs under pytest from the repo root.** Synthetic bars
only — nothing in it reads `data/`, calls the vendor, or opens a result CSV, because a test
that fails when somebody refetches a ticker is a test nobody will trust. It takes seconds,
so there is no excuse for not running it:

```powershell
.\.venv\Scripts\python -m pytest -q                                # everything
.\.venv\Scripts\python -m pytest tests\test_signals.py -q          # one file
.\.venv\Scripts\python -m pytest tests\test_stats.py -k sharpe -q  # one test
```

Two settings in `pyproject.toml` are load-bearing. Collection uses pytest's **default
(prepend) import mode** so that `tests/` lands on `sys.path` and every module can share
`conftest.make_ohlcv` by bare import; `--import-mode=importlib` breaks the whole suite.
And `xfail_strict` is on, because an xfail that starts passing is a defect somebody fixed
without deleting the note.

**The gates are `__main__` scripts that exit nonzero, and they are run directly.** Same
contract as `parity.py`: a nonzero exit is the whole point, so they are not collected even
where `testpaths` lists them for discoverability. Each proves a property that a unit test
on synthetic bars structurally cannot.

| gate | run from | what it proves |
|---|---|---|
| `strategies/tests/test_causality.py` | repo root | every published strategy is causal — by TRUNCATION, not by reading the code. ~20s |
| `tools/test_stats_equivalence.py` | repo root | `stockhunt.stats` reproduces the implementations it replaced, bit for bit |
| `tools/golden.py capture` / `verify` | repo root | positions and scores unchanged across a refactor. See the speed section above |
| `parity.py --n 3` | `backtest engine/` | three engines agree on the same rule |
| `test_t_bar.py` | `walk-forward optimization/` | the significance bar's false-positive rate is actually 5% |
| `test_alpha101.py` | `walk-forward optimization/` | the 101-alpha interpreter is causal, by TRUNCATION, and its cross-sectional and time-series axes are not swapped |
| `test_store.py` | `paper trading engine/` | the desk resumes after a restart instead of resetting |
| `test_runtime_attach.py` | `paper trading engine/` | a strategy can join a RUNNING Nautilus trader. The whole manager desk rests on it, and the failure it guards is SILENT — `add_strategy` returns rather than raising |
| `test_member_desk.py` | `paper trading engine/` | an order in the ledger becomes a fill, a position and a record; and the three refusals come back with reasons |

**Two folders keep their own pytest suites, deliberately outside the root `testpaths`.**
Collecting either from the root would make `pytest` require `fastapi` or
`nautilus_trader` to be installed, and the unit suite depends on numpy and pandas only.

```powershell
cd "paper api";            ..\.venv\Scripts\python -m pytest -q
cd "paper trading engine"; ..\.venv\Scripts\python -m pytest test_accounts.py test_desk_orders.py test_feed_timeframes.py test_book.py test_book_desk.py test_paper_metrics.py test_fill_pnl.py -q
```

The engine's files are named explicitly because the same folder also holds `__main__`
gates — `test_store.py`, `test_runtime_attach.py`, `test_member_desk.py` — which exit
nonzero and are run directly, not collected.

No linter or formatter is configured, and `pyproject.toml` pins no test-time dependency
beyond pytest itself.

## The pipeline, in order

```powershell
cd "backtest engine"
python sp500_membership.py --probe            # who is in the INDEX, point-in-time
python top100_membership.py                   # WHO IS IN THE UNIVERSE: the top 100 of it
python factors.py                             # Fama-French daily -> ../data/reference/
python td_loader.py --class crypto --tf 1d    # fetch  -> ../data/crypto/1d/
python db_loader.py --check                   # CME futures: price the pull first
python db_loader.py                           # ...then fetch -> ../data/futures/1d/
python futures_screen.py --write              # which roots are tradable AND independent
python check_data.py --fix                    # OHLC integrity; run after ANY fetch
python check_data.py --probe-listing          # is each ticker even the US company?
python parity.py --n 3                        # three engines must agree. gate on this
python sweep.py --class us_stocks             # stage 1: 231 singles, single split
python combo_sweep.py                         # stage 2: pairs (legacy split)
python build_payload.py; python build_report.py   # -> report/index.html

cd "../walk-forward optimization"
python walkforward.py --class us_stocks --tf 1d   # stage 1b: THE headline number
python walkforward.py --class us_etfs --tf 1d 4h  # ...and the dashboard's third asset class
python variants.py --tf 1d 4h                     # stage 1c: 8 transforms
python prereg.py --tf 1d 4h --freeze              # stage 1d: 5 published, no free params
python strat_wf.py --tf 1d 4h                     # stage 1e: the strategies/ catalog
python focus_wf.py --tf 1d                        # stage 1f: named-in-advance deep dive
python riskmatch_wf.py --tf 1d 4h                 # stage 1g: THE STANDARD -> edge_standard.csv
python gate_calibration.py                        # are the gates even provable here?
python alpha101.py --ic                           # stage 1i: the 101 PUBLISHED alphas,
./run_alpha101.sh                                 #   a fixed pre-registered set. BASH
python combo_wf.py --tf 1d 4h                     # stage 2b: walk-forward pairs
python portfolio_wf.py --tf 1d --pit              # stage 1h: score the BOOK, not the median
python make_book_rules.py; ./run_book.sh          # ...stage 1h for the WHOLE leaderboard,
                                                  #    and the dashboard's equity curves

cd "../paper trading engine"
python backtest_paper.py --symbols SOXL       # prove the order path fills, offline
python migrate_owner.py --check               # what schema is paper.db on?
python catalog.py                             # which rules may be promoted -> catalog.json
python run_paper.py                           # the live desk: top 3 rules per class,
                                              #   plus every registration in desk.db

cd "../Stockhunt Dashboard"
python build_dashboard.py --serve --dist      # both artifacts
.\run.ps1                                     # serve locally. no login, so loopback only

cd "../paper api"
python admin_users.py allow you@example.com --admin   # there is no sign-up: this is it
.\run.ps1 -Tunnel                             # the board + API behind a login, public URL
```

**Long jobs get killed at 10 minutes** by the harness timeout, so launch them detached with
output redirected to that folder's `logs/`.

**Launch them from bash, not PowerShell**, and this is not a style preference — the
PowerShell form silently killed a full pipeline re-run on 2026-08-12 and then looked busy
for 83 minutes afterwards. Three behaviours combine:

* Windows PowerShell 5.1 wraps every stderr line from a native program in an ErrorRecord
  (`NativeCommandError`). **Every stage here emits numpy `RuntimeWarning`s on a normal
  run** — `riskmatch_wf.py:805`, "invalid value encountered in scalar divide", fires on
  cells with zero variance. Under `$ErrorActionPreference = "Stop"` that warning is a
  terminating error, so the stage dies two minutes in with exit 0 never recorded.
* `*>` buffers until the process exits, so the log sits at 0 bytes and there is nothing to
  read while it happens.
* The python main dies but its multiprocessing workers are orphaned still holding the
  redirected stdout handle, so the launcher never returns and never logs an exit.

The result is a job that is dead, silent, and indistinguishable from a slow one. Use
`nohup ./script.sh > logs/x.log 2>&1 &` with `python -u`; `run_top100.sh` in the
walk-forward folder is the worked example.

**Check progress by process ancestry, not by `Get-Process python`.** This box runs other
projects, and a sibling repo's sweep at 100% CPU reads exactly like your own job making
progress. Filter on `parent_pid`, or you will conclude a dead stage is healthy.

## Generated files are never edited

`report/index.html`, `web/data.js`, `web/live.json`, `web/curves/`, `web/paper_curves.json`,
`dist/dashboard.html`, `universes.py`. Edit the builder, never the output.

## Known build state

`parity.py --n 5` exits nonzero on one cell (`crypto 5m AVAX/USD EMA_1000`). It is a real
vector-vs-reference disagreement about what happens when a short is annihilated, not a
broken build and not a refactor artifact. Unfixed on purpose: fixing it changes published
numbers, so it wants its own task with the result diff as the deliverable.

Each folder's own `CLAUDE.md` carries the structural detail for its stage.
