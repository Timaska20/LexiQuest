from __future__ import annotations

ALIASES = {
    "kz": "kk",      # common country-code typo; Kazakh language ISO 639-1 is kk
    "kaz": "kk",
    "eng": "en",
    "rus": "ru",
}


def normalize_lang(value: str | None, default: str = "en") -> str:
    raw = (value or default).strip().lower().replace("_", "-")
    base, *rest = raw.split("-", 1)
    base = ALIASES.get(base, base)
    return base if not rest else f"{base}-{rest[0]}"
