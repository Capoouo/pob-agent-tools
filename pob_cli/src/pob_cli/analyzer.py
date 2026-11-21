"""Build inspection, offence/defence summary, 4-part inflation audit, and calcs breakdown."""

from __future__ import annotations

from collections import deque
import math
import re
from typing import Any, Iterable

from .bridge_client import BridgeWorker
from .formatters import strip_pob_escapes
from .snapshot import (
    _call_batch,
    _finite_float,
    _json_safe_stat,
    _safe_min_ele_max_hit,
    _stat_float,
    capture_digest,
    diff_digest,
)

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
    """Extract a complete structured summary of the currently loaded build in one pipelined batch."""
    with worker.lock:
        stats_res, sidebar, info, header, skills_res, specs_res, items_res = _call_batch(
            worker,
            [
                ("get_stats", {}),
                ("get_sidebar", {}),
                ("get_build_info", {}),
                ("get_build_header", {}),
                ("list_skills", {}),
                ("list_specs", {}),
                ("list_items", {}),
            ],
        )

    stats = stats_res.get("stats", {}) if isinstance(stats_res, dict) else {}
    sidebar_warnings = sidebar.get("warnings", []) if isinstance(sidebar, dict) else []
    warnings = [
        strip_pob_escapes(w.get("text") or w.get("raw") or "")
        for w in sidebar_warnings
        if isinstance(w, dict) and (w.get("text") or w.get("raw"))
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

    has_minion_stats = bool(minion_sidebar) or _stat_float(stats.get("MinionCombinedDPS")) > 0

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
        "mainGroup": main_group,
        "offence": offence,
        "minionOffence": minion_offence,
        "defence": defence,
        "warnings": warnings,
    }


get_summary = extract_summary


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

    # Pipeline all 4 read-only inspections in ONE batch
    cfg_res, items_res, skills_res, party_res = _call_batch(
        worker,
        [
            ("list_config", {}),
            ("list_items", {}),
            ("list_skills", {}),
            ("get_party", {}),
        ],
    )

    mutations: list[tuple[str, dict[str, Any]]] = []

    # 1. Config inflation & enemyIsBoss = Pinnacle
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
                mutations.append(("reset_config", {"var": var}))
            elif var.startswith(INFLATION_CONFIG_PREFIXES) or var in INFLATION_CONFIG_EXACT:
                stripped["config"].append(f"{var}={val!r} -> reset")
                mutations.append(("reset_config", {"var": var}))

    if current_boss not in ("Pinnacle", "Uber"):
        stripped["config"].append(f"enemyIsBoss={current_boss!r} -> 'Pinnacle'")
    mutations.append(("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"}))
    if had_preset_boss_skills:
        # Second call ensures ConfigTab BuildModList sees updated enemyIsBoss selIndex
        mutations.append(("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"}))

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
        mutations.append(
            (
                "set_custom_mods",
                {"list": [{"title": "Default", "text": "# disabled", "enabled": True}]},
            )
        )

    # 2. Flask inflation (active flasks without Mageblood)
    has_mb = _has_mageblood_equipped(items_res)
    items_by_id = {it["id"]: it for it in items_res.get("items", [])}
    if not has_mb:
        for slot in items_res.get("slots", []):
            if slot.get("isFlask") and slot.get("active"):
                slot_name = slot["name"]
                item = items_by_id.get(slot.get("selItemId") or 0, {})
                item_title = strip_pob_escapes(item.get("titleZh") or item.get("title") or item.get("name") or slot_name)
                stripped["flasks"].append(f"{slot_name} ({item_title}) -> active=False")
                mutations.append(("set_slot_active", {"slotName": slot_name, "active": False}))

    # 3. Temporary guard / burst / Vaal skills
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
                mutations.append(("set_gem", {"group": g_idx, "index": gem["index"], "enabled": False}))
            elif is_vaal and gem.get("enableGlobal2", False):
                stripped["skills"].append(
                    f"Group #{g_idx} Gem #{gem['index']} ({disp}) -> enableGlobal2=False (base skill kept)"
                )
                mutations.append(
                    (
                        "set_gem",
                        {
                            "group": g_idx,
                            "index": gem["index"],
                            "enableGlobal1": True,
                            "enableGlobal2": False,
                        },
                    )
                )

    # 4. PartyTab buffs
    party_fields = party_res.get("fields", {})
    non_empty_party = [k for k, v in party_fields.items() if str(v or "").strip()]
    if non_empty_party or party_res.get("enableExportBuffs"):
        stripped["party"].append(f"PartyTab fields ({', '.join(non_empty_party or ['enableExportBuffs'])}) -> cleared & disabled")
        mutations.append(("party_action", {"action": "disable"}))
        mutations.append(("party_action", {"action": "clear"}))

    if mutations:
        _call_batch(worker, mutations)

    return stripped


