# 知识卡：Codex（CLI / 桌面 App）

适用：OpenAI Codex 在本机留下的状态。命令与数据库表结构会随版本变化，以现场 `codex --help` 和实际目录为准。

## 存储位置

| 路径 | 内容 | 性质 |
|---|---|---|
| `~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<线程ID>.jsonl` | 每个会话的完整轨迹；首行 `session_meta` 含 `cwd`、`source`、`id` | 工具状态 |
| `~/.codex/archived_sessions/` | 已归档会话（若存在） | 工具状态 |
| `~/.codex/session_index.jsonl` | 线程 ID → `thread_name`、`updated_at` 的追加式索引（同一 ID 可有多行，取最新） | 工具状态 |
| `~/.codex/state_*.sqlite`（含 `-wal` / `-shm`） | 线程索引、父子派生边、附件登记 | 工具状态，写入中的库绝对保护 |
| `~/.codex/thread_history_*.sqlite`、`goals_*.sqlite`、`queue_*.sqlite`、`logs_*.sqlite`、`memories_*.sqlite` | 分页历史、长期目标、待处理队列、日志、记忆 | 工具状态；`memories` 与 `goals` 视为用户独有成果 |
| `~/.codex/generated_images/<线程ID>/*.png` | 内置生图工具的原图 | 见下方"生成图"判断，可能是用户独有成果 |
| `~/.codex/attachments/` | 粘贴/上传的附件 | 工具状态 |
| `~/.codex/packages/standalone/releases/*`、`packages/app-server-daemon/releases/*` | 两套独立安装版本与更新暂存 | 可再生缓存，但有 current 链接与安装锁 |
| `~/.codex/auth.json`、`config.toml`、`hooks.json`、`rules/`、`skills/` | 认证、配置、钩子、规则、技能 | 绝对保护 |
| `~/.codex/cache/`、`models_cache.json`、`shell_snapshots/` | 缓存与状态快照 | 可再生缓存 |
| `~/.codex/.tmp/` | Codex 运行时的临时文件 | 临时文件：Codex 没在运行时可以清；运行中只报告，不碰 |

## 一张图存三份：批量出图是最大的占用来源

`codex exec` 每调用一次就留下一个完整会话。用它批量出图的脚本（每张图调一次）会让同一张图在本机存三份：

1. `generated_images/<线程ID>/*.png` 原图；
2. 会话文件里的 base64（写两次，单个会话常有 5–9MB）；
3. `thread_history_*.sqlite` 历史库里的 base64（每条约 3MB）。

本机实测：两天的批量出图留下约 2,400 个会话、合计 30GB 以上，而每个会话都小于 10MB——**按单个会话体积排序永远看不到它们，必须按来源归组。**

## 怎么判断还在用

