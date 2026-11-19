# AGENTS.md — `pob` Headless PoB CLI & Agent Tools (v0.2)

## 1. Quick Commands (`pob [--offline]`)

```powershell
# Lifecycle, Engine Bootstrap/Update & Build I/O (auto-starts 127.0.0.1 daemon + X-PoB-Token; checks GitHub on boot & every 30m)
pob bootstrap [--proxy <url>] [--mirror <url>] [--no-zh] [--from-dir <dir>] [--json]  # Install/seed managed engine (PobTools-zh + PoB master)
pob update [--check] [--force] [--proxy <url>] [--mirror <url>] [--offline] [--json]  # Incremental SHA1 update (hot-reloads live daemon slots)
pob status [--json]                                                                    # Daemon PID, PoB/PobTools/Data versions, updateStatus, main/ref slots
pob load "<url|code|@path.xml>" [--target main|ref] [--json]                           # XML, Base64, 8 share sites, or poe.ninja char URL
pob save <out.xml> | pob export-code [--target main|ref] [--json]                      # Export XML (CLI disk write, R11) or Base64 code
pob switch-main [--spec S] [--skill-group G] [--skill N] [--part N] [--item-set I] [--skill-set S] [--config-set C] [--loadout L]
pob undo | pob redo | pob stop [--json]

# Read-Only Inspection & Audit
pob summary [--target main|ref] [--json]                                               # Offence (Hit/DoT/FullDPS/Minion*), Defence (EHP/MaxHit/Res), Warnings
pob audit [--target main|ref] [--json]                                                 # Hard gaps + 4-dim water audit (Config/Flasks/Skills/Party -> Pinnacle)
pob config list [--all] [--target main|ref] [--json]                                   # Active (or all) ConfigTab vars + raw customMods
pob items [--slot "Weapon 1"|"#1"] [--all] [--raw] [--target main|ref] [--json]        # Equipped table, single slot, or all equipped + allocated jewels raw
pob skills [--group main|1|"label"] [--json]                                           # Skill groups or per-gem DPS tooltips
pob tree [--search "keyword"|"#1"] [--json]                                            # Keystones/Asc/Masteries/Notables/Jewels or search nodes
pob calc "<keyword>" [--json]                                                          # Bilingual EN/ZH get_calcs + calcs_breakdown formulas/tables

# What-If Simulation (0-residue rollback by default) & Persistent Mutation (--apply)
pob item try [--slot S] (--raw "<text>" | --file f) [--tooltip-only] [--apply]
pob item edit --slot S [--replace-mod <old> <new>]... [--add-mod <ln>]... [--remove-mod <sub >]... [--apply]
pob item db "<query>" [--type UNIQUE|RARE] [--slot S] [--index N] [--equip] [--apply] [--limit 15]
pob item flask <1..5|all> <on|off> [--apply]
pob item anoint [--slot Amulet] --notable "<name>" [--index 1..4] [--apply]
pob skill set --group main [--gem <idx|name>] [--new-gem "<name>"] [--level N] [--quality N] [--enabled true|false] [--count N] [--full-dps true|false] [--apply]
pob skill add --group main --gem "<name>" [--level 20] [--quality 20] [--apply]
pob skill rm --group main --gem <idx|name> [--apply]
pob tree change [--allocate id1,id2:effectId] [--deallocate id3] [--apply]
pob tree set-class [Class] [Ascendancy] [--secondary-asc <Bloodline|None>] [--apply]
pob config set [<var>=<val|reset> ...] [--custom-mods "<text|clear>"] [--append-mods "<text>"] [--apply]
pob batch (--file ops.json | --json-ops '[...]') [--apply]
pob matrix [Original Pinnacle Uber FlasksOff "Label:Pinnacle,k=v"] [-s <spec>]... [--json]  # Atomic multi-scenario comparison

# ROI Ranking, 3-Baseline A/B Comparison & Transplant
pob rank-nodes [--stat CombinedDPS|TotalEHP|Life] [--max-dist 5] [--top 15] [--json]
pob rank-gems [--group main] [--slot N | --add-link] [--field CombinedDPS|FullDPS] [--top 15] [--json]
pob compare [<ref_source>] [--json]                                                    # 3 baselines: Original / Target Env Aligned / Pinnacle Dewatered
pob borrow [--item-slot "Weapon 1,Amulet"] [--skill-group main] [--apply] [--json]

# Local 0ms Game Data (Daemon-Free) & External Economy/PoEDB/Wiki (Cached)
pob data <gem|unique|mod|base|translate> "<query>" [--slot S] [--ilvl N] [--limit 25] [--rebuild] [--json]
pob poedb "<query>" [--mods] [--lang cn|us] [--proxy <url>] [--limit 60] [--json]      # Autocomplete (24h) + page/DropChance/fossil_no (12h)
pob price "<query>" [--league L] [--category all|unique|gem|imbuedgem|...] [--variant S] [--limit 20] [--detailed] [--json]
pob meta [--league L] [--skill S] [--class C] [--top 15] [--json]                      # poe.ninja build-index-state ascendancy/skill shares
pob wiki "<topic>" [--json]                                                            # poewiki.net MediaWiki API (24h cache)
```

