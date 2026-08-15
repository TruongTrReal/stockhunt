#!/usr/bin/env bash
# Re-run us_etfs and crypto on the SCREENED baskets — ETF_TOP10 and CRYPTO_TOP20, chosen
# by `universe_screen.py` — over 2000-01-01 to now. Written for the 2026-08-12 universe
# change; the superseded sheets are under `results/_archive/etf65_crypto34/`.
#
# BASH, DELIBERATELY. See the header of `../walk-forward optimization/run_top100.sh` for
# the full account: PowerShell 5.1 turns the numpy RuntimeWarning these stages emit on
# every normal run into a terminating error, `*>` buffers the log to 0 bytes until exit,
# and the orphaned multiprocessing workers keep the driver from ever returning. The result
# is a job that is dead, silent and indistinguishable from a slow one.
#
# TWO FOLDERS, so the script cds between them: module basenames are only unique per
# folder and every stage imports its siblings by bare name.
set -u
cd "$(dirname "$0")"
ENGINE="$(pwd)"
WFO="$ENGINE/../walk-forward optimization"
PY="$ENGINE/../.venv/Scripts/python.exe"
mkdir -p "$ENGINE/logs" "$WFO/logs"

# Six rather than the default ten, for the same reason `run_top100.sh` uses six: the
# walk-forward stages hold ~2.5 GB per worker and ten of them take this box to 3 GB free
# of 32 GB. `sweep.py` would be happy with ten but is not worth a second export.
export STOCKHUNT_WORKERS=6

run_stage () {
  local name="$1"; local dir="$2"; shift 2
  local t0=$SECONDS
  echo "=== $name start $(date +%H:%M:%S)"
  ( cd "$dir" && "$PY" -u "$@" ) > "$ENGINE/logs/screened_$name.log" 2>&1
  local code=$?
  echo "=== $name exit=$code $((SECONDS - t0))s"
  return $code
}

# Stage 1 first: the walk-forward stages read no sweep output, but a sweep failure means
# the signal layer is broken on the new baskets and there is no point running the rest.
run_stage sweep "$ENGINE" sweep.py --class us_etfs crypto --tf 1d 4h
if [ $? -ne 0 ]; then echo "SWEEP FAILED -- stopping"; exit 1; fi

# Stage 1b: THE leaderboard. This is what "rerank" means.
run_stage wf_etfs   "$WFO" walkforward.py --class us_etfs --tf 1d 4h
if [ $? -ne 0 ]; then echo "WF us_etfs FAILED -- stopping"; exit 1; fi
run_stage wf_crypto "$WFO" walkforward.py --class crypto --tf 1d 4h
if [ $? -ne 0 ]; then echo "WF crypto FAILED -- stopping"; exit 1; fi

# Secondary. A failure here must not sink the leaderboard behind it.
#
# The dashboard's equity curves used to be a `curves.py` stage here. They are now written
# by the book run (`portfolio_wf.py --curves`), from the same series it scores, so the
# chart cannot drift from the leaderboard row. `run_stage` runs a PYTHON module, and this
# is a shell script that needs the per-sheet `--start` out of book_rules/starts.csv, so it
# is invoked directly rather than through it.
echo "=== book start $(date +%H:%M:%S)"
( cd "$WFO" && ./run_book.sh ) > "$ENGINE/logs/screened_book.log" 2>&1
echo "=== book exit=$? $(date +%H:%M:%S)"

echo "ALL STAGES DONE $(date +%H:%M:%S)"
