# 浏览器：Chrome、Edge、Brave、Arc、Vivaldi、Opera、115 浏览器、Firefox、Safari

适用于浏览器缓存、旧版本框架、更新暂存、未完成下载的占用判断。浏览器数据同时含缓存、登录态、历史和扩展数据，"可再生"不等于"可以删"：缓存也要先确认浏览器已退出，登录态和站点数据只走浏览器自己的设置入口。

"本机"一列是 2026-10-11 在一台实机上用 `ls -d` / `du -sh` 只读核对的结果，只量体积不读文件内容；体积随使用变化，仅作量级参考。

## 存储位置

Chromium 系（Chrome、Edge、Brave、Arc、Vivaldi、Opera、115 浏览器）布局基本相同。macOS 上要分清两处：HTTP 缓存和 Code Cache 在 `~/Library/Caches/<厂商>/<产品>/<profile>/`，Application Support 里是 GPUCache、站点数据、扩展和账号数据。只看 Application Support 会漏掉最大的一块。

| 路径（`<profile>` 即 `Default` 或 `Profile N`） | 内容 | 性质 | 本机 |
|---|---|---|---|
| `~/Library/Caches/Google/Chrome/<profile>/Cache`、`Code Cache` | Chrome 的 HTTP 缓存、脚本编译缓存 | 可再生缓存 | 存在，Cache 约 1.2 GB、Code Cache 约 291 MB |
| `~/Library/Caches/Google/Chrome/<profile>/Storage` | 缓存区里的扩展相关存储 | 缓存与状态混合 | 存在，约 49 MB，未逐项核对用途 |
| `~/Library/Application Support/Google/Chrome/<profile>/{GPUCache,DawnWebGPUCache,DawnGraphiteCache}` | 图形缓存 | 可再生缓存 | GPUCache 约 5.6 MB、DawnWebGPUCache 约 1.6 MB 存在；DawnGraphiteCache 本机未见 |
| `~/Library/Application Support/Google/Chrome/{ShaderCache,GrShaderCache,GraphiteDawnCache,GPUPersistentCache}` | 着色器缓存 | 可再生缓存 | GraphiteDawnCache 约 13 MB 存在；其余本机不存在 |
| `.../Google/Chrome/{component_crx_cache,extensions_crx_cache}` | 组件与扩展的安装包暂存 | 可再生缓存（会重下） | 存在，约 365 MB / 25 MB |
| `.../Google/Chrome/Crashpad/completed` | 崩溃报告 | 可再生缓存（日志类） | Crashpad 约 19 MB 存在，completed 子目录未单独核对 |
| `.../Google/Chrome/{optimization_guide_model_store,OptGuideOnDeviceModel,OptGuideOnDeviceClassifierModel,OnDeviceHeadSuggestModel}` | 端侧 AI 与建议模型 | 可再生缓存（可重下的大块，下次需要时重新下载） | optimization_guide_model_store 约 48 MB 存在；其余三个本机不存在 |
| `.../Google/Chrome/{screen_ai,TranslateKit,SODA,SODALanguagePacks,WasmTtsEngine}` | 屏幕阅读、翻译、语音等组件包 | 可再生缓存（可重下的大块，随功能使用重新下载） | 存在，约 128 / 98 / 51 / 77 / 23 MB，是否都能安全移除未核实 |
| `/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Versions/<版本>` | 浏览器本体框架 | 可再生（旧版本） | 存在两个版本：154.0.8037.98（约 713 MB，旧）与 154.0.8037.99（约 726 MB，`Current` 指向） |
| `~/Library/Application Support/Google/GoogleUpdater/crx_cache` | 更新器下载暂存 | 可再生缓存 | 存在，约 745 MB |
| `~/Library/Application Support/Google/GoogleUpdater/<版本>` 与 `Current` | 更新器本体（`Current` 是符号链接） | 工具状态 | 156.0.8067.0 约 26 MB，不动 |
| `.../Google/Chrome/<profile>/Service Worker/CacheStorage` | 网站（含 PWA）预缓存 | 灰区 | 存在，约 548 MB |
| `.../Chrome/<profile>/Service Worker/ScriptCache` | 扩展与站点 worker 的脚本 | 工具状态（见"不能做的事"） | 存在，约 15 MB |
| `.../Chrome/<profile>/{IndexedDB,File System}` | 网站本地数据库、文件系统存储 | 灰区 | IndexedDB 约 63 MB 存在；File System 本机未见 |
| `.../Chrome/<profile>/{Cookies,Network/Cookies,Local Storage,Session Storage,Sessions,History,Web Data,Login Data,Bookmarks,Favicons,Sync Data}` | 登录态、历史、表单与密码、书签、同步状态 | 用户独有成果 | History 约 12 MB、Local Storage 约 6 MB、Sync Data 约 0.7 MB 存在；其余文件未逐项核对 |
| `.../Chrome/<profile>/{Extensions,Local Extension Settings,Extension State}` | 扩展本体与扩展自己存的数据 | 用户独有成果 | 存在，约 161 / 14 / 4.7 MB |
| `.../Chrome/Local State` | 全部 profile 的列表与设置 | 工具状态 | 存在 |
| `~/Library/Application Support/Google/{Chrome for Testing,Chrome-headless,ChromeForTesting}` | 自动化测试用的 Chrome 副本 | 工具状态 | 约 33 MB / 0 / 4 KB，归自动化工具卡处理，这里只提醒别当成日常 Chrome |
| `~/Library/Application Support/Microsoft Edge`、`~/Library/Caches/Microsoft Edge`、`~/Library/Caches/com.microsoft.edgemac` | Edge 的同类布局 | 缓存与状态混合 | 本机只有 `Application Support/Microsoft Edge/NativeMessagingHosts`（约 12 KB）；Caches 两处不存在，应用未装 |
| `~/Library/Application Support/BraveSoftware/Brave-Browser`、`~/Library/Caches/BraveSoftware/Brave-Browser` | Brave 的同类布局 | 缓存与状态混合 | 只有空壳目录（约 8 KB，内含 `NativeMessagingHosts`）；Caches 不存在，应用未装 |
| `~/Library/Application Support/Arc/User Data/<profile>`、`~/Library/Caches/company.thebrowser.Browser` | Arc 的同类布局 | 缓存与状态混合 | 只有空壳（`User Data/NativeMessagingHosts`）；Caches 不存在，应用未装 |
| `~/Library/Application Support/Vivaldi`、`~/Library/Caches/Vivaldi` | Vivaldi | 缓存与状态混合 | 只有 `NativeMessagingHosts`；Caches 不存在，应用未装 |
| `~/Library/Application Support/com.operasoftware.Opera`、`~/Library/Caches/com.operasoftware.Opera` | Opera | 缓存与状态混合 | 只有 `NativeMessagingHosts`；Caches 不存在，应用未装 |
| `~/Library/Application Support/Chromium` | Chromium 开源版 | 缓存与状态混合 | 只有 `NativeMessagingHosts`；应用未核对 |
| `~/Library/Caches/115Browser/Default/{Cache,Code Cache}` | 115 浏览器 HTTP 缓存与 Code Cache | 可再生缓存 | 存在，约 287 MB / 19 MB |
| `~/Library/Application Support/115Browser/{Default,GrShaderCache,ShaderCache,component_crx_cache,Crashpad}` | 115 浏览器（Chromium 内核）的 profile 与图形、组件缓存 | 缓存与状态混合 | 存在，约 11 / 6.2 / 5.6 / 2.9 / 1.9 MB；`Default` 里 GPUCache 约 5.6 MB 为缓存，Sessions、Web Data、HistoryEx 为用户数据 |
| `/Applications/115Browser.app/.../115Browser Framework.framework/Versions/<版本>` | 115 浏览器本体框架 | 可再生（旧版本） | 只有一个版本 125.0.6422.61，无旧版本可清 |
| `~/Library/Caches/org.115browser.115Browser` | 115 浏览器的系统层网络缓存（`Cache.db`） | 可再生缓存 | 存在，约 276 KB |
| `~/Library/Caches/Firefox/Profiles/<profile>`、`~/Library/Application Support/Firefox/Profiles/<profile>/cache2` | Firefox 缓存 | 可再生缓存 | 本机不存在，应用未装 |
| `~/Library/Application Support/Firefox/Profiles/<profile>/{cookies.sqlite,places.sqlite,logins.json,key4.db,storage,extensions}` | 登录态、历史与书签、密码、站点数据、扩展 | 用户独有成果 | 本机不存在，文件名为通用知识，未在实机核对 |
| `~/Library/Caches/com.apple.Safari`、`com.apple.Safari.SafeBrowsing` | Safari 与安全浏览缓存 | 可再生缓存 | 存在，约 264 KB / 276 KB |
| `~/Library/Containers/com.apple.Safari/Data/Library/{Caches,WebKit}` | Safari 容器内的缓存与网站数据 | 缓存与状态混合 | Caches 约 1.9 MB、WebKit 约 12 MB 存在 |
| `~/Library/Safari/Metadata Cache`、`Favicon Cache`、`Touch Icons Cache` | 站点元数据与图标缓存 | 可再生缓存 | 约 29 MB / 380 KB / 588 KB 存在 |
| `~/Library/Safari/{History.db,Bookmarks.plist,Downloads.plist,LocalStorage,TopSites.plist,RecentlyClosedTabs.plist,CloudAutoFillCorrections.db}` | 历史、书签、下载记录、站点存储、云端自动填充 | 用户独有成果 | 均存在，History.db 约 384 KB，CloudAutoFillCorrections.db 约 12 MB |
| `~/Downloads/*.crdownload`、`*.download`、`*.part` | 未完成下载的半成品 | 用户独有成果（可能能续传） | 本机当前 0 个 |

