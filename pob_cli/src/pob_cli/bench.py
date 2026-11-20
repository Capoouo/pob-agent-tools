"""Authoritative PoE1 crafting bench catalog and bilingual search.

Dual-source design:
- Primary (when local PoB engine is installed): reads `ModMaster.lua` records
  from the local `game_data` SQLite index (`source = 'Bench'`), guaranteeing
  100% authoritative GGPK Prefix/Suffix classification, exact PoB item types,
  bilingual zh-rCN translations, and automatic updates via `pob update`,
  overlaid with crafting costs and unlock locations from `data/bench_costs.json`.
- Fallback (zero-engine / CI / fresh install before `pob bootstrap`): loads the
  clean embedded snapshot inside `data/bench_costs.json`.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable, Sequence

from . import game_data

DATA_FILE = Path(__file__).resolve().parent / "data" / "bench_costs.json"
UNKNOWN_COST = "unknown (verify in-game)"

_DASH_RE = re.compile(r"[\u2013\u2014\u2212]")
_LEADING_TAG_RE = re.compile(r"^(?:\{[^}]*\}\s*)+")
_NEG_RANGE_RE = re.compile(r"-\(\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*\)")
_RANGE_RE = re.compile(r"\(\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*\)")
_TEMPLATE_RANGE_RE = re.compile(r"[+-]?\(\s*-?\d+(?:\.\d+)?\s*-\s*-?\d+(?:\.\d+)?\s*\)")
_TEMPLATE_NUM_RE = re.compile(r"(?<![a-z0-9_])[+-]?\d+(?:\.\d+)?")
_VALUE_TOKEN_RE = re.compile(
    r"-\(\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*\)"
    r"|\(\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*\)"
    r"|(?<![a-zA-Z0-9_])([+-]?\d+(?:\.\d+)?)"
)
_EN_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_ZH_NOISE_RE = re.compile(r"[\s+\-()（）%#.,，。:：/0-9]+")

_EN_STOPWORDS: frozenset[str] = frozenset(
    {"to", "of", "the", "a", "an", "and", "in", "on", "with", "for", "by", "as", "is", "are", "have", "your", "you"}
)

# Authoritative PoB ModMaster.lua `types` -> normalized CLI slot keys
POB_TYPE_TO_SLOTS: dict[str, tuple[str, ...]] = {
    "Helmet": ("helmet",),
    "Body Armour": ("body_armour",),
    "Gloves": ("gloves",),
    "Boots": ("boots",),
    "Shield": ("shield", "weapon2"),
    "Quiver": ("quiver", "weapon2"),
    "Ring": ("ring1", "ring2", "ring3"),
    "Amulet": ("amulet",),
    "Belt": ("belt",),
    "Flask": ("flask",),
    "Bow": ("bow", "weapon1"),
    "Staff": ("staff", "weapon1"),
    "Two Handed Sword": ("two_handed_sword", "weapon1"),
    "Two Handed Axe": ("two_handed_axe", "weapon1"),
    "Two Handed Mace": ("two_handed_mace", "weapon1"),
    "Wand": ("wand", "weapon1", "weapon2"),
    "Claw": ("claw", "weapon1", "weapon2"),
    "Dagger": ("dagger", "weapon1", "weapon2"),
    "Sceptre": ("sceptre", "weapon1", "weapon2"),
    "One Handed Sword": ("one_handed_sword", "weapon1", "weapon2"),
    "Thrusting One Handed Sword": ("one_handed_sword", "weapon1", "weapon2"),
    "One Handed Axe": ("one_handed_axe", "weapon1", "weapon2"),
    "One Handed Mace": ("one_handed_mace", "weapon1", "weapon2"),
}

_SLOT_ALIASES: dict[str, str] = {
    "helm": "helmet",
    "head": "helmet",
    "helmet": "helmet",
    "chest": "body_armour",
    "body": "body_armour",
    "bodyarmour": "body_armour",
    "body_armour": "body_armour",
    "bodyarmor": "body_armour",
    "body_armor": "body_armour",
    "armour": "body_armour",
    "armor": "body_armour",
    "glove": "gloves",
    "gloves": "gloves",
    "hand": "gloves",
    "hands": "gloves",
    "boot": "boots",
    "boots": "boots",
    "foot": "boots",
    "feet": "boots",
    "shield": "shield",
    "quiver": "quiver",
    "ring": "ring1",
    "ring1": "ring1",
    "ring_1": "ring1",
    "left_ring": "ring1",
    "ring2": "ring2",
    "ring_2": "ring2",
    "right_ring": "ring2",
    "ring3": "ring3",
    "ring_3": "ring3",
    "amulet": "amulet",
    "neck": "amulet",
    "belt": "belt",
    "waist": "belt",
    "flask": "flask",
    "flask1": "flask",
    "flask_1": "flask",
    "flask2": "flask",
    "flask_2": "flask",
    "flask3": "flask",
    "flask_3": "flask",
    "flask4": "flask",
    "flask_4": "flask",
    "flask5": "flask",
    "flask_5": "flask",
    "weapon": "weapon1",
    "weapon1": "weapon1",
    "weapon_1": "weapon1",
    "mainhand": "weapon1",
    "main_hand": "weapon1",
    "mh": "weapon1",
    "weapon2": "weapon2",
    "weapon_2": "weapon2",
    "offhand": "weapon2",
    "off_hand": "weapon2",
    "oh": "weapon2",
    "bow": "bow",
    "bows": "bow",
    "wand": "wand",
    "wands": "wand",
    "claw": "claw",
    "claws": "claw",
    "dagger": "dagger",
    "daggers": "dagger",
    "rune_dagger": "dagger",
    "sceptre": "sceptre",
    "sceptres": "sceptre",
    "scepter": "sceptre",
    "scepters": "sceptre",
    "staff": "staff",
    "staves": "staff",
    "warstaff": "staff",
    "sword": "one_handed_sword",
    "1h_sword": "one_handed_sword",
    "one_hand_sword": "one_handed_sword",
    "one_handed_sword": "one_handed_sword",
    "thrusting_one_handed_sword": "one_handed_sword",
    "thrusting_sword": "one_handed_sword",
    "2h_sword": "two_handed_sword",
    "two_hand_sword": "two_handed_sword",
    "two_handed_sword": "two_handed_sword",
    "axe": "one_handed_axe",
    "1h_axe": "one_handed_axe",
    "one_hand_axe": "one_handed_axe",
    "one_handed_axe": "one_handed_axe",
    "2h_axe": "two_handed_axe",
    "two_hand_axe": "two_handed_axe",
    "two_handed_axe": "two_handed_axe",
    "mace": "one_handed_mace",
    "1h_mace": "one_handed_mace",
    "one_hand_mace": "one_handed_mace",
    "one_handed_mace": "one_handed_mace",
    "2h_mace": "two_handed_mace",
    "two_hand_mace": "two_handed_mace",
    "two_handed_mace": "two_handed_mace",
    "all": "all",
    "*": "all",
}


def slot_key(slot: str) -> str:
    """Normalize a user/CLI slot name to a canonical bench slot key."""
    raw = re.sub(r"[\s\-]+", "_", (slot or "").strip().lower())
    raw = re.sub(r"_+", "_", raw).strip("_")
    if not raw:
        return ""
    if raw in _SLOT_ALIASES:
        return _SLOT_ALIASES[raw]
    compact = raw.replace("_", "")
    if compact in _SLOT_ALIASES:
        return _SLOT_ALIASES[compact]
    return raw


def classes_to_slots(classes: Sequence[str]) -> tuple[str, ...]:
    """Map PoB ModMaster.lua `applicable_types` to canonical sorted CLI slot keys."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for cls in classes:
        mapped = POB_TYPE_TO_SLOTS.get(cls)
        if mapped is None:
            fallback = slot_key(cls)
            mapped = (fallback,) if fallback else ()
        for s in mapped:
            if s not in seen_set:
                seen_set.add(s)
                seen.append(s)
    return tuple(seen)


