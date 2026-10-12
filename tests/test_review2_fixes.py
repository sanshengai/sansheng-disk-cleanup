"""发版前第二轮复核 A-E 的回归测试：每条都含会被拒绝的反例；变异验证方法见交付说明。

A peek 永不读名单补全；B 归档 os.link 不覆盖 + 清理陈旧临时目录；C --trash-reason 与 --reason 拆开；
D 契约文档不再说对话确认能归档；E leftovers 看内嵌登录项 / 辅助 App 与 /System/Applications/Utilities。
"""
import os
import plistlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from test_peek import env, cls  # noqa: E402,F401  (夹具与小工具复用)
from test_review_fixes_act import (act, auto_sel, one_dir_card, run_argv, set_profile, vol, with_gate)  # noqa: E402,F401
from test_review_fixes_act import env as act_env  # noqa: E402,F401
from test_review_fixes_read import genv, clean_cache, _ent, _cp, REAL_READ_APP_GROUPS  # noqa: E402,F401
import leftovers  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ───────────────────────── A：peek 永不读 ─────────────────────────

NEVER_PATHS = [
    # ~/.ssh 以外的私钥
    "Documents/keys/id_ecdsa", "Documents/keys/id_dsa", "Documents/keys/id_rsa", "backup/id_ed25519",
    "backup/id_custom", "backup/id_work_2024", "x/server.ppk", "x/cert.pfx", "x/upload.jks", "x/release.keystore",
    ".android/adbkey", ".android/adbkey.pub",
    # 项目级令牌 / MCP
    "code/app/.npmrc", "code/app/sub/.npmrc", "code/app/.envrc", "code/app/.mcp.json", "code/app/my-mcp.json",
    "code/app/mcp-servers.json", "Library/Application Support/Claude/claude_desktop_config.json",
    # 浏览器与客户端令牌
    "Library/Application Support/Google/Chrome/Local State",
    "Library/Application Support/Microsoft Edge/Local State",
    "Library/Application Support/Google/Chrome/Default/Local Storage/leveldb/000003.log",
    "Library/Application Support/Foo/Session Storage/000003.log",
    "Library/Application Support/Foo/IndexedDB/https_x_0.indexeddb.leveldb/CURRENT",
    "Library/Application Support/Code/User/globalStorage/state.vscdb",
    "Library/Application Support/Cursor/User/globalStorage/state.vscdb.backup",
    "Library/Application Support/Telegram Desktop/tdata/D877F783D5D3EF8C/map0",
    "Documents/wallet.dat",
    # 其它工具凭证
    ".config/github-copilot/hosts.json", ".config/github-copilot/versions/x.json", ".pgpass", ".my.cnf",
    ".s3cfg", ".boto", ".terraformrc", "infra/terraform.tfstate", "infra/terraform.tfstate.backup",
    ".m2/settings.xml", ".gradle/gradle.properties",
    # 相邻词对
    "Documents/api_key.txt", ".config/github-copilot", "Documents/API-KEY.txt", "Documents/private_key.json", "Documents/access-key.csv",
    "Documents/secret_key.txt", "Documents/APIKey.txt", "Documents/PrivateKey.json",
    "keys/service-account-prod.json", "keys/service-account.json",
]


@pytest.mark.parametrize("rel", NEVER_PATHS)
def test_a_never_list_completions(env, rel):
    assert cls(env, rel) == "never", rel


@pytest.mark.parametrize("rel", [
    # 整词匹配：这些名字只是长得像
    "Documents/author.md", "Documents/tokenizer.json", "Documents/keyboard.md", "Documents/monkey.txt",
    "Documents/apiary.txt", "Documents/keyword.md", "Documents/private-notes.md", "Documents/accessory.txt",
    "Documents/secretary.docx",
    # 公钥不算私钥；项目里普通的 gradle.properties / settings.xml 不在家目录固定位置
    "keys/id_rsa.pub", "keys/id_custom.pub", "code/app/gradle.properties", "code/app/settings.xml",
    "code/app/package.json", "code/app/mcp.md", "Documents/notes.md",
    "Library/Application Support/Foo/Local Notes/a.txt", "Documents/idea.txt", "Documents/identity.md",
])
def test_a_lookalikes_are_not_never(env, rel):
    assert cls(env, rel) != "never", rel


