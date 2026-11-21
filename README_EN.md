# pob-agent-tools (`pob`)

[简体中文](README.md) | **English**

`pob-agent-tools` is a Path of Building (PoB) CLI toolkit for AI agents and Path of Exile 1 players. Its command is `pob`. It runs PoB in the background so you can import builds, try gear, compare passives and gems, and look up game data and trade prices.

DPS, effective hit pool (TotalEHP), maximum hit taken (MaxHit), and the effects of gear, passive, gem, or Timeless Jewel changes are calculated by a running PoB engine. The tool does not estimate them from modifiers in a build XML file.

---

## Contents

- [What it does](#what-it-does)
- [Installation and setup](#installation-and-setup)
  - [Windows x64](#1-windows-x64)
  - [macOS and Linux](#2-macos-and-linux)
  - [Set up the PoB engine](#3-set-up-the-pob-engine)
- [Options and build management](#options-and-build-management)
- [Features and examples](#features-and-examples)
  - [1. Inspect a build and its PoB configuration](#1-inspect-a-build-and-its-pob-configuration)
  - [2. Compare combat scenarios](#2-compare-combat-scenarios)
  - [3. Try gear, flasks, anoints, gems, and passives](#3-try-gear-flasks-anoints-gems-and-passives)
  - [4. Evaluate several changes or variants](#4-evaluate-several-changes-or-variants)
  - [5. Find passive and gem upgrades; compare builds](#5-find-passive-and-gem-upgrades-compare-builds)
  - [6. Search Timeless Jewel seeds](#6-search-timeless-jewel-seeds)
  - [7. Look up game data, prices, and build trends](#7-look-up-game-data-prices-and-build-trends)
  - [8. Search trade listings and bench crafts](#8-search-trade-listings-and-bench-crafts)
- [Exit codes](#exit-codes)
- [Development, tests, and releases](#development-tests-and-releases)
- [Credits and license](#credits-and-license)

---

## What it does

- **Install and update the PoB engine** (`pob bootstrap` / `pob update`): Windows uses PobTools-zh; macOS and Linux use a locally compiled LuaJIT runtime. The tool checks for PobTools-zh and PoB updates, keeps the previous working version if an update fails, and can use the local engine while GitHub is unreachable.
- **Manage several builds at once**: Name a build slot with `--target <slug>`, such as `main`, `ref`, or `mageblood_test`. Each slot has its own undo and redo history. `pob serve` keeps the engine running between commands.
- **Check inflated PoB numbers** (`pob audit` / `pob matrix`): See how configuration, flasks, temporary or Vaal skills, and party buffs affect DPS and effective hit pool. PoB recalculates the effect of each condition. Magic flasks sustained by Mageblood, the regular version of a Vaal skill, and persistent auras are handled separately. Keep conditions your build can reliably sustain with `--keep`; the tool checks that the original build is restored afterward.
- **Try and compare changes** (`pob batch` / `pob sweep`): Gear, gems, passives, and configuration changes are simulations by default; the original build is restored afterward. Add `--apply` to save a change. Combine several edits or compare multiple gear and gem setups in one run.
- **Search Timeless Jewel seeds** (`pob timeless list` / `pob timeless search`): Evaluate candidate seeds in PoB using your allocated socket and passives. English and Chinese jewel names are accepted. Use `--exact` to evaluate all candidates.
- **Look up game data and prices** (`pob data` / `pob price` / `pob meta` / `pob poedb` / `pob wiki`): The local bilingual index covers gems, unique items, modifiers, and bases. Online queries cover poe.ninja prices and build trends, PoEDB, and PoE Wiki.
- **Find gear and fill modifier gaps** (`pob trade` / `pob bench`): Search official trade listings, compare upgrades in PoB, inspect available bench crafts, and preview same-tier Harvest resistance conversions. Trade commands only search and calculate; they do not buy items for you.

---

## Installation and setup

### 1. Windows x64

The standalone Windows package includes its runtime; Python is not required.

#### Option 1: PowerShell installer (recommended)

```powershell
irm https://raw.githubusercontent.com/exilemoulei/pob-agent-tools/main/install.ps1 | iex
```

If your network needs a proxy to reach GitHub:

```powershell
$env:POB_PROXY = "http://127.0.0.1:7890"
irm https://raw.githubusercontent.com/exilemoulei/pob-agent-tools/main/install.ps1 | iex
```

Run the same command again to upgrade. The installer checks SHA256 and stops the old background process.

#### Option 2: Scoop

The repository includes a Scoop manifest (`bucket/pob.json`) that is updated with each release:

```powershell
scoop bucket add pob https://github.com/exilemoulei/pob-agent-tools
scoop install pob

scoop update pob
```

#### Option 3: Portable GitHub release

Download `pob-v0.4.0-windows-x64.zip` from [GitHub Releases](https://github.com/exilemoulei/pob-agent-tools/releases/latest), extract it, and add the folder containing `pob.exe` to your `PATH`.

#### Option 4: Python package (Python 3.11+, `uv` / `pipx` / `pip`)

```powershell
uv tool install "git+https://github.com/exilemoulei/pob-agent-tools.git@v0.4.0#subdirectory=pob_cli"

pip install "https://github.com/exilemoulei/pob-agent-tools/releases/download/v0.4.0/pob_cli-0.4.0-py3-none-any.whl"
```

---

### 2. macOS and Linux

On macOS (Apple Silicon `arm64` / Intel `x86_64`) or Linux (`x86_64` / `aarch64`), install Python 3.11+, `cc`, `make`, `git`, and the system `zlib` headers. Then compile `LuaJIT 2.1 + lua-utf8` once:

```bash
git clone https://github.com/exilemoulei/pob-agent-tools.git
cd pob-agent-tools

pip install "./pob_cli"

export POB_RUNTIME_DIR="$HOME/.local/share/pob-agent-tools"
chmod +x pob_cli/packaging/build_posix_runtime.sh
./pob_cli/packaging/build_posix_runtime.sh ./runtime-dist
mkdir -p "$POB_RUNTIME_DIR/engine"
cp -R ./runtime-dist/bin "$POB_RUNTIME_DIR/engine/bin"

pob bootstrap --no-zh
pob status
```

---

### 3. Set up the PoB engine

After installing `pob`, run `pob bootstrap` once to download the PoB engine. You can also import an existing local `PobTools` directory:

```powershell
pob --version
pob bootstrap
pob bootstrap --proxy http://127.0.0.1:7890
pob bootstrap --from-dir "D:\PoE\PobTools"
pob update --check
pob update --proxy http://127.0.0.1:7890
```

Common environment variables:

| Variable | Purpose |
|---|---|
| `POB_ROOT` | Explicit engine root; otherwise `<POB_RUNTIME_DIR>/engine` is discovered automatically |
| `POB_RUNTIME_DIR` | Engine, daemon state, and cache root; installed Windows default: `%LOCALAPPDATA%\pob-agent-tools`, source checkout default: `<repo>/.runtime` |
| `POB_CACHE_DIR` | Override the local game database and online query cache directory |
| `POB_SLOT` | Default build slot if `--target` is omitted; defaults to `main` |
| `POB_PROXY` | HTTP, HTTPS, or SOCKS5 proxy; `HTTPS_PROXY` and `ALL_PROXY` are also supported |
| `POB_GITHUB_MIRROR` | Mirror prefix for GitHub downloads during `bootstrap` and `update` |
| `POB_OFFLINE` | Set to `1` to skip startup and scheduled GitHub update checks; equivalent to global `--offline` |
| `POESESSID` / `POE_COOKIE` | Optional when the official trade API returns 401/403; provide them only as environment variables, never as CLI arguments or log entries |

---

## Options and build management

### Output and build slots

Commands can return JSON, compact output on one line, or write UTF-8 directly to a file. Direct file output also avoids PowerShell redirection encoding issues on Windows:

```powershell
pob --offline --compact --out result.json summary --json

pob status
pob status --json
```

### Keep the PoB engine running (`pob serve`)

Build commands normally start the daemon in the background. If your agent environment removes child processes after each command, run `pob serve` in a separate session. It listens only on local `127.0.0.1` and authenticates requests with `X-PoB-Token`:

```powershell
pob serve --offline

pob stop
```

### Import, create, and export builds

`pob load` accepts local `.xml` / `.txt` files, Base64 share codes, common PoB share links, and `poe.ninja` character page links:

```powershell
pob load "https://poedb.tw/pob/AWqOLSSmyG"
pob load "@D:\builds\my_chieftain.xml"
pob load "https://poe.ninja/poe1/builds/mirage/character/AccountName-1234/CharName"

pob load "https://pobb.in/xxxxxx" --target ref
pob new --class Marauder --asc Chieftain --level 95 --target exp1

pob switch-main --loadout "Endgame Mageblood"
pob switch-main --spec 2 --item-set 2 --skill-group 1

pob save "D:\builds\optimized.xml"
pob export-code --target exp1
```

---

## Features and examples

### 1. Inspect a build and its PoB configuration

When you receive a PoB, start with `pob summary` to inspect its offence and defence. Then use `pob audit` to check resistance, accuracy, spell suppression, and other gaps, as well as temporary conditions that inflate its displayed numbers. The audit disables those conditions and recalculates the baseline:

```powershell
pob summary

pob audit

pob audit --no-attribution
```

If a build reliably meets a condition, such as standing still, sustaining Endurance Charges, or keeping an enemy ignited, retain it with `--keep`. Individual custom modifier groups can also be retained:

```powershell
pob audit --keep conditionStationary,useEnduranceCharges,conditionEnemyIgnited
pob audit --keep "Flask 1,Vaal Haste,customMods:Boss Setup"

pob audit --keep conditionStationery --no-attribution
```

Use these read-only commands to inspect items, gems, passives, configuration, and calculation details:

```powershell
pob config list
pob config list --all --search "boss" --with-options

pob items
pob items --slot "Weapon 1" --raw
pob items --all --raw

pob skills
pob skills --group main

pob tree --search "最大生命" "法术压制" --unallocated --max-cost 4 --show-path
pob tree --type Mastery --allocated --masteries
pob tree --socket-radius 61419

pob calc "Average Hit" "Spell Suppression" "Mana Regen"
```

---

### 2. Compare combat scenarios

`pob matrix` compares the current configuration (`Original`), Pinnacle boss (`Pinnacle`), Uber boss (`Uber`), flasks off (`FlasksOff`), and optional custom scenarios in one run. It checks that the build is restored after the comparison:

```powershell
pob matrix

pob matrix Original Pinnacle Uber `
  -s "Wither15:Pinnacle,MultiplierWitheredStackCount=15" `
  -s "NoGuard:Pinnacle,conditionMoltenShell=false" `
  --keep conditionStationary,flasks

pob matrix Original Pinnacle --full --json --out matrix_full.json
```

---

### 3. Try gear, flasks, anoints, gems, and passives

Changes to gear, passives, gems, and configuration are simulations by default: you see the before/after difference, then the original build is restored. Add `--apply` to keep a change, and use `pob undo` / `pob redo` afterward if needed.

```powershell
pob item try --slot "Amulet" --file .\candidate_amulet.txt

pob item edit --slot "Ring 1" `
  --replace-mod "Cold Resistance" "+48% to Lightning Resistance" `
  --add-mod "+55 to maximum Life"

pob item db "法师之血" --slot Belt --equip
pob item flask all off

pob item anoint --slot Amulet --notable "Whispers of Doom"

pob skill add --group main --gem "Awakened Elemental Focus Support" --level 5 --quality 20
pob skill set --group main --gem 1 --level 21 --quality 20
pob skill new-group --label "Aura" --slot "Helmet" `
  --gem "Determination:21:0" --gem "Grace:21:0" --gem "Enlighten Support:4:0"
pob skill replace-group --group main `
  --gem "Ice Nova of Frostbolts:21:20" `
  --gem "Archmage Support:20:20" `
  --gem "Spell Echo Support:20:20"

pob tree change --allocate "26725,49080:64315" --deallocate "12345"
pob tree set-class Witch Occultist --secondary-asc None

pob config set enemyIsBoss=Pinnacle conditionStationary=true --append-mods "+15% more Damage"

pob item anoint --slot Amulet --notable "Charisma" --apply
pob undo
pob redo
```

---

### 4. Evaluate several changes or variants

#### `pob batch`: combine edits in one simulation

`pob batch` supports **19 operation types** and can return the chosen build panels or audit results: `config`, `flask`, `item`, `item_db`, `item_edit`, `unequip`, `anoint`, `tree`, `tree_reset`, `import_tree_url`, `skill`, `add_group`, `delete_group`, `set_group`, `replace_group`, `new_build`, `set_level`, `set_class`, `switch_main`.

```powershell
pob batch --json-ops '[
  {"op": "item_db", "query": "Headhunter", "slot": "Belt"},
  {"op": "anoint", "slot": "Amulet", "notable": "Charisma"},
  {"op": "config", "set": {"enemyIsBoss": "Pinnacle", "conditionStationary": true}}
]' --keep conditionStationary --return "summary,audit,matrix,calc:DPS;Mana"
```

To see the effect of each temporary condition, change `audit` in `--return` to `audit:full`. For bulk generation, `--apply --no-undo` saves the build without creating undo snapshots.

#### `pob sweep`: compare several setups

Compare gear, anoints, support gems, or Ascendancy paths against a Pinnacle boss. An error in one variant does not stop the others:

```powershell
pob sweep --scenario Pinnacle --keep conditionStationary --calc "Mana" --json-variants '[
  {"name": "Baseline", "ops": []},
  {"name": "Anoint_Whispers", "ops": [{"op": "anoint", "slot": "Amulet", "notable": "Whispers of Doom"}]},
  {"name": "Anoint_Sovereignty", "ops": [{"op": "anoint", "slot": "Amulet", "notable": "Sovereignty"}]},
  {"name": "Mageblood_Swap", "ops": [{"op": "item_db", "query": "Mageblood", "slot": "Belt"}]}
]'
```

---

### 5. Find passive and gem upgrades; compare builds

Rank unallocated passives by point cost, compare support gem swaps or adding a link, compare two builds under three baselines, and try items or skills from a reference build:

```powershell
pob rank-nodes --stat CombinedDPS --max-dist 5 --top 15
pob rank-nodes --stat TotalEHP --max-dist 4 --top 10

pob rank-gems --group main --slot 5 --field FullDPS --top 10
pob rank-gems --group main --add-link --field CombinedDPS --top 10

pob compare "https://pobb.in/ReferenceBuildCode"

pob borrow --item-slot "Weapon 1,Amulet,Ring 1" --skill-group main
```

---

### 6. Search Timeless Jewel seeds

`pob timeless` evaluates candidate seeds inside PoB, including the value lost when an existing notable is replaced. It does not simply add the new modifiers' values. It supports the 11 Timeless Jewel variants in PoB's data:

```powershell
pob timeless list

pob timeless search `
  --jewel "致命的骄傲" `
  --socket 61419 `
  --conqueror Kaom `
  --stat CombinedDPS `
  --top 15 `
  --workers 4 `
  --idle

pob timeless search `
  --jewel "Heroic Tragedy" `
  --socket 26196 `
  --conqueror Vorana `
  --stat Ward `
  --mod "+10 to all Attributes" `
  --top 10

pob timeless search --jewel "Militant Faith" --socket 41263 --stat TotalEHP --exact --workers 4
```

> **Memory use:** Each parallel PoB worker uses about 1.3–1.7 GB. By default, `--workers` uses at most four workers. Add `--idle` to lower their CPU priority during background searches.

---

### 7. Look up game data, prices, and build trends

These commands accept multiple search terms. The local game data index works without starting PoB; price and wiki lookups use the network and cache results locally:

```powershell
pob data gem "Ice Nova of Frostbolts" "Archmage Support"
pob data unique "Mageblood" "Headhunter"
pob data mod "Spell Suppression" --slot Helmet --ilvl 85
pob data base "Spirit Shield" --limit 10
pob data translate "喋血之刃" "Whispers of Doom"

pob poedb "傲视头冠" --mods --lang cn
pob poedb " Hubris Circlet" --mods --lang us

pob price "Mageblood" "Progenesis" "Nimis"
pob price "Forbidden Flame" --variant "Hierophant" --limit 30
pob price "Awakened Spell Echo Support" --detailed
pob meta --skill "Ice Nova of Frostbolts" --top 15

pob wiki "Eldritch Battery" "Leech"
```

---

### 8. Search trade listings and bench crafts

Before buying an upgrade, use `pob audit` to find resistance or attribute gaps and `pob bench` to check whether a bench craft can fill them. Then search official trade listings with `pob trade search` and have PoB compare candidates' DPS and effective hit pool. Trade searches are read-only and never buy an item. See [docs/TRADE.md](docs/TRADE.md).

```powershell
pob trade stats "攻击速度" --slot weapon1
pob trade stats "法术压制率" --slot gloves

pob trade search --slot gloves --budget 25 --currency chaos_equiv `
  --stat "冰霜抗性=40" `
  --stat "最大生命=70" `
  --want "Attack Speed" `
  --allow-conversion `
  --sort pob --pob auto

pob trade search --slot ring1 --budget 15 --stat "最大生命=65" --open-suffix --pob auto
pob bench list --slot ring1 --affix suffix --search "抗性"
pob bench suggest --slot gloves --stat "冰霜抗性"

pob trade convert --file .\dropped_gloves.txt --to lightning
```

---

## Exit codes

Scripts and agents can use exit codes to decide what to do next. Errors are shown as short messages:

| Command | Code | Meaning and suggested action |
|---|---|---|
| General/build commands | `0` | Success |
| | `1` | Invalid arguments or build state, such as an uninitialized engine, empty slot, or damaged share code; fix the request or load a build first |
| | `5` | Daemon, PoB engine, or network failure; follow the error message and check `daemon.log` if needed |
| `pob trade` commands | `0` | Search or conversion succeeded |
| | `1` | Invalid argument or unmappable trade stat; similar stats are suggested |
| | `2` | No matching active listings; relax modifier thresholds or budget |
| | `3` | Official trade API returned 401/403, or Cloudflare blocked the request; set `POESESSID` in the environment if needed |
| | `4` | GGG rate limit reached; wait for the reported `Retry-After` period |
| | `5` | Official trade API error or network timeout |
| | `6` | Local PoB evaluation failed when `--pob on` was requested |

---

## Development, tests, and releases

```powershell
pip install -e "./pob_cli[dev]"

python -m pytest pob_cli/tests -v

pyinstaller --noconfirm --clean pob_cli/packaging/pob.spec
```

**Tag-driven releases** (`.github/workflows/release.yml`):

1. Update `__version__` in `pob_cli/src/pob_cli/__init__.py` and merge it into `main`.
2. Create and push a version tag: `git tag -s v0.4.0 -m "pob-agent-tools v0.4.0" && git push origin main v0.4.0`.
3. GitHub Actions checks the version, runs tests, builds and checks the Windows zip and Python packages, publishes the GitHub Release, and updates the Scoop manifest at `bucket/pob.json`.

For a compact command reference for agents, see [AGENTS.md](AGENTS.md).

---

## Credits and license

### Upstream projects and inspiration

- The architecture was inspired by [**ianderse/pob-mcp**](https://github.com/ianderse/pob-mcp) (GPL-3.0) and [**shalayiding/POEMCP**](https://github.com/shalayiding/POEMCP).
- [**Hsiung-Shao/PobTools-zh**](https://github.com/Hsiung-Shao/PobTools-zh) provides the localized engine and headless bridge (`pob-zh.exe --engine-headless` + `bridge.lua`). Core build calculations and game data come from [**PathOfBuildingCommunity/PathOfBuilding**](https://github.com/PathOfBuildingCommunity/PathOfBuilding).

### Community contributors

- **[@ChangMenQAQ](https://github.com/ChangMenQAQ)** ([PR #1](https://github.com/exilemoulei/pob-agent-tools/pull/1)): official trade search, same-tier Harvest resistance conversion, and crafting bench suggestions.
- **[@restartgr](https://github.com/restartgr)** ([PR #2](https://github.com/exilemoulei/pob-agent-tools/pull/2), [PR #4](https://github.com/exilemoulei/pob-agent-tools/pull/4)): inflated-panel audit, `--keep`, build restoration checks, and CLI exit codes.
- **[@Capoouo](https://github.com/Capoouo)** ([PR #3](https://github.com/exilemoulei/pob-agent-tools/pull/3)): PoB-calculated Timeless Jewel seed search and candidate filtering.

### License

Licensed under the **GNU General Public License v3.0 (GPL-3.0)**. See [LICENSE](LICENSE).
