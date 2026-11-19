"""Output formatting and PoB color escape stripping."""

from __future__ import annotations

import json
import math
import re
from typing import Any

try:
    from tabulate import tabulate as _tabulate
except ImportError:  # pragma: no cover
    _tabulate = None

POB_ESCAPE_RE = re.compile(r"\^x[0-9A-Fa-f]{6}|\^\d")

RAW_PRESERVE_KEYS = frozenset({
    "raw",
    "beforeRaw",
    "afterRaw",
    "xml",
    "code",
    "notes",
    "text",
    "lhsRaw",
    "rhsRaw",
    "last_good_code",
    "updateWarning",
    "customModsText",
})


def strip_pob_escapes(text: str | None) -> str:
    """Strip PoB color codes (^xRRGGBB and ^0..^9) from display strings."""
    if not text:
        return ""
    return POB_ESCAPE_RE.sub("", str(text))


def sanitize_for_output(obj: Any, *, preserve_raw: bool = True) -> Any:
    """Recursively strip PoB color codes from strings while preserving raw payload keys."""
    if isinstance(obj, str):
        return strip_pob_escapes(obj)
    if isinstance(obj, list):
        return [sanitize_for_output(item, preserve_raw=preserve_raw) for item in obj]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if preserve_raw and k in RAW_PRESERVE_KEYS:
                out[k] = v
            else:
                out[k] = sanitize_for_output(v, preserve_raw=preserve_raw)
        return out
    return obj


def _parse_numeric_or_none(val: Any) -> float | None:
    """Parse numeric values and PoB string sentinels ('inf', '-inf', 'nan') to float, or None."""
    if val is None:
        return 0.0
    if isinstance(val, bool):
        return float(int(val))
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return 0.0
        s_low = s.lower()
        if s_low in ("inf", "+inf", "infinity", "+infinity"):
            return math.inf
        if s_low in ("-inf", "-infinity"):
            return -math.inf
        if s_low == "nan":
            return math.nan
        try:
            return float(s.replace(",", ""))
        except ValueError:
            return None
    return None


def fmt_num(val: float | int | str | None, decimals: int = 1) -> str:
    """Format a number concisely for human/LLM tables, safely handling PoB sentinels ('inf', '-inf', 'nan')."""
    if val is None:
        return "-"
    if isinstance(val, bool):
        return "Yes" if val else "No"
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return "-"
        s_low = s.lower()
        if s_low in ("inf", "+inf", "infinity", "+infinity"):
            return "inf"
        if s_low in ("-inf", "-infinity"):
            return "-inf"
        if s_low == "nan":
            return "nan"
        # Preserve non-numeric sentinel strings (or pre-formatted text) verbatim
        return val
    if isinstance(val, float):
        if math.isnan(val):
            return "nan"
        if math.isinf(val):
            return "inf" if val > 0 else "-inf"
        if val.is_integer():
            return f"{int(val):,}"
        if abs(val) >= 100:
            return f"{val:,.0f}"
        return f"{val:,.{decimals}f}"
    if isinstance(val, int):
        return f"{val:,}"
    return str(val)


def fmt_delta(before: float | int | str | None, after: float | int | str | None, unit: str = "") -> str:
    """Format a before -> after delta with absolute and percentage change, hardened against inf/nan/strings."""
    b = _parse_numeric_or_none(before)
    a = _parse_numeric_or_none(after)
    if b is None or a is None:
        b_disp = fmt_num(before)
        a_disp = fmt_num(after)
        if b_disp == a_disp:
            return "0"
        return f"{b_disp}{unit} -> {a_disp}{unit}"

    if math.isnan(b) or math.isnan(a):
        if math.isnan(b) and math.isnan(a):
            return "0"
        return f"{fmt_num(b)}{unit} -> {fmt_num(a)}{unit}"

    if math.isinf(b) or math.isinf(a):
        if b == a:
            return "0"
        return f"{fmt_num(b)}{unit} -> {fmt_num(a)}{unit}"

    diff = a - b
    if abs(diff) < 1e-6:
        return "0"
    sign = "+" if diff > 0 else ""
    diff_str = fmt_num(diff)
    if abs(b) > 1e-6:
        pct = (diff / abs(b)) * 100.0
        pct_sign = "+" if pct > 0 else ""
        return f"{sign}{diff_str}{unit} ({pct_sign}{pct:.1f}%)"
    return f"{sign}{diff_str}{unit}"


def render_update_warning(warning: str | None) -> str:
    """Render a top-level Markdown warning banner for degraded engine update states."""
    if not warning or not str(warning).strip():
        return ""
    return f"> [WARN] {str(warning).strip()}\n\n"


def render_table(headers: list[str], rows: list[list[Any]]) -> str:
    """Render a compact GitHub-flavored Markdown table."""
    if not rows:
        return "_(empty)_"
    cleaned_rows = [[strip_pob_escapes(str(cell)) if cell is not None else "-" for cell in row] for row in rows]
    if _tabulate is not None:
        return _tabulate(cleaned_rows, headers=headers, tablefmt="github")
    str_headers = [str(h) for h in headers]
    widths = [
        max(len(str_headers[i]), *(len(r[i]) if i < len(r) else 0 for r in cleaned_rows), 3)
        for i in range(len(str_headers))
    ]
    hdr_line = "| " + " | ".join(str_headers[i].ljust(widths[i]) for i in range(len(str_headers))) + " |"
    sep_line = "|" + "|".join("-" * (widths[i] + 2) for i in range(len(str_headers))) + "|"
    body_lines = [
        "| " + " | ".join((r[i] if i < len(r) else "-").ljust(widths[i]) for i in range(len(str_headers))) + " |"
        for r in cleaned_rows
    ]
    return "\n".join([hdr_line, sep_line, *body_lines])


def to_json_str(data: Any, *, sanitize: bool = True) -> str:
    """Serialize output data to formatted JSON (optionally skipping PoB escape stripping for external APIs)."""
    payload = sanitize_for_output(data) if sanitize else data
    return json.dumps(payload, ensure_ascii=False, indent=2)
