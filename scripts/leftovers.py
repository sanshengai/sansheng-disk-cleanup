#!/usr/bin/env python3
r"""已卸载应用遗留的低风险缓存：只读探针（不删除、不联网、不用 sudo）。

0.6：残留判据按 AppCleaner 3.7 的匹配规则重写（思路来自 2026-10-12 清理竞品附录 B，自己实现）。

四条底线：
  1. 位置有限：Caches / Logs / Saved Application State / HTTPStorages / WebKit 与 0.5 同口径（第一层）；
     另加三个只认明确形状的位置：LaunchAgents 的 <id>.plist、Cookies 的 <id>.binarycookies、
     Group Containers 的 <TeamID>.xxx / group.xxx 目录。Preferences / Application Support / Containers 仍不评估。
  2. 已装清单不完整（installed_apps()["complete"] 为 False）整个探针拒绝，不输出候选。
  3. 名字不用子串匹配，用「整名 + 以分隔符或数字开头的后缀」：^名字(?:[.\-_\s\d]+.*)?$（忽略大小写）；
     名字或 id 少于 4 个字符只认整串相等；bundle id 先归一（去掉 .trial/.beta/.demo/.debug 等尾部段与数字段），
     达到 8 个字符才放宽前缀匹配，也只有这时才看 LaunchAgents / Cookies。
  4. 任何查询失败都往「应用还在」方向：反查异常、stat 失败、Spotlight 不可用、进程名单读不到，一律跳过不列。
  5. Group Containers 只在读得到 app-group 时评估：对每个已装 App（系统 App 同样）及其 PlugIns/*.appex 跑
     `codesign -d --entitlements :-`（5 秒超时、结果缓存）取 com.apple.security.application-groups；
     任何一个读不到（超时、报错、解析失败），整个位置标 not_evaluated、不报残留。
  6. LaunchAgents 读 plist 的 Program / ProgramArguments[0]：可执行文件存在、或 `launchctl print gui/<uid>/<label>`
     退出码 0，就视为在用不报；launchctl 超时或读不到按「在用」处理。
另有：目录内最新 mtime 距今 >= min_age_days；Spotlight 查到同 bundle id 的 .app 视为还在；
正在运行的 App 对应的目录默认保护；brew_zap=True 时已装 cask 的 zap 路径覆盖候选视为还在；
~/Library 与 /Library 下的标准子目录本身永不算残留。
"""
import fnmatch
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protect  # noqa: E402
import scan  # noqa: E402

# ---- 名字匹配（AppCleaner 3.7 的思路，自己实现） -------------------------------------------------
MIN_NAME_LEN = 4           # 名字或 id 少于 4 个字符：只认整串相等，不做任何前缀/后缀匹配
MIN_RELAXED_ID_LEN = 8     # bundle id 达到 8 个字符才放宽前缀匹配；也只有这时才看 LaunchAgents / Cookies
ID_NOISE_SEGMENTS = ("trial", "beta", "demo", "debug", "app", "application")   # 归一要去掉的尾部段
STRICT_BID = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2,}$")   # 至少两个点，仅字母数字点横线下划线
GROUP_PREFIX = re.compile(r"^(?:group|[A-Za-z0-9]{10})\.(.+)$")        # <TeamID>.xxx / group.xxx

