"""Timeless jewel seed search (`pob timeless`).

Every reported number comes from headless PoB (R5): the seed is socketed and the build recalculated.
Three things keep that fast:

1. For the jewels whose lookup table is one byte per (notable, seed), seeds with identical bytes on the
   notables this build uses must produce the same build, so one seed per such group is calculated.
2. When there are still many groups, a few hundred are calculated, the value of each single node change is
   fitted from them, and that estimate only picks which further seeds to calculate. It stops once no
   uncalculated seed is estimated - plus the largest estimate error seen - to reach the requested top list.
3. Calculations are spread over several throwaway PoB engines running at once.

Jewel names, base types, seed ranges and conquerors are read from PoB's own `Data/Uniques/jewel.lua`,
so new timeless jewels work without a code change.
"""

from __future__ import annotations

import math
import os
import random
import re
import sys
import threading
import time
import zlib
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .bridge_client import BridgeWorker

# Jewels stored as one byte per (notable, seed) in `Data/TimelessJewelData/<stem>.zip`.
# The others (Glorious Vanity, the Abyss family) use variable-length records: every seed is calculated.
FLAT_TABLES = {
    "Lethal Pride": "LethalPride",
    "Brutal Restraint": "BrutalRestraint",
    "Militant Faith": "MilitantFaith",
    "Elegant Hubris": "ElegantHubris",
    "Heroic Tragedy": "HeroicTragedy",
}
SEED_STEP = {"Elegant Hubris": 20}  # PoB Modules/Data.lua: this one only exists in multiples of 20
STAT_KEYS = (
    "CombinedDPS", "TotalEHP", "Life", "EnergyShield", "Mana",
    "FireResist", "ColdResist", "LightningResist", "ChaosResist",
    "EffectiveBlockChance", "EffectiveSpellBlockChance", "Str", "Dex", "Int",
)
RESET_EVERY = 150  # each calculated seed leaves one more item in the build; reload before that slows PoB down
MIN_SAMPLE = 200  # groups calculated before the first estimate
VERIFY_BATCH = 50  # estimated-best groups calculated per round
_SEED_LINE = re.compile(r"^(?:\{variant:[\d,]+\})?(.*\((\d+)-(\d+)\).* (\w+))$")


def default_workers() -> int:
    # An engine grows to roughly 1.3 GB during a search, so stay modest unless the user asks for more.
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def set_idle_priority() -> None:
    """Lowest CPU priority for this process; the PoB engines started afterwards inherit it."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p  # a 64-bit handle; the default int return truncates it
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        if not kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x40):  # IDLE_PRIORITY_CLASS
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.nice(19)


def load_jewels(game_dir: Path) -> dict[str, dict[str, Any]]:
    """Timeless jewel definitions from PoB's unique item database, keyed by English name."""
    text = (Path(game_dir) / "Data" / "Uniques" / "jewel.lua").read_text(encoding="utf-8")
    jewels: dict[str, dict[str, Any]] = {}
    for entry in re.split(r"\]\],[^\[]*\[\[", text):  # entries may have a `-- comment` line between them
        lines = [x.strip() for x in entry.strip().splitlines() if x.strip()]
        if "Historic" not in lines or "Implicits: 0" not in lines or not any(_SEED_LINE.match(x) for x in lines):
            continue
        name = lines[0]
        conquerors: dict[str, str] = {}
        tail: list[str] = []
        lo = hi = 0
        for line in lines[lines.index("Implicits: 0") + 1:]:
            m = _SEED_LINE.match(line)
            if m:
                lo, hi = int(m.group(2)), int(m.group(3))
                conquerors[m.group(4)] = m.group(1).replace(f"({lo}-{hi})", "{seed}")
            elif not line.startswith("{variant:"):  # variant lines are optional mods (Militant Faith devotion)
                tail.append(line)
        jewels[name] = {
            "base": lines[1],
            "head": [x for x in lines[2:] if x.startswith(("Limited to:", "Radius:"))],
            "conquerors": conquerors,
            "tail": tail,
            "seeds": range(lo, hi + 1, SEED_STEP.get(name, 1)),
        }
    return jewels


