---
name: sansheng-disk-cleanup
description: 面向 AI 重度用户的 macOS 磁盘清理：自顶向下找出大占用，按"谁产生、为哪个任务、是否结束、谁还在用"判断 Agent 会话、批量生成图、工作树中间稿、构建暂存、模拟器与模型缓存等能否清理，生成叁笙品牌选择看板，用户确认后复核执行并实测释放空间。腾空间、清理电脑、磁盘快满、调查异常占用时使用；Windows 尚未支持。
---

# 叁笙清理

把真正不再需要的东西找出来，讲清依据和后果，用户确认后安全执行，并实测释放了多少。不安装常驻软件，也不设定时任务。

**少打扰用户**：扫描前不问问题；第一次使用先发一个轻量的 ① 项目台账看板（认识用户的项目，一分钟点完），之后大多数时候在对话里用一句话确认，要用户看的东西多时才发 ② 清理看板；确认后预演、试点、正式执行由脚本连着做完，只有出异常才停下来问。

**核心原则**：判断由你（Agent）根据证据来做，脚本只负责采集事实和安全执行。"多久没动""可以重新生成""没有会话文件"都不能单独作为删除理由；每条建议在写出来之前，都要先做引用检查。完整准则见 [references/judgment.md](references/judgment.md)，必须先读。

## 流程

1. **扫描**（只读）：
   ```sh
   python3 scripts/scan.py scan            # 默认阈值 500MB、引用检查 900 秒（大仓库可能要 10 分钟以上），结果写到 ~/.sansheng/disk-cleanup/runs/<scan_id>/facts.json
   ```
   读脚本打印的摘要；出现“引用检查有 N 个搜索根没查完”的警告时，先加大预算重扫（默认 900 秒，如 `--refs-seconds 1800`），否则相关节点只能保留或待定。出现“没有可搜的引用检查目录”的警告（设置里的引用检查目录为空、节点又不在 git 工作树里）时，节点显示“引用未查”而不是“引用 0”，同样只能保留或待定：先在设置 / 台账里补上成品所在目录，再用下面第 3 步的 `probe item-refs` 补查。前沿节点按体积降序排列，每个节点附带 git 角色、是否被打开、24 小时改动数、引用命中数、对应的知识卡名，以及和上次扫描相比的增长量。