## 怎么判断还在用

分四档，删除后果各不相同：

| 档位 | 包含 | 删除后果 |
|---|---|---|
| 一、纯缓存 | `Cache`、`Code Cache`、`GPUCache`、`DawnCache` 类、`ShaderCache` 类、`Crashpad/completed`、`component_crx_cache`、`extensions_crx_cache`、Safari 的 Caches 与 Metadata Cache | 不丢数据、不登出。浏览器会重建，首次打开网页、扩展或渲染略慢 |
| 二、可重下的大块 | 旧版本框架 `Versions/<旧版本>`、`GoogleUpdater/crx_cache` 及 `*.old`、端侧 AI 与翻译、语音等组件包 | 不丢数据。组件或模型下次用到时重新联网下载（几十到几百 MB 流量）；离线时对应功能暂不可用 |
| 三、灰区 | `Service Worker/CacheStorage`、`IndexedDB`、`File System` | 缓存部分只是变慢；但 PWA 和有离线模式的网站会丢离线数据，部分网站的本地草稿、未同步内容存在这里。默认不进清理计划，只报体积和归属 |
| 四、会登出或丢数据 | `Cookies`、`Local Storage`、`Session Storage`、`Sessions`、`History`、`Web Data`（自动填充）、`Login Data`（密码）、`Bookmarks`、`Sync Data`、扩展目录与扩展数据 | 登出所有网站、丢历史与书签、丢保存的密码与表单、扩展设置清空且可能无法找回。只由用户在浏览器里操作 |

