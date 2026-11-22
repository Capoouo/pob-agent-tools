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
        return tab_fn(cleaned_rows, headers=headers, tablefmt="github", disable_numparse=True)
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


LARGE_JSON_STDOUT_BYTES = 16384


def emit_json(
    data: Any,
    *,
    compact: bool = False,
    out: str | Path | None = None,
    sanitize: bool = True,
    bloat_guard: bool = True,
) -> str:
    """Serialize RFC-8259 JSON safely and write either to a UTF-8 no-BOM file (`out`) or stdout.

    When writing to stdout (`out is None`) and payload exceeds 16 KB, auto-compacts to single-line
    JSON and emits a concise stderr hint so shell/LLM callers don't flood long conversation contexts.
    """
    text = to_json_str(data, sanitize=sanitize, compact=compact)
    if out is not None:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8", newline="\n")
        return text

    if bloat_guard:
        orig_bytes = len(text.encode("utf-8"))
        if orig_bytes > LARGE_JSON_STDOUT_BYTES:
            orig_kb = orig_bytes / 1024.0
            if not compact:
                text = to_json_str(data, sanitize=sanitize, compact=True)
                new_kb = len(text.encode("utf-8")) / 1024.0
                hint = (
                    f"[pob hint] Large JSON output ({orig_kb:.1f} KB > 16 KB) auto-compacted to single line ({new_kb:.1f} KB). "
                    "Prefer default Markdown (omit --json) or pass --out <file> / --compact to keep context lean.\n"
                )
            else:
                hint = (
                    f"[pob hint] Large JSON output ({orig_kb:.1f} KB > 16 KB). "
                    "Prefer default Markdown (omit --json) or pass --out <file> to keep context lean.\n"
                )
            try:
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except Exception:
                pass
            sys.stderr.write(hint)
            sys.stderr.flush()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    return text


def format_recency_stamp(
    res: dict[str, Any] | None = None,
    *,
    slot: str | None = None,
    patch: str | None = None,
    pob_version: str | None = None,
    data_version: str | None = None,
    tree_version: str | None = None,
    rev: int | None = None,
) -> str:
    """Build a compact 1-line recency/game anchor banner for multi-turn anti-hallucination."""
    res_d = res if isinstance(res, dict) else {}
    env_d = res_d.get("env") if isinstance(res_d.get("env"), dict) else {}
    info_d = res_d.get("buildInfo") if isinstance(res_d.get("buildInfo"), dict) else {}

    eff_tree = tree_version or env_d.get("treeVersion") or info_d.get("treeVersion")
    eff_patch = patch or env_d.get("patch")
    if not eff_patch:
        tv_s = str(eff_tree or "")
        eff_patch = tv_s.replace("_", ".") if tv_s.startswith("3_") else "3.30"
    eff_pob = pob_version or env_d.get("pobVersion")
    eff_data = data_version or env_d.get("dataVersion")
    eff_slot = slot or env_d.get("slot") or res_d.get("targetSlot") or "main"

    eff_rev = rev
    if eff_rev is None:
        cand_rev = env_d.get("rev") if "rev" in env_d else res_d.get("rev")
        if isinstance(cand_rev, int) and not isinstance(cand_rev, bool):
            eff_rev = cand_rev

    parts = [f"Env: PoE1 {eff_patch} (NOT PoE2)"]
    if eff_pob and str(eff_pob) not in ("-", "unknown"):
        parts.append(f"PoB {eff_pob}")
    if eff_data and str(eff_data) not in ("-", "unknown"):
        parts.append(f"Data {eff_data}")
    if eff_tree and str(eff_tree) not in ("-", "unknown"):
        parts.append(f"Tree {eff_tree}")
    slot_part = f"Slot: {eff_slot} (rev={eff_rev})" if isinstance(eff_rev, int) and not isinstance(eff_rev, bool) else f"Slot: {eff_slot}"
    parts.append(slot_part)
    return "[" + " | ".join(parts) + "]"


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


