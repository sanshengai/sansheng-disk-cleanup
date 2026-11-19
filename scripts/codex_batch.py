"""Exact-scope opt-in for native Codex retirement. All database access is read-only."""
import hashlib
import json
import math
import os
import stat
from contextlib import closing
from functools import lru_cache
from pathlib import Path

import codex_state as cs


@lru_cache(maxsize=2048)
def _compressed_ids(path, fingerprint):
    from compression import zstd
    with zstd.open(path, "rb") as stream:
        header = stream.readline(cs.HEAD_BYTES + 1)
    if len(header) > cs.HEAD_BYTES:
        raise ValueError("压缩会话首行超出可核对范围")
    record = json.loads(header)
    if record["type"] != "session_meta":
        raise ValueError("首行缺少 session_meta")
    meta = record["payload"]
    source = meta.get("source")
    spawn = source.get("subagent", {}) if isinstance(source, dict) else {}
    spawn = spawn.get("thread_spawn", {}) if isinstance(spawn, dict) else {}
    parent = meta.get("parent_thread_id") or spawn.get("parent_thread_id")
    return meta["id"].lower(), parent.lower() if parent else None


def compressed_ids(path):
    info = path.lstat()
    if path != path.resolve(strict=True) or not stat.S_ISREG(info.st_mode):
        raise ValueError("压缩会话路径不是普通文件或经过软链接")
    return _compressed_ids(path, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))


def compressed_error(path, tid, home):
    try:
        path = Path(path)
        if compressed_ids(path)[0] != tid:
            return "压缩会话正文身份与命令不一致"
        rows, errors = cs.read_threads(home, tid)
        mine = rows.get(tid, [])
        if errors or len(mine) != 1 or mine[0]["rollout_path"] not in (str(path), str(path)[:-4]):
            return "压缩会话与状态库里的唯一路径不一致"
        if path.with_suffix("").exists():
            return "压缩会话的未压缩文件同时存在"
    except Exception as exc:
        return f"无法核对压缩会话身份：{exc}"
    return None


def remaining(home, tid):
    """Require each expected store and table to be readable, including empty turns."""
    result = {}
    for prefix, table, key in (("state_", "threads", "id"), ("thread_history_", "thread_turns", "thread_id"),
                               ("thread_history_", "thread_items", "thread_id")):
        dbs = cs._dbs(home, prefix)
        if not dbs:
            raise ValueError(f"找不到 {prefix} 数据库")
        result[table] = 0
        for db in dbs:
            with closing(cs._ro_connect(db)) as conn:
                result[table] += conn.execute(f"SELECT count(*) FROM {table} WHERE {key}=?", (tid,)).fetchone()[0]
    return result


def batch_error(batch, item, tid, home, now):
    """guard(tid, cutoff) must freshly verify runtime, references, memory and media receipts."""
    try:
        path, digest, guard = batch
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            return "授权封存摘要已变化"
        seal = json.loads(raw)
        if seal["schema"] != 1 or not isinstance(seal["authorization"], str) or not seal["authorization"].strip():
            return "缺少明确的批次授权"
        if seal["items"].get(tid) != item:
            return "条目与授权封存不一致"
        cutoff = seal["cutoff"]
        if isinstance(cutoff, bool) or not math.isfinite(cutoff) or cutoff > now - 72 * 3600:
            return "批次必须保留最近 72 小时"
        why = guard(tid, cutoff)
        if not isinstance(why, list) or why:
            return f"现场运行、引用、记忆或素材保护检查未通过：{why}"
        current, errors = cs.read_threads(home, tid)
        mine = current.get(tid, [])
        if errors or len(mine) != 1:
            return "无法唯一核对线程状态"
        updated = cs.to_epoch(mine[0].get("updated_at"))
        if updated is None or not math.isfinite(updated) or updated >= cutoff:
            return "状态库最近活动仍在保护范围"
        if os.path.lexists(item["path"]) and os.lstat(item["path"]).st_mtime >= cutoff:
            return "会话文件最近活动仍在保护范围"
        relations = cs.relations(home, refresh=True)
        edges, errors = cs.read_edges(home)
        if errors:
            return errors[0]
        for p, children in relations["children"].items():
            edges.setdefault(p, set()).update(children)
        for other, rollout in relations["rollout"].items():
            if rollout.endswith(".jsonl.zst") and os.path.lexists(rollout):
                other_id, parent = compressed_ids(Path(rollout))
                if other != other_id:
                    return "会话图中的压缩文件身份不一致"
                if parent:
                    edges.setdefault(parent, set()).add(other_id)
        for db in cs._dbs(home, "state_"):
            with closing(cs._ro_connect(db)) as conn:
                for p, c in conn.execute("SELECT attached_thread_parent_id,id FROM threads WHERE attached_thread_parent_id IS NOT NULL"):
                    edges.setdefault(p, set()).add(c)
                if conn.execute("SELECT 1 FROM threads WHERE id=? AND attached_thread_parent_id IS NOT NULL", (tid,)).fetchone():
                    return "线程仍有附挂关系"
        for child in cs.descendants(tid, edges):
            path = relations["rollout"].get(child)
            if (path and os.path.lexists(path)) or any(remaining(home, child).values()):
                return "仍有子孙会话或数据库记录"
        turns = []
        for db in cs._dbs(home, "thread_history_"):
            with closing(cs._ro_connect(db)) as conn:
                turns.extend(conn.execute("SELECT status, started_at, completed_at FROM thread_turns WHERE thread_id=? "
                                          "ORDER BY started_at DESC, rollout_ordinal DESC LIMIT 1", (tid,)))
        if len(turns) != 1:
            return "无法唯一核对最后一轮"
        status, start, end = turns[0]
        times = [cs.to_epoch(x) for x in (start, end)]
        if status not in ("completed", "failed", "interrupted") or any(x is None or not math.isfinite(x) or x >= cutoff for x in times):
            return "最后一轮未结束或时间仍在保护范围"
    except Exception as exc:
        return f"批次现场复核失败：{exc}"
    return None