def normalize_line(line: str) -> str:
    """Normalize unicode dashes and whitespace in a single PoB mod line."""
    s = _DASH_RE.sub("-", line)
    return re.sub(r"\s+", " ", s).strip()


def normalize_template(text: str) -> str:
    """Normalize mod text to a lowercase `#`-placeholder template for matching."""
    parts = [p.strip() for p in re.split(r"\s*/\s*|\r?\n", text) if p.strip()]
    if not parts:
        return ""
    norm_parts: list[str] = []
    for part in parts:
        s = _LEADING_TAG_RE.sub("", part)
        s = _DASH_RE.sub("-", s).lower()
        s = _TEMPLATE_RANGE_RE.sub("#", s)
        s = _TEMPLATE_NUM_RE.sub("#", s)
        s = re.sub(r"\s+", " ", s).strip()
        if s:
            norm_parts.append(s)
    return " / ".join(norm_parts)


def parse_values(lines: Sequence[str]) -> tuple[tuple[float, float], ...]:
    """Extract ordered `(min, max)` numeric tuples from PoB mod lines."""
    vals: list[tuple[float, float]] = []
    for line in lines:
        s = _DASH_RE.sub("-", line)
        for m in _VALUE_TOKEN_RE.finditer(s):
            if m.group(1) is not None:
                a, b = -float(m.group(1)), -float(m.group(2))
                vals.append((min(a, b), max(a, b)))
            elif m.group(3) is not None:
                a, b = float(m.group(3)), float(m.group(4))
                vals.append((min(a, b), max(a, b)))
            else:
                v = float(m.group(5))
                vals.append((v, v))
    return tuple(vals)


