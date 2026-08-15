#!/usr/bin/env bash
# Re-run the stages that price SELECTION on the point-in-time top-100 universe.
# Written for the 2026-08-12 universe change.
#
# THIS IS A BASH SCRIPT AND THAT IS DELIBERATE. The PowerShell version of this runner
# silently killed the verdict stage two minutes in and then blocked for 83 minutes looking
# like it was working. Two Windows PowerShell 5.1 behaviours combined:
#
#   1. Redirecting a native program's stderr inside PS 5.1 wraps every line in an
#      ErrorRecord (NativeCommandError). A numpy `RuntimeWarning: invalid value encountered
#      in scalar divide` from riskmatch_wf.py:805 -- harmless, and it fires on every run --
#      therefore became a terminating error under `$ErrorActionPreference = "Stop"`.
#   2. The python main died but its multiprocessing workers were orphaned holding the
#      redirected stdout handle, so the driver never returned and never logged an exit.
#
# On top of that `*>` buffers until process exit, so the log stayed at 0 bytes throughout
# and there was nothing to read. Hence: bash, `python -u`, and `tee` for live output.
#
# ORDER IS DELIBERATE. `riskmatch_wf` runs FIRST because `results/edge_standard.csv` is the
# single place a pass or fail exists in this project -- it IS the reranking. It also runs
# EVERY class, not just us_stocks: a scoped run writes `edge_standard.partial.csv` and
# leaves the stale us_stocks rows standing as the verdict of record.
set -u
cd "$(dirname "$0")"
W="$(pwd)"
PY="$W/../.venv/Scripts/python.exe"
mkdir -p logs

# Six, not the default ten. Ten workers held ~2.5 GB each and drove free memory to 3 GB of
# 32 GB on this box. These stages call `signals.position_for` directly and get no help from
# the position cache, so the working set is real rather than cacheable.
export STOCKHUNT_WORKERS=6

run_stage () {
  local name="$1"; shift
  local t0=$SECONDS
  echo "=== $name start $(date +%H:%M:%S)"
  "$PY" -u "$@" > "logs/top100_$name.log" 2>&1
  local code=$?
  echo "=== $name exit=$code $((SECONDS - t0))s"
  return $code
}

run_stage riskmatch riskmatch_wf.py --tf 1d 4h
if [ $? -ne 0 ]; then echo "VERDICT STAGE FAILED -- stopping"; exit 1; fi

# A secondary study that fails must not sink the ones behind it.
run_stage curves   curves.py   --class us_stocks --tf 1d 4h || true
run_stage combo_wf combo_wf.py --class us_stocks --tf 1d 4h || true
run_stage strat_wf strat_wf.py --class us_stocks --tf 1d 4h || true
run_stage prereg   prereg.py   --class us_stocks --tf 1d 4h --freeze || true
run_stage variants variants.py --class us_stocks --tf 1d 4h || true

echo "ALL STAGES DONE"