2. **逐组判断**：节点的 `producer.card` 就是知识卡名，读 `references/cards/<card>.md`（没有 card 的节点按 judgment.md 四个问题自己判断，拿不准写 `decide`）；需要逐项事实时运行探针：
   ```sh
   python3 scripts/scan.py probe codex-groups                         # Codex 会话按来源归组；默认只列 200MB 以上的组，全面清点加 --min-bytes 0
   python3 scripts/scan.py probe codex-groups --trace 3 --emit <run目录>/items > <run目录>/codex-g3.json   # 第 3 组（从 0 数）逐线程核对并生成条目文件；一次一组，每组各跑一次（工作目录在家目录外时加 --search <成品目录>）
   python3 scripts/scan.py probe codex       # 100MB 以上的单个会话与生成图：线程名、cwd、是否打开、引用
   python3 scripts/scan.py probe claude      # 各项目体积、cleanupPeriodDays、旧版本
   python3 scripts/scan.py probe android     # 系统镜像是否被 AVD 引用
   python3 scripts/scan.py probe worktrees --repo <仓库>   # 只接受 git 仓库；普通目录看 git-worktree-build 卡的标记文件表（节点的 project_marker）
   python3 scripts/scan.py probe grok
   python3 scripts/scan.py probe system      # 完全磁盘访问、本地快照、三口径容量、系统数据；probe updaters 看更新暂存，probe leftovers 看已卸载软件遗留
   python3 scripts/scan.py probe bigfiles    # 下载、桌面、文稿、影片里 90 天没动过的 200MB 以上文件（知识卡 large-old-files，只能 decide）
   python3 scripts/scan.py probe projects --run <run目录>   # 每次必跑：项目台账归属靠它（git 仓库、AI 会话工作目录、开发环境、成品目录），写 <run目录>/projects.json（只读；提案会据此问新项目与久未活动的项目）
   ```
   **`probe projects` 每次扫描后都要跑**：台账里有“进行中”或“别碰”的项目、而这次扫描没有它的结果（或结果来自别的扫描）时，`check` 会让所有卡片降为保留（会话号、Claude 项目目录要靠它才认得出归属），报错提示先跑这条。
   **第一次使用先认识项目**：扫描后读 `profile.json`，没有 `onboarded_at` 就是第一次（`plan.py propose` 打印的 `stage` 也是 `onboarding`）。先 `probe projects`，再 `python3 scripts/plan.py onboard --run <run目录>` 生成 ① 看板交给用户（只有项目表，没有任何删除选项，用一两句话说明“标一下状态，下次清理就按你的习惯来”）。用户回传的文本抬头是“叁笙清理 · 项目台账”：存成文件，`python3 scripts/plan.py profile --apply <文件>` 保存（保存设置，不是清理授权）。用户在对话里说“跳过设置”，就写一份只含 `台账: {"项目":{}}` 的文件同样 apply（全默认，会写 `onboarded_at`，以后不再弹 ①）。之后**按台账判断**：标“进行中”“别碰”的项目不用细查（省探针时间，`check` 也会整卡保留），标“做完了”“归档”的项目才查中间稿、会话和构建产物。`onboarded_at` 已有的（cleanup 阶段）不发 ①。**台账（含各项目的成品目录）保存后、`check` 之前，先跑一次 `probe item-refs`**：成品目录会并进引用检查范围，而扫描时还没搜过这些目录，`check` 会把受影响的卡降为保留（“台账或设置新增了成品目录……需要补查引用”），`item-refs` 按新范围补查并记录搜过哪些目录，之后才放行。首次流程因此是：扫描 → `probe projects` → ① 看板 → 保存台账 → 写计划 → `probe item-refs` → `check`。
   **AI 工具的会话不能只看单个体积**：批量脚本（如 `codex exec` 逐张出图）每次只留几 MB，却能累积几十 GB，必须先归组再判断。探针给出的分类只是事实，结论要读样例路径再下，见知识卡。**交互对话（人和 Agent 来回工作的会话）一律逐个出卡、逐个问**，不能因为没出图、没被引用就批量判 `clean`。

   先读本机偏好 `~/.sansheng/disk-cleanup/profile.json`：`projects` 是项目台账（进行中 / 做完了 / 归档 / 别碰，各项目自己的成品目录），`retention_days` 是用户定的“最近 N 天内用过的 AI 对话一律留着”（`check` 会强制，交互会话在期内整卡保留，不必再写成卡），`rules` 是用户认可过的规则（`propose` 自动套用）。全局设置只剩保留天数和默认档位，成品目录按项目记（`deliverable_roots` 是它们并起来的副本位置）。台账状态怎么用见 judgment.md 第 8 节：只决定问不问、预填什么、进不进推荐档，放行仍靠副本与引用核对。按 [judgment.md](references/judgment.md) 的四个问题，在 run 目录写 `plan.json`。格式见 [references/contract.md](references/contract.md) 第 4 节；条目多时用 `items_file` 引用探针生成的文件，不要手抄。同一软件或同一任务合成一张卡，持续增长的来源写进 `governance`。卡片文字给用户看，用用户认得出的说法：`title` 写“这是什么”（会话写它的标题或任务名，不写编号）；`summary` 写“为什么会有”（谁、在做什么时产生的）；`impact` 写“删了会怎样”；不出现节点、前沿、引用样本等内部词。`cards_used` 第一项决定看板左侧目录的类别，需要时用 `category` 改归类（如 Codex 出图写 `ai-images`），类别见 contract 第 7 节。给每张 `clean`/`decide` 卡写 `preset`（`safe` 删了不丢东西 / `recommended` 有副本或任务已做完 / `all` 删了找不回），看板据此提供一键三档，规则见 contract 第 7 节。结束证据用对象形式（`merged / delivered / copy_verified`，`user_stated` 不算），引用核对用 `refs_reviewed` 对象，见 contract 第 4 节。

