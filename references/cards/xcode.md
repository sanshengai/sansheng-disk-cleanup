# Xcode 与 iOS 模拟器

适用于装过 Xcode 的机器。注意区分可重建的编译产物和不可重建的归档（Archives）。

## 存储位置

| 路径 | 内容 | 性质 |
|---|---|---|
| `~/Library/Developer/Xcode/DerivedData` | 编译中间产物、索引、预览缓存，每个工程一个子目录 | 可再生缓存（下次编译变慢，需要时重建） |
| `~/Library/Developer/Xcode/Archives` | 按日期分目录的 `.xcarchive`，内含已发布二进制和 dSYM | 用户独有成果（dSYM 用于还原崩溃堆栈，丢了可能无法再符号化旧版本） |
| `~/Library/Developer/Xcode/iOS DeviceSupport` | 连接过的真机的系统符号，按系统版本分目录 | 可再生缓存（再连该版本设备会重新拷贝，耗时几分钟） |
| 同级 `watchOS DeviceSupport`、`tvOS DeviceSupport` 等 | 同上，对应其他平台 | 可再生缓存（逐项核实未做） |
| `~/Library/Developer/CoreSimulator/Devices/<UDID>` | 每个模拟器设备的整盘数据：已装 App、沙盒文档、照片 | 用户独有成果（里面可能有测试数据）；设备本身可重建 |
| `~/Library/Developer/CoreSimulator/Caches` | 模拟器缓存 | 可再生缓存 |
| 模拟器运行时（Runtime） | iOS/watchOS 等系统镜像，Xcode 15+ 以磁盘映像方式存放，路径随 Xcode 版本变化（未核实具体路径，以 `xcrun simctl runtime list` 为准） | 可再生缓存（可重新下载，GB 级） |
| `~/Library/Developer/Xcode/UserData`（含 Previews）、`~/Library/Caches/com.apple.dt.Xcode` | 预览、文档、索引缓存 | 可再生缓存 |
| `~/Library/Developer/Xcode/UserData/Provisioning Profiles` | 描述文件 | 工具状态（不删） |

## 怎么判断还在用

- Xcode 或 `xcodebuild`、`Simulator` 进程在运行时不动 DerivedData、CoreSimulator：`pgrep -fl "Xcode|xcodebuild|Simulator|CoreSimulator"`。
- DerivedData：每个子目录名是 `<工程名>-<哈希>`，目录内 `info.plist` 的 `WorkspacePath` 记着源工程路径；该路径已不存在的子目录是孤儿，最稳的清理对象。工程仍在时删了只是下次全量编译。
- Archives：读每个 `.xcarchive` 的 `Info.plist` 中 `Distributions[].uploadEvent.state`（或类似字段），上传成功的归档才可列为候选；未上传、读不到该字段的一律保留并标注。另核对 App Store Connect/发布记录里是否已上线该版本；已上线且 dSYM 已另行上传或保存也算有副本，但要用户确认。未发布的归档当用户成果保留。归档里 `dSYMs/` 与 `Info.plist` 的 `ApplicationProperties` 能看版本号。
- DeviceSupport（iOS/watchOS/tvOS DeviceSupport）：按系统版本命名，每个平台按版本保留最近 2 个，其余才可列为候选；检测到 Xcode 进程运行时全部保留。
- 模拟器设备：`xcrun simctl list devices` 看是否 `unavailable`（运行时已删）；`xcrun simctl list runtimes` 看哪些运行时没有任何设备在用。
- 工程引用检查：Archives、DerivedData 路径可能被 CI 脚本或自定义 Xcode 设置引用（Settings → Locations 可自定义这两个目录），先看用户是否改过位置。

只读命令速查：

```
du -sh ~/Library/Developer/Xcode/DerivedData/* | sort -h | tail
defaults read ~/Library/Developer/Xcode/DerivedData/<目录>/info.plist WorkspacePath
du -sh ~/Library/Developer/Xcode/Archives/* ~/Library/Developer/Xcode/iOS\ DeviceSupport/*
xcrun simctl list devices
xcrun simctl list runtimes
```

没装 Xcode 或 Command Line Tools 时，`xcrun simctl` 会报找不到工具，此时不要去安装，只报告路径不存在。

- 释放量预期：DerivedData 与过期运行时释放最直接；Archives 和模拟器设备的体积要逐项向用户说明用途。
- 删除后复核：重新打开工程能正常编译（首次较慢）；`xcrun simctl list` 不再出现 unavailable 设备。

## 原生清理与保留设置

- 清掉不可用的模拟器：`xcrun simctl delete unavailable`。
- 删除指定模拟器：`xcrun simctl delete <UDID>`。
- 运行时：`xcrun simctl runtime list`、`xcrun simctl runtime delete <id>`（Xcode 15 起可用；是否支持按"多少天未使用"批量删，以本机 `xcrun simctl runtime delete --help` 为准，未核实）。
- 图形入口：Xcode → Settings → Components（旧版叫 Platforms）可删除下载的平台与运行时；Window → Devices and Simulators 可删设备。
- DerivedData 可在 Xcode → Settings → Locations 点箭头定位，或直接按子目录删（先关 Xcode）。
- 官方：https://developer.apple.com/documentation/xcode/downloading-and-installing-additional-xcode-components 。simctl 命令说明以 `xcrun simctl help` 为准。

## 不能做的事

- 不要整体删除 `Archives`：dSYM 丢失后，线上崩溃日志无法还原到源码行。
- 不要手删 `/Library/Developer/CoreSimulator` 或 `CoreSimulator/Devices` 下的单个 UDID 目录而不走 simctl，会留下设备注册表的孤儿项，`simctl list` 与实际不一致。
- 不要删 `Provisioning Profiles`、`Xcode/UserData/` 下的快捷键与片段（`KeyBindings`、`CodeSnippets`），这是用户配置。
- 不要在 Xcode 或模拟器运行时删 DerivedData/CoreSimulator，会让编译中途失败甚至设备损坏。
- 删了空间不涨：先查 APFS 本地快照（见 macos-system 卡）。

## 给用户的一句话问题

"Archives 里这些 <N> 个归档（<应用名/版本范围>）对应的版本都已经上线、崩溃符号也另有备份吗？可以删的话我只动这些，DerivedData 和过期模拟器运行时我直接按原生命令清。"
