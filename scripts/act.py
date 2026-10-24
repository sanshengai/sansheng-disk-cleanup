#!/usr/bin/env python3
"""复核并执行已封存的清理计划（契约第 8 节）。

用法：
  act.py run --run DIR --seal DIGEST --cards c01,c03 [--answers c02=删 c04=归档] [--sel SEL] [--dry-run | --pilot | --skip-pilot --reason 原因]
             [--trash | --no-trash [--trash-reason 用户原话]] [--archive-to /Volumes/盘名/目录]
  act.py go --run DIR (--choice safe|recommended|all | --from-board 看板导出文本文件)
  act.py status --run DIR

只信 seal.json（digest 必须与 --seal 及内容重算一致）。处置方式三种（0.6）：delete 直接删（os.unlink）、
trash 同盘 os.rename 进 ~/.Trash/叁笙清理-<scan_id>-<UTC>/、archive 复制到外接硬盘并 sha256 校验一致后再处置原文件。
默认读 profile.delete_mode（缺省 delete）；一律逐个文件、os.rmdir 清空目录，
不用 rm -rf，不用 shutil.rmtree；每个条目删除前再做一次保护检查，
只用 lstat，绝不跟随符号链接。native 命令不经过 shell。
"""
import argparse
import errno
import fcntl
import glob
import os
import hashlib
import re
import shlex
import shutil
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
ARCHIVE_WORD = "归档"
VOLUMES_ROOT = "/Volumes"      # 归档目标必须在这下面（外接硬盘）；测试里可替换
ARCHIVE_MARGIN = 1.05          # 目标盘剩余空间至少是本卡字节的 1.05 倍
HASH_CHUNK = 1 << 20
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


# ----------------------------------------------------------------- 处置方式（删除 / 废纸篓 / 归档）

def _dev(path):
    return os.stat(path).st_dev


