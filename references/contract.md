# 数据契约与安全规则（v0.6）

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
- `onboarded_at`（可选）：完成初次设置的时间，设置页导出、设置或台账成功应用过一次（含全默认的空台账）就写入；0.5 的老 profile 靠它认“设置过”。
- `setup`（0.6，可选）：`{version, at, answers}`，初始设置页保存的记录（第 7d 节）。`answers` 只存结构化答案，不存“补充”原话。**第一次使用的判据**：`setup.version` 缺失**且** `onboarded_at` 缺失 → `plan.py propose` 的 `stage` 为 `onboarding`，一律走看板，先发设置页；有 `onboarded_at` 但 `setup.version` 小于题库当前版本（`setup_q.SETUP_VERSION`）→ `stage` 为 `cleanup`，`proposal.setup_upgrade` 为真，Agent 先发只补新题的设置页再发清理看板；都不缺则 `cleanup`，不再发设置页。
- `retention_days`（可选，0/7/15/30/60/90）：最近 N 天内有改动的交互会话，`check` 整卡降为 keep；0 或缺省不按天数。看板上显示的当前值取实际设置（没设就是 0，显示“不按天数”并选中），不会替用户默认成 15。
- `default_choice`（可选，safe/recommended/all）：对话里回“好”对应的档，缺省 recommended。
- 设置页写入的字段（0.6，都可选，取值不合法一律当作没设；各字段怎么被 `check` / `propose` 消费见第 7f 节）：
  - `read_scope`：`["basic","extended","deep"]` 的子集，用户允许读哪些类别的文件；`never` 永远隐含、不存；缺省按 `["basic"]`，空数组 = 只给文件名等元信息。`peek.py` 据此拦截（第 9 节）。
  - `tools`：`{工具键: often|sometimes|unused}`，工具键 = 知识卡名（`android` / `xcode` / `docker` / `model-caches` / `creative-media` / `codex` / `claude-code` / `grok` / `gemini-antigravity` / `cursor`）。其中 android、xcode、docker、model-caches 同步台账环境项目：`often` → 进行中，`unused` → 做完了，已标“别碰”的不改。
  - `conversation_policy`：`done` / `sometimes` / `library`（旧对话：做完就不再看 / 偶尔回去接着做 / 当资料库）；同时写 `retention_days`（7 / 30 / 0，用户另选了天数以天数为准）。
  - `rejects_policy`：`drop` / `until_done` / `keep`（没挑中的 AI 图：不留 / 留到项目做完 / 都留）。
  - `worktree_policy`：`whole` / `build_only` / `ask`（合进主线的工作树：整棵删 / 只清构建产物 / 每次问）。
  - `installer_policy`：`auto` / `ask`。
  - `big_file_age_days`：`30` / `90` / `0`（0 = 录屏和大视频别问）。
  - `cloud_policy`：`ok` / `offline` / `unsure`（云盘文件“只留云端”）。
  - `done_project_policy`：`intermediate` / `archive` / `keep`（做完的项目：删中间稿 / 整个搬走 / 先留着）。
  - `governance_mode`：`tell` / `guide` / `fix`（会一直涨的东西：只告诉我 / 告诉我改法 / 同意后你来改）。
  - `delete_mode`：`trash` / `delete`（删除方式：先放废纸篓 / 直接删；缺省按 `delete`）。
  - `confirm_mode`：`smart` / `board`（`board` = 每次都发看板）。
  - `default_choice`：设置页第 12 题“能等多久”映射到它：`minimal` → `safe`，`some` → `recommended`，`space` → `all`。
  - 成品目录（第 4 题）写 `deliverable_roots` 与 `reference_roots`，别碰的目录（第 5 题）并入 `protect`（只增不减）。
- 记忆（0.6，由 `memory.py` 与 `prefs.py` 读写，第 10 节）：
  - `identities`：`{realpath: {title, producer, task, at, scan_id}}`，认下的目录；下次扫描给同路径或最近祖先节点附 `identity`，只作事实附注。
  - `governance`：`{realpath: {change, undo, at, baseline_bytes, scan_id}}`，从源头改过的设置。
  - `pending_rules`：`[{id, text, kind: protect|rule|note, value, at}]`，用户在设置页“补充”里说过、Agent 翻译后待确认的规则；清理看板每条给“对 / 不对”，确认前不生效。
  - `memory_log`：`[{id, kind: rule|identity|setting|governance|project|protect|note, text, ref, at, scan_id, forgotten?}]`，每次真正改动设置、规则、身份、保护都记一行，成果页“这次我记住了”读它，“忘掉”据 `ref` 撤销。
- `rules`（可选）：`{规则键: {"action": "delete"|"keep", "at", "spec"}}`。规则键 = “看板类别/`cards_used` 第一项/档位”（如 `ai-images/codex/recommended`），由 `check` 算出并写进封存；卡片不能自定义，没写 `cards_used` 的卡不参与规则。`delete`：这一类不再算“要你看”，但仍只进它自己那一档及以上，且对降过级、带风险提示或含交互会话的卡不适用；`keep`：这一类不进任何一档、文案不再提。
- `cooldown`（可选）：`{规则键: 截止时间戳}`，用户回“不用”后 60 天内不再问。
- `projects`（可选，0.5 项目台账）：`{项目键: {name, status, deliverables[], updated_at, source}}`。项目键 = 规范化绝对路径（`realpath`，必须是家目录下已存在的目录），环境项目用 `env:android` / `env:xcode` / `env:docker` / `env:models`（各自占的位置见 `protect.ENV_PROJECT_ROOTS`）。`status` ∈ `active`（进行中）/ `done`（做完了）/ `archived`（归档）/ `protected`（别碰）；`deliverables` 是该项目自己的成品目录（`~/…`，家目录下已存在的目录）；`source` = `board`（用户在台账看板或对话里标的）。**有效 profile**：`protect.load_profile()`（scan / plan / act 一律用它）把 `protected` 项目的目录并入 `protect`，把各项目的 `deliverables` 并入 `deliverable_roots` 与 `reference_roots`（副本核对与引用检查范围）；这只发生在内存里，`prefs.load()` 读原始内容、改写后存盘，折进去的部分不会写回文件。
- 以上几项由 `plan.py profile --apply`（看板导出的“设置:”“别再列:”“确认规则:”“不要规则:”行、台账导出、初始设置导出）与 `plan.py rule` 写入；“别碰”“成品”只接受家目录下已存在的子目录。`plan.py project --key <项目键> --status <状态>` 单独改一个项目的状态（回答“X 做完了吗”）。`onboarded_at` 在设置或台账**成功应用过一次**时写入（台账全是默认状态、没有一行需要改也算）。
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
- `identity`（0.6，可选）：`profile.identities` 里同路径或最近祖先登记过的目录身份 `{title, producer, task}`，只作事实附注，不改变任何判断。
- **扫描根（0.6）**：除 `roots` 与 `extra_roots` 外，`/private/tmp/claude-<uid>`（Claude Code 会话临时目录）存在时默认也加入扫描。`producers.json` 里它归 `claude-code` 卡；`protect.py` 只放开它下面的会话子目录，目录本身、`/private/tmp` 其他内容、当前会话（环境变量 `CLAUDE_SESSION_ID` / `CLAUDE_CODE_SESSION_ID` 给出的会话 id）一律保护；24 小时内有改动的照常保留。另有 `~/.codex/.tmp`（Codex 没在运行时可清）与 `~/Library/Caches/clang`（编译器模块缓存）两条归属规则。
- `--no-refs`（0.6）：扫描不做引用检查，所有节点 `refs` 为 `checked=false, reason:"--no-refs"`。这种快扫只给设置页出题，**不能支撑任何删除卡**：`plan.py check` 把相关卡降为保留，`probe item-refs` 也不替它补查；要出清理计划必须再跑一次完整的 `scan.py scan`。
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
- `probe projects [--run DIR] [--runs-dir DIR]`（0.5，只读）：识别项目并写 `<run>/projects.json`（见第 6b 节）。来源：① git 仓库根与链接工作树（facts 节点的 `git.repo`、家目录一二级目录下的 `.git`；链接工作树折进主仓库，记入 `roots`）；② AI 会话的工作目录（Codex 会话 `cwd`、Claude Code 项目目录名——按文件系统逐级反解，不拆字符串、Grok 会话目录名——URL 解码）归到所在仓库根，没有仓库就取家目录（或标准文件夹）下第一级目录；③ 开发环境，每类一个（android / xcode / docker / models），体积为 0 的不列；④ 家目录一级、名字含 成品 / 成片 / final / output / dist / release / export / deliver 的目录。体积来自 facts 前沿节点（归给路径最深的项目），没有事实的项目才做有限遍历（≤2 万条目、1.5 秒、总预算 20 秒，`bytes_partial: true` 表示被截断）；最后活动取节点最新修改、项目目录与 `.git` 的修改时间、会话时间中最大的。不在家目录下、隐藏目录、Library、标准文件夹本身、已不存在的目录不算项目；某类来源取不到就跳过并写进 `skipped`。stdout 输出 JSON（去掉 `session_ids`），摘要写 stderr。0.6 起同一个文件里还有出设置题用的三项机器事实（第 6b 节）：`tools`（装了哪些工具、占多少、最后使用时间）、`deliverable_candidates`（像成品目录的文件夹）、`setup_facts`（AI 对话与生成图体积、工作树数、安装包与录屏体积、云盘本机占用、最近一次 Time Machine 备份、家目录一级目录体积等）。全部只读：只 stat，不读文件内容，不触发云盘下载。
- `probe grok`：`~/.grok/sessions` 体积，`config.toml` 里的 `storage.cleanup_ttl_days`。
- `probe system`：完全磁盘访问权限（三态）、本地快照、Time Machine 目的地、可清除空间（statvfs / 容器 / Finder 三种口径）、未归因的“系统数据”估算、`/private/var/folders` 等系统大块。只读、外部命令带超时、不用 sudo。
- `probe updaters`：各软件更新暂存包与已安装版本比对（bundle id、版本号），状态 `superseded / pending_install / orphaned_update / unknown / in_use`；只有 `superseded` 的卡可写 `updater_verdict: "superseded"`。
- `probe leftovers [--min-age-days N] [--brew-zap]`：已卸载软件的残留，只报告。位置：Caches / Logs / Saved State / HTTPStorages / WebKit 的第一层目录，另有只认明确形状的 LaunchAgents 的 `<id>.plist`、Cookies 的 `<id>.binarycookies`、Group Containers 的 `<TeamID>.xxx` / `group.xxx` 目录（含登录态或可能被同厂商软件共用的位置标 `moderate`，默认不勾选）。名字匹配不用子串，用“整名 + 以分隔符或数字开头的后缀”；名字或 id 少于 4 个字符只认整串相等；已装软件清单不完整时整个探针拒绝；任何查询失败（Spotlight、进程名单、stat）一律按“软件还在”处理。
- `probe done-evidence --run <run目录> --path <目录> [--profile 文件]`（0.6，只读）：给“这个任务做完了吗”找信号，**只是信号，不是结论**。输出 `{path, exists, git, deliverables, last_modified, final_named_files, doc_hits, skipped?}`：`git` 是 `{is_repo, branch, merged_into_main, compared_with, dirty_files, last_commit_at}`；`deliverables` 是成品目录（`profile.deliverable_roots`）里与目录同名或近名的文件数和样本；`final_named_files` 是名字含完成 / 交付 / final / release 一类字样的文件；`doc_hits` 是目录前两层 README、进度、日志一类文档里含“完成”“已上线”“done”字样的行（每份最多 3 行，名字含 password / .env / secret 的不读）。要变成放行依据，必须再转成第 4 节的 `merged` / `delivered` / `copy_verified`。
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

