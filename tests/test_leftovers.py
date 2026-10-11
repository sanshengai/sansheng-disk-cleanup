"""leftovers.py 的测试。夹具全部在 tmp_path；monkeypatch protect.HOME、scan.installed_apps、mdfind、brew。"""
import os
import sys
import time

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)
import leftovers  # noqa: E402
import protect  # noqa: E402
import scan  # noqa: E402

DAY = 86400


def make_apps(*bids, complete=True):
    apps = [{"path": "/Applications/%d.app" % i, "bundle_id": b, "version": "1", "short_version": "1",
             "name": b.split(".")[-1]} for i, b in enumerate(bids)]
    by_id = {}
    for a in apps:
        by_id.setdefault(a["bundle_id"].lower(), []).append(a)
    return {"complete": complete, "errors": [] if complete else [{"path": "x", "error": "boom"}],
            "apps": apps, "by_id": by_id}


def mkdir_aged(path, age_days, size=4096, now=None):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "blob"), "wb") as f:
        f.write(b"x" * size)
    t = (now or time.time()) - age_days * DAY
    for p in (os.path.join(path, "blob"), path):
        os.utime(p, (t, t))


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.home = str(tmp_path.resolve() / "home")
        os.makedirs(self.home + "/Library")
        monkeypatch.setattr(protect, "HOME", self.home)
        self.mp = monkeypatch
        self.spotlight = {}          # bundle id -> [路径]
        self.spotlight_fail = set()  # 这些 bundle id 查询抛异常
        self.spotlight_calls = []
        self.install()

        def fake_mdfind(bid):
            self.spotlight_calls.append(bid)
            if bid in self.spotlight_fail:
                raise RuntimeError("mdfind 失败")
            if bid == leftovers.SPOTLIGHT_SANITY_BID:
                return ["/System/Library/CoreServices/Finder.app"]
            return list(self.spotlight.get(bid, []))
        monkeypatch.setattr(leftovers, "_spotlight_apps", fake_mdfind)

    def install(self, *bids, complete=True):
        apps = make_apps(*bids, complete=complete)
        self.mp.setattr(scan, "installed_apps", lambda: apps)

    def lib(self, loc, name, age_days=90, size=4096):
        p = "%s/Library/%s/%s" % (self.home, loc, name)
        mkdir_aged(p, age_days, size)
        return p

    def probe(self, **kw):
        import types
        return leftovers.probe_leftovers(types.SimpleNamespace(**kw))


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def names(res):
    return sorted(c["name"] for c in res["candidates"])


def test_basic_candidate_fields(env):
    p = env.lib("Caches", "com.gone.OldApp", age_days=90, size=100000)
    res = env.probe()
    assert res["inventory_complete"] is True and res["min_age_days"] == 30
    assert len(res["candidates"]) == 1
    c = res["candidates"][0]
    assert c["path"] == p and c["name"] == "com.gone.OldApp" and c["location"] == "Caches"
    assert c["reason"] == "no_installed_app" and c["age_days"] >= 89 and c["bytes"] >= 100000
    assert {x["location"] for x in res["not_evaluated_locations"]} == {
        "~/Library/Preferences", "~/Library/Application Support", "~/Library/Containers",
        "~/Library/Group Containers"}


def test_all_five_low_risk_locations(env):
    env.lib("Caches", "com.gone.a")
    env.lib("Logs", "com.gone.b")
    env.lib("Saved Application State", "com.gone.c.savedState")
    env.lib("HTTPStorages", "com.gone.d")
    env.lib("WebKit", "com.gone.e")
    res = env.probe()
    assert names(res) == ["com.gone.a", "com.gone.b", "com.gone.c", "com.gone.d", "com.gone.e"]
    assert {c["location"] for c in res["candidates"]} == {
        "Caches", "Logs", "Saved Application State", "HTTPStorages", "WebKit"}


def test_incomplete_inventory_refuses(env):
    env.lib("Caches", "com.gone.OldApp")
    env.install("com.a.b.c", complete=False)
    res = env.probe()
    assert res["refused"] is True and "reason" in res and "candidates" not in res


def test_installed_apps_exception_refuses(env, monkeypatch):
    env.lib("Caches", "com.gone.OldApp")

    def boom():
        raise OSError("nope")
    monkeypatch.setattr(scan, "installed_apps", boom)
    res = env.probe()
    assert res["refused"] is True and "candidates" not in res


def test_installed_app_exact_skipped(env):
    env.install("com.vendor.Tool")
    env.lib("Caches", "com.vendor.Tool")
    res = env.probe()
    assert res["candidates"] == [] and res["skipped"] == {"app_installed:exact": 1}


