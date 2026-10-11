# 知识卡：Grok Build

适用：xAI Grok Build CLI（`grok`）。本卡依据本机 1.0.50 的内置文档（`~/.grok/docs/user-guide/`）与 `--help`，版本不同以现场为准。

## 存储位置

| 路径 | 内容 | 性质 |
|---|---|---|
| `~/.grok/sessions/<编码后的工作目录>/<会话ID>/` | 单个会话目录：`summary.json`、`updates.jsonl`、`chat_history.jsonl` 等 | 工具状态 |
| 会话目录内 `updates.jsonl` | 会话权威日志，`/resume` 以它为准 | 工具状态，不可当临时日志 |
| 会话目录内 `chat_history.jsonl` | 发给模型的原始消息 | 工具状态 |
| 会话目录内 `compaction_checkpoints/`、`rewind_points.jsonl`、`plan.json`、`subagents/` | 压缩检查点、回退点、任务计划、子 Agent 元数据 | 工具状态 |
| 会话目录内媒体（如 `videos/`）和终端日志 | 生成的视频/图片、终端输出 | 可能是用户独有成果，先核实去向 |
| `~/.grok/worktrees/`、`~/.grok/worktrees.db` | `grok -w` / `grok worktree create` 建的 git 工作树及登记库 | 工具状态（树里可能有未合并的工作） |
| `~/.grok/grove/` 与 Grove 重定向目录（"escape jail"） | Grove 工作树的覆盖层与被重定向的 `target`、`node_modules` 等忽略产物 | 可再生缓存（需经守护进程处理） |
| `~/.grok/memory`、`memory-v2`、`memtrace` | 记忆与追踪 | 用户独有成果，保护 |
| `~/.grok/downloads/`、`bin/`、`vendor/`、`bundled/` | 程序与更新下载 | 可再生缓存，但含当前入口 |
| `~/.grok/auth.json`、`config.toml`、`rules/`、`installed-plugins/` | 认证、配置、规则、插件 | 绝对保护 |
| `~/.grok/logs/` | 日志 | 可再生缓存 |

## 怎么判断还在用

- **先用官方统计**：`grok du`（别名 `grok disk-usage`，`--json` 可机读）按体积列出 `~/.grok` 顶层目录，再列每棵工作树的体积、类型、年龄、标签。注意 worktree 克隆共享存储，合计可能大于真实占用。
- **探针**：`scan.py probe grok`，给出 `sessions` 体积与 `config.toml` 中的 `storage.cleanup_ttl_days`。
- **会话是否在用**：会话目录内文件 24 小时内有写入，或 `lsof` 命中，或 `~/.grok/active_sessions.json` 里登记着它，一律保留。
- **分叉关系**：`summary.json` 的 `parent_session_id` 表示分叉或恢复的来源，不等于子 Agent；父会话仍在用时不要孤立地判断子会话。
- **工作树能否回收**：用原生的守卫判断而不是自己猜：`grok worktree gc --max-age 7d --dry-run` 会保留"工作成果在别处找不到"的树；Grove 树还要经守护进程检查（最近有写入、有文件打开会被拒）。再核实分支是否已并入 `origin/main`、`git status` 有无未提交改动。
- **媒体/视频**：会话内 `videos/` 等可能是唯一副本，先对输出文件名做引用检查和成品目录对账。

只读命令速查：

```sh
grok du                                  # 顶层目录与工作树体积（也可 --json）
grok sessions list                       # 仅当前目录的会话；其他工程需切目录
grok worktree list                       # 登记的工作树
grok worktree gc --max-age 7d --dry-run  # 预览，不删除
grep -n cleanup_ttl_days ~/.grok/config.toml
```

- 会话目录名是工作目录的 URL 编码；超过 255 字节时改用"短名加哈希"，原路径记在组目录内的 `.cwd` 文件里，解码时先读它。
- 计量时 `compaction_checkpoints/`、`subagents/`、`videos/` 属于会话体积的一部分，不要再次累加到总数。

## 原生清理与保留设置

- **`storage.cleanup_ttl_days`**（`~/.grok/config.toml`）：整数，会话空闲超过该天数就删除会话目录；同时清理存活会话里超期的媒体与终端日志；不设或为 0 表示关闭。来源：本机内置文档 `26-config-reference.md`。
- **`grok sessions delete <ID>`**：永久删除某个会话（含目录内附件）；`grok sessions list / search` 可查，默认只列当前工作目录的会话，其他工程的会话要换目录或搜索。
  - 退出码 0 不等于成功：曾观察到对格式不被索引识别的会话，命令输出 `No session found` 但目录仍在。删后必须复查目录是否真的消失。
- **`grok worktree gc --max-age 7d [--dry-run]`**：回收过期工作树，不加 `--max-age` 则什么都不过期；`grok worktree rm`、`grok worktree clean-artifacts <挂载点> --yes`（清 Grove 重定向产物）。
- **`grok du --clean --yes` / `--clean-orphaned --yes`**：经 Grove 守护进程清理重定向目录；守护进程未运行时命令拒绝，需先启动。
- 减小长会话体积：会话内 `/compact`。
- 文档：https://docs.x.ai/build/settings/reference

## 不能做的事

- 不删 `auth.json`、`config.toml`、`memory*`、`worktrees.db`（登记库和磁盘上的树对不上会让 gc 漏树）。
- 不手工 `rm -rf ~/.grok/worktrees/<树>`：Grove 树经守护进程管理，`worktree rm --force` 只会丢登记不删目录，手删则留下孤儿登记。用 `grok worktree rm`。
- 不单独删会话目录里的 `updates.jsonl` 或 `chat_history.jsonl`：前者是恢复的权威日志，后者是模型上下文，删一份会让 `/resume` 或列表失效。
- 不在会话进行中删它的目录；删除成功也不保证空间立即释放（有文件仍被进程打开）。
- 不凭 `gc` 的 `--force` 绕过守卫：它跳过的是活动进程与受保护路径检查。
- 不因"重定向目录不在 `grok du` 合计里"就忽略它：Grove 的 escape jail 字节单列报告。

## 给用户的一句话问题

- 「这几个已结束的 Grok 会话（列标题与日期）以后还会接着用吗？」
- 「这几棵 Grok 工作树里的改动都已经合并或不需要了吗？」
- 「要不要把 Grok 会话保留期设为 N 天（`storage.cleanup_ttl_days`），让它自己清？」
