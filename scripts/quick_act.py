#!/usr/bin/env python3
"""已授权清单执行器：Agent 判断好的清单 + 用户原话 → 逐项复核 → 执行 → 回执、清理日志、恢复命令。

为什么有它（2026-11-03 对照实验）：用户已经整批授权（"低风险的直接删""证据充分的全部删"）时，看板全套流程太重，
Agent 会临时写脚本自己删——复核、日志、恢复命令全靠当天自觉，脚本用完即丢，下一个 Agent 拿不到。
同一天的临时脚本还误删了另一个 Agent 正在用的原片：只搜了文档引用、没搜代码，没看那条线当天有人在提交，
没备份的过程件没先隔离（见 references/incident-20261103-active-agent-evidence.md）。本脚本把这些都变成删前强制检查。

  python3 scripts/quick_act.py plan  --items items.json --user-text '<用户原话>' --run <运行目录> [--upload-missing]
  python3 scripts/quick_act.py run   --run <运行目录> --seal <plan 打印的封存码> [--pilot]
  python3 scripts/quick_act.py purge --run <运行目录> [--days 7] [--dry-run]   # 隔离满 N 天后才真正删除

items.json 是数组，每项：
  {"id": "名字", "path": "/绝对路径", "scope": "all|children|files|untracked", "files": [...仅 scope=files],
   "kind": "regenerable|backed_up|process", "reason": "为什么能清", "quiet_hours": 6, "app": "可选，对应 App 名"}
  kind：regenerable = 已登记的可再生缓存（第 1 档白名单、本机配置 extra_tier1、node_modules/__pycache__ 等）→ 直接删；
                      不在登记里的“可再生”一律按 process 处理（Agent 自己声明的“可重建”不算数）；
        backed_up   = 有盘外备份 → 调用本机配置的备份适配器逐文件核对（可 --upload-missing 先补传）后才删；
        process     = 没有盘外备份的过程件 → 只改名隔离（<名>.待删-YYYYMMDD），满 7 天后 purge 才删。
  scope：all 整个路径；children 删目录下每个子项（保留目录本身）；files 只删列出的文件；
         untracked 只删 Git 工作树里未跟踪和忽略的文件（已跟踪文件不动）。

删前强制检查（plan 和 run 各做一遍，run 时以当下事实为准，删每个目标前再测一次）：保护名单、生产目录、
Git 跟踪文件与 .git、文档与代码引用（含仓库相对路径、不受 .gitignore 影响）、目标及上一级目录里的已跟踪脚本、
目标及上一级目录近 7 天的提交、其他 Agent 近 72 小时的对话记录、打开的文件、静默期内的写入（mtime 与 ctime 取晚者）、
对应 App 是否在运行、盘外备份核对。生产目录、有消费者或有他人活动的路径，只有用户原话里写出了它的路径
（或不少于 8 个字的完整目录名）才放行。任何一项检查自身失败（超时、命令出错、配置读不了）都按拒绝处理。
"""
import argparse
import bisect
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import batch_space
from cold_consumers import ConsumerChecks
import session_materials
import production_handoff
import protect  # noqa: E402
import triage  # noqa: E402

