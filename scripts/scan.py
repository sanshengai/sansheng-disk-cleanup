#!/usr/bin/env python3
"""叁笙清理 · 事实采集。只读，不改动任何被扫描的文件。

用法：
  scan.py scan [--profile P] [--runs-dir D] [--threshold 500MB] [--max-depth 10]
               [--max-seconds 900] [--no-refs] [--refs-seconds 900] [--no-lsof]
  scan.py probe codex [--min-bytes 100MB] [--top 20]
  scan.py probe codex-groups [--trace N --emit <run>/items]
  scan.py probe claude | android | grok
  scan.py probe worktrees --repo <路径>

数据契约见 references/contract.md 第 2、3 节。只用 Python 3.9+ 标准库。
--max-seconds 约束的是目录遍历阶段；lsof、git、引用检查各有自己的超时（引用检查默认 180 秒）。
"""
import argparse
import bisect
import getpass
import itertools
import json
import os
import plistlib
import re
import secrets
import shutil
import stat as statmod
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import protect  # noqa: E402  保护名单直接复用，不复制
import codex_state  # noqa: E402  Codex 父子关系，plan/act 共用
import sysprobe  # noqa: E402  容量三口径直接复用探针，不重写
import units  # noqa: E402  体积统一用十进制 GB 的写法
import space_history  # noqa: E402  扫描记录点与范围可比的增长调查

SCHEMA = "facts/1"
HOME = protect.HOME
DEFAULT_RUNS = os.path.join(protect.STATE_DIR, "runs")
PRODUCERS_PATH = os.path.join(HERE, "producers.json")
ATOMIC_NAMES = {".git", "node_modules", "DerivedData", ".venv"}
ATOMIC_SUFFIXES = (".app", ".photoslibrary", ".avd")
# 标记文件 -> 产物目录（与 references/cards/git-worktree-build.md 的表保持一致）。只是事实字段来源，不参与安全判定。
PROJECT_MARKERS = [
    ("rust", ["Cargo.toml"], ["target"]),
    ("node", ["package.json"], ["node_modules", ".next", "dist", ".turbo", ".parcel-cache"]),
    ("maven", ["pom.xml"], ["target"]),
    ("gradle", ["build.gradle", "build.gradle.kts"], ["build", ".gradle"]),
    ("cmake", ["CMakeLists.txt"], ["build"]),
    ("python", ["pyproject.toml", "setup.py"], [".venv", "__pycache__", ".pytest_cache", ".mypy_cache", "build", "dist"]),
    ("swift", ["Package.swift"], [".build"]),
    ("xcode", ["*.xcodeproj"], ["build"]),
    ("elixir", ["mix.exs"], ["_build", "deps"]),
    ("dart", ["pubspec.yaml"], [".dart_tool", "build"]),
    ("php", ["composer.json"], ["vendor"]),
    ("haskell", ["stack.yaml"], [".stack-work"]),
    ("dotnet", ["*.csproj"], ["bin", "obj"]),
]
PROJECT_DIR_NAMES = {d for _, _, ds in PROJECT_MARKERS for d in ds}
RG = shutil.which("rg")
MAX_SKIPPED = 400
MAX_REF_FILE = 2 * 1024 * 1024
sys.setrecursionlimit(20000)


# ───────────────────────── 通用小工具 ─────────────────────────

def parse_size(value):
    if isinstance(value, (int, float)):
        return int(value)
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([KMGT]?)(?:I?B)?\s*", str(value), re.I)
    if not m:
        raise argparse.ArgumentTypeError("无法解析大小: %r（示例 500MB、1GB、1048576）" % (value,))
    unit = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}[m.group(2).upper()]
    return int(float(m.group(1)) * (1024 ** unit))


def iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tilde(path):
    if path == HOME:
        return "~"
    if path.startswith(HOME + "/"):
        return "~/" + path[len(HOME) + 1:]
    return path


def decode(b):
    return b.decode("utf-8", "surrogateescape")


def git_env():
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"  # git status 不写 index，保持只读
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C.UTF-8" if sys.platform != "darwin" else env.get("LC_ALL", "en_US.UTF-8")
    return env


def run(cmd, timeout=60, cwd=None, input_bytes=None, env=None):
    """返回 (returncode|None, stdout, stderr)；超时或找不到命令时 returncode 为 None。"""
    try:
        p = subprocess.run(cmd, cwd=cwd, input=input_bytes, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return None, b"", b"timeout"
    except OSError as exc:
        return None, b"", str(exc).encode()


def git(args, wt, timeout=60, input_bytes=None):
    return run(["git", "-c", "core.quotepath=false", "-C", wt] + args, timeout=timeout,
               input_bytes=input_bytes, env=git_env())


def dump_json(obj, path_or_file=None, indent=2):
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=indent)
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(obj, ensure_ascii=True, indent=indent)
    return text


def tree_size(path):
    """用 du 的口径量一个目录：st_blocks*512，不跟随符号链接，硬链接只计一次。返回 (bytes, files)。"""
    try:
        st0 = os.lstat(path)
    except OSError:
        return 0, 0
    if not statmod.S_ISDIR(st0.st_mode):
        return st0.st_blocks * 512, 1
    total = files = 0
    seen = set()
    root_dev = st0.st_dev
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.stat(follow_symlinks=False).st_dev != root_dev:
                            continue   # 不跨设备（firmlink / 挂载点），与 du -x 同口径
                        stack.append(e.path)
                        continue
                    st = e.stat(follow_symlinks=False)
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                    total += st.st_blocks * 512
                    files += 1
                except OSError:
                    continue
    return total, files


# ───────────────────────── 生产者规则 ─────────────────────────

def _glob_regex(glob, exact=False):
    """exact=True 只匹配这个路径本身，不连子路径。"""
    g = glob
    if g.startswith("~/"):
        g = HOME + g[1:]
    if g.endswith("/**"):
        g = g[:-3]
    out = []
    i = 0
    while i < len(g):
        if g.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif g.startswith("**", i):
            out.append(".*")
            i += 2
        elif g[i] == "*":
            out.append("[^/]*")
            i += 1
        elif g[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(g[i]))
            i += 1
    return re.compile("^" + "".join(out) + ("$" if exact else "(?:/.*)?$"))


# ── 已装应用清单与 bundle id 反查（只读，事实字段，不影响任何 verdict / 删除逻辑）──

# (根目录, 其下继续进入普通子目录的层数)；"~" 在调用时按 protect.HOME 展开，测试可 monkeypatch 整个列表
APP_ROOTS = [
    ("/Applications", 1),             # 顶层 .app + 厂商子文件夹里一层
    ("/Applications/Utilities", 0),
    ("~/Applications", 0),
    ("/System/Applications", 0),
    ("/opt/homebrew/Caskroom", 3),    # Caskroom/<cask>/<version>/X.app：相对根最多 4 层
]
_APPS_CACHE = {}


def _expand_app_root(r):
    return protect.HOME + r[1:] if r.startswith("~") else r


def _read_app_info(app_path):
    with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
        return plistlib.load(f)


def installed_apps():
    """枚举已装应用。complete=False 表示某个存在的根目录读失败，或某个 .app 的 Info.plist 读不出
    （此时拿不到它的 bundle id，不能断言“某 id 未安装”）。每次扫描只算一次（reset_read_state 清缓存）。"""
    if "apps" in _APPS_CACHE:
        return _APPS_CACHE["apps"]
    roots = [(_expand_app_root(r), d) for r, d in APP_ROOTS]
    real_roots = [os.path.realpath(r) for r, _ in roots]
    errors = []
    state = {"complete": True}
    found = {}

    def in_scope(real):
        return any(real == rr or real.startswith(rr + os.sep) for rr in real_roots)

    def add(app_path):
        real = os.path.realpath(app_path)
        if real in found or not in_scope(real):
            return
        rec = {"path": app_path, "bundle_id": None, "version": None, "short_version": None, "name": None}
        try:
            info = _read_app_info(app_path)
            rec["bundle_id"] = info.get("CFBundleIdentifier")
            rec["short_version"] = info.get("CFBundleShortVersionString")
            rec["version"] = info.get("CFBundleVersion")
            rec["name"] = info.get("CFBundleName")
        except Exception as exc:  # noqa: BLE001  读不到/解析失败都算不完整
            state["complete"] = False
            errors.append({"path": app_path, "error": "%s: %s" % (type(exc).__name__, exc)})
        found[real] = rec

    def scan_dir(d, depth_left):
        try:
            entries = list(os.scandir(d))
        except OSError as exc:
            state["complete"] = False
            errors.append({"path": d, "error": "listdir失败: %s" % exc})
            return
        for e in entries:
            try:
                if e.is_symlink():
                    if e.name.endswith(".app") and in_scope(os.path.realpath(e.path)):
                        add(e.path)
                    continue
                if not e.is_dir(follow_symlinks=False):
                    continue
                if e.name.endswith(".app"):
                    add(e.path)
                elif depth_left > 0:
                    scan_dir(e.path, depth_left - 1)
            except OSError as exc:
                state["complete"] = False
                errors.append({"path": e.path, "error": "访问失败: %s" % exc})

    for r, depth in roots:
        if os.path.isdir(r):
            scan_dir(r, depth)
        elif os.path.lexists(r):
            state["complete"] = False
            errors.append({"path": r, "error": "存在但不是可读目录"})
    apps = sorted(found.values(), key=lambda a: a["path"])
    by_id = {}
    for a in apps:
        if a["bundle_id"]:
            by_id.setdefault(a["bundle_id"].lower(), []).append(a)
    res = {"complete": state["complete"], "errors": errors, "apps": apps, "by_id": by_id}
    _APPS_CACHE["apps"] = res
    return res


# 名字（小写）以这些前缀开头、或含 crashpad 的是共享组件：多个软件共用，永不归属给某一个应用
SHARED_PREFIXES = ("com.apple.", "org.chromium.", "com.google.keystone", "com.google.googleupdater",
                   "com.google.softwareupdate", "org.sparkle-project.", "com.github.electron", "com.electron.",
                   "org.qt-project.", "io.qt.")
# (Library 下的目录, 名字后缀)：节点落在 ~/Library/<目录>/<名><后缀> 时取第一层名字
LIB_LOCATIONS = (("Caches", ""), ("Containers", ""), ("Group Containers", ""), ("Application Support", ""),
                 ("HTTPStorages", ""), ("WebKit", ""), ("Saved Application State", ".savedState"),
                 ("Logs", ""), ("Preferences", ".plist"))
CONTAINER_META = ".com.apple.containermanagerd.metadata.plist"
BUNDLE_ID_SHAPE = re.compile(r"^[^\s.]+(?:\.[^\s.]+){2,}$")   # 至少两个点、无空格
LINEAGE_MIN_SEGMENTS = 3   # 点分血缘里较短的一方至少 3 段，避免 com.google 这类厂商前缀把整家当作祖先
SAFETY_LEVELS = ("safe", "moderate", "risky")


def is_shared_component(name):
    low = name.lower()
    return low.startswith(SHARED_PREFIXES) or "crashpad" in low


def looks_like_bundle_id(name):
    return bool(BUNDLE_ID_SHAPE.match(name))


def _lib_first_name(path):
    """节点路径在已知 Library 位置下时返回 (位置目录, 第一层名字(已去后缀), 第一层目录的绝对路径)，否则 None。"""
    base = protect.HOME + "/Library/"
    if not path.startswith(base):
        return None
    parts = path[len(base):].split("/")
    if len(parts) < 2 or not parts[1]:
        return None
    for loc, suffix in LIB_LOCATIONS:
        if parts[0] != loc:
            continue
        raw = parts[1]
        if suffix:
            if not raw.endswith(suffix) or raw == suffix:
                return None
            name = raw[:-len(suffix)]
        else:
            name = raw
        return loc, name, base + loc + "/" + raw
    return None


def _strip_group_prefix(name):
    """Group Containers 名 `<TeamID>.<bundle id>` 或 `group.<bundle id>` -> bundle id；不是这种形态返回 None。"""
    m = re.match(r"^(?:group|[A-Za-z0-9]{10})\.(.+)$", name)
    return m.group(1) if m and "." in m.group(1) else None


def _container_identifier(dirpath):
    try:
        with open(os.path.join(dirpath, CONTAINER_META), "rb") as f:
            ident = plistlib.load(f).get("MCMMetadataIdentifier")
    except Exception:  # noqa: BLE001  无元数据/读不出就当没有
        return None
    return ident if isinstance(ident, str) and ident else None


def _lineage_candidates(name_low, by_id):
    """点分前缀血缘（按段比较，不是字符串包含）。返回 (后代名命中的最长祖先 id, 祖先名命中的 id 列表)。"""
    nt = name_low.split(".")
    up = None
    down = []
    for bid in by_id:
        bt = bid.split(".")
        if bt == nt:
            continue
        if len(bt) < len(nt) and len(bt) >= LINEAGE_MIN_SEGMENTS and nt[:len(bt)] == bt:
            if up is None or len(bt) > len(up.split(".")):
                up = bid          # 名字是某已装 id 的后代（如 com.google.Chrome.helper）
        elif len(nt) < len(bt) and len(nt) >= LINEAGE_MIN_SEGMENTS and bt[:len(nt)] == nt:
            down.append(bid)      # 名字是某已装 id 的祖先（如 com.google.Chrome ↔ com.google.Chrome.canary）
    return up, down


def _app_view(rec):
    return {"name": rec.get("name") or os.path.basename(rec["path"])[:-4],
            "bundle_id": rec.get("bundle_id"),
            "version": rec.get("short_version") or rec.get("version"),
            "path": rec["path"]}


def reverse_lookup(path, apps=None):
    """节点路径 -> {"app","app_match","app_installed"[, "shared_component"]}；不在已知 Library 位置返回全 null。
    只给事实，调用方不得据此改 verdict。"""
    out = {"app": None, "app_match": None, "app_installed": None}
    hit = _lib_first_name(path)
    if hit is None:
        return out
    loc, name, dirpath = hit
    stripped = _strip_group_prefix(name) if loc == "Group Containers" else None
    if is_shared_component(name) or (stripped and is_shared_component(stripped)):
        out["shared_component"] = True
        return out
    apps = apps if apps is not None else installed_apps()
    by_id = apps["by_id"]
    rec = how = None
    low = name.lower()
    if low in by_id:
        rec, how = by_id[low][0], "exact"
    elif stripped and stripped.lower() in by_id:
        rec, how = by_id[stripped.lower()][0], "group_prefix"
    if rec is None and loc in ("Containers", "Group Containers"):
        ident = _container_identifier(dirpath)
        if ident and ident.lower() in by_id and not is_shared_component(ident):
            rec, how = by_id[ident.lower()][0], "container_metadata"
    if rec is None:
        cand = stripped.lower() if stripped else low
        up, down = _lineage_candidates(cand, by_id)
        pick = up or (down[0] if len(down) == 1 else None)   # 祖先方向命中多个不同 id 时不归属
        if pick:
            rec, how = by_id[pick][0], "lineage"
    if rec is not None:
        out.update(app=_app_view(rec), app_match=how)
        out["app_installed"] = True if apps["complete"] else None
        return out
    if apps["complete"] and looks_like_bundle_id(name):
        out["app_installed"] = False
    return out


PRODUCER_SCHEMAS = ("producers/1", "producers/2")
_BASE_PRODUCER = {"tool": None, "card": None, "hint": None}


def _is_file(path):
    """已存在且不是目录（符号链接按链接本身算，不跟随）。"""
    try:
        return not statmod.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


class Producers:
    """规则按顺序匹配，第一条命中即用。producers/2 的可选字段 bundle_ids / safety / blocked_by_processes /
    default_select 命中时原样带进 producer；旧 producers/1 规则文件照常加载。
    0.5：规则可带 exact=true（只匹配路径本身）和 only="file"（只匹配已存在的单个文件，目录一律不套）。"""

    def __init__(self, path=PRODUCERS_PATH):
        self.rules = []
        self.schema_error = None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        schema = data.get("schema")
        if schema is not None and schema not in PRODUCER_SCHEMAS:
            self.schema_error = "未知 schema: %r" % (schema,)
            return
        for r in data.get("rules", []):
            try:
                info = {"tool": r.get("tool"), "card": r.get("card"), "hint": r.get("hint")}
                bids = r.get("bundle_ids")
                bids = [b for b in bids if isinstance(b, str)] if isinstance(bids, list) else []
                if bids:
                    info["bundle_ids"] = bids
                if r.get("safety") in SAFETY_LEVELS:
                    info["safety"] = r["safety"]
                procs = r.get("blocked_by_processes")
                if isinstance(procs, list):
                    info["blocked_by_processes"] = [p for p in procs if isinstance(p, str)]
                if isinstance(r.get("default_select"), bool):
                    info["default_select"] = r["default_select"]
                only_file = r.get("only") == "file"        # 只匹配已存在的单个文件（目录不套这张卡）
                self.rules.append((_glob_regex(r["glob"], exact=r.get("exact") is True), info,
                                   {b.lower() for b in bids}, only_file))
            except (KeyError, re.error):
                continue

    def match(self, path, apps=None):
        """apps 不为 None 时（传 installed_apps() 的结果）额外做 bundle id 反查：写入 app / app_match /
        app_installed，并允许规则按 bundle_ids 命中；apps 为 None 时是纯 glob 匹配。"""
        facts = reverse_lookup(path, apps) if apps is not None else None
        bid = ((facts["app"] or {}).get("bundle_id") or "").lower() if facts else ""
        out = dict(_BASE_PRODUCER)
        for rx, info, bids, only_file in self.rules:
            if (rx.match(path) and (not only_file or _is_file(path))) or (bid and bid in bids):
                out = dict(info)
                break
        if facts is not None:
            out.update(facts)
        return out


# ───────────────────────── 遍历 ─────────────────────────

DATA_VOLUME = "/System/Volumes/Data"


def is_firmlink_dup(path):
    """/ 下的 /System/Volumes/Data 与 / 下的 firmlink 目录是同一份数据；测试可 monkeypatch。"""
    return path == DATA_VOLUME


def roots_cover_data_volume(roots):
    return any(r == "/" or r == DATA_VOLUME for r in roots)


PROGRESS_SECONDS = 30.0      # 遍历阶段每隔多久向 stderr 报一次进度（大盘 7 分钟以上没有任何输出会让人以为卡死）


def print_progress(w):
    """遍历进度：只写 stderr，不污染 stdout（探针/脚本可能把 stdout 当 JSON 解析）。"""
    print("[扫描进度] 已遍历 %s / %d 个目录 / 用时 %d 秒" % (
        units.fmt_bytes(w.bytes_seen), w.dirs, int(time.monotonic() - w.started)), file=sys.stderr, flush=True)