- **先归组**：`scan.py probe codex-groups`。全部会话按"来源（originator / source）+ 工作目录"归组，同一个 Agent 临时目录（`…/scratchpad/子目录`）合成一组，子 Agent 会话归到最上层父会话的组。每组给出会话数、会话文件 / 历史库 / 原图三部分体积、活动时间范围、打开数、24 小时内改动数、子会话数和几条首句样本。`source=exec`、`originator=codex_exec` 的大组就是批量脚本。
- **再逐组核对**：`scan.py probe codex-groups --trace <组号> --emit <run目录>/items > <run目录>/codex-g<组号>.json`（输出很长，写文件再读；带 `--emit` 也是完整输出）。组分两种，`trace.kind` 标明：

  **批量出图组**（`source=exec`）：每个顶层线程一类（子会话随父线程计入 `descendants`，不单独分类，体积不重复）：
  - `active`：树内任一线程（自身或任一后代）正在打开、24 小时内改过——整棵树不动；
  - `copied`：树内每张原图（含子会话的图）都有逐字节相同的副本，且副本都**被 git 跟踪**（`copy_where=["tracked"]`）——图已入库；
  - `copied_elsewhere`：每张原图都有副本，但有副本没被跟踪（`untracked`）或在 `rejected/`、`tmp`、`scratch` 一类目录（`rejected`）——那一份可能本身就是废稿或随时会被清，按 `copy_sample` 判断；
  - `partial`：部分原图有副本；
  - `mentioned`：有原图、没有逐字节副本，但有文件或其他 Codex 会话提到这个线程号；
  - `unmentioned`：有原图，没有副本也没人提到；
  - `unverified`：副本、引用、会话三项检索有一项没查完（超时、出错，或根本没有搜索范围），**不下结论、不出条目**。`mentioned`、`unmentioned`、`partial` 都以三项检索全部完成为前提（看每条的 `checked`）。出现 `unverified` 时加大 `--trace-seconds` 或用 `--search` 补上成品目录再跑；工作目录在家目录之外（如 `/private/tmp` 下的 Agent 临时目录）时，探针找不到项目仓库，必须用 `--search` 指定。
  - `empty`：整棵树都没有原图——看 `last_event`：`task_complete` 而无图多为模型拒绝或失败，`error` / `turn_aborted` 是中断。

  **交互对话组**（其他 `source`）：只有 `active`、`conversation` 和 `unverified`（会话检索没查完）。**没出图不等于废弃**，几 GB 的工作对话也常常一张图都没有。每个会话单独出一个条目文件，逐个出卡、逐个问用户（列标题、首句、最后活动日期），不要按组批量判 `clean`。被仍在运行的会话提到（`mentioned_by_live_sessions` 非空，即有人写过"接着那个会话做"）的会话不出条目，列在 `held_by_live_sessions`，默认保留。

  `--emit` 的文件名带组标签（组 key 的哈希，`trace.group.tag`），不随组号变化：批量组为 `items/codex-<标签>-<分类>.json`，交互组为 `items/codex-<标签>-thread-<UUID>.json`，另有 `codex-<标签>-manifest.json` 记录组 key、生成时间和检索是否查完。每次 emit 先清掉同组的旧文件。计划里用 `items_file` 引用（见 contract.md 第 4 节），不要手抄几千条。条目已按"后代在前、父会话在后、原图目录最后"排好，`codex delete` 带完整 UUID。
  **删父会话保不住任何子会话**：`codex delete` 会连带删除全部后代。要从文件里筛出一部分线程另存时，每个线程的整棵后代必须一起带上、排在它前面——`plan.py check` 会拦下漏列或顺序反了的卡，`act.py` 执行时只要还有一个后代没删掉（被跳过、失败或新出现），就不删父会话。
- **分类只是事实，结论要你看样例再下**：
  - `copied`：读出图脚本确认它只在出图当场取图、之后不再回读旧会话或原图，就可以 `clean`。脚本会回读的，整组保留。
  - `copied_elsewhere`：副本在 `rejected/` 说明那张图被淘汰了，问题是"废稿还留不留"（`decide`）；副本在被忽略的成品目录，说明图只有这一份副本、没入库，也归 `decide`。
  - `mentioned`：读 `mention_sample`。只是溯源字段（记录"这张图来自哪个线程"），且仓库里有**重新编码过的**图（缩小、转 WEBP），说明仓库只剩有损版本，问"压缩版够不够用"；提及处只是失败回执（`exit_code` 非 0、`source` 为空），等同 `empty`；被脚本当输入读取的才是依赖，保留。
  - `unmentioned`：三项检索都查完了、有图却没有副本也没人提到，通常是试画、废稿，删了就找不回，归 `decide`。
  - `empty`：没有图可丢，会话本身也不会再续，可以 `clean`；`last_event` 不是 `task_complete` 且又刚过 24 小时的，先确认批量任务真的结束了。
  - 组的 `cwd_exists=false`（工作目录已不在）是风险信号：查一下后来的计划或交接文档是否还把那里的文件当"真源"，是就归 `decide`。
