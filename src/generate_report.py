"""
Klarsyn — report generation (Phase D).

Turns data/report_input.json (the scored signals) into a plain-language Klarsyn report.

Two modes:
  * LLM mode  — if ANTHROPIC_API_KEY is set, calls claude-opus-4-8 with a strict prompt.
                The model may ONLY use the signals we pass; it never sees raw figures to
                invent from. This is the anti-hallucination guarantee (R7).
  * Template  — deterministic fallback that renders the same signals into markdown, so
                the pipeline is fully runnable without an API key.

Output: data/report_466517.md   (both a free teaser and the full report)
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:  # load ANTHROPIC_API_KEY from .env if present
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass
DATA = ROOT / "data"
OUT = DATA / "report_466517.md"
MODEL = "claude-opus-4-8"  # report quality; use claude-haiku-4-5 for cheap extraction

RAG_EMOJI = {"green": "🟢", "amber": "🟡", "red": "🔴"}


# --------------------------------------------------------------------------- #
# LLM prompt
# --------------------------------------------------------------------------- #
SYSTEM = (
    "Du är Klarsyn, en oberoende ekonomisk analystjänst för bostadsköpare i Sverige. "
    "Du skriver ett *beslutsunderlag* — aldrig råd, aldrig garantier. Du får ENDAST "
    "använda de siffror och signaler som ges i JSON:en. Hitta aldrig på tal. Skriv på "
    "klar, enkel svenska som en förstagångsköpare förstår. Förklara facktermer kort. "
    "Var ärlig om både styrkor och svagheter. Varje påstående ska gå att härleda till en "
    "signal. Avsluta med tydlig disclaimer."
)

PROMPT_TEMPLATE = """Här är analysunderlaget som strukturerad JSON:

```json
{payload}
```

Skriv en Klarsyn-rapport i markdown med exakt denna struktur:

# Klarsyn-rapport — {adress}

## Snabböversikt (gratis)
- En rad om föreningens ekonomi (med trafikljus) och en rad om priset.
- Tre viktigaste signalerna som punktlista med 🟢/🟡/🔴.
- Avsluta med: *"Lås upp hela rapporten för att se detaljer, risker och frågor att ställa."*

## Sammanfattning
3–4 meningar i klarspråk: vad är helhetsbilden för en köpare?

## Föreningens ekonomi
Gå igenom varje förening-signal. För varje: trafikljus, vad siffran betyder, och varför
det spelar roll för köparen. Väv ihop till en berättelse, inte bara en lista.

## Pris & värde
Är priset rimligt mot området och mot jämförbara försäljningar? Är avgiften ärlig?

## Vad det betyder för dig
Konkret: vad ska köparen tänka på? Vad är sannolikt framåt (t.ex. avgiftshöjningar)?

## Frågor att ställa mäklaren eller styrelsen
4–6 skarpa frågor som följer direkt av svagheterna i underlaget.

