"""Build inspection, offence/defence summary, 4-part inflation audit, and calcs breakdown."""

from __future__ import annotations

import re
from typing import Any

from .bridge_client import BridgeWorker
from .formatters import strip_pob_escapes
from .snapshot import capture_digest, diff_digest

# Config variables that represent permanent character choices and must NEVER be stripped in de-watering
PERMANENT_CONFIG_VARS = frozenset({
    "bandit",
    "pantheonMajorGod",
    "pantheonMinorGod",
    "resistancePenalty",
    "characterLevel",
})

# Config variables that represent temporary buffs, conditional states, or boss/target overrides
INFLATION_CONFIG_PREFIXES = (
    "use",
    "buff",
    "condition",
    "multiplier",
    "override",
)

INFLATION_CONFIG_EXACT = frozenset({
    "onslaught",
    "adrenaline",
    "fortify",
    "tailwind",
    "elusive",
    "unholyMight",
    "divineBlessing",
    "innervation",
    "infusedChannelling",
})

TEMPORARY_SKILL_PATTERNS = (
    "molten shell",
    "steelskin",
    "immortal call",
    "arcane cloak",
    "berserk",
    "frost shield",
    "sigil of power",
    "focus",
    "vaal breach",
    "vaal impurity",
    "vaal righteous fire",
    "熔岩护盾",
    "钢铁之肤",
    "不朽怒嚎",
    "奥术斗篷",
    "狂战",
    "冰霜护盾",
    "威能法印",
    "瓦尔裂隙",
    "瓦尔不净",
    "瓦尔正义之火",
)