- **会话之间的引用**：`trace` 会在其他 Codex 会话的 rollout 里搜这些线程号（`mentioned_by_sessions`）。提到同组 10 个以上线程号的会话视为盘点或清理清单（`search_complete.inventory_sessions`），不算逐个依赖；其中仍在运行的，会在被它列过的线程上标 `listed_by_live_inventory`——读那个会话的首句，确认它在做盘点或清理（而不是正在驱动这批任务）再继续；拿不准就整组归 `decide`。
- **逐会话细看**：`scan.py probe codex`，列出 100MB 以上的单个会话：首行 `session_meta`、首条用户消息前 120 字、索引时间、是否被打开，以及 `generated_images/<id>` 的张数、字节、有无 rollout、引用命中。适合交互会话，不适合批量脚本。
- **rollout 文件是否被打开**：`lsof` 命中 rollout 路径，即该会话正在运行；24 小时内修改过同样视为进行中。
- **`~/.codex/.tmp/`**：Codex 运行时的临时文件，先看有没有 Codex 进程在跑（CLI 或桌面 App）；没在跑可以整目录清，运行中只报告、不碰，免得打断正在写的东西。
- **`session_index.jsonl` 的 `updated_at`**：取同一 ID 最后一行；与 rollout 修改时间取较新者作为活动时间。索引里有条目但 rollout 已不存在，是已知的索引失步现象（见 https://github.com/openai/codex/issues/31074 ），不代表可以清别的东西。
- **子 Agent 与父线程**：`session_meta.source` 为 `{"subagent":{"thread_spawn":{"parent_thread_id":...}}}` 的是派生会话。父线程仍在用，子会话一并保留；原生 `delete` 会连带删除派生后代，所以删父等于删整棵树。`source` 为 `exec` 表示由 `codex exec` 非交互启动，`cli` / `vscode` 等为交互入口。
- **`generated_images/<线程ID>`**：
  1. 有对应 rollout：看会话是否结束，并对图片文件名与目录名做引用检查（受跟踪文件、交接记录、清单里是否出现 `generated_images/<id>`）。
  2. **无 rollout 的图目录**：多半来自 `codex exec` 批量出图脚本。必须去看调用脚本是否另存了副本与回执（如复制到输出目录、写 manifest / 验收清单），逐目录对账：有回执证明已另存的才可清，没有回执的不动。
  3. 交接记录写明"保留"的、被验收清单引用的，即使会话已结束也保留。
- **多线程共享库**：`state_*.sqlite` 等是全局共享的，体积不分摊到单个会话，更不能按会话删。
- **状态库里的非 complete 目标、待处理队列**意味着任务未完成：该线程及其关联会话树保留。

只读命令速查：

```sh
du -sh ~/.codex/sessions ~/.codex/generated_images ~/.codex/*.sqlite | sort -h
tail -n 3 ~/.codex/session_index.jsonl                  # 索引是追加式，同 ID 取最后一行
head -n 1 <rollout 文件> | python3 -c 'import sys,json;print(json.loads(sys.stdin.readline())["payload"].get("source"))'
git grep -F "generated_images/<线程ID>" -- .            # 在仓库里查引用
```

- 活动时间的口径：rollout 修改时间与索引 `updated_at` 取较新者；`updated_at` 为 Unix 秒时单位异常或明显未来值时保持"调查"，不据此下结论。
- 近期活动的关联会话（父或兄弟）会把整棵树的旧成员一并保护；子会话未出现在你看到的列表里，不等于不存在。

## 原生清理与保留设置