def audit_build(worker: BridgeWorker) -> dict[str, Any]:
    """Run static build health checks + non-destructive 4-part PoB inflation ('water') audit."""
    with worker.lock:
        prev_last_good = getattr(worker, "last_good_code", None)
        export_res, items_res = _call_batch(
            worker,
            [("export_code", {}), ("list_items", {})],
        )
        snap_code = export_res["code"]
        digest_before = capture_digest(worker)
        stats = digest_before["stats"]
        info = digest_before["buildInfo"]

        # Static health checks
        health_issues: list[dict[str, str]] = []

        # 1. Elemental & Chaos resistances
        for elem in ("Fire", "Cold", "Lightning"):
            val = _finite_float(stats.get(f"{elem}Resist"))
            over = _finite_float(stats.get(f"{elem}ResistOverCap"))
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

        chaos_res = _finite_float(stats.get("ChaosResist"))
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
            cur = _finite_float(stats.get(attr))
            req = _finite_float(stats.get(f"Req{attr}"))
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
        if hit_chance is not None:
            hc_val = _finite_float(hit_chance, default=100.0)
            if hc_val < 100.0:
                health_issues.append(
                    {
                        "category": "Offence",
                        "severity": "MEDIUM",
                        "message": f"Hit Chance is {hc_val:.1f}% (< 100%)",
                    }
                )

        # 4. Mana Cost vs Unreserved Mana & Regen
        mana_cost = _finite_float(stats.get("ManaCost"))
        mana_unres = _finite_float(stats.get("ManaUnreserved"))
        raw_mana_regen = (
            stats.get("ManaRegenRecovery")
            if stats.get("ManaRegenRecovery") is not None
            else stats.get("ManaRegen")
        )
        mana_regen = _finite_float(raw_mana_regen)
        mana_leech = _finite_float(stats.get("ManaLeechGainRate"))
        speed = _finite_float(stats.get("Speed"))
        life_unres = _finite_float(stats.get("LifeUnreserved"))
        if mana_cost > 0 and mana_cost > mana_unres and life_unres > 0:
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
            and life_unres > 0
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
            if hasattr(worker, "last_good_code"):
                worker.last_good_code = prev_last_good

        dew_stats = digest_dewatered["stats"]
        orig_ele = _safe_min_ele_max_hit(stats)
        dew_ele = _safe_min_ele_max_hit(dew_stats)
        return {
            "healthIssues": health_issues,
            "inflationStripped": stripped,
            "original": {
                "primaryDpsKey": digest_before["primaryDpsKey"],
                "primaryDps": digest_before["primaryDps"],
                "fullDps": _json_safe_stat(stats.get("FullDPS")) if stats.get("FullDPS") is not None else None,
                "combinedDps": _json_safe_stat(stats.get("CombinedDPS")) if stats.get("CombinedDPS") is not None else None,
                "totalDps": _json_safe_stat(stats.get("TotalDPS")) if stats.get("TotalDPS") is not None else None,
                "minionCombinedDps": _json_safe_stat(stats.get("MinionCombinedDPS")) if stats.get("MinionCombinedDPS") is not None else None,
                "totalEhp": digest_before["totalEhp"],
                "life": digest_before["life"],
                "physMaxHit": _json_safe_stat(stats.get("PhysicalMaximumHitTaken")) if stats.get("PhysicalMaximumHitTaken") is not None else None,
                "eleMaxHit": orig_ele if orig_ele is not None else 0.0,
                "chaosMaxHit": _json_safe_stat(stats.get("ChaosMaximumHitTaken")) if stats.get("ChaosMaximumHitTaken") is not None else None,
            },
            "dewateredPinnacle": {
                "primaryDpsKey": digest_dewatered["primaryDpsKey"],
                "primaryDps": digest_dewatered["primaryDps"],
                "fullDps": _json_safe_stat(dew_stats.get("FullDPS")) if dew_stats.get("FullDPS") is not None else None,
                "combinedDps": _json_safe_stat(dew_stats.get("CombinedDPS")) if dew_stats.get("CombinedDPS") is not None else None,
                "totalDps": _json_safe_stat(dew_stats.get("TotalDPS")) if dew_stats.get("TotalDPS") is not None else None,
                "minionCombinedDps": _json_safe_stat(dew_stats.get("MinionCombinedDPS")) if dew_stats.get("MinionCombinedDPS") is not None else None,
                "totalEhp": digest_dewatered["totalEhp"],
                "life": digest_dewatered["life"],
                "physMaxHit": _json_safe_stat(dew_stats.get("PhysicalMaximumHitTaken")) if dew_stats.get("PhysicalMaximumHitTaken") is not None else None,
                "eleMaxHit": dew_ele if dew_ele is not None else 0.0,
                "chaosMaxHit": _json_safe_stat(dew_stats.get("ChaosMaximumHitTaken")) if dew_stats.get("ChaosMaximumHitTaken") is not None else None,
            },
            "dewaterDiff": dewater_diff,
        }


def resolve_slot_name(
    worker: BridgeWorker,
    slot_query: str,
    *,
    items_res: dict[str, Any] | None = None,
) -> str:
    """Resolve user slot query (including '#1', '#2' jewel socket shorthand or Flask 1..5) to exact PoB slotName."""
    q = slot_query.strip()
    if items_res is None:
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


_KNOWN_CONFIG_DEFAULTS: dict[str, Any] = {
    "enemyIsBoss": "Pinnacle",
    "resistancePenalty": -60,
    "bandit": "None",
    "pantheonMajorGod": "None",
    "pantheonMinorGod": "None",
    "presetBossSkills": "None",
}

_ALWAYS_SHOW_VISIBLE_CONFIG_VARS = frozenset({
    "enemyIsBoss",
})


def _infer_config_default(item: dict[str, Any]) -> Any:
    var = str(item.get("var") or "")
    if var in _KNOWN_CONFIG_DEFAULTS:
        return _KNOWN_CONFIG_DEFAULTS[var]
    itype = str(item.get("type") or "")
    if itype == "check":
        return False
    if itype == "list":
        opts = item.get("list") or []
        if opts and isinstance(opts[0], dict):
            return opts[0].get("val")
        return "None"
    return None


def inspect_config(
    worker: BridgeWorker,
    show_all: bool = False,
    *,
    include_defaults: bool = False,
    search: str | None = None,
    with_options: bool = False,
) -> dict[str, Any]:
    """Inspect active PoB ConfigTab set, active/non-default/searched config variables, and raw customMods.

    Omits bulky `options` dropdown arrays unless `with_options=True` or matched by `search`.
    """
    effective_all = bool(show_all or include_defaults)
    search_clean = (search or "").strip()
    search_low = search_clean.lower()

    with worker.lock:
        cfg_res = worker.call("list_config", {})

    active_id = cfg_res.get("activeConfigSetId")
    config_sets = cfg_res.get("configSets", [])
    active_set = next((s for s in config_sets if s.get("id") == active_id), None)

    variables: list[dict[str, Any]] = []
    total_count = 0
    active_count = 0

    for sec in cfg_res.get("sections", []):
        sec_name = sec.get("name") or ""
        sec_name_zh = sec.get("nameZh") or sec_name
        for item in sec.get("items", []):
            var = item.get("var")
            if not var:
                continue
            total_count += 1
            val = item.get("value")
            default_val = _infer_config_default(item)
            visible = bool(item.get("visible", True))

            if item.get("type") == "check":
                is_default = (val in (None, False)) and (default_val is False)
            elif item.get("type") == "list":
                is_default = (val is None) or (val == default_val)
            else:
                is_default = val in (None, "") or (val == default_val)

            is_active = visible and (not is_default or var in _ALWAYS_SHOW_VISIBLE_CONFIG_VARS)
            if is_active:
                active_count += 1

            label_clean = strip_pob_escapes(item.get("label") or "")
            label_zh_clean = strip_pob_escapes(item.get("labelZh") or item.get("label") or "")
            raw_list = item.get("list") or []

            matched_search = False
            if search_low:
                hay_parts = [str(var), label_clean, label_zh_clean, sec_name, sec_name_zh]
                for o in raw_list:
                    if isinstance(o, dict):
                        hay_parts.append(str(o.get("val") or ""))
                        hay_parts.append(strip_pob_escapes(o.get("label") or ""))
                        hay_parts.append(strip_pob_escapes(o.get("labelZh") or ""))
                haystack = " ".join(hay_parts).lower()
                matched_search = search_low in haystack
                if not matched_search:
                    continue
            elif not (effective_all or is_active):
                continue

            entry: dict[str, Any] = {
                "section": sec_name,
                "sectionZh": sec_name_zh,
                "var": var,
                "label": label_clean,
                "labelZh": label_zh_clean,
                "type": item.get("type"),
                "value": val,
                "defaultValue": default_val,
                "placeholder": item.get("placeholder"),
                "visible": visible,
                "isDefault": is_default,
            }
            if raw_list and (with_options or matched_search):
                entry["options"] = [
                    {
                        "val": o.get("val"),
                        "label": strip_pob_escapes(o.get("label") or ""),
                        "labelZh": strip_pob_escapes(o.get("labelZh") or o.get("label") or ""),
                    }
                    for o in raw_list
                    if isinstance(o, dict)
                ]
            variables.append(entry)

    custom_mods = cfg_res.get("customMods", [])
    enabled_texts: list[str] = []
    active_custom_lines: list[str] = []
    for blk in custom_mods:
        if blk.get("enabled", True):
            txt = str(blk.get("text") or "")
            if txt.strip():
                enabled_texts.append(txt.strip())
                for line in txt.splitlines():
                    s = line.strip()
                    if s and not s.startswith("#"):
                        active_custom_lines.append(s)

    return {
        "activeConfigSetId": active_id,
        "activeConfigSet": active_set,
        "configSets": config_sets,
        "variables": variables,
        "activeCount": active_count,
        "totalCount": total_count,
        "includeDefaults": effective_all,
        "search": search_clean or None,
        "withOptions": bool(with_options),
        "customMods": custom_mods,
        "customModsText": "\n".join(enabled_texts),
        "activeCustomModLines": active_custom_lines,
        "rev": cfg_res.get("rev"),
    }


