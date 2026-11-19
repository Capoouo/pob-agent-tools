"""Tests for poe.ninja importers, expanded economy/meta queries, and PoEDB client."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from pob_cli import external_data, importers, poedb_client
from pob_cli.importers import (
    _resolve_proxy,
    fetch_poe_ninja_character_code,
    share_code_to_xml,
    xml_to_share_code,
)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate POB_RUNTIME_DIR so tests never read or pollute F:/poe/.runtime/cache."""
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path))
    return tmp_path


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, text: str = "", json_data: Any = None) -> None:
        self.status_code = status_code
        self._text = text
        self._json = json_data

    @property
    def text(self) -> str:
        if self._text:
            return self._text
        if self._json is not None:
            return json.dumps(self._json)
        return ""

    def json(self) -> Any:
        if self._json is not None:
            return self._json
        return json.loads(self._text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("GET", "https://example.invalid")
            resp = httpx.Response(self.status_code, request=req)
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=req, response=resp)


# ---------------------------------------------------------------------------
# A. importers.py (poe.ninja character import & proxy)
# ---------------------------------------------------------------------------


def test_poe_ninja_char_keeps_hyphen_first_and_uses_build_index_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_xml = '<?xml version="1.0"?><PathOfBuilding><Build level="98"/></PathOfBuilding>'
    sample_code = xml_to_share_code(sample_xml)
    seen_requests: list[tuple[str, dict[str, Any] | None]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str, params: dict[str, Any] | None = None, **_: Any) -> _FakeResponse:
            seen_requests.append((url, params))
            if url.endswith("/build-index-state"):
                return _FakeResponse(
                    json_data={
                        "leagueBuilds": [
                            {"leagueName": "Covens", "leagueUrl": "covens", "total": 120000, "statistics": []}
                        ]
                    }
                )
            if url.endswith("/raw/overview/code"):
                assert params is not None
                if params["account"] == "Song_ycs-4781" and params["overview"] == "covens":
                    return _FakeResponse(status_code=200, text=sample_code)
                return _FakeResponse(status_code=404)
            raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(httpx, "Client", FakeClient)

    code = fetch_poe_ninja_character_code(
        "https://poe.ninja/poe1/builds/covens/character/Song_ycs-4781/%E8%8D%89%E9%A5%B2%E5%B1%B1%E4%B9%9E"
    )
    assert code == sample_code
    assert share_code_to_xml(code) == sample_xml
    code_calls = [p for u, p in seen_requests if u.endswith("/raw/overview/code")]
    assert len(code_calls) == 1
    assert code_calls[0]["account"] == "Song_ycs-4781"
    assert code_calls[0]["type"] == "exp"


def test_poe_ninja_char_retries_hash_only_on_404_and_prefers_delve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_code = xml_to_share_code("<PathOfBuilding><Build/></PathOfBuilding>")
    attempts: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str, params: dict[str, Any] | None = None, **_: Any) -> _FakeResponse:
            if url.endswith("/build-index-state"):
                return _FakeResponse(json_data={"leagueBuilds": []})
            if url.endswith("/index-state"):
                return _FakeResponse(
                    json_data={
                        "buildLeagues": [{"name": "Delve", "url": "delve"}],
                        "oldBuildLeagues": [],
                    }
                )
            if url.endswith("/raw/overview/code"):
                assert params is not None
                attempts.append(dict(params))
                if params["account"] == "Exile-1234":
                    return _FakeResponse(status_code=404)
                if params["account"] == "Exile#1234" and params["type"] == "depthsolo":
                    return _FakeResponse(status_code=200, text=sample_code)
                return _FakeResponse(status_code=404)
            raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(httpx, "Client", FakeClient)

    code = fetch_poe_ninja_character_code("https://poe.ninja/poe1/builds/delve/character/Exile-1234/Miner")
    assert code == sample_code
    assert attempts[0]["type"] == "depthsolo"
    assert attempts[0]["account"] == "Exile-1234"
    assert attempts[1]["type"] == "depthsolo"
    assert attempts[1]["account"] == "Exile#1234"


