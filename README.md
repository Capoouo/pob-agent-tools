# pob-agent-tools (`pob`)

**简体中文** | [English](README_EN.md)

面向 AI Agent 与硬核流放之路 1（PoE 1）玩家的**无头 Path of Building（PoB）命令行工具集、双上游自动热更引擎宿主、0ms 离线双语游戏数据库与多槽位常驻守护进程**。

所有伤害（DPS）、有效生命池（TotalEHP）、最大承伤（MaxHit）、天赋收益、装备对比、市集候选升级幅度与永恒珠宝种子排名，**100% 由真实无头 PoB 引擎计算得出**，绝不手算或猜测 XML 词缀数值。

---

## 目录

- [核心特性 (v0.4)](#核心特性-v04)
- [安装与跨平台配置](#安装与跨平台配置)
  - [Windows x64 安装（免 Python 独立版 / Scoop / Python）](#1-windows-x64-安装)
  - [macOS 与 Linux 原生无头环境配置](#2-macos-与-linux-原生无头环境配置)
  - [首次引擎初始化 (`pob bootstrap`) 与环境变量](#3-首次引擎初始化与环境变量)
- [全局参数、多槽位守护进程与构筑读写](#全局参数多槽位守护进程与构筑读写)
- [核心功能详解与实战示例](#核心功能详解与实战示例)
  - [1. 构筑体检、四维去水逐项归因与 `--keep` 条件保留](#1-构筑体检四维去水逐项归因与---keep-条件保留)
  - [2. 多场景矩阵对比 (`pob matrix`)](#2-多场景矩阵对比-pob-matrix)
  - [3. 零残留 What-If 模拟：装备、药剂、涂油、技能、天赋与配置](#3-零残留-what-if-模拟装备药剂涂油技能天赋与配置)
  - [4. 19 操作原子批处理 (`pob batch`) 与多变体横评 (`pob sweep`)](#4-19-操作原子批处理-pob-batch-与多变体横评-pob-sweep)
  - [5. ROI 自动选优、三基准 A/B 对比与跨构筑移植](#5-roi-自动选优三基准-ab-对比与跨构筑移植)
  - [6. 永恒珠宝种子无头实测搜索 (`pob timeless`)](#6-永恒珠宝种子无头实测搜索-pob-timeless)
  - [7. 0ms 离线双语数据库与物价/天梯/编年史/Wiki 查询](#7-0ms-离线双语数据库与物价天梯编年史wiki-查询)
  - [8. 官方市集只读搜装、庄园转抗与工艺台兜底 (`pob trade` / `pob bench`)](#8-官方市集只读搜装庄园转抗与工艺台兜底-pob-trade--pob-bench)
- [退出码契约（脚本与 Agent 自动化）](#退出码契约脚本与-agent-自动化)
- [本地开发、测试与发版](#本地开发测试与发版)
- [致谢与开源协议](#致谢与开源协议)

---

## 核心特性 (v0.4)

- **三端原生无头引擎与双上游增量热更 (`pob bootstrap` / `pob update`)**
  - **Windows x64** 驱动 `PobTools-zh`（`pob-zh.exe --engine-headless`，原生中英双语支持）；**macOS（Apple Silicon / Intel）与 Linux** 通过原生编译 `LuaJIT 2.1 + lua-utf8` 运行 `headless_runner.lua` 复用官方 PoB 与 `bridge.lua`。
  - 守护进程冷启动及每 30 分钟后台检查上游（通过 `302` 重定向解析版本，不撞 `api.github.com` 匿名限流），按官方 `manifest.xml` SHA1 增量拉取变更文件，经 `gate_result` 冒烟门禁验证后无损热重载所有已加载槽位（失败自动回滚 `.bak/`）。
  - GitHub 网络不可达时 3 秒快速降级使用本地引擎，并通过 `stderr` 与 JSON `updateWarning` / Markdown 告警双通道显式提示。
- **多命名槽位常驻守护进程与 `call_batch` 流水线加速**
  - 突破固定双槽限制，通过 `--target <slug>`（或环境变量 `POB_SLOT`）在同一守护进程内并发管理任意命名构筑槽位（如 `main`、`ref`、`pinnacle_test`、`mageblood_test`），各槽位独立维护 30 步 `undo` / `redo` 栈。
  - 全链路引擎 IPC 批处理流水线（`call_batch`）、CLI 全模块懒加载与 `<1ms` 死进程锁清理，热查询毫秒级响应。支持自动后台拉起或 `pob serve` 前台常驻（适配容器与沙盒宿主机）。
- **四维去水逐项实测归因、`--keep` 条件保留与回滚校验 (`pob audit` / `pob matrix`)**
  - 自动剥离构筑中虚高的 **Config 配置项**、**非常驻药剂**（自动识别法师之血保留魔法药剂）、**临时/瓦尔爆发技能**（精准关闭瓦尔爆发 `enableGlobal1`、保留常驻光环/基础主技能并自动切换 `mainActiveSkill`）及 **组队增益**。
  - `customMods` 采用逐组原地禁用 + 哨兵组策略（不破坏原有自定义分组标题与正文），并对每一条被剥离的水分单独回放 PoB 实测重算（回读核实状态生效），精确量化每一项虚高来源的真实 DPS / EHP 占比。
  - 支持通过 `--keep` 保留构筑机制天然常驻的条件（如酋长站定 `conditionStationary`、欺诈师耐力球、单个 `customMods:<分组名>`），内置错词拼写纠错提示（`keepRejected`）与事务结束后的 `verify_restored` 零残留核对。
- **零残留 What-If 模拟、19 操作批处理 (`pob batch`) 与多变体横评 (`pob sweep`)**
  - 所有单步或批量修改默认在内存快照事务内试算并自动无痕回滚，仅当显式传入 `--apply` 时持久化。
  - `pob batch` 单次串联最多 19 类构筑操作并一次性返回指定分析视图；`pob sweep` 在同一热引擎中对 N 套配装/天赋/连法变体做故障隔离横评。
- **永恒珠宝种子无头实测搜索 (`pob timeless list` / `pob timeless search`)**
  - 动态解析 PoB 本地 `Data/Uniques/jewel.lua`，完整覆盖全部 **11 颗**军团与深渊永恒珠宝（中英文名称均可输入）。
  - 对 5 颗平面查表珠宝（致命的骄傲、残酷的约束、军团的信仰、优雅的狂妄、英雄的悲剧）按已分配及装备涂油（`Allocates`）核心天赋字节签名去重，结合对数空间分层采样加性效应预筛（或 `--exact` 全量穷举），跨多进程并行实测，自动摘除其他珠宝孔中的冲突 `Historic` 珠宝。
- **0ms 本地双语数据库 (`pob data`) 与外部生态缓存 (`pob price` / `pob meta` / `pob poedb` / `pob wiki`)**
  - 无需启动无头引擎，15ms 内直接检索本地 SQLite/FTS5 双语索引（宝石、暗金含秽生变体、词缀含工艺/古灵/腐化、底子、中英互译），支持单条命令传入多个查询词并发检索。
- **官方市集只读搜装、庄园转抗与工艺台兜底 (`pob trade` / `pob bench`)**
  - 本地 0ms `TradeSiteStats.lua` 槽位感知双语词缀映射、GGG 响应头动态持久化限流、3.30 一口价 `chaos_equiv` 折算、庄园同阶元素抗性互转冲突校验，以及单快照批量无头 PoB 实测候选装备真实提升。

---

## 安装与跨平台配置

### 1. Windows x64 安装

Windows 独立打包版内置运行环境，**无需安装 Python**。

#### 方式一：PowerShell 一键安装 / 升级（推荐）

```powershell
irm https://raw.githubusercontent.com/exilemoulei/pob-agent-tools/main/install.ps1 | iex
```

若访问 GitHub 需要代理：

```powershell
$env:POB_PROXY = "http://127.0.0.1:7890"
irm https://raw.githubusercontent.com/exilemoulei/pob-agent-tools/main/install.ps1 | iex
```

日后升级只需重新运行同一命令（自动校验 SHA256 并安全停止旧版后台进程）。

#### 方式二：Scoop 包管理器

本仓库内置自动更新的 Scoop 清单（`bucket/pob.json`）：

```powershell
scoop bucket add pob https://github.com/exilemoulei/pob-agent-tools
scoop install pob

# 后续升级：
scoop update pob
```

#### 方式三：GitHub Releases 便携压缩包

前往 [GitHub Releases](https://github.com/exilemoulei/pob-agent-tools/releases/latest) 下载 `pob-v0.4.0-windows-x64.zip`，解压后将包含 `pob.exe` 的目录加入系统 `PATH`。

#### 方式四：Python 包安装（Python 3.11+，`uv` / `pipx` / `pip`）

```powershell
# 使用 uv（推荐）
uv tool install "git+https://github.com/exilemoulei/pob-agent-tools.git@v0.4.0#subdirectory=pob_cli"

# 或直接安装 Release Wheel
pip install "https://github.com/exilemoulei/pob-agent-tools/releases/download/v0.4.0/pob_cli-0.4.0-py3-none-any.whl"
```

---

### 2. macOS 与 Linux 原生无头环境配置

macOS（Apple Silicon `arm64` / Intel `x86_64`）与 Linux（`x86_64` / `aarch64`）需安装 Python 3.11+ 与基础编译工具链（`cc`、`make`、`git`、系统 `zlib` 头文件），编译一次原生 `LuaJIT 2.1 + lua-utf8` 运行时即可：

```bash
git clone https://github.com/exilemoulei/pob-agent-tools.git
cd pob-agent-tools

# 1. 安装 pob CLI
pip install "./pob_cli"

# 2. 编译原生 LuaJIT 2.1 + lua-utf8 运行时并放入托管引擎目录
# macOS 默认运行态目录为 ~/.local/share/pob-agent-tools（或自定义 POB_RUNTIME_DIR）
export POB_RUNTIME_DIR="$HOME/.local/share/pob-agent-tools"
chmod +x pob_cli/packaging/build_posix_runtime.sh
./pob_cli/packaging/build_posix_runtime.sh ./runtime-dist
mkdir -p "$POB_RUNTIME_DIR/engine"
cp -R ./runtime-dist/bin "$POB_RUNTIME_DIR/engine/bin"

# 3. 拉取官方 PoB 数据与 bridge.lua 并完成冒烟校验
pob bootstrap --no-zh
pob status
```

---

### 3. 首次引擎初始化与环境变量

安装 `pob` 后，执行一次 `pob bootstrap` 下载托管无头引擎（或从已有的本地 `PobTools` 目录离线导入）：

```powershell
pob --version
pob bootstrap                                   # 直连 GitHub 下载 PobTools-zh + 官方 PoB
pob bootstrap --proxy http://127.0.0.1:7890     # 通过 HTTP / SOCKS5 代理下载
pob bootstrap --from-dir "D:\PoE\PobTools"      # 从本地已有 PobTools 目录离线导入种子
pob update --check                              # 检查上游是否有新版本
pob update --proxy http://127.0.0.1:7890        # 增量热更并自动刷新守护进程所有槽位
```

常用环境变量一览：

| 环境变量 | 说明 |
|---|---|
| `POB_ROOT` | 显式指定无头引擎根目录（默认自动查找 `<POB_RUNTIME_DIR>/engine`） |
| `POB_RUNTIME_DIR` | 守护进程状态、引擎与缓存根目录（安装版默认 `%LOCALAPPDATA%\pob-agent-tools`，源码检出默认 `<repo>/.runtime`） |
| `POB_CACHE_DIR` | 覆盖本地 SQLite 游戏数据库及外部 API 缓存目录 |
| `POB_SLOT` | 默认目标构筑槽位名（未传 `--target` 时生效，默认为 `main`） |
| `POB_PROXY` | 网络请求代理（支持 `http://`、`https://`、`socks5://`；亦兼容 `HTTPS_PROXY` / `ALL_PROXY`） |
| `POB_GITHUB_MIRROR` | GitHub 下载镜像前缀（加速 `bootstrap` / `update`） |
| `POB_OFFLINE` | 设为 `1` 时跳过启动期与定时 GitHub 更新检查（等价于全局 `--offline`） |
| `POESESSID` / `POE_COOKIE` | 仅在官方市集接口返回 401/403 时按需提供，严禁写入命令行参数或日志 |

---

## 全局参数、多槽位守护进程与构筑读写

### 全局输出与槽位控制

所有子命令统一支持结构化 JSON 输出、单行压缩输出与 UTF-8 无 BOM 直写文件（彻底规避 Windows PowerShell 重定向编码损坏）：

```powershell
# 顶层全局参数（可放在 pob 之后，绝大多数子命令也直接支持同名局部参数）
pob --offline --compact --out result.json summary --json

# 查看守护进程 PID、PoB / PobTools 版本、更新状态及当前所有已加载槽位
pob status
pob status --json
```

### 前台守护进程模式 (`pob serve`)

常规终端下执行任意构筑命令会自动在后台拉起守护进程（监听 `127.0.0.1` 随机端口 + `X-PoB-Token` 鉴权）。若运行在每次命令结束后会清理后台子进程的 AI Agent 沙盒宿主中，可先在后台任务槽位以前台模式常驻运行 `pob serve`：

```powershell
# 在独立后台常驻会话中启动前台守护进程
pob serve --offline

# 停止守护进程并释放全部无头引擎内存
pob stop
```

### 多源构筑加载、新建、多槽位管理与导出

`pob load` 支持本地 `.xml` / `.txt`（可直接传路径或 `@path`）、原始 Base64 分享码、8 大分享站链接（`pobb.in`、`poedb.tw/pob/...`、`pastebin.com`、`poe.ninja/pob/...`、`maxroll.gg` 等）以及 `poe.ninja` 角色主页链接：

```powershell
# 1. 加载构筑到默认 main 槽位
pob load "https://poedb.tw/pob/AWqOLSSmyG"
pob load "@D:\builds\my_chieftain.xml"
pob load "https://poe.ninja/poe1/builds/mirage/character/AccountName-1234/CharName"

# 2. 使用 --target 管理多个独立构筑槽位（如对比槽 ref、实验槽 exp1）
pob load "https://pobb.in/xxxxxx" --target ref
pob new --class Marauder --asc Chieftain --level 95 --target exp1

# 3. 切换同一构筑内的天赋树阶段 (Spec)、装备集 (ItemSet)、技能集或同步 Loadout
pob switch-main --loadout "Endgame Mageblood"
pob switch-main --spec 2 --item-set 2 --skill-group 1

# 4. 导出本地 XML 或 PoB Base64 分享码
pob save "D:\builds\optimized.xml"
pob export-code --target exp1
```

---

## 核心功能详解与实战示例

### 1. 构筑体检、四维去水逐项归因与 `--keep` 条件保留

拿到一份外部 PoB 时，先跑 `pob summary` 看原始面板，再跑 `pob audit` 做抗性/命中/法压/防御硬缺口体检与**四维去水实测归因**：

```powershell
# 基础攻防与召唤物摘要 + PoB 原生告警
pob summary

# 完整去水审计 + 逐项无头 PoB 实测归因（测量每个被剥离项单独恢复时的真实 ΔDPS / ΔEHP）
pob audit

# 快速去水体检（跳过逐项归因重算，适合高频自动化循环）
pob audit --no-attribution
```

当构筑机制本身**确定能常驻满足**某些条件（例如酋长升华站定、被击中得耐力球、点燃构筑敌人常驻点燃、某组真实生效的自定义词缀）时，用 `--keep` 明确保留，不被当成水分剥离：

```powershell
# 保留多个配置项、指定药剂槽、整套药剂、特定技能或单个 customMods 分组
pob audit --keep conditionStationary,useEnduranceCharges,conditionEnemyIgnited
pob audit --keep "Flask 1,Vaal Haste,customMods:Boss Setup"

# 若拼写错误（如把 conditionStationary 写成 conditionStationery），keepRejected 会自动给出拼写建议并告警
pob audit --keep conditionStationery --no-attribution
```

细粒度只读透视命令（装备、技能、天赋树、配置、公式拆解）：

```powershell
# 查看当前激活的配置项，或搜索可选下拉值（--with-options）
pob config list
pob config list --all --search "boss" --with-options

# 查看全身装备摘要、单槽详情，或一次性导出全身装备+已镶嵌珠宝的原始文本
pob items
pob items --slot "Weapon 1" --raw
pob items --all --raw

# 查看全部技能组或主技能组每颗宝石的详细面板与辅助收益提示
pob skills
pob skills --group main

# 搜索天赋节点（支持中英文词条、最短过路点上限、专精列表、珠宝孔半径内节点）
pob tree --search "最大生命" "法术压制" --unallocated --max-cost 4 --show-path
pob tree --type Mastery --allocated --masteries
pob tree --socket-radius 61419

# 多关键词并发查询 PoB Calcs 计算公式与数值来源拆解表（中英文皆可）
pob calc "Average Hit" "Spell Suppression" "Mana Regen"
```

---

### 2. 多场景矩阵对比 (`pob matrix`)

一次调用在同一事务内横向对比构筑在 **原配置 (`Original`)**、**去水 84 巅峰首领 (`Pinnacle`)**、**去水 85 超级首领 (`Uber`)**、**断药 (`FlasksOff`)** 以及**自定义覆盖场景**下的真实表现，结束后自动校验零残留还原：

```powershell
# 默认四场景对比（Original / Pinnacle / Uber / FlasksOff）
pob matrix

# 在 Pinnacle/Uber 场景中保留站定与药剂，并追加自定义满层枯萎与关熔岩护盾场景
pob matrix Original Pinnacle Uber `
  -s "Wither15:Pinnacle,MultiplierWitheredStackCount=15" `
  -s "NoGuard:Pinnacle,conditionMoltenShell=false" `
  --keep conditionStationary,flasks

# 输出完整 200+ 原始统计键 JSON（默认返回精简高信噪比指标）
pob matrix Original Pinnacle --full --json --out matrix_full.json
```

---

### 3. 零残留 What-If 模拟：装备、药剂、涂油、技能、天赋与配置

**默认所有变更命令均为零残留模拟（只看前后差值，不改构筑）**；确认有效后加 `--apply` 才会落盘，且随时可用 `pob undo` / `pob redo` 撤销或重做。

```powershell
# --- 装备、数据库暗金、词缀编辑、药剂开关与项链涂油 ---
# 粘贴游戏内 Ctrl+C 的物品文本试穿对比
pob item try --slot "Amulet" --file .\candidate_amulet.txt

# 直接在现有装备上替换/新增/删除词缀试算
pob item edit --slot "Ring 1" `
  --replace-mod "Cold Resistance" "+48% to Lightning Resistance" `
  --add-mod "+55 to maximum Life"

# 搜索 PoB 内置暗金库并试穿（支持中英文）
pob item db "法师之血" --slot Belt --equip
pob item flask all off

# 调用 PoB 原生涂油弹窗模拟项链涂油（自动校验 notableDB 合法性）
pob item anoint --slot Amulet --notable "Whispers of Doom"

# --- 技能宝石增删改与整组替换 ---
# 新增宝石（若是瓦尔宝石，默认关闭瓦尔爆发 enableGlobal1、开启基础技能 enableGlobal2）
pob skill add --group main --gem "Awakened Elemental Focus Support" --level 5 --quality 20
pob skill set --group main --gem 1 --level 21 --quality 20
pob skill new-group --label "Aura" --slot "Helmet" `
  --gem "Determination:21:0" --gem "Grace:21:0" --gem "Enlighten Support:4:0"
pob skill replace-group --group main `
  --gem "Ice Nova of Frostbolts:21:20" `
  --gem "Archmage Support:20:20" `
  --gem "Spell Echo Support:20:20"

# --- 天赋点分配（含专精 effectId）与职业/升华/副升华切换 ---
pob tree change --allocate "26725,49080:64315" --deallocate "12345"
pob tree set-class Witch Occultist --secondary-asc None

# --- 配置项写入（兼容旧首领别名、下拉 None 选项及不覆盖追加 customMods） ---
pob config set enemyIsBoss=Pinnacle conditionStationary=true --append-mods "+15% more Damage"

# 确认改动落盘与撤销/重做
pob item anoint --slot Amulet --notable "Charisma" --apply
pob undo
pob redo
```

---

### 4. 19 操作原子批处理 (`pob batch`) 与多变体横评 (`pob sweep`)

#### `pob batch`：单事务组合复杂改动 + 自定义附带视图

支持 **19 种操作类型**：`config`、`flask`、`item`、`item_db`、`item_edit`、`unequip`、`anoint`、`tree`、`tree_reset`、`import_tree_url`、`skill`、`add_group`、`delete_group`、`set_group`、`replace_group`、`new_build`、`set_level`、`set_class`、`switch_main`。

```powershell
# 一次快照事务内同时换上猎首、涂油魅力、点亮天赋并直接返回 summary + 去水 audit + matrix
pob batch --json-ops '[
  {"op": "item_db", "query": "Headhunter", "slot": "Belt"},
  {"op": "anoint", "slot": "Amulet", "notable": "Charisma"},
  {"op": "config", "set": {"enemyIsBoss": "Pinnacle", "conditionStationary": true}}
]' --keep conditionStationary --return "summary,audit,matrix,calc:DPS;Mana"
```

需要含逐项归因的完整审计时，可将 `--return` 中的 `audit` 写为 `audit:full`；落盘大批量生成构筑时可加 `--apply --no-undo` 省去撤销栈快照开销。

#### `pob sweep`：在同一热引擎中批量横评 N 套方案

适合对比多套配装、不同涂油、不同辅助宝石组合或不同升华路线的真实 Pinnacle 表现（单变体报错自动隔离，不影响其余变体）：

```powershell
pob sweep --scenario Pinnacle --keep conditionStationary --calc "Mana" --json-variants '[
  {"name": "Baseline", "ops": []},
  {"name": "Anoint_Whispers", "ops": [{"op": "anoint", "slot": "Amulet", "notable": "Whispers of Doom"}]},
  {"name": "Anoint_Sovereignty", "ops": [{"op": "anoint", "slot": "Amulet", "notable": "Sovereignty"}]},
  {"name": "Mageblood_Swap", "ops": [{"op": "item_db", "query": "Mageblood", "slot": "Belt"}]}
]'
```

---

### 5. ROI 自动选优、三基准 A/B 对比与跨构筑移植

```powershell
# 1. 按每点天赋过路成本自动排行未点亮节点（DPS / TotalEHP / Life）
pob rank-nodes --stat CombinedDPS --max-dist 5 --top 15
pob rank-nodes --stat TotalEHP --max-dist 4 --top 10

# 2. 为指定技能组自动排行最佳替换宝石或第 6 连新增辅助（--add-link）
pob rank-gems --group main --slot 5 --field FullDPS --top 10
pob rank-gems --group main --add-link --field CombinedDPS --top 10

# 3. 三基准 A/B 构筑对比（原配置 vs 对齐主槽环境 vs 统一去水 Pinnacle）
pob compare "https://pobb.in/ReferenceBuildCode"

# 4. 从 ref 槽位一键移植指定部位装备与主技能组到 main 槽位试算真实收益
pob borrow --item-slot "Weapon 1,Amulet,Ring 1" --skill-group main
```

---

### 6. 永恒珠宝种子无头实测搜索 (`pob timeless`)

PoB 自带的永恒珠宝查找器使用固定加性权重打分，无法扣减被替换核心天赋的原有收益。`pob timeless` 将每颗候选种子**真实插入无头 PoB 重算面板**，支持全部 11 颗军团与深渊永恒珠宝：

```powershell
# 1. 查看 PoB 支持的 11 颗永恒珠宝（中英文名称）及当前构筑已点亮的有效珠宝孔 ID（<100ms）
pob timeless list

# 2. 搜索指定插槽的最佳种子（支持中文或英文珠宝名，自动移除其他孔内冲突的史实珠宝）
pob timeless search `
  --jewel "致命的骄傲" `
  --socket 61419 `
  --conqueror Kaom `
  --stat CombinedDPS `
  --top 15 `
  --workers 4 `
  --idle

# 3. 针对能量护盾/结界/总有效生命池搜索，并附带额外珠宝词缀
pob timeless search `
  --jewel "Heroic Tragedy" `
  --socket 26196 `
  --conqueror Vorana `
  --stat Ward `
  --mod "+10 to all Attributes" `
  --top 10

# 4. 跳过分层加性预筛，对全部去重种子组做 100% 精确穷举（--exact）
pob timeless search --jewel "Militant Faith" --socket 41263 --stat TotalEHP --exact --workers 4
```

> **性能与内存说明**：每个并行无头引擎工作进程约占用 1.3–1.7 GB 内存，`--workers` 默认取 CPU 物理/逻辑核心数一半且上限为 4；后台跑大范围搜索时建议加上 `--idle` 降低子进程 CPU 优先级。

---

### 7. 0ms 离线双语数据库与物价/天梯/编年史/Wiki 查询

所有数据查询命令均支持**单次传入多个关键词**并发检索：

```powershell
# --- 本地 0ms 离线游戏数据库（无需启动 PoB 守护进程） ---
pob data gem "Ice Nova of Frostbolts" "Archmage Support"
pob data unique "Mageblood" "Headhunter"
pob data mod "Spell Suppression" --slot Helmet --ilvl 85
pob data base "Spirit Shield" --limit 10
pob data translate "喋血之刃" "Whispers of Doom"

# --- 流亡编年史 (poedb.tw) 词缀池、权重与化石标签 ---
pob poedb "傲视头冠" --mods --lang cn
pob poedb " Hubris Circlet" --mods --lang us

# --- poe.ninja 实时物价（52 分类，15 分钟本地缓存）与天梯构筑分布 ---
pob price "Mageblood" "Progenesis" "Nimis"
pob price "Forbidden Flame" --variant "Hierophant" --limit 30
pob price "Awakened Spell Echo Support" --detailed
pob meta --skill "Ice Nova of Frostbolts" --top 15

# --- poewiki.net 机制词条查询 ---
pob wiki "Eldritch Battery" "Leech"
```

---

### 8. 官方市集只读搜装、庄园转抗与工艺台兜底 (`pob trade` / `pob bench`)

推荐优化闭环：先跑 `pob audit` 定位真实抗性/属性缺口 -> 查工艺台 `pob bench` 低成本补齐方案 -> 再跑 `pob trade search` 搜一口价装备并由本地无头 PoB 批量实测真实 DPS / EHP 提升。详见 [docs/TRADE.md](docs/TRADE.md)。

```powershell
# 1. 本地 0ms 解析词缀对应的官方市集 Stat ID（自动按武器/防具区分 Local 与全局词缀）
pob trade stats "攻击速度" --slot weapon1
pob trade stats "法术压制率" --slot gloves

# 2. 官方市集只读搜装 + 庄园同阶异抗自动展开（--allow-conversion）+ 无头 PoB 实测排序
pob trade search --slot gloves --budget 25 --currency chaos_equiv `
  --stat "冰霜抗性=40" `
  --stat "最大生命=70" `
  --want "Attack Speed" `
  --allow-conversion `
  --sort pob --pob auto

# 3. 搜索带空余前/后缀的底子，配合工艺台附魔补齐缺口
pob trade search --slot ring1 --budget 15 --stat "最大生命=65" --open-suffix --pob auto
pob bench list --slot ring1 --affix suffix --search "抗性"
pob bench suggest --slot gloves --stat "冰霜抗性"

# 4. 离线预览庄园元素抗性互转后的物品文本（保留附魔卷随机数值提示）
pob trade convert --file .\dropped_gloves.txt --to lightning
```

---

## 退出码契约（脚本与 Agent 自动化）

所有 CLI 命令均捕获异常并输出简洁错误提示，**绝不向终端泄漏裸 Python Traceback**。

| 命令范围 | 退出码 | 含义与自动化处理建议 |
|---|---|---|
| **常规与构筑命令** | `0` | 执行成功 |
| | `1` | **请求被拒绝或参数/状态非法**（如引擎未 `bootstrap`、槽位无装备、Loadout 不存在、分享码损坏、珠宝孔未点亮）—— 修正命令参数或先加载构筑 |
| | `5` | **后台守护进程、无头引擎或网络上游故障** —— 按提示执行 `pob stop` 后重试，或检查运行态目录 `daemon.log` |
| **`pob trade` 系列** | `0` | 搜索或转换成功 |
| | `1` | 参数或词缀过滤条件无法映射（附相近词缀建议） |
| | `2` | 市集无匹配在售结果（放宽数值门槛或预算） |
| | `3` | 官方市集返回 401/403 或 Cloudflare 拦截（按需设置环境变量 `POESESSID`） |
| | `4` | 触发 GGG 市集速率限制（已解析 `Retry-After`，等待冷却后重试） |
| | `5` | 官方市集上游 API 错误或网络超时 |
| | `6` | 显式指定 `--pob on` 时本地无头 PoB 实测失败 |

---

## 本地开发、测试与发版

```powershell
# 本地可编辑模式安装开发依赖
pip install -e "./pob_cli[dev]"

# 运行全量单元与无头集成测试（离线模式可设 $env:POB_OFFLINE="1"）
python -m pytest pob_cli/tests -v

# 本地构建 Windows x64 独立单目录可执行包（dist/pob/pob.exe）
pyinstaller --noconfirm --clean pob_cli/packaging/pob.spec
```

**Tag 驱动自动发版流水线**（`.github/workflows/release.yml`）：
1. 修改 `pob_cli/src/pob_cli/__init__.py` 中的 `__version__`（单一版本真值源）并合入 `main`。
2. 创建并推送版本标签：`git tag -s v0.4.0 -m "pob-agent-tools v0.4.0" && git push origin main v0.4.0`。
3. GitHub Actions 自动完成版本号核对、全量单测、构建并冒烟测试 `pob-vX.Y.Z-windows-x64.zip` + Wheel/sdist + `SHA256SUMS.txt`、发布 GitHub Release，并将最新 SHA256 自动回写至 `bucket/pob.json` 供 Scoop 升级。

查阅极简高密度 Agent 速查表请见 [AGENTS.md](AGENTS.md)。

---

## 致谢与开源协议

### 上游项目与灵感来源

- 架构灵感源自 [**ianderse/pob-mcp**](https://github.com/ianderse/pob-mcp)（GPL-3.0）与 [**shalayiding/POEMCP**](https://github.com/shalayiding/POEMCP)。
- 无头桥接协议与中文引擎宿主由 [**Hsiung-Shao/PobTools-zh**](https://github.com/Hsiung-Shao/PobTools-zh)（`pob-zh.exe --engine-headless` + `bridge.lua`）驱动，核心构筑计算与游戏数据来自 [**PathOfBuildingCommunity/PathOfBuilding**](https://github.com/PathOfBuildingCommunity/PathOfBuilding)。

### 社区贡献致谢

- **[@ChangMenQAQ](https://github.com/ChangMenQAQ)**（[PR #1](https://github.com/exilemoulei/pob-agent-tools/pull/1)）—— 设计并开拓官方市集只读搜装、庄园同阶转抗与工艺台兜底升级工作流。
- **[@restartgr](https://github.com/restartgr)**（[PR #2](https://github.com/exilemoulei/pob-agent-tools/pull/2)、[PR #4](https://github.com/exilemoulei/pob-agent-tools/pull/4)）—— 设计并实现四维去水逐项 PoB 实测归因、`--keep` 条件保留、`verify_restored` 回滚核对，以及请求拒绝与守护进程故障分离的清晰错误退出码机制。
- **[@Capoouo](https://github.com/Capoouo)**（[PR #3](https://github.com/exilemoulei/pob-agent-tools/pull/3)）—— 设计并实现基于无头 PoB 实测的永恒珠宝种子搜索（`pob timeless list|search`）、核心天赋签名分组去重与分层对数空间预筛算法。

### 开源协议

本项目基于 **GNU General Public License v3.0 (GPL-3.0)** 开源 —— 详见 [LICENSE](LICENSE)。
