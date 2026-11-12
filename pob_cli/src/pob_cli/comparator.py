"""ROI rankings (rank-nodes, rank-gems), 3-baseline A/B build comparison, and cross-build borrowing."""

from __future__ import annotations

from typing import Any

from .analyzer import (
    dewater_build_in_place,
    inspect_tree,
    resolve_skill_group_index,
    resolve_slot_name,
)
from .bridge_client import BridgeWorker
from .formatters import fmt_delta, fmt_num, strip_pob_escapes
from .importers import load_build_into_worker
from .snapshot import CORE_STAT_KEYS, capture_digest, run_mutation

EXTERNAL_ENV_WHITELIST = (
    "enemyIsBoss",
    "enemyLevel",
    "deliriousPercentage",
    "presetBossSkills",
    "enemyPhysicalReduction",
    "enemyLightningResist",
    "enemyColdResist",
    "enemyFireResist",
    "enemyChaosResist",
    "enemyMaxResist",
    "EHPUnluckyWorstOf",
)

TWO_HANDED_TYPES = frozenset({
    "Bow",
    "Staff",
    "Warstaff",
    "Two Handed Sword",
    "Two Handed Axe",
    "Two Handed Mace",
    "Fishing Rod",
})


def _safe_dist(val: Any) -> int:
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val or "").strip()
    if s.isdigit():
        return int(s)
    return 9999


def _safe_float(val: Any) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val or "0").strip())
    except ValueError:
        return 0.0


def rank_nodes(
    worker: BridgeWorker,
    *,
    stat: str = "CombinedDPS",
    max_dist: int = 5,
    top: int = 15,
) -> dict[str, Any]:
    """Rank unallocated passive tree nodes by ROI (`pathPower` per point and raw `power`)."""
    with worker.lock:
        res = worker.call("node_power", {"stat": stat, "maxDepth": max_dist}, timeout=120.0)
        report = res.get("report", [])

        candidates = [
            r
            for r in report
            if not r.get("allocated")
            and _safe_dist(r.get("pathDist")) <= max_dist
            and (_safe_float(r.get("pathPower")) > 0 or _safe_float(r.get("power")) > 0)
        ]

        by_per_point = sorted(
            candidates,
            key=lambda r: (_safe_float(r.get("pathPower")), _safe_float(r.get("power"))),
            reverse=True,
        )[:top]

        by_single_node = sorted(
            candidates,
            key=lambda r: (_safe_float(r.get("power")), _safe_float(r.get("pathPower"))),
            reverse=True,
        )[:top]

        return {
            "stat": res.get("stat") or stat,
            "availableStats": res.get("stats", []),
            "maxDist": max_dist,
            "totalCandidates": len(candidates),
            "byPerPoint": by_per_point,
            "byTotalGain": by_single_node,
        }


def rank_gems(
    worker: BridgeWorker,
    *,
    group_selector: str = "main",
    slot_idx: int | None = None,
    add_link: bool = False,
    field: str = "CombinedDPS",
    top: int = 15,
) -> dict[str, Any]:
    """Rank candidate gems for a skill group slot (or a new link) by DPS gain."""
    with worker.lock:
        skills_res = worker.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        group = next(g for g in skills_res.get("groups", []) if g["index"] == g_idx)
        gems = group.get("gems", [])

        if add_link or slot_idx is None:
            target_idx = len(gems) + 1
            mode = f"add_link (slot #{target_idx})"
            supportable_only = True
        else:
            target_idx = int(slot_idx)
            mode = f"replace_slot (slot #{target_idx})"
            existing_gem = next((gm for gm in gems if gm.get("index") == target_idx), None)
            supportable_only = bool(existing_gem.get("support", True)) if existing_gem else True

        prev_opts = worker.call("get_gem_options", {})
        try:
            worker.call(
                "set_gem_options",
                {
                    "sortGemsByDPS": True,
                    "sortGemsByDPSField": field or "CombinedDPS",
                },
            )
            search_res = worker.call(
                "gem_search",
                {
                    "group": g_idx,
                    "index": target_idx,
                    "byDps": True,
                    "supportableOnly": supportable_only,
                    "limit": top,
                },
            )
            ranked = search_res.get("gems", [])
            # Enrich top 5 with candidate tooltip details
            for cand in ranked[:5]:
                if cand.get("gemId"):
                    try:
                        tip = worker.call(
                            "gem_candidate_tooltip",
                            {"group": g_idx, "index": target_idx, "gemId": cand["gemId"]},
                        )
                        cand["tooltipDps"] = tip.get("dps")
                    except Exception:
                        pass
        finally:
            try:
                restore_payload: dict[str, Any] = {}
                if "sortGemsByDPS" in prev_opts:
                    restore_payload["sortGemsByDPS"] = bool(prev_opts["sortGemsByDPS"])
                if prev_opts.get("sortGemsByDPSField"):
                    restore_payload["sortGemsByDPSField"] = prev_opts["sortGemsByDPSField"]
                if restore_payload:
                    worker.call("set_gem_options", restore_payload)
            except Exception:
                pass

        return {
            "groupIndex": g_idx,
            "groupLabel": group.get("displayLabelZh") or group.get("displayLabel") or group.get("label"),
            "targetIndex": target_idx,
            "mode": mode,
            "dpsField": search_res.get("dpsField") or field,
            "baseDps": search_res.get("baseDps"),
            "candidates": ranked,
        }


