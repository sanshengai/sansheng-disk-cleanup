"""0.6 初始设置的两条硬验收：

1. 每题都有消费者：同一份 facts / plan，答案取两种值，check 或 propose 的输出（verdict_out / prefill / preset /
   mode / board_reasons / proposal 字段）至少有一处不同。答案改了没有任何结果变化的题，说明问得没有意义，这里测试不通过。
   新增题而没登记消费者，同样不通过。
2. 安全不变式：设置只改变“问不问、预填什么、进哪一档”，不改变“能不能放行”。把设置取遍每个值（以及最激进的组合），
   没有任何卡因此从 decide 升为 clean，也没有 keep 变成可执行。并用变异证明这条断言能抓到“读设置时放行”。
"""
import itertools
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, age, card, delete_item, mkfile, node, plan, protect  # noqa: E402
from test_projects import ledger, write_profile  # noqa: E402

import prefs  # noqa: E402
import setup_q  # noqa: E402

DAY = 86400


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# ------------------------------------------------------------ 夹具：一份 facts / plan，覆盖每个设置会碰到的卡

def utime_days(path, days):
    t = time.time() - days * DAY
    for dp, dns, fns in os.walk(path):
        for n in fns:
            os.utime(os.path.join(dp, n), (t, t))


def iso_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(env):
    """返回 {卡号: 说明}。卡：
    tool 安卓缓存（decide）/ conv 交互对话（decide，60 天前）/ convproj 做完项目里的交互对话 / proj 做完项目里的构建缓存
    img AI 出图（decide，没有成品副本）/ wt 工作树根（decide）/ wtc 工作树根（clean）/ inst 安装包（decide）
    big 45 天前的大文件（decide）/ prot 放在“别碰”候选目录里 / deliv 靠成品目录里的副本放行的 clean / slow 重建很慢的缓存（clean）"""
    work, home = env.work, env.home
    proj = home / "proj"
    proj2 = home / "proj2"
    spec = {
        "tool": (work / "tool", "decide", {"cards_used": ["android"]}),
        "conv": (work / "conv", "decide", {"cards_used": ["codex"]}),
        "img": (work / "img", "decide", {"cards_used": ["codex"], "category": "ai-images"}),
        "wt": (work / "wt1", "decide", {"cards_used": ["git-worktree-build"]}),
        "wtc": (work / "wt2", "clean", {"cards_used": ["git-worktree-build"]}),
        "inst": (work / "inst", "decide", {"cards_used": ["app-updaters-installers"]}),
        "big": (work / "big", "decide", {"cards_used": ["large-old-files"]}),
        "prot": (work / "prot", "decide", {"cards_used": ["browsers"]}),
        "deliv": (work / "deliv", "clean", {"cards_used": ["pipelines"]}),
        "slow": (work / "slow", "clean", {"cards_used": ["model-caches"]}),
        "proj": (proj / "build", "decide", {"cards_used": ["pipelines"]}),
        "convproj": (proj / "chat", "decide", {"cards_used": ["claude-code"]}),
        "act": (proj2 / "build", "decide", {"cards_used": ["pipelines"]}),         # 进行中的项目：基线里就是 keep
    }
    nodes, cards = [], []
    (work / "成品").mkdir(exist_ok=True)
    mkfile(work / "成品" / "a.bin")
    for i, (cid, (d, verdict, extra)) in enumerate(spec.items(), 1):
        mkfile(d / "a.bin")
        age(d)
        kw = {}
        if cid in ("wt", "wtc"):
            kw["git"] = {"repo": str(d), "worktree": str(d), "branch": "x", "role": "untracked", "tracked_files": 0,
                         "merged_into_origin_main": True, "dirty_files": 0}
        if cid == "big":
            kw["newest_mtime"] = iso_days_ago(45)
        if cid in ("conv", "convproj"):
            kw["newest_mtime"] = iso_days_ago(50)
        item_kw = {"class": "interactive", "session": cid} if cid in ("conv", "convproj") else {}
        c = card(cid, [delete_item(d, **item_kw)], verdict=verdict, question="删吗？" if verdict == "decide" else None)
        c.update(extra)
        if verdict == "decide" or cid == "deliv":
            c["evidence"] = []
        if cid == "deliv":
            mkfile(work / "成品" / "b.bin", b"y" * 5000)
            mkfile(d / "b.bin", b"y" * 5000)
            age(d)
            c["evidence"] = [{"kind": "copy_verified", "src": str(d / "b.bin"), "dst": str(work / "成品" / "b.bin")}]
        cards.append(c)
        nodes.append(node(d, id=f"n{i}", **kw))
    for cid in ("conv", "convproj"):
        utime_days(spec[cid][0], 50)                     # 50 天前：30 天的保留不挡它，60 天的会挡；隔离出“资料库”这条的影响
    (work / "prot" / "keepme").mkdir()
    env.write(nodes, cards)
    # 台账里有进行中的项目时，check 要求这次扫描有 projects.json（否则认不全归属，所有卡都不放行）
    projs = [{"key": os.path.realpath(d), "name": d.name, "kind": "git", "sources": ["git"], "bytes": 1, "shown": True,
              "roots": [os.path.realpath(d)], "claude_dirs": [], "session_ids": []} for d in (proj, proj2)]
    clouds = [{"path": "~/Library/Mobile Documents/com~apple~CloudDocs/视频素材", "provider": "iCloud Drive", "local_bytes": 12 * 10 ** 9, "files": 40},
              {"path": "~/Library/CloudStorage/Dropbox-团队/项目", "provider": "Dropbox", "local_bytes": 3 * 10 ** 9, "files": 9},
              {"path": "~/Library/CloudStorage/坚果云-x/同步", "provider": "其他", "local_bytes": 2 * 10 ** 9, "files": 3},
              {"path": "~/Library/CloudStorage/OneDrive-个人/小的", "provider": "OneDrive", "local_bytes": 10 ** 8, "files": 1}]     # 不到 1 GB：不提
    (env.run / "projects.json").write_text(json.dumps({"schema": "projects/1", "scan_id": "scan1", "projects": projs, "skipped": [],
                                                       "setup_facts": {"cloud_folders": clouds}}), encoding="utf-8")
    return spec


