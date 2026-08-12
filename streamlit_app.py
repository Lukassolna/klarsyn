"""
Klarsyn — Streamlit demo app.

Run:  streamlit run streamlit_app.py

Enter a Booli URL (prefilled with the anchor) or upload a BRF årsredovisning PDF, pick a
model, and Klarsyn runs the LIVE pipeline (AI extraction → deterministic scoring → report)
and renders a modern report. Model is selectable in the UI.
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

import os
import extract as E
import analyst as AN
import models as M
import booli as B
import booli_docs as BD
from report_html import render_html

# On Streamlit Community Cloud, secrets are set in the app's Secrets UI. Bridge them into
# os.environ so the Anthropic SDK (ANTHROPIC_API_KEY) and booli_docs (BOOLI_SID) read them
# exactly like they read a local .env.
for _k in ("ANTHROPIC_API_KEY", "BOOLI_SID", "KLARSYN_MODEL"):
    if not os.getenv(_k):
        try:
            if _k in st.secrets:
                os.environ[_k] = str(st.secrets[_k])
        except Exception:
            pass


@st.cache_data(show_spinner=False)
def fetch_booli(url: str):
    """Listing facts + the förening's Booli-parsed summary/economy (one call, two pages)."""
    return B.fetch_all(url)


@st.cache_data(show_spinner=False)
def fetch_pdf(url: str, coop_id: int | None):
    """No-browser fetch of the förening's årsredovisning via booli_docs (plain HTTP + a
    stored Booli `sid` cookie).

    Returns (path, error): error is None on success, "AUTH" when the session cookie is
    missing/expired, or a short diagnostic string on any other failure. The full traceback
    is printed to stderr so it lands in the Streamlit Cloud logs (Manage app → logs)."""
    import traceback
    try:
        return str(BD.fetch_annual_report(url, coop_id=coop_id)), None
    except BD.BooliAuthError:
        return None, "AUTH"
    except Exception as e:
        print(f"[fetch_pdf] FAILED url={url} coop_id={coop_id}\n{traceback.format_exc()}",
              file=sys.stderr, flush=True)
        return None, f"{type(e).__name__}: {e}"


# Client-side "AI working" panel. Pure HTML/CSS/JS so it keeps animating while the
# Python pipeline blocks on the LLM calls. Messages are for feel — deliberately vivid.
THINKING_HTML = """
<div id="klar-think">
<style>
#klar-think{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;max-width:640px;margin:0 auto;
  background:linear-gradient(135deg,#0f766e,#115e59);border-radius:20px;padding:26px 28px;color:#fff;
  box-shadow:0 12px 34px rgba(15,118,110,.28)}
#klar-think .hd{display:flex;align-items:center;gap:12px;font-weight:800;font-size:17px}
#klar-think .orb{width:26px;height:26px;border-radius:50%;background:radial-gradient(circle at 30% 30%,#fff,#5eead4);
  box-shadow:0 0 0 0 rgba(94,234,212,.7);animation:pulse 1.4s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(94,234,212,.6)}70%{box-shadow:0 0 0 14px rgba(94,234,212,0)}100%{box-shadow:0 0 0 0 rgba(94,234,212,0)}}
#klar-think .steps{margin:18px 0 6px;display:flex;flex-direction:column;gap:11px}
#klar-think .step{display:flex;align-items:center;gap:11px;font-size:14.5px;opacity:.4;transition:opacity .4s}
#klar-think .step.active{opacity:1;font-weight:600}
#klar-think .step.done{opacity:.85}
#klar-think .ic{width:20px;height:20px;flex:none;border-radius:50%;border:2px solid rgba(255,255,255,.35);
  display:flex;align-items:center;justify-content:center;font-size:12px}
#klar-think .step.active .ic{border-color:#5eead4;border-top-color:transparent;animation:spin .8s linear infinite}
#klar-think .step.done .ic{background:#5eead4;border-color:#5eead4;color:#0f766e}
@keyframes spin{to{transform:rotate(360deg)}}
#klar-think .bar{height:7px;background:rgba(255,255,255,.18);border-radius:6px;margin-top:16px;overflow:hidden}
#klar-think .fill{height:100%;width:2%;background:linear-gradient(90deg,#5eead4,#a7f3d0);border-radius:6px;transition:width .6s}
#klar-think .foot{font-size:12px;opacity:.8;margin-top:10px}
</style>
<div class="hd"><span class="orb"></span>Klarsyn analyserar…</div>
<div class="steps" id="klar-steps"></div>
<div class="bar"><div class="fill" id="klar-fill"></div></div>
<div class="foot" id="klar-foot">Detta tar oftast 1–2 minuter — vi läser hela årsredovisningen.</div>
<script>
const STEPS=[
 "Hämtar bostaden och föreningens uppgifter",
 "Läser årsredovisningen sida för sida",
 "Analyserar nyckeltal, skuldsättning och sparande",
 "Går igenom renoveringar, stambyten och underhåll",
 "Bedömer räntekänslighet och framtida avgifter",
 "Jämför priset mot marknaden i området",
 "Väger samman risker och sätter betyg",
 "Skriver ditt beslutsunderlag i klarspråk"];
const box=document.getElementById('klar-steps');
STEPS.forEach((s,i)=>{box.insertAdjacentHTML('beforeend',
 `<div class="step" id="st${i}"><span class="ic">${i+1}</span><span>${s}</span></div>`);});
let i=0;const fill=document.getElementById('klar-fill');
function tick(){
 if(i>0){const p=document.getElementById('st'+(i-1));p.className='step done';p.querySelector('.ic').textContent='✓';}
 if(i<STEPS.length){document.getElementById('st'+i).className='step active';}
 const pct=Math.min(94,6+i*(88/STEPS.length));fill.style.width=pct+'%';
 i++; if(i<=STEPS.length) setTimeout(tick, 12600+Math.random()*4800);
 else fill.style.width='94%';
}
tick();
</script>
</div>
"""

