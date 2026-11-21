"""Output formatting, RFC-8259 JSON emission, and PoB color escape stripping."""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

_tabulate_checked = False
_tabulate_fn: Any = None

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


def _get_tabulate() -> Any:
    global _tabulate_checked, _tabulate_fn
    if not _tabulate_checked:
        _tabulate_checked = True
        try:
            from tabulate import tabulate as _tab

            _tabulate_fn = _tab
        except ImportError:  # pragma: no cover
            _tabulate_fn = None
    return _tabulate_fn


def strip_pob_escapes(text: str | None) -> str:
    """Strip PoB color codes (^xRRGGBB and ^0..^9) from display strings."""
    if not text:
        return ""
    return POB_ESCAPE_RE.sub("", str(text))


def sanitize_for_output(obj: Any, *, preserve_raw: bool = True) -> Any:
    """Recursively strip PoB color codes from strings while preserving raw payload keys."""
    if isinstance(obj, str):
        return strip_pob_escapes(obj)
    if isinstance(obj, (list, tuple)):
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


def fmt_signed_pct(delta: Any, pct: Any = None) -> str:
    """Render '+1,234 (+4.2%)' for a PoB-measured delta; '-' when unavailable/non-finite, '0' when unchanged."""
    if delta is None or isinstance(delta, bool):
        return "-"
    try:
        d = float(delta)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(d):
        return "-"
    if d == 0:
        return "0"
    sign = "+" if d > 0 else ""
    out = f"{sign}{fmt_num(d)}"
    if pct is not None and not isinstance(pct, bool):
        try:
            p = float(pct)
        except (TypeError, ValueError):
            p = math.nan
        if math.isfinite(p):
            pct_sign = "+" if (p > 0 or (p == 0 and d > 0)) else ""
            out += f" ({pct_sign}{p:.1f}%)"
    return out


def keep_rejected_warning(stripped: dict[str, Any] | list[str] | tuple[str, ...] | None) -> list[str]:
    """Loud warning lines for --keep tokens that matched nothing (those items were NOT kept)."""
    if stripped is None:
        return []
    if isinstance(stripped, dict):
        raw_rejected = stripped.get("keepRejected") or []
    elif isinstance(stripped, (list, tuple)):
        raw_rejected = stripped
    else:
        return []
    rejected = [str(r).strip() for r in raw_rejected if str(r).strip()]
    if not rejected:
        return []
    return ["> **WARNING --keep not matched (item was NOT kept)**: " + "; ".join(rejected)]


def restore_warning(check: dict[str, Any] | None) -> str | None:
    """Warning when the build could not be verified as restored after a what-if run."""
    if not isinstance(check, dict) or not check:
        return None
    chk = (
        check.get("restoreCheck")
        if "restoreCheck" in check and isinstance(check.get("restoreCheck"), dict)
        else check
    )
    if not isinstance(chk, dict) or not chk:
        return None
    if not chk.get("verified", True):
        detail = chk.get("detail") or "build state differs from before the run"
        return (
            "> **WARNING build not verified as restored**: "
            f"{detail}. Reload the build (`pob load ...`) before trusting later results."
        )
    return None


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
    tab_fn = _get_tabulate()
    if tab_fn is not None:
        return tab_fn(cleaned_rows, headers=headers, tablefmt="github")
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


def _sanitize_nonfinite_json(obj: Any) -> Any:
    """Convert non-finite floats (NaN/Inf/-Inf) into RFC-8259 JSON-safe values."""
    if isinstance(obj, float):
        if math.isnan(obj):
            return None
        if math.isinf(obj):
            return "inf" if obj > 0 else "-inf"
        return obj
    if isinstance(obj, (list, tuple)):
        return [_sanitize_nonfinite_json(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_nonfinite_json(v) for k, v in obj.items()}
    return obj


def to_json_str(data: Any, *, sanitize: bool = True, compact: bool = False) -> str:
    """Serialize output data to strict RFC-8259 JSON (no NaN/Infinity tokens)."""
    payload = sanitize_for_output(data) if sanitize else data
    payload = _sanitize_nonfinite_json(payload)
    if compact:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)


