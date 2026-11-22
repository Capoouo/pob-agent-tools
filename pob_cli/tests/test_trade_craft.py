"""Trade bench variants retain the listing and require certain legal occupancy."""
from types import SimpleNamespace

import pytest

from pob_cli import trade_craft
from pob_cli.trade_eval import Candidate, report_json, format_report
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
    text = format_report(build_label='test', slot='Gloves', current=None,
                         candidates=[variant], league='Standard')
    if action == 'replace_craft':
        assert 'Removed craft: 42% increased Damage while Leeching' in text
    else:
        assert 'Removed craft:' not in text
    report['certain'] = False
    assert trade_craft.expand_bench_candidates([candidate], slot='Gloves', wants=('Area Damage',)) == [candidate]


@pytest.mark.parametrize('slot,item_type,maximum', [
    ('weapon1', 'Wand', 66), ('wand', 'Wand', 66), ('Weapon 2', 'Wand', 66),
    ('weapon1', 'Staff', 115), ('staff', 'Staff', 115),
])
def test_weapon_crafts_follow_actual_base_class(monkeypatch, tmp_path, slot, item_type, maximum):
    import json
    import sqlite3
    from pob_cli.bench import BenchRecipe, classes_to_slots

    db = tmp_path / 'game.sqlite'
    recipes = []
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE bases (name_lower TEXT, zh_lower TEXT, item_type TEXT, data_json TEXT)')
        conn.execute('INSERT INTO bases VALUES (?, ?, ?, ?)',
                     ('test base', '', item_type, json.dumps({'tags': [item_type.lower()]})))
        conn.execute('CREATE TABLE mods (source TEXT, weights_json TEXT, zero_weights_json TEXT, types_json TEXT, data_json TEXT)')
        natural = {'id':'cast', 'source':'Explicit', 'generation_type':'Suffix',
                   'group':'IncreasedCastSpeed', 'level':1, 'stats':['12% increased Cast Speed']}
        conn.execute('INSERT INTO mods VALUES (?, ?, ?, ?, ?)',
                     ('Explicit', json.dumps({'wand':1, 'staff':1}), '[]', '[]', json.dumps(natural)))
        for cls, top in [('Wand', 66), ('Staff', 115)]:
            text = f'(1-{top})% increased Spell Damage'
            recipe = BenchRecipe(id=cls, lines=(text,), lines_zh=(), text=text, text_zh='',
                template='#% increased spell damage', affix='prefix', group='SpellDamage', level=1,
                slots=classes_to_slots([cls]), classes=(cls,), values=((1, top),), cost='test', unlock='')
            recipes.append(recipe)
            entry = {'id':cls, 'source':'Bench', 'generation_type':'Bench Prefix',
                     'group':'SpellDamage', 'level':1, 'stats':[text]}
            conn.execute('INSERT INTO mods VALUES (?, ?, ?, ?, ?)',
                         ('Bench', '{}', '[]', json.dumps([cls]), json.dumps(entry)))
    monkeypatch.setattr(trade_craft.item_inspect.game_data, 'ensure_gamedata_index', lambda **k: db)
    monkeypatch.setattr(trade_craft.bench, 'load_recipes', lambda **k: (tuple(recipes), {}))
    raw = 'Rarity: RARE\nTest Weapon\nTest Base\nItem Level: 85\nImplicits: 0\n12% increased Cast Speed'
    candidate = Candidate(raw={}, item=parse_item_text(raw), item_text=raw)
    report = trade_craft.item_inspect.inspect_item(raw, slot=slot, wants=('increased Spell Damage',))
    assert report['benchSlot'] == item_type.lower()
    assert report['suggestions'][0]['id'] == item_type
    variants = trade_craft.expand_bench_candidates([candidate], slot=slot, wants=('increased Spell Damage',))
    assert len(variants) == 2
    assert f'{{crafted}}{maximum}% increased Spell Damage' in variants[1].item_text
