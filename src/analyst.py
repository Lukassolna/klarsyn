"""
Klarsyn — AI analyst (replaces the hardcoded scoring in analyze.py).

ALL judgement lives in the prompt, not in Python. Given the raw extracted förening data +
the scraped listing, one LLM call returns the full assessment: score, grade, per-signal
RAG status + plain-language comment, recommendation, maintenance read, buyer implications
and questions. Domain benchmarks are GUIDANCE in the prompt — the model reasons with them,
it isn't bound by Python thresholds. This scales to any förening/layout/edge case.

Python only does plumbing:
  * pass the EXACT extracted numbers to the model (so figures are never hallucinated),
  * attach those numbers back onto the model's judgement,
  * pass-through raw data (5-yr table, maintenance history) for the premium section.

Output: the report_input.json structure consumed by report_html.py.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass
DATA = ROOT / "data"
MODEL = os.getenv("KLARSYN_MODEL", "claude-sonnet-4-6")

# --- plumbing: where each signal's raw value/label lives (data mapping, not judgement) ---
FLER = {  # signal key -> (flerårsöversikt field, unit)
    "skuldsattning": ("skuldsattning_kr_kvm", "kr/m²"),
    "sparande": ("sparande_kr_kvm", "kr/m²"),
    "rantekanslighet": ("rantekanslighet_pct", "%"),
    "arsavgift": ("arsavgift_kr_kvm", "kr/m²"),
    "energikostnad": ("energikostnad_kr_kvm", "kr/m²"),
    "resultat": ("resultat_efter_finansiella_tkr", "tkr"),
    "soliditet": ("soliditet_pct", "%"),
}
LABELS = {
    "skuldsattning": "Skuldsättning (lån per m²)",
    "sparande": "Sparande (kassaflöde för underhåll, per m²)",
    "rantekanslighet": "Räntekänslighet",
    "arsavgift": "Årsavgift per m²",
    "energikostnad": "Energikostnad per m²",
    "resultat": "Resultat efter finansiella poster",
    "soliditet": "Soliditet",
    "likviditet": "Likvida medel",
    "tomtratt": "Markinnehav",
    "akta_forening": "Föreningstyp",
}


_FLAT = {"dir": "flat", "pct": 0.0, "years": 0, "from": None, "to": None, "pct_ok": False}


def _trend(series):
    """Describe a flerårsöversikt series (newest first).

    Returns dict with direction, % change, the real number of years spanned, and pct_ok —
    which is False when the metric crosses/starts at zero (e.g. sparande +67 → -58), because
    a percentage is meaningless there. The renderer then shows the from→to values instead.
    """
    vals = [v for v in (series or []) if v is not None]
    if len(vals) < 2:
        return dict(_FLAT, years=len(vals))
    new, old = vals[0], vals[-1]
    years = len(vals)  # number of reporting years underlying the trend
    direction = "up" if new > old else "down" if new < old else "flat"
    # % change is only meaningful for a strictly positive metric that stays positive.
    pct_ok = old > 0 and new > 0
    pct = round((new - old) / abs(old) * 100, 1) if old != 0 else 0.0
    return {"dir": direction, "pct": pct, "years": years,
            "from": old, "to": new, "pct_ok": pct_ok}


# --------------------------------------------------------------------------- #
# The analyst prompt — this is where ALL domain judgement lives.
# --------------------------------------------------------------------------- #
SYSTEM = """Du är Klarsyns senioranalytiker — en oberoende expert på bostadsrättsföreningars
ekonomi och bostadspriser i Sverige. Du gör ett *beslutsunderlag* för en bostadsköpare,
aldrig rådgivning eller garanti.

Du får strukturerad data (redan uttolkad ur en årsredovisning + bostadsannons). Bedöm den
med sunt omdöme. Använd riktmärkena nedan som VÄGLEDNING, inte som stelbenta regler —
väg samman helheten, förklara nyanser (t.ex. att ett negativt resultat ofta beror på icke-
likviditetspåverkande avskrivningar), och anpassa dig till vad datan faktiskt visar.

Riktmärken (vägledande):
- Skuldsättning/belåning: <6 000 kr/m² lågt · 6 000–10 000 förhöjt · 10 000–15 000 högt ·
  >15 000 mycket högt. Trend spelar roll.
