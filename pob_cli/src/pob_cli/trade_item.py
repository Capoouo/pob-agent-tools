"""Parse, rebuild, inspect, and Harvest-convert PoE1 3.30 trade/PoB item text.

Two sources are normalised into the same :class:`Item` / :class:`Mod` model:

* PoB clipboard text (``pob items --slot X --raw``), including ``{crafted}``,
  ``{fractured}``, ``{enchant}``, ``{scourge}``, ``{mutated}``, ``{crucible}``,
  and the ``Implicits: N`` block.
* Official PoE1 trade API ``/api/trade/fetch`` payloads (both 3.30 object
  ``explicitMods[].{description, flags, mods}`` and legacy string arrays +
  ``extended.mods.explicit``).

Harvest conversion implements PoE1 3.30 single-element same-tier swap rules
(Resistance and Elemental Damage) with strict legality and same-group conflict
guards.  Harvest preserves the modifier **tier** but re-rolls its numeric value
within that tier's range, so callers must always surface the re-roll caveat and
re-evaluate the resulting item in PoB.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
RANGE_RE = re.compile(r"\(\s*-?\d+(?:\.\d+)?\s*-\s*-?\d+(?:\.\d+)?\s*\)")
POB_TAG_RE = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_:.,-]*)\}")
SIMPLE_TAG_RE = re.compile(r"\{([a-zA-Z]+)\}")
TRAILING_KIND_RE = re.compile(
    r"\s*\((crafted|fractured|implicit|enchant|scourge|mutated|crucible|veiled|rune)\)\s*$",
    re.IGNORECASE,
)
SPACE_RE = re.compile(r"\s+")
SEPARATOR_RE = re.compile(r"^-{3,}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
PROPERTY_RE = re.compile(r"^([A-Za-z][A-Za-z '/()-]+):")

_GGG_TAG_RE = re.compile(r"<[^>]+>\{([^}]*)\}")
_GGG_LINK_PIPE_RE = re.compile(r"\[[^|\]]+\|([^|\]]+)\]")
_GGG_LINK_PLAIN_RE = re.compile(r"\[([^|\]]+)\]")

NEGATIVE_KEYWORDS: tuple[str, ...] = ("reduced", "less", "slower", "decreased", "lower")
NEGATIVE_KEYWORD_RE = re.compile(r"\b(?:reduced|less|slower|decreased|lower)\b")

POSITIVE_REDUCED_PATTERNS: tuple[str, ...] = (
    "reduced mana cost",
    "reduced cost of",
    "reduced attribute requirements",
    "reduced enemy stun threshold",
    "reduced extra damage from critical",
    "reduced reflected",
    "reduced damage taken",
    "reduced effect of curses on you",
    "reduced effect of chill",
    "reduced effect of shock",
    "reduced duration of ailments",
    "reduced soul gain prevention",
    "less damage taken",
    "less mana cost",
)

PROPERTY_KEYS: frozenset[str] = frozenset(
    {
        "item class",
        "rarity",
        "quality",
        "item level",
        "unique id",
        "levelreq",
        "level req",
        "level",
        "sockets",
        "implicits",
        "requirements",
        "requires",
        "requires level",
        "requires class",
        "str",
        "strength",
        "dex",
        "dexterity",
        "int",
        "intelligence",
        "armour",
        "evasion",
        "evasion rating",
        "energy shield",
        "ward",
        "physical damage",
        "elemental damage",
        "chaos damage",
        "attack speed",
        "attacks per second",
        "critical strike chance",
        "critical strike range",
        "chance to block",
        "block chance",
        "weapon range",
        "radius",
        "limited to",
        "memory strands",
        "talisman tier",
        "catalyst",
        "catalystquality",
        "intangibility",
        "cluster jewel skill",
        "cluster jewel node count",
        "prefix",
        "suffix",
        "variant",
        "selected variant",
        "version",
        "selected version",
        "allow duplicate variants",
        "league",
        "unreleased",
        "upgrade",
        "source",
        "stack size",
        "map tier",
        "item quantity",
        "item rarity",
        "monster pack size",
        "note",
    }
)

# Category order matters: check the most specific phrases first.
CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("penalty", ("requirement", "mana cost", "reservation", "cost of")),
    (
        "defense",
        (
            "damage taken",
            "block chance",
            "chance to block",
            "stun threshold",
            "extra damage from critical",
            "reduced reflected",
            "effect of curses on you",
            "effect of chill",
            "effect of shock",
            "duration of ailments",
            "suppress spell damage",
            "spell suppression",
        ),
    ),
    ("resist", ("resistance",)),
    ("life", ("maximum life", "life regeneration", "life leech", "life recoup", "life gained")),
    (
        "mana",
        (
            "maximum mana",
            "mana regeneration",
            "mana leech",
            "mana cost",
            "cost of",
            "reservation efficiency",
        ),
    ),
    ("energy_shield", ("energy shield",)),
    ("armour", ("armour",)),
    ("evasion", ("evasion",)),
    (
        "attributes",
        ("strength", "dexterity", "intelligence", "all attributes", "attributes", "attribute requirements"),
    ),
    ("skill_level", ("level of all", "level of")),
    ("speed", ("attack speed", "cast speed", "movement speed", "skill speed", "action speed")),
    ("crit", ("critical",)),
    ("accuracy", ("accuracy",)),
    ("damage", ("damage", "penetration", "ailment", "ignite", "bleed", "poison")),
    ("rarity", ("rarity",)),
)

FRAME_RARITY: dict[int, str] = {
    0: "NORMAL",
    1: "MAGIC",
    2: "RARE",
    3: "UNIQUE",
    4: "GEM",
    5: "CURRENCY",
    9: "RELIC",
}

INFLUENCE_API_MAP: tuple[tuple[str, str], ...] = (
    ("shaper", "Shaper"),
    ("elder", "Elder"),
    ("warlord", "Warlord"),
    ("hunter", "Hunter"),
    ("crusader", "Crusader"),
    ("redeemer", "Redeemer"),
    ("searing", "Searing Exarch"),
    ("tangled", "Eater of Worlds"),
)

INFLUENCE_LINE_MAP: dict[str, str] = {
    f"{display.lower()} item": display for _, display in INFLUENCE_API_MAP
}

STATE_LINES: frozenset[str] = frozenset(
    {
        "corrupted",
        "mirrored",
        "unidentified",
        "synthesised item",
        "fractured item",
        "split",
    }
    | set(INFLUENCE_LINE_MAP)
)

MOD_KIND_TAGS: frozenset[str] = frozenset(
    {
        "crafted",
        "fractured",
        "enchant",
        "scourge",
        "mutated",
        "implicit",
        "crucible",
        "veiled",
        "rune",
    }
)

NON_CONVERTIBLE_KINDS: frozenset[str] = frozenset(
    {
        "implicit",
        "enchant",
        "fractured",
        "crafted",
        "scourge",
        "crucible",
        "mutated",
        "veiled",
        "rune",
    }
)

# ---------------------------------------------------------------------------
# Harvest 3.30 rules (single-element only)
# ---------------------------------------------------------------------------

ELEMENTS: tuple[str, ...] = ("fire", "cold", "lightning")

ELEMENT_LABELS: dict[str, str] = {
    "fire": "Fire",
    "cold": "Cold",
    "lightning": "Lightning",
}


@dataclass(frozen=True)
class HarvestGroup:
    """One convertible Harvest modifier family (resistance or elemental damage)."""

    key: str
    kind: str
    elements: tuple[str, ...]
    craft: str
    cost: str
    patch: str
    note: str

    def craft_text(self, src: str, dst: str) -> str:
        return self.craft.format(
            src=ELEMENT_LABELS.get(src, src.title()),
            dst=ELEMENT_LABELS.get(dst, dst.title()),
        )

    def as_dict(self, src: str, dst: str) -> dict[str, Any]:
        return {
            "group": self.key,
            "kind": self.kind,
            "from": src,
            "to": dst,
            "craft": self.craft_text(src, dst),
            "cost": self.cost,
            "patch": self.patch,
            "rerolled": True,
            "note": self.note,
        }


RESISTANCE = HarvestGroup(
    key="resistance",
    kind="resistance",
    elements=ELEMENTS,
    craft=(
        "Change a modifier that grants {src} Resistance into a similar-tier "
        "modifier that grants {dst} Resistance"
    ),
    cost="300 Lifeforce (Primal/Vivid/Wild matching target)",
    patch="3.30",
    note="Same tier is kept, the value is re-rolled inside that tier's range.",
)

ELEMENTAL_DAMAGE = HarvestGroup(
    key="elemental_damage",
    kind="damage",
    elements=ELEMENTS,
    craft=(
        "Change a modifier that grants {src} Damage into a similar-tier "
        "modifier that grants {dst} Damage"
    ),
    cost="300 Lifeforce + 1 Crystallised Rancour",
    patch="3.30",
    note="Same tier is kept, the value is re-rolled inside that tier's range.",
)

HARVEST_GROUPS: tuple[HarvestGroup, ...] = (RESISTANCE, ELEMENTAL_DAMAGE)

_HARVEST_PATTERNS: tuple[tuple[re.Pattern[str], HarvestGroup, str], ...] = (
    (re.compile(r"^#% to (fire|cold|lightning) resistance$"), RESISTANCE, "resistance"),
    (re.compile(r"^adds # to # (fire|cold|lightning) damage$"), ELEMENTAL_DAMAGE, "added_damage_local"),
    (
        re.compile(r"^adds # to # (fire|cold|lightning) damage to attacks$"),
        ELEMENTAL_DAMAGE,
        "added_damage_attacks",
    ),
    (
        re.compile(r"^adds # to # (fire|cold|lightning) damage to spells$"),
        ELEMENTAL_DAMAGE,
        "added_damage_spells",
    ),
    (
        re.compile(r"^#% increased (fire|cold|lightning) damage$"),
        ELEMENTAL_DAMAGE,
        "increased_damage",
    ),
)


@dataclass(frozen=True)
class HarvestMatch:
    """A normalized modifier template that belongs to a convertible Harvest group."""

    group: HarvestGroup
    element: str
    subkind: str

    @property
    def element_label(self) -> str:
        return ELEMENT_LABELS.get(self.element, self.element.title())


def match_harvest(template: str) -> HarvestMatch | None:
    """Return the Harvest conversion group, element, and subkind for a template."""
    value = (template or "").strip().lower()
    if not value:
        return None
    for pattern, group, subkind in _HARVEST_PATTERNS:
        found = pattern.match(value)
        if found:
            return HarvestMatch(group=group, element=found.group(1), subkind=subkind)
    return None


def is_convertible_template(template: str) -> bool:
    """Return True if ``template`` is eligible for PoE1 3.30 single-element Harvest swap."""
    return match_harvest(template) is not None


def convert_template(template: str, target: str) -> str | None:
    """Rewrite a convertible template to ``target`` keeping its exact shape."""
    found = match_harvest(template)
    if found is None:
        return None
    if target not in found.group.elements:
        raise ValueError(f"Unsupported conversion target: {target!r}")
    normalized = (template or "").strip().lower()
    return re.sub(r"\b(fire|cold|lightning)\b", target, normalized, count=1)


def conversion_note(template: str, target: str) -> str:
    """Human/agent-facing warning for one converted modifier."""
    found = match_harvest(template)
    if found is None:
        return ""
    if target not in found.group.elements:
        raise ValueError(f"Unsupported conversion target: {target!r}")
    src = found.element_label
    dst = ELEMENT_LABELS.get(target, target.title())
    group = found.group
    return (
        f"{src} -> {dst}: {group.craft_text(found.element, target)} "
        f"({group.patch}); cost: {group.cost}. "
        "Tier is kept but the roll is randomized inside the tier - the listed "
        "value is a best case, re-check the item in PoB before buying."
    )


# ---------------------------------------------------------------------------
# Sanitization, GGG string cleaning, template normalization, classification
# ---------------------------------------------------------------------------


def sanitize_text(text: object, limit: int = 300) -> str:
    """Strip control characters and collapse whitespace from untrusted trade text."""
    raw = "" if text is None else str(text)
    value = CONTROL_RE.sub(" ", raw)
    value = SPACE_RE.sub(" ", value).strip()
    return value[:limit]


def escape_ggg_string(text: str) -> str:
    """Mirror PoB ``Common.lua`` ``escapeGGGString`` for keyword/tag markup.

    * ``<tag>{content}`` -> ``content`` (e.g. ``<augmented>{+42}`` -> ``+42``)
    * ``[Key|Display Text]`` -> ``Display Text``
    * ``[Key]`` -> ``Key``
    """
    value = "" if text is None else str(text)
    value = _GGG_TAG_RE.sub(r"\1", value)
    value = _GGG_LINK_PIPE_RE.sub(r"\1", value)
    value = _GGG_LINK_PLAIN_RE.sub(r"\1", value)
    return value


def normalize_template(text: str) -> str:
    """Normalise a modifier line into a deterministic numeric-free template.

    ``{crafted}+(35–45)% to [Cold|Cold] Resistance`` -> ``#% to cold resistance``
    """
    value = escape_ggg_string("" if text is None else str(text))
    value = POB_TAG_RE.sub("", value)
    value = TRAILING_KIND_RE.sub("", value)
    value = value.strip().lower()
    value = value.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    value = RANGE_RE.sub("#", value)
    value = NUMBER_RE.sub("#", value)
    value = value.replace("+", "")
    value = SPACE_RE.sub(" ", value).strip(" .,")
    return value


def _is_positive_reduced(template: str) -> bool:
    lowered = (template or "").strip().lower()
    return any(pattern in lowered for pattern in POSITIVE_REDUCED_PATTERNS)


def classify_mod(template: str) -> str:
    """Classify a normalised modifier template into a semantic bucket."""
    lowered = (template or "").strip().lower()
    if not lowered:
        return "other"

    positive_reduced = _is_positive_reduced(lowered)
    if positive_reduced:
        if "mana cost" in lowered or "cost of" in lowered:
            return "mana"
        if "attribute requirements" in lowered:
            return "attributes"

    is_reservation_efficiency = "reservation efficiency" in lowered

    for category, keywords in CATEGORY_RULES:
        if category == "penalty" and (positive_reduced or is_reservation_efficiency):
            continue
        if any(keyword in lowered for keyword in keywords):
            return category
    return "other"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Mod:
    """One normalised modifier line."""

    raw: str
    template: str
    values: tuple[float, ...]
    kind: str = "explicit"
    tags: tuple[str, ...] = ()
    line_index: int = -1

    @property
    def category(self) -> str:
        return classify_mod(self.template)

    @property
    def signed_value(self) -> float:
        """Numeric magnitude with genuine negative mods flipped so bigger is better."""
        if self.template.startswith("adds ") and len(self.values) == 2:
            total = (self.values[0] + self.values[1]) / 2.0
        else:
            total = float(sum(self.values))
        if NEGATIVE_KEYWORD_RE.search(self.template) and not _is_positive_reduced(self.template):
            total = -total
        return total


@dataclass
class Item:
    """A parsed PoE1 item: header fields, flags, exact lines, and parsed mods."""

    rarity: str = "RARE"
    name: str = ""
    base_type: str = ""
    item_level: int = 0
    corrupted: bool = False
    mirrored: bool = False
    unidentified: bool = False
    synthesised: bool = False
    influences: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    mods: list[Mod] = field(default_factory=list)
    fractured: bool = False
    split: bool = False

    @property
    def display_name(self) -> str:
        if self.name and self.base_type and self.name.lower() != self.base_type.lower():
            return f"{self.name} ({self.base_type})"
        return self.base_type or self.name or "(unknown item)"


@dataclass(frozen=True)
class Conversion:
    """One modifier rewritten to another element via Harvest."""

    template: str
    converted_template: str
    element: str
    target: str
    group: str
    subkind: str
    values: tuple[float, ...]
    cost: str
    patch: str
    line: str
    tier: str = ""
    tier_range: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.template,
            "to": self.converted_template,
            "element": self.element,
            "target": self.target,
            "group": self.group,
            "subkind": self.subkind,
            "values": list(self.values),
            "cost": self.cost,
            "patch": self.patch,
            "tier": self.tier,
            "tier_range": self.tier_range,
            "rerolled": True,
            "line": self.line,
        }


@dataclass
class ConvertedItem:
    text: str
    item: Item
    conversions: list[Conversion] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Item text builder (PoE1 3.30 + legacy trade API JSON -> PoB clipboard text)
# ---------------------------------------------------------------------------


def _extract_mod_lines(entries: Any, extra_tag: str | None = None) -> list[str]:
    """Extract physical PoB mod lines from 3.30 dict entries or legacy strings.

    Splits multiline entries on ``\\n`` so every physical line receives its
    ``{tag}`` prefix without duplicating tags already present in the text.
    """
    if not isinstance(entries, list):
        return []

    out: list[str] = []
    extra = extra_tag.strip().lower() if extra_tag else None

    for entry in entries:
        raw_text = ""
        ordered_tags: list[str] = []
        if extra:
            ordered_tags.append(extra)

        if isinstance(entry, str):
            raw_text = entry
        elif isinstance(entry, dict):
            raw_text = str(entry.get("description") or entry.get("text") or "")
            flags = entry.get("flags")
            if isinstance(flags, dict):
                for flag_name, flag_val in flags.items():
                    if flag_val:
                        tag_key = str(flag_name).strip().lower()
                        if tag_key and tag_key not in ordered_tags:
                            ordered_tags.append(tag_key)
        else:
            continue

        if not raw_text:
            continue

        for part in raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            cleaned = escape_ggg_string(sanitize_text(part))
            if not cleaned:
                continue
            existing_tags = {tag.lower() for tag in SIMPLE_TAG_RE.findall(cleaned)}
            prefix = "".join(f"{{{tag}}}" for tag in ordered_tags if tag not in existing_tags)
            out.append(f"{prefix}{cleaned}")
    return out


def _format_sockets(sockets: Any) -> str:
    """Format trade API socket array into PoB ``R-G-B R-B W`` notation."""
    if not isinstance(sockets, list) or not sockets:
        return ""
    groups: dict[int, list[str]] = {}
    group_order: list[int] = []
    for socket in sockets:
        if not isinstance(socket, dict):
            continue
        colour = sanitize_text(socket.get("sColour"), 8).upper()
        if not colour:
            continue
        try:
            group_id = int(socket.get("group", 0))
        except (TypeError, ValueError):
            group_id = 0
        if group_id not in groups:
            groups[group_id] = []
            group_order.append(group_id)
        groups[group_id].append(colour)
    if not group_order:
        return ""
    return " ".join("-".join(groups[gid]) for gid in group_order)


def _format_property_line(prop: Any) -> str:
    """Format one trade API property dict into a PoB header line, or empty to skip."""
    if not isinstance(prop, dict):
        return ""
    pname = escape_ggg_string(sanitize_text(prop.get("name"), 120))
    if not pname:
        return ""
    values = prop.get("values")
    if not isinstance(values, list) or not values:
        return ""
    extracted: list[str] = []
    for pair in values:
        if isinstance(pair, (list, tuple)) and pair:
            val_text = escape_ggg_string(sanitize_text(pair[0], 120))
            if val_text:
                extracted.append(val_text)
    if not extracted:
        return ""
    return f"{pname}: {', '.join(extracted)}"


def build_item_text(raw_item: dict[str, Any]) -> str:
    """Build high-fidelity PoB clipboard text from a PoE1 trade fetch row or item dict."""
    if not isinstance(raw_item, dict):
        raise ValueError("trade item payload is not an object")
    item = raw_item.get("item") if isinstance(raw_item.get("item"), dict) else raw_item
    if not isinstance(item, dict):
        raise ValueError("trade item payload is not an object")

    raw_rarity = str(item.get("rarity") or "").strip().upper()
    rarity = raw_rarity if raw_rarity else FRAME_RARITY.get(item.get("frameType"), "RARE")

    name = escape_ggg_string(sanitize_text(item.get("name"), 120))
    type_line = escape_ggg_string(sanitize_text(item.get("typeLine") or item.get("baseType"), 120))
    if not type_line:
        raise ValueError("trade item has no base type")

    lines: list[str] = [f"Rarity: {rarity}"]
    if name:
        lines.append(name)
    lines.append(type_line)

    for prop in item.get("properties") or []:
        prop_line = _format_property_line(prop)
        if prop_line:
            lines.append(prop_line)

    ilvl = item.get("ilvl") or item.get("itemLevel")
    if ilvl is not None and ilvl != "":
        try:
            ilvl_int = int(ilvl)
        except (TypeError, ValueError):
            ilvl_int = 0
        if ilvl_int > 0:
            lines.append(f"Item Level: {ilvl_int}")

    socket_str = _format_sockets(item.get("sockets"))
    if socket_str:
        lines.append(f"Sockets: {socket_str}")

    enchant_lines = _extract_mod_lines(item.get("enchantMods"), extra_tag="enchant")
    scourge_lines = _extract_mod_lines(item.get("scourgeMods"), extra_tag="scourge")
    implicit_lines = _extract_mod_lines(item.get("implicitMods"))
    implicit_block = enchant_lines + scourge_lines + implicit_lines

    lines.append(f"Implicits: {len(implicit_block)}")
    lines.extend(implicit_block)

    explicit_lines = _extract_mod_lines(item.get("explicitMods"))
    lines.extend(explicit_lines)

    explicit_counts = Counter(explicit_lines)
    for field_name, tag in (
        ("fracturedMods", "fractured"),
        ("craftedMods", "crafted"),
        ("mutatedMods", "mutated"),
        ("veiledMods", "veiled"),
    ):
        for mod_line in _extract_mod_lines(item.get(field_name), extra_tag=tag):
            if explicit_counts.get(mod_line, 0) > 0:
                explicit_counts[mod_line] -= 1
                continue
            lines.append(mod_line)

    influences = item.get("influences") if isinstance(item.get("influences"), dict) else {}
    for api_key, display in INFLUENCE_API_MAP:
        if influences.get(api_key) or item.get(api_key):
            lines.append(f"{display} Item")

    if item.get("synthesised"):
        lines.append("Synthesised Item")
    if item.get("fractured"):
        lines.append("Fractured Item")
    if item.get("split"):
        lines.append("Split")
    if item.get("duplicated"):
        lines.append("Mirrored")
    if item.get("corrupted"):
        lines.append("Corrupted")
    if item.get("identified") is False:
        lines.append("Unidentified")

    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# Tier extraction (3.30 explicitMods[].mods[] + legacy extended.mods.explicit)
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return None


def _extract_tier_meta(
    mod_obj: Any,
    *,
    preferred_hash: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(mod_obj, dict):
        return None
    tier = sanitize_text(mod_obj.get("tier") or "", 40)
    name = sanitize_text(mod_obj.get("name") or "", 120)
    level = _to_int(mod_obj.get("level"))

    min_val = _to_float(mod_obj.get("min"))
    max_val = _to_float(mod_obj.get("max"))

    magnitudes = mod_obj.get("magnitudes")
    if (min_val is None or max_val is None) and isinstance(magnitudes, list):
        chosen = None
        if preferred_hash:
            for mag in magnitudes:
                if isinstance(mag, dict) and mag.get("hash") == preferred_hash:
                    chosen = mag
                    break
        if chosen is None:
            for mag in magnitudes:
                if isinstance(mag, dict) and (mag.get("min") is not None or mag.get("max") is not None):
                    chosen = mag
                    break
        if isinstance(chosen, dict):
            if min_val is None:
                min_val = _to_float(chosen.get("min"))
            if max_val is None:
                max_val = _to_float(chosen.get("max"))

    if not tier and not name and level is None and min_val is None and max_val is None:
        return None

    return {
        "tier": tier,
        "name": name,
        "level": level,
        "min": min_val,
        "max": max_val,
    }


def _values_fit_mod(values: tuple[float, ...], mod_obj: dict[str, Any]) -> bool:
    if not values:
        return False
    magnitudes = mod_obj.get("magnitudes")
    if not isinstance(magnitudes, list) or not magnitudes:
        return False
    for mag in magnitudes:
        if not isinstance(mag, dict):
            continue
        lo = _to_float(mag.get("min"))
        hi = _to_float(mag.get("max"))
        if lo is None or hi is None:
            continue
        low_b, high_b = (lo, hi) if lo <= hi else (hi, lo)
        if any(low_b <= v <= high_b for v in values):
            return True
    return False


def trade_mod_tiers(raw_item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map normalized explicit modifier templates to tier metadata dicts.

    Supports both PoE1 3.30 ``explicitMods[].mods[]`` and legacy
    ``extended.mods.explicit[]`` (matched via ``extended.hashes.explicit``,
    magnitude range matching, or positional fallback).
    """
    if not isinstance(raw_item, dict):
        return {}
    item = raw_item.get("item") if isinstance(raw_item.get("item"), dict) else raw_item
    if not isinstance(item, dict):
        return {}

    tiers: dict[str, dict[str, Any]] = {}
    explicit_entries = item.get("explicitMods") or []

    # 1. PoE1 3.30 structured explicitMods[].mods[]
    if isinstance(explicit_entries, list):
        for entry in explicit_entries:
            if not isinstance(entry, dict):
                continue
            raw_desc = str(entry.get("description") or entry.get("text") or "")
            mods_list = entry.get("mods")
            if not raw_desc or not isinstance(mods_list, list):
                continue
            meta = None
            for mod_obj in mods_list:
                meta = _extract_tier_meta(mod_obj)
                if meta is not None:
                    break
            if meta is None:
                continue
            for part in raw_desc.replace("\r\n", "\n").split("\n"):
                template = normalize_template(part)
                if template:
                    tiers.setdefault(template, meta)

    # 2. Legacy extended.mods.explicit[] + extended.hashes.explicit[]
    extended = item.get("extended")
    if not isinstance(extended, dict):
        return tiers
    ext_mods_root = extended.get("mods")
    ext_explicit = ext_mods_root.get("explicit") if isinstance(ext_mods_root, dict) else None
    if not isinstance(ext_explicit, list) or not ext_explicit:
        return tiers

    plain_lines: list[str] = []
    if isinstance(explicit_entries, list):
        for entry in explicit_entries:
            if isinstance(entry, str):
                plain_lines.append(entry)
            elif isinstance(entry, dict):
                plain_lines.append(str(entry.get("description") or entry.get("text") or ""))

    ext_hashes_root = extended.get("hashes")
    ext_hashes = ext_hashes_root.get("explicit") if isinstance(ext_hashes_root, dict) else None

    if isinstance(ext_hashes, list) and ext_hashes:
        for idx, hash_entry in enumerate(ext_hashes):
            if idx >= len(plain_lines):
                break
            template = normalize_template(plain_lines[idx])
            if not template or template in tiers:
                continue
            if not isinstance(hash_entry, (list, tuple)) or len(hash_entry) < 2:
                continue
            stat_hash = str(hash_entry[0]) if hash_entry[0] is not None else None
            mod_indices = hash_entry[1]
            if not isinstance(mod_indices, list):
                continue
            for mod_idx in mod_indices:
                if isinstance(mod_idx, int) and 0 <= mod_idx < len(ext_explicit):
                    meta = _extract_tier_meta(ext_explicit[mod_idx], preferred_hash=stat_hash)
                    if meta is not None:
                        tiers.setdefault(template, meta)
                        break
        return tiers

    # Fallback without hashes: match by magnitude range first, then positional if lengths match
    claimed: set[int] = set()
    for line in plain_lines:
        template = normalize_template(line)
        if not template or template in tiers:
            continue
        clean = escape_ggg_string(line).replace("\u2212", "-")
        vals = tuple(float(v) for v in NUMBER_RE.findall(clean))
        for mod_idx, mod_obj in enumerate(ext_explicit):
            if mod_idx in claimed or not isinstance(mod_obj, dict):
                continue
            if _values_fit_mod(vals, mod_obj):
                meta = _extract_tier_meta(mod_obj)
                if meta is not None:
                    tiers[template] = meta
                    claimed.add(mod_idx)
                    break

    if len(plain_lines) == len(ext_explicit):
        for idx, line in enumerate(plain_lines):
            template = normalize_template(line)
            if not template or template in tiers or idx in claimed:
                continue
            meta = _extract_tier_meta(ext_explicit[idx])
            if meta is not None:
                tiers[template] = meta
                claimed.add(idx)

    return tiers


