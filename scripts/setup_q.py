"""叁笙清理 0.6：初始设置的题库、出题条件、导出文本解析与答案落地。

题库在这里只有一份（单一真源）：设置页的题目、选项、推荐和“用在哪 / 答错了会怎样”都从这里生成；
plan.py setup 把 build_payload 的结果注入 assets/setup.html，plan.py profile --apply 用 parse_setup / apply_answers 存回本机。

安全不变式（与 0.5 台账相同）：设置只改变“问不问、预填什么、进哪一档”，不改变“能不能放行”；
唯一的例外是更严的方向（别碰、永不读）。这里只写 profile，不碰任何文件，也不产生任何清理授权。
"""
import copy
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import prefs  # noqa: E402
import protect  # noqa: E402
import units  # noqa: E402

SETUP_VERSION = 1
SETUP_TITLE = "叁笙清理 · 初始设置"
CLOUD_ENABLED = True            # 第 11 题（云盘只留云端）：只读检测 + 官方入口 + 列最大的已下载文件夹，不执行任何释放；关掉时不出题，但答案仍能解析

GB = 10 ** 9
MB = 10 ** 6
PRIVACY_NOTE = "读的内容会发给你所用的 AI 服务商（如 Claude、OpenAI），不只在本机看"

READ_SCOPES = ("basic", "extended", "deep")             # never 永远隐含，不存
TOOL_STATES = ("often", "sometimes", "unused")
CONV_DAYS = (7, 15, 30, 60, 90)
BIG_FILE_DAYS = (30, 90, 0)
WAIT_TO_DEFAULT = {"minimal": "safe", "some": "recommended", "space": "all"}
DEFAULT_TO_WAIT = {v: k for k, v in WAIT_TO_DEFAULT.items()}
CONV_RETENTION = {"done": 7, "sometimes": 30, "library": 0}
STALE_TOOL_DAYS = 30                                    # 30 天内用过的工具，推荐“常用”
TIME_MACHINE_STALE_DAYS = 14                            # 最近备份超过这么多天，当作“没有备份”

# 工具键 = 知识卡名
TOOL_NAMES = {
    "android": "Android 模拟器", "xcode": "Xcode 与 iOS 模拟器", "docker": "Docker", "model-caches": "本地 AI 模型",
    "creative-media": "剪辑与创作软件", "codex": "Codex", "claude-code": "Claude Code", "grok": "Grok",
    "gemini-antigravity": "Gemini / Antigravity", "cursor": "Cursor",
}
TOOL_KEYS = tuple(TOOL_NAMES)
AI_TOOL_KEYS = ("codex", "claude-code", "grok", "gemini-antigravity", "cursor")
# 这几个工具在台账里有对应的环境项目；“常用 / 不用了”同步成 进行中 / 做完了
TOOL_ENV = {"android": "env:android", "xcode": "env:xcode", "docker": "env:docker", "model-caches": "env:models"}
TOOL_STATE_STATUS = {"often": "active", "unused": "done"}

# 单选题 → (profile 字段, 允许的值)
SINGLE = {
    "conversations": ("conversation_policy", ("done", "sometimes", "library")),
    "rejects": ("rejects_policy", ("drop", "until_done", "keep")),
    "worktrees": ("worktree_policy", ("whole", "build_only", "ask")),
    "installers": ("installer_policy", ("auto", "ask")),
    "cloud": ("cloud_policy", ("ok", "offline", "unsure")),
    "done_projects": ("done_project_policy", ("intermediate", "archive", "keep")),
    "governance": ("governance_mode", ("tell", "guide", "fix")),
    "delete_mode": ("delete_mode", ("trash", "delete")),
    "confirm": ("confirm_mode", ("smart", "board")),
}
ANSWER_IDS = ("read_scope", "tools", "deliverables", "protect", "conversations", "conversation_days", "rejects",
              "worktrees", "installers", "big_files", "cloud", "wait", "done_projects", "governance",
              "delete_mode", "confirm", "note")

GROUPS = [
    ("read", "我会看什么", "先定我能看哪些文件。看不到的部分，我只会变成问题来问你。"),
    ("machine", "你电脑上有什么", "这些是我在你电脑上认出来的，告诉我哪些还在用、哪些不能碰。"),
    ("ai", "你用 AI 的习惯", "AI 工具会留下大量对话、图片和工作树，按你的习惯来处理。"),
    ("files", "日常文件", "下载、录屏这些你自己放的东西，怎么列出来问你。"),
    ("way", "清理的方式", "清多快、删到哪、怎么跟你确认。"),
    ("note", "还有想让我知道的吗", "随便写，我会翻译成规则，在第一张清理看板上请你确认后才生效。"),
]

PROJECT_ROW_FIELDS = ("key", "name", "kind", "bytes", "last_active", "sessions", "deliverables_guess", "default_status")


# ----------------------------------------------------------------- 小工具

def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _parse_ts(v):
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _days_since(iso, now):
    t = _parse_ts(iso)
    return max(0, (now - t).days) if t else None


def _now():
    return datetime.now(timezone.utc)


def _safe_line(s):
    """导出文本里的 JSON 必须单行：U+2028 / 2029 / NEL 等 splitlines 会断开的字符转义掉（与页面里的 safeLine 一致）。"""
    return re.sub("[\u0085  \x0b\x0c\x1c\x1d\x1e]", lambda m: "\\u%04x" % ord(m.group(0)), s)


