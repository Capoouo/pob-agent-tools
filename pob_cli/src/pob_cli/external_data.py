"""External PoE 1 data queries: poe.ninja economy prices/meta (15m cache) and poewiki.net MediaWiki API (24h cache)."""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from typing import Any

from .importers import (
    _fetch_poe_ninja_build_index_state,
    _fetch_poe_ninja_index_state,
    _get_cache_dir,
    _make_httpx_client,
)

CURRENCY_TYPES = ("Currency", "Fragment")
ITEM_TYPES = (
    "DivinationCard",
    "Essence",
    "UniqueMap",
    "Map",
    "UniqueJewel",
    "UniqueFlask",
    "UniqueWeapon",
    "UniqueArmour",
    "UniqueAccessory",
    "SkillGem",
    "BaseType",
    "Fossil",
    "Resonator",
    "Scarab",
    "Beast",
    "Incubator",
    "Oil",
    "Vial",
    "DeliriumOrb",
    "Invitation",
    "BlightedMap",
    "ClusterJewel",
    "Artifact",
    "BlightRavagedMap",
    "ScourgedMap",
    "Tattoo",
    "Omen",
    "UniqueRelic",
    "Memory",
    "IncursionTemple",
    "Coffin",
    "AllflameEmber",
    "KalguuranRune",
    "UniqueIdol",
    "UniqueTincture",
    "Runegraft",
    "Wombgift",
    "ForbiddenJewel",
    "ValdoMap",
    "DjinnCoin",
    "Astrolabe",
    "ShrineBelt",
    "ImbuedGem",
    "Ducat",
    "EnshroudingCrystal",
    "Flask",
    "ScryingOrb",
    "Corpse",
    "Viscera",
    "Thread",
)
ALL_OVERVIEW_TYPES = CURRENCY_TYPES + ITEM_TYPES

CATEGORY_MAP: dict[str, tuple[str, ...]] = {
    # Grouped categories
    "currency": ("Currency", "Fragment"),
    "unique": (
        "UniqueWeapon",
        "UniqueArmour",
        "UniqueAccessory",
        "UniqueFlask",
        "UniqueJewel",
        "ForbiddenJewel",
        "UniqueTincture",
        "UniqueRelic",
        "UniqueMap",
        "UniqueIdol",
    ),
    "weapon": ("UniqueWeapon",),
    "armour": ("UniqueArmour",),
    "accessory": ("UniqueAccessory",),
    "flask": ("UniqueFlask", "UniqueTincture", "Flask"),
    "jewel": ("UniqueJewel", "ClusterJewel", "ForbiddenJewel"),
    "cluster": ("ClusterJewel",),
    "gem": ("SkillGem", "ImbuedGem"),
    "transfiguredgem": ("SkillGem", "ImbuedGem"),
    "map": ("Map", "UniqueMap", "BlightedMap", "BlightRavagedMap", "ScourgedMap", "ValdoMap"),
    "card": ("DivinationCard",),
    "base": ("BaseType",),
    "fossil": ("Fossil", "Resonator"),
    "allflame": ("AllflameEmber",),
    "rune": ("KalguuranRune", "Runegraft"),
    # Exact 1:1 aliases for all 52 overview types
    "fragment": ("Fragment",),
    "divinationcard": ("DivinationCard",),
    "essence": ("Essence",),
    "uniquemap": ("UniqueMap",),
    "uniquejewel": ("UniqueJewel",),
    "uniqueflask": ("UniqueFlask",),
    "uniqueweapon": ("UniqueWeapon",),
    "uniquearmour": ("UniqueArmour",),
    "uniqueaccessory": ("UniqueAccessory",),
    "skillgem": ("SkillGem",),
    "basetype": ("BaseType",),
    "resonator": ("Resonator",),
    "scarab": ("Scarab",),
    "beast": ("Beast",),
    "incubator": ("Incubator",),
    "oil": ("Oil",),
    "vial": ("Vial",),
    "deliriumorb": ("DeliriumOrb",),
    "invitation": ("Invitation",),
    "blightedmap": ("BlightedMap",),
    "clusterjewel": ("ClusterJewel",),
    "artifact": ("Artifact",),
    "blightravagedmap": ("BlightRavagedMap",),
    "scourgedmap": ("ScourgedMap",),
    "tattoo": ("Tattoo",),
    "omen": ("Omen",),
    "uniquerelic": ("UniqueRelic",),
    "memory": ("Memory",),
    "incursiontemple": ("IncursionTemple",),
    "coffin": ("Coffin",),
    "allflameember": ("AllflameEmber",),
    "kalguuranrune": ("KalguuranRune",),
    "uniqueidol": ("UniqueIdol",),
    "uniquetincture": ("UniqueTincture",),
    "runegraft": ("Runegraft",),
    "wombgift": ("Wombgift",),
    "forbiddenjewel": ("ForbiddenJewel",),
    "valdomap": ("ValdoMap",),
    "djinncoin": ("DjinnCoin",),
    "astrolabe": ("Astrolabe",),
    "shrinebelt": ("ShrineBelt",),
    "imbuedgem": ("ImbuedGem",),
    "ducat": ("Ducat",),
    "enshroudingcrystal": ("EnshroudingCrystal",),
    "scryingorb": ("ScryingOrb",),
    "corpse": ("Corpse",),
    "viscera": ("Viscera",),
    "thread": ("Thread",),
}