def test_proxy_resolution_order(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("POB_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8080")
    assert _resolve_proxy() == "http://127.0.0.1:8080"
    monkeypatch.setenv("POB_PROXY", "http://127.0.0.1:7890")
    assert _resolve_proxy() == "http://127.0.0.1:7890"
    assert _resolve_proxy("socks5://127.0.0.1:1080") == "socks5://127.0.0.1:1080"


# ---------------------------------------------------------------------------
# B. external_data.py (52 categories, variant, detailed stash, league cache, meta)
# ---------------------------------------------------------------------------


def test_resolve_current_trade_league_invalidates_stale_standard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_file = tmp_path / "cache" / "poe_trade_league.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"_ts": time.time(), "league": "Standard"}), encoding="utf-8")

    monkeypatch.setattr(
        external_data,
        "_fetch_poe_ninja_index_state",
        lambda ttl_sec=900: {"economyLeagues": [{"name": "Covens", "url": "covens"}]},
    )

    resolved = external_data.resolve_current_trade_league()
    assert resolved == "Covens"
    persisted = json.loads(cache_file.read_text(encoding="utf-8"))
    assert persisted["league"] == "Covens"

    # Subsequent call within 1h uses cached non-Standard league without touching index-state
    monkeypatch.setattr(
        external_data,
        "_fetch_poe_ninja_index_state",
        lambda ttl_sec=900: (_ for _ in ()).throw(AssertionError("should use 1h cache")),
    )
    assert external_data.resolve_current_trade_league() == "Covens"


def test_category_map_covers_all_52_overview_types() -> None:
    expected_52 = {
        "Currency",
        "DivinationCard",
        "Essence",
        "UniqueMap",
        "Map",
        "UniqueJewel",
        "UniqueFlask",
        "UniqueWeapon",
        "UniqueArmour",
        "UniqueAccessory",
        "SkillGem",
        "BaseType",
        "Fossil",
        "Resonator",
        "Scarab",
        "Beast",
        "Incubator",
        "Oil",
        "Vial",
        "DeliriumOrb",
        "Invitation",
        "BlightedMap",
        "ClusterJewel",
        "Artifact",
        "BlightRavagedMap",
        "ScourgedMap",
        "Tattoo",
        "Omen",
        "Fragment",
        "UniqueRelic",
        "Memory",
        "IncursionTemple",
        "Coffin",
        "AllflameEmber",
        "KalguuranRune",
        "UniqueIdol",
        "UniqueTincture",
        "Runegraft",
        "Wombgift",
        "ForbiddenJewel",
        "ValdoMap",
        "DjinnCoin",
        "Astrolabe",
        "ShrineBelt",
        "ImbuedGem",
        "Ducat",
        "EnshroudingCrystal",
        "Flask",
        "ScryingOrb",
        "Corpse",
        "Viscera",
        "Thread",
    }
    assert set(external_data.ALL_OVERVIEW_TYPES) == expected_52
    mapped_types = {t for vals in external_data.CATEGORY_MAP.values() for t in vals}
    assert expected_52.issubset(mapped_types)
    for alias in (
        "imbuedgem",
        "transfiguredgem",
        "runegraft",
        "wombgift",
        "base",
        "flask",
        "corpse",
        "viscera",
        "valdomap",
        "blightedmap",
        "beast",
        "invitation",
        "allflame",
        "astrolabe",
        "memory",
        "incubator",
        "resonator",
        "deliriumorb",
        "vial",
        "kalguuranrune",
        "artifact",
        "omen",
    ):
        assert alias in external_data.CATEGORY_MAP


def test_query_price_variant_and_auto_detailed_stash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dense_mock = {
        "Currency": [{"name": "Divine Orb", "chaos": 220.0, "graph": [0.0]}],
        "ForbiddenJewel": [
            {"name": "Necromancer", "variant": "Forbidden Flame, Necromancer", "chaos": 18500.0, "graph": []},
            {"name": "Trickster", "variant": "Forbidden Flame, Trickster", "chaos": 9000.0, "graph": []},
            {"name": "Necromancer", "variant": "Forbidden Flesh, Necromancer", "chaos": 22300.0, "graph": []},
        ],
        "SkillGem": [
            {"name": "Empower Support", "variant": "4c", "chaos": 1560.0, "graph": []},
            {"name": "Vaal Grace", "variant": "21/20c", "chaos": 450.0, "graph": []},
        ],
        "ClusterJewel": [
            {"name": "Minions deal 10% increased Damage", "variant": "12 passives, 84", "chaos": 7000.0, "graph": []}
        ],
    }

    stash_payloads = {
        "SkillGem": [
            {
                "name": "Empower Support",
                "variant": "4c",
                "chaosValue": 1562.0,
                "divineValue": 7.1,
                "gemLevel": 4,
                "gemQuality": None,
                "corrupted": True,
                "listingCount": 134,
                "detailsId": "empower-support-4c",
                "sparkLine": {"totalChange": 128.2},
            },
            {
                "name": "Vaal Grace",
                "variant": "21/20c",
                "chaosValue": 450.0,
                "divineValue": 2.05,
                "gemLevel": 21,
                "gemQuality": 20,
                "corrupted": True,
                "listingCount": 31,
                "detailsId": "vaal-grace-21-20c",
                "sparkLine": {"totalChange": 12.5},
            },
        ],
        "ClusterJewel": [
            {
                "name": "Minions deal 10% increased Damage",
                "baseType": "Large Cluster Jewel",
                "variant": "12 passives",
                "chaosValue": 7075.0,
                "divineValue": 32.16,
                "listingCount": 48,
                "detailsId": "minions-deal-10-increased-damage-12-passives-84",
                "sparkLine": {"totalChange": 386.5},
                "tradeInfo": [{"mod": "enchant.stat_3086156145", "min": 12, "max": 12}],
            }
        ],
    }

    monkeypatch.setattr(external_data, "_fetch_dense_overviews", lambda league, ttl_sec=900: dense_mock)

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str, params: dict[str, Any] | None = None, **_: Any) -> _FakeResponse:
            assert params is not None
            return _FakeResponse(json_data={"lines": stash_payloads.get(params["type"], [])})

    monkeypatch.setattr(httpx, "Client", FakeClient)

    # 1. Variant filter on Forbidden Flame
    ff = external_data.query_price("Forbidden Flame", league="Covens", variant="Necromancer")
    assert ff["detailed"] is False
    assert ff["totalMatches"] == 1
    assert ff["matches"][0]["name"] == "Necromancer"
    assert "Forbidden Flame" in ff["matches"][0]["variant"]

    # 2. Auto-detailed on 21/20
    vg = external_data.query_price("Vaal Grace 21/20", league="Covens")
    assert vg["detailed"] is True
    assert vg["totalMatches"] == 1
    assert vg["matches"][0]["gemLevel"] == 21
    assert vg["matches"][0]["gemQuality"] == 20
    assert vg["matches"][0]["corrupted"] is True
    assert vg["matches"][0]["listingCount"] == 31
    assert vg["matches"][0]["sparklineChange"] == 12.5
    assert (tmp_path / "cache" / "ninja_stash_Covens_SkillGem.json").exists()

    # 3. Auto-detailed on cluster passive count
    cj = external_data.query_price("Minions deal 10% increased Damage", league="Covens", variant="12 passives")
    assert cj["detailed"] is True
    assert cj["matches"][0]["clusterPassives"] == 12
    assert cj["matches"][0]["tradeInfo"][0]["mod"] == "enchant.stat_3086156145"


