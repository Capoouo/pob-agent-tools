"""poe.ninja build-index-state meta snapshot and poewiki.net MediaWiki API queries."""

from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
import re
import sys
import time
from typing import Any

import httpx

from ..importers import (
    DEFAULT_HTTP_TIMEOUT,
    _atomic_write_json,
    _make_httpx_client,
    _shared_cache_dir,
    read_stale_cache,
)


def _pkg() -> Any:
    return sys.modules["pob_cli.external_data"]


def query_meta(
    *,
    league: str | None = None,
    skill: str | None = None,
    class_name: str | None = None,
    top: int = 20,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Query poe.ninja build-index-state meta snapshot (total builds, ascendancies, top skills)."""
    cache_file = _shared_cache_dir() / "poe_ninja_build_index_state.json"
    is_stale = False
    pre_ts: float | None = None
    if cache_file.exists():
        try:
            pre_ts = float(json.loads(cache_file.read_text(encoding="utf-8")).get("_ts", 0))
        except Exception:
            pre_ts = None

    fetch_build_state = _pkg()._fetch_poe_ninja_build_index_state
    try:
        data = (
            fetch_build_state(ttl_sec=900, proxy=proxy)
            if proxy is not None
            else fetch_build_state(ttl_sec=900)
        )
        # Detect if importers._fetch_poe_ninja_build_index_state silently fell back to an expired cache
        if pre_ts is not None and (time.time() - pre_ts) >= 900 and cache_file.exists():
            try:
                post_ts = float(json.loads(cache_file.read_text(encoding="utf-8")).get("_ts", 0))
                if post_ts == pre_ts:
                    is_stale = True
            except Exception:
                pass
    except (httpx.HTTPError, OSError):
        stale_data, _ = read_stale_cache(cache_file, data_key="data")
        if isinstance(stale_data, dict):
            data = stale_data
            is_stale = True
        else:
            raise
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

    out: dict[str, Any] = {
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
    if is_stale:
        out["stale"] = True
    return out


def _strip_html(raw_html: str) -> str:
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    txt = re.sub(r"</?(br|p|li|tr|h[1-6]|div)[^>]*>", "\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"</?(td|th)[^>]*>", " | ", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html.unescape(txt)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in txt.splitlines()]
    cleaned = [ln for ln in lines if ln]
    return "\n".join(cleaned)


def query_wiki(
    topic: str,
    *,
    ttl_sec: int = 86400,
    proxy: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Query poewiki.net MediaWiki API for mechanics/uniques/keystones with 24h local disk cache."""
    from ..guardrails import GuardrailScope, check_input, validate_url

    q = topic.strip()
    if not q:
        raise ValueError("Wiki topic cannot be empty.")
    validate_url(q, expected_game="poe1", cli_subcommand="wiki")
    check_input(q, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")

    slug_hash = hashlib.sha1(q.lower().encode("utf-8")).hexdigest()[:12]
    cache_file = _shared_cache_dir() / f"poewiki_{slug_hash}.json"
    now = time.time()
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("_ts", 0) < ttl_sec and "data" in cached:
                return cached["data"]
        except Exception:
            pass

    api_url = "https://www.poewiki.net/w/api.php"

    def _fetch_with_client(http_client: httpx.Client) -> dict[str, Any]:
        s_resp = http_client.get(
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
            q_resp = http_client.get(
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

        p_resp = http_client.get(
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

        return {
            "query": topic,
            "title": resolved_title,
            "url": page_url,
            "relatedTitles": titles[1:5],
            "summary": clean_text[:3500],
        }

    try:
        if client is not None:
            result = _fetch_with_client(client)
        else:
            with _make_httpx_client(timeout=DEFAULT_HTTP_TIMEOUT, proxy=proxy) as own_client:
                result = _fetch_with_client(own_client)
    except (httpx.HTTPError, OSError):
        stale_data, _ = read_stale_cache(cache_file, data_key="data")
        if isinstance(stale_data, dict):
            return {**stale_data, "stale": True}
        raise

    _atomic_write_json(cache_file, {"_ts": now, "data": result})
    return result


def query_wiki_multi(
    queries: list[str],
    *,
    ttl_sec: int = 86400,
    proxy: str | None = None,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Query multiple poewiki.net topics concurrently using ONE shared httpx.Client."""
    from ..guardrails import GuardrailScope, check_input, validate_url

    clean_queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    if not clean_queries:
        raise ValueError("Wiki queries cannot be empty.")
    for q_item in clean_queries:
        validate_url(q_item, expected_game="poe1", cli_subcommand="wiki")
        check_input(q_item, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")

    now = time.time()
    cached_hits: dict[str, dict[str, Any]] = {}
    uncached: list[str] = []
    for q in clean_queries:
        slug_hash = hashlib.sha1(q.lower().encode("utf-8")).hexdigest()[:12]
        cache_file = _shared_cache_dir() / f"poewiki_{slug_hash}.json"
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if now - cached.get("_ts", 0) < ttl_sec and "data" in cached:
                    cached_hits[q] = cached["data"]
                    continue
            except Exception:
                pass
        if q not in uncached:
            uncached.append(q)

    fetched: dict[str, dict[str, Any]] = {}
    wiki_fn = _pkg().query_wiki
    if uncached:
        with _make_httpx_client(timeout=DEFAULT_HTTP_TIMEOUT, proxy=proxy) as shared_client:
            def _run_one(topic: str) -> tuple[str, dict[str, Any]]:
                try:
                    return topic, wiki_fn(
                        topic, ttl_sec=ttl_sec, proxy=proxy, client=shared_client
                    )
                except Exception as exc:
                    return topic, {"query": topic, "error": str(exc)}

            if len(uncached) == 1:
                t, res = _run_one(uncached[0])
                fetched[t] = res
            else:
                workers = max(1, min(int(max_workers), len(uncached)))
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    for t, res in pool.map(_run_one, uncached):
                        fetched[t] = res

    results = [cached_hits[q] if q in cached_hits else fetched[q] for q in clean_queries]
    out: dict[str, Any] = {
        "queries": clean_queries,
        "count": len(results),
        "results": results,
    }
    if any(bool(r.get("stale")) for r in results):
        out["stale"] = True
    return out
