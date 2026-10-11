"""0.3.1：落在大目录内部的条目，扫描时没逐个查引用；scan.py probe item-refs 按条目路径补查，plan check 读结果。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, age, card, delete_item, mkfile, node  # noqa: E402
import scan  # noqa: E402

NF = {"checked": False, "count": 0, "samples": [], "reason": "non-frontier"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def setup(env, mention=False):
    d = env.work / "images"
    keep_ref = d / "run-2026-10-01-a1b2c3d4"
    other = d / "run-2026-10-02-e5f6a7b8"
    for x in (keep_ref, other):
        mkfile(x / "a.png")
    age(d)
    notes = env.home / "notes"
    notes.mkdir()
    (notes / "readme.md").write_text(f"见 {keep_ref}\n" if mention else "无关内容\n", encoding="utf-8")
    prof = env.home / ".sansheng" / "disk-cleanup" / "profile.json"
    prof.parent.mkdir(parents=True, exist_ok=True)
    prof.write_text(json.dumps({"reference_roots": ["~/notes"]}), encoding="utf-8")
    env.write([node(d, refs=dict(NF))], [card("c01", [delete_item(keep_ref)]), card("c02", [delete_item(other)])])
    return keep_ref, other


def probe(env, capsys):
    assert scan.main(["probe", "item-refs", "--run", str(env.run), "--seconds", "60"]) == 0
    return json.loads(capsys.readouterr().out)


def verdicts(env):
    return {c["id"]: c["verdict_out"] for c in env.check()["cards"]}


def test_without_probe_items_stay_unverified(env):
    setup(env)
    assert verdicts(env) == {"c01": "keep", "c02": "keep"}


def test_probe_zero_refs_clears_items(env, capsys):
    setup(env, mention=False)
    r = probe(env, capsys)
    assert r["targets"] == 2 and r["checked"] == 2 and r["with_refs"] == 0
    out = verdicts(env)
    assert out == {"c01": "clean", "c02": "clean"}


def test_probe_finds_reference_and_requires_review(env, capsys):
    keep_ref, _ = setup(env, mention=True)
    probe(env, capsys)
    items = json.loads((env.run / "item_refs.json").read_text())["items"]
    hit = items[str(keep_ref)]
    assert hit["checked"] and hit["count"] == 1 and hit["samples"]
    assert verdicts(env) == {"c01": "keep", "c02": "clean"}       # 有引用又没复核样本：留着


def test_item_refs_from_other_scan_ignored(env, capsys):
    setup(env)
    probe(env, capsys)
    p = env.run / "item_refs.json"
    data = json.loads(p.read_text())
    data["scan_id"] = "other"
    p.write_text(json.dumps(data), encoding="utf-8")
    assert verdicts(env) == {"c01": "keep", "c02": "keep"}


def test_bigfiles_lists_old_files_and_skips_recent_git_protected(env, capsys, monkeypatch):
    import os
    monkeypatch.setattr(scan, "HOME", str(env.home))
    dl = env.home / "Downloads"
    old = mkfile(dl / "Foo-1.2.dmg", b"x" * 300000)
    new = mkfile(dl / "new.zip", b"x" * 300000)
    repo = dl / "proj"
    (repo / ".git").mkdir(parents=True)
    mkfile(repo / "big.mov", b"x" * 300000)
    mkfile(env.home / "Movies" / "录屏" / "rec.mov", b"x" * 300000)
    mkfile(env.home / "Movies" / "top.mov", b"x" * 300000)          # ~/Movies 直接子项受保护（契约第 5 节）
    for f in (old, repo / "big.mov", env.home / "Movies" / "录屏" / "rec.mov", env.home / "Movies" / "top.mov"):
        os.utime(f, (1, 1))
    assert scan.main(["probe", "bigfiles", "--min-mb", "0", "--min-age-days", "30"]) == 0
    r = json.loads(capsys.readouterr().out)
    got = {f["path"].split("/")[-1]: f for f in r["files"]}
    assert set(got) == {"Foo-1.2.dmg", "rec.mov"}
    assert got["Foo-1.2.dmg"]["kind"] == "installer" and got["rec.mov"]["kind"] == "video"
    assert r["skipped"]["recent"] >= 1 and r["skipped"]["git"] >= 1 and r["skipped"]["protected"] >= 1


def test_probe_without_any_search_scope_stays_unchecked(env, capsys):
    """0.4 评审 1：默认 profile 没有 reference_roots、条目又不在 git 工作树里时，一个文件都没搜，不能报“零引用”放行。"""
    keep_ref, _ = setup(env, mention=True)
    prof = env.home / ".sansheng" / "disk-cleanup" / "profile.json"
    prof.write_text(json.dumps({}), encoding="utf-8")
    r = probe(env, capsys)
    assert r["checked"] == 0 and r["unchecked"] == 2
    item = json.loads((env.run / "item_refs.json").read_text())["items"][str(keep_ref)]
    assert item["checked"] is False and item["reason"] == "no-search-scope"
    assert verdicts(env) == {"c01": "keep", "c02": "keep"}


# ---------------------------------------------------------------- 0.5：条目正好等于某个非前沿节点本身

def setup_exact(env, mention=False):
    """条目路径 == 非前沿节点（whisper 模型目录、android-sdk 这类）：扫描时 refs 是 non-frontier，没人查过它自己的引用。"""
    d = env.work / "whisper-models"
    mkfile(d / "ggml-base.bin")
    age(d)
    notes = env.home / "notes"
    notes.mkdir()
    (notes / "readme.md").write_text(f"模型放在 {d}\n" if mention else "无关内容\n", encoding="utf-8")
    prof = env.home / ".sansheng" / "disk-cleanup" / "profile.json"
    prof.parent.mkdir(parents=True, exist_ok=True)
    prof.write_text(json.dumps({"reference_roots": ["~/notes"]}), encoding="utf-8")
    env.write([node(d, refs=dict(NF))], [card("c01", [delete_item(d)])])
    return d


def test_exact_nonfrontier_without_probe_still_unfinished(env):
    setup_exact(env)
    c = env.check()["cards"][0]
    assert c["verdict_out"] == "keep" and any("引用检查未完成" in e for e in c["errors"])


def test_probe_targets_exact_nonfrontier_node_and_zero_refs_clears(env, capsys):
    d = setup_exact(env, mention=False)
    r = probe(env, capsys)
    assert r["targets"] == 1 and r["checked"] == 1 and r["with_refs"] == 0
    got = json.loads((env.run / "item_refs.json").read_text())["items"][str(d)]
    assert got["checked"] is True and got["count"] == 0
    c = env.check()["cards"][0]
    assert c["verdict_out"] == "clean" and not c["errors"] and not c.get("risk_notes")


def test_exact_nonfrontier_with_refs_needs_review_with_matching_sample(env, capsys):
    d = setup_exact(env, mention=True)
    probe(env, capsys)
    items = json.loads((env.run / "item_refs.json").read_text())["items"]
    hit = items[str(d)]
    assert hit["checked"] and hit["count"] == 1 and hit["samples"]
    c = env.check()["cards"][0]
    assert c["verdict_out"] == "keep" and any("定向引用检查发现" in e for e in c["errors"])
    # 样本与 item_refs 对不上（伪造）：仍然 keep
    pl = json.loads((env.run / "plan.json").read_text())
    pl["cards"][0]["evidence"].append({"kind": "refs_reviewed", "samples": ["bogus.md:9"], "verdict": "history_only"})
    (env.run / "plan.json").write_text(json.dumps(pl), encoding="utf-8")
    assert env.check()["cards"][0]["verdict_out"] == "keep"
    # 取自 item_refs.json 的样本、只是历史记录：放行
    pl["cards"][0]["evidence"][-1] = {"kind": "refs_reviewed", "samples": list(hit["samples"]), "verdict": "history_only"}
    (env.run / "plan.json").write_text(json.dumps(pl), encoding="utf-8")
    assert env.check()["cards"][0]["verdict_out"] == "clean"


def test_exact_node_other_unchecked_reasons_not_probed(env, capsys):
    """超时、出错等其他原因的未检查仍不可补救，不在探针目标内。"""
    d = setup_exact(env)
    n = node(d, refs={"checked": False, "count": 0, "samples": [], "reason": "timeout"})
    env.write([n], [card("c01", [delete_item(d)])])
    assert probe(env, capsys)["targets"] == 0
    assert env.check()["cards"][0]["verdict_out"] == "keep"


def test_exact_nonfrontier_still_checks_node_level_gates(env, capsys):
    """定向引用零命中只代替引用检查，24 小时改动、被进程打开等节点级核对照旧。"""
    d = setup_exact(env)
    env.write([node(d, refs=dict(NF), recent_24h=3)], [card("c01", [delete_item(d)])])
    probe(env, capsys)
    c = env.check()["cards"][0]
    assert c["verdict_out"] == "keep" and any("24 小时内" in e for e in c["errors"])
