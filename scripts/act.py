#!/usr/bin/env python3
"""复核并执行已封存的清理计划（契约第 8 节）。

用法：
  act.py run --run DIR --seal DIGEST --cards c01,c03 [--answers c02=删] [--sel SEL] [--dry-run | --pilot | --skip-pilot --reason 原因]
  act.py go --run DIR (--choice safe|recommended|all | --from-board 看板导出文本文件)
  act.py status --run DIR

只信 seal.json（digest 必须与 --seal 及内容重算一致）。删除一律 os.unlink / os.rmdir
逐个进行，不用 rm -rf，不用 shutil.rmtree；每个条目删除前再做一次保护检查，
只用 lstat，绝不跟随符号链接。native 命令不经过 shell。
"""
import argparse
import errno
import glob
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import plan  # noqa: E402
import protect  # noqa: E402
import codex_state  # noqa: E402
import units  # noqa: E402

RECENT_SECONDS = plan.RECENT_SECONDS
LSOF_TIMEOUT = 60
PGREP_TIMEOUT = 10
NATIVE_TIMEOUT = 1800
GIT_TIMEOUT = 120
LIST_CAP = 1000
SAMPLE = 20
DELETE_WORD = "删"
VACUUM_PAGES = 5000       # 每批回收的页数（4KB 页约 20MB），批间释放写锁，不长时间挡住正在运行的 Codex
VACUUM_MAX_BATCHES = 20000
VACUUM_FLAT_BATCHES = 20  # 主文件 + -wal 合计体积连续这么多批不降就停


# ----------------------------------------------------------------- 可被测试替换的小函数

def protected(path, profile, entry=False):
    """所有删除路径的唯一保护闸口：条目级用 entry=True（不跟随符号链接）。"""
    if entry:
        return plan.entry_protected(path, profile)
    return protect.protected_reason(path, profile)


def free_bytes():
    st = os.statvfs(protect.HOME)
    return st.f_bavail * st.f_frsize


def parse_lsof(text):
    return [line[1:] for line in text.splitlines() if line.startswith("n") and len(line) > 1]


def open_files(path):
    """返回正在打开 path 的文件名列表；None 表示无法确认。"""
    try:
        is_dir = stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return None
    cmd = ["lsof", "-n", "-P", "-F", "n", "+D", path] if is_dir else ["lsof", "-n", "-P", "-F", "n", "--", path]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=LSOF_TIMEOUT, shell=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if cp.returncode not in (0, 1):
        return None
    return parse_lsof(cp.stdout)