def test_a_envrc_is_now_never_and_peek_main_refuses_it(env):
    p = env.path("code/.envrc")
    p.parent.mkdir(parents=True)
    p.write_text("export TOKEN=abc\n")
    assert env.read(str(p)) == 3                                   # 退出码 3 = 拒绝
    assert env.records()[-1]["mode"] == "refused"


def test_a_real_home_path_classes_do_not_read_content():
    """本机只读样例：只分类，不读内容。"""
    import peek
    home = os.path.expanduser("~")
    assert peek.classify(os.path.join(home, ".android", "adbkey"), home=home) == "never"
    assert peek.classify(os.path.join(home, "Library/Application Support/Google/Chrome/Local State"), home=home) == "never"


# ───────────────────────── B：归档 os.link ─────────────────────────

def _archive_run(env, vol, files=None):
    d, n, c = one_dir_card(env, files or {"x.bin": b"x" * 3000})
    env.write([n], [c])
    digest = env.seal()
    return d, act.main(with_gate(run_argv(env, digest, "", "--archive-to", str(vol), answers=["c01=归档"])))


def test_b_archive_uses_link_and_leaves_no_tmp(act_env, vol):
    d, rc = _archive_run(act_env, vol)
    assert rc == 0 and not (d / "x.bin").exists()
    stored = [p for p in vol.rglob("x.bin")]
    assert len(stored) == 1 and stored[0].read_bytes() == b"x" * 3000
    assert not [p for p in vol.rglob(".叁笙-tmp-*")]               # 硬链接后临时目录已清掉


def test_b_link_eexist_with_different_content_fails_and_never_overwrites(act_env, vol, monkeypatch):
    """目标在“检查不存在”之后被别人建出来：os.link 报 EEXIST，内容不同判失败、原文件保留、对方文件原样。"""
    real = os.link

    def racing_link(src, dst, *a, **k):
        if "叁笙归档" in str(dst):
            with open(dst, "wb") as f:
                f.write(b"someone else")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "link", racing_link)
    d, rc = _archive_run(act_env, vol)
    it = act_env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "failed" and "同名但内容不同" in it["failed"][0]["error"]
    assert (d / "x.bin").exists()
    assert [p.read_bytes() for p in vol.rglob("x.bin")] == [b"someone else"]


def test_b_link_eexist_with_same_content_is_accepted(act_env, vol, monkeypatch):
    real = os.link

    def racing_link(src, dst, *a, **k):
        if "叁笙归档" in str(dst):
            with open(dst, "wb") as f:
                f.write(b"x" * 3000)
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "link", racing_link)
    d, rc = _archive_run(act_env, vol)
    assert rc == 0 and not (d / "x.bin").exists()


@pytest.mark.parametrize("code", ["EPERM", "ENOTSUP", "EXDEV"])
def test_b_link_unsupported_falls_back_to_rename(act_env, vol, monkeypatch, code):
    import errno
    n = getattr(errno, code)

    def no_link(src, dst, *a, **k):
        raise OSError(n, os.strerror(n))
    monkeypatch.setattr(os, "link", no_link)
    renames = []
    real = os.rename
    monkeypatch.setattr(os, "rename", lambda s, d, *a, **k: (renames.append(d), real(s, d, *a, **k))[1])
    d, rc = _archive_run(act_env, vol)
    assert rc == 0 and not (d / "x.bin").exists()
    assert any("叁笙归档" in str(r) for r in renames)
    assert [p.read_bytes() for p in vol.rglob("x.bin")] == [b"x" * 3000]


def test_b_link_other_errors_do_not_fall_back(act_env, vol, monkeypatch):
    import errno

    def bad_link(src, dst, *a, **k):
        raise OSError(errno.EIO, "I/O error")
    monkeypatch.setattr(os, "link", bad_link)
    d, rc = _archive_run(act_env, vol)
    it = act_env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "failed" and (d / "x.bin").exists()
    assert not list(vol.rglob("x.bin"))


def test_b_sweep_only_stale_tmp_dirs_inside_archive_root(tmp_path):
    root = tmp_path / "arch" / "叁笙归档-s1"
    old, fresh = root / "a" / ".叁笙-tmp-old", root / "a" / ".叁笙-tmp-fresh"
    other_old = root / "a" / "keep-me"            # 不是这个前缀：即便很旧也不动
    outside = tmp_path / "arch" / ".叁笙-tmp-outside"   # 归档根之外：不动
    for d in (old, fresh, other_old, outside):
        d.mkdir(parents=True)
        (d / "copy").write_bytes(b"z")
    past = time.time() - 25 * 3600
    for d in (old, other_old, outside):
        os.utime(d, (past, past))
    disp = act.Disposer("archive", scan_id="s1", archive_to=str(tmp_path / "arch"))
    disp.sweep_stale_tmp()
    assert not old.exists()
    assert fresh.exists() and other_old.exists() and outside.exists()


