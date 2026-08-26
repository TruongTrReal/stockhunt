# Working this repo from several sessions at once

How to run more than one Claude Code session (or terminal, or person) against this
checkout without the two of them quietly ruining each other's work.

**Structure only, no results** — same rule as `CLAUDE.md`. Nothing here changes if a
backtest is re-run tonight.

The short version: the folder layout is *almost* the partition — the real one is the file
set (§7) — so most parallel work needs no worktree at all. What needs isolating is the
shared core, and what needs serialising is the push.

## 1. What is actually shared

Everything below follows from this table.

| resource | size | who writes it | safe to share? |
|---|---|---|---|
| `data/` | 8.5 GB | `td_loader.py`, `db_loader.py` | **read: yes.** Write: one session. A fetch mid-run changes another session's OHLCV fingerprint and silently cold-cases its cache |
| `.cache/positions/` | 1.4 GB | every sweep stage | **yes, genuinely** — see below |
| `strategies/**`, `signals.py`, `engines/vector.py` | — | you, adding a rule | **no.** The *code half* of the cache key. One edit invalidates every entry for every class and timeframe |
| `stockhunt/` | — | refactors | **no.** Imported by all five pipeline folders |
| `walk-forward optimization/results/` | 1.8 GB | wf stages, rewritten **whole** | **per sheet.** Same class+timeframe from two sessions = last writer wins, silently. Different class = different file = fine |
| `backtest engine/results/` | 1.2 GB | sweeps | same |
| `results.db` | — | `ingest_results.py`, `research_worker.py` | many readers (`timeout=10.0`), **one writer** |
| `paper.db`, `desk.db` | — | the live desk | **never two.** Both tracked in git — two branches writing them is a binary merge conflict with no resolution |
| `.venv` | — | `pip` | fine. `stockhunt` is **not** installed editable, so there is no cross-checkout import leak. Don't `pip install` from two sessions |
| **12 CPU cores** | — | `stockhunt.parallel` | **the real limit.** `RESERVED_CORES = 2`, so each session defaults to **10 workers** |

**Why the position cache is safe to share.** `poscache.close()` writes to a pid-unique
temp and then `os.replace`s it, and every failure path is swallowed: a write that loses a
race is dropped, and `RuleCache.get()` treats a truncated or half-written npz as a miss
rather than raising. Two sessions generating the same cell cost each other one
regeneration. They cannot cost each other a wrong number.

## 2. Two lanes, cheapest first

### Lane A — one checkout, one session per folder. The default.

Rule #1 in `CLAUDE.md` ("run each folder's scripts from that folder") already isolates
these. Open one session per folder:

```
session 1   cd "paper api"                 # HTTP layer, auth, /v1/*
session 2   cd "Stockhunt Dashboard"       # builder, app.js, board_rank
session 3   cd "backtest engine"           # sweeps, td_loader, check_data
session 4   cd "walk-forward optimization" # wf stages
```

Disjoint files, shared warm cache, shared bars, nothing to merge. Most work fits here.

More than one task inside **one** of these folders is usually fine too, but the rule
changes from "one folder each" to "one file set each" — see §7.

The cost is one index and one working tree for all of them — see §4, which is the part
that bites.

### Lane B — a worktree. For the shared core only.

Use it when the work touches shared state or might be thrown away:

* editing `stockhunt/` or `strategies/**` (invalidates everyone's cache)
* a refactor you may abandon
* anything gated on `tools/golden.py capture` -> change -> `verify`, which is meaningless
  if another session is writing sheets underneath it

A worktree is **not free here**: after junctioning `data/` and `.cache/` it is still
~3 GB of checkout, because the result CSVs are tracked research record, not build output.

## 3. Worktree recipe

`stockhunt/paths.py` sets `REPO_ROOT` from its own location, so in a worktree `REPO_ROOT`
becomes the **worktree** and the gitignored things go missing. That is the whole trick:

```powershell
$main = "C:\Users\Truong\Documents\work desk\quant python projects\stockhunt"
$wt   = "$main\.claude\worktrees\core-refactor"

git -C $main worktree add $wt -b task/core-refactor master

# what paths.REPO_ROOT expects and git does not carry. Junctions need no admin.
New-Item -ItemType Junction -Path "$wt\data"   -Target "$main\data"
New-Item -ItemType Junction -Path "$wt\.cache" -Target "$main\.cache"
Copy-Item "$main\.env.local" "$wt\.env.local"
```

Use the main venv by absolute path. **Do not junction `.venv`** — it has absolute paths
baked into `pyvenv.cfg` and the launcher exes:

```powershell
& "$main\.venv\Scripts\python.exe" -m pytest -q
```

Three gotchas specific to this repo:

1. **`results.db` is untracked**, so a fresh worktree has none and the board comes up
   empty. Either run `python tools\ingest_results.py` there, or read the main one:
   `$env:STOCKHUNT_RESULTS_DB = "$main\walk-forward optimization\results\results.db"`.
   Read-only — it is the one-writer resource.
2. **`paper.db` and `desk.db` are tracked**, so a worktree gets a live copy. Never run
   `run_paper.py` in a worktree without redirecting `STOCKHUNT_DESK_DB` and
   `STOCKHUNT_PUBLISH_DIR`, or you produce two divergent binary forward-test records.
3. **`.claude/settings.json` is tracked**, so the `test research/` and `top 20 stocks/`
   deny rules follow into the worktree. The locks hold.

## 4. Committing, and the one thing that is not isolated

**In Lane A all sessions share one index and one working tree.** `git status` shows
everybody's edits. So:

```powershell
# commit ONLY this session's folder, whatever else is staged or dirty
git commit -m "..." -- "paper api"
```

The `-- <paths>` form commits those paths *regardless of index state*, which is what makes
it safe when someone else has staged something. Never `git commit -a`.

Four commands destroy other sessions' uncommitted work. Do not run them in a shared
checkout without checking with the other sessions first:

| command | what it does to everyone else |
|---|---|
| `git commit -a` | commits their half-finished work under your message |
| `git stash` | takes their work with it |
| `git checkout .` / `git restore .` / `git reset --hard` | deletes it |
| `git pull --rebase`, `git merge`, branch switch | rewrites files under a **running job** — a sweep that re-reads `strategies/**` mid-flight sees a new cache fingerprint |

That last row is the one people forget: committing is safe during a long run because it
does not touch the working tree. Merging and pulling are not.

Lane B sessions each have their own index and their own branch, so they commit freely.
Git will refuse to check out one branch in two worktrees, which is a useful safety net
rather than an obstacle.

### Pushing to master is a deploy

This is the real answer to "can each session land its own work independently".

`deploy/systemd/stockhunt-autodeploy.timer` polls `origin/master` **every five minutes**.
On a new commit `deploy/autodeploy.sh` hard-resets the VPS to it, restarts
`stockhunt-api`, and rebuilds the board payload — rolling back automatically if the API
fails to start.

So:

* **committing is private.** Any number of sessions, any order, no interference.
* **pushing to master is outward-facing and shared.** Half-finished web or API work on
  master is live within five minutes.

The desk is deliberately excluded: `autodeploy.sh` never restarts `stockhunt-desk`,
because that flattens every book and re-warms 1,500 bars. It writes a
`DESK_RESTART_PENDING` marker and leaves the call to a human. If your change is under
`paper trading engine/`, `stockhunt/`, `strategies/`, `backtest engine/` or
`walk-forward optimization/results/`, pushing it does **not** put it live — someone must
restart the desk on purpose.

Practical rule: **each session commits whenever it likes; one session pushes, and pushes
a coherent state.** If two lanes must land together, push once.

Merge order for Lane B branches: **core first**, features after. A `stockhunt/stats.py`
change buried under a feature diff is a conflict nobody enjoys.

## 5. Rules of the road

* **Budget cores explicitly.** Two heavy sessions: `$env:STOCKHUNT_WORKERS = 5` in each.
  Left at the default that is 20 workers on 12 cores, and both jobs finish slower than
  running them one after the other.
* **The core lane is exclusive.** Nobody edits `strategies/` or `stockhunt/` while long
  research runs are in flight.
* **Long jobs:** `nohup ./script.sh > logs/x.log 2>&1 &` from bash, one log per lane.
  With several sessions this matters more, not less. See the PowerShell warning in
  `CLAUDE.md`.
* **Check progress by `parent_pid`.** `CLAUDE.md` already warns that a sibling repo's
  sweep looks identical to yours; with four of your own sessions running,
  `Get-Process python` is pure noise.

## 6. A split that parallelises

Grouped so no two lanes write the same sheet or the same `.py`.

| lane | folder | writes | isolation |
|---|---|---|---|
| web | `paper api/`, `Stockhunt Dashboard/web/` | html/js/css | Lane A |
| board | `board_rank.py`, `tools/ingest_results.py` | `results.db` | Lane A, the one writer |
| desk | `paper trading engine/` | `paper.db`, `desk.db` | Lane A, single session |
| research | `walk-forward optimization/` | `results/wf_* book_*` | Lane A **per class/timeframe** |
| core | `stockhunt/`, `strategies/`, `backtest engine/signals.py` | the cache key | **Lane B, alone** |

## 7. More than one task inside one folder

The partition in §2 is written per folder, but the folder was only ever a proxy. **The
real partition is the file set.** Two tasks in one folder are fine until they aren't, and
which case you are in decides the isolation.

| case | example | what to do |
|---|---|---|
| different files, no long job | `paper api/`: one on auth, one on `/v1/orders` | one checkout, two sessions. Scope commits to the **file** |
| same file | both editing `board_rank.py` | **worktree per task**, then merge. Nothing else arbitrates |
| same stage, different class/tf | `--class crypto` and `--class us_etfs` | one checkout — outputs are already keyed per sheet. But see the memory note below |
| same stage, same class+tf | two `walkforward.py --class us_stocks --tf 1d` | **don't.** The sheet is rewritten whole; last writer wins, silently. Worktree with `--out` redirected, or wait |

Scope the commit to the file, not the folder:

```powershell
git commit -m "..." -- "paper api/auth.py" "paper api/test_auth.py"
```

There is a partial safety net when two sessions share a checkout: an exact-string edit
fails if the region it matched has already changed, so it errors instead of clobbering.
It does **not** cover a whole-file rewrite, which overwrites whatever landed since.

**Different sheets are not free just because the files differ.** `run_book.sh` sets
`STOCKHUNT_WORKERS=6` rather than the default ten, and the header says why: ten drove free
memory to 3 GB of 32 GB. Two concurrent book runs at six each is back over that line — run
two sheets at **three workers each**, not six. The per-sheet outputs and logs
(`book_<cls>_<tf>.csv`, `logs/book_<cls>_<tf>.log`) do not collide, but the driver log
does: give the second run its own `> logs/book_driver_<cls>.log`.

### The per-folder singletons

These are one-per-folder, so "different files" does not save you:

| collision | where | fix |
|---|---|---|
| `logs/run.pids` | `paper api/run.ps1` and `Stockhunt Dashboard/run.ps1` each write **one** | **no override exists.** A second server overwrites it, and `-Stop` then kills the wrong process or loses one. Start the second by hand, or use a worktree |
| port | 8080 (api), 8765 (dashboard) | `-Port 9000` |
| `paper api/state/auth.db` | the allowlist and every live session | `$env:STOCKHUNT_API_STATE = "<tmp>"` |
| `logs/serve.log`, `logs/serve.err` | fixed names | separate state dir, or accept interleaved output |

`run.pids` is the sharpest of these, because `-Stop` looks like it worked.
