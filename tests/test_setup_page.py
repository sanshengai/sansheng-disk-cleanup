"""0.6 D1 初始设置页（assets/setup.html）。

页面把导出文本的纯函数放在 <script id="export-lib"> 里；这里用 node 执行那段（同 tests/test_boards.py 的做法），
再核对“答案 / 台账 / 补充”三行，保证页面写出的和 Agent 读到的是同一种格式。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parent.parent / "assets"
HAS_NODE = shutil.which("node") is not None


def lib_src(page="setup.html"):
    html = (ASSETS / page).read_text(encoding="utf-8")
    m = re.search(r'<script id="export-lib">(.*?)</script>', html, re.S)
    assert m, f"{page} 里找不到 export-lib"
    return m.group(1)


def run_node(expr_js, tmp_path):
    """在 node 里执行页面的 export-lib，再求值 expr_js（输出 JSON）。"""
    f = tmp_path / "setup-lib.js"
    f.write_text(lib_src() + "\nconsole.log(JSON.stringify(" + expr_js + "));", encoding="utf-8")
    out = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def js(obj):
    return json.dumps(obj, ensure_ascii=False)


def answer_of(text):
    """取出“答案:”那一行的 JSON（导出的每一行都是单行）。"""
    for line in text.split("\n"):
        if line.startswith("答案: "):
            return json.loads(line[len("答案: "):])
    raise AssertionError("导出里没有“答案:”行")


def ledger_of(text):
    for line in text.split("\n"):
        if line.startswith("台账: "):
            return json.loads(line[len("台账: "):])
    return None


def extra_of(text):
    for line in text.split("\n"):
        if line.startswith("补充: "):
            return json.loads(line[len("补充: "):])
    return None


# ------------------------------------------------------------ 约定第 5 节的样例 payload（17 题）
def sample_payload(mode="first"):
    edit = mode == "edit"
    def cur(v):
        return v if edit else None

    def single(qid, no, gid, title, options, recommended, **kw):
        d = {"id": qid, "no": no, "group": gid, "type": "single", "title": title,
             "context": "本机发现：示例", "recommended": recommended, "current": cur(recommended),
             "options": [{"value": v, "label": l, "desc": desc} for v, l, desc in options],
             "why": "决定这一类的清理力度", "risk": "答偏了会多清或少清"}
        d.update(kw)
        return d

    read_scope = {"id": "read_scope", "no": 1, "group": "read", "type": "multi",
                  "title": "清理时我可以看到哪些内容", "context": "找到聊天记录、邮件、照片等", "recommended": ["basic", "extended"],
                  "current": cur(["basic", "extended"]), "why": "读取越深，能给的判断越准", "risk": "授权太宽会看到私人内容",
                  "options": [{"value": "basic", "label": "文件名和时间", "desc": "只看名字、大小、修改时间"},
                              {"value": "extended", "label": "聊天收到的文件、照片、截图", "desc": "看这些文件的目录"},
                              {"value": "deep", "label": "聊天正文和邮件正文", "desc": "读正文内容"},
                              {"value": "never", "label": "永不读", "desc": "钥匙串、密码等一直不看", "locked": True}]}
    tools = {"id": "tools", "no": 2, "group": "machine", "type": "tools", "title": "这些大工具你还在用吗",
             "context": "找到几个超过 1 GB 的开发工具缓存", "recommended": None, "current": None,
             "why": "不用了就能清它的缓存", "risk": "还在用却清了要重新下载"}
    projects = {"id": "projects", "no": 3, "group": "machine", "type": "projects", "title": "这些项目做得怎么样了",
                "context": "从扫描里认出几个项目", "recommended": None, "current": None,
                "why": "做完了的项目才好动", "risk": "把在做的项目清了会丢进度"}
    deliverables = {"id": "deliverables", "no": 4, "group": "machine", "type": "paths", "title": "成品都放在哪",
                    "context": "看到几个像成品的目录", "recommended": [], "current": cur([]),
                    "why": "有副本才放心清", "risk": "认错成品会误清"}
    protect = {"id": "protect", "no": 5, "group": "machine", "type": "paths", "title": "有哪些地方别碰",
               "context": "你的家目录下一级目录", "recommended": [], "current": cur([]),
               "why": "别碰的目录一律不看", "risk": "漏标会去动不该动的"}

    conversations = single("conversations", 6, "ai", "用完的 AI 对话，对你来说是",
                           [("done", "做完就不再看", "超过 7 天没用的列出来清"),
                            ("sometimes", "偶尔回去接着做", "30 天内用过的留着"),
                            ("library", "当资料库，都留着", "都不列为可清")],
                           "sometimes", days=[7, 15, 30, 60, 90], current_days=None)
    rejects = single("rejects", 7, "ai", "AI 没挑中的图怎么处理",
                     [("drop", "直接清", "没选中的不用留"), ("until_done", "项目做完再清", "项目没完先留着"),
                      ("keep", "都留着", "图都别清")], "until_done")
    worktrees = single("worktrees", 8, "ai", "代码工作树怎么清",
                       [("whole", "整棵一起清", "连代码一起"), ("build_only", "只清构建产物", "代码先留着"),
                        ("ask", "每次问我", "不预选")], "build_only")
    installers = single("installers", 9, "files", "下载的安装包怎么处理",
                        [("auto", "过期就清", "装完的删掉"), ("ask", "每次问我", "先留着")], "auto")
    big_files = single("big_files", 10, "files", "大录屏和大视频留多久",
                       [(90, "90 天", "三个月前的再列出来"), (30, "30 天", "一个月前的列出来"), (0, "别问", "这类都不列")], 90)
    cloud = single("cloud", 11, "files", "云盘要不要管",
                   [("ok", "可以联网看", "去云盘核对"), ("offline", "只在本机看", "不联网"),
                    ("unsure", "先别动", "我不确定")], "unsure")
    wait = single("wait", 12, "way", "清理前想问多少",
                  [("minimal", "尽量少问", "只问危险的"), ("some", "问几次", "关键的都问"),
                   ("space", "多问点", "尽量腾空间")], "some")
    done_projects = single("done_projects", 13, "way", "做完的项目怎么处理",
                           [("intermediate", "清中间稿", "留成品"), ("archive", "归档", "留成品和过程"),
                            ("keep", "先都留着", "不动")], "intermediate")
    governance = single("governance", 14, "way", "治本的事怎么办",
                        [("tell", "只告诉我", "我自己改"), ("guide", "给我改法", "我照着改"),
                         ("fix", "同意就帮我改", "可撤销")], "fix")
    delete_mode = single("delete_mode", 15, "way", "清掉的东西放哪",
                         [("trash", "放废纸篓", "还能捞回来"), ("delete", "直接删", "马上腾空间")], "trash")
    confirm = single("confirm", 16, "way", "决定的方式",
                     [("smart", "简单的直接清", "危险的才问我"), ("board", "每次都发看板", "我逐个确认")], "smart")
    note = {"id": "note", "no": 17, "group": "note", "type": "text", "title": "还有别的要交代吗",
            "context": "选填", "recommended": "", "current": cur(""),
            "why": "补充规则会交给 Agent 确认", "risk": "写清楚点更省事"}

    return {
        "version": 1, "scan_id": "scan1", "mode": mode,
        "privacy_note": "读的内容会发给你所用的 AI 服务商（如 Claude、OpenAI），不只在本机看",
        "time_machine_last": "2026-10-05T00:00:00Z",
        "tools": [{"key": "android", "name": "Android 模拟器", "bytes": 12_000_000_000,
                   "last_used": "2026-10-01T00:00:00Z", "recommended": "often", "current": "often" if edit else None},
                  {"key": "codex", "name": "Codex", "bytes": 2_000_000_000,
                   "last_used": "2025-01-01T00:00:00Z", "recommended": None, "current": None}],
        "projects": [{"key": "/Users/x/短剧", "name": "短剧", "bytes": 12_000_000_000,
                      "last_active": "2026-10-10T08:00:00Z", "sessions": 3,
                      "deliverables_guess": ["~/短剧/成品"], "default_status": "active",
                      "status": "active" if edit else None, "deliverables": [], "in_ledger": edit}],
        "deliverable_candidates": [{"path": "~/Movies/成片", "why": "名字含“成片”，有 34 个视频", "bytes": 5_000_000_000, "files": 34}],
        "top_dirs": [{"path": "~/Documents", "bytes": 8_000_000_000}, {"path": "~/Movies", "bytes": 9_000_000_000}],
        "groups": [
            {"id": "read", "title": "我会看什么", "desc": "只读和清理有关的", "questions": [read_scope]},
            {"id": "machine", "title": "你电脑上有什么", "desc": "本机发现", "questions": [tools, projects, deliverables, protect]},
            {"id": "ai", "title": "AI 留下什么", "desc": "", "questions": [conversations, rejects, worktrees]},
            {"id": "files", "title": "下载与备份", "desc": "", "questions": [installers, big_files, cloud]},
            {"id": "way", "title": "怎么帮你做", "desc": "", "questions": [wait, done_projects, governance, delete_mode, confirm]},
            {"id": "note", "title": "还有别的要求吗", "desc": "", "questions": [note]},
        ],
    }


def all_questions(payload):
    return [q for g in payload["groups"] for q in g["questions"]]


def test_sample_payload_has_17_questions():
    assert len(all_questions(sample_payload())) == 17


# ------------------------------------------------------------ 首次：全推荐
FIRST_EXPECT = {
    "read_scope": ["basic", "extended"],
    "tools": {"android": "often"},
    "deliverables": [],
    "protect": [],
    "conversations": "sometimes",
    "rejects": "until_done",
    "worktrees": "build_only",
    "installers": "auto",
    "big_files": 90,
    "cloud": "unsure",
    "wait": "some",
    "done_projects": "intermediate",
    "governance": "fix",
    "delete_mode": "trash",
    "confirm": "smart",
}


@pytest.mark.skipif(not HAS_NODE, reason="本机没有 node")
def test_first_mode_exports_all_questions_by_recommended(tmp_path):
    payload = sample_payload("first")
    state = {"values": {}, "note": "",
             "projects": [{"key": "/Users/x/短剧", "name": "短剧", "status": "active",
                           "deliverables": ["~/短剧/成品"], "orig": None}]}
    text = run_node("buildSetupExport(%s,%s)" % (js(state), js(payload)), tmp_path)
    lines = text.split("\n")
    assert lines[0] == "叁笙清理 · 初始设置"
    assert lines[1] == "设置版本: 1"
    assert answer_of(text) == FIRST_EXPECT
    led = ledger_of(text)
    assert led == {"项目": {"/Users/x/短剧": {"状态": "进行中", "成品": ["~/短剧/成品"], "名称": "短剧"}}}
    assert extra_of(text) is None and len(lines) == 4          # 没写补充就没有“补充:”行


@pytest.mark.skipif(not HAS_NODE, reason="本机没有 node")
def test_first_mode_without_projects_has_no_ledger_line(tmp_path):
    payload = sample_payload("first")
    state = {"values": {}, "projects": [], "note": ""}
    text = run_node("buildSetupExport(%s,%s)" % (js(state), js(payload)), tmp_path)
    assert ledger_of(text) is None and extra_of(text) is None
    assert text.split("\n")[2].startswith("答案: ")


# ------------------------------------------------------------ 编辑：只发改动
@pytest.mark.skipif(not HAS_NODE, reason="本机没有 node")
def test_edit_mode_exports_only_changed(tmp_path):
    payload = sample_payload("edit")
    state = {"values": {"read_scope": ["basic"], "conversations": "library", "tools": {"android": "unused"}},
             "note": "",
             "projects": [{"key": "/Users/a", "name": "改过的", "status": "done", "deliverables": [],
                           "orig": {"status": "active", "deliverables": []}},
                          {"key": "/Users/b", "name": "没动的", "status": "done", "deliverables": [],
                           "orig": {"status": "done", "deliverables": []}}]}
    text = run_node("buildSetupExport(%s,%s)" % (js(state), js(payload)), tmp_path)
    assert answer_of(text) == {"read_scope": ["basic"], "tools": {"android": "unused"}, "conversations": "library"}
    assert ledger_of(text) == {"项目": {"/Users/a": {"状态": "做完了", "成品": [], "名称": "改过的"}}}


@pytest.mark.skipif(not HAS_NODE, reason="本机没有 node")
def test_edit_mode_exports_nothing_when_untouched(tmp_path):
    payload = sample_payload("edit")
    values = {q["id"]: q["current"] for q in all_questions(payload)
              if q["type"] in ("single", "multi", "paths")}
    values["tools"] = {"android": "often"}
    state = {"values": values, "note": "", "projects": []}
    text = run_node("buildSetupExport(%s,%s)" % (js(state), js(payload)), tmp_path)
    assert answer_of(text) == {} and ledger_of(text) is None and extra_of(text) is None


# ------------------------------------------------------------ read_scope 永不读
@pytest.mark.skipif(not HAS_NODE, reason="本机没有 node")
def test_read_scope_never_excluded(tmp_path):
    payload = sample_payload("first")
    state = {"values": {"read_scope": ["extended", "never", "deep", "basic", "胡说"]},
             "projects": [], "note": ""}
    text = run_node("buildSetupExport(%s,%s)" % (js(state), js(payload)), tmp_path)
    assert answer_of(text)["read_scope"] == ["basic", "extended", "deep"]     # never 和非法值一律丢掉


# ------------------------------------------------------------ 工具没答就不导出
@pytest.mark.skipif(not HAS_NODE, reason="本机没有 node")
def test_tools_unanswered_not_exported(tmp_path):
    payload = sample_payload("first")
    state = {"values": {}, "projects": [], "note": ""}
    text = run_node("buildSetupExport(%s,%s)" % (js(state), js(payload)), tmp_path)
    assert answer_of(text)["tools"] == {"android": "often"}                   # 有推荐的按推荐
    assert "codex" not in answer_of(text)["tools"]                            # 没答也没推荐的不导出

    payload["tools"] = [{"key": "codex", "name": "Codex", "bytes": 1, "last_used": None,
                         "recommended": None, "current": None}]
    text2 = run_node("buildSetupExport(%s,%s)" % (js(state), js(payload)), tmp_path)
    assert "tools" not in answer_of(text2)


# ------------------------------------------------------------ safeLine 转义
@pytest.mark.skipif(not HAS_NODE, reason="本机没有 node")
def test_export_escapes_line_separators(tmp_path):
    payload = sample_payload("first")
    note = "第一行\u2028第二行\u0085第三行\n末尾"
    name = "名字\u2028带分隔符\u0085和\u2029"
    state = {"values": {}, "note": note,
             "projects": [{"key": "/Users/x/a", "name": name, "status": "done",
                           "deliverables": None, "orig": None}]}
    text = run_node("buildSetupExport(%s,%s)" % (js(state), js(payload)), tmp_path)
    for ch in ("\u2028", "\u0085", "\u2029"):
        assert ch not in text                                                 # 原文里不许留这些字符
    assert all(line for line in text.split("\n"))                             # 每一行都不为空，说明没被拆开
    assert extra_of(text) == note                                             # 补充行能原样解析回来
    assert ledger_of(text)["项目"]["/Users/x/a"]["名称"] == name
    assert "\n" not in [l for l in text.split("\n") if l.startswith("补充: ")][0]


# ------------------------------------------------------------ 页面结构（不发 node 也能跑）
def test_setup_page_structure():
    html = (ASSETS / "setup.html").read_text(encoding="utf-8")
    for s in ("__LOGO_DATA__", "__SETUP_JSON__", "认识你的电脑", "叁笙清理 · 初始设置", "设置版本: ",
              "全部按推荐", "复制，发给 Agent", "为什么问这个", "永不读", "prefers-color-scheme:dark"):
        assert s in html, s
    assert html.count('id="rec-top"') == 1 and html.count('id="rec-bottom"') == 1
    assert html.count('id="copy"') == 1


def test_setup_page_copies_fallback_and_headers():
    html = (ASSETS / "setup.html").read_text(encoding="utf-8")
    assert "navigator.clipboard.writeText" in html and 'id="fallback"' in html
    assert "新版本多问几个问题，之前的设置都保留" in html and "修改设置" in html
    assert "第一次需要多一点时间认识你的电脑，以后会快很多" in html


def test_setup_page_ui_regressions():
    """验收时修过的问题：label 的 class 被当成文字（页面里出现“opt”）、隐私提醒逐项重复、工具内部键外露、编辑模式文案。"""
    html = (ASSETS / "setup.html").read_text(encoding="utf-8")
    assert "E('label','opt" not in html                       # E(tag,text,cls)：class 必须放第三个参数
    assert "E('label',null,'opt'" in html
    assert "(q.type==='multi'?D.privacy_note:'')" not in html  # 隐私提醒只在读取范围题下写一次
    assert "E('div',t.key,'sub')" not in html                  # 不把 android / claude-code 这类内部键给用户看
    assert "lockmark" in html and "已锁定" in html            # “永不读”显示为已锁定
    assert "还没改动" in html and "恢复成现在的设置" in html    # 编辑模式文案
    assert html.count("class=\"rec\"") == 0 and ".rec{" in html and "'rec'" in html  # 无推荐标记的题写推荐说明
