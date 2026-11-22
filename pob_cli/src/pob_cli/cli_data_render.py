"""Markdown renderers for offline game data, PoEDB, poe.ninja prices, and PoE Wiki CLI outputs."""

from __future__ import annotations

from typing import Any


def _render_single_data_markdown(res: dict[str, Any], kind: str, query: str) -> str:
    from .formatters import format_game_data

    return format_game_data(res, kind=kind, query=query)


def _render_gem_quality_breakpoints_markdown(res: dict[str, Any]) -> str:
    from .formatters import render_table

    lines: list[str] = []
    for w in res.get("warnings") or []:
        if isinstance(w, dict):
            tag = "[POE1-PATCH-WARN]" if w.get("category") == "legacy_patch" else "[POE1-TRAP-WARN]"
            lines.append(f"> {tag} `{w.get('trigger', '')}`: {w.get('warning', '')}")
    if res.get("qualityRule"):
        lines.append(f"> **[PoE1 3.30 Gem Quality Rule]** {res['qualityRule']}")

    scope_lbl = "Active Skill Gems Only" if res.get("activeOnly") else "All Gems (Active + Support)"
    q_lbl = f" | Search: `{res['query']}`" if res.get("query") else ""
    lines.append(
        f"\n### PoE1 3.30 Gem Quality Breakpoints ({scope_lbl}{q_lbl}) — "
        f"showing `{res.get('count', 0)}` of `{res.get('totalMatched', 0)}` "
        f"(PoB `{res.get('pobVersion')}` / `{res.get('elapsedMs')}ms`)"
    )
    results = res.get("results") or []
    if not results:
        lines.append("_(no matching gem quality stats)_")
        return "\n".join(lines)

    rows: list[list[Any]] = []
    for idx, g in enumerate(results, start=1):
        name_en = str(g.get("name") or "-")
        name_zh = str(g.get("zhName") or "-")
        kind_s = "Support" if g.get("isSupport") else "Active"
        q_list = g.get("qualityStats") or []
        q0 = q_list[0] if q_list else {}
        stat_desc = "; ".join(str(q.get("label") or q.get("stat") or "") for q in q_list[:2])
        bp_note = "; ".join(str(q.get("breakpointNote") or "") for q in q_list[:2] if q.get("breakpointNote"))
        rows.append(
            [
                idx,
                name_zh,
                name_en,
                kind_s,
                q0.get("category") or "-",
                stat_desc[:90],
                bp_note[:70] or "-",
            ]
        )
    lines.append(
        render_table(
            ["#", "Gem (ZH)", "Gem (EN)", "Type", "Tier", "Quality Effect (@20% -> @40%)", "Breakpoint / Step"],
            rows,
        )
    )
    return "\n".join(lines)


def _render_tree_data_markdown(res: dict[str, Any]) -> str:
    from .formatters import render_table

    lines: list[str] = []
    for w in res.get("warnings") or []:
        if isinstance(w, dict):
            tag = "[POE1-PATCH-WARN]" if w.get("category") == "legacy_patch" else "[POE1-TRAP-WARN]"
            canon = f" (canonical: {w['canonicalTerm']})" if w.get("canonicalTerm") else ""
            lines.append(f"> {tag} `{w.get('trigger', '')}`{canon}: {w.get('warning', '')}")

    filters_obj = res.get("filters") if isinstance(res.get("filters"), dict) else {}
    filters: list[str] = []
    if res.get("queries"):
        filters.append("Q=`" + ", ".join(str(x) for x in res["queries"]) + "`")
    if filters_obj.get("type"):
        filters.append(f"Type=`{filters_obj['type']}`")
    if filters_obj.get("asc"):
        filters.append(f"Asc=`{filters_obj['asc']}`")
    filter_s = " | ".join(filters) or "all"
    lines.append(
        f"### Local Passive Tree Data (`Tree {res.get('treeVersion') or '3_30'}`): {filter_s} — "
        f"`{res.get('count', 0)}` hits (`{res.get('elapsedMs')}ms`)"
    )
    aliases = res.get("resolvedAliases") or []
    if aliases:
        lines.append("- **Resolved Slang**: " + ", ".join(f"`{a['from']}` -> `{a['to']}`" for a in aliases))
    results = res.get("results") or []
    if not results:
        lines.append("_(no matching passive tree nodes)_")
        return "\n".join(lines)

    rows: list[list[Any]] = []
    for n in results:
        name_en = str(n.get("name") or "-")
        name_zh = str(n.get("zhName") or "")
        disp = f"{name_zh} ({name_en})" if (name_zh and name_zh != name_en) else name_en
        asc_en = str(n.get("ascName") or n.get("asc") or "")
        asc_zh = str(n.get("ascNameZh") or n.get("ascZh") or "")
        if asc_zh and asc_en and asc_zh != asc_en:
            asc_disp = f"{asc_zh} ({asc_en})"
        else:
            asc_disp = asc_zh or asc_en or "-"
        if n.get("isBloodline"):
            asc_disp = f"{asc_disp} [Bloodline]"
        stats_lines = n.get("stats") or []
        if not stats_lines and n.get("masteryEffects"):
            stats_lines = ["; ".join(me.get("stats") or []) for me in (n.get("masteryEffects") or [])[:3]]
        stats_txt = "; ".join(str(s) for s in stats_lines[:6]) or "-"
        rows.append([n.get("id"), disp, n.get("type") or "-", asc_disp, stats_txt])
    lines.append(render_table(["ID", "Node", "Type", "Ascendancy", "Live 3.30 Stats"], rows))
    return "\n".join(lines)


