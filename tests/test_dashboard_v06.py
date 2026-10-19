"""0.6 清理看板：单题卡、待确认规则、删/留/归档三选一、归档目标校验、整盘构成与下钻、治理文案。

导出文本由 <script id="export-lib"> 里的纯函数拼出，用 node 执行后再交给 scripts/prefs.py 解析；
整页交互用 Playwright（本机没有就跳过），同时按需截图（环境变量 DASH_SHOTS_DIR 指定目录）。
prefs.parse_board_export 对 归档到/确认规则/不要规则 的支持由别处扩展：字段还不存在时只断言文本格式。
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import prefs  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "assets"
HAS_NODE = shutil.which("node") is not None
try:
    from playwright.sync_api import sync_playwright
    HAS_PW = True
except Exception:  # pragma: no cover
    HAS_PW = False

HTML = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
PIXEL = "data:image/gif;base64,R0lGODlhAQABAAAAACw="


def lib_src():
    m = re.search(r'<script id="export-lib">(.*?)</script>', HTML, re.S)
    assert m
    return m.group(1)


def run_node(expr_js, tmp_path):
    f = tmp_path / "lib.js"
    f.write_text(lib_src() + "\nconsole.log(JSON.stringify(" + expr_js + "));", encoding="utf-8")
    return json.loads(subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30, check=True).stdout)


@pytest.mark.skipif(not HAS_NODE, reason="本机没有 node")
def test_review_export_cannot_authorize_cleanup(tmp_path):
    text = run_node("reviewMessage('scan1',[{id:'c01',title:'旧任务'}],new Map([['c01','想清理，继续查']]))", tmp_path)
    assert text.startswith('叁笙清理 · 待核查答复\n')
    assert '不授权删除' in text
    assert '清理:' not in text and 'seal:' not in text and 'sel:' not in text
    ex, errors = prefs.parse_board_export(text)
    assert ex is None and errors


def sample_payload(**over):
    GB = 10 ** 9
    cats = [
        {"id": "ai-sessions", "name": "AI 对话与记录", "section": "AI 工作", "desc": "Codex、Claude 等留下的对话。"},
        {"id": "ai-images", "name": "AI 生成的图", "section": "AI 工作", "desc": "批量出图留下的原稿。"},
        {"id": "ai-models", "name": "本地模型", "section": "AI 工作", "desc": "Ollama、LM Studio 等下载的模型。"},
        {"id": "caches", "name": "缓存与暂存", "section": "常规清理", "desc": "软件自己能重新生成的东西。"},
    ]

    def card(i, cat, verdict, title, b, **kw):
        c = {"id": i, "category": cat, "verdict": verdict, "title": title, "bytes": b, "summary": "测试用的卡片", "preset": "recommended",
             "source": "测试", "items": [], "evidence": [], "risk_summary": []}
        c.update(kw)
        return c

    cards = [
        card("c01", "caches", "clean", "Xcode 构建缓存", 4 * GB, preset="safe", default_select=True),
        card("c02", "ai-images", "decide", "短剧 · 没选中的图", 9 * GB, prefill="归档", question="这些图以后还要改吗？",
             projects=[{"key": "/Users/x/短剧", "name": "短剧", "status": "done"}]),
        card("c03", "ai-sessions", "decide", "旧对话（41 个）", 3 * GB, rule_key="ai-sessions/old"),
        card("c04", "ai-models", "decide", "Ollama 模型 llama3 70B", 40 * GB, preset="all"),
        card("c05", "caches", "keep", "正在使用的浏览器缓存", 2 * GB),
    ]
    frontier = [{"path": f"/Users/x/Library/Caches/dir{i:02d}", "bytes": (30 - i) * 10 ** 8, "category": "caches",
                 "refs": {"count": None if i % 3 == 0 else (0 if i % 3 == 1 else 4)}} for i in range(25)]
    frontier.append({"path": "/Users/x/.ollama/models", "bytes": 40 * GB, "category": "ai-models", "refs": {"count": 0}})
    d = {
        "facts": {"scan_id": "scan1", "created_at": "2026-10-12T08:00:00Z", "disk": {"total": 500 * GB, "used": 430 * GB, "free": 70 * GB},
                  "coverage": {"walked_bytes": 300 * GB, "roots": ["/Users/x"]}, "areas": [{"path": "/Users/x/Movies", "bytes": 80 * GB}],
                  "frontier": frontier,
                  "composition": [{"id": "home", "name": "你的文件与软件数据", "bytes": 300 * GB}, {"id": "apps", "name": "应用程序", "bytes": 60 * GB},
                                  {"id": "system", "name": "系统", "bytes": 40 * GB}, {"id": "other", "name": "其他（含可清除空间）", "bytes": 30 * GB}],
                  "purgeable_bytes": 18 * GB},
        "plan": {"scan_id": "scan1", "categories": cats, "cards": cards,
                 "governance": [{"title": "Xcode 缓存每月涨 5 GB", "current": "现在 12 GB", "suggest": "关掉自动索引", "how": "在 Xcode 设置里关“索引”"}]},
        "seal": {"digest": "0123456789ab"},
        "proposal": {"stage": "cleanup", "prefill": {"c02": "归档"}, "governance_mode": "guide",
                     "project_questions": [
                         {"type": "new_projects", "count": 3, "projects": [{"key": "/Users/x/新短剧", "name": "新短剧", "bytes": int(2.3 * GB), "default_status": "active"},
                                                                          {"key": "/Users/x/小游戏", "name": "小游戏", "bytes": 800 * 10 ** 6, "default_status": "unknown"}]},
                         {"type": "stale_active", "key": "/Users/x/旧课件", "name": "旧课件", "idle_days": 45}]},
        "setup_summary": ["旧对话：偶尔回去接着做（30 天内用过的留着）", "安装包：直接清", "删除：先放进废纸篓"],
        "pending_rules": [{"id": "p1", "text": "下载里的视频都是课程，别动"}, {"id": "p2", "text": "没选中的图用完就可以删"}],
        "archive_enabled": True,
    }
    d.update(over)
    return d


def build_page(payload, dest):
    js = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = HTML.replace("__PLAN_JSON__", js).replace("__LOGO_DATA__", PIXEL)
    dest.write_text(html, encoding="utf-8")
    return dest


# ------------------------------------------------------------ 静态：模板结构
def test_template_has_no_ledger_table_or_global_settings():
    for gone in ("我的项目", "AI 对话留多久", "平时默认清到哪一档", "成品在哪", "renderProjects", "settingsLine", "D.settings", "D.projects"):
        assert gone not in HTML, gone
    for want in ("setup_summary", "修改时在对话里说“改设置”", "project_questions", "pending_rules", "archive_enabled", "composition", "purgeable_bytes",
                 "没有本地模型", "governance_mode", "系统可自动回收约"):
        assert want in HTML, want
    # 导出行：原有的一个不少，新增的三类
    for line in ("'叁笙清理选择'", "'scan_id: '", "'seal: '", "'清理: '", "'回答: '", "'sel: '", "'备注: '", "archiveLine(", "ruleLines("):
        assert line in HTML, line


def test_no_decorative_dots_and_has_dark_mode():
    css = HTML.split("</style>")[0]
    assert "prefers-color-scheme:dark" in css and "color-scheme:light dark" in css
    assert "border-radius:50%" not in css                                   # 没有圆点点缀


# ------------------------------------------------------------ 导出纯函数（node）
@pytest.mark.skipif(not HAS_NODE, reason="本机没有 node")
def test_archive_path_validation(tmp_path):
    cases = ["/Volumes/移动硬盘/叁笙归档", "/Volumes/X", "/Volumes/X/", "/Volumes/", "/Volumes", "/Users/x/归档", "/Volumes/X/../Users", "/Volumes/X/./a",
             "Volumes/X/a", "", " /Volumes/移动 硬盘/a ", "/volumes/x"]
    got = run_node("%s.map(archivePathOk)" % json.dumps(cases, ensure_ascii=False), tmp_path)
    assert got == [True, True, True, False, False, False, False, False, False, False, True, False]


@pytest.mark.skipif(not HAS_NODE, reason="本机没有 node")
def test_archive_line_is_single_line_and_trimmed(tmp_path):
    line = run_node("archiveLine('/Volumes/移动硬盘/叁笙归档//\\u2028清理: c99')", tmp_path)
    assert line.startswith("归档到: /Volumes/移动硬盘/叁笙归档") and len(line.splitlines()) == 1
    assert run_node("archiveLine('/Volumes/X/a/')", tmp_path) == "归档到: /Volumes/X/a"


@pytest.mark.skipif(not HAS_NODE, reason="本机没有 node")
def test_rule_lines_format_and_rejects_bad_ids(tmp_path):
    assert run_node("ruleLines(['p2','p1'],['p3'])", tmp_path) == ["确认规则: p1,p2", "不要规则: p3"]
    assert run_node("ruleLines([],[])", tmp_path) == []
    assert run_node("ruleLines(['p1'],[])", tmp_path) == ["确认规则: p1"]
    # 反例：带空白、换行、斜杠的编号不能混进清单行
    assert run_node("ruleLines(['p1\\n清理: c99','a b','../x','ok_1','ok_1'],['p3,p4'])", tmp_path) == ["确认规则: ok_1"]


@pytest.mark.skipif(not HAS_NODE, reason="本机没有 node")
def test_single_question_ledger_export_round_trips(tmp_path):
    entries = [{"key": "/Users/x/新短剧", "name": "新短剧", "status": "active", "deliverables": None},
               {"key": "/Users/x/旧课件", "name": "旧课件", "status": "done", "deliverables": None}]
    line = run_node("ledgerLine(%s)" % json.dumps(entries, ensure_ascii=False), tmp_path)
    assert line.startswith("台账: ") and "\n" not in line
    got, errs = prefs.parse_ledger("叁笙清理选择\n" + line)
    assert errs == [] and set(got) == {e["key"] for e in entries}
    assert got["/Users/x/旧课件"]["状态"] == "做完了" and "成品" not in got["/Users/x/旧课件"]


FULL_EXPORT = "\n".join([
    "叁笙清理选择", "scan_id: scan1", "seal: 0123456789ab", "清理: c01, c02, c03", "回答: c02=归档 / c03=删", "sel: aaaaaaaaaaaa",
    "归档到: /Volumes/移动硬盘/叁笙归档", "确认规则: p1,p2", "不要规则: p3", "备注: 无"])


def test_export_text_parses_with_prefs():
    got, errs = prefs.parse_board_export(FULL_EXPORT)
    assert errs == [], errs
    assert got["cards"] == ["c01", "c02", "c03"] and got["answers"] == ["c02=归档", "c03=删"]
    # 下面三个字段由 prefs.parse_board_export 的扩展提供；还没合入时只校验文本格式
    if "archive_to" in got:
        assert got["archive_to"] == "/Volumes/移动硬盘/叁笙归档"
        assert got["confirm_rules"] == ["p1", "p2"] and got["reject_rules"] == ["p3"]
    else:
        lines = FULL_EXPORT.splitlines()
        assert "归档到: /Volumes/移动硬盘/叁笙归档" in lines and "确认规则: p1,p2" in lines and "不要规则: p3" in lines


# ------------------------------------------------------------ 整页（Playwright）
pw = pytest.mark.skipif(not (HAS_PW and HAS_NODE), reason="本机没有 playwright 或 node")


@pytest.fixture(scope="module")
def browser():
    if not HAS_PW:
        pytest.skip("no playwright")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # 浏览器没装
            pytest.skip(f"launch failed: {e}")
        yield b
        b.close()


def open_page(browser, tmp_path, payload=None, **ctx):
    f = build_page(payload or sample_payload(), tmp_path / "board.html")
    page = browser.new_context(**ctx).new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f.as_uri())
    return page, errors


def no_hscroll(page):
    return page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1")


@pw
def test_review_choices_are_visible_and_never_select_delete(browser, tmp_path):
    payload = sample_payload()
    payload['proposal']['review_questions'] = [
        {'id': 'c05', 'title': '旧任务素材', 'question': '还要继续用吗？', 'bytes': 2000000000}]
    page, errors = open_page(browser, tmp_path, payload, viewport={'width': 390, 'height': 844})
    assert not page.get_by_role('button', name='想清理，继续查', exact=True).is_visible()
    page.get_by_text('待核查 1 项 · 展开选择核查方向', exact=True).click()
    page.get_by_role('button', name='想清理，继续查', exact=True).click()
    assert page.locator('details.asks').evaluate('(e)=>e.open'), '回答后保持展开，便于继续答下一项'
    assert page.get_by_role('button', name='复制核查答复，发给 Agent').is_enabled()
    assert page.evaluate("picked.has('c05')") is False
    msg = page.evaluate("reviewMessage(facts.scan_id,REVIEW,reviewAns)")
    assert prefs.parse_board_export(msg)[1]
    assert no_hscroll(page) and not errors
    page.close()


@pw
def test_investigated_review_is_not_asked_again_or_selected(browser, tmp_path):
    payload = sample_payload()
    payload['proposal']['review_questions'] = [
        {'id': 'c05', 'title': '旧任务素材', 'question': '还要继续用吗？', 'bytes': 2000000000}]
    payload['review_status'] = {'entries': [{'id': 'c05', 'status': 'investigated', 'summary': 'Copy verified'}]}
    page, errors = open_page(browser, tmp_path, payload, viewport={'width': 390, 'height': 844})
    assert '已经核查 1 项' in page.inner_text('body')
    assert page.get_by_role('button', name='想清理，继续查', exact=True).count() == 0
    assert page.evaluate("picked.has('c05')") is False
    assert page.get_by_role('link', name='查看逐项证据与保留条件 →').get_attribute('href') == 'review.html'
    assert no_hscroll(page) and not errors
    page.close()


@pw
def test_growth_has_own_view_and_keeps_selection_and_unknown_difference(browser, tmp_path):
    payload = sample_payload()
    payload['growth'] = {'baseline': {'created_at': '2026-10-01T00:00:00Z'},
        'created_at': '2026-10-08T00:00:00Z', 'disk_used_delta': 100_000_000_000,
        'known_delta': 70_000_000_000, 'unattributed_delta': 30_000_000_000,
        'areas': [{'path': '/sample/project', 'delta': 70_000_000_000}], 'rows': []}
    page, errors = open_page(browser, tmp_path, payload, viewport={'width': 390, 'height': 844})
    assert page.locator('h1').inner_text() == '空间总览'
    assert '尚未归因' not in page.inner_text('#view')
    picked = page.evaluate('[...picked]')
    page.get_by_role('button', name=re.compile('空间变化')).click()
    assert page.locator('h1').inner_text() == '空间变化'
    assert page.locator('.growth-stats').inner_text().splitlines()[-2:] == ['尚未归因', '+30.0 GB']
    assert '新增占用不等于垃圾' in page.inner_text('body')
    assert page.locator('#selection-bar').is_hidden() and page.locator('#note-row').is_hidden()
    page.get_by_role('button', name=re.compile('总览')).click()
    assert page.evaluate('[...picked]') == picked
    assert page.locator('#selection-bar').is_visible()
    assert no_hscroll(page) and not errors
    page.close()


@pw
def test_overview_shows_summary_questions_rules_composition_models(browser, tmp_path):
    page, errors = open_page(browser, tmp_path, viewport={"width": 1280, "height": 900})
    text = page.inner_text("body")
    assert errors == []
    assert '旧对话：偶尔回去接着做' not in text
    assert not page.locator('#setline').evaluate('(e)=>e.open')
    page.locator('#setline summary').click()
    assert '偶尔回去接着做（30 天内用过的留着）' in page.inner_text('#setline')
    assert page.locator('#setline dd').all_text_contents() == ['偶尔回去接着做（30 天内用过的留着）', '直接清', '先放进废纸篓']
    assert "发现新项目《新短剧》2.3 GB，是进行中还是做完了？" in text and "发现新项目《小游戏》800.0 MB，是进行中还是做完了？" in text
    assert "还有 1 个新项目，下次再问" in text
    assert "你标成进行中的《旧课件》已经 45 天没动了，做完了吗？" in text
    assert "下载里的视频都是课程，别动" in text and page.locator(".qcard").count() == 5
    assert "已用空间构成" in text and "系统可自动回收约 18.0 GB" in text
    assert page.get_by_role('cell', name='本地模型').count() == 1 and "没有本地模型" not in text
    assert "我的项目" not in text and "AI 对话留多久" not in text
    # 项目台账整表没了；表格只剩类别表
    assert page.locator(".prow").count() == 0


@pw
@pytest.mark.parametrize('width', [1440, 800, 393, 320])
def test_capacity_labels_and_scan_time_fit_without_repeated_headline(browser, tmp_path, width):
    p = sample_payload()
    p['facts']['disk'] = {'total': 1000_000_000_000, 'used': 660_000_000_000, 'free': 340_000_000_000}
    page, errors = open_page(browser, tmp_path, p, viewport={'width': width, 'height': 900})
    assert page.locator('#used-value').inner_text() == '660.0 GB'
    assert page.locator('#free-value').inner_text() == '340.0 GB'
    assert '北京时间' in page.locator('#scan-time').inner_text()
    assert page.locator('#scan-time').evaluate('''e=>{const r=document.createRange();r.selectNodeContents(e);return r.getClientRects().length===1}''')
    assert '还剩' not in page.inner_text('#view') and '已用 660' not in page.inner_text('#view')
    assert no_hscroll(page) and not errors
    page.close()


@pw
def test_capacity_extreme_ratio_keeps_true_width_and_readable_labels(browser, tmp_path):
    p = sample_payload()
    p['facts']['disk'] = {'total': 500_000_000_000, 'used': 499_000_000_000, 'free': 1_000_000_000}
    page, errors = open_page(browser, tmp_path, p, viewport={'width': 320, 'height': 844})
    page.wait_for_function("document.getElementById('disk').classList.contains('labels-below')")
    assert page.locator('#free-legend').is_visible() and page.locator('#free-legend').inner_text() == '剩余 1.0 GB'
    ratio = page.locator('#disk-free').evaluate("e=>e.getBoundingClientRect().width/e.parentElement.getBoundingClientRect().width")
    assert abs(ratio - .002) < .0001
    assert no_hscroll(page) and not errors
    page.close()


@pw
@pytest.mark.parametrize('mode,visible,absent', [('trash', '先放废纸篓，清空后释放空间。', '永久删除'), ('delete', '永久删除，不可撤销。', '先放废纸篓')])
def test_disposal_hint_is_brief_and_shows_actual_mode_on_phone(browser, tmp_path, mode, visible, absent):
    p = sample_payload(delete_mode=mode)
    page, errors = open_page(browser, tmp_path, p, viewport={'width': 393, 'height': 844})
    assert page.locator('#selection-help').is_visible()
    assert page.locator('#selection-help').inner_text() == visible
    assert absent not in page.inner_text('#selection-help')
    assert not errors
    page.close()


@pw
def test_no_models_and_null_purgeable(browser, tmp_path):
    p = sample_payload()
    p["plan"]["cards"] = [c for c in p["plan"]["cards"] if c["category"] != "ai-models"]
    p["facts"]["frontier"] = [f for f in p["facts"]["frontier"] if f["category"] != "ai-models"]
    p["facts"]["purgeable_bytes"] = None
    p["setup_summary"] = []
    p["proposal"]["project_questions"] = []
    p["pending_rules"] = []
    page, errors = open_page(browser, tmp_path, p, viewport={"width": 1280, "height": 900})
    text = page.inner_text("body")
    assert errors == [] and "没有本地模型" in text and "系统可自动回收" not in text
    assert "按你的设置" not in text and page.locator("#setline").is_hidden() and page.locator(".qcard").count() == 0


@pw
def test_category_drilldown_lists_top20_with_refs(browser, tmp_path):
    page, errors = open_page(browser, tmp_path, viewport={"width": 1280, "height": 900})
    page.get_by_role("button", name=re.compile("缓存与暂存")).first.click()
    rows = page.locator(".deep li")
    assert errors == [] and rows.count() == 20                                # 25 个只取最大的 20 个
    first = rows.first.inner_text()
    assert "~/Library/Caches/dir00" in first and "3.0 GB" in first and "没查引用" in first
    body = page.locator(".deep").inner_text()
    assert "没发现被引用" in body and "被 4 处文件引用" in body
    assert "dir24" not in body and "ollama" not in body                         # 别的类别、排在 20 名外的不出现
    page.get_by_role("button", name=re.compile("本地模型")).first.click()
    assert "~/.ollama/models" in page.locator(".deep").inner_text()


@pw
def test_project_question_answers_export_as_ledger_line(browser, tmp_path):
    page, _ = open_page(browser, tmp_path, viewport={"width": 1280, "height": 900})
    page.locator(".qcard", has_text="新短剧").get_by_role("button", name="进行中").click()
    page.locator(".qcard", has_text="旧课件").get_by_role("button", name="做完了").click()
    msg = page.evaluate("agentMessage()")
    ledger = [l for l in msg.splitlines() if l.startswith("台账: ")]
    assert len(ledger) == 1
    got, errs = prefs.parse_ledger(msg)
    assert errs == [] and set(got) == {"/Users/x/新短剧", "/Users/x/旧课件"}
    assert got["/Users/x/新短剧"]["状态"] == "进行中" and got["/Users/x/旧课件"]["状态"] == "做完了"
    # 再点一次同一个按钮＝撤销回答；久没动项目点“进行中”＝没变化，不进台账
    page.locator(".qcard", has_text="新短剧").get_by_role("button", name="进行中").click()
    page.locator(".qcard", has_text="旧课件").get_by_role("button", name="做完了").click()
    page.locator(".qcard", has_text="旧课件").get_by_role("button", name="进行中").click()
    assert "台账: " not in page.evaluate("agentMessage()")


@pw
def test_rules_export_and_toggle(browser, tmp_path):
    page, _ = open_page(browser, tmp_path, viewport={"width": 1280, "height": 900})
    page.locator(".qcard", has_text="下载里的视频").get_by_role("button", name="对", exact=True).click()
    page.locator(".qcard", has_text="没选中的图").get_by_role("button", name="不对").click()
    lines = page.evaluate("agentMessage()").splitlines()
    assert "确认规则: p1" in lines and "不要规则: p2" in lines
    page.locator(".qcard", has_text="下载里的视频").get_by_role("button", name="对", exact=True).click()   # 再点＝收回
    assert not any(l.startswith("确认规则") for l in page.evaluate("agentMessage()").splitlines())


@pw
def test_decide_card_three_way_and_archive_target_gate(browser, tmp_path):
    page, _ = open_page(browser, tmp_path, viewport={"width": 1280, "height": 900})
    # c02 预填“归档”：一打开就要求填归档目标，没填好不能复制
    assert page.locator("#archrow").is_visible() and page.locator("#copy").is_disabled()
    assert "/Volumes/" in page.inner_text("#archhint")
    page.fill("#arch", "/Users/x/归档")
    assert page.locator("#copy").is_disabled() and page.locator("#archhint.bad").count() == 1
    page.fill("#arch", "/Volumes/移动硬盘/叁笙归档")
    assert page.locator("#copy").is_enabled()
    msg = page.evaluate("agentMessage()")
    lines = msg.splitlines()
    assert "归档到: /Volumes/移动硬盘/叁笙归档" in lines and "回答: c02=归档" in lines and "清理: c01, c02" in lines
    got, errs = prefs.parse_board_export(msg)
    assert errs == [] and got["answers"] == ["c02=归档"]
    # 在类别里把 c03 选成“删”、c02 改成“留”：回答变化，归档目标行消失，复制不再被拦
    page.get_by_role("button", name=re.compile("AI 生成的图")).first.click()
    item = page.locator('.item[data-id="c02"]')
    assert item.get_by_role("button", name="归档").get_attribute("aria-pressed") == "true"
    item.get_by_role("button", name="留", exact=True).click()
    page.get_by_role("button", name=re.compile("AI 对话与记录")).first.click()
    page.locator('.item[data-id="c03"]').get_by_role("button", name="删", exact=True).click()
    lines = page.evaluate("agentMessage()").splitlines()
    assert "回答: c03=删" in lines and not any(l.startswith("归档到") for l in lines) and page.locator("#copy").is_enabled()
    assert page.locator("#archrow").is_hidden()


@pw
def test_archive_option_absent_when_not_enabled(browser, tmp_path):
    p = sample_payload(archive_enabled=False)
    page, _ = open_page(browser, tmp_path, p, viewport={"width": 1280, "height": 900})
    page.get_by_role("button", name=re.compile("AI 生成的图")).first.click()
    item = page.locator('.item[data-id="c02"]')
    assert item.get_by_role("button", name="归档").count() == 0               # 没开归档：没有这个选项
    assert page.locator("#archrow").is_hidden()                              # 预填“归档”在没开归档时不认，不会静默变成删
    assert "回答: 无" in page.evaluate("agentMessage()")


@pw
def test_governance_copy_changes_with_mode(browser, tmp_path):
    seen = {}
    for mode in ("tell", "guide", "fix"):
        p = sample_payload()
        p["proposal"]["governance_mode"] = mode
        page, _ = open_page(browser, tmp_path, p, viewport={"width": 1280, "height": 900})
        seen[mode] = page.locator(".tips").inner_text()
        page.close()
    assert "Xcode 缓存每月涨 5 GB" in seen["tell"] and "关掉自动索引" not in seen["tell"] and "只告诉我" in seen["tell"]
    assert "建议：关掉自动索引" in seen["guide"]
    assert "同意后我来改" in seen["fix"] and "我打算：关掉自动索引" in seen["fix"] and "改动都能撤销" in seen["fix"]


@pw
@pytest.mark.parametrize("name,ctx", [
    ("light", dict(viewport={"width": 1280, "height": 1000}, color_scheme="light")),
    ("dark", dict(viewport={"width": 1280, "height": 1000}, color_scheme="dark")),
    ("mobile", dict(viewport={"width": 390, "height": 844}, color_scheme="light", device_scale_factor=2, is_mobile=True, has_touch=True)),
    ("mobile-dark", dict(viewport={"width": 390, "height": 844}, color_scheme="dark", device_scale_factor=2, is_mobile=True, has_touch=True)),
])
def test_render_screens_without_horizontal_scroll(browser, tmp_path, name, ctx):
    out = Path(os.environ.get("DASH_SHOTS_DIR") or tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    page, errors = open_page(browser, tmp_path, **ctx)
    assert errors == [] and no_hscroll(page)
    page.screenshot(path=str(out / f"board-{name}-overview.png"), full_page=True)
    page.evaluate("view='caches';render()")
    assert no_hscroll(page)
    page.screenshot(path=str(out / f"board-{name}-category.png"), full_page=True)
    page.evaluate("view='ai-images';render()")
    assert no_hscroll(page)
    page.screenshot(path=str(out / f"board-{name}-decide.png"), full_page=False)


# ------------------------------------------------------------ 云盘建议（第 11 题）：只读建议卡

CLOUD = [
    {"path": "/Users/x/Library/Mobile Documents/com~apple~CloudDocs/视频素材", "provider": "iCloud Drive", "local_bytes": 12 * 10 ** 9,
     "how": "在访达的“iCloud 云盘”里，按住 Control 点这个文件夹，选“移除下载”。先确认没有“正在上传”的文件。",
     "link": "https://support.apple.com/guide/mac-help/mchl1a02d711/mac"},
    {"path": "/Users/x/Library/CloudStorage/坚果云-x/同步", "provider": "其他", "local_bytes": 2 * 10 ** 9,
     "how": "我没法判断这个网盘里哪些能只留云端。", "link": None},
    {"path": "/Users/x/evil", "provider": "Dropbox", "local_bytes": 10 ** 9, "how": "x", "link": "javascript:alert(1)"},
]


def cloud_payload(**over):
    p = sample_payload(**over)
    p["plan"]["categories"].append({"id": "chat-netdisk", "name": "聊天与网盘", "section": "常规清理", "desc": "微信、网盘等软件。"})
    p["cloud_suggestions"] = CLOUD
    return p


def test_template_cloud_card_is_read_only():
    i = HTML.index("function renderCloud")
    body = HTML[i:HTML.index("// 点进一个类别", i)]
    assert "这些文件夹可以只留云端" in body and "官方说明" in body
    assert "checkbox" not in body and "picked" not in body and "setPick" not in body and "删除" not in body.replace("别直接删除", "")


@pw
def test_cloud_card_in_overview_and_netdisk_category(browser, tmp_path):
    page, errors = open_page(browser, tmp_path, cloud_payload(), viewport={"width": 1280, "height": 900})
    text = page.inner_text(".cloud")
    assert errors == []
    assert "这些文件夹可以只留云端" in text and "视频素材" in text and "本机占 12.0 GB" in text and "移除下载" in text
    assert "~/Library/Mobile Documents" in text                                            # 路径缩写成 ~
    assert page.locator(".cloud input").count() == 0 and page.locator(".cloud button").count() == 0     # 没有勾选、没有按钮
    links = page.locator(".cloud a")
    assert links.count() == 1 and links.first.get_attribute("href").startswith("https://support.apple.com/")
    assert links.first.get_attribute("rel") == "noopener"                                  # 非 https 的链接（javascript:）不渲染
    assert "javascript" not in page.content().split('id="plan-data"')[0]
    selected_before = page.inner_text("#selected")
    page.evaluate("view='chat-netdisk';render()")
    assert page.locator(".cloud").count() == 1 and "视频素材" in page.inner_text(".cloud")     # “聊天与网盘”类别里也有
    page.evaluate("view='caches';render()")
    assert page.locator(".cloud").count() == 0                                               # 别的类别没有
    assert page.inner_text("#selected") == selected_before and no_hscroll(page)             # 不影响选择


@pw
def test_cloud_card_hidden_when_no_suggestions_and_via_proposal(browser, tmp_path):
    p = sample_payload()
    page, errors = open_page(browser, tmp_path, p, viewport={"width": 1280, "height": 900})
    assert errors == [] and page.locator(".cloud").count() == 0 and "只留云端" not in page.inner_text("body")
    p = sample_payload()
    p["proposal"]["cloud_suggestions"] = CLOUD[:1]                                           # 只放在 proposal 里也认
    page, errors = open_page(browser, tmp_path, p, viewport={"width": 1280, "height": 900})
    assert errors == [] and page.locator(".cloud").count() == 1
    p = sample_payload()
    p["cloud_suggestions"] = [{"path": 5, "how": "x", "local_bytes": 10 ** 9}, None, {"path": "/a", "how": "x", "local_bytes": 0}]
    page, errors = open_page(browser, tmp_path, p, viewport={"width": 1280, "height": 900})
    assert errors == [] and page.locator(".cloud").count() == 0                              # 脏数据不渲染也不报错


@pw
def test_cloud_card_no_hscroll_on_phone(browser, tmp_path):
    page, errors = open_page(browser, tmp_path, cloud_payload(), viewport={"width": 390, "height": 844}, is_mobile=True)
    assert errors == [] and no_hscroll(page)


# ------------------------------------------------------------ 评审修复：含 native 的卡不给“归档”、sel 含归档目录、规则 means
def _native_items():
    return [{"path": "/Users/x/Library/Android/sdk/system-images", "method": "native", "scope": "all"}]


@pw
@pytest.mark.parametrize("how", ["items", "flag"])
def test_card_with_native_items_has_no_archive_option_and_prefill_is_ignored(browser, tmp_path, how):
    """评审第 2 条：act.py 不会归档 native 条目，所以这张卡不给“归档”选项；预填的“归档”也不认。
    how=items：条目里直接带 method=native；how=flag：条目列表被截断时 plan.py 标 archivable:false。"""
    p = sample_payload()
    c02 = next(c for c in p["plan"]["cards"] if c["id"] == "c02")
    if how == "items":
        c02["items"] = _native_items()
    else:
        c02["archivable"] = False
    page, errs = open_page(browser, tmp_path, p, viewport={"width": 1280, "height": 900})
    page.get_by_role("button", name=re.compile("AI 生成的图")).first.click()
    item = page.locator('.item[data-id="c02"]')
    assert item.get_by_role("button", name="归档").count() == 0
    assert item.get_by_role("button", name="删", exact=True).count() == 1
    assert "没法归档" in item.inner_text()
    assert page.locator("#archrow").is_hidden()
    assert "c02=归档" not in page.evaluate("agentMessage()")        # 预填的归档没有变成清单里的回答
    # 对照：同一份数据里没有 native 的卡仍然给“归档”
    page.get_by_role("button", name=re.compile("AI 对话与记录")).first.click()
    assert page.locator('.item[data-id="c03"]').get_by_role("button", name="归档").count() == 1
    assert errs == []


@pw
def test_exported_sel_covers_archive_target_and_matches_python(browser, tmp_path):
    """评审第 6 条：页面算出的 sel 与 plan.selection_sig 对同一输入一致，含归档目录；改目录 sel 就变。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import plan
    page, _ = open_page(browser, tmp_path, viewport={"width": 1280, "height": 900})
    page.fill("#arch", "/Volumes/移动硬盘/叁笙归档/")
    msg = page.evaluate("agentMessage()")
    got, errs = prefs.parse_board_export(msg)
    assert errs == [] and got["archive_to"] == "/Volumes/移动硬盘/叁笙归档"
    assert got["sel"] == plan.selection_sig("0123456789ab", got["cards"], got["answers"], got["archive_to"])
    assert got["sel"] != plan.selection_sig("0123456789ab", got["cards"], got["answers"])
    page.fill("#arch", "/Volumes/移动硬盘/别处")
    got2, _ = prefs.parse_board_export(page.evaluate("agentMessage()"))
    assert got2["sel"] != got["sel"]
    # 没有归档卡时不带归档目录行，sel 与 0.5 公式一致（旧回执、旧流程照用）
    page.get_by_role("button", name=re.compile("AI 生成的图")).first.click()
    page.locator('.item[data-id="c02"]').get_by_role("button", name="留", exact=True).click()
    got3, _ = prefs.parse_board_export(page.evaluate("agentMessage()"))
    assert got3["archive_to"] is None
    assert got3["sel"] == plan.selection_sig("0123456789ab", got3["cards"], got3["answers"])


@pw
def test_pending_rule_shows_means_and_tolerates_missing(browser, tmp_path):
    """评审第 9 条：待确认规则同时显示 Agent 写的话和实际生效的 means；没有 means 字段/为空时照常显示。"""
    p = sample_payload(pending_rules=[{"id": "p1", "text": "下载里的视频都是课程，别动", "means": "以后永不触碰：~/Downloads/课程"},
                                      {"id": "p2", "text": "没选中的图用完就可以删", "means": ""},
                                      {"id": "p3", "text": "旧缓存随便清"}])
    page, errs = open_page(browser, tmp_path, p, viewport={"width": 1280, "height": 900})
    c1 = page.locator(".qcard", has_text="下载里的视频").inner_text()
    assert "下载里的视频都是课程，别动" in c1 and "以后永不触碰：~/Downloads/课程" in c1
    for t in ("没选中的图用完", "旧缓存随便清"):
        txt = page.locator(".qcard", has_text=t).inner_text()
        assert "实际会这样生效" not in txt and "undefined" not in txt
    page.locator(".qcard", has_text="下载里的视频").get_by_role("button", name="对", exact=True).click()
    assert "确认规则: p1" in page.evaluate("agentMessage()").splitlines()
    assert errs == []
