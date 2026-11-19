"""triage.py 2026-11 改进的单测：统一阈值、命令写法、App 运行、增长对账、显示与口径。"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import triage  # noqa: E402


def test_effective_hours_takes_stricter():
    assert triage.effective_hours(6, None) == 6
    assert triage.effective_hours(6, 24) == 24
    assert triage.effective_hours(30, 24) == 30


def test_hyperframes_card_24h():
    hrs = [h for cat, gs, _, h in triage.TIER1 if any("hyperframes-extract-cache" in g for g in gs)]
    assert hrs == [24]


def test_tier1_commands_native_and_no_bare_rm():
    for cat, want in (("Homebrew 缓存", "brew cleanup"), ("npm 缓存", "npm cache clean --force"),
                      ("uv 缓存", "uv cache clean")):
        chk, cmd = triage.tier1_commands(cat, "/x/y")
        assert cmd == want
    assert "pip cache purge" in triage.tier1_commands("pip 缓存", "/x")[1]
    chk, cmd = triage.tier1_commands("应用缓存 Library/Caches", "/Users/a/Library/Caches/Foo Bar")
    assert "rm -rf" not in cmd and "rm -rf" not in chk
    assert cmd == "/usr/bin/find -x '/Users/a/Library/Caches/Foo Bar' -mindepth 1 -delete"
    assert "-delete" not in chk and "-print -quit" in chk   # 检查与删除分开
    assert "node_compile_cache.py" in triage.tier1_commands("node 编译缓存", "/t/node-compile-cache")[1]


def test_new_tier1_locations_listed():
    globs = [g for _, gs, _, _ in triage.TIER1 for g in gs]
    assert any(g.endswith("/pytest-of-*") for g in globs)
    assert any(g.endswith("/X/com.google.Chrome.code_sign_clone") for g in globs)
    assert any(g.endswith("/Library/Caches/*") for g in globs)   # 覆盖 Caches/Google、Caches/baoyu-skills


def test_app_running_detection():
    procs = ["/Applications/CodexBar.app/Contents/MacOS/CodexBar", "/usr/sbin/cfprefsd"]
    assert triage.app_running_for("/Users/a/Library/Caches/CodexBar", procs) == "CodexBar"
    assert triage.app_running_for("/Users/a/Library/Caches/Google", procs) is None
    procs.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    assert triage.app_running_for("/Users/a/Library/Caches/Google", procs) == "Google Chrome"
    assert triage.app_running_for("/var/folders/x/y/X/com.google.Chrome.code_sign_clone", procs) == "Google Chrome"
    assert triage.app_running_for("/Users/a/Library/Caches/cmux", ["/Applications/cmux.app/Contents/MacOS/cmux"]) == "cmux"
    assert triage.app_running_for("/Users/a/Library/Caches/Claude", ["/Applications/Claude.app/Contents/MacOS/Claude"]) == "Claude"


def test_growth_gap_sign():
    G = triage.GB
    assert "删除" in triage.growth_gap_note(-5 * G) and "抵消" in triage.growth_gap_note(-5 * G)
    assert "未扫" in triage.growth_gap_note(3 * G) and "删除" not in triage.growth_gap_note(3 * G)
    assert triage.growth_gap_note(None) == "无可比基线"


def test_display_tmpdir_and_urldecode():
    t = triage.TMP.rstrip("/")
    assert triage.display(t + "/node-compile-cache").startswith("$TMPDIR/")
    assert triage.display(triage.HOME + "/.grok/sessions/%2FUsers%2Fa%2Fwork") == "~/.grok/sessions/Users/a/work"


def test_denied_top_groups():
    H = triage.HOME
    d = ["du: %s/Library/Application Support/Google/Chrome: Operation not permitted" % H,
         "du: %s/Library/Application Support/CloudDocs: Operation not permitted" % H,
         "du: %s/Pictures/Photos Library.photoslibrary: Operation not permitted" % H]
    top = dict(triage.denied_top(d))
    assert top["~/Library/Application Support"] == 2
    assert top["~/Pictures/Photos Library.photoslibrary"] == 1


def test_classify_video():
    now = time.time()
    assert triage.classify_video("x/旧片.作废", None, now) == "作废"
    assert triage.classify_video("交付/a", None, now) == "已交付/交付"
    assert triage.classify_video("英语时态教学试片/v2", None, now) == "试做/重制/v1-v3/旧版"
    # 第三轮：先看写入再看名字。今天还在写的“新项目试播”不能被归进“试做/旧版”
    assert triage.classify_video("产品线A/试播", now - 600, now) == "进行中（24 小时内有写入）"
    assert triage.classify_video("产品线A/试播", now - 86400 * 5, now) == "试做/重制/v1-v3/旧版"
    assert triage.classify_video("x/.作废-重叠", now - 60, now) == "作废"   # 明确作废标记优先
    assert triage.classify_video("abc/def", now - 86400 * 3, now) == "其他"


def test_df_note_states_facts_only():
    df = {"device": "/dev/disk3s5", "used": 575 * triage.GB,
          "path_df": {"device": "/dev/disk3s1s1", "used": 600 * triage.GB}}
    s = triage.df_note(df)
    assert "/bin/df" in s and "未证实" in s and "快照" not in s and "GiB" in s


def test_phases_text_not_dict():
    s = triage.phases_text({"lsof": 1.0, "du_breadth": 2.5})
    assert "{" not in s and "并行 du 2.5s" in s


# ---------- 2026-11-03 第三轮对比实验的修正 ----------
import json
import shutil
import subprocess
from types import SimpleNamespace

import clone_dedupe


def test_split_ref_hits_history_vs_consumer():
    hits = ["/Users/a/work/_workspace/disk-watch/2026-11-01/triage.json",
            "/Users/a/work/.local/sample-snapshot/precommit-comparison-example.json",
            "/Users/a/work/.local/stage/registry-before.json",
            "/Users/a/work/_ops/records/2026-10/x.md",
            "/Users/a/work/site/scripts/intake/intake_toolkit.py",
            "/Users/a/work/skills/x/SKILL.md"]
    active, history = triage.split_ref_hits(hits)
    assert active == hits[4:]
    assert len(history) == 4
    assert triage.split_ref_hits(None) == ([], [])


def test_ref_globs_include_scripts():
    for g in ("*.py", "*.ps1", "*.mjs", "SKILL.md"):
        assert g in triage.REF_DOC_GLOBS


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


def test_ignored_usage_counts_ignored_dirs(tmp_path):
    """旧实现用非法参数组合，git fatal 后输出为空，报告“被忽略 0 B”。"""
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".local/\n")
    (tmp_path / ".local" / "big").mkdir(parents=True)
    (tmp_path / ".local" / "big" / "f.bin").write_bytes(b"x" * 300000)
    rows, total, err = triage.ignored_usage(str(tmp_path))
    assert err is None and total >= 300000 and rows[0][0] == ".local"
    # 反例：旧命令在本机 git 上确实失败（证明不是测试凭空构造的问题）
    r = subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain", "--ignored=matching", "--untracked-files=no"],
                       capture_output=True, text=True)
    assert r.returncode != 0 or "!!" not in r.stdout


def test_ignored_usage_failure_returns_none_bytes(monkeypatch):
    """git ls-files 失败时第三返回值是原因，合计必须是 None（不是 0），调用方要能区分。"""
    monkeypatch.setattr(triage, "run", lambda *a, **k: (1, "", "fatal: not a git repository"))
    rows, total, err = triage.ignored_usage("/no/such/wt")
    assert rows == [] and total is None and "fatal" in err


def test_worktrees_tolerates_ignored_bytes_none(tmp_path, monkeypatch):
    """11-06 根因：ignored_bytes=None 时 big_ign 用 >= 会 TypeError。失败时按 0 比较，不再抛错。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init")
    # 建一条可列进 worktree list 的附加工作树（主树会被跳过）
    wt = tmp_path / "linked"
    _git(repo, "worktree", "add", "-b", "feat-none", str(wt))
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(triage, "find_repos", lambda _ctx: [str(repo)])
    monkeypatch.setattr(triage, "ignored_usage", lambda p, timeout=180: ([], None, "git ls-files 失败/超时"))
    monkeypatch.setattr(triage.Ctx, "size", lambda self, path: 4096)
    monkeypatch.setattr(triage.Ctx, "open_under", lambda self, path: 0)
    monkeypatch.setattr(triage.Ctx, "recent_write", lambda self, path, hours=None: False)
    monkeypatch.setattr(triage.Ctx, "keep_reason", lambda self, path, protect=None: None)
    t2, keep, prune = triage.worktrees(ctx)
    all_rows = t2 + keep
    assert any(r["path"] == str(wt) and r["ignored_bytes"] is None and r.get("ignored_error") for r in all_rows)
    # 可移除路径上若走到 note 拼接，也不应因 None 再炸（用 or 0 后 big_ign 为 False）
    for r in t2:
        assert isinstance(r.get("note", ""), str)


