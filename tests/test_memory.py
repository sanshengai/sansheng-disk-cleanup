"""记忆：认下的目录、从源头改好的、待确认的话，以及“忘掉”真的把改动撤销。夹具全在 tmp_path 内。"""
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import memory  # noqa: E402
import prefs  # noqa: E402
import protect  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    (home / "Downloads" / "课程").mkdir(parents=True)
    (home / "Downloads" / "别的").mkdir(parents=True)
    state = home / ".sansheng" / "disk-cleanup"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(protect, "HOME", str(home))
    monkeypatch.setattr(protect, "STATE_DIR", str(state))
    monkeypatch.setattr(protect, "PROFILE", str(state / "profile.json"))
    run = (tmp_path / "runs" / "scan1").resolve()
    run.mkdir(parents=True)
    return SimpleNamespace(home=home, state=state, run=run, tmp=tmp_path)


def profile(env):
    p = env.state / "profile.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def log(env):
    return profile(env).get("memory_log") or []


def facts(env, nodes, scan_id="scan1"):
    (env.run / "facts.json").write_text(json.dumps(
        {"schema": "facts/1", "scan_id": scan_id, "nodes": nodes}), encoding="utf-8")


def claude_dir(env, monkeypatch, name="sess-abcdef"):
    monkeypatch.setattr(protect, "TMP_BASE", str(env.tmp / "tmpbase"))
    d = Path(protect.claude_tmp_root()) / name
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------- identity

def test_identity_set_list_forget(env):
    p = env.home / "Downloads" / "课程"
    done, errs = memory.identity_set(str(p), "课程视频", producer="B站", task="复习", scan_id="scan1")
    assert not errs and done
    rec = profile(env)["identities"][str(p)]
    assert rec["title"] == "课程视频" and rec["producer"] == "B站" and rec["task"] == "复习"
    e = log(env)[0]
    assert e["id"] == "m1" and e["kind"] == "identity" and e["ref"]["path"] == str(p) and e["scan_id"] == "scan1"
    assert e["text"].startswith("~/Downloads/课程：课程视频")            # 给人看的文字用 ~
    lines, errs = memory.identity_list()
    assert not errs and any("课程视频" in x for x in lines)
    done, errs = memory.identity_forget(str(p))
    assert not errs and done
    assert str(p) not in (profile(env).get("identities") or {})
    assert log(env)[0]["forgotten"] is True          # 日志留着，只标记忘掉


def test_identity_set_rejects_bad_paths(env):
    for bad in ("/etc", str(env.home), str(env.home / "Downloads" / "课程" / "不存在")):
        done, errs = memory.identity_set(bad, "x")
        assert errs and not done, bad
    assert not profile(env).get("identities")


def test_identity_set_rejects_outside_home(env):
    other = env.tmp / "外面"
    other.mkdir()
    done, errs = memory.identity_set(str(other), "x")
    assert errs and not done


def test_identity_set_allows_claude_tmp(env, monkeypatch):
    d = claude_dir(env, monkeypatch)
    done, errs = memory.identity_set(str(d), "会话临时目录")
    assert not errs and done
    assert str(d) in profile(env)["identities"]


def test_identity_forget_unknown(env):
    done, errs = memory.identity_forget(str(env.home / "Downloads" / "课程"))
    assert errs and not done


# ---------------------------------------------------------------- pending / confirm / reject

def test_pending_confirm_all_kinds(env):
    assert memory.pending_add("下载里的视频都是课程，别动", "protect", ["~/Downloads/课程"])[1] == []
    assert memory.pending_add("这一类以后都清", "rule", {"key": "ai-images/codex/recommended", "action": "delete"})[1] == []
    assert memory.pending_add("这门课看到第 3 讲", "note", {"path": "~/Downloads/课程"})[1] == []
    lines, errs = memory.pending_list()
    assert not errs and [x.split("［")[0] for x in lines] == ["p1", "p2", "p3"]

    done, errs = memory.confirm(["p1", "p2", "p3"])
    assert not errs and len(done) == 3
    p = profile(env)
    assert "~/Downloads/课程" in p["protect"]
    assert p["rules"]["ai-images/codex/recommended"]["action"] == "delete"
    assert p["notes"]["~/Downloads/课程"] == "这门课看到第 3 讲"
    assert p["pending_rules"] == []
    kinds = [e["kind"] for e in log(env)]
    assert kinds == ["protect", "rule", "note"]
    assert prefs.rule_for(p, "ai-images/codex/recommended") == "delete"


