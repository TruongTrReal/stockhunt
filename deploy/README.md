# deploy/

What runs the public desk at **https://srv1903626.hstgr.cloud**, kept here rather than
only on the box. These files are the deployment; the VPS holds copies of them.

    root@186.241.18.137   Ubuntu 22.04, repo at /opt/stockhunt, one venv, user `stockhunt`
    ssh -i ~/.ssh/id_ed25519_stockhunt root@186.241.18.137

`nginx/` holds the TLS front door as it stands on the box. **Nothing here applies it** --
`autodeploy.sh` restarts the API and never touches nginx, so a change to those files is
inert until somebody copies it over and reloads. They are in the repo because the one
thing that had gone wrong in them, a `gzip_types` list that silently skipped 4 MB of
JavaScript per page load, was invisible for as long as nobody could read the file
alongside the code that produced the responses. See `nginx/README.md`.

## The Next board

`dashboard-next/` is a second front end for the same API, served at **`/next/`** while the
vanilla board keeps `/`. It is a `next build --output export`: a directory of static files,
no Node process, so nothing new is supervised here.

**It needs Node 20+ on the box.** `autodeploy.sh` builds it when `dashboard-next/` or
`Stockhunt Dashboard/web/app.css` moved, or when there is no export yet, and **a failed
build is not a failed deploy** -- `out/` is gitignored, so `git reset --hard` leaves the
previous export alone and `/next/` goes on serving it. A five-minute deploy loop that a
slow npm registry can break is a board taken down by somebody else's outage.

nginx needs no change: `location /` proxies everything to 127.0.0.1:8080 and the export is
served by the same FastAPI process, behind the same session.

## Four services and three timers

| unit | what |
|---|---|
| `stockhunt-api` | the board + manager desk on 127.0.0.1:8080, nginx terminates TLS |
| `stockhunt-desk` | `run_paper.py`, the live Nautilus desk that publishes `live.json` |
| `stockhunt-mirror` | `alpaca_mirror.py`, the broker-side copy of the desk's book |
| `stockhunt-refresh.timer` | rebuilds `data.js` + `paper_curves.json` at 00/06/12/18:05 |
| `stockhunt-autodeploy.timer` | every 5 min: if `origin/master` moved, deploy the board+API |
| `stockhunt-rotation.timer` | every 5 min, 19:00-21:55 UTC: the monthly ETF rotation |
| `stockhunt-research.timer` | every 2 min: score anything submitted through `/v1/research` |

## The mirror needs credentials, and they are not in git

`stockhunt-mirror` is the only unit here that talks to a third party with a secret, so it
is the only one with a provisioning step. Six values go in `/opt/stockhunt/.env.local`,
one key pair per Alpaca paper account:

    ALPACA_STOCKS_KEY_ID / ALPACA_STOCKS_SECRET
    ALPACA_ETFS_KEY_ID   / ALPACA_ETFS_SECRET
    ALPACA_CRYPTO_KEY_ID / ALPACA_CRYPTO_SECRET

`.env.local` is gitignored and owned by `stockhunt` at mode 640, so **`autodeploy.sh` never
touches it and a fresh clone will not have it.** A rebuilt box needs these pasted in again;
without them the unit starts, logs `no Alpaca credentials configured`, and exits 1 into a
restart loop, which is the loudest available way of saying so.

The host is a constant in `alpaca_client.py` with no environment override, so nothing in
this file or in the unit can point the mirror at real money.

```bash
cd "/opt/stockhunt/paper trading engine"
sudo -u stockhunt /opt/stockhunt/.venv/bin/python alpaca_mirror.py --check
sudo -u stockhunt /opt/stockhunt/.venv/bin/python alpaca_mirror.py --once --dry-run
systemctl enable --now stockhunt-mirror
journalctl -u stockhunt-mirror -f
```

**Run it as `stockhunt`, never as root** -- the same trap as the rotation manager one
section down. A root run leaves a root-owned `alpaca.db-wal` beside a `stockhunt`-owned
database and the service then cannot write its own record.

`autodeploy.sh` restarts the API and never this unit, on the same reasoning that keeps it
away from `stockhunt-desk`: the mirror holds a broker-side position, and a restart mid-cycle
is a reconciliation that has to happen again. It is listed in `DESK_PATHS`, so a deploy that
changes its code reports a pending restart rather than performing one.