def _ctx(tmp_path, **kw):
    args = SimpleNamespace(quiet_hours=6, min_gb=0.5, min_tier1_mb=0, workers=2, du_timeout=60, out=str(tmp_path / "out"), stale_days=7, **kw)
    ctx = triage.Ctx(args)
    ctx.holders, ctx.open, ctx.lsof_ok, ctx._argv = {}, [], True, {}
    ctx.rendering = []
    return ctx


def test_stale_subtasks_only_untracked_and_old(tmp_path):
    wt = tmp_path / "wt"; wt.mkdir()
    _git(wt, "init", "-q")
    (wt / ".gitignore").write_text("*.mp4\n")
    old = wt / "季" / "旧子任务"; old.mkdir(parents=True)
    (old / "tracked.txt").write_text("keep")
    _git(wt, "add", "-A"); _git(wt, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init")
    (old / "render.mp4").write_bytes(b"\0" * 2_000_000)          # 忽略
    (old / "scratch.bin").write_bytes(b"\1" * 1_000_000)         # 未跟踪
    fresh = wt / "季" / "新子任务"; fresh.mkdir()
    (fresh / "new.mp4").write_bytes(b"\2" * 3_000_000)
    ten_days = time.time() - 10 * 86400
    for f in (old / "render.mp4", old / "scratch.bin", old / "tracked.txt"):
        os.utime(f, (ten_days, ten_days))
    ctx = _ctx(tmp_path)
    triage.GB_SAVE = triage.GB
    try:
        triage.GB = 1   # 让阈值按字节算，便于小文件测试
        rows = triage.stale_subtasks(ctx, str(wt), depth=3, min_gb=1_000_000, stale_days=7, min_untracked_gb=1)
    finally:
        triage.GB = triage.GB_SAVE
    paths = [r["path"] for r in rows]
    assert str(old) in paths and str(fresh) not in paths
    r = rows[paths.index(str(old))]
    assert r["untracked_files"] == 2 and "clean -n -d -x" in r["check_command"] and "clean -f -d -x" in r["command"]
    assert "rm -rf" not in r["command"]


def test_offdisk_card_no_rm_rf():
    a = triage.offdisk_card({"path": "/Users/a/work/_workspace/cleanup-archive-1/x y.tgz", "bytes": 123})
    assert a["offdisk_ok"] and "rm -rf" not in a["command"] and "-maxdepth 0 -type f -delete" in a["command"]
    assert "123" in a["check_command"]


def test_split_children_frees_idle_children(tmp_path, monkeypatch):
    """整目录有 1 个子项在用时，闲置子项应拆出来进第 1 档，而不是整目录保留。"""
    root = tmp_path / "Caches" / "Foo"
    for n in ("idle", "busy"):
        (root / n).mkdir(parents=True)
        (root / n / "blob").write_bytes(b"x" * 50000)
    old = time.time() - 3 * 86400
    os.utime(root / "idle" / "blob", (old, old))
    ctx = _ctx(tmp_path)
    ctx.open = [str(root / "busy" / "blob")]
    ctx.holders = {str(root / "busy" / "blob"): {("1", "x")}}
    it = {"category": "应用缓存 Library/Caches", "path": str(root), "note": "n"}
    ok, held = triage.split_children(ctx, it, 6, 1)
    assert [o["path"] for o in ok] == [str(root / "idle")]
    assert [h["path"] for h in held] == [str(root / "busy")]
    assert "rm -rf" not in ok[0]["command"]


def test_split_children_chrome_clone_matching(tmp_path, monkeypatch):
    root = tmp_path / "X" / "com.google.Chrome.code_sign_clone"
    for n in ("code_sign_clone.AAA", "code_sign_clone.BBB"):
        (root / n).mkdir(parents=True); (root / n / "f").write_bytes(b"x" * 10000)
    monkeypatch.setattr(triage, "CHROME_CLONE_ROOT", str(root))
    births = {"code_sign_clone.AAA": 1000.0, "code_sign_clone.BBB": 50000.0}
    monkeypatch.setattr(triage, "clone_birthtime", lambda p: births[os.path.basename(p)])
    ctx = _ctx(tmp_path)
    it = {"category": "Chrome 代码签名克隆", "path": str(root), "note": "n"}
    ok, held = triage.split_children(ctx, it, 6, 1, starts=[50010.0])   # 运行中的 Chrome 对应 BBB
    assert [os.path.basename(o["path"]) for o in ok] == ["code_sign_clone.AAA"]
    assert [os.path.basename(h["path"]) for h in held] == ["code_sign_clone.BBB"]
    assert "chrome_clones.py" in ok[0]["command"]
    assert triage.held_summary(held) == "逐份判断：1 份" + held[0]["reason"]
    assert "运行中的 Chrome" in held[0]["reason"]
    both = [{"reason": "x"}, {"reason": "x"}, {}]
    assert triage.held_summary(both) == "逐份判断：2 份x；1 份在用"


def _mk(p, data, age_days=2):
    p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
    t = time.time() - age_days * 86400; os.utime(p, (t, t))


def test_clone_dedupe_plan_and_run(tmp_path):
    data = os.urandom(300000)
    keep = tmp_path / "成品" / "成品库" / "ep1.mp4"
    d1 = tmp_path / "成品" / "交付" / "ep1.mp4"
    d2 = tmp_path / "_workspace" / "worktrees" / "line" / "成品" / "成品库" / "ep1.mp4"
    other = tmp_path / "成品" / "交付" / "ep2.mp4"
    for p in (keep, d1, d2):
        _mk(p, data)
    _mk(other, os.urandom(300000))
    plan = tmp_path / "plan.json"
    doc = clone_dedupe.plan([str(tmp_path)], 100000, str(plan))
    assert len(doc["groups"]) == 1
    g = doc["groups"][0]
    assert g["keep"] == str(keep)                       # 成品库优先；工作树里的“成品库”副本不当保留端
    assert sorted(x["path"] for x in g["dups"]) == sorted([str(d1), str(d2)])
    mt = os.stat(d1).st_mtime_ns
    # 计划之后改动的副本必须跳过
    _mk(d2, os.urandom(300000))
    rc = tmp_path / "r.jsonl"
    r = clone_dedupe.run(str(plan), str(rc))
    assert r["cloned"] == 1 and r["skipped"] == 1
    assert d1.read_bytes() == data and os.stat(d1).st_mtime_ns == mt
    rows = [json.loads(l) for l in rc.read_text().splitlines()]
    assert {x["status"] for x in rows} == {"cloned", "skipped"}
    assert not list(tmp_path.rglob("*.clone-dedupe-tmp"))


def test_clone_dedupe_skips_recent(tmp_path):
    data = os.urandom(200000)
    a = tmp_path / "a" / "x.mov"; b = tmp_path / "b" / "x.mov"
    _mk(a, data); _mk(b, data, age_days=0)
    plan = tmp_path / "p.json"
    clone_dedupe.plan([str(tmp_path)], 100000, str(plan))
    r = clone_dedupe.run(str(plan), str(tmp_path / "r.jsonl"))
    assert r["cloned"] == 0 and r["skipped"] == 1


def test_load_local_defaults_and_override(tmp_path):
    d = triage.load_local(str(tmp_path / "缺失.json"))
    assert d["work_root"] is None and d["production_globs"] == [] and d["offdisk_backup"] is None
    f = tmp_path / "local.json"
    f.write_text(json.dumps({"work_root": "~/X", "video_root": "成品"}), encoding="utf-8")
    d = triage.load_local(str(f))
    assert d["work_root"] == "~/X" and d["video_root"] == "成品" and d["ledger_globs"] == []


def test_unconfigured_work_root_never_matches(monkeypatch):
    # 未配置工作区：占位路径不存在，任何真实路径都不算在工作区内，生产目录与备份检查自动跳过
    monkeypatch.setattr(triage, "COWORK", triage.HOME + "/.sansheng/disk-cleanup/(未配置工作区)")
    monkeypatch.setattr(triage, "PRODUCTION_GLOBS", ())
    monkeypatch.setattr(triage, "BACKUP_STATE", None)
    assert not os.path.isdir(triage.COWORK)
    assert triage.is_production(triage.HOME + "/Documents/x") is None
    assert triage.backup_status(triage.HOME + "/Documents/x") == "未确认（未配置盘外备份台账）"
    assert triage.reference_hits(triage.HOME + "/Documents/x") == []
    assert triage.video_products(None, root=None) is None


# ---------- 已是 APFS 克隆的副本不计入可省、不重写；find -x 走 /usr/bin/find ----------
import fcntl as _fcntl


def _clone_and_copy(tmp_path, size=3 << 20):
    """keep 原件、clone 用 /bin/cp -c 克隆、copy 用 cat 复制（不共享块），三份都在 tmp_path 的不同目录。"""
    data = os.urandom(size)
    keep = tmp_path / "成品" / "成品库" / "ep.mp4"
    _mk(keep, data)
    clone = tmp_path / "成品" / "交付" / "ep.mp4"
    copy = tmp_path / "成品" / "分发暂存" / "ep.mp4"
    clone.parent.mkdir(parents=True); copy.parent.mkdir(parents=True)
    subprocess.run(["/bin/cp", "-c", "-p", str(keep), str(clone)], check=True)
    subprocess.run("cat %s > %s" % (shlex_quote(keep), shlex_quote(copy)), shell=True, check=True)
    for q in (clone, copy):
        t = time.time() - 2 * 86400; os.utime(q, (t, t))
    return data, keep, clone, copy


def shlex_quote(p):
    import shlex
    return shlex.quote(str(p))


def _need_apfs(tmp_path):
    keep = tmp_path / "probe-a"; keep.write_bytes(os.urandom(1 << 20))
    c = tmp_path / "probe-b"
    subprocess.run(["/bin/cp", "-c", str(keep), str(c)], check=True)
    if clone_dedupe.phys_signature(str(keep)) is None or clone_dedupe.phys_signature(str(keep)) != clone_dedupe.phys_signature(str(c)):
        pytest.skip("临时目录不在 APFS 上，无法做克隆正反对照")


def test_share_state_real_apfs_clone_vs_cat_copy(tmp_path):
    _need_apfs(tmp_path)
    data, keep, clone, copy = _clone_and_copy(tmp_path)
    assert clone_dedupe.share_state(str(keep), str(clone)) == "shared"      # 正例
    assert clone_dedupe.share_state(str(keep), str(copy)) == "unshared"     # 反例：内容相同但物理块不同


def test_clone_dedupe_plan_marks_shared_and_run_only_touches_cat_copy(tmp_path):
    _need_apfs(tmp_path)
    data, keep, clone, copy = _clone_and_copy(tmp_path)
    plan = tmp_path / "plan.json"
    doc = clone_dedupe.plan([str(tmp_path / "成品")], 100000, str(plan))
    g = doc["groups"][0]
    assert g["keep"] == str(keep)
    assert [d["path"] for d in g["dups"]] == [str(copy)] and g["dups"][0]["share_state"] == "unshared"
    assert [d["path"] for d in g["shared"]] == [str(clone)]
    size = len(data)
    assert doc["shared_files"] == 1 and doc["shared_bytes"] == size
    assert doc["savable_files"] == 1 and doc["savable_bytes"] == size
    assert doc["nominal_bytes"] == 2 * size and doc["unknown_share_files"] == 0
    clone_ino = os.stat(clone).st_ino
    rc = tmp_path / "r.jsonl"
    r = clone_dedupe.run(str(plan), str(rc))
    assert r["cloned"] == 1 and r["already_shared"] == 0
    assert os.stat(clone).st_ino == clone_ino                                # 已共享的没被重写
    assert clone_dedupe.share_state(str(keep), str(copy)) == "shared"        # cat 副本被换成了克隆
    assert copy.read_bytes() == data


def test_clone_dedupe_run_rechecks_and_skips_newly_shared(tmp_path):
    # 计划时是未共享，执行前这份已被别人换成克隆：run 要再测一次、跳过并记“已共享，跳过”，不重写
    _need_apfs(tmp_path)
    data, keep, clone, copy = _clone_and_copy(tmp_path)
    plan = tmp_path / "plan.json"
    clone_dedupe.plan([str(tmp_path / "成品")], 100000, str(plan))
    st = os.stat(copy)
    os.unlink(copy)
    subprocess.run(["/bin/cp", "-c", "-p", str(keep), str(copy)], check=True)
    os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns))                      # 保持计划里的 mtime/大小，ino 会变
    doc = json.load(open(plan)); doc["groups"][0]["dups"][0]["ident"][1] = os.stat(copy).st_ino
    doc["groups"][0]["dups"][0]["ident"][0] = os.stat(copy).st_dev
    json.dump(doc, open(plan, "w"))
    ino = os.stat(copy).st_ino
    rcp = tmp_path / "r.jsonl"
    r = clone_dedupe.run(str(plan), str(rcp))
    assert r["cloned"] == 0 and r["already_shared"] == 1
    assert os.stat(copy).st_ino == ino
    rows = [json.loads(l) for l in rcp.read_text().splitlines()]
    assert rows[-1]["status"] == "shared-skip" and "已共享，跳过" in rows[-1]["why"]


