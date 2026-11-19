"""Unit and benchmark tests for local PoB game data parser and SQLite index."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from pob_cli.game_data import (
    ensure_gamedata_index,
    query_local_data,
    resolve_pob_dir,
)

REAL_POB_DIR = Path("F:/poe/PobTools[20261102]")
HAS_REAL_POB = (REAL_POB_DIR / "PoE1" / "Data" / "Gems.lua").is_file()


@pytest.mark.skipif(not HAS_REAL_POB, reason="Real PobTools[20261102] directory not available")
def test_cold_build_and_warm_query_benchmarks(tmp_path: Path) -> None:
    """Verify cold SQLite index builds < 5s and warm queries execute < 15ms."""
    t0 = time.perf_counter()
    db_path = ensure_gamedata_index(pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    cold_sec = time.perf_counter() - t0

    assert db_path.is_file()
    assert db_path.parent == tmp_path / "cache"
    assert db_path.name.startswith("gamedata_") and db_path.name.endswith(".sqlite")
    assert cold_sec < 5.0, f"Cold build took {cold_sec:.2f}s (expected < 5.0s)"

    # Subsequent ensure_gamedata_index should hit cache immediately
    t_cache = time.perf_counter()
    db_path_2 = ensure_gamedata_index(pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    cache_ms = (time.perf_counter() - t_cache) * 1000.0
    assert db_path_2 == db_path
    assert cache_ms < 15.0, f"Warm index check took {cache_ms:.2f}ms"

    warm_cases: list[tuple[str, str, dict[str, object]]] = [
        ("mod", "Life Converted to Energy Shield", {}),
        ("mod", "生命转", {}),
        ("mod", "maximum Life", {"slot": "body_armour", "ilvl": 81}),
        ("mod", "Prefixes Cannot Be Changed", {"slot": "ring"}),
        ("gem", "Dark Bargain", {}),
        ("unique", "Sap of the Seasons", {}),
        ("base", "Amber Amulet", {}),
        ("translate", "喋血之刃", {}),
        ("translate", "Dark Bargain", {}),
    ]
    for kind, query, kwargs in warm_cases:
        tq = time.perf_counter()
        res = query_local_data(kind, query, pob_dir=REAL_POB_DIR, runtime_dir=tmp_path, **kwargs)  # type: ignore[arg-type]
        elapsed_ms = (time.perf_counter() - tq) * 1000.0
        assert res["count"] >= 1, f"Expected >= 1 hit for {kind}({query!r})"
        assert elapsed_ms < 15.0, f"Warm query {kind}({query!r}) took {elapsed_ms:.2f}ms (expected < 15ms)"


@pytest.mark.skipif(not HAS_REAL_POB, reason="Real PobTools[20261102] directory not available")
def test_mod_bilingual_foulborn_bench_and_filters(tmp_path: Path) -> None:
    """Verify mod search in EN/ZH, Foulborn mods, ModMaster bench crafts, and slot/ilvl filters."""
    res_en = query_local_data("mod", "Life Converted to Energy Shield", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_en["zhAvailable"] is True
    ids_en = {m["id"] for m in res_en["results"]}
    assert "BreachBodyMaximumLifeConvertedToEnergyShield1___" in ids_en
    assert any(m["source"] == "Foulborn" for m in res_en["results"])

    res_zh = query_local_data("mod", "生命转", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    ids_zh = {m["id"] for m in res_zh["results"]}
    assert "BreachBodyMaximumLifeConvertedToEnergyShield1___" in ids_zh
    assert any("生命转" in (line or "") for m in res_zh["results"] for line in m["stats_zh"])

    # Slot + ilvl filter: Prime (ilvl 86) excluded at ilvl=81, Rapturous (ilvl 81) included
    res_body = query_local_data(
        "mod", "to maximum Life", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path, slot="Body Armour", ilvl=81, limit=50
    )
    body_ids = {m["id"] for m in res_body["results"]}
    assert "IncreasedLife11" in body_ids
    assert "IncreasedLife12" not in body_ids
    for m in res_body["results"]:
        assert m["level"] <= 81
        if m["source"] == "Bench":
            assert "Body Armour" in m["applicable_types"]
        else:
            assert any(
                (k == "body_armour" or k.startswith("body_armour_") or k == "default") and v > 0
                for k, v in m["weights"].items()
            )

    # Bench craft (ModMaster)
    res_bench = query_local_data(
        "mod", "Prefixes Cannot Be Changed", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path, slot="ring"
    )
    assert res_bench["count"] >= 1
    bench = res_bench["results"][0]
    assert bench["source"] == "Bench"
    assert bench["generation_type"] == "Bench Suffix"
    assert "Ring" in bench["applicable_types"]
    assert bench["bench_info"]["source"] == "Crafting Bench"

    # Tincture, Eldritch, JewelCluster coverage
    res_tinc = query_local_data(
        "mod", "Mana Burn", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path, slot="tincture"
    )
    assert any(m["source"] == "Tincture" and m["weights"].get("tincture", 0) > 0 for m in res_tinc["results"])

    res_eldritch = query_local_data(
        "mod", "Attack Speed", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path, slot="gloves", limit=50
    )
    assert any(m["source"] == "Eldritch" and m["generation_type"] in ("Exarch", "Eater") for m in res_eldritch["results"])

    res_cluster = query_local_data(
        "mod", "Prodigious Defence", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path
    )
    assert any(m["source"] == "JewelCluster" for m in res_cluster["results"])


@pytest.mark.skipif(not HAS_REAL_POB, reason="Real PobTools[20261102] directory not available")
def test_gem_stats_and_variants(tmp_path: Path) -> None:
    """Verify gem stats at levels 1/20/21, quality stats, ZH names, and transfigured/vaal/awakened variants."""
    res = query_local_data("gem", "Dark Bargain", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res["count"] >= 2
    dp = res["results"][0]
    assert dp["name"] == "Dark Bargain"
    assert dp["zh_name"] == "黑暗交易"
    assert dp["color"] == "Blue"
    assert dp["req_int"] == 100
    assert dp["cast_time"] == 0.5
    assert "1" in dp["levels"] and "20" in dp["levels"] and "21" in dp["levels"]
    assert dp["levels"]["20"]["stats"]["spell_minimum_base_chaos_damage"] > dp["levels"]["1"]["stats"]["spell_minimum_base_chaos_damage"]
    assert dp["quality_stats"][0]["at_20_quality"] == 2.0
    assert any(v["name"] == "Dark Bargain of Trarthus" and v["relation"] == "transfigured" for v in dp["variants"])

    # ZH gem query
    res_zh = query_local_data("gem", "黑暗交易", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_zh["results"][0]["name"] == "Dark Bargain"

    # Vaal variant discovery
    res_fb = query_local_data("gem", "Fireball", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    fb = res_fb["results"][0]
    assert fb["name"] == "Fireball"
    assert any(v["name"] == "Vaal Fireball" and v["relation"] == "vaal" for v in fb["variants"])

    # Support + Awakened variant discovery
    res_sup = query_local_data("gem", "Added Fire Damage Support", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    sup = res_sup["results"][0]
    assert sup["is_support"] is True
    assert any(v["name"] == "Awakened Added Fire Damage" and v["relation"] == "awakened" for v in sup["variants"])


@pytest.mark.skipif(not HAS_REAL_POB, reason="Real PobTools[20261102] directory not available")
def test_unique_and_base_and_translate(tmp_path: Path) -> None:
    """Verify unique (incl. Foulborn), base, and bidirectional translate queries."""
    res_sap = query_local_data("unique", "Sap of the Seasons", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_sap["count"] >= 1
    sap = res_sap["results"][0]
    assert sap["name"] == "Sap of the Seasons"
    assert sap["zh_name"] == "四季之侵"
    assert sap["base_type"] == "Prismatic Tincture"
    assert sap["base_type_zh"] == "虹彩酊剂"
    assert sap["item_class"] == "Tincture"
    assert sap["zh_foulborn_name"] == "秽生 四季之侵"
    assert len(sap["explicits"]) >= 3

    res_ab = query_local_data("unique", "Foulborn Abyssus", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_ab["count"] >= 1
    ab = res_ab["results"][0]
    assert ab["name"] == "Abyssus"
    assert len(ab["foulborn_mods"]) >= 1

    res_base = query_local_data("base", "Amber Amulet", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_base["count"] == 1
    amber = res_base["results"][0]
    assert amber["zh_name"] == "琥珀护身符"
    assert amber["type"] == "Amulet"
    assert "+(20-30) to Strength" in amber["implicits"]

    res_flask = query_local_data("base", "Small Life Flask", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_flask["results"][0]["properties"]["flask"]["life"] == 70

    # Bidirectional translation exact-first ranking
    res_tr1 = query_local_data("translate", "喋血之刃", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_tr1["count"] >= 2
    assert res_tr1["results"][0]["match_type"] == "exact"
    assert {r["en"] for r in res_tr1["results"]} >= {"Bloodsoaked Blade", "Vampiric Blade"}

    res_tr2 = query_local_data("translate", "Dark Bargain", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_tr2["results"][0] == {
        "en": "Dark Bargain",
        "zh": "黑暗交易",
        "category": "gem",
        "match_type": "exact",
    }


def _create_synthetic_pob_dir(base_dir: Path, *, pob_ver: str = "2.99.0", extra_mod_level: int = 10) -> Path:
    poe1 = base_dir / "PoE1"
    data = poe1 / "Data"
    (data / "Skills").mkdir(parents=True, exist_ok=True)
    (data / "Bases").mkdir(parents=True, exist_ok=True)
    (data / "Uniques").mkdir(parents=True, exist_ok=True)

    (poe1 / "manifest.xml").write_text(
        f"<?xml version='1.0' encoding='utf-8'?><PoBVersion><Version number=\"{pob_ver}\" /></PoBVersion>",
        encoding="utf-8",
    )
    (data / "Gems.lua").write_text(
        """return {
    ["Metadata/Items/Gems/SkillGemTestBolt"] = {
        name = "Test Bolt",
        baseTypeName = "Test Bolt",
        gameId = "Metadata/Items/Gems/SkillGemTestBolt",
        variantId = "TestBolt",
        grantedEffectId = "TestBolt",
        tags = { intelligence = true, spell = true, },
        tagString = "Spell",
        reqStr = 0, reqDex = 0, reqInt = 50,
        naturalMaxLevel = 20,
    },
}""",
        encoding="utf-8",
    )
    (data / "Skills" / "act_int.lua").write_text(
        """local skills = ...
