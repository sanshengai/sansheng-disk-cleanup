"""Read-only checks of chat transcripts, native Codex history and independently retained bytes."""
import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import closing
from pathlib import Path

try:
    from compression import zstd
except ImportError:  # Python < 3.14
    zstd = None

MAX_RECORD = 32 << 20
MAX_BYTES = 256 << 20
MAX_ENTRIES = 100000
PROOF_LIMIT = 8 << 20


def _regular(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or path.resolve() != path:
        raise ValueError("素材或证明路径不是独立普通文件")
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _digest(path, deadline):
    before = _regular(path)
    if before[2] > MAX_BYTES:
        raise ValueError("素材文件超过校验上限")
    with path.open("rb") as stream:
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(1 << 20), b""):
            if time.monotonic() >= deadline:
                raise ValueError("素材校验超时")
            h.update(block)
    if before != _regular(path):
        raise ValueError("读取期间文件变化")
    return h.hexdigest()


def proofs(spec):
    if spec is None:
        return {}
    if not isinstance(spec, dict) or set(spec) != {"path", "sha256"}:
        raise ValueError("素材证明应包含 path 和 sha256")
    path = Path(spec["path"])
    if not path.is_absolute() or _regular(path)[2] > PROOF_LIMIT:
        raise ValueError("素材保留清单路径或体积不合要求")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != spec["sha256"]:
        raise ValueError("素材保留清单摘要不符")
    copies = json.loads(raw)["copies"]
    if not isinstance(copies, dict) or not copies:
        raise ValueError("素材保留清单为空")
    for h, dest in copies.items():
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h) or not isinstance(dest, str) or not Path(dest).is_absolute():
            raise ValueError("素材保留清单格式无效")
    return copies