## The rotation manager is a client, not a fourth service

`stockhunt-rotation` runs `paper trading engine/rotation_manager.py`, which posts orders to
the public API exactly as an outside manager's code would. It imports no Nautilus, opens no
database and holds no position, so **it can fail, hang or be restarted without the desk
noticing** -- the worst case is a delayed rebalance, never a corrupted record. That is why
it is its own oneshot unit rather than a thread inside `stockhunt-desk`.

It fires ~36 times a day and does nothing on almost all of them. The window is wide because
15:45 New York is 19:45 UTC in summer and 20:45 in winter, and the DST arithmetic belongs
in the manager (where `test_rotation_manager.py` pins it) rather than in a timer. Repeat
firings inside the window are deliberate and free: `client_order_id` is derived from the
session date, so a retry after a network error returns the first order instead of opening a
second position.

**No credentials, and no console step.** It writes to `paper trading engine/state/desk.db`
directly -- the same ledger `api_orders` writes to and `desk_control` drains. The HTTP API
exists so a manager who is *not* on this box can reach that ledger; this one is on it. The
registration is created by the manager itself on its first firing and `deskdb.register` is
idempotent on `(account, name)`, so there is no provisioning step to forget and no way to
end up with two books splitting the capital.

```bash
cd "/opt/stockhunt/paper trading engine"
# decide now and print the orders without queuing them
sudo -u stockhunt /opt/stockhunt/.venv/bin/python rotation_manager.py --force --dry-run
# the registration, the desk's view of the book, and the last few orders
sudo -u stockhunt /opt/stockhunt/.venv/bin/python rotation_manager.py --status
systemctl enable --now stockhunt-rotation.timer
```

**Run it as `stockhunt`, never as root.** The unit does, and a stray root run leaves a
root-owned `desk.db-wal` beside a `stockhunt`-owned database, which the desk then cannot
write to. That failure is silent until the next order.

It needs one package the desk does not: `pandas_market_calendars`, for the exchange calendar
that answers "is today the last trading day of the month". That question cannot be answered
from a calendar date -- the strategy this replicates tested `date.day == monthrange(...)` and
therefore skipped 30% of its months. Without the package the manager refuses to guess and
skips, which is the safe failure but is still a failure:

```bash
/opt/stockhunt/.venv/bin/pip install pandas_market_calendars
```

## The research board needs `results.db`, and it is not in git

The leaderboard is a **query** now, not a payload baked into `data.js`:
`board_rank.build_sheet` reads `walk-forward optimization/results/results.db`, and both the
API and the loopback server call it. The store is gitignored on purpose -- it is built from
the tracked result CSVs by `tools/ingest_results.py` and is regenerable in ten seconds, so
a committed copy would be a binary rebuild of already-committed data in every diff.

**So a fresh clone has no store, and the failure is silent.** `/v1/research/board` answers
503, `app.js` treats any non-200 as "no live board" and keeps the baked payload, and the
page renders perfectly with whatever the last build froze. Nothing on screen says the board
stopped being live. `refresh-board.sh` therefore ingests before it builds, and
`stockhunt-refresh.service` carries the results directory in `ReadWritePaths=` -- without
that, the ingest fails on a read-only filesystem and the outcome is identical to not
running it at all.

`stockhunt-api.service` writes there too, and only one kind of row: a scoring **job** from
`POST /v1/research/trials`. It ranks out of the same file and scores nothing itself.

```bash
sudo -u stockhunt /opt/stockhunt/.venv/bin/python /opt/stockhunt/tools/ingest_results.py
sudo -u stockhunt /opt/stockhunt/.venv/bin/python /opt/stockhunt/tools/test_board_equivalence.py verify
```

### `stockhunt-research` is the thing that scores a submission

`research_worker.py` drains the queue the API writes: causality gate, register the trial,
`strat_wf` -> `riskmatch_wf` -> `merge_book`, insert rows. The board is a query, so the
rule is on it at the next request and nothing is rebuilt.

**A timer, not a daemon.** `--watch` exists and is deliberately unused: the worker holds
pandas, numpy and TA-Lib resident and fires rarely, and memory is already what caps how
many cores a sweep may use on this box. Overlap is safe twice over -- systemd skips a
trigger while the unit is still running, and `claim_job` takes its row under
`BEGIN IMMEDIATE`, so two workers on one database cannot take the same job.