def format_compare_markdown(
    res: dict[str, Any],
    *,
    target: str = "main",
    ref_target: str = "ref",
    compact: bool = False,
) -> str:
    """Format `pob compare` (3 baselines + tree/skill/equipment diffs) into concise Markdown."""
    m_info = res.get("mainBuildInfo") or {}
    r_info = res.get("refBuildInfo") or {}
    lines = [
        f"### A/B Build Comparison: `{target}` (Lv.{m_info.get('level')} {strip_pob_escapes(m_info.get('ascendClassNameZh') or m_info.get('ascendClassName') or '-')}) vs `{ref_target}` (Lv.{r_info.get('level')} {strip_pob_escapes(r_info.get('ascendClassNameZh') or r_info.get('ascendClassName') or '-')})",
    ]

    baselines = res.get("baselines") or {}
    b_sections = (
        (
            ("Baseline 1: Original Panels", baselines.get("original", [])),
            ("Baseline 3: Both Dewatered + Pinnacle Boss (True Baseline)", (baselines.get("pinnacleDewatered") or {}).get("rows", [])),
        )
        if compact
        else (
            ("Baseline 1: Original Panels (As Configured)", baselines.get("original", [])),
            (f"Baseline 2: External Target Environment Aligned to `{target}`", (baselines.get("envAlignedToMain") or {}).get("rows", [])),
            ("Baseline 3: Both Dewatered + Pinnacle Boss (True Baseline)", (baselines.get("pinnacleDewatered") or {}).get("rows", [])),
        )
    )
    if compact and (baselines.get("envAlignedToMain") or {}).get("rows"):
        b_sections = (
            ("Baseline 1: Original Panels", baselines.get("original", [])),
            (f"Baseline 2: Env Aligned to `{target}`", (baselines.get("envAlignedToMain") or {}).get("rows", [])),
            ("Baseline 3: Both Dewatered + Pinnacle Boss (True Baseline)", (baselines.get("pinnacleDewatered") or {}).get("rows", [])),
        )
    _COMPACT_COMPARE_LABELS = frozenset(
        {
            "Full DPS",
            "Combined DPS",
            "Effective HP (TotalEHP)",
            "Maximum Life",
            "Energy Shield",
            "Phys Max Hit",
            "Fire Max Hit",
            "Cold Max Hit",
            "Lightning Max Hit",
            "Chaos Max Hit",
        }
    )
    for title, rows in b_sections:
        lines.append(f"\n#### {title}")
        use_rows = (
            [r for r in rows if r.get("label") in _COMPACT_COMPARE_LABELS] or rows
            if compact
            else rows
        )
        t_rows = [[r["label"], r["mainStr"], r["refStr"], r["diffRefVsMainStr"]] for r in use_rows]
        lines.append(render_table(["Metric", target, ref_target, f"{ref_target} vs {target}"], t_rows))

    td = res.get("treeDiff") or {}
    ks = td.get("keystones") or {}
    asc_d = td.get("ascendancy") or {}
    lines.append("\n#### Passive Tree Diff")
    lines.append(
        f"- **Keystones Only in `{target}`**: {', '.join(x['name'] for x in ks.get('onlyInMain', [])) or '-'}\n"
        f"- **Keystones Only in `{ref_target}`**: {', '.join(x['name'] for x in ks.get('onlyInRef', [])) or '-'}\n"
        f"- **Ascendancy Only in `{target}`**: {', '.join(x['name'] for x in (asc_d.get('onlyInMain') or [])[:8]) or '-'}\n"
        f"- **Ascendancy Only in `{ref_target}`**: {', '.join(x['name'] for x in (asc_d.get('onlyInRef') or [])[:8]) or '-'}\n"
        f"- **Notables Shared**: `{(td.get('notables') or {}).get('sharedCount', 0)}` | **Only in `{target}`**: `{len((td.get('notables') or {}).get('onlyInMain', []))}` | **Only in `{ref_target}`**: `{len((td.get('notables') or {}).get('onlyInRef', []))}`"
    )

    sd = res.get("skillDiff") or {}
    m_mg = sd.get("mainGroupMain") or {}
    r_mg = sd.get("refGroupMain") or {}
    m_mg_lbl = strip_pob_escapes(m_mg.get("displayLabelZh") or m_mg.get("displayLabel") or "-")
    r_mg_lbl = strip_pob_escapes(r_mg.get("displayLabelZh") or r_mg.get("displayLabel") or "-")
    lines.append("\n#### Skill & Aura Diff")
    lines.append(
        f"- **`{target}` Primary Skill Group**: `#{m_mg.get('index', '-')}` {m_mg_lbl}\n"
        f"- **`{ref_target}` Primary Skill Group**: `#{r_mg.get('index', '-')}` {r_mg_lbl}\n"
        f"- **Active Skills Only in `{target}`**: {', '.join(sd.get('activeSkillsOnlyInMain', [])) or '-'}\n"
        f"- **Active Skills Only in `{ref_target}`**: {', '.join(sd.get('activeSkillsOnlyInRef', [])) or '-'}\n"
        f"- **Shared Active Skills**: {', '.join(sd.get('sharedActiveSkills', [])) or '-'}"
    )

    slot_diff = res.get("itemSlotDiff") or []
    if compact:
        slot_diff = [s for s in slot_diff if s.get("mainItem") != s.get("refItem")]
    if slot_diff:
        lines.append("\n#### Equipment Slot Diff")
        slot_rows = [[s["slot"], s["mainItem"], s["refItem"]] for s in slot_diff]
        lines.append(render_table(["Slot", f"{target} Item", f"{ref_target} Item"], slot_rows))

    return "\n".join(lines)


