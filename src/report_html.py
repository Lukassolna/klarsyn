"""
Klarsyn — modern HTML report renderer.

Turns data/report_input.json (scored signals) into a clean, modern, self-contained
HTML document (embedded CSS, no external assets). Used by the Streamlit app and can
also be written to a standalone .html file.

    from report_html import render_html
    html = render_html(report_input_dict)
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

STATUS = {
    "green": ("#15803d", "#dcfce7", "Bra"),
    "amber": ("#b45309", "#fef3c7", "Bevaka"),
    "red":   ("#b91c1c", "#fee2e2", "Risk"),
    "okänt": ("#64748b", "#f1f5f9", "Okänt"),
}
_NEUTRAL = ("#64748b", "#f1f5f9", "—")


def _status(s):
    """Tolerant lookup — any status the AI emits that we don't know renders neutral."""
    return STATUS.get(s, _NEUTRAL)


TREND_ARROW = {"up": "↑", "down": "↓", "flat": "→"}

# optional meters for key numeric signals: (min, max, [green_upto, amber_upto], invert)
# invert=True means lower is better (green on the left).
METERS = {
    "skuldsattning":   (0, 15000, [6000, 10000], True),
    "sparande":        (-50, 300, [50, 150], False),
    "rantekanslighet": (0, 15, [5, 10], True),
    "arsavgift":       (400, 1100, [800, 950], True),
}


def _kr(n):
    if n is None:
        return "—"
    return f"{n:,}".replace(",", " ") if isinstance(n, (int, float)) else str(n)


def _bench_score(key, value):
    """0–100 read off the SAME benchmark bands the meter draws, so the number and the
    meter can never disagree: green zone → 100–67, amber → 66–34, red → 33–0. Purely
    deterministic — the same årsredovisning always yields the same score. Returns None
    for signals without a benchmark (those keep their trend badge instead)."""
    spec = METERS.get(key)
    if not spec or not isinstance(value, (int, float)):
        return None
    lo, hi, (b1, b2), invert = spec
    v = max(lo, min(hi, value))
    # (zone_start, zone_end, score_at_start, score_at_end), best zone first
    zones = ([(lo, b1, 100, 67), (b1, b2, 66, 34), (b2, hi, 33, 0)] if invert
             else [(hi, b2, 100, 67), (b2, b1, 66, 34), (b1, lo, 33, 0)])
    for a, b, s_a, s_b in zones:
        if a == b:
            continue
        t = (v - a) / (b - a)
        if 0 <= t <= 1:
            return round(s_a + (s_b - s_a) * t)
    return None


def _meter(key, value):
    spec = METERS.get(key)
    if not spec or not isinstance(value, (int, float)):
        return ""
    lo, hi, bands, invert = spec
    pct = max(0, min(100, (value - lo) / (hi - lo) * 100))
    # colored zones
    b1 = (bands[0] - lo) / (hi - lo) * 100
    b2 = (bands[1] - lo) / (hi - lo) * 100
    if invert:
        zones = f"linear-gradient(90deg,#dcfce7 0 {b1}%,#fef3c7 {b1}% {b2}%,#fee2e2 {b2}% 100%)"
    else:
        zones = f"linear-gradient(90deg,#fee2e2 0 {b1}%,#fef3c7 {b1}% {b2}%,#dcfce7 {b2}% 100%)"
    return (f'<div class="meter"><div class="meter-track" style="background:{zones}">'
            f'<div class="meter-pin" style="left:{pct:.1f}%"></div></div></div>')


