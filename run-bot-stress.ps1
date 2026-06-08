# run-bot-stress.ps1
# Copies the stress suite into the running bot container and executes it.
#
# Usage:
#   .\run-bot-stress.ps1                      # quick mode, all scenarios
#   .\run-bot-stress.ps1 -Mode full           # full load
#   .\run-bot-stress.ps1 -Mode spike          # spike test
#   .\run-bot-stress.ps1 -Mode custom -Requests 2000 -Concurrency 200
#   .\run-bot-stress.ps1 -Scenarios user      # user scenarios only
#   .\run-bot-stress.ps1 -Scenarios admin     # admin scenarios only

param(
    [string]$Mode        = "quick",
    [int]   $Requests    = 0,
    [int]   $Concurrency = 0,
    [string]$Scenarios   = "all",
    [string]$Output      = "/tmp/bot_stress_report.json"
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "   LOGISTICS BOT - STRESS TEST SUITE LAUNCHER" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host ""

$env:PATH += ";C:\Program Files\Docker\Docker\resources\bin"
$docker = "docker"

$ContainerName = "deploy-bot-1"
$ContainerCheck = & $docker ps --filter "name=$ContainerName" --format "{{.Names}}" 2>&1
if (-not $ContainerCheck) {
    Write-Host "ERROR: Container '$ContainerName' is not running." -ForegroundColor Red
    Write-Host "Run .\setup-dev.ps1 first to start the environment." -ForegroundColor Yellow
    exit 1
}

Write-Host "[OK] Found container: $ContainerName" -ForegroundColor Green

Write-Host "Copying stress suite into container..." -ForegroundColor Yellow
& $docker cp "load_test\bot_stress" "${ContainerName}:/tmp/bot_stress"
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: docker cp failed." -ForegroundColor Red; exit 1 }
Write-Host "[OK] Stress suite copied." -ForegroundColor Green

$ExecCmd = "cd /tmp/bot_stress && python runner.py --mode $Mode --scenarios $Scenarios --output $Output"
if ($Requests -gt 0)    { $ExecCmd += " --requests $Requests" }
if ($Concurrency -gt 0) { $ExecCmd += " --concurrency $Concurrency" }

Write-Host ""
Write-Host "Running: $ExecCmd" -ForegroundColor Yellow
Write-Host ""

& $docker exec $ContainerName bash -c $ExecCmd
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Stress test exited with errors." -ForegroundColor Red
    exit $LASTEXITCODE
}

$LocalReport = "load_test\results\bot_stress_$(Get-Date -Format 'yyyyMMdd_HHmmss').json"
New-Item -ItemType Directory -Force -Path "load_test\results" | Out-Null
& $docker cp "${ContainerName}:$Output" $LocalReport 2>&1 | Out-Null
if (Test-Path $LocalReport) {
    Write-Host ""
    Write-Host "[OK] JSON report saved -> $LocalReport" -ForegroundColor Green
}

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "   STRESS TEST COMPLETE" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
