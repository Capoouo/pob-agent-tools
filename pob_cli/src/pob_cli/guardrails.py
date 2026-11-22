"""Reverse PoE2 -> PoE1 cross-game isolation and anti-hallucination guardrails (`pob_cli`).

Pure-stdlib module (zero `httpx`, 0ms cold import) implementing:
  - Gate 1: Build XML & Share-Code validation (`validate_build_xml`)
  - Gate 2: Bidirectional URL domain/path validation (`validate_url`)
  - Gate 3: Engine root directory validation (`validate_engine_root`)
  - Table D: Context-scoped PoE2-only reverse traps (`check_input`, `check_input_item_raw`)
"""

from __future__ import annotations

import contextlib
import re
import sys
import threading
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from .errors import CrossGameHallucinationError, HallucinationWarning, LegacyPatchTrapError

GAME_ID = "poe1"


class GuardrailScope(str, Enum):
    SLOT = "slot"
    CATEGORY = "category"
    ITEM_RAW = "item_raw"
    GEM_NAME = "gem_name"
    NOTABLE_OR_OIL = "notable_or_oil"
    CONFIG_VAR = "config_var"
    STAT_QUERY = "stat_query"
    CALC_QUERY = "calc_query"
    DATA_QUERY = "data_query"


ALL_QUERY_SCOPES: frozenset[GuardrailScope] = frozenset(GuardrailScope)
S = GuardrailScope


# ---------------------------------------------------------------------------
# Warning Context (stack-based thread-local, parity with pob2_cli)
# ---------------------------------------------------------------------------

_WARN_TLS = threading.local()


def _tls_stack() -> list[list[HallucinationWarning]]:
    stack = getattr(_WARN_TLS, "stack", None)
    if stack is None:
        stack = []
        _WARN_TLS.stack = stack
    return stack


def _tls_cli_warnings() -> list[HallucinationWarning]:
    buf = getattr(_WARN_TLS, "warnings", None)
    if buf is None:
        buf = []
        _WARN_TLS.warnings = buf
    return buf


def _tls_emitted_keys() -> set[tuple[str, str]]:
    keys = getattr(_WARN_TLS, "emitted_keys", None)
    if keys is None:
        keys = set()
        _WARN_TLS.emitted_keys = keys
    return keys


def _write_stderr_line(msg: str) -> None:
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _record_warning(warning: HallucinationWarning, *, emit_stderr: bool = True) -> None:
    stack = _tls_stack()
    target_list = stack[-1] if stack else _tls_cli_warnings()
    key = (warning.rule_id, warning.trigger)
    if not any((w.rule_id, w.trigger) == key for w in target_list):
        target_list.append(warning)
    if emit_stderr:
        emitted = _tls_emitted_keys()
        if key not in emitted:
            emitted.add(key)
            _write_stderr_line(warning.format_stderr())


def reset_cli_warnings() -> None:
    _tls_cli_warnings().clear()
    _tls_emitted_keys().clear()
    stack = _tls_stack()
    if stack:
        stack[-1].clear()


@contextlib.contextmanager
def warning_context() -> Iterator[list[HallucinationWarning]]:
    stack = _tls_stack()
    bucket: list[HallucinationWarning] = []
    stack.append(bucket)
    try:
        yield bucket
    finally:
        if stack and stack[-1] is bucket:
            stack.pop()
        elif bucket in stack:
            stack.remove(bucket)


def get_active_warnings() -> list[HallucinationWarning]:
    stack = _tls_stack()
    src = stack[-1] if stack else _tls_cli_warnings()
    return list(src)


def get_active_warning_dicts() -> list[dict[str, Any]]:
    return [w.to_dict() for w in get_active_warnings()]


def emit_stderr_warnings_from_dicts(items: list[dict[str, Any]] | None) -> None:
    if not items:
        return
    for raw in items:
        if not isinstance(raw, dict):
            continue
        warning = HallucinationWarning.from_dict(raw, default_game=GAME_ID)
        _record_warning(warning, emit_stderr=True)


# ---------------------------------------------------------------------------
# Tolerant Equipment Slot Alias Normalization (P0-2)
# ---------------------------------------------------------------------------

EQUIPMENT_SLOT_ALIASES: dict[str, str] = {
    # Rings
    "ring1": "Ring 1",
    "ring_1": "Ring 1",
    "ring-1": "Ring 1",
    "ring 1": "Ring 1",
    "left ring": "Ring 1",
    "leftring": "Ring 1",
    "左戒指": "Ring 1",
    "左戒": "Ring 1",
    "戒指1": "Ring 1",
    "戒指 1": "Ring 1",
    "ring2": "Ring 2",
    "ring_2": "Ring 2",
    "ring-2": "Ring 2",
    "ring 2": "Ring 2",
    "right ring": "Ring 2",
    "rightring": "Ring 2",
    "右戒指": "Ring 2",
    "右戒": "Ring 2",
    "戒指2": "Ring 2",
    "戒指 2": "Ring 2",
    "ring3": "Ring 3",
    "ring_3": "Ring 3",
    "ring-3": "Ring 3",
    "ring 3": "Ring 3",
    "戒指3": "Ring 3",
    "戒指 3": "Ring 3",
    # Armour
    "chest": "Body Armour",
    "body": "Body Armour",
    "body_armour": "Body Armour",
    "body-armour": "Body Armour",
    "bodyarmour": "Body Armour",
    "body armour": "Body Armour",
    "body armor": "Body Armour",
    "衣服": "Body Armour",
    "胸甲": "Body Armour",
    "身甲": "Body Armour",
    "盔甲": "Body Armour",
    "helm": "Helmet",
    "helmet": "Helmet",
    "head": "Helmet",
    "头盔": "Helmet",
    "头部": "Helmet",
    "手套": "Gloves",
    "glove": "Gloves",
    "gloves": "Gloves",
    "hands": "Gloves",
    "boots": "Boots",
    "boot": "Boots",
    "feet": "Boots",
    "shoes": "Boots",
    "鞋子": "Boots",
    "靴子": "Boots",
    # Jewellery & Belt
    "amulet": "Amulet",
    "neck": "Amulet",
    "necklace": "Amulet",
    "项链": "Amulet",
    "护身符": "Amulet",
    "belt": "Belt",
    "waist": "Belt",
    "腰带": "Belt",
    # Weapons / Off-hand
    "weapon1": "Weapon 1",
    "weapon_1": "Weapon 1",
    "weapon-1": "Weapon 1",
    "weapon 1": "Weapon 1",
    "mainhand": "Weapon 1",
    "main_hand": "Weapon 1",
    "main-hand": "Weapon 1",
    "main hand": "Weapon 1",
    "主手": "Weapon 1",
    "主手武器": "Weapon 1",
    "武器1": "Weapon 1",
    "武器 1": "Weapon 1",
    "weapon2": "Weapon 2",
    "weapon_2": "Weapon 2",
    "weapon-2": "Weapon 2",
    "weapon 2": "Weapon 2",
    "offhand": "Weapon 2",
    "off_hand": "Weapon 2",
    "off-hand": "Weapon 2",
    "off hand": "Weapon 2",
    "shield": "Weapon 2",
    "quiver": "Weapon 2",
    "副手": "Weapon 2",
    "副手武器": "Weapon 2",
    "盾牌": "Weapon 2",
    "箭袋": "Weapon 2",
    "武器2": "Weapon 2",
    "武器 2": "Weapon 2",
    # Flasks 1..5
    "flask1": "Flask 1",
    "flask_1": "Flask 1",
    "flask-1": "Flask 1",
    "flask 1": "Flask 1",
    "药剂1": "Flask 1",
    "药水1": "Flask 1",
    "flask2": "Flask 2",
    "flask_2": "Flask 2",
    "flask-2": "Flask 2",
    "flask 2": "Flask 2",
    "药剂2": "Flask 2",
    "药水2": "Flask 2",
    "flask3": "Flask 3",
    "flask_3": "Flask 3",
    "flask-3": "Flask 3",
    "flask 3": "Flask 3",
    "药剂3": "Flask 3",
    "药水3": "Flask 3",
    "flask4": "Flask 4",
    "flask_4": "Flask 4",
    "flask-4": "Flask 4",
    "flask 4": "Flask 4",
    "药剂4": "Flask 4",
    "药水4": "Flask 4",
    "flask5": "Flask 5",
    "flask_5": "Flask 5",
    "flask-5": "Flask 5",
    "flask 5": "Flask 5",
    "药剂5": "Flask 5",
    "药水5": "Flask 5",
    # PoE1 3.30 Wickerman Graft (Viscera) slots
    "graft_head": "Head",
    "graft-head": "Head",
    "graft head": "Head",
    "grafthead": "Head",
    "肢体头部": "Head",
    "graft_body": "Body",
    "graft-body": "Body",
    "graft body": "Body",
    "graftbody": "Body",
    "肢体躯干": "Body",
    "graft_arm": "Arm",
    "graft-arm": "Arm",
    "graft arm": "Arm",
    "graftarm": "Arm",
    "肢体手臂": "Arm",
    "graft_leg": "Leg",
    "graft-leg": "Leg",
    "graft leg": "Leg",
    "graftleg": "Leg",
    "肢体腿部": "Leg",
}


def normalize_equipment_slot(slot: str | None) -> str | None:
    """Normalize common EN/ZH equipment slot aliases (e.g. `ring1`/`左戒指` -> `Ring 1`, `chest`/`衣服` -> `Body Armour`).

    Leaves jewel socket shorthands (`#1`), unknown names, and PoE2 trap inputs untouched so
    `check_input(..., scope=GuardrailScope.SLOT)` always runs cleanly first.
    """
    if slot is None:
        return None
    raw = str(slot).strip()
    if not raw:
        return raw
    key = re.sub(r"\s+", " ", raw.lower())
    hit = EQUIPMENT_SLOT_ALIASES.get(key)
    if hit is not None:
        return hit
    compact = re.sub(r"[\s_\-]+", "", key)
    return EQUIPMENT_SLOT_ALIASES.get(compact, raw)


# ---------------------------------------------------------------------------
# Text Normalization & Token-Boundary Matchers
# ---------------------------------------------------------------------------

_POB_ESC_RE = re.compile(r"\^x[0-9A-Fa-f]{6}|\^\d")
_POE2_LINK_RE = re.compile(r"\[[^\]|]+\|([^\]]+)\]|\[([^\]]+)\]")
_FOLD_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u02bc": "'",
        "\u0060": "'",
        "\u2010": "-",
        "\u2011": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u3000": " ",
        "\u00a0": " ",
    }
)
_GEM_SPEC_TAIL_RE = re.compile(r":\d+(?::\d+)?\s*$")
_STAT_FILTER_TAIL_RE = re.compile(r"\s*(?:>=|<=|=)\s*[-+]?\d+(?:\.\d+)?\s*$")
_STAT_WEIGHT_TAIL_RE = re.compile(r"\s*\*\s*[-+]?\d+(?:\.\d+)?\s*$")


