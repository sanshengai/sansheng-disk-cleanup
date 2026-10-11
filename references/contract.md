# 数据契约与安全规则（v0.5）

三段流水线共用本契约：`scan.py` 产出事实 → Agent 写 `plan.json` → `plan.py` 校验、封存并渲染看板 → 用户选择 → `act.py` 复核并执行。所有脚本只用 Python 3.9+ 标准库，macOS 优先，路径一律 `os.path.realpath` 规范化后再比较。

运行目录默认 `~/.sansheng/disk-cleanup/runs/<scan_id>/`（可用 `--runs-dir` 覆盖），本机画像 `~/.sansheng/disk-cleanup/profile.json`（可选）。

## 1. 本机画像 `profile.json`（可选，私有，不进发行包）

```json
{
  "roots": ["~"],
  "extra_roots": ["/Applications"],
  "reference_roots": ["~/Projects"],
  "protect": ["~/Documents/重要"],
  "notes": {"~/.cache/site-build": "网站构建暂存，发布完成后可清"}
}
```

- `roots`：自顶向下计量的起点，默认 `["~"]`。
- `reference_roots`：做引用检查时额外搜索的目录（git 仓按 `git grep` 搜跟踪文件；非仓目录用 `rg`，没有就用 Python 遍历文本文件，单文件 >2MB 跳过）。
- `protect`：用户追加的保护路径（前缀匹配）。
- `deliverable_roots`（可选）：成品目录。`copy_verified` 证据的副本若不在 git 跟踪的文件里，就必须位于这些目录之内。设置行 `成品` 或台账里某个项目的 `成品` 写入成品目录时，同时追加进 `reference_roots`（0.5 起成品目录按项目记在 `projects[*].deliverables`，由有效 profile 并进来）。
- `notes`：路径 → 一句说明，原样带入事实，供 Agent 判断。
- `onboarded_at`（可选）：完成初次设置的时间，设置或台账成功应用过一次（含全默认的空台账）就写入。没有它就是第一次使用：`plan.py propose` 的 `stage` 为 `onboarding`，一律走看板，且应先发 ①项目台账看板（第 7d 节）；有它则 `stage` 为 `cleanup`，不再发 ①。
- `retention_days`（可选，0/7/15/30）：最近 N 天内有改动的交互会话，`check` 整卡降为 keep；0 或缺省不按天数。看板上显示的当前值取实际设置（没设就是 0，显示“不按天数”并选中），不会替用户默认成 15。
- `default_choice`（可选，safe/recommended/all）：对话里回“好”对应的档，缺省 recommended。
- `rules`（可选）：`{规则键: {"action": "delete"|"keep", "at", "spec"}}`。规则键 = “看板类别/`cards_used` 第一项/档位”（如 `ai-images/codex/recommended`），由 `check` 算出并写进封存；卡片不能自定义，没写 `cards_used` 的卡不参与规则。`delete`：这一类不再算“要你看”，但仍只进它自己那一档及以上，且对降过级、带风险提示或含交互会话的卡不适用；`keep`：这一类不进任何一档、文案不再提。
- `cooldown`（可选）：`{规则键: 截止时间戳}`，用户回“不用”后 60 天内不再问。
- `projects`（可选，0.5 项目台账）：`{项目键: {name, status, deliverables[], updated_at, source}}`。项目键 = 规范化绝对路径（`realpath`，必须是家目录下已存在的目录），环境项目用 `env:android` / `env:xcode` / `env:docker` / `env:models`（各自占的位置见 `protect.ENV_PROJECT_ROOTS`）。`status` ∈ `active`（进行中）/ `done`（做完了）/ `archived`（归档）/ `protected`（别碰）；`deliverables` 是该项目自己的成品目录（`~/…`，家目录下已存在的目录）；`source` = `board`（用户在台账看板或对话里标的）。**有效 profile**：`protect.load_profile()`（scan / plan / act 一律用它）把 `protected` 项目的目录并入 `protect`，把各项目的 `deliverables` 并入 `deliverable_roots` 与 `reference_roots`（副本核对与引用检查范围）；这只发生在内存里，`prefs.load()` 读原始内容、改写后存盘，折进去的部分不会写回文件。
- 以上几项由 `plan.py profile --apply`（看板导出的“设置:”“别再列:”行）与 `plan.py rule` 写入；“别碰”“成品”只接受家目录下已存在的子目录。`plan.py project --key <项目键> --status <状态>` 单独改一个项目的状态（回答“X 做完了吗”）。`onboarded_at` 在设置或台账**成功应用过一次**时写入（台账全是默认状态、没有一行需要改也算）。
- 同目录 `choices.jsonl`：每次正式执行后按 `cardkeys.json`（`propose` / `board` 写出）记一行 `{scan_id, at, key, chosen, bytes, title}`，一类里的卡全被选中才算删。卡片属于某个项目时（封存记录里的 `projects`），再多记一行带项目前缀的键 `project:<sha256(项目键)[:8]>/<原规则键>`（`prefs.project_rule_key`），原规则键的格式不变；带项目前缀的键目前只记录，不参与“以后都这样吗”的提问。同一规则键在最近 3 次不同 scan_id 里同向，且没有规则、不在冷却期，`propose` 才在文案末尾问一次（成长飞轮规范版本 1）。

## 2. 事实 `facts.json`（scan.py 产出）

```json
{
  "schema": "facts/1",
  "scan_id": "20261010T153000Z-ab12cd",
  "created_at": "2026-10-10T15:30:00Z",
  "host": {"platform": "darwin", "user": "alex", "home": "/Users/alex"},
  "disk": {"total": 0, "free": 0, "used": 0},
  "threshold": 524288000,
  "coverage": {"walked_bytes": 0, "frontier_bytes": 0, "incomplete": false, "seconds": 0.0},
  "nodes": [
    {
      "id": "n0001",
      "path": "/Users/alex/.codex/sessions",
      "bytes": 0,
      "files": 0,
      "newest_mtime": "2026-10-10T14:12:00Z",
      "recent_24h": 0,
      "children_over_threshold": 0,
      "remainder_bytes": 0,
      "git": {"repo": null, "worktree": null, "branch": null, "role": "none", "tracked_files": 0, "merged_into_origin_main": null, "dirty_files": null},
      "open_by": [{"pid": 123, "command": "codex"}],
      "refs": {"checked": true, "count": 0, "samples": ["skills/x/README.md:12"]},
      "producer": {"tool": "codex", "card": "codex", "hint": "Codex 会话 rollout"},
      "growth": {"prev_scan_id": null, "prev_bytes": null, "delta_bytes": null},
      "note": null,
      "protected": false
    }
  ],
  "skipped": [{"path": "/Users/alex/Library/Mail", "reason": "permission"}]
}
```

规则：

- **计量**：用 `os.scandir`，`st_blocks*512` 计实际分配；不跟随符号链接；不跨设备（`st_dev` 与根相同）；`nlink>1` 的文件按 `(st_dev, st_ino)` 只计一次。权限错误记入 `skipped`，不中断。
- **前沿节点**：大小 ≥ `threshold`、且没有任何子目录 ≥ `threshold` 的目录，或已到最大深度（默认 10）。每个前沿节点的父链上，非前沿的大目录也要输出，并带 `remainder_bytes`（自身减去大子目录之和）。以下目录视为原子，不再下钻：`.git`、`node_modules`、`*.app`、`*.photoslibrary`、`*.avd`、`DerivedData`、`.venv`。
- `recent_24h`：节点内 24 小时内修改过的文件数。`newest_mtime`：节点内最新修改时间。两者在遍历时顺带计算。
- **git**：从节点向上找 `.git`（目录或文件均可）。`role` 取值：
  - `tracked`：节点内全部是跟踪文件；
  - `ignored`：没有跟踪文件，且 `git check-ignore` 命中；
  - `untracked`：没有跟踪文件，也未被忽略；
  - `mixed`：既有跟踪文件，又有非跟踪文件；
  - `none`：不在仓库中。

  另记 `tracked_files` 数量。节点本身是工作树根时，记 `branch`、`merged_into_origin_main`（`git merge-base --is-ancestor HEAD origin/main`）、`dirty_files`（`git status --porcelain` 非忽略行数）。
