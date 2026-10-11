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
                if fn.startswith("rollout-") and fn.endswith(".jsonl"):
                    m = UUID_RE.search(fn)
                    if m:
                        out[m.group(1).lower()] = os.path.join(dirpath, fn)
    return out


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
