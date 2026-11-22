"""PoE1 trade stat catalog loader, bilingual/slot-aware stat resolver, and query builder."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

from .importers import _get_cache_dir
from .trade_item import ELEMENTS, convert_template, match_harvest, normalize_template
from .updater import resolve_engine_root

# ---------------------------------------------------------------------------
# Slot -> PoE1 trade category & PoB equipment slot mappings
# ---------------------------------------------------------------------------

CATEGORY_BY_SLOT: dict[str, str | None] = {
    # Armour
    "helmet": "armour.helmet",
    "helm": "armour.helmet",
    "body_armour": "armour.chest",
    "body": "armour.chest",
    "chest": "armour.chest",
    "gloves": "armour.gloves",
    "boots": "armour.boots",
    "shield": "armour.shield",
    "quiver": "armour.quiver",
    # Accessory
    "amulet": "accessory.amulet",
    "belt": "accessory.belt",
    "ring": "accessory.ring",
    "ring1": "accessory.ring",
    "ring2": "accessory.ring",
    "ring3": "accessory.ring",
    # Weapon generic
    "weapon": "weapon",
    "weapon1": None,
    "weapon2": None,
    # Weapon subtypes
    "bow": "weapon.bow",
    "wand": "weapon.wand",
    "claw": "weapon.claw",
    "dagger": "weapon.dagger",
    "sceptre": "weapon.sceptre",
    "staff": "weapon.staff",
    "sword": "weapon.onesword",
    "one_handed_sword": "weapon.onesword",
    "two_handed_sword": "weapon.twosword",
    "axe": "weapon.oneaxe",
    "one_handed_axe": "weapon.oneaxe",
    "two_handed_axe": "weapon.twoaxe",
    "mace": "weapon.onemace",
    "one_handed_mace": "weapon.onemace",
    "two_handed_mace": "weapon.twomace",
    "one_hand": "weapon.one",
    "two_hand": "weapon.two",
    # Flask / Jewel
    "flask": "flask",
    "flask1": "flask",
    "flask2": "flask",
    "flask3": "flask",
    "flask4": "flask",
    "flask5": "flask",
    "jewel": "jewel",
    "abyss_jewel": "jewel.abyss",
    "cluster_jewel": "jewel.cluster",
}

# PoB slot names used by `pob items` / `pob item try`.
POB_SLOT_BY_SLOT: dict[str, str] = {
    "helmet": "Helmet",
    "helm": "Helmet",
    "body_armour": "Body Armour",
    "body": "Body Armour",
    "chest": "Body Armour",
    "gloves": "Gloves",
    "boots": "Boots",
    "shield": "Weapon 2",
    "quiver": "Weapon 2",
    "amulet": "Amulet",
    "belt": "Belt",
    "ring": "Ring 1",
    "ring1": "Ring 1",
    "ring2": "Ring 2",
    "ring3": "Ring 3",
    "weapon": "Weapon 1",
    "weapon1": "Weapon 1",
    "weapon2": "Weapon 2",
    "bow": "Weapon 1",
    "wand": "Weapon 1",
    "claw": "Weapon 1",
    "dagger": "Weapon 1",
    "sceptre": "Weapon 1",
    "staff": "Weapon 1",
    "sword": "Weapon 1",
    "one_handed_sword": "Weapon 1",
    "two_handed_sword": "Weapon 1",
    "axe": "Weapon 1",
    "one_handed_axe": "Weapon 1",
    "two_handed_axe": "Weapon 1",
    "mace": "Weapon 1",
    "one_handed_mace": "Weapon 1",
    "two_handed_mace": "Weapon 1",
    "one_hand": "Weapon 1",
    "two_hand": "Weapon 1",
    "flask": "Flask 1",
    "flask1": "Flask 1",
    "flask2": "Flask 2",
    "flask3": "Flask 3",
    "flask4": "Flask 4",
    "flask5": "Flask 5",
    "jewel": "Jewel 1",
    "abyss_jewel": "Jewel 1",
    "cluster_jewel": "Jewel 1",
}

_SLOT_COMPACT_ALIASES: dict[str, str] = {
    "ring_1": "ring1",
    "ring_2": "ring2",
    "ring_3": "ring3",
    "left_ring": "ring1",
    "leftring": "ring1",
    "right_ring": "ring2",
    "rightring": "ring2",
    "左戒指": "ring1",
    "左戒": "ring1",
    "戒指1": "ring1",
    "右戒指": "ring2",
    "右戒": "ring2",
    "戒指2": "ring2",
    "戒指3": "ring3",
    "戒指": "ring",
    "衣服": "body_armour",
    "胸甲": "body_armour",
    "身甲": "body_armour",
    "body_armor": "body_armour",
    "bodyarmour": "body_armour",
    "头盔": "helmet",
    "头部": "helmet",
    "head": "helmet",
    "手套": "gloves",
    "glove": "gloves",
    "hands": "gloves",
    "鞋子": "boots",
    "靴子": "boots",
    "boot": "boots",
    "feet": "boots",
    "shoes": "boots",
    "项链": "amulet",
    "护身符": "amulet",
    "neck": "amulet",
    "腰带": "belt",
    "waist": "belt",
    "盾牌": "shield",
    "箭袋": "quiver",
    "weapon_1": "weapon1",
    "weapon_2": "weapon2",
    "mainhand": "weapon1",
    "main_hand": "weapon1",
    "主手": "weapon1",
    "主手武器": "weapon1",
    "武器1": "weapon1",
    "offhand": "weapon2",
    "off_hand": "weapon2",
    "副手": "weapon2",
    "副手武器": "weapon2",
    "武器2": "weapon2",
    "flask_1": "flask1",
    "flask_2": "flask2",
    "flask_3": "flask3",
    "flask_4": "flask4",
    "flask_5": "flask5",
    "药剂": "flask",
    "药剂1": "flask1",
    "药剂2": "flask2",
    "药剂3": "flask3",
    "药剂4": "flask4",
    "药剂5": "flask5",
    "药水1": "flask1",
    "药水2": "flask2",
    "药水3": "flask3",
    "药水4": "flask4",
    "药水5": "flask5",
    "珠宝": "jewel",
}

_WEAPON_SLOTS: frozenset[str] = frozenset(
    {
        "weapon",
        "weapon1",
        "weapon2",
        "bow",
        "wand",
        "claw",
        "dagger",
        "sceptre",
        "staff",
        "sword",
        "one_handed_sword",
        "two_handed_sword",
        "axe",
        "one_handed_axe",
        "two_handed_axe",
        "mace",
        "one_handed_mace",
        "two_handed_mace",
        "one_hand",
        "two_hand",
    }
)

_ARMOUR_SLOTS: frozenset[str] = frozenset(
    {
        "helmet",
        "helm",
        "body_armour",
        "body",
        "chest",
        "gloves",
        "boots",
        "shield",
    }
)

_SHIELD_SLOTS: frozenset[str] = frozenset({"shield"})

_BASE_ITEM_TYPE_TO_CATEGORY: dict[str, str] = {
    "Bow": "weapon.bow",
    "Wand": "weapon.wand",
    "Claw": "weapon.claw",
    "Dagger": "weapon.dagger",
    "Sceptre": "weapon.sceptre",
    "Staff": "weapon.staff",
    "One Handed Sword": "weapon.onesword",
    "Two Handed Sword": "weapon.twosword",
    "One Handed Axe": "weapon.oneaxe",
    "Two Handed Axe": "weapon.twoaxe",
    "One Handed Mace": "weapon.onemace",
    "Two Handed Mace": "weapon.twomace",
    "Fishing Rod": "weapon.rod",
    "Shield": "armour.shield",
    "Quiver": "armour.quiver",
}

_WEAPON_BASE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("armour.quiver", ("quiver",)),
    ("armour.shield", ("shield", "buckler")),
    ("weapon.bow", ("bow",)),
    ("weapon.sceptre", ("sceptre", "sekhem", "fetish")),
    ("weapon.wand", ("wand", "demon's horn", "imbued wand", "profane wand", "convoking wand")),
    (
        "weapon.claw",
        ("claw", "ripper", "gouger", "awl", "blinder", "stabber", "lasher", "hellion's paw", "eyegouger"),
    ),
    (
        "weapon.dagger",
        (
            "dagger",
            "kris",
            "skean",
            "sai",
            "stiletto",
            "ambusher",
            "poignard",
            "trisula",
            "knife",
            "shank",
            "boot blade",
        ),
    ),
    ("weapon.staff", ("staff", "quarterstaff", "lathi", "maelström", "maelstrom", "gnarled branch")),
    (
        "weapon.twosword",
        (
            "greatsword",
            "two-handed sword",
            "two handed sword",
            "bastard sword",
            "longsword",
            "exquisite blade",
            "highland blade",
            "tiger sword",
            "wraith sword",
            "lion sword",
            "cursed blade",
            "reaver sword",
            "banishing blade",
            "vaal greatsword",
        ),
    ),
    ("weapon.onesword", ("sword", "foil", "rapier", "sabre", "cutlass", "gladius", "estoc", "blade")),
    (
        "weapon.twoaxe",
        (
            "greataxe",
            "two-handed axe",
            "two handed axe",
            "chopper",
            "splitter",
            "labrys",
            "poleaxe",
            "halberd",
            "fleshripper",
            "despot axe",
            "vaal axe",
            "ezomyte axe",
            "sundering axe",
            "headsman axe",
            "talon axe",
            "shadow axe",
            "timber axe",
            "noble axe",
            "abyssal axe",
            "dagger axe",
        ),
    ),
    ("weapon.oneaxe", ("axe", "hatchet", "cleaver", "tomahawk")),
    (
        "weapon.twomace",
        (
            "maul",
            "two-handed mace",
            "two handed mace",
            "great mallet",
            "sledgehammer",
            "piledriver",
            "meatgrinder",
            "steelhead",
            "mallet",
        ),
    ),
    ("weapon.onemace", ("mace", "club", "hammer", "gavel", "breaker", "bludgeon", "morning star", "tenderizer")),
)

# ---------------------------------------------------------------------------
# Pseudo stat IDs, valid trade statuses, stat priority, Chinese aliases
# ---------------------------------------------------------------------------

OPEN_PREFIX_ID = "pseudo.pseudo_number_of_empty_prefix_mods"
OPEN_SUFFIX_ID = "pseudo.pseudo_number_of_empty_suffix_mods"
OPEN_AFFIX_ID = "pseudo.pseudo_number_of_empty_affix_mods"

VALID_STATUSES: tuple[str, ...] = ("available", "securable", "online", "onlineleague", "any")

# `trade_filters.indexed` options of /api/trade/data/filters, as (hours covered, option id).
INDEXED_WINDOWS: tuple[tuple[int, str], ...] = (
    (1, "1hour"), (3, "3hours"), (12, "12hours"), (24, "1day"), (72, "3days"),
    (168, "1week"), (336, "2weeks"), (720, "1month"), (1440, "2months"),
)

STAT_TYPE_ORDER: dict[str, int] = {
    "explicit": 0,
    "crafted": 1,
    "fractured": 1,
    "enchant": 1,
    "implicit": 2,
    "pseudo": 3,
}

ZH_STAT_ALIASES: dict[str, str] = {
    # Life & Mana
    "生命": "+# to maximum Life",
    "最大生命": "+# to maximum Life",
    "血量": "+# to maximum Life",
    "最大生命百分比": "#% increased maximum Life",
    "生命百分比": "#% increased maximum Life",
    "生命回复": "# Life Regenerated per Second",
    "每秒生命回复": "# Life Regenerated per Second",
    "魔力": "+# to maximum Mana",
    "最大魔力": "+# to maximum Mana",
    "魔力回复": "#% increased Mana Regeneration Rate",
    "魔力回复速度": "#% increased Mana Regeneration Rate",
    # Resistances
    "火抗": "+#% to Fire Resistance",
    "火焰抗性": "+#% to Fire Resistance",
    "冰抗": "+#% to Cold Resistance",
    "冰霜抗性": "+#% to Cold Resistance",
    "电抗": "+#% to Lightning Resistance",
    "闪电抗性": "+#% to Lightning Resistance",
    "混沌抗性": "+#% to Chaos Resistance",
    "混抗": "+#% to Chaos Resistance",
    "全抗": "+#% to all Elemental Resistances",
    "全元素抗性": "+#% to all Elemental Resistances",
    "所有元素抗性": "+#% to all Elemental Resistances",
    "元素抗性": "+#% total Elemental Resistance",
    "总元素抗性": "+#% total Elemental Resistance",
    "总抗性": "+#% total Resistance",
    "总抗": "+#% total Resistance",
    # Speed
    "攻速": "#% increased Attack Speed",
    "攻击速度": "#% increased Attack Speed",
    "施法速度": "#% increased Cast Speed",
    "施速": "#% increased Cast Speed",
    "移速": "#% increased Movement Speed",
    "移动速度": "#% increased Movement Speed",
    # Defences & Suppression & Block
    "法压": "+#% chance to Suppress Spell Damage",
    "法术压制": "+#% chance to Suppress Spell Damage",
    "法术压制率": "+#% chance to Suppress Spell Damage",
    "压制法术伤害几率": "+#% chance to Suppress Spell Damage",
    "能量护盾": "+# to maximum Energy Shield",
    "护盾": "+# to maximum Energy Shield",
    "最大能量护盾": "+# to maximum Energy Shield",
    "能量护盾百分比": "#% increased Energy Shield",
    "护盾百分比": "#% increased Energy Shield",
    "护甲": "+# to Armour",
    "护甲百分比": "#% increased Armour",
    "闪避": "+# to Evasion Rating",
    "闪避值": "+# to Evasion Rating",
    "闪避百分比": "#% increased Evasion Rating",
    "结界": "+# to Ward",
    "格挡": "+#% Chance to Block",
    "格挡率": "+#% Chance to Block",
    "格挡几率": "+#% Chance to Block",
    "攻击格挡": "#% Chance to Block Attack Damage",
    "攻击格挡率": "#% Chance to Block Attack Damage",
    "法术格挡": "#% Chance to Block Spell Damage",
    "法术格挡率": "#% Chance to Block Spell Damage",
    "法格": "#% Chance to Block Spell Damage",
    # Crit & Accuracy
    "暴击率": "#% increased Critical Strike Chance",
    "暴击几率": "#% increased Critical Strike Chance",
    "全局暴击率": "#% increased Global Critical Strike Chance",
    "暴击伤害": "+#% to Global Critical Strike Multiplier",
    "暴伤": "+#% to Global Critical Strike Multiplier",
    "全局暴击伤害加成": "+#% to Global Critical Strike Multiplier",
    "命中": "+# to Accuracy Rating",
    "命中值": "+# to Accuracy Rating",
    # Attributes
    "力量": "+# to Strength",
    "敏捷": "+# to Dexterity",
    "智慧": "+# to Intelligence",
    "智力": "+# to Intelligence",
    "全属性": "+# to all Attributes",
    "所有属性": "+# to all Attributes",
    # Damage & Flat Added Damage
    "物理伤害": "#% increased Physical Damage",
    "法术伤害": "#% increased Spell Damage",
    "法伤": "#% increased Spell Damage",
    "元素伤害": "#% increased Elemental Damage",
    "火焰伤害": "#% increased Fire Damage",
    "火伤": "#% increased Fire Damage",
    "冰霜伤害": "#% increased Cold Damage",
    "冰伤": "#% increased Cold Damage",
    "闪电伤害": "#% increased Lightning Damage",
    "电伤": "#% increased Lightning Damage",
    "混沌伤害": "#% increased Chaos Damage",
    "混伤": "#% increased Chaos Damage",
    "火焰点伤": "Adds # to # Fire Damage",
    "附加火焰伤害": "Adds # to # Fire Damage",
    "冰霜点伤": "Adds # to # Cold Damage",
    "附加冰霜伤害": "Adds # to # Cold Damage",
    "闪电点伤": "Adds # to # Lightning Damage",
    "附加闪电伤害": "Adds # to # Lightning Damage",
    "物理点伤": "Adds # to # Physical Damage",
    "附加物理伤害": "Adds # to # Physical Damage",
    "混沌点伤": "Adds # to # Chaos Damage",
    "附加混沌伤害": "Adds # to # Chaos Damage",
    "攻击火焰点伤": "Adds # to # Fire Damage to Attacks",
    "攻击附加火焰伤害": "Adds # to # Fire Damage to Attacks",
    "攻击冰霜点伤": "Adds # to # Cold Damage to Attacks",
    "攻击附加冰霜伤害": "Adds # to # Cold Damage to Attacks",
    "攻击闪电点伤": "Adds # to # Lightning Damage to Attacks",
    "攻击附加闪电伤害": "Adds # to # Lightning Damage to Attacks",
    "攻击物理点伤": "Adds # to # Physical Damage to Attacks",
    "攻击附加物理伤害": "Adds # to # Physical Damage to Attacks",
    "法术火焰点伤": "Adds # to # Fire Damage to Spells",
    "法术附加火焰伤害": "Adds # to # Fire Damage to Spells",
    "法术冰霜点伤": "Adds # to # Cold Damage to Spells",
    "法术附加冰霜伤害": "Adds # to # Cold Damage to Spells",
    "法术闪电点伤": "Adds # to # Lightning Damage to Spells",
    "法术附加闪电伤害": "Adds # to # Lightning Damage to Spells",
    "法术物理点伤": "Adds # to # Physical Damage to Spells",
    "法术附加物理伤害": "Adds # to # Physical Damage to Spells",
    "物品稀有度": "#% increased Rarity of Items found",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_BRACE_NUM_RE = re.compile(r"\{\d+\}")
_SCOPE_SUFFIX_RE = re.compile(r"\s*\((local|shields|global)\)\s*$", re.IGNORECASE)
_LEADING_PLACEHOLDER_RE = re.compile(r"^(?:#%\s+|#\s+)+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_WEAPON_FLAT_DMG_RE = re.compile(r"^adds # to # (?:fire|cold|lightning|physical|chaos) damage$")
_SLOW_NORMALIZE_CHARS_RE = re.compile(r"[<\[{(0-9\u2013\u2014\u2212]")
_MULTI_SPACE_RE = re.compile(r"\s+")
_UNREQUESTED_MULTIPLIER_TOKENS: frozenset[str] = frozenset({"more", "less", "reduced", "total"})

_STOPWORDS: frozenset[str] = frozenset(
    {
        "to",
        "of",
        "the",
        "a",
        "an",
        "in",
        "on",
        "and",
        "or",
        "for",
        "with",
        "per",
        "you",
        "your",
        "have",
        "has",
        "are",
        "is",
        "by",
        "at",
        "from",
        "as",
        "if",
    }
)

# Process-local caches
_LOCAL_STATS_MEMO: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
_LOCAL_STATS_LOCK = threading.Lock()
_ENTRY_INDEX_CACHE: dict[tuple[int, int, str, str], "_EntryIndex"] = {}
_ENTRY_INDEX_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class StatMatch:
    id: str
    text: str
    score: float
    template: str


@dataclass
class StatRequest:
    text: str
    min_value: float = 1.0
    match: StatMatch | None = None


@dataclass
class ConversionPlan:
    """One ``--stat`` expanded into a count group over convertible elements."""

    group: str
    target: str
    min_value: float
    stat_ids: dict[str, str] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    cost: str = ""
    patch: str = ""

    @property
    def elements(self) -> list[str]:
        return list(self.stat_ids)


@dataclass
class WeightedStatEntry:
    """One modifier entry inside an official PoE1 ``type: "weight"`` (Weighted Sum) group."""

    text: str
    weight: float = 1.0
    min_value: float | None = None
    match: StatMatch | None = None


@dataclass
class WeightedGroup:
    """An official PoE1 ``type: "weight"`` stat group whose values sum into ``Sum: XX``."""

    entries: list[WeightedStatEntry] = field(default_factory=list)
    min_sum: float | None = None


@dataclass
class StatPlan:
    plain: list[StatRequest] = field(default_factory=list)
    conversions: list[ConversionPlan] = field(default_factory=list)
    weighted_groups: list[WeightedGroup] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Slot normalization & weapon base category inference
# ---------------------------------------------------------------------------


def _normalize_slot(slot: str | None) -> str:
    if not slot:
        return ""
    cleaned = re.sub(r"[\s\-]+", "_", slot.strip().lower()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    return _SLOT_COMPACT_ALIASES.get(cleaned, cleaned)


def category_for_slot(slot: str) -> str | None:
    """Map a PoE1 slot or weapon subtype name to its trade API ``type_filters.category`` option."""
    return CATEGORY_BY_SLOT.get(_normalize_slot(slot))


def pob_slot_for_slot(slot: str) -> str | None:
    """Map a slot or weapon subtype name to its PoB equipment slot name."""
    return POB_SLOT_BY_SLOT.get(_normalize_slot(slot))


def infer_weapon_category_from_base(base_type: str, pob_root: Path | None = None) -> str | None:
    """Infer trade category (e.g. ``weapon.bow``, ``weapon.wand``) from an item base type.

    Uses the local PoB SQLite ``bases`` table when available, falling back to
    offline keyword matching so CI/no-engine environments still work cleanly.
    """
    cleaned = (base_type or "").strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()

    # Fast path: query local SQLite bases table if engine data exists.
    try:
        resolved_root = Path(pob_root) if pob_root is not None else resolve_engine_root()
        if (resolved_root / "PoE1" / "Data").is_dir():
            from . import game_data

            db_path = game_data.ensure_gamedata_index(pob_dir=resolved_root)
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT item_type, sub_type FROM bases WHERE name_lower = ? OR zh_lower = ? LIMIT 1",
                    (lowered, lowered),
                ).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                mapped = _BASE_ITEM_TYPE_TO_CATEGORY.get(str(row[0]))
                if mapped:
                    return mapped
    except Exception:
        pass

    # Lightweight keyword fallback.
    for category, keywords in _WEAPON_BASE_KEYWORDS:
        for kw in keywords:
            if kw in lowered:
                return category
    return None


# ---------------------------------------------------------------------------
# Trade stats flattening & zero-network local TradeSiteStats.lua loader
# ---------------------------------------------------------------------------


def flatten_stats(groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten official `/api/trade/data/stats` groups into a list of stat entries."""
    entries: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_entries = group.get("entries")
        if isinstance(group_entries, list):
            group_type = str(group.get("id") or "").strip().lower()
            for entry in group_entries:
                if not isinstance(entry, dict):
                    continue
                stat_id = entry.get("id")
                text = entry.get("text")
                if stat_id and text:
                    item: dict[str, Any] = {
                        "id": str(stat_id),
                        "text": str(text),
                        "type": str(entry.get("type") or group_type or str(stat_id).split(".", 1)[0]),
                    }
                    entries.append(item)
        elif group.get("id") and group.get("text"):
            stat_id = str(group["id"])
            entries.append(
                {
                    "id": stat_id,
                    "text": str(group["text"]),
                    "type": str(group.get("type") or stat_id.split(".", 1)[0]),
                }
            )
    return entries