NINJA_DENSE_URL = "https://poe.ninja/poe1/api/economy/current/dense/overviews"
NINJA_STASH_ITEM_URL = "https://poe.ninja/poe1/api/economy/stash/current/item/overview"
NINJA_STASH_CURRENCY_URL = "https://poe.ninja/poe1/api/economy/stash/current/currency/overview"
POE_LEAGUES_URL = "https://www.pathofexile.com/api/leagues?type=main&realm=pc"

_AUTO_DETAIL_PATTERNS = (
    re.compile(r"\b\d{1,2}/\d{1,2}c?\b", re.IGNORECASE),
    re.compile(r"\b(?:corrupted|uncorrupted)\b", re.IGNORECASE),
    re.compile(r"\b[56]l\b", re.IGNORECASE),
    re.compile(r"\b[56]-?link\b", re.IGNORECASE),
    re.compile(r"\b\d+\s*passives?\b", re.IGNORECASE),
)


def _extract_challenge_league_from_index_state() -> str | None:
    try:
        state = _fetch_poe_ninja_index_state()
        econ_leagues = state.get("economyLeagues", [])
        if econ_leagues and econ_leagues[0].get("name"):
            return str(econ_leagues[0]["name"]).strip()
    except Exception:
        pass
    return None


def resolve_current_trade_league(ttl_sec: int = 3600) -> str:
    """Resolve current primary softcore trade league name with 1h disk cache.

    Stale 'Standard' cache entries are automatically invalidated/replaced whenever
    poe.ninja index-state reports a non-Standard challenge league in economyLeagues[0].
    """
    cache_file = _get_cache_dir() / "poe_trade_league.json"
    now = time.time()
    cached_league: str | None = None

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("_ts", 0) < ttl_sec and cached.get("league"):
                cached_league = str(cached["league"]).strip()
        except Exception:
            cached_league = None

    if cached_league and cached_league.lower() != "standard":
        return cached_league

    ninja_league = _extract_challenge_league_from_index_state()
    if ninja_league:
        if ninja_league.lower() != "standard" or not cached_league:
            try:
                cache_file.write_text(json.dumps({"_ts": now, "league": ninja_league}), encoding="utf-8")
            except Exception:
                pass
            return ninja_league

    if cached_league:
        return cached_league

    resolved = "Standard"
    try:
        with _make_httpx_client(timeout=10.0) as client:
            resp = client.get(POE_LEAGUES_URL)
            if resp.status_code == 200:
                leagues = resp.json()
                for lg in leagues:
                    lid = str(lg.get("id") or "").strip()
                    if lid and lid not in ("Standard", "Hardcore", "Solo Self-Found", "Hardcore SSF", "Ruthless"):
                        if not any(tag in lid for tag in ("Hardcore", "SSF", "Ruthless")):
                            resolved = lid
                            break
    except Exception:
        pass

    try:
        cache_file.write_text(json.dumps({"_ts": now, "league": resolved}), encoding="utf-8")
    except Exception:
        pass
    return resolved