def format_diagnose_markdown(res: dict[str, Any], *, compact: bool = False) -> str:
    """Render a low-token, cohesive one-shot Markdown report for `pob diagnose`."""
    if res.get("mode") == "compare" and isinstance(res.get("comparison"), dict):
        return format_diagnose_compare_markdown(res)

    env = res.get("env") or {}
    env_banner = format_recency_stamp(
        res,
        slot=env.get("slot") or res.get("targetSlot") or "main",
        patch=env.get("patch") or "3.30",
        pob_version=env.get("pobVersion"),
        data_version=env.get("dataVersion"),
        tree_version=env.get("treeVersion"),
        rev=env.get("rev") if env.get("rev") is not None else res.get("rev"),
    )
    lines: list[str] = [env_banner]

    u_warn = render_update_warning(res.get("updateWarning"))
    if u_warn:
        lines.append(u_warn.strip())
    r_warn = restore_warning(res.get("restoreCheck"))
    if r_warn:
        lines.append(r_warn)

    ask_g = res.get("askGrounding")
    if isinstance(ask_g, dict) and ask_g.get("query"):
        slang_list = ask_g.get("resolvedSlang") or []
        slang_note = (
            " | Slang: " + ", ".join(f"{s['slang']}={s['canonical']}" for s in slang_list[:6])
            if slang_list
            else ""
        )
        added_ent = ask_g.get("addedEntities") or []
        ent_note = f" | Grounded: {', '.join(added_ent[:6])}" if added_ent else ""
        lines.append(f"> **[Question Grounding]** `{str(ask_g['query'])[:120]}`{slang_note}{ent_note}")

    info = res.get("buildInfo") or {}
    pts = info.get("points") or {}
    mg = res.get("mainGroup") or {}
    cls_str = strip_pob_escapes(info.get("classNameZh") or info.get("className") or "-")
    asc_str = strip_pob_escapes(info.get("ascendClassNameZh") or info.get("ascendClassName") or "-")
    mg_lbl = strip_pob_escapes(mg.get("displayLabelZh") or mg.get("displayLabel") or "-")
    gems_str = ", ".join(
        f"{strip_pob_escapes(g.get('nameZh') or g.get('name') or '')} ({g.get('level')}/{g.get('quality')}%)"
        for g in (mg.get("gems") or [])
        if g.get("enabled", True)
    )
    lines.append(f"\n### Build Diagnosis: Lv.{info.get('level') or '-'} {cls_str} / {asc_str}")
    lines.append(
        f"- **Points**: `{pts.get('used', 0)}/{pts.get('usedMax', 0)}` (Asc `{pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)}`) | "
        f"**Main Skill**: `#{mg.get('index', '-')} {mg_lbl}` (`{mg.get('slot') or '-'}`)"
    )
    amulet_7l = res.get("amulet_7th_link") or res.get("amulet7thLink") or []
    amulet_7l_str = ", ".join(strip_pob_escapes(str(x)) for x in amulet_7l if str(x).strip())
    amulet_7l_suffix = f" + [Amulet 7L: {amulet_7l_str}]" if amulet_7l_str else ""
    if gems_str:
        lines.append(f"- **Linked Gems**: {gems_str}{amulet_7l_suffix}")
    elif amulet_7l_str:
        lines.append(f"- **Linked Gems**: [Amulet 7L: {amulet_7l_str}]")
    ks_list = res.get("keystones") or []
    if ks_list:
        lines.append(f"- **Keystones**: {', '.join(ks_list[:10])}")
    panth = res.get("pantheon") or {}
    if isinstance(panth, dict) and (panth.get("major") or panth.get("minor")):
        p_parts: list[str] = []
        if panth.get("major"):
            mh = f" ({panth['majorHint']})" if panth.get("majorHint") else ""
            p_parts.append(f"Major `{panth['major']}`{mh}")
        if panth.get("minor"):
            nh = f" ({panth['minorHint']})" if panth.get("minorHint") else ""
            p_parts.append(f"Minor `{panth['minor']}`{nh}")
        lines.append(f"- **Pantheon**: {' | '.join(p_parts)}")

    # Allocated Ascendancy & Bloodline Nodes (with stats)
    asc_nodes = res.get("ascendancyNodes") or []
    if asc_nodes:
        lines.append("- **Allocated Ascendancy & Bloodline Nodes**:")
        for an in asc_nodes[: (10 if compact else 16)]:
            an_zh = strip_pob_escapes(str(an.get("nameZh") or "")).strip()
            an_en = strip_pob_escapes(str(an.get("name") or "")).strip()
            lbl = f"{an_zh} ({an_en})" if (an_zh and an_en and an_zh != an_en) else (an_zh or an_en or str(an.get("id")))
            asc_tag = f" [{an['asc']}]" if an.get("asc") else ""
            st_list = an.get("stats") or an.get("statsZh") or []
            st_txt = "; ".join(str(s) for s in st_list[:4])
            lines.append(f"  - `{an.get('id')}` **{lbl}**{asc_tag}: {st_txt}")

    # 1. Original vs Dewatered Pinnacle Baseline
    orig = res.get("original") or {}
    dew = res.get("dewateredPinnacle") or {}
    p_key = orig.get("primaryDpsKey") or "CombinedDPS"
    lines.append("\n#### 1. Panel vs True Pinnacle Boss Baseline (Dewatered)")
    comp_rows = [
        [f"Primary DPS ({p_key})", fmt_num(orig.get("primaryDps")), fmt_num(dew.get("primaryDps")), fmt_delta(orig.get("primaryDps"), dew.get("primaryDps"))],
        ["Effective HP (TotalEHP)", fmt_num(orig.get("totalEhp")), fmt_num(dew.get("totalEhp")), fmt_delta(orig.get("totalEhp"), dew.get("totalEhp"))],
        ["Phys Max Hit", fmt_num(orig.get("physMaxHit")), fmt_num(dew.get("physMaxHit")), fmt_delta(orig.get("physMaxHit"), dew.get("physMaxHit"))],
        ["Elemental Max Hit (Min)", fmt_num(orig.get("eleMaxHit")), fmt_num(dew.get("eleMaxHit")), fmt_delta(orig.get("eleMaxHit"), dew.get("eleMaxHit"))],
        ["Chaos Max Hit", fmt_num(orig.get("chaosMaxHit")), fmt_num(dew.get("chaosMaxHit")), fmt_delta(orig.get("chaosMaxHit"), dew.get("chaosMaxHit"))],
    ]
    lines.append(render_table(["Metric", "Original Panel", "Dewatered Pinnacle", "Water Delta"], comp_rows))

    # Compact Defence & Sustain snapshot line so model doesn't need extra `pob summary` calls
    dfn = res.get("defence") or {}
    off = res.get("offence") or {}
    if dfn:
        life_n = _parse_numeric_or_none(dfn.get("Life"))
        cmh_n = _parse_numeric_or_none(orig.get("chaosMaxHit"))
        is_ci_disp = life_n is not None and life_n <= 1.0 and cmh_n is not None and math.isinf(cmh_n)
        chaos_res_disp = "100(CI)" if is_ci_disp else fmt_num(dfn.get("ChaosResist"), 0)
        life_unres_p = (
            f" (Unres `{fmt_num(dfn.get('LifeUnreserved'))}`/{fmt_num(dfn.get('LifeUnreservedPercent'), 0)}%)"
            if dfn.get("LifeUnreservedPercent") is not None and (_parse_numeric_or_none(dfn.get("LifeUnreservedPercent")) or 100.0) < 99.0
            else ""
        )
        lines.append(
            f"- **Core Pool & Res**: Life `{fmt_num(dfn.get('Life'))}`{life_unres_p} | ES `{fmt_num(dfn.get('EnergyShield'))}` | "
            f"Mana `{fmt_num(dfn.get('Mana'))}` (Unres `{fmt_num(dfn.get('ManaUnreserved'))}`) | "
            f"Res F/C/L/Ch `{fmt_num(dfn.get('FireResist'), 0)}/{fmt_num(dfn.get('ColdResist'), 0)}/{fmt_num(dfn.get('LightningResist'), 0)}/{chaos_res_disp}%` | "
            f"Supp `{fmt_num(dfn.get('EffectiveSpellSuppressionChance'), 0)}%` | "
            f"Block `{fmt_num(dfn.get('EffectiveBlockChance'), 0)}/{fmt_num(dfn.get('EffectiveSpellBlockChance'), 0)}%` | "
            f"Regen L/M `{fmt_num(dfn.get('NetLifeRegen'), 1)}/{fmt_num(dfn.get('NetManaRegen'), 1)}/s` | "
            f"ManaCost `{fmt_num(off.get('ManaCost'))}`"
        )

    top_water = res.get("topWaterSources") or []
    if top_water:
        w_rows = [
            [
                r.get("dimension") or "-",
                r.get("label") or "-",
                fmt_signed_pct(r.get("dpsDelta"), r.get("dpsPct")),
                fmt_signed_pct(r.get("totalEhpDelta"), r.get("totalEhpPct")),
            ]
            for r in top_water[: (4 if compact else 6)]
        ]
        lines.append("\n**Top Water Sources Stripped**:")
        lines.append(render_table(["Dim", "Item", f"Δ{p_key}", "ΔTotalEHP"], w_rows))

    stripped = res.get("inflationStripped") or {}
    kept = stripped.get("kept") or []
    if kept:
        lines.append("\n**Kept (--keep)**: " + "; ".join(kept))
    rej = keep_rejected_warning(stripped)
    if rej:
        lines.extend(rej)

    # 2. Hard Defensive & Mechanic Gaps
    gaps = res.get("healthGaps") or []
    lines.append("\n#### 2. Hard Defensive & Mechanic Gaps")
    if gaps:
        if compact:
            for g in gaps:
                lines.append(f"- **[{g.get('severity') or '-'}/{g.get('category') or '-'}]** {g.get('message') or '-'}")
        else:
            g_rows = [[g.get("severity") or "-", g.get("category") or "-", g.get("message") or "-"] for g in gaps]
            lines.append(render_table(["Severity", "Category", "Finding"], g_rows))
    else:
        lines.append("- No critical resistance, attribute, accuracy, mana, or point gaps detected.")

    # 2b. Core Build Synergies & Do-Not-Brick Advisories
    core_syn = (
        res.get("core_synergies")
        or res.get("coreSynergies")
        or (res.get("buildMechanics") or {}).get("coreSynergies")
        or []
    )
    if core_syn:
        lines.append("\n#### 2b. Core Build Synergies & Do-Not-Brick Advisories")
        for syn in core_syn:
            syn_txt = strip_pob_escapes(str(syn)).strip()
            if syn_txt:
                lines.append(f"- {syn_txt}")

    # 2c. Build-Specific Mechanics & PoB Modeling Notes (Triggers, 0.033s tick CDR, CwC, Tinctures, Inversion)
    bm = res.get("buildMechanics") or {}
    has_bm_section = bool(
        isinstance(bm.get("swordOfAges"), dict)
        or (bm.get("triggers") or [])
        or (isinstance(bm.get("tinctureSetup"), dict) and bm["tinctureSetup"].get("note"))
        or isinstance(bm.get("resistanceInversion"), dict)
    )
    if has_bm_section:
        lines.append("\n#### 2c. Build-Specific Mechanics & PoB Modeling")
        soa = bm.get("swordOfAges")
        if isinstance(soa, dict):
            tm = soa.get("temporalMirageCooldown") or {}
            src_str = ", ".join(
                f"{s.get('source')}: +{fmt_num(s.get('value'), 0)}%" for s in (soa.get("cdrSources") or [])
            ) or "none (0%)"
            nb = tm.get("nextBreakpoint") or {}
            next_str = (
                f"Next tick breakpoint: **`{nb.get('requiredCdrPct')}%` CDR (`+{nb.get('cdrNeededPct')}%` more)** -> "
                f"`{nb.get('targetTicks')}` ticks = **`{nb.get('effectiveCdSec')}s` (`{nb.get('effectiveCdMs')}ms`, max `{nb.get('maxTriggersPerSec')}/s`)**"
                if nb
                else "at minimum 1-tick cap"
            )
            lines.append(
                f"- **The Sword of Ages (`Temporal Mirage`, base CD `0.50s`, PoE1 server tick `0.033s = 33ms`)**: "
                f"Global CDR = **`{fmt_num(soa.get('globalCdrPct'), 0)}%`** ({src_str}) -> "
                f"raw `{tm.get('rawCdSec')}s` -> **`{tm.get('ticks')}` ticks = `{tm.get('effectiveCdSec')}s` (`{tm.get('effectiveCdMs')}ms`, max `{tm.get('maxTriggersPerSec')}/s`)**. "
                f"{next_str}."
            )
            for adv_ln in soa.get("advisories") or []:
                lines.append(f"  - {adv_ln}")
        for trig in bm.get("triggers") or []:
            if not isinstance(trig, dict):
                continue
            if trig.get("kind") == "fixed_interval_channelling":
                lines.append(f"- **Trigger ({trig.get('name')}, Group #{trig.get('groupIndex')})**: {trig.get('note')}")
            else:
                cd = trig.get("cooldown") or {}
                nb = cd.get("nextBreakpoint") or {}
                gem_cdr = trig.get("gemCdrPct") or 0
                cdr_detail = f"{fmt_num(trig.get('totalCdrPct'), 0)}% CDR" + (
                    f" (global {fmt_num(trig.get('globalCdrPct'), 0)}% + gem {gem_cdr}%)" if gem_cdr else ""
                )
                aps_part = ""
                if cd.get("aps") is not None:
                    flag = " **[OVERSPEED!]**" if cd.get("overspeed") else " [OK]"
                    aps_part = f" | Trigger APS `{cd['aps']:.2f}/s` vs cap `{cd.get('effectiveCapPerSec')}/s`{flag}"
                next_part = (
                    f" | Next: `{nb.get('requiredCdrPct')}%` CDR (`+{nb.get('cdrNeededPct')}%`) -> `{nb.get('targetTicks')}t`=`{nb.get('effectiveCdMs')}ms` (`{nb.get('effectiveCapPerSec')}/s`)"
                    if nb
                    else ""
                )
                lines.append(
                    f"- **Trigger (`{trig.get('name')}`, base `{trig.get('baseCooldownSec')}s`, `0.033s` tick)**: "
                    f"**{cdr_detail}** -> **`{cd.get('ticks')}t` = `{cd.get('effectiveCdSec')}s` (`{cd.get('effectiveCdMs')}ms`, cap `{cd.get('effectiveCapPerSec')}/s`)**{aps_part}{next_part}"
                )
        tinc = bm.get("tinctureSetup")
        if isinstance(tinc, dict) and tinc.get("note"):
            lines.append(f"- **Tincture / Bloodsoaked Blade Mechanics**: {tinc['note']}")
        inv = bm.get("resistanceInversion")
        if isinstance(inv, dict):
            lines.append(
                f"- **Resistance Inversion**: {', '.join((inv.get('sources') or [])[:3])} — {inv.get('note') or ''}"
            )

    # 2c. Compact All Skill Groups Overview (in compact compare mode, use dense 1-line bullets)
    all_groups = res.get("skillGroups") or []
    if all_groups:
        lines.append("\n#### 2c. All Skill Groups Overview")
        sg_rows: list[list[Any]] = []
        for g in all_groups:
            g_tag = f"#{g.get('index')}{'*' if g.get('isMain') else ''}"
            if not g.get("enabled", True):
                g_tag += "[OFF]"
            if g.get("includeInFullDPS"):
                g_tag += "[Full]"
            g_gems_parts = []
            for gm in g.get("gems") or []:
                nm = strip_pob_escapes(str(gm.get("nameZh") or gm.get("name") or "-"))
                lv = gm.get("level")
                ql = gm.get("quality")
                meta = f"({lv}/{ql}%)" if lv is not None else ""
                if gm.get("count") and int(gm["count"]) > 1:
                    meta += f"x{gm['count']}"
                if not gm.get("enabled", True):
                    meta += "[OFF]"
                if gm.get("fromItem"):
                    meta += "[Item]"
                g_gems_parts.append(f"{nm}{meta}")
            sg_rows.append([g_tag, g.get("slot") or "-", ", ".join(g_gems_parts) or "-"])
        if compact:
            for r in sg_rows:
                lines.append(f"- `{r[0]}` (`{r[1]}`): {r[2]}")
        else:
            lines.append(render_table(["Group", "Slot", "Gems"], sg_rows))

    # 2d. Compact Equipped Items, 3.30 Wickerman Grafts & Jewels
    eq_items = res.get("equippedItems") or []
    if eq_items:
        gear_bits: list[str] = []
        flask_bits: list[str] = []
        graft_bits: list[str] = []
        jewel_bits: list[str] = []
        for it in eq_items:
            sn = str(it.get("slot") or "-")
            nm = strip_pob_escapes(str(it.get("nameZh") or it.get("name") or "-"))
            en_nm = strip_pob_escapes(str(it.get("name") or ""))
            disp = f"{nm} ({en_nm})" if (it.get("nameZh") and en_nm and nm != en_nm and it.get("rarity") in ("UNIQUE", "RELIC")) else nm
            if it.get("isFlask"):
                st_tag = "[ON]" if it.get("active") else "[OFF]"
                t_tag = "[Tincture]" if it.get("isTincture") else ""
                flask_bits.append(f"`{sn}`{t_tag}{st_tag} {disp}")
            elif sn in ("Head", "Body", "Arm", "Leg") or str(it.get("type") or "") == "Viscera":
                graft_bits.append(f"`{sn}` {disp}")
            elif it.get("nodeId") is not None or sn.startswith("Jewel"):
                nid_tag = f"Jewel `{it.get('nodeId')}`" if it.get("nodeId") is not None else f"`{sn}`"
                jewel_bits.append(f"{nid_tag}: {disp}")
            else:
                gear_bits.append(f"`{sn}`: {disp}")
        if not compact or graft_bits or jewel_bits or flask_bits:
            lines.append("\n#### 2d. Equipped Items, Grafts & Jewels")
            if gear_bits and not compact:
                lines.append("- **Gear**: " + " | ".join(gear_bits))
            if flask_bits:
                lines.append("- **Flasks/Tinctures**: " + " | ".join(flask_bits))
            if graft_bits:
                lines.append("- **3.30 Wickerman Grafts**: " + " | ".join(graft_bits))
            if jewel_bits:
                lines.append("- **Jewels**: " + " | ".join(jewel_bits))

    # 3. Live Current-Patch Fact Cards & Advisories
    fc = res.get("factCards") or {}
    gem_cards = fc.get("gems") or []
    uniq_cards = fc.get("uniques") or []
    advisories = res.get("advisories") or []
    if gem_cards or uniq_cards or advisories:
        lines.append(f"\n#### 3. Live PoE1 {env.get('patch') or '3.30'} Fact Cards & Patch Advisories")
        for adv in advisories:
            lines.append(f"> {adv.get('tag', '[POE1-TRAP-WARN]')} `{adv.get('trigger')}`: {adv.get('message')}")
        for gc in gem_cards[: (4 if compact else 8)]:
            zh_p = f" ({gc['zhName']})" if gc.get("zhName") else ""
            is_sup_card = bool(gc.get("isSupport") or gc.get("is_support"))
            bits: list[str] = []
            bits.append("Support" if is_sup_card else "Active")
            if gc.get("castTime") is not None:
                bits.append(f"Cast={gc['castTime']}s")
            cd_card = _parse_numeric_or_none(gc.get("cooldown"))
            if cd_card is not None and cd_card > 0:
                su_raw = gc.get("stored_uses") if gc.get("stored_uses") is not None else gc.get("storedUses")
                try:
                    su_int = int(su_raw) if su_raw is not None else 0
                except (TypeError, ValueError):
                    su_int = 0
                cd_lbl = f"CD={cd_card:g}s" + (f" ({su_int} uses)" if su_int > 1 else "")
                bits.append(cd_lbl)
            if gc.get("tags"):
                bits.append(f"Tags={gc['tags']}")
            if gc.get("legacy_removed_328") or gc.get("legacyRemoved328"):
                bits.append("**[LEGACY REMOVED 3.28+]**")
            elif gc.get("is_exceptional") or gc.get("isExceptional"):
                nat_lv = gc.get("natural_max_level") or gc.get("naturalMaxLevel")
                cor_lv = gc.get("corrupted_max_level") or gc.get("corruptedMaxLevel")
                if nat_lv is not None and cor_lv is not None:
                    bits.append(f"[Exceptional | MaxLv {nat_lv}/{cor_lv}]")
                else:
                    bits.append("[Exceptional]")
            lv_stats = list((gc.get("lv20Stats") or {}).items())[:4]
            if lv_stats:
                bits.append("Lv20: " + ", ".join(f"{k}={v}" for k, v in lv_stats))
            lines.append(f"- **[Gem] {gc.get('name')}{zh_p}**: " + " | ".join(bits))
        for uc in uniq_cards[: (5 if compact else 10)]:
            zh_p = f" ({uc['zhName']})" if uc.get("zhName") else ""
            mods_preview = "; ".join((uc.get("explicits") or [])[:4])
            lines.append(f"- **[Unique] {uc.get('name')}{zh_p}** (`{uc.get('baseType') or '-'}`): {mods_preview}")

    # 4. Top ROI Upgrades (Nodes & Support Gems)
    top_nodes = res.get("topNodes")
    top_gems = res.get("topGems")
    if top_nodes or top_gems:
        lines.append("\n#### 4. Top ROI Upgrades")
        if isinstance(top_nodes, dict) and (top_nodes.get("byPerPoint") or []):
            n_rows = [
                [
                    idx,
                    n.get("id"),
                    strip_pob_escapes(n.get("nameZh") or n.get("name") or "-"),
                    n.get("pathDist"),
                    strip_pob_escapes(n.get("pathPowerStr") or fmt_num(n.get("pathPower"), 2)),
                    strip_pob_escapes(n.get("powerStr") or fmt_num(n.get("power"), 2)),
                ]
                for idx, n in enumerate(top_nodes.get("byPerPoint") or [], start=1)
            ]
            lines.append(f"\n**Top Passive Nodes (`{top_nodes.get('stat')}`, max dist {top_nodes.get('maxDist')})**:")
            lines.append(render_table(["#", "Node ID", "Name", "Dist", "Gain/Pt", "Total Gain"], n_rows))
        if isinstance(top_gems, dict) and (top_gems.get("candidates") or []):
            cands = top_gems.get("candidates") or []
            has_caveats = any(gm.get("caveat") for gm in cands)
            g_rows = []
            for idx, gm in enumerate(cands, start=1):
                row_g: list[Any] = [
                    idx,
                    strip_pob_escapes(gm.get("nameZh") or gm.get("name") or "-"),
                    strip_pob_escapes(gm.get("name") or "-"),
                    fmt_num(gm.get("delta")),
                    f"{float(gm['pct']):+.1f}%" if gm.get("pct") is not None else "-",
                ]
                if has_caveats:
                    row_g.append(str(gm.get("caveat") or "-"))
                g_rows.append(row_g)
            headers_g = ["#", "Gem (ZH)", "Gem (EN)", "Delta", "Gain %"] + (["Caveat"] if has_caveats else [])
            lines.append(f"\n**Top Candidate Support Gems (`{top_gems.get('dpsField')}`, {top_gems.get('mode')})**:")
            lines.append(render_table(headers_g, g_rows))
            skipped = top_gems.get("filteredArchetypeSwaps") or []
            if skipped:
                lines.append(f"_Filtered archetype-changing / invalid supports: {', '.join(skipped[:6])}_")

    rank_errs = res.get("rankErrors") or []
    if rank_errs:
        lines.append("\n_Ranking notes_: " + "; ".join(rank_errs))

    return "\n".join(lines)


