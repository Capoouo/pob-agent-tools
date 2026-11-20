"""Unit tests for pob_cli.trade_eval (scoring, inf/NaN-safe PoB eval, batch RPC, reporting)."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from pob_cli.trade_eval import (
    DEFAULT_WEIGHTS,
    POB_EHP_WEIGHT,
    PREFERRED_BONUS,
    Candidate,
    PobEval,
    _pob_eval_from_snapshot,
    _safe_pct,
    compare_items,
    format_report,
    parse_weights,
    preferred_templates,
    report_json,
    score_deltas,
    sort_candidates,
    verify_candidate,
    verify_candidates_batch,
)
from pob_cli.trade_item import Conversion, parse_item_text


def _make_item(text: str):
    return parse_item_text(text)


def _make_candidate(
    text: str,
    *,
    price_amount: float | None = 50.0,
    price_currency: str | None = "chaos",
    score: float = 0.0,
    url: str = "https://www.pathofexile.com/trade/search/Standard/abc",
    listing_id: str | None = "listing-1",
    conversions: list[Conversion] | None = None,
    conversion_warnings: list[str] | None = None,
    pob: PobEval | None = None,
    pob_error: str | None = None,
    current_text: str | None = None,
) -> Candidate:
    item = _make_item(text)
    current = _make_item(current_text) if current_text else None
    deltas = compare_items(current, item) if current_text is not None else []
    computed_score = score if current_text is None else score_deltas(deltas)
    return Candidate(
        raw={"id": listing_id or "x"},
        item=item,
        item_text=text,
        price_amount=price_amount,
        price_currency=price_currency,
        score=computed_score,
        deltas=deltas,
        conversions=list(conversions or []),
        conversion_warnings=list(conversion_warnings or []),
        url=url,
        listing_id=listing_id,
        pob=pob,
        pob_error=pob_error,
    )


# ---------------------------------------------------------------------------
# 1. Weights & Mod Comparison (PoE1 clean + substring preferred_templates)
# ---------------------------------------------------------------------------


def test_default_weights_poe1_clean_no_spirit() -> None:
    assert "spirit" not in DEFAULT_WEIGHTS
    assert DEFAULT_WEIGHTS["life"] == 1.0
    assert DEFAULT_WEIGHTS["skill_level"] == 1.2
    assert DEFAULT_WEIGHTS["penalty"] == -0.5

    custom = parse_weights(["life=2.5", "SPEED=1.25"])
    assert custom["life"] == 2.5
    assert custom["speed"] == 1.25
    assert custom["resist"] == DEFAULT_WEIGHTS["resist"]

    with pytest.raises(ValueError, match="Unknown weight category"):
        parse_weights(["spirit=1.0"])
    with pytest.raises(ValueError, match="--weight needs"):
        parse_weights(["life"])
    with pytest.raises(ValueError, match="must be finite"):
        parse_weights(["life=inf"])
    with pytest.raises(ValueError, match="must be finite"):
        parse_weights(["life=nan"])


def test_compare_items_want_attack_speed_substring_boost() -> None:
    """Regression test for PR #1 bug: --want 'Attack Speed' must boost '#% increased attack speed'."""
    current = _make_item(
        """
        Rarity: RARE
        Old Grip
        Titan Gauntlets
        Implicits: 0
        +50 to maximum Life
        10% increased Attack Speed
        """
    )
    candidate = _make_item(
        """
        Rarity: RARE
        New Grip
        Titan Gauntlets
        Implicits: 0
        +50 to maximum Life
        16% increased Attack Speed
        """
    )

    base_deltas = compare_items(current, candidate)
    assert len(base_deltas) == 1
    assert base_deltas[0].template == "#% increased attack speed"
    assert base_deltas[0].delta == pytest.approx(6.0)
    assert base_deltas[0].is_gain is True
    base_contrib = base_deltas[0].contribution
    assert base_contrib == pytest.approx(6.0 * DEFAULT_WEIGHTS["speed"])

    prefs = preferred_templates(["Attack Speed"])
    assert prefs == {"attack speed"}

    boosted_deltas = compare_items(current, candidate, preferred=prefs)
    assert len(boosted_deltas) == 1
    assert boosted_deltas[0].contribution == pytest.approx(base_contrib * PREFERRED_BONUS)
    assert score_deltas(boosted_deltas) == pytest.approx(round(base_contrib * PREFERRED_BONUS, 2))

    # Also works if caller passes raw mixed-case keyword directly or full mod text with numbers
    direct_boost = compare_items(current, candidate, preferred={"Attack Speed"})
    assert direct_boost[0].contribution == pytest.approx(base_contrib * PREFERRED_BONUS)

    exact_pref = preferred_templates(["+16% increased Attack Speed"])
    assert exact_pref == {"#% increased attack speed"}
    exact_boost = compare_items(current, candidate, preferred=exact_pref)
    assert exact_boost[0].contribution == pytest.approx(base_contrib * PREFERRED_BONUS)


