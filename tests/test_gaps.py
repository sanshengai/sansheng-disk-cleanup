"""P5.2 评测补上的两个闸门缺口。
缺口 1：producer.blocked_by_processes（进程名）封存进条目，act 执行前用 pgrep 复核，在运行或查不出来就跳过。
缺口 2：producer.safety=risky——chat-netdisk 卡整卡降 keep；其他卡的 risky 把 clean 降为 decide。
全部在 tmp_path 里造夹具；pgrep 用 monkeypatch 模拟，不依赖本机真有 Chrome。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, age, auto_sel, card, delete_item, mkfile, node, plan  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore")

JUDGMENT = Path(plan.JUDGMENT_MD).read_text(encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def producer(**kw):
    p = {"tool": "t", "card": "browsers", "hint": "h", "safety": "safe", "default_select": True}
    p.update(kw)
    return p


def setup(env, prod, name="cache"):
    d = env.work / name
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d, producer=prod)], [card("c01", [delete_item(d)])])
    return d


def running(env, names):
    """模拟正在运行的进程；返回被查询过的进程名列表。None 表示 pgrep 出错。"""
    asked = []

    def fake(name):
        asked.append(name)
        return None if names is None else name in names

    env.mp.setattr(act, "process_running", fake)
    return asked


def item_of(rc):
    return rc["cards"][0]["items"][0]


# ---------------------------------------------------------------- 缺口 1：进程闸门

def test_blocking_process_running_skips_item(env):
    d = setup(env, producer(blocked_by_processes=["Google Chrome"]))
    running(env, {"Google Chrome"})
    assert env.act("c01") == 0
    it = item_of(env.receipt())
    assert it["status"] == "skipped"
    assert "Google Chrome 正在运行，按知识卡只报告不执行" in it["reason"]
    assert (d / "a.bin").exists()


def test_blocking_process_pgrep_error_skips_conservatively(env):
    d = setup(env, producer(blocked_by_processes=["Google Chrome"]))
    running(env, None)
    assert env.act("c01") == 0
    it = item_of(env.receipt())
    assert it["status"] == "skipped" and "无法确认" in it["reason"] and "Google Chrome" in it["reason"]
    assert (d / "a.bin").exists()


def test_blocking_process_not_running_proceeds(env):
    d = setup(env, producer(blocked_by_processes=["Google Chrome"]))
    asked = running(env, set())
    assert env.act("c01") == 0
    assert item_of(env.receipt())["status"] == "removed"
    assert not (d / "a.bin").exists()
    assert "Google Chrome" in asked          # 确实查过，不是没读字段


def test_no_blocked_by_processes_never_asks_pgrep(env):
    d = setup(env, producer())
    asked = running(env, {"Google Chrome"})
    assert env.act("c01") == 0
    assert item_of(env.receipt())["status"] == "removed" and asked == []


def test_any_of_several_processes_blocks(env):
    d = setup(env, producer(blocked_by_processes=["Google Chrome", "Google Chrome Helper"]))
    running(env, {"Google Chrome Helper"})
    assert env.act("c01") == 0
    it = item_of(env.receipt())
    assert it["status"] == "skipped" and "Google Chrome Helper" in it["reason"]
    assert (d / "a.bin").exists()


def test_pilot_is_also_gated(env):
    d = setup(env, producer(blocked_by_processes=["Google Chrome"]))
    running(env, {"Google Chrome"})
    digest = env.seal()
    assert act.main(auto_sel(["run", "--run", str(env.run), "--seal", digest, "--cards", "c01", "--pilot"])) == 0
    rc = json.loads(next(env.run.glob("receipt-pilot-*.json")).read_text())
    assert item_of(rc)["status"] == "skipped" and (d / "a.bin").exists()


def test_dry_run_marks_blocked_but_deletes_nothing(env, capsys):
    d = setup(env, producer(blocked_by_processes=["Google Chrome"]))
    running(env, {"Google Chrome"})
    assert env.act("c01", dry=True) == 0
    rc = json.loads(next(env.run.glob("receipt-dryrun-*.json")).read_text())
    it = item_of(rc)
    assert it["status"] == "planned" and it["blocked_by"] == ["Google Chrome"]
    assert "正式执行时会跳过" in it["reason"] and "Google Chrome 正在运行" in it["reason"]
    assert (d / "a.bin").exists()
    assert "进程闸门" in capsys.readouterr().out


def test_dry_run_unblocked_has_no_marker(env):
    setup(env, producer(blocked_by_processes=["Google Chrome"]))
    running(env, set())
    assert env.act("c01", dry=True) == 0
    it = item_of(json.loads(next(env.run.glob("receipt-dryrun-*.json")).read_text()))
    assert it["status"] == "planned" and "blocked_by" not in it


def test_seal_carries_blocked_by_processes(env):
    setup(env, producer(blocked_by_processes=["Google Chrome", " ", 7]))      # 非法元素被丢弃
    env.seal()
    seal = json.loads((env.run / "seal.json").read_text())
    assert seal["cards"]["c01"]["items"][0]["blocked_by_processes"] == ["Google Chrome"]


def test_malformed_sealed_field_is_treated_as_unknown(env):
    assert act.blocking_processes({"blocked_by_processes": "Google Chrome"})[1]
    assert act.blocking_processes({"blocked_by_processes": [""]})[1]
    assert act.blocking_processes({}) == ([], [])


@pytest.mark.parametrize("rc,want", [(0, True), (1, False), (2, None), (3, None)])
def test_process_running_exit_codes(monkeypatch, rc, want):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, rc, "", "")

    monkeypatch.setattr(act.subprocess, "run", fake_run)
    assert act.process_running("Google Chrome") is want
    cmd, kw = calls[0]
    assert cmd == ["pgrep", "-x", "--", "Google Chrome"] and kw["shell"] is False and kw["timeout"] > 0


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("pgrep", 1), FileNotFoundError("pgrep")])
def test_process_running_timeout_or_missing_is_unknown(monkeypatch, exc):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(act.subprocess, "run", boom)
    assert act.process_running("Google Chrome") is None


# ---------------------------------------------------------------- 缺口 2：risky 节点

def one(env, prod, name="cache", verdict="clean"):
    d = env.work / name
    mkfile(d / "a.bin")
    age(d)
    c = card("c01", [delete_item(d)], verdict=verdict, question="q?" if verdict == "decide" else None)
    env.write([node(d, producer=prod)], [c])
    return env.check()["cards"][0]


def test_risky_chat_netdisk_downgrades_to_keep(env):
    out = one(env, producer(card="chat-netdisk", safety="risky"))
    assert out["verdict_out"] == "keep" and out["downgraded"] is True
    assert any("聊天记录与网盘本地文件只走软件自身入口" in e for e in out["errors"])
    ref = next(r for r in out["judgment_refs"] if r["title"] == plan.JUDGMENT_MAP["chat_netdisk"][0])
    assert ref["quote"] in JUDGMENT and "聊天" in ref["quote"]
    assert plan.main(["seal", "--run", str(env.run)]) == 0
    assert "c01" not in json.loads((env.run / "seal.json").read_text())["cards"]


def test_risky_chat_netdisk_keep_even_for_decide_card(env):
    out = one(env, producer(card="chat-netdisk", safety="risky"), verdict="decide")
    assert out["verdict_out"] == "keep"


def test_risky_other_card_clean_downgrades_to_decide(env):
    out = one(env, producer(card="browsers", safety="risky"))
    assert out["verdict_out"] == "decide" and out["downgraded"] is True
    assert any("该位置属于用户资产或高风险区，需用户明确选择" in e for e in out["errors"])
    assert out["default_select"] is False and "用户资产" in out["question"]
    ref = next(r for r in out["judgment_refs"] if r["title"] == plan.JUDGMENT_MAP["risky_zone"][0])
    assert ref["quote"] in JUDGMENT and "用户数据层" in ref["quote"]
    env.seal()
    assert json.loads((env.run / "seal.json").read_text())["cards"]["c01"]["verdict"] == "decide"


@pytest.mark.parametrize("prod", [
    dict(card="chat-netdisk", safety="moderate"),     # 如 Slack/Cache 这类更具体的 moderate 规则
    dict(card="browsers", safety="moderate"),
    dict(card="chat-netdisk", safety="safe"),
    dict(card="browsers"),
])
def test_non_risky_producer_unaffected(env, prod):
    out = one(env, producer(**prod))
    assert out["verdict_out"] == "clean" and not out["downgraded"]


def test_specific_moderate_rule_under_risky_parent_unaffected(env):
    """判据只看条目所在节点实际命中的那条规则：risky 的微信根节点下，Cache 子节点命中 moderate 规则，不受影响。"""
    parent = env.work / "Slack"
    child = parent / "Cache"
    mkfile(child / "a.bin")
    age(parent)
    nodes = [node(parent, id="n1", producer=producer(card="chat-netdisk", safety="risky")),
             node(child, id="n2", producer=producer(card="chat-netdisk", safety="moderate"))]
    env.write(nodes, [card("c01", [delete_item(child)])])
    out = env.check()["cards"][0]
    assert out["verdict_out"] == "clean" and not out["downgraded"]
    # 反过来，条目落在 risky 父节点自己的路径上就被拦
    env.write(nodes, [card("c01", [delete_item(parent)])])
    assert env.check()["cards"][0]["verdict_out"] == "keep"
