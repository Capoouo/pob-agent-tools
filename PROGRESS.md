# PROGRESS

## ACTIVE
- 目标：完成 `pob-agent-tools`（`pob` CLI + 双槽守护进程）全量实现、Carbon 代码审查修复（P0-1..5 / P1-1..8 / P2-1..6）并公开推送至 GitHub
- 已做：[x] 实现 M1..M4 全套包 `F:/poe/pob_cli` 与 `AGENTS.md`；[x] 修复 Carbon review 全部 5 项 P0、8 项 P1、6 项 P2 并补齐 E2E 测试；[x] 创建 GPL-3.0 公开仓库 `pob-agent-tools`
- 下一步：[ ] 按需接入更多 PoB 3.28+ 新机制或赛季数据（禁踩：禁调 `compare_*` / `sidebar_breakdown` / `import_from_uri` / `character-window`；混合瓦尔技能脱水只关 `enableGlobal2` 禁关整颗宝石）
- 验证：`python -m pytest F:/poe/pob_cli/tests -v` → 10 passed in 77.05s（2026-11-12 20:02）

## LOG
### 2026-11-12 修复 Carbon Review P0/P1/P2：混合瓦尔脱水仅关 enableGlobal2、borrow 改用 copy_group+paste_group、修复 BridgeWorker 恢复队列竞态与 batch R12 校验 → `python -m pytest F:/poe/pob_cli/tests -v` 10 passed
### 2026-11-12 落地 pob-agent-tools M1-M4：双槽 HTTP 守护进程 + 4 源导入 + 4 维脱水审计 + 3 基准对比与移植 + poe.ninja dense 查价与 poewiki API → E2E 实测 AWqOLSSmyG 全通
