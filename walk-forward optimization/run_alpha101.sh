#!/usr/bin/env bash
# The 101-alpha replication, end to end. Launched detached with output to logs/ --
# every stage here emits numpy RuntimeWarnings on a normal run and PowerShell turns
# those into terminating errors, so bash is not a style preference.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
CLASS=${CLASS:-us_stocks}
TF=${TF:-1d}

echo "=== books ==="
$PY -u -W ignore alpha101.py --positions --class "$CLASS" --tf "$TF" || exit 1

NAMES=$($PY -W ignore alpha101.py --names)
echo "=== gauntlet: $(echo "$NAMES" | wc -w) alphas, fill=close ==="
$PY -u -W ignore riskmatch_wf.py --class "$CLASS" --tf "$TF" --side long \
    --fill close --rules $NAMES || exit 1
echo "=== done ==="