- Sparande (kassaflöde för underhåll): >150 kr/m² bra · 50–150 svagt · <0 tär på kassan.
- Räntekänslighet: ≤5 % lågt · 6–10 % måttligt · >10 % högt (avgiftshöjning vid +1 %-enhet).
- Årsavgift: 600–800 kr/m² normalt · >800 förhöjt. Snabb ökning är en varningssignal.
- Energikostnad: bedöm nivå och trend; kraftig ökning pressar avgiften.
- Resultat efter finansiella poster: återkommande underskott = bevaka, men förklara
  avskrivningarnas roll; väg in kassaflödet från driften om det finns.
- Soliditet: har begränsad relevans i en BRF; nämn det. Förbättring är svagt positivt.
- Kortfristiga vs långfristiga banklån: i svenska BRF:er bokförs lån vars räntebindning löper
  ut inom 12 månader som *kortfristiga* även om de i praktiken är långsiktiga (en normal
  räntetrappa). En hög andel kortfristiga lån är därför INTE i sig ett larm — bedöm
  refinansieringsrisken utifrån helheten (räntenivå, spridning på bindningstider, om lånen
  faktiskt förfaller). Beskriv det som en punkt att utreda, inte en akut risk, om inget annat
  tyder på problem.
- Likvida medel: sätt i relation till årsintäkterna (netto­omsättning) — buffert för drift/underhåll.
- Markinnehav: tomträtt = återkommande, ofta kraftigt stigande avgäld (risk). Friköpt = tryggt.
- Föreningstyp: oäkta förening kan ge sämre skatte-/lånevillkor för köparen; äkta = normalt.
- Underhåll: ett genomfört fullständigt stambyte (byte/relining av avlopps-/vattenstammar) är
  den enskilt största framtida kostnaden — mycket positivt om avklarat. Bedöm varje storkomponent
  (tak, fasad, fönster, hiss, balkonger, ventilation, värme, el) utifrån hur längesedan den
  åtgärdades och rimlig livslängd. Flagga åldrande komponenter.
- Pris: jämför kr/m² mot områdessnitt om det finns (±7 % = marknadsmässigt). Låg avgift + lågt
  pris kan dölja en dyr bostad; förhöjd avgift kan motivera lägre pris.
- Booli-värdering: om `booli_estimate` finns (Boolis egen värdering `varde_kr` med intervall
  `low_kr`–`high_kr`) är den din främsta prisreferens — särskilt när områdessnitt saknas.
  Jämför utgångspriset mot värderingen och intervallet: inom intervallet = marknadsmässigt;
  tydligt över `high_kr` eller under `low_kr` = avvikande (säg åt vilket håll och ungefär hur
  mycket). När en värdering finns ska `pris.status` vara "marknadsmässigt" eller "avvikande" —
  aldrig "okänt". Påminn kort om att en värdering är en modellskattning, inte en garanti. Om
  varken värdering eller områdesdata finns, säg att marknadsjämförelse saknas — gissa aldrig.
- Korskontroll mot Booli: `booli_forening` är Boolis egen tolkning av SAMMA årsredovisning
  (byggår, lån, belåning/kvm, äkta/oäkta, markägande). Använd den som en oberoende
  rimlighetskontroll av dina egna extraherade siffror. Om något tydligt strider mot
  årsredovisningsdatan, nämn avvikelsen kort — hitta inte på, flagga bara osäkerheten.
- Avgift (två tal): annonsens avgift/m² beräknas på lägenhetens yta, medan årsredovisningens
  avgift/m² beräknas på föreningens totala yta — de kan därför skilja sig. Bedöm avgiftsnivån i
  prisdelen (`avgift_status`) KONSEKVENT med hur du bedömer årsavgiften i föreningsdelen; motsäg
  dig inte. Förklara kort i `avgift_comment` om de två talen skiljer sig.

Sätt status "green" (bra), "amber" (bevaka) eller "red" (risk) för varje post utifrån helheten.
Sätt ett helhetsbetyg 0–100 för föreningens ekonomiska hälsa och en bokstav A(≥80)/B(60–79)/
C(40–59)/D(<40). Skriv i klarspråk som en förstagångsköpare förstår. Hitta ALDRIG på siffror —
använd bara de tal som ges. Håll sammanfattningarna korta och vardagliga — premium-omdömet
(är det värt att köpa?) får däremot vara längre, ärligare och mer detaljerat, eftersom köparen
betalar för just den slutsatsen. Svara ENBART med giltig JSON enligt schemat."""

USER_TEMPLATE = """Underlag (JSON):
```json
{payload}
```

