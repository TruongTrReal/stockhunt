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

**Served at `/next/`, not `/`.** `/` still serves the vanilla board, so the two coexist
and this one can be wrong without taking the working one down. Flipping it to the root is
a routing change in `api_board.next_board` plus `NEXT_PUBLIC_BASE_PATH` at build time.

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

## What is not ported yet

The vanilla board is ~4,300 lines across ~25 views. This carries the leaderboard and its
paging. Still on `../Stockhunt Dashboard/web/app.js`: the strategy detail page and its
equity charts (hand-rolled SVG, no chart library), the robustness matrix, the paper desk,
the live tick blotter over `/ws`, and the hover/long-press column documentation.

**`dist/dashboard.html` cannot be produced from here** and is not meant to be. It is one
self-contained file with no server behind it; a static export is a directory, and this app
has no data without an API to ask. That artifact stays with the vanilla builder for as
long as it is wanted.
