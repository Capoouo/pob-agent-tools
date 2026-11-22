"""Build inspection, offence/defence summary, 4-part inflation audit, and calcs breakdown."""

from __future__ import annotations

from collections import deque
import difflib
import math
import re
from typing import Any, Iterable

from .bridge_client import BridgeWorker
from .formatters import _parse_numeric_or_none, strip_pob_escapes
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
    "熔岩护盾",
    "钢铁之肤",
    "不朽怒嚎",
    "奥术斗篷",
    "狂战",
    "冰霜护盾",
    "威能法印",
    "瓦尔裂隙",
)

# PoB NewConfigSet truthy defaultState values (Modules/ConfigOptions.lua) for inflation-matching
# vars. bridge.lua's list_config does not serialize defaultState/isDefault, and reset_config
# restores these to their defaultState (a no-op when already at defaultState).
_POB_TRUTHY_CONFIG_DEFAULTS: dict[str, Any] = {
    "conditionCorruptingCryStages": 1,
    "conditionChampionIntimidate": True,
    "conditionSacrificeMinion": True,
}


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


def _is_tincture_item(item: dict[str, Any] | None) -> bool:
    """Return True if a PoB item entry represents a PoE1 Tincture (NOT a Flask)."""
    if not isinstance(item, dict):
        return False
    if str(item.get("type") or "").strip().lower() == "tincture":
        return True
    text = " ".join(
        str(item.get(k) or "")
        for k in ("name", "nameZh", "title", "titleZh", "baseName", "baseNameZh")
    ).lower()
    return "tincture" in text or "酊剂" in text or "sap of the seasons" in text or "四季之侵" in text


def _has_bloodsoaked_blade_keystone(
    worker: BridgeWorker,
    tree_summary: dict[str, Any] | None = None,
) -> bool:
    """Detect whether the Bloodsoaked Blade (`喋血之刃`) keystone is allocated on the passive tree."""
    if isinstance(tree_summary, dict):
        for ks in tree_summary.get("keystones") or []:
            if isinstance(ks, dict):
                nm = strip_pob_escapes(str(ks.get("name") or "")).lower()
                nm_zh = strip_pob_escapes(str(ks.get("nameZh") or "")).lower()
                if "bloodsoaked blade" in nm or "喋血之刃" in nm_zh or int(ks.get("id") or 0) == 13019:
                    return True
        return False
    try:
        tree_state = worker.call("get_tree_state", {})
        alloc_ids = {int(x) for x in (tree_state.get("allocatedNodes") or [])}
        if not alloc_ids:
            return False
        if 13019 in alloc_ids:
            return True
        nodes_db = worker.get_cached_tree_data(tree_state.get("treeVersion")).get("nodes", {})
        for nid in alloc_ids:
            nd = nodes_db.get(str(nid)) or {}
            nm = str(nd.get("name") or "").lower()
            if "bloodsoaked blade" in nm:
                return True
    except Exception:
        return False
    return False


POE1_SERVER_TICK_SEC = 0.033
_GLOBAL_CDR_LINE_RE = re.compile(
    r"^(?:\{[^}]+\})*\s*\+?(\d+(?:\.\d+)?)%\s+increased\s+cooldown\s+recovery\s+rate\s*$",
    re.IGNORECASE,
)
_ITEM_SOCKETS_RE = re.compile(r"^Sockets:\s*([RGBWADrgbwad\-\s]+)$", re.MULTILINE)
_ITEM_SEL_VARIANT_RE = re.compile(r"^Selected Variant:\s*(\d+)\s*$", re.MULTILINE)
_MOD_VARIANT_RE = re.compile(r"\{variant:([0-9,]+)\}")
_ELE_CURSE_OR_EXPOSURE_NAMES = frozenset(
    {
        "conductivity",
        "flammability",
        "frostbite",
        "elemental weakness",
        "wave of conviction",
        "hydrosphere",
        "frost bomb",
    }
)

# Exact PoE1 3.30 Cast while Channelling (sup_int.lua) fixed trigger intervals (ms) by gem level
_CWC_INTERVAL_MS: dict[int, int] = {
    1: 400, 2: 390, 3: 390, 4: 380, 5: 380, 6: 380, 7: 370, 8: 370, 9: 360, 10: 360,
    11: 360, 12: 350, 13: 350, 14: 340, 15: 340, 16: 340, 17: 330, 18: 330, 19: 320, 20: 320,
    21: 320, 22: 310, 23: 310, 24: 300, 25: 300, 26: 300, 27: 290, 28: 290, 29: 280, 30: 280,
}
_AWAKENED_CWC_INTERVAL_MS: dict[int, int] = {
    1: 340, 2: 330, 3: 320, 4: 310, 5: 300, 6: 300, 7: 290, 8: 290, 9: 280, 10: 280,
}

# Supports whose value is non-DPS utility, multi-hit overlap, or handled by dedicated checks
# (exempt from generic 0%-delta "dead support" alarm so we NEVER false-alarm).
_DEAD_SUPPORT_EXEMPT_NAMES = frozenset(
    {
        "enlighten",
        "empower",
        "enhance",
        "lifetap",
        "item rarity",
        "item quantity",
        "culling strike",
        "life leech",
        "mana leech",
        "energy shield leech",
        "life gain on hit",
        "fortify",
        "blind",
        "maim",
        "power charge on critical",
        "endurance charge on melee stun",
        "rage",
        "arcane surge",
        "greater multiple projectiles",
        "awakened greater multiple projectiles",
        "lesser multiple projectiles",
        "volley",
        "greater volley",
        "returning projectiles",
        "spell cascade",
        "greater spell cascade",
        "awakened spell cascade",
        "multistrike",
        "greater multistrike",
        "awakened multistrike",
        "spell echo",
        "greater spell echo",
        "awakened spell echo",
        "unleash",
        "chain",
        "greater chain",
        "awakened chain",
        "fork",
        "greater fork",
        "awakened fork",
        "pierce",
        "greater pierce",
        "awakened pierce",
        "ancestral call",
        "greater ancestral call",
        "awakened ancestral call",
        "melee splash",
        "awakened melee splash",
        "increased area of effect",
        "increased duration",
        "less duration",
        "second wind",
        "cast on critical strike",
        "awakened cast on critical strike",
        "cast while channelling",
        "awakened cast while channelling",
        "cast when damage taken",
        "cast when stunned",
        "cast on death",
        "cast on melee kill",
        "cast on ward break",
        "spellslinger",
        "arcanist brand",
        "spell totem",
        "ballista totem",
        "multiple totems",
        "trap",
        "cluster traps",
        "multiple traps",
        "high-impact mine",
        "blastchain mine",
        "minefield",
        "automation",
        "autoexertion",
        "generosity",
        "awakened generosity",
        "arrogance",
        "eternal blessing",
        "guardian's blessing",
        "hextouch",
        "awakened hextouch",
        "mark on hit",
        "blasphemy",
        "awakened blasphemy",
        "manaforged arrows",
        "bonechill",
        "feeding frenzy",
        "meat shield",
        "elemental army",
        "minion life",
        "minion speed",
        "summon phantasm",
        "withering touch",
        "mirage archer",
        "focused ballista",
        "shockwave",
        "prismatic burst",
    }
)

_AILMENT_DEPENDENT_SUPPORTS = frozenset({"hypothermia", "immolate", "combustion", "ice bite", "innervate"})
_ELE_OR_CHAOS_DAMAGE_SUPPORTS = frozenset(
    {
        "added fire damage",
        "added cold damage",
        "added lightning damage",
        "added chaos damage",
        "elemental damage with attacks",
        "elemental focus",
        "void manipulation",
        "trinity",
        "cold penetration",
        "fire penetration",
        "lightning penetration",
        "elemental penetration",
        "hypothermia",
        "immolate",
    }
)
_PURE_ELE_DAMAGE_SUPPORTS = frozenset(
    {
        "added fire damage",
        "added cold damage",
        "added lightning damage",
        "elemental damage with attacks",
        "elemental focus",
        "trinity",
        "cold penetration",
        "fire penetration",
        "lightning penetration",
        "elemental penetration",
        "hypothermia",
        "immolate",
    }
)
_ELE_AURA_NAMES = frozenset({"hatred", "anger", "wrath"})


def _normalize_support_base_name(name: str) -> str:
    low = strip_pob_escapes(str(name or "")).strip().lower()
    if low.startswith("awakened "):
        low = low[len("awakened ") :].strip()
    if low.endswith(" support"):
        low = low[: -len(" support")].strip()
    return low


def _awakened_coc_gem_cdr(level: int) -> int:
    """Exact 3.30 Awakened CoC (`SupportCastOnCritTriggeredPlus`) spell CDR by gem level."""
    lv = max(1, int(level or 1))
    return 10 + (lv - 1) * 3


def _cwc_interval_ms(level: int, *, awakened: bool = False) -> int:
    lv = max(1, int(level or 1))
    if awakened:
        if lv in _AWAKENED_CWC_INTERVAL_MS:
            return _AWAKENED_CWC_INTERVAL_MS[lv]
        return 300 if lv >= 5 else 340
    if lv in _CWC_INTERVAL_MS:
        return _CWC_INTERVAL_MS[lv]
    return 320 if lv >= 20 else max(260, 400 - (lv - 1) * 4)


def compute_server_tick_cooldown(
    base_cd: float,
    cdr_pct: float,
    *,
    aps: float | None = None,
    triggered_spells: int = 1,
) -> dict[str, Any]:
    """Compute exact PoE1 0.033s (33ms, NOT 1/30=33.33ms) server-tick effective cooldown & next breakpoint."""
    base = max(0.001, float(base_cd))
    cdr = max(0.0, float(cdr_pct))
    spells = max(1, int(triggered_spells or 1))
    raw_cd = base / (1.0 + cdr / 100.0)
    ticks = max(1, math.ceil(raw_cd / POE1_SERVER_TICK_SEC - 1e-9))
    eff_cd = round(ticks * POE1_SERVER_TICK_SEC, 3)
    eff_ms = ticks * 33
    max_rate = round(1.0 / eff_cd, 2) if eff_cd > 0 else 0.0
    eff_cap = round(max_rate * spells, 2)

    next_ticks = ticks - 1
    next_info: dict[str, Any] | None = None
    if next_ticks >= 1:
        next_cdr_pct = math.ceil((base / (next_ticks * POE1_SERVER_TICK_SEC) - 1.0) * 100.0 - 1e-9)
        cdr_delta = max(0, int(next_cdr_pct - math.floor(cdr + 1e-9)))
        next_rate = round(1.0 / (next_ticks * POE1_SERVER_TICK_SEC), 2)
        next_info = {
            "targetTicks": next_ticks,
            "requiredCdrPct": int(next_cdr_pct),
            "cdrNeededPct": int(cdr_delta),
            "effectiveCdSec": round(next_ticks * POE1_SERVER_TICK_SEC, 3),
            "effectiveCdMs": next_ticks * 33,
            "maxTriggersPerSec": next_rate,
            "effectiveCapPerSec": round(next_rate * spells, 2),
        }

    out: dict[str, Any] = {
        "baseCdSec": base,
        "serverTickSec": POE1_SERVER_TICK_SEC,
        "serverTickMs": 33,
        "globalCdrPct": round(cdr, 2),
        "rawCdSec": round(raw_cd, 4),
        "ticks": ticks,
        "effectiveCdSec": eff_cd,
        "effectiveCdMs": eff_ms,
        "maxTriggersPerSec": max_rate,
        "triggeredSpells": spells,
        "effectiveCapPerSec": eff_cap,
        "nextBreakpoint": next_info,
    }
    if aps is not None and math.isfinite(float(aps)) and float(aps) > 0:
        aps_f = round(float(aps), 2)
        overspeed = aps_f > (eff_cap * 1.01)
        out["aps"] = aps_f
        out["overspeed"] = overspeed
        out["apsRatioToCap"] = round(aps_f / eff_cap, 3) if eff_cap > 0 else None
        if overspeed:
            out["overspeedNote"] = (
                f"APS ({aps_f:.2f}/s) exceeds trigger cap ({eff_cap:.2f}/s at {cdr:g}% CDR, {ticks}t={eff_ms}ms): "
                "attacks land during cooldown and HALVE effective trigger rate!"
            )
    return out


def build_server_tick_table(
    base_cd: float,
    *,
    cdr_pct: float | None = None,
    aps: float | None = None,
    triggered_spells: int = 1,
    max_ticks: int = 16,
) -> dict[str, Any]:
    """Build exact PoE1 0.033s (33ms) server-tick CDR breakpoint table for any cooldown skill/trigger."""
    base = max(0.001, float(base_cd))
    spells = max(1, int(triggered_spells or 1))
    base_ticks = max(1, math.ceil(base / POE1_SERVER_TICK_SEC - 1e-9))
    start_ticks = min(max(1, int(max_ticks)), base_ticks)

    rows: list[dict[str, Any]] = []
    seen_cdrs: set[int] = set()
    for t in range(start_ticks, 0, -1):
        req_cdr = max(0, math.ceil((base / (t * POE1_SERVER_TICK_SEC) - 1.0) * 100.0 - 1e-9))
        if req_cdr in seen_cdrs:
            continue
        seen_cdrs.add(req_cdr)
        eff_cd = round(t * POE1_SERVER_TICK_SEC, 3)
        rate = round(1.0 / eff_cd, 2)
        rows.append(
            {
                "ticks": t,
                "minCdrPct": int(req_cdr),
                "effectiveCdSec": eff_cd,
                "effectiveCdMs": t * 33,
                "maxTriggersPerSec": rate,
                "maxApsCap": round(rate * spells, 2),
            }
        )

    current = (
        compute_server_tick_cooldown(base, float(cdr_pct), aps=aps, triggered_spells=spells)
        if cdr_pct is not None
        else (
            compute_server_tick_cooldown(base, 0.0, aps=aps, triggered_spells=spells)
            if aps is not None
            else None
        )
    )
    return {
        "baseCdSec": base,
        "serverTickSec": POE1_SERVER_TICK_SEC,
        "serverTickMs": 33,
        "triggeredSpells": spells,
        "breakpoints": rows,
        "current": current,
        "cwcNote": (
            "Note: Cast while Channelling (CwC) uses a FIXED channelling interval "
            "(320ms at Lv20, 300ms Awakened Lv5), NOT a Cooldown — CDR has ZERO effect on CwC."
        ),
    }


def _active_item_lines(raw_text: str | None) -> list[str]:
    """Return active mod lines from a PoB item raw block (respecting Selected Variant if present)."""
    if not raw_text:
        return []
    m_sel = _ITEM_SEL_VARIANT_RE.search(raw_text)
    sel_var = int(m_sel.group(1)) if m_sel else None
    out: list[str] = []
    for raw_ln in raw_text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        if sel_var is not None:
            mv = _MOD_VARIANT_RE.search(ln)
            if mv:
                allowed = {int(x) for x in mv.group(1).split(",") if x.isdigit()}
                if allowed and sel_var not in allowed:
                    continue
        out.append(ln)
    return out


def _count_item_sockets(raw_text: str | None) -> int | None:
    if not raw_text:
        return None
    m = _ITEM_SOCKETS_RE.search(raw_text)
    if not m:
        return None
    letters = re.findall(r"[RGBWADrgbwad]", m.group(1))
    return len(letters) if letters else None


