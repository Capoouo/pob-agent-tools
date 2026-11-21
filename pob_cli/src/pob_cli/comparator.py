"""ROI rankings (rank-nodes, rank-gems), 3-baseline A/B build comparison, and cross-build borrowing."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
import re
from typing import Any, Iterable

from .analyzer import (
    dewater_build_in_place,
    inspect_tree,
    resolve_skill_group_index,
    resolve_slot_name,
    verify_restored,
)
from .bridge_client import BridgeWorker
from .formatters import _parse_numeric_or_none, fmt_delta, fmt_num, strip_pob_escapes
from .importers import load_build_into_worker
from .mutators import _apply_config_in_place
from .snapshot import (
    CORE_STAT_KEYS,
    _call_batch,
    _json_safe_stat,
    capture_digest,
    compact_digest_summary,
    diff_digest,
    run_mutation,
)

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
        group = next((g for g in skills_res.get("groups", []) if g.get("index") == g_idx), None)
        if group is None:
            raise ValueError(f"Skill group #{g_idx} not found.")
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
            # Enrich top 5 with candidate tooltip details in ONE pipelined batch
            top_cands = [c for c in ranked[:5] if c.get("gemId")]
            if top_cands:
                tip_calls = [
                    ("gem_candidate_tooltip", {"group": g_idx, "index": target_idx, "gemId": c["gemId"]})
                    for c in top_cands
                ]
                tips = _call_batch(worker, tip_calls, raise_on_error=False)
                for cand, tip in zip(top_cands, tips):
                    if isinstance(tip, dict) and not tip.get("_error") and tip.get("ok") is not False and "dps" in tip:
                        cand["tooltipDps"] = tip.get("dps")
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


def _extract_env_config_from_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sec in cfg.get("sections", []):
        for item in sec.get("items", []):
            var = item.get("var")
            if var in EXTERNAL_ENV_WHITELIST:
                out[var] = item.get("value")
    return out


def _extract_env_config(worker: BridgeWorker) -> dict[str, Any]:
    cfg = worker.call("list_config", {})
    return _extract_env_config_from_cfg(cfg)


def _apply_env_config(worker: BridgeWorker, env_map: dict[str, Any]) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    for var in EXTERNAL_ENV_WHITELIST:
        val = env_map.get(var)
        if val is None or val is False or val == "":
            calls.append(("reset_config", {"var": var}))
        else:
            calls.append(("set_config", {"var": var, "value": val}))
    # Ensure presetBossSkills sees updated enemyIsBoss selIndex
    if env_map.get("presetBossSkills"):
        calls.append(("set_config", {"var": "presetBossSkills", "value": env_map["presetBossSkills"]}))
    if calls:
        _call_batch(worker, calls)


def _build_pair_table(main_dig: dict[str, Any], ref_dig: dict[str, Any]) -> list[dict[str, Any]]:
    m_stats = main_dig.get("stats", {})
    r_stats = ref_dig.get("stats", {})
    rows: list[dict[str, Any]] = []
    for key, label, unit in CORE_STAT_KEYS:
        m_raw = m_stats.get(key)
        r_raw = r_stats.get(key)
        mf = _parse_numeric_or_none(m_raw)
        rf = _parse_numeric_or_none(r_raw)
        if mf is not None and rf is not None:
            if math.isfinite(mf) and math.isfinite(rf) and abs(mf) < 1e-4 and abs(rf) < 1e-4:
                continue
        elif m_raw is None and r_raw is None:
            continue
        rows.append(
            {
                "stat": key,
                "label": label,
                "main": _json_safe_stat(m_raw),
                "ref": _json_safe_stat(r_raw),
                "mainStr": f"{fmt_num(m_raw if m_raw is not None else 0.0)}{unit}",
                "refStr": f"{fmt_num(r_raw if r_raw is not None else 0.0)}{unit}",
                "diffRefVsMainStr": fmt_delta(
                    m_raw if m_raw is not None else 0.0,
                    r_raw if r_raw is not None else 0.0,
                    unit,
                ),
            }
        )
    return rows


def _run_worker_pair(
    main_worker: BridgeWorker,
    ref_worker: BridgeWorker,
    fn_main: Any,
    fn_ref: Any,
) -> tuple[Any, Any]:
    """Run independent main/ref evaluation closures concurrently unless both share a worker or lock."""
    same_lock = (main_worker is ref_worker) or (
        getattr(main_worker, "lock", None) is getattr(ref_worker, "lock", None)
    )
    if same_lock:
        return fn_main(), fn_ref()
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_main = pool.submit(fn_main)
        fut_ref = pool.submit(fn_ref)
        return fut_main.result(), fut_ref.result()


def compare_builds(
    main_worker: BridgeWorker,
    ref_worker: BridgeWorker,
    *,
    ref_source: str | None = None,
    ref_xml_code: str | None = None,
) -> dict[str, Any]:
    """Compare `main` build vs `ref` build across 3 baselines + tree, skill, and item diffs (concurrent main/ref)."""
    if ref_source or ref_xml_code:
        load_build_into_worker(ref_worker, source=ref_source, xml_code=ref_xml_code)

    # Phase 1: Capture original digests, trees, skills, items, main_env, and snapshot codes concurrently
    def _capture_main_phase1() -> dict[str, Any]:
        with main_worker.lock:
            prev_last_good = getattr(main_worker, "last_good_code", None)
            orig = capture_digest(main_worker)
            tree = inspect_tree(main_worker)
            skills, items, cfg, exp = _call_batch(
                main_worker,
                [
                    ("list_skills", {}),
                    ("list_items", {}),
                    ("list_config", {}),
                    ("export_code", {}),
                ],
            )
            if hasattr(main_worker, "last_good_code"):
                main_worker.last_good_code = prev_last_good
            return {
                "orig": orig,
                "tree": tree,
                "skills": skills,
                "items": items,
                "env": _extract_env_config_from_cfg(cfg),
                "snap": exp["code"],
                "prev_last_good": prev_last_good,
            }

    def _capture_ref_phase1() -> dict[str, Any]:
        with ref_worker.lock:
            prev_last_good = getattr(ref_worker, "last_good_code", None)
            orig = capture_digest(ref_worker)
            tree = inspect_tree(ref_worker)
            skills, items, exp = _call_batch(
                ref_worker,
                [
                    ("list_skills", {}),
                    ("list_items", {}),
                    ("export_code", {}),
                ],
            )
            if hasattr(ref_worker, "last_good_code"):
                ref_worker.last_good_code = prev_last_good
            return {
                "orig": orig,
                "tree": tree,
                "skills": skills,
                "items": items,
                "snap": exp["code"],
                "prev_last_good": prev_last_good,
            }

    main_p1, ref_p1 = _run_worker_pair(main_worker, ref_worker, _capture_main_phase1, _capture_ref_phase1)

    main_orig = main_p1["orig"]
    ref_orig = ref_p1["orig"]
    main_tree = main_p1["tree"]
    ref_tree = ref_p1["tree"]
    main_skills = main_p1["skills"]
    ref_skills = ref_p1["skills"]
    main_items_res = main_p1["items"]
    ref_items_res = ref_p1["items"]
    main_env = main_p1["env"]
    main_snap = main_p1["snap"]
    ref_snap = ref_p1["snap"]

    # 1. Baseline ①: Original panels
    baseline_1 = _build_pair_table(main_orig, ref_orig)

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

    # Phase 2: Concurrently evaluate Baseline ③ on `main` while `ref` runs Baseline ② + Baseline ③
    main_prev_last_good = main_p1["prev_last_good"]
    ref_prev_last_good = ref_p1["prev_last_good"]

    def _eval_main_phase2() -> tuple[dict[str, list[str]], dict[str, Any]]:
        with main_worker.lock:
            try:
                stripped = dewater_build_in_place(main_worker)
                dewatered = capture_digest(main_worker)
                return stripped, dewatered
            finally:
                main_worker.call("import_code", {"code": main_snap, "mode": "replace"})
                if hasattr(main_worker, "last_good_code"):
                    main_worker.last_good_code = main_prev_last_good

    def _eval_ref_phase2() -> tuple[dict[str, Any], dict[str, list[str]], dict[str, Any]]:
        with ref_worker.lock:
            try:
                _apply_env_config(ref_worker, main_env)
                env_aligned = capture_digest(ref_worker)
            finally:
                ref_worker.call("import_code", {"code": ref_snap, "mode": "replace"})
                if hasattr(ref_worker, "last_good_code"):
                    ref_worker.last_good_code = ref_prev_last_good

            try:
                stripped = dewater_build_in_place(ref_worker)
                dewatered = capture_digest(ref_worker)
                return env_aligned, stripped, dewatered
            finally:
                ref_worker.call("import_code", {"code": ref_snap, "mode": "replace"})
                if hasattr(ref_worker, "last_good_code"):
                    ref_worker.last_good_code = ref_prev_last_good

    (main_stripped, main_dewatered), (ref_env_aligned, ref_stripped, ref_dewatered) = _run_worker_pair(
        main_worker,
        ref_worker,
        _eval_main_phase2,
        _eval_ref_phase2,
    )

    baseline_2 = _build_pair_table(main_orig, ref_env_aligned)
    baseline_3 = _build_pair_table(main_dewatered, ref_dewatered)

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
            ref_group_obj = next((g for g in ref_skills_res.get("groups", []) if g.get("index") == g_idx), None)
            if ref_group_obj is None:
                raise ValueError(f"Reference skill group #{g_idx} not found.")
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


DEFAULT_MATRIX_SCENARIOS: list[str] = ["Original", "Pinnacle", "Uber", "FlasksOff"]


_MATRIX_TOKEN_SPLIT_RE = re.compile(
    r",\s*(?=[A-Za-z_][A-Za-z0-9_]*\s*=|(?:original|pinnacle|uber|flasks?_?(?:on|off)|no_?flasks)(?=\s*(?:,|$)))",
    re.IGNORECASE,
)


def _parse_matrix_scenario(spec: str | dict[str, Any]) -> dict[str, Any]:
    """Normalize a string or dict matrix scenario specification into a canonical execution plan."""
    if isinstance(spec, dict):
        name = str(spec.get("name") or "").strip()
        base_raw = str(spec.get("base") or "Original").strip()
        base_low = base_raw.lower()
        if base_low == "pinnacle":
            base = "Pinnacle"
        elif base_low == "uber":
            base = "Uber"
        elif base_low == "original":
            base = "Original"
        else:
            raise ValueError(f"Unsupported matrix base '{base_raw}' (expected 'Original', 'Pinnacle', or 'Uber').")

        cfg_raw = spec.get("config") or {}
        if isinstance(cfg_raw, dict):
            config_pairs = list(cfg_raw.items())
        elif isinstance(cfg_raw, list):
            config_pairs = [(k, v) for k, v in cfg_raw]
        else:
            raise ValueError("Matrix scenario 'config' must be a dict or list of (key, value) pairs.")

        flasks = spec.get("flasks")
        if flasks is not None:
            flasks = bool(flasks)

        return {
            "name": name or base,
            "base": base,
            "config": config_pairs,
            "flasks": flasks,
            "customMods": spec.get("customMods"),
            "appendCustomMods": spec.get("appendCustomMods"),
        }

    raw = str(spec or "").strip()
    if not raw:
        raise ValueError("Matrix scenario specification cannot be empty.")

    raw_low = raw.lower()
    if raw_low == "original":
        return {"name": "Original", "base": "Original", "config": [], "flasks": None, "customMods": None, "appendCustomMods": None}
    if raw_low == "pinnacle":
        return {"name": "Pinnacle", "base": "Pinnacle", "config": [], "flasks": None, "customMods": None, "appendCustomMods": None}
    if raw_low == "uber":
        return {"name": "Uber", "base": "Uber", "config": [], "flasks": None, "customMods": None, "appendCustomMods": None}
    if raw_low in ("flasksoff", "flasks_off", "noflasks", "no_flasks"):
        return {"name": "FlasksOff", "base": "Original", "config": [], "flasks": False, "customMods": None, "appendCustomMods": None}
    if raw_low in ("flaskson", "flasks_on"):
        return {"name": "FlasksOn", "base": "Original", "config": [], "flasks": True, "customMods": None, "appendCustomMods": None}

    # Check "Label:body" syntax (only if ':' appears before any '=')
    label = raw
    body = raw
    colon_idx = raw.find(":")
    eq_idx = raw.find("=")
    if colon_idx != -1 and (eq_idx == -1 or colon_idx < eq_idx):
        label = raw[:colon_idx].strip() or raw
        body = raw[colon_idx + 1 :].strip()

    base = "Original"
    flasks: bool | None = None
    config_pairs: list[tuple[str, Any]] = []
    custom_mods: str | None = None
    append_custom_mods: str | None = None

    tokens = [tok.strip() for tok in _MATRIX_TOKEN_SPLIT_RE.split(body) if tok.strip()]
    if not tokens:
        raise ValueError(f"Matrix scenario '{raw}' has an empty specification body.")

    for tok in tokens:
        t_low = tok.lower()
        if "=" in tok:
            k, v = tok.split("=", 1)
            k_clean = k.strip()
            v_clean = v.strip()
            if k_clean.lower() == "custommods":
                custom_mods = v_clean
            elif k_clean.lower() == "appendcustommods":
                append_custom_mods = v_clean
            elif k_clean.lower() == "flasks":
                flasks = v_clean.lower() in ("true", "1", "yes", "on")
            elif k_clean.lower() == "base":
                b_low = v_clean.lower()
                if b_low == "pinnacle":
                    base = "Pinnacle"
                elif b_low == "uber":
                    base = "Uber"
                elif b_low == "original":
                    base = "Original"
                else:
                    raise ValueError(f"Unsupported matrix base '{v_clean}' in scenario '{raw}'.")
            else:
                config_pairs.append((k_clean, v_clean))
        elif t_low == "original":
            base = "Original"
        elif t_low == "pinnacle":
            base = "Pinnacle"
        elif t_low == "uber":
            base = "Uber"
        elif t_low in ("flasksoff", "flasks_off", "noflasks", "no_flasks"):
            flasks = False
        elif t_low in ("flaskson", "flasks_on"):
            flasks = True
        else:
            raise ValueError(
                f"Unrecognized matrix token '{tok}' in scenario '{raw}'. "
                f"Use Original/Pinnacle/Uber/FlasksOff or key=value pairs."
            )

    return {
        "name": label,
        "base": base,
        "config": config_pairs,
        "flasks": flasks,
        "customMods": custom_mods,
        "appendCustomMods": append_custom_mods,
    }


def _apply_matrix_scenario_in_place(
    worker: BridgeWorker,
    plan: dict[str, Any],
    keep: Iterable[str] | str | None = None,
    *,
    already_dewatered: bool = False,
    cached_stripped: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Apply a single parsed matrix scenario in place on the worker.

    ``keep`` is forwarded to de-watering for Pinnacle/Uber bases.
    """
    applied_info: dict[str, Any] = {
        "base": plan["base"],
        "stripped": cached_stripped if already_dewatered else None,
        "config": [],
        "flasks": plan["flasks"],
    }

    if plan["base"] == "Pinnacle":
        if not already_dewatered:
            applied_info["stripped"] = dewater_build_in_place(worker, keep=keep)
    elif plan["base"] == "Uber":
        if not already_dewatered:
            applied_info["stripped"] = dewater_build_in_place(worker, keep=keep)
        uber_cfg = _apply_config_in_place(worker, [("enemyIsBoss", "Uber")])
        applied_info["config"].extend(uber_cfg.get("assignments", []))

    if plan["flasks"] is not None:
        active_flag = bool(plan["flasks"])
        _call_batch(
            worker,
            [("set_slot_active", {"slotName": f"Flask {idx}", "active": active_flag}) for idx in range(1, 6)],
        )

    if plan["config"] or plan["customMods"] is not None or plan["appendCustomMods"] is not None:
        cfg_res = _apply_config_in_place(
            worker,
            plan["config"],
            custom_mods=plan["customMods"],
            append_custom_mods=plan["appendCustomMods"],
        )
        applied_info["config"].extend(cfg_res.get("assignments", []))

    return applied_info