def _signal_card(key, v):
    color, tint, badge = _status(v["status"])
    t = v.get("trend") or {}
    if isinstance(t, (list, tuple)):  # backward-compat with the old ("dir", pct) tuple
        t = {"dir": t[0] if t else "flat", "pct": t[1] if len(t) > 1 else 0.0,
             "years": 5, "pct_ok": True, "from": None, "to": None}
    d = t.get("dir", "flat")
    arrow = TREND_ARROW.get(d, "→")
    years = t.get("years") or 0
    trend_txt = ""
    if d in ("up", "down"):
        if t.get("pct_ok") and t.get("pct"):
            span = f" / {years} år" if years else ""
            trend_txt = f'<span class="trend">{arrow} {t["pct"]:+.0f}%{span}</span>'
        elif t.get("from") is not None and t.get("to") is not None:
            # % is meaningless (metric crosses zero) — show the actual values instead
            trend_txt = f'<span class="trend">{arrow} {_kr(t["from"])}→{_kr(t["to"])}</span>'
        else:
            trend_txt = f'<span class="trend">{arrow}</span>'
    # Where we have a benchmark, the score says more than the trend % did (and can't be
    # meaningless the way a % is on a series that crosses zero). Signals without a
    # benchmark keep the trend badge — there is nothing honest to score them against.
    score = _bench_score(key, v["value"])
    if score is not None:
        trend_txt = (f'<span class="sc" style="color:{color}">{score}'
                     f'<small>/100</small></span>')
    val = f'{_kr(v["value"])} <span class="unit">{v["unit"]}</span>'.strip()
    comment = v.get("comment", "")
    # Comment is collapsed by default so the card reads as number + colour + status,
    # not a wall of text. Native <details> — no JS needed, works in the exported HTML.
    comment_html = (f'<details class="more"><summary>Analys</summary>'
                    f'<div class="comment">{comment}</div></details>') if comment else ""
    return f"""
    <div class="card" style="border-left:6px solid {color}">
      <div class="card-head">
        <span class="label">{v['label']}</span>
        <span class="pill" style="color:{color};background:{tint}">{badge}</span>
      </div>
      <div class="value" style="color:{color}">{val} {trend_txt}</div>
      {_meter(key, v['value'])}
      {comment_html}
    </div>"""


def _score_color(score):
    if score >= 80:
        return "#4ade80"
    if score >= 60:
        return "#fbbf24"
    if score >= 40:
        return "#fb923c"
    return "#f87171"


def _score_ring(score, grade):
    color = _score_color(score)
    r = 50
    circ = 2 * 3.14159 * r
    filled = circ * score / 100
    return f"""<svg width="128" height="128" viewBox="0 0 128 128" class="ring">
      <circle cx="64" cy="64" r="{r}" fill="none" stroke="rgba(255,255,255,.2)" stroke-width="11"/>
      <circle cx="64" cy="64" r="{r}" fill="none" stroke="{color}" stroke-width="11"
        stroke-linecap="round" stroke-dasharray="{filled:.1f} {circ:.1f}"
        transform="rotate(-90 64 64)"/>
      <text x="64" y="58" text-anchor="middle" font-size="34" font-weight="800" fill="#fff">{score}</text>
      <text x="64" y="80" text-anchor="middle" font-size="12" fill="rgba(255,255,255,.75)">av 100 · {grade}</text>
    </svg>"""


def _price_bar(pr):
    """Simple comparative bar: asking vs area vs same-förening (kr/m²)."""
    kv = pr.get("kr_per_m2_vs_omrade")
    if not kv or not kv.get("value") or not kv.get("omrade_snitt"):
        return ""  # no comparable price data (uploaded bostad without pris/områdessnitt)
    ask = kv["value"]
    area = kv["omrade_snitt"]
    same = pr.get("samma_forening_comp", {}).get("value")
    rows = [("Detta objekt", ask, "#0f766e"),
            ("Områdessnitt", area, "#64748b")]
    if same:
        rows.append(("Samma förening", same, "#94a3b8"))
    mx = max(v for _, v, _ in rows) * 1.05
    bars = ""
    for name, val, col in rows:
        w = val / mx * 100
        bars += (f'<div class="bar-row"><span class="bar-name">{name}</span>'
                 f'<div class="bar"><div class="bar-fill" style="width:{w:.0f}%;'
                 f'background:{col}"></div></div>'
                 f'<span class="bar-val">{_kr(val)} kr/m²</span></div>')
    return f'<div class="price-bars">{bars}</div>'


