# 知识卡：Claude Code

适用：Claude Code CLI / 桌面端在本机留下的状态。版本会变，目录结构以现场 `ls` 和 `claude --help` 为准，不要按版本号硬套。

## 存储位置

| 路径 | 内容 | 性质 |
|---|---|---|
| `~/.claude/projects/<编码后的工作目录>/*.jsonl` | 每个会话一份完整对话轨迹（含工具输出） | 工具状态（`/resume` 的数据源，到期会被自动清扫） |
| `~/.claude/projects/<目录>/<会话ID>/` | 该会话的子 Agent 轨迹（`subagents/agent-*.jsonl`）、工具结果落盘 | 工具状态 |
| `~/.claude/projects/<目录>/memory/` | 自动记忆（MEMORY.md 与明细） | 用户独有成果，绝对保护 |
| `~/.claude/file-history/<会话ID>/` | 编辑前的文件快照，用于 `/rewind` 恢复 | 工具状态（随会话到期清扫） |
| `~/.claude/tasks/`、`~/.claude/session-env/`、`~/.claude/shell-snapshots/`、`~/.claude/paste-cache/` | 任务清单、会话环境、shell 快照、粘贴缓存 | 可再生缓存 / 工具状态 |
| `~/.claude/history.jsonl` | 输入历史（上箭头） | 工具状态，体积小 |
| `~/.claude/plugins/`、`~/.claude/skills/` | 插件、技能（可能含 symlink 指向仓库里的本体） | 用户独有成果（技能本体）/ 可再生缓存（插件缓存） |
| `~/.claude/backups/`、`~/.claude/settings.json.bak*` | 配置备份 | 工具状态，体积通常很小 |
| `~/.claude.json`（家目录下，不在 `.claude/` 内） | 全局配置、各工程信任与 MCP 条目 | 工具状态，绝对保护 |
| `~/.local/share/claude/versions/<版本>` | 原生安装器下载的各版本程序 | 可再生缓存（见下方版本规则） |
| `~/.claude/debug/`、`~/.claude/telemetry/` 等 | 调试与遥测日志 | 可再生缓存，若存在 |
| `/private/tmp/claude-<uid>/` | 每个 Claude Code 会话的临时工作目录（`<uid>` 是当前用户 uid），正在运行的会话也在其中 | 工具状态（会话结束后一般会清，异常退出或长期运行会留下） |

## 会话临时目录 `/private/tmp/claude-<uid>`

- **是什么**：Claude Code 给每个会话在 `/private/tmp/claude-<uid>/` 下开一个临时工作目录，放过工具落盘、临时脚本、中间文件。**当前正在运行的会话也在里面**，所以这个目录里永远可能有"活"的数据。
- **判断**：
  - 当前会话与 **24 小时内有改动的一律保留**（和判断准则里的"进行中，保留"一致）；有进程打开（`lsof +D /private/tmp/claude-<uid>`）的同样保留。
  - 其余按"对应会话是否结束"判断：目录里的内容能对上某个已经不再续接的会话、又没有进程在读写，才是候选；对不上任何会话、来源不明时归 `decide`。
  - **不能只因为"它在 `/private/tmp`、是临时目录"就删**：位置临时不等于内容可弃，判断依据是会话是否结束、有没有人在读。
- **保护**：脚本允许删它下面的会话子目录，但不许删它本身，也不许碰 `/private/tmp` 里其他内容（其他程序也在用这个目录）。

给用户的一句话问题：「`/private/tmp/claude-<uid>` 下有 X GB 本次运行留下的临时文件，包含正在跑的会话；跑完的会话临时目录可以清吗？」

## 怎么判断还在用

- **会话是否还会被接着用**：看 `.jsonl` 的修改时间与 `~/.claude/projects/<目录>/` 里最新文件。24 小时内有写入，或被进程打开（`lsof +D ~/.claude/projects/<目录>`），一律视为进行中。
- **用探针一次看全**：`scan.py probe claude`，给出各项目体积、会话数、`cleanupPeriodDays`、`versions` 下各版本与当前运行版本。
- **一个项目的体积主要来自哪里**：对比该项目目录下 `*.jsonl`、`<会话ID>/subagents/`、工具结果落盘三类的合计；超大的单个 `.jsonl` 往往是跑过长任务或大量读文件的会话，问用户这类会话是否还要续接。
- **工作目录是否还存在**：目录名是工作目录路径编码（`/` 换成 `-`）。对应工程目录已被删除或是一次性临时目录（如 scratchpad、`/tmp`、`/private/var/folders`）的会话，几乎不会再被续接，是最安全的候选。
- **正在运行的版本**：`ps` 里 `claude` 进程的可执行路径；`~/.local/bin/claude`（符号链接或启动器）指向的版本。旧会话可能仍运行在旧版本上。
- **引用检查**：会话轨迹常被交接文档、工作日志引用（会话 ID 写进文档）。对会话 ID 做 `git grep`，命中则保留，除非用户点名推翻。

