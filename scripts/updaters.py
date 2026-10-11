#!/usr/bin/env python3
"""只读探针：软件更新暂存（Sparkle / ShipIt / electron-updater / Google Updater / 其他）是否已被已装版本取代。

判据见 references/cards/app-updaters-installers.md：
  superseded       暂存与已装应用 bundle id 相同，暂存版本不高于已装版本
  pending_install  同 id，暂存版本高于已装版本（待安装的更新，保留）
  orphaned_update  暂存读得出 bundle id，但没有已装应用（只作事实，不建议）
  unknown          读不出 / 不比较 / 已装多副本 / 清单不完整
  in_use           更新器进程在跑，或目录里有打开的文件（覆盖上面的结论，原结论放 base_verdict）

只读：不挂载 dmg，不解压 zip（只读一个 Info.plist），不删、不改任何文件。lsof/进程查询失败时 in_use=None（无法确认，不可当成未占用）。
"""
import os
import plistlib
import re
import stat as statmod
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protect  # noqa: E402
import scan  # noqa: E402  复用 installed_apps()

LSOF_TIMEOUT = 20
LSOF_BATCH = 200
LSOF_MAX_FILES = 3000
PGREP_TIMEOUT = 10
MAX_PLIST = 5 * 1024 * 1024
MAX_HINT = 1024 * 1024
PACKAGE_DEPTH = 4
PROC_KEYWORDS = re.compile(r"shipit|updat|sparkle|autoupdate", re.I)

# 最浅一层的 <X>.app/Contents/Info.plist（前面可有一层包裹目录）；嵌套 Frameworks/Helpers 里的更深，靠 min depth 排除
_APP_PLIST_RE = re.compile(r"^(?:.*/)?[^/]+\.app/Contents/Info\.plist$")
_NUMERIC_VERSION = re.compile(r"^\d+(?:\.\d+)*$")
_BUNDLE_ID_SHAPE = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2,}$")

PRIORITY = {"pending_install": 0, "unknown": 1, "orphaned_update": 2, "superseded": 3}
VERDICTS = ("superseded", "pending_install", "orphaned_update", "unknown", "in_use")


# ───────────────────────── 外部命令（测试里整体替换） ─────────────────────────

def lsof_open(paths):
    """返回这些路径当前被打开的文件名列表；None 表示无法确认（失败/超时/文件太多）。"""
    found = []
    failed = False
    for i in range(0, len(paths), LSOF_BATCH):
        batch = paths[i:i + LSOF_BATCH]
        try:
            cp = subprocess.run(["lsof", "-n", "-P", "-Fn", "--"] + batch, capture_output=True, text=True,
                                timeout=LSOF_TIMEOUT, shell=False)
        except (subprocess.TimeoutExpired, OSError):
            failed = True
            continue
        if cp.returncode not in (0, 1):
            failed = True
            continue
        found.extend(line[1:] for line in cp.stdout.splitlines() if line.startswith("n") and len(line) > 1)
    if found:
        return found
    return None if failed else []


