"""Trade bench variants retain the listing and require certain legal occupancy."""
from types import SimpleNamespace

import pytest

from pob_cli import trade_craft
from pob_cli.trade_eval import Candidate, report_json
from pob_cli.trade_item import parse_item_text

RAW = 'Rarity: RARE\nTest Hand\nConjurer Gloves\nItem Level: 85\nImplicits: 0\n+115 to maximum Life\n'


@pytest.mark.parametrize('action', ['open', 'replace_craft'])
def test_expand_keeps_original_and_replaces_only_bench_lines(monkeypatch, action):
    raw = RAW + ('{crafted}42% increased Damage while Leeching\n' if action == 'replace_craft' else '')
    candidate = Candidate(raw={}, item=parse_item_text(raw), item_text=raw, listing_id='listing', price_amount=8)
    report = dict(certain=True, craftEligible=True, item={'itemLevel':85}, scaledAffixTypes=[],
                  affixes=([{'source':'Bench','group':'DamageWhileLeeching','lines':['42% increased Damage while Leeching']}] if action == 'replace_craft' else []),
                  suggestions=[{'status':action,'affix':'prefix','want':'Area Damage','id':'area'}])
    monkeypatch.setattr(trade_craft.item_inspect, 'inspect_item', lambda *a, **k: report)
    recipe = SimpleNamespace(id='area', cost='4x Exalted Orb',
        crafted_lines=lambda *, use_min: [f"{{crafted}}{14 if use_min else 16}% increased Area Damage"])
    monkeypatch.setattr(trade_craft.bench, 'search', lambda *a, **k: [SimpleNamespace(recipe=recipe)])
    result = trade_craft.expand_bench_candidates([candidate], slot='Gloves', wants=('Area Damage',))
    assert result[0] is candidate
    assert len(result) == 2
    variant = result[1]
    assert variant.listing_id == 'listing' and variant.price_amount == 8
    assert '+115 to maximum Life' in variant.item_text
    assert 'Damage while Leeching' not in variant.item_text
    assert '{crafted}16% increased Area Damage' in variant.item_text
    assert variant.bench_craft['roll'] == 'maximum'
    assert variant.bench_craft['listed_item_text'] == raw
    assert report_json([variant])[0]['bench_craft']['action'] == action
    report['certain'] = False
    assert trade_craft.expand_bench_candidates([candidate], slot='Gloves', wants=('Area Damage',)) == [candidate]