def normalize_guardrail_text(text: str) -> str:
    if not text:
        return ""
    s = _POB_ESC_RE.sub("", text)
    s = _POE2_LINK_RE.sub(lambda m: m.group(1) or m.group(2) or "", s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.translate(_FOLD_MAP).lower()
    return re.sub(r"\s+", " ", s).strip()


def normalize_zh_compact(text: str) -> str:
    s = normalize_guardrail_text(text)
    return re.sub(r"[\s·・.．:：()（）]", "", s)


_WORD_EN_CACHE: dict[str, re.Pattern[str]] = {}


def match_word_en(
    norm_text: str,
    phrase: str,
    exclude_re: re.Pattern[str] | None = None,
) -> str | None:
    key = phrase.lower()
    pat = _WORD_EN_CACHE.get(key)
    if pat is None:
        pat = re.compile(rf"(?<![a-z0-9_'-]){re.escape(key)}(?![a-z0-9_'-])")
        _WORD_EN_CACHE[key] = pat
    for m in pat.finditer(norm_text):
        if exclude_re is not None:
            win_start = max(0, m.start() - 24)
            win_end = min(len(norm_text), m.end() + 24)
            if exclude_re.search(norm_text[win_start:win_end]):
                continue
        return m.group(0)
    return None


def match_phrase_zh(
    raw_text: str,
    zh_phrase: str,
    exclude_compounds: tuple[str, ...] = (),
) -> str | None:
    compact = normalize_zh_compact(raw_text)
    target = normalize_zh_compact(zh_phrase)
    if not target:
        return None
    start = 0
    while True:
        idx = compact.find(target, start)
        if idx == -1:
            return None
        win = compact[max(0, idx - 5) : min(len(compact), idx + len(target) + 5)]
        if not any(normalize_zh_compact(comp) in win for comp in exclude_compounds):
            return zh_phrase
        start = idx + 1


# ---------------------------------------------------------------------------
# Item Header Sanitizer (skip random rare/magic titles, Unique ID, Flavour)
# ---------------------------------------------------------------------------

_ITEM_HEADER_KEY_RE = re.compile(
    r"^(?:Item Class|Rarity|Armour|Evasion|Energy Shield|Ward|Quality|Sockets|Item Level|"
    r"LevelReq|Requires|Unique ID|League|Crafted|Prefix|Suffix|Implicits|Variant|Version|"
    r"Selected Variant|Has Alt Variant|Radius|Limited to|Catalyst|CatalystQuality|"
    r"Flavour Text|Memory Strands|Talisman Tier|Rune Sockets|Charm Slots)\b",
    re.IGNORECASE,
)


def extract_scannable_item_parts(raw_text: str) -> tuple[str, str]:
    """Return `(scannable_text, base_line)` stripping random Rare/Magic Line-1 titles, Unique ID, and Flavour Text."""
    lines = (raw_text or "").splitlines()
    if not lines:
        return "", ""

    rarity = ""
    kept_lines: list[str] = []
    header_name_lines: list[str] = []
    in_header = True
    in_flavour = False
    base_line = ""

    def _flush_header_names() -> None:
        nonlocal base_line
        if not header_name_lines:
            return
        if rarity in ("RARE", "MAGIC") and len(header_name_lines) >= 2:
            # Discard line 1 (random rare/magic prefix+suffix title), keep only base type line
            base_line = header_name_lines[-1]
            kept_lines.append(base_line)
        else:
            base_line = header_name_lines[-1]
            kept_lines.extend(header_name_lines)
        header_name_lines.clear()

    for line in lines:
        stripped = line.strip()
        low = stripped.lower()

        if low.startswith("unique id:"):
            continue
        if low.startswith("flavour text:") or low.startswith("{flavour}"):
            in_flavour = True
            continue
        if stripped == "--------":
            if in_header:
                _flush_header_names()
                in_header = False
            in_flavour = False
            continue
        if in_flavour:
            continue

        if in_header:
            if low.startswith("rarity:"):
                rarity = stripped.split(":", 1)[1].strip().upper()
                kept_lines.append(stripped)
                continue
            if low.startswith("implicits:"):
                _flush_header_names()
                in_header = False
                kept_lines.append(stripped)
                continue
            if _ITEM_HEADER_KEY_RE.match(stripped):
                _flush_header_names()
                kept_lines.append(stripped)
                continue
            if stripped:
                header_name_lines.append(stripped)
            continue

        if stripped:
            kept_lines.append(stripped)

    if in_header:
        _flush_header_names()

    return "\n".join(kept_lines), base_line


# ---------------------------------------------------------------------------
# Gate 1: Build XML & Share-Code Gate (`validate_build_xml`)
# ---------------------------------------------------------------------------

_XML_NOTES_STRIP_RE = re.compile(r"<Notes\b[^>]*>[\s\S]*?</Notes>", re.IGNORECASE)
_XML_ROOT_TAG_RE = re.compile(r"<\s*(PathOfBuilding2?)\b")
_XML_TREE_VER_RE = re.compile(r'\btreeVersion\s*=\s*["\']([^"\']+)["\']')
_XML_TARGET_VER_RE = re.compile(r'\btargetVersion\s*=\s*["\']([^"\']+)["\']')
_XML_CLASS_RE = re.compile(r'<\s*Build\b[^>]*\bclassName\s*=\s*["\']([^"\']+)["\']')
_XML_SLOT_RE = re.compile(r'<\s*Slot\b[^>]*\bname\s*=\s*["\']([^"\']+)["\']')

POE1_ONLY_CLASSES = frozenset({"marauder", "duelist", "shadow", "templar", "scion"})
POE2_ONLY_CLASSES = frozenset({"warrior", "monk", "mercenary", "sorceress", "huntress", "druid"})


def validate_build_xml(
    xml_text: str,
    *,
    expected_game: str = GAME_ID,
    source_hint: str | None = None,
) -> None:
    """Hard-block cross-game PoB2 vs PoB1 XML builds before touching Lua `import_code`."""
    cleaned = _XML_NOTES_STRIP_RE.sub("", (xml_text or "").lstrip("\ufeff").strip())
    if not cleaned.startswith("<"):
        return

    root_m = _XML_ROOT_TAG_RE.search(cleaned[:1024])
    root_tag = root_m.group(1) if root_m else None
    tree_versions = _XML_TREE_VER_RE.findall(cleaned)
    target_versions = _XML_TARGET_VER_RE.findall(cleaned)
    class_m = _XML_CLASS_RE.search(cleaned[:4096])
    class_name = class_m.group(1).strip().lower() if class_m else ""
    slots = _XML_SLOT_RE.findall(cleaned)
    src_arg = f' "{source_hint}"' if source_hint else " <source>"

    if expected_game == "poe1":
        if root_tag == "PathOfBuilding2":
            tv_note = f" (treeVersion={tree_versions[0]!r})" if tree_versions else ""
            raise CrossGameHallucinationError(
                game="poe1",
                detected_game="poe2",
                trigger=f"<PathOfBuilding2>{tv_note}",
                error=(
                    f"Build XML uses PoE2 root `<PathOfBuilding2>`{tv_note}; "
                    "`pob` only supports PoE1 `<PathOfBuilding>` builds."
                ),
                remediation="Load PoE2 builds with `pob2` (`F:/poe2`).",
                equivalent_command=f"pob2 load{src_arg}",
                rule_id="XML_POE2_ROOT_IN_POB1",
            )
        for tv in tree_versions:
            if re.match(r"^0_\d+", tv.strip()):
                raise CrossGameHallucinationError(
                    game="poe1",
                    detected_game="poe2",
                    trigger=f'treeVersion="{tv}"',
                    error=f"Build uses PoE2 version `{tv}` (PoE1 uses `3_*`).",
                    remediation="Load PoE2 builds with `pob2` (`F:/poe2`).",
                    equivalent_command=f"pob2 load{src_arg}",
                    rule_id="XML_POE2_TREE_IN_POB1",
                )
        for tv in target_versions:
            if re.match(r"^0_\d+", tv.strip()):
                raise CrossGameHallucinationError(
                    game="poe1",
                    detected_game="poe2",
                    trigger=f'targetVersion="{tv}"',
                    error=f"Build uses PoE2 targetVersion `{tv}` (PoE1 uses `3_0`).",
                    remediation="Load PoE2 builds with `pob2` (`F:/poe2`).",
                    equivalent_command=f"pob2 load{src_arg}",
                    rule_id="XML_POE2_TREE_IN_POB1",
                )
        if class_name in POE2_ONLY_CLASSES:
            raise CrossGameHallucinationError(
                game="poe1",
                detected_game="poe2",
                trigger=f'className="{class_name}"',
                error=f"Build uses PoE2-only class `{class_name}`.",
                remediation="Load PoE2 builds with `pob2` (`F:/poe2`).",
                equivalent_command=f"pob2 load{src_arg}",
                rule_id="XML_POE2_CLASS_IN_POB1",
            )
        for sl in slots:
            if sl in ("Charm 1", "Charm 2", "Charm 3"):
                raise CrossGameHallucinationError(
                    game="poe1",
                    detected_game="poe2",
                    trigger=f'Slot name="{sl}"',
                    error=f"Build XML contains PoE2 belt slot `{sl}`.",
                    remediation="Load PoE2 builds with `pob2` (`F:/poe2`).",
                    equivalent_command=f"pob2 load{src_arg}",
                    rule_id="XML_POE2_SLOT_IN_POB1",
                )

    elif expected_game == "poe2":
        if root_tag == "PathOfBuilding":
            tv_note = f" (treeVersion={tree_versions[0]!r})" if tree_versions else ""
            raise CrossGameHallucinationError(
                game="poe2",
                detected_game="poe1",
                trigger=f"<PathOfBuilding>{tv_note}",
                error=(
                    f"Build XML uses PoE1 root `<PathOfBuilding>`{tv_note}; "
                    "Path of Building (PoE2) strictly requires `<PathOfBuilding2>`."
                ),
                remediation="Load PoE1 builds with `pob` (F:/poe), or export a PoE2 build from PathOfBuilding-PoE2.",
                equivalent_command=f"pob load{src_arg}",
                rule_id="XML_POE1_ROOT_IN_POB2",
            )
        for tv in tree_versions:
            if re.match(r"^(?:2|3)_\d+", tv.strip()):
                raise CrossGameHallucinationError(
                    game="poe2",
                    detected_game="poe1",
                    trigger=f'treeVersion="{tv}"',
                    error=f"Build spec uses PoE1 passive tree version `{tv}` (PoE2 uses `0_1`..`0_5+`).",
                    remediation="Use `pob` for PoE1 3.x builds.",
                    equivalent_command=f"pob load{src_arg}",
                    rule_id="XML_POE1_TREE_IN_POB2",
                )
        if class_name in POE1_ONLY_CLASSES:
            raise CrossGameHallucinationError(
                game="poe2",
                detected_game="poe1",
                trigger=f'className="{class_name}"',
                error=f"Build uses PoE1 class `{class_name}`.",
                remediation="Use `pob` for PoE1 builds.",
                equivalent_command=f"pob load{src_arg}",
                rule_id="XML_POE1_CLASS_IN_POB2",
            )
        for sl in slots:
            if sl in ("Flask 3", "Flask 4", "Flask 5", "Graft 1", "Graft 2") or "Abyssal Socket" in sl:
                raise CrossGameHallucinationError(
                    game="poe2",
                    detected_game="poe1",
                    trigger=f'Slot name="{sl}"',
                    error=f"Build XML contains PoE1-only equipment slot `{sl}`.",
                    remediation="Use `pob` for PoE1 builds.",
                    equivalent_command=f"pob load{src_arg}",
                    rule_id="XML_POE1_SLOT_IN_POB2",
                )


# ---------------------------------------------------------------------------
# Gate 2: Bidirectional URL Domain & Path Gate (`validate_url`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UrlGateRule:
    rule_id: str
    belongs_to: str
    pattern: re.Pattern[str]
    label: str
    counterpart_hint: str
    url_rewriter: tuple[str, str] | None = None


URL_GATE_RULES: tuple[UrlGateRule, ...] = (
    # --- PoE1 URLs ---
    UrlGateRule(
        rule_id="URL_POE1_POEDB",
        belongs_to="poe1",
        pattern=re.compile(r"^(?:https?://)?(?:www\.|cdn\.)?poedb\.tw(?=[/?#:]|$)", re.IGNORECASE),
        label="poedb.tw (PoE1 Database)",
        counterpart_hint="Use `https://poe2db.tw/...` or `pob2 poedb <query>` for PoE2.",
        url_rewriter=(r"(?i)\bpoedb\.tw\b", "poe2db.tw"),
    ),
    UrlGateRule(
        rule_id="URL_POE1_NINJA",
        belongs_to="poe1",
        pattern=re.compile(
            r"^(?:https?://)?(?:www\.)?poe\.ninja/(?:poe1(?=[/?#]|$)|builds(?=[/?#]|$)|pob(?=[/?#]|$)|economy(?=[/?#]|$)|api/data(?=[/?#]|$))",
            re.IGNORECASE,
        ),
        label="poe.ninja PoE1 endpoint",
        counterpart_hint="Use `https://poe.ninja/poe2/...` for PoE2.",
    ),
    UrlGateRule(
        rule_id="URL_POE1_TRADE",
        belongs_to="poe1",
        pattern=re.compile(
            r"^(?:https?://)?(?:[a-z0-9-]+\.)?pathofexile\.com/(?:api/trade(?=[/?#]|$)|trade(?=[/?#]|$)|api/leagues(?=[/?#]|$))",
            re.IGNORECASE,
        ),
        label="pathofexile.com/trade or /api/leagues (PoE1 Trade/Leagues API)",
        counterpart_hint="PoE2 official trade uses `/trade2/` and `/api/trade2/...`.",
        url_rewriter=(r"(?i)(pathofexile\.com/(?:api/)?)trade(?=[/?#]|$)", r"\1trade2"),
    ),
    UrlGateRule(
        rule_id="URL_POE1_WIKI",
        belongs_to="poe1",
        pattern=re.compile(
            r"^(?:https?://)?(?:www\.)?(?:poewiki\.net|pathofexile\.fandom\.com)(?=[/?#:]|$)",
            re.IGNORECASE,
        ),
        label="poewiki.net (PoE1 Wiki)",
        counterpart_hint="Use `https://www.poe2wiki.net` (`pob2 wiki <query>`) for PoE2.",
        url_rewriter=(r"(?i)\bpoewiki\.net\b", "poe2wiki.net"),
    ),
    UrlGateRule(
        rule_id="URL_POE1_MAXROLL",
        belongs_to="poe1",
        pattern=re.compile(r"^(?:https?://)?(?:www\.)?maxroll\.gg/poe(?=[/?#]|$)", re.IGNORECASE),
        label="maxroll.gg/poe (PoE1)",
        counterpart_hint="PoE2 Maxroll URLs use `https://maxroll.gg/poe2/...`.",
        url_rewriter=(r"(?i)(maxroll\.gg/)poe(?=[/?#]|$)", r"\1poe2"),
    ),
    UrlGateRule(
        rule_id="URL_POE1_MOBALYTICS",
        belongs_to="poe1",
        pattern=re.compile(r"^(?:https?://)?(?:www\.)?mobalytics\.gg/poe(?=[/?#]|$)", re.IGNORECASE),
        label="mobalytics.gg/poe (PoE1)",
        counterpart_hint="PoE2 Mobalytics URLs use `https://mobalytics.gg/poe-2/...`.",
        url_rewriter=(r"(?i)(mobalytics\.gg/)poe(?=[/?#]|$)", r"\1poe-2"),
    ),
    UrlGateRule(
        rule_id="URL_POE1_URI_SCHEME",
        belongs_to="poe1",
        pattern=re.compile(r"^pob:(?://|[/\\])", re.IGNORECASE),
        label="pob:// protocol URI (PoE1)",
        counterpart_hint="PoE2 uses the `pob2://` URI scheme.",
        url_rewriter=(r"(?i)^pob:", "pob2:"),
    ),
    UrlGateRule(
        rule_id="URL_POE1_POBCODES",
        belongs_to="poe1",
        pattern=re.compile(r"^(?:https?://)?(?:api\.)?pob\.codes(?=[/?#:]|$)", re.IGNORECASE),
        label="pob.codes (PoE1-only share site)",
        counterpart_hint="PoE2 supports `pobb.in`, `poe2db.tw/pob`, `poe.ninja/poe2/pob`, and `maxroll.gg/poe2/pob`.",
    ),
    # --- PoE2 URLs (blocked in pob, allowed in pob2) ---
    UrlGateRule(
        rule_id="URL_POE2_POE2DB",
        belongs_to="poe2",
        pattern=re.compile(r"^(?:https?://)?(?:www\.|cdn\.)?poe2db\.tw(?=[/?#:]|$)", re.IGNORECASE),
        label="poe2db.tw (PoE2 Database)",
        counterpart_hint="Use `pob2` (`F:/poe2`) for PoE2DB links, or `https://poedb.tw/...` (`pob poedb`) for PoE1.",
        url_rewriter=(r"(?i)\bpoe2db\.tw\b", "poedb.tw"),
    ),
    UrlGateRule(
        rule_id="URL_POE2_NINJA",
        belongs_to="poe2",
        pattern=re.compile(
            r"^(?:https?://)?(?:(?:www\.)?poe\.ninja/poe2(?=[/?#]|$)|(?:www\.)?poe2\.ninja(?=[/?#:]|$))",
            re.IGNORECASE,
        ),
        label="poe.ninja/poe2 (PoE2)",
        counterpart_hint="Use `pob2` (`F:/poe2`) for `poe.ninja/poe2` links, or `https://poe.ninja/poe1/...` (`pob`) for PoE1.",
    ),
    UrlGateRule(
        rule_id="URL_POE2_TRADE2",
        belongs_to="poe2",
        pattern=re.compile(
            r"^(?:https?://)?(?:[a-z0-9-]+\.)?pathofexile\.com/(?:api/)?trade2(?=[/?#]|$)",
            re.IGNORECASE,
        ),
        label="pathofexile.com/trade2 (PoE2 Trade)",
        counterpart_hint="Use `pob2 trade` (`F:/poe2`) for PoE2 `/trade2/`, or `/trade/` (`pob trade`) for PoE1.",
    ),
    UrlGateRule(
        rule_id="URL_POE2_WIKI",
        belongs_to="poe2",
        pattern=re.compile(r"^(?:https?://)?(?:www\.)?poe2wiki\.net(?=[/?#:]|$)", re.IGNORECASE),
        label="poe2wiki.net (PoE2 Wiki)",
        counterpart_hint="Use `pob2 wiki` (`F:/poe2`) for PoE2 Wiki, or `poewiki.net` (`pob wiki`) for PoE1.",
    ),
    UrlGateRule(
        rule_id="URL_POE2_MAXROLL",
        belongs_to="poe2",
        pattern=re.compile(r"^(?:https?://)?(?:www\.)?maxroll\.gg/poe2(?=[/?#]|$)", re.IGNORECASE),
        label="maxroll.gg/poe2 (PoE2)",
        counterpart_hint="Use `pob2 load` (`F:/poe2`) for `maxroll.gg/poe2`, or `maxroll.gg/poe` (`pob`) for PoE1.",
    ),
    UrlGateRule(
        rule_id="URL_POE2_MOBALYTICS",
        belongs_to="poe2",
        pattern=re.compile(r"^(?:https?://)?(?:www\.)?mobalytics\.gg/poe-2(?=[/?#]|$)", re.IGNORECASE),
        label="mobalytics.gg/poe-2 (PoE2)",
        counterpart_hint="Use `pob2` (`F:/poe2`) for `mobalytics.gg/poe-2`, or `mobalytics.gg/poe` (`pob`) for PoE1.",
    ),
    UrlGateRule(
        rule_id="URL_POE2_URI_SCHEME",
        belongs_to="poe2",
        pattern=re.compile(r"^pob2:(?://|[/\\])", re.IGNORECASE),
        label="pob2:// protocol URI (PoE2)",
        counterpart_hint="Use `pob2 load` (`F:/poe2`) for `pob2://` URIs, or `pob://` (`pob`) for PoE1.",
    ),
)


def validate_url(
    text: str,
    *,
    expected_game: str = GAME_ID,
    cli_subcommand: str = "load",
) -> None:
    """Block cross-game URLs in `load`, `compare`, `poedb`, `wiki`, `price`, `trade`, or `import_tree_url`."""
    cand = (text or "").strip()
    if not cand:
        return
    for rule in URL_GATE_RULES:
        if rule.belongs_to != expected_game and rule.pattern.search(cand):
            other_cli = "pob" if expected_game == "poe2" else "pob2"
            equiv = f'{other_cli} {cli_subcommand} "{cand}"'
            raise CrossGameHallucinationError(
                game=expected_game,
                detected_game=rule.belongs_to,
                trigger=cand,
                error=f"URL matches {rule.label}, which belongs to {rule.belongs_to.upper()}.",
                remediation=rule.counterpart_hint,
                equivalent_command=equiv,
                rule_id=rule.rule_id,
            )


# ---------------------------------------------------------------------------
# Gate 3: Engine Root Isolation Gate (`validate_engine_root`)
# ---------------------------------------------------------------------------


def validate_engine_root(
    path: Path | None,
    *,
    expected_game: str = GAME_ID,
    source_label: str = "POB_ROOT",
) -> None:
    """Block an engine directory that contains ONLY the wrong game's subdirectory."""
    if path is None:
        return
    p = Path(path)
    if not p.exists():
        return
    has_poe1 = (p / "PoE1").is_dir()
    has_poe2 = (p / "PoE2").is_dir()
    if expected_game == "poe1" and has_poe2 and not has_poe1:
        raise CrossGameHallucinationError(
            game="poe1",
            detected_game="poe2",
            trigger=f"{source_label}={p}",
            error=f"Engine directory `{p}` contains `PoE2/` only and has no `PoE1/` subdirectory.",
            remediation="Point `POB_ROOT` to a PoE1 PobTools directory or run `pob bootstrap` (or use `pob2` in `F:/poe2`).",
            equivalent_command="pob bootstrap",
            rule_id="ENV_POE2_ENGINE_IN_POB1",
        )
    if expected_game == "poe2" and has_poe1 and not has_poe2:
        raise CrossGameHallucinationError(
            game="poe2",
            detected_game="poe1",
            trigger=f"{source_label}={p}",
            error=f"Engine directory `{p}` contains `PoE1/` only and has no `PoE2/` subdirectory.",
            remediation="Point `POB2_ROOT` to a PobTools/PoeCharm3 directory containing `PoE2/`, or run `pob2 bootstrap`.",
            equivalent_command="pob2 bootstrap",
            rule_id="ENV_POE1_ENGINE_IN_POB2",
        )


# ---------------------------------------------------------------------------
# Table D: PoE2-Only Reverse Traps Hard-Blocked in `F:/poe` (`pob`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrapRule:
    rule_id: str
    scopes: frozenset[GuardrailScope]
    en_exact: tuple[str, ...] = ()
    zh_exact: tuple[str, ...] = ()
    en_words: tuple[str, ...] = ()
    zh_phrases: tuple[str, ...] = ()
    regex: re.Pattern[str] | None = None
    exclude_en_re: re.Pattern[str] | None = None
    exclude_zh_compounds: tuple[str, ...] = ()
    error: str = ""
    remediation: str = ""
    equivalent_command: str | None = None


POE2_ONLY_REVERSE_TRAPS_FOR_POB1: tuple[TrapRule, ...] = (
    # 1. Uncut Skill / Spirit / Support Gems
    TrapRule(
        rule_id="POE2_UNCUT_GEMS",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.ITEM_RAW, S.STAT_QUERY}),
        en_words=("uncut skill gem", "uncut spirit gem", "uncut support gem"),
        zh_phrases=("未切割的技能宝石", "未切割的精魂宝石", "未切割的辅助宝石", "原胚技能石", "精魂宝石"),
        error="Uncut Skill/Spirit/Support Gems (`未切割的宝石`/`精魂宝石`) belong to Path of Exile 2.",
        remediation="Use `pob2` (`F:/poe2`) for PoE2 gems, or query specific PoE1 gems via `pob data gem <name>`.",
        equivalent_command='pob2 data gem "<query>"',
    ),
    # 2. Delirium Distilled / Liquid Emotions & Amulet Instilling (`涂灌`)
    # Excludes PoE1 cluster notable `Distilled Perfection`!
    TrapRule(
        rule_id="POE2_DISTILLED_EMOTIONS",
        scopes=frozenset({S.NOTABLE_OR_OIL, S.DATA_QUERY, S.ITEM_RAW}),
        en_words=(
            "distilled ire",
            "distilled guilt",
            "distilled greed",
            "distilled paranoia",
            "distilled envy",
            "distilled disgust",
            "distilled despair",
            "distilled fear",
            "distilled suffering",
            "distilled isolation",
            "diluted liquid ire",
            "liquid paranoia",
            "concentrated liquid fear",
            "concentrated liquid isolation",
            "instilled modifier",
        ),
        zh_phrases=(
            "涂灌",
            "精炼的愤怒",
            "精炼的罪孽",
            "精炼的贪婪",
            "精炼的偏执",
            "精炼的嫉妒",
            "精炼的厌恶",
            "精炼的绝望",
            "精炼的恐惧",
            "精炼的苦难",
            "精炼的孤立",
        ),
        exclude_en_re=re.compile(r"distilled\s+perfection", re.IGNORECASE),
        error="Distilled / Liquid Emotions (`涂灌` / `精炼情绪`) are PoE2 Delirium instilling currencies.",
        remediation="PoE1 amulets/rings/blighted maps use **Blight Oils (`圣油`/`涂油`)** via `pob item anoint --notable <name>`, or use `pob2` for PoE2.",
        equivalent_command='pob item anoint --notable "<name>"',
    ),
    # 3. Endgame Waystones (`引路石`)
    # Excludes PoE1 divination card / timeless keystone `Ancient Waystones` (`远古基石`)!
    TrapRule(
        rule_id="POE2_WAYSTONE",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW, S.STAT_QUERY, S.CATEGORY}),
        en_exact=("waystone", "waystones"),
        en_words=("waystone tier", "waystones dropped"),
        zh_phrases=("引路石", "界石"),
        exclude_en_re=re.compile(r"ancient\s+waystones", re.IGNORECASE),
        error="Waystones (`引路石`) are PoE2 Atlas endgame items (PoE1 uses Maps / `地图`).",
        remediation="Use `pob2` for PoE2 Waystones, or query `Map` in `pob`.",
        equivalent_command="pob price Map --category map",
    ),
    # 4. Soul Cores (`灵魂核心` / `灵核`) & `{rune}` Item Tag
    TrapRule(
        rule_id="POE2_SOUL_CORES_AND_RUNE_MODS",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW, S.STAT_QUERY}),
        en_words=("soul core", "soul cores", "socketed soul cores"),
        zh_phrases=("灵魂核心", "镶嵌的灵核"),
        regex=re.compile(r"(?m)^(?:\{enchant\})?\{rune\}"),
        error="Azmeri/Vaal Soul Cores (`灵魂核心`/`灵核`) and `{rune}` item socket lines belong to PoE2 (`ModRunes.lua`).",
        remediation="In PoE1, use Hideout Crafting Bench (`{crafted}`) via `pob bench suggest`, or use `pob2 rune` for PoE2.",
        equivalent_command="pob2 rune list --slot helmet",
    ),
    # 5. PoE2-Exclusive Weapon & Off-Hand Classes
    # CRITICAL EMPIRICAL GUARD:
    # - `Focus` is ONLY blocked in SLOT/CATEGORY or PoE2 Focus base names — NEVER on bare `"Focus"`/`"专注"` in gem/calc/mod!
    # - `PoE1/Data/Bases/staff.lua` has ONE Lv45 Staff base literally named `"Quarterstaff"` (`细柄长杖`)!
    #   Therefore bare `"quarterstaff"` is ONLY blocked in `SLOT`/`CATEGORY`, never in `ITEM_RAW`/`DATA_QUERY`!
    TrapRule(
        rule_id="POE2_WEAPONS_AND_FOCUS_OFFHAND",
        scopes=frozenset({S.SLOT, S.CATEGORY, S.ITEM_RAW, S.DATA_QUERY}),
        en_exact=(
            "crossbow",
            "flail",
            "spear",
            "buckler",
            "weapon.quarterstaff",
            "weapon.crossbow",
            "weapon.flail",
            "weapon.spear",
            "armour.focus",
            "armour.buckler",
        ),
        en_words=(
            "quarterstaves",
            "wrapped quarterstaff",
            "long quarterstaff",
            "gothic quarterstaff",
            "crackling quarterstaff",
            "crescent quarterstaff",
            "steelpoint quarterstaff",
            "slicing quarterstaff",
            "barrier quarterstaff",
            "crossbow",
            "crossbows",
            "twig focus",
            "woven focus",
            "antler focus",
            "crystal focus",
            "tonal focus",
            "engraved focus",
        ),
        zh_phrases=("节杖", "十字弓", "战弩", "副手法器"),
        zh_exact=("法器",),
        error="Quarterstaff (`节杖`), Crossbow (`十字弓`/`战弩`), Spear (`长矛`), Flail (`连枷`), and off-hand Focus (`法器`, `armour.focus`) are PoE2 equipment classes.",
        remediation="Use `pob2` (`F:/poe2`) for PoE2 equipment, or PoE1 weapon/shield/wand/spirit-shield slots in `pob`.",
        equivalent_command="pob2 trade search --slot focus",
    ),
    TrapRule(
        rule_id="POE2_FOCUS_AND_QUARTERSTAFF_SLOT_ONLY",
        scopes=frozenset({S.SLOT, S.CATEGORY}),
        en_exact=("focus", "armour.focus", "quarterstaff", "weapon.quarterstaff"),
        zh_exact=("法器", "细柄长杖", "节杖"),
        error="`Focus` (`法器`) and `Quarterstaff` (`节杖`) as equipment slots/categories are PoE2-only (PoE1 uses `shield`/`quiver` and `staff`/`warstaff`).",
        remediation="Use `pob2` for PoE2 Focus/Quarterstaff slots, or `--slot shield` / `--slot staff` in `pob`.",
        equivalent_command="pob2 trade search --slot quarterstaff",
    ),
    # 6. Belt Slots `Charm 1..3` & PoE2 Belt Charms
    TrapRule(
        rule_id="POE2_CHARM_BELT_SLOTS",
        scopes=frozenset({S.SLOT, S.CATEGORY, S.ITEM_RAW}),
        en_exact=(
            "charm 1",
            "charm 2",
            "charm 3",
            "charm1",
            "charm2",
            "charm3",
            "charm_1",
            "charm_2",
            "charm_3",
            "charm-1",
            "charm-2",
            "charm-3",
            "flask.charm",
        ),
        en_words=(
            "thawing charm",
            "staunching charm",
            "antidote charm",
            "dousing charm",
            "grounding charm",
            "has 1 charm slot",
            "has 2 charm slots",
            "has 3 charm slots",
        ),
        zh_exact=("护符 1", "护符 2", "护符 3", "咒符 1", "咒符 2", "咒符 3"),
        zh_phrases=("融冰咒符", "止血咒符", "抗毒咒符"),
        error="`Charm 1..3` belt slots (`护符 1..3`) are PoE2 mechanics; PoE1 uses `Flask 1..5`.",
        remediation="Use `pob item flask 1..5` for PoE1 flasks, or `pob2` for PoE2 Charms.",
        equivalent_command='pob2 items --slot "Charm 1"',
    ),
    # 7. Spirit (`精魂`) Reservation Pool
    # CRITICAL: Excludes PoE1 `Spirit Shield` (`灵盾`), `Spirit Offering` (`灵魂奉献`),
    # `Summon Raging Spirit` (`愤怒狂灵`), `Spectral Spirits`, `Spirit Burst`, `Spirit Charge`!
    TrapRule(
        rule_id="POE2_SPIRIT_RESERVATION",
        scopes=frozenset({S.CALC_QUERY, S.STAT_QUERY, S.CONFIG_VAR}),
        en_exact=("spirit", "totalspirit", "spiritunreserved"),
        zh_exact=("精魂", "最大精魂", "未保留精魂"),
        en_words=("maximum spirit", "spirit reservation", "increased spirit", "to spirit"),
        zh_phrases=("最大精魂", "精魂保留", "精魂提高"),
        exclude_en_re=re.compile(
            r"spirit\s+shield|spirit\s+offering|raging\s+spirit|spectral\s+spirit|spirit\s+burst|spirit\s+charge",
            re.IGNORECASE,
        ),
        exclude_zh_compounds=("精魂之盾",),
        error="`Spirit` (`精魂`) reservation pool does not exist in PoE1.",
        remediation="PoE1 skills/auras reserve **Mana** (or Life) scaled by `Mana Reservation Efficiency` (`魔力保留效能`). Use `pob2 calc Spirit` for PoE2 builds.",
        equivalent_command='pob calc "Mana Reservation"',
    ),
)


