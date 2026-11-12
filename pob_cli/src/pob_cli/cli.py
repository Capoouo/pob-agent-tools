"""Click CLI entry point for `pob`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from .daemon import call_daemon
from .external_data import query_price, query_wiki
from .formatters import (
    fmt_delta,
    fmt_num,
    render_table,
    strip_pob_escapes,
    to_json_str,
)
from .importers import xml_to_share_code


def _echo_utf8(text: str) -> None:
    """Write UTF-8 safely to stdout on Windows consoles."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    click.echo(text)


def _render_mutation_report(title: str, res: dict[str, Any]) -> str:
    lines = [f"### {title} — `{res.get('mode', '')}`"]
    before = res.get("before", {})
    after = res.get("after", {})
    p_key = after.get("primaryDpsKey") or before.get("primaryDpsKey") or "CombinedDPS"
    b_pts = before.get("points", {})
    a_pts = after.get("points", {})

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

    diff = res.get("diff", {})
    deltas = diff.get("deltas", [])
    if deltas:
        lines.append("\n#### Changed Stats")
        d_rows = [[d["label"], d["beforeStr"], d["afterStr"], d["deltaStr"]] for d in deltas]
        lines.append(render_table(["Stat", "Before", "After", "Delta"], d_rows))
    else:
        lines.append("\n_(No stat changes detected)_")

    new_w = diff.get("newWarnings", [])
    res_w = diff.get("resolvedWarnings", [])
    if new_w:
        lines.append("\n#### New Warnings")
        for w in new_w:
            lines.append(f"- [NEW WARNING] {w}")
    if res_w:
        lines.append("\n#### Resolved Warnings")
        for w in res_w:
            lines.append(f"- [RESOLVED] {w}")

    return "\n".join(lines)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main() -> None:
    """Headless Path of Building (PoB) CLI for build inspection, what-if testing, and optimization."""
    pass


# --- 4.1 Lifecycle & Build I/O ---


@main.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_status(as_json: bool) -> None:
    """Show daemon status and currently loaded builds in main/ref slots."""
    res = call_daemon("status")
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    m = res.get("main", {})
    m_info = m.get("buildInfo") or {}
    r = res.get("ref", {})
    r_info = r.get("buildInfo") or {}
    rows = [
        [
            "main",
            "Running" if m.get("alive") else "Stopped",
            m_info.get("classNameZh") or m_info.get("className") or "-",
            m_info.get("ascendClassNameZh") or m_info.get("ascendClassName") or "-",
            m_info.get("level") or "-",
            f"{m.get('undoDepth', 0)} / {m.get('redoDepth', 0)}",
        ],
        [
            "ref",
            "Running" if r.get("alive") else "Idle (Stopped)",
            r_info.get("classNameZh") or r_info.get("className") or "-",
            r_info.get("ascendClassNameZh") or r_info.get("ascendClassName") or "-",
            r_info.get("level") or "-",
            "-",
        ],
    ]
    _echo_utf8(f"Daemon PID: `{res.get('pid')}` | Uptime: `{res.get('uptimeSec')}s`")
    _echo_utf8(render_table(["Slot", "State", "Class", "Ascendancy", "Level", "Undo/Redo"], rows))


@main.command("stop")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_stop(as_json: bool) -> None:
    """Stop the background PoB daemon and release headless processes."""
    try:
        res = call_daemon("stop", auto_start=False)
    except RuntimeError:
        res = {"stopped": True, "note": "Daemon was not running"}
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8("PoB daemon stopped.")


def _prepare_load_source(source: str) -> tuple[str | None, str | None]:
    """Resolve a user build source string (`@path`, `.xml` file, share-code `.txt` file, URL, or Base64 code).

    Returns `(source_str, xml_code_str)`.
    """
    raw = source.strip()
    candidate_path = raw[1:].strip() if raw.startswith("@") else raw
    is_local_file = False
    if len(candidate_path) <= 260 and not candidate_path.startswith(("http://", "https://")):
        try:
            p = Path(candidate_path)
            is_local_file = p.exists() and p.is_file()
        except OSError:
            is_local_file = False

    if is_local_file:
        text = Path(candidate_path).read_text(encoding="utf-8", errors="replace").strip()
        if text.lstrip().startswith("<"):
            return None, xml_to_share_code(text)
        return text, None

    return raw, None


@main.command("load")
@click.argument("source")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main", help="Target slot (main or ref).")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_load(source: str, target: str, as_json: bool) -> None:
    """Load a build from local .xml/.txt file (or @file), share URL (pobb.in/poedb/poe.ninja/...), or Base64 code."""
    params: dict[str, Any] = {"target": target}
    resolved_src, resolved_xml_code = _prepare_load_source(source)
    if resolved_xml_code is not None:
        params["xml_code"] = resolved_xml_code
    else:
        params["source"] = resolved_src

    res = call_daemon("load", params)
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    info = res.get("buildInfo", {})
    pts = info.get("points", {})
    header = res.get("buildHeader", {})
    main_sg = header.get("mainSocketGroup", {})
    main_sk = header.get("mainSkill", {})

    def _dd_label(dd: dict[str, Any]) -> str:
        lst = dd.get("list") or []
        idx = max(0, int(dd.get("index") or 1) - 1)
        if not lst or idx >= len(lst):
            return "-"
        entry = lst[idx]
        if isinstance(entry, dict):
            return strip_pob_escapes(entry.get("labelZh") or entry.get("label") or "-")
        return strip_pob_escapes(str(entry))

    rows = [
        ["Target Slot", target],
        ["Import Method", res.get("importMethod")],
        ["Class / Ascendancy", f"{info.get('classNameZh') or info.get('className')} / {info.get('ascendClassNameZh') or info.get('ascendClassName')}"],
        ["Level", info.get("level")],
        ["Passive Points", f"{pts.get('used', 0)}/{pts.get('usedMax', 0)} (Asc: {pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)})"],
        ["Tree Version", info.get("treeVersion")],
        ["Main Skill Group", f"#{main_sg.get('index')} ({_dd_label(main_sg)})"],
        ["Active Skill", _dd_label(main_sk)],
    ]
    _echo_utf8(render_table(["Field", "Value"], rows))