SCHEMA = "quick-act/2"
HOME = protect.HOME
KINDS = {
    "regenerable": "可再生",
    "backed_up": "有盘外备份",
    "process": "过程件（无盘外备份）",
    "verified_obsolete": "已核实冗余或已交付过程件（用户点名直删）",
}
SCOPES = ("all", "children", "files", "untracked")
SKIP_NAMES = {".DS_Store"}   # 只有它既不算写入、也不进备份核对；*.lock 等是真实内容
CODE_GLOBS = ("*.py", "*.sh", "*.ts", "*.js", "*.mjs", "*.cjs", "*.ps1", "*.psm1", "*.toml", "*.yaml", "*.yml", "*.json")
CODE_EXT = (".py", ".sh", ".ts", ".js", ".mjs", ".cjs", ".ps1", ".psm1")
REGEN_NAMES = {"node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
# 太常见的目录名不单独拿去搜（会被无关文件命中）
GENERIC = {
    "production", "frames", "cache", "caches", "tmp", "temp", "build", "dist", "out", "output", "outputs",
    "src", "data", "runs", "logs", "assets", "sessions", "conversations", "brain", "images", "videos",
    "dl", "store", "inspect", "packs", "drafts", "backups", "work", "workspace", "requests",
    "local", ".local", "public", "v1", "v2", "v3", "v4", "ark",
}
TRANSCRIPT_GLOBS = (
    HOME + "/.claude/projects/*/*.jsonl",
    HOME + "/.claude/projects/*/*/subagents/**/*.jsonl",
    HOME + "/.codex/sessions/**/*.jsonl",
    HOME + "/.grok/sessions/**/*.json*",
    HOME + "/.gemini/antigravity-cli/conversations/*.db",
    HOME + "/.gemini/antigravity-cli/conversations/*.db-wal",
    HOME + "/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript*.jsonl",
)
QUARANTINE_TAG = ".待删-"
QUARANTINE_RX = re.compile(r"\.待删-(\d{8})$")
DIRECT_DELETE_INTENT_RX = re.compile(r"直接.{0,8}删|永久删除|彻底清理|彻底清除|不用隔离")
MIN_MARKER = 12   # 自动识别本会话的原话片段至少这么长；清理日志只写原话前 LOG_QUOTE 个字，保证日志里不含标记
LOG_QUOTE = 10


# ---------- 小工具 ----------
def now_iso():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")


def bjt(fmt="%Y-%m-%d %H:%M"):
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime(fmt)


def sh(cmd, timeout=60, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "not found"


def lexical(p):
    return os.path.normpath(os.path.abspath(os.path.expanduser(p)))


def gib(b):
    b = b or 0
    for unit, n in (("GiB", 2 ** 30), ("MiB", 2 ** 20), ("KiB", 2 ** 10)):
        if abs(b) >= n:
            return "%.2f %s" % (b / n, unit)
    return "%d B" % b


def gib_optional(value):
    return "未知" if value is None else gib(value)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def free_bytes():
    try:
        s = os.statvfs(HOME)
        return s.f_bavail * s.f_frsize
    except OSError:
        return None


def under(p, root):
    return bool(root) and (p == root or p.startswith(root.rstrip("/") + "/"))


def tilde(p):
    return "~" + p[len(HOME):] if under(p, HOME) else p


def check_local_config():
    """本机配置存在却读不了时，生产目录、引用、台账检查会悄悄失效：直接停。"""
    path = os.environ.get("SANSHENG_DISK_CLEANUP_TRIAGE") or HOME + "/.sansheng/disk-cleanup/triage-local.json"
    path = os.path.expanduser(path)
    if os.path.exists(path):
        try:
            json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise SystemExit("本机配置 %s 读不了（%s）：修好再清理，否则保护检查会失效" % (path, e))


def ephemeral_run_dir_problem(run_dir):
    """生产运行的 --run 不允许放在 /tmp 或系统 $TMPDIR 等易失目录，免得隔离回执和恢复命令在重启后丢失。"""
    if not run_dir or not str(run_dir).strip():
        return "运行目录 --run 不能为空；请放到 ~/.sansheng/disk-cleanup/runs/... 或工作区 _workspace/disk-watch/"
    raw = os.path.expanduser(str(run_dir).strip())
    real = os.path.realpath(raw)
    if "/pytest-of-" in raw or "/pytest-of-" in real:
        return None
    sys_tmp = os.path.realpath(os.environ.get("TMPDIR") or "/tmp").rstrip("/")
    for bad in dict.fromkeys(("/tmp", "/private/tmp", sys_tmp)):
        if bad and (real == bad or real.startswith(bad + "/")):
            return "运行目录 --run 不能放在易失临时目录 %s：请放到 ~/.sansheng/disk-cleanup/runs/... 或工作区 _workspace/disk-watch/" % tilde(real)
    return None


# ---------- 清单校验 ----------
def validate(items):
    if not isinstance(items, list) or not items:
        raise ValueError("清单必须是非空数组")
    seen, out = set(), []
    for i, x in enumerate(items):
        need = ("id", "path", "scope", "kind", "reason")
        miss = [k for k in need if not str(x.get(k) or "").strip()]
        if miss:
            raise ValueError("第 %d 项缺少字段：%s" % (i + 1, ", ".join(miss)))
        if x["id"] in seen:
            raise ValueError("id 重复：%s" % x["id"])
        seen.add(x["id"])
        if not os.path.isabs(os.path.expanduser(x["path"])):
            raise ValueError("%s：path 必须是绝对路径" % x["id"])
        if x["scope"] not in SCOPES:
            raise ValueError("%s：scope 只能是 %s" % (x["id"], "/".join(SCOPES)))
        if x["kind"] not in KINDS:
            raise ValueError("%s：kind 只能是 %s" % (x["id"], "/".join(KINDS)))
        if x["scope"] == "files" and not x.get("files"):
            raise ValueError("%s：scope=files 需要 files 列表" % x["id"])
        if x["kind"] == "process" and x["scope"] in ("files", "untracked"):
            raise ValueError("%s：没有盘外备份的过程件只能整目录隔离（scope=all/children）；"
                             "要按文件处理请先备份并标 kind=backed_up" % x["id"])
        keep_names = x.get("keep_names")
        if keep_names is not None:
            if x["scope"] != "children":
                raise ValueError("%s：keep_names 仅适用于 scope=children" % x["id"])
            if not isinstance(keep_names, list) or any(not isinstance(k, str) or not k or "/" in k for k in keep_names):
                raise ValueError("%s：keep_names 必须是不含 / 的文件名列表" % x["id"])
        q = float(x.get("quiet_hours", 6))
        if q < 1:
            raise ValueError("%s：quiet_hours 不能小于 1" % x["id"])
        y = dict(x, path=lexical(x["path"]), quiet_hours=q)
        if keep_names:
            y["keep_names"] = sorted(set(keep_names))
        if x["scope"] == "files":
            y["files"] = [lexical(f) for f in x["files"]]
            bad = [f for f in y["files"] if not f.startswith(y["path"].rstrip("/") + "/")]
            if bad:
                raise ValueError("%s：files 必须在 path 之内：%s" % (x["id"], bad[0]))
        out.append(y)
    for it in out:
        if "session_materials" in it:
            proof = it["session_materials"]
            if not isinstance(proof, dict):
                raise ValueError("素材保留证明格式无效")
            copies = session_materials.proofs(proof)
            for target in out:
                for dep in (proof["path"], *copies.values()):
                    selected = (lexical(dep) in target["files"] if target["scope"] == "files"
                                else under(os.path.realpath(dep), target["path"]))
                    if selected:
                        raise ValueError("素材保留证明或副本也在本轮清理范围")
    return out


def known_regenerable(p):
    """路径落在已登记的可再生缓存里（第 1 档白名单含本机 extra_tier1，或 node_modules 等通用名）。"""
    if any(part in REGEN_NAMES for part in p.split("/")):
        return True
    for _, globs, _, _ in triage.TIER1:
        for g in globs:
            for m in glob.glob(g):
                if under(p, m):
                    return True
    return False


def effective_kind(item):
    if item["kind"] == "regenerable" and not known_regenerable(item["path"]):
        return "process"
    return item["kind"]


# ---------- 事实采集 ----------
def git_top(p):
    d = p if os.path.isdir(p) else os.path.dirname(p)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    rc, out, _ = sh(["git", "-C", d, "rev-parse", "--show-toplevel"], timeout=20)
    return out.strip() if rc == 0 and out.strip() else None


def git_list(top, rel, *extra):
    rc, out, _ = sh(["git", "-C", top, "-c", "core.quotepath=false", "ls-files", "-z", *extra, "--", rel or "."], timeout=180)
    if rc != 0:
        return None
    return [x for x in out.split("\0") if x]


def targets_of(item):
    """返回 [(路径, 类型)]；路径都已存在；隔离区里的东西永远不算目标。"""
    p, scope = item["path"], item["scope"]
    isdir = lambda q: os.path.isdir(q) and not os.path.islink(q)
    if scope == "all":
        return [(p, "dir" if isdir(p) else "file")]
    if scope == "children":
        keep = set(item.get("keep_names") or ())
        return [(os.path.join(p, n), "dir" if isdir(os.path.join(p, n)) else "file")
                for n in sorted(os.listdir(p)) if not QUARANTINE_RX.search(n) and n not in keep]
    if scope == "files":
        return [(f, "file") for f in item["files"] if os.path.lexists(f) and not QUARANTINE_RX.search(f)]
    top = git_top(p)
    if not top:
        raise ValueError("scope=untracked 但路径不在 Git 工作树里")
    rel = os.path.relpath(p, top)
    rel_norm = rel.rstrip("/")
    tracked_here = git_list(top, rel)
    ign_entries = git_list(top, rel, "--others", "--ignored", "--exclude-standard", "--directory")
    if tracked_here is None or ign_entries is None:
        raise ValueError("git ls-files 失败")
    out_dirs, covered = [], []
    for x in ign_entries:
        if not x.endswith("/"):
            continue
        drel = x.rstrip("/")
        full = os.path.join(top, drel)
        if any(QUARANTINE_RX.search(seg) for seg in drel.split("/")):
            continue
        if os.path.islink(full):
            out_dirs.append((full, "file")); covered.append(x); continue
        if any(t == drel or t.startswith(drel + "/") for t in tracked_here):
            continue
        if os.path.exists(os.path.join(full, ".git")):
            continue
        has_q = False
        for _, dns, fns in os.walk(full):
            if any(QUARANTINE_RX.search(n) for n in dns + fns) or ".git" in dns:
                has_q = True; break
        if has_q:
            continue
        if drel == rel_norm:
            for n in sorted(os.listdir(p)):
                if QUARANTINE_RX.search(n) or n == ".git":
                    continue
                fp = os.path.join(p, n)
                if os.path.lexists(fp):
                    out_dirs.append((fp, "dir" if isdir(fp) else "file"))
            return sorted(set(out_dirs))
        out_dirs.append((full, "dir")); covered.append(x)
    files = set()
    untracked = git_list(top, rel, "--others", "--exclude-standard")
    if untracked is None:
        raise ValueError("git ls-files 失败")
    for x in untracked:
        if not any(x.startswith(pre) for pre in covered):
            files.add(x)
    need_expand = [x for x in ign_entries if not any(x == pre or x.startswith(pre) for pre in covered)]
    if any(x.endswith("/") for x in need_expand):
        ign_full = git_list(top, rel, "--others", "--ignored", "--exclude-standard")
        if ign_full is None:
            raise ValueError("git ls-files 失败")
        for x in ign_full:
            if not any(x.startswith(pre) for pre in covered):
                files.add(x)
    else:
        files.update(need_expand)
    file_targets = [(os.path.join(top, f), "file") for f in sorted(files)
                    if os.path.lexists(os.path.join(top, f)) and not any(QUARANTINE_RX.search(x) for x in f.split("/"))]
    return sorted(set(out_dirs + file_targets))


def measure(path):
    """一次遍历：占用（按 inode 去重、多链接单列）、文件数、最新写入（mtime/ctime 取晚者；并单记内容 newest_mtime）、
    是否含 .git 或隔离项、普通文件清单（备份核对用）、符号链接清单（回执里留着，恢复时可重建）。"""
    seen, size, n, newest, newest_mt, files, links, shared = set(), 0, 0, None, None, [], [], 0
    flags = {"has_git": False, "has_quarantine": False}

    def one(fp, st):
        nonlocal size, n, newest, newest_mt, shared
        if (st.st_dev, st.st_ino) in seen:
            return
        seen.add((st.st_dev, st.st_ino))
        n += 1
        if st.st_nlink > 1 and not stat.S_ISDIR(st.st_mode):
            shared += st.st_blocks * 512
        else:
            size += st.st_blocks * 512
        if stat.S_ISLNK(st.st_mode):
            try:
                links.append([fp, os.readlink(fp)])
            except OSError:
                links.append([fp, None])
        if os.path.basename(fp) in SKIP_NAMES:
            return
        if stat.S_ISREG(st.st_mode):
            files.append(fp)
        t = max(st.st_mtime, st.st_ctime)
        if newest is None or t > newest[0]:
            newest = (t, fp)
        if newest_mt is None or st.st_mtime > newest_mt[0]:
            newest_mt = (st.st_mtime, fp)

    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        one(path, st)
    else:
        for d, ds, fs in os.walk(path):
            if ".git" in ds or ".git" in fs:
                flags["has_git"] = True
            for name in ds + fs:
                if QUARANTINE_RX.search(name):
                    flags["has_quarantine"] = True
                fp = os.path.join(d, name)
                try:
                    s = os.lstat(fp)
                except OSError:
                    continue
                if stat.S_ISDIR(s.st_mode):
                    continue  # 目录本身不计；里面的内容会被走到
                one(fp, s)
    return dict(flags, bytes=size, shared_bytes=shared, files=n, file_list=files, symlinks=links,
                newest={"time": dt.datetime.fromtimestamp(newest[0]).isoformat(timespec="minutes"), "file": newest[1]} if newest else None,
                newest_ts=newest[0] if newest else None,
                newest_mtime=newest_mt[0] if newest_mt else None,
                newest_mtime_file=newest_mt[1] if newest_mt else None)


def open_paths():
    rc, out, _ = sh(["lsof", "-nP", "-w", "-Fn"], timeout=120)
    if rc not in (0, 1) or not out:
        return None
    return sorted({ln[1:] for ln in out.splitlines() if ln.startswith("n/")})


def opened_under(snapshot, path):
    """lsof 报的是物理路径（/tmp → /private/tmp）：逻辑路径和物理路径各查一次。"""
    if snapshot is None:
        return None
    hits = []
    for q in dict.fromkeys([path, os.path.realpath(path)]):
        i = bisect.bisect_left(snapshot, q)
        pre = q.rstrip("/") + "/"
        while i < len(snapshot) and (snapshot[i] == q or snapshot[i].startswith(pre)):
            hits.append(snapshot[i]); i += 1
    return hits


def path_keys(item, top):
    p = item["path"]
    keys = {p, tilde(p), os.path.realpath(p)}
    if triage.COWORK and under(p, triage.COWORK) and p != triage.COWORK:
        keys.add(os.path.relpath(p, triage.COWORK))
    if top and p != top:
        keys.add(os.path.relpath(p, top))
    base = os.path.basename(p.rstrip("/"))
    if base.lower() not in GENERIC and len(base) >= 8:
        keys.add(base)
    return sorted(k for k in keys if k and k != ".")


def multi_search(keys, paths, globs=(), timeout=300, max_filesize=None, no_ignore=True):
    """先 rg --null -o -F 精确收集每个文件命中的字面串，再仅对命中文件的相关超集/子集关键字做边界核对
    （既避免 rg -o 短键吞长键，又避免对全量未命中键重复扫几千个文件）。rg 超时、不存在或报错返回 None。"""
    keys = sorted({k for k in keys if k})
    if not keys or not paths:
        return {}
    fd, pf = tempfile.mkstemp(prefix="quick-act-keys-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(keys) + "\n")
    cmd = ["rg", "--null", "-o", "-F", "-H", "--no-line-number", "--hidden", "-g", "!.git", "-g", "!node_modules"]
    if no_ignore:
        cmd.append("--no-ignore")
    if max_filesize:
        cmd += ["--max-filesize", max_filesize]
    for g in globs:
        cmd += ["-g", g]
    try:
        rc, out, err = sh(cmd + ["-f", pf, "--"] + list(paths), timeout=timeout)
    finally:
        os.remove(pf)
    if rc in (124, 127) or rc not in (0, 1) and not out:
        return None
    raw_by_file = {}
    all_raw = set()
    for ln in out.splitlines():
        fpath, sep, rk = ln.partition("\0")
        if sep and fpath:
            raw_by_file.setdefault(fpath, set()).add(rk)
            all_raw.add(rk)
    rk_to_keys = {rk: [k for k in keys if rk in k or k in rk] for rk in all_raw}
    hits = {}
    for f, rks in raw_by_file.items():
        cands = {k for rk in rks for k in rk_to_keys.get(rk, ())}
        for k in mentions(f, cands):
            hits.setdefault(k, set()).add(f)
    return hits


NAME_BYTE = re.compile(rb"[A-Za-z0-9_.\-]")


def mentions(f, keys):
    """文件里按“路径边界”出现了哪些关键字：`…/proc/out` 不算出现在 `…/proc/out2/…` 里。逐块读，C 层子串预过滤后才跑正则。"""
    bkeys = [(k, k.encode("utf-8")) for k in keys if k]
    if not bkeys:
        return set()
    rx = [(k, b, re.compile(rb"(?<![A-Za-z0-9_.\-])" + re.escape(b) + rb"(?![A-Za-z0-9_.\-])")) for k, b in bkeys]
    width = max(len(b) for _, b in bkeys) + 1
    found, tail = set(), b""
    try:
        with open(f, "rb") as fh:
            while True:
                chunk = fh.read(8 << 20)
                if not chunk:
                    break
                buf = tail + chunk
                for k, b, r in rx:
                    if k not in found and b in buf and r.search(buf):
                        found.add(k)
                if len(found) == len(rx):
                    break
                tail = buf[-width:]
    except OSError:
        return set()
    return found


def code_refs(item, keys, cache):
    """代码与配置里提到该路径（绝对、~、工作区相对、仓库相对）。不算目标内部自己引用自己。"""
    if cache is None:
        return None
    pkeys = [k for k in keys if "/" in k]   # 只按路径搜，目录名单独太容易误中
    return sorted({f for k in pkeys for f in cache.get(k, ()) if not under(f, item["path"])})


def scan_dirs(item, top):
    """要看“有没有已跟踪脚本 / 近期提交”的目录：目标本身（或所在目录）和上一级，都限在仓库内且不是仓库根。"""
    d = item["path"] if os.path.isdir(item["path"]) and not os.path.islink(item["path"]) else os.path.dirname(item["path"])
    out = []
    for x in (d, os.path.dirname(d)):
        if top and under(x, top) and x != top:
            out.append(os.path.relpath(x, top))
    return list(dict.fromkeys(out))


def _query_sorted_prefix(sorted_files, rel):
    exact = rel.rstrip("/")
    if exact in ("", "."):
        return list(sorted_files)
    out = []
    i = bisect.bisect_left(sorted_files, exact)
    if i < len(sorted_files) and sorted_files[i] == exact:
        out.append(sorted_files[i])
    pre = exact + "/"
    j = bisect.bisect_left(sorted_files, pre)
    while j < len(sorted_files) and sorted_files[j].startswith(pre):
        out.append(sorted_files[j])
        j += 1
    return out


def _prefetch_repo_commits(top, days):
    rc, out, _ = sh([
        "git", "-C", top, "-c", "core.quotepath=false", "log", "--all",
        "--since=%d.days" % days, "--name-only", "-z",
        "--format=__COMMIT__%h %ad %s", "--date=format:%m-%d %H:%M",
    ], timeout=180)
    if rc != 0:
        return None
    headers, pairs = [], []
    cur_idx = -1
    for tok in out.split("\0"):
        if not tok:
            continue
        clean = tok.lstrip("\n")
        if clean.startswith("__COMMIT__"):
            cur_idx = len(headers)
            headers.append(clean[len("__COMMIT__"):].strip())
        elif cur_idx >= 0 and clean:
            pairs.append((clean, cur_idx))
    pairs.sort(key=lambda x: x[0])
    return headers, [p[0] for p in pairs], [p[1] for p in pairs]


def git_tracked_in(top, rel, env=None):
    if env is not None and top in env.repo_tracked:
        cached = env.repo_tracked[top]
        return None if cached is None else _query_sorted_prefix(cached, rel)
    return git_list(top, rel)


def tracked_code(item, top, env=None):
    """已跟踪的脚本：未跟踪文件可能是它们的输入或证据。git 出错返回 None。"""
    if not top:
        return []
    found = []
    for rel in scan_dirs(item, top):
        got = git_tracked_in(top, rel, env)
        if got is None:
            return None
        found += [f for f in got if f.endswith(CODE_EXT)]
    return list(dict.fromkeys(found))[:20]


def recent_commits(item, top, days=7, env=None):
    """目标及上一级目录近 days 天的提交（所有分支）。git 出错返回 None。"""
    if not top:
        return []
    if env is not None and top in env.repo_commits and days == env.commit_days:
        cached = env.repo_commits[top]
        if cached is None:
            return None
        headers, files_sorted, idxs = cached
        out_all = []
        for rel in scan_dirs(item, top):
            exact = rel.rstrip("/")
            matched = set()
            if exact in ("", "."):
                matched.update(idxs)
            else:
                i = bisect.bisect_left(files_sorted, exact)
                while i < len(files_sorted) and files_sorted[i] == exact:
                    matched.add(idxs[i]); i += 1
                pre = exact + "/"
                j = bisect.bisect_left(files_sorted, pre)
                while j < len(files_sorted) and files_sorted[j].startswith(pre):
                    matched.add(idxs[j]); j += 1
            for ci in sorted(matched)[:3]:
                if headers[ci] not in out_all:
                    out_all.append(headers[ci])
        return out_all
    out_all = []
    for rel in scan_dirs(item, top):
        rc, out, _ = sh(["git", "-C", top, "log", "--all", "--since=%d.days" % days, "--format=%h %ad %s",
                         "--date=format:%m-%d %H:%M", "-3", "--", rel], timeout=90)
        if rc != 0:
            return None
        out_all += [ln for ln in out.splitlines() if ln.strip()]
    return list(dict.fromkeys(out_all))


def self_marker(user_text):
    """本会话的对话记录里一定有用户原话；取最长的一段不含引号/换行的片段作标记（太短则不自动识别）。"""
    parts = [s.strip() for s in re.split(r'["\\\n]', user_text)]
    best = max(parts, key=len) if parts else ""
    return best if len(best) >= MIN_MARKER else None


def recent_transcripts(hours):
    cut = time.time() - hours * 3600
    out = []
    for g in TRANSCRIPT_GLOBS:
        for f in glob.glob(g, recursive=True):
            try:
                if os.path.isfile(f) and os.path.getmtime(f) >= cut:
                    out.append(f)
            except OSError:
                pass
    return sorted(set(out))


UUID_RX = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
INVOKED_CID_RX = re.compile(rb'"conversationId"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"', re.I)


def antigravity_session_id(f):
    if "/antigravity-cli/conversations/" in f:
        m = UUID_RX.search(os.path.basename(f))
        return m.group(1).lower() if m else None
    m = re.search(r"/antigravity-cli/brain/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)", f, re.I)
    return m.group(1).lower() if m else None


def session_key(f):
    cid = antigravity_session_id(f)
    return ("ag", cid) if cid else ("file", f)


def ag_invoked_subagents(files):
    """从 Antigravity transcript*.jsonl 里提取经 invoke_subagent 派生的子会话 UUID 集合。"""
    subs = set()
    for f in files:
        if not (antigravity_session_id(f) and f.endswith(".jsonl")):
            continue
        try:
            if os.path.getsize(f) > 64 * 1024 * 1024:
                continue
            with open(f, "rb") as fh:
                while True:
                    chunk = fh.read(8 << 20)
                    if not chunk:
                        break
                    for m in INVOKED_CID_RX.finditer(chunk):
                        subs.add(m.group(1).decode("ascii").lower())
        except OSError:
            continue
    return subs


def is_subagent(f):
    return "/subagents/" in f


def self_transcripts(files, user_text, extra):
    """返回 (本会话记录集合, 问题说明或 None)。
    自动识别：含原话标记的主会话记录按会话归并后恰好 1 个（多个说明原话被转给了别的会话，不能猜）。
    手工指定的 --self-transcript 必须真的含有原话标记或原话开头；同一 Antigravity 会话的伴生文件与派生子 Agent 一并归入本会话。"""
    marker = self_marker(user_text)
    mine, problem = set(), None
    for x in extra or []:
        x = lexical(x)
        probe = marker or user_text.strip()[:8]
        try:
            ok = probe and probe.encode() in open(x, "rb").read()
        except OSError:
            ok = False
        if not ok:
            return set(), "指定的本会话记录 %s 里找不到用户原话" % tilde(x)
        mine.add(x)
    if not mine:
        if not marker:
            return set(), "原话太短，无法自动识别本会话记录；请用 --self-transcript 指明"
        hits = multi_search([marker], files, no_ignore=True, max_filesize="64M")
        if hits is None:
            return set(), "识别本会话记录时搜索失败"
        main = sorted(f for f in hits.get(marker, ()) if not is_subagent(f))
        distinct = {session_key(f) for f in main}
        if len(distinct) > 1:
            return set(), "原话出现在 %d 个会话记录里（如 %s），无法判断哪个是本会话；请用 --self-transcript 指明" % (
                len(distinct), tilde(main[0]))
        mine = set(main)
    sids = {os.path.splitext(os.path.basename(f))[0] for f in mine if f.endswith(".jsonl") and not antigravity_session_id(f)}
    ag_cids = {antigravity_session_id(f) for f in mine if antigravity_session_id(f)}
    if ag_cids:
        ag_siblings = {f for f in files if antigravity_session_id(f) in ag_cids}
        sub_cids = ag_invoked_subagents(mine | ag_siblings)
        ag_cids |= sub_cids
    expanded = {
        f for f in files
        if any("/%s/subagents/" % s in f for s in sids) or (ag_cids and antigravity_session_id(f) in ag_cids)
    }
    return mine | expanded, problem


def run_self_inputs(plan):
    """run 时交给 self_transcripts() 核对的本会话记录。
    plan 里存的是识别结果：含原话的主会话记录，加上按会话号补进来的子 Agent 记录。子 Agent 记录里没有用户原话，
    原样当成手工指定的记录再核对一遍必然失败，整批拒绝执行。所以只传主记录；它名下的子 Agent 记录由
    self_transcripts() 按会话号重新补回，plan 之后新派的子 Agent 也算本会话。
    不属于列表里任何主记录的子 Agent 记录照旧传入核对（必须含原话），不因为“是子 Agent”就放行。"""
    listed = list(plan.get("self_transcripts") or ())
    mains = [x for x in listed if not is_subagent(x)]
    sids = {os.path.splitext(os.path.basename(x))[0] for x in mains if x.endswith(".jsonl") and not antigravity_session_id(x)}
    # Antigravity：识别哪几个是根主会话（剔除被主会话派生的子会话及同会话伴生的 .db/.db-wal，优先保留主 transcript*.jsonl）
    ag_sub_cids = ag_invoked_subagents(mains)
    ag_by_cid = {}
    non_ag_or_keep = []
    for x in listed:
        if is_subagent(x) and any("/%s/subagents/" % s in x for s in sids):
            continue
        cid = antigravity_session_id(x)
        if cid:
            if cid not in ag_sub_cids:
                ag_by_cid.setdefault(cid, []).append(x)
        else:
            non_ag_or_keep.append(x)
    for cid, cfiles in ag_by_cid.items():
        # 优先选主 transcript.jsonl，避免 .db / .db-wal 无原话明文而失败
        preferred = sorted(cfiles, key=lambda p: (0 if os.path.basename(p) == "transcript.jsonl" else 1 if p.endswith(".jsonl") else 2, p))
        non_ag_or_keep.append(preferred[0])
    return non_ag_or_keep


def transcript_hits(keys, cache):
    if cache is None:
        return None
    return sorted({f for k in keys for f in cache.get(k, ())})


BRACE_RX = re.compile(r"([^\s,;，；、()（）{}<>\"'\n：+]+)\{([^{}]+)\}([^\s,;，；、()（）{}<>\"'\n：+]*)")


def normalize_user_text(user_text):
    """合并终端粘贴产生的软换行，展开花括号路径组 {a,b,c}，并剔除括号内明确写“保留/避开…”的片段。"""
    if not user_text:
        return ""
    joined = re.sub(r"([/\-.,{_0-9A-Za-z\u4e00-\u9fff])\r?\n[ \t]*", r"\1", user_text)
    cleaned = re.sub(r"[（(]\s*(?:保留|避开)[^）)]*[）)]", " ", joined)
    extras = []
    cur = cleaned
    for _ in range(3):
        next_parts = []
        changed = False
        for m in BRACE_RX.finditer(cur):
            changed = True
            pre, body, suf = m.group(1), m.group(2), m.group(3)
            for opt in body.split(","):
                opt = opt.strip()
                if not opt:
                    continue
                cand = pre + opt + suf
                extras.append(cand)
                next_parts.append(cand)
                if "/" in pre and opt.endswith("-") and not suf:
                    extras.append(cand + "*")
        if not changed:
            break
        cur = " ".join(next_parts)
    return cleaned + ("\n" + "\n".join(extras) if extras else "")


def _path_forms(p, top):
    p = p.rstrip("/") or p
    forms = [p]
    tp = tilde(p)
    forms.append(tp)
    if tp.startswith("~/") and "/" in tp[2:]:
        rel_home = tp[2:]
        forms.append("/" + rel_home)
        if rel_home.startswith(".") and "/" in rel_home:
            forms.append(rel_home.lstrip("."))
    if triage.COWORK and under(p, triage.COWORK) and p != triage.COWORK:
        forms.append(os.path.relpath(p, triage.COWORK))
    if top and under(p, top) and p != top:
        forms.append(os.path.relpath(p, top))
    return list(dict.fromkeys(f for f in forms if f and "/" in f))


def _exact_path_hit(norm, forms, allow_file_wildcard=False):
    right = r"(?!(?:[A-Za-z0-9_.\-]|/(?!\.[A-Za-z0-9]+|\*)))" if allow_file_wildcard else r"(?![A-Za-z0-9_.\-/])"
    return any(re.search(r"(?<![A-Za-z0-9_.\-])" + re.escape(f) + right, norm) for f in forms)


def _eligible_ancestor(anc):
    """位于 HOME 或工作区根至少第 2 层的祖先目录（防止点名顶层目录就放行其下全部子目录）。"""
    for root in (HOME, triage.COWORK):
        if root and under(anc, root):
            rel = os.path.relpath(anc, root)
            if rel != "." and "/" in rel:
                return True
    if not under(anc, HOME) and (not triage.COWORK or not under(anc, triage.COWORK)):
        parts = [s for s in anc.split("/") if s]
        return len(parts) >= 2
    return False


def named(item, top, user_text):
    """用户点名：原话（含折行/花括号展开及剥离 ~ 形式）里写出完整路径（右侧禁接 /，防子路径放行父目录）、
    >=2 层且明确带 * 的目录通配、已点名深层祖先覆盖子集、同父上下文短目录名，或不少于 8 个字的完整目录名且前后不连着别的字母数字。"""
    norm = normalize_user_text(user_text)
    if not norm:
        return False
    p = item["path"].rstrip("/") or item["path"]
    allow_fw = item.get("scope") == "files"
    if _exact_path_hit(norm, _path_forms(p, top), allow_file_wildcard=allow_fw):
        return True
    for pat in dict.fromkeys(re.findall(r"(?:~/|/)?[^\s,;，；、()（）{}<>\"'\n]*\*[^\s,;，；、()（）{}<>\"'\n]*", norm)):
        pat_clean = pat.rstrip("/")
        if pat_clean.count("/") < 1 and not (pat_clean.startswith("~/") or pat_clean.startswith("/")):
            continue
        seg_n = len([s for s in pat_clean.split("/") if s and s != "~"])
        if seg_n < 2:
            continue
        cands = [pat_clean]
        if pat_clean.startswith("~/"):
            cands.append(HOME + pat_clean[1:])
        elif pat_clean.startswith("/") and not pat_clean.startswith(HOME + "/"):
            cands.append(HOME + pat_clean)
        elif triage.COWORK:
            cands.append(os.path.join(triage.COWORK, pat_clean))
        for cand in cands:
            rx_str = "^" + r"[^/]*".join(re.escape(part) for part in cand.split("*")) + "$"
            if re.fullmatch(rx_str, p):
                return True
    base = os.path.basename(p)
    # 父目录（须至少位于第 2 层）获严格点名时覆盖子集；或 祖先/base 简写精确匹配
    anc = os.path.dirname(p)
    while anc and anc != "/" and _eligible_ancestor(anc):
        anc_forms = _path_forms(anc, top)
        if _exact_path_hit(norm, anc_forms, allow_file_wildcard=False):
            return True
        if base and _exact_path_hit(norm, [af + "/" + base for af in anc_forms], allow_file_wildcard=False):
            return True
        anc = os.path.dirname(anc)
    # 同父上下文短目录名匹配（父路径可在后续接子路径，要求 base 作为独立词出现）
    parent = os.path.dirname(p)
    if parent and parent != "/":
        p_forms = _path_forms(parent, top)
        if any(re.search(r"(?<![A-Za-z0-9_.\-])" + re.escape(pf) + r"(?![A-Za-z0-9_.\-])", norm) for pf in p_forms):
            if base and re.search(r"(?<![A-Za-z0-9_.\-/])" + re.escape(base) + r"(?![A-Za-z0-9_.\-/])", norm):
                return True
    if len(base) < 8:
        return False
    return re.search(r"(?<![A-Za-z0-9_.\-])" + re.escape(base) + r"(?![A-Za-z0-9_.\-])", norm) is not None


# ---------- 盘外备份适配器 ----------
def adapter_cmd():
    a = (triage.LOCAL.get("offdisk_backup") or {}).get("adapter")
    if not a:
        return None
    return [os.path.expanduser(x) for x in a]


def adapter_call(action, paths, run_dir, sample=0):
    cmd = adapter_cmd()
    if not cmd:
        return None, "未配置盘外备份适配器（triage-local.json 的 offdisk_backup.adapter）"
    pf = os.path.join(run_dir, "adapter-%s-%d.json" % (action, os.getpid()))
    json.dump(paths, open(pf, "w", encoding="utf-8"), ensure_ascii=False)
    args = cmd + [action, "--paths-file", pf] + (["--sample", str(sample)] if action == "check" else [])
    rc, out, err = sh(args, timeout=24 * 3600 if action == "upload" else 3600)
    try:
        os.remove(pf)
    except OSError:
        pass
    if rc != 0:
        return None, "适配器 %s 失败：%s" % (action, (err or out).strip()[-300:])
    try:
        res = json.loads(out)
    except ValueError:
        return None, "适配器输出不是 JSON：%s" % out[-200:]
    if not isinstance(res, dict):
        return None, "适配器输出格式不对"
    return res, None


def backup_verify(files, run_dir, sample, upload):
    """返回 (是否通过, 说明, 恢复提示)。必须每个文件都在 ok 里 + 至少抽样回读 1 个且全部一致才算通过。"""
    if not files:
        return True, "无普通文件需要核对", None
    sample = max(1, int(sample))
    want = set(files)

    def missing(res):
        return sorted(want - set(res.get("ok") or []))
    res, err = adapter_call("check", files, run_dir, sample)
    if err:
        return False, err, None
    bad = missing(res)
    if bad and upload:
        up, err = adapter_call("upload", bad, run_dir)
        if err:
            return False, err, None
        res, err = adapter_call("check", files, run_dir, sample)
        if err:
            return False, err, None
        bad = missing(res)
    if bad:
        return False, "%d 个文件没有确认的盘外备份（如 %s）" % (len(bad), tilde(bad[0])), None
    smp = [s for s in res.get("sample") or [] if s.get("path") in want]
    if not smp:
        return False, "适配器没有做抽样回读", None
    worse = [s for s in smp if not s.get("ok")]
    if worse:
        return False, "抽样回读 %d 个不一致（如 %s）" % (len(worse), tilde(worse[0].get("path", "?"))), None
    return True, "盘外备份核对：%d 个文件台账一致，抽样回读 %d 个一致" % (len(files), len(smp)), res.get("restore_hint")


# ---------- 判定 ----------
class Env:
    def __init__(self, user_text, self_extra=(), activity_hours=72, commit_days=7):
        self.user_text = user_text
        self.profile = protect.load_profile()
        self.procs = triage.running_apps()
        self.activity_hours = activity_hours
        self.transcripts = recent_transcripts(activity_hours)
        self.mine, self.self_problem = self_transcripts(self.transcripts, user_text, self_extra)
        self.commit_days = commit_days
        self.tops, self.keys = {}, {}
        self.repo_tracked, self.repo_commits = {}, {}
        self.lsof, self.lsof_at = None, 0.0
        self.refresh_lsof(force=True)

    def refresh_lsof(self, force=True, min_interval=5.0):
        if not force and self.lsof is not None and (time.monotonic() - self.lsof_at) < min_interval:
            return
        self.lsof = open_paths()
        self.lsof_at = time.monotonic()

    def prefetch(self, items):
        """所有条目的引用与 Git 检查合成批量查询：文档台账、代码配置、其他会话对话记录、仓库 tracked 与近期提交。"""
        for it in items:
            if os.path.lexists(it["path"]):
                self.tops[it["id"]] = git_top(it["path"])
                self.keys[it["id"]] = path_keys(it, self.tops[it["id"]])
        allk = {k for ks in self.keys.values() for k in ks}
        roots = [r for r in dict.fromkeys([triage.COWORK if triage.COWORK and os.path.isdir(triage.COWORK) else None,
                                           *self.tops.values()]) if r]
        self.code = multi_search({k for k in allk if "/" in k}, roots, CODE_GLOBS, max_filesize="8M")
        others = [f for f in self.transcripts if f not in self.mine]
        self.talk = multi_search(allk, others, max_filesize="64M") if not self.self_problem else None
        triage.prefetch_refs([it["path"] for it in items if triage.COWORK and under(it["path"], triage.COWORK)])
        for top in sorted({t for t in self.tops.values() if t}):
            if top not in self.repo_tracked:
                got = git_list(top, ".")
                self.repo_tracked[top] = sorted(got) if got is not None else None
            if top not in self.repo_commits:
                self.repo_commits[top] = _prefetch_repo_commits(top, self.commit_days)


def judge(item, env, run_dir, upload=False, sample=8, planned=None):
    """返回 {status, method, refusals, notes, targets, ...}。planned 给出 plan 时的目标身份（run 时比对）。"""
    p, refuse, notes, block = item["path"], [], [], []
    kind = effective_kind(item)
    res = {"id": item["id"], "path": p, "scope": item["scope"], "kind": kind, "declared_kind": item["kind"],
           "reason": item["reason"]}
    if not os.path.lexists(p):
        return dict(res, status="absent", refusals=["路径已不存在"], notes=[], targets=[])
    if kind != item["kind"]:
        notes.append("不在已登记的可再生缓存里，按过程件处理（只隔离不删）；确属缓存请登记到本机配置 extra_tier1")
        if item["scope"] in ("files", "untracked"):
            refuse.append("未登记的“可再生”不能按文件删：登记到 extra_tier1，或先备份后标 backed_up")
    if os.path.islink(p):
        refuse.append("路径本身是符号链接")
    if QUARANTINE_RX.search(p):
        refuse.append("这是隔离区里的东西：用 purge 处理")
    for root, label in ((HOME, "家目录"), (triage.COWORK, "工作区")):
        if root and under(p, root) and len(os.path.relpath(p, root).split("/")) < 2:
            refuse.append("%s下第一层目录不整体处理（%s）" % (label, tilde(p)))
    why = protect.protected_reason(p, env.profile)
    if why or protect.protection_kind(p, env.profile) == "ancestor":
        refuse.append("保护名单：%s" % (why or "包含受保护路径"))
    # 冷存例外（窄）：冷存批次（kind=backed_up 且带 cold_batch）只能越过用户自己在 hard_keep_extra 里加的保护词，
    # 且用户原话必须写出该路径；内置硬保护（.git、部署槽、/Movies/ 等）不例外。盘外完整性仍由适配器逐个核对。
    if item.get("cold_batch"):
        row = {"abs": p}
        if not ConsumerChecks(triage.LOCAL.get("cold_storage") or {}).permits(row, item.get("files") if item["scope"] == "files" else None):
            return dict(res, status="refused", refusals=[row["local_action"]], notes=[], targets=[],
                        reverse_symlinks=row["reverse_symlinks"])
    user_hard = tuple(triage.LOCAL.get("hard_keep_extra") or ())
    cold_named = kind == "backed_up" and bool(item.get("cold_batch")) and named(item, None, env.user_text)
    for frag in triage.HARD_KEEP:
        if frag in p and not (frag == "cleanup-archive" and kind == "backed_up"):
            if cold_named and frag in user_hard:
                notes.append("硬保护词 “%s” 已由用户点名放行：冷存批次 %s，盘外副本由适配器逐个核对" % (frag, item["cold_batch"]))
            else:
                refuse.append("硬保护：路径含 “%s”" % frag)
    if under(p, triage.CHROME_CLONE_ROOT):
        refuse.append("Chrome 代码签名克隆走专用入口 chrome_clones.py")
    if item.get("app") and any(os.path.basename(c) == item["app"] for c in env.procs):
        refuse.append("App 运行中：%s" % item["app"])
    elif not item.get("app") and triage.app_running_for(p, env.procs):
        refuse.append("App 运行中：%s" % triage.app_running_for(p, env.procs))
    top = env.tops.get(item["id"]) if item["id"] in env.tops else git_top(p)
    if top and p == top:
        refuse.append("仓库根目录不整体处理")
    # 需要“用户点名”才放行的信号
    prod = triage.is_production(p) if triage.COWORK else None
    if prod:
        block.append("生产目录（%s）" % tilde(prod))
    if item["scope"] != "untracked" and top and p != top:
        tracked = git_tracked_in(top, os.path.relpath(p, top), env)
        if tracked is None:
            refuse.append("Git 跟踪状态查不到")
        elif tracked:
            refuse.append("含 %d 个 Git 跟踪文件（如 %s）；改用 scope=untracked" % (len(tracked), tracked[0]))
    keys = env.keys.get(item["id"]) or path_keys(item, top)
    doc = triage.reference_hits(p) if triage.COWORK and under(p, triage.COWORK) else []
    if doc is None:
        refuse.append("文档引用检查超时")
    else:
        active, history = triage.split_ref_hits(doc)
        active = [f for f in active if mentions(f, keys)] if active else active   # 仅对消费者过滤前缀误中
        if active:
            block.append("文档/台账引用：%s" % "，".join(tilde(f) for f in active[:3]))
        if history:
            notes.append("历史记录提到它 %d 处（不挡）" % len(history))
    code = code_refs(item, keys, env.code)
    if code is None:
        refuse.append("代码引用检查失败（超时或 rg 出错）")
    else:
        code, code_hist = triage.split_ref_hits(code)   # 往期体检报告、快照、回执只是历史记录，不算消费者
        if kind == "regenerable" and code:
            # 清理工具自身的分类清单与测试不是项目消费者，过滤口径与 triage.tool_state_refs 保持一致。
            code = [f for f in code if not triage.TOOL_REF_SKIP_RX.search(f)]
        if code and kind == "regenerable":
            # 项目可能按绝对路径调用缓存内的固定版本，须先核对脚本引用。
            refuse.append("可再生缓存被代码按路径引用，是工具状态（%s）：删了会重装成别的版本，点名也不放行"
                          % "，".join(tilde(f) for f in code[:3]))
        elif code:
            block.append("代码引用：%s" % "，".join(tilde(f) for f in code[:3]))
        if code_hist:
            notes.append("历史报告/回执里提到它 %d 处（不挡）" % len(code_hist))
    tc = tracked_code(item, top, env)
    if tc is None:
        refuse.append("已跟踪脚本检查失败（git 出错）")
    elif tc:
        block.append("目标或上一级目录有 %d 个已跟踪脚本（如 %s），未跟踪文件可能是它们的输入或证据" % (len(tc), tc[0]))
    commits = recent_commits(item, top, env.commit_days, env)
    if commits is None:
        refuse.append("近期提交检查失败（git 出错）")
    elif commits:
        block.append("近 %d 天有提交碰过这条路径或上一级目录：%s" % (env.commit_days, commits[0]))
    if env.self_problem:
        refuse.append("他人活动检查做不了：" + env.self_problem)
    else:
        th = transcript_hits(keys, env.talk)
        if th is None:
            refuse.append("对话记录检查失败（超时或 rg 出错）")
        elif th:
            activity = "其他 Agent 近 %g 小时的对话提到它（%d 个会话，如 %s）" % (
                env.activity_hours, len(th), tilde(th[0]))
            if kind == "process" and env.isolate_authorized:
                notes.append(activity + "；沿用本轮已授权清单，先隔离")
            else:
                block.append(activity)
    if kind == "verified_obsolete":
        if not DIRECT_DELETE_INTENT_RX.search(env.user_text or ""):
            refuse.append("直接永久删除（verified_obsolete）要求用户原话含明确直删意图（如“直接永久删除”/“不用隔离”）")
        if not named(item, top, env.user_text):
            refuse.append("直接永久删除（verified_obsolete）需用户在原话里明确写出路径或完整目录名“%s”" % os.path.basename(p))
    if block:
        if named(item, top, env.user_text):
            notes.append("用户原话点名了该路径，以下信号已告知仍放行：" + "；".join(block))
        else:
            refuse.append("需用户点名确认（原话里写出路径或完整目录名“%s”）：%s" % (os.path.basename(p), "；".join(block)))
    # 逐个目标：身份、占用、打开、写入
    try:
        tg = targets_of(item)
    except (ValueError, OSError) as e:
        return dict(res, status="refused", refusals=refuse + [str(e)], notes=notes, targets=[])
    targets, files_all, cut = [], [], time.time() - item["quiet_hours"] * 3600
    for t, typ in tg:
        try:
            st = os.lstat(t)
            m = measure(t)
        except OSError as e:
            targets.append({"path": t, "type": typ, "ident": None, "bytes": 0, "files": 0, "skip": "读不到：%s" % e})
            continue
        tr = {"path": t, "type": typ, "ident": [st.st_dev, st.st_ino], "bytes": m["bytes"],
              "shared_bytes": m["shared_bytes"], "files": m["files"], "newest": m["newest"],
              "file_digest": digest(sorted(m["file_list"])), "symlinks": m["symlinks"][:200]}
        handoff_reason = production_handoff.protection(t, getattr(env, "handoffs", []))
        why = [handoff_reason] if handoff_reason else []
        material_reason = session_materials.path_error(t, HOME, item.get("session_materials"))
        if material_reason:
            why.append(material_reason)
        if t != p and protect.protected_reason(t, env.profile):
            why.append("保护名单")
        if m["has_git"]:
            why.append("含 .git（工作树或仓库）")
        if m["has_quarantine"]:
            why.append("里面有隔离中的内容（.待删-日期）")
        op = opened_under(env.lsof, t)
        if op is None:
            why.append("打开文件检查不可用")
        elif op:
            why.append("有 %d 个文件被进程打开" % len(op))
        if m["newest_ts"] and m["newest_ts"] >= cut:
            why.append("%g 小时内有写入（%s）" % (item["quiet_hours"], tilde(m["newest"]["file"])))
        if planned is not None:
            pl = planned.get(t)
            if pl is None:
                why.append("封存后新出现，不在清单里")
            elif pl["ident"] != tr["ident"]:
                why.append("封存后被替换（inode 变了）")
            elif pl["file_digest"] != tr["file_digest"]:
                why.append("封存后文件清单已变化")
        if why:
            tr["skip"] = "；".join(why)
        else:
            files_all += m["file_list"]
            tr["_files"] = m["file_list"]
        targets.append(tr)
    method = {"regenerable": "delete", "backed_up": "delete", "verified_obsolete": "delete", "process": "quarantine"}[kind]
    restore = {
        "regenerable": "无需恢复：可再生（%s）" % item["reason"],
        "verified_obsolete": "不可恢复（用户点名直接永久删除：%s）" % item["reason"],
        "process": None,
        "backed_up": None,
    }[kind]
    if kind == "backed_up" and not refuse:
        ok, msg, hint = backup_verify(files_all, run_dir, sample, upload)
        (notes if ok else refuse).append(msg)
        label = (triage.LOCAL.get("offdisk_backup") or {}).get("label") or "盘外备份"
        restore = hint or "从%s按原路径取回" % label
    status = "refused" if refuse else ("ok" if any("skip" not in t for t in targets) else "nothing")
    return dict(res, status=status, method=method, refusals=refuse, notes=notes, targets=targets,
                bytes=sum(t["bytes"] for t in targets if "skip" not in t),
                skipped=sum(1 for t in targets if "skip" in t), restore=restore, quiet_hours=item["quiet_hours"])


def strip_private(rows):
    return [dict(r, targets=[{k: v for k, v in t.items() if not k.startswith("_")} for t in r["targets"]]) for r in rows]


def seal_of(plan):
    """封存码按内容算：清单、原话、本会话记录、每项可执行目标的路径与身份。run 时重算比对，改 plan 文件即失效。"""
    return digest({"schema": plan["schema"], "items": plan["items"], "user_text": plan["user_text"],
                   **({"isolate_authorized": plan["isolate_authorized"]} if "isolate_authorized" in plan else {}),
                   **({"production_manifests": plan["production_manifests"]} if "production_manifests" in plan else {}),
                   "self_transcripts": plan.get("self_transcripts"),
                   "targets": {r["id"]: [[t["path"], t["ident"], t.get("file_digest")] for t in r["targets"] if "skip" not in t]
                               for r in plan["results"] if r["status"] == "ok"},
                   "methods": {r["id"]: [r.get("kind"), r.get("method")] for r in plan["results"]}})


# ---------- 清理日志与回执 ----------
LOG_HEAD = ("# 清理日志\n\n发现文件不见了，先在这里按路径搜：每次清理的时间、路径、大小、依据、批准来源和恢复命令都记在这里。\n\n"
            "| 时间 | 操作 | 路径 | 大小 | 依据/检查 | 批准来源 | 恢复命令 |\n|---|---|---|---|---|---|---|\n")


def log_row(op, path, size, basis, source, restore):
    f = triage.CLEANUP_LOG
    if not f:
        return
    os.makedirs(os.path.dirname(f), exist_ok=True)
    new = not os.path.exists(f)
    cell = lambda s: str(s).replace("|", "／").replace("\n", " ")
    with open(f, "a", encoding="utf-8") as fh:
        if new:
            fh.write(LOG_HEAD)
        fh.write("| %s | %s | `%s` | %s | %s | %s | %s |\n" % (bjt(), cell(op), cell(tilde(path)), cell(size),
                                                             cell(basis), cell(source), cell(restore)))
        fh.flush()
        os.fsync(fh.fileno())


def receipt(rec_path, row):
    with open(rec_path, "a", encoding="utf-8") as rf:
        rf.write(json.dumps(dict(row, at=now_iso()), ensure_ascii=False) + "\n")
        rf.flush()
        os.fsync(rf.fileno())


def read_receipts(rec_path):
    out = []
    if os.path.exists(rec_path):
        for ln in open(rec_path, encoding="utf-8"):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    return out


def source_of(plan):
    t = plan["user_text"].strip()
    return "用户原话（%s…，全文见 %s）" % (t[:LOG_QUOTE], "quick-plan.json")


def restore_cmd(dst, orig):
    return "[ -e %s ] || mv -n %s %s" % (shlex.quote(orig), shlex.quote(dst), shlex.quote(orig))


# ---------- 子命令 ----------
def summarize(rows):
    lines = []
    for r in rows:
        head = {"ok": "可执行", "refused": "拒绝", "absent": "已不存在", "nothing": "无可执行目标"}[r["status"]]
        act = {"delete": "删除", "quarantine": "改名隔离（暂不释放空间）"}.get(r.get("method"), "")
        lines.append("[%s] %s  %s  %s  %s" % (head, r["id"], act, gib(r.get("bytes")), tilde(r["path"])))
        for x in r.get("refusals", []):
            lines.append("    ✗ " + x)
        for x in r.get("notes", []):
            lines.append("    · " + x)
        if r.get("skipped"):
            lines.append("    · 跳过 %d 个子项：%s" % (r["skipped"], next(t["skip"] for t in r["targets"] if "skip" in t)))
    return "\n".join(lines)


def cmd_plan(a):
    check_local_config()
    prob = ephemeral_run_dir_problem(a.run)
    if prob:
        raise SystemExit(prob)
    items = validate(json.load(open(a.items, encoding="utf-8")))
    if len(a.user_text.strip()) < 4:
        raise SystemExit("必须给出用户的实际原话（--user-text）")
    os.makedirs(a.run, exist_ok=True)
    env = Env(a.user_text, a.self_transcript, a.activity_hours)
    env.isolate_authorized = a.isolate_authorized
    env.handoffs = [production_handoff.read(p) for p in triage.handoff_paths(a.production_manifest)]
    env.prefetch(items)
    rows = [judge(it, env, a.run, upload=a.upload_missing, sample=a.sample) for it in items]
    plan = {"schema": SCHEMA, "created_at": now_iso(), "user_text": a.user_text, "items": items,
            "results": strip_private(rows), "self_transcripts": sorted(env.mine)}
    if env.isolate_authorized:
        plan["isolate_authorized"] = True
    if env.handoffs:
        plan["production_manifests"] = {h["manifest"]: h["sha256"] for h in env.handoffs}
    plan["seal"] = seal = seal_of(plan)
    json.dump(plan, open(os.path.join(a.run, "quick-plan.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    text = summarize(rows)
    ok = [r for r in rows if r["status"] == "ok"]
    tail = "\n可执行 %d 项，删除约 %s，隔离约 %s；拒绝 %d 项。\n封存码 %s（run --seal %s）" % (
        len(ok), gib(sum(r["bytes"] for r in ok if r["method"] == "delete")),
        gib(sum(r["bytes"] for r in ok if r["method"] == "quarantine")),
        sum(r["status"] == "refused" for r in rows), seal, seal)
    open(os.path.join(a.run, "quick-plan.md"), "w", encoding="utf-8").write(text + "\n" + tail + "\n")
    print(text + tail)


def remove(t):
    if t["type"] == "dir":
        shutil.rmtree(t["path"])   # rmtree 不跟随符号链接（macOS 上走 fd 实现）；目标本身是链接时 type=file，只删链接
    else:
        os.unlink(t["path"])


def final_check(t, planned_t, quiet_hours, lsof):
    """删每个目标前最后一次复核：身份、写入、打开、文件清单与核对时一致。返回问题或 None。"""
    try:
        st = os.lstat(t["path"])
    except OSError as e:
        return "已不存在：%s" % e
    if [st.st_dev, st.st_ino] != planned_t["ident"]:
        return "被替换（inode 变了）"
    m = measure(t["path"])
    if m["newest_ts"] and m["newest_ts"] >= time.time() - quiet_hours * 3600:
        return "刚有写入（%s）" % tilde(m["newest"]["file"])
    if m["has_git"] or m["has_quarantine"]:
        return "出现了 .git 或隔离项"
    if digest(sorted(m["file_list"])) != t.get("file_digest"):
        return "文件清单与复核时不同"
    op = opened_under(lsof, t["path"])
    if op is None or op:
        return "打开文件检查不可用" if op is None else "有文件被进程打开"
    return None


def cmd_run(a):
    check_local_config()
    plan = json.load(open(os.path.join(a.run, "quick-plan.json"), encoding="utf-8"))
    if plan.get("schema") != SCHEMA:
        raise SystemExit("计划格式不对：先用当前版本重新 plan")
    if a.seal != plan.get("seal") or seal_of(plan) != a.seal:
        raise SystemExit("封存码不符或计划文件被改过：重新 plan，并用它打印的封存码")
    env = Env(plan["user_text"], run_self_inputs(plan), a.activity_hours)
    env.isolate_authorized = plan.get("isolate_authorized") is True
    manifests = plan.get("production_manifests", {})
    def load_handoffs():
        reports = [production_handoff.read(p) for p in triage.handoff_paths(manifests)]
        if any(h["manifest"] in manifests and manifests[h["manifest"]] != h["sha256"] for h in reports):
            raise SystemExit("生产交付清单已变更，请重新 plan")
        return reports

    env.handoffs = load_handoffs()
    rec_path = os.path.join(a.run, "quick-receipt.jsonl")
    done_before = {r["target"] for r in read_receipts(rec_path) if r.get("status") in ("deleted", "quarantined")}
    planned_rows = {r["id"]: r for r in plan["results"] if r["status"] == "ok"}
    env.prefetch([it for it in plan["items"] if it["id"] in planned_rows])
    free0, totals = free_bytes(), {"deleted": 0, "quarantined": 0, "skipped": 0, "failed": 0, "bytes": 0}
    src = source_of(plan)
    receipt(rec_path, {"status": "run_started", "seal": a.seal, "pilot": bool(a.pilot)})
    for it in plan["items"]:
        if it["id"] not in planned_rows:
            original = next(row for row in plan["results"] if row["id"] == it["id"])
            why = original.get("refusals") or [t["skip"] for t in original["targets"] if "skip" in t] or [original["status"]]
            receipt(rec_path, {"id": it["id"], "target": it["path"], "status": "skipped", "why": why})
            totals["skipped"] += 1
            continue
        planned = {t["path"]: t for t in planned_rows[it["id"]]["targets"] if "skip" not in t}
        env.refresh_lsof(force=False)
        r = judge(it, env, a.run, upload=False, sample=a.verify_sample, planned=planned)
        if r["status"] != "ok" or r["method"] != planned_rows[it["id"]]["method"]:
            totals["skipped"] += 1
            why = r.get("refusals") or [t["skip"] for t in r["targets"] if "skip" in t] or [r["status"]]
            receipt(rec_path, {"id": it["id"], "target": it["path"], "status": "refused", "why": why})
            print("✗ %s：执行前复核不通过：%s" % (it["id"], "；".join(why)))
            if a.pilot:
                break
            continue
        go = [t for t in r["targets"] if "skip" not in t and t["path"] not in done_before]
        for target in r["targets"]:
            if target.get("skip") or target["path"] in done_before:
                receipt(rec_path, {"id": it["id"], "target": target["path"], "status": "skipped",
                                   "why": target.get("skip") or "先前批次已完成"})
                totals["skipped"] += 1
        # Record each affected device once. A surviving parent can still be
        # sampled after deletion of the leaf.
        anchors = {}
        for target in go:
            device = target["ident"][0]
            parent = os.path.dirname(target["path"])
            if device not in anchors:
                try:
                    parent_device = os.stat(parent).st_dev
                except OSError:
                    parent_device = None
                anchors[device] = parent if parent_device == device else target["path"]
        starts = [batch_space.sample(anchor) for anchor in anchors.values()]
        batch_file = os.path.join(a.run, "quick-space.jsonl")
        receipt(batch_file, {"status": "batch_started", "id": it["id"], "seal": a.seal, "before": starts})
        restore = r["restore"]
        verb = "删除" if r["method"] == "delete" else "隔离"
        log_row("准备%s（%s）" % (verb, it["scope"]), it["path"], "%s / %d 个目标" % (gib(sum(t["bytes"] for t in go)), len(go)),
                "%s；%s" % (KINDS[r["kind"]], it["reason"]), src, restore or "见完成行")
        freed, n_ok, n_fail, n_late, tag = 0, 0, 0, 0, QUARANTINE_TAG + bjt("%Y%m%d")
        env.refresh_lsof(force=False)
        try:
            for t in go:
                late = production_handoff.protection(t["path"], load_handoffs()) or final_check(t, t, it["quiet_hours"], env.lsof)
                late = late or session_materials.path_error(t["path"], HOME, it.get("session_materials"))
                if late:
                    n_late += 1
                    receipt(rec_path, {"id": it["id"], "target": t["path"], "status": "skipped", "why": late})
                    continue
                receipt(rec_path, {"id": it["id"], "target": t["path"], "status": "intent", "method": r["method"],
                                   "bytes": t["bytes"], "symlinks": t.get("symlinks")})
                try:
                    if r["method"] == "delete":
                        remove(t)
                        rec = {"status": "deleted", "restore": restore}
                    else:
                        dst = t["path"] + tag
                        if os.path.lexists(dst):
                            raise OSError("隔离名已存在：%s" % dst)
                        os.rename(t["path"], dst)
                        rec = {"status": "quarantined", "moved_to": dst, "restore": restore_cmd(dst, t["path"])}
                    freed += t["bytes"]; n_ok += 1
                except OSError as e:
                    rec = {"status": "failed", "why": str(e)[:300]}; n_fail += 1
                if rec["status"] == "quarantined" and "session_materials" in it:
                    rec["session_materials"] = it["session_materials"]
                if rec["status"] == "quarantined" and env.handoffs:
                    rec["production_manifests"] = [h["manifest"] for h in env.handoffs]
                receipt(rec_path, dict(rec, id=it["id"], target=t["path"], bytes=t["bytes"], files=t["files"],
                                       symlinks=t.get("symlinks")))
        finally:
            observations = [batch_space.changes(start, batch_space.sample(start["path"])) for start in starts]
            receipt(batch_file, {"status": "batch_space", "id": it["id"], "seal": a.seal, "method": r["method"],
                               "entry_estimate_bytes": freed if r["method"] == "delete" else 0,
                               "observations": observations, "completed_targets": n_ok})
            for obs in observations:
                print("项目 %s · 条目估算 %s · 卷余量变化 %s · 容器余量变化 %s · 独立归因未知" %
                      (it["id"], gib(freed if r["method"] == "delete" else 0), gib_optional(obs["volume_delta_bytes"]),
                       gib_optional(obs["container_delta_bytes"])))
            if r["method"] == "quarantine":
                restore = "逐项命令见 %s（[ -e 原路径 ] || mv -n 隔离名 原路径）；满 7 天后 purge 才删" % tilde(rec_path)
            log_row("已%s（%d 成功，%d 失败，%d 跳过）" % (verb, n_ok, n_fail, r["skipped"] + n_late),
                    it["path"], gib(freed), "执行前逐项复核通过", src, restore)
        totals["deleted" if r["method"] == "delete" else "quarantined"] += n_ok
        totals["failed"] += n_fail
        totals["skipped"] += n_late
        totals["bytes"] += freed if r["method"] == "delete" else 0
        print("✓ %s：%s %d 个目标，%s%s%s" % (it["id"], verb, n_ok, gib(freed), "，失败 %d" % n_fail if n_fail else "",
                                          "，最后复核跳过 %d" % n_late if n_late else ""))
        if a.pilot:
            print("试点只执行第一项，确认无误后去掉 --pilot 重跑（已完成的目标会跳过）")
            break
    free1 = free_bytes()
    totals["free_delta"] = free1 - free0 if free0 is not None and free1 is not None else None
    totals["entry_estimate_bytes"] = totals["bytes"]
    totals["home_volume_available_before"] = free0
    totals["home_volume_available_after"] = free1
    totals["batch_measurement_file"] = "quick-space.jsonl" if os.path.isfile(os.path.join(a.run, "quick-space.jsonl")) else None
    totals["attributable_release_bytes"] = None
    receipt(rec_path, dict(totals, status="run_finished"))
    print(json.dumps(dict(totals, bytes_h=gib(totals["bytes"]), free_delta_h=gib_optional(totals["free_delta"])), ensure_ascii=False))
    if totals["failed"]:
        raise SystemExit(4)


def _all_receipt_paths():
    roots = [os.path.join(HOME, ".sansheng", "disk-cleanup")]
    if triage.COWORK and os.path.isdir(triage.COWORK):
        roots.append(os.path.join(triage.COWORK, "_workspace", "disk-watch"))
    found = []
    for r in roots:
        if os.path.isdir(r):
            found += glob.glob(os.path.join(r, "**", "quick-receipt.jsonl"), recursive=True)
    return sorted(set(found))


def _scan_orphan_quarantines(dirs, known):
    out = []
    for d in dirs or ():
        root = lexical(d)
        if not os.path.isdir(root):
            continue
        for dp, dns, fns in os.walk(root):
            keep_dns = []
            for name in sorted(dns):
                p = os.path.join(dp, name)
                if QUARANTINE_RX.search(name):
                    if p not in known:
                        out.append(p)
                elif name != ".git":
                    keep_dns.append(name)
            dns[:] = keep_dns
            for name in sorted(fns):
                if QUARANTINE_RX.search(name):
                    p = os.path.join(dp, name)
                    if p not in known:
                        out.append(p)
    return sorted(set(out))


def cmd_purge(a):
    if not a.run and not a.all and not a.orphan_scan:
        raise SystemExit("请指定 --run <目录>、--all 或 --orphan-scan <目录>")
    rec_paths = []
    if a.run:
        rec_paths.append(os.path.join(a.run, "quick-receipt.jsonl"))
    if a.all:
        rec_paths += _all_receipt_paths()
    rec_paths = list(dict.fromkeys(rec_paths))
    if a.orphan_scan and a.days < 7:
        if not a.user_text or ".待删-" not in a.user_text:
            raise SystemExit("对 --orphan-scan 提前清理（--days < 7）必须传入含 “.待删-” 的 --user-text")
    cut = time.time() - a.days * 86400
    lsof = open_paths()
    profile = protect.load_profile()
    known_q = set()
    n = 0
    for rec_path in rec_paths:
        rows = read_receipts(rec_path)
        purged = {r.get("moved_to") for r in rows if r.get("status") == "purged"}
        for r in rows:
            if r.get("status") != "quarantined" or r["moved_to"] in purged:
                continue
            q, qt = r["moved_to"], dt.datetime.fromisoformat(r["at"]).timestamp()
            known_q.add(q)
            if not os.path.lexists(q):
                print("· 已不在隔离位置（可能已恢复）：%s" % tilde(q)); continue
            if protect.protected_reason(q, profile):
                print("· 受保护路径，不动：%s" % tilde(q)); continue
            if qt > cut:
                print("· 隔离未满 %g 天：%s" % (a.days, tilde(q))); continue
            if os.path.lexists(r["target"]):
                print("· 原路径又出现了同名内容，不动：%s" % tilde(r["target"])); continue
            current = [production_handoff.read(p) for p in triage.handoff_paths(r.get("production_manifests", []))]
            reason = (production_handoff.protection(r["target"], current) or
                      production_handoff.protection(q, current, declared_only=True) or
                      session_materials.path_error(q, HOME, r.get("session_materials"), original=r["target"]))
            if reason:
                print("· %s：%s" % (reason, tilde(q)))
                if not a.dry_run:
                    receipt(rec_path, {"status": "purge_skipped", "moved_to": q, "why": reason})
                continue
            m = measure(q)
            if m.get("newest_mtime") and m["newest_mtime"] > qt + 60:
                print("· 隔离后有人在里面写过（%s），不动：%s" % (tilde(m["newest_mtime_file"]), tilde(q))); continue
            op = opened_under(lsof, q)
            if op is None or op:
                print("· %s，不动：%s" % ("打开文件检查不可用" if op is None else "有文件被进程打开", tilde(q))); continue
            if a.dry_run:
                print("预演：将删除 %s" % tilde(q)); continue
            start = batch_space.sample(os.path.dirname(q))
            try:
                (shutil.rmtree if os.path.isdir(q) and not os.path.islink(q) else os.unlink)(q)
                status, why = "purged", None
            except OSError as e:
                status, why = "purge_failed", str(e)[:300]
            finally:
                log_row("隔离期满删除" if not why else "隔离期满删除失败", q, gib(r.get("bytes")),
                        "隔离 %g 天无人恢复、无写入、无占用" % a.days, "quick_act purge", "不可恢复" if not why else why)
            obs = batch_space.changes(start, batch_space.sample(os.path.dirname(q)))
            receipt(rec_path, {"status": status, "moved_to": q, "why": why, "space": obs,
                               "entry_estimate_bytes": m["bytes"] if status == "purged" else None})
            n += status == "purged"
    bjt_tz = dt.timezone(dt.timedelta(hours=8))
    for q in _scan_orphan_quarantines(a.orphan_scan, known_q):
        if os.path.islink(q):
            print("· 符号链接，不动：%s" % tilde(q)); continue
        if protect.protected_reason(q, profile):
            print("· 受保护路径，不动：%s" % tilde(q)); continue
        m_tag = QUARANTINE_RX.search(q)
        ymd = m_tag.group(1) if m_tag else ""
        try:
            q_day_start = dt.datetime.strptime(ymd, "%Y%m%d").replace(tzinfo=bjt_tz).timestamp()
        except ValueError:
            print("· 无法解析隔离日期，不动：%s" % tilde(q)); continue
        if q_day_start > cut:
            print("· 隔离未满 %g 天：%s" % (a.days, tilde(q))); continue
        orig = QUARANTINE_RX.sub("", q)
        current = [production_handoff.read(p) for p in triage.handoff_paths([])]
        reason = (production_handoff.protection(orig, current) or
                  production_handoff.protection(q, current, declared_only=True) or
                  session_materials.path_error(q, HOME, original=orig))
        if reason:
            print("· %s：%s" % (reason, tilde(q))); continue
        if os.path.lexists(orig):
            print("· 原路径仍存在，不动：%s" % tilde(orig)); continue
        m = measure(q)
        if m.get("newest_mtime") and m["newest_mtime"] > q_day_start + 86400:
            print("· 隔离日之后有人在里面写过（%s），不动：%s" % (tilde(m["newest_mtime_file"]), tilde(q))); continue
        op = opened_under(lsof, q)
        if op is None or op:
            print("· %s，不动：%s" % ("打开文件检查不可用" if op is None else "有文件被进程打开", tilde(q))); continue
        if a.dry_run:
            print("预演：将删除孤儿隔离项 %s" % tilde(q)); continue
        try:
            (shutil.rmtree if os.path.isdir(q) and not os.path.islink(q) else os.unlink)(q)
            n += 1
            log_row("隔离期满删除", q, gib(m["bytes"]),
                    "孤儿隔离项（后缀 %s）原路径已不在、无写入、无占用" % ymd, "quick_act purge --orphan-scan", "不可恢复")
        except OSError as e:
            log_row("隔离期满删除失败", q, gib(m["bytes"]),
                    "孤儿隔离项（后缀 %s）" % ymd, "quick_act purge --orphan-scan", str(e)[:300])
    print("隔离期满已删除 %d 项" % n)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("plan")
    p.add_argument("--production-manifest", action="append", default=[], help="项目生产交付清单，可重复")
    p.add_argument("--items", required=True)
    p.add_argument("--user-text", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--isolate-authorized", action="store_true",
                   help="清单已获用户授权时，其他会话提及过程件只记录，仍只隔离")
    p.add_argument("--upload-missing", action="store_true", help="backed_up 项缺备份时先调用适配器补传")
    p.add_argument("--sample", type=int, default=8, help="backed_up 项每项抽样回读个数（至少 1）")
    p.add_argument("--self-transcript", action="append", default=[], help="本会话对话记录（自动识别不到时手动给，必须含用户原话）")
    p.add_argument("--activity-hours", type=float, default=72)
    r = sp.add_parser("run")
    r.add_argument("--run", required=True)
    r.add_argument("--seal", required=True)
    r.add_argument("--pilot", action="store_true")
    r.add_argument("--verify-sample", type=int, default=3)
    r.add_argument("--activity-hours", type=float, default=72)
    q = sp.add_parser("purge")
    q.add_argument("--run", help="单个 run 目录（含 quick-receipt.jsonl）")
    q.add_argument("--all", action="store_true", help="扫描 ~/.sansheng/disk-cleanup 与工作区 _workspace/disk-watch 全部回执")
    q.add_argument("--orphan-scan", action="append", default=[], help="扫描未登记在回执里的 .待删-YYYYMMDD 孤儿隔离项")
    q.add_argument("--user-text", default="", help="提前清理孤儿隔离项（--days < 7）时传入的用户授权原话")
    q.add_argument("--days", type=float, default=7)
    q.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    {"plan": cmd_plan, "run": cmd_run, "purge": cmd_purge}[a.cmd](a)


if __name__ == "__main__":
    main()