Like `stockhunt-rotation`, it is a **client of a ledger, not part of the desk**. It can
fail, hang or be killed; the worst case is a submission scored late, and nothing it touches
is a position or a forward-test record.

```bash
systemctl enable --now stockhunt-research.timer
journalctl -u stockhunt-research -n 50          # what it scored, and what it refused
# one job, in the foreground, when something looks stuck
cd "/opt/stockhunt/walk-forward optimization"
sudo -u stockhunt /opt/stockhunt/.venv/bin/python research_worker.py --once
```

**Run it as `stockhunt`, never as root** -- the same rule as the rotation manager, and the
same silent failure if it is broken: a root-owned `results.db-wal` beside a
`stockhunt`-owned database, which the API then cannot write a job into.

It writes into `strategies/published/` when somebody submits a module, which has one
consequence worth knowing: the position cache keys on a hash of `strategies/**`, so an
accepted submission makes the next full `sweep.py` or `walkforward.py` run cold. That is
over-invalidation, not staleness. A submission the causality gate refuses is removed again
and costs nothing.

## The split, and why it exists

**`autodeploy.sh` never restarts the desk.** The API and the board hold no state — a
restart costs milliseconds and loses nothing, so they follow every push. The desk holds
positions and a forward-test record, and restarting it flattens every book and re-warms
1,500 bars. That is a decision, not a side effect of fixing a typo.

When a push changes code the desk loads, autodeploy writes
`/opt/stockhunt/DESK_RESTART_PENDING` naming the files, and stops. Restart when you want
it:

```bash
cat /opt/stockhunt/DESK_RESTART_PENDING     # what changed, and why it is waiting
systemctl restart stockhunt-desk            # ~20s to the feed, longer to the first signal
```

## Why a running deploy does not corrupt the databases

`paper trading engine/results/paper.db` and `state/desk.db` are **tracked files** — the
repo keeps them on purpose, they are the record. So `git reset --hard` reverts them to the
committed blob even when the commit never touched them, because the desk has been writing
them since the last commit. That happened once: the restored `.db` was then replayed
against the surviving `-wal` sidecar, two different databases sharing one write-ahead log,
and `PRAGMA integrity_check` came back with unused pages.

Both files are therefore marked **`skip-worktree`** on the server, which makes `reset
--hard` leave them alone (verified on git 2.34.1). That is what lets `autodeploy.sh` pull
while the desk is mid-write:

```bash
./autodeploy.sh --init          # idempotent; re-run after a fresh clone
git ls-files -v | grep '^S'     # confirm the protection
```

### `skip-worktree` is not enough on its own, and the gap is silent

It means *"assume the worktree matches the index"*, and git honours that only while nothing
asks it to change the file. **A commit that TOUCHES a live database does ask.** Git compares,
finds the desk has rewritten the file since, and refuses the entire operation:

```
error: Entry 'paper trading engine/results/paper.db' not uptodate. Cannot merge.
fatal: Could not reset index file to revision 'origin/master'
```

The deploy exits 128 having changed nothing — and then does it again every five minutes,
into a log nobody reads, while the board serves old code and the timer keeps reporting
`active`. It happened on 2026-08-17: a commit carried a `paper.db` migration, and two
autodeploy ticks failed before anyone looked. The failure is not that it broke; it is that
it broke **invisibly and kept looking scheduled**.

`settle_live_dbs` closes it. Before every reset — the deploy's and the rollback's, which has
the same exposure in the other direction — it points the index straight at the incoming blob
for those paths and leaves the worktree alone, so `reset --hard` has nothing left to change
there. **The live file is never read, never copied and never written.** That matters: it is
being written throughout, and a hot copy of a live SQLite file is exactly the corruption
above. `redeploy.sh` can afford to copy because it stops the services first; this one
deliberately cannot, so it does not copy at all.

If it ever fails again, this is the manual recovery — it is what `redeploy.sh` does, plus
clearing the protection so the reset can proceed:

```bash
cd /opt/stockhunt
git update-index --no-skip-worktree "paper trading engine/results/paper.db" \
                                    "paper trading engine/state/desk.db"
./redeploy.sh                   # stops services, sets DBs aside WITH sidecars, restores
./autodeploy.sh --init          # put the protection back -- do not skip this
```

### The deployed scripts are copies, and they go stale

