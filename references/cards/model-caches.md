# 知识卡：本地模型与浏览器二进制缓存

覆盖：HuggingFace Hub 缓存、ollama、LM Studio、Playwright 浏览器。共同点：体积大（常达几十 GB）、能重新下载，但"重下"要耗流量和时间，且部分模型已下架或需要授权才能再拿到，所以不能只凭"可再生"下结论。

## 存储位置

| 路径 | 内容 | 性质 |
|---|---|---|
| `~/.cache/huggingface/hub/models--<组织>--<名>/{blobs,snapshots,refs}` | HF 模型/数据集缓存；`snapshots/<版本>` 里是指向 `blobs/` 的符号链接 | 可再生缓存（需联网；私有/门控模型需有授权） |
| `~/.cache/huggingface/hub/**/*.incomplete` | 中断下载的残块 | 可再生缓存，纯垃圾 |
| `~/.cache/huggingface/xet/` | Xet 存储的共享块（按 `.refs` 清单被多个仓库共用） | 可再生缓存 |
| `~/.cache/huggingface/token`（若存在） | HF 登录令牌 | 绝对保护 |
| `~/.ollama/models/{manifests,blobs}` | ollama 模型；manifest 指向 `sha256-*` blob | 可再生缓存（自建/改写的模型除外） |
| `~/.ollama/history`、自建 Modelfile | 提示历史、自建模型定义 | 用户独有成果 |
| `~/.lmstudio/models/<发布者>/<模型>/`（旧版 `~/.cache/lm-studio/models`） | LM Studio 下载的模型文件 | 可再生缓存 |
| `~/.lmstudio/conversations/` | LM Studio 聊天记录（JSON） | 用户独有成果 |
| `~/Library/Caches/ms-playwright/<浏览器>-<构建号>/` | Playwright 下载的 Chromium / Firefox / WebKit / ffmpeg | 可再生缓存 |

环境变量可改位置：`HF_HOME`、`HF_HUB_CACHE`、`OLLAMA_MODELS`、`PLAYWRIGHT_BROWSERS_PATH`。路径与默认值以现场环境变量为准。

## 怎么判断还在用

- **有没有进程在加载模型**：`lsof` 命中 blob 文件，或 `ollama ps`（ollama 运行时）显示已加载。模型文件被 mmap 时 `lsof` 会命中。
- **是否被项目引用**：对 `组织/模型名`、`snapshots/<哈希>`、ollama 的 `名称:标签` 在 `reference_roots` 的代码、脚本、配置里做 `git grep`。命中 `from_pretrained("…")`、`ollama run …`、配置里的 `model:` 即保留。
- **HF 有多个版本**：`hf cache ls` 列出每个仓库的各 revision；`hf cache prune` 只删不被任何分支/标签引用的 revision 与 `.incomplete` 残块，是最安全的一步。同一模型多个量化版本要问用户保留哪一个。
- **ollama 的 tag**：`ollama list` 看全部模型与 `MODIFIED` 时间；同一基础模型的多个 tag 通常共享 blob，删单个 tag 释放的空间小于其显示大小。
- **Playwright 旧构建**：缓存里有多个 `chromium-<号>`，而当前项目的 `node_modules/playwright-core/browsers.json`（或 `npx playwright --version`）只需要其中一个。多个 Playwright 版本的工程各要各的构建，删前对各工程的锁定版本做检查。
- **LM Studio**：软件内的模型管理页是首选；聊天记录目录单独询问。

只读命令速查：

```sh
du -sh ~/.cache/huggingface/hub/models--* 2>/dev/null | sort -h | tail
find ~/.cache/huggingface -name '*.incomplete' 2>/dev/null | head
du -sh ~/.ollama/models/blobs 2>/dev/null
du -sh ~/Library/Caches/ms-playwright/* 2>/dev/null
env | grep -E '^(HF_|OLLAMA_|PLAYWRIGHT_)'            # 位置是否被改
```

- HF 的 `snapshots/<版本>/` 里的文件是符号链接，`du` 对它们不重复计，别把 snapshots 和 blobs 加起来。
- 一次性下载的大模型常在 `.incomplete` 里停很久；确认没有下载进程再处理，且优先交给 `hf cache prune`。
- 体积排序时把"同一模型的多个量化/多个 revision"归为一个任务卡，只问一次保留哪一个。

## 原生清理与保留设置

- **HuggingFace**：`hf cache ls`（查看）、`hf cache rm <repo 或 revision>`（删指定）、`hf cache prune`（删无引用 revision、`.incomplete`、无仓库使用的共享块）、`hf cache verify`（校验）。旧命令名 `huggingface-cli` 同族；用 `--cache-dir` 指定缓存目录。说明：https://huggingface.co/docs/huggingface_hub/en/guides/manage-cache 。本机当前未安装 `hf`，只核实了目录存在。
- **ollama**：`ollama list`、`ollama rm <模型名:标签>`（删 manifest 并回收不再被引用的 blob）。位置与迁移：https://docs.ollama.com/faq （路径与命令由第三方文章交叉印证，官方页面未逐字核实）。
- **LM Studio**：App 内 My Models 管理；命令行 `lms ls` / 相关子命令（未核实具体删除命令）。
- **Playwright**：`npx playwright uninstall` 删当前安装的浏览器，`npx playwright uninstall --all` 连其他 Playwright 安装的浏览器一起删；`npx playwright install` 重新装。来源：https://playwright.dev/docs/browsers
- 没有任何一项带按天数的自动清理（HF 的 prune 与 ollama 的 rm 都是手动触发）。这类缓存属于"用完留着"的增长型，治理建议是定期 `hf cache prune`，而不是反复删整桶。

## 不能做的事

- 不手删 HF `blobs/` 里的单个文件：`snapshots/` 下符号链接会悬空，库加载报错；用 `hf cache rm/prune`。
- 不手删 `~/.ollama/models/blobs/` 里的 `sha256-*`：多个模型共享 blob，manifest 里还留着引用，之后 `ollama run` 会报错或重下。
- 不在下载进行中清理：`.incomplete` 可能正是当前下载；先看有无 `hf`、`ollama pull`、浏览器安装进程。
- 不动 HF 令牌、ollama 自建 Modelfile、LM Studio 聊天记录。
- 不因为"Playwright 浏览器能重装"就删掉离线环境（无外网/受限网络）下唯一的一份。
- 不把"体积大"当成可清理的理由：先对"最近有没有加载"和"项目是否引用"做核对。

## 给用户的一句话问题

- 「这几个模型（列名称与大小）你还会在本机跑吗？不用的我用官方命令一次性删掉。」
- 「同一个模型的这几个量化版本，保留哪一个？」
- 「这些 Playwright 浏览器是给哪个项目用的？那个项目还在维护吗？」