3. **校验、封存、提案**：
   ```sh
   python3 scripts/scan.py probe item-refs --run <run目录>   # 计划里落在大目录内部、或正好是未逐项检查的大目录本身（如模型目录、SDK 目录）的条目，按自己的路径补查引用（通常一两分钟）
   python3 scripts/plan.py check   --run <run目录>
   python3 scripts/plan.py seal    --run <run目录>
   python3 scripts/plan.py propose --run <run目录>     # 写 proposal.json，打印“方式”、一段给用户的话和三档对应的命令
   ```
   `item-refs` 查到引用的条目会被 `check` 降为保留：打开 `item_refs.json` 里该条的 `samples` 看一眼，只是历史记录、验收日志、进度文档提到过的，在卡片 evidence 里写 `{"kind":"refs_reviewed","samples":[原样抄样本],"verdict":"history_only"}` 再 check；还在被用的写 `depends`。零引用的条目不再带“没能确认”的提示。

   `check` 会把不满足条件的卡片降级（附 `judgment_refs` 引用准则原句）：缺结束证据、含交互会话、更新暂存未被探针判为 superseded、高风险用户数据降为 `decide`；被引用而未核对、正在改动或被打开、路径受保护、聊天与网盘内容降为 `keep`。完整条件见 contract.md 第 4 节。降级原因要如实告诉用户。`check` 的输出按卡、按原因汇总（每种原因最多列 3 个示例路径），完整明细在 `plan.check.json`（各卡的 `errors`、`error_summary[].paths`）；扫描遍历阶段每 30 秒会向 stderr 打一行进度，不是卡住。

   **按 `propose` 打印的“方式”走，不自己改判**：
   - **在对话里确认**：只有 `propose` 打印“方式：在对话里确认”（`proposal.json` 的 `mode` 是 `chat` 且 `stage` 不是 `onboarding`）时才用。把它打印的那段话原样发给用户（可以补一两句降级原因）。用户回“好 / 稳妥 / 都删”，就运行对应那一行 `act.py go --choice`；用户说要看细节，再发看板。发看板的提案（要用户看的项多、含交互对话、第一次使用）`go --choice` 一律拒绝（退出码 2），也不会列出含交互对话的卡，必须走看板清单。
   - **发 ② 清理看板**：要用户看的项超过 3 项，或含交互会话时（第一次使用已经发过 ① 了）。运行 `python3 scripts/plan.py board --run <run目录>`，把看板路径交给用户，并用几句话说明能腾出多少、要用户决定什么。看板没有设置页，左侧常驻“我的项目”可以随时改状态。用户勾选后点“复制清单，发给 Agent”，会发来抬头“叁笙清理选择”的文本（`scan_id`、`seal`、所选卡片、回答、`sel`），改过设置或项目状态时还有“设置:”“台账:”行，隐藏某类还有“别再列:”行。**把整段文字存成文件，直接交给 `act.py go --from-board`（第 4 步）**，设置/台账的保存和重新校验它自己会做，不必先手动 `profile --apply`。
   - **新项目与久未活动**：cleanup 阶段不再发 ①。`propose` 的 `project_questions` 里有新项目（台账里没有的）或“进行中”超过 30 天没动的项目时，在对话提案末尾用一句话问（“新发现 X、Y，标一下状态？”“Z 做完了吗？”），② 看板顶部也会带同样的提示；用户回答后用 `python3 scripts/plan.py project --key <项目键> --status done|active|archived|protected` 更新。
   - **飞轮提问**：`propose` 的文案末尾可能带“以后都这样吗”的问题（同一类东西连续 3 次做了同样的选择才会问）。用户回“以后都删 / 以后别列”就运行它打印的 `plan.py rule --key … --reply delete|keep`；回“不用”用 `--reply never`（60 天内不再问）；只回了清理档位、没理这个问题就什么都不记。用户说要撤销某条规则用 `--reply forget`。

