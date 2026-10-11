"""P5.R 修复：--sel 强制、封存 prog 必填、回执伪造核对、试点取条目、producers 规则、updaters/leftovers 低危。
夹具全部在 tmp_path；act 部分沿用 test_plan_act 的 Env。"""
import glob
import json
import os
import plistlib
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_leftovers  # noqa: E402
import test_updaters  # noqa: E402
from test_plan_act import Env, act, age, auto_sel, card, delete_item, mkfile, node, plan, protect  # noqa: E402,F401

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import leftovers  # noqa: E402
import scan  # noqa: E402
import updaters  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def uenv(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Library" / "Caches").mkdir(parents=True)
    monkeypatch.setattr(protect, "HOME", str(home))
    state = types.SimpleNamespace(home=home, inv=test_updaters.inventory([]), open_files=[], procs=[])
    monkeypatch.setattr(scan, "installed_apps", lambda: state.inv)
    monkeypatch.setattr(updaters, "lsof_open", lambda paths: state.open_files)
    monkeypatch.setattr(updaters, "pgrep_processes", lambda: state.procs)
    state.caches = home / "Library" / "Caches"
    return state


@pytest.fixture
def lenv(tmp_path, monkeypatch):
    return test_leftovers.Env(tmp_path, monkeypatch)


def two(env, big_second=False):
    """两张 clean 卡 c01/c02，各一个目录。返回 (digest, a, b)。"""
    a, b = env.work / "a", env.work / "b"
    mkfile(a / "x.bin")
    mkfile(b / "y.bin")
    age(env.work)
    env.write([node(a, id="n1"), node(b, id="n2")], [card("c01", [delete_item(a)]), card("c02", [delete_item(b)])])
    return env.seal(), a, b


def argv_of(env, digest, cards, *flags, answers=()):
    out = ["run", "--run", str(env.run), "--seal", digest, "--cards", cards]
    if answers:
        out += ["--answers", *answers]
    return out + list(flags)


def no_receipt(env):
    return not (env.run / "receipt.json").exists()


# ───────────────────────── H4：--sel 对试点与正式执行强制 ─────────────────────────

def test_pilot_without_sel_is_rejected_and_deletes_nothing(env, capsys):
    digest, a, b = two(env)
    assert act.main(argv_of(env, digest, "c01", "--pilot")) == 2
    assert "必须带 --sel" in capsys.readouterr().err
    assert a.exists() and b.exists() and not glob.glob(str(env.run / "receipt-pilot-*.json"))


def test_full_run_without_sel_is_rejected_even_with_skip_pilot(env, capsys):
    digest, a, b = two(env)
    assert act.main(auto_sel(argv_of(env, digest, "c01", "--dry-run"))) == 0
    capsys.readouterr()
    assert act.main(argv_of(env, digest, "c01", "--skip-pilot", "--reason", "x")) == 2
    assert "必须带 --sel" in capsys.readouterr().err
    assert a.exists() and no_receipt(env)


def test_dry_run_without_sel_is_still_allowed(env):
    digest, a, b = two(env)
    assert act.main(argv_of(env, digest, "c01,c02", "--dry-run")) == 0
    assert a.exists() and b.exists()


def test_wrong_cards_with_board_sel_is_rejected(env, capsys):
    """卡号抄错（多加 c02）但带着看板 sel：对不上，一张都不动。"""
    digest, a, b = two(env)
    board_sel = plan.selection_sig(digest, ["c01"], {})
    assert act.main(argv_of(env, digest, "c01,c02", "--pilot", "--sel", board_sel)) == 2
    assert "不一致" in capsys.readouterr().err
    assert a.exists() and b.exists()


# ───────────────────────── 低危：封存缺 prog ─────────────────────────

def native_forged(env, with_prog):
    target = env.home / ".npm"
    mkfile(target / "a.bin")
    age(target)
    argv = ["npm", "cache", "clean", "--force"]
    item = {"path": str(target), "method": "native", "argv": argv}
    if with_prog:
        item["prog"] = protect.resolve_prog("npm")
    return target, env.forge({"c01": ("clean", [item])})


def test_native_item_without_prog_is_refused(env):
    target, digest = native_forged(env, with_prog=False)
    calls = []
    env.mp.setattr(subprocess, "run", lambda cmd, *a, **kw: calls.append(cmd))
    assert env.act("c01", digest=digest) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert calls == [] and it["status"] == "skipped" and "prog" in it["reason"]
    assert (target / "a.bin").exists()


def test_native_item_with_prog_still_runs(env):
    target, digest = native_forged(env, with_prog=True)
    env.mp.setattr(subprocess, "run", lambda cmd, *a, **kw: (__import__("shutil").rmtree(target),
                                                              subprocess.CompletedProcess(cmd, 0, "ok", ""))[1])
    assert env.act("c01", digest=digest) == 0
    assert env.receipt()["cards"][0]["items"][0]["status"] == "removed"


# ───────────────────────── 低危：回执伪造 ─────────────────────────

def forged_receipt(env, digest, sel, mode, cards):
    rc = {"schema": "receipt/1", "scan_id": "scan1", "seal": digest, "sel": sel, "mode": mode,
          "dry_run": mode == "dry_run", "cards": cards}
    if mode == "pilot":
        rc["pilot_done"] = True
    name = {"dry_run": "receipt-dryrun-forged.json", "pilot": "receipt-pilot-forged.json"}[mode]
    (env.run / name).write_text(json.dumps(rc))


def removed_item(path):
    return {"path": str(path), "method": "delete", "status": "removed", "failed_count": 0,
            "removed": {"files": 1, "dirs": 0, "bytes": 5000, "sample": []}}


def test_forged_pilot_receipt_for_still_existing_path_is_not_accepted(env, capsys):
    digest, a, b = two(env)
    sel = plan.selection_sig(digest, ["c01"], {})
    assert act.main(argv_of(env, digest, "c01", "--dry-run", "--sel", sel)) == 0
    forged_receipt(env, digest, sel, "pilot", [{"id": "c01", "items": [removed_item(a)]}])
    assert act.find_gate(str(env.run), plan.read_json(str(env.run / "seal.json")), sel, ["c01"])[1] is None
    capsys.readouterr()
    assert act.main(argv_of(env, digest, "c01", "--sel", sel)) == 2
    assert "试点" in capsys.readouterr().err
    assert a.exists() and no_receipt(env)


def test_forged_pilot_receipt_for_path_outside_seal_is_not_accepted(env):
    digest, a, b = two(env)
    sel = plan.selection_sig(digest, ["c01"], {})
    gone = env.work / "not-in-seal"
    forged_receipt(env, digest, sel, "pilot", [{"id": "c01", "items": [removed_item(gone)]}])
    seal = plan.read_json(str(env.run / "seal.json"))
    assert act.find_gate(str(env.run), seal, sel, ["c01"])[1] is None


def test_real_pilot_receipt_is_accepted_but_not_once_the_path_comes_back(env):
    digest, a, b = two(env)
    sel = plan.selection_sig(digest, ["c01"], {})
    assert act.main(argv_of(env, digest, "c01", "--pilot", "--sel", sel)) == 0
    assert not a.exists()
    seal = plan.read_json(str(env.run / "seal.json"))
    assert act.find_gate(str(env.run), seal, sel, ["c01"])[1] is not None
    mkfile(a / "x.bin")             # 回执说删了，现场又有了：不再认
    assert act.find_gate(str(env.run), seal, sel, ["c01"])[1] is None


def test_dry_run_receipt_with_other_card_set_is_not_accepted(env):
    digest, a, b = two(env)
    sel = plan.selection_sig(digest, ["c01"], {})
    forged_receipt(env, digest, sel, "dry_run", [{"id": "c02", "items": []}])
    seal = plan.read_json(str(env.run / "seal.json"))
    assert act.find_gate(str(env.run), seal, sel, ["c01"])[0] is None
    forged_receipt(env, digest, sel, "dry_run", [{"id": "c01", "items": []}])
    assert act.find_gate(str(env.run), seal, sel, ["c01"])[0] is not None


# ───────────────────────── 低危：试点取最大条目 / decide 优先 ─────────────────────────

def test_pilot_runs_the_largest_item_not_the_first_card(env):
    small, big = env.work / "small", env.work / "big"
    mkfile(small / "x.bin")
    mkfile(big / "y.bin")
    age(env.work)
    digest = env.forge({"c01": ("clean", [{**delete_item(small), "bytes": 10}]),
                        "c02": ("clean", [{**delete_item(big), "bytes": 9000}])})
    sel = plan.selection_sig(digest, ["c01", "c02"], {})
    assert act.main(argv_of(env, digest, "c01,c02", "--pilot", "--sel", sel)) == 0
    assert small.exists() and not big.exists()
    rc = json.loads(next(env.run.glob("receipt-pilot-*.json")).read_text())
    assert [c["id"] for c in rc["cards"]] == ["c01", "c02"]          # 回执仍按选择顺序
    assert rc["cards"][0]["status"] == "skipped" and rc["cards"][0]["items"] == []


def test_pilot_prefers_decide_card_item_over_bigger_clean_item(env):
    big, dec = env.work / "big", env.work / "dec"
    mkfile(big / "y.bin")
    mkfile(dec / "z.bin")
    age(env.work)
    digest = env.forge({"c01": ("clean", [{**delete_item(big), "bytes": 9000}]),
                        "c02": ("decide", [{**delete_item(dec), "bytes": 10}])})
    ans = ["c02=删"]
    sel = plan.selection_sig(digest, ["c01", "c02"], act.parse_answers(ans))
    assert act.main(argv_of(env, digest, "c01", "--pilot", "--sel", sel, answers=ans)) == 0
    assert big.exists() and not dec.exists()


def test_pilot_falls_through_to_next_item_when_the_largest_is_skipped(env):
    big, small = env.work / "big", env.work / "small"
    mkfile(big / "y.bin")
    mkfile(small / "x.bin")
    age(env.work)
    mkfile(big / "fresh.bin")            # 24 小时内有改动：最大的这条会被跳过
    digest = env.forge({"c01": ("clean", [{**delete_item(small), "bytes": 10}, {**delete_item(big), "bytes": 9000}])})
    sel = plan.selection_sig(digest, ["c01"], {})
    assert act.main(argv_of(env, digest, "c01", "--pilot", "--sel", sel)) == 0
    assert big.exists() and not small.exists()
    rc = json.loads(next(env.run.glob("receipt-pilot-*.json")).read_text())
    assert [i["status"] for i in rc["cards"][0]["items"]] == ["skipped", "removed"]


# ───────────────────────── H3 第 2 点：producers 规则 ─────────────────────────

H = scan.HOME


def match(rel):
    return scan.Producers().match(os.path.join(H, rel))


RISKY = [
    "Library/Developer/Xcode/Archives/2026-01-01/App.xcarchive", "Library/Developer/Xcode/UserData/KeyBindings/x",
    ".android/avd/Pixel_6.avd", ".android/debug.keystore", "work/app/release.keystore", "work/app/upload.jks",
    ".sansheng/yiye-english-app/release.keystore", ".gradle/gradle.properties", ".m2/settings.xml",
    ".m2/settings-security.xml", ".cargo/credentials.toml", ".cargo/credentials", ".cargo/bin/rg",
    ".pub-cache/credentials.json", ".cache/huggingface/token", ".lmstudio/conversations/a.json",
    ".ollama/id_ed25519", ".ollama/id_ed25519.pub",
    "Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw",
    "Library/Containers/com.docker.docker/Data/vms/1/Docker.raw",
    "Library/Containers/com.apple.Safari/Data/Library",
]
SAFE = [
    ".npm/_cacache", "Library/Caches/pip", ".cache/uv", "Library/Caches/Homebrew", "Library/Caches/ms-playwright/chromium-1",
    ".bun/install/cache", ".gradle/caches/modules-2/files-2.1", "Library/Developer/Xcode/DerivedData/App-abc",
    "Library/Caches/Google/Chrome/Default/Cache", "Library/Application Support/Google/Chrome/Default/GPUCache",
]
MODERATE = ["Library/Caches", "Library/Caches/com.some.Unknown", ".ollama/models/blobs", ".lmstudio/models/x",
            ".cache/huggingface/hub/models--a--b", ".gradle/wrapper/dists", ".m2/repository", ".cargo/registry",
            ".android/sdk", "Library/Developer/CoreSimulator/Devices/AAAA", ".npm/_logs_other"]


@pytest.mark.parametrize("rel", RISKY)
def test_irreplaceable_paths_are_risky(rel):
    assert match(rel).get("safety") == "risky", (rel, match(rel))


@pytest.mark.parametrize("rel", SAFE)
def test_regenerable_caches_are_safe(rel):
    assert match(rel).get("safety") == "safe", (rel, match(rel))


@pytest.mark.parametrize("rel", MODERATE)
def test_generic_fallbacks_are_moderate_not_safe(rel):
    # .android/sdk 等没有专门规则的路径，只能落到通用兜底（moderate），不能是 safe
    got = match(rel).get("safety")
    assert got in ("moderate", None) and got != "safe", (rel, got)
    if rel in ("Library/Caches", "Library/Caches/com.some.Unknown", ".ollama/models/blobs", ".m2/repository"):
        assert got == "moderate"


def test_no_safe_rule_covers_a_broad_container_directory():
    """safe 只给具体的缓存目录：~/Library/Caches、~/.cache、~/.gradle 这类通用/混合位置本身不能是 safe。"""
    for rel in ("Library/Caches", ".cache", ".gradle", ".cargo", ".m2", ".ollama", ".lmstudio", ".android",
                "Library/Developer", ".pub-cache", ".npm", ".bun", ".yarn", "Library/pnpm"):
        assert match(rel).get("safety") != "safe", rel


# 进程闸门用 pgrep -x：名字必须等于应用的 CFBundleExecutable，否则闸门静默失效（BaiduNetdisk_mac 就是这样漏的）
APP_PROCS = [("Google Chrome", "Google Chrome"), ("115Browser", "115Browser"), ("BaiduNetdisk", "BaiduNetdisk"),
             ("WeChat", "WeChat"), ("Safari", "Safari"), ("IINA", "IINA"), ("iMovie", "iMovie"), ("Music", "Music"),
             ("Podcasts", "Podcasts"), ("Xcode", "Xcode"), ("firefox", "Firefox"), ("Spotify", "Spotify"),
             ("Slack", "Slack"), ("Discord", "Discord"), ("Dropbox", "Dropbox"), ("zoom.us", "zoom.us"),
             ("steam_osx", "Steam")]


def _app_plist(app):
    for base in ("/Applications", "/System/Applications", os.path.expanduser("~/Applications")):
        p = os.path.join(base, app + ".app", "Contents", "Info.plist")
        if os.path.exists(p):
            return p
    return None


def _all_blocked_names():
    data = json.loads((SCRIPTS / "producers.json").read_text(encoding="utf-8"))
    return {n for r in data["rules"] for n in r.get("blocked_by_processes", [])}


@pytest.mark.parametrize("proc,app", APP_PROCS)
def test_blocked_process_name_equals_bundle_executable_of_installed_app(proc, app):
    assert proc in _all_blocked_names(), f"{proc} 不在任何规则的 blocked_by_processes 里（核对表过期？）"
    plist = _app_plist(app)
    if plist is None:
        pytest.skip(f"{app}.app 未安装")
    with open(plist, "rb") as f:
        assert plistlib.load(f)["CFBundleExecutable"] == proc


def test_baidu_netdisk_rule_uses_real_executable_name():
    assert match("Library/Containers/com.baidu.netdisk").get("blocked_by_processes") == ["BaiduNetdisk"]


# ───────────────────────── 低危：updaters ─────────────────────────

def test_updater_dir_with_unclassified_file_is_not_superseded(uenv):
    uenv.inv = test_updaters.inventory([test_updaters.installed("com.acme.foo", "1.0")])
    test_updaters.electron(uenv, "1.0")
    it = test_updaters.only(test_updaters.probe(), "foo-updater")
    assert it["verdict"] == "superseded"              # 基线：只有认得的包时照旧
    (uenv.caches / "foo-updater" / "pending" / "delta-2.bin.part").write_bytes(b"?" * 100)
    it = test_updaters.only(test_updaters.probe(), "foo-updater")
    assert it["verdict"] == "unknown" and it["reason"] == "unclassified_files"
    assert "未被分类" in it["unclassified_note"]


def test_updater_dir_with_only_benign_files_stays_superseded(uenv):
    uenv.inv = test_updaters.inventory([test_updaters.installed("com.acme.foo", "1.0")])
    test_updaters.electron(uenv, "1.0")
    (uenv.caches / "foo-updater" / ".DS_Store").write_bytes(b"x")
    assert test_updaters.only(test_updaters.probe(), "foo-updater")["verdict"] == "superseded"


def test_updater_unknown_in_use_is_not_superseded(uenv, monkeypatch):
    uenv.inv = test_updaters.inventory([test_updaters.installed("com.acme.foo", "1.0")])
    test_updaters.electron(uenv, "1.0")
    monkeypatch.setattr(updaters, "lsof_open", lambda paths: None)
    it = test_updaters.only(test_updaters.probe(), "foo-updater")
    assert it["in_use"] is None and it["verdict"] == "unknown" and it["reason"] == "in_use_unknown"


# ───────────────────────── 低危：leftovers ─────────────────────────

def test_webkit_and_httpstorages_candidates_are_moderate_and_not_default_selected(lenv):
    lenv.lib("WebKit", "com.gone.OldApp")
    lenv.lib("HTTPStorages", "com.gone.OldApp2")
    lenv.lib("Caches", "com.gone.OldApp3")
    res = lenv.probe()
    by = {c["name"]: c for c in res["candidates"]}
    for n in ("com.gone.OldApp", "com.gone.OldApp2"):
        assert by[n]["safety"] == "moderate" and by[n]["default_select"] is False and "登录态" in by[n]["note"]
    assert "safety" not in by["com.gone.OldApp3"]