# ---- 位置规则表（一行一个位置，方便审计） --------------------------------------------------------
# kind：dir=第一层目录（0.5 口径）；file=只认这个后缀的普通文件；group=Group Containers 的目录
# min_id：名字（bundle id）至少多长才看，0 表示不设门槛
Loc = namedtuple("Loc", "loc suffix kind min_id safety note shared match_rule")
KIND_DIR, KIND_FILE, KIND_GROUP = "dir", "file", "group"
LOCATION_RULES = (
    Loc("Caches", "", KIND_DIR, 0, None, None, False, "bundle_id"),
    Loc("Logs", "", KIND_DIR, 0, None, None, False, "bundle_id"),
    Loc("Saved Application State", ".savedState", KIND_DIR, 0, None, None, False, "bundle_id"),
    Loc("HTTPStorages", "", KIND_DIR, 0, "moderate",
        "含登录态或本地存储（WebKit/HTTPStorages），不只是缓存；默认不勾选，需用户确认", False, "bundle_id"),
    Loc("WebKit", "", KIND_DIR, 0, "moderate",
        "含登录态或本地存储（WebKit/HTTPStorages），不只是缓存；默认不勾选，需用户确认", False, "bundle_id"),
    Loc("LaunchAgents", ".plist", KIND_FILE, MIN_RELAXED_ID_LEN, "moderate",
        "已卸载软件的登录项/后台代理；默认不勾选，删前确认没有还在用的软件靠它启动", False, "launch_agent"),
    Loc("Cookies", ".binarycookies", KIND_FILE, MIN_RELAXED_ID_LEN, "moderate",
        "含登录态（Cookies），不只是缓存；默认不勾选，需用户确认", False, "cookie"),
    Loc("Group Containers", "", KIND_GROUP, 0, "moderate",
        "可能被同厂商其它软件共用（App 签名里的 app-group 只在能读到信息时才用来归属）；默认不勾选",
        True, "group_container"),
)
NOT_EVALUATED = (
    ("Preferences", "含用户设置，可能是用户唯一的配置备份，只列不动"),
    ("Application Support", "含用户数据与登录态，误删不可恢复，只列不动"),
    ("Containers", "沙盒容器含用户文档，归属需读容器元数据，风险高，不自动评估"),
    ("Group Containers", "含多个软件共享的数据；只在名字是 <TeamID>. / group. 前缀且去掉前缀后是 bundle id 时才看，"
                         "其余不评估（命中的也可能被同厂商其它软件共用，默认不勾选）"),
)
DEFAULT_MIN_AGE_DAYS = 30
WALK_MAX_ENTRIES = 20000
WALK_MAX_SECONDS = 5.0
MDFIND_TIMEOUT = 10
BREW_TIMEOUT = 60
PGREP_TIMEOUT = 10
CODESIGN_TIMEOUT = 5        # 读一个 App 的签名权限最多等 5 秒
LAUNCHCTL_TIMEOUT = 5
APP_GROUPS_KEY = "com.apple.security.application-groups"
APP_GROUPS_WORKERS = 8
# ProgramArguments[0] 是解释器时，真正的程序是后面第一个绝对路径参数
INTERPRETERS = frozenset(("sh", "bash", "zsh", "dash", "env", "python", "python3", "osascript", "node", "ruby",
                          "perl", "open", "caffeinate", "nohup"))
SPOTLIGHT_SANITY_BID = "com.apple.finder"   # 任何正常的 Spotlight 索引都该找到它

# 标准 Library 子目录（AppCleaner 硬编码的 66 项，另加现代 macOS 的 HTTPStorages、Application Scripts）：
# 这些目录本身是系统/用户的容器，名字再像软件残留也不能当残留列出来。
STANDARD_LIBRARY_DIRS = frozenset((
    "Accounts", "Application Scripts", "Application Support", "Audio", "Autosave Information", "Caches",
    "Caches/Metadata", "ColorPickers", "Colors", "ColorSync", "Components", "Compositions", "Containers",
    "Contextual Menu Items", "Cookies", "CoreMediaIO", "Desktop Pictures", "Developer", "Dictionaries",
    "DirectoryServices", "Extensions", "Favorites", "Filesystems", "FontCollections", "Fonts", "Frameworks",
    "Graphics", "Group Containers", "HTTPStorages", "IdentityServices", "Image Capture", "Input Methods",
    "Internet Plug-Ins", "Keyboard Layouts", "Keychains", "LanguageModeling", "LaunchAgents", "LaunchDaemons",
    "Logs", "MobileDevice", "Modem Scripts", "OpenDirectory", "PDF Services", "PreferencePanes",
    "Preferences", "Printers", "PrivilegedHelperTools", "PubSub", "QuickLook", "QuickTime", "Receipts",
    "Recent Documents", "Sandbox", "Saved Application State", "ScriptingAdditions", "Scripts", "Security",
    "Services", "Sounds", "Speech", "Spelling", "Spotlight", "StartupItems", "SyncedPreferences", "Video",
    "Voices", "WebKit", "Widgets",
))


