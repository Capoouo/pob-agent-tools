"""CLI integration tests for `pob trade` and `pob bench` (fully offline, mocked network & daemon)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob_cli import cli_trade
from pob_cli.cli import main
from pob_cli.trade_client import CookieRequired, NoResults, RateLimited, TradeClient

SAMPLE_STATS = [
    {
        "label": "Explicit",
        "entries": [
            {"id": "explicit.stat_3372524247", "text": "+#% to Fire Resistance", "type": "explicit"},
            {"id": "explicit.stat_4220027924", "text": "+#% to Cold Resistance", "type": "explicit"},
            {"id": "explicit.stat_1671376347", "text": "+#% to Lightning Resistance", "type": "explicit"},
            {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
            {"id": "explicit.stat_681332047", "text": "#% increased Attack Speed", "type": "explicit"},
            {"id": "explicit.stat_210067635", "text": "#% increased Attack Speed (Local)", "type": "explicit"},
        ],
    },
    {
        "label": "Pseudo",
        "entries": [
            {"id": "pseudo.pseudo_number_of_empty_suffix_mods", "text": "# Empty Suffix Modifiers", "type": "pseudo"},
            {"id": "pseudo.pseudo_number_of_empty_prefix_mods", "text": "# Empty Prefix Modifiers", "type": "pseudo"},
        ],
    },
]

SAMPLE_FETCH_ROWS = [
    {
        "id": "row_fire",
        "item": {
            "name": "Doom Hand",
            "typeLine": "Slink Gloves",
            "rarity": "Rare",
            "frameType": 2,
            "ilvl": 84,
            "properties": [{"name": "Evasion Rating", "values": [["240", 1]]}],
            "explicitMods": [
                {
                    "description": "+42% to Fire Resistance",
                    "flags": {},
                    "mods": [{"name": "of the Kiln", "tier": "S2", "level": 72, "magnitudes": [{"min": "42", "max": "45"}]}],
                },
                {
                    "description": "+85 to maximum Life",
                    "flags": {},
                    "mods": [{"name": "Athlete's", "tier": "P2", "level": 64, "magnitudes": [{"min": "80", "max": "89"}]}],
                },
                {
                    "description": "14% increased Attack Speed",
                    "flags": {},
                    "mods": [{"name": "of Grandmastery", "tier": "S1", "level": 77, "magnitudes": [{"min": "14", "max": "16"}]}],
                },
            ],
        },
        "listing": {"price": {"amount": 15, "currency": "chaos"}},
    },
    {
        "id": "row_cold",
        "item": {
            "name": "Rift Claw",
            "typeLine": "Slink Gloves",
            "rarity": "Rare",
            "frameType": 2,
            "ilvl": 82,
            "explicitMods": [
                {"description": "+35% to Cold Resistance", "flags": {}},
                {"description": "+65 to maximum Life", "flags": {}},
            ],
        },
        "listing": {"price": {"amount": 8, "currency": "chaos"}},
    },
]

CURRENT_GLOVES_RAW = """Rarity: RARE
Old Hand
Slink Gloves
Item Level: 75
Implicits: 0
+50 to maximum Life
+25% to Cold Resistance
"""


def _mock_trade_stats(*_args: Any, **_kwargs: Any) -> tuple[list[dict[str, Any]], str]:
    flat: list[dict[str, Any]] = []
    for g in SAMPLE_STATS:
        flat.extend(g["entries"])
    return flat, "mock_local"


def test_cli_help_registers_trade_and_bench() -> None:
    runner = CliRunner()
    res = runner.invoke(main, ["--help"])
    assert res.exit_code == 0
    assert "trade" in res.output
    assert "bench" in res.output


def test_trade_stats_bilingual_and_slot_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    runner = CliRunner()

    res_weapon = runner.invoke(main, ["trade", "stats", "攻击速度", "--slot", "weapon1", "--json"])
    assert res_weapon.exit_code == 0, res_weapon.output
    data_w = json.loads(res_weapon.output)
    assert data_w["match"]["id"] == "explicit.stat_210067635"

    res_gloves = runner.invoke(main, ["trade", "stats", "攻击速度", "--slot", "gloves", "--json"])
    assert res_gloves.exit_code == 0, res_gloves.output
    data_g = json.loads(res_gloves.output)
    assert data_g["match"]["id"] == "explicit.stat_681332047"


def test_trade_search_dump_query_is_zero_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("network must not be called during --dump-query")

    monkeypatch.setattr(httpx.Client, "request", _boom)
    monkeypatch.setattr(cli_trade, "resolve_current_trade_league", _boom)

    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--slot",
            "gloves",
            "--stat",
            "冰霜抗性=40",
            "--budget",
            "20",
            "--allow-conversion",
            "--open-suffix",
            "--dump-query",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    q = payload["query"]["query"]
    assert q["status"] == {"option": "available"}
    # Default currency=chaos_equiv omits price.option
    assert q["filters"]["trade_filters"]["filters"]["price"] == {"max": 20.0}
    # --allow-conversion injects corrupted=false and mirrored=false
    assert q["filters"]["misc_filters"]["filters"] == {
        "corrupted": {"option": "false"},
        "mirrored": {"option": "false"},
    }
    assert any(g["type"] == "count" and g["value"] == {"min": 1} for g in q["stats"])
    assert payload["openAffixes"]["suffix"] is True
    assert payload["benchOptions"]


def test_trade_search_rejects_unique_with_allow_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--slot",
            "gloves",
            "--rarity",
            "unique",
            "--stat",
            "to Cold Resistance=30",
            "--allow-conversion",
            "--dump-query",
        ],
    )
    assert res.exit_code == 1


def test_trade_search_full_loop_with_batch_pob_and_ci_inf_json_safety(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q123", "total": 2, "result": ["row_fire", "row_cold"]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return SAMPLE_FETCH_ROWS[:limit]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)

    rpc_calls: list[str] = []

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        rpc_calls.append(action)
        if action == "items":
            return {"raw": CURRENT_GLOVES_RAW}
        if action == "status":
            return {"main": {"buildInfo": {"className": "Witch", "ascendClassName": "Necromancer", "level": 95}}}
        if action == "trade_eval_items":
            items = (params or {}).get("items") or []
            return {
                "slotName": "Gloves",
                "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1_000_000.0, "totalEhp": "inf", "life": 1.0},
                "results": [
                    {
                        "id": entry["id"],
                        "ok": True,
                        "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1_000_000.0, "totalEhp": "inf", "life": 1.0},
                        "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1_120_000.0 - 50_000 * idx, "totalEhp": "inf", "life": 1.0},
                        "diff": {"newWarnings": [], "resolvedWarnings": []},
                    }
                    for idx, entry in enumerate(items)
                ],
            }
        raise AssertionError(f"Unexpected daemon action: {action}")

    monkeypatch.setattr(cli_trade, "_daemon_call_safe", _fake_daemon)

    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--slot",
            "gloves",
            "--stat",
            "冰霜抗性=30",
            "--want",
            "Attack Speed",
            "--allow-conversion",
            "--pob",
            "on",
            "--sort",
            "pob",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "trade_eval_items" in rpc_calls
    data = json.loads(res.output)
    # Re-serialize strictly forbidding NaN/Infinity (CI inf build safety)
    json.dumps(data, allow_nan=False)
    assert data["league"] == "Covens"
    assert len(data["candidates"]) == 2
    top = data["candidates"][0]
    assert top["pob"]["dps_pct"] == 12.0
    assert top["pob"]["ehp_pct"] is None
    assert top["pob"]["ehp_after"] == "inf"
    assert top["conversions"] and top["conversions"][0]["rerolled"] is True


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        (NoResults(), 2),
        (CookieRequired(403, "cf"), 3),
        (RateLimited(30), 4),
    ],
)
def test_trade_search_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected_code: int,
) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")

    class _ErrClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            raise exc

    monkeypatch.setattr(cli_trade, "TradeClient", _ErrClient)
    runner = CliRunner()
    res = runner.invoke(
        main,
        ["trade", "search", "--slot", "gloves", "--stat", "to Cold Resistance=30", "--no-compare", "--pob", "off"],
    )
    assert res.exit_code == expected_code


def test_trade_search_pob_on_exits_6_on_daemon_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q1", "total": 1, "result": ["row_cold"]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return [SAMPLE_FETCH_ROWS[1]]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)
    monkeypatch.setattr(
        cli_trade,
        "_daemon_call_safe",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("daemon offline")),
    )

    runner = CliRunner()
    res = runner.invoke(
        main,
        ["trade", "search", "--slot", "gloves", "--stat", "to Cold Resistance=30", "--no-compare", "--pob", "on"],
    )
    assert res.exit_code == 6


def test_trade_convert_and_bench_commands(tmp_path: Path) -> None:
    runner = CliRunner()
    out_file = tmp_path / "converted.txt"
    raw = "Rarity: RARE\nDoom Hand\nSlink Gloves\nImplicits: 0\n+42% to Fire Resistance\n"
    res = runner.invoke(
        main,
        ["trade", "convert", "--text", raw, "--to", "cold", "--out", str(out_file), "--json"],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["conversions"][0]["target"] == "cold"
    assert data["conversions"][0]["rerolled"] is True
    assert "+42% to Cold Resistance" in out_file.read_text(encoding="utf-8")

    # Bench list & suggest (EN + ZH)
    b_list = runner.invoke(main, ["bench", "list", "--slot", "gloves", "--affix", "suffix", "--limit", "5", "--json"])
    assert b_list.exit_code == 0, b_list.output
    bl_data = json.loads(b_list.output)
    assert bl_data["count"] > 0
    assert all(r["affix"] == "suffix" for r in bl_data["recipes"])

    b_sugg = runner.invoke(main, ["bench", "suggest", "--slot", "gloves", "--stat", "冰霜抗性", "--json"])
    assert b_sugg.exit_code == 0, b_sugg.output
    bs_data = json.loads(b_sugg.output)
    assert bs_data["best"]["affix"] == "suffix"
    assert any("{crafted}" in ln for ln in bs_data["best"]["craftedLinesMin"])
