# pob-agent-tools (`pob`)

High-signal CLI, managed dual-upstream headless engine, offline Lua game-data SQLite index, and persistent dual-slot daemon for **Path of Building (PoE 1)**, purpose-built for AI Agents and power users.

> **Acknowledgements & Lineage**  
> Inspired by [**ianderse/pob-mcp**](https://github.com/ianderse/pob-mcp) (GPL-3.0) and [**shalayiding/POEMCP**](https://github.com/shalayiding/POEMCP), powered by [**Hsiung-Shao/PobTools-zh**](https://github.com/Hsiung-Shao/PobTools-zh) (`pob-zh.exe --engine-headless` + `bridge.lua`) and official [**PathOfBuildingCommunity/PathOfBuilding**](https://github.com/PathOfBuildingCommunity/PathOfBuilding).

---

## Key Features (v0.2)

- **Managed Dual-Upstream Engine & Auto-Update (`pob bootstrap` / `pob update`)**:
  - Automatically resolves `POB_ROOT` -> `<POB_RUNTIME_DIR>/engine` -> read-only legacy seed (`F:/poe/PobTools[20261102]`).
  - Checks upstream on daemon startup (and every 30m with zero-loss slot hot-reload) via `PobTools-zh` `302` release tags (never `api.github.com`) + official PoB `manifest.xml` SHA1 incremental diff with `gate_result` smoke probe & `.bak/` rollback.
  - Fast-fails (`connect=3s`) when GitHub is unreachable, degrades cleanly to local engine, and emits dual-channel alerts (`stderr` + JSON `updateWarning` / Markdown `> [WARN]`).
- **Zero-Residue `what-if` Simulation & 30-Step Undo/Redo**: Every mutation (`item try/edit/db/flask/anoint`, `skill set/add/rm`, `tree change/set-class`, `config set [--append-mods]`, `batch`, `borrow`) runs inside an in-memory `export_code`/`import_code` transaction and rolls back unless `--apply` is passed.
- **Multi-Scenario Matrix (`pob matrix`) & 4-Dim Inflation Audit (`pob audit`)**: Compare `Original`, `Pinnacle`, `Uber`, `FlasksOff`, and custom `Label:Pinnacle,key=val` scenarios in one atomic pass, or strip Config/Flask/Skill/Party water automatically.
- **3-Baseline A/B Comparison (`pob compare`) & Transplant (`pob borrow`)**: Compare `main` vs `ref` across Original, Target-Env-Aligned, and Dewatered Pinnacle baselines with tree/skill/gear diffs.
- **0ms Local Offline Game Data (`pob data`)**: Pure-Python PoB Lua parser + SQLite/FTS5 index (`gem`, `unique` incl. Foulborn, `mod` incl. Bench/Eldritch/Corrupted, `base`, `translate` EN<->ZH) running daemon-free in `<15ms`.
- **External Economy, Meta, PoEDB & Wiki (`pob price` / `pob meta` / `pob poedb` / `pob wiki`)**:
  - `poe.ninja`: 52 overview categories, `--variant`, `--limit`, `--detailed` (corrupted/21-20 gems/links/listings via stash API), `pob meta` (`build-index-state`), and fixed character URL import (`Name-1234`).
  - `poedb.tw`: 24h CN/US autocomplete index + 12h cleaned pages with `DropChance` weights, `ModFamilyList`, and `fossil_no` tags.

---

## Installation & Environment

Requires **Python 3.11+** (Windows x64 for headless `pob-zh.exe`).

```bash
pip install -e ./pob_cli

# Optional: bootstrap a fresh managed engine (supports proxy, mirror, or offline directory)
pob bootstrap --proxy http://127.0.0.1:7890
pob bootstrap --from-dir "F:/poe/PobTools[20261102]"
```

Supported environment variables: `POB_ROOT`, `POB_RUNTIME_DIR`, `POB_PROXY` / `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`, `POB_GITHUB_MIRROR`, `POB_OFFLINE=1`.

---

## Quick Start

```bash
# 1. Check engine versions, update state, and load builds (URL, poe.ninja char, .xml, Base64)
pob status
pob update --check
pob load "https://poedb.tw/pob/AWqOLSSmyG"

# 2. Inspect summary, water audit, config, all equipped items/jewels, skills, tree, and calcs
pob summary
pob audit
pob config list
pob items --all --raw
pob skills --group main
pob calc "Average Hit"

# 3. What-if simulations (add --apply to persist; pob undo / pob redo to revert)
pob item edit --slot "Ring 1" --replace-mod "Cold Resistance" "+45% to Lightning Resistance"
pob item db "Mageblood" --equip
pob config set --append-mods "+100 to maximum Life"
pob tree set-class Witch Necromancer
pob matrix Original Pinnacle Uber FlasksOff

# 4. Rankings, 3-baseline comparison, and gear/skill transplant
pob rank-nodes --stat CombinedDPS --max-dist 4 --top 10
pob rank-gems --group main --top 10
pob compare "https://poedb.tw/pob/AWqOLSSmyG"
pob borrow --item-slot "Weapon 1,Amulet" --skill-group main

# 5. Local 0ms game data & external poedb / poe.ninja / wiki queries
pob data mod "Life Converted to Energy Shield"
pob data gem "Dark Bargain"
pob data translate "喋血之刃"
pob poedb "傲视头冠" --mods
pob price "Forbidden Flame" --variant "Necromancer" --limit 50
pob price "Empower Support" --detailed
pob meta --top 15
pob wiki "Eldritch Battery"
```

Pass `--json` to any command for structured, color-stripped JSON output. Pass `--offline` before a subcommand (or set `POB_OFFLINE=1`) to skip GitHub update checks. See [AGENTS.md](AGENTS.md) for the compact agent cheat-sheet.

---

## Running Tests

```powershell
$env:PYTHONPATH = "$PWD\pob_cli\src"
python -m pytest pob_cli/tests -v
```

## License

Licensed under the **GNU General Public License v3.0 (GPL-3.0)** — see [LICENSE](LICENSE) for details.