def _unescape_lua_string(raw: str) -> str:
    if "\\" not in raw:
        return raw
    return raw.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")


def _parse_trade_site_stats_lua(lua_path: Path) -> list[dict[str, Any]]:
    """Fast depth-aware scanner for PoB's ``PoE1/Data/TradeSiteStats.lua``.

    Tracks table nesting depth so nested ``["option"] = { ["options"] = { ... } }``
    tables never collide with outer stat entry ``["id"]`` / ``["text"]`` / ``["type"]``.
    """
    text = lua_path.read_text(encoding="utf-8", errors="replace")
    entries: list[dict[str, Any]] = []
    depth = 0
    cur: dict[str, str] | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if stripped.endswith("{"):
            depth += 1
            if depth == 4 and stripped == "{":
                cur = {}
            continue
        if stripped.startswith("}"):
            if depth == 4 and cur is not None:
                if "id" in cur and "text" in cur:
                    if "type" not in cur:
                        cur["type"] = cur["id"].split(".", 1)[0]
                    entries.append(cur)
                cur = None
            depth -= 1
            continue
        if depth == 4 and cur is not None:
            if stripped.startswith('["id"] = "'):
                end_q = stripped.rfind('"')
                if end_q > 10:
                    cur["id"] = stripped[10:end_q]
            elif stripped.startswith('["text"] = "'):
                end_q = stripped.rfind('"')
                if end_q > 12:
                    cur["text"] = _unescape_lua_string(stripped[12:end_q])
            elif stripped.startswith('["type"] = "'):
                end_q = stripped.rfind('"')
                if end_q > 12:
                    cur["type"] = stripped[12:end_q]
    return entries


