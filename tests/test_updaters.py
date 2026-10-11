"""updaters.py（scan.py probe updaters 的探针）测试。夹具全在 tmp_path；
HOME、已装清单、lsof、pgrep 都被替换，不碰真实机器状态。"""
import os
import plistlib
import sys
import types
import zipfile

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)
import protect  # noqa: E402
import scan  # noqa: E402
import updaters  # noqa: E402


def plist_bytes(bid, short=None, build=None):
    d = {"CFBundleIdentifier": bid}
    if short is not None:
        d["CFBundleShortVersionString"] = short
    if build is not None:
        d["CFBundleVersion"] = build
    return plistlib.dumps(d)


def make_zip(path, entries):
    """entries: [(zip 内路径, bytes)]，按给定顺序写入。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries:
            z.writestr(name, data)


def app_entry(bid, short, build=None, name="Foo"):
    return ("%s.app/Contents/Info.plist" % name, plist_bytes(bid, short, build or short))


def inventory(apps, complete=True):
    by_id = {}
    for a in apps:
        by_id.setdefault(a["bundle_id"].lower(), []).append(a)
    return {"complete": complete, "errors": [], "apps": apps, "by_id": by_id}


def installed(bid, short, build=None, path="/Applications/Foo.app"):
    return {"path": path, "bundle_id": bid, "short_version": short, "version": build or short, "name": "Foo"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Library" / "Caches").mkdir(parents=True)
    monkeypatch.setattr(protect, "HOME", str(home))
    state = types.SimpleNamespace(home=home, inv=inventory([]), open_files=[], procs=[])
    monkeypatch.setattr(scan, "installed_apps", lambda: state.inv)
    monkeypatch.setattr(updaters, "lsof_open", lambda paths: state.open_files)
    monkeypatch.setattr(updaters, "pgrep_processes", lambda: state.procs)
    state.caches = home / "Library" / "Caches"
    return state


def probe():
    return updaters.probe_updaters(types.SimpleNamespace())


def only(res, suffix):
    items = [i for i in res["items"] if i["path"].endswith(suffix)]
    assert len(items) == 1, [i["path"] for i in res["items"]]
    return items[0]


def electron(env, short, build=None, bid="com.acme.foo"):
    make_zip(str(env.caches / "foo-updater" / "pending" / "Foo-mac.zip"), [app_entry(bid, short, build)])
    return "foo-updater"


# ───────────────────────── 版本比较 ─────────────────────────

def test_compare_versions_pads_segments_and_falls_back_to_build():
    cv = updaters.compare_versions
    assert cv("1.2", None, "1.2.0", None) == "eq"
    assert cv("1.10", None, "1.9", None) == "gt"
    assert cv("1.4.221", None, "1.4.222", None) == "lt"
    # 非纯数字 → 退回 CFBundleVersion
    assert cv("1.0-beta", "7", "1.0", "6") == "gt"
    assert cv("1.0-beta", "abc", "1.0", "6") is None
    # ShortVersion 相等但 build 更高 → 保守按更高处理
    assert cv("2.0", "101", "2.0", "100") == "gt"
    assert cv("2.0", "100", "2.0", "100") == "eq"


# ───────────────────────── 结论 ─────────────────────────

def test_superseded_when_equal(env):
    env.inv = inventory([installed("com.acme.foo", "1.4.222")])
    electron(env, "1.4.222")
    it = only(probe(), "foo-updater")
    assert it["verdict"] == "superseded"
    assert it["reason"] == "staged_version_equals_installed"
    assert it["staged"]["bundle_id"] == "com.acme.foo"
    assert it["installed"]["short_version"] == "1.4.222"
    assert it["kind"] == "electron-updater" and it["owner_hint"] == "foo"
    assert it["in_use"] is False


def test_superseded_when_lower(env):
    env.inv = inventory([installed("com.acme.foo", "1.4.222")])
    electron(env, "1.4.100")
    it = only(probe(), "foo-updater")
    assert it["verdict"] == "superseded"
    assert it["reason"] == "staged_version_lower_than_installed"


def test_pending_install_when_higher(env):
    env.inv = inventory([installed("com.acme.foo", "1.4.222")])
    electron(env, "1.5.0")
    it = only(probe(), "foo-updater")
    assert it["verdict"] == "pending_install"


def test_bundle_id_is_case_insensitive_and_exact(env):
    # 另一个应用 com.acme.foo.nightly 不能当作 com.acme.foo
    env.inv = inventory([installed("com.acme.foo.nightly", "9.9.9")])
    electron(env, "1.0.0")
    assert only(probe(), "foo-updater")["verdict"] == "orphaned_update"
    env.inv = inventory([installed("COM.ACME.FOO", "1.0.0")])
    assert only(probe(), "foo-updater")["verdict"] == "superseded"


def test_unknown_dmg_not_mounted(env):
    d = env.caches / "foo-updater" / "pending"
    d.mkdir(parents=True)
    (d / "Foo.dmg").write_bytes(b"x" * 4096)
    env.inv = inventory([installed("com.acme.foo", "1.0")])
    it = only(probe(), "foo-updater")
    assert it["verdict"] == "unknown"
    assert it["reason"] == "dmg_not_inspected"


def test_unknown_version_unparseable(env):
    env.inv = inventory([installed("com.acme.foo", "1.0", "build-x")])
    make_zip(str(env.caches / "foo-updater" / "update.zip"), [app_entry("com.acme.foo", "2.0-rc", "nightly-3")])
    it = only(probe(), "foo-updater")
    assert it["verdict"] == "unknown"
    assert it["reason"] == "version_unparseable"


def test_unknown_multiple_installed(env):
    env.inv = inventory([installed("com.acme.foo", "1.0", path="/Applications/Foo.app"),
                         installed("com.acme.foo", "1.0", path="/Users/x/Foo.app")])
    electron(env, "1.0")
    it = only(probe(), "foo-updater")
    assert it["verdict"] == "unknown"
    assert it["reason"] == "multiple_installed"


def test_unknown_inventory_incomplete(env):
    env.inv = inventory([installed("com.acme.foo", "1.0")], complete=False)
    electron(env, "1.0")
    res = probe()
    assert res["inventory_complete"] is False
    it = only(res, "foo-updater")
    assert it["verdict"] == "unknown"
    assert it["reason"] == "inventory_incomplete"


def test_orphaned_update_when_not_installed(env):
    env.inv = inventory([installed("com.other.app", "1.0")])
    electron(env, "1.0")
    it = only(probe(), "foo-updater")
    assert it["verdict"] == "orphaned_update"
    assert it["installed"] is None


def test_zip_takes_shallowest_info_plist_not_nested_framework(env):
    # 嵌套 Framework / Helper 的 plist 排在前面，且 bundle id 与版本都不同
    entries = [
        ("Foo.app/Contents/Frameworks/Bar.framework/Resources/Info.plist", b"not a plist"),
        ("Foo.app/Contents/Frameworks/Foo Helper.app/Contents/Info.plist", plist_bytes("com.acme.foo.helper", "99.0")),
        ("Foo.app/Contents/Resources/Other.app/Contents/Info.plist", plist_bytes("com.acme.other", "0.1")),
        ("Foo.app/Contents/Info.plist", plist_bytes("com.acme.foo", "3.0", "300")),
    ]
    make_zip(str(env.caches / "foo-updater" / "update.zip"), entries)
    env.inv = inventory([installed("com.acme.foo", "3.0", "300")])
    it = only(probe(), "foo-updater")
    assert it["staged"]["bundle_id"] == "com.acme.foo"
    assert it["staged"]["short_version"] == "3.0"
    assert it["staged"]["source"] == "zip:Foo.app/Contents/Info.plist"
    assert it["verdict"] == "superseded"


def test_zip_without_app_plist_is_unknown(env):
    make_zip(str(env.caches / "foo-updater" / "update.zip"), [("readme.txt", b"hi")])
    it = only(probe(), "foo-updater")
    assert it["verdict"] == "unknown"
    assert it["reason"] == "no_app_plist_in_archive"


def test_hint_version_is_recorded_and_used_only_as_fallback(env):
    d = env.caches / "foo-updater"
    make_zip(str(d / "pending" / "Foo.zip"), [app_entry("com.acme.foo", "1.4.222")])
    (d / "pending" / "update-info.json").write_text('{"fileName":"Foo.zip","version":"1.4.222"}')
    env.inv = inventory([installed("com.acme.foo", "1.4.222")])
    it = only(probe(), "foo-updater")
    assert it["staged"]["hint_version"] == "1.4.222"
    assert it["staged"]["source"].startswith("zip:")


def test_unreadable_zip_with_hint_has_no_bundle_id_so_unknown(env):
    d = env.caches / "foo-updater" / "pending"
    d.mkdir(parents=True)
    (d / "Foo.zip").write_bytes(b"this is not a zip" * 100)
    (d / "latest-mac.yml").write_text("version: 2.0.0\nfiles: []\n")
    env.inv = inventory([installed("com.acme.foo", "1.0")])
    it = only(probe(), "foo-updater")
    assert it["verdict"] == "unknown"
    assert it["reason"] == "archive_unreadable"
    assert it["staged"]["short_version"] == "2.0.0"
    assert it["staged"]["bundle_id"] is None


def test_sparkle_reads_staged_app_dir_and_skips_launcher(env):
    base = env.caches / "com.acme.foo" / "org.sparkle-project.Sparkle"
    app = base / "Installation" / "ABC" / "Foo.app" / "Contents"
    app.mkdir(parents=True)
    (app / "Info.plist").write_bytes(plist_bytes("com.acme.foo", "2.0", "20"))
    upd = base / "Launcher" / "XYZ" / "Updater.app" / "Contents"
    upd.mkdir(parents=True)
    (upd / "Info.plist").write_bytes(plist_bytes("org.sparkle-project.Sparkle.Updater", "2.6", "2"))
    env.inv = inventory([installed("com.acme.foo", "1.0")])
    it = only(probe(), "org.sparkle-project.Sparkle")
    assert it["kind"] == "sparkle" and it["owner_hint"] == "com.acme.foo"
    assert [p["staged"]["bundle_id"] for p in it["packages"]] == ["com.acme.foo"]
    assert it["verdict"] == "pending_install"


def test_shipit_without_package_is_unknown(env):
    d = env.caches / "com.acme.foo.ShipIt"
    d.mkdir()
    (d / "ShipIt_stderr.log").write_text("log")
    it = only(probe(), "com.acme.foo.ShipIt")
    assert it["kind"] == "shipit" and it["owner_hint"] == "com.acme.foo"
    assert it["verdict"] == "unknown" and it["reason"] == "no_staged_package"


def test_google_updater_shared_component_never_orphaned(env):
    crx = env.home / "Library" / "Application Support" / "Google" / "GoogleUpdater" / "crx_cache"
    make_zip(str(crx / ("a" * 64)), [app_entry("com.google.GoogleUpdater", "156.0.1", name="GoogleUpdater")])
    old = crx.parent / "updater.log.old"
    old.write_text("old log")
    res = probe()
    it = only(res, "crx_cache")
    assert it["kind"] == "google-updater"
    assert it["verdict"] == "unknown" and it["reason"] == "shared_component_package"
    lo = only(res, "updater.log.old")
    assert lo["verdict"] == "unknown" and lo["reason"] == "updater_log_or_history"


def test_bytes_is_st_blocks_sum_without_following_symlinks(env):
    d = env.caches / "foo-updater"
    make_zip(str(d / "update.zip"), [app_entry("com.acme.foo", "1.0")])
    (d / "pending").mkdir()
    (d / "pending" / "big.bin").write_bytes(b"x" * 100000)
    outside = env.home / "outside.bin"
    outside.write_bytes(b"y" * 500000)
    os.symlink(str(outside), str(d / "link"))
    expect = 0
    for dp, dn, fn in os.walk(str(d)):
        for n in fn:
            expect += os.lstat(os.path.join(dp, n)).st_blocks * 512
    it = only(probe(), "foo-updater")
    assert it["bytes"] == expect
    assert it["bytes"] < 500000 + 100000   # 没把符号链接指向的文件算进去


# ───────────────────────── 占用 ─────────────────────────

def test_in_use_when_files_open(env):
    env.inv = inventory([installed("com.acme.foo", "1.0")])
    electron(env, "1.0")
    env.open_files = ["/x/pending/Foo-mac.zip"]
    res = probe()
    it = only(res, "foo-updater")
    assert it["in_use"] is True
    assert it["verdict"] == "in_use"
    assert it["base_verdict"] == "superseded"
    assert res["summary"]["in_use"]["count"] == 1


def test_in_use_when_updater_process_running_for_this_app_only(env):
    env.inv = inventory([installed("com.acme.foo", "1.0")])
    electron(env, "1.0")
    # 别的应用同名 ShipIt 进程（含 nightly 版 id）不算它的
    env.procs = [(101, "/x/ShipIt com.acme.foo.nightly.ShipIt /x/ShipItState.plist")]
    it = only(probe(), "foo-updater")
    assert it["in_use"] is False and it["verdict"] == "superseded"
    env.procs = [(102, "/Users/u/Library/Caches/foo-updater/pending/Foo-updater helper")]
    it = only(probe(), "foo-updater")
    assert it["in_use"] is True and it["verdict"] == "in_use"
    assert it["processes"][0]["pid"] == 102


def test_shipit_process_needs_matching_bundle_id(env):
    d = env.caches / "com.acme.foo.ShipIt"
    d.mkdir()
    (d / "ShipIt_stderr.log").write_text("log")
    env.procs = [(1, "/a/ShipIt com.acme.foo.nightly.ShipIt")]
    assert only(probe(), "com.acme.foo.ShipIt")["in_use"] is False
    env.procs = [(2, "/a/ShipIt com.acme.foo.ShipIt /x/ShipItState.plist")]
    assert only(probe(), "com.acme.foo.ShipIt")["in_use"] is True


def test_lsof_failure_gives_none_not_false_and_not_superseded(env, monkeypatch):
    # P5.R L4：占用状态无法确认时不判 superseded（改 unknown），base 结论同步
    env.inv = inventory([installed("com.acme.foo", "1.0")])
    electron(env, "1.0")
    monkeypatch.setattr(updaters, "lsof_open", lambda paths: None)
    it = only(probe(), "foo-updater")
    assert it["in_use"] is None
    assert it["verdict"] == "unknown" and it["reason"] == "in_use_unknown"
    assert "in_use_note" in it


def test_pgrep_failure_gives_none(env, monkeypatch):
    env.inv = inventory([installed("com.acme.foo", "1.0")])
    electron(env, "1.0")
    monkeypatch.setattr(updaters, "pgrep_processes", lambda: None)
    res = probe()
    assert only(res, "foo-updater")["in_use"] is None
    assert any(e["path"] == "pgrep" for e in res["errors"])


def test_lsof_open_wrapper_failure_modes(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("lsof", 20)
    monkeypatch.setattr(updaters.subprocess, "run", boom)
    assert updaters.lsof_open(["/a"]) is None
    monkeypatch.setattr(updaters.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    assert updaters.lsof_open(["/a"]) == []
    monkeypatch.setattr(updaters.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="p1\nf3\nn/a/b\n", stderr=""))
    assert updaters.lsof_open(["/a"]) == ["/a/b"]


# ───────────────────────── 输出结构 ─────────────────────────

def test_output_shape_and_json_serializable(env):
    import json
    env.inv = inventory([installed("com.acme.foo", "1.0")])
    electron(env, "1.0")
    res = probe()
    json.dumps(res)
    assert set(res) >= {"generated_at", "inventory_complete", "items", "summary", "errors"}
    it = res["items"][0]
    for k in ("path", "bytes", "kind", "owner_hint", "staged", "installed", "verdict", "reason", "age_days",
              "in_use"):
        assert k in it
    assert set(it["staged"]) >= {"bundle_id", "short_version", "version", "source"}
    assert res["summary"]["superseded"]["bytes"] == it["bytes"]


def test_no_candidates_returns_empty(env):
    res = probe()
    assert res["items"] == []
    assert res["errors"] == []
