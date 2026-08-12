"""
Klarsyn — pipeline orchestrator.

Runs the whole anchor-listing flow end to end:
    parse årsredovisning -> analyse signals -> generate report.

Phase A (listing scrape + förening lookup + comps) is currently captured in
data/listing_466517.json. Later this becomes a live fetch / PDF upload step.

Usage:  python src/run.py [--template]
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import json
import extract as E
import analyze as A
import generate_report as G

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("[1/3] Extracting årsredovisning (AI when key set, else deterministic) …")
    parsed = E.extract()
    (DATA / "arsredovisning_parsed.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"      mode: {parsed.get('_extraction')} · figures: {list(parsed['figures'])}")

    print("[2/3] Analysing signals …")
    result = A.analyze()
    (DATA / "report_input.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"      förening: {result['verdict']['forening_status']} · "
          f"pris: {result['verdict']['pris_status']}")

    print("[3/3] Generating report …")
    report, mode = G.generate(force_template="--template" in sys.argv)
    G.OUT.write_text(report, encoding="utf-8")
    print(f"      mode: {mode} -> {G.OUT}")
    print("\nDone. Open data/report_466517.md")


if __name__ == "__main__":
    main()