class Walker:
    def __init__(self, threshold, max_depth, deadline, now_ts, skip_firmlink_dup=False,
                 progress_every=PROGRESS_SECONDS, progress=print_progress, started=None):
        self.started = time.monotonic() if started is None else started
        self.progress_every = progress_every          # <=0 表示不报
        self.progress = progress
        self.next_progress = self.started + progress_every
        self.bytes_seen = 0                           # 已计入的文件占用（硬链接只算一次），仅用于进度显示
        self.skip_firmlink_dup = skip_firmlink_dup
        self.unread = 0          # 因权限/超时/读错而没计完的目录数（节点体积是下限的依据）
        self.partial_skips = 0   # 含云盘/dataless 跳过在内的未计完数，决定 scan_status
        self.perm_count = 0      # 权限类 skipped 总数（不受 MAX_SKIPPED 截断）
        self.threshold = threshold
        self.max_depth = max_depth
        self.deadline = deadline
        self.cutoff = now_ts - 86400
        self.nodes = {}
        self.skipped = []
        self.skipped_overflow = 0
        self.seen = set()
        self.incomplete = False
        self.dev = None
        self.dirs = 0
        self.files = 0

    def skip(self, path, reason):
        if reason in ("permission", "timeout") or reason.startswith("error:"):
            self.unread += 1
            self.partial_skips += 1
            if reason == "permission":
                self.perm_count += 1
        elif reason in ("cloud-not-entered", "dataless-dir-not-entered"):
            self.partial_skips += 1
        if len(self.skipped) < MAX_SKIPPED:
            self.skipped.append({"path": path, "reason": reason})
        else:
            self.skipped_overflow += 1

    def walk_root(self, root):
        try:
            st = os.lstat(root)
        except OSError as exc:
            self.skip(root, "error:%s" % (exc.strerror or exc))
            return 0
        self.dev = st.st_dev
        if _under_ci(root, _standing_cloud_dirs()):
            self.skip(root, "cloud-not-entered")
            return 0
        if is_dataless(root, st):
            self.skip(root, "dataless-dir-not-entered")
            return 0
        u0 = self.unread
        total, files, newest, recent, big = self.walk(root, 0, True)
        if total >= self.threshold:
            self.nodes[root] = {"bytes": total, "files": files, "newest": newest, "recent": recent,
                                "big": big, "depth": 0, "lower_bound": self.unread > u0}
        return total

    def walk(self, path, depth, reg_children):
        """返回 (bytes, files, newest_mtime, recent_24h, 已登记的大子目录[(path, bytes)])。"""
        if time.monotonic() > self.deadline:
            self.incomplete = True
            self.skip(path, "timeout")
            return 0, 0, 0.0, 0, []
        try:
            it = os.scandir(path)
        except PermissionError:
            self.skip(path, "permission")
            return 0, 0, 0.0, 0, []
        except OSError as exc:
            self.skip(path, "error:%s" % (exc.strerror or exc))
            return 0, 0, 0.0, 0, []
        self.dirs += 1
        if self.progress_every > 0:
            now = time.monotonic()
            if now >= self.next_progress:
                self.next_progress = now + self.progress_every
                self.progress(self)
        total = files = recent = 0
        newest = 0.0
        big = []
        seen = self.seen
        cutoff = self.cutoff
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if self.skip_firmlink_dup and is_firmlink_dup(e.path):
                            self.skip(e.path, "firmlink-duplicate")
                            continue
                        st = e.stat(follow_symlinks=False)
                        if st.st_dev != self.dev:
                            self.skip(e.path, "other-device")
                            continue
                        # 云盘目录与 dataless 目录不进入：列目录本身就会让 File Provider 物化占位项
                        if _under_ci(e.path, _standing_cloud_dirs()):
                            self.skip(e.path, "cloud-not-entered")
                            continue
                        if is_dataless(e.path, st):
                            self.skip(e.path, "dataless-dir-not-entered")
                            continue
                        name = e.name
                        atomic = name in ATOMIC_NAMES or name.endswith(ATOMIC_SUFFIXES)
                        child_reg = reg_children and not atomic and (depth + 1) < self.max_depth
                        u0 = self.unread
                        cb, cf, cn, cr, cbig = self.walk(e.path, depth + 1, child_reg)
                        total += cb
                        files += cf
                        recent += cr
                        if cn > newest:
                            newest = cn
                        if reg_children and cb >= self.threshold:
                            self.nodes[e.path] = {"bytes": cb, "files": cf, "newest": cn, "recent": cr,
                                                  "big": cbig, "depth": depth + 1,
                                                  "lower_bound": self.unread > u0}
                            big.append((e.path, cb))
                    else:
                        st = e.stat(follow_symlinks=False)
                        if st.st_nlink > 1 and not statmod.S_ISLNK(st.st_mode):
                            key = (st.st_dev, st.st_ino)
                            if key in seen:
                                continue
                            seen.add(key)
                        total += st.st_blocks * 512
                        self.bytes_seen += st.st_blocks * 512
                        files += 1
                        mt = st.st_mtime
                        if mt > newest:
                            newest = mt
                        if mt >= cutoff:
                            recent += 1
                except OSError:
                    continue
        self.files += files
        return total, files, newest, recent, big


# ───────────────────────── git ─────────────────────────

class GitCtx:
    def __init__(self):
        self.wt_cache = {}
        self.tracked_cache = {}
        self.errors = {}

    def worktree_of(self, path):
        cur = path
        chain = []
        while True:
            if cur in self.wt_cache:
                res = self.wt_cache[cur]
                break
            if os.path.lexists(os.path.join(cur, ".git")):
                res = cur
                break
            chain.append(cur)
            parent = os.path.dirname(cur)
            if parent == cur:
                res = None
                break
            cur = parent
        self.wt_cache[cur] = res
        for c in chain:
            self.wt_cache[c] = res
        return res

    @staticmethod
    def main_repo(wt):
        g = os.path.join(wt, ".git")
        if os.path.isdir(g):
            return wt
        try:
            with open(g, encoding="utf-8", errors="replace") as f:
                line = f.readline().strip()
            if not line.startswith("gitdir:"):
                return wt
            gitdir = line[7:].strip()
            if not os.path.isabs(gitdir):
                gitdir = os.path.normpath(os.path.join(wt, gitdir))
            common = gitdir
            cd = os.path.join(gitdir, "commondir")
            if os.path.isfile(cd):
                with open(cd, encoding="utf-8", errors="replace") as f:
                    rel = f.readline().strip()
                common = os.path.normpath(os.path.join(gitdir, rel))
            return os.path.dirname(common) if os.path.basename(common) == ".git" else common
        except OSError:
            return wt

    def tracked(self, wt):
        if wt in self.tracked_cache:
            return self.tracked_cache[wt]
        rc, out, err = git(["ls-files", "-z"], wt, timeout=180)
        if rc != 0:
            self.errors[wt] = decode(err).strip()[:200] or "git ls-files 失败"
            self.tracked_cache[wt] = None
            return None
        names = sorted(decode(x) for x in out.split(b"\0") if x)
        self.tracked_cache[wt] = names
        return names

    @staticmethod
    def count_prefix(names, rel):
        if rel == "":
            return len(names)
        lo = bisect.bisect_left(names, rel + "/")
        hi = bisect.bisect_left(names, rel + "0")  # '0' 紧跟在 '/' 之后
        return max(0, hi - lo)

    def check_ignore(self, wt, rels):
        if not rels:
            return set()
        payload = b"\0".join(r.encode("utf-8", "surrogateescape") for r in rels) + b"\0"
        rc, out, err = git(["check-ignore", "--stdin", "-z"], wt, timeout=120, input_bytes=payload)
        if rc not in (0, 1):
            return None
        return {decode(x) for x in out.split(b"\0") if x}


def find_project_marker(path):
    """目录名属于产物目录表时，查父目录是否有对应标记文件；不在表内返回 False（调用方不输出字段）。
    返回 {"marker","ecosystem"} 或 None（在表内但无标记文件）。"""
    name = os.path.basename(path.rstrip("/"))
    if name not in PROJECT_DIR_NAMES:
        return False
    parent = os.path.dirname(path.rstrip("/"))
    try:
        entries = os.listdir(parent)
    except OSError:
        return None
    import fnmatch
    for eco, markers, dirs in PROJECT_MARKERS:
        if name not in dirs:
            continue
        for m in markers:
            hit = [e for e in entries if fnmatch.fnmatchcase(e, m)] if "*" in m else ([m] if m in entries else [])
            if hit:
                return {"marker": hit[0], "ecosystem": eco}
    return None


def annotate_git(nodes, ctx):
    """nodes: 带 path/files 的节点字典列表，就地写入 n['git'] 与 n['git_note']。"""
    groups = {}
    for n in nodes:
        n["git"] = {"repo": None, "worktree": None, "branch": None, "role": "none", "tracked_files": 0,
                    "merged_into_origin_main": None, "dirty_files": None}
        wt = ctx.worktree_of(n["path"])
        if wt is None:
            continue
        rel = os.path.relpath(n["path"], wt)
        rel = "" if rel == "." else rel
        n["git"]["repo"] = ctx.main_repo(wt)
        n["git"]["worktree"] = wt
        if rel == ".git" or rel.startswith(".git/"):
            n["git_note"] = "git 内部数据"
            continue
        groups.setdefault(wt, []).append((n, rel))
    for wt, items in groups.items():
        names = ctx.tracked(wt)
        if names is None:
            for n, rel in items:
                n["git_note"] = "git ls-files 失败: %s" % ctx.errors.get(wt, "")
            continue
        pending = []
        for n, rel in items:
            cnt = ctx.count_prefix(names, rel)
            g = n["git"]
            g["tracked_files"] = cnt
            if cnt > 0:
                g["role"] = "tracked" if cnt >= n["files"] else "mixed"
            elif rel == "":
                g["role"] = "untracked"
            else:
                pending.append((n, rel))
        if pending:
            ignored = ctx.check_ignore(wt, [r for _, r in pending])
            for n, rel in pending:
                if ignored is None:
                    n["git"]["role"] = "untracked"
                    n["git_note"] = "git check-ignore 失败，按未忽略处理"
                else:
                    n["git"]["role"] = "ignored" if rel in ignored else "untracked"
        for n, rel in items:
            if rel != "":
                continue
            g = n["git"]
            rc, out, _ = git(["symbolic-ref", "-q", "--short", "HEAD"], wt, timeout=20)
            g["branch"] = decode(out).strip() if rc == 0 and out.strip() else "(detached)"
            rc, _, _ = git(["rev-parse", "-q", "--verify", "origin/main"], wt, timeout=20)
            if rc == 0:
                rc2, _, _ = git(["merge-base", "--is-ancestor", "HEAD", "origin/main"], wt, timeout=30)
                g["merged_into_origin_main"] = True if rc2 == 0 else (False if rc2 == 1 else None)
            rc, out, _ = git(["status", "--porcelain"], wt, timeout=150)
            if rc == 0:
                g["dirty_files"] = len([x for x in out.split(b"\n") if x])


# ───────────────────────── lsof ─────────────────────────

def run_lsof(timeout=120):
    """一次性读取全部打开文件。返回 [(pid, command, name)]；不可用返回 None。"""
    if not shutil.which("lsof"):
        return None
    rc, out, _ = run(["lsof", "-n", "-P", "-w", "+c", "0", "-F", "pcn"], timeout=timeout)
    if rc is None or (rc != 0 and not out):
        return None
    entries = []
    pid = None
    cmd = ""
    for raw in out.split(b"\n"):
        if not raw:
            continue
        tag = raw[:1]
        val = decode(raw[1:])
        if tag == b"p":
            try:
                pid = int(val)
            except ValueError:
                pid = None
            cmd = ""
        elif tag == b"c":
            cmd = val
        elif tag == b"n" and pid is not None and val.startswith("/"):
            entries.append((pid, cmd, val))
    return entries


def attach_open_by(nodes, entries):
    if entries is None:
        for n in nodes:
            n["open_by"] = "unknown"
        return
    by_path = {n["path"]: n for n in nodes}
    found = {}
    for pid, cmd, name in entries:
        p = name
        while True:
            if p in by_path:
                found.setdefault(p, {})[pid] = cmd
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
    for n in nodes:
        procs = found.get(n["path"], {})
        n["open_by"] = [{"pid": pid, "command": cmd} for pid, cmd in sorted(procs.items())][:20]


# ───────────────────────── 引用检查 ─────────────────────────

def build_patterns(path, wt):
    pats = [path]
    if path.startswith(HOME + "/"):
        pats.append("~/" + path[len(HOME) + 1:])
    if wt:
        rel = os.path.relpath(path, wt)
        if rel != "." and not rel.startswith(".git") and ("/" in rel or len(rel) >= 12):
            pats.append(rel)
    base = os.path.basename(path)
    # 契约：基名长度 >= 12 或含日期/哈希。纯字母的普通单词（orchestrator、conversations）
    # 会命中成百上千个无关文件，不算“独特”，要求长基名里至少带一个数字或 - _ . 才用。
    if re.search(r"\d{4}-?\d{2}-?\d{2}|[0-9a-fA-F]{8,}", base) or (len(base) >= 12 and re.search(r"[-_.0-9]", base)):
        pats.append(base)
    out = []
    for p in pats:
        if p and "\n" not in p and p not in out:
            out.append(p)
    return out


SF_DATALESS = 0x40000000        # macOS：仅在线占位文件，读内容会触发云盘下载
CLOUD_REL = ("Library/Mobile Documents", "Library/CloudStorage")   # 相对 HOME 的云盘目录
# --iglob：APFS 默认大小写不敏感；排除 glob 不以搜索根为锚（根是 ~、/Users 或任意祖先都生效）
RG_CLOUD_EXCLUDES = ["--iglob", "!**/Library/Mobile Documents", "--iglob", "!**/Library/CloudStorage"]
ICLOUD_DD = ("Desktop", "Documents")
READ_SKIPS = []                 # 读内容时因 dataless 等原因跳过的 {"path","reason"}，汇总进 facts.skipped
READ_WARNINGS = []              # 整根被跳过等告警，汇总进 facts.coverage.warnings
_ICLOUD_DD_CACHE = {}

# 引用检查"没查完"的原因，优先级从高到低；任何一种出现，涉及的节点都是 checked=False
ISSUE_ORDER = ("cloud_root_skipped", "dataless_skipped", "icloud_desktop_documents_excluded", "read_error")


class DatalessError(OSError):
    """读内容前发现是 dataless 占位，拒绝读取（读了会触发云盘下载）。"""


def reset_read_state():
    READ_SKIPS.clear()
    READ_WARNINGS.clear()
    _ICLOUD_DD_CACHE.clear()


def is_dataless(path, st=None):
    """路径（文件或目录）是不是 iCloud/File Provider 的 dataless 占位。读内容前必须先问它。"""
    try:
        st = st or os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def _note_read_skip(path, reason="dataless"):
    if len(READ_SKIPS) < MAX_SKIPPED:
        READ_SKIPS.append({"path": path, "reason": reason})


def icloud_desktop_documents():
    """iCloud“桌面与文稿”同步是否开启：只做存在性检查，不列目录内容。"""
    key = HOME
    if key not in _ICLOUD_DD_CACHE:
        base = os.path.join(HOME, "Library", "Mobile Documents", "com~apple~CloudDocs")
        _ICLOUD_DD_CACHE[key] = any(os.path.exists(os.path.join(base, n)) for n in ICLOUD_DD)
    return _ICLOUD_DD_CACHE[key]


def _standing_cloud_dirs():
    return [os.path.join(HOME, r) for r in CLOUD_REL]


def _icloud_dd_dirs():
    return [os.path.join(HOME, n) for n in ICLOUD_DD] if icloud_desktop_documents() else []


def _under_ci(path, bases):
    p = path.lower()
    return any(p == b.lower() or p.startswith(b.lower() + "/") for b in bases)


def _skip_kind(path, st=None):
    """读 path 之前先问：None=可读；"cloud"=云盘目录（Mobile Documents/CloudStorage，常规排除）；
    "icloud_dd"=同步开启时的 ~/Desktop、~/Documents；"dataless"=占位文件/目录。比较大小写不敏感。"""
    if _under_ci(path, _standing_cloud_dirs()):
        return "cloud"
    if _under_ci(path, _icloud_dd_dirs()):
        return "icloud_dd"
    if is_dataless(path, st):
        return "dataless"
    return None


def _in_cloud(path):
    return _skip_kind(path) in ("cloud", "icloud_dd")


def _kind_issue(kind):
    return {"cloud": "cloud_root_skipped", "icloud_dd": "cloud_root_skipped",
            "dataless": "dataless_skipped"}.get(kind)


def _pick_reason(issues):
    for r in ISSUE_ORDER:
        if r in issues:
            return r
    return None


def _rg_cwd():
    return HOME if os.path.isdir(HOME) else None


def _rg_exclude_args():
    """rg 的云盘排除参数。~/Desktop、~/Documents 在同步开启时一并排除；这些 glob 以 cwd=HOME 为锚，
    同时给一条 **/<家目录名>/ 的写法，让根是 ~ 的祖先时也生效。调用 rg 时必须 cwd=_rg_cwd()。"""
    args = list(RG_CLOUD_EXCLUDES)
    if icloud_desktop_documents():
        base = os.path.basename(HOME.rstrip("/"))
        for n in ICLOUD_DD:
            args += ["--iglob", "!/%s" % n, "--iglob", "!**/%s/%s" % (base, n)]
    return args


def _git_exclude_specs(root):
    """git grep 的排除 pathspec：glob 写法不以搜索根为锚，icase 对应大小写不敏感的文件系统。"""
    specs = [":(exclude,glob,icase)**/%s/**" % r for r in CLOUD_REL]
    for c in _icloud_dd_dirs():
        if protect._within(c, root) and c != root:
            specs.append(":(exclude,icase)%s" % os.path.relpath(c, root))
    return specs


def _dd_excluded_inside(root):
    """同步开启时，root 里面是否有被排除的 ~/Desktop、~/Documents（root 是它们的祖先）。"""
    return any(c != root and protect._within(c, root) and os.path.exists(c) for c in _icloud_dd_dirs())


def _read_capped(path, cap=64 * 1024 * 1024):
    if is_dataless(path):
        _note_read_skip(path)
        raise DatalessError("dataless: %s" % path)
    with open(path, "rb") as f:
        return f.read(cap)


