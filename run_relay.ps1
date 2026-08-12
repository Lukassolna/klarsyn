# Klarsyn relay — run on your PC so Booli fetches use your residential IP while the app
# stays hosted at klarsyn.streamlit.app.
#
# HOW IT WORKS
#   Streamlit Cloud can't reach Booli (datacenter IP blocked) or your PC directly (no fixed
#   public IP). This starts a small relay that fetches Booli locally + a public tunnel the
#   cloud app calls. No account, no signup.
#
# PREREQ — in .env on this machine:
#   BOOLI_SID=r%3A...your sid...
#   KLARSYN_RELAY_TOKEN=klarsyn-relay-x7q2m9
#
# USE: right-click -> Run with PowerShell (or: powershell -ExecutionPolicy Bypass -File run_relay.ps1)
#   It prints a https://xxxx.lhr.life URL. Put that in the app's KLARSYN_RELAY secret.
#   Keep this window open = site live. Close it = fetching stops.
#
# NOTE: with no account the URL CHANGES each time you start this. If it dies, restart it and
#   update KLARSYN_RELAY with the new URL. (Tired of that? Use a residential proxy instead —
#   see RELAY_RUNBOOK.md, BOOLI_PROXY section: ~$7, no PC, no tunnel.)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

Write-Host "Starting Klarsyn relay on http://localhost:8899 ..." -ForegroundColor Cyan
Start-Process -WindowStyle Minimized python -ArgumentList "relay.py"
Start-Sleep -Seconds 3

Write-Host ""
Write-Host "Opening public tunnel. COPY the https://...lhr.life URL below into the" -ForegroundColor Cyan
Write-Host "KLARSYN_RELAY secret in your Streamlit app. Keep this window open." -ForegroundColor Cyan
Write-Host ""
ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes `
  -R 80:localhost:8899 nokey@localhost.run

Write-Host ""
Write-Host "Tunnel closed. The site is now down. Re-run this script to bring it back" -ForegroundColor Yellow
Write-Host "(you'll get a NEW url — update KLARSYN_RELAY with it)." -ForegroundColor Yellow
