#!/usr/bin/env bash
# The book stage, on the workstation, after the cloud fleet has returned the verdicts.
#
# WHY THIS RUNS HERE AND NOT ON THE RENTED BOXES. `portfolio_wf` holds a whole cell in pandas
# and does not chunk the way `riskmatch_wf` does. On a 16 GB box, commodities 15m -- five
# symbols, the SMALLEST cell in the grid -- reached 15.9 GB resident with 5.5 GB swapped and
# sat at 87% CPU IDLE for 47 minutes, paging rather than computing. It is chunkable in
# principle, but `--n-trials` and `--trial-dispersion` have to be passed TOGETHER (supplying
# the count while letting the spread be estimated from a handful of rules LOWERS the bar
# instead of raising it, inverting the correction), and `--curves` writes one JSON per
# (class, tf) that would need merging across chunks. This box has 32 GB and these cells
# already complete on it.
#
# ONE LANE BY DEFAULT, and that is a courtesy rather than a limit. The 2026-08-27 collapse
# happened because three chains ran at once on a machine the user was working on. JOBS=2 or 3
# is fine on an idle box; the default assumes it is not idle.
#
#   ./run_books_local.sh --dry-run     # what it would run, and why each cell qualifies
#   ./run_books_local.sh               # one at a time
#   JOBS=3 ./run_books_local.sh        # only if the box is yours for the duration
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
JOBS="${JOBS:-1}"
DRY=""; [ "${1:-}" = "--dry-run" ] && DRY=1
mkdir -p logs

# WHICH CELLS, AND WHY EACH ONE. Anything whose verdict changed needs its book rebuilt,
# because make_book_rules reads the rule list and the --start out of edge_standard.csv.
#   1h, 15m   the sheets are from 08-24 and every bar file under them was rewritten 08-26/27
#   5m        never scored at all until this run -- no wf_summary, nothing in results.db
#   commodities 1d   the gold/silver entry cut moved two of five names from 26 years to 20.6
#   cme_futures 4h   never had a verdict before
wanted() {
  case "$1:$2" in
    *:1h|*:15m|*:5m|commodities:1d|cme_futures:4h) return 0 ;;
    *) return 1 ;;
  esac
}

book_cell() {
  cls="$1"; tf="$2"; start="$3"; fill="$4"
  suf=""; [ "$fill" = "open" ] && suf="_open"
  rules="book_rules/${cls}_${tf}.txt"
  [ -f "$rules" ] || { echo "=== $cls $tf $fill SKIP (no rule list)"; return 0; }
  # 5m IS OPEN FILL ONLY. At 78 bars a day a close-fill number measures the look-ahead rather
  # than the rule -- ibs on commodities reads 5.5%/yr at 1d and 1,970%/yr at 15m on close
  # fill. No close-fill 5m sheet exists in this repo and none should be created.
  [ "$tf" = "5m" ] && [ "$fill" = "close" ] && return 0
  # Close fill owns the curves: book_curves_<cls>_<tf>.json is named from (class, tf) alone,
  # so an open-fill run passing --curves would overwrite the charts the close-fill rows are
  # drawn against, and every detail page would disagree with itself.
  curves=""; [ "$fill" = "close" ] && curves="--curves"
  if [ -n "$DRY" ]; then
    echo "would run: $cls $tf --fill $fill --start $start ${curves:-(no curves)}"
    return 0
  fi
  t0=$SECONDS
  echo "=== $cls $tf $fill start $(date -u +%H:%M:%S)"
  "$PY" -u portfolio_wf.py --class "$cls" --tf "$tf" --pit --start "$start" --cash-rate 0 \
      --rules-file "$rules" --fill "$fill" --out "book_${cls}_${tf}${suf}.csv" $curves \
      > "logs/book_${cls}_${tf}${suf}.log" 2>&1
  echo "=== $cls $tf $fill exit=$? $((SECONDS - t0))s"
}

[ -f book_rules/starts.csv ] || { echo "no book_rules/starts.csv -- run make_book_rules.py first"; exit 1; }
echo "=== LOCAL BOOKS, $JOBS lane(s), $(date -u +%H:%M:%S) ==="
for fill in close open; do
  n=0
  while IFS=, read -r cls tf nr nf start; do
    [ "$cls" = "class" ] && continue
    wanted "$cls" "$tf" || continue
    book_cell "$cls" "$tf" "$(echo "$start" | tr -d '\r')" "$fill" &
    n=$((n + 1))
    if [ "$n" -ge "$JOBS" ]; then wait -n; n=$((n - 1)); fi
  done < book_rules/starts.csv
  wait
done
echo "=== LOCAL BOOKS DONE $(date -u +%H:%M:%S) ==="
