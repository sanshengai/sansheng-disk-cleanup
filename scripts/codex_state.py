"""Codex 会话之间的父子关系（scan / plan / act 共用，只读）。

两个来源取并集：
- `state_*.sqlite` 的 `thread_spawn_edges`（父 → 子）；
- 每个 rollout 首行 `session_meta` 里的父线程（`parent_thread_id`，或 source.subagent.thread_spawn.parent_thread_id）。

`codex delete` 会连带删除全部后代，所以删一个线程前必须知道它的整棵子树。
只用标准库；表结构认不出就当作没有这一来源，不报错。
"""
import os
import re
import sqlite3
import json
from pathlib import Path

UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
PARENT_RE = re.compile(rb'"parent_thread_id"\s*:\s*"([0-9a-fA-F-]{36})"')
HEAD_BYTES = 256 * 1024

_cache = {}


def _cols(con, table):
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _db_relations(base):
    children, rollout_of = {}, {}
    try:
        names = sorted(n for n in os.listdir(base) if n.startswith("state_") and n.endswith(".sqlite"))
    except OSError:
        names = []
    for n in names:
        try:
            con = sqlite3.connect(f"file:{os.path.join(base, n)}?mode=ro", uri=True, timeout=5)
            if {"parent_thread_id", "child_thread_id"} <= _cols(con, "thread_spawn_edges"):
                for p, c in con.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"):
                    if p and c:
                        children.setdefault(str(p).lower(), set()).add(str(c).lower())
            if {"id", "rollout_path"} <= _cols(con, "threads"):
                for t, rp in con.execute("SELECT id, rollout_path FROM threads"):
                    if t and rp:
                        rollout_of[str(t).lower()] = rp
            con.close()
        except sqlite3.Error:
            continue
    return children, rollout_of


def meta_parent(path):
    """rollout 首行里的父线程号；读不到返回 None。只读前 256KB。"""
    if str(path).endswith(".zst"):
        return None  # 压缩会话从 state DB 取关系，不把压缩字节当 JSON。
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_BYTES)
    except OSError:
        return None
    first = head.split(b"\n", 1)[0]
    m = PARENT_RE.search(first)
    return m.group(1).decode().lower() if m else None


def scan_rollouts(base):
    """{线程号: rollout 路径}，覆盖 sessions 与 archived_sessions。"""
    out = {}
    for sub in ("sessions", "archived_sessions"):
        for dirpath, _, filenames in os.walk(os.path.join(base, sub)):
            for fn in filenames:
                if fn.startswith("rollout-") and fn.endswith((".jsonl", ".jsonl.zst")):
                    m = UUID_RE.search(fn)
                    if m:
                        out[m.group(1).lower()] = os.path.join(dirpath, fn)
    return out


def thread_metadata(base):
    """只读最小元信息；错误明确返回，不读提示词和消息正文。"""
    out, errors = {}, []
    for db in sorted(Path(base).glob("state_*.sqlite")):
        try:
            con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)
            try:
                cols = _cols(con, "threads")
                if "id" not in cols:
                    raise ValueError("threads 缺少 id")
                fields = [k for k in ("id", "cwd", "source", "originator", "cli_version", "archived", "updated_at") if k in cols]
                for row in con.execute("SELECT " + ",".join(fields) + " FROM threads"):
                    d = dict(zip(fields, row))
                    if isinstance(d.get("source"), str):
                        try:
                            d["source"] = json.loads(d["source"])
                        except ValueError:
                            pass
                    d["metadata_status"] = "state_db"
                    out[str(d["id"]).lower()] = d
            finally:
                con.close()
        except (sqlite3.Error, OSError, ValueError) as exc:
            errors.append({"path": str(db), "error": str(exc)})
    return out, errors


def compressed_metadata(path):
    p = Path(path)
    base = next((a.parent for a in p.parents if a.name in ("sessions", "archived_sessions")), None)
    m = UUID_RE.search(p.name)
    if base is None or m is None:
        return {"metadata_status": "unknown"}
    # 以数据库和 WAL 指纹失效，避免同一批上千会话重复查库。
    stamp = tuple((str(f), f.stat().st_size, f.stat().st_mtime_ns)
                  for f in sorted(base.glob("state_*.sqlite*")) if f.is_file())
    key = (str(base), stamp)
    if key not in _metadata_cache:
        _metadata_cache.clear()
        _metadata_cache[key] = thread_metadata(base)
    rows, errors = _metadata_cache[key]
    d = dict(rows.get(m.group(1).lower(), {"metadata_status": "unknown"}))
    if errors:
        d["metadata_errors"] = errors
    return d


_metadata_cache = {}


def latest_turns(base):
    """最后一轮状态只作活动线索，绝不等同整个任务结束。"""
    rows, errors = {}, []
    def order(value):
        return (0, float(value)) if isinstance(value, (int, float)) else (1, str(value or ""))
    for db in sorted(Path(base).glob("thread_history_*.sqlite")):
        try:
            con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)
            try:
                required = {"thread_id", "status", "started_at", "completed_at"}
                if not required <= _cols(con, "thread_turns"):
                    raise ValueError("无法识别 thread_turns 结构")
                for tid, status, start, end in con.execute(
                        "SELECT thread_id,status,started_at,completed_at FROM thread_turns"):
                    d = {"status": status, "started_at": start, "completed_at": end}
                    if tid not in rows or order(start) > order(rows[tid].get("started_at")):
                        rows[tid] = d
            finally:
                con.close()
        except (sqlite3.Error, OSError, ValueError) as exc:
            errors.append({"path": str(db), "error": str(exc)})
    return rows, errors


def relations(home, refresh=False):
    """返回 {"children": {父: set(子)}, "parent": {子: 父}, "rollout": {线程: 路径}}。按 home 缓存。"""
    key = os.path.realpath(home)
    if key in _cache and not refresh:
        return _cache[key]
    base = os.path.join(home, ".codex")
    children, rollout_db = _db_relations(base)
    rollout = scan_rollouts(base)
    for tid, path in rollout.items():
        p = meta_parent(path)
        if p and p != tid:
            children.setdefault(p, set()).add(tid)
    for tid, path in rollout_db.items():
        rollout.setdefault(tid, path)
    parent = {}
    for p, cs in children.items():
        for c in cs:
            parent.setdefault(c, p)
    rel = {"children": children, "parent": parent, "rollout": rollout}
    _cache[key] = rel
    return rel


def descendants(tid, children):
    """深度优先，子孙在前、父在后（即安全的删除顺序）；有环也不会死循环。"""
    out, seen = [], {tid}

    def walk(t):
        for c in sorted(children.get(t, ())):
            if c in seen:
                continue
            seen.add(c)
            walk(c)
            out.append(c)

    walk(tid)
    return out