def _free_of(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def rel_for_store(path):
    """搬到废纸篓或归档盘时保持的目录结构：家目录内用相对家目录的路径，家目录外放 _root/ 下。"""
    home = protect.HOME.rstrip("/")
    if path.startswith(home + "/"):
        return path[len(home) + 1:]
    return os.path.join("_root", path.lstrip("/"))


def file_sha256(path):
    """符号链接比较它指向的目标文本，不跟随；普通文件分块算 sha256。"""
    if os.path.islink(path):
        return "link:" + os.readlink(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _sync_path(path):
    """把文件或目录的数据真正写到盘上：macOS 用 F_FULLFSYNC（普通 fsync 只到磁盘缓存），不支持就退回 os.fsync。
    符号链接本身没有数据可同步，直接略过（所在目录另行同步）。"""
    if os.path.islink(path):
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        full = getattr(fcntl, "F_FULLFSYNC", None)
        if full is not None:
            try:
                fcntl.fcntl(fd, full)
                return
            except OSError:
                pass
        os.fsync(fd)
    finally:
        os.close(fd)


def resolve_delete_mode(args, profile):
    """--trash / --no-trash 优先，其次读 profile.delete_mode，缺省 delete（与 0.5 行为一致）。"""
    if getattr(args, "trash", False):
        return "trash"
    if getattr(args, "no_trash", False):
        return "delete"
    mode = (profile or {}).get("delete_mode")
    return mode if mode in ("trash", "delete") else "delete"


def check_archive_target(path, needed):
    """归档目标是否合格。返回 None 或中文原因。要求：在 /Volumes/<盘名>/ 下、目录存在可写、
    和家目录不是同一块盘、剩余空间不小于所需的 1.05 倍。"""
    if not path:
        return "要归档，但没有给归档目录（--archive-to，或看板里“归档到”）"
    d = os.path.abspath(path)
    root = VOLUMES_ROOT.rstrip("/")
    if not d.startswith(root + "/") or d == root + "/":
        return f"归档目录必须在外接硬盘上（{root}/<盘名>/…），收到 {path}"
    if not os.path.isdir(d):
        return f"归档目录 {d} 不存在（硬盘没插好，或路径写错了）"
    rp = os.path.realpath(d)
    if not rp.startswith(root + "/"):
        return f"归档目录 {d} 实际指向 {rp}，不在外接硬盘上"
    try:
        same = _dev(d) == _dev(protect.HOME)
    except OSError as exc:
        return f"读不到归档目录所在的盘：{exc.strerror or exc}"
    if same:
        return "归档目录和你的电脑在同一块盘上，搬过去不会腾出空间"
    if not os.access(d, os.W_OK):
        return f"没有往 {d} 写入的权限"
    try:
        free = _free_of(d)
    except OSError as exc:
        return f"读不到归档盘剩余空间：{exc.strerror or exc}"
    if free < needed * ARCHIVE_MARGIN:
        return f"归档盘剩余空间不够（需要约 {fmt_bytes(int(needed * ARCHIVE_MARGIN))}，只剩 {fmt_bytes(free)}）"
    return None


TMP_PREFIX = ".叁笙-tmp-"
STALE_TMP_SECONDS = 24 * 3600
# 硬链接不可用的错误码（FAT/exFAT/SMB 等常见）：退回 rename
LINK_UNSUPPORTED = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS, errno.EMLINK, errno.EACCES}


class Disposer:
    """统一的“处置一个文件”步骤。mode：delete / trash / archive；archive 逐个复制并校验一致后，
    原文件再按 source_mode（trash 或 delete）处置。error 非空 = 这张卡整卡跳过（归档目标不合格）。"""

    def __init__(self, mode="delete", source_mode="delete", scan_id="", stamp="", archive_to=None, error=None):
        self.mode, self.source_mode, self.error = mode, (source_mode if mode == "archive" else mode), error
        safe = (scan_id or "scan").replace("/", "_")
        self.trash_dir = (os.path.join(protect.HOME, ".Trash", f"叁笙清理-{safe}-{stamp}")
                          if "trash" in (mode, self.source_mode) else None)
        self.archive_root = os.path.join(os.path.abspath(archive_to), f"叁笙归档-{safe}") if mode == "archive" and archive_to else None
        self.volume = None
        self._stale_swept = False
        if self.archive_root:
            tail = os.path.abspath(archive_to)[len(VOLUMES_ROOT.rstrip("/")) + 1:]
            self.volume = tail.split("/", 1)[0] or None

    def describe(self):
        d = {"mode": self.mode, "dir": self.archive_root if self.mode == "archive" else self.trash_dir}
        if self.mode == "archive":
            d["source"] = self.source_mode
            d["volume"] = self.volume
            if self.trash_dir:
                d["source_dir"] = self.trash_dir
        return d

    def plan_text(self):
        if self.mode == "trash":
            return "将移入废纸篓"
        if self.mode == "archive":
            after = "再移入废纸篓" if self.source_mode == "trash" else "再删除原文件"
            return f"将搬到 {self.archive_root}（逐个校验一致后{after}）"
        return "将删除"

    def dispose(self, path):
        """处置一个文件。返回 (None, None) 成功；("skip", 原因) 或 ("fail", 原因)。"""
        if self.mode == "archive":
            bad = self._archive_copy(path)
            if bad:
                return bad
        return self._remove_source(path)

    def _remove_source(self, path):
        if self.source_mode == "trash":
            dest = os.path.join(self.trash_dir, rel_for_store(path))
            if os.path.lexists(dest):
                return "fail", "废纸篓里已有同名文件，没有覆盖"
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.rename(path, dest)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    self._prune(os.path.dirname(dest), os.path.dirname(self.trash_dir))
                    extra = "（已复制到归档盘并校验一致，原文件保留）" if self.mode == "archive" else ""
                    return "skip", "不在同一块盘，没法放进废纸篓" + extra
                return "fail", f"移入废纸篓失败：{exc.strerror or exc}"
            return None, None
        try:
            os.unlink(path)
        except OSError as exc:
            return "fail", f"unlink 失败：{exc.strerror or exc}"
        return None, None

    @staticmethod
    def _prune(d, stop):
        """EXDEV 时 makedirs 留下的空目录（含本次的废纸篓文件夹）顺手收掉：只 rmdir，非空自然失败，stop 本身不动。"""
        while d and d != stop and d.startswith(stop + "/"):
            try:
                os.rmdir(d)
            except OSError:
                return
            d = os.path.dirname(d)

    def _dest_problem(self, path, dest):
        """目标文件的真实位置必须仍在归档根之内，且和源文件不在同一块盘；否则返回原因（该文件失败，原件保留）。
        realpath 对还不存在的尾部不报错，所以在 makedirs 之前就能识别归档根里被换成符号链接的中间目录。"""
        root = os.path.realpath(self.archive_root)
        real_parent = os.path.realpath(os.path.dirname(dest))
        if real_parent != root and not real_parent.startswith(root + "/"):
            return f"归档目标的真实位置 {real_parent} 跑到了归档目录之外，没有复制，原文件保留"
        try:
            probe = real_parent
            while not os.path.lexists(probe) and probe != os.path.dirname(probe):
                probe = os.path.dirname(probe)
            if _dev(probe) == _dev(os.path.dirname(path)):
                return "归档目标和原文件在同一块盘上，搬过去不会腾出空间，没有复制，原文件保留"
        except OSError as exc:
            return f"读不到归档目标所在的盘：{exc.strerror or exc}，没有复制，原文件保留"
        return None

    def sweep_stale_tmp(self, max_age=STALE_TMP_SECONDS):
        """归档开始时清理归档根下超过 24 小时的 .叁笙-tmp-* 残留目录（上次中断留下的）。
        只认这个前缀、只在本次归档根内、只处理真目录（不跟随符号链接）；只 unlink 里面的普通文件再 rmdir，不用 rmtree。"""
        if self._stale_swept or not self.archive_root:
            return
        self._stale_swept = True
        root = self.archive_root
        now = time.time()
        for cur, dirs, _files in os.walk(root, followlinks=False):
            for name in list(dirs):
                if not name.startswith(TMP_PREFIX):
                    continue
                dirs.remove(name)                      # 不再深入残留目录本身
                d = os.path.join(cur, name)
                try:
                    st = os.lstat(d)
                    if not stat.S_ISDIR(st.st_mode) or now - st.st_mtime <= max_age:
                        continue
                    for e in os.scandir(d):
                        if e.is_file(follow_symlinks=False) or e.is_symlink():
                            os.unlink(e.path)
                    os.rmdir(d)
                except OSError:
                    pass

    def _archive_copy(self, path):
        """复制到归档盘：同目录里用 mkdtemp 建一个本次独有的临时目录，副本先写在里面，
        数据落盘 → 校验 → 改名为目标名 → 目标目录落盘 → 回读校验，全部通过才返回成功（调用方随后才处置原件）。
        只会删除本次自己创建的临时文件；目标位置已有同名文件时只比较内容（一致认、不一致判失败），绝不 unlink。"""
        self.sweep_stale_tmp()
        dest = os.path.join(self.archive_root, rel_for_store(path))
        bad = self._dest_problem(path, dest)
        if bad:
            return "fail", bad
        tmpdir = None
        try:
            want = file_sha256(path)
            ddir = os.path.dirname(dest)
            os.makedirs(ddir, exist_ok=True)
            if os.path.lexists(dest):
                if file_sha256(dest) == want:
                    return None          # 上次复制过且一致（中断后重跑），直接认
                return "fail", "归档盘上已有同名但内容不同的文件，没有覆盖，原文件保留"
            tmpdir = tempfile.mkdtemp(dir=ddir, prefix=TMP_PREFIX)
            tmp = os.path.join(tmpdir, "copy")
            shutil.copy2(path, tmp, follow_symlinks=False)
            if file_sha256(tmp) != want:
                return "fail", "复制到归档盘后校验不一致，原文件保留"
            _sync_path(tmp)
            if os.path.lexists(dest):       # 复制期间目标被别人建出来：不覆盖
                if file_sha256(dest) == want:
                    return None
                return "fail", "归档盘上已有同名但内容不同的文件，没有覆盖，原文件保留"
            # 先 os.link：目标已存在会报 EEXIST（绝不覆盖）；文件系统不支持硬链接才退回 rename
            try:
                os.link(tmp, dest)
            except FileExistsError:
                if file_sha256(dest) == want:
                    return None
                return "fail", "归档盘上已有同名但内容不同的文件，没有覆盖，原文件保留"
            except OSError as exc:
                if exc.errno not in LINK_UNSUPPORTED:
                    raise
                os.rename(tmp, dest)
            _sync_path(ddir)
            if file_sha256(dest) != want:
                return "fail", "落盘后回读校验不一致，原文件保留"
        except OSError as exc:
            return "fail", f"复制到归档盘失败，原文件保留：{exc.strerror or exc}"
        finally:
            if tmpdir:
                try:
                    os.unlink(os.path.join(tmpdir, "copy"))      # 只清自己临时目录里的东西
                except OSError:
                    pass
                try:
                    os.rmdir(tmpdir)
                except OSError:
                    pass
        return None


def disposal_flags(it):
    """回执条目 → (已腾出, 在废纸篓里, 搬到了归档盘)。没有 disposal 字段的旧回执、native 都按“已删除”算。
    归档且原文件已删 = 既腾出又在归档盘；归档且原文件进废纸篓 = 在废纸篓（清空后才腾出）又在归档盘。"""
    d = it.get("disposal") if isinstance(it, dict) else None
    mode = (d or {}).get("mode") or "delete"
    if mode == "trash":
        return False, True, False
    if mode == "archive":
        in_trash = (d or {}).get("source") == "trash"
        return (not in_trash), in_trash, True
    return True, False, False


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
    def __init__(self, index, item, disp=None):
        self.d = {"index": index, "path": item.get("path"), "method": item.get("method"),
                  "disposal": (disp or Disposer()).describe(),
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
                first = d["skipped"][0]["reason"] if d["skipped"] else None
                d["reason"] = d["reason"] or (f"没有可处理的内容（{first}）" if first else "没有可删除的内容")
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


def delete_files(files, res, parent_cache, disp=None, authorized_files=None):
    disp = disp or Disposer()
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
        if authorized_files is not None:
            try:
                if plan.run_authorization.snapshot(p) != authorized_files.get(p):
                    raise ValueError("授权后文件身份或内容变化")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                res.add_skip(p, str(exc))
                continue
        kind, why = disp.dispose(p)
        if kind == "skip":
            res.add_skip(p, why)
            continue
        if kind == "fail":
            res.add_fail(p, why)
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


def execute_item(index, item, profile, dry_run, now, run=None, disp=None):
    """进程闸门（知识卡“运行中只报告不执行”）：producer 声明的 blocked_by_processes 任一在运行或无法确认，
    正式执行（含试点）直接 skipped；预演照常算清单，但在结果里标 blocked_by 和提示，不改变“不删任何东西”。"""
    running, unknown = blocking_processes(item)
    if running or unknown:
        if running:
            why = "、".join(running) + " 正在运行，按知识卡只报告不执行"
        else:
            why = "无法确认 " + "、".join(unknown) + " 是否在运行（pgrep 超时或出错），保守跳过"
        if not dry_run:
            return ItemResult(index, item, disp).skip_item(why)
        d = _execute_item(index, item, profile, dry_run, now, run, disp)
        d["blocked_by"] = running + unknown
        d["blocked_reason"] = why
        d["reason"] = f"{d['reason'] or ''}；正式执行时会跳过：{why}".lstrip("；")
        return d
    return _execute_item(index, item, profile, dry_run, now, run, disp)


def _execute_item(index, item, profile, dry_run, now, run=None, disp=None):
    disp = disp or Disposer()
    res = ItemResult(index, item, disp)
    if item.get("method") not in ("delete", "native") or (
            item["method"] == "delete" and item.get("scope") not in plan.SCOPES):
        return res.skip_item("封存条目的 method/scope 不合法")
    rp, why = verify_identity(item, profile)
    if why:
        return res.skip_item(why)

    if disp.error:                  # 归档目标不合格：整卡不动（先于原生命令分支：native 条目也不能绕过）
        return res.skip_item(disp.error)

    if item["method"] == "native":
        if disp.mode == "archive":  # 答复“归档”的卡：原生命令没法先复制再删，一律不执行，也不当成“删”
            return res.skip_item("原生命令没法归档，先留着")
        res.d["disposal"] = {"mode": "native", "dir": None}      # 原生命令按各工具自己的方式清，不走废纸篓和归档
        return run_native(item, rp, res, dry_run, now, run)

    allow_recent, authorized_files = False, None
    if "run_authorization" in item:
        try:
            if not run:
                raise ValueError("authorization: current run required")
            auth = plan.run_authorization.validate(item, run)
            _, auth_row = plan.run_authorization.read_document(item, run)
            authorized_files = {r["path"]: r for r in auth_row["files"]}
            allow_recent = auth.get("allow_recent") is True
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return res.skip_item("本轮授权复核失败：" + str(exc))

    if "checkpoint" in item:
        ok, why = plan.artifact_checkpoint.verify(item)
        if not ok:
            return res.skip_item("临时产物复核失败：" + why)

    cand = collect_candidates(item, rp, profile, res)
    if cand is None:
        return res.d
    files, dirs, root_removable = cand

    recent = [p for p, st in files if st.st_mtime > now - RECENT_SECONDS]
    if recent and not allow_recent:
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
        res.d["reason"] = f"dry-run：{disp.plan_text()} {len(files)} 个文件"
        return res.finish()

    parent_cache = {}
    delete_files(files, res, parent_cache, disp, authorized_files)
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
    """返回 [(card_id, 是否执行, 原因)]。decide 卡只在答复「删」或「归档」时执行（即使没出现在 --cards 里）。"""
    order = list(requested)
    for cid, ans in answers.items():
        if ans in (DELETE_WORD, ARCHIVE_WORD) and cid not in order:
            order.append(cid)
    out = []
    for cid in order:
        card = seal["cards"].get(cid)
        ans = answers.get(cid)
        if card is None:
            out.append((cid, False, "不在封存内（未通过校验、被降级或编号错误）"))
        elif card["verdict"] == "decide":
            if ans in (DELETE_WORD, ARCHIVE_WORD):
                out.append((cid, True, None))
            elif ans is None:
                out.append((cid, False, "decide 卡片没有收到答复，不执行"))
            else:
                out.append((cid, False, f"答复为「{ans}」，不执行"))
        else:
            if ans is not None and ans not in (DELETE_WORD, ARCHIVE_WORD):
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


def archive_bytes(card):
    """一张答复「归档」的卡要复制的字节数：原生命令条目不归档（执行时直接跳过），不算。"""
    total = 0
    for it in card.get("items") or []:
        if it.get("method") == "native":
            continue
        try:
            total += int(it.get("bytes") or 0)
        except (TypeError, ValueError):
            pass
    return total


def make_disposer(card, archive, opts, scan_id, stamp, needed=None):
    """这张卡怎么处置：答复「归档」→ archive（目标先整体校验，不合格整卡跳过）；否则按 delete_mode。
    needed：本次全部归档卡合计需要的字节（run_plan 算好传入，剩余空间按合计核对）；不给就只算这一张卡。"""
    base = opts.get("delete_mode") or "delete"
    if not archive:
        return Disposer(base, scan_id=scan_id, stamp=stamp)
    target = opts.get("archive_to")
    if needed is None:
        needed = archive_bytes(card)
    err = check_archive_target(target, needed)
    return Disposer("archive", source_mode=base, scan_id=scan_id, stamp=stamp, archive_to=target, error=err)


def run_plan(run, seal, selection, dry_run, pilot=False, opts=None, answers=None):
    """opts：{"delete_mode": delete|trash, "archive_to": 目录或 None}；answers：{卡号: 答复}，答复「归档」的卡走归档。
    缺省（都不给）= 0.5 行为：直接删除。"""
    opts = opts or {}
    answers = answers or {}
    profile = plan.load_profile()
    now = time.time()
    before = free_bytes()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scan_id = seal["scan_id"]
    disposers = {}
    # 归档盘的剩余空间按本次全部归档卡的合计核对（不是每张卡各自核对一次）
    arch_needed = sum(archive_bytes(seal["cards"][cid]) for cid, do, _ in selection
                      if do and answers.get(cid) == ARCHIVE_WORD and cid in seal["cards"])

    def disp_for(cid):
        if cid not in disposers:
            disposers[cid] = make_disposer(seal["cards"][cid], answers.get(cid) == ARCHIVE_WORD, opts, scan_id, stamp,
                                           needed=arch_needed)
        return disposers[cid]

    receipt = {"schema": "receipt/1", "scan_id": scan_id, "seal": seal["digest"],
               "started_at": now_iso(), "dry_run": dry_run, "cards": [],
               "mode": "dry_run" if dry_run else ("pilot" if pilot else "full"),
               "delete_mode": opts.get("delete_mode") or "delete"}
    pilot_results = {}
    if pilot:
        # 试点：全局按 pilot_order 逐条尝试，直到有一条真正执行（removed/partial/failed）；其余条目不执行也不记录。
        # 试点证明执行路径可用，不证明每条都安全
        for cid, idx in pilot_order(seal, selection):
            it = seal["cards"][cid]["items"][idx]
            r = execute_item(it.get("index", idx), it, profile, dry_run, now, run, disp_for(cid))
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
            d = disp_for(cid)
            items = [execute_item(it.get("index", i), it, profile, dry_run, now, run, d)
                     for i, it in enumerate(card["items"])]
        entry = {"id": cid, "title": card.get("title"), "verdict": card["verdict"],
                 "status": "planned" if dry_run else ("pilot" if pilot else "executed"), "items": items}
        if answers.get(cid) == ARCHIVE_WORD:
            entry["archive"] = True
        if pilot and not items:
            entry["status"] = "skipped"
            entry["reason"] = "试点模式只执行一条"
        receipt["cards"].append(entry)
    if any(do and answers.get(cid) == ARCHIVE_WORD for cid, do, _ in selection):
        receipt["archive_to"] = opts.get("archive_to")        # 记下来，find_gate 据此核对预演/试点与正式执行用的是同一个归档目录
    trash_dirs = sorted({d.trash_dir for d in disposers.values() if d.trash_dir})
    if trash_dirs:
        receipt["trash_dir"] = trash_dirs[0]
    if pilot:
        receipt["pilot"] = True
    if not dry_run and codex_deleted(receipt):
        receipt["history_reclaim"] = reclaim_codex_history()
    after = free_bytes()
    summary = {"removed_files": 0, "removed_dirs": 0, "skipped_items": 0, "skipped_entries": 0,
               "failed_items": 0, "failed_entries": 0, "removed_items": 0, "bytes_by_files": 0,
               "bytes_native": 0, "bytes_freed_now": 0, "bytes_trash": 0, "bytes_archived": 0}
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
                freed, in_trash, archived = disposal_flags(it)
                b = it["removed"]["bytes"]
                summary["bytes_freed_now"] += b if freed else 0
                summary["bytes_trash"] += b if in_trash else 0
                summary["bytes_archived"] += b if archived else 0
            elif it["status"] == "skipped":
                summary["skipped_items"] += 1
            elif it["status"] == "failed":
                summary["failed_items"] += 1
    measured = after - before
    reclaimed = sum(max(0, r.get("reclaimed", 0)) for r in receipt.get("history_reclaim", []))
    # bytes_by_files 已包含原生命令条目的实测字节（removed.bytes），bytes_native 只是其中的原生命令部分。
    # 放进废纸篓的字节要清空废纸篓才腾出，不计入“按条目释放”（否则会和磁盘余量变化对不上）
    summary.update({"free_before": before, "free_after": after, "freed_measured": measured,
                    "history_reclaimed": reclaimed,
                    "freed_by_items": summary["bytes_freed_now"] + reclaimed})
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
            for sk in it["skipped"][:3]:
                if "废纸篓" in sk["reason"] or "归档" in sk["reason"]:
                    print(f"        跳过 {sk['path']}：{sk['reason']}")
            for f in it["failed"][:5]:
                print(f"        失败 {f['path']}：{f['error']}")
    print(f"合计：处理文件 {s['removed_files']}、目录 {s['removed_dirs']}，跳过条目 {s['skipped_entries']}，"
          f"失败条目 {s['failed_entries']}；按文件 {fmt_bytes(s['bytes_by_files'])}", end="")
    if rc["dry_run"]:
        print("（预计）")
    else:
        print()
        for r in rc.get("history_reclaim", []):
            print(f"  历史库回收 {r['db']}：{fmt_bytes(r['size_before'])} → {fmt_bytes(r['size_after'])}"
                  + (f"；{r['error']}" if r.get("error") else f"，完整性 {r.get('integrity')}"))
        print(f"按条目核算释放 {fmt_bytes(s['freed_by_items'])}（直接删除的文件 {fmt_bytes(s.get('bytes_freed_now', s['bytes_by_files']))}，"
              f"其中原生命令 {fmt_bytes(s['bytes_native'])}；历史库回收 {fmt_bytes(s['history_reclaimed'])}）；"
              f"磁盘余量变化 {fmt_bytes(s['freed_measured'])}")
        if s.get("bytes_trash"):
            print(f"已移入废纸篓 {fmt_bytes(s['bytes_trash'])}（{rc.get('trash_dir')}）：清空废纸篓后才会真正腾出，我不会替你清空")
        if s.get("bytes_archived"):
            print(f"已搬到归档盘 {fmt_bytes(s['bytes_archived'])}（{rc.get('archive_to')}）")
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


def find_gate(run, seal, sel, card_ids=None, delete_mode=None, archive_to=None):
    """在 run 目录里找与本 seal、本 sel 对应的预演回执与成功的试点回执。返回 (预演文件名或 None, 试点文件名或 None)。
    只认回执里的 seal/scan_id/sel/mode 字段（没有 sel 的旧回执一律不算），文件名不参与判断。
    card_ids 给出时，回执里的卡片集合必须与本次选择一致；试点回执里删掉的那一条还要现场核对确实不在了。
    delete_mode / archive_to 给出时（cmd_run 总会给），回执里记录的删除方式与归档目录必须与本次一致：
    预演、试点看到的是什么处置方式，正式执行就必须是什么；没有 delete_mode 字段的旧回执按“delete”算。
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
            if delete_mode is not None and (rc.get("delete_mode") or "delete") != delete_mode:
                continue
            if archive_to is not None and plan.norm_archive_to(rc.get("archive_to")) != plan.norm_archive_to(archive_to):
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


def record_choices(run, seal, chosen, skip=()):
    """飞轮：正式执行完，按 cardkeys.json（propose / board 写出）记下这次每一类删了没有。记录失败不影响清理结果。
    skip：答复「归档」的卡——搬走不等于删掉，也不是拒绝，不记进“删没删”的偏好里。"""
    try:
        keys = plan.read_json(os.path.join(run, "cardkeys.json"))
    except (OSError, ValueError):
        return
    offered = [{"id": cid, **v} for cid, v in keys.items() if isinstance(v, dict) and cid in (seal.get("cards") or {}) and cid not in skip]
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


def effective_archive_to(archive_to, answers):
    """进入选择指纹的归档目录：只有本次确有答复「归档」的卡时才算（没有归档卡时给了 --archive-to 也不参与、不改变签名）。"""
    if any(v == ARCHIVE_WORD for v in (answers or {}).values()):
        return plan.norm_archive_to(archive_to)
    return ""


def trash_conflict(args, profile):
    """--no-trash 与 profile.delete_mode=trash 冲突：用户设置的是“先放废纸篓”，命令行要覆盖它就得给出用户的原话。"""
    return bool(getattr(args, "no_trash", False)) and (profile or {}).get("delete_mode") == "trash"


def cmd_run(args):
    if args.skip_pilot and not (args.reason or "").strip():
        print("拒绝执行：--skip-pilot 必须同时给出非空的 --reason \"<原因>\"（原因会写进回执）", file=sys.stderr)
        return 2
    if args.reason is not None and not args.skip_pilot:
        print("拒绝执行：--reason 只能与 --skip-pilot 一起使用（--no-trash 的用户原话请用 --trash-reason）", file=sys.stderr)
        return 2
    trash_reason = getattr(args, "trash_reason", None)
    if trash_reason is not None and not getattr(args, "no_trash", False):
        print("拒绝执行：--trash-reason 只能与 --no-trash 一起使用", file=sys.stderr)
        return 2
    if args.skip_pilot and (args.dry_run or args.pilot):
        print("拒绝执行：--skip-pilot 只用于正式执行，不能与 --dry-run 或 --pilot 同用", file=sys.stderr)
        return 2
    bad_home = protect.home_problem()
    if bad_home:
        print(f"拒绝执行：{bad_home}", file=sys.stderr)
        return 2
    profile = plan.load_profile()
    if trash_conflict(args, profile) and not (trash_reason or "").strip():
        print("拒绝执行：你的设置是“删除先放废纸篓”（delete_mode=trash），--no-trash 要直接删就必须同时给 "
              "--trash-reason \"<用户的原话>\"（原话会写进回执）；没有原话就不要用 --no-trash", file=sys.stderr)
        return 2
    if resolve_delete_mode(args, profile) == 'trash':
        print('处置方式：先放废纸篓，本步不会立即释放空间；如需立即腾出，需用户授权本轮永久删除。', flush=True)
    else:
        print('处置方式：直接永久删除，完成后腾出空间；删除不可撤销。', flush=True)
    run = os.path.realpath(args.run)
    seal, err = load_seal(run, args.seal)
    if err:
        print(f"拒绝执行：{err}", file=sys.stderr)
        return 2
    stale = protect.profile_guard_problem(seal, profile)
    if stale:
        print(f"拒绝执行：{stale}", file=sys.stderr)
        return 2
    requested = [c.strip() for c in (args.cards or "").split(",") if c.strip()]
    answers = parse_answers(args.answers)
    selection = select_cards(seal, requested, answers)
    arch_eff = effective_archive_to(getattr(args, "archive_to", None), answers)
    sel = plan.selection_sig(seal["digest"], [cid for cid, _, _ in selection], answers, arch_eff)
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
        dry_rc, pilot_rc = find_gate(run, seal, sel, [c for c, _, _ in selection],
                                     delete_mode=resolve_delete_mode(args, profile), archive_to=arch_eff)
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
                print("（确需跳过试点：加 --skip-pilot --reason \"<原因>\"；预演不能跳过。更换卡片或答复后 sel 会变，需要重新预演和试点；"
                      "预演、试点与正式执行的删除方式（--trash/--no-trash/设置里的 delete_mode）和归档目录必须一致，改了也要重做）",
                      file=sys.stderr)
            return 2
        gate = {"dry_run_receipt": dry_rc, "pilot_receipt": pilot_rc, "skip_pilot": bool(args.skip_pilot)}
        if args.skip_pilot:
            gate["skip_reason"] = args.reason.strip()
    opts = {"delete_mode": resolve_delete_mode(args, profile),
            "archive_to": getattr(args, "archive_to", None)}
    receipt = run_plan(run, seal, selection, args.dry_run, pilot=args.pilot, opts=opts, answers=answers)
    if trash_conflict(args, profile):
        receipt["no_trash_reason"] = trash_reason.strip()
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
        record_choices(run, seal, [cid for cid, do, _ in selection if do],
                       skip=[cid for cid, a in answers.items() if a == ARCHIVE_WORD])
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
    started = plan.prefs.now_iso()
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
                sel=opt.get("sel"), skip_pilot=False, reason=None, trash_reason=_go_trash_reason(args), no_report=True, **_dispose_args(args))
    return _go_steps(run, base, since=started)


def _dispose_args(args, archive_to=None):
    """go 传给 cmd_run 的处置方式参数：--trash / --no-trash / --archive-to（看板导出的“归档到:”优先于命令行）。"""
    return {"trash": bool(getattr(args, "trash", False)), "no_trash": bool(getattr(args, "no_trash", False)),
            "archive_to": archive_to or getattr(args, "archive_to", None)}


def _go_trash_reason(args):
    """go 的 --trash-reason 只用于 --no-trash 覆盖“先放废纸篓”设置时的用户原话（go 没有跳过试点，也就没有 --reason）。"""
    return getattr(args, "trash_reason", None)


def board_archive_to(ex, text):
    """看板导出里的“归档到:”。parse_board_export 若已解析出 archive_to 就用它，否则这里自己认这一行
    （接口约定里该字段由 prefs.py 提供，这是兼容读法）。返回 (路径或 None, 错误或 None)。"""
    if ex.get("archive_to"):
        return ex["archive_to"], None
    vals = []
    for ln in (text or "").splitlines():
        m = re.match(r"^\s*归档到\s*[:：]\s*(.*?)\s*$", ln)
        if m and m.group(1) and m.group(1) != "无":
            vals.append(m.group(1))
    if len(set(vals)) > 1:
        return None, "“归档到:”出现了多个不同的路径"
    return (vals[0] if vals else None), None


def _memory_wrapup(run, since, tag="【go】"):
    """成果页之前：汇总这次记住的（remembered.json）；有“从源头改好的”记录就复查一遍（governance-check.json）。
    都是锦上添花：任何一步出问题只提示，不影响已完成的清理，也不挡成果页。"""
    try:
        import memory
        rows = memory.remembered(run, since=since)
        print(f"{tag}这次我记住了 {len(rows)} 条", flush=True)
        if plan.load_profile().get("governance"):
            checked = memory.govern_check(run)
            print(f"{tag}复查了 {len(checked)} 处从源头改好的设置", flush=True)
    except (OSError, ValueError, ImportError) as exc:
        print(f"{tag}记忆汇总没做完：{exc}", file=sys.stderr, flush=True)


def _go_steps(run, base, since=None):
    """预演 → 试点 → 正式执行 → 成果页。对话确认（--choice）与看板清单（--from-board）共用，闸门一步不少：
    每一步都是 cmd_run，sel、profile_guard、预演与试点回执的核对都在 cmd_run 里。
    since：本次 go 开始的时间（ISO），“这次我记住了”据此认出这次保存的设置与规则。"""
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
    _memory_wrapup(run, since)
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
    started = plan.prefs.now_iso()
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
    archive_to, aerr = board_archive_to(ex, text)
    if aerr:
        return _board_stop(f"拒绝：{aerr}，什么都没有删除")
    want = plan.selection_sig(seal["digest"], [cid for cid, _, _ in selection], answers,
                              effective_archive_to(archive_to, answers))
    if want != ex["sel"]:       # 先于保存设置核对：对不上的清单不改动本机设置
        return _board_stop(f"拒绝：清单的 sel {ex['sel']} 与按清单里的卡片和回答算出的 {want} 不一致（清单被改过或转抄出错），"
                           "什么都没有保存、没有删除")
    if not selection:
        return _board_stop("清单里没有选中任何卡片，什么都没有删除；若只想保存设置，请用 plan.py profile --apply")
    print(f"【go】读取看板清单：scan_id {ex['scan_id']}，seal {seal['digest']}，选中 {len(selection)} 张卡片", flush=True)

    # 只有“确认规则: / 不要规则:”行的清单也要先保存规则，且在任何删除之前
    confirm_ids, reject_ids, _ = plan.prefs.parse_rule_decisions(text)
    if settings or hide or ledger is not None or confirm_ids or reject_ids:
        done, perrs = plan.prefs.apply_settings(text, scan_id=ex["scan_id"])
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
                sel=ex["sel"], skip_pilot=False, reason=None, trash_reason=_go_trash_reason(args), no_report=True, **_dispose_args(args, archive_to))
    return _go_steps(run, base, since=started)


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


def _add_dispose_args(p):
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--trash", action="store_true", help="删除改为移入废纸篓（不腾出空间，清空废纸篓后才腾出）；缺省读 profile.delete_mode")
    grp.add_argument("--no-trash", dest="no_trash", action="store_true", help="强制直接删除，忽略 profile.delete_mode=trash")
    p.add_argument("--trash-reason", dest="trash_reason", default=None,
                   help="与 --no-trash 同用：设置里是“删除先放废纸篓”却要直接删时，写用户的原话（写进回执 no_trash_reason）")
    p.add_argument("--archive-to", dest="archive_to", default=None, metavar="DIR",
                   help="答复「归档」的卡搬到这里（必须在 /Volumes/<盘名>/ 下的外接硬盘）；go --from-board 时看板导出的“归档到:”优先")


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
    r.add_argument("--reason", default=None, help="跳过试点的原因，写进正式回执（只与 --skip-pilot 同用）")
    _add_dispose_args(r)
    g = sub.add_parser("go", help="连跑 预演 → 试点 → 正式执行 → 成果页，异常即停；--choice 按提案的一档，--from-board 按看板清单")
    g.add_argument("--run", required=True)
    g.add_argument("--choice", default=None, choices=plan.PRESETS, help="safe=稳妥 / recommended=好 / all=都删（对话确认）")
    g.add_argument("--from-board", default=None, metavar="FILE",
                   help="看板导出文本文件：校验 → 保存设置/台账 → 必要时重新封存 → 预演 → 试点 → 正式执行 → 成果页；与 --choice 二选一")
    _add_dispose_args(g)
    sub.add_parser("reclaim", help="单独重跑 Codex 历史库回收（执行中断后补做）")
    s = sub.add_parser("status")
    s.add_argument("--run", required=True)
    args = ap.parse_args(argv)
    return {"run": cmd_run, "go": cmd_go, "status": cmd_status, "reclaim": cmd_reclaim}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
