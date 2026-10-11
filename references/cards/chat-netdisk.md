# 聊天、协作软件与网盘：按"缓存 / 收到的文件 / 账号与聊天库"分层

适用于微信、企业微信、QQ、飞书、钉钉、Telegram、Slack、Discord、Zoom、腾讯会议、Teams，以及百度网盘、夸克网盘、阿里云盘、OneDrive、Dropbox、Google Drive、iCloud、WPS。

这些软件的数据混着三层。先分层，再谈能做什么：

| 层 | 含义 | Skill 能做什么 |
|---|---|---|
| 缓存层 | 日志、小程序/Chromium 缓存、可重新下载的运行时数据 | 报告体积；仅当本卡明确标"缓存层"、软件未运行、用户在看板确认后，才可作为建议项 |
| 收到的媒体层 | 别人发来的图片、文件、视频，可能是本机唯一副本 | 只报告体积；清理一律走软件自带入口，由用户自己点 |
| 账号与聊天库 | 数据库、登录态、消息索引 | 绝不碰，不进计划，不进看板的可选项 |

硬规则：聊天记录与收到的文件只走软件自带入口。本卡所有路径以本机布局为准，上游清理工具列的 `Caches/<bundle id>` 在国内软件上常常根本不存在（沙盒应用把数据放在容器或 Application Support 里），不照抄。

隐私：只看目录名和体积，不读聊天文件内容与数据库，报告里不写账号 ID（用 `<账号>` 占位）、不列聊天文件名。

"本机"列含义：已核对=2026-10-11 在本机用 `ls`/`du` 确认过存在及体积；本机未核对=本机没装或目录不存在，路径来自上游资料，装了的机器上需再核。

## 存储位置

### 微信 4.x（本机已装 4.1.13）

根：`~/Library/Containers/com.tencent.xinWeChat/Data/Documents`，容器合计约 1.3 GB。

| 路径（相对根） | 内容 | 层 | 本机 | Skill 能做什么 |
|---|---|---|---|---|
| `app_data/log` | 日志 | 缓存层 | 已核对 19M | 运行中只报告；未运行可作建议 |
| `../.wxapplet/WMPF`（容器 `Data/.wxapplet`） | 小程序运行时缓存 | 缓存层 | 本机无此目录 | 有则同上 |
| `xwechat_files/<账号>/msg/attach` | 收到的图片与附件（按月份子目录） | 收到的媒体 | 已核对 3.7M | 只报告；清理走微信"存储空间管理" |
| `xwechat_files/<账号>/msg/file` | 收到的文件 | 收到的媒体 | 已核对 82M | 同上 |
| `xwechat_files/<账号>/msg/video` | 收到的视频 | 收到的媒体 | 已核对 172K | 同上 |
| `xwechat_files/<账号>/{cache,business,resource}`、`msg/migrate`、`app_data/radium/{web,cache}` | 账号级缓存/业务/资源，用途无上游依据 | 用途待核实，不建议 | 已核对 3.2M / 12M / 4.9M；migrate 0；radium/web 52M | 只报告 |
| `xwechat_files/<账号>/db_storage` | 聊天数据库 | 账号与聊天库 | 已核对 41M | 绝不碰 |
| `xwechat_files/{all_users,Backup}` | 账号索引、备份 | 账号与聊天库 | 已核对 256K / 0 | 绝不碰 |
| `app_data/radium/users` | 账号 webview 状态 | 账号与聊天库 | 已核对 205M | 绝不碰 |
| `app_data/xplugin/plugins` | 插件包，上游无任何角色说明 | 用途待核实，不建议 | 已核对 854M，本机最大一块 | 只报告体积，不进建议；可告诉用户它随微信版本更新，不能据此判定可删 |

`.wxapplet/WMPF` 与 `~/Library/Caches/com.tencent.xinWeChat`（3.x 缓存位）、`temp/head_image`（头像缓存）本机均不存在或为 0，不单列。微信 3.x 旧布局（`Application Support/com.tencent.xinWeChat/<版本>/<账号哈希>/Message/MessageTemp/<会话>/{Image,File,Video}`）是收到的媒体层，其余同账号目录下为账号库；本机未核对。

### 企业微信（本机未安装）