def extract_summary(worker: BridgeWorker) -> dict[str, Any]:
    """Extract a complete structured summary of the currently loaded build."""
    with worker.lock:
        info = worker.call("get_build_info", {})
        header = worker.call("get_build_header", {})
        stats_res = worker.call("get_stats", {})
        sidebar = worker.call("get_sidebar", {})
        skills_res = worker.call("list_skills", {})
        specs_res = worker.call("list_specs", {})
        items_res = worker.call("list_items", {})
        config_res = worker.call("list_config", {})

    stats = stats_res.get("stats", {})
    warnings = [
        strip_pob_escapes(w.get("text") or w.get("raw") or "")
        for w in sidebar.get("warnings", [])
        if (w.get("text") or w.get("raw"))
    ]

    # Identify main skill group & gems
    main_idx = skills_res.get("mainSocketGroup") or header.get("mainSocketGroup", {}).get("index") or 1
    main_group: dict[str, Any] | None = None
    for g in skills_res.get("groups", []):
        if g.get("index") == main_idx or g.get("isMain"):
            main_group = g
            break

    # Detect minion sidebar rows if any
    minion_sidebar: list[dict[str, str]] = []
    for row in sidebar.get("rows", []):
        if row.get("actor") == "minion" and (row.get("lhs") or row.get("rhs")):
            minion_sidebar.append(
                {
                    "label": strip_pob_escapes(row.get("lhs", "")),
                    "value": strip_pob_escapes(row.get("rhs", "")),
                    "stat": row.get("stat") or "",
                }
            )

    has_minion_stats = bool(minion_sidebar) or float(stats.get("MinionCombinedDPS") or 0) > 0

    offence = {
        "FullDPS": stats.get("FullDPS"),
        "CombinedDPS": stats.get("CombinedDPS"),
        "TotalDPS": stats.get("TotalDPS"),
        "TotalDot": stats.get("TotalDot"),
        "TotalDotDPS": stats.get("TotalDotDPS"),
        "WithImpaleDPS": stats.get("WithImpaleDPS"),
        "BleedDPS": stats.get("BleedDPS"),
        "IgniteDPS": stats.get("IgniteDPS"),
        "PoisonDPS": stats.get("PoisonDPS"),
        "ImpaleDPS": stats.get("ImpaleDPS"),
        "AverageDamage": stats.get("AverageDamage"),
        "AverageHit": stats.get("AverageHit"),
        "Speed": stats.get("Speed"),
        "CritChance": stats.get("CritChance"),
        "EffectiveCritChance": stats.get("EffectiveCritChance"),
        "CritMultiplier": stats.get("CritMultiplier"),
        "HitChance": stats.get("HitChance"),
        "ManaCost": stats.get("ManaCost"),
        "MainHandAccuracy": stats.get("MainHandAccuracy"),
    }

    minion_offence = None
    if has_minion_stats:
        minion_offence = {
            "MinionCombinedDPS": stats.get("MinionCombinedDPS"),
            "MinionTotalDPS": stats.get("MinionTotalDPS"),
            "MinionTotalDot": stats.get("MinionTotalDot"),
            "MinionAverageDamage": stats.get("MinionAverageDamage"),
            "MinionSpeed": stats.get("MinionSpeed"),
            "MinionCritChance": stats.get("MinionCritChance"),
            "MinionCritMultiplier": stats.get("MinionCritMultiplier"),
            "MinionHitChance": stats.get("MinionHitChance"),
            "MinionLife": stats.get("MinionLife"),
            "sidebarRows": minion_sidebar,
        }

    defence = {
        "TotalEHP": stats.get("TotalEHP"),
        "Life": stats.get("Life"),
        "LifeUnreserved": stats.get("LifeUnreserved"),
        "LifeUnreservedPercent": stats.get("LifeUnreservedPercent"),
        "Mana": stats.get("Mana"),
        "ManaUnreserved": stats.get("ManaUnreserved"),
        "ManaUnreservedPercent": stats.get("ManaUnreservedPercent"),
        "EnergyShield": stats.get("EnergyShield"),
        "Ward": stats.get("Ward"),
        "Armour": stats.get("Armour"),
        "PhysicalDamageReduction": stats.get("PhysicalDamageReduction"),
        "Evasion": stats.get("Evasion"),
        "MeleeEvadeChance": stats.get("MeleeEvadeChance"),
        "ProjectileEvadeChance": stats.get("ProjectileEvadeChance"),
        "PhysicalMaximumHitTaken": stats.get("PhysicalMaximumHitTaken"),
        "FireMaximumHitTaken": stats.get("FireMaximumHitTaken"),
        "ColdMaximumHitTaken": stats.get("ColdMaximumHitTaken"),
        "LightningMaximumHitTaken": stats.get("LightningMaximumHitTaken"),
        "ChaosMaximumHitTaken": stats.get("ChaosMaximumHitTaken"),
        "FireResist": stats.get("FireResist"),
        "FireResistOverCap": stats.get("FireResistOverCap"),
        "ColdResist": stats.get("ColdResist"),
        "ColdResistOverCap": stats.get("ColdResistOverCap"),
        "LightningResist": stats.get("LightningResist"),
        "LightningResistOverCap": stats.get("LightningResistOverCap"),
        "ChaosResist": stats.get("ChaosResist"),
        "ChaosResistOverCap": stats.get("ChaosResistOverCap"),
        "EffectiveBlockChance": stats.get("EffectiveBlockChance"),
        "EffectiveSpellBlockChance": stats.get("EffectiveSpellBlockChance"),
        "EffectiveSpellSuppressionChance": stats.get("EffectiveSpellSuppressionChance"),
        "NetLifeRegen": stats.get("LifeRegenRecovery") if stats.get("LifeRegenRecovery") is not None else stats.get("LifeRegen"),
        "NetManaRegen": stats.get("ManaRegenRecovery") if stats.get("ManaRegenRecovery") is not None else stats.get("ManaRegen"),
        "EnergyShieldRegen": stats.get("EnergyShieldRegenRecovery") if stats.get("EnergyShieldRegenRecovery") is not None else stats.get("EnergyShieldRegen"),
        "Str": stats.get("Str"),
        "ReqStr": stats.get("ReqStr"),
        "Dex": stats.get("Dex"),
        "ReqDex": stats.get("ReqDex"),
        "Int": stats.get("Int"),
        "ReqInt": stats.get("ReqInt"),
    }

    return {
        "buildInfo": info,
        "buildHeader": header,
        "activeSpec": specs_res.get("activeSpec"),
        "specs": specs_res.get("specs", []),
        "activeItemSetId": items_res.get("activeItemSetId"),
        "itemSets": items_res.get("itemSets", []),
        "activeSkillSetId": skills_res.get("activeSkillSetId"),
        "skillSets": skills_res.get("skillSets", []),
        "activeConfigSetId": config_res.get("activeConfigSetId"),
        "configSets": config_res.get("configSets", []),
        "mainGroup": main_group,
        "offence": offence,
        "minionOffence": minion_offence,
        "defence": defence,
        "warnings": warnings,
    }