def values_signature(values: Sequence[tuple[float, float]]) -> str:
    """Stable compact string key for a recipe's numeric value ranges."""
    return "|".join(f"{lo:g}:{hi:g}" for lo, hi in values)


def _fmt_num(val: float) -> str:
    if float(val).is_integer():
        return str(int(val))
    return f"{val:g}"


def _resolve_range_in_line(line: str, *, use_min: bool) -> str:
    """Replace `(a-b)` ranges in a single PoB mod line with the chosen bound."""
    clean = normalize_line(line)

    def _repl(match: re.Match[str]) -> str:
        s1, s2 = match.group(1), match.group(2)
        v1, v2 = float(s1), float(s2)
        if use_min:
            return s1 if v1 <= v2 else s2
        return s1 if v1 >= v2 else s2

    return _RANGE_RE.sub(_repl, clean)


@dataclass(frozen=True)
class BenchRecipe:
    """Single PoE1 crafting bench recipe backed by authoritative ModMaster data."""

    id: str
    lines: tuple[str, ...]
    lines_zh: tuple[str, ...]
    text: str
    text_zh: str
    template: str
    affix: str
    group: str
    level: int
    slots: tuple[str, ...]
    classes: tuple[str, ...]
    values: tuple[tuple[float, float], ...]
    cost: str
    unlock: str

    @property
    def min_value(self) -> float:
        return self.values[0][0] if self.values else 0.0

    @property
    def max_value(self) -> float:
        return self.values[0][1] if self.values else 0.0

    @property
    def roll_text(self) -> str:
        if not self.values:
            return ""
        parts: list[str] = []
        for lo, hi in self.values:
            if lo == hi:
                parts.append(_fmt_num(lo))
            else:
                parts.append(f"{_fmt_num(lo)}-{_fmt_num(hi)}")
        return ", ".join(parts)

    def crafted_lines(self, *, use_min: bool = True) -> list[str]:
        """Return valid PoB `{crafted}...` lines resolved to min (safe floor) or max roll."""
        out: list[str] = []
        for line in self.lines:
            resolved = _resolve_range_in_line(line, use_min=use_min)
            if not resolved.startswith("{crafted}"):
                resolved = f"{{crafted}}{resolved}"
            out.append(resolved)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "textZh": self.text_zh,
            "lines": list(self.lines),
            "linesZh": list(self.lines_zh),
            "template": self.template,
            "affix": self.affix,
            "group": self.group,
            "level": self.level,
            "slots": list(self.slots),
            "classes": list(self.classes),
            "values": [{"min": lo, "max": hi} for lo, hi in self.values],
            "minValue": self.min_value,
            "maxValue": self.max_value,
            "craftedLinesMin": self.crafted_lines(use_min=True),
            "cost": self.cost,
            "unlock": self.unlock,
        }


