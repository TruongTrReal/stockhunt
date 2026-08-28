# Portfolios

The plan for the portfolio feature, and the reasoning behind each decision. Built on
branch `task/portfolios`, in the worktree at `.claude/worktrees/portfolios`.

**Structure only, no results** — same rule as `CLAUDE.md`. No numbers here.

## What changes

The desk's unit stops being a single rule and becomes a **portfolio**: a named basket of
strategy legs with one pot of money, one equity curve, one on/off switch. A single
strategy is something you drill *into*, not a top-level row.

Nothing about how a rule is *measured* changes. The research still scores one rule at a
time and `board_rank.py` is still the one ranking. What is new is a layer above it that
decides which rules are held *together*, and what that does to the result.

## The shape

| | |
|---|---|
| capital | one pot per portfolio, split equally across its legs |
| rebalance | back to equal weight, monthly |
| kinds | `manual` (rules picked by hand) and `follow` (tracks one sheet's top N) |
| a leg | an ordinary book registration carrying a `portfolio_id` |
| the toggle | `want` on the portfolio, cascaded to every leg in one transaction |
| the record | membership changes are appended to a log, never rewritten |

**A leg is deliberately not a new kind of thing.** It is `kind='book'`, the same row
`/v1/house/strategies` writes when a rule is promoted. Warm-up, subscriptions, fills,
P&L, `desk_control`'s attach and retire, `paper_state`'s publishing and the Alpaca mirror
all keep working with no knowledge that portfolios exist. A parallel registration type
would have meant re-proving every one of them, and one of them would have been missed.

**`want` and `state` stay separate**, exactly as in `deskdb`. The API writes intent; the
desk writes what it did. They genuinely disagree for a while, and that disagreement is
information rather than a bug to paper over.

## The house's 25

One `follow` portfolio per leaderboard sheet: five asset classes — top-100 US stocks,
ETFs, commodities, crypto, CME futures — times five timeframes: 1d, 4h, 1h, 15m, 5m.
Each holds *its own* sheet's top 5 and follows it.

Membership is re-checked **once a day**. A rule that has dropped out is retired from the
basket and its replacement is started; every swap is written to the change log with the
reason. Sheets that do not exist yet get no portfolio, and get one automatically when
they land — which is why the sheet list is derived rather than written down.

Three things the selector must refuse, and each of them has been in a top ten:

* an **untradable** cell — on the board but with no dispatcher that can build it live
* an **idle** book — one that sits in cash, which ranks well because doing nothing beats
  a real attempt that lost, and is not a strategy
* a **closet tracker** — one whose holdings are buy-and-hold under another name
* two rules that are **one idea under two names** (`paper_config._same_idea`), which
  would double an exposure while looking like diversification

## Ranking is not passing

Nothing on any sheet clears this repo's acceptance gates. The top five of a sheet are the
least-bad five, not five good ones, and no string this feature puts on screen may imply
otherwise. A portfolio makes that *more* important, not less: combining five rules that
each fail a gate does not produce one that passes.

The correlation between the legs is therefore a first-class number on the portfolio page,
not a detail. Five rules from one sheet all trade the same universe, so the honest
question about any basket here is whether it is five bets or one.

## The combined backtest

`stockhunt/blend.py`. Each leg's full-history book equity curve already exists on disk
(`walk-forward optimization/results/book_curves_<cls>_<tf>.json`), so combining is a
blend of those curves at fixed weights with periodic rebalancing rather than a re-run.
Costs are already inside each leg's curve and are not charged twice.

Two properties it has to keep:

* **The benchmark is blended the same way as the legs.** A benchmark that differs from
  the strategy in more than the signal is this repo's most-repeated warning.
* **The reported span is the intersection of the legs' histories, not the union.** Legs
  may come from classes whose data begins decades apart, and a statistic computed on the
  overlap must not be labelled with the longest leg's history.

**It does not net overlapping positions between legs.** Two legs long the same name make
the portfolio more long that name. That is what an equal-weight basket of strategies is,
and it is what the live desk will actually do — so the preview and the desk agree, which
is the property that matters. It does mean a combined book can concentrate more than the
same strategies held separately.

## Two things a portfolio at a fine timeframe depends on

**The buffer is sized per timeframe, and the history comes off the disk.** A live book
recomputes its rule over a rolling buffer, and one constant cannot be right at both ends of
the timeframe range — the same bar count is years at `1d` and weeks at `15m`, while every
published strategy expresses its lookback in *days*. Below its lookback a rule does not
compute a shorter version of itself; it computes nothing, holds flat, and reads as healthy.

`cache_warmup.py` fills the buffer from `data/` and lets the vendor supply only the recent
tail. The bars were always there; a single vendor request is capped at
`td_live.OUTPUT_SIZE` and was the only source.

**The splice is checked, never trusted.** Two series of one instrument from two routes
either agree on their overlap or they are two different histories with a step in the
middle, and a step in a rolling buffer is invisible to every indicator computed over it.
The cache is written with `adjust=all`, so a corporate action reaches it only at the next
fetch — a cache that predates one disagrees with the live feed by the split ratio. A failed
check drops the disk history and warms from the vendor alone: shallower, and correct. It is
never a refusal to start, because a stale cache must not become an outage.

`cme_futures` is excluded outright. Its bars are ratio back-adjusted and the two routes
anchor on different bars, so splicing them puts a roll-sized step in the buffer — the exact
defect back-adjustment exists to remove.

## Some rules may never be held, and it is enforced twice

A rule whose value at bar *t* depends on bar 0 — an expanding median or quantile rather
than a rolling one — slides underneath the buffer and can never reproduce what was scored.
Unlike a short window this is not fixed by more history; there is no buffer size at which
an expanding statistic converges.

`paper_config.unpromotable_reason` names them. It is checked in `catalog.cells`, so the
picker marks the row untradable and says why, and again in `desk_control.book_refusal`,
because a registration reaches the desk from an older `catalog.json`, a hand-written row or
a member's API call — none of which consulted the build that made the mark. **The catalog is
the courtesy; the desk is the bind.**

## Where each piece lives

| file | what |
|---|---|
| `stockhunt/deskdb.py` | `portfolios` and `portfolio_changes` tables; `portfolio_id` on `registrations` |
| `stockhunt/portfolios.py` | the model: create, toggle, legs, membership diff, resize |
| `stockhunt/blend.py` | the combined curve, its benchmark, and the leg correlation matrix |
| `paper trading engine/portfolio_follow.py` | which rules a follow-portfolio should hold, as DATA |
| `paper api/api_portfolios.py` | `/v1/portfolios` — create, read, toggle, retire, backtest, preview |
| `dashboard-next/` | the portfolio detail page, and the paper desk rebuilt portfolio-first |

`portfolio_follow.plan()` computes the daily reconcile without performing it;
`portfolios.apply_membership()` performs it. Keeping the decision separate from the act
is what makes it testable and what lets a `--dry-run` be honest.

## Order of work

Live things last.

1. the ledger and the model
2. the blend engine
3. the API
4. the board — portfolio detail page, paper page portfolio-first, "add to portfolio" on
   the leaderboard
5. the desk — the daily reconcile, the monthly rebalance, the toggle
6. widen `BOOK_TIMEFRAMES` to 1h and 15m, gated on the three checks in `paper_config`
7. the switch-over

## The switch-over is the irreversible step

Retiring registrations that carry months of forward record cannot be undone, so it is
last and it is deliberate:

* retire every registration the desk currently runs
* **disable `stockhunt-rotation.timer` on the VPS** — `rotation_manager.py` re-registers
  itself idempotently on every firing, so retiring its ledger row alone just means it
  comes back next month
* register the 25 portfolios
* restart the desk by hand. `autodeploy.sh` deliberately never restarts
  `stockhunt-desk`, because that flattens every book and re-warms every buffer; it writes
  a `DESK_RESTART_PENDING` marker and leaves the call to a human

## Do not push this branch to master until it is finished

`deploy/systemd/stockhunt-autodeploy.timer` polls `origin/master` every five minutes and
hard-resets the VPS to it. The web and API halves of this feature go live within five
minutes of reaching master, carried there by the next session's push whether or not they
know this work is sitting under it. `PARALLEL.md` §4 is the full reasoning.
