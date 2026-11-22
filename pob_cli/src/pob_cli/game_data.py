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
import sys
import threading
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import updater as _updater
from .updater import detect_source_checkout_root, get_runtime_dir

SCHEMA_VERSION = 7
_INDEX_BUILD_LOCK = threading.RLock()

_FOLD_CHAR_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u02bc": "'",
        "\u0060": "'",
        "\u00b4": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u3000": " ",
        "\u00a0": " ",
    }
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_CJK_PARTICLE_RE = re.compile(r"[之的・·．\s]+")


def _fold_search_text(text: str) -> str:
    """Fold Unicode diacritics (NFKD), curly apostrophes/dashes, and case for search matching."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.translate(_FOLD_CHAR_MAP).lower()


def _sqlite_ro_uri(db_path: Path) -> str:
    """Build a properly percent-encoded read-only SQLite file URI."""
    return f"{db_path.resolve().as_uri()}?mode=ro"


def _is_pid_alive(pid: int) -> bool:
    """Best-effort OS check whether process ``pid`` is currently alive."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        if pid > 0xFFFFFFFF:
            return False
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            open_process.restype = ctypes.c_void_p
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            get_exit_code.restype = ctypes.c_int
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int

            handle = open_process(0x1000, 0, int(pid))
            if not handle:
                # ERROR_ACCESS_DENIED (5): process exists under higher privilege
                return ctypes.get_last_error() == 5
            try:
                exit_code = ctypes.c_ulong(0)
                if get_exit_code(handle, ctypes.byref(exit_code)):
                    return int(exit_code.value) == 259  # STILL_ACTIVE
                return False
            finally:
                close_handle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False


def _shared_cache_dir(
    runtime_dir: Path | None = None,
    *,
    pob_root: Path | None = None,
) -> Path:
    """Resolve shared engine-scoped cache dir via updater.get_shared_cache_dir if present."""
    env_cache = os.environ.get("POB_CACHE_DIR", "").strip()
    if env_cache:
        cache_dir = Path(env_cache)
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    fn = getattr(_updater, "get_shared_cache_dir", None)
    if callable(fn):
        if pob_root is not None:
            try:
                cache_dir = Path(fn(runtime_dir, pob_root=pob_root))
                cache_dir.mkdir(parents=True, exist_ok=True)
                return cache_dir
            except TypeError:
                # Older/in-flight updater.get_shared_cache_dir without pob_root kwarg:
                # fall through to local explicit-pob_root check below.
                pass
        else:
            cache_dir = Path(fn(runtime_dir))
            cache_dir.mkdir(parents=True, exist_ok=True)
            return cache_dir

    rt = resolve_runtime_dir(runtime_dir)
    eng_candidate: Path | None = Path(pob_root) if pob_root is not None else None
    if eng_candidate is None:
        env_root = os.environ.get("POB_ROOT", "").strip()
        if env_root:
            eng_candidate = Path(env_root)

    shared_candidate: Path | None = None
    if eng_candidate is not None:
        try:
            eng_parent = eng_candidate.resolve().parent
            same_rt = eng_parent == rt.resolve()
        except OSError:
            eng_parent = eng_candidate.parent
            same_rt = False
        if not same_rt and (
            (eng_parent / "cache").is_dir()
            or eng_parent.name in (".runtime", "pob-agent-tools")
        ):
            shared_candidate = eng_parent / "cache"

    if shared_candidate is not None:
        try:
            shared_candidate.mkdir(parents=True, exist_ok=True)
            return shared_candidate
        except OSError:
            pass

    cache_dir = rt / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir

# Constants from PoE1/Data/Misc.lua (GameConstants.dat) used by CalcTools.lua
SKILL_DAMAGE_BASE_EFFECTIVENESS = 3.885209
SKILL_DAMAGE_INCREMENTAL_EFFECTIVENESS = 0.360246

