"""0.6 跨模块衔接：看板只有规则行的清单、记忆日志补全、成果页“忘掉”往返、go 结束时汇总记忆。

夹具沿用 test_go_from_board（tmp_path 内的 Env）。
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, plan  # noqa: E402
from test_go_from_board import export_text, go, no_receipts, profile, setup  # noqa: E402,F401
import memory  # noqa: E402
import prefs  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def pend_ids(env):
    return [x["id"] for x in profile(env).get("pending_rules") or []]


# ---------------------------------------------------------------- 2. 只有规则行的清单也要应用，且在删除之前

def test_rules_only_export_is_applied_before_any_deletion(env, monkeypatch):
    digest, dirs = setup(env)
    memory.pending_add("下载里的缓存以后都清", "rule", {"key": "dl/cache/safe", "action": "delete"})
    memory.pending_add("别再提示日志目录", "rule", {"key": "logs/old/safe", "action": "keep"})
    assert pend_ids(env) == ["p1", "p2"]
    seen = []
    real_run = act.cmd_run

    def spy(args):
        if not seen:                                    # 第一次进 cmd_run（预演）时，规则必须已经保存好
            seen.append((profile(env).get("rules") or {}, pend_ids(env), [(d / "a.bin").exists() for d in dirs.values()]))
        return real_run(args)
    monkeypatch.setattr(act, "cmd_run", spy)
    assert go(env, export_text(digest, ["c01", "c02"], ["c02=删"], extra=["确认规则: p1", "不要规则: p2"])) == 0
    rules, pend, existed = seen[0]
    assert rules["dl/cache/safe"]["action"] == "delete" and rules["dl/cache/safe"]["source"] == "memory"
    assert "logs/old/safe" not in rules and pend == []     # 确认的写进规则，不要的直接丢掉
    assert existed == [True, True]                         # 保存规则时还什么都没删
    assert not dirs["c01"].exists() and not dirs["c02"].exists()


def test_rules_only_export_unknown_rule_id_stops_before_deleting(env, capsys):
    """反例：规则号对不上（没有 p9）→ 没保存全，退出码 3，什么都不删。"""
    digest, dirs = setup(env)
    assert go(env, export_text(digest, ["c01"], extra=["确认规则: p9"])) == 3
    assert "没保存" in capsys.readouterr().err
    assert all((d / "a.bin").exists() for d in dirs.values()) and no_receipts(env)


# ---------------------------------------------------------------- 4. 记忆日志补全

def log_of(env):
    return profile(env).get("memory_log") or []


def test_prefs_answer_logs_rule_and_forget_restores_old(env):
    old_rule = {"action": "keep", "at": "2026-01-01T00:00:00Z", "spec": 1}
    prefs.save({"rules": {"a/b/safe": dict(old_rule)}})
    prefs.answer("a/b/safe", "delete", scan_id="scan1")
    prefs.answer("c/d/safe", "keep")
    prefs.answer("e/f/safe", "once")                    # 只这次：不记
    prefs.answer("g/h/safe", "never")                   # 冷却：不记
    log = log_of(env)
    assert [(e["kind"], e["ref"]["key"], e["ref"]["action"]) for e in log] == [("rule", "a/b/safe", "delete"), ("rule", "c/d/safe", "keep")]
    assert log[0]["scan_id"] == "scan1" and "scan_id" not in log[1]
    assert log[0]["ref"]["old"] == old_rule and log[1]["ref"]["old"] is None
    done, errs = memory.forget("m1,m2")
    assert not errs and len(done) == 2
    rules = profile(env)["rules"]
    assert rules == {"a/b/safe": old_rule}               # 第一条恢复成原来的规则，第二条整个删掉
    assert all(e.get("forgotten") for e in log_of(env))


def test_forget_rule_leaves_a_rule_that_was_changed_later(env):
    """反例：记忆写下之后规则又被改成别的动作，forget 不能把它删掉。"""
    prefs.answer("a/b/safe", "delete")
    prefs.answer("a/b/safe", "keep")                    # 又改成留着（这是 m2）
    memory.forget("m1")
    assert profile(env)["rules"]["a/b/safe"]["action"] == "keep"


def test_board_hide_rules_and_settings_and_projects_are_logged(env):
    (env.home / "proj2").mkdir()
    (env.home / "keep").mkdir()
    text = "\n".join(['设置: {"保留天数":7,"别碰":["~/keep"],"默认":"safe"}', "别再列: x/y/safe",
                      '台账: {"项目":{"%s":{"状态":"做完了"}}}' % (env.home / "proj2")])
    done, errs = prefs.apply_settings(text, scan_id="scan1")
    assert not errs, errs
    kinds = [(e["kind"], e.get("ref", {}).get("field") or e.get("ref", {}).get("key")) for e in log_of(env)]
    assert ("setting", "retention_days") in kinds and ("setting", "default_choice") in kinds
    assert ("protect", None) in [(k, None) for k, _ in kinds] and ("rule", "x/y/safe") in kinds
    assert ("project", str(env.home / "proj2")) in kinds
    assert all(e.get("scan_id") == "scan1" for e in log_of(env))
    # 全部忘掉后设置都恢复成原来没有的样子
    memory.forget(",".join(e["id"] for e in log_of(env)))
    prof = profile(env)
    assert "retention_days" not in prof and "default_choice" not in prof and not prof.get("protect")
    assert "x/y/safe" not in (prof.get("rules") or {})
    assert prof["projects"] == {} or str(env.home / "proj2") not in prof["projects"]


def test_unchanged_settings_are_not_logged_again(env):
    prefs.apply_settings('设置: {"默认":"safe","保留天数":7}')
    n = len(log_of(env))
    prefs.apply_settings('设置: {"默认":"safe","保留天数":7}\n别再列: x/y/safe')
    new = log_of(env)[n:]
    assert [e["kind"] for e in new] == ["rule"]          # 同样的设置再保存一次不重复记；新规则照记
    prefs.apply_settings("别再列: x/y/safe")
    assert len(log_of(env)) == n + 1                      # 已经是同一条“别再列”规则也不重复


def test_project_status_change_logged_and_forget_restores(env):
    proj = env.home / "projA"
    proj.mkdir()
    prefs.set_project(str(proj), "active")               # 新进台账又是默认“进行中”：不刷屏
    assert log_of(env) == []
    prefs.set_project(str(proj), "done", scan_id="scan1")
    log = log_of(env)
    assert len(log) == 1 and log[0]["kind"] == "project" and log[0]["ref"]["old_status"] == "active" and log[0]["scan_id"] == "scan1"
    memory.forget("m1")
    assert profile(env)["projects"][str(proj)]["status"] == "active"
    # 新进台账就不是进行中：forget 把它整个移出
    proj2 = env.home / "projB"
    proj2.mkdir()
    prefs.set_project(str(proj2), "protected")
    assert log_of(env)[-1]["ref"]["old_status"] is None
    memory.forget(log_of(env)[-1]["id"])
    assert str(proj2) not in profile(env)["projects"]


def test_forget_project_keeps_a_status_changed_later(env):
    proj = env.home / "projA"
    proj.mkdir()
    prefs.set_project(str(proj), "active")
    prefs.set_project(str(proj), "done")
    prefs.set_project(str(proj), "archived")
    memory.forget("m1")                                   # m1 是 active→done，后来已经被改成 archived：不动
    assert profile(env)["projects"][str(proj)]["status"] == "archived"


def test_setup_answers_log_settings_and_forget_restores(env):
    import setup_q
    p = prefs.load()
    p["retention_days"] = 15
    prefs.save(p)
    text = setup_q.SETUP_TITLE + "\n设置版本: 1\n答案: " + json.dumps(
        {"conversations": "library", "worktrees": "whole", "delete_mode": "trash", "tools": {"android": "unused"}},
        ensure_ascii=False)
    done, errs, _ = setup_q.apply_export(text, scan_id="scan1")
    assert not errs, errs
    log = log_of(env)
    by_field = {e["ref"]["field"]: e for e in log if e["kind"] == "setting"}
    assert set(by_field) == {"conversation_policy", "worktree_policy", "delete_mode", "tools"}
    conv = by_field["conversation_policy"]                # 旧对话一题连带改了保留天数：放在 also 里一起恢复
    assert conv["ref"]["old"] is None and conv["ref"]["also"] == {"retention_days": 15}
    assert all(e["scan_id"] == "scan1" for e in log)
    memory.forget(",".join(e["id"] for e in log))
    prof = profile(env)
    assert prof["retention_days"] == 15
    for f in ("conversation_policy", "worktree_policy", "delete_mode", "tools"):
        assert f not in prof, f
    # 再回答一遍同样的答案，才会重新记；答案没变就不记
    setup_q.apply_export(text)
    n = len(log_of(env))
    setup_q.apply_export(text)
    assert len(log_of(env)) == n


def test_full_go_writes_remembered_with_rules_and_settings_then_forget(env, capsys):
    digest, dirs = setup(env)
    memory.pending_add("缓存以后都清", "rule", {"key": "dl/cache/safe", "action": "delete"})
    (env.home / "proj2").mkdir()
    extra = ["确认规则: p1", "别再列: x/y/safe", '设置: {"默认":"safe"}',
             '台账: {"项目":{"%s":{"状态":"做完了"}}}' % (env.home / "proj2")]
    assert go(env, export_text(digest, ["c01", "c02"], ["c02=删"], extra=extra)) == 0
    out = capsys.readouterr().out
    assert "这次我记住了" in out
    rows = json.loads((env.run / "remembered.json").read_text())
    kinds = {r["kind"] for r in rows}
    assert {"rule", "setting", "project"} <= kinds, rows
    assert any("缓存以后都清" in r["text"] for r in rows) and any("默认档位" in r["text"] for r in rows)
    html = (env.run / "report.html").read_text()
    assert "x/y/safe" in html                              # 成果页用的就是这份 remembered.json
    ids = ",".join(r["id"] for r in rows)
    assert memory.main(["forget", "--ids", ids]) == 0
    prof = profile(env)
    assert "dl/cache/safe" not in (prof.get("rules") or {}) and "x/y/safe" not in (prof.get("rules") or {})
    assert "default_choice" not in prof and str(env.home / "proj2") not in prof.get("projects", {})


def test_go_without_governance_skips_govern_check_and_with_it_runs(env):
    digest, dirs = setup(env)
    assert go(env, export_text(digest, ["c01"])) == 0
    assert (env.run / "remembered.json").exists() and not (env.run / "governance-check.json").exists()
    gdir = env.home / "g"
    gdir.mkdir()
    memory.govern_record(str(gdir), "把缓存上限调小", "改回去", 100)
    digest2, _ = (env.seal(), None)
    assert go(env, export_text(digest2, ["c02"], ["c02=删"])) == 0
    chk = json.loads((env.run / "governance-check.json").read_text())
    assert chk and chk[0]["change"] == "把缓存上限调小"


# ---------------------------------------------------------------- 5. 成果页“忘掉”往返

def test_report_forget_button_text_roundtrips_into_memory_forget(env, tmp_path):
    """读 report.html 里生成“忘掉: m1,m2”的那一行代码，用它真的拼出文本，再交给 memory.py forget --from-text。"""
    html = (Path(__file__).resolve().parents[1] / "assets" / "report.html").read_text(encoding="utf-8")
    m = re.search(r"\$\('forget-line'\)\.textContent=('忘掉: '\+ids\.join\('[^']*'\))", html)
    assert m, "成果页里生成“忘掉:”文本的代码变了，这个往返测试要跟着改"
    expr = m.group(1)
    ids = ["m1", "m3"]
    if shutil.which("node"):
        text = subprocess.run(["node", "-e", "const ids=%s;process.stdout.write(%s)" % (json.dumps(ids), expr)],
                              capture_output=True, text=True, timeout=30, check=True).stdout
    else:
        text = "忘掉: " + ",".join(ids)
    assert text == "忘掉: m1,m3"
    prefs.answer("a/b/safe", "delete")
    prefs.answer("c/d/safe", "keep")
    prefs.answer("e/f/safe", "delete")
    f = tmp_path / "forget.txt"
    f.write_text("你好，请处理这个\n" + text + "\n谢谢\n", encoding="utf-8")
    assert memory.parse_forget_text(f.read_text(encoding="utf-8")) == ids
    assert memory.main(["forget", "--from-text", str(f)]) == 0
    assert set(profile(env)["rules"]) == {"c/d/safe"}      # m1、m3 的规则撤了，m2 还在
    # 反例：没有“忘掉:”行就报错、什么都不忘
    g = tmp_path / "none.txt"
    g.write_text("随便一句话", encoding="utf-8")
    assert memory.main(["forget", "--from-text", str(g)]) == 2
    assert set(profile(env)["rules"]) == {"c/d/safe"}


def test_skip_setup_with_empty_answers_marks_onboarded(tmp_path, monkeypatch):
    """“跳过设置”：只含空答案的导出也算完成第一次设置，退出码 0。"""
    import json as _json
    import plan, protect, setup_q
    prof = tmp_path / "profile.json"
    monkeypatch.setattr(protect, "PROFILE", str(prof))
    f = tmp_path / "skip.txt"
    f.write_text(setup_q.SETUP_TITLE + "\n设置版本: 1\n答案: {}\n", encoding="utf-8")
    assert plan.main(["profile", "--apply", str(f)]) == 0
    p = _json.loads(prof.read_text(encoding="utf-8"))
    assert p.get("onboarded_at") and p["setup"]["version"] == setup_q.SETUP_VERSION


def test_done_evidence_logs_doc_reads_and_skips_never(tmp_path, monkeypatch):
    import json as _json
    import scan
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "README.md").write_text("第一季已上线，完成交付\n", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setattr(scan, "_peek_refused", lambda fp: False)
    import argparse
    out = scan.probe_done_evidence(argparse.Namespace(path=str(proj), run=str(run)))
    assert out["doc_hits"]
    rows = [_json.loads(l) for l in (run / "reads.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and rows[0]["mode"] == "content" and rows[0]["path"].endswith("README.md")
    # 反例：读取闸门说不能读，就不读也不记
    (run / "reads.jsonl").unlink()
    monkeypatch.setattr(scan, "_peek_refused", lambda fp: True)
    out = scan.probe_done_evidence(argparse.Namespace(path=str(proj), run=str(run)))
    assert out["doc_hits"] == [] and not (run / "reads.jsonl").exists()