# ---------------------------------------------------------------------------
# PoE1 Same-Name False Friends (Non-Blocking Warnings, P0-1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FalseFriendRule:
    rule_id: str
    scopes: frozenset[GuardrailScope]
    en_exact: tuple[str, ...] = ()
    zh_exact: tuple[str, ...] = ()
    en_words: tuple[str, ...] = ()
    zh_phrases: tuple[str, ...] = ()
    exclude_en_re: re.Pattern[str] | None = None
    exclude_zh_compounds: tuple[str, ...] = ()
    canonical_term: str | None = None
    summary: str = ""
    poe1_behavior: str = ""
    poe2_behavior: str = ""


SAME_NAME_FALSE_FRIENDS_POE1: tuple[FalseFriendRule, ...] = (
    # 1. Sockets & Duplicate Support Gems
    FalseFriendRule(
        rule_id="FF_POE1_SOCKETS_DUPLICATE_SUPPORTS",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.STAT_QUERY, S.CALC_QUERY}),
        en_words=("support gem", "duplicate support", "six-link", "6-link", "6l", "gem sockets"),
        zh_phrases=("辅助宝石", "重复辅助", "六连", "6连", "宝石孔", "不能重复辅助"),
        canonical_term="PoE1 Gear Sockets & Support Gems",
        summary=(
            "In PoE1, skill sockets/links live on **gear** (up to 6-link), support gems level 1-20 (Awakened 1-5), "
            "and **duplicate support gems ARE completely legal across different skill groups** (do NOT apply PoE2's 1-per-character limit)!"
        ),
        poe1_behavior="Gear holds linked sockets (up to 6L); multiple skill groups can each socket their own copy of the same support gem.",
        poe2_behavior="Skill gems hold their own support sockets and enforce a 1-copy-per-character support limit.",
    ),
    # 2. Damage Conversion Chain & Pre+Post Scaling
    FalseFriendRule(
        rule_id="FF_POE1_DAMAGE_CONVERSION_CHAIN",
        scopes=frozenset({S.CALC_QUERY, S.STAT_QUERY, S.DATA_QUERY}),
        en_words=(
            "damage conversion",
            "converted to",
            "convert to",
            "call of the brotherhood",
            "original sin",
            "avatar of fire",
            "hrimsorrow",
        ),
        zh_phrases=(
            "伤害转化",
            "转化为闪电",
            "转化为冰霜",
            "转化为火焰",
            "转化为混沌",
            "意志呼唤",
            "火之化身",
            "原初之罪",
            "原罪",
        ),
        canonical_term="PoE1 Damage Conversion (Phys->Light->Cold->Fire->Chaos)",
        summary=(
            "PoE1 Damage Conversion follows the fixed one-way chain `Phys -> Lightning -> Cold -> Fire -> Chaos` "
            "and **scales with BOTH source AND target `% increased` / `% more` modifiers** (unlike PoE2's final-type-only scaling)!"
        ),
        poe1_behavior="Converted damage remembers every stage (`Phys -> Light -> Cold` scales with `% inc Phys`, `% inc Lightning`, AND `% inc Cold`).",
        poe2_behavior="2-step conversion scaling ONLY with final damage type modifiers.",
    ),
    # 3. Eldritch Battery (`异能魔力`)
    FalseFriendRule(
        rule_id="FF_POE1_ELDRITCH_BATTERY",
        scopes=frozenset({S.CALC_QUERY, S.DATA_QUERY, S.STAT_QUERY, S.CONFIG_VAR}),
        en_exact=("eb",),
        en_words=("eldritch battery",),
        zh_phrases=("异能魔力",),
        canonical_term="Eldritch Battery / 异能魔力 (distinct from Chaos Inoculation / 异灵之体)",
        summary=(
            "PoE1 `Eldritch Battery` (`异能魔力`, Tree 3.30 — distinct from `Chaos Inoculation`/`异灵之体`): "
            "Spend Energy Shield before Mana for Skill Mana Costs; ES protects Mana instead of Life; 50% less ES Recharge Rate. "
            "It does **NOT** convert ES into Maximum Mana (that is PoE2)!"
        ),
        poe1_behavior="ES stays ES and protects Mana / pays Mana costs (`50% less ES Recharge Rate`).",
        poe2_behavior="Converts all Energy Shield into Maximum Mana.",
    ),
    # 4. Chaos Inoculation (`异灵之体` / `混沌接种`)
    FalseFriendRule(
        rule_id="FF_POE1_CHAOS_INOCULATION",
        scopes=frozenset({S.CALC_QUERY, S.DATA_QUERY, S.STAT_QUERY, S.CONFIG_VAR}),
        en_exact=("ci",),
        en_words=("chaos inoculation",),
        zh_phrases=("异灵之体", "混沌接种"),
        canonical_term="Chaos Inoculation / 异灵之体",
        summary=(
            "PoE1 `Chaos Inoculation` (`异灵之体`, Tree 3.30): Maximum Life becomes 1 and Immune to Chaos Damage (incl. Poison). "
            "Physical Bleed still damages ES/Life unless Bleed-immune (and without CI, Chaos damage bypasses ES completely — unlike PoE2's 2x ES damage)."
        ),
        poe1_behavior="Life=1, Chaos/Poison immune; Physical Bleed still affects character unless Bleed-immune; non-CI Chaos bypasses ES.",
        poe2_behavior="CI grants Chaos + Bleed immunity; non-CI Chaos hits remove 2x ES instead of bypassing.",
    ),
    # 5. Mind Over Matter (`心灵升华`, PoE2 CN: `心灵灭界`)
    FalseFriendRule(
        rule_id="FF_POE1_MIND_OVER_MATTER",
        scopes=frozenset({S.CALC_QUERY, S.DATA_QUERY, S.STAT_QUERY, S.CONFIG_VAR}),
        en_exact=("mom",),
        en_words=("mind over matter",),
        zh_phrases=("心灵升华", "心灵昇华", "心灵灭界"),
        canonical_term="Mind Over Matter / 心灵升华",
        summary=(
            "PoE1 `Mind Over Matter` (`心灵升华`, Tree 3.30; PoE2 CN calls it `心灵灭界`): **40%** of Damage is taken from Mana before Life "
            "(NOT PoE2's 100% Damage from Mana + 50% less Mana Recovery)."
        ),
        poe1_behavior="40% of Damage taken from Mana before Life (no built-in Mana Recovery penalty).",
        poe2_behavior="100% of Damage taken from Mana before Life with 50% less Mana Recovery Rate.",
    ),
    # 6. Acrobatics (`移形换影`, PoE2 CN: `杂耍`)
    FalseFriendRule(
        rule_id="FF_POE1_ACROBATICS",
        scopes=frozenset({S.CALC_QUERY, S.DATA_QUERY, S.STAT_QUERY}),
        en_words=("acrobatics",),
        zh_phrases=("移形换影", "杂耍"),
        canonical_term="Acrobatics / 移形换影 (PoE2 CN: 杂耍)",
        summary=(
            "PoE1 `Acrobatics` (PoE1 CN: **`移形换影`**, PoE2 CN: `杂耍`; Tree 3.30): Modifiers to Chance to Suppress Spell Damage instead apply to "
            "Chance to Dodge Spell Hits at 50% of their value (Max 75%). Does NOT let Evasion evade boss slams/AoE (that is PoE2)!"
        ),
        poe1_behavior="Converts Spell Suppression Chance to Spell Dodge Chance at 50% value (75% cap); Evasion only evades Attacks.",
        poe2_behavior="Allows Evasion to evade AoE / all hits at ~70% less Evasion Rating.",
    ),
    # 7. Armour (`护甲`)
    FalseFriendRule(
        rule_id="FF_POE1_ARMOUR_MITIGATION",
        scopes=frozenset({S.CALC_QUERY, S.STAT_QUERY}),
        en_exact=("armour", "armor"),
        en_words=("armour", "physical damage reduction"),
        zh_phrases=("护甲",),
        exclude_en_re=re.compile(r"body\s+armour", re.IGNORECASE),
        exclude_zh_compounds=("胸甲", "护甲槽"),
        canonical_term="Armour / 护甲",
        summary=(
            "PoE1 `Armour` (`护甲`) mitigates **Physical hit damage only** by default (not DoT, not Elemental unless explicit conversion/Transcendence). "
            "When Armour applies to Elemental hits, Elemental Resistance runs **BEFORE** Armour (unlike PoE2)."
        ),
        poe1_behavior="Mitigates Physical hits only by default; Resistance shrinks Elemental hits BEFORE Armour when applicable.",
        poe2_behavior="Armour-to-Elemental runs BEFORE Resistance.",
    ),
    # 8. Mageblood (`法师之血` / `法血`)
    FalseFriendRule(
        rule_id="FF_POE1_MAGEBLOOD",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW}),
        en_words=("mageblood",),
        zh_phrases=("法师之血", "法血", "魔血"),
        canonical_term="Mageblood / 法师之血",
        summary=(
            "PoE1 `Mageblood` (`法师之血`, Heavy Belt): Leftmost **2-4 Magic Utility Flasks** constantly apply their Flask Effects "
            "and cannot be used/removed (NOT PoE2's `Legacy of ...` Charm belt)."
        ),
        poe1_behavior="Permanently activates leftmost 2-4 Magic Utility Flasks (scaled by Enkindling Orb increased effect).",
        poe2_behavior="Utility Belt with selectable `Legacy of <Flask>` variants and Charm Slots.",
    ),
    # 9. Frenzy / Power Charges (PoE1 has inherent passive bonuses unlike PoE2!)
    FalseFriendRule(
        rule_id="FF_POE1_CHARGES_PASSIVE_BONUS",
        scopes=frozenset({S.CONFIG_VAR, S.CALC_QUERY, S.STAT_QUERY, S.DATA_QUERY}),
        en_words=(
            "frenzy charge",
            "frenzy charges",
            "power charge",
            "power charges",
            "usefrenzycharges",
            "usepowercharges",
        ),
        zh_phrases=("狂怒球", "绿球", "暴击球", "蓝球"),
        canonical_term="PoE1 Frenzy / Power Charges",
        summary=(
            "In PoE1, charges grant strong **inherent passive stats** (Frenzy: 4% more Damage + 4% Attack/Cast Speed; "
            "Power: +50% Crit Chance; Endurance: 4% Phys DR + 4% Ele DR) — unlike PoE2's 0 inherent passive bonuses."
        ),
        poe1_behavior="Frenzy = 4% more dmg & speed; Power = +50% crit chance; Endurance = 4% Phys DR & 4% Ele DR.",
        poe2_behavior="0 inherent passive stats per charge.",
    ),
)