def base_profile(env):
    return {"onboarded_at": "2026-10-01T00:00:00Z",
            "projects": {**ledger(env.home / "proj", "done"), **ledger(env.home / "proj2", "active")}}


def with_answers(env, answers=None, ledger_lines=None, **raw):
    """base 台账 + 设置页答案（经 apply_answers）+ 原样字段；写成本机 profile。"""
    p = base_profile(env)
    if answers:
        done, errs = setup_q.apply_answers(p, answers)
        assert not errs, errs
    if ledger_lines:
        _, errs, _ = prefs.apply_ledger(p, ledger_lines)
        assert not errs, errs
    p.update(raw)
    write_profile(env, **p)
    return p


def outputs(env):
    """跑 check（plan.check_plan）、封存、提案，返回比较用的快照。"""
    run = str(env.run)
    result, records = plan.check_plan(run)
    assert plan.main(["seal", "--run", run]) == 0
    prop, err = plan.build_proposal(run)
    assert err is None, err
    return {"check": {c["id"]: c for c in result["cards"]}, "records": records, "proposal": prop}


def show(o, cid, *keys):
    c = o["check"][cid]
    return {k: c.get(k) for k in keys}


# ------------------------------------------------------------ 每题的两种取值 + 看哪里不同
# 键 = 题 id（可带 #说明 区分同一题的多个消费点）；值 = (答案 A, 答案 B, 提取函数, 额外的台账行)。
# 取值尽量选“删得更多”的和“更保守”的各一个。