def _is_strict_bundle_id(name):
    return bool(STRICT_BID.match(name))


def normalize_bundle_id(bid):
    """AppCleaner 的归一思路：去掉尾部 .trial/.beta/.demo/.debug/.app 等段与纯数字段。
    'com.foo.bar.trial'、'com.foo.bar.2' -> 'com.foo.bar'；只对长度 >= 8 的 id 做（短 id 归一后容易撞车），
    且至少留两段，'com.foo.bar' 再归一也不会变成 'com'。"""
    s = (bid or "").strip()
    if len(s) < MIN_RELAXED_ID_LEN:
        return s
    parts = s.split(".")
    while len(parts) > 2 and (parts[-1].lower() in ID_NOISE_SEGMENTS or parts[-1].isdigit()):
        parts.pop()
    return ".".join(parts)


def name_matches(target, name):
    """整名 + 以分隔符或数字开头的后缀（忽略大小写），不用子串；少于 4 个字符只认整串相等。
    'Notion' 匹配 'Notion Helper'、'notion-enhancer'；'Arc' 不匹配 'Archive'（短名只认整串）。"""
    t = (target or "").strip().lower()
    n = (name or "").strip().lower()
    if not t or not n:
        return False
    if len(t) < MIN_NAME_LEN:
        return n == t
    return re.match(r"^%s(?:[.\-_\s\d]+.*)?$" % re.escape(t), n) is not None


def owned_by_name(target, name):
    """归属判断（保护方向）：名字对得上“已安装的软件 / 正在运行的进程”就不算残留。
    这里匹配得宽是更安全的一侧，所以短名字也按“整名 + 分隔符或数字开头的后缀”认（'Arc' 认 'Arc Helper'，
    仍不认 'Archive'）；AppCleaner 的长度分级是为“找要删的文件”防误删，用在归属判断上会把在用软件的目录报成残留。"""
    t = (target or "").strip().lower()
    n = (name or "").strip().lower()
    if not t or not n:
        return False
    return re.match(r"^%s(?:[.\-_\s\d]+.*)?$" % re.escape(t), n) is not None


def id_matches(bid, name):
    """bundle id 的匹配：先归一，整串相等永远算；id 达到 8 个字符才放宽为前缀匹配。"""
    b = (bid or "").strip().lower()
    n = (name or "").strip().lower()
    if not b or not n:
        return False
    if n == b or normalize_bundle_id(n) == normalize_bundle_id(b):
        return True
    if len(b) < MIN_RELAXED_ID_LEN:
        return False                      # 短 id 只认整串相等
    return name_matches(b, n)


def is_standard_library_dir(path):
    """这个路径是不是 Library 下的标准子目录本身（含 Caches/Metadata 这种两层）。"""
    p = os.path.normpath(path).rstrip("/")
    return any(p.endswith("/Library/" + d) for d in STANDARD_LIBRARY_DIRS)


def _strip_group_prefix(name):
    """Group Containers 名 `<TeamID>.xxx` / `group.xxx` -> xxx；不是这种形态返回 None（不猜团队 ID）。"""
    m = GROUP_PREFIX.match(name or "")
    return m.group(1) if m else None


def _app_groups(app):
    """App 签名里的 app-group 列表（scan 提供时才有；没有就是空，不猜）。"""
    return [g for g in (app.get("app_groups") or []) if isinstance(g, str) and g.strip()]


# ---- Group Containers：读 App 签名里的 app-group ------------------------------------------------
_APP_GROUP_CACHE = {}       # 真实路径 -> app-group 列表；None = 读不到（本次探针内有效）


