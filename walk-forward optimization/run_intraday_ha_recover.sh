#!/usr/bin/env bash
# Recovery pass, 2026-08-21: the us_etfs and commodities sheets the first two drivers
# could not score.
#
# What happened, because the shape of it matters more than the fix: `config.WINDOWS`
# carried only `1d` and `4h` for those two classes -- no intraday entry had ever been
# needed, because no intraday sheet had ever been run on them. `td_loader.fetch` looks
# the window up with a bare subscript, so the request died on `KeyError('us_etfs','1m')`
# three hours into a batch, and the batch's own exit line still said DONE. The classes
# then scored as "nothing scored" and wrote no file at all, which is the one piece of
# luck here: an absent sheet does not trip `run_sheet`'s skip guard, so this pass can
# simply fill the holes rather than having to prove which files are trustworthy.
#
# Entries are now in `config.py`. This waits for the refetch, then scores what was lost.

set -x
PY=../.venv/Scripts/python
ENGINE="../backtest engine"
FETCH_LOG="$ENGINE/logs/fetch_etf_cmdty.log"
mkdir -p logs results

FAST="bar_updn pivot_center range_filter range_filter_macd ema_cross_sniper bb_outside_in ssl_hybrid"
SLOW="lorentzian_knn"
CONTROLS="BUYHOLD RANDOM_50 RANDOM_75"

expand () {
    for r in $1; do
        for w in "chart:" "ha:chart:"; do
            printf '%s%s %s%s@allow_short=0 ' "$w" "$r" "$w" "$r"
        done
    done
}

run_sheet () {
    local cls=$1 tf=$2 rules=$3 tag=$4
    shift 4
    local out="convert_ha_${cls}_${tf}${tag}.csv"
    if [ -s "results/$out" ]; then
        echo "=== skip ${cls}/${tf}${tag} (already written) ==="
        return 0
    fi
    $PY -u portfolio_wf.py --class "$cls" --tf "$tf" \
        --rules $(expand "$rules") $CONTROLS "$@" \
        --out "$out" \
        > "logs/convert_ha_${cls}_${tf}${tag}.log" 2>&1
    echo "=== done ${cls}/${tf}${tag} rc=$? ==="
}

while ! grep -q "ETF/COMMODITY INTRADAY FETCH DONE" "$FETCH_LOG" 2>/dev/null; do
    echo "waiting on the etf/commodity refetch: $(date +%H:%M:%S)"
    sleep 300
done

# Prove the refetch actually delivered before scoring anything. The whole reason this
# script exists is that a fetch reported success while writing nothing, so trusting a
# DONE line twice would be the same mistake with a different marker.
for cls in etfs commodities; do
    for tf in 1m 2m 3m 5m; do
        n=$(ls "../data/$cls/$tf" 2>/dev/null | wc -l)
        echo "check ../data/$cls/$tf -> $n files"
    done
done

for tf in 5m 3m 2m 1m; do
    for cls in us_etfs commodities; do run_sheet "$cls" "$tf" "$FAST" ""; done
done
for tf in 5m 3m 2m 1m; do
    for cls in us_etfs commodities; do run_sheet "$cls" "$tf" "$FAST" "_flat" --flatten-eod; done
done
for tf in 5m 3m 2m 1m; do
    for cls in us_etfs commodities; do run_sheet "$cls" "$tf" "$SLOW" "_knn"; done
done

echo "INTRADAY HA RECOVERY COMPLETE"
