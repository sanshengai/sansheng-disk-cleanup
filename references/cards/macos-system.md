# macOS 系统层：快照、可清除空间、"系统数据"

适用于"删了很多东西空间不涨"、"系统数据几百 GB"、"可清除空间"这类问题。系统层的大头往往不是文件，而是快照与缓存的计量口径。

## 存储位置

| 路径或对象 | 内容 | 性质 |
|---|---|---|
| APFS 本地快照（无路径，`tmutil` 查看） | Time Machine 本地快照、系统更新前快照 | 可再生缓存（系统会自动回收，也可手动删） |
| "可清除空间"（purgeable） | 系统认为空间不够时可回收的部分，含快照与可重新下载的 iCloud 文件 | 可再生缓存（计量口径，不是目录） |
| "系统数据"（Settings → General → Storage） | 没被归类的一切：缓存、日志、虚拟内存、快照、插件、模拟器等 | 混合（不是一个目录，不能整体清） |
| `~/Library/Caches/<应用或 Bundle ID>` | 应用缓存 | 可再生缓存（按子目录处理） |
| `~/.Trash`、各卷 `.Trashes` | 废纸篓 | 用户确认过的删除，但空间未释放；叁笙清理选了“先放废纸篓”时，东西也在这里的 `叁笙清理-<scan_id>-<时间>` 文件夹里，清空前不算腾出空间（我们不替用户清空，也不删废纸篓里已有的东西） |
| `~/Library/Application Support/MobileSync/Backup` | iPhone/iPad 的本机备份 | 用户独有成果（可能是唯一备份）；本机不存在 |
| `~/Library/Containers/com.apple.mail/Data/Library/Mail Downloads`（旧版 `~/Library/Mail Downloads`） | 邮件附件的打开副本 | 可再生缓存（原件在邮件里）；路径随版本变化；本机两个路径都不存在 |
| `~/Library/Mail`、`~/Library/Messages` | 邮件库、信息库 | 用户独有成果（受保护，不动） |
| `/private/var/vm` 下的 swap 与 sleepimage | 虚拟内存与休眠镜像 | 系统状态（不碰） |
| `/private/var/folders`（每用户一组两级随机目录） | 按子目录角色分：`C/` 缓存；`T/` 运行中程序的临时目录；`0/` 等其余目录为系统与程序状态。只有能确认属于已退出程序、且可重建的 GPU/着色器缓存类（`C/` 下形如 `com.apple.metal*`、`com.apple.gpuarchiver`）才可作为建议 | 混合：`C/` 内上述类目为可再生缓存；`T/`、`0/` 及其余一律只报告不动。本机存在，`probe system` 实测约 36GB（含当前用户读不到的部分，`status=partial_denied`）；手工 `du` 只量到约 20G，口径更窄。部分子目录不可读，不要为读取而提权 |
| `/Applications/Install macOS*.app` | macOS 安装器 | 可再生缓存（可重新下载），但默认不勾。判据三条同时满足才进入候选：bundle id 为 `com.apple.InstallAssistant.*`（只看名字不够）、主版本不等于当前系统、未在运行。用户可能留着做重装或制作启动盘，须问。本机不存在 |
| `~/Library/iTunes/*Software Updates`（iPhone/iPad 固件，`.ipsw`） | 设备刷机固件包，单个常 5–8GB | 可再生缓存（需要时可重新下载）；不要连带碰同级的设备备份。本机目录不存在 |
| `~/Library/Logs/DiagnosticReports` | 当前用户的崩溃与诊断报告 | 可再生缓存（用户可读可清）；正在排查故障时保留。本机存在，3.8M |
| `/Library/Logs/DiagnosticReports`、`/private/var/db/diagnostics`、`/private/var/db/powerlog` | 系统级崩溃报告、统一日志数据、电源日志 | 系统状态，需管理员权限：只报告，给用户命令自己执行；正在排查故障时保留，清掉会丢现场。本机均存在：19M、1.6G、2.6G（`probe system` 在完全磁盘访问下可读到大小） |
| `/Library/Updates`、`/macOS Install Data` | 系统更新的暂存区 | 见下文"看似缓存其实不能删"。本机 `/Library/Updates` 存在 152K，`/macOS Install Data` 不存在 |

## 怎么判断还在用