def format_diagnose_compare_markdown(res: dict[str, Any]) -> str:
    """Format `pob diagnose --compare` into a unified, compact (<8KB) lossless Markdown report."""
    target = str(res.get("target") or "main")
    ref_target = str(res.get("refTarget") or "ref")
    build_a = res.get("buildA") or {}
    build_b = res.get("buildB") or {}
    cmp_res = res.get("comparison") or {}
    env = res.get("env") or build_a.get("env") or {}

    rev_a = (build_a.get("env") or {}).get("rev") if isinstance(build_a, dict) else None
    rev_b = (build_b.get("env") or {}).get("rev") if isinstance(build_b, dict) else None
    slot_a_lbl = f"{target} (rev={rev_a})" if isinstance(rev_a, int) and not isinstance(rev_a, bool) else target
    slot_b_lbl = f"{ref_target} (rev={rev_b})" if isinstance(rev_b, int) and not isinstance(rev_b, bool) else ref_target
    parts: list[str] = [
        f"[Env: PoE1 {env.get('patch') or '3.30'} (NOT PoE2) | PoB {env.get('pobVersion') or '-'} | "
        f"Data {env.get('dataVersion') or '-'} | Slots: {slot_a_lbl} vs {slot_b_lbl}]"
    ]
    u_warn = render_update_warning(res.get("updateWarning"))
    if u_warn:
        parts.append(u_warn.strip())

    parts.append("\n## Dual-Build 3-Baseline Comparison")
    parts.append(format_compare_markdown(cmp_res, target=target, ref_target=ref_target, compact=True))

    if build_a:
        parts.append(f"\n## Build A Diagnosis (`{target}`)")
        parts.append(format_diagnose_markdown(build_a, compact=True))
    if build_b:
        parts.append(f"\n## Build B Diagnosis (`{ref_target}`)")
        parts.append(format_diagnose_markdown(build_b, compact=True))
    return "\n".join(parts)