def test_chrome_for_testing_not_candidate_when_chrome_installed(env):
    # reverse_lookup 的实际行为：com.google.Chrome.for.Testing 是已装 com.google.Chrome 的点分后代（lineage），
    # 视为“应用还在”，偏保守（可能漏报，不会误报）。
    env.install("com.google.Chrome")
    env.lib("Caches", "com.google.Chrome.for.Testing")
    env.lib("Caches", "com.google.Chrome")
    res = env.probe()
    assert res["candidates"] == []
    assert res["skipped"].get("app_installed:lineage") == 1 and res["skipped"].get("app_installed:exact") == 1


def test_apple_fedstats_not_candidate_of_stats(env):
    env.install("eu.exelban.Stats")
    env.lib("Caches", "com.apple.aiml.mlpt.FedStats.plugin")
    env.lib("Caches", "com.apple.priml.FedStatsPlugin.x")
    env.lib("Logs", "com.google.keystone.agent")
    env.lib("Caches", "com.google.Chrome.chrome_crashpad_handler")
    res = env.probe()
    assert res["candidates"] == []
    assert res["skipped"] == {"shared_component": 4}
    assert env.spotlight_calls == []


def test_qq_installed_wechat_installed_skips_wechat(env):
    env.install("com.tencent.qq", "com.tencent.xinWeChat")
    env.lib("Caches", "com.tencent.xinWeChat")
    env.lib("Caches", "com.tencent.xinWeChat.WeChatAppEx")
    env.lib("Caches", "com.tencent.qq")
    res = env.probe()
    assert res["candidates"] == []


def test_qq_installed_wechat_gone_is_wechat_candidate_not_qq(env):
    env.install("com.tencent.qq")
    env.lib("Caches", "com.tencent.xinWeChat")
    env.lib("Caches", "com.tencent.qq")
    res = env.probe()
    # 微信缓存归微信自己（bundle id com.tencent.xinWeChat），不会因同厂商被归给 QQ
    assert names(res) == ["com.tencent.xinWeChat"]
    assert res["skipped"] == {"app_installed:exact": 1}
    assert all(c["reason"] == "no_installed_app" for c in res["candidates"])


def test_non_bundle_id_names_skipped(env):
    for n in ("SDImageCache", "Kingfisher", "com.example", "com.example.", "a b.c.d", "com..x", "未知.应用.缓存",
              "com.foo.bar baz"):
        env.lib("Caches", n)
    env.lib("Logs", "DiagnosticReports")
    res = env.probe()
    assert res["candidates"] == []
    assert res["skipped"] == {"skipped_non_bundle_id": 9}


def test_too_recent_skipped_and_boundary(env):
    env.lib("Caches", "com.gone.fresh", age_days=5)
    env.lib("Caches", "com.gone.old", age_days=40)
    res = env.probe()
    assert names(res) == ["com.gone.old"] and res["skipped"] == {"too_recent": 1}
    res2 = env.probe(min_age_days=3)
    assert names(res2) == ["com.gone.fresh", "com.gone.old"]


def test_newest_nested_mtime_counts(env):
    p = env.lib("Caches", "com.gone.mixed", age_days=100)
    sub = os.path.join(p, "sub")
    os.makedirs(sub)
    f = os.path.join(sub, "recent.bin")
    open(f, "wb").write(b"1")           # 新文件，目录 mtime 也是现在
    old = time.time() - 100 * DAY
    os.utime(p, (old, old))             # 顶层目录看着很旧，但里面有新写入
    res = env.probe()
    assert res["candidates"] == [] and res["skipped"] == {"too_recent": 1}


def test_mdfind_found_app_skipped(env):
    env.lib("Caches", "com.setapp.Thing")
    env.lib("Caches", "com.gone.real")
    env.spotlight["com.setapp.Thing"] = ["/Users/x/Setapp/Thing.app"]
    res = env.probe()
    assert names(res) == ["com.gone.real"] and res["skipped"] == {"found_by_spotlight": 1}


def test_mdfind_failure_skipped(env):
    env.lib("Caches", "com.gone.real")
    env.spotlight_fail.add("com.gone.real")
    res = env.probe()
    assert res["candidates"] == [] and res["skipped"] == {"mdfind_failed": 1}


def test_spotlight_unavailable_skips_all(env, monkeypatch):
    env.lib("Caches", "com.gone.real")
    monkeypatch.setattr(leftovers, "_spotlight_apps", lambda bid: [])   # 索引关闭：全部静默返回空
    res = env.probe()
    assert res["candidates"] == [] and res["skipped"] == {"mdfind_failed": 1}
    assert any(e.get("stage") == "spotlight" for e in res["errors"])


