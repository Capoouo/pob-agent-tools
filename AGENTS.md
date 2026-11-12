# AGENTS.md — `pob` Headless PoB CLI & Agent Tools

## 1. Quick Commands (`pob`)

```powershell
# Lifecycle & I/O (auto-starts local daemon on 127.0.0.1 + X-PoB-Token; state in F:/poe/.runtime/)
pob status [--json]                                  # Check main / ref slots & undo/redo depth
pob load "<url|code|@path.xml>" [--target main|ref]  # Load XML, Base64, pobb.in/poedb/maxroll/rentry/pastebin, or poe.ninja char URL
pob save <out.xml> [--target main|ref]               # Export build XML to disk (written locally by CLI)
pob export-code [--target main|ref]                  # Print URL-safe Base64 share code
pob switch-main [--spec <N|title>] [--skill-group G] [--skill N] [--part N] [--item-set <ID|title>] [--skill-set <ID|title>] [--config-set <ID|title>] [--loadout <N|title>]
pob undo | pob redo | pob stop

# Read-Only Inspection & Audit
pob summary [--target main|ref] [--json]             # Offence (Hit/DoT/FullDPS/Minion*), Defence (TotalEHP/MaxHit/Res/Regen), Warnings
pob audit [--target main|ref] [--json]               # Hard gaps + 4-dim water audit (Config/Flasks/TempSkills/Party -> Pinnacle baseline)
pob items [--slot "Weapon 1"|"#1"] [--raw] [--json]  # List equipped items or inspect slot/jewel socket (#1..#N)
pob skills [--group main|1|"label"] [--json]         # List skill groups or per-gem DPS tooltips
pob tree [--search "keyword"|"#1"] [--json]          # Allocated Keystones/Asc/Masteries/Notables/Jewels or search nodes
pob calc "<keyword>" [--json]                        # Search get_calcs & fetch calcs_breakdown tables/formulas

# What-If Simulation (default: 0-residue rollback) & Persistent Mutation (--apply)
pob item try [--slot S] (--raw "<text>" | --file f) [--tooltip-only] [--apply]
pob item flask <1..5|all> <on|off> [--apply]
pob item anoint [--slot Amulet] --notable "<name>" [--index 1..4] [--apply]
pob skill set --group main [--gem <idx|name>] [--new-gem "<name>"] [--level N] [--quality N] [--enabled true|false] [--count N] [--full-dps true|false] [--apply]
pob skill add --group main --gem "<name>" [--level 20] [--quality 20] [--apply]
pob skill rm --group main --gem <idx|name> [--apply]
pob tree change [--allocate id1,id2:effectId] [--deallocate id3] [--apply]
pob config set [<var>=<val|reset> ...] [--custom-mods "<text|clear>"] [--apply]
pob batch (--file ops.json | --json-ops '[...]') [--apply]

# ROI Ranking, 3-Baseline A/B Comparison & Transplant
pob rank-nodes [--stat CombinedDPS|TotalEHP|Life] [--max-dist 5] [--top 15] [--json]
pob rank-gems [--group main] [--slot N | --add-link] [--field CombinedDPS|FullDPS] [--top 15] [--json]
pob compare [<ref_source>] [--json]                  # 3 baselines: (1) Original (2) Target Env Aligned to main (3) Both Pinnacle Dewatered
pob borrow [--item-slot "Weapon 1,Amulet"] [--skill-group main] [--apply] [--json]

# External Economy & Mechanics (Cached)
pob price "<query>" [--league L] [--category unique|gem|currency|card|all] [--json]  # poe.ninja (15m cache)
pob wiki "<topic>" [--json]                                                          # poewiki.net MediaWiki API (24h cache)
```

## 2. Architecture & Paths

- Engine: `F:/poe/PobTools[20261102]/pob-zh.exe --engine-headless F:/poe/PobTools[20261102]/PoE1` (`cwd` must be `PoE1`; in PowerShell always pass `-LiteralPath` because of `[20261102]`).
- Bridge dispatcher: `F:/poe/PobTools[20261102]/Data/bridge/bridge.lua` (PoB `2.67.2` master, protocol `1`, bridge `0.2.0`).
- CLI package: `F:/poe/pob_cli/src/pob_cli/` (`bridge_client.py`, `daemon.py`, `importers.py`, `snapshot.py`, `analyzer.py`, `mutators.py`, `comparator.py`, `external_data.py`, `formatters.py`, `cli.py`).
- Runtime state: `F:/poe/.runtime/daemon.json` (`pid`, `port`, `token`), `slot_main.json`, `slot_ref.json`, `cache/`.

## 3. Hard Constraints (`R1`..`R12`)

- Blocked RPCs (`FORBIDDEN_METHODS` in `bridge_client.py`):
  - `compare_*` (`R1`: `Build.compareTab` absent on `2.67.2` master; use dual-slot `comparator.py`).
  - `sidebar_breakdown` (`R2`: `caps.sidebarBreakdown == false`; use `get_calcs` + `calcs_breakdown`).
  - `config_mod_search`, `config_mod_add` (`R3`: `caps.configModBrowser == false`; use `list_config`/`set_config`/`set_custom_mods`).
  - `set_pob_options`, `oauth_logout` (`R4`: never mutate `Settings.xml`; always call `new_build{}` on worker boot).
  - `import_from_uri` (`R7`: rejects parentheses/non-ASCII; for `poe.ninja` characters resolve `index-state` -> `httpx.get("https://poe.ninja/poe1/pob/raw/overview/code")` -> `import_code`).
  - `save_build_as` (`R11`: restricted to `main.buildPath`; export via `export_code`/`save_xml` and write disk files in `cli.py`).
- Never parse `Build.xml` directly to compute DPS/EHP (`R5`: all stats must come from `pob-zh.exe --engine-headless`).
- Never scrape or call `www.pathofexile.com/character-window/*` (`R6`: Cloudflare `403` without `POESESSID`).
- Timeout & crash auto-healing (`R8`): `BridgeWorker` serializes RPCs with `RLock`, drains `stderr`, enforces per-RPC deadlines (`15s`/`30s`/`120s`), and restores `last_good_code` on crash.
- Color codes (`^xRRGGBB`, `^0..^9`, `R9`): never strip in `bridge_client.py` (corrupts `item_raw`/`save_xml`/`export_code`); strip only in `formatters.py` while preserving `raw`/`xml`/`code`/`notes`/`text`.
- Loopback security (`R10`): daemon binds `127.0.0.1` only and validates `X-PoB-Token`.
- `tree_click` is a toggle (`R12`): always check `get_tree_state()["allocatedNodes"]` before clicking each node; validate `notableDB` options before `item_edit_popup` `pick` so invalid anoints never silently pass.
- Config quirks: disable `customMods` with `[{"title": "Default", "text": "# disabled", "enabled": True}]` (empty list falls back to legacy `input.customMods` in `ConfigTab.lua`); re-apply `presetBossSkills` after changing `enemyIsBoss`.
