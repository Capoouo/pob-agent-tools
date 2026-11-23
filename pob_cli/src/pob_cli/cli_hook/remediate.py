"""Bounded (<=2.5s) auto-remediation runner and `<pob-hook-correction>` formatter for Gate 3."""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..cli_common import _prepare_load_source
from ..daemon import call_daemon, read_daemon_state
from ..formatters import fmt_num, format_diagnose_markdown
from ..game_data import ground_text_entities
from ..item_inspect import format_inspection, inspect_item
from ..ticks import build_server_tick_table

MAX_CORRECTION_CHARS = 2500
DEFAULT_BUDGET_SEC = 2.5


@contextlib.contextmanager
def _disable_receipts() -> Iterator[None]:
    prev = os.environ.get("POB_HOOK_DISABLE_RECEIPTS")
    os.environ["POB_HOOK_DISABLE_RECEIPTS"] = "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("POB_HOOK_DISABLE_RECEIPTS", None)
        else:
            os.environ["POB_HOOK_DISABLE_RECEIPTS"] = prev


def _detect_trigger_base_cd(claims: list[str], fallback_text: str) -> tuple[float, str]:
    blob = (" ".join(claims) + " " + fallback_text).lower()
    if any(k in blob for k in ("sword of ages", "万古之剑", "soa")):
        return 0.50, "Sword of Ages (0.50s)"
    if any(k in blob for k in ("cospri", "冰剑", "mjolner", "mjölner", "雷锤", "poet", "诗人", "cwdt", "受伤时施放")):
        return 0.25, "0.25s Trigger (Cospri/Mjolner/Poet/CWDT)"
    return 0.15, "Cast On Critical Strike (0.15s)"


def _remediate_v1(v1_issues: list[Any], *, runtime_dir: Path | None) -> str:
    lines: list[str] = []
    terms: list[str] = []
    seen_terms: set[str] = set()
    for iss in v1_issues[:5]:
        lines.append(f"- [{iss.rule_id}] `{iss.trigger}`: {iss.reason} -> {iss.remediation}")
        for term in (iss.ground_terms or (iss.trigger,)):
            t = str(term or "").strip()
            if t and t.lower() not in seen_terms:
                seen_terms.add(t.lower())
                terms.append(t)
    if terms:
        try:
            grounded = ground_text_entities(" ".join(terms[:8]), limit=4, runtime_dir=runtime_dir)
            block = str(grounded.get("contextBlock") or "").strip()
            if block:
                lines.append(block)
        except Exception:
            pass
    return "\n".join(lines).strip()


def _format_compact_summary(res: dict[str, Any], slot: str) -> str:
    info = res.get("buildInfo") or {}
    off = res.get("offence") or {}
    dfn = res.get("defence") or {}
    rev = res.get("rev")
    dps = off.get("FullDPS") or off.get("CombinedDPS") or off.get("TotalDPS")
    asc = info.get("ascendClassNameZh") or info.get("ascendClassName") or info.get("className") or "-"
    rev_str = f" | rev={rev}" if rev is not None else ""
    return (
        f"[Summary `{slot}`{rev_str}] Lv.{info.get('level', '-')} {asc}\n"
        f"- Offence: Full/Combined DPS={fmt_num(dps)} | Hit={fmt_num(off.get('TotalDPS'))} | "
        f"DoT={fmt_num(off.get('TotalDotDPS') or off.get('TotalDot'))} | Speed={fmt_num(off.get('Speed'), 2)}/s\n"
        f"- Defence: TotalEHP={fmt_num(dfn.get('TotalEHP'))} | Life={fmt_num(dfn.get('Life'))} | "
        f"ES={fmt_num(dfn.get('EnergyShield'))} | PhysMaxHit={fmt_num(dfn.get('PhysicalMaximumHitTaken'))} | "
        f"Res F/C/L/Ch={fmt_num(dfn.get('FireResist'), 0)}/{fmt_num(dfn.get('ColdResist'), 0)}/"
        f"{fmt_num(dfn.get('LightningResist'), 0)}/{fmt_num(dfn.get('ChaosResist'), 0)}%"
    )