def jewel_text(jewel: dict[str, Any], name: str, conqueror: str | None = None, mods: Sequence[str] = ()) -> str:
    """PoB item text for the jewel with the seed left as `{seed}`."""
    conqueror = conqueror or next(iter(jewel["conquerors"]))
    return "\n".join([
        "Rarity: UNIQUE", name, jewel["base"], "Item Level: 84", *jewel["head"], "Implicits: 0",
        jewel["conquerors"][conqueror], *mods, *jewel["tail"],
    ])


def load_node_index(game_dir: Path) -> dict[int, int]:
    """Node id -> row in the flat tables (only notables have rows)."""
    text = (Path(game_dir) / "Data" / "TimelessJewelData" / "NodeIndexMapping.lua").read_text(encoding="utf-8")
    n_notable = int(re.search(r'nodeIDList\["sizeNotable"\] = (\d+)', text).group(1))  # type: ignore[union-attr]
    rows = {int(a): int(b) for a, b in re.findall(r"nodeIDList\[(\d+)\] = \{ index = (\d+),", text)}
    return {node: row for node, row in rows.items() if row < n_notable}


def load_table(game_dir: Path, stem: str) -> bytes:
    """Decompressed lookup table. Reads PoB's files only; never writes next to them."""
    folder = Path(game_dir) / "Data" / "TimelessJewelData"
    packed = folder / f"{stem}.zip"
    if packed.exists():
        raw = packed.read_bytes()
    else:
        parts = sorted(folder.glob(f"{stem}.zip.part*"), key=lambda p: int(p.suffix[5:]))
        raw = b"".join(p.read_bytes() for p in parts)
    try:
        return zlib.decompress(raw)
    except zlib.error:
        return zlib.decompress(raw, -15)


def group_seeds(seeds: Sequence[int], table: bytes, index: dict[int, int], nodes: Sequence[int]) -> dict[bytes, list[int]]:
    """Table bytes on `nodes` (the signature) -> the seeds that share them."""
    n = len(seeds)
    groups: dict[bytes, list[int]] = defaultdict(list)
    for offset, seed in enumerate(seeds):
        groups[bytes(table[index[node] * n + offset] for node in nodes)].append(seed)
    return dict(groups)


def fit_pieces(samples: Sequence[tuple[bytes, float]], rounds: int = 25) -> Callable[[bytes], float]:
    """Additive estimate from calculated samples: value(signature) = mean + one effect per (position, byte).

    Backfitting: each position's effects are the average of what the other positions leave unexplained.
    """
    k = len(samples[0][0])
    mean = sum(value for _, value in samples) / len(samples)
    effects: list[dict[int, float]] = [defaultdict(float) for _ in range(k)]
    for _ in range(rounds):
        for pos in range(k):
            left: dict[int, list[float]] = defaultdict(list)
            for sig, value in samples:
                left[sig[pos]].append(value - mean - sum(effects[p][sig[p]] for p in range(k) if p != pos))
            for byte, values in left.items():
                effects[pos][byte] = sum(values) / len(values)
    return lambda sig: mean + sum(effects[p].get(sig[p], 0.0) for p in range(k))


class Engine:
    """One throwaway PoB engine holding the build; recalculates it with a given seed socketed."""

    def __init__(self, code: str, template: str, slot: str) -> None:
        self.code, self.template, self.slot, self.count = code, template, slot, 0
        self.worker = BridgeWorker("test")  # "test" slots are never persisted to the runtime dir
        self.worker.start()

    def reset(self) -> None:
        self.worker.call("import_code", {"code": self.code, "mode": "replace"}, timeout=120)
        self.count = 0

    def read(self) -> dict[str, Any]:
        stats = self.worker.call("get_stats", {}, timeout=60).get("stats") or {}
        return {key: stats.get(key) for key in STAT_KEYS}

    def stats(self, seed: int) -> dict[str, Any]:
        if self.count % RESET_EVERY == 0:
            self.reset()
        self.count += 1
        self.worker.call(
            "add_item", {"raw": self.template.format(seed=seed), "equip": True, "slotName": self.slot}, timeout=60
        )
        return self.read()

    def stop(self) -> None:
        self.worker.stop()