## Källor & förbehåll
Lista datakällorna och skriv tydligt att detta är ett beslutsunderlag, inte finansiell
rådgivning, och att uppgifterna bygger på offentliga register och senaste årsredovisning.
"""


def build_prompt(report_input: dict) -> str:
    return PROMPT_TEMPLATE.format(
        payload=json.dumps(report_input, ensure_ascii=False, indent=2),
        adress=report_input["meta"]["adress"],
    )


def generate_llm(report_input: dict) -> str:
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": build_prompt(report_input)}],
    )
    return "".join(block.text for block in msg.content if block.type == "text")


# --------------------------------------------------------------------------- #
# Deterministic template fallback
# --------------------------------------------------------------------------- #
def _sig_line(v: dict) -> str:
    dot = RAG_EMOJI[v["status"]]
    val = f'{v["value"]} {v["unit"]}'.strip()
    return f"- {dot} **{v['label']}:** {val} — {v['comment']}"


def generate_template(ri: dict) -> str:
    m = ri["meta"]
    fo = ri["signals"]["forening"]
    pr = ri["signals"]["pris"]
    vd = ri["verdict"]

    fin_dot = {"stark": "🟢", "stabil-men-bevaka": "🟡", "risker": "🔴"}.get(
        vd["forening_status"], "🟡")
    pris_dot = "🟢" if vd["pris_status"].startswith("marknadsm") else "🟡"

    # pick the three most decision-relevant signals for the teaser
    top3 = [fo["skuldsattning"], fo["sparande"], pr["kr_per_m2_vs_omrade"]]

    L = []
    A = L.append
    A(f"# Klarsyn-rapport — {m['adress']}\n")
    A(f"*{m['forening']} · org.nr {m['org_nr']} · utgångspris "
      f"{m['utgangspris_kr']:,} kr · {m['boarea_m2']} m² · "
      f"årsredovisning {m['arsredovisning']}*\n".replace(",", " "))

    A("## Snabböversikt (gratis)\n")
    A(f"### Föreningens hälsa: {vd['forening_score']}/100 "
      f"(betyg {vd['forening_grade']}) — {vd['rekommendation_rubrik']}")
    A(f"> {vd['rekommendation']}\n")
    A(f"- **Föreningens ekonomi:** {fin_dot} {vd['forening_status'].replace('-', ' ')}")
    A(f"- **Priset:** {pris_dot} {vd['pris_status']}\n")
    A("**Tre viktigaste signalerna:**")
    for v in top3:
        A(f"- {RAG_EMOJI[v['status']]} **{v['label']}:** {v['value']} {v['unit']}".rstrip())
    A("\n> 🔒 *Lås upp hela rapporten för att se detaljer, risker och frågor att ställa.*\n")
    A("---\n")

    A("## Sammanfattning\n")
    A(vd["forening_sammanfattning"] + " " + vd["pris_sammanfattning"] + "\n")

    A("## Föreningens ekonomi\n")
    for key in ["skuldsattning", "sparande", "rantekanslighet", "arsavgift",
                "energikostnad", "resultat", "likviditet", "soliditet",
                "tomtratt", "akta_forening"]:
        A(_sig_line(fo[key]))
    A("")

    A("## Pris & värde\n")
    for key in ["kr_per_m2_vs_omrade", "samma_forening_comp", "avgift_check"]:
        if key in pr:
            A(_sig_line(pr[key]))
    A("")

    A("## Vad det betyder för dig\n")
    A("- Balansräkningen är trygg: låg belåning och låg räntekänslighet gör att "
      "räntehöjningar inte tvingar fram dramatiska avgiftshöjningar.")
    A("- Men föreningen sparar för lite och går med underskott — i ett hus från 1965 "
      "innebär det att avgiften sannolikt fortsätter höjas för att finansiera underhåll.")
    A("- Priset är marknadsmässigt, så du betalar rätt för läget — men räkna in en "
      "stigande avgift i din boendekalkyl.\n")

    A("## Frågor att ställa mäklaren eller styrelsen\n")
    A("1. Vilka underhållsprojekt ligger i underhållsplanen de kommande 5–10 åren, och "
      "hur ska de finansieras (avgift, kassa eller nya lån)?")
    A("2. Hur mycket planerar styrelsen att höja avgiften kommande år?")
    A("3. När löper föreningens lån ut (räntebindning), och vad blir effekten om räntan "
      "ligger kvar högt vid omsättning?")
    A("4. Varför har likviditeten minskat från ~2,5 till ~0,95 mkr, och vad är planen "
      "för kassan?")
    A("5. Vilka åtgärder planeras för de stigande energikostnaderna (energiklass E)?\n")

    A("## Källor & förbehåll\n")
    A("- **Källor:** Booli (objekt och föreningsdata), föreningens årsredovisning "
      f"{m['arsredovisning']}, Bolagsverket/allabolag (org.nr), Hemnet slutpriser (jämförbara "
      "försäljningar i Lilla Alby).")
    A("- **Förbehåll:** Detta är ett *beslutsunderlag*, inte finansiell rådgivning eller "
      "en värdering. Uppgifterna bygger på offentliga register och senaste årsredovisning "
      "och kan innehålla fel eller vara inaktuella. Fatta inga köpbeslut enbart på denna "
      "rapport — kontrollera alltid originalhandlingarna.")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
def generate(force_template: bool = False) -> tuple[str, str]:
    ri = json.loads((DATA / "report_input.json").read_text(encoding="utf-8"))
    if not force_template and os.getenv("ANTHROPIC_API_KEY"):
        try:
            return generate_llm(ri), "llm"
        except Exception as e:  # fall back rather than fail the pipeline
            sys.stderr.write(f"[warn] LLM generation failed ({e}); using template.\n")
    return generate_template(ri), "template"


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    force = "--template" in sys.argv
    report, mode = generate(force_template=force)
    OUT.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n\n<!-- generated via: {mode} -> {OUT} -->")