def _mini_chart(label, years, series):
    """Small vertical bar chart for one metric's 5-year series — a visual companion to
    the table. Heights are normalised within the series (min→20%, max→100%) so the trend
    reads even when absolute values are large; negative bars render red."""
    pairs = [(y, v) for y, v in zip(years, series) if isinstance(v, (int, float))]
    if len(pairs) < 2:
        return ""
    vals = [v for _, v in pairs]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    cols = ""
    for y, v in pairs:
        h = 20 + (v - lo) / span * 80
        col = "#ef4444" if v < 0 else "#0f766e"
        cols += (f'<div class="col"><div class="col-val">{_kr(v)}</div>'
                 f'<div class="col-bar"><div class="col-fill" '
                 f'style="height:{h:.0f}%;background:{col}"></div></div>'
                 f'<div class="col-yr">{y}</div></div>')
    return (f'<div class="chart"><div class="chart-label">{label}</div>'
            f'<div class="bars">{cols}</div></div>')


FLER_ROWS_LABELS = [
    ("skuldsattning_kr_kvm", "Skuldsättning, kr/m²"),
    ("sparande_kr_kvm", "Sparande, kr/m²"),
    ("rantekanslighet_pct", "Räntekänslighet, %"),
    ("arsavgift_kr_kvm", "Årsavgift, kr/m²"),
    ("energikostnad_kr_kvm", "Energikostnad, kr/m²"),
    ("resultat_efter_finansiella_tkr", "Resultat e. fin., tkr"),
    ("soliditet_pct", "Soliditet, %"),
]


def _premium_html(ri: dict) -> str:
    u = ri["signals"].get("underhall", {})
    det = ri.get("detaljer", {})
    fo = det.get("flerarsoversikt", {})

    # stambyte banner
    if u.get("stambyte_gjort"):
        banner = (f'<div class="pbanner good">✓ <b>Stambyte genomfört {u["stambyte_ar"]}</b>'
                  f' — den enskilt största framtida kostnaden är redan avklarad.</div>')
    else:
        banner = f'<div class="pbanner warn">⚠ {u.get("sammanfattning","")}</div>'

    # component status table
    rows = ""
    for c in u.get("komponenter", []):
        color, tint, badge = _status(c["status"])
        yr = c["senaste_ar"] or "–"
        age = f'~{c["alder"]} år' if c["alder"] is not None else "okänt"
        rows += (f'<tr><td>{c["komponent"]}</td><td>{yr}</td><td>{age}</td>'
                 f'<td><span class="pill" style="color:{color};background:{tint}">{badge}</span></td></tr>')
    comp_table = (f'<table class="ptable"><thead><tr><th>Komponent</th><th>Senast</th>'
                  f'<th>Ålder</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table>')

    # flags
    flaggor = "".join(f"<li>{f}</li>" for f in u.get("flaggor", []))
    flag_block = (f'<div class="callout warn"><b>Åldrande — troligt underhåll framåt:</b>'
                  f'<ul>{flaggor}</ul></div>') if flaggor else ""

    # 5-year detailed table. Data is stored newest-first; reverse so years read
    # oldest → newest left to right. Each series aligns to `years` by index, so it
    # must be reversed the same way to stay column-aligned.
    years = list(reversed(fo.get("years", [])))
    head = "".join(f"<th>{y}</th>" for y in years)
    body = ""
    for key, lbl in FLER_ROWS_LABELS:
        series = list(reversed(fo.get(key) or []))
        cells = "".join(f"<td>{_kr(v) if v is not None else '–'}</td>" for v in series)
        body += f"<tr><td class='rl'>{lbl}</td>{cells}</tr>"
    fler_table = (f'<table class="ptable"><thead><tr><th>Nyckeltal</th>{head}</tr></thead>'
                  f'<tbody>{body}</tbody></table>')

    # maintenance timeline (most recent first, compact)
    hist = u.get("historik", {}) or {}
    timeline = ""
    for y in sorted(hist, reverse=True):
        txt = " ".join(hist[y])
        timeline += f'<div class="tl"><span class="tl-y">{y}</span><span>{txt}</span></div>'

    # premium AI verdict — the "is it worth buying?" block the buyer pays for. Leads the
    # premium section so unlocking clearly delivers something. All fields optional/defensive.
    pv = ri.get("premium", {}) or {}
    pv_html = ""
    if pv:
        sticker = "".join(f"<li>{s}</li>" for s in pv.get("sticker_ut", []))
        alarm = "".join(f"<li>{s}</li>" for s in pv.get("alarmerande", []))
        cols = ""
        if sticker:
            cols += f'<div class="pv-col good"><h4>✓ Det här sticker ut</h4><ul>{sticker}</ul></div>'
        if alarm:
            cols += f'<div class="pv-col bad"><h4>⚠ Det här är alarmerande</h4><ul>{alarm}</ul></div>'
        cols_html = f'<div class="pv-cols">{cols}</div>' if cols else ""
        pv_html = f"""
      <div class="pverdict">
        <div class="pv-badge">🧠 AI-omdöme · är det värt att köpa?</div>
        <div class="pv-title">{pv.get('omdome_rubrik','')}</div>
        <div class="pv-body">{pv.get('koprekommendation','')}</div>
        {cols_html}
      </div>"""

    # per-metric mini bar charts (visual companion to the 5-year table)
    charts = "".join(
        _mini_chart(lbl, years, list(reversed(fo.get(key) or [])))
        for key, lbl in FLER_ROWS_LABELS)
    charts_html = f'<div class="charts">{charts}</div>' if charts.strip() else ""

    # closing one-liner for the very end of the detailed analysis
    slutsats = pv.get("slutsats", "")
    slutsats_html = f'<div class="pclose">✅ {slutsats}</div>' if slutsats else ""

    return f"""
    <div class="premium">
      {pv_html}
      <h3>Underhåll &amp; renoveringar</h3>
      {banner}
      {comp_table}
      {flag_block}
      <h3>Detaljerade nyckeltal · 5 år</h3>
      {fler_table}
      {charts_html}
      <h3>Fullständig underhållshistorik</h3>
      <div class="timeline">{timeline}</div>
      {slutsats_html}
    </div>"""