def _py_scan_root(root, regex, deadline, issues=None):
    """无 rg、无 git 时的兜底扫描。跳过 dataless/云盘；因 dataless、桌面与文稿同步或读失败而漏看的，
    往 issues（list）里记原因，调用方据此把相关节点标为未核实。"""
    def note(reason):
        if issues is not None:
            issues.append(reason)

    hits = set()
    skip_dirs = {".git", "node_modules", ".venv", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() > deadline:
            return None
        keep = []
        for d in dirnames:
            if d in skip_dirs:
                continue
            dp = os.path.join(dirpath, d)
            kind = _skip_kind(dp)
            if kind:
                _note_read_skip(dp, "dataless" if kind == "dataless" else "cloud-excluded")
                if kind == "dataless":
                    note("dataless_skipped")
                elif kind == "icloud_dd":
                    note("icloud_desktop_documents_excluded")
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                st = os.lstat(p)
                if not statmod.S_ISREG(st.st_mode) or st.st_size > MAX_REF_FILE:
                    continue
                if is_dataless(p, st):
                    _note_read_skip(p)
                    note("dataless_skipped")
                    continue
                with open(p, "rb") as f:
                    data = f.read()
            except OSError:
                note("read_error")
                continue
            if b"\0" in data[:8000]:
                continue
            if regex.search(data):
                hits.add(p)
    return hits


def _grep_scope(root, pats, deadline, issues=None):
    """在一个范围里找出含任一模式的文件（绝对路径集合）；失败、超时或整根被跳过返回 None。
    没查完但还有部分结果（dataless 被跳过、rg 退出码 2、桌面与文稿被排除）时返回已找到的集合，
    并把原因记进 issues（list）；调用方必须把这些情形当作"未核实"，不能当 0 引用。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    kind = _skip_kind(root)
    if kind:
        READ_WARNINGS.append("搜索根位于云盘/占位目录内，已跳过以免触发下载：%s" % root)
        _note_read_skip(root, "dataless" if kind == "dataless" else "cloud-excluded")
        if issues is not None:
            issues.append(_kind_issue(kind))
        return None
    fd, pf = tempfile.mkstemp(prefix="refpat-", suffix=".txt")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(("\n".join(pats) + "\n").encode("utf-8", "surrogateescape"))
        dd_inside = _dd_excluded_inside(root)
        if dd_inside and issues is not None:
            issues.append("icloud_desktop_documents_excluded")
        # 被 git 忽略的过程文件（镜头 manifest、出图回执）恰恰最常引用中间产物，
        # 所以优先用 rg 并带 --no-ignore；git grep 只搜受跟踪文件，只在没有 rg 时兜底。
        if RG:
            cmd = [RG, "-F", "-l", "-0", "--hidden", "--no-ignore", "--max-filesize", "2M",
                   "--no-messages", "-g", "!.git", "-g", "!node_modules"] + _rg_exclude_args() + ["-f", pf, "--", root]
            rc, out, _ = run(cmd, timeout=remaining, cwd=_rg_cwd())
            if rc == 1:
                return set()
            if rc is None or rc not in (0, 2):
                return None
            if rc == 2 and issues is not None:
                issues.append("read_error")      # 有读取错误：已列出的命中可信，但没列出的可能漏了
            elif rc == 2:
                return None
            files = {decode(x) for x in out.split(b"\0") if x}
        elif os.path.lexists(os.path.join(root, ".git")):
            rc, out, _ = git(["grep", "-F", "-l", "-z", "-I", "--no-color", "--untracked", "-f", pf, "--",
                             "."] + _git_exclude_specs(root), root, timeout=remaining)
            if rc == 1:
                return set()
            if rc != 0:
                return None
            files = {os.path.join(root, decode(x)) for x in out.split(b"\0") if x}
        else:
            regex = re.compile(b"|".join(re.escape(p.encode("utf-8", "surrogateescape")) for p in pats))
            files = _py_scan_root(root, regex, deadline, issues)
            if files is None:
                return None
    finally:
        try:
            os.unlink(pf)
        except OSError:
            pass
    state = protect.STATE_DIR
    return {f for f in files if not (f == state or f.startswith(state + "/"))}


LOCATE_MAX = 1500       # 命中文件不超过这个数，直接逐个文件归属
SAMPLE_SIZE = 400       # 命中太多时，抽样找出“到处都是”的模式
GENERIC_RATIO = 0.03    # 抽样里出现比例 >= 3% 的模式视为泛用，不再逐文件归属


def _first_hit(data, pats_b):
    """data 里最早命中的模式位置（字节偏移）；没有返回 None。"""
    best = None
    for pb in pats_b:
        idx = data.find(pb)
        if idx >= 0 and (best is None or idx < best):
            best = idx
    return best


def _search_scope(root, keys, by_key, result, deadline):
    """在一个范围里搜 keys 的全部模式，结果写入 result。返回统计信息；失败时把 keys 标 checked=False。"""
    owners = {}   # 模式 -> 拥有它的 key 列表
    for k in sorted(keys):
        for p in by_key[k]["patterns"]:
            owners.setdefault(p, []).append(k)
    pats = list(owners)
    info = {"root": root, "patterns": len(pats), "nodes": len(keys), "ok": True, "hit_files": 0,
            "generic_patterns": 0}

    issues = []   # 没查完的原因（云盘根被跳过 / dataless / 读失败）；非空就不能判"已核实"

    def fail(reason=None):
        info["ok"] = False
        for k in keys:
            result[k]["checked"] = False
            result[k]["reason"] = result[k].get("reason") or reason or "timeout-or-error"
        return info

    if not pats:
        return info
    found = _grep_scope(root, pats, deadline, issues)
    if found is None:
        return fail(_pick_reason(issues))
    info["hit_files"] = len(found)
    generic = set()
    sample_hits = {}   # 泛用模式 -> {文件: 行号}
    if len(found) > LOCATE_MAX:
        ordered = sorted(found)
        step = max(1, len(ordered) // SAMPLE_SIZE)
        sample = ordered[::step][:SAMPLE_SIZE]
        freq = {}
        pats_b = {p: p.encode("utf-8", "surrogateescape") for p in pats}
        loaded = 0
        for f in sample:
            try:
                data = _read_capped(f, 4 * 1024 * 1024)
            except OSError as exc:
                issues.append("dataless_skipped" if isinstance(exc, DatalessError) else "read_error")
                continue
            loaded += 1
            for p, pb in pats_b.items():
                idx = data.find(pb)
                if idx >= 0:
                    freq.setdefault(p, {})[f] = data.count(b"\n", 0, idx) + 1
        generic = {p for p, fl in freq.items() if loaded and len(fl) / loaded >= GENERIC_RATIO}
        sample_hits = {p: freq[p] for p in generic}
        info["generic_patterns"] = len(generic)
        rest = [p for p in pats if p not in generic]
        found = _grep_scope(root, rest, deadline, issues) if rest else set()
        if found is None:
            return fail(_pick_reason(issues))
        info["hit_files_after_generic"] = len(found)
        pats = rest
    for p in generic:
        for k in owners[p]:
            result[k]["generic"].add(p)
            result[k]["lower_bound"] = True
            inside = by_key[k].get("path")
            for f, ln in sample_hits[p].items():
                if inside and (f == inside or f.startswith(inside + "/")):
                    continue
                result[k]["files"].setdefault(f, ln)
    pats_b = [(p, p.encode("utf-8", "surrogateescape")) for p in pats]
    for f in found:
        if time.monotonic() > deadline:
            return fail()
        try:
            data = _read_capped(f)
        except OSError as exc:
            # rg 已报命中却读不到（dataless/读失败）：这条引用丢了，不能再当"0 引用"
            issues.append("dataless_skipped" if isinstance(exc, DatalessError) else "read_error")
            continue
        for p, pb in pats_b:
            idx = data.find(pb)
            if idx < 0:
                continue
            ln = data.count(b"\n", 0, idx) + 1
            for k in owners[p]:
                inside = by_key[k].get("path")
                if inside and (f == inside or f.startswith(inside + "/")):
                    continue
                cur = result[k]["files"].get(f)
                if cur is None or ln < cur:
                    result[k]["files"][f] = ln
    if issues:
        info["issues"] = sorted(set(issues))
        fail(_pick_reason(issues))
    return info


def search_references(items, ref_roots, deadline):
    """items: [{'key','patterns','path','scopes'}]。
    返回 ({key: {'checked': bool, 'files': {绝对文件: 首个命中行号}, 'scopes': [范围根],
               'lower_bound': bool, 'generic': set}}, 各范围耗时统计)。
    path 是节点自身目录，其内部的文件不算引用。
    某个模式在抽样里到处都是（如 project-name 这类通用基名）时，不再逐文件归属，
    只用抽样中的真实命中做样例，并把 lower_bound 置 true（count 是下限）。"""
    by_key = {it["key"]: it for it in items}
    all_keys = set(by_key)
    scopes = {}
    for r in ref_roots:
        root = protect.real(r)
        if os.path.isdir(root):
            scopes.setdefault(root, set()).update(all_keys)
    for it in items:
        for s in it.get("scopes", []):
            root = protect.real(s)
            if os.path.isdir(root):
                scopes.setdefault(root, set()).add(it["key"])
    result = {k: {"checked": True, "files": {}, "scopes": [], "lower_bound": False, "generic": set(),
                    "reason": None}
              for k in all_keys}
    # 一个目录都没搜过（没有引用检查目录、条目也不在 git 工作树里）不能报“已查 0 处”：记为未查，
    # plan.py check 按“引用未查”降级（与 probe item-refs 的口径一致）
    searched = {k for ks in scopes.values() for k in ks}
    for k in all_keys - searched:
        result[k]["checked"] = False
        result[k]["reason"] = "no-search-scope"
    stats = []
    for root, keys in scopes.items():
        for k in keys:
            result[k]["scopes"].append(root)
        t_scope = time.monotonic()
        info = _search_scope(root, keys, by_key, result, deadline)
        info["seconds"] = round(time.monotonic() - t_scope, 1)
        stats.append(info)
    return result, stats


def rel_to_scope(f, scopes):
    roots = [s for s in scopes if f.startswith(s + "/")]
    return os.path.relpath(f, max(roots, key=len)) if roots else f


def format_samples(files, scopes, limit=5):
    out = []
    for f in sorted(files):
        item = "%s:%d" % (rel_to_scope(f, scopes), files[f])
        if item not in out:   # 主仓与工作树里的同名同位置副本只列一次
            out.append(item)
        if len(out) >= limit:
            break
    return out


# ───────────────────────── scan ─────────────────────────

def pick_roots(profile):
    roots = []
    for r in profile.get("roots", ["~"]) + profile.get("extra_roots", []):
        rp = protect.real(r)
        if not os.path.isdir(rp):
            continue
        if any(rp == x or rp.startswith(x + "/") for x in roots):
            continue
        roots = [x for x in roots if not x.startswith(rp + "/")] + [rp]
    # 0.6：Claude Code 会话临时目录（存在时）默认也扫；根本身受保护，只有其下会话目录可清
    ct = protect.real(protect.claude_tmp_root())
    if os.path.isdir(ct) and not any(ct == x or ct.startswith(x + "/") for x in roots):
        roots = [x for x in roots if not x.startswith(ct + "/")] + [ct]
    return roots


def match_note(path, notes):
    best = None
    for key, text in notes.items():
        k = protect.real(key)
        if (path == k or path.startswith(k + "/")) and (best is None or len(k) > len(best[0])):
            best = (k, text)
    return best[1] if best else None


def collect_capacity():
    """容量三口径，复用 sysprobe.probe_capacity；任何失败都落成 null，不拖垮扫描。"""
    out = {"statvfs_free": None, "container_free": None, "container_size": None,
           "finder_available": None, "purgeable_estimate": None, "reasons": {}}
    try:
        cap = sysprobe.probe_capacity()
    except Exception as exc:  # noqa: BLE001
        out["reasons"]["probe"] = "容量探针失败：%s" % exc
        return out
    out["statvfs_free"] = cap.get("statvfs_free")
    out["container_free"] = cap.get("apfs_container_free")
    out["container_size"] = cap.get("apfs_container_size")
    out["finder_available"] = cap.get("finder_available")
    if out["finder_available"] is not None and out["statvfs_free"] is not None:
        out["purgeable_estimate"] = cap.get("purgeable_estimate")
    for k, rk in (("finder_available", "finder_reason"), ("container_free", "diskutil_reason"),
                  ("purgeable_estimate", "purgeable_reason")):
        if out[k] is None and cap.get(rk):
            out["reasons"][k] = cap[rk]
    return out


def do_scan(opts):
    t0 = time.monotonic()
    now_ts = time.time()
    reset_read_state()
    profile = protect.load_profile(opts.profile) if opts.profile else protect.load_profile()
    roots = pick_roots(profile)
    runs_dir = os.path.abspath(os.path.expanduser(opts.runs_dir))
    scan_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    run_dir = os.path.join(runs_dir, scan_id)
    os.makedirs(run_dir, mode=0o700, exist_ok=True)

    walker = Walker(opts.threshold, opts.max_depth, t0 + opts.max_seconds, now_ts,
                    skip_firmlink_dup=roots_cover_data_volume(roots),
                    progress_every=getattr(opts, "progress_seconds", PROGRESS_SECONDS), started=t0)
    walked = 0
    for root in roots:
        walked += walker.walk_root(root)
    walk_seconds = time.monotonic() - t0

    nodes = []
    for path, d in walker.nodes.items():
        big_sum = sum(b for _, b in d["big"])
        nodes.append({"path": path, "bytes": d["bytes"], "files": d["files"], "newest_mtime": iso(d["newest"]),
                      "recent_24h": d["recent"], "children_over_threshold": len(d["big"]),
                      "remainder_bytes": max(0, d["bytes"] - big_sum), "frontier": not d["big"],
                      "depth": d["depth"], "bytes_lower_bound": bool(d.get("lower_bound"))})
    nodes.sort(key=lambda n: (-n["bytes"], n["path"]))
    for i, n in enumerate(nodes, 1):
        n["id"] = "n%04d" % i
    frontier = [n for n in nodes if n["frontier"]]

    # git：只对节点做，每个仓库一次 ls-files
    ctx = GitCtx()
    annotate_git(nodes, ctx)

    # 项目标记文件：纯事实字段，不影响任何判定
    for n in nodes:
        pm = find_project_marker(n["path"])
        if pm is not False:
            n["project_marker"] = pm
            if pm is None:
                n["project_marker_checked"] = True

    # lsof：全局一次
    attach_open_by(nodes, None if opts.no_lsof else run_lsof())

    # protected / producer / note / growth
    producers = Producers()
    _APPS_CACHE.clear()   # 每次扫描只枚举一次应用
    apps = installed_apps()
    for n in nodes:
        reason = protect.protected_reason(n["path"], profile)
        n["protected"] = reason is not None
        if reason:
            n["protected_reason"] = reason
        n["producer"] = producers.match(n["path"], apps)
        notes = [x for x in (match_note(n["path"], profile.get("notes", {})), n.pop("git_note", None)) if x]
        n["note"] = "；".join(notes) if notes else None
        n["growth"] = {"prev_scan_id": None, "prev_bytes": None, "delta_bytes": None}

    # 引用检查：只对未受保护的前沿节点
    refs_t0 = time.monotonic()
    ref_stats = []
    default_refs = lambda why: {"checked": False, "count": 0, "samples": [], "reason": why}  # noqa: E731
    # 扫描时实际搜过哪些引用检查目录：plan.py check 据此发现“扫描之后才新增的成品目录”（台账 / 设置），要求补查
    refs_search = {"roots": [] if opts.no_refs else protect.ref_roots_real(profile), "worktree_scopes": False}
    for n in nodes:
        n["refs"] = default_refs("non-frontier" if not n["frontier"] else ("protected" if n["protected"] else "pending"))
    if opts.no_refs:
        for n in frontier:
            n["refs"] = default_refs("--no-refs")
    else:
        todo = [n for n in frontier if not n["protected"]]
        items = []
        for n in todo:
            wt = n["git"]["worktree"]
            items.append({"key": n["id"], "patterns": build_patterns(n["path"], wt), "path": n["path"],
                          "scopes": [wt] if wt else []})
        res, ref_stats = search_references(items, [protect.real(r) for r in profile.get("reference_roots", [])],
                                           refs_t0 + opts.refs_seconds) if items else ({}, [])
        refs_search["worktree_scopes"] = any(it["scopes"] for it in items)
        for n in todo:
            r = res[n["id"]]
            n["refs"] = {"checked": r["checked"], "count": len(r["files"]) if r["checked"] else 0,
                         "samples": format_samples(r["files"], r["scopes"]) if r["checked"] else [],
                         "scopes": [tilde(s) for s in r["scopes"]]}
            if r["checked"] and r["lower_bound"]:
                n["refs"]["count_is_lower_bound"] = True
                n["refs"]["generic_patterns"] = sorted(r["generic"])
            if not r["checked"]:
                n["refs"]["reason"] = r.get("reason") or "timeout-or-error"
    refs_seconds = time.monotonic() - refs_t0

    nodes_out = []
    for n in nodes:
        nodes_out.append({
            "id": n["id"], "path": n["path"], "bytes": n["bytes"], "files": n["files"],
            "newest_mtime": n["newest_mtime"], "recent_24h": n["recent_24h"],
            "children_over_threshold": n["children_over_threshold"], "remainder_bytes": n["remainder_bytes"],
            "frontier": n["frontier"], "depth": n["depth"], "bytes_lower_bound": n["bytes_lower_bound"],
            "git": n["git"], "open_by": n["open_by"], "refs": n["refs"], "producer": n["producer"],
            "growth": n["growth"], "note": n["note"], "protected": n["protected"],
            **({"protected_reason": n["protected_reason"]} if n["protected"] else {}),
            **({"project_marker": n["project_marker"]} if "project_marker" in n else {}),
            **({"project_marker_checked": True} if n.get("project_marker_checked") else {}),
        })

    # 0.6：用户/Agent 登记过的目录身份（profile.identities，键为 realpath）：同路径或最近祖先匹配，只作事实附注
    idents = {protect.real(k): v for k, v in (profile.get("identities") or {}).items() if isinstance(v, dict)}
    if idents:
        for n in nodes_out:
            cur = os.path.realpath(n["path"])
            while True:
                if cur in idents:
                    v = idents[cur]
                    n["identity"] = {"title": v.get("title"), "producer": v.get("producer"), "task": v.get("task")}
                    break
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent

    try:
        du = shutil.disk_usage(HOME)
        disk = {"total": du.total, "free": du.free, "used": du.used}
    except OSError:
        disk = {"total": 0, "free": 0, "used": 0}
    capacity = collect_capacity()   # 放在扫描末尾：swift 取 Finder 口径慢，超时 15 秒
    frontier_bytes = sum(n["bytes"] for n in frontier)
    remainder_bytes = sum(n["remainder_bytes"] for n in nodes if not n["frontier"])
    total_seconds = time.monotonic() - t0
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = os.environ.get("USER", "")
    facts = {
        "schema": SCHEMA, "scan_id": scan_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": {"platform": sys.platform, "user": user, "home": HOME},
        "disk": disk, "capacity": capacity, "threshold": opts.threshold,
        "coverage": {"walked_bytes": walked, "frontier_bytes": frontier_bytes, "incomplete": walker.incomplete,
                     "scan_status": "partial" if (walker.incomplete or walker.partial_skips) else "complete",
                     "partial_count": walker.partial_skips,
                     "inaccessible_count": walker.perm_count + sum(1 for x in READ_SKIPS if x.get("reason") == "permission"),
                     "seconds": round(total_seconds, 1), "walk_seconds": round(walk_seconds, 1),
                     "refs_seconds": round(refs_seconds, 1), "remainder_bytes": remainder_bytes,
                     "dirs": walker.dirs, "files": walker.files, "roots": roots, "max_depth": opts.max_depth,
                     "refs_scopes": ref_stats, "refs_search": refs_search, "icloud_desktop_documents": icloud_desktop_documents()},
        "nodes": nodes_out, "skipped": walker.skipped + READ_SKIPS,
    }
    if READ_WARNINGS:
        facts["coverage"]["warnings"] = list(dict.fromkeys(READ_WARNINGS))
    if walker.skipped_overflow:
        facts["coverage"]["skipped_overflow"] = walker.skipped_overflow
    try:
        facts["disk"]["device"] = os.stat(HOME).st_dev
        space_history.make(run_dir, facts)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # 记录失败不丢扫描；明确报告，不能静默宣称已有增长基准。
        facts["coverage"].setdefault("warnings", []).append("空间记录点未生成：" + str(exc))
        print("警告：空间记录点未生成：" + str(exc), file=sys.stderr)
    out_path = os.path.join(run_dir, "facts.json")
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(dump_json(facts))
    os.replace(tmp_path, out_path)
    return run_dir, facts


human = units.fmt_bytes   # 十进制 GB（与访达、成果页一致），见 units.py


def print_summary(run_dir, facts, top=30):
    cov = facts["coverage"]
    nodes = facts["nodes"]
    frontier = [n for n in nodes if n["frontier"]]
    print(run_dir)
    if os.path.isfile(os.path.join(run_dir, "growth.html")):
        print("空间记录与增长调查：" + os.path.join(run_dir, "growth.html"))
    ratio = (cov["frontier_bytes"] / cov["walked_bytes"] * 100) if cov["walked_bytes"] else 0.0
    print("扫描 %s：遍历 %s（%d 目录 / %d 文件），前沿节点合计 %s，覆盖 %.1f%%；用时 %.0fs（遍历 %.0fs，引用检查 %.0fs）%s" % (
        facts["scan_id"], human(cov["walked_bytes"]), cov["dirs"], cov["files"], human(cov["frontier_bytes"]),
        ratio, cov["seconds"], cov["walk_seconds"], cov["refs_seconds"],
        "；未完整（遍历超时）" if cov["incomplete"] else ""))
    print("节点共 %d 个（前沿 %d），跳过项 %d 个%s；磁盘已用 %s / %s，可用 %s" % (
        len(nodes), len(frontier), len(facts["skipped"]) + cov.get("skipped_overflow", 0),
        "" if not facts["skipped"] else "（" + "、".join(sorted({s["reason"] for s in facts["skipped"]})) + "）",
        human(facts["disk"]["used"]), human(facts["disk"]["total"]), human(facts["disk"]["free"])))
    print("磁盘口径：上行「磁盘已用/可用」是 HOME 所在卷 shutil.disk_usage（POSIX 口径，不含可清除空间）")
    cap = facts.get("capacity") or {}
    fmt = lambda v: human(v) if v is not None else "测不到"  # noqa: E731
    print("容量三口径：statvfs 可用 %s；APFS 容器 可用 %s / 总 %s；Finder 可用 %s；可清除空间估算 %s"
          "（purgeable 是估算 = Finder - statvfs；Finder 口径取不到时为空，不等于 0）" % (
              fmt(cap.get("statvfs_free")), fmt(cap.get("container_free")), fmt(cap.get("container_size")),
              fmt(cap.get("finder_available")), fmt(cap.get("purgeable_estimate"))))
    print("覆盖状态：%s；无法读取 %d 处" % (cov.get("scan_status", "?"), cov.get("inaccessible_count", 0)))
    if cov.get("scan_status") == "partial":
        n_in = cov.get("inaccessible_count", 0)
        if n_in:
            print("提示：有 %d 处无法读取，体积是下限，不是 0（带 bytes_lower_bound 的节点更是如此）" % n_in)
        else:
            print("提示：有 %d 处因云盘/超时等未计入，相关体积是下限，不是 0" % cov.get("partial_count", 0))
    bad_scopes = [x for x in cov.get("refs_scopes") or [] if not x.get("ok")]
    if bad_scopes:
        unchecked = sum(1 for n in frontier if not (n.get("refs") or {}).get("checked"))
        print("警告：引用检查有 %d 个搜索根没查完（用时 %.0fs），%d 个前沿节点记为“引用未查”，"
              "写计划时它们只能保留或待定。请加大预算重扫（默认 900 秒）：scan.py scan --refs-seconds 1800" % (
                  len(bad_scopes), cov.get("refs_seconds") or 0, unchecked))
    no_scope = sum(1 for n in frontier if (n.get("refs") or {}).get("reason") == "no-search-scope")
    if no_scope:
        print("警告：没有可搜的引用检查目录（设置里的引用检查目录为空，这些节点也不在 git 工作树里），"
              "%d 个前沿节点记为“引用未查”，写计划时它们只能保留或待定；"
              "先在设置 / 台账里补上成品所在目录，再用 scan.py probe item-refs 补查" % no_scope)
    print("前沿节点（按大小前 %d）：" % min(top, len(frontier)))
    for n in sorted(frontier, key=lambda x: -x["bytes"])[:top]:
        refs = n["refs"]
        rtxt = ("引用%d" % refs["count"]) if refs.get("checked") else "引用未查"
        print("  %8s  %-9s %-8s %s  %s%s%s" % (
            human(n["bytes"]), n["git"]["role"], rtxt, tilde(n["path"]),
            "[" + (n["producer"]["card"] or "-") + "]",
            " 受保护" if n["protected"] else "",
            " 在用" if n["open_by"] and n["open_by"] != "unknown" else ""))


# ───────────────────────── probe ─────────────────────────

UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


def _read_line_capped(f, cap):
    """读一行，最多 cap 字节；超长则吞掉剩余部分，返回 (bytes, 是否被截断)。"""
    line = f.readline(cap)
    if line and not line.endswith(b"\n") and len(line) >= cap:
        while True:
            more = f.readline(1 << 20)
            if not more or more.endswith(b"\n"):
                break
        return line, True
    return line, False


def _first_text(payload):
    content = payload.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
    if isinstance(content, str):
        return content
    if isinstance(payload.get("message"), str):
        return payload["message"]
    return None


def read_rollout_head(path, max_lines=400, max_bytes=8 * 1024 * 1024):
    """流式读取 rollout：首行 session_meta 与第一条真实用户消息，读到即停。"""
    if str(path).endswith(".jsonl.zst"):
        return codex_state.compressed_metadata(path), None
    meta = {}
    first_user = None
    read = 0
    try:
        with open(path, "rb") as f:
            line, truncated = _read_line_capped(f, 4 * 1024 * 1024)
            read += len(line)
            try:
                d = json.loads(line) if not truncated else None
            except ValueError:
                d = None
            if d and isinstance(d.get("payload"), dict):
                meta = d["payload"]
            elif line:
                text = decode(line)
                for key in ("id", "cwd", "source", "parent_thread_id"):
                    m = re.search(r'"%s"\s*:\s*"([^"]{1,300})"' % key, text)
                    if m:
                        meta[key] = m.group(1)
            for _ in range(max_lines):
                if read > max_bytes:
                    break
                line, truncated = _read_line_capped(f, 1024 * 1024)
                read += len(line)
                if not line:
                    break
                if truncated:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                p = d.get("payload")
                if not isinstance(p, dict):
                    continue
                is_user = (d.get("type") == "response_item" and p.get("type") == "message"
                           and p.get("role") == "user") or (d.get("type") == "event_msg"
                                                             and p.get("type") == "user_message")
                if not is_user:
                    continue
                text = _first_text(p)
                if not text:
                    continue
                s = text.lstrip()
                if s.startswith("# AGENTS.md") or s.startswith("<"):
                    continue
                first_user = " ".join(s.split())[:120]
                break
    except OSError:
        pass
    return meta, first_user


def _source_info(meta):
    src = meta.get("source")
    parent = meta.get("parent_thread_id")
    kind = src if isinstance(src, str) else None
    if isinstance(src, dict):
        sub = src.get("subagent")
        if isinstance(sub, dict):
            kind = "subagent"
            spawn = sub.get("thread_spawn")
            if isinstance(spawn, dict) and spawn.get("parent_thread_id"):
                parent = parent or spawn["parent_thread_id"]
        else:
            kind = next(iter(src), "unknown")
    return kind, parent


def probe_codex(opts):
    base = os.path.join(HOME, ".codex")
    lifecycle, lifecycle_errors = codex_state.latest_turns(base)
    index = {}
    try:
        with open(os.path.join(base, "session_index.jsonl"), "rb") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                sid = d.get("id")
                if not sid:
                    continue
                cur = index.get(sid)
                if cur is None or (d.get("updated_at") or "") >= (cur.get("updated_at") or ""):
                    index[sid] = {"thread_name": d.get("thread_name"), "updated_at": d.get("updated_at")}
    except OSError:
        pass
    lsof_entries = run_lsof()
    open_paths = set()
    if lsof_entries:
        open_paths = {name for _, _, name in lsof_entries if name.startswith(base + "/")}

    rollouts = []
    rollout_ids = set()
    for sub in ("sessions", "archived_sessions"):
        top = os.path.join(base, sub)
        for dirpath, _, filenames in os.walk(top):
            for fn in filenames:
                if not (fn.startswith("rollout-") and fn.endswith((".jsonl", ".jsonl.zst"))):
                    continue
                p = os.path.join(dirpath, fn)
                m = UUID_RE.search(fn)
                if m:
                    rollout_ids.add(m.group(1).lower())
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                rollouts.append((p, st, sub))
    total_bytes = sum(st.st_blocks * 512 for _, st, _ in rollouts)
    listed = []
    for p, st, sub in rollouts:
        size = st.st_blocks * 512
        if size < opts.min_bytes:
            continue
        meta, first_user = read_rollout_head(p)
        m = UUID_RE.search(os.path.basename(p))
        sid = meta.get("id") or (m.group(1) if m else None)
        kind, parent = _source_info(meta)
        idx = index.get(sid, {})
        listed.append({
            "path": p, "location": sub, "id": sid, "bytes": size,
            "mtime": iso(st.st_mtime), "cwd": meta.get("cwd"), "source": kind,
            "originator": meta.get("originator"), "cli_version": meta.get("cli_version"),
            "parent_thread_id": parent, "is_subagent": bool(parent) or kind == "subagent",
            "thread_name": idx.get("thread_name"), "index_updated_at": idx.get("updated_at"),
            "first_user_message": first_user, "open": p in open_paths,
            "last_turn": lifecycle.get(sid), "task_finished": None,
            "metadata_status": meta.get("metadata_status", "rollout"),
            "metadata_errors": meta.get("metadata_errors", []), "archived": meta.get("archived"),
        })
    listed.sort(key=lambda x: -x["bytes"])

    images = []
    img_root = os.path.join(base, "generated_images")
    try:
        entries = [e for e in os.scandir(img_root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        entries = []
    sized = []
    for e in entries:
        b, files = tree_size(e.path)
        sized.append((b, files, e))
    sized.sort(key=lambda x: -x[0])
    img_total = sum(b for b, _, _ in sized)
    top_imgs = sized[:opts.top]
    profile = protect.load_profile(opts.profile) if getattr(opts, "profile", None) else protect.load_profile()
    ref_roots = [protect.real(r) for r in profile.get("reference_roots", [])]
    items = [{"key": e.name, "patterns": ["generated_images/" + e.name], "path": None, "scopes": []}
             for _, _, e in top_imgs]
    refs = search_references(items, ref_roots, time.monotonic() + 180)[0] if items and ref_roots else {}
    for b, files, e in top_imgs:
        r = refs.get(e.name)
        images.append({
            "id": e.name, "path": e.path, "files": files, "bytes": b,
            "has_rollout": e.name.lower() in rollout_ids,
            "referenced_by": sorted({rel_to_scope(f, r["scopes"]) for f in r["files"]})[:20] if r else [],
            "refs_checked": bool(r and r["checked"]) if ref_roots else True,
        })
    return {
        "kind": "codex", "generated_at": iso(time.time()),
        "summary": {
            "rollout_files": len(rollouts), "rollout_bytes": total_bytes,
            "listed_min_bytes": opts.min_bytes, "listed_count": len(listed),
            "listed_bytes": sum(x["bytes"] for x in listed),
            "listed_subagent": sum(1 for x in listed if x["is_subagent"]),
            "open_count": sum(1 for x in listed if x["open"]),
            "generated_images_dirs": len(entries), "generated_images_bytes": img_total,
            "generated_images_listed": len(images), "lsof_available": lsof_entries is not None,
            "reference_roots": [tilde(r) for r in ref_roots],
        },
        "sessions": listed, "generated_images": images, "lifecycle_errors": lifecycle_errors,
    }


# ----------------------------------------------------------------- Codex 会话分组

RECENT_SECONDS = 24 * 3600
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".next", ".turbo"}


def _sqlite_ro(path):
    import sqlite3
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.execute("PRAGMA query_only=1")
    return con


def _table_cols(con, table):
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except Exception:  # noqa: BLE001  表不存在或库被锁
        return set()


def codex_history_bytes(base):
    """{线程ID: 历史库中该线程条目的字节数}。表结构随版本变化，认不出就返回空字典。"""
    out = {}
    for db in sorted(glob_files(base, "thread_history_", ".sqlite")):
        try:
            con = _sqlite_ro(db)
            cols = _table_cols(con, "thread_items")
            if not {"thread_id", "item_json"} <= cols:
                con.close()
                continue
            for tid, b in con.execute(
                    "SELECT thread_id, sum(length(CAST(item_json AS BLOB))) FROM thread_items GROUP BY thread_id"):
                if tid:
                    out[str(tid).lower()] = out.get(str(tid).lower(), 0) + (b or 0)
            con.close()
        except Exception:  # noqa: BLE001
            continue
    return out


def codex_relations(base):
    """父子关系与线程 → rollout 路径（派生边与 session_meta 父线程取并集，见 codex_state.py）。"""
    rel = codex_state.relations(os.path.dirname(base))
    return {p: sorted(cs) for p, cs in rel["children"].items()}, rel["parent"], rel["rollout"]


def glob_files(base, prefix, suffix):
    try:
        return [os.path.join(base, n) for n in os.listdir(base) if n.startswith(prefix) and n.endswith(suffix)]
    except OSError:
        return []


def _project_root(cwd):
    """cwd 所在的 git 仓库根；cwd 已不存在时向上找最近的现存祖先。"""
    p = cwd
    while p and p != "/" and not os.path.isdir(p):
        p = os.path.dirname(p)
    if not p or p == "/" or not within_home(p):
        return None
    rc, out, _ = run(["git", "-C", p, "rev-parse", "--show-toplevel"], timeout=20, env=git_env())
    out = decode(out).strip()
    root = out if rc == 0 and out else p
    return root if root != HOME else p


def within_home(p):
    return p == HOME or p.startswith(HOME + "/")


def collect_codex_threads(base, with_lsof=True):
    lsof_entries = run_lsof() if with_lsof else None
    open_paths = {name for _, _, name in lsof_entries} if lsof_entries else set()
    now = time.time()
    threads = {}
    for sub in ("sessions", "archived_sessions"):
        for dirpath, _, filenames in os.walk(os.path.join(base, sub)):
            for fn in filenames:
                if not (fn.startswith("rollout-") and fn.endswith((".jsonl", ".jsonl.zst"))):
                    continue
                m = UUID_RE.search(fn)
                if not m:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                meta, _ = read_rollout_head(p, max_lines=0)
                kind, parent = _source_info(meta)
                tid = m.group(1).lower()
                threads[tid] = {
                    "id": tid, "path": p, "bytes": st.st_blocks * 512, "mtime": st.st_mtime,
                    "cwd": meta.get("cwd"), "source": kind, "originator": meta.get("originator"),
                    "parent": (parent or "").lower() or None, "open": p in open_paths,
                    "recent": st.st_mtime > now - RECENT_SECONDS,
                }
    return threads, lsof_entries is not None


def _descendants(tid, children):
    """全部后代，子孙在前、父在后（安全的删除顺序）。"""
    return codex_state.descendants(tid, children)


def codex_groups(base):
    threads, lsof_ok = collect_codex_threads(base)
    hist = codex_history_bytes(base)
    children, parent_of, rollout_of = codex_relations(base)
    img_root = os.path.join(base, "generated_images")
    groups = {}
    for t in threads.values():
        img = os.path.join(img_root, t["id"])
        t["images_dir"] = img if os.path.isdir(img) else None
        t["image_bytes"] = tree_size(img)[0] if t["images_dir"] else 0
        t["history_bytes"] = hist.get(t["id"], 0)
    for t in threads.values():
        # 子 Agent 会话归到最上层父会话所在的组：删父会连带删子，二者必须一起判断
        root, hops = t, 0
        while hops < 50:
            pid = parent_of.get(root["id"]) or root.get("parent")
            if not pid or pid not in threads:
                break
            root, hops = threads[pid], hops + 1
        key = f"{root['source']}|{root['originator'] or '-'}|{_cwd_family(root['cwd']) or '-'}"
        groups.setdefault(key, []).append(t)
    return groups, threads, children, parent_of, rollout_of, lsof_ok


SCRATCH_RE = re.compile(r"^(.*?/scratchpad)(?:/|$)")


def _cwd_family(cwd):
    """同一个 Agent 临时目录（…/scratchpad/子目录）下的批量任务合成一组。"""
    if not cwd:
        return cwd
    m = SCRATCH_RE.match(cwd)
    return m.group(1) if m else cwd


def _group_summary(i, key, ts, children, threads):
    ids = {t["id"] for t in ts}
    kids_out = sum(1 for t in ts for c in children.get(t["id"], []) if c not in ids and c in threads)
    total = sum(t["bytes"] + t["history_bytes"] + t["image_bytes"] for t in ts)
    roots = [t for t in ts if not t["parent"]] or ts
    head = roots[0]
    cwd = _cwd_family(head["cwd"])
    cwds = {}
    for t in ts:
        cwds[t["cwd"] or "-"] = cwds.get(t["cwd"] or "-", 0) + 1
    samples = []
    for t in sorted(ts, key=lambda x: -x["mtime"])[:3]:
        _, first = read_rollout_head(t["path"], max_lines=200, max_bytes=2 * 1024 * 1024)
        samples.append({"id": t["id"], "first_user_message": first})
    return {
        "index": i, "key": key, "source": head["source"], "originator": head["originator"],
        "cwd": cwd, "cwd_exists": bool(cwd and os.path.isdir(cwd)),
        "cwd_breakdown": sorted(cwds.items(), key=lambda kv: -kv[1])[:8],
        "project_root": _project_root(cwd) if cwd else None,
        "threads": len(ts), "bytes_total": total,
        "rollout_bytes": sum(t["bytes"] for t in ts),
        "history_bytes": sum(t["history_bytes"] for t in ts),
        "image_dirs": sum(1 for t in ts if t["images_dir"]),
        "image_bytes": sum(t["image_bytes"] for t in ts),
        "first_activity": iso(min(t["mtime"] for t in ts)), "last_activity": iso(max(t["mtime"] for t in ts)),
        "open": sum(1 for t in ts if t["open"]), "recent_24h": sum(1 for t in ts if t["recent"]),
        "subagents": sum(1 for t in ts if t["parent"]),
        "children_outside_group": kids_out,
        "samples": samples,
    }


def _sha256(path):
    import hashlib
    if is_dataless(path):
        _note_read_skip(path)
        raise OSError("dataless: %s" % path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def find_copies(images, roots, deadline):
    """images: [图片路径]。在 roots 内按"大小相同 → sha256 相同"找逐字节相同的副本。
    返回 ({图片路径: 副本路径}, 是否在时限内查完)。~/.codex 自身不算副本。"""
    want = {}
    for p in images:
        try:
            want.setdefault(os.lstat(p).st_size, []).append(p)
        except OSError:
            pass
    codex = os.path.join(HOME, ".codex")
    cand = {}
    complete = True
    for root in roots:
        if _skip_kind(root):
            READ_WARNINGS.append("副本查找根位于云盘/占位目录内，已跳过：%s" % root)
            _note_read_skip(root)
            complete = False       # 根没搜过：不能把"没找到副本"当结论
            continue
        for dp, dns, fns in os.walk(root):
            if time.monotonic() > deadline:
                complete = False
                break
            keep = []
            for d in dns:
                full = os.path.join(dp, d)
                if d in SKIP_DIRS or protect._within(full, codex):
                    continue
                kind = _skip_kind(full)
                if kind:
                    if kind != "cloud":     # 常规云盘排除不影响结论；dataless / 桌面与文稿漏看了就是没查完
                        _note_read_skip(full, "dataless" if kind == "dataless" else "cloud-excluded")
                        complete = False
                    continue
                keep.append(d)
            dns[:] = keep
            for fn in fns:
                fp = os.path.join(dp, fn)
                try:
                    st = os.lstat(fp)
                except OSError:
                    continue
                if statmod.S_ISREG(st.st_mode) and st.st_size in want:
                    cand.setdefault(st.st_size, []).append(fp)
    found = {}
    hashed = {}
    for size, srcs in want.items():
        if size not in cand:
            continue
        by_hash = {}
        for c in cand[size]:
            if time.monotonic() > deadline:
                return found, False
            try:
                by_hash.setdefault(hashed.get(c) or hashed.setdefault(c, _sha256(c)), c)
            except OSError:
                complete = False   # 候选副本读不了（dataless 等）：可能漏了真副本
                continue
        for s in srcs:
            try:
                h = _sha256(s)
            except OSError:
                complete = False
                continue
            if h in by_hash:
                found[s] = by_hash[h]
    return found, complete


def _list_images(d):
    out = []
    for dp, _, fns in os.walk(d):
        out.extend(os.path.join(dp, f) for f in fns)
    return out


INVENTORY_MIN = 10       # 提到超过这么多个同组线程号的会话视为盘点清单，不算依赖
REJECT_TOKENS = {"rejected", "reject", "failed", "fail", "trash", "discard", "discarded", "tmp", "temp", "scratch"}
TOKEN_SPLIT = re.compile(r"[^0-9a-z]+")


def _rejectish(segment):
    seg = segment.lower()
    return "废" in seg or any(t in REJECT_TOKENS or t.startswith("reject") for t in TOKEN_SPLIT.split(seg) if t)


def _copy_locations(paths):
    """{副本路径: 'tracked' | 'untracked' | 'rejected'}。tracked=在 git 仓库里被跟踪；
    rejected=某一级目录名是 rejected / failed / tmp / scratch 一类（那一份本身多半也是废稿）。"""
    out, tracked_by_repo = {}, {}
    for p in paths:
        if any(_rejectish(s) for s in p.split("/")[:-1]):
            out[p] = "rejected"
            continue
        d = os.path.dirname(p)
        rc, top, _ = run(["git", "-C", d, "rev-parse", "--show-toplevel"], timeout=20, env=git_env())
        top = decode(top).strip() if rc == 0 else ""
        if not top:
            out[p] = "untracked"
            continue
        if top not in tracked_by_repo:
            rc, raw, _ = run(["git", "-C", top, "ls-files", "-z"], timeout=120, env=git_env())
            tracked_by_repo[top] = {os.path.join(top, decode(x)) for x in raw.split(b"\0") if x} if rc == 0 else set()
        out[p] = "tracked" if p in tracked_by_repo[top] else "untracked"
    return out


def _last_event(path, tail=256 * 1024):
    """rollout 最后一条事件的类型（如 task_complete / error / turn_aborted），读不出返回 None。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail))
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        p = d.get("payload") if isinstance(d, dict) else None
        if isinstance(p, dict) and p.get("type"):
            return p["type"]
        if isinstance(d, dict) and d.get("type"):
            return d["type"]
    return None