skills["TestBolt"] = {
    name = "Test Bolt",
    color = 3,
    description = "Fires a test bolt.",
    castTime = 0.7,
    qualityStats = { { "spell_damage_+%", 1 }, },
    stats = { "spell_minimum_base_fire_damage", },
    levels = {
        [1] = { 10, critChance = 5, levelRequirement = 1, cost = { Mana = 5 }, statInterpolation = { 1 }, },
        [20] = { 100, critChance = 6, levelRequirement = 70, cost = { Mana = 15 }, statInterpolation = { 1 }, },
    },
}""",
        encoding="utf-8",
    )
    (data / "Bases" / "amulet.lua").write_text(
        """local itemBases = ...
itemBases["Test Amulet"] = {
    type = "Amulet",
    tags = { amulet = true, default = true, },
    implicit = "+10 to Intelligence",
    req = { level = 12, },
}""",
        encoding="utf-8",
    )
    (data / "Uniques" / "amulet.lua").write_text(
        """return {
[[
Heart of Test
Test Amulet
Requires Level 12
Implicits: 1
+10 to Intelligence
+50 to maximum Life
]]
}""",
        encoding="utf-8",
    )
    (data / "ModExplicit.lua").write_text(
        f"""return {{
    ["TestLife1"] = {{ type = "Prefix", affix = "Testing", "+({extra_mod_level}-20) to maximum Life", level = {extra_mod_level}, group = "IncreasedLife", weightKey = {{ "amulet", "default" }}, weightVal = {{ 1000, 0 }}, modTags = {{ "life" }} }},
}}""",
        encoding="utf-8",
    )
    return base_dir


def test_no_zh_fallback_resolution_and_auto_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify --no-zh mode, <runtime_dir>/engine resolution, auto rebuild on update, and invalid kind error."""
    monkeypatch.delenv("POB_ROOT", raising=False)
    rt = tmp_path / "rt"
    engine_dir = _create_synthetic_pob_dir(rt / "engine", pob_ver="2.99.0", extra_mod_level=10)

    resolved = resolve_pob_dir(None, rt)
    assert resolved == engine_dir

    # --no-zh mode works 100% in English without Data/poe1/zh-rCN
    res_gem = query_local_data("gem", "Test Bolt", runtime_dir=rt)
    assert res_gem["zhAvailable"] is False
    assert res_gem["pobVersion"] == "2.99.0"
    assert res_gem["dataVersion"].startswith("nozh-")
    assert res_gem["results"][0]["zh_name"] is None
    assert res_gem["results"][0]["levels"]["20"]["stats"]["spell_minimum_base_fire_damage"] == 100

    res_mod = query_local_data("mod", "maximum Life", runtime_dir=rt, slot="amulet")
    assert res_mod["count"] == 1
    assert res_mod["results"][0]["level"] == 10
    assert res_mod["results"][0]["stats_zh"] == []

    res_tr = query_local_data("translate", "Test Bolt", runtime_dir=rt)
    assert res_tr["zhAvailable"] is False
    assert res_tr["results"][0]["en"] == "Test Bolt"
    assert res_tr["results"][0]["zh"] is None

    # Update engine data & version -> automatic index rebuild
    _create_synthetic_pob_dir(engine_dir, pob_ver="2.99.1", extra_mod_level=44)
    res_mod2 = query_local_data("mod", "maximum Life", runtime_dir=rt, slot="amulet")
    assert res_mod2["pobVersion"] == "2.99.1"
    assert res_mod2["results"][0]["level"] == 44

    with pytest.raises(ValueError, match="Unsupported kind"):
        query_local_data("invalid_kind", "foo", runtime_dir=rt)