Returnera EXAKT detta JSON-objekt:
{{
  "score": <int 0-100>,
  "grade": "A|B|C|D",
  "forening_status": "<kort etikett, t.ex. 'stark' / 'stabil men bevaka' / 'blandad' / 'risker'>",
  "rekommendation_rubrik": "<kort, t.ex. 'Gå vidare — men ställ frågorna'>",
  "rekommendation": "<2-3 meningar; väg in priset om områdesdata finns>",
  "rekommendation_hojdpunkt": "<EN kort mening om vad som sticker ut mest (positivt ELLER negativt), inled med en passande emoji — t.ex. '🏗️ Nybyggt hus (2015) med aktivt underhåll' eller '⚠️ Hela låneskulden är kortfristig'>",
  "forening_sammanfattning": "<2-3 KORTA meningar i vardaglig ton — som du förklarar för en vän, inte som en myndighet. Undvik stelt/byråkratiskt språk och långa inskott. Lyft det viktigaste plus den största risken.>",
  "signals": {{
    "skuldsattning": {{"status": "...", "comment": "..."}},
    "sparande": {{"status": "...", "comment": "..."}},
    "rantekanslighet": {{"status": "...", "comment": "..."}},
    "arsavgift": {{"status": "...", "comment": "..."}},
    "energikostnad": {{"status": "...", "comment": "..."}},
    "resultat": {{"status": "...", "comment": "..."}},
    "soliditet": {{"status": "...", "comment": "..."}},
    "likviditet": {{"status": "...", "comment": "..."}},
    "tomtratt": {{"status": "...", "comment": "...", "value": "Friköpt|Tomträtt"}},
    "akta_forening": {{"status": "...", "comment": "...", "value": "Äkta bostadsrättsförening|Oäkta förening"}}
  }},
  "pris": {{
    "status": "marknadsmässigt|avvikande|okänt",
    "sammanfattning": "<1-2 meningar; väg in Booli-värderingen om den finns>",
    "kr_per_m2_comment": "<kommentar om priset per m² (och ev. områdesjämförelse)>",
    "vardering_comment": "<utgångspris jämfört med Booli-värderingen och intervallet, om värdering finns — annars tom sträng>",
    "avgift_status": "green|amber|red",
    "avgift_comment": "<kommentar om avgiftsnivån och vad den betyder för priset>"
  }},
  "underhall": {{
    "sammanfattning": "<1 mening, lyft stambyte-status>",
    "stambyte_gjort": <true|false>,
    "stambyte_ar": <int|null>,
    "komponenter": {{ "<komponentnamn exakt som i datan>": {{"status": "...", "kommentar": "..."}} }},
    "flaggor": ["<åldrande komponent — kort>", ...]
  }},
  "premium": {{
    "omdome_rubrik": "<kort slutomdöme i klartext, t.ex. 'Bra köp – med öppna ögon' / 'Köp med försiktighet' / 'Avvakta'>",
    "koprekommendation": "<4-6 meningar: din ärliga, detaljerade bedömning av OM det är värt att köpa. Väg samman ekonomi, underhåll, räntekänslighet, likviditet och (om möjligt) priset. Var nyanserad och våga ta ställning — t.ex. 'det kan vara ett bra köp om du är medveten om riskerna och har marginal för en avgiftshöjning'. Det här är vad köparen betalar för — gör det konkret och värt pengarna, inte generiskt.>",
    "sticker_ut": ["<konkret styrka som gör föreningen/bostaden attraktiv — kort>", ...],
    "alarmerande": ["<det mest alarmerande köparen MÅSTE vara medveten om — rakt och konkret, inga svepande formuleringar>", ...],
    "slutsats": "<EN kraftfull avslutande mening som knyter ihop hela analysen — t.ex. 'Solid förening — men stambytesfrågan måste besvaras innan du skriver på'>"
  }},
  "implications": ["<vad det betyder för köparen — punkt>", ...],
  "questions": ["<skarp fråga till mäklare/styrelse som följer av svagheterna>", ...]
}}"""


def build_messages(payload: dict):
    return SYSTEM, USER_TEMPLATE.format(payload=json.dumps(payload, ensure_ascii=False, indent=2))


def _call_llm(payload: dict, model: str | None) -> dict:
    import anthropic
    client = anthropic.Anthropic()
    system, user = build_messages(payload)
    # 8000 tokens: the full JSON (all signals + premium + price cross-check) runs long; 4000
    # truncated it mid-string → JSONDecodeError. Guard explicitly so a future overflow gives a
    # clear message instead of a cryptic parse error.
    msg = client.messages.create(model=model or MODEL, max_tokens=8000,
                                 system=system, messages=[{"role": "user", "content": user}])
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(
            "Analysen blev för lång och klipptes av (max_tokens). Höj max_tokens i analyst.py.")
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1].lstrip("json").strip()
    return json.loads(raw)


def analyse(ar: dict, listing: dict | None, model: str | None = None,
            booli_forening: dict | None = None) -> dict:
    """ar = extracted årsredovisning; listing = scraped Booli fields (or None);
    booli_forening = Booli's own parse of the förening (summary + economy) for cross-check."""
    fo = ar.get("flerarsoversikt", {})
    fig = ar.get("figures", {})
    forening = ar.get("forening", {})
    underhall_raw = ar.get("underhall", {})
    listing = listing or {}
    booli_forening = booli_forening or {}

    # Build the compact payload the analyst reasons over (exact numbers only).
    payload = {
        "forening": forening,
        "flerarsoversikt": fo,
        "figures": fig,
        "lan": ar.get("lan") or [],  # individual loans (räntebindning) → informs refi risk
        "underhall_komponenter": {k: v.get("senaste_ar")
                                  for k, v in (underhall_raw.get("komponenter") or {}).items()},
        "underhall_historik": underhall_raw.get("historik", {}),
        "underhall_planerat": underhall_raw.get("planerat", []),
        "listing": {k: listing.get(k) for k in
                    ("address", "boarea_m2", "utgangspris_kr", "kr_per_m2",
                     "avgift_kr_man", "driftkostnad_kr_man", "byggar", "omrade",
                     "vaning", "antal_vaningar")},
        # Booli's own valuation of THIS apartment (värdering + low/high band) — the price anchor.
        "booli_estimate": listing.get("booli_estimate"),
        # Booli's independent read of the SAME förening from the AR — a hallucination cross-check.
        "booli_forening": {k: booli_forening.get(k) for k in
                           ("sammanfattning", "ekonomi", "belaning_per_kvm_kr",
                            "beskrivning")} if booli_forening else None,
        "omrade_snitt_kr_m2": listing.get("omrade_snitt_kr_m2"),
    }

    a = _call_llm(payload, model)

    # ---- assemble report_input by attaching EXACT values to the AI's judgement ----
    signals = {"forening": {}, "pris": {}}
    for key, (field, unit) in FLER.items():
        series = fo.get(field) or []
        s = a["signals"].get(key, {})
        signals["forening"][key] = {
            "label": LABELS[key], "value": series[0] if series else None, "unit": unit,
            "status": s.get("status", "amber"), "trend": _trend(series),
            "comment": s.get("comment", ""),
        }
    # likviditet (from figures)
    s = a["signals"].get("likviditet", {})
    signals["forening"]["likviditet"] = {
        "label": LABELS["likviditet"], "value": fig.get("likvida_medel_slut"), "unit": "kr",
        "status": s.get("status", "amber"), "trend": dict(_FLAT), "comment": s.get("comment", ""),
    }
    # categorical signals (value comes from the AI's reading of the extracted flag)
    for key in ("tomtratt", "akta_forening"):
        s = a["signals"].get(key, {})
        signals["forening"][key] = {
            "label": LABELS[key], "value": s.get("value", "—"), "unit": "",
            "status": s.get("status", "amber"), "trend": dict(_FLAT), "comment": s.get("comment", ""),
        }

    # price signals
    ask = listing.get("kr_per_m2")
    if ask is None and listing.get("utgangspris_kr") and listing.get("boarea_m2"):
        ask = round(listing["utgangspris_kr"] / listing["boarea_m2"])
    ap = a.get("pris", {})

    # Booli-värdering card — the price verdict. Diff/band membership are pure math (kept in
    # Python so the number is never hallucinated); the nuance comes from the AI's comment.
    est = listing.get("booli_estimate") or {}
    ask_total = listing.get("utgangspris_kr")
    within_band = None
    if est.get("varde_kr") and ask_total:
        varde, low, high = est["varde_kr"], est.get("low_kr"), est.get("high_kr")
        diff_pct = round((ask_total - varde) / varde * 100, 1)
        within_band = (low is None or ask_total >= low) and (high is None or ask_total <= high)
        rikt = "över" if diff_pct > 0 else "under" if diff_pct < 0 else "i linje med"
        band = f" (Boolis intervall {low:,}–{high:,} kr)".replace(",", " ") if low and high else ""
        default_c = (f"Utgångspriset ligger {abs(diff_pct):.0f} % {rikt} Boolis värdering på "
                     f"{varde:,} kr".replace(",", " ") + band + ". Värderingen är en "
                     "modellskattning, inte en garanti.")
        signals["pris"]["booli_vardering"] = {
            "label": "Utgångspris vs Booli-värdering", "value": ask_total, "unit": "kr",
            "status": "green" if within_band else "amber",
            "comment": ap.get("vardering_comment") or default_c,
            "vardering_kr": varde, "low_kr": low, "high_kr": high, "diff_pct": diff_pct,
        }

    if ask:
        signals["pris"]["kr_per_m2_vs_omrade"] = {
            "label": "Pris per m²", "value": ask, "unit": "kr/m²",
            "omrade_snitt": listing.get("omrade_snitt_kr_m2"),
            "diff_pct": None, "status": ap.get("status", "okänt"),
            "benchmark": "", "comment": ap.get("kr_per_m2_comment", ""),
        }
    if listing.get("avgift_kr_man") and listing.get("boarea_m2"):
        avgift_m2 = round(listing["avgift_kr_man"] * 12 / listing["boarea_m2"])
        # Judged by the analyst; fall back to the föreningens årsavgift status so the price
        # section can never contradict the förening section (no more green-vs-amber on the fee).
        avgift_status = (ap.get("avgift_status")
                         or a["signals"].get("arsavgift", {}).get("status") or "amber")
        signals["pris"]["avgift_check"] = {
            "label": "Avgiftsnivå (påverkar priset)", "value": avgift_m2, "unit": "kr/m²/år",
            "status": avgift_status, "benchmark": "", "comment": ap.get("avgift_comment", ""),
        }

    # maintenance: attach the AI's per-component status onto the extracted years/events
    komp_out = []
    komp_ai = (a.get("underhall") or {}).get("komponenter", {})
    for comp, info in (underhall_raw.get("komponenter") or {}).items():
        ai = komp_ai.get(comp, {})
        komp_out.append({
            "komponent": comp, "senaste_ar": info.get("senaste_ar"),
            "alder": (2026 - info["senaste_ar"]) if info.get("senaste_ar") else None,
            "status": ai.get("status", "amber"),
            "kommentar": ai.get("kommentar") or (info.get("handelser") or [{}])[-1].get("text", ""),
        })

    signals["underhall"] = {
        "sammanfattning": (a.get("underhall") or {}).get("sammanfattning", ""),
        "stambyte_gjort": (a.get("underhall") or {}).get("stambyte_gjort", False),
        "stambyte_ar": (a.get("underhall") or {}).get("stambyte_ar"),
        "komponenter": komp_out,
        "flaggor": (a.get("underhall") or {}).get("flaggor", []),
        "historik": underhall_raw.get("historik", {}),
        "implications": a.get("implications", []),
        "questions": a.get("questions", []),
    }

    # A Booli valuation means the price half is no longer "okänt" even if the model hedged.
    pris_status = ap.get("status") or "okänt"
    if pris_status == "okänt" and within_band is not None:
        pris_status = "marknadsmässigt" if within_band else "avvikande"

    return {
        "meta": {
            "adress": listing.get("address") or "—",
            "forening": forening.get("namn") or "—",
            "org_nr": forening.get("org_nr") or "—",
            "utgangspris_kr": listing.get("utgangspris_kr"),
            "boarea_m2": listing.get("boarea_m2"),
            "arsredovisning": (fo.get("years") or ["—"])[0],
        },
        "signals": signals,
        "verdict": {
            "forening_score": a.get("score"), "forening_grade": a.get("grade"),
            "rekommendation_rubrik": a.get("rekommendation_rubrik", ""),
            "rekommendation": a.get("rekommendation", ""),
            "rekommendation_hojdpunkt": a.get("rekommendation_hojdpunkt", ""),
            "forening_status": a.get("forening_status", ""),
            "forening_sammanfattning": a.get("forening_sammanfattning", ""),
            "pris_status": pris_status,
            "pris_sammanfattning": ap.get("sammanfattning", ""),
        },
        "detaljer": {"flerarsoversikt": fo, "figures": fig},
        "premium": a.get("premium", {}) or {},
        "_analyst_model": model or MODEL,
    }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ar = json.loads((DATA / "arsredovisning_parsed.json").read_text(encoding="utf-8"))
    out = analyse(ar, None)
    (DATA / "report_input.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    print(f"score {out['verdict']['forening_score']} · {out['verdict']['rekommendation_rubrik']}")