def _extract_env_config(worker: BridgeWorker) -> dict[str, Any]:
    cfg = worker.call("list_config", {})
    out: dict[str, Any] = {}
    for sec in cfg.get("sections", []):
        for item in sec.get("items", []):
            var = item.get("var")
            if var in EXTERNAL_ENV_WHITELIST:
                out[var] = item.get("value")
    return out


def _apply_env_config(worker: BridgeWorker, env_map: dict[str, Any]) -> None:
    for var in EXTERNAL_ENV_WHITELIST:
        val = env_map.get(var)
        if val in (None, False, ""):
            worker.call("reset_config", {"var": var})
        else:
            worker.call("set_config", {"var": var, "value": val})
    # Ensure presetBossSkills sees updated enemyIsBoss selIndex
    if env_map.get("presetBossSkills"):
        worker.call("set_config", {"var": "presetBossSkills", "value": env_map["presetBossSkills"]})


def _build_pair_table(main_dig: dict[str, Any], ref_dig: dict[str, Any]) -> list[dict[str, Any]]:
    m_stats = main_dig.get("stats", {})
    r_stats = ref_dig.get("stats", {})
    rows: list[dict[str, Any]] = []
    for key, label, unit in CORE_STAT_KEYS:
        mv = float(m_stats.get(key) or 0.0)
        rv = float(r_stats.get(key) or 0.0)
        if abs(mv) < 1e-4 and abs(rv) < 1e-4:
            continue
        rows.append(
            {
                "stat": key,
                "label": label,
                "main": mv,
                "ref": rv,
                "mainStr": f"{fmt_num(mv)}{unit}",
                "refStr": f"{fmt_num(rv)}{unit}",
                "diffRefVsMainStr": fmt_delta(mv, rv, unit),
            }
        )
    return rows


