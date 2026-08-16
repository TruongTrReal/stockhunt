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
    git reset -q --hard "$OLD"
    systemctl restart stockhunt-api
    say "rolled back; investigate with: journalctl -u stockhunt-api -n 50"
    exit 1
fi

# Results may have moved with the code, so the board payload is rebuilt from what just
# arrived. Failure here is not a failed deploy -- the site still serves the previous
# payload -- so it is reported and not fatal.
if sudo -u stockhunt "$REPO/refresh-board.sh" >>"$LOG" 2>&1; then
    say "board payload rebuilt"
else
    say "!! board rebuild failed (site still serving the previous payload)"
fi

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
