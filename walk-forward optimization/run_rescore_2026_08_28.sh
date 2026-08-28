#!/usr/bin/env bash
# Re-score the two cells whose INPUT changed on 2026-08-28:
#   cme_futures  -- gained NQ.v.0 / YM.v.0 / RTY.v.0 (16 -> 19 roots) at 1d/1h/15m/5m/1m.
#                   4h has no bars for the three, so that sheet is untouched.
#   commodities 4h -- rebuilt from the UTC-corrected 1h; the bars genuinely changed.
#
# BASH, detached, python -u, logs/ -- see run_top100.sh for why PowerShell kills this.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs

step () {  # step <label> <args...>
  local label="$1"; shift
  local t0=$SECONDS
  echo "=== $label start $(date -u +%H:%M:%S)"
  "$PY" -u "$@" > "logs/rescore_${label}.log" 2>&1
  local rc=$?
  echo "=== $label exit=$rc $((SECONDS - t0))s"
  return $rc
}

echo "### STAGE 1b walkforward $(date -u +%H:%M:%S)"
step wf_cme walkforward.py --class cme_futures --tf 1d 1h 15m
step wf_com walkforward.py --class commodities --tf 4h

echo "### STAGE 2b combo_wf $(date -u +%H:%M:%S)"
step cwf_cme combo_wf.py --class cme_futures --tf 1d
step cwf_com combo_wf.py --class commodities --tf 4h

echo "### STAGE 1e strat_wf $(date -u +%H:%M:%S)"
step strat_cme strat_wf.py --class cme_futures --tf 1d
step strat_com strat_wf.py --class commodities --tf 4h

echo "### RESCORE PRE-VERDICT STAGES DONE $(date -u +%H:%M:%S)"
