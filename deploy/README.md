# deploy/

What runs the public desk at **https://srv1903626.hstgr.cloud**, kept here rather than
only on the box. These files are the deployment; the VPS holds copies of them.

    root@186.241.18.137   Ubuntu 22.04, repo at /opt/stockhunt, one venv, user `stockhunt`
    ssh -i ~/.ssh/id_ed25519_stockhunt root@186.241.18.137

## Three services and three timers

| unit | what |
|---|---|
| `stockhunt-api` | the board + manager desk on 127.0.0.1:8080, nginx terminates TLS |
| `stockhunt-desk` | `run_paper.py`, the live Nautilus desk that publishes `live.json` |
| `stockhunt-refresh.timer` | rebuilds `data.js` + `paper_curves.json` at 00/06/12/18:05 |
| `stockhunt-autodeploy.timer` | every 5 min: if `origin/master` moved, deploy the board+API |

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
cp deploy/*.sh /opt/stockhunt/ && cp deploy/systemd/* /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now stockhunt-api stockhunt-desk \
    stockhunt-refresh.timer stockhunt-autodeploy.timer
/opt/stockhunt/autodeploy.sh --init
```

`.env.local` is **not** in git and never will be. Copy it over SSH; the API needs
`GMAIL_USER` / `GMAIL_APP_PASSWORD`, the desk needs `TWELVEDATA_API_KEY`. `data/reference/`
plus the 1d and 4h bars (~384 MB) are needed by `paper_curves.py` and `build_dashboard.py`,
not by the desk, which warms up over REST.

**The unit files quote their paths, and that is load-bearing.** systemd splits directive
values on whitespace and takes quotes literally in single-path settings, so
`WorkingDirectory=/opt/stockhunt/paper api` cannot be spelled at all — both units `cd` in a
shell instead. `ReadWritePaths=` is a list type and does honour quotes.