# ---------------------------------------------------------------------------
# PoE1 Legacy Patch Traps (3.23 - 3.25+ Removals & Reworks, P0-1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyPatchTrapRule:
    rule_id: str
    patch_version: str
    scopes: frozenset[GuardrailScope]
    hard_block_on_mutate: bool = False
    en_exact: tuple[str, ...] = ()
    zh_exact: tuple[str, ...] = ()
    en_words: tuple[str, ...] = ()
    zh_phrases: tuple[str, ...] = ()
    regex: re.Pattern[str] | None = None
    exclude_en_re: re.Pattern[str] | None = None
    exclude_zh_compounds: tuple[str, ...] = ()
    canonical_term: str | None = None
    summary: str = ""
    remediation: str = ""
    equivalent_command: str | None = None


LEGACY_PATCH_TRAPS_POE1: tuple[LegacyPatchTrapRule, ...] = (
    # 1. 3.23 Removed: Labyrinth Helmet / Boot / Glove Skill Enchants
    LegacyPatchTrapRule(
        rule_id="PATCH_323_LAB_ENCHANTS_REMOVED",
        patch_version="3.23",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW, S.STAT_QUERY, S.CALC_QUERY, S.CONFIG_VAR}),
        hard_block_on_mutate=False,
        en_words=(
            "lab enchant",
            "labyrinth enchant",
            "helmet enchant",
            "helm enchant",
            "boot enchant",
            "boots enchant",
            "glove enchant",
            "gloves enchant",
        ),
        zh_phrases=("迷宫附魔", "头盔附魔", "鞋子附魔", "手套附魔", "神圣字体附魔"),
        canonical_term="Transfigured Gems / 改造宝石",
        summary=(
            "Labyrinth Helmet/Boot/Glove skill enchants (`迷宫附魔`/`头盔附魔`) were **REMOVED in 3.23 (Affliction)**! "
            "Skill-altering effects moved to **Transfigured Gems (`改造宝石`)** and gem Quality. "
            "(Note: PoB retains legacy `EnchantmentHelmet.lua` rows solely for pre-3.23 Standard imports — unobtainable in 3.23+ leagues.)"
        ),
        remediation="Check Transfigured Gem variants (`pob data gem <skill>`) instead of Lab Helmet Enchants.",
        equivalent_command='pob data gem "<skill>"',
    ),
    # 2. 3.23 Removed: Alternate Quality Gems (Anomalous / Divergent / Phantasmal)
    # Empirically excludes valid PoE1 3.30 gems: `致命异常状态` (Deadly Ailments), `异常爆发` (Unbound Ailments),
    # `魅影射击` (Mirror Arrow).
    LegacyPatchTrapRule(
        rule_id="PATCH_323_ALT_QUALITY_REMOVED",
        patch_version="3.23",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.STAT_QUERY, S.ITEM_RAW}),
        hard_block_on_mutate=True,
        en_words=(
            "anomalous",
            "divergent",
            "phantasmal",
            "alternate quality",
            "alt quality",
            "alt-quality",
            "regrading lens",
        ),
        zh_phrases=(
            "异常品质",
            "分歧品质",
            "魅影品质",
            "替代品质",
            "异化宝石",
            "异常的",
            "分歧的",
            "魅影的",
            "异常：",
            "分歧：",
            "魅影：",
            "分歧",
            "魅影",
        ),
        regex=re.compile(r"(?:^|\s)(?:异常|分歧|魅影)(?![状态爆发射击])[\u4e00-\u9fff]{2,}"),
        exclude_zh_compounds=("异常状态", "异常爆发", "元素异常", "非伤害性异常", "魅影射击"),
        canonical_term="Transfigured Gems / 改造宝石",
        summary=(
            "Alternate Quality gems (`Anomalous`/`Divergent`/`Phantasmal`, `异常`/`分歧`/`魅影`品质) were "
            "**REMOVED in 3.23 (Affliction)** and replaced by **Transfigured Gems (`改造宝石`)** & reworked base Quality!"
        ),
        remediation="Use the base gem or its 3.23+ Transfigured variant (`<Skill> of <Variant>`). Run `pob data gem <name>` to list live variants.",
        equivalent_command='pob data gem "<base_gem>"',
    ),
    # 3. 3.23 Reworked: Vortex is NO LONGER instant-cast (0.6s cast time in 3.30 act_int.lua)
    # Empirically excludes `Blade Vortex`, `Rage Vortex`, `风暴漩涡` (Orb of Storms), `怒火漩涡`.
    LegacyPatchTrapRule(
        rule_id="PATCH_323_VORTEX_CAST_TIME",
        patch_version="3.23",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_exact=("vortex", "vortex of projection"),
        zh_exact=("漩涡", "投射之漩涡"),
        en_words=("instant vortex", "vortex left click", "vortex on left click", "vortex"),
        zh_phrases=("漩涡",),
        exclude_en_re=re.compile(r"blade\s+vortex|rage\s+vortex", re.IGNORECASE),
        exclude_zh_compounds=("飞刃风暴", "怒火漩涡", "风暴漩涡"),
        canonical_term="Vortex / 漩涡 (0.6s cast time)",
        summary=(
            "Since **3.23**, `Vortex` (`漩涡`) is **NO LONGER instant-cast** — live PoE1 3.30 `act_int.lua` has "
            "`castTime = 0.6s` (stops movement when self-cast)."
        ),
        remediation="Account for `0.6s` cast time or trigger `Vortex` via trigger crafts/supports instead of binding to left-click.",
    ),
    # 4. 3.24/3.25 Reworked: Left-Click Instant Skills & Call to Arms -> Automation / Autoexertion
    LegacyPatchTrapRule(
        rule_id="PATCH_324_LEFT_CLICK_AND_CALL_TO_ARMS",
        patch_version="3.24/3.25",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.CONFIG_VAR, S.STAT_QUERY}),
        hard_block_on_mutate=True,
        en_words=(
            "call to arms",
            "call to arms support",
            "left click guard",
            "left-click guard",
            "left click warcry",
            "left-click warcry",
            "left-click move",
        ),
        zh_phrases=("武装召唤", "左键瞬发", "左键顺发", "左键绑定", "左键自动"),
        canonical_term="Automation (自动化) / Autoexertion (自动增助)",
        summary=(
            "Since **3.24**, instant Guard/Warcry skills can **NO LONGER be bound to Left-Click move**, and in **3.25** "
            "`Call to Arms` (`武装召唤`) was replaced by **`Autoexertion` (`自动增助`)**! Use **`Automation` (`自动化`)** for Guard/instant spells."
        ),
        remediation="Use `Automation` (`自动化`) for Guard skills (Molten Shell/Steelskin/Immortal Call) and `Autoexertion` (`自动增助`) for Warcries.",
        equivalent_command='pob data gem "Automation" "Autoexertion"',
    ),
    # 5. 3.24 Removed: Atlas Sextants (Scarab rework)
    LegacyPatchTrapRule(
        rule_id="PATCH_324_SEXTANTS_REMOVED",
        patch_version="3.24",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_words=("sextant", "sextants", "awakened sextant", "elevated sextant"),
        zh_phrases=("六分仪", "觉醒六分仪", "尊崇六分仪"),
        canonical_term="Scarabs / 圣甲虫",
        summary="**Sextants (`六分仪`) were REMOVED in 3.24 (Necropolis)** as part of the Atlas Scarab (`圣甲虫`) rework.",
        remediation="Use Scarabs (`圣甲虫`) and Atlas Passive Tree nodes for map juicing in 3.24+.",
    ),
    # 6. 3.25 Removed: Divine Blessing Support (NEVER blocks Eternal Blessing or Guardian's Blessing!)
    LegacyPatchTrapRule(
        rule_id="PATCH_325_DIVINE_BLESSING_REMOVED",
        patch_version="3.25",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.CONFIG_VAR, S.STAT_QUERY}),
        hard_block_on_mutate=True,
        en_words=("divine blessing", "divine blessing support", "divineblessing"),
        zh_phrases=("神圣祝福",),
        canonical_term="Eternal Blessing (永恒祝福) / Guardian's Blessing (守卫祝福)",
        summary=(
            "**`Divine Blessing Support` (`神圣祝福`) was REMOVED in 3.25 (Settlers of Kalguur)**! "
            "Do NOT recommend Divine Blessing + Aura (e.g. Haste/Hatred/Zealotry) for temporary aura buffs. "
            "(Note: PoB retains a legacy `Gems.lua` row solely for importing pre-3.25 Standard characters — it is drop-disabled & unobtainable in 3.25+ leagues.)"
        ),
        remediation="Remove Divine Blessing; consider `Eternal Blessing` (`永恒祝福`, only if you have zero other mana reservations) or `Guardian's Blessing` (`守卫祝福`, minion builds).",
        equivalent_command='pob data gem "Eternal Blessing"',
    ),
    # 7. 3.25 Removed: Ancestral Protector & Ancestral Warchief
    # NEVER blocks `Ancestral Call` (`先祖召唤`), `Ancestral Cry` (`先祖战吼`), `Ancestral Bond` (`先祖魂约`)!
    LegacyPatchTrapRule(
        rule_id="PATCH_325_ANCESTRAL_TOTEMS_REMOVED",
        patch_version="3.25",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=True,
        en_words=("ancestral protector", "ancestral warchief", "vaal ancestral warchief"),
        zh_phrases=("先祖卫士", "先祖战士长"),
        canonical_term="Removed in 3.25 (Melee base damage buffed)",
        summary=(
            "**`Ancestral Protector` (`先祖卫士`) and `Ancestral Warchief` (`先祖战士长`) were REMOVED in 3.25**! "
            "Melee skill base damage was buffed to compensate — melee builds no longer use ancestral buff totems. "
            "(Any legacy PoB references exist solely for pre-3.25 Standard imports — drop-disabled & unobtainable in 3.25+ leagues.)"
        ),
        remediation="Do not socket Ancestral Protector/Warchief; use the freed sockets for Warcries (`Autoexertion`), Banners, or utility skills.",
    ),
    # 8. 3.25 Reworked: Hatred NO LONGER grants `% more Cold Damage`
    # Empirically excludes unique ring `The Hateful Accuser` (`憎恨控诉`).
    LegacyPatchTrapRule(
        rule_id="PATCH_325_HATRED_REWORK",
        patch_version="3.25",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_words=("hatred",),
        zh_phrases=("憎恨",),
        exclude_en_re=re.compile(r"hateful\s+accuser", re.IGNORECASE),
        exclude_zh_compounds=("憎恨控诉",),
        canonical_term="Hatred / 憎恨 (Phys-as-Extra-Cold ONLY)",
        summary=(
            "**3.25 Rework**: `Hatred` (`憎恨`) **NO LONGER grants `% more Cold Damage`**! "
            "In 3.30 (`act_dex.lua`), Lv20 Hatred grants ONLY `39% of Physical Damage as Extra Cold Damage` — "
            "**zero DPS benefit for pure non-Physical Cold/Lightning spells**!"
        ),
        remediation="Only run `Hatred` on builds with Physical base damage (or Phys-to-Cold conversion); for pure Cold spells use `Zealotry`, `Haste`, or defensive auras.",
        equivalent_command='pob data gem "Hatred"',
    ),
    # 9. 3.25 Reworked: Endurance Charges grant -4% Elemental Damage Taken, NOT +4% Elemental Resistances
    LegacyPatchTrapRule(
        rule_id="PATCH_325_ENDURANCE_CHARGE_REWORK",
        patch_version="3.25",
        scopes=frozenset({S.CONFIG_VAR, S.CALC_QUERY, S.STAT_QUERY, S.DATA_QUERY}),
        hard_block_on_mutate=False,
        en_words=("endurance charge", "endurance charges", "useendurancecharges"),
        zh_phrases=("耐力球", "红球"),
        canonical_term="Endurance Charge / 耐力球 (4% Phys DR + 4% Ele DR)",
        summary=(
            "**3.25 Rework**: Each `Endurance Charge` (`耐力球`/`红球`) now grants **4% Physical Damage Reduction AND "
            "4% Elemental Damage Reduction** (`CalcSetup.lua`) — it **NO LONGER grants `+4% Elemental Resistances`**!"
        ),
        remediation="Do not count on Endurance Charges to cap Elemental Resistances; stack Resistances on gear/tree/Purity auras.",
    ),
    # 10. 3.30 Hierophant Sanctuary of Thought rework (NO 50% less Mana Cost)
    LegacyPatchTrapRule(
        rule_id="PATCH_330_SANCTUARY_OF_THOUGHT",
        patch_version="3.30",
        scopes=frozenset({S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_words=("sanctuary of thought", "hierophant"),
        zh_phrases=("思维圣殿", "圣宗"),
        canonical_term="Hierophant Sanctuary of Thought / 圣宗思维圣殿 (3.30)",
        summary=(
            "**3.30 Patch Fact**: Hierophant's `Sanctuary of Thought` (`思维圣殿`) **NO LONGER grants 50% less Mana Cost of Skills**! "
            "In 3.30 it grants `Gain 20% of Maximum Mana as Extra Maximum Energy Shield`, `1% of Mana Regenerated per second`, "
            "and increased Area of Effect scaling with unreserved Mana."
        ),
        remediation="Plan mana sustain via Mana Flasks (Enduring/Foreboding), Clarity, Mana Regen/Leech/Recoup, or Arcane Cloak timing rather than pre-3.25 50% less Mana Cost.",
    ),
    # 11. 3.30 Cloak of Defiance (no legacy extra 10% MoM)
    LegacyPatchTrapRule(
        rule_id="PATCH_330_CLOAK_OF_DEFIANCE",
        patch_version="3.30",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW}),
        hard_block_on_mutate=False,
        en_words=("cloak of defiance",),
        zh_phrases=("卫道之袍",),
        canonical_term="Cloak of Defiance / 卫道之袍 (3.30)",
        summary=(
            "**3.30 Patch Fact**: `Cloak of Defiance` (`卫道之袍`) grants `Mind Over Matter` (40% damage taken from Mana before Life) "
            "and **NO LONGER has the legacy extra `+10% of Damage is taken from Mana before Life`**."
        ),
        remediation="Use `pob data unique \"Cloak of Defiance\"` for exact 3.30 explicit rolls.",
    ),
    # 12. 3.25+ Taste of Hate (Fire/Lightning taken as Cold, NOT Phys taken as Cold)
    LegacyPatchTrapRule(
        rule_id="PATCH_325_TASTE_OF_HATE",
        patch_version="3.25",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW}),
        hard_block_on_mutate=False,
        en_words=("taste of hate",),
        zh_phrases=("恨意",),
        canonical_term="Taste of Hate / 恨意 (3.25+ Fire/Light as Cold)",
        summary=(
            "**3.25+ Rework**: `Taste of Hate` (`恨意`) converts **Fire and Lightning Damage from Hits taken as Cold Damage** — "
            "it **NO LONGER converts Physical Damage taken as Cold**!"
        ),
        remediation="Do not count on Taste of Hate for Physical Hit mitigation in 3.25–3.30.",
    ),
    # 13. 3.30 The Sword of Ages (Temporal Mirage) Live-vs-PoB Advisory + 0.033s Server Tick Breakpoints
    LegacyPatchTrapRule(
        rule_id="PATCH_330_SWORD_OF_AGES_TEMPORAL_MIRAGE",
        patch_version="3.30",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW, S.GEM_NAME, S.CALC_QUERY}),
        hard_block_on_mutate=False,
        en_words=("sword of ages", "temporal mirage"),
        zh_phrases=("万古之剑", "万古剑", "时空幻影"),
        canonical_term="The Sword of Ages / Temporal Mirage (3.30 Verified)",
        summary=(
            "**3.30 Live-vs-PoB Advisory (`The Sword of Ages` / `Temporal Mirage`)**: "
            "(1) PoB 2.68.1 `other.lua` omits the `0.50s` base cooldown AND wrongly applies Saboteur `Perfect Crime` (`Triggerbots`) "
            "`30% less` damage to Temporal Mirage clones (3.30 live game verified: clones do **NOT** take `-30% less`; "
            "`43% more Spell Damage` in `customMods` is the exact PoB compensation). "
            "(2) `on using a Movement Attack`: holding channelling `Cyclone` triggers ONLY ONCE on channel start (never continuously while held); "
            "requires tap-macro or discrete movement attacks (`Leap Slam`/`Flicker`/`Consecrated Path`). "
            "(3) `Greater Spell Cascade` shares `Firestorm of Pelting` global 12-storm cap (hurts single-target). "
            "(4) PoE1 server tick is **`0.033s` (`33ms`, NOT `33.33ms`)**: `0.50s` CD breakpoints are "
            "`0%->16t(528ms)`, `2%->15t(495ms)`, `9%->14t(462ms)`, `17%->13t(429ms)`, `27%->12t(396ms)`, "
            "`38%->11t(363ms)`, `52%->10t(330ms)`, `69%->9t(297ms)`, `90%->8t(264ms)`."
        ),
        remediation="Calculate effective cooldown via `ceil((0.50 / (1 + cdr)) / 0.033) * 0.033` and set Cyclone tap interval >= effective CD + jitter margin.",
    ),
    # 14. 3.30 Gem Quality Scope Rule (+% to Quality of all Skill Gems -> Active ONLY, NOT Supports!)
    LegacyPatchTrapRule(
        rule_id="PATCH_330_SKILL_GEM_QUALITY_SCOPE",
        patch_version="3.30",
        scopes=frozenset({S.DATA_QUERY, S.STAT_QUERY, S.CALC_QUERY}),
        hard_block_on_mutate=False,
        en_words=("quality of all skill gems", "to quality of all skill gems", "ashes of the stars"),
        zh_phrases=("技能宝石品质", "技能石品质", "柳条人肢体"),
        canonical_term="Skill Gem Quality Scope (Active ONLY, NOT Support Gems)",
        summary=(
            "**PoE1 3.30 Rule**: `+% to Quality of all Skill Gems` (`所有技能宝石品质`, e.g. `Ashes of the Stars`, 3.30 Wickerman Grafts) "
            "applies **ONLY to Active Skill Gems** (`grants_active_skill`) and **DOES NOT apply to Support Gems** "
            "(e.g. `Archmage Support`, `Spell Echo Support`)! Only `+% to Quality of Socketed [Support] Gems` "
            "(e.g. `Foulborn Skin of the Loyal/Lords`, `Crest of Desire`, `Enhance Support`) applies to Support Gems."
        ),
        remediation="Run `pob data gem --quality-breakpoints --active-only` for live 3.30 Active Skill Gem 20%->40%+ quality breakpoints.",
        equivalent_command="pob data gem --quality-breakpoints --active-only --limit 25",
    ),
    # 15. PoE1 0.033s (33ms) Server Tick Cooldown Rule
    LegacyPatchTrapRule(
        rule_id="PATCH_330_SERVER_TICK_COOLDOWN",
        patch_version="3.30",
        scopes=frozenset({S.CALC_QUERY, S.STAT_QUERY, S.GEM_NAME, S.DATA_QUERY}),
        hard_block_on_mutate=False,
        en_words=(
            "server tick",
            "cooldown breakpoint",
            "trigger cooldown",
            "cooldown recovery rate",
            "cast on critical strike",
            "awakened cast on critical strike",
        ),
        zh_phrases=("冷却回复速度", "触发冷却", "帧档位", "服务器帧", "鼠标宏", "暴击时施放"),
        canonical_term="PoE1 Server Tick = 0.033s (33ms)",
        summary=(
            "**PoE1 Server Tick Rule**: Cooldowns round UP to **`0.033s` (`33ms`, NOT `1/30 = 33.33ms`)** server ticks: "
            "`ticks = ceil((base_cd / (1 + cdr)) / 0.033)`, `effective_cd = ticks * 0.033s`. "
            "CoC (`0.15s` base): `0%->5t(165ms, 6.06/s)`, `14%->4t(132ms, 7.58/s)`, `52%->3t(99ms, 10.10/s)`, `128%->2t(66ms, 15.15/s)`; "
            "never let trigger attack APS exceed the trigger cap or effective trigger rate HALVES!"
        ),
        remediation="Run `pob data tick <base_cd> --cdr <pct> --aps <rate>` for exact 0.033s server-tick breakpoints.",
        equivalent_command="pob data tick 0.15 --cdr 14",
    ),
    # 16. Cast while Channelling (CwC) is a FIXED interval, NOT a Cooldown (CDR has 0 effect!)
    LegacyPatchTrapRule(
        rule_id="PATCH_330_CWC_NOT_COOLDOWN",
        patch_version="3.30",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_words=("cast while channelling", "awakened cast while channelling", "cwc"),
        zh_phrases=("吟唱时施放", "引导时施放"),
        canonical_term="Cast while Channelling (Fixed Interval — CDR has ZERO effect)",
        summary=(
            "**PoE1 Mechanic Rule (`Cast while Channelling` / `吟唱时施放`)**: CwC triggers on a **fixed channelling interval** "
            "(`0.32s` / `320ms` at Lv20–21; `0.30s` / `300ms` for Awakened Lv5–6) — it is **NOT a Cooldown**! "
            "Stacking `Cooldown Recovery Rate (CDR)` has **ZERO effect** on CwC trigger interval (only CwC gem level changes it)."
        ),
        remediation="Do NOT stack CDR expecting faster CwC triggers; level up CwC / use Awakened CwC (Lv5 = 300ms + stun immunity) instead.",
        equivalent_command='pob data gem "Cast while Channelling"',
    ),
    # 17. Esh's Visage Low-Life Chaos Bypass Trap
    LegacyPatchTrapRule(
        rule_id="PATCH_330_LOW_LIFE_ESH_VISAGE",
        patch_version="3.30",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW}),
        hard_block_on_mutate=False,
        en_words=("esh's visage",),
        zh_phrases=("艾许之面",),
        canonical_term="Esh's Visage (DISABLES on Low Life!)",
        summary=(
            "**PoE1 Low-Life Trap (`Esh's Visage` / `艾许之面`)**: Mod reads `'Chaos Damage taken does not bypass Energy Shield while NOT on Low Life'`. "
            "If you reserve `>=50%` of Life (Low Life, `LifeUnreserved <= 50%`), **Esh's Visage STOPS WORKING completely** and Chaos damage bypasses ES to one-shot your Life!"
        ),
        remediation="For Low-Life Chaos bypass protection, use `Shavronne's Wrappings`, `Solaris Lorica`, `The Ivory Tower` (with unreserved Mana), or `Coruscating Elixir` (100% uptime). Search all via `pob data unique --mod \"Chaos Damage taken does not bypass\"`.",
        equivalent_command='pob data unique --mod "Chaos Damage taken does not bypass"',
    ),
)