判断步骤：

1. **浏览器是否在运行**。看进程名，再看 profile 根目录有没有锁：
   - Chromium 系：profile 根目录（如 `.../Google/Chrome/`）里的 `SingletonLock` 是符号链接，指向 `主机名-进程号`；进程号还活着就是运行中，进程号已不存在的旧锁可视为残留，但要再用进程名确认一次。
   - Firefox：profile 目录里的 `parent.lock` 或 `.parentlock` 被占用即运行中。
   - Safari 和不确定的情况：按"运行中"处理。
   - **运行中只报告体积，不建议清缓存，也不把它放进可执行计划**；状态判断不出来，同样按运行中处理。
2. **识别多个 profile**。读 `Local State` 里的 profile 列表是最准的办法；不读内容时，退一步看 profile 根目录下的 `Default`、`Profile 1`、`Profile 2`…，且目录里要有 `History` 数据库才算真 profile（只有 `System Profile` 或 `Guest Profile` 的不算）。每个 profile 的缓存分开报体积。
3. **旧版本框架**：同时满足才算候选。
   - `Versions/` 下的版本不是 `Current` 指向的版本（`Current` 是符号链接，或看 `Info.plist` 的 `CFBundleShortVersionString`）；
   - 对应浏览器没有运行；
   - `Current` 指针完好，指针损坏或无法读取就整个跳过。
   本机例：154.0.8037.98 是候选，154.0.8037.99 是当前版本。这类目录在 `/Applications` 里，系统可能要求管理员权限；没有权限就写给用户，不用提权绕过。
