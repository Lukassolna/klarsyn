"""
Klarsyn — Booli scraper (Phase A, live).

Two public pages, both embedding a Next.js/Apollo cache (__NEXT_DATA__) we read directly:

  * the listing  /bostad/<id>            → apartment facts + Booli's own valuation (estimate)
  * the förening /bostadsrattsforening/<id> → BRF summary + economy Booli parsed from the AR

`fetch_listing` returns the apartment dict (shaped as analyze.py's `listing_override`, so a
pasted URL replaces manual entry). `fetch_forening` returns the BRF dict. `fetch_all` does
both in one call, reusing the coop id the listing already gives us (avoids a 2nd listing hit,
which Booli 403s on rapid repeats).

Design note: Booli's page structure is stable GraphQL, so we navigate it by key. But the BRF
summary/economy blocks are *generic* key/label/value lists — we flatten whatever data points
Booli returns rather than hardcoding a fixed set of metrics, so a differently-shaped förening
(oäkta, tomträtt, missing fields …) still comes through. The årsredovisning LLM extraction
remains the source of truth; this Booli data rides alongside as extra context and a
cross-check against hallucinated figures.

On any failure we return None (listing) / {} (förening) and the pipeline degrades gracefully.
"""
from __future__ import annotations
import http.cookiejar
import json
import re
import time
import urllib.request

# Full browser-like header set. Booli sits behind Cloudflare; a realistic fingerprint plus a
# warmed cookie session gives us the best shot from a server. (NB: no Accept-Encoding — we
# don't want gzip we'd have to decompress.)
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
               "image/apng,*/*;q=0.8"),
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    "Sec-Ch-Ua": '"Chromium";v="120", "Not(A:Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', re.DOTALL)

# One cookie-carrying opener reused across requests, like a browser tab. We "warm" it by
# hitting the homepage first so Cloudflare can hand us its clearance cookie before we ask
# for a listing (a bare listing request from a cold session is what tends to get 403'd).
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))
_warmed = False


def _warmup() -> None:
    global _warmed
    if _warmed:
        return
    _warmed = True
    try:
        req = urllib.request.Request("https://www.booli.se/", headers=HEADERS)
        _OPENER.open(req, timeout=20).read()
    except Exception:
        pass


def _raw(v):
    """FormattedValue objects expose {'raw': ...}; plain values pass through."""
    if isinstance(v, dict):
        return v.get("raw")
    return v


def _num(v):
    """Best-effort integer out of a FormattedValue / Swedish-formatted string / number.

    Booli is inconsistent: some values carry `raw` (a number), others only `value`/`formatted`
    as a spaced string ("583 000", "6 067 m²"). We take raw when present, else pull the digits
    out of whatever text is there. Returns None if nothing numeric is found.
    """
    if v is None:
        return None
    if isinstance(v, dict):
        if v.get("raw") is not None:
            return _num(v["raw"])
        v = v.get("formatted") or v.get("value") or v.get("plainText")
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        digits = re.sub(r"[^\d]", "", v.replace(" ", " "))
        return int(digits) if digits else None
    return None


def _text(v):
    """Best-effort display string out of a FormattedValue / DisplayText / plain value."""
    if v is None:
        return None
    if isinstance(v, dict):
        return v.get("formatted") or v.get("plainText") or v.get("value") or v.get("markdown")
    return v


def _fetch_html(url: str, retries: int = 4) -> str | None:
    _warmup()
    hdrs = {**HEADERS, "Referer": "https://www.booli.se/", "Sec-Fetch-Site": "same-origin"}
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            return _OPENER.open(req, timeout=25).read().decode("utf-8", "ignore")
        except Exception:
            time.sleep(1.5 * (i + 1))  # back off — Booli/Cloudflare throttles rapid repeats
    return None


def _apollo(url: str) -> dict | None:
    """Fetch a Booli page and return its Apollo cache dict, or None."""
    html = _fetch_html(url)
    if not html:
        return None
    m = NEXT_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))["props"]["pageProps"]["__APOLLO_STATE__"]
    except Exception:
        return None


def _datapoints(node) -> dict:
    """Flatten a Booli {dataPoints:[{key,label,value,assessment,description}]} block.

    Generic on purpose — returns {key: {label, value, assessment}} for whatever points are
    present, so we don't hardcode Booli's metric set (it varies by förening).
    """
    out: dict = {}
    for dp in (node or {}).get("dataPoints", []) or []:
        key = dp.get("key")
        if not key:
            continue
        out[key] = {
            "label": dp.get("label"),
            "value": _text(dp.get("value")),
            "assessment": dp.get("assessment"),
        }
    return out


