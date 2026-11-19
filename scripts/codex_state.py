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


# ----------------------------------------------------------------------------
# 会话文件已不在、状态库与历史库里还有记录的线程（条目 identity.kind = codex_thread_without_rollout）
#
# plan.py check、act.py 执行前复核、scan.py probe codex-history 共用下面这些只读函数。
# 全部用 mode=ro 打开、只做短查询，不写库；读不出来一律返回错误说明，由调用方跳过或降级，不当作“没有记录”。
# ----------------------------------------------------------------------------

IDENTITY_KIND = "codex_thread_without_rollout"
IDENTITY_KEYS = ("kind", "thread_id", "rollout_path", "state_updated_at", "history_items", "history_bytes")
NO_ROLLOUT_DIR = ".no-rollout"
RECENT_SECONDS = 24 * 3600
_ROLLOUT_NAME = r"rollout-.+-{}\.jsonl"


def is_identity_item(item):
    """条目带有本类 identity（只看 kind，字段是否合格另由 identity_error 判断）。"""
    return (isinstance(item, dict) and isinstance(item.get("identity"), dict)
            and item["identity"].get("kind") == IDENTITY_KIND)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def identity_error(idn):
    """identity 的结构校验：返回错误说明或 None。多余的键也拒绝（不让别的字段夹带进封存）。"""
    if not isinstance(idn, dict) or idn.get("kind") != IDENTITY_KIND:
        return "identity.kind 不是 " + IDENTITY_KIND
    extra = sorted(set(idn) - set(IDENTITY_KEYS))
    if extra:
        return "identity 含未登记的字段：" + "、".join(str(k) for k in extra[:5])
    missing = [k for k in IDENTITY_KEYS if k not in idn]
    if missing:
        return "identity 缺少字段：" + "、".join(missing)
    if not isinstance(idn["thread_id"], str) or not UUID_RE.fullmatch(idn["thread_id"]):
        return "identity.thread_id 必须是完整线程 UUID"
    if not isinstance(idn["rollout_path"], str):
        return "identity.rollout_path 必须是字符串（状态库里没有记录时写空串）"
    if not (idn["state_updated_at"] is None or _is_int(idn["state_updated_at"])
            or isinstance(idn["state_updated_at"], (float, str))):
        return "identity.state_updated_at 必须是数字、字符串或 null"
    for k in ("history_items", "history_bytes"):
        if not _is_int(idn[k]) or idn[k] < 0:
            return f"identity.{k} 必须是不小于 0 的整数"
    return None


def _within(path, base):
    return path == base or path.startswith(base.rstrip("/") + "/")


def expected_item_path(home, tid, rollout_path):
    """这类条目的 path 只有一种合法写法：状态库记录的路径本身（在 ~/.codex/sessions 或 archived_sessions 下、
    文件名是 rollout-…-<UUID>.jsonl 时）；记录为空、或不在这两处时用占位路径 ~/.codex/sessions/.no-rollout/<UUID>。
    path 由 identity 推出，条目自己写别的位置一律不接受（防止“核对的是 A、删的是 B”）。"""
    base = os.path.join(home, ".codex")
    rp = rollout_path or ""
    if rp and os.path.isabs(rp):
        norm = os.path.normpath(rp)
        if (any(_within(norm, os.path.join(base, d)) for d in ("sessions", "archived_sessions"))
                and re.fullmatch(_ROLLOUT_NAME.format(re.escape(tid.lower())), os.path.basename(norm), re.I)):
            return norm
    return os.path.join(base, "sessions", NO_ROLLOUT_DIR, tid.lower())


