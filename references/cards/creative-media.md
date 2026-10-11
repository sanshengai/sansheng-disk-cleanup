# 知识卡：视频剪辑、设计、音乐与游戏平台

覆盖：Final Cut Pro、DaVinci Resolve、Premiere / After Effects、剪映 / CapCut；IINA、VLC、mpv、QuickTime；Figma、Sketch、Adobe 公共缓存、Blender；Spotify、Apple Music / Podcasts、网易云音乐、QQ 音乐；Steam、Epic、Battle.net、Minecraft；以及未单独列出的 Electron 应用的通用缓存识别。

总原则（见 `judgment.md`）：这类软件的"缓存"往往不便宜。渲染缓存重建要几十分钟到数小时，游戏重下要吃流量和等待，离线曲目可能是用户专门下载的。"可再生"、"放了很久"都不能单独当删除理由，建议里必须写清**删除后果**（要重做什么、大约多久、占多少额度）。每个类别都分两类：**生成物 / 缓存**（可以进建议）和**用户资产**（剪辑工程、素材、草稿、存档、游戏本体、离线下载、数据库，一律不碰）。

出处：目录名等事实参考 Mole、腾讯柠檬、BleachBit（均为 GPL-3.0，只借事实与思路）以及 Purge、mac-cleanup 系列（MIT / Apache-2.0）；说明用自己的话写，不复制代码与注释。

标注约定：本机列为 2026-10-11 实测（只读 `ls -d`、`du -sh`）。"不存在"表示路径没有；"本机未核对"表示本机没装该软件，只写通用规则，真实布局随软件版本可能不同，遇到时先浅层 `ls` 再下判断。

## 存储位置

按类别分列在下面一～六节的表里：每行写路径、内容、性质（生成物 / 可再生缓存 / **用户资产，保护**）和本机实测。没装的软件标“本机未核对”，遇到时先浅层 `ls` 再判断。

## 原生清理与保留设置

- 优先走软件自带入口：剪辑类在偏好里清渲染缓存、媒体缓存（Resolve 的 CacheClip、Premiere / AE 的 Media Cache 都能在偏好里清或改位置）；音乐类见第四节“原生清理与保留设置”（Spotify 设置 → 存储，Apple Music / Podcasts 走 App 内下载与存储管理，网易云 / QQ 音乐用软件内的缓存清理）。
- 游戏平台的本体与下载走平台客户端的卸载 / 移动，Skill 不删游戏目录。
- 这一类软件都没有可供 Skill 调用的命令行清理入口；`protect.SHAPES` 不含它们，建议只能是“按路径删可再生缓存”或“请用户在软件里操作”。

## 怎么判断还在用（通用运行判据）

- 软件进程在运行时**只报告，不建议**：`pgrep -fl "<进程名>"` 有输出即视为运行中。无法确认进程状态（命令失败）时也按运行中处理。
- 剪辑类的渲染缓存所在工程**正在打开**时不动。判据：软件在运行，或 `lsof +D <缓存目录> 2>/dev/null | head` 有输出。
- 不确定某个目录是缓存还是用户内容时，先问用户，不猜。

## 一、视频剪辑

本机状态：Final Cut Pro、DaVinci Resolve、Adobe 系、剪映 / CapCut 均未装（`/Applications` 未发现），下列路径全部"本机未核对"。仅 iMovie 已装，见表后。

