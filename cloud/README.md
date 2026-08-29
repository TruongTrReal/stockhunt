# cloud/

Run the walk-forward grid on a rented box instead of the workstation, then destroy the box.

    ./cloud/grid.sh up        provision, install, upload, start
    ./cloud/grid.sh status    alive? how far in? what has it cost?
    ./cloud/grid.sh fetch     pull results back (repeatable, safe mid-run)
    ./cloud/grid.sh down      DESTROY -- the only thing that stops billing
    ./cloud/grid.sh cost      hours x rate, touching nothing

## Why

The grid is ~20 hours of compute and it does not fit here. One `riskmatch_wf` cell peaks at
**9.6 GB**, and on 2026-08-27 an attempt to run it locally took the workstation down.

The failure is worth stating precisely, because it drove every design choice below. **RAM
never ran out** -- it sat at 12 GB free throughout. What ran out was the **commit charge**,
0.1 GB of 127.8 GB, because a deadlocked cell left six orphaned workers holding **85.3 GB
of commit with a zero working set**: entirely paged out, doing nothing, reserving
everything. Every process that started afterwards died on allocation, including
`make_book_rules.py` while merely importing pandas, and including the tooling used to
diagnose it.

The trigger was a guard of mine that **failed open**. Three chains each waited for the
others by polling the process list; under load the poll itself failed to spawn, produced no
output, and empty output was read as "nothing is running".

## What the design takes from that

**One process, phases in order, no cross-process guard.** A guard that can fail open is
worse than no concurrency, because the failure is silent and surfaces three stages later.

**`have_headroom` watches commit, not just RAM, and fails closed.** Watching free RAM would
have missed the entire event. An unreadable number stops the run.

**Every cell is banked the moment it finishes.** `riskmatch_wf` rewrites `edge_standard.csv`
whole unless the run is scoped, and scoping is judged on `--class`/`--rules` and **never on
`--tf`** -- so one class at a time is not a style preference, it is the difference between
adding a cell and deleting fifteen. Banking per cell also means a crash costs one cell.

**The destroy is in a trap.** Any failure between "instance created" and "grid running"
tears the box down. The instance id and a bare `curl` that deletes it are printed before
anything else happens, so a total loss of this script still leaves a way to stop the meter.

## Billing: stopping is not stopping

Vultr and Hetzner both bill a **powered-off** instance at the full rate -- it still reserves
CPU, RAM, disk and IP. Only **delete** stops the meter. `down` verifies the instance is
really gone and shouts with a manual command if it is not.

## The payload is 2.45 GB, not 9.6

`payload_manifest.py` names it: 2,581 files. The grid reads 1d/4h/1h/15m/5m, and 5m and 15m
are already materialised as their own parquets, so the 1m cache and the 2m/3m derived from
it -- 5.5 GB -- stay home. Two small things travel besides bars and both are load-bearing:

* `data/reference/` -- quarantine, point-in-time membership, the ETF and commodity entry
  dates, the futures roll ledger. Without them `td_loader.load` returns series it should
  have cut: gold and silver print a fabricated Open before 2006, and three recycled tickers
  are somebody else entirely.
* `data/rates/` -- the DTB3 path `riskmatch_wf` credits idle capital at. Absent, every
  part-time rule is scored as though cash earned nothing, which is the defect that stage
  exists to fix.

Everything else arrives by `git clone` from the public remote, so **no credentials travel**.
No vendor API keys either: the grid only reads the cache, nothing fetches.

## Before the first run

1. Vultr account with a card.
2. Add `~/.ssh/id_ed25519_stockhunt.pub` under **Account -> SSH Keys**.
3. Create an API key under **Account -> API**, and **whitelist your public IP** under Access
   Control -- Vultr blocks API calls from unlisted addresses, so the key alone is not enough.
4. Put it in `.env.local` as `VULTR_API_KEY=...`.

Defaults are `vc2-16c-64gb` in `sgp` (Singapore, ~30-50 ms from Vietnam) at ~$0.48/hr.
**64 GB rather than 32 is deliberate**: RAM is what binds, and 32 GB is the size the local
run died at. Override with `PLAN=`, `REGION=`, `RATE_USD=`.

## What it produces

The full 25-cell board: the four outstanding 15m verdicts, the entire 5m column (never
scored before -- zero `wf_summary` rows, nothing in `results.db`), the books those verdicts
unlock, and an ingest into `results.db`. `fetch` brings back sheets only, ~110 MB.

**Expect nothing to pass.** This repo's record is 0 of 231 indicators, 0 of 698 published
strategies, 0 of 82 formulaic alphas, and a coin flip at matched exposure currently outranks
`ibs`. The last full `edge_standard` had zero rows clearing all six criteria. What the money
buys is a *complete and correct measurement*, including a timeframe that has never been
measured at all -- not an expectation of an edge.
