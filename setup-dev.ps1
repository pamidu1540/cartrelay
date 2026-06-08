param (
    [string]$BotToken = "",
    [string]$DatadogApiKey = "d23b2da46acae58aed4aca58a0199122",
    [string]$DatadogSite = "us5.datadoghq.com",
    [string]$AdminUsers = "",
    [string]$MapboxToken = ""
)

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "   🚚 LOGISTICS ENGINE - WINDOWS TEST ENVIRONMENT SETUP" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host ""

# Ensure we are in the right directory
$DeployDir = Join-Path $PSScriptRoot "deploy"
if (-Not (Test-Path $DeployDir)) {
    Write-Host "ERROR: deploy directory not found. Please run this script from the project root." -ForegroundColor Red
    exit 1
}

# 1. Collect Telegram Bot Token
if ([string]::IsNullOrWhiteSpace($BotToken)) {
    $BotToken = Read-Host "Enter your Telegram Bot Token (from @BotFather)"
}
if ([string]::IsNullOrWhiteSpace($BotToken)) {
    Write-Host "ERROR: Telegram Bot Token is required." -ForegroundColor Red
    exit 1
}

# 2. Collect Admin User IDs
if ([string]::IsNullOrWhiteSpace($AdminUsers)) {
    $AdminUsers = Read-Host "Enter Admin Telegram User ID(s) (comma separated)"
}

# 3. Collect Datadog Config
if ([string]::IsNullOrWhiteSpace($DatadogApiKey)) {
    $InputApiKey = Read-Host "Enter Datadog API Key (Press Enter to skip)"
    if (-Not [string]::IsNullOrWhiteSpace($InputApiKey)) {
        $DatadogApiKey = $InputApiKey
    }
}

if ([string]::IsNullOrWhiteSpace($DatadogSite)) {
    $InputSite = Read-Host "Enter Datadog Site (Press Enter to use default: us5.datadoghq.com)"
    if (-Not [string]::IsNullOrWhiteSpace($InputSite)) {
        $DatadogSite = $InputSite
    }
}

$DatadogAgentHost = ""
if (-Not [string]::IsNullOrWhiteSpace($DatadogApiKey)) {
    $DatadogAgentHost = "datadog-agent"
}

# 4. Generate Secrets
Write-Host "`nGenerating secure local testing secrets..." -ForegroundColor Yellow
$InternalToken = -join ((48..57) + (97..122) | Get-Random -Count 32 | % {[char]$_})
$PostgresPassword = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 16 | % {[char]$_})
Write-Host "✔ Secrets generated successfully." -ForegroundColor Green

# 5. Build .env file
Write-Host "Configuring deploy/.env file..." -ForegroundColor Yellow

$EnvPath = Join-Path $DeployDir ".env"
$EnvExamplePath = Join-Path $DeployDir ".env.example"

if (-Not (Test-Path $EnvExamplePath)) {
    Write-Host "ERROR: deploy/.env.example not found." -ForegroundColor Red
    exit 1
}

$EnvContent = Get-Content $EnvExamplePath -Raw

# Replace placeholders using Regex
$EnvContent = $EnvContent -replace 'TELEGRAM_BOT_TOKEN=.*', "TELEGRAM_BOT_TOKEN=$BotToken"
$EnvContent = $EnvContent -replace 'INTERNAL_TOKEN=.*', "INTERNAL_TOKEN=$InternalToken"
$EnvContent = $EnvContent -replace 'POSTGRES_PASSWORD=.*', "POSTGRES_PASSWORD=$PostgresPassword"
$EnvContent = $EnvContent -replace 'ADMIN_USER_IDS=.*', "ADMIN_USER_IDS=$AdminUsers"

# Mapbox Token
$EnvContent = $EnvContent -replace 'MAPBOX_TOKEN=.*', "MAPBOX_TOKEN=$MapboxToken"

# Datadog Observability
$EnvContent = $EnvContent -replace 'DD_API_KEY=.*', "DD_API_KEY=$DatadogApiKey"
$EnvContent = $EnvContent -replace 'DD_SITE=.*', "DD_SITE=$DatadogSite"
$EnvContent = $EnvContent -replace 'DD_AGENT_HOST=.*', "DD_AGENT_HOST=$DatadogAgentHost"

# Set dev environment overrides
$EnvContent = $EnvContent -replace 'DEPLOY_ENV=production', "DEPLOY_ENV=development"

Set-Content -Path $EnvPath -Value $EnvContent -Encoding UTF8
Write-Host "✔ deploy/.env configured successfully." -ForegroundColor Green

# 6. Launch Docker Compose
Write-Host "Starting local test environment via Docker Compose..." -ForegroundColor Yellow
Write-Host "Running: docker compose up --build -d" -ForegroundColor Gray

Push-Location $DeployDir

$dockerExists = Get-Command "docker" -ErrorAction SilentlyContinue
if ($dockerExists) {
    docker compose up --build -d
    if ($LASTEXITCODE -eq 0) {
        Write-Host "LOCAL TEST ENVIRONMENT IS RUNNING!" -ForegroundColor Green
    } else {
        Write-Host "Docker compose failed to start." -ForegroundColor Red
    }
} else {
    Write-Host "ERROR: docker is not installed or not in PATH." -ForegroundColor Red
}

Pop-Location