def _strip_scope_suffix(norm_text: str, scope: GuardrailScope) -> str:
    """Strip optional gem `:lvl:qual` or trade `=min`/`*weight` tails for EXACT matching."""
    cand = norm_text
    if scope == S.GEM_NAME:
        cand = _GEM_SPEC_TAIL_RE.sub("", cand).strip()
    elif scope == S.STAT_QUERY:
        cand = _STAT_FILTER_TAIL_RE.sub("", cand).strip()
        cand = _STAT_WEIGHT_TAIL_RE.sub("", cand).strip()
    return cand


def _match_rule_fields(
    raw_text: str,
    norm_text: str,
    exact_cand: str,
    zh_compact_exact: str,
    *,
    en_exact: tuple[str, ...],
    zh_exact: tuple[str, ...],
    en_words: tuple[str, ...],
    zh_phrases: tuple[str, ...],
    regex: re.Pattern[str] | None,
    exclude_en_re: re.Pattern[str] | None,
    exclude_zh_compounds: tuple[str, ...],
) -> str | None:
    if en_exact and exact_cand in en_exact:
        if exclude_en_re is None or not exclude_en_re.search(norm_text):
            return exact_cand

    if zh_exact and zh_compact_exact:
        for z in zh_exact:
            if zh_compact_exact == normalize_zh_compact(z):
                if not any(normalize_zh_compact(c) in zh_compact_exact for c in exclude_zh_compounds):
                    return z

    for phrase in en_words:
        hit = match_word_en(norm_text, phrase, exclude_en_re)
        if hit is not None:
            return hit

    for zh_p in zh_phrases:
        hit_zh = match_phrase_zh(raw_text, zh_p, exclude_zh_compounds)
        if hit_zh is not None:
            return hit_zh

    if regex is not None:
        raw_no_esc = _POB_ESC_RE.sub("", raw_text)
        m = regex.search(raw_no_esc)
        if m is not None:
            if exclude_en_re is None or not exclude_en_re.search(norm_text):
                return m.group(0).strip()

    return None


