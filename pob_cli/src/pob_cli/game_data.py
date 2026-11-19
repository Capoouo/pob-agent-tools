"""Local offline PoE 1 game data parser and SQLite index (zero network, zero BridgeWorker lock).

Reads PoB Lua data files (`PoE1/Data/`) and optional Simplified Chinese dictionaries
(`Data/poe1/zh-rCN/`) into a versioned SQLite + FTS5 trigram cache under
`<runtime_dir>/cache/gamedata_<pobVersion>_<dataVersion>.sqlite`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3

# Constants from PoE1/Data/Misc.lua (GameConstants.dat) used by CalcTools.lua
SKILL_DAMAGE_BASE_EFFECTIVENESS = 3.885209
SKILL_DAMAGE_INCREMENTAL_EFFECTIVENESS = 0.360246

DEFAULT_PROJECT_ROOT = Path(os.environ.get("POB_HOME", str(Path(__file__).resolve().parents[3])))
FALLBACK_POB_ROOT = Path("F:/poe/PobTools[20261102]")
FALLBACK_RUNTIME_DIR = Path("F:/poe/.runtime")

# Process-local memoization to avoid re-parsing manifest.xml or re-opening SQLite on warm queries
_MANIFEST_VER_CACHE: dict[tuple[str, int, int], str] = {}
_ZH_META_VER_CACHE: dict[tuple[str, int, int], str] = {}
_VALID_INDEX_CACHE: set[tuple[str, str, str]] = set()

LUA_SKIP_KEYS_MOD: frozenset[str] = frozenset(
    {"tradeHashes", "statOrder", "weightMultiplierKey", "weightMultiplierVal"}
)
LUA_SKIP_KEYS_SKILL: frozenset[str] = frozenset(
    {
        "skillTypes",
        "minionSkillTypes",
        "statMap",
        "baseMods",
        "qualityMods",
        "levelMods",
        "requireSkillTypes",
        "addSkillTypes",
        "excludeSkillTypes",
        "notMinionStat",
    }
)
SKILL_KEEP_LEVELS: frozenset[int] = frozenset({1, 3, 4, 5, 6, 7, 20, 21})

MOD_FILES: tuple[tuple[str, str], ...] = (
    ("ModExplicit.lua", "Explicit"),
    ("ModCorrupted.lua", "Corrupted"),
    ("ModMaster.lua", "Bench"),
    ("ModEldritch.lua", "Eldritch"),
    ("ModTincture.lua", "Tincture"),
    ("ModFoulborn.lua", "Foulborn"),
    ("ModJewel.lua", "Jewel"),
    ("ModJewelCluster.lua", "JewelCluster"),
)

SKILL_FILES: tuple[str, ...] = (
    "act_str.lua",
    "act_dex.lua",
    "act_int.lua",
    "sup_str.lua",
    "sup_dex.lua",
    "sup_int.lua",
    "other.lua",
    "glove.lua",
    "minion.lua",
)

COLOR_NAMES: dict[int, str] = {
    1: "Red",
    2: "Green",
    3: "Blue",
    4: "White",
}

UNIQUE_META_KEYS: frozenset[str] = frozenset(
    {
        "Variant",
        "Has Alt Variant",
        "Has Alt Variant Two",
        "Has Alt Variant Three",
        "Selected Variant",
        "Selected Alt Variant",
        "Selected Alt Variant Two",
        "Selected Alt Variant Three",
        "League",
        "Source",
        "LevelReq",
        "Requires Level",
        "Sockets",
        "Item Level",
        "Radius",
        "Limited to",
        "Upgrade",
        "Talisman Tier",
        "Crafted",
        "Suffix",
    }
)

# Slot aliases -> (weightKeys, parent_zero_check_tags, ModMaster types)
SLOT_ALIASES: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "body": (("body_armour",), ("body_armour", "armour", "default"), ("Body Armour",)),
    "body_armour": (("body_armour",), ("body_armour", "armour", "default"), ("Body Armour",)),
    "chest": (("body_armour",), ("body_armour", "armour", "default"), ("Body Armour",)),
    "helmet": (("helmet",), ("helmet", "armour", "default"), ("Helmet",)),
    "helm": (("helmet",), ("helmet", "armour", "default"), ("Helmet",)),
    "gloves": (("gloves",), ("gloves", "armour", "default"), ("Gloves",)),
    "boots": (("boots",), ("boots", "armour", "default"), ("Boots",)),
    "shield": (("shield",), ("shield", "armour", "default"), ("Shield",)),
    "quiver": (("quiver",), ("quiver", "default"), ("Quiver",)),
    "amulet": (("amulet",), ("amulet", "default"), ("Amulet",)),
    "ring": (("ring",), ("ring", "default"), ("Ring",)),
    "ring_1": (("ring",), ("ring", "default"), ("Ring",)),
    "ring_2": (("ring",), ("ring", "default"), ("Ring",)),
    "ring_3": (("ring",), ("ring", "default"), ("Ring",)),
    "belt": (("belt",), ("belt", "default"), ("Belt",)),
    "flask": (("flask",), ("flask", "default"), ("Flask",)),
    "tincture": (("tincture",), ("tincture", "default"), ()),
    "jewel": (("jewel", "abyss_jewel"), ("jewel", "default"), ()),
    "abyss_jewel": (("abyss_jewel",), ("abyss_jewel", "default"), ()),
    "cluster_jewel": (
        ("expansion_jewel_large", "expansion_jewel_medium", "expansion_jewel_small"),
        ("default",),
        (),
    ),
    "bow": (("bow",), ("bow", "weapon", "ranged", "default"), ("Bow",)),
    "wand": (("wand",), ("wand", "one_hand_weapon", "onehand", "weapon", "ranged", "default"), ("Wand",)),
    "claw": (("claw",), ("claw", "one_hand_weapon", "onehand", "weapon", "default"), ("Claw",)),
    "dagger": (("dagger", "attack_dagger"), ("dagger", "one_hand_weapon", "onehand", "weapon", "default"), ("Dagger",)),
    "sceptre": (("sceptre",), ("sceptre", "mace", "one_hand_weapon", "onehand", "weapon", "default"), ("Sceptre",)),
    "staff": (("staff", "warstaff", "attack_staff"), ("staff", "two_hand_weapon", "weapon", "default"), ("Staff",)),
    "sword": (
        ("sword", "2h_sword"),
        ("sword", "weapon", "default"),
        ("One Handed Sword", "Thrusting One Handed Sword", "Two Handed Sword"),
    ),
    "one_handed_sword": (
        ("sword",),
        ("sword", "one_hand_weapon", "onehand", "weapon", "default"),
        ("One Handed Sword", "Thrusting One Handed Sword"),
    ),
    "thrusting_one_handed_sword": (
        ("sword",),
        ("sword", "one_hand_weapon", "onehand", "weapon", "default"),
        ("Thrusting One Handed Sword",),
    ),
    "two_handed_sword": (("2h_sword", "sword"), ("2h_sword", "sword", "two_hand_weapon", "weapon", "default"), ("Two Handed Sword",)),
    "axe": (("axe", "2h_axe"), ("axe", "weapon", "default"), ("One Handed Axe", "Two Handed Axe")),
    "one_handed_axe": (("axe",), ("axe", "one_hand_weapon", "onehand", "weapon", "default"), ("One Handed Axe",)),
    "two_handed_axe": (("2h_axe", "axe"), ("2h_axe", "axe", "two_hand_weapon", "weapon", "default"), ("Two Handed Axe",)),
    "mace": (("mace", "2h_mace"), ("mace", "weapon", "default"), ("One Handed Mace", "Two Handed Mace")),
    "one_handed_mace": (("mace",), ("mace", "one_hand_weapon", "onehand", "weapon", "default"), ("One Handed Mace",)),
    "two_handed_mace": (("2h_mace", "mace"), ("2h_mace", "mace", "two_hand_weapon", "weapon", "default"), ("Two Handed Mace",)),
    "weapon": (
        ("weapon", "one_hand_weapon", "two_hand_weapon", "sword", "axe", "mace", "dagger", "claw", "wand", "bow", "staff", "sceptre"),
        ("weapon", "default"),
        ("One Handed Sword", "Two Handed Sword", "One Handed Axe", "Two Handed Axe", "One Handed Mace", "Two Handed Mace", "Dagger", "Claw", "Wand", "Bow", "Staff", "Sceptre", "Thrusting One Handed Sword"),
    ),
    "weapon_1": (
        ("weapon", "one_hand_weapon", "two_hand_weapon", "sword", "axe", "mace", "dagger", "claw", "wand", "bow", "staff", "sceptre"),
        ("weapon", "default"),
        ("One Handed Sword", "Two Handed Sword", "One Handed Axe", "Two Handed Axe", "One Handed Mace", "Two Handed Mace", "Dagger", "Claw", "Wand", "Bow", "Staff", "Sceptre", "Thrusting One Handed Sword"),
    ),
    "weapon_2": (
        ("weapon", "one_hand_weapon", "shield", "quiver", "sword", "axe", "mace", "dagger", "claw", "wand", "sceptre"),
        ("weapon", "shield", "quiver", "default"),
        ("One Handed Sword", "One Handed Axe", "One Handed Mace", "Dagger", "Claw", "Wand", "Sceptre", "Thrusting One Handed Sword", "Shield", "Quiver"),
    ),
}

# UI/non-domain files to skip from general translation index
ZH_SKIP_TRANSLATION_FILES: frozenset[str] = frozenset(
    {
        "meta.json",
        "Test.json",
        "unused.json",
        "GUI.json",
        "Help.json",
        "ui.json",
        "Z-UI.json",
        "Main.json",
        "Build.json",
        "BuildListControl.json",
        "ExtBuildListControl.json",
        "ImportTab.json",
        "NotesTab.json",
        "PartyTab.json",
        "PoBArchivesProvider.json",
        "SharedItemListControl.json",
        "SharedItemSetListControl.json",
    }
)

ZH_FILE_CATEGORY_HINT: dict[str, tuple[str, int]] = {
    "gems.json": ("gem", 1),
    "Gems_data.txt.json": ("gem", 1),
    "Items_Gems.txt.json": ("gem", 2),
    "Z-Skills.json": ("gem", 1),
    "uniques.json": ("unique", 1),
    "Uniques.txt.json": ("unique", 1),
    "Z-S28-Foulborn.json": ("unique", 2),
    "items.json": ("base", 1),
    "Items_Accessories.txt.json": ("base", 1),
    "Items_Armour.txt.json": ("base", 1),
    "Items_Flasks.txt.json": ("base", 1),
    "Items_Jewels.txt.json": ("base", 1),
    "Items_Weapons.txt.json": ("base", 1),
    "Items_Oils.json": ("base", 2),
    "Z-Item.json": ("base", 2),
    "tree_dn.json": ("passive", 1),
    "Z-Keystone_Proverb.json": ("passive", 2),
    "Z-Ascendancy_Proverb.json": ("passive", 2),
    "TattooPassives.json": ("passive", 2),
    "passiveTree.json": ("passive", 3),
    "tree_sd.json": ("stat", 3),
    "tree_rt.json": ("stat", 4),
    "Query_Mod.json": ("mod", 2),
    "ModMap.json": ("mod", 3),
    "stats.json": ("stat", 4),
    "statDescriptions.json": ("stat", 4),
}

NUM_PLACEHOLDER_RE = re.compile(
    r"[+-]?\([+-]?\d+(?:\.\d+)?-[+-]?\d+(?:\.\d+)?\)|[+-]?\d+(?:\.\d+)?|\{\d+\}|#"
)
STAT_VALUE_RE = re.compile(
    r"[+-]?\([+-]?\d+(?:\.\d+)?-[+-]?\d+(?:\.\d+)?\)|[+-]?\d+(?:\.\d+)?"
)

# Single-pass Lua lexer regex (whitespace, comments, and flat metadata tables skipped in C)
LUA_TOKEN_RE = re.compile(
    r"--\[(?P<BEQ>=*)\[[\s\S]*?\](?P=BEQ)\]|--[^\n]*|"
    r"(?:(?:statOrder|skillTypes|minionSkillTypes|notMinionStat|requireSkillTypes|addSkillTypes|excludeSkillTypes)\s*=\s*\{[^{}]*\})|"
    r"(\[(?P<LEQ>=*)\[[\s\S]*?\](?P=LEQ)\])|"
    r'("(?:\\.|[^"\\])*")|'
    r"('(?:\\.|[^'\\])*')|"
    r"(0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|"
    r"([A-Za-z_][A-Za-z0-9_]*)|"
    r"(\.\.\.|\.\.|==|~=|<=|>=|[\{\}\[\]\(\)=,;:\.\+\-\*/%\^#<>])"
)

LUA_ESCAPE_RE = re.compile(r'\\(["\'\\abfnrtv0-9])')
LUA_ESCAPE_MAP: dict[str, str] = {
    '"': '"',
    "'": "'",
    "\\": "\\",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}

MOD_TAG_PREFIX_RE = re.compile(r"^(?:\{[^}]*\})+")
VARIANT_TAG_RE = re.compile(r"\{variant:([^}]+)\}")


def resolve_runtime_dir(runtime_dir: Path | None = None) -> Path:
    """Resolve runtime directory following explicit arg -> POB_RUNTIME_DIR -> repo/.runtime."""
    if runtime_dir is not None:
        return Path(runtime_dir)
    env_rt = os.environ.get("POB_RUNTIME_DIR", "").strip()
    if env_rt:
        return Path(env_rt)
    candidate = DEFAULT_PROJECT_ROOT / ".runtime"
    if candidate.exists() or (DEFAULT_PROJECT_ROOT / "PobTools[20261102]").exists():
        return candidate
    if FALLBACK_RUNTIME_DIR.parent.exists():
        return FALLBACK_RUNTIME_DIR
    return candidate


def resolve_pob_dir(pob_dir: Path | None = None, runtime_dir: Path | None = None) -> Path:
    """Resolve PoB engine root: explicit arg -> POB_ROOT -> <runtime_dir>/engine -> PobTools[20261102]."""
    if pob_dir is not None:
        return Path(pob_dir)
    env_pob = os.environ.get("POB_ROOT", "").strip()
    if env_pob:
        return Path(env_pob)
    rt = resolve_runtime_dir(runtime_dir)
    managed_engine = rt / "engine"
    if (managed_engine / "PoE1" / "Data").is_dir() or (managed_engine / "Data").is_dir():
        return managed_engine
    local_pob = DEFAULT_PROJECT_ROOT / "PobTools[20261102]"
    if local_pob.is_dir():
        return local_pob
    return FALLBACK_POB_ROOT


def _resolve_poe1_data_dirs(pob_dir: Path) -> tuple[Path, Path | None]:
    """Return `(poe1_dir, zh_dir_or_none)` for a given PoB root directory."""
    if (pob_dir / "PoE1" / "Data").is_dir():
        poe1_dir = pob_dir / "PoE1"
    elif (pob_dir / "Data").is_dir():
        poe1_dir = pob_dir
    else:
        raise FileNotFoundError(f"PoE1/Data directory not found under {pob_dir}")

    zh_candidate = pob_dir / "Data" / "poe1" / "zh-rCN"
    zh_dir = zh_candidate if zh_candidate.is_dir() else None
    return poe1_dir, zh_dir


def _scan_dir_entries(hasher: Any, directory: Path, suffix: str) -> None:
    if not directory.is_dir():
        return
    with os.scandir(directory) as it:
        entries = [e for e in it if e.name.endswith(suffix) and e.is_file()]
    entries.sort(key=lambda e: e.name)
    for e in entries:
        st = e.stat()
        hasher.update(f"{e.name}:{st.st_size}:{st.st_mtime_ns};".encode("utf-8"))


def detect_versions(poe1_dir: Path, zh_dir: Path | None) -> tuple[str, str]:
    """Detect `(pobVersion, dataVersion)` so engine or dictionary updates trigger index rebuild."""
    pob_version = "unknown"
    manifest_path = poe1_dir / "manifest.xml"
    hasher = hashlib.blake2b(digest_size=4)

    if manifest_path.is_file():
        st = manifest_path.stat()
        hasher.update(f"manifest.xml:{st.st_size}:{st.st_mtime_ns};".encode("utf-8"))
        mkey = (str(manifest_path), st.st_size, st.st_mtime_ns)
        cached_ver = _MANIFEST_VER_CACHE.get(mkey)
        if cached_ver is not None:
            pob_version = cached_ver
        else:
            try:
                root = ET.parse(manifest_path).getroot()
                ver_el = root.find("Version")
                if ver_el is not None and ver_el.get("number"):
                    pob_version = ver_el.get("number", "unknown").strip()
                    _MANIFEST_VER_CACHE[mkey] = pob_version
            except ET.ParseError:
                pass
    if pob_version == "unknown":
        gv_path = poe1_dir / "GameVersions.lua"
        if gv_path.is_file():
            gv_txt = gv_path.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"treeVersionList\s*=\s*\{([^}]+)\}", gv_txt)
            if m:
                vers = re.findall(r'"([^"]+)"', m.group(1))
                if vers:
                    pob_version = vers[-1]

    data_dir = poe1_dir / "Data"
    _scan_dir_entries(hasher, data_dir, ".lua")
    _scan_dir_entries(hasher, data_dir, ".jsonc")
    _scan_dir_entries(hasher, data_dir / "Skills", ".lua")
    _scan_dir_entries(hasher, data_dir / "Bases", ".lua")
    _scan_dir_entries(hasher, data_dir / "Uniques", ".lua")
    race_lua = data_dir / "Uniques" / "Special" / "race.lua"
    if race_lua.is_file():
        st = race_lua.stat()
        hasher.update(f"race.lua:{st.st_size}:{st.st_mtime_ns};".encode("utf-8"))

    zh_prefix = "nozh"
    if zh_dir is not None and zh_dir.is_dir():
        zh_ver = "1.0"
        meta_path = zh_dir / "meta.json"
        if meta_path.is_file():
            st = meta_path.stat()
            zkey = (str(meta_path), st.st_size, st.st_mtime_ns)
            cached_zver = _ZH_META_VER_CACHE.get(zkey)
            if cached_zver is not None:
                zh_ver = cached_zver
            else:
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    zh_ver = str(meta.get("version") or "1.0")
                    _ZH_META_VER_CACHE[zkey] = zh_ver
                except Exception:
                    pass
        zh_prefix = f"zh-{zh_ver}"
        _scan_dir_entries(hasher, zh_dir, ".json")

    safe_pob = re.sub(r"[^A-Za-z0-9._-]", "_", pob_version)
    safe_data = re.sub(r"[^A-Za-z0-9._-]", "_", f"{zh_prefix}-{hasher.hexdigest()}")
    return pob_version, safe_data


def _unescape_lua_quoted(raw: str) -> str:
    body = raw[1:-1]
    if "\\" not in body:
        return body

    def _repl(match: re.Match[str]) -> str:
        ch = match.group(1)
        if ch in LUA_ESCAPE_MAP:
            return LUA_ESCAPE_MAP[ch]
        if ch.isdigit():
            return chr(int(ch))
        return ch

    return LUA_ESCAPE_RE.sub(_repl, body)


def _strip_long_bracket(raw: str) -> str:
    eq_end = raw.index("[", 1)
    content = raw[eq_end + 1 : -(eq_end + 1)]
    if content.startswith("\r\n"):
        content = content[2:]
    elif content.startswith("\n"):
        content = content[1:]
    return content


def _tokenize_lua(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    append = tokens.append
    for m in LUA_TOKEN_RE.finditer(text):
        idx = m.lastindex
        if idx is None:
            continue
        val = m.group(idx)
        if idx == 8:
            append(("OP", val))
        elif idx == 7:
            if val in ("true", "false", "nil"):
                append(("BOOL_NIL", val))
            else:
                append(("IDENT", val))
        elif idx == 4 or idx == 5:
            append(("STR", _unescape_lua_quoted(val)))
        elif idx == 6:
            append(("NUM", val))
        elif idx == 2:
            append(("STR", _strip_long_bracket(val)))
    return tokens


def _parse_num(s: str) -> int | float:
    if s.startswith(("0x", "0X")):
        return int(s, 16)
    if "." in s or "e" in s or "E" in s:
        return float(s)
    return int(s)


class _LuaParser:
    """Recursive-descent parser over Lua data tables and top-level table assignments."""

    def __init__(
        self,
        tokens: list[tuple[str, str]],
        *,
        skip_keys: frozenset[str] = frozenset(),
        keep_levels: frozenset[int] | None = None,
    ) -> None:
        self.tokens = tokens
        self.n = len(tokens)
        self.pos = 0
        self.skip_keys = skip_keys
        self.keep_levels = keep_levels

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        idx = self.pos + offset
        if 0 <= idx < self.n:
            return self.tokens[idx]
        return None

    def advance(self) -> tuple[str, str]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _skip_balanced(self, open_tok: str, close_tok: str) -> None:
        depth = 0
        while self.pos < self.n:
            _, val = self.advance()
            if val == open_tok:
                depth += 1
            elif val == close_tok:
                depth -= 1
                if depth <= 0:
                    return

    def _skip_function_block(self) -> None:
        # Called positioned on 'function' keyword; in Lua, for/while always pair with 'do'.
        self.advance()
        depth = 1
        while self.pos < self.n:
            kind, val = self.advance()
            if kind == "IDENT":
                if val in ("function", "do", "if", "repeat"):
                    depth += 1
                elif val in ("end", "until"):
                    depth -= 1
                    if depth <= 0:
                        return

    def _parse_expr(self) -> Any:
        tok = self.peek()
        if tok is None:
            return None
        kind, val = tok

        if kind == "IDENT" and val == "function":
            self._skip_function_block()
            return None

        # Unary minus / plus / not / #
        if kind == "OP" and val in ("-", "+", "#"):
            op = self.advance()[1]
            nxt = self.peek()
            if op in ("-", "+") and nxt is not None and nxt[0] == "NUM":
                num = _parse_num(self.advance()[1])
                v = -num if op == "-" else num
                return self._maybe_consume_binary_tail(v)
            sub = self._parse_expr()
            if op == "-" and isinstance(sub, (int, float)):
                return -sub
            return sub

        if kind == "IDENT" and val == "not":
            self.advance()
            self._parse_expr()
            return None

        if kind == "OP" and val == "{":
            return self.parse_table()

        if kind == "STR":
            self.advance()
            return self._maybe_consume_binary_tail(val)

        if kind == "NUM":
            self.advance()
            return self._maybe_consume_binary_tail(_parse_num(val))

        if kind == "BOOL_NIL":
            self.advance()
            if val == "true":
                return True
            if val == "false":
                return False
            return None

        if kind == "OP" and val == "(":
            self._skip_balanced("(", ")")
            return None

        if kind == "IDENT":
            parts = [self.advance()[1]]
            while self.pos < self.n:
                nxt = self.peek()
                if nxt is not None and nxt[0] == "OP" and nxt[1] in (".", ":"):
                    sep = self.advance()[1]
                    nxt2 = self.peek()
                    if nxt2 is not None and nxt2[0] == "IDENT":
                        parts.append(sep + self.advance()[1])
                    continue
                if nxt is not None and nxt[0] == "OP" and nxt[1] == "(":
                    self._skip_balanced("(", ")")
                    continue
                if nxt is not None and nxt[0] == "OP" and nxt[1] == "[":
                    self._skip_balanced("[", "]")
                    continue
                break
            expr_str = "".join(parts)
            return self._maybe_consume_binary_tail(expr_str)

        # Fallback: consume unexpected token
        self.advance()
        return None

    def _maybe_consume_binary_tail(self, left: Any) -> Any:
        while self.pos < self.n:
            nxt = self.peek()
            if nxt is None or nxt[0] != "OP" or nxt[1] in (",", "}", "]", ";"):
                break
            if nxt[1] in ("+", "-", "*", "/", "%", "^", "..", "==", "~=", "<=", ">=", "<", ">"):
                op = self.advance()[1]
                right = self._parse_expr()
                if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                    if op == "+":
                        left = left + right
                    elif op == "-":
                        left = left - right
                    elif op == "*":
                        left = left * right
                    elif op == "/" and right != 0:
                        left = left / right
                elif op == ".." and isinstance(left, str) and isinstance(right, str):
                    left = left + right
                continue
            break
        return left

    def parse_table(self, *, filter_int_keys: frozenset[int] | None = None) -> dict[Any, Any] | list[Any]:
        tokens = self.tokens
        n = self.n
        self.pos += 1  # Consume '{'
        result: dict[Any, Any] = {}
        seq_idx = 1
        has_explicit_keys = False
        skip_keys = self.skip_keys
        keep_levels = self.keep_levels

        while self.pos < n:
            tok = tokens[self.pos]
            tk0, tv0 = tok
            if tk0 == "OP":
                if tv0 == "}":
                    self.pos += 1
                    break
                if tv0 == "," or tv0 == ";":
                    self.pos += 1
                    continue
                if tv0 == "[":
                    has_explicit_keys = True
                    self.pos += 1
                    key = self._parse_expr()
                    if self.pos < n and tokens[self.pos] == ("OP", "]"):
                        self.pos += 1
                    if self.pos < n and tokens[self.pos] == ("OP", "="):
                        self.pos += 1
                    if filter_int_keys is not None and isinstance(key, int) and key not in filter_int_keys:
                        if self.pos < n and tokens[self.pos] == ("OP", "{"):
                            self._skip_balanced("{", "}")
                        else:
                            self._parse_expr()
                        continue
                    val = self._parse_expr()
                    if key is not None:
                        result[key] = val
                    continue

            if tk0 == "IDENT" and self.pos + 1 < n and tokens[self.pos + 1] == ("OP", "="):
                has_explicit_keys = True
                key = tv0
                self.pos += 2  # ident, '='
                if key in skip_keys and self.pos < n and tokens[self.pos] == ("OP", "{"):
                    self._skip_balanced("{", "}")
                    continue
                if key == "levels" and keep_levels is not None and self.pos < n and tokens[self.pos] == ("OP", "{"):
                    result[key] = self.parse_table(filter_int_keys=keep_levels)
                    continue
                result[key] = self._parse_expr()
            else:
                result[seq_idx] = self._parse_expr()
                seq_idx += 1

        if not has_explicit_keys and result:
            return [result[i] for i in range(1, seq_idx)]
        return result

    def parse_return_table(self) -> dict[Any, Any] | list[Any]:
        while self.pos < self.n:
            kind, val = self.advance()
            if kind == "IDENT" and val == "return":
                nxt = self.peek()
                if nxt is not None and nxt == ("OP", "{"):
                    return self.parse_table()
        return {}

    def parse_var_assignments(self, var_name: str) -> dict[str, dict[Any, Any]]:
        out: dict[str, dict[Any, Any]] = {}
        while self.pos < self.n:
            tok = self.peek()
            if tok is None:
                break
            if tok == ("IDENT", var_name) and self.peek(1) == ("OP", "["):
                self.advance()  # var_name
                self.advance()  # '['
                key = self._parse_expr()
                if self.peek() == ("OP", "]"):
                    self.advance()
                if self.peek() == ("OP", "="):
                    self.advance()
                val = self._parse_expr()
                if isinstance(key, str) and isinstance(val, dict):
                    out[key] = val
            else:
                self.advance()
        return out


MOD_TRADEHASH_STRIP_RE = re.compile(r",\s*tradeHashes\s*=\s*\{[^\n]*\}")
SKILL_HIGH_LEVEL_STRIP_RE = re.compile(
    r"^\t\t\[(?:[89]|1\d|2[2-9]|[34]\d)\]\s*=\s*\{[^\n]*\},\r?\n", re.MULTILINE
)


def _parse_lua_return(
    path: Path,
    *,
    skip_keys: frozenset[str] = frozenset(),
    prestrip_re: re.Pattern[str] | None = None,
    prestrip_repl: str = "",
) -> Any:
    txt = path.read_text(encoding="utf-8", errors="ignore")
    if prestrip_re is not None:
        txt = prestrip_re.sub(prestrip_repl, txt)
    return _LuaParser(_tokenize_lua(txt), skip_keys=skip_keys).parse_return_table()


def _parse_lua_assignments(
    path: Path,
    var_name: str,
    *,
    skip_keys: frozenset[str] = frozenset(),
    keep_levels: frozenset[int] | None = None,
    prestrip_re: re.Pattern[str] | None = None,
) -> dict[str, dict[Any, Any]]:
    txt = path.read_text(encoding="utf-8", errors="ignore")
    if prestrip_re is not None:
        txt = prestrip_re.sub("", txt)
    return _LuaParser(_tokenize_lua(txt), skip_keys=skip_keys, keep_levels=keep_levels).parse_var_assignments(var_name)


def _as_seq(tbl: Any) -> list[Any]:
    if isinstance(tbl, list):
        return tbl
    if isinstance(tbl, dict):
        return [tbl[k] for k in sorted((k for k in tbl if isinstance(k, int)))]
    return []


def _norm_template(text: str) -> str:
    return NUM_PLACEHOLDER_RE.sub("{#}", text).lower().strip()


def _build_template_variants(text: str) -> tuple[str, ...]:
    n1 = _norm_template(text)
    n2 = n1.replace("+{#}", "{#}")
    if n2 != n1:
        return (n1, n2)
    return (n1,)


class _ZhDictionary:
    """Loaded Simplified Chinese lookup tables and mod stat template translator."""

    def __init__(self) -> None:
        self.available = False
        self.gems: dict[str, str] = {}
        self.uniques: dict[str, str] = {}
        self.bases: dict[str, str] = {}
        self.passives: dict[str, str] = {}
        self.stats_exact: dict[str, str] = {}
        self.stat_templates: dict[str, str] = {}
        self.all_entries: list[tuple[str, str, str, int]] = []

    @classmethod
    def load(cls, zh_dir: Path | None) -> _ZhDictionary:
        obj = cls()
        if zh_dir is None or not zh_dir.is_dir():
            return obj

        obj.available = True
        load_order: list[str] = []
        meta_path = zh_dir / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                load_order = [str(f) for f in meta.get("load_order", [])]
            except Exception:
                load_order = []

        seen_files: set[str] = set()
        ordered_files: list[Path] = []
        for fn in load_order:
            p = zh_dir / fn
            if p.is_file() and fn not in seen_files:
                ordered_files.append(p)
                seen_files.add(fn)
        for p in sorted(zh_dir.glob("*.json")):
            if p.name not in seen_files:
                ordered_files.append(p)
                seen_files.add(p.name)

        # Skip files fully superseded by merged dictionaries or UI/lore-only files
        superseded: set[str] = set()
        if (zh_dir / "stats.json").is_file():
            superseded.update({"statDescriptions.json", "A-1a.json", "A-1b.json"})
        if (zh_dir / "tree_dn.json").is_file() and (zh_dir / "tree_sd.json").is_file():
            superseded.update({"passives.json", "tree_rt.json"})
        if (zh_dir / "gems.json").is_file():
            superseded.update({"Gems_data.txt.json", "Gems_data.json", "Items_Gems.txt.json", "Items_Gems.json"})
        if (zh_dir / "uniques.json").is_file():
            superseded.add("Uniques.txt.json")
        if (zh_dir / "items.json").is_file():
            superseded.update(
                {
                    "Items_Accessories.txt.json",
                    "Items_Armour.txt.json",
                    "Items_Flasks.txt.json",
                    "Items_Jewels.txt.json",
                    "Items_Weapons.txt.json",
                    "Items_Minions.json",
                }
            )
        ui_or_lore = {
            "ConfigOptions.json",
            "ConfigTab.json",
            "CalcBreakdown.json",
            "CalcDefence.json",
            "CalcOffence.json",
            "CalcSections.json",
            "CalcSetup.json",
            "CalcsTab.json",
            "CompareBuySimilar.json",
            "ComparePowerReportListControl.json",
            "CompareTab.json",
            "BuildDisplayStats.json",
            "Data.json",
            "Flask_tag.json",
            "Gems_tag.json",
            "GemSelectControl.json",
            "ItemDBControl.json",
            "ItemListControl.json",
            "ItemSetListControl.json",
            "ItemsTab.json",
            "MinionListControl.json",
            "Minions_tag.json",
            "PassiveTreeView.json",
            "PowerReportListControl.json",
            "SkillsTab.json",
            "TimelessJewelListControl.json",
            "TradeQuery.json",
            "TradeQueryGenerator.json",
            "Tree.json",
            "TreeTab.json",
            "monsters.json",
            "ui.json",
            "Unsorted.json",
            "A-Skills1.json",
            "A-Skills2.json",
            "A-Skillsfrom1.json",
            "A-Skillsfrom2.json",
            "Z-Uniques_Proverb.json",
            "Z-Exceptional_Gems_Lore.json",
            "Z-Replica_Dragonfang's_Flight.json",
            "Z-Skills_Gems_Overview.json",
            "Z-Support_Gems_Overview.json",
        }

        dedup_pairs: dict[tuple[str, str, str], tuple[str, int]] = {}

        for p in ordered_files:
            fn = p.name
            if fn in ZH_SKIP_TRANSLATION_FILES or fn in superseded or fn in ui_or_lore:
                continue
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            entries = payload.get("entries") if isinstance(payload, dict) else None
            if not isinstance(entries, dict):
                continue

            cat_hint, rank_hint = ZH_FILE_CATEGORY_HINT.get(fn, ("other", 3))
            is_season_patch = fn.startswith("Z-S") and fn.endswith(".json")
            is_gem_file = fn in ("gems.json", "Gems_data.txt.json", "Items_Gems.txt.json", "Z-Skills.json") or is_season_patch
            is_unique_file = fn in ("uniques.json", "Uniques.txt.json", "Z-S28-Foulborn.json", "Z-Flask.json", "Z-Jewel.json") or is_season_patch
            is_base_file = fn in ("items.json", "Items_Accessories.txt.json", "Items_Armour.txt.json", "Items_Flasks.txt.json", "Items_Jewels.txt.json", "Items_Weapons.txt.json", "Z-Item.json", "Z-Flask.json", "Z-Jewel.json", "Z-Addition.json", "Z.json") or is_season_patch
            is_passive_file = fn in ("tree_dn.json", "Z-Keystone_Proverb.json", "Z-Ascendancy_Proverb.json", "TattooPassives.json", "Z-Phrecia.json") or is_season_patch
            is_stat_file = fn in ("statDescriptions.json", "stats.json", "Query_Mod.json", "ModMap.json", "A-1a.json", "A-1b.json", "Z-Item.json", "tree_sd.json", "Z-Addition.json", "Z.json") or is_season_patch

            for raw_en, raw_zh in entries.items():
                if not isinstance(raw_en, str) or not isinstance(raw_zh, str):
                    continue
                en = raw_en.strip()
                zh = raw_zh.strip()
                if not en or not zh:
                    continue

                if is_gem_file:
                    # Prefer gems.json / Gems_data.txt.json over Items_Gems.txt.json (which adds （辅）)
                    if fn != "Items_Gems.txt.json" or en not in obj.gems:
                        obj.gems[en] = zh
                if is_unique_file:
                    obj.uniques[en] = zh
                if is_base_file:
                    obj.bases[en] = zh
                if is_passive_file:
                    obj.passives[en] = zh
                if is_stat_file:
                    obj.stats_exact[en.lower()] = zh
                    if "{" in en or "#" in en:
                        for norm_k in _build_template_variants(en):
                            obj.stat_templates[norm_k] = zh

                key = (en.lower(), zh, cat_hint)
                prev = dedup_pairs.get(key)
                if prev is None or rank_hint < prev[1]:
                    dedup_pairs[key] = (en, rank_hint)

        obj.all_entries = [(en, k[1], k[2], rank) for k, (en, rank) in dedup_pairs.items()]
        return obj

    def translate_gem(self, name: str) -> str | None:
        if not self.available:
            return None
        return (
            self.gems.get(name)
            or self.gems.get(f"{name} Support")
            or self.gems.get(name.removesuffix(" Support"))
        )

    def translate_unique(self, name: str) -> str | None:
        if not self.available:
            return None
        return self.uniques.get(name)

    def translate_base(self, name: str) -> str | None:
        if not self.available:
            return None
        return self.bases.get(name)

    def translate_stat_line(self, line: str) -> str | None:
        if not self.available or not line:
            return None
        clean = MOD_TAG_PREFIX_RE.sub("", line).strip()
        if not clean:
            return None
        exact = self.stats_exact.get(clean.lower())
        if exact is not None:
            return exact

        for norm_k in _build_template_variants(clean):
            tpl = self.stat_templates.get(norm_k)
            if tpl is not None:
                return _apply_stat_values(clean, tpl)
        return None


def _apply_stat_values(en_line: str, zh_tpl: str) -> str:
    vals = STAT_VALUE_RE.findall(en_line)
    if not vals:
        return zh_tpl
    out = zh_tpl
    for idx, val in enumerate(vals):
        token = f"{{{idx}}}"
        if token in out:
            out = out.replace(token, val)
    if "#" in out:
        for val in vals:
            out = out.replace("#", val, 1)
    return out


def _round_pob(val: float) -> int:
    """PoB's round(x) uses math.floor(x + 0.5) for positive numbers."""
    return int(val + 0.5)


