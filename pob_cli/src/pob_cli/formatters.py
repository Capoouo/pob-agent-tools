"""Output formatting and PoB color escape stripping."""

from __future__ import annotations

import json
import re
from typing import Any

try:
    from tabulate import tabulate as _tabulate
except ImportError:  # pragma: no cover
    _tabulate = None

POB_ESCAPE_RE = re.compile(r"\^x[0-9A-Fa-f]{6}|\^\d")

RAW_PRESERVE_KEYS = frozenset({
    "raw",
    "xml",
    "code",
    "notes",
    "text",
    "lhsRaw",
    "rhsRaw",
    "last_good_code",
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


def fmt_num(val: float | int | None, decimals: int = 1) -> str:
    """Format a number concisely for human/LLM tables."""
    if val is None:
        return "-"
    if isinstance(val, bool):
        return "Yes" if val else "No"
    if isinstance(val, int) or (isinstance(val, float) and val.is_integer()):
        return f"{int(val):,}"
    if abs(val) >= 100:
        return f"{val:,.0f}"
    return f"{val:,.{decimals}f}"


def fmt_delta(before: float | int | None, after: float | int | None, unit: str = "") -> str:
    """Format a before -> after delta with absolute and percentage change."""
    b = float(before or 0.0)
    a = float(after or 0.0)
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
