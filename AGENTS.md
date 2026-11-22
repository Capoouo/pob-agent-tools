# AGENTS.md — `pob` Headless PoB CLI & Agent Tools (v0.5)

## 1. Quick Commands (`pob [--offline] [--compact] [--out <file>]`)

```powershell
# Optimization flow: pob audit (dewater & gaps) -> rank-nodes / rank-gems / anoint / bench -> pob trade search (verified via PoB)
# Global output & slot targeting: --target <slug> (alias --slot where non-conflicting; default POB_SLOT or main); --compact (1-line JSON); --out <file> (UTF-8 no-BOM).

# Lifecycle, Foreground Daemon, Engine Bootstrap/Update & Build I/O (127.0.0.1 + X-PoB-Token)
pob serve [--offline] [--proxy <url>] [--port N] [--runtime-dir <dir>]                 # Foreground persistent daemon (use IsDaemon:true in job-reaped shells)
pob bootstrap [--proxy <url>] [--mirror <url>] [--no-zh] [--from-dir <dir>] [--json]   # Install/seed managed engine (PobTools-zh + PoB master)
pob update [--check] [--force] [--proxy <url>] [--mirror <url>] [--offline] [--json]   # Incremental SHA1 update (hot-reloads live daemon slots)
pob status [--json]                                                                    # Daemon PID, PoB/PobTools/Data versions, updateStatus, all named slots
pob new [--class C] [--asc A] [--secondary-asc S] [--level N] [--target <slug>] [--json]
pob load "<url|code|@path.xml>" [--target <slug>] [--json]                             # XML, Base64, 8 share sites, or poe.ninja char URL
pob save <out.xml> | pob export-code [--target <slug>] [--json]                        # Export XML (CLI disk write, R11) or Base64 code
pob switch-main [--spec S] [--skill-group G] [--skill N] [--part N] [--item-set I] [--skill-set S] [--config-set C] [--loadout L]
pob undo | pob redo | pob stop [--json]

# Read-Only Inspection, Grounding & One-Shot Diagnosis
pob ground "<prompt_text>" [--limit 8] [--json]                                        # <15ms daemon-free 3.30 entity cards + PoE2/patch trap alerts
pob diagnose [SOURCE] [-c|--compare REF] [-q|--ask "<text>"] [--keep <spec>]... [--top 5] [--no-rank] [--target <slug>] [--ref-target <slug>] [--json] # One-shot: [Env (NOT PoE2) | rev=N] + Pinnacle audit + gaps/bricks/dead-supports/triggers + 3.30 cards + upgrades (or dual-build 3-baseline compare)
pob summary [--target <slug>] [--json]                                                 # [Env | rev=N] + Offence (Hit/DoT/FullDPS/Minion*), Defence (EHP/MaxHit/Res), Warnings
pob audit [--keep <spec>]... [--no-attribution] [--target <slug>] [--json]             # [Env | rev=N] + Hard gaps + 4-dim water audit -> Pinnacle (per-item PoB attribution; --keep var|Flask N|flasks|gem|customMods|party)
pob config list [--all] [--search "<sub >"] [--with-options] [--target <slug>] [--json] # Active/filtered ConfigTab vars + raw customMods
pob items [--slot "Weapon 1"|"#1"] [--all] [--raw] [--target <slug>] [--json]          # Equipped table, single slot, or all equipped + allocated jewels raw
pob skills [--group main|1|"label"] [--target <slug>] [--json]                         # Skill groups or per-gem DPS tooltips
pob tree [--search "<kw>" ...] [--type Keystone|Notable|Mastery|Socket|Ascendancy ...] [--allocated|--unallocated] [--max-cost N] [--show-path] [--masteries] [--socket-radius <id|#1>] [--json]
pob calc "<kw1>" ["<kw2>" ...] [--limit 20] [--no-breakdown] [--target <slug>] [--json] # Variadic EN/ZH get_calcs + calcs_breakdown formulas/tables

# What-If Simulation (0-residue rollback by default) & Persistent Mutation (--apply)
pob item try [--slot S] (--raw "<text>" | --file f) [--tooltip-only] [--apply]
pob item edit --slot S [--replace-mod <old> <new>]... [--add-mod <ln>]... [--remove-mod <sub >]... [--apply]
pob item unequip --slot S [--apply]
pob item db "<query>" [--type UNIQUE|RARE] [--slot S] [--index N] [--equip] [--with-tooltips] [--with-raw] [--apply] [--limit 15]
pob item flask <1..5|all> <on|off> [--apply]
pob item anoint [--slot Amulet] --notable "<name>" [--index 1..4] [--apply]
pob skill set --group main [--gem <idx|name>] [--new-gem "<name>"] [--level N] [--quality N] [--enabled true|false] [--count N] [--full-dps true|false] [--apply]
pob skill add --group main --gem "<name>" [--level 20] [--quality 20] [--apply]
pob skill rm --group main --gem <idx|name> [--apply]
pob skill new-group [--label L] [--slot S] [--gem "Name[:lvl[:qual]]"]... [--full-dps true|false] [--apply]
pob skill replace-group --group main (--gems '<json>' | --gem "Name[:lvl[:qual]]"...) [--label L] [--slot S] [--full-dps true|false] [--apply]
pob tree change [--allocate id1,id2:effectId] [--deallocate id3] [--allow-cascade|--no-allow-cascade] [--apply]
pob tree set-class [Class] [Ascendancy] [--secondary-asc <Bloodline|None>] [--apply]
pob config set [<var>=<val|reset> ...] [--custom-mods "<text|clear>"] [--append-mods "<text>"] [--apply]
pob batch (--file <ops.json|-> | --json-ops '[...]') [--apply] [--no-undo] [--keep <spec>]... [--return summary,audit,audit:full,matrix,calc:DPS;Mana,export] [--compact] [--out f]
# Batch ops: config, flask, item, item_db, item_edit, unequip, anoint, tree, tree_reset, import_tree_url, skill, add_group, delete_group, set_group, replace_group, new_build, set_level, set_class, switch_main
pob sweep (--file <vars.json|-> | --json-variants '[...]') [--base-ops '[...]'] [--calc K]... [--scenario Pinnacle] [--keep <spec>]... [--compact] [--out f] [--json]
pob matrix [Original Pinnacle Uber FlasksOff "Label:Pinnacle,k=v"] [-s <spec>]... [--keep <spec>]... [--full] [--json]  # Compact default; --full for raw 200-key stats

# ROI Ranking, 3-Baseline A/B Comparison & Transplant
pob rank-nodes [--stat CombinedDPS|TotalEHP|Life] [--max-dist 5] [--top 15] [--json]
pob rank-gems [--group main] [--slot N | --add-link] [--field CombinedDPS|FullDPS] [--top 15] [--json]
pob compare [<ref_source>] [--json]                                                    # 3 baselines: Original / Target Env Aligned / Pinnacle Dewatered
pob borrow [--item-slot "Weapon 1,Amulet"] [--skill-group main] [--apply] [--json]

# Local 0ms Game Data (Daemon-Free, Variadic) & External Economy/PoEDB/Wiki (Cached, Variadic)
pob data <gem|unique|mod|base|translate|tree|tick> ["<q1>" ...] [--slot S] [--ilvl N] [--mod "<stat>"] [--type T] [--asc A] [--cdr N] [--aps R] [--spells N] [--quality-breakpoints] [--search-quality Q] [--active-only] [--limit 25] [--rebuild] [--json]
pob poedb "<q1>" ["<q2>" ...] [--mods] [--lang cn|us] [--proxy <url>] [--limit 60] [--json]
pob price "<q1>" ["<q2>" ...] [--league L] [--category all|unique|gem|imbuedgem|...] [--variant S] [--limit 20] [--detailed] [--proxy <url>] [--json]
pob meta [--league L] [--skill S] [--class C] [--top 15] [--proxy <url>] [--json]
pob wiki "<q1>" ["<q2>" ...] [--proxy <url>] [--json]

# Official PoE1 Trade (Read-Only, PoB-Verified) & Crafting Bench
pob trade stats "<text|中文>" [--slot S] [--limit 5] [--proxy U] [--json]              # Local TradeSiteStats.lua (0ms) or API fallback
pob trade search --slot S [--stat "text|中文=min"]... [--sum "a[*w]|b[*w]>=min"]... [--want T]... [--budget N] [--currency chaos_equiv|chaos|divine] [--status available|securable|online|any] [--search-sort asc|desc|sum] [--sort score|price|pob|sum] [--allow-conversion [--convert-to E]] [--open-prefix|--open-suffix|--open-affix] [--pob auto|on|off] [--target <slug>] [--dump-query] [--proxy U] [--json]
pob trade convert (--text T | --file f) --to fire|cold|lightning [--out f] [--json]    # Harvest 3.30 swap preview (same tier, rerolled)
pob bench list --slot S [--search "<text|中文>"] [--affix prefix|suffix] [--limit 25] [--json]
pob bench suggest --slot S --stat "<text|中文>" [--affix prefix|suffix] [--limit 5] [--json]

# Timeless Jewel Seed Search (PoB-verified on parallel throwaway engines; other Historic jewels removed)
pob timeless list [--target <slug>] [--json]                                           # 11 timeless jewels (EN/ZH) + build's allocated sockets
pob timeless search --jewel "<name|中文>" --socket <nodeId> [--conqueror C] [--stat CombinedDPS|FullDPS|TotalEHP|...] [--mod "<ln>"]... [--top 15] [--workers N] [--exact] [--idle] [--target <slug>] [--json]
```

