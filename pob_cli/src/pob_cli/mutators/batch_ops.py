"""Composite batch op handlers and dispatcher (`_execute_batch_ops`)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..analyzer import resolve_skill_group_index
from ..bridge_client import BridgeWorker
from ..guardrails import GuardrailScope, check_input, check_input_item_raw, validate_url
from .build_state import (
    _apply_class_ascendancy_in_place,
    _apply_new_build_in_place,
    _apply_switch_main_in_place,
)
from .common import (
    _BatchCache,
    _call_batch,
    _check_agnostic_dealloc_caution,
    _is_jewel_slot_name,
    _merge_cautions,
    _resolve_slot_from_items,
    _validate_gem_level,
)
from .config import _apply_config_in_place
from .item_edit import _apply_edit_item_mods_in_place
from .items import _apply_anoint_in_place, _parse_flask_slots, _search_item_db_items
from .skills import _apply_add_group_in_place, _apply_replace_group_in_place, _guard_source_skill_group
from .skills_resolve import (
    _add_gem_with_default_skill_flags,
    _resolve_gem_candidate,
    _resolve_gem_in_group,
)
from .tree import _apply_tree_changes_in_place, _resolve_node_token

_BatchOpHandler = Callable[[BridgeWorker, dict[str, Any], _BatchCache, int], dict[str, Any]]


def _op_config(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    set_map = op.get("set")
    if set_map is not None and not isinstance(set_map, dict):
        raise ValueError(f"Batch op 'config' at step {idx} expects 'set' to be a JSON object.")
    reset_list = op.get("reset")
    if reset_list is not None and not isinstance(reset_list, list):
        raise ValueError(f"Batch op 'config' at step {idx} expects 'reset' to be a JSON list.")
    pairs = list((set_map or {}).items())
    for var in reset_list or []:
        pairs.append((var, "reset"))
    cm = op.get("customMods") if "customMods" in op else op.get("custom_mods")
    acm = op.get("appendCustomMods") if "appendCustomMods" in op else op.get("append_custom_mods")
    cfg_res = _apply_config_in_place(
        w,
        pairs,
        custom_mods=str(cm) if cm is not None else None,
        append_custom_mods=str(acm) if acm is not None else None,
        cache=cache,
    )
    return {"step": idx, "op": "config", "count": len(cfg_res["assignments"])}


def _op_flask(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    f_sel = op.get("index", "all")
    act = bool(op.get("active", True))
    slots = _parse_flask_slots(f_sel)
    _call_batch(w, [("set_slot_active", {"slotName": s_name, "active": act}) for s_name in slots])
    cache.invalidate("items")
    return {"step": idx, "op": "flask", "slotName": ",".join(slots), "active": act}


def _op_item(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    raw_val = op.get("raw")
    raw_text = str(raw_val).strip() if raw_val is not None else ""
    if not raw_text:
        raise ValueError(f"Batch op 'item' at step {idx} requires non-empty 'raw'.")
    slot_query = op.get("slot") or op.get("slotName")
    if slot_query:
        check_input(str(slot_query), scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input_item_raw(raw_text, expected_game="poe1")
    slot_name = _resolve_slot_from_items(cache.get_items(), str(slot_query)) if slot_query else None
    params: dict[str, Any] = {"raw": raw_text, "equip": True}
    if slot_name:
        params["slotName"] = slot_name
    res = w.call("add_item", params)
    cache.invalidate("items", "skills")
    if _is_jewel_slot_name(slot_name) or "jewel" in str((res.get("item") or {}).get("type") or "").lower():
        cache.invalidate("tree")
    return {"step": idx, "op": "item", "slotName": slot_name, "item": res.get("item")}


def _op_item_db(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    query = str(op.get("query") or "").strip()
    db_type = str(op.get("dbType") or op.get("db_type") or op.get("kind") or "UNIQUE")
    item_index = op.get("index") if op.get("index") is not None else op.get("itemIndex", op.get("item_index"))
    slot_query = op.get("slot") or op.get("slotName")
    if slot_query:
        check_input(str(slot_query), scope=GuardrailScope.SLOT, expected_game="poe1")
    if query:
        check_input(query, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    resolved_slot = _resolve_slot_from_items(cache.get_items(), str(slot_query)) if slot_query else None
    db_kind, items, _ = _search_item_db_items(w, query=query, db_type=db_type, limit=max(15, int(item_index or 1)))
    if not items:
        raise ValueError(f"No {db_kind} database items matched query '{query}' at step {idx}.")
    pick_idx = int(item_index) if item_index is not None else 1
    if pick_idx < 1 or pick_idx > len(items):
        raise ValueError(f"item_db index {pick_idx} out of range (1..{len(items)}) at step {idx}.")
    chosen = items[pick_idx - 1]
    chosen_raw = str(chosen.get("raw") or "").strip()
    if not chosen_raw:
        raise RuntimeError(f"Selected DB item '{chosen.get('title') or chosen.get('name')}' has no raw text.")
    add_params: dict[str, Any] = {"raw": chosen_raw, "equip": True}
    if resolved_slot:
        add_params["slotName"] = resolved_slot
    res = w.call("add_item", add_params)
    cache.invalidate("items", "skills")
    if _is_jewel_slot_name(resolved_slot) or "jewel" in str(chosen.get("type") or "").lower():
        cache.invalidate("tree")
    return {
        "step": idx,
        "op": "item_db",
        "query": query,
        "slotName": resolved_slot or (res.get("item") or {}).get("primarySlot"),
        "item": res.get("item"),
    }


def _op_item_edit(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    slot_query = str(op.get("slot") or op.get("slotName") or "").strip()
    raw_reps = op.get("replaceMods") or op.get("replace_mods") or []
    rep_tuples = [(str(p[0]), str(p[1])) for p in raw_reps if isinstance(p, (list, tuple)) and len(p) == 2]
    edit_res = _apply_edit_item_mods_in_place(
        w,
        slot_name=slot_query,
        replace_mods=rep_tuples or None,
        add_mods=op.get("addMods") or op.get("add_mods"),
        remove_mods=op.get("removeMods") or op.get("remove_mods"),
        cache=cache,
    )
    return {"step": idx, "op": "item_edit", "slotName": edit_res["slotName"], "changes": edit_res["changes"]}


def _op_unequip(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    slot_query = str(op.get("slot") or op.get("slotName") or "").strip()
    if not slot_query:
        raw_kind = str(op.get("op") or op.get("type") or "unequip")
        raise ValueError(f"Batch op '{raw_kind}' at step {idx} requires 'slot'.")
    check_input(slot_query, scope=GuardrailScope.SLOT, expected_game="poe1")
    resolved_slot = _resolve_slot_from_items(cache.get_items(), slot_query)
    w.call("unequip_slot", {"slotName": resolved_slot})
    cache.invalidate("items", "skills")
    if _is_jewel_slot_name(resolved_slot):
        cache.invalidate("tree")
    return {"step": idx, "op": "unequip", "slotName": resolved_slot}


def _op_anoint(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    notable_val = op.get("notable")
    notable_text = str(notable_val).strip() if notable_val is not None else ""
    if not notable_text:
        raise ValueError(f"Batch op 'anoint' at step {idx} requires non-empty 'notable'.")
    an_res = _apply_anoint_in_place(
        w,
        slot_name=str(op.get("slot") or "Amulet"),
        notable_query=notable_text,
        anoint_index=int(op["index"]) if op.get("index") is not None else 1,
        cache=cache,
    )
    return {"step": idx, "op": "anoint", "notable": an_res.get("notable")}


def _op_tree(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    tree_state = cache.get_tree()
    nodes_db = w.get_cached_tree_data(tree_state.get("treeVersion")).get("nodes", {})
    dynamic_map = {str(n["id"]): n for n in tree_state.get("dynamicNodes", [])}
    resolved_alloc = [_resolve_node_token(w, str(tok), nodes_db, dynamic_map) for tok in (op.get("allocate") or [])]
    resolved_dealloc = [
        _resolve_node_token(w, str(tok), nodes_db, dynamic_map) for tok in (op.get("deallocate") or [])
    ]
    allow_casc = bool(op["allowCascade"] if "allowCascade" in op else op.get("allow_cascade", True))
    tree_cautions = _check_agnostic_dealloc_caution(w, {nid for nid, _ in resolved_dealloc}, cache=cache)
    actions_taken, final_state, orphaned_nodes, points_delta, cascade_warning = _apply_tree_changes_in_place(
        w, resolved_alloc, resolved_dealloc, initial_tree_state=tree_state, allow_cascade=allow_casc
    )
    cache.tree = final_state
    cache.invalidate("items", "skills")
    step_entry: dict[str, Any] = {
        "step": idx,
        "op": "tree",
        "actions": actions_taken,
        "pointsDelta": points_delta,
        "orphanedNodes": orphaned_nodes,
    }
    if cascade_warning:
        step_entry["cascadeWarning"] = cascade_warning
    if tree_cautions:
        step_entry["cautions"] = tree_cautions
    return step_entry


def _op_tree_reset(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    tattoos = bool(op.get("tattoos", False))
    res = w.call("reset_tree", {"tattoos": True} if tattoos else {})
    if isinstance(res, dict) and "allocatedNodes" in res:
        cache.tree = res
    else:
        cache.invalidate("tree")
    cache.invalidate("items", "skills")
    return {"step": idx, "op": "tree_reset", "tattoos": tattoos, "points": res.get("points")}


def _op_import_tree_url(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    url = str(op.get("url") or "").strip()
    title = str(op.get("title") or op.get("name") or "Imported Tree").strip()
    if not url:
        raise ValueError(f"Batch op 'import_tree_url' at step {idx} requires 'url'.")
    validate_url(url, expected_game="poe1", cli_subcommand="batch")
    specs_res = w.call("import_tree_url", {"url": url, "title": title})
    cache.invalidate("tree", "classes", "items", "skills")
    cache.specs = specs_res
    return {"step": idx, "op": "import_tree_url", "title": title, "activeSpec": specs_res.get("activeSpec")}


def _op_new_build(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    nb_res = _apply_new_build_in_place(
        w,
        class_name=op.get("class") or op.get("className") or op.get("class_name"),
        ascendancy=op.get("ascendancy") or op.get("asc") or op.get("ascendancyName") or op.get("ascendancy_name"),
        secondary_ascendancy=(
            op.get("secondaryAscendancy") or op.get("secondary_ascendancy") or op.get("secondaryAsc")
        ),
        level=int(op["level"]) if op.get("level") is not None else None,
        name=op.get("name"),
        cache=cache,
    )
    return {"step": idx, "op": "new_build", "changes": nb_res["changes"], "level": nb_res["level"]}


def _op_set_level(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    raw_lv = op.get("level") if op.get("level") is not None else op.get("value")
    if raw_lv is None and "auto" not in op:
        raise ValueError(f"Batch op 'set_level' at step {idx} requires 'level' or 'auto'.")
    lv_val: int | None = None
    if raw_lv is not None:
        lv_val = int(raw_lv)
        if lv_val < 1 or lv_val > 100:
            raise ValueError(f"Character level must be between 1 and 100 (got {lv_val}).")
        w.call("set_build_field", {"field": "level", "value": lv_val})
    if "auto" in op:
        w.call("set_build_field", {"field": "levelAuto", "value": bool(op["auto"])})
    cache.invalidate("tree", "config", "items", "skills")
    return {"step": idx, "op": "set_level", "level": lv_val, "auto": op.get("auto")}


def _op_set_class(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    cls_res = _apply_class_ascendancy_in_place(
        w,
        class_name=op.get("class") or op.get("className") or op.get("class_name"),
        ascendancy_name=(
            op.get("ascendancy") or op.get("asc") or op.get("ascendancyName") or op.get("ascendancy_name")
        ),
        secondary_ascendancy_name=(
            op.get("secondaryAscendancy")
            or op.get("secondary_ascendancy")
            or op.get("secondaryAsc")
            or op.get("secondaryAscendancyName")
        ),
        cache=cache,
    )
    return {"step": idx, "op": "set_class", "changes": cls_res["changes"], "after": cls_res["after"]}


def _op_switch_main(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    sw_res = _apply_switch_main_in_place(
        w,
        spec_index=op.get("spec") if "spec" in op else op.get("specIndex", op.get("spec_index")),
        skill_group=(
            op.get("skillGroup") if "skillGroup" in op else op.get("skill_group", op.get("group"))
        ),
        main_skill_index=(
            op.get("mainSkill") if "mainSkill" in op else op.get("mainSkillIndex", op.get("main_skill_index"))
        ),
        skill_part_index=(
            op.get("skillPart") if "skillPart" in op else op.get("skillPartIndex", op.get("skill_part_index"))
        ),
        item_set_id=op.get("itemSet") if "itemSet" in op else op.get("itemSetId", op.get("item_set_id")),
        skill_set_id=op.get("skillSet") if "skillSet" in op else op.get("skillSetId", op.get("skill_set_id")),
        config_set_id=op.get("configSet") if "configSet" in op else op.get("configSetId", op.get("config_set_id")),
        loadout_index=op.get("loadout") if "loadout" in op else op.get("loadoutIndex", op.get("loadout_index")),
        cache=cache,
    )
    return {"step": idx, "op": "switch_main", "changes": sw_res["changes"]}


def _op_add_group(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    full_dps_val = (
        op.get("includeInFullDPS")
        if "includeInFullDPS" in op
        else op.get("include_in_full_dps", op.get("fullDps"))
    )
    ag_res = _apply_add_group_in_place(
        w,
        gems=op.get("gems"),
        label=op.get("label"),
        slot=op.get("slot"),
        enabled=bool(op.get("enabled", True)),
        include_in_full_dps=bool(full_dps_val) if full_dps_val is not None else None,
        make_main=bool(op.get("makeMain", op.get("make_main", False))),
        cache=cache,
    )
    ag_step: dict[str, Any] = {
        "step": idx,
        "op": "add_group",
        "groupIndex": ag_res["groupIndex"],
        "gemsCount": len(ag_res["gemsAdded"]),
    }
    if ag_res.get("cautions"):
        ag_step["cautions"] = ag_res["cautions"]
    return ag_step


def _op_delete_group(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    g_sel = op.get("group") if op.get("group") is not None else op.get("index")
    if g_sel is None:
        raw_kind = str(op.get("op") or op.get("type") or "delete_group")
        raise ValueError(f"Batch op '{raw_kind}' at step {idx} requires 'group' or 'index'.")
    g_idx = resolve_skill_group_index(cache.get_skills(), g_sel)
    w.call("delete_group", {"index": g_idx})
    cache.invalidate("skills")
    return {"step": idx, "op": "delete_group", "groupIndex": g_idx}


def _op_set_group(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    g_sel = op.get("group") if op.get("group") is not None else op.get("index", "main")
    g_idx = resolve_skill_group_index(cache.get_skills(), g_sel)
    sg_params: dict[str, Any] = {"index": g_idx}
    if "label" in op and op["label"] is not None:
        sg_params["label"] = str(op["label"])
    if "enabled" in op and op["enabled"] is not None:
        sg_params["enabled"] = bool(op["enabled"])
    full_dps_val = (
        op.get("includeInFullDPS")
        if "includeInFullDPS" in op
        else op.get("include_in_full_dps", op.get("fullDps"))
    )
    if full_dps_val is not None:
        sg_params["includeInFullDPS"] = bool(full_dps_val)
    if "slot" in op and op["slot"] is not None:
        s_clean = str(op["slot"]).strip()
        if s_clean:
            check_input(s_clean, scope=GuardrailScope.SLOT, expected_game="poe1")
        sg_params["slot"] = _resolve_slot_from_items(cache.get_items(), s_clean) if s_clean else ""
    if "mainActiveSkill" in op or "main_active_skill" in op:
        mas = op.get("mainActiveSkill", op.get("main_active_skill"))
        if mas is not None:
            sg_params["mainActiveSkill"] = int(mas)
    w.call("set_group", sg_params)
    cache.invalidate("skills")
    return {"step": idx, "op": "set_group", "groupIndex": g_idx, "updates": sg_params}


def _op_replace_group(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    g_sel = op.get("group") if op.get("group") is not None else op.get("index", "main")
    full_dps_val = (
        op.get("includeInFullDPS")
        if "includeInFullDPS" in op
        else op.get("include_in_full_dps", op.get("fullDps"))
    )
    rg_res = _apply_replace_group_in_place(
        w,
        g_sel,
        list(op.get("gems") or []),
        label=op.get("label"),
        slot=op.get("slot"),
        enabled=bool(op["enabled"]) if "enabled" in op and op["enabled"] is not None else None,
        include_in_full_dps=bool(full_dps_val) if full_dps_val is not None else None,
        cache=cache,
    )
    rg_step: dict[str, Any] = {
        "step": idx,
        "op": "replace_group",
        "groupIndex": rg_res["groupIndex"],
        "removedCount": rg_res["removedCount"],
        "gemsCount": len(rg_res["gemsAdded"]),
    }
    if rg_res.get("cautions"):
        rg_step["cautions"] = rg_res["cautions"]
    return rg_step


def _op_skill(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    skills_res = cache.get_skills()
    g_idx = resolve_skill_group_index(skills_res, op.get("group", "main"))
    group = next((g for g in skills_res.get("groups", []) if g["index"] == g_idx), None)
    if group is None:
        raise ValueError(f"Skill group #{g_idx} not found in build.")

    grp_updates: dict[str, Any] = {"index": g_idx}
    full_dps_val = op.get("includeInFullDPS") if "includeInFullDPS" in op else op.get("include_in_full_dps")
    if full_dps_val is not None:
        grp_updates["includeInFullDPS"] = bool(full_dps_val)
    grp_en = op.get("groupEnabled") if "groupEnabled" in op else op.get("group_enabled")
    if grp_en is not None:
        grp_updates["enabled"] = bool(grp_en)
    if len(grp_updates) > 1:
        w.call("set_group", grp_updates)

    skill_cautions: list[str] = []
    add_gem_name = op.get("addGem") or op.get("add_gem")
    remove_gem_sel = op.get("removeGem") if "removeGem" in op else op.get("remove_gem")
    if add_gem_name:
        gem_name = str(add_gem_name).strip()
        check_input(gem_name, scope=GuardrailScope.GEM_NAME, expected_game="poe1")
        raw_level = op.get("level")
        req_level = int(raw_level) if raw_level is not None else None
        _, pre_c = _validate_gem_level(gem_name, req_level)
        cand = _resolve_gem_candidate(w, g_idx, gem_name)
        resolved_name = (cand.get("name") if cand else None) or gem_name
        eff_lv, res_c = _validate_gem_level(resolved_name, req_level)
        skill_cautions = _merge_cautions(pre_c, res_c)
        add_params: dict[str, Any] = {
            "group": g_idx,
            "level": eff_lv if eff_lv is not None else 20,
            "quality": int(op["quality"]) if op.get("quality") is not None else 20,
            "enabled": bool(op["enabled"]) if op.get("enabled") is not None else True,
            "count": int(op["count"]) if op.get("count") is not None else 1,
        }
        if cand and cand.get("gemId"):
            add_params["gemId"] = cand["gemId"]
            add_params["nameSpec"] = resolved_name
        else:
            add_params["nameSpec"] = gem_name
        _add_gem_with_default_skill_flags(w, add_params)
    elif remove_gem_sel is not None:
        t_gem = _resolve_gem_in_group(group, remove_gem_sel)
        if group.get("source") and int(t_gem.get("index", 0)) == 1:
            _guard_source_skill_group(group, action="remove gem #1")
        w.call("delete_gem", {"group": g_idx, "index": t_gem["index"]})
    elif op.get("gem") is not None:
        t_gem = _resolve_gem_in_group(group, op["gem"])
        gem_params: dict[str, Any] = {"group": g_idx, "index": t_gem["index"]}
        new_gem_name = op.get("newGem") or op.get("new_gem")
        req_level = int(op["level"]) if ("level" in op and op["level"] is not None) else None
        if new_gem_name:
            check_input(str(new_gem_name), scope=GuardrailScope.GEM_NAME, expected_game="poe1")
            _, pre_c = _validate_gem_level(str(new_gem_name).strip(), req_level)
            cand = _resolve_gem_candidate(w, g_idx, str(new_gem_name))
            resolved_name = (cand.get("name") if cand else None) or str(new_gem_name).strip()
            eff_lv, res_c = _validate_gem_level(resolved_name, req_level)
            skill_cautions = _merge_cautions(pre_c, res_c)
            if cand and cand.get("gemId"):
                gem_params["gemId"] = cand["gemId"]
                gem_params["nameSpec"] = resolved_name
            else:
                gem_params["nameSpec"] = str(new_gem_name).strip()
            if req_level is not None or (eff_lv is not None and eff_lv < 20):
                gem_params["level"] = eff_lv
        elif req_level is not None:
            existing_name = str(t_gem.get("name") or t_gem.get("nameSpec") or "").strip()
            eff_lv, res_c = _validate_gem_level(existing_name, req_level)
            skill_cautions = _merge_cautions(skill_cautions, res_c)
            gem_params["level"] = eff_lv
        for k in ("quality", "enabled", "count"):
            if k in op:
                gem_params[k] = op[k]
        w.call("set_gem", gem_params)

    cache.invalidate("skills")
    sk_step: dict[str, Any] = {"step": idx, "op": "skill", "group": g_idx}
    if skill_cautions:
        sk_step["cautions"] = skill_cautions
    return sk_step


_BATCH_OP_HANDLERS: dict[str, _BatchOpHandler] = {
    "config": _op_config,
    "flask": _op_flask,
    "item": _op_item,
    "item_db": _op_item_db,
    "item_edit": _op_item_edit,
    "unequip": _op_unequip,
    "item_unequip": _op_unequip,
    "unequip_slot": _op_unequip,
    "anoint": _op_anoint,
    "tree": _op_tree,
    "tree_reset": _op_tree_reset,
    "reset_tree": _op_tree_reset,
    "import_tree_url": _op_import_tree_url,
    "new_build": _op_new_build,
    "set_level": _op_set_level,
    "set_class": _op_set_class,
    "switch_main": _op_switch_main,
    "add_group": _op_add_group,
    "new_group": _op_add_group,
    "delete_group": _op_delete_group,
    "rm_group": _op_delete_group,
    "remove_group": _op_delete_group,
    "set_group": _op_set_group,
    "replace_group": _op_replace_group,
    "skill": _op_skill,
}


def _execute_batch_ops(
    w: BridgeWorker,
    ops: list[dict[str, Any]],
    cache: _BatchCache,
) -> list[dict[str, Any]]:
    """Execute a list of batch mutation ops in place with smart per-tab caching."""
    step_results: list[dict[str, Any]] = []
    for idx, op in enumerate(ops, start=1):
        if not isinstance(op, dict):
            raise ValueError(f"Batch op at step {idx} must be a JSON object.")
        raw_kind = str(op.get("op") or op.get("type") or "").strip().lower()
        kind = raw_kind.replace("-", "_")
        handler = _BATCH_OP_HANDLERS.get(kind)
        if handler is None:
            raise ValueError(f"Unsupported batch op type '{raw_kind}' at step {idx}.")
        step_results.append(handler(w, op, cache, idx))
    return step_results
