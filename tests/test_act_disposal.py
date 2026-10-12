"""0.6 执行方式：delete / trash / archive（接口约定第 8 节）。

夹具全在 tmp_path 内（沿用 test_plan_act 的 Env）：HOME 是临时目录，所以“废纸篓”是 <临时HOME>/.Trash；
“外接硬盘”是 <tmp>/vol/<盘名>，用 monkeypatch 把 VOLUMES_ROOT 指过去、把 _dev 对它报告成另一块盘。
"""
import errno
import json
import os
import shutil
import sys
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, age, auto_sel, card, delete_item, mkfile, node, plan, protect, with_gate  # noqa: E402,F401


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def vol(tmp_path, monkeypatch):
    """一块假的外接硬盘 <tmp>/vol/备份盘/，返回其下的归档目录。"""
    root = (tmp_path / "vol").resolve()
    target = root / "备份盘" / "叁笙归档目标"
    target.mkdir(parents=True)
    monkeypatch.setattr(act, "VOLUMES_ROOT", str(root))
    real_dev = act._dev
    monkeypatch.setattr(act, "_dev", lambda p: 777 if str(p).startswith(str(root)) else real_dev(p))
    return target


def setup(env, spec=(("c01", "clean"),), size=5000):
    dirs, nodes, cards = {}, [], []
    for i, (cid, verdict) in enumerate(spec, 1):
        d = env.work / f"d{i}"
        mkfile(d / "x.bin", b"x" * size)
        mkfile(d / "sub" / "y.bin", b"y" * size)
        age(d)
        dirs[cid] = d
        nodes.append(node(d, id=f"n{i}"))
        cards.append(card(cid, [delete_item(d)], verdict=verdict, question="处理吗？" if verdict == "decide" else None))
    env.write(nodes, cards)
    return env.seal(), dirs


def run_full(env, digest, cards, *extra, answers=()):
    argv = ["run", "--run", str(env.run), "--seal", digest, "--cards", cards]
    if answers:
        argv += ["--answers", *answers]
    return act.main(with_gate(argv + list(extra)))


def trash_dirs(env):
    return sorted((env.home / ".Trash").glob("叁笙清理-scan1-*")) if (env.home / ".Trash").exists() else []


def set_profile(env, **kw):
    p = env.home / ".sansheng" / "disk-cleanup"
    p.mkdir(parents=True, exist_ok=True)
    (p / "profile.json").write_text(json.dumps(kw))


def item_of(env, cid="c01"):
    return next(c for c in env.receipt()["cards"] if c["id"] == cid)["items"][0]


# ───────────────────────── 默认行为不变 ─────────────────────────

def test_default_is_delete_and_receipt_has_disposal(env):
    digest, dirs = setup(env)
    assert run_full(env, digest, "c01") == 0
    assert not dirs["c01"].exists() and not trash_dirs(env)
    rc = env.receipt()
    it = item_of(env)
    assert it["disposal"]["mode"] == "delete" and rc["delete_mode"] == "delete"
    assert rc["summary"]["bytes_freed_now"] == rc["summary"]["bytes_by_files"] > 0
    assert rc["summary"]["bytes_trash"] == 0 and rc["summary"]["freed_by_items"] == rc["summary"]["bytes_freed_now"]


# ───────────────────────── trash ─────────────────────────

def test_trash_moves_files_keeps_structure_and_does_not_count_as_freed(env):
    digest, dirs = setup(env)
    assert run_full(env, digest, "c01", "--trash") == 0
    assert not dirs["c01"].exists()
    td = trash_dirs(env)
    assert len(td) == 1
    assert (td[0] / "work" / "d1" / "x.bin").read_bytes() == b"x" * 5000      # 相对家目录的结构
    assert (td[0] / "work" / "d1" / "sub" / "y.bin").exists()
    rc, it = env.receipt(), item_of(env)
    assert it["status"] == "removed" and it["disposal"]["mode"] == "trash" and it["disposal"]["dir"] == str(td[0])
    assert rc["summary"]["bytes_trash"] > 0 and rc["summary"]["bytes_freed_now"] == 0
    assert rc["summary"]["freed_by_items"] == 0           # 废纸篓里的不算释放，否则和磁盘余量对不上
    assert rc["delete_mode"] == "trash" and rc["trash_dir"] == str(td[0])


