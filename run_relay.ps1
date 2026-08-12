# Klarsyn relay — run on your PC so Booli fetches use your residential IP while the app
# stays hosted at klarsyn.streamlit.app.
#
# HOW IT WORKS
#   Streamlit Cloud can't reach Booli (datacenter IP is blocked) and can't reach your PC
#   directly (no fixed public IP). So this starts a small relay that fetches Booli locally,
#   plus a public tunnel that gives your PC a stable web address the cloud app can call.
#
# ONE-TIME SETUP (makes the tunnel address PERMANENT, so you set the secret only once):
#   1. Sign up free at https://admin.localhost.run  and add this machine's SSH public key
#      (C:\Users\lukas\.ssh\id_ed25519.pub).
#   2. Put your Booli cookie + token in .env:
#        BOOLI_SID=r%3A...your sid...
#        KLARSYN_RELAY_TOKEN=klarsyn-relay-x7q2m9
#   3. In the Streamlit app secrets set (once):
#        KLARSYN_RELAY = "https://<your-stable>.lhr.life"   (printed below on first run)
#        KLARSYN_RELAY_TOKEN = "klarsyn-relay-x7q2m9"
#
# THEN, whenever you want the site live: right-click this file -> Run with PowerShell,
# and leave the window open. It auto-reconnects if the tunnel drops (same URL each time).

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

Write-Host "Starting Klarsyn relay on http://localhost:8899 ..." -ForegroundColor Cyan
Start-Process -WindowStyle Minimized python -ArgumentList "relay.py"
Start-Sleep -Seconds 3

Write-Host "Opening public tunnel. Copy the https://...lhr.life URL below into KLARSYN_RELAY" -ForegroundColor Cyan
Write-Host "the first time; it stays the same afterwards. Keep this window open." -ForegroundColor Cyan
while ($true) {
    # Keyed connection (no 'nokey@') -> localhost.run gives your registered, STABLE subdomain.
    ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes `
        -R 80:localhost:8899 localhost.run
    Write-Host "Tunnel dropped — reconnecting in 3s (same URL) ..." -ForegroundColor Yellow
    Start-Sleep -Seconds 3
}
