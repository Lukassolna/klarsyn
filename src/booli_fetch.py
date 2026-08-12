"""
Klarsyn — Booli årsredovisning fetcher (browser automation).

Drives your REAL Google Chrome through Playwright, reusing a saved profile so you
stay logged in to Booli. Given a listing (/annons/… or /bostad/…) or a förening
(/bostadsrattsforening/…) URL, it opens the förening's document section and downloads
the Årsredovisning PDF — no manual clicking, no pixel coordinates.

Why this works (and sidesteps the Cloudflare Turnstile login wall):
  * You log in ONCE, by hand, in the Chrome window the script opens.
  * The login is saved in a persistent profile dir (.booli_profile), so later runs are
    already authenticated — the script never has to solve Turnstile itself.
  * The Årsredovisning file lives on Booli's public CDN (documents.bcdn.se); once the
    logged-in page hands us the link, the PDF downloads without any auth.

Setup (one time):
    pip install playwright
    playwright install chrome          # uses your installed Google Chrome

Usage:
    python src/booli_fetch.py https://www.booli.se/annons/6125326
    python src/booli_fetch.py https://www.booli.se/bostadsrattsforening/55598
    python src/booli_fetch.py <url> --headless      # after login is saved
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROFILE_DIR = ROOT / ".booli_profile"      # saved Chrome profile (keeps you logged in)
DATA.mkdir(exist_ok=True)


def _log(msg: str) -> None:
    # Diagnostics go to stderr so stdout stays clean for --print-path parsing.
    print(f"[booli_fetch] {msg}", file=sys.stderr, flush=True)


def _is_logged_in(page) -> bool:
    """Heuristic: logged-out pages show 'Logga in'; the doc links show 'Ladda ner'."""
    try:
        # If any "Ladda ner" is present, the document links are unlocked → logged in.
        if page.get_by_text("Ladda ner", exact=False).count() > 0:
            return True
    except Exception:
        pass
    return False


def _forening_id_from_next_data(page) -> str | None:
    """Read the housing-coop id from the page's embedded __NEXT_DATA__ JSON.
    More robust than a rendered <a> (works even when links are lazy/unrendered)."""
    try:
        txt = page.evaluate(
            "() => { const el = document.getElementById('__NEXT_DATA__');"
            " return el ? el.textContent : ''; }"
        ) or ""
    except Exception:
        txt = ""
    for pat in (r"/bostadsrattsforening/(\d+)", r"HousingCoop:(\d+)",
                r'"housingCoopId":\s*"?(\d+)'):
        m = re.search(pat, txt)
        if m:
            return m.group(1)
    return None


def _goto_forening(page, url: str) -> None:
    """Navigate to the förening page. If given a listing, resolve its förening."""
    _log(f"opening {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)

    if "/bostadsrattsforening/" in page.url:
        return  # already on the förening page

    fid = None
    # 1) Prefer a rendered link if present (fast path).
    try:
        page.wait_for_selector("a[href*='/bostadsrattsforening/']", timeout=6000)
        href = page.locator("a[href*='/bostadsrattsforening/']").first.get_attribute("href")
        if href:
            m = re.search(r"/bostadsrattsforening/(\d+)", href)
            fid = m.group(1) if m else None
    except PWTimeout:
        pass
    # 2) Fallback: parse __NEXT_DATA__ (robust in headless).
    if not fid:
        fid = _forening_id_from_next_data(page)
    if not fid:
        raise RuntimeError(
            "Could not resolve the förening from this listing. Pass the "
            "/bostadsrattsforening/<id> URL directly."
        )
    full = f"https://www.booli.se/bostadsrattsforening/{fid}"
    _log(f"resolved förening → {full}")
    page.goto(full, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)


def _ensure_login(page, forening_url: str, wait_secs: int = 300,
                  allow_manual: bool = True) -> None:
    if _is_logged_in(page):
        _log("session is logged in ✓")
        return
    if not allow_manual:
        raise RuntimeError(
            "NOT logged in and running headless — the saved Booli session has expired. "
            "Run once with a visible window to log in: python src/booli_fetch.py <url>"
        )
    _log("NOT logged in. A Chrome window is open — please LOG IN TO BOOLI now.")
    _log(f"(email/password + the Cloudflare checkbox). Waiting up to {wait_secs}s for you…")
    deadline = time.time() + wait_secs
    last_renav = time.time()
    while time.time() < deadline:
        page.wait_for_timeout(4000)
        # Non-disruptive check first: re-query the current DOM (no reload — a reload
        # would wipe a half-filled login form / Turnstile).
        if _is_logged_in(page):
            _log("detected login ✓")
            page.wait_for_timeout(1500)
            return
        # Every ~20s, if you're NOT on a login/challenge page, bounce back to the
        # förening page and re-check — covers the case where login lands elsewhere.
        if time.time() - last_renav > 20:
            cur = (page.url or "")
            if not any(s in cur for s in ("/konto", "logga-in", "challenges.cloudflare")):
                try:
                    page.goto(forening_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
                if _is_logged_in(page):
                    _log("detected login ✓")
                    return
            last_renav = time.time()
    raise RuntimeError("Timed out waiting for manual login.")


def _download_arsredovisning(context, page, out_path: Path) -> Path:
    """Find the Årsredovisning download control and save the PDF to out_path."""
    # Bring the documents section into view (helps lazy-rendered elements attach).
    try:
        page.get_by_text("Dokument", exact=False).first.scroll_into_view_if_needed(timeout=8000)
    except Exception:
        page.mouse.wheel(0, 4000)
    page.wait_for_timeout(800)

    ars = page.get_by_text("Årsredovisning", exact=True).first
    ars.scroll_into_view_if_needed(timeout=8000)

    # Strategy 1: the anchor right after the "Årsredovisning" label often carries the
    # CDN href once authenticated → download it directly with the session's cookies.
    try:
        anchor = ars.locator("xpath=following::a[1]")
        href = anchor.get_attribute("href", timeout=4000)
        if href and ("bcdn.se" in href or href.lower().endswith(".pdf")):
            _log(f"found direct PDF link → {href}")
            resp = context.request.get(href)
            if resp.ok and "pdf" in (resp.headers.get("content-type", "")):
                out_path.write_bytes(resp.body())
                return out_path
            _log("direct link did not return a PDF; falling back to click.")
    except Exception as e:
        _log(f"direct-link strategy skipped ({e}).")

    # Strategy 2: click the "Ladda ner" nearest to the Årsredovisning label and catch
    # the browser download event.
    dl_control = ars.locator("xpath=following::*[normalize-space(.)='Ladda ner'][1]")
    _log("clicking 'Ladda ner' and waiting for the download…")
    with page.expect_download(timeout=30000) as di:
        dl_control.click()
    download = di.value
    download.save_as(str(out_path))
    return out_path


def fetch(url: str, headless: bool = False) -> Path:
    with sync_playwright() as p:
        # channel="chrome" drives your installed Google Chrome (less bot-detection than
        # bundled Chromium). Falls back to Chromium if Chrome isn't found.
        launch_kwargs = dict(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = p.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
        except Exception as e:
            _log(f"Chrome channel unavailable ({e}); using bundled Chromium.")
            context = p.chromium.launch_persistent_context(**launch_kwargs)

        page = context.pages[0] if context.pages else context.new_page()
        try:
            _goto_forening(page, url)
            forening_url = page.url
            _ensure_login(page, forening_url, allow_manual=not headless)
            # Re-navigate the förening page after login so document links populate.
            if not _is_logged_in(page):
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(1500)

            fid = page.url.rstrip("/").split("/")[-1]
            out = DATA / f"arsredovisning_booli_{fid}.pdf"
            result = _download_arsredovisning(context, page, out)

            size = result.stat().st_size if result.exists() else 0
            head = result.read_bytes()[:5] if result.exists() else b""
            ok = head.startswith(b"%PDF")
            _log(f"saved {result}  ({size:,} bytes)  {'PDF ✓' if ok else '⚠ not a PDF'}")
            if not ok:
                raise RuntimeError("downloaded file is not a valid PDF")
            return result
        finally:
            if not headless:
                _log("done — you can close the Chrome window (or it stays for reuse).")
            context.close()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    headless = "--headless" in sys.argv
    print_path = "--print-path" in sys.argv
    if not args:
        print(__doc__)
        sys.exit(1)
    try:
        result = fetch(args[0], headless=headless)
    except Exception as e:
        _log(f"ERROR: {e}")
        sys.exit(2)
    if print_path:
        # ONLY the path on stdout, so callers (Streamlit subprocess) can parse it.
        print(str(result))


if __name__ == "__main__":
    main()
