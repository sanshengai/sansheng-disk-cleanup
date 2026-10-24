"""保护名单与路径边界：scan / plan / act 共用，Agent 不能放行。规则见 references/contract.md 第 5 节。"""
import fnmatch
import hashlib
import json
import os
import pwd
import re


def _passwd_home():
    """当前用户的家目录：只信系统用户库（pwd），不信环境变量 HOME（它可以被调用者随手改成 / 之类）。"""
    return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)


# 测试专用注入点：模块级常量，测试用 monkeypatch.setattr(protect, "HOME", tmp) 覆盖；
# 生产环境没有任何环境变量或参数能改它。
HOME = _passwd_home()
STATE_DIR = os.path.join(HOME, ".sansheng", "disk-cleanup")
PROFILE = os.path.join(STATE_DIR, "profile.json")

SYSTEM = ["/", "/System", "/usr", "/bin", "/sbin", "/private/var/db", "/Library", "/Applications",
          "/etc", "/private/etc"]
# 家目录下：(相对路径或 glob, 是否连子路径一起保护)
HOME_RULES = [
    ("Documents", True), ("Desktop", True), ("Pictures", True),
    ("Library/Mobile Documents", True), ("Library/Mail", True), ("Library/Messages", True),
    ("Library/Keychains", True), ("Library/Photos", True),
    (".ssh", True), (".gnupg", True), (".aws", True), (".config", True), (".netrc", True),
    (".claude.json", True), (".codex/auth.json", True), (".claude/projects/*/memory", True),
    # iOS 设备备份，可能是唯一一份备份，删了无法重来
    ("Library/Application Support/MobileSync", True),
    # 废纸篓目录本身不可删；其内文件是否可删本版不开放，统一保护
    (".Trash", True),
]
# 固定系统位置：(路径, 是否连子路径一起保护, 原因)
FIXED_RULES = [
    # 虚拟内存/休眠镜像（sleepimage、swapfile），运行中删除会导致系统异常
    ("/private/var/vm", True, "虚拟内存与休眠镜像 /private/var/vm"),
    ("/var/vm", True, "虚拟内存与休眠镜像 /var/vm"),
]
USERS_ROOT = "/Users"
VOLUMES_ROOT = "/Volumes"
# 任意卷、任意深度按名称保护的系统/备份目录（Time Machine、Spotlight 索引、文件事件、文档版本、本地备份）
VOLUME_META = (".trashes", "backups.backupdb", ".spotlight-v100", ".fseventsd", ".documentrevisions-v100",
               ".mobilebackups")
# Claude Code 会话临时目录：TMP_BASE/claude-<uid>。测试用 monkeypatch 覆盖 TMP_BASE，生产里是固定值。
# 其下的会话子目录允许清理；根本身、TMP_BASE 其他内容、当前会话（环境变量里的会话 id）一律保护。
TMP_BASE = "/private/tmp"
SESSION_ENV_VARS = ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")
# 只保护目录本身及其直接子项，深层子目录可由用户点名（契约第 5 节的 Movies 例外）
SHALLOW = ["Movies"]


def claude_tmp_root():
    """/private/tmp/claude-<uid>（词法路径，不要求存在）。"""
    return os.path.join(TMP_BASE, "claude-%d" % os.getuid())


def current_session_ids():
    """当前 Claude 会话 id（环境变量给出；太短的值不当 id 用，免得误伤）。"""
    out = []
    for k in SESSION_ENV_VARS:
        v = os.environ.get(k, "").strip()
        if len(v) >= 8 and v not in out:
            out.append(v)
    return out


def expand(path):
    """展开 ~：当前用户的 ~ 一律用 HOME 常量（来自系统用户库），不看环境变量 HOME。"""
    if path == "~" or path.startswith("~/"):
        return HOME + path[1:]
    return os.path.expanduser(path)


def real(path):
    return os.path.realpath(expand(path))


def broad_roots():
    """太宽泛、不能当成品位置（或引用证据根）的目录：系统顶层、家目录本身、家目录的各级父目录。"""
    home = os.path.realpath(HOME)
    out = {"/", "/Applications", "/Users", "/Library", "/System", "/usr", "/opt", "/private", "/var", "/tmp",
           "/Volumes", "/bin", "/sbin", "/etc", "/cores", home}
    d = os.path.dirname(home)
    while d and d != "/":
        out.add(d)
        d = os.path.dirname(d)
    return out


# 家目录里装的是“一大类东西”而不是某个项目成品的位置：存成品目录会让“有副本”的判断和引用搜索范围失去意义
_HOME_BROAD = ("Library", "Library/Caches", "Library/Application Support", "Library/Containers", "Library/Logs",
               "Downloads", "Desktop", "Documents", "Movies", "Pictures", "Music", "Public", "Applications",
               ".cache", ".config", ".local", ".Trash")


def broad_deliverable_roots():
    home = os.path.realpath(HOME)
    return broad_roots() | {os.path.join(home, r) for r in _HOME_BROAD}


def is_broad_deliverable(path):
    """路径本身（不含其下的子目录）是不是太宽泛的成品位置。词法路径与真实路径任一命中即算。"""
    broad = {b.casefold() for b in broad_deliverable_roots()}
    return any(x.rstrip("/").casefold() in broad or x.casefold() in broad for x in (lexical(path), real(path)) if x)