def _has_mageblood_equipped(items_res: dict[str, Any]) -> bool:
    """Return True if a Belt slot has Mageblood equipped."""
    items_by_id = {it["id"]: it for it in items_res.get("items", [])}
    for slot in items_res.get("slots", []):
        if slot.get("name") == "Belt" and slot.get("selItemId"):
            item = items_by_id.get(slot["selItemId"], {})
            text = " ".join(
                str(item.get(k) or "")
                for k in ("name", "nameZh", "title", "titleZh", "baseName", "baseNameZh")
            ).lower()
            if "mageblood" in text or "法师之血" in text:
                return True
    return False


def dewater_build_in_place(worker: BridgeWorker) -> dict[str, list[str]]:
    """Strip PoB inflation ('water') in place and set enemyIsBoss='Pinnacle'.

    Must be called inside a snapshot wrapper if non-destructive behavior is desired.
    Returns a dictionary of stripped inflation items across the 4 audit dimensions.
    """
    stripped: dict[str, list[str]] = {
        "config": [],
        "flasks": [],
        "skills": [],
        "party": [],
    }

    # 1. Config inflation & enemyIsBoss = Pinnacle
    cfg_res = worker.call("list_config", {})
    current_boss = None
    had_preset_boss_skills = False

    for sec in cfg_res.get("sections", []):
        for item in sec.get("items", []):
            var = item.get("var")
            val = item.get("value")
            if not var or var in PERMANENT_CONFIG_VARS:
                continue
            if var == "enemyIsBoss":
                current_boss = val
                continue
            if val in (None, False, "", 0):
                continue

            if var == "presetBossSkills":
                had_preset_boss_skills = True
                stripped["config"].append(f"{var}={val!r} -> reset")
                worker.call("reset_config", {"var": var})
            elif var.startswith(INFLATION_CONFIG_PREFIXES) or var in INFLATION_CONFIG_EXACT:
                stripped["config"].append(f"{var}={val!r} -> reset")
                worker.call("reset_config", {"var": var})

    if current_boss not in ("Pinnacle", "Uber"):
        stripped["config"].append(f"enemyIsBoss={current_boss!r} -> 'Pinnacle'")
    worker.call("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"})
    if had_preset_boss_skills:
        # Second call ensures ConfigTab BuildModList sees updated enemyIsBoss selIndex
        worker.call("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"})

    # Check customMods (use '# disabled' comment block so ConfigTab.lua doesn't fall back to legacy input.customMods)
    custom_mods = cfg_res.get("customMods", [])
    active_custom_lines: list[str] = []
    for block in custom_mods:
        if block.get("enabled", True):
            text = (block.get("text") or "").strip()
            for line in text.splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    active_custom_lines.append(s)
    if active_custom_lines:
        stripped["config"].append(f"customMods ({len(active_custom_lines)} active lines: {'; '.join(active_custom_lines[:3])}) -> disabled")
        worker.call(
            "set_custom_mods",
            {"list": [{"title": "Default", "text": "# disabled", "enabled": True}]},
        )

    # 2. Flask inflation (active flasks without Mageblood)
    items_res = worker.call("list_items", {})
    has_mb = _has_mageblood_equipped(items_res)
    items_by_id = {it["id"]: it for it in items_res.get("items", [])}
    if not has_mb:
        for slot in items_res.get("slots", []):
            if slot.get("isFlask") and slot.get("active"):
                slot_name = slot["name"]
                item = items_by_id.get(slot.get("selItemId") or 0, {})
                item_title = strip_pob_escapes(item.get("titleZh") or item.get("title") or item.get("name") or slot_name)
                stripped["flasks"].append(f"{slot_name} ({item_title}) -> active=False")
                worker.call("set_slot_active", {"slotName": slot_name, "active": False})

    # 3. Temporary guard / burst / Vaal skills
    skills_res = worker.call("list_skills", {})
    for group in skills_res.get("groups", []):
        if not group.get("enabled", True):
            continue
        g_idx = group["index"]
        for gem in group.get("gems", []):
            if not gem.get("enabled", True) or gem.get("support"):
                continue
            names = [
                str(gem.get("nameSpec") or ""),
                str(gem.get("name") or ""),
                str(gem.get("nameZh") or ""),
                str(gem.get("skillId") or ""),
            ]
            combined_lower = " ".join(names).lower()
            is_vaal = (
                combined_lower.startswith("vaal ")
                or " vaal " in combined_lower
                or "瓦尔" in combined_lower
            )
            is_temp = any(pat in combined_lower for pat in TEMPORARY_SKILL_PATTERNS)
            disp = strip_pob_escapes(gem.get("nameZh") or gem.get("name") or gem.get("nameSpec") or "")
            if is_temp:
                stripped["skills"].append(f"Group #{g_idx} Gem #{gem['index']} ({disp}) -> enabled=False")
                worker.call("set_gem", {"group": g_idx, "index": gem["index"], "enabled": False})
            elif is_vaal and gem.get("enableGlobal2", False):
                stripped["skills"].append(
                    f"Group #{g_idx} Gem #{gem['index']} ({disp}) -> enableGlobal2=False (base skill kept)"
                )
                worker.call(
                    "set_gem",
                    {
                        "group": g_idx,
                        "index": gem["index"],
                        "enableGlobal1": True,
                        "enableGlobal2": False,
                    },
                )

    # 4. PartyTab buffs
    party_res = worker.call("get_party", {})
    party_fields = party_res.get("fields", {})
    non_empty_party = [k for k, v in party_fields.items() if str(v or "").strip()]
    if non_empty_party or party_res.get("enableExportBuffs"):
        stripped["party"].append(f"PartyTab fields ({', '.join(non_empty_party or ['enableExportBuffs'])}) -> cleared & disabled")
        worker.call("party_action", {"action": "disable"})
        worker.call("party_action", {"action": "clear"})

    return stripped


