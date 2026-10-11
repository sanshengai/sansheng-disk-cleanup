# Android：SDK、系统镜像与模拟器

适用于装过 Android Studio 或命令行 SDK 的机器。大头通常是 `system-images`（单个 1–3GB）和 AVD 的用户数据盘与快照。

## 存储位置

默认位置如下，实际位置可被环境变量改写（`ANDROID_HOME`、`ANDROID_SDK_ROOT`（已弃用）、`ANDROID_USER_HOME`、`ANDROID_AVD_HOME`、`ANDROID_EMULATOR_HOME`），先看变量再看默认路径。官方说明：https://developer.android.com/tools/variables

| 路径 | 内容 | 性质 |
|---|---|---|
| `~/Library/Android/sdk/system-images/android-<API>/<变体>/<ABI>` | 模拟器系统镜像（变体如 google_apis、google_apis_playstore、android-tv、wear） | 可再生缓存（可用 sdkmanager 重新下载，要联网） |
| `~/Library/Android/sdk/platforms`、`build-tools`、`ndk`、`cmake` | 编译用平台、构建工具、NDK | 可再生缓存（但被现役工程引用时删了会构建失败） |
| `~/Library/Android/sdk/emulator`、`platform-tools` | 模拟器本体、adb | 工具状态 |
| `~/.android/avd/<名称>.avd/` | 单个虚拟设备：`config.ini`、`userdata-qemu.img`、`sdcard.img`、`snapshots/` | 用户独有成果（里面可能有测试装的 App 与数据） |
| `~/.android/avd/<名称>.ini` | AVD 指针文件，指向 `.avd` 目录 | 工具状态 |
| `~/.android/avd/<名称>.avd/snapshots/` | 快速启动与手动快照 | 可再生缓存（删后冷启动一次） |
| `~/.android/cache`、`~/.android/adbkey` | 下载缓存；adb 密钥 | 缓存可再生；密钥不要删 |
| 机器上的第二套 SDK（例如命令行装在 `~/.local/share/android-sdk`） | 与 Android Studio 那套并存 | 见下文判断 |

## 怎么判断还在用

- 镜像是否被引用：读每个 `~/.android/avd/*/config.ini` 的 `image.sysdir.1`（形如 `system-images/android-36/google_apis_playstore/arm64-v8a/`，相对 SDK 根）。没有任何 AVD 引用的镜像才是候选。探针：`scan.py probe android` 已按此列出各 SDK 根的镜像及被引用情况。
- 两套 SDK 并存时不能只看哪套大：同一个 API 号可能两套都有。对每个镜像检查"引用它的 AVD 实际落在哪套 SDK 根"。AVD 的 `config.ini` 里路径是相对的，实际落点由启动模拟器时的 SDK 根决定；用 `echo $ANDROID_HOME`、Android Studio 的 SDK Location、`ps` 里 emulator 进程的路径交叉确认。无法确定归属时，两套都保留并问用户。
- 工程是否还要某个 `platforms`/`build-tools`：在用户常用工程里搜 `compileSdk`、`buildToolsVersion`、`ndkVersion`；有引用就不删。
- AVD 是否在用：`pgrep -fl qemu-system|emulator`；`snapshots/` 或 `*.lock` 的 mtime 在 24 小时内就当进行中。
- 用户明确说"不再做电视/穿戴/某 API 测试"可作为删除对应变体镜像的强信号，仍要先过引用检查。

只读命令速查：

```
scan.py probe android                      # 各 SDK 根的镜像、被引用情况、AVD 体积与快照体积
grep -H image.sysdir ~/.android/avd/*/config.ini
du -sh ~/.android/avd/*.avd/snapshots 2>/dev/null
sdkmanager --list_installed
avdmanager list avd
```

提示：同一 API 号在两套 SDK 都装了，而现役 AVD 只引用其中一套时，另一套的同名镜像是更稳的候选；但要先确认没有 CI 或脚本显式使用那套 SDK。

- 释放量预期：每个镜像卸载后约释放其目录大小；AVD 删除后连同其快照一并释放。报告里分开写，不合并。
- 删除后复核：再跑 `probe android`，确认没有 AVD 的 `image.sysdir` 指向已卸载镜像，否则该 AVD 会无法启动。

## 原生清理与保留设置

- 列出已装：`sdkmanager --list_installed`（本机 `--help` 已核对 `--uninstall`、`--list_installed`）。
- 卸载镜像：`sdkmanager --uninstall "system-images;android-31;android-tv;arm64-v8a"`。新版会提示 `sdkmanager` 已弃用，替代为 Android CLI（`android sdk`，见 https://d.android.com/tools/agents/android-cli）；命令名以本机实际输出为准。
- 删除虚拟设备：`avdmanager delete avd -n <名称>`；列出：`avdmanager list avd`。
- 只清快照：Android Studio 的 Device Manager 里对设备选 Wipe Data / Cold Boot；或删除该 AVD 的 `snapshots/` 目录（先关模拟器）。
- 官方 SDK Manager 文档：https://developer.android.com/tools/sdkmanager ；avdmanager：https://developer.android.com/tools/avdmanager

## 不能做的事

- 不要直接 `rm -rf` SDK 子目录而不走 sdkmanager：SDK 有安装记录（`package.xml`），手删后 sdkmanager 状态与磁盘不一致，且可能破坏许可与依赖。
- 不要只凭"这套 SDK 更大"就删整套：Android Studio 的 SDK Location 指向哪套，Gradle 的 `local.properties` 的 `sdk.dir` 指向哪套，都可能与 shell 变量不同。
- 不要删 `~/.android/avd/*/config.ini` 与 `*.ini` 指针文件，否则 Studio 会认为设备不存在而磁盘数据变成孤儿。
- 不要删 `~/.android/adbkey`：删后所有已授权的真机需要重新点"允许 USB 调试"。
- 卸载镜像后空间不涨：先查 APFS 快照与废纸篓（见 macos-system 卡）。

## 给用户的一句话问题

"这几个 Android 系统镜像（<列出 API/变体>）没有任何现存模拟器在用，卸载后需要时可重新下载约 N GB；你还会做 <电视/穿戴/旧版本> 的测试吗？"