def _compute_skill_level_stats(skill: dict[str, Any], lvl_num: int) -> dict[str, Any] | None:
    levels = skill.get("levels")
    if not isinstance(levels, dict) or lvl_num not in levels:
        return None
    lvl = levels[lvl_num]
    if not isinstance(lvl, dict):
        return None

    stat_names = _as_seq(skill.get("stats"))
    interp_list = _as_seq(lvl.get("statInterpolation"))
    base_eff = float(skill.get("baseEffectiveness") or 1.0)
    inc_eff = float(skill.get("incrementalEffectiveness") or 0.0)
    actor_level = int(lvl.get("levelRequirement") or 1)

    avail_eff: float | None = None
    resolved_stats: dict[str, Any] = {}
    for idx, stat_name in enumerate(stat_names):
        if not isinstance(stat_name, str):
            continue
        raw_val = lvl.get(idx + 1, 1)
        interp = interp_list[idx] if idx < len(interp_list) else 1
        if interp == 3 and isinstance(raw_val, (int, float)):
            if avail_eff is None:
                avail_eff = (
                    (SKILL_DAMAGE_BASE_EFFECTIVENESS + SKILL_DAMAGE_INCREMENTAL_EFFECTIVENESS * (actor_level - 1))
                    * base_eff
                    * ((1.0 + inc_eff) ** (actor_level - 1))
                )
            resolved_stats[stat_name] = _round_pob(avail_eff * float(raw_val))
        elif interp == 2 and isinstance(raw_val, (int, float)):
            resolved_stats[stat_name] = _round_pob(float(raw_val))
        else:
            resolved_stats[stat_name] = raw_val

    cost_raw = lvl.get("cost")
    cost = {str(k): v for k, v in cost_raw.items()} if isinstance(cost_raw, dict) else {}

    entry: dict[str, Any] = {
        "levelRequirement": lvl.get("levelRequirement"),
        "critChance": lvl.get("critChance"),
        "damageEffectiveness": lvl.get("damageEffectiveness"),
        "manaMultiplier": lvl.get("manaMultiplier"),
        "cooldown": lvl.get("cooldown"),
        "cost": cost,
        "stats": resolved_stats,
    }
    return {k: v for k, v in entry.items() if v is not None and v != {}}