def audit_build(worker: BridgeWorker) -> dict[str, Any]:
    """Run static build health checks + non-destructive 4-part PoB inflation ('water') audit."""
    with worker.lock:
        snap_code = worker.call("export_code", {})["code"]
        digest_before = capture_digest(worker)
        stats = digest_before["stats"]
        info = digest_before["buildInfo"]
        items_res = worker.call("list_items", {})

        # Static health checks
        health_issues: list[dict[str, str]] = []

        # 1. Elemental & Chaos resistances
        for elem in ("Fire", "Cold", "Lightning"):
            val = float(stats.get(f"{elem}Resist") or 0)
            over = float(stats.get(f"{elem}ResistOverCap") or 0)
            if val < 75:
                health_issues.append(
                    {
                        "category": "Resistance",
                        "severity": "HIGH",
                        "message": f"{elem} Resist is uncapped: {val:.0f}% (gap: {75 - val:.0f}%)",
                    }
                )
            elif over < 15:
                health_issues.append(
                    {
                        "category": "Resistance",
                        "severity": "LOW",
                        "message": f"{elem} Resist overcap is thin: +{over:.0f}% over cap (vulnerable to curses/exposure)",
                    }
                )

        chaos_res = float(stats.get("ChaosResist") or 0)
        if chaos_res < 0:
            health_issues.append(
                {
                    "category": "Resistance",
                    "severity": "MEDIUM",
                    "message": f"Chaos Resist is negative: {chaos_res:.0f}%",
                }
            )

        # 2. Attributes vs Requirements
        for attr in ("Str", "Dex", "Int"):
            cur = float(stats.get(attr) or 0)
            req = float(stats.get(f"Req{attr}") or 0)
            if req > cur:
                health_issues.append(
                    {
                        "category": "Attribute",
                        "severity": "HIGH",
                        "message": f"{attr} insufficient: have {cur:.0f}, require {req:.0f}",
                    }
                )

        # 3. Accuracy / Hit Chance
        hit_chance = stats.get("HitChance")
        if hit_chance is not None and float(hit_chance) < 100.0:
            health_issues.append(
                {
                    "category": "Offence",
                    "severity": "MEDIUM",
                    "message": f"Hit Chance is {float(hit_chance):.1f}% (< 100%)",
                }
            )

        # 4. Mana Cost vs Unreserved Mana & Regen
        mana_cost = float(stats.get("ManaCost") or 0)
        mana_unres = float(stats.get("ManaUnreserved") or 0)
        raw_mana_regen = (
            stats.get("ManaRegenRecovery")
            if stats.get("ManaRegenRecovery") is not None
            else stats.get("ManaRegen")
        )
        mana_regen = float(raw_mana_regen or 0)
        mana_leech = float(stats.get("ManaLeechGainRate") or 0)
        speed = float(stats.get("Speed") or 0)
        if mana_cost > 0 and mana_cost > mana_unres and float(stats.get("LifeUnreserved") or 0) > 0:
            health_issues.append(
                {
                    "category": "Sustain",
                    "severity": "HIGH",
                    "message": f"Skill Mana Cost ({mana_cost:.0f}) exceeds Unreserved Mana ({mana_unres:.0f})",
                }
            )
        elif mana_cost > 0 and mana_regen < 0:
            health_issues.append(
                {
                    "category": "Sustain",
                    "severity": "MEDIUM",
                    "message": f"Net Mana Regen is negative ({mana_regen:.1f}/s) with Mana Cost {mana_cost:.0f}",
                }
            )
        elif (
            mana_cost > 0
            and speed > 0
            and (mana_cost * speed) > (max(0.0, mana_regen) + mana_leech)
            and float(stats.get("LifeUnreserved") or 0) > 0
        ):
            spend_rate = mana_cost * speed
            rec_rate = max(0.0, mana_regen) + mana_leech
            health_issues.append(
                {
                    "category": "Sustain",
                    "severity": "LOW",
                    "message": (
                        f"Mana spend rate ({spend_rate:.1f}/s = {mana_cost:.0f} * {speed:.2f}/s) "
                        f"exceeds net Mana recovery + leech ({rec_rate:.1f}/s)"
                    ),
                }
            )

        # 5. Unallocated points
        pts = info.get("points", {})
        used, used_max = int(pts.get("used") or 0), int(pts.get("usedMax") or 0)
        asc_used, asc_max = int(pts.get("ascUsed") or 0), int(pts.get("ascMax") or 0)
        if used > used_max:
            health_issues.append(
                {
                    "category": "Points",
                    "severity": "HIGH",
                    "message": f"Over-allocated passive points: {used}/{used_max}",
                }
            )
        if asc_used < asc_max:
            health_issues.append(
                {
                    "category": "Points",
                    "severity": "LOW",
                    "message": f"Unallocated Ascendancy points: {asc_used}/{asc_max}",
                }
            )

        # 6. Sidebar warnings
        for w in digest_before["warnings"]:
            health_issues.append(
                {
                    "category": "EngineWarning",
                    "severity": "HIGH",
                    "message": w,
                }
            )

        # 7. Empty primary equipment slots
        primary_slots = {"Weapon 1", "Body Armour", "Helmet", "Gloves", "Boots", "Amulet", "Ring 1", "Ring 2", "Belt"}
        for slot in items_res.get("slots", []):
            if slot.get("name") in primary_slots and not slot.get("inactive") and not slot.get("selItemId"):
                health_issues.append(
                    {
                        "category": "Equipment",
                        "severity": "MEDIUM",
                        "message": f"Equipment slot '{slot['name']}' is empty",
                    }
                )

        # Run non-destructive de-watering snapshot
        try:
            stripped = dewater_build_in_place(worker)
            digest_dewatered = capture_digest(worker)
            dewater_diff = diff_digest(digest_before, digest_dewatered)
        finally:
            worker.call("import_code", {"code": snap_code, "mode": "replace"})

        return {
            "healthIssues": health_issues,
            "inflationStripped": stripped,
            "original": {
                "primaryDpsKey": digest_before["primaryDpsKey"],
                "primaryDps": digest_before["primaryDps"],
                "fullDps": stats.get("FullDPS"),
                "combinedDps": stats.get("CombinedDPS"),
                "totalDps": stats.get("TotalDPS"),
                "minionCombinedDps": stats.get("MinionCombinedDPS"),
                "totalEhp": digest_before["totalEhp"],
                "life": digest_before["life"],
                "physMaxHit": stats.get("PhysicalMaximumHitTaken"),
                "eleMaxHit": min(
                    float(stats.get("FireMaximumHitTaken") or 0),
                    float(stats.get("ColdMaximumHitTaken") or 0),
                    float(stats.get("LightningMaximumHitTaken") or 0),
                ),
                "chaosMaxHit": stats.get("ChaosMaximumHitTaken"),
            },
            "dewateredPinnacle": {
                "primaryDpsKey": digest_dewatered["primaryDpsKey"],
                "primaryDps": digest_dewatered["primaryDps"],
                "fullDps": digest_dewatered["stats"].get("FullDPS"),
                "combinedDps": digest_dewatered["stats"].get("CombinedDPS"),
                "totalDps": digest_dewatered["stats"].get("TotalDPS"),
                "minionCombinedDps": digest_dewatered["stats"].get("MinionCombinedDPS"),
                "totalEhp": digest_dewatered["totalEhp"],
                "life": digest_dewatered["life"],
                "physMaxHit": digest_dewatered["stats"].get("PhysicalMaximumHitTaken"),
                "eleMaxHit": min(
                    float(digest_dewatered["stats"].get("FireMaximumHitTaken") or 0),
                    float(digest_dewatered["stats"].get("ColdMaximumHitTaken") or 0),
                    float(digest_dewatered["stats"].get("LightningMaximumHitTaken") or 0),
                ),
                "chaosMaxHit": digest_dewatered["stats"].get("ChaosMaximumHitTaken"),
            },
            "dewaterDiff": dewater_diff,
        }


