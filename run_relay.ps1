# Klarsyn relay - run on your PC so Booli fetches use your residential IP while the app
# stays hosted at klarsyn.streamlit.app.
#
# HOW IT WORKS
#   Streamlit Cloud can't reach Booli (datacenter IP blocked) or your PC directly (no fixed
#   public IP). This starts a small relay that fetches Booli locally + a Cloudflare tunnel
#   the cloud app calls. Cloudflare's tunnel is stable (unlike localhost.run).
#
# PREREQ on this machine:
#   - cloudflared.exe in this folder (already downloaded)
#   - .env contains:
#       BOOLI_SID=r%3A...your sid...
#       KLARSYN_RELAY_TOKEN=klarsyn-relay-x7q2m9
#
# USE: right-click -> Run with PowerShell (or: powershell -ExecutionPolicy Bypass -File run_relay.ps1)
#   It prints a https://xxxx.trycloudflare.com URL. Put that in the app's KLARSYN_RELAY secret.
#   Keep this window open = site live. Close it = fetching stops.
#
# NOTE: the URL changes each time you restart this. While it runs it stays up reliably;
#   if you restart, update KLARSYN_RELAY with the new url. (Want a fixed url / PC-off / no
#   window at all? Use a residential proxy instead - see RELAY_RUNBOOK.md, BOOLI_PROXY.)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

Write-Host "Starting Klarsyn relay on http://localhost:8899 ..." -ForegroundColor Cyan
Start-Process -WindowStyle Minimized python -ArgumentList "relay.py"
Start-Sleep -Seconds 3

Write-Host ""
Write-Host "Opening Cloudflare tunnel. COPY the https://...trycloudflare.com URL below into the" -ForegroundColor Cyan
Write-Host "KLARSYN_RELAY secret in your Streamlit app. Keep this window open." -ForegroundColor Cyan
Write-Host ""
& .\cloudflared.exe tunnel --url http://localhost:8899 --no-autoupdate

Write-Host ""
Write-Host "Tunnel closed. Site is down. Re-run this script to bring it back (you get a new url)." -ForegroundColor Yellow