def test_profile_delete_mode_trash_is_default_and_no_trash_overrides(env):
    set_profile(env, delete_mode="trash")
    digest, dirs = setup(env, spec=(("c01", "clean"), ("c02", "clean")))
    assert run_full(env, digest, "c01") == 0
    assert item_of(env)["disposal"]["mode"] == "trash" and trash_dirs(env)
    assert run_full(env, digest, "c02", "--no-trash", "--trash-reason", "别放废纸篓，直接删") == 0   # 覆盖“先放废纸篓”必须带用户原话
    assert item_of(env, "c02")["disposal"]["mode"] == "delete" and not dirs["c02"].exists()
    assert env.receipt()["no_trash_reason"]                    # 原话写进回执（with_gate 追加的 --reason test 排在后面，所以这里只验非空；精确值见 test_review_fixes_act）


def test_trash_and_no_trash_together_are_rejected(env):
    digest, dirs = setup(env)
    with pytest.raises(SystemExit):
        act.main(["run", "--run", str(env.run), "--seal", digest, "--cards", "c01", "--trash", "--no-trash"])
    assert dirs["c01"].exists()


def test_trash_exdev_skips_file_with_chinese_reason(env):
    digest, dirs = setup(env)
    real = os.rename

    def fake(src, dst, *a, **k):
        if ".Trash" in str(dst):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst, *a, **k)

    env.mp.setattr(os, "rename", fake)
    assert run_full(env, digest, "c01", "--trash") == 0
    it = item_of(env)
    assert it["status"] == "skipped" and it["skipped_count"] == 2 and it["failed_count"] == 0
    assert "不在同一块盘，没法放进废纸篓" in it["skipped"][0]["reason"]
    assert "不在同一块盘" in it["reason"]
    assert (dirs["c01"] / "x.bin").exists()
    assert not list((env.home / ".Trash").rglob("*.bin"))
    assert not trash_dirs(env)                                        # 失败时顺手建的空目录也收掉了


def test_trash_partial_exdev_counts_only_moved_files(env):
    digest, dirs = setup(env)
    real = os.rename

    def fake(src, dst, *a, **k):
        if ".Trash" in str(dst) and str(dst).endswith("y.bin"):
            raise OSError(errno.EXDEV, "x")
        return real(src, dst, *a, **k)

    env.mp.setattr(os, "rename", fake)
    assert run_full(env, digest, "c01", "--trash") == 0
    it = item_of(env)
    assert it["status"] == "partial" and it["removed"]["files"] == 1 and it["skipped_count"] == 1
    assert (dirs["c01"] / "sub" / "y.bin").exists() and not (dirs["c01"] / "x.bin").exists()


def test_trash_empty_dirs_are_removed_like_delete(env):
    digest, dirs = setup(env)
    assert run_full(env, digest, "c01", "--trash") == 0
    assert not dirs["c01"].exists()                       # scope=all：文件移走后空目录照样 rmdir
    assert item_of(env)["removed"]["dirs"] >= 1


def test_trash_does_not_overwrite_existing_destination(env):
    digest, dirs = setup(env)
    stamp = "20990101T000000Z"
    clash = env.home / ".Trash" / f"叁笙清理-scan1-{stamp}" / "work" / "d1" / "x.bin"
    mkfile(clash, b"old")
    d = act.Disposer("trash", scan_id="scan1", stamp=stamp)
    kind, why = d.dispose(str(dirs["c01"] / "x.bin"))
    assert kind == "fail" and "同名" in why
    assert clash.read_bytes() == b"old" and (dirs["c01"] / "x.bin").exists()


# ───────────────────────── 安全反例：trash 模式下闸门不放松 ─────────────────────────

def test_trash_mode_still_refuses_protected_paths(env):
    mkfile(env.home / "Documents" / "x" / "a.txt")
    age(env.home)
    digest = env.forge({"c01": ("clean", [delete_item(env.home / "Documents" / "x")])})
    assert run_full(env, digest, "c01", "--trash") == 0
    it = item_of(env)
    assert it["status"] == "skipped" and "保护" in it["reason"]
    assert (env.home / "Documents" / "x" / "a.txt").exists() and not trash_dirs(env)


