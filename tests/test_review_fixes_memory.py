"""0.6 发版前评审：第 5、8、9、10 条的回归测试（记忆撤销、安装包策略、待确认释义）。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import memory  # noqa: E402
import plan  # noqa: E402
import prefs  # noqa: E402
import protect  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = (tmp_path / "home").resolve()
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(protect, "HOME", str(h))
    monkeypatch.setattr(protect, "STATE_DIR", str(h / ".sansheng" / "disk-cleanup"))
    monkeypatch.setattr(protect, "PROFILE", str(h / ".sansheng" / "disk-cleanup" / "profile.json"))
    return h


# ---- 第 5 条：确认“别动 A”，A 原本就在保护名单里，忘掉不能把原有保护移除
def test_forget_pending_protect_keeps_preexisting_protection(home):
    (home / "Projects" / "A").mkdir(parents=True)
    (home / "Projects" / "B").mkdir(parents=True)
    p = prefs.load(); p["protect"] = ["~/Projects/A"]; prefs.save(p)
    memory.pending_add("别动 A", "protect", "~/Projects/A")
    memory.confirm(["p1"])
    # 本次没有真正新增：不写可撤销的记忆
    assert not [e for e in prefs.load().get("memory_log", []) if e.get("kind") == "protect"]
    assert "~/Projects/A" in prefs.load()["protect"]


def test_forget_pending_protect_only_removes_newly_added(home):
    for n in "AB":
        (home / "Projects" / n).mkdir(parents=True)
    p = prefs.load(); p["protect"] = ["~/Projects/A"]; prefs.save(p)
    memory.pending_add("别动 A 和 B", "protect", ["~/Projects/A", "~/Projects/B"])
    memory.confirm(["p1"])
    log = prefs.load()["memory_log"][-1]
    assert log["ref"]["paths"] == ["~/Projects/B"]
    memory.forget([log["id"]])
    assert prefs.load()["protect"] == ["~/Projects/A"]


# ---- 第 10 条：撤销设置前核对当前值仍是自己写的
def test_forget_setting_rolls_back_when_value_is_still_mine(home):
    prefs.apply_settings("设置: 默认=safe")
    mid = prefs.load()["memory_log"][-1]["id"]
    assert prefs.load()["memory_log"][-1]["ref"]["new"] == "safe"
    done, errs = memory.forget([mid])
    assert not errs and "default_choice" not in prefs.load()


def test_forget_setting_does_not_roll_back_when_changed_later(home):
    prefs.apply_settings("设置: 默认=safe")
    mid = prefs.load()["memory_log"][-1]["id"]
    prefs.apply_settings("设置: 默认=all")                    # 之后又改了
    mid2 = prefs.load()["memory_log"][-1]["id"]
    done, errs = memory.forget([mid])
    assert prefs.load()["default_choice"] == "all"           # 没被回滚到 old
    assert any("之后改过，没动" in d for d in done)
    assert prefs.load()["memory_log"][0]["forgotten"] is True
    memory.forget([mid2])                                    # 最新那条仍能撤销
    assert prefs.load().get("default_choice") == "safe"


def test_forget_legacy_setting_entry_without_new_is_conservative(home):
    p = prefs.load()
    p["default_choice"] = "all"
    p["memory_log"] = [{"id": "m1", "kind": "setting", "text": "旧", "at": "2026-01-01T00:00:00Z",
                        "ref": {"field": "default_choice", "old": "safe"}}]
    prefs.save(p)
    done, errs = memory.forget(["m1"])
    assert prefs.load()["default_choice"] == "all"
    assert "没动" in done[0]


def test_forget_setting_also_fields_checked_individually(home):
    (home / "Docs").mkdir()
    prefs.apply_settings('设置: {"成品":["~/Docs"]}')
    log = prefs.load()["memory_log"][-1]
    assert log["ref"]["also_new"]["reference_roots"] == prefs.load()["reference_roots"]
    p = prefs.load(); p["reference_roots"] = ["~/Docs", "~/Other"]; prefs.save(p)   # 引用目录之后被别处改过
    memory.forget([log["id"]])
    p = prefs.load()
    assert p["reference_roots"] == ["~/Docs", "~/Other"]      # 这个字段没动
    assert not p.get("deliverable_roots")                     # 另一个字段仍恢复


# ---- 第 8 条：installer_policy 只认真安装包，且整卡全是
def _it(path, **kw):
    d = {"path": path, "method": "delete", "scope": "all"}
    d.update(kw)
    return d


@pytest.mark.parametrize("name", ["a.dmg", "A.PKG", "b.mpkg", "c.xip", "d.iso"])
def test_installer_card_all_installers(name):
    assert plan._installer_card({"cards_used": ["browsers"]}, [_it("/x/" + name)]) is True


def test_installer_card_rejects_zip_and_mixed_cards():
    assert plan._installer_card({}, [_it("/x/data.zip")]) is False
    assert plan._installer_card({}, [_it("/x/a.dmg"), _it("/x/report.pdf")]) is False
    assert plan._installer_card({}, [_it("/x/a.dmg"), _it("/x/src.zip")]) is False
    assert plan._installer_card({}, [_it("/x/dl", files=["/x/dl/a.dmg", "/x/dl/notes.docx"])]) is False
    assert plan._installer_card({"cards_used": ["app-updaters-installers"]}, [_it("/x/Cache")]) is False   # 不看卡片 id
    assert plan._installer_card({}, []) is False
    assert plan._installer_card({}, [_it("/x/dl", files=["/x/dl/a.dmg", "/x/dl/b.pkg"])]) is True


# ---- 第 9 条：pending_rules 带 means
def test_pending_rules_have_means_from_value(home):
    (home / "a").mkdir(); (home / "b").mkdir()
    memory.pending_add("别动 a 和 b 的东西", "protect", ["~/a", "~/b"])
    memory.pending_add("图片都清", "rule", {"key": "ai-images/codex/recommended", "action": "delete"})
    memory.pending_add("这里是旧稿", "note", {"path": "~/a"})
    rows = plan.pending_rules_of(prefs.load())
    by = {r["id"]: r for r in rows}
    assert all(r["text"] and r["means"] for r in rows)
    assert "以后永不触碰：~/a、~/b" == by["p1"]["means"]
    assert "ai-images/codex/recommended" in by["p2"]["means"] and "都清" in by["p2"]["means"]
    assert "~/a" in by["p3"]["means"] and "备注" in by["p3"]["means"]
    assert plan.pending_means("rule", {"key": "k", "action": "keep"}).endswith("都留着、不再列出")