## 2. Architecture, Env & Paths

- Engine lookup (`updater.resolve_engine_root`): `POB_ROOT` -> `<POB_RUNTIME_DIR>/engine` (default `F:/poe/.runtime/engine`) -> read-only legacy seed `F:/poe/PobTools[20261102]` (never mutate legacy dir; PowerShell always `-LiteralPath` for `[20261102]`; `cwd` must be `PoE1`).
- Env vars: `POB_ROOT`, `POB_RUNTIME_DIR`, `POB_PROXY` / `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`, `POB_GITHUB_MIRROR`, `POB_OFFLINE=1`, `POB_LEGACY_DIR`.
- Modules (`pob_cli/src/pob_cli/`): `updater.py`, `bridge_client.py`, `daemon.py`, `importers.py`, `snapshot.py`, `analyzer.py`, `mutators.py`, `comparator.py`, `game_data.py`, `external_data.py`, `poedb_client.py`, `formatters.py`, `cli.py`.
- Runtime state (`<POB_RUNTIME_DIR>/`): `daemon.json` (`pid`, `port`, `token`), `update_state.json`, `slot_{main,ref}.json`, `cache/gamedata_<pobVer>_<dataVer>.sqlite`, `cache/ninja_*.json`, `cache/poedb_*.json`.

## 3. Hard Constraints (`R1`..`R12` & v0.2 Contracts)

- Blocked RPCs (`bridge_client.py`): `compare_*` (`R1`: use dual-slot `comparator.py`); `sidebar_breakdown` (`R2`: dynamic — allowed iff runtime `caps.sidebarBreakdown == True`, else use `get_calcs` + `calcs_breakdown`); `config_mod_search`/`config_mod_add` (`R3`: use `list_config`/`set_config`/`set_custom_mods`); `set_pob_options`/`oauth_logout` (`R4`: never mutate `Settings.xml`, always `new_build{}` on boot); `import_from_uri` (`R7`: rejects non-ASCII/parens); `save_build_as` (`R11`: write disk files in `cli.py`).
- No manual XML DPS/EHP (`R5`): all build numbers must come from `pob-zh.exe --engine-headless`.
- External APIs (`R6`/`R7`): never call `www.pathofexile.com/character-window/*` (CF 403) or bypass CF Turnstile on `poe.ninja/poe1/builds/*` HTML; `poe.ninja` char import keeps `Name-1234` hyphen first (`#` retry only on 404) and resolves `overview` from `build-index-state` / `index-state`.
- Engine & updater red lines: never commit 1GB `PobTools` pack; never run standard LuaJIT on PoB `>=2.68` (`+=` syntax fails); never call `api.github.com` (use `releases/latest` `302 Location` tag); never call Lua `UpdateCheck.lua`/`UpdateApply.lua` inside worker; enforce `gate_result` smoke probe + `.bak/` rollback.
- Degraded network warning contract: `check_and_apply_updates` fast-fails (`connect=3s, read=8s`); on `degraded_*`, CLI prints single Chinese alert to `stderr` AND `status`/`summary`/`load` expose top-level `updateWarning` (JSON) + `> [WARN]` (Markdown). If `pob load` fails while degraded, tell user to set proxy and run `pob update --proxy ...` — never blame build code or hand-parse XML (`R5`).
- Reliability & safety (`R8`..`R12`): `BridgeWorker` uses `RLock`, drains `stderr`, heals `import_code` `b:Shutdown()` deadlock (`new_build{}` + restore `last_good_code`) and flags `treeVersion` mismatches (`R8`); strip `^xRRGGBB`/`^0..^9` only in `formatters.py` preserving raw keys (`R9`); loopback `127.0.0.1` + `X-PoB-Token` (`R10`); `tree_click` checks `allocatedNodes` before every click and validates `notableDB` before anoint (`R12`); clear `customMods` with `[{"title":"Default","text":"# disabled","enabled":True}]` and re-apply `presetBossSkills` after `enemyIsBoss`.