def _extract_global_cdr_sources(
    alloc_node_stats: list[tuple[str, list[str]]],
    equipped_slots: list[dict[str, Any]],
    cfg_res: dict[str, Any] | None,
) -> dict[str, Any]:
    """Collect global Cooldown Recovery Rate sources (excluding Travel/Guard/Warcry/other-group gem CDR)."""
    sources: list[dict[str, Any]] = []
    total_cdr = 0.0

    for node_label, stats_lines in alloc_node_stats:
        for ln in stats_lines:
            clean = strip_pob_escapes(str(ln or "")).strip()
            m = _GLOBAL_CDR_LINE_RE.match(clean)
            if m:
                val = float(m.group(1))
                total_cdr += val
                sources.append({"source": f"Tree: {node_label}", "value": val, "line": clean})

    for slot_entry in equipped_slots:
        sname = str(slot_entry.get("slotName") or "")
        if "Swap" in sname or slot_entry.get("inactive"):
            continue
        if slot_entry.get("isFlask") and not slot_entry.get("active"):
            continue
        raw_txt = slot_entry.get("raw")
        if not raw_txt:
            continue
        it = slot_entry.get("item") or {}
        it_name = strip_pob_escapes(str(it.get("title") or it.get("name") or sname))
        for ln in _active_item_lines(raw_txt):
            clean = strip_pob_escapes(ln).strip()
            m = _GLOBAL_CDR_LINE_RE.match(clean)
            if m:
                val = float(m.group(1))
                total_cdr += val
                sources.append({"source": f"{sname} ({it_name})", "value": val, "line": clean})

    for blk in (cfg_res or {}).get("customMods") or []:
        if not isinstance(blk, dict) or blk.get("enabled", True) is False:
            continue
        for raw_ln in str(blk.get("text") or "").splitlines():
            clean = strip_pob_escapes(raw_ln).strip()
            if not clean or clean.startswith("#"):
                continue
            m = _GLOBAL_CDR_LINE_RE.match(clean)
            if m:
                val = float(m.group(1))
                total_cdr += val
                sources.append({"source": "customMods", "value": val, "line": clean})

    return {
        "totalGlobalCdrPct": round(total_cdr, 2),
        "sources": sources,
    }