def format_ground_markdown(res: dict[str, Any]) -> str:
    """Format `pob ground` entity & trap grounding output cleanly."""
    block = str(res.get("contextBlock") or "").strip()
    elapsed = res.get("elapsedMs")
    if elapsed is not None:
        return f"{block}\n_(grounded in {elapsed:.1f}ms)_" if block else f"_(no entities or traps matched; {elapsed:.1f}ms)_"
    return block or "_(no entities or traps matched)_"


def format_cautions(res: dict[str, Any] | list[str] | tuple[str, ...] | None) -> list[str]:
    """Extract and format mutation caution lines (`> [CAUTION] <msg>`) deduplicated in order."""
    raw_items: list[Any] = []
    if isinstance(res, (list, tuple)):
        raw_items.extend(res)
    elif isinstance(res, dict):
        if isinstance(res.get("cautions"), (list, tuple)):
            raw_items.extend(res["cautions"])
        detail = res.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("cautions"), (list, tuple)):
            raw_items.extend(detail["cautions"])
        steps = res.get("steps") or (detail.get("steps") if isinstance(detail, dict) else None) or []
        if isinstance(steps, list):
            for st in steps:
                if isinstance(st, dict) and isinstance(st.get("cautions"), (list, tuple)):
                    raw_items.extend(st["cautions"])
    seen: set[str] = set()
    out: list[str] = []
    for item in raw_items:
        msg = strip_pob_escapes(str(item or "")).strip()
        if msg and msg not in seen:
            seen.add(msg)
            out.append(f"> [CAUTION] {msg}")
    return out