def test_clone_dedupe_unknown_when_fcntl_fails_counts_as_unshared(tmp_path, monkeypatch):
    data = os.urandom(300000)
    a = tmp_path / "成品库" / "x.mov"; b = tmp_path / "交付" / "x.mov"
    _mk(a, data); _mk(b, data)

    def boom(*args, **kw):
        raise OSError(25, "Inappropriate ioctl for device")
    monkeypatch.setattr(clone_dedupe.fcntl, "fcntl", boom)
    assert clone_dedupe.phys_signature(str(a)) is None
    assert clone_dedupe.share_state(str(a), str(b)) == "unknown"
    plan = tmp_path / "plan.json"
    doc = clone_dedupe.plan([str(tmp_path)], 100000, str(plan))
    d = doc["groups"][0]["dups"][0]
    assert d["share_state"] == "unknown" and doc["savable_files"] == 1 and doc["shared_files"] == 0
    assert doc["unknown_share_files"] == 1
    r = clone_dedupe.run(str(plan), str(tmp_path / "r.jsonl"))
    assert r["cloned"] == 1 and r["already_shared"] == 0                      # 未知按未共享处理，照常克隆


def test_clone_dedupe_partial_sharing_counts_as_unshared(tmp_path, monkeypatch):
    # 3 处位置里只有 1 处相同：不能算已共享
    monkeypatch.setattr(clone_dedupe, "phys_signature", lambda p, size=None: (1, 2, 3) if str(p).endswith("k") else (1, 9, 9))
    k = tmp_path / "k"; d = tmp_path / "d"; k.write_bytes(b"x"); d.write_bytes(b"x")
    assert clone_dedupe.share_state(str(k), str(d)) == "unshared"


