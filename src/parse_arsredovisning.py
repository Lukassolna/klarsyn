"""
Klarsyn — årsredovisning parser (Phase B).

Turns a BRF annual-report PDF into structured JSON. Built against the real AR for
HSB Brf Ekbacken i Sundbyberg (org 715200-0290), our anchor case.

Two hard problems this handles:
  1. Broken encoding: åäö extract as U+FFFD. We match labels on their ASCII skeleton
     (regex tolerant of the mangled positions), never on exact Swedish spelling.
  2. Space-as-thousands-separator ambiguity ("3 466" = one number, "96 -17" = two).
     Solved with column-position awareness: numeric tokens are bucketed to the nearest
     column centre (derived from the header row), then joined within a bucket.

Output: dict -> data/arsredovisning_parsed.json
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PDF = ROOT / "exempel årsredvosning.pdf"
OUT = ROOT / "data" / "arsredovisning_parsed.json"

# A token that looks like a (possibly negative) integer with optional grouping spaces
# handled at the column level, so here just single runs of digits or a lone minus+digits.
NUM_RE = re.compile(r"^-?\d+$")


def _num_words(words):
    """Yield (center_x, top, text) for word tokens that are pure integers."""
    for w in words:
        t = w["text"].replace("−", "-")  # unicode minus -> ascii
        if NUM_RE.match(t):
            yield ((w["x0"] + w["x1"]) / 2, w["top"], t)


def _column_centers(words, year_like=True):
    """Derive column x-centres from the header row.

    year_like=True  -> match 'YYYY/YYYY' headers (flerårsöversikt).
    year_like=False -> match 'YYYY-MM-DD' headers (balans/resultat/kassaflöde).
    """
    pat = re.compile(r"^\d{4}/\d{4}$") if year_like else re.compile(r"^\d{4}-\d{2}-\d{2}$")
    hits = [w for w in words if pat.match(w["text"])]
    if not hits:
        return []
    # Group headers that share a column (date headers stack on two lines at same x).
    centers = {}
    for w in hits:
        cx = round((w["x0"] + w["x1"]) / 2)
        # merge near-equal x (within 6pt)
        key = next((k for k in centers if abs(k - cx) < 6), cx)
        centers.setdefault(key, key)
    return sorted(centers)


def _row_values(words, top, centers, tol=4.0, max_dist=40.0):
    """All numeric tokens on the text-row at `top`, bucketed to nearest column centre.

    Tokens in the same bucket are concatenated in x-order (rejoins '3' '466' -> 3466).
    Tokens farther than `max_dist` from every centre are dropped — this discards stray
    note-reference numbers ("Not 13") sitting mid-row, away from the value columns.
    Returns a list aligned to `centers` (None where a column has no value).
    """
    buckets = {i: [] for i in range(len(centers))}
    for cx, wtop, text in _num_words(words):
        if abs(wtop - top) > tol:
            continue
        i = min(range(len(centers)), key=lambda k: abs(centers[k] - cx))
        if abs(centers[i] - cx) > max_dist:
            continue  # stray number (e.g. a note reference), not a column value
        buckets[i].append((cx, text))
    out = []
    for i in range(len(centers)):
        toks = sorted(buckets[i])
        if not toks:
            out.append(None)
            continue
        joined = toks[0][1] + "".join(t for _, t in toks[1:])
        # a leading '-' split from digits e.g. '-' '17' would need care; NUM_RE keeps
        # '-17' intact, so simple concat is safe here.
        out.append(int(joined))
    return out


def _find_row_tops(words, label_regex):
    """All `top` y's whose reconstructed text-row matches, in top-to-bottom order."""
    rows = {}
    for w in words:
        key = round(w["top"] / 2) * 2
        rows.setdefault(key, []).append(w)
    out = []
    for key in sorted(rows):
        line = " ".join(x["text"] for x in sorted(rows[key], key=lambda a: a["x0"]))
        if label_regex.search(line):
            out.append(key)
    return out


def _find_row_top(words, label_regex):
    tops = _find_row_tops(words, label_regex)
    return tops[0] if tops else None


# Flerårsöversikt rows: label-skeleton regex (tolerant of � for åäö) -> json key.
# The mangled char is matched with '.' so we never depend on Swedish letters.
FLERARS_ROWS = {
    "sparande_kr_kvm":            r"Sparande, kr/kvm",
    "skuldsattning_kr_kvm":       r"Skulds.ttning, kr/kvm",
    "skuldsattning_brf_kr_kvm":   r"Skulds.ttning bostadsr.ttsyta",
    "rantekanslighet_pct":        r"R.ntek.nslighet, %",
    "energikostnad_kr_kvm":       r"Energikostnad, kr/kvm",
    "arsavgift_kr_kvm":           r".rsavgifter, kr/kvm",
    "resultat_efter_finansiella_tkr": r"Resultat efter finansiella poster, tkr",
    "soliditet_pct":              r"Soliditet, %",
}


# --------------------------------------------------------------------------- #
# Maintenance history (renovations, stambyte, roof, facade …)
# --------------------------------------------------------------------------- #
YEAR_LINE = re.compile(r"^(19[7-9]\d|20[0-4]\d)$")

