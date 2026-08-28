# CLAUDE.md

Guidance for Claude Code working in this directory. Read `../CLAUDE.md` first.
**No results here** — this folder renders them; the `results/` CSVs own them.

## What this is

The monitor: backtest results and live paper trading on one page. It is a **reader**.
Nothing here can place an order, fetch price data for the desk, or write into another
folder's results.

One payload, two outputs:

```
board_rank.py         reads results.db -> the ranked leaderboard
   |                  ...and `paper api` calls the SAME function per request
payload.py            + the CSVs it still opens by name -> one document
   |
build_dashboard.py --serve   -> web/data.js + web/curves/*.json    (SPA, live)
                   --dist    -> dist/dashboard.html                (one file, no server)
```

**The leaderboard is no longer baked.** `board_rank.build_sheet` was moved out of
`payload.py` whole -- every filter, every tiebreak and every comment came across unchanged
-- and reads `results.db` instead of six CSVs. `payload.py` still calls it for the baked
build, and `paper api` calls it for `/v1/research/board`, which `app.js` fetches on load.
One ranking, two callers.

`tools/test_board_equivalence.py` is the gate on that move: it compares the rendered sheet
against a captured baseline as JSON **text**, so a value-wise-equal difference cannot slip
through. The one drift it has ever caught was rounding a metric inside the store instead of
inside the ranker, which turned every `-0.0` into `0.0` on three of twenty sheets.

Four properties of `board_rank.py` to preserve:

* **It imports pandas and `stockhunt.resultsdb`, and nothing else from this repo.** Not
  `dash_config`, not `paper_config`, not the engine's `config`. `paper api/api_paths.py`
  is the one bootstrap that pulls in no trading code, and the HTTP layer starting without
  a TA-Lib build depends on it. The six gate definitions therefore travel in the store's
  `meta` table, written there by `tools/ingest_results.py`.
* **Its caches are keyed on `resultsdb.revision()`.** They were written for a builder that
  ran once and exited; behind an endpoint an un-keyed cache is a board frozen at the first
  request, which is the same staleness this whole change removed, one level down and far
  harder to see because nothing is stale on disk.
* **The population statistics are computed on every call** -- `noise_ceiling`, the trial
  count, `exposure_corr`. Adding one rule changes them for every existing row, so a stored
  copy is wrong the moment anything lands.
* **`build_board()` holds `_BOARD_LOCK` across a rebuild, and sweeps the older revision
  before it starts.** Both are consequences of the caches outliving a single call now.
  Without the lock, N readers arriving cold each run the whole join and N-1 answers are
  thrown away; without `_evict`, `_RM_CACHE` / `_EDGE_CACHE` / `_BOOK_CACHE` keep one
  full copy per revision for the life of the process, and the per-asset rows are the
  largest thing this module holds.

`build_board()` -- all twenty sheets at once, which is what both servers hand the page --
is **cold once per store revision and a dictionary lookup after that**. The first request
after the worker inserts a rule pays the rebuild and every other one is free. Most of the
cold time is the per-asset layer: `_per_asset_from_riskmatch` ranks every name for every
rule on the sheet (~400 x ~600 on us_stocks 1d) and only the `TOP_N` rows that ship carry
the result. Ranking first and building per-asset rows only for those would remove most of
it -- a real change to moved code, so it wants `test_board_equivalence.py` either side of
it and a commit of its own.

**A server may take that cold build off the request path** with
`board_rank.start_warmer(seconds)` / `stop_warmer()`, which `paper api` calls from its
lifespan (`api_config.BOARD_WARM_SECONDS`, `0` to disable). It is a daemon thread that
calls the same `build_board()` a reader calls, so the freshness contract is untouched --
the revision still decides whether anything is rebuilt. What it changes is **who waits**:
`app.js` awaits `/v1/research/board` before its first `render()`, so without it the first
person to open the board after a deploy watches a blank page for the whole build. The poll
is what notices a write from `research_worker.py`, which scores in its own interpreter and
can announce itself no other way.

`tests/test_board_cache.py` pins the lock, the sweep and the warmer. It is in the root
suite, which is why `tests/conftest.py` puts this folder on `sys.path` -- `board_rank`
brings no dependency the unit suite did not already have.

There used to be two independent builders over two different subsets of the same CSVs, with
two separate implementations of every view. They drifted: at the time they were merged the
single-file board was a day stale and did not show paper trading at all, which is the thing
this dashboard exists to watch. Do not re-fork them.

## Commands

```powershell
python build_dashboard.py --serve            # rebuild what serve.py serves
python build_dashboard.py --dist             # rebuild the shareable single file
python build_dashboard.py --dist --no-curves # ...without the DETAIL-page curve JSONs.
                                             #   the small board curves stay either way:
                                             #   they back the chart on the landing view
python build_dashboard.py --serve --offline  # skip the live price snapshot
python paper_curves.py --top 5               # rebuild web/paper_curves.json

.\run.ps1                                    # serve on 127.0.0.1:8765, local only
.\run.ps1 -Stop
```

Sharing the board is no longer done from this folder. `serve.py` has no login, so the
public URL was the whole security model; the same `web/` directory is served behind an
emailed sign-in code by `..\paper api\run.ps1 -Tunnel`.

`--offline` matters: the price snapshot is the only network call in the whole folder, and
everything else comes off local CSVs. A dead API key must not block a rebuild.

## Where the numbers come from

| section | source |
|---|---|
| backtest leaderboards, per-asset | **`results.db`**, via `board_rank.build_sheet`. Filled from `wf_*` / `cwf_*` / `strat_*` by `tools/ingest_results.py` |
| **edge standard** (`#/edge`) | **`results.db`** `edge` table, from `edge_standard.csv` |
| **book columns + the ranking** | **`results.db`** `book` table, from `book_<class>_<tf>.csv` |
| equity curves | `../walk-forward optimization/results/book_curves_*.json` — **written by the same run as the book columns** |
| research summary, gate power, prereg | same folder, `wf_meta`, `prereg_*` |
| the old study-2 ETF sheet | `../top 20 stocks/results/` (frozen, read-only) — payload only, unrendered |
| parity | `../paper trading engine/results/parity_live_1d.csv` |
| paper trading | `../paper trading engine/results/paper_state.json` |
| live tick stream | `web/live.json`, republished by the desk every ~2s |

Nothing comes from `../backtest engine/results/` — every figure on this dashboard is
walk-forward, which is the point.

## One verdict, on the backtest page

There is no separate standard page. There was one for about an hour and it was a mistake:
two places to look for one answer. **The backtest leaderboard is the standard**, with an
`n/6` **Standard** column on every row, and since 2026-08-13 it is **ranked on that
column** — criteria cleared, descending, with `book vs B&H` (the book's cash-matched excess
CAGR) breaking ties inside each tier. Sheets with no book run break ties on Sharpe instead,
which is why `ranked_on` and `ranked_tiebreak` are two separate fields.

### One measurement: the book (2026-08-13)

**Every column on the leaderboard is the BOOK** — one account holding the whole universe,
equal-weighted, rebalanced every bar, point-in-time membership, **idle capital earning
nothing** — read from `book_<class>_<tf>.csv`. No exceptions: the verdict and the cost
headroom moved with the rest on 2026-08-13. `edge_standard.csv` is still built and still
holds the per-asset answer; nothing on this page reads it any more except the buy-and-hold
row's existence check.