DEFAULT_PROJECT_ROOT = (
    Path(os.environ["POB_HOME"].strip())
    if os.environ.get("POB_HOME", "").strip()
    else (detect_source_checkout_root() or get_runtime_dir())
)
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
        "Version",
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
        "Level",
        "Requirements",
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
    # Tolerant EN/ZH aliases (P0-2)
    "offhand": (
        ("weapon", "one_hand_weapon", "shield", "quiver", "sword", "axe", "mace", "dagger", "claw", "wand", "sceptre"),
        ("weapon", "shield", "quiver", "default"),
        ("One Handed Sword", "One Handed Axe", "One Handed Mace", "Dagger", "Claw", "Wand", "Sceptre", "Thrusting One Handed Sword", "Shield", "Quiver"),
    ),
    "mainhand": (
        ("weapon", "one_hand_weapon", "two_hand_weapon", "sword", "axe", "mace", "dagger", "claw", "wand", "bow", "staff", "sceptre"),
        ("weapon", "default"),
        ("One Handed Sword", "Two Handed Sword", "One Handed Axe", "Two Handed Axe", "One Handed Mace", "Two Handed Mace", "Dagger", "Claw", "Wand", "Bow", "Staff", "Sceptre", "Thrusting One Handed Sword"),
    ),
    "ring1": (("ring",), ("ring", "default"), ("Ring",)),
    "ring2": (("ring",), ("ring", "default"), ("Ring",)),
    "ring3": (("ring",), ("ring", "default"), ("Ring",)),
    "weapon1": (
        ("weapon", "one_hand_weapon", "two_hand_weapon", "sword", "axe", "mace", "dagger", "claw", "wand", "bow", "staff", "sceptre"),
        ("weapon", "default"),
        ("One Handed Sword", "Two Handed Sword", "One Handed Axe", "Two Handed Axe", "One Handed Mace", "Two Handed Mace", "Dagger", "Claw", "Wand", "Bow", "Staff", "Sceptre", "Thrusting One Handed Sword"),
    ),
    "weapon2": (
        ("weapon", "one_hand_weapon", "shield", "quiver", "sword", "axe", "mace", "dagger", "claw", "wand", "sceptre"),
        ("weapon", "shield", "quiver", "default"),
        ("One Handed Sword", "One Handed Axe", "One Handed Mace", "Dagger", "Claw", "Wand", "Sceptre", "Thrusting One Handed Sword", "Shield", "Quiver"),
    ),
    "衣服": (("body_armour",), ("body_armour", "armour", "default"), ("Body Armour",)),
    "胸甲": (("body_armour",), ("body_armour", "armour", "default"), ("Body Armour",)),
    "头盔": (("helmet",), ("helmet", "armour", "default"), ("Helmet",)),
    "手套": (("gloves",), ("gloves", "armour", "default"), ("Gloves",)),
    "鞋子": (("boots",), ("boots", "armour", "default"), ("Boots",)),
    "靴子": (("boots",), ("boots", "armour", "default"), ("Boots",)),
    "盾牌": (("shield",), ("shield", "armour", "default"), ("Shield",)),
    "箭袋": (("quiver",), ("quiver", "default"), ("Quiver",)),
    "项链": (("amulet",), ("amulet", "default"), ("Amulet",)),
    "护身符": (("amulet",), ("amulet", "default"), ("Amulet",)),
    "戒指": (("ring",), ("ring", "default"), ("Ring",)),
    "左戒指": (("ring",), ("ring", "default"), ("Ring",)),
    "右戒指": (("ring",), ("ring", "default"), ("Ring",)),
    "腰带": (("belt",), ("belt", "default"), ("Belt",)),
    "药剂": (("flask",), ("flask", "default"), ("Flask",)),
    "珠宝": (("jewel", "abyss_jewel"), ("jewel", "default"), ()),
    "主手": (
        ("weapon", "one_hand_weapon", "two_hand_weapon", "sword", "axe", "mace", "dagger", "claw", "wand", "bow", "staff", "sceptre"),
        ("weapon", "default"),
        ("One Handed Sword", "Two Handed Sword", "One Handed Axe", "Two Handed Axe", "One Handed Mace", "Two Handed Mace", "Dagger", "Claw", "Wand", "Bow", "Staff", "Sceptre", "Thrusting One Handed Sword"),
    ),
    "副手": (
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
    """Resolve runtime directory: explicit arg -> POB_RUNTIME_DIR -> POB_HOME -> checkout/.runtime -> user appdata."""
    return get_runtime_dir(runtime_dir)


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
    checkout_root = detect_source_checkout_root()
    if checkout_root is not None:
        local_pob = checkout_root / "PobTools[20261102]"
        if local_pob.is_dir():
            return local_pob
    if FALLBACK_POB_ROOT.is_dir():
        return FALLBACK_POB_ROOT
    return managed_engine


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
    _scan_dir_entries(hasher, data_dir / "Uniques" / "Special", ".lua")

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
        self.aliases: dict[str, list[str]] = {}
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
            "Z-Keystone_Proverb.json",
            "Z-Ascendancy_Proverb.json",
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
            is_passive_file = fn in ("tree_dn.json", "TattooPassives.json", "Z-Phrecia.json") or is_season_patch
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

        # Also index short entity-name community/TW aliases (e.g. "象牙塔", "看守之眼", "禁忌烈焰")
        # from A-1a.json without pulling 38k superseded stat templates.
        alias_path = zh_dir / "A-1a.json"
        if alias_path.is_file() and "A-1a.json" in superseded:
            try:
                alias_payload = json.loads(alias_path.read_text(encoding="utf-8"))
                alias_entries = alias_payload.get("entries") if isinstance(alias_payload, dict) else None
                if isinstance(alias_entries, dict):
                    for raw_en, raw_zh in alias_entries.items():
                        if not isinstance(raw_en, str) or not isinstance(raw_zh, str):
                            continue
                        en = raw_en.strip()
                        zh = raw_zh.strip()
                        if (
                            not en
                            or not zh
                            or len(en) > 60
                            or len(zh) > 60
                            or "{" in en
                            or "#" in en
                            or "\n" in en
                            or "\n" in zh
                        ):
                            continue
                        bucket = obj.aliases.setdefault(en.lower(), [])
                        if zh not in bucket:
                            bucket.append(zh)
            except Exception:
                pass
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


ITEM_SKILL_FILES: frozenset[str] = frozenset({"other.lua", "glove.lua", "minion.lua"})


def _parse_gems_and_skills(poe1_dir: Path, zh: _ZhDictionary) -> list[dict[str, Any]]:
    data_dir = poe1_dir / "Data"
    skills: dict[str, dict[Any, Any]] = {}
    skill_source_file: dict[str, str] = {}
    for fn in SKILL_FILES:
        p = data_dir / "Skills" / fn
        if p.is_file():
            is_item_file = fn in ITEM_SKILL_FILES
            parsed_sk = _parse_lua_assignments(
                p,
                "skills",
                skip_keys=LUA_SKIP_KEYS_SKILL,
                keep_levels=None if is_item_file else SKILL_KEEP_LEVELS,
                prestrip_re=None if is_item_file else SKILL_HIGH_LEVEL_STRIP_RE,
            )
            skills.update(parsed_sk)
            for sk_id in parsed_sk:
                skill_source_file[sk_id] = fn

    gems_raw = _parse_lua_return(data_dir / "Gems.lua")
    if not isinstance(gems_raw, dict):
        gems_raw = {}

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
    referenced_skill_ids: set[str] = set()
    records: list[dict[str, Any]] = []

    for gem_id, gem in gems_raw.items():
        if not isinstance(gem_id, str) or not isinstance(gem, dict):
            continue
        name = str(gem.get("name") or "").strip()
        if not name:
            continue
        ge_id = str(gem.get("grantedEffectId") or "")
        sec_id = gem.get("secondaryGrantedEffectId")
        if ge_id:
            referenced_skill_ids.add(ge_id)
        if isinstance(sec_id, str) and sec_id:
            referenced_skill_ids.add(sec_id)
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
            if sec_skill and not is_vaal:
                sec_lvl = _compute_skill_level_stats(sec_skill, lvl_num)
                if sec_lvl is not None:
                    if lvl_stats is None:
                        lvl_stats = sec_lvl
                    else:
                        merged_st = dict(sec_lvl.get("stats") or {})
                        merged_st.update(lvl_stats.get("stats") or {})
                        lvl_stats["stats"] = merged_st
                        for k_aux in ("cooldown", "critChance", "damageEffectiveness", "manaMultiplier", "cost"):
                            if lvl_stats.get(k_aux) is None and sec_lvl.get(k_aux) is not None:
                                lvl_stats[k_aux] = sec_lvl[k_aux]
            if lvl_stats is not None:
                levels_out[str(lvl_num)] = lvl_stats

        quality_stats: list[dict[str, Any]] = []
        seen_qs: set[str] = set()
        for src_sk in (skill, sec_skill) if (sec_skill and not is_vaal) else (skill,):
            for qs in _as_seq(src_sk.get("qualityStats")):
                if isinstance(qs, list) and len(qs) >= 2 and isinstance(qs[1], (int, float)) and str(qs[0]) not in seen_qs:
                    seen_qs.add(str(qs[0]))
                    quality_stats.append(
                        {
                            "stat": qs[0],
                            "per_quality": qs[1],
                            "at_20_quality": round(float(qs[1]) * 20, 4),
                        }
                    )

        constant_stats: list[dict[str, Any]] = []
        seen_cs: set[str] = set()
        for src_sk in (skill, sec_skill) if (sec_skill and not is_vaal) else (skill,):
            for cs in _as_seq(src_sk.get("constantStats")):
                if isinstance(cs, list) and len(cs) >= 2 and str(cs[0]) not in seen_cs:
                    seen_cs.add(str(cs[0]))
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
            "is_item_skill": False,
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

    # Index item-, ascendancy-, tree-, enchant-, and minion-granted skills from Data/Skills/*.lua
    for sk_id, skill in skills.items():
        if sk_id in referenced_skill_ids or not isinstance(skill, dict):
            continue
        src_fn = skill_source_file.get(sk_id, "")
        from_item = bool(skill.get("fromItem"))
        from_tree = bool(skill.get("fromTree"))
        if src_fn not in ITEM_SKILL_FILES and not from_item and not from_tree:
            continue
        name = str(skill.get("name") or "").strip()
        if not name:
            continue

        is_support = bool(skill.get("support"))
        color = COLOR_NAMES.get(int(skill.get("color") or 0), "")
        raw_levels = skill.get("levels")
        avail_lvl_nums = sorted(k for k in raw_levels if isinstance(k, int)) if isinstance(raw_levels, dict) else []
        if len(avail_lvl_nums) > 8:
            pick = sorted({avail_lvl_nums[0], avail_lvl_nums[-1], *(lv for lv in (1, 10, 15, 20, 21, 25, 30) if lv in avail_lvl_nums)})
        else:
            pick = avail_lvl_nums

        levels_out = {}
        for lvl_num in pick:
            lvl_stats = _compute_skill_level_stats(skill, lvl_num)
            if lvl_stats is not None:
                levels_out[str(lvl_num)] = lvl_stats

        quality_stats = []
        for qs in _as_seq(skill.get("qualityStats")):
            if isinstance(qs, list) and len(qs) >= 2 and isinstance(qs[1], (int, float)):
                quality_stats.append(
                    {
                        "stat": qs[0],
                        "per_quality": qs[1],
                        "at_20_quality": round(float(qs[1]) * 20, 4),
                    }
                )

        constant_stats = []
        for cs in _as_seq(skill.get("constantStats")):
            if isinstance(cs, list) and len(cs) >= 2:
                constant_stats.append({"stat": cs[0], "value": cs[1]})

        base_flags = skill.get("baseFlags") if isinstance(skill.get("baseFlags"), dict) else {}
        tags_list = sorted(str(k) for k, v in base_flags.items() if v)
        if is_support and "support" not in tags_list:
            tags_list.append("support")
        if from_item:
            skill_source = "item"
        elif from_tree:
            skill_source = "tree"
        else:
            skill_source = src_fn.removesuffix(".lua") or "skill"

        ref_lvl = (
            levels_out.get("20")
            or (levels_out.get(str(pick[-1])) if pick else None)
            or levels_out.get("1")
            or {}
        )
        zh_name = zh.translate_gem(name) or (zh.passives.get(name) if zh.available else None)

        rec = {
            "id": f"Skill/{sk_id}",
            "name": name,
            "display_name": name,
            "zh_name": zh_name,
            "game_id": "",
            "variant_id": "",
            "granted_effect_id": sk_id,
            "secondary_granted_effect_id": None,
            "plus_version_of": None,
            "is_support": is_support,
            "is_vaal": False,
            "is_transfigured": False,
            "is_imbued": False,
            "is_item_skill": True,
            "skill_source": skill_source,
            "color": color,
            "tag_string": ", ".join(t.title() for t in tags_list),
            "tags": tags_list,
            "req_str": 0,
            "req_dex": 0,
            "req_int": 0,
            "natural_max_level": pick[-1] if pick else 1,
            "description": str(skill.get("description") or ""),
            "cast_time": skill.get("castTime"),
            "crit_chance": ref_lvl.get("critChance"),
            "damage_effectiveness": ref_lvl.get("damageEffectiveness"),
            "mana_multiplier": ref_lvl.get("manaMultiplier"),
            "mana_cost": {lvl_k: lvl_v.get("cost") for lvl_k, lvl_v in levels_out.items() if lvl_v.get("cost")},
            "quality_stats": quality_stats,
            "constant_stats": constant_stats,
            "levels": levels_out,
            "secondary_skill_name": None,
            "variants": [],
        }
        records.append(rec)
        by_granted_id.setdefault(sk_id, []).append(rec)
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


def _extract_special_lua_blocks(special_dir: Path, data_dir: Path) -> list[tuple[str, str]]:
    """Safely extract unique item text blocks from all ``Data/Uniques/Special/*.lua`` files.

    Handles standard return-table files (``race.lua``), ``New.lua`` (``data.uniques.new``),
    mod-pool tables (``WatchersEye.lua``, ``BoundByDestiny.lua``), and programmatic unique
    generators in ``Generated.lua`` (Watcher's Eye, Sublime Vision, Vorana's March,
    Bound by Destiny, Forbidden Flame/Flesh, Impossible Escape, Skin of the Lords,
    The Balance of Terror, Precursor's Emblem, Megalomaniac, Forbidden Shako,
    Replica Dragonfang's Flight, Pearl of Tsoatha, etc.).
    """
    if not special_dir.is_dir():
        return []

    out: list[tuple[str, str]] = []
    watchers_eye_mods: list[str] = []
    sublime_vision_mods: list[str] = []
    voranas_march_mods: list[str] = []
    bound_by_destiny_mods: list[str] = []

    we_path = special_dir / "WatchersEye.lua"
    if we_path.is_file():
        parsed = _parse_lua_return(
            we_path,
            skip_keys=LUA_SKIP_KEYS_MOD,
            prestrip_re=MOD_TRADEHASH_STRIP_RE,
            prestrip_repl=" }",
        )
        if isinstance(parsed, dict):
            for mod_id, entry in parsed.items():
                if not isinstance(mod_id, str) or not isinstance(entry, dict):
                    continue
                stat_lines = [str(s).strip() for s in _as_seq(entry) if isinstance(s, str) and s.strip()]
                if not stat_lines:
                    continue
                if mod_id.startswith("SublimeVision"):
                    sublime_vision_mods.extend(stat_lines)
                elif mod_id.startswith("SummonArbalist"):
                    voranas_march_mods.extend(stat_lines)
                else:
                    watchers_eye_mods.extend(stat_lines)

    bbd_path = special_dir / "BoundByDestiny.lua"
    if bbd_path.is_file():
        parsed = _parse_lua_return(
            bbd_path,
            skip_keys=LUA_SKIP_KEYS_MOD,
            prestrip_re=MOD_TRADEHASH_STRIP_RE,
            prestrip_repl=" }",
        )
        if isinstance(parsed, dict):
            for entry in parsed.values():
                if isinstance(entry, dict):
                    stat_lines = [str(s).strip() for s in _as_seq(entry) if isinstance(s, str) and s.strip()]
                    if stat_lines:
                        bound_by_destiny_mods.append(" ".join(stat_lines))

    gen_path = special_dir / "Generated.lua"
    if gen_path.is_file():
        gen_txt = gen_path.read_text(encoding="utf-8", errors="ignore")
        tokens = _tokenize_lua(gen_txt)
        parser = _LuaParser(tokens)
        local_tables: dict[str, Any] = {}
        local_inserts: dict[str, list[str]] = {}
        generated_vars: list[str] = []

        # Single linear scan over Generated.lua tokens to capture local tables & static inserts
        while parser.pos < parser.n:
            tok = parser.peek()
            if tok == ("IDENT", "local"):
                nxt = parser.peek(1)
                eq = parser.peek(2)
                brace = parser.peek(3)
                if (
                    nxt is not None
                    and nxt[0] == "IDENT"
                    and eq == ("OP", "=")
                    and brace == ("OP", "{")
                ):
                    var_name = nxt[1]
                    parser.pos += 3
                    local_tables[var_name] = parser.parse_table()
                    continue
            if (
                tok == ("IDENT", "table")
                and parser.peek(1) == ("OP", ".")
                and parser.peek(2) == ("IDENT", "insert")
                and parser.peek(3) == ("OP", "(")
            ):
                parser.pos += 4
                target_tok = parser.peek()
                if target_tok is not None and target_tok[0] == "IDENT":
                    target_var = target_tok[1]
                    # Check table.insert(data.uniques.generated, table.concat(<var>, "\n"))
                    if (
                        target_var == "data"
                        and parser.peek(1) == ("OP", ".")
                        and parser.peek(2) == ("IDENT", "uniques")
                        and parser.peek(3) == ("OP", ".")
                        and parser.peek(4) == ("IDENT", "generated")
                        and parser.peek(5) == ("OP", ",")
                        and parser.peek(6) == ("IDENT", "table")
                        and parser.peek(7) == ("OP", ".")
                        and parser.peek(8) == ("IDENT", "concat")
                        and parser.peek(9) == ("OP", "(")
                    ):
                        src_tok = parser.peek(10)
                        if src_tok is not None and src_tok[0] == "IDENT" and src_tok[1] not in generated_vars:
                            generated_vars.append(src_tok[1])
                    elif parser.peek(1) == ("OP", ",") and parser.peek(2) is not None and parser.peek(2)[0] == "STR":
                        # Only record direct literal string inserts (not loop concat expressions like "Variant: "..x)
                        if parser.peek(3) == ("OP", ")"):
                            val_str = parser.peek(2)[1]
                            local_inserts.setdefault(target_var, []).append(val_str)
                continue
            parser.advance()

        # Collect charge/curse mod dictionaries defined inside Generated.lua
        charge_mod_lines: list[str] = []
        for cname in ("enduranceChargeMods", "frenzyChargeMods", "powerChargeMods"):
            tbl = local_tables.get(cname)
            if isinstance(tbl, dict):
                for tier_tbl in tbl.values():
                    if isinstance(tier_tbl, dict):
                        for v in tier_tbl.values():
                            if isinstance(v, str) and v.strip():
                                charge_mod_lines.append(v.strip())
        balance_mod_lines: list[str] = []
        bot_tbl = local_tables.get("balanceOfTerrorMods")
        if isinstance(bot_tbl, dict):
            for v in bot_tbl.values():
                if isinstance(v, str) and v.strip():
                    balance_mod_lines.append(v.strip())

        extra_by_var: dict[str, list[str]] = {
            "precursorsEmblem": charge_mod_lines,
            "balanceOfTerror": balance_mod_lines,
            "watchersEye": watchers_eye_mods,
            "sublimeVision": sublime_vision_mods,
            "voranasMarch": voranas_march_mods,
            "boundByDestiny": bound_by_destiny_mods,
            "megalomaniac": ["1 Added Passive Skill is <Cluster Notable>"],
            "forbiddenShako": [
                "Socketed Gems are Supported by Level (1-10) <Support Gem>",
                "Socketed Gems are Supported by Level (25-35) <Support Gem>",
            ],
            "replicaForbiddenShako": [
                "Socketed Gems are Supported by Level (1-10) <Support Gem>",
                "Socketed Gems are Supported by Level (25-35) <Support Gem>",
            ],
            "skinOfTheLords": ["Allocates a random Keystone Passive Skill"],
            "impossibleEscape": [
                "Passive Skills in radius of <Keystone> can be allocated without being connected to your tree"
            ],
            "replicaDragonfangsFlight": ["+3 to Level of all <Active Skill> Gems"],
            "pearl": [
                "+(8-10)% to all Elemental Resistances",
                "Socketed/Random Support Gem (Helmet / Gloves / Boots / Passive Tree)",
            ],
        }

        # Preserve order of generated_vars, plus any known local unique tables
        ordered_vars = list(generated_vars)
        for known in (
            "megalomaniac",
            "forbiddenShako",
            "replicaForbiddenShako",
            "precursorsEmblem",
            "balanceOfTerror",
            "watchersEye",
            "sublimeVision",
            "voranasMarch",
            "boundByDestiny",
            "skinOfTheLords",
            "impossibleEscape",
            "replicaDragonfangsFlight",
            "pearl",
        ):
            if known in local_tables and known not in ordered_vars:
                ordered_vars.append(known)

        for var_name in ordered_vars:
            tbl = local_tables.get(var_name)
            parts: list[str] = [str(x) for x in _as_seq(tbl) if isinstance(x, str) and x.strip()]
            parts.extend(s for s in local_inserts.get(var_name, []) if s.strip())
            parts.extend(extra_by_var.get(var_name, []))
            if parts:
                out.append((var_name, "\n".join(parts)))

        # Synthesize Forbidden Flame & Forbidden Flesh from buildForbidden()
        if "buildForbidden" in gen_txt:
            out.append(
                (
                    "ForbiddenFlame",
                    "\n".join(
                        [
                            "Rarity: UNIQUE",
                            "Forbidden Flame",
                            "Crimson Jewel",
                            "Source: Drops from unique{The Searing Exarch}",
                            "Limited to: 1",
                            "Item Level: 83",
                            "Implicits: 0",
                            "Allocates <Ascendancy Notable> if you have the matching modifier on Forbidden Flesh",
                            "Corrupted",
                        ]
                    ),
                )
            )
            out.append(
                (
                    "ForbiddenFlesh",
                    "\n".join(
                        [
                            "Rarity: UNIQUE",
                            "Forbidden Flesh",
                            "Cobalt Jewel",
                            "Source: Drops from unique{The Eater of Worlds}",
                            "Limited to: 1",
                            "Item Level: 83",
                            "Implicits: 0",
                            "Allocates <Ascendancy Notable> if you have the matching modifier on Forbidden Flame",
                            "Corrupted",
                        ]
                    ),
                )
            )
    else:
        # Even without Generated.lua (e.g. minimal/synthetic engine dir), synthesize Watcher's Eye /
        # Bound by Destiny if their mod tables exist under Special/.
        if watchers_eye_mods:
            out.append(
                (
                    "WatchersEye",
                    "\n".join(
                        [
                            "Watcher's Eye",
                            "Prismatic Jewel",
                            "Limited to: 1",
                            "Implicits: 0",
                            "(4-6)% increased maximum Energy Shield",
                            "(4-6)% increased maximum Life",
                            "(4-6)% increased maximum Mana",
                            *watchers_eye_mods,
                        ]
                    ),
                )
            )
        if bound_by_destiny_mods:
            out.append(
                (
                    "BoundByDestiny",
                    "\n".join(
                        [
                            "Bound by Destiny",
                            "Prismatic Jewel",
                            "Limited to: 1",
                            "Implicits: 0",
                            *bound_by_destiny_mods,
                        ]
                    ),
                )
            )

    # Parse all other Special/*.lua files (race.lua, New.lua, or custom return-table special uniques)
    handled_special = {"Generated.lua", "WatchersEye.lua", "BoundByDestiny.lua"}
    for p in sorted(special_dir.glob("*.lua")):
        if p.name in handled_special:
            continue
        blocks = _parse_lua_return(p)
        seq = [b for b in _as_seq(blocks) if isinstance(b, str) and b.strip()]
        if seq:
            for b in seq:
                out.append((p.stem, b))
            continue
        # Handle `data.uniques.new = { [[...]], ... }` style files such as New.lua
        txt = p.read_text(encoding="utf-8", errors="ignore")
        toks = _tokenize_lua(txt)
        p_sub = _LuaParser(toks)
        while p_sub.pos < p_sub.n:
            if p_sub.peek() == ("OP", "{"):
                tbl = p_sub.parse_table()
                for b in _as_seq(tbl):
                    if isinstance(b, str) and b.strip():
                        out.append((p.stem, b))
            else:
                p_sub.advance()

    return out


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

    raw_blocks: list[tuple[str, str]] = []
    for p in sorted(uniques_dir.glob("*.lua")):
        blocks = _parse_lua_return(p)
        for block in _as_seq(blocks):
            if isinstance(block, str):
                raw_blocks.append((p.stem, block))

    raw_blocks.extend(_extract_special_lua_blocks(uniques_dir / "Special", data_dir))

    records: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for fallback_slot, block in raw_blocks:
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        while lines and lines[0].startswith(("Item Class:", "Rarity:")):
            lines = lines[1:]
        if len(lines) < 2:
            continue
        name = lines[0]
        if name in seen_names:
            continue
        seen_names.add(name)
        base_type = MOD_TAG_PREFIX_RE.sub("", lines[1]).strip() or lines[1]

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
            if ln.startswith("Requirements:"):
                idx += 1
                continue
            if ln.startswith("Requires Level ") or ln.startswith("LevelReq:") or ln.startswith("Level:"):
                m_lvl = re.search(r"(\d+)", ln)
                if m_lvl:
                    req_level = int(m_lvl.group(1))
                idx += 1
                continue
            if ln.startswith("{variant:") and idx < 10 and MOD_TAG_PREFIX_RE.sub("", ln).strip() in bases_by_name:
                # Alternate base-type lines (e.g. Precursor's Emblem {variant:2}Sapphire Ring)
                idx += 1
                continue
            if ln in ("Shaper Item", "Elder Item", "Fractured Item", "Synthesised Item"):
                idx += 1
                continue
            if ":" in ln and not ln.startswith("{") and ln.split(":", 1)[0].strip() in UNIQUE_META_KEYS:
                idx += 1
                continue
            # Some legacy or generated uniques omit "Implicits:" line directly
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
            is_item_skill INTEGER NOT NULL DEFAULT 0,
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
        conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
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


def _is_build_lock_stale(lock_path: Path, *, stale_sec: float = 60.0) -> bool:
    try:
        st = lock_path.stat()
    except (OSError, OverflowError):
        return False
    # Reclaim any lockfile (including same-PID leaked locks) older than stale_sec.
    if (time.time() - st.st_mtime) > stale_sec:
        return True
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            ts = payload.get("ts")
            if isinstance(ts, (int, float)) and (time.time() - float(ts)) > stale_sec:
                return True
            pid = int(payload.get("pid") or 0)
            if pid > 0 and pid != os.getpid() and not _is_pid_alive(pid):
                return True
    except Exception:
        pass
    return False


def _try_break_stale_lock(lock_path: Path) -> bool:
    """Atomically claim a stale lockfile via rename before unlinking so peers never delete a fresh lock."""
    trash = lock_path.parent / f".{lock_path.name}.stale.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    try:
        os.replace(lock_path, trash)
    except (OSError, OverflowError):
        return False
    try:
        trash.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def _release_build_lock(lock_path: Path, owner_token: str) -> None:
    """Unlink ``lock_path`` only if its stored owner token still matches ``owner_token``."""
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict) or payload.get("token") != owner_token:
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _acquire_build_lock(
    lock_path: Path,
    db_path: Path,
    pob_version: str,
    data_version: str,
    *,
    force_rebuild: bool = False,
    timeout_sec: float = 45.0,
    stale_sec: float = 60.0,
) -> str | None:
    """Acquire atomic cross-process lockfile; returns owner token if acquired, None if DB became valid while waiting."""
    deadline = time.monotonic() + max(0.05, float(timeout_sec))
    deadline_break_attempted = False
    stale_breaks = 0
    while True:
        token = f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(
                    fd,
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "tid": threading.get_ident(),
                            "token": token,
                            "ts": time.time(),
                        }
                    ).encode("utf-8"),
                )
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            else:
                try:
                    os.close(fd)
                except BaseException:
                    try:
                        lock_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
            return token
        except FileExistsError:
            if not force_rebuild and _is_index_valid(db_path, pob_version, data_version):
                return None
            now_mono = time.monotonic()
            if _is_build_lock_stale(lock_path, stale_sec=stale_sec):
                if _try_break_stale_lock(lock_path):
                    stale_breaks += 1
                    if now_mono < deadline:
                        if stale_breaks > 1:
                            time.sleep(0.05)
                        continue
                    if not deadline_break_attempted:
                        deadline_break_attempted = True
                        continue
                    raise TimeoutError(f"Timed out acquiring game data build lock: {lock_path}")
                if now_mono >= deadline:
                    raise TimeoutError(f"Timed out breaking stale game data build lock: {lock_path}")
                time.sleep(0.05)
                continue
            if now_mono >= deadline:
                if not deadline_break_attempted:
                    deadline_break_attempted = True
                    if _try_break_stale_lock(lock_path):
                        continue
                raise TimeoutError(f"Timed out waiting for game data build lock: {lock_path}")
            time.sleep(0.05)
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Failed to acquire game data build lock {lock_path}: {exc}") from exc
            time.sleep(0.05)