def test_lookup_exception_skipped(env, monkeypatch):
    env.lib("Caches", "com.gone.real")

    def boom(path, apps=None):
        raise RuntimeError("plist 读失败")
    monkeypatch.setattr(scan, "reverse_lookup", boom)
    res = env.probe()
    assert res["candidates"] == [] and res["skipped"] == {"lookup_error": 1}


def test_lookup_inconclusive_skipped(env, monkeypatch):
    env.lib("Caches", "com.gone.real")
    monkeypatch.setattr(scan, "reverse_lookup",
                        lambda path, apps=None: {"app": None, "app_match": None, "app_installed": None})
    res = env.probe()
    assert res["candidates"] == [] and res["skipped"] == {"lookup_inconclusive": 1}


def test_walk_limit_skipped(env, monkeypatch):
    p = env.lib("Caches", "com.gone.big")
    for i in range(30):
        open(os.path.join(p, "f%d" % i), "wb").write(b"x")
    old = time.time() - 90 * DAY
    for f in os.listdir(p):
        os.utime(os.path.join(p, f), (old, old))
    os.utime(p, (old, old))
    monkeypatch.setattr(leftovers, "WALK_MAX_ENTRIES", 10)
    orig = leftovers._walk_stats
    monkeypatch.setattr(leftovers, "_walk_stats", lambda top: orig(top, max_entries=10))
    res = env.probe()
    assert res["candidates"] == [] and res["skipped"] == {"walk_limit_exceeded": 1}


def test_locations_outside_low_risk_never_listed(env):
    for loc in ("Preferences", "Application Support", "Containers", "Group Containers", "LaunchAgents",
                "Application Scripts"):
        env.lib(loc, "com.gone.Hidden")
    open(env.home + "/Library/Preferences/com.gone.pref.plist", "wb").write(b"x")
    os.makedirs(env.home + "/Documents/com.gone.docs")
    res = env.probe()
    assert res["candidates"] == [] and res["skipped"] == {}


def test_symlink_and_file_entries_not_candidates(env, tmp_path):
    real = tmp_path / "elsewhere"
    mkdir_aged(str(real), 90)
    os.makedirs(env.home + "/Library/Caches")
    os.symlink(str(real), env.home + "/Library/Caches/com.gone.link")
    open(env.home + "/Library/Caches/com.gone.file", "wb").write(b"x")
    res = env.probe()
    assert res["candidates"] == [] and res["skipped"] == {"not_plain_directory": 2}


def test_brew_zap_owned_by_installed_cask_removed(env, monkeypatch):
    own = env.lib("Caches", "com.cask.Owned")
    env.lib("Caches", "com.cask.WildOwned2")
    env.lib("Caches", "com.gone.real")
    casks = [{"token": "owned-app", "artifacts": [
        {"app": ["Owned.app"]},
        {"zap": [{"trash": ["~/Library/Caches/com.cask.Owned", "~/Library/Caches/com.cask.WildOwned*"]}]}]}]
    monkeypatch.setattr(leftovers, "_brew_installed_casks", lambda: casks)
    off = env.probe()   # 默认不查 brew
    assert names(off) == ["com.cask.Owned", "com.cask.WildOwned2", "com.gone.real"]
    on = env.probe(brew_zap=True)
    assert names(on) == ["com.gone.real"] and on["skipped"] == {"owned_by_installed_cask": 2}
    assert own not in [c["path"] for c in on["candidates"]]


def test_brew_failure_ignored(env, monkeypatch):
    env.lib("Caches", "com.gone.real")

    def boom():
        raise RuntimeError("brew 不在")
    monkeypatch.setattr(leftovers, "_brew_installed_casks", boom)
    res = env.probe(brew_zap=True)
    assert names(res) == ["com.gone.real"]
    assert any(e.get("stage") == "brew_zap" for e in res["errors"])


def test_bytes_use_blocks_and_not_follow_symlink(env, tmp_path):
    p = env.lib("Caches", "com.gone.sym", size=8192)
    big = tmp_path / "huge.bin"
    big.write_bytes(b"y" * 5_000_000)
    os.symlink(str(big), os.path.join(p, "link"))
    old = time.time() - 90 * DAY
    os.utime(os.path.join(p, "link"), (old, old), follow_symlinks=False)
    os.utime(p, (old, old))
    c = env.probe()["candidates"][0]
    st = os.lstat(p)
    expect = st.st_blocks * 512 + os.lstat(p + "/blob").st_blocks * 512 + os.lstat(p + "/link").st_blocks * 512
    assert c["bytes"] == expect and c["bytes"] < 1_000_000