def _parse_gems_and_skills(poe1_dir: Path, zh: _ZhDictionary) -> list[dict[str, Any]]:
    data_dir = poe1_dir / "Data"
    skills: dict[str, dict[Any, Any]] = {}
    for fn in SKILL_FILES:
        p = data_dir / "Skills" / fn
        if p.is_file():
            skills.update(
                _parse_lua_assignments(
                    p,
                    "skills",
                    skip_keys=LUA_SKIP_KEYS_SKILL,
                    keep_levels=SKILL_KEEP_LEVELS,
                    prestrip_re=SKILL_HIGH_LEVEL_STRIP_RE,
                )
            )

    gems_raw = _parse_lua_return(data_dir / "Gems.lua")
    if not isinstance(gems_raw, dict):
        return []

    # Replicate PoB's synthesized Vaal + Transfigured hybrid gems (Modules/Data.lua:1098-1118)
    synthesized: dict[str, dict[Any, Any]] = {}
    for gem_id, gem in gems_raw.items():
        if not isinstance(gem, dict) or not gem.get("vaalGem"):
            continue
        sec_id = gem.get("secondaryGrantedEffectId")
        if not isinstance(sec_id, str):
            continue
        for alt in ("AltX", "AltY"):
            alt_skill = skills.get(sec_id + alt)
            if isinstance(alt_skill, dict):
                alt_base_name = alt_skill.get("baseTypeName") or alt_skill.get("name") or ""
                cloned = dict(gem)
                cloned["name"] = f"Vaal {alt_base_name}"
                cloned["variantId"] = f"{gem.get('variantId', '')}{alt}"
                cloned["secondaryGrantedEffectId"] = sec_id + alt
                synthesized[f"{gem_id}{alt}"] = cloned
    gems_raw.update(synthesized)

    # Index relations for variant discovery (gameId, secondaryGrantedEffectId, plusVersionOf, Imbued)
    by_game_id: dict[str, list[dict[str, Any]]] = {}
    by_granted_id: dict[str, list[dict[str, Any]]] = {}
    by_name_lower: dict[str, list[dict[str, Any]]] = {}
    records: list[dict[str, Any]] = []

    for gem_id, gem in gems_raw.items():
        if not isinstance(gem_id, str) or not isinstance(gem, dict):
            continue
        name = str(gem.get("name") or "").strip()
        if not name:
            continue
        ge_id = str(gem.get("grantedEffectId") or "")
        sec_id = gem.get("secondaryGrantedEffectId")
        skill = skills.get(ge_id, {})
        sec_skill = skills.get(sec_id, {}) if isinstance(sec_id, str) else {}
        tags_raw = gem.get("tags") if isinstance(gem.get("tags"), dict) else {}
        tags_list = sorted(str(k) for k, v in tags_raw.items() if v)
        is_support = bool(skill.get("support") or ("support" in tags_raw))
        is_vaal = bool(gem.get("vaalGem") or ("vaal" in tags_raw))
        variant_id = str(gem.get("variantId") or "")
        is_transfigured = bool(re.search(r"Alt[XYZ]$", variant_id))
        is_imbued = bool("imbued" in tags_raw or name.startswith("Imbued "))
        color = COLOR_NAMES.get(int(skill.get("color") or 0), "")
        if not color:
            if "strength" in tags_raw:
                color = "Red"
            elif "dexterity" in tags_raw:
                color = "Green"
            elif "intelligence" in tags_raw:
                color = "Blue"

        nat_max = int(gem.get("naturalMaxLevel") or 20)
        target_levels = sorted({1, nat_max, nat_max + 1, 20, 21})
        levels_out: dict[str, Any] = {}
        for lvl_num in target_levels:
            lvl_stats = _compute_skill_level_stats(skill, lvl_num)
            if lvl_stats is not None:
                levels_out[str(lvl_num)] = lvl_stats

        quality_stats: list[dict[str, Any]] = []
        for qs in _as_seq(skill.get("qualityStats")):
            if isinstance(qs, list) and len(qs) >= 2 and isinstance(qs[1], (int, float)):
                quality_stats.append(
                    {
                        "stat": qs[0],
                        "per_quality": qs[1],
                        "at_20_quality": round(float(qs[1]) * 20, 4),
                    }
                )

        constant_stats: list[dict[str, Any]] = []
        for cs in _as_seq(skill.get("constantStats")):
            if isinstance(cs, list) and len(cs) >= 2:
                constant_stats.append({"stat": cs[0], "value": cs[1]})

        lvl20 = levels_out.get("20") or levels_out.get(str(nat_max)) or levels_out.get("1") or {}
        zh_name = zh.translate_gem(name)
        display_name = f"{name} Support" if (is_support and not name.endswith("Support") and ge_id != "SupportBarrage") else name

        rec: dict[str, Any] = {
            "id": gem_id,
            "name": name,
            "display_name": display_name,
            "zh_name": zh_name,
            "game_id": str(gem.get("gameId") or ""),
            "variant_id": variant_id,
            "granted_effect_id": ge_id,
            "secondary_granted_effect_id": sec_id if isinstance(sec_id, str) else None,
            "plus_version_of": skill.get("plusVersionOf") if isinstance(skill.get("plusVersionOf"), str) else None,
            "is_support": is_support,
            "is_vaal": is_vaal,
            "is_transfigured": is_transfigured,
            "is_imbued": is_imbued,
            "color": color,
            "tag_string": str(gem.get("tagString") or ""),
            "tags": tags_list,
            "req_str": int(gem.get("reqStr") or 0),
            "req_dex": int(gem.get("reqDex") or 0),
            "req_int": int(gem.get("reqInt") or 0),
            "natural_max_level": nat_max,
            "description": str(skill.get("description") or ""),
            "cast_time": skill.get("castTime"),
            "crit_chance": lvl20.get("critChance"),
            "damage_effectiveness": lvl20.get("damageEffectiveness"),
            "mana_multiplier": lvl20.get("manaMultiplier"),
            "mana_cost": {lvl_k: lvl_v.get("cost") for lvl_k, lvl_v in levels_out.items() if lvl_v.get("cost")},
            "quality_stats": quality_stats,
            "constant_stats": constant_stats,
            "levels": levels_out,
            "secondary_skill_name": sec_skill.get("name") if isinstance(sec_skill, dict) else None,
            "variants": [],
        }
        records.append(rec)
        if rec["game_id"]:
            by_game_id.setdefault(rec["game_id"], []).append(rec)
        if ge_id:
            by_granted_id.setdefault(ge_id, []).append(rec)
        by_name_lower.setdefault(name.lower(), []).append(rec)

    by_sec_id: dict[str, list[dict[str, Any]]] = {}
    by_plus_ver: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        if rec["secondary_granted_effect_id"]:
            by_sec_id.setdefault(rec["secondary_granted_effect_id"], []).append(rec)
        if rec["plus_version_of"]:
            by_plus_ver.setdefault(rec["plus_version_of"], []).append(rec)

    # Link variants cleanly
    for rec in records:
        related: dict[str, dict[str, Any]] = {}

        def _add_rel(other: dict[str, Any], relation: str) -> None:
            if other["id"] == rec["id"]:
                return
            if other["id"] not in related:
                related[other["id"]] = {
                    "id": other["id"],
                    "name": other["name"],
                    "zh_name": other["zh_name"],
                    "relation": relation,
                }

        for other in by_game_id.get(rec["game_id"], []):
            rel = "imbued" if other["is_imbued"] else ("transfigured" if other["is_transfigured"] else "base")
            _add_rel(other, rel)

        if rec["secondary_granted_effect_id"]:
            for other in by_granted_id.get(rec["secondary_granted_effect_id"], []):
                _add_rel(other, "base")
        for other in by_sec_id.get(rec["granted_effect_id"], []):
            _add_rel(other, "vaal")

        if rec["plus_version_of"]:
            for other in by_granted_id.get(rec["plus_version_of"], []):
                _add_rel(other, "base")
        for other in by_plus_ver.get(rec["granted_effect_id"], []):
            _add_rel(other, "awakened")

        for prefix, rel in (("Imbued ", "imbued"), ("Vaal ", "vaal"), ("Awakened ", "awakened")):
            if rec["name"].startswith(prefix):
                base_n = rec["name"][len(prefix) :].lower()
                for other in by_name_lower.get(base_n, []):
                    _add_rel(other, "base")
            else:
                for other in by_name_lower.get((prefix + rec["name"]).lower(), []):
                    _add_rel(other, rel)

        rec["variants"] = list(related.values())

    return records


