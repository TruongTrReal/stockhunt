# Full walk-forward re-run on the 2000-01-01 window and the quarantined universe.
#
# Written 2026-08-11, after two changes invalidated every us_stocks sheet at once:
# `config.BACKTEST_START = 2000-01-01` and the liquidity/price quarantine that removed 69
# recycled tickers. Other classes carry no impostors but are cut by the same window, so
# they are rebuilt too -- a dashboard comparing a 2000+ stocks sheet against a 1993+ ETF
# sheet is comparing two studies.
#
# SEQUENTIAL on purpose. Each stage is single-core and memory-hungry (strat_wf peaked at
# 2.7 GB), and the later stages READ what the earlier ones wrote:
#
#     walkforward  ->  wf_folds_*      which variants.py shortlists from
#                  ->  wf_summary_*    which riskmatch_wf and the dashboard read
#     riskmatch    ->  edge_standard.csv, the single place a verdict exists
#
# Running them concurrently would have variants.py shortlist from a half-written file.
#
# `riskmatch_wf.py` is deliberately UNSCOPED. A scoped run writes edge_standard.partial.csv
# and leaves the real verdict alone -- that guard exists because the file was clobbered
# twice in one session -- so anything narrower here would silently not update the page.

$ErrorActionPreference = "Continue"
$py = (Resolve-Path "..\.venv\Scripts\python.exe").Path
$log = "logs"
New-Item -ItemType Directory -Force $log | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$summary = "$log\rerun_$stamp.summary"

$stages = @(
    @{ n = "1_walkforward"; a = "walkforward.py --class us_stocks us_etfs crypto commodities --tf 1d 4h" },
    @{ n = "2_variants";    a = "variants.py --class us_stocks us_etfs crypto commodities --tf 1d 4h" },
    @{ n = "3_prereg";      a = "prereg.py --class us_stocks us_etfs crypto commodities --tf 1d 4h --freeze" },
    @{ n = "4_strat_wf";    a = "strat_wf.py --class us_stocks us_etfs crypto commodities --tf 1d 4h" },
    @{ n = "5_combo_wf";    a = "combo_wf.py --class us_stocks us_etfs crypto commodities --tf 1d 4h" },
    @{ n = "6_riskmatch";   a = "riskmatch_wf.py --tf 1d 4h" },
    @{ n = "7_gatecalib";   a = "gate_calibration.py" },
    @{ n = "8_curves";      a = "curves.py --class us_stocks us_etfs crypto commodities --tf 1d 4h" },
    @{ n = "9_portfolio";   a = "portfolio_wf.py --class us_stocks --tf 1d --pit --catalog --out portfolio_final_2000.csv" }
)

"rerun started $(Get-Date -Format 'HH:mm:ss')  window=2000-01-01" | Tee-Object $summary
foreach ($s in $stages) {
    $t0 = Get-Date
    "[$($t0.ToString('HH:mm:ss'))] START $($s.n)  $($s.a)" | Tee-Object $summary -Append
    $p = Start-Process -FilePath $py -ArgumentList $s.a -WindowStyle Hidden -PassThru `
         -RedirectStandardOutput "$log\$($s.n).log" -RedirectStandardError "$log\$($s.n).err"
    $p.WaitForExit()
    $mins = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)
    $verdict = if ($p.ExitCode -eq 0) { "OK" } else { "FAILED exit=$($p.ExitCode)" }
    "[$((Get-Date).ToString('HH:mm:ss'))] $verdict $($s.n)  ${mins}m" | Tee-Object $summary -Append
    # Do NOT stop on failure. A later stage that cannot read a missing input fails loudly
    # in its own log, and stopping here would leave the earlier, already-correct sheets
    # looking as stale as the ones that never ran.
}
"rerun finished $(Get-Date -Format 'HH:mm:ss')" | Tee-Object $summary -Append