def _is_local_build_hint(hint: str | None) -> bool:
    if not hint:
        return False
    s = hint.strip()
    if s.startswith("@"):
        s = s[1:].strip()
    if not s or s.startswith(("http://", "https://")) or len(s) > 260:
        return False
    try:
        return Path(s).is_file()
    except OSError:
        return False


def _remediate_v2(
    v2_info: dict[str, Any],
    *,
    runtime_dir: Path | None,
    deadline: float,
) -> str:
    target_slot = str(v2_info.get("targetSlot") or "main")
    source_hint = v2_info.get("buildSourceHint")
    reason_kind = str(v2_info.get("reasonKind") or "missing_compute")
    remaining = deadline - time.monotonic()

    if read_daemon_state(runtime_dir) is not None and remaining > 0.25:
        try:
            if reason_kind == "missing_compute" and _is_local_build_hint(source_hint):
                src, xml_code = _prepare_load_source(str(source_hint))
                params: dict[str, Any] = {"target": target_slot, "no_rank": True}
                if xml_code is not None:
                    params["xml_code"] = xml_code
                else:
                    params["source"] = src
                res = call_daemon(
                    "diagnose",
                    params,
                    auto_start=False,
                    timeout=min(2.2, remaining),
                    runtime_dir=runtime_dir,
                    emit_cold_start_alert=False,
                )
                return format_diagnose_markdown(res)[:1200]
            res = call_daemon(
                "summary",
                {"target": target_slot},
                auto_start=False,
                timeout=min(1.5, remaining),
                runtime_dir=runtime_dir,
                emit_cold_start_alert=False,
            )
            return _format_compact_summary(res, target_slot)
        except Exception:
            pass

    if reason_kind == "stale_rev":
        return f"当前 daemon 未响应或超时，请先执行 `pob summary --target {target_slot}` 获取 `--apply` 后的最新面板真值再重写结论。"
    src_cmd = f"pob diagnose {source_hint}" if source_hint else "pob diagnose <build_source>"
    return f"当前 daemon 未运行或超时，请先执行 `{src_cmd}` 获取 PoB 引擎实算面板后再给出数字结论，严禁凭空估算 DPS/EHP。"


def _remediate_v3(
    missing_slots: list[str],
    *,
    target_slot: str,
    runtime_dir: Path | None,
    deadline: float,
) -> str:
    blocks: list[str] = []
    daemon_up = read_daemon_state(runtime_dir) is not None
    for eq_slot in missing_slots[:3]:
        remaining = deadline - time.monotonic()
        inspected_md = ""
        if daemon_up and remaining > 0.2:
            try:
                loaded = call_daemon(
                    "items",
                    {"target": target_slot, "slot": eq_slot, "raw": True},
                    auto_start=False,
                    timeout=min(1.2, remaining),
                    runtime_dir=runtime_dir,
                    emit_cold_start_alert=False,
                )
                raw_text = loaded.get("raw") if isinstance(loaded, dict) else None
                if isinstance(raw_text, str) and raw_text.strip():
                    slot_name = (
                        (loaded.get("slot") or {}).get("name")
                        if isinstance(loaded.get("slot"), dict)
                        else None
                    ) or eq_slot
                    report = inspect_item(raw_text, slot=slot_name, limit=4, runtime_dir=runtime_dir)
                    inspected_md = format_inspection(report)
            except Exception:
                inspected_md = ""
        if inspected_md:
            blocks.append(inspected_md[:700])
        else:
            blocks.append(
                f"未验视 `{eq_slot}`（daemon 未运行或该槽位为空/超时）：请先运行 `pob item inspect --slot \"{eq_slot}\"` 核验真实前后缀空位，切勿盲猜空词缀。"
            )
    return "\n\n".join(blocks).strip()


