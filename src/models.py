"""Model registry — single source of truth for selectable Claude models.

IDs per the Anthropic API. NOTE: claude-opus-4-8 rejects the `temperature` param
(handled in extract.py). Keep labels human-friendly for the UI dropdown.
"""

MODELS = {
    "Opus 4.8 — högst kvalitet":      {"id": "claude-opus-4-8",   "hint": "Bäst, dyrast/långsammast"},
    "Sonnet 4.6 — balanserad":        {"id": "claude-sonnet-4-6", "hint": "Snabb, bra kvalitet, billigare"},
    "Haiku 4.5 — snabb & billig":     {"id": "claude-haiku-4-5",  "hint": "Snabbast/billigast, för bulk"},
}

DEFAULT_LABEL = "Sonnet 4.6 — balanserad"  # sensible default: fast + cheap + good


def id_for(label: str) -> str:
    return MODELS.get(label, MODELS[DEFAULT_LABEL])["id"]
