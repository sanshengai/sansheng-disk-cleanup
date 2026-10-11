"""plan.py / act.py 的关键闸门测试。

全部夹具在 tmp_path 内：HOME、protect.HOME、profile、runs 目录都指向临时目录，
不触碰真实家目录；夹具里不创建任何 SKILL.md。
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import act  # noqa: E402
import codex_state  # noqa: E402
import plan  # noqa: E402
import protect  # noqa: E402

REAL_OPEN_FILES = act.open_files
OLD = time.time() - 10 * 86400


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    "-C", str(cwd), *args], check=True, capture_output=True)


def node(path, **kw):
    n = {"id": "n1", "path": str(path), "bytes": 1000, "files": 1, "newest_mtime": "2026-01-01T00:00:00Z",
         "recent_24h": 0, "children_over_threshold": 0, "remainder_bytes": 0,
         "git": {"repo": None, "tracked_files": 0, "role": "none"}, "open_by": [],
         "refs": {"checked": True, "count": 0, "samples": []},
         "producer": {"tool": None, "card": None, "hint": None}, "protected": False}
    n.update(kw)
    return n


# 旧用例关心的是别的闸门；clean 卡现在要「任务已结束」的复核证据，这里统一补一条必然通过的。
# delivered 要求成品落在 git 工作树（或 reference_roots / deliverable_roots）内，所以在 ~/.cache 下建一个只含一个成品的
# 临时仓库，不依赖本 Skill 自己是否放在 git 检出里；进程结束时删除。结束证据规则本身见 test_evidence.py。
def _make_deliverable():
    import atexit
    import tempfile
    cache = os.path.expanduser("~/.cache")
    os.makedirs(cache, exist_ok=True)
    root = tempfile.mkdtemp(prefix="sansheng-cleanup-deliverable-", dir=cache)
    atexit.register(shutil.rmtree, root, True)
    subprocess.run(["git", "init", "-q", root], check=True)
    f = os.path.join(root, "成品.txt")
    with open(f, "w", encoding="utf-8") as h:
        h.write("done\n")
    return os.path.realpath(f)


END_EVIDENCE = {"kind": "delivered", "glob": _make_deliverable(), "expect": 1}


def reviewed(*samples, verdict="history_only"):
    """结构化引用核对证据（样本须取自 facts 里节点的 refs.samples；未检查的可补救节点无样本可比，任意合法样本即可）。"""
    return {"kind": "refs_reviewed", "samples": list(samples), "verdict": verdict}


def card(cid, items, verdict="clean", evidence=None, question=None):
    return {"id": cid, "title": f"卡片 {cid}", "group": "g", "verdict": verdict, "bytes": 1000,
            "summary": "s", "evidence": list(evidence or []) + [END_EVIDENCE], "impact": "i", "question": question,
            "cards_used": [], "items": items}


def age(path):
    """把目录树内所有文件的 mtime 调到 10 天前，避免被 24 小时规则挡住。"""
    for dp, dns, fns in os.walk(path):
        for n in fns + [x for x in dns if os.path.islink(os.path.join(dp, x))]:
            p = os.path.join(dp, n)
            os.utime(p, (OLD, OLD), follow_symlinks=False)


def mkfile(path, data=b"x" * 5000):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def tree(root):
    out = {}
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            p = os.path.join(dp, n)
            st = os.lstat(p)
            out[p] = (st.st_ino, st.st_size, st.st_mtime_ns)
    return out


def auto_sel(argv):
    """试点与正式执行必须带 --sel（P5.R H4）：旧用例没有看板，按本次 --cards/--answers 现算一个补上。"""
    if "--sel" in argv:
        return argv
    val = lambda flag: argv[argv.index(flag) + 1] if flag in argv else ""
    answers = []
    if "--answers" in argv:
        i = argv.index("--answers") + 1
        while i < len(argv) and not argv[i].startswith("--"):
            answers.append(argv[i])
            i += 1
    seal, err = act.load_seal(os.path.realpath(val("--run")), val("--seal"))
    if err:
        return argv
    req = [c.strip() for c in val("--cards").split(",") if c.strip()]
    ans = act.parse_answers(answers)
    sel = plan.selection_sig(seal["digest"], [c for c, _, _ in act.select_cards(seal, req, ans)], ans)
    return argv + ["--sel", sel]


def with_gate(argv):
    """正式执行闸门（预演 + 试点）不是多数旧用例要测的东西：先静默补一次同选择的预演，再用 --skip-pilot 跳过试点。
    闸门本身见 test_run_gate.py。"""
    import contextlib
    import io
    argv = auto_sel(argv)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        act.main(argv + ["--dry-run"])
    return argv + ["--skip-pilot", "--reason", "test"]


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.home = (tmp_path / "home").resolve()
        self.home.mkdir()
        self.work = self.home / "work"
        self.work.mkdir()
        (self.home / "Documents").mkdir()
        self.run = (tmp_path / "runs" / "scan1").resolve()
        self.run.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setattr(protect, "HOME", str(self.home))
        monkeypatch.setattr(protect, "STATE_DIR", str(self.home / ".sansheng" / "disk-cleanup"))
        monkeypatch.setattr(protect, "PROFILE", str(self.home / ".sansheng" / "disk-cleanup" / "profile.json"))
        monkeypatch.setattr(act, "open_files", lambda p: [])
        monkeypatch.setattr(act, "free_bytes", lambda: 10 ** 9)
        self.mp = monkeypatch

    # 事实与计划
    def write(self, nodes, cards, scan_id="scan1"):
        (self.run / "facts.json").write_text(json.dumps(
            {"schema": "facts/1", "scan_id": scan_id, "created_at": "2026-10-10T00:00:00Z",
             "disk": {"total": 10, "free": 5, "used": 5}, "coverage": {"incomplete": False},
             "nodes": nodes, "skipped": []}), encoding="utf-8")
        (self.run / "plan.json").write_text(json.dumps(
            {"schema": "plan/1", "scan_id": scan_id, "cards": cards, "governance": []}, ensure_ascii=False),
            encoding="utf-8")

    def check(self):
        assert plan.main(["check", "--run", str(self.run)]) in (0, 1)
        return json.loads((self.run / "plan.check.json").read_text())

    def seal(self):
        assert plan.main(["seal", "--run", str(self.run)]) == 0
        return json.loads((self.run / "seal.json").read_text())["digest"]

    def act(self, cards="", answers=None, dry=False, digest=None):
        if digest is None:
            digest = self.seal()
        argv = ["run", "--run", str(self.run), "--seal", digest, "--cards", cards]
        if answers:
            argv += ["--answers", *answers]
        if dry:
            argv.append("--dry-run")
        else:
            argv = with_gate(argv)
        return act.main(argv)

    def receipt(self):
        return json.loads((self.run / "receipt.json").read_text())

    def forge(self, cards):
        """绕过 check 直接伪造封存，用来验证 act 端自己的复核。cards: {id: (verdict, [item dict])}"""
        out = {}
        for cid, (verdict, items) in cards.items():
            recs = []
            for i, it in enumerate(items):
                st = os.lstat(it["path"])
                rec = {"index": i, "realpath": os.path.realpath(it["path"]), "ino": st.st_ino,
                       "dev": st.st_dev, "bytes": 1000}
                rec.update(it)
                recs.append(rec)
            out[cid] = {"title": cid, "verdict": verdict, "items": recs}
        seal = {"schema": "seal/1", "scan_id": "scan1", "created_at": "t", "cards": out,
                "profile_guard": protect.profile_guard(plan.load_profile())}
        seal["digest"] = plan.digest_of(seal)
        (self.run / "seal.json").write_text(json.dumps(seal, ensure_ascii=False))
        return seal["digest"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def delete_item(path, scope="all", **kw):
    d = {"path": str(path), "method": "delete", "scope": scope}
    d.update(kw)
    return d


def make_repo(env):
    """repo/build 里：被 git 跟踪的 tracked.txt（强制加入的被忽略路径）+ 两个未跟踪的被忽略文件。"""
    repo = env.work / "proj"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / ".gitignore").write_text("build/\n")
    mkfile(repo / "src" / "a.py", b"print(1)\n")
    mkfile(repo / "build" / "tracked.txt", b"tracked\n")
    git(repo, "add", ".gitignore", "src")
    git(repo, "add", "-f", "build/tracked.txt")
    git(repo, "commit", "-q", "-m", "init")
    mkfile(repo / "build" / "x.bin")
    mkfile(repo / "build" / "sub" / "y.bin")
    age(repo)
    return repo


# ---------------------------------------------------------------- 保护路径

def test_check_rejects_protected_path_and_accepts_normal(env):
    mkfile(env.home / "Documents" / "x" / "a.txt")
    mkfile(env.work / "cache" / "a.bin")
    age(env.home)
    env.write([node(env.home / "Documents" / "x"), node(env.work / "cache", id="n2")], [
        card("c01", [delete_item(env.home / "Documents" / "x")]),
        card("c02", [delete_item(env.work / "cache")]),
    ])
    res = env.check()
    by = {c["id"]: c for c in res["cards"]}
    assert by["c01"]["verdict_out"] == "keep" and by["c01"]["downgraded"]
    assert any("保护" in e for e in by["c01"]["errors"])
    assert by["c02"]["verdict_out"] == "clean" and not by["c02"]["errors"]


def test_check_rejects_ancestor_of_protected_path(env):
    mkfile(env.home / ".claude" / "projects" / "p" / "memory" / "m.md")
    mkfile(env.home / ".claude" / "other" / "o.bin")
    age(env.home)
    env.write([node(env.home / ".claude")], [card("c01", [delete_item(env.home / ".claude")])])
    res = env.check()
    assert res["cards"][0]["verdict_out"] == "keep"
    assert any("保护" in e for e in res["cards"][0]["errors"])


def test_act_skips_protected_path_even_if_forged_seal(env):
    f = mkfile(env.home / "Documents" / "x" / "a.txt")
    mem = mkfile(env.home / ".claude" / "projects" / "p" / "memory" / "m.md")
    age(env.home)
    d = env.forge({"c01": ("clean", [delete_item(env.home / "Documents" / "x"),
                                     delete_item(env.home / ".claude")])})
    assert env.act("c01", digest=d) == 0
    assert f.exists() and mem.exists()
    items = env.receipt()["cards"][0]["items"]
    assert [i["status"] for i in items] == ["skipped", "skipped"]
    assert all("保护" in i["reason"] for i in items)


def test_act_unlinks_symlink_but_never_follows_it(env):
    target = mkfile(env.home / "Documents" / "keep.txt")
    d = env.work / "cache"
    mkfile(d / "a.bin")
    os.symlink(env.home / "Documents", d / "link-to-docs")
    os.symlink(target, d / "link-to-file")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    assert env.act("c01") == 0
    assert target.exists() and (env.home / "Documents").is_dir()
    assert not d.exists()


def test_check_rejects_item_that_is_itself_a_symlink(env):
    real = env.work / "real"
    mkfile(real / "a.bin")
    os.symlink(real, env.work / "lnk")
    env.write([node(env.work / "lnk")], [card("c01", [delete_item(env.work / "lnk")])])
    assert env.check()["cards"][0]["verdict_out"] == "keep"


# ---------------------------------------------------------------- git 范围

def test_ignored_only_keeps_tracked_files(env):
    repo = make_repo(env)
    env.write([node(repo / "build", git={"repo": str(repo), "tracked_files": 1, "role": "mixed"})],
              [card("c01", [delete_item(repo / "build", "ignored-only")])])
    assert env.act("c01") == 0
    assert (repo / "build" / "tracked.txt").exists()
    assert not (repo / "build" / "x.bin").exists()
    assert not (repo / "build" / "sub").exists()          # 变空的目录被清掉
    assert (repo / "src" / "a.py").exists() and (repo / ".git").is_dir()
    r = env.receipt()["cards"][0]["items"][0]
    assert r["status"] == "removed" and r["removed"]["files"] == 2


def test_ignored_only_never_deletes_tracked_even_if_listing_is_wrong(env):
    repo = make_repo(env)
    tracked = str(repo / "build" / "tracked.txt")
    real_list = act.list_ignored
    env.mp.setattr(act, "list_ignored", lambda rp: real_list(rp) + [tracked])
    env.write([node(repo / "build", git={"repo": str(repo), "tracked_files": 1, "role": "mixed"})],
              [card("c01", [delete_item(repo / "build", "ignored-only")])])
    assert env.act("c01") == 0
    assert os.path.exists(tracked)
    assert not (repo / "build" / "x.bin").exists()


def test_ignored_only_outside_git_repo_is_skipped(env):
    mkfile(env.work / "plain" / "a.bin")
    age(env.work)
    env.write([node(env.work / "plain")], [card("c01", [delete_item(env.work / "plain", "ignored-only")])])
    assert env.act("c01") == 0
    assert (env.work / "plain" / "a.bin").exists()
    assert "不在 git 仓库内" in env.receipt()["cards"][0]["items"][0]["reason"]


def test_scope_all_refused_when_tracked_files_present(env):
    repo = make_repo(env)
    # check 层：事实里写明有跟踪文件 -> 降级
    env.write([node(repo / "build", git={"repo": str(repo), "tracked_files": 1, "role": "mixed"})],
              [card("c01", [delete_item(repo / "build", "all")])])
    assert env.check()["cards"][0]["verdict_out"] == "keep"
    # act 层：事实说谎（tracked_files=0）也要在现场拒绝
    env.write([node(repo / "build")], [card("c01", [delete_item(repo / "build", "all")])])
    assert env.check()["cards"][0]["verdict_out"] == "clean"
    assert env.act("c01") == 0
    assert (repo / "build" / "tracked.txt").exists() and (repo / "build" / "x.bin").exists()
    r = env.receipt()["cards"][0]["items"][0]
    assert r["status"] == "skipped" and "跟踪" in r["reason"] and "ignored-only" in r["reason"]


def test_scope_all_removes_untracked_dir_completely(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    mkfile(d / "deep" / "er" / "b.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    assert env.act("c01") == 0
    assert not d.exists()
    r = env.receipt()["cards"][0]["items"][0]
    assert r["status"] == "removed" and r["removed"]["files"] == 2 and r["removed"]["dirs"] == 3


def test_scope_all_refuses_nested_git_repo(env):
    d = env.work / "bundle"
    mkfile(d / "a.bin")
    (d / "inner" / ".git").mkdir(parents=True)
    mkfile(d / "inner" / "f.txt")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    assert env.act("c01") == 0
    assert (d / "inner" / "f.txt").exists() and (d / "a.bin").exists()
    assert "git 仓库" in env.receipt()["cards"][0]["items"][0]["reason"]


# ---------------------------------------------------------------- 封存后变化

def test_replaced_path_skipped_when_inode_changes(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    digest = env.seal()
    os.rename(d, env.work / "cache.old")            # 旧 inode 仍然存在，新目录必得到新 inode
    mkfile(d / "new.bin")
    age(d)
    assert env.act("c01", digest=digest) == 0
    assert (d / "new.bin").exists() and (env.work / "cache.old" / "a.bin").exists()
    assert "ino" in env.receipt()["cards"][0]["items"][0]["reason"]


def test_recent_file_blocks_whole_item(env):
    d = env.work / "cache"
    mkfile(d / "old.bin")
    age(d)
    mkfile(d / "fresh.bin")                         # mtime = 现在
    env.write([node(d)], [card("c01", [delete_item(d)])])
    assert env.act("c01") == 0
    assert (d / "old.bin").exists() and (d / "fresh.bin").exists()
    assert "24 小时" in env.receipt()["cards"][0]["items"][0]["reason"]
    # 正例：同一目录文件变旧后可以删
    os.utime(d / "fresh.bin", (OLD, OLD))
    assert env.act("c01") == 0
    assert not d.exists()


def test_open_by_process_and_unknown_open_state_skip(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    digest = env.seal()
    env.mp.setattr(act, "open_files", lambda p: [str(d / "a.bin")])
    assert env.act("c01", digest=digest) == 0
    assert (d / "a.bin").exists() and "打开" in env.receipt()["cards"][0]["items"][0]["reason"]
    env.mp.setattr(act, "open_files", lambda p: None)
    assert env.act("c01", digest=digest) == 0
    assert (d / "a.bin").exists() and "无法确认是否被打开" in env.receipt()["cards"][0]["items"][0]["reason"]


def test_open_files_uses_lsof_with_timeout_and_no_shell(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    calls = []

    def fake(cmd, *a, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, f"p1\nn{d}/a.bin\n", "")

    env.mp.setattr(subprocess, "run", fake)
    assert REAL_OPEN_FILES(str(d)) == [f"{d}/a.bin"]
    assert calls[0][0] == ["lsof", "-n", "-P", "-F", "n", "+D", str(d)]
    assert calls[0][1].get("shell") is not True and calls[0][1]["timeout"] == 60

    def boom(cmd, *a, **kw):
        raise subprocess.TimeoutExpired(cmd, 60)

    env.mp.setattr(subprocess, "run", boom)
    assert REAL_OPEN_FILES(str(d)) is None


# ---------------------------------------------------------------- digest

def test_digest_mismatch_rejects_everything(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    digest = env.seal()
    assert env.act("c01", digest="000000000000") == 2
    assert (d / "a.bin").exists() and not (env.run / "receipt.json").exists()
    # 篡改 seal.json 内容但保留 digest 字段：同样拒绝
    seal = json.loads((env.run / "seal.json").read_text())
    seal["cards"]["c01"]["items"][0]["path"] = str(env.home / "Documents")
    (env.run / "seal.json").write_text(json.dumps(seal))
    assert env.act("c01", digest=digest) == 2
    assert (d / "a.bin").exists() and (env.home / "Documents").is_dir()
    # 正例：未改动时通过
    env.write([node(d)], [card("c01", [delete_item(d)])])
    assert env.act("c01") == 0 and not d.exists()


# ---------------------------------------------------------------- native

def test_native_check_whitelist(env):
    d = env.home / ".npm"                       # npm cache clean 的 path 必须恰好是 ~/.npm（形状表规则）
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d)], [
        card("c01", [{"path": str(d), "method": "native", "argv": ["npm", "cache", "clean"]}]),
        card("c02", [{"path": str(d), "method": "native", "argv": ["rm", "-rf", str(d)]}]),
        card("c03", [{"path": str(d), "method": "native", "argv": ["/tmp/evil/npm", "x"]}]),
        card("c04", [{"path": str(d), "method": "native", "argv": ["sh", "-c", "rm -rf ~"]}]),
    ])
    by = {c["id"]: c["verdict_out"] for c in env.check()["cards"]}
    assert by == {"c01": "clean", "c02": "keep", "c03": "keep", "c04": "keep"}


def test_native_runs_argv_verbatim_without_shell(env):
    d = env.home / "Library" / "Android" / "sdk" / "system-images" / "android-31" / "android-tv" / "arm64-v8a"
    mkfile(d / "a.bin")
    age(d)
    argv = ["sdkmanager", "--uninstall", "system-images;android-31;android-tv;arm64-v8a"]
    env.write([node(d)], [card("c01", [{"path": str(d), "method": "native", "argv": argv}])])
    digest = env.seal()
    calls = []

    def fake(cmd, *a, **kw):
        calls.append((cmd, kw))
        os.remove(d / "a.bin")                     # 模拟 sdkmanager 真的卸载了
        return subprocess.CompletedProcess(cmd, 0, "done", "")

    env.mp.setattr(subprocess, "run", fake)
    assert env.act("c01", digest=digest) == 0
    assert len(calls) == 1
    cmd, kw = calls[0]
    assert cmd == argv and isinstance(cmd, list)
    assert kw.get("shell") is not True
    assert env.receipt()["cards"][0]["items"][0]["status"] == "removed"


def test_native_not_in_whitelist_is_refused_by_act(env):
    d = env.work / "sdk"
    mkfile(d / "a.bin")
    age(d)
    digest = env.forge({"c01": ("clean", [{"path": str(d), "method": "native", "argv": ["rm", "-rf", str(d)]},
                                          {"path": str(d), "method": "native", "argv": ["/tmp/x/npm", "a"]}])})
    calls = []
    env.mp.setattr(subprocess, "run", lambda cmd, *a, **kw: calls.append(cmd))
    assert env.act("c01", digest=digest) == 0
    assert calls == [] and (d / "a.bin").exists()
    assert [i["status"] for i in env.receipt()["cards"][0]["items"]] == ["skipped", "skipped"]


def test_native_nonzero_exit_is_recorded_as_failed(env):
    d = env.home / ".npm"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d)], [card("c01", [{"path": str(d), "method": "native",
                                        "argv": ["npm", "cache", "clean", "--force"]}])])
    digest = env.seal()
    env.mp.setattr(subprocess, "run", lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 3, "", "boom"))
    assert env.act("c01", digest=digest) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "failed" and "3" in it["failed"][0]["error"]


# ---------------------------------------------------------------- 引用证据

def test_refs_without_evidence_downgrades_to_keep(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    refs = {"checked": True, "count": 2, "samples": ["a.md:1"]}
    env.write([node(d, refs=refs)], [
        card("c01", [delete_item(d)]),
        card("c02", [delete_item(d)], evidence=[reviewed("a.md:1")]),
        card("c03", [delete_item(d)], evidence=["随便写的证据"]),
    ])
    by = {c["id"]: c for c in env.check()["cards"]}
    assert by["c01"]["verdict_out"] == "keep" and any("引用" in e for e in by["c01"]["errors"])
    assert by["c02"]["verdict_out"] == "clean"
    assert by["c03"]["verdict_out"] == "keep"


def test_refs_unchecked_cannot_be_rescued_by_evidence(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    refs = {"checked": False, "count": 0, "samples": []}
    env.write([node(d, refs=refs)], [card("c01", [delete_item(d)], evidence=[reviewed("a.md:1")])])
    assert env.check()["cards"][0]["verdict_out"] == "keep"


def test_non_frontier_exact_node_rescued_only_by_evidence(env):
    d = env.work / "build"
    mkfile(d / "big" / "a.bin")
    age(d)
    nf = {"checked": False, "count": 0, "samples": [], "reason": "non-frontier"}
    env.write([node(d, refs=nf), node(d / "big", id="n2")],
              [card("c01", [delete_item(d)]),
               card("c02", [delete_item(d)], evidence=[reviewed("build/x.log:1")])])
    # P5.R M1：没有 samples 可比对的节点，refs_reviewed 只能软降级成 decide，不再算已核对
    res = env.check()["cards"]
    assert [x["verdict_out"] for x in res] == ["keep", "decide"]
    assert any("引用没有被核实" in e for e in res[1]["errors"])


def test_recent_or_open_nodes_downgrade_in_check(env):
    a, b, c = env.work / "a", env.work / "b", env.work / "c"
    for p in (a, b, c):
        mkfile(p / "f.bin")
    env.write([node(a, recent_24h=3), node(b, open_by=[{"pid": 1, "command": "x"}], id="n2"),
               node(c, open_by="unknown", id="n3")],
              [card("c01", [delete_item(a)]), card("c02", [delete_item(b)]), card("c03", [delete_item(c)])])
    assert [x["verdict_out"] for x in env.check()["cards"]] == ["keep", "keep", "keep"]


def test_deep_item_not_blocked_by_busy_ancestor_but_checked_live(env):
    """祖先目录被监视、兄弟文件刚写过：深层的旧文件仍可清；深层条目自己刚改过则降级。"""
    d = env.work / "sessions"
    old, busy = mkfile(d / "old.jsonl"), mkfile(d / "live.jsonl")
    age(d)
    os.utime(busy, None)
    fresh = mkfile(d / "fresh.jsonl")
    anc = node(d, recent_24h=2, open_by=[{"pid": 1, "command": "CodexBar"}])
    env.write([anc], [card("c01", [delete_item(old)]), card("c02", [delete_item(fresh)])])
    res = env.check()
    assert [x["verdict_out"] for x in res["cards"]] == ["clean", "keep"]
    assert "24 小时内" in res["cards"][1]["errors"][0]


def test_deep_item_under_unchecked_ancestor_needs_evidence(env):
    d = env.work / "images"
    f = mkfile(d / "t1" / "a.png")
    age(d)
    anc = node(d, refs={"checked": False, "count": 0, "samples": [], "reason": "non-frontier"})
    env.write([anc], [card("c01", [delete_item(d / "t1")]),
                      card("c02", [delete_item(d / "t1")], evidence=[reviewed("x.md:1")])])
    assert [x["verdict_out"] for x in env.check()["cards"]] == ["keep", "decide"]     # P5.R M1：软降级
    assert f.exists()


def test_scan_id_mismatch_fails_whole_plan(env):
    mkfile(env.work / "a" / "f.bin")
    env.write([node(env.work / "a")], [card("c01", [delete_item(env.work / "a")])])
    plan_json = json.loads((env.run / "plan.json").read_text())
    plan_json["scan_id"] = "other"
    (env.run / "plan.json").write_text(json.dumps(plan_json))
    assert plan.main(["seal", "--run", str(env.run)]) == 1
    assert not (env.run / "seal.json").exists()


# ---------------------------------------------------------------- decide

def test_decide_card_requires_explicit_delete_answer(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d)], [card("c02", [delete_item(d)], verdict="decide", question="还要吗？")])
    digest = env.seal()
    assert env.act("c02", digest=digest) == 0                       # 没有答复
    assert d.exists() and "没有收到答复" in env.receipt()["cards"][0]["reason"]
    assert env.act("c02", answers=["c02=留"], digest=digest) == 0   # 答复留
    assert d.exists()
    assert env.act("", answers=["c02=删"], digest=digest) == 0      # 只在 --answers 里给出「删」
    assert not d.exists()
    assert env.receipt()["cards"][0]["status"] == "executed"


def test_decide_card_without_question_is_downgraded(env):
    mkfile(env.work / "a" / "f.bin")
    env.write([node(env.work / "a")], [card("c02", [delete_item(env.work / "a")], verdict="decide")])
    assert env.check()["cards"][0]["verdict_out"] == "keep"


def test_unsealed_or_unknown_card_is_not_executed(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)]), card("c09", [], verdict="keep")])
    digest = env.seal()
    assert env.act("c09,c77", digest=digest) == 0
    assert d.exists()
    assert [c["status"] for c in env.receipt()["cards"]] == ["skipped", "skipped"]


# ---------------------------------------------------------------- dry-run / receipt

def test_dry_run_changes_nothing(env, capsys):
    repo = make_repo(env)
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(repo / "build"), node(d, id="n2")],
              [card("c01", [delete_item(repo / "build", "ignored-only"), delete_item(d)])])
    digest = env.seal()
    before = tree(env.home)
    run_before = sorted(os.listdir(env.run))
    assert env.act("c01", dry=True, digest=digest) == 0
    assert tree(env.home) == before
    after = sorted(os.listdir(env.run))
    assert [n for n in after if n not in run_before and not n.startswith("receipt-dryrun-")] == []   # 只多出预演回执
    assert len([n for n in after if n.startswith("receipt-dryrun-")]) == 1
    assert not (env.run / "receipt.json").exists()
    out = capsys.readouterr().out
    assert "dry-run" in out and "将删除" in out
    assert env.act("c01", digest=digest) == 0                     # 对照：真跑会删
    assert not d.exists()


def test_receipt_records_removed_skipped_failed_and_measured_bytes(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    mkfile(d / "keepme.txt")
    mkfile(d / "ro" / "stuck.bin")
    age(d)
    stuck_dir = d / "ro"
    other = env.work / "fresh"
    mkfile(other / "f.bin")                                       # 新文件 -> 整条跳过
    env.write([node(d), node(other, id="n2")],
              [card("c01", [delete_item(d, keep=["keepme.txt"]), delete_item(other)])])
    digest = env.seal()
    values = iter([0, 0, 1000, 5000])                             # 前两个被 with_gate 补的预演用掉
    env.mp.setattr(act, "free_bytes", lambda: next(values))
    os.chmod(stuck_dir, 0o500)                                    # 目录不可写 -> unlink 失败
    try:
        assert env.act("c01", digest=digest) == 0
    finally:
        os.chmod(stuck_dir, 0o700)
    rc = env.receipt()
    first, second = rc["cards"][0]["items"]
    assert first["status"] == "partial"
    assert first["removed"]["files"] == 1 and first["removed"]["bytes"] > 0
    assert first["failed_count"] == 1 and first["failed"][0]["path"].endswith("stuck.bin")
    assert first["kept_count"] >= 1
    assert second["status"] == "skipped" and "24 小时" in second["reason"]
    s = rc["summary"]
    assert s["freed_measured"] == 4000 and s["free_before"] == 1000 and s["free_after"] == 5000
    assert s["bytes_by_files"] == first["removed"]["bytes"]
    assert s["difference"] == 4000 - s["bytes_by_files"] and s["difference_note"]
    assert s["failed_entries"] == 1 and s["skipped_items"] == 1
    assert (d / "keepme.txt").exists() and (stuck_dir / "stuck.bin").exists() and not (d / "a.bin").exists()
    assert act.main(["status", "--run", str(env.run)]) == 0


# ---------------------------------------------------------------- keep / files

def test_keep_entries_are_preserved(env):
    d = env.work / "cache"
    mkfile(d / "important.txt")
    mkfile(d / "docs" / "guide.md")
    mkfile(d / "sub" / "important.txt")                            # 按文件名匹配，各层都保留
    mkfile(d / "tmp.bin")
    mkfile(d / "sub" / "junk.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d, keep=["important.txt", "docs"])])])
    assert env.act("c01") == 0
    assert (d / "important.txt").exists() and (d / "sub" / "important.txt").exists()
    assert (d / "docs" / "guide.md").exists()
    assert not (d / "tmp.bin").exists() and not (d / "sub" / "junk.bin").exists()


def test_files_scope_skips_changed_files(env):
    d = env.work / "cache"
    f1, f2, f3, f4 = (mkfile(d / n) for n in ("f1.bin", "f2.bin", "f3.bin", "f4.bin"))
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d, "files", files=[str(f1), str(f2), str(f3)])])])
    digest = env.seal()
    with open(f1, "ab") as fh:                                     # size 变
        fh.write(b"more")
    os.utime(f1, (OLD, OLD))
    os.utime(f2, (OLD + 100, OLD + 100))                           # 只有 mtime 变
    assert env.act("c01", digest=digest) == 0
    assert f1.exists() and f2.exists() and f4.exists()             # 变化的跳过，未列出的不动
    assert not f3.exists()
    it = env.receipt()["cards"][0]["items"][0]
    assert it["removed"]["files"] == 1 and it["skipped_count"] == 2 and it["status"] == "partial"


def test_files_scope_rejects_file_outside_item_and_directory(env):
    d = env.work / "cache"
    mkfile(d / "f1.bin")
    outside = mkfile(env.work / "outside.bin")
    env.write([node(d)], [
        card("c01", [delete_item(d, "files", files=[str(outside)])]),
        card("c02", [delete_item(d, "files", files=[str(d)])]),
        card("c03", [delete_item(d, "files", files=[])]),
    ])
    assert [c["verdict_out"] for c in env.check()["cards"]] == ["keep", "keep", "keep"]


# ---------------------------------------------------------------- 杂项

def test_parse_answers():
    assert act.parse_answers(["c02=删", "/", "c05=留"]) == {"c02": "删", "c05": "留"}
    assert act.parse_answers(["c02=删,c05=留"]) == {"c02": "删", "c05": "留"}
    assert act.parse_answers(None) == {}


def test_board_injects_json_and_escapes(env, tmp_path):
    nodes = [node(env.work / f"d{i}", id=f"n{i}", bytes=i + 1, producer={"card": "codex"}) for i in range(60)]
    for i in range(60):
        mkfile(env.work / f"d{i}" / "f.bin")
    c = card("c01", [delete_item(env.work / "d5")])
    c["summary"] = "</script><b>x</b>"
    env.write(nodes, [c])
    digest = env.seal()
    tpl = tmp_path / "tpl.html"
    tpl.write_text('<img src="__LOGO_DATA__"><script>const D = __PLAN_JSON__;</script>', encoding="utf-8")
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG")
    env.mp.setattr(plan, "TEMPLATE", str(tpl))
    env.mp.setattr(plan, "LOGO", str(logo))
    assert plan.main(["board", "--run", str(env.run)]) == 0
    html = (env.run / "board.html").read_text()
    assert "data:image/png;base64," in html and "</script><b>" not in html
    data = json.loads(html.split("const D = ", 1)[1].rsplit(";</script>", 1)[0])
    assert data["seal"]["digest"] == digest and data["plan"]["cards"][0]["summary"] == "</script><b>x</b>"
    fr = data["facts"]["frontier"]
    assert len(fr) == 40 and fr[0]["bytes"] == 60
    assert set(fr[0]) == {"path", "bytes", "producer", "refs", "recent_24h"}
    assert data["check"]["cards"][0]["verdict_out"] == "clean"
    env.mp.setattr(plan, "TEMPLATE", str(tmp_path / "missing.html"))
    assert plan.main(["board", "--run", str(env.run)]) == 2


def test_board_shows_checked_verdict_and_truncates_items(env):
    a, b = env.work / "a", env.work / "b"
    files = [mkfile(a / f"f{i}.bin") for i in range(30)]
    mkfile(b / "x.bin")
    age(env.work)
    env.write([node(a), node(b, recent_24h=1, id="n2")],
              [card("c01", [delete_item(a, "files", files=[str(f) for f in files])] +
                    [delete_item(a / f"f{i}.bin") for i in range(25)]),
               card("c02", [delete_item(b)])])
    result, _ = plan.check_plan(str(env.run))
    bp = plan.board_plan(json.loads((env.run / "plan.json").read_text()), result)
    c1, c2 = bp["cards"]
    assert c2["verdict"] == "keep" and c2["downgraded"] and "24 小时内" in c2["check_errors"][0]
    assert c1["items_total"] == 26 and len(c1["items"]) == plan.BOARD_ITEMS
    assert "files" not in c1["items"][0] and c1["items"][0]["files_total"] == 30


def test_board_bytes_come_from_checked_items_not_plan(env):
    """0.4 试用：Agent 没在 plan.json 写 bytes（或写错）时，看板仍显示封存条目的实测体积。"""
    a = env.work / "a"
    mkfile(a / "x.bin")
    age(env.work)
    c = card("c01", [delete_item(a)])
    c["bytes"] = 1
    env.write([node(a, bytes=123456)], [c])
    result, records = plan.check_plan(str(env.run))
    bp = plan.board_plan(json.loads((env.run / "plan.json").read_text()), result)
    expected = sum(int(r.get("bytes") or 0) for r in records["c01"]["items"])
    assert expected > 1 and bp["cards"][0]["bytes"] == expected


def test_protection_is_case_insensitive(env):
    for p in ("documents/x", "DOCUMENTS", ".SSH/id", ".Config/a", "work/repo/.GIT/HEAD"):
        assert protect.protected_reason(str(env.home / p)) is not None, p
    assert protect.protected_reason(str(env.home / "work" / "cache")) is None
    prof = {"protect": [str(env.home / "Keep")]}
    assert protect.protected_reason(str(env.home / "keep" / "x"), prof) is not None


def test_files_scope_never_deletes_tracked_file(env):
    repo = make_repo(env)
    t, x = repo / "build" / "tracked.txt", repo / "build" / "x.bin"
    item = delete_item(repo / "build", "files", files=[str(t), str(x)])
    st = {str(f): os.lstat(f) for f in (t, x)}
    for f in item["files"]:
        pass
    digest = env.forge({"c01": ("clean", [dict(item, files=[
        {"path": str(f), "size": st[str(f)].st_size, "mtime_ns": st[str(f)].st_mtime_ns} for f in (t, x)])])})
    env.act("c01", digest=digest)
    assert t.exists() and not x.exists()


def test_ancestor_refs_timeout_cannot_be_rescued_by_evidence(env):
    d = env.work / "big"
    f = mkfile(d / "sub" / "a.bin")
    age(d)
    anc = node(d, refs={"checked": False, "count": 0, "samples": [], "reason": "timeout-or-error"})
    env.write([anc], [card("c01", [delete_item(d / "sub")], evidence=[reviewed("a.md:1")])])
    assert env.check()["cards"][0]["verdict_out"] == "keep"


def test_files_scope_bytes_counts_only_listed_files(env):
    d = env.work / "mix"
    a = mkfile(d / "a.bin", b"x" * 100000)
    mkfile(d / "b.bin", b"x" * 900000)
    age(d)
    env.write([node(d, bytes=10 ** 6)], [card("c01", [delete_item(d, "files", files=[str(a)])])])
    _, recs = plan.check_plan(str(env.run))
    assert recs["c01"]["items"][0]["bytes"] == os.lstat(a).st_blocks * 512


# ---------------------------------------------------------------- items_file / codex 对应 / 试点 / 历史库回收

TID = "00000000-0000-7000-8000-00000000000a"


def test_items_file_is_expanded_into_seal(env):
    d = env.work / "a"
    mkfile(d / "x.bin")
    age(d)
    (env.run / "items").mkdir()
    (env.run / "items" / "g.json").write_text(json.dumps([delete_item(d)]))
    c = card("c01", [])
    c["items_file"] = "items/g.json"
    env.write([node(d)], [c])
    assert env.check()["counts"] == {"clean": 1}
    seal = json.loads((env.run / "seal.json").read_text()) if env.seal() else None
    assert [i["path"] for i in seal["cards"]["c01"]["items"]] == [str(d)]


def test_items_file_outside_run_dir_is_rejected(env):
    d = env.work / "a"
    mkfile(d / "x.bin")
    age(d)
    outside = env.work / "evil.json"
    outside.write_text(json.dumps([delete_item(d)]))
    c1 = card("c01", [])
    c1["items_file"] = "../../home/work/evil.json"
    c2 = card("c02", [])
    c2["items_file"] = str(outside)
    env.write([node(d)], [c1, c2])
    res = env.check()
    assert res["counts"] == {"keep": 2}
    assert all(c["downgraded"] for c in res["cards"])


def rollout(env, tid, parent=None, day="03"):
    """在假 ~/.codex/sessions 里造一个会话文件；parent 给出时写进 session_meta（子 Agent）。"""
    p = env.home / ".codex" / "sessions" / "2026" / "10" / day / f"rollout-2026-10-{day}T00-00-00-{tid}.jsonl"
    meta = {"id": tid}
    if parent:
        meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
    mkfile(p, (json.dumps({"type": "session_meta", "payload": meta}) + "\n" + "x" * 5000).encode())
    os.utime(p, (OLD, OLD))
    codex_state._cache.clear()
    return p


def codex_item(path, tid, session=None):
    # P5.R M2：codex 会话删除按形状推断为 interactive，一个会话（含后代线程）共用同一个 session
    return {"path": str(path), "method": "native", "argv": ["codex", "delete", "--force", tid], "session": session or tid}


def dcard(cid, items):
    """交互会话只能是 decide 卡（P5.R M2：缺 class 也按 interactive），用户答“删”后才执行。"""
    return card(cid, items, verdict="decide", question="这个会话不再需要了吗？")


TID2 = "00000000-0000-7000-8000-00000000000b"
TID3 = "00000000-0000-7000-8000-00000000000c"


def test_codex_delete_id_must_match_path(env):
    good = rollout(env, TID)
    other = rollout(env, TID2)
    img = env.home / ".codex" / "generated_images" / TID
    mkfile(img / "a.png")
    stray = mkfile(env.home / ".codex" / "tmp" / f"rollout-x-{TID}.jsonl")   # 在 ~/.codex 里，但不在会话目录
    age(env.home)
    base = env.home / ".codex"
    env.write([node(base)], [
        dcard("c01", [codex_item(good, TID)]),
        card("c02", [codex_item(other, TID)]),                                         # 号与文件不对应
        card("c03", [{"path": str(good), "method": "native", "argv": ["codex", "delete", "--force", "01a0fd72"]}]),
        card("c04", [{"path": str(good), "method": "native", "argv": ["codex", "delete", "我的会话"]}]),
        card("c05", [{"path": str(good), "method": "native",
                      "argv": ["codex", "-c", "k=v", "delete", "--force", TID]}]),      # 子命令前夹带全局参数
        card("c06", [codex_item(img, TID)]),                                           # path 写成了图目录
        card("c07", [codex_item(stray, TID)]),                                         # 不在会话目录里
    ])
    res = {c["id"]: c["verdict_out"] for c in env.check()["cards"]}
    assert res == {"c01": "decide", "c02": "keep", "c03": "keep", "c04": "keep", "c05": "keep",
                   "c06": "keep", "c07": "keep"}


def test_codex_parent_needs_children_listed_first(env):
    parent = rollout(env, TID)
    child = rollout(env, TID2, parent=TID)
    grand = rollout(env, TID3, parent=TID2)
    age(env.home)
    base = env.home / ".codex"
    env.write([node(base)], [
        card("c01", [codex_item(parent, TID)]),                                          # 漏了子孙
        card("c02", [codex_item(parent, TID), codex_item(child, TID2, TID), codex_item(grand, TID3, TID)]),   # 顺序反了
        dcard("c03", [codex_item(grand, TID3, TID), codex_item(child, TID2, TID), codex_item(parent, TID)]),
    ])
    res = {c["id"]: c["verdict_out"] for c in env.check()["cards"]}
    assert res == {"c01": "keep", "c02": "keep", "c03": "decide"}


def test_codex_parent_skipped_when_child_survives(env):
    parent = rollout(env, TID)
    child = rollout(env, TID2, parent=TID)
    age(env.home)
    env.write([node(env.home / ".codex")], [dcard("c01", [codex_item(child, TID2, TID), codex_item(parent, TID)])])
    digest = env.seal()
    os.utime(child, None)                         # 封存后子会话又被用了
    calls = []
    env.mp.setattr(subprocess, "run", lambda cmd, *a, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    env.mp.setattr(act, "reclaim_codex_history", lambda home=None: [])
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    st = [it["status"] for it in env.receipt()["cards"][0]["items"]]
    assert st == ["skipped", "skipped"] and calls == []
    assert "子会话" in env.receipt()["cards"][0]["items"][1]["reason"]


def test_codex_delete_success_but_file_remains_is_failure(env):
    f = rollout(env, TID)
    age(env.home)
    env.write([node(env.home / ".codex")], [dcard("c01", [codex_item(f, TID)])])
    digest = env.seal()
    env.mp.setattr(subprocess, "run", lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 0, "Deleted", ""))
    env.mp.setattr(act, "reclaim_codex_history", lambda home=None: [])
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    rc = env.receipt()
    assert rc["cards"][0]["items"][0]["status"] == "failed"
    assert rc["summary"]["freed_by_items"] == 0


def test_pilot_executes_only_one_item_and_keeps_main_receipt(env):
    a, b = env.work / "a", env.work / "b"
    mkfile(a / "x.bin")
    mkfile(b / "y.bin")
    age(env.work)
    c = env.work / "c"
    mkfile(c / "z.bin")
    age(env.work)
    env.write([node(a, id="n1"), node(b, id="n2"), node(c, id="n3")],
              [card("c01", [delete_item(a), delete_item(c)]), card("c02", [delete_item(b)])])
    digest = env.seal()
    assert act.main(auto_sel(["run", "--run", str(env.run), "--seal", digest, "--cards", "c01,c02", "--pilot"])) == 0
    assert not a.exists() and b.exists() and c.exists()
    assert not (env.run / "receipt.json").exists()
    assert len(list(env.run.glob("receipt-pilot-*.json"))) == 1
    assert env.act("c01,c02", digest=digest) == 0      # 正式执行：已删的跳过，其余照常
    assert not b.exists() and not c.exists()
    st = [it["status"] for cd in env.receipt()["cards"] for it in cd["items"]]
    assert st == ["skipped", "removed", "removed"]


def _history_db(home, auto_vacuum):
    import sqlite3
    db = home / ".codex" / "thread_history_1.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute(f"PRAGMA auto_vacuum={auto_vacuum}")
    con.execute("CREATE TABLE thread_items (thread_id TEXT, item_json TEXT)")
    con.executemany("INSERT INTO thread_items VALUES (?, ?)", [("t", "x" * 100000)] * 100)
    con.commit()
    con.execute("DELETE FROM thread_items")
    con.commit()
    con.close()
    return db


def test_reclaim_history_shrinks_incremental_db(env):
    db = _history_db(env.home, "INCREMENTAL")
    before = db.stat().st_size
    env.mp.setattr(act, "VACUUM_PAGES", 100)
    [r] = act.reclaim_codex_history(str(env.home))
    assert r["error"] is None and r["integrity"] == "ok" and r["batches"] > 1
    assert db.stat().st_size < before / 5 and r["reclaimed"] == before - db.stat().st_size


def test_reclaim_history_refuses_full_vacuum(env):
    db = _history_db(env.home, "NONE")
    before = db.stat().st_size
    [r] = act.reclaim_codex_history(str(env.home))
    assert "未自动执行" in r["error"] and db.stat().st_size == before
    assert r["batches"] == 0


def test_native_codex_delete_counts_measured_bytes_and_reclaims(env):
    f = rollout(env, TID)
    age(env.home)
    size = os.lstat(f).st_blocks * 512
    env.write([node(env.home / ".codex")], [dcard("c01", [codex_item(f, TID)])])
    digest = env.seal()

    def fake(cmd, *a, **kw):
        os.remove(f)
        return subprocess.CompletedProcess(cmd, 0, "Deleted", "")

    env.mp.setattr(subprocess, "run", fake)
    calls = []
    env.mp.setattr(act, "reclaim_codex_history", lambda home=None: calls.append(1) or [
        {"db": "h", "size_before": 10, "size_after": 4, "reclaimed": 6, "batches": 1, "error": None,
         "integrity": "ok"}])
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    s = env.receipt()["summary"]
    assert calls == [1]
    assert s["bytes_native"] == size == s["bytes_by_files"] and s["history_reclaimed"] == 6
    assert s["freed_by_items"] == size + 6


def test_no_reclaim_without_codex_delete(env):
    d = env.work / "a"
    mkfile(d / "x.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    env.mp.setattr(act, "reclaim_codex_history", lambda home=None: pytest.fail("不应回收"))
    assert env.act("c01") == 0
    assert "history_reclaim" not in env.receipt()


def test_reclaim_history_stops_when_no_progress(env, monkeypatch):
    import sqlite3
    _history_db(env.home, "INCREMENTAL")
    real = sqlite3.connect

    class Stuck:
        def __init__(self, *a, **kw):
            self.c = real(*a, **kw)

        def execute(self, sql, *a):
            if sql.startswith("PRAGMA incremental_vacuum"):
                return self.c.execute("SELECT 1")   # 模拟被锁住、一页也没回收
            return self.c.execute(sql, *a)

        def close(self):
            self.c.close()

    monkeypatch.setattr(sqlite3, "connect", Stuck)
    monkeypatch.setattr(act.time, "sleep", lambda s: None)
    [r] = act.reclaim_codex_history(str(env.home))
    assert r["batches"] == 1 and "停滞" in r["error"]


def test_reclaim_history_in_wal_mode_shrinks_main_plus_wal(env):
    import sqlite3
    db = env.home / ".codex" / "thread_history_1.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("PRAGMA auto_vacuum=INCREMENTAL")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE thread_items (thread_id TEXT, item_json TEXT)")
    con.executemany("INSERT INTO thread_items VALUES (?, ?)", [("t", "x" * 100000)] * 200)
    con.commit()
    con.execute("DELETE FROM thread_items")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    before = act._db_size(str(db))
    env.mp.setattr(act, "VACUUM_PAGES", 500)
    env.mp.setattr(act.time, "sleep", lambda s: None)
    [r] = act.reclaim_codex_history(str(env.home))
    assert r["journal_mode"] == "wal" and r["error"] is None and r["integrity"] == "ok"
    assert act._db_size(str(db)) < before / 5 and r["reclaimed"] > 0


# ---------------------------------------------------------------- 原生命令形状表（P1.1）
# 每条登记形状至少一个通过例；反例在 plan check 与 act 执行前复核两处都必须被拒。

SHAPE_OK = [   # (形状 id, argv, 家目录下的 path, path 是文件而不是目录)
    ("sdkmanager-uninstall", ["sdkmanager", "--uninstall", "system-images;android-31;android-tv;arm64-v8a"],
     "Library/Android/sdk/system-images/android-31/android-tv/arm64-v8a", False),
    ("avdmanager-delete-avd", ["avdmanager", "delete", "avd", "-n", "Pixel_6"], ".android/avd/Pixel_6.avd", False),
    ("brew-cleanup", ["brew", "cleanup"], "Library/Caches/Homebrew", False),
    ("brew-cleanup", ["brew", "cleanup", "-s", "--prune=30"], "Library/Caches/Homebrew", False),
    ("brew-cleanup", ["brew", "cleanup", "--prune=all"], "Library/Caches/Homebrew", False),
    ("simctl-delete-unavailable", ["xcrun", "simctl", "delete", "unavailable"],
     "Library/Developer/CoreSimulator/Devices", False),
    ("hf-cache-prune", ["hf", "cache", "prune", "-y"], ".cache/huggingface", False),
    ("hf-cache-rm", ["hf", "cache", "rm", "model/org/name", "--yes"], ".cache/huggingface/hub/models--org--name", False),
    ("hf-cache-rm", ["hf", "cache", "rm", "dataset/glue", "-y"], ".cache/huggingface/hub/datasets--glue", False),
    ("ollama-rm", ["ollama", "rm", "llama3:8b"], ".ollama/models/manifests/registry.ollama.ai/library/llama3/8b", True),
    ("ollama-rm", ["ollama", "rm", "llama3"], ".ollama/models/manifests/registry.ollama.ai/library/llama3/latest", True),
    ("ollama-rm", ["ollama", "rm", "someone/tool:q4"], ".ollama/models/manifests/registry.ollama.ai/someone/tool/q4", True),
    ("ollama-rm", ["ollama", "rm", "hf.co/org/model:Q4_K_M"], ".ollama/models/manifests/hf.co/org/model/Q4_K_M", True),
    ("npm-cache-clean", ["npm", "cache", "clean", "--force"], ".npm", False),
    ("npm-cache-verify", ["npm", "cache", "verify"], ".npm", False),
    ("pnpm-store-prune", ["pnpm", "store", "prune"], "Library/pnpm/store", False),
    ("bun-pm-cache-rm", ["bun", "pm", "cache", "rm"], ".bun/install/cache", False),
    ("uv-cache-prune", ["uv", "cache", "prune", "--ci"], ".cache/uv", False),
    ("uv-cache-clean", ["uv", "cache", "clean"], ".cache/uv", False),
    ("pip-cache-purge", ["pip", "cache", "purge"], "Library/Caches/pip", False),
    ("pip3-cache-purge", ["pip3", "cache", "purge"], "Library/Caches/pip", False),
    ("docker-builder-prune", ["docker", "builder", "prune", "-f", "--filter=until=168h"],
     "Library/Containers/com.docker.docker", False),
    ("grok-sessions-delete", ["grok", "sessions", "delete", TID], f".grok/sessions/%2Ftmp%2Fproj/{TID}", False),
]


def _make_target(env, rel, is_file):
    p = env.home / rel
    if is_file:
        mkfile(p)
        os.utime(p, (OLD, OLD))
    else:
        mkfile(p / "a.bin")
        age(p)
    return p


def test_every_registered_shape_has_a_passing_example():
    covered = {sid for sid, *_ in SHAPE_OK} | {"codex-delete"}      # codex 的通过例在上面的 codex_* 测试里
    assert covered == {sh.sid for sh in protect.SHAPES}
    assert len({sh.sid for sh in protect.SHAPES}) == len(protect.SHAPES)


@pytest.mark.parametrize("sid,argv,rel,is_file", SHAPE_OK, ids=[f"{c[0]}:{' '.join(c[1][1:])[:40]}" for c in SHAPE_OK])
def test_registered_shape_passes_check_and_runs_in_act(env, sid, argv, rel, is_file):
    target = _make_target(env, rel, is_file)
    assert protect.native_match(argv)[0].sid == sid
    # P5.R：会话删除（grok）按形状是 interactive、avd 是用户资产（risky）：这两类只能是 decide 卡，用户答“删”才执行
    item = {"path": str(target), "method": "native", "argv": argv}
    if sid == "grok-sessions-delete":
        item["session"] = TID
    env.write([node(target)], [card("c01", [item])])
    verdict = {c["id"]: c["verdict_out"] for c in env.check()["cards"]}["c01"]
    assert verdict == ("decide" if sid in ("grok-sessions-delete", "avdmanager-delete-avd") else "clean")
    digest = env.seal()
    calls = []

    def fake(cmd, *a, **kw):
        if cmd and cmd[0] == "pgrep":           # 进程闸门（docker 等）：按“没在运行”回答
            return subprocess.CompletedProcess(cmd, 1, "", "")
        calls.append((cmd, kw))
        shutil.rmtree(target) if target.is_dir() else os.remove(target)
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    env.mp.setattr(subprocess, "run", fake)
    assert env.act("c01", digest=digest, answers=["c01=删"] if verdict == "decide" else None) == 0
    assert [c[0] for c in calls] == [argv] and calls[0][1].get("shell") is not True
    assert env.receipt()["cards"][0]["items"][0]["status"] == "removed"


NOT_REGISTERED = True          # 期望报错里带"形状未登记"（而不是 path 对应错误）
NEG = [   # (说明, argv, 家目录下的 path, 报错是否应为"形状未登记")
    ("docker-system-prune-volumes", ["docker", "system", "prune", "-a", "--volumes"], "Library/Containers/com.docker.docker/Data", True),
    ("docker-volume-prune", ["docker", "volume", "prune"], "Library/Containers/com.docker.docker/Data", True),
    ("docker-image-prune-a", ["docker", "image", "prune", "-a", "-f"], "Library/Containers/com.docker.docker/Data", True),
    ("docker-builder-prune-all", ["docker", "builder", "prune", "-a", "-f"], "Library/Containers/com.docker.docker/Data", True),
    ("docker-builder-prune-no-force", ["docker", "builder", "prune"], "Library/Containers/com.docker.docker/Data", True),
    ("brew-uninstall", ["brew", "uninstall", "foo"], "Library/Caches/Homebrew", True),
    ("brew-cleanup-formula-arg", ["brew", "cleanup", "wget"], "Library/Caches/Homebrew", True),
    ("brew-autoremove", ["brew", "autoremove"], "Library/Caches/Homebrew", True),
    ("npm-exec", ["npm", "exec", "foo"], ".npm", True),
    ("npx", ["npx", "foo"], ".npm", True),
    ("npm-install-g", ["npm", "install", "-g", "foo"], ".npm", True),
    ("pip-install", ["pip", "install", "foo"], "Library/Caches/pip", True),
    ("pip3-uninstall", ["pip3", "uninstall", "-y", "foo"], "Library/Caches/pip", True),
    ("yarn-cache-clean", ["yarn", "cache", "clean"], "Library/Caches/Yarn", True),
    ("claude-purge-all", ["claude", "purge", "--all", "--yes"], ".claude", True),
    ("simctl-delete-all", ["xcrun", "simctl", "delete", "all"], "Library/Developer/CoreSimulator/Devices", True),
    ("xcrun-other-tool", ["xcrun", "clang", "-o", "x"], "Library/Developer/CoreSimulator/Devices", True),
    ("uv-run", ["uv", "run", "python", "-c", "print(1)"], ".cache/uv", True),
    ("hf-download", ["hf", "download", "org/name"], ".cache/huggingface", True),
    ("hf-cache-prune-needs-yes", ["hf", "cache", "prune"], ".cache/huggingface/hub", True),
    ("hf-cache-rm-revision-hash", ["hf", "cache", "rm", "8f3ad1c90fed7a62", "-y"], ".cache/huggingface/hub", True),
    ("hf-cache-rm-two-repos", ["hf", "cache", "rm", "model/a/b", "model/c/d", "-y"], ".cache/huggingface/hub", True),
    ("huggingface-cli-delete-cache", ["huggingface-cli", "delete-cache"], ".cache/huggingface/hub", True),
    ("ollama-rm-two-models", ["ollama", "rm", "a", "b"], ".ollama/models/manifests", True),
    ("ollama-run", ["ollama", "run", "llama3"], ".ollama", True),
    ("ollama-rm-dash-arg", ["ollama", "rm", "--help"], ".ollama", True),
    ("sdkmanager-install", ["sdkmanager", "--install", "platform-tools"], "Library/Android/sdk/platform-tools", True),
    ("sdkmanager-two-packages", ["sdkmanager", "--uninstall", "ndk;1", "ndk;2"], "Library/Android/sdk/ndk/1", True),
    ("sdkmanager-extra-option", ["sdkmanager", "--uninstall", "ndk;1", "--sdk_root=/"], "Library/Android/sdk/ndk/1", True),
    ("sdkmanager-dotdot", ["sdkmanager", "--uninstall", "ndk;..;..;Documents"], "Library/Android/sdk/ndk", True),
    ("avdmanager-dotdot-name", ["avdmanager", "delete", "avd", "-n", "../x"], ".android/avd", True),
    ("avdmanager-create", ["avdmanager", "create", "avd", "-n", "x", "-k", "k"], ".android/avd", True),
    ("shell", ["sh", "-c", "rm -rf ~"], ".npm", True),
    ("rm", ["rm", "-rf", "~/.npm"], ".npm", True),
    # 合法命令 + 一个未登记选项
    ("npm-cache-clean-extra-option", ["npm", "cache", "clean", "--force", "--prefix=/tmp/x"], ".npm", True),
    ("brew-cleanup-dry-run-option", ["brew", "cleanup", "--prune=30", "--dry-run"], "Library/Caches/Homebrew", True),
    ("brew-cleanup-bad-prune-value", ["brew", "cleanup", "--prune=30; rm"], "Library/Caches/Homebrew", True),
    ("uv-cache-clean-force", ["uv", "cache", "clean", "--force"], ".cache/uv", True),
    ("uv-cache-clean-package", ["uv", "cache", "clean", "numpy"], ".cache/uv", True),
    ("uv-cache-prune-cache-dir", ["uv", "cache", "prune", "--cache-dir", "/"], ".cache/uv", True),
    ("hf-cache-prune-duplicate-yes", ["hf", "cache", "prune", "-y", "--yes"], ".cache/huggingface/hub", True),
    ("hf-cache-prune-cache-dir", ["hf", "cache", "prune", "-y", "--cache-dir", "/"], ".cache/huggingface/hub", True),
    ("docker-builder-prune-bad-filter", ["docker", "builder", "prune", "-f", "--filter=label=x"],
     "Library/Containers/com.docker.docker/Data", True),
    ("pnpm-store-prune-dir-option", ["pnpm", "store", "prune", "--store-dir=/"], "Library/pnpm/store", True),
    ("codex-extra-option", ["codex", "delete", "--force", TID, "--all"], f".codex/sessions/2026/10/03/rollout-x-{TID}.jsonl", True),
    ("grok-sessions-delete-all", ["grok", "sessions", "delete", "all"], ".grok/sessions", True),
    # 程序名带路径
    ("abs-path-brew", ["/usr/bin/brew", "cleanup"], "Library/Caches/Homebrew", True),
    ("abs-path-brew-opt", ["/opt/homebrew/bin/brew", "cleanup"], "Library/Caches/Homebrew", True),
    ("rel-path-npm", ["./npm", "cache", "verify"], ".npm", True),
    ("tmp-evil-npm", ["/tmp/evil/npm", "cache", "verify"], ".npm", True),
    # 命令形状对，但 path 与对象/数据目录不对应
    ("ollama-rm-model-path-mismatch", ["ollama", "rm", "llama3:8b"],
     ".ollama/models/manifests/registry.ollama.ai/library/llama3/70b", False),
    ("ollama-rm-path-is-models-root", ["ollama", "rm", "llama3:8b"], ".ollama/models", False),
    ("ollama-rm-path-is-blobs", ["ollama", "rm", "llama3:8b"], ".ollama/models/blobs", False),
    ("ollama-rm-default-tag-mismatch", ["ollama", "rm", "llama3"],
     ".ollama/models/manifests/registry.ollama.ai/library/llama3/8b", False),
    ("sdkmanager-package-path-mismatch", ["sdkmanager", "--uninstall", "ndk;25.1"], "Library/Android/sdk/ndk/26.0", False),
    ("sdkmanager-path-outside-sdk", ["sdkmanager", "--uninstall", "ndk;25.1"], "work/ndk/25.1", False),
    ("avdmanager-path-mismatch", ["avdmanager", "delete", "avd", "-n", "Pixel_6"], ".android/avd/Pixel_7.avd", False),
    ("avdmanager-path-is-avd-root", ["avdmanager", "delete", "avd", "-n", "Pixel_6"], ".android/avd", False),
    ("brew-cleanup-path-outside-cache", ["brew", "cleanup"], "work/stuff", False),
    ("npm-cache-clean-path-outside", ["npm", "cache", "clean", "--force"], "work/stuff", False),
    ("npm-cache-clean-path-sibling-prefix", ["npm", "cache", "clean", "--force"], ".npmrc-dir", False),
    ("simctl-path-outside", ["xcrun", "simctl", "delete", "unavailable"], "Library/Developer/Xcode/DerivedData", False),
    ("docker-builder-prune-path-outside", ["docker", "builder", "prune", "-f"], "work/stuff", False),
    ("hf-cache-rm-path-mismatch", ["hf", "cache", "rm", "model/org/name", "-y"], ".cache/huggingface/hub/models--org--other", False),
    ("hf-cache-rm-path-is-hub", ["hf", "cache", "rm", "model/org/name", "-y"], ".cache/huggingface/hub", False),
    ("hf-cache-prune-path-outside", ["hf", "cache", "prune", "-y"], "work/stuff", False),
    ("grok-session-path-mismatch", ["grok", "sessions", "delete", TID], f".grok/sessions/%2Ftmp%2Fproj/{TID2}", False),
    ("grok-session-path-too-deep", ["grok", "sessions", "delete", TID], f".grok/sessions/%2Ftmp%2Fproj/{TID}/sub", False),
    ("uv-cache-path-outside", ["uv", "cache", "prune"], "work/stuff", False),
]


@pytest.mark.parametrize("name,argv,rel,unregistered", NEG, ids=[n[0] for n in NEG])
def test_unregistered_native_shape_is_refused_by_check_and_act(env, name, argv, rel, unregistered):
    target = _make_target(env, rel, False)
    item = {"path": str(target), "method": "native", "argv": argv}
    # plan check：降级为 keep，且报错写明原因
    env.write([node(target)], [card("c01", [item])])
    res = env.check()
    assert {c["id"]: c["verdict_out"] for c in res["cards"]} == {"c01": "keep"}, name
    text = json.dumps(res, ensure_ascii=False)
    if unregistered:
        assert "该命令形状未登记" in text and "protect.py 形状表" in text
    else:
        assert "不对应" in text or "必须" in text
    # act：即使封存被伪造（digest 自洽），执行前复核也必须拒绝，不启动任何子进程
    digest = env.forge({"c01": ("clean", [item])})
    calls = []
    env.mp.setattr(subprocess, "run", lambda cmd, *a, **kw: calls.append(cmd))
    assert env.act("c01", digest=digest) == 0
    assert calls == [] and target.exists()
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "skipped", name


def test_native_check_rejects_malformed_argv():
    for bad in (None, [], "npm cache verify", ["npm", ""], ["npm", 3], [""], ["npm\x00", "cache", "verify"]):
        assert protect.native_check(bad, "/x") is not None
        assert not protect.native_allowed(bad)
    # 槽位里不接受换行/空格夹带
    assert not protect.native_allowed(["ollama", "rm", "llama3\n"])
    assert not protect.native_allowed(["ollama", "rm", "llama3 --all"])
    assert not protect.native_allowed(["sdkmanager", "--uninstall", "ndk;1\n"])


# ---------------------------------------------------------------- P1.3 硬保护名单补强

def _plan_rejects(env, path):
    """沿用 test_check_rejects_protected_path 的方式：计划里删该路径，check 必须降级为 keep 并报"保护"。"""
    env.write([node(path)], [card("c01", [delete_item(path)])])
    res = env.check()
    c = res["cards"][0]
    assert c["verdict_out"] == "keep" and c["downgraded"]
    assert any("保护" in e for e in c["errors"])


@pytest.mark.parametrize("path", [
    "/private/var/vm", "/private/var/vm/sleepimage", "/var/vm", "/var/vm/swapfile0",
    "/Volumes", "/Volumes/Backup", "/Volumes/Backup/",
    "/Users", "/Users/someoneelse", "/Users/someoneelse/x", "/Users/Shared", "/Users/Shared/y",
    "/Volumes/Backup/.Trashes", "/Volumes/Backup/.Trashes/501/a.txt", "/.Trashes",
])
def test_p13_fixed_hard_protections(env, path):
    assert protect.protected_reason(path) is not None


def test_p13_volume_contents_below_root_not_blanket_protected(env):
    # 卷根受保护，但外置盘里更深的普通缓存目录不应被一刀切
    assert protect.protected_reason("/Volumes/Backup/cache/tmp") is None


@pytest.mark.parametrize("rel", [
    "Library/Application Support/MobileSync",
    "Library/Application Support/MobileSync/Backup/abc/Info.plist",
    ".Trash", ".Trash/old.txt",
])
def test_p13_home_hard_protections(env, rel):
    assert protect.protected_reason(str(env.home / rel)) is not None
    assert protect.protected_reason(str(env.home / "Library/Application Support/Other")) is None


@pytest.mark.parametrize("rel", ["Library/Application Support/MobileSync/Backup", ".Trash"])
def test_p13_plan_check_rejects_new_home_paths(env, rel):
    mkfile(env.home / rel / "a.bin")
    age(env.home)
    _plan_rejects(env, env.home / rel)


def test_p13_plan_and_act_gates_reject_system_paths(env):
    # 这些路径在测试机上不存在，check 会先报"不存在"；改验 plan/act 共用的两个保护闸口
    for p in ("/Users/someoneelse/x", "/private/var/vm/sleepimage", "/Volumes/Backup",
              "/Volumes/Backup/.Trashes/501/a"):
        assert plan.entry_protected(p, {}) is not None, p
        assert act.protected(p, {}) is not None, p
        assert act.protected(p, {}, entry=True) is not None, p


def test_p13_symlink_to_protected_dir_is_protected(env):
    # 指向受保护目录的链接：用链接下的子路径查，必须按物理路径命中
    link = env.work / "lnk"
    link.symlink_to(env.home / "Documents")
    assert protect.protected_reason(str(link / "sub" / "x.txt")) is not None
    # 链接指向尚不存在的受保护子路径（悬空/拼接部分）同样命中
    (env.home / ".ssh").mkdir()
    link2 = env.work / "lnk2"
    link2.symlink_to(env.home / ".ssh")
    assert protect.protected_reason(str(link2 / "not" / "yet" / "there")) is not None
    # 普通目录不受影响
    (env.work / "real").mkdir()
    assert protect.protected_reason(str(env.work / "real" / "x")) is None


def test_p13_protected_name_that_is_symlink_elsewhere_still_protected(env, tmp_path):
    # ~/.ssh 本身是指向别处的链接：物理路径落在名单外，原路径必须仍命中
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (env.home / ".ssh").symlink_to(elsewhere)
    assert protect.protected_reason(str(env.home / ".ssh" / "id_rsa")) is not None
    assert protect.protected_reason(str(env.home / ".ssh")) is not None


def test_p13_symlink_into_trash_and_mobilesync(env):
    ms = env.home / "Library/Application Support/MobileSync"
    ms.mkdir(parents=True)
    link = env.work / "to_ms"
    link.symlink_to(ms)
    assert protect.protected_reason(str(link / "Backup")) is not None


def test_p13_var_vm_listed_independently_of_realpath(env):
    # /var 在 macOS 上是 /private/var 的链接，realpath 会掩盖 /var/vm 条目缺失；直接查词法路径
    assert protect._reason("/var/vm/swapfile0") is not None
    assert protect._reason("/var/vm") is not None


# ---------------------------------------------------------------- P1.R 修补（H2 / M1 / M2 / M3 / M4 / L2 / L3 / L4）

def _native_item(path, argv):
    return {"path": str(path), "method": "native", "argv": argv}


def _run_native_capture(env, target, argv, cwd_probe=None):
    """封存并执行一条原生命令，返回 (子进程调用记录, 回执条目)。子进程被替换成记录器，不会真的执行。"""
    env.write([node(target)], [card("c01", [_native_item(target, argv)])])
    digest = env.seal()
    calls = []

    def fake(cmd, *a, **kw):
        if cmd and cmd[0] == "pgrep":           # 进程闸门（docker 等）：按“没在运行”回答
            return subprocess.CompletedProcess(cmd, 1, "", "")
        probe = sorted(os.listdir(kw["cwd"])) if kw.get("cwd") else None
        calls.append((cmd, kw, probe))
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    env.mp.setattr(subprocess, "run", fake)
    assert env.act("c01", digest=digest) == 0
    return calls, env.receipt()["cards"][0]["items"][0]


POISON_ENV = {
    "PIP_CACHE_DIR": "/evil/pip", "PIP_INDEX_URL": "http://evil", "npm_config_cache": "/evil/npm",
    "NPM_CONFIG_CACHE": "/evil/npm", "NODE_OPTIONS": "--require /evil.js", "UV_CACHE_DIR": "/evil/uv",
    "BUN_INSTALL_CACHE_DIR": "/evil/bun", "HOMEBREW_CACHE": "/evil/brew", "HOMEBREW_NO_AUTOREMOVE_X": "0",
    "XDG_CACHE_HOME": "/evil/xdg", "HF_HOME": "/evil/hf", "HUGGINGFACE_HUB_CACHE": "/evil/hf",
    "OLLAMA_MODELS": "/evil/ollama", "DOCKER_HOST": "tcp://evil:2375", "DOCKER_CONFIG": "/evil/docker",
    "ANDROID_HOME": "/evil/android", "ANDROID_SDK_ROOT": "/evil/android", "ANDROID_AVD_HOME": "/evil/avd",
    "CODEX_HOME": "/evil/codex", "DEVELOPER_DIR": "/evil/xcode", "JAVA_HOME": "/evil/jdk",
}
ENV_ALLOWED = {"HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "PATH"}


def test_m1_native_runs_in_empty_fixed_cwd_with_whitelisted_env(env):
    target = env.home / ".npm"
    mkfile(target / "a.bin")
    age(target)
    proj = env.work / "proj"
    proj.mkdir()
    (proj / ".npmrc").write_text(f"cache={env.work}/evil-npm\n")
    (proj / "uv.toml").write_text(f'cache-dir = "{env.work}/evil-uv"\n')
    env.mp.chdir(proj)
    for k, v in POISON_ENV.items():
        env.mp.setenv(k, v)
    env.mp.setenv("LC_ALL", "en_US.UTF-8")
    env.mp.setenv("LANG", "en_US.UTF-8")

    calls, it = _run_native_capture(env, target, ["npm", "cache", "clean", "--force"])
    assert len(calls) == 1
    cmd, kw, probe = calls[0]
    # 工作目录：run 目录下固定的 native-cwd，执行时是空的，而不是带 .npmrc 的当前目录
    base = str(env.run / "native-cwd")
    assert os.path.dirname(kw["cwd"]) == base and os.path.basename(kw["cwd"]).startswith("cmd-")
    assert kw["cwd"] != str(proj) and os.listdir(kw["cwd"]) == []
    assert probe == []
    # 环境：只剩白名单 + LC_*，调用者的缓存位置类变量全部不见
    sub = kw["env"]
    assert set(sub) <= ENV_ALLOWED | {"LC_ALL"}, sorted(set(sub) - ENV_ALLOWED)
    assert not (set(POISON_ENV) & set(sub))
    assert sub["HOME"] == protect.HOME and sub["LC_ALL"] == "en_US.UTF-8" and sub["LANG"] == "en_US.UTF-8"
    assert sub["PATH"].split(":")[:6] == list(protect.NATIVE_FIXED_PATH)
    # 回执记录程序路径、工作目录与环境
    assert it["cwd"] == kw["cwd"] and it["env"] == sub and "prog" in it


@pytest.mark.parametrize("argv,rel", [
    (["pip3", "cache", "purge"], "Library/Caches/pip"),
    (["uv", "cache", "clean"], ".cache/uv"),
    (["bun", "pm", "cache", "rm"], ".bun/install/cache"),
    (["brew", "cleanup"], "Library/Caches/Homebrew"),
    (["docker", "builder", "prune", "-f"], "Library/Containers/com.docker.docker"),
    (["hf", "cache", "prune", "-y"], ".cache/huggingface"),
])
def test_m1_poisoned_env_never_reaches_any_cache_tool(env, argv, rel):
    target = _make_target(env, rel, False)
    for k, v in POISON_ENV.items():
        env.mp.setenv(k, v)
    [(cmd, kw, probe)], _ = _run_native_capture(env, target, argv)
    forced = set(protect.native_match(argv)[0].forced_env(protect.HOME))
    assert set(kw["env"]) <= ENV_ALLOWED | forced and not (set(POISON_ENV) & set(kw["env"]) - forced)


def test_m1_android_location_is_derived_from_path_not_inherited(env):
    d = env.home / "Library/Android/sdk/ndk/25.1"
    mkfile(d / "a.bin")
    age(d)
    for k, v in POISON_ENV.items():
        env.mp.setenv(k, v)
    [(cmd, kw, probe)], _ = _run_native_capture(env, d, ["sdkmanager", "--uninstall", "ndk;25.1"])
    sub, sdk = kw["env"], str(env.home / "Library/Android/sdk")
    assert sub["ANDROID_HOME"] == sub["ANDROID_SDK_ROOT"] == sdk
    assert sub["ANDROID_AVD_HOME"] == str(env.home / ".android/avd")
    assert sub.get("JAVA_HOME") != "/evil/jdk" and "/evil" not in json.dumps(sub)


def test_m1_nonempty_native_cwd_skips_item_without_running(env):
    target = env.home / ".npm"
    mkfile(target / "a.bin")
    age(target)
    env.write([node(target)], [card("c01", [_native_item(target, ["npm", "cache", "clean", "--force"])])])
    digest = env.seal()
    (env.run / "native-cwd").mkdir()
    (env.run / "native-cwd" / ".npmrc").write_text("cache=/evil\n")
    calls = []
    env.mp.setattr(subprocess, "run", lambda cmd, *a, **kw: calls.append(cmd))
    assert env.act("c01", digest=digest) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert calls == [] and it["status"] == "skipped" and "不该有的东西" in it["reason"]
    assert (target / "a.bin").exists()


def test_m1_program_path_is_sealed_and_must_match_at_execution(env):
    target = env.home / ".npm"
    mkfile(target / "a.bin")
    age(target)
    argv = ["npm", "cache", "clean", "--force"]
    env.write([node(target)], [card("c01", [_native_item(target, argv)])])
    digest = env.seal()
    sealed = json.loads((env.run / "seal.json").read_text())["cards"]["c01"]["items"][0]
    assert sealed["prog"] == protect.resolve_prog("npm")
    # 封存之后 PATH 里的程序换了位置：执行时解析结果与封存不一致，不能执行
    env.mp.setattr(protect, "resolve_prog", lambda name, environ=None: "/tmp/elsewhere/npm")
    calls = []
    env.mp.setattr(subprocess, "run", lambda cmd, *a, **kw: calls.append(cmd))
    assert env.act("c01", digest=digest) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert calls == [] and it["status"] == "skipped" and "程序路径" in it["reason"]


def test_m1_resolve_prog_ignores_relative_node_modules_and_venv_dirs(env, tmp_path):
    nm = tmp_path / "proj" / "node_modules" / ".bin"
    nm.mkdir(parents=True)
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (tmp_path / "venv" / "pyvenv.cfg").write_text("home = x\n")
    good = tmp_path / "tools"
    good.mkdir()
    for d in (nm, venv, good):
        f = d / "zzcleanup-tool"
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    path = ":".join(["", ".", "relbin", str(nm), str(venv), str(good)])
    assert protect.resolve_prog("zzcleanup-tool", {"PATH": path}) == str(good / "zzcleanup-tool")
    assert protect.resolve_prog("zzcleanup-tool", {"PATH": ":".join(["", ".", str(nm), str(venv)])}) is None
    assert protect.resolve_prog("../x", {"PATH": str(good)}) is None


# --- H2 brew cleanup

def test_h2_brew_cleanup_forces_no_autoremove_env_and_says_what_it_does(env):
    target = _make_target(env, "Library/Caches/Homebrew", False)
    for k in ("HOMEBREW_NO_AUTOREMOVE", "HOMEBREW_AUTOREMOVE"):
        env.mp.delenv(k, raising=False)
    env.mp.setenv("HOMEBREW_AUTOREMOVE", "1")
    [(cmd, kw, probe)], it = _run_native_capture(env, target, ["brew", "cleanup", "--prune=30"])
    sub = kw["env"]
    assert sub["HOMEBREW_NO_AUTOREMOVE"] == "1" and sub["HOMEBREW_NO_AUTO_UPDATE"] == "1"
    assert sub["HOMEBREW_NO_INSTALL_CLEANUP"] == "1" and "HOMEBREW_AUTOREMOVE" not in sub
    sealed = json.loads((env.run / "seal.json").read_text())["cards"]["c01"]["items"][0]
    assert sealed["env_forced"] == protect.BREW_ENV
    purpose = protect.native_match(["brew", "cleanup"])[0].purpose
    assert "旧版本" in purpose and "不卸载" in purpose and "autoremove" in purpose


def test_h2_brew_env_file_that_could_override_forced_vars_blocks_execution(env):
    target = _make_target(env, "Library/Caches/Homebrew", False)
    (env.home / ".homebrew").mkdir()
    (env.home / ".homebrew" / "brew.env").write_text("HOMEBREW_NO_AUTOREMOVE=\n")
    env.write([node(target)], [card("c01", [_native_item(target, ["brew", "cleanup"])])])
    digest = env.seal()
    calls = []
    env.mp.setattr(subprocess, "run", lambda cmd, *a, **kw: calls.append(cmd))
    assert env.act("c01", digest=digest) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert calls == [] and it["status"] == "skipped" and "brew.env" in it["reason"]
    # 反过来：文件里只有无关键，照常执行
    (env.home / ".homebrew" / "brew.env").write_text("HOMEBREW_NO_ANALYTICS=1\n")
    env.mp.setattr(subprocess, "run", lambda cmd, *a, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    env.act("c01", digest=digest)
    assert calls == [["brew", "cleanup"]]


# --- M2 缓存类形状只接受缓存根本身

CACHE_SHAPES = [
    (["npm", "cache", "clean", "--force"], ".npm", ".npm/_logs"),
    (["npm", "cache", "verify"], ".npm", ".npm/_cacache"),
    (["brew", "cleanup"], "Library/Caches/Homebrew", "Library/Caches/Homebrew/downloads"),
    (["xcrun", "simctl", "delete", "unavailable"], "Library/Developer/CoreSimulator/Devices",
     "Library/Developer/CoreSimulator/Devices/AAAA"),
    (["hf", "cache", "prune", "-y"], ".cache/huggingface", ".cache/huggingface/hub"),
    (["pnpm", "store", "prune"], "Library/pnpm/store", "Library/pnpm/store/v3"),
    (["bun", "pm", "cache", "rm"], ".bun/install/cache", ".bun/install/cache/x"),
    (["uv", "cache", "prune"], ".cache/uv", ".cache/uv/archive-v0"),
    (["uv", "cache", "clean"], "Library/Caches/uv", "Library/Caches/uv/wheels"),
    (["pip", "cache", "purge"], "Library/Caches/pip", "Library/Caches/pip/http"),
    (["pip3", "cache", "purge"], ".cache/pip", ".cache/pip/wheels"),
    (["docker", "builder", "prune", "-f"], "Library/Containers/com.docker.docker",
     "Library/Containers/com.docker.docker/Data"),
]


@pytest.mark.parametrize("argv,root,sub", CACHE_SHAPES, ids=[" ".join(c[0][:3]) + ":" + c[2] for c in CACHE_SHAPES])
def test_m2_cache_shapes_accept_only_the_cache_root(env, argv, root, sub):
    assert protect.native_check(argv, str(env.home / root)) is None
    err = protect.native_check(argv, str(env.home / sub))
    assert err and "缓存根" in err
    # 端到端：check 降级、伪造封存的 act 也不执行
    target = _make_target(env, sub, False)
    env.write([node(target)], [card("c01", [_native_item(target, argv)])])
    assert {c["id"]: c["verdict_out"] for c in env.check()["cards"]} == {"c01": "keep"}
    digest = env.forge({"c01": ("clean", [_native_item(target, argv)])})
    calls = []
    env.mp.setattr(subprocess, "run", lambda cmd, *a, **kw: calls.append(cmd))
    assert env.act("c01", digest=digest) == 0
    assert calls == [] and env.receipt()["cards"][0]["items"][0]["status"] == "skipped"


# --- M3 HOME

def test_m3_protect_home_comes_from_passwd_not_environment():
    import pwd
    assert protect._passwd_home() == os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)


def test_m3_home_problem_detects_mismatched_environment_home(env):
    assert protect.home_problem() is None
    for bad in ("/", "/Users", "", str(env.work)):
        env.mp.setenv("HOME", bad)
        assert protect.home_problem() is not None, bad
    env.mp.setenv("HOME", str(env.home) + "/")       # 同一目录的不同写法不算不一致
    assert protect.home_problem() is None
    env.mp.setattr(protect, "HOME", "/")
    assert protect.home_problem() is not None


def test_m3_tilde_expansion_ignores_environment_home(env):
    env.mp.setenv("HOME", "/")
    assert protect.real("~/Documents") == str(env.home / "Documents")
    assert protect.lexical("~") == str(env.home)
    assert protect.protected_reason("~/Documents/a") is not None
    assert protect.protected_reason("/System/Library/x") is not None      # 系统位置规则不被 HOME=/ 豁免
    assert protect.protected_reason("/Users/Shared/x") is not None


def test_m3_plan_check_and_seal_refuse_when_home_env_differs(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    env.mp.setenv("HOME", "/")
    assert plan.main(["check", "--run", str(env.run)]) == 1
    res = json.loads((env.run / "plan.check.json").read_text())
    assert res["ok"] is False and "HOME" in res["plan_errors"][0]
    assert plan.main(["seal", "--run", str(env.run)]) == 1 and not (env.run / "seal.json").exists()


def test_m3_act_refuses_when_home_env_differs(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    digest = env.seal()                              # HOME 正常时封存
    env.mp.setenv("HOME", "/")                       # 执行时环境被改
    assert env.act("c01", digest=digest) == 2
    assert (d / "a.bin").exists() and not (env.run / "receipt.json").exists()
    env.mp.setenv("HOME", str(env.home))
    assert env.act("c01", digest=digest) == 0 and not d.exists()


# --- M4 祖先型保护不污染内部条目

def _appsupport_frontier(env, **node_kw):
    aps = env.home / "Library" / "Application Support"
    target = aps / "Code" / "Cache"
    mkfile(target / "a.bin")
    age(env.home)
    assert protect.protection_kind(str(aps)) == "ancestor"      # MobileSync 的祖先
    n = node(aps, protected=True, refs={"checked": False, "count": 0, "samples": [], "reason": "protected"}, **node_kw)
    return aps, target, n


def test_m4_item_inside_ancestor_protected_frontier_is_not_poisoned(env):
    aps, target, n = _appsupport_frontier(env)
    env.write([n], [card("c01", [delete_item(target)], evidence=[reviewed("x.md:1")])])
    c = env.check()["cards"][0]
    assert c["verdict_out"] == "decide" and any("引用没有被核实" in e for e in c["errors"]), c["errors"]   # P5.R M1
    # 没有引用核对说明时仍不放行（引用没查，要靠 evidence 补）
    env.write([n], [card("c01", [delete_item(target)])])
    c = env.check()["cards"][0]
    assert c["verdict_out"] == "keep" and any("refs_reviewed" in e for e in c["errors"])
    assert not any("不可补救" in e for e in c["errors"])


def test_m4_ancestor_itself_still_cannot_be_deleted(env):
    aps, target, n = _appsupport_frontier(env)
    env.write([n], [card("c01", [delete_item(aps)], evidence=[reviewed("x.md:1")])])
    c = env.check()["cards"][0]
    assert c["verdict_out"] == "keep" and any("保护" in e for e in c["errors"])


def test_m4_self_protected_node_still_poisons_items_inside(env):
    # 真正受保护的节点（~/Documents 之内）不因这次修正放行：节点内条目依旧不可补救
    inside = env.home / "Documents" / "proj" / "cache"
    mkfile(inside / "a.bin")
    age(env.home)
    docs = env.home / "Documents"
    assert protect.protection_kind(str(docs)) == "self"
    n = node(docs, protected=True, refs={"checked": False, "count": 0, "samples": [], "reason": "protected"})
    env.write([n], [card("c01", [delete_item(inside)], evidence=[reviewed("x.md:1")])])
    assert env.check()["cards"][0]["verdict_out"] == "keep"
    # 伪造 protected 标记（实际不受保护）同样按不可补救处理
    plain = env.work / "plainnode"
    mkfile(plain / "sub" / "a.bin")
    age(env.home)
    n2 = node(plain, protected=True, refs={"checked": False, "count": 0, "samples": [], "reason": "protected"})
    env.write([n2], [card("c01", [delete_item(plain / "sub")], evidence=[reviewed("x.md:1")])])
    c = env.check()["cards"][0]
    assert c["verdict_out"] == "keep" and any("不可补救" in e for e in c["errors"])


def test_m4_protection_kind_classification(env):
    h = env.home
    assert protect.protection_kind(str(h / "Library/Application Support")) == "ancestor"
    assert protect.protection_kind(str(h / "Library/Application Support/MobileSync")) == "self"
    assert protect.protection_kind(str(h / "Library/Application Support/MobileSync/Backup")) == "self"
    assert protect.protection_kind(str(h / "Library/Application Support/Code/Cache")) is None
    assert protect.protection_kind(str(h / ".claude")) == "ancestor"            # 含 projects/*/memory
    assert protect.protection_kind(str(h / ".claude/projects/p/memory/x.md")) == "self"
    assert protect.protection_kind(str(h)) == "self"
    assert protect.protection_kind("/private/var") == "ancestor"
    assert protect.protection_kind("/private/var/vm/x") == "self"
    assert protect.protection_kind("/System") == "self"
    assert protect.protection_kind(str(h / "keep"), {"protect": [str(h / "keep/deep")]}) == "ancestor"
    assert protect.protection_kind(str(h / "keep/deep/x"), {"protect": [str(h / "keep/deep")]}) == "self"


# --- L2 用途说明与 SDK 基础包

def test_l2_purpose_texts_state_side_effects():
    sid = {sh.sid: sh for sh in protect.SHAPES}
    assert ".ini" in sid["avdmanager-delete-avd"].purpose
    assert "cmdline-tools" in sid["sdkmanager-uninstall"].purpose and "platform-tools" in sid["sdkmanager-uninstall"].purpose
    assert "占用没有变化" in sid["docker-builder-prune"].purpose


@pytest.mark.parametrize("pkg", [
    "cmdline-tools;latest", "cmdline-tools;11.0", "cmdline-tools", "platform-tools", "Platform-Tools",
    "CMDLINE-TOOLS;latest", "platform-tools;34",
])
def test_l2_sdkmanager_refuses_to_uninstall_itself_or_adb(env, pkg):
    assert not protect.native_allowed(["sdkmanager", "--uninstall", pkg])
    assert protect.native_allowed(["sdkmanager", "--uninstall", "platform-tools-preview-x"])    # 只禁这两个包本身
    assert protect.native_allowed(["sdkmanager", "--uninstall", "system-images;android-31;x;arm64-v8a"])
    assert protect.native_allowed(["sdkmanager", "--uninstall", "build-tools;34.0.0"])


# --- L3 补充保护

@pytest.mark.parametrize("path", [
    "/etc", "/etc/hosts", "/private/etc", "/private/etc/ssh/sshd_config",
    "/Volumes/Backup/Backups.backupdb", "/Volumes/Backup/Backups.backupdb/Mac/2026-01-01/x",
    "/Volumes/Backup/deep/er/.Spotlight-V100", "/Volumes/Backup/.Spotlight-V100/Store-V2/a",
    "/Volumes/Backup/.fseventsd/0000", "/Volumes/Backup/x/.DocumentRevisions-V100/db",
    "/Volumes/Backup/.MobileBackups/a", "/Volumes/Backup/.mobilebackups", "/Volumes/Backup/BACKUPS.BACKUPDB/a",
])
def test_l3_extra_system_and_volume_metadata_protected(env, path):
    assert protect.protected_reason(path) is not None
    if not os.path.islink(path):        # /etc 是指向 /private/etc 的链接，条目级检查对链接只看它所在的位置
        assert plan.entry_protected(path, {}) is not None and act.protected(path, {}) is not None


def test_l3_lookalike_names_not_blanket_protected(env):
    for p in ("/Volumes/Backup/cache/Backups.backupdb.old", "/Volumes/Backup/cache/fseventsd-notes",
              "/Volumes/Backup/etc/x", "/Volumes/Backup/cache/Spotlight"):
        assert protect.protected_reason(p) is None, p
    assert protect.protected_reason(str(env.home / "work" / "etc" / "x")) is None


# --- L4 文档与代码一致

def test_l4_users_shared_and_trash_documentation_matches_code(env):
    assert protect.protected_reason("/Users/Shared") is not None
    assert protect.protected_reason("/Users/Shared/Downloads/a.dmg") is not None
    assert protect.protected_reason(str(env.home / ".Trash")) is not None
    assert protect.protected_reason(str(env.home / ".Trash" / "a.txt")) is not None
    root = Path(__file__).resolve().parent.parent
    contract = (root / "references" / "contract.md").read_text(encoding="utf-8")
    card_text = (root / "references" / "cards" / "app-updaters-installers.md").read_text(encoding="utf-8")
    assert "`~/.Trash` 本身与其内容" in contract
    assert "/Users/Shared/Downloads" not in card_text or "受保护" in card_text


def test_m1_each_native_command_gets_fresh_cwd(env):
    """前一条命令在工作目录里留下文件，不能让后面的命令被跳过，也不能把它的文件带给下一条。"""
    first, err = act.native_cwd(str(env.run))
    assert err is None
    Path(first, "leftover.txt").write_text("x")
    second, err = act.native_cwd(str(env.run))
    assert err is None and second != first and os.listdir(second) == []