def fetch_listing(url: str) -> dict | None:
    """Return apartment fields for a Booli listing URL, or None on failure."""
    if not url or "booli.se" not in url:
        return None
    apollo = _apollo(url)
    if not apollo:
        return None
    lk = next((k for k in apollo if k.startswith("Listing:")), None)
    if not lk:
        return None
    L = apollo[lk]

    coop_ref = (L.get("housingCoop") or {}).get("__ref") if isinstance(L.get("housingCoop"), dict) else None
    coop = apollo.get(coop_ref, {}) if coop_ref else {}
    # The coop id lets the doc fetcher (booli_docs) skip a second listing request (→ 403).
    forening_id = None
    if coop_ref and ":" in coop_ref:
        try:
            forening_id = int(coop_ref.split(":")[1])
        except (ValueError, IndexError):
            forening_id = None

    # Booli's own valuation — the key new signal. `low`/`high` sometimes lack `raw`, so _num.
    est = L.get("estimate") or {}
    estimate = None
    if est:
        estimate = {
            "varde_kr": _num(est.get("price")),
            "low_kr": _num(est.get("low")),
            "high_kr": _num(est.get("high")),
        }
        if not any(estimate.values()):
            estimate = None

    # amenities are refs like Amenity:{"key":"balcony"} — pull the key out generically.
    amenities = []
    for a in L.get("amenities", []) or []:
        ref = a.get("__ref", "") if isinstance(a, dict) else ""
        m = re.search(r'"key":"([^"]+)"', ref)
        if m:
            amenities.append(m.group(1))

    # Prior sales of THIS apartment (ids only in the cache; price needs a follow-up fetch).
    sale_ids = []
    for s in L.get("salesOfResidence", []) or []:
        ref = s.get("__ref", "") if isinstance(s, dict) else ""
        if ":" in ref:
            sale_ids.append(ref.split(":")[1])

    return {
        "address": L.get("streetAddress"),
        "omrade": L.get("descriptiveAreaName"),
        "boarea_m2": _raw(L.get("livingArea")),
        "rooms": _raw(L.get("rooms")),
        "utgangspris_kr": _raw(L.get("listPrice")) or _raw(L.get("soldPrice")),
        "kr_per_m2": _raw(L.get("listSqmPrice")),
        "avgift_kr_man": _raw(L.get("rent")),
        "driftkostnad_kr_man": _raw(L.get("operatingCost")),
        "byggar": L.get("constructionYear"),
        "vaning": _raw(L.get("floor")),
        "antal_vaningar": L.get("buildingFloors"),
        "upplatelseform": L.get("tenureForm"),
        "amenities": amenities,
        "booli_estimate": estimate,
        "tidigare_forsaljning_ids": sale_ids,
        "forening_namn": coop.get("name"),
        "forening_id": forening_id,
        "source_url": url,
    }


def fetch_forening(coop_id: int | None, url: str | None = None) -> dict:
    """Return the BRF's Booli-parsed summary + economy, or {} on failure.

    Pass either the coop id (preferred — the listing scrape already has it) or a förening URL.
    """
    if coop_id is None and url:
        m = re.search(r"/bostadsrattsforening/(\d+)", url)
        coop_id = int(m.group(1)) if m else None
    if coop_id is None:
        return {}
    apollo = _apollo(f"https://www.booli.se/bostadsrattsforening/{coop_id}")
    if not apollo:
        return {}
    hc = apollo.get(f"HousingCoop:{coop_id}") or next(
        (apollo[k] for k in apollo if k.startswith("HousingCoop:")), None)
    if not hc:
        return {}

    # documents are stored as __ref pointers to top-level HousingCoopDocument objects.
    documents = []
    for d in (hc.get("documents") or []):
        if not isinstance(d, dict):
            continue
        doc = apollo.get(d["__ref"], {}) if d.get("__ref") else d
        documents.append({"type": doc.get("type"), "label": doc.get("label"),
                          "has_url": bool(doc.get("url"))})
    antal_lagenheter = sum(1 for k in apollo if k.startswith("Residence:"))

    out = {
        "forening_id": coop_id,
        "namn": hc.get("name"),
        "orgnummer": hc.get("orgNumber"),
        "beskrivning": _text(hc.get("description")),
        "har_arsredovisning": hc.get("hasValidAnnualReport"),
        "sammanfattning": _datapoints(hc.get("summary")),   # byggår, antal bostäder, mark, äkta…
        "ekonomi": _datapoints(hc.get("economy")),          # lån, belåning/kvm, sparande, RAG…
        "dokument": documents,
        "antal_lagenheter_pa_sidan": antal_lagenheter,
    }

    # Convenience: belåning per kvm computed ourselves when Booli leaves its field blank but
    # gives us the raw loan + total area (both live in the two data-point blocks).
    loan = _num((out["ekonomi"].get("totalLoan") or {}).get("value"))
    area = _num((out["sammanfattning"].get("totalLivingArea") or {}).get("value"))
    if loan and area:
        out["belaning_per_kvm_kr"] = round(loan / area)
    return out


def fetch_all(url: str) -> dict:
    """Full Phase-A scrape for a listing URL: {'listing': {...}, 'forening': {...}}."""
    listing = fetch_listing(url)
    forening = {}
    if listing and listing.get("forening_id"):
        forening = fetch_forening(listing["forening_id"])
        # The listing page often omits the coop name / byggår that the BRF page carries.
        if not listing.get("forening_namn"):
            listing["forening_namn"] = forening.get("namn")
        if not listing.get("byggar"):
            listing["byggar"] = _num((forening.get("sammanfattning", {})
                                      .get("constructionYear") or {}).get("value"))
    return {"listing": listing, "forening": forening}


if __name__ == "__main__":
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else "https://www.booli.se/bostad/449903"
    print(json.dumps(fetch_all(u), ensure_ascii=False, indent=2))