class Check:
    def __init__(self, home, scope, spec):
        self.home = Path(home)
        self.scope = Path(scope)
        self.copies = proofs(spec)
        self.size = self.entries = 0
        self.deadline = time.monotonic() + 30

    def debit(self, size):
        self.size += size
        self.entries += 1
        if size > MAX_RECORD or self.size > MAX_BYTES or self.entries > MAX_ENTRIES or time.monotonic() >= self.deadline:
            raise ValueError("会话素材预检达到上限，需单独分批核查")

    def retained(self, digest):
        if digest not in self.copies:
            raise ValueError("会话含素材，需要独立副本及 SHA-256 证明")
        dest = Path(self.copies[digest])
        if (dest.is_relative_to(self.scope) or dest.is_relative_to(self.home / ".codex")
                or dest.is_relative_to(self.home / ".claude") or _digest(dest, self.deadline) != digest):
            raise ValueError("保留副本在删除范围、位于原生目录或摘要不符")

    def encoded(self, data):
        if not isinstance(data, str) or not data:
            raise ValueError("附件或生成素材的编码不明")
        self.retained(hashlib.sha256(base64.b64decode(data, validate=True)).hexdigest())

    def inspect(self, value):
        if isinstance(value, list):
            for element in value:
                self.inspect(element)
        elif isinstance(value, dict):
            typ = value.get("type")
            if typ in ("imageGeneration", "image_generation_call") and value.get("result"):
                self.encoded(value["result"])
            if typ in ("imageGeneration", "image_generation_call") and not value.get("result") and value.get("status") in ("complete", "completed"):
                raise ValueError("已完成的生成图缺少可验证的原始数据")
            if typ == "base64" or ("data" in value and (typ in ("image", "audio", "blob", "file") or "mimeType" in value)):
                self.encoded(value.get("data"))
            if typ in ("image", "document", "audio", "file") and isinstance(value.get("content"), str):
                self.encoded(value["content"])
            pasted = value.get("pastedContents")
            if isinstance(pasted, dict):
                for paste in pasted.values():
                    if isinstance(paste, dict) and paste.get("type") == "text" and isinstance(paste.get("content"), str):
                        self.retained(hashlib.sha256(paste["content"].encode()).hexdigest())
            if typ == "resource" and isinstance(value.get("resource"), dict):
                resource = value["resource"]
                if "blob" in resource:
                    self.encoded(resource["blob"])
                elif isinstance(resource.get("text"), str):
                    self.retained(hashlib.sha256(resource["text"].encode()).hexdigest())
            if isinstance(value.get("input_audio"), dict):
                self.encoded(value["input_audio"].get("data"))
            if typ in ("function_call_output", "tool_result") and isinstance(value.get("output"), str):
                output = value["output"].strip()
                if output.startswith(("{", "[")):
                    try:
                        decoded = json.loads(output)
                    except ValueError:
                        if re.search(r'data:[^, ]+;base64,|"base64"', output, re.I):
                            raise ValueError("工具返回含无法完整核对的素材")
                    else:
                        self.inspect(decoded)
            if typ in ("input_file", "file") and value.get("file_data"):
                if not isinstance(value["file_data"], str) or not value["file_data"].startswith("data:"):
                    self.encoded(value["file_data"])
            if typ in ("image", "document", "file", "audio") and isinstance(value.get("source"), dict):
                src = value["source"]
                if src.get("type") == "text" and isinstance(src.get("data"), str):
                    self.retained(hashlib.sha256(src["data"].encode()).hexdigest())
                elif src.get("type") not in ("base64", "url", "file", "text"):
                    raise ValueError("无法识别附件来源，需要保留整个会话文件")
            if typ == "attachment" and isinstance(value.get("attachment"), dict):
                attachment = value["attachment"]
                if attachment.get("type") in ("file", "pdf", "image", "directory") and not any(
                        k in attachment for k in ("data", "source", "image_url")):
                    raise ValueError("会话内有附件，须核查独立原件或保留整个会话文件")
            for child in value.values():
                self.inspect(child)
        elif isinstance(value, str) and value.startswith("data:"):
            header, sep, body = value.partition(",")
            if not sep or not header.lower().endswith(";base64"):
                raise ValueError("会话内有无法解析的数据 URI")
            self.encoded(body)

    def transcript(self, path, logical):
        before = _regular(path)
        if before[2] > MAX_BYTES:
            raise ValueError("会话文件超过预检上限")
        # A verified byte-for-byte copy also preserves unknown historical attachment formats.
        if self.copies:
            h = _digest(path, self.deadline)
            if h in self.copies:
                self.retained(h)
                return
        if logical.name.endswith(".zst"):
            if zstd is None:
                raise ValueError("当前 Python 无法读取 zstd 会话")
            source = zstd.open(path, "rb")
        else:
            source = path.open("rb")
        with source:
            while True:
                line = source.readline(MAX_RECORD + 1)
                if not line:
                    break
                self.debit(len(line))
                if line.strip():
                    self.inspect(json.loads(line))
        if before != _regular(path):
            raise ValueError("会话文件预检期间变化")

    def collateral(self, path):
        if path.is_symlink():
            raise ValueError("原生附件目录含符号链接，无法核实原件范围")
        self.debit(0)
        if path.is_dir():
            for child in path.iterdir():
                self.collateral(child)
        else:
            size = _regular(path)[2]
            if size > MAX_BYTES - self.size:
                raise ValueError("原生附件超过预检上限")
            self.size += size
            self.retained(_digest(path, self.deadline))

    def attachment(self, data):
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("未知的 Codex 附件登记格式")
        if value.get("type") == "base64" or isinstance(value.get("source"), dict) and value["source"].get("type") == "base64":
            self.inspect(value)
            return
        path = value.get("path") or value.get("filePath")
        if isinstance(path, str) and Path(path).is_absolute():
            self.collateral(Path(path))
            return
        raise ValueError("需核实 Codex 原生附件登记对应的原件")

    def records(self, con, table, column, tids):
        for tid in tids:
            query = (f"SELECT length(CAST({column} AS BLOB)),CASE WHEN length(CAST({column} AS BLOB))"
                     f" <= ? THEN {column} END FROM {table} WHERE thread_id=?")
            for size, data in con.execute(query, (MAX_RECORD, tid)):
                self.debit(size)
                if table == "thread_attachments":
                    self.attachment(data)
                    continue
                if self.copies:
                    raw = data.encode() if isinstance(data, str) else data
                    digest = hashlib.sha256(raw).hexdigest()
                    if digest in self.copies:
                        self.retained(digest)
                        continue
                self.inspect(json.loads(data))

    def history(self, tids):
        base = self.home / ".codex"
        for tid in tids:
            if not isinstance(tid, str) or str(uuid.UUID(tid)) != tid.lower():
                raise ValueError("无效的线程 UUID")
            for sub in ("generated_images", "attachments"):
                path = base / sub / tid
                if os.path.lexists(path):
                    self.collateral(path)
        for db in base.iterdir():
            history = db.match("thread_history_*.sqlite")
            if not history and not db.match("state_*.sqlite"):
                continue
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)) as con:
                con.set_progress_handler(lambda: int(time.monotonic() >= self.deadline), 10000)
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if history:
                    if "thread_items" not in tables:
                        raise ValueError("无法识别 Codex 历史库")
                    self.records(con, "thread_items", "item_json", tids)
                    if "thread_realtime_items" in tables:
                        self.records(con, "thread_realtime_items", "item_json", tids)
                else:
                    columns = {r[1] for r in con.execute("PRAGMA table_info(threads)")}
                    if "rollout_path" in columns:
                        for tid in tids[1:]:
                            for (raw,) in con.execute("SELECT rollout_path FROM threads WHERE id=?", (tid,)):
                                if raw and (os.path.lexists(raw) or os.path.lexists(raw + ".zst")):
                                    raise ValueError("后代会话文件仍在，须先单独核查")
                    if "attached_thread_parent_id" in columns:
                        for tid in tids:
                            if any(child not in tids for (child,) in con.execute(
                                    "SELECT id FROM threads WHERE attached_thread_parent_id=?", (tid,))):
                                raise ValueError("线程含未纳入核查范围的挂接子会话")
                    if "thread_attachments" in tables:
                        self.records(con, "thread_attachments", "payload", tids)