- `keep` 卡可带非空 `question`：表示暂时不能执行、但需要用户选择调查方向。`propose` 将其放进 `review_questions` 并要求发看板；不封存为执行条目，不改变安全条件。看板单独导出“叁笙清理 · 待核查答复”，只有卡号、标题与核查方向，`act.py go --from-board` 拒绝该抬头。用户答复后须补查并重新出清理清单。
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

0.6 补充：`/private/tmp` 本身受保护，不属于 Claude 会话的内容默认受保护；除 `/private/tmp/claude-<uid>` 下的会话子目录（第 2 节）外，仅允许[阶段退役说明中的已退役测试副本](task-retirement.md#系统临时目录内已退役测试副本)通过 `retired_test_copy` 检查点进入精确文件计划：绑定单个任务临时根、原始测试回执、阶段退役授权及逐叶独立保留副本，并在处置前重验两端 SHA，不豁免其他保护；废纸篓模式（第 8 节）是把文件移**进** `~/.Trash/叁笙清理-…/`，不是去删废纸篓里已有的东西，废纸篓里的内容仍不开放删除。

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

`plan.py seal` 同时写入 `profile_guard`（`{digest, fields}`）：封存时影响判断的本机设置——`roots`、`extra_roots`、`reference_roots`、`protect`、`deliverable_roots`、`retention_days`——的规范化摘要，以及 0.5 起的 `projects`（项目台账里每个项目的 `status` 与 `deliverables`；没有台账时不进摘要，与旧摘要一致；名字、时间、来源只影响显示，不在其内）（列表排序去重，缺省值与显式默认值等价；`rules`、`cooldown`、`default_choice`、`notes`、`onboarded_at` 只影响提案档位与文案，不在其内）；0.6 起再加会改变 `check` 结果（降保留、预填、进哪一档）的设置：`tools`（只有 `unused` 的工具键进摘要，`often` / `sometimes` / 没设等价）、`conversation_policy`（`done` / `library`）、`rejects_policy`（`drop` / `keep`）、`worktree_policy`（`whole` / `build_only` / `ask`）、`big_file_age_days`（30 / 90 / 0）、`done_project_policy`（`archive` / `keep`）、`installer_policy`（`auto`）。取值等于“没设”的不进摘要，所以没有这些字段的 0.5 旧封存摘要不变。`read_scope`、`delete_mode`、`governance_mode`、`confirm_mode`、`cloud_policy`、`setup`、记忆类字段不影响放行，不在其内（改了不让封存失效）。`profile_guard` 不参与 `digest` 的计算：改了设置重新封存，卡片没变时 digest 不变，用户已发来的选择清单仍然有效。`act.py run`（含预演、试点）与 `go` 比对现在的设置与它，不同或旧封存没有这一项就拒绝（退出码 2，什么都不做），要求重新 `plan.py check` / `seal`。

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
  "tools": [{"key": "android", "name": "Android 模拟器", "bytes": 17200000000, "last_used": "2026-09-01T00:00:00Z", "paths": ["~/.android/avd"]}],
  "deliverable_candidates": [{"path": "~/Movies/成片", "why": "名字含“成片”，有 34 个视频", "bytes": 18000000000, "files": 34}],
  "setup_facts": {
    "ai_sessions_bytes": 0, "ai_images_bytes": 0, "worktrees": 0, "installers_bytes": 0, "recordings_bytes": 0,
    "cloud_local_bytes": 0, "time_machine_last": "2026-09-07T00:00:00Z", "pipelines": false,
    "top_dirs": [{"path": "~/Documents", "bytes": 0}],
    "cloud_folders": [{"path": "~/Library/Mobile Documents/com~apple~CloudDocs/素材", "provider": "iCloud Drive", "local_bytes": 0, "files": 0}]
  },
  "skipped": [{"source": "codex", "reason": "没有 ~/.codex/sessions"}]
}
```

- `kind`：主类型，优先级 `git` > `ai-session` > `env` > `deliverable-dir`；`sources` 是全部来源。环境项目的 `key` 是 `env:android` 这类，另带 `env_roots`（`~/…`）。
- `default_status`：最后活动 7 天内 → `active`；30 天以上 → `done`；其间或不知道 → `unknown`（让用户选）。
- 按 `bytes` 降序全部输出，前 15 个 `shown: true`，其余折叠；台账看板只列 `shown` 的。`sessions` 是关联的 AI 会话数（Codex 顶层线程 + Claude jsonl + Grok 会话目录）。
- `roots` / `claude_dirs` / `session_ids` 是 `check` 判断条目归属用的，不给用户看。
- **设置页用的新字段（0.6，都可缺；0.5 的 `projects.json` 没有它们，设置页当空处理）**：
  - `tools`：工具键同第 1 节 `profile.tools`；`bytes` 来自 facts 节点（取最上层、不重复计），没有节点才按已知位置有限估算；`last_used` 是该工具位置里最新的修改时间，`null` = 不知道。设置页只列 ≥ 1 GB 的。
  - `deliverable_candidates`：家目录下名字含 成品 / 成片 / 导出 / 交付 / final / output / export / release、或直接放了大量视频图片的目录（限深度与时间，不进隐藏目录、Library、依赖目录与 git 仓库内部），最多 20 个。
  - `setup_facts`：`ai_sessions_bytes` / `ai_images_bytes`（AI 对话记录、生成图和流水线目录体积）、`worktrees`（git 链接工作树数，不含主仓库）、`installers_bytes`（下载与桌面里的安装包）、`recordings_bytes`（桌面、影片、下载里的录屏与视频）、`cloud_local_bytes`（iCloud 与 CloudStorage 里已下载到本机的字节，只 stat，占位文件不计）、`time_machine_last`（最近一次备份的 UTC 时间，`null` = 没有配置或读不到）、`pipelines`（有没有认出自建流水线节点）、`top_dirs`（家目录一级目录按体积前 12）、`cloud_folders`（云盘第二层文件夹里本机占用最大的几个，给“只留云端”的只读建议用）。任何一项取不到保持默认值，不拖垮整个探针。

**台账导出行**：0.5 的项目台账看板导出的文本抬头写“叁笙清理 · 项目台账”，台账放在一行 JSON 里（路径里有 `|`、`;` 也不会被拆）。0.6 起这张表并入初始设置页（第 7d 节，导出里同样是 `台账:` 行，抬头换成“叁笙清理 · 初始设置”），清理看板上只对新项目和久没动的项目单题询问（答案仍以 `台账:` 行导出）；0.5 抬头照旧被识别和拒绝：

```
叁笙清理 · 项目台账
台账: {"项目":{"/Users/alex/Projects/短剧":{"状态":"做完了","成品":["~/Projects/短剧/成品"],"名称":"短剧"},"env:android":{"状态":"归档"}}}
```

状态接受中文（进行中 / 做完了 / 归档 / 别碰）与英文（active / done / archived / protected），键 `状态`/`status`、`成品`/`deliverables`、`名称`/`name`。`plan.py profile --apply <文件>` 解析并写入 `profile.projects`；不认识的项目键、不在家目录下的路径、不存在的目录、太宽泛的成品目录（`~/Library`、`~/Library/Caches`、`~/Downloads`、`~/Desktop`、`~/Documents` 根等，判定见 `protect.is_broad_deliverable`，设置行 `成品` 同样拒绝）、不合法的状态逐项报“没保存”；文件读不了时 `plan.py profile --apply` 给中文错误（退出码 2），其余照写；成功应用过一次（含 `{"项目":{}}` 全默认）就写 `onboarded_at`。**这种导出只保存设置，不含任何清理授权**：`prefs.is_ledger_export` 认抬头；`act.py run` 只读 `--cards` / `--answers` / `--sel` 与封存；`act.py go --from-board` 只接受抬头为“叁笙清理选择”的清单，遇到这种抬头（和“叁笙清理 · 初始设置”）直接拒绝（第 7c 节），文本里即使混进 `清理:` 行也不会产生任何清理选择。保存后项目台账变了，已封存的计划按 `profile_guard` 失效（重新 `check` / `seal`）。

## 7. 看板 `board.html`（plan.py board 产出）

- 由 `assets/dashboard.html` 模板渲染：替换 `__PLAN_JSON__`（facts 摘要 + plan + seal digest）和 `__LOGO_DATA__`。
- 左侧目录是垂直居中的深绿悬浮导航岛：总览 + 按用户认得出的类别分区成块（每区顶部写本区合计）（AI 工作 / 常规清理 / 你的文件，定义在 `plan.py` 的 `BOARD_CATEGORIES`），每类一行写“可删 x/总量”或“留 总量”。类别默认由 `cards_used` 第一项映射（`USER_SOURCES`），卡片可写 `category`（取 `BOARD_CATEGORIES` 的 id，如 Codex 出图写 `ai-images`）改归类，写错的不认；`category` 只影响显示，不进封存摘要。底栏“复制清单”左侧是“一键选择”三档按钮（写腾出量，悬停说明，另有“清空”；总览不重复放）：稳妥（删了不丢东西：会自动重新下载、失败或废弃的记录）、推荐（再加有副本的、任务已做完的过程记录和中间稿）、全部可删（再加删了找不回来的）。卡片可写 `preset`（`safe / recommended / all`），不写时 `clean` 为 `safe`、`decide` 为 `recommended`；风险提示含账号数据风险（RISKY_ZONE）的卡一律为 `all`，写低了也不认。选一档 = 勾上所有档位不高于它的可执行卡，之后可逐项加减；每个条目显示所属档（稳妥 / 推荐 / 慎重）。`preset` 只影响显示，不进封存摘要。总览先回答“已用空间都去哪了”：逐项查过的各类别、扫过但没逐项分析的在用文件（列扫描根下第一层大目录）、扫描不到的系统区域（已用 − walked_bytes），`governance` 放在总览底部（`done: true` 表示已办好）。右侧只显示当前类别：每项左侧写标题、来源与为什么会有（`summary`）、勾选前要想的问题（Agent 原写的 `question`，看板字段 `ask`）、合并后的风险提示（`risk_summary`），右侧是体积和勾选框；删了会怎样（`impact`）与路径在“详情”里。
- 勾选＝清理，没勾＝不动：decide 卡勾上即“清理”行带上它、“回答”行记 `卡号=删`；没勾的不出现在导出文本里。页面只有一个主操作“复制清单，发给 Agent”，剪贴板不可用时退回到选中文本框让用户手动复制。
- `clean` 卡初始勾选与否取 `plan.check.json` 里算出的 `default_select`（`true` 才预勾选，其余不勾）；用户仍可随时改选。
- **② 清理看板（0.6 起不再内嵌台账表和全局设置）**：改设置在对话里说“改设置”，再打开设置页（第 7d 节）。看板 payload（`plan.py board`）与 0.5 的差异：
  - 删除 `projects`（台账整表）与 `settings`。
  - 新增 `setup_summary`：`["旧对话：偶尔回去接着做（30 天内用过的留着）", …]`，按 profile 一项一句生成（`setup_q.summary_lines`，没设过的项不写），页面顶部显示为“按你的设置：……（要改就跟我说“改设置”）”。
  - `project_questions`：沿用 `proposal.project_questions`（新项目 / 久没动的进行中项目），页面渲染为一题一卡（“发现新项目《X》2.3 GB，是进行中还是做完了？”）；答案仍以 `台账:` 行导出。
  - `pending_rules`：`[{id, text, means}]`，用户在设置页“补充”里说过、Agent 翻译后待确认的规则；`text` 是 Agent 写的话，`means` 是 `plan.py pending_means` 由实际生效的 `value` 生成的中文释义（如“以后永不触碰：~/a、~/b”“以后“ai-images/codex/recommended”这一类都清”），看板两行都显示，以 `means` 为准；每条两个按钮“对 / 不对”，导出 `确认规则: p1,p2` 与 `不要规则: p3`（只导出编号，文字不进清单）。
  - `archive_enabled`：真，decide 卡的选项为 删 / 留 / 归档；选了归档时页面要求填“归档到”（外接硬盘路径，形如 `/Volumes/<盘名>/…`，内置硬盘与相对路径不收），导出 `归档到: /Volumes/X/叁笙归档`。
  - `cloud_suggestions`：`[{path, provider, local_bytes, how, link}]`，来自提案（第 7a 节），页面显示“这些文件夹可以只留云端”：只读建议，没有勾选、不动任何文件。
  - `facts.composition`：`[{id: home|apps|system|other, name, bytes}]` 整盘构成；`facts.purgeable_bytes`：访达口径的可清除空间估算，`null` = 不知道；`facts.frontier` 每项带 `category`（看板类别 id），页面可按类别下钻。总览单独露出“本地模型”一行（类别 `ai-models`），即使为 0 也写“没有本地模型”。
  - 治理区文案按 `proposal.governance_mode`：`tell` 只写现象，`guide` 写改法，`fix` 写“同意后我来改，可撤销”。
  - 左下角小字入口：GitHub、反馈问题、公众号、小红书、小宇宙。
  - 清理清单抬头仍是“叁笙清理选择”，`回答:` 的值域是 删 / 留 / 归档；`prefs.parse_board_export` 返回 `archive_to`、`confirm_rules`、`reject_rules`；`selection_sig` 仍对 `answers` 签名（归档写进 answers 即被签，归档目标不进 `sel`，由 `act.py` 单独复核，见第 8 节）。改过项目状态或有别再列时，导出文本在 `sel:` 之后多出下列行（都是整行 JSON 或编号列表，目录名里可以有 `|`、`;`；都不改变 `sel` 的计算）：
    - `设置: {"保留天数":15,"别碰":["~/a"],"成品":[],"默认":"recommended"}`：0.5 的旧写法，`plan.py profile --apply` 仍兼容（含旧的 `保留天数=15; 别碰=a|b; …` 写法）；
    - `台账: {"项目":{"<项目键>":{"状态":"做完了","成品":["~/…"],"名称":"…"}}}`（格式同第 6b 节，只是抬头仍是“叁笙清理选择”，所以这份文本**同时是**清理清单）；
    - `别再列: <规则键>, …`；
    - `归档到: /Volumes/…`、`确认规则: p1,p2`、`不要规则: p3`。
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

按封存给出三档：档位、规则键、体积（封存条目实测字节之和）和是否逐个确认都取自 `seal.json` 里每张卡的 `preset` / `rule_key` / `review` / `interactive`，封存后再改 `plan.json` 不影响执行范围（旧格式封存会被拒绝，重新 seal）；每档 `{word, label, cards, answers, sel, bytes, by_category}`：`safe`（回复“稳妥”）、`recommended`（回复“好”）、`all`（回复“都删”）。档内 = 可执行卡中 `preset` 档位不高于该档的；decide 卡的答复记为“删”；`sel` 用第 7 节同一公式。`mode` 为 `chat`（在对话里确认）或 `board`（发看板），`board_reasons` 写原因：第一次使用（`stage` 为 `onboarding`）、设置里选了“每次都发看板”（`confirm_mode=board`）、待你决定的卡超过 3 张（`BOARD_ATTENTION_MAX`），或有卡含交互会话。`mode` 为 `board` 或 `stage` 为 `onboarding` 的提案，`act.py go --choice` 一律拒绝（退出码 2，什么都不做），三档 `options` 也从不含交互会话的卡，这类提案只能走看板清单（第 7c 节）。0.5 新增字段：`stage`（`onboarding` = 第一次使用，判据见第 1 节 `setup` 条，应先发初始设置页并保存设置；`cleanup` = 否则）与 `onboarding_hint`（仅 onboarding）；`project_questions`：`[{"type":"new_projects","count":N,"projects":[{key,name,bytes,default_status}]}]`（本次 `projects.json` 里 `shown`、台账里还没有的项目）和 `[{"type":"stale_active","key","name","idle_days"}]`（台账标“进行中”、最后活动 ≥30 天），没有 `projects.json`（或 `scan_id` 对不上）就为空，两类问题也各用一句话写在 `message` 末尾；`prefill`：`{卡号: "删"}`（项目做完了 / 归档的 `decide` 卡）；`projects_by_card`：`{卡号: [{key,name,status}]}`（取自封存）。`cardkeys.json` 里属于项目的卡多一个 `projects`（项目键列表），`act.py` 记选择时据此多记带项目前缀的规则键。0.6 新增字段：`governance_mode`（`tell` / `guide` / `fix`，没设按 `guide`）、`delete_mode`（`trash` / `delete`，没设按 `delete`，与 0.5 行为一致）、`read_scope`（第二轮能读哪些文件，没设按 `["basic"]`）、`setup_upgrade`（题库比用户答过的新，Agent 先发只补新题的设置页；此时另有 `setup_hint`）、`cloud_suggestions`（`[{path, provider, local_bytes, how, link}]`：设置里第 11 题答“可以”或“不确定”，且云盘文件夹本机占用 ≥ 1 GB 时，最多列 8 个，每个写明官方的“只留云端”做法；答“不行，我要离线用”或没答为空；只读，不执行任何释放）。`prefill` 的取值扩为 `删` / `归档`（第 7f 节）。

`message` 是发给用户的一段话：默认档能腾出多少（按类别列前几项）、按规则直接清的逐个点名、要你拿主意的逐条列出标题、体积、回“好”删不删和问题、删了找不回的逐个点名（默认档为“全部可删”时写成“回‘好’也会删掉”）、三个回复词各对应什么。

`act.py go --run <目录> --choice safe|recommended|all`（对话确认；看板清单见第 7c 节）从提案读卡号、答复和 `sel`，依次调用第 8 节的 预演 → 试点 → 正式执行（闸门不变）；预演有失败条目或没有可执行条目、试点那一条没删成功时退出码 3，后面步骤不执行；正式执行有条目没删成功时其余照常完成、退出码 4；`proposal.json` 里卡号与 `sel` 对不上时由 `run` 拒绝，退出码 2（`sel` 只防转抄出错，不防有意重算；执行范围由封存兜底）。

## 7b. 成果页 `report.html` / `report.json`（report.py 产出，`act.py go` 与 `act.py run` 正式执行后自动生成）

- 释放量 = 同一 `scan_id`、同一 seal 下全部正式回执（`receipt.json` 与带时间戳的 `receipt-<时间>.json`，中断后重跑、分几次执行都会有多份）和试点回执（`receipt-pilot-*.json`）里 `removed/partial` 条目的 `removed.bytes` 之和，加 Codex 历史库回收（计入“AI 对话记录”）；预演回执不计。同一份回执只算一次；同一卡同一路径在多份回执里都记为 `removed` 的 delete 条目只取最大的一份（整个路径不可能删两次），`partial` 与原生命令条目逐份累加。试点先删的那一条在正式回执里是“已不存在”跳过，只看最后一份正式回执会少算。回执文件名为 UTC 秒级时间戳，同一秒内再写一份时顺延一秒，不覆盖。
- 所有体积文案（act 打印、check/提案、成果页、看板、扫描摘要）统一用十进制：1 GB = 1e9 字节，一位小数，不足 1 GB 写 MB（`scripts/units.py` 的 `fmt_bytes`，两个页面里的 JS 与它逐值一致，测试在 node 里对照）。
- 已用前 = 磁盘总量 − 第一份回执的 `free_before`；已用后 = 已用前 − 释放量。按类别汇总用与看板相同的归类（`plan.card_category`），`ai_freed` = “AI 工作”分区之和。
- 分享链接 `SHARE_BASE?v=1&f=&b=&a=&ai=`：只带腾出量、清理前后已用量、AI 部分（GB，一位小数），不带路径、用户名、卡片标题。二维码矩阵由内置的 `scripts/vendor/qrcodegen.py`（Project Nayuki，MIT）在 Python 端编码，页面只负责绘制；分享图在浏览器里用 canvas 画成 1080×1440 PNG。

**0.6 成果页新增内容**（数据文件不存在时对应区块不显示）：

**0.7 补充**：看板确认区必须按 `proposal.delete_mode` 说明本轮是直接永久删除，还是先放废纸篓、暂不释放。仅暂存时，成果页与分享图的首屏显示暂存量和待完成步骤，同时明确实际释放为 0；小于 1 GB 用 MB/KB，避免四舍五入变成 0.0 GB。用户授权永久删除后，`finalize_trash.py --run DIR --seal DIGEST` 先封存内容指纹预演，`--execute <摘要> --reason <用户原话>` 只删除本轮回执可证明的普通单文件，要求原 inode 一致、无软硬链接、未被占用、内容未变化；目录条目不支持。逐文件写 `finalize-trash.json`，原移动回执不改；报告按精确路径将已完成文件从待释放转为已释放，不重复计算。文件已不在原废纸篓位置、无法验证或空输入均拒绝；不推断已被清空，也不清空整个废纸篓。

- **处置方式**（`report.json` 的 `disposal`：`{freed, trash, trash_dirs, archived:[{volume, source, bytes}]}`）：释放量只算 `disposal_flags` 判为“已腾出”的条目——直接删除的、归档后原文件已删的；移入废纸篓的字节单列为“已移入废纸篓，清空废纸篓后才会腾出”（附废纸篓里的文件夹名，不替用户清空，上面的“已腾出”与分享链接都不含它）；搬到外接硬盘的按盘名单列，注明原文件已删除还是在废纸篓里。没有 `disposal` 字段的旧回执按“已删除”算。
- **这次读了哪些文件**：读 `<run>/reads.jsonl`（`peek.py` 每次调用追加一行），显示读过内容 / 只看了元信息 / 被拒绝的次数与前 20 条读过内容的路径（口径同 `peek.py --summary`，路径已是 `~/…`），标题旁写“这些内容发给了你的 AI 服务商”。
- **这次我记住了**：读 `<run>/remembered.json`（`memory.py remembered` 写；`act.py go` 在成果页之前自动生成），每条带“不对，忘掉”勾选，勾了就出现复制按钮，复制 `忘掉: m1,m2` 发给 Agent，Agent 运行 `memory.py forget --from-text <文件>`。
- **从源头改好的**：读 `<run>/governance-check.json`（`memory.py govern check` 写），`stopped` 写“已停止增长”，`growing` 写“还在涨，下次再看”，`unknown` 只写改了什么。

## 7c. 看板清单一条命令 `act.py go --run <目录> --from-board <导出文本文件>`（0.5）

输入是 ② 清理看板“复制清单，发给 Agent”的整段文本（第 7 节格式，抬头“叁笙清理选择”；`scan_id:`、`seal:`、`清理:`、`回答:`、`sel:` 五行必须各出现一次，`seal` / `sel` 为 12 位小写十六进制；`清理:` 与 `回答:` 可以写 `无`，回答之间用 ` / ` 分隔；`设置:`、`台账:`、`别再列:`、`归档到:`、`确认规则:`、`不要规则:`、`备注:` 可有可无；`回答:` 的值是 删 / 留 / 归档）。与 `--choice` 二选一。`prefs.parse_board_export` 只解析，不核对。

顺序（任何一步不满足就停，之前的步骤不回退但之后的不执行）：

1. 抬头是“叁笙清理 · 项目台账”（0.5 台账看板的导出）或“叁笙清理 · 初始设置”（设置页的导出）→ 拒绝（退出码 2，什么都没删），提示用 `plan.py profile --apply` 保存；这两种文本从不是清理授权，即使里面混进合法的清理行也不执行（设置页导出没有 `seal:` / `sel:`，清单解析同样会因抬头不是“叁笙清理选择”而报格式错误）。
2. 解析清单、解析设置/台账行；格式不对 → 拒绝，什么都不保存。
3. `seal` 必须与 run 目录里的 `seal.json` digest 一致，`scan_id` 必须与封存的一致；按清单里的卡片与回答重算 `selection_sig` 必须等于清单的 `sel`（先于保存设置，对不上的清单不改动本机设置）；没有选中任何卡片也拒绝。`sel` 只来自清单，本命令从不重新生成它。
4. 有 `设置:` / `台账:` / `别再列:` / `确认规则:` / `不要规则:` 行 → 调 `prefs.apply_settings`（与 `plan.py profile --apply` 同一实现；规则编号交给 `memory.confirm` / `memory.reject`）保存，且在任何删除之前；有路径没保存成功 → 停。
5. 保存后（或本来就是）封存的 `profile_guard` 与现在的设置不一致 → 在进程内调 `plan.py seal`（内含 `check`，同样写 `plan.check.json`）重新封存；新 digest 与清单里的不同 → 停，打印原因（设置/台账让哪些卡的判断变了，或判断没变但卡里目录的内容在封存之后又变了：实测体积、文件大小、修改时间都是封存内容的一部分，不沿用旧值）、“需要用新看板重新确认”和 `plan.py propose` / `plan.py board` 的命令；相同则继续。
6. 预演 → 试点 → 正式执行，与 `--choice` 共用同一段 `_go_steps` → `cmd_run`：`sel`、`profile_guard`、预演与成功试点的回执核对一步不少；清单里的 `归档到:`（优先）或命令行 `--archive-to`、`--trash` / `--no-trash [--trash-reason]` 一并传下去（第 8 节）；结束时先汇总“这次我记住了”并复查“从源头改好的”，再自动生成成果页。每步打印 `【go】…`，适合 `nohup` 后台运行。

退出码：`0` 完成；`2` 拒绝（台账导出、清单格式不对、scan_id / seal / sel 对不上、没有选中卡片、闸门拒绝，什么都没删）；`3` 停下请用户看（设置/台账让判断变了需要重新确认、设置没保存全、重新封存没通过、预演有失败或没有可执行条目、试点没成功）；`4` 正式执行有条目没删成功，其余已完成。

## 7d. 初始设置页 `plan.py setup --run <目录> [--mode first|upgrade|edit] [--out 文件]`（0.6，模板 `assets/setup.html`）

第一次使用时发的独立页面，标题“认识你的电脑”。题库、出题条件、推荐值、导出解析与答案落地都在 `scripts/setup_q.py`（单一真源，`SETUP_VERSION`、`SETUP_TITLE`）；`plan.py onboard` 是 `setup` 的别名，旧命令照用。0.5 的独立台账页已取消，项目表成为设置页第 3 题。

**触发与模式**（`--mode` 缺省按 profile 自动判断）：
- `first`：没有 `setup.version` 也没有 `onboarded_at`（第 1 节），出全部适用的题。
- `upgrade`：设置过但题库比用户答过的新（`proposal.setup_upgrade` 为真），只出新增的题（0.5 老用户只补 0.6 新增的题，不重问项目状态，`projects` 为空数组）。
- `edit`：用户说“改设置”或“重新认识我的电脑”，用最近一次扫描重新出题，每题 `current` 预填现值，导出只带改动过的题。
- 没有题可问时（如 `upgrade` 模式下没有新增的题）只打印提示，不写页面。

**数据**：读 `<run>/projects.json`（第 6b 节，须是 `schema: projects/1` 且 `scan_id` 与 `facts.json` 一致），写 `<run>/setup.html`；占位符 `__SETUP_JSON__`、`__LOGO_DATA__`。payload（`setup_q.build_payload`）：

```json
{"version": 1, "scan_id": "…", "mode": "first|upgrade|edit",
 "groups": [{"id": "read", "title": "我会看什么", "desc": "…", "questions": [<题对象>]}],
 "projects": [<0.5 台账看板的项目行：key, name, kind, bytes, last_active, sessions, deliverables_guess, default_status, status, deliverables, in_ledger>],
 "deliverable_candidates": [], "top_dirs": [],
 "tools": [{"key", "name", "bytes", "last_used", "paths", "recommended": "often|null", "current": "often|sometimes|unused|null"}],
 "time_machine_last": "ISO|null",
 "privacy_note": "读的内容会发给你所用的 AI 服务商（如 Claude、OpenAI），不只在本机看"}