def _fetch_dense_overviews(league: str, ttl_sec: int = 900) -> dict[str, list[dict[str, Any]]]:
    safe_key = re.sub(r"[^A-Za-z0-9_-]", "_", league)
    cache_file = _get_cache_dir() / f"ninja_dense_{safe_key}.json"
    now = time.time()
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("_ts", 0) < ttl_sec and "data" in cached:
                return cached["data"]
        except Exception:
            pass

    type_to_lines: dict[str, list[dict[str, Any]]] = {}
    with _make_httpx_client(timeout=30.0) as client:
        resp = client.get(NINJA_DENSE_URL, params={"league": league, "language": "en"})
        resp.raise_for_status()
        data = resp.json()
        for section in ("itemOverviews", "currencyOverviews"):
            for ov in data.get(section, []):
                ov_type = ov.get("type")
                if ov_type:
                    type_to_lines[str(ov_type)] = ov.get("lines", [])

    try:
        cache_file.write_text(json.dumps({"_ts": now, "data": type_to_lines}), encoding="utf-8")
    except Exception:
        pass
    return type_to_lines


def _fetch_stash_overview(league: str, item_type: str, ttl_sec: int = 900) -> list[dict[str, Any]]:
    """Fetch detailed poe.ninja stash overview for a single item/currency type (15m disk cache)."""
    safe_league = re.sub(r"[^A-Za-z0-9_-]", "_", league)
    safe_type = re.sub(r"[^A-Za-z0-9_-]", "_", item_type)
    cache_file = _get_cache_dir() / f"ninja_stash_{safe_league}_{safe_type}.json"
    now = time.time()
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("_ts", 0) < ttl_sec and "lines" in cached:
                return cached["lines"]
        except Exception:
            pass

    lines: list[dict[str, Any]] = []
    with _make_httpx_client(timeout=30.0) as client:
        if item_type in CURRENCY_TYPES:
            resp = client.get(NINJA_STASH_CURRENCY_URL, params={"league": league, "type": item_type})
            resp.raise_for_status()
            payload = resp.json()
            for row in payload.get("lines", []) or []:
                recv = row.get("receive") or {}
                spark = row.get("receiveSparkLine") or {}
                lines.append(
                    {
                        "name": row.get("currencyTypeName") or row.get("name"),
                        "chaosValue": row.get("chaosEquivalent") or recv.get("value") or 0.0,
                        "listingCount": recv.get("listing_count") or recv.get("count"),
                        "count": recv.get("count"),
                        "sparkLine": spark,
                        "detailsId": row.get("detailsId"),
                    }
                )
        else:
            resp = client.get(NINJA_STASH_ITEM_URL, params={"league": league, "type": item_type})
            resp.raise_for_status()
            payload = resp.json()
            lines = payload.get("lines", []) or []

    try:
        cache_file.write_text(json.dumps({"_ts": now, "lines": lines}), encoding="utf-8")
    except Exception:
        pass
    return lines


def _should_auto_detail(query: str, variant: str | None) -> bool:
    combined = f"{query} {variant or ''}"
    return any(pat.search(combined) for pat in _AUTO_DETAIL_PATTERNS)


def _extract_cluster_passives(line: dict[str, Any]) -> int | None:
    variant = str(line.get("variant") or "")
    m = re.search(r"(\d+)\s*passives?", variant, re.IGNORECASE)
    if m:
        return int(m.group(1))
    for ti in line.get("tradeInfo") or []:
        if isinstance(ti, dict) and str(ti.get("mod") or "") == "enchant.stat_3086156145":
            val = ti.get("min") or ti.get("max")
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    pass
    return None


def _build_searchable_fields(line: dict[str, Any]) -> tuple[str, str, str]:
    """Return `(name_low, primary_blob, variant_blob)` for query & variant filtering."""
    name = str(line.get("name") or "")
    variant = str(line.get("variant") or "")
    details_id = str(line.get("detailsId") or "")
    base_type = str(line.get("baseType") or "")
    metadata = line.get("metadata") or {}
    meta_parts = [str(v) for v in metadata.values() if v] if isinstance(metadata, dict) else []

    extras: list[str] = []
    if line.get("corrupted") is True:
        extras.append("corrupted")
    elif line.get("corrupted") is False:
        extras.append("uncorrupted")
    gem_lv = line.get("gemLevel")
    gem_q = line.get("gemQuality")
    if gem_lv is not None:
        q_val = int(gem_q) if gem_q is not None else 0
        extras.append(f"{gem_lv}/{q_val}")
        if line.get("corrupted") is True:
            extras.append(f"{gem_lv}/{q_val}c")
    links = line.get("links")
    if links:
        extras.append(f"{links}l {links}-link")

    name_low = name.lower()
    primary_blob = " | ".join([name, variant, details_id, base_type, *extras]).lower()
    variant_blob = " | ".join([variant, details_id, name, base_type, *meta_parts, *extras]).lower()
    return name_low, primary_blob, variant_blob