def test_trash_mode_still_skips_files_modified_within_24h(env):
    d = env.work / "fresh"
    mkfile(d / "a.bin")                                   # 不调 age：刚写的
    digest = env.forge({"c01": ("clean", [delete_item(d)])})
    assert run_full(env, digest, "c01", "--trash") == 0
    it = item_of(env)
    assert it["status"] == "skipped" and "24 小时" in it["reason"]
    assert (d / "a.bin").exists() and not trash_dirs(env)


def test_trash_mode_detects_replaced_file_inode(env):
    """搬之前文件被换成别的（ino 变了）：跳过，不搬。"""
    d = env.work / "swap"
    f = mkfile(d / "a.bin")
    age(d)
    items = [{"path": str(d), "method": "delete", "scope": "files",
              "files": [{"path": str(f), "size": 5000, "mtime_ns": os.lstat(f).st_mtime_ns}]}]
    st0 = os.lstat(f)
    res = act.ItemResult(0, items[0], act.Disposer("trash", scan_id="scan1", stamp="t"))
    f.unlink()
    mkfile(f)                                             # 同名新文件，ino 不同
    if os.lstat(f).st_ino == st0.st_ino:
        pytest.skip("文件系统复用了 inode")
    act.delete_files([(str(f), st0)], res, {}, act.Disposer("trash", scan_id="scan1", stamp="t"))
    assert res.d["skipped_count"] == 1 and f.exists()


# ───────────────────────── archive ─────────────────────────

def test_archive_copies_verifies_then_deletes_source(env, vol):
    digest, dirs = setup(env, spec=(("c01", "decide"),))
    assert run_full(env, digest, "c01", "--archive-to", str(vol), answers=["c01=归档"]) == 0
    root = vol / "叁笙归档-scan1"
    assert (root / "work" / "d1" / "x.bin").read_bytes() == b"x" * 5000
    assert (root / "work" / "d1" / "sub" / "y.bin").exists()
    assert not dirs["c01"].exists()
    it = item_of(env)
    assert it["status"] == "removed"
    assert it["disposal"]["mode"] == "archive" and it["disposal"]["dir"] == str(root)
    assert it["disposal"]["volume"] == "备份盘" and it["disposal"]["source"] == "delete"
    s = env.receipt()["summary"]
    assert s["bytes_archived"] > 0 and s["bytes_freed_now"] == s["bytes_by_files"]     # 原文件已删 → 腾出
    assert not list(root.rglob("*.part"))
    assert env.receipt()["cards"][0]["archive"] is True


def test_archive_with_trash_mode_moves_source_to_trash(env, vol):
    set_profile(env, delete_mode="trash")
    digest, dirs = setup(env, spec=(("c01", "decide"),))
    assert run_full(env, digest, "c01", "--archive-to", str(vol), answers=["c01=归档"]) == 0
    it = item_of(env)
    assert it["disposal"]["source"] == "trash" and (vol / "叁笙归档-scan1" / "work" / "d1" / "x.bin").exists()
    assert (trash_dirs(env)[0] / "work" / "d1" / "x.bin").exists() and not dirs["c01"].exists()
    s = env.receipt()["summary"]
    assert s["bytes_freed_now"] == 0 and s["bytes_trash"] > 0 and s["bytes_archived"] > 0


def test_archive_checksum_mismatch_keeps_source_and_fails(env, vol):
    digest, dirs = setup(env, spec=(("c01", "decide"),))
    real = shutil.copy2

    def corrupt(src, dst, **kw):
        real(src, dst, **kw)
        with open(dst, "ab") as f:
            f.write(b"!")                                 # 复制后内容变了
    env.mp.setattr(shutil, "copy2", corrupt)
    assert run_full(env, digest, "c01", "--archive-to", str(vol), answers=["c01=归档"]) == 0
    it = item_of(env)
    assert it["status"] == "failed" and it["failed_count"] == 2 and it["removed"]["files"] == 0
    assert "校验不一致" in it["failed"][0]["error"] and "原文件保留" in it["failed"][0]["error"]
    assert (dirs["c01"] / "x.bin").read_bytes() == b"x" * 5000          # 源文件完好
    left = [x.name for x in (vol / "叁笙归档-scan1").rglob("*") if x.is_file()]
    assert left == []                                    # 校验失败的副本和 .part 都清掉了，不留半成品