# ---------------------------------------------------------------------------
# Item text parser
# ---------------------------------------------------------------------------


def _first_int(value: str) -> int:
    match = NUMBER_RE.search(str(value).replace("+", ""))
    if not match:
        return 0
    try:
        return int(float(match.group(0)))
    except ValueError:
        return 0


def _is_property_line(line: str) -> bool:
    match = PROPERTY_RE.match(line)
    if not match:
        return False
    key = match.group(1).strip().lower()
    if key in PROPERTY_KEYS:
        return True
    if key.startswith("quality (") and key.endswith(")"):
        return True
    if key.endswith("basepercentile"):
        return True
    if key.startswith("selected ") or key.startswith("has alt variant"):
        return True
    return False


def _is_meta_or_property_line(line: str) -> bool:
    lowered = line.strip().lower()
    if not lowered:
        return True
    if lowered in STATE_LINES:
        return True
    if lowered.startswith(("implicits:", "item level:", "rarity:", "item class:")):
        return True
    if lowered.startswith("foil unique"):
        return True
    return _is_property_line(line)


def _kind_from(tags: tuple[str, ...], implicit_remaining: int) -> str:
    for tag in tags:
        if tag in MOD_KIND_TAGS:
            return tag
    return "implicit" if implicit_remaining > 0 else "explicit"


