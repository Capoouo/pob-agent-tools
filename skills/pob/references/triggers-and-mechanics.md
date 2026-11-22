# PoE1 3.30 Triggers, `0.033s` Server Ticks & Build-Brick Mechanics

All values verified against live PoE1 3.30 `PoE1/Data/` (`Gems.lua`, `Uniques/`, `TreeData/3_30/tree.lua`). Always run `pob data tick <base_cd|preset> [--cdr N] [--aps R]` instead of doing mental `1/30` math.

## 1. PoE1 Server Tick Formula (`0.033s = 33ms`, NOT `33.33ms`)
- `raw_cd = base_cd / (1 + total_cdr / 100)` -> `ticks = ceil(raw_cd / 0.033)` -> `effective_cd = ticks * 0.033s` -> `max_triggers_per_sec = 1 / effective_cd`.
- **CoC (`0.15s` base) Breakpoints** (single triggered spell; multiply APS cap by `N` for `N` distinct alternating spells):
  - `0%` CDR -> `5t` (`165ms`, cap **`6.06/s`**) | **`14%`** CDR -> `4t` (`132ms`, cap **`7.58/s`**) | **`52%`** CDR -> `3t` (`99ms`, cap **`10.10/s`**) | **`128%`** CDR -> `2t` (`66ms`, cap **`15.15/s`**).
- **Awakened CoC Gem CDR**: grants `10 + 3 * (level - 1)%` spell CDR (`Lv5 = 22%`, `Lv6 = 25%`, `Lv8 = 31%`), additive with gear/belt/boots/abyss CDR.
- **APS Overspeed Halving**: If trigger attack APS exceeds `max_triggers_per_sec * N` (even slightly, e.g., `8.5/s` vs `7.58/s` at 14% CDR), hits land inside cooldown and **HALVE** real trigger rate! Fix: drop APS `<= cap` OR hit the next CDR tick breakpoint (`52%`).

## 2. Fixed-Interval Channelling (`CwC`) vs Cooldown Triggers
| Trigger Source | Mechanism | 3.30 Base Value | CDR Scales It? |
|---|---|---|---|
| `Cast while Channelling` (`吟唱时施放`) | Fixed channelling interval | `0.32s` (`320ms`) at Lv19–21 (`0.30s` at Lv24–26) | **NO — 0% effect!** Only gem level scales it. |
| `Awakened Cast While Channelling` | Fixed channelling interval | `0.30s` (`300ms`) at Lv5–6 (+100% avoid stun) | **NO — 0% effect!** |
| `Cast On Critical Strike` / `Awakened CoC` | Cooldown (`0.033s` tick) | `0.15s` (`150ms`) | **YES** (Global CDR + Awakened CoC gem CDR) |
| `Cospri's Malice` / `Mjölner` / `The Poet's Pen` / `CwDT` | Cooldown (`0.033s` tick) | `0.25s` (`250ms`) -> 0%/9%/27%/52% | **YES** (`8t=264ms`, `7t=231ms`, `6t=198ms`, `5t=165ms`) |
| `The Sword of Ages` (`Temporal Mirage`) / `The Hidden Blade` | Cooldown (`0.033s` tick) | `0.50s` (`500ms`) -> 0%/2%/10%/19%/27%/38%/52%/69% | **YES** (`Mirage` skips Triggerbots `-30%`; Cyclone triggers ONCE on start) |
| `Spellslinger` / `Kitava's Thirst` | Cooldown (`0.033s` tick) | `0.60s` / `0.10s` (`Kitava` 50% proc on >=100 Mana) | **YES** |

## 3. Universal Defensive, Sustain & Support-Gem Bricks (Checked by `pob diagnose`)
- **Low-Life Chaos Bypass & `Esh's Visage` (`艾许之面`)**: `Esh's Visage` states *"Chaos Damage taken does not bypass ES **while NOT on Low Life**"*. Reserving `>=50%` Life (`LifeUnreserved <= 50%`) **DEACTIVATES** it completely — Chaos bypasses ES and one-shots Life! Safe Low-Life options (`pob data unique --mod "Chaos Damage taken does not bypass"`): `Shavronne's Wrappings`, `Solaris Lorica`, `The Ivory Tower` (needs unreserved Mana!), `Coruscating Elixir` (needs 100% uptime).
- **Fatal Keystone / Jewel Bricks**: `Eldritch Battery + Chaos Inoculation` (`1` Life + `0` ES on Life = instant death); `Dissolution of the Flesh` (`血肉溶解`) + `Righteous Fire` / `Rathpith Globe` / `Sacrifice Support` / `Lifetap` (constant Life loss/sacrifice prevents 2s unreservation).
- **Dead / Anti-Synergy Supports**: `Inspiration` in a `0` Mana Cost group (generates `0` charges in live play — PoB's assumed max charges are fake water!); `Elemental Focus` linked with `Hypothermia` / `Immolate` / `Combustion` without another ailment source; `Brutality` linked with Elemental/Chaos supports or `Hatred`/`Anger`/`Wrath`; `Void Manipulation` + Elemental supports; `Controlled Destruction` (`80% less Crit`) on CoC/Crit builds.
- **Crit Attack Double Accuracy Roll**: Attacks roll Hit Chance **TWICE** for crits (once to hit, once to confirm crit); `<99%` Hit Chance severely penalizes real Crit DPS and CoC trigger rate.
- **3.30 Scope Rules**: `+% to Quality of all Skill Gems` applies ONLY to **Active Skill Gems** (NOT Supports); **Tinctures != Flasks** (`Bloodsoaked Blade` sustains Tinctures with `Screams of the Desiccated`).
