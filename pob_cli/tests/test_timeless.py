"""Unit tests for `pob_cli.timeless` (no PoB engine needed) plus one check against real engine data when present."""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from pob_cli import timeless

JEWEL_LUA = """return {
[[
Emperor's Wit
Cobalt Jewel
(4-6)% increased Intelligence
]],
-- Jewel: Timeless
[[
Brutal Restraint
Timeless Jewel
League: Legion
Limited to: 1 Historic
Variant: Asenath (Dance with Death)
Variant: Balbala (The Traitor)
Radius: Large
Implicits: 0
{variant:1}Denoted service of (500-8000) dekhara in the akhara of Asenath
{variant:2}Denoted service of (500-8000) dekhara in the akhara of Balbala
Passives in radius are Conquered by the Maraketh
Historic
]],[[
Elegant Hubris
Timeless Jewel
Limited to: 1 Historic
Radius: Large
Implicits: 0
Commissioned (2000-160000) coins to commemorate Cadiro
Passives in radius are Conquered by the Eternal Empire
Historic
]],[[
Militant Faith
Timeless Jewel
Limited to: 1 Historic
Variant: Dominus (Inner Conviction)
Variant: Totem Damage
Radius: Large
Implicits: 0
{variant:1}Carved to glorify (2000-10000) new faithful converted by High Templar Dominus
{variant:2}4% increased Totem Damage per 10 Devotion
Passives in radius are Conquered by the Templars
Historic
]],[[
Festering Vengeance
Murderous Eye Jewel
Limited to: 1 Historic
Implicits: 0
Subjugating (100-8000) souls in the thrall of Tecrod
Passives affected are Conquered by the Abyssal
Historic
]],
}
"""


@pytest.fixture
def game_dir(tmp_path: Path) -> Path:
    (tmp_path / "Data" / "Uniques").mkdir(parents=True)
    (tmp_path / "Data" / "Uniques" / "jewel.lua").write_text(JEWEL_LUA, encoding="utf-8")
    return tmp_path


def test_load_jewels_reads_only_timeless_entries(game_dir: Path) -> None:
    jewels = timeless.load_jewels(game_dir)
    # the entry after a `-- comment` separator must not be glued to the item before it
    assert list(jewels) == ["Brutal Restraint", "Elegant Hubris", "Militant Faith", "Festering Vengeance"]
    assert jewels["Brutal Restraint"]["base"] == "Timeless Jewel"
    assert list(jewels["Brutal Restraint"]["conquerors"]) == ["Asenath", "Balbala"]
    assert jewels["Brutal Restraint"]["seeds"] == range(500, 8001)
    assert jewels["Festering Vengeance"]["base"] == "Murderous Eye Jewel"


def test_elegant_hubris_seeds_step_by_twenty(game_dir: Path) -> None:
    seeds = timeless.load_jewels(game_dir)["Elegant Hubris"]["seeds"]
    assert (seeds[0], seeds[1], seeds[-1], len(seeds)) == (2000, 2020, 160000, 7901)


def test_jewel_text_fills_conqueror_and_keeps_optional_mods_out(game_dir: Path) -> None:
    jewels = timeless.load_jewels(game_dir)
    text = timeless.jewel_text(jewels["Brutal Restraint"], "Brutal Restraint", "Balbala").format(seed=777)
    assert text.splitlines() == [
        "Rarity: UNIQUE", "Brutal Restraint", "Timeless Jewel", "Item Level: 84", "Limited to: 1 Historic",
        "Radius: Large", "Implicits: 0", "Denoted service of 777 dekhara in the akhara of Balbala",
        "Passives in radius are Conquered by the Maraketh", "Historic",
    ]
    faith = timeless.jewel_text(jewels["Militant Faith"], "Militant Faith")
    assert "Totem Damage" not in faith  # devotion mods only when asked for
    asked = timeless.jewel_text(jewels["Militant Faith"], "Militant Faith", mods=["4% increased Totem Damage per 10 Devotion"])
    assert asked.splitlines()[-3] == "4% increased Totem Damage per 10 Devotion"


def test_group_seeds_only_splits_on_the_given_nodes() -> None:
    seeds = [10, 11, 12, 13]
    index = {100: 0, 200: 1}
    #               node 100    node 200  (one byte per seed)
    table = bytes([1, 1, 2, 2,  5, 6, 5, 6])
    assert timeless.group_seeds(seeds, table, index, [100]) == {b"\x01": [10, 11], b"\x02": [12, 13]}
    assert list(timeless.group_seeds(seeds, table, index, [100, 200]).values()) == [[10], [11], [12], [13]]
    assert timeless.group_seeds(seeds, table, index, []) == {b"": [10, 11, 12, 13]}  # nothing seed-dependent is used


def test_fit_pieces_recovers_additive_effects_for_unseen_combinations() -> None:
    effect = [{1: 0.0, 2: 5.0, 3: -2.0}, {1: 0.0, 2: 1.0, 3: 7.0}, {1: 0.0, 2: -4.0, 3: 3.0}]

    def true_value(sig: bytes) -> float:
        return 100.0 + sum(effect[p][sig[p]] for p in range(3))

    every = [bytes([a, b, c]) for a in (1, 2, 3) for b in (1, 2, 3) for c in (1, 2, 3)]
    seen, unseen = every[::2], every[1::2]
    predict = timeless.fit_pieces([(sig, true_value(sig)) for sig in seen])
    assert max(abs(predict(sig) - true_value(sig)) for sig in unseen) < 1e-6
    assert max(unseen, key=predict) == max(unseen, key=true_value)


