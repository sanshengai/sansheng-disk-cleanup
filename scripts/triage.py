#!/usr/bin/env python3
"""叁笙清理 · 快速分档体检（只读，默认输出到 /tmp）。

为什么有它：见 references/why-manual-beat-skill.md。全量遍历 + 全量引用检查又慢又判不出东西；
这里照“手工高手”的做法：并行 du 先宽后深 → 已知垃圾位置直查 → 便宜信号（lsof / 最近写入 /
git status / merge-base）分档。只读：不删、不改、不写家目录。所有命令卡由用户批准后才执行。

  python3 scripts/triage.py [--out /tmp/diskscan-C] [--quiet-hours 6] [--min-gb 0.5] [--workers 8]
                            [--prev <上次 triage.json>]

分档：
  tier1  可再生缓存/临时文件：在白名单类别里、无进程打开、静默期内无写入 → 一键批量命令（用户批准后执行）
  tier2  工作树/构建残留：核对 locked / 未提交 / 进程 / 最近写入 / HEAD 已在远端 → `git worktree remove` 命令卡
  tier3  会话/模型/镜像/存档/未归类大目录：给大小与最小定位，需用户判断
  keep   在用或受保护（含原因），绝不进 tier1/2
"""
import argparse, bisect, collections, glob, json, os, re, shlex, subprocess, sys, time
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import protect  # 只用其只读判定
except Exception:  # noqa: BLE001
    protect = None

HOME = os.path.expanduser("~")
TMP = os.path.realpath(os.environ.get("TMPDIR") or "/tmp")
STAT = "/usr/bin/stat"  # 本机 PATH 里可能是 GNU stat，BSD 参数会报错
GB = 1024 ** 3   # 本报告全程用 GiB（1024³）；scan.py 的摘要是十进制 GB，两者不可直接相减
XDIR = os.path.join(os.path.dirname(TMP.rstrip("/")), "X")   # 与 $TMPDIR(T) 同级的 X 目录（Chrome 代码签名克隆）

# (c) 第1档里用原生命令代替 rm -rf 的类别
# 规则见 SKILL.md「第 1 档统一规则」：原生命令优先；其余“删除目录内容”用 find -x DIR -mindepth 1 -delete（不跟随链接、不跨卷、保留目录本身），
# 不写裸 rm -rf（硬约束）。检查命令与删除命令永远是两条独立命令。
NATIVE_CMD = {"Homebrew 缓存": "brew cleanup", "npm 缓存": "npm cache clean --force", "uv 缓存": "uv cache clean",
              "pip 缓存": "python3 -m pip cache purge"}
# 有专用窄入口的类别（自带 plan/check/run，比通用写法更严）
DEDICATED_CMD = {"node 编译缓存": "python3 scripts/node_compile_cache.py plan --run <run目录> --method direct",
                 "Chrome 代码签名克隆": "python3 scripts/chrome_clones.py plan --run <run目录>"}

# 第1档“对应 App 正在运行”识别：(路径正则, App 名, 进程名正则)。命中且进程在跑 → 移入“待 App 退出后”。
APP_RULES = [
    (r"/Caches/CodexBar|codexbar", "CodexBar", r"(^|/)CodexBar$"),
    (r"/Caches/cmux|cmux", "cmux", r"(^|/)cmux$"),
    (r"/Caches/(com\.openai\.codex|Codex)\b|/\.cache/codex-runtimes", "Codex", r"(^|/)Codex$|(^|/)codex$"),
    (r"/Caches/Google|com\.google\.Chrome|chrome_crashpad", "Google Chrome", r"(^|/)Google Chrome( Helper.*)?$"),
    (r"/Caches/(Cursor|com\.todesktop|com\.anysphere)", "Cursor", r"(^|/)Cursor( Helper.*)?$"),
    (r"/Caches/(Claude|claude-cli-nodejs|com\.anthropic)", "Claude", r"(^|/)Claude( Helper.*)?$|(^|/)claude$"),
    (r"/Caches/(Homebrew|cask)", "Homebrew", r"(^|/)brew$"),
]
# (c) 程序本体（删了要重装才能用），不是缓存：不进第1档，降到第3档
PROGRAM_NOT_CACHE = ("ms-playwright", "puppeteer", "Cypress", "electron", "node-gyp", "camoufox", "selenium")
# (d) Agent 合并/部署流程产物：保留档标注“流程结束后应由创建者删除”
import fnmatch
FLOW_PATTERNS = ("*/_merge-tmp*", "*/_cmp-tag*", "*/sansheng-sync-wt*", "*/build-tmp/*-slot*")
FLOW_NOTE = "流程产物，流程结束后应由创建者删除"


def flow_artifact(path):
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g + "/*") for g in FLOW_PATTERNS)

# ===== 本机配置（不进公开仓）=====
# 工作区根、生产目录、制作台账、盘外备份台账等因人而异，写在 ~/.sansheng/disk-cleanup/triage-local.json
# （或环境变量 SANSHENG_DISK_CLEANUP_TRIAGE 指向的文件）。没配置时，依赖它们的检查自动跳过，其余照常。
# 字段与示例见 references/triage-local.example.json。
LOCAL_DEFAULTS = {
    "work_root": None,            # 你的工作区根目录（引用检查、生产目录、成品拆分都在这里面做）
    "production_globs": [],       # 生产目录（相对 work_root 的 glob），只展示、永不进可删档
    "ledger_globs": [],           # 制作台账等二进制登记文件（相对文件名 glob），路径出现在里面即视为登记在册
    "ref_doc_extra": [],          # 额外要搜引用的文档文件名 glob
    "hard_keep_extra": [],        # 额外的永不进可删档路径片段
    "video_root": None,           # 成品目录（相对 work_root），第二层逐项列给用户判断
    "prefer_worktree": None,      # 最大工作树拆解优先选名字含它的
    "same_disk_archive_globs": [],  # 同盘存档目录（相对 work_root 的 glob）
    "cleanup_log": "~/.sansheng/disk-cleanup/cleanup-log.md",
    "offdisk_backup": None,       # {"label": "网盘", "state": "<state.json，键为 work_root 相对路径>", "logs": ["<日志 glob>"]}
}


