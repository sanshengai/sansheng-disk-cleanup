# 知识卡：软件更新暂存与安装包

覆盖：Sparkle、Squirrel/ShipIt、electron-updater、Google Updater、Microsoft AutoUpdate、Zoom、Karabiner 等第三方更新器的下载暂存；macOS 系统更新（只报告）；Homebrew 下载缓存（交叉引用）；下载目录里的 `.dmg/.pkg/.mpkg/.zip/.xip/.iso` 安装包。本 Skill 不做软件卸载，这里只处理"更新时下载下来、已经用过或可能用不上"的包。

总原则（见 `judgment.md`）：暂存"可再生"、"放了很久"都不能单独当删除理由。更新器下载的东西可能正是**待安装的更新**，删了要重下，甚至更新中途失败。建议清理必须有正向证据：暂存里的应用与已装应用是同一个 bundle id，且暂存版本**不高于**已装版本。

出处：判据思路来自 Mole 对 Codex Sparkle 暂存的处理（GPL-3.0，只借事实与思路，不复制其代码与原文）；路径事实参考 mac-cleanup-go（MIT）与 Sparkle 项目（MIT，`SPUUpdateDriver` 对已下载更新可续用）。

## 存储位置

"本机"列为 2026-10-11 实测（只读 `ls`、`du`）。

| 路径 | 内容 | 性质 | 本机 |
|---|---|---|---|
| `~/Library/Caches/<bid>/org.sparkle-project.Sparkle/{Launcher,Installation,PersistentDownloads}` | Sparkle 下载的更新包、解包后的待安装 `.app`、更新器副本 | 更新暂存（可能是待安装更新） | 存在 4 个：`com.steipete.codexbar` 312 KB，`com.cmuxterm.app`、`com.cmuxterm.app.nightly`、`com.openai.codex` 为空壳 |
| `~/Library/Caches/<bid>.ShipIt` | Squirrel（Electron）的更新器工作目录，内含待安装的更新 | 更新暂存（可能是待安装更新） | 存在：`com.anysphere.sand.ShipIt` 20 KB，`com.stablyai.orca.ShipIt` 44 KB |
| `~/Library/Caches/<名>-updater/pending`、同级 `update.zip` | electron-updater 下载的更新包与 `update-info.json`（含 sha512） | 更新暂存 | 存在：`orca-updater` 487 MB（示例见下） |
| `~/Library/Application Support/Google/GoogleUpdater/crx_cache` | Google Updater 下载的安装/更新包与扩展包，`metadata.json` 记录 hash 对应的 appid | 更新暂存，**共享组件**，不归单个应用 | 存在 745 MB：两个 hash 文件，27 MB（9-27）与 753 MB（10-9，appid `com.google.chrome`）；已装 Chrome 154.0.8037.99 |
| `~/Library/Application Support/Google/GoogleUpdater/*.old`（含 `updater.log.old`、`updater_history.jsonl.old`） | 更新器自己的旧日志与历史 | 日志 | 存在，各约 1 MB；`GoogleUpdater` 整体 773 MB |
| `~/Library/Caches/com.google.GoogleUpdater`、`~/Library/Google/GoogleSoftwareUpdate` | Google 更新器缓存与旧版 Keystone | 共享组件 | 存在：80 KB、14 MB |
| `~/Library/Caches/org.pqrs.Karabiner-Updater` | Karabiner 更新器的网络缓存（`Cache.db`） | 可再生缓存 | 存在 288 KB |
| `~/Library/Application Support/Microsoft/AutoUpdate`、`/Library/Application Support/Microsoft/MAU2.0`、`~/Library/Caches/com.microsoft.autoupdate.fba` | Microsoft AutoUpdate 的下载与状态 | 更新暂存 / 共享组件 | 不存在（`AutoUpdate` 已核）；其余未核对 |
| `~/Library/Application Support/zoom.us/AutoUpdater` | Zoom 的更新包 | 更新暂存 | 不存在 |
| `~/Library/Caches/com.microsoft.EdgeUpdater` | Edge 更新器缓存 | 更新暂存 | 不存在 |
| `/Library/Updates`、`/Library/Caches/com.apple.SoftwareUpdate`、`/macOS Install Data`、`/Applications/Install macOS *.app` | macOS 系统更新与完整安装器 | **刻意不碰**，系统自己管理 | `/Library/Updates` 存在 152 KB（只有两个 plist）；`/macOS Install Data` 不存在；其余未核对 |
| `~/Library/Caches/Homebrew`（`downloads/`、`Cask/`） | brew 下载的 bottle、Cask 安装包 | 交给 `package-managers.md` | 存在 173 MB |
| `~/Downloads`、`~/Desktop`、`~/Documents`、`~/Library/Mobile Documents/com~apple~CloudDocs/Downloads` | 手动下载的安装包 | 用户文件候选 | 体积 108 MB / 136 MB / 2.0 GB / 8 KB；前三者浅层（深度 2）没有 `.dmg/.pkg/.mpkg/.zip/.xip/.iso`，更深层未核对 |