def _matches_text_or_tokens(needle_low: str, blob: str) -> bool:
    if needle_low in blob:
        return True
    tokens = [tok for tok in re.split(r"\s+", needle_low) if tok]
    return len(tokens) > 1 and all(tok in blob for tok in tokens)


def _resolve_types_to_search(cat_key: str, available_keys: tuple[str, ...]) -> tuple[str, ...]:
    if cat_key == "all":
        return available_keys or ALL_OVERVIEW_TYPES
    if cat_key in CATEGORY_MAP:
        return CATEGORY_MAP[cat_key]
    for k in available_keys or ALL_OVERVIEW_TYPES:
        if k.lower() == cat_key:
            return (k,)
    return available_keys or ALL_OVERVIEW_TYPES


def query_price(
    query: str,
    *,
    league: str | None = None,
    category: str = "all",
    limit: int = 20,
    variant: str | None = None,
    detailed: bool = False,
) -> dict[str, Any]:
    """Search poe.ninja economy prices with 15-minute local disk cache.

    Supports `--variant` substring filtering and `--detailed` (or auto-upgraded)
    queries backed by `poe1/api/economy/stash/current/item/overview`.
    """
    q = query.strip().lower()
    if not q:
        raise ValueError("Price query cannot be empty.")

    v_low = variant.strip().lower() if variant and variant.strip() else None
    is_detailed = bool(detailed or _should_auto_detail(query, variant))

    resolved_league = league or resolve_current_trade_league()
    overviews = _fetch_dense_overviews(resolved_league)

    cat_key = category.strip().lower()
    types_to_search = _resolve_types_to_search(cat_key, tuple(overviews.keys()))

    divine_chaos_rate: float | None = None
    for line in overviews.get("Currency", []):
        if str(line.get("name") or "").lower() == "divine orb":
            divine_chaos_rate = float(line.get("chaos") or line.get("chaosValue") or 0.0)
            break

    if is_detailed:
        # Narrow types using dense overview index when searching 'all' so we don't fire 52 stash requests.
        if cat_key == "all" and overviews:
            q_tokens = [tok for tok in re.split(r"\s+", q) if len(tok) >= 3]
            narrowed: list[str] = []
            for ov_type in types_to_search:
                ov_lines = overviews.get(ov_type, [])
                matched_type = False
                for ln in ov_lines:
                    _, p_blob, _ = _build_searchable_fields(ln)
                    if _matches_text_or_tokens(q, p_blob) or (q_tokens and any(tok in p_blob for tok in q_tokens)):
                        matched_type = True
                        break
                if matched_type:
                    narrowed.append(ov_type)
            target_types = tuple(narrowed)
        else:
            target_types = types_to_search

        source_by_type: dict[str, list[dict[str, Any]]] = {}
        for ov_type in target_types:
            stash_lines = _fetch_stash_overview(resolved_league, ov_type)
            source_by_type[ov_type] = stash_lines if stash_lines else overviews.get(ov_type, [])
    else:
        target_types = types_to_search
        source_by_type = {ov_type: overviews.get(ov_type, []) for ov_type in target_types}

    matches: list[dict[str, Any]] = []
    for ov_type in target_types:
        for line in source_by_type.get(ov_type, []):
            name = str(line.get("name") or "")
            raw_variant = str(line.get("variant") or "")
            name_low, primary_blob, variant_blob = _build_searchable_fields(line)

            if not _matches_text_or_tokens(q, primary_blob):
                continue
            if v_low and not _matches_text_or_tokens(v_low, variant_blob):
                continue

            if q == name_low:
                score = 3.0
            elif name_low.startswith(q):
                score = 2.0
            elif q in primary_blob:
                score = 1.0
            else:
                score = 0.5

            chaos_val = float(line.get("chaos") or line.get("chaosValue") or 0.0)
            raw_div = line.get("divineValue")
            if raw_div is not None:
                div_val: float | None = float(raw_div)
            elif divine_chaos_rate and divine_chaos_rate > 0:
                div_val = chaos_val / divine_chaos_rate
            else:
                div_val = None

            spark_obj = line.get("sparkLine")
            if isinstance(spark_obj, dict) and spark_obj.get("totalChange") is not None:
                sparkline_change: float | None = round(float(spark_obj["totalChange"]), 2)
            else:
                graph = line.get("graph") or []
                latest_trend = graph[-1] if (isinstance(graph, list) and graph and graph[-1] is not None) else None
                sparkline_change = round(float(latest_trend), 1) if latest_trend is not None else None

            entry: dict[str, Any] = {
                "name": name,
                "type": ov_type,
                "variant": raw_variant or None,
                "baseType": line.get("baseType"),
                "detailsId": line.get("detailsId"),
                "chaosValue": chaos_val,
                "divineValue": div_val,
                "gemLevel": line.get("gemLevel"),
                "gemQuality": line.get("gemQuality"),
                "corrupted": line.get("corrupted"),
                "links": line.get("links"),
                "listingCount": line.get("listingCount") if line.get("listingCount") is not None else line.get("count"),
                "sparklineChange": sparkline_change,
                "trend24hPct": sparkline_change,
                "_score": score,
            }
            if ov_type == "ClusterJewel" or line.get("tradeInfo") is not None:
                entry["tradeInfo"] = line.get("tradeInfo")
                entry["clusterPassives"] = _extract_cluster_passives(line)
            matches.append(entry)

    matches.sort(key=lambda m: (float(m.get("_score") or 0), float(m.get("chaosValue") or 0.0)), reverse=True)
    for m in matches:
        m.pop("_score", None)

    return {
        "query": query,
        "league": resolved_league,
        "category": cat_key,
        "variant": variant,
        "detailed": is_detailed,
        "divineChaosRate": divine_chaos_rate,
        "totalMatches": len(matches),
        "matches": matches[:limit],
    }