def test_dup_scan_separates_shared_from_savable(tmp_path, monkeypatch):
    _need_apfs(tmp_path)
    data, keep, clone, copy = _clone_and_copy(tmp_path)
    monkeypatch.setattr(triage, "dup_roots", lambda ctx: [str(tmp_path / "成品")])
    r = triage.dup_scan(SimpleNamespace(profile={}), min_mb=1)
    size = len(data)
    assert r["nominal_bytes"] == 2 * size and r["shared_bytes"] == size and r["savable_bytes"] == size
    assert r["shared_files"] == 1 and r["dup_files"] == 1
    assert r["samples"][0]["dup"] == str(copy)                               # 示例里不出现已共享的克隆
    assert dict(r["by_location"]) and sum(v for _, v in r["by_location"]) == size
    text = "\n".join(triage.render_dups(r))
    assert "名义重复 %s，已共享 %s" % (triage.human(2 * size), triage.human(size)) in text
    assert "扣除后可省 %s" % triage.human(size) in text
    # 全部已共享：不给执行命令
    r2 = dict(r, dup_files=0, groups=0, savable_bytes=0, samples=[], by_location=[])
    t2 = "\n".join(triage.render_dups(r2))
    assert "全部已共享" in t2 and "clone_dedupe.py run" not in t2


