"""Deterministic exact vendor identity; no punctuation or alias heuristics."""

import unicodedata


def normalize_vendor_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name).casefold()
    normalized = unicodedata.normalize("NFKC", normalized)
    result = " ".join(normalized.split())
    if not result:
        raise ValueError("vendor name must not be blank")
    return result