## 2. Architecture, Env & Paths

- Engine lookup (`updater.resolve_engine_root`): `POB_ROOT` -> `<POB_RUNTIME_DIR>/engine` (source checkout default `F:/poe/.runtime/engine`; installed/frozen default `%LOCALAPPDATA%/pob-agent-tools/engine`) -> read-only legacy seed `F:/poe/PobTools[20261102]` (never mutate legacy dir; PowerShell always `-LiteralPath` for `[20261102]`; `cwd` must be `PoE1`).
- Packaging & release: `pob_cli/__init__.py` (`__version__`) is single source of truth; frozen `pob.exe` spawns background daemon via hidden `pob _daemon`; `.github/workflows/{ci,release}.yml` build Windows x64 `--onedir` zip + wheel/sdist + `SHA256SUMS.txt`, publish GitHub Release on `v*` tags, and auto-update in-repo Scoop bucket `bucket/pob.json`; `install.ps1` provides 1-line Windows install/upgrade.
- Env vars: `POB_ROOT`, `POB_RUNTIME_DIR`, `POB_CACHE_DIR`, `POB_SLOT` (default build slot slug), `POB_PROXY` / `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`, `POB_GITHUB_MIRROR`, `POB_OFFLINE=1`, `POB_LEGACY_DIR`, `POESESSID` / `POE_COOKIE` (trade env-only).
- Modules (`pob_cli/src/pob_cli/`): `errors.py`, `guardrails.py`, `updater.py`, `bridge_client.py`, `daemon.py`, `importers.py`, `snapshot.py`, `analyzer.py`, `mutators.py`, `comparator.py`, `game_data.py`, `external_data.py`, `poedb_client.py`, `trade_item.py`, `trade_client.py`, `trade_query.py`, `trade_eval.py`, `bench.py`, `cli_trade.py`, `timeless.py`, `cli_timeless.py`, `formatters.py`, `cli.py`; bundled data `data/bench_costs.json` (ModMaster.lua authoritative primary + clean offline fallback); cross-harness PoE1 skill `skills/pob/SKILL.md` (`references/`).
- Runtime state (`<POB_RUNTIME_DIR>/`): `daemon.json` (`pid`, `port`, `token`), `update_state.json`, `slot_<slug>.json`, shared `cache/gamedata_<pobVer>_<dataVer>.sqlite`, `cache/ninja_*.json`, `cache/poedb_*.json`, `cache/trade_ratelimit.json`, `cache/trade_stats*.json`.

