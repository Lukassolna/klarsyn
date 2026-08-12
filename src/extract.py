"""
Klarsyn — dynamic extraction layer (replaces brittle, layout-specific parsing).

Every årsredovisning has a different layout, wording and (often) broken encoding. Instead
of regex tuned to one HSB template, we hand the raw text to an LLM with a STRICT schema and
a clear, guided prompt, and let it map any layout onto our canonical contract.

Contract (same shape the rest of the pipeline consumes, so this is a drop-in):
    { "forening": {...}, "flerarsoversikt": {...}, "figures": {...},
      "underhall": {"historik": {year:[...]}, "komponenter": {...}} }

Modes:
  * LLM (default when ANTHROPIC_API_KEY is set): claude reads the whole AR text.
  * Fallback: the deterministic parser (parse_arsredovisning) — kept only as a safety net
    / offline demo, NOT as the primary path.

Design rules baked into the prompt:
  - Extract ONLY what is present. Never guess. Missing value -> null.
  - åäö may be garbled (U+FFFD); interpret by meaning, output clean Swedish.
  - Numbers as integers in the stated unit. No thousands separators.
  - Return ONLY valid JSON matching the schema.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
try:  # load ANTHROPIC_API_KEY from .env if present
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass
DEFAULT_PDF = ROOT / "exempel årsredvosning.pdf"
# default model; overridable per-call or via env (UI sets this)
MODEL = os.getenv("KLARSYN_MODEL", "claude-opus-4-8")

# --------------------------------------------------------------------------- #
# The canonical schema, described for the model. Keep this the single source of
# truth for what a Klarsyn extraction contains.
# --------------------------------------------------------------------------- #
SCHEMA_DOC = """
Returnera EXAKT detta JSON-objekt (samma nycklar, null där uppgift saknas):

{
  "forening": {
    "namn": string|null,
    "org_nr": string|null,
    "byggar": int|null,                       // byggnadsår
    "akta_forening": bool|null,               // äkta (privatbostadsföretag) = true
    "antal_lagenheter": int|null,
    "antal_lokaler": int|null,
    "total_boarea_m2": int|null,              // bostadsrättsyta (boyta)
    "tomtratt": bool|null,                     // true om tomträtt, false om friköpt
    "markinnehav": string|null                 // t.ex. "friköpt" eller "tomträtt"
  },
  "flerarsoversikt": {                         // 5-årsöversikten, nyaste året FÖRST
    "years": [string, ...],                    // t.ex. ["2024/2025", ...]
    "skuldsattning_kr_kvm": [int|null, ...],
    "sparande_kr_kvm": [int|null, ...],
    "rantekanslighet_pct": [number|null, ...],
    "arsavgift_kr_kvm": [int|null, ...],
    "energikostnad_kr_kvm": [int|null, ...],
    "resultat_efter_finansiella_tkr": [int|null, ...],
    "soliditet_pct": [number|null, ...]
  },
  "figures": {                                 // senaste årets belopp i hela kronor
    "arets_resultat": int|null,
    "nettoomsattning": int|null,
    "eget_kapital": int|null,
    "underhallsfond": int|null,
    "langfr_kreditinstitut": int|null,         // långfristiga skulder till kreditinstitut
    "kortfr_kreditinstitut": int|null,         // kortfristiga skulder till kreditinstitut
    "summa_skulder": int|null,
    "likvida_medel_slut": int|null,
    "kassaflode_lopande": int|null,            // kassaflöde från löpande verksamheten
    "rantekostnad_lan": int|null
  },
  "lan": [                                      // varje lån om det redovisas, annars []
    {"langivare": string|null, "belopp": int|null, "ranta_pct": number|null,
     "bindning_till": string|null}
  ],
  "underhall": {
    "historik": { "<år>": [string, ...] },     // år -> lista av genomförda åtgärder
    "planerat": [ {"ar": int|null, "atgard": string, "belopp": int|null} ],
    "komponenter": {                           // klassificera historiken per komponent
      "<Komponentnamn>": { "senaste_ar": int, "handelser": [ {"year": int, "text": string} ] }
    }
  }
}

Komponentnamn att använda (utelämna de som saknas i historiken):
"Stammar / VVS (avlopp & vatten)", "Tak", "Fasad", "Balkonger", "Fönster", "Hiss",
"Ventilation (OVK/fläktar)", "Värme / undercentral", "El (IMD/laddning)".
Ett fullständigt stambyte (byte/relining av avlopps- eller vattenstammar) ska hamna under
"Stammar / VVS" — men skilj på det och enklare åtgärder som stamventiler eller kulvertbyte.
"""

SYSTEM = (
    "Du är en noggrann extraktionsmotor för svenska bostadsrättsföreningars "
    "årsredovisningar. Du läser rå, ibland trasig text (åäö kan vara förvanskade till "
    "'�') och mappar innehållet till ett strikt JSON-schema. Du hanterar VILKEN "
    "layout eller formulering som helst — HSB, SBC, Riksbyggen, egen förvaltning. "
    "Regler: (1) Extrahera BARA det som faktiskt står i dokumentet. Gissa aldrig. "
    "Saknas en uppgift → null. (2) Tal som heltal i angiven enhet, utan tusentalsavgränsare. "
    "(3) Tolka förvanskade tecken utifrån betydelse och skriv ren svenska i textfält. "
    "(4) Svara ENBART med giltig JSON enligt schemat — ingen förklaring, ingen markdown."
)

USER_TEMPLATE = """Här är den råa texten ur en årsredovisning (alla sidor):

