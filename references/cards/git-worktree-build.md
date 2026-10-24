# git worktree 与构建产物

适用于 AI 重度用户常见的"每个任务开一棵 worktree"工作方式，以及各语言的构建产物目录。

## 存储位置

| 路径 | 内容 | 性质 |
|---|---|---|
| `<仓库>/../worktrees/*`、`<仓库>/.worktrees/*`、`~/.<工具>/worktrees/*` | `git worktree add` 创建的工作树（位置由创建者决定，以 `git worktree list` 为准） | 工具状态；含未提交改动时为用户独有成果 |
| `<树>/node_modules` | JS 依赖 | 可再生缓存（`npm ci`/`pnpm install` 重建，需联网和锁文件） |
| `<树>/dist`、`build`、`out`、`.next`、`.nuxt`、`.turbo`、`.parcel-cache` | 前端构建产物 | 可再生缓存（需构建命令仍可运行） |
| `<树>/target`（Rust/Maven） | 编译产物 | 可再生缓存 |
| `<树>/.venv`、`venv`、`__pycache__` | Python 虚拟环境与字节码 | 可再生缓存（需 requirements 或锁文件） |
| `<树>/Pods`、`.gradle`、`build`（Android/iOS） | 原生依赖与编译缓存 | 可再生缓存 |
| `<树>/.local`、`out/`、`runs/`、`tmp/` 等自建目录 | 被忽略但可能存放中间稿、转写缓存、模型输出 | 不能默认当缓存，见 pipelines 卡 |
| 被忽略的 `.env`、`*.sqlite`、本地数据文件 | 密钥与本地状态 | 用户独有成果 |

### 从 Agent 宿主追到真正的落盘位置

用户怀疑 ORCA、终端或 IDE 先生成一份再复制到项目时，应用安装目录和同名数据目录的体积不能排除外部副本。先查当前版本的活跃配置或数据库，再追实际创建文件的代码/日志；历史规则里的路径仅作线索。按以下链路逐段落到真实路径：

1. 工作区根与嵌套规则 → 准备区 → 任务工作树 → 主仓/交付目录。结合 `git worktree list`、历史创建记录与实物检查，识别已移除的登记、未登记的残留和外部根；准备后 `move` 与 `copy` 的结果不同。
2. 宿主管理的 Agent home → Agent 原生 home → 生成图/会话附件 → 项目内副本。目录可能叫 `generated_images`、`sessions` 等，不带宿主名称；查到原生输出不能直接归因为宿主复制整仓。
3. 多账号 home、浮动工作区、附件导入和远程上传/下载暂存。按当前配置与实现定位，不把固定路径清单当作完整覆盖。

逐段区分软链接、同设备同 inode 的硬链接、普通副本和 APFS 共享块。已做过共享块处理的文件要与旧回执的新身份比对，不能再次把全部名义体积计作独占可回收量；清理自己引起的 ctime 变化也不能未经追证就说成用户正在编辑。未知共享程度仍以执行前后实测为准。

报告列出已核查的位置、产生副本的条件、当前是否有实物、未覆盖的边界。配置漂移先报事实，不为本轮调查擅自修改宿主设置或现役工作区。

### 标记文件与产物目录

只有产物目录的同级（父目录）存在对应标记文件，才算该项目的可再生产物；孤零零的 `target`、`build`、`dist` 不能凭名字当缓存。思路参考 kondo（MIT），表为自写。

| 标记文件 | 产物目录 |
|---|---|
| `Cargo.toml` | `target` |
| `package.json` | `node_modules`、`.next`、`dist`、`.turbo`、`.parcel-cache` |
| `pom.xml` | `target` |
| `build.gradle`、`build.gradle.kts` | `build`、`.gradle` |
| `CMakeLists.txt` | `build` |
| `pyproject.toml`、`setup.py` | `.venv`、`__pycache__`、`.pytest_cache`、`.mypy_cache`、`build`、`dist` |
| `Package.swift` | `.build` |
| `*.xcodeproj` | `build` |
| `mix.exs` | `_build`、`deps` |
| `pubspec.yaml` | `.dart_tool`、`build` |
| `composer.json` | `vendor` |
| `go.mod` | 无项目内产物目录；go 缓存在 `~/Library/Caches/go-build` |
| `stack.yaml` | `.stack-work` |
| `*.csproj` | `bin`、`obj` |

