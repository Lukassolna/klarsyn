"""
Klarsyn — analysis engine (Phase C).

Loads the scraped listing + free förening data (data/listing_466517.json) and the
parsed årsredovisning (data/arsredovisning_parsed.json), then produces a structured
set of SIGNALS with red/amber/green status against Swedish BRF benchmarks.

Design rule: this module owns ALL judgement and numbers. The report generator (Phase D)
only renders these signals into prose — it never sees raw figures to invent from.

Output: data/report_input.json
"""
from __future__ import annotations
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

GREEN, AMBER, RED = "green", "amber", "red"


def _kr(n) -> str:
    """Swedish thousands formatting with space separator (no sentence-comma damage)."""
    return f"{n:,}".replace(",", " ") if isinstance(n, (int, float)) else str(n)


def _join_sv(items):
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " och " + items[-1]


def _summary(forening_signals, score, grade):
    """Dynamic förening summary assembled from signal statuses — layout-agnostic."""
    strengths = [s["label"].split(" (")[0].lower()
                 for s in forening_signals.values() if s["status"] == GREEN]
    watch = [s["label"].split(" (")[0].lower()
             for s in forening_signals.values() if s["status"] in ("amber", "red")]
    parts = []
    if strengths:
        parts.append("Styrkor: " + _join_sv(strengths) + ".")
    if watch:
        parts.append("Att bevaka: " + _join_sv(watch) + ".")
    parts.append(f"Sammantaget {score}/100 (betyg {grade}).")
    return " ".join(parts)


def _price_summary(ask, area, diff_pct, avgift_m2_ar, pris_signals):
    rel = ("i linje med" if abs(diff_pct) <= 7
           else ("över" if diff_pct > 0 else "under"))
    txt = (f"Utgångspriset ({_kr(ask)} kr/m²) ligger {rel} områdessnittet "
           f"(~{_kr(area)} kr/m²), {diff_pct:+.1f} %.")
    if "samma_forening_comp" in pris_signals:
        s = pris_signals["samma_forening_comp"]
        txt += (f" Jämfört med en försäljning i samma förening ({_kr(s['value'])} kr/m²) "
                f"är priset {s['diff_pct']:+.1f} %.")
    if avgift_m2_ar is not None:
        txt += (f" Avgiften ({avgift_m2_ar} kr/m²/år) är normal, så priset är inte uppblåst "
                "av en konstlat låg avgift." if avgift_m2_ar <= 900 else
                f" Avgiften ({avgift_m2_ar} kr/m²/år) är förhöjd — väg in det i priset.")
    return txt


def _band(value, thresholds):
    """thresholds: list of (upper_bound, status). First bound the value is <= wins."""
    for upper, status in thresholds:
        if value <= upper:
            return status
    return thresholds[-1][1]


def _trend(series):
    """series newest-first. Return ('up'|'down'|'flat', pct_change_over_period)."""
    vals = [v for v in series if v is not None]
    if len(vals) < 2:
        return ("flat", 0.0)
    newest, oldest = vals[0], vals[-1]
    if oldest == 0:
        return ("up" if newest > 0 else "flat", 0.0)
    pct = (newest - oldest) / abs(oldest) * 100
    direction = "up" if newest > oldest else ("down" if newest < oldest else "flat")
    return (direction, round(pct, 1))


def _trend_phrase(series, unit=""):
    """Human phrase from a series (newest-first), using the ACTUAL values — no hardcode."""
    vals = [v for v in series if v is not None]
    if len(vals) < 2:
        return ""
    new, old = vals[0], vals[-1]
    n = len(vals)
    if new > old:
        return f"stiger ({_kr(old)} → {_kr(new)} {unit} på {n} år)".replace("  ", " ")
    if new < old:
        return f"faller ({_kr(old)} → {_kr(new)} {unit} på {n} år)".replace("  ", " ")
    return f"ligger stabilt kring {_kr(new)} {unit}".strip()


