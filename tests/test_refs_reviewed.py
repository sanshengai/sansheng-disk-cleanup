"""结构化引用核对 refs_reviewed 的闸门测试（取代旧的「引用已核对:」字符串前缀）。

夹具沿用 test_plan_act 的 Env；每个用例都在 tmp_path 内。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, age, card, delete_item, mkfile, node, plan, reviewed  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore")

REFS = {"checked": True, "count": 2, "samples": ["docs/a.md:1", "docs/b.md:3"]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def setup(env, evidence, refs=REFS):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d, refs=refs)], [card("c01", [delete_item(d)], evidence=evidence)])
    return env.check()["cards"][0]


def test_valid_history_only_releases(env):
    c = setup(env, [reviewed("docs/a.md:1")])
    assert c["verdict_out"] == "clean", c["errors"]
    assert not c.get("warnings")


def test_sample_normalization_tolerates_format(env):
    # 去空白、./、重复斜杠、省略命中数都能对上
    c = setup(env, [reviewed("  ./docs//b.md ")])
    assert c["verdict_out"] == "clean", c["errors"]


def test_forged_samples_without_intersection_rejected(env):
    c = setup(env, [reviewed("docs/other.md:1", "nope.txt:9")])
    assert c["verdict_out"] == "keep" and c["downgraded"]
    assert any("refs_reviewed" in e for e in c["errors"])
    assert any("无交集" in w for w in c["warnings"])


def test_samples_of_other_node_do_not_count(env):
    d, e2 = env.work / "cache", env.work / "other"
    mkfile(d / "a.bin")
    mkfile(e2 / "b.bin")
    age(env.work)
    other = node(e2, id="n2", refs={"checked": True, "count": 1, "samples": ["z/z.md:1"]})
    env.write([node(d, refs=REFS), other], [card("c01", [delete_item(d)], evidence=[reviewed("z/z.md:1")])])
    assert env.check()["cards"][0]["verdict_out"] == "keep"


def test_depends_downgrades_to_keep(env):
    c = setup(env, [reviewed("docs/a.md:1", verdict="depends")])
    assert c["verdict_out"] == "keep" and c["downgraded"]
    assert any("depends" in e for e in c["errors"])


def test_depends_downgrades_even_without_references_needed(env):
    c = setup(env, [reviewed("anything", verdict="depends")], refs={"checked": True, "count": 0, "samples": []})
    assert c["verdict_out"] == "keep"


def test_legacy_prefix_warns_and_does_not_release(env):
    c = setup(env, ["引用已核对: 只是历史日志"])
    assert c["verdict_out"] == "keep"
    assert any("旧前缀" in w for w in c["warnings"])
    assert any("旧前缀" in e for e in c["errors"])


def test_legacy_prefix_cannot_rescue_unchecked_either(env):
    nf = {"checked": False, "count": 0, "samples": [], "reason": "non-frontier"}
    c = setup(env, ["引用已核对: 看过了"], refs=nf)
    assert c["verdict_out"] == "keep" and c["warnings"]


@pytest.mark.parametrize("reason", ["timeout-or-error", "pending", "--no-refs", "protected"])
def test_unchecked_refs_not_released_by_refs_reviewed(env, reason):
    refs = {"checked": False, "count": 0, "samples": [], "reason": reason}
    c = setup(env, [reviewed("docs/a.md:1")], refs=refs)
    assert c["verdict_out"] == "keep" and any("引用检查未完成" in e for e in c["errors"])


def test_unchecked_node_cannot_be_made_verified_even_with_matching_samples(env):
    # 节点没查（checked=False）却带着样本：样本不被采信，仍不放行
    refs = {"checked": False, "count": 0, "samples": ["docs/a.md:1"], "reason": "timeout-or-error"}
    assert setup(env, [reviewed("docs/a.md:1")], refs=refs)["verdict_out"] == "keep"


def test_malformed_refs_reviewed_ignored_with_warning(env):
    bad = [{"kind": "refs_reviewed", "samples": [], "verdict": "history_only"},
           {"kind": "refs_reviewed", "samples": ["docs/a.md:1"], "verdict": "maybe"}]
    for ev in bad:
        c = setup(env, [ev])
        assert c["verdict_out"] == "keep" and c["warnings"], ev


def test_refs_reviewed_is_not_end_evidence(env):
    r = plan.verify_evidence(reviewed("docs/a.md:1"), {"profile": {}, "item_paths": []})
    assert r["ok"] and r["end"] is False


def test_clean_with_only_refs_reviewed_still_needs_end_evidence(env):
    d = env.work / "build"
    mkfile(d / "a.bin")
    age(d)
    c = card("c01", [delete_item(d)])
    c["evidence"] = [reviewed("docs/a.md:1")]      # 去掉 card() 自带的结束证据
    env.write([node(d, refs=REFS)], [c])
    out = env.check()["cards"][0]
    assert out["verdict_out"] == "decide" and any("结束" in e for e in out["errors"])