- `open_by`：一次性运行 `lsof -n -P -F pcn` 解析全部打开文件，按路径前缀归到节点；`lsof` 不可用时整体标 `"unknown"`。
- **refs（引用检查）**：对每个前沿节点生成路径变体：绝对路径、`~/` 相对路径、所在仓库的相对路径，以及 `basename`（仅当 basename 长度 ≥ 12，或含日期/哈希这类独特片段时）。到 `reference_roots` 与节点所在仓库搜索，每个仓库只跑一次 `git grep -F -l -f <模式文件>`，再把结果映射回节点。节点自身内部的文件不算引用。`samples` 最多 5 条 `文件:行号`。超时或失败时置 `checked=false`。**一个目录都没搜过**（`reference_roots` 为空、节点又不在 git 工作树里）时置 `checked=false, reason:"no-search-scope"`，不是“已查 0 处”；`plan.py check` 把它按“引用未查”降级，扫描摘要显示“引用未查”并打警告。`facts.json` 的 `coverage.refs_search` 记下扫描时实际搜过的目录：`{"roots":[规范化的绝对路径…],"worktree_scopes":bool}`（只含存在的目录）。扫描之后台账 / 设置新增了成品目录（有效 profile 的 `reference_roots` 里有扫描时没搜过的根），`check` 把查过引用的节点视为“引用未查”（错误提示“台账或设置新增了成品目录……运行 scan.py probe item-refs --run …”），用 `probe item-refs` 按当前引用检查目录补查后恢复；`item_refs.json` 的每条记录 `roots` 为实际搜过的目录，`check` 据此判断是否覆盖当前全部引用检查目录。没有 `refs_search` 的旧格式 facts 不做这项比较。
- `producer`：按 `scripts/producers.json` 的路径规则匹配，给出 `tool`、对应知识卡名 `card`、一句 `hint`；匹配不到时 `tool=null`。规则默认连子路径一起匹配；可选 `exact: true`（只匹配路径本身）和 `only: "file"`（只匹配已存在的单个文件，目录不匹配）。`large-old-files` 只套给下载、桌面、影片里的单个文件，这三个目录本身是入口提示，其下的子目录不套这张卡。
- `growth`：与 runs 目录里最近一次 `facts.json` 按路径对比。
- `protected`：命中第 5 节保护名单时为 true，此时照常输出事实，但标记不可清理。

## 3. 探针（scan.py probe <kind>，Agent 按知识卡调用）

只读输出 JSON 到 stdout。最少实现：

- `probe codex`：
  - 逐会话：`~/.codex/sessions/**/rollout-*.jsonl`，只读首行 `session_meta`（`cwd`、`source`、子 Agent 的 `parent_thread_id`）和第一条用户消息前 120 字，大文件流式读取、拿到即停；`session_index.jsonl` 里的 `thread_name`、`updated_at`；文件大小、最后修改时间、是否被打开。
  - `generated_images/<id>`：张数、字节数、是否有对应 rollout，以及引用检查（在 `reference_roots` 中搜 `generated_images/<id>`，返回被引用的具体文件名）。
  - 只列体积 ≥ `--min-bytes`（默认 100MB）的会话，以及前 `--top` 个图片目录；同时给出汇总。
- `probe codex-groups [--min-bytes 200MB] [--trace N] [--search 目录]... [--emit 目录]`：
  - 全部会话按 `source|originator|cwd 族` 归组（`…/scratchpad/子目录` 合为一族；子 Agent 归到最上层父会话的组），不设单个会话的体积门槛。每组给出线程数、会话文件 / 历史库（`thread_history_*.sqlite` 中 `thread_items` 按线程汇总）/ 原图三部分字节、活动时间范围、打开数、24 小时改动数、子会话数、组外子会话数、`cwd_breakdown` 与 3 条首句样本。组按总字节降序编号，只列 ≥ `--min-bytes` 的组。
  - 父子关系（`scripts/codex_state.py`，scan / plan / act 共用）：`state_*.sqlite` 的 `thread_spawn_edges` 与每个 rollout 首行 `session_meta` 里的父线程取并集。
  - `--trace N`：对第 N 组的顶层线程分类（子会话随父线程计入 `descendants`，不单独分类、不重复计体积；整棵树的原图一起参与分类）。`source=exec` 的组为批量组，分类 `active / copied / copied_elsewhere / partial / mentioned / unmentioned / empty / unverified`，并给出 `copy_where`（副本是否被 git 跟踪、是否有某级目录名是 rejected / failed / tmp / scratch 一类）、`last_event` 与 `checked`（副本、引用、会话三项检索是否查完）；其他组为交互对话组，只有 `active / conversation / unverified`，附首句与最后活动时间。定义见 cards/codex.md。
    - 副本按"大小相同 → sha256 相同"查找，`~/.codex` 自身不算；引用按线程 UUID 在搜索范围（cwd 所在 git 仓库根 + profile `reference_roots` + `--search`，嵌套范围只搜最外层）检索；会话互引在 `~/.codex/sessions` 与 `archived_sessions` 的其他 rollout 中检索。三项各有 `--trace-seconds` 秒时限，互不挤占。
    - 任一项没查完，或根本没有搜索范围，依赖"找不到"下结论的分类（`partial / mentioned / unmentioned`）一律记为 `unverified`；没有搜索范围时输出 `warning`。
    - 提到超过 10 个同组线程号的会话视为盘点清单、不算依赖（`inventory_sessions`，含是否仍在运行）；仍在运行的盘点式会话在它列过的线程上标 `listed_by_live_inventory`，交给 Agent 核对。
    - 树内任一线程忙碌（打开或 24 小时内改过），整棵树记为 `active`。输出不截断。
  - `--emit 目录`：文件名带组标签 `tag`（组 key 的 sha256 前 8 位）。批量组每个可出条目的分类写 `codex-<tag>-<分类>.json`；交互组每个会话写 `codex-<tag>-thread-<UUID>.json`，被仍在运行的会话提到的不写（列入 `held_by_live_sessions`）。`active`、`unverified` 从不出条目。每个条目带 `class`（交互对话组 → `interactive`，批量出图组各分类 → `batch`，判不出 → `unknown`）和 `session`（所属顶层会话 UUID；后代线程与原图目录同属一个会话）。另写 `codex-<tag>-manifest.json`（组 key、组号、生成时间、检索完成情况、文件清单）。写之前删除同组标签的旧文件。条目顺序为"后代在前、父在后、整棵树的原图目录（`scope=all`）最后"，`argv` 为 `["codex","delete","--force",<完整UUID>]`。
- 引用检索（扫描与探针共用）：有 `rg` 时用 `rg -F --no-ignore --hidden`，排除 `.git`、`node_modules` 与 2MB 以上文件——被 git 忽略的过程文件（回执、manifest）也要搜到；没有 `rg` 才退回 `git grep --untracked`。
- `probe claude`：`~/.claude/projects/*` 各项目体积与会话数，`settings.json` 里的 `cleanupPeriodDays`，`~/.local/share/claude/versions` 各版本与当前运行版本。
- `probe android`：各 SDK 根（`ANDROID_HOME`、`~/Library/Android/sdk`、`~/.local/share/android-sdk`）下的 `system-images`，以及哪些被 `~/.android/avd/*/config.ini` 的 `image.sysdir.*` 引用；各 AVD 的体积和快照体积。
- `probe worktrees --repo <路径>`：`git worktree list --porcelain` 的每棵树：分支、是否 locked、是否已并入 `origin/main`、dirty 行数、被忽略内容体积（`git ls-files -o -i --exclude-standard --directory` 加上 `du`）。
- `probe projects [--run DIR] [--runs-dir DIR]`（0.5，只读）：识别项目并写 `<run>/projects.json`（见第 6b 节）。来源：① git 仓库根与链接工作树（facts 节点的 `git.repo`、家目录一二级目录下的 `.git`；链接工作树折进主仓库，记入 `roots`）；② AI 会话的工作目录（Codex 会话 `cwd`、Claude Code 项目目录名——按文件系统逐级反解，不拆字符串、Grok 会话目录名——URL 解码）归到所在仓库根，没有仓库就取家目录（或标准文件夹）下第一级目录；③ 开发环境，每类一个（android / xcode / docker / models），体积为 0 的不列；④ 家目录一级、名字含 成品 / 成片 / final / output / dist / release / export / deliver 的目录。体积来自 facts 前沿节点（归给路径最深的项目），没有事实的项目才做有限遍历（≤2 万条目、1.5 秒、总预算 20 秒，`bytes_partial: true` 表示被截断）；最后活动取节点最新修改、项目目录与 `.git` 的修改时间、会话时间中最大的。不在家目录下、隐藏目录、Library、标准文件夹本身、已不存在的目录不算项目；某类来源取不到就跳过并写进 `skipped`。stdout 输出 JSON（去掉 `session_ids`），摘要写 stderr。
- `probe grok`：`~/.grok/sessions` 体积，`config.toml` 里的 `storage.cleanup_ttl_days`。
- `probe system`：完全磁盘访问权限（三态）、本地快照、Time Machine 目的地、可清除空间（statvfs / 容器 / Finder 三种口径）、未归因的“系统数据”估算、`/private/var/folders` 等系统大块。只读、外部命令带超时、不用 sudo。
- `probe updaters`：各软件更新暂存包与已安装版本比对（bundle id、版本号），状态 `superseded / pending_install / orphaned_update / unknown / in_use`；只有 `superseded` 的卡可写 `updater_verdict: "superseded"`。
- `probe leftovers [--min-age-days N] [--brew-zap]`：已卸载软件在 Caches / Logs / Saved State / HTTPStorages / WebKit 留下的目录，只报告；Spotlight 查询失败一律按“软件还在”处理。
- 需要 sudo 才能处理的对象（本地快照、`/Library` 下的系统缓存等）：Skill 自己不使用 sudo，只在卡片 `question`/`impact` 里合成一两行命令，由用户在终端自行执行。

