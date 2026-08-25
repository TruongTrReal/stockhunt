#!/usr/bin/env bash
# The whole research grid: every strategy, every asset class, every timeframe on the axis.
#
# Written 2026-08-23, when the axis became 1d/4h/1h/15m and the intraday caches were
# filled for all five classes. Four stages, in this order, and the order is not a
# preference:
#
#   1. strat_wf   -- the published catalogue. It picks up `strategies/published/` by
#                    DISCOVERY, so the thirteen converted third-party strategies join the
#                    ordinary leaderboard here simply by being files: the sheet on disk
#                    before this run held 155 rules out of 175 published, and every one of
#                    the converted ones was missing because it post-dates that run. They
#                    are ranked as normal strategies from now on -- same folds, same
#                    benchmark, same trial family, same six criteria.
#   2. riskmatch  -- THE VERDICT, and the one stage that cannot be run piecemeal.
#                    `edge_standard.csv` is written WHOLE with no merge, so any (class,
#                    timeframe) left out of this command is silently deleted from the file
#                    rather than left alone. Every class and every timeframe, or nothing.
#   3. make_book_rules + run_book -- the account-level scores and the equity curves the
#                    dashboard draws. `--start` per sheet comes from make_book_rules, so
#                    the two are regenerated together or they drift.
#   4. ingest_results -- the board is a query over `results.db` now, so nothing above is
#                    visible until this runs.
#
# BASH, NOT POWERSHELL: PS 5.1 turns numpy's routine RuntimeWarning into a terminating
# error, `*>` buffers until exit, and orphaned workers hold the redirected handle so the
# launcher never returns. See run_top100.sh.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
REPO=..
CLASSES="us_stocks us_etfs crypto commodities cme_futures"
TFS="1d 4h 1h 15m"

stamp() { date -u +"%Y-%m-%d %H:%M:%S UTC"; }
echo "=== FULL GRID START $(stamp) ==="
echo "    classes: $CLASSES"
echo "    timeframes: $TFS"

echo ""
echo "=== [1/4] strat_wf: the published catalogue, converted strategies included  $(stamp) ==="
$PY -u strat_wf.py --class $CLASSES --tf $TFS || echo "!!! strat_wf exited $?"

echo ""
echo "=== [2/4] riskmatch_wf: the verdict, all cells in ONE write  $(stamp) ==="
$PY -u riskmatch_wf.py --class $CLASSES --tf $TFS || echo "!!! riskmatch exited $?"

echo ""
echo "=== [3/4] book rules + book scores  $(stamp) ==="
$PY -u make_book_rules.py || echo "!!! make_book_rules exited $?"
./run_book.sh || echo "!!! run_book exited $?"

echo ""
echo "=== [4/4] ingest into results.db  $(stamp) ==="
$PY -u "$REPO/tools/ingest_results.py" || echo "!!! ingest exited $?"

echo ""
echo "=== FULL GRID DONE $(stamp) ==="