- 删了东西空间不涨：先看是不是选了“先放废纸篓”（成果页会写“已移入废纸篓，清空后腾出”），再依次查 `tmutil listlocalsnapshots /`、废纸篓是否已清空、APFS 克隆（复制出来的文件共享块，删一份不释放）。
- 看真实可用量：`df -h /System/Volumes/Data` 或 `diskutil info /` 的可用空间；Finder 的"可用"含 purgeable，口径更乐观。
- 快照列表为空但"可清除"仍大：这部分多半是可重新下载的 iCloud 文件，不要靠删文件去换。
- `~/Library/Caches`：只按子目录判断。看该子目录对应的应用是否在运行（`pgrep`），是否 24 小时内有改动。
- iOS 备份：每个子目录是一台设备，`Info.plist` 里有设备名与最后备份日期；用户是否还有这台设备、是否有 iCloud 备份，必须问。
- 最近使用时间只是软信号：APFS 的 atime 不可靠（见 Eclectic Light 对 APFS 时间戳的说明：https://eclecticlight.co/2025/10/24/be-careful-when-interpreting-apfs-timestamps ）；`kMDItemLastUsedDate`（`mdls -name kMDItemLastUsedDate <文件>`）对很多文件为空。两者都不能单独作为"没用过"的依据。

优先用探针，一次拿到完全磁盘访问状态、本地快照、容量三口径、System Data 粗估和系统级大块：

```
python3 scripts/scan.py probe system [--run <run目录>]
```

字段含义：`full_disk_access` 为 granted / denied / inconclusive 三态；`local_snapshots` 分 `os_update` / `time_machine` / `other` 计数（tmutil 不给单个快照大小）；`capacity` 给三个口径；`system_data` 是 APFS 容器已用减去已计量得到的未归因粗估；`big_system_items` 列系统级大块，`/Library/Updates` 与 `/macOS Install Data` 标 `do_not_touch`。

容量三口径各自含义：
- statvfs 可用：文件系统实际空闲，不含可清除空间，是"现在真能写多少"的口径，释放量以它为准。
- APFS 容器空闲：整个容器（可能被多个卷共享）的空闲，与 statvfs 通常很接近。
- Finder 口径可用：把可清除空间也算作可用，数字最乐观，也是设置里存储条的口径。
- purgeable 只是估算（Finder 口径减 statvfs），系统缺空间时才会回收，不代表能被某个动作主动释放，不要当作可清理目标。
- 本机实测：statvfs 约 408 GiB，Finder 口径约 410 GiB，purgeable 估算约 2.6 GiB，无本地快照。

手工命令作后备（探针不可用时）：

```
tmutil listlocalsnapshots /
df -h /System/Volumes/Data
diskutil info / | grep -i -E "free|available|purgeable"
du -sh ~/Library/Caches/* 2>/dev/null | sort -h | tail
du -sh ~/Library/Application\ Support/MobileSync/Backup/* 2>/dev/null
```

读取 `~/Library/Application Support/MobileSync` 或 `~/Library/Mail` 可能触发系统的隐私授权弹窗；被拒绝时记入 skipped，不要要求用户去开"完全磁盘访问"来换取扫描。

- 释放量预期：删快照后 `df` 可用量会很快变化，而设置里的存储条可能滞后一段时间；以 `df` 为准。
- 删除前提：确认外置备份最近一次成功；没有备份时不建议手动清全部快照。
- `com.apple.os.update-*` 快照是系统更新用的，`tmutil` 删不掉，不要尝试，也不要把它算进可清理量；其余快照（`com.apple.TimeMachine.*` 等）才是上面命令的对象。
- 本机没有快照、也没有配置 Time Machine 时，提醒用户一句：没有备份本身比腾空间更值得关注。

## 原生清理与保留设置

- 列快照：`tmutil listlocalsnapshots /`；按日期删一个：`sudo tmutil deletelocalsnapshots <日期>`。
- 让系统释放指定字节：`sudo tmutil thinlocalsnapshots / <字节数> <紧急度 1-4>`（命令出现在 `tmutil help`，参数含义以 `man tmutil` 为准）。
- 较新的系统是否提供一次性清除全部快照的参数（有来源称 macOS 26 加入 `--purge`）：未核实，以本机 `tmutil help` 为准。
- Apple 的说明：https://support.apple.com/en-us/102154 （Time Machine 会随时间或空间需求自动删除本地快照；想手动删可先把备份频率设为"手动"，稍等几分钟）。
- 存储管理：Settings → General → Storage 里有"建议"（优化存储、自动清空废纸篓、减少杂乱）。
- iOS 备份：Finder 侧边栏选设备 → 管理备份；或系统设置里的存储项（入口随系统版本变化，未核实）。
- 邮件附件：Mail → Settings → Accounts → Download Attachments 设为"最近"或"无"。

## `~/Library/Caches` 保护清单