# ---------------------------------------------------------------------------
# 2. inf / NaN-Safe PobEval (CI builds)
# ---------------------------------------------------------------------------


def test_safe_pct_edge_cases() -> None:
    assert _safe_pct(1000.0, 1150.0) == 15.0
    assert _safe_pct(1000.0, 900.0) == -10.0
    assert _safe_pct("inf", "inf") is None
    assert _safe_pct(math.inf, math.inf) is None
    assert _safe_pct("inf", 50000.0) is None
    assert _safe_pct(50000.0, "inf") is None
    assert _safe_pct("-inf", "inf") is None
    assert _safe_pct("nan", 100.0) is None
    assert _safe_pct(math.nan, 100.0) is None
    assert _safe_pct(0.0, 0.0) == 0.0
    assert _safe_pct(0.0, 250.0) is None
    assert _safe_pct(None, 100.0) is None


def test_pob_eval_ci_build_inf_ehp_and_strict_json() -> None:
    """CI builds report TotalEHP='inf'; PobEval must never emit NaN/Infinity in Python or JSON."""
    pob = _pob_eval_from_snapshot(
        before={"primaryDpsKey": "CombinedDPS", "primaryDps": 1_000_000.0, "totalEhp": "inf", "life": 1.0},
        after={"primaryDpsKey": "CombinedDPS", "primaryDps": 1_125_000.0, "totalEhp": "inf", "life": 1.0},
        diff={"newWarnings": ["Missing 12 Int"], "resolvedWarnings": ["Uncapped Cold Res"]},
    )

    assert pob.dps_pct == pytest.approx(12.5)
    assert pob.ehp_pct is None
    assert pob.ehp_pct_str == "0% (inf)"
    assert pob.dps_pct_str == "+12.5%"
    assert pob.life_delta == 0.0
    assert math.isfinite(pob.combined_pct)
    assert pob.combined_pct == pytest.approx(12.5)
    assert pob.warnings == ("Missing 12 Int",)
    assert pob.resolved_warnings == ("Uncapped Cold Res",)

    # Direct construction with raw float('inf') and float('nan') must also serialize strictly
    raw_nonfinite = PobEval(
        dps_key="FullDPS",
        dps_before=math.inf,
        dps_after=50000.0,
        ehp_before=math.inf,
        ehp_after=math.inf,
        life_before=math.nan,
        life_after=1.0,
    )
    assert raw_nonfinite.dps_pct is None
    assert raw_nonfinite.ehp_pct is None
    assert raw_nonfinite.ehp_pct_str == "0% (inf)"
    assert raw_nonfinite.dps_pct_str == "inf -> 50,000"
    assert raw_nonfinite.life_delta == 0.0
    assert math.isfinite(raw_nonfinite.combined_pct)

    serialized_eval = json.dumps(pob.as_dict(), allow_nan=False)
    parsed_eval = json.loads(serialized_eval)
    assert parsed_eval["ehp_before"] == "inf"
    assert parsed_eval["ehp_after"] == "inf"
    assert parsed_eval["ehp_pct"] is None
    assert parsed_eval["dps_pct"] == 12.5

    serialized_raw = json.dumps(raw_nonfinite.as_dict(), allow_nan=False)
    parsed_raw = json.loads(serialized_raw)
    assert parsed_raw["dps_before"] == "inf"
    assert parsed_raw["ehp_before"] == "inf"
    assert parsed_raw["life_before"] == "nan"

    cand = _make_candidate(
        "Rarity: RARE\nCI Crown\nHubris Circlet\nImplicits: 0\n+250 to maximum Energy Shield",
        pob=pob,
    )
    payload = report_json([cand])
    dumped = json.dumps(payload, allow_nan=False)
    loaded = json.loads(dumped)
    assert loaded[0]["pob"]["ehp_before"] == "inf"
    assert loaded[0]["pob"]["ehp_pct"] is None