def setup_version(profile):
    """profile 里记的设置版本；没有返回 0。"""
    s = profile.get("setup") if isinstance(profile, dict) else None
    v = s.get("version") if isinstance(s, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def stage_of(profile):
    """onboarding = 第一次使用（既没有 setup.version，也没有 0.5 的 onboarded_at）；其余都是 cleanup。"""
    if setup_version(profile) == 0 and not (isinstance(profile, dict) and profile.get("onboarded_at")):
        return "onboarding"
    return "cleanup"


def setup_upgrade(profile):
    """老用户（已设置过）但题库比他答过的新：清理看板之前先补问新题。第一次使用不算。"""
    return stage_of(profile) == "cleanup" and setup_version(profile) < SETUP_VERSION


def auto_mode(profile):
    """没指定模式时按 profile 判断：第一次 first；设置过但题库更新了 upgrade；都是最新的 edit（改设置）。"""
    if stage_of(profile) == "onboarding":
        return "first"
    return "upgrade" if setup_upgrade(profile) else "edit"


# ----------------------------------------------------------------- 本机事实（projects.json）

def facts_of(pfile):
    """把 projects.json 里出题要用的部分取出来；缺的新字段按空处理（0.5 的 projects.json 没有 tools / setup_facts）。"""
    pfile = pfile if isinstance(pfile, dict) else {}
    sf = pfile.get("setup_facts") if isinstance(pfile.get("setup_facts"), dict) else {}
    tools = []
    for t in pfile.get("tools") if isinstance(pfile.get("tools"), list) else []:
        if isinstance(t, dict) and t.get("key") in TOOL_KEYS:
            tools.append(dict(t, bytes=_int(t.get("bytes"))))
    projects = [p for p in pfile.get("projects") or [] if isinstance(p, dict) and p.get("shown") and p.get("key")]
    cands = [c for c in pfile.get("deliverable_candidates") or [] if isinstance(c, dict) and isinstance(c.get("path"), str)]
    tops = [d for d in sf.get("top_dirs") or [] if isinstance(d, dict) and isinstance(d.get("path"), str)]
    return {
        "sf": {k: _int(sf.get(k)) for k in ("ai_sessions_bytes", "ai_images_bytes", "worktrees", "installers_bytes",
                                            "recordings_bytes", "cloud_local_bytes")},
        "pipelines": bool(sf.get("pipelines")),
        "time_machine_last": sf.get("time_machine_last") if isinstance(sf.get("time_machine_last"), str) else None,
        "tools": tools, "projects": projects, "candidates": cands, "top_dirs": tops,
    }


def _big_tools(F):
    return [t for t in F["tools"] if t["bytes"] >= GB]


# ----------------------------------------------------------------- 题库

def _opt(value, label, desc=""):
    return {"value": value, "label": label, "desc": desc}


def _ai_names(F):
    names = [TOOL_NAMES[t["key"]] for t in F["tools"] if t["key"] in AI_TOOL_KEYS and t["bytes"] > 0]
    return "、".join(names[:3]) + (" 等" if len(names) > 3 else "") if names else "AI 工具"


def _ctx_read(F, ctx):
    return "这是第二轮能读到哪些文件的总开关；读不到的部分只会变成问题来问你。成果页会列出这次读了哪些文件"


def _ctx_tools(F, ctx):
    big = _big_tools(F)
    return f"检测到 {len(big)} 个，合计 {units.fmt_bytes(sum(t['bytes'] for t in big))}，下面每行写了体积和最后使用时间"


def _ctx_projects(F, ctx):
    return f"从你的 Git 仓库、AI 对话和工具目录里认出 {len(F['projects'])} 个项目"


def _ctx_deliverables(F, ctx):
    n, img = len(F["candidates"]), F["sf"]["ai_images_bytes"]
    bits = []
    if n:
        bits.append(f"找到 {n} 个像成品目录的文件夹")
    if img:
        bits.append(f"AI 生成的图片和视频共 {units.fmt_bytes(img)}")
    return "；".join(bits) or "还没认出像成品目录的文件夹，可以自己补充"


def _ctx_protect(F, ctx):
    return "家目录下的一级文件夹和各自体积见下面；大多数人不用填"


def _ctx_conversations(F, ctx):
    names = _ai_names(F)
    names = names[:-2] + "等工具" if names.endswith(" 等") else names + " "
    return f"找到 {names}的对话记录，共 {units.fmt_bytes(F['sf']['ai_sessions_bytes'])}"


def _ctx_rejects(F, ctx):
    img = F["sf"]["ai_images_bytes"]
    return f"AI 生成的图片和视频共 {units.fmt_bytes(img)}" if img else "找到了出图、出视频用的流水线工作目录"


def _ctx_worktrees(F, ctx):
    return f"找到 {F['sf']['worktrees']} 个 AI 写代码时开过的工作树"


def _ctx_installers(F, ctx):
    return f"下载等目录里的安装包共 {units.fmt_bytes(F['sf']['installers_bytes'])}"


def _ctx_big(F, ctx):
    return f"桌面、影片等目录里的录屏和大视频共 {units.fmt_bytes(F['sf']['recordings_bytes'])}"


def _ctx_cloud(F, ctx):
    return f"iCloud 或网盘在本机存了 {units.fmt_bytes(F['sf']['cloud_local_bytes'])}"


def _ctx_delete_mode(F, ctx):
    d = _days_since(F["time_machine_last"], ctx["now"])
    if d is None:
        return "最近一次 Time Machine 备份：没有读到（没配置，或读不到）"
    return f"最近一次 Time Machine 备份：{d} 天前" if d else "最近一次 Time Machine 备份：今天"


def _rec_delete_mode(F, ctx):
    """第一次使用、或没有备份（读不到 / 超过 14 天）时推荐先放废纸篓。"""
    d = _days_since(F["time_machine_last"], ctx["now"])
    if ctx["mode"] == "first" or d is None or d > TIME_MACHINE_STALE_DAYS:
        return "trash"
    return "delete"


def _always(F):
    return True


QUESTIONS = [
    {"id": "read_scope", "no": 1, "group": "read", "type": "multi", "since": 1, "legacy": False,
     "title": "我可以看哪些文件", "context": _ctx_read, "when": _always,
     "options": [
         _opt("basic", "基础：项目说明、脚本、日志、AI 对话的标题和开头",
              "读到的文字会发给你所用的 AI 服务商（如 Claude、OpenAI）"),
         _opt("extended", "扩展：聊天软件收到的文件、邮件附件、照片和截图",
              "只看名字、大小、类型、日期，不看内容"),
         _opt("deep", "深度：聊天正文、图片里的内容", "看得最准，但这些内容也会发给你所用的 AI 服务商"),
         dict(_opt("never", "永不读：密码、钥匙串、浏览器登录数据、密钥、.env 文件", "由脚本强制，不能勾掉"),
              locked=True, checked=True),
     ],
     "recommended": ["basic", "extended"],
     "why": "决定第二轮能读哪些文件来判断东西是做什么用的；读不到的部分只能变成问题问你",
     "risk": "选窄了，第二轮的问题会变多；选宽了，读到的内容会发给你的 AI 服务商，所以每档旁边都写明了"},
    {"id": "tools", "no": 2, "group": "machine", "type": "tools", "since": 1, "legacy": False,
     "title": "这些软件和开发环境你还用吗", "context": _ctx_tools, "when": lambda F: bool(_big_tools(F)),
     "options": [_opt("often", "常用", "清理时跳过深查，省时间"),
                 _opt("sometimes", "偶尔", "不改变判断（没回答就按这个）"),
                 _opt("unused", "不用了", "整套环境进入判断，题目预填“删”，仍要你逐项确认")],
     "recommended": None,
     "why": "“不用了”的整套环境会进入清理判断并预填“删”；“常用”的会跳过深查",
     "risk": "误标“不用了”：每一项仍要在清理看板上逐项确认，并写明重新下载要多久"},
    {"id": "projects", "no": 3, "group": "machine", "type": "projects", "since": 1, "legacy": True,
     "title": "你的项目是在做，还是做完了", "context": _ctx_projects, "when": lambda F: bool(F["projects"]),
     "options": [_opt("active", "进行中", "里面的东西一律不出删除卡"),
                 _opt("done", "做完了", "中间稿和对话可以进入判断，成品仍要有副本才放行"),
                 _opt("archived", "归档", "同做完了；连原本“删了找不回”的东西也会按推荐勾上"),
                 _opt("protected", "别碰", "整个目录加入保护名单")],
     "recommended": None,
     "why": "项目状态决定里面的东西出不出删除卡、问题预填什么",
     "risk": "标“做完了”不能单独让任何东西直接放行；成品没有副本或交付证据，照样只是问你"},
    {"id": "deliverables", "no": 4, "group": "machine", "type": "paths", "since": 1, "legacy": True,
     "title": "做好的成品放在哪个文件夹", "context": _ctx_deliverables,
     "when": lambda F: bool(F["projects"]) or F["sf"]["ai_images_bytes"] > 0,
     "options": [], "recommended": [],
     "why": "知道成品放在哪，才能核对“生成目录里的图在成品里有没有一模一样的一份”，放行 AI 一张图存三份的大头",
     "risk": "填错目录：副本按内容逐个比对，找不到就不放行，所以不会多删，只会少放行；太宽的目录（如整个文稿）会被拒收"},
    {"id": "protect", "no": 5, "group": "machine", "type": "paths", "since": 1, "legacy": True,
     "title": "有没有哪些文件夹，不管多久没动都不能碰", "context": _ctx_protect, "when": _always,
     "options": [], "recommended": [],
     "why": "加入保护名单，由脚本强制执行，我和清理脚本都动不了",
     "risk": "只会更保守，没有风险"},
    {"id": "conversations", "no": 6, "group": "ai", "type": "single", "since": 1, "legacy": False,
     "title": "用完的 AI 对话，对你来说是", "context": _ctx_conversations,
     "when": lambda F: F["sf"]["ai_sessions_bytes"] > 0,
     "options": [_opt("done", "做完就不再看", "超过 7 天没用的对话列出来清；已做完项目的对话预填“删”"),
                 _opt("sometimes", "偶尔回去接着做", "30 天内用过的留着，更早的列出来问你"),
                 _opt("library", "当资料库，都想留着", "对话一律不列出来")],
     "recommended": "sometimes",
     "why": "决定旧对话要不要列出来、留几天、做完的项目里的对话要不要预填“删”",
     "risk": "选“不再看”但其实还要：对话仍会逐个列出标题和日期，由你确认后才删",
     "days_options": list(CONV_DAYS)},
    {"id": "rejects", "no": 7, "group": "ai", "type": "single", "since": 1, "legacy": False,
     "title": "AI 生成的图和视频，没被挑中的（试画、废稿）怎么办", "context": _ctx_rejects,
     "when": lambda F: F["sf"]["ai_images_bytes"] > 0 or F["pipelines"],
     "options": [_opt("drop", "不留", "没挑中的图预填“删”"),
                 _opt("until_done", "留到项目做完", "项目做完前不动"),
                 _opt("keep", "都留着", "找不到成品副本的图一律保留")],
     "recommended": "until_done",
     "why": "生成图里找不到成品副本的那部分，按这题决定删、留还是问",
     "risk": "依赖第 4 题：成品位置没填全时，被挑中的图也可能看起来“没有副本”，所以这类东西最多帮你预选，仍要你确认"},
    {"id": "worktrees", "no": 8, "group": "ai", "type": "single", "since": 1, "legacy": False,
     "title": "AI 写代码时开过的工作树，代码合进主线以后", "context": _ctx_worktrees,
     "when": lambda F: F["sf"]["worktrees"] > 0,
     "options": [_opt("whole", "整棵删掉", "工作树卡预填“删”"),
                 _opt("build_only", "只清里面的构建产物", "整棵工作树先留着，每次再问"),
                 _opt("ask", "每次问我", "不预填，也不适用“以后都删”的规则")],
     "recommended": "build_only",
     "why": "决定工作树卡怎么提议：整棵删、只清构建产物，还是每次问",
     "risk": "还没合并、有未提交改动的工作树不受影响，这是硬规则"},
    {"id": "installers", "no": 9, "group": "files", "type": "single", "since": 1, "legacy": False,
     "title": "软件装好后留下的安装包", "context": _ctx_installers,
     "when": lambda F: F["sf"]["installers_bytes"] >= 300 * MB,
     "options": [_opt("auto", "直接清", "已装好同一版本的安装包不再列出来问"),
                 _opt("ask", "每次问我", "每次都列出来")],
     "recommended": "auto",
     "why": "“直接清”会把安装包当成一条规则；只对已装好同一版本的生效，认不出来的照样问你",
     "risk": "官网能重新下载，最多是要再下一次"},
    {"id": "big_files", "no": 10, "group": "files", "type": "single", "since": 1, "legacy": False,
     "title": "录屏和大视频，超过多久没动就列出来问你", "context": _ctx_big,
     "when": lambda F: F["sf"]["recordings_bytes"] >= GB,
     "options": [_opt(30, "30 天", "更早的才列出来"), _opt(90, "90 天", "更早的才列出来"),
                 _opt(0, "别问我", "录屏和大视频一律不列")],
     "recommended": 90,
     "why": "决定大旧文件的时间门槛；这类东西没有副本，只会列出来问你，不会自动删",
     "risk": "只影响问得多还是问得少"},
    {"id": "cloud", "no": 11, "group": "files", "type": "single", "since": 1, "legacy": False,
     "title": "iCloud 或网盘里已下载到本机的文件，能接受“只留云端”吗", "context": _ctx_cloud,
     "when": lambda F: CLOUD_ENABLED and F["sf"]["cloud_local_bytes"] >= 5 * GB,
     "options": [_opt("ok", "可以", "文件还留在云上，本机只留一个占位，用到时再下载；我会列出占地方最大的文件夹，告诉你在哪点"),
                 _opt("offline", "不行，我要离线用", "不提云盘方面的建议"),
                 _opt("unsure", "不确定", "先把建议列出来给你看，要不要做你来定")],
     "recommended": "unsure",
     "why": "只用来在看板里给建议、指路各家官方的操作入口；我不会替你释放，也不会动云盘里的任何文件",
     "risk": "不动文件，没有风险。你自己动手前请先确认没有“正在上传”的文件，并用官方入口的“移除下载 / 释放空间”；"
             "直接删除或把文件拖出云盘，会连云端的一起删掉"},
    {"id": "wait", "no": 12, "group": "way", "type": "single", "since": 1, "legacy": True,
     "title": "清掉以后要重新下载、重新生成时，你能等多久", "context": lambda F, ctx: "清掉的缓存有的几秒就能重新下载，有的（比如本地模型、编译缓存）要等很久",
     "when": _always,
     "options": [_opt("minimal", "尽量别让我等", "默认只勾删了不丢东西、马上能补回的"),
                 _opt("some", "可以等一会儿", "默认按我的推荐勾，重新生成慢的不勾"),
                 _opt("space", "空间优先", "连重新下载很慢的也一起勾；删了找不回的仍要你逐个确认")],
     "recommended": "some",
     "why": "决定默认勾选到哪一档；重建很慢的缓存（如本地模型）只在“空间优先”时进默认勾选",
     "risk": "只影响默认勾选，每次执行前仍有看板或一句话确认"},
    {"id": "done_projects", "no": 13, "group": "way", "type": "single", "since": 1, "legacy": False,
     "title": "做完的项目，素材通常怎么处理", "context": lambda F, ctx: "做完的项目常常留着几 GB 的草稿、中间稿和渲染文件",
     "when": _always,
     "options": [_opt("intermediate", "删中间稿，留成品", "做完项目的中间稿预填“删”"),
                 _opt("archive", "整个搬到移动硬盘", "预填“归档”，由你指定外接硬盘"),
                 _opt("keep", "先都留着", "做完的项目不再预填")],
     "recommended": "intermediate",
     "why": "做完了的项目，清理建议是删、归档还是不动",
     "risk": "成品始终要先有副本或交付证据；归档前会先核对复制是否一致"},
    {"id": "governance", "no": 14, "group": "way", "type": "single", "since": 1, "legacy": False,
     "title": "发现某样东西会一直涨（比如 AI 工具一直存对话），你希望", "context": lambda F, ctx: "AI 工具的对话、批量出图脚本这类东西，删了还会再涨回来",
     "when": _always,
     "options": [_opt("tell", "只告诉我", "只说现象"),
                 _opt("guide", "告诉我改法，我自己改", "给出具体设置路径"),
                 _opt("fix", "你帮我改，改之前问我", "同意后我来改，并且可以撤销")],
     "recommended": "fix",
     "why": "决定治本建议只写在卡上，还是你同意后由我直接改工具设置或脚本",
     "risk": "每次改动都单独确认，并且给出怎么撤销"},
    {"id": "delete_mode", "no": 15, "group": "way", "type": "single", "since": 1, "legacy": False,
     "title": "删掉的东西怎么处理", "context": _ctx_delete_mode, "when": _always,
     "options": [_opt("trash", "先放进废纸篓，过几天没问题再清空", "清空废纸篓之前空间不会释放"),
                 _opt("delete", "直接删，马上腾出空间", "删了找不回")],
     "recommended": _rec_delete_mode,
     "why": "决定执行时是移入废纸篓还是直接删除",
     "risk": "选“直接删”且没有备份：删了找不回，所以执行前还要再确认一次"},
    {"id": "confirm", "no": 16, "group": "way", "type": "single", "since": 1, "legacy": False,
     "title": "以后怎么跟你确认", "context": lambda F, ctx: "清理前总要你点头，可以在对话里说一句，也可以在网页里逐项看", "when": _always,
     "options": [_opt("smart", "小事在对话里一句话确认，大事发看板", ""),
                 _opt("board", "每次都发看板", "")],
     "recommended": "smart",
     "why": "决定什么时候在对话里问、什么时候发看板",
     "risk": "只影响确认方式，该确认的一项也不会少"},
    {"id": "note", "no": 17, "group": "note", "type": "text", "since": 1, "legacy": False,
     "title": "还有什么想让我知道的", "context": lambda F, ctx: "例：“下载里的视频都是课程，别动”“~/work 是公司电脑的同步盘”",
     "when": _always, "options": [], "recommended": "",
     "why": "我会把这句话翻译成规则或备注，在第一张清理看板上请你确认一次后才生效；原话不存，只存翻译后的规则",
     "risk": "翻译错了你会在看板上看到并划掉，不会直接生效"},
]
_BY_ID = {q["id"]: q for q in QUESTIONS}


def is_new_question(spec, version):
    """upgrade 模式要不要出这道题：设置过（version≥1）看 since；0.5 老用户（没有 setup.version）跳过 0.5 已经问过的题。"""
    if version >= 1:
        return spec["since"] > version
    return not spec["legacy"]


# ----------------------------------------------------------------- 当前值（edit 模式预填）

def current_value(qid, profile):
    p = profile if isinstance(profile, dict) else {}
    if qid == "read_scope":
        v = p.get("read_scope")
        return [x for x in v if x in READ_SCOPES] if isinstance(v, list) else None
    if qid == "tools":
        return dict(p["tools"]) if isinstance(p.get("tools"), dict) else None
    if qid == "deliverables":
        v = p.get("deliverable_roots")
        return list(v) if isinstance(v, list) and v else None
    if qid == "protect":
        v = p.get("protect")
        return list(v) if isinstance(v, list) and v else None
    if qid == "big_files":
        v = p.get("big_file_age_days")
        return v if v in BIG_FILE_DAYS and isinstance(v, int) and not isinstance(v, bool) else None
    if qid == "wait":
        return DEFAULT_TO_WAIT.get(p.get("default_choice"))
    if qid in SINGLE:
        field, allowed = SINGLE[qid]
        return p.get(field) if p.get(field) in allowed else None
    return None


# ----------------------------------------------------------------- 工具行 / 项目行

def project_row(p, ledger):
    """给页面看的项目行：只带显示用字段。roots / claude_dirs / session_ids 是 check 判断归属用的，不进页面。"""
    row = {k: p.get(k) for k in PROJECT_ROW_FIELDS if k in p}
    e = ledger.get(p.get("key")) if isinstance(ledger.get(p.get("key")), dict) else None
    row["status"] = e.get("status") if e and e.get("status") in protect.PROJECT_STATUSES else None
    row["deliverables"] = list(e.get("deliverables") or []) if e else []
    row["in_ledger"] = bool(e)
    if e and e.get("name"):
        row["name"] = e["name"]
    return row


def onboard_projects(profile, pfile):
    """本次扫描 shown 的项目（体积降序，projects.json 已排好），台账里已有的状态与成品目录一并带上。"""
    ledger = profile.get("projects") if isinstance(profile.get("projects"), dict) else {}
    return [project_row(p, ledger) for p in (pfile or {}).get("projects", []) if isinstance(p, dict) and p.get("shown") and p.get("key")]


def tool_rows(F, profile, now):
    cur = profile.get("tools") if isinstance(profile.get("tools"), dict) else {}
    rows = []
    for t in sorted(_big_tools(F), key=lambda x: -x["bytes"]):
        d = _days_since(t.get("last_used"), now)
        rows.append({"key": t["key"], "name": t.get("name") or TOOL_NAMES[t["key"]], "bytes": t["bytes"],
                     "last_used": t.get("last_used"), "paths": t.get("paths") or [],
                     "recommended": "often" if d is not None and d <= STALE_TOOL_DAYS else None,
                     "current": cur.get(t["key"]) if cur.get(t["key"]) in TOOL_STATES else None})
    return rows


# ----------------------------------------------------------------- 设置页 payload（约定第 5 节）

def _question_object(spec, F, profile, ctx):
    rec = spec["recommended"]
    rec = rec(F, ctx) if callable(rec) else rec
    q = {"id": spec["id"], "no": spec["no"], "group": spec["group"], "type": spec["type"], "title": spec["title"],
         "context": spec["context"](F, ctx), "options": [dict(o) for o in spec["options"]],
         "recommended": rec, "current": current_value(spec["id"], profile) if ctx["mode"] == "edit" else None,
         "why": spec["why"], "risk": spec["risk"]}
    if spec["id"] == "tools":
        q["recommended"] = {r["key"]: r["recommended"] for r in tool_rows(F, profile, ctx["now"]) if r["recommended"]}
    if spec["id"] == "conversations":
        q["days_options"] = q["days"] = list(CONV_DAYS)            # days：设置页读这个名字；days_options 是同一份
        q["days_recommended"] = {"done": CONV_RETENTION["done"], "sometimes": CONV_RETENTION["sometimes"]}
        days = profile.get("retention_days")
        q["current_days"] = days if ctx["mode"] == "edit" and days in CONV_DAYS else None
    if spec["id"] == "delete_mode":
        q["time_machine_last"] = F["time_machine_last"]
    return q


def build_payload(profile, pfile, mode, now=None):
    """设置页数据。mode：first（第一次，出全部适用的题）/ upgrade（只出新题，不出项目表）/ edit（全部适用的题，current 填现值）。
    pfile 是 projects.json 内容（可以是 None：当作空机器，只出总是出现的题）。"""
    if mode not in ("first", "upgrade", "edit"):
        raise ValueError(f"mode 只能是 first / upgrade / edit，收到 {mode!r}")
    profile = profile if isinstance(profile, dict) else {}
    now = now or _now()
    F = facts_of(pfile)
    ctx = {"mode": mode, "now": now}
    ver = setup_version(profile)
    questions = [s for s in QUESTIONS if s["when"](F) and (mode != "upgrade" or is_new_question(s, ver))]
    ids = {s["id"] for s in questions}
    groups = []
    for gid, title, desc in GROUPS:
        qs = [_question_object(s, F, profile, ctx) for s in questions if s["group"] == gid]
        if qs:
            groups.append({"id": gid, "title": title, "desc": desc, "questions": qs})
    return {
        "version": SETUP_VERSION, "scan_id": (pfile or {}).get("scan_id") or "", "mode": mode, "groups": groups,
        "projects": onboard_projects(profile, pfile) if "projects" in ids else [],
        "deliverable_candidates": F["candidates"] if "deliverables" in ids else [],
        "top_dirs": F["top_dirs"] if "protect" in ids else [],
        "tools": tool_rows(F, profile, now) if "tools" in ids else [],
        "time_machine_last": F["time_machine_last"],
        "privacy_note": PRIVACY_NOTE,
    }


# ----------------------------------------------------------------- 清理看板顶部“按你的设置”

def summary_lines(profile):
    """清理看板顶部的“按你的设置”：按 profile 生成，一项一句，没设过的项不写。"""
    p = profile if isinstance(profile, dict) else {}
    out = []
    rs = p.get("read_scope")
    if isinstance(rs, list):
        names = {"basic": "基础", "extended": "扩展", "deep": "深度"}
        got = "、".join(names[x] for x in rs if x in names)
        out.append(f"我会看的文件：{got or '只看文件名和大小'}（密码、钥匙串、密钥永远不读）")
    cp = p.get("conversation_policy")
    days = p.get("retention_days") if p.get("retention_days") in CONV_DAYS else None
    if cp == "library":
        out.append("旧对话：当资料库，都留着")
    elif cp == "done":
        out.append(f"旧对话：做完就不再看（{days or 7} 天内用过的留着）")
    elif cp == "sometimes":
        out.append(f"旧对话：偶尔回去接着做（{days or 30} 天内用过的留着）")
    for key, lines in (
        ("rejects_policy", {"drop": "没挑中的 AI 图：不留", "until_done": "没挑中的 AI 图：留到项目做完",
                            "keep": "没挑中的 AI 图：都留着"}),
        ("worktree_policy", {"whole": "合进主线的工作树：整棵删", "build_only": "合进主线的工作树：只清构建产物",
                             "ask": "合进主线的工作树：每次问你"}),
        ("installer_policy", {"auto": "安装包：装好后直接清", "ask": "安装包：每次问你"}),
    ):
        if p.get(key) in lines:
            out.append(lines[p[key]])
    bf = p.get("big_file_age_days")
    if bf in BIG_FILE_DAYS and isinstance(bf, int) and not isinstance(bf, bool):
        out.append("录屏和大视频：不问你" if bf == 0 else f"录屏和大视频：超过 {bf} 天没动才问你")
    wait = DEFAULT_TO_WAIT.get(p.get("default_choice"))
    if wait:
        out.append({"minimal": "重新下载要等：尽量别让你等（默认只勾不丢东西的）", "some": "重新下载要等：可以等一会儿（默认按推荐勾）",
                    "space": "重新下载要等：空间优先（重新下载慢的也勾上）"}[wait])
    for key, lines in (
        ("done_project_policy", {"intermediate": "做完的项目：删中间稿，留成品", "archive": "做完的项目：整个搬到移动硬盘",
                                 "keep": "做完的项目：先都留着"}),
        ("governance_mode", {"tell": "会一直涨的东西：只告诉你", "guide": "会一直涨的东西：告诉你改法",
                             "fix": "会一直涨的东西：同意后我来改，可撤销"}),
        ("delete_mode", {"trash": "删掉的东西：先放进废纸篓", "delete": "删掉的东西：直接删"}),
        ("confirm_mode", {"smart": "确认方式：小事对话里问，大事发看板", "board": "确认方式：每次都发看板"}),
    ):
        if p.get(key) in lines:
            out.append(lines[p[key]])
    tools = p.get("tools") if isinstance(p.get("tools"), dict) else {}
    unused = [TOOL_NAMES[k] for k, v in tools.items() if v == "unused" and k in TOOL_NAMES]
    if unused:
        out.append("不用了的软件：" + "、".join(unused))
    return out


# ----------------------------------------------------------------- 导出文本（约定第 3 节）

def is_setup_export(text):
    """文本抬头是“叁笙清理 · 初始设置”：这是保存设置的导出，不是清理清单（没有清理授权）。"""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip().startswith(SETUP_TITLE)
    return False


def export_text(answers, ledger=None, note=None, version=SETUP_VERSION):
    """生成导出文本（页面里有同样功能的 JS；Python 端给测试和脚本用）。"""
    lines = [SETUP_TITLE, f"设置版本: {version}",
             "答案: " + _safe_line(json.dumps(answers, ensure_ascii=False, separators=(",", ":")))]
    if ledger is not None:
        lines.append("台账: " + _safe_line(json.dumps({"项目": ledger}, ensure_ascii=False, separators=(",", ":"))))
    if note:
        lines.append("补充: " + _safe_line(json.dumps(note, ensure_ascii=False)))
    return "\n".join(lines) + "\n"


def parse_setup(text):
    """解析初始设置导出。返回 ({version, answers, ledger, note}, 错误列表)；有错误时第一项为 None。
    只认抬头和下面几行：设置版本 / 答案 / 台账（0.5 格式，可缺）/ 补充（JSON 字符串，可缺），其余行忽略。
    这种导出只保存设置，不含任何清理授权。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None, ["设置文本是空的"]
    if not lines[0].startswith(SETUP_TITLE):
        return None, [f"首行不是“{SETUP_TITLE}”（设置页“复制”的导出文本）"]
    raw, errs = {}, []
    for ln in lines[1:]:
        m = re.match(r"^(设置版本|答案|补充)\s*[:：]\s*(.*)$", ln)
        if not m:
            continue
        if m.group(1) in raw:
            errs.append(f"“{m.group(1)}:”行出现了多次")
        raw[m.group(1)] = m.group(2).strip()
    version = None
    if "设置版本" in raw:
        try:
            version = int(raw["设置版本"])
        except ValueError:
            errs.append(f"设置版本必须是整数，收到 {raw['设置版本']!r}")
    else:
        errs.append("缺少“设置版本:”行")
    answers = {}
    if "答案" in raw:
        try:
            answers = json.loads(raw["答案"])
        except ValueError as exc:
            errs.append(f"“答案:”行不是合法的 JSON：{exc}")
        else:
            if not isinstance(answers, dict):
                errs.append("“答案:”行的 JSON 必须是对象")
                answers = {}
    note = None
    if "补充" in raw and raw["补充"]:
        try:
            note = json.loads(raw["补充"])
        except ValueError as exc:
            errs.append(f"“补充:”行不是合法的 JSON 字符串：{exc}")
        else:
            if not isinstance(note, str):
                errs.append("“补充:”行必须是一个 JSON 字符串")
                note = None
    ledger, lerrs = prefs.parse_ledger(text)
    errs += lerrs
    if isinstance(answers, dict) and isinstance(answers.get("note"), str) and answers["note"].strip() and not note:
        note = answers["note"]                    # 兼容把补充放进答案里的写法
    if "答案" not in raw and ledger is None and not note:
        errs.append("没有找到“答案:”或“台账:”行")
    if errs:
        return None, errs
    return {"version": version, "answers": answers, "ledger": ledger, "note": (note or "").strip() or None}, []


# ----------------------------------------------------------------- 答案 → profile（约定第 2 节映射）

def _paths_answer(profile, val, deliverable, label, qid, done, errs):
    if not isinstance(val, list):
        raise ValueError(f"{label}必须是路径数组")
    paths, e = prefs._home_paths(val, deliverable=deliverable)
    errs += e
    old = ((profile.get("setup") or {}).get("answers") or {}).get(qid)
    if qid == "deliverables":
        # 重新设置时，上一次由设置页写进去的成品目录先去掉（用户可以在编辑里取消），再并入这一次的；别处加的目录不动
        gone = set(old) if isinstance(old, list) else set()
        for key in ("deliverable_roots", "reference_roots"):
            cur = [x for x in (profile.get(key) or []) if x not in gone or x in paths]
            profile[key] = list(dict.fromkeys(cur + paths))
        if paths:
            done.append("成品目录（也在这里查引用）：" + "、".join(paths))
    else:
        # 保护名单只增不减：设置页只会让保护更严
        profile["protect"] = list(dict.fromkeys(list(profile.get("protect") or []) + paths))
        if paths:
            done.append("永不触碰：" + "、".join(paths))
    return paths


def _apply_one(profile, qid, val, answers, done, errs, scan_id=None):
    """返回要记进 setup.answers 的规范化值；不合法抛 ValueError。"""
    if qid == "read_scope":
        if not isinstance(val, list):
            raise ValueError("读取范围必须是数组")
        scope = list(dict.fromkeys(x for x in val if x in READ_SCOPES))
        profile["read_scope"] = scope
        names = {"basic": "基础", "extended": "扩展", "deep": "深度"}
        done.append("读取范围：" + ("、".join(names[x] for x in scope) if scope else "只读文件名和大小")
                    + "（密码、钥匙串、登录数据、密钥永远不读）")
        return scope
    if qid == "tools":
        if not isinstance(val, dict):
            raise ValueError("软件使用情况必须是对象（工具键: often / sometimes / unused）")
        good = {}
        for k, v in val.items():
            if k not in TOOL_KEYS:
                errs.append(f"不认识的工具：{k}")
            elif v not in TOOL_STATES:
                errs.append(f"工具 {k} 的回答只能是 often / sometimes / unused，收到 {v!r}")
            else:
                good[k] = v
        tools = dict(profile["tools"]) if isinstance(profile.get("tools"), dict) else {}
        tools.update(good)
        profile["tools"] = tools
        for k, v in good.items():                          # 同步台账里的环境项目；已经标“别碰”的不改（别碰只会更严）
            env_key, status = TOOL_ENV.get(k), TOOL_STATE_STATUS.get(v)
            if not env_key or not status:
                continue
            old = (profile.get("projects") or {}).get(env_key)
            if isinstance(old, dict) and old.get("status") == "protected":
                continue
            _, e, _ = prefs.apply_ledger(profile, {env_key: {"status": status}}, scan_id)
            errs += e
        if good:
            zh = {"often": "常用", "sometimes": "偶尔", "unused": "不用了"}
            done.append("软件与开发环境：" + "、".join(f"{TOOL_NAMES[k]}={zh[v]}" for k, v in good.items()))
        return good
    if qid == "deliverables":
        return _paths_answer(profile, val, True, "成品目录", qid, done, errs)
    if qid == "protect":
        return _paths_answer(profile, val, False, "别碰的目录", qid, done, errs)
    if qid == "conversations":
        if val not in SINGLE["conversations"][1]:
            raise ValueError(f"旧对话的回答只能是 done / sometimes / library，收到 {val!r}")
        profile["conversation_policy"] = val
        days = answers.get("conversation_days")
        if days is None:
            days = CONV_RETENTION[val]
        elif days not in CONV_DAYS or isinstance(days, bool):
            errs.append(f"对话保留天数只能是 {'/'.join(map(str, CONV_DAYS))}，收到 {days!r}；按默认天数处理")
            days = CONV_RETENTION[val]
        profile["retention_days"] = days
        done.append({"done": "旧对话：做完就不再看", "sometimes": "旧对话：偶尔回去接着做",
                     "library": "旧对话：当资料库，都留着"}[val]
                    + (f"（{days} 天内用过的留着）" if days else "（不按天数限制）"))
        return val
    if qid == "conversation_days":
        if val not in CONV_DAYS or isinstance(val, bool):
            raise ValueError(f"对话保留天数只能是 {'/'.join(map(str, CONV_DAYS))}，收到 {val!r}")
        profile["retention_days"] = val
        done.append(f"旧对话：{val} 天内用过的留着")
        return val
    if qid == "big_files":
        try:
            n = int(val)
        except (TypeError, ValueError):
            n = None
        if n not in BIG_FILE_DAYS or isinstance(val, bool):
            raise ValueError(f"录屏和大视频的回答只能是 {'/'.join(map(str, BIG_FILE_DAYS))}，收到 {val!r}")
        profile["big_file_age_days"] = n
        done.append("录屏和大视频：不问你" if n == 0 else f"录屏和大视频：超过 {n} 天没动才问你")
        return n
    if qid == "wait":
        if val not in WAIT_TO_DEFAULT:
            raise ValueError(f"重新下载等多久的回答只能是 minimal / some / space，收到 {val!r}")
        profile["default_choice"] = WAIT_TO_DEFAULT[val]
        done.append("默认档位：" + {"safe": "稳妥", "recommended": "推荐", "all": "全部可删"}[WAIT_TO_DEFAULT[val]])
        return val
    if qid in SINGLE:
        field, allowed = SINGLE[qid]
        if val not in allowed:
            raise ValueError(f"“{_BY_ID[qid]['title']}”的回答只能是 {' / '.join(allowed)}，收到 {val!r}")
        profile[field] = val
        label = next((o["label"] for o in _BY_ID[qid]["options"] if o["value"] == val), str(val))
        done.append(f"{_BY_ID[qid]['title']}：{label.split('：')[0]}")
        return val
    raise ValueError(f"不认识的设置项：{qid}")


# 设置页会改的 profile 字段（记忆日志据此对比前后）；保护名单只增不减，单独按“新增的路径”记
LOGGED_FIELDS = ("read_scope", "tools", "conversation_policy", "retention_days", "rejects_policy", "worktree_policy",
                 "installer_policy", "big_file_age_days", "cloud_policy", "done_project_policy", "governance_mode",
                 "delete_mode", "confirm_mode", "default_choice", "deliverable_roots", "reference_roots")


def _log_answer(profile, before, done_lines, scan_id):
    """这一题改了哪些字段，就写哪条记忆日志：第一个改动的字段是主字段（ref {field, old}），同题连带改的别的字段放进 also。
    “忘掉”据此把这些字段恢复成回答之前的样子；没变化的题不记。保护名单只增不减，按新增的路径记 protect。"""
    changed = [f for f in LOGGED_FIELDS if profile.get(f) != before.get(f)]
    if changed:
        main, rest = changed[0], changed[1:]
        ref = {"field": main, "old": copy.deepcopy(before.get(main)), "new": copy.deepcopy(profile.get(main))}
        if rest:
            ref["also"] = {f: copy.deepcopy(before.get(f)) for f in rest}
            ref["also_new"] = {f: copy.deepcopy(profile.get(f)) for f in rest}
        prefs._note(profile, "setting", "；".join(done_lines) or main, ref, scan_id)
    added = [x for x in (profile.get("protect") or []) if x not in (before.get("protect") or [])]
    if added:
        prefs._note(profile, "protect", "永不触碰：" + "、".join(added), {"paths": added}, scan_id)


def apply_answers(profile, answers, scan_id=None):
    """把设置页的答案写进 profile 字典（不存盘）。返回 (已保存说明列表, 错误列表)。非法项不写，其余照写。
    note 不存，由调用方（plan.py profile --apply）原样交给 Agent 翻译成待确认规则。
    成功应用后写 setup = {version, at, answers} 与 onboarded_at（0.5 兼容）。
    每一题真正改动了设置，就写一条记忆日志（kind setting，ref {field, old}）；scan_id 由调用方传入，拿不到靠时间认。"""
    done, errs = [], []
    if not isinstance(answers, dict):
        return [], ["答案必须是对象"]
    applied = {}
    for qid, val in answers.items():
        if qid == "note":
            continue
        if qid not in ANSWER_IDS:
            errs.append(f"不认识的设置项：{qid}")
            continue
        if qid == "conversation_days" and "conversations" in answers:
            continue                                # 天数随 conversations 一起处理
        before = {f: copy.deepcopy(profile.get(f)) for f in LOGGED_FIELDS + ("protect",)}
        n_done = len(done)
        try:
            norm = _apply_one(profile, qid, val, answers, done, errs, scan_id)
        except ValueError as exc:
            errs.append(str(exc))
            continue
        applied[qid] = norm
        _log_answer(profile, before, done[n_done:], scan_id)
    if "conversations" in applied and isinstance(answers.get("conversation_days"), int) \
            and answers["conversation_days"] in CONV_DAYS and not isinstance(answers["conversation_days"], bool):
        applied["conversation_days"] = answers["conversation_days"]
    if applied or not errs:
        old = (profile.get("setup") or {}).get("answers") if isinstance(profile.get("setup"), dict) else None
        merged = dict(old) if isinstance(old, dict) else {}
        merged.update(applied)
        profile["setup"] = {"version": SETUP_VERSION, "at": prefs.now_iso(), "answers": merged}
        profile["onboarded_at"] = profile.get("onboarded_at") or prefs.now_iso()
    return done, errs


def apply_export(text, scan_id=None):
    """解析并保存一份初始设置导出（含“台账:”行）。返回 (已保存说明, 错误, note 原文或 None)。
    先写答案再写台账：台账里用户逐项标的状态比“软件还用不用”的同步更具体，以台账为准。"""
    data, errs = parse_setup(text)
    if data is None:
        return [], errs, None
    if data["version"] is not None and data["version"] > SETUP_VERSION:
        return [], [f"设置版本 {data['version']} 比这个 Skill 认识的 {SETUP_VERSION} 新，先更新 Skill"], None
    p = prefs.load()
    before = json.dumps(p, sort_keys=True, ensure_ascii=False)
    done, aerrs = apply_answers(p, data["answers"], scan_id)
    errs = list(aerrs)
    ledger_ok = False
    if data["ledger"] is not None:
        d, e, ledger_ok = prefs.apply_ledger(p, data["ledger"], scan_id)
        done += d
        errs += e
        if not data["ledger"]:
            done.append("项目台账：全部按默认状态，已记下第一次设置完成")
    if not done and not errs and not data["answers"] and data["ledger"] is None:
        # 用户说“跳过设置”：答案为空也算完成第一次设置（全部按默认），以后不再弹设置页
        done.append("全部按默认，已记下第一次设置完成（想改随时说“改设置”）")
    if done or ledger_ok:
        p["onboarded_at"] = p.get("onboarded_at") or prefs.now_iso()
        if "setup" not in p:
            p["setup"] = {"version": SETUP_VERSION, "at": prefs.now_iso(), "answers": {}}
    if json.dumps(p, sort_keys=True, ensure_ascii=False) != before:      # 有变化才存盘（取消成品目录这类“清空”也算变化）
        prefs.save(p)
    return done, errs, data["note"]
