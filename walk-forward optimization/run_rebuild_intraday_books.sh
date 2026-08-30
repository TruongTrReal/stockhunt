#!/usr/bin/env bash
# Rebuild the intraday book sheets that a cloud fetch overwrote with a stale copy.
#
# WHAT HAPPENED. `cloud/fleet.sh` used to fetch a finished box's results by tarring
# `wfo-results/` WHOLE. Each box was seeded from the 2026-08-25 commit, so its copy of every
# book sheet was that commit's -- 180 rules, the pre-promotion population. The 1h and 15m
# books had been rebuilt locally on 08-27 with the full ~412 rules; the fetch on 08-29 12:35
# restored the 08-25 copies over them, and every one of those sheets is now byte-identical
# to HEAD again. `fleet.sh` has since been narrowed to fetch `edge_cells` and `wf_summary_*`
# only, so this cannot recur -- but the 08-27 work is gone and has to be recomputed.
#
# WHY IT MATTERS RATHER THAN BEING COSMETIC. `board_rank` drops any rule with no book row,
# because the book is where the money columns live. So a sheet holding 180 of its 412 scored
# rules does not rank 412 rules slightly wrongly -- it silently ranks 180 and discards the
# rest. That is the whole reason `us_stocks 1h` shows 49 candidates against 493 at 1d.
#
# `cme_futures 4h` is here for a different reason: it was never scored until 08-28, so its
# book is the 08-25 seed by age rather than by clobber. Same repair.
#
# `cme_futures 1h` is NOT here -- it has 409 rows against 411 labels, which is two rules that
# legitimately produced nothing, not a truncated sheet.
#
# CLOSE FILL OWNS THE CURVES. `book_curves_<cls>_<tf>.json` is named from (class, tf) alone,
# so only the close-fill pass may pass `--curves`; an open-fill run passing it would
# overwrite the charts the close-fill rows are drawn against. Same rule as
# `run_refresh_chain.sh`, and the reason the two fills run as separate passes.
#
# TWO WIDE, not three: `book_crypto_5m_open.csv` is still building in this same box's RAM.
#
#   nohup ./run_rebuild_intraday_books.sh > logs/rebuild_books.log 2>&1 &
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs

CELLS="us_stocks:1h us_stocks:15m us_etfs:1h us_etfs:15m crypto:1h crypto:15m commodities:1h commodities:15m cme_futures:4h"

start_for() {   # the --start this sheet needs, out of the generated ledger
  awk -F, -v c="$1" -v t="$2" '$1==c && $2==t {gsub(/\r/,"",$5); print $5; exit}' book_rules/starts.csv
}

book_cell() {
  cls="$1"; tf="$2"; fill="$3"
  start="$(start_for "$cls" "$tf")"
  rules="book_rules/${cls}_${tf}.txt"
  [ -f "$rules" ] || { echo "=== $cls $tf $fill SKIP (no rule list)"; return 0; }
  [ -n "$start" ] || { echo "=== $cls $tf $fill SKIP (no start row)"; return 0; }
  suf=""; [ "$fill" = "open" ] && suf="_open"
  curves=""; [ "$fill" = "close" ] && curves="--curves"
  t0=$SECONDS
  echo "=== $cls $tf $fill start $(date -u +%H:%M:%S)  ($(wc -l < "$rules" | tr -d ' ') labels, from $start)"
  "$PY" -u portfolio_wf.py \
      --class "$cls" --tf "$tf" --pit --start "$start" --cash-rate 0 \
      --rules-file "$rules" --fill "$fill" --out "book_${cls}_${tf}${suf}.csv" $curves \
      > "logs/book_${cls}_${tf}${suf}.log" 2>&1
  rc=$?
  rows=0; [ -f "results/book_${cls}_${tf}${suf}.csv" ] && rows=$(($(wc -l < "results/book_${cls}_${tf}${suf}.csv") - 1))
  echo "=== $cls $tf $fill exit=$rc $((SECONDS - t0))s -> $rows rows"
}

echo "=== REBUILD INTRADAY BOOKS $(date -u +%H:%M:%S) ==="
for fill in close open; do
  echo "=== pass: $fill fill, 2 wide"
  n=0
  for cell in $CELLS; do
    book_cell "${cell%%:*}" "${cell##*:}" "$fill" &
    n=$((n + 1))
    if [ "$n" -ge 2 ]; then wait -n; n=$((n - 1)); fi
  done
  wait
  echo "=== $fill pass done $(date -u +%H:%M:%S)"
done

echo "=== ingesting into results.db"
(cd .. && "./.venv/Scripts/python" -u tools/ingest_results.py) > logs/rebuild_ingest.log 2>&1
echo "=== ingest exit=$?"
echo "=== REBUILD DONE $(date -u +%H:%M:%S) ==="