def _remediate_v4(trigger_claims: list[str], assistant_text: str) -> str:
    base_cd, label = _detect_trigger_base_cd(trigger_claims, assistant_text)
    tbl = build_server_tick_table(base_cd)
    rows = tbl.get("breakpoints") or []
    lines = [
        f"[PoE1 0.033s (33ms) 服务端帧率断点表 — {label}]",
        "| Ticks | Min CDR | Eff CD | Max APS/Triggers |",
        "|---:|---:|---:|---:|",
    ]
    for r in rows[:5]:
        lines.append(
            f"| {r['ticks']}t | {r['minCdrPct']}% | {r['effectiveCdSec']:.3f}s ({r['effectiveCdMs']}ms) | {r['maxApsCap']:.2f}/s |"
        )
    lines.append(f"- {tbl['cwcNote']}")
    return "\n".join(lines)


def build_correction_prompt(
    reasons: list[str],
    evidence_blocks: list[str],
    *,
    max_chars: int = MAX_CORRECTION_CHARS,
) -> str:
    """Format `<pob-hook-correction retry="1/1">` strictly capped at `<= max_chars`."""
    header = (
        '<pob-hook-correction retry="1/1">\n'
        "[硬护栏拦截] 你的上一版回复未通过确定性核验：\n"
        "- 原因：" + "；".join(reasons) + "\n"
        "- 实测真值（已自动为你补跑）：\n"
    )
    footer = (
        "\n\n请严格依据上述实测真值直接重写给用户的最终回复"
        "（修正违规内容与数值，不要向用户暴露内部拦截日志，无需重复调用已代跑的命令）。\n"
        "</pob-hook-correction>"
    )
    body = "\n\n".join(b.strip() for b in evidence_blocks if b and b.strip())
    avail = max(120, max_chars - len(header) - len(footer))
    if len(body) > avail:
        suffix = "\n...(truncated)"
        body = body[: max(0, avail - len(suffix))].rstrip() + suffix
    out = f"{header}{body}{footer}"
    if len(out) > max_chars:
        # Extreme fallback if reasons themselves were unusually long
        keep = max_chars - len("\n</pob-hook-correction>") - 16
        out = out[: max(0, keep)].rstrip() + "\n...(truncated)\n</pob-hook-correction>"
    return out


def run_remediation(
    *,
    v1_issues: list[Any],
    v2_info: dict[str, Any] | None,
    v3_slots: list[str],
    v4_claims: list[str],
    assistant_text: str,
    target_slot: str = "main",
    runtime_dir: Path | None = None,
    budget_sec: float = DEFAULT_BUDGET_SEC,
) -> str:
    """Execute bounded in-process auto-remediation (never cold-starts daemon) and format correction XML."""
    deadline = time.monotonic() + max(0.2, float(budget_sec))
    reasons: list[str] = []
    evidence: list[str] = []

    with _disable_receipts():
        if v1_issues:
            triggers = ", ".join(dict.fromkeys(str(i.trigger) for i in v1_issues[:4]))
            reasons.append(f"V1 终稿命中违禁词/谬误（{triggers}）")
            evidence.append(_remediate_v1(v1_issues, runtime_dir=runtime_dir))
        if v2_info:
            if v2_info.get("reasonKind") == "stale_rev":
                reasons.append(f"V2 槽位 `{v2_info.get('targetSlot', target_slot)}` 在 `--apply` 变更后未重新读取最新 rev 面板")
            else:
                reasons.append("V2 本轮无有效 PoB 算力回执即输出构筑/面板数字结论")
            evidence.append(_remediate_v2(v2_info, runtime_dir=runtime_dir, deadline=deadline))
        if v3_slots:
            reasons.append(f"V3 未执行 item inspect 即断言装备空词缀（{', '.join(v3_slots[:3])}）")
            evidence.append(
                _remediate_v3(
                    v3_slots,
                    target_slot=target_slot,
                    runtime_dir=runtime_dir,
                    deadline=deadline,
                )
            )
        if v4_claims:
            reasons.append("V4 未执行 `pob diagnose` 或 `pob data tick` 即断言触发器 APS/CDR 断点")
            evidence.append(_remediate_v4(v4_claims, assistant_text))

    return build_correction_prompt(reasons, evidence)