def format_mutation_report(title: str, res: dict[str, Any], *, target: str | None = None) -> str:
    """Format mutation results (`tree change`, `skill set`, `skill add`, `batch`, etc.) into Markdown."""
    stamp = format_recency_stamp(res, slot=target or res.get("targetSlot"))
    lines = [stamp, f"### {title} — `{res.get('mode', '')}`"]
    before = res.get("before") or {}
    after = res.get("after") or {}
    no_undo = bool(res.get("noUndo")) or (not before and bool(after))
    p_key = after.get("primaryDpsKey") or before.get("primaryDpsKey") or "CombinedDPS"
    b_pts = before.get("points") or {}
    a_pts = after.get("points") or {}

    if no_undo:
        summary_rows = [
            [f"Primary DPS ({p_key})", "-", fmt_num(after.get("primaryDps")), "(no-undo)"],
            ["Effective HP (TotalEHP)", "-", fmt_num(after.get("totalEhp")), "(no-undo)"],
            ["Maximum Life", "-", fmt_num(after.get("life")), "(no-undo)"],
            [
                "Passive Points",
                "-",
                f"{a_pts.get('used', 0)}/{a_pts.get('usedMax', 0)} (Asc {a_pts.get('ascUsed', 0)}/{a_pts.get('ascMax', 0)})",
                "(no-undo)",
            ],
        ]
    else:
        summary_rows = [
            [
                f"Primary DPS ({p_key})",
                fmt_num(before.get("primaryDps")),
                fmt_num(after.get("primaryDps")),
                fmt_delta(before.get("primaryDps"), after.get("primaryDps")),
            ],
            [
                "Effective HP (TotalEHP)",
                fmt_num(before.get("totalEhp")),
                fmt_num(after.get("totalEhp")),
                fmt_delta(before.get("totalEhp"), after.get("totalEhp")),
            ],
            [
                "Maximum Life",
                fmt_num(before.get("life")),
                fmt_num(after.get("life")),
                fmt_delta(before.get("life"), after.get("life")),
            ],
            [
                "Passive Points",
                f"{b_pts.get('used', 0)}/{b_pts.get('usedMax', 0)} (Asc {b_pts.get('ascUsed', 0)}/{b_pts.get('ascMax', 0)})",
                f"{a_pts.get('used', 0)}/{a_pts.get('usedMax', 0)} (Asc {a_pts.get('ascUsed', 0)}/{a_pts.get('ascMax', 0)})",
                fmt_delta(b_pts.get("used", 0), a_pts.get("used", 0), " pts"),
            ],
        ]
    lines.append(render_table(["Headline Metric", "Before", "After", "Delta"], summary_rows))

    diff = res.get("diff") or {}
    deltas = diff.get("deltas") or []
    if deltas:
        lines.append("\n#### Changed Stats")
        d_rows = [[d["label"], d["beforeStr"], d["afterStr"], d["deltaStr"]] for d in deltas]
        lines.append(render_table(["Stat", "Before", "After", "Delta"], d_rows))
    elif no_undo:
        lines.append("\n_(Before/diff snapshot skipped in `--no-undo` mode)_")
    else:
        lines.append("\n_(No stat changes detected)_")

    cascade_warn = res.get("cascadeWarning") or (res.get("detail") or {}).get("cascadeWarning")
    if cascade_warn:
        lines.append(f"\n> [WARN] {cascade_warn}")

    caution_lines = format_cautions(res)
    if caution_lines:
        lines.append("")
        lines.extend(caution_lines)

    new_w = diff.get("newWarnings") or []
    res_w = diff.get("resolvedWarnings") or []
    if new_w:
        lines.append("\n#### New Warnings")
        for w in new_w:
            lines.append(f"- [NEW WARNING] {w}")
    if res_w:
        lines.append("\n#### Resolved Warnings")
        for w in res_w:
            lines.append(f"- [RESOLVED] {w}")

    return "\n".join(lines)