## 4. 计划 `plan.json`（Agent 撰写）

```json
{
  "schema": "plan/1",
  "scan_id": "<与 facts 一致>",
  "cards": [
    {
      "id": "c01",
      "title": "已上线短剧的渲染中间稿",
      "group": "动画短剧 · 第一季",
      "verdict": "clean",
      "bytes": 12950000000,
      "summary": "6 季已上线，成片在 视频成品/，中间稿可重渲但不再需要",
      "evidence": ["每季成片 16–17 条：~/Projects/短剧/成片/*/final-*.mp4", "上线提交 a1b2c3d4e5"],
      "impact": "以后改这 6 季要重新出镜头和渲染",
      "question": null,
      "cards_used": ["git-worktree-build", "pipelines"],
      "items": [
        {"path": "/abs/path", "method": "delete", "scope": "ignored-only", "keep": ["相对路径或文件名"]},
        {"path": "/abs/path2", "method": "native", "argv": ["sdkmanager", "--uninstall", "system-images;android-31;android-tv;arm64-v8a"]}
      ]
    }
  ],
  "governance": [
    {"id": "g01", "title": "Claude Code 会话保留期", "current": "cleanupPeriodDays=99999", "suggest": "30", "how": "改 ~/.claude/settings.json", "bytes_per_month": null}
  ]
}
```

- `verdict` 取值：
  - `clean`：证据充分，建议清理；
  - `decide`：需要用户回答 `question`；
  - `keep`：保留，写明依据；
  - `govern`：持续增长的源头，只放进 `governance`，不进 items。
- `items[].scope`：
  - `all`：整个路径；
  - `ignored-only`：只删 git 忽略的文件，被跟踪文件永不删除；
  - `files`：只删 `files` 数组里列出的文件（绝对路径）。

  `keep`：排除的相对路径或文件名。
- `items_file`（可选，字符串或数组）：run 目录内的相对路径，指向探针 `--emit` 生成的条目数组（单个文件不超过 50,000 条），校验时追加到 `items` 之后，封存与看板都使用展开后的条目。越出 run 目录、绝对路径、读不出的文件让整张卡降级。
- **条目 `class` 与 `default_select`（0.3）**：
  - 条目可带 `class`：`interactive`（交互对话，人还可能回来续聊）/ `batch`（批量出图驱动）/ `unknown`；由探针 `--emit` 写入，Agent 不手填。取值不在这三个里让整张卡降为 `keep`。**缺失或 `unknown` 时 `plan.py` 按路径/命令自行推断（P5.R M2）**：`codex delete` / `grok sessions delete` 形状的条目、`~/.claude/projects/**/*.jsonl`、`~/.codex/sessions/**` 与 `~/.codex/archived_sessions/**` 下的条目一律按 `interactive` 处理（两条会话闸门照常生效），推断不出才是 `unknown`。手写的 `class=batch` 与这个推断冲突、又不是来自探针 `--emit` 的 `items_file` 时（`plan.py` 展开 `items_file` 时给来自它的条目打内部来源标记，手写条目自带的标记会被清除），仍按 `interactive` 处理并给 warning；来自探针 `--emit` 的 `batch` 照信。`class` 由 Agent 可写的文件承载，这一推断拦的是“判档误把交互会话当批量”和缺字段，不是对恶意伪造的完整防线，最终仍由结束证据兜底。
  - 含 `class=interactive` 条目的卡不得为 `clean`：降为 `decide`（沿用 `downgraded`，原因写进 `errors`，问题默认问「是否不再续聊」）。
  - 一张卡最多一个交互会话：按条目的 `session` 去重计数（后代线程、原图目录与顶层会话同属一个），超过则整张卡被拒（降为 `keep`、不可封存）；没有 `session` 的 `interactive` 条目每条各算一个。`decide` 卡同样适用。
  - `default_select`（看板是否初始勾选）**只由 `plan.py check` 计算**，写进 `plan.check.json` 每张卡和看板数据；`plan.json` 里手写的值被忽略并给 warning。`false`：不是 `clean`、含交互会话、条目是安装包（`.dmg/.pkg/.mpkg/.zip/.xip/.iso`）或 Xcode 归档（`/Xcode/Archives`、`.xcarchive`）、合并后的 producer（见下文“条目自己的风险画像”）含 `model-caches` 卡、含 `app-updaters-installers` 且卡上没有 `updater_verdict: "superseded"`。其余 `clean` 卡，所有条目要么是纯缓存原生命令（非会话删除形状），要么合并后的 producer（条目自己的规则或所在节点、后代节点）`default_select` 都明写 `true`，才是 `true`；没有 `default_select` 一律 `false`。
