# PROGRESS

## ACTIVE
- 目标：`pob-agent-tools` v0.2.0 打包 CI、tag 发版流水线、一键安装与 Scoop 支持已本地提交并打好 `v0.2.0` tag，待用户手动推送远端触发首个 Release
- 已做：[x] 修复冻结/非 dev 安装运行态：`pob_cli/__init__.py` 单源版本 `0.2.0`、`pob --version`、隐藏 `pob _daemon` 子命令、统一 `get_runtime_dir`（源码仓 `.runtime`，安装版 `%LOCALAPPDATA%/pob-agent-tools`）与 `httpx[socks]`；[x] 落地 CI/CD 与分发：`.github/workflows/{ci,release}.yml`、`pob_cli/packaging/{pob.spec,pyinstaller_entry.py,release_tools.py}`、`install.ps1`、仓内 Scoop `bucket/pob.json`、测试构筑归档至 `examples/builds/`，并将发版 SOP 存为全局 skill `github-cli-release-ci`
- 下一步：[ ] 用户终端执行 `git push origin main; git push origin v0.2.0` 触发 GitHub Actions CI & Release，随后 `gh run watch --exit-status` 验证产物与 Scoop 清单自动更新（`曼波过滤/` 与 `NS曼波过滤.7z` 保持未跟踪不入库）
- 验证：`pytest` 47 passed (1 skipped) + `install.ps1` PS AST / `bucket/pob.json` / `ci.yml` & `release.yml` YAML 校验通过；全局 skill `C:/Users/moule/.agents/skills/github-cli-release-ci/SKILL.md` 已落盘（2026-11-20 13:59）

## LOG
### 2026-11-20 落地 `pob-agent-tools` 打包 CI、tag 发版（PyInstaller `--onedir` + wheel/sdist + 仓内 Scoop bucket `bucket/pob.json`）与 Windows 一键安装 `install.ps1`，修复冻结版守护进程拉起（`pob _daemon`）与运行态目录（`%LOCALAPPDATA%/pob-agent-tools`） → 47 项单测/集成全通 + `install.ps1`/`bucket/pob.json`/YAML 校验全通
### 2026-11-20 交付 `pob-agent-tools` v0.2（`f5a41dc`）：两阶段 Swarm 落地双上游 SHA1 增量热更+断网双通道告警+死锁自愈+SQLite 本地库+`poedb`/`meta`/`matrix`（排除 `api.github.com` 限流、热重载无锁唤醒竞态与 CI `inf` 非法 NaN JSON） → `python -m pytest F:/poe/pob_cli/tests -v` 52 passed + 8 门禁全通
### 2026-11-20 定稿 `pob-agent-tools` v0.2 Swarm 方案（`F:/poe/pob_v2_plan.md`）：选 `PobTools-zh` 开源宿主+原版 PoB `manifest.xml` 按需增量热更并加 GitHub 断网双通道显式告警（排除 1GB 整合包入库与标准 LuaJIT 不兼容 `2.68+` `+=` 语法） → `python -m pytest F:/poe/pob_cli/tests -q` 10 passed
### 2026-11-14 交付曼波过滤 NeverSink 迁移套件：选 `NormalChange`+前置自定义规则拆离魔镜与神圣石、全槽锁 `volume=300` 交由 48kHz Mono 3.5LU 母带控响（排除 `styleCombi` 粗粒度混音与缺省音量 100） → `python F:/poe/曼波过滤/neversink/tests/verify_e2e.py` ALL PASS
### 2026-11-12 评测评 PoB（poedb 链接 tLP41HmSYw，守护者 CI 魔力/闪避双叠 悖论之魔蛊爆炸）：修 `formatters.fmt_num` 遇 PoB 字符串哨兵（`ChaosMaximumHitTaken="inf"`）抛 `TypeError: bad operand type for abs(): 'str'` 导致 `summary`/`audit` 全崩 → 10 passed
### 2026-11-12 修复 Carbon Review P0/P1/P2：混合瓦尔脱水仅关 enableGlobal2、borrow 改用 copy_group+paste_group、修复 BridgeWorker 恢复队列竞态与 batch R12 校验 → `python -m pytest F:/poe/pob_cli/tests -v` 10 passed
### 2026-11-12 落地 pob-agent-tools M1-M4：双槽 HTTP 守护进程 + 4 源导入 + 4 维脱水审计 + 3 基准对比与移植 + poe.ninja dense 查价与 poewiki API → E2E 实测 AWqOLSSmyG 全通