def _detect_build_mechanics_and_pitfalls(
    *,
    stats: dict[str, Any],
    skills_res: dict[str, Any] | None,
    items_res: dict[str, Any] | None,
    cfg_res: dict[str, Any] | None,
    tree_summary: dict[str, Any] | None = None,
    equipped_slots_raw: list[dict[str, Any]] | None = None,
    alloc_node_stats: list[tuple[str, list[str]]] | None = None,
    trigger_attack_aps: float | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Detect high-impact PoE1 3.30 build mechanics, dead/anti-synergy links, triggers, & defensive bricks."""
    gaps: list[dict[str, str]] = []
    mechanics: dict[str, Any] = {}

    groups = (skills_res or {}).get("groups") or []
    main_idx = (skills_res or {}).get("mainSocketGroup")
    items_by_id = {it["id"]: it for it in (items_res or {}).get("items", [])}
    slots_list = (items_res or {}).get("slots") or []

    # Check active equipped items by slot name (excluding weapon swap)
    active_items_by_slot: dict[str, dict[str, Any]] = {}
    for s in slots_list:
        iid = s.get("selItemId")
        sname = str(s.get("name") or "")
        if iid and not s.get("inactive") and "Swap" not in sname:
            active_items_by_slot[sname] = items_by_id.get(iid, {})

    raw_by_slot: dict[str, str] = {}
    for entry in equipped_slots_raw or []:
        sn = str(entry.get("slotName") or "")
        if sn and entry.get("raw"):
            raw_by_slot[sn] = str(entry["raw"])

    def _item_display(it: dict[str, Any]) -> str:
        return strip_pob_escapes(str(it.get("title") or it.get("name") or "")).strip()

    def _item_names_low(it: dict[str, Any]) -> str:
        return " ".join(
            strip_pob_escapes(str(it.get(k) or ""))
            for k in ("title", "titleZh", "name", "nameZh", "baseName", "baseNameZh")
        ).lower()

    w1_item = active_items_by_slot.get("Weapon 1") or {}
    has_sword_of_ages = "the sword of ages" in _item_names_low(w1_item)

    # Check allocated keystones & ascendancy names
    ks_lower: set[str] = set()
    asc_names_lower: set[str] = set()
    if isinstance(tree_summary, dict):
        for ks in tree_summary.get("keystones") or []:
            if isinstance(ks, dict):
                ks_lower.add(strip_pob_escapes(str(ks.get("name") or "")).lower())
        for an in tree_summary.get("ascendancyNodes") or []:
            if isinstance(an, dict):
                asc_names_lower.add(strip_pob_escapes(str(an.get("name") or "")).lower())

    has_perfect_crime = "perfect crime" in asc_names_lower
    has_bloodsoaked_blade = "bloodsoaked blade" in ks_lower
    has_living_blade = "the living blade" in asc_names_lower
    has_toads_price = "toad's price" in asc_names_lower

    # Check customMods active lines
    active_custom_lines: list[str] = []
    for blk in (cfg_res or {}).get("customMods") or []:
        if isinstance(blk, dict) and blk.get("enabled", True) is not False:
            for ln in str(blk.get("text") or "").splitlines():
                s_ln = strip_pob_escapes(ln).strip()
                if s_ln and not s_ln.startswith("#"):
                    active_custom_lines.append(s_ln)
    custom_mods_joined = "\n".join(active_custom_lines).lower()

    # Collect global CDR once for all cooldown triggers (Sword of Ages, CoC, Cospri, Mjolner, Poet's Pen, etc.)
    cdr_data = _extract_global_cdr_sources(
        alloc_node_stats or [],
        equipped_slots_raw or [],
        cfg_res,
    )
    global_cdr_pct = float(cdr_data["totalGlobalCdrPct"])

    # Collect all enabled non-swap active skill names across the build (for aura/burning cross-checks)
    all_active_non_swap_low: set[str] = set()
    for g in groups:
        if not g.get("enabled", True) or "Swap" in str(g.get("slot") or ""):
            continue
        for gm in g.get("gems") or []:
            if gm.get("enabled", True) and not gm.get("support"):
                all_active_non_swap_low.add(
                    strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip().lower()
                )

    # 1. Main group dead/anti-synergy supports + Firestorm/Cascade cap + Lv1 + empty sockets
    main_mana_cost = _finite_float(stats.get("ManaCost"), default=-1.0)
    main_group = next(
        (g for g in groups if g.get("isMain") or (main_idx is not None and g.get("index") == main_idx)),
        None,
    )
    if main_group and main_group.get("enabled", True):
        mg_idx = main_group.get("index")
        mg_slot = str(main_group.get("slot") or "")
        mg_gems = [gm for gm in (main_group.get("gems") or []) if gm.get("enabled", True)]
        mg_names_low = {strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).lower() for gm in mg_gems}
        mg_skill_ids = {str(gm.get("skillId") or "") for gm in mg_gems}
        mg_sup_norms = {
            _normalize_support_base_name(str(gm.get("name") or gm.get("nameSpec") or ""))
            for gm in mg_gems
            if gm.get("support")
        }

        is_zero_mana_group = (main_mana_cost == 0.0) or (has_sword_of_ages and mg_slot == "Weapon 1")
        has_arcane_surge_in_main = (
            "SupportArcaneSurge" in mg_skill_ids
            or "arcane surge" in mg_sup_norms
        )
        if is_zero_mana_group and has_arcane_surge_in_main:
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "HIGH",
                    "message": (
                        f"Dead Arcane Surge Support in 0-mana triggered Group #{mg_idx} ({mg_slot or 'main'}): "
                        "Arcane Surge requires spending mana and NEVER procs on a 0-mana triggered skill; "
                        "PoB buffArcaneSurge=True is fake water. Move Arcane Surge to a mana-spending utility skill "
                        "(e.g. Arcane Cloak/Frostblink) and replace with a real damage support."
                    ),
                }
            )

        # Inspiration Support in a 0-mana group generates 0 Inspiration Charges in live play
        if is_zero_mana_group and (
            "SupportInspiration" in mg_skill_ids or "inspiration" in mg_sup_norms
        ):
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "HIGH",
                    "message": (
                        f"Dead Inspiration Support in 0-mana Group #{mg_idx} ({mg_slot or 'main'}): "
                        "Inspiration Support in a 0-mana group generates 0 Inspiration Charges in live play — "
                        "PoB max-charge assumption is fake water! Spend mana on a linked/supported skill or swap support."
                    ),
                }
            )

        # Elemental Focus + Ailment-dependent supports (Hypothermia / Immolate / Combustion / Ice Bite / Innervate)
        if "elemental focus" in mg_sup_norms:
            ailment_conflicts = sorted(mg_sup_norms & _AILMENT_DEPENDENT_SUPPORTS)
            if ailment_conflicts:
                c_disp = ", ".join(x.title() for x in ailment_conflicts)
                gaps.append(
                    {
                        "category": "Mechanic",
                        "severity": "HIGH",
                        "message": (
                            f"Anti-synergy in Group #{mg_idx}: Elemental Focus prevents the supported skill from "
                            f"inflicting Elemental Ailments (Chill/Freeze/Ignite/Shock), disabling linked {c_disp} "
                            "unless a separate skill/ground effect reliably applies the ailment (verify PoB enemy "
                            "Chilled/Ignited config is NOT fake water!)."
                        ),
                    }
                )

        # Brutality + Elemental/Chaos damage supports or auras
        if "brutality" in mg_sup_norms:
            brut_sup_conflicts = sorted(mg_sup_norms & _ELE_OR_CHAOS_DAMAGE_SUPPORTS)
            brut_aura_conflicts = sorted(all_active_non_swap_low & _ELE_AURA_NAMES)
            if brut_sup_conflicts or brut_aura_conflicts:
                parts = [x.title() for x in brut_sup_conflicts] + [f"{x.title()} (aura)" for x in brut_aura_conflicts]
                gaps.append(
                    {
                        "category": "Mechanic",
                        "severity": "HIGH",
                        "message": (
                            f"Anti-synergy in Group #{mg_idx}: Brutality Support causes supported skill to deal NO "
                            f"Elemental or Chaos damage, nullifying {', '.join(parts)}!"
                        ),
                    }
                )

        # Void Manipulation + Pure Elemental damage supports
        if "void manipulation" in mg_sup_norms:
            vm_conflicts = sorted(mg_sup_norms & _PURE_ELE_DAMAGE_SUPPORTS)
            if vm_conflicts:
                gaps.append(
                    {
                        "category": "Mechanic",
                        "severity": "HIGH",
                        "message": (
                            f"Anti-synergy in Group #{mg_idx}: Void Manipulation prevents Elemental damage, "
                            f"nullifying {', '.join(x.title() for x in vm_conflicts)}!"
                        ),
                    }
                )

        # Controlled Destruction on CoC or high-crit builds
        if "controlled destruction" in mg_sup_norms:
            has_coc_main = "cast on critical strike" in mg_sup_norms
            crit_val = _finite_float(stats.get("CritChance"), default=0.0)
            if has_coc_main:
                gaps.append(
                    {
                        "category": "Mechanic",
                        "severity": "HIGH",
                        "message": (
                            f"Anti-synergy in Group #{mg_idx}: Controlled Destruction (80% less Crit Chance) "
                            "bricks Cast On Critical Strike trigger reliability!"
                        ),
                    }
                )
            elif crit_val >= 35.0:
                gaps.append(
                    {
                        "category": "Mechanic",
                        "severity": "MEDIUM",
                        "message": (
                            f"Caveat in Group #{mg_idx}: Controlled Destruction imposes 80% less Critical Strike Chance "
                            f"on a crit build (current CritChance={crit_val:.1f}%)."
                        ),
                    }
                )

        # Immolate without Ignite chance or burning ground
        if "immolate" in mg_sup_norms and "elemental focus" not in mg_sup_norms:
            ignite_ch = _finite_float(stats.get("IgniteChance"), default=-1.0)
            has_burn_skill = any(
                k in nm
                for nm in all_active_non_swap_low
                for k in ("righteous fire", "flame wall", "scorching ray", "searing bond", "fire trap")
            )
            if ignite_ch == 0.0 and not has_burn_skill:
                gaps.append(
                    {
                        "category": "Mechanic",
                        "severity": "MEDIUM",
                        "message": (
                            f"Conditional Support in Group #{mg_idx}: Immolate requires enemies to be Burning/Ignited, "
                            "but build has 0% Ignite Chance and no burning-ground skill."
                        ),
                    }
                )

        # Firestorm of Pelting + Greater Spell Cascade 12-storm cap + Lv1 check
        if any("firestorm of pelting" in nm for nm in mg_names_low) and any(
            "greater spell cascade" in nm or "spell cascade" in nm for nm in mg_names_low
        ):
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "HIGH",
                    "message": (
                        f"Group #{mg_idx} links Firestorm of Pelting with Spell Cascade / Greater Spell Cascade: "
                        "all cascades share Firestorm of Pelting's global 12-storm cap (hurts single-target DPS "
                        "while adding Less Area Damage penalty)."
                    ),
                }
            )

        # Check under-leveled Lv1 support gems in main group (excluding Empower/Enlighten/Enhance/CwDT)
        for gm in mg_gems:
            if gm.get("fromItem") or not gm.get("support"):
                continue
            g_lvl = int(gm.get("level") or 0)
            g_name = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or ""))
            g_low = g_name.lower()
            if g_lvl == 1 and not any(
                ex in g_low
                for ex in ("empower", "enlighten", "enhance", "cast when damage taken", "arcane surge", "culling strike", "invert the rules")
            ):
                gaps.append(
                    {
                        "category": "Skills",
                        "severity": "MEDIUM",
                        "message": f"Main Group #{mg_idx} has Level 1 support gem '{g_name}' (check if under-leveled).",
                    }
                )

        # Check empty sockets on 5/6-socket main item
        if mg_slot and mg_slot in raw_by_slot:
            total_socks = _count_item_sockets(raw_by_slot[mg_slot])
            if total_socks is not None and total_socks >= 5:
                used_socks = sum(
                    1
                    for g in groups
                    if g.get("slot") == mg_slot and not g.get("source")
                    for gm in (g.get("gems") or [])
                    if not gm.get("fromItem")
                )
                if used_socks < total_socks:
                    gaps.append(
                        {
                            "category": "Skills",
                            "severity": "HIGH",
                            "message": (
                                f"Main skill item '{mg_slot}' has {total_socks} sockets but only {used_socks} gem(s) socketed "
                                f"({total_socks - used_socks} empty socket(s))!"
                            ),
                        }
                    )

    # 2. Orphan support gems in Weapon Swap
    for g in groups:
        g_slot = str(g.get("slot") or "")
        if "Swap" not in g_slot:
            continue
        g_gems = [gm for gm in (g.get("gems") or []) if not gm.get("fromItem")]
        if not g_gems:
            continue
        sup_gems = [gm for gm in g_gems if gm.get("support")]
        act_gems = [gm for gm in g_gems if not gm.get("support")]
        if sup_gems and not act_gems:
            names_s = ", ".join(
                strip_pob_escapes(str(gm.get("nameZh") or gm.get("name") or ""))
                for gm in sup_gems
            )
            gaps.append(
                {
                    "category": "Skills",
                    "severity": "HIGH",
                    "message": (
                        f"Orphan support gem(s) in '{g_slot}' (Group #{g.get('index')}: {names_s}) — "
                        "inactive in weapon swap (check if mistakenly left out of main link)!"
                    ),
                }
            )
        elif any("archmage" in strip_pob_escapes(str(gm.get("name") or "")).lower() for gm in sup_gems):
            names_s = ", ".join(
                strip_pob_escapes(str(gm.get("nameZh") or gm.get("name") or ""))
                for gm in g_gems
            )
            gaps.append(
                {
                    "category": "Skills",
                    "severity": "HIGH",
                    "message": (
                        f"Core support gem (Archmage) sitting in '{g_slot}' (Group #{g.get('index')}: {names_s}) — "
                        "inactive in weapon swap!"
                    ),
                }
            )

    # 3. The Sword of Ages (Temporal Mirage) 3.30 Live-vs-PoB Advisory & 0.033s Server Tick Cooldown Breakdown
    if has_sword_of_ages:
        tick_info = compute_server_tick_cooldown(0.50, global_cdr_pct)
        has_43_comp = bool(re.search(r"4[23](?:\.\d+)?%\s+more\s+spell\s+damage", custom_mods_joined))
        soa_block: dict[str, Any] = {
            "equipped": True,
            "baseCooldownSec": 0.50,
            "globalCdrPct": global_cdr_pct,
            "cdrSources": cdr_data["sources"],
            "temporalMirageCooldown": tick_info,
            "hasPerfectCrime": has_perfect_crime,
            "hasTriggerbotCompensationCustomMod": has_43_comp,
            "advisories": [
                (
                    "PoB 2.68.1 `other.lua` misses Temporal Mirage's 0.50s base cooldown AND wrongly applies Saboteur "
                    "`Perfect Crime` (Triggerbots) `30% less` damage to clones. 3.30 live game verified: clones do NOT take "
                    "`-30% less` (`43% more Spell Damage` in customMods is the exact PoB compensation"
                    + ("; ALREADY PRESENT in customMods" if has_43_comp else ("; currently MISSING in customMods" if has_perfect_crime else ""))
                    + ")."
                ),
                (
                    "`on using a Movement Attack`: holding channelling `Cyclone` triggers ONLY ONCE on channel start "
                    "(never continuously while held); requires tap-macro (interval >= "
                    f"{tick_info['effectiveCdMs']}ms + jitter margin) or discrete movement attacks "
                    "(`Leap Slam`/`Flicker Strike`/`Consecrated Path`)."
                ),
            ],
        }
        mechanics["swordOfAges"] = soa_block
        if has_perfect_crime and not has_43_comp:
            gaps.append(
                {
                    "category": "PoBModeling",
                    "severity": "MEDIUM",
                    "message": (
                        "The Sword of Ages + Saboteur Perfect Crime: PoB 2.68.1 wrongly applies Triggerbots -30% less "
                        "to Temporal Mirage clones (live 3.30 does NOT). Add `43% more Spell Damage` in customMods to model true live DPS."
                    ),
                }
            )

    # 3b. Universal PoE1 Trigger, Cooldown (0.033s Tick) & APS Overspeed Engine + CwC Fixed Interval
    triggers_detected: list[dict[str, Any]] = []
    seen_trigger_keys: set[str] = set()
    main_speed = _finite_float(stats.get("Speed"), default=0.0)
    eff_attack_aps = (
        float(trigger_attack_aps)
        if (trigger_attack_aps is not None and math.isfinite(float(trigger_attack_aps)) and float(trigger_attack_aps) > 0)
        else (main_speed if main_speed > 0 else None)
    )

    for g in groups:
        if not g.get("enabled", True) or "Swap" in str(g.get("slot") or ""):
            continue
        g_idx = g.get("index")
        is_mg = bool(g.get("isMain")) or (main_idx is not None and g_idx == main_idx)
        g_gems = [gm for gm in (g.get("gems") or []) if gm.get("enabled", True)]
        act_gems_in_g = [gm for gm in g_gems if not gm.get("support")]

        for gm in g_gems:
            gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip()
            glow = gnm.lower()
            gsid = str(gm.get("skillId") or "")
            glvl = int(gm.get("level") or 20)

            # Cast On Critical Strike / Awakened CoC
            if (
                "cast on critical strike" in glow
                or gsid in ("SupportCastOnCriticalStrike", "SupportAwakenedCastOnCriticalStrike")
            ):
                is_awk = "awakened" in glow or gsid == "SupportAwakenedCastOnCriticalStrike"
                gem_cdr = _awakened_coc_gem_cdr(glvl) if is_awk else 0
                total_cdr = round(global_cdr_pct + gem_cdr, 2)
                num_spells = max(1, len(act_gems_in_g) - 1) if len(act_gems_in_g) >= 2 else 1
                aps_for_group = eff_attack_aps if is_mg else None
                tick = compute_server_tick_cooldown(0.15, total_cdr, aps=aps_for_group, triggered_spells=num_spells)
                trig_entry: dict[str, Any] = {
                    "kind": "cooldown_trigger",
                    "name": gnm,
                    "groupIndex": g_idx,
                    "slot": g.get("slot") or "-",
                    "baseCooldownSec": 0.15,
                    "globalCdrPct": global_cdr_pct,
                    "gemCdrPct": gem_cdr,
                    "totalCdrPct": total_cdr,
                    "cdrSources": cdr_data["sources"],
                    "triggeredSpells": num_spells,
                    "cooldown": tick,
                }
                triggers_detected.append(trig_entry)
                if tick.get("overspeed") and aps_for_group is not None:
                    nb = tick.get("nextBreakpoint") or {}
                    next_hint = (
                        f" or reach next CDR breakpoint ({nb['requiredCdrPct']}% CDR -> {nb['targetTicks']}t = "
                        f"{nb['effectiveCdMs']}ms, cap {nb['effectiveCapPerSec']:.2f}/s)"
                        if nb
                        else ""
                    )
                    gaps.append(
                        {
                            "category": "TriggerOverspeed",
                            "severity": "HIGH",
                            "message": (
                                f"Trigger Overspeed ({gnm}, Group #{g_idx}): Attack rate ({aps_for_group:.2f}/s) "
                                f"exceeds CoC trigger cap ({tick['effectiveCapPerSec']:.2f}/s at {total_cdr:g}% CDR, "
                                f"{tick['ticks']}t = {tick['effectiveCdMs']}ms) — attacks land inside cooldown and "
                                f"HALVE effective trigger rate! Either lower APS <= {tick['effectiveCapPerSec']:.2f}/s{next_hint}."
                            ),
                        }
                    )

            # Cast while Channelling / Awakened CwC (FIXED INTERVAL — NOT a Cooldown!)
            elif (
                "cast while channelling" in glow
                or gsid in ("SupportCastWhileChannelling", "SupportAwakenedCastWhileChannelling")
            ):
                is_awk = "awakened" in glow or gsid == "SupportAwakenedCastWhileChannelling"
                iv_ms = _cwc_interval_ms(glvl, awakened=is_awk)
                triggers_detected.append(
                    {
                        "kind": "fixed_interval_channelling",
                        "name": gnm,
                        "groupIndex": g_idx,
                        "slot": g.get("slot") or "-",
                        "level": glvl,
                        "intervalMs": iv_ms,
                        "intervalSec": round(iv_ms / 1000.0, 3),
                        "scaledByCdr": False,
                        "note": (
                            f"`{gnm}` (Lv.{glvl}) uses a **fixed channelling trigger interval of {iv_ms}ms "
                            f"({iv_ms / 1000.0:.2f}s)**, NOT a Cooldown — `Cooldown Recovery Rate (CDR)` has "
                            "**ZERO effect** on CwC trigger rate!"
                        ),
                    }
                )

            # Spellslinger (0.60s base CD)
            elif glow == "spellslinger" or gsid == "Spellslinger":
                tkey = f"spellslinger:{g_idx}"
                if tkey not in seen_trigger_keys:
                    seen_trigger_keys.add(tkey)
                    tick = compute_server_tick_cooldown(0.60, global_cdr_pct)
                    triggers_detected.append(
                        {
                            "kind": "cooldown_trigger",
                            "name": "Spellslinger",
                            "groupIndex": g_idx,
                            "slot": g.get("slot") or "-",
                            "baseCooldownSec": 0.60,
                            "globalCdrPct": global_cdr_pct,
                            "totalCdrPct": global_cdr_pct,
                            "cooldown": tick,
                        }
                    )

            # Cast when Damage Taken (0.25s base CD, main group only to avoid CwDT-Molten-Shell noise)
            elif is_mg and ("cast when damage taken" in glow or gsid == "SupportCastWhenDamageTaken"):
                tick = compute_server_tick_cooldown(0.25, global_cdr_pct)
                triggers_detected.append(
                    {
                        "kind": "cooldown_trigger",
                        "name": gnm,
                        "groupIndex": g_idx,
                        "slot": g.get("slot") or "-",
                        "baseCooldownSec": 0.25,
                        "globalCdrPct": global_cdr_pct,
                        "totalCdrPct": global_cdr_pct,
                        "cooldown": tick,
                    }
                )

    # Item-granted cooldown triggers (Cospri's Malice, Mjolner, Poet's Pen, Hidden Blade, Kitava's Thirst)
    _ITEM_TRIGGER_SPECS: tuple[tuple[str, str, float, bool], ...] = (
        ("cospri's malice", "Cospri's Malice", 0.25, True),
        ("mjölner", "Mjölner", 0.25, True),
        ("mjolner", "Mjölner", 0.25, True),
        ("the poet's pen", "The Poet's Pen", 0.25, True),
        ("the hidden blade", "The Hidden Blade", 0.50, False),
        ("kitava's thirst", "Kitava's Thirst", 0.10, False),
    )
    for sname, it in active_items_by_slot.items():
        nm_low = _item_names_low(it)
        for pat, disp_name, b_cd, is_atk_trig in _ITEM_TRIGGER_SPECS:
            if pat in nm_low and disp_name not in seen_trigger_keys:
                seen_trigger_keys.add(disp_name)
                aps_for_item = eff_attack_aps if is_atk_trig else None
                tick = compute_server_tick_cooldown(b_cd, global_cdr_pct, aps=aps_for_item)
                triggers_detected.append(
                    {
                        "kind": "item_cooldown_trigger",
                        "name": disp_name,
                        "slot": sname,
                        "baseCooldownSec": b_cd,
                        "globalCdrPct": global_cdr_pct,
                        "totalCdrPct": global_cdr_pct,
                        "cooldown": tick,
                    }
                )
                if is_atk_trig and tick.get("overspeed") and aps_for_item is not None:
                    nb = tick.get("nextBreakpoint") or {}
                    next_hint = (
                        f" or reach {nb['requiredCdrPct']}% CDR ({nb['targetTicks']}t={nb['effectiveCdMs']}ms, cap {nb['effectiveCapPerSec']:.2f}/s)"
                        if nb
                        else ""
                    )
                    gaps.append(
                        {
                            "category": "TriggerOverspeed",
                            "severity": "HIGH",
                            "message": (
                                f"Trigger Overspeed ({disp_name}, {sname}): Attack rate ({aps_for_item:.2f}/s) "
                                f"exceeds trigger cap ({tick['effectiveCapPerSec']:.2f}/s at {global_cdr_pct:g}% CDR, "
                                f"{tick['ticks']}t = {tick['effectiveCdMs']}ms) — lower APS <= {tick['effectiveCapPerSec']:.2f}/s{next_hint}."
                            ),
                        }
                    )

    if triggers_detected:
        mechanics["triggers"] = triggers_detected

    # 4. Invert the Rules / Resistance Inversion Conflict Detector
    invert_sources: list[str] = []
    for g in groups:
        if not g.get("enabled", True) or "Swap" in str(g.get("slot") or ""):
            continue
        for gm in g.get("gems") or []:
            if not gm.get("enabled", True):
                continue
            gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or ""))
            if "invert the rules" in gnm.lower() or str(gm.get("skillId") or "") == "SupportInvertTheRules":
                invert_sources.append(f"Group #{g.get('index')} Invert the Rules Support")
    for sname, raw_txt in raw_by_slot.items():
        if "Swap" in sname:
            continue
        for ln in _active_item_lines(raw_txt):
            if "resistance values as inverted" in ln.lower() or "resistances as inverted" in ln.lower():
                it_nm = _item_display(active_items_by_slot.get(sname) or {}) or sname
                invert_sources.append(f"{sname} ({it_nm}): {strip_pob_escapes(ln)}")
    for n_lbl, n_stats in alloc_node_stats or []:
        for ln in n_stats:
            if "resistance values as inverted" in str(ln).lower() or "resistances as inverted" in str(ln).lower():
                invert_sources.append(f"Tree ({n_lbl}): {strip_pob_escapes(ln)}")

    if invert_sources:
        curse_hits_active: list[str] = []
        curse_hits_swap: list[str] = []
        for g in groups:
            if not g.get("enabled", True):
                continue
            is_swap = "Swap" in str(g.get("slot") or "")
            for gm in g.get("gems") or []:
                if not gm.get("enabled", True):
                    continue
                gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or ""))
                if gnm.lower() in _ELE_CURSE_OR_EXPOSURE_NAMES:
                    label = f"{gnm} (Group #{g.get('index')} {g.get('slot') or '-'})"
                    if is_swap:
                        curse_hits_swap.append(label)
                    else:
                        curse_hits_active.append(label)
        mechanics["resistanceInversion"] = {
            "sources": invert_sources,
            "conflictingActiveCursesOrExposure": curse_hits_active,
            "conflictingSwapCursesOrExposure": curse_hits_swap,
            "note": (
                "Lowering enemy elemental resistance via Curses/Exposure BEFORE inversion shrinks the inverted "
                "negative-resistance bonus; Elemental Penetration applies AFTER inversion and still works fully."
            ),
        }
        if curse_hits_active:
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "HIGH",
                    "message": (
                        f"Resistance Inversion anti-synergy: {', '.join(invert_sources[:2])} is active alongside "
                        f"resistance-lowering skill(s) {', '.join(curse_hits_active)} — lowering enemy resistance before "
                        "inversion REDUCES inverted bonus (use Elemental Penetration instead, which applies after inversion)."
                    ),
                }
            )
        elif curse_hits_swap:
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "LOW",
                    "message": (
                        f"Resistance Inversion note ({invert_sources[0]}): do NOT pair with elemental curses/exposure "
                        f"(seen in swap: {', '.join(curse_hits_swap)}); Penetration still works after inversion."
                    ),
                }
            )

    # 5. Bloodsoaked Blade + Tinctures + Screams of the Desiccated Awareness
    active_tinctures: list[str] = []
    for s in slots_list:
        if s.get("isFlask") and s.get("active") and s.get("selItemId"):
            it = items_by_id.get(s["selItemId"], {})
            if _is_tincture_item(it):
                active_tinctures.append(f"{s['name']}: {_item_display(it)}")
    belt_item = active_items_by_slot.get("Belt") or {}
    has_screams_belt = "screams of the desiccated" in _item_display(belt_item).lower()
    if has_bloodsoaked_blade or active_tinctures or has_screams_belt:
        t_limit = int(_finite_float(stats.get("TinctureLimit"), default=1.0))
        mechanics["tinctureSetup"] = {
            "bloodsoakedBlade": has_bloodsoaked_blade,
            "theLivingBlade": has_living_blade,
            "toadsPrice": has_toads_price,
            "screamsOfTheDesiccated": has_screams_belt,
            "tinctureLimit": t_limit,
            "activeTinctureCount": len(active_tinctures),
            "activeTinctures": active_tinctures,
            "note": (
                f"Active Tinctures: {len(active_tinctures)}/{t_limit}"
                + (" (valid +2 limit from Reliquarian 'The Living Blade' / 活木之刃)" if has_living_blade else "")
                + ". CRITICAL PoE1 rule: **Tinctures are NOT Flasks** — `Screams of the Desiccated` "
                "('while affected by no Flasks') works 100% with active Tinctures"
                + (" (reinforced by `Toad's Price` disabling Utility Flasks)" if has_toads_price else "")
                + (
                    "; `Bloodsoaked Blade` sustains Tinctures via Life Regen against Weeping Wounds (not temporary flask water)."
                    if has_bloodsoaked_blade
                    else "."
                )
            ),
        }

    # 6. Dark Bargain / Dark Pact count > 1 with Greater Spell Echo / Spell Echo double-counting FullDPS
    for g in groups:
        if not g.get("enabled", True) or not g.get("includeInFullDPS"):
            continue
        g_gems = [gm for gm in (g.get("gems") or []) if gm.get("enabled", True)]
        echo_gem = next(
            (
                strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or ""))
                for gm in g_gems
                if gm.get("support")
                and "spell echo" in strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).lower()
            ),
            None,
        )
        if not echo_gem:
            continue
        for gm in g_gems:
            if gm.get("support"):
                continue
            gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or ""))
            cnt = int(gm.get("count") or 1)
            if cnt > 1 and any(k in gnm.lower() for k in ("dark bargain", "dark pact")):
                gaps.append(
                    {
                        "category": "PoBWater",
                        "severity": "HIGH",
                        "message": (
                            f"Group #{g.get('index')} '{gnm}' has count={cnt} with '{echo_gem}' enabled (includeInFullDPS=True): "
                            f"PoB already models Spell Echo repeats in skill DPS, so count={cnt} double-counts FullDPS by {cnt}x!"
                        ),
                    }
                )

    # 7. Universal Defensive & Sustain Brick Detectors
    defensive_bricks: list[str] = []
    life_val = _finite_float(stats.get("Life"), default=0.0)
    life_unres = _finite_float(stats.get("LifeUnreserved"), default=life_val)
    life_unres_pct = (
        _finite_float(stats.get("LifeUnreservedPercent"), default=100.0)
        if stats.get("LifeUnreservedPercent") is not None
        else ((life_unres / life_val * 100.0) if life_val > 0 else 100.0)
    )
    mana_unres = _finite_float(stats.get("ManaUnreserved"), default=-1.0)
    mana_unres_pct = (
        _finite_float(stats.get("ManaUnreservedPercent"), default=100.0)
        if stats.get("ManaUnreservedPercent") is not None
        else 100.0
    )
    es_val = _finite_float(stats.get("EnergyShield"), default=0.0)
    chaos_res = _finite_float(stats.get("ChaosResist"), default=0.0)
    has_ci = ("chaos inoculation" in ks_lower) or (0.0 < life_val <= 1.0)
    has_eb = "eldritch battery" in ks_lower

    # 7a. EB + CI fatal brick
    if has_eb and has_ci:
        msg = (
            "FATAL Keystone Brick — Eldritch Battery + Chaos Inoculation (EB + CI): "
            "CI sets Maximum Life to 1 while EB moves Energy Shield to protect Mana instead of Life, "
            "leaving 1 Life and 0 ES protecting your Life pool (instant death to any hit)!"
        )
        gaps.append({"category": "DefensiveBrick", "severity": "HIGH", "message": msg})
        defensive_bricks.append(msg)

    # 7b. Low-Life Chaos Bypass & Esh's Visage Brick
    is_low_life = (not has_ci) and life_val > 1.0 and (
        life_unres_pct <= 50.0 or (life_val > 0 and (life_unres / life_val) <= 0.50)
    )
    equipped_names_all = {sn: _item_names_low(it) for sn, it in active_items_by_slot.items()}
    esh_slot = next(
        (sn for sn, nl in equipped_names_all.items() if "esh's visage" in nl or "艾许之面" in nl),
        None,
    )
    has_shavs = any("shavronne's wrappings" in nl or "薛朗的护身长袍" in nl for nl in equipped_names_all.values())
    has_solaris = any("solaris lorica" in nl or "烈炎之袍" in nl for nl in equipped_names_all.values())
    has_ivory = any("the ivory tower" in nl or "象牙之塔" in nl for nl in equipped_names_all.values())
    has_active_coruscating = any(
        s.get("isFlask")
        and s.get("active")
        and s.get("selItemId")
        and ("coruscating elixir" in _item_names_low(items_by_id.get(s["selItemId"], {})) or "闪耀精华" in _item_names_low(items_by_id.get(s["selItemId"], {})))
        for s in slots_list
    )
    has_unconditional_chaos_shield = False
    for sn, raw_txt in raw_by_slot.items():
        if "Swap" in sn or sn == esh_slot:
            continue
        for ln in _active_item_lines(raw_txt):
            ll = ln.lower()
            if "chaos damage" in ll and "does not bypass energy shield" in ll and "low life" not in ll:
                has_unconditional_chaos_shield = True
                break

    if esh_slot is not None:
        esh_raw = raw_by_slot.get(esh_slot, "")
        esh_checks_low_mana = any("low mana" in ln.lower() for ln in _active_item_lines(esh_raw))
        is_low_mana = mana_unres_pct <= 35.0
        if is_low_life or (esh_checks_low_mana and is_low_mana):
            reason = (
                f"Low Life (LifeUnreserved={life_unres:.0f}/{life_val:.0f} = {life_unres_pct:.1f}% <= 50%)"
                if is_low_life
                else f"Low Mana (ManaUnreserved={mana_unres_pct:.1f}% <= 35%)"
            )
            msg = (
                f"CRITICAL Low-Life Brick — Esh's Visage ({esh_slot}: 'Chaos Damage taken does not bypass ES "
                f"while NOT on Low Life') is BROKEN because character is on {reason}! "
                "Chaos damage bypasses Energy Shield completely and one-shots unreserved Life! "
                "Fix: equip Shavronne's Wrappings, Solaris Lorica, The Ivory Tower (with unreserved Mana), "
                "100%-uptime Coruscating Elixir, or switch to Chaos Inoculation (CI)."
            )
            gaps.append({"category": "DefensiveBrick", "severity": "HIGH", "message": msg})
            defensive_bricks.append(msg)
    elif (
        is_low_life
        and es_val > max(life_unres * 2.0, 500.0)
        and not (has_shavs or has_solaris or has_ivory or has_active_coruscating or has_unconditional_chaos_shield)
    ):
        msg = (
            f"Low-Life Chaos Bypass Risk: character is on Low Life ({life_unres:.0f}/{life_val:.0f} = "
            f"{life_unres_pct:.1f}% unreserved, ES {es_val:.0f}, Chaos Res {chaos_res:.0f}%) with NO active "
            "Chaos-bypass protection (Shavronne's Wrappings / Solaris Lorica / The Ivory Tower / Coruscating Elixir) — "
            "Chaos damage bypasses ES directly into low unreserved Life!"
        )
        gaps.append({"category": "DefensiveBrick", "severity": "HIGH", "message": msg})
        defensive_bricks.append(msg)

    if has_ivory and is_low_life and mana_unres == 0.0:
        msg = (
            "The Ivory Tower Brick: 'Chaos Damage is taken from Mana before Life' provides ZERO protection "
            "when Unreserved Mana is 0 — leave enough Unreserved Mana to absorb Chaos hits!"
        )
        gaps.append({"category": "DefensiveBrick", "severity": "HIGH", "message": msg})
        defensive_bricks.append(msg)

    # 7c. Dissolution of the Flesh + Life Sacrifice / Self-Damage / Lifetap conflict
    has_dissolution = any(
        "dissolution of the flesh" in nl or "血肉溶解" in nl for nl in equipped_names_all.values()
    )
    if has_dissolution:
        diss_conflicts: list[str] = []
        if any("righteous fire" in nm for nm in all_active_non_swap_low):
            diss_conflicts.append("Righteous Fire (continuous self-burn prevents Dissolution 2s unreserve!)")
        if any("rathpith globe" in nl or "拉斯皮斯之球" in nl for nl in equipped_names_all.values()):
            diss_conflicts.append("Rathpith Globe (sacrifices 10% Life per cast)")
        all_enabled_sups_low = {
            _normalize_support_base_name(str(gm.get("name") or gm.get("nameSpec") or ""))
            for g in groups
            if g.get("enabled", True) and "Swap" not in str(g.get("slot") or "")
            for gm in (g.get("gems") or [])
            if gm.get("enabled", True) and gm.get("support")
        }
        if "sacrifice" in all_enabled_sups_low:
            diss_conflicts.append("Sacrifice Support (sacrifices 20% current Life per cast)")
        if "lifetap" in all_enabled_sups_low:
            diss_conflicts.append("Lifetap Support (spends Life directly while Life pool is reserved)")
        if diss_conflicts:
            msg = (
                "Dissolution of the Flesh conflict: reserves Life on damage (and removes all ES) until 2s "
                f"with no Life damage taken — conflicts with {'; '.join(diss_conflicts)}!"
            )
            gaps.append({"category": "DefensiveBrick", "severity": "HIGH", "message": msg})
            defensive_bricks.append(msg)

    if defensive_bricks:
        mechanics["defensiveBricks"] = defensive_bricks

    # 7d. Crit Attack Accuracy Double-Roll Check
    raw_hc = stats.get("HitChance")
    crit_ch = _finite_float(stats.get("CritChance"), default=0.0)
    mh_acc = _finite_float(stats.get("MainHandAccuracy"), default=0.0)
    if raw_hc is not None:
        hc_val = _finite_float(raw_hc, default=100.0)
        if hc_val < 99.0 and crit_ch >= 25.0 and mh_acc > 0.0:
            eff_hit_sq = (hc_val / 100.0) ** 2 * 100.0
            gaps.append(
                {
                    "category": "Offence",
                    "severity": "MEDIUM",
                    "message": (
                        f"Crit Attack Accuracy Double-Roll: Hit Chance is {hc_val:.1f}% (< 99%) with "
                        f"{crit_ch:.1f}% Crit Chance — PoE1 rolls Accuracy TWICE to confirm critical strikes "
                        f"(effective crit hit factor ~{eff_hit_sq:.1f}%). Cap Accuracy to 100%!"
                    ),
                }
            )

    return gaps, mechanics


_KEEP_KEYWORDS = ("flasks", "custommods", "party")
DEWATER_SENTINEL_TITLE = "pob-cli dewater"


def _normalize_keep(keep: Iterable[str] | str | None) -> set[str]:
    """Normalize user keep tokens (config vars, 'Flask N', 'flasks', gem names, 'customMods', 'party')."""
    if keep is None:
        return set()
    tokens: Iterable[str] = (keep,) if isinstance(keep, str) else keep
    out: set[str] = set()
    for tok in tokens:
        for part in str(tok).split(","):
            p = part.strip().lower()
            if p:
                out.add(p)
    return out


def _unmatched_keep(keep_set: set[str], known: dict[str, str]) -> list[str]:
    """Keep tokens that name nothing in this build, with a close-match hint.

    pob-mcp rejects an unknown config key per key instead of silently ignoring it;
    a mistyped ``--keep`` would otherwise strip the item it meant to protect.
    """
    out: list[str] = []
    for tok in sorted(keep_set):
        if tok in known:
            continue
        hint = difflib.get_close_matches(tok, list(known), n=1, cutoff=0.8)
        msg = f"{tok}: not a config var, flask slot, skill gem, or flasks/customMods/party"
        if hint:
            msg += f" (did you mean {known[hint[0]]}?)"
        out.append(msg)
    return out


def _normalize_custom_mod_blocks(custom_mods: Any) -> list[dict[str, Any]]:
    """Normalize PoB customMods block list (matching bridge.lua's ``enabled ~= false`` semantics)."""
    return [
        {
            "title": b.get("title") or "Default",
            "text": b.get("text") or "",
            "enabled": bool(b.get("enabled", True) is not False),
        }
        for b in (custom_mods or [])
        if isinstance(b, dict)
    ]


def _is_vaal_text(text: str) -> bool:
    low = strip_pob_escapes(text or "").strip().lower()
    if not low:
        return False
    return low.startswith("vaal ") or " vaal " in low or "瓦尔" in low


def _is_vaal_skill_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    for key in ("name", "nameZh"):
        if _is_vaal_text(str(entry.get(key) or "")):
            return True
    sid = strip_pob_escapes(str(entry.get("skillId") or "")).strip().lower()
    return sid.startswith("vaal")


def dewater_build_in_place(
    worker: BridgeWorker,
    keep: Iterable[str] | str | None = None,
) -> dict[str, list[str]]:
    """Strip PoB inflation ('water') in place and set enemyIsBoss='Pinnacle'.

    Must be called inside a snapshot wrapper if non-destructive behavior is desired.
    Returns a dictionary of stripped inflation items across the 4 audit dimensions,
    plus ``kept`` (items left untouched because the user listed them in ``keep``)
    and ``keepRejected`` (unmatched ``keep`` tokens with close-match hints).
    """
    stripped, _records = dewater_build_with_records(worker, keep=keep)
    return stripped


def dewater_build_with_records(
    worker: BridgeWorker,
    keep: Iterable[str] | str | None = None,
    *,
    has_bloodsoaked_blade: bool | None = None,
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """De-water in place (2 pipelined RPC batches) and return reversible per-item records.

    Each record carries the exact bridge calls that ``restore`` the original state of
    that single item and ``revert`` it back to the de-watered state, so callers can
    measure every stripped item's real PoB contribution one at a time
    (see :func:`attribute_water`). ``keep`` tokens are matched case-insensitively
    against config var names, flask slot names (``Flask 1``) or ``flasks`` (all),
    skill gem names, ``customMods`` / ``customMods:<title>``, and ``party``.
    """
    keep_set = _normalize_keep(keep)
    stripped: dict[str, list[str]] = {
        "config": [],
        "flasks": [],
        "skills": [],
        "party": [],
        "kept": [],
        "keepRejected": [],
    }
    records: list[dict[str, Any]] = []
    # Every name a keep token may legitimately refer to (lowercase -> display form).
    known: dict[str, str] = {k: k for k in _KEEP_KEYWORDS}

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
            if var:
                known[str(var).lower()] = str(var)
            if not var or var in PERMANENT_CONFIG_VARS:
                continue
            if var == "enemyIsBoss":
                current_boss = val
                continue
            if val in (None, False, "", 0):
                continue
            if item.get("isDefault") is True:
                continue
            if "defaultState" in item and _same_state(val, item.get("defaultState")):
                continue
            if var == "presetBossSkills" and val == "None":
                continue
            if var in _POB_TRUTHY_CONFIG_DEFAULTS and _same_state(val, _POB_TRUTHY_CONFIG_DEFAULTS[var]):
                continue

            is_inflation = (
                var == "presetBossSkills"
                or var.startswith(INFLATION_CONFIG_PREFIXES)
                or var in INFLATION_CONFIG_EXACT
            )
            if not is_inflation:
                continue
            if var.lower() in keep_set:
                stripped["kept"].append(f"{var}={val!r} (kept)")
                continue
            if var == "presetBossSkills":
                had_preset_boss_skills = True
            stripped["config"].append(f"{var}={val!r} -> reset")
            mutations.append(("reset_config", {"var": var}))
            records.append(
                {
                    "dimension": "config",
                    "key": var,
                    "label": f"{var}={val!r}",
                    "restore": [("set_config", {"var": var, "value": val})],
                    "revert": [("reset_config", {"var": var})],
                    "probe": {"kind": "config", "var": var},
                    "original": val,
                }
            )

    if "enemyisboss" in keep_set:
        stripped["kept"].append(f"enemyIsBoss={current_boss!r} (kept)")
    else:
        if current_boss != "Pinnacle":
            stripped["config"].append(f"enemyIsBoss={current_boss!r} -> 'Pinnacle'")
        mutations.append(("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"}))
        if had_preset_boss_skills:
            # Second call ensures ConfigTab BuildModList sees updated enemyIsBoss selIndex
            mutations.append(("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"}))

    # Disable each active customMods group in place (title and text preserved) and attribute
    # each group on its own. A trailing comment-only sentinel keeps an enabled block with text,
    # so ConfigTab:BuildModList does not fall back to legacy input.customMods.
    custom_mods = cfg_res.get("customMods", [])
    blocks = _normalize_custom_mod_blocks(custom_mods)
    for b in blocks:
        raw_title = str(b["title"])
        clean_title = strip_pob_escapes(raw_title).strip()
        display_title = clean_title or raw_title
        known[f"custommods:{raw_title.lower()}"] = f"customMods:{display_title}"
        if clean_title:
            known[f"custommods:{clean_title.lower()}"] = f"customMods:{display_title}"

    dewatered_blocks = [dict(b) for b in blocks]
    group_records: list[tuple[int, str, str]] = []
    for i, b in enumerate(blocks):
        lines = [
            s
            for ln in b["text"].splitlines()
            if (s := strip_pob_escapes(ln).strip()) and not s.startswith("#")
        ]
        if not b["enabled"] or not lines:
            continue
        raw_title = str(b["title"])
        clean_title = strip_pob_escapes(raw_title).strip()
        display_title = clean_title or raw_title
        label = f"customMods[{display_title}] ({len(lines)} active lines: {'; '.join(lines[:3])})"
        keep_keys = {f"custommods:{raw_title.lower()}"}
        if clean_title:
            keep_keys.add(f"custommods:{clean_title.lower()}")
        if "custommods" in keep_set or bool(keep_keys & keep_set):
            stripped["kept"].append(f"{label} (kept)")
            continue
        dewatered_blocks[i]["enabled"] = False
        stripped["config"].append(f"{label} -> disabled")
        group_records.append((i, display_title, label))

    if group_records:
        has_sentinel = any(
            b["title"] == DEWATER_SENTINEL_TITLE
            and b["enabled"]
            and bool(b["text"].strip())
            and all(
                not (s := strip_pob_escapes(ln).strip()) or s.startswith("#")
                for ln in b["text"].splitlines()
            )
            for b in dewatered_blocks
        )
        if not has_sentinel:
            dewatered_blocks.append({"title": DEWATER_SENTINEL_TITLE, "text": "# disabled", "enabled": True})
        mutations.append(("set_custom_mods", {"list": dewatered_blocks}))
        for i, display_title, label in group_records:
            restored_blocks = [dict(b) for b in dewatered_blocks]
            restored_blocks[i]["enabled"] = True
            records.append(
                {
                    "dimension": "config",
                    "key": f"customMods:{display_title}",
                    "label": label,
                    "restore": [("set_custom_mods", {"list": restored_blocks})],
                    "revert": [("set_custom_mods", {"list": dewatered_blocks})],
                    "probe": {"kind": "customMods"},
                    "original": restored_blocks,
                }
            )

    # 2. Flask inflation (active flasks without Mageblood; preserve sustained Bloodsoaked Blade Tinctures)
    has_mb = _has_mageblood_equipped(items_res)
    items_by_id = {it["id"]: it for it in items_res.get("items", [])}
    for slot in items_res.get("slots", []):
        if slot.get("isFlask") and slot.get("name"):
            known[str(slot["name"]).lower()] = str(slot["name"])
    if not has_mb:
        has_active_tincture = any(
            slot.get("isFlask")
            and slot.get("active")
            and _is_tincture_item(items_by_id.get(slot.get("selItemId") or 0))
            for slot in items_res.get("slots", [])
        )
        bloodsoaked_active = (
            bool(has_bloodsoaked_blade)
            if has_bloodsoaked_blade is not None
            else (_has_bloodsoaked_blade_keystone(worker) if has_active_tincture else False)
        )
        for slot in items_res.get("slots", []):
            if slot.get("isFlask") and slot.get("active"):
                slot_name = slot["name"]
                item = items_by_id.get(slot.get("selItemId") or 0, {})
                item_title = strip_pob_escapes(item.get("titleZh") or item.get("title") or item.get("name") or slot_name)
                if "flasks" in keep_set or slot_name.lower() in keep_set:
                    stripped["kept"].append(f"{slot_name} ({item_title}) (kept)")
                    continue
                if bloodsoaked_active and _is_tincture_item(item):
                    stripped["kept"].append(
                        f"{slot_name} ({item_title}) (kept: Bloodsoaked Blade sustained Tincture; Tinctures != Flasks)"
                    )
                    continue
                stripped["flasks"].append(f"{slot_name} ({item_title}) -> active=False")
                mutations.append(("set_slot_active", {"slotName": slot_name, "active": False}))
                records.append(
                    {
                        "dimension": "flasks",
                        "key": slot_name,
                        "label": f"{slot_name} ({item_title})",
                        "restore": [("set_slot_active", {"slotName": slot_name, "active": True})],
                        "revert": [("set_slot_active", {"slotName": slot_name, "active": False})],
                        "probe": {"kind": "flask", "slot": slot_name},
                        "original": True,
                    }
                )

    # 3. Temporary guard / burst / Vaal skills
    # In PoB Data/Gems.lua & CalcSetup.lua:1831, grantedEffectList[1] (enableGlobal1) is the
    # VAAL burst skill and grantedEffectList[2] (enableGlobal2) is the BASE skill.
    # For non-hasGlobalEffect Vaal main skills (e.g. Vaal Ice Nova, Vaal Lightning Strike,
    # Vaal Spark), CalcSetup.lua:1831 ignores enableGlobal1 and CalcSetup.lua:1963 selects
    # socketGroupSkillList[group.mainActiveSkill], so we also switch mainActiveSkill to the base skill.
    main_socket_group = skills_res.get("mainSocketGroup")
    for group in skills_res.get("groups", []):
        g_idx = group["index"]
        group_enabled = group.get("enabled", True)
        is_main_group = bool(group.get("isMain")) or (main_socket_group is not None and g_idx == main_socket_group)
        skills_list = group.get("skills") or []
        orig_active = int(group.get("mainActiveSkill") or 1)
        for gem in group.get("gems", []):
            raw_names = [
                str(gem.get("nameSpec") or ""),
                str(gem.get("name") or ""),
                str(gem.get("nameZh") or ""),
                str(gem.get("skillId") or ""),
            ]
            names = [strip_pob_escapes(n).strip() for n in raw_names]
            for n in names:
                if n:
                    known[n.lower()] = n
            if not group_enabled or not gem.get("enabled", True) or gem.get("support"):
                continue
            combined_lower = " ".join(names).lower()
            is_vaal = (
                combined_lower.startswith("vaal ")
                or " vaal " in combined_lower
                or "瓦尔" in combined_lower
            )
            is_temp = any(pat in combined_lower for pat in TEMPORARY_SKILL_PATTERNS)
            original_vaal_g1 = bool(gem.get("enableGlobal1", False))
            is_active_vaal = is_vaal and original_vaal_g1

            selected_main_is_vaal = False
            next_skill: dict[str, Any] = {}
            if (
                is_vaal
                and not is_temp
                and not gem.get("hasGlobalEffect")
                and is_main_group
                and 1 <= orig_active
                and orig_active + 1 <= len(skills_list)
            ):
                cur_skill = skills_list[orig_active - 1]
                cand_next = skills_list[orig_active]
                if _is_vaal_skill_entry(cur_skill) and not _is_vaal_skill_entry(cand_next):
                    cur_names = {
                        strip_pob_escapes(str(cur_skill.get(k) or "")).strip().lower()
                        for k in ("name", "nameZh", "skillId")
                        if cur_skill.get(k)
                    }
                    gem_names = {n.lower() for n in names if n}
                    if not cur_names or bool(cur_names & gem_names) or any(
                        cn in gn or gn in cn
                        for cn in cur_names
                        for gn in gem_names
                        if len(cn) >= 4 and len(gn) >= 4
                    ):
                        selected_main_is_vaal = True
                        next_skill = cand_next

            disp = strip_pob_escapes(gem.get("nameZh") or gem.get("name") or gem.get("nameSpec") or "")
            if not (is_temp or is_active_vaal or selected_main_is_vaal):
                continue
            if any(n.lower() in keep_set for n in names if n):
                stripped["kept"].append(f"Group #{g_idx} Gem #{gem['index']} ({disp}) (kept)")
                continue
            gem_ref = {"group": g_idx, "index": gem["index"]}
            if is_temp:
                stripped["skills"].append(f"Group #{g_idx} Gem #{gem['index']} ({disp}) -> enabled=False")
                mutations.append(("set_gem", {**gem_ref, "enabled": False}))
                records.append(
                    {
                        "dimension": "skills",
                        "key": disp,
                        "label": f"Group #{g_idx} {disp}",
                        "restore": [("set_gem", {**gem_ref, "enabled": True})],
                        "revert": [("set_gem", {**gem_ref, "enabled": False})],
                        "probe": {"kind": "gem", **gem_ref, "fields": ["enabled"]},
                        "original": {"enabled": True},
                    }
                )
            else:
                original_base_enabled = bool(gem.get("enableGlobal2", True))
                dewater_calls: list[tuple[str, dict[str, Any]]] = [
                    (
                        "set_gem",
                        {
                            **gem_ref,
                            "enableGlobal1": False,
                            "enableGlobal2": original_base_enabled,
                        },
                    )
                ]
                restore_calls: list[tuple[str, dict[str, Any]]] = [
                    (
                        "set_gem",
                        {
                            **gem_ref,
                            "enableGlobal1": original_vaal_g1,
                            "enableGlobal2": original_base_enabled,
                        },
                    )
                ]
                probe_fields = ["enabled", "enableGlobal1", "enableGlobal2"]
                orig_state: dict[str, Any] = {
                    "enabled": True,
                    "enableGlobal1": original_vaal_g1,
                    "enableGlobal2": original_base_enabled,
                }
                if selected_main_is_vaal:
                    dewater_calls.append(("set_group", {"index": g_idx, "mainActiveSkill": orig_active + 1}))
                    restore_calls.append(("set_group", {"index": g_idx, "mainActiveSkill": orig_active}))
                    probe_fields.append("mainActiveSkill")
                    orig_state["mainActiveSkill"] = orig_active
                    next_name = strip_pob_escapes(
                        str(next_skill.get("nameZh") or next_skill.get("name") or f"skill #{orig_active + 1}")
                    )
                    stripped["skills"].append(
                        f"Group #{g_idx} Gem #{gem['index']} ({disp}) -> enableGlobal1=False, "
                        f"mainActiveSkill={orig_active + 1} ({next_name})"
                    )
                else:
                    stripped["skills"].append(
                        f"Group #{g_idx} Gem #{gem['index']} ({disp}) -> enableGlobal1=False (base skill kept)"
                    )
                mutations.extend(dewater_calls)
                records.append(
                    {
                        "dimension": "skills",
                        "key": disp,
                        "label": f"Group #{g_idx} {disp} (Vaal)",
                        "restore": restore_calls,
                        "revert": list(dewater_calls),
                        "probe": {
                            "kind": "gem",
                            **gem_ref,
                            "fields": probe_fields,
                        },
                        "original": orig_state,
                    }
                )

    # 4. PartyTab buffs (cleared wholesale; not individually reversible, so no attribution record)
    party_fields = party_res.get("fields", {})
    non_empty_party = [k for k, v in party_fields.items() if str(v or "").strip()]
    if non_empty_party or party_res.get("enableExportBuffs"):
        label = f"PartyTab fields ({', '.join(non_empty_party or ['enableExportBuffs'])})"
        if "party" in keep_set:
            stripped["kept"].append(f"{label} (kept)")
        else:
            stripped["party"].append(f"{label} -> cleared & disabled")
            mutations.append(("party_action", {"action": "disable"}))
            mutations.append(("party_action", {"action": "clear"}))

    if mutations:
        _call_batch(worker, mutations)

    stripped["keepRejected"] = _unmatched_keep(keep_set, known)
    return stripped, records


def _probe_rpc_method(probe: dict[str, Any]) -> str:
    kind = probe.get("kind")
    if kind in ("config", "customMods"):
        return "list_config"
    if kind == "flask":
        return "list_items"
    if kind == "gem":
        return "list_skills"
    raise ValueError(f"unknown probe kind {kind!r}")


def _extract_probe_state(probe: dict[str, Any], res: dict[str, Any]) -> Any:
    kind = probe.get("kind")
    if kind == "customMods":
        return _normalize_custom_mod_blocks(res.get("customMods", []))
    if kind == "config":
        for sec in res.get("sections", []):
            for item in sec.get("items", []):
                if item.get("var") == probe["var"]:
                    return item.get("value")
        return None
    if kind == "flask":
        for slot in res.get("slots", []):
            if slot.get("name") == probe["slot"]:
                return bool(slot.get("active"))
        return None
    if kind == "gem":
        for group in res.get("groups", []):
            if group.get("index") != probe["group"]:
                continue
            for gem in group.get("gems", []):
                if gem.get("index") == probe["index"]:
                    state: dict[str, Any] = {}
                    for f in probe["fields"]:
                        if f == "mainActiveSkill":
                            state[f] = int(group.get("mainActiveSkill") or 1)
                        else:
                            state[f] = bool(gem.get(f, f == "enabled"))
                    return state
        return None
    raise ValueError(f"unknown probe kind {kind!r}")


def _probe_state(worker: BridgeWorker, probe: dict[str, Any]) -> Any:
    """Read back from PoB the live state a record's restore/revert is meant to set."""
    method = _probe_rpc_method(probe)
    return _extract_probe_state(probe, worker.call(method, {}))


def _same_state(a: Any, b: Any) -> bool:
    """Compare a read-back value with the expected one (PoB stores unset checks as nil)."""
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a == b
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return bool(a) == bool(b)
    try:
        fa = float(a)
        fb = float(b)
    except (TypeError, ValueError):
        return a == b
    if math.isnan(fa) or math.isnan(fb):
        return math.isnan(fa) and math.isnan(fb)
    if math.isinf(fa) or math.isinf(fb):
        return fa == fb
    return abs(fa - fb) <= 1e-9 * max(1.0, abs(fa))


def _metric_val(raw: Any) -> float | str | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    parsed = _parse_numeric_or_none(raw)
    if parsed is None or math.isnan(parsed):
        return None
    if math.isinf(parsed):
        return _json_safe_stat(parsed)
    return parsed


def _digest_metrics(digest: dict[str, Any], dps_key: str) -> dict[str, float | str | None]:
    stats = digest.get("stats", {}) if isinstance(digest, dict) else {}
    return {
        "dps": _metric_val(stats.get(dps_key)),
        "totalEhp": _metric_val(stats.get("TotalEHP")),
        "physMaxHit": _metric_val(stats.get("PhysicalMaximumHitTaken")),
        "eleMaxHit": _safe_min_ele_max_hit(stats),
    }


def _parse_stat_for_compare(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, str) and not val.strip():
        return None
    return _parse_numeric_or_none(val)


def verify_restored(
    worker: BridgeWorker,
    before_code: str,
    before_digest: dict[str, Any],
) -> dict[str, Any]:
    """Check that rolling back to ``before_code`` really reproduced the original build.

    Mirrors pob-mcp's batch_modify rollback check: the restored build must export the
    same code, or at least recalculate to the same stats. Anything else is reported,
    because every later command would otherwise run on a silently modified build.
    """
    after_code = worker.call("export_code", {}).get("code")
    if before_code and after_code == before_code:
        return {"verified": True, "detail": "restored build exports identical code"}
    after = capture_digest(worker).get("stats") or {}
    before = (before_digest.get("stats") or {}) if isinstance(before_digest, dict) else {}
    differing: list[str] = []
    for k in set(before.keys()) | set(after.keys()):
        b = _parse_stat_for_compare(before.get(k))
        a = _parse_stat_for_compare(after.get(k))
        if b is None and a is None:
            continue
        if b is None or a is None:
            differing.append(k)
            continue
        if math.isnan(b) or math.isnan(a):
            if not (math.isnan(b) and math.isnan(a)):
                differing.append(k)
            continue
        if math.isinf(b) or math.isinf(a):
            if a != b:
                differing.append(k)
            continue
        if abs(a - b) > 1e-6 * max(1.0, abs(b)):
            differing.append(k)
    if differing:
        differing.sort()
        return {
            "verified": False,
            "detail": f"{len(differing)} stat(s) differ from before the run (e.g. {', '.join(differing[:5])})",
            "differing": differing,
        }
    return {"verified": True, "detail": "export code differs, but every stat recalculates identical"}


def _rpc_error_text(res: Any) -> str | None:
    if not isinstance(res, dict):
        return None
    if res.get("ok") is False or res.get("_error"):
        return str(res.get("error") or res.get("_error") or "RPC failed")
    return None


def attribute_water(
    worker: BridgeWorker,
    records: list[dict[str, Any]],
    dewatered_digest: dict[str, Any],
    dps_key: str,
) -> list[dict[str, Any]]:
    """Measure each stripped item's real PoB contribution, one at a time (pipelined per item).

    Starting from the de-watered state, every record is restored alone, verified by
    read-back, recalculated by PoB (`get_stats`), and reverted again with read-back
    verification. Deltas are relative to the de-watered baseline, so they answer
    "how much does keeping only this item add?". No value is estimated.
    """
    if not records:
        return []

    base = _digest_metrics(dewatered_digest, dps_key)

    # Pre-validate probe kinds and batch unique read RPCs ONCE to capture all `before` states
    needed_probe_rpcs: list[str] = []
    rec_probe_rpcs: list[str | None] = []
    rec_probe_errors: list[str | None] = []
    for rec in records:
        probe = rec.get("probe")
        if not probe:
            rec_probe_rpcs.append(None)
            rec_probe_errors.append(None)
            continue
        try:
            rpc_name = _probe_rpc_method(probe)
        except Exception as exc:  # noqa: BLE001
            rec_probe_rpcs.append(None)
            rec_probe_errors.append(f"cannot read state before restore: {exc}")
            continue
        rec_probe_rpcs.append(rpc_name)
        rec_probe_errors.append(None)
        if rpc_name not in needed_probe_rpcs:
            needed_probe_rpcs.append(rpc_name)

    pre_probe_results: dict[str, dict[str, Any]] = {}
    pre_probe_batch_err: str | None = None
    if needed_probe_rpcs:
        try:
            raw_pre = _call_batch(
                worker,
                [(m, {}) for m in needed_probe_rpcs],
                raise_on_error=False,
            )
            for m, r in zip(needed_probe_rpcs, raw_pre):
                pre_probe_results[m] = r
        except Exception as exc:  # noqa: BLE001
            pre_probe_batch_err = str(exc)

    rows: list[dict[str, Any]] = []
    broken_after: str | None = None

    for idx, rec in enumerate(records):
        row: dict[str, Any] = {
            "dimension": rec["dimension"],
            "key": rec["key"],
            "label": rec["label"],
        }
        if broken_after is not None:
            # The de-watered baseline is no longer trustworthy: never measure on it.
            row["error"] = f"skipped: baseline not restored after {broken_after}"
            rows.append(row)
            continue

        probe = rec.get("probe")
        probe_rpc = rec_probe_rpcs[idx]
        if rec_probe_errors[idx] is not None:
            row["error"] = rec_probe_errors[idx]
            rows.append(row)
            continue

        before: Any = None
        if probe and probe_rpc is not None:
            if pre_probe_batch_err is not None:
                row["error"] = f"cannot read state before restore: {pre_probe_batch_err}"
                rows.append(row)
                continue
            pre_res = pre_probe_results.get(probe_rpc)
            pre_err = _rpc_error_text(pre_res)
            if pre_err is not None:
                row["error"] = f"cannot read state before restore: {pre_err}"
                rows.append(row)
                continue
            try:
                before = _extract_probe_state(probe, pre_res or {})
            except Exception as exc:  # noqa: BLE001
                row["error"] = f"cannot read state before restore: {exc}"
                rows.append(row)
                continue
            if probe.get("kind") == "config" and _same_state(before, rec.get("original")):
                # reset_config left the var at its PoB defaultState (no-op); skip phantom 0-delta row.
                continue

        restore_calls = list(rec.get("restore") or [])
        revert_calls = list(rec.get("revert") or [])

        batch_calls: list[tuple[str, dict[str, Any]]] = []
        restore_start = len(batch_calls)
        batch_calls.extend(restore_calls)
        restore_end = len(batch_calls)

        probe_after_idx: int | None = None
        if probe and probe_rpc is not None:
            probe_after_idx = len(batch_calls)
            batch_calls.append((probe_rpc, {}))

        stats_idx = len(batch_calls)
        batch_calls.append(("get_stats", {}))

        revert_start = len(batch_calls)
        batch_calls.extend(revert_calls)
        revert_end = len(batch_calls)

        probe_back_idx: int | None = None
        if probe and probe_rpc is not None:
            probe_back_idx = len(batch_calls)
            batch_calls.append((probe_rpc, {}))

        try:
            batch_res = _call_batch(worker, batch_calls, raise_on_error=False)
        except Exception as exc:  # noqa: BLE001
            row["error"] = str(exc)
            broken_after = rec["key"]
            rows.append(row)
            continue

        if len(batch_res) < len(batch_calls):
            row["error"] = (
                f"incomplete batch response ({len(batch_res)}/{len(batch_calls)} RPCs)"
            )
            broken_after = rec["key"]
            rows.append(row)
            continue

        # 1. Verify restore RPCs + post-restore read-back before trusting get_stats
        restore_err: str | None = None
        for r_i in range(restore_start, restore_end):
            err_txt = _rpc_error_text(batch_res[r_i])
            if err_txt is not None:
                restore_err = err_txt
                break

        if restore_err is None and probe and probe_after_idx is not None:
            p_err = _rpc_error_text(batch_res[probe_after_idx])
            if p_err is not None:
                restore_err = p_err
            else:
                try:
                    got = _extract_probe_state(probe, batch_res[probe_after_idx])
                    if not _same_state(got, rec.get("original")):
                        restore_err = (
                            f"restore not applied: expected {rec.get('original')!r}, PoB reads {got!r}"
                        )
                except Exception as exc:  # noqa: BLE001
                    restore_err = str(exc)

        if restore_err is None:
            stats_res = batch_res[stats_idx]
            s_err = _rpc_error_text(stats_res)
            if s_err is not None:
                restore_err = s_err
            else:
                m = _digest_metrics({"stats": stats_res.get("stats", {})}, dps_key)
                for k in ("dps", "totalEhp", "physMaxHit", "eleMaxHit"):
                    mv = m[k]
                    bv = base[k]
                    row[k] = mv
                    if (
                        isinstance(mv, (int, float))
                        and isinstance(bv, (int, float))
                        and math.isfinite(mv)
                        and math.isfinite(bv)
                    ):
                        delta = mv - bv
                        row[f"{k}Delta"] = delta
                        row[f"{k}Pct"] = (delta / abs(bv) * 100.0) if abs(bv) > 0.0 else None
                    else:
                        row[f"{k}Delta"] = None
                        row[f"{k}Pct"] = None
        else:
            row["error"] = restore_err

        # 2. Always verify revert RPCs + post-revert read-back; halt remaining items if baseline is dirty
        revert_err: str | None = None
        for r_i in range(revert_start, revert_end):
            err_txt = _rpc_error_text(batch_res[r_i])
            if err_txt is not None:
                revert_err = err_txt
                break

        if revert_err is None and probe and probe_back_idx is not None:
            pb_err = _rpc_error_text(batch_res[probe_back_idx])
            if pb_err is not None:
                revert_err = pb_err
            else:
                try:
                    back = _extract_probe_state(probe, batch_res[probe_back_idx])
                    if not _same_state(back, before):
                        revert_err = f"revert not applied: expected {before!r}, PoB reads {back!r}"
                except Exception as exc:  # noqa: BLE001
                    revert_err = str(exc)

        if revert_err is not None:
            broken_after = rec["key"]
            prev = row.get("error")
            row["error"] = f"{prev}; {revert_err}" if prev else revert_err

        rows.append(row)

    return rows


def audit_build(
    worker: BridgeWorker,
    keep: Iterable[str] | str | None = None,
    attribution: bool = True,
) -> dict[str, Any]:
    """Run static build health checks + non-destructive 4-part PoB inflation ('water') audit.

    ``keep`` lists inflation items the user asserts are real for this build (e.g. an
    ascendancy that needs ``conditionStationary``); they are left untouched. With
    ``attribution`` every stripped item is restored alone and re-measured by PoB.
    """
    with worker.lock:
        prev_last_good = getattr(worker, "last_good_code", None)
        try:
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
            life_val = _finite_float(stats.get("Life"))
            chaos_mh_raw = _parse_numeric_or_none(stats.get("ChaosMaximumHitTaken"))
            is_chaos_immune = (life_val <= 1.0 and chaos_mh_raw is not None and math.isinf(chaos_mh_raw)) or chaos_res >= 100
            if chaos_res < 0 and not is_chaos_immune:
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

            # 8. Build mechanic & PoB-modeling pitfall checks (uses already-supported RPCs cleanly)
            try:
                skills_res_audit, cfg_res_audit = _call_batch(
                    worker,
                    [("list_skills", {}), ("list_config", {})],
                )
            except Exception:
                skills_res_audit, cfg_res_audit = None, None
            mech_gaps, _ = _detect_build_mechanics_and_pitfalls(
                stats=stats,
                skills_res=skills_res_audit,
                items_res=items_res,
                cfg_res=cfg_res_audit,
            )
            health_issues.extend(mech_gaps)

            # Run non-destructive de-watering snapshot
            try:
                stripped, records = dewater_build_with_records(worker, keep=keep)
                digest_dewatered = capture_digest(worker)
                dewater_diff = diff_digest(digest_before, digest_dewatered)
                water_attribution = (
                    attribute_water(worker, records, digest_dewatered, digest_before["primaryDpsKey"])
                    if attribution
                    else []
                )
            finally:
                worker.call("import_code", {"code": snap_code, "mode": "replace"})
            restore_check = verify_restored(worker, snap_code, digest_before)
        finally:
            if hasattr(worker, "last_good_code"):
                worker.last_good_code = prev_last_good

        dew_stats = digest_dewatered["stats"]
        orig_ele = _safe_min_ele_max_hit(stats)
        dew_ele = _safe_min_ele_max_hit(dew_stats)
        return {
            "healthIssues": health_issues,
            "inflationStripped": stripped,
            "waterAttribution": water_attribution,
            "restoreCheck": restore_check,
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
    """Resolve user slot query (including '#1', '#2' jewel socket shorthand, 3.30 graft slots Head/Body/Arm/Leg, EN/ZH aliases, or Flask 1..5) to exact PoB slotName."""
    from .guardrails import GuardrailScope, check_input, normalize_equipment_slot

    raw_q = slot_query.strip()
    check_input(raw_q, scope=GuardrailScope.SLOT, expected_game="poe1")
    if items_res is None:
        items_res = worker.call("list_items", {})
    slots = items_res.get("slots", [])

    # Preserve exact PoE1 3.30 Wickerman Graft slot names (`Head`, `Body`, `Arm`, `Leg`)
    # when explicitly capitalized and present/equipped on this build, avoiding collision with
    # lowercase `body` -> `Body Armour` and `head` -> `Helmet` aliases.
    if raw_q in ("Head", "Body", "Arm", "Leg"):
        for s in slots:
            if s.get("name") == raw_q and (s.get("selItemId") or s.get("shown")):
                return s["name"]

    q = normalize_equipment_slot(raw_q) or raw_q

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
    **_POB_TRUTHY_CONFIG_DEFAULTS,
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
    if search_clean:
        from .guardrails import GuardrailScope, check_input

        check_input(search_clean, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")
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
                if default_val is True:
                    is_default = val is True
                else:
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
    if effective_slot:
        from .guardrails import GuardrailScope, check_input

        check_input(effective_slot, scope=GuardrailScope.SLOT, expected_game="poe1")
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
    if group_selector is None or str(group_selector).strip().lower() in ("", "main"):
        raw_main = skills_res.get("mainSocketGroup")
        try:
            main_idx = int(raw_main) if raw_main is not None else 1
        except (TypeError, ValueError):
            main_idx = 1
        if any(g.get("index") == main_idx for g in groups):
            return main_idx
        return int(groups[0]["index"])
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
            group = next((g for g in skills_res.get("groups", []) if g.get("index") == idx), None)
            if group is None:
                raise ValueError(f"Skill group #{idx} not found.")
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
            if meta.get("asc") or meta.get("ascendancyName"):
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
    else:
        from .guardrails import GuardrailScope, check_input

        for kw in kw_list:
            if kw.strip():
                check_input(kw, scope=GuardrailScope.CALC_QUERY, expected_game="poe1")

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


_TRIGGER_ATTACK_HINTS = (
    "cyclone",
    "lancing steel",
    "blast rain",
    "barrage",
    "kinetic blast",
    "power siphon",
    "frenzy",
    "rain of arrows",
    "split arrow",
    "spectral throw",
    "blade flurry",
    "flicker strike",
    "smite",
    "lightning strike",
    "molten strike",
    "frost blades",
    "double strike",
    "reave",
    "static strike",
    "shield charge",
    "whirling blades",
    "leap slam",
    "consecrated path",
    "tornado shot",
    "ice shot",
    "lightning arrow",
    "cobra lash",
    "venom gyre",
    "splitting steel",
    "shattering steel",
)


def _extract_trigger_attack_aps(
    worker: BridgeWorker,
    skills_res: dict[str, Any],
    items_res: dict[str, Any],
    base_stats: dict[str, Any],
) -> float | None:
    """Measure the real PoB Attack Rate (`Speed`) of the trigger attack in CoC/Cospri/Mjolner/Poet's Pen setups."""
    groups = skills_res.get("groups") or []
    main_idx = skills_res.get("mainSocketGroup")
    main_group = next(
        (g for g in groups if g.get("isMain") or (main_idx is not None and g.get("index") == main_idx)),
        None,
    )
    if not main_group or not main_group.get("enabled", True):
        return None

    mg_gems = [gm for gm in (main_group.get("gems") or []) if gm.get("enabled", True)]
    has_coc = any(
        "cast on critical strike" in strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).lower()
        or str(gm.get("skillId") or "") in ("SupportCastOnCriticalStrike", "SupportAwakenedCastOnCriticalStrike")
        for gm in mg_gems
    )
    items_by_id = {it["id"]: it for it in (items_res.get("items") or [])}
    has_atk_trigger_item = False
    for s in items_res.get("slots") or []:
        if s.get("selItemId") and not s.get("inactive") and "Swap" not in str(s.get("name") or ""):
            it = items_by_id.get(s["selItemId"], {})
            nm = strip_pob_escapes(str(it.get("title") or it.get("name") or "")).lower()
            if any(k in nm for k in ("cospri's malice", "mjölner", "mjolner", "the poet's pen")):
                has_atk_trigger_item = True
                break
    if not has_coc and not has_atk_trigger_item:
        return None

    # Check if main_group has a `skills` list where we can identify the trigger attack skill
    skills_list = main_group.get("skills") or []
    orig_active = int(main_group.get("mainActiveSkill") or 1)
    if skills_list and 1 <= orig_active <= len(skills_list):
        cur_sk = skills_list[orig_active - 1]
        cur_nm = strip_pob_escapes(str(cur_sk.get("name") or cur_sk.get("skillId") or "")).lower()
        if any(h in cur_nm for h in _TRIGGER_ATTACK_HINTS):
            sp = _finite_float(base_stats.get("Speed"), default=0.0)
            return sp if sp > 0 else None
        # Look for trigger attack in the same skill group
        for idx_0, sk in enumerate(skills_list):
            sk_nm = strip_pob_escapes(str(sk.get("name") or sk.get("skillId") or "")).lower()
            if any(h in sk_nm for h in _TRIGGER_ATTACK_HINTS):
                atk_idx = idx_0 + 1
                g_idx = int(main_group["index"])
                try:
                    with worker.lock:
                        res_list = _call_batch(
                            worker,
                            [
                                ("set_group", {"index": g_idx, "mainActiveSkill": atk_idx}),
                                ("get_stats", {}),
                                ("set_group", {"index": g_idx, "mainActiveSkill": orig_active}),
                            ],
                            raise_on_error=False,
                        )
                    if len(res_list) >= 2 and isinstance(res_list[1], dict) and isinstance(res_list[1].get("stats"), dict):
                        sp = _finite_float(res_list[1]["stats"].get("Speed"), default=0.0)
                        if sp > 0:
                            return sp
                except Exception:
                    try:
                        worker.call("set_group", {"index": g_idx, "mainActiveSkill": orig_active})
                    except Exception:
                        pass
                break

    # Fallback: for Cospri/Mjolner/Poet's Pen where attack is in a separate skill group
    if has_atk_trigger_item:
        orig_main_idx = int(main_group["index"])
        for g in groups:
            if not g.get("enabled", True) or "Swap" in str(g.get("slot") or "") or int(g.get("index") or 0) == orig_main_idx:
                continue
            for gm in g.get("gems") or []:
                if gm.get("enabled", True) and not gm.get("support"):
                    gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).lower()
                    if any(h in gnm for h in _TRIGGER_ATTACK_HINTS):
                        try:
                            with worker.lock:
                                res_list = _call_batch(
                                    worker,
                                    [
                                        ("set_build_field", {"field": "mainSocketGroup", "value": int(g["index"])}),
                                        ("get_stats", {}),
                                        ("set_build_field", {"field": "mainSocketGroup", "value": orig_main_idx}),
                                    ],
                                    raise_on_error=False,
                                )
                            if len(res_list) >= 2 and isinstance(res_list[1], dict) and isinstance(res_list[1].get("stats"), dict):
                                sp = _finite_float(res_list[1]["stats"].get("Speed"), default=0.0)
                                if sp > 0:
                                    return sp
                        except Exception:
                            try:
                                worker.call("set_build_field", {"field": "mainSocketGroup", "value": orig_main_idx})
                            except Exception:
                                pass
                        break
    return None


def _detect_dead_main_supports(
    worker: BridgeWorker,
    main_group: dict[str, Any] | None,
    base_stats: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Engine-verified 0% dead support detector on `mainGroup` via one batched toggle+restore pass."""
    if not main_group or not main_group.get("enabled", True):
        return [], []
    mg_idx = main_group.get("index")
    if mg_idx is None:
        return [], []

    base_comb = _finite_float(base_stats.get("CombinedDPS"), default=0.0)
    base_full = _finite_float(base_stats.get("FullDPS"), default=0.0)
    base_tot = _finite_float(base_stats.get("TotalDPS"), default=0.0)
    base_ehp = _finite_float(base_stats.get("TotalEHP"), default=0.0)
    base_mana_cost = _finite_float(base_stats.get("ManaCost"), default=-1.0)
    if base_comb <= 0.0 and base_full <= 0.0 and base_tot <= 0.0:
        return [], []

    candidates: list[dict[str, Any]] = []
    for gm in main_group.get("gems") or []:
        if not gm.get("enabled", True) or not gm.get("support"):
            continue
        if gm.get("fromItem") or gm.get("fromNode"):
            continue
        if gm.get("index") is None:
            continue
        gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip()
        norm = _normalize_support_base_name(gnm)
        if not norm or norm in _DEAD_SUPPORT_EXEMPT_NAMES:
            continue
        # Exempt Inspiration ONLY when ManaCost > 0 (0-mana Inspiration is already flagged as HIGH gap!)
        if norm == "inspiration" and base_mana_cost > 0.0:
            continue
        candidates.append(gm)

    if not candidates:
        return [], []

    batch_calls: list[tuple[str, dict[str, Any]]] = []
    for gm in candidates:
        g_ref = {"group": int(mg_idx), "index": int(gm["index"])}
        batch_calls.append(("set_gem", {**g_ref, "enabled": False}))
        batch_calls.append(("get_stats", {}))
        batch_calls.append(("set_gem", {**g_ref, "enabled": True}))

    try:
        with worker.lock:
            responses = _call_batch(worker, batch_calls, raise_on_error=False)
    except Exception:
        # Best-effort restore all candidate supports
        for gm in candidates:
            try:
                worker.call("set_gem", {"group": int(mg_idx), "index": int(gm["index"]), "enabled": True})
            except Exception:
                pass
        return [], []

    def _unchanged(a: float, b: float) -> bool:
        return abs(a - b) <= 1e-5 * max(1.0, abs(b))

    dead_gaps: list[dict[str, str]] = []
    dead_records: list[dict[str, Any]] = []
    for i, gm in enumerate(candidates):
        res_off = responses[i * 3] if (i * 3) < len(responses) else None
        res_stats = responses[i * 3 + 1] if (i * 3 + 1) < len(responses) else None
        if not isinstance(res_off, dict) or res_off.get("_error") or res_off.get("ok") is False:
            continue
        if not isinstance(res_stats, dict) or res_stats.get("_error") or not isinstance(res_stats.get("stats"), dict):
            continue
        st_off = res_stats["stats"]
        off_comb = _finite_float(st_off.get("CombinedDPS"), default=base_comb)
        off_full = _finite_float(st_off.get("FullDPS"), default=base_full)
        off_tot = _finite_float(st_off.get("TotalDPS"), default=base_tot)
        off_ehp = _finite_float(st_off.get("TotalEHP"), default=base_ehp)

        if (
            _unchanged(off_comb, base_comb)
            and _unchanged(off_full, base_full)
            and _unchanged(off_tot, base_tot)
            and _unchanged(off_ehp, base_ehp)
        ):
            gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip()
            gnm_zh = strip_pob_escapes(str(gm.get("nameZh") or "")).strip()
            disp = f"{gnm_zh} ({gnm})" if (gnm_zh and gnm_zh != gnm) else gnm
            norm = _normalize_support_base_name(gnm)
            hint = ""
            if norm == "hypothermia":
                hint = " (requires Enemy Chilled — blocked by Elemental Focus or missing Chill source)"
            elif norm == "immolate":
                hint = " (requires Enemy Burning/Ignited)"
            elif norm == "inspiration":
                hint = " (0-mana group generates 0 Inspiration Charges)"
            dead_records.append(
                {
                    "groupIndex": int(mg_idx),
                    "gemIndex": int(gm["index"]),
                    "name": gnm,
                    "nameZh": gnm_zh or None,
                    "deltaCombinedDpsPct": 0.0,
                    "deltaFullDpsPct": 0.0,
                    "deltaTotalEhpPct": 0.0,
                }
            )
            dead_gaps.append(
                {
                    "category": "DeadSupport",
                    "severity": "HIGH",
                    "message": (
                        f"Engine-Verified 0% Dead Support in Group #{mg_idx}: '{disp}' contributes 0.0% "
                        f"CombinedDPS, 0.0% FullDPS, and 0.0% TotalEHP when toggled off{hint} — replace with an active damage support!"
                    ),
                }
            )

    return dead_gaps, dead_records


def diagnose_build(
    worker: BridgeWorker,
    *,
    keep: Iterable[str] | str | None = None,
    top: int = 5,
    no_rank: bool = False,
    ask: str | None = None,
    pob_dir: Any = None,
    runtime_dir: Any = None,
    target: str = "main",
) -> dict[str, Any]:
    """One-shot comprehensive build diagnosis for fast/weak LLM agents (`pob diagnose`).

    Combines in ONE call:
      1. Environment grounding metadata (`game`, `patch`, `pobVersion`, `dataVersion`, `treeVersion`, `slot`)
      2. Build identity + main skill group (`extract_summary`)
      3. Original vs Pinnacle Dewatered DPS / EHP / MaxHit + per-item water attribution (`audit_build`)
      4. Hard defensive, dead-support, trigger-overspeed, & mechanical gaps
      5. Live 3.30 Fact Cards for main skill + support gems + equipped Unique items (`game_data.sqlite`)
         + optional player question grounding (`--ask`) + triggered PoE1 patch/false-friend advisories
      6. Top N ROI passive nodes (`rank_nodes`) & candidate support gem swaps (`rank_gems`), unless `no_rank=True`.
    """
    from .comparator import rank_gems, rank_nodes
    from .game_data import ground_text_entities, lookup_fact_cards_for_build
    from .guardrails import scan_text_traps

    top = max(1, int(top))
    summary = extract_summary(worker)
    audit = audit_build(worker, keep=keep, attribution=True)
    items_info = inspect_items(worker, all_raw=True)
    with worker.lock:
        all_skills_res, cfg_res_diag, items_raw_res, tree_state_diag = _call_batch(
            worker,
            [
                ("list_skills", {}),
                ("list_config", {}),
                ("list_items", {}),
                ("get_tree_state", {}),
            ],
            raise_on_error=False,
        )
    if not isinstance(all_skills_res, dict) or all_skills_res.get("_error"):
        all_skills_res = {"groups": [summary["mainGroup"]] if summary.get("mainGroup") else []}
    if not isinstance(cfg_res_diag, dict) or cfg_res_diag.get("_error"):
        cfg_res_diag = {}
    if not isinstance(items_raw_res, dict) or items_raw_res.get("_error"):
        items_raw_res = {"items": [], "slots": []}

    info = summary.get("buildInfo") or {}
    pts = info.get("points") or {}
    used_pts = int(pts.get("used") or 0)
    used_max = int(pts.get("usedMax") or 0)

    # Copy healthIssues and add unallocated regular passive points notice if applicable
    gaps = list(audit.get("healthIssues") or [])
    if used_max > 0 and used_pts < used_max:
        gaps.append(
            {
                "category": "Points",
                "severity": "LOW",
                "message": f"Unallocated passive points: {used_pts}/{used_max} ({used_max - used_pts} remaining)",
            }
        )

    # Extract main skill + support gem names and equipped Unique names for live 3.30 Fact Cards
    mg = summary.get("mainGroup") or {}
    main_gems_raw = [
        strip_pob_escapes(g.get("name") or g.get("nameSpec") or "")
        for g in (mg.get("gems") or [])
        if g.get("enabled", True) and (g.get("name") or g.get("nameSpec"))
    ]
    unique_names: list[str] = []
    equipped_uniques_meta: list[dict[str, Any]] = []
    equipped_items_compact: list[dict[str, Any]] = []
    for s in items_info.get("equippedSlots") or []:
        it = s.get("item")
        if not isinstance(it, dict):
            continue
        uname = strip_pob_escapes(it.get("title") or it.get("name") or "")
        uname_zh = strip_pob_escapes(it.get("titleZh") or it.get("nameZh") or "") or None
        base_nm = strip_pob_escapes(it.get("baseName") or "") or None
        rarity = str(it.get("rarity") or "").upper()
        slot_nm = str(s.get("slotName") or "")
        if "Swap" not in slot_nm:
            eq_entry: dict[str, Any] = {
                "slot": slot_nm,
                "name": uname,
                "nameZh": uname_zh,
                "base": base_nm,
                "rarity": rarity or "-",
                "type": it.get("type") or "-",
            }
            if s.get("isFlask"):
                eq_entry["isFlask"] = True
                eq_entry["active"] = bool(s.get("active"))
                if _is_tincture_item(it):
                    eq_entry["isTincture"] = True
            if s.get("nodeId") is not None:
                eq_entry["nodeId"] = s.get("nodeId")
                eq_entry["socketIndex"] = s.get("socketIndex")
            equipped_items_compact.append(eq_entry)
        if rarity in ("UNIQUE", "RELIC") and uname and "Swap" not in slot_nm:
            unique_names.append(uname)
            equipped_uniques_meta.append(
                {
                    "slot": slot_nm,
                    "name": uname,
                    "nameZh": uname_zh,
                    "base": base_nm,
                }
            )

    fact_cards: dict[str, Any] = {"pobVersion": "unknown", "dataVersion": "unknown", "gems": [], "uniques": []}
    try:
        fact_cards = lookup_fact_cards_for_build(
            main_gems_raw,
            unique_names,
            pob_dir=pob_dir,
            runtime_dir=runtime_dir,
        )
    except Exception:
        pass

    # Scan active gems, uniques, ascendancy/bloodline nodes, and allocated keystones
    tree_summary: dict[str, Any] = {}
    try:
        tree_summary = inspect_tree(worker)
    except Exception:
        tree_summary = {}

    # Collect stats of ALL allocated passive/ascendancy/bloodline nodes for exact global CDR & mechanic detection
    alloc_node_stats: list[tuple[str, list[str]]] = []
    if isinstance(tree_state_diag, dict) and not tree_state_diag.get("_error"):
        try:
            tv_diag = tree_state_diag.get("treeVersion")
            nodes_db_diag = worker.get_cached_tree_data(tv_diag).get("nodes", {})
            ov_diag = tree_state_diag.get("overrides") or {}
            dyn_diag = {str(n["id"]): n for n in (tree_state_diag.get("dynamicNodes") or []) if isinstance(n, dict) and "id" in n}
            for raw_nid in tree_state_diag.get("allocatedNodes") or []:
                sid = str(int(raw_nid))
                nd = dict(nodes_db_diag.get(sid) or {})
                if sid in dyn_diag:
                    nd.update(dyn_diag[sid])
                if sid in ov_diag and isinstance(ov_diag[sid], dict):
                    if ov_diag[sid].get("stats"):
                        nd["stats"] = ov_diag[sid]["stats"]
                    if ov_diag[sid].get("name"):
                        nd["name"] = ov_diag[sid]["name"]
                st_lines = [str(x) for x in (nd.get("stats") or []) if str(x).strip()]
                if st_lines:
                    lbl = strip_pob_escapes(str(nd.get("name") or sid))
                    alloc_node_stats.append((lbl, st_lines))
        except Exception:
            pass

    # Allocated Ascendancy & Bloodline nodes WITH stats (includes type="Normal" nodes with asc/ascendancyName!)
    ascendancy_nodes_compact: list[dict[str, Any]] = []
    for an in tree_summary.get("ascendancyNodes") or []:
        if not isinstance(an, dict):
            continue
        an_type = str(an.get("type") or "Normal")
        if an_type in ("AscendClassStart", "ClassStart"):
            continue
        an_name = strip_pob_escapes(str(an.get("name") or "")).strip()
        an_name_zh = strip_pob_escapes(str(an.get("nameZh") or "")).strip() or None
        an_stats = [strip_pob_escapes(str(x)).strip() for x in (an.get("stats") or []) if str(x).strip()]
        an_stats_zh = [strip_pob_escapes(str(x)).strip() for x in (an.get("statsZh") or []) if str(x).strip()]
        if an_name == "Passive Point" and an_stats == ["Grants 1 Passive Skill Point"]:
            continue
        if not an_stats and not an_stats_zh and an_type != "Notable":
            continue
        if not an_stats and not an_stats_zh:
            continue
        ascendancy_nodes_compact.append(
            {
                "id": an.get("id"),
                "name": an_name,
                "nameZh": an_name_zh,
                "asc": an.get("asc") or an.get("ascendancyName"),
                "type": an_type,
                "stats": an_stats,
                "statsZh": an_stats_zh or None,
            }
        )

    # Compact All Skill Groups overview (including disabled gems, swap groups, counts)
    skill_groups_compact: list[dict[str, Any]] = []
    all_active_skill_names: list[str] = []
    for g in all_skills_res.get("groups") or []:
        if not isinstance(g, dict):
            continue
        g_gems_compact: list[dict[str, Any]] = []
        for gm in g.get("gems") or []:
            if not isinstance(gm, dict):
                continue
            gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip()
            gnm_zh = strip_pob_escapes(str(gm.get("nameZh") or "")).strip() or None
            g_en = bool(gm.get("enabled", True))
            if g_en and not gm.get("support") and gnm:
                all_active_skill_names.append(gnm)
            g_entry: dict[str, Any] = {
                "name": gnm,
                "nameZh": gnm_zh,
                "level": gm.get("level"),
                "quality": gm.get("quality"),
                "enabled": g_en,
                "support": bool(gm.get("support")),
            }
            if gm.get("fromItem"):
                g_entry["fromItem"] = True
            cnt = int(gm.get("count") or 1)
            if cnt > 1:
                g_entry["count"] = cnt
            g_gems_compact.append(g_entry)
        skill_groups_compact.append(
            {
                "index": g.get("index"),
                "isMain": bool(g.get("isMain")),
                "slot": g.get("slot") or "-",
                "enabled": bool(g.get("enabled", True)),
                "includeInFullDPS": bool(g.get("includeInFullDPS")),
                "source": bool(g.get("source")),
                "gems": g_gems_compact,
            }
        )

    # Run full build mechanics & pitfall detector (with tree_summary + equipped raw items + all alloc stats + trigger APS!)
    stats_full: dict[str, Any] = {}
    stats_full.update(summary.get("offence") or {})
    stats_full.update(summary.get("defence") or {})
    try:
        raw_stats_res = worker.call("get_stats", {})
        if isinstance(raw_stats_res, dict) and isinstance(raw_stats_res.get("stats"), dict):
            stats_full.update(raw_stats_res["stats"])
    except Exception:
        pass

    trig_aps = _extract_trigger_attack_aps(worker, all_skills_res, items_raw_res, stats_full)
    diag_mech_gaps, build_mechanics = _detect_build_mechanics_and_pitfalls(
        stats=stats_full,
        skills_res=all_skills_res,
        items_res=items_raw_res,
        cfg_res=cfg_res_diag,
        tree_summary=tree_summary,
        equipped_slots_raw=items_info.get("equippedSlots") or [],
        alloc_node_stats=alloc_node_stats,
        trigger_attack_aps=trig_aps,
    )
    dead_sup_gaps, dead_sup_records = _detect_dead_main_supports(worker, mg, stats_full)
    if dead_sup_records:
        build_mechanics["deadSupports"] = dead_sup_records

    existing_gap_msgs = {str(g.get("message") or "") for g in gaps}
    for mgap in (*diag_mech_gaps, *dead_sup_gaps):
        if mgap["message"] not in existing_gap_msgs:
            existing_gap_msgs.add(mgap["message"])
            gaps.append(mgap)

    keystone_names = [
        strip_pob_escapes(k.get("name") or "")
        for k in (tree_summary.get("keystones") or [])
        if isinstance(k, dict) and k.get("name")
    ]
    asc_class_token = strip_pob_escapes(str(info.get("ascendClassName") or ""))
    asc_node_names = [an["name"] for an in ascendancy_nodes_compact if an.get("name")]
    scan_corpus = " ; ".join(
        main_gems_raw
        + all_active_skill_names
        + unique_names
        + keystone_names
        + asc_node_names
        + ([asc_class_token] if asc_class_token else [])
    )
    trap_scan = scan_text_traps(scan_corpus)
    advisories_merged: list[dict[str, Any]] = []
    seen_adv_rules: set[str] = set()
    for w in trap_scan.get("warnings") or []:
        rid = str(w.get("ruleId") or w.get("trigger") or "")
        if rid and rid in seen_adv_rules:
            continue
        if rid:
            seen_adv_rules.add(rid)
        advisories_merged.append(w)

    # Optional pre-turn player question grounding (`--ask` / `-q`) merged & deduplicated into report
    ask_grounding: dict[str, Any] | None = None
    ask_clean = str(ask or "").strip()
    if ask_clean:
        try:
            g_ask = ground_text_entities(ask_clean, limit=8, pob_dir=pob_dir, runtime_dir=runtime_dir)
            for w in g_ask.get("warnings") or []:
                rid = str(w.get("ruleId") or w.get("trigger") or "")
                if rid and rid in seen_adv_rules:
                    continue
                if rid:
                    seen_adv_rules.add(rid)
                advisories_merged.append(w)
            gem_cards = list(fact_cards.get("gems") or [])
            uniq_cards = list(fact_cards.get("uniques") or [])
            seen_gem_names = {str(c.get("name") or "").lower() for c in gem_cards}
            seen_uniq_names = {str(c.get("name") or "").lower() for c in uniq_cards}
            added_entities: list[str] = []
            for ent in g_ask.get("entities") or []:
                enm = str(ent.get("name") or "")
                elow = enm.lower()
                if ent.get("kind") == "gem" and elow and elow not in seen_gem_names:
                    seen_gem_names.add(elow)
                    gem_cards.append(ent)
                    added_entities.append(enm)
                elif ent.get("kind") == "unique" and elow and elow not in seen_uniq_names:
                    seen_uniq_names.add(elow)
                    uniq_cards.append(ent)
                    added_entities.append(enm)
            fact_cards["gems"] = gem_cards
            fact_cards["uniques"] = uniq_cards
            ask_grounding = {
                "query": ask_clean,
                "resolvedSlang": g_ask.get("resolvedSlang") or [],
                "hasAlerts": bool(g_ask.get("hasAlerts")),
                "addedEntities": added_entities,
                "elapsedMs": g_ask.get("elapsedMs"),
            }
        except Exception:
            ask_grounding = {"query": ask_clean, "resolvedSlang": [], "hasAlerts": False, "addedEntities": []}

    # Rank top ROI water sources by absolute DPS or EHP impact
    raw_attr = [r for r in (audit.get("waterAttribution") or []) if not r.get("error")]
    top_water = sorted(
        raw_attr,
        key=lambda r: (abs(float(r.get("dpsPct") or 0.0)) + abs(float(r.get("totalEhpPct") or 0.0))),
        reverse=True,
    )[:6]

    top_nodes: dict[str, Any] | None = None
    top_gems: dict[str, Any] | None = None
    rank_errors: list[str] = []
    if not no_rank:
        primary_key = str((audit.get("original") or {}).get("primaryDpsKey") or "CombinedDPS")
        node_stat = "CombinedDPS" if primary_key in ("CombinedDPS", "TotalDPS", "TotalDotDPS", "FullDPS") else primary_key
        gem_field = "FullDPS" if primary_key == "FullDPS" else "CombinedDPS"
        try:
            top_nodes = rank_nodes(worker, stat=node_stat, max_dist=5, top=top)
        except Exception as exc:  # noqa: BLE001
            rank_errors.append(f"rank_nodes: {exc}")
        try:
            if mg and (mg.get("gems") or []):
                top_gems = rank_gems(
                    worker,
                    group_selector="main",
                    slot_idx=None,
                    add_link=True,
                    field=gem_field,
                    top=top,
                    exclude_archetype_swaps=True,
                )
        except Exception as exc:  # noqa: BLE001
            rank_errors.append(f"rank_gems: {exc}")

    hello = getattr(worker, "hello_info", None) or {}
    pob_ver = fact_cards.get("pobVersion")
    if not pob_ver or pob_ver == "unknown":
        pob_ver = hello.get("pobVersion") or "unknown"
    tree_ver = info.get("treeVersion") or "3_30"
    patch_str = str(tree_ver).replace("_", ".") if str(tree_ver).startswith("3_") else "3.30"

    worker_rev = getattr(worker, "rev", None)
    env: dict[str, Any] = {
        "game": "poe1",
        "patch": patch_str,
        "pobVersion": pob_ver,
        "dataVersion": fact_cards.get("dataVersion", "unknown"),
        "treeVersion": tree_ver,
        "slot": target,
    }
    if isinstance(worker_rev, int) and not isinstance(worker_rev, bool):
        env["rev"] = worker_rev

    out_diag: dict[str, Any] = {
        "env": env,
        "buildInfo": info,
        "mainGroup": mg,
        "keystones": keystone_names,
        "ascendancyNodes": ascendancy_nodes_compact,
        "skillGroups": skill_groups_compact,
        "equippedItems": equipped_items_compact,
        "buildMechanics": build_mechanics,
        "offence": summary.get("offence"),
        "minionOffence": summary.get("minionOffence"),
        "defence": summary.get("defence"),
        "original": audit.get("original"),
        "dewateredPinnacle": audit.get("dewateredPinnacle"),
        "inflationStripped": audit.get("inflationStripped"),
        "topWaterSources": top_water,
        "healthGaps": gaps,
        "factCards": {
            "gems": fact_cards.get("gems", []),
            "uniques": fact_cards.get("uniques", []),
            "equippedUniques": equipped_uniques_meta,
        },
        "advisories": advisories_merged,
        "topNodes": top_nodes,
        "topGems": top_gems,
        "rankErrors": rank_errors,
        "restoreCheck": audit.get("restoreCheck"),
    }
    if ask_grounding is not None:
        out_diag["askGrounding"] = ask_grounding
    return out_diag


def diagnose_compare_builds(
    main_worker: BridgeWorker,
    ref_worker: BridgeWorker,
    *,
    source: str | None = None,
    xml_code: str | None = None,
    compare_source: str | None = None,
    compare_xml_code: str | None = None,
    keep: Iterable[str] | str | None = None,
    top: int = 5,
    no_rank: bool = True,
    ask: str | None = None,
    pob_dir: Any = None,
    runtime_dir: Any = None,
    target: str = "main",
    ref_target: str = "ref",
) -> dict[str, Any]:
    """One-shot dual-build 3-baseline A/B comparison + concurrent Pinnacle diagnosis of both builds."""
    from .comparator import _run_worker_pair, compare_builds
    from .importers import load_build_into_worker

    # Phase 1: Load both builds concurrently if sources were provided
    def _load_main() -> None:
        if source or xml_code:
            load_build_into_worker(main_worker, source=source, xml_code=xml_code)
            if hasattr(main_worker, "bump_rev"):
                main_worker.bump_rev()
                if hasattr(main_worker, "save_slot_state"):
                    main_worker.save_slot_state()

    def _load_ref() -> None:
        if compare_source or compare_xml_code:
            load_build_into_worker(ref_worker, source=compare_source, xml_code=compare_xml_code)
            if hasattr(ref_worker, "bump_rev"):
                ref_worker.bump_rev()
                if hasattr(ref_worker, "save_slot_state"):
                    ref_worker.save_slot_state()

    _run_worker_pair(main_worker, ref_worker, _load_main, _load_ref)

    # Phase 2: 3-baseline comparison (already internally parallel across main & ref!)
    comparison = compare_builds(main_worker, ref_worker)

    # Phase 3: Concurrent Pinnacle diagnostics for both builds
    def _diag_a() -> dict[str, Any]:
        return diagnose_build(
            main_worker,
            keep=keep,
            top=top,
            no_rank=no_rank,
            ask=ask,
            pob_dir=pob_dir,
            runtime_dir=runtime_dir,
            target=target,
        )

    def _diag_b() -> dict[str, Any]:
        return diagnose_build(
            ref_worker,
            keep=keep,
            top=top,
            no_rank=no_rank,
            ask=None,
            pob_dir=pob_dir,
            runtime_dir=runtime_dir,
            target=ref_target,
        )

    diag_a, diag_b = _run_worker_pair(main_worker, ref_worker, _diag_a, _diag_b)
    return {
        "mode": "compare",
        "target": target,
        "refTarget": ref_target,
        "env": diag_a.get("env"),
        "comparison": comparison,
        "buildA": diag_a,
        "buildB": diag_b,
        "askGrounding": diag_a.get("askGrounding"),
    }
