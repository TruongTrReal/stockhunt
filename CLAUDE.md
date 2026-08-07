# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working anywhere in this repo.

## What this is

A quant research repo that asks one question — **does any technical rule beat buy-and-hold?**
— and has answered it "no" at increasing scale for three studies running. It now also
paper-trades the least-bad candidates through NautilusTrader and watches the result on a
dashboard.

The honest summary: **nothing has cleared the acceptance gates.** 0 of 231 TA-Lib rules
across 501 S&P tickers; 0 across 20 mega-caps at three timeframes; 0 across two asset
classes and seven timeframes; 0 of 698 published-strategy cells. Best net out-of-sample
IR anywhere is **-0.048** (crypto 1d), and on most sheets the noise ceiling sits *above*
the gate, so they could not be passed meaningfully even in principle.

**Treat a run that suddenly produces winners as a bug** until `parity.py` and the
multiplicity correction have both been re-checked. That is not pessimism; it is what the
history of this repo says. Two findings have already been retracted.

## Layout

```
data/                       every price bar, shared. stocks/ crypto/ etfs/ by timeframe
   |
strategies/                 talib_signals.py (231 rules) + catalog.py (26 published)
   |
backtest engine/            the machinery: engines, signals, metrics, td_loader, parity
   |                        -> results/ single-split sweeps, combos, parity, validation
walk-forward optimization/  what prices SELECTION: rolling re-fit, variants, prereg
   |                        -> results/ wf_* cwf_* var_* prereg_* strat_* curves_*
paper trading engine/       the live desk: Nautilus sandbox on live Twelve Data bars
   |                        -> publishes live.json to the dashboard
Stockhunt Dashboard/        the monitor. one builder, two outputs (served SPA + one file)

engine-bakeoff/             reference vs nautilus vs manifoldbt; yfinance vs Twelve Data
test research/              LOCKED - study 1, S&P-wide daily
top 20 stocks/              LOCKED - study 2, 20 mega-caps at 1d/1h/5m
AI generated strategies/    empty
```

`test research/` and `top 20 stocks/` are **frozen** — `.claude/settings.json` denies
writes to both. Read `LOCKED.md` before trying to change anything there.

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

The chain is three hops and the order matters: a bootstrap puts `backtest engine/` on the
path, importing its `config` puts the **repo root** on the path, and that is the only
reason `strategies.talib_signals` resolves. Import the bootstrap first.

## The pipeline, in order

```powershell
cd "backtest engine"
python td_loader.py --class crypto --tf 1d    # fetch  -> ../data/crypto/1d/
python check_data.py --fix                    # OHLC integrity; run after ANY fetch
python parity.py --n 3                        # three engines must agree. gate on this
python sweep.py --class us_stocks             # stage 1: 231 singles, single split
python combo_sweep.py                         # stage 2: pairs (legacy split)
python build_payload.py; python build_report.py   # -> report/index.html

cd "../walk-forward optimization"
python walkforward.py --class us_stocks --tf 1d   # stage 1b: THE headline number
python variants.py --tf 1d 4h                     # stage 1c: 8 transforms
python prereg.py --tf 1d 4h --freeze              # stage 1d: 5 published, no free params
python strat_wf.py --tf 1d 4h                     # stage 1e: the strategies/ catalog
python gate_calibration.py                        # are the gates even provable here?
python combo_wf.py --tf 1d 4h                     # stage 2b: walk-forward pairs
python curves.py --tf 1d 4h                       # equity curves for the dashboard

cd "../paper trading engine"
python backtest_paper.py --symbols SOXL       # prove the order path fills, offline
python run_paper.py --top 5                   # the live desk

cd "../Stockhunt Dashboard"
python build_dashboard.py --serve --dist      # both artifacts
.\run.ps1 -Tunnel                             # serve + public URL
```

**Long jobs get killed at 10 minutes** by the harness timeout. Launch them detached
(`Start-Process ... -WindowStyle Hidden`) with output redirected to that folder's `logs/`.

## Conventions that carry real meaning

These are load-bearing. Each one exists because getting it wrong produced a wrong answer
that looked right.

- **Rank on IR against buy-and-hold on the same asset.** Not raw Sharpe, which measures
  long-bias in a rising market. A rule that sits flat scores roughly *minus* the
  benchmark's Sharpe, so on intraday sheets the "best" rules are the ones that most nearly
  do nothing — a fact about the metric, not a finding.
- **Walk-forward is mandatory, and IS#1 is the headline.** A single split scores a rule;
  it does not score *choosing* a rule. `sweep.py` picks its winner with the whole test
  column visible — nobody trading in 2015 had that table. On 1d equities the gap is
  **-0.21 IR**, and per-fold tuning lost on all 14 re-optimisable families. Selection is a
  cost, never a free upgrade.
- **Shortlist on train-period IR only.** Sorting by a test column and reading the top rows
  is selection on test. This repo has done it and had to retract.
- **Read `long_frac` before believing any equity IR improvement.** `corr(IR, long_frac)`
  is 0.881 on 1d, so the leaderboard is largely a ranking of time-in-market. Rank on
  `ir_vs_random` — IR minus the signal-free control curve at the row's own exposure. It
  reverses the ordering.
- **IR is blind to variance drag.** `ibs` on SOXL scores IR -0.134 and simultaneously
  compounds +25.4%/yr more than buy-and-hold. Report `excess_cagr` beside IR on leveraged
  names. Both numbers are correct.
- **The baseline is never charged and never flattened.** Flattening the benchmark turns it
  into a different strategy — exactly what made the old 5m "beat" an artifact.
- **One source, one adjustment.** Twelve Data with `adjust=all`. Never compare against the
  yfinance-derived leaderboards: 0.05% of position-days differ but final equity moves up
  to 18%, which looks exactly like alpha.
- **Positions re-size to the target fraction every bar.** Any new engine must match this or
  parity will correctly fail.
- **Annualisation is measured**, never a constant. `ddof=1` everywhere.
- **Check the gates against the sample before believing a null.** Only `us_stocks 1d` is
  coherent. Elsewhere the stated 0.50 gate sits *below* what luck produces; relaxing such a
  gate makes it easier and more worthless at once. The coherent moves are more years or
  fewer trials.
- **`np.nanmedian(whole_series)` is look-ahead, and it is still in this repo.**
  `prereg.volmanaged` and `variants._vol_scale` carry it; stage 1c and 1d were not re-run.
  Test causality by truncation, not by reading the code — that is what caught it.

Each folder's own `CLAUDE.md` carries the detail for its stage. `backtest engine/CLAUDE.md`
is the longest and worth reading in full before touching the engine.

## Gotchas

- **No crypto volume from Twelve Data** — absent, not zero. AD, ADOSC, MFI, OBV cannot be
  evaluated there; `signals.usable_rules` skips and counts them.
- **The vendor ships broken intraday OHLC and decimal-point bad ticks.** 126 spikes on BTC
  1-minute once turned buy-and-hold into 1e125 and contaminated every IR on the sheet.
  Structural validation did not catch it; looking at the rendered number did. Run
  `check_data.py --fix` after every fetch.
- **Nautilus parity runs at zero cost only**, with a tolerance that scales with
  sqrt(fills), not with equity.
- **Generated files are never edited**: `report/index.html`, `web/data.js`,
  `dist/dashboard.html`, `web/live.json`.
