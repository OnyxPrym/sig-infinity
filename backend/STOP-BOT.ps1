# Stop the local bot
$conns = Get-NetTCPConnection -LocalPort 10000 -State Listen -ErrorAction SilentlyContinue
if ($conns) {
    foreach ($c in $conns) {
        Write-Host "Stopping PID $($c.OwningProcess)..." -ForegroundColor Yellow
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}
tailscale funnel reset
Write-Host "Bot stopped." -ForegroundColor Green