def query_meta(
    *,
    league: str | None = None,
    skill: str | None = None,
    class_name: str | None = None,
    top: int = 20,
) -> dict[str, Any]:
    """Query poe.ninja build-index-state meta snapshot (total builds, ascendancies, top skills)."""
    data = _fetch_poe_ninja_build_index_state(ttl_sec=900)
    league_builds = data.get("leagueBuilds", []) or []
    if not league_builds:
        raise RuntimeError("poe.ninja build-index-state returned no leagueBuilds entries.")

    target_lb: dict[str, Any] | None = None
    if league and league.strip():
        needle = league.strip().lower()
        for lb in league_builds:
            if (
                str(lb.get("leagueName") or "").lower() == needle
                or str(lb.get("leagueUrl") or "").lower() == needle
            ):
                target_lb = lb
                break
        if target_lb is None:
            sample_names = ", ".join(str(x.get("leagueName")) for x in league_builds[:8])
            raise ValueError(f"League '{league}' not found in poe.ninja build-index-state (e.g. {sample_names}).")
    else:
        for lb in league_builds:
            if not lb.get("hardcore") and lb.get("category") == 0:
                target_lb = lb
                break
        if target_lb is None:
            target_lb = league_builds[0]

    total_builds = int(target_lb.get("total") or 0)
    stats = target_lb.get("statistics", []) or []

    skill_q = skill.strip().lower() if skill and skill.strip() else None
    class_q = class_name.strip().lower() if class_name and class_name.strip() else None

    asc_map: dict[str, dict[str, Any]] = {}
    skill_map: dict[str, dict[str, Any]] = {}
    combinations: list[dict[str, Any]] = []

    for row in stats:
        cls = str(row.get("class") or "")
        sk = str(row.get("skill") or "")
        pct = float(row.get("percentage") or 0.0)
        trend = row.get("trend")

        if class_q and class_q not in cls.lower():
            continue
        if skill_q and skill_q not in sk.lower():
            continue

        est_builds = round(total_builds * pct / 100.0)
        combinations.append(
            {
                "class": cls,
                "skill": sk,
                "percentage": round(pct, 2),
                "estimatedBuilds": est_builds,
                "trend": trend,
            }
        )

        if cls:
            a = asc_map.setdefault(cls, {"class": cls, "percentage": 0.0, "estimatedBuilds": 0, "skills": []})
            a["percentage"] += pct
            a["estimatedBuilds"] += est_builds
            if sk and sk not in a["skills"]:
                a["skills"].append(sk)

        if sk:
            s = skill_map.setdefault(sk, {"skill": sk, "percentage": 0.0, "estimatedBuilds": 0, "classes": []})
            s["percentage"] += pct
            s["estimatedBuilds"] += est_builds
            if cls and cls not in s["classes"]:
                s["classes"].append(cls)

    ascendancies = sorted(
        (
            {
                **v,
                "percentage": round(v["percentage"], 2),
            }
            for v in asc_map.values()
        ),
        key=lambda x: x["percentage"],
        reverse=True,
    )[:top]

    top_skills = sorted(
        (
            {
                **v,
                "percentage": round(v["percentage"], 2),
            }
            for v in skill_map.values()
        ),
        key=lambda x: x["percentage"],
        reverse=True,
    )[:top]

    combinations.sort(key=lambda x: x["percentage"], reverse=True)

    return {
        "league": target_lb.get("leagueName"),
        "leagueUrl": target_lb.get("leagueUrl"),
        "totalBuilds": total_builds,
        "hardcore": bool(target_lb.get("hardcore", False)),
        "filters": {"skill": skill, "className": class_name},
        "ascendancies": ascendancies,
        "topSkills": top_skills,
        "combinations": combinations[:top],
        "availableLeagues": [x.get("leagueName") for x in league_builds[:12] if x.get("leagueName")],
    }