DONE_PROJ = ("proj",)
CASES = {
    "read_scope": ({"read_scope": ["basic"]}, {"read_scope": ["basic", "extended", "deep"]},
                   lambda o: o["proposal"]["read_scope"]),
    "tools": ({"tools": {"android": "unused"}}, {"tools": {"android": "often"}},
              lambda o: show(o, "tool", "verdict_out", "prefill")),
    "protect": ({"protect": []}, {"protect": ["__PROT__"]},
                lambda o: show(o, "prot", "verdict_out", "downgraded")),
    "deliverables": ({"deliverables": []}, {"deliverables": ["__DELIV__"]},
                     lambda o: show(o, "deliv", "verdict_out")),
    "conversations": ({"conversations": "done"}, {"conversations": "library"},
                      lambda o: show(o, "conv", "verdict_out")),
    "conversations#done_projects": ({"conversations": "done"}, {"conversations": "sometimes"},
                                    lambda o: show(o, "convproj", "prefill")),
    "conversations#days": ({"conversations": "done", "conversation_days": 30}, {"conversations": "done", "conversation_days": 60},
                           lambda o: show(o, "conv", "verdict_out")),
    "rejects#drop": ({"rejects": "drop"}, {"rejects": "until_done"}, lambda o: show(o, "img", "prefill")),
    "rejects#keep": ({"rejects": "keep"}, {"rejects": "until_done"}, lambda o: show(o, "img", "verdict_out")),
    "worktrees#whole": ({"worktrees": "whole"}, {"worktrees": "build_only"}, lambda o: show(o, "wt", "prefill")),
    "worktrees#build_only": ({"worktrees": "build_only"}, {"worktrees": "whole"}, lambda o: show(o, "wtc", "verdict_out")),
    "worktrees#ask": ({"worktrees": "ask"}, {"worktrees": "whole"},
                      lambda o: (o["records"]["wtc"]["preset"], o["records"]["wtc"]["review"])),
    "installers": ({"installers": "auto"}, {"installers": "ask"}, lambda o: "inst" in o["proposal"]["attention"]),
    "big_files#30": ({"big_files": 30}, {"big_files": 90}, lambda o: show(o, "big", "verdict_out")),
    "big_files#off": ({"big_files": 0}, {"big_files": 30}, lambda o: show(o, "big", "verdict_out")),
    "wait": ({"wait": "minimal"}, {"wait": "space"}, lambda o: o["proposal"]["default"]),
    "done_projects#archive": ({"done_projects": "archive"}, {"done_projects": "intermediate"}, lambda o: show(o, "proj", "prefill")),
    "done_projects#keep": ({"done_projects": "keep"}, {"done_projects": "intermediate"}, lambda o: show(o, "proj", "prefill")),
    "governance": ({"governance": "tell"}, {"governance": "fix"}, lambda o: o["proposal"]["governance_mode"]),
    "delete_mode": ({"delete_mode": "trash"}, {"delete_mode": "delete"}, lambda o: o["proposal"]["delete_mode"]),
    "confirm": ({"confirm": "board"}, {"confirm": "smart"}, lambda o: (o["proposal"]["mode"], o["proposal"]["board_reasons"])),
    "cloud#ok": ({"cloud": "ok"}, {"cloud": "offline"}, lambda o: [c["path"] for c in o["proposal"]["cloud_suggestions"]]),
    "cloud#unsure": ({"cloud": "unsure"}, {"cloud": "offline"}, lambda o: [c["path"] for c in o["proposal"]["cloud_suggestions"]]),
}
# “projects”题的消费者是台账（写 profile.projects）：进行中 / 做完了让 proj 卡在 check 里 keep / decide
LEDGER_CASES = {
    "projects": ({"/PROJ/": {"状态": "进行中"}}, {"/PROJ/": {"状态": "做完了"}}, lambda o: show(o, "proj", "verdict_out")),
}
EXEMPT = {
    "note": "文本题：原话不存档，plan.py profile --apply 原样交给 Agent 翻译成待确认规则（往返见 test_setup.py）；"
            "翻译后的规则显示在清理看板 pending_rules（见 test_boards.py）",
}


def test_every_question_is_covered_or_exempt():
    covered = {k.split("#")[0] for k in list(CASES) + list(LEDGER_CASES)}
    assert covered | set(EXEMPT) == {q["id"] for q in setup_q.QUESTIONS}, \
        "新增 / 删除了题，但没有在 CASES / EXEMPT 里登记消费者"
    assert not covered & set(EXEMPT)
    assert all(len(v) > 20 for v in EXEMPT.values())              # 豁免必须写原因


def concrete(env, answers):
    """把答案里的占位路径换成夹具里真实存在的目录。"""
    out = json.loads(json.dumps(answers))
    for k, v in out.items():
        if isinstance(v, list):
            out[k] = [str(env.work / "prot") if x == "__PROT__" else str(env.work / "成品") if x == "__DELIV__" else x for x in v]
    return out


@pytest.mark.parametrize("name", list(CASES))
def test_answer_changes_check_or_propose_output(env, name):
    a, b, pick = CASES[name]
    build(env)
    with_answers(env, concrete(env, a))
    oa = pick(outputs(env))
    with_answers(env, concrete(env, b))
    ob = pick(outputs(env))
    assert oa != ob, f"{name}：两种答案下输出相同（{oa!r}），这题没有消费者"