def test_dir_index_find_failure_marked_unchecked_not_empty(monkeypatch):
    calls = []
    monkeypatch.setattr(triage, "_DIR_INDEX", {})
    monkeypatch.setattr(triage, "run", lambda cmd, timeout=60, cwd=None: (calls.append(cmd) or (1, "", "find: unknown predicate `-x'")))
    assert triage.dir_index() == {}
    assert calls[0][0] == "/usr/bin/find"                                    # 不走 PATH 里的 GNU find
    why = triage.dir_index_unchecked()
    assert why and "rc=1" in why                                              # 标“未查”，不是静默的空结果


def test_dir_index_ok_has_no_unchecked(monkeypatch):
    monkeypatch.setattr(triage, "_DIR_INDEX", {})
    monkeypatch.setattr(triage, "run", lambda cmd, timeout=60, cwd=None: (0, "/w/a/b\n/w/c\n", ""))
    assert triage.dir_index() == {"b": "/w/a/b", "c": "/w/c"}
    assert triage.dir_index_unchecked() is None


def test_voided_dirs_find_failure_marked_unchecked(monkeypatch):
    seen = []
    monkeypatch.setattr(triage, "run", lambda cmd, timeout=60, cwd=None: (seen.append(cmd) or (1, "", "unknown predicate")))
    dirs, why = triage.voided_dirs("/x")
    assert dirs == [] and why and "rc=1" in why and seen[0][0] == "/usr/bin/find"
    monkeypatch.setattr(triage, "run", lambda cmd, timeout=60, cwd=None: (124, "", "timeout"))
    assert triage.voided_dirs("/x")[1] == "超时"
    monkeypatch.setattr(triage, "run", lambda cmd, timeout=60, cwd=None: (0, "/x/a.作废\n", ""))
    assert triage.voided_dirs("/x") == (["/x/a.作废"], None)