It used to be the other way round: all but three columns were the **median single asset**
over that name's own membership spell (~12 years), while the chart under them was an
account over 23.6 years. A row was two measurements side by side and nothing said which was
which. The gaps are not small — `ibs` on us_stocks 1d:

| | median asset | the book |
|---|---|---|
| $10k became | $34,725 | $250,195 |
| Max drawdown | −39.6% | −18.7% |
| Sharpe | 0.61 | 1.17 |

Drawdown is the clearest case: 189 names fall on different days, so the account falls about
half as far as its typical member, and nobody ever held the median stock. The per-asset
figures all survive in `edge_standard.csv` and in each detail page's own per-asset table,
where the header says what they are.

**A rule that never opens a position is off the board** (2026-08-13). `BBANDS`, `T3_1000`
and `CDL3STARSINSOUTH` ranked 5th, 6th and 7th on us_stocks 1d holding *nothing* for 23.6
years — 0% exposure, 0 trades, $14,875 of pure cash — because the per-asset standard scored
their SHORT side, which is "sell everything, always", a different strategy from the one the
rest of the row described. `payload.build_sheet` now drops rows whose book opened no
trades, and `#lb-note` says how many went. They stay in `book_*.csv`. Filter on the trade
count, not on exposure: it is the column the page prints, and one trade is a bad strategy
where none is not a strategy.

Consequences to preserve:

- **`$10k / asset` and the per-asset `vs B&H` are deleted**, not moved. `$10k / book` and
  `book vs B&H` ask the same two questions of the account. Two money columns on two
  measurements invited exactly one mistake — reading them as a bigger and a smaller version
  of one number — and the header note that used to guard against it is gone with them.
- **A book column blanks rather than falling back.** `bookNum` and friends print an
  em-dash where there is no book record. A fallback to `r.edge` would silently restore the
  mixing on exactly the rows where the book run is missing.
- **`LB_COLS[].cell` takes `(row, sh)`.** The book's benchmark drawdown and Sharpe are one
  figure for the whole sheet (`sh.book_bench`), not a per-row value.
- **`vs random` is measured, not modelled.** `RANDOM_25/50/75/90` are backtested as books
  by the same run; `portfolio_wf._vs_random` interpolates their measured Sharpes at each
  rule's exposure. Every control therefore scores exactly +0.000, which is the check.
- **`ROE/yr` and `vs constant` are new book columns** (`portfolio_wf.score`). ROE annualises
  over *deployed* years rather than calendar years, so it collapses to CAGR at full exposure
  — `ALWAYS_LONG` shows the two identical, which is the check.
- **`fees` is the book's cost headroom, and it is exact rather than re-fitted.** Every term
  in `vector.net_returns`' cost is linear in its own rate, so the account at *k* times the
  schedule is `gross - k*(gross - net)` on the same series. `build_book` carries a
  zero-cost copy of each book for this and nothing else; the ladder and the
  break-on-first-failure are `riskmatch_wf`'s, so the two numbers mean the same thing.
- **Idle capital earns NOTHING** (`--cash-rate 0` in `run_book.sh`). Both sides lose the
  credit — the volatility-matched benchmark blend and the constant-weight control hold
  their idle share at 0% too — so what goes is a return that was never the signal's, not a
  handicap on one side. It costs a part-time rule ~0.9%/yr over 2003-2026: `ibs` reads
  14.6%/yr and $250k where the T-bill version read 15.6% and $308k. Omit the flag to get
  the historical path back.
- **The `Standard` is computed on the book** (`portfolio_wf._standard`), through the same
  `metrics.apply_edge_standard` the per-asset stage calls — one definition of what an edge
  is, two sets of inputs. The board ranks on it as before.

  Two of the six are **not** the statistic they are per asset, and the difference decides
  rows. Criteria S and T are defined as "mean of per-fold delta-Sharpe" and "t across
  folds", so the book computes them on the sheet's own fold calendar
  (`portfolio_wf.fold_edges`) rather than from the pooled Sharpe difference or the block
  bootstrap that sit beside them on the row. That was not cosmetic: `ibs` on us_stocks 1d
  bootstraps to t = **3.87** and scores **2.81** on 21 folds. The threshold in `config`
  was calibrated on fold-to-fold sd; a bootstrap t scored against it is not the test that
  was validated. `dsharpe` and `boot_t` stay on the row as the pooled second opinion.

  **The bar T is scored against is measured, not Bonferroni** — a sign-flip permutation of
  the panel's own per-fold edges, which reads the redundancy between rules off the data
  instead of assuming there is none (`portfolio_wf._t_bar`). It lands at **3.76** on
  us_stocks 1d where Bonferroni assumed 3.84. `standard.t_bar`, `t_bar_source` and
  `t_bar_bonferroni` all reach the page, and the `Standard` tooltip names the bar, because
  "T failed" beside a printed target of ">= 2.0" and a t of 2.81 is unreadable otherwise.
- **`Side` is gone.** It named which side the per-asset verdict chose; the book is built
  long/flat only, so the column would be a constant that still implied a choice. Scoring
  long/short BOOKS would double the run and is a separate decision. `sideWarning` and
  `sideDiagNote` went with it — the mismatch they explained cannot occur now that one
  measurement drives the page.

**The primary key is coarse and that is the point.** Six integer tiers, and nothing has
ever cleared all six — on `etf 4h` all thirty visible rows sit at 2/6, so the money key
does all the visible work there, while on `crypto 1d` the tiers separate 5/6 from 3/6 and a
row cannot climb one by earning more. The verdict orders; the money orders within a
verdict. Both are named in `#lb-note` because six flat tiers look like no ordering at all
until the caption says what is sorting inside them.

**Buy-and-hold has no count**, because the six criteria measure a rule *against* it — so
under this key it falls below every scored row rather than landing mid-table where its
+0.00% excess used to put it. Last place on a leaderboard reads as "worst" and this repo's
whole finding is the opposite, so `benchRow` puts a **not ranked** chip on it. Do not let
that chip fall off; position alone would state a conclusion the page is arguing against.

### The two money columns are coloured on different questions, on purpose

Both are the book now (`$10k / asset` is gone), but they still disagree, and rows routinely
have one without the other:

* `$10k / book` is coloured on **raw money** — did the account beat holding.
* `book vs B&H` is coloured on **risk-matched skill** — did it beat holding once the
  baseline is scaled down with T-bills to the rule's own volatility.

A rule invested half the time can clear holding per unit of risk and still finish with far
less money, because it was only ever exposed to half the market. Colouring the money
column on the risk-matched verdict would tell a reader they made money they did not make;
splitting the two keeps both facts. See `bookWealthCell` in `web/app.js`.

The retired four IR gates are **no longer rendered anywhere** (2026-08-08). They survive as
the `legacy_*` columns in the CSVs and in `payload.py`, so old sheets stay interpretable, but
the page no longer carries a second strip of pips that nothing was judged on. The STRCWH
letter monogram went with them: it encoded which six criteria passed in six characters whose
only legend was the column header, so the header stopped naming the column and started being
a key. The count is what a reader wants; *which* six is a tooltip.

Three presentation rules to preserve. Each exists because breaking it produced a page that
misled:

- **Sharpe, Max DD and Profit factor are shown against the benchmark's own value**, and
  coloured by that comparison rather than by their level. Profit factor is the exception and
  is scored against 1.00, because the benchmark never closes a trade and therefore has no
  profit factor to compare with — `bench_profit_factor` does not exist and should not be
  invented.
- **`Trades/asset` sits next to `Profit factor` because it is what makes it readable.** A
  profit factor over a handful of trades is an anecdote. Anything above 90% long has its
  profit factor greyed for the same reason.
- **`Trades/asset` and `Profit factor` are the book's pooled trades**, divided by the
  names it holds. A profit factor over a handful of trades is an anecdote, which is why
  the count sits beside it and anything above 90% invested has its factor greyed.
**An unscored row prints em-dashes, it is not dropped.** The sweep universe and the scored
universe can drift apart; hiding the gap would read as "everything here was judged".
Likewise `powered: false` says *cannot tell*, not a fail — and it is a statement about the
**book's** fold count, which is the stricter one. Five of the eight sheets have 1-3 folds at
book level against the 20 the threshold was calibrated on, so their `Standard` column cannot
deliver a verdict at all. Their money columns still can: what the account did is a
measurement and needs no power.

**It is said in the `Standard` column's `doc`, not in a banner over the table.** The banner
was a paragraph of prose above the leaderboard on five of eight sheets — the same shape as
the notes that came off this page in 2026-08-11, and read past for the same reason. The
fact and the sheet's own fold count are now in the column that carries the verdict, where
somebody who does not recognise the column asks for them. Do not put the banner back; if
this needs saying louder, say it on the column.

## The header is the interface: hover explains, click ranks (2026-08-11)

The prose came off the backtest page: the three notes under the summary strip, the ~800-word
`<caption>` legend under the table, the `Method` tab and the whole of `methodView`. The same
text is now asked for a column at a time. A leaderboard header does two things, which are
the two things a reader does with a column they do not recognise:

- **Dwell 3s** (`DOC_DWELL_MS`) → `#lb-doc`, a popover under that header saying what the
  column is. On touch, **press and hold 500ms** (`DOC_HOLD_MS`) does the same.
- **Click, or a short tap** → re-rank on it. Descending, then ascending, then back to the
  delivered order.

The delay is not timidity about the animation. A popover that opens the instant the cursor
crosses a header fires on the way to a different one, so reading down sixteen columns sets
off a flicker of panels nobody asked for; three seconds means appearing is consent. On touch
the long press **swallows the click that ends it** — a hold is not a tap that took a while,
and sorting the table out from under a reader who was asking what a column meant is the whole
failure this guards against. A press anywhere else dismisses the panel, because a phone has
no `pointerleave` to do it.

Both come off `LB_COLS`. `doc` is a plain string, or a function of `{sh, grp, bench}` where
the answer depends on the sheet (its folds, its universe size, its benchmark, its
`exposure_corr`). `sv` is the row's sort value and `bsv` the benchmark's — **null wherever
buy-and-hold has no comparable figure, which is the same set of columns that print an
em-dash on its row**, and those sink it to the bottom instead of letting a blank win an
ascending sort. `text: true` marks the two columns that ascend on the first click.

**Clicking re-orders the `TOP_N` rows the ranking already selected. It does not fetch the
`TOP_N` best by that column** — `payload.py` cut the list before the page saw it. That is the
difference between reading a leaderboard and selecting on a test column, which this repo has
done once and had to retract, so `#lb-note` says *picked on `Standard, then book vs B&H`,
re-ordered by X — not the best 30 by X* whenever the order is not the default. Do not quiet
that line down.

`#lb-note` **names the basis rather than assuming it**, and `sh.ranked_on` /
`sh.ranked_tiebreak` carry it from `payload.py` so the two cannot drift. The basis has now
changed three times — ΔSharpe, then raw Sharpe, then the book's cash-matched excess, now
the standard's own count with that excess demoted to the tiebreak — and each time a
hardcoded caption survived the change and described the previous one.

Two mechanics worth keeping:

- **Sorting rewrites `#lb-body` and nothing else.** The `<thead>` nodes survive, so the
  popover does not blink out from under the cursor that just clicked, and a sixteen-column
  table does not lose its horizontal scroll position.
- **`.coldoc` is `position:absolute` with `pointer-events:none`, and its width is set from
  JS.** In flow it pushed the table down, which moved the header out from under the cursor,
  which closed it, which put the header back — a loop. And an absolutely positioned box
  shrink-to-fits between its `left` and the section's right edge, so without an explicit
  width the last column's explanation came out 112px wide against the first column's 515.

Nothing was deleted, only relocated. Every load-bearing warning lives in some column's `doc`
now: exposure-before-money on `Long %`, t across folds and never across assets on `t`, profit
factor scored against 1.00 on `Profit factor`, median-not-mean on `$10k at equal risk`, and
the paired-comparison argument on `vs B&H`. If one of them stops being true, the `doc` is
where it has to change. **A column added without a `doc` is the one column nobody can ask
about.**

## One board, and the batch that used to have its own (2026-08-26)

`#/backtest` carried **two leaderboards** for five days, switched by a `bf.board` pill:
the house catalogue, and a second board for the thirteen third-party rules converted on
2026-08-18. That board is gone. The conversions -- and the 130 megacellar forecasters, and
`sp100_momentum` -- were scored at **1d and 4h through the ordinary stages** on
2026-08-26, so they are rows on the house board like any other rule and a second one would
be two places to look for one answer.

**What the retirement cost, stated plainly.** The second board was also the only place the
**minute-chart study** was rendered -- the same eight Pine rules at 1m/2m/3m/5m, with the
`ha:` and `chart:` overlays and the overnight-flat pass. The house board's timeframe axis
is `dash_config.TIMEFRAMES` and has no 1m/2m/3m, and the house columns have no facet chips,
so those sheets are no longer drawn anywhere. **The numbers are not lost** -- the
`convert_*.csv`, `convert_book_*.csv` and `convert_curves_*.json` files are all still in
`../walk-forward optimization/results/`, and `strategies/CONVERSIONS.md` still records what
they were. Putting them back means restoring a board, not re-running a stage.

**One thing the merge does not do, and it matters for `dsr`.** The conversions were
pre-registered as their own trial family, so `n_trials` for a converted row is the count
its own scope carries in `data/reference/trials.csv` -- which is now the same ledger the
house rules deflate against, because both populations were registered into it. Read
`n_trials_source` on any row before quoting its `dsr`, exactly as the root `CLAUDE.md`
says.

`bindColHeaders(host, ctx, onSort, cols = LB_COLS)` still takes the column list rather than
closing over `LB_COLS`, and that is worth keeping even with one caller: **a column added
without a `doc` is the one column nobody can ask about.**

## One research page, three questions (2026-08-26)

`#/backtest` had three views behind a **Research** pill strip — Discover, Compare and
Robustness, each a real hash route. Two of them are gone, absorbed by the pages that were
already carrying most of their content, and the strip went with them.

| question | where it is answered now |
|---|---|
| what looks interesting on this sheet? | the leaderboard, `#/backtest` |
| which of these is better? | the same leaderboard: **every** metric as a column, and a chart of the ticked rows over the table |
| does it generalise? | the **Robustness** section at the bottom of each strategy's own page |