def _render_tick_data_markdown(res: dict[str, Any]) -> str:
    from .formatters import render_table

    cd = res.get("current") or {}
    nb = cd.get("nextBreakpoint") or {}
    preset = res.get("preset") or res.get("presetLabel")
    label = f" (`{preset}`)" if preset else ""
    base_cd = res.get("baseCdSec")
    cdr_val = cd.get("globalCdrPct", 0)
    spells = res.get("triggeredSpells", 1)
    lines = [
        f"### PoE1 Server Tick Cooldown Calculator (`0.033s = 33ms` tick){label}",
        "- **Formula**: `ticks = ceil((base_cd / (1 + cdr/100)) / 0.033)`, `effective_cd = ticks * 0.033s` (NOT `1/30 = 33.33ms`)",
    ]
    if cd:
        lines.append(
            f"- **Current (`base={base_cd}s`, `CDR={cdr_val:g}%`, `spells={spells}`)**: "
            f"raw `{cd.get('rawCdSec')}s` -> **`{cd.get('ticks')}` ticks = `{cd.get('effectiveCdSec')}s` (`{cd.get('effectiveCdMs')}ms`)**, "
            f"cap **`{cd.get('effectiveCapPerSec')}/s`**"
            + (
                f" | **Next breakpoint**: `{nb.get('requiredCdrPct')}%` CDR (`+{nb.get('cdrNeededPct')}%` more) -> "
                f"`{nb.get('targetTicks')}` ticks = `{nb.get('effectiveCdMs')}ms` (`{nb.get('effectiveCapPerSec')}/s`)"
                if nb
                else " | at minimum 1-tick cap"
            )
        )
    if cd.get("aps") is not None:
        if cd.get("overspeed"):
            lines.append(f"> **[OVERSPEED WARNING]** {cd.get('overspeedNote')}")
        else:
            lines.append(
                f"- **APS Check**: OK — `{cd['aps']:.2f}/s` <= cap `{cd.get('effectiveCapPerSec')}/s` "
                f"(ratio `{cd.get('apsRatioToCap')}`)"
            )
    if res.get("cwcNote"):
        lines.append(f"- **CwC Reminder**: {res['cwcNote']}")

    bp_rows = [
        [
            f"{bp.get('ticks')}t" + (" *" if cd and bp.get("ticks") == cd.get("ticks") else ""),
            f"{bp.get('effectiveCdSec')}s ({bp.get('effectiveCdMs')}ms)",
            f"{bp.get('minCdrPct')}%",
            f"{bp.get('maxApsCap') or bp.get('maxTriggersPerSec')}/s",
        ]
        for bp in (res.get("breakpoints") or [])
    ]
    if bp_rows:
        lines.append("\n#### Exact `0.033s` Tick Breakpoint Table")
        lines.append(render_table(["Ticks", "Effective CD", "Min CDR %", "Max Triggers/s"], bp_rows))
    return "\n".join(lines)