def home_problem():
    """环境里的 HOME 与系统记录的家目录不一致（或家目录本身不合理）时返回说明，否则 None。
    plan.py check/seal 与 act.py run 都先调它：HOME=/ 之类会让依赖家目录的豁免规则整体失效，直接拒绝。"""
    if not HOME or HOME in ("/", USERS_ROOT):
        return f"系统记录的家目录异常（{HOME or '空'}），拒绝运行"
    env_home = os.environ.get("HOME")
    if env_home is not None and (not env_home or os.path.realpath(env_home).casefold() != HOME.casefold()):
        return (f"环境变量 HOME={env_home!r} 与系统记录的家目录 {HOME} 不一致；保护名单只认系统记录的家目录，"
                "为避免规则失效，拒绝运行。请在正常登录环境里重新运行（不要覆盖 HOME）")
    return None


# ---------------------------------------------------------------- 项目台账（0.5）
# profile.projects = {项目键: {name, status, deliverables[], updated_at, source}}；项目键 = 规范化绝对路径，
# 环境项目用 env:android 这类。状态含义见 references/judgment.md“项目状态怎么用”。
PROJECT_STATUSES = ("active", "done", "archived", "protected")
PROJECT_STATUS_ZH = {"active": "进行中", "done": "做完了", "archived": "归档", "protected": "别碰"}
PROJECT_STATUS_FROM_ZH = {v: k for k, v in PROJECT_STATUS_ZH.items()}
# 环境项目没有单一的项目目录：键 → 它占的位置（家目录相对）。标“别碰”时这些位置并入保护名单。
ENV_PROJECT_ROOTS = {
    "env:android": ["Library/Android", ".android", ".local/share/android-sdk", ".gradle"],
    "env:xcode": ["Library/Developer"],
    "env:docker": ["Library/Containers/com.docker.docker", ".docker"],
    "env:models": [".cache/huggingface", ".ollama", ".cache/whisper", ".cache/torch", ".lmstudio", ".cache/lm-studio"],
}
ENV_PROJECT_NAMES = {"env:android": "安卓开发环境", "env:xcode": "Xcode 与模拟器", "env:docker": "Docker",
                     "env:models": "本地模型缓存"}


def project_roots(key):
    """项目键对应的目录列表（绝对路径，词法规范化）；未知的 env: 键返回空列表。"""
    if not isinstance(key, str) or not key:
        return []
    if key.startswith("env:"):
        return [os.path.join(HOME, r) for r in ENV_PROJECT_ROOTS.get(key, [])]
    return [lexical(key)]


def _apply_projects(data):
    """把台账折进有效 profile：别碰项目的目录并入 protect；各项目自己的成品目录并入 deliverable_roots 与 reference_roots
    （副本核对、引用检查范围）。只用于内存里的有效 profile，不写回文件（prefs 改写 profile 时读原始内容）。"""
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return
    prot, deliv = [], []
    for key, e in projects.items():
        if not isinstance(e, dict):
            continue
        if e.get("status") == "protected":
            prot += project_roots(key)
        deliv += [d for d in (e.get("deliverables") or []) if isinstance(d, str) and d and not is_broad_deliverable(d)]

    def merge(name, extra):
        cur = data.get(name) if isinstance(data.get(name), list) else []
        data[name] = list(dict.fromkeys(list(cur) + extra))
    if prot:
        merge("protect", prot)
    if deliv:
        merge("deliverable_roots", deliv)
        merge("reference_roots", deliv)


