# CLAUDE.md

Guidance for Claude Code working in this directory. Read `../CLAUDE.md` first.

## What this is

The monitor: backtest results and live paper trading on one page. It is a **reader**.
Nothing here can place an order, fetch price data for the desk, or write into another
folder's results.

One payload, two outputs:

```
payload.py            reads the CSVs -> one document
   |
build_dashboard.py --serve   -> web/data.js + web/curves/*.json    (SPA, live)
                   --dist    -> dist/dashboard.html                (one file, no server)
```

There used to be two independent builders over two different subsets of the same CSVs,
with two separate implementations of every view. They drifted: at the time they were
merged the single-file board was a day stale and did not show paper trading at all, which
is the thing this dashboard exists to watch. Do not re-fork them.

## Commands

```powershell
python build_dashboard.py --serve            # rebuild what serve.py serves
python build_dashboard.py --dist             # rebuild the shareable single file (9.2 MB)
python build_dashboard.py --dist --no-curves # ...without the curve JSONs (4.2 MB)
python build_dashboard.py --serve --offline  # skip the live price snapshot
python paper_curves.py --top 5               # rebuild web/paper_curves.json

.\run.ps1                                    # serve on 127.0.0.1:8765
.\run.ps1 -Tunnel                            # ...plus a public trycloudflare.com URL
.\run.ps1 -Stop
```

`--offline` matters: the price snapshot is the only network call in the whole folder, and
everything else comes off local CSVs. A dead API key must not block a rebuild.

## Where the numbers come from

| section | source |
|---|---|
| backtest leaderboards, per-asset, combos | `../walk-forward optimization/results/wf_*`, `cwf_*` |
| equity curves | `../walk-forward optimization/results/curves_*.json` |
| research summary, gate power, prereg | same folder, `wf_meta`, `prereg_*` |
| ETF sheets | `../top 20 stocks/results/` (frozen, read-only) |
| parity | `../paper trading engine/results/parity_live_1d.csv` |
| paper trading | `../paper trading engine/results/paper_state.json` |
| live tick stream | `web/live.json`, republished by the desk every ~2s |

Nothing comes from `../backtest engine/results/` — every figure on this dashboard is
walk-forward, which is the point.

## `serve.py` serves and nothing else

Static files plus the WebSocket on **one port**, because two ports break the moment the
desk is shared: an HTTPS tunnel forbids a `ws://` socket from an HTTPS page (mixed
content), and a quick tunnel exposes exactly one port. One origin, one URL to hand out,
and `wss://` follows from `https://` automatically.

The `/ws` handler is a **file watcher** on `web/live.json`. It never talks to the trading
node. If this process dies the desk keeps trading.

Files are served from an **allowlist**, not "whatever is under `web/`", because this may
face the public internet through a tunnel and the directory also holds `demo_data.js`.
Path resolution additionally does `.resolve()` + `is_relative_to` to block traversal. If
you add a served file, add it to `ALLOWED` or it 404s.

A bare `HEAD /` is rejected by `websockets` while parsing the request line — before
`process_request` runs — so it cannot be answered properly, only muted. `_DropNonGet`
does that; the connection is still refused, the traceback is not logged.

## The single-file build does not fork the application

`app.js` is unmodified. A ~20-line shim installed ahead of it overrides `fetch` to answer
out of an embedded map (`window.__EMBEDDED__`) and stubs `WebSocket`, so the same views
render a frozen snapshot — charts included. Anything not embedded falls through to a
synthetic 404, which the application already treats as "no data".

That is why there is one implementation of every view instead of two. Keep it that way: a
change to a chart should never need to be made twice.

## Gotchas

- **`web/data.js`, `web/live.json`, `web/curves/`, `web/paper_curves.json` and
  `dist/dashboard.html` are generated.** Edit `payload.py`, `app.js`, `app.css` or
  `index.html`; never the outputs.
- **`demo_data.js` is the layout fixture and must never be the tag that ships.** It sets
  `window.DEMO = true`, which renders a warning bar. It is deliberately not in `ALLOWED`.
- **`drop_selection_rows` is a correctness requirement, not a filter.** `IS#1` rows are the
  *act of choosing* a rule scored as a strategy. In a list sorted by IR they read as one
  more candidate someone might pick up and trade. They stay in the CSVs, which is where
  the selection-cost question belongs.
- **Curves are not inlined into `data.js`.** Fifteen rules x twenty assets x two series is
  several MB per sheet, and every visitor would parse all four before the leaderboard could
  render. The detail view fetches its sheet on demand.
- **Do not import `run_paper` or `backtest_paper` here.** Both pull in `nautilus_trader` at
  module scope. `paper_config` has what this folder needs (`top_rules`, the universe, the
  warm-up constants) and imports nothing heavy.
- **The tunnel URL changes on every start.** A quick tunnel has no account and no stable
  hostname; the current one is printed into `logs/tunnel.err` a few seconds after launch.