def _strip_html(raw_html: str) -> str:
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    txt = re.sub(r"</?(br|p|li|tr|h[1-6]|div)[^>]*>", "\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"</?(td|th)[^>]*>", " | ", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html.unescape(txt)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in txt.splitlines()]
    cleaned = [ln for ln in lines if ln]
    return "\n".join(cleaned)


def query_wiki(topic: str, *, ttl_sec: int = 86400) -> dict[str, Any]:
    """Query poewiki.net MediaWiki API for mechanics/uniques/keystones with 24h local disk cache."""
    q = topic.strip()
    if not q:
        raise ValueError("Wiki topic cannot be empty.")

    slug_hash = hashlib.sha1(q.lower().encode("utf-8")).hexdigest()[:12]
    cache_file = _get_cache_dir() / f"poewiki_{slug_hash}.json"
    now = time.time()
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("_ts", 0) < ttl_sec and "data" in cached:
                return cached["data"]
        except Exception:
            pass

    api_url = "https://www.poewiki.net/w/api.php"
    with _make_httpx_client(timeout=20.0) as client:
        s_resp = client.get(
            api_url,
            params={
                "action": "opensearch",
                "search": q,
                "limit": 5,
                "namespace": 0,
                "format": "json",
            },
        )
        s_resp.raise_for_status()
        s_json = s_resp.json()
        titles: list[str] = s_json[1] if len(s_json) > 1 else []
        urls: list[str] = s_json[3] if len(s_json) > 3 else []

        if not titles:
            q_resp = client.get(
                api_url,
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": q,
                    "srlimit": 5,
                    "format": "json",
                },
            )
            q_resp.raise_for_status()
            s_hits = q_resp.json().get("query", {}).get("search", [])
            titles = [h["title"] for h in s_hits]
            urls = [f"https://www.poewiki.net/wiki/{t.replace(' ', '_')}" for t in titles]

        if not titles:
            raise ValueError(f"No poewiki.net page found for '{topic}'.")

        page_title = titles[0]
        page_url = urls[0] if urls else f"https://www.poewiki.net/wiki/{page_title.replace(' ', '_')}"

        p_resp = client.get(
            api_url,
            params={
                "action": "parse",
                "page": page_title,
                "prop": "text|sections",
                "redirects": 1,
                "format": "json",
            },
        )
        p_resp.raise_for_status()
        p_data = p_resp.json().get("parse", {})
        resolved_title = p_data.get("title") or page_title
        raw_html = p_data.get("text", {}).get("*", "")
        clean_text = _strip_html(raw_html)

        result = {
            "query": topic,
            "title": resolved_title,
            "url": page_url,
            "relatedTitles": titles[1:5],
            "summary": clean_text[:3500],
        }

    try:
        cache_file.write_text(json.dumps({"_ts": now, "data": result}), encoding="utf-8")
    except Exception:
        pass
    return result