scan.py 对表内目录名输出事实字段 `project_marker`（有标记为 `{marker, ecosystem}`，无则 null），不改变任何判定。

## 怎么判断还在用

worktree 整棵可清理，三个条件缺一不可（判据借鉴 storage-scout）：

- 分支已并入远端默认分支：`git fetch origin` 后 `git merge-base --is-ancestor <分支> origin/main`，或 `git branch -r --merged origin/main`。默认分支名用 `git symbolic-ref refs/remotes/origin/HEAD` 取，不要写死 main。
- 没有锁：`git worktree list --porcelain` 里不含 `locked` 行（锁常表示有 Agent 或工具正在使用，或位于可移动磁盘）。
- 没有未提交改动：该树内 `git status --porcelain` 为空（忽略文件不算在内，但见下）。同时看没有未推送提交：`git log origin/main..HEAD` 为空。

构建产物只删 git 忽略的文件：

- 列出：`git ls-files -o -i --exclude-standard --directory`（被忽略的清单），只能删这个清单里的项。
- 被 git 跟踪的文件任何时候都不删。`git ls-files <路径>` 有输出，说明该目录含跟踪文件，改用只清忽略部分。
- 忽略不等于可再生：`.env`、本地数据库、训练产出也常被忽略。逐项核对，不确定的放进 keep 列表。
- 进行中信号：`lsof +D <树>`、24 小时内修改过的文件、`pgrep` 里有 `npm|pnpm|vite|next|cargo|gradle`。
- 引用检查：见 pipelines 卡；被跟踪脚本读取的"忽略目录"必须保留。

长期战役树：连续数周、会攒大量中间提交的长期 worktree，不删整棵。只清已交付部分的中间稿：先确认成品已进入主线并有确认记录，再只删忽略的中间产物；树本身、跟踪文件、续跑依赖保留。

只读命令速查：

```
git worktree list --porcelain
git -C <树> status --porcelain
git -C <树> log origin/main..HEAD --oneline
git -C <树> ls-files -o -i --exclude-standard --directory
scan.py probe worktrees --repo <主仓路径>
```

每棵树的结论按四个字段写进证据：分支、是否已并入默认分支、是否 locked、dirty 行数；缺一项就不写"可整棵移除"。

## 原生清理与保留设置

- 删除树：`git worktree remove <路径>`（有未提交改动或锁时会拒绝，不要加 `--force` 绕过）。
- 清理失联登记：`git worktree prune`。
- 预览清理忽略文件：`git clean -ndX`（`-X` 只含被忽略的，`-n` 为预演）。真实执行只对已列入计划的路径做，不对整仓跑 `git clean -fdx`。
- 各语言原生命令：`cargo clean`、`npm ci`（重建）、`pnpm store prune`、`uv cache prune`、`brew cleanup`。
- 工具：kondo（https://github.com/tbillington/kondo）、npkill（https://github.com/voidcosmos/npkill）可列出构建产物目录，借鉴其识别规则，不直接采用其"全删"结果。
- 官方：https://git-scm.com/docs/git-worktree

## 不能做的事

- 不要 `git clean -fdx` 或 `git worktree remove --force` 当清理手段：会连同未提交改动、`.env`、本地数据一起删。
- 不要因为"很久没动"就删整棵 worktree：分支可能只在这棵树里有提交。
- 不要把别的会话正在写的树当垃圾。存在 `locked` 或 24 小时内有改动就跳过。
- 不要删主仓的 `.git`，也不要删 worktree 的 `.git` 文件（指针文件），会让仓库失去登记。
- 公开案例：Agent 拼错路径把家目录删掉（https://github.com/anthropics/claude-code/issues/24207 、/issues/41737）。路径必须规范化后做边界校验，不用通配符与变量展开。

## 给用户的一句话问题

"这棵 worktree（<分支>）已并入主线、没有未提交改动；你还会回到它上面继续做吗？不会的话我整棵移除，会的话只清它里面的构建产物。"
