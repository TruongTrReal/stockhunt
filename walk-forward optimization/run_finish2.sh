#!/usr/bin/env bash
# The last pass: repair what the fill left behind, rebuild every widened book, ingest.
#
# WHY A SECOND FILL IS NEEDED, and it is two different reasons.
#
# 1. `run_fill_verdict_gaps.sh` LOSES A FAILED CHUNK. It scores 40 rules, and if that run
#    dies it logs FAILED and moves to the next 40 -- so `crypto 5m chunk_001` cost 40 rules
#    at 01:00 on 2026-08-30. The cause was not the cell being too big: the parallel pool
#    died on memory, riskmatch_wf fell back to ONE CORE, and the 1800s chunk timeout then
#    killed the serial pass before it could finish. Three workers was still too wide for a
#    5m panel with a book running beside it.
#
#    So this pass BISECTS instead of giving up, exactly as the cloud runner does: a failed
#    range is split and each half retried, down to a single rule, which is the only way to
#    tell "this chunk is too big" from "one rule in it is". Two workers and an hour per
#    slice, because that failure was a memory failure wearing a timeout's clothes.
#
# 2. `run_pairs_intraday.sh` ADDS RULES. Every pair it scores is new to
#    `leaderboard_universe` and therefore has no verdict, and `board_rank` DROPS a rule with
#    no verdict row. Scoring pairs and stopping would leave them in a results CSV and
#    nowhere the board can see them.
#
# STALE SLICE DIRS ARE DELETED FIRST, and that is not tidiness. The chunk tag is a counter
# over whatever is MISSING at the time, and the loop skips a tag whose file exists. On a
# second pass the missing list is shorter and different, so `c001.csv` from the first pass
# would make the loop skip a completely different set of rules -- silently, looking like a
# resume. Nothing is lost: the banked work was stitched into the cell, and the cell is
# copied back in as `000_existing.csv`.
#
#   nohup ./run_finish.sh > logs/finish.log 2>&1 &
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs results/edge_cells
export STOCKHUNT_WORKERS=2
CHUNK="${CHUNK:-25}"
SLICE_TIMEOUT="${SLICE_TIMEOUT:-3600}"

CLASSES="us_stocks us_etfs crypto commodities cme_futures"
TFS="1d 4h 1h 15m 5m"

# WAIT ON THE PYTHON STAGES ONLY, and filter by process NAME before touching CommandLine.
#
# The first version asked `Get-CimInstance Win32_Process | Where-Object` with no name
# filter and matched `*run_finish.sh*` among its patterns. That enumerates EVERY process
# including the powershell.exe running the query, whose own command line contains every
# pattern it is searching for -- so the check reported "busy" forever and the script could
# never fire. Not a race, not a slow stage: a predicate that is always true.
# `run_fill_verdict_gaps.sh` got this right by accident, with `-Filter "Name='python.exe'"`
# ahead of the match, which excludes the querying shell by construction.
#
# The count is read with `grep -oE '[0-9]+'` rather than `tr -d`, because powershell.exe
# returns a carriage return and two earlier attempts to strip it got the escaping wrong in
# opposite directions -- once leaving a literal CR, so `grep -v '^0$'` never matched "0"
# and the predicate was always true AGAIN. Pulling the digits out cannot be got wrong.
#
# The python stages are the whole answer: when the pairs run ends there is no `combo_wf.py`
# left. Two consecutive clear reads guard the gap between two stage launches.
busy() {
  n=$(powershell.exe -NoProfile -Command "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*riskmatch_wf.py*' -or \$_.CommandLine -like '*portfolio_wf.py*' -or \$_.CommandLine -like '*combo_wf.py*' }).Count" 2>/dev/null | grep -oE '[0-9]+' | head -1)
  [ "${n:-1}" != "0" ]
}
echo "=== waiting for the fill and the pairs $(date -u +%H:%M:%S)"
clear_runs=0
while [ "$clear_runs" -lt 2 ]; do
  if busy; then clear_runs=0; else clear_runs=$((clear_runs + 1)); fi
  sleep 45
done
echo "=== clear $(date -u +%H:%M:%S)"

score() {
  cls="$1"; tf="$2"; names="$3"; tag="$4"; slices="$5"; pop="$6"
  [ -z "${names// /}" ] && return 0
  timeout "$SLICE_TIMEOUT" "$PY" -u riskmatch_wf.py --class "$cls" --tf "$tf" --rules $names --n-trials "$pop" > "logs/fin_${cls}_${tf}_${tag}.log" 2>&1
  rc=$?
  if [ $rc -eq 0 ] && [ -f results/edge_standard.partial.csv ]; then
    cp results/edge_standard.partial.csv "$slices/${tag}.csv"
    return 0
  fi
  w=$(echo $names | wc -w)
  if [ "$w" -le 1 ]; then
    echo "      RULE WILL NOT SCORE HERE: $names"
    echo "$cls,$tf,$names" >> results/unscorable_rules.csv
    return 0
  fi
  half=$(( (w + 1) / 2 ))
  f1=$(echo $names | cut -d" " -f1-$half)
  f2=$(echo $names | cut -d" " -f$((half + 1))-)
  echo "      $tag: $w failed -- splitting into $half + $((w - half))"
  score "$cls" "$tf" "$f1" "${tag}a" "$slices" "$pop"
  score "$cls" "$tf" "$f2" "${tag}b" "$slices" "$pop"
}