def test_query_meta_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_state = {
        "leagueBuilds": [
            {
                "leagueName": "Covens",
                "leagueUrl": "covens",
                "total": 100000,
                "category": 0,
                "hardcore": False,
                "statistics": [
                    {"class": "Chieftain", "skill": "Righteous Fire", "percentage": 5.0, "trend": 1},
                    {"class": "Necromancer", "skill": "Raise Spectre", "percentage": 3.5, "trend": 0},
                    {"class": "Necromancer", "skill": "Summon Raging Spirit", "percentage": 2.0, "trend": -1},
                ],
            },
            {
                "leagueName": "Hardcore Covens",
                "leagueUrl": "covenshc",
                "total": 8000,
                "category": 0,
                "hardcore": True,
                "statistics": [
                    {"class": "Juggernaut", "skill": "Boneshatter", "percentage": 9.0, "trend": 1},
                ],
            },
        ]
    }
    monkeypatch.setattr(external_data, "_fetch_poe_ninja_build_index_state", lambda ttl_sec=900: fake_state)

    res = external_data.query_meta(class_name="Necro")
    assert res["league"] == "Covens"
    assert res["totalBuilds"] == 100000
    assert len(res["ascendancies"]) == 1
    assert res["ascendancies"][0]["class"] == "Necromancer"
    assert res["ascendancies"][0]["percentage"] == 5.5
    assert len(res["topSkills"]) == 2

    res_hc = external_data.query_meta(league="covenshc", skill="Boneshatter")
    assert res_hc["league"] == "Hardcore Covens"
    assert res_hc["hardcore"] is True
    assert res_hc["topSkills"][0]["estimatedBuilds"] == 720