| 路径 | 内容 | 性质 | 本机 |
|---|---|---|---|
| `<资源库>.fcpbundle/<事件>/Render Files/`（含 `High Quality Media`） | Final Cut Pro 的渲染文件 | 生成物，可再生 | 本机未核对 |
| `<资源库>.fcpbundle/<事件>/Transcoded Media/{Proxy Media,High Quality Media}` | 代理与优化媒体 | 生成物，可再生（需重新转码） | 本机未核对 |
| `<资源库>.fcpbundle/<事件>/Original Media`、`Analysis Files`、`Motion Templates`、`Final Cut Pro Backups`、`CurrentVersion.flexolibrary` | 导入的原始素材、分析结果、模板、备份、库描述 | **用户资产，保护** | 本机未核对 |
| `~/Library/Caches/com.apple.FinalCut` | 软件自身缓存 | 可再生缓存 | 不存在 |
| `~/Movies/CacheClip`、`~/Library/Caches/com.blackmagic-design.DaVinciResolve` | DaVinci Resolve 渲染缓存、优化媒体、Proxy（`CacheClip` 默认在 Movies，用户可在偏好里改位置） | 生成物，可再生 | 均不存在 |
| `~/Library/Application Support/Blackmagic Design/DaVinci Resolve`（数据库、项目、`Preferences`） | Resolve 的项目库与设置 | **用户资产，保护** | 不存在 |
| `~/Library/Application Support/Adobe/Common/Media Cache Files`、`Media Cache` | Premiere / After Effects 的导入索引、预览音频、峰值文件 | 可再生缓存（重建要几分钟到更久） | 不存在 |
| `~/Library/Caches/Adobe/*`、`~/Library/Caches/com.adobe.*` | Adobe 通用缓存，Premiere、AE 的磁盘缓存（Disk Cache）也可能落在用户自选位置 | 可再生缓存 | `Caches/Adobe` 不存在 |
| 项目文件（`.prproj`、`.aep`）、素材、导出成品 | 工程与素材 | **用户资产，保护** | 本机未核对 |
| `~/Movies/JianyingPro/User Data/Cache/{recognize,frameThumbnail,audioWave,AlgorithmCache,ILASDKDB,RemuxCache,prerender,segmentPrerenderCache,MotionBlurCache,ttsTemp,tmp}`（剪映专业版，`com.lemon.lvpro`） | 识别、缩略图、波形、预渲染等生成物，**仅此白名单内的子目录** | 生成物，可再生 | `~/Movies/JianyingPro` 不存在 |
| `.../JianyingPro/User Data/Projects`（草稿）、`image/`、`importcache3/`、`effect`、`onlineMaterial`、`music`、`template`、`local_models` | 草稿、导入素材副本、已下载的特效 / 音乐 / 模型 | **用户资产或下载内容，保护**（`importcache3` 存着导入素材的副本，草稿是加密的，无法证明没被引用） | 本机未核对 |
| `~/Movies/iMovie 剪辑资源库.imovielibrary`、`iMovie Theater.theater` | iMovie 资源库（已装 iMovie） | **用户资产，保护**；本机 196 KB、8 KB，几乎是空库 | 存在 196 KB / 8 KB |

### 怎么判断还在用

```
pgrep -fl "Final Cut|DaVinci|Resolve|Premiere|After Effects|VideoFusion|JianyingPro|CapCut"
lsof +D "<缓存目录>" 2>/dev/null | head                  # 有输出 = 打开中
find ~/Movies -maxdepth 4 -name "*.fcpbundle" -o -maxdepth 4 -name "CacheClip" 2>/dev/null
ls "<资源库>.fcpbundle"                                    # 只列事件目录名
```

- Final Cut 的资源库在用户自选位置（常见 `~/Movies`，也可能在外置盘），**不在 Library 里**。只处理 `.fcpbundle` 内上表两个生成物目录，且确认路径是真实目录、不是符号链接、确实落在 `.fcpbundle` 之内再动。
- 剪映只匹配主程序进程（`VideoFusion-macOS`），不要被托盘助手进程误导成"永远在运行"或"没运行"。
- 渲染缓存、代理媒体的位置都是用户可改的，**用户自选的暂存盘（外置盘、其他卷）不碰**，只报告。
- 工程已归档、不会再改，是用户的说法，不是我们能从目录时间推出的结论。

### 删除后果（写进计划）

下次打开对应工程，需要重新渲染、重新转码、重新生成代理和缩略图，时间随工程长度从几分钟到数小时不等；Resolve 的优化媒体和 Proxy 要重新生成，Premiere 要重新索引导入的素材，剪映旧草稿重开时会重新生成预览。**空间换时间**，要把"约 X GB、可能要重做多久"一起写进建议。

## 二、播放器

| 路径 | 内容 | 性质 | 本机 |
|---|---|---|---|
| `~/Library/Caches/com.colliderli.iina`（`thumb_cache`、`Cache.db`、`fsCachedData`、`WebKit`） | IINA 的进度条缩略图与网络缓存 | 可再生缓存 | 存在 5.6 MB，其中 `thumb_cache` 5.3 MB |
| `~/Library/Application Support/com.colliderli.iina/{plugins,watch_later,history.plist,input_conf}` | 插件、各视频的续播位置、播放历史、按键配置 | **用户设置与数据**，不碰（`watch_later` 清了会丢续播进度） | 存在 26 MB（插件）+ 296 KB（续播） |
| `~/Library/Caches/org.videolan.vlc`、`~/Library/Preferences/org.videolan.vlc`（含最近播放 `ml.xspf`） | VLC 缓存与最近播放记录 | 缓存可再生；最近播放是用户历史，清除前说明 | 不存在 |
| `~/Library/Caches/io.mpv`、`~/.config/mpv`、`~/Library/Application Support/mpv` | mpv 缓存与配置 | 缓存可再生；配置是用户资产 | 均不存在 |
| `~/Library/Containers/com.apple.QuickTimePlayerX` | QuickTime Player 容器（缩略图、最近文档、录屏暂存） | 内容不明，体积小 | 存在 48 KB，不值得建议 |