# component -> regex over the (mangled-tolerant) event text. Ordered by buyer priority.
COMPONENT_PATTERNS = {
    "Stammar / VVS (avlopp & vatten)": r"stam|kulvert|avlopp|vattenledning|relining|stamledning",
    "Tak":            r"tak|pl.ttak",
    "Fasad":          r"fasad|puts",
    "Balkonger":      r"balkong",
    "Fönster":        r"f.nster",
    "Hiss":           r"hiss",
    "Ventilation (OVK/fläktar)": r"OVK|ventilation|fl.kt|fr.nluft",
    "Värme / undercentral":      r"undercentral|v.rme|fj.rrv.rme|v.rmesy",
    "El (IMD/laddning)":         r"\bIMD\b|laddstolp|laddstation|el-ladd|elstam|elsystem|bredband",
}


def parse_maintenance(pages) -> dict:
    """Extract the year-by-year renovation list and classify big-ticket components.

    The AR lists a bare 4-digit year on its own line, followed by 1+ description lines,
    until the next year. We collect those, then tag each event to components by keyword.
    """
    events: dict[str, list[str]] = {}
    cur_year = None
    for pg in pages:
        for raw in (pg.extract_text() or "").splitlines():
            line = raw.strip()
            if YEAR_LINE.match(line):
                cur_year = line
                events.setdefault(cur_year, [])
                continue
            if cur_year and line and not line.startswith("Document ID"):
                # stop collecting once we leave the history block into notes/figures
                if re.search(r"\d{3}\s\d{3}|RESULTATR|BALANSR|Not \d", line):
                    cur_year = None
                    continue
                if len(line) > 3:
                    events[cur_year].append(line)

    # keep only years that actually have descriptive text
    events = {y: t for y, t in events.items() if t}

    components: dict[str, dict] = {}
    for comp, pat in COMPONENT_PATTERNS.items():
        rx = re.compile(pat, re.IGNORECASE)
        hits = []
        for year in sorted(events):
            for ev in events[year]:
                if rx.search(ev):
                    hits.append({"year": int(year), "text": ev})
        if hits:
            components[comp] = {
                "senaste_ar": max(h["year"] for h in hits),
                "handelser": hits,
            }
    return {"historik": events, "komponenter": components}


def parse(pdf_path: Path = DEFAULT_PDF) -> dict:
    pdf_path = Path(pdf_path)
    result: dict = {"source_pdf": pdf_path.name, "flerarsoversikt": {}, "figures": {},
                    "underhall": {}}

    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages
        result["underhall"] = parse_maintenance(pages)
        # --- Flerårsöversikt: find the page holding it ---
        fler_words = None
        for pg in pages:
            words = pg.extract_words()
            if any("FLER" in w["text"] and "VERSIKT" in w["text"] for w in words):
                fler_words = words
                break
            # header split across tokens: fall back to detecting the years + 'Sparande'
            line = " ".join(w["text"] for w in words)
            if "Sparande," in line and "Skulds" in line:
                fler_words = words
                break
        if fler_words:
            centers = _column_centers(fler_words, year_like=True)
            result["flerarsoversikt"]["years"] = ["2024/2025", "2023/2024",
                                                  "2022/2023", "2021/2022", "2020/2021"]
            for key, lbl in FLERARS_ROWS.items():
                top = _find_row_top(fler_words, re.compile(lbl))
                if top is None:
                    result["flerarsoversikt"][key] = None
                    continue
                result["flerarsoversikt"][key] = _row_values(fler_words, top, centers)

        # --- Single figures from balans/resultat/kassaflöde (current-year = col 0) ---
        FIG_ROWS = {
            "arets_resultat":        r".rets resultat",
            "nettoomsattning":       r"Summa Nettooms.ttning",
            "avskrivningar":         r"Av- och nedskrivningar",
            "underhallsfond":        r"Fond f.r yttre underh.ll",
            "eget_kapital":          r"Summa Eget kapital",
            "langfr_kreditinstitut": r"Summa L.ngfristiga skulder",
            "kortfr_kreditinstitut": r".vriga kortfristiga skulder till kreditinstitut",
            "summa_skulder":         r"Summa Skulder",
            "likvida_medel_slut":    r"Likvida medel vid .rets slut",
            "kassaflode_lopande":    r"Kassafl.de fr.n den l.pande verksamheten +-?\d",
        }
        for pg in pages:
            words = pg.extract_words()
            centers = _column_centers(words, year_like=False)
            if not centers:
                continue
            for key, lbl in FIG_ROWS.items():
                if key in result["figures"]:
                    continue
                # try every matching row; keep the first that yields a real value in
                # the current-year column (skips section-header lines with no numbers).
                for top in _find_row_tops(words, re.compile(lbl)):
                    vals = _row_values(words, top, centers)
                    if vals and vals[0] is not None:
                        result["figures"][key] = vals[0]  # current-year column
                        break

    return result


if __name__ == "__main__":
    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    data = parse(pdf)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n-> written to {OUT}")
