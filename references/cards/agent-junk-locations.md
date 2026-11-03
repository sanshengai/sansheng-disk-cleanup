# 知识卡：Agent 过程资料的固定扫描位置

适用：本机同时跑多家 AI Agent（Codex、Gemini/Antigravity、Grok、Claude Code 等）和自建流水线时，过程资料会落在一批**固定但不在项目目录里**的位置。每轮扫描都要把下表逐项量一遍，即使上轮已经清过；只量、只读，判断仍走 judgment.md 四个问题。路径里的 `<date>`、`<N>`、`<uid>` 按本机实际展开，私有路径写进 profile.json，不写进本卡。

## 固定扫描位置

| 位置 | 是什么 | 默认判断 | 问谁 |
|---|---|---|---|
| `~/.codex/sessions/YYYY/MM/DD/` | Codex 会话轨迹 | 按 codex 卡归组；交互会话逐个问 | 用户 |
| `~/.codex/generated_images/<线程ID>/` | Codex 生图原件 | 先查最终图库是否有逐字副本，再按批量试验母版规则 | 用户 |
| `~/.codex/thread_history_*.sqlite` | Codex 历史库（可达十几 GB） | 只在 `codex delete` 后由原生回收缩小，不手动删 | — |
| `~/Documents/Codex/<date>/task-<N>`、`new-chat*` | Codex 桌面 App 的任务工作目录 | 任务产物是否已搬进项目或成品目录；未搬走的问 | 用户 |
| `~/.gemini/antigravity-cli/conversations`、`brain` | Antigravity 会话与工作记忆 | 按 gemini-antigravity 卡；brain 内含任务产物 | 用户 |
| `~/.grok/sessions/<编码工作目录>/` | Grok Build 会话 | 按 grok 卡；工作目录已删除的会话优先列出 | 用户 |
| `$TMPDIR/sansheng-sync-wt/<pid>-<hash>` | 同步/合回主线用的临时工作树 | **pid 仍在运行或部署锁持有时绝对保留**；pid 已退出且 `git worktree list` 不再登记才可列 | 当前持有锁的 Agent |
| `$TMPDIR/node-compile-cache/` | Node 编译缓存 | 第 1 档；走 `node_compile_cache.py` 专用入口 | — |
| `$TMPDIR/pytest-of-*` | pytest 临时根 | 第 1 档（统一规则） | — |
| `$TMPDIR/hyperframes-extract-cache-<uid>`、`c7-input-<uid>` | 视频渲染抽帧/输入缓存 | 第 1 档；静默期：hyperframes 24 小时（本卡阈值，按 SKILL.md「第 1 档统一规则」取较严者），c7-input 默认 6 小时 | 视频生产 Agent |
| `/private/var/folders/<xx>/<id>/X/com.google.Chrome.code_sign_clone` | Chrome 更新后的代码签名克隆（与 `$TMPDIR` 同级的 X 目录） | 第 1 档，**仅 Chrome 未运行时**；走 `chrome_clones.py` | — |
| `~/Library/Caches/Google`、`~/Library/Caches/baoyu-skills` | Google 应用缓存、baoyu-skills 下载/渲染缓存 | 第 1 档（Library/Caches 24 小时；Google 需 Chrome 未运行） | — |
| `~/.sansheng/disk-cleanup` | 本 Skill 自己的设置、运行目录、隔离区 quarantine、开发证据、候选 zip | 第 3 档需判断；`profile.json`、`history/` 必留 | 用户 |
| `$TMPDIR/chuxin-*-full-repo-*`、`sansheng-merge-self-*`、大量 `*-tests-*` 小目录 | 整仓复制件与测试现场 | 无进程、未登记为工作树时可列；数量多时按前缀聚合汇报 | 创建它的 Agent |
| `~/.sansheng/build-tmp/sansheng-heavytask-*` | 重任务暂存槽 | 空目录或只剩回执的可列；`sansheng-site-releases`、部署槽位保留 | 部署 Agent |
| `git worktree list --porcelain` 中 `locked initializing` 且路径已不存在 | 中断初始化的登记残留 | 不占空间，但会误导工作树判断；只建议 `git worktree unlock` + `prune` 命令，由用户或持有方执行 | 用户 |
| `~/.cache/huggingface/hub/models--*` | 模型权重 | 逐个模型查脚本/虚拟环境引用；同族多尺寸（如 whisper base/small/medium）只留被引用的 | 视频/语音 Agent |
| `~/.cache/codex-runtimes/codex-runtime-install-*` | Codex 运行时安装中间目录 | 有 `codex-primary-runtime` 且无进程时为安装残留 | — |