def test_user_facing_find_commands_use_absolute_path():
    chk, cmd = triage.tier1_commands("应用缓存 Library/Caches", "/x/y")
    assert chk.startswith("/usr/bin/find -x ") and cmd.startswith("/usr/bin/find -x ")
    a = triage.offdisk_card({"path": "/x/a.tgz", "bytes": 5})
    assert a["command"].startswith("/usr/bin/find -x ")


# ---------- find 失败（rc≠0 且无输出）≠ “没有写入”/“不在渲染” ----------
class _FakeCtx:
    class args:
        quiet_hours = 6


def _fail_run(rc=1):
    return lambda cmd, timeout=60, cwd=None: (rc, "", "Permission denied")


def test_recent_write_find_failure_is_unchecked(monkeypatch):
    monkeypatch.setattr(triage, "run", _fail_run(1))
    assert triage.Ctx.recent_write(_FakeCtx(), "/x") is None          # 不是 False
    monkeypatch.setattr(triage, "run", _fail_run(124))
    assert triage.Ctx.recent_write(_FakeCtx(), "/x") is None


def test_recent_write_normal_cases_unchanged(monkeypatch):
    monkeypatch.setattr(triage, "run", lambda cmd, timeout=60, cwd=None: (0, "", ""))
    assert triage.Ctx.recent_write(_FakeCtx(), "/x") is False
    monkeypatch.setattr(triage, "run", lambda cmd, timeout=60, cwd=None: (0, "/x/a\n", ""))
    assert triage.Ctx.recent_write(_FakeCtx(), "/x") is True
    # 部分子目录拒绝访问但已找到写入：仍是 True
    monkeypatch.setattr(triage, "run", lambda cmd, timeout=60, cwd=None: (1, "/x/a\n", "denied"))
    assert triage.Ctx.recent_write(_FakeCtx(), "/x") is True