def inspect_items(
    worker: BridgeWorker,
    slot_selector: str | None = None,
    *,
    slot_name: str | None = None,
    raw: bool = False,
    include_raw: bool = False,
    all_raw: bool = False,
) -> dict[str, Any]:
    """List equipped items (pipelining all `item_raw` RPCs in ONE batch) or inspect one slot."""
    effective_slot = slot_selector if slot_selector is not None else slot_name
    want_raw = bool(raw or include_raw or all_raw)

    with worker.lock:
        if effective_slot:
            items_res, build_info = _call_batch(
                worker,
                [("list_items", {}), ("get_build_info", {})],
            )
            items_by_id = {it["id"]: it for it in items_res.get("items", [])}
            resolved_slot = resolve_slot_name(worker, effective_slot, items_res=items_res)
            slot_obj = next((s for s in items_res.get("slots", []) if s["name"] == resolved_slot), None)
            item_id = slot_obj.get("selItemId") if slot_obj else None
            if not item_id:
                return {
                    "slot": slot_obj or {"name": resolved_slot},
                    "item": None,
                    "tooltip": None,
                    "raw": None,
                    "buildInfo": build_info,
                }
            if want_raw:
                tip, raw_res = _call_batch(
                    worker,
                    [
                        ("item_tooltip", {"id": item_id, "slotName": resolved_slot, "compare": False}),
                        ("item_raw", {"id": item_id}),
                    ],
                )
                raw_text = raw_res.get("raw") if isinstance(raw_res, dict) else None
            else:
                tip = worker.call("item_tooltip", {"id": item_id, "slotName": resolved_slot, "compare": False})
                raw_text = None
            return {
                "slot": slot_obj,
                "item": items_by_id.get(item_id),
                "tooltip": tip,
                "raw": raw_text,
                "buildInfo": build_info,
            }

        allocated_nodes: set[int] = set()
        if all_raw:
            items_res, tree_state, build_info = _call_batch(
                worker,
                [("list_items", {}), ("get_tree_state", {}), ("get_build_info", {})],
            )
            allocated_nodes = {int(x) for x in tree_state.get("allocatedNodes", [])}
        else:
            items_res, build_info = _call_batch(
                worker,
                [("list_items", {}), ("get_build_info", {})],
            )

        items_by_id = {it["id"]: it for it in items_res.get("items", [])}
        equipped_slots: list[dict[str, Any]] = []
        raw_ids_ordered: list[int] = []
        raw_ids_seen: set[int] = set()

        for s in items_res.get("slots", []):
            item_id = s.get("selItemId")
            node_id = s.get("nodeId")
            is_jewel_socket = node_id is not None
            is_allocated_socket = is_jewel_socket and (int(node_id) in allocated_nodes)

            if all_raw:
                # Include all equipped active slots AND all allocated jewel sockets (#1..#N)
                if s.get("inactive"):
                    continue
                if not item_id and not is_allocated_socket:
                    continue
            else:
                if not item_id and not s.get("shown"):
                    continue

            item = items_by_id.get(item_id) if item_id else None
            entry: dict[str, Any] = {
                "slotName": s["name"],
                "label": s.get("labelZh") or s.get("label") or s["name"],
                "socketIndex": s.get("socketIndex"),
                "nodeId": node_id,
                "isFlask": bool(s.get("isFlask")),
                "active": bool(s.get("active")),
                "inactive": bool(s.get("inactive")),
                "itemId": item_id,
                "item": item,
            }
            if all_raw and is_jewel_socket:
                entry["allocated"] = is_allocated_socket
            if want_raw and item_id:
                iid = int(item_id)
                if iid not in raw_ids_seen:
                    raw_ids_seen.add(iid)
                    raw_ids_ordered.append(iid)
            equipped_slots.append(entry)

        if want_raw and raw_ids_ordered:
            raw_responses = _call_batch(
                worker,
                [("item_raw", {"id": iid}) for iid in raw_ids_ordered],
                raise_on_error=False,
            )
            raw_cache: dict[int, str | None] = {}
            for iid, res_obj in zip(raw_ids_ordered, raw_responses):
                if isinstance(res_obj, dict) and not res_obj.get("_error") and res_obj.get("ok") is not False:
                    raw_cache[iid] = res_obj.get("raw")
                else:
                    raw_cache[iid] = None
            for entry in equipped_slots:
                if entry.get("itemId"):
                    entry["raw"] = raw_cache.get(int(entry["itemId"]))

        return {
            "buildInfo": build_info,
            "activeItemSetId": items_res.get("activeItemSetId"),
            "itemSets": items_res.get("itemSets", []),
            "equippedSlots": equipped_slots,
            "equippedCount": sum(1 for e in equipped_slots if e.get("itemId")),
            "allRaw": all_raw,
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


JEWEL_RADII_316: list[tuple[str, float]] = [
    ("Small", 960.0),
    ("Medium", 1440.0),
    ("Large", 1800.0),
    ("Very Large", 2400.0),
    ("Massive", 2880.0),
]

JEWEL_RADII_315: list[tuple[str, float]] = [
    ("Small", 800.0),
    ("Medium", 1200.0),
    ("Large", 1500.0),
]


def _jewel_radii_for_version(tree_version: str | None) -> list[tuple[str, float]]:
    tv = str(tree_version or "").strip()
    if tv.startswith(("3_15", "3_14", "3_13", "3_12", "3_11", "3_10", "3_9", "3_8")):
        return JEWEL_RADII_315
    return JEWEL_RADII_316


def _normalize_query_list(search: str | None, queries: Iterable[str] | str | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    raw_items: list[str] = []
    if search is not None and str(search).strip():
        raw_items.append(str(search).strip())
    if isinstance(queries, str):
        if queries.strip():
            raw_items.append(queries.strip())
    elif queries is not None:
        for q in queries:
            s = str(q or "").strip()
            if s:
                raw_items.append(s)
    for item in raw_items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _normalize_node_types(node_types: Iterable[str] | str | None) -> set[str]:
    if node_types is None:
        return set()
    items = [node_types] if isinstance(node_types, str) else list(node_types)
    out: set[str] = set()
    for it in items:
        for part in str(it or "").split(","):
            s = part.strip().lower()
            if not s:
                continue
            if s in ("jewel", "jewelsocket", "jewel_socket"):
                s = "socket"
            elif s in ("asc", "ascend", "ascendancy"):
                s = "ascendancy"
            out.add(s)
    return out


def _matches_node_types(meta: dict[str, Any], allowed_types: set[str]) -> bool:
    if not allowed_types:
        return True
    ntype = str(meta.get("type") or "Normal").lower()
    is_asc = bool(meta.get("asc"))
    if "ascendancy" in allowed_types and is_asc:
        return True
    if ntype in allowed_types:
        return True
    if "mastery" in allowed_types and meta.get("overrideWhy") == "mastery":
        return True
    return False


def _resolve_socket_radius_spec(
    socket_radius: str | int,
    radii: list[tuple[str, float]],
    socket_index_by_node: dict[int, int],
    items_res: dict[str, Any],
    tree_state: dict[str, Any],
    nodes_db: dict[str, Any],
) -> tuple[int, float, str]:
    """Resolve `socket_radius` ('#1', '26725', '#1:Large', 'Jewel 26725') to `(socket_node_id, max_radius, label)`."""
    raw = str(socket_radius).strip()
    if not raw:
        raise ValueError("socket_radius cannot be empty.")

    sock_part = raw
    max_radius_label = radii[-1][0]
    max_radius = radii[-1][1]

    if ":" in raw:
        left, right = raw.rsplit(":", 1)
        r_query = right.strip().lower().replace("_", " ").replace("-", " ")
        matched_r = None
        for lbl, val in radii:
            if lbl.lower() == r_query:
                matched_r = (lbl, val)
                break
        if matched_r is not None:
            sock_part = left.strip()
            max_radius_label, max_radius = matched_r
        else:
            try:
                num_r = float(right.strip())
                if num_r > 0:
                    sock_part = left.strip()
                    max_radius = num_r
                    max_radius_label = f"Custom({num_r:g})"
            except ValueError:
                pass

    # 1. '#N' jewel socket index
    m_hash = re.match(r"^#(\d+)$", sock_part)
    if m_hash:
        target_idx = int(m_hash.group(1))
        for nid, s_idx in socket_index_by_node.items():
            if s_idx == target_idx:
                return nid, max_radius, max_radius_label
        raise ValueError(f"Jewel socket #{target_idx} not found in build slots.")

    # 2. Numeric: exact nodeId in nodes_db, else socketIndex
    if sock_part.isdigit():
        num = int(sock_part)
        if str(num) in nodes_db:
            return num, max_radius, max_radius_label
        for nid, s_idx in socket_index_by_node.items():
            if s_idx == num:
                return nid, max_radius, max_radius_label
        raise ValueError(f"Jewel socket '{sock_part}' not found by node ID or socket index.")

    # 3. Match against slot name/label or socket node name
    q_low = sock_part.lower()
    for slot in items_res.get("slots", []):
        if slot.get("nodeId") is not None:
            cand_text = " ".join(
                str(slot.get(k) or "") for k in ("name", "label", "labelZh")
            ).lower()
            if q_low == str(slot.get("name") or "").lower() or q_low in cand_text:
                return int(slot["nodeId"]), max_radius, max_radius_label

    for sock in tree_state.get("sockets", []):
        nid = int(sock["nodeId"])
        meta = nodes_db.get(str(nid)) or {}
        if q_low in str(meta.get("name") or "").lower() or q_low in str(meta.get("nameZh") or "").lower():
            return nid, max_radius, max_radius_label

    raise ValueError(f"Jewel socket '{socket_radius}' not found.")


def inspect_skills(
    worker: BridgeWorker,
    *,
    group_selector: str | None = None,
) -> dict[str, Any]:
    """List all skill groups or inspect a specific group with pipelined per-gem DPS tooltips."""
    with worker.lock:
        skills_res = worker.call("list_skills", {})
        if group_selector is not None:
            idx = resolve_skill_group_index(skills_res, group_selector)
            group = next(g for g in skills_res.get("groups", []) if g["index"] == idx)
            gems = group.get("gems", [])
            tip_calls = [("gem_tooltip", {"group": idx, "index": gem["index"]}) for gem in gems]
            tips = _call_batch(worker, tip_calls, raise_on_error=False) if tip_calls else []
            gems_with_tips = []
            for gem, tip in zip(gems, tips):
                dps_val = (
                    tip.get("dps")
                    if isinstance(tip, dict) and not tip.get("_error") and tip.get("ok") is not False
                    else None
                )
                gems_with_tips.append({**gem, "tooltipDps": dps_val})
            return {
                "group": {**group, "gems": gems_with_tips},
                "mainSocketGroup": skills_res.get("mainSocketGroup"),
            }
        return skills_res


def inspect_tree(
    worker: BridgeWorker,
    search: str | None = None,
    *,
    queries: Iterable[str] | str | None = None,
    node_types: Iterable[str] | str | None = None,
    allocated: bool | None = None,
    max_cost: int | None = None,
    show_path: bool = False,
    masteries: bool = False,
    socket_radius: str | int | None = None,
) -> dict[str, Any]:
    """Summarize allocated passive tree nodes or search/filter nodes with pipelined `node_hover` RPCs."""
    query_list = _normalize_query_list(search, queries)
    allowed_types = _normalize_node_types(node_types)
    if masteries and not query_list and not allowed_types and socket_radius is None:
        allowed_types = {"mastery"}

    is_filtered_mode = bool(
        query_list
        or allowed_types
        or allocated is not None
        or max_cost is not None
        or masteries
        or socket_radius is not None
    )

    with worker.lock:
        # Pipeline get_tree_state + list_items in ONE batch (dropping unused list_specs!)
        tree_state, items_res = _call_batch(
            worker,
            [("get_tree_state", {}), ("list_items", {})],
        )
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
            # Preserve full untruncated masteryEffects for Mastery nodes
            raw_base = nodes_db.get(sid) or {}
            if raw_base.get("masteryEffects") and not base.get("masteryEffects"):
                base["masteryEffects"] = raw_base["masteryEffects"]
            return base

        if is_filtered_mode:
            type_rank = {"Keystone": 0, "Notable": 1, "Mastery": 2, "Socket": 3, "Normal": 4}
            socket_radius_info: dict[str, Any] | None = None
            matches: list[dict[str, Any]] = []
            force_path_ids: set[int] = set()

            if socket_radius is not None:
                radii = _jewel_radii_for_version(tree_version)
                sock_nid, max_r, max_r_label = _resolve_socket_radius_spec(
                    socket_radius, radii, socket_index_by_node, items_res, tree_state, nodes_db
                )
                sock_node = _get_node_meta(sock_nid)
                sx, sy = sock_node.get("x"), sock_node.get("y")
                if not isinstance(sx, (int, float)) or not isinstance(sy, (int, float)):
                    raise ValueError(f"Jewel socket node {sock_nid} has no tree coordinates.")

                counts_by_radius: dict[str, int] = {lbl: 0 for lbl, _ in radii}
                all_node_ids = set(nodes_db.keys()) | set(dynamic_map.keys())
                for sid in all_node_ids:
                    nid = int(sid)
                    if nid == sock_nid:
                        continue
                    meta = _get_node_meta(nid)
                    if meta.get("asc") or meta.get("proxy") or meta.get("expansion"):
                        continue
                    if meta.get("type") in ("ClassStart", "AscendClassStart"):
                        continue
                    nx, ny = meta.get("x"), meta.get("y")
                    if not isinstance(nx, (int, float)) or not isinstance(ny, (int, float)):
                        continue
                    dist = math.hypot(float(nx) - float(sx), float(ny) - float(sy))
                    if dist > max_r:
                        continue
                    tier_label = max_r_label
                    for lbl, r_outer in radii:
                        if dist <= r_outer:
                            tier_label = lbl
                            break
                    for lbl, r_outer in radii:
                        if dist <= r_outer:
                            counts_by_radius[lbl] += 1
                    meta["distance"] = round(dist, 1)
                    meta["radiusTier"] = tier_label
                    matches.append(meta)

                socket_radius_info = {
                    "id": sock_nid,
                    "nodeId": sock_nid,
                    "socketNodeId": sock_nid,
                    "name": sock_node.get("name"),
                    "nameZh": sock_node.get("nameZh"),
                    "query": str(socket_radius),
                    "socketIndex": socket_index_by_node.get(sock_nid),
                    "x": sx,
                    "y": sy,
                    "maxRadius": max_r,
                    "maxRadiusLabel": max_r_label,
                    "radiusThresholds": {lbl: r for lbl, r in radii},
                    "countsByRadius": counts_by_radius,
                    "radii": {lbl: [] for lbl, _ in radii},
                }
            else:
                all_node_ids = set(nodes_db.keys()) | set(dynamic_map.keys())
                matches = [_get_node_meta(int(sid)) for sid in all_node_ids]

            # Apply multi-query matching (OR semantics across queries)
            if query_list:
                socket_queries: dict[int, str] = {}
                text_queries: list[tuple[str, str]] = []
                for q in query_list:
                    m_sock = re.match(r"^#(\d+)$", q)
                    if m_sock:
                        socket_queries[int(m_sock.group(1))] = q
                    else:
                        text_queries.append((q, q.lower()))

                filtered_by_query: list[dict[str, Any]] = []
                for meta in matches:
                    hit_queries: list[str] = []
                    s_idx = meta.get("socketIndex")
                    if s_idx is not None and int(s_idx) in socket_queries:
                        hit_queries.append(socket_queries[int(s_idx)])
                        force_path_ids.add(int(meta["id"]))

                    if text_queries:
                        hay_parts = [
                            str(meta.get("id") or ""),
                            str(meta.get("name") or ""),
                            str(meta.get("nameZh") or ""),
                            " ".join(str(s) for s in (meta.get("stats") or [])),
                            " ".join(str(s) for s in (meta.get("statsZh") or [])),
                        ]
                        for eff in meta.get("masteryEffects") or []:
                            if isinstance(eff, dict):
                                hay_parts.append(str(eff.get("effect") or ""))
                                hay_parts.append(" ".join(str(s) for s in (eff.get("stats") or [])))
                                hay_parts.append(" ".join(str(s) for s in (eff.get("statsZh") or [])))
                        haystack = " ".join(hay_parts).lower()
                        for orig_q, low_q in text_queries:
                            if low_q in haystack:
                                hit_queries.append(orig_q)

                    if hit_queries:
                        if len(query_list) > 1:
                            meta["matchedQueries"] = hit_queries
                        filtered_by_query.append(meta)
                matches = filtered_by_query

            # Apply node_types and allocated filters
            if allowed_types:
                matches = [m for m in matches if _matches_node_types(m, allowed_types)]
            if allocated is not None:
                want_alloc = bool(allocated)
                matches = [m for m in matches if bool(m.get("allocated")) is want_alloc]

            # Sort: socket_radius sorts by distance first; max_cost pre-sorts by tree proximity
            # (BFS hop distance over adjacency, falling back to Euclidean distance) BEFORE hover slicing.
            bfs_dist: dict[int, int] = {}
            if socket_radius is not None:
                matches.sort(
                    key=lambda m: (
                        float(m.get("distance", 1e9)),
                        type_rank.get(m.get("type", "Normal"), 5),
                        m.get("id", 0),
                    )
                )
            elif max_cost is not None:
                adj: dict[int, set[int]] = {}
                group_notables: dict[Any, list[int]] = {}
                mastery_groups: list[tuple[int, Any]] = []
                for src_map in (nodes_db, dynamic_map):
                    for sid, raw_node in src_map.items():
                        if not isinstance(raw_node, dict):
                            continue
                        try:
                            uid = int(raw_node.get("id", sid))
                        except (TypeError, ValueError):
                            continue
                        for edge_key in ("linked", "links", "out", "in"):
                            edges = raw_node.get(edge_key)
                            if not isinstance(edges, (list, tuple)):
                                continue
                            for raw_nb in edges:
                                try:
                                    vid = int(raw_nb)
                                except (TypeError, ValueError):
                                    continue
                                if vid == uid:
                                    continue
                                adj.setdefault(uid, set()).add(vid)
                                adj.setdefault(vid, set()).add(uid)
                        grp_id = raw_node.get("group")
                        if grp_id is not None:
                            ntype = raw_node.get("type")
                            if ntype == "Notable":
                                group_notables.setdefault(grp_id, []).append(uid)
                            elif ntype == "Mastery":
                                mastery_groups.append((uid, grp_id))
                for mid, grp_id in mastery_groups:
                    if not adj.get(mid):
                        for nid in group_notables.get(grp_id, ()):
                            adj.setdefault(mid, set()).add(nid)
                            adj.setdefault(nid, set()).add(mid)

                if allocated_set and adj:
                    bfs_dist = {nid: 0 for nid in allocated_set}
                    queue = deque(allocated_set)
                    while queue:
                        cur = queue.popleft()
                        next_d = bfs_dist[cur] + 1
                        for nb in adj.get(cur, ()):
                            if nb not in bfs_dist:
                                bfs_dist[nb] = next_d
                                queue.append(nb)

                alloc_coords: list[tuple[float, float]] = []
                for aid in allocated_set:
                    ameta = dynamic_map.get(str(aid)) or nodes_db.get(str(aid)) or {}
                    ax, ay = ameta.get("x"), ameta.get("y")
                    if isinstance(ax, (int, float)) and isinstance(ay, (int, float)):
                        alloc_coords.append((float(ax), float(ay)))

                def _min_euclid_sq(meta: dict[str, Any]) -> float:
                    nx, ny = meta.get("x"), meta.get("y")
                    if not alloc_coords or not isinstance(nx, (int, float)) or not isinstance(ny, (int, float)):
                        return float("inf")
                    fx, fy = float(nx), float(ny)
                    return min((fx - ax) * (fx - ax) + (fy - ay) * (fy - ay) for ax, ay in alloc_coords)

                def _proximity_sort_key(m: dict[str, Any]) -> tuple[int, int, float, int, int]:
                    nid = int(m["id"])
                    trank = type_rank.get(m.get("type", "Normal"), 5)
                    if m.get("allocated"):
                        return (0, 0, 0.0, trank, nid)
                    hop = bfs_dist.get(nid)
                    if hop is not None:
                        return (1, hop, 0.0, trank, nid)
                    return (2, 999999, _min_euclid_sq(m), trank, nid)

                matches.sort(key=_proximity_sort_key)
            else:
                matches.sort(
                    key=lambda m: (
                        0 if m.get("allocated") else 1,
                        type_rank.get(m.get("type", "Normal"), 5),
                        m.get("id", 0),
                    )
                )

            # Pipeline node_hover RPCs in ONE batch!
            if max_cost is not None:
                cost_cap = int(max_cost)
                within_hop = sum(
                    1
                    for m in matches
                    if m.get("allocated") or bfs_dist.get(int(m["id"]), 999999) <= cost_cap
                )
                hover_limit = min(max(60, within_hop), 250)
            else:
                hover_limit = 50 if (masteries or socket_radius is not None) else 25
            hover_targets = matches[:hover_limit]
            if hover_targets:
                hover_calls = [("node_hover", {"id": m["id"]}) for m in hover_targets]
                hover_results = _call_batch(worker, hover_calls, raise_on_error=False)
                for m, hover in zip(hover_targets, hover_results):
                    if isinstance(hover, dict) and not hover.get("_error") and hover.get("ok") is not False:
                        m["cost"] = hover.get("cost")
                        if show_path or int(m["id"]) in force_path_ids:
                            m["path"] = hover.get("path")
                            if m.get("allocated") and hover.get("depends") is not None:
                                m["depends"] = hover.get("depends")

            if max_cost is not None:
                cost_cap = int(max_cost)
                matches = [
                    m
                    for m in matches
                    if m.get("cost") is not None and int(m["cost"]) <= cost_cap
                ]
                if socket_radius is None:
                    matches.sort(
                        key=lambda m: (
                            0 if m.get("allocated") else 1,
                            int(m["cost"]),
                            type_rank.get(m.get("type", "Normal"), 5),
                            m.get("id", 0),
                        )
                    )

            result_limit = 100 if (masteries or socket_radius is not None) else 50
            out: dict[str, Any] = {
                "search": search if search is not None else (", ".join(query_list) if query_list else None),
                "queries": query_list,
                "masteries": bool(masteries),
                "showPath": bool(show_path),
                "totalMatches": len(matches),
                "matches": matches[:result_limit],
            }
            if socket_radius_info is not None:
                radii_groups: dict[str, list[dict[str, Any]]] = {
                    lbl: [] for lbl in socket_radius_info["radii"].keys()
                }
                for m in matches[:result_limit]:
                    tier = m.get("radiusTier")
                    if tier in radii_groups:
                        radii_groups[tier].append(m)
                    elif tier:
                        radii_groups.setdefault(str(tier), []).append(m)
                socket_radius_info["radii"] = radii_groups
                out["socketRadius"] = socket_radius_info
            return out

        keystones: list[dict[str, Any]] = []
        notables: list[dict[str, Any]] = []
        masteries_list: list[dict[str, Any]] = []
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
                masteries_list.append(meta)

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
            "allocatedCount": len(allocated_ids),
            "keystones": keystones,
            "ascendancyNodes": ascendancy_nodes,
            "masteries": masteries_list,
            "notables": notables,
            "jewelSockets": jewel_sockets,
        }


_CALC_CELL_NUM_RE = re.compile(
    r"^\s*[xX×]?\s*([+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|[+-]?\d+(?:\.\d+)?)\s*(?:%|/s|s|sec|ms|x|X|×|m|pts?|秒|次/秒|米)?\s*$"
)


def _parse_calc_cell_value(raw_text: str, disp_text: str) -> float | None:
    """Parse a clean numeric value from English `raw` (preferred) or localized `text`, returning finite float or None."""
    for candidate in (raw_text, disp_text):
        s = (candidate or "").strip()
        if not s:
            continue
        s_low = s.lower()
        if s_low in ("inf", "+inf", "infinity", "-inf", "-infinity", "nan"):
            return None
        m = _CALC_CELL_NUM_RE.match(s)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if math.isfinite(val):
                    return val
            except ValueError:
                continue
    return None


_CAMEL_SPLIT_RE = re.compile(r"([a-z0-9])([A-Z])")
_CALC_PUNCT_RE = re.compile(r"[\s_\-\"'`.,:/\\()\[\]{}%+]+")

_CALC_TOKEN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "regen": ("regeneration", "recovery", "回复"),
    "regeneration": ("regen", "recovery", "回复"),
    "recovery": ("regen", "regeneration", "回复"),
    "es": ("energy shield", "能量护盾"),
    "ehp": ("effective hit pool", "effective health pool", "有效生命"),
    "effective": ("eff", "有效"),
    "eff": ("effective", "有效"),
    "dps": ("damage", "totaldps", "秒伤", "伤害"),
    "dmg": ("damage", "伤害"),
    "avg": ("average", "平均"),
    "average": ("avg", "平均"),
    "crit": ("critical", "crits", "暴击"),
    "critical": ("crit", "crits", "暴击"),
    "mult": ("multiplier", "multi", "加成", "倍率"),
    "multi": ("multiplier", "mult", "加成", "倍率"),
    "multiplier": ("multi", "mult", "加成", "倍率"),
    "res": ("resist", "resistance", "抗性"),
    "resist": ("res", "resistance", "抗性"),
    "resistance": ("resist", "res", "抗性"),
    "phys": ("physical", "物理"),
    "ele": ("elemental", "元素"),
    "pen": ("penetration", "穿透"),
    "supp": ("suppression", "压制"),
    "suppression": ("supp", "suppress", "压制"),
    "inc": ("increased", "提高"),
    "max": ("maximum", "最大"),
    "min": ("minimum", "最小"),
    "mana": ("魔力",),
    "life": ("生命",),
    "hit": ("击中",),
    "dot": ("持续伤", "damage over time"),
    "speed": ("rate", "速度"),
    "armour": ("armor", "护甲"),
    "armor": ("armour", "护甲"),
    "evasion": ("evade", "闪避"),
    "block": ("格挡",),
    "leech": ("偷取",),
    "cost": ("消耗",),
}