def pgrep_processes():
    """返回 [(pid, 命令行)]，只含命令行里带 shipit/updat/sparkle 的进程；None 表示无法查询。"""
    try:
        cp = subprocess.run(["pgrep", "-fli", "--", "shipit|updat|sparkle"], capture_output=True, text=True,
                            timeout=PGREP_TIMEOUT, shell=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if cp.returncode not in (0, 1):
        return None
    me = {os.getpid(), os.getppid()}
    out = []
    for line in cp.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit() or int(parts[0]) in me:
            continue
        out.append((int(parts[0]), parts[1]))
    return out


# ───────────────────────── 版本 ─────────────────────────

def _num_tuple(s):
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not _NUMERIC_VERSION.match(s):
        return None
    return tuple(int(x) for x in s.split("."))


def _cmp_tuples(a, b):
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def compare_versions(staged_short, staged_build, inst_short, inst_build):
    """返回 'lt'/'eq'/'gt'/None。优先比 ShortVersion；任一方非纯数字就退回比 CFBundleVersion。
    ShortVersion 相等但暂存的 build 号更高（均为纯数字）按 gt 处理（可能是同版本的新构建，保守保留）。"""
    a, b = _num_tuple(staged_short), _num_tuple(inst_short)
    if a is not None and b is not None:
        c = _cmp_tuples(a, b)
        if c == 0:
            ba, bb = _num_tuple(staged_build), _num_tuple(inst_build)
            if ba is not None and bb is not None and _cmp_tuples(ba, bb) > 0:
                return "gt"
        return "gt" if c > 0 else "lt" if c < 0 else "eq"
    ba, bb = _num_tuple(staged_build), _num_tuple(inst_build)
    if ba is not None and bb is not None:
        c = _cmp_tuples(ba, bb)
        return "gt" if c > 0 else "lt" if c < 0 else "eq"
    return None


# ───────────────────────── 读暂存包 ─────────────────────────

def _info_dict(info, source):
    return {"bundle_id": info.get("CFBundleIdentifier"), "short_version": info.get("CFBundleShortVersionString"),
            "version": info.get("CFBundleVersion"), "source": source}


def read_zip_app_info(path):
    """只取最浅一层 <X>.app/Contents/Info.plist。返回 (staged dict | None, reason | None)。"""
    try:
        with zipfile.ZipFile(path) as z:
            infos = {i.filename: i for i in z.infolist()}
            matches = [n for n in infos if _APP_PLIST_RE.match(n) and not n.startswith("__MACOSX/")]
            if not matches:
                return None, "no_app_plist_in_archive"
            depth = min(n.count("/") for n in matches)
            shallow = [n for n in matches if n.count("/") == depth]
            found = []
            for n in shallow:
                if infos[n].file_size > MAX_PLIST:
                    return None, "plist_too_large"
                found.append((n, plistlib.loads(z.read(n))))
    except (zipfile.BadZipFile, OSError, plistlib.InvalidFileException, ValueError, KeyError, NotImplementedError,
            RuntimeError, EOFError):
        return None, "archive_unreadable"
    ids = {i.get("CFBundleIdentifier") for _, i in found}
    if len(ids) > 1:
        return None, "ambiguous_app_in_archive"
    n, info = found[0]
    return _info_dict(info, "zip:" + n), None


def read_app_info(path):
    try:
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            return _info_dict(plistlib.load(f), "app:Contents/Info.plist"), None
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None, "plist_unreadable"


def _read_small(path):
    try:
        if os.lstat(path).st_size > MAX_HINT:
            return None
        with open(path, "rb") as f:
            return f.read(MAX_HINT).decode("utf-8", "replace")
    except OSError:
        return None


def read_version_hint(root):
    """Electron 的 update-info.json / latest-mac.yml 里的 version，只作辅助。返回 (version, 文件名) 或 (None, None)。"""
    import json
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            it = list(os.scandir(d))
        except OSError:
            continue
        for e in it:
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < 2 and not e.name.endswith(".app"):
                        stack.append((e.path, depth + 1))
                    continue
            except OSError:
                continue
            if e.name == "update-info.json":
                text = _read_small(e.path)
                try:
                    v = json.loads(text).get("version") if text else None
                except (ValueError, AttributeError):
                    v = None
                if isinstance(v, str):
                    return v, e.name
            elif e.name == "latest-mac.yml":
                text = _read_small(e.path)
                m = re.search(r"^version:\s*['\"]?([^\s'\"]+)", text or "", re.M)
                if m:
                    return m.group(1), e.name
    return None, None


# ───────────────────────── 发现候选与包 ─────────────────────────

def _is_bundle_id(s):
    return bool(s) and bool(_BUNDLE_ID_SHAPE.match(s))


def _classify_file(name):
    low = name.lower()
    if low.endswith(".zip"):
        return "zip"
    if low.endswith(".dmg"):
        return "dmg"
    if low.endswith((".pkg", ".mpkg")):
        return "pkg"
    if low.endswith((".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".txz", ".tbz")):
        return "tar"
    if low.endswith((".xip", ".iso")):
        return "xip"
    return None


def _sniff_extensionless(path, size):
    """crx_cache 里的包没有扩展名：按魔数认 zip / CRX3（头部 + zip）。"""
    if size < 64:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return None
    if head == b"PK\x03\x04":
        return "zip"
    if head == b"Cr24":
        return "zip"   # CRX3 = 头部 + zip，zipfile 可读
    return None


def find_packages(root, skip_names=(), sniff=False, errors=None):
    """在候选目录里找暂存包：(类型, 路径)。.app 目录不再深入；类型 app/zip/dmg/pkg/tar/xip。"""
    out = []
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError as exc:
            if errors is not None:
                errors.append({"path": d, "error": "listdir失败: %s" % exc})
            continue
        for e in entries:
            if e.name in skip_names:
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if statmod.S_ISLNK(st.st_mode):
                continue
            if statmod.S_ISDIR(st.st_mode):
                if e.name.endswith(".app"):
                    out.append(("app", e.path))
                elif depth < PACKAGE_DEPTH:
                    stack.append((e.path, depth + 1))
            elif statmod.S_ISREG(st.st_mode):
                kind = _classify_file(e.name)
                if kind is None and sniff and "." not in e.name:
                    kind = _sniff_extensionless(e.path, st.st_size)
                if kind:
                    out.append((kind, e.path))
    return out


def walk_tree(root):
    """st_blocks*512 求和（不跟随符号链接，硬链接只计一次）、最新 mtime、文件列表（有上限）。"""
    try:
        st0 = os.lstat(root)
    except OSError:
        return 0, None, [], False
    if not statmod.S_ISDIR(st0.st_mode):
        return st0.st_blocks * 512, st0.st_mtime, [root], False
    total = 0
    newest = st0.st_mtime
    files = []
    truncated = False
    seen = set()
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                newest = max(newest, st.st_mtime)
                if statmod.S_ISDIR(st.st_mode):
                    stack.append(e.path)
                    continue
                if st.st_nlink > 1:
                    key = (st.st_dev, st.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                total += st.st_blocks * 512
                if statmod.S_ISREG(st.st_mode):
                    if len(files) < LSOF_MAX_FILES:
                        files.append(e.path)
                    else:
                        truncated = True
    return total, newest, files, truncated


def discover_candidates(home, errors):
    """返回 [{path, kind, owner_hint, shared, skip}]。"""
    cands = []
    caches = os.path.join(home, "Library", "Caches")

    def add(path, kind, owner, shared=False, skip=(), sniff=False):
        cands.append({"path": path, "kind": kind, "owner_hint": owner, "shared": shared, "skip": skip,
                      "sniff": sniff})

    try:
        entries = sorted(os.scandir(caches), key=lambda e: e.name)
    except OSError as exc:
        entries = []
        if os.path.lexists(caches):
            errors.append({"path": caches, "error": "listdir失败: %s" % exc})
    for e in entries:
        try:
            if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        name = e.name
        spk = os.path.join(e.path, "org.sparkle-project.Sparkle")
        try:
            if statmod.S_ISDIR(os.lstat(spk).st_mode):
                add(spk, "sparkle", name, skip=("Launcher",))   # Launcher 里是更新器自己的副本，不是暂存
        except OSError:
            pass
        if name.endswith(".ShipIt"):
            add(e.path, "shipit", name[:-len(".ShipIt")])
        elif name.endswith("-updater"):
            add(e.path, "electron-updater", name[:-len("-updater")])
        elif name.lower().startswith("com.microsoft.autoupdate") or name == "com.microsoft.EdgeUpdater":
            add(e.path, "other", name)

    gu = os.path.join(home, "Library", "Application Support", "Google", "GoogleUpdater")
    crx = os.path.join(gu, "crx_cache")
    if os.path.isdir(crx) and not os.path.islink(crx):
        add(crx, "google-updater", "GoogleUpdater", shared=True, sniff=True)
    try:
        for e in sorted(os.scandir(gu), key=lambda x: x.name):
            if e.name.endswith(".old"):
                add(e.path, "google-updater", "GoogleUpdater", shared=True)
    except OSError:
        pass
    for rel, owner in ((("Microsoft", "AutoUpdate"), "Microsoft AutoUpdate"), (("zoom.us", "AutoUpdater"), "zoom.us")):
        p = os.path.join(home, "Library", "Application Support", *rel)
        if os.path.isdir(p) and not os.path.islink(p):
            add(p, "other", owner)
    return cands


# ───────────────────────── 判定 ─────────────────────────

def evaluate_package(staged, reason_hint, inventory, shared=False):
    """staged 为 dict 或 None。返回 (verdict, reason, installed | None)。"""
    if staged is None:
        return "unknown", reason_hint or "staged_unreadable", None
    bid = staged.get("bundle_id")
    if not bid:
        return "unknown", reason_hint or "bundle_id_unreadable", None
    if shared and scan.is_shared_component(str(bid)):
        return "unknown", "shared_component_package", None   # 更新器自己的包，不归属任何应用
    if not inventory.get("complete", False):
        return "unknown", "inventory_incomplete", None
    matches = (inventory.get("by_id") or {}).get(str(bid).lower(), [])
    if not matches:
        if shared:
            return "unknown", "shared_component_no_installed_match", None
        return "orphaned_update", "no_installed_app_with_same_bundle_id", None
    if len(matches) > 1:
        return "unknown", "multiple_installed", None
    m = matches[0]
    inst = {"path": m.get("path"), "short_version": m.get("short_version"), "version": m.get("version")}
    rel = compare_versions(staged.get("short_version"), staged.get("version"),
                           inst["short_version"], inst["version"])
    if rel is None:
        return "unknown", "version_unparseable", inst
    if rel == "gt":
        return "pending_install", "staged_version_higher_than_installed", inst
    if rel == "eq":
        return "superseded", "staged_version_equals_installed", inst
    return "superseded", "staged_version_lower_than_installed", inst


def inspect_package(kind, path, hint_version, hint_src):
    """读一个暂存包的版本信息。返回 (staged dict | None, reason | None)。"""
    if kind == "app":
        staged, reason = read_app_info(path)
    elif kind == "zip":
        staged, reason = read_zip_app_info(path)
    else:
        return None, "%s_not_inspected" % kind
    if staged is not None and hint_version:
        staged["hint_version"] = hint_version
        staged["hint_source"] = hint_src
        if not staged.get("short_version") and not staged.get("version"):
            staged["short_version"] = hint_version
            staged["source"] += "+" + str(hint_src)
    if staged is None and hint_version:
        # 包读不出时，辅助版本只作记录；没有 bundle id 就不能与已装应用比较
        return {"bundle_id": None, "short_version": hint_version, "version": None, "source": hint_src}, reason
    return staged, reason


def _token_re(tok):
    return re.compile(r"(?<![A-Za-z0-9._-])" + re.escape(tok) + r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9])", re.I)


def match_processes(procs, tokens):
    """进程命令行含更新器关键字，并且含该应用的 bundle id / 目录名 / 路径才算它的（ShipIt 同名，不能只看进程名）。
    bundle id 要完整匹配：com.x.app 不匹配 com.x.app.nightly。"""
    res = []
    toks = [_token_re(t) for t in tokens if t]
    for pid, cmd in procs or []:
        if PROC_KEYWORDS.search(cmd) and any(r.search(cmd) for r in toks):
            res.append({"pid": pid, "command": cmd[:200]})
    return res


def _tokens(cand, ids, installed_paths):
    """归属令牌。目录名只在足够具体时用（shipit / electron-updater）；sparkle 的固定子目录名、
    AutoUpdate 这类通用名不能当令牌，否则会匹配到别的应用的进程。"""
    kind, owner = cand["kind"], cand["owner_hint"]
    toks = [cand["path"]]
    if kind == "shipit":
        toks.append(os.path.basename(cand["path"]))
    elif kind == "electron-updater":
        toks.append(os.path.basename(cand["path"]))
    elif kind == "google-updater":
        toks.append("GoogleUpdater")
    elif kind == "other":
        toks.append(owner)
    if _is_bundle_id(owner):
        toks.append(owner)
    toks.extend(i for i in ids if i)
    toks.extend(installed_paths)
    return list(dict.fromkeys(toks))


BENIGN_FILES = {".DS_Store", ".localized", "update-info.json", "latest-mac.yml"}


def unclassified_files(root, files, results, skip_names=()):
    """目录里没有被分类为暂存包、也不在包内部的文件：探针不认识它们是什么，不能凭其余包 superseded 就整目录放行。
    .DS_Store 和版本提示文件（update-info.json / latest-mac.yml）不算；被 skip_names 跳过的子目录不看。"""
    pkg_roots = [r["path"] for r in results]
    out = []
    for f in files:
        if os.path.basename(f) in BENIGN_FILES:
            continue
        if any(f == r or f.startswith(r.rstrip("/") + "/") for r in pkg_roots):
            continue
        rel = os.path.relpath(f, root)
        if rel != f and any(part in skip_names for part in rel.split(os.sep)[:-1]):
            continue
        out.append(f)
    return out


def build_item(cand, inventory, procs, now, errors):
    path = cand["path"]
    nbytes, newest, files, truncated = walk_tree(path)
    age = None
    if newest is not None:
        age = round(max(0.0, now - newest) / 86400.0, 1)

    is_dir_cand = True
    try:
        is_dir_cand = statmod.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        pass
    pkgs = []
    if is_dir_cand:
        pkgs = find_packages(path, skip_names=cand.get("skip", ()), sniff=cand.get("sniff", False), errors=errors)
    hint_version, hint_src = (read_version_hint(path) if is_dir_cand else (None, None))

    results = []
    for kind, p in pkgs:
        staged, why = inspect_package(kind, p, hint_version, hint_src)
        verdict, reason, inst = evaluate_package(staged, why, inventory, shared=bool(cand.get("shared")))
        results.append({"path": p, "type": kind, "staged": staged, "installed": inst,
                        "verdict": verdict, "reason": reason})

    if not results:
        if cand["kind"] == "google-updater" and os.path.basename(path).endswith(".old"):
            verdict, reason = "unknown", "updater_log_or_history"
        else:
            verdict, reason = "unknown", "no_staged_package"
        staged = {"bundle_id": None, "short_version": hint_version, "version": None,
                  "source": hint_src} if hint_version else {"bundle_id": None, "short_version": None,
                                                              "version": None, "source": None}
        primary = {"staged": staged, "installed": None, "verdict": verdict, "reason": reason}
    else:
        primary = min(results, key=lambda r: PRIORITY[r["verdict"]])

    ids = [r["staged"]["bundle_id"] for r in results if r["staged"] and r["staged"].get("bundle_id")]
    inst_paths = [r["installed"]["path"] for r in results if r["installed"] and r["installed"].get("path")]
    matched = match_processes(procs, _tokens(cand, ids, inst_paths)) if procs is not None else []

    open_list = None if truncated else lsof_open([path] + files)
    in_use = None
    if matched or open_list:
        in_use = True
    elif open_list is not None and procs is not None:
        in_use = False

    # superseded 的前提是「这个目录里的东西探针都认识、也确认没人在用」：有认不出的文件，或占用状态无法确认，一律 unknown
    final_verdict, final_reason = primary["verdict"], primary["reason"]
    if final_verdict == "superseded":
        stray = unclassified_files(path, files, results, cand.get("skip", ())) if is_dir_cand else []
        if stray or (is_dir_cand and truncated):
            final_verdict = "unknown"
            final_reason = "unclassified_files"
        elif in_use is None:
            final_verdict = "unknown"
            final_reason = "in_use_unknown"

    item = {
        "path": path,
        "bytes": nbytes,
        "kind": cand["kind"],
        "owner_hint": cand["owner_hint"],
        "shared_component": bool(cand.get("shared")),
        "staged": primary["staged"],
        "installed": primary["installed"],
        "verdict": final_verdict,
        "reason": final_reason,
        "base_verdict": final_verdict,
        "packages": [{"path": r["path"], "type": r["type"], "verdict": r["verdict"], "reason": r["reason"],
                      "staged": r["staged"], "installed": r["installed"]} for r in results],
        "in_use": in_use,
        "open_files": (open_list or [])[:5],
        "processes": matched,
        "age_days": age,
    }
    if in_use is None:
        item["in_use_note"] = "无法确认占用（lsof/进程查询失败或文件过多），不可当成未占用"
    if final_reason == "unclassified_files":
        item["unclassified_note"] = "目录内有未被分类的文件（如 %s），不判 superseded" % (
            (unclassified_files(path, files, results, cand.get("skip", ())) or ["文件过多被截断"])[0])
    if in_use:
        item["verdict"] = "in_use"
        item["reason"] = "open_files" if open_list else "updater_process_running"
    return item


def probe_updaters(opts=None):
    now = time.time()
    errors = []
    inventory = scan.installed_apps()
    for e in inventory.get("errors") or []:
        errors.append({"path": e.get("path"), "error": "已装清单：%s" % e.get("error")})
    home = protect.HOME
    cands = discover_candidates(home, errors)
    procs = pgrep_processes() if cands else []
    if procs is None:
        errors.append({"path": "pgrep", "error": "进程查询失败，in_use 无法确认"})
    items = [build_item(c, inventory, procs, now, errors) for c in cands]
    summary = {v: {"count": 0, "bytes": 0} for v in VERDICTS}
    for it in items:
        s = summary[it["verdict"]]
        s["count"] += 1
        s["bytes"] += it["bytes"]
    return {
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
        "inventory_complete": bool(inventory.get("complete")),
        "items": items,
        "summary": summary,
        "errors": errors,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(probe_updaters(None), ensure_ascii=False, indent=1))
