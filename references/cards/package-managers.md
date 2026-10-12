# 知识卡：包管理器与构建缓存

覆盖：npm、pnpm、yarn、bun、uv、pip、Homebrew、Gradle。总原则：这些是"可再生缓存"，清掉的代价是下次安装重新下载（离线环境或慢网络除外），原则上优先走各自原生命令，不手删目录。

## 存储位置

| 路径（macOS 默认） | 内容 | 性质 |
|---|---|---|
| `~/.npm/_cacache`（另有 `_logs`、`_npx`） | npm 下载缓存、日志、`npx` 临时包 | 可再生缓存 |
| pnpm 内容寻址存储，`pnpm store path` 给出位置（macOS 常见 `~/Library/pnpm/store`，未核实） | 全局包存储；项目 `node_modules` 用硬链接指向它 | 可再生缓存，但与项目硬链接共享 |
| yarn 缓存，`yarn cache dir` 给出位置（Classic 常见 `~/Library/Caches/Yarn`，Berry 为项目内 `.yarn/cache` 或全局，未核实） | 包压缩包 | 可再生缓存 |
| `~/.bun/install/cache` | bun 全局缓存 | 可再生缓存 |
| `~/.cache/uv`（`archive-v0`、`wheels-v6`、`sdists-v9`、`environments-v2`、`builds-v0`、`simple-v25`…） | uv 的 wheel、源码包、集中式项目环境、索引缓存 | 可再生缓存 |
| `~/Library/Caches/pip` | pip 的 HTTP 缓存与 wheel 缓存 | 可再生缓存 |
| `~/Library/Caches/Homebrew`（`downloads/`、`Cask/`、`api/`） | 下载的 bottle、Cask 安装包、元数据 | 可再生缓存 |
| `~/Library/Caches/clang` | 编译器模块缓存（Clang / 编译工具链的 `.pcm` 等） | 可再生缓存，下次编译自动重建，稍慢 |
| `/opt/homebrew/Cellar/<包>/<版本>` | 已安装的各版本包 | 工具状态（旧版本由 `brew cleanup` 处理） |
| `~/.gradle/caches/{modules-2,build-cache-1,journal-1,…}` | 依赖工件、构建缓存、访问时间索引 | 可再生缓存，`journal-1` 是索引不能删 |
| `~/.gradle/wrapper/dists/`、`~/.gradle/jdks/`、`~/.gradle/daemon/` | Wrapper 下载的 Gradle 发行包、下载的 JDK、守护进程注册与日志 | 工具状态（含运行程序） |

位置可由环境变量改：`NPM_CONFIG_CACHE`、`UV_CACHE_DIR`、`GRADLE_USER_HOME`、`HOMEBREW_CACHE`、`PIP_CACHE_DIR`。先看环境再看默认路径。

## 怎么判断还在用

- **有没有安装/构建进程**：`pgrep -fl "npm|pnpm|yarn|bun|uv|pip|brew|gradle"`；有则跳过。Gradle 守护进程（`gradle` / `GradleDaemon`）运行时不动 `~/.gradle`。
- **24 小时内有没有被写**：包管理缓存每次安装都会被碰，近期有写入属于正常，不能用作"在用"的证据，只能用作"别和它同时清"。
- **pnpm 与项目的硬链接**：`pnpm store prune` 只清没有任何项目引用的包；store 里仍被 `node_modules` 硬链接的文件，删 store 不会损坏项目，但真正释放空间要等项目目录也删了。
- **Gradle**：`build-cache-1` 是任务输出缓存，`modules-2` 是依赖工件，`wrapper/dists` 与 `jdks` 被工程直接消费。有在维护的 Android/Java 工程时，旧 Gradle 版本、JDK 可能仍被某个工程锁定（看工程里的 `gradle/wrapper/gradle-wrapper.properties`）。
- **uv**：`uv cache size` / `uv cache dir`；集中式项目环境被 `uv prune` 清理后下次自动重建。
- **Homebrew**：`brew cleanup -n` 先看会删什么；`brew autoremove -n` 看孤儿依赖。
- **增长视角**：缓存会长回来。清一次后记录体积，治理建议是设自动清理或定期 prune，而不是反复手删。
- **`~/Library/Caches/clang`**：编译器模块缓存，删了下次编译自动重建，只是第一次编译会慢一些；没有编译任务在跑时可作建议。

