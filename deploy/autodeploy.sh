#!/bin/bash
# Poll origin/master and deploy the STATELESS half automatically.
#
# Split on purpose. The API and the board hold nothing: restarting them costs a few
# hundred milliseconds and loses no state, so they should follow every push. The desk
# holds positions and a forward-test record, and restarting it flattens every book and
# re-warms 1,500 bars — that is a decision, not a side effect of fixing a typo. So this
# never touches `stockhunt-desk`; it reports when the desk is running code older than
# master and leaves the call to a human.
#
# Polling rather than a webhook: no inbound port, no shared secret, and a missed run
# self-heals on the next tick instead of being lost forever.
#
# The live databases are safe here even though the desk is WRITING them throughout,
# because they are marked `skip-worktree` (see `--init`): `git reset --hard` leaves them
# alone. Without that, a reset reverts them to the committed blob — they are tracked
# files — which is what corrupted paper.db once already.
#
# `skip-worktree` alone is not enough, and the gap is silent. See `settle_live_dbs`.
set -euo pipefail

REPO=/opt/stockhunt
LOG=$REPO/logs/autodeploy.log
PENDING=$REPO/DESK_RESTART_PENDING
LIVE_DBS=("paper trading engine/results/paper.db" "paper trading engine/state/desk.db" "paper api/state/auth.db")
# Anything the DESK process loads. Deliberately broad: over-reporting a pending restart
# costs a line of log, under-reporting means the desk quietly runs stale code.
DESK_PATHS=("paper trading engine/" "stockhunt/" "strategies/" "backtest engine/"
            "walk-forward optimization/results/")