# ---------------------------------------------------------------------------
# C. poedb_client.py (autocomplete discovery, CN/US lookup, ModsView parsing)
# ---------------------------------------------------------------------------


def test_poedb_client_offline_discovery_parsing_and_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_html = '<html><head><script src="https://cdn.poedb.tw/js/poedb_header.abc123.js"></script></head></html>'
    hdr_js = 'var cn="autocomplete_cn.111aaa.json"; var us="autocomplete_us.222bbb.json";'
    ac_cn = [
        {"label": "大恶魔之冠", "value": "Archdemon_Crown", "desc": "头盔"},
        {"label": "法师之血", "value": "Mageblood", "desc": "腰带"},
    ]
    ac_us = [
        {"label": "Archdemon Crown", "value": "Archdemon_Crown", "desc": "Helmets"},
        {"label": "Mageblood", "value": "Mageblood", "desc": "Belts"},
    ]
    crown_html = """
    <html><head><title>大恶魔之冠 - 流亡编年史, Path of Exile Wiki cn</title></head><body>
    <div class="newItemPopup"><div class="content"><div class="Stats">
      <div class="property">护甲: 100</div>
      <div class="requirements">需求 等级 75</div>
      <div class="implicitMod">此物品上装备的辅助技能石等级 -2<br>镶嵌的技能宝石等级提高 +2</div>
    </div></div></div>
    <div class="card name-value"><div class="name">Class</div><div class="value"><a class="ItemClasses" href="Helmets">头盔</a></div></div>
    <div class="card name-value"><div class="name">Tags</div><div class="value">str_int_armour, helmet, armour</div></div>
    <h2>Version history</h2>
    <table class="table"><tbody><tr><td>3.13.0</td><td><li>Introduced Archdemon Crown.</li></td></tr></tbody></table>
    </body></html>
    """
    modsview_payload = {
        "normal": [
            {
                "Name": "鼓舞的",
                "Level": "84",
                "ModGenerationTypeID": "1",
                "ModFamilyList": ["DefencesPercent"],
                "DropChance": 1000,
                "str": "护甲与能量护盾提高 (92—100)%",
                "fossil_no": ["defences", "energy_shield"],
                "mod_no": [],
            },
            {
                "Name": "博学之",
                "Level": "85",
                "ModGenerationTypeID": "2",
                "ModFamilyList": ["Intelligence"],
                "DropChance": 1000,
                "str": "+(56—60) 智慧",
                "fossil_no": ["attribute"],
                "mod_no": [],
            },
        ],
        "corrupted": [
            {
                "Name": "V2MaxPowerChargesCorrupted",
                "Level": "75",
                "ModGenerationTypeID": "5",
                "ModFamilyList": ["IncreasedMaximumPowerCharges"],
                "DropChance": 1000,
                "str": "暴击球数量上限 +1",
                "fossil_no": ["power_charge"],
                "mod_no": [],
            }
        ],
        "shaper": {
            "1879": {
                "Name": "塑界者之",
                "Level": "82",
                "ModGenerationTypeID": "1",
                "ModFamilyList": ["SupportedBySocketedGems"],
                "DropChance": 500,
                "str": "此物品上的技能石受到 20 级急冻辅助",
                "fossil_no": ["cold", "gem"],
                "mod_no": [],
            }
        },
    }
    helm_pool_html = f"""
    <html><head><title>Helmets_str_int - PoEDB</title></head><body>
    <script>$(function() {{ new ModsView({json.dumps(modsview_payload, ensure_ascii=False)}); }});</script>
    </body></html>
    """

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str, **_: Any) -> _FakeResponse:
            if url == "https://poedb.tw/cn/":
                return _FakeResponse(text=home_html)
            if url == "https://cdn.poedb.tw/js/poedb_header.abc123.js":
                return _FakeResponse(text=hdr_js)
            if url == "https://cdn.poedb.tw/json/autocomplete_cn.111aaa.json":
                return _FakeResponse(json_data=ac_cn)
            if url == "https://cdn.poedb.tw/json/autocomplete_us.222bbb.json":
                return _FakeResponse(json_data=ac_us)
            if url == "https://poedb.tw/cn/Archdemon_Crown":
                return _FakeResponse(text=crown_html)
            if url == "https://poedb.tw/cn/Helmets_str_int":
                return _FakeResponse(text=helm_pool_html)
            raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(httpx, "Client", FakeClient)

    res = poedb_client.query_poedb("傲视头冠", include_mods=True)
    assert res["slug"] == "Archdemon_Crown"
    assert res["modPoolSlug"] == "Helmets_str_int"
    assert len(res["implicits"]) == 2
    assert res["versionHistory"][0]["version"] == "3.13.0"
    assert res["modCountTotal"] == 4
    sides = {m["side"] for m in res["mods"]}
    assert {"prefix", "suffix", "corrupted", "influence_shaper_prefix"} <= sides
    assert (tmp_path / "cache" / "poedb_autocomplete_index.json").exists()

    # Cross-language US query against cached CN+US index
    res_en = poedb_client.query_poedb("Archdemon Crown", include_mods=False)
    assert res_en["slug"] == "Archdemon_Crown"

    # Network failure raises RuntimeError with proxy guidance
    class BrokenClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> BrokenClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str, **_: Any) -> _FakeResponse:
            raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx, "Client", BrokenClient)
    with pytest.raises(RuntimeError, match="POB_PROXY"):
        poedb_client.query_poedb("Mageblood")


