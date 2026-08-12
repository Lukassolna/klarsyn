"""Klarsyn fetch relay — run this on a residential machine (your PC).

Booli/Cloudflare 403s datacenter IPs, so the Streamlit Cloud app can't fetch Booli itself.
This tiny server does the Booli work locally (from your residential IP, using your local
BOOLI_SID) and the cloud app calls it instead of hitting Booli directly.

Endpoints (POST, JSON, header X-Token must match KLARSYN_RELAY_TOKEN):
  /listing  {url}            -> booli.fetch_all(url)  (listing + förening)
  /pdf      {url, coop_id}   -> {"pdf_b64": ...} | {"error": "AUTH" | "<msg>"}
  /health   (GET)           -> {"ok": true}

Run:
  python relay.py                       # uses .env for BOOLI_SID + KLARSYN_RELAY_TOKEN
Then expose it (no signup, built-in ssh):
  ssh -R 80:localhost:8899 nokey@localhost.run
and set in the app's Streamlit secrets:
  KLARSYN_RELAY = "https://<your>.lhr.life"
  KLARSYN_RELAY_TOKEN = "<same token as here>"
"""
from __future__ import annotations
import base64
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

import booli as B          # noqa: E402
import booli_docs as BD     # noqa: E402

TOKEN = os.getenv("KLARSYN_RELAY_TOKEN", "changeme")
PORT = int(os.getenv("KLARSYN_RELAY_PORT", "8899"))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/health"):
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "POST /listing or /pdf"})

    def do_POST(self):
        if self.headers.get("X-Token") != TOKEN:
            self._send(403, {"error": "forbidden"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        path = self.path.rstrip("/")
        url = body.get("url")
        try:
            if path.endswith("/listing"):
                self._send(200, {"data": B.fetch_all(url)})
            elif path.endswith("/pdf"):
                pdf_path = BD.fetch_annual_report(url, coop_id=body.get("coop_id"))
                data = Path(pdf_path).read_bytes()
                self._send(200, {"pdf_b64": base64.b64encode(data).decode("ascii")})
            else:
                self._send(404, {"error": "unknown endpoint"})
        except BD.BooliAuthError as e:
            self._send(200, {"error": "AUTH", "detail": str(e)})
        except Exception as e:
            self._send(200, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    warn = "  ⚠ using DEFAULT token — set KLARSYN_RELAY_TOKEN" if TOKEN == "changeme" else ""
    print(f"Klarsyn relay listening on 0.0.0.0:{PORT}{warn}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