fill_cell() {
  cls="$1"; tf="$2"
  cell="results/edge_cells/edge_${cls}_${tf}.csv"
  miss="logs/fin_gap_${cls}_${tf}.txt"
  "$PY" gap_rules.py "$cls" "$tf" > "$miss" 2>/dev/null || return 0
  n=$(grep -cve '^$' "$miss" 2>/dev/null || echo 0)
  pop=$("$PY" gap_rules.py "$cls" "$tf" --pop 2>/dev/null)
  if [ "${n:-0}" -lt 1 ]; then echo "=== $cls $tf complete ($pop)"; return 0; fi
  echo "=== $cls $tf: $n missing of $pop  $(date -u +%H:%M:%S)"
  slices="results/slices_fin_${cls}_${tf}"
  rm -rf "$slices"; mkdir -p "$slices"
  [ -f "$cell" ] && cp -f "$cell" "$slices/000_existing.csv"
  i=0; k=0; t0=$SECONDS
  while [ "$i" -lt "$n" ]; do
    k=$((k + 1))
    names=$(sed -n "$((i + 1)),$((i + CHUNK))p" "$miss" | tr '\n' ' ')
    [ -z "${names// /}" ] && break
    score "$cls" "$tf" "$names" "$(printf 'c%03d' "$k")" "$slices" "$pop"
    echo "    chunk $k done ($((SECONDS - t0))s)"
    i=$((i + CHUNK))
  done
  if "$PY" stitch_slices.py "$slices" "$cell.tmp" 2>&1 | sed 's/^/    /'; then
    mv "$cell.tmp" "$cell"
    echo "=== $cls $tf -> $(($(wc -l < "$cell") - 1)) rows  $((SECONDS - t0))s"
  else
    echo "=== $cls $tf STITCH FAILED -- slices kept"
  fi
}

echo "=== PASS 1: verdicts"
for tf in $TFS; do for cls in $CLASSES; do fill_cell "$cls" "$tf"; done; done

echo "=== merging and regenerating book rules"
"$PY" -u merge_edge_standard.py > logs/fin_merge.log 2>&1; echo "  merge=$?"
"$PY" -u make_book_rules.py     > logs/fin_rules.log 2>&1; echo "  rules=$?"

book_cell() {
  cls="$1"; tf="$2"; fill="$3"
  suf=""; [ "$fill" = "open" ] && suf="_open"
  rules="book_rules/${cls}_${tf}.txt"
  out="results/book_${cls}_${tf}${suf}.csv"
  [ -f "$rules" ] || return 0
  want=$(wc -l < "$rules" | tr -d ' ')
  have=0; [ -f "$out" ] && have=$(($(wc -l < "$out") - 1))
  if [ "$have" -ge "$want" ]; then echo "=== $cls $tf $fill up to date ($have)"; return 0; fi
  start=$(awk -F, -v c="$cls" -v t="$tf" '$1==c && $2==t {gsub(/\r/,"",$5); print $5; exit}' book_rules/starts.csv)
  if [ -z "$start" ]; then echo "=== $cls $tf $fill no start row"; return 0; fi
  curves=""; [ "$fill" = "close" ] && curves="--curves"
  t0=$SECONDS
  echo "=== book $cls $tf $fill start $(date -u +%H:%M:%S) ($have -> $want)"
  "$PY" -u portfolio_wf.py --class "$cls" --tf "$tf" --pit --start "$start" --cash-rate 0 --rules-file "$rules" --fill "$fill" --out "book_${cls}_${tf}${suf}.csv" $curves > "logs/book_${cls}_${tf}${suf}.log" 2>&1
  echo "=== book $cls $tf $fill exit=$? $((SECONDS - t0))s -> $(($(wc -l < "$out") - 1)) rows"
}

echo "=== PASS 2: books, 2 wide"
for fill in close open; do
  n=0
  for tf in $TFS; do
    if [ "$tf" = "5m" ] && [ "$fill" = "close" ]; then continue; fi
    for cls in $CLASSES; do
      book_cell "$cls" "$tf" "$fill" &
      n=$((n + 1))
      if [ "$n" -ge 2 ]; then wait -n; n=$((n - 1)); fi
    done
  done
  wait
done

echo "=== ingesting"
(cd .. && "./.venv/Scripts/python" -u tools/ingest_results.py) > logs/fin_ingest.log 2>&1
echo "  ingest=$?"
(cd .. && "./.venv/Scripts/python" -u tools/check_complete.py) 2>&1 | tail -20
echo "=== FINISH DONE $(date -u +%H:%M:%S) ==="