ANCHOR_URL = "https://www.booli.se/bostad/466517"
HAS_KEY = bool(os.getenv("ANTHROPIC_API_KEY"))
HAS_SID = bool(os.getenv("BOOLI_SID"))

st.set_page_config(page_title="Klarsyn", page_icon="🏠", layout="centered",
                   initial_sidebar_state="collapsed")

st.markdown("""
<style>
#MainMenu, footer, header {visibility:hidden;}
.block-container {padding-top:2.2rem; max-width:900px;}
html, body, [class*="css"] {font-family:-apple-system,'Segoe UI',Roboto,sans-serif;}
.klar-brand {font-size:30px; font-weight:800; letter-spacing:-1px; color:#0f766e;}
.klar-tag {color:#64748b; font-size:16px; margin:-4px 0 18px;}
.stButton>button {background:#0f766e; color:#fff; border:0; border-radius:10px;
  padding:.55rem 1.4rem; font-weight:700; font-size:15px;}
.stButton>button:hover {background:#115e59; color:#fff;}
div[data-testid="stTextInput"] input {border-radius:10px;}
.klar-step {color:#94a3b8; font-size:13px; font-weight:600; text-transform:uppercase;
  letter-spacing:1px;}
.run-meta {color:#64748b; font-size:13px;}
</style>
""", unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def run_pipeline(model_id: str, pdf_path: str, listing_override: dict | None,
                 booli_forening: dict | None = None):
    """Live pipeline: extract (chosen model) -> analyse -> report_input. Cached per
    (model, pdf path, listing, förening) so re-runs are instant."""
    t0 = time.time()
    try:
        parsed = E.extract(Path(pdf_path), model=model_id)
    finally:
        # The PDF is a transient artifact — once extraction has read it we no longer
        # need it (the parsed JSON is what everything downstream uses). Remove it even
        # if extraction failed, so a retry re-fetches a fresh copy.
        try:
            Path(pdf_path).unlink(missing_ok=True)
        except OSError:
            pass
    (DATA / "arsredovisning_parsed.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    result = AN.analyse(parsed, listing_override, model=model_id,
                        booli_forening=booli_forening)
    (DATA / "report_input.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    elapsed = time.time() - t0
    return result, {"mode": parsed.get("_extraction"),
                    "model": parsed.get("_model"), "elapsed": elapsed}


# ---------------- Landing / input ----------------
st.markdown('<div class="klar-brand">Klarsyn</div>', unsafe_allow_html=True)
st.markdown('<div class="klar-tag">Föreningens ekonomi och priset — granskat på minuter, '
            'i ett beslutsunderlag.</div>', unsafe_allow_html=True)

with st.container(border=True):
    st.markdown('<span class="klar-step">Klistra in en bostadsannons</span>',
                unsafe_allow_html=True)
    url = st.text_input("Länk till bostaden (Booli)", value=ANCHOR_URL,
                        label_visibility="collapsed", placeholder="https://www.booli.se/bostad/…")

    run = st.button("Analysera →", use_container_width=True)

    if not HAS_KEY:
        st.caption("⚠️ Analystjänsten är inte tillgänglig just nu — försök igen senare.")
    elif not HAS_SID:
        st.caption("⚠️ Ingen BOOLI_SID satt → kan inte hämta årsredovisningen automatiskt. "
                   "Lägg in `sid`-cookien från en inloggad booli.se-session.")
    else:
        st.caption("Klarsyn hämtar föreningens årsredovisning automatiskt från länken.")

# ---------------- Report ----------------
if run or st.session_state.get("shown"):
    st.session_state["shown"] = True
    # Model is fixed and never surfaced to the user (single source of truth in models.py).
    model_id = M.id_for(M.DEFAULT_LABEL)

    if not (url and "booli.se" in url):
        st.error("Klistra in en Booli-länk (t.ex. https://www.booli.se/bostad/…).")
        st.stop()

    # 1) Listing facts (price/m²/avgift) + the förening's Booli data, scraped from the URL.
    with st.spinner("Hämtar bostaden från Booli …"):
        scraped = fetch_booli(url) or {}
    lo = scraped.get("listing") or {}   # empty dict -> price shows "okänt", never demo leakage
    foren = scraped.get("forening") or {}
    listing_note = (f"📍 {lo.get('address') or '—'} · hämtad från Booli" if lo
                    else "⚠️ Kunde inte hämta bostadens uppgifter (fortsätter med föreningen).")

    # 2) Fetch the årsredovisning PDF + run the pipeline behind the working animation.
    think = st.empty()
    with think.container():
        components.html(THINKING_HTML, height=430)
    coop_id = lo.get("forening_id")  # reuse the id from the listing scrape → avoids a 2nd fetch
    pdf_path, fetch_err = fetch_pdf(url, coop_id)
    if pdf_path and not Path(pdf_path).exists():
        # Cached path points at a PDF that a previous run already cleaned up — re-fetch.
        fetch_pdf.clear()
        pdf_path, fetch_err = fetch_pdf(url, coop_id)
    if fetch_err == "AUTH":
        think.empty()
        st.error(
            "Booli-sessionen har gått ut. Logga in på booli.se igen, kopiera `sid`-cookien "
            "och uppdatera **BOOLI_SID** (lokalt i `.env`, eller under *Secrets* på "
            "Streamlit Cloud)."
        )
        st.stop()
    if not pdf_path:
        think.empty()
        st.error(
            "Kunde inte hämta årsredovisningen. Antingen saknar föreningen en "
            "årsredovisning på Booli, eller så gick hämtningen inte igenom — försök igen."
        )
        # Temporary debug surface: shows WHY the fetch failed (prod vs local). Remove once
        # the prod fetch is confirmed working.
        with st.expander("Teknisk info (debug)"):
            st.code(
                f"listing_scrape_ok = {bool(lo)}\n"
                f"forening_id       = {coop_id}\n"
                f"url               = {url}\n"
                f"error             = {fetch_err}"
            )
        st.stop()
    ri, meta = run_pipeline(model_id, pdf_path, lo, foren)
    html = render_html(ri)
    think.empty()

    st.caption(listing_note)
    components.html(html, height=3200, scrolling=True)
    st.download_button("⬇︎ Ladda ner rapporten (HTML)", data=html,
                       file_name="klarsyn-rapport.html", mime="text/html")
else:
    st.info("Klistra in en Booli-länk och tryck **Analysera** för en live-analys.")