def compare_builds(
    main_worker: BridgeWorker,
    ref_worker: BridgeWorker,
    *,
    ref_source: str | None = None,
    ref_xml_code: str | None = None,
) -> dict[str, Any]:
    """Compare `main` build vs `ref` build across 3 baselines + tree, skill, and item diffs."""
    if ref_source or ref_xml_code:
        load_build_into_worker(ref_worker, source=ref_source, xml_code=ref_xml_code)

    with main_worker.lock, ref_worker.lock:
        # 1. Baseline ①: Original panels
        main_orig = capture_digest(main_worker)
        ref_orig = capture_digest(ref_worker)
        baseline_1 = _build_pair_table(main_orig, ref_orig)

        # Structural diffs (captured on original state)
        main_tree = inspect_tree(main_worker)
        ref_tree = inspect_tree(ref_worker)

        def _diff_node_lists(m_list: list[dict[str, Any]], r_list: list[dict[str, Any]]) -> dict[str, Any]:
            m_map = {
                int(n["id"]): strip_pob_escapes(n.get("nameZh") or n.get("name") or str(n["id"]))
                for n in m_list
            }
            r_map = {
                int(n["id"]): strip_pob_escapes(n.get("nameZh") or n.get("name") or str(n["id"]))
                for n in r_list
            }
            m_ids = set(m_map.keys())
            r_ids = set(r_map.keys())
            return {
                "onlyInMain": [{"id": i, "name": m_map[i]} for i in sorted(m_ids - r_ids)],
                "onlyInRef": [{"id": i, "name": r_map[i]} for i in sorted(r_ids - m_ids)],
                "sharedCount": len(m_ids & r_ids),
            }

        tree_diff = {
            "mainPoints": main_tree.get("points"),
            "refPoints": ref_tree.get("points"),
            "keystones": _diff_node_lists(main_tree.get("keystones", []), ref_tree.get("keystones", [])),
            "ascendancy": _diff_node_lists(main_tree.get("ascendancyNodes", []), ref_tree.get("ascendancyNodes", [])),
            "notables": _diff_node_lists(main_tree.get("notables", []), ref_tree.get("notables", [])),
            "masteries": _diff_node_lists(main_tree.get("masteries", []), ref_tree.get("masteries", [])),
        }

        # Skill diff
        main_skills = main_worker.call("list_skills", {})
        ref_skills = ref_worker.call("list_skills", {})

        def _active_skill_names(skills_res: dict[str, Any]) -> set[str]:
            names: set[str] = set()
            for g in skills_res.get("groups", []):
                if not g.get("enabled", True):
                    continue
                for gm in g.get("gems", []):
                    if gm.get("enabled", True) and not gm.get("support"):
                        nm = strip_pob_escapes(gm.get("nameZh") or gm.get("name") or gm.get("nameSpec") or "")
                        if nm:
                            names.add(nm)
            return names

        m_act = _active_skill_names(main_skills)
        r_act = _active_skill_names(ref_skills)
        m_main_idx = main_skills.get("mainSocketGroup") or 1
        r_main_idx = ref_skills.get("mainSocketGroup") or 1
        m_main_grp = next((g for g in main_skills.get("groups", []) if g["index"] == m_main_idx), None)
        r_main_grp = next((g for g in ref_skills.get("groups", []) if g["index"] == r_main_idx), None)

        skill_diff = {
            "mainGroupMain": m_main_grp,
            "refGroupMain": r_main_grp,
            "activeSkillsOnlyInMain": sorted(m_act - r_act),
            "activeSkillsOnlyInRef": sorted(r_act - m_act),
            "sharedActiveSkills": sorted(m_act & r_act),
        }

        # Item slot diff
        main_items_res = main_worker.call("list_items", {})
        ref_items_res = ref_worker.call("list_items", {})
        m_items_by_id = {it["id"]: it for it in main_items_res.get("items", [])}
        r_items_by_id = {it["id"]: it for it in ref_items_res.get("items", [])}
        m_slots = {s["name"]: s for s in main_items_res.get("slots", [])}
        r_slots = {s["name"]: s for s in ref_items_res.get("slots", [])}

        compare_slot_names = [
            "Weapon 1",
            "Weapon 2",
            "Helmet",
            "Body Armour",
            "Gloves",
            "Boots",
            "Amulet",
            "Ring 1",
            "Ring 2",
            "Belt",
            "Flask 1",
            "Flask 2",
            "Flask 3",
            "Flask 4",
            "Flask 5",
        ]
        slot_comparison: list[dict[str, Any]] = []
        for sname in compare_slot_names:
            ms = m_slots.get(sname, {})
            rs = r_slots.get(sname, {})
            m_it = m_items_by_id.get(ms.get("selItemId") or 0)
            r_it = r_items_by_id.get(rs.get("selItemId") or 0)
            slot_comparison.append(
                {
                    "slot": sname,
                    "mainItem": strip_pob_escapes(
                        (m_it.get("titleZh") or m_it.get("title") or m_it.get("nameZh") or m_it.get("name"))
                        if m_it
                        else "-"
                    ),
                    "mainRarity": m_it.get("rarity") if m_it else "-",
                    "refItem": strip_pob_escapes(
                        (r_it.get("titleZh") or r_it.get("title") or r_it.get("nameZh") or r_it.get("name"))
                        if r_it
                        else "-"
                    ),
                    "refRarity": r_it.get("rarity") if r_it else "-",
                }
            )

        # 2. Baseline ②: External target environment aligned to `main`
        main_env = _extract_env_config(main_worker)
        ref_snap = ref_worker.call("export_code", {})["code"]
        try:
            _apply_env_config(ref_worker, main_env)
            ref_env_aligned = capture_digest(ref_worker)
            baseline_2 = _build_pair_table(main_orig, ref_env_aligned)
        finally:
            ref_worker.call("import_code", {"code": ref_snap, "mode": "replace"})

        # 3. Baseline ③: Both de-watered + Pinnacle Boss
        main_snap = main_worker.call("export_code", {})["code"]
        try:
            main_stripped = dewater_build_in_place(main_worker)
            ref_stripped = dewater_build_in_place(ref_worker)
            main_dewatered = capture_digest(main_worker)
            ref_dewatered = capture_digest(ref_worker)
            baseline_3 = _build_pair_table(main_dewatered, ref_dewatered)
        finally:
            main_worker.call("import_code", {"code": main_snap, "mode": "replace"})
            ref_worker.call("import_code", {"code": ref_snap, "mode": "replace"})

        return {
            "mainBuildInfo": main_orig["buildInfo"],
            "refBuildInfo": ref_orig["buildInfo"],
            "baselines": {
                "original": baseline_1,
                "envAlignedToMain": {
                    "envConfig": main_env,
                    "rows": baseline_2,
                },
                "pinnacleDewatered": {
                    "mainStripped": main_stripped,
                    "refStripped": ref_stripped,
                    "rows": baseline_3,
                },
            },
            "treeDiff": tree_diff,
            "skillDiff": skill_diff,
            "itemSlotDiff": slot_comparison,
        }


