"""结束证据（evidence 对象形式）与 clean 卡降级为 decide 的闸门测试。

夹具全在 tmp_path 内（沿用 test_plan_act 的 Env）；git 仓用 git init 现建。
每种证据一个通过例、至少一个伪造例；卡片层断言「只有口述/字符串证据的 clean 卡会降为 decide」。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, age, card, delete_item, git, mkfile, node, plan  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def ctx(env, item_paths=(), profile=None):
    return {"profile": profile or {}, "item_paths": [str(p) for p in item_paths]}


def verify(env, ev, **kw):
    return plan.verify_evidence(ev, ctx(env, **kw))


def make_repo(env, name="repo"):
    """main 上一个提交；origin/main 指向它；另有未合并分支 feat 和已合并分支 done。"""
    repo = env.work / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    mkfile(repo / "base.txt", b"base\n")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "done")
    git(repo, "checkout", "-q", "-b", "feat")
    mkfile(repo / "feat.txt", b"feat\n")
    git(repo, "add", "feat.txt")
    git(repo, "commit", "-q", "-m", "feat")
    git(repo, "checkout", "-q", "main")
    mkfile(repo / "second.txt", b"second\n")
    git(repo, "add", "second.txt")
    git(repo, "commit", "-q", "-m", "second")
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    return repo


def add_wt(repo, branch, path):
    """给分支开一个工作树（待清理条目就是它）；merged 证据的 ref 必须是条目所在工作树当前检出的提交。"""
    git(repo, "worktree", "add", "-q", str(path), branch)
    return path


# ---------------------------------------------------------------- merged
# 条目是该仓里分支的工作树（P5.R H2：merged 证据必须和被删条目同仓、ref 是条目所在工作树当前检出的提交）

def test_merged_passes_when_branch_is_ancestor_of_origin_main(env):
    repo = make_repo(env)
    wt = add_wt(repo, "done", env.work / "wt-done")
    r = verify(env, {"kind": "merged", "repo": str(repo), "ref": "done"}, item_paths=[wt])
    assert r["ok"] and r["end"], r


def test_merged_fails_when_branch_not_merged(env):
    repo = make_repo(env)
    wt = add_wt(repo, "feat", env.work / "wt-feat")
    r = verify(env, {"kind": "merged", "repo": str(repo), "ref": "feat"}, item_paths=[wt])
    assert not r["ok"] and not r["end"] and "没有并入" in r["msg"], r


def test_merged_honors_explicit_target_and_rejects_bad_refs(env):
    repo = make_repo(env)
    wt = add_wt(repo, "feat", env.work / "wt-feat")
    git(repo, "merge", "-q", "--no-ff", "-m", "m", "feat")
    # 本地分支不是远端跟踪分支，不能当 target；换成另一个远端跟踪引用才行
    assert not verify(env, {"kind": "merged", "repo": str(repo), "ref": "feat", "target": "main"}, item_paths=[wt])["end"]
    git(repo, "update-ref", "refs/remotes/upstream/main", "main")
    assert verify(env, {"kind": "merged", "repo": str(repo), "ref": "feat", "target": "upstream/main"}, item_paths=[wt])["end"]
    # 默认 target 是 origin/main，它还停在合并前
    assert not verify(env, {"kind": "merged", "repo": str(repo), "ref": "feat"}, item_paths=[wt])["end"]
    for bad in ("--all", "no-such-branch", "", None):
        assert not verify(env, {"kind": "merged", "repo": str(repo), "ref": bad, "target": "upstream/main"},
                          item_paths=[wt])["end"], bad
    assert not verify(env, {"kind": "merged", "repo": str(env.work / "nope"), "ref": "done"}, item_paths=[wt])["end"]


def test_git_timeout_counts_as_not_passed(env, monkeypatch):
    repo = make_repo(env)
    wt = add_wt(repo, "done", env.work / "wt-done")
    real_run = subprocess.run

    def boom(cmd, *a, **kw):
        assert kw.get("timeout"), "外部命令必须带超时"
        if "merge-base" in cmd:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(plan.subprocess, "run", boom)
    r = verify(env, {"kind": "merged", "repo": str(repo), "ref": "done"}, item_paths=[wt])
    assert not r["ok"] and "无法复核" in r["msg"], r


# ---------------------------------------------------------------- delivered

def deliverable(env):
    """P5.R H2：delivered 的 glob 根目录必须在 profile 的 reference_roots / deliverable_roots（或 git 工作树）内。"""
    return {"deliverable_roots": [str(env.work / "成品"), str(env.work / "渲染")]}


def test_delivered_passes_when_count_reaches_expect(env):
    for i in range(3):
        mkfile(env.work / "成品" / f"成片-{i}.mp4", b"v")
    prof = deliverable(env)
    r = verify(env, {"kind": "delivered", "glob": str(env.work / "成品" / "成片-*.mp4"), "expect": 3}, profile=prof)
    assert r["ok"] and r["end"], r
    assert verify(env, {"kind": "delivered", "glob": str(env.work / "成品" / "成片-*.mp4"), "expect": 2}, profile=prof)["end"]


def test_delivered_fails_when_count_short_or_only_in_rejected(env):
    for i in range(2):
        mkfile(env.work / "成品" / f"成片-{i}.mp4", b"v")
    mkfile(env.work / "成品" / "rejected" / "成片-9.mp4", b"v")
    prof = deliverable(env)
    r = verify(env, {"kind": "delivered", "glob": str(env.work / "成品" / "**" / "成片-*.mp4"), "expect": 3}, profile=prof)
    assert not r["ok"] and not r["end"] and "只有 2 个" in r["msg"], r
    for bad in (0, -1, True, "3", None):
        assert not verify(env, {"kind": "delivered", "glob": str(env.work / "成品" / "*"), "expect": bad}, profile=prof)["end"], bad
    assert not verify(env, {"kind": "delivered", "glob": "relative/*", "expect": 1}, profile=prof)["end"]


def test_delivered_ignores_matches_inside_items_being_deleted(env):
    d = env.work / "渲染"
    for i in range(3):
        mkfile(d / f"成片-{i}.mp4", b"v")
    ev = {"kind": "delivered", "glob": str(d / "成片-*.mp4"), "expect": 3}
    assert verify(env, ev, profile=deliverable(env))["end"]
    assert not verify(env, ev, profile=deliverable(env), item_paths=[d])["end"]


# ---------------------------------------------------------------- copy_verified

def copy_case(env, content=b"payload" * 100):
    repo = make_repo(env)
    src = mkfile(env.work / "gen" / "a.png", content)
    dst = mkfile(repo / "assets" / "a.png", content)
    git(repo, "add", "assets/a.png")
    git(repo, "commit", "-q", "-m", "asset")
    return repo, src, dst


def test_copy_verified_passes_for_identical_tracked_copy(env):
    repo, src, dst = copy_case(env)
    r = verify(env, {"kind": "copy_verified", "src": str(src), "dst": str(dst)}, item_paths=[src.parent])
    assert r["ok"] and r["end"], r


def test_copy_verified_fails_when_hash_differs(env):
    repo, src, dst = copy_case(env)
    dst.write_bytes(b"PAYLOAD" * 100)    # 同样大小、内容不同
    assert dst.stat().st_size == src.stat().st_size
    r = verify(env, {"kind": "copy_verified", "src": str(src), "dst": str(dst)}, item_paths=[src.parent])
    assert not r["ok"] and "sha256" in r["msg"], r
    dst.write_bytes(b"short")
    assert "大小不同" in verify(env, {"kind": "copy_verified", "src": str(src), "dst": str(dst)}, item_paths=[src.parent])["msg"]


def test_copy_verified_fails_when_copy_in_rejected_dir(env):
    repo, src, _ = copy_case(env)
    bad = mkfile(repo / "rejected" / "a.png", src.read_bytes())
    git(repo, "add", "rejected/a.png")
    git(repo, "commit", "-q", "-m", "rej")
    r = verify(env, {"kind": "copy_verified", "src": str(src), "dst": str(bad)}, item_paths=[src.parent])
    assert not r["ok"] and "rejected" in r["msg"], r
    for word in ("tmp", "scratch", "temp"):
        d = mkfile(repo / word / "a.png", src.read_bytes())
        git(repo, "add", f"{word}/a.png")
        git(repo, "commit", "-q", "-m", word)
        assert not verify(env, {"kind": "copy_verified", "src": str(src), "dst": str(d)}, item_paths=[src.parent])["end"], word


def test_copy_verified_needs_tracked_or_deliverable_location(env):
    repo, src, _ = copy_case(env)
    loose = mkfile(repo / "untracked.png", src.read_bytes())          # 在仓里但没跟踪
    outside = mkfile(env.work / "elsewhere" / "a.png", src.read_bytes())  # 不在仓里
    items = [src.parent]
    assert not verify(env, {"kind": "copy_verified", "src": str(src), "dst": str(loose)}, item_paths=items)["end"]
    assert not verify(env, {"kind": "copy_verified", "src": str(src), "dst": str(outside)}, item_paths=items)["end"]
    prof = {"deliverable_roots": [str(env.work / "elsewhere")]}
    assert verify(env, {"kind": "copy_verified", "src": str(src), "dst": str(outside)}, profile=prof, item_paths=items)["end"]
    # 成品目录里的 rejected 子目录同样不算
    rej = mkfile(env.work / "elsewhere" / "rejected" / "a.png", src.read_bytes())
    assert not verify(env, {"kind": "copy_verified", "src": str(src), "dst": str(rej)}, profile=prof, item_paths=items)["end"]


def test_copy_verified_ignores_broad_deliverable_roots(env):
    """低9：手改 profile 塞进宽泛的成品目录（家目录本身、~/Downloads）时，副本落在里面不算“有副本”。"""
    repo, src, _ = copy_case(env)
    items = [src.parent]
    for broad in (env.home, env.home / "Downloads", env.home / "Documents"):
        broad.mkdir(exist_ok=True)
        copy = mkfile(broad / "loose-copy" / "a.png", src.read_bytes())
        assert not verify(env, {"kind": "copy_verified", "src": str(src), "dst": str(copy)},
                          profile={"deliverable_roots": [str(broad)]}, item_paths=items)["end"], broad


def test_copy_verified_rejects_hardlink_symlink_and_copy_inside_item(env):
    repo, src, dst = copy_case(env)
    link = env.work / "gen" / "hard.png"
    link.hardlink_to(src)
    assert not verify(env, {"kind": "copy_verified", "src": str(src), "dst": str(link)},
                      profile={"deliverable_roots": [str(env.work / "gen")]}, item_paths=[src])["end"]
    sym = repo / "assets" / "sym.png"
    sym.symlink_to(src)
    assert not verify(env, {"kind": "copy_verified", "src": str(src), "dst": str(sym)}, item_paths=[src])["end"]
    # 副本在要清理的目录里：清完就没了
    assert not verify(env, {"kind": "copy_verified", "src": str(src), "dst": str(dst)}, item_paths=[src, repo])["end"]


# ---------------------------------------------------------------- user_stated / 字符串 / 畸形

def test_user_stated_string_and_malformed_are_never_end_evidence(env):
    assert verify(env, {"kind": "user_stated", "text": "她说做完了"}) == {
        "kind": "user_stated", "ok": True, "end": False, "msg": "用户口述，不算结束证据"}
    assert not verify(env, "成片 16 条已上线")["end"]
    for bad in ({"kind": "bogus"}, {"text": "x"}, 5, None, ["merged"]):
        r = verify(env, bad)
        assert not r["ok"] and not r["end"], bad


# ---------------------------------------------------------------- 卡片层：clean 无结束证据 → decide

def one_card(env, evidence, producer_card=None, verdict="clean", d=None, safety="safe", **kw):
    d = d or env.work / "stage"
    mkfile(d / "a.bin")
    age(d)
    n = node(d)
    if producer_card:
        # P5.R H3：只有 producer.safety == "safe" 才免结束证据
        n["producer"] = {"tool": "x", "card": producer_card, "hint": None}
        if safety:
            n["producer"]["safety"] = safety
    c = card("c01", [delete_item(d)], verdict=verdict, **kw)
    c["evidence"] = evidence       # 绕开旧 helper 自带的通过证据
    env.write([n], [c])
    return d


def out_card(res):
    return res["cards"][0]


def test_clean_with_only_user_stated_is_downgraded_to_decide(env):
    one_card(env, [{"kind": "user_stated", "text": "做完了"}])
    res = env.check()
    c = out_card(res)
    assert c["verdict_out"] == "decide" and c["downgraded"], c
    assert any("结束" in e for e in c["errors"]), c
    assert c["question"], "降级成 decide 必须给用户一个问题"
    assert res["counts"] == {"decide": 1}
    env.seal()
    sealed = json.loads((env.run / "seal.json").read_text())
    assert sealed["cards"]["c01"]["verdict"] == "decide"


def test_clean_with_only_string_evidence_is_downgraded_to_decide(env):
    one_card(env, ["成片 16 条已上线，上线提交 ee769e9494", "引用已核对: 只是历史记录里提了一句"])
    assert out_card(env.check())["verdict_out"] == "decide"


def test_clean_with_failed_object_evidence_is_downgraded_and_says_why(env):
    repo = make_repo(env)
    one_card(env, [{"kind": "merged", "repo": str(repo), "ref": "feat"}], d=add_wt(repo, "feat", env.work / "stage"))
    c = out_card(env.check())
    assert c["verdict_out"] == "decide"
    assert any("没有并入" in e for e in c["errors"]), c
    assert c["evidence_checks"][0]["ok"] is False


def test_clean_with_verified_end_evidence_stays_clean(env):
    repo = make_repo(env)
    d = one_card(env, [{"kind": "merged", "repo": str(repo), "ref": "done"}, {"kind": "user_stated", "text": "x"}],
                 d=add_wt(repo, "done", env.work / "wt-done") / "build")      # 工作树里的未跟踪构建目录
    c = out_card(env.check())
    assert c["verdict_out"] == "clean" and not c["downgraded"], c
    digest = env.seal()
    assert env.act(cards="c01", digest=digest) == 0
    assert not d.exists()


def test_regenerable_cache_cards_do_not_need_end_evidence(env):
    one_card(env, [], producer_card="package-managers")
    assert out_card(env.check())["verdict_out"] == "clean"
    # moderate 或没写 safety 的缓存卡不再整卡豁免（P5.R H3）：要结束证据，否则 decide
    for safety in ("moderate", None):
        one_card(env, [], producer_card="package-managers", safety=safety)
        assert out_card(env.check())["verdict_out"] == "decide", safety
    # 风险档 risky 的缓存目录不豁免
    d = env.work / "stage"
    n = node(d)
    n["producer"] = {"tool": "x", "card": "browsers", "hint": None, "safety": "risky"}
    c = card("c01", [delete_item(d)])
    c["evidence"] = []
    env.write([n], [c])
    assert out_card(env.check())["verdict_out"] == "decide"


def test_non_clean_verdicts_are_not_touched(env):
    one_card(env, [], verdict="decide", question="还要吗？")
    c = out_card(env.check())
    assert c["verdict_out"] == "decide" and not c["downgraded"], c


def test_native_cache_commands_exempt_but_session_deletes_need_end_evidence():
    brew = {"path": "/x", "method": "native", "argv": ["brew", "cleanup"]}
    uid = "00000000-0000-7000-8000-0123456789ab"
    codex = {"path": "/x", "method": "native", "argv": ["codex", "delete", "--force", uid]}
    grok = {"path": "/x", "method": "native", "argv": ["grok", "sessions", "delete", uid]}
    assert plan._item_needs_end_evidence(brew, []) is False
    assert plan._item_needs_end_evidence(codex, []) is True
    assert plan._item_needs_end_evidence(grok, []) is True