def test_b_sweep_runs_when_archiving_starts(act_env, vol):
    stale = vol / "叁笙归档-scan1" / "old" / ".叁笙-tmp-dead"
    stale.mkdir(parents=True)
    (stale / "copy").write_bytes(b"junk")
    past = time.time() - 30 * 3600
    os.utime(stale, (past, past))
    d, rc = _archive_run(act_env, vol)
    assert rc == 0 and not stale.exists()


# ───────────────────────── C：--trash-reason ─────────────────────────

def _prepared(env):
    set_profile(env, delete_mode="trash")
    d, n, c = one_dir_card(env, {"x.bin": b"x" * 3000}, verdict="clean")
    env.write([n], [c])
    digest = env.seal()
    return d, auto_sel(run_argv(env, digest, "c01"))


def test_c_no_trash_needs_trash_reason_and_reason_does_not_count(act_env, capsys):
    d, base = _prepared(act_env)
    assert act.main(base + ["--dry-run", "--no-trash"]) == 2
    assert act.main(base + ["--dry-run", "--no-trash", "--reason", "用户说直接删"]) == 2        # --reason 不再顶替
    assert act.main(base + ["--dry-run", "--no-trash", "--trash-reason", "  "]) == 2
    capsys.readouterr()
    assert act.main(base + ["--skip-pilot", "--reason", "只有一条", "--no-trash"]) == 2      # 跳过试点的原因不能当用户原话
    assert "--trash-reason" in capsys.readouterr().err                                      # 且是被“缺原话”拦下，不是别的闸门
    assert act.main(base + ["--dry-run", "--no-trash", "--trash-reason", "用户说直接删"]) == 0
    assert (d / "x.bin").exists()


def test_c_misuse_is_rejected(act_env):
    d, base = _prepared(act_env)
    assert act.main(base + ["--dry-run", "--trash-reason", "x"]) == 2            # 没有 --no-trash
    assert act.main(base + ["--dry-run", "--reason", "x"]) == 2                  # 没有 --skip-pilot


def test_c_two_reasons_land_in_separate_receipt_fields(act_env):
    d, base = _prepared(act_env)
    assert act.main(base + ["--dry-run", "--no-trash", "--trash-reason", "原话A"]) == 0
    assert act.main(base + ["--skip-pilot", "--reason", "原因B", "--no-trash", "--trash-reason", "原话A"]) == 0
    rc = act_env.receipt()
    assert rc["no_trash_reason"] == "原话A" and rc["gate"]["skip_reason"] == "原因B"


def test_c_go_takes_trash_reason_not_reason(act_env):
    from test_go_from_board import export_text, write
    set_profile(act_env, delete_mode="trash")
    d, n, c = one_dir_card(act_env, {"x.bin": b"x" * 3000}, verdict="clean")
    act_env.write([n], [c])
    digest = act_env.seal()
    f = write(act_env, export_text(digest, ["c01"]))
    base = ["go", "--run", str(act_env.run), "--from-board", f, "--no-trash"]
    with pytest.raises(SystemExit):
        act.main(base + ["--reason", "用户说直接删"])                              # go 已没有 --reason
    assert act.main(base) == 2 and (d / "x.bin").exists()
    assert act.main(base + ["--trash-reason", "用户说直接删"]) == 0
    assert act_env.receipt()["no_trash_reason"] == "用户说直接删"


# ───────────────────────── D：文档 ─────────────────────────

def test_d_docs_say_dialog_confirmation_cannot_archive():
    text = (ROOT / "references" / "contract.md").read_text(encoding="utf-8")
    assert "对话确认不支持归档，归档只走看板" in text
    assert "要归档在 `go` 上加 `--archive-to`" not in text
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "对话确认时加 `--archive-to" not in skill
    for doc in (text, skill):
        assert "--no-trash --reason" not in doc and "--no-trash` 与设置 `delete_mode=trash` 冲突时必须同时给 `--reason" not in doc


