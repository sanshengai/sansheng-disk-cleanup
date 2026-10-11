"""降级原因附 judgment.md 小节引用：每种降级有 judgment_refs，quote 必须是 judgment.md 原文。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, age, card, delete_item, mkfile, node, plan, reviewed  # noqa: E402
from test_interactive import cx, codex_env  # noqa: E402
from test_plan_act import TID  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore")

JUDGMENT = plan.JUDGMENT_MD
TEXT = Path(JUDGMENT).read_text(encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def run_one(env, nodes, c):
    env.write(nodes, [c])
    return env.check()["cards"][0]


def assert_refs(out, kind):
    refs = out["judgment_refs"]
    assert refs, out
    title = plan.JUDGMENT_MAP[kind][0]
    r = next(x for x in refs if x["title"] == title)
    assert r["section"] and r["quote"] in TEXT, r
    return r


def cache(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    return d


def test_end_evidence_downgrade(env):
    d = cache(env)
    c = card("c01", [delete_item(d)])
    c["evidence"] = ["仅文字"]
    out = run_one(env, [node(d)], c)
    assert out["verdict_out"] == "decide"
    assert "结束要有正向证据" in assert_refs(out, "end_evidence")["quote"]


def test_refs_depends_downgrade(env):
    d = cache(env)
    refs = {"checked": True, "count": 1, "samples": ["docs/a.md:1"]}
    out = run_one(env, [node(d, refs=refs)], card("c01", [delete_item(d)], evidence=[reviewed("docs/a.md:1", verdict="depends")]))
    assert out["verdict_out"] == "keep"
    assert_refs(out, "refs_depends")


def test_refs_unreviewed_downgrade(env):
    d = cache(env)
    refs = {"checked": True, "count": 1, "samples": ["docs/a.md:1"]}
    out = run_one(env, [node(d, refs=refs)], card("c01", [delete_item(d)]))
    assert out["verdict_out"] == "keep"
    assert_refs(out, "refs_unreviewed")


def test_class_invalid_downgrade(env):
    d = cache(env)
    out = run_one(env, [node(d)], card("c01", [delete_item(d, **{"class": "bogus"})]))
    assert out["verdict_out"] == "keep"
    assert_refs(out, "class_invalid")


def test_interactive_session_downgrade(env):
    a, _, base = codex_env(env)
    out = run_one(env, [base], card("c01", [cx(a, TID, "interactive", TID)]))
    assert out["verdict_out"] == "decide"
    assert_refs(out, "interactive_session")


def test_multi_session_downgrade(env):
    import test_plan_act as t
    a, b, base = codex_env(env)
    out = run_one(env, [base], card("c01", [cx(a, t.TID, "interactive", t.TID), cx(b, t.TID2, "interactive", t.TID2)]))
    assert out["verdict_out"] == "keep"
    assert_refs(out, "multi_session")


def test_in_use_downgrade(env):
    d = cache(env)
    out = run_one(env, [node(d, recent_24h=3)], card("c01", [delete_item(d)]))
    assert out["verdict_out"] == "keep"
    assert_refs(out, "in_use")


def test_print_check_shows_quote(env, capsys):
    d = cache(env)
    env.write([node(d, recent_24h=3)], [card("c01", [delete_item(d)])])
    res, _ = plan.check_plan(str(env.run))
    plan._print_check(res)
    assert "依据 judgment.md §" in capsys.readouterr().out


def test_every_mapping_resolves_in_current_judgment():
    for kind in plan.JUDGMENT_MAP:
        r = plan.judgment_ref(kind)
        assert r["section"], f"{kind}: {r['quote']}"
        assert r["quote"] in TEXT, f"{kind}: {r['quote']}"
    for _, kind in plan.DOWNGRADE_PATTERNS:
        assert kind in plan.JUDGMENT_MAP


def test_missing_section_is_reported_not_silent():
    r = plan.judgment_ref("in_use", text="# x\n\n## 1. 别的\n- foo\n")
    assert r["section"] is None and "未找到对应小节" in r["quote"]
