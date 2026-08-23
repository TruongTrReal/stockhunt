#!/usr/bin/env bash
# Futures intraday, hourly first then minute. SEQUENTIAL on purpose: each run uses four
# workers and this vendor answers eight concurrent requests by closing the connection
# rather than by slowing down, so two runs at once is a failure mode, not a speedup.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
echo "=== 1h  $(date -u +%H:%M:%S) ==="
$PY -u db_intraday.py --tf 1h
echo "=== 1m  $(date -u +%H:%M:%S) ==="
$PY -u db_intraday.py --tf 1m
echo "=== done $(date -u +%H:%M:%S) ==="