def emit_json(
    data: Any,
    *,
    compact: bool = False,
    out: str | Path | None = None,
    sanitize: bool = True,
) -> str:
    """Serialize RFC-8259 JSON safely and write either to a UTF-8 no-BOM file (`out`) or stdout."""
    text = to_json_str(data, sanitize=sanitize, compact=compact)
    if out is not None:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8", newline="\n")
        return text
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    return text


def _safe_min_ele_max_hit(stats: dict[str, Any]) -> float | str | None:
    if not stats:
        return None
    if stats.get("eleMaxHit") is not None:
        return stats.get("eleMaxHit")
    vals: list[float] = []
    for key_group in (
        ("FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken"),
        ("fireMaxHit", "coldMaxHit", "lightningMaxHit"),
    ):
        for key in key_group:
            raw = stats.get(key)
            if raw is None:
                continue
            parsed = _parse_numeric_or_none(raw)
            if parsed is not None and not math.isnan(parsed):
                vals.append(parsed)
        if vals:
            break
    if not vals:
        return None
    m = min(vals)
    return "inf" if math.isinf(m) and m > 0 else m


def _extract_metrics_block(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalize metrics from either full `digest` or compact `summary`/`coreStats` payloads."""
    dig = entry.get("digest") if isinstance(entry.get("digest"), dict) else {}
    summ = entry.get("summary") if isinstance(entry.get("summary"), dict) else {}
    core = entry.get("coreStats") if isinstance(entry.get("coreStats"), dict) else {}
    if not core and isinstance(summ.get("coreStats"), dict):
        core = summ["coreStats"]
    st = dig.get("stats") if isinstance(dig.get("stats"), dict) else {}
    if not st and isinstance(entry.get("stats"), dict):
        st = entry["stats"]
    off = summ.get("offence") if isinstance(summ.get("offence"), dict) else {}
    dfn = summ.get("defence") if isinstance(summ.get("defence"), dict) else {}

    def _pick(*candidates: Any) -> Any:
        for val in candidates:
            if val is not None:
                return val
        return None

    primary_key = _pick(
        dig.get("primaryDpsKey"),
        summ.get("primaryDpsKey"),
        off.get("primaryDpsKey"),
        entry.get("primaryDpsKey"),
        "CombinedDPS",
    )
    primary_dps = _pick(
        dig.get("primaryDps"),
        summ.get("primaryDps"),
        off.get("primaryDps"),
        off.get(primary_key),
        core.get(primary_key),
        st.get(primary_key),
        entry.get("primaryDps"),
    )
    total_ehp = _pick(
        dig.get("totalEhp"),
        summ.get("totalEhp"),
        dfn.get("TotalEHP"),
        dfn.get("totalEhp"),
        core.get("TotalEHP"),
        st.get("TotalEHP"),
        entry.get("totalEhp"),
    )
    avg_hit = _pick(
        st.get("AverageDamage"),
        core.get("AverageDamage"),
        off.get("AverageDamage"),
        dig.get("averageDamage"),
        summ.get("averageDamage"),
    )
    speed = _pick(
        st.get("Speed"),
        core.get("Speed"),
        off.get("Speed"),
        dig.get("speed"),
        summ.get("speed"),
    )
    life = _pick(
        st.get("Life"),
        core.get("Life"),
        dfn.get("Life"),
        dig.get("life"),
        summ.get("life"),
        entry.get("life"),
    )
    es = _pick(
        st.get("EnergyShield"),
        core.get("EnergyShield"),
        dfn.get("EnergyShield"),
        dig.get("energyShield"),
        summ.get("energyShield"),
        entry.get("energyShield"),
    )
    phys_max = _pick(
        st.get("PhysicalMaximumHitTaken"),
        core.get("PhysicalMaximumHitTaken"),
        dfn.get("PhysicalMaximumHitTaken"),
        dig.get("physMaxHit"),
        summ.get("physMaxHit"),
    )
    ele_max = _pick(
        dig.get("eleMaxHit"),
        summ.get("eleMaxHit"),
        entry.get("eleMaxHit"),
        _safe_min_ele_max_hit(st) if st else None,
        _safe_min_ele_max_hit(core) if core else None,
        _safe_min_ele_max_hit(dfn) if dfn else None,
        _safe_min_ele_max_hit(summ) if summ else None,
        _safe_min_ele_max_hit(dig) if dig else None,
        _safe_min_ele_max_hit(entry),
    )
    chaos_max = _pick(
        st.get("ChaosMaximumHitTaken"),
        core.get("ChaosMaximumHitTaken"),
        dfn.get("ChaosMaximumHitTaken"),
        dig.get("chaosMaxHit"),
        summ.get("chaosMaxHit"),
    )
    return {
        "primaryDpsKey": primary_key,
        "primaryDps": primary_dps,
        "totalEhp": total_ehp,
        "averageDamage": avg_hit,
        "speed": speed,
        "life": life,
        "energyShield": es,
        "physMaxHit": phys_max,
        "eleMaxHit": ele_max,
        "chaosMaxHit": chaos_max,
    }


def format_matrix_markdown(res: dict[str, Any], *, target: str = "main") -> str:
    """Format `pob matrix` results for both compact (default) and `--full` payloads."""
    warn = restore_warning(res.get("restoreCheck"))
    sc_list = res.get("scenarios") or []
    if not sc_list:
        return f"{warn}\n_(No matrix scenarios evaluated)_" if warn else "_(No matrix scenarios evaluated)_"

    base_m = _extract_metrics_block(sc_list[0])
    base_dps = base_m["primaryDps"]
    base_ehp = base_m["totalEhp"]
    p_key = str(res.get("primaryDpsKey") or base_m["primaryDpsKey"] or "CombinedDPS")

    rows: list[list[Any]] = []
    kept: list[str] = []
    rejected: list[str] = []
    for idx, sc in enumerate(sc_list):
        m = _extract_metrics_block(sc)
        p_dps = m["primaryDps"]
        ehp = m["totalEhp"]
        deltas = sc.get("deltasVsFirst") if isinstance(sc.get("deltasVsFirst"), dict) else {}
        if idx == 0:
            dps_delta = "Baseline"
            ehp_delta = "Baseline"
        else:
            dps_delta = (
                deltas.get("primaryDpsStr")
                or deltas.get("dpsDeltaStr")
                or (fmt_delta(base_dps, p_dps) if base_dps is not None and p_dps is not None else "-")
            )
            ehp_delta = (
                deltas.get("totalEhpStr")
                or deltas.get("ehpDeltaStr")
                or (fmt_delta(base_ehp, ehp) if base_ehp is not None and ehp is not None else "-")
            )

        max_hit_str = (
            f"{fmt_num(m['physMaxHit'])} / "
            f"{fmt_num(m['eleMaxHit'])} / "
            f"{fmt_num(m['chaosMaxHit'])}"
        )
        speed_val = m["speed"]
        rows.append(
            [
                sc.get("name") or "-",
                fmt_num(p_dps),
                dps_delta,
                fmt_num(m["averageDamage"]),
                f"{fmt_num(speed_val, 2)}/s" if speed_val is not None else "-",
                fmt_num(ehp),
                ehp_delta,
                f"{fmt_num(m['life'])} / {fmt_num(m['energyShield'])}",
                max_hit_str,
            ]
        )

        st_info = (sc.get("applied") or {}).get("stripped") or {}
        if isinstance(st_info, dict):
            for k in st_info.get("kept") or []:
                if k not in kept:
                    kept.append(k)
            for r in st_info.get("keepRejected") or []:
                if r not in rejected:
                    rejected.append(r)

    header = f"### Scenario Matrix Comparison (`{target}`, Baseline: `{res.get('baseline')}`, Primary: `{p_key}`)"
    table = render_table(
        ["Scenario", p_key, "DPS Delta", "Avg Hit", "Speed", "TotalEHP", "EHP Delta", "Life / ES", "MaxHit (P/E/C)"],
        rows,
    )
    parts: list[str] = []
    if warn:
        parts.append(warn)
    parts.append(header)
    parts.append(table)
    if kept:
        parts.append("\n**Kept (--keep)**: " + "; ".join(kept))
    rej_lines = keep_rejected_warning(rejected)
    if rej_lines:
        parts.append("\n" + "\n".join(rej_lines))
    return "\n".join(parts)


def format_sweep_markdown(result: dict[str, Any], *, target: str | None = None) -> str:
    """Format `pob sweep` variant comparison results into a concise Markdown table."""
    warn = restore_warning(result.get("restoreCheck"))
    variants = result.get("variants") or result.get("results") or []
    if not variants:
        return f"{warn}\n_(No sweep variants evaluated)_" if warn else "_(No sweep variants evaluated)_"

    base_entry = result.get("baseline") or result.get("base") or {}
    base_m = _extract_metrics_block(base_entry) if isinstance(base_entry, dict) and base_entry else None
    if base_m is None or base_m["primaryDps"] is None:
        first_ok = next((v for v in variants if v.get("ok", True) and not v.get("error")), None)
        base_m = _extract_metrics_block(first_ok) if first_ok else _extract_metrics_block({})

    base_dps = base_m["primaryDps"]
    base_ehp = base_m["totalEhp"]
    p_key = str(result.get("primaryDpsKey") or base_m["primaryDpsKey"] or "CombinedDPS")

    calc_keys: list[str] = []
    for k in result.get("calcKeywords") or result.get("calc_keywords") or []:
        ks = str(k).strip()
        if ks and ks not in calc_keys:
            calc_keys.append(ks)
    if not calc_keys:
        for v in variants:
            calcs = v.get("calcs")
            if isinstance(calcs, dict):
                for ck in calcs.keys():
                    if ck not in calc_keys:
                        calc_keys.append(str(ck))

    headers = ["Variant", "Status", p_key, "DPS Delta", "TotalEHP", "EHP Delta", "Life / ES", "MaxHit (P/E/C)"]
    headers.extend(calc_keys[:4])

    rows: list[list[Any]] = []
    errors: list[tuple[str, str]] = []

    if isinstance(base_entry, dict) and base_entry and base_m["primaryDps"] is not None:
        b_mh = f"{fmt_num(base_m['physMaxHit'])} / {fmt_num(base_m['eleMaxHit'])} / {fmt_num(base_m['chaosMaxHit'])}"
        b_row: list[Any] = [
            base_entry.get("name") or "(Baseline)",
            "Base",
            fmt_num(base_m["primaryDps"]),
            "Baseline",
            fmt_num(base_m["totalEhp"]),
            "Baseline",
            f"{fmt_num(base_m['life'])} / {fmt_num(base_m['energyShield'])}",
            b_mh,
        ]
        b_calcs = base_entry.get("calcs") if isinstance(base_entry.get("calcs"), dict) else {}
        for ck in calc_keys[:4]:
            b_row.append(_format_calc_cell(b_calcs.get(ck)))
        rows.append(b_row)

    for v in variants:
        v_name = str(v.get("name") or "-")
        if not v.get("ok", True) or v.get("error"):
            err_msg = str(v.get("error") or "failed")
            errors.append((v_name, err_msg))
            err_row: list[Any] = [v_name, "ERROR", "-", err_msg[:48], "-", "-", "-", "-"]
            err_row.extend(["-"] * len(calc_keys[:4]))
            rows.append(err_row)
            continue

        m = _extract_metrics_block(v)
        deltas = v.get("deltas") if isinstance(v.get("deltas"), dict) else {}
        dps_delta = (
            deltas.get("primaryDpsStr")
            or deltas.get("dpsDeltaStr")
            or (fmt_delta(base_dps, m["primaryDps"]) if base_dps is not None and m["primaryDps"] is not None else "-")
        )
        ehp_delta = (
            deltas.get("totalEhpStr")
            or deltas.get("ehpDeltaStr")
            or (fmt_delta(base_ehp, m["totalEhp"]) if base_ehp is not None and m["totalEhp"] is not None else "-")
        )
        mh = f"{fmt_num(m['physMaxHit'])} / {fmt_num(m['eleMaxHit'])} / {fmt_num(m['chaosMaxHit'])}"
        row: list[Any] = [
            v_name,
            "OK",
            fmt_num(m["primaryDps"]),
            dps_delta,
            fmt_num(m["totalEhp"]),
            ehp_delta,
            f"{fmt_num(m['life'])} / {fmt_num(m['energyShield'])}",
            mh,
        ]
        v_calcs = v.get("calcs") if isinstance(v.get("calcs"), dict) else {}
        for ck in calc_keys[:4]:
            row.append(_format_calc_cell(v_calcs.get(ck)))
        rows.append(row)

    meta_bits = []
    if target:
        meta_bits.append(f"Slot: `{target}`")
    raw_sc = result.get("scenario")
    if raw_sc:
        sc_label = (raw_sc.get("name") or raw_sc.get("base") or "Custom") if isinstance(raw_sc, dict) else str(raw_sc)
        meta_bits.append(f"Scenario: `{sc_label}`")
    meta_bits.append(f"Variants: `{len(variants)}`")
    meta_bits.append(f"Primary: `{p_key}`")
    lines: list[str] = []
    if warn:
        lines.append(warn)
    lines.extend([f"### Build Variant Sweep ({', '.join(meta_bits)})", render_table(headers, rows)])

    sc_applied = result.get("scenarioApplied")
    st_info = sc_applied.get("stripped") if isinstance(sc_applied, dict) else None
    if isinstance(st_info, dict):
        kept = st_info.get("kept") or []
        if kept:
            lines.append("\n**Kept (--keep)**: " + "; ".join(kept))
        rej_lines = keep_rejected_warning(st_info)
        if rej_lines:
            lines.append("\n" + "\n".join(rej_lines))

    if errors:
        lines.append("\n#### Variant Errors")
        for ename, emsg in errors:
            lines.append(f"- `{ename}`: {emsg}")
    return "\n".join(lines)


def _format_calc_cell(val: Any) -> str:
    if val is None:
        return "-"
    if isinstance(val, (int, float, str)):
        return fmt_num(val)
    if isinstance(val, dict):
        values = val.get("values")
        if isinstance(values, list) and values:
            for item in values:
                formatted = _format_calc_cell(item)
                if formatted and formatted != "-":
                    return formatted
        for k in ("raw", "text", "value", "display"):
            if val.get(k) is not None and str(val.get(k)).strip() != "":
                return fmt_num(val[k])
        return "-"
    if isinstance(val, list):
        parts = [_format_calc_cell(x) for x in val[:2]]
        return "; ".join(p for p in parts if p and p != "-") or "-"
    return str(val)[:40]


def _format_path(path_val: Any) -> str:
    if not path_val:
        return "-"
    if isinstance(path_val, list):
        parts = []
        for item in path_val:
            if isinstance(item, dict):
                nid = item.get("id")
                nm = strip_pob_escapes(item.get("nameZh") or item.get("name") or "")
                parts.append(f"{nid}({nm})" if nm else str(nid))
            else:
                parts.append(str(item))
        return " -> ".join(parts)
    return str(path_val)


def _format_mastery_effects_lines(node: dict[str, Any]) -> list[str]:
    effects = node.get("masteryEffects") or node.get("effects") or []
    if not effects:
        return []
    nid = node.get("id")
    nname = strip_pob_escapes(node.get("nameZh") or node.get("name") or f"Mastery {nid}")
    sel_id = node.get("selectedEffect") or node.get("selectedEffectId") or node.get("effectId")
    out = [f"\n##### Mastery `{nid}` — {nname}"]
    for eff in effects:
        if isinstance(eff, dict):
            eid = eff.get("effect") or eff.get("effectId") or eff.get("id")
            stats_list = eff.get("statsZh") or eff.get("stats") or []
            if isinstance(stats_list, str):
                stats_txt = stats_list
            else:
                stats_txt = "; ".join(strip_pob_escapes(str(s)) for s in stats_list)
            marker = " **[SELECTED]**" if (sel_id is not None and str(eid) == str(sel_id)) else ""
            out.append(f"- `{nid}:{eid}`{marker}: {stats_txt or '-'}")
        else:
            out.append(f"- {strip_pob_escapes(str(eff))}")
    return out


def format_tree_markdown(
    res: dict[str, Any],
    *,
    search_label: str | None = None,
    show_path: bool = False,
    masteries: bool = False,
) -> str:
    """Format passive tree inspection, search/filter, mastery effect lists, and socket radius queries."""
    # Socket radius view
    sr = res.get("socketRadius") or res.get("socket")
    if isinstance(sr, dict) and (sr.get("nodes") is not None or sr.get("radii") is not None or res.get("nearbyNodes") is not None):
        sock_id = sr.get("id") or sr.get("nodeId") or "-"
        sock_name = strip_pob_escapes(sr.get("nameZh") or sr.get("name") or f"Socket {sock_id}")
        lines = [f"### Jewel Socket Radius: `{sock_id}` ({sock_name})"]
        radii_map = sr.get("radii")
        if isinstance(radii_map, dict):
            for r_name, r_nodes in radii_map.items():
                if not r_nodes:
                    continue
                lines.append(f"\n#### Radius `{r_name}` ({len(r_nodes)} nodes)")
                rows = [
                    [
                        n.get("id"),
                        strip_pob_escapes(n.get("nameZh") or n.get("name")),
                        n.get("type") or "Normal",
                        "Yes" if n.get("allocated") else "No",
                        "; ".join(str(s) for s in (n.get("statsZh") or n.get("stats") or [])[:2])[:80],
                    ]
                    for n in r_nodes
                ]
                lines.append(render_table(["Node ID", "Name", "Type", "Allocated", "Stats"], rows))
        else:
            nearby = sr.get("nodes") or res.get("nearbyNodes") or res.get("matches") or []
            rows = [
                [
                    n.get("id"),
                    strip_pob_escapes(n.get("nameZh") or n.get("name")),
                    n.get("type") or "Normal",
                    "Yes" if n.get("allocated") else "No",
                    n.get("radius") or n.get("ring") or (fmt_num(n.get("distance"), 1) if n.get("distance") is not None else "-"),
                    "; ".join(str(s) for s in (n.get("statsZh") or n.get("stats") or [])[:2])[:80],
                ]
                for n in nearby
            ]
            lines.append(render_table(["Node ID", "Name", "Type", "Allocated", "Radius", "Stats"], rows))
        return "\n".join(lines)

    # Search / filtered matches view
    matches = res.get("matches")
    if search_label or matches is not None:
        matches = matches or []
        has_path = show_path or any(m.get("path") for m in matches)
        headers = ["Node ID", "Name", "Type", "Allocated", "Path Cost"]
        if has_path:
            headers.append("Path")
        headers.append("Stats")

        rows = []
        mastery_nodes: list[dict[str, Any]] = []
        for m in matches:
            stats_s = "; ".join(str(s) for s in (m.get("statsZh") or m.get("stats") or [])[:2])
            row: list[Any] = [
                m.get("id"),
                strip_pob_escapes(m.get("nameZh") or m.get("name")),
                m.get("type") or "Normal",
                "Yes" if m.get("allocated") else "No",
                m.get("cost") if m.get("cost") is not None else "-",
            ]
            if has_path:
                row.append(_format_path(m.get("path")))
            row.append(stats_s[:80])
            rows.append(row)
            if (masteries or m.get("masteryEffects")) and (m.get("masteryEffects") or m.get("effects")):
                mastery_nodes.append(m)

        label = search_label or ", ".join(str(q) for q in (res.get("queries") or [])) or "Filter"
        lines = [
            f"### Passive Tree Search: `{label}` ({res.get('totalMatches', len(matches))} matches)",
            render_table(headers, rows),
        ]
        for mn in mastery_nodes:
            lines.extend(_format_mastery_effects_lines(mn))
        return "\n".join(lines)

    # Default allocated passive tree summary
    pts = res.get("points") or {}
    lines = [
        f"### Passive Tree Summary: `{pts.get('used', 0)}/{pts.get('usedMax', 0)}` points | Ascendancy `{pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)}`",
    ]
    for title, key in (
        ("Keystones", "keystones"),
        ("Ascendancy Nodes", "ascendancyNodes"),
        ("Masteries", "masteries"),
        ("Notables", "notables"),
    ):
        items = res.get(key) or []
        lines.append(f"\n#### {title} ({len(items)})")
        rows = [
            [
                n["id"],
                strip_pob_escapes(n.get("nameZh") or n.get("name")),
                "; ".join(str(s) for s in (n.get("statsZh") or n.get("stats") or [])[:2]),
            ]
            for n in items
        ]
        lines.append(render_table(["ID", "Name", "Stats"], rows))
        if key == "masteries" and (masteries or any(n.get("masteryEffects") for n in items)):
            for mn in items:
                lines.extend(_format_mastery_effects_lines(mn))

    return "\n".join(lines)