def make_mod(
    raw: str,
    *,
    kind: str = "explicit",
    tags: tuple[str, ...] = (),
    line_index: int = -1,
) -> Mod:
    """Create a :class:`Mod` from a single raw modifier line."""
    unwrapped = escape_ggg_string("" if raw is None else str(raw))
    clean = POB_TAG_RE.sub("", unwrapped)
    clean = TRAILING_KIND_RE.sub("", clean).strip()
    template = normalize_template(clean)
    numeric_source = clean.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    values = tuple(float(value) for value in NUMBER_RE.findall(numeric_source))
    return Mod(
        raw=clean,
        template=template,
        values=values,
        kind=kind,
        tags=tags,
        line_index=line_index,
    )


def parse_item_text(text: str) -> Item:
    """Parse PoB clipboard text or text produced by :func:`build_item_text`."""
    raw_lines = [line.strip() for line in str(text or "").replace("\r\n", "\n").split("\n")]
    lines = [line for line in raw_lines if line and not SEPARATOR_RE.match(line)]
    if not lines:
        raise ValueError("item text is empty")

    item = Item(lines=lines)
    index = 0
    if lines[index].lower().startswith("item class:"):
        index += 1

    if index < len(lines) and lines[index].lower().startswith("rarity:"):
        item.rarity = lines[index].split(":", 1)[1].strip().upper()
        index += 1

    if item.rarity in {"RARE", "UNIQUE", "RELIC"}:
        if index < len(lines):
            first = escape_ggg_string(sanitize_text(lines[index], 120))
            index += 1
            if index < len(lines) and not _is_meta_or_property_line(lines[index]):
                item.name = first
                item.base_type = escape_ggg_string(sanitize_text(lines[index], 120))
                index += 1
            else:
                item.name = ""
                item.base_type = first
    else:
        if index < len(lines):
            item.base_type = escape_ggg_string(sanitize_text(lines[index], 120))
            index += 1

    implicit_remaining = 0
    for line_index in range(index, len(lines)):
        line = lines[line_index]
        lowered = line.lower()

        if lowered.startswith("implicits:"):
            implicit_remaining = _first_int(line.split(":", 1)[1])
            continue
        if lowered.startswith("item level:"):
            item.item_level = _first_int(line.split(":", 1)[1])
            continue
        if lowered == "corrupted":
            item.corrupted = True
            continue
        if lowered == "mirrored":
            item.mirrored = True
            continue
        if lowered == "unidentified":
            item.unidentified = True
            continue
        if lowered == "synthesised item":
            item.synthesised = True
            continue
        if lowered == "fractured item":
            item.fractured = True
            continue
        if lowered == "split":
            item.split = True
            continue
        if lowered in INFLUENCE_LINE_MAP:
            display = INFLUENCE_LINE_MAP[lowered]
            if display not in item.influences:
                item.influences.append(display)
            continue
        if lowered.startswith("foil unique"):
            continue
        if _is_property_line(line):
            continue

        unwrapped = escape_ggg_string(line)
        tags_list = [tag.lower() for tag in SIMPLE_TAG_RE.findall(unwrapped)]
        trailing = TRAILING_KIND_RE.search(unwrapped)
        if trailing:
            trailing_tag = trailing.group(1).lower()
            if trailing_tag not in tags_list:
                tags_list.append(trailing_tag)
        tags = tuple(tags_list)

        clean = POB_TAG_RE.sub("", unwrapped)
        clean = TRAILING_KIND_RE.sub("", clean).strip()
        if not clean:
            continue

        kind = _kind_from(tags, implicit_remaining)
        if implicit_remaining > 0:
            implicit_remaining -= 1

        item.mods.append(make_mod(clean, kind=kind, tags=tags, line_index=line_index))

    return item


