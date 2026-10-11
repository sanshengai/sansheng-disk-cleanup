"""对话确认：plan.py propose 生成三档提案，act.py go 按用户回复的一档连跑 预演 → 试点 → 正式执行。

夹具全在 tmp_path 内（沿用 test_plan_act 的 Env）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, age, card, delete_item, mkfile, node, plan  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def setup(env, aged=True):
    """c01 clean（稳妥）、c02 decide（推荐）、c03 decide 且标 all（慎重）。"""
    dirs, nodes, cards = {}, [], []
    spec = [("c01", "clean", None), ("c02", "decide", None), ("c03", "decide", "all")]
    for i, (cid, verdict, preset) in enumerate(spec, 1):
        d = env.work / f"d{i}"
        mkfile(d / "a.bin")
        if aged:
            age(d)
        dirs[cid] = d
        nodes.append(node(d, id=f"n{i}"))
        c = card(cid, [delete_item(d)], verdict=verdict, question="删吗？" if verdict == "decide" else None)
        if preset:
            c["preset"] = preset
        cards.append(c)
    env.write(nodes, cards)
    digest = env.seal()
    return digest, dirs


def onboard(env, **extra):
    p = Path(env.home / ".sansheng" / "disk-cleanup" / "profile.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"onboarded_at": "2026-10-11T00:00:00Z", **extra}), encoding="utf-8")


def propose(env):
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    return json.loads((env.run / "proposal.json").read_text())


def test_propose_tiers_and_sel(env):
    digest, _ = setup(env)
    p = propose(env)
    o = p["options"]
    assert o["safe"]["cards"] == ["c01"]
    assert o["recommended"]["cards"] == ["c01", "c02"] and o["recommended"]["answers"] == ["c02=删"]
    assert o["all"]["cards"] == ["c01", "c02", "c03"]
    for v in o.values():                       # 与看板 / act.py run 用同一个指纹函数
        assert v["sel"] == plan.selection_sig(digest, v["cards"], v["answers"])
    assert "好" in p["message"] and "都删" in p["message"]


def test_propose_board_vs_chat(env):
    setup(env)
    assert propose(env)["mode"] == "board"            # 没有 onboarded_at：第一次使用发看板
    onboard(env)
    p = propose(env)
    assert p["mode"] == "chat" and p["board_reasons"] == []
    env.mp.setattr(plan, "BOARD_ATTENTION_MAX", 1)    # 待你看 2 项 > 1：发看板
    assert propose(env)["mode"] == "board"


def test_go_recommended_runs_all_three_steps(env):
    _, dirs = setup(env)
    onboard(env)                                      # 第一次使用阶段 go --choice 一律不收（见 test_go_choice_refused_*）
    propose(env)
    assert act.main(["go", "--run", str(env.run), "--choice", "recommended"]) == 0
    assert not dirs["c01"].exists() and not dirs["c02"].exists()
    assert (dirs["c03"] / "a.bin").exists()           # 慎重档没选，不动
    assert list(env.run.glob("receipt-dryrun-*.json")) and list(env.run.glob("receipt-pilot-*.json"))
    assert (env.run / "receipt.json").exists()


def test_go_stops_when_nothing_executable(env):
    _, dirs = setup(env, aged=False)                  # 都是 24 小时内改过的：预演里全被跳过
    onboard(env)                                      # 第一次使用阶段 go --choice 一律不收（见 test_go_choice_refused_*）
    propose(env)
    assert act.main(["go", "--run", str(env.run), "--choice", "safe"]) == 3
    assert (dirs["c01"] / "a.bin").exists()
    assert not list(env.run.glob("receipt-pilot-*.json")) and not (env.run / "receipt.json").exists()


def test_go_rejects_tampered_proposal(env):
    _, dirs = setup(env)
    onboard(env)                                      # 第一次使用阶段 go --choice 一律不收（见 test_go_choice_refused_*）
    p = propose(env)
    p["options"]["safe"]["cards"] = ["c01", "c03"]    # 改了卡号没改 sel：一张都不动
    (env.run / "proposal.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    assert act.main(["go", "--run", str(env.run), "--choice", "safe"]) == 2
    assert all((d / "a.bin").exists() for d in dirs.values())


def test_go_needs_proposal_and_nonempty_choice(env):
    _, dirs = setup(env)
    assert act.main(["go", "--run", str(env.run), "--choice", "safe"]) == 2      # 没有 proposal.json
    p = propose(env)
    p["options"]["safe"]["cards"] = []
    (env.run / "proposal.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    assert act.main(["go", "--run", str(env.run), "--choice", "safe"]) == 2
    assert all((d / "a.bin").exists() for d in dirs.values())


def test_go_stops_after_failed_pilot(env):
    _, dirs = setup(env)
    onboard(env)                                      # 第一次使用阶段 go --choice 一律不收（见 test_go_choice_refused_*）
    propose(env)
    env.mp.setattr(act, "pilot_order", lambda seal, selection: [])   # 试点一条都没执行成功
    assert act.main(["go", "--run", str(env.run), "--choice", "recommended"]) == 3
    assert (dirs["c01"] / "a.bin").exists() and not (env.run / "receipt.json").exists()


def test_report_counts_pilot_and_full_and_shares_only_totals(env):
    from urllib.parse import parse_qs, urlparse
    import report
    _, dirs = setup(env)
    onboard(env)                                      # 第一次使用阶段 go --choice 一律不收（见 test_go_choice_refused_*）
    propose(env)
    assert act.main(["go", "--run", str(env.run), "--choice", "recommended"]) == 0
    rp = json.loads((env.run / "report.json").read_text())
    full = json.loads((env.run / "receipt.json").read_text())
    pilot = json.loads((env.run / full["gate"]["pilot_receipt"]).read_text())
    # 试点先删掉的那一条在正式回执里是“已不存在”跳过，成果页要把两份加起来
    want = sum(it["removed"]["bytes"] for rc in (pilot, full) for c in rc["cards"] for it in c["items"]
               if it["status"] in ("removed", "partial"))
    assert want > 0 and rp["freed"] == want
    assert rp["freed"] > sum(it["removed"]["bytes"] for c in full["cards"] for it in c["items"]
                             if it["status"] in ("removed", "partial"))
    assert (env.run / "report.html").exists()
    q = parse_qs(urlparse(rp["share"]["url"]).query)
    assert set(q) == {"v", "f", "b", "a", "ai"}                     # 分享只带汇总数字
    assert str(env.home) not in rp["share"]["url"]
    n = len(rp["share"]["qr"])
    assert n >= 21 and all(len(r) == n and set(r) <= {"0", "1"} for r in rp["share"]["qr"])
    assert report.qr_rows(rp["share"]["url"]) == rp["share"]["qr"]


def test_go_partial_failure_returns_4(env, capsys):
    """0.4 评审 8：正式执行有条目没删成功时，不能报“完成”、退出码 0。"""
    import os
    _, dirs = setup(env)
    for k in ("c01", "c02"):
        mkfile(dirs[k] / "sub" / "b.bin")
        age(dirs[k])
    real_run, calls, locked = act.cmd_run, [], []

    def run_then_lock(ns):                            # 预演、试点照常；正式执行前才锁住剩下那张卡，模拟执行中途删不掉
        calls.append(1)
        if len(calls) == 3:
            sub = next(dirs[k] / "sub" for k in ("c01", "c02") if (dirs[k] / "sub").exists())
            os.chmod(sub, 0o500)
            locked.append(sub)
        return real_run(ns)
    env.mp.setattr(act, "cmd_run", run_then_lock)
    onboard(env)
    try:
        propose(env)
        rc = act.main(["go", "--run", str(env.run), "--choice", "recommended"])
        err = capsys.readouterr().err
    finally:
        for sub in locked:
            os.chmod(sub, 0o700)
    assert locked and rc == 4 and "没删成功" in err


def test_interactive_session_card_forces_board(env):
    """0.4 评审 2：含交互会话的卡不在对话里一句话带过，发看板逐个确认。"""
    d = env.work / "sess"
    mkfile(d / "a.jsonl")
    age(d)
    it = delete_item(d)
    it["class"] = "interactive"
    env.write([node(d, id="n1")], [card("c01", [it], verdict="decide", question="这段对话还续吗？")])
    env.seal()
    onboard(env)
    p = propose(env)
    assert p["mode"] == "board" and any("交互对话" in r for r in p["board_reasons"])


# ---------------------------------------------------------------- 发布前评审 中2：--choice 只收对话确认方式的提案

def test_go_choice_refused_when_proposal_is_board_mode(env, capsys):
    _, dirs = setup(env)
    onboard(env)
    env.mp.setattr(plan, "BOARD_ATTENTION_MAX", 1)     # 待你看 2 项 > 1：这份提案要发看板
    p = propose(env)
    assert p["mode"] == "board"
    assert act.main(["go", "--run", str(env.run), "--choice", "safe"]) == 2
    err = capsys.readouterr().err
    assert "不是对话确认方式" in err and "--from-board" in err
    assert all((d / "a.bin").exists() for d in dirs.values())
    assert not list(env.run.glob("receipt*.json"))


def test_go_choice_refused_in_onboarding_stage(env, capsys):
    _, dirs = setup(env)
    p = propose(env)                                   # 没有 onboarded_at：第一次使用
    assert p["stage"] == "onboarding"
    p["mode"] = "chat"                                 # 即使有人把 mode 改成 chat，阶段不对也不收
    (env.run / "proposal.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    assert act.main(["go", "--run", str(env.run), "--choice", "safe"]) == 2
    assert all((d / "a.bin").exists() for d in dirs.values())


def test_go_choice_refused_when_proposal_has_no_mode(env):
    _, dirs = setup(env)
    onboard(env)
    p = propose(env)
    p.pop("mode")
    (env.run / "proposal.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    assert act.main(["go", "--run", str(env.run), "--choice", "safe"]) == 2
    assert all((d / "a.bin").exists() for d in dirs.values())


def test_interactive_cards_never_in_any_option(env):
    """含交互会话的卡只能在看板里逐个确认：即使本机规则是“删”，三档选项里也不含它。"""
    onboard(env, rules={"other/recommended": {"action": "delete"}})
    d = env.work / "sess"
    mkfile(d / "a.jsonl")
    age(d)
    it = delete_item(d)
    it["class"] = "interactive"
    env.write([node(d)], [card("c01", [it], verdict="decide", question="这个对话你还要续聊吗？")])
    env.seal()
    p = propose(env)
    assert p["mode"] == "board"
    for name, o in p["options"].items():
        assert "c01" not in o["cards"] and not o["answers"], name