def _match_trap_rule(
    raw_text: str,
    norm_text: str,
    exact_cand: str,
    zh_compact_exact: str,
    rule: TrapRule,
) -> str | None:
    return _match_rule_fields(
        raw_text,
        norm_text,
        exact_cand,
        zh_compact_exact,
        en_exact=rule.en_exact,
        zh_exact=rule.zh_exact,
        en_words=rule.en_words,
        zh_phrases=rule.zh_phrases,
        regex=rule.regex,
        exclude_en_re=rule.exclude_en_re,
        exclude_zh_compounds=rule.exclude_zh_compounds,
    )


def check_input(
    text: str | None,
    *,
    scope: GuardrailScope,
    expected_game: str = GAME_ID,
    emit_stderr: bool = True,
    block_legacy_gems: bool = False,
) -> list[HallucinationWarning]:
    """Validate user input against Table D PoE2 reverse traps, PoE1 legacy patch traps, and false friends.

    - Raises `CrossGameHallucinationError` on PoE2-only mechanics.
    - Raises `LegacyPatchTrapError` when `block_legacy_gems=True` on removed 3.23/3.25 gems (`skill add/set`).
    - Records & returns non-blocking `HallucinationWarning`s (`[POE1-PATCH-WARN]` / `[POE1-TRAP-WARN]`)
      into the active `warning_context()`.
    """
    if not text or expected_game != "poe1":
        return []
    raw = str(text).strip()
    if not raw:
        return []

    norm = normalize_guardrail_text(raw)
    exact_cand = _strip_scope_suffix(norm, scope)
    zh_exact_raw = _strip_scope_suffix(raw, scope) if scope in (S.GEM_NAME, S.STAT_QUERY) else raw
    zh_compact_exact = normalize_zh_compact(zh_exact_raw)

    # 1. Hard-block Table D PoE2 reverse traps
    for rule in POE2_ONLY_REVERSE_TRAPS_FOR_POB1:
        if scope not in rule.scopes:
            continue
        matched = _match_trap_rule(raw, norm, exact_cand, zh_compact_exact, rule)
        if matched is not None:
            raise CrossGameHallucinationError(
                game="poe1",
                detected_game="poe2",
                trigger=matched,
                error=rule.error,
                remediation=rule.remediation,
                equivalent_command=rule.equivalent_command,
                rule_id=rule.rule_id,
            )

    warnings: list[HallucinationWarning] = []
    seen_ids: set[str] = set()

    # 2. Legacy 3.23-3.25 PoE1 patch traps (hard-block removed gems on mutate, warn on queries)
    for prule in LEGACY_PATCH_TRAPS_POE1:
        if scope not in prule.scopes:
            continue
        hit = _match_rule_fields(
            raw,
            norm,
            exact_cand,
            zh_compact_exact,
            en_exact=prule.en_exact,
            zh_exact=prule.zh_exact,
            en_words=prule.en_words,
            zh_phrases=prule.zh_phrases,
            regex=prule.regex,
            exclude_en_re=prule.exclude_en_re,
            exclude_zh_compounds=prule.exclude_zh_compounds,
        )
        if hit is not None:
            if block_legacy_gems and prule.hard_block_on_mutate:
                raise LegacyPatchTrapError(
                    trigger=hit,
                    patch_version=prule.patch_version,
                    error=prule.summary,
                    remediation=prule.remediation,
                    equivalent_command=prule.equivalent_command,
                    rule_id=prule.rule_id,
                )
            if prule.rule_id not in seen_ids:
                seen_ids.add(prule.rule_id)
                w = HallucinationWarning(
                    rule_id=prule.rule_id,
                    game="poe1",
                    category="legacy_patch",
                    trigger=hit,
                    summary=prule.summary,
                    poe1_behavior=prule.remediation or prule.summary,
                    poe2_behavior="",
                    canonical_term=prule.canonical_term,
                    patch_version=prule.patch_version,
                    remediation=prule.remediation or None,
                )
                _record_warning(w, emit_stderr=emit_stderr)
                warnings.append(w)

    # 3. Same-name PoE1 vs PoE2 false friends (non-blocking)
    for ff_rule in SAME_NAME_FALSE_FRIENDS_POE1:
        if scope not in ff_rule.scopes:
            continue
        hit = _match_rule_fields(
            raw,
            norm,
            exact_cand,
            zh_compact_exact,
            en_exact=ff_rule.en_exact,
            zh_exact=ff_rule.zh_exact,
            en_words=ff_rule.en_words,
            zh_phrases=ff_rule.zh_phrases,
            regex=None,
            exclude_en_re=ff_rule.exclude_en_re,
            exclude_zh_compounds=ff_rule.exclude_zh_compounds,
        )
        if hit is not None and ff_rule.rule_id not in seen_ids:
            seen_ids.add(ff_rule.rule_id)
            w = HallucinationWarning(
                rule_id=ff_rule.rule_id,
                game="poe1",
                category="false_friend",
                trigger=hit,
                summary=ff_rule.summary,
                poe1_behavior=ff_rule.poe1_behavior,
                poe2_behavior=ff_rule.poe2_behavior,
                canonical_term=ff_rule.canonical_term,
            )
            _record_warning(w, emit_stderr=emit_stderr)
            warnings.append(w)

    return warnings