def _parse_bases(poe1_dir: Path, zh: _ZhDictionary) -> list[dict[str, Any]]:
    bases_dir = poe1_dir / "Data" / "Bases"
    records: list[dict[str, Any]] = []
    if not bases_dir.is_dir():
        return records

    for p in sorted(bases_dir.glob("*.lua")):
        raw_bases = _parse_lua_assignments(p, "itemBases")
        for name, b in raw_bases.items():
            if not isinstance(b, dict):
                continue
            tags_raw = b.get("tags") if isinstance(b.get("tags"), dict) else {}
            tags = sorted(str(k) for k, v in tags_raw.items() if v)
            req_raw = b.get("req") if isinstance(b.get("req"), dict) else {}
            implicit_raw = b.get("implicit")
            implicits = [s.strip() for s in str(implicit_raw).splitlines() if s.strip()] if implicit_raw else []
            implicits_zh = [zh.translate_stat_line(line) for line in implicits] if zh.available else []

            props: dict[str, Any] = {}
            for prop_key in ("weapon", "armour", "flask", "tincture"):
                if isinstance(b.get(prop_key), dict):
                    props[prop_key] = dict(b[prop_key])

            rec = {
                "name": name,
                "zh_name": zh.translate_base(name),
                "type": str(b.get("type") or ""),
                "sub_type": str(b.get("subType") or "") or None,
                "socket_limit": b.get("socketLimit"),
                "hidden": bool(b.get("hidden")),
                "tags": tags,
                "req": {
                    "level": int(req_raw.get("level") or 0),
                    "str": int(req_raw.get("str") or 0),
                    "dex": int(req_raw.get("dex") or 0),
                    "int": int(req_raw.get("int") or 0),
                },
                "implicits": implicits,
                "implicits_zh": implicits_zh,
                "enchant": b.get("enchant"),
                "properties": props,
            }
            records.append(rec)
    return records