## 工作树里的 ignored / untracked 目录

工作树不是"整个都在用"。对每个工作树：

1. `git ls-files -z -- <dir>` 与 `git ls-files -z -o -- <dir>` 分别求和，区分**被跟踪**与**未跟踪+忽略**字节；被跟踪部分不进删除卡。
2. 对未跟踪/忽略的大目录，找主仓同名路径（`<主仓>/<同一相对路径>`），记录 `工作树大小 / 主仓大小` 比值、双方最新真实写入时间。比值 ≫ 1 且工作树侧 7 天无真实写入，说明是只留在工作树的过程现场，优先下钻。
3. 在过程现场里按文件大小分组再完整哈希，单列"同目录树内逐字重复"（例如横版/竖版各复制一份同样的素材）；这类重复往往占一半以上。
4. 成品已在成品目录（如 `成品/`）或主仓时，过程现场按 B 类处理：保留回执、清单、脚本，删除可再生的渲染中间件。

## 完成证据补充

- `<项目>/web/.local/<task>-YYYYMMDD/` 下存在 `authorized-*`、`DELIVERY*`、`RECEIPT*` 标记文件，且标记之后没有真实写入，可作为 `delivered` 线索（仍需引用检查；被其他 .local 任务的 JSON 以字符串提到，通常只是来源记录，写 `refs_reviewed(history_only)`）。
- 带版本号的迭代目录（如 `v7执行`、`v10执行`、`v11重建`）：只保留最新 vN、现役工具脚本和最终样图备份；旧 vN 的出图批次（roll*、stage*-prep）列 B 类，先确认私有备份覆盖。
- 读书蒸馏类 `_inbox/<batch>/`：书已上架（站点数据或正式书目录能找到同名 slug）时，`work/`、`raw/` 可删，保留 manifest/回执。

## 不能做的事

- 不因"在 `$TMPDIR`"就删：临时区可能被用作正式生产根。
- 不对正在运行的同步工作树、部署槽位、serve 进程所在目录出删除卡。
- 不自己执行 `git worktree prune`、`git gc`，只给命令。

## 2026-10 补充：直查与分档

- 本表的 `$TMPDIR` 缓存、npm/uv/pip、codex-runtime-install-*、Library/Caches 已由 `scripts/triage.py` 直查并按“无进程 + 静默期无写入”给第 1 档命令；工作树由它核对 locked/未提交/进程/写入/远端后给第 2 档 `git worktree remove` 命令卡（仍由用户批准执行）。
- 整仓 worktree 是最大的反复增长源（每个 ~20G）；同盘清理存档会抵消回收。见 why-manual-beat-skill.md。

## 2026-11 补充

- 静默期、命令写法、App 运行中处理一律见 SKILL.md「第 1 档统一规则」，本卡只登记本卡的更严阈值（hyperframes 24 小时）。
- 2026-11 深度清理新增的可清位置：`$TMPDIR/node-compile-cache`、`$TMPDIR/pytest-of-*`、Chrome `code_sign_clone`、`~/Library/Caches/Google`、`~/Library/Caches/baoyu-skills` 已进 triage 第 1 档；`~/.sansheng/disk-cleanup` 进第 3 档。
- 成品目录（`triage-local.json` 的 `video_root`）整体硬保护，但 triage 会按第二层拆成“需用户判断的成品清单”（试做/旧版、已交付、作废、当日产出），不出删除命令。
