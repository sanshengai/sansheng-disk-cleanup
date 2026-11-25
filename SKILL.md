---
name: sansheng-disk-cleanup
description: 面向 AI 重度用户的 macOS 磁盘清理，会读懂你的电脑：第一次用一页选择题认识用户，按"谁产生、为哪个任务、是否结束、谁还在用"判断 Agent 会话、批量生成图、工作树中间稿、构建暂存、模拟器与模型缓存等能否清理，认不出的大目录会先调查；生成叁笙品牌选择看板，用户确认后复核执行（可先放废纸篓或归档到外接硬盘）并实测释放空间。腾空间、清理电脑、磁盘快满、调查异常占用、清理本对话或任务阶段收尾、培养 Agent 清理习惯时使用；Windows 尚未支持。
metadata:
  version: "0.15.1"
---

# 叁笙清理

把真正不再需要的东西找出来，讲清依据和后果，用户确认后安全执行，并实测释放了多少。不安装常驻软件，也不设定时任务。

**少打扰用户**：扫描前不问问题；第一次使用发一页设置选择题（只发一次），之后大多数时候在对话里用一句话确认，要用户看的东西多时才发清理看板；确认后预演、试点、正式执行由脚本连着做完，只有出异常才停下来问。

**核心原则**：判断由你（Agent）根据证据来做，脚本只负责采集事实和安全执行。"多久没动""可以重新生成""没有会话文件"都不能单独作为删除理由（例外：triage 1 档白名单类别，可再生 + 无进程 + 静默期无写入三者同时成立即可）；每条建议写出来之前，都要先做引用检查。**用户的设置和你的记忆只改变“问不问、预填什么、进哪一档”，不改变“能不能放行”。** 完整准则见 [references/judgment.md](references/judgment.md)，必须先读。

生产任务可提交 [生产交付清单](references/production-handoff.md)，体检和执行复核会读取成品、仍需保留的原料、消费者与验收证据。分批汇报读取 `quick-space.jsonl`，并列说明条目估算、目标卷及 APFS 容器实际余量变化、保留原因。并发写入时，能单独归因给本任务的字节数可为未知。

## 防误删红线（硬规则，优先于下文一切；2026-11-02 事故后加，见 [复盘](references/incident-20261102-production-dirs.md)）

1. **生产目录默认保护**：本机配置 `triage-local.json` 的 `production_globs`（成品、正在生产的产品线目录），以及登记在 `ledger_globs`（如制作台账 sqlite）或被 `AGENTS.md`/交接文档引用的路径，只进“受保护·生产目录”展示档，**任何年龄、大小、命名（试做/重制/v1–v3/旧版）都不进可删档**。triage.py `is_production()`/`apply_red_lines()` 已实现。
2. **引用检查是删前必做**：工作区（`work_root`）内任何候选，用 `rg -l -F` 搜目录名、绝对路径、`~` 路径、工作区相对路径，范围 `AGENTS.md`、`README*.md`、`ref_doc_extra`、`*.json` 台账、脚本（`*.py/*.sh/*.ps1/*.mjs/*.js/*.ts`）与 `SKILL.md`，外加 `rg -a` 搜 `ledger_globs` 登记文件。命中分两类：**消费者**（脚本、Agent/Skill 指令、README、非快照配置）命中或超时 → “被引用·需逐项问用户”档，不给删除命令；**只是历史记录提到**（本 Skill 往期报告、快照比对清单、前后对照 json、回执、日志、records/）不算依赖，只在说明里写“仅历史记录提到 N 处”（`triage.split_ref_hits()`）。triage.py `reference_hits()` 已实现；手工处理 scan.py/plan.py 候选时也必须照跑一遍。
3. **只有可再生的才能“清”**：缓存、构建产物、重复下载、node_modules、venv、derived/中间件，可按常规批准删除。人做的成品、素材、配音、音乐、拍摄/渲染成片一律算“搬走”不算“清理”，需要用户**逐项**批准，批准话术里写出目录全名。
4. **备份检查**：只有盘外备份才算“可恢复”——盘外备份台账（`offdisk_backup.state`，键为工作区相对路径）列有它，或外接盘归档经核对存在。**同盘存档不是备份。** triage.py `backup_status()` 只读给出结果（外接盘需人工核对）。
5. **清理日志**：每次真实删除**之前和之后**都追加到清理日志（`cleanup_log`，默认 `~/.sansheng/disk-cleanup/cleanup-log.md`）：时间、路径、大小、存档路径、恢复命令、谁要求的。其他 Agent 找不到文件时被指向这份日志（**把“文件不见了先查清理日志”写进共享 AGENTS.md**，否则别的 Agent 只会报“删除者未查明”）。`quick_act.py` 自动写前后两行。
6. **隔离期**：没有盘外备份的非缓存内容，先改名为 `<名>.待删-YYYYMMDD`（同卷、瞬时、可撤销），等 7 天无人反馈再删。纯缓存与已核对盘外备份的除外。`quick_act.py` 对 `kind=process` 强制走隔离，`purge` 满期才删。
8. **核查其他 Agent 是否在用**（见 [复盘](references/incident-20261103-active-agent-evidence.md)）：清理前查近期提交、其他 Agent 对话、已跟踪脚本和代码引用。用户已经授权处理明确范围（包括「把上面列出的剩余项清理掉」）时，直接使用原话和 `--isolate-authorized`。其他会话仅提到过程件路径时记入执行计划，按可恢复的隔离流程处理，无需让用户复述路径。实际占用、近期写入、代码引用等检查仍按执行器规则处理。
7. **收尾报告**：删除后在最终回复里给出每项的确切恢复命令（如 `tar -xzf <存档> -C <原上级目录>` 或 `mv <名>.待删-YYYYMMDD <名>`）。

## 目标与默认流程（2026-10 改造，先读）