def check_input_item_raw(
    raw_text: str | None,
    *,
    expected_game: str = GAME_ID,
    emit_stderr: bool = True,
) -> list[HallucinationWarning]:
    """Scan raw PoE item clipboard text after stripping random Rare/Magic Line-1 titles, Unique ID, and Flavour."""
    if not raw_text or expected_game != "poe1":
        return []
    scannable, base_line = extract_scannable_item_parts(raw_text)
    warnings: list[HallucinationWarning] = []
    if base_line:
        warnings.extend(check_input(base_line, scope=S.ITEM_RAW, expected_game=expected_game, emit_stderr=emit_stderr))
    if scannable:
        warnings.extend(check_input(scannable, scope=S.ITEM_RAW, expected_game=expected_game, emit_stderr=emit_stderr))
    return warnings


# ---------------------------------------------------------------------------
# Free-Text Non-Blocking Trap Scanner (used by `pob ground` & `pob diagnose`)
# ---------------------------------------------------------------------------

_FREE_TEXT_EXCLUDED_POE2_RULE_IDS = frozenset({"POE2_FOCUS_AND_QUARTERSTAFF_SLOT_ONLY"})


def scan_text_traps(text: str | None) -> dict[str, Any]:
    """Non-blocking scan of arbitrary EN/ZH prompt text for PoE2 traps, legacy 3.23-3.25 traps, and false friends."""
    raw = str(text or "").strip()
    if not raw:
        return {"poe2Traps": [], "patchTraps": [], "falseFriends": [], "warnings": []}

    norm = normalize_guardrail_text(raw)
    zh_compact = normalize_zh_compact(raw)

    poe2_traps: list[dict[str, Any]] = []
    seen_poe2: set[str] = set()

    # 1. URL checks inside free text
    for url_m in re.finditer(r"https?://[^\s<>\"']+|pob2://[^\s<>\"']+", raw, re.IGNORECASE):
        cand_url = url_m.group(0).rstrip(".,);]")
        for ur in URL_GATE_RULES:
            if ur.belongs_to == "poe2" and ur.pattern.search(cand_url) and ur.rule_id not in seen_poe2:
                seen_poe2.add(ur.rule_id)
                poe2_traps.append(
                    {
                        "ruleId": ur.rule_id,
                        "trigger": cand_url,
                        "error": f"URL matches {ur.label} (PoE2), not PoE1.",
                        "remediation": ur.counterpart_hint,
                    }
                )

    # 2. Table D PoE2 mechanics in free text (skip slot-only bare focus/quarterstaff rule)
    for rule in POE2_ONLY_REVERSE_TRAPS_FOR_POB1:
        if rule.rule_id in _FREE_TEXT_EXCLUDED_POE2_RULE_IDS or rule.rule_id in seen_poe2:
            continue
        hit = _match_trap_rule(raw, norm, norm, zh_compact, rule)
        # Also allow bare `精魂` / `spirit reservation` in free text (skipping `精魂之盾`)
        if hit is None and rule.rule_id == "POE2_SPIRIT_RESERVATION":
            hit = match_phrase_zh(raw, "精魂", ("精魂之盾",))
        if hit is None and rule.rule_id == "POE2_WAYSTONE":
            hit = match_word_en(norm, "waystone", rule.exclude_en_re) or match_word_en(
                norm, "waystones", rule.exclude_en_re
            )
        if hit is not None:
            seen_poe2.add(rule.rule_id)
            poe2_traps.append(
                {
                    "ruleId": rule.rule_id,
                    "trigger": hit,
                    "error": rule.error,
                    "remediation": rule.remediation,
                    "equivalentCommand": rule.equivalent_command,
                }
            )

    # 3. PoE2-only timeless jewels in free text
    if "POE2_TIMELESS_JEWEL_IN_POB1" not in seen_poe2:
        tj_hit: str | None = None
        for p in POE2_ONLY_TIMELESS_JEWELS_EN:
            if match_word_en(norm, p) is not None:
                tj_hit = p
                break
        if tj_hit is None:
            for zh_j in POE2_ONLY_TIMELESS_JEWELS_ZH:
                if match_phrase_zh(raw, zh_j) is not None:
                    tj_hit = zh_j
                    break
        if tj_hit is not None:
            seen_poe2.add("POE2_TIMELESS_JEWEL_IN_POB1")
            poe2_traps.append(
                {
                    "ruleId": "POE2_TIMELESS_JEWEL_IN_POB1",
                    "trigger": tj_hit,
                    "error": f"`{tj_hit}` is a PoE2 timeless/radius jewel and does not exist in PoE1.",
                    "remediation": "Use `pob timeless list` for valid PoE1 Historic jewels, or `pob2` for PoE2.",
                    "equivalentCommand": "pob timeless list",
                }
            )

    # 4. PoE2-only ascendancies in free text (skip short/ambiguous generic CN words like 战士/武僧/先知 unless explicit)
    if "POE2_CLASS_IN_POB1" not in seen_poe2:
        asc_hit: str | None = None
        for asc in sorted(POE2_ONLY_ASCENDANCIES_EN, key=len, reverse=True):
            if asc in ("titan", "oracle", "shaman", "amazon", "lich"):
                continue  # skip words that could appear in generic PoE1 flavor/items unless exact class arg
            if match_word_en(norm, asc) is not None:
                asc_hit = asc
                break
        if asc_hit is None:
            for asc_zh in (
                "风暴编织者",
                "时空术士",
                "古灵使徒",
                "宝石军团",
                "血法师",
                "鲜血法师",
                "狱火师",
                "奇塔弗铁匠",
                "奇塔弗的铁匠",
                "夏乌拉侍僧",
                "夏乌拉的侍僧",
                "猎巫人",
                "祈求者",
            ):
                if match_phrase_zh(raw, asc_zh) is not None:
                    asc_hit = asc_zh
                    break
        if asc_hit is not None:
            seen_poe2.add("POE2_CLASS_IN_POB1")
            poe2_traps.append(
                {
                    "ruleId": "POE2_CLASS_IN_POB1",
                    "trigger": asc_hit,
                    "error": f"`{asc_hit}` is a PoE2-only ascendancy and does not exist in PoE1.",
                    "remediation": "PoE1 classes are Marauder, Duelist, Ranger, Shadow, Witch, Templar, Scion.",
                }
            )

    # 5. Legacy 3.23-3.25 patch traps
    patch_traps: list[dict[str, Any]] = []
    seen_patch: set[str] = set()
    for prule in LEGACY_PATCH_TRAPS_POE1:
        hit = _match_rule_fields(
            raw,
            norm,
            norm,
            zh_compact,
            en_exact=prule.en_exact,
            zh_exact=prule.zh_exact,
            en_words=prule.en_words,
            zh_phrases=prule.zh_phrases,
            regex=prule.regex,
            exclude_en_re=prule.exclude_en_re,
            exclude_zh_compounds=prule.exclude_zh_compounds,
        )
        if hit is not None and prule.rule_id not in seen_patch:
            seen_patch.add(prule.rule_id)
            patch_traps.append(
                {
                    "ruleId": prule.rule_id,
                    "patchVersion": prule.patch_version,
                    "category": "legacy_patch",
                    "trigger": hit,
                    "canonicalTerm": prule.canonical_term,
                    "warning": prule.summary,
                    "remediation": prule.remediation,
                    "equivalentCommand": prule.equivalent_command,
                }
            )

    # 6. Same-name PoE1 vs PoE2 false friends
    false_friends: list[dict[str, Any]] = []
    seen_ff: set[str] = set()
    _standalone_abbr = {
        "FF_POE1_ELDRITCH_BATTERY": re.compile(r"(?<![A-Za-z0-9_])EB(?![A-Za-z0-9_])"),
        "FF_POE1_CHAOS_INOCULATION": re.compile(r"(?<![A-Za-z0-9_])CI(?![A-Za-z0-9_])"),
        "FF_POE1_MIND_OVER_MATTER": re.compile(r"(?<![A-Za-z0-9_])MoM(?![A-Za-z0-9_])"),
    }
    for ff_rule in SAME_NAME_FALSE_FRIENDS_POE1:
        hit = _match_rule_fields(
            raw,
            norm,
            norm,
            zh_compact,
            en_exact=ff_rule.en_exact,
            zh_exact=ff_rule.zh_exact,
            en_words=ff_rule.en_words,
            zh_phrases=ff_rule.zh_phrases,
            regex=_standalone_abbr.get(ff_rule.rule_id),
            exclude_en_re=ff_rule.exclude_en_re,
            exclude_zh_compounds=ff_rule.exclude_zh_compounds,
        )
        if hit is not None and ff_rule.rule_id not in seen_ff:
            seen_ff.add(ff_rule.rule_id)
            false_friends.append(
                {
                    "ruleId": ff_rule.rule_id,
                    "category": "false_friend",
                    "trigger": hit,
                    "canonicalTerm": ff_rule.canonical_term,
                    "warning": ff_rule.summary,
                    "poe1Behavior": ff_rule.poe1_behavior,
                    "poe2Behavior": ff_rule.poe2_behavior,
                }
            )

    combined_warnings: list[dict[str, Any]] = []
    for t in poe2_traps:
        combined_warnings.append(
            {
                "severity": "BLOCK",
                "tag": "[POE1-CROSS-GAME-BLOCK]",
                "ruleId": t["ruleId"],
                "trigger": t["trigger"],
                "message": f"{t['error']} {t.get('remediation') or ''}".strip(),
            }
        )
    for p in patch_traps:
        combined_warnings.append(
            {
                "severity": "PATCH",
                "tag": "[POE1-PATCH-WARN]",
                "ruleId": p["ruleId"],
                "trigger": p["trigger"],
                "message": p["warning"],
            }
        )
    for f in false_friends:
        combined_warnings.append(
            {
                "severity": "TRAP",
                "tag": "[POE1-TRAP-WARN]",
                "ruleId": f["ruleId"],
                "trigger": f["trigger"],
                "message": f["warning"],
            }
        )

    return {
        "poe2Traps": poe2_traps,
        "patchTraps": patch_traps,
        "falseFriends": false_friends,
        "warnings": combined_warnings,
    }