ERRORS = (OSError, TypeError, ValueError, KeyError, ImportError, sqlite3.Error, EOFError, RecursionError) + ((zstd.ZstdError,) if zstd else ())


def roots(home):
    h = Path(home)
    return (h / ".claude/projects", h / ".claude/history.jsonl", h / ".codex/sessions", h / ".codex/archived_sessions", h / ".codex/history.jsonl")


def path_error(path, home, spec=None, original=None):
    actual = Path(path)
    logical = Path(original or path)
    codex = Path(home) / ".codex"
    db_name = r"(?:thread_history|state)_\d+\.sqlite(?:-(?:wal|shm|journal))?"
    if logical.parent == codex and re.fullmatch(db_name, logical.name):
        return "Codex 原生历史库及其日志须通过线程删除入口处理"
    if codex.is_relative_to(logical) and (any(codex.glob("thread_history_*.sqlite")) or any(codex.glob("state_*.sqlite"))):
        return "目标包含 Codex 原生历史库，应按线程处理"
    monitored = roots(home)
    if not any(logical.is_relative_to(r) or r.is_relative_to(logical) for r in monitored):
        return None
    try:
        check = Check(home, actual, spec)
        def visit(p, q):
            check.debit(0)
            if p.is_symlink():
                return  # Removing the link does not delete its referent.
            if p.is_dir():
                for child in p.iterdir():
                    mapped = q / child.name
                    if any(mapped.is_relative_to(r) or r.is_relative_to(mapped) for r in monitored):
                        visit(child, mapped)
            elif q.name.endswith((".jsonl", ".jsonl.zst")):
                check.transcript(p, q)
            elif any(q.is_relative_to(r) for r in monitored):
                raise ValueError("会话目录含非日志文件，须单独核查素材")
        visit(actual, logical)
    except ERRORS as exc:
        return f"会话素材预检未通过：{exc}"
    return None


def native_error(tids, home, rollout=None, spec=None):
    try:
        check = Check(home, Path(home) / ".codex", spec)
        if rollout:
            check.transcript(Path(rollout), Path(rollout))
        check.history(tids)
    except ERRORS as exc:
        return f"Codex 会话素材预检未通过：{exc}"
    return None


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("path", type=Path, nargs="?")
    cli.add_argument("--thread", action="append", help="also check this Codex thread in native history")
    args = cli.parse_args()
    if not (args.path or args.thread):
        cli.error("请指定会话文件或 --thread UUID")
    reason = path_error(args.path.absolute(), Path.home()) if args.path else None
    if args.thread and not reason:
        reason = native_error(args.thread, Path.home())
    print(reason or "预检通过")
    raise SystemExit(1 if reason else 0)