def session_mentions(ids, session_roots, threads, deadline):
    """在 Codex 会话 rollout（sessions 与 archived_sessions）里找这些线程号——"接着那个会话做"是真实依赖。
    返回 ({线程: [提到它的线程]}, 是否查完, [盘点式会话], {线程: [列过它的、仍在运行的盘点式会话]})。
    提到超过 INVENTORY_MIN 个线程号的会话视为盘点清单，不算依赖。仍在运行的盘点式会话单独标出，
    由 Agent 读它的首句确认是盘点/清理而不是批量驱动；驱动会话还在跑时，它派出的线程通常也在
    24 小时内，会先被判为 active。"""
    roots = [r for r in session_roots if os.path.isdir(r)]
    safe = [r for r in roots if not _skip_kind(r)]
    for r in roots:
        if r not in safe:
            READ_WARNINGS.append("会话根位于云盘/占位目录内，已跳过：%s" % r)
    all_searched = len(safe) == len(roots)     # 有根被跳过：结果只是下限，不能报"查完"
    roots = safe
    if not RG or not ids or not roots:
        return {}, False, [], {}
    fd, pf = tempfile.mkstemp(prefix="sesspat-", suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(ids) + "\n")
    try:
        remaining = max(1, deadline - time.monotonic())
        rc, out, _ = run([RG, "-F", "-o", "-i", "--no-ignore", "--hidden", "--no-line-number", "--with-filename",
                          "--no-messages", "-g", "rollout-*.jsonl"] + _rg_exclude_args() + ["-f", pf, "--"] + roots,
                         timeout=remaining, cwd=_rg_cwd())
    finally:
        os.unlink(pf)
    if rc is None or rc not in (0, 1):
        return {}, False, [], {}
    found, per_src = {}, {}
    for line in decode(out).splitlines():
        path, _, hit = line.rpartition(":")
        m = UUID_RE.search(os.path.basename(path))
        if not m:
            continue
        src, hit = m.group(1).lower(), hit.lower()
        if src == hit:
            continue
        found.setdefault(hit, set()).add(src)
        per_src.setdefault(src, set()).add(hit)

    def live(src):
        t = threads.get(src)
        return bool(t and (t["open"] or t["recent"]))

    inventory = {src for src, hits in per_src.items() if len(hits) > INVENTORY_MIN}
    mentions = {k: sorted(v - inventory) for k, v in found.items()}
    listed_live = {}
    for src in inventory:
        if live(src):
            for hit in per_src[src]:
                listed_live.setdefault(hit, []).append(src)
    return ({k: v for k, v in mentions.items() if v}, all_searched,
            [{"id": src, "threads": len(per_src[src]), "live": live(src)} for src in sorted(inventory)],
            listed_live)


