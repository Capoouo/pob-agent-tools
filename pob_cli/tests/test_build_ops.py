"""Unit and real headless PoB engine tests for Wave 1 build-ops slice."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from pob_cli.analyzer import inspect_config, inspect_items, search_calcs_breakdown
from pob_cli.bridge_client import BridgeWorker
from pob_cli.comparator import _parse_matrix_scenario, run_matrix
from pob_cli.formatters import (
    RAW_PRESERVE_KEYS,
    fmt_delta,
    fmt_num,
    render_update_warning,
    sanitize_for_output,
    to_json_str,
)
from pob_cli.mutators import (
    _edit_raw_item_mods,
    edit_item_mods,
    modify_config,
    search_and_try_item_db,
    set_class_ascendancy,
)
from pob_cli.snapshot import diff_digest, undo_build

POB_DIR = Path("F:/poe/PobTools[20261102]")


# ---------------------------------------------------------------------------
# Pure unit tests (formatters, raw item mod editor, matrix parser)
# ---------------------------------------------------------------------------


def test_formatters_inf_nan_and_sentinels() -> None:
    assert fmt_num(None) == "-"
    assert fmt_num("") == "-"
    assert fmt_num(True) == "Yes"
    assert fmt_num(False) == "No"
    assert fmt_num("inf") == "inf"
    assert fmt_num("-inf") == "-inf"
    assert fmt_num("Infinity") == "inf"
    assert fmt_num("nan") == "nan"
    assert fmt_num("N/A") == "N/A"
    assert fmt_num(math.inf) == "inf"
    assert fmt_num(-math.inf) == "-inf"
    assert fmt_num(math.nan) == "nan"
    assert fmt_num(12345.6) == "12,346"
    assert fmt_num(12.34, decimals=2) == "12.34"

    # fmt_delta with CI "inf" sentinels and NaN/strings
    assert fmt_delta("inf", 1000) == "inf -> 1,000"
    assert fmt_delta(1000, "inf") == "1,000 -> inf"
    assert fmt_delta("inf", "inf") == "0"
    assert fmt_delta(math.inf, math.inf) == "0"
    assert fmt_delta(-math.inf, math.inf) == "-inf -> inf"
    assert fmt_delta(math.nan, 100) == "nan -> 100"
    assert fmt_delta(math.nan, math.nan) == "0"
    assert fmt_delta("N/A", "N/A") == "0"
    assert fmt_delta("N/A", "100") == "N/A -> 100"
    assert fmt_delta(100, 150, "%") == "+50% (+50.0%)"


def test_diff_digest_ci_inf_nan_rfc8259_json() -> None:
    """Verify diff_digest + to_json_str handles PoB 'inf'/'-inf'/'nan' stats with valid RFC-8259 JSON."""
    before = {
        "stats": {
            "CombinedDPS": 100000.0,
            "TotalEHP": "inf",
            "ChaosMaximumHitTaken": "inf",
            "PhysicalMaximumHitTaken": 15000.0,
            "Ward": "nan",
            "Life": 1.0,
        },
        "warnings": [],
        "buildInfo": {"points": {"used": 100}},
    }
    after = {
        "stats": {
            "CombinedDPS": 120000.0,
            "TotalEHP": "inf",           # unchanged 'inf' must be skipped!
            "ChaosMaximumHitTaken": 12000.0,  # 'inf' -> finite must emit None delta/pct
            "PhysicalMaximumHitTaken": "inf",  # finite -> 'inf' must emit None delta/pct
            "Ward": math.nan,            # both NaN must be skipped!
            "Life": 1.0,
        },
        "warnings": [],
        "buildInfo": {"points": {"used": 100}},
    }
    diff = diff_digest(before, after)
    by_stat = {d["stat"]: d for d in diff["deltas"]}

    assert "TotalEHP" not in by_stat
    assert "Ward" not in by_stat
    assert "Life" not in by_stat

    assert by_stat["CombinedDPS"]["delta"] == pytest.approx(20000.0)
    assert by_stat["CombinedDPS"]["pct"] == pytest.approx(20.0)

    assert by_stat["ChaosMaximumHitTaken"]["before"] == "inf"
    assert by_stat["ChaosMaximumHitTaken"]["after"] == 12000.0
    assert by_stat["ChaosMaximumHitTaken"]["delta"] is None
    assert by_stat["ChaosMaximumHitTaken"]["pct"] is None
    assert by_stat["ChaosMaximumHitTaken"]["deltaStr"] == "inf -> 12,000"

    assert by_stat["PhysicalMaximumHitTaken"]["before"] == 15000.0
    assert by_stat["PhysicalMaximumHitTaken"]["after"] == "inf"
    assert by_stat["PhysicalMaximumHitTaken"]["delta"] is None
    assert by_stat["PhysicalMaximumHitTaken"]["pct"] is None
    assert by_stat["PhysicalMaximumHitTaken"]["deltaStr"] == "15,000 -> inf"

    json_text = to_json_str(diff)
    assert "NaN" not in json_text
    assert "Infinity" not in json_text

    def _reject_nonfinite(tok: str) -> None:
        raise ValueError(f"Non-RFC-8259 JSON token: {tok}")

    parsed = json.loads(json_text, parse_constant=_reject_nonfinite)
    assert len(parsed["deltas"]) == 3


def test_render_update_warning_and_raw_preserve_keys() -> None:
    assert render_update_warning(None) == ""
    assert render_update_warning("   ") == ""
    assert render_update_warning("GitHub timeout") == "> [WARN] GitHub timeout\n\n"

    assert "updateWarning" in RAW_PRESERVE_KEYS
    assert "customModsText" in RAW_PRESERVE_KEYS
    payload = {
        "display": "^xFF0000Red Text^7",
        "updateWarning": "^7Keep exact warning",
        "customModsText": "^7+100 to maximum Life",
        "beforeRaw": "^7Rarity: RARE",
        "afterRaw": "^7Rarity: RARE",
    }
    sanitized = sanitize_for_output(payload)
    assert sanitized["display"] == "Red Text"
    assert sanitized["updateWarning"] == "^7Keep exact warning"
    assert sanitized["customModsText"] == "^7+100 to maximum Life"
    assert sanitized["beforeRaw"] == "^7Rarity: RARE"
    assert sanitized["afterRaw"] == "^7Rarity: RARE"


def test_edit_raw_item_mods_preserves_headers_and_trailers() -> None:
    sample = "\n".join(
        [
            "Rarity: RARE",
            "Viper Band",
            "Ruby Ring",
            "Crafted: true",
            "Prefix: {range:0.5}IncreasedLife8",
            "Item Level: 84",
            "Implicits: 1",
            "{tags:elemental,fire,resistance}+30% to Fire Resistance",
            "+75 to maximum Life",
            "{tags:elemental,cold,resistance}+42% to Cold Resistance",
            "{crafted}+15% to Fire and Chaos Resistances",
            "Corrupted",
        ]
    )

    after, changes = _edit_raw_item_mods(
        sample,
        replace_mods=[("Cold Resistance", "+45% to Lightning Resistance")],
        remove_mods=["Fire and Chaos"],
        add_mods=["+30 to Intelligence"],
    )

    lines = after.splitlines()
    # Headers untouched
    assert lines[:7] == sample.splitlines()[:7]
    assert lines[7] == "{tags:elemental,fire,resistance}+30% to Fire Resistance"
    assert "+75 to maximum Life" in lines
    assert "+45% to Lightning Resistance" in lines
    assert "+30 to Intelligence" in lines
    assert all("Cold Resistance" not in ln for ln in lines)
    assert all("Fire and Chaos" not in ln for ln in lines)
    # Added mod placed BEFORE Corrupted trailer
    assert lines[-1] == "Corrupted"
    assert lines[-2] == "+30 to Intelligence"
    assert len(changes) == 3

    # Removing an implicit decrements Implicits count
    after_no_imp, _ = _edit_raw_item_mods(sample, remove_mods=["+30% to Fire Resistance"])
    assert "Implicits: 0" in after_no_imp.splitlines()

    # Non-matching pattern raises clear ValueError
    with pytest.raises(ValueError, match="did not match"):
        _edit_raw_item_mods(sample, replace_mods=[("NonExistentModXYZ", "+10 to Life")])
    with pytest.raises(ValueError, match="did not match"):
        _edit_raw_item_mods(sample, remove_mods=["NonExistentModXYZ"])


def test_parse_matrix_scenario_formats() -> None:
    p_orig = _parse_matrix_scenario("original")
    assert p_orig["name"] == "Original" and p_orig["base"] == "Original"

    p_uber = _parse_matrix_scenario("Uber")
    assert p_uber["name"] == "Uber" and p_uber["base"] == "Uber"

    p_wither = _parse_matrix_scenario("Wither15:MultiplierWitheredStackCount=15")
    assert p_wither["name"] == "Wither15"
    assert p_wither["config"] == [("MultiplierWitheredStackCount", "15")]

    p_noguard = _parse_matrix_scenario("NoGuard:FlasksOff")
    assert p_noguard["name"] == "NoGuard"
    assert p_noguard["flasks"] is False

    p_dict = _parse_matrix_scenario(
        {"name": "CustomPinnacle", "base": "Pinnacle", "config": {"usePowerCharges": True}, "flasks": False}
    )
    assert p_dict["name"] == "CustomPinnacle"
    assert p_dict["base"] == "Pinnacle"
    assert p_dict["config"] == [("usePowerCharges", True)]
    assert p_dict["flasks"] is False


# ---------------------------------------------------------------------------
# Real headless BridgeWorker integration tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pob_worker():
    """Boot one isolated headless pob-zh.exe worker for build-ops integration tests (never touches .runtime/)."""
    worker = BridgeWorker(slot_name="test", pob_dir=POB_DIR)
    worker.start()

    # Build a clean, deterministic test character in memory (zero network dependency)
    # Witch -> Elementalist with Fireball, Ring 1, Belt, Flask 1, and an allocated jewel socket
    lc = worker.call("list_classes", {})
    witch = next(c for c in lc["classes"] if c["name"] == "Witch")
    worker.call("set_class", {"classId": witch["id"]})
    elem = next(a for a in witch["ascendancies"] if a["name"] == "Elementalist")
    worker.call("set_ascendancy", {"ascendClassId": elem["id"]})

    # Add Fireball main skill
    worker.call("add_group", {"label": "Main Fireball"})
    worker.call("add_gem", {"group": 1, "nameSpec": "Fireball", "level": 20, "quality": 20, "enabled": True})
    worker.call("set_build_field", {"field": "mainSocketGroup", "value": 1})

    # Equip Ring 1, Belt, and an active Diamond Flask
    ring_raw = "\n".join(
        [
            "Rarity: RARE",
            "Test Storm Loop",
            "Ruby Ring",
            "Item Level: 84",
            "Implicits: 1",
            "+30% to Fire Resistance",
            "+70 to maximum Life",
            "+40% to Cold Resistance",
            "+30 to maximum Mana",
        ]
    )
    belt_raw = "\n".join(
        [
            "Rarity: RARE",
            "Test Heavy Belt",
            "Leather Belt",
            "Item Level: 84",
            "Implicits: 1",
            "+40 to maximum Life",
            "+80 to maximum Life",
            "+35% to Fire Resistance",
        ]
    )
    flask_raw = "\n".join(
        [
            "Rarity: MAGIC",
            "Diamond Flask",
            "Quality: 20",
            "Implicits: 0",
        ]
    )
    worker.call("add_item", {"raw": ring_raw, "slotName": "Ring 1", "equip": True})
    worker.call("add_item", {"raw": belt_raw, "slotName": "Belt", "equip": True})
    worker.call("add_item", {"raw": flask_raw, "slotName": "Flask 1", "equip": True})
    worker.call("set_slot_active", {"slotName": "Flask 1", "active": True})

    # Allocate the nearest base-tree jewel socket to Witch start and socket a Cobalt Jewel
    items_res = worker.call("list_items", {})
    socket_slots = [s for s in items_res.get("slots", []) if s.get("nodeId")]
    best_sock = None
    best_cost = 9999
    for sock in socket_slots:
        try:
            hover = worker.call("node_hover", {"id": int(sock["nodeId"])})
        except Exception:
            continue
        cost = hover.get("cost")
        if cost is not None and int(cost) < best_cost:
            best_cost = int(cost)
            best_sock = sock
    if best_sock is not None:
        worker.call("tree_click", {"id": int(best_sock["nodeId"])})
        jewel_raw = "\n".join(
            [
                "Rarity: RARE",
                "Test Cobalt Jewel",
                "Cobalt Jewel",
                "Implicits: 0",
                "+15 to Intelligence",
                "7% increased maximum Life",
            ]
        )
        worker.call("add_item", {"raw": jewel_raw, "slotName": best_sock["name"], "equip": True})

    # Set a config var & custom mod for inspection tests
    worker.call("set_config", {"var": "usePowerCharges", "value": True})
    worker.call(
        "set_custom_mods",
        {"list": [{"title": "Default", "text": "+25 to maximum Mana", "enabled": True}]},
    )
    worker.update_checkpoint()
    worker.undo_stack.clear()
    worker.redo_stack.clear()

    try:
        yield worker
    finally:
        worker.stop()


def test_inspect_config(pob_worker: BridgeWorker) -> None:
    cfg = inspect_config(pob_worker)
    assert cfg["activeConfigSetId"] is not None
    assert cfg["activeConfigSet"] is not None
    vars_by_name = {v["var"]: v for v in cfg["variables"]}
    assert "usePowerCharges" in vars_by_name
    assert vars_by_name["usePowerCharges"]["value"] is True
    assert vars_by_name["usePowerCharges"]["defaultValue"] is False
    assert "enemyIsBoss" in vars_by_name
    assert "+25 to maximum Mana" in cfg["customModsText"]
    assert "+25 to maximum Mana" in cfg["activeCustomModLines"]

    cfg_all = inspect_config(pob_worker, include_defaults=True)
    assert len(cfg_all["variables"]) > len(cfg["variables"])
    assert cfg_all["totalCount"] == len(cfg_all["variables"])


def test_inspect_items_all_raw_and_backward_compat(pob_worker: BridgeWorker) -> None:
    res = inspect_items(pob_worker, all_raw=True)
    assert res["allRaw"] is True
    slots_by_name = {s["slotName"]: s for s in res["equippedSlots"]}
    assert "Ring 1" in slots_by_name
    assert "Belt" in slots_by_name
    assert "Flask 1" in slots_by_name
    assert "+40% to Cold Resistance" in (slots_by_name["Ring 1"].get("raw") or "")
    assert "+80 to maximum Life" in (slots_by_name["Belt"].get("raw") or "")

    # Allocated jewel socket must be included with raw text
    jewel_entries = [s for s in res["equippedSlots"] if s.get("nodeId") and s.get("itemId")]
    assert len(jewel_entries) >= 1
    assert "Cobalt Jewel" in (jewel_entries[0].get("raw") or "")

    # Backward-compatible single-slot lookup
    single = inspect_items(pob_worker, slot_name="Ring 1", raw=True)
    assert single["slot"]["name"] == "Ring 1"
    assert "+40% to Cold Resistance" in (single["raw"] or "")


def test_search_calcs_breakdown_bilingual(pob_worker: BridgeWorker) -> None:
    # 1. English spaced query
    res_avg = search_calcs_breakdown(pob_worker, "Average Hit")
    assert res_avg["totalMatches"] >= 1
    top_avg = res_avg["matches"][0]
    assert "average hit" in top_avg["rowLabel"].lower()
    assert any(c.get("breakdown") for c in top_avg["cells"])

    # 2. Compact/camelCase query ('Average Hit' == 'averagehit')
    res_compact = search_calcs_breakdown(pob_worker, "averagehit")
    assert res_compact["totalMatches"] >= 1
    assert "average hit" in res_compact["matches"][0]["rowLabel"].lower()

    # 3. English semantic synonym query ('Mana Regeneration' -> Mana / Recovery)
    res_mana_regen = search_calcs_breakdown(pob_worker, "Mana Regeneration")
    assert res_mana_regen["totalMatches"] >= 1
    top_mr = res_mana_regen["matches"][0]
    assert top_mr["sectionId"] == "Mana"
    assert "recovery" in top_mr["rowLabel"].lower()
    assert any(c.get("breakdown") for c in top_mr["cells"])

    # 4. Chinese queries
    res_ehp_zh = search_calcs_breakdown(pob_worker, "有效生命")
    assert res_ehp_zh["totalMatches"] >= 1

    res_mr_zh = search_calcs_breakdown(pob_worker, "魔力回复")
    assert res_mr_zh["totalMatches"] >= 1
    assert res_mr_zh["matches"][0]["sectionId"] == "Mana"

    # 5. Even when translate is temporarily turned OFF, ZH queries still work AND translate state is restored!
    pob_worker.call("set_translate", {"enabled": False})
    try:
        res_zh_off = search_calcs_breakdown(pob_worker, "有效生命")
        assert res_zh_off["totalMatches"] >= 1
        assert pob_worker.call("version", {})["translate"] is False
    finally:
        pob_worker.call("set_translate", {"enabled": True})


def _capture_semantic_state(worker: BridgeWorker) -> dict:
    """Capture full semantic build state for strict 0-residue verification (independent of Lua XML attribute order)."""
    stats = worker.call("get_stats", {})["stats"]
    cls = worker.call("list_classes", {})["current"]
    cfg = inspect_config(worker)
    items = inspect_items(worker, all_raw=True)
    return {
        "stats": stats,
        "class": cls,
        "configVars": {v["var"]: v["value"] for v in cfg["variables"]},
        "customModsText": cfg["customModsText"],
        "equippedRaw": {s["slotName"]: s.get("raw") for s in items["equippedSlots"]},
    }


def test_modify_config_append_custom_mods_what_if_and_apply(pob_worker: BridgeWorker) -> None:
    before_state = _capture_semantic_state(pob_worker)
    before_life = float(before_state["stats"]["Life"])

    # 1. What-if append (0-residue rollback)
    res_whatif = modify_config(
        pob_worker,
        append_custom_mods="+200 to maximum Life; +10% to all Elemental Resistances",
        apply=False,
    )
    assert res_whatif["applied"] is False
    assert res_whatif["after"]["life"] > before_life + 150
    assert _capture_semantic_state(pob_worker) == before_state

    # 2. Apply append -> preserves existing custom mod AND adds new one, then test clear + append strips '# disabled'
    res_apply = modify_config(
        pob_worker,
        assignments={"MultiplierWitheredStackCount": 10},
        append_custom_mods="+200 to maximum Life",
        apply=True,
    )
    assert res_apply["applied"] is True
    try:
        cfg_applied = inspect_config(pob_worker)
        assert "+25 to maximum Mana" in cfg_applied["customModsText"]
        assert "+200 to maximum Life" in cfg_applied["customModsText"]
        vars_by_name = {v["var"]: v for v in cfg_applied["variables"]}
        assert vars_by_name["multiplierWitheredStackCount"]["value"] == 10

        # Clear customMods ('# disabled' placeholder), then append must strip '# disabled'
        modify_config(pob_worker, custom_mods="clear", apply=True)
        try:
            res_after_clear = modify_config(
                pob_worker,
                append_custom_mods="+100 to maximum Energy Shield",
                apply=False,
            )
            appended_entry = next(a for a in res_after_clear["detail"]["assignments"] if a["var"] == "customMods")
            assert "# disabled" not in appended_entry["customModsText"]
            assert appended_entry["customModsText"] == "+100 to maximum Energy Shield"
        finally:
            undo_build(pob_worker)
    finally:
        undo_build(pob_worker)
        pob_worker.redo_stack.clear()

    assert _capture_semantic_state(pob_worker) == before_state


def test_edit_item_mods_what_if_and_apply(pob_worker: BridgeWorker) -> None:
    before_state = _capture_semantic_state(pob_worker)
    cold_before = float(before_state["stats"]["ColdResist"])
    light_before = float(before_state["stats"]["LightningResist"])

    # 1. What-if replace Cold Res -> Lightning Res (0-residue)
    res_whatif = edit_item_mods(
        pob_worker,
        slot_name="Ring 1",
        replace_mods=[("Cold Resistance", "+45% to Lightning Resistance")],
        apply=False,
    )
    assert res_whatif["applied"] is False
    assert res_whatif["slotName"] == "Ring 1"
    assert "+45% to Lightning Resistance" in res_whatif["afterRaw"]
    assert "Test Storm Loop" in res_whatif["afterRaw"]
    deltas = {d["stat"]: d for d in res_whatif["mutation"]["diff"]["deltas"]}
    assert deltas["ColdResist"]["after"] < cold_before
    assert deltas["LightningResist"]["after"] > light_before
    assert _capture_semantic_state(pob_worker) == before_state

    # 2. Apply add_mods -> persists, then undo restores
    res_apply = edit_item_mods(
        pob_worker,
        slot_name="Ring 1",
        add_mods=["+150 to maximum Life"],
        apply=True,
    )
    assert res_apply["applied"] is True
    try:
        ring_after = inspect_items(pob_worker, slot_name="Ring 1", raw=True)
        assert "+150 to maximum Life" in (ring_after["raw"] or "")
    finally:
        undo_build(pob_worker)
        pob_worker.redo_stack.clear()

    assert _capture_semantic_state(pob_worker) == before_state


def test_search_and_try_item_db(pob_worker: BridgeWorker) -> None:
    before_state = _capture_semantic_state(pob_worker)

    # Pure search EN
    search_en = search_and_try_item_db(pob_worker, query="Mageblood", db_type="UNIQUE", limit=5)
    assert search_en["equipped"] is False
    assert search_en["total"] >= 1
    assert search_en["matches"][0]["title"] == "Mageblood"
    assert search_en["matches"][0].get("raw")
    assert search_en["matches"][0].get("tooltipLines")

    # Pure search ZH
    search_zh = search_and_try_item_db(pob_worker, query="法师之血", db_type="UNIQUE", limit=5)
    assert search_zh["total"] >= 1
    assert search_zh["matches"][0]["title"] == "Mageblood"

    # Try equip (what-if, 0-residue)
    try_res = search_and_try_item_db(
        pob_worker,
        query="Mageblood",
        db_type="UNIQUE",
        slot_name="Belt",
        equip=True,
        apply=False,
    )
    assert try_res["equipped"] is True
    assert try_res["applied"] is False
    assert try_res["slotName"] == "Belt"
    assert try_res["tooltip"] is not None
    assert try_res["mutation"] is not None
    assert _capture_semantic_state(pob_worker) == before_state


def test_set_class_ascendancy_what_if_and_apply(pob_worker: BridgeWorker) -> None:
    before_state = _capture_semantic_state(pob_worker)

    # 1. What-if switch to Templar / Hierophant (ZH) + Aul Bloodline (0-residue)
    res_whatif = set_class_ascendancy(
        pob_worker,
        class_name="Templar",
        ascendancy_name="圣宗",
        secondary_ascendancy_name="Aul Bloodline",
        apply=False,
    )
    assert res_whatif["applied"] is False
    after_detail = res_whatif["detail"]["after"]
    assert after_detail["className"] == "Templar"
    assert after_detail["ascendClassName"] == "Hierophant"
    assert after_detail["secondaryAscendClassId"] > 0
    assert _capture_semantic_state(pob_worker) == before_state

    # 2. Apply switch to Reliquarian (inferred Scion) + Abyssal Bloodline, then undo
    res_apply = set_class_ascendancy(
        pob_worker,
        ascendancy_name="Reliquarian",
        secondary_ascendancy_name="深渊血脉",
        apply=True,
    )
    assert res_apply["applied"] is True
    try:
        cur = pob_worker.call("list_classes", {})["current"]
        assert cur["className"] == "Scion"
        assert cur["ascendClassName"] == "Reliquarian"
        assert cur["secondaryAscendClassId"] > 0
    finally:
        undo_build(pob_worker)
        pob_worker.redo_stack.clear()

    assert _capture_semantic_state(pob_worker) == before_state


def test_run_matrix_zero_residue(pob_worker: BridgeWorker) -> None:
    before_state = _capture_semantic_state(pob_worker)
    before_undo = list(pob_worker.undo_stack)
    before_redo = list(pob_worker.redo_stack)
    before_last_good = pob_worker.last_good_code

    # Default 4 scenarios
    res_default = run_matrix(pob_worker)
    assert res_default["baseline"] == "Original"
    names = [s["name"] for s in res_default["scenarios"]]
    assert names == ["Original", "Pinnacle", "Uber", "FlasksOff"]
    # Uber Boss DPS should be strictly lower than Original (Original has Power Charges + Diamond Flask + Pinnacle)
    orig_dps = res_default["scenarios"][0]["digest"]["primaryDps"]
    pinnacle_dps = res_default["scenarios"][1]["digest"]["primaryDps"]
    uber_dps = res_default["scenarios"][2]["digest"]["primaryDps"]
    flasks_off_dps = res_default["scenarios"][3]["digest"]["primaryDps"]
    assert orig_dps > pinnacle_dps > uber_dps > 0
    assert orig_dps > flasks_off_dps > 0

    # Custom scenarios (string + dict)
    res_custom = run_matrix(
        pob_worker,
        scenarios=[
            "Original",
            "Wither15:MultiplierWitheredStackCount=15",
            "NoGuard:FlasksOff",
            {"name": "PinnaclePlusLife", "base": "Pinnacle", "customMods": "+500 to maximum Life"},
        ],
    )
    assert [s["name"] for s in res_custom["scenarios"]] == [
        "Original",
        "Wither15",
        "NoGuard",
        "PinnaclePlusLife",
    ]
    orig_life = res_custom["scenarios"][0]["digest"]["life"]
    plus_life = res_custom["scenarios"][3]["digest"]["life"]
    assert plus_life >= orig_life + 500

    # Strict 0-residue & stack preservation check
    assert _capture_semantic_state(pob_worker) == before_state
    assert pob_worker.undo_stack == before_undo
    assert pob_worker.redo_stack == before_redo
    assert pob_worker.last_good_code == before_last_good