def describe(code: str) -> dict[str, Any]:
    """Jewel types (English + this PoB's display language) and the build's allocated jewel sockets."""
    engine = Engine(code, "", "")
    try:
        engine.reset()
        jewels = load_jewels(engine.worker.game_dir)
        dialog = engine.worker.call("tj_open", {}, timeout=60)
        engine.worker.call("tj_close", {})
        allocated = {int(x) for x in engine.worker.call("get_tree_state", {}, timeout=60)["allocatedNodes"]}
        local = {o["label"]: o.get("labelZh") or o["label"] for o in dialog["jewel"]["options"]}
        sockets = [
            {"nodeId": node, "label": opt.get("label"), "labelLocal": opt.get("labelZh")}
            for node, opt in zip(dialog.get("socketIds") or [], dialog["socket"]["options"])
            if node in allocated
        ]
        return {
            "jewels": [
                {"name": name, "nameLocal": local.get(name, name), "seeds": len(j["seeds"]),
                 "conquerors": list(j["conquerors"]), "grouped": name in FLAT_TABLES}
                for name, j in jewels.items()
            ],
            "sockets": sockets,
        }
    finally:
        engine.stop()


def _resolve_jewel(name: str, jewels: dict[str, Any], engine: Engine) -> str:
    """English name, or the name in whatever language this PoB shows (asked from PoB, nothing hardcoded)."""
    for key in jewels:
        if key.lower() == name.lower():
            return key
    options = engine.worker.call("tj_open", {}, timeout=60)["jewel"]["options"]
    engine.worker.call("tj_close", {})
    for option in options:
        if name in (option.get("labelZh"), option.get("label")) and option["label"] in jewels:
            return option["label"]
    raise ValueError(f"Unknown timeless jewel {name!r}. Known: {', '.join(jewels)}")


def _seed_nodes(engine: Engine, xml: str, index: dict[int, int]) -> list[int]:
    """Notables whose table entry can change this build: conquered by the socketed jewel AND either allocated
    or granted by an item ("Allocates X", e.g. an anoint) - PoB uses the conquered version for those too."""
    state = engine.worker.call("get_tree_state", {}, timeout=60)
    conquered = {int(k) for k, v in state["overrides"].items() if v.get("why") == "conquered"}
    used = {int(x) for x in state["allocatedNodes"]}
    granted = set(re.findall(r"Allocates ([^\n<]+)", xml))
    if granted:
        tree = engine.worker.get_cached_tree_data(state.get("treeVersion")).get("nodes", {})
        used |= {int(node) for node, info in tree.items() if info.get("name") in granted}
    return sorted(node for node in conquered & used if node in index)


