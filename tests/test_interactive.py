"""0.3：条目 class（interactive/batch/unknown）与卡片 default_select 的闸门测试。

夹具沿用 test_plan_act 的 Env（HOME 与 profile 都在 tmp_path 内）。
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import (TID, TID2, Env, age, card, codex_item, delete_item, mkfile, node,  # noqa: E402
                           plan, rollout)

pytestmark = pytest.mark.filterwarnings("ignore")

DASH = Path(__file__).resolve().parent.parent / "assets" / "dashboard.html"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def prod(card_name="browsers", default_select=True):
    return {"tool": "t", "card": card_name, "hint": "h", "safety": "safe", "default_select": default_select}


def cache_dir(env, name="cache"):
    d = env.work / name
    mkfile(d / "a.bin")
    age(d)
    return d


def check_one(env, nodes, c):
    env.write(nodes, [c])
    return env.check()["cards"][0]


# ---------------------------------------------------------------- class

def cx(path, tid, cls=None, session=None):
    it = codex_item(path, tid)
    if cls is not None:
        it["class"] = cls
    if session is not None:
        it["session"] = session
    return it


def codex_env(env):
    a, b = rollout(env, TID), rollout(env, TID2)
    age(env.home)
    return a, b, node(env.home / ".codex")


def test_clean_card_with_interactive_item_is_downgraded_to_decide(env):
    a, _, base = codex_env(env)
    out = check_one(env, [base], card("c01", [cx(a, TID, "interactive", TID)]))
    assert out["verdict_in"] == "clean" and out["verdict_out"] == "decide" and out["downgraded"] is True
    assert any("interactive" in e for e in out["errors"])
    assert "续聊" in out["question"]
    assert out["default_select"] is False
    # 降级后仍可封存为 decide（用户确认后才执行），不是被拒
    assert env.seal() and json.loads((env.run / "seal.json").read_text())["cards"]["c01"]["verdict"] == "decide"


def test_two_interactive_sessions_on_one_card_is_rejected(env):
    a, b, base = codex_env(env)
    out = check_one(env, [base], card("c01", [cx(a, TID, "interactive", TID), cx(b, TID2, "interactive", TID2)]))
    assert out["verdict_out"] == "keep" and out["downgraded"] is True
    assert any("最多一个交互会话" in e for e in out["errors"])
    env.seal()
    assert "c01" not in json.loads((env.run / "seal.json").read_text())["cards"]     # 被拒的卡不可封存


def test_decide_card_with_two_interactive_sessions_is_also_rejected(env):
    a, b, base = codex_env(env)
    c = card("c01", [cx(a, TID, "interactive", TID), cx(b, TID2, "interactive", TID2)],
             verdict="decide", question="删吗？")
    assert check_one(env, [base], c)["verdict_out"] == "keep"


def test_one_session_with_descendants_counts_once(env):
    child = rollout(env, TID2, parent=TID)
    parent = rollout(env, TID)
    age(env.home)
    items = [cx(child, TID2, "interactive", TID), cx(parent, TID, "interactive", TID)]
    out = check_one(env, [node(env.home / ".codex")], card("c01", items))
    assert not any("最多一个交互会话" in e for e in out["errors"])
    assert out["verdict_out"] == "decide"          # 仍是交互会话：降级，但不是被拒


def test_missing_or_unknown_class_on_session_is_interactive_and_handwritten_batch_conflict_is_overridden(env):
    """P5.R M2：缺 class / unknown 的会话按路径与命令推断为 interactive；手写 batch 与推断冲突、又没有探针 emit 来源时按 interactive 并给 warning。
    来自探针 items_file 的 batch 仍信（见 test_r5_plan.py）。"""
    a, b, base = codex_env(env)
    batch = check_one(env, [base], card("c01", [cx(a, TID, "batch", TID)]))
    assert batch["verdict_out"] == "decide" and any("手写 class=batch" in w for w in batch["warnings"])
    old = check_one(env, [base], card("c01", [cx(a, TID), cx(b, TID2)]))
    assert old["verdict_out"] == "keep" and any("最多一个交互会话" in e for e in old["errors"])
    unk = check_one(env, [base], card("c01", [cx(a, TID, "unknown")]))
    assert unk["verdict_out"] == "decide"


def test_invalid_class_value_is_rejected(env):
    a, _, base = codex_env(env)
    out = check_one(env, [base], card("c01", [cx(a, TID, "Interactive")]))
    assert out["verdict_out"] == "keep" and any("class 必须是" in e for e in out["errors"])


def test_interactive_without_session_each_counts(env):
    a, b, base = codex_env(env)
    out = check_one(env, [base], card("c01", [cx(a, TID, "interactive"), cx(b, TID2, "interactive")]))
    assert out["verdict_out"] == "keep"            # 缺 session 宁可多计，不漏计


# ---------------------------------------------------------------- default_select

def test_default_select_true_for_pure_cache_clean(env):
    d = cache_dir(env)
    out = check_one(env, [node(d, producer=prod())], card("c01", [delete_item(d)]))
    assert out["verdict_out"] == "clean" and out["default_select"] is True


def test_default_select_true_for_native_cache_command(env):
    d = env.home / ".npm"
    mkfile(d / "a.bin")
    age(d)
    out = check_one(env, [node(d)], card("c01", [{"path": str(d), "method": "native", "argv": ["npm", "cache", "clean"]}]))
    assert out["verdict_out"] == "clean" and out["default_select"] is True       # 节点没写 default_select 也成立


def test_default_select_false_when_producer_does_not_opt_in(env):
    d = cache_dir(env)
    assert check_one(env, [node(d, producer=prod(default_select=False))], card("c01", [delete_item(d)]))["default_select"] is False
    assert check_one(env, [node(d)], card("c01", [delete_item(d)]))["default_select"] is False    # 节点无 default_select


def test_default_select_false_for_interactive_session(env):
    a, _, base = codex_env(env)
    base["producer"] = prod("codex")
    out = check_one(env, [base], card("c01", [cx(a, TID, "interactive", TID)]))
    assert out["default_select"] is False


def test_default_select_false_for_installers(env):
    f = mkfile(env.work / "Setup.dmg")
    age(env.work)
    out = check_one(env, [node(f, producer=prod())], card("c01", [delete_item(f)]))
    assert out["verdict_out"] == "clean" and out["default_select"] is False
    z = mkfile(env.work / "dl" / "x.bin")
    age(env.work)
    out = check_one(env, [node(env.work / "dl", producer=prod())],
                    card("c01", [delete_item(env.work / "dl", scope="files", files=[str(z)])]))
    assert out["default_select"] is True                                   # 对照：同形但不是安装包
    zz = mkfile(env.work / "dl2" / "pkg.zip")
    age(env.work)
    out = check_one(env, [node(env.work / "dl2", producer=prod())],
                    card("c01", [delete_item(env.work / "dl2", scope="files", files=[str(zz)])]))
    assert out["verdict_out"] == "clean" and out["default_select"] is False


def test_default_select_false_for_updater_staging_unless_superseded(env):
    env.mp.setattr(plan, "updater_verdict_for", lambda rp, ctx: "superseded")
    d = cache_dir(env, "upd")
    nodes = [node(d, producer=prod("app-updaters-installers"))]
    assert check_one(env, nodes, card("c01", [delete_item(d)]))["default_select"] is False
    c = card("c01", [delete_item(d)])
    c["updater_verdict"] = "superseded"
    assert check_one(env, nodes, c)["default_select"] is True


def test_default_select_false_for_archives(env):
    d = env.work / "Library" / "Developer" / "Xcode" / "Archives"
    mkfile(d / "a.xcarchive" / "x.bin")
    age(env.work)
    out = check_one(env, [node(d, producer=prod("xcode"))], card("c01", [delete_item(d)]))
    assert out["verdict_out"] == "clean" and out["default_select"] is False


def test_default_select_false_for_model_caches(env):
    d = cache_dir(env, "models")
    out = check_one(env, [node(d, producer=prod("model-caches"))], card("c01", [delete_item(d)]))
    assert out["verdict_out"] == "clean" and out["default_select"] is False


def test_default_select_false_for_decide_and_keep_cards(env):
    d = cache_dir(env)
    nodes = [node(d, producer=prod())]
    dec = check_one(env, nodes, card("c01", [delete_item(d)], verdict="decide", question="删吗？"))
    assert dec["verdict_out"] == "decide" and dec["default_select"] is False
    keep = check_one(env, nodes, card("c01", [], verdict="keep"))
    assert keep["default_select"] is False


def test_user_written_default_select_is_ignored_with_warning(env):
    d = cache_dir(env)
    nodes = [node(d, producer=prod(default_select=False))]
    c = card("c01", [delete_item(d)])
    c["default_select"] = True
    env.write(nodes, [c])
    res = env.check()
    assert res["cards"][0]["default_select"] is False
    assert any("default_select" in w["message"] for w in res["warnings"])
    # 反方向：用户写 false 压不住计算出的 true
    c2 = card("c01", [delete_item(d)])
    c2["default_select"] = False
    env.write([node(d, producer=prod())], [c2])
    assert env.check()["cards"][0]["default_select"] is True


def test_board_data_carries_computed_default_select_not_user_value(env):
    d = cache_dir(env)
    c = card("c01", [delete_item(d)])
    c["default_select"] = True                                 # 用户手写，不作数
    env.write([node(d, producer=prod(default_select=False))], [c])
    result = env.check()
    p, _ = plan.load_plan(str(env.run))
    assert plan.board_plan(p, result)["cards"][0]["default_select"] is False


def test_dashboard_preselects_only_default_select_clean_cards():
    html = DASH.read_text(encoding="utf-8")
    assert "c.verdict==='clean'&&c.default_select===true" in html


@pytest.mark.parametrize("probe_says", ["unknown", "pending_install", "not_found", "probe_error"])
def test_updater_staging_clean_needs_probe_superseded(env, probe_says):
    """卡上手写 updater_verdict=superseded 不作数：plan check 现场跑 probe updaters，结论不是 superseded 就降为 decide。"""
    env.mp.setattr(plan, "updater_verdict_for", lambda rp, ctx: probe_says)
    d = cache_dir(env, "upd")
    c = card("c01", [delete_item(d)])
    c["updater_verdict"] = "superseded"
    out = check_one(env, [node(d, producer=prod("app-updaters-installers"))], c)
    assert out["verdict_out"] == "decide" and out["default_select"] is False
    assert any(probe_says in e for e in out["errors"])
    assert out["judgment_refs"] and out["judgment_refs"][0]["section"]


def test_updater_verdict_for_matches_overlapping_candidates(monkeypatch, tmp_path):
    import updaters
    a, b = tmp_path / "ShipIt" / "u1", tmp_path / "Other" / "u2"
    monkeypatch.setattr(updaters, "probe_updaters", lambda opts: {"items": [
        {"path": str(a), "verdict": "superseded"}, {"path": str(b), "verdict": "unknown"}]})
    ctx = {}
    assert plan.updater_verdict_for(os.path.realpath(str(a)), ctx) == "superseded"
    assert plan.updater_verdict_for(os.path.realpath(str(tmp_path / "ShipIt")), ctx) == "superseded"
    assert plan.updater_verdict_for(os.path.realpath(str(tmp_path)), ctx) == "unknown"     # 覆盖两个候选，一个不是 superseded
    assert plan.updater_verdict_for(os.path.realpath(str(tmp_path / "none")), ctx) == "not_found"
    monkeypatch.setattr(updaters, "probe_updaters", lambda opts: 1 / 0)
    assert plan.updater_verdict_for("/x", {}) == "probe_error"
