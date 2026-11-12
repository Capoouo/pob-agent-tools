"""External PoE 1 data queries: poe.ninja economy prices (15m cache) and poewiki.net MediaWiki API (24h cache)."""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .importers import USER_AGENT, _fetch_poe_ninja_index_state, _get_cache_dir

CURRENCY_TYPES = ("Currency", "Fragment")
ITEM_TYPES = (
    "UniqueWeapon",
    "UniqueArmour",
    "UniqueAccessory",
    "UniqueFlask",
    "UniqueJewel",
    "ForbiddenJewel",
    "UniqueTincture",
    "UniqueRelic",
    "UniqueMap",
    "SkillGem",
    "ClusterJewel",
    "DivinationCard",
    "BaseType",
    "Oil",
    "Scarab",
    "Tattoo",
    "Omen",
    "Fossil",
    "Resonator",
    "Essence",
    "Vial",
)

CATEGORY_MAP: dict[str, tuple[str, ...]] = {
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
    ),
    "weapon": ("UniqueWeapon",),
    "armour": ("UniqueArmour",),
    "accessory": ("UniqueAccessory",),
    "flask": ("UniqueFlask", "UniqueTincture"),
    "jewel": ("UniqueJewel", "ClusterJewel", "ForbiddenJewel"),
    "cluster": ("ClusterJewel",),
    "gem": ("SkillGem",),
    "card": ("DivinationCard",),
    "oil": ("Oil",),
    "scarab": ("Scarab",),
    "tattoo": ("Tattoo",),
    "omen": ("Omen",),
    "fossil": ("Fossil", "Resonator"),
    "essence": ("Essence",),
}


NINJA_DENSE_URL = "https://poe.ninja/poe1/api/economy/current/dense/overviews"
POE_LEAGUES_URL = "https://www.pathofexile.com/api/leagues?type=main&realm=pc"


def resolve_current_trade_league() -> str:
    """Resolve current primary softcore trade league name from poe.ninja index-state or PoE public leagues API."""
    try:
        state = _fetch_poe_ninja_index_state()
        econ_leagues = state.get("economyLeagues", [])
        if econ_leagues and econ_leagues[0].get("name"):
            return str(econ_leagues[0]["name"])
    except Exception:
        pass
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            resp = client.get(POE_LEAGUES_URL)
            if resp.status_code == 200:
                leagues = resp.json()
                for lg in leagues:
                    lid = str(lg.get("id") or "")
                    if lid and lid not in ("Standard", "Hardcore", "Solo Self-Found", "Hardcore SSF", "Ruthless"):
                        if not any(tag in lid for tag in ("Hardcore", "SSF", "Ruthless")):
                            return lid
    except Exception:
        pass
    return "Standard"


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
    with httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
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


def query_price(
    query: str,
    *,
    league: str | None = None,
    category: str = "all",
    limit: int = 20,
) -> dict[str, Any]:
    """Search poe.ninja dense economy overviews with a 15-minute local disk cache."""
    q = query.strip().lower()
    if not q:
        raise ValueError("Price query cannot be empty.")

    resolved_league = league or resolve_current_trade_league()
    overviews = _fetch_dense_overviews(resolved_league)

    cat_key = category.strip().lower()
    if cat_key == "all":
        types_to_search = tuple(overviews.keys())
    else:
        types_to_search = CATEGORY_MAP.get(cat_key) or tuple(overviews.keys())

    divine_chaos_rate: float | None = None
    for line in overviews.get("Currency", []):
        if str(line.get("name") or "").lower() == "divine orb":
            divine_chaos_rate = float(line.get("chaos") or 0.0)
            break

    matches: list[dict[str, Any]] = []
    for ov_type in types_to_search:
        for line in overviews.get(ov_type, []):
            name = str(line.get("name") or "")
            variant = str(line.get("variant") or "")
            name_low = name.lower()
            if q not in name_low and q not in variant.lower():
                continue
            score = 3 if q == name_low else (2 if name_low.startswith(q) else 1)
            chaos_val = float(line.get("chaos") or line.get("chaosValue") or 0.0)
            div_val = (chaos_val / divine_chaos_rate) if (divine_chaos_rate and divine_chaos_rate > 0) else None
            graph = line.get("graph") or []
            latest_trend = graph[-1] if (isinstance(graph, list) and graph and graph[-1] is not None) else None
            matches.append(
                {
                    "name": name,
                    "type": ov_type,
                    "variant": variant or None,
                    "links": None,
                    "chaosValue": chaos_val,
                    "divineValue": div_val,
                    "trend24hPct": round(float(latest_trend), 1) if latest_trend is not None else None,
                    "listingCount": line.get("count") or line.get("listingCount"),
                    "_score": score,
                }
            )

    matches.sort(key=lambda m: (int(m.get("_score") or 0), float(m.get("chaosValue") or 0.0)), reverse=True)
    for m in matches:
        m.pop("_score", None)

    return {
        "query": query,
        "league": resolved_league,
        "category": cat_key,
        "divineChaosRate": divine_chaos_rate,
        "totalMatches": len(matches),
        "matches": matches[:limit],
    }


def _strip_html(raw_html: str) -> str:
    # Remove style/script tags
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    # Replace <br>, </p>, </li>, </tr>, </h1..6> with newlines
    txt = re.sub(r"</?(br|p|li|tr|h[1-6]|div)[^>]*>", "\n", txt, flags=re.IGNORECASE)
    # Replace <td>, <th> with tab/space
    txt = re.sub(r"</?(td|th)[^>]*>", " | ", txt, flags=re.IGNORECASE)
    # Strip remaining tags
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html.unescape(txt)
    # Collapse excessive blank lines
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
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        # 1. OpenSearch to resolve exact page title
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
            # Fallback to action=query&list=search
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

        # 2. Fetch parsed HTML content of the page (sections 0..2 or full text truncated)
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