def trace_group(ts, threads, children, parent_of, rollout_of, roots, seconds, batch=True):
    """给组内每个顶层线程分类。子会话随父线程计入 descendants：不单独分类、不重复计体积；
    整棵树的原图一起参与分类（emit 也会一起删）。

    批量出图组（source=exec）：
      active           树内任一线程正在打开 / 24 小时内改过
      copied           树内每张原图都有逐字节相同的副本，且副本都被 git 跟踪
      copied_elsewhere 每张原图都有副本，但有副本没被跟踪或在 rejected / tmp 一类目录
      partial          部分原图有副本
      mentioned        有原图、没有副本，但有文件或其他会话提到这个线程号
      unmentioned      有原图，没有副本也没人提到
      empty            树内没有原图（多为生成失败，看 last_event）
      unverified       副本 / 引用 / 会话检索有一项没查完（超时、出错或没有搜索范围），不下结论、不出条目
    交互对话组（其他 source）：active / conversation / unverified（会话检索没查完），逐个会话判断。
    三项检索各自有 seconds 秒时限，互不挤占。"""
    ids_in = {t["id"] for t in ts}
    tops = [t for t in ts if parent_of.get(t["id"]) not in ids_in]
    trees = {t["id"]: _descendants(t["id"], children) for t in tops}

    def tree_images(tid):
        out = []
        for x in [tid] + trees[tid]:
            d = (threads.get(x) or {}).get("images_dir")
            if d:
                out.extend(_list_images(d))
        return out

    imgs = {t["id"]: tree_images(t["id"]) for t in tops}
    copies, copies_ok = (find_copies([p for v in imgs.values() for p in v], roots, time.monotonic() + seconds)
                         if batch and any(imgs.values()) else ({}, True))
    where = _copy_locations(sorted(set(copies.values()))) if copies else {}
    items = [{"key": t["id"], "patterns": [t["id"]], "path": None, "scopes": []} for t in tops]
    refs, _ = search_references(items, roots, time.monotonic() + seconds) if roots else ({}, [])
    codex = os.path.join(HOME, ".codex")
    smen, smen_ok, inventory, listed_live = session_mentions(
        [t["id"] for t in tops], [os.path.join(codex, "sessions"), os.path.join(codex, "archived_sessions")],
        threads, time.monotonic() + seconds)
    out = []
    for t in tops:
        tree = trees[t["id"]]
        tree_set = set(tree) | {t["id"]}
        tree_threads = [threads.get(c) for c in tree]
        busy = t["open"] or t["recent"] or any(x and (x["open"] or x["recent"]) for x in tree_threads)
        r = refs.get(t["id"]) if roots else None
        refs_ok = bool(r and r.get("checked"))
        mentions = sorted(r["files"]) if r else []
        by_sessions = [s for s in smen.get(t["id"], []) if s not in tree_set]
        live_sessions = [s for s in by_sessions if threads.get(s) and (threads[s]["open"] or threads[s]["recent"])]
        mine = imgs[t["id"]]
        copied = [p for p in mine if p in copies]
        locs = sorted({where.get(copies[p], "untracked") for p in copied})
        verified = copies_ok and refs_ok and smen_ok
        if busy:
            cls = "active"
        elif not batch:
            cls = "conversation" if smen_ok else "unverified"
        elif not mine:
            cls = "empty"
        elif copied and len(copied) == len(mine):
            cls = "copied" if locs == ["tracked"] else "copied_elsewhere"
        elif not verified:
            cls = "unverified"
        elif copied:
            cls = "partial"
        elif mentions or by_sessions:
            cls = "mentioned"
        else:
            cls = "unmentioned"
        rec = {
            "id": t["id"], "class": cls, "images": len(mine), "copied": len(copied),
            "copy_where": locs, "copy_sample": [copies[p] for p in copied[:2]],
            "mentions": len(mentions), "mention_sample": mentions[:3],
            "mentioned_by_sessions": by_sessions[:5], "mentioned_by_live_sessions": live_sessions[:5],
            "listed_by_live_inventory": [x for x in listed_live.get(t["id"], []) if x not in tree_set][:3],
            "checked": {"copies": copies_ok, "refs": refs_ok, "sessions": smen_ok},
            "descendants": [{"id": c, "path": (threads.get(c) or {}).get("path") or rollout_of.get(c)}
                            for c in tree],
            "bytes": t["bytes"] + t["history_bytes"] + t["image_bytes"]
                     + sum((x or {}).get("bytes", 0) + (x or {}).get("history_bytes", 0)
                           + (x or {}).get("image_bytes", 0) for x in tree_threads),
        }
        if batch:
            rec["last_event"] = _last_event(t["path"])
        else:
            _, first = read_rollout_head(t["path"], max_lines=200, max_bytes=2 * 1024 * 1024)
            rec["first_user_message"] = first
            rec["last_activity"] = iso(max([t["mtime"]] + [x["mtime"] for x in tree_threads if x]))
        out.append(rec)
    return out, {"copies": copies_ok, "refs": all(x["checked"]["refs"] for x in out) if out else bool(roots),
                 "sessions": smen_ok, "inventory_sessions": inventory}


NO_EMIT = {"active", "unverified"}
# 条目的 class（0.3，plan.py 用它拦「交互会话不得判 clean」）：由探针按来源分类给出，不由 Agent 手填。
# 交互对话组的会话 → interactive；批量出图（exec）组的各分类 → batch；其余判不出 → unknown。
BATCH_CLASSES = {"copied", "copied_elsewhere", "partial", "mentioned", "unmentioned", "empty"}


def item_class(thread_class):
    if thread_class == "conversation":
        return "interactive"
    return "batch" if thread_class in BATCH_CLASSES else "unknown"


def emit_items(traced, threads, cls=None, only=None):
    """把线程转成 plan.json 可直接引用的 items：后代在前、父线程在后，最后是整棵树的原图目录。
    cls 选一类（批量组）；only 选一个线程（交互组逐个会话出卡）。
    每个条目带 class（interactive/batch/unknown）与 session（所属顶层会话 id；后代与原图目录同属一个会话，
    plan.py 按 session 去重计「一张卡几个交互会话」）。"""
    items, seen, dirs = [], set(), []
    for x in traced:
        if x["class"] in NO_EMIT:
            continue
        if (cls is not None and x["class"] != cls) or (only is not None and x["id"] != only):
            continue
        kind = item_class(x["class"])
        for d in x["descendants"] + [{"id": x["id"], "path": threads[x["id"]]["path"]}]:
            if d["id"] in seen or not d["path"] or not os.path.exists(d["path"]):
                continue
            seen.add(d["id"])
            items.append({"path": d["path"], "method": "native", "argv": ["codex", "delete", "--force", d["id"]],
                          "class": kind, "session": x["id"]})
            t = threads.get(d["id"])
            if t and t.get("images_dir"):
                dirs.append((t["images_dir"], kind, x["id"]))
    items += [{"path": d, "method": "delete", "scope": "all", "class": k, "session": sid} for d, k, sid in dirs]
    return items


def _group_tag(key):
    import hashlib
    return hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()[:8]


