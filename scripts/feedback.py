#!/usr/bin/env python3
"""叁笙清理：知识缺口反馈（只生成文字，绝不自动发送任何网络请求）。

    python3 scripts/feedback.py gaps --run <run目录> [--min-gb 1]   # 找出认不出来源的大目录，输出 JSON（含给用户看的预览全文）
    python3 scripts/feedback.py issue-url --run <run目录>          # 生成预填好的 GitHub Issue 链接（用户自己点开、自己提交）
    python3 scripts/feedback.py decline                            # 用户说“不用”：90 天内不再问

“知识缺口”= 本次扫描里体积不小、却没有任何知识卡、也没被生产者规则认出来源的前沿目录。
只回传：路径模式、体积、能从路径推出的软件名、Skill 版本、macOS 版本。
路径模式里的用户名一律换成 ~；家目录下凡不是众所周知的目录名，一律换成 <目录>（项目名、人名、文件名都不会出现）；
只有 Application Support / Containers / Group Containers 的直接子目录里形如反向域名（com.foo.bar）或已知软件名的、
以及 /Applications、~/Applications 直接下级的 .app 原样保留；家目录一级的点目录只保留常见工具目录白名单。
发送由用户自己完成最后一步：先把全文给用户看，用户点开链接自己按“提交”，或复制文字发到官网反馈页 / 公众号。
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import prefs  # noqa: E402
import protect  # noqa: E402
import units  # noqa: E402

SKILL_ROOT = os.path.dirname(HERE)
REPO = "https://github.com/sanshengai/sansheng-disk-cleanup"
ISSUE_BASE = REPO + "/issues/new"
FALLBACK_PAGE = "https://sanshengai.top/tools/disk-cleanup/feedback/"
DEFAULT_MIN_GB = 1.0
COOLDOWN_DAYS = 90                       # 用户说“不用”之后多久不再问
URL_LIMIT = 7000                         # 预填链接的总长度上限（GitHub 对过长的链接会拒绝）
PREVIEW_MAX = 30                         # 预览里最多列多少条
DIR = "<目录>"

# 众所周知的目录名（不分大小写整段相等才保留）：系统与常见工具的固定目录，本身不含任何个人信息。
WELL_KNOWN = {n.casefold() for n in (
    "Library", "Application Support", "Caches", "Cache", "Containers", "Group Containers", "Logs", "Preferences",
    "Application Scripts", "Saved Application State", "HTTPStorages", "WebKit", "Cookies", "Mobile Documents",
    "CloudStorage", "Developer", "CoreSimulator", "Devices", "Xcode", "DerivedData", "Archives", "iOS DeviceSupport",
    "Documents", "Downloads", "Desktop", "Movies", "Pictures", "Music", "Public", "Applications", "Volumes",
    ".cache", ".local", ".config", ".npm", ".cargo", ".rustup", ".gradle", ".m2", ".pnpm-store", ".yarn", ".docker",
    ".nvm", ".pyenv", ".conda", ".virtualenvs", ".bun", ".deno", ".ollama", ".android", ".gem", ".bundle",
    "share", "state", "bin", "lib", "var", "tmp", "private", "opt", "usr", "local", "System", "Users",
    "node_modules", "site-packages", "target", "build", "dist", "Pods", "Models", "models", "venv", ".venv",
    "Homebrew", "Cellar", "Caskroom", "Frameworks", "Resources", "Contents", "MacOS",
)}
# 家目录一级的点目录：只认这份常见工具目录白名单（其余的点目录可能是项目名、人名，一律换成 <目录>）
HOME_DOT_DIRS = {n.casefold() for n in (
    ".cache", ".npm", ".cargo", ".rustup", ".gradle", ".m2", ".docker", ".ollama", ".local", ".config", ".vscode",
    ".cursor", ".codex", ".claude", ".gemini", ".android", ".pyenv", ".nvm", ".bun", ".deno", ".conda",
    ".pnpm-store", ".yarn", ".virtualenvs", ".gem", ".bundle", ".cocoapods", ".swiftpm", ".Trash", ".ssh", ".gnupg",
    ".aws", ".kube", ".pip", ".pipx", ".poetry", ".uv", ".rbenv", ".rvm", ".sdkman", ".asdf", ".volta", ".fnm",
    ".pub-cache", ".flutter", ".dart", ".expo", ".huggingface", ".keras", ".torch", ".matplotlib", ".jupyter",
    ".ipython", ".lmstudio", ".grok", ".antigravity", ".windsurf", ".zed", ".anaconda", ".nuget", ".dotnet", ".lima",
    ".colima", ".minikube", ".orbstack", ".sansheng", ".agents", ".opencode", ".continue", ".aider", ".nix-profile",
    ".bash_sessions", ".zsh_sessions", ".oh-my-zsh", ".tldrc", ".node-gyp", ".electron", ".cache-loader",
)}
# 路径里这几个目录的直接子目录是软件名 / bundle id，满足“反向域名形态”或在已知软件名单里才原样保留并作为“软件名”
SOFTWARE_PARENTS = ("application support", "containers", "group containers")
KNOWN_SOFTWARE = {n.casefold() for n in (
    "Google", "Code", "Cursor", "Claude", "Slack", "Discord", "Firefox", "Mozilla", "Arc", "Zed", "Notion", "Obsidian",
    "Figma", "Spotify", "Steam", "Telegram", "WhatsApp", "Dropbox", "Docker", "Docker Desktop", "JetBrains",
    "Microsoft", "Microsoft Edge", "Adobe", "BraveSoftware", "Postman", "Sublime Text", "iTerm2", "Warp", "Raycast",
    "1Password", "MobileSync", "AddressBook", "CrashReporter", "Antigravity", "Codex", "Ollama", "LM Studio",
    "Anthropic", "OpenAI", "Windsurf", "Typora", "Zoom", "zoom.us", "Skype", "Teams", "Chromium", "Vivaldi", "Opera",
    "Android Studio", "AndroidStudio", "Xcode", "Simulator", "CloudDocs", "Knowledge", "com.apple.sharedfilelist",
    "Final Cut Pro", "DaVinci Resolve", "Blackmagic Design", "Logic", "GarageBand", "Unity", "Unreal Engine", "Blender",
    "Homebrew", "pip", "Yarn", "npm", "pnpm", "Cypress", "Electron", "Kindle", "Mail", "Safari", "Photos",
)}
# 反向域名形态：com.foo.bar、org.mozilla.firefox、ABCDE12345.com.foo（Team ID 前缀）、group.com.foo
_TLDS = {"com", "org", "net", "io", "app", "dev", "co", "cn", "me", "ai", "edu", "gov", "top", "xyz", "tv", "us", "uk",
         "de", "fr", "jp", "ru", "group", "mac", "ios", "info", "biz", "cc", "so", "sh", "to"}
_REVERSE_DOMAIN = re.compile(r"^(?:[A-Z0-9]{10}\.)?([A-Za-z0-9]+)(?:\.[A-Za-z0-9_-]+)+$")


def _keep_segment(seg, user, level1_in_home):
    """这一段是不是众所周知、可以原样保留。用户名相关的段一律不保留。"""
    c = seg.casefold()
    if user:
        u = user.casefold()
        if c == u or (len(u) >= 3 and u in c):
            return False
    if c in WELL_KNOWN:
        return True
    if level1_in_home and c in HOME_DOT_DIRS:                         # 一级家目录里的点目录：只认常见工具目录白名单
        return True
    return False


def _software_ok(seg, user):
    """软件名段能保留的前提：不含用户名。"""
    if not seg:
        return False
    if user:
        c, u = seg.casefold(), user.casefold()
        if c == u or (len(u) >= 3 and u in c):
            return False
    return True


def _looks_like_software(seg):
    """Application Support / Containers / Group Containers 的直接子目录：反向域名形态（含点号，首段是常见域名后缀，
    可带 10 位 Team ID 前缀）或在已知软件名单里才算软件名；其余（人名、项目名、中文目录名）换成 <目录>。"""
    if seg.casefold() in KNOWN_SOFTWARE:
        return True
    m = _REVERSE_DOMAIN.match(seg)
    if not m:
        return False
    if m.group(1).casefold() in _TLDS:
        return True
    rest = seg.split(".")                                            # Team ID 前缀后面还得跟域名后缀
    return len(rest) >= 3 and re.fullmatch(r"[A-Z0-9]{10}", rest[0]) is not None and rest[1].casefold() in _TLDS


def mask_path(path, home=None):
    """路径 → (路径模式, 软件名或 None)。home 默认取 protect.HOME。"""
    home = os.path.normpath(home or protect.HOME)
    user = os.path.basename(home)
    p = os.path.normpath(str(path))
    parts = [x for x in p.split("/") if x]
    software = None
    out = []
    in_home = False
    start = 0
    if p == home or p.startswith(home + "/"):
        in_home = True
        prefix = "~"
        start = len([x for x in home.split("/") if x])
    elif len(parts) >= 2 and parts[0] == "Users" and user and parts[1].casefold() == user.casefold():
        in_home = True
        prefix = "~"
        start = 2
    elif len(parts) >= 2 and parts[0] == "Users":                    # 同机其他用户的家目录
        in_home = True
        prefix = "/Users/<用户>"
        start = 2
    elif parts and parts[0] == "Volumes":                            # 外置盘 / 卷名属于个人信息
        prefix = "/Volumes/<卷>" if len(parts) > 1 else "/Volumes"
        start = 2
    else:
        prefix = ""
    rel = parts[start:]
    prev = None
    for i, seg in enumerate(rel):
        level1 = in_home and i == 0
        if (i == 2 and rel[0].casefold() == "library" and prev in SOFTWARE_PARENTS    # 只认 Library/<这三个>/<软件名> 这一个位置
                and _software_ok(seg, user) and _looks_like_software(seg)):
            out.append(seg)
            software = software or seg
        elif (seg.endswith(".app") and i == 1 and rel[0].casefold() == "applications"      # .app 只在 /Applications、~/Applications 的直接下级保留
              and prefix in ("~", "") and _software_ok(seg, user)):
            out.append(seg)
            software = software or seg[:-4]
        elif _keep_segment(seg, user, level1):
            out.append(seg)
        else:
            out.append(DIR)
        prev = seg.casefold()
    pattern = prefix + ("/" + "/".join(out) if out else "")
    return pattern or "/", software


# ------------------------------------------------------------ 版本

def skill_version():
    """Skill 版本：以 .claude-plugin/plugin.json 为真源（发布时才改），读不到再看 CHANGELOG 里第一个版本号。"""
    try:
        with open(os.path.join(SKILL_ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            v = json.load(f).get("version")
        if isinstance(v, str) and v:
            return v
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(SKILL_ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^##\s+(\d+\.\d+\.\d+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return "未知"


def macos_version():
    try:
        r = subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True, timeout=5)
        v = r.stdout.strip()
        return v if r.returncode == 0 and v else "未知"
    except (OSError, subprocess.SubprocessError):
        return "未知"


# ------------------------------------------------------------ 冷却

def _parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def cooling_down(profile, now=None):
    """90 天内用户说过“不用”则 True。"""
    at = _parse_iso(profile.get("feedback_declined_at"))
    if not at:
        return False
    return (now or datetime.now(timezone.utc)) < at + timedelta(days=COOLDOWN_DAYS)


# ------------------------------------------------------------ 检测与文字

def find_gaps(facts, min_bytes, home=None):
    """facts → 合并后的条目列表（同一路径模式合并：体积相加、记个数），按体积从大到小。"""
    merged = {}
    for n in facts.get("nodes") or []:
        if not isinstance(n, dict) or not n.get("frontier") or n.get("protected"):
            continue
        b = n.get("bytes")
        if not isinstance(b, (int, float)) or isinstance(b, bool) or b < min_bytes:
            continue
        pr = n.get("producer") if isinstance(n.get("producer"), dict) else {}
        if pr.get("card") or pr.get("tool"):                  # 有知识卡、或被生产者规则认出来源 → 不是缺口
            continue
        path = n.get("path")
        if not isinstance(path, str) or not path:
            continue
        pattern, software = mask_path(path, home)
        e = merged.setdefault(pattern, {"pattern": pattern, "bytes": 0, "count": 0, "software": software})
        e["bytes"] += int(b)
        e["count"] += 1
        e["software"] = e["software"] or software
    out = sorted(merged.values(), key=lambda e: (-e["bytes"], e["pattern"]))
    for e in out:
        e["size"] = units.fmt_bytes(e["bytes"])
    return out


def _entry_line(i, e):
    s = f"{i}. {e['pattern']}　{e['size']}"
    if e["count"] > 1:
        s += f"（共 {e['count']} 处）"
    if e.get("software"):
        s += f"　软件：{e['software']}"
    return s


def build_text(gaps, versions, min_gb, shown=None):
    """给用户看的预览全文（也是 Issue 正文）。shown：只列前几条，其余写“另有 N 条省略”。"""
    total_dirs = sum(e["count"] for e in gaps)
    k = len(gaps) if shown is None else max(0, min(shown, len(gaps)))
    lines = [
        "【叁笙清理 · 知识缺口反馈】",
        f"Skill 版本：{versions['skill']}　macOS：{versions['macos']}",
        f"这次扫描里有 {total_dirs} 个大目录（每个 ≥ {min_gb:g} GB）认不出来源，没有对应的知识卡。以下是将要反馈的全部内容：",
        "",
    ]
    lines += [_entry_line(i, e) for i, e in enumerate(gaps[:k], 1)]
    if k < len(gaps):
        lines.append(f"（另有 {len(gaps) - k} 条省略）")
    lines += ["", "不含：文件名、文件内容、用户名、项目名。路径里的用户名已换成 ~，不是众所周知的目录名已换成 <目录>。"]
    return "\n".join(lines)


def detect(run, min_gb=DEFAULT_MIN_GB, now=None, home=None, profile=None, with_versions=True):
    """返回完整结果字典。run 里没有 facts.json 抛 OSError / ValueError，由调用方处理。
    with_versions=False（汇报末尾只想知道有没有、有几个）时不查 macOS 版本，不起任何子进程。"""
    with open(os.path.join(run, "facts.json"), encoding="utf-8") as f:
        facts = json.load(f)
    profile = prefs.load() if profile is None else profile
    gaps = find_gaps(facts, int(min_gb * 1e9), home)
    cooling = cooling_down(profile, now)
    versions = {"skill": skill_version(), "macos": macos_version() if with_versions and gaps else "未知"}
    shown = min(len(gaps), PREVIEW_MAX)
    if not gaps:
        reason, asked = "none", False
    elif cooling:
        reason, asked = "declined", False
    else:
        reason, asked = "ok", True
    res = {
        "schema": "feedback-gaps/1", "asked": asked, "reason": reason,
        "min_gb": min_gb, "skill_version": versions["skill"], "macos_version": versions["macos"],
        "total_dirs": sum(e["count"] for e in gaps), "gaps": gaps,
        "preview": build_text(gaps, versions, min_gb, shown) if gaps else "",
    }
    if reason == "declined":
        res["hint"] = f"用户在 {profile.get('feedback_declined_at')} 说过不用，{COOLDOWN_DAYS} 天内不要再问"
    elif reason == "none":
        res["hint"] = "没有认不出来源的大目录，不需要问"
    else:
        res["hint"] = "可以问用户一句；同意后把 preview 全文展示给用户，再运行 issue-url。用户说不用就运行 decline"
    return res


def build_issue(res):
    """预填链接：整条链接不超过 URL_LIMIT，超出就少列几条并写明“另有 N 条省略”。返回 (url, 正文, 省略条数)。"""
    gaps = res["gaps"]
    versions = {"skill": res["skill_version"], "macos": res["macos_version"]}
    title = f"知识缺口反馈：{res['total_dirs']} 个大目录认不出来源（叁笙清理 {res['skill_version']}）"
    k = len(gaps)
    while True:
        body = build_text(gaps, versions, res["min_gb"], k)
        url = f"{ISSUE_BASE}?title={quote(title, safe='')}&body={quote(body, safe='')}"
        if len(url) <= URL_LIMIT or k <= 1:
            return url, body, len(gaps) - k
        k -= 1


def fallback_line():
    return f"上不了 GitHub 时：复制上面的文字，粘贴到 {FALLBACK_PAGE} 或发到公众号后台"


# ------------------------------------------------------------ 提示（act.py 汇报末尾用）

def hint_line(run):
    """有认不出来源的大目录、且不在冷却期时返回一行提示；否则返回 None。出任何错都返回 None，绝不影响主流程。"""
    try:
        res = detect(run, with_versions=False)
    except Exception:  # noqa: BLE001 - 提示是锦上添花
        return None
    if not res["asked"]:
        return None
    return (f"这次有 {res['total_dirs']} 个大目录认不出来源，可运行 "
            f"python3 scripts/feedback.py issue-url --run {run} 生成反馈（发送前给用户看全文）")


# ------------------------------------------------------------ 命令行

def cmd_gaps(args):
    run = os.path.realpath(args.run)
    try:
        res = detect(run, args.min_gb)
    except (OSError, ValueError) as exc:
        print(f"错误：读不到 {run}/facts.json（{exc}）", file=sys.stderr)
        return 2
    if args.text:
        print(res["preview"] or res["hint"])
    else:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_issue_url(args):
    run = os.path.realpath(args.run)
    try:
        res = detect(run, args.min_gb)
    except (OSError, ValueError) as exc:
        print(f"错误：读不到 {run}/facts.json（{exc}）", file=sys.stderr)
        return 2
    if not res["gaps"]:
        print("没有认不出来源的大目录，不需要反馈")
        return 1
    url, body, omitted = build_issue(res)
    if args.json:
        print(json.dumps({"url": url, "body": body, "omitted": omitted, "fallback": FALLBACK_PAGE},
                         ensure_ascii=False, indent=2))
        return 0
    print("将要提交的全文（请先给用户看）：")
    print("----------------------------------------")
    print(body)
    print("----------------------------------------")
    print("预填链接（用户点开后自己按“提交”，这个脚本不会发送任何东西）：")
    print(url)
    print()
    print(fallback_line())
    return 0


def cmd_decline(args):
    profile = prefs.load()
    profile["feedback_declined_at"] = prefs.now_iso()
    prefs.save(profile)
    print(f"已记下：{COOLDOWN_DAYS} 天内不再询问反馈")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="叁笙清理：知识缺口反馈（只生成文字，不发送）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gaps", help="找出认不出来源的大目录，输出 JSON")
    g.add_argument("--run", required=True)
    g.add_argument("--min-gb", type=float, default=DEFAULT_MIN_GB, help="体积阈值（GB，默认 1）")
    g.add_argument("--text", action="store_true", help="只输出给用户看的预览全文")
    u = sub.add_parser("issue-url", help="生成预填好的 GitHub Issue 链接")
    u.add_argument("--run", required=True)
    u.add_argument("--min-gb", type=float, default=DEFAULT_MIN_GB)
    u.add_argument("--json", action="store_true", help="以 JSON 输出链接与正文")
    sub.add_parser("decline", help="用户说“不用”：记下时间，90 天内不再问")
    args = ap.parse_args(argv)
    return {"gaps": cmd_gaps, "issue-url": cmd_issue_url, "decline": cmd_decline}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