def test_pending_add_rejects_bad_values(env):
    assert memory.pending_add("x", "protect", ["/etc"])[1]
    assert memory.pending_add("x", "protect", [str(env.tmp / "外面")])[1]
    assert memory.pending_add("x", "rule", {"key": "不合法键", "action": "delete"})[1]
    assert memory.pending_add("x", "rule", {"key": "a/b", "action": "乱来"})[1]
    assert memory.pending_add("x", "note", {"path": "/etc"})[1]
    assert memory.pending_add("", "note", {"path": "~/Downloads/课程"})[1]
    assert not profile(env).get("pending_rules")


def test_confirm_keeps_unapplied_pending(env):
    memory.pending_add("占位", "protect", ["~/Downloads/课程"])
    # 把目录删掉，确认时校验不过 → 这条不写、也不移除
    shutil.rmtree(env.home / "Downloads" / "课程")
    done, errs = memory.confirm("p1")
    assert errs and not done
    assert [x["id"] for x in profile(env)["pending_rules"]] == ["p1"]
    assert not profile(env).get("protect")


def test_confirm_unknown_id(env):
    done, errs = memory.confirm("p9")
    assert errs and "p9" in errs[0]


def test_pending_reject(env):
    memory.pending_add("下载里的视频都是课程，别动", "protect", ["~/Downloads/课程"])
    done, errs = memory.reject("p1")
    assert not errs and done
    assert profile(env)["pending_rules"] == [] and not log(env)
    done, errs = memory.reject("p9")
    assert errs and not done


# ---------------------------------------------------------------- forget

def test_forget_really_undoes(env):
    p = env.home / "Downloads" / "课程"
    memory.identity_set(str(p), "课程视频")
    memory.pending_add("别动", "protect", ["~/Downloads/课程"])
    memory.pending_add("以后都清", "rule", {"key": "ai-images/codex/recommended", "action": "delete"})
    memory.pending_add("看到第 3 讲", "note", {"path": "~/Downloads/课程"})
    memory.confirm(["p1", "p2", "p3"])
    m = [e["id"] for e in log(env)]

    done, errs = memory.forget(m)
    assert not errs and len(done) == len(m)
    prof = profile(env)
    assert prof.get("identities") in ({}, None)
    assert "~/Downloads/课程" not in (prof.get("protect") or [])
    assert "ai-images/codex/recommended" not in (prof.get("rules") or {})
    assert prefs.rule_for(prof, "ai-images/codex/recommended") is None
    assert "~/Downloads/课程" not in (prof.get("notes") or {})
    assert all(e["forgotten"] for e in log(env)) and len(log(env)) == len(m)


def test_forget_unknown_id(env):
    done, errs = memory.forget("m9")
    assert errs and "m9" in errs[0]


def test_govern_forget_removes_record(env):
    p = env.home / "Downloads" / "课程"
    memory.govern_record(str(p), "把缓存目录改到别处", "搬回来即可", 5000)
    assert str(p) in profile(env)["governance"]
    done, errs = memory.forget("m1")
    assert not errs and done
    assert not profile(env).get("governance")
    assert log(env)[0]["forgotten"] is True


def test_identity_forget_via_generic_forget(env):
    p = env.home / "Downloads" / "课程"
    memory.identity_set(str(p), "课程视频")
    memory.forget("m1")
    assert str(p) not in (profile(env).get("identities") or {})


# ---------------------------------------------------------------- govern check

def test_govern_check_three_verdicts(env):
    course = env.home / "Downloads" / "课程"
    (course / "subA" / "deep").mkdir(parents=True)
    (course / "subB").mkdir()
    other = env.home / "Downloads" / "别的"
    gone = env.home / "Downloads" / "没扫到"
    gone.mkdir()
    memory.govern_record(str(other), "还在长", "无", 500)              # 同路径：1000 > 500 → growing
    memory.govern_record(str(course), "清空的素材", "搬回来", 1500)    # 只算它之下的最上层节点
    memory.govern_record(str(gone), "记录但这次没扫到", "无", 999)      # 没有同路径也没有子节点 → unknown

    nodes = [
        {"id": "n1", "path": str(other), "bytes": 1000},
        {"id": "n2", "path": str(course / "subA"), "bytes": 1000},
        {"id": "n3", "path": str(course / "subA" / "deep"), "bytes": 400},
        {"id": "n4", "path": str(course / "subB"), "bytes": 500},
    ]
    facts(env, nodes)
    rows = memory.govern_check(str(env.run))
    by = {r["path"]: r for r in rows}
    assert by[str(other)]["verdict"] == "growing" and by[str(other)]["delta"] == 500
    assert by[str(course)]["verdict"] == "stopped" and by[str(course)]["now_bytes"] == 1500   # 不把 deep 重复计
    assert by[str(gone)]["verdict"] == "unknown" and by[str(gone)]["now_bytes"] is None
    written = json.loads((env.run / "governance-check.json").read_text(encoding="utf-8"))
    assert {r["verdict"] for r in written} == {"stopped", "growing", "unknown"}
    assert set(written[0]) == {"path", "change", "baseline_bytes", "now_bytes", "delta", "verdict"}