def _norm_calc_text(text: str | None) -> str:
    """Normalize calc label/query: strip PoB escapes, split camelCase, lowercase, collapse punctuation."""
    if not text:
        return ""
    cleaned = strip_pob_escapes(str(text))
    cleaned = _CAMEL_SPLIT_RE.sub(r"\1 \2", cleaned)
    cleaned = _CALC_PUNCT_RE.sub(" ", cleaned).strip().lower()
    return cleaned


def _compact_calc_text(text: str | None) -> str:
    """Strip spaces/underscores/punctuation and lowercase for compact key matching ('Average Hit' == 'averagehit')."""
    return _norm_calc_text(text).replace(" ", "")


def _token_matches_haystack(token: str, hay_norm: str, hay_compact: str) -> bool:
    if token in hay_norm or token in hay_compact:
        return True
    for syn in _CALC_TOKEN_SYNONYMS.get(token, ()):
        syn_norm = _norm_calc_text(syn)
        syn_compact = syn_norm.replace(" ", "")
        if syn_norm in hay_norm or (syn_compact and syn_compact in hay_compact):
            return True
    return False


def _score_calc_row(
    q_norm: str,
    q_compact: str,
    q_tokens: list[str],
    sec_norm: str,
    sub_norm: str,
    row_norm: str,
    cells_norm: str,
    sub_row_compact: str,
) -> float:
    """Score how well a calcs row matches the bilingual query (0.0 means no match)."""
    if not q_compact:
        return 1.0

    row_compact = row_norm.replace(" ", "")
    sub_compact = sub_norm.replace(" ", "")
    full_norm = f"{sec_norm} {sub_norm} {row_norm} {cells_norm}".strip()
    full_compact = full_norm.replace(" ", "")

    # 1. Direct normalized or compact match in row label (highest priority)
    if q_norm in row_norm or q_compact in row_compact:
        return 100.0 + (20.0 if row_compact == q_compact else 0.0)

    # 2. Combined subsection + row match (e.g. '魔力回复' matching sub '魔力' + row '回复')
    if q_compact in sub_row_compact:
        return 90.0

    # 3. Direct match in subsection or section or cell values (e.g. '有效生命' or skill/minion names like '寒霜法师')
    if q_norm in sub_norm or q_compact in sub_compact:
        return 75.0
    if q_norm in cells_norm or q_compact in cells_norm.replace(" ", ""):
        return 70.0
    if q_norm in sec_norm or q_compact in sec_norm.replace(" ", ""):
        return 60.0

    # 4. Multi-token + bilingual synonym matching (e.g. 'Mana Regeneration' -> sub 'Mana' + row 'Recovery')
    if len(q_tokens) >= 2:
        if all(_token_matches_haystack(tok, full_norm, full_compact) for tok in q_tokens):
            row_hits = sum(1 for tok in q_tokens if _token_matches_haystack(tok, row_norm, row_compact))
            sub_hits = sum(1 for tok in q_tokens if _token_matches_haystack(tok, sub_norm, sub_compact))
            if row_hits > 0:
                return 80.0 + row_hits * 5.0 + sub_hits * 3.0
            return 55.0 + sub_hits * 3.0
    elif len(q_tokens) == 1:
        tok = q_tokens[0]
        if _token_matches_haystack(tok, row_norm, row_compact):
            return 65.0
        if _token_matches_haystack(tok, sub_norm, sub_compact):
            return 50.0
        if _token_matches_haystack(tok, full_norm, full_compact):
            return 40.0

    return 0.0


