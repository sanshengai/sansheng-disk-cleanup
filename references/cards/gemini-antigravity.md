# 知识卡：Gemini CLI / Antigravity CLI

适用：Google 的两个相关命令行 Agent。Gemini CLI 已在向 Antigravity CLI（命令 `agy`）过渡（见 https://github.com/google-gemini/gemini-cli/discussions/27274 ），本机可能两者的残留并存。版本差异大，一律先 `ls` 现场结构。

## 存储位置

| 路径 | 内容 | 性质 |
|---|---|---|
| `~/.gemini/antigravity-cli/conversations/<UUID>.db`（含 `-wal` / `-shm`） | 每个会话一个 SQLite 数据库（旧版为 `<UUID>.pb`） | 工具状态，写入中绝对保护 |
| `~/.gemini/antigravity-cli/conversation_summaries.db` | 全部会话的共享摘要索引 | 工具状态，共享，不分摊给单个会话 |
| `~/.gemini/antigravity-cli/brain/<UUID>/` | 该会话的产物：`implementation_plan.md`、`task.md`、`walkthrough.md`、媒体附件、`.system_generated/logs/transcript*.jsonl` | 可能是用户独有成果 |
| `~/.gemini/antigravity-cli/scratch/<UUID>/` | 该会话的暂存 | 工具状态 |
| `~/.gemini/antigravity-cli/annotations/<UUID>.pbtxt`、`implicit/<UUID>.pb`、`presence/<UUID>.lock` | 批注、隐式上下文、存在锁 | 工具状态 |
| `~/.gemini/antigravity-cli/knowledge/`、`skills/`、`settings.json` | 知识库、技能、配置 | 用户独有成果 / 绝对保护 |
| `~/.gemini/antigravity-cli/bin/`、`updater/`、`cache/`、`log/`、`cli.log` | 程序、更新器、缓存、日志 | 可再生缓存 |
| `~/.gemini/config/`（`config.json`、`mcp_config.json`、`skills.json`、`projects/`） | 共享配置、MCP、技能登记 | 绝对保护 |
| `~/.gemini/GEMINI.md`、`~/.gemini/settings.json`、`~/.gemini/tmp/` | Gemini CLI 的记忆、设置、会话临时区 | 记忆/设置保护；`tmp` 未核实（本机无 Gemini CLI 实例） |

## 怎么判断还在用

- **本机最大头通常是 `conversations` 加 `brain`**（实测量级：各数 GB）。单个会话体积以"数据库 + brain 目录 + scratch + annotations"合计看，共享索引库单独计。
- **会话关系**：摘要索引里有父子、组关系和 `not_fully_idle` 标志；`not_fully_idle=1`、`CASCADE_RUN_STATUS_IDLE` 都不是结束证明，反过来没有锁文件也不是。关联组里只要一个成员近期活跃或有打开文件，整组保留。
- **是否被打开**：`lsof` 命中 `conversations/<UUID>.db*` 或 `presence/<UUID>.lock`。
- **brain 里的产物是不是成果**：`implementation_plan.md`、`walkthrough.md` 常是该次任务的唯一书面总结；对其中提到的文件路径、仓库做引用检查，任务对应的工程已上线/合并才可降级。
- **工作区范围**：Antigravity CLI 把会话历史限定在启动时的工作目录（`agy -c` 按当前目录续接）。来源：https://antigravity.google/docs/cli/conversations 。所以"这个目录已经不存在"是强信号。
- **Gemini CLI 旧版本**：`gemini --list-sessions` 列出当前项目的会话（官方文档）。

只读命令速查：

```sh
du -sh ~/.gemini/antigravity-cli/* | sort -h | tail
ls ~/.gemini/antigravity-cli/conversations | head
ls ~/.gemini/antigravity-cli/brain/<UUID>             # 该会话的产物
ls ~/.gemini/antigravity-cli/presence                 # 存在锁（只看，不据此断定活跃）
python3 -c "import sqlite3,sys;c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True);print(c.execute('select name from sqlite_master').fetchall())" <拷贝出来的副本>
```

- 读 SQLite 一律先拷出含 `-wal` 的私有副本再只读查询，不对原库开连接；只查 ID、时间、保留状态、父子关系，不读标题与正文。
- 两个产品的目录名不同：Antigravity CLI 在 `~/.gemini/antigravity-cli/`，早期 Antigravity 桌面端在 `~/.gemini/antigravity/`（论坛帖提到，未核实当前版本是否仍使用）。别把旧目录的删除建议套到新目录。
- 年龄口径：取文件最新修改时间与索引中最近事件时间的较新者；空值与 `0001-01-01` 这类零日期不参与计算。

## 原生清理与保留设置

- **Gemini CLI**：`settings.json` 的 `general.sessionRetention`：`enabled`（默认 true）、`maxAge`（默认 `"30d"`）、`maxCount`（默认不限）、`minRetention`（默认 `"1d"`）；删除会话会连带删除计划、任务、工具输出、活动日志。命令行 `gemini --list-sessions`、`gemini --delete-session <序号或ID>`，会话浏览器 `/resume` 中按 `x`。来源：https://geminicli.com/docs/cli/session-management
- **Antigravity CLI**：设置文件为 `~/.gemini/antigravity-cli/settings.json`，用 `/config` 或 `/settings` 编辑（来源：https://antigravity.google/docs/cli/using ）。**是否有会话保留天数设置：未核实**（本机 settings 中没有该类键，官方文档未检到）；官方文档也未提供按会话删除的命令，社区做法是手删 `brain/<ID>`、`annotations/<ID>.pbtxt`、`conversations/<ID>.*`（论坛帖，非官方），而共享摘要库里仍会留索引行，需要自行评估一致性风险。
- 迁移旧 Gemini CLI 数据：`agy plugin import gemini`（讨论帖中的官方说明，未本机实测）。

## 不能做的事

- 不手删 `conversation_summaries.db` 或任何会话库的 `-wal` / `-shm`：索引与会话库失步，程序可能无法列出或加载。
- 不只删 `conversations/<UUID>.db` 而留下索引：列表仍显示，打开时报错。要删就成组处理，并且只处理已确认结束且无锁的会话。
- 不把 `brain/<UUID>/` 当缓存：里面的计划与总结可能是唯一副本。
- 不删 `~/.gemini/config/`、`skills.json`、`mcp_config.json`：多个 Agent 共用这套入口，删了影响不止 Antigravity。
- 不依据"UUID 文件夹很久没动"直接删：文件时间不等于最后打开时间。
- 论坛里"整个 `rm -rf ~/.gemini`"类建议会连配置、技能和共享入口一起抹掉，不采用。

## 给用户的一句话问题

- 「这几个旧的 Antigravity 会话（列标题与日期、所属目录）里的计划和总结还需要吗？」
- 「这些会话对应的工程目录已经不在了，可以连同产物一起清吗？」
- 「Gemini CLI 的会话保留期现在是 30 天，要改吗？」
