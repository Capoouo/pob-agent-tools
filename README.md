# pob-agent-tools (`pob`)

High-signal CLI and persistent dual-slot headless daemon for **Path of Building (PoE 1)**, purpose-built for AI Agents and power users.

> **Acknowledgements & Lineage**  
> This project is a ground-up architectural redesign inspired by and forked in concept from:
> - [**ianderse/pob-mcp**](https://github.com/ianderse/pob-mcp) (GPL-3.0) — live Path of Building calculation engine integration, passive/gem optimization workflows, and build validation.
> - [**shalayiding/POEMCP**](https://github.com/shalayiding/POEMCP) — runtime `poe.ninja` dense economy pricing, `poewiki.net` mechanics extraction, and high-signal build diagnostics.
>
> Instead of exposing dozens of stateful MCP tools that bloat agent context windows or leave dirty build state behind, `pob-agent-tools` provides a single deterministic **`pob` CLI** backed by an authenticated localhost daemon (`main` + lazy `ref` slots) driving `pob-zh.exe --engine-headless` (`bridge.lua`).

---

## Key Features

- **Zero-Residue `what-if` Simulation by Default**: Every mutation (`item try`, `item flask`, `item anoint`, `skill set/add/rm`, `tree change`, `config set`, `batch`, `borrow`) executes inside a `<15ms` in-memory `export_code`/`import_code` transaction and rolls back automatically unless `--apply` is passed.
- **30-Step Undo / Redo Stack**: Persistent mutations (`--apply`) push lossless snapshots onto a per-slot undo/redo stack (`pob undo` / `pob redo`) persisted across daemon restarts.
- **4-Dimension PoB Inflation ("Water") Audit (`pob audit`)**: Non-destructively strips Config inflation, non-Mageblood active flasks, temporary Guard/Burst/Vaal skills (`Arcane Cloak`, `Sigil of Power`, `Frost Shield`, `Molten Shell`, `Immortal Call`, `Berserk`, `Vaal *`), and `PartyTab` buffs against a standard **Pinnacle Boss** to expose true baseline DPS and EHP.
- **3-Baseline A/B Build Comparison (`pob compare`) & Transplant (`pob borrow`)**:
  1. **Baseline 1**: Original configured panels (`main` vs `ref`).
  2. **Baseline 2**: External target environment whitelist aligned to `main` (without overwriting `ref`'s Bandit or Pantheon choices).
  3. **Baseline 3**: Both builds fully de-watered against a Pinnacle Boss.
  - Includes Keystone/Notable/Mastery/Ascendancy diffs, skill link diffs, equipment slot tables, and 1-command item/skill borrowing (`pob borrow`).
- **4-Source Unified Importer (`pob load`)**:
  - Local `.xml` files (encoded client-side) and raw Base64 share codes.
  - 8 PoB share sites (`pobb.in`, `poedb.tw/pob`, `poe.ninja/pob`, `maxroll.gg/poe/pob`, `pob.codes`, `pastebin.com`, `pastebinp.com`, `rentry.co`) with Python `/raw` fallback.
  - `poe.ninja` character URLs (`https://poe.ninja/poe1/builds/<league>/character/<account>/<name>`) via `index-state` + `raw/overview/code` (bypassing GGG `character-window` Cloudflare 403).
- **Economy & Wiki Queries**: `pob price` (`poe.ninja` dense overview API, 15m disk cache) and `pob wiki` (`poewiki.net` MediaWiki API, 24h disk cache).

---

## Installation

Requires **Python 3.11+** and a headless-capable `pob-zh.exe` installation (defaults to `F:/poe/PobTools[20261102]`, configurable via `POB_ROOT`).

```bash
pip install -e ./pob_cli
```

---

## Quick Start

```bash
# 1. Load a build (URL, poe.ninja character URL, local .xml, or Base64 share code)
pob load "https://poedb.tw/pob/AWqOLSSmyG"

# 2. Inspect offence/defence summary and run 4-dimension inflation ("water") audit
pob summary
pob audit

# 3. Inspect items, skills, passive tree, and formula breakdowns
pob items
pob items --slot "Amulet" --raw
pob skills --group main
pob tree --search "#1"
pob calc "Mana"

# 4. Run zero-residue what-if simulations (add --apply to persist)
pob item flask all off
pob item anoint --slot Amulet --notable "Charisma"
pob skill set --group main --gem 1 --new-gem "Vaal Ice Nova"
pob config set enemyIsBoss=Uber --custom-mods "+20% more Damage"
pob tree change --allocate 26725

# 5. Rank top ROI passive nodes and support gems
pob rank-nodes --stat CombinedDPS --max-dist 4 --top 10
pob rank-gems --group main --top 10

# 6. Compare against a reference build across 3 baselines and borrow gear/skills
pob compare "https://poedb.tw/pob/AWqOLSSmyG"
pob borrow --item-slot "Weapon 1,Amulet" --skill-group main

# 7. Query live economy prices & PoE Wiki mechanics
pob price "Mageblood" --category accessory
pob wiki "Eldritch Battery"
```

Pass `--json` to any command for structured, color-stripped JSON output suitable for programmatic consumption. See [AGENTS.md](AGENTS.md) for the compact agent cheat-sheet and [plan.md](plan.md) for full architecture specifications.

---

## Running Tests

```bash
pytest pob_cli/tests -v
```

---

## License

Licensed under the **GNU General Public License v3.0 (GPL-3.0)** — see [LICENSE](LICENSE) for details.