def search_calcs_breakdown(
    worker: BridgeWorker,
    keyword: str | None = None,
    *,
    keywords: Iterable[str] | str | None = None,
    limit: int = 20,
    max_breakdowns: int = 8,
    no_breakdown: bool = False,
) -> dict[str, Any]:
    """Bilingual (EN + ZH) search across `get_calcs` rows with variadic keywords and pipelined `calcs_breakdown`."""
    kw_list = _normalize_query_list(keyword, keywords)
    if not kw_list:
        kw_list = [""]

    with worker.lock:
        # Ensure we have both English and Chinese labels regardless of current set_translate state.
        # Avoid redundant version{} RPC by checking tracked `worker.translate_enabled` / `hello_info` first.
        orig_translate: bool | None = None
        toggled_translate = False
        if worker.caps.get("translateToggle", False):
            tracked = getattr(worker, "translate_enabled", None)
            if tracked is None:
                info_dict = getattr(worker, "hello_info", None) or getattr(worker, "info", None) or {}
                if isinstance(info_dict, dict) and "translate" in info_dict:
                    tracked = info_dict.get("translate")
            if tracked is None:
                try:
                    ver = worker.call("version", {})
                    tracked = ver.get("translate")
                except Exception:
                    tracked = None
            if tracked is False:
                orig_translate = False
                try:
                    worker.call("set_translate", {"enabled": True})
                    toggled_translate = True
                except Exception:
                    toggled_translate = False

        try:
            calcs = worker.call("get_calcs", {})

            # Pre-extract normalized rows once so multi-keyword queries share ONE get_calcs scan
            indexed_rows: list[dict[str, Any]] = []
            seq = 0
            for sec in calcs.get("sections", []):
                si = sec.get("si")
                sec_id = str(sec.get("id") or "")
                sec_group = str(sec.get("group") or "")
                sec_norm = _norm_calc_text(f"{sec_id} {sec_group}")
                for sub in sec.get("subsections", []):
                    ui = sub.get("ui")
                    sub_en = str(sub.get("label") or "")
                    sub_zh = str(sub.get("labelZh") or "")
                    sub_extra = str(sub.get("extra") or "")
                    sub_norm = _norm_calc_text(f"{sub_en} {sub_zh} {sub_extra}")
                    for row in sub.get("rows", []):
                        ri = row.get("ri")
                        r_en = str(row.get("label") or "")
                        r_zh = str(row.get("labelZh") or "")
                        row_norm = _norm_calc_text(f"{r_en} {r_zh}")

                        cell_parts: list[str] = []
                        for cell in row.get("cells", []):
                            if cell.get("text"):
                                cell_parts.append(str(cell["text"]))
                            if cell.get("raw") and cell.get("raw") != cell.get("text"):
                                cell_parts.append(str(cell["raw"]))
                            widget = cell.get("widget")
                            if isinstance(widget, dict):
                                if widget.get("value"):
                                    cell_parts.append(str(widget["value"]))
                                sel_idx = widget.get("selIndex")
                                w_list = widget.get("list")
                                if isinstance(w_list, list) and isinstance(sel_idx, int) and 1 <= sel_idx <= len(w_list):
                                    sel_entry = w_list[sel_idx - 1]
                                    if isinstance(sel_entry, dict):
                                        cell_parts.append(str(sel_entry.get("label") or ""))
                                        cell_parts.append(str(sel_entry.get("labelZh") or ""))
                                    else:
                                        cell_parts.append(str(sel_entry))
                        cells_norm = _norm_calc_text(" ".join(cell_parts))
                        sub_row_compact = (
                            _compact_calc_text(f"{sub_en} {r_en}")
                            + " "
                            + _compact_calc_text(f"{sub_zh} {r_zh}")
                        )
                        indexed_rows.append(
                            {
                                "seq": seq,
                                "si": si,
                                "ui": ui,
                                "ri": ri,
                                "sec_id": sec_id,
                                "sec_group": sec_group,
                                "sec_norm": sec_norm,
                                "sub_en": sub_en,
                                "sub_zh": sub_zh or sub_en,
                                "sub_norm": sub_norm,
                                "row_en": r_en,
                                "row_zh": r_zh,
                                "row_norm": row_norm,
                                "cells_norm": cells_norm,
                                "sub_row_compact": sub_row_compact,
                                "cells": row.get("cells", []),
                            }
                        )
                        seq += 1

            # Collect unique breakdown coordinates across all keywords and execute ONE batch
            per_kw_scored: list[tuple[str, list[tuple[float, int, dict[str, Any]]]]] = []
            bd_coord_order: list[tuple[Any, Any, Any, Any]] = []
            bd_coord_seen: set[tuple[Any, Any, Any, Any]] = set()

            for kw in kw_list:
                q_norm = _norm_calc_text(kw)
                q_compact = q_norm.replace(" ", "")
                q_tokens = [t for t in q_norm.split(" ") if t]
                scored: list[tuple[float, int, dict[str, Any]]] = []
                for item in indexed_rows:
                    score = _score_calc_row(
                        q_norm,
                        q_compact,
                        q_tokens,
                        item["sec_norm"],
                        item["sub_norm"],
                        item["row_norm"],
                        item["cells_norm"],
                        item["sub_row_compact"],
                    )
                    if score > 0:
                        scored.append((score, item["seq"], item))
                scored.sort(key=lambda pair: (-pair[0], pair[1]))
                per_kw_scored.append((kw, scored))

                if not no_breakdown and max_breakdowns > 0:
                    kw_bd_count = 0
                    for _, _, item in scored[:limit]:
                        for cell in item["cells"]:
                            if cell.get("hasBreakdown") and kw_bd_count < max_breakdowns:
                                coord = (item["si"], item["ui"], item["ri"], cell.get("ci"))
                                if coord not in bd_coord_seen:
                                    bd_coord_seen.add(coord)
                                    bd_coord_order.append(coord)
                                kw_bd_count += 1

            bd_map: dict[tuple[Any, Any, Any, Any], list[Any]] = {}
            if not no_breakdown and bd_coord_order:
                bd_calls = [
                    ("calcs_breakdown", {"si": si, "ui": ui, "ri": ri, "ci": ci})
                    for (si, ui, ri, ci) in bd_coord_order
                ]
                bd_responses = _call_batch(worker, bd_calls, raise_on_error=False)
                for coord, bd in zip(bd_coord_order, bd_responses):
                    if isinstance(bd, dict) and not bd.get("_error") and bd.get("ok") is not False and "sections" in bd:
                        bd_map[coord] = bd.get("sections", [])

            def _build_matches(scored: list[tuple[float, int, dict[str, Any]]]) -> list[dict[str, Any]]:
                out_matches: list[dict[str, Any]] = []
                kw_bd_count = 0
                for score, _, item in scored[:limit]:
                    cells_out: list[dict[str, Any]] = []
                    for cell in item["cells"]:
                        ci = cell.get("ci")
                        raw_clean = strip_pob_escapes(cell.get("raw") or cell.get("text") or "")
                        text_clean = strip_pob_escapes(cell.get("text") or cell.get("raw") or "")
                        c_entry: dict[str, Any] = {
                            "ci": ci,
                            "text": text_clean,
                            "raw": raw_clean,
                            "value": _parse_calc_cell_value(raw_clean, text_clean),
                            "hasBreakdown": bool(cell.get("hasBreakdown")),
                        }
                        if not no_breakdown and cell.get("hasBreakdown") and kw_bd_count < max_breakdowns:
                            coord = (item["si"], item["ui"], item["ri"], ci)
                            if coord in bd_map:
                                c_entry["breakdown"] = bd_map[coord]
                            kw_bd_count += 1
                        cells_out.append(c_entry)

                    out_matches.append(
                        {
                            "sectionId": item["sec_id"],
                            "sectionGroup": item["sec_group"],
                            "subsection": item["sub_zh"],
                            "subsectionEn": item["sub_en"],
                            "rowLabel": item["row_en"],
                            "rowLabelZh": item["row_zh"],
                            "score": score,
                            "coords": {"si": item["si"], "ui": item["ui"], "ri": item["ri"]},
                            "cells": cells_out,
                        }
                    )
                return out_matches

            if len(per_kw_scored) == 1:
                kw, scored = per_kw_scored[0]
                return {
                    "keyword": kw,
                    "totalMatches": len(scored),
                    "matches": _build_matches(scored),
                }

            per_kw_results: list[dict[str, Any]] = []
            combined_matches: list[dict[str, Any]] = []
            seen_coords: set[tuple[Any, Any, Any]] = set()
            for kw, scored in per_kw_scored:
                kw_matches = _build_matches(scored)
                per_kw_results.append(
                    {
                        "keyword": kw,
                        "totalMatches": len(scored),
                        "matches": kw_matches,
                    }
                )
                for m in kw_matches:
                    c = m["coords"]
                    key = (c["si"], c["ui"], c["ri"])
                    if key not in seen_coords:
                        seen_coords.add(key)
                        combined_matches.append({**m, "matchedKeyword": kw})

            return {
                "keyword": kw_list[0],
                "keywords": kw_list,
                "results": per_kw_results,
                "totalMatches": sum(r["totalMatches"] for r in per_kw_results),
                "matches": combined_matches,
            }
        finally:
            if toggled_translate and orig_translate is not None:
                try:
                    worker.call("set_translate", {"enabled": orig_translate})
                except Exception:
                    pass


inspect_calcs = search_calcs_breakdown