`#/backtest/compare` and `#/backtest/robust[/<rule>]` are **kept as redirects, not
deleted**. Both were bookmarkable by design and both were linked from the board, from
detail pages and from an empty state, so those URLs are in the world; a hash route that
matches nothing falls through to `paperMaster`, which would answer "compare these
strategies" with the paper desk. Compare goes back to the board. A robustness link carries
the rule it was about, so it opens that rule on the sheet currently selected.

Asset class and timeframe are **filters**, not navigation, and they are **pill strips**
again (2026-08-27). They were native `.fsel` selects for a day, on the argument that five
classes times five timeframes stops fitting a row — which was true about width and wrong
about reading. **A select shows one option and hides the rest behind a click**, so nothing
on the page said that `cme_futures` is a fifth class or that 15m and 5m are scored; the
one strip whose options a reader most needed to see was the only one concealing them, on a
board where every other strip (paper class, paper timeframe) is pills. The width objection
is answered in CSS instead — `.f-group` wraps, so ten buttons reflow rather than overflow.
`.fsel` stays for the Robustness fill and metric selectors, which are genuinely dropdowns.
They are the only two filters, since the board switch went with the second board. The
masthead item is still labelled **Research**, in all three
masthead copies (here, `../paper api/web/desk.html`, `docs.html`) — the copies must keep
saying the same word or the nav jumps between processes.

### The leaderboard shows every column, by default

`lbAdv` starts **true**: all of `LB_COLS`, and the toggle now hides rather than reveals.
It defaulted the other way on the argument that the full table reads as a spreadsheet. It
does — and a spreadsheet is what somebody comparing thirty strategies came for. The nine
`adv` columns are ΔSharpe, t, Expectancy, Win %, ROE/yr, the two signal-free controls and
the cost headroom, which is most of the evidence on the row; hidden, the first screenful
looked thinner than the sheet is, and the reader who most needed them was the least
likely to know the button was there. Collapsing to the ranking ten is still one click, for
a phone or a screenshot.

Two rules survive the flip unchanged. **Hiding a column never renumbers an explanation** —
`data-doc` indexes the FULL list. And **a column added without a `doc` is the one column
nobody can ask about**.

### The chart is on the board, above the table

The ticked rows are drawn as one chart at the top of `#/backtest` (`paintBoardChart`,
`pnlLines`). **Buy-and-hold is always on it**, whether or not anything is ticked. Ticking
a row adds its book; six is the ceiling.

**A sheet opens with its top five already drawn** (`seedSel`, `LB_SEL_SEED = 5`). It
opened empty for about a day, on the reasoning that a selection the reader did not make is
a claim they did not ask for — which was wrong about which claim was being made. An empty
chart says "this page has nothing for you until you work out what the checkboxes do", and
the picture worth seeing first is the one the ranking has already argued for. Three
properties of the seeding:

- **It is the DELIVERED order** — `Standard`, ties on `book vs B&H` — not the column the
  reader has since sorted by. "The top five" has to mean the same five whichever way the
  table is pointing, or the chart quietly re-picks itself when somebody sorts on a test
  column, which is the selection-on-a-test-column mistake this repo has made once.
- **Five, not six.** Buy-and-hold is a line too, and six plus the benchmark is the tangle
  `LB_SEL_MAX` exists to prevent.
- **It seeds once per sheet, guarded on the SHEET KEY and not on the list being empty.**
  That is what makes `Clear` mean clear: emptying the selection leaves `cls`/`tf` pointing
  here, so nothing grows back under the reader. Switching sheets and returning re-seeds,
  because that is a different chart.

The floating `#cmp-bar` is **hidden until `lbSel.touched`** for the same reason: the
seeded five are the page's opening position, and a bar floating over the ranking to
announce a choice nobody made is noise on every visit. `bc-note` says which of the two
states is on screen — "the top 5 on this sheet" against a plain count.

- **The selection is pinned to ONE sheet** (`lbSel`, `LB_SEL_MAX = 6`). Rows from two
  sheets sit on different bars, benchmarks and cost grids, and one chart across them would
  be the mixed-measurement bug this page already removed once — ticking on another sheet
  starts a fresh selection.
- **Colour follows the STRATEGY, not its position in the list.** `lbSel.slot` holds the
  assignment and `toggleSel` reuses a freed slot. Keyed on the index instead, un-ticking
  the second of four lines would repaint the other three, and a reader who removed one
  thing would watch the whole chart change identity. The ticked checkbox wears the same
  colour, which is what ties a row to a line whose name has been clipped.
- **Six is also the palette.** `--s1`..`--s6` in `app.css` are a categorical set validated
  **in that order** against both surfaces for adjacent CVD and normal-vision separation;
  the order is the safety mechanism, not decoration. They contain **no green and no red**,
  because on this page those two mean gained and lost, and a line's identity must never be
  readable as a verdict. Buy-and-hold is not in the set at all — muted ink, dashed, because
  it is the reference rather than a seventh competitor. Three of the light steps sit under
  3:1 against the paper, which is only allowed because **every line is named at its own
  end**; that direct label is the relief, so do not drop it and keep the colours.
- **`pnlLines` is a second chart function, deliberately.** `equityChart` draws one strategy
  against its risk-matched benchmarks and names them in a legend underneath; this draws up
  to seven independent books and has to answer "which line is that?" once they cross. The
  label column is real width taken out of the plot (`pad.r`), never an overlay, and labels
  are de-collided by a pass down and a pass back up — without the second, a sheet whose
  lines all finish high runs its last labels off the canvas.
- **The chart is `as traded`, and the caption says so**, pointing at `book vs B&H` for the
  exposure-priced version and at the detail page for the risk-matched one.

### Its curves are their own file, and that is the point

The chart reads `curves/board_<cls>_<tf>.json`, built by `payload.board_curves`:
`dates`, `bench` and `rules`, downsampled onto **one shared index list** and rounded to
2dp. Tens of kB a sheet against 300–650 kB for `curves/<cls>_<tf>.json`, which carries
full-resolution series plus `matched`, `metrics` and `bench_metrics` for every shipped
rule. None of that is wanted above a leaderboard, and paying it on every visit to the
board — to draw one dashed line until somebody ticks something — is the cost this file
exists to remove.

- **The last index is always kept** (`_downsample`). That point is the terminal wealth the
  `$10k / book` column prints, and dropping it would hang a chart over the table that
  disagrees with the row underneath — the exact disagreement `curves.py` was deleted for.
- **Only the rows the sheet SHIPS get a line, and an empty list means no lines** — the
  opposite of `_reachable`'s "no list, publish everything". A 409-line header chart is not
  a fallback for a missing 30-line one.
- **It reaches both builds through `payload["_files"]`**, a side channel `emit_serve`
  writes to `web/` and `emit_dist` merges into `__EMBEDDED__`. Neither may leave it in the
  serialized document. Going through the channel is what lets `--dist` alone embed freshly
  built bytes rather than whatever a previous `--serve` left on disk, while keeping the
  standing rule that `--dist` never writes into `web/`.

### Robustness is a section on the strategy's page

**It sits above the per-name table, and the order is the argument.** Both sections answer
"where else does this hold up" on different axes — the matrix across asset classes and
timeframes, the table across the names inside this one sheet. The matrix is the wider
question and by far the cheaper read, twenty-five squares against several hundred rows, so
a reader who stops after one section should have stopped after that one. It is also the
only section on the page that navigates anywhere, and underneath the longest table on the
page the exits were the hardest thing on it to find.

