---
name: sansheng-disk-cleanup
description: 面向 AI 重度用户的 macOS 磁盘清理，会读懂你的电脑：第一次用一页选择题认识用户，按"谁产生、为哪个任务、是否结束、谁还在用"判断 Agent 会话、批量生成图、工作树中间稿、构建暂存、模拟器与模型缓存等能否清理，认不出的大目录会先调查；生成叁笙品牌选择看板，用户确认后复核执行（可先放废纸篓或归档到外接硬盘）并实测释放空间。腾空间、清理电脑、磁盘快满、调查异常占用时使用；Windows 尚未支持。
---

# 叁笙清理

把真正不再需要的东西找出来，讲清依据和后果，用户确认后安全执行，并实测释放了多少。不安装常驻软件，也不设定时任务。

**少打扰用户**：扫描前不问问题；第一次使用发一页设置选择题（只发一次），之后大多数时候在对话里用一句话确认，要用户看的东西多时才发清理看板；确认后预演、试点、正式执行由脚本连着做完，只有出异常才停下来问。

**核心原则**：判断由你（Agent）根据证据来做，脚本只负责采集事实和安全执行。"多久没动""可以重新生成""没有会话文件"都不能单独作为删除理由；每条建议写出来之前，都要先做引用检查。**用户的设置和你的记忆只改变“问不问、预填什么、进哪一档”，不改变“能不能放行”。** 完整准则见 [references/judgment.md](references/judgment.md)，必须先读。

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
   - 标“常用”的工具和标“进行中”的项目跳过深查；标“不用了”的工具、“做完了”的项目重点查。
   - 认不出的大目录先调查，再用 `memory.py identity set` 记成本机身份卡（judgment.md 第 11 节）；用 `python3 scripts/scan.py probe done-evidence --run <run目录> --path <目录>` 找“做完了”的证据（第 12 节）；有长期价值的素材给“归档”选项（第 13 节）；会一直涨的来源按用户的 `governance_mode` 写治本建议（第 14 节）。
   - 然后进入下面的“扫描与判断”（扫描已经做过，从“逐组判断”接着做）。

### 以后每次

扫描 → 判断与写计划 → 校验、封存、提案 → 清理看板或对话确认 → 执行 → 成果页。新出现的项目、标成“进行中”却很久没动的项目，在对话里或看板上单题问一句；设置不再重发。

**没有可直接删除的项目，也必须交付判断入口**：不得把“没有通过校验的可执行项”说成“没有东西可清理”，也不得用磁盘余量充足作为结束理由。此时仍生成并交付看板：说明哪些确实在用、哪些只是尚缺证据。对于只因任务状态、用途或副本不明而暂时保留的对象，按具体任务拆卡，`verdict: keep` 并写 `question`；看板会给“想清理，继续查 / 保留 / 暂不确定”的选择。不要把所有未判断的大目录合成一张“其他资料”后结束。

看板的“复制核查答复”与清理清单分开：抬头为“叁笙清理 · 待核查答复”，**不是删除授权**。用户选“想清理，继续查”后按具体对象补查，重新写计划、校验并交付清理清单；仍在运行、绝对保护和确有依赖的对象继续保留，不因答复而放宽条件。大项目“进行中”不证明其中每个旧子任务都还要用；可以询问具体旧任务是否需要清理，但不能自动改整个项目状态或直接删除。没有待核查项时也展示占用与保留依据，不发送空的“回复好就删”邀请。

## 扫描与判断

1. **扫描**（只读）：
   ```sh
   python3 scripts/scan.py scan            # 默认阈值 500MB、引用检查 900 秒（大仓库可能要 10 分钟以上）
   python3 scripts/scan.py probe projects --run <run目录>   # 每次必跑
   ```
   读脚本打印的摘要。出现“引用检查有 N 个搜索根没查完”时，先加大预算重扫（`--refs-seconds 1800`）；出现“没有可搜的引用检查目录”时，节点显示“引用未查”，只能保留或待定，先补成品目录再用 `probe item-refs` 补查。`/private/tmp/claude-<uid>`（Claude Code 会话临时目录）存在时默认也会扫，当前会话与 24 小时内有改动的一律保留。每个前沿节点带 git 角色、是否被打开、24 小时改动数、引用命中数、知识卡名、与上次的增长量，认下的目录还带 `identity`。

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

**按 `propose` 打印的“方式”走，不自己改判**：
- **在对话里确认**（`mode=chat`）：把它打印的那段话原样发给用户；回“好 / 稳妥 / 都删”就运行对应那一行 `act.py go --choice`；要看细节再发看板。发看板的提案 `go --choice` 一律拒绝。
- **发清理看板**：要用户看的项超过 3 项、含交互会话、或用户设了“每次都发看板”。运行 `python3 scripts/plan.py board --run <run目录>`，把路径交给用户，用几句话说能腾出多少、要用户决定什么。看板顶部写着“按你的设置……”，新项目和久没动的项目是单题卡，待确认规则每条两个按钮，卡片选项是删 / 留 / 归档（选归档要填外接硬盘路径）。用户点“复制清单，发给 Agent”后，把整段文字存成文件交给 `act.py go --from-board`。
- **新项目与久未活动**：`proposal.project_questions` 在对话里用一句话问，用户回答后 `plan.py project --key <项目键> --status done|active|archived|protected`。
- **飞轮提问**：提案末尾“以后都这样吗”，用户回“以后都删 / 以后别列”就运行它打印的 `plan.py rule --key … --reply delete|keep`；回“不用”用 `--reply never`；撤销用 `--reply forget`。
- **云盘建议**（`proposal.cloud_suggestions` 非空）：只给建议和官方入口，不动云盘文件；用户自己动手前提醒别直接删除。
- **治本**（`proposal.governance_mode`）：`tell` 只说现象，`guide` 给改法，`fix` 写“同意后我来改，可撤销”；改完用 `memory.py govern record --path P --change … --undo … --bytes N` 记一笔，下次扫描 `memory.py govern check --run <run目录>` 核对有没有停涨。

## 执行

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
- 保护名单由 `scripts/protect.py` 强制执行，不得绕过，也不得用 shell 的 `rm -rf`。不在封存计划里的路径一律不动。
- 原生清理命令优先（`sdkmanager --uninstall`、`codex delete`、`hf cache rm`、`brew cleanup`、`uv cache prune`），它们不进废纸篓、不归档。**不要推荐 `claude purge`**：它会连记忆与配置一起删，`protect.py` 有意不登记它。工具自带的保留设置写进 `governance`，经用户同意后再改。
- 聊天软件（微信、企业微信等）的记录，只引导用户使用软件内的清理入口；云盘文件夹只给“只留云端”的官方做法。
- 需要 sudo 的对象（本地快照、系统级缓存）自己不用 sudo，只合成一两行命令交给用户执行。
- 汇报时说明覆盖率和跳过的部分，不要把“扫描完成”说成“全盘清理完成”。

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

本机私有信息写在 `~/.sansheng/disk-cleanup/profile.json`，不放进本 Skill：引用检查目录、保护路径、设置页答案、项目台账、认下的目录、记忆等。格式见 contract.md 第 1 节。
