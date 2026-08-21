#!/usr/bin/env bash
# The 5m half of the intraday Heikin-Ashi study, added 2026-08-20 at the owner's
# request. 128 cells, pre-registered before scoring.
#
# It is a SEPARATE file from `run_intraday_ha.sh` on purpose: that script was already
# running when 5m was asked for, and bash reads a script by byte offset as it executes,
# so editing a live one leaves the rest of the run undefined. Two files, no edit.
#
# 5m differs from 1m/2m/3m in one way that matters: it is a NATIVE vendor interval, so
# nothing is resampled and the cache predates this study. `us_stocks` runs immediately
# (21 symbols cached, SPY included); the other classes wait on the 1m fetch batch, which
# is saturating the API key, and then fetch their own 5m.

set -x
PY=../.venv/Scripts/python
ENGINE="../backtest engine"
FETCH_LOG="$ENGINE/logs/fetch_1m_batch.log"
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

# run_sheet <class> <tf> <rules> <tag> [extra portfolio_wf flags...]
# `shift 4` is load-bearing: without it the extra flags land in $5 and are silently
# dropped, and the flattened pass would write a copy of the unflattened one.
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

# ------------------------------------------------ phase 1: the cache we already have
run_sheet us_stocks 5m "$FAST" ""
run_sheet crypto    5m "$FAST" ""

# ---------------------------------------- phase 2: the classes that need a 5m fetch
while ! grep -q "FETCH BATCH DONE" "$FETCH_LOG" 2>/dev/null; do
    echo "waiting on 1m fetch before asking the vendor for 5m: $(date +%H:%M:%S)"
    sleep 300
done
( cd "$ENGINE" && $PY -u td_loader.py --class us_etfs --tf 5m )
( cd "$ENGINE" && $PY -u td_loader.py --class commodities --tf 5m --symbols XAU/USD XAG/USD WTI/USD )
( cd "$ENGINE" && $PY -u td_loader.py --class crypto --tf 5m )
( cd "$ENGINE" && $PY -u check_data.py --fix )
run_sheet us_etfs     5m "$FAST" ""
run_sheet commodities 5m "$FAST" ""
rm -f results/convert_ha_crypto_5m.csv      # rerun on the full 20-pair universe
run_sheet crypto      5m "$FAST" ""

# ------------------------------------------------- phase 3: the end-of-day BOUND
# Crypto is skipped: a 24/7 market has no session to flatten into.
for cls in us_stocks us_etfs commodities; do
    run_sheet "$cls" 5m "$FAST" "_flat" --flatten-eod
done

# ------------------------------------------------------ phase 4: the slow one
for cls in us_stocks crypto us_etfs commodities; do
    run_sheet "$cls" 5m "$SLOW" "_knn"
done

echo "INTRADAY HA 5M STUDY COMPLETE"