def test_last_write_find_failure_is_unchecked(monkeypatch):
    monkeypatch.setattr(triage, "run", _fail_run(1))
    w = triage.Ctx.last_write(_FakeCtx(), "/x")
    assert w and w.get("unchecked") and w["time"] is None
    assert triage._lw({"last_write": w}) == "未查"
    monkeypatch.setattr(triage, "run", lambda cmd, timeout=60, cwd=None: (0, "", ""))
    assert triage.Ctx.last_write(_FakeCtx(), "/x") is None             # 真的没文件


def test_rendering_dirs_find_failure_treated_as_rendering(tmp_path, monkeypatch):
    d = tmp_path / "prod" / "a" / "b"; d.mkdir(parents=True)
    monkeypatch.setattr(triage, "PRODUCTION_GLOBS", [str(tmp_path / "prod")])
    monkeypatch.setattr(triage, "run", _fail_run(1))
    out = triage.rendering_dirs()
    assert str(d) in out and str(d) in triage.RENDERING_UNCHECKED
    monkeypatch.setattr(triage, "run", lambda cmd, timeout=60, cwd=None: (0, "", ""))
    assert triage.rendering_dirs() == [] and triage.RENDERING_UNCHECKED == []


# ---------- 大文件加密探测点 ----------
def test_probe_offsets_dense_for_big_files():
    assert len(clone_dedupe.probe_offsets(100 << 20)) == 3
    big = clone_dedupe.probe_offsets(1 << 30)
    assert len(big) == 8 and big[0] == 0 and all(o % 4096 == 0 for o in big)
    assert big[-1] == ((1 << 30) - 1) // 4096 * 4096