mkdir -p "$REPO/logs"
say() { printf '%s  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG"; }

# --init: mark the live databases so git stops fighting the processes that own them.
if [[ "${1:-}" == "--init" ]]; then
    cd "$REPO"
    for db in "${LIVE_DBS[@]}"; do
        # Only TRACKED files need this. `paper api/state/` is gitignored, so git never
        # touches auth.db and marking it fails loudly for no reason.
        if git ls-files --error-unmatch "$db" >/dev/null 2>&1; then
            git update-index --skip-worktree "$db" && say "protected (skip-worktree): $db"
        else
            say "untracked, needs no protection: $db"
        fi
    done
    say "init done"
    exit 0
fi

# `skip-worktree` means "assume the worktree matches the index", and git honours that only
# while nothing asks it to change the file. A commit that TOUCHES a live database does ask.
# Git then compares, finds the desk has rewritten the file since, and refuses the whole
# operation:
#
#     error: Entry 'paper trading engine/results/paper.db' not uptodate. Cannot merge.
#     fatal: Could not reset index file to revision 'origin/master'
#
# The deploy exits 128 having changed nothing — and then does it again every five minutes,
# into a log nobody reads, while the board quietly serves old code. That is the whole
# failure: not that it broke, but that it broke invisibly and kept looking scheduled.
#
# So the paths are SETTLED first: point the index straight at the incoming blob and leave
# the worktree alone, so by the time `reset --hard` runs there is nothing left for it to
# change there. The live file is never read, never copied and never written — which is the
# point. It is being written throughout, and a hot copy of a live SQLite file is exactly
# the corruption `redeploy.sh` stops the services to avoid; that script can afford the copy
# because it takes the desk down, and this one deliberately cannot.
#
# Called before EVERY reset, including the rollback, which has the same exposure in the
# other direction.
settle_live_dbs() {
    local target=$1 db mode type sha rest
    for db in "${LIVE_DBS[@]}"; do
        # Untracked (`paper api/state/` is gitignored) — git never touches it.
        git ls-files --error-unmatch "$db" >/dev/null 2>&1 || continue
        # Unchanged by this commit — plain skip-worktree already covers it.
        git diff --quiet HEAD "$target" -- "$db" && continue
        read -r mode type sha rest < <(git ls-tree "$target" -- "$db")
        [ -n "${sha:-}" ] || continue
        git update-index --no-skip-worktree "$db"
        git update-index --cacheinfo "$mode,$sha,$db"
        git update-index --skip-worktree "$db"
        say "settled index for $db (kept the live file; git wanted ${sha:0:7})"
    done
}

# One deploy at a time, and never on top of a manual redeploy.
exec 9>"$REPO/.deploy.lock"
flock -n 9 || { echo "another deploy holds the lock; skipping"; exit 0; }

cd "$REPO"
git fetch -q origin master
OLD=$(git rev-parse HEAD)
NEW=$(git rev-parse origin/master)
[ "$OLD" = "$NEW" ] && exit 0            # nothing to do, and nothing logged

say "deploying ${OLD:0:7} -> ${NEW:0:7}"
git log --oneline "$OLD..$NEW" | sed 's/^/    /' | tee -a "$LOG"

settle_live_dbs origin/master
git reset -q --hard origin/master

# The reset cannot touch the databases (skip-worktree), but a new file arriving from git
# lands as root and the services do not run as root.
chown -R stockhunt:stockhunt \
    "$REPO/paper trading engine/results" "$REPO/paper trading engine/state" \
    "$REPO/paper trading engine/logs" "$REPO/paper api/state" "$REPO/paper api/logs" \
    "$REPO/Stockhunt Dashboard/web" "$REPO/data" "$REPO/logs" 2>/dev/null || true
chown stockhunt:stockhunt "$REPO/.env.local"; chmod 640 "$REPO/.env.local"

systemctl restart stockhunt-api
sleep 3
if systemctl is-active --quiet stockhunt-api; then
    say "stockhunt-api restarted on ${NEW:0:7}"
else
    say "!! stockhunt-api FAILED to start on ${NEW:0:7} -- rolling back to ${OLD:0:7}"
    settle_live_dbs "$OLD"
    git reset -q --hard "$OLD"
    systemctl restart stockhunt-api
    say "rolled back; investigate with: journalctl -u stockhunt-api -n 50"
    exit 1
fi

# The Next board, rebuilt only when its sources moved.
#
# NON-FATAL BY DESIGN, exactly like the payload rebuild below it. `dashboard-next/out/` is
# gitignored, so `git reset --hard` never removes it: a failed build leaves the PREVIOUS
# export in place and `/next/` keeps serving it. That is the whole reason this may fail
# quietly -- a five-minute deploy loop that can be broken by a slow npm registry is a
# board taken down by somebody else's outage.
#
# Conditional because `npm ci` is the expensive part and most pushes here are results
# CSVs. Rebuilt when the app's own files moved, when `../Stockhunt Dashboard/web/app.css`
# moved (it is COPIED in by `prebuild`, so a stylesheet change reaches this app only
# through a rebuild), or when there is no export on disk yet.
if command -v npm >/dev/null 2>&1; then
    if git diff --name-only "$OLD" "$NEW" -- dashboard-next/ "Stockhunt Dashboard/web/app.css" | grep -q .        || [ ! -d "$REPO/dashboard-next/out" ]; then
        say "building dashboard-next"
        if (cd "$REPO/dashboard-next" && npm ci --no-audit --no-fund && npm run build)                 >>"$LOG" 2>&1; then
            chown -R stockhunt:stockhunt "$REPO/dashboard-next/out" 2>/dev/null || true
            say "dashboard-next built"
        else
            say "!! dashboard-next build FAILED (/next/ still serving the previous export)"
        fi
    fi
else
    say "!! no npm on this box -- /next/ cannot be built. Install Node 20+ and re-deploy."
fi

# Results may have moved with the code, so the board payload is rebuilt from what just
# arrived. Failure here is not a failed deploy -- the site still serves the previous
# payload -- so it is reported and not fatal.
# Ownership again, immediately before the rebuild that needs it. The chown after the reset
# ought to be enough and on 2026-08-27 it was not: `web/index.html` came out of that deploy
# owned by root, `stamp_cache_busters` runs as `stockhunt`, and the whole payload rebuild
# died on `PermissionError` — leaving `data.js` rewritten but its cache-buster still
# pointing at the previous hash, so no browser would have fetched it.
#
# The cause is not established. `index.html` is TRACKED and `stamp_cache_busters` rewrites
# it on every refresh, so it is permanently dirty and every reset rewrites it as root —
# but the chown above covers that directory and works when run by hand. Rather than leave a
# theory in place of a fix, the guard is repeated where it is actually needed. It is
# idempotent and costs milliseconds.
chown -R stockhunt:stockhunt "$REPO/Stockhunt Dashboard/web" 2>/dev/null || true

if sudo -u stockhunt "$REPO/refresh-board.sh" >>"$LOG" 2>&1; then
    say "board payload rebuilt"
else
    say "!! board rebuild failed (site still serving the previous payload)"
fi

# The systemd units run `$REPO/autodeploy.sh`, which is a COPY of `deploy/autodeploy.sh`
# taken at install time. The reset above updated the repo's copy and NOT the one currently
# executing, so a fix to this script lands in git and changes nothing until somebody copies
# it across — the same shape of silent staleness as the failure above.
#
# Reported, not self-applied: bash reads a script incrementally as it runs, so rewriting the
# file that is mid-execution is its own class of bug. The next tick picks up the new copy.
for s in autodeploy.sh redeploy.sh refresh-board.sh; do
    [ -e "$REPO/$s" ] && [ -e "$REPO/deploy/$s" ] || continue
    cmp -s "$REPO/deploy/$s" "$REPO/$s" || \
        say "!! $s is stale against deploy/$s -- cp '$REPO/deploy/$s' '$REPO/$s'"
done

# Does the running desk now differ from master?
if git diff --name-only "$OLD" "$NEW" -- "${DESK_PATHS[@]}" | grep -q .; then
    {
        echo "The desk is running code older than master."
        echo "  master : ${NEW:0:7}"
        echo "  changed:"
        git diff --name-only "$OLD" "$NEW" -- "${DESK_PATHS[@]}" | sed 's/^/    /'
        echo "  restart when you are ready:  systemctl restart stockhunt-desk"
        echo "  (books go flat and re-warm 1,500 bars; ~20s to the feed, longer to first signal)"
    } > "$PENDING"
    say "DESK RESTART PENDING -- see $PENDING"
else
    rm -f "$PENDING"
    say "no desk-relevant changes; desk left running"
fi
