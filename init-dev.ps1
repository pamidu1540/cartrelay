$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "   🚀 CARTRELAY - BOOTSTRAP TASK RUNNER (SOURCE)" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$ToolsDir = Join-Path $PSScriptRoot ".tools\bin"
if (-not (Test-Path $ToolsDir)) {
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
}

Write-Host "Compiling Task runner securely from source using Go..." -ForegroundColor Yellow
$env:GOBIN = (Resolve-Path $ToolsDir).Path
go install github.com/go-task/task/v3/cmd/task@latest

Write-Host "Task installed successfully!" -ForegroundColor Green
Write-Host "Running 'task setup' to bootstrap all other dependencies..." -ForegroundColor Yellow

$env:PATH = "$ToolsDir;" + $env:PATH
& "$ToolsDir\task.exe" setup

if ($LASTEXITCODE -eq 0) {
    Write-Host "==========================================================" -ForegroundColor Cyan
    Write-Host "🎉 Environment bootstrapped successfully!" -ForegroundColor Green
    Write-Host "To use the tools in your current shell, run:" -ForegroundColor Yellow
    Write-Host "    `$env:PATH = `".tools\bin;`" + `$env:PATH" -ForegroundColor White
    Write-Host "Then use commands like: task up, task lint, task test" -ForegroundColor White
} else {
    Write-Host "Failed to bootstrap environment." -ForegroundColor Red
}