只读命令速查（均不改动任何文件）：

```sh
du -sh ~/.claude/projects/* | sort -h | tail -10     # 各工程轨迹体积
ls -lt ~/.claude/projects/<目录> | head               # 最近写入的会话
lsof +D ~/.claude/projects/<目录> 2>/dev/null         # 是否有进程在写
grep -n cleanupPeriodDays ~/.claude/settings.json      # 当前保留期
ls ~/.local/share/claude/versions                      # 已下载的各版本
```

- 工程目录被编码成路径名，`projects` 下的名字可能是已被删除的一次性目录。先对解码后的工作目录做 `test -d`，再谈是否孤立。
- 大会话的成因判断：`.jsonl` 里工具输出占大头时（读大文件、长日志），体积远超对话本身，这类会话"信息密度低"，但仍要问用户是否要保留可恢复性。

## 原生清理与保留设置

- **`cleanupPeriodDays`**（`~/.claude/settings.json`）：会话轨迹与其他应用数据的保留天数，默认 30，最小 1。Claude Code 在会话启动后以后台清扫方式静默删除过期轨迹，过期会话不再出现在 `/resume` 列表。来源：https://code.claude.com/docs/en/settings-reference
  - 若本机设为极大值（如 99999），等于永不清理，这是增长型治理项：建议问用户改成 30–90。
  - `desktopSessionCleanupPeriodDays`：在 Claude Desktop 中开始或最近续接的会话单独的保留期，两者取更长者；未设置则这类轨迹不按年龄删除。
  - 非交互 `claude -p` / `--bare` 等场景下清扫可能被跳过；设置不可判定时同样跳过（见官方 monitoring 文档的 `retention_sweep` 事件）。
- **`claude purge [path] [--dry-run] [--yes] [--interactive] [--all]`**：删除某个工程的全部状态（轨迹、任务、文件历史、配置条目）。本机 2.1.296 `--help` 核实。
  - 注意：它按工程整体删除，**包括该工程的 memory 目录与配置条目**（来自对 2.1.292 在隔离目录的实测，见本 Skill 重建记录）；没有按单个会话删除的选项。
  - 首次 `--dry-run` 可能改写 `.claude.json` 并生成备份，不是严格只读（同一实测）。
- 原生版本保留：官方说明原生安装器保留启动入口指向的版本、当前机器上会话运行的版本和最新两个版本；自定义 launcher 会保留全部版本。来源：https://code.claude.com/docs/en/setup 。不要自己按版本号或年龄删 `versions/`。
- 目录说明总览：https://code.claude.com/docs/en/claude-directory

## 不能做的事

- 不删 `~/.claude/projects/*/memory/`、`~/.claude.json`、`~/.claude/settings.json`、`~/.claude/skills/` 下的 symlink 目标。
- 不对整个 `~/.claude/projects` 做 `rm -rf`：会同时抹掉所有工程的 memory 与历史，且不可恢复。
- 不用 `claude purge --all` 当普通清理入口：它删全部工程的状态与配置条目（信任、MCP 配置也会丢）。
- 不手工删正在使用会话的 `.jsonl` 或 `file-history/<会话ID>`：`/resume`、`/rewind` 会失效。
- 磁盘写满时 `.claude.json` 可能被写坏（见官方 issue 讨论 https://github.com/anthropics/claude-code/issues/41737 ），所以清理本身要先预留空间，且不要在磁盘接近 100% 时反复启动 Claude 会话。
- 不能因为"可重新下载"就删 `versions/` 里当前入口指向的版本或仍在运行的版本。
- 不按版本号删 `~/.local/share/claude/versions/` 下的原生版本：官方安装器只保留最新两个（外加入口指向和会话在用的版本），删了它会自己补回来，白删还可能打断正在跑旧版本的会话。

## 给用户的一句话问题

- 「这几个工程（列出临时目录/已删除目录的）的 Claude 会话以后还会接着用吗？」
- 「Claude 会话保留期现在是 N 天，改成 30 天可以吗？（超过的会话以后不再出现在恢复列表）」
- 「这个超大会话（X GB）还要保留完整轨迹吗？」
