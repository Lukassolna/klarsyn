# Klarsyn

Independent, plain-language decision support for Swedish apartment buyers. Paste a Booli
listing URL — Klarsyn reads the housing cooperative's annual report (*årsredovisning*) and the
listing, judges the förening's finances and the asking price, and renders a modern report
you can act on.

## Run locally

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Set `ANTHROPIC_API_KEY` in a `.env` file (and `BOOLI_SID` for automatic document fetch).

## Pipeline

| Phase | File | Output |
|---|---|---|
| Listing scrape | `src/booli.py` | address, price, m², avgift, Booli valuation, förening data |
| Fetch årsredovisning | `src/booli_docs.py` | the förening's annual-report PDF |
| Extract | `src/extract.py` | structured data (LLM) |
| Analyse | `src/analyst.py` | score, RAG signals, price verdict, questions (LLM) |
| Render | `src/report_html.py` | in-app report + HTML download |

## Design

- **Anti-hallucination:** the analyst LLM returns *judgement only*; exact extracted numbers
  are re-attached in Python, so a figure in the report is never model-written.
- **No hardcoded domain logic:** benchmarks live in the analyst prompt as guidance, not as
  Python thresholds — so any förening, layout, or edge case is handled by reasoning.
- **Graceful degradation:** a failed scrape shows "okänt" rather than leaking placeholder data.

## Disclaimer

Klarsyn produces decision support, not financial advice or a formal valuation. Data comes from
public sources and may contain errors — always verify original documents before buying.
