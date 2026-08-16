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
cd "/opt/stockhunt/Stockhunt Dashboard"
/opt/stockhunt/.venv/bin/python -u build_dashboard.py --serve
/opt/stockhunt/.venv/bin/python -u paper_curves.py
