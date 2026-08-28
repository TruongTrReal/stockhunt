# dashboard-next

The board as a Next.js app, talking to `../paper api` over HTTP instead of reading a baked
payload. **This file describes how it works, never what it found** — same rule as the rest
of the repo. No numbers here.

## Why it exists

The vanilla board (`../Stockhunt Dashboard/web/`) bakes every row into `data.js` at build
time. That caps what it can show: **94% of a leaderboard row's bytes are the
asset-by-asset table underneath it**, which only the detail page ever draws, so carrying
~500 rows a sheet the baked way costs ~319 MB. Capping at 30 rows was the consequence, not
the intent.

Paging an API instead makes depth free — one page is one request — which is the whole
reason this app exists. `GET /v1/research/leaderboard?cls=&tf=&offset=&limit=`.

## The four decisions, and what each costs

**`output: "export"`.** A directory of static files, served by the FastAPI process that
is already in front of the board. Everything this app draws comes from that API at
request time behind its email-code session, so there is nothing for a Node server to
render — and shipping one would put a second runtime on a VPS whose deploy is `git pull`
every five minutes. **The cost: no SSR, no ISR, no route handlers.** The day a page needs
server rendering is the day to revisit this, not before.

**Served at `/`, since 2026-08-28.** It was `/next/` while it was being built, precisely so
that it could be wrong without taking the working board down; it is not being built any
more. `/next/` 302s here rather than serving a second copy.

**The vanilla board moved to `/classic` and is KEPT, not retired.** It is still the only
thing that produces `dist/dashboard.html`, it still holds views this app has no equivalent
for, and its hash URLs are in bookmarks and in this repo's own documentation. Retiring a
board because a second one exists is how you find out a month later which of the two
somebody was relying on.

**No trailing slash on the `/classic` that matters.** `web/index.html` loads its assets
relatively — `app.css?v=`, `data.js?v=`, `app.js?v=` — so the document has to be served
from a path with no directory segment or every asset resolves under `/classic/` and 404s.
That renders unstyled HTML rather than failing outright, which is the worse of the two ways
to be broken. Both spellings are served, because the relative base is what the BROWSER
computes from the address bar.

**The old hash URLs are translated on arrival.** `/#/backtest/stocks/1d/ibs` lands here
with a hash the server never sees, so `legacyHashTarget` in `app/page.tsx` maps it to
`/rule/?cls=us_stocks&tf=1d&rule=ibs` before the sheet is fetched. Without it the reader
asked for one rule and silently got the list, which is the worst kind of broken link — one
that renders a perfectly good page.

**`app/board.css` is COPIED from `../Stockhunt Dashboard/web/app.css`**, by
`scripts/copy-css.mjs` on every `predev`/`prebuild`, and is gitignored. It is not a fork.
That stylesheet is 730 hand-tuned lines carrying an argument: colour on this site means
gained or lost, the six series hues deliberately contain neither, and **their order is a
colour-vision safety mechanism rather than decoration**. A second hand-maintained copy
would drift and take the argument with it. Edit the original; this one is regenerated.

**Auth is proxied, never reimplemented.** `paper api` owns the allowlist, the one-time
codes and the session cookie. This app sends `credentials: "same-origin"` and, on a 401,
sends the browser to `/login`. In `next dev` the rewrite in `next.config.ts` makes :3000
look like the API's origin, because a cross-origin dev server would drop that cookie and
every request would arrive unauthenticated — which reads as an auth bug rather than a
dev-proxy one.

## Two invariants worth keeping

**Paging windows the ROWS and nothing else.** `n_rules`, `noise_ceiling`,
`exposure_corr` and the fold count stay defined over the whole population, so page 4
describes the same sheet page 1 does. A statistic that moved with the page would make two
screens of one leaderboard disagree about how many rules were searched — and that number
is what every `dsr` on it is deflated against. There is a test in
`../paper api/test_research.py` asserting exactly this.

**Page on `n_ranked`, never `n_rules`.** `n_rules` counts the whole candidate population
including everything dropped before ranking: never scored by the standard, no book row,
never opened a position, closet trackers. Paging on it offers pages that are structurally
empty.

## Commands

```bash
npm run dev      # :3000, proxying /v1 to http://127.0.0.1:8000
npm run build    # -> out/, which `paper api` serves at /next/
```

`NEXT_PUBLIC_API_ORIGIN` moves the dev proxy target. `NEXT_PUBLIC_BASE_PATH` must match
whatever path the API serves the export from — `/next` today.

## Where the data comes from, and why it is four sources

The vanilla board gets everything from `data.js` — one 3.7 MB document loaded as a
`<script>` and read off a global. **This app must not do that**: 87% of that file is the
`backtest` section the paged leaderboard exists to stop shipping, so fetching it would undo
the paging with nothing on screen to say so.

| source | what | notes |
|---|---|---|
| `/v1/research/leaderboard` | the ranked sheet | PAGED, `assets=false`. One page is one request |
| `/v1/research/row/{cls}/{tf}/{rule}` | one row + its sheet context | the detail page's hero strip. Without it, finding one label means paging the whole sheet |
| `/v1/board/meta` | config and headline numbers | ~46 KB, one request at start-up. Gates, timeframes, group labels, universe notes |
| `/live.json`, `/robust.json`, `/curves/*` | the big per-view files | fetched by the one view that draws each |

`/v1/board` exists because a `<script>` is not an API; `../paper api/CLAUDE.md` carries the
reasoning. It parses the same document `payload.py` writes and derives nothing, and it
**will not serve `backtest`** — a baked ranking beside the live one is the drift
`tools/test_board_equivalence.py` exists to catch, coming back through a different door.

