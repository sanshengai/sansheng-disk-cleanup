# 会话里的素材如何保留

清理 Claude 的 `~/.claude/projects/` 和 `~/.claude/history.jsonl`，或 Codex 的 `history.jsonl`、`sessions/`、`archived_sessions/` 前，先逐行读取会话。扫描包括 Claude 消息、工具结果、粘贴记录、Base64 附件，以及 Codex 的用户输入、生成图和工具返回。普通工作目录里的 JSONL 不按会话处理。项目里的外部文件路径引用不会删除对应项目文件。

Codex 的 `codex delete` 还会处理数据库记录。执行器同时检查目标及可能连带删除的后代的 `thread_items`、`thread_realtime_items`、状态库附件、按线程分目录的 `generated_images` 与 `attachments`。直接删除整个活动历史库或其 WAL 会被拦下。执行器对旧普通会话文件、压缩文件及只有库内记录的线程使用相同的素材保留条件。

可以对已知路径单独只读预检。返回码 0 表示本项未发现需要拦截的素材，1 表示保留。该命令不检查会话是否已结束，也不产生删除授权。

```sh
python3 scripts/session_materials.py ~/.claude/projects/某个项目/某个会话.jsonl
python3 scripts/session_materials.py --thread 线程UUID
```

若有需保留的素材，先将原始字节留在独立、可持续使用的位置。清单为 UTF-8 JSON。下例的键需替换为实际的 64 位小写 SHA-256。

```json
{"copies":{"替换为素材原始字节的SHA256":"/绝对路径/保留素材/原图.png"}}
```

在原有清理条目上加以下字段。清单摘要使用清单文件本身的完整字节计算。验证时从待删除的原始数据解码，再检查与此摘要对应的实际副本。原样保留整个会话文件也可证明其中的内嵌内容已完整保留，此时映射使用会话文件本身的 SHA-256。SQLite 的普通历史条目也可保留完整 `item_json` 原始字节。独立附件引用须核对附件本身。副本必须在原生会话目录和本批删除目标以外。

```json
{"session_materials":{"path":"/绝对路径/保留素材/清单.json","sha256":"替换为清单文件SHA256"}}
```

适用于 `quick_act.py` 的清单、封存执行、隔离到期，以及 `plan.py` / `act.py` 的删除条目与本轮废纸篓最终清除。归档处置沿用原有完整复制校验。已使用 `batch=(seal_path, digest, guard)` 的 Python 调用方继续使用该签名，guard 仍负责现场活动和项目状态。若已存有逐项素材映射，按上面的格式汇集其中的 SHA-256 和保留路径，先将证明写进目标 item，再生成批次 seal。仅有用户授权或回调返回空列表不能证明某张图片已有副本。

预检限制单条 32 MiB、单次扫描 256 MiB、10 万条和 30 秒。超限、读取错误、格式无法解析、证明变化或副本失效均会保留；可缩小扫描范围，或先保留并核验完整会话。读取压缩会话需当前 Python 提供 `compression.zstd`。这些检查不提取原图，也不把聊天正文写入预检输出。
