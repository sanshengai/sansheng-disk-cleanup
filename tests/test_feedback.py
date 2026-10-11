"""0.5 P4：知识缺口反馈（隐私是硬性要求）、成果页首次 Star 提示、看板路径执行后自动出成果页。

隐私夹具：家目录用户名 alexsecret、项目目录 myclientproject、文件名 passport.pdf，三者不得出现在
gaps 的 JSON、预览全文、Issue 链接（解码后）里。全部夹具在 tmp_path 内，不碰真实家目录与真实 profile。
"""
import json
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from test_plan_act import Env, act, age, auto_sel, card, delete_item, mkfile, node, plan  # noqa: E402
from test_report_receipts import setup as receipts_setup  # noqa: E402
import feedback  # noqa: E402
import prefs  # noqa: E402
import protect  # noqa: E402
import report  # noqa: E402

SECRETS = ("alexsecret", "myclientproject", "passport.pdf")
GB = 10 ** 9


def fnode(path, gb, **kw):
    n = node(path, id=kw.pop("id", "n1"), bytes=int(gb * GB), frontier=True)
    n.update(kw)
    return n


@pytest.fixture
def fb(tmp_path, monkeypatch):
    """用户名为 alexsecret 的假家目录 + 隔离的 profile；返回 (家目录, run 目录)。"""
    home = (tmp_path / "Users" / "alexsecret").resolve()
    home.mkdir(parents=True)
    state = home / ".sansheng" / "disk-cleanup"
    monkeypatch.setattr(protect, "HOME", str(home))
    monkeypatch.setattr(protect, "STATE_DIR", str(state))
    monkeypatch.setattr(protect, "PROFILE", str(state / "profile.json"))
    monkeypatch.setattr(feedback, "macos_version", lambda: "26.1")
    run = tmp_path / "runs" / "scan1"
    run.mkdir(parents=True)
    # 任何网络访问都是失败：反馈功能只生成文字
    def no_net(*a, **k):
        raise AssertionError("反馈功能不得发起网络连接")
    monkeypatch.setattr(socket, "create_connection", no_net)
    monkeypatch.setattr(socket.socket, "connect", no_net)
    return home, run


def write_facts(run, nodes):
    (run / "facts.json").write_text(json.dumps(
        {"schema": "facts/1", "scan_id": "scan1", "created_at": "2026-10-11T00:00:00Z",
         "disk": {"total": 10, "free": 5, "used": 5}, "coverage": {"incomplete": False},
         "nodes": nodes, "skipped": []}), encoding="utf-8")


def privacy_nodes(home):
    return [
        fnode(home / "myclientproject" / "data", 5.2, id="n1"),
        fnode(home / ".cache" / "myclientproject" / "models", 3.0, id="n2"),
        fnode(home / "Documents" / "myclientproject" / "passport.pdf", 2.0, id="n3"),      # 未标 protected 的目录名撞上文件名
        fnode(home / "Library" / "Application Support" / "com.foo.app" / "myclientproject" / "cache", 1.8, id="n4"),
        fnode(home / "Library" / "Containers" / "com.foo.bar" / "Data", 1.5, id="n5"),
        fnode(home / ".alexsecret-tool" / "big", 1.2, id="n6"),
        fnode("/Volumes/alexsecret-backup/myclientproject", 4.0, id="n7"),
        fnode("/Users/alexsecret/Movies/passport.pdf", 1.1, id="n8"),
    ]


def decoded_url(url):
    q = parse_qs(urlparse(url).query)
    return q["title"][0] + "\n" + q["body"][0]


# ------------------------------------------------------------ 隐私（硬性）

def all_outputs(home, run):
    write_facts(run, privacy_nodes(home))
    res = feedback.detect(str(run))
    url, body, _ = feedback.build_issue(res)
    return json.dumps(res, ensure_ascii=False), res["preview"], url, decoded_url(url), body


def test_outputs_never_contain_username_project_or_filename(fb):
    home, run = fb
    outs = all_outputs(home, run)
    for text in outs:
        for secret in SECRETS:
            assert secret not in text, f"{secret!r} 泄漏进了输出"
    res = feedback.detect(str(run))
    pats = {g["pattern"] for g in res["gaps"]}
    assert "~/<目录>/<目录>" in pats                                                   # 家目录下的项目目录被换掉
    assert "~/.cache/<目录>/models" in pats                                            # 一级点目录和众所周知的 models 保留，项目名换掉
    assert "~/Documents/<目录>/<目录>" in pats
    assert "~/Library/Application Support/com.foo.app/<目录>/cache" in pats           # 反向域名形态的软件名保留
    assert "~/Library/Containers/com.foo.bar/<目录>" in pats
    assert "/Volumes/<卷>/<目录>" in pats                                              # 卷名换掉
    assert "~/Movies/<目录>" in pats                                                   # 其中 /Users/alexsecret 认作家目录