- **`evidence` 的对象形式**（字符串写法照常接受，只作说明，不算结束证据）。`plan.py check` 现场复核，外部命令带 20 秒超时，失败按未通过：`{"kind":"merged","repo":"/abs","ref":"分支或提交","target":"origin/main"}`（`git merge-base --is-ancestor ref target`）；`{"kind":"delivered","glob":"/abs/成片*","expect":16}`（匹配数 ≥ expect，路径段含 rejected/tmp/temp/scratch 的、或在待清理路径内的不计）；`{"kind":"copy_verified","src":"/abs/a","dst":"/abs/b"}`（大小与 sha256 相同、dst 不是硬链接或符号链接，且被 git 跟踪或在 profile `deliverable_roots` 内，路径段含 rejected/tmp/temp/scratch 或在待清理路径内的不算）；`{"kind":"user_stated","text":"…"}`（接受，不算结束证据）。**结束证据必须与被删条目关联（P5.R H2）**：`merged` 的 `repo` 必须与某个待删条目同仓（条目落在该仓某个 git 工作树内、或包含该工作树），`ref` 解析出的提交必须正是条目所在工作树当前检出的提交，`target` 必须是远端跟踪分支（`refs/remotes/*`，如 `origin/main`；本地分支、`HEAD` 不行），且 `ref` 与 `target` 不能是同一个提交（`HEAD` 对 `HEAD` 恒真，不算）；`delivered` 的 glob 根目录（去掉通配后的目录）不能是 `/`、`/Applications`、家目录及其上级这类宽泛位置，必须落在 profile 的 `reference_roots` / `deliverable_roots` 内或某个 git 工作树内，悬空符号链接与指到根目录之外的链接不计数；`copy_verified` 的 `src` 必须在某个待删条目之内（证明被删的东西有副本），`dst` 必须在所有待删条目之外。**`clean` 卡若涉及需要「任务已结束」的条目，却没有任何一条通过复核的 merged / delivered / copy_verified，自动降为 `decide`**，原因写进 `plan.check.json` 该卡的 `errors`，复核明细在 `evidence_checks`，通过复核的证据原文随看板数据（`evidence_verified`）显示在卡片详情里，让用户看见“已结束”依据的是什么。**免证据只认 `safety=="safe"`（H3）**：需要结束证据的条目是会话删除（`codex delete` / `grok sessions delete`）以及一切不满足下面条件的路径；只有缓存清理类原生命令，或条目合并后的 producer（见下一条）`safety` 明确为 `safe` 的路径才免，`moderate`、缺 `safety`、归不进任何规则的未知目录、`risky` 一律要证据（不再按“整张知识卡”豁免）。
- **用户资产与高风险位置（条目自己的风险画像，P5.R H1）**：`plan.py check` 对每个条目三方合并 producer：① 同路径或最深祖先事实节点的 producer，② 对条目 realpath 本身现跑的 producer 规则（scan 只给 ≥ 阈值的目录建节点，列节点内部子目录时必须看条目自己的规则），③ 条目是其父目录的每个后代节点的 producer。任一方 `safety` 为 `risky` 即按 risky 处理：同时属于 chat-netdisk 卡（聊天记录、网盘本地文件）的整卡降为 `keep`，只走软件自身入口；其他 risky 的 `clean` 降为 `decide`，交给用户明确选择；`blocked_by_processes` 取并集写进封存；任一方卡为 `app-updaters-installers` 就做下面的更新暂存复核。**`decide` 卡也一样（M3）**：含 risky 条目、引用未核实或更新暂存未确认时，原因写进该卡的 `risk_notes` 与 `warnings`，看板在卡片上显示“风险提示”，问题前面拼上风险默认问题（“这里面有你的账号数据或用户资产，你明确要清理它们吗？”等）。更具体的 `moderate` 规则（如 `Slack/Cache`）命中的条目不受影响。
- **更新暂存现场复核**：条目所在节点 producer 卡为 `app-updaters-installers` 时，`plan.py check` 现场跑一次 `probe updaters`（每次 check 一次），与条目路径重叠的候选全部为 `superseded` 才能保持 `clean`；`unknown`、`pending_install`、找不到候选（`not_found`）或探针出错（`probe_error`）一律降为 `decide`。卡上手写的 `updater_verdict` 只影响预勾选，不能代替这次复核。
- **进程闸门**：`plan.py check` 把所在节点 producer 的 `blocked_by_processes` 写进封存条目；`act.py` 试点与正式执行每条前用 `pgrep -x` 逐个查（10 秒超时），进程在运行、或查询出错超时，该条记 `skipped`（“<进程> 正在运行，按知识卡只报告不执行”）；预演仍为 `planned`，但带 `blocked_by` 并提示正式执行时会跳过。
- `argv[0]` 为 `codex` 时只接受 `["codex","delete","--force",<完整线程UUID>]` 这一种写法；`path` 的真实路径必须在 `~/.codex/sessions` 或 `archived_sessions` 下，文件名整串匹配 `rollout-…-<UUID>.jsonl`。否则降级（防止核对的是 A、删掉的是 B）。
- 同一张卡里每个 `codex delete <UUID>`，它仍存在的全部后代都必须作为 `codex delete` 条目列在同一张卡、且排在它前面，否则降级（删父会连带删子）。
- `method=native` 时，整条 `argv` 必须匹配 `scripts/protect.py` 形状表 `SHAPES` 中的一条：程序名、固定子命令、允许的选项逐项比对，多一个未登记的选项也拒绝；带对象槽位的形状（如 `sdkmanager --uninstall <包ID>`、`ollama rm <模型>`、`grok sessions delete <UUID>`）要求 `path` 与对象对应，缓存类形状（如 `brew cleanup`、`uv cache prune`）要求 `path` **恰好是**该工具的缓存根之一（规范化后相等，不接受其子目录：命令清的是整个缓存，子目录的占用与最近修改核对覆盖不了它）。`sdkmanager --uninstall` 不接受 `cmdline-tools`、`platform-tools`（卸载会删掉 sdkmanager 自身与 adb）。`plan.py check` 与 `act.py` 执行前用同一个 `protect.native_check` 校验。会删除镜像卷、安装卸载软件或执行任意代码的命令（`docker system prune`、`docker volume prune`、`brew uninstall`、`npm exec`、`pip install` 等）不登记。新增形状须在形状表登记并附通过例与反例测试。执行时工作目录是 run 目录下 `native-cwd/` 里为每条命令新建的空子目录，环境变量按白名单重建、`PATH` 固定（会改缓存位置的 `PIP_*`、`npm_config_*`、`UV_*`、`HOMEBREW_*` 等一律去掉；`brew cleanup` 强制 `HOMEBREW_NO_AUTOREMOVE` 等，`brew.env` 里出现相关键时不执行）；程序路径在封存时记录，执行时重新解析不一致就跳过。用户级配置文件（如 `~/.npmrc` 里的 `cache=`）仍会生效，这时命令清的是用户自己配置的缓存目录，回执可能显示 path 占用没变化。脚本不经过 shell，直接 `subprocess.run(argv)`。
- **原生命令的执行环境**（`act.py`，参数由 `protect.native_env` 生成）：命令真正清哪里取决于工作目录里的配置文件、环境变量和 PATH，而不只取决于 `path`，所以执行时固定：① 工作目录为 run 目录下的 `native-cwd/`，必须是空目录，非空则跳过该条（工具往里写了东西会记入回执 `cwd_leftovers`）；② 环境变量白名单重建，只保留 `HOME`（系统用户库给出的家目录）、`USER`、`LOGNAME`、`LANG`、`LC_*`、`TMPDIR`，`PATH` 固定为 `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` 加上程序所在目录，`PIP_*`、`npm_config_*`、`UV_*`、`BUN_*`、`HOMEBREW_*`、`XDG_*`、`HF_*`、`OLLAMA_*`、`DOCKER_*`、`ANDROID_*`、`CODEX_HOME` 等一律不继承；位置必须依赖变量的工具（Android 的 SDK/AVD 目录）由形状的 path 规则推导后显式设置；③ 程序绝对路径在 `plan.py check` 时解析并写入封存（`prog`），执行时按同一规则重新解析，不一致就跳过；回执记录 `prog`、`cwd`、`env`。
- **`brew cleanup` 的真实作用**：除下载缓存、日志外，还会删除已安装软件的旧版本（在 Cellar/Caskroom，`path` 之外），默认还会 autoremove 卸载无人依赖的 formula。形状保留，执行时强制 `HOMEBREW_NO_AUTOREMOVE=1`、`HOMEBREW_NO_AUTO_UPDATE=1`、`HOMEBREW_NO_INSTALL_CLEANUP=1`（封存里记 `env_forced`）；`brew.env` 配置文件会覆盖进程环境，其中出现这些键或 `HOMEBREW_CACHE` 等位置键时不执行。
- 其他副作用已写在形状用途里：`avdmanager delete avd` 同时删 `~/.android/avd/<名>.ini`；`docker builder prune` 清的是 `Docker.raw` 内部的构建缓存，`path` 的占用可能不立即变小，回执会显示“占用没有变化”。

## 5. 保护名单（脚本强制，Agent 不能放行）

以下路径及其子路径永远不进 `delete`：

- `/`、`/System`、`/usr`、`/bin`、`/sbin`、`/etc`、`/private/etc`、`/private/var/db`、`/Library`（系统级）、`/Applications`（本版不做软件卸载）、家目录本身；
- `/private/var/vm`（虚拟内存与休眠镜像）、`/Volumes` 及各卷根、`/Users` 与其他用户的家目录（含 `/Users/Shared`）、任意卷、任意深度的 `.Trashes`、`Backups.backupdb`（Time Machine）、`.Spotlight-V100`、`.fseventsd`、`.DocumentRevisions-V100`、`.MobileBackups`（目录本身及其内容），`~/.Trash` 本身与其内容（废纸篓里的文件是否可删本版不开放），`~/Library/Application Support/MobileSync`（iOS 设备备份，可能是唯一备份）；
- `~/Documents`、`~/Desktop`、`~/Pictures`、`~/Movies` 下的原件（仅限这几个顶层目录本身及直接子项；例外：`~/Movies` 中带 `cache`/`render` 字样的子目录可由用户点名）、`~/Library/Mobile Documents`（iCloud）、`~/Library/Mail`、`~/Library/Messages`、`~/Library/Keychains`、`~/Library/Photos`；
- `~/.ssh`、`~/.gnupg`、`~/.aws`、`~/.config`、`~/.netrc`、`~/.claude.json`、`~/.codex/auth.json`、`~/.claude/projects/*/memory`、`~/.config/agents`；
- 任何 `.git` 目录或文件；
- `profile.protect` 中的路径。

另外：

