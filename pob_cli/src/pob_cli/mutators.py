"""Build mutation operations (items, flasks, anoints, skills, passive tree, config, batch)."""

from __future__ import annotations

from typing import Any

from .analyzer import resolve_skill_group_index, resolve_slot_name
from .bridge_client import BridgeWorker
from .formatters import strip_pob_escapes
from .snapshot import run_mutation


def try_item(
    worker: BridgeWorker,
    *,
    raw_text: str,
    slot_name: str | None = None,
    tooltip_only: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Evaluate or equip a pasted raw PoE item text.

    - Always runs PoB's native `item_tooltip{raw, slotName, compare=True}`.
    - Unless `tooltip_only=True`, also runs a snapshot `add_item{raw, equip=True}` to catch secondary
      effects (attribute shortages, uncapped resistances, lost item-granted skills) or persists when `apply=True`.
    - When `slot_name` is omitted and the item fits multiple slots (e.g. Ring 1 vs Ring 2, Weapon 1 vs Weapon 2),
      includes the comparison for all candidate slots in `tooltip`.
    """
    raw_clean = raw_text.strip()
    if not raw_clean:
        raise ValueError("Item raw text cannot be empty.")

    with worker.lock:
        resolved_slot = resolve_slot_name(worker, slot_name) if slot_name else None
        tip_params: dict[str, Any] = {"raw": raw_clean, "compare": True}
        if resolved_slot:
            tip_params["slotName"] = resolved_slot
        native_tip = worker.call("item_tooltip", tip_params)

        # Build multi-slot comparisons when slot_name is omitted and item fits multiple slots (P1-1)
        summary = native_tip.get("summary") or {}
        primary_slot = resolved_slot or native_tip.get("slotName") or summary.get("primarySlot") or ""
        item_type = str(summary.get("type") or "")

        if resolved_slot:
            candidate_slots = [resolved_slot]
        elif primary_slot in ("Ring 1", "Ring 2") or "Ring" in item_type:
            candidate_slots = ["Ring 1", "Ring 2"]
        elif primary_slot in ("Weapon 1", "Weapon 2"):
            candidate_slots = ["Weapon 1", "Weapon 2"]
        elif primary_slot.startswith("Flask") or "Flask" in item_type:
            candidate_slots = [f"Flask {i}" for i in range(1, 6)]
        elif "Jewel" in primary_slot or "Jewel" in item_type:
            items_res = worker.call("list_items", {})
            tree_state = worker.call("get_tree_state", {})
            alloc_set = {int(x) for x in tree_state.get("allocatedNodes", [])}
            candidate_slots = [
                s["name"]
                for s in items_res.get("slots", [])
                if s.get("nodeId") and int(s["nodeId"]) in alloc_set
            ][:8]
            if not candidate_slots and primary_slot:
                candidate_slots = [primary_slot]
        else:
            candidate_slots = [primary_slot] if primary_slot else []

        slot_tooltips: list[dict[str, Any]] = []
        for c_slot in candidate_slots:
            if c_slot == (resolved_slot or native_tip.get("slotName")):
                c_tip = native_tip
            else:
                try:
                    c_tip = worker.call("item_tooltip", {"raw": raw_clean, "slotName": c_slot, "compare": True})
                except Exception:
                    continue
            slot_tooltips.append(
                {
                    "slotName": c_slot,
                    "lines": c_tip.get("lines", []),
                    "summary": c_tip.get("summary"),
                }
            )

        if tooltip_only and not apply:
            return {
                "applied": False,
                "mode": "TOOLTIP-ONLY (fast preview)",
                "slotName": primary_slot or None,
                "tooltip": native_tip,
                "slotTooltips": slot_tooltips,
                "mutation": None,
            }

        def _do_equip(w: BridgeWorker) -> dict[str, Any]:
            add_params: dict[str, Any] = {"raw": raw_clean, "equip": True}
            if resolved_slot:
                add_params["slotName"] = resolved_slot
            res = w.call("add_item", add_params)
            return {
                "addedItem": res.get("item"),
                "slotName": resolved_slot or res.get("item", {}).get("primarySlot"),
            }

        mutation_res = run_mutation(worker, _do_equip, apply=apply)
        return {
            "applied": apply,
            "mode": mutation_res["mode"],
            "slotName": resolved_slot or mutation_res["detail"].get("slotName") or primary_slot,
            "tooltip": native_tip,
            "slotTooltips": slot_tooltips,
            "mutation": mutation_res,
        }


def set_flask_active(
    worker: BridgeWorker,
    *,
    flask_index: int | str,
    active: bool,
    apply: bool = False,
) -> dict[str, Any]:
    """Toggle a flask slot (`Flask 1`..`Flask 5` or `all`) active or inactive."""
    idx_str = str(flask_index).strip().lower()
    if idx_str == "all":
        slot_names = [f"Flask {i}" for i in range(1, 6)]
    elif idx_str.isdigit() and 1 <= int(idx_str) <= 5:
        slot_names = [f"Flask {int(idx_str)}"]
    else:
        raise ValueError("Flask selector must be 1..5 or 'all'.")

    def _do_flask(w: BridgeWorker) -> dict[str, Any]:
        toggled: list[dict[str, Any]] = []
        for s_name in slot_names:
            res = w.call("set_slot_active", {"slotName": s_name, "active": active})
            toggled.append({"slotName": s_name, "active": res.get("active", active)})
        label = "Flask 1..5 (all)" if len(slot_names) > 1 else slot_names[0]
        return {"slotName": label, "active": active, "slots": toggled}

    return run_mutation(worker, _do_flask, apply=apply)


def _apply_anoint_in_place(
    w: BridgeWorker,
    *,
    slot_name: str = "Amulet",
    notable_query: str,
    anoint_index: int = 1,
) -> dict[str, Any]:
    if anoint_index < 1 or anoint_index > 4:
        raise ValueError("anoint_index must be between 1 and 4.")
    action_key = "anoint" if anoint_index == 1 else f"anoint{anoint_index}"

    resolved_slot = resolve_slot_name(w, slot_name)
    items_res = w.call("list_items", {})
    slot_obj = next((s for s in items_res.get("slots", []) if s["name"] == resolved_slot), None)
    if not slot_obj or not slot_obj.get("selItemId"):
        raise RuntimeError(f"Slot '{resolved_slot}' has no equipped item to anoint.")

    item_id = int(slot_obj["selItemId"])
    edit_state = w.call("item_edit_begin", {"id": item_id})
    try:
        actions = edit_state.get("actions", {})
        if not actions.get(action_key):
            raise RuntimeError(
                f"Item '{edit_state.get('name')}' in slot '{resolved_slot}' does not support '{action_key}' "
                f"(may be Corrupted, Mirrored, or not an anointable base)."
            )

        pop = w.call(
            "item_edit_popup",
            {"kind": "anoint", "action": "open", "slot": anoint_index},
        )
        notable_ctl = next(
            (c for c in pop.get("controls", []) if c.get("name") == "notableDB"),
            None,
        )
        if not notable_ctl:
            raise RuntimeError("Anoint popup did not expose 'notableDB' control.")

        options = notable_ctl.get("options", [])
        q = notable_query.strip().lower()
        q_stripped = q.removeprefix("allocate ").removeprefix("配置 ").strip()

        matched_opt: dict[str, Any] | None = None
        for opt in options:
            lbl = strip_pob_escapes(opt.get("label") or "").lower().strip()
            lbl_zh = strip_pob_escapes(opt.get("labelZh") or "").lower().strip()
            lbl_s = lbl.removeprefix("allocate ").strip()
            lbl_zh_s = lbl_zh.removeprefix("配置 ").strip()
            if q in (lbl, lbl_zh, lbl_s, lbl_zh_s) or q_stripped in (lbl, lbl_zh, lbl_s, lbl_zh_s):
                matched_opt = opt
                break

        if not matched_opt:
            sub_matches = [
                opt
                for opt in options
                if q_stripped in strip_pob_escapes(opt.get("label") or "").lower()
                or q_stripped in strip_pob_escapes(opt.get("labelZh") or "").lower()
            ]
            if len(sub_matches) == 1:
                matched_opt = sub_matches[0]
            elif len(sub_matches) > 1:
                samples = ", ".join(
                    f"{strip_pob_escapes(o.get('labelZh') or o.get('label'))}"
                    for o in sub_matches[:5]
                )
                raise ValueError(
                    f"Ambiguous notable query '{notable_query}' matched {len(sub_matches)} notables: {samples}"
                )

        if not matched_opt:
            raise ValueError(f"Notable '{notable_query}' not found in anointable notableDB options.")

        w.call(
            "item_edit_popup",
            {
                "kind": "anoint",
                "action": "pick",
                "name": "notableDB",
                "value": matched_opt["id"],
            },
        )
        w.call(
            "item_edit_popup",
            {"kind": "anoint", "action": "apply", "button": "save"},
        )
        commit_res = w.call("item_edit_commit", {"equip": True})
        return {
            "slotName": resolved_slot,
            "anointIndex": anoint_index,
            "notable": matched_opt,
            "item": commit_res.get("item"),
        }
    except Exception:
        try:
            w.call("item_edit_cancel", {})
        except Exception:
            pass
        raise


def anoint_item(
    worker: BridgeWorker,
    *,
    slot_name: str = "Amulet",
    notable_query: str,
    anoint_index: int = 1,
    apply: bool = False,
) -> dict[str, Any]:
    """Anoint an equipped item (default Amulet) using PoB's native `item_edit_*` popup workflow."""
    if anoint_index < 1 or anoint_index > 4:
        raise ValueError("anoint_index must be between 1 and 4.")

    def _do_anoint(w: BridgeWorker) -> dict[str, Any]:
        return _apply_anoint_in_place(
            w,
            slot_name=slot_name,
            notable_query=notable_query,
            anoint_index=anoint_index,
        )

    return run_mutation(worker, _do_anoint, apply=apply)


def _resolve_gem_in_group(group: dict[str, Any], gem_selector: str | int) -> dict[str, Any]:
    gems = group.get("gems", [])
    s = str(gem_selector).strip()
    target_gem = None
    if s.isdigit():
        idx = int(s)
        target_gem = next((gm for gm in gems if gm["index"] == idx), None)
    else:
        s_low = s.lower()
        for gm in gems:
            names = [
                str(gm.get("nameSpec") or ""),
                str(gm.get("name") or ""),
                str(gm.get("nameZh") or ""),
            ]
            if any(s_low in n.lower() for n in names):
                target_gem = gm
                break
    if not target_gem:
        raise ValueError(f"Gem '{gem_selector}' not found in skill group #{group.get('index')}.")
    return target_gem


def _resolve_gem_candidate(w: BridgeWorker, g_idx: int, gem_name: str) -> dict[str, Any] | None:
    search_res = w.call(
        "gem_search",
        {
            "group": g_idx,
            "query": gem_name.strip(),
            "byDps": False,
            "supportableOnly": False,
            "limit": 20,
        },
    )
    candidates = search_res.get("gems", [])
    q_low = gem_name.strip().lower()
    for c in candidates:
        if q_low in (str(c.get("name") or "").lower(), str(c.get("nameZh") or "").lower()):
            return c
    return candidates[0] if candidates else None


def modify_skill(
    worker: BridgeWorker,
    *,
    group_selector: str,
    gem_selector: str | int | None = None,
    new_gem: str | None = None,
    level: int | None = None,
    quality: int | None = None,
    enabled: bool | None = None,
    count: int | None = None,
    include_in_full_dps: bool | None = None,
    group_enabled: bool | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Modify a skill group or a specific gem within a skill group."""
    def _do_skill_set(w: BridgeWorker) -> dict[str, Any]:
        skills_res = w.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        group = next(g for g in skills_res.get("groups", []) if g["index"] == g_idx)

        group_updates: dict[str, Any] = {"index": g_idx}
        if include_in_full_dps is not None:
            group_updates["includeInFullDPS"] = include_in_full_dps
        if group_enabled is not None:
            group_updates["enabled"] = group_enabled
        if gem_selector is None and enabled is not None:
            group_updates["enabled"] = enabled

        if len(group_updates) > 1:
            w.call("set_group", group_updates)

        gem_result = None
        if gem_selector is not None:
            target_gem = _resolve_gem_in_group(group, gem_selector)
            gem_params: dict[str, Any] = {"group": g_idx, "index": target_gem["index"]}
            if new_gem:
                cand = _resolve_gem_candidate(w, g_idx, new_gem)
                if cand and cand.get("gemId"):
                    gem_params["gemId"] = cand["gemId"]
                    gem_params["nameSpec"] = cand.get("name") or new_gem.strip()
                else:
                    gem_params["nameSpec"] = new_gem.strip()
            if level is not None:
                gem_params["level"] = level
            if quality is not None:
                gem_params["quality"] = quality
            if enabled is not None:
                gem_params["enabled"] = enabled
            if count is not None:
                gem_params["count"] = count
            gem_result = w.call("set_gem", gem_params)

        return {"groupIndex": g_idx, "groupUpdates": group_updates, "gemResult": gem_result}

    return run_mutation(worker, _do_skill_set, apply=apply)


def add_skill_gem(
    worker: BridgeWorker,
    *,
    group_selector: str,
    gem_name: str,
    level: int = 20,
    quality: int = 20,
    enabled: bool = True,
    apply: bool = False,
) -> dict[str, Any]:
    """Add a gem (resolved via `gem_search` if needed) to a skill group."""
    def _do_add(w: BridgeWorker) -> dict[str, Any]:
        skills_res = w.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        chosen = _resolve_gem_candidate(w, g_idx, gem_name)

        add_params: dict[str, Any] = {
            "group": g_idx,
            "level": level,
            "quality": quality,
            "enabled": enabled,
        }
        if chosen and chosen.get("gemId"):
            add_params["gemId"] = chosen["gemId"]
            add_params["nameSpec"] = chosen.get("name") or gem_name.strip()
        else:
            add_params["nameSpec"] = gem_name.strip()

        res = w.call("add_gem", add_params)
        return {"groupIndex": g_idx, "addedGem": res, "resolvedCandidate": chosen}

    return run_mutation(worker, _do_add, apply=apply)


def remove_skill_gem(
    worker: BridgeWorker,
    *,
    group_selector: str,
    gem_selector: str | int,
    apply: bool = False,
) -> dict[str, Any]:
    """Remove a gem from a skill group by 1-based index or gem name."""
    def _do_rm(w: BridgeWorker) -> dict[str, Any]:
        skills_res = w.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        group = next(g for g in skills_res.get("groups", []) if g["index"] == g_idx)
        target_gem = _resolve_gem_in_group(group, gem_selector)
        res = w.call("delete_gem", {"group": g_idx, "index": target_gem["index"]})
        return {"groupIndex": g_idx, "removedGem": target_gem, "result": res}

    return run_mutation(worker, _do_rm, apply=apply)


def _resolve_node_token(
    worker: BridgeWorker,
    token: str,
    nodes_db: dict[str, Any],
    dynamic_map: dict[str, Any],
) -> tuple[int, int | None]:
    """Resolve a node token (`12345`, `12345:67890` mastery, or node name) into `(node_id, effect_id)`."""
    t = token.strip()
    effect_id: int | None = None
    if ":" in t:
        left, right = t.split(":", 1)
        t = left.strip()
        if right.strip().isdigit():
            effect_id = int(right.strip())
        else:
            # Resolve mastery effect by substring later once node_id is known
            effect_query = right.strip().lower()
            nid, _ = _resolve_node_token(worker, t, nodes_db, dynamic_map)
            node_meta = nodes_db.get(str(nid)) or dynamic_map.get(str(nid)) or {}
            for eff in node_meta.get("masteryEffects", []):
                text = " ".join(str(s) for s in (eff.get("stats") or []) + (eff.get("statsZh") or [])).lower()
                if effect_query in text:
                    return nid, int(eff["effect"])
            raise ValueError(f"Mastery effect matching '{right}' not found on node {nid}.")

    if t.isdigit():
        nid = int(t)
        if str(nid) not in nodes_db and str(nid) not in dynamic_map:
            raise ValueError(f"Passive tree node ID {nid} does not exist.")
        return nid, effect_id

    t_low = t.lower()
    exact_matches: list[int] = []
    sub_matches: list[int] = []
    for sid, meta in {**nodes_db, **dynamic_map}.items():
        name_en = str(meta.get("name") or "").lower()
        name_zh = str(meta.get("nameZh") or "").lower()
        if t_low in (name_en, name_zh):
            exact_matches.append(int(sid))
        elif t_low in name_en or t_low in name_zh:
            sub_matches.append(int(sid))

    candidates = exact_matches or sub_matches
    if not candidates:
        raise ValueError(f"No passive node found matching '{t}'.")
    if len(candidates) == 1:
        return candidates[0], effect_id

    # Prefer allocated or shortest path distance
    best_id = candidates[0]
    best_dist = 999999
    for cid in candidates[:15]:
        try:
            hover = worker.call("node_hover", {"id": cid})
            cost = hover.get("cost")
            dist = 0 if hover.get("allocated") else (int(cost) if cost is not None else 9999)
            if dist < best_dist:
                best_dist = dist
                best_id = cid
        except Exception:
            pass
    return best_id, effect_id


def _apply_tree_changes_in_place(
    w: BridgeWorker,
    resolved_alloc: list[tuple[int, int | None]],
    resolved_dealloc: list[tuple[int, int | None]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    actions_taken: list[dict[str, Any]] = []

    for nid, eff_id in resolved_alloc:
        cur_state = w.call("get_tree_state", {})
        alloc_set = {int(x) for x in cur_state.get("allocatedNodes", [])}
        if nid in alloc_set:
            if eff_id is not None:
                w.call("select_mastery", {"id": nid, "effect": eff_id})
                actions_taken.append({"action": "select_mastery", "id": nid, "effect": eff_id})
            else:
                actions_taken.append({"action": "skip_already_allocated", "id": nid})
            continue

        click_params: dict[str, Any] = {"id": nid}
        if eff_id is not None:
            click_params["effect"] = eff_id
        res = w.call("tree_click", click_params)
        if res.get("needsMastery"):
            effects = res.get("effects", [])
            opts_str = "; ".join(
                f"{e['effect']}: {' / '.join(e.get('statsZh') or e.get('stats') or [])}"
                for e in effects[:6]
            )
            raise ValueError(
                f"Node {nid} is a Mastery node and requires `<nodeId>:<effectId>`. Available effects: {opts_str}"
            )
        if res.get("needsConfirm"):
            raise ValueError(f"Node {nid} requires confirmation ({res.get('needsConfirm')}).")
        actions_taken.append({"action": "allocated", "id": nid, "effect": eff_id})

    for nid, _ in resolved_dealloc:
        cur_state = w.call("get_tree_state", {})
        alloc_set = {int(x) for x in cur_state.get("allocatedNodes", [])}
        if nid not in alloc_set:
            actions_taken.append({"action": "skip_already_unallocated", "id": nid})
            continue
        res = w.call("tree_click", {"id": nid})
        if res.get("needsConfirm"):
            raise ValueError(f"Node {nid} deallocation requires confirmation ({res.get('needsConfirm')}).")
        actions_taken.append({"action": "deallocated", "id": nid})

    final_state = w.call("get_tree_state", {})
    return actions_taken, final_state.get("points")


def change_tree(
    worker: BridgeWorker,
    *,
    allocate: list[str] | None = None,
    deallocate: list[str] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Allocate and/or deallocate passive tree nodes.

    Follows R12:
    - Single-node what-if (`apply=False`, 1 node total, no mastery effect mutation) uses fast `node_info{diff=true}` + `node_hover` preview PLUS full snapshot diff for accuracy.
    - Multi-node or `--apply` re-queries `get_tree_state()["allocatedNodes"]` before every `tree_click` so auto-pathing or cascading dependency removal never inverts a target node's state.
    """
    alloc_tokens = [x for x in (allocate or []) if x.strip()]
    dealloc_tokens = [x for x in (deallocate or []) if x.strip()]
    if not alloc_tokens and not dealloc_tokens:
        raise ValueError("Specify at least one node in --allocate or --deallocate.")

    with worker.lock:
        tree_state = worker.call("get_tree_state", {})
        nodes_db = worker.get_cached_tree_data(tree_state.get("treeVersion")).get("nodes", {})
        dynamic_map = {str(n["id"]): n for n in tree_state.get("dynamicNodes", [])}

        resolved_alloc = [_resolve_node_token(worker, tok, nodes_db, dynamic_map) for tok in alloc_tokens]
        resolved_dealloc = [_resolve_node_token(worker, tok, nodes_db, dynamic_map) for tok in dealloc_tokens]

        # Capture fast node_info + node_hover preview when operating on a single node
        single_preview = None
        if len(resolved_alloc) + len(resolved_dealloc) == 1:
            single_nid = (resolved_alloc or resolved_dealloc)[0][0]
            try:
                n_info = worker.call("node_info", {"id": single_nid, "diff": True})
                n_hover = worker.call("node_hover", {"id": single_nid})
                single_preview = {"nodeInfo": n_info, "nodeHover": n_hover}
            except Exception:
                pass

        def _do_tree_changes(w: BridgeWorker) -> dict[str, Any]:
            actions_taken, points = _apply_tree_changes_in_place(w, resolved_alloc, resolved_dealloc)
            return {
                "actions": actions_taken,
                "points": points,
                "singlePreview": single_preview,
            }

        return run_mutation(worker, _do_tree_changes, apply=apply)


def _coerce_config_value(raw_val: Any) -> Any:
    if isinstance(raw_val, (bool, int, float)):
        return raw_val
    s = str(raw_val).strip()
    if s.lower() in ("true", "yes", "on"):
        return True
    if s.lower() in ("false", "no", "off"):
        return False
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _apply_config_in_place(
    w: BridgeWorker,
    assignments: list[tuple[str, Any]],
    custom_mods: str | None = None,
) -> dict[str, Any]:
    applied_vars: list[dict[str, Any]] = []
    touched_boss = False
    for var, raw_val in assignments:
        v_str = str(raw_val).strip()
        if v_str.lower() in ("reset", "none", "null", "default"):
            res = w.call("reset_config", {"var": var})
            applied_vars.append({"var": var, "action": "reset", "result": res})
        else:
            coerced = _coerce_config_value(raw_val)
            res = w.call("set_config", {"var": var, "value": coerced})
            applied_vars.append({"var": var, "action": "set", "value": coerced, "result": res})
        if var in ("enemyIsBoss", "presetBossSkills"):
            touched_boss = True

    if touched_boss:
        cfg = w.call("list_config", {})
        for sec in cfg.get("sections", []):
            for item in sec.get("items", []):
                if item.get("var") == "presetBossSkills" and item.get("value"):
                    w.call("set_config", {"var": "presetBossSkills", "value": item["value"]})
                    break

    if custom_mods is not None:
        cm_clean = custom_mods.strip()
        if cm_clean.lower() in ("clear", "reset", "none", "disable", "disabled", ""):
            w.call(
                "set_custom_mods",
                {"list": [{"title": "Default", "text": "# disabled", "enabled": True}]},
            )
            applied_vars.append({"var": "customMods", "action": "clear"})
        else:
            w.call(
                "set_custom_mods",
                {"list": [{"title": "CLI Custom Mods", "text": cm_clean, "enabled": True}]},
            )
            applied_vars.append({"var": "customMods", "action": "set", "value": cm_clean})

    return {"assignments": applied_vars}


def modify_config(
    worker: BridgeWorker,
    *,
    assignments: list[tuple[str, Any]],
    custom_mods: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Set or reset one or more PoB ConfigTab variables (`key=value` or `key=reset`) and/or `customMods`."""
    if not assignments and custom_mods is None:
        raise ValueError("No config assignments or --custom-mods provided.")

    def _do_cfg(w: BridgeWorker) -> dict[str, Any]:
        return _apply_config_in_place(w, assignments, custom_mods=custom_mods)

    return run_mutation(worker, _do_cfg, apply=apply)


LOADOUT_SENTINELS = frozenset({
    "loadouts:",
    "-----",
    "sync",
    "new loadout",
    "help >>",
})


def _resolve_id_or_title(
    items: list[dict[str, Any]],
    selector: int | str,
    *,
    id_key: str = "id",
    title_keys: tuple[str, ...] = ("title", "titleZh", "label", "labelZh"),
    entity_name: str = "item",
) -> int:
    s = str(selector).strip()
    if s.isdigit():
        val = int(s)
        if any(int(it.get(id_key, -1)) == val for it in items):
            return val
        if 1 <= val <= len(items):
            return int(items[val - 1][id_key])
    s_low = s.lower()
    for it in items:
        for k in title_keys:
            if s_low == strip_pob_escapes(str(it.get(k) or "")).strip().lower():
                return int(it[id_key])
    for it in items:
        for k in title_keys:
            if s_low in strip_pob_escapes(str(it.get(k) or "")).strip().lower():
                return int(it[id_key])
    raise ValueError(f"Could not find {entity_name} matching '{selector}'.")


def switch_main(
    worker: BridgeWorker,
    *,
    spec_index: int | str | None = None,
    skill_group: str | int | None = None,
    main_skill_index: int | None = None,
    skill_part_index: int | None = None,
    item_set_id: int | str | None = None,
    skill_set_id: int | str | None = None,
    config_set_id: int | str | None = None,
    loadout_index: int | str | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Switch active spec, main skill group, active skill/part, item/skill/config set, or synced loadout."""
    def _do_switch(w: BridgeWorker) -> dict[str, Any]:
        changes: list[str] = []
        if loadout_index is not None:
            lo_res = w.call("list_loadouts", {})
            raw_list = lo_res.get("list", [])
            real_loadouts = [
                it
                for it in raw_list
                if strip_pob_escapes(str(it.get("label") or "")).strip().lower() not in LOADOUT_SENTINELS
            ]
            if not real_loadouts:
                raise ValueError("Build has no synced loadouts available.")
            s_lo = str(loadout_index).strip()
            if s_lo.isdigit():
                k = int(s_lo)
                if 1 <= k <= len(real_loadouts):
                    target_raw_idx = int(real_loadouts[k - 1]["index"])
                elif any(int(it["index"]) == k for it in real_loadouts):
                    target_raw_idx = k
                else:
                    raise ValueError(f"Loadout #{k} out of range (1..{len(real_loadouts)}).")
            else:
                target_raw_idx = _resolve_id_or_title(
                    real_loadouts,
                    s_lo,
                    id_key="index",
                    title_keys=("label",),
                    entity_name="loadout",
                )
            w.call("select_loadout", {"index": target_raw_idx})
            changes.append(f"loadout={loadout_index} (rawIndex={target_raw_idx})")
        if spec_index is not None:
            specs_res = w.call("list_specs", {})
            resolved_spec = _resolve_id_or_title(
                specs_res.get("specs", []),
                spec_index,
                id_key="index",
                entity_name="tree spec",
            )
            w.call("set_active_spec", {"index": resolved_spec})
            changes.append(f"activeSpec={resolved_spec}")
        if item_set_id is not None:
            items_res = w.call("list_items", {})
            resolved_iset = _resolve_id_or_title(
                items_res.get("itemSets", []),
                item_set_id,
                id_key="id",
                entity_name="item set",
            )
            w.call("set_item_set", {"id": resolved_iset})
            changes.append(f"itemSetId={resolved_iset}")
        if skill_set_id is not None:
            skills_res = w.call("list_skills", {})
            resolved_sset = _resolve_id_or_title(
                skills_res.get("skillSets", []),
                skill_set_id,
                id_key="id",
                entity_name="skill set",
            )
            w.call("set_skill_set", {"id": resolved_sset})
            changes.append(f"skillSetId={resolved_sset}")
        if config_set_id is not None:
            cfg_res = w.call("list_config", {})
            resolved_cset = _resolve_id_or_title(
                cfg_res.get("configSets", []),
                config_set_id,
                id_key="id",
                entity_name="config set",
            )
            w.call("set_config_set", {"id": resolved_cset})
            changes.append(f"configSetId={resolved_cset}")
        if skill_group is not None:
            skills_res = w.call("list_skills", {})
            g_idx = resolve_skill_group_index(skills_res, skill_group)
            w.call("set_build_field", {"field": "mainSocketGroup", "value": g_idx})
            changes.append(f"mainSocketGroup={g_idx}")
        if main_skill_index is not None:
            w.call("set_build_field", {"field": "mainSkill", "value": int(main_skill_index)})
            changes.append(f"mainSkill={main_skill_index}")
        if skill_part_index is not None:
            w.call("set_build_field", {"field": "mainSkillPart", "value": int(skill_part_index)})
            changes.append(f"mainSkillPart={skill_part_index}")
        return {"changes": changes, "header": w.call("get_build_header", {})}

    return run_mutation(worker, _do_switch, apply=apply)


def run_batch(
    worker: BridgeWorker,
    *,
    ops: list[dict[str, Any]],
    apply: bool = False,
) -> dict[str, Any]:
    """Execute a composite list of mutations in a single snapshot transaction."""
    if not ops:
        raise ValueError("Batch operations list cannot be empty.")

    def _do_batch(w: BridgeWorker) -> dict[str, Any]:
        step_results: list[dict[str, Any]] = []
        for idx, op in enumerate(ops, start=1):
            kind = str(op.get("op") or op.get("type") or "").lower()
            if kind == "config":
                pairs = [(k, v) for k, v in (op.get("set") or {}).items()]
                for var in op.get("reset") or []:
                    pairs.append((var, "reset"))
                cm = op.get("customMods")
                cfg_res = _apply_config_in_place(w, pairs, custom_mods=str(cm) if cm is not None else None)
                step_results.append({"step": idx, "op": "config", "count": len(cfg_res["assignments"])})
            elif kind == "flask":
                f_sel = str(op.get("index", "all")).strip().lower()
                act = bool(op.get("active", True))
                slots = [f"Flask {i}" for i in range(1, 6)] if f_sel == "all" else [f"Flask {int(f_sel)}"]
                for s_name in slots:
                    w.call("set_slot_active", {"slotName": s_name, "active": act})
                step_results.append({"step": idx, "op": "flask", "slotName": ",".join(slots)})
            elif kind == "item":
                slot_name = resolve_slot_name(w, op["slot"]) if op.get("slot") else None
                params: dict[str, Any] = {"raw": str(op["raw"]).strip(), "equip": True}
                if slot_name:
                    params["slotName"] = slot_name
                res = w.call("add_item", params)
                step_results.append({"step": idx, "op": "item", "item": res.get("item")})
            elif kind == "anoint":
                an_res = _apply_anoint_in_place(
                    w,
                    slot_name=str(op.get("slot") or "Amulet"),
                    notable_query=str(op["notable"]),
                    anoint_index=int(op.get("index", 1)),
                )
                step_results.append({"step": idx, "op": "anoint", "notable": an_res.get("notable")})
            elif kind == "tree":
                tree_state = w.call("get_tree_state", {})
                nodes_db = w.get_cached_tree_data(tree_state.get("treeVersion")).get("nodes", {})
                dynamic_map = {str(n["id"]): n for n in tree_state.get("dynamicNodes", [])}
                resolved_alloc = [
                    _resolve_node_token(w, str(tok), nodes_db, dynamic_map)
                    for tok in (op.get("allocate") or [])
                ]
                resolved_dealloc = [
                    _resolve_node_token(w, str(tok), nodes_db, dynamic_map)
                    for tok in (op.get("deallocate") or [])
                ]
                actions_taken, _ = _apply_tree_changes_in_place(w, resolved_alloc, resolved_dealloc)
                step_results.append({"step": idx, "op": "tree", "actions": actions_taken})
            elif kind == "skill":
                skills_res = w.call("list_skills", {})
                g_idx = resolve_skill_group_index(skills_res, op.get("group", "main"))
                group = next(g for g in skills_res.get("groups", []) if g["index"] == g_idx)
                if op.get("addGem"):
                    gem_name = str(op["addGem"]).strip()
                    cand = _resolve_gem_candidate(w, g_idx, gem_name)
                    add_params: dict[str, Any] = {
                        "group": g_idx,
                        "level": int(op.get("level", 20)),
                        "quality": int(op.get("quality", 20)),
                        "enabled": bool(op.get("enabled", True)),
                    }
                    if cand and cand.get("gemId"):
                        add_params["gemId"] = cand["gemId"]
                        add_params["nameSpec"] = cand.get("name") or gem_name
                    else:
                        add_params["nameSpec"] = gem_name
                    w.call("add_gem", add_params)
                elif op.get("removeGem") is not None:
                    t_gem = _resolve_gem_in_group(group, op["removeGem"])
                    w.call("delete_gem", {"group": g_idx, "index": t_gem["index"]})
                elif op.get("gem") is not None:
                    t_gem = _resolve_gem_in_group(group, op["gem"])
                    gem_params: dict[str, Any] = {"group": g_idx, "index": t_gem["index"]}
                    if op.get("newGem"):
                        cand = _resolve_gem_candidate(w, g_idx, str(op["newGem"]))
                        if cand and cand.get("gemId"):
                            gem_params["gemId"] = cand["gemId"]
                            gem_params["nameSpec"] = cand.get("name") or str(op["newGem"]).strip()
                        else:
                            gem_params["nameSpec"] = str(op["newGem"]).strip()
                    for k in ("level", "quality", "enabled", "count"):
                        if k in op:
                            gem_params[k] = op[k]
                    w.call("set_gem", gem_params)
                step_results.append({"step": idx, "op": "skill", "group": g_idx})
            else:
                raise ValueError(f"Unsupported batch op type '{kind}' at step {idx}.")

        return {"steps": step_results}

    return run_mutation(worker, _do_batch, apply=apply)