def test_privacy_assertions_have_teeth_when_masking_is_off(fb, monkeypatch):
    """变异：把“换成 <目录>”的判定整个关掉（任何段都保留），隐私断言必须红。"""
    home, run = fb
    monkeypatch.setattr(feedback, "_keep_segment", lambda *a, **k: True)
    monkeypatch.setattr(feedback, "_software_ok", lambda *a, **k: True)
    leaked = [s for text in all_outputs(home, run) for s in SECRETS if s in text]
    assert "myclientproject" in leaked and "passport.pdf" in leaked


def test_other_users_home_is_masked(fb):
    pat, _ = feedback.mask_path("/Users/someoneelse/Projects/secretthing/x", home=str(fb[0]))
    assert "someoneelse" not in pat and "secretthing" not in pat and pat.startswith("/Users/<用户>")


def test_username_inside_segments_is_never_kept(fb):
    home, _ = fb
    # 点目录名里带用户名、软件名里带用户名：都不保留
    assert feedback.mask_path(str(home / ".alexsecret-tool" / "x"), str(home))[0] == "~/<目录>/<目录>"
    p, sw = feedback.mask_path(str(home / "Library" / "Application Support" / "alexsecret" / "x"), str(home))
    assert "alexsecret" not in p and sw is None


# ------------------------------------------------------------ 发布前评审 中5：路径脱敏漏项

@pytest.mark.parametrize("path, want", [
    # .app 只在 /Applications、~/Applications 的直接下级保留
    ("{h}/dev/AcmeClientPortal.app/Contents", "~/<目录>/<目录>/Contents"),
    ("{h}/Documents/客户X/Secret.app", "~/Documents/<目录>/<目录>"),
    ("/Applications/Foo.app/Contents/Frameworks/Big", "/Applications/Foo.app/Contents/Frameworks/<目录>"),
    ("{h}/Applications/Foo.app/Contents", "~/Applications/Foo.app/Contents"),
    ("/Applications/Utilities/Secret.app", "/Applications/<目录>/<目录>"),
    ("/opt/Applications/Secret.app", "/opt/Applications/<目录>"),
    # 家目录一级点目录只认白名单
    ("{h}/.acme-secret-project", "~/<目录>"),
    ("{h}/.acme-secret-project/models", "~/<目录>/models"),
    ("{h}/.cache/x", "~/.cache/<目录>"),
    ("{h}/.vscode/extensions", "~/.vscode/<目录>"),
    ("{h}/.claude/projects", "~/.claude/<目录>"),
    ("{h}/.codex/sessions", "~/.codex/<目录>"),
    ("{h}/Documents/.acme-secret-project", "~/Documents/<目录>"),            # 非一级的点目录同样换掉
    # Application Support / Containers / Group Containers 的直接子目录：反向域名或已知软件名才保留
    ("{h}/Library/Application Support/张三的工作资料", "~/Library/Application Support/<目录>"),
    ("{h}/Library/Application Support/alex-backup", "~/Library/Application Support/<目录>"),
    ("{h}/Library/Application Support/Slack/客户X", "~/Library/Application Support/Slack/<目录>"),
    ("{h}/Library/Application Support/Code/User", "~/Library/Application Support/Code/<目录>"),
    ("{h}/Library/Containers/com.acme.internal-tool", "~/Library/Containers/com.acme.internal-tool"),
    ("{h}/Library/Containers/张三的朋友", "~/Library/Containers/<目录>"),
    ("{h}/Library/Containers/john.smith", "~/Library/Containers/<目录>"),            # 有点号但不像反向域名（首段不是域名后缀）
    ("{h}/Library/Group Containers/ABCDE12345.com.acme.secret", "~/Library/Group Containers/ABCDE12345.com.acme.secret"),
    ("{h}/Library/Group Containers/group.com.acme.x", "~/Library/Group Containers/group.com.acme.x"),
    ("{h}/Library/Group Containers/客户X共享", "~/Library/Group Containers/<目录>"),
    # 原有行为不变
    ("/users/alex/Projects/客户X", "/users/<目录>/<目录>/<目录>"),
    ("/Volumes/张三的硬盘/客户X", "/Volumes/<卷>/<目录>"),
])
def test_mask_path_review_cases(path, want):
    home = "/Users/alex"
    got, _ = feedback.mask_path(path.format(h=home), home=home)
    assert got == want
    for leaked in ("alex", "张三", "客户X", "acme-secret", "AcmeClient", "Secret"):
        assert leaked not in got, (leaked, got)