# ───────────────────────── E：内嵌 App 与 Utilities ─────────────────────────

def _mkapp(path, bid):
    (path / "Contents").mkdir(parents=True)
    (path / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bid, "CFBundleName": path.stem}))
    return str(path)


def test_e_bundle_paths_include_embedded_apps(tmp_path):
    app = tmp_path / "Host.app"
    li = app / "Contents/Library/LoginItems/Helper.app"
    hp = app / "Contents/Helpers/Tool.app"
    lh = app / "Contents/Library/Helpers/Other.app"
    ext = app / "Contents/PlugIns/W.appex"
    for d in (li, hp, lh, ext):
        d.mkdir(parents=True)
    (app / "Contents/Helpers/not-an-app.txt").write_text("x")
    got = leftovers._bundle_paths(str(app))
    assert got[0] == str(app)
    assert {str(ext), str(li), str(hp), str(lh)} <= set(got) and len(got) == 5


def test_e_embedded_app_group_is_read_and_unreadable_one_fails_closed(tmp_path, monkeypatch, clean_cache):
    app = tmp_path / "Host.app"
    li = app / "Contents/Library/LoginItems/Helper.app"
    li.mkdir(parents=True)
    seen = {}
    monkeypatch.setattr(leftovers, "_read_app_groups", lambda p: seen.setdefault(p, ["group.x"] if p == str(li) else []))
    apps, failed = leftovers.collect_app_groups({"apps": [{"path": str(app)}]})
    assert str(li) in seen and apps["apps"][0]["app_groups"] == ["group.x"] and failed == []
    monkeypatch.setattr(leftovers, "_read_app_groups", lambda p: None if p == str(li) else [])
    apps, failed = leftovers.collect_app_groups({"apps": [{"path": str(app)}]})
    assert failed == [str(li)]                                   # 读不到就报出来，调用方据此整位置不评估


def test_e_merge_extra_app_roots(tmp_path):
    util = tmp_path / "Utilities"
    util.mkdir()
    _mkapp(util / "Terminal.app", "com.apple.Terminal")
    base = {"complete": True, "errors": [], "apps": [{"path": "/Applications/A.app", "bundle_id": "a.b"}], "by_id": {}}
    out = leftovers.merge_extra_app_roots(base, roots=(str(util), str(tmp_path / "missing")))
    assert [a["path"] for a in out["apps"]] == ["/Applications/A.app", str(util / "Terminal.app")]
    assert out["complete"] is True and "com.apple.terminal" in out["by_id"]
    assert len(base["apps"]) == 1                                # 不改入参
    # 反例：Info.plist 读不出 -> 清单标不完整
    (util / "Broken.app" / "Contents").mkdir(parents=True)
    bad = leftovers.merge_extra_app_roots(base, roots=(str(util),))
    assert bad["complete"] is False and bad["errors"]
    assert leftovers.EXTRA_APP_ROOTS == () or "/System/Applications/Utilities" in leftovers.EXTRA_APP_ROOTS


def test_e_extra_root_constant_covers_system_utilities(monkeypatch):
    import importlib
    monkeypatch.undo()
    fresh = importlib.reload(leftovers)
    try:
        assert "/System/Applications/Utilities" in fresh.EXTRA_APP_ROOTS
    finally:
        importlib.reload(leftovers)


def test_e_utilities_app_group_owns_group_container(genv, tmp_path, monkeypatch):
    util = tmp_path / "System" / "Applications" / "Utilities"
    util.mkdir(parents=True)
    app = _mkapp(util / "Grapher.app", "com.example.grapher")
    monkeypatch.setattr(leftovers, "EXTRA_APP_ROOTS", (str(util),))
    genv.install_raw([])
    genv.codesign[app] = _cp(_ent(["group.org.vendor.sharedstore"]))
    genv.lib("Group Containers", "group.org.vendor.sharedstore")
    res = genv.probe()
    assert res["candidates"] == [] and res["skipped"] == {"app_installed:app_group": 1}
    # 反例：没有这个额外目录时，同一个 group 会被当成孤儿候选
    monkeypatch.setattr(leftovers, "EXTRA_APP_ROOTS", ())
    leftovers._APP_GROUP_CACHE.clear()
    res = genv.probe()
    assert [c["name"] for c in res["candidates"]] == ["org.vendor.sharedstore"]