def _parse_uniques(
    poe1_dir: Path,
    bases_by_name: dict[str, dict[str, Any]],
    foulborn_mods_by_id: dict[str, dict[str, Any]],
    zh: _ZhDictionary,
) -> list[dict[str, Any]]:
    data_dir = poe1_dir / "Data"
    uniques_dir = data_dir / "Uniques"
    if not uniques_dir.is_dir():
        return []

    # Load Foulborn mappings
    foulborn_lua_map: dict[str, list[str]] = {}
    fb_lua_path = data_dir / "ModFoulbornMap.lua"
    if fb_lua_path.is_file():
        parsed = _parse_lua_return(fb_lua_path)
        if isinstance(parsed, dict):
            for u_name, lines in parsed.items():
                foulborn_lua_map[str(u_name)] = [str(x) for x in _as_seq(lines) if x]

    foulborn_json_map: dict[str, dict[str, str]] = {}
    fb_json_path = data_dir / "ModFoulbornMap.jsonc"
    if fb_json_path.is_file():
        txt = fb_json_path.read_text(encoding="utf-8", errors="ignore")
        clean = re.sub(r"^\s*//.*$", "", txt, flags=re.M)
        try:
            raw_map = json.loads(clean)
            if isinstance(raw_map, dict):
                for u_name, mod_pair in raw_map.items():
                    if isinstance(mod_pair, dict):
                        foulborn_json_map[str(u_name)] = {str(k): str(v) for k, v in mod_pair.items()}
        except json.JSONDecodeError:
            pass

    files = sorted(uniques_dir.glob("*.lua"))
    race_path = uniques_dir / "Special" / "race.lua"
    if race_path.is_file():
        files.append(race_path)

    records: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for p in files:
        fallback_slot = p.stem
        blocks = _parse_lua_return(p)
        for block in _as_seq(blocks):
            if not isinstance(block, str):
                continue
            lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
            if len(lines) < 2:
                continue
            name = lines[0]
            if name in seen_names:
                continue
            seen_names.add(name)
            base_type = lines[1]

            variants: list[str] = []
            league: str | None = None
            source: str | None = None
            req_level: int | None = None
            implicits_count = 0
            idx = 2

            while idx < len(lines):
                ln = lines[idx]
                if ln.startswith("Implicits:"):
                    try:
                        implicits_count = int(ln.split(":", 1)[1].strip())
                    except ValueError:
                        implicits_count = 0
                    idx += 1
                    break
                if ln.startswith("Variant:"):
                    variants.append(ln.split(":", 1)[1].strip())
                    idx += 1
                    continue
                if ln.startswith("League:"):
                    league = ln.split(":", 1)[1].strip()
                    idx += 1
                    continue
                if ln.startswith("Source:"):
                    source = ln.split(":", 1)[1].strip()
                    idx += 1
                    continue
                if ln.startswith("Requires Level ") or ln.startswith("LevelReq:"):
                    m_lvl = re.search(r"(\d+)", ln)
                    if m_lvl:
                        req_level = int(m_lvl.group(1))
                    idx += 1
                    continue
                if ":" in ln and not ln.startswith("{") and ln.split(":", 1)[0].strip() in UNIQUE_META_KEYS:
                    idx += 1
                    continue
                # Some legacy uniques omit "Implicits:" line directly
                break

            implicits = lines[idx : idx + implicits_count] if implicits_count > 0 else []
            explicits = lines[idx + implicits_count :]

            implicits_zh = [zh.translate_stat_line(ln) for ln in implicits] if zh.available else []
            explicits_zh = [zh.translate_stat_line(ln) for ln in explicits] if zh.available else []

            base_info = bases_by_name.get(base_type)
            item_class = base_info["type"] if base_info else fallback_slot
            base_tags = base_info["tags"] if base_info else [fallback_slot]

            foulborn_mods: list[dict[str, Any]] = []
            seen_fb_lines: set[str] = set()
            for orig_id, fb_id in foulborn_json_map.get(name, {}).items():
                fb_mod = foulborn_mods_by_id.get(fb_id)
                if fb_mod:
                    for line in fb_mod["stats"]:
                        seen_fb_lines.add(line)
                    foulborn_mods.append(
                        {
                            "mod_id": fb_id,
                            "replaces_mod_id": orig_id,
                            "stats": fb_mod["stats"],
                            "stats_zh": fb_mod["stats_zh"],
                        }
                    )
            for fb_line in foulborn_lua_map.get(name, []):
                if fb_line not in seen_fb_lines:
                    foulborn_mods.append(
                        {
                            "mod_id": None,
                            "replaces_mod_id": None,
                            "stats": [fb_line],
                            "stats_zh": [zh.translate_stat_line(fb_line)] if zh.available else [],
                        }
                    )

            zh_name = zh.translate_unique(name)
            foulborn_en = f"Foulborn {name}"
            zh_foulborn = zh.translate_unique(foulborn_en)

            records.append(
                {
                    "name": name,
                    "zh_name": zh_name,
                    "foulborn_name": foulborn_en if (foulborn_mods or zh_foulborn) else None,
                    "zh_foulborn_name": zh_foulborn,
                    "base_type": base_type,
                    "base_type_zh": zh.translate_base(base_type),
                    "item_class": item_class,
                    "tags": base_tags,
                    "req_level": req_level,
                    "league": league,
                    "source": source,
                    "variants": variants,
                    "implicits": implicits,
                    "implicits_zh": implicits_zh,
                    "explicits": explicits,
                    "explicits_zh": explicits_zh,
                    "foulborn_mods": foulborn_mods,
                }
            )
    return records


