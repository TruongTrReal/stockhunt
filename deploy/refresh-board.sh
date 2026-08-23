#!/bin/sh
# Rebuild the parts of the board that are NOT written by the desk.
#
# live.json is the real-time half and the desk publishes it itself, every tick. These two
# are the slow half: data.js is the research payload (changes only when a sweep is re-run
# and its results are pulled), and paper_curves.json is a backtest of each registered
# system's recent history (changes when the registrations change or the window rolls).
#
# The quotes around the cd are load-bearing -- "Stockhunt Dashboard" has a space in it,
# which is the same rule that shapes every sys.path bootstrap in this repo and that has
# now bitten the unit files twice.
set -e

# INGEST FIRST, and this is the step whose absence is invisible.
#
# The research leaderboard is a QUERY over `results.db` now, not a payload baked into
# `data.js` -- `board_rank.build_sheet` reads the store and both servers call it. But the
# store is gitignored, because it is built from the tracked result CSVs and a committed
# copy would be a second answer to a question those CSVs already answer. So on a fresh
# clone it does not exist, and nothing else here creates it.
#
# What that looks like when it is missing: `/v1/research/board` answers 503, `app.js`
# treats any non-200 as "no live board" and keeps the baked payload, and the page renders
# perfectly with whatever numbers the last build froze. No error, no blank page, nothing
# on screen saying the board stopped being live. Exactly the class of silent staleness the
# `?v=` cache-buster and the new-build watcher exist to prevent one level up.
#
# It is also what carries a SUBMITTED rule's sheet rows across: `research_worker.py`
# inserts into the store directly, and this keeps the store in step with any stage that
# has been re-run since.
/opt/stockhunt/.venv/bin/python -u /opt/stockhunt/tools/ingest_results.py

cd "/opt/stockhunt/Stockhunt Dashboard"
/opt/stockhunt/.venv/bin/python -u build_dashboard.py --serve
/opt/stockhunt/.venv/bin/python -u paper_curves.py