<arsredovisning>
{ar_text}
</arsredovisning>

{schema}

Returnera JSON-objektet nu."""


def read_pdf_pages(pdf_path: Path) -> list[str]:
    """Extract text per page (empty string for pages with no text layer)."""
    with pdfplumber.open(pdf_path) as pdf:
        return [pg.extract_text() or "" for pg in pdf.pages]


def _pages_to_text(pages: list[str]) -> str:
    return "\n".join(f"--- Sida {i} ---\n{t}" for i, t in enumerate(pages, 1))


def read_pdf_text(pdf_path: Path) -> str:
    return _pages_to_text(read_pdf_pages(pdf_path))


def looks_scanned(pages: list[str]) -> bool:
    """True when the PDF has essentially no text layer (a scanned/image AR).

    A text-based årsredovisning yields hundreds–thousands of characters per page; a
    scanned one yields ~nothing. We route the latter to vision extraction instead.
    """
    n = len(pages)
    if n == 0:
        return True
    real_chars = sum(len(t.strip()) for t in pages)
    return real_chars < 50 * n  # avg < 50 chars/page → no usable text layer


def build_messages(ar_text: str):
    user = USER_TEMPLATE.format(ar_text=ar_text, schema=SCHEMA_DOC)
    return SYSTEM, user


def extract_llm(ar_text: str, model: str | None = None) -> dict:
    import anthropic
    client = anthropic.Anthropic()
    system, user = build_messages(ar_text)
    msg = client.messages.create(
        model=model or MODEL,
        max_tokens=6000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    # tolerate a stray code fence if the model adds one
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1].lstrip("json").strip()
    return json.loads(raw)


def extract_llm_vision(pdf_path: Path, model: str | None = None) -> dict:
    """Vision extraction for scanned/image PDFs with no text layer.

    Sends the PDF itself to Claude as a base64 `document` block (Claude renders and reads
    the pages), running the same canonical schema against what it sees. No text layer needed.
    """
    import base64
    import anthropic
    client = anthropic.Anthropic()
    pdf_b64 = base64.standard_b64encode(Path(pdf_path).read_bytes()).decode("ascii")
    user_text = (
        "Här är en årsredovisning som en INSKANNAD PDF (sidorna är bilder, ingen "
        "maskinläsbar text). Läs sidorna visuellt och extrahera uppgifterna.\n\n"
        + SCHEMA_DOC + "\n\nReturnera JSON-objektet nu."
    )
    msg = client.messages.create(
        model=model or MODEL,
        max_tokens=6000,
        system=SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
            {"type": "text", "text": user_text},
        ]}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1].lstrip("json").strip()
    return json.loads(raw)


def extract(pdf_path: Path = DEFAULT_PDF, force_fallback: bool = False,
            model: str | None = None) -> dict:
    """Primary entry point. LLM extraction when a key is present, else deterministic.

    Text-layer PDFs go through the cheap/fast text path; scanned/image PDFs (no text) are
    routed to vision extraction so we still read them instead of returning all-nulls.
    """
    if not force_fallback and os.getenv("ANTHROPIC_API_KEY"):
        try:
            pages = read_pdf_pages(pdf_path)
            if looks_scanned(pages):
                sys.stderr.write("[info] PDF has no text layer (scanned) — using vision "
                                 "extraction.\n")
                data = extract_llm_vision(pdf_path, model=model)
                data["_extraction"] = "llm-vision"
            else:
                data = extract_llm(_pages_to_text(pages), model=model)
                data["_extraction"] = "llm"
            data["_model"] = model or MODEL
            return data
        except Exception as e:
            sys.stderr.write(f"[warn] LLM extraction failed ({e}); using deterministic.\n")
    import parse_arsredovisning as P
    data = P.parse(pdf_path)
    data["_extraction"] = "deterministic-fallback"
    return data


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--prompt" in sys.argv:
        # print the exact guided prompt (for review / iteration) without calling the API
        _, user = build_messages("<AR-TEXT HÄR>")
        print("SYSTEM:\n" + SYSTEM + "\n\nUSER:\n" + user)
    else:
        out = extract(force_fallback="--fallback" in sys.argv)
        print(f"[extraction mode: {out.get('_extraction')}]")
        print(json.dumps({k: out[k] for k in out if k != "_extraction"},
                         ensure_ascii=False, indent=2)[:1500])
