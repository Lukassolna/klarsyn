"""
Klarsyn — no-browser Booli document fetch.

Fetches a BRF's årsredovisning PDF over plain HTTP — NO Playwright, NO Chrome, no home PC.

How it works (established empirically, see progress.md § Session 4):
  * The förening's document link is served by the `getHousingCoop` GraphQL query, but the
    `url` field is BLANK unless the request carries a logged-in Booli session cookie.
  * Only Booli's *login* is Cloudflare-challenged — reads and this GraphQL query are NOT.
  * The session cookie (`sid`) is NOT IP-bound, so a cookie captured once (in any browser)
    works from a server / any network.
  * The PDF itself lives on a PUBLIC CDN (documents.bcdn.se) and needs no auth at all.

So the whole recipe is: carry a stored `sid` cookie → ask getHousingCoop for the document
url → download the public PDF. Log in to Booli once, copy the `sid` cookie's value, put it in
BOOLI_SID (local .env, or a Streamlit secret). If it ever expires the url comes back empty and
we raise BooliAuthError → refresh the cookie.

CLI:  python src/booli_docs.py https://www.booli.se/bostad/466517
      python src/booli_docs.py https://www.booli.se/bostadsrattsforening/55598 --urls-only
"""
from __future__ import annotations
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
try:  # allow local BOOLI_SID / keys from .env
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

GRAPHQL = "https://www.booli.se/graphql"
# Persisted-query hash for `getHousingCoop`, captured from booli.se. If Booli rotates their
# client the hash changes and the query 404s (PersistedQueryNotFound) — recapture it from
# DevTools → Network → the graphql request → the `sha256Hash` in the URL.
GETHOUSINGCOOP_HASH = "919a7d30f31d598ae973942c427c2e3aaf09460c55cc471fc6a8f02737358d8b"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class BooliAuthError(RuntimeError):
    """The Booli session cookie is missing or expired (document urls come back empty)."""


def _sid(sid: str | None) -> str:
    sid = sid or os.getenv("BOOLI_SID")
    if not sid:
        raise BooliAuthError(
            "No Booli session cookie. Set BOOLI_SID to the value of the `sid` cookie from "
            "a logged-in booli.se session (local .env or a Streamlit secret).")
    return sid.strip()


def _get(url: str, cookie: str | None = None, accept: str = "text/html",
         extra: dict | None = None, timeout: int = 30, retries: int = 3) -> bytes:
    headers = {
        "User-Agent": UA,
        "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
        "Accept": accept,
        "Sec-Ch-Ua": '"Chromium";v="120", "Not(A:Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Origin": "https://www.booli.se",
        "Referer": "https://www.booli.se/",
    }
    if cookie:
        headers["Cookie"] = cookie
    if extra:
        headers.update(extra)
    # Route through a residential proxy when BOOLI_PROXY is set (Booli 403s datacenter IPs
    # like Streamlit Cloud). Unset → direct, unchanged.
    proxy = os.getenv("BOOLI_PROXY")
    opener = (urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})) if proxy
        else urllib.request.build_opener())
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 429, 502, 503) and i < retries - 1:
                import time
                time.sleep(1.5 * (i + 1))  # Booli throttles rapid repeats → back off
                continue
            raise
        except urllib.error.URLError as e:
            last = e
            if i < retries - 1:
                import time
                time.sleep(1.5 * (i + 1))
                continue
            raise
    raise last if last else RuntimeError("request failed")


def coop_id_from_url(url: str) -> int | None:
    """Resolve the housingCoop id from a förening URL or a listing (/bostad|/annons) URL."""
    m = re.search(r"/bostadsrattsforening/(\d+)", url)
    if m:
        return int(m.group(1))
    # Listing URL → the page's embedded JSON references the coop as "HousingCoop:<id>".
    try:
        html = _get(url).decode("utf-8", "ignore")
    except Exception:
        return None
    m = (re.search(r'"HousingCoop:(\d+)"', html)
         or re.search(r"/bostadsrattsforening/(\d+)", html)
         or re.search(r'"housingCoopId":\s*"?(\d+)', html))
    return int(m.group(1)) if m else None


def get_document_urls(coop_id: int, sid: str | None = None) -> dict[str, str]:
    """Return {doc_type: url} for the coop (annualreport / byelaws / energideklaration).

    Empty dict means the session did not un-blank any urls → treat as auth failure upstream.
    """
    variables = json.dumps({"housingCoopId": coop_id}, separators=(",", ":"))
    extensions = json.dumps(
        {"clientLibrary": {"name": "@apollo/client", "version": "4.1.6"},
         "persistedQuery": {"version": 1, "sha256Hash": GETHOUSINGCOOP_HASH}},
        separators=(",", ":"))
    qs = urllib.parse.urlencode({"operationName": "getHousingCoop",
                                 "variables": variables, "extensions": extensions})
    raw = _get(
        f"{GRAPHQL}?{qs}",
        cookie=f"sid={_sid(sid)}",
        accept="application/json",
        # content-type satisfies Apollo's CSRF guard; api-client is what booli.se sends.
        extra={"content-type": "application/json", "api-client": "booli.se"},
    )
    data = json.loads(raw.decode("utf-8", "ignore"))
    docs = (((data.get("data") or {}).get("housingCoop") or {}).get("documents")) or []
    return {d.get("type"): d.get("url") for d in docs if d.get("url")}


def download_pdf(url: str, out: Path) -> Path:
    """Download a public CDN PDF (no auth). Validates the %PDF- magic bytes."""
    data = _get(url, accept="application/pdf", timeout=60)
    if data[:5] != b"%PDF-":
        raise RuntimeError("Downloaded file is not a valid PDF.")
    out.write_bytes(data)
    return out


def fetch_annual_report(url: str, sid: str | None = None, out_dir: Path = DATA,
                        coop_id: int | None = None) -> Path:
    """Listing/förening URL → saved årsredovisning PDF path. The full no-browser fetch.

    Pass `coop_id` if you already know it (e.g. from booli.fetch_listing) to avoid a second
    request to the listing page, which Booli tends to 403 on rapid repeats.
    """
    coop_id = coop_id or coop_id_from_url(url)
    if not coop_id:
        raise RuntimeError(f"Could not resolve a bostadsrättsförening from: {url}")
    urls = get_document_urls(coop_id, sid)
    pdf_url = urls.get("annualreport")
    if not pdf_url:
        raise BooliAuthError(
            "No annual-report link returned — the Booli session cookie is missing or expired. "
            "Log in to booli.se again and update BOOLI_SID.")
    return download_pdf(pdf_url, out_dir / f"arsredovisning_booli_{coop_id}.pdf")


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)
    url = args[0]
    try:
        if "--urls-only" in sys.argv:
            cid = coop_id_from_url(url)
            print(f"coop_id={cid}")
            for t, u in get_document_urls(cid).items():
                print(f"  {t}: {u}")
        else:
            path = fetch_annual_report(url)
            print(str(path))
    except BooliAuthError as e:
        print(f"[booli_docs] AUTH: {e}", file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        print(f"[booli_docs] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