def _parse_mods(poe1_dir: Path, zh: _ZhDictionary) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    data_dir = poe1_dir / "Data"
    records: list[dict[str, Any]] = []
    foulborn_by_id: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()

    for filename, source_name in MOD_FILES:
        path = data_dir / filename
        if not path.is_file():
            continue
        raw_table = _parse_lua_return(
            path,
            skip_keys=LUA_SKIP_KEYS_MOD,
            prestrip_re=MOD_TRADEHASH_STRIP_RE,
            prestrip_repl=" }",
        )

        items: list[tuple[str, dict[Any, Any]]] = []
        if isinstance(raw_table, list):
            for idx, entry in enumerate(raw_table, 1):
                if isinstance(entry, dict):
                    grp = str(entry.get("group") or "Craft")
                    gen = str(entry.get("type") or "Craft")
                    lvl = int(entry.get("level") or 0)
                    items.append((f"Master_{grp}_{gen}_{lvl}_{idx}", entry))
        elif isinstance(raw_table, dict):
            for mod_id, entry in raw_table.items():
                if isinstance(entry, dict):
                    items.append((str(mod_id), entry))

        for mod_id, entry in items:
            if mod_id in seen_ids:
                continue
            stat_lines = [str(s) for s in _as_seq(entry) if isinstance(s, str) and s.strip()]
            if not stat_lines:
                continue
            seen_ids.add(mod_id)
            stats_zh = [zh.translate_stat_line(ln) for ln in stat_lines] if zh.available else []

            raw_type = str(entry.get("type") or "")
            if source_name == "Bench":
                gen_type = f"Bench {raw_type}".strip()
            elif source_name == "Foulborn" and not raw_type:
                gen_type = "Foulborn"
            else:
                gen_type = raw_type or source_name

            wk_list = _as_seq(entry.get("weightKey"))
            wv_list = _as_seq(entry.get("weightVal"))
            positive_weights: dict[str, int] = {}
            zero_weights: list[str] = []
            for k, v in zip(wk_list, wv_list):
                if not isinstance(k, str) or not isinstance(v, (int, float)):
                    continue
                iv = int(v)
                if iv > 0:
                    positive_weights[k] = iv
                else:
                    zero_weights.append(k)

            types_raw = entry.get("types")
            applicable_types = sorted(str(k) for k, v in types_raw.items() if v) if isinstance(types_raw, dict) else []
            mod_tags = [str(t) for t in _as_seq(entry.get("modTags")) if isinstance(t, str)]

            bench_info: dict[str, Any] | None = None
            if source_name == "Bench":
                bench_info = {
                    "source": "Crafting Bench",
                    "types": applicable_types,
                    "cost": entry.get("cost"),
                }

            rec = {
                "id": mod_id,
                "source": source_name,
                "generation_type": gen_type,
                "affix": str(entry.get("affix") or ""),
                "group": str(entry.get("group") or ""),
                "level": int(entry.get("level") or 0),
                "stats": stat_lines,
                "stats_zh": stats_zh,
                "mod_tags": mod_tags,
                "weights": positive_weights,
                "zero_weights": zero_weights,
                "applicable_types": applicable_types,
                "bench_info": bench_info,
            }
            records.append(rec)
            if source_name == "Foulborn":
                foulborn_by_id[mod_id] = rec

    return records, foulborn_by_id


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE gems (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            name_lower TEXT NOT NULL,
            display_name_lower TEXT NOT NULL,
            zh_name TEXT,
            zh_lower TEXT,
            game_id TEXT,
            variant_id TEXT,
            is_support INTEGER NOT NULL,
            is_vaal INTEGER NOT NULL,
            is_transfigured INTEGER NOT NULL,
            is_imbued INTEGER NOT NULL,
            color TEXT,
            tag_string TEXT,
            search_text_lower TEXT NOT NULL,
            data_json TEXT NOT NULL
        );
        CREATE INDEX idx_gems_name_lower ON gems(name_lower);
        CREATE INDEX idx_gems_display_lower ON gems(display_name_lower);
        CREATE INDEX idx_gems_zh_lower ON gems(zh_lower);

        CREATE TABLE uniques (
            name TEXT PRIMARY KEY,
            name_lower TEXT NOT NULL,
            zh_name TEXT,
            zh_lower TEXT,
            foulborn_lower TEXT,
            zh_foulborn_lower TEXT,
            base_type TEXT NOT NULL,
            item_class TEXT NOT NULL,
            slots_lower TEXT NOT NULL,
            has_foulborn INTEGER NOT NULL,
            search_text_lower TEXT NOT NULL,
            data_json TEXT NOT NULL
        );
        CREATE INDEX idx_uniques_name_lower ON uniques(name_lower);
        CREATE INDEX idx_uniques_zh_lower ON uniques(zh_lower);

        CREATE TABLE bases (
            name TEXT PRIMARY KEY,
            name_lower TEXT NOT NULL,
            zh_name TEXT,
            zh_lower TEXT,
            item_type TEXT NOT NULL,
            sub_type TEXT,
            req_level INTEGER NOT NULL,
            hidden INTEGER NOT NULL,
            slots_lower TEXT NOT NULL,
            search_text_lower TEXT NOT NULL,
            data_json TEXT NOT NULL
        );
        CREATE INDEX idx_bases_name_lower ON bases(name_lower);
        CREATE INDEX idx_bases_zh_lower ON bases(zh_lower);

        CREATE TABLE mods (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            gen_type TEXT NOT NULL,
            mod_group TEXT NOT NULL,
            mod_group_lower TEXT NOT NULL,
            affix TEXT NOT NULL,
            affix_lower TEXT NOT NULL,
            level INTEGER NOT NULL,
            stats_en_lower TEXT NOT NULL,
            stats_zh_lower TEXT NOT NULL,
            weights_json TEXT NOT NULL,
            zero_weights_json TEXT NOT NULL,
            types_json TEXT NOT NULL,
            search_en_lower TEXT NOT NULL,
            search_zh_lower TEXT NOT NULL,
            search_text_lower TEXT NOT NULL,
            data_json TEXT NOT NULL
        );
        CREATE INDEX idx_mods_level ON mods(level);
        CREATE INDEX idx_mods_group ON mods(mod_group_lower);

        CREATE TABLE translations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            en TEXT NOT NULL,
            en_lower TEXT NOT NULL,
            zh TEXT NOT NULL,
            zh_lower TEXT NOT NULL,
            category TEXT NOT NULL,
            cat_rank INTEGER NOT NULL
        );

        CREATE VIRTUAL TABLE gems_fts USING fts5(search_text_lower, content='gems', tokenize='trigram');
        CREATE VIRTUAL TABLE uniques_fts USING fts5(search_text_lower, content='uniques', tokenize='trigram');
        CREATE VIRTUAL TABLE bases_fts USING fts5(search_text_lower, content='bases', tokenize='trigram');
        CREATE VIRTUAL TABLE mods_fts USING fts5(search_text_lower, content='mods', tokenize='trigram');
        CREATE VIRTUAL TABLE translations_zh_fts USING fts5(
            zh_lower, content='translations', content_rowid='id', tokenize='trigram'
        );
        CREATE VIRTUAL TABLE translations_en_fts USING fts5(
            en_lower, content='translations', content_rowid='id', tokenize='unicode61'
        );
        """
    )


def _is_index_valid(db_path: Path, pob_version: str, data_version: str) -> bool:
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            return (
                rows.get("schema_version") == str(SCHEMA_VERSION)
                and rows.get("pob_version") == pob_version
                and rows.get("data_version") == data_version
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def ensure_gamedata_index(
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    force_rebuild: bool = False,
) -> Path:
    """Build (if missing/stale) and return path to the versioned SQLite game data index."""
    rt_dir = resolve_runtime_dir(runtime_dir)
    resolved_pob = resolve_pob_dir(pob_dir, rt_dir)
    poe1_dir, zh_dir = _resolve_poe1_data_dirs(resolved_pob)
    pob_version, data_version = detect_versions(poe1_dir, zh_dir)

    cache_dir = rt_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    db_path = cache_dir / f"gamedata_{pob_version}_{data_version}.sqlite"

    cache_key = (str(db_path), pob_version, data_version)
    if not force_rebuild:
        if cache_key in _VALID_INDEX_CACHE and db_path.is_file():
            return db_path
        if _is_index_valid(db_path, pob_version, data_version):
            _VALID_INDEX_CACHE.add(cache_key)
            return db_path
    else:
        _VALID_INDEX_CACHE.discard(cache_key)

    zh = _ZhDictionary.load(zh_dir)
    gems = _parse_gems_and_skills(poe1_dir, zh)
    bases = _parse_bases(poe1_dir, zh)
    bases_by_name = {b["name"]: b for b in bases}
    mods, foulborn_by_id = _parse_mods(poe1_dir, zh)
    uniques = _parse_uniques(poe1_dir, bases_by_name, foulborn_by_id, zh)

    tmp_path = cache_dir / f".gamedata_{os.getpid()}_{time.time_ns()}.sqlite.tmp"
    if tmp_path.exists():
        tmp_path.unlink()

    conn = sqlite3.connect(str(tmp_path))
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -65536")
        _create_schema(conn)

        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("pob_version", pob_version),
                ("data_version", data_version),
                ("zh_available", "1" if zh.available else "0"),
                ("built_at", str(time.time())),
            ],
        )

        # Gems
        gem_rows = []
        for g in gems:
            s_txt = " ".join(
                p for p in (g["name"], g["display_name"], g["zh_name"] or "", g["tag_string"], g["granted_effect_id"]) if p
            ).lower()
            gem_rows.append(
                (
                    g["id"],
                    g["name"],
                    g["name"].lower(),
                    g["display_name"].lower(),
                    g["zh_name"],
                    g["zh_name"].lower() if g["zh_name"] else None,
                    g["game_id"],
                    g["variant_id"],
                    int(g["is_support"]),
                    int(g["is_vaal"]),
                    int(g["is_transfigured"]),
                    int(g["is_imbued"]),
                    g["color"],
                    g["tag_string"],
                    s_txt,
                    json.dumps(g, ensure_ascii=False, separators=(",", ":")),
                )
            )
        conn.executemany(
            "INSERT INTO gems VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            gem_rows,
        )
        conn.execute("INSERT INTO gems_fts(gems_fts) VALUES('rebuild')")

        # Bases
        base_rows = []
        for b in bases:
            slots_l = " ".join([b["type"].lower(), (b["sub_type"] or "").lower(), *(t.lower() for t in b["tags"])])
            s_txt = " ".join(
                p
                for p in (
                    b["name"],
                    b["zh_name"] or "",
                    b["type"],
                    b["sub_type"] or "",
                    " ".join(b["implicits"]),
                    " ".join(x for x in b["implicits_zh"] if x),
                )
                if p
            ).lower()
            base_rows.append(
                (
                    b["name"],
                    b["name"].lower(),
                    b["zh_name"],
                    b["zh_name"].lower() if b["zh_name"] else None,
                    b["type"],
                    b["sub_type"],
                    b["req"]["level"],
                    int(b["hidden"]),
                    slots_l,
                    s_txt,
                    json.dumps(b, ensure_ascii=False, separators=(",", ":")),
                )
            )
        conn.executemany("INSERT INTO bases VALUES (?,?,?,?,?,?,?,?,?,?,?)", base_rows)
        conn.execute("INSERT INTO bases_fts(bases_fts) VALUES('rebuild')")

        # Uniques
        unique_rows = []
        for u in uniques:
            slots_l = " ".join([u["item_class"].lower(), *(t.lower() for t in u["tags"])])
            fb_lines = " ".join(
                " ".join(fm["stats"]) + " " + " ".join(x for x in fm["stats_zh"] if x) for fm in u["foulborn_mods"]
            )
            s_txt = " ".join(
                p
                for p in (
                    u["name"],
                    u["zh_name"] or "",
                    u["foulborn_name"] or "",
                    u["zh_foulborn_name"] or "",
                    u["base_type"],
                    u["base_type_zh"] or "",
                    " ".join(u["implicits"]),
                    " ".join(u["explicits"]),
                    " ".join(x for x in u["implicits_zh"] if x),
                    " ".join(x for x in u["explicits_zh"] if x),
                    fb_lines,
                )
                if p
            ).lower()
            unique_rows.append(
                (
                    u["name"],
                    u["name"].lower(),
                    u["zh_name"],
                    u["zh_name"].lower() if u["zh_name"] else None,
                    u["foulborn_name"].lower() if u["foulborn_name"] else None,
                    u["zh_foulborn_name"].lower() if u["zh_foulborn_name"] else None,
                    u["base_type"],
                    u["item_class"],
                    slots_l,
                    int(bool(u["foulborn_mods"] or u["zh_foulborn_name"])),
                    s_txt,
                    json.dumps(u, ensure_ascii=False, separators=(",", ":")),
                )
            )
        conn.executemany("INSERT INTO uniques VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", unique_rows)
        conn.execute("INSERT INTO uniques_fts(uniques_fts) VALUES('rebuild')")

        # Mods
        mod_rows = []
        for m in mods:
            en_stats = "\n".join(m["stats"]).lower()
            zh_stats = "\n".join(x for x in m["stats_zh"] if x).lower()
            search_en = " ".join([m["id"], m["affix"], m["group"], en_stats]).lower()
            search_zh = zh_stats
            fts_txt = f"{search_en} {search_zh}".strip()
            mod_rows.append(
                (
                    m["id"],
                    m["source"],
                    m["generation_type"],
                    m["group"],
                    m["group"].lower(),
                    m["affix"],
                    m["affix"].lower(),
                    m["level"],
                    en_stats,
                    zh_stats,
                    json.dumps(m["weights"], separators=(",", ":")),
                    json.dumps(m["zero_weights"], separators=(",", ":")),
                    json.dumps(m["applicable_types"], separators=(",", ":")),
                    search_en,
                    search_zh,
                    fts_txt,
                    json.dumps(m, ensure_ascii=False, separators=(",", ":")),
                )
            )
        conn.executemany("INSERT INTO mods VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", mod_rows)
        conn.execute("INSERT INTO mods_fts(mods_fts) VALUES('rebuild')")

        # Translations (merge ZH dictionaries + parsed entities, deduplicating identical (en_lower, zh))
        tr_best: dict[tuple[str, str], tuple[str, str, str, str, str, int]] = {}

        def _add_tr(en_s: str, zh_s: str, cat_s: str, rank_i: int) -> None:
            k = (en_s.lower(), zh_s)
            prev = tr_best.get(k)
            if prev is None or rank_i < prev[5]:
                tr_best[k] = (en_s, k[0], zh_s, zh_s.lower(), cat_s, rank_i)

        for en, zh_str, cat, rank in zh.all_entries:
            _add_tr(en, zh_str, cat, rank)
        for g in gems:
            if g["zh_name"]:
                _add_tr(g["name"], g["zh_name"], "gem", 1)
        for u in uniques:
            if u["zh_name"]:
                _add_tr(u["name"], u["zh_name"], "unique", 1)
        for b in bases:
            if b["zh_name"]:
                _add_tr(b["name"], b["zh_name"], "base", 1)

        conn.executemany(
            "INSERT INTO translations (en, en_lower, zh, zh_lower, category, cat_rank) VALUES (?,?,?,?,?,?)",
            list(tr_best.values()),
        )
        conn.execute("CREATE INDEX idx_tr_en_lower ON translations(en_lower)")
        conn.execute("CREATE INDEX idx_tr_zh_lower ON translations(zh_lower)")
        conn.execute("INSERT INTO translations_zh_fts(translations_zh_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO translations_en_fts(translations_en_fts) VALUES('rebuild')")

        conn.commit()
    finally:
        conn.close()

    os.replace(tmp_path, db_path)
    _VALID_INDEX_CACHE.add(cache_key)
    return db_path


def _fts_phrase(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def _build_fts_match(query_lower: str) -> tuple[str | None, list[str]]:
    tokens = [t for t in query_lower.split() if t]
    long_tokens = [t for t in tokens if len(t) >= 3]
    if not long_tokens:
        return None, tokens
    return " AND ".join(_fts_phrase(t) for t in long_tokens), tokens


def _normalize_slot_filter(slot: str) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    norm = re.sub(r"[\s\-]+", "_", slot.strip().lower())
    if norm in SLOT_ALIASES:
        wk, parent_tags, master_types = SLOT_ALIASES[norm]
        return norm, wk, parent_tags, master_types
    return norm, (norm,), (norm, "default"), (slot.strip(),)


def _mod_matches_slot(
    source: str,
    weights_json: str,
    zero_weights_json: str,
    types_json: str,
    slot_spec: tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]],
) -> bool:
    norm, wk_targets, parent_tags, master_types = slot_spec
    if source == "Bench":
        types_list = json.loads(types_json)
        types_lower = {t.lower() for t in types_list}
        for mt in master_types:
            if mt.lower() in types_lower:
                return True
        return norm in {re.sub(r"[\s\-]+", "_", t.lower()) for t in types_list}

    weights: dict[str, int] = json.loads(weights_json)
    for k in wk_targets:
        if weights.get(k, 0) > 0:
            return True
    # Also support exact/prefix influence weightKey like "body_armour_elder" when slot="body_armour"
    for k, v in weights.items():
        if v > 0 and (k == norm or k.startswith(norm + "_")):
            return True

    # PoB default-weight inheritance: rolls if default > 0 and no parent/slot tag is explicitly 0
    if weights.get("default", 0) > 0 and source in ("Explicit", "Corrupted"):
        zeros = set(json.loads(zero_weights_json))
        if not any(pt in zeros for pt in parent_tags):
            return True
    return False


def _query_gems(conn: sqlite3.Connection, q_lower: str, limit: int) -> list[dict[str, Any]]:
    if not q_lower:
        rows = conn.execute("SELECT data_json FROM gems ORDER BY name LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    fts_expr, tokens = _build_fts_match(q_lower)
    if fts_expr:
        rows = conn.execute(
            """
            SELECT g.name_lower, g.display_name_lower, g.zh_lower, g.is_vaal, g.is_transfigured, g.data_json
            FROM gems_fts f
            JOIN gems g ON g.rowid = f.rowid
            WHERE gems_fts MATCH ?
            LIMIT 250
            """,
            (fts_expr,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT name_lower, display_name_lower, zh_lower, is_vaal, is_transfigured, data_json
            FROM gems
            WHERE instr(search_text_lower, ?) > 0
            LIMIT 250
            """,
            (q_lower,),
        ).fetchall()

    scored: list[tuple[tuple[int, int, int, str], str]] = []
    for name_l, disp_l, zh_l, is_vaal, is_trans, data_json in rows:
        zh_l = zh_l or ""
        if tokens and not all(
            t in name_l or t in disp_l or t in zh_l or t in data_json.lower() for t in tokens
        ):
            continue
        if q_lower == name_l or q_lower == disp_l or (zh_l and q_lower == zh_l):
            tier = 0
        elif name_l.startswith(q_lower) or disp_l.startswith(q_lower) or (zh_l and zh_l.startswith(q_lower)):
            tier = 1
        elif q_lower in name_l or q_lower in disp_l or (zh_l and q_lower in zh_l):
            tier = 2
        else:
            tier = 3
        scored.append(((tier, is_vaal + is_trans, len(name_l), name_l), data_json))

    scored.sort(key=lambda item: item[0])
    return [json.loads(item[1]) for item in scored[:limit]]