**目标指标**：同一台机器、同一时刻，Skill 扫描耗时 ≤ 手工（≈9 分钟）；对手工独立核实的可回收条目核对漏项；按同一保留条件比较净减量；误报（把不该删的列进 1/2 档）= 0。改扫描器时按[固定样本对照方法](references/why-manual-beat-skill.md#固定样本对照方法)分别验收判断和耗时。

**本机配置**：工作区根、生产目录、制作台账、成品目录、盘外备份台账因人而异，写在 `~/.sansheng/disk-cleanup/triage-local.json`（示例见 [references/triage-local.example.json](references/triage-local.example.json)，字段含义见 `triage.py` 的 `LOCAL_DEFAULTS`）。没配置时，依赖它们的红线检查、成品拆分、存档核对自动跳过并在报告里写明，其余照常；第一次使用时按用户的回答帮他写好这份文件。

**第一步永远是快速分档体检（只读、默认写 /tmp、不写家目录）**：
```sh
python3 scripts/triage.py --out /tmp/<目录> [--prev <上次 triage.json>]
```
并行 du 先宽后深 → [agent-junk-locations](references/cards/agent-junk-locations.md) 白名单直查 → 便宜信号（一次 lsof 快照、`find -mmin` 最近写入、git status、merge-base）。输出：

| 档 | 内容 | 条件 | 交付 |
|---|---|---|---|
| 1 | 可再生缓存/临时文件（node 编译缓存、pytest-of-*、渲染缓存、Chrome 代码签名克隆、Homebrew/npm/uv/pip/gradle/bun、Library/Caches〔含 Google、baoyu-skills〕、codex 运行时安装残留） | 见下方「第 1 档统一规则」 | 检查命令 + 删除命令，**用户批准后**执行；App 运行中的放“待 App 退出后” |
| 2 | 链接 worktree | 非 locked、0 未提交改动、无打开文件、静默期无写入、HEAD 在 origin/main 或任一远端分支 | `git worktree remove <path>` 命令卡（不加 --force，不删分支）；路径已不存在的给 `git worktree prune` |
| 3 | 会话、生图、模型、镜像、构建暂存、同盘存档、未归类大目录 | — | 大小 + 最小定位 + 问谁 |
| 保留 | 在用或受保护（含原因） | 任一信号失败即落此档 | 说明原因 |

### 第 1 档统一规则（唯一出处，卡片只引用这里）

1. **条件**：白名单类别 + 无进程打开（lsof 快照）+ 对应 App 未运行（`ps`/`pgrep`：CodexBar、cmux、Codex、Google Chrome、Cursor、Claude、brew 等）+ 静默期内无真实写入 + 不在保护名单。
2. **静默期**：默认 6 小时（`--quiet-hours`）；知识卡写了更严（更长）的阈值时取较严者：视频渲染缓存 `hyperframes-extract-cache*` 24 小时、`~/Library/Caches/*` 24 小时（macos-system 卡）、node 编译缓存 24 小时（专用入口自带）。阈值登记在 `triage.py` 的 `TIER1` 表第 4 列，报告“静默期”列显示实际采用值。
3. **命令**：每项两条独立命令——先“检查”（`/usr/bin/find -x DIR -type f -mmin -N … -print -quit`，无输出才继续），再“删除”。删除原生优先：Homebrew `brew cleanup`、npm `npm cache clean --force`、uv `uv cache clean`、pip `pip cache purge`（本机写 `python3 -m pip cache purge`）；有专用入口的走入口（node 编译缓存 `node_compile_cache.py`、Chrome 克隆 `chrome_clones.py`）；其余“删除目录内容”统一写 `/usr/bin/find -x DIR -mindepth 1 -delete`（保留目录本身、不跟随符号链接、不跨卷）。**不写 `rm -rf`**（见硬约束）。
4. **App 运行中**：命中第 1–3 条但对应 App 在跑时，标“App 运行中，删前需退出”，移出一键清单，列入“待 App 退出后”小节。
5. scan.py / plan.py 流程里的“24 小时内有改动就保留”是第 3 档和卡片判断的默认，不是第 1 档的阈值；两者冲突时按本节。

Skill 自己不执行任何删除或 `worktree remove`。之后需要逐项深挖（会话归组、成品引用、done-evidence）时再走下面的 `scan.py` 流程；**只读体检用 `scan.py scan --read-only`**（运行目录落 `/tmp/sansheng-disk-cleanup-ro/runs`，只读家目录历史当基准），不往 `~/.sansheng` 写。引用检查只给少数疑难项补查（`probe item-refs`），大 git 树默认跳过，汇总会列出“引用未查”的目录。

**本机坑**：PATH 里的 `stat`/`sed` 可能是 GNU 版，一律写 `/usr/bin/stat`、`/usr/bin/sed`。

**反复出现的增长源**：Agent 每次修复新建整仓 worktree（每个 ~20G，10/9、10/25 各 +80G 级）——建议 Agent 用稀疏检出、合并后立刻 remove；清理存档放同一块盘会抵消回收——**禁止往本盘归档**：要留就搬外接盘或上云并核对上传，再删本地；已在本盘的存档，盘外有同路径同大小副本（盘外备份台账）的，triage 给“本地可删”命令卡。

**增长对比**：`growth.json` 里“本次新出现的大目录”计入合计并单列；同时给 df 口径（本卷已用）变化和“已列出合计 vs df 变化”差额（= df 变化 − 已列增长）：**差额为负 = 已列增长之外有删除/清理抵消；差额为正 = 低于阈值的小增长与未扫区域**；基准没有 df 口径时写“无可比基线”，不用容器口径数字下结论。

**口径（triage 报告）**：统一用 `/bin/df` 读数据卷（本机 `disk3s5`）的已用/可用，全程 GiB（1024³）。PATH 里的 GNU df（Homebrew coreutils）Used ≈ Size − Avail，与 BSD df 相差约 30 GiB，根因未证实（“~ 映射到系统快照卷”的解释已被实测反驳），只作参考，不下因果结论。scan.py 摘要是十进制 GB（shutil.disk_usage），不和 triage 数字直接相减。仓库体积要写明“只算 pack”（`.git/objects/pack`）还是“整个 .git”。

**成品目录等硬保护大目录**：`video_root` 整体仍硬保护（不进 1/2 档），triage 把第二层逐项列进“需用户判断的成品清单”：大小、最近写入、在工作区里被引用次数（一次 rg，超时标“部分”），先看写入再看命名：明确“作废”标记最优先，其次 24 小时内有写入的标“进行中”（不因名字含“试播/验证”被归进旧版），再按命名分“试做·重制·v1–v3·旧版 / 已交付·交付 / 当日产出 / 其他”；另在任意深度找 `*作废*` 目录单列（深层作废目录体积已含在上级，不重复计）。只列不给删除命令；作废且 0 引用也只写“建议用户确认”。

**最大 worktree 拆解**：triage 对最大链接 worktree（优先名字含 `prefer_worktree` 的）只读给出已跟踪变更数、未跟踪数、被忽略体积（`git ls-files --others --ignored --directory` + 批量 du；失败写明原因，不再静默报 0）、`git count-objects -vH`、各一级目录体积与主仓同名目录对比；单步超时标“部分”。**活跃工作树里停写的旧子任务**：工作树整体仍保留，但 ≥1 GiB、`--stale-days`（默认 7）天无写入、不在渲染的最外层子目录单列，统计其中未跟踪+忽略体积，给 `git clean -n -d -x -- <目录>`（预览）/`git clean -f -d -x -- <目录>`（删除）命令卡——只清 Git 不管的文件，已跟踪的在仓库里不动；问创建它的 Agent 或用户。

**2026-11-03 审计改进（triage）**：
- **开头/结尾各读一次 df**，报告给出扫描期间变化；**在渲染/波动**：生产目录下最近 10 分钟有写入的目录标“在渲染”，其大小与增长不计入增长合计（单列波动量），worktree 拆解每行带量取时间并标波动，主仓数字不再当作稳定值。
- **同盘存档 tgz 核对**：对 `same_disk_archive_globs` 下的 `**/*.tgz` 读首个成员定位原路径，判“唯一副本（原路径不在）/原路径在未比对”；`--archive-deep` 用 `tar -tvzf` 逐文件对比（文件数、大小、缺失文件是否在盘外备份台账），判“重复件/部分重复”，结果缓存在 `$TMPDIR`。**原路径在 ≠ 重复件**：2026-11-03 实测两个“已恢复”的 tgz 仍各有 24/68 个成片文件（2.5/1.9 GiB）原路径已没有。删存档前必须跑 deep 对比。
- **备份状态全覆盖**：worktree、受保护生产目录、第 3 档（含只读参考）、存档行都给 `backup_status`；只读解析 `offdisk_backup.logs` 的最终失败项（重试 N/N）并入“盘外备份失败清单”，落在失败前缀下的目录在备份状态里标出。
- 报告检查清理日志是否存在，缺失时报警（红线 5）。
- `/opt/homebrew` 列为程序本体只读参考；HF 共享 `hub/blobs` 不再进“未归类”，只在模型行旁注明；Grok 会话 URL 编码路径解码后合并 `//`。
- `--update-latest <链接>`：显式给出时把 latest-triage.json 指向本次 `--out/triage.json`（唯一写操作，默认不写），避免指错日期目录。
- 性能（仍只读）：盘外备份台账只加载一次并二分查前缀；引用检查合成一次批量 rg（`-o -F -f`），超时则逐项回退。

**2026-11-03 第三轮对比改进（triage）**，依据见 [why-manual-beat-skill.md](references/why-manual-beat-skill.md) 第三轮：
- **第 1 档拆到子项**：整目录因“有打开文件/近期写入”不过时，逐个子项复核，闲置子项进第 1 档、在用子项留在保留档（保留档只计在用部分）。程序本体目录不拆。Chrome 代码签名克隆逐份判：对应运行中 Chrome 主进程（`ps etime` 推算启动时刻，与克隆创建时刻差 ≤3 分钟）的保留，其余（含与安装版同版本、异常退出留下的残留）走 `chrome_clones.py`。
- **重复副本**：`--no-dups` 可关。对工作区与 profile 的成品目录里 ≥20MB 文件按大小 + 抽样哈希找重复，报名义可共享量与分布；处置用 `scripts/clone_dedupe.py plan/run`：全量 sha256、保留端按“成品库 > 一般目录 > 交付 > 分发暂存 > 临时 > 工作树”选定，其余副本 `cp -c -p` 原地换成 APFS 克隆（路径、内容、mtime 不变，不删任何路径）；执行时复核身份、打开、6 小时写入与保护名单。已是克隆的再克隆不腾空间；“已共享”是抽样判定（开头/中间/结尾，>256MiB 取 8 处），不是全量，以磁盘实测为准。
- **存档定位一次建索引**（原来每个 tgz 各跑一次 find，占总耗时六成）；盘外已备份的同盘存档单列命令卡。
- **扫描中出现又消失的目录**（临时快照等）标“临时产物”，不计入增长合计与未归类。
- 本 Skill 自己 7 天未写的旧运行目录（`runs/`、`investigations/`、`/tmp` 只读运行目录）进第 1 档。

**报告可读性**：第 1 档带“最近写入”与“静默期”列；`$TMPDIR` 显示为 `$TMPDIR/...`；URL 编码路径（如 `~/.grok/sessions`）解码显示；无权限位置按目录归类给 Top 10 并提示给终端完全磁盘访问后重扫；第 3 档里受保护的只读参考单列，不和“需决定”混写。triage 末尾提示运行 `probe projects`（它依赖 scan.py 的 facts.json 并写 projects.json，triage 不代跑）。

## 本任务收尾与习惯养成

用户说“清理本对话 / 阶段结束”时，先读 [references/task-housekeeping.md](references/task-housekeeping.md)，利用当前上下文盘点本任务产物，不先跑全盘扫描。创建时设置临时文件生命周期，阶段交付前主动收尾；第三方扫描用于发现遗漏。把稳定边界、扫描观察、具体文件账本分开，观察不自动变成删除权限，也不自动写入各家 Agent 的记忆。常驻提醒需安装到用户现有共享规则并核验加载；Skill 更新本身不代表所有 Agent 已养成习惯。

## 流程

### 第一次使用

判断依据：`python3 scripts/plan.py propose` 打印的 `stage` 为 `onboarding`，或 `~/.sansheng/disk-cleanup/profile.json` 里既没有 `setup.version` 也没有 `onboarded_at`。

1. **快扫**（约 1 分钟，用户不用等引用检查；结果只用来出题）：
   ```sh
   python3 scripts/scan.py scan --no-refs     # 结果写到 ~/.sansheng/disk-cleanup/runs/<scan_id>/facts.json
   python3 scripts/scan.py probe projects --run <run目录>   # 认出项目、已装工具、成品候选目录和本机情况，写 projects.json
   ```
   告诉用户：第一次要多花一点时间认识你的电脑，以后会快很多。

2. **发设置页**：`python3 scripts/plan.py setup --run <run目录>` 生成 `setup.html`，把路径交给用户，说一句“按你的电脑出题，大多数题有推荐，可以点‘全部按推荐’；这页只保存设置，不会删任何东西”。按机器出题：没装 Docker 就不问 Docker。
   - 第一次：全部适用的题（`stage=onboarding`）。
   - 老用户升级：`propose` 打印 `setup_upgrade: true` 时，先发只补新题的版本（`plan.py setup --mode upgrade`），之前的设置都保留，再发清理看板。
   - 用户说“改设置”“重新认识我的电脑”：`plan.py setup --mode edit`，所有题预填现值，导出只带改动过的题。
   - 用户说“跳过设置”：存一个只含 `叁笙清理 · 初始设置`、`设置版本: 1`、`答案: {}` 三行的文件走下一步，全按默认，以后不再弹。
   - **发出设置页的同时，在后台启动完整扫描**（`nohup python3 scripts/scan.py scan > <run目录>/full-scan.log 2>&1 &`，约 7 分钟），用户答题的几分钟里就扫完了，不让用户额外等。

3. **保存设置**：用户回传抬头为“叁笙清理 · 初始设置”的文本，存成文件，`python3 scripts/plan.py profile --apply <文件> --scan-id <scan_id>`。这是保存设置，不是清理授权，`act.py` 也不接受它。输出里的“补充（用户原话，不存档）”由你翻译成一句规则或备注：`python3 scripts/memory.py pending add --text "<一句话>" --kind protect|rule|note [--value JSON]`（写法见 contract.md 第 10 节）；它会出现在下一张清理看板上请用户点“对 / 不对”，确认前不生效。

4. **第二轮：完整扫描 + 深度复审**（按设置分配精力）：快扫没查引用，它的结果只用来出题，不能支撑任何删除卡，所以用第 2 步在后台跑完的那次完整扫描（还没跑完就等它；没启动就现在跑 `python3 scripts/scan.py scan`）：
   ```sh
   python3 scripts/scan.py probe projects --run <完整扫描的run目录>   # 新的扫描要重跑
   ```
   用户在设置里新标的成品目录，后台扫描时还不知道：写完计划后照常跑 `probe item-refs`（见下文），它会按新范围补查引用，`check` 才放行相关条目。
   - 用户的文件一律经 `python3 scripts/peek.py --run <run目录> <路径>` 读，不直接 `cat`；它按用户授权的读取范围放行，凭证类永远拒绝，每次读取都记录，成果页会列出（见 judgment.md 第 10 节）。读之前向用户说明：读到的内容会发给你所用的 AI 服务商。
   - 常用工具、进行中项目同样分析占用和增长；按具体子任务、输入、产物、运行状态拆分，不把项目状态当作所有文件仍有用的证明。
   - 认不出的大目录先调查，再用 `memory.py identity set` 记成本机身份卡（judgment.md 第 11 节）；用 `python3 scripts/scan.py probe done-evidence --run <run目录> --path <目录>` 找“做完了”的证据（第 12 节）；有长期价值的素材给“归档”选项（第 13 节）；会一直涨的来源按用户的 `governance_mode` 写治本建议（第 14 节）。
   - 然后进入下面的“扫描与判断”（扫描已经做过，从“逐组判断”接着做）。

### 以后每次

扫描 → 判断与写计划 → 校验、封存、提案 → 清理看板或对话确认 → 执行 → 成果页。新出现的项目、标成“进行中”却很久没动的项目，在对话里或看板上单题问一句；设置不再重发。

**没有可直接删除的项目，也必须交付判断入口**：不得把“没有通过校验的可执行项”说成“没有东西可清理”，也不得用磁盘余量充足作为结束理由。此时仍生成并交付看板：说明哪些确实在用、哪些只是尚缺证据。对于只因任务状态、用途或副本不明而暂时保留的对象，按具体任务拆卡，`verdict: keep` 并写 `question`；看板会给“想清理，继续查 / 保留 / 暂不确定”的选择。不要把所有未判断的大目录合成一张“其他资料”后结束。

看板只有一个导出按钮“复制清单，发给 Agent”：一段文本里先是“叁笙清理选择”，有待决定答复时后面接抬头“叁笙清理 · 待核查答复”的部分，这一部分**不是删除授权**，`act.py go --from-board` 只把前一部分当清理授权、答复另存为 `review-answers-<时间戳>.txt`。用户选“想清理，继续查”后按具体对象补查，重新写计划、校验并交付清理清单；仍在运行、绝对保护和确有依赖的对象继续保留，不因答复而放宽条件。大项目“进行中”不证明其中每个旧子任务都还要用；可以询问具体旧任务是否需要清理，但不能自动改整个项目状态或直接删除。没有待核查项时也展示占用与保留依据，不发送空的“回复好就删”邀请。

## 深度调查与授权

大型目录必须下钻到子任务；小于扫描阈值的文件也按类型、创建/修改时间和用途聚合，避免数百个小目录合计几十 GB 却全部漏掉。用 `scripts/deep_inventory.py ROOT --out FILE` 采集只读清单；时间只作线索。重复文件须逐字节哈希、明确保留副本及依赖；重复统计不能直接当作可释放空间。

大量旧执行现场、登记副本或中断初始化构建树，按 [task-retirement.md](references/task-retirement.md) 追踪具体阶段与实际生产脚本。通过窄 checkpoint 证明已退役副本，仍走完整保护与封存执行；不能因为父项目进行中而停止调查。怀疑 Agent 宿主在别处先生成再复制时，按 [工作区落盘链](references/cards/git-worktree-build.md#从-agent-宿主追到真正的落盘位置) 核对活跃配置、准备区、专属与原生 Agent home、最终交付位置；只扫描软件同名目录不能证明没有副本。

当前用户系统临时区中的 Node 编译缓存，使用 [包管理与构建缓存卡](references/cards/package-managers.md#当前用户的-node-编译缓存) 的 `scripts/node_compile_cache.py` 专用入口；它从系统发现根目录、封存精确文件，按 plan → check → dry-run → pilot → run 执行，不开放任意临时路径清理。

**Agent 过程资料的固定扫描位置**：每轮都按 [agent-junk-locations 卡](references/cards/agent-junk-locations.md) 逐项量 Codex / Gemini / Grok 会话与生图、`~/Documents/Codex/<date>/task-N`、`$TMPDIR` 的同步工作树与渲染缓存、`build-tmp` 暂存槽、`locked initializing` 的登记残留和模型权重；工作树内按“跟踪 / 未跟踪+忽略”分开计量，并与主仓同名目录比体积和活跃度。

**新鲜度检查要说清改了什么**：“24 小时内有改动”只看真实内容写入，忽略 `.DS_Store`、`*.lock`、`index`、`*.pyc`、目录 mtime/ctime 和只改元信息的文件（如 `.agent_harnesses.json`）；报告时列出最近改动的文件名和时间，而不是只给一个计数。仅元信息变化不再当作“正在使用”，但仍走进程与引用检查。

**估算释放量要扣掉共享块**：先用 `/usr/bin/stat -f %l`（必须写全路径） 找硬链接，同 inode 只计一次；APFS 克隆无法用 du 区分，标“可能为克隆、实际释放待实测”。同一轮里把旧目录打包成 tgz 放在同一磁盘会抵消释放量；并发的构建、同步工作树和渲染也会吃掉余量。报告按“条目核算 / 打包新增 / 并发增长 / 磁盘实测”四项分开写。

**看不见的地方要说出来**：终端没有“完全磁盘访问”时，`~/Library/Containers`、`Group Containers`、Mail、Messages 等只能量到极小值；报告里写明“未覆盖，需在系统设置给终端完全磁盘访问后重扫”，不要把它们算成 0。

**问谁**：会话与生图问用户；正在跑的项目子任务问创建它的 Agent（看 README、交接说明、进程命令行里的工作目录）；部署槽位和同步工作树问持有部署锁的 Agent；定时任务相关目录问维护它的 Agent；需要 sudo 或系统设置的事给用户命令。卡片的 `question` 写明对象。

**审计要收敛为处理结论**：按占用从大到小核查本轮范围，每个已查组给出“推荐删除 / 保持路径去重 / 明确保留 / 具体缺口”之一，附实际文件范围、依据与影响。明确保留写出仍需它的消费者；缺口写出尚未取得的证据和下一步，不用笼统的“在用”“其他资料”结束。已获授权且通过现有校验的项继续执行，不等无关组全部查完。父项目活跃时，仍可核验并退休其中已完成的登记、测试、渲染或构建子阶段，保持父项目状态不变。

汇总前把跨目录候选按实际文件去重，并固定全批次的保留副本；同一文件不能同时计入删除和共享块，两个候选也不能互为唯一保留端。分别报告候选量、执行成功的条目核算量和磁盘实测余量变化；名义重复量、已扫描体积不算已释放。具体方法见 [agent-artifacts.md](references/agent-artifacts.md)。

会话分别记录文件是否存在、父子关系、最后一轮状态、归档标记、运行进程与项目交付状态。最后一轮 completed、归档为 false、没有进程或很久没动，都不单独证明任务结束。Codex 的 `.jsonl.zst` 也计入；压缩文件的元信息优先从只读数据库读取，读不到必须标 unknown，不直接解压巨型文件。

会话退休前按[会话内嵌素材核验](references/session-materials.md)检查可能只有一份的附件。执行入口须能复核原件和保留副本，无法读取的部分标明范围。

用户追问“对话怎么只有这么大”时，同时盘点普通和压缩会话、分页历史库及其 WAL、生图与工具输出、任务工作目录、构建与渲染暂存。逐组写出本机现存量、活跃或唯一副本、可处置范围；父目录与子项只计一次。查前次回执解释已经移走的部分，历史回收量另列。

用户已经明确授权清理本轮证据充分的冗余内容时，不重复索要同一授权，不伪造看板导出。保存用户原话和实际清单，照常经过 check、seal、dry-run、pilot、run；仍有唯一副本、依赖或用途疑问的对象保留并单独说明。用户要求腾空间时，清理授权覆盖清单内的永久删除；不得顺便清空整个废纸篓。

进行中项目默认继续保护；对单个临时 Git 索引提供机器可证的例外：`checkpoint={kind:"git_index", repo:"绝对路径", commit:"完整提交号", retain_ref:"refs/remotes/origin/main", sha256:"文件哈希"}`。条目必须 method=delete、scope=all、单个被忽略的普通 `.index` 文件，完整索引树必须匹配保留分支中的提交；校验和执行都重验。仍须确认生产脚本会重新初始化索引、该旧步骤不会继续消费它，并通过引用、24 小时和打开文件检查；不自动改项目状态，不清理 Git 管理目录。

本轮明确允许清理近期文件时，先写好精确 items JSON 数组，再运行：

```sh
python3 scripts/run_authorization.py --run <run目录> --items <精确条目数组.json> \
  --user-text '<用户实际原话>' --allow-recent
```

将输出的 `run_authorization` 引用放入每个对应 item。账本绑定本轮扫描、文件身份及 SHA；check、执行和删除瞬间重验。它只允许越过默认时间保护，不能越过进程占用、保护路径、Git 跟踪或仍需核实的依赖；不要修改 mtime 或把其他会话改成已完成。用户同时明确退休已完成的构建输出时可加 `--retire-completed-outputs`，并提供 `completed_output_stage` checkpoint：网站装配使用保留的 manifest/receipt 和真实消费者代码，旧 APK 使用原始 version 回执及明确保留包；结构与要求见 [阶段退役说明](references/task-retirement.md#已完成的网站装配与旧安装包)。缺证明仍保留，不能仅以“可再生”代替。永久删除继续走原有 `--no-trash --trash-reason '<用户原话>'`，不改变个人设置。

另支持 `git_blob_copy`：已完成子任务中的测试或发布候选复制件，可用合入提交、带哈希的任务回执及完整 Git blob 恢复证明逐文件清理。不得把整个项目改成已完成，不放行受保护项目、正在使用的文件、真实 Git 管理文件或唯一成果。恢复仓库、回执不能同时出现在删除清单中。字段及反例见 [references/agent-artifacts.md](references/agent-artifacts.md)。

**Agent 过程资料占大头时，必须执行 [深挖与存储去向核对](references/agent-artifacts.md)**：分析大目录和小文件聚合、具体任务的完成证据、同内容副本、网页内嵌媒体、发布与备份覆盖。为每组说明留本地、部署到 VPS、公开对象存储、私有备份或删除的理由；不把“已上线”当成“原素材已备份”。用户要求独立模型复核时，先保存自己的结论，再经已授权 API 提交脱敏证据，比较分歧并保留实际模型回执；模型意见不能替代本机验证。

旧验收 HTML 重复内嵌 MP4 可用 `scripts/compact_review.py` 改成同目录树的媒体引用；只支持脚本明确识别的格式，完整哈希验证和恢复演练通过才执行。页面将依赖旁边的视频，交付单文件网页前须恢复。命令、保留清单及验收见上述 reference。

## 扫描与判断

1. **扫描**（只读）：
   ```sh
   python3 scripts/scan.py scan [--read-only]   # 默认阈值 500MB、引用检查 900 秒；大 git 树跳过引用扫描
   python3 scripts/scan.py probe projects --run <run目录>   # 每次必跑
   ```
   读脚本打印的摘要。出现“引用检查有 N 个搜索根没查完”时，先加大预算重扫（`--refs-seconds 1800`）；出现“没有可搜的引用检查目录”时，节点显示“引用未查”，只能保留或待定，先补成品目录再用 `probe item-refs` 补查。`/private/tmp/claude-<uid>`（Claude Code 会话临时目录）存在时默认也会扫，当前会话继续保护，24 小时内有改动默认保留；用户明确授权本轮近期文件时，按下文的精确清单授权核验，不改时间或全局默认。每个前沿节点带 git 角色、是否被打开、24 小时改动数、引用命中数、知识卡名、与上次的增长量，认下的目录还带 `identity`。

   **`probe projects` 每次扫描后都要跑**：台账里有“进行中”或“别碰”的项目而这次没有它的结果时，`check` 会让所有卡片降为保留。

2. **逐组判断**：节点的 `producer.card` 就是知识卡名，读 `references/cards/<card>.md`；没有 card 的节点按 judgment.md 四个问题自己判断，拿不准写 `decide`。需要逐项事实时运行探针：
   ```sh
   python3 scripts/scan.py probe codex-groups            # Codex 会话按来源归组；--trace N --emit <run目录>/items 逐线程核对并出条目文件，一次一组
   python3 scripts/scan.py probe codex | claude | android | grok   # 各家专项
   python3 scripts/scan.py probe worktrees --repo <仓库>  # 只接受 git 仓库
   python3 scripts/scan.py probe system | updaters | leftovers | bigfiles
   python3 scripts/scan.py probe done-evidence --run <run目录> --path <目录>
   ```
   **AI 工具的会话不能只看单个体积**：批量脚本每次只留几 MB，却能累积几十 GB，必须先归组再判断。**交互对话一律逐个出卡、逐个问**，不能因为没出图、没被引用就批量判 `clean`。

3. **按用户设置写计划**：先读 `profile.json`（各字段含义见 contract.md 第 1 节，各设置怎么影响判断见 judgment.md 第 15 节）；台账状态见第 8 节。在 run 目录写 `plan.json`，格式见 contract.md 第 4 节；条目多时用 `items_file` 引用探针生成的文件，不要手抄。同一软件或同一任务合成一张卡。卡片文字给用户看，用用户认得出的说法：`title` 写“这是什么”，`summary` 写“为什么会有”，`impact` 写“删了会怎样”，不出现节点、前沿、引用样本等内部词。`cards_used` 第一项决定类别；每张 `clean`/`decide` 卡写 `preset`（`safe` / `recommended` / `all`）。结束证据用对象形式（`merged / delivered / copy_verified`，`user_stated` 不算），引用核对用 `refs_reviewed`。

## 校验、封存、提案

```sh
python3 scripts/scan.py probe item-refs --run <run目录>   # 计划写好后：落在大目录内部的条目按自己的路径补查引用（通常一两分钟）
python3 scripts/plan.py check   --run <run目录>
python3 scripts/plan.py seal    --run <run目录>
python3 scripts/plan.py propose --run <run目录>     # 写 proposal.json，打印“方式”、一段给用户的话和三档对应的命令
```

`check` 把不满足条件的卡降级并写明原因（缺结束证据、含交互会话、被引用未核对、受保护、正在使用等，条件见 contract.md 第 4 节），降级原因要如实告诉用户；`item-refs` 查到引用的条目，打开 `item_refs.json` 看样本，只是历史记录的在卡片 evidence 里写 `refs_reviewed(history_only)` 再 check，还在用的写 `depends`。

**先预查，再承诺**（2026-11-04 两次再犯后加）：给用户可复制的授权句、或说“回一句话能腾 X”之前，先用同一份清单跑执行器的计划阶段：看板链跑 `plan.py check`，对话授权链跑 `quick_act.py plan`（不带 `--upload-missing`）；两者都不删东西。授权句只写通过的和“需点名”的条目，并写出要点名的目录名；硬保护的（如成品目录）只说明原因，不写进删除授权句。调查结论里有“已被取代”“没人用了”“旧脚本”这类话，先打开消费者脚本，看它属于哪条流程，再复述。

**按 `propose` 打印的“方式”走，不自己改判**：
- **在对话里确认**（`mode=chat`）：把它打印的那段话原样发给用户；回“好 / 稳妥 / 都删”就运行对应那一行 `act.py go --choice`；要看细节再发看板。发看板的提案 `go --choice` 一律拒绝。
- **发清理看板**：要用户看的项超过 3 项、含交互会话、或用户设了“每次都发看板”。运行 `python3 scripts/plan.py board --run <run目录>`，把路径交给用户，用几句话说能腾出多少、要用户决定什么。看板顶部写着“按你的设置……”，新项目和久没动的项目是单题卡，待确认规则每条两个按钮，卡片选项是删 / 留 / 归档（选归档要填外接硬盘路径）。用户点“复制清单，发给 Agent”后，把整段文字存成文件交给 `act.py go --from-board`。
- **新项目与久未活动**：`proposal.project_questions` 在对话里用一句话问，用户回答后 `plan.py project --key <项目键> --status done|active|archived|protected`。用户要“项目还在做，但里面的 AI 对话逐个问我”时用 `--conversations ask`（只放开按会话归属的单个 AI 对话记录；带生成图的对话、整目录、项目目录里的文件照旧保护；放开的卡只在看板上由用户一张张确认，不预填、不进对话档位，档位按钮也不勾它们），不要改成“做完了”。含交互对话或因“逐个问”放开的卡，只按用户发来的看板导出用 `act.py go --from-board` 执行，不自己算 `sel` 去跑 `act.py run`。
- **飞轮提问**：提案末尾“以后都这样吗”，用户回“以后都删 / 以后别列”就运行它打印的 `plan.py rule --key … --reply delete|keep`；回“不用”用 `--reply never`；撤销用 `--reply forget`。
- **云盘建议**（`proposal.cloud_suggestions` 非空）：只给建议和官方入口，不动云盘文件；用户自己动手前提醒别直接删除。
- **治本**（`proposal.governance_mode`）：`tell` 只说现象，`guide` 给改法，`fix` 写“同意后我来改，可撤销”；改完用 `memory.py govern record --path P --change … --undo … --bytes N` 记一笔，下次扫描 `memory.py govern check --run <run目录>` 核对有没有停涨。

## 执行

### 用户已整批授权：已授权清单执行器（首选）

用户在对话里整批授权（“低风险的直接删”“证据充分的全部删”）、不需要看板时，**不要临时写脚本自己删**：把判断好的清单写成 items JSON，交给 `scripts/quick_act.py`，复核、执行、清理日志、回执、恢复命令都由它做。

```sh
python3 scripts/quick_act.py plan --items <清单.json> --user-text '<用户原话>' --run <运行目录> [--isolate-authorized] [--upload-missing]
python3 scripts/quick_act.py run  --run <运行目录> --seal <封存码> --pilot   # 先试点第一项
python3 scripts/quick_act.py run  --run <运行目录> --seal <封存码>           # 再全部（已完成的自动跳过）
python3 scripts/quick_act.py purge --run <运行目录>                           # 7 天后清单个隔离区
python3 scripts/quick_act.py purge --all [--orphan-scan <目录>]               # 扫描全盘回执或孤儿隔离项
```

- **持久化 `--run` 目录**：生产运行（如 `--run` 传 `~/.sansheng/disk-cleanup/runs/<任务名>` 或工作区 `_workspace/disk-watch/...`）严禁放在 `/tmp` / `$TMPDIR`，避免重启丢失回执。
- 每项写明 `kind`：`regenerable`（**已登记**的可再生缓存：第 1 档白名单、本机配置 `extra_tier1`、node_modules/__pycache__ 等，直接删；Agent 自己声明“可重建”但不在登记里的，一律按 `process` 只隔离）、`backed_up`（有盘外备份：调用本机配置的备份适配器逐文件核对台账、抽样从盘外回读，`--upload-missing` 先补传缺的，全部一致才删）、`verified_obsolete`（已核实冗余或已交付过程件：仅当用户原话同时给出明确直删意图与点名路径时直接删，保护名单/占用/静默期红线照旧拦截）、`process`（没有盘外备份的过程件：只改名隔离，不释放空间）。
- `scope`：`all` / `children`（支持可选 `keep_names` 保留指定直属子项）/ `files` / `untracked`（Git 工作树里只删未跟踪和忽略的文件）。
- plan 与 run 各做一遍红线检查（保护名单、生产目录、Git 跟踪、文档与代码引用、他人活动、打开文件、静默期写入、App 运行、盘外备份），run 以执行瞬间的事实为准，目标被替换（inode 变了）就跳过。
- 对话已明确授权处理某份清单或其中一部分时，清单只纳入已授权项，使用 `--isolate-authorized`，沿用原话和已有范围。该选项只处理「其他对话提到过路径」这一个信号，只允许隔离过程件。plan 的其他拒绝原因按实际检查结果处理。
- 任何一项检查自身失败（超时、git/rg 出错、本机配置读不了、认不出本会话记录）都按拒绝处理；封存码按计划内容重算，改过计划文件就失效；删每个目标前再测一次身份、写入、占用和文件清单。
- 本会话自己的对话记录不算“他人活动”：脚本按用户原话识别；识别不到时用 `--self-transcript <路径>` 指明。
- 盘外备份适配器协议见 [triage-local.example.json](references/triage-local.example.json) 的 `offdisk_backup.adapter`。

### 冷存：暂时不用、但要留着的大目录

第 3 档里“很久没人改、不可再生、但还想留着”的目录（旧成片、已交付批次、研究素材），删又舍不得、留着又占地方时，本机配置了冷存工具（`triage-local.json` 的 `cold_storage`）就给用户第三个选项：**搬到盘外，用时按原路径取回**。没配置时只列候选，不提这个选项。

**一次扫描，三种去向**（用户要“深度清理”“腾空间”时的默认做法，不要先清理扫一遍、冷存再扫一遍）：
1. `triage.py` 全盘体检一次（上文第一步）。
2. `cold_store.py candidates --from-triage <triage.json>`：只在体检列出的大目录里下钻（受保护的生产目录、要问人的、认不出的、第 3 档），找出其中整棵子树都很久没改的部分，不再扫全盘。受保护目录是大头藏身处：体检只给总数，不下钻就只能整块保留。
3. 对每处逐个判断去向，汇成**一张清单**让用户一次点名：
   - **删**：可再生（缓存、构建产物、依赖）、已交付成品的中间稿与候选、已合回的工作树、过程临时目录（下钻结果标“像过程件”的先往这边判，看过内容再定）→ 走 `quick_act.py`；
   - **冷存**：不可再生或重建代价高（成片、原始素材、资料库、已交付批次、旧会话），且闲置 → 走 `cold_store.py offload`；**垃圾不冷存**，冷存目录里的中间稿先删再搬；
   - **留**：近期在写、构建/门禁输入、进行中的项目。
4. 清单里每行写清大小、闲置天数、去向和理由；带保护原因的行需要用户原话写出路径。

```sh
python3 scripts/cold_store.py candidates --from-triage <triage.json> [--max-depth 6] [--json 结果.json]   # 一次扫描后的下钻（推荐）
python3 scripts/cold_store.py candidates [--min-idle-days 14] [--min-gib 1] [--max-depth 6]             # 只扫工作区根：整棵子树都冷的目录；Git 已跟踪的文件留原处、不计体积
python3 scripts/cold_store.py offload <目录> --user-text '<用户原话>' [--keep <保留项>] [--quiet-hours N] [--self-transcript <路径>]   # 交给冷存工具：上传、逐个核对，再经 quick_act 复核后删本地
python3 scripts/cold_store.py where <路径>  /  restore <路径>                  # 找不到的文件先查是不是冷存了，在就取回
```

- 候选只是建议：删本地仍由 `quick_act.py` 复核红线（生产目录、引用、他人活动、占用、静默期），需要点名的照常要用户原话写出路径；冷存工具上传核对未通过就不删。
- 近期会复用且允许等待的资料可放协作盘；对端休眠时排队，本机继续保留。已交付、罕用且能按原路径恢复的资料可进冷存。混合目录按文件拆分，已部分归档的批次先核对旧回执和目标端内容，再处理差额。报告写明实际核验的副本数。
- 空间分开报三项：成功处置的文件规模、按本批目标核算的净减量、执行窗口内目标卷和 APFS 容器的余量变化。净减量按目标实际分配空间的前后差计算，扣除测量范围外本轮新增的素材与回执；共享块无法归因时标未知。`free_delta` 只说明执行窗口的余量变化。
- 配置 `cold_storage.consumer_scopes` 可登记“源目录”和“通过软链接使用它的目录”（每组为 `{"source": "/data/originals", "roots": ["/data/by-year"]}`）。候选调查一次每个登记范围；发现引用或该范围扫描不完整时，保留原件并说明原因。独立经校验的分层存储可先只上传，待使用方迁移后再释放本地。未登记的范围仍须核查实际使用方。
- 冷存工具删本地时条目带 `cold_batch`：只有这种条目、且用户原话写出该路径，才能越过用户自己在 `hard_keep_extra` 里加的保护词（如成片目录）；内置硬保护（`.git`、部署槽、`/Movies/` 等）不例外。
- 原目录会留一张说明文件（`cold_storage.placeholder`），任何人或 Agent 看到它就知道去冷存工具取回，而不是重新生成。

### 看板与对话确认

只在用户本轮明确回复或发来看板清单之后：

**确认时先讲清最终结果**：直接删除写“预计腾出 X，删除后不可撤销”；先放废纸篓写“处理 X，暂不释放空间，彻底删除后才腾出”。用户以腾空间为目标时，不要把暂存完成当作任务完成。当前设置为先放废纸篓、用户明确要求本轮直接永久删除时，沿用这次授权并用下面的 `--no-trash --trash-reason`，不再重复询问，也不擅自改变长期偏好。

```sh
# 看板清单：用户发来的整段文字存成文件，一条命令走完（校验 → 保存设置/规则 → 预演 → 试点 → 正式执行 → 成果页）
nohup python3 scripts/act.py go --run <run目录> --from-board <清单文件> > <run目录>/go.log 2>&1 < /dev/null &
# 对话确认
nohup python3 scripts/act.py go --run <run目录> --choice recommended > <run目录>/go.log 2>&1 < /dev/null &
```

- **删除方式**：默认读 `profile.delete_mode`（没设就是直接删）。`--trash` 把文件移进本轮专用废纸篓目录，暂不释放空间；`--no-trash` 强制直接删（若用户设置是“先放废纸篓”，必须另给 `--trash-reason "<用户原话>"`，它与 `--skip-pilot` 用的 `--reason` 是两个参数）。用户已明确同意永久删除时不要重复确认。跨盘放不进废纸篓的文件会跳过并写明原因。
- **已经放进废纸篓，用户要求立即腾空间**：仅对本轮封存、执行回执可证明的普通单文件条目，先运行 `python3 scripts/finalize_trash.py --run <run目录> --seal <digest>` 预演，再加 `--execute <预演摘要> --reason "<用户同意永久删除的原话>"` 执行。逐文件验证原 inode、内容指纹、无软硬链接，永久删除后自动刷新成果页；不触碰废纸篓的其他内容。目录型条目尚不支持该补清流程，必须如实报告，不能改用 shell 删除或绕过保护。文件已被用户清空或移走时拒绝重复删除，不猜测已释放量。
- **归档**：看板上答复“归档”的卡，目标取清单里的“归档到:”（`run` 直接执行时可加 `--archive-to /Volumes/<盘名>/<目录>`）；对话确认（`go --choice`）不支持归档，归档只走看板。目标必须是外接硬盘、剩余空间够；脚本逐文件复制并核对 sha256，一致后才处置原文件（原文件再按删除方式处理）；目标不合格整卡跳过并写清原因。
- `go` 依次跑 预演 → 试点 → 正式执行，每步打印 `【go】…`。退出码：0 完成；2 拒绝（清单或提案不合格，什么都没删）；3 停下请用户看；4 正式执行有条目失败（用 `act.py status --run <run目录>` 看哪几条及原因，不要报“全部完成”）。
- `--from-board` 会拒绝抬头为“叁笙清理 · 项目台账”或“叁笙清理 · 初始设置”的文本；清单的 `scan_id`、`seal` 对不上、`sel` 被改过也拒绝。清单里的设置、规则变了就自动重新封存，digest 不变继续执行，变了就停下发新看板；封存后设置改过，`run` 与 `go` 一律拒绝并要求重新 `check` / `seal`。细节见 contract.md 第 7c、8 节。
- 备用（go 出问题时手动分步）：`act.py run --seal <digest> --sel <sel> --cards … --answers … --dry-run`，再 `--pilot`，再正式执行；参数从清单原样抄。
- 执行过 `codex delete` 后会自动回收 Codex 历史库空间；被打断用 `act.py reclaim` 补做。

执行时逐项重新复核（路径身份、保护名单、进程占用、24 小时内的改动、被跟踪文件），写出回执，正式执行结束自动生成成果页 `report.html`（补做用 `python3 scripts/report.py --run <run目录>`）。**清理完把成果页路径交给用户**，用一两句话说腾出多少、其中多少是 AI 工作留下的，并报告“按条目核算释放”（直接删除的字节），与磁盘余量变化对不上时先查同时运行的构建、下载；移入废纸篓的、搬到外接硬盘的单独说；再报告跳过和失败的原因。成果页有三块新内容：**这次读了哪些文件**（读过内容的文件和次数）、**这次我记住了**（用户勾“不对”后复制“忘掉: m1,m2”发给你，运行 `memory.py forget --from-text <文件>`）、**从源头改好的**。第一次清理的成果页会带一句“好用的话，去 GitHub 点个 Star”，不需要你另外提。

**反馈（只在脚本提示时）**：脚本提示“这次有 N 个大目录认不出来源”时，在汇报末尾问一句要不要生成反馈。同意就运行 `python3 scripts/feedback.py issue-url --run <run目录>`，把“将要提交的全文”原样给用户看，确认后把预填链接给用户，由用户自己点开提交；上不了 GitHub 就复制文字让用户发到 https://sanshengai.top/tools/disk-cleanup/feedback/ 或公众号；用户说“不用”就 `feedback.py decline`（90 天内不再问）。反馈只含脱敏路径模式、体积、软件名，绝不代用户提交。

## 硬约束

- 只有用户在本轮明确选择的才能执行：对话里回复了提案中的某一档，或发来看板清单。历史授权不覆盖新的对象，“打开看板”“看了提案”都不等于授权。
- 项目台账、设置页答案、待确认规则和记忆都**不能单独作为删除依据**，也不能替代副本核对、交付证据、引用检查、进程与 24 小时改动检查；最激进的一整套设置取下来，也不该有任何卡从 `decide` 变 `clean`。唯一更严的方向是“别碰”和“永不读”。
- 读取：用户的文件只经 `peek.py` 读；`never` 类（钥匙串、浏览器登录数据、密钥、`.env`、密码库等）任何设置都读不了；读了就留痕。
- 保护名单由 `scripts/protect.py` 强制执行，不得绕过，也不得用 shell 的 `rm -rf`。第 1 档白名单缓存目录唯一允许的通用写法是 `/usr/bin/find -x DIR -mindepth 1 -delete`，且必须在独立的检查命令之后、用户批准之后执行。不在封存计划里的路径一律不动。
- 原生清理命令优先（`sdkmanager --uninstall`、`codex delete`、`hf cache rm`、`brew cleanup`、`npm cache clean --force`、`uv cache clean`/`uv cache prune`、`pip cache purge`），它们不进废纸篓、不归档。**不要推荐 `claude purge`**：它会连记忆与配置一起删，`protect.py` 有意不登记它。工具自带的保留设置写进 `governance`，经用户同意后再改。
- 聊天软件（微信、企业微信等）的记录，只引导用户使用软件内的清理入口；云盘文件夹只给“只留云端”的官方做法。
- 需要 sudo 的对象（本地快照、系统级缓存）自己不用 sudo，只合成一两行命令交给用户执行。
- 汇报时说明覆盖率和跳过的部分，不要把“扫描完成”说成“全盘清理完成”。

业务资料还应区分原件、转换版、内嵌附件与历史导出，先测重复量和无损压缩潜力，不按文件夹名称或目标体积猜可删量。旧媒体、HEIC 和 PDF 副本有原路径依赖时，可按 [agent-artifacts.md](references/agent-artifacts.md) 评估 APFS 共享块。它会改变 inode；不用于资源库、同步目录或活动素材，先试点再分批实测，不能把名义重复体积当成已释放。

## 空间巡检（不装定时任务）

`python3 scripts/disk_watch.py [--notify] [--triage]`：一秒读完磁盘余量，和约一周前比；余量低于阈值或一周少得太多才算异常（退出码 10），`--notify` 弹通知，`--triage` 顺带跑一次分档体检。阈值写在本机配置 `watch`。Skill 不安装定时任务；用户想每天看一次，挂进他已有的定时任务。

清理中新认出的缓存或大目录，写回本机配置 `extra_tier1`（可再生缓存）/ `extra_tier3`（要问人的大目录），下次体检直接认识；只写本机配置，不改公开仓。

## 空间记录与增长调查

每次 `scan.py scan` 自动保存精简的空间记录点，并生成 `growth.json` / `growth.html`；只复用本次已测的目录元信息，不增加一轮遍历、不保存文件内容。正式清理后的磁盘实测余量另记一点，预演不记。记录保存在 `~/.sansheng/disk-cleanup/history/`，即使移走旧运行目录也能比较。

看板首页以容量条、已用空间构成和分类占用为主；已用与剩余只在双色容量条内标注，扫描时间单独显示、不换行。增长详情放左侧“空间变化”，设置详情默认收起，避免长段说明占据首页。

用户问“最近为什么少了几十 GB / 谁在快速占空间”时，直接调查增长，不必先进入删除流程：

```bash
python3 scripts/space_history.py import                         # 一次性补入既有扫描与正式回执
python3 scripts/space_history.py list
python3 scripts/space_history.py compare --run <本次运行目录>    # 自动跳过不可比的局部扫描
python3 scripts/space_history.py compare --run <本次运行目录> --baseline <指定scan_id>
```

默认比较最近一次同电脑、同磁盘容量、同主要根范围和扫描配置的记录；调查一周增长时可指定更早基准。旧记录缺少磁盘设备身份时只依据已有电脑、路径与容量，不能声称已核验卷身份。两次都实际量到、且无扫描缺口的同一目录才计算增减；上次未单列不能按零，父子目录不能重复累计。将磁盘余量变化、已定位目录净变化和尚未归因的差额分开说，别把差额推断成系统垃圾。清理后若只记了磁盘余量，不能冒充目录也重新扫描过。

调查不能止于已知目录清单：先对齐整盘、卷和目录统计口径；差额大时检查本轮未覆盖的位置（包括由系统发现的用户临时区）、APFS 共享块、快照、已删除但仍打开的文件，以及扫描期间运行的构建。发现大量重复应用克隆时按 browsers 卡追到启动与退出流程，不能只说“系统缓存”或把名义体积当成可释放量。发现新位置只增加只读调查，不自动开放删除。

从增长最大的目录追到产生它的任务、工具与用途，读取仍走 `peek.py`。可将已经查证的解释写入本轮 `growth-notes.json`（`{"绝对目录路径":"解释与证据"}`）后重跑 compare，随报告展示。新增占用不等于垃圾，不改项目状态、不授予删除权限。用户选择“继续查”后继续核查并交付证据，不重复问已回答的同一道题。

已核查项可写本轮 `review-status.json`（`schema: review-status/1`、`scan_id`、`entries: [{id,status:"investigated",summary}]`），并交付本轮 `review.html`，再生成看板。看板会展示已核查数量，不再重复询问这些方向；该文件仅用于展示，不能改变 verdict、封存或删除授权。

## 知识卡

`references/cards/` 中，每张卡说明一类对象：存储位置、怎么判断还在用、原生清理方式、不能做的事、通常只需问用户的那一句话。

| 卡 | 对象 |
|---|---|
| claude-code、codex、grok、gemini-antigravity、cursor | AI Agent 的会话、生成图、版本与状态 |
| model-caches、package-managers | 模型权重、浏览器内核、各类包缓存 |
| android、xcode、docker | 模拟器、SDK、镜像、构建缓存 |
| git-worktree-build、pipelines | 工作树、构建产物、自建流水线的中间稿与快照 |
| macos-system、chat-netdisk | 系统快照与缓存、聊天软件与网盘 |
| browsers、app-updaters-installers、creative-media | 浏览器、更新暂存与安装包、剪辑设计音乐游戏 |
| large-old-files | 下载、桌面、文稿、影片里长期不用的大文件 |
| agent-junk-locations | 各家 Agent 与自建流水线固定落盘位置、工作树 ignored 目录比对、完成标记 |

本机私有信息写在 `~/.sansheng/disk-cleanup/profile.json`，不放进本 Skill：引用检查目录、保护路径、设置页答案、项目台账、认下的目录、记忆等。格式见 contract.md 第 1 节。