# ---------------------------------------------------------------------------
# PoE2-Only Timeless / Radius Jewels & Classes / Ascendancies Reverse Guards
# ---------------------------------------------------------------------------

POE2_ONLY_TIMELESS_JEWELS_EN: tuple[str, ...] = (
    "undying hate",
    "controlled metamorphosis",
    "against the darkness",
    "prism of belief",
    "from nothing",
)
# NOTE: `英雄的悲歌` is the PoE2 CN localization of `Heroic Tragedy`; do NOT block
# English `Heroic Tragedy` or PoE1 CN `英勇悲剧` / `英勇的悲剧` (valid PoE1 jewel).
POE2_ONLY_TIMELESS_JEWELS_ZH: tuple[str, ...] = (
    "不死之恨",
    "不朽之恨",
    "受控的蜕变",
    "可控变形",
    "力抗黑暗",
    "抗衡黑暗",
    "信仰棱镜",
    "信念的棱镜",
    "无中生有",
    "无根之源",
    "英雄的悲歌",
)


def check_poe2_timeless_jewel(jewel_name: str | None, *, expected_game: str = GAME_ID) -> None:
    """Hard-block PoE2-only timeless / radius jewels in `pob timeless search`."""
    if not jewel_name or expected_game != "poe1":
        return
    raw = str(jewel_name).strip()
    if not raw:
        return

    norm = normalize_guardrail_text(raw)
    matched: str | None = None
    for phrase in POE2_ONLY_TIMELESS_JEWELS_EN:
        if norm == phrase or match_word_en(norm, phrase) is not None:
            matched = raw
            break
    if matched is None:
        compact = normalize_zh_compact(raw)
        for zh in POE2_ONLY_TIMELESS_JEWELS_ZH:
            if compact == normalize_zh_compact(zh) or match_phrase_zh(raw, zh) is not None:
                matched = zh
                break

    if matched is not None:
        raise CrossGameHallucinationError(
            game="poe1",
            detected_game="poe2",
            trigger=matched,
            error=f"Jewel `{raw}` is a Path of Exile 2 timeless/radius jewel and does not exist in PoE1 `pob timeless`.",
            remediation=(
                "Use `pob2` (`F:/poe2`) for PoE2 timeless/radius jewels, or run `pob timeless list` "
                "for supported PoE1 Historic jewels (e.g. `Lethal Pride`, `Brutal Restraint`, `Heroic Tragedy`)."
            ),
            equivalent_command="pob timeless list",
            rule_id="POE2_TIMELESS_JEWEL_IN_POB1",
        )


# PoE2-only base classes (excludes shared `Witch` and `Ranger`).
POE2_ONLY_CLASSES_EN: frozenset[str] = POE2_ONLY_CLASSES
POE2_ONLY_CLASSES_ZH: frozenset[str] = frozenset(
    {
        "战士",
        "武僧",
        "佣兵",
        "魔巫",
        "女术士",
        "女猎手",
        "德鲁伊",
    }
)

# PoE2-only ascendancies (excludes shared `Deadeye`, `Pathfinder`, and all PoE1 ascendancies).
POE2_ONLY_ASCENDANCIES_EN: frozenset[str] = frozenset(
    {
        # Warrior
        "titan",
        "warbringer",
        "smith of kitava",
        # Monk
        "invoker",
        "acolyte of chayula",
        # Mercenary
        "witchhunter",
        "gemling legionnaire",
        "tactician",
        # Sorceress
        "stormweaver",
        "chronomancer",
        "disciple of varashta",
        # Witch (PoE2-only)
        "blood mage",
        "infernalist",
        "lich",
        "abyssal lich",
        # Huntress
        "amazon",
        "ritualist",
        # Druid
        "oracle",
        "shaman",
    }
)
POE2_ONLY_ASCENDANCIES_ZH: frozenset[str] = frozenset(
    {
        "泰坦",
        "战争使者",
        "奇塔弗铁匠",
        "奇塔弗的铁匠",
        "祈求者",
        "夏乌拉侍僧",
        "夏乌拉的侍僧",
        "猎巫人",
        "宝石军团",
        "古灵使徒",
        "战术家",
        "风暴编织者",
        "时空术士",
        "瓦拉什塔信徒",
        "瓦拉什塔的信徒",
        "血法师",
        "鲜血法师",
        "狱火师",
        "巫妖",
        "深渊巫妖",
        "亚马逊",
        "仪式师",
        "先知",
        "萨满",
    }
)
_POE2_CLASS_ASC_EN = POE2_ONLY_CLASSES_EN | POE2_ONLY_ASCENDANCIES_EN
_POE2_CLASS_ASC_ZH = frozenset(
    normalize_zh_compact(x) for x in (POE2_ONLY_CLASSES_ZH | POE2_ONLY_ASCENDANCIES_ZH)
)


def check_poe2_class_or_ascendancy(
    value: str | None,
    *,
    field: str = "class",
    expected_game: str = GAME_ID,
) -> None:
    """Hard-block PoE2-only character classes and ascendancies in PoE1 `new` / `tree set-class` / `batch`."""
    if not value or expected_game != "poe1":
        return
    raw = str(value).strip()
    if not raw:
        return

    norm = normalize_guardrail_text(raw)
    zh_compact = normalize_zh_compact(raw)
    if norm in _POE2_CLASS_ASC_EN or (zh_compact and zh_compact in _POE2_CLASS_ASC_ZH):
        is_cls = norm in POE2_ONLY_CLASSES_EN or zh_compact in {
            normalize_zh_compact(x) for x in POE2_ONLY_CLASSES_ZH
        }
        kind_label = "class" if is_cls else "ascendancy"
        equiv = f'pob2 new --class "{raw}"' if field == "class" and is_cls else f'pob2 tree set-class "{raw}"'
        raise CrossGameHallucinationError(
            game="poe1",
            detected_game="poe2",
            trigger=raw,
            error=f"`{raw}` is a Path of Exile 2 {kind_label} and does not exist in PoE1.",
            remediation=(
                "Use `pob2` (`F:/poe2`) for PoE2 classes/ascendancies, or pick a PoE1 class "
                "(Marauder, Duelist, Ranger, Shadow, Witch, Templar, Scion)."
            ),
            equivalent_command=equiv,
            rule_id="POE2_CLASS_IN_POB1",
        )
