"""End-to-end integration tests against real pob-zh.exe --engine-headless using AWqOLSSmyG."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

GOLDEN_URL = "https://poedb.tw/pob/AWqOLSSmyG"


def _run_pob(*args: str, timeout: float = 180.0) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "pob_cli.cli", *args, "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=True,
    )
    return json.loads(proc.stdout)


def test_e2e_m1_load_summary_audit_calc(tmp_path: Path) -> None:
    """Verify M1: load AWqOLSSmyG, summary, audit 4-dimension dewatering, items/skills/tree/calc, XML roundtrip."""
    load_res = _run_pob("load", GOLDEN_URL)
    info = load_res["buildInfo"]
    assert info["level"] == 94
    assert info["className"] == "Templar"
    assert info["ascendClassName"] == "Hierophant"

    sum_res = _run_pob("summary")
    assert sum_res["offence"]["CombinedDPS"] > 100000
    assert sum_res["defence"]["Life"] == 3501
    assert sum_res["defence"]["Mana"] == 9075
    assert sum_res["defence"]["TotalEHP"] > 50000

    # Verify audit non-destructiveness
    audit_res = _run_pob("audit")
    assert "inflationStripped" in audit_res
    assert audit_res["original"]["primaryDps"] == pytest.approx(sum_res["offence"]["CombinedDPS"], rel=1e-4)

    # Verify tree #1 jewel socket search
    tree_res = _run_pob("tree", "--search", "#1")
    assert tree_res["totalMatches"] >= 1
    assert tree_res["matches"][0]["socketIndex"] == 1

    # Verify calc breakdown
    calc_res = _run_pob("calc", "Mana")
    assert calc_res["totalMatches"] >= 1

    # Verify save & load local XML (R11)
    xml_file = tmp_path / "golden_saved.xml"
    save_res = _run_pob("save", str(xml_file))
    assert xml_file.is_file()
    assert save_res["bytes"] > 1000

    reload_res = _run_pob("load", str(xml_file))
    assert reload_res["importMethod"] == "xml_file"
    assert reload_res["buildInfo"]["level"] == 94


def test_e2e_m2_what_if_apply_undo_redo_and_anoint() -> None:
    """Verify M2: zero-residue what-if, --apply, undo/redo, idempotent tree change (R12), and anoint validation."""
    sum_before = _run_pob("summary")
    dps_before = sum_before["offence"]["CombinedDPS"]
    ehp_before = sum_before["defence"]["TotalEHP"]

    # 1. What-if flask off (single & 'all' per M2 acceptance test, must not mutate state)
    flask_whatif = _run_pob("item", "flask", "1", "off")
    assert flask_whatif["applied"] is False

    flask_all_whatif = _run_pob("item", "flask", "all", "off")
    assert flask_all_whatif["applied"] is False
    assert len(flask_all_whatif["detail"]["slots"]) == 5

    # Verify `pob item try` non-JSON tooltip lines and multi-slot Ring comparison (P1-1)
    sample_ring = "Rarity: RARE\nTest Ring\nRuby Ring\n+30% to Fire Resistance\n+70 to maximum Life\n+50 to maximum Mana"
    try_ring_json = _run_pob("item", "try", "--raw", sample_ring, "--tooltip-only")
    slot_names = [st["slotName"] for st in try_ring_json["slotTooltips"]]
    assert "Ring 1" in slot_names and "Ring 2" in slot_names

    try_ring_md = subprocess.run(
        [sys.executable, "-m", "pob_cli.cli", "item", "try", "--raw", sample_ring, "--tooltip-only"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    assert "Ruby Ring" in try_ring_md or "红玉戒指" in try_ring_md
    assert "- id\n" not in try_ring_md

    sum_mid = _run_pob("summary")
    assert sum_mid["offence"]["CombinedDPS"] == pytest.approx(dps_before, rel=1e-6)
    assert sum_mid["defence"]["TotalEHP"] == pytest.approx(ehp_before, rel=1e-6)

    # 2. Apply config change -> undo -> redo -> undo
    cfg_apply = _run_pob("config", "set", "enemyIsBoss=Uber", "--apply")
    assert cfg_apply["applied"] is True
    assert cfg_apply["undoDepth"] >= 1

    undo_res = _run_pob("undo")
    assert undo_res["action"] == "undo"
    sum_after_undo = _run_pob("summary")
    assert sum_after_undo["offence"]["CombinedDPS"] == pytest.approx(dps_before, rel=1e-6)

    redo_res = _run_pob("redo")
    assert redo_res["action"] == "redo"
    _run_pob("undo")

    # 3. Anoint validation: non-existent notable must fail cleanly and leave zero residue
    proc_bad = subprocess.run(
        [sys.executable, "-m", "pob_cli.cli", "item", "anoint", "--slot", "Amulet", "--notable", "NonExistentNotableXYZ999", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc_bad.returncode != 0

    # Valid anoint what-if on Amulet (Charisma / 魅力)
    anoint_ok = _run_pob("item", "anoint", "--slot", "Amulet", "--notable", "Charisma")
    assert anoint_ok["applied"] is False
    assert anoint_ok["detail"]["slotName"] == "Amulet"

    # 4. Idempotent tree change (allocating an already-allocated node is a safe no-op)
    tree_summary = _run_pob("tree")
    allocated_ks = tree_summary["keystones"][0]["id"]
    tree_idemp = _run_pob("tree", "change", "--allocate", str(allocated_ks))
    assert tree_idemp["detail"]["actions"][0]["action"] == "skip_already_allocated"

    # 5. Batch R12 validation: unallocated Mastery node without :effectId must fail cleanly
    unalloc_mastery = _run_pob("tree", "--search", "Mastery")
    unalloc_m_ids = [m["id"] for m in unalloc_mastery["matches"] if m.get("type") == "Mastery" and not m.get("allocated")]
    if unalloc_m_ids:
        bad_batch_ops = json.dumps([{"op": "tree", "allocate": [str(unalloc_m_ids[0])]}])
        proc_batch_bad = subprocess.run(
            [sys.executable, "-m", "pob_cli.cli", "batch", "--json-ops", bad_batch_ops, "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert proc_batch_bad.returncode != 0
        assert "Mastery" in proc_batch_bad.stdout or "Mastery" in proc_batch_bad.stderr


def test_e2e_m3_rankings_compare_and_borrow() -> None:
    """Verify M3: rank-gems, rank-nodes, 3-baseline compare, and borrow."""
    rg_res = _run_pob("rank-gems", "--group", "main", "--top", "5")
    assert len(rg_res["candidates"]) >= 1

    rn_res = _run_pob("rank-nodes", "--stat", "CombinedDPS", "--max-dist", "2", "--top", "5")
    assert len(rn_res["byPerPoint"]) >= 1

    # Load same build into ref, tweak ref config to create artificial inflation, then run compare
    _run_pob("load", GOLDEN_URL, "--target", "ref")
    _run_pob("config", "set", "enemyIsBoss=None", "--target", "ref", "--apply")

    cmp_res = _run_pob("compare")
    assert "original" in cmp_res["baselines"]
    assert "envAlignedToMain" in cmp_res["baselines"]
    assert "pinnacleDewatered" in cmp_res["baselines"]

    # In pinnacleDewatered (Baseline 3), both builds are identical so CombinedDPS diff must be 0
    b3_rows = {r["stat"]: r for r in cmp_res["baselines"]["pinnacleDewatered"]["rows"]}
    assert b3_rows["CombinedDPS"]["main"] == pytest.approx(b3_rows["CombinedDPS"]["ref"], rel=1e-5)

    # Borrow Amulet and main skill group from ref into main (what-if, P0-3)
    borrow_res = _run_pob("borrow", "--item-slot", "Amulet", "--skill-group", "main")
    assert borrow_res["applied"] is False
    assert len(borrow_res["detail"]["borrowedItems"]) == 1
    assert borrow_res["detail"]["borrowedSkillGroup"]["newGroupIndex"] is not None
    assert borrow_res["detail"]["borrowedSkillGroup"]["switchedMain"] is True

    # Verify Vaal hybrid main skill dewatering keeps base skill active (P0-2)
    _run_pob("skill", "set", "--group", "main", "--gem", "1", "--new-gem", "Vaal Ice Nova", "--apply")
    try:
        vaal_audit = _run_pob("audit")
        assert vaal_audit["dewateredPinnacle"]["primaryDps"] > 10000
    finally:
        _run_pob("undo")