## What the port had to get right, and what it deliberately changed

The vanilla board's decisions are ported, not just its pixels. `../Stockhunt Dashboard/CLAUDE.md`
is the authority on all of them; the ones easiest to undo by accident:

* **The two money columns are coloured on different questions.** `$10k / book` on raw money,
  `book vs B&H` on risk-matched skill. Colouring the first on the second tells a reader they
  made money they did not make.
* **One sizing on the detail page.** Every benchmark at the strategy's own volatility. There
  is no full-size toggle, because offering the misleading reading as an equal option was
  rejected deliberately; the full-size figures are caption prose.
* **The chart seeds the top five on the DELIVERED order**, never on the column the reader has
  since sorted by — "the top five" has to mean the same five whichever way the table points.
* **`realised` and `pnl` are different questions** on a fill, and a payload with no `realised`
  key at all is a THIRD state that prints em-dashes rather than asserting "0 closed trades".
* **The desk is not a fund.** No desk total, no mean P&L across systems.
* **A column without a `doc` is the one column nobody can ask about** — here that is a *type*
  requirement, so such a column does not compile.

Three things are deliberately NOT the same as the vanilla, and each is an upgrade rather
than drift:

* **The board pages.** The old one cannot; that is the whole reason this app exists.
* **A page in flight looks different from one that has landed** — `app/busy.css`. The old
  board never waits for anything, so it needed none of this.
* **`Robustness` is reduced in the browser** from `/robust.json`, using
  `payload.robustness_index`'s own definition. `board_rank` cannot know about robustness —
  importing pandas and `stockhunt.resultsdb` and nothing else is what lets the HTTP layer
  start without a TA-Lib build — and this app has no baked payload to copy the counts from
  the way `loadLiveBoard` does. Verified against the baked counts before it was trusted.

## Two names that are not interchangeable

`us_stocks` / `us_etfs` / `cme_futures` are **class** names and are what every
`/v1/research/*` route takes. `stocks` / `etf` / `futures` are **group** keys, and are what
`/v1/board/meta`'s `groups`, `robust.json`'s `envs` and the `curves/board_<key>_<tf>.json`
filenames use. `CLASS_GROUP` in `components/BoardChart.tsx` is the one mapping; import it
rather than writing a second one.

## What is not ported

`dist/dashboard.html` **cannot be produced from here** and is not meant to be. It is one
self-contained file with no server behind it; a static export is a directory, and this app
has no data without an API to ask. That artifact stays with the vanilla builder.

`enhanceTables` — the site-wide horizontal-scroll fade over every table — is an `app.js`
enhancer rather than a feature of any one view. Tables still scroll via `.tbl-wrap`.

## Portfolios, and the one seam that makes them buildable

A **portfolio** is a named basket of strategy legs with one pot of money, one combined
curve and one switch. Four routes, and a fifth thing bolted onto the leaderboard:

| route | what |
|---|---|
| `/portfolio/` | every basket visible to the reader, with a sparkline of its combined curve |
| `/portfolio/detail/?id=` | THE page: the curve, the legs and what each contributed, how alike the legs are, the membership log, the switch |
| `/paper/` | rebuilt portfolio-first — the desk's top-level row is a basket, and what belongs to none of them is its own labelled section |
| `/` | "As a portfolio" on the floating selection bar: preview the ticked rows blended, then create |

Both new pages take a **query string**, for the same reason `/rule/` does: `output:
"export"` pre-renders every route and a dynamic segment would mean enumerating every id
at build time from an API that needs a session.

**`lib/portfolio.ts` is the whole seam, and that is the point.** `BlendResponse` is
`stockhunt/blend.py`'s wire shape and `adaptBlend` is the ONLY function that reads it;
every component — chart, leg table, correlation panel, sparkline — is written against
`Blend`, which that file defines. The front end was built against the assumed shape before
the engine existed and moved to the real one by editing the adapter and nothing else.
Keep it that way: a component that reaches into a response field directly is the thing
this seam exists to prevent.

Two conversions live in the adapter and nowhere else. The engine's `curve` is **dollars
starting at `capital`**, and it is rebased to growth of 100 there because that is the
convention every chart on this site is written against; and every rate it reports is a
**fraction** (`cagr: 0.085`), so each page has one `pct()` helper rather than a scattering
of `* 100`.

**Three honesty properties belong to these pages specifically.**

* **`want` vs `state`, with the heartbeat.** The API writes intent, the desk writes what it
  did, and they disagree while it catches up. `want <> state` alone says the same thing
  whether the desk read the row a second ago or has been down since Tuesday, so
  `settlementOf` reads `/v1/desk` and draws the four states `paper api/web/desk.html`
  established: settled, in flight, nobody is home, and *never beaten* — which is NOT the
  same claim as down. The switch shows `want` and never moves optimistically.
* **A portfolio may print a combined figure; the DESK may not.** A portfolio is genuinely
  one pot split equally, so the equal-weight mean across its legs is that pot's result —
  but only when every leg has a record, and it prints an em-dash until then. There is still
  no desk total and no mean across portfolios.
* **Correlation is not a heat map.** Colour means gained or lost here, so a tinted matrix
  would be read as profit and loss. Magnitude is bar length from a centre line in one
  neutral ink; the six series hues carry leg IDENTITY only, in their fixed order, matching
  the slot each leg has elsewhere on the page.

`NEXT_TURBOPACK_ROOT` in `next.config.ts` is unset in a normal checkout and exists for one
case: building inside a git worktree, whose `node_modules` is a junction back to the main
checkout. Turbopack refuses to follow a symlink out of the project root and dies before
compiling; point this at a directory containing both and it builds.