def test_mask_path_software_names_returned_only_when_kept():
    home = "/Users/alex"
    assert feedback.mask_path("/Applications/Foo.app/Contents", home=home)[1] == "Foo"
    assert feedback.mask_path(home + "/dev/Secret.app/Contents", home=home)[1] is None
    assert feedback.mask_path(home + "/Library/Application Support/张三的工作资料/x", home=home)[1] is None
    assert feedback.mask_path(home + "/Library/Containers/com.acme.tool/Data", home=home)[1] == "com.acme.tool"


def test_software_name_only_at_library_position(fb):
    home, _ = fb
    p, sw = feedback.mask_path(str(home / "Documents" / "Application Support" / "client-name"), str(home))
    assert "client-name" not in p and sw is None
    p, sw = feedback.mask_path("/Applications/Foo.app/Contents/Frameworks/Big", str(home))
    assert p == "/Applications/Foo.app/Contents/Frameworks/<目录>" and sw == "Foo"


# ------------------------------------------------------------ 检测规则

def test_gaps_filters(fb):
    home, run = fb
    known = {"tool": "x", "card": "xcode-derived-data", "hint": None}
    write_facts(run, [
        fnode(home / "a1", 2.0, id="n1"),                                                     # 缺口
        fnode(home / "a2", 0.9, id="n2"),                                                     # 太小
        fnode(home / "a3", 3.0, id="n3", producer=known),                                     # 有知识卡
        fnode(home / "a4", 3.0, id="n4", producer={"tool": "somerule", "card": None, "hint": "h"}),   # 被生产者规则认出
        fnode(home / "a5", 3.0, id="n5", protected=True),                                     # 受保护
        fnode(home / "a6", 3.0, id="n6", frontier=False),                                     # 非前沿
    ])
    res = feedback.detect(str(run))
    assert res["total_dirs"] == 1 and res["gaps"][0]["bytes"] == 2 * GB and res["asked"] is True
    assert feedback.detect(str(run), min_gb=0.5)["total_dirs"] == 2
    assert feedback.detect(str(run), min_gb=5)["asked"] is False and feedback.detect(str(run), min_gb=5)["reason"] == "none"


def test_same_pattern_merged_and_sorted(fb):
    home, run = fb
    write_facts(run, [fnode(home / "p1", 1.5, id="n1"), fnode(home / "p2", 2.0, id="n2"),
                      fnode(home / "Library" / "Application Support" / "com.foo.app", 1.1, id="n3")])
    gaps = feedback.detect(str(run))["gaps"]
    assert [g["pattern"] for g in gaps] == ["~/<目录>", "~/Library/Application Support/com.foo.app"]
    assert gaps[0]["count"] == 2 and gaps[0]["bytes"] == int(3.5 * GB) and gaps[0]["size"] == "3.5 GB"
    assert gaps[1]["software"] == "com.foo.app"


def test_preview_has_versions_and_sizes(fb):
    home, run = fb
    write_facts(run, [fnode(home / "p1", 1.55, id="n1")])
    res = feedback.detect(str(run))
    assert res["macos_version"] == "26.1" and res["skill_version"] != "未知"
    assert res["skill_version"] in res["preview"] and "macOS：26.1" in res["preview"] and "1.6 GB" in res["preview"]
    assert "1 个大目录" in res["preview"]