def analyze(listing_override: dict | None = None) -> dict:
    anchor = json.loads((DATA / "listing_466517.json").read_text(encoding="utf-8"))
    ar = json.loads((DATA / "arsredovisning_parsed.json").read_text(encoding="utf-8"))

    fo = ar["flerarsoversikt"]
    fig = ar["figures"]
    ext = ar.get("forening") or {}  # förening identity extracted from THE uploaded AR

    # Förening identity: prefer what the AI extracted from the actual PDF; fall back to
    # the bundled anchor only when a field is missing.
    ap = anchor["forening_public"]
    F = {
        "namn": ext.get("namn") or ap["namn"],
        "org_nr": ext.get("org_nr") or ap["org_nr"],
        "akta_forening": ext.get("akta_forening") if ext.get("akta_forening") is not None
                         else ap.get("akta_forening"),
        "total_boarea_m2": ext.get("total_boarea_m2") or ap.get("total_boarea_m2"),
        "byggar": ext.get("byggar") or anchor["listing"].get("byggar"),
        "tomtratt": ext.get("tomtratt") if ext.get("tomtratt") is not None
                    else ap.get("tomtratt"),
        "senaste_arsredovisning": (fo.get("years") or [ap.get("senaste_arsredovisning")])[0],
    }

    # Listing (price/area/avgift/address) comes from the listing, not the AR.
    #  - No override  -> anchor demo (full Ekbacken listing + comps).
    #  - Override      -> ONLY the user-supplied fields (no anchor leakage).
    if listing_override is None:
        L = dict(anchor["listing"])
        C = anchor["comps"]
    else:
        L = {k: v for k, v in listing_override.items() if v is not None}
        snitt = listing_override.get("omrade_snitt_kr_m2")
        C = {"snitt_kr_per_m2": snitt, "sales": []} if snitt else None
    L["byggar"] = F["byggar"]

    signals = {"forening": {}, "pris": {}}

    # ---------------- FÖRENING (financial health) ----------------
    skuld = fo["skuldsattning_kr_kvm"][0]
    signals["forening"]["skuldsattning"] = {
        "label": "Skuldsättning (lån per m²)",
        "value": skuld, "unit": "kr/m²",
        "status": _band(skuld, [(3000, GREEN), (6000, GREEN), (10000, AMBER),
                                (15000, AMBER), (10**9, RED)]),
        "trend": _trend(fo["skuldsattning_kr_kvm"]),
        "benchmark": "<6 000 lågt · 6 000–10 000 förhöjt · >15 000 högt",
        "comment": ("Låg belåning = liten risk för stora ränterelaterade avgiftshöjningar."
                    if skuld <= 6000 else
                    "Förhöjd belåning gör föreningen känsligare för räntor.")
                   + " Skulden " + _trend_phrase(fo["skuldsattning_kr_kvm"], "kr/m²") + ".",
    }

    spar = fo["sparande_kr_kvm"][0]
    signals["forening"]["sparande"] = {
        "label": "Sparande (kassaflöde för underhåll, per m²)",
        "value": spar, "unit": "kr/m²",
        "status": _band(spar, [(0, RED), (50, AMBER), (150, AMBER), (10**9, GREEN)]),
        "trend": _trend(fo["sparande_kr_kvm"]),
        "benchmark": ">150 bra · 50–150 svagt · <0 föreningen tär på kassan",
        "comment": (("Gott sparutrymme för framtida underhåll." if spar >= 150 else
                     "Tunt sparande — litet utrymme att möta framtida underhåll utan "
                     "avgiftshöjning eller nya lån.")
                    + (f" Har varit negativt något av åren."
                       if any(v is not None and v < 0 for v in fo["sparande_kr_kvm"]) else "")),
    }

    rk = fo["rantekanslighet_pct"][0]
    signals["forening"]["rantekanslighet"] = {
        "label": "Räntekänslighet",
        "value": rk, "unit": "%",
        "status": _band(rk, [(5, GREEN), (10, AMBER), (10**9, RED)]),
        "trend": _trend(fo["rantekanslighet_pct"]),
        "benchmark": "avgiftshöjning som krävs vid +1 %-enhet ränta. ≤5 % lågt",
        "comment": ("Låg — en räntehöjning slår milt mot avgiften." if rk <= 5 else
                    f"Förhöjd — vid +1 %-enhet ränta krävs ~{rk:.0f} % avgiftshöjning för "
                    "oförändrat resultat."),
    }

    avg = fo["arsavgift_kr_kvm"][0]
    avg_trend = _trend(fo["arsavgift_kr_kvm"])
    signals["forening"]["arsavgift"] = {
        "label": "Årsavgift per m²",
        "value": avg, "unit": "kr/m²",
        # amber if the level is elevated (>800) OR the fee is climbing fast
        "status": AMBER if (avg > 800 or avg_trend[1] >= 15) else GREEN,
        "trend": avg_trend,
        "benchmark": "600–800 normalt. Trenden viktigare än nivån.",
        "comment": (f"Nivån är {'normal' if avg <= 800 else 'förhöjd'} men avgiften "
                    + _trend_phrase(fo["arsavgift_kr_kvm"], "kr/m²")
                    + (". Trolig fortsatt press uppåt." if avg_trend[1] >= 15 else ".")),
    }

    energi = fo["energikostnad_kr_kvm"][0]
    energi_trend = _trend(fo["energikostnad_kr_kvm"])
    signals["forening"]["energikostnad"] = {
        "label": "Energikostnad per m²",
        "value": energi, "unit": "kr/m²",
        "status": AMBER if energi_trend[1] >= 20 else GREEN,
        "trend": energi_trend,
        "benchmark": "kontext för avgiftstrenden",
        "comment": "Energikostnaden " + _trend_phrase(fo["energikostnad_kr_kvm"], "kr/m²")
                   + (" och driver kostnaderna uppåt." if energi_trend[1] >= 20 else "."),
    }

    res = fo["resultat_efter_finansiella_tkr"]
    neg_years = sum(1 for v in res if v is not None and v < 0)
    kf = fig.get("kassaflode_lopande")
    res_status = (RED if (res[0] is not None and res[0] < 0 and neg_years >= 3
                          and kf is not None and kf < 0)
                  else AMBER if (res[0] is not None and res[0] < 0) else GREEN)
    res_cmt = f"Underskott {neg_years} av {len(res)} år." if neg_years else "Positivt resultat."
    if kf is not None:
        res_cmt += (f" Kassaflödet från driften var negativt ({_kr(kf)} kr) senaste året."
                    if kf < 0 else f" Kassaflödet från driften var positivt senaste året.")
    res_cmt += " (Resultatet tyngs ofta av bokföringsmässiga avskrivningar, ej likviditet.)"
    signals["forening"]["resultat"] = {
        "label": "Resultat efter finansiella poster",
        "value": res[0], "unit": "tkr",
        "status": res_status,
        "trend": _trend(res),
        "benchmark": f"negativt {neg_years} av {len(res)} år",
        "comment": res_cmt,
    }

    sol = fo["soliditet_pct"][0]
    sol_trend = _trend(fo["soliditet_pct"])
    signals["forening"]["soliditet"] = {
        "label": "Soliditet",
        "value": sol, "unit": "%",
        "status": GREEN if (sol >= 25 or sol_trend[0] == "up") else AMBER,
        "trend": sol_trend,
        "benchmark": "begränsad relevans i en BRF, men förbättring är positivt",
        "comment": "Soliditeten " + _trend_phrase(fo["soliditet_pct"], "%")
                   + ". Nyckeltalet har begränsad betydelse för en BRF.",
    }

    is_tomtratt = bool(F.get("tomtratt"))
    signals["forening"]["tomtratt"] = {
        "label": "Markinnehav",
        "value": "Tomträtt" if is_tomtratt else "Friköpt", "unit": "",
        "status": AMBER if is_tomtratt else GREEN, "trend": ("flat", 0.0),
        "benchmark": "tomträtt = återkommande, ofta kraftigt stigande avgäld",
        "comment": ("Föreningen har tomträtt — avgälden kan höjas kraftigt och är en "
                    "återkommande kostnad att bevaka." if is_tomtratt else
                    "Föreningen äger marken (ingen tomträtt) — en stor dold kostnad är utesluten."),
    }

    is_akta = F.get("akta_forening")
    signals["forening"]["akta_forening"] = {
        "label": "Föreningstyp",
        "value": "Äkta bostadsrättsförening" if is_akta is not False else "Oäkta förening",
        "unit": "",
        "status": GREEN if is_akta is not False else AMBER, "trend": ("flat", 0.0),
        "benchmark": "oäkta förening kan ge sämre skatte-/lånevillkor för köpare",
        "comment": ("Äkta förening → normal skattemässig behandling för dig som köpare."
                    if is_akta is not False else
                    "Oäkta förening → kan ge sämre skatte- och lånevillkor för dig som köpare."),
    }

    lik = fig.get("likvida_medel_slut")
    netto = fig.get("nettoomsattning")
    lik_status = AMBER
    if lik is not None and netto:
        lik_status = GREEN if lik >= netto * 0.25 else AMBER
    signals["forening"]["likviditet"] = {
        "label": "Likvida medel",
        "value": lik, "unit": "kr",
        "status": lik_status,
        "trend": ("flat", 0.0),
        "benchmark": "buffert för löpande utgifter och underhåll",
        "comment": (f"Kassan motsvarar ~{round(lik / netto * 100)} % av årsintäkterna."
                    if (lik is not None and netto) else
                    "Buffert för löpande utgifter och underhåll.")
                   + (" Ansträngd — ökar behovet av avgiftshöjningar eller nya lån."
                      if lik_status == AMBER else " Rimlig buffert."),
    }

    # ---------------- UNDERHÅLL (premium) ----------------
    NOW_YEAR = 2026
    BIG_TICKET = {  # component -> typical lifespan hint used only for the comment
        "Stammar / VVS (avlopp & vatten)": 50,
        "Tak": 40, "Fasad": 35, "Balkonger": 40, "Fönster": 40,
        "Hiss": 30, "Ventilation (OVK/fläktar)": 20,
        "Värme / undercentral": 25, "El (IMD/laddning)": 40,
    }
    komp_in = ar.get("underhall", {}).get("komponenter", {})
    underhall_list = []
    for comp, life in BIG_TICKET.items():
        info = komp_in.get(comp)
        if not info:
            underhall_list.append({"komponent": comp, "senaste_ar": None,
                                   "alder": None, "status": AMBER,
                                   "kommentar": "Ingen åtgärd hittad i historiken — okänt skick."})
            continue
        yr = info["senaste_ar"]
        age = NOW_YEAR - yr
        status = GREEN if age <= 12 else (AMBER if age <= 30 else RED)
        underhall_list.append({
            "komponent": comp, "senaste_ar": yr, "alder": age, "status": status,
            "kommentar": info["handelser"][-1]["text"],
        })

    # stambyte: was there a *full* stambyte (not just valves/culvert)?
    vvs = komp_in.get("Stammar / VVS (avlopp & vatten)", {})
    stambyte_ev = next((h for h in vvs.get("handelser", [])
                        if re.search(r"stambyte|relining", h["text"], re.IGNORECASE)), None)
    flaggor = [u for u in underhall_list if u["status"] in (AMBER, RED) and u["senaste_ar"]]
    flaggor.sort(key=lambda u: u["alder"], reverse=True)

    if stambyte_ev:
        underhall_head = f"Stambyte genomfört {stambyte_ev['year']} — den enskilt största " \
                         "framtida kostnaden är redan avklarad."
    else:
        underhall_head = "Inget fullständigt stambyte syns i historiken — för ett hus från " \
                         f"{L['byggar']} är detta en möjlig stor framtida kostnad."

    signals["underhall"] = {
        "sammanfattning": underhall_head,
        "stambyte_gjort": bool(stambyte_ev),
        "stambyte_ar": stambyte_ev["year"] if stambyte_ev else None,
        "komponenter": underhall_list,
        "flaggor": [f"{u['komponent']} — senast {u['senaste_ar']} "
                    f"(~{u['alder']} år sedan)" for u in flaggor[:4]],
        "historik": ar.get("underhall", {}).get("historik", {}),
    }

    # ---------------- PRIS ----------------
    ask = L.get("kr_per_m2")
    if ask is None and L.get("utgangspris_kr") and L.get("boarea_m2"):
        ask = round(L["utgangspris_kr"] / L["boarea_m2"])
    area = C["snitt_kr_per_m2"] if C else None
    diff_pct = round((ask - area) / area * 100, 1) if (ask and area) else None

    if ask and area:
        signals["pris"]["kr_per_m2_vs_omrade"] = {
            "label": "Pris per m² mot området",
            "value": ask, "unit": "kr/m²",
            "omrade_snitt": area,
            "diff_pct": diff_pct,
            "status": GREEN if abs(diff_pct) <= 7 else AMBER,
            "benchmark": f"områdessnitt ~{area:,} kr/m²".replace(",", " "),
            "comment": f"Utgångspriset ligger {diff_pct:+.1f} % mot områdessnittet — "
                       + ("i praktiken marknadsmässigt." if abs(diff_pct) <= 7
                          else "en avvikelse att undersöka."),
        }
    elif ask:
        signals["pris"]["kr_per_m2_vs_omrade"] = {
            "label": "Pris per m²", "value": ask, "unit": "kr/m²",
            "omrade_snitt": None, "diff_pct": None, "status": AMBER,
            "benchmark": "marknadsjämförelse ännu ej tillgänglig",
            "comment": "Priset visas utan områdesjämförelse (comps kopplas på härnäst).",
        }

    same = next((s for s in (C["sales"] if C else []) if s.get("samma_forening")), None)
    if same:
        d2 = round((ask - same["kr_per_m2"]) / same["kr_per_m2"] * 100, 1)
        signals["pris"]["samma_forening_comp"] = {
            "label": "Jämförelse med försäljning i samma förening",
            "value": same["kr_per_m2"], "unit": "kr/m²",
            "referens": f'{same["adress"]} ({same["datum"]})',
            "diff_pct": d2,
            "status": GREEN if abs(d2) <= 8 else AMBER,
            "benchmark": "bästa möjliga jämförelse — samma hus/förening",
            "comment": f"Såld till {same['kr_per_m2']:,} kr/m² för ~11 mån sedan; "
                       f"utgångspriset är {d2:+.1f} % — rimligt givet tiden."
                       .replace(",", " "),
        }

    avgift_m2_ar = None
    if L.get("avgift_kr_man") and L.get("boarea_m2"):
        avgift_m2_ar = round(L["avgift_kr_man"] * 12 / L["boarea_m2"])
        signals["pris"]["avgift_check"] = {
            "label": "Avgiftsnivå (påverkar priset)",
            "value": avgift_m2_ar, "unit": "kr/m²/år",
            "status": GREEN if avgift_m2_ar <= 900 else AMBER,
            "benchmark": "låg avgift + lågt pris kan dölja en dyr bostad",
            "comment": (f"Avgiften ({avgift_m2_ar} kr/m²/år) är normal — priset är inte "
                        "uppblåst av en konstlat låg avgift." if avgift_m2_ar <= 900 else
                        f"Avgiften ({avgift_m2_ar} kr/m²/år) är förhöjd — väg in den i priset."),
        }

    # ---------------- Förening score (0–100) ----------------
    # Weighted: debt & land dominate (biggest hidden risks), then savings/rates/result.
    SCORE_WEIGHTS = {
        "skuldsattning": 20, "sparande": 15, "rantekanslighet": 10, "resultat": 12,
        "likviditet": 8, "arsavgift": 8, "energikostnad": 5, "soliditet": 5,
        "tomtratt": 12, "akta_forening": 5,
    }
    STATUS_FACTOR = {GREEN: 1.0, AMBER: 0.5, RED: 0.0}
    score = round(sum(w * STATUS_FACTOR[signals["forening"][k]["status"]]
                      for k, w in SCORE_WEIGHTS.items()))

    if score >= 80:
        fin_verdict, grade, rec_head = "stark", "A", "Trygg förening"
        rec = ("Föreningens ekonomi är stark. Du kan gå vidare med god trygghet — "
               "stäm ändå av frågorna nedan innan bud.")
    elif score >= 60:
        fin_verdict, grade, rec_head = "stabil-men-bevaka", "B", "Gå vidare — men ställ frågorna"
        rec = ("Ekonomiskt sund förening med några punkter att bevaka. Gå vidare, men "
               "ta upp frågorna nedan med mäklare/styrelse och räkna in framtida "
               "avgiftshöjningar i din kalkyl.")
    elif score >= 40:
        fin_verdict, grade, rec_head = "blandad", "C", "Var försiktig — gräv djupare"
        rec = ("Blandad bild. Flera varningsflaggor gör att du bör granska "
               "årsredovisningen noga och ställa krav på svar innan du lägger bud.")
    else:
        fin_verdict, grade, rec_head = "risker", "D", "Betydande risker"
        rec = ("Svag ekonomi med betydande risker. Gå vidare endast med stor försiktighet "
               "och professionell genomgång av föreningens ekonomi.")

    # Blend the price verdict into the overall recommendation (only when we know it).
    if diff_pct is not None:
        if abs(diff_pct) <= 7:
            rec += " Priset är marknadsmässigt för området."
        elif diff_pct > 7:
            rec += f" Notera att priset ligger {diff_pct:.0f} % över områdessnittet."
        else:
            rec += f" Priset ligger {abs(diff_pct):.0f} % under områdessnittet."
        pris_status = "marknadsmässigt" if abs(diff_pct) <= 7 else "avvikande"
        pris_sammanfattning = _price_summary(ask, area, diff_pct, avgift_m2_ar, signals["pris"])
    else:
        pris_status = "okänt"
        pris_sammanfattning = ("Prisbedömning saknas — ange utgångspris, boarea och ett "
                               "områdessnitt för att jämföra mot marknaden.")

    verdict = {
        "forening_score": score,
        "forening_grade": grade,
        "rekommendation_rubrik": rec_head,
        "rekommendation": rec,
        "forening_status": fin_verdict,
        # Built dynamically from the signals — no förening-specific prose. In production
        # the polished narrative is the LLM's job (guided prompt); this is the safe fallback.
        "forening_sammanfattning": _summary(signals["forening"], score, grade),
        "pris_status": pris_status,
        "pris_sammanfattning": pris_sammanfattning,
    }

    return {
        "meta": {
            "adress": L.get("address") or "—",
            "forening": F["namn"],
            "org_nr": F["org_nr"],
            "utgangspris_kr": L.get("utgangspris_kr"),
            "boarea_m2": L.get("boarea_m2"),
            "arsredovisning": F["senaste_arsredovisning"],
        },
        "signals": signals,
        "verdict": verdict,
        "detaljer": {  # premium / locked content
            "flerarsoversikt": fo,
            "figures": fig,
        },
    }


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console is cp1252 by default
    except Exception:
        pass
    out = analyze()
    (DATA / "report_input.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n-> written to {DATA / 'report_input.json'}")