`robSection` puts the container in with the rest of the detail page and `paintRob` fills
it when the index arrives, so nothing else on the page waits on that fetch and changing
the fill or the metric costs one `innerHTML` rather than a re-render under the reader.
It carries what the old view carried: the fill selector, the metric selector, the summary
strip, and the matrix marked at the page's own cell.

As a third tab it asked the reader to pick a strategy twice — once on the leaderboard to
find it, again from a dropdown over there — and drew a matrix about one rule on a page
that was about none.

**`D.robust` is fetched, not inlined.** `payload.robustness_index` still cuts it from the
FULL `book_*.csv` sheets — ~400 rules × 25 environments (5 classes × 1d/4h/1h/15m/5m),
because a matrix built from the shipped rows would show a rule only where it ranked well
and its weak environments would vanish, which inverts the question. At that size it was
the second largest thing in `data.js` and was read by exactly one view, so it publishes as
`web/robust.json` and `ensureRobust()` fetches it the first time a detail page needs it.
`payload["robust"]` is the stub `{"file": "robust.json"}`; `ensureRobust` still reads an
inlined index, so a `dist/dashboard.html` of either vintage renders.

**EVERY scored cell is a link now.** It used to link only where the sheet's shipped board
carried the rule, which here means almost nowhere — a leaderboard ships thirty of ~400,
and `riskmatch_wf.py` has only been run at some timeframes — so most of the matrix was
drawn, tinted, titled, and swallowed the click. A square that does that reads as a broken
page, not as an absence. `backtestDetail` falls through to **`offBoardDetail`** for the
cells no leaderboard carries.

Five things to preserve:

- **`robust.fields` and the field lookups in `app.js`** (`ROB_METRICS`,
  `robMatrixTable`) **must move together** — the per-cell arrays are positional.
- **The `Robustness` column and the section's summary are raw counts** — environments
  where the book's Sharpe cleared the same universe held passively — never a composite
  score. The matrix tint carries that same single meaning whatever metric is displayed,
  via `color-mix` so it stays honest in dark mode.
- **The `Robustness` column is carried across the live-board swap by `carryRob`.**
  `board_rank.build_sheet` ranks and does not know about the robustness index — it must
  not, since importing pandas and `stockhunt.resultsdb` and nothing else is what lets the
  HTTP layer start without a TA-Lib build — so `rob` is attached one level up by
  `payload.robustness_index`. The served board therefore never carried it and the column
  printed an em-dash on every row of every server-backed page, visible only on
  `dist/dashboard.html`, which makes no such fetch and so looked like a column that
  worked. `loadLiveBoard` now copies it across by (class, timeframe, rule), the same shape
  as `applyLive` carrying `group`. A rule that reached the board after the last build has
  no baked count and still prints an em-dash, which is the honest answer for it.
- **A cell is honest about why it is empty**: an em-dash where the book stage never scored
  the rule, `0 trades` where it never opened a position, and — since 2026-08-25 — a
  separate reading for a cell scored at the *other* fill, which the reader can act on: the
  tooltip says to switch the fill selector rather than claiming the rule was never scored
  there.
- **EITHER fill makes an environment real** (`robustness_index`). The loop used to
  `continue` unless the close-fill book existed, with the open sheet attached underneath
  it — fine while `open` was strictly a second pass over cells that already had a `close`
  one. `5m` broke that assumption in the other direction: it was run at `open` ONLY, on
  purpose, because a close-fill number on 78 bars a day is the look-ahead rather than the
  rule. Gating on the close sheet dropped all five 5m cells out of the matrix without a
  word — the exact silent-narrowing failure this view exists to prevent. `years` and
  `n_names` come off whichever sheet is present (they are facts about the cell, not the
  fill); the benchmark's own Sharpe never crosses, because a close-fill benchmark under an
  `open` label charges the delay to one side only.

### `offBoardDetail` — a page for a cell no leaderboard carries

It is deliberately smaller than the ranked page and says so at the top. What it has is the
book's own record for the cell (off the robustness index, at the **same `robFill` the
matrix is on**, so a reader who switched fills and clicked through is looking at the fill
they chose), the same matrix again so they can keep walking, and the equity curve where
the sheet published one.

What it must not grow is the six-criteria verdict, the fold statistics or a per-asset
table: none of those is computed for a rule the standard never scored, and rendering an
empty one would imply the measurement exists somewhere. It reuses `paintCurves`,
`equitySection` and `metricsSection` through a synthetic row marked `offBoard: true` —
that flag exists because a pair and an off-board single both arrive at `equitySection`
with an empty `per_asset`, and the pair's sentence (leg diagnostics instead of per-symbol
rows) is untrue of the other. Two absences, two reasons, two sentences.

## One leaderboard per asset class

The **house** board splits on **asset class and timeframe, and on nothing else**. Single rules
and pairs are ranked in one table: a pair is a strategy in exactly the sense a single rule is
— same folds, same benchmark, same gates — and splitting the page by which sweep emitted a
row asked the reader to care about this repo's plumbing. `us_etfs` is the third tab, a real
walk-forward sheet in `WFO_RESULTS` (not the frozen `top 20 stocks/` one), and
`commodities` is the fourth, so all four tabs carry identical columns.

**The tabs are `dash_config.GROUPS` and nothing else.** `payload.build` loops over that
list and the pill strip is `Object.keys(D.backtest)`, so a class absent from it is invisible
no matter how complete its results are. Commodities had `wf_*`, `cwf_*`, `strat_*`, `book_*`
and `book_curves_*` for every stage, and a tab on the paper page, and still could not be
reached from here for three days. A group key is not the class name for three of the four,
so `CLASS_LABEL` **and** `CLASS_ARG` in `app.js` both need the key — the second is what the
two empty states print as the command to re-run.

Merging cost something and the page has to pay it visibly:

- **`long_frac` is on every row, singles included.** `walkforward.py` did not emit it while
  the singles had a list of their own; it does now, on `combo_wf.py`'s definitions. Ranking a
  pair against a single with the exposure shown for only one of them would be a worse page,
  not a simpler one. Anything above 90% is flagged in the table.
- **The trial count and the noise ceiling are computed over singles + pairs together.**
  Ranking 385 candidates and quoting the ceiling for 245 of them understates luck.