def _read_app_groups(path):
    """`codesign -d --entitlements :- <path>` 读 com.apple.security.application-groups。
    返回列表（没有权限声明或未签名 = 空列表）；超时、命令缺失、解析失败返回 None（调用方按「读不到」处理）。"""
    key = os.path.realpath(path)
    if key in _APP_GROUP_CACHE:
        return _APP_GROUP_CACHE[key]
    result = None
    try:
        r = subprocess.run(["codesign", "-d", "--entitlements", ":-", path], capture_output=True,
                           timeout=CODESIGN_TIMEOUT, shell=False)
        out = r.stdout or b""
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", "replace").lower()
            result = [] if "not signed at all" in err else None
        elif not out.strip():
            result = []
        else:
            start = out.find(b"<?xml")
            if start < 0:
                start = out.find(b"<plist")
            data = plistlib.loads(out[start:] if start >= 0 else out)
            groups = data.get(APP_GROUPS_KEY) if isinstance(data, dict) else None
            result = [g for g in groups if isinstance(g, str) and g.strip()] if isinstance(groups, list) else []
    except Exception:  # noqa: BLE001  超时 / 命令缺失 / plist 解析失败：一律读不到
        result = None
    _APP_GROUP_CACHE[key] = result
    return result


def _bundle_paths(app_path):
    """App 本体加它 Contents/PlugIns 下的扩展（扩展常单独声明 app-group）。"""
    out = [app_path]
    plugins = os.path.join(app_path, "Contents", "PlugIns")
    try:
        out.extend(sorted(e.path for e in os.scandir(plugins) if e.name.endswith(".appex")))
    except OSError:
        pass
    return out


def collect_app_groups(apps):
    """给每个已装 App 记录补上 app_groups（新字典，不改 scan 的缓存）。
    返回 (带 app_groups 的 apps, 读不到的路径列表)；读不到列表非空就不能断言某 group 无人用。"""
    recs = [dict(a) for a in apps.get("apps") or []]
    targets = []
    for a in recs:
        for bp in _bundle_paths(a.get("path") or ""):
            targets.append((a, bp))
    with ThreadPoolExecutor(max_workers=APP_GROUPS_WORKERS) as ex:
        results = list(ex.map(lambda t: _read_app_groups(t[1]), targets))
    failed = []
    for (a, bp), groups in zip(targets, results):
        if groups is None:
            failed.append(bp)
            continue
        a.setdefault("app_groups", [])
        a["app_groups"] = list(a["app_groups"]) + groups
    return dict(apps, apps=recs), failed


# ---- LaunchAgents：plist 指向的程序还在、或 launchd 已加载就是在用 -------------------------------
def _launch_program(plist_path):
    """plist 的 (Label, 可执行文件路径)。Program 优先，否则 ProgramArguments[0]；
    第一项是解释器（sh、bash、env、python3……）时取后面第一个绝对路径参数。读不出抛异常。"""
    with open(plist_path, "rb") as f:
        data = plistlib.load(f)
    if not isinstance(data, dict):
        raise ValueError("plist 不是字典")
    label = data.get("Label") if isinstance(data.get("Label"), str) else None
    prog = data.get("Program")
    if isinstance(prog, str) and prog:
        return label, prog
    args = data.get("ProgramArguments")
    if isinstance(args, list) and args and isinstance(args[0], str) and args[0]:
        first = args[0]
        if os.path.basename(first).lower() in INTERPRETERS:
            for extra in args[1:]:
                if isinstance(extra, str) and extra.startswith("/"):
                    return label, extra
        return label, first
    return label, None


def _launchctl_loaded(label):
    """`launchctl print gui/<uid>/<label>` 退出码 0 = 已加载。超时、命令缺失抛异常（调用方按「在用」处理）。"""
    r = subprocess.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)], capture_output=True,
                       timeout=LAUNCHCTL_TIMEOUT, shell=False)
    return r.returncode == 0