4. **更新暂存**：`GoogleUpdater/crx_cache` 与 `*.old` 文件，浏览器和更新器都没在运行时才算候选；`GoogleUpdater/<版本>`、`Current` 不动。本机 crx_cache 约 745 MB，可重新下载，但仍需让用户确认。
5. **未完成下载**：`.crdownload`、`.download`、`.part` 同时满足三条才算候选：没有进程打开它、最后修改已超过 7 天、对应浏览器没有在运行。仍要提醒这可能是还能续传的大文件，让用户确认下载来源是否还需要。
6. **有离线模式的网站数据**：Google Docs、Sheets、Drive、Notion、Figma、GitHub、Linear、飞书、Excalidraw 等，它们的 `CacheStorage`、`IndexedDB` 可能存着未同步的离线改动。只要灰区要动，就先问用户。
7. **多久没动都不是理由**：旧的 profile、很久没用的浏览器，需要用户说"这个浏览器我不再用"才能进入卸载建议，由用户在应用管理里卸载，不拆目录。

只读核对命令：

```
pgrep -fl "Google Chrome|Microsoft Edge|Brave Browser|Arc|Vivaldi|Opera|115Browser|firefox|Safari"
ls -la ~/Library/Application\ Support/Google/Chrome/SingletonLock
du -sh ~/Library/Caches/Google/Chrome/* ~/Library/Application\ Support/Google/Chrome 2>/dev/null
ls -d ~/Library/Application\ Support/Google/Chrome/Default ~/Library/Application\ Support/Google/Chrome/Profile* 2>/dev/null
ls "/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Versions/"
readlink "/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Versions/Current"
du -sh ~/Library/Application\ Support/Google/GoogleUpdater/crx_cache 2>/dev/null
du -sh ~/Library/Caches/115Browser ~/Library/Application\ Support/115Browser ~/Library/Safari 2>/dev/null
```

只量体积，不读 `Cookies`、`History`、`Login Data` 等文件的内容。报告里不写访问过的网站名。

## 原生清理与保留设置

以下入口是给用户点的，Agent 不替她点，也不通过命令模拟：

- Chrome：地址栏输入 `chrome://settings/clearBrowserData`，时间范围选"全部时间"，只勾"缓存的图片和文件"，不勾 Cookie 与其他网站数据、密码、自动填充数据。
- Edge：`edge://settings/clearBrowserData`，同样只勾缓存。
- Brave：`brave://settings/clearBrowserData`，同样只勾缓存。
- Arc、Vivaldi、Opera、115 浏览器：设置里的"清除浏览数据"，只勾缓存一项；具体菜单名随版本变化，本机未核对。
- Firefox：设置 → 隐私与安全 → Cookie 和网站数据 → 清除数据，只勾"缓存的 Web 内容"，不勾"Cookie 和网站数据"。
- Safari：设置 → 隐私 → 管理网站数据可按网站删除；"开发"菜单（需在高级设置里开启）里有"清空缓存"，只清缓存。"清除历史记录"会同时清掉 Cookie 与浏览记录，不要当成清缓存用。
- 清理后不用手动重启，浏览器会自己重建；让用户看到的体积不涨回，是因为缓存会随浏览慢慢增长，这是正常的。
- 旧版本框架与 `crx_cache` 没有原生入口，Chrome 重启后常自动回收。让用户先完全退出并重启一次浏览器，再复查，比手工删稳妥。

## 不能做的事