def load_local_trade_stats(
    pob_root: Path | None = None,
    cache_dir: Path | None = None,
) -> list[dict[str, Any]] | None:
    """Load PoE1 trade stats from local ``PoE1/Data/TradeSiteStats.lua`` with JSON cache.

    Returns ``None`` cleanly when the local engine or ``TradeSiteStats.lua`` is absent.
    """
    engine_root = Path(pob_root) if pob_root is not None else resolve_engine_root()
    lua_path = engine_root / "PoE1" / "Data" / "TradeSiteStats.lua"
    if not lua_path.is_file():
        return None

    try:
        st = lua_path.stat()
    except OSError:
        return None

    memo_key = (str(lua_path.resolve()), st.st_mtime_ns, st.st_size)
    with _LOCAL_STATS_LOCK:
        cached_mem = _LOCAL_STATS_MEMO.get(memo_key)
        if cached_mem is not None:
            return cached_mem

    resolved_cache_dir = Path(cache_dir) if cache_dir is not None else _get_cache_dir()
    cache_file = resolved_cache_dir / f"trade_stats_local_{st.st_mtime_ns}_{st.st_size}.json"

    if cache_file.is_file():
        try:
            loaded = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(loaded, list) and loaded:
                with _LOCAL_STATS_LOCK:
                    _LOCAL_STATS_MEMO[memo_key] = loaded
                return loaded
        except (OSError, ValueError):
            pass

    entries = _parse_trade_site_stats_lua(lua_path)
    if not entries:
        return None

    try:
        resolved_cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = resolved_cache_dir / f".{cache_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp_path.write_text(
            json.dumps(entries, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp_path, cache_file)
    except OSError:
        pass

    with _LOCAL_STATS_LOCK:
        _LOCAL_STATS_MEMO[memo_key] = entries
    return entries


