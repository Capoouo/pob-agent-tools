"""Text folding, Traditional Chinese normalization, and legacy Awakened gem helpers."""

from __future__ import annotations

import re
import unicodedata

from .constants import (
    _ACTIVE_AWAKENED_EXCEPTIONAL_LOWER,
    _CJK_RE,
    _FOLD_CHAR_MAP,
    _POB_ESCAPE_RE,
)
from .tables_tw import _TRAD_TO_SIMP_MAP, _TW_ALIAS_RE, TW_TO_EN_ALIASES


def is_legacy_awakened_gem(name: str) -> bool:
    """Return True iff `name` is a legacy Awakened support removed/drop-disabled in PoE1 3.28 (Mirage).

    Only Awakened Empower, Awakened Enhance, and Awakened Enlighten remain active as Exceptional gems.
    """
    if not name:
        return False
    cleaned = _POB_ESCAPE_RE.sub("", str(name)).strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+support$", "", cleaned).strip()
    return cleaned.startswith("awakened ") and cleaned not in _ACTIVE_AWAKENED_EXCEPTIONAL_LOWER


def normalize_tw_query(text: str) -> str:
    """Normalize Traditional Chinese (TW) PoE1 gem/unique aliases and common Trad->Simp characters."""
    if not text:
        return ""
    s = str(text)
    if not _CJK_RE.search(s) and "．" not in s and "・" not in s:
        return s

    stripped = s.strip()
    if stripped:
        dot_norm = stripped.replace("．", "·").replace("・", "·")
        exact = TW_TO_EN_ALIASES.get(dot_norm)
        if exact is not None:
            return exact

    out = s.replace("．", "·").replace("・", "·")
    if _TW_ALIAS_RE.search(out):
        parts: list[str] = []
        last_end = 0
        prev_was_match_end = -1
        for m in _TW_ALIAS_RE.finditer(out):
            start, end = m.span()
            if start > last_end:
                parts.append(out[last_end:start])
            rep = TW_TO_EN_ALIASES[m.group(0)]
            need_pad_l = (start == prev_was_match_end) or (
                start > 0 and out[start - 1].isascii() and out[start - 1].isalnum()
            )
            need_pad_r = end < len(out) and out[end].isascii() and out[end].isalnum()
            parts.append((" " if need_pad_l else "") + rep + (" " if need_pad_r else ""))
            last_end = end
            prev_was_match_end = end
        if last_end < len(out):
            parts.append(out[last_end:])
        out = "".join(parts)

    return out.translate(_TRAD_TO_SIMP_MAP)


def _fold_search_text(text: str) -> str:
    """Fold Unicode diacritics (NFKD), curly apostrophes/dashes, and case for search matching."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.translate(_FOLD_CHAR_MAP).lower()
