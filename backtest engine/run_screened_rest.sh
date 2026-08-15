#!/usr/bin/env bash
# Second half of the 2026-08-12 universe change: every remaining stage whose us_etfs or
# crypto sheet was measured on the old 65/34 baskets. `run_screened.sh` did the sweep, the
# walk-forward leaderboard and the curves; this closes the rest.
#
# Bash, `python -u`, per-stage logs. Same reasoning as `run_screened.sh` — see its header.
#
# ORDER. `riskmatch_wf` runs LAST and UNSCOPED, and both of those are deliberate:
# `results/edge_standard.csv` is the single place in this project where a pass or fail
# exists, and a class-scoped run writes `edge_standard.partial.csv` instead, leaving the
# stale rows standing as the verdict of record. It is also the long pole, because it runs
# us_stocks' point-in-time top 100 as well — nothing else here needs to wait on it, so it
# goes at the back.
set -u
cd "$(dirname "$0")"
ENGINE="$(pwd)"
WFO="$ENGINE/../walk-forward optimization"
PY="$ENGINE/../.venv/Scripts/python.exe"
mkdir -p "$ENGINE/logs" "$WFO/logs"
export STOCKHUNT_WORKERS=6

run_stage () {
  local name="$1"; local dir="$2"; shift 2
  local t0=$SECONDS
  echo "=== $name start $(date +%H:%M:%S)"
  ( cd "$dir" && "$PY" -u "$@" ) > "$ENGINE/logs/rest_$name.log" 2>&1
  local code=$?
  echo "=== $name exit=$code $((SECONDS - t0))s"
  return $code
}

# Secondary studies. A failure in one must not sink the ones behind it, so every line
# ends in `|| true` and the exit codes are read off this driver's log rather than trusted.
run_stage combo_sweep "$ENGINE" combo_sweep.py --class us_etfs crypto                || true
run_stage variants    "$WFO"    variants.py    --class us_etfs crypto --tf 1d 4h     || true
run_stage prereg      "$WFO"    prereg.py      --class us_etfs crypto --tf 1d 4h --freeze || true
run_stage strat_wf    "$WFO"    strat_wf.py    --class us_etfs crypto --tf 1d 4h     || true
run_stage combo_wf    "$WFO"    combo_wf.py    --class us_etfs crypto --tf 1d 4h     || true

# THE VERDICT. Every class, or it writes a partial sheet and changes nothing.
run_stage riskmatch   "$WFO"    riskmatch_wf.py --tf 1d 4h
echo "=== riskmatch exit above is the one that matters"

# The artifacts anyone actually reads.
run_stage payload   "$ENGINE" build_payload.py || true
run_stage report    "$ENGINE" build_report.py  || true

echo "ALL STAGES DONE $(date +%H:%M:%S)"