```

题对象：`{id, no, group, type: multi|single|tools|projects|paths|text, title, context, options: [{value, label, desc}], recommended, current, why, risk}`；“对话”题另带 `days`（可选天数 7 / 15 / 30 / 60 / 90）、`days_recommended`、`current_days`，“删除方式”题带 `time_machine_last`。`context` 是带着本机发现的一句话（“找到 Codex、Claude 等对话共 41.2 GB”），`why` 写清理时用在哪，`risk` 写答错了会怎样。分组：`read`（我会看什么）、`machine`（你电脑上有什么）、`ai`（你用 AI 的习惯）、`files`（日常文件）、`way`（清理的方式）、`note`（还有想让我知道的吗）。

**题库**（17 题，按机器出题，条件读 `projects.json` 的 `tools` / `projects` / `setup_facts`；没有对应情况的题不出，空机器只出总是出现的题）：

| 题 | id | 出现条件 | 推荐 | 写入 profile |
|---|---|---|---|---|
| 1 我可以看哪些文件 | `read_scope`（多选 basic / extended / deep；“永不读”锁定不可勾） | 总是 | basic + extended | `read_scope` |
| 2 这些软件和开发环境还用吗 | `tools`（每个工具 often / sometimes / unused） | 有 ≥ 1 GB 的工具 | 30 天内用过的预选 often，其余不预选（没答按 sometimes） | `tools`，并同步环境项目 |
| 3 你的项目是在做还是做完了 | `projects`（走 `台账:` 行） | 有 `shown` 的项目 | 同 0.5：最近有改动的预选进行中，猜测“做完了”的只提示 | `projects`（台账） |
| 4 做好的成品放在哪 | `deliverables`（目录） | 有项目或 AI 生成图 | 不预选，列 `deliverable_candidates` | `deliverable_roots`、`reference_roots` |
| 5 哪些文件夹不管多久没动都不能碰 | `protect`（目录） | 总是 | 不勾 | `protect`（只增不减） |
| 6 用完的 AI 对话 | `conversations`：done / sometimes / library（可附 `conversation_days`） | 有 AI 对话记录 | sometimes | `conversation_policy`、`retention_days` |
| 7 没挑中的 AI 图和视频 | `rejects`：drop / until_done / keep | 有生成图或流水线目录 | until_done | `rejects_policy` |
| 8 合进主线的工作树 | `worktrees`：whole / build_only / ask | 有 git 工作树 | build_only | `worktree_policy` |
| 9 装好后的安装包 | `installers`：auto / ask | 安装包 ≥ 300 MB | auto | `installer_policy` |
| 10 录屏和大视频多久没动才问 | `big_files`：30 / 90 / 0 | 录屏和大视频 ≥ 1 GB | 90 | `big_file_age_days` |
| 11 云盘文件能接受只留云端吗 | `cloud`：ok / offline / unsure | 云盘本机占用 ≥ 5 GB（`CLOUD_ENABLED` 开着，现为开） | unsure | `cloud_policy` |
| 12 重新下载、生成能等多久 | `wait`：minimal / some / space | 总是 | some | `default_choice` |
| 13 做完的项目素材怎么处理 | `done_projects`：intermediate / archive / keep | 总是 | intermediate | `done_project_policy` |
| 14 发现东西一直涨时 | `governance`：tell / guide / fix | 总是 | fix | `governance_mode` |
| 15 删掉的东西怎么处理 | `delete_mode`：trash / delete，题旁显示最近一次 Time Machine 备份 | 总是 | 第一次使用、或没有备份（读不到或超过 14 天）→ trash，否则 delete | `delete_mode` |
| 16 以后怎么跟你确认 | `confirm`：smart / board | 总是 | smart | `confirm_mode` |
| 17 还有什么想让我知道的 | `note`（文本） | 总是 | 空 | 不存（见下） |

页面顶部和底部各有一个“全部按推荐”；顶部显示进度（“第 2 组，共 6 组”）；不确定的题默认取更保守的选项。页面只保存设置，没有任何删除选项。

**导出文本**（点“复制，发给 Agent”；每行 JSON 必须单行，U+2028 / U+2029 / NEL 转义，`setup_q.export_text` 是 Python 端同格式实现）：

```
叁笙清理 · 初始设置
设置版本: 1
答案: {"read_scope":["basic","extended"],"tools":{"android":"unused"},"conversations":"sometimes","conversation_days":30,…}
台账: {"项目":{"<项目键>":{"状态":"做完了","成品":["~/…"],"名称":"…"}}}
补充: "下载里的视频都是课程，别动"
```

首行固定为“叁笙清理 · 初始设置”（`setup_q.SETUP_TITLE`，`setup_q.is_setup_export` 据此识别）；`设置版本:` 必须有；`答案:`、`台账:`（0.5 格式，第 6b 节）、`补充:`（JSON 字符串）可缺，三者都没有算错误。`first` 与 `upgrade` 导出所有出现的题（含按推荐未改动的；没答也没推荐的工具不导出），`edit` 只导出改动过的题与项目行。“永不读”永远不写进答案。

**保存**：Agent 把导出存成文件，`python3 scripts/plan.py profile --apply <文件> [--scan-id <scan_id>]`（`setup_q.apply_export`）。设置版本比 Skill 认识的新就拒绝。先写答案再写台账：台账里用户逐项标的状态比“软件还用不用”的同步更具体，以台账为准。每题一条映射（第 1 节字段），非法值逐项报“没保存”、其余照写；每道真正改了设置的题写一条 `memory_log`（kind `setting`，`ref` 记旧值，供“忘掉”撤销）。成功后写 `setup = {version, at, answers}`（`answers` 只存结构化答案，不存补充原话）和 `onboarded_at`。保护名单只增不减；设置页取消掉的、上一次由它写进去的成品目录会被去掉，别处加的不动。

**补充（第 17 题）**：原话**不存档**。`plan.py profile --apply` 把它原样打印出来，Agent 翻译成一句规则或备注，用 `memory.py pending add`（第 10 节）记成待确认；第一张清理看板上用户点“对 / 不对”，确认后才进 profile（`protect` 并入保护名单，`rule` 写进 `rules`，`note` 写进 `notes`）。

**与清理的隔离**：设置导出不含 `seal:` / `sel:`，`act.py run` 只读 `--cards` / `--answers` / `--sel` 与封存，`act.py go --from-board` 只接受抬头为“叁笙清理选择”的清单、遇到“叁笙清理 · 初始设置”（和“叁笙清理 · 项目台账”）一律拒绝（第 7c 节）；文本里即使混进 `清理:` 行也不会产生任何清理选择。设置只改变要不要问、预填什么、进哪一档，**不改变能不能放行**；唯一让保护更严的方向是别碰和永不读。保存后影响判断的设置变了，已封存的计划按 `profile_guard` 失效，重新 `check` / `seal`。用户在对话里说“跳过设置”，等价于 apply 一份只有 `设置版本: 1` 和 `答案: {}` 的导出（全按默认，写上 `setup` 与 `onboarded_at`，以后不再弹）。

清理看板的顶部摘要（`setup_summary`）、各设置对 `check` / `propose` 的影响见第 7f 节。

## 7e. 反馈 `feedback.py` 与成果页首次 Star（0.5）

- `feedback.py gaps --run <run> [--min-gb 1] [--text]` 输出 `feedback-gaps/1` JSON：`asked`（现在是否可以问用户）、`reason`（`ok` / `declined` / `none`）、`gaps`（`pattern`、`bytes`、`size`、`count`、`software`）、`total_dirs`、`preview`、`hint`。缺口判据：前沿节点、未受保护、体积 ≥ 阈值、`producer.card` 与 `producer.tool` 均为空。
- `feedback.py issue-url [--json]` 生成 GitHub Issue 预填链接（用户自己点开、自己提交；整条链接超过 7000 字符时少列条目并写“另有 N 条省略”）。`feedback.py decline` 写 profile 的 `feedback_declined_at`，冷却 90 天。
- 路径脱敏：用户名换 `~`；家目录下只有众所周知目录名（`feedback.WELL_KNOWN`）、一级点目录、`Library/{Application Support|Containers|Group Containers}` 的直接子目录原样保留，其余换 `<目录>`；外置卷名换 `<卷>`，其他用户换 `<用户>`。反馈不含文件名、文件内容、项目名。
- profile 新增 `star_asked_at`、`feedback_declined_at`，都不在 `PROFILE_GUARD_FIELDS` 内（写它们不会让封存失效）。
- `report.make` 是唯一写 `star_asked_at` 的地方，`build_report` 纯计算；`report.json` 新增 `star: {show, url}`，只有第一次清理的成果页 `show` 为真。
- `act.py run` 正式执行成功后调用 `finish_report(run)`：生成成果页，再打印反馈提示行；`cmd_go` 内部三步传 `no_report=True`，全部结束后调一次，避免重复。

## 7f. 设置对 `check` / `propose` 的消费（0.6）

安全不变式：设置只改变“问不问、预填什么、进哪一档”，**不改变能不能放行**；任何设置都不能让一张卡从 `decide` 升为 `clean`，也不能绕过结束证据复核、引用核对、进程与 24 小时改动检查。唯一让保护更严的方向是别碰和永不读。每一项都有测试：同一份 facts 与计划，设置取两种值时 `check` 或 `propose` 输出不同；另有“所有设置取最激进值也没有任何卡 `decide` → `clean`”的反例测试。

| 设置 | `check` / `propose` 的变化（卡指 `cards_used` 第一项或看板类别） |
|---|---|
| `tools[x]=unused` | 该工具知识卡的 `decide` 卡（无风险提示、不含交互会话）`prefill` 为“删” |
| `conversation_policy=library` | 含交互会话的卡整卡降 `keep`，原因“你说过旧对话当资料库，都留着” |
| `conversation_policy=done` | 只影响提问措辞与保留天数，**不预填**：含交互会话的卡永不预填（0.6 评审 13，与 0.5 一致），做完项目里的对话仍逐个确认 |
| `rejects_policy=drop` | 类别 `ai-images` 的 `decide` 卡 `prefill` “删” |
| `rejects_policy=keep` | 类别 `ai-images` 且没有通过复核的 `copy_verified` / `delivered` 证据的卡降 `keep`，原因“你说过没挑中的图都留着……” |
| `worktree_policy=whole` | 知识卡 `git-worktree-build` 的 `decide` 卡 `prefill` “删” |
| `worktree_policy=build_only` | 同类卡里条目正好是某个 git 工作树根的 `clean` 卡降 `decide`，问题“只清构建产物，整棵工作树先留着？” |
| `worktree_policy=ask` | 同类卡 `preset` 至少 `recommended`，且不适用“以后都删”的规则 |
| `installer_policy=auto` | 知识卡 `app-updaters-installers`，或条目扩展名是安装包（`.dmg` / `.pkg` / `.mpkg` / `.zip` / `.xip` / `.iso`）的卡，提案按 `rule=delete` 处理（和一条“以后都删”规则一样，仍不适用于降过级、带风险提示或含交互会话的卡；用户明确的“留”规则优先） |
| `big_file_age_days=N` | 知识卡 `large-old-files` 里最近 N 天内有改动的条目，整卡降 `keep`，原因“不到你定的 N 天” |
| `big_file_age_days=0` | `large-old-files` 卡一律降 `keep`，原因“你说过录屏和大视频别问” |
| `done_project_policy` | `archive`：项目做完了 / 归档的 `decide` 卡 `prefill` “归档”而不是“删”；`keep`：不再预填；`intermediate`（缺省）：预填“删”（0.5 行为） |
| `default_choice` | 同 0.5：对话里回“好”对应的档 |
| `confirm_mode=board` | `needs_board` 给原因“你选了每次都发看板”，`proposal.mode` 为 `board` |
| `governance_mode` / `delete_mode` / `read_scope` | 原样写进 `proposal`；治理文案、`act.py` 的默认处置方式、`peek.py` 的读取拦截据此变化（本身不改变放行） |
| 重建很慢的缓存 | 卡片写了 `rebuild: "slow"` 或首卡是 `model-caches`（`plan.py` 的 `SLOW_REBUILD_CARDS`）：`preset` 为 `safe` 时提为 `recommended`，慢重建缓存不进稳妥档 |
| `cloud_policy` | 只决定提案里有没有 `cloud_suggestions`，不影响任何卡 |

`prefill` 只是预填，仍要用户确认；有风险提示的卡、含交互会话的卡（任何设置下）不预填。`tools[x]=often` 没有对应的 `check` 行为，只是提示 Agent 第二轮跳过深查（SKILL.md）。

## 8. 执行 `act.py run --run <目录> --seal <digest> --cards c01,c03 [--answers c05=归档] [--dry-run | --pilot | --skip-pilot --reason <原因>] [--trash | --no-trash [--trash-reason <用户原话>]] [--archive-to /Volumes/盘名/目录]`（`go` 同样接受 `--trash` / `--no-trash` / `--trash-reason` / `--archive-to`，但没有 `--reason`）

1. 读取 `seal.json`，`digest` 必须一致；`profile_guard` 与现在的本机设置不一致（或旧封存没有）就拒绝，退出码 2；只处理指定卡片中封存过的 items。
   `--sel <12位>`：按上面的公式，用 seal digest、本次选中的卡（`--cards` 并上答复“删”的卡）和全部 `--answers` 重算，不符直接拒绝（退出码 2，不做任何删除）。**从看板导出文本来的运行必须带上其 `sel:` 值**，用来防止转抄时写错卡号、多加卡或改了回答。**试点与正式执行必须带 `--sel`**，缺省直接退出码 2、什么都不删；只有预演可以不带（用户还没选时 Agent 自己核对清单），此时 stderr 提示一行。
2. 每个 item 执行前复核：
   - 路径存在，`ino`/`dev` 与封存一致；
   - 不在保护名单；
   - 没有被进程打开（`lsof +D`，大目录改用全局 `lsof` 前缀匹配）；
   - 24 小时内没有修改。

   任一项不满足就跳过并写明原因。
3a. **处置方式（0.6）**：三种——`delete` 直接删（`os.unlink`）、`trash` 移入废纸篓、`archive` 复制到外接硬盘再处置原文件。选择顺序：答复“归档”的卡走 `archive`；其余的 `--trash` 强制废纸篓、`--no-trash` 强制直接删，都没给读 `profile.delete_mode`，缺省 `delete`（与 0.5 一致）。`native` 条目不受影响（照常由原生命令处理，回执 `disposal.mode` 记 `native`），回执里标明。
   - **`trash`**：文件 `os.rename` 到 `~/.Trash/叁笙清理-<scan_id>-<UTC 时间>/<相对家目录的路径>`（家目录外的路径放 `_root/<绝对路径去掉开头 />`），保持目录结构，先建父目录；原有的身份检查（inode / 设备）与保护检查照旧；目标已存在则该文件记失败、不覆盖；跨卷 `rename` 失败（`EXDEV`）→ 该文件 `skip`，原因“不在同一块盘，没法放进废纸篓”（并清掉为它建的空目录）。
   - **`archive`**：需要归档目录（`--archive-to` 或看板导出的 `归档到:`，后者优先）。执行前整体校验，任一不满足整卡 `skip` 并写清原因：必须在 `/Volumes/<盘名>/` 下且真实路径仍在 `/Volumes` 下；目录存在、可写；与家目录不在同一块盘；剩余空间不小于本次全部归档卡封存字节合计的 1.05 倍（原生命令条目不算，见下）。通过后逐文件 `shutil.copy2` 到 `<归档目录>/叁笙归档-<scan_id>/<相对家目录的路径>`（先写进同目录里本次独有的 `.叁笙-tmp-*` 临时目录，数据 `F_FULLFSYNC`（不支持则 `fsync`）落盘并校验，再用 `os.link` 硬链接成目标名（目标已存在即 EEXIST，同哈希认、不同判失败，绝不覆盖；文件系统不支持硬链接——EPERM / ENOTSUP / EXDEV 等——才退回 `os.rename`），目标目录同步落盘，再回读校验），**全部通过才处置原文件**（原文件再按删除方式 `trash` 或 `delete` 处理）；每个目标文件的真实路径必须仍在归档根之内、所在设备与源文件不同，否则该文件失败、原件保留。校验不一致、同名但内容不同、复制失败，原文件保留并记失败；归档开始时顺手清理归档根下超过 24 小时的 `.叁笙-tmp-*` 残留目录（只清这个前缀、只在本次归档根内）；只会删除本次自己创建的临时文件，绝不 unlink 目标位置已有的文件。重跑时目标已有逐字节相同的副本直接认。含 `native`（原生命令）条目的卡答复“归档”时，该条目一律跳过（“原生命令没法归档，先留着”），看板对这类卡不给“归档”选项。`sel` 在选了归档时多带 `archive_to`（规范化后的路径），为空时与 0.5 的签名逐位一致；预演/试点回执记录 `delete_mode` 与 `archive_to`，正式执行与之不同即拒绝；`--no-trash` 与设置 `delete_mode=trash` 冲突时必须同时给 `--trash-reason "<用户原话>"`（写进回执 `no_trash_reason`；`--reason` 只给 `--skip-pilot`，两者不互相顶替）。归档不免除结束证据、引用检查和其他任何复核，看板上“归档”只是 `decide` 卡的第三个答复。
   - 对话确认不支持归档，归档只走看板（`go --choice` 不用于归档）；记飞轮选择时，答复“归档”的卡不算“删”也不算“留”，不计入“以后都这样吗”。
3. 删除规则（在 3a 选定的处置方式下逐个文件进行）：
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
   - `summary.freed_by_items` = 真正腾出的字节（`bytes_freed_now`：直接删除的文件，含原生命令条目按前后差额实测的部分 `bytes_native`，以及归档后原文件已删的部分）+ 历史库回收（`history_reclaimed`，负数按 0 计），这是对用户报告的释放量。**0.6 口径**：移入废纸篓的字节（`bytes_trash`）清空废纸篓后才腾出，不计入 `freed_by_items`，否则会和磁盘余量变化对不上；归档到外接硬盘的字节记 `bytes_archived`；`bytes_by_files` 仍是“处理掉的源文件字节”（含废纸篓与归档），`removed.bytes` 的口径不变；
   - 磁盘余量变化（`freed_measured`）与它的差异单独注明：同时运行的其他程序会抵消余量，也可能是 APFS 克隆、快照、废纸篓。
7. `--dry-run` 只输出将处理的内容，不改动任何文件（只写预演回执 `receipt-dryrun-<时间>.json`，`status` 不显示它）。
8. `--pilot` 按卡片顺序只真正执行第一条结果为删除或失败的条目，其余卡片记为跳过；回执写 `receipt-pilot-<时间>.json`，不覆盖 `receipt.json`；`status` 优先显示正式回执，没有正式回执时显示最近一次试点回执。不能与 `--dry-run` 同用。
9. **回执字段**：预演、试点、正式执行的回执都带 `mode`（`dry_run` / `pilot` / `full`）、`seal`、`sel`、`delete_mode`（`delete` / `trash`）。`sel` 无论是否传 `--sel`，都按本次实际选中的卡与答复用 `selection_sig` 算出。0.6 起每个条目带 `disposal: {mode: delete|trash|archive|native, dir, …}`：`trash` 的 `dir` 是废纸篓里本次的文件夹，`archive` 另带 `source`（原文件的处置：`delete` / `trash`）、`volume`（盘名）、`source_dir`；答复“归档”的卡在回执卡片条目上标 `archive: true`，回执顶层有 `trash_dir` 与（归档时）`archive_to`。没有 `disposal` 字段的旧回执、原生命令条目，成果页都按“已删除”算。预演时每条 `reason` 写明将怎么处置（“将删除 / 将移入废纸篓 / 将搬到 …”）。
10. **正式执行闸门**：非预演、非试点的 run 在动手前，要在 run 目录里找到 `seal`、`scan_id`、`sel` 都一致的回执：① `mode=dry_run` 的预演回执；② `mode=pilot` 且试点成功的回执（`pilot_done` 为真，且第一条被执行的条目状态为 `removed`、没有失败文件；`skipped`、`partial`、`failed` 都不算；回执里记为删除的那一条还要现场核对：必须是封存里的条目，路径现在已不存在，或删除样本全不存在，原生命令则要求占用小于封存时）。回执是 run 目录里的普通文件，这道核对防的是失误与旧回执，不防同一用户的蓄意伪造。缺任何一项就退出码 2、什么都不删，stderr 给出该先跑的完整命令。没有 `sel` 的旧回执不算；换了卡片或答复，`sel` 变了，必须重新预演和试点。
11. `--skip-pilot --reason "<原因>"`：只跳过②，预演仍必须有。`--reason` 必填且非空白，只能与 `--skip-pilot` 同用，不能与 `--dry-run`、`--pilot` 同用；正式回执写 `skip_pilot: true`、`skip_reason`，并在 `gate` 里记下用到的预演/试点回执文件名。
12. **试点取哪一条**：不按 `--cards` 顺序。`decide` 卡的条目优先，再按封存字节从大到小；被跳过就顺延到下一条。试点证明的是这条执行路径走得通，不证明每一条都安全。

## 9. 读取授权 `scripts/peek.py`（0.6）

Agent 读用户的文件一律经这里，不直接 `cat` 或用别的方式打开：

```
python3 scripts/peek.py --run <run目录> <路径> [--lines 200] [--bytes 65536] [--meta]
python3 scripts/peek.py --run <run目录> --summary
```

**分类**（`classify`，先取真实路径，按家目录相对路径与文件名匹配，不区分大小写；优先级 never > extended > deep > basic）：

- `never`：钥匙串（`~/Library/Keychains/**`、`*.keychain*`）、浏览器登录与 Cookie（`Login Data*`、`Cookies*`、`Web Data`、`key4.db`、`logins.json`）、`~/.ssh/**`、`~/.gnupg/**`、`.env` 与 `.env.*`、`*.pem`、`*.key`、`*.p12`、`*.kdbx`、任意 `id_*` 私钥（`*.pub` 公钥除外）、`*.ppk`、`*.pfx`、`*.jks`、`*.keystore`、`~/.android/adbkey*`、任意目录的 `.npmrc` / `.envrc` / `.mcp.json` / `*mcp*.json`、`claude_desktop_config.json`、Chrome 系 `Local State`、路径里含 `Local Storage` / `Session Storage` / `IndexedDB` / `tdata` 的内容、`state.vscdb*`、`wallet.dat`、`~/.config/github-copilot/**`、`.pgpass`、`.my.cnf`、`.s3cfg`、`.boto`、`.terraformrc`、`*.tfstate*`、`~/.m2/settings.xml`、`~/.gradle/gradle.properties`、`service-account*.json`、文件名切词后相邻的 api+key / private+key / access+key / secret+key（按整词，author.md、tokenizer.json、keyboard.md 不误伤）、`.netrc`、`~/.aws/credentials`、`~/.config/gh/hosts.yml`、`~/.codex/auth.json`、`~/.claude/.credentials.json`、文件名含 password / 密码、1Password / Bitwarden / KeePass 的数据目录。
- `extended`：聊天软件收到的文件（微信 `msg/file`、`msg/video`）、邮件附件（`~/Library/Mail` 下叫 `Attachments` 的目录）、照片图库（`*.photoslibrary`）、桌面与 `~/Pictures/Screenshots` 下的截图（`截屏*`、`Screenshot*`）。
- `deep`：聊天与邮件正文（`~/Library/Messages`、`~/Library/Mail` 的 `*.emlx`、微信 `Message` 与 `db_storage`、QQ `nt_msg`、Telegram `postbox/db`）、图片内容（png / jpg / jpeg / heic / webp）。
- `basic`：其余。

**授权**（`decide`，授权取 `profile.read_scope`，缺省 `["basic"]`，空数组 = 只读文件名）：

| 类别 | 授权了什么 | 结果 |
|---|---|---|
| `never` | 任何设置 | 一律拒绝，退出码 3，只记 `refused` |
| `extended` | 含 `deep` 且没加 `--meta` | 给内容（判断截图、重复照片要看图） |
| `extended` | 只含 `extended`（或含 `deep` 但加了 `--meta`） | 只给元信息：名字、大小、类型、修改时间 |
| `extended` | 都没授权 | 拒绝，退出码 3，连名字都不给 |
| `deep` | 含 `deep` 且没加 `--meta` | 给内容 |
| `deep` | 没授权 | 只给元信息 |
| `basic` | 含 `basic` 且没加 `--meta` | 给内容 |
| `basic` | 没授权，或加了 `--meta` | 只给元信息 |

目录只给元信息和前 50 个子项名（`never` 类子项连名字也隐去，写“（凭证类，已隐去）”）。文本内容按 `--lines` / `--bytes` 截断；二进制只给元信息与类型，授权了 `deep` 的图片会提示用自己的看图工具打开。设置只能决定“进哪一档”，**不能把 `never` 放行**。

**留痕**：每次调用（含被拒绝的）往 `<run>/reads.jsonl` 追加一行 `{"at","path","class","mode":"content|meta|refused"}`，路径写成 `~/…`。`--summary` 输出 `{content, meta, refused, paths}`（计数按调用次数，`paths` 是去重后前 50 条读过内容的路径），成果页“这次读了哪些文件”读同一份数据。

注意：脚本自己的只读探针（如 `probe done-evidence` 读目录前两层的 README、进度文档）不经过 `peek.py`、不记入 `reads.jsonl`，它们只在固定的文档名范围内取含“完成”字样的几行，且跳过名字含 password / `.env` / secret 的文件。

## 10. 记忆 `scripts/memory.py`（0.6）

读写 profile 里的四块记忆（`identities`、`governance`、`pending_rules`、`memory_log`，第 1 节）。原则：**记忆只改变问不问、预填什么，不改变能不能放行**；`forget` 必须真的撤销，日志里只标 `forgotten: true`，不物理删。

- `identity set --path P --title T [--producer X] [--task Y] [--scan-id S]` / `identity forget --path P` / `identity list`：`P` 必须是存在的家目录下路径或 `/private/tmp/claude-<uid>` 下路径，按 realpath 存。认下的目录下次扫描带 `identity`。
- `govern record --path P --change "改了什么" --undo "怎么撤销" --bytes N [--scan-id S]`：记一处“从源头改好”的设置，`N` 是改动时的字节数（基准）。`govern check --run <run目录>`：对每条记录，在 `<run>/facts.json` 里找同路径（或最近祖先 / 后代合计）的现值，写 `<run>/governance-check.json`：`[{path, change, baseline_bytes, now_bytes, delta, verdict: stopped|growing|unknown}]`（涨幅不超过 1% 或为负算 `stopped`）。
- `pending add --text T --kind protect|rule|note [--value JSON]` / `pending list`：记一句待确认的话。`--value`：`protect` 写路径（字符串或数组，须是家目录下已存在的目录，走 `prefs._home_paths` 同样的校验）；`rule` 写 `{"key":"<规则键>","action":"delete|keep"}`；`note` 写 `{"path":"<目录>"}`（备注文字就是 `--text`）。`memory.confirm(ids)` / `memory.reject(ids)`：看板导出的 `确认规则:` / `不要规则:` 由 `prefs.apply_settings` 调用，确认时 `protect` 并入保护名单、`rule` 写进 `rules`、`note` 写进 `notes`，都写 `memory_log`；`reject` 直接移除。
- `remembered --run <run目录> [--since ISO]`：汇总本次记住的条目（`memory_log` 里 `scan_id` 与本次扫描一致，或 `at` 不早于 `--since`，且没被忘掉）写 `<run>/remembered.json`：`[{id, kind, text}]`。
- `forget --ids m1,m2` / `forget --from-text <文件>`：撤销对应改动（规则删除或恢复旧值、认下的目录删除、保护路径移出、设置恢复 `ref.old`）并标 `forgotten`。设置类（`setting`）写日志时同时记 `ref.new`（连带字段记 `ref.also_new`），撤销时当前值不等于 `ref.new` 说明之后被改过，**不回滚**并提示“之后改过，没动”；没有 `new` 的旧日志条目没法确认，按不回滚处理。`protect` 类只记本次真正新增的路径（`ref.paths`），原来就在保护名单里的忘掉时不移除，没有新增就不写日志；`--from-text` 解析成果页复制来的“忘掉: m1,m2”行。

