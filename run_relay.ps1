# Klarsyn relay — run on your PC so Booli fetches use your residential IP while the app
# stays hosted at klarsyn.streamlit.app.
#
# Booli/Cloudflare 403s Streamlit Cloud's datacenter IP. This starts a small relay that does
# the Booli fetching locally, plus a public tunnel (built-in ssh -> localhost.run, no signup).
# Copy the https://...lhr.life URL it prints into the app's KLARSYN_RELAY secret.
#
# Prereq: put your Booli cookie in .env on this machine:
#   BOOLI_SID=r%3A...your sid value...
#   KLARSYN_RELAY_TOKEN=klarsyn-relay-x7q2m9   (must match the app's secret)
#
# Usage: right-click -> Run with PowerShell (keep the window open; closing it stops the site).

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Starting Klarsyn relay on http://localhost:8899 ..." -ForegroundColor Cyan
Start-Process -WindowStyle Minimized python -ArgumentList "relay.py"
Start-Sleep -Seconds 3

Write-Host "Opening public tunnel — copy the https://...lhr.life URL below into the" -ForegroundColor Cyan
Write-Host "KLARSYN_RELAY secret in your Streamlit app, then it works from klarsyn.streamlit.app." -ForegroundColor Cyan
ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes `
  -R 80:localhost:8899 nokey@localhost.run
