"""triage.py 2026-11 改进的单测：统一阈值、命令写法、App 运行、增长对账、显示与口径。"""
import os
import sys
import time

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
    assert cmd == "find -x '/Users/a/Library/Caches/Foo Bar' -mindepth 1 -delete"
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
    # 第三轮：先看写入再看名字。今天还在写的“历史英语试播”不能被归进“试做/旧版”
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
            "/Users/a/work/.local/2026-09-30-snapshot-resume/71-file-precommit-comparison-x.json",
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