def load_profile(path=PROFILE, raw=False):
    """读 profile。raw=False（默认）返回“有效 profile”：台账里的别碰目录与成品目录已折进 protect / deliverable_roots /
    reference_roots，scan / plan / act 一律用它；prefs 要改写 profile 时用 raw=True，免得把折进去的内容写回文件。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("roots", ["~"])
    data.setdefault("extra_roots", [])
    data.setdefault("reference_roots", [])
    data.setdefault("protect", [])
    data.setdefault("notes", {})
    if not raw:
        _apply_projects(data)
    return data


def ref_roots_real(profile):
    """有效 profile 的引用检查目录：规范化后的真实路径列表，只留实际存在的目录（search_references 也只搜这些）。"""
    out = []
    for r in (profile.get("reference_roots") if isinstance(profile, dict) else None) or []:
        if not isinstance(r, str) or not r:
            continue
        root = real(r)
        if os.path.isdir(root) and root not in out:
            out.append(root)
    return out


def roots_uncovered(wanted, searched):
    """wanted 里没有被 searched 覆盖的根（相同或落在某个已搜根之下才算覆盖）。"""
    have = [str(x).rstrip("/") for x in (searched or []) if isinstance(x, str) and x]
    return [w for w in wanted if not any(w.rstrip("/") == h or w.startswith(h + "/") for h in have)]


# 设置时序闸门（0.5）：这些字段会改变 check 的判断或 act 的保护名单；seal 时记下摘要，
# 执行时发现现在的设置与封存时不同就拒绝。rules / cooldown / default_choice / notes / onboarded_at
# 只影响提案档位和文案，不影响 check 与执行的判断，不在其内（否则“以后都删”的回复会挡住本轮执行）。
PROFILE_GUARD_FIELDS = (
    ("roots", "扫描根目录"), ("extra_roots", "额外扫描根"), ("reference_roots", "引用检查目录"),
    ("protect", "永不触碰的路径"), ("deliverable_roots", "成品目录"), ("retention_days", "AI 对话保留天数"),
    ("projects", "项目台账"),
    # 0.6 初始设置里会改变 check 结果（降 keep、预填、进哪一档）的项；缺省与“等于没设”的取值都不进摘要，
    # 所以没有这些字段的 0.5 旧封存摘要不变。read_scope / delete_mode / governance_mode / confirm_mode / cloud_policy
    # 不影响放行，不在其内。允许值与 setup_q 一致（tests/test_profile_guard.py 有一致性测试）。
    ("tools", "常用工具的设置"), ("conversation_policy", "旧对话的处理方式"), ("rejects_policy", "没挑中图片的处理方式"),
    ("worktree_policy", "工作树的处理方式"), ("big_file_age_days", "录屏和大视频的天数"),
    ("done_project_policy", "做完项目的处理方式"), ("installer_policy", "安装包的处理方式"),
)
# 字段 → 会改变判断的取值（其余取值与没设等价）。tools 单独处理：只有“不用了”会改变判断。
GUARD_EFFECTIVE = {
    "conversation_policy": ("done", "library"),
    "rejects_policy": ("drop", "keep"),
    "worktree_policy": ("whole", "build_only", "ask"),
    "done_project_policy": ("archive", "keep"),
    "installer_policy": ("auto",),
}
GUARD_BIG_DAYS = (30, 90, 0)
GUARD_TOOL_KEYS = ("android", "xcode", "docker", "model-caches", "creative-media", "codex", "claude-code", "grok",
                   "gemini-antigravity", "cursor")


def guard_projects(v):
    """台账里影响判断的部分：{项目键: {status, deliverables}}（名字、时间、来源不影响判断，不在其内）。"""
    out = {}
    if isinstance(v, dict):
        for k, e in v.items():
            if isinstance(k, str) and isinstance(e, dict) and e.get("status") in PROJECT_STATUSES:
                out[k] = {"status": e["status"],
                          "deliverables": sorted({x for x in (e.get("deliverables") or []) if isinstance(x, str)})}
    return out


def profile_guard(profile):
    """返回 {"digest": 12 位, "fields": {...}}：影响判断的设置字段的规范化摘要。缺省值与显式写默认值等价。"""
    profile = profile if isinstance(profile, dict) else {}
    fields = {}
    for key, _ in PROFILE_GUARD_FIELDS:
        v = profile.get(key)
        if key == "retention_days":
            fields[key] = v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0
        elif key == "roots":
            fields[key] = sorted({x for x in (v if isinstance(v, list) else ["~"]) if isinstance(x, str)})
        elif key == "projects":
            gp = guard_projects(v)
            if gp:                      # 没有台账时不进摘要：与 0.5 之前的封存摘要保持一致
                fields[key] = gp
        elif key == "tools":
            unused = sorted(k for k, x in (v if isinstance(v, dict) else {}).items()
                            if x == "unused" and k in GUARD_TOOL_KEYS)
            if unused:                  # 只有“不用了”会预填删除；常用 / 偶尔用 / 没设等价
                fields[key] = unused
        elif key == "big_file_age_days":
            if isinstance(v, int) and not isinstance(v, bool) and v in GUARD_BIG_DAYS:
                fields[key] = v
        elif key in GUARD_EFFECTIVE:
            if v in GUARD_EFFECTIVE[key]:
                fields[key] = v
        else:
            fields[key] = sorted({x for x in (v if isinstance(v, list) else []) if isinstance(x, str)})
    blob = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {"digest": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12], "fields": fields}


def profile_guard_problem(seal, profile):
    """封存后设置有没有改过。没改返回 None；改过或旧封存没有摘要返回要告诉用户的说明。"""
    sealed = seal.get("profile_guard") if isinstance(seal, dict) else None
    if not (isinstance(sealed, dict) and isinstance(sealed.get("digest"), str)):
        return ("这份封存没有记录当时的设置（旧版本封存），无法确认设置在封存后没改过；"
                "请重新运行 plan.py check / seal 再执行")
    now = profile_guard(profile)
    if now["digest"] == sealed["digest"]:
        return None
    old = sealed.get("fields") if isinstance(sealed.get("fields"), dict) else {}
    changed = [name for key, name in PROFILE_GUARD_FIELDS if old.get(key) != now["fields"].get(key)]
    what = "、".join(changed) if changed else "影响判断的设置"
    return (f"设置在封存后改过（{what}），封存时的判断已经过期；请重新运行 plan.py check / seal"
            "（若封存 digest 没变，原来的选择清单仍然有效；变了就需要用户重新确认）")


def _within(path, base):
    path, base = path.casefold(), base.casefold()
    return path == base or path.startswith(base.rstrip("/") + "/")


def lexical(path):
    """不解析符号链接的规范化路径（展开 ~、去掉 .. 和重复斜杠）。"""
    return os.path.normpath(os.path.abspath(expand(path)))


def _candidates(path):
    return dict.fromkeys((lexical(path), real(path)))


def protected_reason(path, profile=None):
    """返回保护原因字符串；不受保护返回 None。

    符号链接穿透防护：real() 是"最近存在祖先做 realpath + 剩余部分拼接"的物理路径；
    原路径（只做词法规范化）与物理路径各查一次名单，任一命中即保护。
    只查物理路径会漏掉"受保护目录本身是指向别处的链接"，只查原路径会漏掉"指向受保护目录的链接"。"""
    for p in _candidates(path):
        reason = _reason(p, profile)
        if reason:
            return reason
    return None


def protection_kind(path, profile=None):
    """不受保护返回 None；路径本身在保护范围内（或就是保护路径）返回 "self"；
    仅因为它是某个受保护路径的祖先（整删会连带受保护内容）返回 "ancestor"。
    后者只意味着"不能整删这个目录本身"，其内部不受保护的条目不受影响。"""
    kinds = {k for p in _candidates(path) for k, _ in _reasons(p, profile)}
    return "self" if "self" in kinds else ("ancestor" if kinds else None)


def _reason(p, profile=None):
    return next((why for _, why in _reasons(p, profile)), None)


def _reasons(p, profile=None):
    """依次产出适用于 p 的 (kind, 原因)；第一条就是 protected_reason 返回的原因。"""
    cf = p.casefold()
    for base, _, why in FIXED_RULES:
        if _within(p, base):
            yield "self", why
        elif _within(base, p):  # 含其祖先（/private/var 删了也会带走 vm）
            yield "ancestor", why
    # /Users 本身与非当前用户的家目录（含 /Users/Shared）：别人的数据，不碰
    if _within(p, USERS_ROOT) and not _within(p, HOME):
        yield "self", "他人用户目录或 /Users 本身"
    # /Volumes 本身与卷根（直接子目录）：挂载点，删除等于动整块磁盘/网盘
    if cf == VOLUMES_ROOT.casefold() or (
            _within(p, VOLUMES_ROOT) and len(p[len(VOLUMES_ROOT):].strip("/").split("/")) == 1):
        yield "self", "卷根 /Volumes（挂载点）"
    # 任意卷、任意深度的 .Trashes 与系统/备份目录：目录本身及其内容统一保护
    for part in cf.split("/"):
        if part == ".trashes":
            yield "self", "卷废纸篓 .Trashes"
        elif part in VOLUME_META:
            yield "self", f"系统/备份目录 {part}"
    # 临时目录：只放开 claude-<uid> 下的会话子目录
    base = TMP_BASE.casefold()
    if cf == base:
        yield "self", "临时目录 %s 本身" % TMP_BASE
    elif _within(p, TMP_BASE):
        croot = claude_tmp_root().casefold()
        if cf == croot:
            yield "self", "Claude 会话临时目录本身"
        elif not _within(p, croot):
            yield "self", "临时目录里不属于 Claude 会话的内容"
    for sid in current_session_ids():
        if sid.casefold() in cf:
            yield "self", "当前会话正在用的目录（会话 id）"
            break
    if cf == HOME.casefold():
        yield "self", "家目录本身"
    if cf in {x.casefold() for x in SYSTEM}:
        yield "self", "系统目录"
    for base in SYSTEM[1:]:
        if _within(p, base) and not _within(p, HOME):
            yield "self", f"系统位置 {base}"
    parts = cf.split("/")
    if ".git" in parts:
        yield "self", "git 内部数据"
    if _within(p, HOME):
        rel = os.path.relpath(p, HOME).split("/")
        for pattern, _ in HOME_RULES:
            pat = pattern.split("/")
            if all(fnmatch.fnmatchcase(r.casefold(), q.casefold()) for r, q in zip(rel, pat)):
                if len(rel) >= len(pat):
                    yield "self", f"保护名单 ~/{pattern}"
                else:
                    yield "ancestor", f"包含保护路径 ~/{pattern}"
        for shallow in SHALLOW:
            if rel[0].casefold() == shallow.casefold() and len(rel) <= 2:
                yield "self", f"保护名单 ~/{shallow}（顶层及直接子项）"
    for extra in (profile or {}).get("protect", []):
        e = real(extra)
        if _within(p, e):
            yield "self", f"用户保护 {extra}"
        elif _within(e, p):
            yield "ancestor", f"用户保护 {extra}"


def chrome_clone_root():
    """只信 macOS 的当前用户目录；不读取 TMPDIR，也不开放整个 var/folders。"""
    import subprocess
    from pathlib import Path
    value = subprocess.check_output(['/usr/bin/getconf', 'DARWIN_USER_TEMP_DIR'], text=True).strip()
    temp = Path(value).resolve()
    if not re.fullmatch(r'/private/var/folders/[^/]{2}/[^/]+/T', str(temp)):
        raise ValueError('无法确认当前用户系统临时目录')
    if temp.stat().st_uid != os.getuid():
        raise ValueError('系统临时目录不属于当前用户')
    return temp.parent / 'X' / 'com.google.Chrome.code_sign_clone'


def chrome_clone_path(path, profile):
    """专用原生清理的窄入口；通用 under_roots 与保护名单不变。"""
    from pathlib import Path
    p = Path(path)
    root = chrome_clone_root()
    if p.parent != root or not re.fullmatch(r'code_sign_clone\.[A-Za-z0-9]{6}', p.name):
        raise ValueError('只接受系统发现位置中的单个 Chrome 启动克隆')
    if p.resolve() != p or root.resolve() != root or not p.is_dir():
        raise ValueError('路径不存在或经过符号链接')
    if p.stat().st_uid != os.getuid() or protected_reason(str(p), profile):
        raise ValueError('归属或保护检查拒绝')
    return p


def under_roots(path, profile):
    p = real(path)
    roots = [real(r) for r in profile.get("roots", ["~"]) + profile.get("extra_roots", [])]
    return any(_within(p, r) for r in roots)


# ----------------------------------------------------------------------------
# 原生命令形状表（契约第 5 节）：method=native 的 argv 必须逐词落在下表某一条形状里。
# 只比程序名是不够的（docker system prune -a --volumes、brew uninstall x 程序名都"合法"），
# 所以 plan.py check 与 act.py 执行前复核共用 native_check()：程序名、子命令词序列、选项、
# 至多一个对象槽位都要精确匹配，槽位还要与 item.path 对应。新增形状必须在此登记并附反例测试
# （tests/test_plan_act.py），只登记"卡里推荐过、且只删除该工具自身数据"的命令。
# 不登记（刻意）：docker system/volume/image prune、brew uninstall/autoremove、npm exec/npx、
# pip/npm/brew install、yarn cache clean（取决于 cwd，Berry 会删项目内缓存）、claude purge
# （连 memory 与配置条目一起删）、simctl runtime/设备 delete <UDID>、任何带 shell 或任意代码的命令。
# ----------------------------------------------------------------------------

NATIVE_REJECT_HINT = "该命令形状未登记；如需新增请在 protect.py 形状表登记并附反例测试"

_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
UUID_PAT = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class Slot:
    """对象槽位：argv 里恰好一个词，必须整串匹配 pattern（开头必为字母数字，因此不可能是选项）。"""

    def __init__(self, name, pattern, ignore_case=False):
        self.name = name
        self.pattern = re.compile(pattern, re.I if ignore_case else 0)


class Opt:
    """可选/必选选项：names 里的精确词，或 pattern 整串匹配的词（如 --prune=30）。每组最多出现一次。"""

    def __init__(self, names=(), pattern=None, required=False):
        self.names = tuple(names)
        self.pattern = re.compile(pattern) if pattern else None
        self.required = required

    def accepts(self, tok):
        return tok in self.names or bool(self.pattern and self.pattern.fullmatch(tok))


class Shape:
    """一条形状：argv = [prog] + fixed（字面词与至多一个 Slot，按序）+ opts（任意顺序，每组至多一次）。
    path 是 (槽位值, item 真实路径, 家目录) -> 错误字符串或 None 的对应规则。"""

    def __init__(self, sid, prog, fixed, purpose, path, opts=(), env=None, guard=None):
        self.sid, self.prog, self.fixed, self.purpose, self.path, self.opts = sid, prog, tuple(fixed), purpose, path, tuple(opts)
        # env：执行时强制设置的环境变量（dict，或 家目录 -> dict 的函数，用于由 path 规则推导出的位置）。
        # guard：家目录 -> 错误说明或 None，执行前的额外现场检查（如会覆盖强制变量的配置文件）。
        self.env, self.guard = env, guard
        assert sum(isinstance(f, Slot) for f in self.fixed) <= 1

    def forced_env(self, home):
        return dict(self.env(home) if callable(self.env) else (self.env or {}))

    def match(self, argv):
        """匹配返回 (槽位值或 None,)；不匹配返回 None。"""
        if argv[0] != self.prog or len(argv) < 1 + len(self.fixed):
            return None
        value = None
        for want, tok in zip(self.fixed, argv[1:]):
            if isinstance(want, Slot):
                if not want.pattern.fullmatch(tok):
                    return None
                value = tok
            elif want != tok:
                return None
        used = set()
        for tok in argv[1 + len(self.fixed):]:
            hit = next((k for k, o in enumerate(self.opts) if o.accepts(tok)), None)
            if hit is None or hit in used:
                return None
            used.add(hit)
        if any(o.required and k not in used for k, o in enumerate(self.opts)):
            return None
        return (value,)


def _dir(home, rel):
    return os.path.realpath(os.path.join(home, rel))


def _root(*rels):
    """缓存类命令清的是该工具的整个缓存：path 必须恰好是该工具缓存根之一（规范化后相等），不接受其子目录——
    否则最近修改、占用、引用核对只覆盖子目录，而命令实际清掉的是整个缓存。"""
    def rule(value, rp, home):
        if any(rp.casefold() == _dir(home, r).casefold() for r in rels):
            return None
        return "path 必须恰好是该工具的缓存根（不接受其子目录）：" + "、".join("~/" + r for r in rels)
    return rule


def _exact(fn, what):
    """能对应具体对象的命令：path 必须恰好是槽位值推出的那个路径，防止核对 A、删掉 B。"""
    def rule(value, rp, home):
        want = fn(value, home)
        if want is None:
            return f"无法由 {value} 推出对应路径"
        if rp.casefold() != os.path.realpath(want).casefold():
            return f"{value} 与 path 不对应（{what}应为 {want}）"
        return None
    return rule


def _codex_rule(value, rp, home):
    base = os.path.join(home, ".codex")
    if not any(_within(rp, os.path.join(base, d)) for d in ("sessions", "archived_sessions")):
        return "codex delete 的 path 必须是 ~/.codex/sessions 或 archived_sessions 里的会话文件"
    if not re.fullmatch(r"rollout-.+-" + re.escape(value) + r"\.jsonl", os.path.basename(rp), re.I):
        return f"codex delete 的线程 {value.lower()} 与 path 的文件名不对应"
    return None


def _grok_rule(value, rp, home):
    base = os.path.realpath(os.path.join(home, ".grok", "sessions"))
    if not _within(rp, base):
        return "grok sessions delete 的 path 必须在 ~/.grok/sessions 内"
    rel = os.path.relpath(rp, base).split("/")
    if len(rel) != 2 or rel[1].casefold() != value.casefold():
        return f"会话 {value} 与 path 不对应（path 应为 ~/.grok/sessions/<工作目录编码>/{value}）"
    return None


def _sdk_path(pkg, home):
    return os.path.join(home, "Library", "Android", "sdk", *pkg.split(";"))


def _avd_path(name, home):
    return os.path.join(home, ".android", "avd", name + ".avd")


def _ollama_path(model, home):
    name, _, tag = model.partition(":")
    parts = name.split("/")
    host, ns, leaf = ("registry.ollama.ai", "library", parts[0]) if len(parts) == 1 else \
        ("registry.ollama.ai", parts[0], parts[1]) if len(parts) == 2 else tuple(parts)
    return os.path.join(home, ".ollama", "models", "manifests", host, ns, leaf, tag or "latest")


def _hf_path(repo, home):
    kind, _, rest = repo.partition("/")
    return os.path.join(home, ".cache", "huggingface", "hub", f"{kind}s--" + rest.replace("/", "--"))


# SDK 包 ID：; 分层。第一层不得是 cmdline-tools / platform-tools（卸载会删掉 sdkmanager 自身或 adb，之后再也无法用它管理 SDK）。
_PKG = Slot("sdk-package", r"(?!(?:cmdline-tools|platform-tools)(?:;|$))[A-Za-z0-9_][A-Za-z0-9_.-]*(?:;[A-Za-z0-9_][A-Za-z0-9_.-]*)*",
            ignore_case=True)

BREW_ENV = {"HOMEBREW_NO_AUTOREMOVE": "1", "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_CLEANUP": "1"}


def _java_home():
    """Android 命令行工具需要 JDK：只从固定的几处安装位置找，不继承调用者的 JAVA_HOME。"""
    import glob
    pats = ("/opt/homebrew/opt/openjdk*/libexec/openjdk.jdk/Contents/Home",
            "/usr/local/opt/openjdk*/libexec/openjdk.jdk/Contents/Home",
            "/Library/Java/JavaVirtualMachines/*/Contents/Home")
    for pat in pats:
        for cand in sorted(glob.glob(pat), reverse=True):
            if os.access(os.path.join(cand, "bin", "java"), os.X_OK):
                return cand
    return None


def _android_env(home):
    """位置全部由 path 规则推导（SDK = ~/Library/Android/sdk，AVD = ~/.android/avd），显式设置，不继承调用者的 ANDROID_*。"""
    sdk = os.path.join(home, "Library", "Android", "sdk")
    env = {"ANDROID_HOME": sdk, "ANDROID_SDK_ROOT": sdk, "ANDROID_USER_HOME": os.path.join(home, ".android"),
           "ANDROID_AVD_HOME": os.path.join(home, ".android", "avd")}
    jh = _java_home()
    if jh:
        env["JAVA_HOME"] = jh
    return env


def _brew_env_guard(home):
    """brew 启动时会读 brew.env 文件并覆盖进程环境（含上面强制的变量与缓存位置），文件里出现这些键就不执行。"""
    keys = ("HOMEBREW_NO_AUTOREMOVE", "HOMEBREW_AUTOREMOVE", "HOMEBREW_NO_AUTO_UPDATE", "HOMEBREW_NO_INSTALL_CLEANUP",
            "HOMEBREW_CACHE", "HOMEBREW_LOGS", "HOMEBREW_TEMP", "HOMEBREW_CLEANUP_PERIODIC_FULL_DAYS")
    files = ["/etc/homebrew/brew.env", "/opt/homebrew/etc/homebrew/brew.env", "/usr/local/etc/homebrew/brew.env",
             os.path.join(home, ".homebrew", "brew.env")]
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    k = line.strip().split("=", 1)[0].strip()
                    if k in keys:
                        return f"{f} 里设置了 {k}，会覆盖强制环境变量或缓存位置，请先人工确认该文件"
        except OSError:
            continue
    return None


# 每条：id、程序名、固定词序、用途、path 对应规则、选项。来源卡与本机核对见 notes/P1.1.md。
SHAPES = [
    # codex 单会话删除（会连带删后代，plan.py/act.py 另有"子在前父在后"检查）。来源：cards/codex.md；本机 0.162.1 核对。
    Shape("codex-delete", "codex", ("delete", "--force", Slot("thread-uuid", UUID_PAT, True)),
          "按线程 UUID 删除一个 Codex 会话", _codex_rule),
    # 来源：cards/android.md；本机 sdkmanager --help 核对（--uninstall <package>）。包 ID 的 ; 对应 SDK 目录层级。
    Shape("sdkmanager-uninstall", "sdkmanager", ("--uninstall", _PKG),
          "卸载一个 SDK 包（如系统镜像）；不允许卸载 cmdline-tools 与 platform-tools",
          _exact(_sdk_path, "SDK 包目录"), env=_android_env),
    # 来源：cards/android.md；本机 avdmanager delete avd 用法核对（-n/--name 必填）。仅默认 ~/.android/avd。
    Shape("avdmanager-delete-avd", "avdmanager", ("delete", "avd", "-n", Slot("avd-name", r"[A-Za-z0-9_][A-Za-z0-9_.-]*")),
          "删除一个 Android 虚拟设备（同时会删除 ~/.android/avd/<名>.ini，它在 path 之外）",
          _exact(_avd_path, "AVD 目录"), env=_android_env),
    # 来源：cards/package-managers.md、git-worktree-build.md；本机 brew cleanup --help 核对（--prune 值为天数或 all，-s/--scrub）。
    # 本机 Homebrew 7.0.9 核对：cleanup.rb 默认会 autoremove（卸载无人依赖的 formula），HOMEBREW_NO_AUTOREMOVE 才能关；
    # 同时会删除已安装软件的旧版本（Cellar/Caskroom，在 path 之外）。执行时强制 BREW_ENV，见 notes/M1-fix-act.md。
    Shape("brew-cleanup", "brew", ("cleanup",),
          "清理 Homebrew 的下载缓存、日志，并删除已安装软件的旧版本（旧版本在 Cellar/Caskroom，path 之外；不卸载任何软件，已强制关闭 autoremove）",
          _root("Library/Caches/Homebrew"),
          (Opt(("-s", "--scrub")), Opt(pattern=r"--prune=(?:[0-9]{1,4}|all)")), env=BREW_ENV, guard=_brew_env_guard),
    # 来源：cards/xcode.md；本机未装 Xcode，simctl 未本机核对（按卡与官方说明）。
    Shape("simctl-delete-unavailable", "xcrun", ("simctl", "delete", "unavailable"),
          "删除运行时已不存在的模拟器设备", _root("Library/Developer/CoreSimulator/Devices")),
    # 来源：cards/model-caches.md；huggingface_hub 官方 manage-cache 文档核对（本机未装 hf，未本机核对）。
    Shape("hf-cache-prune", "hf", ("cache", "prune"), "删无引用的 revision、.incomplete 与无人使用的共享块（必须 -y，否则交互确认）",
          _root(".cache/huggingface"), (Opt(("-y", "--yes"), required=True),)),
    Shape("hf-cache-rm", "hf", ("cache", "rm", Slot("hf-repo", rf"(?:model|dataset|space)/{_NAME}(?:/{_NAME})?")),
          "删除一个缓存的 HF 仓库（只接受 类型/组织/名 形式，不接受 revision 哈希与 hf:// 文件）",
          _exact(_hf_path, "HF 仓库缓存目录"), (Opt(("-y", "--yes"), required=True),)),
    # 来源：cards/model-caches.md；本机未装 ollama（未本机核对）。manifest 路径 = manifests/<host>/<namespace>/<name>/<tag>。
    Shape("ollama-rm", "ollama", ("rm", Slot("ollama-model", rf"{_NAME}(?:/{_NAME}){{0,2}}(?::{_NAME})?")),
          "删除一个 ollama 模型 tag", _exact(_ollama_path, "manifest 文件")),
    # 来源：cards/package-managers.md；本机 npm 11 核对（clean 不带 --force 会被 npm 拒绝，无副作用；不带 <key>）。
    Shape("npm-cache-clean", "npm", ("cache", "clean"), "清空 npm 下载缓存", _root(".npm"), (Opt(("--force",)),)),
    Shape("npm-cache-verify", "npm", ("cache", "verify"), "校验并回收 npm 缓存", _root(".npm")),
    # 来源：cards/package-managers.md；pnpm 本机未装（未本机核对）。
    Shape("pnpm-store-prune", "pnpm", ("store", "prune"), "清 pnpm 全局存储里无项目引用的包",
          _root("Library/pnpm/store", ".local/share/pnpm/store", ".pnpm-store")),
    # 来源：cards/package-managers.md；本机 bun pm 帮助核对（bun pm cache rm = clear the cache）。
    Shape("bun-pm-cache-rm", "bun", ("pm", "cache", "rm"), "清空 bun 全局缓存", _root(".bun/install/cache")),
    # 来源：cards/package-managers.md；本机 uv cache --help 核对（prune --ci；clean 不带包名）。不登记 --force/--cache-dir。
    Shape("uv-cache-prune", "uv", ("cache", "prune"), "清 uv 缓存里的悬空条目与集中式环境",
          _root(".cache/uv", "Library/Caches/uv"), (Opt(("--ci",)),)),
    Shape("uv-cache-clean", "uv", ("cache", "clean"), "清空 uv 缓存（不带包名）", _root(".cache/uv", "Library/Caches/uv")),
    # 来源：cards/package-managers.md；本机 pip3 cache --help 核对（purge）。
    Shape("pip-cache-purge", "pip", ("cache", "purge"), "清空 pip 缓存", _root("Library/Caches/pip", ".cache/pip")),
    Shape("pip3-cache-purge", "pip3", ("cache", "purge"), "清空 pip 缓存", _root("Library/Caches/pip", ".cache/pip")),
    # 来源：cards/docker.md；本机未装 docker（未本机核对）。只清构建缓存；非交互必须 -f/--force；不带 -a/--all。
    Shape("docker-builder-prune", "docker", ("builder", "prune"), "清 Docker 构建缓存（不含镜像与卷）；构建缓存在 Docker.raw 虚拟磁盘内，清理后 path 的占用可能不立即变小，回执会显示“占用没有变化”",
          _root("Library/Containers/com.docker.docker"),
          (Opt(("-f", "--force"), required=True), Opt(pattern=r"--filter=until=[0-9]{1,5}h"))),
    # 来源：cards/grok.md；本机 grok 1.0.50 sessions delete --help 核对（<ID>）。会话目录名 = 会话 ID。
    Shape("grok-sessions-delete", "grok", ("sessions", "delete", Slot("session-id", UUID_PAT, True)),
          "永久删除一个 Grok 会话", _grok_rule),
]


def _bad_argv(argv):
    return not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv)


def native_match(argv):
    """返回 (Shape, 槽位值) 或 None。程序名必须是不含路径的裸名，且整串相等。"""
    if _bad_argv(argv):
        return None
    for shape in SHAPES:
        hit = shape.match(argv)
        if hit is not None:
            return shape, hit[0]
    return None


def native_allowed(argv):
    return native_match(argv) is not None


def native_check(argv, path):
    """native 条目的唯一校验入口（plan.py check 与 act.py 执行前复核共用）：通过返回 None，否则返回错误说明。"""
    if _bad_argv(argv):
        return "native 的 argv 必须是非空字符串数组"
    hit = native_match(argv)
    if hit is None:
        return f"native 命令 {' '.join(argv)[:120]!r} {NATIVE_REJECT_HINT}"
    shape, value = hit
    err = shape.path(value, real(path), HOME)
    return f"native 命令 {shape.sid}：{err}" if err else None


# ----------------------------------------------------------------------------
# 原生命令的执行环境（M1）：形状表只管"命令长什么样、path 对不对"，命令真正清哪里还取决于
# 工作目录里的配置文件（.npmrc、uv.toml、bunfig.toml）、环境变量（PIP_CACHE_DIR、HOMEBREW_CACHE……）和 PATH
# 里的同名程序。所以执行时一律用固定的空工作目录 + 白名单重建的环境 + 解析并核对过的程序路径。
# plan.py check（记入封存）与 act.py（执行前复核、写进回执）共用下面三个函数。
# ----------------------------------------------------------------------------

NATIVE_FIXED_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _untrusted_path_dir(d):
    """项目内的 node_modules/.bin、已激活虚拟环境的 bin 里的同名 shim 不参与解析。"""
    if "node_modules" in d.split("/"):
        return True
    return os.path.exists(os.path.join(os.path.dirname(d.rstrip("/")), "pyvenv.cfg"))


def resolve_prog(name, environ=None):
    """按固定顺序解析程序的绝对路径：先是 NATIVE_FIXED_PATH，再是调用者 PATH 里的绝对、可信目录（keg-only 的
    node@24、~/.local/bin 之类）。找不到返回 None。check 时记入封存，执行时再解析一次，必须一致。"""
    environ = os.environ if environ is None else environ
    if not name or "/" in name:
        return None
    dirs = list(NATIVE_FIXED_PATH)
    for d in environ.get("PATH", "").split(":"):
        if d and os.path.isabs(d) and d not in dirs and not _untrusted_path_dir(d):
            dirs.append(d)
    for d in dirs:
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def native_env(shape, prog, home=None, environ=None):
    """白名单重建的子进程环境：只带 HOME/USER/LOGNAME/LANG/LC_*/TMPDIR、固定 PATH，加上形状强制的变量。
    调用者环境里的一切缓存位置类变量（PIP_*、npm_config_*、UV_*、BUN_*、HOMEBREW_*、XDG_*、HF_*、OLLAMA_*、
    DOCKER_*、ANDROID_*、CODEX_HOME……）都不会传进去。"""
    environ = os.environ if environ is None else environ
    home = HOME if home is None else home
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = ""
    env = {"HOME": home}
    for k in ("USER", "LOGNAME"):
        if environ.get(k) or user:
            env[k] = environ.get(k) or user
    for k, v in environ.items():
        if k in ("LANG", "TMPDIR") or k.startswith("LC_"):
            env[k] = v
    path = list(NATIVE_FIXED_PATH)
    if prog and os.path.dirname(prog) not in path:
        path.append(os.path.dirname(prog))
    env["PATH"] = ":".join(path)
    env.update(shape.forced_env(home))
    return env