其他"Telegram 下载目录、Mail Downloads"本机未核对。`/Users/Shared`（含其下的 Downloads）整体属于他人共享目录，脚本一刀切保护，清理计划里不会出现。

## 怎么判断还在用

先用 `scan.py probe updaters`（将提供）拿事实：它对每个暂存输出 bundle id、暂存版本、已装版本和结论。

| 结论 | 含义 | 建议 |
|---|---|---|
| `superseded` | 暂存与已装应用 bundle id 相同，暂存版本**不高于**已装版本 | 可作为建议清理；仍须通过下面的进程与打开文件检查 |
| `pending_install` | 同 id，暂存版本**高于**已装版本 | 保留。这是待安装的更新，任何年龄都不动 |
| `unknown` | 读不出版本、读不出 bundle id、已装多个副本，或没有对应已装应用 | 不下结论，不作为建议依据；只把年龄作为参考，问用户 |
| `in_use` | 更新器进程在运行，或目录有打开文件 | 不动，等进程结束再看 |

补充规则：

- bundle id 必须完全相同。不同应用之间比版本没有意义；`com.stablyai.orca` 与 `com.stablyai.orca.nightly` 是两个应用。
- 同一 bundle id 在多个位置装了多个副本时不下结论（`mdfind` 失败或超时也算）。
- ShipIt 二进制在多个 Electron 应用里同名，所以不能用 `pgrep -x ShipIt` 判断谁在用。进程归属要看命令行里是否出现**完整** bundle id，且同行没有别的 id 片段。
- 共享组件不归属到单个应用：Sparkle 框架本身、Google Keystone / GoogleUpdater 服务、Electron 运行时。它们的 `crx_cache` 即使能对上 appid，也只在能读出包内版本并与已装版本比较后才给结论；读不出就是 `unknown`。
- 年龄只用于排序和提示，不是"多久没动就删"的依据。

只读命令速查：

```sh
du -sh ~/Library/Caches/*-updater ~/Library/Caches/*.ShipIt ~/Library/Caches/*/org.sparkle-project.Sparkle 2>/dev/null
ls -la ~/Library/Caches/<名>-updater ~/Library/Caches/<名>-updater/pending
unzip -l <暂存.zip> | grep 'Contents/Info.plist' | head          # 只列目录，不解压
unzip -p <暂存.zip> '<App名>.app/Contents/Info.plist' > "$TMPDIR/x.plist"   # 只读取一个文件
defaults read /Applications/<App>.app/Contents/Info CFBundleIdentifier CFBundleShortVersionString CFBundleVersion
mdfind "kMDItemCFBundleIdentifier == '<bid>'"                      # 确认已装副本是否唯一
pgrep -fl "Sparkle|ShipIt|Updater|updater"
lsof +D <暂存目录> 2>/dev/null | head                              # 有输出就是打开中
```

注意：`unzip -p '*/Contents/Info.plist'` 会命中嵌套的 helper 与 Framework 的 Info.plist，输出混在一起无法解析；要写成 `<App名>.app/Contents/Info.plist` 的精确路径（本机 Orca 实测踩到）。

### 示例：orca-updater（本机实测）