@pytest.mark.parametrize("name", list(LEDGER_CASES))
def test_ledger_answers_change_check_output(env, name):
    a, b, pick = LEDGER_CASES[name]
    build(env)
    key = str(env.home / "proj")
    fix = lambda lines: {key if k == "/PROJ/" else k: v for k, v in lines.items()}  # noqa: E731
    p = base_profile(env)
    p.pop("projects")
    write_profile(env, **p)
    with_answers(env, ledger_lines=fix(a), **{k: v for k, v in p.items() if k != "projects"})
    oa = pick(outputs(env))
    with_answers(env, ledger_lines=fix(b), **{k: v for k, v in p.items() if k != "projects"})
    ob = pick(outputs(env))
    assert oa != ob, name


def test_consumer_details_are_what_the_spec_says(env):
    """不只是“不同”，还要是约定里写的那种不同。"""
    build(env)
    with_answers(env, {"tools": {"android": "unused"}, "rejects": "drop", "worktrees": "whole", "done_projects": "archive",
                       "conversations": "done"})
    o = outputs(env)
    assert o["check"]["tool"]["prefill"] == "删" and o["check"]["img"]["prefill"] == "删" and o["check"]["wt"]["prefill"] == "删"
    assert o["check"]["proj"]["prefill"] == "归档"
    assert o["check"]["convproj"]["prefill"] == "归档"                 # 含交互对话的卡：选了“做完就不再看”才预填，值随做完项目的处理方式
    assert o["proposal"]["prefill"]["tool"] == "删"                    # 进了提案
    with_answers(env, {"conversations": "library"})
    o = outputs(env)
    assert o["check"]["conv"]["verdict_out"] == "keep" and any("资料库" in e for e in o["check"]["conv"]["errors"])
    with_answers(env, {"rejects": "keep"})
    o = outputs(env)
    assert o["check"]["img"]["verdict_out"] == "keep" and any("都留着" in e for e in o["check"]["img"]["errors"])
    with_answers(env, {"worktrees": "build_only"})
    o = outputs(env)
    assert o["check"]["wtc"]["verdict_out"] == "decide" and o["check"]["wtc"]["question"] == "只清构建产物，整棵工作树先留着？"
    assert o["check"]["wt"]["verdict_out"] == "decide"                 # 原本就是 decide：不变，也不预填
    assert o["check"]["wt"].get("prefill") is None
    with_answers(env, {"big_files": 30})
    assert outputs(env)["check"]["big"]["verdict_out"] == "decide"     # 45 天前，超过 30 天：照常问
    with_answers(env, {"big_files": 90})
    o = outputs(env)
    assert o["check"]["big"]["verdict_out"] == "keep" and "不到你定的 90 天" in o["check"]["big"]["errors"][0]
    with_answers(env, {"big_files": 0})
    assert "别问" in outputs(env)["check"]["big"]["errors"][0]


def test_cloud_suggestions_content_and_filters(env):
    """第 11 题的消费者：回答“可以 / 不确定”才出建议；只列本机占 ≥ 1 GB 的；每条写明官方怎么做和官方链接；offline / 没答为空。"""
    build(env)
    with_answers(env, {"cloud": "ok"})
    o = outputs(env)
    sug = o["proposal"]["cloud_suggestions"]
    assert [c["provider"] for c in sug] == ["iCloud Drive", "Dropbox", "其他"]                 # 按体积从大到小；0.1 GB 那条不提
    assert all(c["local_bytes"] >= 10 ** 9 for c in sug)
    assert "移除下载" in sug[0]["how"] and sug[0]["link"].startswith("https://support.apple.com/")
    assert "设为仅在线" in sug[1]["how"] and sug[1]["link"].startswith("https://help.dropbox.com/")
    assert sug[2]["link"] is None and "没法判断" in sug[2]["how"]
    assert all("正在上传" in c["how"] or c["provider"] == "其他" for c in sug)
    assert all("brctl" not in c["how"] for c in sug)                                          # 不教命令行驱逐
    assert "文件夹在本机占了" in o["proposal"]["message"]
    with_answers(env, {"cloud": "offline"})
    assert outputs(env)["proposal"]["cloud_suggestions"] == []
    with_answers(env)                                                                         # 没回答过
    assert outputs(env)["proposal"]["cloud_suggestions"] == []


