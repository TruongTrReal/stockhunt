#!/usr/bin/env bash
# Top up every verdict cell that covers only part of its rule population.
#
# WHY THERE IS A GAP AT ALL, and it is not one cause. `cme_futures 4h` was scored before the
# conversions and the megacellar batch were promoted, so its cell holds 171 of 420.
# `commodities 5m` and `crypto 5m` lost chunks to the memory ceiling. The 1d and 4h sheets
# are short by 14-20 apiece, which is the irreducible tail: rules that build no position on
# that class (the volume family on crypto) and never produce a row.
#
# The consequence is identical whatever the cause, and it is silent: `board_rank` DROPS a
# rule with no verdict row, so a cell ranks whatever fraction happens to have one and the
# page looks finished. `cme_futures 4h` showed 25 candidates against `cme_futures 1h`'s 204
# for exactly this reason, and nothing on the board said so.
#
# TOP UP, DO NOT RE-SCORE. `gap_rules.py` prints the population minus what the cell already
# holds; only those are scored, and `stitch_slices.py` unions the new slices with the
# existing cell copied in as slice `000_existing.csv`. It de-duplicates on
# (class, tf, side, rule) keeping the LAST, and the sort is by filename, so a re-scored rule
# wins over the banked copy while everything untouched is carried through byte-identical.
# Re-scoring `cme_futures 4h` whole would cost four times as much for the same sheet.
#
# `--n-trials` IS MANDATORY AND IS THE POPULATION, not the chunk. Left off, `_n_trials` falls
# back to the rules in hand, so a 40-rule chunk would deflate against a search of 40 and
# every t bar on those rows would be understated. `gap_rules.py --pop` is where the honest
# count comes from -- the same list `riskmatch_wf` itself would have scored.
#
# CHUNK 40, THREE WORKERS. Memory here scales with RULE COUNT, not bar count: a full
# commodities 5m sheet reached 11.4 GB in one worker while a 20-rule chunk peaked at 0.5 GB
# on the same bars. This box has 32 GB and shares it.
#
# ORDER IS CHEAPEST-FIRST so the quick cells bank before the expensive ones start, and every
# chunk is banked the moment it finishes -- a kill costs one chunk, never a cell.
#
#   nohup ./run_fill_verdict_gaps.sh > logs/fill_gaps.log 2>&1 &
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs results/edge_cells
export STOCKHUNT_WORKERS=3
CHUNK="${CHUNK:-40}"
SLICE_TIMEOUT="${SLICE_TIMEOUT:-1800}"

# Cheapest first. The 5m cells go last; `cme_futures` 15m/5m and `us_etfs` 5m are absent
# because the cloud boxes own them.
CELLS="us_stocks:1d us_stocks:4h us_etfs:1d us_etfs:4h crypto:1d crypto:4h commodities:1d commodities:4h cme_futures:1d cme_futures:1h us_stocks:1h us_etfs:1h crypto:1h commodities:1h cme_futures:4h us_stocks:15m us_etfs:15m crypto:15m commodities:15m us_stocks:5m crypto:5m commodities:5m"

echo "=== waiting for the book rebuild $(date -u +%H:%M:%S)"
while powershell.exe -NoProfile -Command \
    "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*portfolio_wf.py*' }).Count" \
    2>/dev/null | tr -d '\r' | grep -qv '^0$'; do
  sleep 60
done
echo "=== clear $(date -u +%H:%M:%S)"

fill_cell() {
  local cls="$1" tf="$2"
  local cell="results/edge_cells/edge_${cls}_${tf}.csv"
  local miss="logs/gap_${cls}_${tf}.txt"
  "$PY" gap_rules.py "$cls" "$tf" > "$miss" 2>/dev/null
  local n pop
  n=$(grep -cve '^$' "$miss" 2>/dev/null || echo 0)
  pop=$("$PY" gap_rules.py "$cls" "$tf" --pop 2>/dev/null)
  if [ "${n:-0}" -lt 1 ]; then echo "=== $cls $tf complete ($pop rules)"; return 0; fi
  echo "=== $cls $tf: $n missing of $pop  $(date -u +%H:%M:%S)"

  local slices="results/slices_fill_${cls}_${tf}"
  mkdir -p "$slices"
  [ -f "$cell" ] && cp -f "$cell" "$slices/000_existing.csv"

  local i=0 k=0 t0=$SECONDS
  while [ "$i" -lt "$n" ]; do
    k=$((k + 1))
    local tag; tag=$(printf "chunk_%03d" "$k")
    if [ -f "$slices/$tag.csv" ]; then i=$((i + CHUNK)); continue; fi
    local names; names=$(sed -n "$((i + 1)),$((i + CHUNK))p" "$miss" | tr '\n' ' ')
    [ -z "${names// /}" ] && break
    timeout "$SLICE_TIMEOUT" "$PY" -u riskmatch_wf.py --class "$cls" --tf "$tf" \
        --rules $names --n-trials "$pop" > "logs/fill_${cls}_${tf}_${tag}.log" 2>&1
    if [ $? -eq 0 ] && [ -f results/edge_standard.partial.csv ]; then
      cp results/edge_standard.partial.csv "$slices/$tag.csv"
      echo "    $tag ok ($((SECONDS - t0))s)"
    else
      echo "    $tag FAILED -- see logs/fill_${cls}_${tf}_${tag}.log"
    fi
    i=$((i + CHUNK))
  done

  if "$PY" stitch_slices.py "$slices" "$cell.tmp" 2>&1 | sed 's/^/    /'; then
    mv "$cell.tmp" "$cell"
    echo "=== $cls $tf DONE $((SECONDS - t0))s -> $(($(wc -l < "$cell") - 1)) rows"
  else
    echo "=== $cls $tf STITCH FAILED -- slices kept in $slices"
  fi
}

for cell in $CELLS; do fill_cell "${cell%%:*}" "${cell##*:}"; done

echo "=== merging cells into the verdict of record"
"$PY" -u merge_edge_standard.py > logs/fill_merge.log 2>&1; echo "=== merge exit=$?"
"$PY" -u make_book_rules.py > logs/fill_bookrules.log 2>&1; echo "=== make_book_rules exit=$?"
echo "=== FILL DONE $(date -u +%H:%M:%S) -- books must be rebuilt for the widened cells ==="