The units run `$REPO/autodeploy.sh`; `deploy/autodeploy.sh` is what git updates. A fix to
either script therefore lands in the repo and changes nothing until it is copied across.
`autodeploy.sh` now logs `!! <script> is stale against deploy/<script>` when they diverge —
reported rather than applied, because bash reads a script incrementally as it runs and
rewriting the file mid-execution is its own class of bug.

```bash
cp /opt/stockhunt/deploy/*.sh /opt/stockhunt/     # the fix the warning asks for
```

### The UNIT files are copies too, and nothing warns about those

`autodeploy.sh` checks the `.sh` copies and says nothing about `/etc/systemd/system/`,
because it never installs them. A pull that changes a unit therefore lands in the repo and
changes nothing at all -- no warning, no failure, just the old unit still running.

That is not academic: adding a path to `ReadWritePaths=` is exactly the kind of change
that is invisible until the process tries the write. `stockhunt-api` gained
`walk-forward optimization/results` so it can queue a scoring job; without the unit being
reinstalled, the code deploys, the board renders, and every `POST /v1/research/trials`
answers 500 on a read-only filesystem.

```bash
cp /opt/stockhunt/deploy/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl restart stockhunt-api                  # picks up the new ReadWritePaths
systemctl enable --now stockhunt-research.timer  # if it is not enabled yet
systemctl list-timers 'stockhunt-*'              # confirm NEXT is not n/a
```

`redeploy.sh` — the manual, full, stop-everything path — does not rely on that: it stops
the services and physically sets each database aside **with its `-wal`/`-shm` sidecars**,
because a `.db` restored without its journal, or a journal left behind for a different
`.db`, is the corruption above.

## Which script to run

| | |
|---|---|
| `autodeploy.sh` | on a timer. Board + API only, rolls back if the API fails to start |
| `redeploy.sh` | by hand, when you want everything on the new code including the desk |
| `refresh-board.sh` | on a timer. Rebuilds the payload; pulls nothing |

Logs: `/opt/stockhunt/logs/autodeploy.log`, and `journalctl -u stockhunt-<unit>`.

## Installing on a fresh box

```bash
git clone https://github.com/TruongTrReal/stockhunt.git /opt/stockhunt
cd /opt/stockhunt && python3.12 -m venv .venv
.venv/bin/pip install fastapi 'uvicorn[standard]' httpx nautilus_trader==1.230.0 \
    TA-Lib numpy pandas pyarrow requests websockets       # TA-Lib needs the C library:
    # wget https://github.com/TA-Lib/ta-lib/releases/download/v0.6.4/ta-lib_0.6.4_amd64.deb
useradd -r -s /usr/sbin/nologin -d /opt/stockhunt stockhunt
# Gitignored, so absent on a fresh clone -- and a ReadWritePaths= entry pointing at a
# missing path makes systemd refuse to start the unit rather than skip the entry.
mkdir -p "/opt/stockhunt/walk-forward optimization/logs" /opt/stockhunt/.cache          "/opt/stockhunt/paper trading engine/logs" "/opt/stockhunt/paper api/logs"
chown -R stockhunt:stockhunt /opt/stockhunt
cp deploy/*.sh /opt/stockhunt/ && cp deploy/systemd/* /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now stockhunt-api stockhunt-desk \
    stockhunt-refresh.timer stockhunt-autodeploy.timer stockhunt-research.timer
/opt/stockhunt/autodeploy.sh --init
# The board is a query and its store is not in git. Build it before the first
# page load, or the board silently serves whatever `data.js` was baked with.
sudo -u stockhunt .venv/bin/python tools/ingest_results.py
```

`.env.local` is **not** in git and never will be. Copy it over SSH; the API needs
`GMAIL_USER` / `GMAIL_APP_PASSWORD`, the desk needs `TWELVEDATA_API_KEY`. `data/reference/`
plus the 1d and 4h bars (~384 MB) are needed by `paper_curves.py` and `build_dashboard.py`,
not by the desk, which warms up over REST.

**The unit files quote their paths, and that is load-bearing.** systemd splits directive
values on whitespace and takes quotes literally in single-path settings, so
`WorkingDirectory=/opt/stockhunt/paper api` cannot be spelled at all — both units `cd` in a
shell instead. `ReadWritePaths=` is a list type and does honour quotes.