def _query_uniques(
    conn: sqlite3.Connection, q_lower: str, slot: str | None, ilvl: int | None, limit: int
) -> list[dict[str, Any]]:
    slot_spec = _normalize_slot_filter(slot) if slot else None
    if not q_lower:
        rows = conn.execute(
            "SELECT name_lower, zh_lower, foulborn_lower, zh_foulborn_lower, slots_lower, data_json FROM uniques LIMIT 300"
        ).fetchall()
    else:
        fts_expr, tokens = _build_fts_match(q_lower)
        if fts_expr:
            rows = conn.execute(
                """
                SELECT u.name_lower, u.zh_lower, u.foulborn_lower, u.zh_foulborn_lower, u.slots_lower, u.data_json
                FROM uniques_fts f
                JOIN uniques u ON u.rowid = f.rowid
                WHERE uniques_fts MATCH ?
                LIMIT 300
                """,
                (fts_expr,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT name_lower, zh_lower, foulborn_lower, zh_foulborn_lower, slots_lower, data_json
                FROM uniques
                WHERE instr(search_text_lower, ?) > 0
                LIMIT 300
                """,
                (q_lower,),
            ).fetchall()

    scored: list[tuple[tuple[int, int, str], dict[str, Any] | str]] = []
    for name_l, zh_l, fb_l, zh_fb_l, slots_l, data_json in rows:
        zh_l = zh_l or ""
        fb_l = fb_l or ""
        zh_fb_l = zh_fb_l or ""
        if slot_spec:
            norm, wk_targets, _, master_types = slot_spec
            slot_tokens = set(slots_l.split())
            if not (
                norm in slot_tokens
                or any(k in slot_tokens for k in wk_targets)
                or any(mt.lower() in slots_l for mt in master_types)
            ):
                continue

        payload: dict[str, Any] | str = data_json
        if ilvl is not None:
            obj = json.loads(data_json)
            if obj.get("req_level") is not None and int(obj["req_level"]) > ilvl:
                continue
            payload = obj

        if q_lower:
            if q_lower in (name_l, zh_l, fb_l, zh_fb_l):
                tier = 0
            elif any(s.startswith(q_lower) for s in (name_l, zh_l, fb_l, zh_fb_l) if s):
                tier = 1
            elif any(q_lower in s for s in (name_l, zh_l, fb_l, zh_fb_l) if s):
                tier = 2
            else:
                tier = 3
        else:
            tier = 0
        scored.append(((tier, len(name_l), name_l), payload))

    scored.sort(key=lambda item: item[0])
    return [item[1] if isinstance(item[1], dict) else json.loads(item[1]) for item in scored[:limit]]


def _query_bases(
    conn: sqlite3.Connection, q_lower: str, slot: str | None, ilvl: int | None, limit: int
) -> list[dict[str, Any]]:
    slot_spec = _normalize_slot_filter(slot) if slot else None
    where_ilvl = "AND b.req_level <= ?" if ilvl is not None else ""
    params: list[Any] = []

    if not q_lower:
        sql = f"SELECT b.name_lower, b.zh_lower, b.hidden, b.slots_lower, b.data_json FROM bases b WHERE 1=1 {where_ilvl} LIMIT 300"
        if ilvl is not None:
            params.append(ilvl)
        rows = conn.execute(sql, params).fetchall()
    else:
        fts_expr, _ = _build_fts_match(q_lower)
        if fts_expr:
            sql = f"""
            SELECT b.name_lower, b.zh_lower, b.hidden, b.slots_lower, b.data_json
            FROM bases_fts f
            JOIN bases b ON b.rowid = f.rowid
            WHERE bases_fts MATCH ? {where_ilvl}
            LIMIT 300
            """
            params = [fts_expr] + ([ilvl] if ilvl is not None else [])
            rows = conn.execute(sql, params).fetchall()
        else:
            sql = f"""
            SELECT b.name_lower, b.zh_lower, b.hidden, b.slots_lower, b.data_json
            FROM bases b
            WHERE instr(b.search_text_lower, ?) > 0 {where_ilvl}
            LIMIT 300
            """
            params = [q_lower] + ([ilvl] if ilvl is not None else [])
            rows = conn.execute(sql, params).fetchall()

    scored: list[tuple[tuple[int, int, int, str], str]] = []
    for name_l, zh_l, hidden, slots_l, data_json in rows:
        zh_l = zh_l or ""
        if slot_spec:
            norm, wk_targets, _, master_types = slot_spec
            slot_tokens = set(slots_l.split())
            if not (
                norm in slot_tokens
                or any(k in slot_tokens for k in wk_targets)
                or any(mt.lower() in slots_l for mt in master_types)
            ):
                continue

        if not q_lower:
            tier = 0
        elif q_lower == name_l or (zh_l and q_lower == zh_l):
            tier = 0
        elif name_l.startswith(q_lower) or (zh_l and zh_l.startswith(q_lower)):
            tier = 1
        elif q_lower in name_l or (zh_l and q_lower in zh_l):
            tier = 2
        else:
            tier = 3
        scored.append(((tier, hidden, len(name_l), name_l), data_json))

    scored.sort(key=lambda item: item[0])
    return [json.loads(item[1]) for item in scored[:limit]]


def _query_mods(
    conn: sqlite3.Connection, q_lower: str, slot: str | None, ilvl: int | None, limit: int
) -> list[dict[str, Any]]:
    slot_spec = _normalize_slot_filter(slot) if slot else None
    where_ilvl = "AND m.level <= ?" if ilvl is not None else ""

    if not q_lower:
        sql = f"""
        SELECT m.id, m.source, m.level, m.affix_lower, m.mod_group_lower,
               m.stats_en_lower, m.stats_zh_lower, m.weights_json, m.zero_weights_json,
               m.types_json, m.data_json
        FROM mods m
        WHERE 1=1 {where_ilvl}
        LIMIT 500
        """
        params: list[Any] = [ilvl] if ilvl is not None else []
        rows = conn.execute(sql, params).fetchall()
        tokens: list[str] = []
    else:
        fts_expr, tokens = _build_fts_match(q_lower)
        if fts_expr:
            sql = f"""
            SELECT m.id, m.source, m.level, m.affix_lower, m.mod_group_lower,
                   m.stats_en_lower, m.stats_zh_lower, m.weights_json, m.zero_weights_json,
                   m.types_json, m.data_json
            FROM mods_fts f
            JOIN mods m ON m.rowid = f.rowid
            WHERE mods_fts MATCH ? {where_ilvl}
            LIMIT 800
            """
            params = [fts_expr] + ([ilvl] if ilvl is not None else [])
            rows = conn.execute(sql, params).fetchall()
        else:
            sql = f"""
            SELECT m.id, m.source, m.level, m.affix_lower, m.mod_group_lower,
                   m.stats_en_lower, m.stats_zh_lower, m.weights_json, m.zero_weights_json,
                   m.types_json, m.data_json
            FROM mods m
            WHERE (instr(m.search_en_lower, ?) > 0 OR instr(m.search_zh_lower, ?) > 0) {where_ilvl}
            LIMIT 800
            """
            params = [q_lower, q_lower] + ([ilvl] if ilvl is not None else [])
            rows = conn.execute(sql, params).fetchall()

    scored: list[tuple[tuple[int, int, int, str], str]] = []
    for (
        mod_id,
        source,
        level,
        affix_l,
        grp_l,
        en_l,
        zh_l,
        weights_j,
        zero_j,
        types_j,
        data_json,
    ) in rows:
        if slot_spec and not _mod_matches_slot(source, weights_j, zero_j, types_j, slot_spec):
            continue

        if q_lower:
            combined = f"{mod_id.lower()} {affix_l} {grp_l} {en_l} {zh_l}"
            if tokens and not all(t in combined for t in tokens):
                continue
            if q_lower == mod_id.lower() or q_lower == grp_l or q_lower == affix_l or q_lower in en_l or q_lower in zh_l:
                tier = 0 if (q_lower in en_l or q_lower in zh_l or q_lower == mod_id.lower()) else 1
            else:
                tier = 2
        else:
            tier = 0

        # Prioritize mods with positive weights/bench applicability and higher ilvl tiers
        has_pos = 0 if (weights_j != "{}" or types_j != "[]") else 1
        scored.append(((tier, has_pos, -int(level), mod_id), data_json))

    scored.sort(key=lambda item: item[0])
    return [json.loads(item[1]) for item in scored[:limit]]


def _query_translate(conn: sqlite3.Connection, q_lower: str, limit: int, zh_available: bool) -> list[dict[str, Any]]:
    if not q_lower:
        return []

    if not zh_available:
        # Graceful fallback when Data/poe1/zh-rCN is absent
        results: list[dict[str, Any]] = []
        for tbl, cat in (("gems", "gem"), ("uniques", "unique"), ("bases", "base")):
            for (name,) in conn.execute(
                f"SELECT name FROM {tbl} WHERE instr(name_lower, ?) > 0 ORDER BY length(name) LIMIT ?",
                (q_lower, limit),
            ).fetchall():
                match_type = "exact" if name.lower() == q_lower else ("prefix" if name.lower().startswith(q_lower) else "substring")
                results.append({"en": name, "zh": None, "category": cat, "match_type": match_type})
        results.sort(key=lambda r: (0 if r["match_type"] == "exact" else (1 if r["match_type"] == "prefix" else 2), len(r["en"])))
        return results[:limit]

    seen_ids: set[int] = set()
    candidates: list[tuple[int, str, str, str, str, str, int]] = []

    # 1. Exact & prefix via B-tree indexes (fast)
    exact_rows = conn.execute(
        """
        SELECT id, en, en_lower, zh, zh_lower, category, cat_rank
        FROM translations
        WHERE en_lower = ? OR zh_lower = ?
        LIMIT 100
        """,
        (q_lower, q_lower),
    ).fetchall()
    for row in exact_rows:
        if row[0] not in seen_ids:
            seen_ids.add(row[0])
            candidates.append(row)

    prefix_rows = conn.execute(
        """
        SELECT id, en, en_lower, zh, zh_lower, category, cat_rank
        FROM translations
        WHERE (en_lower >= ? AND en_lower < ?) OR (zh_lower >= ? AND zh_lower < ?)
        LIMIT 150
        """,
        (q_lower, q_lower + "\uffff", q_lower, q_lower + "\uffff"),
    ).fetchall()
    for row in prefix_rows:
        if row[0] not in seen_ids:
            seen_ids.add(row[0])
            candidates.append(row)

    # 2. Substring via FTS5 (zh trigram / en unicode61, with instr fallback)
    if len(candidates) < limit:
        has_non_ascii = any(ord(ch) > 127 for ch in q_lower)
        fts_expr, tokens = _build_fts_match(q_lower)
        sub_rows: list[Any] = []
        if has_non_ascii and fts_expr:
            sub_rows = conn.execute(
                """
                SELECT t.id, t.en, t.en_lower, t.zh, t.zh_lower, t.category, t.cat_rank
                FROM translations_zh_fts f
                JOIN translations t ON t.id = f.rowid
                WHERE translations_zh_fts MATCH ?
                LIMIT 250
                """,
                (fts_expr,),
            ).fetchall()
        elif not has_non_ascii and tokens:
            en_match = " AND ".join(_fts_phrase(t) for t in tokens)
            sub_rows = conn.execute(
                """
                SELECT t.id, t.en, t.en_lower, t.zh, t.zh_lower, t.category, t.cat_rank
                FROM translations_en_fts f
                JOIN translations t ON t.id = f.rowid
                WHERE translations_en_fts MATCH ?
                LIMIT 250
                """,
                (en_match,),
            ).fetchall()

        if not sub_rows:
            sub_rows = conn.execute(
                """
                SELECT id, en, en_lower, zh, zh_lower, category, cat_rank
                FROM translations
                WHERE instr(en_lower, ?) > 0 OR instr(zh_lower, ?) > 0
                LIMIT 250
                """,
                (q_lower, q_lower),
            ).fetchall()
            tokens = [q_lower]

        for row in sub_rows:
            if row[0] in seen_ids:
                continue
            combined = row[2] + " " + row[4]
            if tokens and not all(t in combined for t in tokens):
                continue
            seen_ids.add(row[0])
            candidates.append(row)

    scored: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
    for _, en, en_l, zh, zh_l, cat, cat_rank in candidates:
        if en_l == q_lower or zh_l == q_lower:
            tier = 0
            mtype = "exact"
        elif en_l.startswith(q_lower) or zh_l.startswith(q_lower):
            tier = 1
            mtype = "prefix"
        else:
            tier = 2
            mtype = "substring"
        scored.append(
            (
                (tier, cat_rank, min(len(en_l), len(zh_l)), en_l),
                {"en": en, "zh": zh, "category": cat, "match_type": mtype},
            )
        )

    scored.sort(key=lambda item: item[0])
    dedup_out: list[dict[str, Any]] = []
    seen_out: set[tuple[str, str]] = set()
    for _, item in scored:
        key = (item["en"].lower(), item["zh"])
        if key not in seen_out:
            seen_out.add(key)
            dedup_out.append(item)
            if len(dedup_out) >= limit:
                break
    return dedup_out


def query_local_data(
    kind: str,
    query: str,
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    slot: str | None = None,
    ilvl: int | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Query local PoB game data (`gem`, `unique`, `mod`, `base`, `translate`) via SQLite index."""
    kind_norm = kind.strip().lower()
    if kind_norm not in ("gem", "unique", "mod", "base", "translate"):
        raise ValueError(
            f"Unsupported kind {kind!r}; expected one of: gem, unique, mod, base, translate."
        )

    db_path = ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
    limit = max(0, int(limit))
    q_clean = query.strip()
    q_lower = q_clean.lower()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        zh_available = meta.get("zh_available") == "1"

        if limit == 0:
            results: list[dict[str, Any]] = []
        elif kind_norm == "gem":
            results = _query_gems(conn, q_lower, limit)
        elif kind_norm == "unique":
            results = _query_uniques(conn, q_lower, slot, ilvl, limit)
        elif kind_norm == "base":
            results = _query_bases(conn, q_lower, slot, ilvl, limit)
        elif kind_norm == "mod":
            results = _query_mods(conn, q_lower, slot, ilvl, limit)
        else:
            results = _query_translate(conn, q_lower, limit, zh_available)
    finally:
        conn.close()

    return {
        "kind": kind_norm,
        "query": q_clean,
        "filters": {"slot": slot, "ilvl": ilvl, "limit": limit},
        "pobVersion": meta.get("pob_version", "unknown"),
        "dataVersion": meta.get("data_version", "unknown"),
        "zhAvailable": zh_available,
        "count": len(results),
        "results": results,
    }
