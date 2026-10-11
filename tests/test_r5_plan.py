"""P5.R 安全评审修补（plan.py 一路）的反例与正例：H1 条目风险画像三方合并、H2 结束证据必须与条目关联、
H3 免证据只认 safety=="safe"、M1 无样本补救只能软降级、M2 缺 class 的会话按路径/命令推断、M3 decide 卡的风险提示。
夹具全在 tmp_path 内（HOME 指向临时目录）；伪造例都是评审里能让卡片以 clean 通过的写法，现在必须停在 decide/keep。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import (END_EVIDENCE, TID, TID2, Env, age, card, codex_item, delete_item, git, mkfile,  # noqa: E402
                           node, plan, reviewed)
import scan  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(scan, "HOME", str(e.home))
    import updaters
    monkeypatch.setattr(updaters, "probe_updaters", lambda *a, **k: {"items": []})
    return e


def prod(path):
    return scan.Producers().match(str(path))


def bare(cid, items, evidence=(), verdict="clean", question=None):
    """不带 test_plan_act 默认通过证据的卡：证据完全由用例给。"""
    c = card(cid, items, verdict=verdict, question=question or ("还要吗？" if verdict == "decide" else None))
    c["evidence"] = list(evidence)
    return c


def run_check(env, nodes, cards):
    env.write(nodes, cards)
    return {c["id"]: c for c in env.check()["cards"]}


def make_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    mkfile(path / "base.txt", b"base\n")
    git(path, "add", "base.txt")
    git(path, "commit", "-q", "-m", "base")
    git(path, "branch", "done")
    mkfile(path / "second.txt", b"2\n")
    git(path, "add", "second.txt")
    git(path, "commit", "-q", "-m", "second")
    git(path, "update-ref", "refs/remotes/origin/main", "main")
    return path


# ============================================================ H1：条目自己的风险画像，不只看最近的事实节点

def test_h1_A_netdisk_cache_under_generic_caches_node_is_keep(env):
    caches = env.home / "Library/Caches"
    gd = caches / "com.google.GoogleDrive"
    mkfile(gd / "offline/doc.bin")
    age(caches)
    assert prod(gd)["card"] == "chat-netdisk" and prod(gd)["safety"] == "risky"
    out = run_check(env, [node(caches, producer=prod(caches))], [card("c1", [delete_item(gd)])])
    assert out["c1"]["verdict_out"] == "keep", out["c1"]
    mp = plan.merged_producer(os.path.realpath(gd), [node(caches, producer=prod(caches))])
    assert mp["chat"] and "Google Drive" in mp["blockers"], mp


def test_h1_B_chat_and_browser_data_under_ancestor_node_with_fake_evidence(env):
    asup = env.home / "Library/Application Support"
    lark, ff = asup / "LarkShell", asup / "Firefox"
    mkfile(lark / "aha/users/1/chat.db")
    mkfile(ff / "Profiles/x/logins.json")
    (asup / "MobileSync").mkdir(parents=True)
    mkfile(env.home / "any/x.txt")
    age(asup)
    n = node(asup, producer=prod(asup), protected=True, refs={"checked": False, "reason": "protected"})
    ev = [reviewed("whatever.md"), {"kind": "delivered", "glob": str(env.home / "any/*"), "expect": 1}]
    out = run_check(env, [n], [card("c1", [delete_item(lark)], evidence=ev), card("c2", [delete_item(ff)], evidence=ev)])
    assert out["c1"]["verdict_out"] == "keep", out["c1"]       # 聊天记录：硬 keep
    assert out["c2"]["verdict_out"] == "decide", out["c2"]     # 浏览器配置：risky 至少要用户明确选择
    assert any("账号数据或用户资产" in e or "高风险区" in e for e in out["c2"]["errors"]), out["c2"]


def test_h1_firefox_is_decide_even_with_real_end_evidence(env):
    asup = env.home / "Library/Application Support"
    ff = asup / "Firefox"
    mkfile(ff / "Profiles/x/logins.json")
    age(asup)
    out = run_check(env, [node(asup, producer=prod(asup))], [card("c1", [delete_item(ff)])])
    assert out["c1"]["verdict_out"] == "decide", out["c1"]


def test_h1_H_parent_of_risky_descendant_node_is_decide(env):
    asup = env.home / "Library/Application Support"
    bm = asup / "Blackmagic Design"
    res = bm / "DaVinci Resolve"
    mkfile(res / "Resolve Disk Database/Resolve Projects/Users/guest/Projects/P/Project.db")
    age(asup)
    assert prod(res)["safety"] == "risky"
    # 祖先节点引用已核对（不靠 M1 的补救降级）：只有后代节点的 risky 能把它拦成 decide
    nodes = [node(asup, producer=prod(asup)), dict(node(res, producer=prod(res)), id="n2")]
    out = run_check(env, nodes, [card("c1", [delete_item(bm)])])      # card() 自带一条合格的结束证据
    assert out["c1"]["verdict_out"] == "decide", out["c1"]
    assert any(plan.RISKY_ZONE_REASON in e for e in out["c1"]["errors"]), out["c1"]


def test_h1_small_updater_staging_under_caches_node_is_flagged(env):
    caches = env.home / "Library/Caches"
    sp = caches / "com.example.App" / "org.sparkle-project.Sparkle"
    mkfile(sp / "update.zip")
    age(caches)
    mp = plan.merged_producer(os.path.realpath(sp), [node(caches, producer=prod(caches))])
    assert mp["updater"] and not mp["safe"], mp
    out = run_check(env, [node(caches, producer=prod(caches))], [card("c1", [delete_item(sp)])])
    assert out["c1"]["verdict_out"] == "decide", out["c1"]


# ============================================================ H2：结束证据必须与被删条目关联

def stage_node(env):
    """一个没有 producer 规则的普通目录（工作区里的中间稿）：clean 必须有结束证据。"""
    d = env.work / "stage"
    mkfile(d / "a.bin")
    age(d)
    return d


def test_h2_merged_HEAD_against_HEAD_does_not_count(env):
    repo = make_repo(env.work / "r")
    d = stage_node(env)
    for ev in ({"kind": "merged", "repo": str(repo), "ref": "HEAD", "target": "HEAD"},      # 恒真伪证
               {"kind": "merged", "repo": str(repo), "ref": "HEAD", "target": "main"}):     # 本地分支不是远端跟踪分支
        out = run_check(env, [node(d)], [bare("c1", [delete_item(d)], [ev])])
        assert out["c1"]["verdict_out"] == "decide", (ev, out["c1"])


def test_h2_merged_repo_unrelated_to_item_does_not_count(env):
    repo = make_repo(env.work / "r")
    d = stage_node(env)        # d 不在 repo 的任何工作树内
    ev = {"kind": "merged", "repo": str(repo), "ref": "done"}
    out = run_check(env, [node(d)], [bare("c1", [delete_item(d)], [ev])])
    assert out["c1"]["verdict_out"] == "decide", out["c1"]
    assert any("不是同一个仓库" in e for e in out["c1"]["errors"]), out["c1"]


def test_h2_merged_ref_must_be_the_commit_checked_out_in_item_worktree(env):
    repo = make_repo(env.work / "r")
    git(repo, "branch", "feat", "done")
    wt = env.work / "wt-feat"
    git(repo, "worktree", "add", "-q", str(wt), "feat")
    mkfile(wt / "feat.txt", b"unmerged work\n")             # 工作树里有一个没并入任何地方的提交
    git(wt, "add", "feat.txt")
    git(wt, "commit", "-q", "-m", "feat work")
    item = mkfile(wt / "build" / "a.bin").parent
    ctx = {"profile": {}, "item_paths": [str(item)]}
    # 拿仓里一个无关的老提交（done 是 origin/main 的祖先）当 ref：它不是条目所在工作树检出的提交
    r = plan.verify_evidence({"kind": "merged", "repo": str(repo), "ref": "done"}, ctx)
    assert not r["end"] and "当前检出的提交" in r["msg"], r
    # 老实写 feat：没并入，同样不过
    r = plan.verify_evidence({"kind": "merged", "repo": str(repo), "ref": "feat"}, ctx)
    assert not r["end"] and "没有并入" in r["msg"], r


def test_h2_merged_ref_equal_to_target_commit_does_not_count(env):
    repo = make_repo(env.work / "r")
    git(repo, "branch", "same", "main")                           # 与 origin/main 同一个提交
    wt = env.work / "wt-same"
    git(repo, "worktree", "add", "-q", str(wt), "same")
    r = plan.verify_evidence({"kind": "merged", "repo": str(repo), "ref": "same", "target": "origin/main"},
                             {"profile": {}, "item_paths": [str(wt)]})
    assert not r["end"] and "同一个提交" in r["msg"], r


def test_h2_delivered_pointing_at_unrelated_places_does_not_count(env):
    d = stage_node(env)
    mkfile(env.home / "any/x.txt")
    for pat in ("/Applications/*.app", str(env.home / "*"), str(env.home / "any/*"), "/*"):
        ev = {"kind": "delivered", "glob": pat, "expect": 1}
        out = run_check(env, [node(d)], [bare("c1", [delete_item(d)], [ev])])
        assert out["c1"]["verdict_out"] == "decide", (pat, out["c1"])


def test_h2_delivered_dangling_symlink_is_not_counted(env):
    d = stage_node(env)
    root = env.work / "成品"
    root.mkdir()
    (root / "ghost.mp4").symlink_to(root / "missing.mp4")        # 指向根目录内、但不存在的文件
    prof = {"deliverable_roots": [str(root)]}
    r = plan.verify_evidence({"kind": "delivered", "glob": str(root / "*.mp4"), "expect": 1},
                             {"profile": prof, "item_paths": [str(d)]})
    assert not r["end"], r


def test_h2_copy_verified_two_unrelated_empty_files_does_not_count(env):
    repo = env.work / "r"
    (repo / "a").mkdir(parents=True)
    (repo / "b").mkdir()
    (repo / "a/__init__.py").write_text("")
    (repo / "b/__init__.py").write_text("")
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "i")
    wt = env.work / "proj-wt"
    mkfile(wt / "draft/final.psd")
    age(wt)
    ev = {"kind": "copy_verified", "src": str(repo / "a/__init__.py"), "dst": str(repo / "b/__init__.py")}
    out = run_check(env, [node(wt)], [bare("c1", [delete_item(wt)], [ev])])
    assert out["c1"]["verdict_out"] == "decide", out["c1"]
    assert any("src 不在任何待清理条目之内" in e for e in out["c1"]["errors"]), out["c1"]


def test_h2_legit_merged_copy_verified_and_delivered_stay_clean(env):
    repo = make_repo(env.work / "r")
    # merged：条目是分支 done 的工作树里的构建目录
    git(repo, "worktree", "add", "-q", str(env.work / "wt-done"), "done")
    b = env.work / "wt-done" / "build"
    mkfile(b / "a.bin")
    age(env.work / "wt-done")
    out = run_check(env, [node(b)], [bare("c1", [delete_item(b)], [{"kind": "merged", "repo": str(repo), "ref": "done"}])])
    assert out["c1"]["verdict_out"] == "clean", out["c1"]
    # 看板数据里带着通过复核的证据原文
    assert any(c.get("evidence", {}).get("kind") == "merged" and c["end"] for c in out["c1"]["evidence_checks"])
    # copy_verified：src 在待删条目里，dst 是仓里被跟踪的同内容文件
    gen = env.work / "gen"
    src = mkfile(gen / "a.png", b"payload" * 50)
    dst = mkfile(repo / "assets" / "a.png", b"payload" * 50)
    git(repo, "add", "assets/a.png")
    git(repo, "commit", "-q", "-m", "asset")
    age(gen)
    out = run_check(env, [node(gen)], [bare("c2", [delete_item(gen)],
                                            [{"kind": "copy_verified", "src": str(src), "dst": str(dst)}])])
    assert out["c2"]["verdict_out"] == "clean", out["c2"]
    # delivered：成品在 profile.deliverable_roots 内（待删条目之外）
    out_dir = env.work / "成品"
    mkfile(out_dir / "成片-1.mp4", b"v")
    d = stage_node(env)
    prof = {"deliverable_roots": [str(out_dir)]}
    r = plan.verify_evidence({"kind": "delivered", "glob": str(out_dir / "成片-*.mp4"), "expect": 1},
                             {"profile": prof, "item_paths": [str(d)]})
    assert r["ok"] and r["end"], r


# ============================================================ H3：免证据只认 safety == "safe"

def test_h3_xcode_archives_gradle_docker_raw_need_evidence(env):
    dev = env.home / "Library/Developer"
    mkfile(dev / "Xcode/Archives/2026-01-01/App.xcarchive/dSYMs/App.dSYM/x")
    g = env.home / ".gradle"
    mkfile(g / "gradle.properties", b"signing.password=x")
    mkfile(g / "caches/a")
    dock = env.home / "Library/Containers/com.docker.docker"
    raw = dock / "Data/vms/0/data/Docker.raw"
    mkfile(raw)
    for p in (dev, g, dock):
        age(p)
    nodes = [node(dev, producer=prod(dev)), dict(node(g, producer=prod(g)), id="n2"),
             dict(node(dock, producer=prod(dock)), id="n3")]
    out = run_check(env, nodes, [bare("c1", [delete_item(dev / "Xcode/Archives")]),
                                 bare("c2", [delete_item(g)]), bare("c3", [delete_item(raw)])])
    for cid in ("c1", "c2", "c3"):
        assert out[cid]["verdict_out"] == "decide", (cid, out[cid])


def test_h3_moderate_or_missing_safety_cache_card_is_not_exempt(env):
    for i, safety in enumerate(("moderate", None)):
        d = env.work / f"cache{i}"
        mkfile(d / "a.bin")
        age(d)
        n = node(d)
        n["producer"] = {"tool": "x", "card": "package-managers", "hint": None}
        if safety:
            n["producer"]["safety"] = safety
        out = run_check(env, [n], [bare("c1", [delete_item(d)])])
        assert out["c1"]["verdict_out"] == "decide", (safety, out["c1"])


def test_h3_safe_cache_with_zero_evidence_stays_clean(env):
    npm = env.home / ".npm/_cacache"
    mkfile(npm / "content-v2/a")
    age(npm)
    assert prod(npm)["safety"] == "safe"
    out = run_check(env, [node(npm, producer=prod(npm))], [bare("c1", [delete_item(npm)])])
    assert out["c1"]["verdict_out"] == "clean", out["c1"]
    assert plan.merged_producer(os.path.realpath(npm), [node(npm, producer=prod(npm))])["safe"]


# ============================================================ M1：没有样本可比的节点，补救只能软降级

def m1_nodes(env):
    proj = env.work / "proj"
    sub = proj / "sub"
    mkfile(sub / "a.bin")
    age(proj)
    n = node(proj, refs={"checked": False, "reason": "non-frontier", "count": 0, "samples": []})
    return n, sub


def test_m1_rescue_with_arbitrary_history_only_is_only_decide(env):
    n, sub = m1_nodes(env)
    out = run_check(env, [n], [card("c1", [delete_item(sub)], evidence=[reviewed("anything.md")]),
                               card("c2", [delete_item(sub)], verdict="decide", evidence=[reviewed("anything.md")],
                                    question="还要吗？")])
    c1 = out["c1"]
    assert c1["verdict_out"] == "decide" and c1["downgraded"], c1
    assert any(plan.UNVERIFIED_REFS_REASON in e for e in c1["errors"]), c1
    assert plan.UNVERIFIED_REFS_REASON in c1["question"], c1
    # 同样写成 decide 的卡：看板必须带这条提示
    c2 = out["c2"]
    assert c2["verdict_out"] == "decide" and any(plan.UNVERIFIED_REFS_REASON in r for r in c2["risk_notes"]), c2
    # 不再把这条复核当作“已匹配”：不会因此多出“样本无交集、视为伪造”的警告被吞掉或误报
    assert not any("视为伪造" in w for w in c1.get("warnings", [])), c1


def test_m1_rescue_uses_only_one_review_so_other_fake_reviews_still_warn(env):
    n, sub = m1_nodes(env)
    out = run_check(env, [n], [card("c1", [delete_item(sub)], evidence=[reviewed("a.md"), reviewed("b.md")])])
    c1 = out["c1"]
    assert c1["verdict_out"] == "decide", c1
    assert sum("视为伪造" in w for w in c1["warnings"]) == 1, c1      # 第二条没被一并吞掉


def test_m1_no_review_at_all_is_still_keep(env):
    n, sub = m1_nodes(env)
    out = run_check(env, [n], [card("c1", [delete_item(sub)])])
    assert out["c1"]["verdict_out"] == "keep", out["c1"]


# ============================================================ M2：缺 class 的会话按路径/命令推断

def test_m2_session_items_without_class_are_interactive(env):
    proj = env.home / ".claude/projects/-Users-x-proj"
    s1, s2 = proj / "aaaa.jsonl", proj / "bbbb.jsonl"
    mkfile(s1)
    mkfile(s2)
    age(proj)
    n = node(proj, producer=prod(proj))
    items = [delete_item(s1, scope="all"), delete_item(s2, scope="all")]          # 都没有 class
    assert plan.item_class(items[0]) == "interactive"
    out = run_check(env, [n], [card("c1", items, verdict="decide", question="删吗"),
                               card("c2", [delete_item(s1, scope="all")])])
    assert out["c1"]["verdict_out"] == "keep" and any("最多一个交互会话" in e for e in out["c1"]["errors"]), out["c1"]
    assert out["c2"]["verdict_out"] == "decide" and any("交互会话" in e for e in out["c2"]["errors"]), out["c2"]


def test_m2_codex_delete_and_sessions_dir_without_class_are_interactive(env):
    sess = env.home / ".codex/sessions/2026/10/rollout-x.jsonl"
    cdel = codex_item(sess, TID)
    assert "class" not in cdel and plan.item_class(cdel) == "interactive"
    assert plan.item_class(delete_item(sess)) == "interactive"
    arch = env.home / ".codex/archived_sessions/rollout-y.jsonl"
    assert plan.item_class(delete_item(arch)) == "interactive"
    assert len(plan.interactive_sessions([codex_item(sess, TID), codex_item(arch, TID2)])) == 2
    # 不是会话的路径不被推断成会话
    assert plan.item_class(delete_item(env.work / "x")) == "unknown"
    assert plan.item_class({"path": str(env.home / ".claude/projects/p/notes.md"), "method": "delete"}) == "unknown"


def test_m2_hand_written_batch_conflicting_with_session_shape_stays_interactive(env):
    proj = env.home / ".claude/projects/-Users-x-proj"
    s1 = proj / "aaaa.jsonl"
    mkfile(s1)
    hand = delete_item(s1, scope="all", **{"class": "batch"})
    assert plan.item_class(hand) == "interactive"
    assert plan.class_conflict(hand)
    # 探针 --emit 来源的 batch 仍信 batch
    assert plan.item_class(dict(hand, _emit=True)) == "batch"
    assert plan.class_conflict(dict(hand, _emit=True)) is None
    age(proj)
    out = run_check(env, [node(proj, producer=prod(proj))], [card("c1", [hand])])
    assert out["c1"]["verdict_out"] == "decide" and any("手写 class=batch" in w for w in out["c1"]["warnings"]), out["c1"]


def test_m2_hand_written_emit_marker_is_stripped_on_load(env):
    proj = env.home / ".claude/projects/-Users-x-proj"
    s1 = proj / "aaaa.jsonl"
    mkfile(s1)
    age(proj)
    hand = delete_item(s1, scope="all", **{"class": "batch", "_emit": True})
    env.write([node(proj, producer=prod(proj))], [card("c1", [hand])])
    loaded, _ = plan.load_plan(str(env.run))
    assert "_emit" not in loaded["cards"][0]["items"][0]


# ============================================================ M3：decide 卡里的风险提示也要给用户看

def test_m3_decide_card_with_risky_item_carries_risk_note_and_question(env):
    asup = env.home / "Library/Application Support"
    ff = asup / "Firefox"
    mkfile(ff / "Profiles/x/logins.json")
    age(asup)
    c = card("c1", [delete_item(ff)], verdict="decide", question="清理 Firefox 缓存？")
    out = run_check(env, [node(asup, producer=prod(asup))], [c])["c1"]
    assert out["verdict_out"] == "decide", out
    assert any(plan.RISKY_ZONE_REASON in r for r in out["risk_notes"]), out
    assert "账号数据或用户资产" in out["question"] and "清理 Firefox 缓存？" in out["question"], out
    assert any("风险提示" in w for w in out["warnings"]), out


def test_m3_h2_board_data_carries_risk_notes_and_verified_evidence(env):
    asup = env.home / "Library/Application Support"
    ff = asup / "Firefox"
    mkfile(ff / "Profiles/x/logins.json")
    age(asup)
    d = stage_node(env)
    gate = dict(END_EVIDENCE)      # 在一个临时 git 工作树内、待删条目之外（不依赖本 Skill 是否在 git 检出里）
    cards = [card("c1", [delete_item(ff)], verdict="decide", question="清理？"),
             bare("c2", [delete_item(d)], [gate])]
    env.write([node(asup, producer=prod(asup)), dict(node(d), id="n2")], cards)
    result = env.check()
    board = plan.board_plan(plan.read_json(str(env.run / "plan.json")), result)
    by = {c["id"]: c for c in board["cards"]}
    assert any(plan.RISKY_ZONE_REASON in r for r in by["c1"]["risk_notes"]), by["c1"]
    ver = by["c2"]["evidence_verified"]
    assert ver and ver[0]["evidence"]["kind"] == "delivered", by["c2"]
    html = (Path(__file__).resolve().parent.parent / "assets" / "dashboard.html").read_text(encoding="utf-8")
    assert "c.risk_notes" in html and "c.evidence_verified" in html


def test_board_condenses_risk_notes_and_maps_user_source():
    """看板每种风险原因只显示一行（去掉路径、计数），来源按 cards_used 第一项映射成用户认得的分类。"""
    notes = [f"/u/.codex/generated_images/t{i}（节点 n0031 /u/.codex/generated_images）：{plan.UNVERIFIED_REFS_REASON}（所在节点没有引用样本可比对，只能由你确认）"
             for i in range(104)] + [f"/u/x（节点 n1 /u）：{plan.RISKY_ZONE_REASON}"] \
        + [f"/u/upd{i}（节点 n2 /u）：更新暂存未确认是否已安装" for i in range(2)]
    rs = plan.risk_summary(notes)
    assert len(rs) == 3, rs
    assert {r["count"] for r in rs} == {104, 1, 2}
    assert {"text": "更新暂存未确认是否已安装", "count": 2} in rs, rs
    assert not any("/u/" in r["text"] or "节点" in r["text"] for r in rs), rs
    pl = {"cards": [{"id": "c1", "title": "t", "verdict": "decide", "question": "删吗？", "cards_used": ["codex"], "items": []},
                    {"id": "c3", "title": "t", "verdict": "keep", "category": "ai-images", "cards_used": ["codex"], "items": []},
                    {"id": "c4", "title": "t", "verdict": "keep", "category": "乱写", "cards_used": ["codex"], "items": []},
                    {"id": "c2", "title": "t", "verdict": "keep", "group": "某组", "cards_used": [], "items": []}]}
    res = {"cards": [{"id": "c1", "verdict_out": "decide", "question": "这批内容的引用没有被核实，你确认没有别的项目还在用它们吗？ 删吗？",
                      "risk_notes": notes[:3]}, {"id": "c2", "verdict_out": "keep"}, {"id": "c3", "verdict_out": "keep"}, {"id": "c4", "verdict_out": "keep"}]}
    board = plan.board_plan(pl, res)
    by = {c["id"]: c for c in board["cards"]}
    assert by["c1"]["category"] == "ai-chats" and by["c1"]["ask"] == "删吗？"
    assert by["c3"]["category"] == "ai-images" and by["c4"]["category"] == "ai-chats"   # 合法覆盖生效，乱写的不认
    assert {c["id"] for c in board["categories"]} >= {"ai-chats", "ai-images", "other"}
    assert by["c1"]["risk_summary"] == [{"text": plan._RISK_PLAIN[plan.UNVERIFIED_REFS_REASON], "count": 3}]
    assert by["c2"]["category"] == "other" and by["c2"]["source"] == "某组"
    html = (Path(__file__).resolve().parent.parent / "assets" / "dashboard.html").read_text(encoding="utf-8")
    assert "c.risk_summary" in html and "c.ask" in html and "catOf" in html


def test_board_preset_tiers_with_risky_floor():
    """一键选择三档：没写时 clean→稳妥、decide→推荐；写了按写的；碰账号数据的只进“全部可删”；保留卡不进任何档。"""
    risky = [f"/u/x（节点 n1 /u）：{plan.RISKY_ZONE_REASON}"]
    pl = {"cards": [
        {"id": "a", "verdict": "clean", "items": []},
        {"id": "b", "verdict": "decide", "question": "q", "items": []},
        {"id": "c", "verdict": "decide", "question": "q", "preset": "safe", "items": []},
        {"id": "d", "verdict": "decide", "question": "q", "preset": "safe", "items": []},
        {"id": "e", "verdict": "keep", "preset": "safe", "items": []},
        {"id": "f", "verdict": "decide", "question": "q", "preset": "乱写", "items": []}]}
    res = {"cards": [{"id": "a", "verdict_out": "clean"}, {"id": "b", "verdict_out": "decide"},
                     {"id": "c", "verdict_out": "decide"}, {"id": "d", "verdict_out": "decide", "risk_notes": risky},
                     {"id": "e", "verdict_out": "keep"}, {"id": "f", "verdict_out": "decide"}]}
    by = {c["id"]: c["preset"] for c in plan.board_plan(pl, res)["cards"]}
    assert by == {"a": "safe", "b": "recommended", "c": "safe", "d": "all", "e": None, "f": "recommended"}, by
    html = (Path(__file__).resolve().parent.parent / "assets" / "dashboard.html").read_text(encoding="utf-8")
    assert "c.preset" in html
