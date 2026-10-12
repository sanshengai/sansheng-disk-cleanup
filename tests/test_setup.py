"""0.6 初始设置：题库出现条件、导出文本解析与答案落地、升级 / 改设置模式、设置页数据、清理看板新字段。

夹具全在 tmp_path 内（沿用 test_plan_act 的 Env）。设置页模板由别处编写，这里 plan.py setup 用临时假模板。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, age, card, delete_item, mkfile, node, plan, protect  # noqa: E402
from test_projects import ledger, write_profile  # noqa: E402

import prefs  # noqa: E402
import setup_q  # noqa: E402

NOW = datetime(2026, 10, 12, 8, 0, 0, tzinfo=timezone.utc)
GB = 10 ** 9


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def proj(name, bytes_=2 * GB, status="active"):
    return {"key": f"/Users/x/{name}", "name": name, "kind": "git", "bytes": bytes_, "last_active": iso(3), "sessions": 2,
            "deliverables_guess": [], "default_status": status, "shown": True, "roots": ["/R"], "claude_dirs": [], "session_ids": []}


def tool(key, bytes_, days_ago=5):
    return {"key": key, "name": setup_q.TOOL_NAMES[key], "bytes": bytes_, "last_used": iso(days_ago) if days_ago is not None else None,
            "paths": ["~/x"]}


def pf(projects=(), tools=(), cands=(), **sf):
    base = {"ai_sessions_bytes": 0, "ai_images_bytes": 0, "worktrees": 0, "installers_bytes": 0, "recordings_bytes": 0,
            "cloud_local_bytes": 0, "time_machine_last": None, "top_dirs": [{"path": "~/Documents", "bytes": 5 * GB}]}
    base.update(sf)
    return {"schema": "projects/1", "scan_id": "scan1", "projects": list(projects), "tools": list(tools),
            "deliverable_candidates": list(cands), "setup_facts": base}


AI_HEAVY = lambda: pf(  # noqa: E731
    projects=[proj("短剧"), proj("旧稿", status="done")],
    tools=[tool("android", 17 * GB, 41), tool("codex", 40 * GB, 1), tool("claude-code", 6 * GB, 0), tool("xcode", 30 * GB, 90)],
    cands=[{"path": "~/Movies/成片", "why": "名字含“成片”", "bytes": GB, "files": 34}],
    ai_sessions_bytes=41 * GB, ai_images_bytes=30 * GB, worktrees=4, installers_bytes=2 * GB, recordings_bytes=9 * GB,
    cloud_local_bytes=8 * GB)
OFFICE = lambda: pf(installers_bytes=400 * 10 ** 6, recordings_bytes=3 * GB)  # noqa: E731
DEVELOPER = lambda: pf(  # noqa: E731
    projects=[proj("app")], tools=[tool("xcode", 40 * GB), tool("docker", 12 * GB)],
    worktrees=2, installers_bytes=GB, recordings_bytes=100 * 10 ** 6)
ALWAYS = {"read_scope", "protect", "wait", "done_projects", "governance", "delete_mode", "confirm", "note"}


def ids(payload):
    return {q["id"] for g in payload["groups"] for q in g["questions"]}


def nos(payload):
    return sorted(q["no"] for g in payload["groups"] for q in g["questions"])


# ------------------------------------------------------------ 出题条件

def test_ai_heavy_user_sees_all_17_questions():
    p = setup_q.build_payload({}, AI_HEAVY(), "first", now=NOW)
    assert nos(p) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]       # 云盘那题（11）本机存了 8 GB，要问
    assert [g["id"] for g in p["groups"]] == ["read", "machine", "ai", "files", "way", "note"]
    assert [t["key"] for t in p["tools"]] == ["codex", "xcode", "android", "claude-code"]        # 体积降序
    assert {t["key"]: t["recommended"] for t in p["tools"]} == {"codex": "often", "xcode": None, "android": None, "claude-code": "often"}
    assert [x["name"] for x in p["projects"]] == ["短剧", "旧稿"] and p["deliverable_candidates"][0]["path"] == "~/Movies/成片"
    assert p["top_dirs"][0]["path"] == "~/Documents" and p["version"] == 1 and p["mode"] == "first"


def test_cloud_question_needs_flag_and_size(monkeypatch):
    assert setup_q.CLOUD_ENABLED is True                                                     # 0.6 上线
    assert 11 in nos(setup_q.build_payload({}, AI_HEAVY(), "first", now=NOW))
    assert 11 not in nos(setup_q.build_payload({}, pf(cloud_local_bytes=GB), "first", now=NOW))     # 不到 5 GB 不问
    monkeypatch.setattr(setup_q, "CLOUD_ENABLED", False)                                     # 总开关还在：关了就不问，答案仍能解析
    assert 11 not in nos(setup_q.build_payload({}, AI_HEAVY(), "first", now=NOW))
    p = {}
    done, errs = setup_q.apply_answers(p, {"cloud": "ok"})
    assert not errs and p["cloud_policy"] == "ok"


def test_cloud_question_text_explains_what_cloud_only_means():
    """“只留云端”必须讲清：文件留在云上、本机只留占位、用到再下载；先确认没有在上传的文件；删除 ≠ 只留云端。"""
    q = next(x for x in setup_q.QUESTIONS if x["id"] == "cloud")
    opts = {o["value"]: o for o in q["options"]}
    assert "只留云端" in q["title"] and "占位" in opts["ok"]["desc"] and "再下载" in opts["ok"]["desc"]
    assert "正在上传" in q["risk"] and "删除" in q["risk"] and "连云端" in q["risk"]
    assert "不会替你释放" in q["why"] or "不会" in q["why"]
    payload = setup_q.build_payload({}, AI_HEAVY(), "first", now=NOW)
    cq = next(x for g in payload["groups"] for x in g["questions"] if x["id"] == "cloud")
    assert "8" in cq["context"] and cq["recommended"] == "unsure"


def test_office_user_sees_the_nine_always_or_file_questions():
    p = setup_q.build_payload({}, OFFICE(), "first", now=NOW)
    assert nos(p) == [1, 5, 9, 10, 12, 13, 14, 15, 16, 17]
    assert p["projects"] == [] and p["tools"] == [] and p["deliverable_candidates"] == []


def test_developer_sees_dev_questions_without_ai_habits():
    p = setup_q.build_payload({}, DEVELOPER(), "first", now=NOW)
    assert nos(p) == [1, 2, 3, 4, 5, 8, 9, 12, 13, 14, 15, 16, 17]


@pytest.mark.parametrize("pfile", [None, {}, pf(), {"schema": "projects/1", "scan_id": "s", "projects": []}])
def test_empty_machine_only_gets_always_questions(pfile):
    p = setup_q.build_payload({}, pfile, "first", now=NOW)
    assert ids(p) == ALWAYS and nos(p) == [1, 5, 12, 13, 14, 15, 16, 17]


def test_small_amounts_do_not_trigger_questions():
    p = pf(tools=[tool("docker", 999 * 10 ** 6)], installers_bytes=299 * 10 ** 6, recordings_bytes=GB - 1, ai_sessions_bytes=0)
    assert ids(setup_q.build_payload({}, p, "first", now=NOW)) == ALWAYS


def test_every_question_has_why_risk_and_plain_text():
    p = setup_q.build_payload({}, AI_HEAVY(), "first", now=NOW)
    for g in p["groups"]:
        for q in g["questions"]:
            assert q["title"] and q["context"] and q["why"] and q["risk"], q["id"]
            assert set(q) >= {"id", "no", "group", "type", "title", "context", "options", "recommended", "current", "why", "risk"}
            assert q["current"] is None                                  # 第一次使用没有现值
    read = next(q for g in p["groups"] for q in g["questions"] if q["id"] == "read_scope")
    assert [o["value"] for o in read["options"]] == ["basic", "extended", "deep", "never"]
    assert read["options"][-1]["locked"] and read["recommended"] == ["basic", "extended"]
    assert "AI 服务商" in p["privacy_note"]


def test_context_carries_machine_findings():
    p = setup_q.build_payload({}, AI_HEAVY(), "first", now=NOW)
    by = {q["id"]: q for g in p["groups"] for q in g["questions"]}
    assert "41.0 GB" in by["conversations"]["context"] and "Codex" in by["conversations"]["context"]
    assert "4 个" in by["worktrees"]["context"]
    assert "30.0 GB" in by["rejects"]["context"]


@pytest.mark.parametrize("tm, first, expect", [
    (None, False, "trash"), (iso(30), False, "trash"), (iso(3), False, "delete"), (iso(3), True, "trash"),
    (iso(14), False, "delete"), (iso(15), False, "trash")])
def test_delete_mode_recommendation(tm, first, expect):
    p = setup_q.build_payload({}, pf(time_machine_last=tm), "first" if first else "edit", now=NOW)
    q = next(q for g in p["groups"] for q in g["questions"] if q["id"] == "delete_mode")
    assert q["recommended"] == expect and p["time_machine_last"] == tm
    assert ("没有读到" in q["context"]) == (tm is None)


# ------------------------------------------------------------ 导出文本解析与落地

def make_dirs(env):
    (env.work / "成品").mkdir(exist_ok=True)
    (env.home / "Documents" / "合同").mkdir(exist_ok=True)
    (env.home / "短剧").mkdir(exist_ok=True)
    return env.work / "成品", env.home / "Documents" / "合同", env.home / "短剧"


def full_answers(env):
    deliv, contract, _ = make_dirs(env)
    return {"read_scope": ["basic", "extended"], "tools": {"android": "unused", "codex": "often", "xcode": "sometimes"},
            "deliverables": [str(deliv)], "protect": [str(contract)],
            "conversations": "sometimes", "conversation_days": 15, "rejects": "until_done", "worktrees": "build_only",
            "installers": "auto", "big_files": 90, "cloud": "unsure", "wait": "some", "done_projects": "archive",
            "governance": "fix", "delete_mode": "trash", "confirm": "board", "note": "下载里的视频都是课程，别动"}


def read_profile(env):
    f = env.home / ".sansheng" / "disk-cleanup" / "profile.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def test_roundtrip_export_parse_apply(env):
    _, contract, short = make_dirs(env)
    answers = full_answers(env)
    note = answers.pop("note")
    text = setup_q.export_text(answers, ledger={str(short): {"状态": "做完了", "名称": "短剧"}}, note=note)
    assert setup_q.is_setup_export(text) and not prefs.is_ledger_export(text)
    data, errs = setup_q.parse_setup(text)
    assert not errs and data["answers"] == answers and data["note"] == note and data["version"] == 1
    assert list(data["ledger"]) == [str(short)]
    done, errs, got_note = setup_q.apply_export(text)
    assert not errs and done and got_note == note
    p = read_profile(env)
    assert p["read_scope"] == ["basic", "extended"] and p["tools"] == {"android": "unused", "codex": "often", "xcode": "sometimes"}
    assert p["conversation_policy"] == "sometimes" and p["retention_days"] == 15           # conversation_days 优先
    assert p["rejects_policy"] == "until_done" and p["worktree_policy"] == "build_only" and p["installer_policy"] == "auto"
    assert p["big_file_age_days"] == 90 and p["cloud_policy"] == "unsure" and p["default_choice"] == "recommended"
    assert p["done_project_policy"] == "archive" and p["governance_mode"] == "fix" and p["delete_mode"] == "trash"
    assert p["confirm_mode"] == "board"
    assert str(env.work / "成品").replace(str(env.home), "~") in p["deliverable_roots"]
    assert str(env.work / "成品").replace(str(env.home), "~") in p["reference_roots"]            # 成品目录同时是引用检查目录
    assert "~/Documents/合同" in p["protect"]
    assert p["projects"]["env:android"]["status"] == "done" and p["projects"]["env:android"]["source"] == "board"   # unused → 做完了
    assert "env:xcode" not in p["projects"]                                                  # sometimes → 不改
    assert p["projects"][str(short)]["status"] == "done"
    assert p["setup"]["version"] == 1 and p["onboarded_at"] and p["setup"]["answers"]["tools"]["android"] == "unused"
    assert "note" not in p["setup"]["answers"] and "下载里的视频" not in json.dumps(p, ensure_ascii=False)     # 原话不存


def test_tools_often_marks_env_project_active_but_never_loosens_protected(env):
    write_profile(env, projects={"env:docker": {"name": "Docker", "status": "protected", "deliverables": [],
                                                "updated_at": "2026-10-01T00:00:00Z", "source": "board"}})
    done, errs, _ = setup_q.apply_export(setup_q.export_text({"tools": {"android": "often", "docker": "unused", "xcode": "unused"}}))
    p = read_profile(env)
    assert not errs
    assert p["projects"]["env:android"]["status"] == "active" and p["projects"]["env:xcode"]["status"] == "done"
    assert p["projects"]["env:docker"]["status"] == "protected"          # “不用了”不能把用户标的“别碰”放松
    assert p["tools"]["docker"] == "unused"                               # 回答本身照记


def test_conversation_days_defaults_and_validation(env):
    for val, days in (("done", 7), ("sometimes", 30), ("library", 0)):
        _, errs, _ = setup_q.apply_export(setup_q.export_text({"conversations": val}))
        assert not errs and read_profile(env)["retention_days"] == days
    _, errs, _ = setup_q.apply_export(setup_q.export_text({"conversations": "done", "conversation_days": 45}))
    assert any("45" in e for e in errs) and read_profile(env)["retention_days"] == 7           # 非法天数按默认
    _, errs, _ = setup_q.apply_export(setup_q.export_text({"conversation_days": 60}))               # 编辑模式只改了天数
    assert not errs and read_profile(env)["retention_days"] == 60 and read_profile(env)["conversation_policy"] == "done"


def test_wait_maps_to_default_choice_and_big_files_accept_strings(env):
    for wait, dc in (("minimal", "safe"), ("some", "recommended"), ("space", "all")):
        setup_q.apply_export(setup_q.export_text({"wait": wait}))
        assert read_profile(env)["default_choice"] == dc
    setup_q.apply_export(setup_q.export_text({"big_files": "30"}))
    assert read_profile(env)["big_file_age_days"] == 30


def test_bad_values_are_reported_and_good_ones_still_saved(env):
    make_dirs(env)
    answers = {"conversations": "sometimes", "rejects": "burn", "worktrees": 5, "big_files": 45, "wait": "fast",
               "read_scope": "basic", "mystery": 1, "deliverables": ["~/Documents"], "protect": ["/etc"],
               "tools": {"android": "maybe", "nope": "often", "docker": "often"}}
    done, errs, _ = setup_q.apply_export(setup_q.export_text(answers))
    p = read_profile(env)
    assert p["conversation_policy"] == "sometimes" and p["tools"] == {"docker": "often"}
    for k in ("rejects_policy", "worktree_policy", "big_file_age_days", "default_choice", "read_scope"):
        assert k not in p, k
    assert not p.get("deliverable_roots") and not p.get("protect")
    text = "\n".join(errs)
    for want in ("burn", "45", "fast", "mystery", "太宽泛", "只接受家目录", "maybe", "nope"):
        assert want in text, want
    assert done


def test_all_bad_saves_nothing(env):
    done, errs, _ = setup_q.apply_export(setup_q.export_text({"rejects": "burn"}))
    assert not done and errs and not read_profile(env).get("setup") and not read_profile(env).get("onboarded_at")


@pytest.mark.parametrize("text, frag", [
    ("", "空"), ("叁笙清理选择\n答案: {}", "首行"), (f"{setup_q.SETUP_TITLE}\n答案: {{}}", "设置版本"),
    (f"{setup_q.SETUP_TITLE}\n设置版本: x\n答案: {{}}", "整数"), (f"{setup_q.SETUP_TITLE}\n设置版本: 1\n答案: [1]", "对象"),
    (f"{setup_q.SETUP_TITLE}\n设置版本: 1\n答案: {{oops", "JSON"), (f"{setup_q.SETUP_TITLE}\n设置版本: 1", "没有找到"),
    (f"{setup_q.SETUP_TITLE}\n设置版本: 1\n答案: {{}}\n补充: 123", "字符串"),
    (f"{setup_q.SETUP_TITLE}\n设置版本: 1\n答案: {{}}\n答案: {{}}", "多次"),
    (f"{setup_q.SETUP_TITLE}\n设置版本: 1\n答案: {{}}\n台账: [", "JSON")])
def test_parse_setup_rejects_malformed(text, frag):
    data, errs = setup_q.parse_setup(text)
    assert data is None and any(frag in e for e in errs), errs


def test_future_version_is_refused_without_saving(env):
    done, errs, _ = setup_q.apply_export(setup_q.export_text({"wait": "some"}, version=99))
    assert not done and any("新" in e for e in errs) and not read_profile(env)


def test_unicode_line_separators_stay_on_one_line():
    text = setup_q.export_text({"protect": ["~/a b"]}, note="x y\u0085z")
    data, errs = setup_q.parse_setup(text)
    assert not errs and data["answers"]["protect"] == ["~/a b"] and data["note"] == "x y\u0085z"
    assert len(text.splitlines()) == 4


def test_partial_export_keeps_earlier_answers(env):
    setup_q.apply_export(setup_q.export_text({"wait": "some", "governance": "fix"}))
    setup_q.apply_export(setup_q.export_text({"governance": "tell"}))              # 编辑模式：只带改动过的题
    p = read_profile(env)
    assert p["governance_mode"] == "tell" and p["default_choice"] == "recommended"
    assert p["setup"]["answers"] == {"wait": "some", "governance": "tell"}


def test_editing_deliverables_removes_only_what_setup_added(env):
    deliv, _, _ = make_dirs(env)
    other = env.work / "别处加的"
    other.mkdir()
    write_profile(env, deliverable_roots=["~/work/别处加的"], reference_roots=["~/work/别处加的"])
    setup_q.apply_export(setup_q.export_text({"deliverables": [str(deliv)]}))
    assert read_profile(env)["deliverable_roots"] == ["~/work/别处加的", "~/work/成品"]
    setup_q.apply_export(setup_q.export_text({"deliverables": []}))                  # 用户在编辑里取消了
    p = read_profile(env)
    assert p["deliverable_roots"] == ["~/work/别处加的"] and p["reference_roots"] == ["~/work/别处加的"]


def test_protect_only_grows_through_setup(env):
    _, contract, _ = make_dirs(env)
    setup_q.apply_export(setup_q.export_text({"protect": [str(contract)]}))
    setup_q.apply_export(setup_q.export_text({"protect": []}))
    assert read_profile(env)["protect"] == ["~/Documents/合同"]


def test_ledger_line_wins_over_tool_sync(env):
    answers = {"tools": {"android": "unused"}}
    setup_q.apply_export(setup_q.export_text(answers, ledger={"env:android": {"状态": "别碰"}}))
    assert read_profile(env)["projects"]["env:android"]["status"] == "protected"


def test_only_ledger_export_still_completes_setup(env):
    done, errs, note = setup_q.apply_export(setup_q.export_text({}, ledger={}))
    assert not errs and done and note is None
    p = read_profile(env)
    assert p["onboarded_at"] and p["setup"]["version"] == 1 and setup_q.stage_of(p) == "cleanup"


# ------------------------------------------------------------ plan.py profile --apply / act.py 拒收

def test_profile_apply_detects_setup_export_and_prints_note(env, capsys, tmp_path):
    make_dirs(env)
    f = tmp_path / "setup.txt"
    f.write_text(setup_q.export_text({"wait": "space", "governance": "guide"}, note="~/work 是公司电脑的同步盘"), encoding="utf-8")
    assert plan.main(["profile", "--apply", str(f)]) == 0
    out = capsys.readouterr()
    assert "已保存：默认档位：全部可删" in out.out and "~/work 是公司电脑的同步盘" in out.out and "memory.py pending add" in out.out
    assert read_profile(env)["governance_mode"] == "guide"
    f.write_text(setup_q.export_text({"rejects": "burn"}), encoding="utf-8")
    assert plan.main(["profile", "--apply", str(f)]) == 2                                   # 全是错的：退出码 2
    assert "没保存" in capsys.readouterr().err


def test_go_from_board_refuses_setup_export(env, tmp_path, capsys):
    """初始设置导出不含清理授权：act.py go --from-board 一律拒绝，什么都不保存、不删。"""
    d = env.work / "d1"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d, id="n1")], [card("c01", [delete_item(d)])])
    env.seal()
    f = tmp_path / "setup.txt"
    f.write_text(setup_q.export_text({"wait": "space"}) + "清理: c01\n回答: c01=删\nsel: 000000000000\nseal: 000000000000\n"
                 "scan_id: scan1\n", encoding="utf-8")
    assert act.main(["go", "--run", str(env.run), "--from-board", str(f)]) == 2
    assert "叁笙清理选择" in capsys.readouterr().err
    assert (d / "a.bin").exists() and not read_profile(env).get("setup") and not list(env.run.glob("receipt*.json"))


# ------------------------------------------------------------ 升级 / 改设置模式

def v05_profile(env):
    """0.5 的老用户：有台账和 default_choice，没有 setup.version。"""
    write_profile(env, onboarded_at="2026-10-01T00:00:00Z", default_choice="safe", retention_days=15,
                  projects=ledger(env.home / "短剧", "done"))


def test_stage_and_upgrade_flag():
    assert setup_q.stage_of({}) == "onboarding" and not setup_q.setup_upgrade({})
    assert setup_q.stage_of({"onboarded_at": "x"}) == "cleanup" and setup_q.setup_upgrade({"onboarded_at": "x"})
    assert setup_q.stage_of({"setup": {"version": 1}}) == "cleanup" and not setup_q.setup_upgrade({"setup": {"version": 1}})
    assert setup_q.setup_upgrade({"setup": {"version": 0}, "onboarded_at": "x"})
    assert setup_q.auto_mode({}) == "first" and setup_q.auto_mode({"onboarded_at": "x"}) == "upgrade"
    assert setup_q.auto_mode({"setup": {"version": 1}}) == "edit"


def test_upgrade_for_v05_user_has_no_project_table_and_only_new_questions(env):
    (env.home / "短剧").mkdir()
    v05_profile(env)
    p = setup_q.build_payload(read_profile(env), AI_HEAVY(), "upgrade", now=NOW)
    assert p["projects"] == [] and p["deliverable_candidates"] == [] and p["top_dirs"] == []
    assert ids(p) == {"read_scope", "tools", "conversations", "rejects", "worktrees", "installers", "big_files", "cloud",
                      "done_projects", "governance", "delete_mode", "confirm", "note"}
    assert not ids(p) & {"projects", "deliverables", "protect", "wait"}               # 0.5 已经问过的不重问
    assert [g["id"] for g in p["groups"]] == ["read", "machine", "ai", "files", "way", "note"]
    assert p["mode"] == "upgrade"


def test_upgrade_after_version_bump_asks_only_questions_newer_than_answered():
    old = {"id": "a", "since": 1, "legacy": False}
    new = {"id": "b", "since": 2, "legacy": False}
    assert not setup_q.is_new_question(old, 1) and setup_q.is_new_question(new, 1)
    assert setup_q.is_new_question(old, 0) and not setup_q.is_new_question(dict(old, legacy=True), 0)


def test_upgrade_with_nothing_new_drops_empty_groups():
    p = setup_q.build_payload({"setup": {"version": 1}}, AI_HEAVY(), "upgrade", now=NOW)
    assert p["groups"] == [] and p["tools"] == [] and p["projects"] == []


def test_edit_mode_prefills_current_values(env):
    make_dirs(env)
    setup_q.apply_export(setup_q.export_text(full_answers(env) | {"note": ""}))
    p = setup_q.build_payload(read_profile(env), AI_HEAVY(), "edit", now=NOW)
    by = {q["id"]: q for g in p["groups"] for q in g["questions"]}
    assert by["read_scope"]["current"] == ["basic", "extended"] and by["conversations"]["current"] == "sometimes"
    assert by["conversations"]["current_days"] == 15 and by["big_files"]["current"] == 90
    assert by["wait"]["current"] == "some" and by["delete_mode"]["current"] == "trash" and by["confirm"]["current"] == "board"
    assert by["tools"]["current"]["android"] == "unused" and by["protect"]["current"] == ["~/Documents/合同"]
    assert {t["key"]: t["current"] for t in p["tools"]}["android"] == "unused"
    assert by["note"]["current"] is None and p["mode"] == "edit"


def fake_template(env):
    t = env.run.parent / "tpl.html"
    t.write_text('<img src="__LOGO_DATA__"><script type="application/json" id="setup-data">__SETUP_JSON__</script>', encoding="utf-8")
    env.mp.setattr(plan, "SETUP_TEMPLATE", str(t))


def write_projects(env, pfile):
    pfile = dict(pfile, scan_id="scan1")
    (env.run / "projects.json").write_text(json.dumps(pfile, ensure_ascii=False), encoding="utf-8")


def injected_setup(env):
    import re
    html = (env.run / "setup.html").read_text(encoding="utf-8")
    return json.loads(re.search(r'id="setup-data">(.*?)</script>', html, re.S).group(1))


def test_plan_setup_auto_mode_and_old_projects_json(env):
    """0.5 的 projects.json 没有 tools / setup_facts：缺的字段按空处理，只出总是出现的题。"""
    fake_template(env)
    old = {"schema": "projects/1", "projects": [proj("短剧")], "skipped": []}
    env.write([], [])
    write_projects(env, old)
    assert plan.main(["setup", "--run", str(env.run)]) == 0
    d = injected_setup(env)
    assert d["mode"] == "first" and ids(d) == ALWAYS | {"projects", "deliverables"}
    (env.home / "短剧").mkdir()
    v05_profile(env)
    assert plan.main(["setup", "--run", str(env.run)]) == 0
    d = injected_setup(env)
    assert d["mode"] == "upgrade" and d["projects"] == [] and "projects" not in ids(d)
    assert plan.main(["setup", "--run", str(env.run), "--mode", "edit"]) == 0
    assert injected_setup(env)["mode"] == "edit"


def test_propose_reports_setup_upgrade_for_v05_profile(env):
    d = env.work / "d1"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d, id="n1")], [card("c01", [delete_item(d)])])
    env.seal()
    v05_profile(env)
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    pr = json.loads((env.run / "proposal.json").read_text())
    assert pr["stage"] == "cleanup" and pr["setup_upgrade"] is True and "只补新题" in pr["setup_hint"]
    setup_q.apply_export(setup_q.export_text({"wait": "some"}))
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    pr = json.loads((env.run / "proposal.json").read_text())
    assert pr["setup_upgrade"] is False and "setup_hint" not in pr


# ------------------------------------------------------------ 清理看板新字段

def test_composition_purgeable_and_frontier_category(env):
    docs = env.home / "Documents" / "项目"
    cache = env.work / "cache"
    mkfile(docs / "a.bin")
    mkfile(cache / "a.bin")
    nodes = [node(env.home, id="n0", depth=0, bytes=60 * GB),
             node(docs, id="n1", depth=1, bytes=20 * GB),
             node(cache, id="n2", depth=1, bytes=5 * GB, producer={"tool": "t", "card": "docker", "hint": None}),
             node(env.work / "other", id="n3", depth=1, bytes=GB),
             node("/Applications", id="n4", depth=0, bytes=10 * GB)]
    c = card("c01", [delete_item(cache)])
    c["cards_used"] = ["android"]
    c["category"] = "ai-images"                                  # 计划里的卡片归类优先于识别出的工具
    env.write(nodes, [c])
    facts = json.loads((env.run / "facts.json").read_text())
    facts.update({"disk": {"total": 200 * GB, "used": 100 * GB, "free": 100 * GB}, "host": {"home": str(env.home)},
                  "capacity": {"purgeable_estimate": 12 * GB}})
    (env.run / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
    tpl = env.run.parent / "tpl.html"
    tpl.write_text('<img src="__LOGO_DATA__"><script>const D = __PLAN_JSON__;</script>', encoding="utf-8")
    env.mp.setattr(plan, "TEMPLATE", str(tpl))
    logo = env.run.parent / "logo.png"
    logo.write_bytes(b"\x89PNG")
    env.mp.setattr(plan, "LOGO", str(logo))
    assert plan.main(["board", "--run", str(env.run)]) == 0
    html = (env.run / "board.html").read_text(encoding="utf-8")
    d = json.loads(html.split("const D = ", 1)[1].rsplit(";</script>", 1)[0])
    comp = {c["id"]: c for c in d["facts"]["composition"]}
    assert [c["id"] for c in d["facts"]["composition"]] == ["home", "apps", "system", "other"]
    assert comp["home"]["bytes"] == 60 * GB and comp["apps"]["bytes"] == 10 * GB
    assert comp["system"]["bytes"] is None and comp["system"]["estimated"] and "没有单独量" in comp["system"]["note"]
    assert comp["other"]["bytes"] == 30 * GB and comp["other"]["estimated"]                    # 100 − 60 − 10，不编系统的数
    assert d["facts"]["purgeable_bytes"] == 12 * GB
    cats = {f["path"]: f["category"] for f in d["facts"]["frontier"]}
    assert cats[str(docs)] == "my-files" and cats[str(cache)] == "ai-images" and cats[str(env.work / "other")] == "other"
    assert d["archive_enabled"] is True


def test_composition_without_measurements_does_not_invent_numbers():
    comp = {c["id"]: c for c in plan.composition_of({"disk": {"used": 100 * GB}, "nodes": []})}
    assert comp["home"]["bytes"] is None and comp["apps"]["bytes"] is None and comp["other"]["bytes"] is None
    assert plan.purgeable_bytes({}) is None and plan.purgeable_bytes({"capacity": {"purgeable_estimate": None}}) is None
    assert plan.purgeable_bytes({"capacity": {"purgeable_estimate": 0}}) == 0              # 0 是 0，None 才是不知道


def test_summary_lines_follow_profile(env):
    write_profile(env, onboarded_at="2026-10-01T00:00:00Z", worktree_policy="build_only", big_file_age_days=0,
                  tools={"android": "unused", "docker": "often"})
    assert setup_q.summary_lines(read_profile(env)) == ["合进主线的工作树：只清构建产物", "录屏和大视频：不问你", "不用了的软件：Android 模拟器"]
    assert setup_q.summary_lines({"big_file_age_days": 30, "default_choice": "all", "confirm_mode": "board"}) == [
        "录屏和大视频：超过 30 天没动才问你", "重新下载要等：空间优先（默认全部可删档）", "确认方式：每次都发看板"]
