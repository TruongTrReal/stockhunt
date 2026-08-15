# Regenerate the stages that feed the dashboard's DETAIL views, on the 2000-01-01 window.
#
# `edge_standard.csv` is already current -- the shortlist run rewrote it at 03:37, after
# `config.BACKTEST_START` landed. These four are not: every one was written before ~03:00,
# so they carry the liquidity quarantine but NOT the window, which is why the ibs detail
# page still shows MNST at 2.78e12% over 53.6 years when the cut caps any asset at 26.6.
#
#   walkforward -> wf_per_asset_*   the per-asset table for TA-Lib singles
#                  wf_summary_*     the IR / Long % / CAGR diagnostics on every row
#   strat_wf    -> strat_per_asset_*  the per-asset table for the published catalogue,
#                                     which is the one `ibs` is read from
#   combo_wf    -> cwf_summary_*     the pair rows that share the leaderboard
#   curves      -> curves_*.json     the equity curves behind each detail page
#
# Order matters: combo_wf shortlists from `wf_folds_*`, so walkforward must land first.
# Sequential for the same reason as `rerun_all.ps1` -- these are single-core and
# memory-hungry, and running them together has a later stage read a half-written file.
#
# Not included: variants, prereg, riskmatch, gate_calibration. The first two feed sections
# the detail pages do not read, and riskmatch already ran on the correct window.

$ErrorActionPreference = "Continue"
$py = (Resolve-Path "..\.venv\Scripts\python.exe").Path
New-Item -ItemType Directory -Force logs | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$summary = "logs\detail_$stamp.summary"

$stages = @(
    @{ n = "1_walkforward"; a = "walkforward.py --class us_stocks us_etfs crypto commodities --tf 1d 4h" },
    @{ n = "2_strat_wf";    a = "strat_wf.py --class us_stocks us_etfs crypto commodities --tf 1d 4h" },
    @{ n = "3_combo_wf";    a = "combo_wf.py --class us_stocks us_etfs crypto commodities --tf 1d 4h" },
    @{ n = "4_curves";      a = "curves.py --class us_stocks us_etfs crypto commodities --tf 1d 4h" }
)

"detail rerun started $(Get-Date -Format 'HH:mm:ss')  window=2000-01-01" | Tee-Object $summary
foreach ($s in $stages) {
    $t0 = Get-Date
    "[$($t0.ToString('HH:mm:ss'))] START $($s.n)" | Tee-Object $summary -Append
    $p = Start-Process -FilePath $py -ArgumentList $s.a -WindowStyle Hidden -PassThru `
         -RedirectStandardOutput "logs\$($s.n).log" -RedirectStandardError "logs\$($s.n).err"
    $p.WaitForExit()
    $mins = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)
    $verdict = if ($p.ExitCode -eq 0) { "OK" } else { "FAILED exit=$($p.ExitCode)" }
    "[$((Get-Date).ToString('HH:mm:ss'))] $verdict $($s.n)  ${mins}m" | Tee-Object $summary -Append
}
"detail rerun finished $(Get-Date -Format 'HH:mm:ss')" | Tee-Object $summary -Append