def test_skill_version_follows_plugin_json():
    pj = json.loads((Path(feedback.SKILL_ROOT) / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert feedback.skill_version() == pj["version"]


# ------------------------------------------------------------ Issue 链接

def test_issue_url_shape_and_fallback_text(fb, capsys):
    home, run = fb
    write_facts(run, privacy_nodes(home))
    assert feedback.main(["issue-url", "--run", str(run)]) == 0
    out = capsys.readouterr().out
    url = [ln for ln in out.splitlines() if ln.startswith(feedback.ISSUE_BASE)][0]
    assert url.startswith("https://github.com/sanshengai/sansheng-disk-cleanup/issues/new?title=")
    q = parse_qs(urlparse(url).query)
    assert "知识缺口" in q["title"][0] and "~/<目录>/<目录>" in q["body"][0]
    assert "上不了 GitHub 时：复制上面的文字，粘贴到 https://sanshengai.top/tools/disk-cleanup/feedback/ 或发到公众号后台" in out
    assert q["body"][0] in out                                      # 展示给用户的全文就是将要提交的正文
    for secret in SECRETS:
        assert secret not in out


def test_issue_url_truncates_long_lists(fb):
    home, run = fb
    write_facts(run, [fnode(home / "Library" / "Application Support" / f"com.soft{i:03d}.app" / "x", 1.0 + i / 1000, id=f"n{i}")
                      for i in range(200)])
    res = feedback.detect(str(run))
    url, body, omitted = feedback.build_issue(res)
    assert len(url) <= feedback.URL_LIMIT and omitted > 0 and f"另有 {omitted} 条省略" in body
    assert "另有" in res["preview"]                                  # 预览也只列前 30 条
    assert len(decoded_url(url)) < len(url)


def test_issue_url_no_gaps_exits_1(fb, capsys):
    home, run = fb
    write_facts(run, [fnode(home / "tiny", 0.1)])
    assert feedback.main(["issue-url", "--run", str(run)]) == 1


# ------------------------------------------------------------ 冷却

def test_decline_cooldown_90_days(fb, capsys):
    home, run = fb
    write_facts(run, [fnode(home / "p1", 2.0)])
    assert feedback.detect(str(run))["asked"] is True
    assert feedback.main(["decline"]) == 0
    prof = json.loads(Path(protect.PROFILE).read_text(encoding="utf-8"))
    assert prof["feedback_declined_at"].endswith("Z")
    res = feedback.detect(str(run))
    assert res["asked"] is False and res["reason"] == "declined" and "不要再问" in res["hint"]
    soon = datetime.now(timezone.utc) + timedelta(days=89)
    later = datetime.now(timezone.utc) + timedelta(days=91)
    assert feedback.detect(str(run), now=soon)["asked"] is False
    assert feedback.detect(str(run), now=later)["asked"] is True
    capsys.readouterr()
    assert feedback.main(["gaps", "--run", str(run)]) == 0
    assert json.loads(capsys.readouterr().out)["asked"] is False


def test_hint_line(fb):
    home, run = fb
    write_facts(run, [fnode(home / "p1", 2.0), fnode(home / "p2", 3.0, id="n2")])
    line = feedback.hint_line(str(run))
    assert "这次有 2 个大目录认不出来源" in line and "feedback.py issue-url" in line
    feedback.main(["decline"])
    assert feedback.hint_line(str(run)) is None
    assert feedback.hint_line(str(run / "不存在")) is None


# ------------------------------------------------------------ 成果页首次 Star

@pytest.fixture
def renv(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def star_in_page(run):
    return "R.star" in (Path(run) / "report.html").read_text(encoding="utf-8") and \
        json.loads((Path(run) / "report.json").read_text(encoding="utf-8"))["star"]["show"]


def test_star_first_report_only(renv):
    digest, _ = receipts_setup(renv)
    assert renv.act("c01") == 0                                         # 看板路径正式执行已自动出成果页（第一次）
    assert json.loads((renv.run / "report.json").read_text())["star"] == {"show": True, "url": report.REPO_URL}
    assert "好用的话" in (renv.run / "report.html").read_text() and 'id="star"' in (renv.run / "report.html").read_text()
    prof = json.loads(Path(protect.PROFILE).read_text())
    assert prof["star_asked_at"].endswith("Z")
    # 第二次清理（另一个 run）：不再出现
    run2 = renv.run.parent / "scan2"
    run2.mkdir()
    for name in ("facts.json", "plan.json", "seal.json", "receipt.json"):
        (run2 / name).write_text((renv.run / name).read_text())
    report.make(str(run2))
    assert json.loads((run2 / "report.json").read_text())["star"]["show"] is False


def test_star_stays_when_regenerating_same_run(renv):
    receipts_setup(renv)
    assert renv.act("c01") == 0
    first = json.loads(Path(protect.PROFILE).read_text())["star_asked_at"]
    report.make(str(renv.run))                                          # 同一次清理里刷新成果页：上一份带着就继续带着
    assert json.loads((renv.run / "report.json").read_text())["star"]["show"] is True
    assert json.loads(Path(protect.PROFILE).read_text())["star_asked_at"] == first


def test_build_report_is_pure_and_main_cli_writes_profile(renv):
    receipts_setup(renv)
    assert renv.act("c01") == 0
    Path(protect.PROFILE).unlink()
    report.build_report(str(renv.run))                                  # 纯计算：不写 profile
    assert not Path(protect.PROFILE).exists()
    assert report.main(["--run", str(renv.run)]) == 0
    assert "star_asked_at" in json.loads(Path(protect.PROFILE).read_text())


def test_star_link_is_github_repo_and_template_guards_scheme():
    html = (Path(report.TEMPLATE)).read_text(encoding="utf-8")
    assert report.REPO_URL == "https://github.com/sanshengai/sansheng-disk-cleanup"
    assert "好用的话，" in html and "去 GitHub 点个 Star" in html and r"^https:\/\/github\.com\/" in html


# ------------------------------------------------------------ 看板路径自动成果页 + 汇报末尾提示

def test_run_formal_generates_report_but_dry_and_pilot_do_not(renv, capsys):
    digest, _ = receipts_setup(renv)
    base = ["run", "--run", str(renv.run), "--seal", digest, "--cards", "c01"]
    assert act.main(auto_sel(base + ["--dry-run"])) == 0
    assert act.main(auto_sel(base + ["--pilot"])) == 0
    assert not (renv.run / "report.html").exists() and not (renv.run / "report.json").exists()
    assert not Path(protect.PROFILE).exists() or "star_asked_at" not in json.loads(Path(protect.PROFILE).read_text())
    capsys.readouterr()
    assert act.main(auto_sel(base)) == 0
    out = capsys.readouterr().out
    assert (renv.run / "report.html").exists()
    assert f"成果页：{renv.run}/report.html" in out and "腾出" in out


def test_run_report_failure_does_not_fail_cleanup(renv, monkeypatch, capsys):
    digest, dirs = receipts_setup(renv)
    monkeypatch.setattr(report, "make", lambda run: (_ for _ in ()).throw(ValueError("boom")))
    assert renv.act("c01") == 0
    assert not dirs["c01"].exists() and "成果页没生成：boom" in capsys.readouterr().err


def gap_facts(renv, gb=2.0):
    """把 receipts_setup 写的 facts 追加一个认不出来源的大目录。"""
    f = json.loads((renv.run / "facts.json").read_text())
    f["nodes"].append(fnode(renv.work / "mystery", gb, id="n99"))
    (renv.run / "facts.json").write_text(json.dumps(f))


def test_run_prints_feedback_hint_once_and_respects_cooldown(renv, capsys):
    receipts_setup(renv)
    gap_facts(renv)
    assert renv.act("c01") == 0
    out = capsys.readouterr().out
    assert out.count("这次有 1 个大目录认不出来源") == 1 and "feedback.py issue-url" in out
    # 冷却期内不再提示
    feedback.main(["decline"])
    capsys.readouterr()
    assert renv.act("c02") == 0
    assert "认不出来源" not in capsys.readouterr().out


def test_hint_never_sends_or_writes_anything(renv, capsys, monkeypatch):
    receipts_setup(renv)
    gap_facts(renv)
    def no_net(*a, **k):
        raise AssertionError("不得联网")
    monkeypatch.setattr(socket.socket, "connect", no_net)
    assert renv.act("c01") == 0
    prof = json.loads(Path(protect.PROFILE).read_text())
    assert "feedback_declined_at" not in prof                           # 只提示，不替用户做任何决定


def test_go_generates_report_once(renv, capsys):
    """go 自己在全部步骤结束后生成成果页，三步里的 run 不重复生成。"""
    digest, _ = receipts_setup(renv)
    gap_facts(renv)
    plan.main(["check", "--run", str(renv.run)])
    seal = json.loads((renv.run / "seal.json").read_text())
    (renv.run / "proposal.json").write_text(json.dumps(
        {"seal": seal["digest"], "mode": "chat", "stage": "cleanup", "options": {"safe": {"cards": ["c01"], "answers": [],
                                                      "sel": plan.selection_sig(seal["digest"], ["c01"], {})}}}))
    capsys.readouterr()
    assert act.main(["go", "--run", str(renv.run), "--choice", "safe"]) == 0
    out = capsys.readouterr().out
    assert out.count("成果页：") == 1 and "【go】成果页：" in out
    assert out.count("认不出来源") == 1
