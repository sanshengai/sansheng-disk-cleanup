"""0.5 check 输出按卡、按原因汇总；风险提示同一原因只出现一次。完整明细仍在 plan.check.json。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, age, card, delete_item, mkfile, node, plan  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def risky_producer():
    return {"tool": "t", "card": "browsers", "hint": "h", "safety": "risky", "default_select": False}


def test_split_message_and_grouping():
    assert plan.split_check_message("/a/b：不在 profile.roots 之内") == ("/a/b", "不在 profile.roots 之内")
    assert plan.split_check_message("/a/b（节点 n3 /a）：存在 3 处引用") == ("/a/b", "存在 3 处引用")
    assert plan.split_check_message("风险提示：/a/b：里面有账号数据") == ("/a/b", "风险提示：里面有账号数据")
    assert plan.split_check_message("一张卡最多一个交互会话条目") == (None, "一张卡最多一个交互会话条目")
    g = plan.summarize_messages([f"/p/{i}（节点 n1 /p）：存在 {i} 处引用" for i in range(1, 6)] + ["/q：路径不存在"])
    assert [x["count"] for x in g] == [5, 1] and len(g[0]["examples"]) == 3 and len(g[0]["paths"]) == 5


def test_check_prints_per_card_per_reason_and_writes_full_detail(env, capsys):
    nodes, items = [], []
    for i in range(6):                                         # 6 个不存在的路径：同一原因
        items.append(delete_item(env.work / f"gone{i}"))
    for i in range(4):                                         # 4 个受保护路径：另一类原因（还会附带“没有覆盖节点”）
        p = mkfile(env.home / "Documents" / f"x{i}" / "a.txt").parent
        items.append(delete_item(p))
    env.write(nodes, [card("c01", items)])
    assert plan.main(["check", "--run", str(env.run)]) == 0
    out = capsys.readouterr().out
    res = json.loads((env.run / "plan.check.json").read_text())
    c = res["cards"][0]
    # 明细不丢：errors / 顶层 errors 仍是逐条的
    assert len(c["errors"]) >= 10 and len(res["errors"]) == len(c["errors"])
    s = c["error_summary"]
    assert sum(g["count"] for g in s) == len(c["errors"])
    assert all(len(g["examples"]) <= 3 for g in s)
    gone = next(g for g in s if "不存在" in g["reason"])
    assert gone["count"] == 6 and len(gone["paths"]) == 6 and len(gone["examples"]) == 3
    # 输出：每种原因一行，不再逐条目
    assert f"c01 卡片 c01：{len(c['errors'])} 条，原因 {len(s)} 种" in out
    assert "（6 条）" in out and "（4 条）" in out
    assert out.count("路径不存在或无法读取") == 1
    assert len(out.splitlines()) < 15
    assert "plan.check.json" in out


def test_risk_note_reported_once_per_card(env, capsys):
    """c18 实例：同一张卡里同一条风险提示被重复列出。"""
    items, nodes = [], []
    for i in range(3):
        d = env.work / f"acct{i}"
        mkfile(d / "a.bin")
        age(d)
        items.append(delete_item(d))
        nodes.append(node(d, id=f"n{i}", producer=risky_producer()))
    env.write(nodes, [card("c18", items, verdict="decide", question="要清吗？")])
    assert plan.main(["check", "--run", str(env.run)]) == 0
    out = capsys.readouterr().out
    res = json.loads((env.run / "plan.check.json").read_text())["cards"][0]
    assert res["verdict_out"] == "decide" and len(res["risk_notes"]) == 3
    lines = [l for l in out.splitlines() if "风险提示" in l]
    assert len(lines) == 1 and "（3 条）" in lines[0]
    # 同一张卡里完全相同的提示不重复写进 warnings
    assert len(res["warnings"]) == len(set(res["warnings"]))


def test_identical_warnings_deduped(env, monkeypatch):
    """同一张卡里字面完全相同的提示（如两条相同的 evidence 告警）只留一条。"""
    d = env.work / "c"
    mkfile(d / "a.bin")
    age(d)
    bad = {"kind": "refs_reviewed", "samples": ["x.md:1"], "verdict": "history_only"}
    env.write([node(d)], [card("c01", [delete_item(d)], evidence=[bad, dict(bad)])])
    res = env.check()["cards"][0]
    assert len(res["warnings"]) == len(set(res["warnings"])) and res["warnings"]