- Codex **没有按天数自动清理会话的设置项**（据本 Skill 调研；新版本可能增加，用 `codex --help` 与配置参考复核）。
- 逐会话原生命令（本机 0.162.1 `--help` 核实）：
  - `codex archive <会话ID或名称>`：归档，可 `codex unarchive` 恢复；
  - `codex delete --force <线程UUID>`：**永久删除**，并连带派生后代；非交互调用必须带 `--force`，否则报"需要交互终端"。删除会话文件、状态库记录和历史库条目，**不删 `generated_images/<id>`**（要另列条目）。实测约 0.5 秒/个，几千个会话要在后台跑。计划里一律写完整 UUID，不写名称或前缀（前缀会撞上别的线程）；`plan.py check` 会核对 UUID 与 path 的文件名对应；
  - `codex migrate-rollouts`：只报告可迁移的旧会话，加 `--apply` 才迁移到分页历史。
- `sqlite_home` 只改变 SQLite 状态库的位置，不隔离 rollout、认证与配置。配置参考：https://developers.openai.com/codex/config-reference ；App Server 的线程语义：https://developers.openai.com/codex/app-server
- **删完要交还历史库空间**：`codex delete` 只删历史库里的行，`thread_history_*.sqlite` 文件不会变小。本机 0.162.1 的库开着 `auto_vacuum=incremental`，`act.py` 在执行过 `codex delete` 后会自动分批 `PRAGMA incremental_vacuum`（每批约 20MB，批间暂停）。库是 WAL 模式时，搬动的页先进 `-wal`，每批之后做一次 checkpoint，按“主文件 + `-wal`”合计体积判断进展；Codex 开着的读事务会挡住 checkpoint，合计体积连续 20 批不降就停下并说明，等 Codex 空闲时再 `act.py reclaim`。结束跑 `quick_check`。中途被打断用 `act.py reclaim` 补做。没开增量回收的库只报告，不做整库 `VACUUM`（需要 Codex 完全退出）。实测一次回收 7.18GB（Codex 桌面版开着）。
- **源头治理**：批量脚本给 `codex exec` 加 `--ephemeral`（不把会话写入磁盘），写进 `governance` 问用户。推广前先单张实测：图片是否仍落在 `generated_images/<tid>/`、`--json` 输出里是否仍有 `thread.started` 线程号，脚本靠它取图。
- 程序版本与暂存由 Codex 自己的安装脚本管理，不要手删 `packages/*/releases`、`current`、`install.lock`。

## 不能做的事

- 不删 `auth.json`、`config.toml`、`memories_*.sqlite`、`goals_*.sqlite`、`queue_*.sqlite`、任何带 `-wal` / `-shm` 的库（正在写入或存在未合并事务）。
- 不凭"没有 rollout"判定图目录无主：这恰是批量出图的常态（见上）。本机实战：一次 4.98GB 的"无会话图目录"估算，逐个按回执对账后只证明了 2.57GB 可删。
- 不凭"全仓零引用"以外的单项信号删图：旧估算曾说"无引用"，实际交接记录写明保留，且记录声称的副本目录已被删。
- 不在真实 `~/.codex` 上试新命令：在 APFS 克隆（`/bin/cp -c -R`，排除 `auth.json`）里试，并先把克隆里状态库 `threads.rollout_path` 的绝对路径改成克隆路径，否则会删一半（文件留着、库里记录没了）。
- 不把 `thread_history_*.sqlite` 当可删缓存：实测移走它后原生接口仍返回成功，但历史 turns 为空，原始 rollout 还在也一样。
- 不手工删 rollout 而保留数据库登记：索引与边会指向不存在的文件。
- 不在会话运行中删它的 `generated_images/<id>` 或附件。

## 给用户的一句话问题

- 「这几个已结束的 Codex 会话（列标题与最后活动日期）以后还会接着用吗？」
- 「这批没有会话的出图目录是由哪个脚本生成的？成品图已经另存在别处了吗？」
- 「这 N 个出图会话里的图没有另存，多半是试画和废稿，还想留着翻看吗？」
- 「批量出图脚本要不要加 `--ephemeral`？以后不再留会话，同一张图只存一份。」
- 「这个会话的生成图是正式交付素材吗？是的话请先指出成品位置。」
