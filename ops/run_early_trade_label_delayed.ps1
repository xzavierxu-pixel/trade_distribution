param(
    [int]$DelaySeconds = 7200,
    [string]$Config = "configs/early_trade_label_v1.yaml",
    [string]$RunDir = "models/early_trade_label_v1"
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
Set-Location -LiteralPath $repo

$runPath = Join-Path $repo $RunDir
New-Item -ItemType Directory -Force -Path $runPath | Out-Null

$statePath = Join-Path $runPath "delayed_run_state.json"
$logPath = Join-Path $runPath "delayed_run.log"
$startedAt = Get-Date
$plannedAt = $startedAt.AddSeconds($DelaySeconds)

@{
    status = "waiting"
    delay_seconds = $DelaySeconds
    started_at = $startedAt.ToUniversalTime().ToString("o")
    planned_run_at = $plannedAt.ToUniversalTime().ToString("o")
    config = $Config
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath $statePath

"[$((Get-Date).ToUniversalTime().ToString("o"))] Waiting $DelaySeconds seconds before formal run." | Add-Content -Encoding UTF8 -LiteralPath $logPath
Start-Sleep -Seconds $DelaySeconds

@{
    status = "running"
    delay_seconds = $DelaySeconds
    started_at = $startedAt.ToUniversalTime().ToString("o")
    planned_run_at = $plannedAt.ToUniversalTime().ToString("o")
    actual_run_started_at = (Get-Date).ToUniversalTime().ToString("o")
    config = $Config
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath $statePath

"[$((Get-Date).ToUniversalTime().ToString("o"))] Starting formal pipeline." | Add-Content -Encoding UTF8 -LiteralPath $logPath
try {
    python -m early_trade_label.cli run-all --config $Config *>> $logPath
    $status = "complete"
    $exitCode = 0
} catch {
    $_ | Out-String | Add-Content -Encoding UTF8 -LiteralPath $logPath
    $status = "failed"
    $exitCode = 1
}

@{
    status = $status
    exit_code = $exitCode
    delay_seconds = $DelaySeconds
    started_at = $startedAt.ToUniversalTime().ToString("o")
    planned_run_at = $plannedAt.ToUniversalTime().ToString("o")
    finished_at = (Get-Date).ToUniversalTime().ToString("o")
    config = $Config
    evaluation_path = (Join-Path $RunDir "evaluation.json")
    log_path = (Join-Path $RunDir "delayed_run.log")
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath $statePath

exit $exitCode