怎么判断还在用：`pgrep -fl "IINA|VLC|mpv"`；IINA 缩略图目录下次打开视频会重新生成，不影响播放。IINA 本机当前未见在运行，但清之前仍要复核。

后果：缩略图重新生成，首次拖动进度条略慢，对网络流媒体需要重新缓冲。**不要碰 `watch_later` 与 `history.plist`**，那是用户的观看进度。

## 三、设计

| 路径 | 内容 | 性质 | 本机 |
|---|---|---|---|
| `~/Library/Caches/com.figma.Desktop`、`com.figma.agent`；`~/Library/Application Support/Figma/{Cache,...}` | Figma 桌面端缓存 | 可再生缓存 | 均不存在 |
| Figma 本地字体 / 离线文件（`Application Support/Figma/` 下除缓存外） | 同步与离线草稿 | 用户数据，只经 App 处理 | 本机未核对 |
| `~/Library/Caches/com.bohemiancoding.sketch3`、`~/Library/Application Support/com.bohemiancoding.sketch3/cache` | Sketch 缓存 | 可再生缓存 | 均不存在 |
| `/.DocumentRevisions-V100` | 文档版本历史（系统级，需管理员） | **用户资产**，只报告不建议 | 本机未核对 |
| `~/Library/Caches/Adobe`、`~/Library/Application Support/Adobe/Common/Media Cache*` | Adobe 公共缓存（见第一章） | 可再生缓存 | 不存在 |
| `~/Library/Application Support/Blender/<版本>/`、`~/Library/Caches/Blender` | Blender 缓存与临时文件；其中 `config`、`scripts`、插件是用户配置 | 缓存可再生；配置是用户资产 | 均不存在 |
| 字体缓存（系统 `com.apple.FontRegistry`、`/private/var/folders/.../com.apple.FontRegistry`） | 系统字体注册缓存 | 系统组件 | **不建议**：删了字体会重建，期间应用可能显示异常，收益小 |

后果：缓存重建后首次打开文件略慢，Figma 需要重新下载字体与资源；Blender 的 `Application Support` 里先分清缓存与插件、配置。设计源文件（`.fig` 本地副本、`.sketch`、`.blend`、`.psd`）永远不碰。

## 四、音乐与播客

| 路径 | 内容 | 性质 | 本机 |
|---|---|---|---|
| `~/Library/Caches/com.spotify.client` | Spotify 缓存 | **缓存与离线曲目可能混放**，见下 | 不存在 |
| `~/Library/Application Support/Spotify/PersistentCache/Storage` | 离线下载（`offline.bnk`、加密曲目 `*.file`） | 用户选择下载的离线内容 | 不存在 |
| `~/Library/Caches/com.apple.Music` | Apple Music 封面、网络缓存（`MusicUIArtworkCache`、`Cache.db`、`fsCachedData`） | 可再生缓存 | 存在 1.3 MB |
| `~/Music/Music`（音乐资料库本体） | 导入的音乐与资料库数据库 | **用户资产**，本机仅 492 KB | 存在 492 KB |
| `~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts`（`Documents/MTLibrary.sqlite` 资料库、`Library/Cache`） | Podcasts 订阅库与节目缓存 | 资料库是用户数据；下载的节目是用户选择的离线内容 | 存在 14 MB（资料库 3.8 MB，`Library/Cache` 9.5 MB） |
| `~/Library/Containers/com.apple.podcasts/Data/{Library/BackgroundDownloads,tmp}` | 下载中暂存与临时文件 | 暂存 | 存在，`BackgroundDownloads` 0，`tmp` 0 |
| `~/Library/Caches/com.netease.163music` | 网易云音乐缓存 | 缓存可再生；用户"下载的歌曲"另存，且可能在自选目录 | 不存在 |
| `~/Library/Containers/com.tencent.QQMusicMac/Data/Library/Application Support/QQMusicMac/{iRRCache,iLog,iCache,iTemp}` | QQ 音乐流缓存、日志、临时 | 缓存可再生 | 不存在 |
| `.../QQMusicMac/iDownloadProxy` | QQ 音乐离线下载 | **用户选择的离线内容，保护** | 不存在 |