| 路径 | 层 | 本机 | Skill 能做什么 |
|---|---|---|---|
| `~/Library/Containers/com.tencent.WeWorkMac/Data/Library/Application Support/WXWork/Log` | 缓存层 | 本机未核对 | 软件未运行、用户确认后可作建议 |
| `…/Data/Documents/cefcache/<Default 或 wew_数字>/{Cache,Code Cache,GPUCache,Service Worker/CacheStorage}` | 缓存层（Chromium 缓存，profile 名只接受这两种形态） | 本机未核对 | 同上；profile 名不符就不处理 |
| `…/Data/Documents/Profiles/<id>/Caches/{Images,Files,Videos}` | 收到的媒体 | 本机未核对 | 只报告，走软件入口 |
| `…/Documents/Profiles/*/Messages1` | 消息库 | 账号与聊天库 | 绝不碰 |
| `cefcache/<profile>` 里的 cookies、`Account Web Data` 等登录态 | 账号 | 账号与聊天库 | 绝不碰 |
| `…/Profiles/*/Publishsys` | 账号相关，用途无依据 | 账号与聊天库 | 绝不碰 |

软件内"文件储存位置"可能被用户改到别处，扫描时先问或读设置，不假定在容器内。

### QQ NT（本机已装 7.0.2）

根：`~/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ`，容器合计约 418 MB。

| 路径（相对根） | 内容 | 层 | 本机 | Skill 能做什么 |
|---|---|---|---|---|
| `nt_qq_<账号>/nt_data/log`、`log-cache` | 日志 | 缓存层 | 已核对 2.2M / 320K | 未运行、用户确认后可作建议；量小，通常不值得 |
| `nt_qq_<账号>/nt_data/Pic`、`File`、`Video` | 收到的图片、文件、视频 | 收到的媒体 | 本机不存在（没收过/版本不同） | 有则只报告，走 QQ 设置里的存储管理 |
| `nt_qq_<账号>/nt_data/Emoji` | 表情相关，上游无角色说明 | 用途待核实，不建议 | 已核对 215M，本机最大一块 | 只报告，不进建议 |
| `nt_qq_<账号>/nt_db` | 聊天数据库 | 账号与聊天库 | 已核对 8.0M | 绝不碰 |
| `global` | 全局数据（含账号相关） | 账号与聊天库（保守） | 已核对 158M | 绝不碰 |
| `Cookies`、`Local Storage`、`Partitions` | 登录态 | 账号 | 已核对 | 绝不碰 |

`nt_data/{avatar,OnlineStatus,…}` 与根下 `Cache`、`Cookies`、`Local Storage`：配置、状态或登录态，不碰。旧版 QQ 的 `Caches/{Images,Files,Videos}` 属收到的媒体，本机为空壳（236K）。

### 飞书 / Lark、钉钉（本机均未安装）

| 软件 | 路径 | 层 | 本机 | Skill 能做什么 |
|---|---|---|---|---|
| 飞书 | `~/Library/Application Support/LarkShell/aha/users/*/profile_explorer/Service Worker/CacheStorage`（国际版 Lark 为 `LarkInternational` 对应位置） | 缓存层，**只有这一项**：预缓存的编辑器包，无淘汰机制，重度用户可达数十 GB | 本机未核对 | 未运行、用户确认后可作建议；先报告体积 |
| 飞书 | `~/Library/Caches/com.feishu.*` | 缓存层 | 本机未核对 | 同上 |
| 钉钉 | `~/Library/Caches/dd.work.exclusive4aliding`、`~/Library/Application Support/iDingTalk/{log,holmeslogs}` | 缓存层/日志 | 本机未核对 | 同上，但钉钉容器 `5ZSL2CJU2T.com.dingtalk.mac` 与 `Caches/com.alibaba.DingTalkMac` 边界不清，在上游工具里有被排除的先例，只报告 |

### Telegram（本机未安装）

| 路径 | 层 | Skill 能做什么 |
|---|---|---|
| `~/Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/[stable 或 appstore]/account-*/postbox/media` | 可从云端重下的媒体缓存 | 未运行、用户确认后可作建议；路径必须严格锚定到 `postbox/media` |
| `…/postbox/db` 及 `postbox` 其余 | 账号库 | 绝不碰。某些上游清理工具会（带确认地）删 `postbox/db`，这是反例，不能学 |
| `~/Library/Caches/ru.keepcoder.Telegram` | 缓存层 | 本机未核对；可作建议 |

注意：私密聊天的媒体可能只存在本机（未核实），路径上无法区分，动 `postbox/media` 前要告知用户。

### Slack、Discord、Zoom、腾讯会议、Teams（本机均未安装）