def format_game_data(res: dict[str, Any], kind: str | None = None, query: str | None = None) -> str:
    """Format `pob data <gem|unique|mod|base|translate>` local game data results into Markdown."""
    kind_norm = str(res.get("kind") or kind or "gem").lower()
    q_disp = str(query if query is not None else (res.get("query") or ""))
    results = res.get("results") or []
    lines: list[str] = []
    for w in res.get("warnings") or []:
        if isinstance(w, dict):
            tag = "[POE1-PATCH-WARN]" if w.get("category") == "legacy_patch" else "[POE1-TRAP-WARN]"
            canon = f" (canonical: {w['canonicalTerm']})" if w.get("canonicalTerm") else ""
            lines.append(f"> {tag} `{w.get('trigger', '')}`{canon}: {w.get('warning', '')}")
    alias = res.get("resolvedAlias")
    alias_note = f" (alias `{alias['from']}` -> `{alias['to']}`)" if isinstance(alias, dict) and alias.get("to") else ""
    lines.append(
        f"### Local Game Data (`{kind_norm}`): `{q_disp}`{alias_note} — `{res.get('count', len(results))}` hits "
        f"(PoB `{res.get('pobVersion')}` / Data `{res.get('dataVersion')}`)"
    )
    if not results:
        lines.append("_(no matches)_")
        sugs = res.get("suggestions") or []
        if sugs:
            s_rows = [
                [s.get("name") or "-", s.get("zh_name") or "-", f"{float(s.get('score') or 0):.2f}"]
                for s in sugs
            ]
            lines.append("\n#### Did You Mean")
            lines.append(render_table(["Name (EN)", "Name (ZH)", "Score"], s_rows))
        if res.get("hint"):
            lines.append(f"\n> {res['hint']}")
        return "\n".join(lines)

    if kind_norm == "translate":
        rows = [[r.get("en"), r.get("zh") or "-", r.get("category") or "-", r.get("match_type") or "-"] for r in results]
        lines.append(render_table(["English", "Chinese", "Category", "Match"], rows))
    elif kind_norm == "gem":
        for r in results:
            gem_name = r.get("display_name") or r.get("name") or "-"
            zh_nm = r.get("zh_name") or r.get("zhName")
            zh_part = f" ({zh_nm})" if zh_nm else ""
            is_sup = bool(r.get("is_support") or r.get("isSupport"))
            kind_lbl = "Support" if is_sup else "Active"
            is_legacy_328 = bool(r.get("legacy_removed_328") or r.get("legacyRemoved328"))
            is_exceptional = bool(r.get("is_exceptional") or r.get("isExceptional"))
            badges: list[str] = []
            if is_legacy_328:
                badges.append("**[LEGACY REMOVED 3.28+]**")
            elif r.get("legacyOnly"):
                badges.append(f"**[LEGACY / REMOVED IN {r.get('removedInPatch') or '3.25'}]**")
            if is_exceptional:
                nat_lv = r.get("natural_max_level") if r.get("natural_max_level") is not None else r.get("naturalMaxLevel")
                cor_lv = r.get("corrupted_max_level") if r.get("corrupted_max_level") is not None else r.get("corruptedMaxLevel")
                if nat_lv is not None and cor_lv is not None:
                    badges.append(f"[Exceptional | MaxLv {nat_lv}/{cor_lv}]")
                else:
                    badges.append("[Exceptional]")
            badge_str = (" " + " ".join(badges)) if badges else ""
            lines.append(f"\n#### {gem_name}{zh_part} — `{r.get('color') or '-'}` {kind_lbl}{badge_str}")
            if r.get("legacyNote"):
                lines.append(f"> [POE1-PATCH-WARN] {r['legacyNote']}")
            elif is_legacy_328:
                lines.append(
                    f"> [POE1-PATCH-WARN] '{gem_name}' was drop-disabled in PoE1 3.28 (Mirage) "
                    "and is unavailable in current leagues."
                )

            meta_bits: list[str] = []
            if r.get("tag_string"):
                meta_bits.append(f"Tags: {r['tag_string']}")
            if r.get("cast_time") is not None:
                meta_bits.append(f"Cast: {r['cast_time']}s")
            cd_val = _parse_numeric_or_none(r.get("cooldown"))
            if (cd_val is None or cd_val <= 0.0) and isinstance(r.get("levels"), dict):
                lvls_map = r["levels"]
                lvl_ref = lvls_map.get("20") or lvls_map.get(20) or (next(iter(lvls_map.values())) if lvls_map else {})
                if isinstance(lvl_ref, dict):
                    cd_val = _parse_numeric_or_none(lvl_ref.get("cooldown"))
            if cd_val is not None and cd_val > 0.0:
                su_raw = r.get("stored_uses") if r.get("stored_uses") is not None else r.get("storedUses")
                try:
                    su_int = int(su_raw) if su_raw is not None else 0
                except (TypeError, ValueError):
                    su_int = 0
                cd_lbl = f"CD: {cd_val:g}s" + (f" ({su_int} uses)" if su_int > 1 else "")
                meta_bits.append(cd_lbl)
            if r.get("crit_chance") is not None:
                meta_bits.append(f"Crit: {r['crit_chance']}%")
            if r.get("damage_effectiveness") is not None:
                meta_bits.append(f"DmgEff: {r['damage_effectiveness']}")
            if r.get("mana_multiplier") is not None:
                meta_bits.append(f"CostMult: {r['mana_multiplier']}%")
            if meta_bits:
                lines.append("- " + " | ".join(meta_bits))

            lvls = r.get("levels") or {}
            if lvls:
                lvl_rows = []
                for lk in sorted(lvls.keys(), key=lambda x: int(x) if str(x).isdigit() else 999):
                    lv = lvls[lk]
                    cost_s = ", ".join(f"{k}:{v}" for k, v in (lv.get("cost") or {}).items()) or "-"
                    stats_s = "; ".join(f"{k}={v}" for k, v in list((lv.get("stats") or {}).items())[:6]) or "-"
                    lvl_rows.append(
                        [
                            f"Lv.{lk}",
                            lv.get("levelRequirement") if lv.get("levelRequirement") is not None else "-",
                            cost_s,
                            lv.get("critChance") if lv.get("critChance") is not None else "-",
                            lv.get("damageEffectiveness") if lv.get("damageEffectiveness") is not None else "-",
                            stats_s[:100],
                        ]
                    )
                lines.append(render_table(["Lv", "ReqLv", "Cost", "Crit", "DmgEff", "Key Stats"], lvl_rows))

            q_stats = r.get("quality_stats") or []
            if q_stats:
                q_parts = []
                for q in q_stats[:4]:
                    pq = q.get("per_quality")
                    at20 = q.get("at_20_quality")
                    at40 = q.get("at_40_quality")
                    lbl = q.get("label")
                    if lbl:
                        q_parts.append(f"{lbl} [`{q['stat']}`, {pq}/1%, @20%={at20}, @40%={at40}]")
                    else:
                        q_parts.append(f"{q['stat']}={at20} (@20%, {pq}/1%, @40%={at40})")
                lines.append(f"- **Quality**: {'; '.join(q_parts)}")
            variants = r.get("variants") or []
            if variants:
                v_parts = []
                for v in variants[:8]:
                    v_zh = f" ({v['zh_name']})" if v.get("zh_name") else ""
                    v_parts.append(f"{v.get('name')}{v_zh} [{v.get('relation')}]")
                lines.append(f"- **Variants**: {', '.join(v_parts)}")
    elif kind_norm == "unique":
        for r in results:
            zh_part = f" ({r['zh_name']})" if r.get("zh_name") else ""
            base_zh = f" ({r['base_type_zh']})" if r.get("base_type_zh") else ""
            lines.append(f"\n#### {r.get('name')}{zh_part} — {r.get('base_type')}{base_zh} (`{r.get('item_class')}`)")
            meta = [x for x in [f"ReqLv {r['req_level']}" if r.get("req_level") else "", r.get("league") or "", r.get("source") or ""] if x]
            if meta:
                lines.append("- " + " | ".join(meta))
            matched_mods = r.get("matched_mods") or []
            if matched_mods:
                for mm in matched_mods[:4]:
                    lines.append(f"- **[Match]** {mm}")
            for imp, imp_zh in zip(r.get("implicits") or [], (r.get("implicits_zh") or []) + [None] * 10):
                lines.append(f"- [Implicit] {imp}" + (f" / {imp_zh}" if imp_zh else ""))
            for exp, exp_zh in zip(r.get("explicits") or [], (r.get("explicits_zh") or []) + [None] * 30):
                lines.append(f"- {exp}" + (f" / {exp_zh}" if exp_zh else ""))
            fb_mods = r.get("foulborn_mods") or []
            if fb_mods:
                lines.append("- **Foulborn Mutations**:")
                for fb in fb_mods[:8]:
                    st_en = "; ".join(fb.get("stats") or [])
                    st_zh = "; ".join(x for x in (fb.get("stats_zh") or []) if x)
                    lines.append(f"  - {st_en}" + (f" / {st_zh}" if st_zh else ""))
    elif kind_norm == "mod":
        rows = []
        for r in results:
            en_s = "; ".join(r.get("stats") or [])
            zh_s = "; ".join(x for x in (r.get("stats_zh") or []) if x)
            stat_disp = f"{en_s} / {zh_s}" if zh_s else en_s
            weights = r.get("weights") or {}
            bench = r.get("bench_info")
            if bench:
                cost = bench.get("cost")
                cost_s = ", ".join(f"{k}:{v}" for k, v in cost.items()) if isinstance(cost, dict) else str(cost or "")
                types_s = ",".join((bench.get("types") or [])[:5])
                w_disp = f"Bench[{types_s}]" + (f" ({cost_s})" if cost_s else "")
            else:
                w_disp = ", ".join(f"{k}:{v}" for k, v in list(weights.items())[:5]) or "-"
            affix_grp = f"{r.get('affix') or '-'} ({r.get('group') or '-'})"
            rows.append(
                [
                    r.get("source") or "-",
                    r.get("generation_type") or "-",
                    affix_grp[:40],
                    r.get("level") if r.get("level") is not None else "-",
                    stat_disp[:120],
                    w_disp[:60],
                ]
            )
        lines.append(render_table(["Source", "Type", "Affix (Group)", "iLvl", "Stats", "Weights / Bench"], rows))
    elif kind_norm == "base":
        rows = []
        for r in results:
            name_disp = f"{r.get('name')} ({r['zh_name']})" if r.get("zh_name") else r.get("name")
            req = r.get("req") or {}
            req_s = f"Lv{req.get('level', 0)}/{req.get('str', 0)}/{req.get('dex', 0)}/{req.get('int', 0)}"
            props = r.get("properties") or {}
            prop_bits = []
            for pk, pv in props.items():
                if isinstance(pv, dict):
                    prop_bits.append(f"{pk}: " + ",".join(f"{k}={v}" for k, v in list(pv.items())[:4]))
            imp_s = "; ".join(r.get("implicits") or []) or "-"
            sub_part = f" ({r['sub_type']})" if r.get("sub_type") else ""
            rows.append(
                [
                    name_disp,
                    f"{r.get('type') or '-'}{sub_part}",
                    req_s,
                    "; ".join(prop_bits)[:60] or "-",
                    imp_s[:80],
                ]
            )
        lines.append(render_table(["Base", "Class", "Req (Lv/S/D/I)", "Properties", "Implicits"], rows))

    return "\n".join(lines)