def launch_agent_in_use(plist_path, fallback_label):
    """返回 (在用?, 原因)。读不到 plist、launchctl 超时一律往「在用」方向。"""
    try:
        label, exe = _launch_program(plist_path)
    except Exception:  # noqa: BLE001
        return True, "plist_unreadable"
    if exe and os.path.exists(exe):
        return True, "program_exists"
    try:
        if _launchctl_loaded(label or fallback_label):
            return True, "launchctl_loaded"
    except Exception:  # noqa: BLE001
        return True, "launchctl_inconclusive"
    return False, None


def owner_of(name, apps):
    """这个残留名字归哪个还在装的 App：返回 (app 记录, 命中规则)；归不上返回 (None, None)。
    规则顺序 = id 整串相等 -> id 归一后相等 -> id 前缀（仅 id >= 8）-> 名字匹配 -> app-group。"""
    low = (name or "").strip().lower()
    if not low:
        return None, None
    norm = normalize_bundle_id(low)
    for app in apps.get("apps") or []:
        bid = (app.get("bundle_id") or "").strip().lower()
        if bid:
            if low == bid:
                return app, "id_exact"
            if norm == normalize_bundle_id(bid):
                return app, "id_normalized"
            if len(bid) >= MIN_RELAXED_ID_LEN and name_matches(bid, low):
                return app, "id_prefix"
        aname = (app.get("name") or "").strip()
        if aname and owned_by_name(aname, low):
            return app, "name_match"
        for grp in _app_groups(app):
            g = grp.strip().lower()
            if not g:
                continue
            if low == g or norm == normalize_bundle_id(g):
                return app, "app_group"
            gs = _strip_group_prefix(g)          # 签名里 app-group 常写成 <TeamID>.xxx / group.xxx
            if gs and (low == gs.lower() or norm == normalize_bundle_id(gs)):
                return app, "app_group"
    return None, None


def _process_names():
    """当前正在运行的进程名（小写集合）。命令缺失、超时、非 0/1 退出码都抛异常（调用方按「在运行」保守处理）。"""
    cp = subprocess.run(["pgrep", "-lx", "--", ".+"], capture_output=True, text=True,
                        timeout=PGREP_TIMEOUT, shell=False)
    if cp.returncode != 0:
        raise RuntimeError("pgrep 退出码 %s" % cp.returncode)
    names = set()
    for line in cp.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1].strip():
            names.add(parts[1].strip().lower())
    return names


def _process_protects(name, running):
    """正在运行的进程名和这个残留名字对得上（整串相等或名字规则命中）就保护它：
    目录名整串、或目录名最后一段（bundle id 的软件名部分）与进程名对得上。"""
    cands = {name.lower()}
    seg = name.rsplit(".", 1)[-1].lower()
    if seg:
        cands.add(seg)
    for p in running:
        for c in cands:
            if p == c or owned_by_name(p, c) or owned_by_name(c, p):
                return True
    return False


def _entry_stats(path, is_dir):
    """目录按 _walk_stats 遍历；单个文件直接 lstat（文件不是目录，scandir 会报错）。"""
    if is_dir:
        return _walk_stats(path)
    st = os.lstat(path)
    return st.st_mtime, st.st_blocks * 512, 1


def _walk_stats(top, deadline_s=WALK_MAX_SECONDS, max_entries=WALK_MAX_ENTRIES):
    """只读 lstat 遍历（不跟随符号链接）。返回 (最新 mtime, 字节数 st_blocks*512, 条目数)；
    超过条目上限或时间上限、或任何 stat/listdir 失败抛 OSError/TimeoutError（调用方据此跳过）。"""
    t0 = time.monotonic()
    newest = 0.0
    total = 0
    count = 0
    seen = set()
    stack = [top]
    while stack:
        d = stack.pop()
        st = os.lstat(d)
        count += 1
        newest = max(newest, st.st_mtime)
        if (st.st_dev, st.st_ino) not in seen:
            seen.add((st.st_dev, st.st_ino))
            total += st.st_blocks * 512
        with os.scandir(d) as it:
            for e in it:
                count += 1
                if count > max_entries:
                    raise TimeoutError("entries>%d" % max_entries)
                if time.monotonic() - t0 > deadline_s:
                    raise TimeoutError("time>%ss" % deadline_s)
                est = e.stat(follow_symlinks=False)
                newest = max(newest, est.st_mtime)
                key = (est.st_dev, est.st_ino)
                if key not in seen:
                    seen.add(key)
                    total += est.st_blocks * 512
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
    return newest, total, count


