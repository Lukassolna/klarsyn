# Klarsyn — run locally and expose it publicly (no signup, no extra downloads).
#
# Why: Streamlit Community Cloud runs on a datacenter IP, which Booli/Cloudflare 403s.
# Running the app from your own machine means Booli requests exit your *residential* IP,
# so the auto-fetch just works. This script starts the app and opens a public tunnel
# using the SSH client built into Windows.
#
# Usage:  right-click → Run with PowerShell   (or:  powershell -ExecutionPolicy Bypass -File run_klarsyn.ps1)
# The tunnel window prints a public https URL like https://xxxx.lhr.life — share that.
# Keep this window open; closing it takes the site down. The URL changes each run
# (create a free localhost.run account + add an SSH key for a permanent subdomain).

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Starting Klarsyn on http://localhost:8501 ..." -ForegroundColor Cyan
Start-Process -WindowStyle Minimized python -ArgumentList `
  "-m","streamlit","run","streamlit_app.py",`
  "--server.port","8501","--server.headless","true",`
  "--server.enableCORS","false","--server.enableXsrfProtection","false"

# give Streamlit a few seconds to boot before we tunnel to it
Start-Sleep -Seconds 6

Write-Host "Opening public tunnel (watch for the https://...lhr.life URL below) ..." -ForegroundColor Cyan
ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes `
  -R 80:localhost:8501 nokey@localhost.run