def _implications(ri: dict) -> list[str]:
    """Buyer implications come from the AI analyst (ri.signals.underhall.implications)."""
    return (ri.get("signals", {}).get("underhall", {}) or {}).get("implications", [])


def _questions(ri: dict) -> list[str]:
    """Questions to ask come from the AI analyst (ri.signals.underhall.questions)."""
    return (ri.get("signals", {}).get("underhall", {}) or {}).get("questions", [])


def render_html(ri: dict) -> str:
    m = ri["meta"]
    fo = ri["signals"]["forening"]
    pr = ri["signals"]["pris"]
    vd = ri["verdict"]

    fin_color = {"stark": "#15803d", "stabil-men-bevaka": "#b45309",
                 "risker": "#b91c1c"}.get(vd["forening_status"], "#b45309")
    fin_label = vd["forening_status"].replace("-", " ").capitalize()
    pris_color = "#15803d" if vd["pris_status"].startswith("marknadsm") else "#b45309"

    top3 = ["skuldsattning", "sparande", "rantekanslighet"]
    teaser = "".join(
        f'<div class="teaser-item"><span class="dot" '
        f'style="background:{_status(fo[k]["status"])[0]}"></span>'
        f'<div><div class="t-label">{fo[k]["label"]}</div>'
        f'<div class="t-val">{_kr(fo[k]["value"])} {fo[k]["unit"]}</div></div></div>'
        for k in top3)

    forening_cards = "".join(_signal_card(k, fo[k]) for k in [
        "skuldsattning", "sparande", "rantekanslighet", "arsavgift",
        "energikostnad", "resultat", "likviditet", "soliditet",
        "tomtratt", "akta_forening"])
    price_cards = "".join(_signal_card(k, pr[k]) for k in pr)

    fragor = "".join(f"<li>{q}</li>" for q in _questions(ri))
    implications = "".join(f"<li>{b}</li>" for b in _implications(ri))

    area_txt = f"{m['boarea_m2']} m² · " if m.get("boarea_m2") else ""
    price_html = (f'<div class="price">{_kr(m["utgangspris_kr"])} kr '
                  f'<small>utgångspris</small></div>') if m.get("utgangspris_kr") else ""

    hojd = vd.get("rekommendation_hojdpunkt", "")
    rec_hi = f'<div class="rec-hi">{hojd}</div>' if hojd else ""

    # Only ever name sources we actually read for THIS report — a source line that lists
    # something the pipeline never fetched is the cheapest way to lose the reader's trust.
    kallor = [f"föreningens årsredovisning {m['arsredovisning']}"]
    if m.get("utgangspris_kr") or m.get("boarea_m2"):
        kallor.insert(0, "Booli-annonsen")
    if pr.get("kr_per_m2_vs_omrade", {}).get("omrade_snitt"):
        kallor.append("områdesstatistik för slutpriser")
    if pr.get("booli_vardering"):
        kallor.append("Boolis värdering")
    kallor_txt = ", ".join(kallor)

    return f"""<!doctype html><html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{ --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --bg:#f6f7f9; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  color:var(--ink); background:var(--bg); line-height:1.5; }}
.wrap {{ max-width:860px; margin:0 auto; padding:28px 20px 60px; }}
.brand {{ font-weight:800; letter-spacing:-.5px; font-size:15px; color:#0f766e; }}
.brand span {{ color:var(--muted); font-weight:500; }}
.hero {{ background:linear-gradient(135deg,#0f766e,#115e59); color:#fff; border-radius:22px;
  padding:26px 28px; margin-top:14px; box-shadow:0 10px 30px rgba(15,118,110,.25); }}
.hero h1 {{ margin:2px 0 4px; font-size:24px; letter-spacing:-.4px; }}
.hero .sub {{ opacity:.85; font-size:14px; }}
.hero .price {{ font-size:30px; font-weight:800; margin-top:12px; }}
.hero .price small {{ font-size:14px; font-weight:500; opacity:.8; }}
.hero-top {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }}
.hero-info {{ min-width:0; }}
.hero-score {{ text-align:center; flex:none; }}
.score-cap {{ font-size:11px; text-transform:uppercase; letter-spacing:.6px; opacity:.8;
  margin-bottom:4px; }}
.ring {{ display:block; }}
.rec {{ background:rgba(255,255,255,.14); border:1px solid rgba(255,255,255,.18);
  border-radius:14px; padding:14px 16px; margin-top:18px; }}
.rec-head {{ font-weight:800; font-size:16px; }}
.rec-body {{ font-size:13.5px; opacity:.92; margin-top:4px; }}
.rec-hi {{ margin-top:12px; background:rgba(255,255,255,.2); border-radius:10px;
  padding:9px 13px; font-size:14px; font-weight:700; display:inline-block; }}
.badges {{ display:flex; gap:12px; margin-top:16px; flex-wrap:wrap; }}
.badge {{ background:rgba(255,255,255,.14); backdrop-filter:blur(4px); border-radius:14px;
  padding:12px 16px; flex:1; min-width:180px; }}
.badge .k {{ font-size:12px; text-transform:uppercase; letter-spacing:.5px; opacity:.8; }}
.badge .v {{ font-size:17px; font-weight:700; display:flex; align-items:center; gap:8px; margin-top:3px; }}
.bdot {{ width:11px; height:11px; border-radius:50%; display:inline-block; }}
section {{ margin-top:30px; }}
h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:1px; color:#0f766e;
  margin:0 0 14px; font-weight:800; position:relative; padding-left:14px; }}
h2::before {{ content:""; position:absolute; left:0; top:2px; bottom:2px; width:5px;
  background:#0f766e; border-radius:3px; }}
.summary {{ background:#fff; border:1px solid var(--line); border-radius:16px; padding:20px 22px;
  font-size:15.5px; }}
.teaser {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
.teaser-item {{ background:#fff; border:1px solid var(--line); border-radius:14px; padding:14px;
  display:flex; gap:10px; align-items:flex-start; }}
.t-label {{ font-size:12px; color:var(--muted); }}
.t-val {{ font-size:18px; font-weight:700; margin-top:2px; }}
.grid {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
.card {{ background:#fff; border:1px solid var(--line); border-radius:16px; padding:16px 18px; }}
.card-head {{ display:flex; align-items:center; gap:9px; }}
.dot {{ width:10px; height:10px; border-radius:50%; flex:none; }}
.label {{ font-weight:600; font-size:14px; flex:1; }}
.pill {{ font-size:11px; font-weight:700; padding:3px 9px; border-radius:999px; }}
.value {{ font-size:30px; font-weight:800; margin:8px 0 2px; letter-spacing:-.6px; }}
.unit {{ font-size:14px; font-weight:600; color:var(--muted); }}
.trend {{ font-size:12.5px; font-weight:700; color:var(--muted); margin-left:6px; }}
.sc {{ font-size:15px; font-weight:800; margin-left:9px; letter-spacing:-.3px; }}
.sc small {{ font-size:11px; font-weight:600; opacity:.6; margin-left:1px; }}
.comment {{ font-size:13.5px; color:#475569; margin-top:8px; }}
.more {{ margin-top:10px; }}
.more>summary {{ list-style:none; cursor:pointer; font-size:12.5px; font-weight:700;
  color:#0f766e; user-select:none; display:inline-block; }}
.more>summary::-webkit-details-marker {{ display:none; }}
.more>summary::after {{ content:" ▾"; }}
.more[open]>summary::after {{ content:" ▴"; }}
.meter {{ margin:10px 0 2px; }}
.meter-track {{ height:8px; border-radius:6px; position:relative; }}
.meter-pin {{ position:absolute; top:-3px; width:3px; height:14px; background:#0f172a;
  border-radius:2px; transform:translateX(-50%); box-shadow:0 0 0 2px #fff; }}
.price-bars {{ background:#fff; border:1px solid var(--line); border-radius:16px; padding:18px 20px;
  margin-bottom:14px; }}
.bar-row {{ display:flex; align-items:center; gap:12px; margin:9px 0; }}
.bar-name {{ width:120px; font-size:13px; color:var(--muted); }}
.bar {{ flex:1; background:#f1f5f9; border-radius:6px; height:12px; overflow:hidden; }}
.bar-fill {{ height:100%; border-radius:6px; }}
.bar-val {{ width:110px; text-align:right; font-size:13px; font-weight:600; }}
.callout {{ background:#ecfdf5; border:1px solid #a7f3d0; border-radius:16px; padding:18px 20px; }}
.callout li {{ margin:6px 0; }}
ol.q {{ background:#fff; border:1px solid var(--line); border-radius:16px; padding:18px 20px 18px 40px; }}
ol.q li {{ margin:8px 0; }}
.foot {{ margin-top:26px; font-size:12px; color:var(--muted); }}
.locknote {{ text-align:center; font-size:13px; color:var(--muted); margin:6px 0 0; }}
/* premium / lock */
.lockwrap {{ position:relative; }}
.locked {{ filter:blur(8px); pointer-events:none; user-select:none; max-height:420px;
  overflow:hidden; }}
.lockgate {{ position:absolute; inset:0; display:flex; flex-direction:column;
  align-items:center; justify-content:center; text-align:center; gap:8px; padding:24px;
  background:linear-gradient(180deg,rgba(246,247,249,.72),rgba(246,247,249,.97)); }}
.lock-ico {{ font-size:30px; }}
.lock-title {{ font-weight:800; font-size:18px; }}
.lock-sub {{ font-size:13.5px; color:var(--muted); max-width:360px; }}
#pw {{ padding:10px 14px; border:1px solid var(--line); border-radius:10px; font-size:15px;
  width:230px; text-align:center; }}
.lockgate button {{ background:#0f766e; color:#fff; border:0; border-radius:10px;
  padding:10px 22px; font-weight:700; font-size:15px; cursor:pointer; }}
.lockgate button:hover {{ background:#115e59; }}
.pwerr {{ color:#b91c1c; font-size:13px; min-height:16px; }}
.lock-note {{ font-size:12px; color:#94a3b8; }}
.premium h3 {{ font-size:16px; margin:22px 0 10px; }}
.premium h3:first-child {{ margin-top:4px; }}
.pverdict {{ background:linear-gradient(135deg,#0f766e,#115e59); color:#fff;
  border-radius:18px; padding:22px 24px; margin-bottom:24px;
  box-shadow:0 10px 30px rgba(15,118,110,.25); }}
.pv-badge {{ font-size:12px; text-transform:uppercase; letter-spacing:.8px; opacity:.85;
  font-weight:700; }}
.pv-title {{ font-size:23px; font-weight:800; margin:6px 0 12px; letter-spacing:-.4px; }}
.pv-body {{ font-size:14.5px; line-height:1.65; opacity:.95; }}
.pv-cols {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:20px; }}
.pv-col {{ background:rgba(255,255,255,.12); border-radius:14px; padding:14px 16px; }}
.pv-col h4 {{ margin:0 0 8px; font-size:13.5px; }}
.pv-col ul {{ margin:0; padding-left:18px; }}
.pv-col li {{ margin:5px 0; font-size:13.5px; opacity:.95; }}
.pv-col.good {{ border:1px solid rgba(134,239,172,.5); }}
.pv-col.bad {{ border:1px solid rgba(252,165,165,.5); }}
@media (max-width:640px) {{ .pv-cols {{ grid-template-columns:1fr; }} }}
.charts {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:14px; }}
.chart {{ background:#fff; border:1px solid var(--line); border-radius:12px; padding:12px 14px; }}
.chart-label {{ font-size:12px; color:var(--muted); font-weight:600; margin-bottom:10px; }}
.bars {{ display:flex; align-items:flex-end; gap:8px; height:96px; }}
.col {{ flex:1; display:flex; flex-direction:column; align-items:center;
  justify-content:flex-end; height:100%; min-width:0; }}
.col-val {{ font-size:9.5px; font-weight:700; color:#334155; margin-bottom:3px;
  white-space:nowrap; }}
.col-bar {{ width:100%; flex:1; display:flex; align-items:flex-end; }}
.col-fill {{ width:100%; border-radius:4px 4px 0 0; min-height:3px; }}
.col-yr {{ font-size:10px; color:var(--muted); margin-top:5px; }}
.pclose {{ margin-top:20px; background:linear-gradient(135deg,#0f766e,#115e59); color:#fff;
  border-radius:14px; padding:16px 18px; font-size:15.5px; font-weight:700;
  box-shadow:0 8px 22px rgba(15,118,110,.22); }}
.lock-teaser {{ width:100%; max-width:420px; margin-bottom:14px; }}
.lt-badge {{ font-size:13px; font-weight:800; color:#0f766e; text-transform:uppercase;
  letter-spacing:.6px; margin-bottom:12px; }}
.lt-blur {{ filter:blur(5px); opacity:.55; pointer-events:none; user-select:none; }}
.lt-line {{ height:12px; background:#cbd5e1; border-radius:6px; margin:9px 0; }}
.lt-line.short {{ width:58%; }}
.lt-chart {{ width:100%; height:56px; margin-top:12px; }}
.lt-chart rect {{ fill:#0f766e; opacity:.85; }}
@media (max-width:640px) {{ .charts {{ grid-template-columns:1fr; }} }}
.pbanner {{ border-radius:12px; padding:12px 14px; font-size:14px; margin-bottom:14px; }}
.pbanner.good {{ background:#dcfce7; border:1px solid #86efac; color:#14532d; }}
.pbanner.warn {{ background:#fef3c7; border:1px solid #fcd34d; color:#78350f; }}
.ptable {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line);
  border-radius:12px; overflow:hidden; font-size:13.5px; }}
.ptable th {{ background:#f8fafc; text-align:left; padding:9px 12px; font-size:12px;
  color:var(--muted); text-transform:uppercase; letter-spacing:.4px; }}
.ptable td {{ padding:9px 12px; border-top:1px solid var(--line); }}
.ptable td.rl {{ color:var(--muted); }}
.callout.warn {{ background:#fffbeb; border-color:#fde68a; margin-top:14px; }}
.timeline {{ background:#fff; border:1px solid var(--line); border-radius:12px; padding:6px 14px; }}
.tl {{ display:flex; gap:12px; padding:8px 0; border-top:1px solid var(--line); font-size:13.5px; }}
.tl:first-child {{ border-top:0; }}
.tl-y {{ font-weight:700; color:#0f766e; min-width:44px; }}
@media (max-width:640px) {{ .grid,.teaser {{ grid-template-columns:1fr; }}
  .hero-top {{ flex-direction:column; align-items:flex-start; }} }}
</style></head><body><div class="wrap">

<div class="brand">Klarsyn <span>· beslutsunderlag för bostadsköp</span></div>

<div class="hero">
  <div class="hero-top">
    <div class="hero-info">
      <div class="sub">{m['forening']} · org.nr {m['org_nr']}</div>
      <h1>{m['adress']}</h1>
      <div class="sub">{area_txt}årsredovisning {m['arsredovisning']}</div>
      {price_html}
    </div>
    <div class="hero-score">
      <div class="score-cap">Föreningens hälsa</div>
      {_score_ring(vd['forening_score'], vd['forening_grade'])}
    </div>
  </div>
  <div class="rec">
    <div class="rec-head">✓ {vd['rekommendation_rubrik']}</div>
    <div class="rec-body">{vd['rekommendation']}</div>
    {rec_hi}
  </div>
  <div class="badges">
    <div class="badge"><div class="k">Föreningens ekonomi</div>
      <div class="v"><span class="bdot" style="background:{fin_color}"></span>{fin_label}</div></div>
    <div class="badge"><div class="k">Priset</div>
      <div class="v"><span class="bdot" style="background:{pris_color}"></span>{vd['pris_status'].capitalize()}</div></div>
  </div>
</div>

<section>
  <h2>Snabböversikt · gratis</h2>
  <div class="teaser">{teaser}</div>
  <p class="locknote">🔓 Demo: hela rapporten visas nedan. I skarpt läge låses den upp mot betalning.</p>
</section>

<section>
  <h2>Sammanfattning</h2>
  <div class="summary">{vd['forening_sammanfattning']} {vd['pris_sammanfattning']}</div>
</section>

<section>
  <h2>Föreningens ekonomi</h2>
  <div class="grid">{forening_cards}</div>
</section>

<section>
  <h2>Pris &amp; värde</h2>
  {_price_bar(pr)}
  <div class="grid">{price_cards}</div>
</section>

<section>
  <h2>Vad det betyder för dig</h2>
  <div class="callout"><ul>{implications}</ul></div>
</section>

<section>
  <h2>Frågor att ställa mäklaren eller styrelsen</h2>
  <ol class="q">{fragor}</ol>
</section>

<section>
  <h2>Detaljerad analys · Premium</h2>
  <div class="lockwrap">
    <div id="premium-content" class="locked">{_premium_html(ri)}</div>
    <div id="lockgate" class="lockgate">
      <div class="lock-teaser">
        <div class="lt-badge">🧠 AI-omdöme · är det värt att köpa?</div>
        <div class="lt-blur">
          <div class="lt-line"></div>
          <div class="lt-line"></div>
          <div class="lt-line short"></div>
          <svg class="lt-chart" viewBox="0 0 200 60" preserveAspectRatio="none">
            <rect x="6" y="24" width="26" height="36" rx="3"/>
            <rect x="40" y="16" width="26" height="44" rx="3"/>
            <rect x="74" y="30" width="26" height="30" rx="3"/>
            <rect x="108" y="10" width="26" height="50" rx="3"/>
            <rect x="142" y="20" width="26" height="40" rx="3"/>
          </svg>
        </div>
      </div>
      <div class="lock-ico">🔒</div>
      <div class="lock-title">Lås upp den fullständiga analysen</div>
      <div class="lock-sub">Underhållshistorik, stambyte-status, komponentbesiktning och
        detaljerade nyckeltal i 5 år.</div>
      <button onclick="klarUnlock()">Lås upp den fullständiga analysen</button>
      <div class="lock-note">I skarp version ersätts detta av betalning via Swish.</div>
    </div>
  </div>
</section>

<div class="foot">
  <strong>Källor:</strong> {kallor_txt}.<br>
  <strong>Förbehåll:</strong> Detta är ett beslutsunderlag, inte finansiell rådgivning
  eller värdering. Uppgifter bygger på offentliga register och kan innehålla fel.
  Kontrollera alltid originalhandlingarna innan köp.
</div>

</div>
<script>
function klarUnlock(){{
  document.getElementById('premium-content').classList.remove('locked');
  document.getElementById('lockgate').style.display='none';
}}
</script>
</body></html>"""


if __name__ == "__main__":
    ri = json.loads((DATA / "report_input.json").read_text(encoding="utf-8"))
    out = DATA / "report_466517.html"
    out.write_text(render_html(ri), encoding="utf-8")
    print(f"-> {out}")