def _spotlight_apps(bundle_id):
    """mdfind 查 bundle id，返回路径列表；任何失败（超时、非零退出、命令缺失）抛异常。"""
    r = subprocess.run(["mdfind", "kMDItemCFBundleIdentifier == '%s'" % bundle_id],
                       capture_output=True, text=True, timeout=MDFIND_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError("mdfind 退出码 %s" % r.returncode)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _brew_executable():
    return shutil.which("brew") or next((p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
                                         if os.path.exists(p)), None)


def _brew_installed_casks():
    """brew info --cask --json=v2 --installed -> casks 列表；失败抛异常（调用方忽略）。只含已安装 cask。"""
    brew = _brew_executable()
    if not brew:
        raise RuntimeError("找不到 brew")
    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_ANALYTICS="1")
    r = subprocess.run([brew, "info", "--cask", "--json=v2", "--installed"], capture_output=True, text=True,
                       timeout=BREW_TIMEOUT, env=env)
    if r.returncode != 0:
        raise RuntimeError("brew 退出码 %s" % r.returncode)
    return json.loads(r.stdout).get("casks") or []


def _zap_paths(cask):
    """cask 的 zap 里 trash/delete 的路径（展开 ~）。"""
    out = []
    for art in cask.get("artifacts") or []:
        if not isinstance(art, dict):
            continue
        for block in art.get("zap") or []:
            if not isinstance(block, dict):
                continue
            for key in ("trash", "delete"):
                v = block.get(key)
                for p in ([v] if isinstance(v, str) else (v or [])):
                    if isinstance(p, str) and p:
                        out.append(protect.HOME + p[1:] if p.startswith("~") else p)
    return out


def _zap_owner(path, zap_index):
    """候选路径被某已装 cask 的 zap 覆盖（相等、通配符命中、或 zap 路径在候选目录之内）时返回 cask token。"""
    p = path.rstrip("/")
    for token, paths in zap_index:
        for z in paths:
            z = z.rstrip("/")
            if z == p or z.startswith(p + "/"):
                return token
            if any(c in z for c in "*?[") and fnmatch.fnmatchcase(p, z):
                return token
    return None


