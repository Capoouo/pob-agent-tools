"""Protocol, red-line blocklist, color-strip, and codec unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from pob_cli.bridge_client import FORBIDDEN_METHODS, BridgeError, BridgeWorker
from pob_cli.cli import _prepare_load_source
from pob_cli.external_data import _strip_html, query_price
from pob_cli.formatters import fmt_delta, sanitize_for_output, strip_pob_escapes, to_json_str
from pob_cli.importers import POE_NINJA_CHAR_RE, share_code_to_xml, xml_to_share_code


def test_forbidden_methods_blocked() -> None:
    """Verify R1, R3, R4, R7, R11 static blocklist and R2 dynamic caps.sidebarBreakdown gating."""
    worker = BridgeWorker(slot_name="test")
    expected_blocked = [
        "compare_load",
        "compare_state",
        "compare_select",
        "compare_remove",
        "compare_set",
        "compare_tree",
        "compare_items",
        "compare_skills",
        "compare_config",
        "compare_use",
        "config_mod_search",
        "config_mod_add",
        "set_pob_options",
        "oauth_logout",
        "import_from_uri",
        "save_build_as",
    ]
    for method in expected_blocked:
        assert method in FORBIDDEN_METHODS
        with pytest.raises(BridgeError):
            worker.call(method, {})

    # Dynamic R2: sidebar_breakdown is NOT in static FORBIDDEN_METHODS, but raises when caps.sidebarBreakdown is False
    assert "sidebar_breakdown" not in FORBIDDEN_METHODS

    class _FakeProc:
        def poll(self) -> None:
            return None

    worker._proc = _FakeProc()  # type: ignore[assignment]
    worker.caps = {"sidebarBreakdown": False}
    with pytest.raises(BridgeError, match="R2: sidebarBreakdown"):
        worker.call("sidebar_breakdown", {})


def test_color_escape_stripping_and_raw_preservation() -> None:
    """Verify R9: strip ^xRRGGBB and ^0..^9 in display fields while preserving raw/xml/code/notes/text."""
    colored = "^x33FF77+120^7 to ^1Maximum Life^0"
    assert strip_pob_escapes(colored) == "+120 to Maximum Life"

    payload = {
        "title": "^xD02090Foulborn Skin of the Lords^7",
        "raw": "Rarity: UNIQUE\n^xD02090Foulborn Skin of the Lords^7",
        "xml": "<PathOfBuilding>^x123456</PathOfBuilding>",
        "code": "eNrt^7abc",
        "notes": "Keep ^xABCDEF color in notes",
        "text": "Keep ^7notes text",
        "nested": [{"label": "^2Fire Resist^7"}],
    }
    cleaned = sanitize_for_output(payload)
    assert cleaned["title"] == "Foulborn Skin of the Lords"
    assert cleaned["nested"][0]["label"] == "Fire Resist"
    # Raw keys must remain untouched
    assert cleaned["raw"] == payload["raw"]
    assert cleaned["xml"] == payload["xml"]
    assert cleaned["code"] == payload["code"]
    assert cleaned["notes"] == payload["notes"]
    assert cleaned["text"] == payload["text"]

    # External JSON with sanitize=False must preserve math exponents like 10^6
    ext_json = to_json_str({"summary": "10^6 damage"}, sanitize=False)
    assert "10^6 damage" in ext_json


def test_xml_share_code_roundtrip() -> None:
    """Verify zlib + urlsafe base64 share code round-trip."""
    sample_xml = '<?xml version="1.0" encoding="UTF-8"?>\n<PathOfBuilding><Build level="95" className="Witch"/></PathOfBuilding>'
    code = xml_to_share_code(sample_xml)
    restored = share_code_to_xml(code)
    assert restored == sample_xml


def test_utf8_bom_xml_file_routes_to_xml_not_base64(tmp_path: Path) -> None:
    """Verify Windows UTF-8 BOM XML files and raw BOM strings decode cleanly as XML, not Base64."""
    xml_body = '<?xml version="1.0" encoding="UTF-8"?>\n<PathOfBuilding><Build level="99" className="Scion"/></PathOfBuilding>'
    bom_file = tmp_path / "bom_build.xml"
    bom_file.write_bytes(b"\xef\xbb\xbf" + xml_body.encode("utf-8"))

    src1, xml_code1 = _prepare_load_source(str(bom_file))
    assert src1 is None and xml_code1 is not None
    assert share_code_to_xml(xml_code1) == xml_body

    src2, xml_code2 = _prepare_load_source(f"@{bom_file}")
    assert src2 is None and xml_code2 is not None
    assert share_code_to_xml(xml_code2) == xml_body

    src3, xml_code3 = _prepare_load_source("\ufeff" + xml_body)
    assert src3 is None and xml_code3 is not None
    assert share_code_to_xml(xml_code3) == xml_body


def test_poe_ninja_character_url_regex() -> None:
    """Verify poe.ninja character URL parsing."""
    url = "https://poe.ninja/poe1/builds/mirage/character/ExileAcc-1234/MyNecroChar?i=0&timemachine=week-2"
    m = POE_NINJA_CHAR_RE.match(url)
    assert m is not None
    assert m.group("league") == "mirage"
    assert m.group("account") == "ExileAcc-1234"
    assert m.group("name") == "MyNecroChar"


def test_fmt_delta() -> None:
    assert fmt_delta(1000, 1250) == "+250 (+25.0%)"
    assert fmt_delta(1000, 1000) == "0"


def test_mocked_query_price_all_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify P1-7: category='all' searches DivinationCard, ClusterJewel, ForbiddenJewel, and Scarab."""
    fake_overviews = {
        "Currency": [{"name": "Divine Orb", "chaos": 150.0, "count": 5000, "graph": [1.5, 2.4]}],
        "DivinationCard": [{"name": "The Apothecary", "chaos": 6000.0, "count": 42, "graph": [-3.2]}],
        "ClusterJewel": [{"name": "Large Cluster Jewel", "variant": "8 Passive", "chaos": 45.0, "count": 120, "graph": [0.0]}],
    }
    monkeypatch.setattr("pob_cli.external_data._fetch_dense_overviews", lambda league, ttl_sec=900: fake_overviews)
    res = query_price("The Apothecary", league="Mirage", category="all")
    assert res["totalMatches"] == 1
    hit = res["matches"][0]
    assert hit["name"] == "The Apothecary"
    assert hit["type"] == "DivinationCard"
    assert hit["divineValue"] == pytest.approx(40.0)
    assert hit["trend24hPct"] == -3.2


def test_strip_html_wiki() -> None:
    raw = "<div><style>.foo{color:red}</style><p>Mind Over Matter takes <b>40%</b> of Damage from Mana before Life (10^6).</p></div>"
    cleaned = _strip_html(raw)
    assert "Mind Over Matter takes 40% of Damage from Mana before Life (10^6)." in cleaned