@dataclass(frozen=True)
class BenchSearchResult:
    """Scored search hit for a crafting bench query."""

    recipe: BenchRecipe
    score: float

    def as_dict(self) -> dict[str, Any]:
        payload = self.recipe.as_dict()
        payload["score"] = round(self.score, 4)
        return payload


# ---------------------------------------------------------------------------
# Data loading, cost overlay, and caching
# ---------------------------------------------------------------------------

_COSTS_FILE_CACHE: dict[Path, dict[str, Any]] = {}
_RECIPES_CACHE: dict[tuple[Any, ...], tuple[tuple[BenchRecipe, ...], dict[str, Any]]] = {}


def clear_cache() -> None:
    """Clear in-memory recipe and costs caches (for tests and hot-reloads)."""
    _COSTS_FILE_CACHE.clear()
    _RECIPES_CACHE.clear()


def _resolve_data_file() -> Path:
    if DATA_FILE.is_file():
        return DATA_FILE
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "pob_cli" / "data" / "bench_costs.json"
        if candidate.is_file():
            return candidate
    return DATA_FILE


def _load_costs_payload() -> tuple[Path, dict[str, Any]]:
    path = _resolve_data_file()
    cached = _COSTS_FILE_CACHE.get(path)
    if cached is not None:
        return path, cached
    if not path.is_file():
        payload: dict[str, Any] = {"meta": {}, "snapshot": []}
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    _COSTS_FILE_CACHE[path] = payload
    return path, payload