def probe_leftovers(opts=None):
    min_age = getattr(opts, "min_age_days", None)
    min_age = DEFAULT_MIN_AGE_DAYS if min_age is None else min_age
    use_brew = bool(getattr(opts, "brew_zap", False))
    now = time.time()
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "inventory_complete": False, "min_age_days": min_age, "candidates": [], "skipped": {},
           "not_evaluated_locations": [{"location": "~/Library/" + n, "reason": why} for n, why in NOT_EVALUATED],
           "group_containers_evaluated": None, "errors": []}
    skipped = out["skipped"]
    _APP_GROUP_CACHE.clear()
    group_state = {"done": False, "apps": None, "failed": []}

    def group_apps():
        """惰性读一次所有 App 的 app-group；返回带 app_groups 的清单，有任何读不到的返回 None。"""
        if not group_state["done"]:
            group_state["done"] = True
            try:
                group_state["apps"], group_state["failed"] = collect_app_groups(apps)
            except Exception as exc:  # noqa: BLE001
                group_state["apps"], group_state["failed"] = None, ["%s: %s" % (type(exc).__name__, exc)]
            if group_state["failed"]:
                group_state["apps"] = None
                out["errors"].append({"stage": "app_groups", "error": "读不到 %d 个签名的 app-group：%s"
                                      % (len(group_state["failed"]), "；".join(group_state["failed"][:5]))})
                out["group_containers_evaluated"] = False
                for loc in out["not_evaluated_locations"]:
                    if loc["location"] == "~/Library/Group Containers":
                        loc["reason"] = ("读不到部分 App 签名里的 app-group（%d 处），无法断言哪个 group 没人用，整个位置不评估"
                                         % len(group_state["failed"]))
            else:
                out["group_containers_evaluated"] = True
        return group_state["apps"]

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    # 底线 2：清单不完整（或枚举本身出错）就拒绝
    try:
        apps = scan.installed_apps()
        complete = bool(apps.get("complete"))
    except Exception as exc:  # noqa: BLE001
        return {"refused": True, "reason": "无法枚举已安装应用（%s: %s），不输出候选" % (type(exc).__name__, exc),
                "inventory_complete": False, "not_evaluated_locations": out["not_evaluated_locations"]}
    if not complete:
        return {"refused": True,
                "reason": "已安装应用清单不完整（%d 个读取错误），无法断言某个 bundle id 未安装，不输出候选"
                          % len(apps.get("errors") or []),
                "inventory_complete": False, "not_evaluated_locations": out["not_evaluated_locations"]}
    out["inventory_complete"] = True

    spotlight_ok = None   # 惰性做一次健全性检查：Spotlight 关闭时 mdfind 会静默返回空，不能当“没找到”
    zap_index = []
    proc = {"done": False, "names": None}

    def running_names():
        """惰性取一次进程名单；取不到返回 None，调用方按「在运行」保守处理。"""
        if not proc["done"]:
            proc["done"] = True
            try:
                proc["names"] = _process_names()
            except Exception as exc:  # noqa: BLE001  读不到进程名单：宁可漏报也不动还在跑的软件
                out["errors"].append({"stage": "processes", "error": "%s: %s" % (type(exc).__name__, exc)})
        return proc["names"]

    if use_brew:
        try:
            zap_index = [(c.get("token"), _zap_paths(c)) for c in _brew_installed_casks() if c.get("token")]
        except Exception as exc:  # noqa: BLE001  zap 只是佐证，失败忽略
            out["errors"].append({"stage": "brew_zap", "error": "%s: %s" % (type(exc).__name__, exc)})

    lib = protect.HOME + "/Library"
    for rule in LOCATION_RULES:
        base = lib + "/" + rule.loc
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name)
        except FileNotFoundError:
            continue
        except OSError as exc:
            out["errors"].append({"path": base, "error": "listdir失败: %s" % exc})
            skip("location_unreadable")
            continue
        for e in entries:
            try:
                path = e.path
                # 标准 Library 子目录本身（含 Caches/Metadata 这种两层）永不算残留
                if is_standard_library_dir(path):
                    skip("standard_library_dir")
                    continue
                if rule.kind == KIND_DIR:
                    # 0.5 口径：第一层目录，名字必须是 bundle id 形式，计数口径不变
                    if rule.suffix:
                        if not e.name.endswith(rule.suffix) or e.name == rule.suffix:
                            skip("skipped_non_bundle_id")
                            continue
                        name = e.name[:-len(rule.suffix)]
                    else:
                        name = e.name
                    if not _is_strict_bundle_id(name):
                        skip("skipped_non_bundle_id")
                        continue
                    if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                        skip("not_plain_directory")
                        continue
                    is_dir = True
                elif rule.kind == KIND_FILE:
                    # LaunchAgents / Cookies：只认 <id><后缀> 的普通文件，别的形状不看也不计数
                    if e.is_symlink() or e.is_dir(follow_symlinks=False) or not e.name.endswith(rule.suffix):
                        continue
                    name = e.name[:-len(rule.suffix)]
                    if not _is_strict_bundle_id(name) or len(name) < rule.min_id:
                        continue
                    is_dir = False
                else:
                    # Group Containers：只认 <TeamID>.xxx / group.xxx，去掉前缀后还得是 bundle id
                    if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                        continue
                    name = _strip_group_prefix(e.name)
                    if not name or not _is_strict_bundle_id(name):
                        continue
                    is_dir = True
                if rule.kind == KIND_FILE:
                    # LaunchAgents / Cookies 不在 scan 的已知 Library 位置里，反查只会得到「不确定」；
                    # 这两个位置只按共享组件名单 + AppCleaner 的名字规则判断。
                    if scan.is_shared_component(name):
                        skip("shared_component")
                        continue
                    facts = {"app": None, "app_match": None, "app_installed": False}
                else:
                    if rule.kind == KIND_GROUP and group_apps() is None:
                        skip("group_containers_not_evaluated")
                        continue
                    try:
                        facts = scan.reverse_lookup(path, apps)
                    except Exception:  # noqa: BLE001  反查异常 -> 当作应用还在
                        skip("lookup_error")
                        continue
                    if facts.get("shared_component"):
                        skip("shared_component")
                        continue
                    if facts.get("app") is not None:
                        skip("app_installed:%s" % (facts.get("app_match") or "unknown"))
                        continue
                    if facts.get("app_installed") is not False:
                        skip("lookup_inconclusive")
                        continue
                owner, why = owner_of(name, group_apps() if rule.kind == KIND_GROUP else apps)   # 再按 AppCleaner 的匹配规则复核一遍
                if owner is not None:
                    skip("app_installed:%s" % why)
                    continue
                running = running_names()
                if running is None:
                    skip("process_check_failed")
                    continue
                if _process_protects(name, running):
                    skip("app_running")
                    continue
                try:
                    newest, size, _ = _entry_stats(path, is_dir)
                except TimeoutError:
                    skip("walk_limit_exceeded")
                    continue
                except OSError:
                    skip("stat_error")
                    continue
                age_days = (now - newest) / 86400.0
                if age_days < min_age:
                    skip("too_recent")
                    continue
                if rule.match_rule == "launch_agent":
                    in_use, why_use = launch_agent_in_use(path, name)
                    if in_use:
                        skip("launch_agent_in_use:%s" % why_use)
                        continue
                # Spotlight：装在清单根目录之外（Setapp、自定义目录、iOS Wrapper）的 .app
                if spotlight_ok is None:
                    try:
                        spotlight_ok = any(p.endswith(".app") for p in _spotlight_apps(SPOTLIGHT_SANITY_BID))
                    except Exception as exc:  # noqa: BLE001
                        spotlight_ok = False
                        out["errors"].append({"stage": "spotlight", "error": "%s: %s" % (type(exc).__name__, exc)})
                    if not spotlight_ok:
                        out["errors"].append({"stage": "spotlight", "error": "Spotlight 不可用或索引为空，全部候选按“还在”处理"})
                if not spotlight_ok:
                    skip("mdfind_failed")
                    continue
                try:
                    found = _spotlight_apps(name)
                except Exception:  # noqa: BLE001
                    skip("mdfind_failed")
                    continue
                if any(".app" in p for p in found):
                    skip("found_by_spotlight")
                    continue
                zap_owner = _zap_owner(path, zap_index) if zap_index else None
                if zap_owner:
                    skip("owned_by_installed_cask")
                    continue
                cand = {"path": path, "name": name, "location": rule.loc, "bytes": size,
                        "age_days": int(age_days), "reason": "no_installed_app", "match_rule": rule.match_rule}
                if rule.shared:
                    cand["shared"] = True
                if rule.safety:
                    cand["safety"] = rule.safety
                    cand["default_select"] = False
                    cand["note"] = rule.note
                out["candidates"].append(cand)
            except Exception as exc:  # noqa: BLE001  任何意外都往“还在”方向
                skip("unexpected_error")
                out["errors"].append({"path": getattr(e, "path", "?"), "error": "%s: %s" % (type(exc).__name__, exc)})
    out["candidates"].sort(key=lambda c: (-c["bytes"], c["path"]))
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="已卸载应用遗留的低风险缓存（只读）")
    ap.add_argument("--min-age-days", dest="min_age_days", type=int, default=DEFAULT_MIN_AGE_DAYS)
    ap.add_argument("--brew-zap", dest="brew_zap", action="store_true")
    print(json.dumps(probe_leftovers(ap.parse_args()), ensure_ascii=False, indent=1))