@main.command("save")
@click.argument("xml_path", type=click.Path(dir_okay=False, writable=True))
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_save(xml_path: str, target: str, as_json: bool) -> None:
    """Save the current build to a local .xml file (written by CLI, R11)."""
    res = call_daemon("export", {"target": target, "include_xml": True})
    out_file = Path(xml_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    xml_content = res.get("xml") or ""
    out_file.write_text(xml_content, encoding="utf-8")
    payload = {"savedPath": str(out_file), "bytes": len(xml_content.encode("utf-8")), "target": target}
    if as_json:
        _echo_utf8(to_json_str(payload))
    else:
        _echo_utf8(f"Saved `{target}` build XML to `{out_file}` ({payload['bytes']:,} bytes).")


@main.command("export-code")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_export_code(target: str, as_json: bool) -> None:
    """Export current build as a URL-safe Base64 PoB share code."""
    res = call_daemon("export", {"target": target, "include_xml": False})
    if as_json:
        _echo_utf8(to_json_str({"target": target, "code": res.get("code")}))
    else:
        _echo_utf8(res.get("code") or "")


@main.command("switch-main")
@click.option("--spec", "spec_index", help="1-based passive tree spec index or title.")
@click.option("--skill-group", "skill_group", help="1-based skill group index or label.")
@click.option("--skill", "main_skill_index", type=int, help="1-based active skill index in group.")
@click.option("--part", "skill_part_index", type=int, help="1-based skill part index.")
@click.option("--item-set", "item_set_id", help="Item set ID or title.")
@click.option("--skill-set", "skill_set_id", help="Skill set ID or title.")
@click.option("--config-set", "config_set_id", help="Config set ID or title.")
@click.option("--loadout", "loadout_index", help="1-based synced loadout index or title.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_switch_main(
    spec_index: str | None,
    skill_group: str | None,
    main_skill_index: int | None,
    skill_part_index: int | None,
    item_set_id: str | None,
    skill_set_id: str | None,
    config_set_id: str | None,
    loadout_index: str | None,
    target: str,
    as_json: bool,
) -> None:
    """Switch active passive tree spec, main skill group/part, item/skill/config set, or loadout."""
    res = call_daemon(
        "switch_main",
        {
            "target": target,
            "spec_index": spec_index,
            "skill_group": skill_group,
            "main_skill_index": main_skill_index,
            "skill_part_index": skill_part_index,
            "item_set_id": item_set_id,
            "skill_set_id": skill_set_id,
            "config_set_id": config_set_id,
            "loadout_index": loadout_index,
            "apply": True,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8(_render_mutation_report("Switch Active Selection", res))


@main.command("undo")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_undo(target: str, as_json: bool) -> None:
    """Undo the last `--apply` mutation."""
    res = call_daemon("undo", {"target": target})
    if as_json:
        _echo_utf8(to_json_str(res))
        return
    deltas = res.get("diff", {}).get("deltas", [])
    rows = [[d["label"], d["beforeStr"], d["afterStr"], d["deltaStr"]] for d in deltas]
    _echo_utf8(f"### Undo Applied (Undo depth: `{res.get('undoDepth')}`, Redo depth: `{res.get('redoDepth')}`)")
    _echo_utf8(render_table(["Stat", "Before Undo", "After Undo", "Delta"], rows))


@main.command("redo")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_redo(target: str, as_json: bool) -> None:
    """Redo the last undone mutation."""
    res = call_daemon("redo", {"target": target})
    if as_json:
        _echo_utf8(to_json_str(res))
        return
    deltas = res.get("diff", {}).get("deltas", [])
    rows = [[d["label"], d["beforeStr"], d["afterStr"], d["deltaStr"]] for d in deltas]
    _echo_utf8(f"### Redo Applied (Undo depth: `{res.get('undoDepth')}`, Redo depth: `{res.get('redoDepth')}`)")
    _echo_utf8(render_table(["Stat", "Before Redo", "After Redo", "Delta"], rows))


# --- 4.2 Build Inspection & Audit ---


@main.command("summary")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_summary(target: str, as_json: bool) -> None:
    """Display comprehensive build identity, offence, minion offence, defence, and warnings."""
    res = call_daemon("summary", {"target": target})
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    info = res.get("buildInfo", {})
    pts = info.get("points", {})
    mg = res.get("mainGroup") or {}
    gems_str = ", ".join(
        f"{strip_pob_escapes(g.get('nameZh') or g.get('name'))} ({g.get('level')}/{g.get('quality')}%)"
        for g in mg.get("gems", [])
        if g.get("enabled", True)
    )

    lines = [
        f"### Build Summary (`{target}`): Lv.{info.get('level')} {info.get('classNameZh') or info.get('className')} / {info.get('ascendClassNameZh') or info.get('ascendClassName')}",
        f"- **Points**: `{pts.get('used', 0)}/{pts.get('usedMax', 0)}` | **Ascendancy**: `{pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)}` | **Tree**: `{info.get('treeVersion')}`",
        f"- **Main Skill Group**: `#{mg.get('index', '-')} {strip_pob_escapes(mg.get('displayLabelZh') or mg.get('displayLabel') or '-')}` (`{mg.get('slot') or 'Unassigned'}`)",
        f"- **Linked Gems**: {gems_str or '-'}",
    ]

    off = res.get("offence", {})
    crit_mult = off.get("CritMultiplier")
    crit_mult_str = f"{float(crit_mult) * 100:.0f}% (x{float(crit_mult):.2f})" if crit_mult is not None else "-"
    dot_dps = off.get("TotalDotDPS") or off.get("TotalDot")
    off_rows = [
        ["Full DPS", fmt_num(off.get("FullDPS")), "Combined DPS", fmt_num(off.get("CombinedDPS"))],
        ["Hit DPS (TotalDPS)", fmt_num(off.get("TotalDPS")), "DoT DPS (TotalDotDPS)", fmt_num(dot_dps)],
        ["Average Damage", fmt_num(off.get("AverageDamage")), "Attack/Cast Rate", f"{fmt_num(off.get('Speed'), 2)}/s"],
        ["Crit Chance", f"{fmt_num(off.get('CritChance'), 1)}%", "Crit Multiplier", crit_mult_str],
        ["Hit Chance", f"{fmt_num(off.get('HitChance'), 1)}%", "Mana Cost", fmt_num(off.get("ManaCost"))],
    ]
    lines.append("\n#### Offence")
    lines.append(render_table(["Metric", "Value", "Metric ", "Value "], off_rows))

    m_off = res.get("minionOffence")
    if m_off:
        lines.append("\n#### Minion Offence")
        m_rows = [
            ["Minion Combined DPS", fmt_num(m_off.get("MinionCombinedDPS")), "Minion Hit DPS", fmt_num(m_off.get("MinionTotalDPS"))],
            ["Minion Avg Damage", fmt_num(m_off.get("MinionAverageDamage")), "Minion Speed", f"{fmt_num(m_off.get('MinionSpeed'), 2)}/s"],
            ["Minion Crit Chance", f"{fmt_num(m_off.get('MinionCritChance'), 1)}%", "Minion Life", fmt_num(m_off.get("MinionLife"))],
        ]
        for s_row in m_off.get("sidebarRows", []):
            m_rows.append([s_row["label"], s_row["value"], s_row["stat"], "-"])
        lines.append(render_table(["Minion Metric", "Value", "Detail", "Extra"], m_rows))

    dfn = res.get("defence", {})
    def_rows = [
        ["Effective HP (TotalEHP)", fmt_num(dfn.get("TotalEHP")), "Maximum Life / Unres", f"{fmt_num(dfn.get('Life'))} / {fmt_num(dfn.get('LifeUnreserved'))} ({fmt_num(dfn.get('LifeUnreservedPercent'), 0)}%)"],
        ["Energy Shield / Ward", f"{fmt_num(dfn.get('EnergyShield'))} / {fmt_num(dfn.get('Ward'))}", "Maximum Mana / Unres", f"{fmt_num(dfn.get('Mana'))} / {fmt_num(dfn.get('ManaUnreserved'))} ({fmt_num(dfn.get('ManaUnreservedPercent'), 0)}%)"],
        ["Phys / Chaos Max Hit", f"{fmt_num(dfn.get('PhysicalMaximumHitTaken'))} / {fmt_num(dfn.get('ChaosMaximumHitTaken'))}", "Fire/Cold/Light Max Hit", f"{fmt_num(dfn.get('FireMaximumHitTaken'))} / {fmt_num(dfn.get('ColdMaximumHitTaken'))} / {fmt_num(dfn.get('LightningMaximumHitTaken'))}"],
        ["Fire / Cold Resist", f"{fmt_num(dfn.get('FireResist'), 0)}% (+{fmt_num(dfn.get('FireResistOverCap'), 0)}%) / {fmt_num(dfn.get('ColdResist'), 0)}% (+{fmt_num(dfn.get('ColdResistOverCap'), 0)}%)", "Light / Chaos Resist", f"{fmt_num(dfn.get('LightningResist'), 0)}% (+{fmt_num(dfn.get('LightningResistOverCap'), 0)}%) / {fmt_num(dfn.get('ChaosResist'), 0)}% (+{fmt_num(dfn.get('ChaosResistOverCap'), 0)}%)"],
        ["Armour / Evade", f"{fmt_num(dfn.get('Armour'))} / {fmt_num(dfn.get('Evasion'))}", "Block / Spell Block / Supp", f"{fmt_num(dfn.get('EffectiveBlockChance'), 0)}% / {fmt_num(dfn.get('EffectiveSpellBlockChance'), 0)}% / {fmt_num(dfn.get('EffectiveSpellSuppressionChance'), 0)}%"],
        ["Net Life / Mana Regen", f"{fmt_num(dfn.get('NetLifeRegen'), 1)}/s / {fmt_num(dfn.get('NetManaRegen'), 1)}/s", "Str / Dex / Int", f"{fmt_num(dfn.get('Str'))} / {fmt_num(dfn.get('Dex'))} / {fmt_num(dfn.get('Int'))}"],
    ]
    lines.append("\n#### Defence & Sustain")
    lines.append(render_table(["Metric", "Value", "Metric ", "Value "], def_rows))

    warnings = res.get("warnings", [])
    if warnings:
        lines.append("\n#### Warnings")
        for w in warnings:
            lines.append(f"- {w}")

    _echo_utf8("\n".join(lines))


@main.command("audit")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_audit(target: str, as_json: bool) -> None:
    """Run build health check and non-destructive 4-part PoB inflation ('water') audit."""
    res = call_daemon("audit", {"target": target})
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    lines = ["### Build Health & Inflation ('Water') Audit"]
    issues = res.get("healthIssues", [])
    if issues:
        lines.append("\n#### 1. Health & Hard Gaps")
        i_rows = [[x["severity"], x["category"], x["message"]] for x in issues]
        lines.append(render_table(["Severity", "Category", "Finding"], i_rows))
    else:
        lines.append("\n#### 1. Health & Hard Gaps\n- No critical resistance, attribute, or sustain gaps detected.")

    stripped = res.get("inflationStripped", {})
    lines.append("\n#### 2. Inflation ('Water') Stripped for Pinnacle Boss Baseline")
    s_rows = []
    for dim in ("config", "flasks", "skills", "party"):
        items = stripped.get(dim) or []
        s_rows.append([dim.capitalize(), len(items), "; ".join(items) if items else "_(Clean)_"])
    lines.append(render_table(["Dimension", "Count", "Stripped Items"], s_rows))

    orig = res.get("original", {})
    dew = res.get("dewateredPinnacle", {})
    lines.append("\n#### 3. Original Panel vs True Pinnacle Boss Baseline (De-watered)")
    comp_rows = [
        [
            f"Primary DPS ({orig.get('primaryDpsKey')})",
            fmt_num(orig.get("primaryDps")),
            fmt_num(dew.get("primaryDps")),
            fmt_delta(orig.get("primaryDps"), dew.get("primaryDps")),
        ],
        [
            "Effective HP (TotalEHP)",
            fmt_num(orig.get("totalEhp")),
            fmt_num(dew.get("totalEhp")),
            fmt_delta(orig.get("totalEhp"), dew.get("totalEhp")),
        ],
        [
            "Phys Max Hit",
            fmt_num(orig.get("physMaxHit")),
            fmt_num(dew.get("physMaxHit")),
            fmt_delta(orig.get("physMaxHit"), dew.get("physMaxHit")),
        ],
        [
            "Elemental Max Hit (Min)",
            fmt_num(orig.get("eleMaxHit")),
            fmt_num(dew.get("eleMaxHit")),
            fmt_delta(orig.get("eleMaxHit"), dew.get("eleMaxHit")),
        ],
        [
            "Chaos Max Hit",
            fmt_num(orig.get("chaosMaxHit")),
            fmt_num(dew.get("chaosMaxHit")),
            fmt_delta(orig.get("chaosMaxHit"), dew.get("chaosMaxHit")),
        ],
    ]
    lines.append(render_table(["Metric", "Original Panel", "Dewatered Pinnacle", "Water Delta"], comp_rows))
    _echo_utf8("\n".join(lines))


@main.command("items")
@click.option("--slot", help="Inspect a specific slot (e.g. 'Weapon 1', 'Amulet', 'Flask 1', or '#1' jewel socket).")
@click.option("--raw", is_flag=True, help="Include raw PoE item text.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_items(slot: str | None, raw: bool, target: str, as_json: bool) -> None:
    """List equipped items or inspect a single slot's mods and raw text."""
    res = call_daemon("items", {"target": target, "slot": slot, "raw": raw})
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    if slot:
        item = res.get("item")
        if not item:
            _echo_utf8(f"Slot `{res.get('slot', {}).get('name', slot)}` is empty.")
            return
        lines = [
            f"### Slot `{res['slot']['name']}`: {strip_pob_escapes(item.get('titleZh') or item.get('title') or item.get('name'))} (`{item.get('rarity')}`)",
        ]
        tip = res.get("tooltip") or {}
        for ln in tip.get("lines", []):
            txt = strip_pob_escapes(ln.get("text") or ln.get("raw") or "")
            if txt:
                lines.append(f"  {txt}")
        if raw and res.get("raw"):
            lines.append("\n```text\n" + res["raw"].strip() + "\n```")
        _echo_utf8("\n".join(lines))
        return

    rows = []
    for s in res.get("equippedSlots", []):
        it = s.get("item") or {}
        slot_disp = s["slotName"]
        if s.get("socketIndex"):
            slot_disp = f"{slot_disp} (#{s['socketIndex']})"
        state = "Active" if s.get("active") else ("Flask Off" if s.get("isFlask") else ("Inactive" if s.get("inactive") else "Equipped"))
        rows.append(
            [
                slot_disp,
                strip_pob_escapes(it.get("titleZh") or it.get("title") or it.get("nameZh") or it.get("name") or "-"),
                it.get("baseNameZh") or it.get("baseName") or "-",
                it.get("rarity") or "-",
                state if it else "Empty",
            ]
        )
    _echo_utf8(render_table(["Slot", "Item Name", "Base", "Rarity", "Status"], rows))


@main.command("skills")
@click.option("--group", help="Inspect a specific skill group ('main', index, or label).")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_skills(group: str | None, target: str, as_json: bool) -> None:
    """List all skill groups or inspect gems and per-gem DPS tooltips in a group."""
    res = call_daemon("skills", {"target": target, "group": group})
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    if group is not None:
        grp = res.get("group", {})
        rows = []
        for gm in grp.get("gems", []):
            dps_info = gm.get("tooltipDps") or {}
            rows.append(
                [
                    gm.get("index"),
                    strip_pob_escapes(gm.get("nameZh") or gm.get("name") or gm.get("nameSpec")),
                    "Support" if gm.get("support") else "Active",
                    f"{gm.get('level')}/{gm.get('quality')}%",
                    "Yes" if gm.get("enabled", True) else "No",
                    f"{dps_info.get('pct', 0):+.1f}% ({fmt_num(dps_info.get('delta'))})" if dps_info.get("pct") is not None else "-",
                ]
            )
        _echo_utf8(f"### Skill Group #{grp.get('index')}: {strip_pob_escapes(grp.get('displayLabelZh') or grp.get('displayLabel'))} (Slot: `{grp.get('slot') or '-'}`)")
        _echo_utf8(render_table(["#", "Gem", "Type", "Lv/Qual", "Enabled", "Tooltip DPS Impact"], rows))
        return

    rows = []
    for g in res.get("groups", []):
        gems_summary = ", ".join(
            f"{strip_pob_escapes(gm.get('nameZh') or gm.get('name'))}{'' if gm.get('enabled', True) else '[OFF]'}"
            for gm in g.get("gems", [])
        )
        rows.append(
            [
                f"#{g['index']}{' *MAIN*' if g.get('isMain') else ''}",
                strip_pob_escapes(g.get("displayLabelZh") or g.get("displayLabel") or g.get("label")),
                g.get("slot") or "-",
                "Yes" if g.get("enabled", True) else "No",
                "Yes" if g.get("includeInFullDPS") else "No",
                gems_summary,
            ]
        )
    _echo_utf8(render_table(["Group", "Label", "Slot", "Enabled", "FullDPS", "Gems"], rows))


def _run_tree_inspect(search: str | None, target: str, as_json: bool) -> None:
    """Summarize allocated Keystones, Ascendancy, Masteries, Notables, Jewels, or search tree nodes."""
    res = call_daemon("tree", {"target": target, "search": search})
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    if search:
        rows = []
        for m in res.get("matches", []):
            stats_s = "; ".join(str(s) for s in (m.get("statsZh") or m.get("stats") or [])[:2])
            rows.append(
                [
                    m.get("id"),
                    strip_pob_escapes(m.get("nameZh") or m.get("name")),
                    m.get("type") or "Normal",
                    "Yes" if m.get("allocated") else "No",
                    m.get("cost") if m.get("cost") is not None else "-",
                    stats_s[:80],
                ]
            )
        _echo_utf8(f"### Passive Tree Search: `{search}` ({res.get('totalMatches', 0)} matches)")
        _echo_utf8(render_table(["Node ID", "Name", "Type", "Allocated", "Path Cost", "Stats"], rows))
        return

    pts = res.get("points", {})
    lines = [
        f"### Passive Tree Summary: `{pts.get('used', 0)}/{pts.get('usedMax', 0)}` points | Ascendancy `{pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)}`",
    ]
    for title, key in (
        ("Keystones", "keystones"),
        ("Ascendancy Nodes", "ascendancyNodes"),
        ("Masteries", "masteries"),
        ("Notables", "notables"),
    ):
        items = res.get(key, [])
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

    _echo_utf8("\n".join(lines))


@main.command("calc")
@click.argument("keyword")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_calc(keyword: str, target: str, as_json: bool) -> None:
    """Search PoB Calcs breakdown tables and formulas by English/Chinese keyword."""
    res = call_daemon("calc", {"target": target, "keyword": keyword})
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    matches = res.get("matches", [])
    if not matches:
        _echo_utf8(f"No calculation rows matched `{keyword}`.")
        return

    lines = [f"### Calcs Breakdown for `{keyword}` ({res.get('totalMatches')} matches)"]
    for m in matches[:10]:
        cell_texts = " | ".join(c["text"] for c in m.get("cells", []) if c.get("text"))
        lines.append(
            f"\n#### [{m.get('sectionGroup')} / {m.get('subsection')}] {m.get('rowLabelZh') or m.get('rowLabel')}: `{cell_texts}`"
        )
        for c in m.get("cells", []):
            for sec in c.get("breakdown") or []:
                if sec.get("type") == "text":
                    for ln in sec.get("lines", []):
                        lines.append(f"  - {strip_pob_escapes(ln)}")
                elif sec.get("type") == "table":
                    cols = sec.get("cols", [])
                    headers = [col.get("label") or col.get("key") or "" for col in cols]
                    keys = [col.get("key") for col in cols]
                    t_rows = [[r.get(k, "") for k in keys] for r in sec.get("rows", [])]
                    if headers and t_rows:
                        lines.append(render_table(headers, t_rows))

    _echo_utf8("\n".join(lines))


# --- 4.3 What-If & Apply Mutations ---


@main.group("item")
def grp_item() -> None:
    """Evaluate or mutate equipped items, flasks, and anoints."""
    pass


@grp_item.command("try")
@click.option("--slot", help="Target equipment slot (e.g. 'Weapon 1', 'Ring 1', '#1').")
@click.option("--raw", "raw_text", help="Raw PoE item text string.")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), help="File containing raw PoE item text.")
@click.option("--tooltip-only", is_flag=True, help="Fast native tooltip preview only (skip snapshot secondary checks).")
@click.option("--apply", is_flag=True, help="Persist equipped item into build and push undo snapshot.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_item_try(
    slot: str | None,
    raw_text: str | None,
    file_path: str | None,
    tooltip_only: bool,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Evaluate or equip a raw PoE item with native tooltip comparison + full snapshot diff."""
    if file_path:
        raw_text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    if not raw_text:
        raise click.UsageError("Provide either --raw \"<item_text>\" or --file <item.txt>.")

    res = call_daemon(
        "item_try",
        {
            "target": target,
            "slot": slot,
            "raw": raw_text,
            "tooltip_only": tooltip_only,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    lines = [f"### Item Evaluation (Slot: `{res.get('slotName') or 'Auto'}`)"]
    slot_tips = res.get("slotTooltips") or []
    if not slot_tips and res.get("tooltip"):
        slot_tips = [res["tooltip"]]
    for st in slot_tips:
        st_slot = st.get("slotName") or res.get("slotName") or "Default"
        t_lines = st.get("lines") or []
        if t_lines:
            lines.append(f"\n#### Native PoB Tooltip (`{st_slot}`)")
            for ln in t_lines:
                txt = (
                    strip_pob_escapes(ln.get("text") or ln.get("raw") or "")
                    if isinstance(ln, dict)
                    else strip_pob_escapes(str(ln))
                )
                if txt:
                    lines.append(f"  {txt}")

    if res.get("mutation"):
        lines.append("\n" + _render_mutation_report("Snapshot Full Simulation", res["mutation"]))
    _echo_utf8("\n".join(lines))


@grp_item.command("flask")
@click.argument("index", type=str)
@click.argument("state", type=click.Choice(["on", "off"], case_sensitive=False))
@click.option("--apply", is_flag=True, help="Persist change into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_item_flask(index: str, state: str, apply: bool, target: str, as_json: bool) -> None:
    """Toggle Flask 1..5 or `all` on or off."""
    res = call_daemon(
        "item_flask",
        {
            "target": target,
            "flask_index": index,
            "active": state.lower() == "on",
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8(_render_mutation_report(f"Flask {index} -> {state.upper()}", res))


@grp_item.command("anoint")
@click.option("--slot", default="Amulet", show_default=True, help="Equipment slot to anoint.")
@click.option("--notable", required=True, help="Notable passive name (Chinese or English).")
@click.option("--index", "anoint_idx", type=int, default=1, show_default=True, help="Anoint slot index (1..4).")
@click.option("--apply", is_flag=True, help="Persist anoint into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_item_anoint(
    slot: str,
    notable: str,
    anoint_idx: int,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Anoint an equipped item via PoB's native item_edit_popup workflow."""
    res = call_daemon(
        "item_anoint",
        {
            "target": target,
            "slot": slot,
            "notable": notable,
            "index": anoint_idx,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        notable_obj = res.get("detail", {}).get("notable", {})
        lbl = strip_pob_escapes(notable_obj.get("labelZh") or notable_obj.get("label") or notable)
        _echo_utf8(_render_mutation_report(f"Anoint {slot} (#{anoint_idx}) -> {lbl}", res))


@main.group("skill")
def grp_skill() -> None:
    """Modify skill groups and support/active gems."""
    pass


@grp_skill.command("set")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", help="Gem 1-based index or name within group.")
@click.option("--new-gem", "--name", "new_gem", help="Replace gem with a new gem name (English or Chinese).")
@click.option("--level", type=int, help="Gem level.")
@click.option("--quality", type=int, help="Gem quality.")
@click.option("--enabled", type=click.Choice(["true", "false"], case_sensitive=False), help="Enable/disable gem or group.")
@click.option("--count", type=int, help="Skill/minion count.")
@click.option("--full-dps", type=click.Choice(["true", "false"], case_sensitive=False), help="Include group in FullDPS.")
@click.option("--apply", is_flag=True, help="Persist change into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_skill_set(
    group: str,
    gem: str | None,
    new_gem: str | None,
    level: int | None,
    quality: int | None,
    enabled: str | None,
    count: int | None,
    full_dps: str | None,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Modify gem level/quality/enabled/count/gemId or group FullDPS state."""
    res = call_daemon(
        "skill_set",
        {
            "target": target,
            "group": group,
            "gem": gem,
            "new_gem": new_gem,
            "level": level,
            "quality": quality,
            "enabled": (enabled.lower() == "true") if enabled is not None else None,
            "count": count,
            "include_in_full_dps": (full_dps.lower() == "true") if full_dps is not None else None,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8(_render_mutation_report(f"Skill Set (Group: {group}, Gem: {gem or 'Group'})", res))


@grp_skill.command("add")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", required=True, help="Gem name (English or Chinese).")
@click.option("--level", type=int, default=20, show_default=True)
@click.option("--quality", type=int, default=20, show_default=True)
@click.option("--apply", is_flag=True, help="Persist change into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_skill_add(
    group: str,
    gem: str,
    level: int,
    quality: int,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Add a gem to a skill group."""
    res = call_daemon(
        "skill_add",
        {
            "target": target,
            "group": group,
            "gem": gem,
            "level": level,
            "quality": quality,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8(_render_mutation_report(f"Add Gem '{gem}' ({level}/{quality}%) to Group '{group}'", res))


@grp_skill.command("rm")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", required=True, help="Gem 1-based index or name to remove.")
@click.option("--apply", is_flag=True, help="Persist change into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_skill_rm(group: str, gem: str, apply: bool, target: str, as_json: bool) -> None:
    """Remove a gem from a skill group."""
    res = call_daemon(
        "skill_rm",
        {
            "target": target,
            "group": group,
            "gem": gem,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8(_render_mutation_report(f"Remove Gem '{gem}' from Group '{group}'", res))


@main.group("tree", invoke_without_command=True)
@click.option("--search", help="Search passive tree nodes by English/Chinese name, stat, ID, or '#1' jewel socket.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@click.pass_context
def grp_tree(ctx: click.Context, search: str | None, target: str, as_json: bool) -> None:
    """Inspect passive tree (`pob tree [--search ...]`) or mutate nodes (`pob tree change ...`)."""
    if ctx.invoked_subcommand is None:
        _run_tree_inspect(search=search, target=target, as_json=as_json)


@grp_tree.command("change")
@click.option("--allocate", help="Comma-separated node IDs, names, or Mastery `<nodeId>:<effectId>` to allocate.")
@click.option("--deallocate", help="Comma-separated node IDs or names to deallocate.")
@click.option("--apply", is_flag=True, help="Persist passive tree changes into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_tree_change(
    allocate: str | None,
    deallocate: str | None,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Allocate or deallocate passive tree nodes idempotently (R12)."""
    alloc_list = [x.strip() for x in allocate.split(",") if x.strip()] if allocate else []
    dealloc_list = [x.strip() for x in deallocate.split(",") if x.strip()] if deallocate else []
    res = call_daemon(
        "tree_change",
        {
            "target": target,
            "allocate": alloc_list,
            "deallocate": dealloc_list,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return
    _echo_utf8(_render_mutation_report("Passive Tree Change", res))


@main.group("config")
def grp_config() -> None:
    """Inspect or modify PoB ConfigTab parameters."""
    pass


@grp_config.command("set")
@click.argument("pairs", nargs=-1, required=False)
@click.option("--custom-mods", "custom_mods", help="Set custom modifiers text (or 'clear' to disable).")
@click.option("--apply", is_flag=True, help="Persist config changes into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_config_set(
    pairs: tuple[str, ...],
    custom_mods: str | None,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Set or reset ConfigTab variables (`key=value` or `key=reset`) and/or `--custom-mods`."""
    if not pairs and custom_mods is None:
        raise click.UsageError("Provide at least one `key=value` assignment or `--custom-mods`.")
    assignments: list[list[str]] = []
    for p in pairs:
        if "=" not in p:
            raise click.UsageError(f"Invalid config assignment '{p}'; expected key=value.")
        k, v = p.split("=", 1)
        assignments.append([k.strip(), v.strip()])

    res = call_daemon(
        "config_set",
        {
            "target": target,
            "assignments": assignments,
            "custom_mods": custom_mods,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        desc = ", ".join(pairs) + (f", customMods={custom_mods!r}" if custom_mods is not None else "")
        _echo_utf8(_render_mutation_report(f"Config Set ({desc.strip(', ')})", res))


@main.command("batch")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), help="JSON file containing batch operations list.")
@click.option("--json-ops", "json_ops", help="Inline JSON array of batch operations.")
@click.option("--apply", is_flag=True, help="Persist batch changes into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_batch(
    file_path: str | None,
    json_ops: str | None,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Execute multiple item/skill/tree/config mutations in a single snapshot transaction."""
    if file_path:
        raw = Path(file_path).read_text(encoding="utf-8", errors="replace")
    elif json_ops:
        raw = json_ops
    else:
        raise click.UsageError("Provide either --file <ops.json> or --json-ops '<json>'.")

    parsed = json.loads(raw)
    ops = parsed.get("ops") if isinstance(parsed, dict) else parsed
    if not isinstance(ops, list):
        raise click.UsageError("Batch input must be a JSON list of operation objects (or `{\"ops\": [...]}`).")

    res = call_daemon("batch", {"target": target, "ops": ops, "apply": apply})
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8(_render_mutation_report(f"Batch Execution ({len(ops)} steps)", res))


# --- 4.4 ROI Rankings & Cross-Build Comparison ---


@main.command("rank-nodes")
@click.option("--stat", default="CombinedDPS", show_default=True, help="Target stat (CombinedDPS, FullDPS, TotalEHP, Life, etc.).")
@click.option("--max-dist", type=int, default=5, show_default=True, help="Maximum path distance in points.")
@click.option("--top", type=int, default=15, show_default=True, help="Number of top candidates to show.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_rank_nodes(stat: str, max_dist: int, top: int, target: str, as_json: bool) -> None:
    """Rank unallocated passive tree nodes by ROI per point (`pathPower`) and total gain (`power`)."""
    res = call_daemon(
        "rank_nodes",
        {"target": target, "stat": stat, "max_dist": max_dist, "top": top},
        timeout=180.0,
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    rows = []
    for idx, r in enumerate(res.get("byPerPoint", []), start=1):
        rows.append(
            [
                idx,
                r.get("id"),
                strip_pob_escapes(r.get("nameZh") or r.get("name")),
                r.get("type"),
                r.get("pathDist"),
                strip_pob_escapes(r.get("pathPowerStr") or fmt_num(r.get("pathPower"), 3)),
                strip_pob_escapes(r.get("powerStr") or fmt_num(r.get("power"), 3)),
            ]
        )
    _echo_utf8(f"### Passive Node ROI Ranking (Stat: `{res.get('stat')}`, Max Dist: `{max_dist}` pts)")
    _echo_utf8(render_table(["#", "Node ID", "Name", "Type", "Cost", "Per-Point Gain", "Total Gain"], rows))


@main.command("rank-gems")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--slot", "slot_idx", type=int, help="1-based gem slot index to replace.")
@click.option("--add-link", is_flag=True, help="Rank as an additional support link instead of replacing a slot.")
@click.option("--field", default="CombinedDPS", show_default=True, help="DPS field to sort by (CombinedDPS, FullDPS, TotalDPS, etc.).")
@click.option("--top", type=int, default=15, show_default=True, help="Number of top gems to return.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_rank_gems(
    group: str,
    slot_idx: int | None,
    add_link: bool,
    field: str,
    top: int,
    target: str,
    as_json: bool,
) -> None:
    """Rank support/active candidate gems for a skill group by DPS gain."""
    res = call_daemon(
        "rank_gems",
        {
            "target": target,
            "group": group,
            "slot_idx": slot_idx,
            "add_link": add_link,
            "field": field,
            "top": top,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    rows = []
    for idx, gm in enumerate(res.get("candidates", []), start=1):
        pct = gm.get("pct")
        pct_str = f"{float(pct):+.1f}%" if pct is not None else "-"
        rows.append(
            [
                idx,
                strip_pob_escapes(gm.get("nameZh") or gm.get("name")),
                strip_pob_escapes(gm.get("name")),
                fmt_num(gm.get("dps")),
                fmt_num(gm.get("delta")),
                pct_str,
            ]
        )
    _echo_utf8(
        f"### Candidate Gem Ranking — Group #{res.get('groupIndex')} `{strip_pob_escapes(res.get('groupLabel'))}` ({res.get('mode')}, Base `{res.get('dpsField')}`: `{fmt_num(res.get('baseDps'))}`)"
    )
    _echo_utf8(render_table(["#", "Gem (ZH)", "Gem (EN)", "New DPS", "Delta", "Gain %"], rows))


@main.command("compare")
@click.argument("ref_source", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_compare(ref_source: str | None, as_json: bool) -> None:
    """Compare `main` build vs `ref` build across 3 baselines + tree/skill/equipment diffs."""
    params: dict[str, Any] = {}
    if ref_source:
        resolved_src, resolved_xml_code = _prepare_load_source(ref_source)
        if resolved_xml_code is not None:
            params["ref_xml_code"] = resolved_xml_code
        else:
            params["ref_source"] = resolved_src

    res = call_daemon("compare", params)
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    m_info = res.get("mainBuildInfo", {})
    r_info = res.get("refBuildInfo", {})
    lines = [
        f"### A/B Build Comparison: `main` (Lv.{m_info.get('level')} {m_info.get('ascendClassNameZh') or m_info.get('ascendClassName')}) vs `ref` (Lv.{r_info.get('level')} {r_info.get('ascendClassNameZh') or r_info.get('ascendClassName')})",
    ]

    baselines = res.get("baselines", {})
    for title, rows in (
        ("Baseline 1: Original Panels (As Configured)", baselines.get("original", [])),
        ("Baseline 2: External Target Environment Aligned to `main`", baselines.get("envAlignedToMain", {}).get("rows", [])),
        ("Baseline 3: Both Dewatered + Pinnacle Boss (True Baseline)", baselines.get("pinnacleDewatered", {}).get("rows", [])),
    ):
        lines.append(f"\n#### {title}")
        t_rows = [[r["label"], r["mainStr"], r["refStr"], r["diffRefVsMainStr"]] for r in rows]
        lines.append(render_table(["Metric", "Main", "Ref", "Ref vs Main"], t_rows))

    td = res.get("treeDiff", {})
    ks = td.get("keystones", {})
    lines.append("\n#### Passive Tree Diff")
    lines.append(
        f"- **Keystones Only in Main**: {', '.join(x['name'] for x in ks.get('onlyInMain', [])) or '-'}\n"
        f"- **Keystones Only in Ref**: {', '.join(x['name'] for x in ks.get('onlyInRef', [])) or '-'}\n"
        f"- **Notables Shared**: `{td.get('notables', {}).get('sharedCount', 0)}` | **Only in Main**: `{len(td.get('notables', {}).get('onlyInMain', []))}` | **Only in Ref**: `{len(td.get('notables', {}).get('onlyInRef', []))}`"
    )

    sd = res.get("skillDiff", {})
    m_mg = sd.get("mainGroupMain") or {}
    r_mg = sd.get("refGroupMain") or {}
    m_mg_lbl = strip_pob_escapes(m_mg.get("displayLabelZh") or m_mg.get("displayLabel") or "-")
    r_mg_lbl = strip_pob_escapes(r_mg.get("displayLabelZh") or r_mg.get("displayLabel") or "-")
    lines.append("\n#### Skill & Aura Diff")
    lines.append(
        f"- **Main Primary Skill Group**: `#{m_mg.get('index', '-')}` {m_mg_lbl}\n"
        f"- **Ref Primary Skill Group**: `#{r_mg.get('index', '-')}` {r_mg_lbl}\n"
        f"- **Active Skills Only in Main**: {', '.join(sd.get('activeSkillsOnlyInMain', [])) or '-'}\n"
        f"- **Active Skills Only in Ref**: {', '.join(sd.get('activeSkillsOnlyInRef', [])) or '-'}\n"
        f"- **Shared Active Skills**: {', '.join(sd.get('sharedActiveSkills', [])) or '-'}"
    )

    lines.append("\n#### Equipment Slot Diff")
    slot_rows = [[s["slot"], s["mainItem"], s["refItem"]] for s in res.get("itemSlotDiff", [])]
    lines.append(render_table(["Slot", "Main Item", "Ref Item"], slot_rows))

    _echo_utf8("\n".join(lines))


@main.command("borrow")
@click.option("--item-slot", "item_slot", help="Comma-separated equipment slot(s) to borrow from `ref` into `main` (e.g. 'Weapon 1,Amulet').")
@click.option("--skill-group", "skill_group", help="Skill group ('main', index, or label) to borrow from `ref` into `main`.")
@click.option("--apply", is_flag=True, help="Persist borrowed items/skills into `main` build.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_borrow(item_slot: str | None, skill_group: str | None, apply: bool, as_json: bool) -> None:
    """Borrow items and/or a skill group from `ref` into `main` and compute exact delta."""
    slots = [x.strip() for x in item_slot.split(",") if x.strip()] if item_slot else []
    res = call_daemon(
        "borrow",
        {
            "item_slots": slots,
            "skill_group": skill_group,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    notices = res.get("detail", {}).get("notices", [])
    report = _render_mutation_report("Borrow from Reference Build (`ref` -> `main`)", res)
    if notices:
        notice_block = "\n#### Transplant Notices\n" + "\n".join(f"- {n}" for n in notices)
        report += notice_block
    _echo_utf8(report)


# --- 4.5 External Price & Wiki Queries ---


@main.command("price")
@click.argument("query")
@click.option("--league", help="League name (defaults to current primary softcore trade league).")
@click.option("--category", default="all", show_default=True, help="Category: unique, weapon, armour, accessory, flask, jewel, cluster, gem, currency, card, oil, scarab, tattoo, omen, fossil, essence, all.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_price(query: str, league: str | None, category: str, as_json: bool) -> None:
    """Query poe.ninja economy prices (15m local cache)."""
    res = query_price(query, league=league, category=category)
    if as_json:
        _echo_utf8(to_json_str(res, sanitize=False))
        return

    rows = []
    for m in res.get("matches", []):
        trend = m.get("trend24hPct")
        trend_str = f"{float(trend):+.1f}%" if trend is not None else "-"
        rows.append(
            [
                m.get("name"),
                m.get("type"),
                m.get("variant") or (f"{m['links']}L" if m.get("links") else "-"),
                fmt_num(m.get("chaosValue"), 1),
                fmt_num(m.get("divineValue"), 2) if m.get("divineValue") is not None else "-",
                trend_str,
                m.get("listingCount") if m.get("listingCount") is not None else "-",
            ]
        )
    div_rate = res.get("divineChaosRate")
    rate_str = f" | 1 Divine = `{fmt_num(div_rate, 1)}` Chaos" if div_rate else ""
    _echo_utf8(f"### poe.ninja Price Search: `{query}` (League: `{res.get('league')}`{rate_str})")
    _echo_utf8(render_table(["Item / Currency", "Category", "Variant / Links", "Chaos", "Divine", "24h Trend", "Listings"], rows))


@main.command("wiki")
@click.argument("topic")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_wiki(topic: str, as_json: bool) -> None:
    """Query poewiki.net MediaWiki API for mechanics, uniques, and keystones (24h local cache)."""
    res = query_wiki(topic)
    if as_json:
        _echo_utf8(to_json_str(res, sanitize=False))
        return

    _echo_utf8(f"### PoE Wiki: {res.get('title')} ({res.get('url')})\n")
    _echo_utf8(res.get("summary") or "")


if __name__ == "__main__":
    main()
