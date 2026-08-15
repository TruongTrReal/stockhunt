# Re-score the historical shortlist on the clean universe and the 2000-01-01 window.
#
# 107 candidates gathered from every sheet this project has produced: the standard's own
# >=4/6 near-misses, published strategies and their grid cells that beat an exposure-matched
# control, the top walk-forward pairs, the top singles, and the five pre-registered rules.
# 18 of them are catalog grid cells that `edge_standard.csv` has never once judged.
#
# `--n-trials 1273` is the load-bearing argument. These 107 are survivors of a search over
# 1,273 candidates, and correcting for 107 would lower every t bar and every noise ceiling
# for no reason but the shortlist being short. Declaring the original population is what
# keeps this a re-test rather than selection on the test set.
#
# `--promote` is deliberate: this shortlist IS the study, so it writes the real
# edge_standard.csv rather than the *.partial.csv a scoped run defaults to.

$ErrorActionPreference = "Continue"
$py = (Resolve-Path "..\.venv\Scripts\python.exe").Path
New-Item -ItemType Directory -Force logs | Out-Null

$rules = (Import-Csv results\shortlist.csv | ForEach-Object { $_.rule })
"shortlist: $($rules.Count) rules"

# One process, all four classes and both timeframes, so every sheet is scored against the
# same declared trial count in a single verdict file.
$argv = @("riskmatch_wf.py",
          "--class", "us_stocks", "us_etfs", "crypto", "commodities",
          "--tf", "1d", "4h",
          "--n-trials", "1273",
          "--promote",
          "--rules") + $rules

$t0 = Get-Date
$p = Start-Process -FilePath $py -ArgumentList $argv -WindowStyle Hidden -PassThru `
     -RedirectStandardOutput "logs\shortlist.log" -RedirectStandardError "logs\shortlist.err"
$p.WaitForExit()
"exit=$($p.ExitCode)  elapsed=$([math]::Round(((Get-Date)-$t0).TotalMinutes,1))m"