def test_cloud_suggestions_ignore_stale_or_missing_projects_file(env):
    """反例：projects.json 的 scan_id 对不上（换过扫描）或没有 cloud_folders：不给建议，也不报错。"""
    build(env)
    with_answers(env, {"cloud": "ok"})
    f = env.run / "projects.json"
    data = json.loads(f.read_text())
    data["scan_id"] = "别的扫描"
    f.write_text(json.dumps(data), encoding="utf-8")
    assert outputs(env)["proposal"]["cloud_suggestions"] == []
    data["scan_id"] = "scan1"
    data["setup_facts"] = {}
    f.write_text(json.dumps(data), encoding="utf-8")
    assert outputs(env)["proposal"]["cloud_suggestions"] == []
    assert plan.cloud_suggestions_of({"cloud_policy": "ok"}, {"setup_facts": {"cloud_folders": [{"path": "~/x", "local_bytes": "9" * 3}, "垃圾", None]}}) == []


def test_board_payload_passes_cloud_suggestions_through(env):
    """propose 写进 proposal.json 的云盘建议，plan.py board 原样带进看板数据（顶层和 proposal 里各一份）。"""
    import re
    build(env)
    with_answers(env, {"cloud": "ok"})
    assert plan.main(["seal", "--run", str(env.run)]) == 0
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    want = json.loads((env.run / "proposal.json").read_text())["cloud_suggestions"]
    assert len(want) == 3
    assert plan.main(["board", "--run", str(env.run)]) == 0
    html = (env.run / "board.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r'<script type="application/json" id="plan-data">(.*?)</script>', html, re.S).group(1))
    assert data["cloud_suggestions"] == want and data["proposal"]["cloud_suggestions"] == want
    with_answers(env, {"cloud": "offline"})                                  # 反例：回答“不行”后重新提案，看板里就没有了
    assert plan.main(["seal", "--run", str(env.run)]) == 0
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    assert plan.main(["board", "--run", str(env.run)]) == 0
    html = (env.run / "board.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r'<script type="application/json" id="plan-data">(.*?)</script>', html, re.S).group(1))
    assert data["cloud_suggestions"] == [] and data["proposal"]["cloud_suggestions"] == []


def test_cloud_policy_does_not_change_any_verdict(env):
    """云盘设置只多给建议，不影响任何卡的放行（check 结果逐卡相同）。"""
    build(env)
    with_answers(env, {"cloud": "offline"})
    a = outputs(env)
    with_answers(env, {"cloud": "ok"})
    b = outputs(env)
    assert {k: v["verdict_out"] for k, v in a["check"].items()} == {k: v["verdict_out"] for k, v in b["check"].items()}
    assert a["proposal"]["options"] == b["proposal"]["options"]
    assert protect.profile_guard({"cloud_policy": "ok"})["digest"] == protect.profile_guard({"cloud_policy": "offline"})["digest"]


def test_installer_auto_acts_like_a_delete_rule_but_not_for_review_cards(env):
    build(env)
    with_answers(env, {"installers": "auto"})
    o = outputs(env)
    assert o["records"]["inst"]["installer"] is True and "inst" not in o["proposal"]["attention"]
    assert "inst" in o["proposal"]["options"]["recommended"]["cards"]       # 仍只进它自己那一档，不是更低的档
    with_answers(env, {"installers": "auto"}, rules={"updates/app-updaters-installers/recommended": {"action": "keep"}})
    assert "inst" not in outputs(env)["proposal"]["options"]["recommended"]["cards"]   # 用户明确的“留”规则优先
    # 降过级的安装包卡（引用没核实、更新暂存未确认等）不适用
    c = json.loads((env.run / "plan.json").read_text())
    inst = next(x for x in c["cards"] if x["id"] == "inst")
    inst["verdict"], inst["evidence"] = "clean", []                         # clean 缺结束证据 → 降为 decide（downgraded）
    (env.run / "plan.json").write_text(json.dumps(c), encoding="utf-8")
    with_answers(env, {"installers": "auto"})
    o = outputs(env)
    assert o["records"]["inst"]["review"] is True and "inst" in o["proposal"]["attention"]


def test_slow_rebuild_caches_never_enter_safe_tier_whatever_the_settings(env):
    build(env)
    with_answers(env)
    o = outputs(env)
    assert o["records"]["slow"]["preset"] == "recommended" and o["records"]["slow"]["preset_raw"] == "recommended"
    assert "slow" not in o["proposal"]["options"]["safe"]["cards"]
    c = json.loads((env.run / "plan.json").read_text())
    slow = next(x for x in c["cards"] if x["id"] == "slow")
    slow["cards_used"] = ["pipelines"]                                      # 不是模型缓存，但卡片自己标了重建很慢
    slow["rebuild"] = "slow"
    (env.run / "plan.json").write_text(json.dumps(c), encoding="utf-8")
    assert outputs(env)["records"]["slow"]["preset"] == "recommended"
    slow.pop("rebuild")
    (env.run / "plan.json").write_text(json.dumps(c), encoding="utf-8")
    assert outputs(env)["records"]["slow"]["preset"] == "safe"             # 对照：普通的 clean 缓存仍在稳妥档


# ------------------------------------------------------------ 安全不变式

RANK = {"keep": 0, "govern": 0, "decide": 1, "clean": 2}


def escalations(base, other):
    """other 相对 base 变得更宽松的卡：判定升级（keep→decide、decide→clean），或原本不可执行的卡进了封存。"""
    bad = []
    for cid, bc in base["check"].items():
        oc = other["check"][cid]
        if RANK[oc["verdict_out"]] > RANK[bc["verdict_out"]]:
            bad.append(f"{cid}: {bc['verdict_out']} -> {oc['verdict_out']}")
    for cid, rec in other["records"].items():
        if cid not in base["records"]:
            bad.append(f"{cid}: 原来不在封存里，现在可执行")
        elif RANK[rec["verdict"]] > RANK[base["records"][cid]["verdict"]]:
            bad.append(f"{cid}: 封存里 {base['records'][cid]['verdict']} -> {rec['verdict']}")
    return bad


def assert_no_escalation(base, other):
    bad = escalations(base, other)
    assert not bad, "设置让这些卡变得更宽松：" + "；".join(bad)


EVERY_VALUE = {  # 每个设置的全部取值
    "tools": [{"tools": {k: v for k in setup_q.TOOL_KEYS}} for v in setup_q.TOOL_STATES],
    "conversations": [{"conversations": v} for v in ("done", "sometimes", "library")]
                     + [{"conversations": "done", "conversation_days": d} for d in setup_q.CONV_DAYS],
    "rejects": [{"rejects": v} for v in ("drop", "until_done", "keep")],
    "worktrees": [{"worktrees": v} for v in ("whole", "build_only", "ask")],
    "installers": [{"installers": v} for v in ("auto", "ask")],
    "big_files": [{"big_files": v} for v in (30, 90, 0)],
    "wait": [{"wait": v} for v in ("minimal", "some", "space")],
    "done_projects": [{"done_projects": v} for v in ("intermediate", "archive", "keep")],
    "governance": [{"governance": v} for v in ("tell", "guide", "fix")],
    "delete_mode": [{"delete_mode": v} for v in ("trash", "delete")],
    "confirm": [{"confirm": v} for v in ("smart", "board")],
    "read_scope": [{"read_scope": s} for s in ([], ["basic"], ["basic", "extended", "deep"])],
    "cloud": [{"cloud": v} for v in ("ok", "offline", "unsure")],
}
AGGRESSIVE = {"tools": {k: "unused" for k in setup_q.TOOL_KEYS}, "conversations": "done", "conversation_days": 7,
              "rejects": "drop", "worktrees": "whole", "installers": "auto", "big_files": 30, "wait": "space",
              "done_projects": "archive", "governance": "fix", "delete_mode": "delete", "confirm": "smart", "cloud": "ok",
              "read_scope": ["basic", "extended", "deep"]}
CAUTIOUS = {"conversations": "library", "rejects": "keep", "worktrees": "build_only", "big_files": 0, "wait": "minimal",
            "done_projects": "keep", "installers": "ask", "confirm": "board", "delete_mode": "trash", "read_scope": []}


def baseline(env):
    build(env)
    with_answers(env)
    return outputs(env)


def test_no_setting_value_makes_any_card_more_permissive(env):
    base = baseline(env)
    n = 0
    for group, variants in EVERY_VALUE.items():
        for answers in variants:
            with_answers(env, answers)
            assert_no_escalation(base, outputs(env))
            n += 1
    assert n > 30
    for combo in (AGGRESSIVE, CAUTIOUS):
        with_answers(env, combo)
        assert_no_escalation(base, outputs(env))


def test_most_aggressive_answers_keep_decide_cards_decide_and_keep_cards_keep(env):
    base = baseline(env)
    with_answers(env, AGGRESSIVE)
    agg = outputs(env)
    for cid, bc in base["check"].items():
        assert agg["check"][cid]["verdict_out"] == bc["verdict_out"] or RANK[agg["check"][cid]["verdict_out"]] < RANK[bc["verdict_out"]], cid
        if bc["verdict_out"] == "decide":
            assert agg["check"][cid]["verdict_out"] != "clean", cid
        if bc["verdict_out"] == "keep":
            assert agg["check"][cid]["verdict_out"] == "keep" and cid not in agg["records"], cid
    # 反过来确认这不是空转：激进答案确实改变了东西（预填、档位或提案），只是没放行
    assert agg["proposal"]["prefill"] != base["proposal"]["prefill"]
    assert agg["check"]["tool"]["verdict_out"] == "decide"


def test_settings_never_bypass_evidence_or_protection(env):
    """设置不能绕过证据复核与保护名单：clean 卡缺结束证据照样降 decide，保护路径照样 keep。"""
    build(env)
    with_answers(env, AGGRESSIVE)
    o = outputs(env)
    c = json.loads((env.run / "plan.json").read_text())
    for card_ in c["cards"]:
        if card_["id"] == "slow":
            card_["evidence"] = []
    (env.run / "plan.json").write_text(json.dumps(c), encoding="utf-8")
    assert outputs(env)["check"]["slow"]["verdict_out"] == "decide"
    with_answers(env, dict(AGGRESSIVE, protect=[str(env.work / "tool")]))
    assert outputs(env)["check"]["tool"]["verdict_out"] == "keep"


# ------------------------------------------------------------ 变异：让 check 读到设置就放行，不变式测试必须失败

def _mutate_decide_to_clean(env, setting, value):
    real = plan.check_plan

    def mutated(run):
        res, rec = real(run)
        if plan.load_profile().get(setting) == value:
            for c in res["cards"]:
                if c["verdict_out"] == "decide":
                    c["verdict_out"] = "clean"
        return res, rec
    env.mp.setattr(plan, "check_plan", mutated)


def _mutate_keep_to_decide(env, setting, value):
    real = plan.check_plan

    def mutated(run):
        res, rec = real(run)
        if plan.load_profile().get(setting) == value:
            for c in res["cards"]:
                if c["verdict_out"] == "keep":
                    c["verdict_out"] = "decide"
        return res, rec
    env.mp.setattr(plan, "check_plan", mutated)


@pytest.mark.parametrize("mutate, answers, setting, value", [
    (_mutate_decide_to_clean, AGGRESSIVE, "rejects_policy", "drop"),
    (_mutate_decide_to_clean, AGGRESSIVE, "conversation_policy", "done"),
    (_mutate_keep_to_decide, {"big_files": 0}, "big_file_age_days", 0),
    (_mutate_keep_to_decide, {"conversations": "library"}, "conversation_policy", "library"),
])
def test_invariant_test_catches_a_check_that_reads_settings_and_lets_things_through(env, mutate, answers, setting, value):
    base = baseline(env)
    with_answers(env, answers)
    assert_no_escalation(base, outputs(env))                        # 没变异时通过
    mutate(env, setting, value)                                     # 变异：check 读到这个设置就放行
    mutated = outputs(env)
    with pytest.raises(AssertionError):
        assert_no_escalation(base, mutated)
    assert escalations(base, mutated)


def test_invariant_test_would_also_catch_a_sealed_card_that_was_not_executable_before(env):
    base = baseline(env)
    with_answers(env, {"big_files": 0})
    other = outputs(env)
    other["records"]["act"] = {"verdict": "decide", "items": []}   # 变异：本来 keep、不在封存里的卡混进了封存
    assert "act" not in base["records"] and base["check"]["act"]["verdict_out"] == "keep"
    with pytest.raises(AssertionError):
        assert_no_escalation(base, other)
