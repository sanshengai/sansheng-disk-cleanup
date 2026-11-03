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
import protect  # noqa: E402
import triage  # noqa: E402

SCHEMA = "quick-act/2"
HOME = protect.HOME
KINDS = {"regenerable": "可再生", "backed_up": "有盘外备份", "process": "过程件（无盘外备份）"}
SCOPES = ("all", "children", "files", "untracked")
SKIP_NAMES = {".DS_Store"}   # 只有它既不算写入、也不进备份核对；*.lock 等是真实内容
CODE_GLOBS = ("*.py", "*.sh", "*.ts", "*.js", "*.mjs", "*.cjs", "*.ps1", "*.psm1", "*.toml", "*.yaml", "*.yml", "*.json")
CODE_EXT = (".py", ".sh", ".ts", ".js", ".mjs", ".cjs", ".ps1", ".psm1")
REGEN_NAMES = {"node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
# 太常见的目录名不单独拿去搜（会被无关文件命中）
GENERIC = {"production", "frames", "cache", "caches", "tmp", "temp", "build", "dist", "out", "output", "outputs",
           "src", "data", "runs", "logs", "assets", "sessions", "conversations", "brain", "images", "videos"}
TRANSCRIPT_GLOBS = (HOME + "/.claude/projects/*/*.jsonl", HOME + "/.claude/projects/*/*/subagents/**/*.jsonl",
                    HOME + "/.codex/sessions/**/*.jsonl", HOME + "/.grok/sessions/**/*.json*",
                    HOME + "/.gemini/antigravity-cli/conversations/**/*")
QUARANTINE_TAG = ".待删-"
QUARANTINE_RX = re.compile(r"\.待删-\d{8}$")
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


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def free_bytes():
    s = os.statvfs(HOME)
    return s.f_bavail * s.f_frsize


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
        q = float(x.get("quiet_hours", 6))
        if q < 1:
            raise ValueError("%s：quiet_hours 不能小于 1" % x["id"])
        y = dict(x, path=lexical(x["path"]), quiet_hours=q)
        if x["scope"] == "files":
            y["files"] = [lexical(f) for f in x["files"]]
            bad = [f for f in y["files"] if not f.startswith(y["path"].rstrip("/") + "/")]
            if bad:
                raise ValueError("%s：files 必须在 path 之内：%s" % (x["id"], bad[0]))
        out.append(y)
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
        return [(os.path.join(p, n), "dir" if isdir(os.path.join(p, n)) else "file")
                for n in sorted(os.listdir(p)) if not QUARANTINE_RX.search(n)]
    if scope == "files":
        return [(f, "file") for f in item["files"] if os.path.lexists(f) and not QUARANTINE_RX.search(f)]
    top = git_top(p)
    if not top:
        raise ValueError("scope=untracked 但路径不在 Git 工作树里")
    rel = os.path.relpath(p, top)
    files = set()
    for extra in (["--others", "--exclude-standard"], ["--others", "--ignored", "--exclude-standard"]):
        got = git_list(top, rel, *extra)
        if got is None:
            raise ValueError("git ls-files 失败")
        files.update(got)
    return [(os.path.join(top, f), "file") for f in sorted(files)
            if os.path.lexists(os.path.join(top, f)) and not any(QUARANTINE_RX.search(x) for x in f.split("/"))]


def measure(path):
    """一次遍历：占用（按 inode 去重、多链接单列）、文件数、最新写入（mtime/ctime 取晚者）、
    是否含 .git 或隔离项、普通文件清单（备份核对用）、符号链接清单（回执里留着，恢复时可重建）。"""
    seen, size, n, newest, files, links, shared = set(), 0, 0, None, [], [], 0
    flags = {"has_git": False, "has_quarantine": False}

    def one(fp, st):
        nonlocal size, n, newest, shared
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
                newest_ts=newest[0] if newest else None)


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
    """先 rg -l 找出含任一关键字的文件，再在 Python 里逐个关键字核对（rg -o 会让短关键字吞掉长关键字）。
    返回 {关键字: {命中文件}}；rg 超时、不存在或报错返回 None（调用方按拒绝处理）。"""
    keys = sorted({k for k in keys if k})
    if not keys or not paths:
        return {}
    fd, pf = tempfile.mkstemp(prefix="quick-act-keys-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(keys) + "\n")
    cmd = ["rg", "-l", "-F", "--hidden", "-g", "!.git", "-g", "!node_modules"]
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
    hits = {}
    for f in out.splitlines():
        for k in mentions(f, keys):
            hits.setdefault(k, set()).add(f)
    return hits


NAME_BYTE = re.compile(rb"[A-Za-z0-9_.\-]")


def mentions(f, keys):
    """文件里按“路径边界”出现了哪些关键字：`…/proc/out` 不算出现在 `…/proc/out2/…` 里。逐块读，不整份载入。"""
    bkeys = [(k, k.encode("utf-8")) for k in keys]
    rx = [(k, re.compile(rb"(?<![A-Za-z0-9_.\-])" + re.escape(b) + rb"(?![A-Za-z0-9_.\-])")) for k, b in bkeys]
    width = max(len(b) for _, b in bkeys) + 1
    found, tail = set(), b""
    try:
        with open(f, "rb") as fh:
            while True:
                chunk = fh.read(8 << 20)
                if not chunk:
                    break
                buf = tail + chunk
                found.update(k for k, r in rx if k not in found and r.search(buf))
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


def tracked_code(item, top):
    """已跟踪的脚本：未跟踪文件可能是它们的输入或证据。git 出错返回 None。"""
    if not top:
        return []
    found = []
    for rel in scan_dirs(item, top):
        got = git_list(top, rel)
        if got is None:
            return None
        found += [f for f in got if f.endswith(CODE_EXT)]
    return list(dict.fromkeys(found))[:20]


def recent_commits(item, top, days=7):
    """目标及上一级目录近 days 天的提交（所有分支）。git 出错返回 None。"""
    if not top:
        return []
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


def is_subagent(f):
    return "/subagents/" in f


def self_transcripts(files, user_text, extra):
    """返回 (本会话记录集合, 问题说明或 None)。
    自动识别：含原话标记的主会话记录恰好 1 个（多个说明原话被转给了别的会话，不能猜）。
    手工指定的 --self-transcript 必须真的含有原话标记或原话开头。"""
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
        hits = multi_search([marker], files, no_ignore=True)
        if hits is None:
            return set(), "识别本会话记录时搜索失败"
        main = sorted(f for f in hits.get(marker, ()) if not is_subagent(f))
        if len(main) > 1:
            return set(), "原话出现在 %d 个会话记录里（如 %s），无法判断哪个是本会话；请用 --self-transcript 指明" % (
                len(main), tilde(main[0]))
        mine = set(main)
    sids = {os.path.splitext(os.path.basename(f))[0] for f in mine if f.endswith(".jsonl")}
    return mine | {f for f in files if any("/%s/subagents/" % s in f for s in sids)}, problem


def transcript_hits(keys, cache):
    if cache is None:
        return None
    return sorted({f for k in keys for f in cache.get(k, ())})


def named(item, top, user_text):
    """用户点名：原话里写出了路径（绝对、~、工作区相对、仓库相对），或不少于 8 个字的完整目录名且前后不连着别的字母数字。"""
    p = item["path"]
    forms = [p, tilde(p)]
    if triage.COWORK and under(p, triage.COWORK) and p != triage.COWORK:
        forms.append(os.path.relpath(p, triage.COWORK))
    if top and under(p, top) and p != top:
        forms.append(os.path.relpath(p, top))
    if any(f and "/" in f and f in user_text for f in forms):
        return True
    base = os.path.basename(p.rstrip("/"))
    if len(base) < 8:
        return False
    return re.search(r"(?<![A-Za-z0-9_.\-])%s(?![A-Za-z0-9_.\-])" % re.escape(base), user_text) is not None


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
        self.refresh_lsof()

    def refresh_lsof(self):
        self.lsof = open_paths()

    def prefetch(self, items):
        """所有条目的引用检查合成三次搜索：文档台账、代码配置（不受 .gitignore 影响）、其他会话的对话记录（不限大小）。"""
        for it in items:
            if os.path.lexists(it["path"]):
                self.tops[it["id"]] = git_top(it["path"])
                self.keys[it["id"]] = path_keys(it, self.tops[it["id"]])
        allk = {k for ks in self.keys.values() for k in ks}
        roots = [r for r in dict.fromkeys([triage.COWORK if triage.COWORK and os.path.isdir(triage.COWORK) else None,
                                           *self.tops.values()]) if r]
        self.code = multi_search({k for k in allk if "/" in k}, roots, CODE_GLOBS, max_filesize="8M")
        others = [f for f in self.transcripts if f not in self.mine]
        self.talk = multi_search(allk, others) if not self.self_problem else None
        triage.prefetch_refs([it["path"] for it in items if triage.COWORK and under(it["path"], triage.COWORK)])


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
    for frag in triage.HARD_KEEP:
        if frag in p and not (frag == "cleanup-archive" and kind == "backed_up"):
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
        tracked = git_list(top, os.path.relpath(p, top))
        if tracked is None:
            refuse.append("Git 跟踪状态查不到")
        elif tracked:
            refuse.append("含 %d 个 Git 跟踪文件（如 %s）；改用 scope=untracked" % (len(tracked), tracked[0]))
    doc = triage.reference_hits(p) if triage.COWORK and under(p, triage.COWORK) else []
    if doc is None:
        refuse.append("文档引用检查超时")
    else:
        doc = [f for f in doc if mentions(f, path_keys(item, top))] if doc else doc   # 去掉只是前缀相同的误中
        active, history = triage.split_ref_hits(doc)
        if active:
            block.append("文档/台账引用：%s" % "，".join(tilde(f) for f in active[:3]))
        if history:
            notes.append("历史记录提到它 %d 处（不挡）" % len(history))
    keys = env.keys.get(item["id"]) or path_keys(item, top)
    code = code_refs(item, keys, env.code)
    if code is None:
        refuse.append("代码引用检查失败（超时或 rg 出错）")
    else:
        code, code_hist = triage.split_ref_hits(code)   # 往期体检报告、快照、回执只是历史记录，不算消费者
        if code:
            block.append("代码引用：%s" % "，".join(tilde(f) for f in code[:3]))
        if code_hist:
            notes.append("历史报告/回执里提到它 %d 处（不挡）" % len(code_hist))
    tc = tracked_code(item, top)
    if tc is None:
        refuse.append("已跟踪脚本检查失败（git 出错）")
    elif tc:
        block.append("目标或上一级目录有 %d 个已跟踪脚本（如 %s），未跟踪文件可能是它们的输入或证据" % (len(tc), tc[0]))
    commits = recent_commits(item, top, env.commit_days)
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
            block.append("其他 Agent 近 %g 小时的对话提到它（%d 个会话，如 %s）" % (env.activity_hours, len(th), tilde(th[0])))
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
        why = []
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
            elif pl != tr["ident"]:
                why.append("封存后被替换（inode 变了）")
        if why:
            tr["skip"] = "；".join(why)
        else:
            files_all += m["file_list"]
            tr["_files"] = m["file_list"]
        targets.append(tr)
    method = {"regenerable": "delete", "backed_up": "delete", "process": "quarantine"}[kind]
    restore = {"regenerable": "无需恢复：可再生（%s）" % item["reason"], "process": None, "backed_up": None}[kind]
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
    items = validate(json.load(open(a.items, encoding="utf-8")))
    if len(a.user_text.strip()) < 4:
        raise SystemExit("必须给出用户的实际原话（--user-text）")
    os.makedirs(a.run, exist_ok=True)
    env = Env(a.user_text, a.self_transcript, a.activity_hours)
    env.prefetch(items)
    rows = [judge(it, env, a.run, upload=a.upload_missing, sample=a.sample) for it in items]
    plan = {"schema": SCHEMA, "created_at": now_iso(), "user_text": a.user_text, "items": items,
            "results": strip_private(rows), "self_transcripts": sorted(env.mine)}
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
    env = Env(plan["user_text"], plan.get("self_transcripts"), a.activity_hours)
    rec_path = os.path.join(a.run, "quick-receipt.jsonl")
    done_before = {r["target"] for r in read_receipts(rec_path) if r.get("status") in ("deleted", "quarantined")}
    planned_rows = {r["id"]: r for r in plan["results"] if r["status"] == "ok"}
    env.prefetch([it for it in plan["items"] if it["id"] in planned_rows])
    free0, totals = free_bytes(), {"deleted": 0, "quarantined": 0, "skipped": 0, "failed": 0, "bytes": 0}
    src = source_of(plan)
    receipt(rec_path, {"status": "run_started", "seal": a.seal, "pilot": bool(a.pilot)})
    for it in plan["items"]:
        if it["id"] not in planned_rows:
            continue
        planned = {t["path"]: t["ident"] for t in planned_rows[it["id"]]["targets"] if "skip" not in t}
        env.refresh_lsof()
        r = judge(it, env, a.run, upload=False, sample=a.verify_sample, planned=planned)
        if r["status"] != "ok" or r["method"] != planned_rows[it["id"]]["method"]:
            totals["skipped"] += 1
            why = r.get("refusals") or [r["status"]]
            receipt(rec_path, {"id": it["id"], "target": it["path"], "status": "refused", "why": why})
            print("✗ %s：执行前复核不通过：%s" % (it["id"], "；".join(why)))
            if a.pilot:
                break
            continue
        go = [t for t in r["targets"] if "skip" not in t and t["path"] not in done_before]
        restore = r["restore"]
        verb = "删除" if r["method"] == "delete" else "隔离"
        log_row("准备%s（%s）" % (verb, it["scope"]), it["path"], "%s / %d 个目标" % (gib(sum(t["bytes"] for t in go)), len(go)),
                "%s；%s" % (KINDS[r["kind"]], it["reason"]), src, restore or "见完成行")
        freed, n_ok, n_fail, n_late, tag = 0, 0, 0, 0, QUARANTINE_TAG + bjt("%Y%m%d")
        env.refresh_lsof()
        try:
            for t in go:
                late = final_check(t, t, it["quiet_hours"], env.lsof)
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
                receipt(rec_path, dict(rec, id=it["id"], target=t["path"], bytes=t["bytes"], files=t["files"],
                                       symlinks=t.get("symlinks")))
        finally:
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
    totals["free_delta"] = free_bytes() - free0
    receipt(rec_path, dict(totals, status="run_finished"))
    print(json.dumps(dict(totals, bytes_h=gib(totals["bytes"]), free_delta_h=gib(totals["free_delta"])), ensure_ascii=False))
    if totals["failed"]:
        raise SystemExit(4)


def cmd_purge(a):
    rec_path = os.path.join(a.run, "quick-receipt.jsonl")
    rows = read_receipts(rec_path)
    purged = {r.get("moved_to") for r in rows if r.get("status") == "purged"}
    cut = time.time() - a.days * 86400
    lsof = open_paths()
    n = 0
    for r in rows:
        if r.get("status") != "quarantined" or r["moved_to"] in purged:
            continue
        q, qt = r["moved_to"], dt.datetime.fromisoformat(r["at"]).timestamp()
        if not os.path.lexists(q):
            print("· 已不在隔离位置（可能已恢复）：%s" % tilde(q)); continue
        if qt > cut:
            print("· 隔离未满 %g 天：%s" % (a.days, tilde(q))); continue
        if os.path.lexists(r["target"]):
            print("· 原路径又出现了同名内容，不动：%s" % tilde(r["target"])); continue
        m = measure(q)
        if m["newest_ts"] and m["newest_ts"] > qt + 60:
            print("· 隔离后有人在里面写过（%s），不动：%s" % (tilde(m["newest"]["file"]), tilde(q))); continue
        op = opened_under(lsof, q)
        if op is None or op:
            print("· %s，不动：%s" % ("打开文件检查不可用" if op is None else "有文件被进程打开", tilde(q))); continue
        if a.dry_run:
            print("预演：将删除 %s" % tilde(q)); continue
        try:
            (shutil.rmtree if os.path.isdir(q) and not os.path.islink(q) else os.unlink)(q)
            status, why = "purged", None
        except OSError as e:
            status, why = "purge_failed", str(e)[:300]
        finally:
            log_row("隔离期满删除" if not why else "隔离期满删除失败", q, gib(r.get("bytes")),
                    "隔离 %g 天无人恢复、无写入、无占用" % a.days, "quick_act purge", "不可恢复" if not why else why)
        receipt(rec_path, {"status": status, "moved_to": q, "why": why})
        n += status == "purged"
    print("隔离期满已删除 %d 项" % n)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("plan")
    p.add_argument("--items", required=True)
    p.add_argument("--user-text", required=True)
    p.add_argument("--run", required=True)
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
    q.add_argument("--run", required=True)
    q.add_argument("--days", type=float, default=7)
    q.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    {"plan": cmd_plan, "run": cmd_run, "purge": cmd_purge}[a.cmd](a)


if __name__ == "__main__":
    main()
