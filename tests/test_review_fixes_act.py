"""发版前评审（REVIEW.md）第 1、2、6、7、14 条的回归测试：act.py 的归档、原生命令与处置方式闸门。

夹具全在 tmp_path 内（沿用 test_plan_act 的 Env）：HOME 是临时目录；“外接硬盘”是 <tmp>/vol/…，
用 monkeypatch 把 VOLUMES_ROOT 指过去、让 _dev 对它报告成另一块盘。每条都对应一个“修复回退就变红”的反例，
变异验证方法见交付说明（把对应修复改回去，再跑本文件）。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, age, auto_sel, card, delete_item, mkfile, node, plan, protect, with_gate  # noqa: E402,F401

DASHBOARD = Path(__file__).resolve().parent.parent / "assets" / "dashboard.html"
HAS_NODE = shutil.which("node") is not None


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(protect, "current_session_ids", lambda: [])
    monkeypatch.setattr(protect, "TMP_BASE", str(tmp_path / "noptmp"))
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def vol(tmp_path, monkeypatch):
    """一块假的外接硬盘 <tmp>/vol/disk/arch，返回归档目录。"""
    root = (tmp_path / "vol").resolve()
    target = root / "disk" / "arch"
    target.mkdir(parents=True)
    monkeypatch.setattr(act, "VOLUMES_ROOT", str(root))
    real_dev = act._dev
    monkeypatch.setattr(act, "_dev", lambda p: 777 if str(p).startswith(str(root)) else real_dev(p))
    return target


def run_argv(env, digest, cards, *extra, answers=()):
    argv = ["run", "--run", str(env.run), "--seal", digest, "--cards", cards]
    if answers:
        argv += ["--answers", *answers]
    return argv + list(extra)


def set_profile(env, **kw):
    p = env.home / ".sansheng" / "disk-cleanup"
    p.mkdir(parents=True, exist_ok=True)
    (p / "profile.json").write_text(json.dumps(kw))


def one_dir_card(env, files, cid="c01", verdict="decide", name="dl", idx=1):
    d = env.work / name
    for fname, data in files.items():
        mkfile(d / fname, data)
    age(d)
    return d, node(d, id=f"n{idx}"), card(cid, [delete_item(d)], verdict=verdict,
                                         question="?" if verdict == "decide" else None)


def archived_names(vol):
    return {p.name for p in vol.rglob("*") if p.is_file()}


# ───────────────────────── 第 1 条：X.part 同名文件不能被临时文件误删 ─────────────────────────

def test_part_sibling_is_archived_not_destroyed(env, vol):
    d, n, c = one_dir_card(env, {"movie.mkv": b"A" * 6000, "movie.mkv.part": b"B" * 7000})
    env.write([n], [c])
    digest = env.seal()
    assert act.main(with_gate(run_argv(env, digest, "", "--archive-to", str(vol), answers=["c01=归档"]))) == 0
    found = {p.name: p.read_bytes() for p in vol.rglob("*") if p.is_file()}
    assert found.get("movie.mkv") == b"A" * 6000 and found.get("movie.mkv.part") == b"B" * 7000, "已归档的文件丢了"
    assert not d.exists() or not any(d.iterdir())            # 两个原件都按归档流程处置掉了
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "removed" and it["failed_count"] == 0


def test_archive_leaves_no_temp_files_and_never_unlinks_preexisting_dest(env, vol):
    """目标位置已有内容不同的同名文件：判失败、原件保留、目标文件原样不动；成功路径不留临时目录。"""
    d, n, c = one_dir_card(env, {"a.bin": b"new" * 100, "b.bin": b"b" * 50})
    env.write([n], [c])
    digest = env.seal()
    pre = vol / "叁笙归档-scan1" / "work" / "dl" / "a.bin"
    pre.parent.mkdir(parents=True)
    pre.write_bytes(b"someone else's file")
    assert act.main(with_gate(run_argv(env, digest, "", "--archive-to", str(vol), answers=["c01=归档"]))) == 0
    assert pre.read_bytes() == b"someone else's file"          # 绝不覆盖、绝不删除不是本次创建的文件
    assert (d / "a.bin").read_bytes() == b"new" * 100           # 原件保留
    it = env.receipt()["cards"][0]["items"][0]
    assert it["failed_count"] == 1 and "内容不同" in it["failed"][0]["error"]
    assert (vol / "叁笙归档-scan1" / "work" / "dl" / "b.bin").exists() and not (d / "b.bin").exists()
    assert not [p for p in vol.rglob("*") if p.name.startswith(".叁笙-tmp-")]


# ───────────────────────── 第 2 条：答复“归档”的卡里 native 条目不得被执行 ─────────────────────────

def native_card(env):
    d = env.home / "Library" / "Android" / "sdk" / "system-images" / "android-31" / "android-tv" / "arm64-v8a"
    mkfile(d / "a.bin")
    age(d)
    argv = ["sdkmanager", "--uninstall", "system-images;android-31;android-tv;arm64-v8a"]
    return d, node(d), card("c01", [{"path": str(d), "method": "native", "argv": argv}], verdict="decide", question="卸载吗？")


def fake_native(env, d):
    calls = []

    def fake(cmd, *a, **kw):
        calls.append(cmd)
        if cmd and cmd[0] == "sdkmanager":
            os.remove(d / "a.bin")
            return subprocess.CompletedProcess(cmd, 0, "done", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")
    env.mp.setattr(subprocess, "run", fake)
    return calls


@pytest.mark.parametrize("with_target", [False, True])
def test_archive_answer_never_runs_native(env, vol, with_target):
    d, n, c = native_card(env)
    env.write([n], [c])
    digest = env.seal()
    calls = fake_native(env, d)
    extra = ["--archive-to", str(vol)] if with_target else []
    act.main(with_gate(run_argv(env, digest, "", *extra, answers=["c01=归档"])))
    assert not any(x and x[0] == "sdkmanager" for x in calls), "原生命令在“归档”答复下被执行了"
    assert (d / "a.bin").exists()
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "skipped"
    assert ("没法归档" in it["reason"]) or ("没有给归档目录" in it["reason"])
    if with_target:
        assert "没法归档" in it["reason"] and it["reason"].endswith("先留着")


def test_native_still_runs_under_delete_answer(env):
    """对照：答复“删”的 native 卡照旧执行（修复只挡归档，不改变原有行为）。"""
    d, n, c = native_card(env)
    env.write([n], [c])
    digest = env.seal()
    calls = fake_native(env, d)
    assert act.main(with_gate(run_argv(env, digest, "", answers=["c01=删"]))) == 0
    assert any(x and x[0] == "sdkmanager" for x in calls)
    assert env.receipt()["cards"][0]["items"][0]["status"] == "removed"


# ───────────────────────── 第 7 条：先落盘、再回读，才处置原件 ─────────────────────────

def test_archive_syncs_file_then_dir_before_source_is_disposed(env, vol):
    d, n, c = one_dir_card(env, {"x.bin": b"x" * 3000})
    env.write([n], [c])
    digest = env.seal()
    events = []
    real_sync, real_remove = act._sync_path, act.Disposer._remove_source
    env.mp.setattr(act, "_sync_path", lambda p: (events.append(("sync", p)), real_sync(p))[1])

    def spy_remove(self, path):
        events.append(("remove", path))
        return real_remove(self, path)
    env.mp.setattr(act.Disposer, "_remove_source", spy_remove)
    assert act.main(with_gate(run_argv(env, digest, "", "--archive-to", str(vol), answers=["c01=归档"]))) == 0
    kinds = [(k, p) for k, p in events]
    ri = next(i for i, (k, _) in enumerate(kinds) if k == "remove")
    synced = [p for k, p in kinds[:ri] if k == "sync"]
    dest_dir = str(vol / "叁笙归档-scan1" / "work" / "dl")
    assert any(".叁笙-tmp-" in p for p in synced), "副本数据没有先落盘"
    assert dest_dir in synced, "目标目录没有同步（改名没落盘）"


def test_fullfsync_used_when_available_and_falls_back_to_fsync(env, tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"1")
    calls = []
    env.mp.setattr(act.fcntl, "fcntl", lambda fd, op, *a: calls.append(op) or 0)
    act._sync_path(str(f))
    assert calls == [act.fcntl.F_FULLFSYNC]

    def refuse(fd, op, *a):
        raise OSError(errno_notsup(), "no")
    seen = []
    env.mp.setattr(act.fcntl, "fcntl", refuse)
    env.mp.setattr(os, "fsync", lambda fd: seen.append(fd))
    act._sync_path(str(f))
    assert len(seen) == 1                                        # 不支持 F_FULLFSYNC 就退回 os.fsync


def errno_notsup():
    import errno
    return errno.ENOTSUP


def test_readback_mismatch_after_rename_keeps_source(env, vol):
    d, n, c = one_dir_card(env, {"x.bin": b"x" * 3000})
    env.write([n], [c])
    digest = env.seal()
    real = os.rename

    def rename_then_corrupt(src, dst, *a, **k):
        real(src, dst, *a, **k)
        if "叁笙归档" in str(dst):
            with open(dst, "ab") as fh:
                fh.write(b"!")                                    # 改名之后落盘的内容变了
    env.mp.setattr(os, "rename", rename_then_corrupt)
    assert act.main(with_gate(run_argv(env, digest, "", "--archive-to", str(vol), answers=["c01=归档"]))) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "failed" and "回读" in it["failed"][0]["error"]
    assert (d / "x.bin").read_bytes() == b"x" * 3000              # 回读不一致：原件保留


# ───────────────────────── 第 14 条：目标必须在归档根内且异盘；空间按全部归档卡累计 ─────────────────────────

def test_dest_escaping_archive_root_via_symlink_fails_and_keeps_source(env, vol, tmp_path):
    d, n, c = one_dir_card(env, {"x.bin": b"x" * 3000})
    env.write([n], [c])
    digest = env.seal()
    outside = tmp_path / "outside"
    outside.mkdir()
    root = vol / "叁笙归档-scan1"
    root.mkdir()
    (root / "work").symlink_to(outside)                           # 归档根里的中间目录被换成指向别处的符号链接
    assert act.main(with_gate(run_argv(env, digest, "", "--archive-to", str(vol), answers=["c01=归档"]))) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "failed" and "归档目录之外" in it["failed"][0]["error"]
    assert (d / "x.bin").exists() and not list(outside.rglob("*"))   # 原件在，外面什么也没写


def test_dest_on_same_device_as_source_fails_and_keeps_source(env, vol, monkeypatch):
    d, n, c = one_dir_card(env, {"x.bin": b"x" * 3000})
    env.write([n], [c])
    digest = env.seal()
    sub = vol / "叁笙归档-scan1" / "work"
    sub.mkdir(parents=True)
    src_dev = os.stat(env.home).st_dev
    base = act._dev
    monkeypatch.setattr(act, "_dev", lambda p: src_dev if str(p).startswith(str(sub)) else base(p))   # 该子目录“其实挂着源盘”
    assert act.main(with_gate(run_argv(env, digest, "", "--archive-to", str(vol), answers=["c01=归档"]))) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "failed" and "同一块盘" in it["failed"][0]["error"]
    assert (d / "x.bin").exists()


def test_free_space_is_checked_against_all_archive_cards_together(env, vol, monkeypatch):
    nodes, cards, dirs = [], [], []
    for i in (1, 2):
        d = env.work / f"d{i}"
        mkfile(d / "x.bin", b"x" * 5000)
        age(d)
        dirs.append(d)
        nodes.append(node(d, id=f"n{i}", bytes=100_000))
        cards.append(card(f"c0{i}", [delete_item(d)], verdict="decide", question="?"))
    env.write(nodes, cards)
    digest = env.seal()
    monkeypatch.setattr(act, "_free_of", lambda p: 150_000)       # 单张卡 105,000 够，两张合计 210,000 不够
    assert act.main(with_gate(run_argv(env, digest, "", "--archive-to", str(vol), answers=["c01=归档", "c02=归档"]))) == 0
    for c in env.receipt()["cards"]:
        it = c["items"][0]
        assert it["status"] == "skipped" and "剩余空间不够" in it["reason"], c["id"]
    assert all((d / "x.bin").exists() for d in dirs)
    # 对照：只归档一张卡时放得下
    assert act.main(with_gate(run_argv(env, digest, "", "--archive-to", str(vol), answers=["c01=归档", "c02=留"]))) == 0
    cards_out = {c["id"]: c for c in env.receipt()["cards"]}
    assert cards_out["c01"]["items"][0]["status"] == "removed"


# ───────────────────────── 第 6 条：处置方式受 sel 与闸门约束 ─────────────────────────

def old_sig(seal, cards, answers):
    """0.5 的签名公式，独立重写（不调用 plan）。"""
    ans = sorted(f"{k}={v}" for k, v in answers.items()) if isinstance(answers, dict) else sorted(answers)
    body = {"seal": seal, "cards": sorted(set(cards)), "answers": ans}
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def test_sel_without_archive_target_is_byte_identical_to_old_signature():
    for at in (None, "", "   ", "/"):
        assert plan.selection_sig("a1b2c3d4e5f6", ["c03", "c01"], ["c05=留", "c02=删"], at) == old_sig(
            "a1b2c3d4e5f6", ["c03", "c01"], ["c05=留", "c02=删"])
    assert plan.selection_sig("a1b2c3d4e5f6", ["c03", "c01"], {"c05": "留", "c02": "删"}) == "d98580a7f052"   # 0.5 的固定向量


def test_sel_changes_with_archive_target_and_ignores_trailing_slash():
    base = plan.selection_sig("a1b2c3d4e5f6", ["c01"], ["c01=归档"])
    a = plan.selection_sig("a1b2c3d4e5f6", ["c01"], ["c01=归档"], "/Volumes/X/a")
    assert a != base and a != plan.selection_sig("a1b2c3d4e5f6", ["c01"], ["c01=归档"], "/Volumes/X/b")
    assert a == plan.selection_sig("a1b2c3d4e5f6", ["c01"], ["c01=归档"], "/Volumes/X/a/")


def extract_sel_js():
    m = re.search(r"/\*SEL-BEGIN\*/(.*?)/\*SEL-END\*/", DASHBOARD.read_text(encoding="utf-8"), re.S)
    assert m
    return m.group(1)


@pytest.mark.skipif(not HAS_NODE, reason="本机没有 node")
def test_dashboard_js_sel_matches_python_with_archive_target(tmp_path):
    cases = [
        ("abc123def456", ["c02", "c01"], ["c02=归档", "c01=删"], "/Volumes/移动硬盘/叁笙归档"),
        ("abc123def456", ["c02"], ["c02=归档"], "/Volumes/X/a/"),
        ("abc123def456", ["c02"], ["c02=归档"], '/Volumes/带"引号\\反斜杠/目录'),
        ("abc123def456", ["c02"], ["c02=归档"], ""),                 # 空目录：必须与旧签名一致
        ("a1b2c3d4e5f6", ["c03", "c01"], ["c02=删", "c05=留"], ""),
    ]
    prog = extract_sel_js() + "\n(async()=>{const cases=" + json.dumps(cases, ensure_ascii=False) + ";\n" \
        "console.log(JSON.stringify(await Promise.all(cases.map(([s,c,a,p])=>selSig(s,c,a,p)))));})();"
    f = tmp_path / "sel.js"
    f.write_text(prog, encoding="utf-8")
    js_vals = json.loads(subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30, check=True).stdout)
    assert js_vals == [plan.selection_sig(s, c, a, p) for s, c, a, p in cases]
    assert js_vals[3] == old_sig("abc123def456", ["c02"], ["c02=归档"])
    assert js_vals[4] == "d98580a7f052"


def test_run_refuses_when_archive_target_differs_from_sel(env, vol, tmp_path):
    """sel 绑定归档目录：看板导出时选的是 A，命令行换成 B（或没给）就对不上，什么都不动。"""
    d, n, c = one_dir_card(env, {"x.bin": b"x" * 3000})
    env.write([n], [c])
    digest = env.seal()
    sel_for_vol = plan.selection_sig(digest, ["c01"], ["c01=归档"], str(vol))
    other = vol.parent / "arch2"
    other.mkdir()
    argv = run_argv(env, digest, "", "--archive-to", str(other), "--sel", sel_for_vol, "--skip-pilot", "--reason", "t", answers=["c01=归档"])
    assert act.main(argv) == 2
    assert (d / "x.bin").exists() and not (env.run / "receipt.json").exists()


def test_stray_archive_to_without_archive_answers_does_not_change_sel(env, vol):
    d, n, c = one_dir_card(env, {"x.bin": b"x" * 3000}, verdict="clean")
    env.write([n], [c])
    digest = env.seal()
    sel = plan.selection_sig(digest, ["c01"], {})
    argv = run_argv(env, digest, "c01", "--archive-to", str(vol), "--sel", sel, "--dry-run")
    assert act.main(argv) == 0


def test_go_from_board_rejects_archive_dir_not_covered_by_sel(env, vol):
    from test_go_from_board import export_text, go
    d, n, c = one_dir_card(env, {"x.bin": b"x" * 3000})
    env.write([n], [c])
    digest = env.seal()
    ans = ["c01=归档"]
    covered = export_text(digest, [], ans, extra=[f"归档到: {vol}"], sel=plan.selection_sig(digest, ["c01"], ans, str(vol)))
    uncovered = export_text(digest, [], ans, extra=[f"归档到: {vol}"], sel=plan.selection_sig(digest, ["c01"], ans))
    assert go(env, uncovered) == 2 and (d / "x.bin").exists()      # 目录没进 sel：当作清单被改过
    assert go(env, covered) == 0 and not d.exists()
    assert (vol / "叁笙归档-scan1" / "work" / "dl" / "x.bin").exists()


def test_find_gate_requires_same_delete_mode_and_archive_target(env):
    d, n, c = one_dir_card(env, {"x.bin": b"x" * 3000}, verdict="clean")
    env.write([n], [c])
    digest = env.seal()
    seal, _ = act.load_seal(str(env.run), digest)
    sel = plan.selection_sig(digest, ["c01"], {})

    def put(**over):
        rc = {"schema": "receipt/1", "mode": "dry_run", "dry_run": True, "seal": digest, "scan_id": "scan1", "sel": sel,
              "cards": [{"id": "c01", "items": []}]}
        rc.update(over)
        for f in env.run.glob("receipt-dryrun-*.json"):
            f.unlink()
        (env.run / "receipt-dryrun-20261012T000000Z.json").write_text(json.dumps(rc))

    put(delete_mode="trash", archive_to="/Volumes/X/a")
    assert act.find_gate(str(env.run), seal, sel, ["c01"])[0] is not None                          # 不给参数：旧调用方式照旧
    assert act.find_gate(str(env.run), seal, sel, ["c01"], delete_mode="trash", archive_to="/Volumes/X/a/")[0] is not None
    assert act.find_gate(str(env.run), seal, sel, ["c01"], delete_mode="delete", archive_to="/Volumes/X/a")[0] is None
    assert act.find_gate(str(env.run), seal, sel, ["c01"], delete_mode="trash", archive_to="/Volumes/X/b")[0] is None
    put()                                                                                          # 0.5 的回执：没有这两个字段
    assert act.find_gate(str(env.run), seal, sel, ["c01"], delete_mode="delete", archive_to="")[0] is not None
    assert act.find_gate(str(env.run), seal, sel, ["c01"], delete_mode="trash", archive_to="")[0] is None


def test_formal_run_refused_when_dry_run_was_under_another_delete_mode(env):
    set_profile(env, delete_mode="trash")
    d, n, c = one_dir_card(env, {"x.bin": b"x" * 3000}, verdict="clean")
    env.write([n], [c])
    digest = env.seal()
    base = auto_sel(run_argv(env, digest, "c01"))
    assert act.main(base + ["--dry-run"]) == 0                                       # 预演按“先放废纸篓”
    assert act.main(base + ["--skip-pilot", "--no-trash", "--reason", "用户说直接删"]) == 2
    assert (d / "x.bin").exists() and not (env.run / "receipt.json").exists()          # 预演没看过“直接删”，不放行
    assert act.main(base + ["--dry-run", "--no-trash", "--reason", "用户说直接删"]) == 0   # 按直接删重新预演
    assert act.main(base + ["--skip-pilot", "--no-trash", "--reason", "用户说直接删"]) == 0
    assert not d.exists()
    rc = env.receipt()
    assert rc["delete_mode"] == "delete" and rc["no_trash_reason"] == "用户说直接删"


def test_no_trash_conflicting_with_trash_profile_needs_reason(env):
    set_profile(env, delete_mode="trash")
    d, n, c = one_dir_card(env, {"x.bin": b"x" * 3000}, verdict="clean")
    env.write([n], [c])
    digest = env.seal()
    base = auto_sel(run_argv(env, digest, "c01"))
    for extra in (["--dry-run", "--no-trash"], ["--dry-run", "--no-trash", "--reason", "  "]):
        assert act.main(base + extra) == 2
    assert not list(env.run.glob("receipt-*.json"))
    assert act.main(base + ["--dry-run", "--no-trash", "--reason", "别放废纸篓"]) == 0
    assert (d / "x.bin").exists()


def test_no_trash_without_conflict_and_reason_misuse(env):
    d, n, c = one_dir_card(env, {"x.bin": b"x" * 3000}, verdict="clean")
    env.write([n], [c])
    digest = env.seal()
    base = auto_sel(run_argv(env, digest, "c01"))
    assert act.main(base + ["--dry-run", "--no-trash"]) == 0                         # 设置本来就是直接删：不需要原话
    assert act.main(base + ["--dry-run", "--reason", "x"]) == 2                      # --reason 既不配 --skip-pilot 也不配 --no-trash


def test_go_from_board_no_trash_conflict_needs_reason(env):
    from test_go_from_board import export_text, write
    set_profile(env, delete_mode="trash")
    d, n, c = one_dir_card(env, {"x.bin": b"x" * 3000}, verdict="clean")
    env.write([n], [c])
    digest = env.seal()
    f = write(env, export_text(digest, ["c01"]))
    assert act.main(["go", "--run", str(env.run), "--from-board", f, "--no-trash"]) == 2          # 没有原话：拒绝，什么都没删
    assert (d / "x.bin").exists()
    assert act.main(["go", "--run", str(env.run), "--from-board", f, "--no-trash", "--reason", "用户说直接删"]) == 0
    assert not d.exists()
    assert env.receipt()["no_trash_reason"] == "用户说直接删" and env.receipt()["delete_mode"] == "delete"


def test_board_marks_cards_with_native_beyond_listed_items_not_archivable():
    """看板只带前 20 个条目；第 21 个是原生命令时，卡仍要标成不能归档。"""
    import plan
    items = [{"path": f"/x/{i}", "method": "delete", "scope": "all"} for i in range(plan.BOARD_ITEMS)]
    items.append({"path": "/x/n", "method": "native", "argv": ["brew", "cleanup"]})
    bp = plan.board_plan({"cards": [{"id": "c1", "verdict": "decide", "items": items},
                                    {"id": "c2", "verdict": "decide", "items": items[:3]}]}, {"cards": []})
    by = {c["id"]: c for c in bp["cards"]}
    assert by["c1"]["archivable"] is False and by["c2"]["archivable"] is True