@pytest.mark.parametrize("case", ["no_target", "not_volumes", "same_disk", "missing", "too_small"])
def test_archive_bad_target_skips_whole_card(env, vol, tmp_path, case):
    digest, dirs = setup(env, spec=(("c01", "decide"),))
    target = {"no_target": None, "not_volumes": str(tmp_path / "elsewhere"), "same_disk": str(vol),
              "missing": str(vol.parent / "没这个目录"), "too_small": str(vol)}[case]
    if case == "not_volumes":
        (tmp_path / "elsewhere").mkdir()
    if case == "same_disk":
        env.mp.setattr(act, "_dev", lambda p: 5)           # 和家目录同一块盘
    if case == "too_small":
        env.mp.setattr(act, "_free_of", lambda p: 100)
    extra = ["--archive-to", target] if target else []
    assert run_full(env, digest, "c01", *extra, answers=["c01=归档"]) == 0
    it = item_of(env)
    assert it["status"] == "skipped" and it["removed"]["files"] == 0
    want = {"no_target": "没有给归档目录", "not_volumes": "外接硬盘", "same_disk": "同一块盘",
            "missing": "不存在", "too_small": "空间不够"}[case]
    assert want in it["reason"]
    assert (dirs["c01"] / "x.bin").exists() and not list(vol.rglob("*.bin"))


def test_archive_target_must_not_escape_volumes_via_symlink(env, vol, tmp_path):
    link = vol.parent / "逃逸"
    link.symlink_to(tmp_path)
    assert "实际指向" in act.check_archive_target(str(link), 10)


def test_archive_rerun_accepts_identical_existing_copy(env, vol):
    digest, dirs = setup(env, spec=(("c01", "decide"),))
    d = act.Disposer("archive", scan_id="scan1", stamp="t", archive_to=str(vol))
    f = dirs["c01"] / "x.bin"
    dest = vol / "叁笙归档-scan1" / "work" / "d1" / "x.bin"
    mkfile(dest, b"x" * 5000)                              # 上次中断前已复制好
    assert d.dispose(str(f)) == (None, None) and not f.exists()
    mkfile(f, b"z")
    mkfile(dest, b"different")
    kind, why = d.dispose(str(f))
    assert kind == "fail" and "同名但内容不同" in why and f.exists()


def test_archive_answer_accepted_by_select_cards_and_signed(env):
    digest, dirs = setup(env, spec=(("c01", "clean"), ("c02", "decide")))
    seal, _ = act.load_seal(str(env.run), digest)
    sel = act.select_cards(seal, [], {"c02": "归档"})
    assert sel == [("c02", True, None)]                    # decide 卡答复“归档”也执行，且不需要写进 --cards
    assert act.select_cards(seal, ["c01"], {"c01": "归档"}) == [("c01", True, None)]
    assert act.select_cards(seal, ["c02"], {"c02": "留"})[0][1] is False
    assert plan.selection_sig(digest, ["c02"], {"c02": "归档"}) != plan.selection_sig(digest, ["c02"], {"c02": "删"})


def test_archive_card_not_recorded_as_delete_choice(env):
    digest, dirs = setup(env, spec=(("c01", "clean"), ("c02", "clean")))
    (env.run / "cardkeys.json").write_text(json.dumps({"c01": {"key": "k1", "bytes": 1, "title": "t"},
                                                       "c02": {"key": "k2", "bytes": 1, "title": "t"}}))
    seal, _ = act.load_seal(str(env.run), digest)
    seen = []
    env.mp.setattr(plan.prefs, "record_choices", lambda sid, offered, chosen: seen.append([o["id"] for o in offered]))
    act.record_choices(str(env.run), seal, ["c01", "c02"], skip=["c02"])
    assert seen == [["c01"]]


# ───────────────────────── 看板导出 → go --from-board ─────────────────────────