def test_govern_check_exact_and_negative_delta(env):
    d = env.home / "Downloads" / "课程"
    memory.govern_record(str(d), "腾过一次", "无", 4000)
    facts(env, [{"id": "n1", "path": str(d), "bytes": 1000}])       # 变小 → 也算停下来了
    rows = memory.govern_check(str(env.run))
    assert rows[0]["verdict"] == "stopped" and rows[0]["delta"] == -3000


# ---------------------------------------------------------------- remembered

def test_remembered_only_this_run(env):
    p = env.home / "Downloads" / "课程"
    d = env.home / "Downloads" / "别的"
    memory.identity_set(str(p), "这次的", scan_id="scan1")
    memory.identity_set(str(d), "上次的", scan_id="scan2")
    memory.govern_record(str(p), "这次改的", "无", 10, scan_id="scan1")
    facts(env, [], scan_id="scan1")
    rows = memory.remembered(str(env.run))
    assert sorted(r["text"] for r in rows) == sorted([f"{memory._short(str(p))}：这次的", "这次改的"])
    assert json.loads((env.run / "remembered.json").read_text(encoding="utf-8")) == rows


def test_remembered_excludes_forgotten_and_since(env):
    p = env.home / "Downloads" / "课程"
    d = env.home / "Downloads" / "别的"
    memory.identity_set(str(p), "忘掉这条", scan_id="scan1")
    memory.identity_set(str(d), "按时间算进来", scan_id="scan2")
    memory.forget("m1")
    facts(env, [], scan_id="scan1")
    rows = memory.remembered(str(env.run), since="2000-01-01T00:00:00Z")
    assert [r["text"] for r in rows] == [f"{memory._short(str(d))}：按时间算进来"]


def test_remembered_without_facts(env):
    memory.identity_set(str(env.home / "Downloads" / "课程"), "没人认领", scan_id="scan1")
    (env.run / "facts.json").unlink(missing_ok=True)
    rows = memory.remembered(str(env.run))
    assert rows == []


# ---------------------------------------------------------------- CLI

def test_cli_exit_codes(env, capsys):
    assert memory.main(["identity", "set", "--path", "/etc", "--title", "x"]) == 2
    assert memory.main(["forget", "--ids", "m9"]) == 2
    assert memory.main(["forget"]) == 2
    assert memory.main(["pending", "list"]) == 0
    assert memory.main(["identity", "set", "--path", "~/Downloads/课程", "--title", "课程视频",
                        "--scan-id", "scan1"]) == 0
    out = capsys.readouterr().out
    assert "已记住" in out
    assert memory.main(["identity", "list"]) == 0
    assert memory.main(["forget", "--ids", "m1"]) == 0


def test_cli_forget_from_text(env, tmp_path, capsys):
    memory.identity_set(str(env.home / "Downloads" / "课程"), "课程视频")
    memory.identity_set(str(env.home / "Downloads" / "别的"), "别的")
    txt = tmp_path / "忘掉.txt"
    txt.write_text("忘掉: m1, m2\n", encoding="utf-8")
    assert memory.main(["forget", "--from-text", str(txt)]) == 0
    assert not profile(env).get("identities")
    assert all(e["forgotten"] for e in log(env))


def test_cli_pending_confirm_roundtrip(env, capsys):
    assert memory.main(["pending", "add", "--text", "别动下载里的视频", "--kind", "protect",
                        "--value", '["~/Downloads/课程"]']) == 0
    assert memory.main(["pending", "add", "--text", "以后都清", "--kind", "rule",
                        "--value", '{"key":"ai-images/codex/recommended","action":"keep"}']) == 0
    done, errs = memory.confirm(["p1", "p2"])
    assert not errs
    assert "~/Downloads/课程" in profile(env)["protect"]
    assert profile(env)["rules"]["ai-images/codex/recommended"]["action"] == "keep"