**关于本机 Podcasts 体积的更正**：`catalog-local.json` 里"约 711 MB"是 `~/Library/Group Containers/*` 整个通配的合计（约 727,604 KB），不是 Podcasts 一家。逐项核实：该合计里最大是 `group.com.apple.VoiceMemos.shared` 约 253 MB 和 `group.com.apple.SiriTTS` 约 237 MB，Podcasts 容器只占 13.5 MB。写计划时不要把 711 MB 归给 Podcasts。

### Spotify 的判据

先查离线下载，再决定缓存能不能动：

```
ls -la "$HOME/Library/Application Support/Spotify/PersistentCache/Storage" 2>/dev/null | head
stat -f %z "$HOME/Library/Application Support/Spotify/PersistentCache/Storage/offline.bnk" 2>/dev/null
ls "$HOME/Library/Application Support/Spotify/PersistentCache/Storage" 2>/dev/null | grep -c '\.file$'
```

`offline.bnk` 大于 1 KB，或存在加密曲目文件，即视为**有离线下载**：此时缓存与离线曲目混放，**不建议清缓存**，让用户在 Spotify 设置里调整缓存上限、或删除下载。没有离线下载时，`com.spotify.client` 缓存才可作为建议，后果是歌曲重新缓冲。

### 怎么判断还在用

`pgrep -fl "Spotify|Music|Podcasts|NeteaseMusic|QQMusic"`。注意本机 Podcasts 的 widget 进程（`PodcastsWidget`）常驻，不等于主程序在播放；只要有相关进程，缓存目录就只报告。

### 原生清理与保留设置

- Apple Music / Podcasts：下载的节目与歌曲是用户选择的离线内容，走 App 内设置（Podcasts 的设置 → 下载；Music 的 管理存储），不删容器文件。
- Spotify：设置 → 存储，可调缓存位置与上限、删除下载。
- 网易云 / QQ 音乐：软件设置里有缓存清理，优先走它；"已下载歌曲"要区分用户手动下载与流缓存。

## 五、游戏平台

本机状态：Steam、Epic、Battle.net、Minecraft 均未发现（`/Applications` 与 `Application Support` 都没有），本节全部"本机未核对"。

| 路径 | 内容 | 性质 | 本机 |
|---|---|---|---|
| `~/Library/Application Support/Steam/{appcache,depotcache,htmlcache,logs}` | 应用信息缓存、下载仓库缓存、内置浏览器缓存、日志 | 可再下载 | 不存在 |
| `~/Library/Application Support/Steam/steamapps/shadercache` | 着色器缓存 | 可再生（首次进游戏会卡顿重编译） | 不存在 |
| `~/Library/Application Support/Steam/steamapps/{temp,downloading}` | 下载中的暂存 | 暂存，可能是进行中的下载 | 不存在 |
| `~/Library/Application Support/Steam/steamapps/common` | **游戏本体** | **用户资产**，卸载走 Steam | 不存在 |
| `~/Library/Application Support/Steam/userdata`、`steamapps/compatdata`、游戏自己的存档 | 云存档之外的本地存档、配置 | **用户资产**，保护 | 不存在 |
| `~/Library/Caches/com.valvesoftware.steam` | Steam 软件缓存 | 可再生缓存 | 不存在 |
| `~/Library/Caches/com.epicgames.EpicGamesLauncher`、`~/Library/Application Support/Epic` | Epic 启动器缓存；已装游戏通常在用户选的目录 | 缓存可再生；游戏本体是用户资产 | 均不存在 |
| `~/Library/Caches/com.blizzard.Battle.net`、`~/Library/Application Support/Battle.net/Cache` | Battle.net 缓存 | 可再生缓存 | 不存在 |
| `~/Library/Application Support/minecraft/{logs,crash-reports,webcache,webcache2}` | 日志、崩溃报告、内置网页缓存 | 可再生 | 不存在 |
| `~/Library/Application Support/minecraft/{saves,resourcepacks,mods,versions}` | 存档、资源包、模组、游戏版本 | **用户资产**，保护 | 不存在 |

怎么判断还在用：`pgrep -fl "steam_osx|Steam|Epic|Battle.net|Minecraft|java.*minecraft"`；有下载在进行（`steamapps/downloading` 有文件、近期仍在增长）一律不动。

