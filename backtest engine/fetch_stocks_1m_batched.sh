#!/usr/bin/env bash
# Fetch us_stocks 1m in SYMBOL BATCHES, because one call for the whole universe dies.
#
# `td_loader` accumulates every window for every symbol and writes the parquet files only
# after the last request returns. On 196 names at one-minute resolution that is ~117M rows
# live in pandas at once: the 2026-08-22 run reached 8,616 of 8,624 requests -- 99.9%,
# 8h34m -- and was killed with no traceback and no files. Eight and a half hours bought
# nothing.
#
# Batching bounds the peak (~24 names) and makes progress durable: each batch writes its
# own parquet files, so a death costs one batch and a re-run skips what already landed.
# It is not slower -- the same requests, in the same order, at the same rate.
#
# THE CARRIAGE RETURN IS LOAD-BEARING. Python on Windows prints \r\n, so without the
# `tr -d` below the shell array holds "A\r" and every request asks the vendor for a ticker
# that does not exist. It fails SILENTLY: an empty response is not an error to td_loader,
# it is a symbol with no frames, so the run prints "no data" per symbol and exits clean.
# That reads exactly like a vendor gap rather than a quoting bug, which is how it cost an
# hour to find. `$CR` is built with printf rather than written literally so this file
# cannot be broken again by an editor that normalises line endings.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
BATCH=${BATCH:-24}
CR=$(printf '\r')

# `grep -v '^$'` is the second half of the same lesson as the CR strip. When nothing is
# missing, `'\n'.join([])` prints ONE EMPTY LINE, so `mapfile` builds a one-element array
# holding "" -- the `n -eq 0` guard never fires, and the loop asks the vendor for the
# symbol `""` forever (404, symbol=&interval=1min...). Emptiness has to reach the shell
# as zero lines, not as one blank one.
missing_symbols() {
  "$PY" - <<'EOF' | tr -d "$CR" | grep -v '^$'
import os, config
have = {os.path.splitext(f)[0] for f in os.listdir('../data/stocks/1m')}
print('\n'.join(s for s in config.US_STOCKS if config.safe_symbol(s) not in have))
EOF
}

while :; do
  # Recomputed every pass rather than listed once, so this is resumable: interrupt it,
  # run it again, and it picks up whatever is still missing.
  mapfile -t MISSING < <(missing_symbols)
  n=${#MISSING[@]}
  [ "$n" -eq 0 ] && { echo "=== all symbols cached $(date -u +%H:%M:%S) ==="; break; }
  group=("${MISSING[@]:0:$BATCH}")
  echo "=== $(date -u +%H:%M:%S)  ${#group[@]} of $n remaining: ${group[*]} ==="
  if ! "$PY" -u td_loader.py --class us_stocks --tf 1m --symbols "${group[@]}"; then
    echo "!!! batch failed; retrying the remainder next pass"
  fi
  mapfile -t STILL < <(missing_symbols)
  # A batch that writes nothing would otherwise loop forever on the same names.
  if [ "${#STILL[@]}" -eq "$n" ]; then
    echo "!!! no progress on this pass -- stopping rather than spinning"
    exit 1
  fi
done