def to_epoch(value):
    """状态库里的时间 → 秒（整数/浮点按秒或毫秒，数字字符串、ISO 字符串也认）；认不出返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        s = value.strip()
        try:
            value = float(s)
        except ValueError:
            try:
                from datetime import datetime
                return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    return None


def iso_of(value):
    """给人看的时间（北京时间以外的显示由调用方决定，这里统一 UTC ISO）；认不出返回原值的字符串。"""
    ts = to_epoch(value)
    if ts is None:
        return None if value is None else str(value)
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tables(con):
    """库里的表名。与 _cols 不同：库文件损坏 / 不是数据库时抛 sqlite3.Error，不会被当成“没有这张表”。"""
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _ro_connect(db):
    return sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True, timeout=5)


def _dbs(home, prefix):
    base = os.path.join(home, ".codex")
    try:
        return [os.path.join(base, n) for n in sorted(os.listdir(base))
                if n.startswith(prefix) and n.endswith(".sqlite")]
    except OSError:
        return []


def read_threads(home, tid=None):
    """只读状态库 threads 表。返回 ({线程号: [行字典（每个状态库一行）]}, [错误说明])。
    行字典：id、rollout_path（None 记为空串）、source、cwd、title、first_user_message（前 200 字）、created_at、
    updated_at、archived、db（来自哪个库）。tid 给出时只留这一个线程。"""
    rows, errors = {}, []
    for db in _dbs(home, "state_"):
        try:
            con = _ro_connect(db)
            try:
                cols = _cols(con, "threads")
                if not {"id", "rollout_path"} <= cols:
                    raise ValueError("threads 表缺少 id 或 rollout_path")
                want = ["id", "rollout_path", "source", "cwd", "title", "created_at", "updated_at", "archived"]
                fields = [k for k in want if k in cols]
                exprs = list(fields)
                if "first_user_message" in cols:
                    fields.append("first_user_message")
                    exprs.append("substr(first_user_message, 1, 200)")
                for row in con.execute("SELECT " + ",".join(exprs) + " FROM threads"):
                    d = dict(zip(fields, row))
                    d["id"] = str(d["id"]).lower()
                    if tid is not None and d["id"] != tid.lower():
                        continue
                    d["rollout_path"] = d.get("rollout_path") or ""
                    d["db"] = db
                    rows.setdefault(d["id"], []).append(d)
            finally:
                con.close()
        except (sqlite3.Error, OSError, ValueError) as exc:
            errors.append(f"{os.path.basename(db)}：{exc}")
    if not _dbs(home, "state_"):
        errors.append("没有找到状态库（~/.codex/state_*.sqlite）")
    return rows, errors


def read_edges(home):
    """只读状态库里的父子边。返回 ({父: set(子)}, [错误说明])；表不存在按“没有边”，库读不出来才报错。"""
    children, errors = {}, []
    for db in _dbs(home, "state_"):
        try:
            con = _ro_connect(db)
            try:
                if "thread_spawn_edges" in _tables(con) and {"parent_thread_id", "child_thread_id"} <= _cols(con, "thread_spawn_edges"):
                    for p, c in con.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"):
                        if p and c:
                            children.setdefault(str(p).lower(), set()).add(str(c).lower())
            finally:
                con.close()
        except (sqlite3.Error, OSError) as exc:
            errors.append(f"{os.path.basename(db)}：{exc}")
    return children, errors


def _id_params(tids):
    out = []
    for t in tids:
        out.extend((t.lower(), t.upper()))
    return out


HISTORY_BATCH = 40


def history_counts(home, tids, with_bytes=False, batch=HISTORY_BATCH):
    """只读历史库 thread_items：返回 ({线程号: (条目数, 字节数或 None)}, [错误说明])。
    按线程号走 (thread_id, rollout_ordinal) 索引的短查询，每 batch 个线程开一次连接、查完即关
    （不整库扫描，不长时间占着读事务把 WAL 撑大）。
    没有任何 thread_history 库时全部记 0 条（没有记录可删）；有库但表结构认不出来、读不出来才报错。"""
    tids = [t.lower() for t in dict.fromkeys(tids)]
    out = {t: (0, 0 if with_bytes else None) for t in tids}
    errors = []
    sql = ("SELECT COUNT(*), SUM(LENGTH(CAST(item_json AS BLOB))) FROM thread_items WHERE thread_id IN (?, ?)"
           if with_bytes else "SELECT COUNT(*), 0 FROM thread_items WHERE thread_id IN (?, ?)")
    for db in _dbs(home, "thread_history_"):
        try:
            con = _ro_connect(db)
            try:
                if "thread_items" not in _tables(con):    # 库损坏会在这里抛错，不当成“没有这张表”
                    continue                          # 这个库里没有 thread_items 表：该库没有可删记录
                if not {"thread_id", "item_json"} <= _cols(con, "thread_items"):
                    raise ValueError("thread_items 表缺少 thread_id 或 item_json")
            finally:
                con.close()
            for i in range(0, len(tids), max(1, batch)):
                con = _ro_connect(db)
                try:
                    for t in tids[i:i + batch]:
                        n, b = con.execute(sql, (t, t.upper())).fetchone()
                        n0, b0 = out[t]
                        out[t] = (n0 + (n or 0), (b0 + (b or 0)) if with_bytes else None)
                finally:
                    con.close()
        except (sqlite3.Error, OSError, ValueError) as exc:
            errors.append(f"{os.path.basename(db)}：{exc}")
    return out, errors


def is_exec_source(source):
    """codex exec 脚本会话：状态库 source 精确等于 "exec"（批量组只认这一种）。
    不用子串匹配：子任务的 JSON source 里 agent_path 可能含 exec 字样，"exec-xxx" 之类也不是 exec。"""
    return isinstance(source, str) and source == "exec"


def is_json_source(source):
    """source 是 JSON 对象（子任务 / 子 Agent 的来源描述）。这类线程不进批量组。"""
    return isinstance(source, str) and source.lstrip().startswith("{")


def images_dir_of(home, tid):
    """线程的原图目录 ~/.codex/generated_images/<线程号>（是真目录、不是符号链接才算），没有返回 None。"""
    p = os.path.join(home, ".codex", "generated_images", tid.lower())
    return p if os.path.isdir(p) and not os.path.islink(p) else None


def beijing_of(value):
    """给人看的时间：北京时间（Asia/Shanghai；没有 zoneinfo 或时区库时用固定 UTC+8），格式 2026-10-03 16:00:00 北京时间。
    认不出的值原样转字符串，None 返回 None。存储字段不用这个，仍保留 UTC / 原值。"""
    ts = to_epoch(value)
    if ts is None:
        return None if value is None else str(value)
    from datetime import datetime
    return datetime.fromtimestamp(ts, _bj_tz()).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间"


def _bj_tz():
    from datetime import timedelta, timezone
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Shanghai")
    except Exception:       # 没有 zoneinfo 模块，或系统没有时区库
        return timezone(timedelta(hours=8))


def tree_of(home, tid):
    """线程 tid 的全部后代（状态库的边与 rollout 首行父线程取并集），子孙在前。返回 (后代列表, [错误说明])。
    边每次现读（不走 relations 的缓存）；rollout 一侧用 relations(home)，与 run_native 的后代规则同源。"""
    children, errors = read_edges(home)
    for p, cs in relations(home)["children"].items():
        children.setdefault(p, set()).update(cs)
    return descendants(tid.lower(), children), errors