def test_pob_eval_finite_to_inf_ehp_transition() -> None:
    pob = PobEval(
        dps_key="CombinedDPS",
        dps_before=500_000.0,
        dps_after=550_000.0,
        ehp_before=50_000.0,
        ehp_after="inf",
        life_before=4000.0,
        life_after=4120.0,
    )
    assert pob.dps_pct == pytest.approx(10.0)
    assert pob.ehp_pct is None
    assert pob.ehp_pct_str == "50,000 -> inf"
    assert pob.life_delta == pytest.approx(120.0)
    assert pob.combined_pct == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 3. Batch & Single PoB Verification (trade_eval_items + fallback to item_try)
# ---------------------------------------------------------------------------


def test_verify_candidates_batch_single_rpc() -> None:
    c1 = _make_candidate("Rarity: RARE\nItem A\nRuby Ring\nImplicits: 0\n+60 to maximum Life", listing_id="a")
    c2 = _make_candidate("Rarity: RARE\nItem B\nRuby Ring\nImplicits: 0\n+70 to maximum Life", listing_id="b")
    c3 = _make_candidate("Rarity: RARE\nItem C\nRuby Ring\nImplicits: 0\n+80 to maximum Life", listing_id="c")

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_daemon(action: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((action, params))
        assert action == "trade_eval_items"
        return {
            "slotName": "Ring 1",
            "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1000.0, "totalEhp": "inf", "life": 4000.0},
            "results": [
                {
                    "id": 0,
                    "ok": True,
                    "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1000.0, "totalEhp": "inf", "life": 4000.0},
                    "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1100.0, "totalEhp": "inf", "life": 4120.0},
                    "diff": {"newWarnings": ["Low Dex"], "resolvedWarnings": []},
                },
                {
                    "id": 1,
                    "ok": False,
                    "error": "Could not parse item text",
                },
            ],
        }

    verify_candidates_batch([c1, c2, c3], slot="Ring 1", target="main", limit=2, daemon_call=fake_daemon)

    assert len(calls) == 1
    assert calls[0][0] == "trade_eval_items"
    assert len(calls[0][1]["items"]) == 2
    assert c1.pob is not None
    assert c1.pob.dps_pct == pytest.approx(10.0)
    assert c1.pob.ehp_pct is None
    assert c1.pob.warnings == ("Low Dex",)
    assert c1.pob_error is None

    assert c2.pob is None
    assert c2.pob_error == "Could not parse item text"

    # Candidate 3 was outside limit=2 and must remain untouched
    assert c3.pob is None
    assert c3.pob_error is None


def test_verify_candidates_batch_fallback_to_item_try() -> None:
    c1 = _make_candidate("Rarity: RARE\nItem A\nRuby Ring\nImplicits: 0\n+60 to maximum Life", listing_id="a")
    c2 = _make_candidate("Rarity: RARE\nItem B\nRuby Ring\nImplicits: 0\n+70 to maximum Life", listing_id="b")

    calls: list[str] = []

    def legacy_daemon(action: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append(action)
        if action == "trade_eval_items":
            raise RuntimeError("Unknown daemon action 'trade_eval_items'.")
        assert action == "item_try"
        if "Item B" in params["raw"]:
            raise RuntimeError("Bridge parse error")
        return {
            "mutation": {
                "before": {"primaryDpsKey": "FullDPS", "primaryDps": 2000.0, "totalEhp": 50000.0, "life": 3500.0},
                "after": {"primaryDpsKey": "FullDPS", "primaryDps": 2200.0, "totalEhp": 55000.0, "life": 3600.0},
                "diff": {"newWarnings": [], "resolvedWarnings": ["Low Fire Res"]},
            }
        }

    verify_candidates_batch([c1, c2], slot="Ring 1", target="main", limit=5, daemon_call=legacy_daemon)

    assert calls == ["trade_eval_items", "item_try", "item_try"]
    assert c1.pob is not None
    assert c1.pob.dps_key == "FullDPS"
    assert c1.pob.dps_pct == pytest.approx(10.0)
    assert c1.pob.ehp_pct == pytest.approx(10.0)
    assert c1.pob.combined_pct == pytest.approx(10.0 + POB_EHP_WEIGHT * 10.0)
    assert c1.pob.resolved_warnings == ("Low Fire Res",)
    assert c1.pob_error is None

    assert c2.pob is None
    assert c2.pob_error == "Bridge parse error"


def test_verify_candidates_batch_error_and_limit_zero() -> None:
    c1 = _make_candidate("Rarity: RARE\nItem A\nRuby Ring\nImplicits: 0\n+60 to maximum Life")
    c2 = _make_candidate("Rarity: RARE\nItem B\nRuby Ring\nImplicits: 0\n+70 to maximum Life")

    called = False

    def fail_daemon(action: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        raise RuntimeError("Invalid slot 'Bogus'")

    verify_candidates_batch([c1, c2], slot="Bogus", limit=0, daemon_call=fail_daemon)
    assert called is False
    assert c1.pob is None and c1.pob_error is None

    verify_candidates_batch([c1, c2], slot="Bogus", limit=1, daemon_call=fail_daemon)
    assert called is True
    assert c1.pob is None
    assert c1.pob_error == "Invalid slot 'Bogus'"
    assert c2.pob is None and c2.pob_error is None

    # Partial batch response (missing id=1)
    c3 = _make_candidate("Rarity: RARE\nItem C\nRuby Ring\nImplicits: 0\n+60 to maximum Life")
    c4 = _make_candidate("Rarity: RARE\nItem D\nRuby Ring\nImplicits: 0\n+70 to maximum Life")

    def partial_daemon(action: str, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 100.0, "totalEhp": 1000.0, "life": 1000.0},
            "results": [
                {
                    "id": 0,
                    "ok": True,
                    "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 110.0, "totalEhp": 1000.0, "life": 1000.0},
                    "diff": {},
                }
            ],
        }

    verify_candidates_batch([c3, c4], slot="Ring 1", limit=2, daemon_call=partial_daemon)
    assert c3.pob is not None and c3.pob.dps_pct == pytest.approx(10.0)
    assert c4.pob is None and c4.pob_error == "Missing PoB evaluation result."


def test_verify_candidate_single() -> None:
    cand = _make_candidate("Rarity: RARE\nSolo Ring\nRuby Ring\nImplicits: 0\n+60 to maximum Life")

    def fake_daemon(action: str, params: dict[str, Any]) -> dict[str, Any]:
        assert action == "item_try"
        assert params["apply"] is False
        assert params["slot"] == "Ring 1"
        return {
            "mutation": {
                "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 500.0, "totalEhp": 20000.0, "life": 3000.0},
                "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 525.0, "totalEhp": 21000.0, "life": 3100.0},
                "diff": {"newWarnings": []},
            }
        }

    pob = verify_candidate(cand, slot="Ring 1", daemon_call=fake_daemon)
    assert pob.dps_pct == pytest.approx(5.0)
    assert pob.ehp_pct == pytest.approx(5.0)
    assert pob.life_delta == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 4. Sorting (score, price, pob — NaN-free)
# ---------------------------------------------------------------------------


def test_sort_candidates_modes() -> None:
    pob_ci = PobEval("CombinedDPS", 1000.0, 1150.0, "inf", "inf", 1.0, 1.0)  # combined 15.0
    pob_low = PobEval("CombinedDPS", 1000.0, 1020.0, 40000.0, 44000.0, 4000.0, 4050.0)  # 2 + 0.25*10 = 4.5

    c_expensive = _make_candidate(
        "Rarity: RARE\nExpensive\nRuby Ring\nImplicits: 0\n+70 to maximum Life",
        price_amount=200.0,
        score=50.0,
        pob=pob_low,
    )
    c_mid = _make_candidate(
        "Rarity: RARE\nCI Upgrade\nRuby Ring\nImplicits: 0\n+60 to maximum Life",
        price_amount=80.0,
        score=30.0,
        pob=pob_ci,
    )
    c_unpriced = _make_candidate(
        "Rarity: RARE\nUnpriced\nRuby Ring\nImplicits: 0\n+90 to maximum Life",
        price_amount=None,
        price_currency=None,
        score=90.0,
        pob=None,
    )

    by_score = sort_candidates([c_mid, c_expensive, c_unpriced], sort="score")
    assert [c.display_name for c in by_score] == [
        "Unpriced (Ruby Ring)",
        "Expensive (Ruby Ring)",
        "CI Upgrade (Ruby Ring)",
    ]

    by_price = sort_candidates([c_unpriced, c_expensive, c_mid], sort="price")
    assert [c.display_name for c in by_price] == [
        "CI Upgrade (Ruby Ring)",
        "Expensive (Ruby Ring)",
        "Unpriced (Ruby Ring)",
    ]

    by_pob = sort_candidates([c_unpriced, c_expensive, c_mid], sort="pob")
    assert [c.display_name for c in by_pob] == [
        "CI Upgrade (Ruby Ring)",
        "Expensive (Ruby Ring)",
        "Unpriced (Ruby Ring)",
    ]


# ---------------------------------------------------------------------------
# 5. Markdown Report (with/without PoB, conversions, bench options, pipe escaping)
# ---------------------------------------------------------------------------


def test_format_report_without_pob_and_empty() -> None:
    empty_md = format_report(
        build_label="Demo Build",
        slot="Ring 1",
        current=None,
        candidates=[],
        league="Standard",
        status="securable",
        budget=None,
        currency="chaos",
        search_url="",
        total=0,
        fetched=0,
    )
    assert "No analysable candidates." in empty_md
    assert "- Status: securable" in empty_md
    assert "- Budget: unlimited" in empty_md

    cand = _make_candidate(
        "Rarity: RARE\nPlain | Ring\nRuby Ring\nImplicits: 0\n+75 to maximum Life",
        current_text="Rarity: RARE\nOld Ring\nRuby Ring\nImplicits: 0\n+40 to maximum Life",
    )
    md = format_report(
        build_label="Life Slayer",
        slot="Ring 1",
        current=_make_item("Rarity: RARE\nOld Ring\nRuby Ring\nImplicits: 0\n+40 to maximum Life"),
        candidates=[cand],
        league="Mirage",
        budget=100.0,
        currency="chaos",
        search_url="https://www.pathofexile.com/trade/search/Mirage/xyz",
        total=12,
        fetched=1,
    )
    table_lines = [ln for ln in md.splitlines() if ln.startswith("|")]
    assert table_lines
    assert "PoB DPS" not in table_lines[0]
    assert "PoB EHP" not in table_lines[0]
    assert "Plain \\| Ring" in md
    assert "- Budget: 100 chaos" in md
    assert "Mod score is a weighted template delta" in md


def test_format_report_with_pob_conversions_bench_and_escaping() -> None:
    current = _make_item(
        """
        Rarity: RARE
        Old Band
        Amethyst Ring
        Implicits: 0
        +50 to maximum Life
        +35% to Fire Resistance
        """
    )
    conv = Conversion(
        template="#% to cold resistance",
        converted_template="#% to fire resistance",
        element="cold",
        target="fire",
        group="resistance",
        subkind="resistance",
        values=(46.0,),
        cost="300 Lifeforce (Primal/Vivid/Wild matching target)",
        patch="3.30",
        line="+46% to Fire Resistance",
        tier="S1",
        tier_range="46-48",
    )
    pob_ci = PobEval(
        dps_key="CombinedDPS",
        dps_before=1_000_000.0,
        dps_after=1_085_000.0,
        ehp_before="inf",
        ehp_after="inf",
        life_before=1.0,
        life_after=1.0,
        warnings=("Attribute requirement | Str short",),
        resolved_warnings=("Fire Resistance uncapped",),
    )

    c1 = _make_candidate(
        """
        Rarity: RARE
        Storm | Band
        Amethyst Ring
        Implicits: 0
        +75 to maximum Life
        +46% to Fire Resistance
        """,
        current_text="""
        Rarity: RARE
        Old Band
        Amethyst Ring
        Implicits: 0
        +50 to maximum Life
        +35% to Fire Resistance
        """,
        price_amount=1.5,
        price_currency="divine",
        conversions=[conv],
        conversion_warnings=["Item has a crafted target modifier; remove it at the crafting bench before Harvest conversion."],
        pob=pob_ci,
    )
    c2 = _make_candidate(
        """
        Rarity: RARE
        Unverified Band
        Amethyst Ring
        Implicits: 0
        +65 to maximum Life
        """,
        current_text="""
        Rarity: RARE
        Old Band
        Amethyst Ring
        Implicits: 0
        +50 to maximum Life
        +35% to Fire Resistance
        """,
        price_amount=40.0,
        price_currency="chaos",
        pob=None,
        pob_error="Timed out evaluating item",
    )

    bench_options = [
        {
            "affix": "prefix",
            "text": "+(55-70) to maximum Life",
            "textZh": "+(55-70) 最大生命",
            "values": [{"min": 55, "max": 70}],
            "craftedLinesMin": ["{crafted}+55 to maximum Life"],
            "cost": "4x Chaos Orb",
        },
        {
            "affix": "suffix",
            "text": "+(20-25)% to Fire Resistance",
            "values": [{"min": 20, "max": 25}],
            "cost": "2x Orb of Alchemy",
        },
    ]

    md = format_report(
        build_label="CI Trickster",
        slot="Ring 1",
        current=current,
        candidates=[c1, c2],
        league="Mirage",
        status="securable",
        budget=5.0,
        currency="divine",
        search_url="https://www.pathofexile.com/trade/search/Mirage/q123",
        total=42,
        fetched=2,
        conversion_note="Harvest resistance swap enabled (cold/lightning -> fire)",
        open_affixes={"prefix": True, "suffix": False},
        bench_options=bench_options,
    )

    assert "PoB DPS" in md and "PoB EHP" in md
    assert "0% (inf)" in md
    assert "+8.5%" in md
    assert "Storm \\| Band" in md
    assert "Attribute requirement \\| Str short" in md
    assert "PoB resolved warnings: Fire Resistance uncapped" in md
    assert "PoB verification failed: Timed out evaluating item" in md
    assert "tier S1; range 46-48; 3.30 rules; same tier, roll re-rolled inside the tier" in md
    assert "remove it at the crafting bench before Harvest conversion." in md
    assert "{crafted}+55 to maximum Life" in md
    assert "{crafted}+20% to Fire Resistance" in md
    assert "Open affixes: prefix" in md
    assert "- Status: securable" in md

    # Verify JSON payload for the full mixed report is strictly RFC-8259 valid
    raw_json = json.dumps(report_json([c1, c2]), allow_nan=False)
    decoded = json.loads(raw_json)
    assert len(decoded) == 2
    assert decoded[0]["pob"]["ehp_pct"] is None
    assert decoded[0]["conversions"][0]["tier"] == "S1"
    assert decoded[0]["conversions"][0]["tier_range"] == "46-48"
    assert decoded[1]["pob"] is None
    assert decoded[1]["pob_error"] == "Timed out evaluating item"
