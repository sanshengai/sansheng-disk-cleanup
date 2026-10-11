"""发布前评审 高1：主扫描没有可搜的引用目录时不能报“已查 0 处”；扫描之后台账 / 设置新增的成品目录，
扫描时没搜过，plan check 要求用 probe item-refs 补查（补查结果记录搜过哪些目录，check 据此判断是否覆盖）。"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, age, card, delete_item, mkfile, node  # noqa: E402
from test_scan import FAKE_CAP, MB, node_map, run_scan, write_file  # noqa: E402
import protect  # noqa: E402
import scan  # noqa: E402

NF = {"checked": False, "count": 0, "samples": [], "reason": "non-frontier"}
NSS = {"checked": False, "count": 0, "samples": [], "reason": "no-search-scope"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _fast_capacity(monkeypatch):
    monkeypatch.setattr(scan.sysprobe, "probe_capacity", lambda: dict(FAKE_CAP))


def write_profile(env, **kw):
    prof = env.home / ".sansheng" / "disk-cleanup" / "profile.json"
    prof.parent.mkdir(parents=True, exist_ok=True)
    prof.write_text(json.dumps(kw), encoding="utf-8")


def set_refs_search(env, roots):
    p = env.run / "facts.json"
    facts = json.loads(p.read_text())
    facts["coverage"]["refs_search"] = {"roots": roots, "worktree_scopes": False}
    p.write_text(json.dumps(facts), encoding="utf-8")


def probe(env, capsys):
    capsys.readouterr()
    assert scan.main(["probe", "item-refs", "--run", str(env.run), "--seconds", "60"]) == 0
    return json.loads(capsys.readouterr().out)


def verdict(env):
    return env.check()["cards"][0]


# ---------------------------------------------------------------- 搜索层与主扫描

def test_search_references_empty_scope_is_unchecked():
    res, stats = scan.search_references([{"key": "n1", "patterns": ["/x/y/build-2026-01-01"],
                                          "path": "/x/y/build-2026-01-01", "scopes": []}], [], time.monotonic() + 5)
    assert res["n1"]["checked"] is False and res["n1"]["reason"] == "no-search-scope" and stats == []


def test_main_scan_without_reference_roots_marks_nodes_unchecked(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scan, "HOME", str(tmp_path.resolve() / "home"))
    root = tmp_path.resolve() / "tree"
    target = root / "render-out-2026-09-01"
    write_file(str(target / "big.bin"), 3 * MB)
    write_file(str(root / "notes" / "plan.md"), 10, f"成片素材在 {target}\n".encode())
    facts = run_scan(tmp_path, [root], refs=True)           # profile 的 reference_roots 为空
    r = node_map(facts)[str(target)]["refs"]
    assert r["checked"] is False and r["reason"] == "no-search-scope" and r["count"] == 0, r
    assert facts["coverage"]["refs_search"] == {"roots": [], "worktree_scopes": False}
    scan.print_summary(str(tmp_path), facts)
    out = capsys.readouterr().out
    assert "引用未查" in out and "引用0" not in out and "没有可搜的引用检查目录" in out


def test_main_scan_records_searched_reference_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "HOME", str(tmp_path.resolve() / "home"))
    root = tmp_path.resolve() / "tree"
    plain = tmp_path.resolve() / "plain"
    write_file(str(root / "cache_dir" / "x.bin"), 2 * MB)
    write_file(str(plain / "notes.txt"), 1, b"nothing here")
    facts = run_scan(tmp_path, [root], refs=True, profile_extra={"reference_roots": [str(plain), str(plain / "missing")]})
    assert facts["coverage"]["refs_search"]["roots"] == [str(plain)]          # 不存在的目录没搜过，不记
    assert node_map(facts)[str(root / "cache_dir")]["refs"]["checked"] is True


# ---------------------------------------------------------------- plan check：no-search-scope

def setup_exact(env, mention=False, node_refs=None):
    d = env.work / "render-out-2026-09-01"
    mkfile(d / "a.bin")
    age(d)
    notes = env.home / "notes"
    notes.mkdir(exist_ok=True)
    (notes / "readme.md").write_text(f"成片在 {d}\n" if mention else "无关内容\n", encoding="utf-8")
    write_profile(env, reference_roots=["~/notes"])
    env.write([node(d, refs=dict(node_refs or NSS))], [card("c01", [delete_item(d)])])
    return d


def test_no_search_scope_node_is_kept_without_probe(env):
    setup_exact(env)
    c = verdict(env)
    assert c["verdict_out"] == "keep" and any("引用未查" in e and "item-refs" in e for e in c["errors"])


def test_no_search_scope_recovered_by_item_refs_zero_hits(env, capsys):
    d = setup_exact(env)
    r = probe(env, capsys)
    assert r["targets"] == 1 and r["checked"] == 1
    assert verdict(env)["verdict_out"] == "clean"


def test_no_search_scope_recovery_still_flags_real_reference(env, capsys):
    setup_exact(env, mention=True)
    probe(env, capsys)
    c = verdict(env)
    assert c["verdict_out"] == "keep" and any("定向引用检查发现" in e for e in c["errors"])


def test_no_search_scope_inside_node_not_rescued_by_reviews(env):
    """条目在 no-search-scope 的节点里面：refs_reviewed 也不能补救（没搜过就是没搜过）。"""
    d = env.work / "images"
    x = d / "run-2026-10-01-a1b2c3d4"
    mkfile(x / "a.png")
    age(d)
    write_profile(env)
    c0 = card("c01", [delete_item(x)], evidence=[{"kind": "refs_reviewed", "samples": ["a.md:1"], "verdict": "history_only"}])
    env.write([node(d, refs=dict(NSS))], [c0])
    c = verdict(env)
    assert c["verdict_out"] == "keep" and any("引用未查" in e for e in c["errors"])


# ---------------------------------------------------------------- plan check：扫描后新增的成品目录

def setup_stale(env, exact=True, mention=False):
    notes = env.home / "notes"
    notes.mkdir(exist_ok=True)
    if exact:
        d = env.work / "render-out-2026-09-01"
        item = d
        mkfile(d / "a.bin")
    else:
        d = env.work / "images"
        item = d / "run-2026-10-01-a1b2c3d4"
        mkfile(item / "a.png")
    age(d)
    (notes / "readme.md").write_text(f"成片在 {item}\n" if mention else "无关内容\n", encoding="utf-8")
    write_profile(env, reference_roots=["~/notes"])
    env.write([node(d)], [card("c01", [delete_item(item)])])        # 扫描时 refs.checked=True、0 引用
    set_refs_search(env, [])                                        # 但扫描时一个引用目录都没搜（成品目录是后来新增的）
    return item


@pytest.mark.parametrize("exact", [True, False])
def test_new_reference_root_after_scan_demotes_checked_nodes(env, exact):
    setup_stale(env, exact=exact)
    c = verdict(env)
    assert c["verdict_out"] == "keep"
    assert any("新增了成品目录" in e and "scan.py probe item-refs --run" in e for e in c["errors"]), c["errors"]


@pytest.mark.parametrize("exact", [True, False])
def test_item_refs_probe_restores_after_new_root(env, capsys, exact):
    setup_stale(env, exact=exact)
    r = probe(env, capsys)
    assert r["targets"] == 1 and r["checked"] == 1
    items = json.loads((env.run / "item_refs.json").read_text())["items"]
    assert list(items.values())[0]["roots"] == [str(env.home / "notes")]           # 记录搜过哪些目录
    assert verdict(env)["verdict_out"] == "clean"


@pytest.mark.parametrize("exact", [True, False])
def test_item_refs_after_new_root_finds_reference(env, capsys, exact):
    setup_stale(env, exact=exact, mention=True)
    probe(env, capsys)
    c = verdict(env)
    assert c["verdict_out"] == "keep" and any("定向引用检查发现" in e for e in c["errors"])


def test_item_refs_not_covering_current_roots_is_not_trusted(env, capsys):
    """补查之后又新增了目录：补查记录的搜索根不覆盖当前全部引用检查目录，仍要重新补查。"""
    item = setup_stale(env)
    probe(env, capsys)
    assert verdict(env)["verdict_out"] == "clean"
    more = env.home / "more"
    more.mkdir()
    write_profile(env, reference_roots=["~/notes", "~/more"])
    c = verdict(env)
    assert c["verdict_out"] == "keep" and any("新增了成品目录" in e for e in c["errors"])
    probe(env, capsys)
    assert verdict(env)["verdict_out"] == "clean"


def test_item_refs_without_recorded_roots_not_trusted_when_stale(env, capsys):
    setup_stale(env)
    probe(env, capsys)
    p = env.run / "item_refs.json"
    data = json.loads(p.read_text())
    for v in data["items"].values():
        v.pop("roots", None)
    p.write_text(json.dumps(data), encoding="utf-8")
    assert verdict(env)["verdict_out"] == "keep"


def test_ledger_deliverables_count_as_new_reference_roots(env, capsys):
    """台账里项目的成品目录并入有效 profile 的 reference_roots：扫描时没有的，也算新增。"""
    d = env.work / "render-out-2026-09-01"
    mkfile(d / "a.bin")
    age(d)
    out = env.home / "Deliver"
    out.mkdir()
    write_profile(env, projects={str(env.home / "proj"): {"name": "p", "status": "done", "deliverables": [str(out)]}})
    env.write([node(d)], [card("c01", [delete_item(d)])])
    set_refs_search(env, [])
    assert protect.ref_roots_real(protect.load_profile(protect.PROFILE)) == [str(out)]
    assert verdict(env)["verdict_out"] == "keep"
    probe(env, capsys)
    assert verdict(env)["verdict_out"] == "clean"


def test_no_stale_when_scan_already_covered_roots(env):
    """扫描时已经搜过全部引用目录（含上层目录覆盖下层）：不算新增，行为不变。"""
    d = env.work / "render-out-2026-09-01"
    mkfile(d / "a.bin")
    age(d)
    (env.home / "notes").mkdir()
    write_profile(env, reference_roots=["~/notes"])
    env.write([node(d)], [card("c01", [delete_item(d)])])
    set_refs_search(env, [str(env.home)])                           # 上层目录已搜，覆盖 ~/notes
    assert verdict(env)["verdict_out"] == "clean"
