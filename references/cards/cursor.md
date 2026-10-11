# 知识卡：Cursor（VS Code 系 AI 编辑器）

适用：macOS 上的 Cursor 桌面端。本卡所有路径与行为来自 Cursor 官方论坛的员工回复和社区实测，Cursor 官方文档对本地保留策略几乎没有说明；本机没有 Cursor 实例，下面凡标"未核实"的均未现场验证，使用前必须 `ls` 现场确认。

## 存储位置

| 路径 | 内容 | 性质 |
|---|---|---|
| `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` | 全局 SQLite：聊天/Composer/Agent 的索引与内容（表 `cursorDiskKV`，键如 `composerData:`、`bubbleId:`、`checkpointId:`、`agentKv:`） | 工具状态，**最危险对象**，可达十几 GB |
| `.../globalStorage/state.vscdb.backup` | 该库的旧备份副本 | 工具状态（据员工回复已不再更新，见下） |
| `~/Library/Application Support/Cursor/User/workspaceStorage/<哈希>/state.vscdb` | 每个工作区一个库：会话列表、该工作区的状态 | 工具状态 |
| `~/Library/Application Support/Cursor/User/History/` | 本地文件编辑历史（VS Code 的 Local History） | 工具状态，体积通常中等 |
| `~/Library/Application Support/Cursor/User/globalStorage/anysphere.cursor-commits/checkpoints/` | Agent 检查点（来自第三方文章，未核实） | 工具状态 |
| `~/Library/Application Support/Cursor/{Cache,CachedData,Code Cache,GPUCache,logs}` | Electron 通用缓存与日志（沿用 VS Code 布局，未核实具体名称） | 可再生缓存 |
| `~/Library/Application Support/Cursor/User/settings.json`、`keybindings.json`、`snippets/` | 用户设置 | 用户独有成果，保护 |
| `~/.cursor/`（`extensions/`、`mcp.json`、`rules` 等，未核实） | 扩展、MCP 配置、规则 | 配置保护；扩展可重装 |
| `~/Library/Caches/com.todesktop.*`（Cursor 更新器缓存，未核实） | 更新包 | 可再生缓存 |

## 怎么判断还在用

- **进程**：Cursor 是否在运行（`pgrep -fl Cursor`）。只要在运行，所有 `state.vscdb` 都按"正在写入"处理，不动。`lsof` 看库文件是否被打开。
- **体积来源**：`state.vscdb` 大小要和 `state.vscdb.backup` 分开看；`workspaceStorage/<哈希>` 的哈希对应工作区路径，打开其中 `workspace.json`（含目录 URI，未核实文件名）可知它属于哪个工程。工程目录已不存在的工作区库是候选，但要走官方入口清理，见下。
- **是否有可恢复路径**：全局库只保存索引与引用，真正的聊天数据在各工作区库；删全局库会让聊天卡在 "Loading Chat"（官方论坛员工确认的已知限制，下详）。
- **不提供按天数的信号**：没有可靠的"多久没用"判断，不能用文件年龄代替。
- **备份文件**：论坛员工称 `state.vscdb.backup` Cursor 已不再更新，是遗留副本，退出 Cursor 后删它不影响聊天（据 deja-vu 引述员工，未现场验证）。

只读命令速查（先确认 Cursor 已退出）：

```sh
pgrep -fl Cursor || echo "Cursor 未运行"
D=~/Library/Application\ Support/Cursor/User
du -sh "$D"/globalStorage/state.vscdb* "$D"/workspaceStorage "$D"/History 2>/dev/null
ls -lt "$D"/workspaceStorage | head          # 最近有写入的工作区库
```

- 读库要先拷副本再用只读模式打开；`cursorDiskKV` 表里 `composerData:` 是一条聊天，`bubbleId:<聊天ID>:*` 是其中的消息，按键前缀统计行数与字节即可判断哪类数据撑大了库，不需要读内容。
- 体积特征：`agentKv:` 与 `checkpointId:` 前缀的行占大头，通常说明是 Agent 模式的检查点与工具输出，而不是纯对话。
- 如果用户只想腾空间且不想动聊天：优先 `state.vscdb.backup`（遗留副本）、Electron 缓存目录、旧扩展，再谈聊天本身。
- 兼容说明：Cursor 基于 VS Code，同路径布局的其他 VS Code 系编辑器（VS Code、Windsurf 等）规律相同，但各自库内表结构不同，不能把 Cursor 的键名套过去。

## 原生清理与保留设置

- 官方没有保留天数设置（未核实，未见官方文档）。
- 命令面板（Cursor 运行时）：`Delete old chats…` 批量删旧对话；开发者命令 `GC Agent KV Blobs` 清理没有任何对话引用的 blob（均来自第三方指南 https://vshulcz.github.io/deja-vu/guide/cursor-state-vscdb-too-large.html ，未在官方核实）。
- 删后文件不一定变小：旧版本库里释放的空间留在文件里，需要在**完全退出 Cursor** 后对库做 `VACUUM`：`sqlite3 <库路径> "VACUUM;"`（官方员工在论坛的建议）。VACUUM 需要约等于库体积（有时接近两倍）的空闲空间，磁盘紧张时先别做。
- 导出想留的对话后再删，是第三方指南的建议；Cursor 本身的导出能力以现场为准。

## 不能做的事

- **不删全局 `state.vscdb`**：论坛员工确认，删除后已有项目的聊天历史无限 "Loading Chat"，Cursor 无法从工作区库重建全局索引；恢复的唯一办法是放回原文件。来源：https://forum.cursor.com/t/deleting-global-state-vscdb-causes-infinite-loading-chat-in-projects-history-not-recoverable-without-corrupted-backup/153220
- 不在 Cursor 运行时对这些库做 `sqlite3` 写入或手动删行：社区经验是窗口状态连聊天一起丢。
- 不让 Agent 凭"清理缓存"的笼统指令动 Cursor 目录：有用户报告 Cursor 自己在一次"简单缓存清理"请求中清掉了约 300GB 的系统数据（论坛帖 https://forum.cursor.com/t/cursor-wiped-300gb-from-my-system-after-a-simple-cache-cleanup-request-critical-issue/159065 ，经过的细节以原帖为准）。本 Skill 的路径边界校验和封存机制就是为此存在。
- 不手工复制 `workspaceStorage` 目录来"迁移"历史：哈希随安装变化、SQLite 日志可能损坏、不同版本表结构不同。
- 不删 `User/settings.json`、`keybindings.json`、`snippets/`。

- 任何写入操作之前，先把整个 `globalStorage` 与对应 `workspaceStorage/<哈希>` 目录拷一份到有空间的位置作为回退点；回退点本身也占空间，要把它算进释放量的账里。

## 给用户的一句话问题

- 「Cursor 里这些旧项目的聊天记录还要吗？要的话我先让你在 Cursor 里导出，再用它自带的删除功能清理。」
- 「Cursor 可以现在完全退出吗？压缩数据库需要它关闭，并需要一块和数据库差不多大的空闲空间。」