def resolve_slot_name(worker: BridgeWorker, slot_query: str) -> str:
    """Resolve user slot query (including '#1', '#2' jewel socket shorthand or Flask 1..5) to exact PoB slotName."""
    q = slot_query.strip()
    items_res = worker.call("list_items", {})
    slots = items_res.get("slots", [])

    # Check '#1', '#2' jewel socket shorthand
    m_jewel = re.match(r"^#?(\d+)$", q)
    if m_jewel:
        idx = int(m_jewel.group(1))
        for s in slots:
            if s.get("socketIndex") == idx:
                return s["name"]
        raise ValueError(f"Jewel socket #{idx} not found in build slots.")

    # Check exact or case-insensitive match on name / label / labelZh
    q_lower = q.lower()
    for s in slots:
        if (
            s.get("name", "").lower() == q_lower
            or (s.get("label") or "").lower() == q_lower
            or (s.get("labelZh") or "").lower() == q_lower
        ):
            return s["name"]

    # Check alias like "Flask 1" -> "Flask 1"
    for s in slots:
        if q_lower in s.get("name", "").lower() or q_lower in (s.get("labelZh") or "").lower():
            return s["name"]

    raise ValueError(f"Equipment slot '{slot_query}' not found.")


def inspect_items(
    worker: BridgeWorker,
    *,
    slot_name: str | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    """List equipped items or inspect a single slot's tooltip and raw text."""
    with worker.lock:
        items_res = worker.call("list_items", {})
        items_by_id = {it["id"]: it for it in items_res.get("items", [])}

        if slot_name:
            resolved_slot = resolve_slot_name(worker, slot_name)
            slot_obj = next((s for s in items_res.get("slots", []) if s["name"] == resolved_slot), None)
            item_id = slot_obj.get("selItemId") if slot_obj else None
            if not item_id:
                return {
                    "slot": slot_obj or {"name": resolved_slot},
                    "item": None,
                    "tooltip": None,
                    "raw": None,
                }
            tip = worker.call("item_tooltip", {"id": item_id, "slotName": resolved_slot, "compare": False})
            raw_text = worker.call("item_raw", {"id": item_id}).get("raw") if raw else None
            return {
                "slot": slot_obj,
                "item": items_by_id.get(item_id),
                "tooltip": tip,
                "raw": raw_text,
            }

        equipped_slots: list[dict[str, Any]] = []
        for s in items_res.get("slots", []):
            item_id = s.get("selItemId")
            if not item_id and not s.get("shown"):
                continue
            item = items_by_id.get(item_id) if item_id else None
            entry: dict[str, Any] = {
                "slotName": s["name"],
                "label": s.get("labelZh") or s.get("label") or s["name"],
                "socketIndex": s.get("socketIndex"),
                "nodeId": s.get("nodeId"),
                "isFlask": bool(s.get("isFlask")),
                "active": bool(s.get("active")),
                "inactive": bool(s.get("inactive")),
                "itemId": item_id,
                "item": item,
            }
            if raw and item_id:
                entry["raw"] = worker.call("item_raw", {"id": item_id}).get("raw")
            equipped_slots.append(entry)

        return {
            "activeItemSetId": items_res.get("activeItemSetId"),
            "itemSets": items_res.get("itemSets", []),
            "equippedSlots": equipped_slots,
            "totalItemsInBuild": len(items_res.get("items", [])),
        }


def resolve_skill_group_index(skills_res: dict[str, Any], group_selector: str | int | None) -> int:
    """Resolve 'main', 1-based group index, or group label substring into 1-based group index."""
    groups = skills_res.get("groups", [])
    if not groups:
        raise ValueError("Build has no skill groups.")
    if group_selector is None or str(group_selector).strip().lower() == "main":
        return int(skills_res.get("mainSocketGroup") or 1)
    s = str(group_selector).strip()
    if s.isdigit():
        idx = int(s)
        if any(g.get("index") == idx for g in groups):
            return idx
        raise ValueError(f"Skill group #{idx} does not exist (1..{len(groups)}).")
    s_lower = s.lower()
    for g in groups:
        for key in ("label", "displayLabel", "displayLabelZh"):
            if s_lower in str(g.get(key) or "").lower():
                return int(g["index"])
    raise ValueError(f"Could not find skill group matching '{group_selector}'.")


def inspect_skills(
    worker: BridgeWorker,
    *,
    group_selector: str | None = None,
) -> dict[str, Any]:
    """List all skill groups or inspect a specific group with per-gem DPS tooltips."""
    with worker.lock:
        skills_res = worker.call("list_skills", {})
        if group_selector is not None:
            idx = resolve_skill_group_index(skills_res, group_selector)
            group = next(g for g in skills_res.get("groups", []) if g["index"] == idx)
            gems_with_tips = []
            for gem in group.get("gems", []):
                tip = worker.call("gem_tooltip", {"group": idx, "index": gem["index"]})
                gems_with_tips.append({**gem, "tooltipDps": tip.get("dps")})
            return {
                "group": {**group, "gems": gems_with_tips},
                "mainSocketGroup": skills_res.get("mainSocketGroup"),
            }
        return skills_res


def inspect_tree(
    worker: BridgeWorker,
    *,
    search: str | None = None,
) -> dict[str, Any]:
    """Summarize allocated Keystones, Notables, Masteries, Ascendancy nodes, Jewels, or search tree nodes."""
    with worker.lock:
        tree_state = worker.call("get_tree_state", {})
        specs_res = worker.call("list_specs", {})
        items_res = worker.call("list_items", {})
        tree_version = tree_state.get("treeVersion")
        nodes_db = worker.get_cached_tree_data(tree_version).get("nodes", {})

        # Build map of dynamic/overridden nodes from get_tree_state
        overrides = tree_state.get("overrides", {})
        dynamic_map = {str(n["id"]): n for n in tree_state.get("dynamicNodes", [])}
        allocated_ids = [int(nid) for nid in tree_state.get("allocatedNodes", [])]
        allocated_set = set(allocated_ids)

        # Map socket nodeId -> socketIndex from list_items.slots
        socket_index_by_node: dict[int, int] = {}
        for slot in items_res.get("slots", []):
            if slot.get("nodeId") and slot.get("socketIndex"):
                socket_index_by_node[int(slot["nodeId"])] = int(slot["socketIndex"])

        def _get_node_meta(nid: int) -> dict[str, Any]:
            sid = str(nid)
            base = dict(nodes_db.get(sid) or {})
            if sid in dynamic_map:
                base.update(dynamic_map[sid])
            if sid in overrides:
                ov = overrides[sid]
                base["overrideWhy"] = ov.get("why")
                if ov.get("name"):
                    base["name"] = ov["name"]
                if ov.get("nameZh"):
                    base["nameZh"] = ov["nameZh"]
                if ov.get("stats"):
                    base["stats"] = ov["stats"]
                if ov.get("statsZh"):
                    base["statsZh"] = ov["statsZh"]
            base["id"] = nid
            base["allocated"] = nid in allocated_set
            if nid in socket_index_by_node:
                base["socketIndex"] = socket_index_by_node[nid]
            return base

        if search:
            q = search.strip()
            # Check '#1', '#2' jewel socket shorthand
            m_sock = re.match(r"^#(\d+)$", q)
            matches: list[dict[str, Any]] = []
            if m_sock:
                target_idx = int(m_sock.group(1))
                for nid, s_idx in socket_index_by_node.items():
                    if s_idx == target_idx:
                        meta = _get_node_meta(nid)
                        hover = worker.call("node_hover", {"id": nid})
                        meta["cost"] = hover.get("cost")
                        meta["path"] = hover.get("path")
                        matches.append(meta)
            else:
                q_lower = q.lower()
                all_node_ids = set(nodes_db.keys()) | set(dynamic_map.keys())
                for sid in all_node_ids:
                    nid = int(sid)
                    meta = _get_node_meta(nid)
                    haystack = " ".join(
                        [
                            str(meta.get("id") or ""),
                            str(meta.get("name") or ""),
                            str(meta.get("nameZh") or ""),
                            " ".join(str(s) for s in (meta.get("stats") or [])),
                            " ".join(str(s) for s in (meta.get("statsZh") or [])),
                        ]
                    ).lower()
                    if q_lower in haystack:
                        matches.append(meta)

                # Sort allocated first, then Keystone/Notable/Mastery/Socket before Normal
                type_rank = {"Keystone": 0, "Notable": 1, "Mastery": 2, "Socket": 3, "Normal": 4}
                matches.sort(
                    key=lambda m: (
                        0 if m.get("allocated") else 1,
                        type_rank.get(m.get("type", "Normal"), 5),
                        m.get("id", 0),
                    )
                )
                # Enrich top 25 matches with path cost
                for m in matches[:25]:
                    try:
                        hover = worker.call("node_hover", {"id": m["id"]})
                        m["cost"] = hover.get("cost")
                    except Exception:
                        pass

            return {
                "search": search,
                "totalMatches": len(matches),
                "matches": matches[:50],
            }

        keystones: list[dict[str, Any]] = []
        notables: list[dict[str, Any]] = []
        masteries: list[dict[str, Any]] = []
        ascendancy_nodes: list[dict[str, Any]] = []

        for nid in sorted(allocated_ids):
            meta = _get_node_meta(nid)
            ntype = meta.get("type")
            if meta.get("asc"):
                if ntype not in ("AscendClassStart", "ClassStart"):
                    ascendancy_nodes.append(meta)
            elif ntype == "Keystone":
                keystones.append(meta)
            elif ntype == "Notable":
                notables.append(meta)
            elif ntype == "Mastery" or meta.get("overrideWhy") == "mastery":
                masteries.append(meta)

        jewel_sockets = []
        for sock in tree_state.get("sockets", []):
            nid = int(sock["nodeId"])
            jewel_sockets.append(
                {
                    **sock,
                    "socketIndex": socket_index_by_node.get(nid),
                    "allocated": nid in allocated_set,
                }
            )

        return {
            "treeVersion": tree_version,
            "className": tree_state.get("className"),
            "ascendClassName": tree_state.get("ascendClassName"),
            "points": tree_state.get("points"),
            "activeSpec": specs_res.get("activeSpec"),
            "specs": specs_res.get("specs", []),
            "allocatedCount": len(allocated_ids),
            "keystones": keystones,
            "ascendancyNodes": ascendancy_nodes,
            "masteries": masteries,
            "notables": notables,
            "jewelSockets": jewel_sockets,
        }


def search_calcs_breakdown(worker: BridgeWorker, keyword: str) -> dict[str, Any]:
    """Search `get_calcs` rows by keyword and fetch `calcs_breakdown` for matching cells."""
    q = keyword.strip().lower()
    with worker.lock:
        calcs = worker.call("get_calcs", {})
        matches: list[dict[str, Any]] = []

        for sec in calcs.get("sections", []):
            si = sec.get("si")
            sec_id = sec.get("id")
            sec_group = sec.get("group")
            for sub in sec.get("subsections", []):
                ui = sub.get("ui")
                sub_label = sub.get("labelZh") or sub.get("label") or ""
                for row in sub.get("rows", []):
                    ri = row.get("ri")
                    r_label = row.get("label") or ""
                    r_label_zh = row.get("labelZh") or ""
                    haystack = f"{sec_id} {sec_group} {sub.get('label', '')} {sub_label} {r_label} {r_label_zh}".lower()
                    if q in haystack:
                        cells_out = []
                        for cell in row.get("cells", []):
                            ci = cell.get("ci")
                            c_entry = {
                                "ci": ci,
                                "text": strip_pob_escapes(cell.get("text") or cell.get("raw") or ""),
                                "hasBreakdown": bool(cell.get("hasBreakdown")),
                            }
                            if cell.get("hasBreakdown") and len(matches) < 8:
                                try:
                                    bd = worker.call(
                                        "calcs_breakdown",
                                        {"si": si, "ui": ui, "ri": ri, "ci": ci},
                                    )
                                    c_entry["breakdown"] = bd.get("sections", [])
                                except Exception:
                                    pass
                            cells_out.append(c_entry)
                        matches.append(
                            {
                                "sectionId": sec_id,
                                "sectionGroup": sec_group,
                                "subsection": sub_label,
                                "rowLabel": r_label,
                                "rowLabelZh": r_label_zh,
                                "coords": {"si": si, "ui": ui, "ri": ri},
                                "cells": cells_out,
                            }
                        )

        return {
            "keyword": keyword,
            "totalMatches": len(matches),
            "matches": matches[:20],
        }