- 不要动 `Service Worker/ScriptCache`：里面是扩展和站点 worker 的脚本，删了会让扩展 worker 失效，浏览器还可能弹出安全警告。看起来像缓存，实际不是。
- 不要动 `Cookies`、`Local Storage`、`Session Storage`、`History`、`Web Data`、`Login Data`、`Bookmarks`、`Sync Data`，也不要整删 profile 目录或 `Local State`：会登出网站、丢密码和书签，同步账号的 profile 还可能触发重新同步。有些清理工具曾把 Arc 的 `History`、`Cookies`、`Web Data` 当缓存删，这是反面例子。
- 不要删扩展目录与 `Local Extension Settings`：扩展自己存的数据（如密码管理器、脚本管理器的脚本）往往没有备份。
- 不要手删 SQLite 数据库的主文件而留下 `-journal`、`-wal`、`-shm`，或相反：这类文件成组出现，单删会让库损坏。
- 不要在浏览器运行中清缓存或删 `Versions/` 下的旧版本，也不要在 `Current` 指针损坏时删任何版本目录。
- 不要把有离线模式的网站数据（Google Docs、Notion、Figma、GitHub 等）当缓存清。
- Safari 的数据在容器里，不手动删 `~/Library/Containers/com.apple.Safari` 或 `~/Library/Safari` 下的数据文件（沙盒与权限问题，且包含历史、书签、云端同步数据）；缓存目录按 Safari 设置入口处理，或让用户在系统设置里管理。
- 不要对 `~/Library/Caches/*` 整体清空：钥匙串里会因此反复弹窗，且其他应用缓存不属于本卡。
- `.crdownload` 等下载半成品不因"太旧"就删，先看有没有被打开，再问用户是否仍需续传。
- 不要把 Playwright、Puppeteer 的浏览器副本（`~/Library/Caches/ms-playwright` 等）当作日常浏览器处理，它们归自动化工具卡；本机 `ms-playwright` 约 555 MB。
- 不要拿"多久没动""体积大"单独当作删除理由。

## 给用户的一句话问题

"Chrome 的缓存目前约 <X> GB，旧版本框架和更新暂存还有约 <Y> GB。这些都能重新下载，但我只在浏览器完全退出后才动。可以先退出 Chrome 吗？另外只清缓存、不清登录、历史和密码，可以吗？"

灰区和用户数据只在用户明确说"清登录态/站点数据"后，指导她在浏览器的"清除浏览数据"里自己操作。


## macOS Chrome 启动克隆（code_sign_clone）

系统以当前用户的 `getconf DARWIN_USER_TEMP_DIR` 定位 `T/`，同级 `X/com.google.Chrome.code_sign_clone/` 里可能积累启动克隆。它们是应用程序副本，不是浏览记录、成果或用户资料。Chromium 每启动实例创建克隆，正常析构时调用独立清理助手；强杀进程、异常终止或助手失败可能留下残留。不能仅凭目录名、年龄或当前没有名为 Chrome 的进程就删除。

先按创建时间、应用版本、主程序 inode 分组，再调查产生它的启动器、任务回执和关闭方式。代码中的 `fast: true` / `SIGKILL` 是可验证的风险路径，但没有逐次日志时不能声称所有残留都由它造成。整改优先复用合适的浏览器实例，在 finally 中请求 `Browser.close` 并等待进程退出，超时才强杀；测试与渲染工具适合时选 Chrome for Testing / headless shell（先检查现有依赖与渲染兼容性）。不要为了省空间禁用日常 Chrome 的安全机制，不批量杀用户浏览器。

APFS 克隆共享数据块；74 份应用不等于 74 倍物理占用。只报告名义体积，删除前后实测磁盘变化，并说明并发任务影响。即使是旧版，其数据块也可能仍被安装目录保留的旧框架引用。

专用命令（沿用本会话既有授权；新授权应针对具体清单）：

```sh
python3 scripts/chrome_clones.py plan --run <已有运行目录>
python3 scripts/chrome_clones.py apply --run <同目录> --seal <输出摘要> --reason <授权依据> --pilot
python3 scripts/chrome_clones.py apply --run <同目录> --seal <同摘要> --reason <同依据>
```

执行仅允许当前用户、系统发现路径下的单个六位后缀目录。整目录必须只有有效 Google 签名的 Chrome 包、无越界链接/额外资料；创建超过一天、版本严格早于已安装版本，整个目录 `lsof +D` 无打开 inode 且无错误，封存后配置、目录清单、版本与身份一致。保留全部现版本克隆，即使它们看似无人使用。调用已签名安装包内的 Chrome 原生清理助手，不手工递归删除，不使用跳过父进程等待的测试参数。先试点，异常立即停，回执不可覆盖。此专用入口不扩展通用 roots，不改变 T/0 或浏览器资料的保护规则。

源码依据：[Chromium 管理器说明](https://chromium.googlesource.com/chromium/src/+/main/chrome/browser/mac/code_sign_clone_manager.h)、[创建与退出清理实现](https://chromium.googlesource.com/chromium/src/+/main/chrome/browser/mac/code_sign_clone_manager.mm)。源码行为仍须与本机版本、签名、实际执行结果核对。