# ---------------------------------------------------------------------------
# D. Live read-only smoke tests (skipped only if POB_OFFLINE=1)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.environ.get("POB_OFFLINE") == "1", reason="POB_OFFLINE=1")
def test_live_smoke_external_apis() -> None:
    # 1. poe.ninja character code with hyphenated account Song_ycs-4781
    code = fetch_poe_ninja_character_code(
        "https://poe.ninja/poe1/builds/covens/character/Song_ycs-4781/%E8%8D%89%E9%A5%B2%E5%B1%B1%E4%B9%9E"
    )
    xml = share_code_to_xml(code)
    assert "<PathOfBuilding>" in xml

    # 2. Forbidden Flame --variant Necromancer
    ff = external_data.query_price("Forbidden Flame", variant="Necromancer", limit=50)
    assert ff["totalMatches"] >= 1
    assert any("Necromancer" in str(m.get("name")) or "Necromancer" in str(m.get("variant")) for m in ff["matches"])

    # 3. Empower Support --detailed
    emp = external_data.query_price("Empower Support", detailed=True, limit=10)
    assert emp["detailed"] is True
    assert emp["totalMatches"] >= 1
    assert any(m.get("gemLevel") is not None and m.get("listingCount") is not None for m in emp["matches"])

    # 4. query_meta
    meta = external_data.query_meta(top=5)
    assert meta["totalBuilds"] > 0
    assert len(meta["topSkills"]) >= 1

    # 5. PoEDB 傲视头冠 + Archdemon Crown
    pdb_cn = poedb_client.query_poedb("傲视头冠", include_mods=True, limit_mods=20)
    assert pdb_cn["slug"] == "Archdemon_Crown"
    assert pdb_cn["modPoolSlug"] == "Helmets_str_int"
    assert pdb_cn["modCountTotal"] > 100
    assert all(m.get("family") is not None and "dropChance" in m and "fossilNo" in m for m in pdb_cn["mods"])

    pdb_us = poedb_client.query_poedb("Archdemon Crown", include_mods=False, lang="us")
    assert pdb_us["slug"] == "Archdemon_Crown"
    assert pdb_us["title"] == "Archdemon Crown"