4. **执行**（只在用户本轮明确回复或发来看板清单之后）：
   ```sh
   # 看板清单：用户发来的整段文字存成文件，一条命令走完（校验 → 保存设置/台账 → 预演 → 试点 → 正式执行 → 成果页）
   nohup python3 scripts/act.py go --run <run目录> --from-board <清单文件> > <run目录>/go.log 2>&1 < /dev/null &

   # 对话确认：用户回了哪一档，就跑哪一档（卡号、答复、sel 都从 proposal.json 读，不要转抄）
   nohup python3 scripts/act.py go --run <run目录> --choice recommended > <run目录>/go.log 2>&1 < /dev/null &

   # 备用（go 出问题时手动分步）：参数从清单原样抄，三条依次跑，前一条正常结束（退出码 0）再跑下一条
   python3 scripts/act.py run --run <run目录> --seal <digest> --sel <sel> --cards c01,c03 --answers c05=删 --dry-run
   python3 scripts/act.py run --run <run目录> --seal <digest> --sel <sel> --cards c01,c03 --answers c05=删 --pilot
   nohup python3 scripts/act.py run --run <run目录> --seal <digest> --sel <sel> --cards c01,c03 --answers c05=删 > <run目录>/run.log 2>&1 < /dev/null &
   ```
   - `go` 依次跑 预演 → 试点 → 正式执行，每步打印 `【go】…`（放后台时看 `go.log`）。预演有失败条目或没有可执行条目、试点那一条没删成功，就以退出码 3 停下，后面的步骤不执行；这时把日志里的原因告诉用户再决定。**正式执行有条目没删成功时退出码是 4**（其余已完成）：告诉用户哪几条失败及原因（`act.py status --run <run目录>`），不要报“全部完成”。退出码汇总：0 完成；2 拒绝（清单 / 提案不合格，什么都没删）；3 停下请用户看；4 正式执行有条目失败。
   - **`--from-board` 的停点**：抬头是“叁笙清理 · 项目台账”的文本（① 的导出）会被拒绝（退出码 2，什么都不删），提示改用 `plan.py profile --apply`；清单的 `scan_id`、`seal` 与这次扫描不符，或 `sel` 与清单里的卡片和回答对不上（被改过），也是退出码 2。清单带“设置:”“台账:”行时先保存，保存后影响判断的设置变了就自动重新 `plan.py seal`（内含 check）：封存 digest 不变，清单仍有效，继续执行；digest 变了，以退出码 3 停下，打印原因和重新生成看板的命令（`plan.py propose`、`plan.py board`）——原因分两种：设置 / 台账让卡片的判断变了（列出卡号），或判断没变但卡里目录的内容在封存之后又变了（体积、文件大小、修改时间是封存内容的一部分，目录在变化就会换 digest，不会沿用旧封存里的数值）。把变化告诉用户，发新看板，不要拿旧清单硬执行。设置里有路径没保存成功，同样退出码 3。
   - **设置闸门**：`seal` 会记下封存时影响判断的本机设置（保留天数、保护路径、成品目录、引用检查目录等）。封存后这些设置改过，`run`（预演、试点、正式执行都算）和 `go` 一律拒绝（退出码 2），提示重新 `plan.py check` / `seal`；旧版本的封存没有这条记录，同样要重新封存。重新封存后卡片没变，`seal` 的 digest 就不变，用户发来的清单仍然有效；digest 变了，就要让用户重新确认。
   - **闸门**：试点与正式执行必须带 `--sel`；正式执行还要求同 seal + sel 的预演和成功试点（试点自动取待定卡或最大的那一条）。确需跳过试点加 `--skip-pilot --reason "<原因>"`。
   - **正式执行放后台**：条目上百或有几百次原生命令时前台会超时；用 `nohup` 脱离当前进程，定时看日志。中断后可以直接重跑，已删的自动跳过。
   - 执行过 `codex delete` 后会自动分批回收 Codex 历史库空间；被打断用 `act.py reclaim` 补做。

   执行时会逐项重新复核（路径身份、保护名单、进程占用、24 小时内的改动、被跟踪文件），并写出 `receipt.json`。正式执行结束后脚本自动生成成果页 `report.html`（`go` 和手动 `run` 都一样，补做用 `python3 scripts/report.py --run <run目录>`）：前后对比图、各类腾出多少、从源头改好的事、分享图和二维码。第一次清理的成果页会带一句“好用的话，去 GitHub 点个 Star”，之后不再出现，不需要你另外提。**清理完把成果页路径交给用户**，用一两句话说腾出多少、其中多少是 AI 工作留下的。**向用户报告"按条目核算释放"**（逐文件字节 + 原生命令删掉的对象 + 历史库回收），同时给出磁盘余量变化。两者对不上时，先查同时运行的构建、测试、下载（余量会被它们抵消），不要把余量差直接报成释放量；再报告跳过和失败的原因。

   **反馈（只在脚本提示时）**：脚本输出“这次有 N 个大目录认不出来源，可运行 feedback.py issue-url 生成反馈”时，在汇报末尾问用户一句：这次有 N 个大目录我认不出来源，要不要生成一份反馈，帮这个工具认得更多？
   - 用户同意：运行 `python3 scripts/feedback.py issue-url --run <run目录>`，把输出的“将要提交的全文”原样给用户看，确认后把预填链接给用户，由用户自己点开、自己按“提交”。
   - 用户上不了 GitHub：把同一段文字复制给用户，让对方粘贴到 https://sanshengai.top/tools/disk-cleanup/feedback/ 或发到公众号后台。
   - 用户说“不用”：运行 `python3 scripts/feedback.py decline`，90 天内不再问。

   反馈只含路径模式（用户名换成 `~`，不是众所周知的目录名换成 `<目录>`）、体积、软件名、Skill 与 macOS 版本，不含文件名、文件内容、项目名。绝不代用户提交，绝不自动发送。