def search(
    code: str,
    xml: str,
    socket: int,
    jewel_name: str,
    *,
    conqueror: str | None = None,
    mods: Sequence[str] = (),
    stat: str = "CombinedDPS",
    top: int = 15,
    workers: int | None = None,
    exact: bool = False,
    verify: int = 10,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Find the seeds of a timeless jewel at `socket` that give the highest `stat`.

    The jewel replaces whatever is in that socket; `baseline` is the build as loaded. With `exact` every
    group is calculated; otherwise large searches calculate a sample, estimate the rest, and calculate the
    estimated-best until none of the remaining can reach the top `top` (see module docstring).
    """
    started = time.time()
    top = max(1, top)
    say = progress or (lambda _msg: None)
    slot = f"Jewel {socket}"
    first = Engine(code, "", slot)
    engines: list[Engine | None] = [first]
    try:
        first.reset()
        jewels = load_jewels(first.worker.game_dir)
        name = _resolve_jewel(jewel_name, jewels, first)
        jewel = jewels[name]
        if conqueror and conqueror not in jewel["conquerors"]:
            raise ValueError(f"{name} conquerors: {', '.join(jewel['conquerors'])}")
        conqueror = conqueror or next(iter(jewel["conquerors"]))
        allocated = {int(x) for x in first.worker.call("get_tree_state", {}, timeout=60)["allocatedNodes"]}
        if socket not in allocated:
            raise ValueError(f"Node {socket} is not an allocated jewel socket of this build (see `pob timeless list`).")
        template = first.template = jewel_text(jewel, name, conqueror, mods)
        seeds = list(jewel["seeds"])
        baseline = first.read()

        first.stats(seeds[0])  # socket one so PoB reports what the jewel conquers
        nodes: list[int] = []
        if name in FLAT_TABLES:
            index = load_node_index(first.worker.game_dir)
            table = load_table(first.worker.game_dir, FLAT_TABLES[name])
            if len(table) != len(index) * len(seeds):
                raise ValueError(f"{name} lookup table has an unexpected size for its seed range.")
            nodes = _seed_nodes(first, xml, index)
            groups = group_seeds(seeds, table, index, nodes)
        else:
            groups = {str(seed).encode(): [seed] for seed in seeds}  # no usable signature: one group per seed

        n_workers = max(1, min(workers or default_workers(), len(groups)))
        lock = threading.Lock()
        results: dict[bytes, dict[str, Any]] = {}

        def calculate(sigs: Sequence[bytes]) -> None:
            n = min(n_workers, len(sigs))
            engines.extend([None] * (n - len(engines)))

            def work(i: int) -> None:
                engine = engines[i] = engines[i] or Engine(code, template, slot)
                for sig in sigs[i::n]:
                    stats = engine.stats(groups[sig][0])
                    with lock:
                        results[sig] = stats
                        if len(results) % 1000 == 0:
                            say(f"{len(results)} calculated")

            with ThreadPoolExecutor(n) as pool:
                list(pool.map(work, range(n)))

        rng = random.Random(1)
        all_sigs = list(groups)
        sample_size = max(MIN_SAMPLE, 2 * sum(len({sig[p] for sig in all_sigs}) for p in range(len(nodes))))
        estimate: dict[str, Any] | None = None
        if exact or not nodes or len(groups) <= 3 * sample_size:
            calculate(all_sigs)
        else:
            calculate(rng.sample(all_sigs, sample_size))
            say(f"{len(results)} sampled of {len(groups)} groups; estimating the rest")
            use_log = all((results[sig].get(stat) or 0) > 0 for sig in results)

            def value(sig: bytes) -> float:
                raw = float(results[sig].get(stat) or 0)
                return math.log(max(raw, 1e-9)) if use_log else raw

            while True:
                done = [(sig, value(sig)) for sig in results]
                predict = fit_pieces(done)
                # doubled: the fit has seen these samples, so its error on unseen groups runs higher
                error = 2 * max(abs(predict(sig) - v) for sig, v in done)
                cutoff = sorted((v for _, v in done), reverse=True)[min(top, len(done)) - 1]
                open_sigs = sorted((s for s in all_sigs if s not in results and predict(s) + error >= cutoff),
                                   key=predict, reverse=True)
                estimate = {"calculated": len(results), "open": len(open_sigs),
                            "maxError": (math.exp(error) - 1) if use_log else error, "relative": use_log}
                if not open_sigs:
                    break
                if len(results) > 0.4 * len(groups):  # the estimate is not narrowing things down: finish exactly
                    calculate([s for s in all_sigs if s not in results])
                    estimate = None
                    break
                calculate(open_sigs[:VERIFY_BATCH])
                say(f"{len(results)} calculated; {max(0, len(open_sigs) - VERIFY_BATCH)} could still reach the top {top}")

        # Self-check: another member of a group must give the very same numbers as its representative.
        multi = [sig for sig in results if len(groups[sig]) > 1]
        picks = rng.sample(multi, min(verify, len(multi)))
        mismatches = []
        for sig in picks:
            other = rng.choice(groups[sig][1:])
            if first.stats(other) != results[sig]:
                mismatches.append(other)
    finally:
        for engine in engines:
            if engine:
                engine.stop()

    outcomes: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for sig, stats in results.items():
        outcomes[tuple(stats[key] for key in STAT_KEYS)].extend(groups[sig])
    ranked = sorted(outcomes.items(), key=lambda kv: -(kv[0][STAT_KEYS.index(stat)] or 0))
    # With an estimate, only seeds at or above the value of the `top`-th best calculated group are complete.
    cutoff = None
    if estimate:
        cutoff = sorted((float(s.get(stat) or 0) for s in results.values()), reverse=True)[min(top, len(results)) - 1]
    return {
        "cutoff": cutoff,
        "jewel": name,
        "conqueror": conqueror,
        "socket": socket,
        "mods": list(mods),
        "stat": stat,
        "seedCount": len(seeds),
        "groupCount": len(groups),
        "calculated": len(results),
        "estimate": estimate,
        "seedNodes": nodes,
        "workers": n_workers,
        "baseline": baseline,
        "outcomes": [{"stats": dict(zip(STAT_KEYS, key)), "seeds": sorted(members)} for key, members in ranked],
        "selfCheck": {"checked": len(picks), "mismatches": mismatches},
        "seconds": round(time.time() - started, 1),
    }
