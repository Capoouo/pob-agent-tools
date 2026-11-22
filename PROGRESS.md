# PROGRESS

## ACTIVE
- 目标：推送 Windows CI 修复与 v0.6.0 正式发版标签并验收 GitHub Actions CI + Release 全绿
- 已做：[x] 完成 pob_cli 5层严格单向 DAG 重构、Ruff + check.py 三模门禁落地及 3 路 DeepSeek V4.1F 实战验收（a5f100c）；[x] 修复 GitHub Actions windows-latest 上的 CRLF 字节膨胀（.gitattributes *.py eol=lf + 拆薄 parsers_special/batch_ops_skills/mutation_md + arch_lint 按最坏 CRLF 字节上限校验 <=24000B）、cp1252 控制台编码报错及 pwsh 退出码检查，升级版本号至 v0.6.0；[x] 编写重构后 Hook 防假执行硬护栏施工方案 docs/HOOK_GUARDRAILS_PLAN.md
- 下一步：[ ] 经用户确认后执行 git push origin main 与 v0.6.0 tag 推送，监控 CI 与 Release 全绿及 Scoop 清单自动更新，随后按 docs/HOOK_GUARDRAILS_PLAN.md 落地 pob hook
- 验证：python pob_cli/scripts/check.py --fast (max CRLF=23768B) && PYTHONIOENCODING=cp1252 python pob_cli/scripts/check.py --self-test (5/5 PASS) && pytest 78+ passed && pob --version (0.6.0)（2026-11-23 03:08）