def _normalize_remote_stats(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict) and isinstance(raw.get("result"), list):
        return flatten_stats(raw["result"])
    if isinstance(raw, Iterable):
        return flatten_stats(raw)
    return []


def load_trade_stats(
    *,
    pob_root: Path | None = None,
    cache_dir: Path | None = None,
    remote_fetcher: Callable[[], Any] | None = None,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Load PoE1 trade stats preferring zero-network local ``TradeSiteStats.lua``.

    Returns ``(entries, source)`` where ``source`` is ``"local_engine"`` or ``"remote_api"``.
    Raises ``RuntimeError`` when neither local nor remote stats are available.
    """
    if not force_refresh:
        local = load_local_trade_stats(pob_root=pob_root, cache_dir=cache_dir)
        if local:
            return local, "local_engine"

    if remote_fetcher is not None:
        remote_raw = remote_fetcher()
        remote_entries = _normalize_remote_stats(remote_raw)
        if remote_entries:
            return remote_entries, "remote_api"

    if force_refresh:
        local = load_local_trade_stats(pob_root=pob_root, cache_dir=cache_dir)
        if local:
            return local, "local_engine"

    raise RuntimeError("No trade stats available locally or remotely.")


# ---------------------------------------------------------------------------
# Bilingual EN/ZH stat translation
# ---------------------------------------------------------------------------


def translate_stat_query(text: str) -> str:
    """Translate a Chinese PoE1 stat query into English stat text.

    Non-CJK text is returned unchanged. Uses the curated ``ZH_STAT_ALIASES`` table
    first, then falls back to local SQLite ``game_data`` translations if available.
    """
    raw = (text or "").strip()
    if not raw or not _CJK_RE.search(raw):
        return raw

    # Direct exact lookup
    direct = ZH_STAT_ALIASES.get(raw)
    if direct:
        return direct

    # Strip common numeric/symbol prefixes/suffixes (e.g., "+#% 火焰抗性", "最大生命 +")
    cleaned = re.sub(r"[+#%\d\s()（）\-–—]+", "", raw)
    if cleaned:
        direct_clean = ZH_STAT_ALIASES.get(cleaned)
        if direct_clean:
            return direct_clean
        # Strip leading verb hints like "提高"/"增加" when exact alias follows
        for prefix in ("提高", "增加"):
            if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
                sub = cleaned[len(prefix) :]
                if sub in ZH_STAT_ALIASES:
                    return ZH_STAT_ALIASES[sub]

    # Fallback: query local PoB SQLite translations table when available
    try:
        resolved_root = resolve_engine_root()
        if (resolved_root / "PoE1" / "Data").is_dir():
            from . import game_data

            query_term = cleaned or raw
            res = game_data.query_local_data("translate", query_term, limit=12)
            rows = [
                r
                for r in (res.get("results") or [])
                if str(r.get("match_type") or "") != "partial"
            ]
            if rows:
                cat_rank = {"mod": 0, "stat": 1, "passive": 2, "base": 3, "gem": 4, "unique": 5}
                match_rank = {"exact": 0, "prefix": 1, "substring": 2}
                best_row = min(
                    rows,
                    key=lambda r: (
                        cat_rank.get(str(r.get("category") or ""), 9),
                        match_rank.get(str(r.get("match_type") or ""), 9),
                        len(str(r.get("en") or "")),
                    ),
                )
                en_text = str(best_row.get("en") or "").strip()
                if en_text:
                    return _BRACE_NUM_RE.sub("#", en_text)
    except Exception:
        pass

    return raw


# ---------------------------------------------------------------------------
# Fast indexed, slot-aware (Local)/(Shields) stat resolution
# ---------------------------------------------------------------------------


def _stat_priority(stat_id: str) -> int:
    prefix = stat_id.split(".", 1)[0]
    return STAT_TYPE_ORDER.get(prefix, 5)


def _split_scope_tag(text: str) -> tuple[str, str]:
    """Split trailing ``(Local)`` / ``(Shields)`` / ``(Global)`` from raw stat text."""
    raw = (text or "").strip()
    m = _SCOPE_SUFFIX_RE.search(raw)
    if not m:
        return raw, ""
    return raw[: m.start()].strip(), m.group(1).lower()


def _strip_leading_placeholder(template: str) -> str:
    return _LEADING_PLACEHOLDER_RE.sub("", template).strip()


def _extract_tokens(text: str) -> frozenset[str]:
    return frozenset(tok for tok in _TOKEN_RE.findall(text) if len(tok) >= 2)


def _meaningful_tokens(tokens: frozenset[str]) -> frozenset[str]:
    filtered = tokens - _STOPWORDS
    return filtered if filtered else tokens


def _is_weapon_local_template(base_template: str) -> bool:
    if "attack speed" in base_template:
        return True
    if "critical strike chance" in base_template and "global" not in base_template:
        return True
    if (
        "physical damage" in base_template
        and "global" not in base_template
        and "over time" not in base_template
    ):
        return True
    if _WEAPON_FLAT_DMG_RE.match(base_template):
        return True
    if "accuracy rating" in base_template:
        return True
    if "chance to poison on hit" in base_template:
        return True
    if "physical attack damage leeched as" in base_template:
        return True
    return False


def _is_armour_local_template(base_template: str) -> bool:
    return any(
        kw in base_template
        for kw in ("armour", "evasion rating", "evasion", "energy shield", "ward")
    )


def _is_shield_block_template(base_template: str) -> bool:
    return "chance to block" in base_template


def _scope_adjustment(
    base_template: str,
    entry_scope: str,
    slot_norm: str,
    requested_scope: str,
) -> float:
    """Compute slot-aware preference adjustment for ``(Local)`` / ``(Shields)`` / global entries."""
    if requested_scope:
        if entry_scope == requested_scope:
            return 0.08
        if requested_scope == "global" and entry_scope == "":
            return 0.06
        return -0.08

    if not slot_norm:
        if entry_scope == "":
            return 0.01
        return -0.01

    if _is_shield_block_template(base_template):
        if slot_norm in _SHIELD_SLOTS:
            if entry_scope == "shields":
                return 0.08
            if entry_scope == "":
                return 0.0
            return -0.06
        if entry_scope == "shields":
            return -0.14

    if _is_weapon_local_template(base_template):
        if slot_norm in _WEAPON_SLOTS:
            if entry_scope == "local":
                return 0.08
            if entry_scope == "":
                return 0.0
            return -0.06
        # Non-weapon slot (gloves, ring, amulet, quiver, armour, etc.)
        if entry_scope == "local":
            return -0.14
        if entry_scope in ("", "global"):
            return 0.02

    if _is_armour_local_template(base_template):
        if slot_norm in _ARMOUR_SLOTS:
            if entry_scope == "local":
                return 0.08
            if entry_scope == "":
                return 0.0
            return -0.06
        # Non-armour slot (rings, amulet, belt, weapon, jewel, etc.)
        if entry_scope == "local":
            return -0.14
        if entry_scope in ("", "global"):
            return 0.02

    # Fallback for any other scoped entry when slot is known
    if entry_scope == "shields" and slot_norm not in _SHIELD_SLOTS:
        return -0.12
    if entry_scope == "":
        return 0.01
    return 0.0


@dataclass(frozen=True)
class _PreparedEntry:
    entry: dict[str, Any]
    stat_id: str
    raw_text: str
    base_template: str
    core_template: str
    scope_tag: str
    tokens: frozenset[str]
    priority: int


def _fast_normalize_stat_text(base_raw: str) -> str:
    """Fast path equivalent to ``normalize_template`` for clean TradeSiteStats lines."""
    if _SLOW_NORMALIZE_CHARS_RE.search(base_raw):
        return normalize_template(base_raw)
    value = base_raw.lower().replace("+", "")
    if "  " in value or "\t" in value or "\n" in value:
        value = _MULTI_SPACE_RE.sub(" ", value)
    return value.strip(" .,")


class _EntryIndex:
    """In-memory inverted index over trade stat entries for <15ms lookups."""

    __slots__ = ("prepared", "by_base", "by_core", "by_token")

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        prepared: list[_PreparedEntry] = []
        by_base: dict[str, list[_PreparedEntry]] = {}
        by_core: dict[str, list[_PreparedEntry]] = {}
        by_token: dict[str, list[int]] = {}

        for entry in entries:
            raw_text = str(entry.get("text") or "")
            stat_id = str(entry.get("id") or "")
            if not raw_text or not stat_id:
                continue
            base_raw, scope_tag = _split_scope_tag(raw_text)
            base_template = _fast_normalize_stat_text(base_raw)
            if not base_template:
                continue
            core_template = _strip_leading_placeholder(base_template)
            tokens = _extract_tokens(core_template)
            idx = len(prepared)
            item = _PreparedEntry(
                entry=entry,
                stat_id=stat_id,
                raw_text=raw_text,
                base_template=base_template,
                core_template=core_template,
                scope_tag=scope_tag,
                tokens=tokens,
                priority=_stat_priority(stat_id),
            )
            prepared.append(item)
            by_base.setdefault(base_template, []).append(item)
            if core_template:
                by_core.setdefault(core_template, []).append(item)
            for tok in tokens:
                by_token.setdefault(tok, []).append(idx)

        self.prepared = prepared
        self.by_base = by_base
        self.by_core = by_core
        self.by_token = by_token


def _get_entry_index(entries: list[dict[str, Any]]) -> _EntryIndex:
    if not entries:
        return _EntryIndex([])
    first_id = str(entries[0].get("id") or "")
    last_id = str(entries[-1].get("id") or "")
    key = (id(entries), len(entries), first_id, last_id)
    with _ENTRY_INDEX_LOCK:
        cached = _ENTRY_INDEX_CACHE.get(key)
        if cached is not None:
            return cached
    index = _EntryIndex(entries)
    with _ENTRY_INDEX_LOCK:
        if len(_ENTRY_INDEX_CACHE) >= 8:
            _ENTRY_INDEX_CACHE.clear()
        _ENTRY_INDEX_CACHE[key] = index
    return index


def _verb_penalty(target_tokens: frozenset[str], item_tokens: frozenset[str]) -> float:
    """Avoid letting rare ``more`` / ``less`` / ``reduced`` mods hijack plain queries."""
    for tok in _UNREQUESTED_MULTIPLIER_TOKENS:
        if tok in item_tokens and tok not in target_tokens:
            return -0.04
    return 0.0


def _base_similarity(
    target: str,
    target_core: str,
    target_tokens: frozenset[str],
    item: _PreparedEntry,
) -> float:
    if target == item.base_template:
        return 1.0
    if target_core and target_core == item.core_template:
        return 0.98

    # Safe one-way containment: short query inside longer template ONLY when
    # the query is substantial (>=6 chars) and covers at least 45% of the entry.
    if len(target_core) >= 6 and target_core in item.core_template:
        ratio = len(target_core) / max(1, len(item.core_template))
        if ratio >= 0.45:
            return 0.90 + 0.05 * ratio

    union_len = len(target_tokens | item.tokens)
    jaccard = (len(target_tokens & item.tokens) / union_len) if union_len else 0.0
    seq_core = SequenceMatcher(None, target_core, item.core_template).ratio()
    blended = 0.45 * jaccard + 0.55 * seq_core
    if target != target_core and blended >= 0.55:
        seq_full = SequenceMatcher(None, target, item.base_template).ratio()
        return max(blended, seq_full * 0.95)
    return blended


def _select_candidates(
    index: _EntryIndex,
    target: str,
    target_core: str,
    target_tokens: frozenset[str],
    *,
    max_candidates: int = 120,
) -> list[_PreparedEntry]:
    """Fast candidate pre-filter using exact maps + inverted token index."""
    if len(index.prepared) <= max_candidates:
        return index.prepared

    seen_ids: set[int] = set()
    candidates: list[_PreparedEntry] = []

    def _add_prepared(items: list[_PreparedEntry]) -> None:
        for it in items:
            obj_id = id(it)
            if obj_id not in seen_ids:
                seen_ids.add(obj_id)
                candidates.append(it)

    exact_base = index.by_base.get(target)
    if exact_base:
        _add_prepared(exact_base)
    exact_core = index.by_core.get(target_core)
    if exact_core:
        _add_prepared(exact_core)

    # If we already have exact base/core matches, no need to scan 18k entries.
    if candidates:
        return candidates

    # Rank entries by overlapping meaningful tokens via inverted index.
    query_toks = _meaningful_tokens(target_tokens)
    overlap_counts: dict[int, int] = {}
    for tok in query_toks:
        for entry_idx in index.by_token.get(tok, ()):
            overlap_counts[entry_idx] = overlap_counts.get(entry_idx, 0) + 1

    if not overlap_counts and target_tokens != query_toks:
        for tok in target_tokens:
            for entry_idx in index.by_token.get(tok, ()):
                overlap_counts[entry_idx] = overlap_counts.get(entry_idx, 0) + 1

    if overlap_counts:
        # Sort by descending token overlap, breaking ties by shorter template length.
        ranked_indices = sorted(
            overlap_counts.keys(),
            key=lambda i: (-overlap_counts[i], len(index.prepared[i].core_template)),
        )[:max_candidates]
        return [index.prepared[i] for i in ranked_indices]

    # Last-resort prefix/character filter if no exact token matched (e.g. typo).
    prefix = target_core[:3]
    if prefix:
        prefix_hits = [it for it in index.prepared if prefix in it.core_template]
        if prefix_hits:
            return prefix_hits[:max_candidates]
    return index.prepared[:max_candidates]


def resolve_stat(
    text: str,
    entries: list[dict[str, Any]],
    *,
    slot: str | None = None,
    threshold: float = 0.80,
) -> StatMatch | None:
    """Resolve an EN or ZH modifier query to its PoE1 trade stat ID.

    Slot-aware: prefers ``(Local)`` for local weapon stats on weapon slots,
    ``(Local)`` for local defence stats on armour slots, ``(Shields)`` for
    block chance on shields, and global/unscoped stats otherwise.
    """
    translated = translate_stat_query(text)
    query_base, requested_scope = _split_scope_tag(translated)
    target = normalize_template(query_base)
    if not target:
        return None
    target_core = _strip_leading_placeholder(target)
    target_tokens = _extract_tokens(target_core)
    slot_norm = _normalize_slot(slot)

    index = _get_entry_index(entries)
    if not index.prepared:
        return None

    candidates = _select_candidates(index, target, target_core, target_tokens)

    best: tuple[float, int, int, float, _PreparedEntry] | None = None
    for item in candidates:
        base_score = _base_similarity(target, target_core, target_tokens, item)
        if base_score < threshold:
            continue
        scope_adj = _scope_adjustment(item.base_template, item.scope_tag, slot_norm, requested_scope)
        verb_adj = _verb_penalty(target_tokens, item.tokens)
        effective = round(base_score + scope_adj + verb_adj, 6)
        key = (effective, -item.priority, -len(item.base_template), base_score, item)
        if best is None or key[:3] > best[:3]:
            best = key

    if best is None:
        return None
    _, _, _, base_score, item = best
    return StatMatch(
        id=item.stat_id,
        text=item.raw_text,
        score=round(min(1.0, max(0.0, base_score)), 3),
        template=item.base_template,
    )


def suggest_stats(
    text: str,
    entries: list[dict[str, Any]],
    limit: int = 5,
    *,
    slot: str | None = None,
) -> list[StatMatch]:
    """Return top candidate trade stat matches for error diagnostics or discovery."""
    translated = translate_stat_query(text)
    query_base, requested_scope = _split_scope_tag(translated)
    target = normalize_template(query_base)
    if not target:
        return []
    target_core = _strip_leading_placeholder(target)
    target_tokens = _extract_tokens(target_core)
    slot_norm = _normalize_slot(slot)

    index = _get_entry_index(entries)
    if not index.prepared:
        return []

    candidates = _select_candidates(index, target, target_core, target_tokens, max_candidates=200)
    scored: list[tuple[float, int, int, float, _PreparedEntry]] = []
    for item in candidates:
        base_score = _base_similarity(target, target_core, target_tokens, item)
        scope_adj = _scope_adjustment(item.base_template, item.scope_tag, slot_norm, requested_scope)
        verb_adj = _verb_penalty(target_tokens, item.tokens)
        effective = round(base_score + scope_adj + verb_adj, 6)
        scored.append((effective, -item.priority, -len(item.base_template), base_score, item))

    scored.sort(key=lambda row: row[:3], reverse=True)
    return [
        StatMatch(
            id=item.stat_id,
            text=item.raw_text,
            score=round(min(1.0, max(0.0, base_score)), 3),
            template=item.base_template,
        )
        for _, _, _, base_score, item in scored[:limit]
    ]


def resolve_requests(
    requests: Iterable[StatRequest],
    entries: list[dict[str, Any]],
    *,
    slot: str | None = None,
) -> None:
    """Resolve every ``StatRequest`` in place, raising ``ValueError`` with suggestions on miss."""
    for request in requests:
        match = resolve_stat(request.text, entries, slot=slot)
        if match is None:
            lines = [f"Could not map this modifier to a trade filter: {request.text}"]
            for suggestion in suggest_stats(request.text, entries, slot=slot):
                lines.append(f"  - {suggestion.text}  ({suggestion.id})")
            lines.append("Rewrite the text, or use --want (scoring only, no filter).")
            raise ValueError("\n".join(lines))
        request.match = match


# ---------------------------------------------------------------------------
# Harvest conversion planning & official trade query builder
# ---------------------------------------------------------------------------


def parse_stat_argument(value: str) -> StatRequest:
    """Parse ``"to Cold Resistance=40"``, ``"生命>=70"``, or ``"冰霜抗性=40"`` into a ``StatRequest``."""
    text = (value or "").strip()
    minimum = 1.0
    if "=" in text:
        candidate_text, candidate_min = text.rsplit("=", 1)
        try:
            minimum = float(candidate_min.strip())
            text = candidate_text.rstrip(">").strip()
        except ValueError:
            pass
    if not text:
        raise ValueError("--stat cannot be empty")
    return StatRequest(text=text, min_value=minimum)


_SUM_MIN_SUFFIX_RE = re.compile(r">=\s*([+-]?\d+(?:\.\d+)?)\s*$")
_WEIGHT_SUFFIX_RE = re.compile(r"\*\s*([+-]?\d+(?:\.\d+)?)\s*$")


def parse_sum_group_argument(value: str) -> WeightedGroup:
    """Parse a Weighted Sum group argument into a :class:`WeightedGroup`.

    Format: ``"stat1[*w1][=min1]|stat2[*w2][=min2]|...[>=min_sum]"`` (``;`` also accepted
    as separator). Example:
    ``"+#% to Global Critical Strike Multiplier|+#% to Critical Strike Multiplier for Spell Damage>=25"``.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("--sum cannot be empty")

    min_sum: float | None = None
    m_min = _SUM_MIN_SUFFIX_RE.search(raw)
    if m_min:
        parsed_min = float(m_min.group(1))
        if not math.isfinite(parsed_min):
            raise ValueError(f"Invalid --sum minimum: {m_min.group(1)}")
        min_sum = parsed_min
        raw = raw[: m_min.start()].strip()

    parts = [part.strip() for part in re.split(r"[|;]", raw) if part.strip()]
    if not parts:
        raise ValueError("--sum must include at least one modifier")

    entries: list[WeightedStatEntry] = []
    for part in parts:
        text = part
        weight = 1.0
        entry_min: float | None = None

        m_weight = _WEIGHT_SUFFIX_RE.search(text)
        if m_weight:
            parsed_w = float(m_weight.group(1))
            if not math.isfinite(parsed_w):
                raise ValueError(f"Invalid --sum weight: {m_weight.group(1)}")
            weight = parsed_w
            text = text[: m_weight.start()].strip()

        if "=" in text:
            candidate_text, candidate_min = text.rsplit("=", 1)
            try:
                parsed_entry_min = float(candidate_min.strip())
            except ValueError:
                parsed_entry_min = None
            if parsed_entry_min is not None:
                if not math.isfinite(parsed_entry_min):
                    raise ValueError(f"Invalid --sum stat min: {candidate_min.strip()}")
                entry_min = parsed_entry_min
                text = candidate_text.strip()

        if not text:
            raise ValueError(f"Empty modifier entry in --sum: {value!r}")
        entries.append(WeightedStatEntry(text=text, weight=weight, min_value=entry_min))

    return WeightedGroup(entries=entries, min_sum=min_sum)


def _conversion_plan(
    request: StatRequest,
    entries: list[dict[str, Any]],
    *,
    slot: str | None = None,
) -> ConversionPlan | None:
    """Expand one convertible request into sibling-element stat IDs (slot-aware)."""
    if request.match is None:
        return None
    found = match_harvest(request.match.template)
    if found is None:
        return None

    stat_ids: dict[str, str] = {}
    templates: dict[str, str] = {}
    for element in found.group.elements:
        if element not in ELEMENTS:
            continue
        template = request.match.template
        if element != found.element:
            template = convert_template(template, element) or ""
        if not template:
            continue
        resolved = resolve_stat(template, entries, slot=slot)
        if resolved is None:
            continue
        stat_ids[element] = resolved.id
        templates[element] = resolved.template
    if len(stat_ids) < 2:
        return None
    return ConversionPlan(
        group=found.group.key,
        target=found.element,
        min_value=request.min_value,
        stat_ids=stat_ids,
        templates=templates,
        cost=found.group.cost,
        patch=found.group.patch,
    )


def plan_stats(
    requests: list[StatRequest],
    entries: list[dict[str, Any]],
    *,
    slot: str | None = None,
    allow_conversion: bool = False,
    convert_to: str | None = None,
    rarity: str | None = "rare",
    weighted_groups: list[WeightedGroup] | None = None,
) -> StatPlan:
    """Resolve requests and optionally expand Harvest-convertible ones into count groups."""
    if allow_conversion and (rarity or "").strip().lower() == "unique":
        raise ValueError(
            "--allow-conversion cannot be combined with --rarity unique "
            "(uniques cannot use Harvest conversion)."
        )

    resolve_requests(requests, entries, slot=slot)
    plan = StatPlan()
    target_override = convert_to.strip().lower() if convert_to else None

    for request in requests:
        conversion = _conversion_plan(request, entries, slot=slot) if allow_conversion else None
        if conversion is None:
            plan.plain.append(request)
            continue
        if target_override:
            if target_override not in conversion.stat_ids:
                raise ValueError(
                    f"Cannot convert {request.match.text if request.match else request.text} "
                    f"to {target_override!r}; available: {', '.join(conversion.elements)}"
                )
            conversion.target = target_override
        plan.conversions.append(conversion)

    for group in weighted_groups or []:
        resolved_entries: list[WeightedStatEntry] = []
        for item in group.entries:
            match = resolve_stat(item.text, entries, slot=slot)
            if match is None:
                lines = [f"Could not map this --sum modifier to a trade filter: {item.text}"]
                for suggestion in suggest_stats(item.text, entries, slot=slot):
                    lines.append(f"  - {suggestion.text}  ({suggestion.id})")
                raise ValueError("\n".join(lines))
            resolved_entries.append(
                WeightedStatEntry(
                    text=item.text,
                    weight=item.weight,
                    min_value=item.min_value,
                    match=match,
                )
            )
        if resolved_entries:
            plan.weighted_groups.append(
                WeightedGroup(entries=resolved_entries, min_sum=group.min_sum)
            )

    return plan


def build_query(
    *,
    plan: StatPlan,
    category: str | None,
    rarity: str | None,
    item_level_min: int = 0,
    status: str = "available",
    currency: str | None = "chaos_equiv",
    budget: float | None = None,
    search_sort: str = "asc",
    pseudo_filters: list[dict[str, Any]] | None = None,
    exclude_corrupted_mirrored: bool | None = None,
    allow_corrupted: bool = False,
    max_age_hours: float | None = None,
) -> dict[str, Any]:
    """Build an official PoE1 ``/api/trade/search`` JSON request body."""
    status_norm = (status or "available").strip().lower()
    if status_norm not in VALID_STATUSES:
        raise ValueError(
            f"Invalid trade status {status!r}; expected one of: {', '.join(VALID_STATUSES)}"
        )

    stat_groups: list[dict[str, Any]] = []

    # Place weighted sum groups first (index 0) matching PoB TradeQueryRequests.lua,
    # so `statgroup.0` cleanly targets the primary weighted sum on the trade site.
    first_weight_idx: int | None = None
    for wgroup in plan.weighted_groups:
        wfilters: list[dict[str, Any]] = []
        for entry in wgroup.entries:
            if entry.match is None:
                continue
            val: dict[str, Any] = {"weight": entry.weight}
            if entry.min_value is not None:
                val["min"] = entry.min_value
            wfilters.append(
                {
                    "id": entry.match.id,
                    "value": val,
                    "disabled": False,
                }
            )
        if wfilters:
            if first_weight_idx is None:
                first_weight_idx = len(stat_groups)
            group_obj: dict[str, Any] = {
                "type": "weight",
                "filters": wfilters,
            }
            if wgroup.min_sum is not None:
                group_obj["value"] = {"min": wgroup.min_sum}
            stat_groups.append(group_obj)

    if plan.plain:
        stat_groups.append(
            {
                "type": "and",
                "filters": [
                    {
                        "id": request.match.id,
                        "value": {"min": request.min_value},
                        "disabled": False,
                    }
                    for request in plan.plain
                    if request.match is not None
                ],
            }
        )
    if pseudo_filters:
        stat_groups.append({"type": "and", "filters": list(pseudo_filters)})
    for conversion in plan.conversions:
        # PoE1 trade API rejects "type": "or"; a count group with min=1 is the supported OR form.
        stat_groups.append(
            {
                "type": "count",
                "value": {"min": 1},
                "filters": [
                    {
                        "id": stat_id,
                        "value": {"min": conversion.min_value},
                        "disabled": False,
                    }
                    for stat_id in conversion.stat_ids.values()
                ],
            }
        )

    query: dict[str, Any] = {"status": {"option": status_norm}}
    if stat_groups:
        query["stats"] = stat_groups

    filter_block: dict[str, Any] = {}
    type_filters: dict[str, Any] = {}
    if category:
        type_filters["category"] = {"option": category}
    if rarity and rarity.strip().lower() != "any":
        type_filters["rarity"] = {"option": rarity.strip().lower()}
    if item_level_min > 0:
        type_filters["ilvl"] = {"min": item_level_min}
    if type_filters:
        filter_block["type_filters"] = {"filters": type_filters}

    trade_filters: dict[str, Any] = {}
    if budget is not None and budget > 0:
        curr_norm = (currency or "").strip().lower()
        if curr_norm in ("", "chaos_equiv", "equiv"):
            price_filter: dict[str, Any] = {"max": budget}
        else:
            price_filter = {"option": curr_norm, "max": budget}
        trade_filters["price"] = price_filter
    if max_age_hours is not None and max_age_hours > 0:
        # The site only offers fixed windows: take the smallest one that covers the
        # limit; the caller trims the remainder from each listing's `indexed` time.
        window = next((opt for hours, opt in INDEXED_WINDOWS if max_age_hours <= hours), None)
        if window:
            trade_filters["indexed"] = {"option": window}
    if trade_filters:
        filter_block["trade_filters"] = {"filters": trade_filters}

    should_exclude = (
        (bool(plan.conversions) and not allow_corrupted)
        if exclude_corrupted_mirrored is None
        else bool(exclude_corrupted_mirrored)
    )
    if should_exclude:
        filter_block["misc_filters"] = {
            "filters": {
                "corrupted": {"option": "false"},
                "mirrored": {"option": "false"},
            }
        }

    if filter_block:
        query["filters"] = filter_block

    sort_norm = (search_sort or "asc").strip().lower()
    if sort_norm in ("sum", "weight"):
        if first_weight_idx is None:
            raise ValueError("--search-sort sum requires at least one --sum weighted group.")
        sort_block: dict[str, str] = {f"statgroup.{first_weight_idx}": "desc"}
    elif sort_norm in ("asc", "desc"):
        sort_block = {"price": sort_norm}
    else:
        raise ValueError(f"Invalid search_sort {search_sort!r}; expected asc, desc, or sum.")

    return {"query": query, "sort": sort_block}