def test_go_from_board_reads_archive_target_line(env, vol):
    from test_go_from_board import export_text, go
    digest, dirs = setup(env, spec=(("c01", "clean"), ("c02", "decide")))
    ans = ["c02=归档"]
    text = export_text(digest, ["c01"], ans, extra=[f"归档到: {vol}"],
                       sel=plan.selection_sig(digest, ["c01", "c02"], ans, str(vol)))
    assert go(env, text) == 0
    assert not dirs["c01"].exists() and not dirs["c02"].exists()
    assert (vol / "叁笙归档-scan1" / "work" / "d2" / "x.bin").exists()
    assert item_of(env, "c01")["disposal"]["mode"] == "delete"
    assert item_of(env, "c02")["disposal"]["mode"] == "archive"


def test_go_from_board_archive_without_target_skips_card_only(env, vol):
    from test_go_from_board import export_text, go
    digest, dirs = setup(env, spec=(("c01", "clean"), ("c02", "decide")))
    ans = ["c02=归档"]
    text = export_text(digest, ["c01"], ans, sel=plan.selection_sig(digest, ["c01", "c02"], ans))
    go(env, text)
    assert not dirs["c01"].exists() and (dirs["c02"] / "x.bin").exists()
    assert "没有给归档目录" in item_of(env, "c02")["reason"]


def test_go_from_board_conflicting_archive_lines_rejected(env, vol, capsys):
    from test_go_from_board import export_text, go
    digest, dirs = setup(env, spec=(("c02", "decide"),))
    ans = ["c02=归档"]
    text = export_text(digest, [], ans, extra=[f"归档到: {vol}", f"归档到: {vol}/别处"],
                       sel=plan.selection_sig(digest, ["c02"], ans))
    assert go(env, text) == 2
    assert "归档到" in capsys.readouterr().err and (dirs["c02"] / "x.bin").exists()


# ───────────────────────── 预演说明 / native ─────────────────────────

def test_dry_run_reason_says_what_will_happen(env, vol):
    digest, dirs = setup(env, spec=(("c01", "decide"), ("c02", "decide"), ("c03", "decide")))
    base = ["run", "--run", str(env.run), "--seal", digest, "--cards", "c01,c02,c03",
            "--answers", "c01=删", "c02=删", "c03=归档", "--dry-run"]
    assert act.main(base + ["--no-trash", "--archive-to", str(vol)]) == 0
    reasons = {c["id"]: c["items"][0]["reason"] for c in json.loads(
        sorted(env.run.glob("receipt-dryrun-*.json"))[-1].read_text())["cards"]}
    assert "将删除" in reasons["c01"] and "将搬到" in reasons["c03"] and str(vol) in reasons["c03"]
    assert act.main(base + ["--trash", "--archive-to", str(vol)]) == 0
    rc = json.loads(sorted(env.run.glob("receipt-dryrun-*.json"))[-1].read_text())
    assert all("将移入废纸篓" in c["items"][0]["reason"] for c in rc["cards"] if c["id"] != "c03")
    assert "再移入废纸篓" in next(c for c in rc["cards"] if c["id"] == "c03")["items"][0]["reason"]
    assert all((d / "x.bin").exists() for d in dirs.values()) and not trash_dirs(env)


def test_native_item_unaffected_by_trash_mode(env):
    target = env.home / ".npm"
    mkfile(target / "a.bin")
    age(target)
    prog = protect.resolve_prog("npm")
    if not prog:
        pytest.skip("本机没有 npm")
    item = {"path": str(target), "method": "native", "argv": ["npm", "cache", "clean", "--force"], "prog": prog}
    digest = env.forge({"c01": ("clean", [item])})
    env.mp.setattr(subprocess, "run", lambda cmd, *a, **kw: (shutil.rmtree(target),
                                                              subprocess.CompletedProcess(cmd, 0, "ok", ""))[1])
    assert run_full(env, digest, "c01", "--trash") == 0
    it = item_of(env)
    assert it["status"] == "removed" and it["disposal"]["mode"] == "native"
    s = env.receipt()["summary"]
    assert s["bytes_trash"] == 0 and s["bytes_freed_now"] == s["bytes_by_files"] > 0
    assert not trash_dirs(env)