def process_running(name):
    """进程名为 name 的进程是否在运行（pgrep -x，精确匹配，argv 列表调用不经 shell）。
    True=在运行；False=没找到（pgrep 退出 1）；None=无法确认（超时、pgrep 不可用、其他退出码）。"""
    try:
        cp = subprocess.run(["pgrep", "-x", "--", name], capture_output=True, text=True,
                            timeout=PGREP_TIMEOUT, shell=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if cp.returncode == 0:
        return True
    if cp.returncode == 1:
        return False
    return None


def blocking_processes(item):
    """封存条目带的 blocked_by_processes（来自节点 producer）里，正在运行的 / 无法确认的进程名。
    返回 (running, unknown)。字段缺失=没有闸门；字段存在但格式不对按“无法确认”处理（保守）。"""
    names = item.get("blocked_by_processes")
    if names is None:
        return [], []
    if not isinstance(names, list) or not all(isinstance(n, str) and n.strip() for n in names):
        return [], ["blocked_by_processes 格式不对"]
    running, unknown = [], []
    for n in names:
        r = process_running(n)
        if r is True:
            running.append(n)
        elif r is None:
            unknown.append(n)
    return running, unknown


class GitUnknown(Exception):
    pass


class NotRepo(Exception):
    pass


def _git(cwd, args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C", LANG="C")
    try:
        cp = subprocess.run(["git", "-c", "core.quotepath=false", "-C", cwd] + args, capture_output=True,
                            timeout=GIT_TIMEOUT, env=env, shell=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise GitUnknown(f"git 无法运行：{exc}")
    if cp.returncode != 0:
        err = cp.stderr.decode("utf-8", "replace")
        if "not a git repository" in err:
            raise NotRepo(err.strip())
        raise GitUnknown(f"git {' '.join(args[:2])} 失败（{cp.returncode}）：{err.strip()[:200]}")
    return cp.stdout


def _split_z(raw):
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]


def git_base(real_path):
    """目录用自身作为 cwd；文件用其父目录。返回 (cwd, pathspec)。"""
    if os.path.isdir(real_path):
        return real_path, "."
    return os.path.dirname(real_path), os.path.basename(real_path)


def list_tracked(real_path):
    cwd, spec = git_base(real_path)
    return [os.path.join(cwd, p) for p in _split_z(_git(cwd, ["ls-files", "-z", "--", spec]))]


def list_ignored(real_path):
    cwd, spec = git_base(real_path)
    raw = _git(cwd, ["ls-files", "-o", "-i", "--exclude-standard", "-z", "--", spec])
    return [os.path.join(cwd, p) for p in _split_z(raw)]


# ----------------------------------------------------------------- 目录树与 keep

def walk_tree(root):
    """不跟随符号链接地枚举 root 下全部条目。返回 files[(path, st)], dirs, nested_git, errors。"""
    files, dirs, nested, errors = [], [], [], []
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode):
        return [(root, st)], dirs, nested, errors
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError as exc:
            errors.append((d, f"无法读取目录：{exc.strerror or exc}"))
            continue
        with it:
            for e in it:
                p = os.path.join(d, e.name)
                try:
                    est = e.stat(follow_symlinks=False)
                except OSError as exc:
                    errors.append((p, f"无法 lstat：{exc.strerror or exc}"))
                    continue
                if e.name == ".git":
                    nested.append(p)
                if stat.S_ISDIR(est.st_mode):
                    dirs.append(p)
                    stack.append(p)
                else:
                    files.append((p, est))
    return files, dirs, nested, errors


def is_kept(path, root, keep):
    if not keep:
        return False
    rel = os.path.relpath(path, root)
    comps = rel.split(os.sep)
    for k in keep:
        k = k.rstrip("/")
        if not k:
            continue
        if os.path.isabs(k):
            if path == k or path.startswith(k + "/"):
                return True
        elif "/" in k:
            if rel == k or rel.startswith(k + "/"):
                return True
        elif k in comps:
            return True
    return False


def alloc_bytes(st):
    if st.st_nlink > 1 and not stat.S_ISDIR(st.st_mode):
        return 0  # 硬链接：最后一个链接删除时才真正释放，这里不计
    return st.st_blocks * 512


class ItemResult:
    def __init__(self, index, item):
        self.d = {"index": index, "path": item.get("path"), "method": item.get("method"),
                  "scope": item.get("scope"), "status": None, "reason": None,
                  "removed": {"files": 0, "dirs": 0, "bytes": 0, "sample": []},
                  "skipped": [], "skipped_count": 0, "failed": [], "failed_count": 0,
                  "kept_count": 0, "sealed_bytes": item.get("bytes")}

    def skip_item(self, reason):
        self.d["status"], self.d["reason"] = "skipped", reason
        return self.d

    def add_skip(self, path, reason):
        self.d["skipped_count"] += 1
        if len(self.d["skipped"]) < LIST_CAP:
            self.d["skipped"].append({"path": path, "reason": reason})

    def add_fail(self, path, reason):
        self.d["failed_count"] += 1
        if len(self.d["failed"]) < LIST_CAP:
            self.d["failed"].append({"path": path, "error": reason})

    def finish(self):
        d = self.d
        removed = d["removed"]["files"] + d["removed"]["dirs"]
        if d["status"] is None:
            if d["failed_count"] and not removed:
                d["status"] = "failed"
            elif removed and (d["failed_count"] or d["skipped_count"]):
                d["status"] = "partial"
            elif removed:
                d["status"] = "removed"
            else:
                d["status"] = "skipped"
                d["reason"] = d["reason"] or "没有可删除的内容"
        return d


# ----------------------------------------------------------------- 单个条目

def verify_identity(item, profile):
    """路径仍是封存时那一个；返回 (真实路径, None) 或 (None, 跳过原因)。"""
    path = item["path"]
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, "路径已不存在"
    except OSError as exc:
        return None, f"无法读取路径：{exc.strerror or exc}"
    if stat.S_ISLNK(st.st_mode):
        return None, "路径现在是符号链接，已被替换"
    rp = os.path.realpath(path)
    if rp != item["realpath"]:
        return None, f"路径解析结果已变化（封存时 {item['realpath']}，现在 {rp}）"
    if (st.st_ino, st.st_dev) != (item["ino"], item["dev"]):
        return None, "路径已被替换（ino/dev 与封存时不一致）"
    reason = protected(rp, profile)
    if reason:
        return None, f"保护路径：{reason}"
    if not protect.under_roots(rp, profile):
        return None, "不在 profile.roots 之内"
    return rp, None


def collect_candidates(item, rp, profile, res):
    """按 scope 现场枚举要删除的文件与目录。返回 (files[(path,st)], dirs, root_dir_removable) 或 None（整条跳过）。"""
    scope = item["scope"]
    keep = item.get("keep") or []
    if scope == "files":
        try:
            tracked_set = {t.lower() for t in list_tracked(rp)}
        except NotRepo:
            tracked_set = set()
        except GitUnknown as exc:
            res.skip_item(f"无法确认跟踪状态：{exc}")
            return None
        files = []
        for f in item.get("files") or []:
            p = f["path"]
            if not plan.within(plan.canon_nofollow(p), rp):
                res.add_skip(p, "不在 item 路径之内")
                continue
            try:
                st = os.lstat(p)
            except OSError as exc:
                res.add_skip(p, f"文件不存在或无法读取（{exc.strerror or exc}）")
                continue
            if stat.S_ISDIR(st.st_mode) or (st.st_size, st.st_mtime_ns) != (f["size"], f["mtime_ns"]):
                res.add_skip(p, "size 或 mtime 与封存时不一致，已变化")
                continue
            if p.lower() in tracked_set:
                res.add_skip(p, "被 git 跟踪的文件永不删除")
                continue
            if is_kept(p, rp, keep):
                res.d["kept_count"] += 1
                continue
            pr = protected(p, profile, entry=True)
            if pr:
                res.add_skip(p, f"保护路径：{pr}")
                continue
            files.append((p, st))
        return files, [], False

    try:
        tracked = list_tracked(rp)
    except NotRepo:
        tracked = None
    except GitUnknown as exc:
        res.skip_item(f"无法确认跟踪状态：{exc}")
        return None

    if scope == "all":
        if tracked:
            res.skip_item(f"路径内含 {len(tracked)} 个被 git 跟踪的文件（如 {tracked[0]}），scope=all 拒绝；请改用 ignored-only")
            return None
        files, dirs, nested, errors = walk_tree(rp)
        if nested:
            res.skip_item(f"路径内含 git 仓库（{nested[0]}），scope=all 拒绝；请改用 ignored-only 或拆开处理")
            return None
        for p, why in errors:
            res.add_skip(p, why)
        out_files = []
        for p, st in files:
            if is_kept(p, rp, keep):
                res.d["kept_count"] += 1
                continue
            pr = protected(p, profile, entry=True)
            if pr:
                res.add_skip(p, f"保护路径：{pr}")
                continue
            out_files.append((p, st))
        out_dirs = []
        for d in dirs:
            if is_kept(d, rp, keep):
                continue
            pr = protected(d, profile, entry=True)
            if pr:
                res.add_skip(d, f"保护路径：{pr}")
                continue
            out_dirs.append(d)
        return out_files, out_dirs, os.path.isdir(rp)

    # ignored-only
    if tracked is None:
        res.skip_item("不在 git 仓库内，scope=ignored-only 不适用")
        return None
    try:
        ignored = list_ignored(rp)
    except NotRepo:
        res.skip_item("不在 git 仓库内，scope=ignored-only 不适用")
        return None
    except GitUnknown as exc:
        res.skip_item(f"无法列出被忽略的文件：{exc}")
        return None
    tracked_set = {t.lower() for t in tracked}
    files = []
    for p in ignored:
        if p.lower() in tracked_set:  # 跟踪文件永不删除（即便 git 列表里混进来）
            res.d["kept_count"] += 1
            continue
        if not plan.within(plan.canon_nofollow(p), rp):
            res.add_skip(p, "不在 item 路径之内")
            continue
        try:
            st = os.lstat(p)
        except OSError as exc:
            res.add_skip(p, f"无法读取（{exc.strerror or exc}）")
            continue
        if stat.S_ISDIR(st.st_mode):
            res.add_skip(p, "git 列表里出现目录，不处理")
            continue
        if is_kept(p, rp, keep):
            res.d["kept_count"] += 1
            continue
        pr = protected(p, profile, entry=True)
        if pr:
            res.add_skip(p, f"保护路径：{pr}")
            continue
        files.append((p, st))
    return files, [], False


def delete_files(files, res, parent_cache):
    for p, st0 in files:
        parent = os.path.dirname(p)
        expect = parent_cache.setdefault(parent, os.path.realpath(parent))
        if expect != parent:
            res.add_skip(p, "父目录解析路径已变化（疑似符号链接替换）")
            continue
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            res.add_skip(p, "删除前已消失")
            continue
        except OSError as exc:
            res.add_fail(p, f"lstat 失败：{exc.strerror or exc}")
            continue
        if (st.st_ino, st.st_dev) != (st0.st_ino, st0.st_dev):
            res.add_skip(p, "删除前被替换（ino 变化）")
            continue
        try:
            os.unlink(p)
        except OSError as exc:
            res.add_fail(p, f"unlink 失败：{exc.strerror or exc}")
            continue
        r = res.d["removed"]
        r["files"] += 1
        r["bytes"] += alloc_bytes(st0)
        if len(r["sample"]) < SAMPLE:
            r["sample"].append(p)


def delete_dirs(dirs, res, profile):
    for d in sorted(set(dirs), key=lambda x: (x.count("/"), x), reverse=True):
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            res.add_skip(d, "不再是目录，未处理")
            continue
        try:
            os.rmdir(d)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                continue  # 目录里还有保留内容，属预期，不算失败
            res.add_fail(d, f"rmdir 失败：{exc.strerror or exc}")
            continue
        res.d["removed"]["dirs"] += 1


def execute_item(index, item, profile, dry_run, now, run=None):
    """进程闸门（知识卡“运行中只报告不执行”）：producer 声明的 blocked_by_processes 任一在运行或无法确认，
    正式执行（含试点）直接 skipped；预演照常算清单，但在结果里标 blocked_by 和提示，不改变“不删任何东西”。"""
    running, unknown = blocking_processes(item)
    if running or unknown:
        if running:
            why = "、".join(running) + " 正在运行，按知识卡只报告不执行"
        else:
            why = "无法确认 " + "、".join(unknown) + " 是否在运行（pgrep 超时或出错），保守跳过"
        if not dry_run:
            return ItemResult(index, item).skip_item(why)
        d = _execute_item(index, item, profile, dry_run, now, run)
        d["blocked_by"] = running + unknown
        d["blocked_reason"] = why
        d["reason"] = f"{d['reason'] or ''}；正式执行时会跳过：{why}".lstrip("；")
        return d
    return _execute_item(index, item, profile, dry_run, now, run)


def _execute_item(index, item, profile, dry_run, now, run=None):
    res = ItemResult(index, item)
    if item.get("method") not in ("delete", "native") or (
            item["method"] == "delete" and item.get("scope") not in plan.SCOPES):
        return res.skip_item("封存条目的 method/scope 不合法")
    rp, why = verify_identity(item, profile)
    if why:
        return res.skip_item(why)

    if item["method"] == "native":
        return run_native(item, rp, res, dry_run, now, run)

    cand = collect_candidates(item, rp, profile, res)
    if cand is None:
        return res.d
    files, dirs, root_removable = cand

    recent = [p for p, st in files if st.st_mtime > now - RECENT_SECONDS]
    if recent:
        return res.skip_item(f"含 {len(recent)} 个 24 小时内修改过的文件（如 {recent[0]}），可能仍在使用")

    opened = open_files(rp)
    if opened is None:
        return res.skip_item("无法确认是否被打开（lsof 超时或不可用）")
    if opened:
        return res.skip_item(f"有进程正在打开其中的文件（如 {opened[0]}）")

    if dry_run:
        r = res.d["removed"]
        r["files"], r["bytes"] = len(files), sum(alloc_bytes(st) for _, st in files)
        r["sample"] = [p for p, _ in files[:SAMPLE]]
        res.d["status"] = "planned"
        res.d["reason"] = f"dry-run：将删除 {len(files)} 个文件"
        return res.finish()

    parent_cache = {}
    delete_files(files, res, parent_cache)
    if item["scope"] == "ignored-only":
        # 只清理因删除而变空的目录（根目录自身保留）
        touched = set()
        for p, _ in files:
            d = os.path.dirname(p)
            while len(d) > len(rp) and plan.within(d, rp):
                touched.add(d)
                d = os.path.dirname(d)
        delete_dirs([d for d in touched if not protected(d, profile, entry=True)], res, profile)
    elif item["scope"] == "all" and root_removable:
        delete_dirs(dirs + [rp], res, profile)
    elif item["scope"] == "all":
        delete_dirs(dirs, res, profile)
    return res.finish()


def native_cwd(run):
    """原生命令的工作目录：run 目录下 native-cwd/ 里为每条命令新建一个空子目录（cmd-*）。
    不用调用者的当前目录：里面的 .npmrc、uv.toml 之类会改变工具实际清理的位置；uv 等还会向上查找配置，
    所以 native-cwd/ 本身也只允许有之前命令留下的 cmd-* 子目录，出现别的东西就拒绝。
    run 为空（只会出现在直接调用的测试里）时用全新的临时目录。返回 (目录, 错误)。"""
    if not run:
        return tempfile.mkdtemp(prefix="sansheng-native-cwd-"), None
    base = os.path.join(run, "native-cwd")
    try:
        os.makedirs(base, exist_ok=True)
        if os.path.islink(base) or not os.path.isdir(base):
            return None, f"{base} 不是普通目录"
        stray = [n for n in os.listdir(base)
                 if not (n.startswith("cmd-") and os.path.isdir(os.path.join(base, n))
                         and not os.path.islink(os.path.join(base, n)))]
        if stray:
            return None, f"工作目录 {base} 里有不该有的东西（{len(stray)} 项，如 {stray[0]}），请人工查看后清空再重试"
        return tempfile.mkdtemp(prefix="cmd-", dir=base), None
    except OSError as exc:
        return None, f"无法准备工作目录 {base}：{exc.strerror or exc}"


def run_native(item, rp, res, dry_run, now, run=None):
    argv = item.get("argv")
    bad = plan._native_target_mismatch(argv, rp)     # 与 plan check 同一套形状表：防封存后被篡改或旧计划绕过
    if bad:
        return res.skip_item(f"native 命令与形状表不符：{bad}")
    shape = protect.native_match(argv)[0]
    # 程序路径：执行时按同一规则重新解析，必须与封存时一致；写进回执
    prog = protect.resolve_prog(argv[0])
    if "prog" not in item:
        return res.skip_item("封存条目缺少 prog（程序路径记录），旧封存或被改动的封存不执行原生命令")
    if item["prog"] != prog:
        return res.skip_item(f"程序路径与封存时不一致（封存 {item['prog']}，现在 {prog}），已跳过")
    res.d["prog"] = prog
    if shape.guard:
        gerr = shape.guard(protect.HOME)
        if gerr:
            return res.skip_item(f"执行环境检查未通过：{gerr}")
    tid = plan._codex_target(argv)
    if tid:
        # codex delete 会连带删除全部后代：任一后代还在（被跳过、失败或封存后新出现），父会话就不删
        rel = codex_state.relations(protect.HOME)
        alive = [d for d in codex_state.descendants(tid, rel["children"])
                 if rel["rollout"].get(d) and os.path.lexists(rel["rollout"][d])]
        if alive:
            return res.skip_item(f"子会话 {alive[0]} 等 {len(alive)} 个仍在，删除父会话会连带删除它们，已跳过")
    files, _, _, errors = walk_tree(rp)
    recent = [p for p, st in files if st.st_mtime > now - RECENT_SECONDS]
    if recent:
        return res.skip_item(f"含 {len(recent)} 个 24 小时内修改过的文件（如 {recent[0]}），可能仍在使用")
    opened = open_files(rp)
    if opened is None:
        return res.skip_item("无法确认是否被打开（lsof 超时或不可用）")
    if opened:
        return res.skip_item(f"有进程正在打开其中的文件（如 {opened[0]}）")
    before_bytes = sum(alloc_bytes(st) for _, st in files)
    if dry_run:
        res.d["status"] = "planned"
        res.d["reason"] = "dry-run：将执行 " + " ".join(argv)
        res.d["argv"] = list(argv)
        res.d["env_forced"] = shape.forced_env(protect.HOME)
        return res.d
    res.d["argv"] = list(argv)
    cwd, cwd_err = native_cwd(run)
    if cwd_err:
        return res.skip_item(cwd_err)
    env = protect.native_env(shape, prog)
    res.d["cwd"] = cwd
    res.d["env"] = env
    try:
        cp = subprocess.run(argv, shell=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=NATIVE_TIMEOUT, cwd=cwd, env=env)
    except FileNotFoundError:
        res.add_fail(rp, f"命令不存在：{argv[0]}")
        return res.finish()
    except subprocess.TimeoutExpired:
        res.add_fail(rp, f"命令超过 {NATIVE_TIMEOUT} 秒仍未结束")
        return res.finish()
    except OSError as exc:
        res.add_fail(rp, f"命令无法启动：{exc}")
        return res.finish()
    try:
        leftovers = os.listdir(cwd)
    except OSError:
        leftovers = []
    if leftovers:
        res.d["cwd_leftovers"] = leftovers[:20]      # 工具往工作目录写了东西；下一条原生命令会因目录非空而跳过
    res.d["returncode"] = cp.returncode
    res.d["output_tail"] = ((cp.stdout or "") + (cp.stderr or ""))[-500:]
    if cp.returncode != 0:
        res.add_fail(rp, f"命令退出码 {cp.returncode}")
        return res.finish()
    # 退出码 0 不等于删掉了：现场核实，按前后实际占用计字节
    left = sum(alloc_bytes(st) for _, st in walk_tree(rp)[0]) if os.path.lexists(rp) else 0
    if tid and os.path.lexists(rp):
        res.add_fail(rp, "codex delete 返回成功，但会话文件仍在（可能只删了数据库记录）")
        return res.finish()
    res.d["removed"]["bytes"] = max(0, before_bytes - left)
    res.d["status"] = "removed" if before_bytes - left > 0 or not os.path.lexists(rp) else "skipped"
    res.d["reason"] = ("native 命令执行成功" if res.d["status"] == "removed"
                       else "native 命令返回成功，但路径下的占用没有变化")
    return res.d


# ----------------------------------------------------------------- 主流程

def parse_answers(parts):
    text = " ".join(parts or [])
    return {m.group(1): m.group(2) for m in re.finditer(r"([A-Za-z0-9_\-]+)\s*=\s*([^\s,;/，；、]+)", text)}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_seal(run, digest):
    try:
        seal = plan.read_json(os.path.join(run, "seal.json"))
    except ValueError as exc:
        return None, str(exc)
    if not isinstance(seal, dict) or seal.get("schema") != "seal/1":
        return None, "seal.json 格式不对"
    recomputed = plan.digest_of(seal)
    if seal.get("digest") != recomputed:
        return None, f"seal.json 内容与其 digest 不符（记录 {seal.get('digest')}，重算 {recomputed}），文件可能被改动"
    if digest != recomputed:
        return None, f"--seal {digest} 与封存 digest {recomputed} 不一致"
    return seal, None


def select_cards(seal, requested, answers):
    """返回 [(card_id, 是否执行, 原因)]。decide 卡只在答复「删」时执行（即使没出现在 --cards 里）。"""
    order = list(requested)
    for cid, ans in answers.items():
        if ans == DELETE_WORD and cid not in order:
            order.append(cid)
    out = []
    for cid in order:
        card = seal["cards"].get(cid)
        ans = answers.get(cid)
        if card is None:
            out.append((cid, False, "不在封存内（未通过校验、被降级或编号错误）"))
        elif card["verdict"] == "decide":
            if ans == DELETE_WORD:
                out.append((cid, True, None))
            elif ans is None:
                out.append((cid, False, "decide 卡片没有收到答复，不执行"))
            else:
                out.append((cid, False, f"答复为「{ans}」，不执行"))
        else:
            if ans is not None and ans != DELETE_WORD:
                out.append((cid, False, f"答复为「{ans}」，不执行"))
            else:
                out.append((cid, True, None))
    return out


def codex_deleted(receipt):
    return any(it.get("argv", [""])[0] == "codex" and it["status"] == "removed"
               for c in receipt["cards"] for it in c["items"])


def _db_size(db):
    total = 0
    for suffix in ("", "-wal"):
        try:
            total += os.path.getsize(db + suffix)
        except OSError:
            pass
    return total


def reclaim_codex_history(home=None):
    """codex delete 只删除历史库里的行，文件不会变小。库开着 auto_vacuum=incremental 时，
    分批执行 incremental_vacuum 把空页交还磁盘；否则只报告，不做整库 VACUUM（要求 Codex 退出）。

    WAL 模式下搬动的页先写进 -wal，主文件要等 checkpoint 才变小，所以每批之后做一次 PASSIVE
    checkpoint，并按"主文件 + -wal"合计体积判断进展；合计体积连续几批不降、或空闲页不降就停。
    每批页数小（约 20MB），批间暂停，尽量不挡住正在运行的 Codex 的写入。"""
    import sqlite3
    base = os.path.join(home or protect.HOME, ".codex")
    out = []
    for db in sorted(glob.glob(os.path.join(base, "thread_history_*.sqlite"))):
        rec = {"db": db, "size_before": _db_size(db), "batches": 0, "error": None}
        try:
            con = sqlite3.connect(db, timeout=30)
            mode = con.execute("PRAGMA auto_vacuum").fetchone()[0]
            rec["auto_vacuum"] = mode
            rec["journal_mode"] = con.execute("PRAGMA journal_mode").fetchone()[0]
            wal = rec["journal_mode"].lower() == "wal"
            if mode != 2:
                rec["error"] = "库未开启 auto_vacuum=incremental，需 Codex 完全退出后手动 VACUUM，未自动执行"
            else:
                free = con.execute("PRAGMA freelist_count").fetchone()[0]
                best, flat = _db_size(db), 0
                while free and rec["batches"] < VACUUM_MAX_BATCHES:
                    t0 = time.monotonic()
                    con.execute(f"PRAGMA incremental_vacuum({VACUUM_PAGES})").fetchall()
                    if wal:
                        con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
                    rec["batches"] += 1
                    now_free = con.execute("PRAGMA freelist_count").fetchone()[0]
                    if now_free >= free:
                        rec["error"] = f"回收停滞（空闲页仍有 {now_free}），已停止；可稍后重跑 act.py reclaim"
                        break
                    free = now_free
                    size = _db_size(db)
                    if size < best:
                        best, flat = size, 0
                    else:
                        flat += 1
                        if flat >= VACUUM_FLAT_BATCHES:
                            rec["error"] = ("主文件没有随回收变小（多半是有读事务挡住了 checkpoint），已停止；"
                                            "Codex 空闲时重跑 act.py reclaim")
                            break
                    time.sleep(max(0.2, 2 * (time.monotonic() - t0)))
                if wal:
                    try:
                        con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                    except sqlite3.Error:
                        pass
                rec["integrity"] = con.execute("PRAGMA quick_check").fetchone()[0]
            con.close()
        except sqlite3.Error as exc:
            rec["error"] = f"回收中断：{exc}（可稍后重跑 act.py reclaim）"
        rec["size_after"] = _db_size(db)
        rec["reclaimed"] = rec["size_before"] - rec["size_after"]
        out.append(rec)
    return out


def pilot_order(seal, selection):
    """试点的尝试顺序：[(卡号, 条目下标)]。decide 卡的条目优先，再按封存字节从大到小（未知按 0），同级保持原顺序。
    不用 --cards 顺序第一条：顺序不进 sel，调用方可借此把最小最无害的条目排到最前面。"""
    cand = []
    for pos, (cid, do, _) in enumerate(selection):
        if not do:
            continue
        card = seal["cards"][cid]
        for idx, it in enumerate(card["items"]):
            try:
                size = int(it.get("bytes") or 0)
            except (TypeError, ValueError):
                size = 0
            cand.append((0 if card["verdict"] == "decide" else 1, -size, pos, idx, cid))
    cand.sort()
    return [(cid, idx) for _, _, _, idx, cid in cand]


def run_plan(run, seal, selection, dry_run, pilot=False):
    profile = plan.load_profile()
    now = time.time()
    before = free_bytes()
    receipt = {"schema": "receipt/1", "scan_id": seal["scan_id"], "seal": seal["digest"],
               "started_at": now_iso(), "dry_run": dry_run, "cards": [],
               "mode": "dry_run" if dry_run else ("pilot" if pilot else "full")}
    pilot_results = {}
    if pilot:
        # 试点：全局按 pilot_order 逐条尝试，直到有一条真正执行（removed/partial/failed）；其余条目不执行也不记录。
        # 试点证明执行路径可用，不证明每条都安全
        for cid, idx in pilot_order(seal, selection):
            it = seal["cards"][cid]["items"][idx]
            r = execute_item(it.get("index", idx), it, profile, dry_run, now, run)
            pilot_results.setdefault(cid, []).append(r)
            if r["status"] in ("removed", "partial", "failed"):
                receipt["pilot_done"] = True
                break
    for cid, do, why in selection:
        if not do:
            receipt["cards"].append({"id": cid, "status": "skipped", "reason": why, "items": []})
            continue
        card = seal["cards"][cid]
        if pilot:
            items = pilot_results.get(cid, [])
        else:
            items = [execute_item(it.get("index", i), it, profile, dry_run, now, run)
                     for i, it in enumerate(card["items"])]
        entry = {"id": cid, "title": card.get("title"), "verdict": card["verdict"],
                 "status": "planned" if dry_run else ("pilot" if pilot else "executed"), "items": items}
        if pilot and not items:
            entry["status"] = "skipped"
            entry["reason"] = "试点模式只执行一条"
        receipt["cards"].append(entry)
    if pilot:
        receipt["pilot"] = True
    if not dry_run and codex_deleted(receipt):
        receipt["history_reclaim"] = reclaim_codex_history()
    after = free_bytes()
    summary = {"removed_files": 0, "removed_dirs": 0, "skipped_items": 0, "skipped_entries": 0,
               "failed_items": 0, "failed_entries": 0, "removed_items": 0, "bytes_by_files": 0,
               "bytes_native": 0}
    for c in receipt["cards"]:
        for it in c["items"]:
            summary["removed_files"] += it["removed"]["files"]
            summary["removed_dirs"] += it["removed"]["dirs"]
            summary["bytes_by_files"] += it["removed"]["bytes"]
            summary["skipped_entries"] += it["skipped_count"]
            summary["failed_entries"] += it["failed_count"]
            if it["status"] in ("removed", "partial"):
                summary["removed_items"] += 1
                if it["method"] == "native":
                    summary["bytes_native"] += it["removed"]["bytes"]
            elif it["status"] == "skipped":
                summary["skipped_items"] += 1
            elif it["status"] == "failed":
                summary["failed_items"] += 1
    measured = after - before
    reclaimed = sum(max(0, r.get("reclaimed", 0)) for r in receipt.get("history_reclaim", []))
    # bytes_by_files 已包含原生命令条目的实测字节（removed.bytes），bytes_native 只是其中的原生命令部分
    summary.update({"free_before": before, "free_after": after, "freed_measured": measured,
                    "history_reclaimed": reclaimed,
                    "freed_by_items": summary["bytes_by_files"] + reclaimed})
    if not dry_run:
        diff = measured - summary["freed_by_items"]
        summary["difference"] = diff
        summary["difference_note"] = (
            "按条目核算与磁盘余量变化一致" if abs(diff) < 64 * 1024 * 1024 else
            "按条目核算与磁盘余量变化不一致；余量会被同时运行的其他程序（构建、测试、下载）抵消，"
            "也可能是 APFS 克隆、本地快照或废纸篓。以按条目核算为准，必要时在其他写入停止后复测")
    receipt["summary"] = summary
    receipt["finished_at"] = now_iso()
    return receipt


fmt_bytes = units.fmt_bytes      # 十进制 GB（与成果页、访达一致），见 units.py


def print_receipt(rc):
    s = rc["summary"]
    tag = "【dry-run，未改动任何文件】" if rc["dry_run"] else ""
    print(f"清理{'预演' if rc['dry_run'] else '执行'}结果 {tag}scan_id={rc['scan_id']} seal={rc['seal']}")
    for c in rc["cards"]:
        print(f"  卡片 {c['id']}：{c['status']}" + (f"（{c['reason']}）" if c.get("reason") else ""))
        for it in c["items"]:
            line = f"    - [{it['status']}] {it['path']}"
            if it["reason"]:
                line += f"：{it['reason']}"
            print(line)
            if it.get("blocked_by"):
                print(f"        ！进程闸门：{'、'.join(it['blocked_by'])}（{it.get('blocked_reason')}）")
            r = it["removed"]
            if r["files"] or r["dirs"]:
                print(f"        文件 {r['files']}、目录 {r['dirs']}、{fmt_bytes(r['bytes'])}")
            for f in it["failed"][:5]:
                print(f"        失败 {f['path']}：{f['error']}")
    print(f"合计：删除文件 {s['removed_files']}、目录 {s['removed_dirs']}，跳过条目 {s['skipped_entries']}，"
          f"失败条目 {s['failed_entries']}；按文件 {fmt_bytes(s['bytes_by_files'])}", end="")
    if rc["dry_run"]:
        print("（预计）")
    else:
        print()
        for r in rc.get("history_reclaim", []):
            print(f"  历史库回收 {r['db']}：{fmt_bytes(r['size_before'])} → {fmt_bytes(r['size_after'])}"
                  + (f"；{r['error']}" if r.get("error") else f"，完整性 {r.get('integrity')}"))
        print(f"按条目核算释放 {fmt_bytes(s['freed_by_items'])}（删除的文件 {fmt_bytes(s['bytes_by_files'])}，"
              f"其中原生命令 {fmt_bytes(s['bytes_native'])}；历史库回收 {fmt_bytes(s['history_reclaimed'])}）；"
              f"磁盘余量变化 {fmt_bytes(s['freed_measured'])}")
        if s.get("difference") and abs(s["difference"]) >= 64 * 1024 * 1024:
            print(f"  差异 {fmt_bytes(s['difference'])}：{s['difference_note']}")
        if rc.get("pilot"):
            print("试点完成：请核对这一条确实已删除、相关程序没有报错，再去掉 --pilot 正式执行（已删的会自动跳过）")


def pilot_succeeded(rc):
    """试点成功 = 试点真的执行了一条（pilot_done），且按卡片顺序第一条被执行的条目状态为 removed、没有失败的文件。
    只有 skipped（没有可执行内容）、partial（部分失败）、failed 都不算：试点没有证明这条路径走得通。"""
    if not rc.get("pilot_done"):
        return False
    for c in rc.get("cards", []):
        for it in c.get("items", []):
            if it.get("status") in ("removed", "partial", "failed"):
                return it["status"] == "removed" and not it.get("failed_count")
    return False


def _removed_claim_holds(it, seal_item):
    """试点回执里记为 removed 的条目，现在是否真的不在了。回执是 run 目录里的普通 JSON，手写一份很容易，
    所以交叉核对现状：条目必须是封存里的条目（同路径）；路径已不存在才认；路径仍在时（scope=files、原生缓存清理）
    要求回执的删除样本全不存在，没有样本的原生命令则要求路径现在的占用小于封存时的字节数。"""
    if not isinstance(it, dict) or not isinstance(seal_item, dict) or it.get("path") != seal_item.get("path"):
        return False
    path = it["path"]
    if not isinstance(path, str) or not path:
        return False
    if not os.path.lexists(path):
        return True
    sample = (it.get("removed") or {}).get("sample")
    if isinstance(sample, list) and sample and all(isinstance(x, str) for x in sample):
        return not any(os.path.lexists(x) for x in sample)
    sealed = seal_item.get("bytes")
    if it.get("method") == "native" and isinstance(sealed, int) and sealed > 0:
        try:
            now_bytes = sum(alloc_bytes(st) for _, st in walk_tree(path)[0])
        except OSError:
            return False
        return now_bytes < sealed
    return False


def pilot_receipt_verified(rc, seal):
    """被执行的那一条（第一条 removed/partial/failed）在封存的对应卡片里，且删除现场核对属实。"""
    for c in rc.get("cards", []):
        card = seal["cards"].get(c.get("id")) if isinstance(c, dict) else None
        for it in (c.get("items") or []) if isinstance(c, dict) else []:
            if not isinstance(it, dict) or it.get("status") not in ("removed", "partial", "failed"):
                continue
            if card is None:
                return False
            seal_item = next((x for x in card.get("items", []) if x.get("path") == it.get("path")), None)
            return _removed_claim_holds(it, seal_item)
    return False


def find_gate(run, seal, sel, card_ids=None):
    """在 run 目录里找与本 seal、本 sel 对应的预演回执与成功的试点回执。返回 (预演文件名或 None, 试点文件名或 None)。
    只认回执里的 seal/scan_id/sel/mode 字段（没有 sel 的旧回执一律不算），文件名不参与判断。
    card_ids 给出时，回执里的卡片集合必须与本次选择一致；试点回执里删掉的那一条还要现场核对确实不在了。
    不加签名/随机数：回执与脚本在同一用户、同一目录下，随机数同样读得到，防的只是失误，不是蓄意伪造。"""
    found = {"dry_run": None, "pilot": None}
    pattern = {"dry_run": "receipt-dryrun-*.json", "pilot": "receipt-pilot-*.json"}
    for mode, pat in pattern.items():
        for path in sorted(glob.glob(os.path.join(run, pat))):
            try:
                rc = plan.read_json(path)
            except ValueError:
                continue
            if not isinstance(rc, dict) or rc.get("schema") != "receipt/1" or rc.get("mode") != mode:
                continue
            if rc.get("seal") != seal["digest"] or rc.get("scan_id") != seal["scan_id"] or rc.get("sel") != sel:
                continue
            if card_ids is not None:
                ids = sorted(c.get("id") for c in rc.get("cards", []) if isinstance(c, dict))
                if ids != sorted(card_ids):
                    continue
            if mode == "pilot" and (rc.get("dry_run") or not pilot_succeeded(rc) or not pilot_receipt_verified(rc, seal)):
                continue
            found[mode] = os.path.basename(path)
    return found["dry_run"], found["pilot"]


def gate_command(args, sel, flag):
    cmd = ["python3", "scripts/act.py", "run", "--run", args.run, "--seal", args.seal, "--cards", args.cards or ""]
    if args.answers:
        cmd += ["--answers", *args.answers]
    cmd += ["--sel", sel, flag]
    return " ".join(shlex.quote(c) for c in cmd)


def record_choices(run, seal, chosen):
    """飞轮：正式执行完，按 cardkeys.json（propose / board 写出）记下这次每一类删了没有。记录失败不影响清理结果。"""
    try:
        keys = plan.read_json(os.path.join(run, "cardkeys.json"))
    except (OSError, ValueError):
        return
    offered = [{"id": cid, **v} for cid, v in keys.items() if isinstance(v, dict) and cid in (seal.get("cards") or {})]
    try:
        plan.prefs.record_choices(seal.get("scan_id"), offered, set(chosen))
    except OSError as exc:
        print(f"提示：选择记录没写进去（{exc}），不影响这次清理", file=sys.stderr)

def finish_report(run, tag=""):
    """正式执行结束后：生成成果页并打印路径，再按需提示一次“有认不出来源的大目录，可以反馈”。
    go 与 run（看板路径）共用；成果页或提示出任何问题都不影响已完成的清理，提示只打印、不自动做任何事。"""
    try:
        import report
        path, rp = report.make(run)
        print(f"{tag}成果页：{path}（腾出 {fmt_bytes(rp['freed'])}）", flush=True)
    except (OSError, ValueError, KeyError) as exc:      # 成果页失败不影响已完成的清理
        print(f"{tag}成果页没生成：{exc}；可以稍后运行 python3 scripts/report.py --run {run}", file=sys.stderr)
    try:
        import feedback
        line = feedback.hint_line(run)
    except Exception:  # noqa: BLE001 - 提示是锦上添花
        line = None
    if line:
        print(f"{tag}{line}", flush=True)


def cmd_run(args):
    if args.skip_pilot and not (args.reason or "").strip():
        print("拒绝执行：--skip-pilot 必须同时给出非空的 --reason \"<原因>\"（原因会写进回执）", file=sys.stderr)
        return 2
    if args.reason is not None and not args.skip_pilot:
        print("拒绝执行：--reason 只能与 --skip-pilot 一起使用", file=sys.stderr)
        return 2
    if args.skip_pilot and (args.dry_run or args.pilot):
        print("拒绝执行：--skip-pilot 只用于正式执行，不能与 --dry-run 或 --pilot 同用", file=sys.stderr)
        return 2
    bad_home = protect.home_problem()
    if bad_home:
        print(f"拒绝执行：{bad_home}", file=sys.stderr)
        return 2
    run = os.path.realpath(args.run)
    seal, err = load_seal(run, args.seal)
    if err:
        print(f"拒绝执行：{err}", file=sys.stderr)
        return 2
    stale = protect.profile_guard_problem(seal, plan.load_profile())
    if stale:
        print(f"拒绝执行：{stale}", file=sys.stderr)
        return 2
    requested = [c.strip() for c in (args.cards or "").split(",") if c.strip()]
    answers = parse_answers(args.answers)
    selection = select_cards(seal, requested, answers)
    sel = plan.selection_sig(seal["digest"], [cid for cid, _, _ in selection], answers)
    if args.sel is None and not args.dry_run:
        # --sel 是「选择来自看板」的唯一机器核对：试点与正式执行必须带，只有预演（用户还没选时自己核对）可以不带
        print("拒绝执行：试点与正式执行必须带 --sel（看板导出文本里的 sel: 值），什么都没有删除。"
              "只有 --dry-run 预演可以不带", file=sys.stderr)
        return 2
    if args.sel is not None:
        # 看板导出的 sel: 行 = 卡号集合（--cards 并上答复「删」的卡）+ 全部答复 + seal 的指纹；对不上就一张都不动
        if args.sel.strip().lower() != sel:
            print(f"拒绝执行：--sel {args.sel} 与本次选择（卡片 {','.join(sorted(c for c, _, _ in selection)) or '无'}；"
                  f"答复 {','.join(sorted(f'{k}={v}' for k, v in answers.items())) or '无'}）算出的 {sel} 不一致。"
                  "请核对 --cards/--answers 是否与看板导出的文本完全一致", file=sys.stderr)
            return 2
    else:
        print("提示：未提供 --sel，本次预演没有与看板导出文本核对；试点与正式执行必须带 --sel", file=sys.stderr)
    if not selection:
        print("没有选中任何卡片（--cards 为空且没有「删」的答复）", file=sys.stderr)
        return 2
    if args.dry_run and args.pilot:
        print("--dry-run 与 --pilot 不能同时使用", file=sys.stderr)
        return 2
    gate = None
    if not args.dry_run and not args.pilot:
        # 正式执行闸门：同一 seal、同一 sel 的预演回执 + 成功的试点回执（或带原因跳过试点）都在，才放行
        dry_rc, pilot_rc = find_gate(run, seal, sel, [c for c, _, _ in selection])
        missing = []
        if not dry_rc:
            missing.append(("预演", "--dry-run"))
        if not pilot_rc and not args.skip_pilot:
            missing.append(("试点（且试点那一条必须成功删除）", "--pilot"))
        if missing:
            print(f"拒绝正式执行：没有找到与本次选择（seal={seal['digest']}，sel={sel}）对应的 "
                  + "、".join(m[0] for m in missing) + "回执，什么都没有删除。先依次运行：", file=sys.stderr)
            for _, flag in missing:
                print("  " + gate_command(args, sel, flag), file=sys.stderr)
            if not pilot_rc:
                print("（确需跳过试点：加 --skip-pilot --reason \"<原因>\"；预演不能跳过。更换卡片或答复后 sel 会变，需要重新预演和试点）",
                      file=sys.stderr)
            return 2
        gate = {"dry_run_receipt": dry_rc, "pilot_receipt": pilot_rc, "skip_pilot": bool(args.skip_pilot)}
        if args.skip_pilot:
            gate["skip_reason"] = args.reason.strip()
    receipt = run_plan(run, seal, selection, args.dry_run, pilot=args.pilot)
    receipt["sel"] = sel
    receipt["cards_requested"] = requested
    receipt["answers"] = answers
    if gate:
        receipt["gate"] = gate
        if gate["skip_pilot"]:
            receipt["skip_pilot"] = True
            receipt["skip_reason"] = gate["skip_reason"]
    print_receipt(receipt)
    kind = "dryrun-" if args.dry_run else ("pilot-" if args.pilot else "")
    when = datetime.now(timezone.utc)
    path = os.path.join(run, f"receipt-{kind}{when.strftime('%Y%m%dT%H%M%SZ')}.json")
    while os.path.exists(path):       # 同一秒内的两次执行不互相覆盖（成果页要把同一 seal 下的全部回执加起来）；顺延一秒，文件名仍按时间排序
        when += timedelta(seconds=1)
        path = os.path.join(run, f"receipt-{kind}{when.strftime('%Y%m%dT%H%M%SZ')}.json")
    plan.write_json(path, receipt)
    if not args.dry_run and not args.pilot:
        path = os.path.join(run, "receipt.json")
        plan.write_json(path, receipt)
        record_choices(run, seal, [cid for cid, do, _ in selection if do])
    print(f"回执：{path}")
    if not args.dry_run and not args.pilot and not getattr(args, "no_report", False):
        finish_report(run)          # 看板路径（手动 run）正式执行后同样自动出成果页；go 自己在全部步骤结束后生成，传 no_report 避免重复
    return 0


def _latest(run, pattern):
    paths = sorted(glob.glob(os.path.join(run, pattern)))
    return plan.read_json(paths[-1]) if paths else None


def cmd_go(args):
    """按 proposal.json 里用户回复的那一档，连着跑 预演 → 试点 → 正式执行；任何一步有异常就停下，什么都不往下做。
    用户在对话里回的那一句（如“好”）就是本轮的明确选择；卡号、答复、sel 一律从提案读，Agent 不转抄。
    --from-board <导出文本文件>：用户从看板复制回来的清单就是本轮的明确选择，见 cmd_go_from_board。"""
    if getattr(args, "from_board", None):
        if args.choice:
            print("拒绝执行：--choice 与 --from-board 只能给一个", file=sys.stderr)
            return 2
        return cmd_go_from_board(args)
    if not args.choice:
        print("拒绝执行：go 需要 --choice（对话确认）或 --from-board <看板导出文本文件>", file=sys.stderr)
        return 2
    run = os.path.realpath(args.run)
    try:
        proposal = plan.read_json(os.path.join(run, "proposal.json"))
    except (OSError, ValueError):
        print("拒绝执行：run 目录里没有 proposal.json，先运行 plan.py propose", file=sys.stderr)
        return 2
    # --choice 只用于对话确认（mode=chat）：发看板的提案（要用户看的项多、含交互对话、第一次使用核对习惯）
    # 必须让用户在看板里逐项确认，一句“好”不能代替；第一次使用阶段（onboarding）还没有核对过使用习惯，同样不收
    if proposal.get("mode") != "chat" or proposal.get("stage") == "onboarding":
        why = "；".join(proposal.get("board_reasons") or []) or "第一次使用，要先核对使用习惯"
        print(f"拒绝执行：这份提案不是对话确认方式（{why}），不能用 --choice；"
              "请发看板，用户确认后把导出清单交给 go --from-board", file=sys.stderr)
        return 2
    opt = (proposal.get("options") or {}).get(args.choice)
    if not opt or not opt.get("cards"):
        print(f"拒绝执行：提案里没有“{args.choice}”这一档，或这一档是空的", file=sys.stderr)
        return 2
    base = dict(run=run, seal=proposal.get("seal") or "", cards=",".join(opt["cards"]), answers=list(opt.get("answers") or []),
                sel=opt.get("sel"), skip_pilot=False, reason=None, no_report=True)
    return _go_steps(run, base)


def _go_steps(run, base):
    """预演 → 试点 → 正式执行 → 成果页。对话确认（--choice）与看板清单（--from-board）共用，闸门一步不少：
    每一步都是 cmd_run，sel、profile_guard、预演与试点回执的核对都在 cmd_run 里。"""
    steps = (("预演", dict(dry_run=True, pilot=False)), ("试点", dict(dry_run=False, pilot=True)),
             ("正式执行", dict(dry_run=False, pilot=False)))
    for name, flags in steps:
        print(f"【go】{name}开始", flush=True)
        rc = cmd_run(argparse.Namespace(**base, **flags))
        if rc != 0:
            print(f"【go】{name}被拒绝（退出码 {rc}），后面的步骤没有执行", file=sys.stderr)
            return rc
        if name == "预演":
            r = _latest(run, "receipt-dryrun-*.json") or {}
            s = r.get("summary") or {}
            if s.get("failed_items") or s.get("failed_entries"):
                print(f"【go】预演有 {s.get('failed_items', 0)} 条失败，停下来请用户看，什么都没有删除", file=sys.stderr)
                return 3
            if not any(it.get("status") == "planned" or (it.get("removed") or {}).get("files")
                       for c in r.get("cards", []) for it in c.get("items", [])):
                print("【go】预演里没有任何可执行条目（可能都被现场复核跳过），停下", file=sys.stderr)
                return 3
        if name == "试点":
            r = _latest(run, "receipt-pilot-*.json") or {}
            if not pilot_succeeded(r):
                print("【go】试点那一条没有成功删除，停下来请用户看；正式执行没有开始", file=sys.stderr)
                return 3
    try:
        s = plan.read_json(os.path.join(run, "receipt.json")).get("summary") or {}
    except ValueError:
        s = {}
    failed = (s.get("failed_items") or 0) + (s.get("failed_entries") or 0)
    if failed:
        print(f"【go】正式执行有 {s.get('failed_items', 0)} 条没删成功，其余已完成；告诉用户哪几条失败及原因"
              f"（python3 scripts/act.py status --run {run}）", file=sys.stderr)
    else:
        print("【go】完成", flush=True)
    finish_report(run, "【go】")
    return 4 if failed else 0


def _seal_drift(old, new):
    """比较重新封存前后的两份封存：返回 (判断变了的卡号, 只有条目现场数据变了的卡号)。
    “判断”= 有没有这张卡、判定、档位、规则键、是否需逐个确认、项目归属、条目路径与执行方式；
    其余（实测体积、文件大小、修改时间等封存时现量的数据）变了，说明目录内容在封存之后又变了。"""
    def judged(c):
        keys = ("verdict", "preset", "preset_raw", "rule_key", "review", "interactive", "projects", "prefill")
        items = [(i.get("path"), i.get("method"), i.get("scope"), i.get("ino"), i.get("dev")) for i in c.get("items") or []
                 if isinstance(i, dict)]
        return [c.get(k) for k in keys], items
    oc, nc = old.get("cards") or {}, new.get("cards") or {}
    changed, drift = [], []
    for cid in sorted(set(oc) | set(nc)):
        a, b = oc.get(cid), nc.get(cid)
        if a is None or b is None or judged(a) != judged(b):
            changed.append(cid)
        elif a != b:
            drift.append(cid)
    return changed, drift


def _board_stop(msg, code=2):
    print(f"【go】{msg}", file=sys.stderr, flush=True)
    return code


def cmd_go_from_board(args):
    """看板路径一条命令：读导出文本 → 校验 → 保存设置/台账 → 必要时重新封存 → 预演 → 试点 → 正式执行 → 成果页。
    退出码：2 = 拒绝（清单不合格、台账导出、scan_id/seal/sel 对不上，什么都没删）；3 = 停下请用户看（设置/台账让判断变了需要
    用新看板重新确认、设置没保存全、重新封存失败、预演或试点异常）；4 = 正式执行有条目没删成功。
    sel 只来自清单：清单里的卡号或答复被改过，cmd_run 重算 sel 对不上就拒绝，本函数不重新生成 sel。"""
    run = os.path.realpath(args.run)
    try:
        with open(args.from_board, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        return _board_stop(f"读不了清单文件 {args.from_board}：{exc}")
    if plan.prefs.is_ledger_export(text):
        return _board_stop("拒绝：这是项目台账（抬头“叁笙清理 · 项目台账”），不是清理清单，不含任何清理授权，什么都没有删除。"
                           "请用 python3 scripts/plan.py profile --apply <文件> 保存台账，再让用户从清理看板复制清单")
    ex, errs = plan.prefs.parse_board_export(text)
    if errs:
        return _board_stop("拒绝：清单格式不对（" + "；".join(errs) + "），什么都没有删除")
    settings, hide, serrs = plan.prefs.parse_settings(text)
    ledger, lerrs = plan.prefs.parse_ledger(text)
    if serrs or lerrs:
        return _board_stop("拒绝：清单里的设置/台账行不合法（" + "；".join(serrs + lerrs) + "），什么都没有保存、没有删除")
    seal, err = load_seal(run, ex["seal"])
    if err:
        return _board_stop(f"拒绝：{err}。清单可能来自别的扫描或旧看板；什么都没有删除")
    if ex["scan_id"] != seal.get("scan_id"):
        return _board_stop(f"拒绝：清单的 scan_id {ex['scan_id']} 与这次扫描 {seal.get('scan_id')} 不一致，什么都没有删除")
    answers = parse_answers(ex["answers"])
    selection = select_cards(seal, ex["cards"], answers)
    want = plan.selection_sig(seal["digest"], [cid for cid, _, _ in selection], answers)
    if want != ex["sel"]:       # 先于保存设置核对：对不上的清单不改动本机设置
        return _board_stop(f"拒绝：清单的 sel {ex['sel']} 与按清单里的卡片和回答算出的 {want} 不一致（清单被改过或转抄出错），"
                           "什么都没有保存、没有删除")
    if not selection:
        return _board_stop("清单里没有选中任何卡片，什么都没有删除；若只想保存设置，请用 plan.py profile --apply")
    print(f"【go】读取看板清单：scan_id {ex['scan_id']}，seal {seal['digest']}，选中 {len(selection)} 张卡片", flush=True)

    if settings or hide or ledger is not None:
        done, perrs = plan.prefs.apply_settings(text)
        for d in done:
            print(f"【go】已保存：{d}", flush=True)
        for e in perrs:
            print(f"【go】没保存：{e}", file=sys.stderr, flush=True)
        if perrs:
            return _board_stop("清单里的设置/台账没有全部保存成功，停下请用户看；什么都没有删除", 3)
    stale = protect.profile_guard_problem(seal, plan.load_profile())
    if stale:
        print(f"【go】{stale}；自动重新封存（plan.py seal，内含 check）", flush=True)
        if plan.main(["seal", "--run", run]) != 0:
            return _board_stop("重新封存没有通过，停下请用户看；什么都没有删除", 3)
        try:
            new_digest = plan.read_json(os.path.join(run, "seal.json")).get("digest")
        except ValueError:
            new_digest = None
        if new_digest != seal["digest"]:
            try:
                changed, drift = _seal_drift(seal, plan.read_json(os.path.join(run, "seal.json")))
            except ValueError:
                changed, drift = [], []
            if changed:
                why = f"设置/台账改变了判断结果（{', '.join(changed[:8])}）"
                if drift:
                    why += f"；另有 {len(drift)} 张卡的目录内容也在变化"
            elif drift:
                why = ("判断没有变，但这几张卡的目录内容在封存之后又变了（体积、文件大小或修改时间不同："
                       + ", ".join(drift[:8]) + "），封存内容变了所以清单要重新确认")
            else:
                why = "设置/台账改变了判断结果"
            print(f"【go】{why}（封存 digest "
                  f"{seal['digest']} → {new_digest}），需要用新看板重新确认；什么都没有删除。重新生成看板：\n"
                  f"  python3 scripts/plan.py propose --run {shlex.quote(run)}\n"
                  f"  python3 scripts/plan.py board --run {shlex.quote(run)}", file=sys.stderr, flush=True)
            return 3
        print(f"【go】重新封存后 digest 不变（{new_digest}），清单仍然有效，继续", flush=True)
        seal, err = load_seal(run, ex["seal"])
        if err:
            return _board_stop(f"拒绝：{err}", 3)
    base = dict(run=run, seal=seal["digest"], cards=",".join(ex["cards"]), answers=list(ex["answers"]),
                sel=ex["sel"], skip_pilot=False, reason=None, no_report=True)
    return _go_steps(run, base)


def cmd_reclaim(args):
    for r in reclaim_codex_history():
        print(f"{r['db']}：{fmt_bytes(r['size_before'])} → {fmt_bytes(r['size_after'])}"
              + (f"；{r['error']}" if r.get("error") else f"，完整性 {r.get('integrity')}"))
    return 0


def cmd_status(args):
    run = os.path.realpath(args.run)
    files = sorted(f for f in glob.glob(os.path.join(run, "receipt-*.json")) if "-pilot-" not in f and "-dryrun-" not in f)
    pilots = sorted(glob.glob(os.path.join(run, "receipt-pilot-*.json")))
    path = files[-1] if files else (pilots[-1] if pilots else os.path.join(run, "receipt.json"))
    try:
        rc = plan.read_json(path)
    except ValueError:
        print("还没有执行回执（receipt）")
        return 1
    print(f"回执文件：{path}（{rc.get('finished_at')}）")
    print_receipt(rc)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="叁笙清理：复核并执行")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--run", required=True)
    r.add_argument("--seal", required=True, help="seal digest（前 12 位）")
    r.add_argument("--cards", default="", help="逗号分隔的卡片 id")
    r.add_argument("--answers", nargs="*", default=[], help="decide 卡答复，如 c02=删")
    r.add_argument("--sel", default=None, help="看板导出文本里的 sel: 值（12 位）；按选中卡片与答复重算，不符则拒绝执行。--pilot 与正式执行必须带，只有 --dry-run 可省")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--pilot", action="store_true", help="只真正执行一条（decide 卡优先、再取字节最大的可执行条目），用于正式执行前试点；必须带 --sel")
    r.add_argument("--skip-pilot", action="store_true", help="正式执行时跳过试点要求（预演仍必须有），必须同时给 --reason")
    r.add_argument("--reason", default=None, help="跳过试点的原因，写进正式回执")
    g = sub.add_parser("go", help="连跑 预演 → 试点 → 正式执行 → 成果页，异常即停；--choice 按提案的一档，--from-board 按看板清单")
    g.add_argument("--run", required=True)
    g.add_argument("--choice", default=None, choices=plan.PRESETS, help="safe=稳妥 / recommended=好 / all=都删（对话确认）")
    g.add_argument("--from-board", default=None, metavar="FILE",
                   help="看板导出文本文件：校验 → 保存设置/台账 → 必要时重新封存 → 预演 → 试点 → 正式执行 → 成果页；与 --choice 二选一")
    sub.add_parser("reclaim", help="单独重跑 Codex 历史库回收（执行中断后补做）")
    s = sub.add_parser("status")
    s.add_argument("--run", required=True)
    args = ap.parse_args(argv)
    return {"run": cmd_run, "go": cmd_go, "status": cmd_status, "reclaim": cmd_reclaim}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