## 快缓存与慢缓存（写卡时的 rebuild 标记）

清掉以后"能不能很快回来"决定进哪一档，不能一律当稳妥项：

- **快缓存**（npm/pnpm/yarn/bun/uv/pip/Homebrew 的下载缓存、Clang 模块缓存）：重新下载或重编译通常几分钟内完成，可以进稳妥档。
- **重建慢的缓存要显式标慢**：Gradle 构建缓存与依赖、本地模型（`model-caches` 卡）、大型依赖缓存这类，重建可能几十分钟到几小时。给它们写卡时在卡对象里加 `rebuild: "slow"`；`plan.py` 据此把它们从稳妥档提为推荐档，不默认帮你清。

只读命令速查：

```sh
du -sh ~/.npm ~/.bun/install/cache ~/.cache/uv ~/Library/Caches/pip ~/Library/Caches/Homebrew 2>/dev/null
uv cache dir && uv cache size
brew cleanup -n          # 只预演
du -sh ~/.gradle/* 2>/dev/null | sort -h
```

## 原生清理与保留设置

| 工具 | 命令 | 说明 |
|---|---|---|
| npm | `npm cache verify`（校验并回收）、`npm cache clean --force` | 后者清整个 `_cacache`；`_logs` 可单独删 |
| pnpm | `pnpm store prune` | 官方说明：不会影响项目，之后若需要会重新下载；`pnpm store path` 查位置。https://pnpm.io/cli/store |
| yarn | `yarn cache clean` | Berry 与 Classic 行为不同，以 `yarn --version` 区分（未核实 Berry 细节） |
| bun | `bun pm cache rm`、`bun pm cache`（查看目录） | 本机 `bun pm` 帮助中核实有 `cache` 子命令，未实际执行 |
| uv | `uv cache prune`（删未用条目和集中式环境，官方称可定期运行）、`uv cache prune --ci`、`uv cache clean [包名]`、`uv cache size` | https://docs.astral.sh/uv/concepts/cache ；本机 `uv cache --help` 核实 |
| pip | `pip cache info`、`pip cache purge` | 未在本机执行 |
| Homebrew | `brew cleanup`（默认清 120 天以前的下载与旧版本，`HOMEBREW_CLEANUP_MAX_AGE_DAYS` 可调）、`brew cleanup --prune=all`（清所有缓存下载）、`-n` / `--dry-run` | 不要与 `brew install/upgrade/uninstall` 同时运行 |
| Gradle | 原生自动清理：已发布版本相关缓存 30 天未用、快照 7 天、下载资源 30 天、创建资源 7 天、本地构建缓存 7 天、守护进程日志 14 天；无用的 `wrapper/dists` 随之删除 | 保留期可在 `~/.gradle/init.d/` 的 init 脚本里改。https://docs.gradle.org/current/userguide/directory_layout.html |

## 不能做的事

- 不手删 `~/.gradle/caches/journal-1` 和 `modules-2/*.lock`：前者是原生清理依赖的访问记录。整桶 `rm -rf ~/.gradle` 会连 `wrapper/dists`、`jdks` 一起删，工程下次要重新下载整套发行包与 JDK。
- 不对 `~/.gradle/daemon` 在守护进程运行时下手，不凭进程名缺失判断空闲。
- 不手删 Homebrew `Cellar` 里的目录：用 `brew uninstall`/`brew cleanup`，否则符号链接和 `opt` 指向会悬空。
- 不手删 pnpm store 中的单个文件：硬链接计数和 store 索引会失配。
- 不在 CI 或离线环境上"顺手清缓存"，那份缓存可能是离线构建的唯一来源。
- 清缓存不能当作修复手段的第一步，也不要期待它解决项目内 `node_modules`、`.venv`、`target` 的体积（那些是工程产物，走工作树/构建产物卡）。
- 实测提醒：释放量常小于 `du` 的显示值，因为 APFS 克隆、硬链接共享；以前后 `df` 为准。

## 给用户的一句话问题

- 「这些缓存可以清，代价是下次装依赖要重新下载（约 N GB），现在网络环境允许吗？」
- 「旧版本的 Gradle / JDK 还有哪些工程在用？没有的话我交给 Gradle 自己回收。」
- 「要不要把清理改成定期自动（例如每月 `brew cleanup` 和 `uv cache prune`），不再手动回收？」