## LOG
### 2026-11-23 修复 Windows CI CRLF 字节膨胀与 cp1252 编码报错（.gitattributes + 3 子模块拆薄 + arch_lint 最坏 CRLF 字节校验 <=23768B + check.py UTF-8 加固）并准备 pob4agents v0.6.0 发版 → cp1252 --self-test (5/5) + --fast + 回归单测全绿
### 2026-11-23 通过 Paseo 拉起 3 路 DeepSeek V4.1 Flash 子代理完成新架构实战验收（3/3 主动全文 Read 模块，捕获并修复 pyproject.toml known-first-party 排序差异，新增 test_ticks_unit.py 17 用例） → 双目录 check.py --fast + --self-test (5/5) + 902+5 passed 全绿
### 2026-11-23 完成 pob_cli 5层严格单向 DAG 重构（149受检文件全部<=641L/<=23995B，避开24KB钩子墙）并接入 Ruff + check.py 三模门禁与 CI/pob.spec → check.py --self-test (5/5) + --fast (0.97s) + 全量 pytest 890 passed (1 skipped) + 实机 daemon/wheel 全绿
### 2026-11-23 编写重构后 Hook 硬护栏防假执行施工方案 docs/HOOK_GUARDRAILS_PLAN.md（三道闸门+进程外回执+否定语境豁免+自动代跑塞真值） → 未改重构中源码
### 2026-11-22 完成 DSH 3/3 实机验收（H01/H02/H03 全 PASS）并落地二阶精修（Ramako 固定 -20% DoT 抗、Frost Bomb 禁回血、幽魂藏身处施法、diagnose 展示 Pantheon、PowerShell --return 空格容错、SKILL 步数纪律） → pytest 531/207 passed + 实机 smoke 全绿
### 2026-11-22 Swarm 3 路 + 文档落地 pob4agents 3.28–3.30 防幻觉与构筑守卫全栈加固（退场觉醒过滤/卓越上限硬拦截/台服繁简映射/冷却提取/CWS零盾与多里亚尼等8项诊断守卫/rank-nodes双修/Skill同步） → 全量 pytest 883 passed (1 skipped) + 最终复检 393 passed + 3 套真实 PoB 实机验收全绿
### 2026-11-22 完成项目改名 pob-agent-tools -> pob4agents（b7df897 + 合并 3dc7128 -> 0035f32），同步重命名 GitHub 仓库为 exilemoulei/pob4agents 并推送 origin/main → PYTHONPATH=F:/poe/pob_cli/src pytest 228 passed (1 skipped) + git push exit 0
### 2026-11-22 将项目从 pob-agent-tools 重命名为 pob4agents（15 文件 + 旧运行态目录/PATH/缓存平滑兼容）并提交本地 main → PYTHONPATH=F:/poe/pob_cli/src pytest 228 passed (1 skipped) + install.ps1/JSON 校验通过
### 2026-11-22 完成 PR #5 合并适配（d627bc5）并经确认推送 origin/main，PR #5 已自动 MERGED → pytest 全量通过 + git push origin main exit 0
### 2026-11-22 本地合并 PR #5（d627bc5，pob item inspect + --bench-craft 交易试算），解决 3 处与 v0.5.0 冲突并补齐 PoE2 跨代护栏、pob.spec 打包项、无引擎容错与 skills/pob 索引 → pytest 全量 682+72 回归全绿（待确认是否推送 origin/main）
### 2026-11-22 修复无引擎时 pob ground 崩溃（c859d5e）并完成 pob-agent-tools v0.5.0 正式发版（Win/Linux/macOS CI + Release + Scoop 清单全绿） → pytest 662 passed + CI #45018335612 & Release #45018339764 success
### 2026-11-22 同步合并 origin/main (2abee10, PR #6 中英双语 README) 并准备 pob-agent-tools v0.5.0 发版（融合通用认知卸载 CLI 原语、反向跨代/补丁防幻觉护栏与跨宿主泛用 skills/pob） → pytest 662 passed + Wheel/AST/YAML/Spec 门禁全绿
### 2026-11-22 泛化落地 pob_cli 全流派认知卸载层（diagnose -c/-q、引擎0%死链与Caveat、CoC超速/CwC非冷却/0.033s data tick、低血艾许之面/EB+CI探针、unique --mod、含永恒大点的 data tree、双段宝石合并、rev近因钢印）与 62 行跨宿主通用 SKILL.md → pytest 662 passed + 纯 Skill 模式 G01..G04 新流派盲测 4/4 全绿
### 2026-11-22 打通 F:/dsh -> Magpie (opencode-go/deepseek-v4.1-flash)，提取 6 道玩家历史真题 (R01..R06) 拷打验收并经 Neon Swarm 全栈加固（一站式 pob diagnose + 0.033s 帧率器 + 喋血之刃保酊剂 + 0ms 品质台阶检索 + dsh-pob-assistant Markdown/timeless/sum/compare_with） → pytest 650 passed + 二轮真机 6/6 全过（步数 -82%、pwsh 144->0、耗时 -68%）
### 2026-11-22 落地弱模型 P0/P1 防幻觉优化（pob ground + pob diagnose + 同名异义/3.23-3.25 补丁陷阱 + 黑话/模糊兜底 + 极简 SKILL.md）与 F:/dsh 原生 Cordis 插件测试台，并通过 Paseo 内 pi/deepseek-v4.1-flash 两轮真机压测捕获修完 7 处实战坑 → pytest 640 passed (0 failed) + DSH verify-plugin & 8/8 ground 全绿
### 2026-11-21 落地 Lore 建仓自动 link（routes.json 回退 + lore link --auto + git-template 钩子）与 AGY lore_record 写入工具并双机同步（moule-host + tui7），同时挂载 F:/poe -> lore-vaults/poe1 (v3.30) → LORE check.mjs --host 34 pass (0 skip) + git-template E2E 与 MCP lore_record 实测通过
### 2026-11-21 按用户反馈完成 poedb_wiki 去 AI 味与 60%+ 首屏减法重构（原生 nav-tabs 净颜方案 A + fa-info-circle 悬浮提示 + collapse/details 按需展开 + 极简 5 键 MD 编辑器） → pytest 76 passed (ec=0) + 真机 CN/US 263/263 (100%) 零误差
### 2026-11-21 同步落地 PoE1 (pob_cli) 反向跨代防幻觉护栏 guardrails.py 与 test_cross_game_guardrails.py（136 项），拦截 <PathOfBuilding2>、trade2/poe2db/poe.ninja-poe2、PoE2 专属职业/珠宝/精魂/护符/符文/涂灌并精确豁免元素集中/灵盾/愤怒狂灵等合法词条 → 全量隔离 pytest 502 passed (0 failed)
### 2026-11-21 重写 v0.4.0 详尽中文主文档 README.md 并新增英文版 README_EN.md（补齐 macOS/Linux 安装、多槽位/pob serve、audit --keep 逐项归因、19-op batch、sweep、timeless 永恒珠宝搜索与退出码契约完整实战示例）