def _render_single_poedb_markdown(res: dict[str, Any]) -> str:
    from .formatters import render_table

    if res.get("error"):
        return f"### PoEDB: `{res.get('query', '-')}` — Error: {res['error']}"

    lines = [
        f"### PoEDB: {res.get('title')} (`{res.get('slug')}`) — {res.get('url')}",
    ]
    if res.get("stale"):
        lines.append("> [WARN] Serving stale cached PoEDB response due to network error.")
    if res.get("category"):
        lines.append(f"- **Category**: {res['category']}")
    for prop in res.get("properties") or []:
        lines.append(f"- {prop}")
    if res.get("implicits"):
        lines.append("\n#### Implicits")
        for ln in res["implicits"]:
            lines.append(f"- {ln}")
    if res.get("explicits"):
        lines.append("\n#### Explicits")
        for ln in res["explicits"]:
            lines.append(f"- {ln}")

    vh = res.get("versionHistory") or []
    if vh:
        lines.append("\n#### Version History")
        v_rows = [[v.get("version"), "; ".join(v.get("changes") or [])[:140]] for v in vh[:10]]
        lines.append(render_table(["Version", "Changes"], v_rows))

    mods = res.get("mods")
    if mods is not None:
        pool_slug = res.get("modPoolSlug") or res.get("slug")
        total_m = res.get("modCountTotal", len(mods))
        lines.append(f"\n#### Mod Pool (`{pool_slug}` — showing {len(mods)} of {total_m} mods)")
        m_rows = []
        for m in mods:
            fam = ", ".join(m.get("family") or [])[:35] or "-"
            tags = ", ".join(m.get("fossilNo") or m.get("tags") or [])[:40] or "-"
            m_rows.append(
                [
                    m.get("side") or "-",
                    fam,
                    m.get("level") if m.get("level") is not None else "-",
                    (m.get("text") or "-")[:90],
                    m.get("dropChance") if m.get("dropChance") is not None else "-",
                    tags,
                ]
            )
        lines.append(render_table(["Side", "Family", "iLvl", "Mod Text", "Weight", "Tags (fossil_no)"], m_rows))

    return "\n".join(lines)


def _render_single_price_markdown(res: dict[str, Any], query: str) -> str:
    from .formatters import fmt_num, render_table

    if res.get("error"):
        return f"### poe.ninja Price Search: `{query}` — Error: {res['error']}"

    matches = res.get("matches", [])
    is_det = bool(res.get("detailed")) or any(
        m.get("gemLevel") is not None or m.get("corrupted") is not None for m in matches
    )
    div_rate = res.get("divineChaosRate")
    rate_str = f" | 1 Divine = `{fmt_num(div_rate, 1)}` Chaos" if div_rate else ""
    det_tag = " [Detailed]" if is_det else ""
    lines = [
        f"### poe.ninja Price Search{det_tag}: `{query}` (League: `{res.get('league')}`{rate_str}, {res.get('totalMatches', len(matches))} matches)"
    ]
    if res.get("stale"):
        lines.append("> [WARN] Serving stale cached poe.ninja price data due to network error.")

    if is_det:
        rows = []
        for m in matches:
            lv = m.get("gemLevel")
            ql = m.get("gemQuality")
            lv_q = f"{lv}/{ql or 0}" if lv is not None else "-"
            corr = "Yes" if m.get("corrupted") is True else ("No" if m.get("corrupted") is False else "-")
            links = f"{m['links']}L" if m.get("links") else "-"
            rows.append(
                [
                    m.get("name"),
                    m.get("type"),
                    m.get("variant") or "-",
                    lv_q,
                    corr,
                    links,
                    fmt_num(m.get("chaosValue"), 1),
                    fmt_num(m.get("divineValue"), 2) if m.get("divineValue") is not None else "-",
                    m.get("listingCount") if m.get("listingCount") is not None else "-",
                ]
            )
        lines.append(
            render_table(
                ["Item / Currency", "Category", "Variant", "Lvl/Qual", "Corrupted", "Links", "Chaos", "Divine", "Listings"],
                rows,
            )
        )
    else:
        rows = []
        for m in matches:
            trend = m.get("trend24hPct")
            trend_str = f"{float(trend):+.1f}%" if trend is not None else "-"
            rows.append(
                [
                    m.get("name"),
                    m.get("type"),
                    m.get("variant") or (f"{m['links']}L" if m.get("links") else "-"),
                    fmt_num(m.get("chaosValue"), 1),
                    fmt_num(m.get("divineValue"), 2) if m.get("divineValue") is not None else "-",
                    trend_str,
                    m.get("listingCount") if m.get("listingCount") is not None else "-",
                ]
            )
        lines.append(
            render_table(["Item / Currency", "Category", "Variant / Links", "Chaos", "Divine", "24h Trend", "Listings"], rows)
        )
    return "\n".join(lines)


def _render_single_wiki_markdown(res: dict[str, Any], topic: str) -> str:
    if res.get("error"):
        return f"### PoE Wiki: `{topic}` — Error: {res['error']}"
    parts = [f"### PoE Wiki: {res.get('title') or topic} ({res.get('url') or '-'})"]
    if res.get("stale"):
        parts.append("> [WARN] Serving stale cached PoE Wiki response due to network error.")
    if res.get("summary"):
        parts.append(str(res["summary"]))
    return "\n\n".join(parts)