def test_narrow_down_finds_a_rare_valuable_byte_without_calculating_everything() -> None:
    import random

    # 3 nodes x 12 table bytes; byte 99 on node 0 is rare (5 groups of 1733) and worth far more than the rest
    sigs = [bytes([a, b, c]) for a in range(12) for b in range(12) for c in range(12)]
    sigs += [bytes([99, b, 0]) for b in range(5)]
    truth = {sig: 1000.0 * (1 + sig[0] / 50) * (1 + sig[1] / 80) * (1 + sig[2] / 200) * (3 if sig[0] == 99 else 1)
             for sig in sigs}
    for rng_seed in range(20):  # a purely random first sample misses byte 99 in most of these
        results: dict[bytes, dict[str, float]] = {}
        estimate = timeless.narrow_down(
            sigs, results, lambda todo: results.update({sig: {"X": truth[sig]} for sig in todo}),
            "X", 5, random.Random(rng_seed), lambda _msg: None,
        )
        assert estimate is not None and len(results) < len(sigs) // 2
        assert sorted(truth, key=truth.get, reverse=True)[:5] == sorted(results, key=lambda s: truth[s], reverse=True)[:5]


def test_engine_keeps_braces_in_item_lines_and_takes_other_jewels_out_after_each_reload() -> None:
    calls: list[tuple[str, dict]] = []

    class FakeWorker:
        def call(self, method: str, params: dict, timeout: float = 0) -> dict:
            calls.append((method, params))
            return {}

    engine = timeless.Engine.__new__(timeless.Engine)  # no PoB process
    engine.code, engine.template, engine.slot, engine.clear, engine.count = "CODE", "{crafted}x\nseed {seed}", "Jewel 1", ["Jewel 2"], 0
    engine.worker = FakeWorker()  # type: ignore[assignment]
    engine.stats(777)
    assert [method for method, _ in calls] == ["import_code", "unequip_slot", "add_item", "get_stats"]
    assert calls[1][1] == {"slotName": "Jewel 2"}
    assert calls[2][1]["raw"] == "{crafted}x\nseed 777"


class FakeEngine:
    """Stands in for a PoB engine: sockets 7 (holding a Brutal Restraint) and 9 are allocated, socket 11 is not,
    922 is an allocated notable."""

    game_dir: Path
    made: list[FakeEngine] = []

    def __init__(self, code: str, template: str, slot: str, clear: object = ()) -> None:
        self.template, self.slot, self.clear, self.worker = template, slot, clear, self
        FakeEngine.made.append(self)

    def call(self, method: str, params: dict, timeout: float = 0) -> dict:
        assert method == "get_tree_state"
        return {"allocatedNodes": [7, 9, 922], "overrides": {},
                "sockets": [{"nodeId": 7, "title": "Brutal Restraint"}, {"nodeId": 9}, {"nodeId": 11}]}

    def reset(self) -> None:
        pass

    stop = reset

    def read(self) -> dict:
        return {}

    def stats(self, seed: int) -> dict:
        raise RuntimeError("reached PoB")


def test_search_rejects_non_sockets_and_takes_out_a_second_historic_jewel(game_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeEngine.game_dir, FakeEngine.made = game_dir, []
    monkeypatch.setattr(timeless, "Engine", FakeEngine)
    for node in (922, 11, 5):  # an allocated notable, an unallocated socket, no such node
        with pytest.raises(ValueError, match="not an allocated jewel socket"):
            timeless.search("code", "", node, "Militant Faith")
    with pytest.raises(RuntimeError, match="reached PoB"):
        timeless.search("code", "", 9, "Militant Faith")
    assert FakeEngine.made[-1].clear == ["Jewel 7"]  # the game allows one Historic jewel
    with pytest.raises(RuntimeError, match="reached PoB"):
        timeless.search("code", "", 7, "Militant Faith")  # replaced in its own socket: nothing else to take out
    assert FakeEngine.made[-1].clear == []


def test_load_table_reads_single_file_and_numbered_parts(tmp_path: Path) -> None:
    folder = tmp_path / "Data" / "TimelessJewelData"
    folder.mkdir(parents=True)
    payload = bytes(range(256)) * 50
    packed = zlib.compress(payload)
    (folder / "One.zip").write_bytes(packed)
    cut = len(packed) // 3
    for i, chunk in enumerate([packed[:cut], packed[cut:2 * cut], packed[2 * cut:]]):
        (folder / f"Split.zip.part{i}").write_bytes(chunk)
    assert timeless.load_table(tmp_path, "One") == payload
    assert timeless.load_table(tmp_path, "Split") == payload


def test_real_engine_tables_match_their_seed_ranges() -> None:
    """With a local engine: every flat table must be exactly notables x seeds bytes, or grouping would misread it."""
    from pob_cli.bridge_client import BridgeWorker

    game_dir = BridgeWorker("test").game_dir
    if not (game_dir / "Data" / "TimelessJewelData" / "NodeIndexMapping.lua").is_file():
        pytest.skip("no local PoB engine data")
    jewels = timeless.load_jewels(game_dir)
    index = timeless.load_node_index(game_dir)
    assert set(timeless.FLAT_TABLES) <= set(jewels)
    for name, stem in timeless.FLAT_TABLES.items():
        assert len(timeless.load_table(game_dir, stem)) == len(index) * len(jewels[name]["seeds"]), name