def load_local(path=None):
    path = path or os.environ.get("SANSHENG_DISK_CLEANUP_TRIAGE") or HOME + "/.sansheng/disk-cleanup/triage-local.json"
    cfg = dict(LOCAL_DEFAULTS)
    try:
        cfg.update(json.load(open(os.path.expanduser(path), encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


LOCAL = load_local()
_ex = lambda v: os.path.expanduser(v) if v else None  # noqa: E731
# 未配置工作区时用一个不存在的路径占位：isdir 为假、startswith 不会命中任何真实路径
COWORK = (_ex(LOCAL["work_root"]) or HOME + "/.sansheng/disk-cleanup/(未配置工作区)").rstrip("/")


def _under_work(rel_globs):
    return tuple(os.path.join(COWORK, g) for g in rel_globs or ())


# 永不进可删档（tier1/2）的路径片段；另叠加 profile.json 的保护名单
HARD_KEEP = ("cleanup-archive", "-slot", "deploy-slot", "/.git", "thread_history", "/Movies/") + tuple(LOCAL["hard_keep_extra"] or ())

# ===== 防误删红线（2026-11-02 事故后加，见 references/incident-20261102-production-dirs.md）=====
PRODUCTION_GLOBS = _under_work(LOCAL["production_globs"])
LEDGER_GLOBS = tuple(LOCAL["ledger_globs"] or ())
REF_DOC_GLOBS = ("AGENTS.md", "README*.md", "*.json",
                 # 真正会读路径的是脚本与 Skill 指令，原来只搜文档和 json，脚本依赖反而漏查
                 "*.py", "*.sh", "*.ps1", "*.mjs", "*.js", "*.ts", "SKILL.md") + tuple(LOCAL["ref_doc_extra"] or ())
# 引用命中分两类（根因见 references/why-manual-beat-skill.md）：只有“消费者”命中才拦截；“历史记录”只提到路径，不代表还要用。
# 典型污染：本 Skill 往期报告（disk-watch/*/triage.json）、快照比对清单（*comparison*.json、snapshot-resume）、
# 前后对照（*-before.json / *-after.json）、回执与日志、records/ 下的过程记录。
HISTORY_REF_RX = re.compile(
    r"/disk-watch/|/\.sansheng/disk-cleanup/|/sansheng-triage/|/sansheng-disk-cleanup-ro/"
    r"|snapshot|comparison|[-_](before|after)\.json$|registry-(before|after)|/records/|receipt|回执|台账快照"
    r"|\.log$|\.jsonl$|/history/|audit|/runs/|/_archive/|/cleanup-archive-", re.I)


def split_ref_hits(hits):
    """引用命中 → (消费者, 历史记录)。消费者 = 脚本、Skill/Agent 指令、README、非快照类配置。"""
    active, history = [], []
    for h in hits or []:
        (history if HISTORY_REF_RX.search(h) else active).append(h)
    return active, history
_BK = LOCAL["offdisk_backup"] or {}
BACKUP_LABEL = _BK.get("label") or "盘外备份"
BACKUP_STATE = _ex(_BK.get("state"))
BACKUP_LOGS = tuple(_ex(g) for g in _BK.get("logs") or ())
CLEANUP_LOG = _ex(LOCAL["cleanup_log"])
_BACKUP = {}


def backup_state():
    """只读加载盘外备份台账一次并缓存（键为工作区相对路径；原来每项都重读几万项 JSON，是耗时来源之一）。"""
    if "st" not in _BACKUP:
        try:
            st = json.load(open(BACKUP_STATE, encoding="utf-8"))
            _BACKUP["st"] = st
            _BACKUP["keys"] = sorted(st)
        except Exception:  # noqa: BLE001
            _BACKUP["st"], _BACKUP["keys"] = None, []
    return _BACKUP["st"]


def backup_count(rel):
    """台账里以 rel 为前缀（或恰为 rel）的项数；二分查找，O(log n)。"""
    keys = _BACKUP.get("keys") or []
    pre = rel.rstrip("/") + "/"
    i = bisect.bisect_left(keys, pre)
    n = 0
    while i < len(keys) and keys[i].startswith(pre):
        n += 1; i += 1
    return n + (1 if rel.rstrip("/") in (_BACKUP.get("st") or {}) else 0)


def backup_failures(limit=50):
    """只读解析备份日志里的最终失败项（“重试 N/N … ：路径：错误”）和最近一次汇总行。不碰备份脚本与状态文件。"""
    logs = sorted(x for g in BACKUP_LOGS for x in glob.glob(g))
    out = []
    for lg in logs:
        if not os.path.isfile(lg):
            continue
        fails, summary = {}, None
        try:
            for ln in open(lg, encoding="utf-8", errors="replace"):
                m = re.match(r"重试 (\d+)/(\d+)（[^）]*）：(.+?)：(.*)$", ln.strip())
                if m and m.group(1) == m.group(2):
                    fails[m.group(3)] = m.group(4)[:160]
                if "汇总" in ln and "失败" in ln:
                    summary = ln.strip()[:200]
        except OSError:
            continue
        out.append({"log": lg, "summary": summary,
                    "failed": [{"path": p, "error": e} for p, e in list(fails.items())[:limit]]})
    return out


def is_production(path):
    """红线 1：生产目录默认受保护（只展示，不进任何可删档）。"""
    for g in PRODUCTION_GLOBS:
        for root in glob.glob(g):
            if path == root or path.startswith(root.rstrip("/") + "/"):
                return root
    return None


def ledger_registered(path, timeout=30):
    """红线 1：路径名出现在任一登记文件（本机配置 ledger_globs，如制作台账 sqlite；rg -a 搜二进制里的文本，便宜）→ 视为登记在册。"""
    base = os.path.basename(path.rstrip("/"))
    if not base or not LEDGER_GLOBS or not os.path.isdir(COWORK):
        return []
    cmd = ["rg", "-l", "-a", "-F", "--no-ignore", "--hidden"]
    for g in LEDGER_GLOBS:
        cmd += ["-g", g]
    rc, out, _ = run(cmd + ["-e", base, COWORK], timeout=timeout)
    return [l for l in out.splitlines() if l]


_REF_CACHE = {}


def prefetch_refs(paths, timeout=180):
    """性能：把所有候选的引用检查合成一次 rg（-o -F -f 模式文件），结果按关键字回填缓存。
    语义与 reference_hits 相同（只读）；超时则不填缓存，逐项回退。"""
    paths = [p for p in paths if p and p.startswith(COWORK + "/")]
    if not paths or not os.path.isdir(COWORK):
        return
    keymap = {}
    for p in paths:
        base = os.path.basename(p.rstrip("/"))
        for k in {base, p, tilde(p), os.path.relpath(p, COWORK)}:
            if k:
                keymap.setdefault(k, set()).add(p)
    pf = os.path.join(TMP, "triage-ref-patterns-%d.txt" % os.getpid())
    open(pf, "w", encoding="utf-8").write("\n".join(sorted(keymap)) + "\n")
    cmd = ["rg", "-o", "-F", "-H", "--no-line-number", "--no-ignore", "--hidden", "--max-filesize", "8M",
           "-g", "!.git", "-g", "!node_modules"]
    for g in REF_DOC_GLOBS:
        cmd += ["-g", g]
    rc, out, _ = run(cmd + ["-f", pf, COWORK], timeout=timeout)
    rc2, out2 = 0, ""
    if LEDGER_GLOBS:
        lcmd = ["rg", "-o", "-a", "-F", "-H", "--no-line-number", "--no-ignore", "--hidden"]
        for g in LEDGER_GLOBS:
            lcmd += ["-g", g]
        rc2, out2, _ = run(lcmd + ["-f", pf, COWORK], timeout=timeout)
    try:
        os.remove(pf)
    except OSError:
        pass
    if rc in (124, 127) or rc2 in (124, 127):
        return
    hits = {p: set() for p in paths}
    for ln in (out + "\n" + out2).splitlines():
        fpath, sep, key = ln.partition(":")
        if not sep:
            continue
        for p in keymap.get(key, ()):
            if not fpath.startswith(p.rstrip("/") + "/"):
                hits[p].add(fpath)
    for p in paths:
        _REF_CACHE[p] = sorted(hits[p])


def reference_hits(path, timeout=60):
    if path in _REF_CACHE:
        return _REF_CACHE[path]
    """红线 2：在工作区的 AGENTS.md / README*.md / *.json 台账、脚本与 Skill 指令里搜目录名与路径（rg -l -F）。
    返回命中文件列表；超时返回 None（= 引用未查，按“引用、问用户”处理）。"""
    base = os.path.basename(path.rstrip("/"))
    if not os.path.isdir(COWORK) or not base:
        return []
    keys = {base, path, tilde(path)}
    if path.startswith(COWORK + "/"):
        keys.add(os.path.relpath(path, COWORK))
    cmd = ["rg", "-l", "-F", "--no-ignore", "--hidden", "--max-filesize", "8M", "-g", "!.git", "-g", "!node_modules"]
    for g in REF_DOC_GLOBS:
        cmd += ["-g", g]
    for k in sorted(keys):
        cmd += ["-e", k]
    rc, out, _ = run(cmd + [COWORK], timeout=timeout)
    if rc in (124, 127):
        return None
    hits = [l for l in out.splitlines() if l and not l.startswith(path.rstrip("/") + "/")]
    return hits + ledger_registered(path)


def backup_status(path):
    """红线 4：只有盘外备份才算“可恢复”。盘外备份台账（本机配置 offdisk_backup.state）的键是工作区相对路径。
    同盘存档（cleanup-archive 等）不算备份。外接盘归档无法自动确认，返回“未确认”。"""
    if not BACKUP_STATE:
        return "未确认（未配置盘外备份台账）"
    if not path.startswith(COWORK + "/"):
        return "未确认（不在工作区，%s台账不覆盖）" % BACKUP_LABEL
    rel = os.path.relpath(path, COWORK).rstrip("/")
    if backup_state() is None:
        return "未确认（读不到%s台账）" % BACKUP_LABEL
    n = backup_count(rel)
    fl = [f["path"] for g in (_BACKUP.get("fails") or []) for f in g["failed"] if f["path"].startswith(rel + "/")]
    if fl:
        return "%s %d 项，但有 %d 个上传失败（如 %s）" % (BACKUP_LABEL, n, len(fl), fl[0][-60:])
    return ("%s %d 项（不保证完整，删前仍需核对）" % (BACKUP_LABEL, n)) if n else "无盘外备份（同盘存档不算备份）"


def apply_red_lines(items):
    """把候选项拆成：保持原档 / protected-production / referenced-ask。纯只读。"""
    keep, prod, refd = [], [], []
    prefetch_refs([x.get("path") for x in items if not is_production(x.get("path") or "")])
    for x in items:
        p = x.get("path") or ""
        root = is_production(p)
        if root:
            x["red_line"] = "生产目录（%s）默认保护" % tilde(root)
            x["backup"] = backup_status(p)
            prod.append(x); continue
        if p.startswith(COWORK + "/"):
            h = reference_hits(p)
            active, history = split_ref_hits(h)
            if history:
                x["history_refs"] = len(history)
                x["history_ref_samples"] = [tilde(f) for f in history[:3]]
            if h is None or active:
                x["red_line"] = "引用未查完（超时）" if h is None else "被引用（消费者）：" + "，".join(tilde(f) for f in active[:5])
                x["backup"] = backup_status(p)
                refd.append(x); continue
            x["backup"] = backup_status(p)
        keep.append(x)
    return keep, prod, refd


# tier1 白名单：可再生缓存（类别 → glob 列表 → 说明）
# 第4列 = 卡片阈值（小时）。实际静默期 = max(--quiet-hours 默认 6, 卡片阈值)，即取较严者；规则唯一出处见 SKILL.md。
TIER1 = [
    ("node 编译缓存", [TMP + "/node-compile-cache"], "Node 自动重建；走 node_compile_cache.py 专用入口", 24),
    ("pytest 临时目录", [TMP + "/pytest-of-*"], "pytest 每次运行重建", None),
    ("视频渲染缓存", [TMP + "/hyperframes-extract-cache*"], "抽帧缓存，渲染时重建（agent-junk-locations 卡：24 小时）", 24),
    ("视频渲染缓存", [TMP + "/c7-input*"], "渲染输入缓存，渲染时重建", None),
    ("Chrome 代码签名克隆", [XDIR + "/com.google.Chrome.code_sign_clone"], "Chrome 更新后的启动克隆；仅 Chrome 未运行时可清（browsers 卡）", None),
    ("Homebrew 缓存", [HOME + "/Library/Caches/Homebrew"], "旧版本下载包；用原生命令", None),
    ("npx 缓存", [HOME + "/.npm/_npx"], "npx 临时安装，下次运行重装", None),
    ("npm 缓存", [HOME + "/.npm/_cacache"], "npm 重新下载；用原生命令清理", None),
    ("uv 缓存", [HOME + "/.cache/uv"], "用原生命令 `uv cache clean`", None),
    ("pip 缓存", [HOME + "/.cache/pip", HOME + "/Library/Caches/pip"], "用原生命令 `pip cache purge`", None),
    ("Codex 运行时安装残留", [HOME + "/.cache/codex-runtimes/codex-runtime-install-*"], "已有 codex-primary-runtime 时为安装中间目录", None),
    ("Gradle 缓存", [HOME + "/.gradle/caches"], "构建时重新下载", None),
    ("bun 缓存", [HOME + "/.bun/install/cache"], "重新下载", None),
    ("应用缓存 Library/Caches", [HOME + "/Library/Caches/*"], "应用缓存，关闭应用后可再生；com.apple.* 不列（macos-system 卡：24 小时）", 24),
    # 第三轮：本 Skill 自己的运行目录也会攒到几个 GB（11-03 实测 runs 2.3G + investigations 1G）
    ("清理 Skill 旧运行目录", [HOME + "/.sansheng/disk-cleanup/runs/*", HOME + "/.sansheng/disk-cleanup/investigations/*",
                         "/tmp/sansheng-disk-cleanup-ro/runs/*", "/tmp/sansheng-triage/*"],
     "本 Skill 历次扫描/调查的运行目录；空间历史另存 history/，设置在 profile.json，都不在这里", 168),
]
CHROME_CLONE_ROOT = XDIR + "/com.google.Chrome.code_sign_clone"


def chrome_starts():
    try:
        import chrome_clones
        return chrome_clones.chrome_main_starts()
    except Exception:  # noqa: BLE001
        return None


def clone_birthtime(p):
    try:
        return os.stat(p).st_birthtime
    except (OSError, AttributeError):
        return None


def held_summary(held):
    """子项全部保留时的父项原因：按子项真实理由计数（父目录 mtime/打开文件只是表象）。"""
    reasons = collections.Counter(h.get("reason") or "在用" for h in held)
    return "逐份判断：" + "；".join("%d 份%s" % (n, r) for r, n in reasons.items())


def split_children(ctx, it, hrs, min_bytes, starts=None):
    """第三轮根因 1：整目录因“有 1 个打开文件 / 有写入”被整体判在用，同目录下大量真正闲置的子项一起被保留
    （Chrome 克隆 5 份只开 1 份，7.1G 全保留）。整目录不过时拆到子项逐个判，子项各自过 打开/写入/保护。"""
    p = it["path"]
    try:
        kids = [os.path.join(p, n) for n in sorted(os.listdir(p))]
    except OSError:
        return [], []
    ok, held = [], []
    for k in kids:
        if os.path.islink(k) or not os.path.isdir(k):
            continue
        b = ctx.size(k) or 0
        if b < min_bytes:
            continue
        row = {"category": it["category"] + "（子项）", "path": k, "bytes": b, "note": it["note"], "quiet_hours": hrs,
               "open": ctx.open_under(k), "app_running": None, "parent": p}
        if p == CHROME_CLONE_ROOT:
            birth = clone_birthtime(k)
            if starts is None or birth is None:
                row["reason"] = "读不到 Chrome 进程或克隆创建时间，保守保留"; held.append(row); continue
            import chrome_clones
            if chrome_clones.matches_running(birth, starts):
                row["reason"] = "对应运行中的 Chrome 进程（启动时刻与克隆创建时刻相差 ≤3 分钟）"; held.append(row); continue
            if time.time() - birth < 3600:
                row["reason"] = "创建不足 1 小时"; held.append(row); continue
            row["note"] = "运行中的 Chrome 对不上这份克隆（异常退出留下的残留）"
        why = ctx.keep_reason(k)
        rec = ctx.recent_write(k, hrs) if p != CHROME_CLONE_ROOT else False
        if why:
            row["reason"] = why
        elif row["open"] is None or rec is None:
            row["reason"] = "信号不全"
        elif row["open"]:
            row["reason"] = "有 %d 个打开文件（在用）" % row["open"]
        elif rec:
            row["reason"] = "%g 小时内有写入（在用）" % hrs
        if row.get("reason"):
            held.append(row); continue
        row["last_write"] = ctx.last_write(k)
        chk, cmd = tier1_commands(it["category"], k)
        row["check_command"] = chk.replace("%MIN%", str(int(hrs * 60)))
        row["command"] = cmd if it["category"] in DEDICATED_CMD else "find -x %s -delete" % shlex.quote(k)
        ok.append(row)
    return ok, held
SKIP_IN_GENERIC_CACHES = ("pip", "Homebrew")   # 已有专门类别


def effective_hours(default_hours, card_hours):
    """第1档统一规则：默认静默期与卡片阈值取较严（更长）者。"""
    return max(float(default_hours), float(card_hours or 0))


def tier1_commands(cat, path):
    """返回 (检查命令, 删除命令)。两条独立命令；删除命令绝不是裸 rm -rf。"""
    q = shlex.quote(path)
    check = "find -x %s -type f -mmin -%%MIN%% ! -name .DS_Store -print -quit   # 无输出 = 静默期无写入" % q
    if cat in NATIVE_CMD:
        return check, NATIVE_CMD[cat]
    if cat in DEDICATED_CMD:
        return check, DEDICATED_CMD[cat]
    return check, "find -x %s -mindepth 1 -delete" % q


def running_apps():
    rc, out, _ = run(["ps", "-axo", "comm="], timeout=10)
    return [l.strip() for l in out.splitlines() if l.strip()]


def app_running_for(path, procs):
    """path 对应的 App 若在运行，返回 App 名；否则 None。"""
    for prx, name, crx in APP_RULES:
        if re.search(prx, path, re.I) and any(re.search(crx, c) for c in procs):
            return name
    return None


def growth_gap_note(gap):
    """增长对账：差额 = df 变化 − 已列增长。负 → 已列增长之外有删除抵消；正 → 小增长与未扫区域。"""
    if gap is None:
        return "无可比基线"
    if gap < 0:
        return "差额为负：已列增长之外有删除/清理抵消了约 %s" % human(-gap)
    if gap > 0:
        return "差额为正：低于阈值的零碎增长 + 未扫到/无权限区域，约 %s" % human(gap)
    return "差额为 0"


def display(p):
    """报告显示用：$TMPDIR 缩写、家目录 ~、URL 编码段解码。"""
    t = TMP.rstrip("/")
    if p == t or p.startswith(t + "/"):
        p = "$TMPDIR" + p[len(t):]
    elif p.startswith(HOME + "/"):
        p = "~" + p[len(HOME):]
    if "%" in p:
        p = re.sub(r"/{2,}", "/", unquote(p))   # Grok 会话目录名是 URL 编码的绝对路径，解码后会出现 //
    return p


def denied_top(denied, n=10):
    """无权限位置按目录归类（家目录下取两级）的 Top N。"""
    cnt = {}
    for ln in denied:
        m = re.search(r"(/\S.*?): (Permission denied|Operation not permitted)", ln)
        if not m:
            continue
        p = m.group(1).strip("'\u2018\u2019\"")
        if p.startswith(HOME + "/"):
            parts = p[len(HOME) + 1:].split("/")
            key = "~/" + "/".join(parts[:2])
        else:
            key = "/".join(p.split("/")[:4])
        cnt[key] = cnt.get(key, 0) + 1
    return sorted(cnt.items(), key=lambda x: -x[1])[:n]


def run(cmd, timeout=60, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as e:
        return 127, "", str(e)


def human(b):
    """GiB 口径（1024 进制）。"""
    if b is None:
        return "?"
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(b) < 1024 or u == "TiB":
            return ("%.1f %s" % (b, u)) if u in ("GiB", "TiB") else ("%d %s" % (b, u))
        b /= 1024.0


def tilde(p):
    return display(p)


class Ctx:
    def __init__(self, args):
        self.args = args
        self.t0 = time.monotonic()
        self.phase = {}
        self.denied = []
        self.sizes = {}
        self.open = []
        self.profile = None
        if protect:
            try:
                self.profile = protect.load_profile()
            except Exception:  # noqa: BLE001
                self.profile = None

    def mark(self, name, t):
        self.phase[name] = round(time.monotonic() - t, 1)

    # ---- 便宜信号 ----
    def snapshot_lsof(self):
        rc, out, _ = run(["lsof", "-nP", "-w", "-Fpcn"], timeout=90)
        holders, pid, cmd = {}, None, ""
        for ln in out.splitlines():
            if ln.startswith("p"):
                pid, cmd = ln[1:], ""
            elif ln.startswith("c"):
                cmd = ln[1:]
            elif ln.startswith("n/") and pid:
                holders.setdefault(ln[1:], set()).add((pid, cmd))
        self.holders = holders
        self.open = sorted(holders)
        self.lsof_ok = rc in (0, 1) and bool(self.open)
        self._argv = {}

    def who_uses(self, path, limit=3):
        """(b) 持有该路径下文件的进程：[{pid, name, cmdline, files}]（来自 lsof 快照）。"""
        if not getattr(self, "lsof_ok", False):
            return []
        pre = path.rstrip("/") + "/"
        i = bisect.bisect_left(self.open, path)
        cnt = {}
        while i < len(self.open) and (self.open[i] == path or self.open[i].startswith(pre)):
            for pc in self.holders[self.open[i]]:
                cnt[pc] = cnt.get(pc, 0) + 1
            i += 1
        out = []
        for (pid, name), n in sorted(cnt.items(), key=lambda x: -x[1])[:limit]:
            if pid not in self._argv:
                _, a, _ = run(["ps", "-o", "command=", "-p", pid], timeout=5)
                self._argv[pid] = a.strip()[:200]
            out.append({"pid": int(pid), "name": name, "cmdline": self._argv[pid], "files": n})
        return out

    def last_write(self, path):
        """(b) 最近写入时间（目录树内最新 mtime 的文件，忽略 .DS_Store；45s 封顶）。"""
        rc, out, _ = run(["find", path, "-xdev", "-type", "f", "!", "-name", ".DS_Store",
                          "-exec", STAT, "-f", "%m %N", "{}", "+"], timeout=45)
        best = None
        for ln in out.splitlines():
            t, _, f = ln.partition(" ")
            try:
                t = int(t)
            except ValueError:
                continue
            if best is None or t > best[0]:
                best = (t, f)
        if not best:
            return None
        return {"time": datetime.fromtimestamp(best[0]).astimezone().isoformat(timespec="minutes"),
                "file": best[1], "partial": rc == 124}

    def open_under(self, path):
        pre = path.rstrip("/") + "/"
        i = bisect.bisect_left(self.open, path)
        n = 0
        while i < len(self.open) and (self.open[i] == path or self.open[i].startswith(pre)):
            n += 1
            i += 1
        return n if self.lsof_ok else None

    def recent_write(self, path, hours=None):
        """静默期内是否有真实文件写入（忽略 .DS_Store / *.lock / index）。返回 True/False/None(超时)。"""
        mins = int((hours or self.args.quiet_hours) * 60)
        rc, out, err = run(["find", path, "-xdev", "-type", "f", "-mmin", "-%d" % mins,
                            "!", "-name", ".DS_Store", "!", "-name", "*.lock", "!", "-name", "index", "-print", "-quit"],
                           timeout=45)
        if rc == 124:
            return None
        return bool(out.strip())

    def size(self, path):
        if path in self.sizes:
            return self.sizes[path]
        rc, out, _ = run(["du", "-xsk", path], timeout=300)
        try:
            v = int(out.split()[0]) * 1024
        except (IndexError, ValueError):
            v = None
        self.sizes[path] = v
        return v

    def keep_reason(self, path):
        for frag in HARD_KEEP:
            if frag in path:
                return "硬保护：路径含 “%s”" % frag
        if protect and self.profile is not None:
            try:
                r = protect.protected_reason(path, self.profile)
                if r:
                    return "profile 保护：" + str(r)
            except Exception:  # noqa: BLE001
                pass
        return None


def rendering_dirs(minutes=10, timeout=30):
    """“在渲染/波动”：生产目录下（取到第 3 层）最近 N 分钟有真实写入的目录。只读、每目录 -quit 早停。"""
    out = []
    cands = []
    for g in PRODUCTION_GLOBS:
        for root in glob.glob(g):
            for d in glob.glob(root.rstrip("/") + "/*/*") + glob.glob(root.rstrip("/") + "/*/*/*"):
                if os.path.isdir(d) and not os.path.islink(d):
                    cands.append(d)
    def chk(d):
        rc, o, _ = run(["find", d, "-xdev", "-type", "f", "-mmin", "-%d" % minutes, "!", "-name", ".DS_Store", "-print", "-quit"], timeout=timeout)
        return d if o.strip() else None
    with ThreadPoolExecutor(8) as ex:
        hit = [d for d in ex.map(chk, cands) if d]
    # 只保留最深的命中（父目录命中多半因为子目录）
    for d in sorted(hit, key=len, reverse=True):
        if not any(o.startswith(d + "/") for o in out):
            out.append(d)
    return sorted(out)


def under_any(p, roots):
    return any(p == r or p.startswith(r + "/") or r.startswith(p + "/") for r in roots)


ARCHIVE_CACHE = os.path.join(TMP, "sansheng-triage-archive-cache")


def tgz_first_member(t, timeout=20):
    """只读 tgz 开头即可拿到顶层目录名：读到第一行就结束 tar，不解压整个包。"""
    try:
        p = subprocess.Popen(["tar", "-tzf", t], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        line = p.stdout.readline().strip()
        p.kill(); p.wait(timeout=timeout)
        return line
    except Exception:  # noqa: BLE001
        return ""


def tgz_members(t, timeout):
    """完整成员清单（解压流，慢）；按 路径+大小+mtime 缓存到 $TMPDIR，第二次起秒回。"""
    st = os.stat(t)
    key = re.sub(r"[^0-9A-Za-z]+", "_", "%s_%d_%d" % (t[-80:], st.st_size, int(st.st_mtime)))
    cf = os.path.join(ARCHIVE_CACHE, key + ".json")
    try:
        return json.load(open(cf, encoding="utf-8"))
    except (OSError, ValueError):
        pass
    rc, out, _ = run(["tar", "-tvzf", t], timeout=timeout)
    if rc:
        return None
    members = {}
    for ln in out.splitlines():
        f = ln.split(None, 8)
        if len(f) == 9 and not f[0].startswith("d"):
            try:
                members[f[8]] = int(f[4])
            except ValueError:
                pass
    try:
        os.makedirs(ARCHIVE_CACHE, exist_ok=True)
        json.dump(members, open(cf, "w", encoding="utf-8"), ensure_ascii=False)
    except OSError:
        pass
    return members


_DIR_INDEX = {}


def dir_index(timeout=120):
    """工作区 5 层内的目录名 → 第一个路径。整轮只建一次（第三轮根因 7：原来每个 tgz 各跑一次 find，
    每次最长 60 秒，26 个包占了 triage 总耗时 592 秒里的 350 秒）。"""
    if "idx" not in _DIR_INDEX:
        rc, out, _ = run(["find", "-x", COWORK, "-maxdepth", "5", "-type", "d",
                          "(", "-name", ".git", "-o", "-name", "node_modules", "-o", "-name", "cleanup-archive-*", ")", "-prune",
                          "-o", "-type", "d", "-print"], timeout=timeout)
        idx = {}
        for ln in out.splitlines():
            idx.setdefault(os.path.basename(ln), ln)
        _DIR_INDEX["idx"] = idx
    return _DIR_INDEX["idx"]


def offdisk_card(a):
    """同盘存档已有盘外副本（盘外备份台账同路径同大小）→ 给“本地可删”命令卡。只读生成，不执行。"""
    q = shlex.quote(a["path"])
    a["offdisk_ok"] = True
    a["check_command"] = "/usr/bin/stat -f %%z %s   # 应输出 %d（与%s台账记录的大小一致）" % (q, a["bytes"], BACKUP_LABEL)
    a["command"] = "find -x %s -maxdepth 0 -type f -delete" % q
    return a


def archive_check(deep=False, timeout=180):
    """同盘存档 tgz：读成员清单（tar -tvzf，只读），对比原路径是否还在、文件数/字节是否一致，
    区分“唯一副本 / 重复件 / 部分重复（tgz 里有原路径已没有的文件）”，并给 tgz 自身的盘外备份状态。"""
    rows = []
    st = backup_state() or {}
    tgzs = {t for g in _under_work(LOCAL["same_disk_archive_globs"]) for t in glob.glob(g + "/**/*.tgz", recursive=True)}
    for t in sorted(tgzs):
        r = {"path": t, "bytes": os.path.getsize(t)}
        rel_t = os.path.relpath(t, COWORK)
        r["tgz_offdisk"] = rel_t in st and st[rel_t].get("size") == r["bytes"]
        r["tgz_backup"] = BACKUP_LABEL + ("已传" if r["tgz_offdisk"] else "有记录但大小不符" if rel_t in st else "无记录")
        first = tgz_first_member(t)
        tops = [first.split("/")[0]] if first else []
        r["top"] = tops
        # 原路径：盘外备份台账里含 “/<top>/” 的键推断父目录；退而在工作区 5 层内按名字找
        orig = None
        if tops:
            needle = "/" + tops[0] + "/"
            for k in _BACKUP.get("keys") or []:
                if needle in k and "cleanup-archive" not in k:
                    orig = os.path.join(COWORK, k[:k.index(needle)], tops[0]); break
            if not orig:
                orig = dir_index().get(tops[0])
        r["orig"] = orig
        if not orig or not os.path.isdir(orig):
            r["status"] = "唯一副本（原路径不在；本机仅此一份，删前必须恢复或确认盘外完整）"
        elif not deep:
            r["status"] = "原路径在，但未逐文件比对（加 --archive-deep 用 tar -tvzf 对比；原目录可能已被改动/删减，不能直接当重复件删）"
        elif (members := tgz_members(t, timeout)) is None:
            r["status"] = "原路径在；tar 读取失败/超时，未比对"
        else:
            r["members"], r["member_bytes"] = len(members), sum(members.values())
            base = os.path.dirname(orig)
            missing = [m for m in members if not os.path.isfile(os.path.join(base, m))]
            ms = set(missing)
            diff = [m for m in members if m not in ms and os.path.getsize(os.path.join(base, m)) != members[m]]
            r["missing_files"], r["missing_bytes"] = len(missing), sum(members[m] for m in missing)
            r["size_diff"] = len(diff)
            r["missing_backup"] = sum(1 for m in missing if os.path.relpath(os.path.join(base, m), COWORK) not in st)
            r["missing_samples"] = missing[:5]
            if not missing and not diff:
                r["status"] = "重复件（原路径在，文件与大小一致）"
            else:
                r["status"] = "部分重复：tgz 里 %d 个文件（%s）原路径已没有、%d 个大小不同——不是纯重复，删 tgz 会丢本机唯一副本" % (
                    len(missing), human(r["missing_bytes"]), len(diff))
        if r["tgz_offdisk"]:
            offdisk_card(r)
        rows.append(r)
    return rows


def sampled_hash(p, chunk=1 << 20):
    """头/中/尾各 1MB 的抽样哈希：报告用，执行前由 clone_dedupe.py 全量 sha256 复核。"""
    import hashlib
    h = hashlib.blake2b()
    size = os.path.getsize(p)
    with open(p, "rb") as f:
        for off in (0, max(0, size // 2 - chunk // 2), max(0, size - chunk)):
            f.seek(off); h.update(f.read(chunk))
    return h.hexdigest()


def dup_roots(ctx):
    roots = [COWORK] if os.path.isdir(COWORK) else []
    for r in ((ctx.profile or {}).get("deliverable_roots") or []):
        r = os.path.realpath(os.path.expanduser(r if isinstance(r, str) else (r.get("path") or "")))
        if r and os.path.isdir(r) and not any(r == x or r.startswith(x + "/") for x in roots):
            roots.append(r)
    return roots


def dup_scan(ctx, min_mb=20, top=12):
    """第三轮根因 3：跨目录重复副本（成品库 / 交付 / 分发暂存 / 工作树 各一份，11-03 实测 42G）从未检测。
    这里按大小分组 + 抽样哈希给出名义可共享量与分布；处置用 clone_dedupe.py（APFS 克隆原地替换，不删路径）。"""
    import clone_dedupe as cd
    roots = dup_roots(ctx)
    if not roots:
        return None
    by = cd.collect(roots, int(min_mb * 2 ** 20))
    groups = []
    for size, ps in by.items():
        if len(ps) < 2:
            continue
        hs = {}
        for q in ps:
            try:
                hs.setdefault(sampled_hash(q), []).append(q)
            except OSError:
                pass
        for grp in hs.values():
            if len(grp) < 2:
                continue
            grp.sort(key=cd.keeper_rank)
            ino = os.stat(grp[0]).st_ino
            dups = [q for q in grp[1:] if os.stat(q).st_ino != ino]
            if dups:
                groups.append({"size": size, "keep": grp[0], "dups": dups})
    where = {}
    for g in groups:
        for q in g["dups"]:
            r = next((x for x in roots if q.startswith(x + "/")), "")
            k = "/".join(os.path.relpath(q, r).split("/")[:3]) if r else q
            where[k] = where.get(k, 0) + g["size"]
    nominal = sum(g["size"] * len(g["dups"]) for g in groups)
    rq = " ".join("--root %s" % shlex.quote(r) for r in roots)
    return {"roots": roots, "groups": len(groups), "dup_files": sum(len(g["dups"]) for g in groups), "nominal_bytes": nominal,
            "by_location": sorted(where.items(), key=lambda x: -x[1])[:top],
            "samples": [{"keep": g["keep"], "dup": g["dups"][0], "size": g["size"]} for g in sorted(groups, key=lambda g: -g["size"] * len(g["dups"]))[:5]],
            "plan_command": "python3 scripts/clone_dedupe.py plan %s --out <run目录>/dedupe-plan.json" % rq,
            "dry_command": "python3 scripts/clone_dedupe.py run --plan <run目录>/dedupe-plan.json --receipt <run目录>/dedupe.jsonl --dry-run",
            "command": "python3 scripts/clone_dedupe.py run --plan <run目录>/dedupe-plan.json --receipt <run目录>/dedupe.jsonl"}


def du_tree(ctx, top, depth):
    rc, out, err = run(["du", "-xk", "-d", str(depth), top], timeout=ctx.args.du_timeout)
    res = {}
    for ln in out.splitlines():
        try:
            k, p = ln.split("\t", 1)
            res[p] = int(k) * 1024
        except ValueError:
            continue
    den = [l for l in err.splitlines() if "denied" in l or "not permitted" in l]
    return res, den, rc == 124


def breadth_first(ctx):
    """并行：家目录每个一级子目录各跑一次 du -d 3（一次遍历拿到 4 层），外加 $TMPDIR。"""
    t = time.monotonic()
    tops = [os.path.join(HOME, n) for n in os.listdir(HOME)]
    tops = [p for p in tops if os.path.isdir(p) and not os.path.islink(p)]
    tops.append(TMP.rstrip("/"))
    timeouts = []
    with ThreadPoolExecutor(ctx.args.workers) as ex:
        for top, (res, den, to) in zip(tops, ex.map(lambda p: du_tree(ctx, p, 3 if p != TMP.rstrip("/") else 1), tops)):
            ctx.sizes.update(res)
            ctx.denied += den
            if to:
                timeouts.append(top)
    ctx.du_timeouts = timeouts
    ctx.mark("du_breadth", t)


def tier1(ctx):
    out = []
    for cat, globs, note, card_h in TIER1:
        for g in globs:
            for p in sorted(glob.glob(g)):
                if not os.path.isdir(p) or os.path.islink(p):
                    continue
                if cat.startswith("应用缓存") and (os.path.basename(p).startswith("com.apple.") or os.path.basename(p) in SKIP_IN_GENERIC_CACHES):
                    continue
                if cat.startswith("Codex 运行时") and not glob.glob(HOME + "/.cache/codex-runtimes/codex-primary-runtime*"):
                    continue
                out.append((cat, p, note, effective_hours(ctx.args.quiet_hours, card_h)))
    items = []
    procs = running_apps()

    def check(c):
        cat, p, note, hrs = c
        size = ctx.size(p)
        return {"category": cat, "path": p, "bytes": size, "note": note, "quiet_hours": hrs,
                "open": ctx.open_under(p), "recent": ctx.recent_write(p, hrs),
                "app_running": app_running_for(p, procs), "last_write": ctx.last_write(p)}
    t = time.monotonic()
    with ThreadPoolExecutor(ctx.args.workers) as ex:
        items = list(ex.map(check, out))
    ctx.mark("tier1_checks", t)
    t1, keep, demoted, wait_app = [], [], [], []
    starts = chrome_starts() if os.path.isdir(CHROME_CLONE_ROOT) else None
    min_b = ctx.args.min_tier1_mb * 1024 * 1024
    for it in items:
        if (it["bytes"] or 0) < min_b:
            continue
        why = ctx.keep_reason(it["path"])
        if why:
            it["reason"] = why
            keep.append(it)
        elif it["open"] is None or it["recent"] is None:
            it["reason"] = "信号不全（lsof 或写入检查超时），降为 tier3"
            it["tier"] = 3
            keep.append(it)
        elif it["open"] > 0 or it["recent"] or it["path"] == CHROME_CLONE_ROOT:
            it["reason"] = ("有 %d 个打开文件（在用）" % it["open"]) if it["open"] else ("%g 小时内有写入（在用）" % it["quiet_hours"]) if it["recent"] else "逐份判断"
            is_program = any(k.lower() in os.path.basename(it["path"]).lower() for k in PROGRAM_NOT_CACHE)
            ok, held = ([], []) if is_program else split_children(ctx, it, it["quiet_hours"], min_b, starts)
            if ok:
                it["split"] = {"freed_children": len(ok), "held_children": len(held), "held_bytes": sum(h["bytes"] for h in held)}
                it["reason"] += "；已拆到子项：%d 个闲置子项进第 1 档，%d 个在用子项保留" % (len(ok), len(held))
                app = it["app_running"] if it["path"] != CHROME_CLONE_ROOT else None
                for o in ok:
                    o["app_running"] = app
                    if app:
                        o["reason"] = "App 运行中（%s），删前需退出" % app
                        wait_app.append(o)
                    else:
                        t1.append(o)
                it["bytes"] = sum(h["bytes"] for h in held)   # 保留档只计仍在用的部分，避免与第 1 档重复计量
            elif held and it["path"] == CHROME_CLONE_ROOT:
                # 子项全部保留时，原因写子项的真实理由（“6 小时内有写入”只是父目录的表象）
                it["reason"] = held_summary(held)
            keep.append(it)
        elif any(k.lower() in os.path.basename(it["path"]).lower() for k in PROGRAM_NOT_CACHE):
            it["tier"] = 3
            it["category"] = "程序本体（非缓存）"
            it["note"] = "浏览器/运行时程序本体，删后需重装（如 `npx playwright install`）；需用户判断"
            demoted.append(it)
        else:
            chk, cmd = tier1_commands(it["category"], it["path"])
            it["check_command"] = chk.replace("%MIN%", str(int(it["quiet_hours"] * 60)))
            it["command"] = cmd
            if it["app_running"]:
                it["reason"] = "App 运行中（%s），删前需退出" % it["app_running"]
                wait_app.append(it)
            else:
                t1.append(it)
    return t1, keep, demoted, wait_app


def du_many(paths, timeout=300):
    """分批 du -sk，返回 {路径: 字节}。一次进程处理几百个参数，比逐个 du 快一个数量级。"""
    res = {}
    for i in range(0, len(paths), 400):
        rc, out, _ = run(["du", "-xsk", "--"] + paths[i:i + 400], timeout=timeout)
        for ln in out.splitlines():
            k, _, q = ln.partition("\t")
            try:
                res[q] = int(k) * 1024
            except ValueError:
                pass
    return res


def ignored_usage(wt, timeout=180):
    """工作树里被 .gitignore 忽略的内容：[(相对路径, 字节)] 按大小降序、合计、错误。
    第三轮根因：原来用 `git status --ignored=matching --untracked-files=no`，这两个参数在 git 2.4x 里是非法组合，
    git 直接 fatal、输出为空，于是报告“被忽略 0 B”（实际 20G+）且不报错。现在检查退出码，失败写明原因。"""
    rc, out, err = run(["git", "-C", wt, "-c", "core.quotepath=false", "ls-files", "-z", "--others", "--ignored",
                        "--exclude-standard", "--directory"], timeout=timeout)
    if rc:
        return [], None, (err.strip() or "git ls-files 失败/超时")[:120]
    rels = [x.rstrip("/") for x in out.split("\0") if x and os.path.basename(x.rstrip("/")) != ".DS_Store"]
    sizes = du_many([os.path.join(wt, x) for x in rels])
    rows = sorted(((x, sizes.get(os.path.join(wt, x), 0)) for x in rels), key=lambda r: -r[1])
    return rows, sum(b for _, b in rows), None


def stale_subtasks(ctx, wt, depth=4, min_gb=1.0, stale_days=7, timeout=300, min_untracked_gb=0.5):
    """第三轮根因 4：活跃的长期工作树整棵“保留”，里面停写多日的旧子任务（11-03 实测 73G）从不出现。
    只读：du -d depth 找 ≥min_gb 的目录，取“整体 stale_days 天无真实写入”的最外层（至少第 2 层，
    不把顶层目录整个交给 git clean），统计其中 未跟踪+忽略 的体积，给 git clean 命令卡（已跟踪的在仓库里，不动）。"""
    rc, out, _ = run(["du", "-xk", "-d", str(depth), wt], timeout=timeout)
    sz = {}
    for ln in out.splitlines():
        k, _, q = ln.partition("\t")
        try:
            sz[q] = int(k) * 1024
        except ValueError:
            pass
    th = min_gb * GB
    big = {q: b for q, b in sz.items() if b >= th and q != wt and "/.git" not in q and "/node_modules" not in q}
    nodes = []
    for q in sorted(big, key=len):
        if os.path.relpath(q, wt).count("/") < 1:
            continue
        if any(q.startswith(n["path"] + "/") for n in nodes):
            continue
        if under_any(q, getattr(ctx, "rendering", [])) or ctx.recent_write(q, stale_days * 24) is not False:
            continue
        nodes.append({"path": q, "bytes": big[q]})
    rows = []
    cutoff = stale_days * 86400
    for n in nodes:
        q = n["path"]
        rel = os.path.relpath(q, wt)
        rc, o, _ = run(["git", "-C", wt, "-c", "core.quotepath=false", "ls-files", "-z", "--others", "--exclude-standard", "--", rel], timeout=timeout)
        rc2, o2, _ = run(["git", "-C", wt, "-c", "core.quotepath=false", "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--", rel], timeout=timeout)
        if rc or rc2:
            continue
        files = [os.path.join(wt, x) for x in set(o.split("\0") + o2.split("\0")) if x]
        ub = 0
        for f in files:
            try:
                ub += os.lstat(f).st_blocks * 512
            except OSError:
                pass
        if ub < min_untracked_gb * GB:
            continue
        lw = ctx.last_write(q)
        rows.append({"path": q, "bytes": n["bytes"], "untracked_bytes": ub, "untracked_files": len(files), "last_write": lw,
                     "keep_hint": ctx.keep_reason(q), "backup": backup_status(q),
                     "check_command": "git -C %s clean -n -d -x -- %s   # 预览：只列 Git 不管的文件" % (shlex.quote(wt), shlex.quote(rel)),
                     "command": "git -C %s clean -f -d -x -- %s" % (shlex.quote(wt), shlex.quote(rel))})
    return sorted(rows, key=lambda r: -r["untracked_bytes"])


def find_repos(ctx):
    repos = set()
    for pat in ("/*/.git", "/*/*/.git"):
        for g in glob.glob(HOME + pat):
            if os.path.isdir(g):
                repos.add(os.path.dirname(g))
    return sorted(repos)


def worktrees(ctx):
    t = time.monotonic()
    rows, prune = [], []
    for repo in find_repos(ctx):
        rc, out, _ = run(["git", "-C", repo, "worktree", "list", "--porcelain"], timeout=30)
        if rc:
            continue
        blocks = [b for b in out.strip().split("\n\n") if b.strip()]
        for i, b in enumerate(blocks):
            kv = {}
            for ln in b.splitlines():
                k, _, v = ln.partition(" ")
                kv[k] = v
            path = kv.get("worktree")
            if i == 0 or not path:
                continue  # 主工作树永不删
            if not os.path.exists(path):
                prune.append({"repo": repo, "path": path, "locked": "locked" in kv,
                              "command": "git -C %s worktree prune   # 路径已不存在%s" % (
                                  shlex.quote(repo), "；先 unlock" if "locked" in kv else "")})
                continue
            rows.append({"repo": repo, "path": path, "branch": kv.get("branch", "(detached)").replace("refs/heads/", ""),
                         "head": kv.get("HEAD"), "locked": "locked" in kv})

    def check(r):
        p = r["path"]
        r["bytes"] = ctx.size(p)
        r["open"] = ctx.open_under(p)
        rc, out, _ = run(["git", "-C", p, "status", "--porcelain"], timeout=180)
        dl = [l for l in out.splitlines() if l.strip()]
        r["dirty"] = None if rc else len(dl)
        r["dirty_paths"] = [l[3:] for l in dl[:5]]
        r["dirty_symlink_only"] = bool(dl) and all(l.startswith("?? ") and os.path.islink(os.path.join(p, l[3:].rstrip("/"))) for l in dl)
        r["tag"] = (run(["git", "-C", p, "tag", "--points-at", "HEAD"], timeout=30)[1].split() or [None])[0]
        ign, r["ignored_bytes"], r["ignored_error"] = ignored_usage(p)
        r["ignored_paths"] = [x for x, _ in ign[:10]]
        rc1, _, _ = run(["git", "-C", r["repo"], "merge-base", "--is-ancestor", r["head"] or "HEAD", "origin/main"], timeout=30)
        rc2, out2, _ = run(["git", "-C", r["repo"], "branch", "-r", "--contains", r["head"] or "HEAD"], timeout=60)
        r["on_origin_main"] = rc1 == 0
        r["remote_branches"] = [l.strip() for l in out2.splitlines() if l.strip()][:5]
        r["recent"] = ctx.recent_write(p)
        return r
    with ThreadPoolExecutor(min(4, ctx.args.workers)) as ex:
        rows = list(ex.map(check, rows))
    t2, keep = [], []
    for r in rows:
        fails = []
        why = ctx.keep_reason(r["path"])
        if why: fails.append(why)
        if r["locked"]: fails.append("locked")
        if r["dirty"] is None: fails.append("git status 超时/失败")
        elif r["dirty"] and not r["dirty_symlink_only"]:
            fails.append("%d 处未提交改动（%s）" % (r["dirty"], ", ".join(r["dirty_paths"][:3])))
        if r["open"] is None: fails.append("lsof 不可用")
        elif r["open"]: fails.append("%d 个文件被进程打开" % r["open"])
        if r["recent"] is None: fails.append("写入检查超时")
        elif r["recent"]: fails.append("%g 小时内有写入" % ctx.args.quiet_hours)
        if not (r["on_origin_main"] or r["remote_branches"] or r["tag"]): fails.append("HEAD 不在任何远端分支（可能有未推送提交）")
        big_ign = r["ignored_bytes"] >= 500 * 1024 * 1024
        if fails:
            r["reason"] = "；".join(fails)
            keep.append(r)
        else:
            r["command"] = "git -C %s worktree remove %s" % (shlex.quote(r["repo"]), shlex.quote(r["path"]))
            if r["dirty_symlink_only"]:
                r["command"] = r["command"].replace(" worktree remove ", " worktree remove --force ")
            r["note"] = ("唯一改动是未跟踪的符号链接 %s（删链接不删目标）；" % ", ".join(r["dirty_paths"]) if r["dirty_symlink_only"] else "") + (
                "HEAD 有 tag %s 保住；" % r["tag"] if r["tag"] and not (r["on_origin_main"] or r["remote_branches"]) else "") + (
                "%s；" % FLOW_NOTE if flow_artifact(r["path"]) else "") + "不删分支 %s。" % r["branch"] + (
                "注意：忽略文件 %s（%s）会一并删除，先看一眼。" % (human(r["ignored_bytes"]), ", ".join(r["ignored_paths"][:3])) if big_ign else "")
            t2.append(r)
    ctx.mark("worktrees", t)
    ctx.wt_rows = rows
    return t2, keep, prune


TIER3_GLOBS = [
    ("Codex 会话", HOME + "/.codex/sessions/*/*", "按月；交互会话逐个问，正在写的不动（codex 卡）"),
    ("Codex 生图", HOME + "/.codex/generated_images", "先查成品库是否有副本"),
    ("Codex 归档会话", HOME + "/.codex/archived_sessions", "已归档会话"),
    ("Antigravity 会话", HOME + "/.gemini/antigravity-cli/*", "conversations / brain 问用户"),
    ("Grok 会话", HOME + "/.grok/sessions/*", "工作目录已删的优先"),
    ("HF 模型", HOME + "/.cache/huggingface/hub/models--*", "逐模型查引用；删后重下"),
    ("Ollama 模型", HOME + "/.ollama/models", "按模型问"),
    ("Android 镜像", HOME + "/Library/Android/sdk/system-images/*", "probe android"),
    ("Docker 数据", HOME + "/Library/Containers/com.docker.docker/Data", "docker system df"),
    ("构建暂存", HOME + "/.sansheng/build-tmp/*", "问部署/构建 Agent；releases 只保留最近一次"),
    *[("清理存档（同盘）", g, "同盘存档抵消回收：建议搬到外接盘后再删本机副本") for g in _under_work(LOCAL["same_disk_archive_globs"])],
    ("Codex 桌面任务目录", HOME + "/Documents/Codex/*", "产物是否已搬进项目"),
    ("下载目录", HOME + "/Downloads", "旧安装包/大文件"),
    ("清理 Skill 自身工作区", HOME + "/.sansheng/disk-cleanup", "本 Skill 的设置/运行目录/隔离区(quarantine)/开发证据与候选包；profile.json、history/ 必留，runs 旧目录、候选 zip、quarantine 需用户判断"),
    ("仓库对象库（只算 pack）", HOME + "/*/.git/objects/pack", "只算 .git/objects/pack，不是整个 .git；看 `git count-objects -vH`；新 pack 可能是 repack 残留，由用户决定 `git gc`，Skill 不执行"),
]


def tier3(ctx, claimed):
    out = []
    for cat, g, note in TIER3_GLOBS:
        for p in sorted(glob.glob(g)):
            if not os.path.isdir(p) or any(p == c or p.startswith(c + "/") or c.startswith(p + "/") for c in claimed):
                continue
            b = ctx.sizes.get(p)
            if b is None:
                b = ctx.size(p)
            if (b or 0) >= ctx.args.min_gb * GB:
                out.append({"category": cat, "path": p, "bytes": b, "note": note,
                            "keep_hint": ctx.keep_reason(p)})
    hb = HOME + "/.cache/huggingface/hub/blobs"
    if os.path.isdir(hb):
        b = ctx.sizes.get(hb) or ctx.size(hb)
        for x in out:
            if x["category"] == "HF 模型":
                x["note"] += "（另有共享 blobs %s，已计入模型，不重复列）" % human(b); break
    # 程序本体参考：/opt/homebrew（不在家目录，du 不覆盖，以前漏列）
    if os.path.isdir("/opt/homebrew"):
        out.append({"category": "程序本体（非缓存）", "path": "/opt/homebrew", "bytes": ctx.size("/opt/homebrew"),
                    "note": "Homebrew 安装的程序本体，只读参考；缩小用 `brew autoremove` / 卸载不用的包", "keep_hint": "程序本体，只列参考"})
    # 特殊文件：thread_history sqlite、仓库 .git
    for f in glob.glob(HOME + "/.codex/thread_history*.sqlite"):
        out.append({"category": "Codex 历史库", "path": f, "bytes": os.path.getsize(f),
                    "note": "文件本身不手删；要缩小只能先决定删哪些 Codex 会话（`codex delete`），之后由原生回收", "keep_hint": None})
    if os.path.isdir(HOME + "/.sansheng/disk-cleanup"):
        for x in out:
            if x["path"] == HOME + "/.sansheng/disk-cleanup":
                parts = []
                for sub in ("runs", "quarantine", "history"):
                    b = ctx.size(os.path.join(x["path"], sub)) if os.path.isdir(os.path.join(x["path"], sub)) else None
                    if b:
                        parts.append("%s %s" % (sub, human(b)))
                zips = glob.glob(x["path"] + "/*.zip")
                if zips:
                    parts.append("候选 zip %d 个 %s" % (len(zips), human(sum(os.path.getsize(z) for z in zips))))
                x["note"] += "（" + "，".join(parts) + "）" if parts else ""
    return out


VIDEO_ROOT = os.path.join(COWORK, LOCAL["video_root"]) if LOCAL["video_root"] else None
VIDEO_CLASSES = [("作废", r"作废"), ("已交付/交付", r"已交付|交付|final|定稿"),
                 ("试做/重制/v1-v3/旧版", r"试做|试片|试播|重制|旧版|(^|[^0-9a-z])v[1-3]([^0-9]|$)|验证|对比|test")]


def classify_video(rel, mtime, now=None):
    """先看最近写入再看名字（第三轮根因 5：今天还在写的“历史英语试播”因名字含“试播”被归进“试做/旧版”）。
    “作废”是明确标记，优先级最高。"""
    if re.search(VIDEO_CLASSES[0][1], rel, re.I):
        return VIDEO_CLASSES[0][0]
    now = now or time.time()
    if mtime and now - mtime < 86400:
        return "进行中（24 小时内有写入）"
    for name, rx in VIDEO_CLASSES[1:]:
        if re.search(rx, rel, re.I):
            return name
    if mtime and datetime.fromtimestamp(mtime).date() == datetime.fromtimestamp(now).date():
        return "当日产出"
    return "其他"


def voided_dirs(root, timeout=120):
    """任意深度带“作废”标记的目录（取最外层）。第三轮：只列第二层时，第五层的 .作废-重叠（3.2G、0 引用）看不见。"""
    rc, out, _ = run(["find", "-x", root, "-type", "d", "-name", "*作废*", "-prune"], timeout=timeout)
    return sorted(set(l for l in out.splitlines() if l.strip())), rc == 124


def video_products(ctx, root=VIDEO_ROOT, ref_root=None, timeout=120):
    """成品目录（本机配置 video_root）整体仍硬保护；这里只把第二层拆开列出，供用户判断。不产生删除命令。"""
    if not root or not os.path.isdir(root):
        return None
    t = time.monotonic()
    rows = []
    vd, vd_partial = voided_dirs(root)
    for a in sorted(os.listdir(root)):
        pa = os.path.join(root, a)
        if not os.path.isdir(pa) or os.path.islink(pa) or a.startswith("__"):
            continue
        kids = [os.path.join(pa, b) for b in sorted(os.listdir(pa)) if os.path.isdir(os.path.join(pa, b)) and not b.startswith(".DS")]
        for q in (kids or [pa]):
            rows.append({"path": q, "rel": os.path.relpath(q, root)})
    have = {r["path"] for r in rows}
    for q in vd:
        if q not in have:
            rows.append({"path": q, "rel": os.path.relpath(q, root), "deep_void": True})
    def fill(r):
        r["bytes"] = ctx.sizes.get(r["path"]) or ctx.size(r["path"])
        lw = ctx.last_write(r["path"])
        r["last_write"] = lw
        try:
            mt = datetime.fromisoformat(lw["time"]).timestamp() if lw else None
        except ValueError:
            mt = None
        r["class"] = classify_video(r["rel"], mt)
        return r
    with ThreadPoolExecutor(ctx.args.workers) as ex:
        rows = list(ex.map(fill, rows))
    # 引用次数：一次 rg，在工作区（排除成品目录自身、.git、node_modules）里搜每项的“区分度够的名字”
    pats = {}
    for r in rows:
        base = os.path.basename(r["path"])
        # 用“上级/本级”相对路径做键：runtime、assets 这类通用名单独搜会被大量误计
        key = r["rel"] if "/" in r["rel"] else base
        pats[key] = r
        r["ref_key"] = key
    ref_root = ref_root or os.path.dirname(root)
    partial = False
    counts = {}
    if pats:
        pf = os.path.join(ctx.args.out, "video-ref-patterns.txt")
        open(pf, "w", encoding="utf-8").write("\n".join(pats) + "\n")
        rc, out, _ = run(["rg", "-o", "-F", "--no-filename", "--no-line-number", "-f", pf, "--max-filesize", "4M",
                          "-g", "!" + os.path.basename(root) + "/**", "-g", "!.git", "-g", "!node_modules",
                          "-g", "!*.mp4", "-g", "!*.mov", "-g", "!*.png", "-g", "!*.jpg", ref_root], timeout=timeout)
        partial = rc == 124 or rc == 127
        for ln in out.splitlines():
            counts[ln] = counts.get(ln, 0) + 1
    for r in rows:
        r["refs"] = counts.get(r["ref_key"], 0) if not partial or counts else None
        r["suggest"] = "建议用户确认（作废且 0 引用）" if r["class"] == "作废" and r["refs"] == 0 else ""
    ctx.mark("video_products", t)
    for r in rows:   # 深层作废目录的体积已含在它所属的第二层目录里，合计时不重复
        r["nested"] = bool(r.get("deep_void"))
    return {"root": root, "rows": sorted(rows, key=lambda x: -(x["bytes"] or 0)), "refs_partial": partial, "void_partial": vd_partial}


def worktree_breakdown(ctx, rows, prefer=LOCAL["prefer_worktree"], timeout=120):
    """最大（优先名字含 prefer）的链接 worktree 拆解，只读。"""
    cand = [r for r in rows if r.get("path") and os.path.isdir(r["path"])]
    if not cand:
        return None
    pick = [r for r in cand if prefer and prefer in r["path"]] or cand
    w = max(pick, key=lambda r: r.get("bytes") or 0)
    p, repo = w["path"], w["repo"]
    t = time.monotonic()
    res = {"path": p, "repo": repo, "bytes": w.get("bytes"), "partial": [], "backup": backup_status(p)}
    rc, out, _ = run(["git", "-C", p, "status", "--porcelain", "--untracked-files=all"], timeout=timeout)
    if rc == 124:
        res["partial"].append("git status 超时")
    ls = [l for l in out.splitlines() if l.strip()]
    res["tracked_changes"] = sum(1 for l in ls if not l.startswith("??"))
    res["untracked"] = sum(1 for l in ls if l.startswith("??"))
    rc, out, _ = run(["git", "-C", p, "count-objects", "-vH"], timeout=60)
    res["count_objects"] = dict(l.split(": ", 1) for l in out.splitlines() if ": " in l) if rc == 0 else None
    res["ignored_bytes"] = w.get("ignored_bytes")
    res["ignored_paths"] = w.get("ignored_paths")
    res["ignored_error"] = w.get("ignored_error")
    ts = time.monotonic()
    res["stale_subtasks"] = stale_subtasks(ctx, p, stale_days=getattr(ctx.args, "stale_days", 7))
    ctx.mark("stale_subtasks", ts)
    tops = []
    for n in sorted(os.listdir(p)):
        q = os.path.join(p, n)
        if n == ".git" or os.path.islink(q):
            continue
        b = ctx.sizes.get(q)
        if b is None:
            rc, o, _ = run(["du", "-xsk", q], timeout=timeout)
            b = int(o.split()[0]) * 1024 if o.split() else None
            if rc == 124:
                res["partial"].append("du %s 超时" % n)
        m = os.path.join(repo, n)
        mb = None
        if os.path.exists(m):
            mb = ctx.sizes.get(m)
            if mb is None:
                rc, o, _ = run(["du", "-xsk", m], timeout=timeout)
                mb = int(o.split()[0]) * 1024 if o.split() else None
        tops.append({"name": n, "bytes": b, "main_bytes": mb, "measured_at": datetime.now().strftime("%H:%M:%S"),
                     "rendering": under_any(m, getattr(ctx, "rendering", []))})
    res["tops"] = sorted(tops, key=lambda x: -(x["bytes"] or 0))[:20]
    ctx.mark("worktree_breakdown", t)
    return res


def unclassified(ctx, claimed):
    """du 结果里最深、≥ min-gb、未被任何档覆盖的目录（先宽后深的“最大处”）。"""
    big = {p: b for p, b in ctx.sizes.items() if b is not None and b >= ctx.args.min_gb * GB and p != HOME and p.startswith(HOME + "/")}
    leaves = []
    for p, b in big.items():
        if any(p == c or p.startswith(c + "/") or c.startswith(p + "/") for c in claimed):
            continue
        kids = [q for q in big if q.startswith(p + "/")]
        if kids and sum(big[k] for k in kids if os.path.dirname(k) == p) >= 0.6 * b:
            continue  # 主要体积在子目录里，报更深的那层
        if "/huggingface/hub/blobs" in p or p.endswith("/huggingface/hub"):
            continue  # HF 共享 blob 存储：与“HF 模型”行重复计量，只在模型行旁注明
        leaves.append({"path": p, "bytes": b, "open": ctx.open_under(p), "keep_hint": ctx.keep_reason(p)})
    return sorted(leaves, key=lambda x: -x["bytes"])[:40]


def _leaf_growth(rows):
    """去掉被子项覆盖的父项（父增长主要来自已列子项时只报子项），避免合计重复。"""
    rows = sorted(rows, key=lambda x: len(x["path"]), reverse=True)
    keep = []
    for r in rows:
        kids = [k for k in keep if k["path"].startswith(r["path"] + "/")]
        rest = r["delta"] - sum(k["delta"] for k in kids)
        if kids and rest < 0.5 * GB:
            continue
        if kids:
            r = dict(r, delta=rest, note="除已列子项外")
        keep.append(r)
    return sorted(keep, key=lambda x: -x["delta"])


def growth(ctx, prev_path, df):
    """(a) 对比 --prev：新出现目录 + 已有目录增长（≥ min-gb），并给已列合计 vs 磁盘变化差额。"""
    if not prev_path or not os.path.exists(prev_path):
        return None
    try:
        pj = json.load(open(prev_path, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    prev = pj.get("du_sizes", {})
    th = ctx.args.min_gb * GB
    new = [{"path": p, "bytes": b, "delta": b, "kind": "new"} for p, b in ctx.sizes.items()
           if b and b >= th and p not in prev and os.path.dirname(p) in prev]
    grown = [{"path": p, "bytes": b, "before": prev[p], "delta": b - prev[p], "kind": "grown"}
             for p, b in ctx.sizes.items() if b and p in prev and b - prev[p] >= th]
    # 新目录的父目录增长里已含新目录，先从父增长里扣掉
    rows = _leaf_growth(new + grown)
    rend = getattr(ctx, "rendering", [])
    for r in rows:
        if under_any(r["path"], rend):
            r["rendering"] = True
            r["note"] = ((r.get("note") or "") + "；在渲染（波动），不计入增长").lstrip("；")
    listed = sum(r["delta"] for r in rows if not r.get("rendering"))
    pdf = (pj.get("df") or {}).get("used")
    ddf = df["used"] - pdf if pdf is not None else None
    gap = (ddf - listed) if ddf is not None else None
    return {"prev": prev_path, "prev_at": pj.get("created_at"), "new": [r for r in rows if r["kind"] == "new"],
            "grown": [r for r in rows if r["kind"] == "grown"], "listed_bytes": listed,
            "df_used_delta": ddf, "gap": gap, "gap_note": growth_gap_note(gap),
            "rendering_bytes": sum(r["delta"] for r in rows if r.get("rendering"))}


def df_both(path):
    """统一口径：/bin/df（BSD，本数据卷已用）。另记 PATH 里 GNU df 的值并解释差异。"""
    rc, out, _ = run(["/bin/df", "-k", path])
    f = out.splitlines()[-1].split()
    d = {"used": int(f[2]) * 1024, "avail": int(f[3]) * 1024, "total": int(f[1]) * 1024, "source": "/bin/df -k " + path, "device": f[0]}
    rc, out, _ = run(["df", "-k", path])   # PATH 里的 df（可能是 GNU coreutils），仅供参考
    try:
        g = out.splitlines()[-1].split()
        d["path_df"] = {"device": g[0], "used": int(g[2]) * 1024, "avail": int(g[3]) * 1024, "size": int(g[1]) * 1024, "mount": g[-1]}
    except (IndexError, ValueError):
        d["path_df"] = None
    return d


def df_note(df):
    """只陈述事实，不下未证实的因果结论。"""
    s = "口径：全报告统一用 `/bin/df`（BSD）读数据卷 %s 的已用/可用，单位 GiB。" % df["device"]
    g = df.get("path_df")
    if g and g["used"] != df["used"]:
        s += ("PATH 里的 df（%s，常为 Homebrew GNU coreutils）显示已用 %s，与 /bin/df 相差 %s；"
              "GNU df 的 Used ≈ Size − Avail，与 BSD df 口径不同，仅供参考，差异根因未证实，本报告不据此下结论。") % (
            g["device"], human(g["used"]), human(g["used"] - df["used"]))
    return s


def phases_text(ph):
    names = {"lsof": "lsof", "du_breadth": "并行 du", "tier1_checks": "第1档核对", "worktrees": "工作树",
             "video_products": "成品目录拆分", "worktree_breakdown": "worktree 拆解", "rendering": "在渲染探测", "archives": "存档核对",
             "stale_subtasks": "旧子任务", "dups": "重复副本", "archive_index": "存档定位索引"}
    return "、".join("%s %ss" % (names.get(k, k), v) for k, v in ph.items())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="/tmp/sansheng-triage/" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--quiet-hours", type=float, default=6, help="第1档默认静默期；卡片有更严阈值时取较严者")
    ap.add_argument("--min-gb", type=float, default=0.5)
    ap.add_argument("--min-tier1-mb", type=float, default=20)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--du-timeout", type=int, default=600)
    ap.add_argument("--prev", help="上次 triage.json，用于列“本次新出现的大目录”")
    ap.add_argument("--no-video", action="store_true", help="跳过成品目录拆分")
    ap.add_argument("--no-wt-breakdown", action="store_true", help="跳过最大 worktree 拆解")
    ap.add_argument("--stale-days", type=int, default=7, help="活跃工作树里多少天无写入的子任务算“停写的旧子任务”")
    ap.add_argument("--no-dups", action="store_true", help="跳过重复副本扫描")
    ap.add_argument("--archive-deep", action="store_true", help="同盘存档 tgz 逐文件对比原路径（解压流，慢；结果缓存在 $TMPDIR）")
    ap.add_argument("--update-latest", help="显式给出 latest-triage.json 链接路径时，指向本次 --out/triage.json（唯一的写操作，默认不写）")
    args = ap.parse_args()
    if os.path.realpath(args.out).startswith(os.path.realpath(HOME) + "/") and "--out" not in " ".join(sys.argv):
        sys.exit("只读模式拒绝写家目录")
    os.makedirs(args.out, exist_ok=True)
    ctx = Ctx(args)
    t = time.monotonic(); ctx.snapshot_lsof(); ctx.mark("lsof", t)
    df = df_both(HOME)
    t = time.monotonic(); ctx.rendering = rendering_dirs(); ctx.mark("rendering", t)
    breadth_first(ctx)
    t1, k1, t1_demoted, t1_wait = tier1(ctx)
    t2, k2, prune = worktrees(ctx)
    backup_state(); _BACKUP["fails"] = backup_failures()
    for r in t2 + k2:
        r["backup"] = backup_status(r["path"])
    claimed = [x["path"] for x in t1 + k1 + t1_wait + t2 + k2]
    t3_all = t1_demoted + tier3(ctx, claimed + [x["path"] for x in t1_demoted])
    t3 = [x for x in t3_all if not x.get("keep_hint")]
    t3_ref = [x for x in t3_all if x.get("keep_hint")]   # 受保护：只读参考，不与“需决定”混在一起
    un = unclassified(ctx, claimed + [x["path"] for x in t3_all])
    # 防误删红线：任何可删/待决定档都过一遍生产目录保护与引用检查
    t1, p1, r1 = apply_red_lines(t1)
    t2, p2, r2 = apply_red_lines(t2)
    t3, p3, r3 = apply_red_lines(t3)
    un, p4, r4 = apply_red_lines(un)
    protected_production, referenced_ask = p1 + p2 + p3 + p4, r1 + r2 + r3 + r4
    gr = growth(ctx, args.prev, df)
    nd = gr["new"] if gr else None
    vp = None if args.no_video or not VIDEO_ROOT else video_products(ctx)
    wb = None if args.no_wt_breakdown else worktree_breakdown(ctx, getattr(ctx, "wt_rows", []))
    keep_all = k1 + k2
    def enrich(x):
        if (x.get("bytes") or 0) >= ctx.args.min_gb * GB:
            x["holders"] = ctx.who_uses(x["path"])
            if "last_write" not in x:
                x["last_write"] = ctx.last_write(x["path"])
        if flow_artifact(x["path"]):
            x["flow_artifact"] = True
        return x
    with ThreadPoolExecutor(min(4, ctx.args.workers)) as ex:
        keep_all = list(ex.map(enrich, keep_all))
    flows = [x for x in keep_all + t2 + t3 + un if flow_artifact(x["path"])]
    t = time.monotonic(); arch = archive_check(deep=args.archive_deep); ctx.mark("archives", t)
    t = time.monotonic(); dups = None if args.no_dups else dup_scan(ctx); ctx.mark("dups", t)
    # 第三轮根因 8：扫描期间出现又消失的临时目录（如 App 内容更新快照 7.2G）不能当“新增大目录”或“未归类”
    vanished = []
    if gr:
        for k in ("new", "grown"):
            for r in gr[k]:
                if not os.path.exists(r["path"]):
                    r["vanished"] = True; vanished.append(r["path"])
        gone = sum(r["delta"] for k in ("new", "grown") for r in gr[k] if r.get("vanished") and not r.get("rendering"))
        if gone:
            gr["listed_bytes"] -= gone
            if gr["df_used_delta"] is not None:
                gr["gap"] = gr["df_used_delta"] - gr["listed_bytes"]
                gr["gap_note"] = growth_gap_note(gr["gap"])
    un_gone = [x["path"] for x in un if not os.path.exists(x["path"])]
    un = [x for x in un if os.path.exists(x["path"])]
    vanished += un_gone
    for x in t3 + t3_ref:
        x.setdefault("backup", backup_status(x["path"]))
    df_end = df_both(HOME)
    elapsed = round(time.monotonic() - ctx.t0, 1)
    dtop = denied_top(ctx.denied)
    res = {"schema": "triage/2", "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "units": "GiB (1024^3)", "elapsed_seconds": elapsed, "phases": ctx.phase, "df": df, "tmpdir": TMP,
           "df_end": df_end, "rendering": ctx.rendering, "archives": arch, "backup_failures": _BACKUP.get("fails"),
           "cleanup_log": CLEANUP_LOG if CLEANUP_LOG and os.path.isfile(CLEANUP_LOG) else None,
           "tier1": t1, "tier1_wait_app": t1_wait, "tier2": t2, "tier2_prune": prune, "tier3": t3, "tier3_protected_ref": t3_ref, "protected_production": protected_production, "referenced_ask": referenced_ask,
           "keep": keep_all, "flow_artifacts": [x["path"] for x in flows], "growth": gr, "unclassified": un,
           "video_products": vp, "worktree_breakdown": wb, "duplicates": dups, "vanished_during_scan": sorted(set(vanished)),
           "new_dirs": nd, "coverage": {"denied_count": len(ctx.denied), "denied_samples": ctx.denied[:15], "denied_top": dtop,
                                        "du_timeouts": ctx.du_timeouts, "lsof_ok": ctx.lsof_ok},
           "du_sizes": {p: b for p, b in ctx.sizes.items() if b and b >= 100 * 1024 * 1024}}
    json.dump(res, open(os.path.join(args.out, "triage.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    md = render(res, args)
    open(os.path.join(args.out, "triage.md"), "w", encoding="utf-8").write(md)
    print(os.path.join(args.out, "triage.md"))
    if args.update_latest:   # 显式要求才写：让 latest 指向“本次输出目录”，避免指错日期
        lk = os.path.abspath(os.path.expanduser(args.update_latest))
        tmp = lk + ".tmp-%d" % os.getpid()
        os.symlink(os.path.relpath(os.path.join(args.out, "triage.json"), os.path.dirname(lk)), tmp)
        os.replace(tmp, lk)
        print("latest 链接 → %s" % os.readlink(lk))
    s = lambda xs: sum((x.get("bytes") or 0) for x in xs)
    print("耗时 %ss；第1档 %s（%d 项，另 %d 项待 App 退出）；第2档 %s（%d 项）；第3档 %s；保留 %d 项" % (
        elapsed, human(s(t1)), len(t1), len(t1_wait), human(s(t2)), len(t2), human(s(t3)), len(keep_all)))
    print("下一步（SKILL.md 要求每次扫描后运行 probe projects；它依赖 scan.py 的 facts.json 并写 <run>/projects.json，triage 没有 facts，故不代跑）：")
    print("  python3 scripts/scan.py scan --read-only && python3 scripts/scan.py probe projects --run <上一步打印的 run 目录>")


def _lw(x):
    w = x.get("last_write")
    return ("%s `%s`%s" % (w["time"].replace("T", " ")[:16], display(w["file"])[-50:], "（超时，可能更晚）" if w["partial"] else "")) if w else "—"


def _who(x):
    hs = x.get("holders") or []
    return "<br>".join("%s PID %d（%d 个文件）`%s`" % (h["name"], h["pid"], h["files"], h["cmdline"][:90].replace("|", "\\|")) for h in hs) or "—"


def render(res, args):
    s = lambda xs: sum((x.get("bytes") or 0) for x in xs)
    df, t1, tw, t2, t3, t3r = res["df"], res["tier1"], res["tier1_wait_app"], res["tier2"], res["tier3"], res["tier3_protected_ref"]
    keep_all, gr, un, vp, wb = res["keep"], res["growth"], res["unclassified"], res["video_products"], res["worktree_breakdown"]
    cov = res["coverage"]
    L = ["# 叁笙清理 · 快速分档体检（只读）", "",
         "时间 %s；耗时 %ss（%s）；数据卷已用 %s / 可用 %s（/bin/df）" % (
             res["created_at"], res["elapsed_seconds"], phases_text(res["phases"]), human(df["used"]), human(df["avail"])),
         "", df_note(df),
         "", "**开头/结尾 df**：开头已用 %s / 可用 %s → 结尾已用 %s / 可用 %s（扫描期间变化 %s%s）" % (
             human(df["used"]), human(df["avail"]), human(res["df_end"]["used"]), human(res["df_end"]["avail"]),
             "+" if res["df_end"]["used"] >= df["used"] else "-", human(abs(res["df_end"]["used"] - df["used"]))),
         "", "**在渲染/波动**（最近 10 分钟有写入的生产目录，大小与增长不可信、不计入增长）：" + ("；".join("`%s`" % display(d) for d in res["rendering"]) or "无"),
         "", ("清理日志：`%s` 存在" % display(res["cleanup_log"])) if res.get("cleanup_log") else "**⚠ 清理日志 `%s` 不存在（红线 5 未落地）**" % display(CLEANUP_LOG or ""), "",
         "第1档统一规则：白名单类别 + 无进程打开 + 对应 App 未运行 + 静默期无写入（默认 %g 小时；卡片更严时取较严者，如视频渲染/Library/Caches 24 小时）。"
         "每项先跑“检查”命令（无输出才继续），再跑“删除”命令；删除命令原生优先，其余为 `find -x DIR -mindepth 1 -delete`（保留目录本身、不跟随链接），不使用 `rm -rf`。" % args.quiet_hours,
         "", "## 第 1 档 可再生缓存 · 合计 %s（用户批准后执行）" % human(s(t1)), "",
         "| 类别 | 路径 | 大小 | 最近写入 | 静默期 | 检查（先跑） | 删除（后跑） |", "|---|---|---|---|---|---|---|"]
    L += ["| %s | `%s` | %s | %s | %gh | `%s` | `%s` |" % (x["category"], display(x["path"]), human(x["bytes"]), _lw(x), x["quiet_hours"],
          x["check_command"].replace(x["path"], display(x["path"])).replace("|", "\\|"), x["command"]) for x in sorted(t1, key=lambda x: -(x["bytes"] or 0))] or ["| — | 无 | | | | | |"]
    L += ["", "### 待 App 退出后（App 运行中，删前需退出；不在一键清单里）· 合计 %s" % human(s(tw)), ""]
    L += ["- %s `%s` — **App 运行中（%s），删前需退出**；最近写入 %s；退出后：检查 `%s`，删除 `%s`" % (
        human(x["bytes"]), display(x["path"]), x["app_running"], _lw(x), x["check_command"], x["command"]) for x in sorted(tw, key=lambda x: -(x["bytes"] or 0))] or ["- 无"]
    L += ["", "## 第 2 档 工作树 · 合计 %s（locked/未提交/进程/写入/远端 全部通过；不删分支）" % human(s(t2)), "",
          "| 路径 | 大小 | 分支 | 命令 | 备注 |", "|---|---|---|---|---|"]
    L += ["| `%s` | %s | %s | `%s` | %s；备份：%s |" % (display(x["path"]), human(x["bytes"]), x["branch"], x["command"], x["note"], x.get("backup", "?")) for x in t2]
    L += ["- `%s`" % x["command"] for x in res["tier2_prune"]]
    if wb:
        L += ["", "### 最大 worktree 拆解：`%s`（只读%s）" % (display(wb["path"]), "；**部分**：" + "、".join(wb["partial"]) if wb["partial"] else ""), "",
              "- 总大小 %s；已跟踪变更 %d 个；未跟踪 %d 个；被忽略 %s（%s）" % (
                  human(wb["bytes"]), wb["tracked_changes"], wb["untracked"],
                  ("未算出：" + wb["ignored_error"]) if wb.get("ignored_error") else human(wb["ignored_bytes"]), ", ".join(wb["ignored_paths"] or [])[:120]),
              "- `git count-objects -vH`（对象库与主仓共享）：%s" % ("、".join("%s=%s" % (k, v) for k, v in (wb["count_objects"] or {}).items() if k in ("count", "size", "in-pack", "size-pack", "garbage")) or "—"),
              "- 备份：%s" % wb.get("backup", "?"),
              "", "| 一级目录 | worktree | 主仓同名 | 比值 | 量取时间 |", "|---|---|---|---|---|"]
        L += ["| %s | %s | %s%s | %s | %s |" % (t["name"], human(t["bytes"]), human(t["main_bytes"]) if t["main_bytes"] is not None else "主仓无",
              "（在渲染，波动）" if t.get("rendering") else "",
              ("%.1f" % (t["bytes"] / t["main_bytes"])) if t["bytes"] and t["main_bytes"] else "—", t.get("measured_at", "")) for t in wb["tops"]]
    if wb and wb.get("stale_subtasks"):
        st = wb["stale_subtasks"]
        L += ["", "### 活跃工作树里停写的旧子任务 · Git 不管的部分合计 %s（工作树整体仍保留；已跟踪文件在仓库里，不动）" % human(sum(x["untracked_bytes"] for x in st)), "",
              "判据：≥1 GiB、最近 %d 天无真实写入、不在渲染。只给 `git clean -x` 命令卡：先跑预览，确认列出的都是过程件，再跑删除。问创建它的 Agent 或用户。" % getattr(args, "stale_days", 7), "",
              "| 目录 | 总大小 | 可清（未跟踪+忽略） | 最近写入 | 预览（先跑） | 删除（后跑） | 备注 |", "|---|---|---|---|---|---|---|"]
        L += ["| `%s` | %s | %s（%d 个文件） | %s | `%s` | `%s` | %s；备份：%s |" % (
            display(x["path"]), human(x["bytes"]), human(x["untracked_bytes"]), x["untracked_files"], _lw(x),
            x["check_command"].replace("|", "\\|"), x["command"], x["keep_hint"] or "—", x["backup"]) for x in st]
    L += ["", "## 第 3 档 需用户判断 · 合计 %s" % human(s(t3)), "", "| 类别 | 路径 | 大小 | 说明 |", "|---|---|---|---|"]
    L = L[:-2] + ["| 类别 | 路径 | 大小 | 说明 | 备份 |", "|---|---|---|---|---|"] if L[-1] == "|---|---|---|---|" else L
    L += ["| %s | `%s` | %s | %s%s | %s |" % (x["category"], display(x["path"]), human(x["bytes"]), x["note"],
          "；仅历史记录提到 %d 处（不算依赖）" % x["history_refs"] if x.get("history_refs") else "", x.get("backup", "?")) for x in sorted(t3, key=lambda x: -(x["bytes"] or 0))]
    if t3r:
        L += ["", "### 只读参考（受保护，不给删除，只帮你看清占用）", ""]
        L += ["- %s `%s` — %s（%s）；备份：%s" % (human(x["bytes"]), display(x["path"]), x["note"], x["keep_hint"], x.get("backup", "?")) for x in sorted(t3r, key=lambda x: -(x["bytes"] or 0))]
    if res.get("archives"):
        L += ["", "### 同盘存档 tgz 核对（tar -tvzf 对比原路径；同盘 tgz 不算备份）", "",
              "| tgz | 大小 | 原路径 | 判定 | tgz 盘外备份 |", "|---|---|---|---|---|"]
        L += ["| `%s` | %s | %s | %s | %s |" % (display(a["path"]), human(a["bytes"]), "`%s`" % display(a["orig"]) if a.get("orig") else "—",
              a["status"] + ("（缺失文件中 %d 个盘外备份无记录）" % a["missing_backup"] if a.get("missing_backup") else ""), a["tgz_backup"]) for a in res["archives"]]
    od = [a for a in (res.get("archives") or []) if a.get("offdisk_ok")]
    if od:
        L += ["", "### 盘外已备份的同盘存档 · 合计 %s（本地这份可删）" % human(sum(a["bytes"] for a in od)), "",
              "同盘存档本身不腾空间（规则：存档必须放外接盘或云端，不得留在本盘）。这些 tgz 在盘外备份台账里有同路径、同大小的副本，"
              "本地删除后从网盘同路径取回。先跑检查（输出应等于台账大小），再跑删除；用户批准后执行。", "",
              "| tgz | 大小 | 检查（先跑） | 删除（后跑） |", "|---|---|---|---|"]
        L += ["| `%s` | %s | `%s` | `%s` |" % (display(a["path"]), human(a["bytes"]), a["check_command"], a["command"]) for a in od]
    if vp:
        L += ["", "### 需用户判断的成品清单（`%s` 整体仍受保护；只列，不给删除命令）%s" % (display(vp["root"]), "；引用计数**部分**（搜索超时）" if vp["refs_partial"] else ""), "",
              "| 分类 | 目录 | 大小 | 最近写入 | 被引用次数 | 提示 |", "|---|---|---|---|---|---|"]
        order = {"作废": 0, "试做/重制/v1-v3/旧版": 1, "已交付/交付": 2, "进行中（24 小时内有写入）": 3, "当日产出": 4, "其他": 5}
        for x in sorted(vp["rows"], key=lambda x: (order.get(x["class"], 9), -(x["bytes"] or 0))):
            L.append("| %s | `%s` | %s | %s | %s | %s |" % (x["class"] + ("（深层）" if x.get("deep_void") else ""), x["rel"], human(x["bytes"]), _lw(x),
                     "?" if x["refs"] is None else x["refs"], x["suggest"]))
        L += ["", "合计（按分类；深层作废目录已含在上级目录里，不重复计）：" + "；".join("%s %s" % (c, human(sum(r["bytes"] or 0 for r in vp["rows"] if r["class"] == c and not r.get("nested")))) for c in order if any(r["class"] == c for r in vp["rows"]))]
        dv = [r for r in vp["rows"] if r.get("deep_void")]
        if dv:
            L += ["", "其中深层“作废”目录 %d 个、合计 %s（名字带作废标记；0 引用的可请用户确认后删除）。" % (len(dv), human(sum(r["bytes"] or 0 for r in dv)))]
    dp = res.get("duplicates")
    if dp and dp["dup_files"]:
        L += ["", "## 重复副本（APFS 克隆去重，不删任何路径）· 名义可共享 %s（%d 组、%d 个副本，≥20MB）" % (human(dp["nominal_bytes"]), dp["groups"], dp["dup_files"]), "",
              "同一内容在多个目录各存一份（常见：成品库 / 交付 / 分发暂存 / 工作树）。删副本会破坏交付目录和流水线路径；"
              "改成 APFS 克隆后每个路径照常可用，磁盘只占一份。这里是抽样哈希，`plan` 会全量 sha256 复核；已经是克隆的不会再腾空间，以执行后实测为准。", "",
              "| 副本所在位置 | 名义可共享 |", "|---|---|"]
        L += ["| `%s` | %s |" % (k, human(v)) for k, v in dp["by_location"]]
        L += ["", "示例（保留 ← 副本）："] + ["- `%s` ← `%s`（%s）" % (display(x["keep"]), display(x["dup"]), human(x["size"])) for x in dp["samples"]]
        L += ["", "命令（用户批准后按顺序跑）：", "", "1. 计划（只读，全量哈希）：`%s`" % dp["plan_command"],
              "2. 预演：`%s`" % dp["dry_command"], "3. 执行：`%s`" % dp["command"]]
    pp, ra = res.get("protected_production") or [], res.get("referenced_ask") or []
    if pp:
        L += ["", "## 受保护·生产目录（只展示，不可删；红线 1）", ""]
        L += ["- %s `%s` — %s；备份：%s" % (human(x.get("bytes")), display(x["path"]), x["red_line"], x.get("backup", "?")) for x in pp]
    if ra:
        L += ["", "## 被引用·需逐项问用户（红线 2；不给删除命令）", ""]
        L += ["- %s `%s` — %s；备份：%s" % (human(x.get("bytes")), display(x["path"]), x["red_line"], x.get("backup", "?")) for x in ra]
    L += ["", "## 保留（在用/受保护，绝不进 1/2 档）", "", "| 路径 | 大小 | 原因 | 谁在用（lsof 快照） | 最近写入 |", "|---|---|---|---|---|"]
    L += ["| `%s` | %s | %s%s | %s | %s |" % (display(x["path"]), human(x.get("bytes")), x["reason"],
          "；**%s**" % FLOW_NOTE if x.get("flow_artifact") else "", _who(x), _lw(x))
          for x in sorted(keep_all, key=lambda x: -(x.get("bytes") or 0))]
    flows = [x for x in keep_all + t2 + t3 + un if flow_artifact(x["path"])]
    L += ["", "## 正在合并/部署的临时 worktree 与构建槽（流程产物，流程结束后应由创建者删除）", ""]
    L += ["- %s `%s` — %s；谁在用：%s；最近写入：%s" % (human(x.get("bytes")), display(x["path"]),
          ("可删：`%s`" % x["command"]) if x.get("command") else ("保留：" + (x.get("reason") or x.get("note") or "")),
          _who(x).replace("<br>", "；"), _lw(x)) for x in sorted(flows, key=lambda x: -(x.get("bytes") or 0))] or ["- 无"]
    L += ["", "## 未归类大目录（先宽后深的最大处，需调查/身份卡）", ""]
    L += ["- %s `%s`%s%s" % (human(x["bytes"]), display(x["path"]), "（%d 个打开文件）" % x["open"] if x["open"] else "", "（%s）" % x["keep_hint"] if x["keep_hint"] else "") for x in un]
    if gr is not None:
        L += ["", "## 增长来源（对比 --prev %s）" % (gr["prev_at"] or gr["prev"]), "", "### 新出现的大目录", ""]
        L += ["- %s `%s`%s" % (human(x["bytes"]), display(x["path"]), "（已消失：扫描中的临时产物，不计入合计）" if x.get("vanished") else "") for x in gr["new"][:30]] or ["- 无"]
        L += ["", "### 已有目录的增长（≥ %g GiB）" % args.min_gb, ""]
        L += ["- +%s `%s`（%s → %s%s）" % (human(x["delta"]), display(x["path"]), human(x["before"]), human(x["bytes"]),
              "，" + x["note"] if x.get("note") else "") for x in gr["grown"][:30]] or ["- 无"]
        sg = lambda v: ("%s%s" % ("+" if v >= 0 else "-", human(abs(v)))) if v is not None else "?"
        L += ["", "**已列合计 +%s vs 磁盘变化（/bin/df 已用）%s；差额 %s** —— %s" % (
            human(gr["listed_bytes"]), sg(gr["df_used_delta"]) if gr["df_used_delta"] is not None else "（基线无 df）",
            sg(gr["gap"]), gr.get("gap_note") or growth_gap_note(gr["gap"]))]
        if gr.get("rendering_bytes"):
            L += ["", "另有在渲染目录的波动 %s 未计入上面的合计。" % human(gr["rendering_bytes"])]
    else:
        L += ["", "本次新出现的大目录：无可比基线（未给 --prev）。"]
    if res.get("vanished_during_scan"):
        L += ["", "扫描期间出现又消失的目录（临时产物，不计入增长与未归类）：" + "；".join("`%s`" % display(v) for v in res["vanished_during_scan"][:10])]
    bf = res.get("backup_failures") or []
    L += ["", "## 盘外备份失败清单（只读解析日志）", ""]
    for g in bf:
        L += ["- `%s`：%s" % (display(g["log"]), g["summary"] or "无汇总行")]
        L += ["  - 失败：`%s` — %s" % (f["path"], f["error"][:80]) for f in g["failed"]]
    if not bf:
        L += ["- 未配置或未找到备份日志"]
    L += ["", "## 覆盖缺口", "", "- 无权限 %d 处（这些不是 0）。**给终端“完全磁盘访问”（系统设置 › 隐私与安全性）后重扫**。按目录归类 Top 10：" % cov["denied_count"]]
    L += ["  - `%s` × %d" % (display(k) if k.startswith("/") else k, n) for k, n in cov.get("denied_top") or []] or ["  - 无"]
    L += ["- du 超时：%s；lsof：%s" % (", ".join(map(display, cov["du_timeouts"])) or "无", "正常" if cov["lsof_ok"] else "不可用")]
    L += ["", "## 下一步", "", "- SKILL.md 要求每次扫描后运行 `probe projects`；它依赖 scan.py 的 facts.json 并写 `<run>/projects.json`（不加 --run 会写进家目录最近一次 run），triage 不产生 facts，所以只提示不代跑；用 `--read-only` 扫描时 run 落在 /tmp：",
          "  `python3 scripts/scan.py scan --read-only` 后 `python3 scripts/scan.py probe projects --run <run目录>`"]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