- `~/Library/Caches/orca-updater` 共 487 MB（10 月 7 日）：`update.zip` 与 `pending/Orca-1.4.222-arm64-mac.zip` 都是 254,935,569 字节，`cmp` 证实逐字节相同（同一份下载放了两处）；`pending/update-info.json` 172 字节，记录文件名与 sha512。
- 读 zip 内 `Orca.app/Contents/Info.plist`：`CFBundleIdentifier=com.stablyai.orca`，`CFBundleShortVersionString=CFBundleVersion=1.4.222`。已装 `/Applications/Orca.app` 同为 `com.stablyai.orca` 1.4.222；`mdfind` 未发现第二个副本，Orca 与其更新器进程当时没有在运行。
- 结论 `superseded`：暂存版本等于已装版本，更新已安装完。可作为建议，估计释放 487 MB（`update.zip` 与 `pending` 内那份合计）；默认不勾选，由用户在看板确认。
- 同一应用的 `~/Library/Caches/com.stablyai.orca.ShipIt`（44 KB）体积太小，不单独建议。

### 本机其余项

- `GoogleUpdater/crx_cache` 的 753 MB 文件（appid `com.google.chrome`，10-9）：updater 目录版本 156.0.8067.0 高于已装 Chrome 154.0.8037.99，包很可能是**还没装上的 Chrome 更新**，且属共享组件，结论按 `unknown` 处理，不建议。27 MB 的那个 hash 对应 appid `{44fc7fe2-…}`，本机未识别出对应应用。
- 本机有一个已运行约 4 天的 `stats-updater-*.sh` 进程，命令行指向 `~/Downloads/Stats.dmg`（文件当前不存在）。这类更新脚本进程出现时，对应应用相关暂存一律 `in_use`。
- Sparkle 的 4 个目录里只有 `com.steipete.codexbar` 有内容（312 KB），其余是空壳，不值得建议。

## 安装包（`.dmg/.pkg/.mpkg/.zip/.xip/.iso`）

扫到的只是**候选**，不是结论。

- `.zip`：先 `unzip -l` 看前若干条目里有没有 `.app`、`.pkg`、`.dmg`、`.xip`，没有就不算安装包（普通压缩包别碰）；有的话用精确路径读 `<App>.app/Contents/Info.plist`，bundle id 与版本都与已装一致才算"已装过"，可作为建议。
- `.dmg`：**不挂载**，标"无法核对，问用户"。
- `.pkg/.mpkg`：可只读 `pkgutil --pkg-info` 或 `--payload-files` 对照收据；对不上就是"无法核对"。
- `.xip/.iso`：通常是 Xcode、系统镜像一类大文件，标"无法核对"，问用户。
- 全部**默认不勾选**。用户可能留着安装包做降级、离线安装或给别的机器用；Homebrew `downloads/` 里的 dmg/zip 若与已装版本一致，也是"已装过"证据，但清理走 `brew cleanup`。
- 在 iCloud Drive 的 Downloads 里的文件，删除会同步到所有设备，先告知。

## 原生清理与保留设置

- 各软件自己的"检查更新 / 安装更新"：安装成功后暂存会被它自己消费或清掉，这是最稳的释放办法。暂存很大又是 `pending_install` 时，让用户在软件里完成更新，比删暂存好。
- Homebrew：`brew cleanup -n` 预演、`brew cleanup` 执行，详见 `package-managers.md`。
- macOS 系统更新：系统设置 → 通用 → 软件更新，由系统管理。

## 不能做的事

- 不对 `~/Library/Caches/*.ShipIt/*` 或 `*-updater/*` 一律清：会误删待安装的更新（mac-cleanup-go 的通配规则就是反例）。
- 不碰 `/Library/Updates`、`/Library/Caches/com.apple.SoftwareUpdate`、`/macOS Install Data`；这些需要 sudo，且无法证明扫描到删除之间没人使用。只报告体积。
- 不删正在下载的更新：更新器进程在跑、目录有打开文件、文件近期仍在增长，都当 `in_use`。
- 不把共享组件（Sparkle 框架、Google Updater/Keystone、Electron 运行时）归给某个应用，也不把它们当孤儿。
- 不挂载 `.dmg`；不读安装包里除 `Info.plist` 以外的内容。
- 不用 sudo。
- 不以"多久没动""可再生"作为单独理由。

## 给用户的一句话问题

- "这个安装包对应的软件已经装好了同一版本，要留着做降级或重装吗？"
- "这个更新包比你现在装的版本新，看起来还没装上，要先去软件里完成更新吗？"
- "这个文件我没法确认它属于哪个软件，你还要用吗？"
