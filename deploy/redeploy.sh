#!/bin/bash
# Pull master and restart the stack, WITHOUT destroying the live databases.
#
# `git reset --hard` is not safe on its own here. `paper trading engine/results/paper.db`
# and `state/desk.db` are TRACKED files -- the repo keeps them on purpose, they are the
# forward-test record -- so a reset rewrites them from the committed blob and, run as root,
# leaves them root-owned so the desk cannot write them either.
#
# It did worse than that once: the running desk's `-wal` sidecars are NOT tracked, so they
# survived the reset and were then replayed against the file git had just restored. Two
# different databases, one write-ahead log. `pragma integrity_check` came back with unused
# pages, and the desk exited 1 on every start.
#
# So: stop, set the live databases aside WITH their sidecars, pull, put them back.
set -euo pipefail

REPO=/opt/stockhunt
LIVE_DBS=("paper trading engine/results/paper.db" "paper trading engine/state/desk.db" "paper api/state/auth.db")
KEEP=$(mktemp -d)

echo "==> stopping services"
systemctl stop stockhunt-desk stockhunt-api || true

echo "==> setting live databases aside"
cd "$REPO"
for db in "${LIVE_DBS[@]}"; do
    [ -e "$db" ] || continue
    mkdir -p "$KEEP/$(dirname "$db")"
    # The sidecars must travel with their database. A .db restored without its -wal, or a
    # -wal left behind for a different .db, is the corruption described above.
    for f in "$db" "$db-wal" "$db-shm"; do
        [ -e "$f" ] && cp -a "$f" "$KEEP/$f"
    done
done

echo "==> pulling master"
git fetch -q origin
git reset -q --hard origin/master

echo "==> restoring live databases"
for db in "${LIVE_DBS[@]}"; do
    for f in "$db" "$db-wal" "$db-shm"; do
        [ -e "$KEEP/$f" ] && cp -a "$KEEP/$f" "$f"
    done
done

echo "==> fixing ownership"
chown -R stockhunt:stockhunt \
    "$REPO/paper trading engine/results" \
    "$REPO/paper trading engine/state" \
    "$REPO/paper trading engine/logs" \
    "$REPO/paper api/state" \
    "$REPO/paper api/logs" \
    "$REPO/Stockhunt Dashboard/web" \
    "$REPO/data" \
    "$REPO/.env.local"
chmod 640 "$REPO/.env.local"

echo "==> integrity check"
for db in "${LIVE_DBS[@]}"; do
    [ -e "$db" ] || continue
    printf '    %-46s ' "$(basename "$db")"
    sudo -u stockhunt "$REPO/.venv/bin/python" -c "
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute('pragma integrity_check').fetchone()[0])
" "$db"
done

echo "==> starting services"
systemctl start stockhunt-api stockhunt-desk
sleep 20
systemctl is-active stockhunt-api stockhunt-desk

echo "==> kept a copy at $KEEP (delete when satisfied)"