def borrow_from_ref(
    main_worker: BridgeWorker,
    ref_worker: BridgeWorker,
    *,
    item_slots: list[str] | None = None,
    skill_group: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Borrow one or more equipment slots and/or a skill group from `ref` into `main`."""
    slots_to_borrow = [s.strip() for s in (item_slots or []) if s.strip()]
    if not slots_to_borrow and not skill_group:
        raise ValueError("Specify at least one of --item-slot or --skill-group to borrow.")

    with main_worker.lock, ref_worker.lock:
        ref_items_res = ref_worker.call("list_items", {})
        ref_items_by_id = {it["id"]: it for it in ref_items_res.get("items", [])}
        ref_slots_by_name = {s["name"]: s for s in ref_items_res.get("slots", [])}
        ref_skills_res = ref_worker.call("list_skills", {})

        # Pre-fetch raw items to borrow from ref
        borrowed_items_plan: list[dict[str, Any]] = []
        for s_query in slots_to_borrow:
            resolved = resolve_slot_name(ref_worker, s_query)
            r_slot = ref_slots_by_name.get(resolved)
            if not r_slot or not r_slot.get("selItemId"):
                raise ValueError(f"Reference build has no equipped item in slot '{resolved}'.")
            item_id = int(r_slot["selItemId"])
            raw_text = ref_worker.call("item_raw", {"id": item_id}).get("raw")
            borrowed_items_plan.append(
                {
                    "slotName": resolved,
                    "refItem": ref_items_by_id.get(item_id),
                    "raw": raw_text,
                }
            )

        # Pre-fetch skill group from ref if requested
        ref_group_obj: dict[str, Any] | None = None
        copied_group_texts: list[tuple[str, str | None]] = []
        borrow_main_flag = False
        if skill_group:
            g_idx = resolve_skill_group_index(ref_skills_res, skill_group)
            ref_group_obj = next(g for g in ref_skills_res.get("groups", []) if g["index"] == g_idx)
            borrow_main_flag = (
                str(skill_group).strip().lower() == "main"
                or bool(ref_group_obj.get("isMain"))
            )
            src_slot = ref_group_obj.get("slot")
            if ref_group_obj.get("source"):
                # If the skill group comes from an item (`source` is set) and its slot is not yet in borrowed_items_plan,
                # auto-borrow that item slot so the item-granted skill exists in `main`!
                if src_slot and not any(p["slotName"] == src_slot for p in borrowed_items_plan):
                    r_slot = ref_slots_by_name.get(src_slot)
                    if r_slot and r_slot.get("selItemId"):
                        item_id = int(r_slot["selItemId"])
                        raw_text = ref_worker.call("item_raw", {"id": item_id}).get("raw")
                        borrowed_items_plan.append(
                            {
                                "slotName": src_slot,
                                "refItem": ref_items_by_id.get(item_id),
                                "raw": raw_text,
                                "autoBorrowedForSkillSource": True,
                            }
                        )
                # Also copy any non-source support groups socketed into src_slot in ref
                if src_slot:
                    for sg in ref_skills_res.get("groups", []):
                        if not sg.get("source") and sg.get("slot") == src_slot and sg.get("enabled", True):
                            cp = ref_worker.call("copy_group", {"index": sg["index"]}).get("text")
                            if cp:
                                copied_group_texts.append((cp, src_slot))
            else:
                cp = ref_worker.call("copy_group", {"index": g_idx}).get("text")
                if cp:
                    copied_group_texts.append((cp, src_slot))

        def _do_borrow(w: BridgeWorker) -> dict[str, Any]:
            notices: list[str] = []
            equipped_items: list[dict[str, Any]] = []

            main_items_before = w.call("list_items", {})
            main_items_by_id = {it["id"]: it for it in main_items_before.get("items", [])}
            main_slots_by_name = {s["name"]: s for s in main_items_before.get("slots", [])}

            borrowed_slot_names = {p["slotName"] for p in borrowed_items_plan}

            for plan in borrowed_items_plan:
                target_slot = plan["slotName"]
                ref_item = plan.get("refItem") or {}
                ref_type = str(ref_item.get("type") or "")

                # Check 2H Weapon 1 vs Main's Weapon 2 conflict
                if target_slot == "Weapon 1" and ref_type in TWO_HANDED_TYPES and "Weapon 2" not in borrowed_slot_names:
                    w2_slot = main_slots_by_name.get("Weapon 2")
                    if w2_slot and w2_slot.get("selItemId"):
                        w.call("unequip_slot", {"slotName": "Weapon 2"})
                        notices.append(
                            f"Unequipped 'Weapon 2' in main build because borrowed 'Weapon 1' is Two-Handed ({ref_type})."
                        )
                elif target_slot == "Weapon 2" and "Weapon 1" not in borrowed_slot_names:
                    w1_slot = main_slots_by_name.get("Weapon 1")
                    w1_item = main_items_by_id.get(w1_slot.get("selItemId") or 0) if w1_slot else None
                    if w1_item and str(w1_item.get("type") or "") in TWO_HANDED_TYPES:
                        notices.append(
                            f"Warning: Borrowing 'Weapon 2' while main 'Weapon 1' has a Two-Handed weapon ({w1_item.get('type')})."
                        )

                if plan.get("autoBorrowedForSkillSource"):
                    notices.append(
                        f"Auto-borrowed source item in slot '{target_slot}' because skill group '{ref_group_obj.get('displayLabel')}' is granted by that item."
                    )

                add_res = w.call(
                    "add_item",
                    {"raw": plan["raw"], "slotName": target_slot, "equip": True},
                )
                equipped_items.append(
                    {
                        "slotName": target_slot,
                        "item": add_res.get("item"),
                    }
                )

            borrowed_skill_info = None
            if ref_group_obj:
                new_idx: int | None = None
                new_grp: dict[str, Any] = {}
                pasted_indices: list[int] = []

                for cp_text, target_slot_name in copied_group_texts:
                    paste_res = w.call("paste_group", {"text": cp_text})
                    p_idx = paste_res.get("index")
                    if p_idx is not None:
                        p_idx = int(p_idx)
                        pasted_indices.append(p_idx)
                        new_grp = paste_res.get("group") or {}
                        if target_slot_name:
                            set_res = w.call("set_group", {"index": p_idx, "slot": target_slot_name})
                            new_grp = set_res.get("group") or new_grp

                if ref_group_obj.get("source"):
                    # Locate the newly created item-granted source group in `main`
                    src_slot = ref_group_obj.get("slot")
                    skills_after = w.call("list_skills", {})
                    for g in skills_after.get("groups", []):
                        if g.get("source") and (not src_slot or g.get("slot") == src_slot):
                            new_idx = int(g["index"])
                            new_grp = g
                            break
                elif pasted_indices:
                    new_idx = pasted_indices[-1]

                switched_main = False
                if borrow_main_flag and new_idx is not None:
                    w.call("set_build_field", {"field": "mainSocketGroup", "value": int(new_idx)})
                    switched_main = True
                    notices.append(f"Switched mainSocketGroup to borrowed skill group #{new_idx}.")

                borrowed_skill_info = {
                    "newGroupIndex": new_idx,
                    "label": (
                        new_grp.get("displayLabelZh")
                        or new_grp.get("displayLabel")
                        or new_grp.get("label")
                        or ref_group_obj.get("displayLabelZh")
                        or ref_group_obj.get("displayLabel")
                        or ref_group_obj.get("label")
                    ),
                    "switchedMain": switched_main,
                }

            return {
                "borrowedItems": equipped_items,
                "borrowedSkillGroup": borrowed_skill_info,
                "notices": notices,
            }

        return run_mutation(main_worker, _do_borrow, apply=apply)
