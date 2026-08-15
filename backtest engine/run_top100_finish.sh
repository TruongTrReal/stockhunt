#!/usr/bin/env bash
# The stages that come AFTER the walk-forward chain, for the 2026-08-12 top-100 universe.
#
# Bash, not PowerShell, for the reason recorded at the top of
# `../walk-forward optimization/run_top100.sh`: PS 5.1 turns a native program's stderr
# into terminating NativeCommandErrors, and every one of these stages emits numpy
# RuntimeWarnings on a normal run.
set -u
cd "$(dirname "$0")"
E="$(pwd)"
ROOT="$(dirname "$E")"
PY="$ROOT/.venv/Scripts/python.exe"
mkdir -p logs
export STOCKHUNT_WORKERS=6

run_stage () {
  local name="$1"; local dir="$2"; shift 2
  local t0=$SECONDS
  echo "=== $name start $(date +%H:%M:%S)"
  ( cd "$dir" && "$PY" -u "$@" ) > "$E/logs/top100_$name.log" 2>&1
  local code=$?
  echo "=== $name exit=$code $((SECONDS - t0))s"
  return $code
}

# Stage 2 (legacy single-split pairs) — its us_stocks sheets were archived with the rest.
run_stage combo_sweep "$E" combo_sweep.py --class us_stocks --tf 1d 4h || true

# Stage 1h: score the BOOK. --pit is where the ~100-name point-in-time mask actually binds;
# --catalog measures the trial dispersion properly rather than deflating a hand-picked
# winner against the handful of rules it was hand-picked into.
run_stage portfolio "$ROOT/walk-forward optimization" portfolio_wf.py \
  --class us_stocks --tf 1d 4h --pit --catalog --out portfolio_top100.csv || true

run_stage payload   "$E" build_payload.py
run_stage report    "$E" build_report.py
run_stage dashboard "$ROOT/Stockhunt Dashboard" build_dashboard.py --serve --dist

echo "FINISH STAGES DONE"