删除后果：`depotcache`、`appcache` 要重新拉取，Steam 首次启动变慢；着色器缓存要重新编译，进游戏时卡顿；浏览器缓存重建后商店页面首次加载慢。游戏本体不属于缓存，要释放它走 Steam 的卸载，并让用户决定。

## 六、Electron 应用通用缓存（兜底）

对本卡没有单独列出的 Electron 应用（Chromium 内核的桌面应用），在 `~/Library/Application Support/<App>/`（有时是 `<App>/Electron v3` 一类的子目录）下识别下面这些目录。**这只是兜底规则**：能在专门的卡里找到的应用，以专门的卡为准；应用运行中一律只报告。

| 子目录 | 内容 | 性质 | 本机核对 |
|---|---|---|---|
| `Cache`、`Code Cache`、`GPUCache` | HTTP 缓存、编译后的 JS、GPU 着色器 | 可再生缓存 | 思源笔记 `SiYuan-Electron`：`Cache` 9.0 MB、`Code Cache` 492 KB、`GPUCache` 548 KB；Xmind `Electron v3`：5.4 MB、16 KB、548 KB；MarkText：0、16 KB、548 KB |
| `DawnCache`、`DawnGraphiteCache`、`DawnWebGPUCache`、`GraphiteDawnCache`、`GPUPersistentCache` | WebGPU / Graphite 着色器缓存 | 可再生缓存 | 思源笔记：548 KB、548 KB、5.8 MB、0；MarkText 548 KB |
| `Service Worker/CacheStorage` | 网页离线缓存，部分应用（飞书类）可达数十 GB | **灰区**：可再生，但个别应用把离线内容放这里 | 抽查三个应用都不存在 |
| `Service Worker/ScriptCache` | Service Worker 脚本 | 不当缓存处理，保护 | 本机未核对 |
| `Local Storage`、`Session Storage`、`IndexedDB`、`Cookies`、`Preferences`、`databases` | 登录状态、设置、离线数据、应用自己的库 | **用户数据，不碰** | 全部存在，不动 |
| `blob_storage`、`Network Persistent State`、`Trust Tokens`、`DIPS`、`SharedStorage` | 网络与隐私状态 | 不碰，体积小 | 存在 |

识别方法：先看目录里有没有 `Local State`、`Preferences`、`Cookies`、`Code Cache`——这四个同时出现，基本就是 Electron。再确认进程是否在跑：`pgrep -fl "<App名>"`，或看进程命令行里的 `--user-data-dir=...` 是不是这个目录；`SingletonLock` 存在也说明正在运行（本机 `SiYuan-Electron/SingletonLock` 存在，思源正在运行，所以其缓存当前只报告）。

后果：缓存重建后应用首次打开略慢、需要重新联网拉资源；`CacheStorage` 在个别应用里可能是离线内容，先问用户。**不要整目录清 `Application Support/<App>`**，里面的 `IndexedDB`、`Local Storage` 是账号与数据。

注意思源笔记的笔记数据在 `~/Library/Application Support/SiYuan`（`data`、`history`、`repo`），**不在** Electron 缓存目录，是用户资产，不属于本章。

## 不能做的事

- 不整目录清 `~/Library/Caches/*`、`~/Movies/*` 或 `Application Support/<App>`。
- 不碰 `Original Media`、资源库本体、`.prproj`、`.aep`、剪映草稿与 `importcache3`、素材文件。
- 不清 Spotify 缓存当存在离线下载；不碰 Podcasts / Music 的资料库与下载节目；不碰 QQ 音乐 `iDownloadProxy`。
- 不碰 `steamapps/common`、`userdata`、Minecraft `saves`、任何游戏存档。
- 不删用户自选位置的缓存（外置盘、其他卷）；只报告。
- 不删字体缓存、`/.DocumentRevisions-V100`。
- 软件运行中、渲染缓存所在工程打开中，只报告。
- 不用 sudo；不以"多久没动""可再生"作为单独理由。

## 给用户的一句话问题

- "这些是剪辑软件的渲染缓存，删了下次打开工程要重新渲染约 X GB，这几个工程还会再改吗？"
- "Spotify 里有离线下载的歌，缓存和这些歌放在一起，清缓存会不会连离线曲目一起丢？要先在 Spotify 里看一下吗？"
- "这个目录是 Steam 的下载缓存，清了会重新拉取、进游戏时要重新编译着色器，要清吗？游戏本体我不会动。"
- "这些是某个应用的网页缓存，应用现在开着，等你关掉它我再看？"
