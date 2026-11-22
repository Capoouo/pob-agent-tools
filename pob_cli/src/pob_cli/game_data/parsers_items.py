"""PoB Lua parsers for item bases, uniques (including Special/*.lua), and mods."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .constants import (
    LUA_SKIP_KEYS_MOD,
    MOD_FILES,
    MOD_TAG_PREFIX_RE,
    UNIQUE_META_KEYS,
)
from .lua_parser import (
    MOD_TRADEHASH_STRIP_RE,
    _as_seq,
    _LuaParser,
    _parse_lua_assignments,
    _parse_lua_return,
    _tokenize_lua,
)
from .zh_dict import _ZhDictionary

_KNOWN_GENERATED_UNIQUE_VARS: tuple[str, ...] = (
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
)


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

            records.append(
                {
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
            )
    return records


def _load_special_mod_pools(special_dir: Path) -> dict[str, list[str]]:
    pools: dict[str, list[str]] = {
        "watchers_eye": [],
        "sublime_vision": [],
        "voranas_march": [],
        "bound_by_destiny": [],
    }
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
                    pools["sublime_vision"].extend(stat_lines)
                elif mod_id.startswith("SummonArbalist"):
                    pools["voranas_march"].extend(stat_lines)
                else:
                    pools["watchers_eye"].extend(stat_lines)

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
                        pools["bound_by_destiny"].append(" ".join(stat_lines))
    return pools


def _synthesize_forbidden_jewel_blocks() -> list[tuple[str, str]]:
    return [
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
        ),
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
        ),
    ]


def _parse_generated_lua(gen_path: Path, pools: dict[str, list[str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    gen_txt = gen_path.read_text(encoding="utf-8", errors="ignore")
    parser = _LuaParser(_tokenize_lua(gen_txt))
    local_tables: dict[str, Any] = {}
    local_inserts: dict[str, list[str]] = {}
    generated_vars: list[str] = []

    while parser.pos < parser.n:
        tok = parser.peek()
        if tok == ("IDENT", "local"):
            nxt = parser.peek(1)
            if nxt is not None and nxt[0] == "IDENT" and parser.peek(2) == ("OP", "=") and parser.peek(3) == ("OP", "{"):
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
                    if parser.peek(3) == ("OP", ")"):
                        local_inserts.setdefault(target_var, []).append(parser.peek(2)[1])
            continue
        parser.advance()

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
        "watchersEye": pools["watchers_eye"],
        "sublimeVision": pools["sublime_vision"],
        "voranasMarch": pools["voranas_march"],
        "boundByDestiny": pools["bound_by_destiny"],
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

    ordered_vars = list(generated_vars)
    for known in _KNOWN_GENERATED_UNIQUE_VARS:
        if known in local_tables and known not in ordered_vars:
            ordered_vars.append(known)

    for var_name in ordered_vars:
        tbl = local_tables.get(var_name)
        parts: list[str] = [str(x) for x in _as_seq(tbl) if isinstance(x, str) and x.strip()]
        parts.extend(s for s in local_inserts.get(var_name, []) if s.strip())
        parts.extend(extra_by_var.get(var_name, []))
        if parts:
            out.append((var_name, "\n".join(parts)))

    if "buildForbidden" in gen_txt:
        out.extend(_synthesize_forbidden_jewel_blocks())
    return out


def _synthesize_minimal_special_blocks(pools: dict[str, list[str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if pools["watchers_eye"]:
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
                        *pools["watchers_eye"],
                    ]
                ),
            )
        )
    if pools["bound_by_destiny"]:
        out.append(
            (
                "BoundByDestiny",
                "\n".join(
                    [
                        "Bound by Destiny",
                        "Prismatic Jewel",
                        "Limited to: 1",
                        "Implicits: 0",
                        *pools["bound_by_destiny"],
                    ]
                ),
            )
        )
    return out


def _extract_other_special_files(special_dir: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
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
        txt = p.read_text(encoding="utf-8", errors="ignore")
        p_sub = _LuaParser(_tokenize_lua(txt))
        while p_sub.pos < p_sub.n:
            if p_sub.peek() == ("OP", "{"):
                tbl = p_sub.parse_table()
                for b in _as_seq(tbl):
                    if isinstance(b, str) and b.strip():
                        out.append((p.stem, b))
            else:
                p_sub.advance()
    return out


def _extract_special_lua_blocks(special_dir: Path, data_dir: Path) -> list[tuple[str, str]]:
    """Safely extract unique item text blocks from all ``Data/Uniques/Special/*.lua`` files."""
    if not special_dir.is_dir():
        return []
    pools = _load_special_mod_pools(special_dir)
    gen_path = special_dir / "Generated.lua"
    out = _parse_generated_lua(gen_path, pools) if gen_path.is_file() else _synthesize_minimal_special_blocks(pools)
    out.extend(_extract_other_special_files(special_dir))
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
            if ln.startswith(("Requires Level ", "LevelReq:", "Level:")):
                m_lvl = re.search(r"(\d+)", ln)
                if m_lvl:
                    req_level = int(m_lvl.group(1))
                idx += 1
                continue
            if ln.startswith("{variant:") and idx < 10 and MOD_TAG_PREFIX_RE.sub("", ln).strip() in bases_by_name:
                idx += 1
                continue
            if ln in ("Shaper Item", "Elder Item", "Fractured Item", "Synthesised Item"):
                idx += 1
                continue
            if ":" in ln and not ln.startswith("{") and ln.split(":", 1)[0].strip() in UNIQUE_META_KEYS:
                idx += 1
                continue
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
            for k, v in zip(wk_list, wv_list, strict=False):
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