class _CostLookup:
    """Multi-tier cost/unlock lookup indexed by stable normalized keys."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.by_tpl_vals_lvl: dict[tuple[str, str, int], tuple[str, str]] = {}
        self.by_tpl_vals: dict[tuple[str, str], tuple[str, str]] = {}
        self.by_tpl_lvl: dict[tuple[str, int], tuple[str, str]] = {}
        self.by_tpl: dict[str, tuple[str, str]] = {}

        for item in payload.get("snapshot", []):
            cost = str(item.get("c") or "").strip()
            unlock = str(item.get("u") or "").strip()
            if not cost or cost.startswith("unknown"):
                continue
            lines = [normalize_line(str(ln)) for ln in item.get("l", []) if str(ln).strip()]
            if not lines:
                continue
            tpl_flat = normalize_template(" ".join(lines))
            vals_sig = values_signature(parse_values(lines))
            level = int(item.get("lv") or 0)
            pair = (cost, unlock)
            self.by_tpl_vals_lvl.setdefault((tpl_flat, vals_sig, level), pair)
            self.by_tpl_vals.setdefault((tpl_flat, vals_sig), pair)
            self.by_tpl_lvl.setdefault((tpl_flat, level), pair)
            self.by_tpl.setdefault(tpl_flat, pair)

    def resolve(
        self,
        lines: Sequence[str],
        values: Sequence[tuple[float, float]],
        level: int,
    ) -> tuple[str, str, bool]:
        tpl_flat = normalize_template(" ".join(lines))
        vals_sig = values_signature(values)
        for mapping, key in (
            (self.by_tpl_vals_lvl, (tpl_flat, vals_sig, level)),
            (self.by_tpl_vals, (tpl_flat, vals_sig)),
            (self.by_tpl_lvl, (tpl_flat, level)),
            (self.by_tpl, tpl_flat),
        ):
            hit = mapping.get(key)  # type: ignore[arg-type]
            if hit is not None:
                return hit[0], hit[1], True
        return UNKNOWN_COST, "", False


def _normalize_affix(raw_gen: str) -> str:
    low = (raw_gen or "").strip().lower()
    if low in ("p", "prefix", "bench prefix") or "prefix" in low:
        return "prefix"
    if low in ("s", "suffix", "bench suffix") or "suffix" in low:
        return "suffix"
    raise ValueError(f"Unrecognized crafting bench affix type: {raw_gen!r}")


def _build_recipe(
    *,
    recipe_id: str,
    raw_lines: Sequence[str],
    raw_lines_zh: Sequence[Any],
    affix_raw: str,
    group: str,
    level: int,
    classes: Sequence[str],
    cost: str,
    unlock: str,
) -> BenchRecipe:
    lines = tuple(normalize_line(str(ln)) for ln in raw_lines if isinstance(ln, str) and ln.strip())
    lines_zh = tuple(normalize_line(str(ln)) for ln in raw_lines_zh if isinstance(ln, str) and ln.strip())
    text = " / ".join(lines)
    text_zh = " / ".join(lines_zh)
    template = normalize_template(text)
    values = parse_values(lines)
    clean_classes = tuple(str(c) for c in classes if str(c).strip())
    slots = classes_to_slots(clean_classes)
    affix = _normalize_affix(affix_raw)
    return BenchRecipe(
        id=recipe_id,
        lines=lines,
        lines_zh=lines_zh,
        text=text,
        text_zh=text_zh,
        template=template,
        affix=affix,
        group=str(group or ""),
        level=int(level or 0),
        slots=slots,
        classes=clean_classes,
        values=values,
        cost=cost if cost else UNKNOWN_COST,
        unlock=unlock,
    )


def _load_from_modmaster(
    db_path: Path,
    costs_path: Path,
    costs_payload: dict[str, Any],
) -> tuple[tuple[BenchRecipe, ...], dict[str, Any]]:
    lookup = _CostLookup(costs_payload)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        rows = conn.execute("SELECT data_json FROM mods WHERE source = 'Bench' ORDER BY rowid").fetchall()
    finally:
        conn.close()

    recipes: list[BenchRecipe] = []
    cost_matched = 0
    for (raw_json,) in rows:
        rec = json.loads(raw_json)
        stat_lines = [normalize_line(str(s)) for s in rec.get("stats", []) if isinstance(s, str) and s.strip()]
        if not stat_lines:
            continue
        vals = parse_values(stat_lines)
        level = int(rec.get("level") or 0)
        cost, unlock, matched = lookup.resolve(stat_lines, vals, level)
        if matched:
            cost_matched += 1
        recipes.append(
            _build_recipe(
                recipe_id=str(rec["id"]),
                raw_lines=stat_lines,
                raw_lines_zh=rec.get("stats_zh") or [],
                affix_raw=str(rec.get("generation_type") or ""),
                group=str(rec.get("group") or ""),
                level=level,
                classes=rec.get("applicable_types") or [],
                cost=cost,
                unlock=unlock,
            )
        )

    metadata: dict[str, Any] = {
        "mode": "local_modmaster",
        "count": len(recipes),
        "pobVersion": meta.get("pob_version", "unknown"),
        "dataVersion": meta.get("data_version", "unknown"),
        "zhAvailable": meta.get("zh_available") == "1",
        "dbPath": str(db_path),
        "costsFile": str(costs_path),
        "costsMeta": costs_payload.get("meta", {}),
        "costMatched": cost_matched,
    }
    return tuple(recipes), metadata


def _load_from_snapshot(
    costs_path: Path,
    costs_payload: dict[str, Any],
) -> tuple[tuple[BenchRecipe, ...], dict[str, Any]]:
    type_table = [str(t) for t in costs_payload.get("types", [])]
    recipes: list[BenchRecipe] = []
    cost_matched = 0
    for item in costs_payload.get("snapshot", []):
        cost = str(item.get("c") or "").strip() or UNKNOWN_COST
        unlock = str(item.get("u") or "").strip()
        if cost and not cost.startswith("unknown"):
            cost_matched += 1
        raw_types = item.get("t") or []
        classes = [
            type_table[t] if isinstance(t, int) and 0 <= t < len(type_table) else str(t)
            for t in raw_types
        ]
        recipes.append(
            _build_recipe(
                recipe_id=str(item["id"]),
                raw_lines=item.get("l") or [],
                raw_lines_zh=item.get("z") or [],
                affix_raw=str(item.get("a") or ""),
                group=str(item.get("g") or ""),
                level=int(item.get("lv") or 0),
                classes=classes,
                cost=cost,
                unlock=unlock,
            )
        )

    costs_meta = costs_payload.get("meta", {})
    metadata: dict[str, Any] = {
        "mode": "embedded_snapshot",
        "count": len(recipes),
        "pobVersion": costs_meta.get("pobVersion", "unknown"),
        "dataVersion": costs_meta.get("dataVersion", "unknown"),
        "zhAvailable": any(bool(r.lines_zh) for r in recipes),
        "dbPath": None,
        "costsFile": str(costs_path),
        "costsMeta": costs_meta,
        "costMatched": cost_matched,
    }
    return tuple(recipes), metadata


def load_recipes(*, allow_engine: bool = True) -> tuple[tuple[BenchRecipe, ...], dict[str, Any]]:
    """Load all crafting bench recipes and source metadata, cached per engine state."""
    costs_path, costs_payload = _load_costs_payload()
    costs_mtime = costs_path.stat().st_mtime_ns if costs_path.is_file() else 0

    if allow_engine:
        try:
            db_path = game_data.ensure_gamedata_index()
            db_mtime = db_path.stat().st_mtime_ns
            cache_key = ("local_modmaster", str(db_path), db_mtime, str(costs_path), costs_mtime)
            cached = _RECIPES_CACHE.get(cache_key)
            if cached is not None:
                return cached
            result = _load_from_modmaster(db_path, costs_path, costs_payload)
            _RECIPES_CACHE[cache_key] = result
            return result
        except Exception:
            pass

    cache_key = ("embedded_snapshot", str(costs_path), costs_mtime)
    cached = _RECIPES_CACHE.get(cache_key)
    if cached is not None:
        return cached
    result = _load_from_snapshot(costs_path, costs_payload)
    _RECIPES_CACHE[cache_key] = result
    return result


def source_metadata(*, allow_engine: bool = True) -> dict[str, Any]:
    """Return metadata describing the active bench data source (`local_modmaster` or `embedded_snapshot`)."""
    _, meta = load_recipes(allow_engine=allow_engine)
    return dict(meta)


def recipes_for_slot(slot: str, *, allow_engine: bool = True) -> list[BenchRecipe]:
    """Return all crafting bench recipes applicable to `slot` (or all recipes if `slot` is empty/`all`)."""
    recipes, _ = load_recipes(allow_engine=allow_engine)
    target = slot_key(slot)
    if not target or target == "all":
        return list(recipes)
    return [r for r in recipes if target in r.slots]


def _tier_strength(recipe: BenchRecipe) -> float:
    """Numeric bench tier strength: `min_value` for positive stats, min magnitude for negative stats."""
    if not recipe.values:
        return 0.0
    lo, hi = recipe.values[0]
    if lo < 0 and hi < 0:
        return min(abs(lo), abs(hi))
    return lo


def list_for_slot(
    slot: str,
    *,
    affix: str | None = None,
    allow_engine: bool = True,
) -> list[BenchRecipe]:
    """List crafting bench recipes for `slot`, optionally filtered by `affix` (`prefix` or `suffix`)."""
    items = recipes_for_slot(slot, allow_engine=allow_engine)
    if affix is not None and affix.strip():
        want = affix.strip().lower()
        if want not in ("prefix", "suffix"):
            raise ValueError(f"Invalid affix filter {affix!r}; expected 'prefix' or 'suffix'.")
        items = [r for r in items if r.affix == want]
    items.sort(key=lambda r: (r.affix, r.group, -_tier_strength(r), -r.level, r.text))
    return items


# ---------------------------------------------------------------------------
# Bilingual search & ranking
# ---------------------------------------------------------------------------


def _clean_en_for_match(template: str) -> str:
    s = template.replace("#%", "").replace("#", "")
    return re.sub(r"\s+", " ", s).strip()


def _en_tokens(text: str) -> list[str]:
    words = _EN_WORD_RE.findall(text.lower())
    filtered = [w for w in words if w not in _EN_STOPWORDS]
    return filtered or words


def _clean_zh_for_match(text: str) -> str:
    return _ZH_NOISE_RE.sub("", text.lower())


def _is_subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(ch in it for ch in needle)


def _score_english(query_raw: str, query_tpl: str, recipe: BenchRecipe) -> float:
    q_clean = _clean_en_for_match(query_tpl)
    if not q_clean:
        return 0.0
    r_tpl = recipe.template
    r_clean = _clean_en_for_match(r_tpl)

    if query_tpl == r_tpl or q_clean == r_clean:
        return 1.0

    # Check exact/substring against individual lines as well as full template
    line_cleans = [_clean_en_for_match(normalize_template(ln)) for ln in recipe.lines]
    if any(q_clean == lc for lc in line_cleans):
        return 0.96

    if q_clean in r_clean:
        coverage = len(q_clean) / max(len(r_clean), 1)
        return 0.85 + 0.14 * min(coverage, 1.0)

    q_words = _en_tokens(q_clean)
    if not q_words:
        return 0.0
    r_words = _en_tokens(r_clean)
    r_word_set = set(r_words)

    exact_hits = sum(1 for w in q_words if w in r_word_set)
    substr_hits = sum(1 for w in q_words if w in r_word_set or any(w in rw for rw in r_words))

    if exact_hits == len(q_words):
        coverage = sum(len(w) for w in q_words) / max(sum(len(w) for w in r_words), 1)
        return 0.72 + 0.12 * min(coverage, 1.0)

    if substr_hits == len(q_words):
        coverage = sum(len(w) for w in q_words) / max(sum(len(w) for w in r_words), 1)
        return 0.62 + 0.12 * min(coverage, 1.0)

    if recipe.group and q_clean.replace(" ", "") == recipe.group.lower():
        return 0.78

    if substr_hits > 0:
        recall = substr_hits / len(q_words)
        precision = substr_hits / max(len(r_words), 1)
        score = 0.58 * (0.75 * recall + 0.25 * precision)
        return score if recall >= 0.5 else 0.0

    # Raw text fallback (handles explicit numbers in query like "+2 to level")
    q_low = normalize_line(query_raw).lower()
    if q_low and q_low in recipe.text.lower():
        coverage = len(q_low) / max(len(recipe.text), 1)
        return 0.82 + 0.12 * min(coverage, 1.0)

    return 0.0


def _score_chinese(query_raw: str, recipe: BenchRecipe) -> float:
    if not recipe.text_zh:
        return 0.0
    q_zh = _clean_zh_for_match(query_raw)
    if not q_zh:
        return 0.0
    r_zh = _clean_zh_for_match(recipe.text_zh)
    if not r_zh:
        return 0.0

    if q_zh == r_zh:
        return 1.0

    line_zhs = [_clean_zh_for_match(ln) for ln in recipe.lines_zh]
    if any(q_zh == lz for lz in line_zhs):
        return 0.96

    if q_zh in r_zh:
        coverage = len(q_zh) / max(len(r_zh), 1)
        return 0.85 + 0.14 * min(coverage, 1.0)

    # Space-separated Chinese keywords (e.g. "冰霜 抗性", "攻击 速度")
    parts = [_clean_zh_for_match(p) for p in re.split(r"\s+", query_raw.strip()) if _clean_zh_for_match(p)]
    if len(parts) > 1 and all(p in r_zh for p in parts):
        coverage = sum(len(p) for p in parts) / max(len(r_zh), 1)
        return 0.74 + 0.12 * min(coverage, 1.0)

    # Ordered CJK character subsequence fallback (e.g. "冰抗" -> "冰霜抗性")
    q_cjk = "".join(_CJK_CHAR_RE.findall(q_zh))
    r_cjk = "".join(_CJK_CHAR_RE.findall(r_zh))
    if len(q_cjk) >= 2 and r_cjk:
        if _is_subsequence(q_cjk, r_cjk):
            coverage = len(q_cjk) / max(len(r_cjk), 1)
            return 0.45 + 0.20 * min(coverage, 1.0)
        hits = sum(1 for ch in set(q_cjk) if ch in r_cjk)
        recall = hits / len(set(q_cjk))
        if recall >= 0.7:
            return 0.30 + 0.15 * recall

    return 0.0


def search(
    slot: str,
    text: str,
    limit: int = 5,
    *,
    affix: str | None = None,
    allow_engine: bool = True,
) -> list[BenchSearchResult]:
    """Search crafting bench recipes for `slot` using English or Chinese text.

    Ranks by match score descending, then highest `min_value` (best bench tier),
    then highest `level`.
    """
    candidates = list_for_slot(slot, affix=affix, allow_engine=allow_engine)
    if limit <= 0:
        return []

    q_raw = (text or "").strip()
    if not q_raw:
        candidates.sort(key=lambda r: (-_tier_strength(r), -r.level, r.text))
        return [BenchSearchResult(recipe=r, score=1.0) for r in candidates[:limit]]

    q_tpl = normalize_template(q_raw)
    has_cjk = bool(_CJK_CHAR_RE.search(q_raw))

    scored: list[BenchSearchResult] = []
    for recipe in candidates:
        en_score = _score_english(q_raw, q_tpl, recipe)
        zh_score = _score_chinese(q_raw, recipe) if has_cjk else 0.0
        best_score = max(en_score, zh_score)
        if best_score >= 0.25:
            scored.append(BenchSearchResult(recipe=recipe, score=best_score))

    scored.sort(
        key=lambda item: (
            -round(item.score, 6),
            -_tier_strength(item.recipe),
            -item.recipe.level,
            item.recipe.text,
        )
    )
    return scored[:limit]


def best(
    slot: str,
    text: str,
    *,
    affix: str | None = None,
    allow_engine: bool = True,
) -> BenchRecipe | None:
    """Return the highest-ranked crafting bench recipe matching `text` for `slot`."""
    hits = search(slot, text, limit=1, affix=affix, allow_engine=allow_engine)
    return hits[0].recipe if hits else None


def can_craft_on_item(
    recipe: BenchRecipe,
    existing_templates: Iterable[str],
    *,
    has_crafted_mod: bool = False,
) -> tuple[bool, str]:
    """Check whether `recipe` can be crafted on an item with `existing_templates`.

    Returns `(allowed, message)`:
    - `(False, ...)` if any normalized stat template in `recipe` conflicts with
      an existing modifier on the item.
    - `(True, ...)` if craftable; when `has_crafted_mod=True`, notes that the
      existing crafted modifier can be replaced at the crafting bench.
    """
    norm_existing = {normalize_template(t) for t in existing_templates if t and normalize_template(t)}
    recipe_templates = [recipe.template]
    for ln in recipe.lines:
        lt = normalize_template(ln)
        if lt and lt not in recipe_templates:
            recipe_templates.append(lt)

    for rt in recipe_templates:
        if rt in norm_existing:
            return (
                False,
                f"Conflict: item already has matching modifier template '{rt}'.",
            )

    if has_crafted_mod:
        return (
            True,
            "Item already has a crafted modifier; existing craft can be replaced at the crafting bench.",
        )

    return True, "Craftable on item."


def format_recipe(recipe: BenchRecipe, *, show_zh: bool = True) -> str:
    """Format a single bench recipe as a concise human-readable line."""
    parts = [f"[{recipe.affix}] {recipe.text}"]
    if show_zh and recipe.text_zh:
        parts.append(recipe.text_zh)
    parts.append(f"Cost: {recipe.cost}")
    if recipe.unlock:
        parts.append(f"Unlock: {recipe.unlock}")
    return " | ".join(parts)