| 软件 | 路径 | 层 | Skill 能做什么 |
|---|---|---|---|
| Slack | `~/Library/Application Support/Slack/{Cache,Code Cache}` | 缓存层 | 未运行、用户确认后可作建议 |
| Discord | `~/Library/Application Support/discord/{Cache,Code Cache}` | 缓存层 | 同上 |
| Zoom | `~/Library/Caches/us.zoom.xos` | 缓存层 | 同上 |
| Zoom | 本地录制文件（默认位置未核实，可自定义） | 用户自己的成果 | 只报告，不碰，走用户确认 |
| 腾讯会议 | `~/Library/Caches/com.tencent.meeting` | 缓存层 | 同上；本机未核对 |
| Teams（新版） | `~/Library/Caches/com.microsoft.teams2` | 缓存层 | 同上 |
| Teams（旧版） | `Application Support/Microsoft/Teams/{Cache,Application Cache,Code Cache,GPUCache,logs,tmp}` | 缓存层 | 同上 |
| Teams | `IndexedDB`、`Local Storage`、`databases` | 登录态与本地数据 | 绝不碰（个别上游清理工具会删，是过激做法） |

以上均本机未核对。Slack/Discord 的 Electron 目录名可用"Chromium 缓存目录名"通用识别，但只处理上表列出的名字。

### 网盘

通用判断：同步目录与已下载文件是用户文件，可能是云端内容在本机的唯一副本；很多同步盘是双向，删本地会同步删除云端。Skill 不删这些。

| 软件 | 路径 | 层 | 本机 | Skill 能做什么 |
|---|---|---|---|---|
| 百度网盘 | `~/Library/Containers/com.baidu.netdisk/Data/Library/Application Support/com.baidu.netdisk/tmp` | 缓存层（临时文件） | 已核对 8.5M | 无传输任务、未运行时可作建议，量小 |
| 百度网盘 | 同目录下 `<账号哈希>` 目录（账号数据库） | 账号 | 已核对 145M | 绝不碰 |
| 百度网盘 | 用户设置的下载目录与同步目录 | 用户文件 | 容器 `Data/Downloads` 与 `Documents` 为空；用户自选路径未知 | 只报告，不碰 |
| 夸克网盘 | `~/Library/Application Support/QuarkCloudDrive/updates` | 更新安装包（形如下载好的新版 `.app` 与 `.install`） | 已核对 754M，本机最大一块 | 先按 `app-updaters-installers` 卡判断：与已安装版本（7.2.2.805）对比、软件未运行、确认是否仍在安装流程；用途未经实测前只作"待核实的建议" |
| 夸克网盘 | `QuarkCloudDrive/Default`、`account.json` | 账户状态 | 已核对 6.1M | 绝不碰；其余内核缓存（`pars_data` 34M 等）用途未核实，只报告 |
| 阿里云盘 | `~/Library/Caches/com.alibaba.teambitiondisk`（上游路径，Teambition 盘） | 缓存层 | 本机未装/不存在 | 本机未核对；装了的机器上先看实际布局，不照抄 |
| OneDrive | `~/Library/Caches/com.microsoft.OneDrive` | 缓存层 | 本机未装 | 本机未核对；可作建议但提示可能触发重新校验 |
| Dropbox | `~/Library/Caches/com.dropbox.*`、`com.getdropbox.dropbox` | 缓存层 | 本机未装 | 本机未核对；同上提示重新校验 |
| Dropbox | `~/Dropbox/.dropbox.cache` | 删除缓存 | 本机未核对 | 在同步目录内，删除代价高，只报告 |
| Google Drive | `~/Library/Caches/com.google.GoogleDrive`、`~/Library/Application Support/Google/DriveFS/<id>/content_cache` | 内容缓存，可重下，但可能含"离线可用"文件副本 | 本机无 `DriveFS`（`Application Support/Google` 共 2.5G 属 Chrome 等） | 只报告，不建议 |
| iCloud | `~/Library/Mobile Documents`、`com.apple.bird`、`cloudd`、`CloudKit`、`iCloudNotificationAgent` 相关缓存 | 同步状态与用户文档 | 已核对 `Mobile Documents` 58M | **不碰清单**：不删、不建议、不进计划；腾空间走系统存储设置的"优化存储" |

### WPS Office（本机已装 12.1.28496）

Office 与网盘/云文档分开处理。容器：`~/Library/Containers/com.kingsoft.wpsoffice.mac`，合计 604M。

| 路径（相对 `Data`） | 内容 | 层 | 本机 | Skill 能做什么 |
|---|---|---|---|---|
| `.kingsoft/office6/data` | 配置与本地数据，用途未核实 | 混合 | 已核对 271M | 只报告，不碰 |
| `.kingsoft/wps/addons` | 插件 | 状态 | 已核对 142M | 只报告 |
| `Library/Application Support/Kingsoft` | 配置、恢复文档、云文档缓存 | 混合 | 已核对 176M | 不碰（可能含未保存文档的恢复文件） |
| `tmp` | 临时文件 | 缓存层 | 已核对 5.1M | 量小 |