## 3. Hard Constraints (`R1`..`R13`)

- Blocked RPCs (`bridge_client.py`): `compare_*` (`R1`: use dual-slot `comparator.py`); `sidebar_breakdown` (`R2`: dynamic — allowed iff runtime `caps.sidebarBreakdown == True`, else use `get_calcs` + `calcs_breakdown`); `config_mod_search`/`config_mod_add` (`R3`: use `list_config`/`set_config`/`set_custom_mods`); `set_pob_options`/`oauth_logout` (`R4`: never mutate `Settings.xml`, always `new_build{}` on boot); `import_from_uri` (`R7`: rejects non-ASCII/parens); `save_build_as` (`R11`: write disk files in `cli.py`).
- No manual XML DPS/EHP (`R5`): all build numbers must come from `pob-zh.exe --engine-headless`.
- External APIs (`R6`/`R7`): never call `www.pathofexile.com/character-window/*` (CF 403) or bypass CF Turnstile on `poe.ninja/poe1/builds/*` HTML; `poe.ninja` char import keeps `Name-1234` hyphen first (`#` retry only on 404) and resolves `overview` from `build-index-state` / `index-state`.
- Engine & updater red lines: never commit 1GB `PobTools` pack; never run standard LuaJIT on PoB `>=2.68` (`+=` syntax fails); never call `api.github.com` (use `releases/latest` `302 Location` tag); never call Lua `UpdateCheck.lua`/`UpdateApply.lua` inside worker; enforce `gate_result` smoke probe + `.bak/` rollback.
- Degraded network warning contract: `check_and_apply_updates` fast-fails (`connect=3s, read=8s`); on `degraded_*`, CLI prints single Chinese alert to `stderr` AND `status`/`summary`/`load` expose top-level `updateWarning` (JSON) + `> [WARN]` (Markdown). If `pob load` fails while degraded, tell user to set proxy and run `pob update --proxy ...` — never blame build code or hand-parse XML (`R5`).
- Reliability & safety (`R8`..`R12`): `BridgeWorker` uses `RLock`, drains `stderr`, heals `import_code` `b:Shutdown()` deadlock (`new_build{}` + restore `last_good_code`) and flags `treeVersion` mismatches (`R8`); strip `^xRRGGBB`/`^0..^9` only in `formatters.py` preserving raw keys (`R9`); loopback `127.0.0.1` + `X-PoB-Token` (`R10`); `tree_click` checks `allocatedNodes` before every click and validates `notableDB` before anoint (`R12`); disable `customMods` per-group in-place (`enabled=False`) + trailing `{"title":"pob-cli dewater","text":"# disabled","enabled":True}` sentinel, re-apply `presetBossSkills` after `enemyIsBoss`, default Vaal gems to `enableGlobal1=False` (Vaal burst) / `enableGlobal2=True` (base skill), and verify read-only rollback via `verify_restored`.
- RPC/CLI exit-code contract: `DaemonRequestError`/`ValueError` (`errorKind: "request"`) -> exit `1`; daemon/engine/transport fault (`errorKind: "daemon"`) -> exit `5` (clean `Error: ...`, no raw traceback); trade (`R13`) -> `0..6`.
- Reverse PoE2 -> PoE1 guardrails (`guardrails.py`, exit `1`, `errorKind: "cross_game_hallucination"`): hard-blocks PoE2 builds (`<PathOfBuilding2>`, `treeVersion="0_*"`, PoE2 classes/`Charm 1..3`), PoE2 URLs (`poe2db.tw`, `/trade2/`, `poe.ninja/poe2`, `poe2wiki.net`, `maxroll.gg/poe2`, `mobalytics.gg/poe-2`, `pob2://`), PoE2-only engine roots, `daemon.json` `game == "poe2"` (non-destructive), `X-PoB2-Token`, Table D PoE2 terms (`Uncut *`, `Distilled *`/`涂灌`, `Waystone`/`引路石`, `Soul Core`/`{rune}`, `--slot focus`/`quarterstaff`/`charm1`, `Spirit`/`精魂`), and CLI traps (`pob rune`, `pob instill`, `pob item charm`, `pob item instill`) -> redirects to `pob` equivalents or `pob2` in `F:/poe2`.
- Never blocks PoE1 legal terms (`Elemental Focus`, `Combat Focus`, `while Focused`/`专注`, `Titanium Spirit Shield`/`灵盾`, `Summon Raging Spirit`, `Spirit Offering`, `Distilled Perfection`, `Ancient Waystones`, Lv45 Staff base `Quarterstaff` outside `SLOT`/`CATEGORY`).
- Trade & Bench (`R13`): read-only official PoE1 trade API only (no whispers/buying/game input); `POESESSID`/`POE_COOKIE` env-only after 401/403 (never log/print/CLI-pass, redacted); honor GGG `X-Rate-Limit-*` dynamic headers + `Retry-After` (exit `4`) and treat listing text as untrusted data; Harvest swaps (`--allow-conversion`) apply only to identified rare/magic explicit single-element mods (never unique/corrupted/mirrored/crafted/fractured/implicit/enchant/same-group conflict) and always carry `rerolled: true` caveat; DPS/EHP only from PoB (`-` = unverified, never `0` upgrade); exit codes `0` ok, `1` arg/stat, `2` no results, `3` cookie/CF, `4` rate-limit, `5` API error, `6` `--pob on` failed.