- **A pair's detail page has no asset-by-asset table**, because `combo_wf.py` records
  leg-correlation diagnostics instead of per-symbol rows. It says so; it does not render an
  empty table. It **does** carry a curve and a metrics table: the book run scores pairs like
  anything else.

  The page refused to draw them until 2026-08-15, and the refusal was a leftover. The note
  in its place named `curves.py`, which only ever stitched single rules and no longer
  exists; `run_book.sh` scores every label on the sheet, `book_curves_*.json` carries the
  pairs and `copy_curves` publishes them, so `web/curves/` held files the page would not
  open. **It reads as a per-class bug rather than a per-row one**, because how many pairs a
  sheet ships varies wildly: `crypto 1d` is 23 of 30 rows and `stocks 1d` is 0, so crypto's
  leaderboard led almost entirely to stub pages while stocks looked complete. If a
  measurement is ever gated on `kind === "pair"` again, check first whether the source
  actually lacks it — two of the three things that used to be gated here have it now. The
  prose that points at *Asset by asset* is gated on `r.per_asset.length`, not on `asset_n`:
  a pair carries the count (it is the sheet's universe size) while shipping no rows.
- **The asset-by-asset table is the WHOLE universe, sorted on `P&L vs B&H`, and the sort key
  is a visible column.** `payload._rank_assets` orders and cuts nothing (2026-08-13). It used
  to ship the best 10 and worst 5, which made the table a selection — the middle invisible,
  the ends needing a span floor to stop a two-month name winning on a rate, and a caption that
  had to keep saying "not a sample you can average". No selection, no floor, no such caption.
  The sort key must stay a column the reader can see: this ranked on `net_pct` once, the
  strategy's own terminal wealth, under a header that said "by P&L" beside a `P&L vs B&H`
  column that was a different number. Change the key and the header in the same commit.
- **Ordering on money is partly ordering on holding period, and the caption says so.**
  `years` differs by asset by a factor of twenty, so a name held four decades outranks a
  recent one at a far better annual rate. That was fatal when the ends were a *selection*;
  as a sort order it is a reading hazard, which is why `vs B&H / yr` stays on the row and
  the caption points at `Years`.

## `serve.py` serves and nothing else — on loopback, and nowhere else

Static files plus the WebSocket on **one port**, because two ports break the moment the desk
is shared: an HTTPS tunnel forbids a `ws://` socket from an HTTPS page (mixed content), and a
quick tunnel exposes exactly one port. One origin, one URL to hand out, and `wss://` follows
from `https://` automatically.

**It authenticates nobody, so it now refuses any bind but loopback** (`_check_host`), and
`run.ps1 -Tunnel` refuses with it. That was the documented way to share the desk, which is
exactly why removing the instruction was not enough: one `--host 0.0.0.0` published every
position, every fill and the whole research record to whoever had the URL, and the page
looked identical. Sharing goes through `../paper api/`, which serves this same directory
behind an emailed sign-in code.

**The one sanctioned exception is `--lan`** (`run.ps1 -Lan`), added 2026-08-22 for
checking a view on a phone: it binds 0.0.0.0 and prints a banner naming the audience —
every device on the network, no login — plus the device URL and the firewall hint. It is
a named flag rather than a permitted `--host` value so the exposure can never be the
side effect of a copied command line; `--host <anything non-loopback>` still refuses and
now points at the flag. It is for a network you trust, for as long as the test runs, and
it is not a way to share the desk — that stays `../paper api/`.

The `/ws` handler is a **file watcher** on `web/live.json`. It never talks to the trading
node. If this process dies the desk keeps trading.

Files are served from an **allowlist**, not "whatever is under `web/`", because the
directory also holds `demo_data.js` and because the other server does face the internet.
That list, the traversal check and the `Cache-Control` policy live in **`web_files.py`**,
imported by both servers — two implementations of a traversal check is one implementation
and one liability. If you add a served file, add it to `web_files.ALLOWED` or it 404s in
both places.

`web_files.py` imports nothing, deliberately. Not `dash_config`, which would drag the
backtest engine into an HTTP process that has no use for it.

A bare `HEAD /` is rejected by `websockets` while parsing the request line — before
`process_request` runs — so it cannot be answered properly, only muted. `_DropNonGet` does
that; the connection is still refused, the traceback is not logged.

## The single-file build does not fork the application

`app.js` is unmodified. A ~20-line shim installed ahead of it overrides `fetch` to answer out
of an embedded map (`window.__EMBEDDED__`) and stubs `WebSocket`, so the same views render a
frozen snapshot — charts included. Anything not embedded falls through to a synthetic 404,
which the application already treats as "no data".

That is why there is one implementation of every view instead of two. Keep it that way: a
change to a chart should never need to be made twice.

## The paper page reports SYSTEMS, not a desk total

The five-tile strip at the top of `#/paper` is one muted line now (`.deskline`): systems
live, the class split, the fill count, the feed. The two figures that added the desk up —
mean P&L across every system, and dollar P&L on capital deployed — are **gone on purpose**.
The desk is not a fund. Its systems are independent forward tests that happen to share a
process, so their sum tracks how many are switched on and their mean reads "flat" when half
are up and half are down. Neither figure decides anything, and both drew the eye first.

What replaced them is per system, in the row:

- **The live cumulative P&L curve** — `paper_curve` off the desk, percent since that
  system's first fill, chained across restarts. `pnlLive` draws it: baseline at **0**, not
  100, and cut at `curve_breaks` rather than drawn straight through an outage. A segment of
  one point is a dot, because a young system with a restart in it is exactly two points
  either side of a gap and a polyline-only renderer drew nothing for it.
- **The list is ranked on P&L, best first, and the ranking is FROZEN** (`orderSystems`).
  It re-ranks when the reader acts — loads the view, clicks a filter — and every tick
  repaint in between reuses that order. Numbers move several times a second; a list that
  re-sorts under the cursor cannot be read.
- **Every DETAIL chart on the paper side draws one line: the system's own** (2026-08-17).
  `pnlFigure`, `pnlPanel` and `pnlSpark` no longer take a benchmark, and the "buy & hold
  x%" that sat beside each simulated window went with the dashed line. The `bench_curve`
  is still published and is still drawn on **the ranked list**, at 34px, where a market
  line is context rather than a verdict. Everything full-size is the record alone, and the
  comparison a strategy is judged on stays where it can be made properly — risk-matched,
  over decades, on `#/backtest`. The paper figures also stopped being capped at 820px:
  they take the full 1240px rail, because the record is what these pages are for.

### Every filter strip reads its options from the payload, never from a literal

Both pill rows are built from lists `payload.build` ships, and the paper one had drifted
badly: it was `1d / 4h` — the two horizons the **house** promotes its own books at — while
the desk accepts a registration at any of seven (`paper_config.MEMBER_TIMEFRAMES`, which is
what `/v1/limits` advertises and what the join wizard offers). A member registering at 1h
or 5m got a strategy that ran, filled and published, and a board with no button that could
reach it. Same failure the class strip had before `paperClasses`, one axis over.

| strip | source | field |
|---|---|---|
| paper timeframe | `paper_config.MEMBER_TIMEFRAMES`, via `paper_state()` | `D.paper_timeframes` |
| backtest timeframe | `dash_config.TIMEFRAMES` — what `build` asked for sheets on | `D.timeframes` |
| paper asset class | `PAPER_CLASS_ORDER` + anything unknown in the rows | — |

Two rules that follow from it. **The list is what the desk CAN run, not what it happens to
be running** — a timeframe with nothing deployed still gets a pill, because "nothing is
running at 1h" is a fact worth being able to check, and the empty state under it already
says so. And **anything unknown in the rows is appended, never dropped**, so an old record
still has a home. `payload.py` imports `paper_config` for this: it is the one module in
that folder that is safe here, importing the backtest engine's `config` and nothing
heavier.

## The list is a list; a system has its own page (2026-08-17)

`#/paper` is one link per system (`systemList`, `.grp-row`) and `#/paper/sys/<cls>/<tf>/<rule>`
is where that system's record lives (`paperSystem` + `paintSystem`). The row shows what the
old `<summary>` showed — name, deployment, live sparkline, P&L — so the ranking reads the
same; everything that used to unfold underneath it moved onto the page.

It was an accordion, and the accordion had become a detail view wearing a list item's
clothes. **Every system on this desk is a book holding a whole asset class**, so opening one
unfolded a hundred-name table *inside the ranked list*, and opening two made the ranking
unreadable — which is the only thing the list is for. None of it had a URL either: a
disclosure triangle cannot be bookmarked, linked in a message, or sent to somebody.

The page carries, in this order: a `.strip` of six tiles (P&L, fills, names held, turnover,
equity, running), the live record as a `pnlFigure` rather than a 34px sparkline, the
**performance metrics** table, the two simulated windows, the **trade history** with its
CSV export, and then **one section per universe** with every name in it.

Seven things to preserve:

- **The route sits BEFORE `#/paper/<id>` in `render`.** That pattern is `(.+)`, so it
  swallows `sys/...` whole and hands `paperDetail` an id no strategy has, which bounces
  straight back to the list. A strategy id carries no `/`, so nothing else collides.
- **The rule comes off the ROW, never off the key.** `systemKey` joins on `|` and a pair's
  name contains one — `MININDEX~SAREXT|and` — so `key.split("|")` truncated every pair at
  its operator, and the list printed `MININDEX~SAREXT` for two systems that differ only in
  whether the legs vote or agree. `pcurves` is keyed on the full string and was never
  affected, which is why this survived: only the label was wrong.
- **The URL slugs the rule and never reverses it.** `paperSystem` finds its rows by
  matching `slug(s.rule)` against the segment, the same way `backtestDetail` does.
- **`#sys-body` is the volatile half.** The hero, the banners and the backtest pointer sit
  outside it so a tick repaint cannot move them. `repaintPaper` rebuilds only that
  container, and it puts back the **horizontal and vertical** scroll of every `.tbl-wrap`
  as well as `scrollY` — the holdings table is nine columns and would otherwise snap to
  column one twice a second, and the fills list scrolls inside its own box.
- **The metrics table has no benchmark column, and that is not the same decision as the
  backtest page's.** There, a strategy is scored against the same basket held *at the
  strategy's own volatility over decades*, which is a comparison that decides something.
  Days of paper fills against days of holding is not that comparison, and printed beside
  these figures it was being read as one. `liveMetrics` is the desk's own arithmetic over
  `paper_curve` and the published fills — computed in the page so it moves with the tick
  stream instead of freezing at build time, and deliberately **not** the research
  definitions in `stockhunt/stats.py`. The caption says both. Volatility and Sharpe print
  an em-dash under `MIN_METRIC_BARS` (20) rather than annualising a two-day record, and
  `BARS_PER_YEAR` is nominal per (timeframe, 24/7-ness): a US equity 4h "day" is two bars,
  not six.
- **The trade statistics count `realised != null`, and NEVER `pnl != 0`.** A published
  fill carries two P&Ls and they are different questions: `realised` is what that one fill
  closed against what the closed part cost — **null** when it opened or added — and `pnl`
  is the whole book's mark at that instant. The table showed the second under the heading
  *Realised P&L* and `liveMetrics` filtered closed trades on it, which is wrong twice
  over: every name filling in one second carries the same book mark, so one snapshot
  became several "trades", and a fill's own price moves cash and units equally, so the
  mark is nonzero only when some **unrelated** name has moved since. Both P&L columns are
  on the table now, labelled, with the caption saying which is which; the CSV heads them
  `realised_pnl` and `book_pnl`, and it used to head the book snapshot `realised_pnl`, so
  a spreadsheet built off the export inherited the mistake and kept it.

  `m.priced` is the third state. A payload published before the desk recorded the column
  has no `realised` key at all, and "0 closed trades" would be an assertion about it
  rather than the absence of one — so those rows print em-dashes and say why. It matters
  for `dist/dashboard.html`, whose embedded snapshot is frozen at build time.
- **The fills table is what the DESK PUBLISHES, which is not the whole record.**
  `paper_state.MAX_TRADES` caps it at 200 per strategy while `lifetime_trades` counts the
  database, so the section header and the caption say which of the two is on screen. The
  CSV export is a Blob built from the same rows — the board is static files behind a login
  and has no endpoint to ask for a file, and it does not need one. Its handler is re-bound
  inside `paintSystem` on every repaint, because `#sys-body` is rewritten whole and a
  listener attached once would be attached to a node that no longer exists. For the same
  reason the list is a scroll box and never an expand/collapse: state would be thrown away
  twice a second.
- **The pointer to the backtest is checked, not assumed** (`backtestHref`). The desk runs
  promotions whose leaderboard row was cut by `TOP_N`, and a link that bounces back to the
  leaderboard is worse than a sentence saying the page is not there.

`D.paper_groups[].note` is rendered here and nowhere else. It says what a universe is worth
as evidence — the one the rule was ranked on, or a transfer onto instruments the research
never held — which is the first thing somebody about to read a table of names needs.
`paperDetail`'s back link goes **up to the system**, not to the list, because that page is
reached from a system's holdings table now.

## Gotchas

- **`web/data.js`, `web/live.json`, `web/curves/`, `web/paper_curves.json` and
  `dist/dashboard.html` are generated.** Edit `payload.py`, `app.js`, `app.css` or
  `index.html`; never the outputs.
- **`data.js`'s `backtest` section is a FALLBACK now, not the source.** `app.js` fetches
  `/v1/research/board` before its first `render()` and replaces `D.backtest` with the
  answer; a non-200 leaves the baked payload in place. That degradation is deliberate --
  the board is served by two processes and a session can expire, and yesterday's numbers
  beat a blank page. It is skipped entirely under `__SNAPSHOT__`, because a single-file
  board with no server behind it is *supposed* to be frozen.
- **`serve.py` answers `/v1/research/board` too**, read-only, importing `board_rank`
  directly. Without it the loopback board silently shows whatever the last build froze.
  There is no submission route there and there must not be: queuing a rule is an act
  attributable to an account, and that process authenticates nobody.
- **`paper_curves.py` takes its system list from the DESK, not from the sheet.**
  `run_paper.py` defaults to `--top 0` and trades what has been registered — books of
  combos and published strategies — while `paper_config.top_rules` returns single rules off
  the walk-forward leaderboard. Selecting off the sheet therefore drew 24 charts for systems
  that were not running while all 24 running ones had none, for four days, with nothing on
  screen to say so. `desk_systems()` reads `paper_state.json`, the same document the board
  renders. It also uses `live_signal.position_for` — the desk's own dispatcher, and the only
  one that can build `MININDEX~SAREXT|and` or `ibs`. A book's per-asset curves are computed
  and then dropped: nothing renders them (the page looks up `assets[row.symbol]` and a
  book's symbol is the phrase "100 names"), and writing them cost 6.3 MB against 0.2 MB.
- **The masthead is duplicated in `../paper api/web/desk.html` and `docs.html`, and the
  three copies must stay dimensionally identical.** Those pages are self-contained by
  design — they must not link a stylesheet out of this build-output directory — so the rail
  (1240px + `--col`), the gap, the font size and the underline offset are copied, not
  shared. They are real navigations between two processes: any difference shows up as the
  nav jumping when you switch pages, which is what a 820px-centred column against a
  1240px-centred one did (~170px). The active link recolours and underlines and **never**
  changes weight, because bolding it re-measures the text and shifts every link after it.
- **A tab on another device is told when a newer build exists.** `app.watchForNewBuild`
  fetches `index.html` once a minute, reads the `data.js?v=` stamp out of it and compares
  with the one this tab booted on; a mismatch shows the `#update-bar` with a Reload button.
  It never reloads on its own — someone mid-page should not have it vanish.

  It needs no endpoint and no server restart, which is why it reads `index.html` rather
  than a version file: an SPA never navigates, so a phone left open on the LAN renders the
  morning's numbers indefinitely while the masthead clock — which is the *build* time, not
  now — makes it look live. An afternoon went into investigating a "wrong" chart that was
  simply an old one.
- **`web_files.cache_control` is set by what each URL promises.** `index.html` is
  `no-cache` (may be held, must be revalidated) because it carries the stamps; the stamped
  assets are `immutable` for a year because their URL changes with their bytes; curve JSONs
  keep a short max-age since their URLs are stable and their contents are not. A browser
  holding yesterday's `index.html` asks for yesterday's `app.js` **by name** and is served
  it, correctly, forever — that is the failure this ordering prevents.
- **The `?v=` cache-busters in `index.html` are stamped by the build, not by you.**
  `build_dashboard.stamp_cache_busters` rewrites them with 8 hex characters of each file's
  SHA-256 on every `--serve`. They were hand-bumped integers until 2026-08-13, and the
  failure that retired them is the nastiest kind: the build succeeds, the files on disk are
  right, and a returning browser keeps running the JavaScript it cached — so a stale chart
  is indistinguishable from a wrong one, and gets investigated as a bug. Do not hand-edit
  them, do not remove them, and if you add an asset to `index.html` add its name to the
  stamp list. `dist/` needs none of this: it inlines everything.
- **`demo_data.js` is the layout fixture and must never be the tag that ships.** It sets
  `window.DEMO = true`, which renders a warning bar. It is deliberately not in `ALLOWED`.
- **`drop_selection_rows` is a correctness requirement, not a filter.** `IS#1` rows are the
  *act of choosing* a rule scored as a strategy. In a list sorted by IR they read as one more
  candidate someone might pick up and trade. They stay in the CSVs, which is where the
  selection-cost question belongs.
- **The two-stage side mismatch is HISTORY, and the guard that remains is inert.** A row
  used to take `Side`, `Sharpe`, `t`, `Max DD`, `Trades` and `Standard` from

  `edge_standard.csv` on whichever side scored better, and `IR`, `Long %`, `CAGR` and
  `$10k became` from sheets that are long/flat always — so a short-endorsed rule printed
  half one strategy and half another. Every one of those columns now comes from the
  long/flat book, so there is no second side on the page to disagree with.

  `payload._drop_offside_diagnostics` still nulls the per-asset columns and still sets
  `diag_missing`; nothing rendered reads them. Leave it: it costs nothing, and it is the
  thing that keeps those columns honest if they are ever put back on the page.
- **Three things are deliberately NOT inlined into `data.js`**, and the reason is the same
  each time: the landing view must not pay for a page most readers never open.
  * **The detail-page curves.** Several hundred kB per sheet, and every visitor would parse
    all twenty before the leaderboard could render. Fetched per sheet on demand.
  * **The board curves** (`curves/board_<cls>_<tf>.json`). Tens of kB, fetched by the one
    view that draws them. See *Its curves are their own file*.
  * **The robustness index** (`robust.json`, ~830 kB). Read only by a strategy's own page,
    and `ensureRobust()` fetches it the first time one is opened.
- **The chart is the row (2026-08-13).** `book_curves_*.json` is written by
  `portfolio_wf.py --curves` from the same `build_book` result that produced
  `book_<class>_<tf>.csv`, so the chart's terminal wealth, CAGR, Sharpe and drawdown ARE the
  `$10k / book` and `book vs B&H` columns. Both files come out of one `./run_book.sh`;
  regenerating one without the other is the only way to make them disagree.

  This replaced a separate `curves.py` that built its own equal-weight portfolio. It
  disagreed with the row on the same page — `ibs` on us_stocks 1d ended at $270,661 there
  against $308,442 on the row — because it paid no interest on idle cash, ignored
  point-in-time membership, and computed Sharpe raw rather than over the bill rate, which
  gave the weaker book the higher Sharpe. **Do not add a second portfolio builder.**
- **Every rule on the board has a curve, and there is no top-N cut** — `run_book.sh` scores
  all ~409 labels per sheet, singles and published strategies alike. A missing chart now
  means the JSON is older than the CSV, not that the rule ranked too low; the page says so.
- **`copy_curves` publishes only the rules the sheet SHIPS.** `results/` holds all ~409;
  `web/curves/` gets the `TOP_N` rows a reader can actually click, because a detail page is
  reachable only from a leaderboard row. Publishing all of them put 13x the bytes on the
  wire and took `dist/dashboard.html` to **38 MB**; filtered it is **7.3 MB** and each
  served sheet is ~400 kB. Raise `TOP_N` and the rest publish themselves — no re-run, the
  source files already have them. `payload.curves[*]` carries both counts (`rules` shipped,
  `n_scored` available), so the gap is visible rather than assumed.
- **There are no per-asset thumbnail charts** (2026-08-13). One curve per strategy. The
  per-asset *numbers* are unaffected — the `Asset by asset` table is built by `payload.py`
  from `wf_per_asset_*.csv` and still carries every name.
- **There is ONE sizing on the detail page: every benchmark at the strategy's own
  volatility** (`equitySection` and `metricsSection`, from `curve.matched`). The chart, the
  metrics table and the caption are all the matched comparison; nothing on the page draws a
  benchmark at full size.

  Ranking lines by where they end pays for volatility, which is why: on us_stocks 1d QQQ
  finishes **26% above** `ibs` at full size and about **a third of it** at equal risk, at
  57% held. Only the second is about the strategy. This went through two worse shapes
  first — a separate bar chart underneath (two pictures of one comparison) and then a
  full-size/equal-risk toggle (which leaves the misleading reading on screen as an equal
  option) — before landing on one chart with one basis. The full-size figures survive in
  the caption as a sentence, which is where a fact that is true but easy to misread
  belongs.

  Two mechanics to keep:
  * The blended series is stored per line by `portfolio_wf`, **not** derived in the page:
    the weight applies to returns bar by bar before they compound, so `w*curve + (1-w)` is
    simply the wrong curve. Each matched line's last point x100 equals its `wealth`, which
    is the cheapest check that the two agree.
  * **Matching does not change Sharpe or Sortino**, and the table's caption says so. With
    idle cash at 0%, scaling a benchmark divides its return and its volatility by the same
    number. Volatility, drawdown, CAGR and wealth all move; the volatility row is
    identical across the table by construction, which is the point rather than a bug.
- **Do not import `run_paper` or `backtest_paper` here.** Both pull in `nautilus_trader` at
  module scope. `paper_config` has what this folder needs (`top_rules`, the universe, the
  warm-up constants) and imports nothing heavy.
- **The tunnel URL changes on every start.** A quick tunnel has no account and no stable
  hostname; the current one is printed into `logs/tunnel.err` a few seconds after launch.
