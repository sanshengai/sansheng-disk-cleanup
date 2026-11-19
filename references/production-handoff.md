# 生产交付清单与清理回执

生产者可在**单个任务根目录**保存 `production-handoff.json`。路径相对此文件所在目录，填写确切文件或目录，目录条目覆盖其子树。清单与阶段验收证据均应保留。已有消费者的过程件注明消费者，待核实的字段可留缺。每个候选接受本机原有保护、备份、进程、引用和执行复核。

```json
{
  "schema": "production-handoff/1",
  "entries": [
    {"path": "source/input.mov", "role": "source", "reason": "留作后续剪辑的原始素材"},
    {"path": "release/movie.mp4", "role": "delivery", "reason": "本次交付"},
    {"path": "accepted.json", "role": "evidence", "reason": "验收结果及重做依据"},
    {"path": "render/old", "role": "intermediate", "consumers": [], "evidence": ["accepted.json"], "reason": "旧渲染阶段已完成"}
  ]
}
```

`triage.py` 默认发现 `work_root/production-handoff.json` 以及 `production_globs` 每个目录直属的同名文件。本机 `triage-local.json` 可设 `production_manifest_globs`，例如 `["projects/*/production-handoff.json"]`。清单覆盖的目录内，未列入待复核过程件的路径继续保留。存在的成品、已登记原料、非空证据文件、显式空消费者列表是列为候选的必要条件。证据内容和消费者枚举的真实性还需由生产者核对。

```sh
python3 scripts/triage.py --production-manifest /path/to/project/production-handoff.json
python3 scripts/quick_act.py plan --production-manifest /path/to/project/production-handoff.json --items /path/to/items.json --user-text '用户当轮授权原话' --run /path/to/run
python3 scripts/quick_act.py run --run /path/to/run --seal 封存码 --pilot
```

两个发现入口均可重复指定清单。清单仅补充判断依据，执行仍使用原有 items、授权与封存流程。执行前和逐目标复核时读取清单，清单内容变化须重新生成计划。登记的交付或证据消失会保留对应候选。清单外的其他项目沿用原有规则。

每次 `run` 在 `quick-space.jsonl` 追加每项操作的开始和结束记录。完成回执列出成功删除条目的估算字节、目标卷的可用字节前后值、APFS 容器余量前后值及有符号差额。隔离仅改名，条目释放估算为零。APFS 克隆、快照和并发写入会影响观察，单项可归因净释放填 `null`。容器不可用、设备变化时相应差额为 `null`。同容器的多卷观察不可相加。原 `quick-receipt.jsonl` 保留动作、跳过依据、试点与续跑历史。原 `free_delta` 字段表示本轮 HOME 所在卷的余量变化。