def probe_codex_groups(opts):
    base = os.path.join(HOME, ".codex")
    groups, threads, children, parent_of, rollout_of, lsof_ok = codex_groups(base)
    ordered = sorted(groups.items(), key=lambda kv: -sum(
        t["bytes"] + t["history_bytes"] + t["image_bytes"] for t in kv[1]))
    summaries = [dict(_group_summary(i, k, ts, children, threads), tag=_group_tag(k))
                 for i, (k, ts) in enumerate(ordered)
                 if sum(t["bytes"] + t["history_bytes"] + t["image_bytes"] for t in ts) >= opts.min_bytes]
    result = {
        "kind": "codex-groups", "generated_at": iso(time.time()), "lsof_available": lsof_ok,
        "summary": {"threads": len(threads), "groups": len(groups),
                    "bytes_total": sum(t["bytes"] + t["history_bytes"] + t["image_bytes"] for t in threads.values()),
                    "listed_groups": len(summaries), "listed_min_bytes": opts.min_bytes},
        "groups": summaries,
    }
    if opts.trace is None:
        return result
    if not 0 <= opts.trace < len(ordered):
        raise SystemExit(f"--trace 超出范围：共 {len(ordered)} 组（按体积降序，从 0 开始）")
    key, ts = ordered[opts.trace]
    s = dict(_group_summary(opts.trace, key, ts, children, threads), tag=_group_tag(key))
    profile = protect.load_profile(opts.profile) if opts.profile else protect.load_profile()
    roots = [r for r in [s["project_root"]] + [protect.real(x) for x in profile.get("reference_roots", [])]
             + [protect.real(x) for x in (opts.search or [])] if r and os.path.isdir(r)]
    roots = sorted(set(roots), key=len)
    roots = [r for i, r in enumerate(roots) if not any(r.startswith(o + "/") for o in roots[:i])]
    batch = s["source"] == "exec"
    traced, complete = trace_group(ts, threads, children, parent_of, rollout_of, roots,
                                   opts.trace_seconds, batch=batch)
    classes = {}
    for x in traced:
        c = classes.setdefault(x["class"], {"threads": 0, "bytes": 0})
        c["threads"] += 1
        c["bytes"] += x["bytes"]
    result["trace"] = {"group": s, "kind": "batch" if batch else "conversation",
                       "search_roots": [tilde(r) for r in roots], "search_complete": complete,
                       "classes": classes, "threads": traced}
    if not roots:
        result["trace"]["warning"] = "没有搜索范围（工作目录不在家目录下的仓库里、profile 也没有 reference_roots），引用无法核对，有图的线程都记为 unverified；用 --search 指定成品所在目录"
    if opts.emit:
        os.makedirs(opts.emit, exist_ok=True)
        prefix = f"codex-{s['tag']}-"
        for old in os.listdir(opts.emit):          # 同组上次的结果先清掉，免得被当成本次结论引用
            if old.startswith(prefix) and old.endswith(".json"):
                os.unlink(os.path.join(opts.emit, old))
        files, held = {}, []

        def write(name, its):
            fp = os.path.join(opts.emit, prefix + name + ".json")
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(its, f, ensure_ascii=False, indent=1)
            return {"file": fp, "items": len(its)}

        if batch:
            for cls in ("copied", "copied_elsewhere", "partial", "mentioned", "unmentioned", "empty"):
                its = emit_items(traced, threads, cls=cls)
                if its:
                    files[cls] = write(cls, its)
        else:
            # 交互对话不按分类批量出条目：每个会话一个文件，逐个出卡、逐个问；被仍在运行的会话引用的不出
            for x in traced:
                if x["class"] in NO_EMIT:
                    continue
                if x["mentioned_by_live_sessions"]:
                    held.append(x["id"])
                    continue
                its = emit_items(traced, threads, only=x["id"])
                if its:
                    files[x["id"]] = write("thread-" + x["id"], its)
        manifest = {"group_key": key, "group_index": opts.trace, "generated_at": iso(time.time()),
                    "search_complete": complete, "files": files, "held_by_live_sessions": held}
        with open(os.path.join(opts.emit, prefix + "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        result["trace"]["emitted"] = files
        result["trace"]["held_by_live_sessions"] = held
    return result


def probe_claude(opts):
    base = os.path.join(HOME, ".claude")
    projects = []
    proot = os.path.join(base, "projects")
    try:
        entries = sorted((e for e in os.scandir(proot) if e.is_dir(follow_symlinks=False)), key=lambda e: e.name)
    except OSError:
        entries = []
    for e in entries:
        b, files = tree_size(e.path)
        sessions = 0
        newest = 0.0
        try:
            for f in os.scandir(e.path):
                if f.name.endswith(".jsonl") and f.is_file(follow_symlinks=False):
                    sessions += 1
                    newest = max(newest, f.stat(follow_symlinks=False).st_mtime)
        except OSError:
            pass
        projects.append({"name": e.name, "path": e.path, "bytes": b, "files": files, "sessions": sessions,
                         "newest_session_mtime": iso(newest),
                         "has_memory": os.path.isdir(os.path.join(e.path, "memory"))})
    projects.sort(key=lambda x: -x["bytes"])
    cleanup = None
    try:
        with open(os.path.join(base, "settings.json"), encoding="utf-8") as f:
            cleanup = json.load(f).get("cleanupPeriodDays")  # 只取这一个键，不输出其他内容
    except (OSError, ValueError, AttributeError):
        pass
    vroot = os.path.join(HOME, ".local", "share", "claude", "versions")
    running = set()
    rc, out, _ = run(["ps", "-axo", "pid=,comm="], timeout=30)
    pids = []
    for raw in out.split(b"\n"):
        parts = decode(raw).strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            comm = parts[1]
            if comm.startswith(vroot + "/"):
                running.add(comm[len(vroot) + 1:].split("/")[0])
            elif os.path.basename(comm) == "claude" or "/claude/" in comm:
                pids.append(parts[0])
    if pids:  # comm 可能是 ~/.local/bin/claude 这个软链，用 txt 映射确认真正运行的版本
        rc, out, _ = run(["lsof", "-a", "-p", ",".join(pids), "-d", "txt", "-F", "n", "-n", "-P", "-w"], timeout=60)
        for raw in out.split(b"\n"):
            name = decode(raw[1:]) if raw.startswith(b"n") else ""
            if name.startswith(vroot + "/"):
                running.add(name[len(vroot) + 1:].split("/")[0])
    active = None
    link = os.path.join(HOME, ".local", "bin", "claude")
    try:
        target = os.path.realpath(link)
        if target.startswith(vroot + "/"):
            active = target[len(vroot) + 1:].split("/")[0]
    except OSError:
        pass
    versions = []
    try:
        for e in sorted(os.scandir(vroot), key=lambda x: x.name):
            b, _ = tree_size(e.path)
            versions.append({"version": e.name, "path": e.path, "bytes": b,
                             "mtime": iso(e.stat(follow_symlinks=False).st_mtime),
                             "running": e.name in running, "active_symlink": e.name == active})
    except OSError:
        pass
    return {
        "kind": "claude", "generated_at": iso(time.time()),
        "projects_total_bytes": sum(p["bytes"] for p in projects), "projects": projects,
        "settings": {"cleanupPeriodDays": cleanup},
        "versions": {"root": vroot, "items": versions, "running": sorted(running), "active_symlink": active},
    }


def _parse_ini_kv(path, wanted_prefix=None):
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" not in line or line.lstrip().startswith(("#", ";")):
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if wanted_prefix is None or k.startswith(wanted_prefix):
                    out[k] = v.strip()
    except OSError:
        pass
    return out


def probe_android(opts):
    env = os.environ
    cand = [env.get("ANDROID_HOME"), env.get("ANDROID_SDK_ROOT"),
            os.path.join(HOME, "Library", "Android", "sdk"), os.path.join(HOME, ".local", "share", "android-sdk")]
    sdk_roots = []
    for c in cand:
        if c:
            rp = os.path.realpath(os.path.expanduser(c))
            if os.path.isdir(rp) and rp not in sdk_roots:
                sdk_roots.append(rp)
    avd_homes = []
    for c in (env.get("ANDROID_AVD_HOME"),
              os.path.join(env["ANDROID_USER_HOME"], "avd") if env.get("ANDROID_USER_HOME") else None,
              os.path.join(HOME, ".android", "avd")):
        if c:
            rp = os.path.realpath(os.path.expanduser(c))
            if os.path.isdir(rp) and rp not in avd_homes:
                avd_homes.append(rp)
    avds = []
    for home in avd_homes:
        for fn in sorted(os.listdir(home)):
            if not fn.endswith(".ini"):
                continue
            name = fn[:-4]
            ini = _parse_ini_kv(os.path.join(home, fn))
            avd_dir = ini.get("path") or os.path.join(home, name + ".avd")
            if not os.path.isabs(avd_dir):
                avd_dir = os.path.join(home, avd_dir)
            if not os.path.isdir(avd_dir):
                alt = os.path.join(home, name + ".avd")
                avd_dir = alt if os.path.isdir(alt) else avd_dir
            cfg = _parse_ini_kv(os.path.join(avd_dir, "config.ini"), "image.sysdir")
            b, _ = tree_size(avd_dir) if os.path.isdir(avd_dir) else (0, 0)
            snap, _ = tree_size(os.path.join(avd_dir, "snapshots")) if os.path.isdir(os.path.join(avd_dir, "snapshots")) else (0, 0)
            avds.append({"name": name, "path": avd_dir, "ini": os.path.join(home, fn), "bytes": b,
                         "snapshots_bytes": snap, "target": ini.get("target"),
                         "image_sysdirs": [v for _, v in sorted(cfg.items())]})
    sdks = []
    for root in sdk_roots:
        used = {}
        for a in avds:
            for sd in a["image_sysdirs"]:
                p = sd if os.path.isabs(sd) else os.path.join(root, sd)
                used.setdefault(os.path.normpath(p), []).append(a["name"])
        images = []
        sroot = os.path.join(root, "system-images")
        try:
            apis = sorted(os.listdir(sroot))
        except OSError:
            apis = []
        for api in apis:
            for variant in sorted(_listdirs(os.path.join(sroot, api))):
                for abi in sorted(_listdirs(os.path.join(sroot, api, variant))):
                    p = os.path.join(sroot, api, variant, abi)
                    b, _ = tree_size(p)
                    users = used.get(os.path.normpath(p), [])
                    images.append({"id": "system-images;%s;%s;%s" % (api, variant, abi), "path": p, "bytes": b,
                                   "used_by": users, "unused": not users})
        comps = {}
        for name in sorted(_listdirs(root)):
            comps[name] = tree_size(os.path.join(root, name))[0]
        sdks.append({"root": root, "components_bytes": comps, "system_images": images,
                     "unused_system_images_bytes": sum(i["bytes"] for i in images if i["unused"])})
    return {"kind": "android", "generated_at": iso(time.time()), "sdk_roots": sdks, "avd_homes": avd_homes,
            "avds": avds}


def _listdirs(path):
    try:
        return [e.name for e in os.scandir(path) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def probe_worktrees(opts):
    repo = protect.real(opts.repo)
    rc, out, err = git(["worktree", "list", "--porcelain"], repo, timeout=30)
    if rc != 0:
        return {"kind": "worktrees", "repo": repo, "error": decode(err).strip()[:300] or "git worktree list 失败",
                "worktrees": []}
    blocks = []
    cur = None
    for raw in decode(out).split("\n"):
        if raw == "":
            if cur:
                blocks.append(cur)
            cur = None
            continue
        key, _, val = raw.partition(" ")
        if key == "worktree":
            cur = {"path": val, "head": None, "branch": None, "detached": False, "bare": False,
                   "locked": False, "lock_reason": None, "prunable": False}
        elif cur is not None:
            if key == "HEAD":
                cur["head"] = val
            elif key == "branch":
                cur["branch"] = val[11:] if val.startswith("refs/heads/") else val
            elif key == "detached":
                cur["detached"] = True
            elif key == "bare":
                cur["bare"] = True
            elif key == "locked":
                cur["locked"] = True
                cur["lock_reason"] = val or None
            elif key == "prunable":
                cur["prunable"] = True
    if cur:
        blocks.append(cur)
    for w in blocks:
        p = w["path"]
        w["exists"] = os.path.isdir(p)
        w["merged_into_origin_main"] = None
        w["dirty_files"] = None
        w["ignored"] = {"bytes": 0, "entries": 0, "top": [], "complete": True}
        if not w["exists"] or w["bare"]:
            continue
        rc, _, _ = git(["rev-parse", "-q", "--verify", "origin/main"], p, timeout=20)
        if rc == 0:
            rc2, _, _ = git(["merge-base", "--is-ancestor", "HEAD", "origin/main"], p, timeout=30)
            w["merged_into_origin_main"] = True if rc2 == 0 else (False if rc2 == 1 else None)
        rc, out, _ = git(["status", "--porcelain"], p, timeout=150)
        if rc == 0:
            w["dirty_files"] = len([x for x in out.split(b"\n") if x])
        rc, out, _ = git(["ls-files", "-o", "-i", "--exclude-standard", "--directory", "-z"], p, timeout=180)
        if rc != 0:
            w["ignored"]["complete"] = False
            continue
        deadline = time.monotonic() + 240
        sizes = []
        entries = [decode(x) for x in out.split(b"\0") if x]
        for rel in entries:
            if time.monotonic() > deadline:
                w["ignored"]["complete"] = False
                break
            b, _ = tree_size(os.path.join(p, rel.rstrip("/")))
            sizes.append((b, rel))
        sizes.sort(reverse=True)
        w["ignored"].update({"bytes": sum(b for b, _ in sizes), "entries": len(entries),
                             "top": [{"path": r, "bytes": b} for b, r in sizes[:15]]})
    return {"kind": "worktrees", "repo": repo, "generated_at": iso(time.time()), "worktrees": blocks}


BIGFILE_ROOTS = ("Downloads", "Desktop", "Documents", "Movies")
BIGFILE_KINDS = {
    "installer": (".dmg", ".pkg", ".mpkg", ".iso", ".xip", ".exe", ".msi", ".apk", ".ipa"),
    "archive": (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".zst"),
    "video": (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".flv"),
    "disk-image": (".sparseimage", ".sparsebundle", ".vmdk", ".qcow2", ".vdi", ".img"),
}


def _bigfile_kind(name):
    low = name.lower()
    for k, exts in BIGFILE_KINDS.items():
        if low.endswith(exts):
            return k
    return "other"


def probe_bigfiles(opts):
    """下载、桌面、文稿、影片里长期没动的大文件（只报告）：安装包、压缩包、视频、磁盘映像等。
    不读文件内容；云盘占位文件（dataless）、受保护路径、隐藏目录、.app 包和 git 工作树内部都跳过。
    安装包附“同名应用是否已装”，压缩包附“旁边是否已有解压出的同名目录”，供 Agent 按知识卡 large-old-files 判断。"""
    min_bytes = opts.min_mb * 1024 * 1024
    cutoff = time.time() - opts.min_age_days * 86400
    profile = protect.load_profile(protect.PROFILE)
    apps = set()
    for base in ("/Applications", os.path.join(HOME, "Applications")):
        try:
            apps.update(e[:-4].lower() for e in os.listdir(base) if e.endswith(".app"))
        except OSError:
            pass
    out, skipped = [], {"dataless": 0, "protected": 0, "recent": 0, "git": 0}
    opened = None
    if opts.lsof:
        entries = run_lsof()
        if entries is not None:
            opened = {}
            for _, cmd, name in entries:
                opened.setdefault(name, set()).add(cmd)
    deadline = time.monotonic() + opts.seconds
    timed_out = False
    for r in BIGFILE_ROOTS:
        root = os.path.join(HOME, r)
        for dp, dns, fns in os.walk(root):
            if time.monotonic() > deadline:
                timed_out = True
                break
            if ".git" in dns:
                skipped["git"] += 1
                dns[:] = []
                continue
            dns[:] = [d for d in dns if not d.startswith(".") and not d.endswith((".app", ".photoslibrary", ".fcpbundle"))]
            for fn in fns:
                fp = os.path.join(dp, fn)
                try:
                    st = os.lstat(fp)
                except OSError:
                    continue
                if not statmod.S_ISREG(st.st_mode) or st.st_blocks * 512 < min_bytes:
                    continue
                if is_dataless(fp, st):
                    skipped["dataless"] += 1
                    continue
                if protect.protected_reason(fp, profile):
                    skipped["protected"] += 1
                    continue
                newest = max(st.st_mtime, st.st_atime)
                if newest > cutoff:
                    skipped["recent"] += 1
                    continue
                kind = _bigfile_kind(fn)
                stem = re.sub(r"[-_ ]?v?\d[\w.\-]*$", "", os.path.splitext(fn)[0]).strip().lower()
                e = {"path": tilde(fp), "bytes": st.st_blocks * 512, "kind": kind, "root": r,
                     "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d"),
                     "atime": datetime.fromtimestamp(st.st_atime, timezone.utc).strftime("%Y-%m-%d"),
                     "open_by": sorted(opened.get(fp, ())) if opened is not None else "unchecked"}
                if kind == "installer":
                    e["app_installed"] = bool(stem) and any(a == stem or a.startswith(stem) for a in apps)
                if kind == "archive":
                    e["extracted_sibling"] = os.path.isdir(os.path.join(dp, os.path.splitext(fn)[0].removesuffix(".tar")))
                out.append(e)
        if timed_out:
            break
    out.sort(key=lambda x: -x["bytes"])
    by_kind = {}
    for e in out:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + e["bytes"]
    return {"min_mb": opts.min_mb, "min_age_days": opts.min_age_days, "roots": ["~/" + r for r in BIGFILE_ROOTS],
            "count": len(out), "bytes": sum(e["bytes"] for e in out), "by_kind": by_kind, "files": out[:opts.limit],
            "truncated": len(out) > opts.limit, "skipped": skipped, "timed_out": timed_out}


def probe_item_refs(opts):
    """0.3.1 遗留：扫描时只对前沿节点查引用，落在大目录内部的条目（节点 refs.reason=non-frontier）没人查过。
    0.5：条目路径正好等于某个非前沿节点本身（如 whisper 模型目录、~/.local/share/android-sdk）的也一并补查。
    这里按计划里这些条目自己的路径补查一次，写 <run>/item_refs.json；plan.py check 读它，查过且零引用的条目不再挂“没能确认”。"""
    run = os.path.realpath(opts.run)
    with open(os.path.join(run, "facts.json"), encoding="utf-8") as f:
        facts = json.load(f)
    import plan as planmod                       # 延迟导入：plan 也会导入本模块的探针
    pl, _ = planmod.load_plan(run)
    nodes = [n for n in facts.get("nodes") or [] if isinstance(n, dict)]
    profile = protect.load_profile(protect.PROFILE)
    # 扫描之后台账 / 设置新增的成品目录：扫描时查过的节点也没搜过这些目录，要按条目路径补查
    rs = (facts.get("coverage") or {}).get("refs_search")
    stale = protect.roots_uncovered(protect.ref_roots_real(profile), rs.get("roots")) if isinstance(rs, dict) else []
    targets = {}
    for c in pl.get("cards") or []:
        if not isinstance(c, dict) or c.get("verdict") not in ("clean", "decide"):
            continue
        for it in c.get("items") or []:
            if not (isinstance(it, dict) and isinstance(it.get("path"), str)):
                continue
            rp = protect.lexical(it["path"])
            primary, _ = planmod.related_nodes(rp, nodes)
            if primary is None:
                continue
            prefs_ = primary.get("refs") or {}
            if os.path.normpath(str(primary.get("path"))) == rp:
                # 节点本身就是这一条：扫描时查过就用那个结果；非前沿节点（扫描时没查它自己的引用，如模型目录、SDK 目录）要补查
                if not ((prefs_.get("checked") is not True and prefs_.get("reason") in ("non-frontier", "no-search-scope"))
                        or (prefs_.get("checked") is True and stale)):
                    continue
            elif prefs_.get("checked") is True and not stale:
                continue
            wt = (primary.get("git") or {}).get("worktree")
            targets[rp] = {"key": rp, "patterns": build_patterns(rp, wt), "path": rp, "scopes": [wt] if wt else []}
    t0 = time.monotonic()
    # 一个目录都没搜过（profile 没有引用检查目录、条目也不在 git 工作树里）时 search_references 记 no-search-scope：
    # 保持未查，plan check 照旧要求 refs_reviewed
    res, stats = search_references(list(targets.values()), [protect.real(r) for r in profile.get("reference_roots", [])],
                                   t0 + opts.seconds) if targets else ({}, [])
    items = {}
    for rp in targets:
        r = res[rp]
        items[rp] = {"checked": r["checked"], "count": len(r["files"]) if r["checked"] else 0,
                     "roots": list(r["scopes"]),     # 这一条实际搜过的目录：plan check 据此判断是否覆盖新成品目录
                     "samples": format_samples(r["files"], r["scopes"]) if r["checked"] else [],
                     **({"reason": r.get("reason") or "timeout-or-error"} if not r["checked"] else {}),
                     **({"count_is_lower_bound": True} if r["checked"] and r["lower_bound"] else {})}
    out = {"schema": "item-refs/1", "scan_id": facts.get("scan_id"), "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "seconds": round(time.monotonic() - t0, 1), "items": items, "stats": stats}
    with open(os.path.join(run, "item_refs.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return {"targets": len(items), "checked": sum(1 for v in items.values() if v["checked"]),
            "with_refs": sum(1 for v in items.values() if v["checked"] and v["count"]),
            "unchecked": sum(1 for v in items.values() if not v["checked"]), "seconds": out["seconds"],
            "file": os.path.join(run, "item_refs.json")}


def probe_grok(opts):
    base = os.path.join(HOME, ".grok")
    sess = os.path.join(base, "sessions")
    b, files = tree_size(sess) if os.path.isdir(sess) else (0, 0)
    ttl = None
    cfg = os.path.join(base, "config.toml")
    try:
        try:
            import tomllib  # Python 3.11+
            with open(cfg, "rb") as f:
                ttl = (tomllib.load(f).get("storage") or {}).get("cleanup_ttl_days")
        except ImportError:
            section = None
            with open(cfg, encoding="utf-8", errors="replace") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("["):
                        section = s.strip("[] ")
                    elif section == "storage":
                        m = re.match(r"cleanup_ttl_days\s*=\s*(\d+)", s)
                        if m:
                            ttl = int(m.group(1))
    except (OSError, ValueError):
        pass
    top = []
    try:
        for e in os.scandir(sess):
            if e.is_dir(follow_symlinks=False):
                top.append((tree_size(e.path)[0], e.name))
    except OSError:
        pass
    top.sort(reverse=True)
    return {"kind": "grok", "generated_at": iso(time.time()),
            "sessions": {"path": sess, "bytes": b, "files": files, "dirs": len(top),
                         "top": [{"name": n, "decoded": unquote(n), "bytes": x} for x, n in top[:15]]},
            "storage": {"cleanup_ttl_days": ttl}}


# ───────────────────────── 项目台账：probe projects（0.5）─────────────────────────

PROJECTS_SCHEMA = "projects/1"
PROJECTS_SHOWN = 15
PROJECT_ACTIVE_DAYS = 7          # 这么多天内有活动 → 默认“进行中”
PROJECT_DONE_DAYS = 30           # 这么多天没活动 → 默认“做完了”；其间 unknown，让用户选
HOME_STD_DIRS = {"Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures", "Public", "Library", "Applications"}
DELIVERABLE_WORDS = ("成品", "成片", "final", "output", "dist", "release", "export", "deliver")
PROJECT_KIND_RANK = ("git", "ai-session", "env", "deliverable-dir")
ENV_PROJECTS = ("env:android", "env:xcode", "env:docker", "env:models")
SIZE_WALK_ENTRIES = 20000        # 没有扫描事实时给项目估体积：最多看这么多条目
SIZE_WALK_SECONDS = 1.5
SIZE_WALK_TOTAL_SECONDS = 20.0


def _enc_claude(name):
    """Claude Code 的项目目录名：路径里所有非字母数字字符（含 / . _ 和中文）各换成一个 -。"""
    return re.sub(r"[^A-Za-z0-9]", "-", name)


def decode_claude_project_dir(dirname):
    """把 ~/.claude/projects 下的目录名反解成真实路径：名字有歧义（- 可能是 / . _ 或本来的 -），
    所以不拆字符串，而是沿文件系统逐级找“编码后正好吻合”的目录。找不到（项目已删、临时目录）返回 None。"""
    if not dirname.startswith("-"):
        return None
    cache = {}

    def names(d):
        if d not in cache:
            try:
                cache[d] = sorted(os.listdir(d), key=len, reverse=True)
            except OSError:
                cache[d] = []
        return cache[d]

    def walk(cur, rest, depth):
        if depth > 40:
            return None
        r = rest[1:]                      # 去掉这一级前面的分隔符
        for nm in names(cur):
            enc = _enc_claude(nm)
            if not enc:
                continue
            full = os.path.join(cur, nm)
            if r == enc:
                return full
            if r.startswith(enc + "-") and os.path.isdir(full) and not os.path.islink(full):
                hit = walk(full, r[len(enc):], depth + 1)
                if hit:
                    return hit
        return None
    return walk("/", dirname, 0)


def _home_project_root(path, gctx):
    """cwd / 目录 → (项目根, 额外工作树或 None)。不在家目录下、家目录本身、隐藏目录、Library、标准文件夹本身都不算项目。
    在 git 仓库里取仓库根（链接工作树折进主仓库）；不在仓库里取家目录（或标准文件夹）下第一级目录。"""
    try:
        p = os.path.realpath(path)
    except OSError:
        return None, None
    if not within_home(p) or p == HOME:
        return None, None
    rel = p[len(HOME) + 1:].split("/")
    top = rel[0]
    if top.startswith(".") or top in ("Library", "Applications"):
        return None, None
    if top in HOME_STD_DIRS and len(rel) < 2:
        return None, None
    if any(part in ("scratchpad", "node_modules") for part in rel):
        return None, None
    wt = gctx.worktree_of(p)
    if wt and wt != HOME and within_home(wt) and os.path.realpath(wt) == wt:
        main = os.path.realpath(GitCtx.main_repo(wt))
        if main != wt and os.path.isdir(main) and within_home(main) and main != HOME:
            return main, wt
        return wt, None
    base = os.path.join(HOME, top, rel[1]) if top in HOME_STD_DIRS else os.path.join(HOME, top)
    return base, None


def _capped_walk(root, deadline):
    """没有扫描事实时给项目估体积和最近修改：限条目数与时间，不跟随链接，不进 .git / node_modules。返回 (bytes, newest_mtime, partial)。"""
    total, newest, seen, partial = 0, 0.0, 0, False
    stack = [root]
    t_end = min(deadline, time.monotonic() + SIZE_WALK_SECONDS)
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                seen += 1
                if seen > SIZE_WALK_ENTRIES or time.monotonic() > t_end:
                    return total, newest, True
                try:
                    st = e.stat(follow_symlinks=False)
                    newest = max(newest, st.st_mtime)
                    if e.is_dir(follow_symlinks=False):
                        if e.name not in (".git", "node_modules"):
                            stack.append(e.path)
                    else:
                        total += st.st_blocks * 512
                except OSError:
                    continue
    return total, newest, partial


def _iso_to_ts(v):
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _pick_run(opts):
    """--run 指定的目录；没给就取 --runs-dir 下最近一次扫描。返回 (run 目录或 None, facts 或 None)。"""
    run = getattr(opts, "run", None)
    if run:
        run = os.path.realpath(run)
    else:
        runs_dir = os.path.abspath(os.path.expanduser(opts.runs_dir))
        best = None
        try:
            names = os.listdir(runs_dir)
        except OSError:
            names = []
        for name in names:
            if not re.match(r"^\d{8}T\d{6}Z-", name):
                continue
            try:
                with open(os.path.join(runs_dir, name, "facts.json"), encoding="utf-8") as f:
                    created = json.load(f).get("created_at") or ""
            except (OSError, ValueError):
                continue
            if best is None or created > best[0]:
                best = (created, os.path.join(runs_dir, name))
        run = best[1] if best else None
    if not run:
        return None, None
    try:
        with open(os.path.join(run, "facts.json"), encoding="utf-8") as f:
            return run, json.load(f)
    except (OSError, ValueError):
        return run, None


def probe_projects(opts):
    """识别项目（只读）：git 仓库 / AI 会话的工作目录 / 开发环境 / 成品目录候选，给出体积、最后活动、会话数、成品猜测、默认状态。
    只用 facts.json 与廉价的 stat：不做全盘遍历；某类来源取不到就跳过并写进 skipped。结果写 <run>/projects.json。"""
    now = time.time()
    skipped = []
    run, facts = _pick_run(opts)
    if facts is None:
        skipped.append({"source": "facts", "reason": "没有找到扫描结果（facts.json）：体积只能用有限遍历估算，git 仓库不含扫描里认出来的"})
    nodes = [n for n in (facts or {}).get("nodes") or [] if isinstance(n, dict) and isinstance(n.get("path"), str)]
    frontier = [n for n in nodes if not n.get("children_over_threshold")]
    gctx = GitCtx()
    projects = {}                        # 根路径或 env:xxx → 项目

    def get(key, name, kind):
        p = projects.get(key)
        if p is None:
            p = projects[key] = {"key": key, "name": name, "sources": [], "roots": [key] if not key.startswith("env:") else [],
                                 "sessions": 0, "session_ids": [], "claude_dirs": [], "_ts": [], "_has_nodes": False,
                                 "bytes": 0}
        if kind not in p["sources"]:
            p["sources"].append(kind)
        return p

    def add_root(proj, root):
        if root and root not in proj["roots"]:
            proj["roots"].append(root)

    # 1. git 仓库：扫描事实里的节点 + 家目录一、二级目录下的 .git
    seen_git = set()
    for n in nodes:
        g = n.get("git") or {}
        repo = g.get("repo")
        if not repo:
            continue
        root, wt = _home_project_root(repo, gctx)
        if root and root not in seen_git:
            seen_git.add(root)
            add_root(get(root, os.path.basename(root), "git"), wt)
    try:
        for e in os.scandir(HOME):
            if e.name.startswith(".") or e.name in ("Library", "Applications") or not e.is_dir(follow_symlinks=False):
                continue
            tops = [e.path] if e.name not in HOME_STD_DIRS else []      # 标准文件夹本身不算项目，只看它下面一层
            try:
                tops += [k.path for k in itertools.islice(os.scandir(e.path), 400)
                         if k.is_dir(follow_symlinks=False) and not k.name.startswith(".")]
            except OSError:
                pass
            for t in tops:
                if os.path.lexists(os.path.join(t, ".git")):
                    root, wt = _home_project_root(t, gctx)
                    if root:
                        seen_git.add(root)
                        add_root(get(root, os.path.basename(root), "git"), wt)
    except OSError as exc:
        skipped.append({"source": "git-home", "reason": f"读不了家目录：{exc.strerror or exc}"})

    # 2. AI 会话的工作目录
    def attach_session(cwd, sid, ts, count, claude_dir=None):
        root, wt = _home_project_root(cwd, gctx)
        if not root:
            return
        p = get(root, os.path.basename(root), "ai-session")
        add_root(p, wt)
        p["sessions"] += count
        if sid:
            p["session_ids"].append(sid)
        if claude_dir and claude_dir not in p["claude_dirs"]:
            p["claude_dirs"].append(claude_dir)
        if ts:
            p["_ts"].append(ts)
    cbase = os.path.join(HOME, ".codex")
    if os.path.isdir(os.path.join(cbase, "sessions")):
        try:
            threads, _ = collect_codex_threads(cbase, with_lsof=False)
            for t in threads.values():
                if t.get("cwd"):
                    attach_session(_cwd_family(t["cwd"]), t["id"], t["mtime"], 0 if t.get("parent") else 1)
        except Exception as exc:                      # 探针只读、尽力而为：别因为一类来源拖垮整个识别
            skipped.append({"source": "codex", "reason": f"读取 Codex 会话失败：{str(exc)[:120]}"})
    else:
        skipped.append({"source": "codex", "reason": "没有 ~/.codex/sessions"})
    cl = os.path.join(HOME, ".claude", "projects")
    undecoded = 0
    try:
        entries = [e for e in os.scandir(cl) if e.is_dir(follow_symlinks=False)]
    except OSError:
        entries = []
        skipped.append({"source": "claude", "reason": "没有 ~/.claude/projects"})
    for e in entries:
        path = decode_claude_project_dir(e.name)
        if not path:
            undecoded += 1
            continue
        jl = [f for f in os.scandir(e.path) if f.name.endswith(".jsonl") and f.is_file(follow_symlinks=False)]
        newest = max((f.stat(follow_symlinks=False).st_mtime for f in jl), default=0.0)
        attach_session(path, None, newest, len(jl), claude_dir=e.name)
    if undecoded:
        skipped.append({"source": "claude", "reason": f"{undecoded} 个 Claude 项目目录对应的路径已不存在或在临时目录，没有计入"})
    gs = os.path.join(HOME, ".grok", "sessions")
    try:
        gentries = [e for e in os.scandir(gs) if e.is_dir(follow_symlinks=False)]
    except OSError:
        gentries = []
    for e in gentries:                                # Grok 的会话目录名就是 URL 编码的工作目录
        cwd = unquote(e.name)
        if os.path.isabs(cwd):
            kids = [k for k in os.scandir(e.path) if k.is_dir(follow_symlinks=False)]
            attach_session(_cwd_family(cwd), None, e.stat(follow_symlinks=False).st_mtime, len(kids))

    # 3. 开发环境：每类一个
    for key in ENV_PROJECTS:
        rts = [r for r in protect.project_roots(key) if os.path.exists(r)]
        if not rts:
            continue
        p = get(key, protect.ENV_PROJECT_NAMES[key], "env")
        p["env_roots"] = [tilde(r) for r in rts]

    # 4. 家目录一级、名字像成品的目录
    try:
        for e in os.scandir(HOME):
            if (e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in HOME_STD_DIRS
                    and any(w in e.name.lower() for w in DELIVERABLE_WORDS)):
                get(os.path.realpath(e.path), e.name, "deliverable-dir")
    except OSError:
        pass

    # 体积与最后活动：先用扫描事实（各前沿节点归给最深的项目），没有事实的才做有限遍历
    def roots_of(p):
        if p["key"].startswith("env:"):
            return [os.path.realpath(r) for r in protect.project_roots(p["key"])]
        return [os.path.realpath(r) for r in p["roots"]]
    owners = sorted(((r, p) for p in projects.values() for r in roots_of(p)), key=lambda x: -len(x[0]))
    for n in frontier:
        np_ = os.path.realpath(n["path"])
        for r, p in owners:
            if np_ == r or np_.startswith(r.rstrip("/") + "/"):
                p["bytes"] += int(n.get("bytes") or 0)
                p["_has_nodes"] = True
                p["_ts"].append(_iso_to_ts(n.get("newest_mtime")))
                break
    deadline = time.monotonic() + SIZE_WALK_TOTAL_SECONDS
    for p in projects.values():
        if p["key"].startswith("env:"):
            continue
        for r in roots_of(p):
            try:
                st = os.stat(r)
                p["_ts"].append(st.st_mtime)
                for gitf in (".git/HEAD", ".git/index", ".git/logs/HEAD"):
                    try:
                        p["_ts"].append(os.stat(os.path.join(r, gitf)).st_mtime)
                    except OSError:
                        pass
            except OSError:
                pass
        if not p["_has_nodes"]:
            tot, newest, partial = 0, 0.0, False
            for r in roots_of(p):
                b, nw, pt = _capped_walk(r, deadline)
                tot, newest, partial = tot + b, max(newest, nw), partial or pt
            p["bytes"] = tot
            p["_ts"].append(newest)
            if partial:
                p["bytes_partial"] = True
    out = []
    for p in projects.values():
        if p["key"].startswith("env:") and not p["bytes"]:
            continue                                  # 环境没占多少，不值得问
        last = max([t for t in p["_ts"] if t], default=0.0)
        age_days = (now - last) / 86400 if last else None
        status = ("unknown" if age_days is None else "active" if age_days < PROJECT_ACTIVE_DAYS
                  else "done" if age_days >= PROJECT_DONE_DAYS else "unknown")
        guess = []
        if not p["key"].startswith("env:"):
            for r in roots_of(p)[:1]:
                if p["sources"] == ["deliverable-dir"]:
                    guess = [tilde(r)]
                    break
                try:
                    for k in itertools.islice(os.scandir(r), 300):
                        if k.is_dir(follow_symlinks=False) and any(w in k.name.lower() for w in DELIVERABLE_WORDS):
                            guess.append(tilde(k.path))
                except OSError:
                    pass
        kind = next(k for k in PROJECT_KIND_RANK if k in p["sources"])
        item = {"key": p["key"], "name": p["name"], "kind": kind, "sources": p["sources"], "bytes": p["bytes"],
                "last_active": iso(last) if last else None, "sessions": p["sessions"],
                "deliverables_guess": guess[:5], "default_status": status, "roots": p["roots"],
                "claude_dirs": p["claude_dirs"], "session_ids": sorted(set(p["session_ids"]))}
        if p.get("env_roots"):
            item["env_roots"] = p["env_roots"]
        if p.get("bytes_partial"):
            item["bytes_partial"] = True
        out.append(item)
    out.sort(key=lambda x: (-x["bytes"], x["name"]))
    for i, item in enumerate(out):
        item["shown"] = i < PROJECTS_SHOWN
    result = {"schema": PROJECTS_SCHEMA, "scan_id": (facts or {}).get("scan_id"), "kind": "projects",
              "created_at": iso(now), "shown_limit": PROJECTS_SHOWN, "projects": out, "skipped": skipped}
    # 0.6：设置页要用的事实。全部有时间预算与异常兜底，任何一项失败只留空，不拖垮探针。
    ext_deadline = time.monotonic() + 40.0
    try:
        result["tools"] = collect_tools(nodes, ext_deadline)
    except Exception as exc:   # noqa: BLE001
        result["tools"] = []
        skipped.append({"source": "tools", "reason": str(exc)[:120]})
    try:
        result["deliverable_candidates"] = collect_deliverable_candidates()
    except Exception as exc:   # noqa: BLE001
        result["deliverable_candidates"] = []
        skipped.append({"source": "deliverable_candidates", "reason": str(exc)[:120]})
    try:
        result["setup_facts"] = collect_setup_facts(nodes, out, ext_deadline)
    except Exception as exc:   # noqa: BLE001
        result["setup_facts"] = {}
        skipped.append({"source": "setup_facts", "reason": str(exc)[:120]})
    if run and facts is not None:
        tmp = os.path.join(run, f"projects.json.tmp-{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(run, "projects.json"))
        result["file"] = os.path.join(run, "projects.json")
    elif run:
        skipped.append({"source": "projects.json", "reason": "run 目录里没有可读的 facts.json，没有写 projects.json（check/propose 认不到它）"})
    sys.stderr.write(f"认出 {len(out)} 个项目（前 {min(len(out), PROJECTS_SHOWN)} 个会列给用户）："
                     + "、".join(f"{p['name']}({units.fmt_bytes(p['bytes'])})" for p in out[:5]) + ("……" if len(out) > 5 else "") + "\n")
    for sk in skipped:
        sys.stderr.write(f"跳过 {sk['source']}：{sk['reason']}\n")
    return {k: ([{kk: vv for kk, vv in p.items() if kk != "session_ids"} for p in v] if k == "projects" else v)
            for k, v in result.items()}


# ───────────────────────── 0.6：设置页要用的机器事实（tools / 成品候选 / setup_facts）─────────────────────────

TOOL_NAMES = {
    "android": "Android 模拟器与开发包", "xcode": "Xcode 与 iOS 模拟器", "docker": "Docker",
    "model-caches": "本地 AI 模型与缓存", "creative-media": "剪辑、音乐等创作软件缓存",
    "codex": "Codex", "claude-code": "Claude Code", "grok": "Grok", "gemini-antigravity": "Gemini / Antigravity",
    "cursor": "Cursor",
}
# 没有扫描事实时按这些位置估算（家目录相对路径）
TOOL_PATHS = {
    "android": [".android", "Library/Android"],
    "xcode": ["Library/Developer"],
    "docker": ["Library/Containers/com.docker.docker", ".docker"],
    "model-caches": [".cache/huggingface", ".ollama", ".lmstudio", ".cache/whisper", ".cache/torch"],
    "creative-media": ["Movies/CacheClip", "Library/Caches/Adobe"],
    "codex": [".codex"], "claude-code": [".claude", ".local/share/claude"], "grok": [".grok"],
    "gemini-antigravity": [".gemini", ".antigravity"], "cursor": [".cursor", "Library/Application Support/Cursor"],
}
AI_TOOL_KEYS = ("codex", "claude-code", "grok", "gemini-antigravity", "cursor")
# 各 AI 工具的会话（对话记录）位置：节点路径等于或落在其下才算会话
AI_SESSION_PATHS = [".codex/sessions", ".codex/archived_sessions", ".claude/projects", ".grok/sessions",
                    ".gemini/antigravity/conversations", ".gemini/antigravity/brain", ".cursor/projects"]
AI_IMAGE_PATHS = [".codex/generated_images"]
INSTALLER_EXTS = (".dmg", ".pkg", ".iso", ".xip", ".ipa", ".apk", ".msi", ".exe", ".mpkg")
RECORDING_EXTS = (".mov", ".mp4", ".m4v", ".mkv", ".webm")
RECORDING_NAME_HINTS = ("录屏", "屏幕录制", "screen recording", "screen_recording")
DELIVERABLE_CAND_WORDS = ("成品", "成片", "导出", "交付", "final", "output", "export", "release")
MEDIA_EXTS = (".mp4", ".mov", ".png", ".jpg", ".jpeg")
DELIVERABLE_CAND_MAX = 20
DELIVERABLE_MEDIA_MIN = 30        # 目录里直接放这么多视频/图片，也当成品候选
DELIVERABLE_SCAN_DEPTH = 3
DELIVERABLE_SCAN_SECONDS = 6.0
SKIP_TOP_DIRS = {"Library", "Applications", "Public"}
SKIP_NAMES = {"node_modules", ".git", "venv", ".venv", "__pycache__", "site-packages", "Pods"}


def _under(path, base):
    return path == base or path.startswith(base.rstrip("/") + "/")


def _topmost(items):
    """items: [(realpath, node)]；去掉被同组其他路径包含的子路径（避免体积重复计）。"""
    items = sorted(items, key=lambda x: len(x[0]))
    kept = []
    for p, n in items:
        if not any(_under(p, k) and p != k for k, _ in kept):
            kept.append((p, n))
    return kept


def _node_by_path(nodes):
    return {os.path.realpath(n["path"]): n for n in nodes}


def _est_bytes(path, deadline):
    """没有扫描事实时的有限估算，返回 (bytes, newest_mtime, partial)。"""
    try:
        if os.path.isfile(path):
            st = os.lstat(path)
            return st.st_blocks * 512, st.st_mtime, False
        return _capped_walk(path, deadline)
    except OSError:
        return 0, 0.0, False


def collect_tools(nodes, deadline):
    """工具键 → 占用。facts 节点的 producer.card 优先（取最上层，不重复计），没有节点才按已知位置有限估算。"""
    out = []
    for key, name in TOOL_NAMES.items():
        try:
            items = _topmost([(os.path.realpath(n["path"]), n) for n in nodes
                              if (n.get("producer") or {}).get("card") == key])
            total, newest, paths = 0, 0.0, []
            if items:
                for p, n in sorted(items, key=lambda x: -int(x[1].get("bytes") or 0)):
                    total += int(n.get("bytes") or 0)
                    newest = max(newest, _iso_to_ts(n.get("newest_mtime")))
                    paths.append(tilde(p))
            else:
                for rel in TOOL_PATHS.get(key, []):
                    p = os.path.join(HOME, rel)
                    if not os.path.exists(p):
                        continue
                    b, nw, _ = _est_bytes(p, deadline)
                    total, newest = total + b, max(newest, nw)
                    paths.append(tilde(p))
            if not paths:
                continue
            out.append({"key": key, "name": name, "bytes": total,
                        "last_used": iso(newest) if newest else None, "paths": paths[:5]})
        except Exception:   # noqa: BLE001  单个工具失败不拖垮探针
            continue
    out.sort(key=lambda t: -t["bytes"])
    return out


def _media_count(path, limit=2000):
    """目录下（递归、限条目）的视频与图片数，只数不读。"""
    n = seen = 0
    stack = [path]
    while stack and seen < limit:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                seen += 1
                if seen > limit:
                    break
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name not in SKIP_NAMES:
                            stack.append(e.path)
                    elif e.name.lower().endswith(MEDIA_EXTS):
                        n += 1
                except OSError:
                    continue
    return n


def collect_deliverable_candidates(budget=DELIVERABLE_SCAN_SECONDS):
    """家目录下像“成品”的目录：名字含成品/成片/导出/交付/final/output/export/release，或直接放了一大堆视频图片。
    限深度、限时间；不进隐藏目录、Library、依赖目录、git 仓库内部（仓库属于项目，不当成品目录）。"""
    t_end = time.monotonic() + budget
    found = {}
    stack = [(HOME, 0)]
    while stack and time.monotonic() < t_end:
        d, depth = stack.pop()
        try:
            entries = list(itertools.islice(os.scandir(d), 600))
        except OSError:
            continue
        direct_media = 0
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if (e.name.startswith(".") or e.name in SKIP_NAMES or (depth == 0 and e.name in SKIP_TOP_DIRS)
                            or e.name.endswith((".app", ".photoslibrary", ".imovielibrary", ".fcpbundle"))):
                        continue
                    if os.path.lexists(os.path.join(e.path, ".git")):
                        continue
                    low = e.name.lower()
                    if any(w in low for w in DELIVERABLE_CAND_WORDS):
                        found.setdefault(e.path, ("名字含成品类字样", None))
                    if depth + 1 < DELIVERABLE_SCAN_DEPTH:
                        stack.append((e.path, depth + 1))
                elif e.name.lower().endswith(MEDIA_EXTS):
                    direct_media += 1
            except OSError:
                continue
        if depth > 0 and direct_media >= DELIVERABLE_MEDIA_MIN:
            found.setdefault(d, ("里面放了很多视频或图片", direct_media))
    cands = []
    for path, (why, direct) in found.items():
        if path == HOME:
            continue
        if any(_under(path, o) and path != o for o in found):
            # 外层已是候选时，内层不重复列，除非内层自己名字就像成品
            if why != "名字含成品类字样":
                continue
        files = _media_count(path)
        if files == 0 and why != "名字含成品类字样":
            continue
        b, _, partial = _capped_walk(path, time.monotonic() + 1.0)
        base = os.path.basename(path)
        if why == "名字含成品类字样":
            hit = next((w for w in DELIVERABLE_CAND_WORDS if w in base.lower()), "")
            why_txt = "名字含“%s”" % hit + ("，有 %d 个视频或图片" % files if files else "")
        else:
            why_txt = "里面放了 %d 个视频或图片" % files
        item = {"path": tilde(path), "why": why_txt, "bytes": b, "files": files}
        if partial:
            item["bytes_partial"] = True
        cands.append(item)
    cands.sort(key=lambda c: (-c["files"], -c["bytes"], c["path"]))
    return cands[:DELIVERABLE_CAND_MAX]


def _sum_ext_files(dirs, matcher, budget, max_depth=3):
    """dirs 下（限深、限时）符合 matcher(name) 的普通文件字节合计；dataless 占位不计。"""
    t_end = time.monotonic() + budget
    total = seen = 0
    for base in dirs:
        stack = [(base, 0)]
        while stack:
            d, depth = stack.pop()
            if time.monotonic() > t_end:
                return total
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    seen += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if depth + 1 < max_depth and e.name not in SKIP_NAMES and not e.name.endswith(".app"):
                                stack.append((e.path, depth + 1))
                        elif matcher(e.name):
                            st = e.stat(follow_symlinks=False)
                            if not (getattr(st, "st_flags", 0) & SF_DATALESS):
                                total += st.st_blocks * 512
                    except OSError:
                        continue
    return total


def _cloud_local_bytes(budget):
    """iCloud 与 CloudStorage 里已下载到本地的字节；只 stat，不读内容（读了会触发下载）。dataless 与块数为 0 的不计。"""
    t_end = time.monotonic() + budget
    total = 0
    for base in (os.path.join(HOME, "Library", "Mobile Documents"), os.path.join(HOME, "Library", "CloudStorage")):
        stack = [base]
        while stack:
            if time.monotonic() > t_end:
                return total
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                        if getattr(st, "st_flags", 0) & SF_DATALESS:
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += st.st_blocks * 512
                    except OSError:
                        continue
    return total


CLOUD_FOLDERS_LIMIT = 12            # 最多报告几个
CLOUD_WALK_ENTRIES = 60000          # 单个文件夹最多看多少个条目（大文件夹只报告已看到的部分，体积偏小而不是偏大）
CLOUD_LAYER2_TRANSPARENT = ("my drive", "我的云端硬盘")      # Google Drive 的这一层只是个壳，再往下一层才是用户的文件夹
_CLOUD_PROVIDERS = (("dropbox", "Dropbox"), ("onedrive", "OneDrive"), ("googledrive", "Google Drive"),
                    ("google drive", "Google Drive"), ("icloud", "iCloud Drive"), ("com~apple~clouddocs", "iCloud Drive"))


def _cloud_provider(name, icloud_base=False):
    low = name.lower()
    for key, label in _CLOUD_PROVIDERS:
        if low.startswith(key):
            return label
    return "iCloud Drive" if icloud_base else "其他"


def _subdirs(path):
    """path 下的第一层真目录（不跟符号链接、不进占位目录）。读不了返回空。"""
    out = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False) and not (getattr(e.stat(follow_symlinks=False), "st_flags", 0) & SF_DATALESS):
                        out.append(e.path)
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(out)


def _cloud_walk(root, t_end):
    """文件夹里已下载到本机的体积与文件数：只 stat，不读内容（读了会触发下载）；占位文件 / 占位目录一律跳过，
    不进符号链接。条目数、时间都有上限，超了就报告已看到的部分。"""
    total, files, seen = 0, 0, 0
    stack = [root]
    while stack and seen < CLOUD_WALK_ENTRIES and time.monotonic() < t_end:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue
        with it:
            for e in it:
                seen += 1
                try:
                    st = e.stat(follow_symlinks=False)
                    if getattr(st, "st_flags", 0) & SF_DATALESS or e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        total += st.st_blocks * 512
                        files += 1
                except OSError:
                    continue
    return total, files


def _cloud_folders(budget=8.0, limit=CLOUD_FOLDERS_LIMIT):
    """iCloud 云盘与 ~/Library/CloudStorage 里网盘的第二层文件夹，哪些在本机占的地方最大（给第 11 题“只留云端”的建议用）。
    只读：不执行任何释放动作，不调 brctl。返回 [{path, provider, local_bytes, files}]，按本机体积从大到小。"""
    t_end = time.monotonic() + budget
    cands = []          # (路径, provider)
    mobile = os.path.join(HOME, "Library", "Mobile Documents")
    for c in _subdirs(mobile):
        name = os.path.basename(c)
        if name == "com~apple~CloudDocs":             # iCloud 云盘本体：看它里面的文件夹；其余是各 App 自己的 iCloud 数据，按一个整体报告
            cands += [(d, "iCloud Drive") for d in _subdirs(c)]
        else:
            cands.append((c, "iCloud Drive"))
    for root in _subdirs(os.path.join(HOME, "Library", "CloudStorage")):
        prov = _cloud_provider(os.path.basename(root))
        for d in _subdirs(root):
            if prov == "Google Drive" and os.path.basename(d).lower() in CLOUD_LAYER2_TRANSPARENT:
                cands += [(x, prov) for x in _subdirs(d)]
            else:
                cands.append((d, prov))
    out = []
    for path, prov in cands:
        if time.monotonic() > t_end:
            break
        b, n = _cloud_walk(path, t_end)
        if b > 0:
            out.append({"path": tilde(path), "provider": prov, "local_bytes": b, "files": n})
    out.sort(key=lambda r: (-r["local_bytes"], r["path"]))
    return out[:limit]


_TM_DIR_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(\d{6})$")
_TM_PREF_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \+0000")


def time_machine_last():
    """最近一次时间机器备份（UTC ISO）；没配置或读不到返回 None。只读命令，失败不报错。"""
    rc, out, _ = run(["tmutil", "latestbackup"], timeout=8)
    if rc == 0 and out.strip():
        m = _TM_DIR_RE.search(decode(out).strip())
        if m:
            y, mo, d, hms = m.groups()
            # 目录名是本地时间；按本地时区换成 UTC
            try:
                local = datetime(int(y), int(mo), int(d), int(hms[:2]), int(hms[2:4]), int(hms[4:6]))
                return iso(local.timestamp())
            except ValueError:
                pass
    rc, out, _ = run(["defaults", "read", "/Library/Preferences/com.apple.TimeMachine"], timeout=8)
    if rc == 0:
        dates = _TM_PREF_RE.findall(decode(out))
        if dates:
            return max(dates).replace(" ", "T") + "Z"
    return None


def _worktree_count(nodes, project_items, budget=8.0):
    """git 链接工作树数（不含主仓库）：facts 节点 git.role=="worktree"、项目的额外根、各主仓库的 git worktree list。"""
    found = set()
    for n in nodes:
        if (n.get("git") or {}).get("role") == "worktree":
            found.add(os.path.realpath(n["path"]))
    repos = set()
    for it in project_items:
        for r in it.get("roots") or []:
            if os.path.isfile(os.path.join(r, ".git")):
                found.add(os.path.realpath(r))
        if "git" in (it.get("sources") or []):
            repos.add((it.get("roots") or [it.get("key")])[0])
    for n in nodes:
        repo = (n.get("git") or {}).get("repo")
        if repo:
            repos.add(repo)
    t_end = time.monotonic() + budget
    for repo in sorted(repos):
        if time.monotonic() > t_end:
            break
        if not os.path.isdir(repo):
            continue
        rc, out, _ = git(["worktree", "list", "--porcelain"], repo, timeout=5)
        if rc != 0:
            continue
        paths = [ln[9:] for ln in decode(out).splitlines() if ln.startswith("worktree ")]
        for p in paths[1:]:
            if os.path.isdir(p):
                found.add(os.path.realpath(p))
    return len(found)


def _paths_bytes(rels, by_path, nodes, deadline):
    """家目录相对路径列表的合计字节：有节点用节点（最上层），没有就有限估算。"""
    total = 0
    for rel in rels:
        p = os.path.realpath(os.path.join(HOME, rel))
        if not os.path.exists(p):
            continue
        if p in by_path:
            total += int(by_path[p].get("bytes") or 0)
        else:
            total += _est_bytes(p, deadline)[0]
    return total


def collect_setup_facts(nodes, project_items, deadline):
    by_path = _node_by_path(nodes)
    facts = {"ai_sessions_bytes": 0, "ai_images_bytes": 0, "worktrees": 0, "installers_bytes": 0,
             "recordings_bytes": 0, "cloud_local_bytes": 0, "time_machine_last": None, "top_dirs": [],
             "cloud_folders": [], "pipelines": False}

    def safe(key, fn):
        try:
            facts[key] = fn()
        except Exception:   # noqa: BLE001  任何一项失败都保持默认值
            pass
    safe("ai_sessions_bytes", lambda: _paths_bytes(AI_SESSION_PATHS, by_path, nodes, deadline))

    def images():
        total = _paths_bytes(AI_IMAGE_PATHS, by_path, nodes, deadline)
        for _, n in _topmost([(os.path.realpath(n["path"]), n) for n in nodes
                              if (n.get("producer") or {}).get("card") == "pipelines"
                              and not any(_under(os.path.realpath(n["path"]), os.path.realpath(os.path.join(HOME, r)))
                                          for r in AI_IMAGE_PATHS)]):
            total += int(n.get("bytes") or 0)
        return total
    safe("ai_images_bytes", images)
    safe("worktrees", lambda: _worktree_count(nodes, project_items))
    dl = [os.path.join(HOME, d) for d in ("Downloads", "Desktop")]
    safe("installers_bytes", lambda: _sum_ext_files(dl, lambda nm: nm.lower().endswith(INSTALLER_EXTS), 4.0, 2))
    rec = [os.path.join(HOME, d) for d in ("Desktop", "Movies", "Downloads")]
    safe("recordings_bytes", lambda: _sum_ext_files(
        rec, lambda nm: nm.lower().endswith(RECORDING_EXTS) or any(h in nm.lower() for h in RECORDING_NAME_HINTS), 5.0, 3))
    safe("cloud_local_bytes", lambda: _cloud_local_bytes(5.0))
    safe("cloud_folders", lambda: _cloud_folders(8.0))
    safe("pipelines", lambda: any((n.get("producer") or {}).get("card") == "pipelines" for n in nodes))
    safe("time_machine_last", time_machine_last)

    def top():
        rows = []
        for e in os.scandir(HOME):
            if not e.is_dir(follow_symlinks=False):
                continue
            p = os.path.realpath(e.path)
            if p in by_path:
                b = int(by_path[p].get("bytes") or 0)
            else:
                b = _est_bytes(p, min(deadline, time.monotonic() + 2.0))[0]
            rows.append({"path": tilde(e.path), "bytes": b})
        rows.sort(key=lambda r: (-r["bytes"], r["path"]))
        return rows[:12]
    safe("top_dirs", top)
    return facts


# ───────────────────────── 0.6：probe done-evidence（找“做完了”的证据，只给信号不下结论）─────────────────────────

DONE_FILE_WORDS = ("完成", "交付", "final", "release", "上线", "成品", "delivered", "published")
DONE_TEXT_RE = re.compile(r"完成|已上线|\bdone\b", re.I)
DONE_DOC_RE = re.compile(r"^(readme|进度|progress|status|todo|changelog|工作日志|日志|交接|handoff)", re.I)
DONE_DOC_EXTS = (".md", ".txt", ".rst", "")
DONE_DOC_MAX_BYTES = 2 * 1024 * 1024
NEVER_NAME_HINTS = ("password", "密码", ".env", "secret", "credential")


def _near_name(a, b):
    a, b = a.casefold(), b.casefold()
    return len(a) >= 3 and len(b) >= 3 and (a == b or a in b or b in a)


def _peek_refused(fp):
    import peek
    return peek.decide(peek.classify(fp), peek.read_scope()) != "content"


def _log_read(run, fp):
    """读过的文档记进 <run>/reads.jsonl（成果页“这次读了哪些文件”）；没给运行目录或目录不存在就不记。"""
    if not run or not os.path.isdir(os.path.realpath(run)):
        return
    import peek
    try:
        peek.record(os.path.realpath(run), fp, peek.classify(fp), "content")
    except OSError:
        pass


def probe_done_evidence(opts):
    path = os.path.realpath(os.path.expanduser(opts.path))
    base = os.path.basename(path.rstrip("/"))
    out = {"path": tilde(path), "exists": os.path.isdir(path), "git": {"is_repo": False},
           "deliverables": {"count": 0, "samples": []}, "last_modified": None,
           "final_named_files": [], "doc_hits": []}
    skipped = []
    if not out["exists"]:
        out["skipped"] = [{"source": "path", "reason": "路径不存在或不是目录"}]
        return out
    # git
    try:
        rc, top, _ = git(["rev-parse", "--show-toplevel"], path, timeout=10)
        if rc == 0 and os.path.realpath(decode(top).strip()) == path:
            g = out["git"]
            g["is_repo"] = True
            rc, br, _ = git(["symbolic-ref", "-q", "--short", "HEAD"], path, timeout=10)
            g["branch"] = decode(br).strip() if rc == 0 and br.strip() else "(detached)"
            g["merged_into_main"] = None
            for ref in ("origin/main", "main", "origin/master", "master"):
                rc, _, _ = git(["rev-parse", "-q", "--verify", ref], path, timeout=10)
                if rc == 0:
                    rc2, _, _ = git(["merge-base", "--is-ancestor", "HEAD", ref], path, timeout=20)
                    g["merged_into_main"] = True if rc2 == 0 else (False if rc2 == 1 else None)
                    g["compared_with"] = ref
                    break
            rc, st, _ = git(["status", "--porcelain"], path, timeout=60)
            g["dirty_files"] = len([x for x in st.split(b"\n") if x]) if rc == 0 else None
            rc, lg, _ = git(["log", "-1", "--format=%ct"], path, timeout=10)
            try:
                g["last_commit_at"] = iso(int(decode(lg).strip())) if rc == 0 and lg.strip() else None
            except ValueError:
                g["last_commit_at"] = None
    except Exception as exc:   # noqa: BLE001
        skipped.append({"source": "git", "reason": str(exc)[:120]})
    # 成品目录里同名/近名
    try:
        prof = protect.load_profile(getattr(opts, "profile", None) or protect.PROFILE)
        t_end = time.monotonic() + 6.0
        count, samples = 0, []
        for root in prof.get("deliverable_roots", []) or []:
            rr = protect.real(root)
            if not os.path.isdir(rr) or _under(path, rr):
                continue
            stack = [(rr, 0, False)]
            seen = 0
            while stack and time.monotonic() < t_end and seen < 5000:
                d, depth, matched = stack.pop()
                try:
                    it = os.scandir(d)
                except OSError:
                    continue
                with it:
                    for e in it:
                        seen += 1
                        stem = os.path.splitext(e.name)[0]
                        m = matched or _near_name(base, stem)
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if depth + 1 < 4 and e.name not in SKIP_NAMES:
                                    stack.append((e.path, depth + 1, m))
                            elif m:
                                count += 1
                                if len(samples) < 5:
                                    samples.append(tilde(e.path))
                        except OSError:
                            continue
        out["deliverables"] = {"count": count, "samples": samples}
    except Exception as exc:   # noqa: BLE001
        skipped.append({"source": "deliverables", "reason": str(exc)[:120]})
    # 目录里的文件名与文档
    try:
        t_end = time.monotonic() + 6.0
        newest, seen = 0.0, 0
        final_files, docs = [], []
        stack = [(path, 0)]
        while stack and seen < 4000 and time.monotonic() < t_end:
            d, depth = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    seen += 1
                    try:
                        st = e.stat(follow_symlinks=False)
                        newest = max(newest, st.st_mtime)
                        if e.is_dir(follow_symlinks=False):
                            if depth + 1 < 4 and e.name not in SKIP_NAMES:
                                stack.append((e.path, depth + 1))
                            continue
                        low = e.name.lower()
                        if len(final_files) < 10 and any(w in low for w in DONE_FILE_WORDS):
                            final_files.append(os.path.relpath(e.path, path))
                        stem, ext = os.path.splitext(low)
                        if (depth <= 2 and DONE_DOC_RE.match(low) and ext in DONE_DOC_EXTS
                                and st.st_size <= DONE_DOC_MAX_BYTES and not any(h in low for h in NEVER_NAME_HINTS)
                                and not (getattr(st, "st_flags", 0) & SF_DATALESS)):
                            docs.append(e.path)
                    except OSError:
                        continue
        out["last_modified"] = iso(newest) if newest else None
        out["final_named_files"] = final_files
        hits = []
        for fp in sorted(docs)[:10]:
            if _peek_refused(fp):           # 与 peek.py 同一口径：凭证类永不读，未授权的类别不读内容
                continue
            try:
                with open(fp, "rb") as f:
                    text = f.read(DONE_DOC_MAX_BYTES).decode("utf-8", "replace")
            except OSError:
                continue
            _log_read(getattr(opts, "run", None), fp)
            lines = [ln.strip()[:160] for ln in text.splitlines() if DONE_TEXT_RE.search(ln)][:3]
            if lines:
                hits.append({"file": os.path.relpath(fp, path), "lines": lines})
        out["doc_hits"] = hits
    except Exception as exc:   # noqa: BLE001
        skipped.append({"source": "files", "reason": str(exc)[:120]})
    if skipped:
        out["skipped"] = skipped
    return out


# ───────────────────────── CLI ─────────────────────────

def build_parser():
    ap = argparse.ArgumentParser(description="叁笙清理 · 事实采集（只读）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("scan", help="自顶向下计量，产出 facts.json")
    sp.add_argument("--profile", help="profile.json 路径，默认 ~/.sansheng/disk-cleanup/profile.json")
    sp.add_argument("--runs-dir", default=DEFAULT_RUNS)
    sp.add_argument("--threshold", type=parse_size, default=parse_size("500MB"))
    sp.add_argument("--max-depth", type=int, default=10)
    sp.add_argument("--max-seconds", type=float, default=900)
    sp.add_argument("--refs-seconds", type=float, default=900)
    sp.add_argument("--no-refs", action="store_true")
    sp.add_argument("--no-lsof", action="store_true", help="跳过 lsof（open_by 记为 unknown）")
    sp.add_argument("--progress-seconds", type=float, default=PROGRESS_SECONDS,
                    help="遍历阶段每隔多少秒向 stderr 打一行进度（已遍历多少、多少目录、用时），默认 30，0 表示不打")
    pp = sub.add_parser("probe", help="按知识卡调用的专项探针，输出 JSON")
    psub = pp.add_subparsers(dest="kind", required=True)
    pc = psub.add_parser("codex")
    pc.add_argument("--min-bytes", type=parse_size, default=parse_size("100MB"))
    pc.add_argument("--top", type=int, default=20)
    pc.add_argument("--profile")
    pg = psub.add_parser("codex-groups", help="按来源（originator+cwd）归组全部会话；--trace N 对第 N 组核对副本与引用")
    pg.add_argument("--min-bytes", type=parse_size, default=parse_size("200MB"))
    pg.add_argument("--trace", type=int, help="对第 N 组（按体积降序，从 0 开始）逐线程分类")
    pg.add_argument("--search", action="append", help="额外的副本/引用搜索目录，可重复")
    pg.add_argument("--trace-seconds", type=float, default=600)
    pg.add_argument("--emit", help="把各分类写成 plan.json 可引用的 items 文件，放到此目录（通常是 <run>/items）")
    pg.add_argument("--profile")
    psub.add_parser("claude")
    ppj = psub.add_parser("projects", help="识别项目（git 仓库、AI 会话工作目录、开发环境、成品目录），写 <run>/projects.json（项目台账用）")
    ppj.add_argument("--run", help="运行目录（含 facts.json）；缺省取 --runs-dir 下最近一次扫描")
    ppj.add_argument("--runs-dir", default=DEFAULT_RUNS)
    psub.add_parser("android")
    psub.add_parser("grok")
    pde = psub.add_parser("done-evidence", help="找“做完了”的证据：git 状态、成品目录同名文件、完成类文件名与文档字样（只给信号，不下结论）")
    pde.add_argument("--run", help="运行目录（保留参数，便于和其他探针同形；不读写其中文件）")
    pde.add_argument("--path", required=True)
    pde.add_argument("--profile")
    pb = psub.add_parser("bigfiles", help="下载、桌面、文稿、影片里长期没动的大文件（只报告）")
    pb.add_argument("--min-mb", type=int, default=200, help="单个文件至少多大（MB），默认 200")
    pb.add_argument("--min-age-days", type=int, default=90, help="修改与访问都早于多少天，默认 90")
    pb.add_argument("--limit", type=int, default=200, help="最多列多少个，默认 200")
    pb.add_argument("--seconds", type=int, default=120, help="遍历预算秒数，默认 120")
    pb.add_argument("--lsof", action="store_true", help="逐个检查是否被打开（慢）")
    pi = psub.add_parser("item-refs", help="对计划里落在大目录内部、扫描时没查过引用的条目，按条目路径补查引用，写 item_refs.json")
    pi.add_argument("--run", required=True)
    pi.add_argument("--seconds", type=int, default=900, help="引用检查预算秒数，默认 900")
    pw = psub.add_parser("worktrees")
    pw.add_argument("--repo", required=True)
    ps = psub.add_parser("system", help="系统层：完全磁盘访问三态、本地快照、三口径容量、未归因估算、系统大块")
    ps.add_argument("--run", help="最近一次 scan 的 run 目录，用来估算未归因的“系统数据”")
    psub.add_parser("updaters", help="各软件更新暂存：同 bundle id 且版本不高于已装为 superseded，高于为 pending_install")
    pl = psub.add_parser("leftovers", help="已卸载软件在 Caches/Logs/Saved State/HTTPStorages/WebKit 留下的目录（只报告）")
    pl.add_argument("--min-age-days", type=int, default=30)
    pl.add_argument("--brew-zap", action="store_true", help="用已安装 cask 的 zap 字段排除仍在装的软件")
    return ap


def main(argv=None):
    opts = build_parser().parse_args(argv)
    bad_home = protect.home_problem()
    if bad_home:                          # HOME 被改时保护名单与搜索范围都会错位，扫描结果不可用
        print(bad_home, file=sys.stderr)
        return 2
    if opts.cmd == "scan":
        run_dir, facts = do_scan(opts)
        print_summary(run_dir, facts)
        return 0
    if opts.kind in ("system", "updaters", "leftovers"):
        import importlib                  # 独立模块：只读，带超时
        mod, fname = {"system": ("sysprobe", "probe_system"), "updaters": ("updaters", "probe_updaters"),
                      "leftovers": ("leftovers", "probe_leftovers")}[opts.kind]
        print(dump_json(getattr(importlib.import_module(mod), fname)(opts)))
        return 0
    fn = {"codex": probe_codex, "codex-groups": probe_codex_groups, "claude": probe_claude, "android": probe_android,
          "worktrees": probe_worktrees, "grok": probe_grok, "item-refs": probe_item_refs,
          "bigfiles": probe_bigfiles, "projects": probe_projects, "done-evidence": probe_done_evidence}[opts.kind]
    print(dump_json(fn(opts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