def _write_gamedata_sqlite(
    tmp_path: Path,
    *,
    pob_version: str,
    data_version: str,
    zh: _ZhDictionary,
    gems: list[dict[str, Any]],
    bases: list[dict[str, Any]],
    uniques: list[dict[str, Any]],
    mods: list[dict[str, Any]],
) -> None:
    if tmp_path.exists():
        tmp_path.unlink()

    conn = sqlite3.connect(str(tmp_path))
    try:
        conn.execute("PRAGMA page_size = 8192")
        conn.execute("PRAGMA locking_mode = EXCLUSIVE")
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -65536")
        _create_schema(conn)
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")

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
            s_txt = _fold_search_text(
                " ".join(
                    p
                    for p in (
                        g["name"],
                        g["display_name"],
                        g["zh_name"] or "",
                        g["tag_string"],
                        g["granted_effect_id"],
                    )
                    if p
                )
            )
            gem_rows.append(
                (
                    g["id"],
                    g["name"],
                    _fold_search_text(g["name"]),
                    _fold_search_text(g["display_name"]),
                    g["zh_name"],
                    _fold_search_text(g["zh_name"]) if g["zh_name"] else None,
                    g["game_id"],
                    g["variant_id"],
                    int(g["is_support"]),
                    int(g["is_vaal"]),
                    int(g["is_transfigured"]),
                    int(g["is_imbued"]),
                    int(bool(g.get("is_item_skill"))),
                    g["color"],
                    g["tag_string"],
                    s_txt,
                    json.dumps(g, ensure_ascii=False, separators=(",", ":")),
                )
            )
        conn.executemany(
            "INSERT INTO gems VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            gem_rows,
        )
        conn.execute("INSERT INTO gems_fts(gems_fts) VALUES('rebuild')")

        # Bases
        base_rows = []
        for b in bases:
            slots_l = _fold_search_text(
                " ".join([b["type"], b["sub_type"] or "", *b["tags"]])
            )
            s_txt = _fold_search_text(
                " ".join(
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
                )
            )
            base_rows.append(
                (
                    b["name"],
                    _fold_search_text(b["name"]),
                    b["zh_name"],
                    _fold_search_text(b["zh_name"]) if b["zh_name"] else None,
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
            slots_l = _fold_search_text(" ".join([u["item_class"], *u["tags"]]))
            fb_lines = " ".join(
                " ".join(fm["stats"]) + " " + " ".join(x for x in fm["stats_zh"] if x) for fm in u["foulborn_mods"]
            )
            s_txt = _fold_search_text(
                " ".join(
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
                )
            )
            unique_rows.append(
                (
                    u["name"],
                    _fold_search_text(u["name"]),
                    u["zh_name"],
                    _fold_search_text(u["zh_name"]) if u["zh_name"] else None,
                    _fold_search_text(u["foulborn_name"]) if u["foulborn_name"] else None,
                    _fold_search_text(u["zh_foulborn_name"]) if u["zh_foulborn_name"] else None,
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
            en_stats = _fold_search_text("\n".join(m["stats"]))
            zh_stats = _fold_search_text("\n".join(x for x in m["stats_zh"] if x))
            search_en = _fold_search_text(" ".join([m["id"], m["affix"], m["group"]])) + " " + en_stats
            search_zh = zh_stats
            fts_txt = f"{search_en} {search_zh}".strip()
            mod_rows.append(
                (
                    m["id"],
                    m["source"],
                    m["generation_type"],
                    m["group"],
                    _fold_search_text(m["group"]),
                    m["affix"],
                    _fold_search_text(m["affix"]),
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

        # Translations (merge ZH dictionaries + parsed entities + community aliases)
        tr_best: dict[tuple[str, str], tuple[str, str, str, str, str, int]] = {}

        def _add_tr(en_s: str, zh_s: str, cat_s: str, rank_i: int) -> None:
            en_folded = _fold_search_text(en_s)
            zh_folded = _fold_search_text(zh_s)
            k = (en_folded, zh_s)
            prev = tr_best.get(k)
            if prev is None or rank_i < prev[5]:
                tr_best[k] = (en_s, en_folded, zh_s, zh_folded, cat_s, rank_i)

        for en, zh_str, cat, rank in zh.all_entries:
            _add_tr(en, zh_str, cat, rank)
        for g in gems:
            if g["zh_name"]:
                _add_tr(g["name"], g["zh_name"], "gem", 1)
            for alias_zh in zh.aliases.get(g["name"].lower(), ()):
                _add_tr(g["name"], alias_zh, "gem", 2)
        for u in uniques:
            if u["zh_name"]:
                _add_tr(u["name"], u["zh_name"], "unique", 1)
            for alias_zh in zh.aliases.get(u["name"].lower(), ()):
                _add_tr(u["name"], alias_zh, "unique", 2)
        for b in bases:
            if b["zh_name"]:
                _add_tr(b["name"], b["zh_name"], "base", 1)
            for alias_zh in zh.aliases.get(b["name"].lower(), ()):
                _add_tr(b["name"], alias_zh, "base", 2)
        for p_en in zh.passives:
            for alias_zh in zh.aliases.get(p_en.lower(), ()):
                _add_tr(p_en, alias_zh, "passive", 2)

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

    cache_dir = _shared_cache_dir(runtime_dir, pob_root=resolved_pob)
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

    lock_path = Path(f"{db_path}.build.lock")
    with _INDEX_BUILD_LOCK:
        if not force_rebuild and _is_index_valid(db_path, pob_version, data_version):
            _VALID_INDEX_CACHE.add(cache_key)
            return db_path

        lock_token = _acquire_build_lock(
            lock_path,
            db_path,
            pob_version,
            data_version,
            force_rebuild=force_rebuild,
        )
        if not lock_token:
            _VALID_INDEX_CACHE.add(cache_key)
            return db_path

        tmp_path = cache_dir / f".gamedata_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.sqlite.tmp"
        try:
            # Re-check after acquiring cross-process lock in case another worker just finished
            if not force_rebuild and _is_index_valid(db_path, pob_version, data_version):
                _VALID_INDEX_CACHE.add(cache_key)
                return db_path

            zh = _ZhDictionary.load(zh_dir)
            gems = _parse_gems_and_skills(poe1_dir, zh)
            bases = _parse_bases(poe1_dir, zh)
            bases_by_name = {b["name"]: b for b in bases}
            mods, foulborn_by_id = _parse_mods(poe1_dir, zh)
            uniques = _parse_uniques(poe1_dir, bases_by_name, foulborn_by_id, zh)

            _write_gamedata_sqlite(
                tmp_path,
                pob_version=pob_version,
                data_version=data_version,
                zh=zh,
                gems=gems,
                bases=bases,
                uniques=uniques,
                mods=mods,
            )

            replaced = False
            last_perm_err: PermissionError | None = None
            for attempt in range(4):
                try:
                    os.replace(tmp_path, db_path)
                    replaced = True
                    break
                except PermissionError as exc:
                    last_perm_err = exc
                    if not force_rebuild and _is_index_valid(db_path, pob_version, data_version):
                        replaced = True
                        break
                    if attempt < 3:
                        time.sleep(0.05)

            if not replaced:
                if not force_rebuild and _is_index_valid(db_path, pob_version, data_version):
                    replaced = True
                elif last_perm_err is not None:
                    raise last_perm_err

            _VALID_INDEX_CACHE.add(cache_key)
            return db_path
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            if lock_token:
                _release_build_lock(lock_path, lock_token)


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
        rows = conn.execute("SELECT data_json FROM gems ORDER BY is_item_skill, name LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    fts_expr, tokens = _build_fts_match(q_lower)
    if fts_expr:
        rows = conn.execute(
            """
            SELECT g.name_lower, g.display_name_lower, g.zh_lower, g.is_item_skill,
                   g.is_vaal, g.is_transfigured, g.search_text_lower, g.data_json
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
            SELECT name_lower, display_name_lower, zh_lower, is_item_skill,
                   is_vaal, is_transfigured, search_text_lower, data_json
            FROM gems
            WHERE instr(search_text_lower, ?) > 0
            LIMIT 250
            """,
            (q_lower,),
        ).fetchall()

    scored: list[tuple[tuple[int, int, int, int, str], str]] = []
    for name_l, disp_l, zh_l, is_item_skill, is_vaal, is_trans, search_l, data_json in rows:
        zh_l = zh_l or ""
        if tokens and not all(t in search_l for t in tokens):
            continue
        if q_lower == name_l or q_lower == disp_l or (zh_l and q_lower == zh_l):
            tier = 0
        elif name_l.startswith(q_lower) or disp_l.startswith(q_lower) or (zh_l and zh_l.startswith(q_lower)):
            tier = 1
        elif q_lower in name_l or q_lower in disp_l or (zh_l and q_lower in zh_l):
            tier = 2
        else:
            tier = 3
        scored.append(((tier, int(is_item_skill), is_vaal + is_trans, len(name_l), name_l), data_json))

    scored.sort(key=lambda item: item[0])
    return [json.loads(item[1]) for item in scored[:limit]]


def _unique_matching_mod_lines(obj: dict[str, Any], mod_tokens: list[str], mod_phrase: str) -> tuple[int, list[str]]:
    """Return `(tier, matched_lines)` for a unique item against a folded `--mod` query, or `(-1, [])` if unmatched."""
    en_lines: list[str] = []
    zh_lines: list[str] = []
    for k_en, k_zh in (("implicits", "implicits_zh"), ("explicits", "explicits_zh")):
        raw_en = obj.get(k_en) or []
        raw_zh = obj.get(k_zh) or []
        for idx, line in enumerate(raw_en):
            s_en = MOD_TAG_PREFIX_RE.sub("", str(line or "")).strip()
            s_zh = str(raw_zh[idx]).strip() if idx < len(raw_zh) and raw_zh[idx] else ""
            if s_en:
                en_lines.append(s_en)
                zh_lines.append(s_zh)
    for fb in obj.get("foulborn_mods") or []:
        fb_en = fb.get("stats") or []
        fb_zh = fb.get("stats_zh") or []
        for idx, line in enumerate(fb_en):
            s_en = MOD_TAG_PREFIX_RE.sub("", str(line or "")).strip()
            s_zh = str(fb_zh[idx]).strip() if idx < len(fb_zh) and fb_zh[idx] else ""
            if s_en:
                en_lines.append(f"[Foulborn] {s_en}")
                zh_lines.append(s_zh)

    exact_line_hits: list[str] = []
    token_line_hits: list[str] = []
    all_folded_parts: list[str] = []

    for s_en, s_zh in zip(en_lines, zh_lines):
        f_en = _fold_search_text(s_en)
        f_zh = _fold_search_text(s_zh) if s_zh else ""
        comb = f"{f_en} {f_zh}".strip()
        all_folded_parts.append(comb)
        disp = f"{s_en} / {s_zh}" if s_zh else s_en
        if mod_phrase and (mod_phrase in f_en or (f_zh and mod_phrase in f_zh)):
            exact_line_hits.append(disp)
        elif mod_tokens and all(t in comb for t in mod_tokens):
            token_line_hits.append(disp)

    if exact_line_hits:
        return 0, exact_line_hits
    if token_line_hits:
        return 1, token_line_hits

    whole_text = " ".join(all_folded_parts)
    if mod_tokens and all(t in whole_text for t in mod_tokens):
        partial_hits = [
            f"{s_en} / {s_zh}" if s_zh else s_en
            for s_en, s_zh in zip(en_lines, zh_lines)
            if any(t in _fold_search_text(f"{s_en} {s_zh}") for t in mod_tokens)
        ]
        return 2, partial_hits[:6]
    return -1, []


def _query_uniques(
    conn: sqlite3.Connection,
    q_lower: str,
    slot: str | None,
    ilvl: int | None,
    limit: int,
    *,
    mod_filter: str | None = None,
) -> list[dict[str, Any]]:
    slot_spec = _normalize_slot_filter(slot) if slot else None
    mod_lower = _fold_search_text(mod_filter or "").strip()
    mod_tokens = [t for t in mod_lower.split() if t] if mod_lower else []

    # Build combined search expression for FTS5 candidate narrowing when possible
    combined_q = " ".join(p for p in (q_lower, mod_lower) if p).strip()
    if not combined_q:
        rows = conn.execute(
            "SELECT name_lower, zh_lower, foulborn_lower, zh_foulborn_lower, slots_lower, data_json FROM uniques LIMIT 300"
        ).fetchall()
    else:
        fts_expr, _ = _build_fts_match(combined_q)
        row_limit = 600 if mod_lower else 300
        if fts_expr:
            rows = conn.execute(
                f"""
                SELECT u.name_lower, u.zh_lower, u.foulborn_lower, u.zh_foulborn_lower, u.slots_lower, u.data_json
                FROM uniques_fts f
                JOIN uniques u ON u.rowid = f.rowid
                WHERE uniques_fts MATCH ?
                LIMIT {row_limit}
                """,
                (fts_expr,),
            ).fetchall()
        else:
            probe = mod_lower or q_lower
            rows = conn.execute(
                f"""
                SELECT name_lower, zh_lower, foulborn_lower, zh_foulborn_lower, slots_lower, data_json
                FROM uniques
                WHERE instr(search_text_lower, ?) > 0
                LIMIT {row_limit}
                """,
                (probe,),
            ).fetchall()
        # If mod_filter had short (<3 char) or mixed CJK tokens missed by FTS5, fall back cleanly to full table scan
        if mod_lower and not rows:
            rows = conn.execute(
                "SELECT name_lower, zh_lower, foulborn_lower, zh_foulborn_lower, slots_lower, data_json FROM uniques"
            ).fetchall()

    q_tokens = [t for t in q_lower.split() if t] if q_lower else []
    scored: list[tuple[tuple[int, int, int, str], dict[str, Any] | str]] = []
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
        mod_tier = 0
        if mod_lower or ilvl is not None or (q_tokens and mod_lower):
            obj = json.loads(data_json)
            if ilvl is not None and obj.get("req_level") is not None and int(obj["req_level"]) > ilvl:
                continue
            if mod_lower:
                mod_tier, matched_lines = _unique_matching_mod_lines(obj, mod_tokens, mod_lower)
                if mod_tier < 0:
                    continue
                obj["matched_mods"] = matched_lines
            if q_tokens and mod_lower:
                hay_q = _fold_search_text(
                    " ".join(
                        str(x or "")
                        for x in (
                            obj.get("name"),
                            obj.get("zh_name"),
                            obj.get("base_type"),
                            obj.get("base_type_zh"),
                            obj.get("item_class"),
                        )
                    )
                )
                if not all(t in hay_q for t in q_tokens):
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
        scored.append(((mod_tier, tier, len(name_l), name_l), payload))

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
            for name, name_l in conn.execute(
                f"SELECT name, name_lower FROM {tbl} WHERE instr(name_lower, ?) > 0 ORDER BY length(name) LIMIT ?",
                (q_lower, limit),
            ).fetchall():
                match_type = "exact" if name_l == q_lower else ("prefix" if name_l.startswith(q_lower) else "substring")
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

    # 3. Folded substring fallback across indexed uniques/gems/bases/mods when translations had 0 hits
    if not scored:
        for tbl, cat, rank in (("uniques", "unique", 1), ("gems", "gem", 1), ("bases", "base", 1)):
            for name, name_l, zh_val, zh_l in conn.execute(
                f"""
                SELECT name, name_lower, zh_name, zh_lower
                FROM {tbl}
                WHERE instr(name_lower, ?) > 0 OR (zh_lower IS NOT NULL AND instr(zh_lower, ?) > 0)
                LIMIT 50
                """,
                (q_lower, q_lower),
            ).fetchall():
                zh_l_s = zh_l or ""
                if name_l == q_lower or (zh_l_s and zh_l_s == q_lower):
                    tier, mtype = 0, "exact"
                elif name_l.startswith(q_lower) or (zh_l_s and zh_l_s.startswith(q_lower)):
                    tier, mtype = 1, "prefix"
                else:
                    tier, mtype = 2, "substring"
                scored.append(
                    (
                        (tier, rank, len(name_l), name_l),
                        {"en": name, "zh": zh_val, "category": cat, "match_type": mtype},
                    )
                )
        if not scored:
            fts_expr, _ = _build_fts_match(q_lower)
            if fts_expr:
                mod_rows = conn.execute(
                    """
                    SELECT m.data_json
                    FROM mods_fts f
                    JOIN mods m ON m.rowid = f.rowid
                    WHERE mods_fts MATCH ?
                    LIMIT 30
                    """,
                    (fts_expr,),
                ).fetchall()
            else:
                mod_rows = conn.execute(
                    """
                    SELECT data_json
                    FROM mods
                    WHERE instr(stats_en_lower, ?) > 0 OR instr(stats_zh_lower, ?) > 0
                    LIMIT 30
                    """,
                    (q_lower, q_lower),
                ).fetchall()
            for (data_json,) in mod_rows:
                m_obj = json.loads(data_json)
                en_stat = (m_obj.get("stats") or [""])[0]
                zh_stat = next((s for s in (m_obj.get("stats_zh") or []) if s), None)
                if en_stat and zh_stat:
                    scored.append(
                        (
                            (2, 3, len(en_stat), _fold_search_text(en_stat)),
                            {"en": en_stat, "zh": zh_stat, "category": "mod", "match_type": "substring"},
                        )
                    )

    # 4. Partial CJK fallback (particle-stripped + high-overlap short-entity bigram/char match)
    #    when Steps 1-3 had 0 hits, so partial CN queries like "象牙塔" (-> "象牙之塔") or
    #    "寒冰法师" (-> "寒霜法师") return relevant core entities without lore pollution.
    if not scored and _CJK_RE.search(q_lower):
        q_cjk = _CJK_PARTICLE_RE.sub("", q_lower)
        cjk_chars = [ch for ch in q_cjk if _CJK_RE.match(ch)]
        if len(cjk_chars) >= 2:
            # 4a. Particle-stripped match (e.g. "象牙塔" matches "象牙之塔")
            p_rows = conn.execute(
                """
                SELECT en, en_lower, zh, zh_lower, category, cat_rank
                FROM translations
                WHERE cat_rank <= 2
                  AND instr(
                        replace(
                          replace(
                            replace(
                              replace(
                                replace(
                                  replace(zh_lower, '之', ''),
                                '的', ''),
                              '・', ''),
                            '·', ''),
                          '．', ''),
                        ' ', ''),
                        ?
                      ) > 0
                LIMIT 80
                """,
                (q_cjk,),
            ).fetchall()
            for en, en_l, zh, zh_l, cat, cat_rank in p_rows:
                zh_stripped = _CJK_PARTICLE_RE.sub("", zh_l)
                tier = 1 if zh_stripped == q_cjk else 2
                mtype = "substring" if zh_stripped == q_cjk else "partial"
                scored.append(
                    (
                        (tier, cat_rank, len(zh_l), en_l),
                        {"en": en, "zh": zh, "category": cat, "match_type": mtype},
                    )
                )

            # Skip 4b/4c when 4a already matched, or when query is too long for fuzzy short-entity lookup
            max_target_len = max(len(q_cjk) * 3, 12)
            q_char_set = set(cjk_chars)
            min_char_hits = max(2, int(-(-len(q_char_set) * 3 // 4)))  # ceil(0.75 * len)

            # 4b. CJK 2-char bigram overlap across core entities (strict >=75% char coverage & short targets)
            if not scored and len(q_cjk) <= 6:
                bigrams = list(dict.fromkeys(q_cjk[i : i + 2] for i in range(len(q_cjk) - 1)))
                bg_where = " OR ".join("instr(zh_lower, ?) > 0" for _ in bigrams)
                bg_rows = conn.execute(
                    f"""
                    SELECT en, en_lower, zh, zh_lower, category, cat_rank
                    FROM translations
                    WHERE cat_rank <= 2 AND ({bg_where})
                    ORDER BY length(zh_lower) ASC
                    LIMIT 400
                    """,
                    bigrams,
                ).fetchall()
                for en, en_l, zh, zh_l, cat, cat_rank in bg_rows:
                    if len(_CJK_PARTICLE_RE.sub("", zh_l)) > max_target_len:
                        continue
                    ch_hits = sum(1 for ch in q_char_set if ch in zh_l)
                    if ch_hits < min_char_hits or (ch_hits / len(q_char_set)) < 0.75:
                        continue
                    bg_hits = sum(1 for bg in bigrams if bg in zh_l)
                    rank_tier = 30 - min(bg_hits, 9) * 3 - min(ch_hits, 9)
                    scored.append(
                        (
                            (rank_tier, cat_rank, len(zh_l), en_l),
                            {"en": en, "zh": zh, "category": cat, "match_type": "partial"},
                        )
                    )

            # 4c. Character overlap fallback if still empty (strict >=75% char coverage & short targets)
            if not scored and len(q_cjk) <= 6:
                uniq_chars = list(dict.fromkeys(cjk_chars))
                ch_where = " OR ".join("instr(zh_lower, ?) > 0" for _ in uniq_chars)
                ch_rows = conn.execute(
                    f"""
                    SELECT en, en_lower, zh, zh_lower, category, cat_rank
                    FROM translations
                    WHERE cat_rank <= 2 AND ({ch_where})
                    ORDER BY length(zh_lower) ASC
                    LIMIT 400
                    """,
                    uniq_chars,
                ).fetchall()
                for en, en_l, zh, zh_l, cat, cat_rank in ch_rows:
                    if len(_CJK_PARTICLE_RE.sub("", zh_l)) > max_target_len:
                        continue
                    ch_hits = sum(1 for ch in q_char_set if ch in zh_l)
                    if ch_hits < min_char_hits or (ch_hits / len(q_char_set)) < 0.75:
                        continue
                    rank_tier = 50 - min(ch_hits, 9) * 5
                    scored.append(
                        (
                            (rank_tier, cat_rank, len(zh_l), en_l),
                            {"en": en, "zh": zh, "category": cat, "match_type": "partial"},
                        )
                    )

    scored.sort(key=lambda item: item[0])
    dedup_out: list[dict[str, Any]] = []
    seen_out: set[tuple[str, str | None]] = set()
    for _, item in scored:
        key = (_fold_search_text(item["en"]), item["zh"])
        if key not in seen_out:
            seen_out.add(key)
            dedup_out.append(item)
            if len(dedup_out) >= limit:
                break
    return dedup_out


def _dispatch_query(
    conn: sqlite3.Connection,
    kind_norm: str,
    q_folded: str,
    *,
    slot: str | None,
    ilvl: int | None,
    limit: int,
    zh_available: bool,
    mod_filter: str | None = None,
) -> list[dict[str, Any]]:
    if limit == 0:
        return []
    if kind_norm == "gem":
        return _query_gems(conn, q_folded, limit)
    if kind_norm == "unique":
        return _query_uniques(conn, q_folded, slot, ilvl, limit, mod_filter=mod_filter)
    if kind_norm == "base":
        return _query_bases(conn, q_folded, slot, ilvl, limit)
    if kind_norm == "mod":
        return _query_mods(conn, q_folded, slot, ilvl, limit)
    return _query_translate(conn, q_folded, limit, zh_available)


def _guardrail_scope_for_kind(kind_norm: str) -> Any:
    from .guardrails import GuardrailScope

    if kind_norm == "gem":
        return GuardrailScope.GEM_NAME
    if kind_norm == "mod":
        return GuardrailScope.STAT_QUERY
    return GuardrailScope.DATA_QUERY


# ---------------------------------------------------------------------------
# Player Slang Map, Zero-Empty-Result Suggestions & Entity Grounding (P0-2)
# ---------------------------------------------------------------------------

PLAYER_SLANG_MAP: dict[str, tuple[str, str]] = {
    # Iconic Uniques
    "法血": ("Mageblood", "unique"),
    "魔血": ("Mageblood", "unique"),
    "猎首": ("Headhunter", "unique"),
    "尼米斯": ("Nimis", "unique"),
    "原罪": ("Original Sin", "unique"),
    "百属": ("Astramentis", "unique"),
    "百属项链": ("Astramentis", "unique"),
    "全知": ("Crystallised Omniscience", "unique"),
    "全知项链": ("Crystallised Omniscience", "unique"),
    "星尘": ("Ashes of the Stars", "unique"),
    "骨灰盒": ("Ashes of the Stars", "unique"),
    "群星": ("Ashes of the Stars", "unique"),
    "象牙塔": ("The Ivory Tower", "unique"),
    "白袍": ("Tabula Rasa", "unique"),
    "三球鞋": ("Ralakesh's Impatience", "unique"),
    "拉利克斯": ("Ralakesh's Impatience", "unique"),
    "格挡盾": ("Svalinn", "unique"),
    "斯瓦林": ("Svalinn", "unique"),
    "塑界手": ("Shaper's Touch", "unique"),
    "信念之砧": ("Defiance of Destiny", "unique"),
    "女王头": ("Crown of the Inward Eye", "unique"),
    "禁断肉体": ("Forbidden Flesh", "unique"),
    "禁断火焰": ("Forbidden Flame", "unique"),
    "狮眼甲": ("Lioneye's Vision", "unique"),
    "龙牙": ("Replica Dragonfang's Flight", "unique"),
    "火衣": ("Cloak of Flame", "unique"),
    "电甲": ("Lightning Coil", "unique"),
    "万古之剑": ("The Sword of Ages", "unique"),
    "万古剑": ("The Sword of Ages", "unique"),
    "靛蓝头": ("Indigon", "unique"),
    "靛蓝": ("Indigon", "unique"),
    "不眨之眼": ("The Unblinking Eye", "unique"),
    "枯萎尖叫": ("Screams of the Desiccated", "unique"),
    "四季之侵": ("Sap of the Seasons", "unique"),
    "卫道之袍": ("Cloak of Defiance", "unique"),
    "恨意": ("Taste of Hate", "unique"),
    "艾许之面": ("Esh's Visage", "unique"),
    "薛朗的护身长袍": ("Shavronne's Wrappings", "unique"),
    "夏乌拉衣": ("Shavronne's Wrappings", "unique"),
    "大CI": ("Shavronne's Wrappings", "unique"),
    "小CI": ("Solaris Lorica", "unique"),
    "血肉溶解": ("Dissolution of the Flesh", "unique"),
    "卡斯普里": ("Cospri's Malice", "unique"),
    "冰剑": ("Cospri's Malice", "unique"),
    "雷锤": ("Mjölner", "unique"),
    "诗人之笔": ("The Poet's Pen", "unique"),
    "闪耀精华": ("Coruscating Elixir", "unique"),
    # Popular Skill & Support Gems
    "吟唱时施放": ("Cast while Channelling", "gem"),
    "引导时施放": ("Cast while Channelling", "gem"),
    "暴击时施放": ("Cast On Critical Strike", "gem"),
    "急冻": ("Hypothermia", "gem"),
    "元素集中": ("Elemental Focus", "gem"),
    "献祭": ("Immolate", "gem"),
    "启迪": ("Inspiration", "gem"),
    "大法师": ("Archmage", "gem"),
    "时空幻影": ("Temporal Mirage", "gem"),
    "倾泻火雨": ("Firestorm of Pelting", "gem"),
    "改造火雨": ("Firestorm of Pelting", "gem"),
    "碎焰之烈炎风暴": ("Firestorm of Pelting", "gem"),
    "陨星火雨": ("Firestorm of Meteors", "gem"),
    "陨石火雨": ("Firestorm of Meteors", "gem"),
    "沸燃": ("Volatile Dead of Seething", "gem"),
    "沸腾灵体火球": ("Volatile Dead of Seething", "gem"),
    "沸燃之灵体火球": ("Volatile Dead of Seething", "gem"),
    "逆转规则": ("Invert the Rules", "gem"),
    "寒冰法师": ("Frostmage", "gem"),
    "冰法师": ("Frostmage", "gem"),
    "暗夜血契": ("Dark Bargain", "gem"),
    "暗夜契约": ("Dark Bargain", "gem"),
    "黑暗交易": ("Dark Bargain", "gem"),
    "电弧": ("Arc", "gem"),
    "闪打": ("Flicker Strike", "gem"),
    "正火": ("Righteous Fire", "gem"),
    "龙卷": ("Tornado Shot", "gem"),
    "冰矛": ("Ice Spear", "gem"),
    "电打": ("Lightning Strike", "gem"),
    "冰霜新星": ("Ice Nova of Frostbolts", "gem"),
    "力爆": ("Kinetic Blast", "gem"),
    "力抽": ("Power Siphon", "gem"),
    "灵体火球": ("Volatile Dead", "gem"),
    "天雷": ("Ball Lightning", "gem"),
    "火雨": ("Firestorm", "gem"),
    "毒雨": ("Toxic Rain", "gem"),
    "毒瓶": ("Poisonous Concoction", "gem"),
    "旋风斩": ("Cyclone", "gem"),
    "忏悔烙印": ("Penance Brand of Dissipation", "gem"),
    "魔蛊": ("Hexblast", "gem"),
    "飞刃": ("Blade Vortex", "gem"),
    "刀雨": ("Bladefall", "gem"),
    "活栓": ("Lifetap", "gem"),
    "启蒙": ("Enlighten", "gem"),
    "赋予": ("Empower", "gem"),
    "增幅": ("Enhance", "gem"),
    "熔盾": ("Molten Shell", "gem"),
    "跃击": ("Leap Slam", "gem"),
    "冰闪": ("Frostblink", "gem"),
    "盾冲": ("Shield Charge", "gem"),
    "自动触发": ("Automation", "gem"),
    "自动增助": ("Autoexertion", "gem"),
    # Stats / Mechanics
    "法压": ("Spell Suppression", "stat"),
    "暴伤": ("Critical Strike Multiplier", "stat"),
    "暴击率": ("Critical Strike Chance", "stat"),
    "魔保": ("Mana Reservation Efficiency", "stat"),
    "魔力保留": ("Mana Reservation Efficiency", "stat"),
    "扑杀": ("Culling Strike", "stat"),
    "攻速": ("Attack Speed", "stat"),
    "施法速度": ("Cast Speed", "stat"),
    "移速": ("Movement Speed", "stat"),
    "元穿": ("Elemental Penetration", "stat"),
    "持续伤害加成": ("Damage over Time Multiplier", "stat"),
    "最大抗": ("Maximum Elemental Resistance", "stat"),
    "物转元": ("Physical Damage taken as", "stat"),
    "回血": ("Life Regeneration", "stat"),
    "秒回": ("Life Regenerated per Second", "stat"),
    # PoE2-CN -> PoE1-CN Keystone & Mechanic Mistranslation Aliases & 3.30 Keystones/Ascendancies
    "杂耍": ("Acrobatics", "keystone"),
    "心灵灭界": ("Mind Over Matter", "keystone"),
    "混沌接种": ("Chaos Inoculation", "keystone"),
    "异能魔力": ("Eldritch Battery", "keystone"),
    "喋血之刃": ("Bloodsoaked Blade", "keystone"),
    "守藏者": ("Reliquarian", "ascendancy"),
    "圣宗": ("Hierophant", "ascendancy"),
    "欺诈师": ("Trickster", "ascendancy"),
    "守望者": ("Warden", "ascendancy"),
    "聚魂": ("Polymath", "passive"),
    "博学者": ("Polymath", "passive"),
    "思维圣殿": ("Sanctuary of Thought", "passive"),
    "意识圣殿": ("Sanctuary of Thought", "passive"),
}

_SLANG_SORTED_KEYS: tuple[str, ...] = tuple(sorted(PLAYER_SLANG_MAP.keys(), key=len, reverse=True))

_GROUND_ENTITY_INDEX_CACHE: dict[str, dict[str, Any]] = {}
_GROUND_EN_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "with", "from", "this", "that", "can", "use", "get", "how", "why",
        "what", "does", "not", "but", "are", "was", "has", "have", "will", "should", "build",
        "item", "gem", "gems", "skill", "skills", "ring", "belt", "helm", "helmet", "body",
        "boots", "gloves", "shield", "staff", "wand", "bow", "claw", "sword", "axe", "mace",
        "flask", "jewel", "map", "mod", "mods", "dps", "ehp", "life", "mana", "crit", "hit",
        "dot", "cast", "time", "damage", "chance", "more", "less", "cold", "fire", "chaos",
        "physical", "lightning", "elemental", "support", "attack", "spell", "minion", "totem",
        "aura", "curse", "guard", "warcry", "charge", "charges", "socket", "sockets", "link",
        "links", "level", "quality", "poe", "poe1", "poe2", "pob", "still", "work", "works",
        "best", "good", "bad", "need", "want", "check", "test", "make", "take", "give", "one",
        "two", "all", "any", "into", "over", "under", "than", "then", "when", "where", "only",
        "pure", "base", "extra", "added", "increased", "reduced", "maximum", "minimum", "energy",
        "armour", "armor", "evasion", "block", "dodge", "bleed", "poison", "ignite", "shock",
        "chill", "freeze", "focus", "spirit", "charm", "rune", "waystone", "portal", "mark",
        "brand", "mine", "trap", "banner", "stance", "offering", "golem", "spectre", "zombie",
        "skeleton", "melee", "ranged", "proj", "projectile", "area", "aoe", "speed", "power",
        "frenzy", "endurance", "resist", "resistance", "resistances", "left", "right", "click",
        "instant", "manual", "auto", "trigger", "buff", "debuff", "boss", "pinnacle", "uber",
    }
)
_GROUND_ZH_STOPWORDS: frozenset[str] = frozenset(
    {
        "攻击", "施法", "法术", "闪电", "火焰", "冰霜", "混沌", "物理", "元素", "伤害", "暴击",
        "生命", "魔力", "护盾", "护甲", "闪避", "格挡", "抗性", "范围", "投射物", "近战",
        "辅助", "光环", "诅咒", "印记", "图腾", "召唤", "地雷", "陷阱", "烙印", "战吼",
        "药剂", "珠宝", "装备", "技能", "宝石", "天赋", "升华", "戒指", "项链", "腰带",
        "头盔", "胸甲", "手套", "鞋子", "盾牌", "武器", "单手", "双手", "弓", "法杖",
        "流血", "中毒", "点燃", "感电", "冰缓", "冰冻", "穿透", "贯穿", "转化", "保留",
    }
)


def resolve_player_slang(query: str) -> tuple[str, str] | None:
    """Resolve a PoE1 CN player slang term (exact match) into `(canonical_en, kind_hint)`."""
    if not query:
        return None
    q = query.strip()
    hit = PLAYER_SLANG_MAP.get(q)
    if hit is not None:
        return hit
    q_low = _fold_search_text(q)
    return PLAYER_SLANG_MAP.get(q_low)


def _suggest_similar(
    conn: sqlite3.Connection,
    kind_norm: str,
    q_lower: str,
    *,
    limit: int = 5,
    zh_available: bool = True,
) -> list[dict[str, Any]]:
    """Return up to `limit` fuzzy/token/difflib suggestions ONLY when primary query yields 0 hits."""
    import difflib

    if not q_lower or limit <= 0:
        return []

    is_cjk = bool(_CJK_RE.search(q_lower))
    q_tokens = [t for t in re.split(r"[\s_\-/]+", q_lower) if len(t) >= 2]

    candidates: list[tuple[str, str | None, str]] = []  # (name_en, zh_name, key_for_match)
    if kind_norm == "gem":
        rows = conn.execute("SELECT name, name_lower, zh_name, zh_lower FROM gems").fetchall()
        for nm, nm_l, zh, zh_l in rows:
            candidates.append((nm, zh, (zh_l or nm_l) if is_cjk else nm_l))
    elif kind_norm == "unique":
        rows = conn.execute("SELECT name, name_lower, zh_name, zh_lower FROM uniques").fetchall()
        for nm, nm_l, zh, zh_l in rows:
            candidates.append((nm, zh, (zh_l or nm_l) if is_cjk else nm_l))
    elif kind_norm == "base":
        rows = conn.execute("SELECT name, name_lower, zh_name, zh_lower FROM bases").fetchall()
        for nm, nm_l, zh, zh_l in rows:
            candidates.append((nm, zh, (zh_l or nm_l) if is_cjk else nm_l))
    elif kind_norm == "mod":
        # Sample top mod stat templates matching any token/char for fast suggestions
        if is_cjk:
            chars = list(dict.fromkeys(ch for ch in q_lower if _CJK_RE.match(ch)))[:4]
            if chars:
                where = " OR ".join("instr(stats_zh_lower, ?) > 0" for _ in chars)
                mrows = conn.execute(
                    f"SELECT id, stats_en_lower, stats_zh_lower, data_json FROM mods WHERE {where} LIMIT 250",
                    chars,
                ).fetchall()
            else:
                mrows = []
        else:
            toks = [t for t in q_tokens if len(t) >= 3][:3] or [q_lower[:3]]
            where = " OR ".join("instr(stats_en_lower, ?) > 0" for _ in toks)
            mrows = conn.execute(
                f"SELECT id, stats_en_lower, stats_zh_lower, data_json FROM mods WHERE {where} LIMIT 250",
                toks,
            ).fetchall()
        seen_mod_txt: set[str] = set()
        for _, en_l, zh_l, dj in mrows:
            obj = json.loads(dj)
            en_s = (obj.get("stats") or [""])[0]
            zh_s = next((s for s in (obj.get("stats_zh") or []) if s), None)
            if en_s and en_s not in seen_mod_txt:
                seen_mod_txt.add(en_s)
                candidates.append((en_s, zh_s, (zh_l or en_l) if is_cjk else en_l))
    else:  # translate
        for tbl in ("gems", "uniques", "bases"):
            rows = conn.execute(f"SELECT name, name_lower, zh_name, zh_lower FROM {tbl}").fetchall()
            for nm, nm_l, zh, zh_l in rows:
                candidates.append((nm, zh, (zh_l or nm_l) if is_cjk else nm_l))
        if zh_available and not candidates:
            rows = conn.execute("SELECT en, en_lower, zh, zh_lower FROM translations WHERE cat_rank <= 2 LIMIT 2000").fetchall()
            for en, en_l, zh, zh_l in rows:
                candidates.append((en, zh, zh_l if is_cjk else en_l))

    if not candidates:
        return []

    scored: list[tuple[float, str, str | None]] = []
    seen_en: set[str] = set()
    q_chars = set(ch for ch in q_lower if _CJK_RE.match(ch)) if is_cjk else set()

    for en, zh, key_txt in candidates:
        if not key_txt or en in seen_en:
            continue
        score = 0.0
        if is_cjk and q_chars:
            overlap = sum(1 for ch in q_chars if ch in key_txt)
            if overlap == 0:
                continue
            score = (overlap / len(q_chars)) * 0.7 + difflib.SequenceMatcher(None, q_lower, key_txt).ratio() * 0.3
        else:
            tok_hits = sum(1 for t in q_tokens if t in key_txt) if q_tokens else 0
            ratio = difflib.SequenceMatcher(None, q_lower, key_txt).ratio()
            if tok_hits == 0 and ratio < 0.45:
                continue
            score = ratio * 0.75 + (tok_hits / max(1, len(q_tokens))) * 0.25
        if score >= 0.40:
            seen_en.add(en)
            scored.append((score, en, zh))

    scored.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    return [
        {"name": en, "zh_name": zh, "score": round(sc, 3)}
        for sc, en, zh in scored[:limit]
    ]


def _build_zero_result_hint(kind_norm: str, query: str, suggestions: list[dict[str, Any]]) -> str:
    if suggestions:
        top_names = ", ".join(
            f"{s['name']} ({s['zh_name']})" if s.get("zh_name") else s["name"]
            for s in suggestions[:3]
        )
        return (
            f"0 exact {kind_norm} matches for '{query}'. "
            f"Did you mean: {top_names}? Try re-querying with one of those names or `pob data translate \"{query}\"`."
        )
    return (
        f"0 exact {kind_norm} matches for '{query}'. "
        f"Verify the exact PoE1 3.30 name via `pob data translate \"{query}\"` or `pob poedb \"{query}\"` — do NOT guess stats from memory."
    )


def _format_gem_fact_card(gem_obj: dict[str, Any], *, matched_by: str | None = None) -> dict[str, Any]:
    lvls = gem_obj.get("levels") or {}
    lv20 = lvls.get("20") or lvls.get("1") or (next(iter(lvls.values())) if lvls else {})
    q_stats = gem_obj.get("quality_stats") or []
    card: dict[str, Any] = {
        "kind": "gem",
        "name": gem_obj.get("display_name") or gem_obj.get("name"),
        "zhName": gem_obj.get("zh_name"),
        "isSupport": bool(gem_obj.get("is_support")),
        "color": gem_obj.get("color"),
        "tags": gem_obj.get("tag_string"),
        "castTime": gem_obj.get("cast_time"),
        "critChance": gem_obj.get("crit_chance") or lv20.get("critChance"),
        "damageEffectiveness": gem_obj.get("damage_effectiveness") or lv20.get("damageEffectiveness"),
        "manaMultiplier": gem_obj.get("mana_multiplier"),
        "lv20Cost": lv20.get("cost"),
        "lv20Stats": dict(list((lv20.get("stats") or {}).items())[:6]),
        "quality20": [
            {
                "stat": q.get("stat"),
                "perQuality": q.get("per_quality"),
                "at20": q.get("at_20_quality"),
                "at40": round(float(q.get("per_quality") or 0.0) * 40, 4),
            }
            for q in q_stats[:3]
        ],
    }
    variants = gem_obj.get("variants") or []
    if variants:
        card["variants"] = [
            f"{v.get('name')} ({v['zh_name']})" if v.get("zh_name") else str(v.get("name"))
            for v in variants[:4]
        ]
    if matched_by:
        card["matchedBy"] = matched_by
    return card


def _format_unique_fact_card(u_obj: dict[str, Any], *, matched_by: str | None = None) -> dict[str, Any]:
    implicits = [MOD_TAG_PREFIX_RE.sub("", s).strip() for s in (u_obj.get("implicits") or [])[:3]]
    explicits = [MOD_TAG_PREFIX_RE.sub("", s).strip() for s in (u_obj.get("explicits") or [])[:8]]
    card: dict[str, Any] = {
        "kind": "unique",
        "name": u_obj.get("name"),
        "zhName": u_obj.get("zh_name"),
        "baseType": u_obj.get("base_type"),
        "baseTypeZh": u_obj.get("base_type_zh"),
        "itemClass": u_obj.get("item_class"),
        "reqLevel": u_obj.get("req_level"),
        "implicits": [s for s in implicits if s],
        "explicits": [s for s in explicits if s],
    }
    if matched_by:
        card["matchedBy"] = matched_by
    return card


_REMOVED_LEGACY_GEM_NAMES_LOWER: frozenset[str] = frozenset(
    {
        "divine blessing",
        "divine blessing support",
        "ancestral protector",
        "ancestral warchief",
        "vaal ancestral warchief",
        "call to arms",
    }
)


def _get_ground_entity_index(conn: sqlite3.Connection, db_key: str) -> dict[str, Any]:
    cached = _GROUND_ENTITY_INDEX_CACHE.get(db_key)
    if cached is not None:
        return cached

    zh_entries: list[tuple[str, str, int, str]] = []  # (zh_compact, kind, rowid, name)
    en_entries: list[tuple[str, str, int, str]] = []  # (name_lower, kind, rowid, name)

    for kind, tbl in (("gem", "gems"), ("unique", "uniques")):
        rows = conn.execute(f"SELECT rowid, name, name_lower, zh_lower FROM {tbl}").fetchall()
        for rid, name, name_l, zh_l in rows:
            if kind == "gem" and name_l in _REMOVED_LEGACY_GEM_NAMES_LOWER:
                continue
            if name_l and len(name_l) >= 3 and name_l not in _GROUND_EN_STOPWORDS:
                en_entries.append((name_l, kind, int(rid), name))
            if zh_l:
                zh_clean = re.sub(r"\s*(?:\(辅\)|（辅）|\(强辅\)|（强辅）|辅助)$", "", zh_l).strip()
                zh_compact = re.sub(r"[\s·・.．:：()（）]", "", zh_clean)
                if len(zh_compact) >= 2 and zh_compact not in _GROUND_ZH_STOPWORDS:
                    zh_entries.append((zh_compact, kind, int(rid), name))

    # Longest-first ensures specific names (e.g. "Vaal Molten Shell", "投射之漩涡") win over shorter substrings
    zh_entries.sort(key=lambda x: (-len(x[0]), 0 if x[1] == "gem" else 1, x[3]))
    en_entries.sort(key=lambda x: (-len(x[0]), 0 if x[1] == "gem" else 1, x[3]))

    idx = {"zh": zh_entries, "en": en_entries}
    _GROUND_ENTITY_INDEX_CACHE[db_key] = idx
    return idx


def lookup_fact_cards_for_build(
    gem_names: list[str],
    unique_names: list[str],
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Fetch compact live 3.30 gem & unique fact cards from SQLite for `pob diagnose` (<5ms)."""
    db_path = ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
    conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        gem_cards: list[dict[str, Any]] = []
        seen_gems: set[str] = set()
        for raw_g in gem_names:
            g_clean = re.sub(r"\s+support$", "", str(raw_g or "").strip(), flags=re.IGNORECASE)
            if not g_clean:
                continue
            g_low = _fold_search_text(g_clean)
            row = conn.execute(
                "SELECT data_json FROM gems WHERE name_lower = ? OR display_name_lower = ? OR zh_lower = ? LIMIT 1",
                (g_low, g_low, g_low),
            ).fetchone()
            if row is None:
                # Try stripping " (辅)" suffix on zh or prefix lookup
                hits = _query_gems(conn, g_low, 1)
                if hits:
                    obj = hits[0]
                else:
                    continue
            else:
                obj = json.loads(row[0])
            cname = str(obj.get("name") or "")
            if cname and cname not in seen_gems:
                seen_gems.add(cname)
                gem_cards.append(_format_gem_fact_card(obj))

        unique_cards: list[dict[str, Any]] = []
        seen_uniques: set[str] = set()
        for raw_u in unique_names:
            u_clean = str(raw_u or "").strip()
            if not u_clean:
                continue
            u_low = _fold_search_text(u_clean)
            row = conn.execute(
                "SELECT data_json FROM uniques WHERE name_lower = ? OR zh_lower = ? LIMIT 1",
                (u_low, u_low),
            ).fetchone()
            if row is None:
                continue
            obj = json.loads(row[0])
            uname = str(obj.get("name") or "")
            if uname and uname not in seen_uniques:
                seen_uniques.add(uname)
                unique_cards.append(_format_unique_fact_card(obj))

        return {
            "pobVersion": meta.get("pob_version", "unknown"),
            "dataVersion": meta.get("data_version", "unknown"),
            "gems": gem_cards,
            "uniques": unique_cards,
        }
    finally:
        conn.close()


def ground_text_entities(
    text: str,
    *,
    limit: int = 8,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Scan arbitrary CN/EN player prompt text (<15ms warm) against live 3.30 SQLite + guardrails + patch traps.

    Returns live gem/unique fact cards, resolved player slang, PoE2 cross-game warnings,
    3.23-3.25 legacy patch warnings, same-name false friends, and a ready-to-inject Markdown `contextBlock`.
    Never raises on PoE2/legacy traps — reports them cleanly for pre-turn LLM grounding.
    """
    from .guardrails import normalize_guardrail_text, normalize_zh_compact, scan_text_traps

    t0 = time.perf_counter()
    raw = str(text or "").strip()
    limit = max(0, int(limit))

    traps = scan_text_traps(raw)

    db_path = ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
    conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        pob_ver = meta.get("pob_version", "unknown")
        data_ver = meta.get("data_version", "unknown")

        resolved_slang: list[dict[str, str]] = []
        entities: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()

        if raw and limit > 0:
            # 1. Resolve player slang mentions in text (longest-first)
            for slang in _SLANG_SORTED_KEYS:
                if slang in raw:
                    canon_en, kind_hint = PLAYER_SLANG_MAP[slang]
                    resolved_slang.append({"slang": slang, "canonical": canon_en, "kind": kind_hint})
                    if len(entities) >= limit:
                        continue
                    c_low = _fold_search_text(canon_en)
                    if kind_hint == "unique":
                        row = conn.execute(
                            "SELECT data_json FROM uniques WHERE name_lower = ? LIMIT 1", (c_low,)
                        ).fetchone()
                        if row:
                            obj = json.loads(row[0])
                            ekey = ("unique", str(obj.get("name")))
                            if ekey not in seen_keys:
                                seen_keys.add(ekey)
                                entities.append(_format_unique_fact_card(obj, matched_by=f"slang:{slang}"))
                    elif kind_hint == "gem":
                        row = conn.execute(
                            "SELECT data_json FROM gems WHERE name_lower = ? OR display_name_lower = ? LIMIT 1",
                            (c_low, c_low),
                        ).fetchone()
                        if row:
                            obj = json.loads(row[0])
                            ekey = ("gem", str(obj.get("name")))
                            if ekey not in seen_keys:
                                seen_keys.add(ekey)
                                entities.append(_format_gem_fact_card(obj, matched_by=f"slang:{slang}"))

            # 2. Scan live SQLite gem & unique names (ZH & EN word-bounded)
            if len(entities) < limit:
                idx = _get_ground_entity_index(conn, f"{db_path}:{pob_ver}:{data_ver}")
                has_cjk = bool(_CJK_RE.search(raw))
                text_zh_compact = normalize_zh_compact(raw) if has_cjk else ""
                text_en_norm = normalize_guardrail_text(raw)
                has_ascii = bool(re.search(r"[a-z]{3,}", text_en_norm))

                matched_refs: list[tuple[str, int, str]] = []  # (kind, rowid, matched_by)
                if has_cjk and text_zh_compact:
                    covered_zh: list[str] = []
                    for zh_c, kind, rid, name in idx["zh"]:
                        if len(entities) + len(matched_refs) >= limit:
                            break
                        if zh_c in text_zh_compact:
                            if any(zh_c in prev for prev in covered_zh):
                                continue
                            ekey = (kind, name)
                            if ekey not in seen_keys:
                                seen_keys.add(ekey)
                                covered_zh.append(zh_c)
                                matched_refs.append((kind, rid, f"zh:{zh_c}"))

                if has_ascii and len(entities) + len(matched_refs) < limit:
                    for name_l, kind, rid, name in idx["en"]:
                        if len(entities) + len(matched_refs) >= limit:
                            break
                        if name_l not in text_en_norm:
                            continue
                        if not re.search(rf"(?<![a-z0-9_'-]){re.escape(name_l)}(?![a-z0-9_'-])", text_en_norm):
                            continue
                        ekey = (kind, name)
                        if ekey not in seen_keys:
                            seen_keys.add(ekey)
                            matched_refs.append((kind, rid, f"en:{name_l}"))

                for kind, rid, mby in matched_refs:
                    tbl = "gems" if kind == "gem" else "uniques"
                    row = conn.execute(f"SELECT data_json FROM {tbl} WHERE rowid = ?", (rid,)).fetchone()
                    if not row:
                        continue
                    obj = json.loads(row[0])
                    if kind == "gem":
                        entities.append(_format_gem_fact_card(obj, matched_by=mby))
                    else:
                        entities.append(_format_unique_fact_card(obj, matched_by=mby))
    finally:
        conn.close()

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    env = {
        "game": "poe1",
        "patch": "3.30",
        "pobVersion": pob_ver,
        "dataVersion": data_ver,
    }

    # Build concise, high-signal Markdown block for direct pre-turn prompt injection
    ctx_lines = [f"[Env: PoE1 3.30 | PoB {pob_ver} | Data {data_ver}]"]
    for w in traps["warnings"]:
        ctx_lines.append(f"- {w['tag']} `{w['trigger']}`: {w['message']}")
    if resolved_slang:
        slang_bits = ", ".join(f"{s['slang']}={s['canonical']}" for s in resolved_slang[:6])
        ctx_lines.append(f"- Slang: {slang_bits}")
    for ent in entities:
        zh_p = f" ({ent['zhName']})" if ent.get("zhName") else ""
        if ent["kind"] == "gem":
            bits = []
            if ent.get("isSupport"):
                bits.append("Support")
            if ent.get("castTime") is not None:
                bits.append(f"Cast={ent['castTime']}s")
            if ent.get("tags"):
                bits.append(f"Tags={ent['tags']}")
            st_items = list((ent.get("lv20Stats") or {}).items())[:4]
            if st_items:
                bits.append("Lv20: " + ", ".join(f"{k}={v}" for k, v in st_items))
            ctx_lines.append(f"- [Gem] {ent['name']}{zh_p}: " + " | ".join(bits))
        else:
            mods_preview = "; ".join((ent.get("explicits") or [])[:4])
            ctx_lines.append(
                f"- [Unique] {ent['name']}{zh_p} ({ent.get('baseType') or '-'}): {mods_preview}"
            )

    return {
        "env": env,
        "query": raw,
        "entities": entities,
        "resolvedSlang": resolved_slang,
        "poe2Traps": traps["poe2Traps"],
        "patchTraps": traps["patchTraps"],
        "falseFriends": traps["falseFriends"],
        "warnings": traps["warnings"],
        "hasAlerts": bool(traps["warnings"]),
        "contextBlock": "\n".join(ctx_lines),
        "elapsedMs": elapsed_ms,
    }


SKILL_GEM_QUALITY_SCOPE_RULE = (
    '"+X% to Quality of all Skill Gems" (所有主动技能石品质，如 Ashes of the Stars / 3.30 柳条人肢体) '
    "applies ONLY to Active Skill Gems (`grants_active_skill`), NOT Support Gems (e.g. Archmage / Spell Echo)! "
    'Only "+X% to Quality of Socketed [Support] Gems" (e.g. Foulborn Skin of the Loyal/Lords, Crest of Desire, Enhance Support) '
    "applies to Support Gems."
)

_QUALITY_STAT_ZH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "projectile": ("投射物", "箭矢", "飞弹", "弹幕"),
    "chain": ("连锁", "弹射", "链"),
    "split": ("分裂",),
    "pierce": ("穿透",),
    "fork": ("分叉",),
    "final": ("更多", "独立乘区", "more"),
    "more": ("更多", "独立乘区", "final"),
    "cooldown": ("冷却", "cdr"),
    "minion": ("召唤", "召唤物", "魔像", "僵尸", "骷髅", "灵体"),
    "totem": ("图腾",),
    "stage": ("阶段", "层数", "叠层", "脉冲", "爆发"),
    "pulse": ("脉冲", "冲击", "波"),
    "beam": ("光束", "射线"),
    "wall": ("墙", "冰墙"),
    "repeat": ("重复", "回响", "次数"),
    "additional": ("额外", "数量", "上限"),
    "maximum": ("上限", "最大"),
    "double_damage": ("双倍", "双倍伤害"),
    "critical": ("暴击", "暴伤"),
    "penetration": ("元素穿透", "抗性穿透"),
    "area": ("范围", "aoe"),
    "speed": ("攻速", "施法速度", "速度"),
    "brand": ("烙印",),
    "mine": ("地雷",),
    "trap": ("陷阱",),
    "corpse": ("灵柩", "尸体"),
    "wither": ("凋零",),
    "bleeding": ("流血",),
    "poison": ("中毒",),
    "ignite": ("点燃",),
    "shock": ("感电",),
    "reservation": ("保留",),
    "aura": ("光环",),
    "curse": ("诅咒",),
    "quality": ("品质",),
}

_INTEGER_STEP_STAT_TOKENS: tuple[str, ...] = (
    "projectile",
    "number_of_chains",
    "chain_count",
    "chains_+",
    "split",
    "pierce",
    "fork",
    "additional_",
    "maximum_number",
    "max_number",
    "maximum_stages",
    "max_stages",
    "stage_count",
    "stages_+",
    "repeat",
    "pulse",
    "burst",
    "beam",
    "target",
    "wall",
    "totem",
    "minion",
    "golem",
    "zombie",
    "skeleton",
    "spectre",
    "sentinel",
    "spider",
    "storm",
    "geyser",
    "spire",
    "brand",
    "seal",
    "orb",
    "arrow",
    "bolt",
    "blade",
    "wave",
    "shard",
    "strike",
    "corpse",
    "limit",
    "count",
    "clone",
    "mirror",
    "trap",
    "mine",
    "charge",
    "link",
    "bounce",
    "explosion",
    "impact",
    "arc",
    "radius",
    "metres",
)

_INTEGER_STEP_EXCLUDE_TOKENS: tuple[str, ...] = (
    "chance_%",
    "%_chance",
    "damage_+%",
    "speed_+%",
    "duration_+%",
    "radius_+%",
    "area_of_effect_+%",
    "effect_+%",
    "cost_+%",
    "life_+%",
    "multiplier",
    "effectiveness",
    "accuracy",
    "critical_strike",
    "threshold",
    "distance",
    "range",
    "velocity",
    "delay",
)

_HIGH_VALUE_QUALITY_TOKENS: tuple[str, ...] = (
    "cooldown_speed",
    "cooldown_recovery",
    "double_damage",
    "triple_damage",
    "penetration",
    "base_critical_strike_chance",
    "wither",
    "impale",
    "aura_effect",
    "curse_effect",
    "mana_reservation",
    "action_speed",
    "elusive",
    "fortify",
    "exposure",
    "culling",
    "trigger",
)


def _humanize_quality_stat(stat_key: str, per_q: float) -> str:
    """Turn PoB internal gem quality stat keys into readable EN/ZH descriptions."""
    s = str(stat_key or "").strip()
    low = s.lower()
    is_final = "_+%_final" in low or low.endswith("_final")
    is_pct = "+%" in low or "_%_" in low or low.endswith("_%")
    cleaned = (
        low.replace("_+%_final", "")
        .replace("_final", "")
        .replace("_+%", "")
        .replace("_+", "")
        .replace("active_skill_", "")
        .replace("support_", "")
        .replace("base_", "")
        .replace("_", " ")
        .strip()
    )
    val20 = round(per_q * 20, 3)
    val40 = round(per_q * 40, 3)
    sign = "+" if per_q >= 0 else ""

    def _fmt_v(v: float) -> str:
        return str(int(v)) if float(v).is_integer() else f"{v:g}"

    if is_final:
        return f"{sign}{_fmt_v(val20)}% -> {sign}{_fmt_v(val40)}% more {cleaned} (独立乘区)"
    if is_pct:
        return f"{sign}{_fmt_v(val20)}% -> {sign}{_fmt_v(val40)}% {cleaned}"
    return f"{sign}{_fmt_v(val20)} -> {sign}{_fmt_v(val40)} {cleaned}"


def _enrich_gem_quality_stats(results: list[dict[str, Any]], kind_norm: str) -> None:
    """Attach `at_40_quality` and human-readable `label` to every gem's `quality_stats` without schema migration."""
    if kind_norm != "gem":
        return
    for r in results:
        if not isinstance(r, dict):
            continue
        q_list = r.get("quality_stats")
        if not isinstance(q_list, list):
            continue
        cleaned_q: list[dict[str, Any]] = []
        for q in q_list:
            if not isinstance(q, dict):
                continue
            st_low = str(q.get("stat") or "").strip().lower()
            if st_low.startswith("quality_display_"):
                continue
            per_q = q.get("per_quality")
            if isinstance(per_q, (int, float)):
                q["at_40_quality"] = round(float(per_q) * 40, 4)
                if not q.get("label"):
                    q["label"] = _humanize_quality_stat(str(q.get("stat") or ""), float(per_q))
            cleaned_q.append(q)
        r["quality_stats"] = cleaned_q


def _classify_quality_stat(stat_key: str, per_q: float) -> tuple[str, int, str]:
    """Classify a quality stat into `(category, priority_score, breakpoint_note)` for 20% -> 40%+ analysis."""
    import math

    low = str(stat_key or "").lower()
    if low.startswith("quality_display_"):
        return ("other", 0, "")
    abs_pq = abs(float(per_q))
    if abs_pq <= 0:
        return ("other", 0, "")

    at20_raw = abs_pq * 20.0
    at40_raw = abs_pq * 40.0

    # 1. Integer-step mechanic breakpoints (e.g. +0.05 projectiles/qual -> +1@20%, +2@40%; +0.1 chains -> +2@20%, +4@40%)
    is_step_candidate = (
        abs_pq <= 0.5
        and "+%" not in low
        and "_%_" not in low
        and not low.endswith("_%")
        and "_final" not in low
        and not any(ex in low for ex in _INTEGER_STEP_EXCLUDE_TOKENS)
        and any(tok in low for tok in _INTEGER_STEP_STAT_TOKENS)
    )
    if is_step_candidate:
        int20 = int(math.floor(at20_raw + 1e-9))
        int40 = int(math.floor(at40_raw + 1e-9))
        if int40 > int20:
            step_q = round(1.0 / abs_pq, 1)
            step_str = str(int(step_q)) if float(step_q).is_integer() else f"{step_q:g}"
            core_bonus = 30 if any(k in low for k in ("number_of_projectiles", "number_of_chains", "projectile_number_to_split", "maximum_number_of_stages")) else (
                20 if any(k in low for k in ("projectile", "chain", "split", "additional_", "maximum_number", "repeat", "stage", "pulse", "beam", "totem", "minion", "brand", "seal", "bounce", "wall")) else 10
            )
            return (
                "integer_step",
                100 + core_bonus + min(20, (int40 - int20) * 5),
                f"+1 per {step_str}% qual (@20%={int20}, @40%={int40})",
            )

    # 2. Independent `more` (`_final`) multiplier scaling
    if "_+%_final" in low or low.endswith("_final"):
        delta = round(at40_raw - at20_raw, 2)
        if delta >= 4.0:
            bonus = 20 if "damage" in low or "speed" in low or "explosion" in low else 8
            return (
                "more_multiplier",
                85 + bonus + min(25, int(delta // 5)),
                f"more multiplier: {at20_raw:g}% (@20%) -> {at40_raw:g}% (@40%) [+{delta:g}%]",
            )

    # 3. High-value non-generic scaling (CDR, double damage, penetration, aura/curse effect, base crit, wither)
    if any(tok in low for tok in _HIGH_VALUE_QUALITY_TOKENS):
        delta = round(at40_raw - at20_raw, 2)
        return (
            "high_value",
            60 + min(20, int(delta // 2)),
            f"@20%={at20_raw:g} -> @40%={at40_raw:g}",
        )

    return ("standard", 10, f"@20%={at20_raw:g} -> @40%={at40_raw:g}")


def query_gem_quality_breakpoints(
    search_quality: str | None = None,
    *,
    active_only: bool = False,
    limit: int = 30,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Scan local 3.30 SQLite gems (<30ms) for 20% -> 40%+ quality breakpoints or quality-stat search."""
    from .guardrails import GuardrailScope, check_input

    t0 = time.perf_counter()
    q_clean = str(search_quality or "").strip()
    warnings = check_input(q_clean, scope=GuardrailScope.DATA_QUERY, expected_game="poe1") if q_clean else []
    q_low = _fold_search_text(q_clean)
    limit = max(1, int(limit))

    # Expand bilingual search terms for `--search-quality`
    search_terms: list[str] = []
    if q_low:
        search_terms.append(q_low)
        for en_tok, zh_list in _QUALITY_STAT_ZH_KEYWORDS.items():
            if q_low == en_tok or en_tok in q_low or any(z in q_low or q_low in z for z in zh_list):
                if en_tok not in search_terms:
                    search_terms.append(en_tok)
                for z in zh_list:
                    if z not in search_terms:
                        search_terms.append(z)

    db_path = ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
    conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        where_clauses = ["is_item_skill = 0"]
        if active_only:
            where_clauses.append("is_support = 0")
        sql = f"SELECT name, name_lower, zh_name, is_support, is_vaal, is_transfigured, is_imbued, color, tag_string, data_json FROM gems WHERE {' AND '.join(where_clauses)}"
        rows = conn.execute(sql).fetchall()

        matches: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
        for name, name_l, zh_name, is_sup, is_vaal, is_trans, is_imbued, color, tag_str, dj in rows:
            if name_l in _REMOVED_LEGACY_GEM_NAMES_LOWER:
                continue
            obj = json.loads(dj)
            q_stats = obj.get("quality_stats") or []
            if not q_stats:
                continue

            best_score = -1
            matched_q: list[dict[str, Any]] = []
            for q in q_stats:
                st = str(q.get("stat") or "")
                pq = q.get("per_quality")
                if not st or not isinstance(pq, (int, float)) or float(pq) == 0.0:
                    continue
                pq_f = float(pq)
                at20 = round(pq_f * 20, 4)
                at40 = round(pq_f * 40, 4)
                label = _humanize_quality_stat(st, pq_f)
                cat, score, bp_note = _classify_quality_stat(st, pq_f)

                if search_terms:
                    hay = _fold_search_text(f"{name} {zh_name or ''} {st} {label} {bp_note}")
                    if not any(t in hay for t in search_terms):
                        continue
                else:
                    # Breakpoints mode: only surface meaningful integer-step, independent more, or high-value scaling
                    if cat not in ("integer_step", "more_multiplier", "high_value"):
                        continue

                if score > best_score:
                    best_score = score
                matched_q.append(
                    {
                        "stat": st,
                        "label": label,
                        "category": cat,
                        "perQuality": pq_f,
                        "at20": at20,
                        "at40": at40,
                        "breakpointNote": bp_note,
                    }
                )

            if not matched_q:
                continue

            # Prefer non-Vaal base/transfigured gems slightly ahead of Vaal duplicates with identical quality stat
            dup_penalty = 1 if is_vaal else 0
            # Sort matched_q so highest-scoring quality stat is first
            matched_q.sort(key=lambda item: (-_classify_quality_stat(item["stat"], item["perQuality"])[1], item["stat"]))
            top_cat = matched_q[0]["category"]
            sort_key = (-best_score, dup_penalty, int(is_sup), name_l)
            matches.append(
                (
                    sort_key,
                    {
                        "name": obj.get("display_name") or name,
                        "zhName": zh_name,
                        "isSupport": bool(is_sup),
                        "isTransfigured": bool(is_trans),
                        "isVaal": bool(is_vaal),
                        "color": color,
                        "tags": tag_str,
                        "topCategory": top_cat,
                        "score": best_score,
                        "qualityStats": matched_q,
                    },
                )
            )

        matches.sort(key=lambda item: item[0])
        if not search_terms and limit >= 6:
            # Stratify overview breakpoints across integer_step / more_multiplier / high_value
            # so iconic independent `more` multipliers (e.g. Zenith, Glacial Cascade, Lacerate,
            # Viper Strike of the Mamba, Blade Flurry, Flame Surge) and CDR/high-value scaling
            # (e.g. Summon Holy Relic of Conviction) aren't drowned out by 150+ integer_step gems.
            by_cat: dict[str, list[dict[str, Any]]] = {
                "integer_step": [],
                "more_multiplier": [],
                "high_value": [],
            }
            for _, item in matches:
                cat_k = item.get("topCategory")
                if cat_k in by_cat:
                    by_cat[cat_k].append(item)
            q_int = max(2, int(round(limit * 0.45)))
            q_more = max(2, int(round(limit * 0.40)))
            q_hv = max(1, limit - q_int - q_more)
            chosen: list[dict[str, Any]] = []
            picked_names: set[str] = set()
            for cat_k, quota in (("integer_step", q_int), ("more_multiplier", q_more), ("high_value", q_hv)):
                for it in by_cat[cat_k][:quota]:
                    if it["name"] not in picked_names:
                        chosen.append(it)
                        picked_names.add(it["name"])
            if len(chosen) < limit:
                for _, it in matches:
                    if it["name"] not in picked_names:
                        chosen.append(it)
                        picked_names.add(it["name"])
                        if len(chosen) >= limit:
                            break
            top_results = chosen[:limit]
        else:
            top_results = [item[1] for item in matches[:limit]]
    finally:
        conn.close()

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    out: dict[str, Any] = {
        "kind": "gem_quality",
        "mode": "search_quality" if q_clean else "quality_breakpoints",
        "query": q_clean or None,
        "activeOnly": bool(active_only),
        "pobVersion": meta.get("pob_version", "unknown"),
        "dataVersion": meta.get("data_version", "unknown"),
        "qualityRule": SKILL_GEM_QUALITY_SCOPE_RULE,
        "totalMatched": len(matches),
        "count": len(top_results),
        "elapsedMs": elapsed_ms,
        "results": top_results,
    }
    if warnings:
        out["warnings"] = [w.to_dict() for w in warnings]
    return out


def _annotate_legacy_gem_rows(results: list[dict[str, Any]], kind_norm: str) -> None:
    """Explicitly tag retained legacy-only PoB `Gems.lua` rows (e.g. Divine Blessing) so weak models never misread them as active 3.30 gems."""
    if kind_norm != "gem":
        return
    _enrich_gem_quality_stats(results, kind_norm)
    for r in results:
        if not isinstance(r, dict):
            continue
        nm_low = str(r.get("name") or "").strip().lower()
        disp_low = str(r.get("display_name") or "").strip().lower()
        if nm_low in _REMOVED_LEGACY_GEM_NAMES_LOWER or disp_low in _REMOVED_LEGACY_GEM_NAMES_LOWER:
            r["legacyOnly"] = True
            r["removedInPatch"] = "3.25"
            r["legacyNote"] = (
                "REMOVED in 3.25 (retained in PoB Gems.lua solely for importing pre-3.25 Standard characters; "
                "drop-disabled & unobtainable in 3.25+ leagues)."
            )


def query_local_data(
    kind: str,
    query: str = "",
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    slot: str | None = None,
    ilvl: int | None = None,
    mod_filter: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Query local PoB game data (`gem`, `unique`, `mod`, `base`, `translate`) via SQLite index."""
    from .guardrails import GuardrailScope, check_input, validate_url

    kind_norm = kind.strip().lower()
    if kind_norm not in ("gem", "unique", "mod", "base", "translate"):
        raise ValueError(
            f"Unsupported kind {kind!r}; expected one of: gem, unique, mod, base, translate."
        )

    q_clean = str(query or "").strip()
    mod_clean = str(mod_filter or "").strip() or None
    if mod_clean and kind_norm != "unique":
        raise ValueError("--mod filter is only supported for kind='unique'.")
    if q_clean:
        validate_url(q_clean, expected_game="poe1", cli_subcommand=f"data {kind_norm}")
    warnings = check_input(q_clean, scope=_guardrail_scope_for_kind(kind_norm), expected_game="poe1") if q_clean else []
    if mod_clean:
        warnings.extend(check_input(mod_clean, scope=GuardrailScope.STAT_QUERY, expected_game="poe1"))
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")

    db_path = ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
    limit = max(0, int(limit))
    q_lower = _fold_search_text(q_clean)

    resolved_alias: dict[str, str] | None = None
    suggestions: list[dict[str, Any]] | None = None
    hint: str | None = None

    conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        zh_available = meta.get("zh_available") == "1"
        results = _dispatch_query(
            conn,
            kind_norm,
            q_lower,
            slot=slot,
            ilvl=ilvl,
            limit=limit,
            zh_available=zh_available,
            mod_filter=mod_clean,
        )
        needs_slang = (not results) or (
            kind_norm == "translate" and not any(r.get("match_type") == "exact" for r in results)
        )
        if needs_slang and limit > 0 and q_clean:
            slang_hit = resolve_player_slang(q_clean)
            if slang_hit is not None:
                canon_en, _ = slang_hit
                slang_res = _dispatch_query(
                    conn,
                    kind_norm,
                    _fold_search_text(canon_en),
                    slot=slot,
                    ilvl=ilvl,
                    limit=limit,
                    zh_available=zh_available,
                    mod_filter=mod_clean,
                )
                if slang_res:
                    results = slang_res
                    resolved_alias = {"from": q_clean, "to": canon_en}
            if not results and not mod_clean:
                suggestions = _suggest_similar(conn, kind_norm, q_lower, limit=5, zh_available=zh_available)
                hint = _build_zero_result_hint(kind_norm, q_clean, suggestions)
        elif not results and mod_clean and limit > 0:
            # Try player slang expansion on mod_filter if direct match returned 0
            slang_mod = resolve_player_slang(mod_clean)
            if slang_mod is not None:
                canon_mod, _ = slang_mod
                slang_res = _dispatch_query(
                    conn,
                    kind_norm,
                    q_lower,
                    slot=slot,
                    ilvl=ilvl,
                    limit=limit,
                    zh_available=zh_available,
                    mod_filter=canon_mod,
                )
                if slang_res:
                    results = slang_res
                    resolved_alias = {"from": mod_clean, "to": canon_mod}
        _annotate_legacy_gem_rows(results, kind_norm)
    finally:
        conn.close()

    out: dict[str, Any] = {
        "kind": kind_norm,
        "query": q_clean,
        "filters": {"slot": slot, "ilvl": ilvl, "mod": mod_clean, "limit": limit},
        "pobVersion": meta.get("pob_version", "unknown"),
        "dataVersion": meta.get("data_version", "unknown"),
        "zhAvailable": zh_available,
        "count": len(results),
        "results": results,
    }
    if resolved_alias is not None:
        out["resolvedAlias"] = resolved_alias
    if suggestions is not None:
        out["suggestions"] = suggestions
    if hint is not None:
        out["hint"] = hint
    if warnings:
        out["warnings"] = [w.to_dict() for w in warnings]
    return out


def query_multi(
    db: Path | str | sqlite3.Connection | None = None,
    kind: str = "",
    queries: list[str] | None = None,
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    slot: str | None = None,
    ilvl: int | None = None,
    mod_filter: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Run multiple local game-data queries in one SQLite connection for variadic `pob data`."""
    kind_norm = str(kind).strip().lower()
    if kind_norm not in ("gem", "unique", "mod", "base", "translate"):
        raise ValueError(
            f"Unsupported kind {kind!r}; expected one of: gem, unique, mod, base, translate."
        )

    clean_queries = list(
        dict.fromkeys(q.strip() for q in (queries or []) if isinstance(q, str) and q.strip())
    )
    if not clean_queries:
        raise ValueError("queries must contain at least one non-empty query string.")

    from .guardrails import GuardrailScope, check_input, validate_url

    q_scope = _guardrail_scope_for_kind(kind_norm)
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    mod_clean = str(mod_filter or "").strip() or None
    warnings_by_query: dict[str, list[dict[str, Any]]] = {}
    for q_clean in clean_queries:
        validate_url(q_clean, expected_game="poe1", cli_subcommand=f"data {kind_norm}")
        ws = check_input(q_clean, scope=q_scope, expected_game="poe1")
        if ws:
            warnings_by_query[q_clean] = [w.to_dict() for w in ws]

    limit = max(0, int(limit))
    own_conn = False
    if isinstance(db, sqlite3.Connection):
        conn = db
    else:
        db_path = Path(db) if db is not None else ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
        conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
        own_conn = True

    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        zh_available = meta.get("zh_available") == "1"
        pob_ver = meta.get("pob_version", "unknown")
        data_ver = meta.get("data_version", "unknown")
        results_by_query: dict[str, dict[str, Any]] = {}
        for q_clean in clean_queries:
            q_folded = _fold_search_text(q_clean)
            res = _dispatch_query(
                conn,
                kind_norm,
                q_folded,
                slot=slot,
                ilvl=ilvl,
                limit=limit,
                zh_available=zh_available,
                mod_filter=mod_clean,
            )
            resolved_alias: dict[str, str] | None = None
            suggestions: list[dict[str, Any]] | None = None
            hint: str | None = None
            needs_slang_m = (not res) or (
                kind_norm == "translate" and not any(r.get("match_type") == "exact" for r in res)
            )
            if needs_slang_m and limit > 0 and q_clean:
                slang_hit = resolve_player_slang(q_clean)
                if slang_hit is not None:
                    canon_en, _ = slang_hit
                    slang_res = _dispatch_query(
                        conn,
                        kind_norm,
                        _fold_search_text(canon_en),
                        slot=slot,
                        ilvl=ilvl,
                        limit=limit,
                        zh_available=zh_available,
                        mod_filter=mod_clean,
                    )
                    if slang_res:
                        res = slang_res
                        resolved_alias = {"from": q_clean, "to": canon_en}
                if not res and not mod_clean:
                    suggestions = _suggest_similar(conn, kind_norm, q_folded, limit=5, zh_available=zh_available)
                    hint = _build_zero_result_hint(kind_norm, q_clean, suggestions)
            _annotate_legacy_gem_rows(res, kind_norm)
            entry: dict[str, Any] = {
                "kind": kind_norm,
                "query": q_clean,
                "filters": {"slot": slot, "ilvl": ilvl, "mod": mod_clean, "limit": limit},
                "pobVersion": pob_ver,
                "dataVersion": data_ver,
                "zhAvailable": zh_available,
                "count": len(res),
                "results": res,
            }
            if resolved_alias is not None:
                entry["resolvedAlias"] = resolved_alias
            if suggestions is not None:
                entry["suggestions"] = suggestions
            if hint is not None:
                entry["hint"] = hint
            if q_clean in warnings_by_query:
                entry["warnings"] = warnings_by_query[q_clean]
            results_by_query[q_clean] = entry
    finally:
        if own_conn:
            conn.close()

    out_multi: dict[str, Any] = {
        "kind": kind_norm,
        "queries": clean_queries,
        "filters": {"slot": slot, "ilvl": ilvl, "mod": mod_clean, "limit": limit},
        "pobVersion": pob_ver,
        "dataVersion": data_ver,
        "zhAvailable": zh_available,
        "count": sum(sub["count"] for sub in results_by_query.values()),
        "results": results_by_query,
    }
    dedup_warns: list[dict[str, Any]] = []
    seen_warn_keys: set[tuple[str, str]] = set()
    for ws in warnings_by_query.values():
        for w in ws:
            wkey = (str(w.get("ruleId") or ""), str(w.get("trigger") or ""))
            if wkey not in seen_warn_keys:
                seen_warn_keys.add(wkey)
                dedup_warns.append(w)
    if dedup_warns:
        out_multi["warnings"] = dedup_warns
    return out_multi


# ---------------------------------------------------------------------------
# 0ms Daemon-Free Passive Tree Index (`pob data tree`) & Tick Calculator (`pob data tick`)
# ---------------------------------------------------------------------------

TREE_SCHEMA_VERSION = 4
_TREE_INDEX_LOCK = threading.RLock()
_VALID_TREE_INDEX_CACHE: set[tuple[str, str, str]] = set()

_TREE_NODE_BLOCK_RE = re.compile(r"\n\t\t\[(\d+)\]\s*=\s*\{([\s\S]*?)\n\t\t\},")
_TREE_QUOTED_FIELD_RE = re.compile(r'\["(name|ascendancyName)"\]\s*=\s*"((?:\\[\s\S]|[^"\\])*)"')
_TREE_BOOL_FIELD_RE = re.compile(
    r'\["(isKeystone|isNotable|isMastery|isJewelSocket|isBloodline|isMultipleChoiceOption|isProxy|isAscendancyStart)"\]\s*=\s*true'
)
_TREE_STATS_BLOCK_RE = re.compile(r'\["stats"\]\s*=\s*\{([\s\S]*?)\n\t\t\t\},')
_TREE_REMINDER_BLOCK_RE = re.compile(r'\["reminderText"\]\s*=\s*\{([\s\S]*?)\n\t\t\t\},')
_TREE_MASTERY_BLOCK_RE = re.compile(r'\["masteryEffects"\]\s*=\s*\{([\s\S]*?)\n\t\t\t\},')
_TREE_MASTERY_ITEM_RE = re.compile(
    r'\["effect"\]\s*=\s*(\d+),[\s\S]*?\["stats"\]\s*=\s*\{([\s\S]*?)\n\t\t\t\t\t\},'
)
_TREE_LUA_STR_RE = re.compile(r'"((?:\\[\s\S]|[^"\\])*)"')
_TREE_ASC_ID_NAME_RE = re.compile(r'\["id"\]\s*=\s*"([^"]+)"\s*,\s*\["name"\]\s*=\s*"([^"]+)"')


def _find_latest_tree_lua(poe1_dir: Path) -> tuple[str, Path]:
    tree_root = poe1_dir / "TreeData"
    if not tree_root.is_dir():
        raise FileNotFoundError(f"PoE1/TreeData directory not found under {poe1_dir}")
    candidates: list[tuple[tuple[int, int, int, str], str, Path]] = []
    for sub in tree_root.iterdir():
        if not sub.is_dir():
            continue
        lua_p = sub / "tree.lua"
        if not lua_p.is_file():
            continue
        m = re.match(r"^(\d+)_(\d+)(.*)$", sub.name)
        if m:
            is_standard = 1 if not m.group(3) else 0
            ver_key = (int(m.group(1)), int(m.group(2)), is_standard, sub.name)
        else:
            ver_key = (0, 0, 0, sub.name)
        candidates.append((ver_key, sub.name, lua_p))
    if not candidates:
        raise FileNotFoundError(f"No tree.lua found under {tree_root}")
    candidates.sort(key=lambda x: x[0])
    _, ver_name, best_path = candidates[-1]
    return ver_name, best_path


def _extract_top_lua_section(txt: str, key: str) -> str | None:
    marker = f'\n\t["{key}"] = {{'
    start = txt.find(marker)
    if start < 0:
        return None
    end = txt.find("\n\t},", start)
    if end < 0:
        end = txt.find("\n\t}", start)
    if end < 0:
        return None
    return txt[start + len(marker) - 1 : end + 3]


def _unescape_tree_str(raw_inner: str) -> list[str]:
    """Unescape a Lua double-quoted string from tree.lua (splitting multiline `\\\\\\n` stats)."""
    s = raw_inner.replace("\\\r\n", "\n").replace("\\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    return [part.strip() for part in s.splitlines() if part.strip()]


def _load_tree_zh_maps(zh_dir: Path | None) -> tuple[dict[str, str], dict[str, str]]:
    """Load EN->ZH maps for passive node/ascendancy names (`tree_dn.json`) and stats (`tree_sd.json` + S30)."""
    dn_map: dict[str, str] = {}
    sd_map: dict[str, str] = {}
    if zh_dir is None or not zh_dir.is_dir():
        return dn_map, sd_map
    for fn in ("tree_dn.json", "Z-S25.json", "Z-S26.json", "Z-S27.json", "Z-S28.json", "Z-S29.json", "Z-S30.json", "Z-S30-Pearl_of_Tsoatha-Passive_Tree.json"):
        p = zh_dir / fn
        if not p.is_file():
            continue
        try:
            entries = json.loads(p.read_text(encoding="utf-8")).get("entries")
        except Exception:
            continue
        if not isinstance(entries, dict):
            continue
        for k, v in entries.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                dn_map[k.strip()] = v.strip()
                if fn != "tree_dn.json":
                    sd_map[k.strip().lower()] = v.strip()
    sd_path = zh_dir / "tree_sd.json"
    if sd_path.is_file():
        try:
            entries = json.loads(sd_path.read_text(encoding="utf-8")).get("entries")
            if isinstance(entries, dict):
                for k, v in entries.items():
                    if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                        kl = k.strip().lower()
                        if kl not in sd_map:
                            sd_map[kl] = v.strip()
        except Exception:
            pass
    return dn_map, sd_map


def _parse_tree_lua_nodes(tree_lua_path: Path, zh_dir: Path | None) -> list[dict[str, Any]]:
    txt = tree_lua_path.read_text(encoding="utf-8", errors="ignore")
    dn_map, sd_map = _load_tree_zh_maps(zh_dir)

    # 1. Build ascendancy ID -> (display_name, class_name, is_alternate_asc, is_bloodline)
    #    Critical: in 3.25-3.30 tree.lua, Ranger's Warden keeps internal id="Raider",
    #    while Affliction 3.23 legacy alternate_ascendancies has id="Warden", name="Warden of the Maji"!
    asc_meta: dict[str, tuple[str, str, bool, bool]] = {}
    cls_sec = _extract_top_lua_section(txt, "classes")
    if cls_sec:
        for aid, aname in _TREE_ASC_ID_NAME_RE.findall(cls_sec):
            aid_s = aid.strip()
            aname_s = aname.strip() or aid_s
            if aid_s:
                asc_meta[aid_s] = (aname_s, "", False, False)

    alt_sec = _extract_top_lua_section(txt, "alternate_ascendancies")
    if alt_sec:
        for aid, aname in _TREE_ASC_ID_NAME_RE.findall(alt_sec):
            aid_s = aid.strip()
            aname_s = aname.strip() or aid_s
            if aid_s and aid_s not in asc_meta:
                is_bl = "bloodline" in aname_s.lower()
                asc_meta[aid_s] = (aname_s, "", not is_bl, is_bl)

    nodes_sec = _extract_top_lua_section(txt, "nodes")
    if not nodes_sec:
        return []

    records: list[dict[str, Any]] = []
    for m in _TREE_NODE_BLOCK_RE.finditer(nodes_sec):
        nid = int(m.group(1))
        body = m.group(2)
        flags = set(_TREE_BOOL_FIELD_RE.findall(body))
        if "isProxy" in flags or "isAscendancyStart" in flags or '["classStartIndex"]' in body:
            continue

        fields = dict(_TREE_QUOTED_FIELD_RE.findall(body))
        raw_name = fields.get("name", "")
        name_parts = _unescape_tree_str(raw_name)
        name = name_parts[0] if name_parts else ""
        if not name:
            continue

        raw_asc_id = fields.get("ascendancyName", "").strip()
        stats: list[str] = []
        m_st = _TREE_STATS_BLOCK_RE.search(body)
        if m_st:
            for sm in _TREE_LUA_STR_RE.finditer(m_st.group(1)):
                stats.extend(_unescape_tree_str(sm.group(1)))

        mastery_effects: list[dict[str, Any]] = []
        if "isMastery" in flags:
            m_me = _TREE_MASTERY_BLOCK_RE.search(body)
            if m_me:
                for em in _TREE_MASTERY_ITEM_RE.finditer(m_me.group(1)):
                    eid = int(em.group(1))
                    estats: list[str] = []
                    for sm in _TREE_LUA_STR_RE.finditer(em.group(2)):
                        estats.extend(_unescape_tree_str(sm.group(1)))
                    estats_zh = [sd_map.get(s.lower()) for s in estats]
                    mastery_effects.append(
                        {
                            "effect": eid,
                            "stats": estats,
                            "statsZh": [z for z in estats_zh if z],
                        }
                    )

        reminder: list[str] = []
        m_rem = _TREE_REMINDER_BLOCK_RE.search(body)
        if m_rem:
            for rm in _TREE_LUA_STR_RE.finditer(m_rem.group(1)):
                reminder.extend(_unescape_tree_str(rm.group(1)))

        is_keystone = "isKeystone" in flags
        is_notable = "isNotable" in flags
        is_mastery = "isMastery" in flags
        is_socket = "isJewelSocket" in flags
        is_bloodline_flag = "isBloodline" in flags
        is_multi_opt = "isMultipleChoiceOption" in flags

        asc_display = ""
        class_name = ""
        is_alt_asc = False
        is_bloodline = is_bloodline_flag
        if raw_asc_id:
            if raw_asc_id in asc_meta:
                asc_display, class_name, is_alt_asc, meta_bl = asc_meta[raw_asc_id]
                is_bloodline = is_bloodline or meta_bl
            else:
                asc_display = raw_asc_id

        is_ascendancy = bool(raw_asc_id)
        if is_keystone:
            ntype = "Keystone"
        elif is_mastery:
            ntype = "Mastery"
        elif is_socket:
            ntype = "Socket"
        elif is_bloodline:
            ntype = "Bloodline"
        elif is_ascendancy and (is_notable or is_multi_opt):
            ntype = "Ascendancy"
        elif is_notable:
            ntype = "Notable"
        else:
            ntype = "Normal"

        zh_name = dn_map.get(name)
        asc_zh = dn_map.get(asc_display) if asc_display else None
        stats_zh = [z for s in stats if (z := sd_map.get(s.lower()))]

        records.append(
            {
                "id": nid,
                "name": name,
                "zhName": zh_name,
                "type": ntype,
                "ascId": raw_asc_id or None,
                "ascName": asc_display or None,
                "ascNameZh": asc_zh,
                "className": class_name or None,
                "isAscendancy": is_ascendancy,
                "isBloodline": is_bloodline,
                "isAlternateAsc": is_alt_asc,
                "stats": stats,
                "statsZh": stats_zh,
                "masteryEffects": mastery_effects,
                "reminderText": reminder,
            }
        )

    # Also index Timeless / Historic Jewel Keystones & Notables from LegionPassives.lua
    legion_path = tree_lua_path.parent.parent.parent / "Data" / "TimelessJewelData" / "LegionPassives.lua"
    if legion_path.is_file():
        leg_txt = legion_path.read_text(encoding="utf-8", errors="ignore")
        leg_nodes_sec = _extract_top_lua_section(leg_txt, "nodes") or leg_txt
        seen_leg_names: set[str] = set()
        for m_leg in _TREE_NODE_BLOCK_RE.finditer(leg_nodes_sec):
            leg_idx = int(m_leg.group(1))
            lbody = m_leg.group(2)
            is_lks = bool(re.search(r'\["ks"\]\s*=\s*true', lbody))
            is_lnot = bool(re.search(r'\["not"\]\s*=\s*true', lbody))
            if not (is_lks or is_lnot):
                continue
            m_dn = re.search(r'\["dn"\]\s*=\s*"((?:\\[\s\S]|[^"\\])*)"', lbody)
            if not m_dn:
                continue
            lname_parts = _unescape_tree_str(m_dn.group(1))
            lname = lname_parts[0] if lname_parts else ""
            if not lname or lname.lower() in seen_leg_names:
                continue
            seen_leg_names.add(lname.lower())
            lstats: list[str] = []
            m_sd = re.search(r'\["sd"\]\s*=\s*\{([\s\S]*?)\n\t\t\t\},', lbody)
            if m_sd:
                for sm in _TREE_LUA_STR_RE.finditer(m_sd.group(1)):
                    lstats.extend(_unescape_tree_str(sm.group(1)))
            lzh_name = dn_map.get(lname)
            lstats_zh = [z for s in lstats if (z := sd_map.get(s.lower()))]
            records.append(
                {
                    "id": 900000 + leg_idx,
                    "name": lname,
                    "zhName": lzh_name,
                    "type": "Keystone" if is_lks else "Notable",
                    "ascId": "Timeless",
                    "ascName": "Timeless Jewel",
                    "ascNameZh": "永恒珠宝",
                    "className": None,
                    "isAscendancy": False,
                    "isBloodline": False,
                    "isAlternateAsc": False,
                    "stats": lstats,
                    "statsZh": lstats_zh,
                    "masteryEffects": [],
                    "reminderText": [],
                }
            )

    return records


def ensure_treedata_index(
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    force_rebuild: bool = False,
) -> tuple[Path, str]:
    """Build (if missing/stale) and return `(db_path, tree_version)` for 0ms daemon-free passive tree queries."""
    rt_dir = resolve_runtime_dir(runtime_dir)
    resolved_pob = resolve_pob_dir(pob_dir, rt_dir)
    poe1_dir, zh_dir = _resolve_poe1_data_dirs(resolved_pob)
    tree_ver, tree_lua_path = _find_latest_tree_lua(poe1_dir)

    hasher = hashlib.blake2b(digest_size=4)
    st = tree_lua_path.stat()
    hasher.update(f"{tree_ver}:{st.st_size}:{st.st_mtime_ns};".encode("utf-8"))
    if zh_dir is not None and zh_dir.is_dir():
        for fn in ("tree_dn.json", "tree_sd.json", "Z-S30.json"):
            zp = zh_dir / fn
            if zp.is_file():
                zst = zp.stat()
                hasher.update(f"{fn}:{zst.st_size}:{zst.st_mtime_ns};".encode("utf-8"))
    data_hash = hasher.hexdigest()

    cache_dir = _shared_cache_dir(runtime_dir, pob_root=resolved_pob)
    db_path = cache_dir / f"treedata_{tree_ver}_{data_hash}.sqlite"
    cache_key = (str(db_path), tree_ver, data_hash)

    def _is_tree_db_valid() -> bool:
        if not db_path.is_file():
            return False
        try:
            c = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
            try:
                m = dict(c.execute("SELECT key, value FROM meta").fetchall())
                return (
                    m.get("schema_version") == str(TREE_SCHEMA_VERSION)
                    and m.get("tree_version") == tree_ver
                    and m.get("data_hash") == data_hash
                )
            finally:
                c.close()
        except sqlite3.Error:
            return False

    if not force_rebuild:
        if cache_key in _VALID_TREE_INDEX_CACHE and db_path.is_file():
            return db_path, tree_ver
        if _is_tree_db_valid():
            _VALID_TREE_INDEX_CACHE.add(cache_key)
            return db_path, tree_ver
    else:
        _VALID_TREE_INDEX_CACHE.discard(cache_key)

    with _TREE_INDEX_LOCK:
        if not force_rebuild and _is_tree_db_valid():
            _VALID_TREE_INDEX_CACHE.add(cache_key)
            return db_path, tree_ver

        nodes = _parse_tree_lua_nodes(tree_lua_path, zh_dir)
        tmp_path = cache_dir / f".treedata_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.sqlite.tmp"
        try:
            conn = sqlite3.connect(str(tmp_path))
            try:
                conn.execute("PRAGMA page_size = 8192")
                conn.execute("PRAGMA journal_mode = OFF")
                conn.execute("PRAGMA synchronous = OFF")
                conn.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE tree_nodes (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        name_lower TEXT NOT NULL,
                        zh_name TEXT,
                        zh_lower TEXT,
                        node_type TEXT NOT NULL,
                        asc_id_lower TEXT,
                        asc_name_lower TEXT,
                        asc_zh_lower TEXT,
                        is_asc INTEGER NOT NULL,
                        is_bloodline INTEGER NOT NULL,
                        is_alt_asc INTEGER NOT NULL,
                        has_stats INTEGER NOT NULL,
                        search_text_lower TEXT NOT NULL,
                        data_json TEXT NOT NULL
                    );
                    CREATE INDEX idx_tn_type ON tree_nodes(node_type);
                    CREATE INDEX idx_tn_asc ON tree_nodes(asc_name_lower);
                    CREATE VIRTUAL TABLE tree_nodes_fts USING fts5(
                        search_text_lower, content='tree_nodes', content_rowid='id', tokenize='trigram'
                    );
                    """
                )
                conn.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    [
                        ("schema_version", str(TREE_SCHEMA_VERSION)),
                        ("tree_version", tree_ver),
                        ("data_hash", data_hash),
                        ("zh_available", "1" if zh_dir is not None else "0"),
                    ],
                )
                rows_to_insert = []
                for nd in nodes:
                    me_texts = []
                    for me in nd["masteryEffects"]:
                        me_texts.extend(me["stats"])
                        me_texts.extend(me["statsZh"])
                    s_txt = _fold_search_text(
                        " ".join(
                            p
                            for p in (
                                nd["name"],
                                nd["zhName"] or "",
                                nd["ascName"] or "",
                                nd["ascNameZh"] or "",
                                nd["ascId"] or "",
                                nd["className"] or "",
                                " ".join(nd["stats"]),
                                " ".join(nd["statsZh"]),
                                " ".join(me_texts),
                            )
                            if p
                        )
                    )
                    rows_to_insert.append(
                        (
                            nd["id"],
                            nd["name"],
                            _fold_search_text(nd["name"]),
                            nd["zhName"],
                            _fold_search_text(nd["zhName"]) if nd["zhName"] else None,
                            nd["type"],
                            _fold_search_text(nd["ascId"]) if nd["ascId"] else None,
                            _fold_search_text(nd["ascName"]) if nd["ascName"] else None,
                            _fold_search_text(nd["ascNameZh"]) if nd["ascNameZh"] else None,
                            int(nd["isAscendancy"]),
                            int(nd["isBloodline"]),
                            int(nd["isAlternateAsc"]),
                            int(bool(nd["stats"] or nd["masteryEffects"])),
                            s_txt,
                            json.dumps(nd, ensure_ascii=False, separators=(",", ":")),
                        )
                    )
                conn.executemany(
                    "INSERT INTO tree_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows_to_insert,
                )
                conn.execute("INSERT INTO tree_nodes_fts(tree_nodes_fts) VALUES('rebuild')")
                conn.commit()
            finally:
                conn.close()
            try:
                os.replace(tmp_path, db_path)
            except PermissionError:
                if not _is_tree_db_valid():
                    raise
            _VALID_TREE_INDEX_CACHE.add(cache_key)
            return db_path, tree_ver
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def query_tree_data(
    queries: list[str] | str | None = None,
    *,
    node_type: str | None = None,
    asc: str | None = None,
    limit: int = 25,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """0ms daemon-free search directly over PoB's local `TreeData/3_30/tree.lua` + ZH translation overlay."""
    from .guardrails import GuardrailScope, check_input

    t0 = time.perf_counter()
    if isinstance(queries, str):
        raw_queries = [queries.strip()] if queries.strip() else []
    else:
        raw_queries = [str(q).strip() for q in (queries or []) if str(q or "").strip()]

    asc_clean = str(asc or "").strip() or None
    type_clean = str(node_type or "").strip() or None
    warnings: list[dict[str, Any]] = []
    for q in raw_queries:
        ws = check_input(q, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
        warnings.extend(w.to_dict() for w in ws)
    if asc_clean:
        ws_a = check_input(asc_clean, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
        warnings.extend(w.to_dict() for w in ws_a)

    # Resolve slang aliases on queries & --asc (e.g. "聚魂" -> "Polymath", "守望者" -> "Warden", "欺诈师" -> "Trickster")
    resolved_aliases: list[dict[str, str]] = []
    expanded_queries: list[tuple[str, str]] = []  # (folded_primary, folded_alt_or_empty)
    for q in raw_queries:
        qf = _fold_search_text(q)
        sl = resolve_player_slang(q)
        if sl is not None:
            resolved_aliases.append({"from": q, "to": sl[0]})
            expanded_queries.append((qf, _fold_search_text(sl[0])))
        else:
            expanded_queries.append((qf, ""))

    asc_folded = ""
    asc_alt = ""
    if asc_clean:
        asc_folded = _fold_search_text(asc_clean)
        sl_a = resolve_player_slang(asc_clean)
        if sl_a is not None:
            resolved_aliases.append({"from": asc_clean, "to": sl_a[0]})
            asc_alt = _fold_search_text(sl_a[0])

    db_path, tree_ver = ensure_treedata_index(
        pob_dir=pob_dir, runtime_dir=runtime_dir, force_rebuild=force_rebuild
    )
    limit = max(1, int(limit))
    type_low = type_clean.lower() if type_clean else ""

    conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
    try:
        rows = conn.execute(
            """
            SELECT id, name_lower, zh_lower, node_type, asc_id_lower, asc_name_lower, asc_zh_lower,
                   is_asc, is_bloodline, is_alt_asc, has_stats, search_text_lower, data_json
            FROM tree_nodes
            """
        ).fetchall()

        # Check if --asc exactly matches an active (non-alternate) class ascendancy (e.g. Warden / 守望者)
        # so we exclude legacy Affliction 3.23 "Warden of the Maji" unless explicitly queried!
        asc_targets = [x for x in (asc_folded, asc_alt) if x]
        asc_matches_active_exact = False
        if asc_targets:
            for r in rows:
                if r[9] == 0 and (
                    (r[5] and r[5] in asc_targets) or (r[6] and r[6] in asc_targets)
                ):
                    asc_matches_active_exact = True
                    break

        type_rank = {
            "Keystone": 0,
            "Ascendancy": 1,
            "Bloodline": 1,
            "Notable": 2,
            "Mastery": 3,
            "Socket": 4,
            "Normal": 5,
        }

        scored: list[tuple[tuple[int, int, int, int, str], dict[str, Any]]] = []
        for (
            nid,
            name_l,
            zh_l,
            ntype,
            asc_id_l,
            asc_name_l,
            asc_zh_l,
            is_asc,
            is_bl,
            is_alt_asc,
            has_stats,
            search_l,
            dj,
        ) in rows:
            zh_l = zh_l or ""
            asc_id_l = asc_id_l or ""
            asc_name_l = asc_name_l or ""
            asc_zh_l = asc_zh_l or ""

            # Filter by --type
            if type_low:
                if type_low in ("asc", "ascend", "ascendancy"):
                    if not is_asc:
                        continue
                elif type_low == "bloodline":
                    if not is_bl:
                        continue
                elif ntype.lower() != type_low:
                    continue

            # Filter by --asc
            if asc_targets:
                if not is_asc:
                    continue
                if asc_matches_active_exact:
                    if is_alt_asc:
                        continue
                    if not (asc_name_l in asc_targets or asc_zh_l in asc_targets):
                        continue
                else:
                    if not any(
                        (t in asc_name_l or t in asc_zh_l or t == asc_id_l)
                        for t in asc_targets
                    ):
                        continue

            # When listing an Ascendancy without text query, skip blank/minor passive nodes if notables exist
            if not expanded_queries and asc_targets and ntype == "Normal" and not has_stats:
                continue

            # Match text queries (OR across multiple queries; within each query, match primary OR slang alt)
            match_tier = 2
            if expanded_queries:
                best_q_tier = 99
                for q_prim, q_alt in expanded_queries:
                    for cand_q in (q_prim, q_alt):
                        if not cand_q:
                            continue
                        toks = [t for t in cand_q.split() if t]
                        if not toks:
                            continue
                        if cand_q == name_l or (zh_l and cand_q == zh_l):
                            best_q_tier = min(best_q_tier, 0)
                        elif name_l.startswith(cand_q) or (zh_l and zh_l.startswith(cand_q)):
                            best_q_tier = min(best_q_tier, 1)
                        elif cand_q in name_l or (zh_l and cand_q in zh_l):
                            best_q_tier = min(best_q_tier, 2)
                        elif all(t in search_l for t in toks):
                            best_q_tier = min(best_q_tier, 3)
                if best_q_tier >= 99:
                    continue
                match_tier = best_q_tier

            scored.append(
                (
                    (
                        is_alt_asc,
                        match_tier,
                        type_rank.get(ntype, 6),
                        0 if has_stats else 1,
                        name_l,
                    ),
                    json.loads(dj),
                )
            )

        scored.sort(key=lambda item: item[0])
        # Deduplicate identical (name, ascName, stats) small travel nodes when listing an ascendancy
        results: list[dict[str, Any]] = []
        seen_small: set[tuple[str, str, tuple[str, ...]]] = set()
        for _, nd in scored:
            if nd["type"] == "Normal" and not nd.get("isBloodline"):
                skey = (nd["name"], str(nd.get("ascName") or ""), tuple(nd.get("stats") or ()))
                if skey in seen_small:
                    continue
                seen_small.add(skey)
            results.append(nd)
            if len(results) >= limit:
                break
    finally:
        conn.close()

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    out: dict[str, Any] = {
        "kind": "tree",
        "queries": raw_queries,
        "filters": {"type": type_clean, "asc": asc_clean, "limit": limit},
        "treeVersion": tree_ver,
        "count": len(results),
        "totalMatched": len(scored),
        "elapsedMs": elapsed_ms,
        "results": results,
    }
    if resolved_aliases:
        out["resolvedAliases"] = resolved_aliases
    if warnings:
        out["warnings"] = warnings
    return out


_TICK_BASE_CD_PRESETS: dict[str, tuple[float, str]] = {
    "coc": (0.15, "Cast On Critical Strike (0.15s)"),
    "cospri": (0.25, "Cospri's Malice (0.25s)"),
    "mjolner": (0.25, "Mjölner (0.25s)"),
    "poet": (0.25, "The Poet's Pen (0.25s)"),
    "cwdt": (0.25, "Cast when Damage Taken (0.25s)"),
    "soa": (0.50, "The Sword of Ages / Temporal Mirage (0.50s)"),
    "mirage": (0.50, "Temporal Mirage (0.50s)"),
    "hidden": (0.50, "The Hidden Blade (0.50s)"),
    "spellslinger": (0.60, "Spellslinger (0.60s)"),
    "kitava": (0.10, "Kitava's Thirst (0.10s)"),
}


def query_tick_breakpoints(
    base_cd_spec: str | float,
    *,
    cdr_pct: float | None = None,
    aps: float | None = None,
    triggered_spells: int = 1,
) -> dict[str, Any]:
    """Daemon-free 0ms PoE1 0.033s (33ms) server-tick cooldown breakpoint calculator."""
    from .analyzer import build_server_tick_table

    raw = str(base_cd_spec or "").strip()
    low = raw.lower()
    preset_label: str | None = None
    if low in _TICK_BASE_CD_PRESETS:
        base_cd, preset_label = _TICK_BASE_CD_PRESETS[low]
    elif low.endswith("ms"):
        try:
            base_cd = float(low[:-2].strip()) / 1000.0
        except ValueError as exc:
            raise ValueError(f"Invalid base cooldown {base_cd_spec!r}; pass seconds (e.g. 0.15), ms (150ms), or preset ({', '.join(_TICK_BASE_CD_PRESETS)}).") from exc
    else:
        try:
            base_cd = float(low.rstrip("s").strip())
        except ValueError as exc:
            raise ValueError(f"Invalid base cooldown {base_cd_spec!r}; pass seconds (e.g. 0.15), ms (150ms), or preset ({', '.join(_TICK_BASE_CD_PRESETS)}).") from exc
    if base_cd <= 0:
        raise ValueError(f"Base cooldown must be > 0 (got {base_cd}).")

    tbl = build_server_tick_table(
        base_cd,
        cdr_pct=cdr_pct,
        aps=aps,
        triggered_spells=triggered_spells,
    )
    tbl["kind"] = "tick"
    if preset_label:
        tbl["preset"] = preset_label
    return tbl