- 路径是保护路径的祖先时也拒绝。不能删除包含保护路径的父目录。但这种“仅因是祖先而受保护”的节点（如含 MobileSync 的 `~/Library/Application Support`）只拒绝整删它本身：节点内部不受保护的条目不受影响，按“未做引用检查的节点”处理，卡片 `evidence` 须有一条合法的 `refs_reviewed`（`history_only`，见第 5 节检查项）；本身在保护范围内的节点（如 `~/Documents` 之内），其内条目仍不可补救。
- “当前用户的家目录”一律取系统用户库（`pwd`）记录的目录，不信环境变量 `HOME`；环境里的 `HOME` 与之不一致（如 `HOME=/`）时，`plan.py check/seal` 与 `act.py run` 直接拒绝。`~` 也按系统记录的家目录展开。
- 原路径与物理路径（最近存在的祖先做 realpath 后拼接剩余部分）各查一次，任一命中即保护，防止借符号链接绕过。
- 任何节点在执行时 `recent_24h>0` 或被进程打开时，跳过并报原因。

## 6. 封存 `seal.json`（plan.py seal 产出）

`plan.py check` 校验 `plan.json`：

- 结构合法、`scan_id` 一致；
- 每个 item 路径存在、在 `roots` 之下、不在保护名单内；
- `clean`/`decide` 卡片中，每个 item 所在节点要满足 `refs.checked==true`，并且 `refs.count==0`；不为 0 时，卡片 `evidence` 中必须有一条 `{"kind":"refs_reviewed","samples":["docs/a.md:1"],"verdict":"history_only"}`（结构化引用核对）：`samples` 须取自该节点 facts 里的 `refs.samples`，与之无交集即视为伪造、按未核对处理；比较前规范化（NFC、去空白、`\`→`/`、去 `./`、折叠重复斜杠、去末尾 `:命中数`）；`verdict` 为 `depends` 时整卡直接降为 `keep`；它不是「已结束」证据，不满足 `clean` 的结束证据要求；旧写法 `引用已核对:` 前缀只给 warning，按未核对处理，不再放行；
  - 唯一例外：节点因为是非前沿节点（`refs.reason=="non-frontier"`，其大块子目录已各自检查）而未查时，可以用合法的 `refs_reviewed(history_only)` 补足，但**只能软降级**（P5.R M1）：这类节点没有 `refs.samples` 可比对，复核只校验结构、不算已核对，节点仍是“未检查”；`clean` 卡因此降为 `decide`、问题写“这批内容的引用没有被核实，你确认没有别的项目还在用它们吗？”，`decide` 卡带同样的风险提示。补救只用掉第一条合法复核，同卡其余复核仍要和节点样本比对，对不上照样报“视为伪造”；超时、出错等其他原因的未检查不可补救；
- `recent_24h==0`，`open_by` 为空；
- **条目在某个节点内部**（例如几千个会话文件中挑出的单个文件，或大目录里的一个子目录）时，祖先节点的 `recent_24h`、`open_by`、`protected` 描述的是兄弟项，不再套用到该条目。改为现场遍历该条目，24 小时内有修改就降级；进程占用由 `act.py` 执行时逐项用 `lsof` 复核；保护名单仍对条目路径本身直接判定。祖先节点没做引用检查（非前沿节点，或仅因包含受保护路径而被标 protected）或引用数大于 0 时，卡片 `evidence` 必须有合法的 `refs_reviewed`（引用数大于 0 的已检查节点须样本相交）；
- `native` 的整条 `argv` 匹配形状表，且 `path` 与形状要求对应（见第 4 节）。

不合格的卡片降级为 `keep` 并写入 `plan.check.json` 的 `errors`。

`plan.py check` 打印时按卡、按原因汇总（每种原因一行：原因、条数、最多 3 个示例路径），同一张卡里字面相同的提示只留一条。`plan.check.json` 每张卡在原有的 `errors`（逐条原文）、`warnings` 之外多两个汇总字段：`error_summary`、`warning_summary`，元素为 `{reason, count, examples（≤3 个路径）, paths（该原因下的全部路径）}`，完整明细以它们和 `errors` 为准。

降级的卡片在 `plan.check.json` 里另带 `judgment_refs`：按降级原因（结束证据不足、交互会话、引用未核对、受保护、被跟踪、正在使用等）运行时从 `references/judgment.md` 对应小节取出原句，格式 `{section,title,quote}`，找不到小节或句子时 `quote` 写明未找到。看板与汇报用它向用户说明依据，所以 judgment.md 里被引用的小节标题和句子不得随意改动（`tests/test_judgment_refs.py` 守着）。

**项目台账接入 `check`（0.5）**：用有效 profile 与 `<run>/projects.json`（`scan_id` 须一致，否则只能按项目目录路径判断）建项目索引。**台账里有 `active` / `protected` 项目、而本次 run 没有 `scan_id` 一致的 `projects.json` 时，`check` 让所有 clean / decide 卡降为 `keep`**（原因“台账里有进行中或别碰的项目，但这次扫描还没有项目识别结果……先运行 scan.py probe projects”）：会话号与 Claude 项目目录的归属只有它能认出，少了它不能放行任何删除卡；台账只有 `done` / `archived` 项目时不要求。条目属于某个项目的方式有三种：路径与项目目录重叠（在里面；或条目把整个进行中 / 别碰项目包含在内）；Codex 会话 / 生成图的会话号在该项目的 `session_ids` 里（会话号取自条目的 `session`、路径文件名、`codex delete` 的 argv）；路径在 `~/.claude/projects/<目录>` 下且该目录在项目的 `claude_dirs` 里。`active` / `protected` 项目里的条目让整张卡降为 `keep`（原因“项目「X」还在进行中……”/“项目「X」你标了‘别碰’……”，附 `judgment.md` §8 引用）；`protected` 项目的目录同时在保护名单里，执行时 `protect.py` 也会拦。`done` / `archived` **不改变任何放行条件**（`user_stated` 仍不是结束证据，副本核对、引用核对、进程与 24 小时改动照旧），只在封存记录里写 `projects: [{key,name,status}]`，并影响提案：卡里全部条目都属于 `done` / `archived` 项目的 `decide` 卡（无风险提示、不含交互会话）记 `prefill: "删"`；全部条目都属于 `archived` 项目时，档位上限为 `recommended`（`preset: all` 的卡降到推荐档；风险区仍只在 `all`）；封存记录另有 `preset_raw`（不计台账上限的原始档位），提案文案与看板据此仍提醒“删了找不回”（看板卡片带 `preset_raw`，与 `preset` 不同时标出）。`plan.check.json` 每张卡带 `projects`，有上限 / 预填时带 `project_cap` / `prefill`；看板数据（`plan.py board` 的 `plan.cards[]`）同样带 `projects` 与 `prefill`。

`plan.py seal` 同时写入 `profile_guard`（`{digest, fields}`）：封存时影响判断的本机设置——`roots`、`extra_roots`、`reference_roots`、`protect`、`deliverable_roots`、`retention_days`——的规范化摘要，以及 0.5 起的 `projects`（项目台账里每个项目的 `status` 与 `deliverables`；没有台账时不进摘要，与旧摘要一致；名字、时间、来源只影响显示，不在其内）（列表排序去重，缺省值与显式默认值等价；`rules`、`cooldown`、`default_choice`、`notes`、`onboarded_at` 只影响提案档位与文案，不在其内）。`profile_guard` 不参与 `digest` 的计算：改了设置重新封存，卡片没变时 digest 不变，用户已发来的选择清单仍然有效。`act.py run`（含预演、试点）与 `go` 比对现在的设置与它，不同或旧封存没有这一项就拒绝（退出码 2，什么都不做），要求重新 `plan.py check` / `seal`。

`plan.py seal` 对通过的 items 记录指纹：`realpath`、`st_ino`、`st_dev`，以及计量时的 `bytes`；对 `files` 范围的条目，还记录每个文件的 `(size, mtime_ns)`。整份内容写入 `seal.json`，`digest` 取前 12 位 sha256。

## 6b. 项目清单 `projects.json` 与台账导出行（0.5）

`scan.py probe projects` 写 `<run>/projects.json`：

```json
{
  "schema": "projects/1", "scan_id": "<与 facts 一致>", "created_at": "2026-10-11T15:00:00Z", "shown_limit": 15,
  "projects": [
    {"key": "/Users/alex/Projects/短剧", "name": "短剧", "kind": "git", "sources": ["git", "ai-session"],
     "bytes": 12950000000, "last_active": "2026-10-10T08:00:00Z", "sessions": 42,
     "deliverables_guess": ["~/Projects/短剧/成品"], "default_status": "active", "shown": true,
     "roots": ["/Users/alex/Projects/短剧", "/Users/alex/wt-feature"],
     "claude_dirs": ["-Users-alex-Projects---"], "session_ids": ["<Codex 会话 UUID>"]}
  ],
  "skipped": [{"source": "codex", "reason": "没有 ~/.codex/sessions"}]
}
```

- `kind`：主类型，优先级 `git` > `ai-session` > `env` > `deliverable-dir`；`sources` 是全部来源。环境项目的 `key` 是 `env:android` 这类，另带 `env_roots`（`~/…`）。
- `default_status`：最后活动 7 天内 → `active`；30 天以上 → `done`；其间或不知道 → `unknown`（让用户选）。
- 按 `bytes` 降序全部输出，前 15 个 `shown: true`，其余折叠；台账看板只列 `shown` 的。`sessions` 是关联的 AI 会话数（Codex 顶层线程 + Claude jsonl + Grok 会话目录）。
- `roots` / `claude_dirs` / `session_ids` 是 `check` 判断条目归属用的，不给用户看。

**台账导出行**：台账看板（P3）导出的文本抬头写“叁笙清理 · 项目台账”，台账放在一行 JSON 里（路径里有 `|`、`;` 也不会被拆）：

```
叁笙清理 · 项目台账
台账: {"项目":{"/Users/alex/Projects/短剧":{"状态":"做完了","成品":["~/Projects/短剧/成品"],"名称":"短剧"},"env:android":{"状态":"归档"}}}
```

状态接受中文（进行中 / 做完了 / 归档 / 别碰）与英文（active / done / archived / protected），键 `状态`/`status`、`成品`/`deliverables`、`名称`/`name`。`plan.py profile --apply <文件>` 解析并写入 `profile.projects`；不认识的项目键、不在家目录下的路径、不存在的目录、太宽泛的成品目录（`~/Library`、`~/Library/Caches`、`~/Downloads`、`~/Desktop`、`~/Documents` 根等，判定见 `protect.is_broad_deliverable`，设置行 `成品` 同样拒绝）、不合法的状态逐项报“没保存”；文件读不了时 `plan.py profile --apply` 给中文错误（退出码 2），其余照写；成功应用过一次（含 `{"项目":{}}` 全默认）就写 `onboarded_at`。**这种导出只保存设置，不含任何清理授权**：`prefs.is_ledger_export` 认抬头；`act.py run` 只读 `--cards` / `--answers` / `--sel` 与封存；`act.py go --from-board` 只接受抬头为“叁笙清理选择”的清单，遇到这种抬头直接拒绝（第 7c 节），文本里即使混进 `清理:` 行也不会产生任何清理选择。保存后项目台账变了，已封存的计划按 `profile_guard` 失效（重新 `check` / `seal`）。

## 7. 看板 `board.html`（plan.py board 产出）

- 由 `assets/dashboard.html` 模板渲染：替换 `__PLAN_JSON__`（facts 摘要 + plan + seal digest）和 `__LOGO_DATA__`。
- 左侧目录是垂直居中的深绿悬浮导航岛：总览 + 按用户认得出的类别分区成块（每区顶部写本区合计）（AI 工作 / 常规清理 / 你的文件，定义在 `plan.py` 的 `BOARD_CATEGORIES`），每类一行写“可删 x/总量”或“留 总量”。类别默认由 `cards_used` 第一项映射（`USER_SOURCES`），卡片可写 `category`（取 `BOARD_CATEGORIES` 的 id，如 Codex 出图写 `ai-images`）改归类，写错的不认；`category` 只影响显示，不进封存摘要。底栏“复制清单”左侧是“一键选择”三档按钮（写腾出量，悬停说明，另有“清空”；总览不重复放）：稳妥（删了不丢东西：会自动重新下载、失败或废弃的记录）、推荐（再加有副本的、任务已做完的过程记录和中间稿）、全部可删（再加删了找不回来的）。卡片可写 `preset`（`safe / recommended / all`），不写时 `clean` 为 `safe`、`decide` 为 `recommended`；风险提示含账号数据风险（RISKY_ZONE）的卡一律为 `all`，写低了也不认。选一档 = 勾上所有档位不高于它的可执行卡，之后可逐项加减；每个条目显示所属档（稳妥 / 推荐 / 慎重）。`preset` 只影响显示，不进封存摘要。总览先回答“已用空间都去哪了”：逐项查过的各类别、扫过但没逐项分析的在用文件（列扫描根下第一层大目录）、扫描不到的系统区域（已用 − walked_bytes），`governance` 放在总览底部（`done: true` 表示已办好）。右侧只显示当前类别：每项左侧写标题、来源与为什么会有（`summary`）、勾选前要想的问题（Agent 原写的 `question`，看板字段 `ask`）、合并后的风险提示（`risk_summary`），右侧是体积和勾选框；删了会怎样（`impact`）与路径在“详情”里。
- 勾选＝清理，没勾＝不动：decide 卡勾上即“清理”行带上它、“回答”行记 `卡号=删`；没勾的不出现在导出文本里。页面只有一个主操作“复制清单，发给 Agent”，剪贴板不可用时退回到选中文本框让用户手动复制。
- `clean` 卡初始勾选与否取 `plan.check.json` 里算出的 `default_select`（`true` 才预勾选，其余不勾）；用户仍可随时改选。
- **② 清理看板没有设置页**（0.5）：全局设置只剩“AI 对话保留天数”和“默认档位”，成品目录按项目；左侧常驻“我的项目”入口（随时改项目状态，页底有“回到清单”），每个条目旁有“这个项目做完了 / 还在做”快捷按钮，顶部有“新发现 N 个项目”提示行（来自 `proposal.json` 的 `project_questions` 与 `projects.json`），左下角小字入口：GitHub、反馈问题、公众号、小红书、小宇宙。改过设置或项目状态时，导出文本在 `sel:` 之后多出下列行（都是整行 JSON，目录名里可以有 `|`、`;`；都不改变 `sel` 的计算）：
  - `设置: {"保留天数":15,"别碰":["~/a"],"成品":[],"默认":"recommended"}`；`plan.py profile --apply` 同时兼容旧的 `保留天数=15; 别碰=a|b; …` 写法；
  - `台账: {"项目":{"<项目键>":{"状态":"做完了","成品":["~/…"],"名称":"…"}}}`（格式同第 6b 节，只是抬头仍是“叁笙清理选择”，所以这份文本**同时是**清理清单）；
  - `别再列: <规则键>, …`。
  影响判断的设置或台账保存后，已封存的计划按上面的 `profile_guard` 失效，须重新 `check` / `seal`；`act.py go --from-board` 会自己做（第 7c 节）。
- 主操作"复制给 Agent"生成如下文本：

  ```
  叁笙清理选择
  scan_id: …
  seal: <digest>
  清理: c01, c03
  回答: c02=删
  sel: <12 位>
  备注: …
  ```

  `sel` = `sha256(JSON)[:12]`，JSON 为 `{"answers":[排序的"卡号=回答"],"cards":[排序的卡号],"seal":"<digest>"}`（键序固定、无空白、不转义非 ASCII）；`cards` 是“清理”行的卡号（含答复为“删”的 decide 卡），`answers` 是“回答”行的全部条目。浏览器内用 `crypto.subtle` 计算，`plan.py` 的 `selection_sig` 是 Python 端唯一实现，`tests/test_selection.py` 抽出页面里的 JS 在 node 中逐值对照。

## 6a. 定向引用检查 `item_refs.json`（scan.py probe item-refs 产出）

扫描时只对前沿节点查引用。计划里 clean/decide 卡的条目若落在某节点内部、且该节点 `refs.checked` 不为 true，或条目路径正好等于某个 `refs.reason` 为 `non-frontier`（0.5：如模型目录、SDK 目录）或 `no-search-scope`（扫描时没有可搜目录）的节点本身，或扫描之后台账 / 设置新增了成品目录（扫描时查过的节点也没搜过新目录），就按条目自身路径（同扫描的 `build_patterns`）在当前有效 profile 的 `reference_roots` 与所在工作树里补查（两者都没有、一个目录都没搜时记 `checked:false, reason:"no-search-scope"`，不算零引用），写 `{schema:"item-refs/1", scan_id, items:{条目绝对路径:{checked,count,roots[],samples[,reason]}}}`（`roots` = 这一条实际搜过的目录）。`check` 只认 scan_id 相同的文件：`checked` 且 `count=0` 视为已核实；`count>0` 时卡片须有样本与该条 `samples` 有交集的 `refs_reviewed`（history_only），否则整卡降为 keep；没查到的条目照旧按节点规则处理。条目等于非前沿节点本身时，定向结果只代替“引用检查未完成”这一项，节点级的 24 小时改动、被进程打开、受保护等核对照旧；节点未检查的原因是超时、出错等其他情形的不补查、不放行。

## 7a. 提案 `proposal.json`（plan.py propose 产出）

按封存给出三档：档位、规则键、体积（封存条目实测字节之和）和是否逐个确认都取自 `seal.json` 里每张卡的 `preset` / `rule_key` / `review` / `interactive`，封存后再改 `plan.json` 不影响执行范围（旧格式封存会被拒绝，重新 seal）；每档 `{word, label, cards, answers, sel, bytes, by_category}`：`safe`（回复“稳妥”）、`recommended`（回复“好”）、`all`（回复“都删”）。档内 = 可执行卡中 `preset` 档位不高于该档的；decide 卡的答复记为“删”；`sel` 用第 7 节同一公式。`mode` 为 `chat`（在对话里确认）或 `board`（发看板），`board_reasons` 写原因：没有 `onboarded_at`、待你决定的卡超过 3 张（`BOARD_ATTENTION_MAX`），或有卡含交互会话。`mode` 为 `board` 或 `stage` 为 `onboarding` 的提案，`act.py go --choice` 一律拒绝（退出码 2，什么都不做），三档 `options` 也从不含交互会话的卡，这类提案只能走看板清单（第 7c 节）。0.5 新增字段：`stage`（`onboarding` = profile 没有 `onboarded_at`，第一次使用，应先发 ①「认识你的电脑」台账看板并保存台账；`cleanup` = 否则）与 `onboarding_hint`（仅 onboarding）；`project_questions`：`[{"type":"new_projects","count":N,"projects":[{key,name,bytes,default_status}]}]`（本次 `projects.json` 里 `shown`、台账里还没有的项目）和 `[{"type":"stale_active","key","name","idle_days"}]`（台账标“进行中”、最后活动 ≥30 天），没有 `projects.json`（或 `scan_id` 对不上）就为空，两类问题也各用一句话写在 `message` 末尾；`prefill`：`{卡号: "删"}`（项目做完了 / 归档的 `decide` 卡）；`projects_by_card`：`{卡号: [{key,name,status}]}`（取自封存）。`cardkeys.json` 里属于项目的卡多一个 `projects`（项目键列表），`act.py` 记选择时据此多记带项目前缀的规则键。

`message` 是发给用户的一段话：默认档能腾出多少（按类别列前几项）、按规则直接清的逐个点名、要你拿主意的逐条列出标题、体积、回“好”删不删和问题、删了找不回的逐个点名（默认档为“全部可删”时写成“回‘好’也会删掉”）、三个回复词各对应什么。

`act.py go --run <目录> --choice safe|recommended|all`（对话确认；看板清单见第 7c 节）从提案读卡号、答复和 `sel`，依次调用第 8 节的 预演 → 试点 → 正式执行（闸门不变）；预演有失败条目或没有可执行条目、试点那一条没删成功时退出码 3，后面步骤不执行；正式执行有条目没删成功时其余照常完成、退出码 4；`proposal.json` 里卡号与 `sel` 对不上时由 `run` 拒绝，退出码 2（`sel` 只防转抄出错，不防有意重算；执行范围由封存兜底）。

## 7b. 成果页 `report.html` / `report.json`（report.py 产出，`act.py go` 与 `act.py run` 正式执行后自动生成）

- 释放量 = 同一 `scan_id`、同一 seal 下全部正式回执（`receipt.json` 与带时间戳的 `receipt-<时间>.json`，中断后重跑、分几次执行都会有多份）和试点回执（`receipt-pilot-*.json`）里 `removed/partial` 条目的 `removed.bytes` 之和，加 Codex 历史库回收（计入“AI 对话记录”）；预演回执不计。同一份回执只算一次；同一卡同一路径在多份回执里都记为 `removed` 的 delete 条目只取最大的一份（整个路径不可能删两次），`partial` 与原生命令条目逐份累加。试点先删的那一条在正式回执里是“已不存在”跳过，只看最后一份正式回执会少算。回执文件名为 UTC 秒级时间戳，同一秒内再写一份时顺延一秒，不覆盖。
- 所有体积文案（act 打印、check/提案、成果页、看板、扫描摘要）统一用十进制：1 GB = 1e9 字节，一位小数，不足 1 GB 写 MB（`scripts/units.py` 的 `fmt_bytes`，两个页面里的 JS 与它逐值一致，测试在 node 里对照）。
- 已用前 = 磁盘总量 − 第一份回执的 `free_before`；已用后 = 已用前 − 释放量。按类别汇总用与看板相同的归类（`plan.card_category`），`ai_freed` = “AI 工作”分区之和。
- 分享链接 `SHARE_BASE?v=1&f=&b=&a=&ai=`：只带腾出量、清理前后已用量、AI 部分（GB，一位小数），不带路径、用户名、卡片标题。二维码矩阵由内置的 `scripts/vendor/qrcodegen.py`（Project Nayuki，MIT）在 Python 端编码，页面只负责绘制；分享图在浏览器里用 canvas 画成 1080×1440 PNG。

## 7c. 看板清单一条命令 `act.py go --run <目录> --from-board <导出文本文件>`（0.5）

输入是 ② 清理看板“复制清单，发给 Agent”的整段文本（第 7 节格式，抬头“叁笙清理选择”；`scan_id:`、`seal:`、`清理:`、`回答:`、`sel:` 五行必须各出现一次，`seal` / `sel` 为 12 位小写十六进制；`清理:` 与 `回答:` 可以写 `无`，回答之间用 ` / ` 分隔；`设置:`、`台账:`、`别再列:`、`备注:` 可有可无）。与 `--choice` 二选一。`prefs.parse_board_export` 只解析，不核对。

顺序（任何一步不满足就停，之前的步骤不回退但之后的不执行）：

1. 抬头是“叁笙清理 · 项目台账”（① 的导出）→ 拒绝，提示用 `plan.py profile --apply` 保存；这种文本从不是清理授权，即使里面混进合法的清理行也不执行。
2. 解析清单、解析设置/台账行；格式不对 → 拒绝，什么都不保存。
3. `seal` 必须与 run 目录里的 `seal.json` digest 一致，`scan_id` 必须与封存的一致；按清单里的卡片与回答重算 `selection_sig` 必须等于清单的 `sel`（先于保存设置，对不上的清单不改动本机设置）；没有选中任何卡片也拒绝。`sel` 只来自清单，本命令从不重新生成它。
4. 有 `设置:` / `台账:` / `别再列:` 行 → 调 `prefs.apply_settings`（与 `plan.py profile --apply` 同一实现）保存；有路径没保存成功 → 停。
5. 保存后（或本来就是）封存的 `profile_guard` 与现在的设置不一致 → 在进程内调 `plan.py seal`（内含 `check`，同样写 `plan.check.json`）重新封存；新 digest 与清单里的不同 → 停，打印原因（设置/台账让哪些卡的判断变了，或判断没变但卡里目录的内容在封存之后又变了：实测体积、文件大小、修改时间都是封存内容的一部分，不沿用旧值）、“需要用新看板重新确认”和 `plan.py propose` / `plan.py board` 的命令；相同则继续。
6. 预演 → 试点 → 正式执行，与 `--choice` 共用同一段 `_go_steps` → `cmd_run`：`sel`、`profile_guard`、预演与成功试点的回执核对一步不少；结束自动生成成果页。每步打印 `【go】…`，适合 `nohup` 后台运行。

退出码：`0` 完成；`2` 拒绝（台账导出、清单格式不对、scan_id / seal / sel 对不上、没有选中卡片、闸门拒绝，什么都没删）；`3` 停下请用户看（设置/台账让判断变了需要重新确认、设置没保存全、重新封存没通过、预演有失败或没有可执行条目、试点没成功）；`4` 正式执行有条目没删成功，其余已完成。

## 7d. ① 项目台账看板 `plan.py onboard --run <目录>`（0.5，模板 `assets/onboard.html`）

第一次使用（`profile.json` 没有 `onboarded_at`，提案 `stage` 为 `onboarding`）时先发的轻量页，数据来自 `<run>/projects.json`（第 6b 节，只列 `shown` 的项目）。页面只有一张项目表（名称、体积、最后活动时间、四个状态分段按钮，“成品在哪”可展开修改）加两三个全局选项，**没有任何删除选项**；只有 `default_status` 为 `active` 的项目预选“进行中”（它只会让清理更保守）；扫描猜测的“做完了”不预选，只在行内用小字提示“看起来做完了（N 天没动）”，用户自己点了才算标注；导出只含用户点过的和预选“进行中”的项目。底部一个按钮“复制，发给 Agent”，导出文本：

```
叁笙清理 · 项目台账
scan_id: …
台账: {"项目":{…}}
设置: {"保留天数":15,"默认":"recommended"}
```

首行固定为“叁笙清理 · 项目台账”（`prefs.LEDGER_TITLE`，`prefs.is_ledger_export` 据此区分）；`台账:` 格式见第 6b 节；`设置:` 只含用户动过的全局项。Agent 把它存成文件交给 `plan.py profile --apply`（成功应用即写 `onboarded_at`，之后不再出 ①）；它不含 `seal:` / `sel:`，`act.py` 不会、也不能把它当清理清单（见 7c 第 1 条）。用户在对话里说“跳过设置”，等价于 apply 一份 `台账: {"项目":{}}`。

## 7e. 反馈 `feedback.py` 与成果页首次 Star（0.5）

- `feedback.py gaps --run <run> [--min-gb 1] [--text]` 输出 `feedback-gaps/1` JSON：`asked`（现在是否可以问用户）、`reason`（`ok` / `declined` / `none`）、`gaps`（`pattern`、`bytes`、`size`、`count`、`software`）、`total_dirs`、`preview`、`hint`。缺口判据：前沿节点、未受保护、体积 ≥ 阈值、`producer.card` 与 `producer.tool` 均为空。
- `feedback.py issue-url [--json]` 生成 GitHub Issue 预填链接（用户自己点开、自己提交；整条链接超过 7000 字符时少列条目并写“另有 N 条省略”）。`feedback.py decline` 写 profile 的 `feedback_declined_at`，冷却 90 天。
- 路径脱敏：用户名换 `~`；家目录下只有众所周知目录名（`feedback.WELL_KNOWN`）、一级点目录、`Library/{Application Support|Containers|Group Containers}` 的直接子目录原样保留，其余换 `<目录>`；外置卷名换 `<卷>`，其他用户换 `<用户>`。反馈不含文件名、文件内容、项目名。
- profile 新增 `star_asked_at`、`feedback_declined_at`，都不在 `PROFILE_GUARD_FIELDS` 内（写它们不会让封存失效）。
- `report.make` 是唯一写 `star_asked_at` 的地方，`build_report` 纯计算；`report.json` 新增 `star: {show, url}`，只有第一次清理的成果页 `show` 为真。
- `act.py run` 正式执行成功后调用 `finish_report(run)`：生成成果页，再打印反馈提示行；`cmd_go` 内部三步传 `no_report=True`，全部结束后调一次，避免重复。

## 8. 执行 `act.py run --run <目录> --seal <digest> --cards c01,c03 [--dry-run | --pilot | --skip-pilot --reason <原因>]`

1. 读取 `seal.json`，`digest` 必须一致；`profile_guard` 与现在的本机设置不一致（或旧封存没有）就拒绝，退出码 2；只处理指定卡片中封存过的 items。
   `--sel <12位>`：按上面的公式，用 seal digest、本次选中的卡（`--cards` 并上答复“删”的卡）和全部 `--answers` 重算，不符直接拒绝（退出码 2，不做任何删除）。**从看板导出文本来的运行必须带上其 `sel:` 值**，用来防止转抄时写错卡号、多加卡或改了回答。**试点与正式执行必须带 `--sel`**，缺省直接退出码 2、什么都不删；只有预演可以不带（用户还没选时 Agent 自己核对清单），此时 stderr 提示一行。
2. 每个 item 执行前复核：
   - 路径存在，`ino`/`dev` 与封存一致；
   - 不在保护名单；
   - 没有被进程打开（`lsof +D`，大目录改用全局 `lsof` 前缀匹配）；
   - 24 小时内没有修改。

   任一项不满足就跳过并写明原因。
3. 删除规则：
   - `scope=ignored-only`：现场重新运行 `git ls-files -o -i --exclude-standard -z -- <path>`，逐个删除文件，删完再清理空目录；绝不删除 `git ls-files` 中的跟踪文件。
   - `scope=all`：先确认路径内没有跟踪文件；有就拒绝，并提示改用 `ignored-only`。
   - `scope=files`：只删列出的、且 `(size, mtime_ns)` 与封存一致的文件。
   - `keep` 中列出的条目始终排除。
4. 原生命令条目：
   - `codex delete <UUID>` 执行前，现场查一次全部后代（见上方父子关系）；只要还有后代的会话文件存在（被跳过、失败或封存后新出现），就跳过这个父会话。
   - 退出码 0 之后现场核实：`codex delete` 的会话文件仍在，记为失败；其他命令按路径下实际占用的前后差额计字节，没有变化记为跳过。
5. 执行过至少一条成功的 `codex delete` 后，对 `~/.codex/thread_history_*.sqlite` 回收空间：`auto_vacuum=incremental` 的库分批 `PRAGMA incremental_vacuum(5000)`，WAL 模式下每批后 `wal_checkpoint(PASSIVE)`，按“主文件 + `-wal`”合计体积判断进展；空闲页不降、或合计体积连续 20 批不降即停止并记录原因；结束 `wal_checkpoint(TRUNCATE)`（失败不报错）与 `quick_check`。其他模式的库只报告、不做整库 `VACUUM`。结果写进回执的 `history_reclaim`。`act.py reclaim` 可单独补做。
6. 执行前后各取一次 `os.statvfs` 可用空间，写 `receipt.json`，内容包括：
   - 逐条结果：`removed` / `skipped` / `failed`，附原因；
   - `summary.freed_by_items` = 实际删除的字节（`bytes_by_files`，含原生命令条目按前后差额实测的部分 `bytes_native`）+ 历史库回收（`history_reclaimed`，负数按 0 计），这是对用户报告的释放量；
   - 磁盘余量变化（`freed_measured`）与它的差异单独注明：同时运行的其他程序会抵消余量，也可能是 APFS 克隆、快照、废纸篓。
7. `--dry-run` 只输出将处理的内容，不改动任何文件（只写预演回执 `receipt-dryrun-<时间>.json`，`status` 不显示它）。
8. `--pilot` 按卡片顺序只真正执行第一条结果为删除或失败的条目，其余卡片记为跳过；回执写 `receipt-pilot-<时间>.json`，不覆盖 `receipt.json`；`status` 优先显示正式回执，没有正式回执时显示最近一次试点回执。不能与 `--dry-run` 同用。
9. **回执字段**：预演、试点、正式执行的回执都带 `mode`（`dry_run` / `pilot` / `full`）、`seal`、`sel`。`sel` 无论是否传 `--sel`，都按本次实际选中的卡与答复用 `selection_sig` 算出。
10. **正式执行闸门**：非预演、非试点的 run 在动手前，要在 run 目录里找到 `seal`、`scan_id`、`sel` 都一致的回执：① `mode=dry_run` 的预演回执；② `mode=pilot` 且试点成功的回执（`pilot_done` 为真，且第一条被执行的条目状态为 `removed`、没有失败文件；`skipped`、`partial`、`failed` 都不算；回执里记为删除的那一条还要现场核对：必须是封存里的条目，路径现在已不存在，或删除样本全不存在，原生命令则要求占用小于封存时）。回执是 run 目录里的普通文件，这道核对防的是失误与旧回执，不防同一用户的蓄意伪造。缺任何一项就退出码 2、什么都不删，stderr 给出该先跑的完整命令。没有 `sel` 的旧回执不算；换了卡片或答复，`sel` 变了，必须重新预演和试点。
11. `--skip-pilot --reason "<原因>"`：只跳过②，预演仍必须有。`--reason` 必填且非空白，只能与 `--skip-pilot` 同用，不能与 `--dry-run`、`--pilot` 同用；正式回执写 `skip_pilot: true`、`skip_reason`，并在 `gate` 里记下用到的预演/试点回执文件名。
12. **试点取哪一条**：不按 `--cards` 顺序。`decide` 卡的条目优先，再按封存字节从大到小；被跳过就顺延到下一条。试点证明的是这条执行路径走得通，不证明每一条都安全。
