# ============================================================
#  SIG INFINITY AI - Local Bot Startup
#  Runs server.py + Tailscale Funnel
# ============================================================

$ErrorActionPreference = "Continue"
$kit = "C:\Users\User\sig-infinity-deploy\backend"

Clear-Host
Write-Host ""
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host "  SIG INFINITY AI - LOCAL BOT" -ForegroundColor Cyan
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor DarkGray
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host ""

Set-Location $kit

# --- Kill any old server on port 10000 ---
$old = Get-NetTCPConnection -LocalPort 10000 -State Listen -ErrorAction SilentlyContinue
if ($old) {
    foreach ($c in $old) {
        Write-Host "  Killing old PID $($c.OwningProcess)..." -ForegroundColor Yellow
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep 2
}

# --- Start Flask server in background ---
Write-Host "[1/3] Starting server.py..." -ForegroundColor Yellow
$serverLog = "$kit\server.log"
$serverErr = "$kit\server.err"

$proc = Start-Process -FilePath "python" -ArgumentList "server.py" `
    -WorkingDirectory $kit `
    -RedirectStandardOutput $serverLog `
    -RedirectStandardError $serverErr `
    -PassThru -NoNewWindow

Write-Host "  Server PID: $($proc.Id)" -ForegroundColor Gray
Write-Host "  Waiting for it to boot..." -ForegroundColor Gray
Start-Sleep 8

# --- Check it's actually up ---
try {
    $h = Invoke-WebRequest "http://localhost:10000/healthz" -UseBasicParsing -TimeoutSec 10
    Write-Host "  [OK] Server is up (healthz: $($h.StatusCode))" -ForegroundColor Green
} catch {
    Write-Host "  [X] Server didn't respond: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Last 20 lines of server.log:" -ForegroundColor Yellow
    Get-Content $serverLog -Tail 20 -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host "  Last 20 lines of server.err:" -ForegroundColor Yellow
    Get-Content $serverErr -Tail 20 -ErrorAction SilentlyContinue
    Read-Host "Press Enter to close"
    return
}

# --- Start Tailscale Funnel ---
Write-Host ""
Write-Host "[2/3] Starting Tailscale Funnel..." -ForegroundColor Yellow

# Check Tailscale is connected
$ts = tailscale status 2>&1 | Select-String "desktop-aalpnd1"
if (-not $ts) {
    Write-Host "  [X] Tailscale not connected" -ForegroundColor Red
    Write-Host "  Run: tailscale up" -ForegroundColor Yellow
    return
}

# Check if Funnel already enabled
$funnelStatus = tailscale funnel status 2>&1 | Out-String
if ($funnelStatus -match "10000") {
    Write-Host "  [=] Funnel already running" -ForegroundColor Gray
} else {
    Write-Host "  Enabling Funnel on port 10000..." -ForegroundColor Gray
    tailscale funnel --bg 10000
    Start-Sleep 3
}

# Get the public URL
$funnelOut = tailscale funnel status 2>&1 | Out-String
$url = "unknown"
if ($funnelOut -match "(https://[^\s]+)") {
    $url = $matches[1]
}

Write-Host "  [OK] Funnel active" -ForegroundColor Green
Write-Host "  Public URL: $url" -ForegroundColor Cyan
Write-Host ""

# --- Check collector ---
Write-Host "[3/3] Checking collector..." -ForegroundColor Yellow
Start-Sleep 5

try {
    $status = Invoke-RestMethod "http://localhost:10000/api/status" -TimeoutSec 15
    Write-Host "  collector_connected: $($status.collector_connected)" -ForegroundColor $(if ($status.collector_connected) { "Green" } else { "Yellow" })
    Write-Host "  collector_running:   $($status.collector_running)" -ForegroundColor $(if ($status.collector_running) { "Green" } else { "Yellow" })
    Write-Host "  session_loaded:      $($status.session_loaded)" -ForegroundColor $(if ($status.session_loaded) { "Green" } else { "Yellow" })
    if ($status.collector_error) {
        Write-Host "  collector_error:     $($status.collector_error)" -ForegroundColor Red
    }
} catch {
    Write-Host "  [X] Status check failed" -ForegroundColor Red
}

Write-Host ""
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host "  BOT IS RUNNING" -ForegroundColor Green
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Local:  http://localhost:10000" -ForegroundColor Cyan
Write-Host "  Public: $url" -ForegroundColor Cyan
Write-Host ""
Write-Host "  To stop: close this window OR run STOP-BOT.ps1" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Logs are in: server.log" -ForegroundColor Gray
Write-Host ""

# --- Show live log tail ---
Write-Host "  Live log (Ctrl+C to stop tailing, server keeps running):" -ForegroundColor Yellow
Get-Content $serverLog -Wait -Tail 20