def _plan_has_post_base_mutations(plan: dict[str, Any]) -> bool:
    return bool(
        plan["base"] == "Uber"
        or plan["flasks"] is not None
        or plan["config"]
        or plan["customMods"] is not None
        or plan["appendCustomMods"] is not None
    )


def run_matrix(
    worker: BridgeWorker,
    scenarios: list[str | dict[str, Any]] | None = None,
    *,
    full: bool = False,
    keep: Iterable[str] | str | None = None,
) -> dict[str, Any]:
    """Evaluate multiple build scenarios in a single atomic 0-residue transaction.

    Caches the dewatered `Pinnacle` base build code once when 2+ scenarios require
    `Pinnacle`/`Uber` dewatering, forwards `keep` to dewatering, verifies rollback
    integrity via `verify_restored`, and emits compact `summary` + `coreStats` +
    `deltasVsFirst` by default (`full=False`, ~85% smaller JSON) while preserving
    full digests when `full=True`.
    """
    raw_scenarios = list(scenarios) if scenarios else list(DEFAULT_MATRIX_SCENARIOS)
    parsed_plans = [_parse_matrix_scenario(s) for s in raw_scenarios]
    pinnacle_count = sum(1 for p in parsed_plans if p["base"] in ("Pinnacle", "Uber"))
    first_is_unmutated_original = bool(
        parsed_plans
        and parsed_plans[0]["base"] == "Original"
        and not _plan_has_post_base_mutations(parsed_plans[0])
    )

    with worker.lock:
        prev_last_good = getattr(worker, "last_good_code", None)
        try:
            baseline_code = worker.call("export_code", {})["code"]
            baseline_digest: dict[str, Any] | None = (
                None if first_is_unmutated_original else capture_digest(worker)
            )
            results: list[dict[str, Any]] = []
            first_digest: dict[str, Any] | None = None

            pinnacle_code: str | None = None
            pinnacle_stripped: dict[str, list[str]] | None = None
            # Track current worker build state: "original", "pinnacle", or "dirty"
            worker_state = "original"
            ever_mutated = False

            try:
                for idx, plan in enumerate(parsed_plans):
                    needs_pinnacle = plan["base"] in ("Pinnacle", "Uber")
                    want_base = "pinnacle" if (needs_pinnacle and pinnacle_code is not None) else "original"

                    if worker_state != want_base:
                        target_code = pinnacle_code if want_base == "pinnacle" else baseline_code
                        worker_state = "dirty"
                        worker.call("import_code", {"code": target_code, "mode": "replace"})
                        worker_state = want_base

                    has_post_mutations = _plan_has_post_base_mutations(plan)

                    if needs_pinnacle and pinnacle_code is None:
                        # Mark dirty before mutating so finally always restores baseline_code on error
                        worker_state = "dirty"
                        ever_mutated = True
                        pinnacle_stripped = dewater_build_in_place(worker, keep=keep)
                        if pinnacle_count >= 2:
                            pinnacle_code = worker.call("export_code", {})["code"]
                            if not has_post_mutations:
                                worker_state = "pinnacle"
                        applied_info = _apply_matrix_scenario_in_place(
                            worker,
                            plan,
                            keep=keep,
                            already_dewatered=True,
                            cached_stripped=pinnacle_stripped,
                        )
                    elif needs_pinnacle and pinnacle_code is not None:
                        if has_post_mutations:
                            worker_state = "dirty"
                            ever_mutated = True
                        applied_info = _apply_matrix_scenario_in_place(
                            worker,
                            plan,
                            keep=keep,
                            already_dewatered=True,
                            cached_stripped=pinnacle_stripped,
                        )
                    else:
                        if has_post_mutations:
                            worker_state = "dirty"
                            ever_mutated = True
                        applied_info = _apply_matrix_scenario_in_place(worker, plan, keep=keep)

                    digest = capture_digest(worker)
                    if idx == 0 and first_is_unmutated_original:
                        baseline_digest = digest
                    if first_digest is None:
                        first_digest = digest
                    deltas_vs_first = diff_digest(first_digest, digest)

                    compact = compact_digest_summary(digest)
                    summary_only = {k: v for k, v in compact.items() if k != "coreStats"}
                    if full:
                        entry_digest = digest
                    else:
                        entry_digest = {
                            "primaryDpsKey": compact["primaryDpsKey"],
                            "primaryDps": compact["primaryDps"],
                            "totalEhp": compact["totalEhp"],
                            "life": compact["life"],
                            "warnings": compact["warnings"],
                        }

                    results.append(
                        {
                            "name": plan["name"],
                            "base": plan["base"],
                            "applied": applied_info,
                            "summary": summary_only,
                            "coreStats": compact["coreStats"],
                            "digest": entry_digest,
                            "deltasVsFirst": deltas_vs_first,
                        }
                    )
            finally:
                if worker_state != "original":
                    worker.call("import_code", {"code": baseline_code, "mode": "replace"})

            if not ever_mutated:
                restore_check: dict[str, Any] = {
                    "verified": True,
                    "detail": "build was not mutated",
                }
            else:
                restore_check = verify_restored(worker, baseline_code, baseline_digest or {})
        finally:
            if hasattr(worker, "last_good_code"):
                worker.last_good_code = prev_last_good

        return {
            "baseline": results[0]["name"] if results else "Original",
            "full": bool(full),
            "scenarios": results,
            "restoreCheck": restore_check,
        }