## 硬约束

- 只有用户在本轮明确选择的才能执行：对话里回复了提案中的某一档，或发来看板清单。历史授权不覆盖新的对象，"打开看板""看了提案"都不等于授权。
- 项目台账与项目状态只决定问不问、预填什么、进不进推荐档，**不能单独作为删除依据**；用户标的状态是 `user_stated`，不能替代副本核对、交付证据、引用检查。台账导出文本（抬头“叁笙清理 · 项目台账”）只保存设置，从不是清理授权。
- 保护名单由 `scripts/protect.py` 强制执行，不得绕过，也不得改用 shell 的 `rm -rf` 删除大目录。不在封存计划里的路径一律不动。
- 原生清理命令优先，例如 `sdkmanager --uninstall`、`codex delete`、`claude purge`、`hf cache rm`、`brew cleanup`、`uv cache prune`。工具自带的保留设置写进 `governance`，经用户同意后再改。
- 聊天软件（微信、企业微信等）的记录，只引导用户使用软件内的清理入口；浏览器与聊天软件按缓存 / 用户数据 / 账号内容三层处理，见 judgment.md。
- 需要 sudo 的对象（本地快照、系统级缓存）Skill 自己不用 sudo，只合成一两行命令交给用户执行。
- 汇报时说明覆盖率和跳过的部分，不要把"扫描完成"说成"全盘清理完成"。

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

本机私有信息写在 `~/.sansheng/disk-cleanup/profile.json`，不放进本 Skill，包括：引用检查要搜的额外目录、用户追加的保护路径、自建流水线的说明。格式见 contract.md 第 1 节。