def test_big_file_middle_rewrite_detected_with_dense_probes(tmp_path, monkeypatch):
    # 模拟：300MiB 文件，1/7 处那块被改写成不同物理块；旧 3 点抽样看不到，8 点必须发现
    size = 300 << 20
    bad = clone_dedupe.probe_offsets(size)[1]
    assert bad not in {0, (size // 2) // 4096 * 4096, (size - 1) // 4096 * 4096}
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: type("S", (), {"st_size": size, "st_dev": 1})())
    monkeypatch.setattr(os, "open", lambda *a, **k: 99)
    monkeypatch.setattr(os, "close", lambda fd: None)
    def fake_fcntl(fd, op, arg):
        import struct as st
        _, n, off = st.unpack(clone_dedupe._L2P, arg)
        return st.pack(clone_dedupe._L2P, 0, n, off + (10**9 if (CUR[0] == "dup" and off == bad) else 0))
    CUR = ["keep"]
    monkeypatch.setattr(clone_dedupe.fcntl, "fcntl", fake_fcntl)
    a = clone_dedupe.phys_signature("k"); CUR[0] = "dup"; b = clone_dedupe.phys_signature("d")
    assert a != b
    # 对照：只看旧的 3 个点则相同
    three = [i for i, o in enumerate(clone_dedupe.probe_offsets(size)) if o != bad]
    assert [a[i] for i in three] == [b[i] for i in three]


def _git_repo(path, files):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    for rel, text in files.items():
        f = path / rel; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(text)
    _git(path, "add", "-A"); _git(path, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init")


def test_npx_referenced_by_project_code_is_tool_state_not_tier1(tmp_path, monkeypatch):
    """已跟踪脚本依赖固定版本路径时，缓存应归到工具状态。"""
    home = tmp_path / "home"; npx = home / ".npm" / "_npx"
    (npx / "0123456789abcdef" / "node_modules" / "hyperframes").mkdir(parents=True)
    (npx / "0123456789abcdef" / "node_modules" / "hyperframes" / "chunk.js").write_bytes(b"\0" * 4096)
    ten_days = time.time() - 10 * 86400
    for d, _, fs in os.walk(npx):
        for n in fs: os.utime(os.path.join(d, n), (ten_days, ten_days))
    repo = tmp_path / "work"
    _git_repo(repo, {"src/render.py": "engines = sorted((Path.home()/'.npm/_npx').glob('*/node_modules/hyperframes'))\n",
                     "_archive/old/x.py": "p = '.npm/_cacache'\n"})
    monkeypatch.setattr(triage, "HOME", str(home))
    monkeypatch.setattr(triage, "COWORK", str(repo))
    monkeypatch.setattr(triage, "TIER1", [("npx 缓存", [str(npx)], "npx 临时安装", None)])
    monkeypatch.setattr(triage.Ctx, "keep_reason", lambda self, path, protect=None: None)
    monkeypatch.setattr(triage, "running_apps", lambda: [])
    refs = triage.tool_state_refs([str(npx), str(home / ".npm" / "_cacache")], repo=str(repo))
    assert list(refs) == [str(npx)] and refs[str(npx)][0].endswith("src/render.py")   # 归档里的引用不算
    ctx = _ctx(tmp_path)
    t1, keep, _, _ = triage.tier1(ctx)
    assert str(npx) not in [r["path"] for r in t1]
    assert any(r["path"] == str(npx) and "工具状态" in r["reason"] for r in keep)
    # 反例：去掉代码引用后，同一个目录照常进第 1 档
    (repo / "src" / "render.py").write_text("print('no engine')\n")
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qam", "drop")
    t1, keep, _, _ = triage.tier1(_ctx(tmp_path))
    assert str(npx) in [r["path"] for r in t1]


def test_generic_short_basenames_not_false_referenced(tmp_path, monkeypatch):
    repo = tmp_path / "work"
    (repo / "web" / "dist").mkdir(parents=True)
    (repo / "sub" / "dl").mkdir(parents=True)
    (repo / "proc" / "out").mkdir(parents=True)
    (repo / "proc" / "out2").mkdir(parents=True)
    (repo / "line" / "old-campaign-20261101").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("不要碰 proc/out2/a.bin；构建产物请看 dist 说明与 old-campaign-20261101。\n", encoding="utf-8")
    monkeypatch.setattr(triage, "COWORK", str(repo))
    monkeypatch.setattr(triage, "LEDGER_GLOBS", ())
    monkeypatch.setattr(triage, "_REF_CACHE", {})
    assert triage.reference_hits(str(repo / "web" / "dist")) == []
    assert triage.reference_hits(str(repo / "sub" / "dl")) == []
    assert triage.reference_hits(str(repo / "proc" / "out")) == []
    assert any(h.endswith("AGENTS.md") for h in triage.reference_hits(str(repo / "proc" / "out2")))
    assert any(h.endswith("AGENTS.md") for h in triage.reference_hits(str(repo / "line" / "old-campaign-20261101")))


def test_tool_state_skips_producers_and_build_dirs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    c1 = home / "Library" / "Caches" / "115Browser"
    c1.mkdir(parents=True)
    repo = tmp_path / "work"
    _git_repo(repo, {
        "skills/sansheng-disk-cleanup/producers.json": '{"p": "Library/Caches/115Browser"}\n',
        "app/build/intermediates/x.json": '{"p": "Library/Caches/115Browser"}\n',
    })
    monkeypatch.setattr(triage, "HOME", str(home))
    assert triage.tool_state_refs([str(c1)], repo=str(repo)) == {}


def test_active_android_system_image_gets_keep_hint(tmp_path):
    home = tmp_path / "home"
    avd = home / ".android" / "avd" / "Pixel.avd"
    avd.mkdir(parents=True)
    (avd / "config.ini").write_text("image.sysdir.1 = system-images/android-36/google_apis/arm64-v8a/\n", encoding="utf-8")
    refs = triage.active_android_system_images(str(home))
    assert str(home / "Library" / "Android" / "sdk" / "system-images" / "android-36") in refs
    assert str(home / "Library" / "Android" / "sdk" / "system-images" / "android-34") not in refs