def mods_by_template(item: Item | None) -> dict[str, float]:
    """Sum signed values per template so duplicate mods stack correctly."""
    result: dict[str, float] = {}
    if item is None:
        return result
    for mod in item.mods:
        result[mod.template] = result.get(mod.template, 0.0) + mod.signed_value
    return result


# ---------------------------------------------------------------------------
# Strict Harvest conversion
# ---------------------------------------------------------------------------


def _rewrite_element(line: str, target: str) -> str:
    return re.sub(
        r"\b(fire|cold|lightning)\b",
        ELEMENT_LABELS.get(target, target.capitalize()),
        line,
        count=1,
        flags=re.IGNORECASE,
    )


def _fmt_roll(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def _resolve_tier_info(
    tiers: dict[str, Any],
    template: str,
) -> tuple[str, str]:
    entry = tiers.get(template)
    if isinstance(entry, str):
        return entry, ""
    if isinstance(entry, dict):
        tier_label = str(entry.get("tier") or "")
        lo = _to_float(entry.get("min"))
        hi = _to_float(entry.get("max"))
        if lo is not None and hi is not None:
            return tier_label, f"{_fmt_roll(lo)}-{_fmt_roll(hi)}"
        return tier_label, ""
    return "", ""


def convert_item_text(
    text: str,
    target: str,
    *,
    force: bool = False,
    group: str | None = None,
    tiers: dict[str, Any] | None = None,
) -> ConvertedItem:
    """Rewrite convertible explicit modifiers to ``target`` (``fire``/``cold``/``lightning``).

    Enforces PoE1 3.30 Harvest rules:
    * Refuses unidentified, corrupted, mirrored, and non-MAGIC/RARE items unless ``force=True``.
    * Refuses non-explicit modifiers (implicit, enchant, fractured, crafted,
      scourge, crucible, mutated, veiled, rune).
    * Enforces same-group conflict checks: skips if an explicit/fractured target
      modifier already exists in the same ``(group, subkind)``; warns (but allows
      converting the natural explicit) if a crafted target modifier exists; and
      converts at most ONE modifier per ``(group, subkind)``, choosing the highest
      ``signed_value`` candidate first.
    """
    if target not in ELEMENTS:
        raise ValueError(f"Unsupported conversion target: {target!r}")

    tier_map = tiers or {}
    item = parse_item_text(text)

    if not force:
        blockers: list[str] = []
        if item.unidentified:
            blockers.append("Unidentified items cannot use Harvest conversion.")
        if item.corrupted:
            blockers.append("Corrupted items cannot be modified with Harvest conversion.")
        if item.mirrored:
            blockers.append("Mirrored items cannot be modified with Harvest conversion.")
        if item.rarity not in {"MAGIC", "RARE"}:
            blockers.append(
                f"{item.rarity.title()} items cannot use Harvest conversion (magic/rare only)."
            )
        if blockers:
            return ConvertedItem(text=text, item=item, skipped=blockers)

    existing_locked: set[tuple[str, str, str]] = set()
    existing_crafted: set[tuple[str, str, str]] = set()
    for mod in item.mods:
        found = match_harvest(mod.template)
        if found is None:
            continue
        key = (found.group.key, found.subkind, found.element)
        if mod.kind in {"explicit", "fractured"}:
            existing_locked.add(key)
        elif mod.kind == "crafted":
            existing_crafted.add(key)

    skipped: list[str] = []
    warnings: list[str] = []
    candidates: list[tuple[Mod, HarvestMatch]] = []

    for mod in item.mods:
        found = match_harvest(mod.template)
        if found is None:
            continue
        if group is not None and found.group.key != group:
            continue
        if mod.kind in NON_CONVERTIBLE_KINDS:
            if found.element != target:
                skipped.append(f"{mod.raw}: {mod.kind} modifiers cannot be converted")
            continue
        if found.element == target:
            continue
        candidates.append((mod, found))

    # Choose the strongest tier / highest signed_value candidate first within each group.
    candidates.sort(key=lambda pair: (-pair[0].signed_value, pair[0].line_index))

    lines = list(item.lines)
    conversions: list[Conversion] = []
    converted_groups: set[tuple[str, str]] = set()
    warned_crafted_groups: set[tuple[str, str]] = set()

    for mod, found in candidates:
        group_sub = (found.group.key, found.subkind)
        if (found.group.key, found.subkind, target) in existing_locked:
            skipped.append(
                f"{mod.raw}: item already has target {target} modifier in the same group"
            )
            continue
        if group_sub in converted_groups:
            skipped.append(
                f"{mod.raw}: another higher-value modifier in the same group was already converted to {target}"
            )
            continue

        new_line = _rewrite_element(mod.raw, target)
        if new_line == mod.raw or mod.line_index < 0:
            skipped.append(f"{mod.raw}: could not rewrite the element in place")
            continue

        if (
            (found.group.key, found.subkind, target) in existing_crafted
            and group_sub not in warned_crafted_groups
        ):
            warnings.append(
                "Item has a crafted target modifier; remove it at the crafting bench before Harvest conversion."
            )
            warned_crafted_groups.add(group_sub)

        lines[mod.line_index] = new_line
        converted_groups.add(group_sub)
        tier_label, tier_range = _resolve_tier_info(tier_map, mod.template)
        conversions.append(
            Conversion(
                template=mod.template,
                converted_template=convert_template(mod.template, target) or "",
                element=found.element,
                target=target,
                group=found.group.key,
                subkind=found.subkind,
                values=mod.values,
                cost=found.group.cost,
                patch=found.group.patch,
                line=new_line,
                tier=tier_label,
                tier_range=tier_range,
            )
        )

    converted_text = "\n".join(lines)
    return ConvertedItem(
        text=converted_text,
        item=parse_item_text(converted_text),
        conversions=conversions,
        skipped=skipped,
        warnings=warnings,
    )


def conversion_summary(converted: ConvertedItem) -> str:
    """Short human/agent-facing summary of a Harvest conversion run."""
    if not converted.conversions:
        return "no convertible modifiers found"
    parts: list[str] = []
    for conv in converted.conversions:
        tier_part = ""
        if conv.tier and conv.tier_range:
            tier_part = f" [{conv.tier} {conv.tier_range}]"
        elif conv.tier:
            tier_part = f" [{conv.tier}]"
        parts.append(
            f"{conv.template} -> {conv.converted_template}{tier_part} "
            f"(cost: {conv.cost}; tier kept, roll re-rolled)"
        )
    return "; ".join(parts)