## 怎么判断还在用

- 运行中只报告：`pgrep -fl "WeChat|WXWork|WeWork|QQ|Lark|Feishu|DingTalk|Telegram|Slack|Discord|zoom.us|wemeet|Teams|BaiduNetdisk|QuarkCloudDrive|AliyunDrive|OneDrive|Dropbox|Google Drive|wpsoffice"`。有进程就不把任何缓存层条目放进可选项，只写体积。
- 收到的媒体层：不在目录里挑文件，建议用户在软件自带的存储管理里按会话查看再清。聊天软件每月增长是正常的，更好的做法是在软件内设保留规则，而不是每次手清。
- 网盘分两块：客户端缓存与下载暂存（软件内"清除缓存"）；同步目录与已下载文件（用户文件，先确认云端也有、同步状态已完成，再问用户，且提醒双向同步会删云端）。
- 百度网盘传输队列有未完成任务时，不动其下载目录与 `tmp`。
- "多久没动"不是依据：聊天库长期不动也是用户的全部记录。atime 在 APFS 上不可靠，不拿来判断。

只读命令速查（只量大小，不读内容；报告只写体积不列文件名）：

```
du -sh ~/Library/Containers/com.tencent.xinWeChat ~/Library/Containers/com.tencent.qq 2>/dev/null
du -sh ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/*/msg/* 2>/dev/null
du -sh ~/Library/Containers/com.baidu.netdisk ~/Library/Containers/com.kingsoft.wpsoffice.mac 2>/dev/null
du -sh ~/Library/Application\ Support/QuarkCloudDrive/* 2>/dev/null | sort -h | tail -5
```

## 原生清理与保留设置

- 微信：设置 → 存储空间管理（或"通用设置"里的存储管理），可按会话清理聊天文件；路径随版本变，以实机界面为准。
- 企业微信：电脑端头像 → 设置 → 存储管理 → 文件储存位置，官方帮助 https://open.work.weixin.qq.com/help2/pc/13352 。
- QQ、百度网盘、夸克网盘、WPS、飞书、钉钉、Telegram、Slack、Discord、Zoom、Teams：设置里的缓存清理、存储管理、下载位置入口（具体菜单名未逐一核实，以实机为准）。Telegram 在"设置 → 数据与存储 → 存储用量"可按类型清理。

向用户问保留期时给选项，不替她定：

- 保留最近 90 天（腾讯柠檬"文件搬家"功能按创建时间以 90 天分"近/远"两组，作为参考分界）
- 保留最近 365 天（CleanMyWechat 默认天数）
- 自定义，或只清某几个会话

Skill 只报告各档大致能腾出多少，不替用户执行删除。

## 不能做的事

- 不手删聊天库：`db_storage`、`nt_db`、`Messages1`、`postbox/db`、`radium/users`、Teams 的 `IndexedDB`/`Local Storage` 等一律不碰；不要整目录删 `~/Library/Containers/*` 下这些软件的目录，聊天数据库、账号状态与索引绑在一起，删了聊天记录丢失或软件无法启动，且大多无法恢复。
- 不手删同步目录与已下载文件，也不因为"云端也有"就删本地：同步盘可能双向，删本地会同步删云端。
- 不拷贝或移动 `xwechat_files` 再改软链接，除非遵循官方迁移说明；版本间机制不同。
- 不照抄上游工具的路径：国内软件以本机布局为准（百度网盘不在 `Caches/com.baidu.netdisk`，WPS 不在 `Caches/com.kingsoft.wpsoffice.mac`，Mole 里的"夸克"是夸克浏览器）。
- 软件运行中不处理任何缓存层条目。
- 不把 WPS Office 的清理动作套到 WPS 云文档上，也不删 WPS 恢复文件目录。
- 不把"用途待核实"的大目录（微信 `app_data/xplugin`、QQ `nt_data/Emoji`、夸克 `updates` 在核实前）当垃圾；体积大不是删除理由，可再生也不是。
- 删了空间不涨：看快照与废纸篓（见 macos-system 卡）。

## 给用户的一句话问题

"微信/QQ/企业微信的聊天文件占了 <X> GB，我不会直接删。你希望保留最近多长时间的聊天文件（比如 90 天或 365 天）？确定后我指导你在软件的存储空间管理里按会话清理。"