整个目录不能清空（见下文），逐个子目录处理时，以下类别即使体积大也只报告，不勾选。误删的后果是钥匙串反复弹窗、被登出需重新登录、授权或许可证失效、输入法词库丢失，且症状出现得晚、不易追溯到这次清理。

| 类别 | 典型目录（名称或关键词） | 本机核对 |
|---|---|---|
| 账号与身份 | `com.apple.accountsd`、`com.apple.appleaccountd`、`com.apple.amsaccountsd`、`com.apple.akd`、`CloudKit`、`FamilyCircle`、`familycircled`、`PassKit`、含 icloud / appleid / authkit 的目录 | 均存在：accountsd 3.6M、amsaccountsd 2.3M、akd 2.2M、CloudKit 13M |
| 授权与许可证 | 各应用的 license / activation 缓存目录 | 未核对 |
| 钥匙串相关 | 名称含 keychain 的目录 | 未核对 |
| 输入法 | `com.tencent.inputmethod.wetype`（微信输入法）、搜狗等第三方输入法目录、名称含 inputmethod 的目录 | wetype 存在 92K；搜狗未核对 |
| 密码管理器 | 1Password、Bitwarden、LastPass、Dashlane、KeePass、Authy 等 | 未核对 |
| Finder、Dock、系统设置 | `com.apple.finder*`、`com.apple.dock*`、`com.apple.systempreferences*`、`com.apple.systemsettings*` | 本机 Caches 下不存在 |
| 企业安全代理与 MDM | CrowdStrike、SentinelOne、ESET、Jamf、Palo Alto、Cisco AnyConnect 等 | 未核对；碰了可能触发篡改告警 |
| 屏幕使用时间 | 名称含 screentime 的目录 | 未核对 |

判断时先看目录名是否落入上述类别，落入就只报告；体积再大也不改变这一点。

## 看似缓存其实不能删

| 路径 | 为什么不能删 | 本机 |
|---|---|---|
| `/Library/Updates` | 系统更新的暂存区，年龄、进程都证明不了它不活跃；系统会自己清理 | 存在 152K（`do_not_touch`） |
| `/macOS Install Data` | 同上，系统更新进行中会用 | 不存在 |
| `~/Library/Calendars/Calendar Cache` | 日历数据的一部分，日历服务常开着它，删了可能让日历出错 | 不存在 |
| `~/Library/Autosave Information` | 可能存着应用还没保存的文稿 | 存在 0 |
| `/Library/Application Support/com.apple.idleassetsd/Customer` | 屏保航拍视频，由系统按需下载与管理 | 不存在 |
| `~/Library/Caches/com.apple.e5rt.e5bundlecache` | 已编译的模型缓存，删后系统重编译，收益小且可能触发卡顿 | 存在 0 |
| `~/Library/Application Support/com.apple.wallpaper/aerials/thumbnails` | 动态壁纸缩略图，删了会让壁纸重新生成 | 未核对 |

这些条目体积小或为 0 是常态；被命中时只报告，附上原因。

## Electron 应用通用缓存

Electron 应用（聊天、笔记、编辑器等）在自己的 Application Support 下放 `Cache`、`Code Cache`、`GPUCache` 等可再生目录，应用退出后才可作为建议，运行中一律只报告；同级的登录态、本地数据库、`Local Storage` 不是缓存。具体目录与判据见 `creative-media` 卡「Electron 应用通用缓存（兜底）」一节。

## 相关卡

- 浏览器缓存与 profile：`browsers` 卡。
- 应用更新暂存与安装包：`app-updaters-installers` 卡。

## 不能做的事

- 不要清空整个 `~/Library/Caches`：有些应用把登录态、已下载内容放在这里，清空后会被登出或重新下载；对话类应用可能丢未同步内容。只按子目录处理，逐个确认。
- 不要手动删 `/private/var/db`、`/Library/Application Support/...`、或 `/System/Volumes/Data/.Spotlight-V100` 等系统目录来"减少系统数据"。
- 不要删 `~/Library/Mail`、`~/Library/Messages`、`~/Library/Mobile Documents`（iCloud）里的数据来腾空间。
- 不要把 iOS 备份当缓存。备份可能是手机丢失后唯一的恢复来源。
- 不要把 "系统数据" 数字归零当目标：目标是可用空间足够，数字滞后、被重新归类都很常见。
- 不要用 `sudo rm` 删快照挂载点；快照只能走 `tmutil` 或 `diskutil apfs deleteSnapshot`。

## 给用户的一句话问题

"我看到 <N> 个本地快照约占 <X> GB，是系统自动留的；你的 Time Machine 备份最近一次是什么时候？确认有备份后我可以按日期清掉旧快照。"
