"""看板选择指纹 sel：看板导出文本里的 `sel:` 与 `act.py run --sel` 的一致性。

夹具全在 tmp_path 内（沿用 test_plan_act 的 Env）；拒绝类用例都用真实（非 dry-run）执行，
并断言文件原封不动、没有回执，证明被拒时什么都没删。
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, age, card, delete_item, mkfile, node, plan, with_gate  # noqa: E402,F401

DASHBOARD = Path(__file__).resolve().parent.parent / "assets" / "dashboard.html"

# 固定输入 → 期望值：JS 与 Python 两端都必须算出这些值（无 node 时靠它对照）
FIXED = [
    ("a1b2c3d4e5f6", ["c03", "c01"], ["c05=留", "c02=删"], "d98580a7f052"),
    ("a1b2c3d4e5f6", [], [], None),
    ("000000000000", ["c01"], [], None),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def setup_three(env):
    """c01、c03 为 clean，c02 为 decide；返回 (digest, {卡号: 目录})。"""
    dirs = {}
    cards = []
    nodes = []
    for i, (cid, verdict) in enumerate((("c01", "clean"), ("c02", "decide"), ("c03", "clean")), 1):
        d = env.work / f"cache{cid}"
        mkfile(d / "a.bin")
        age(d)
        dirs[cid] = d
        nodes.append(node(d, id=f"n{i}"))
        cards.append(card(cid, [delete_item(d)], verdict=verdict, question="还要吗？" if verdict == "decide" else None))
    env.write(nodes, cards)
    return env.seal(), dirs


def run(env, digest, cards, answers=(), sel=None, dry=False):
    argv = ["run", "--run", str(env.run), "--seal", digest, "--cards", cards]
    if answers:
        argv += ["--answers", *answers]
    if sel is not None:
        argv += ["--sel", sel]
    if dry:
        argv.append("--dry-run")
    else:
        argv = with_gate(argv)
    return act.main(argv)


def assert_nothing_happened(env, dirs):
    assert all((d / "a.bin").exists() for d in dirs.values())
    assert not [p for p in env.run.glob("receipt*.json") if "-dryrun-" not in p.name]


def test_formula_is_canonical_and_order_independent():
    a = plan.selection_sig("a1b2c3d4e5f6", ["c03", "c01"], {"c05": "留", "c02": "删"})
    b = plan.selection_sig("a1b2c3d4e5f6", ["c01", "c03", "c01"], ["c02=删", "c05=留"])
    assert a == b == FIXED[0][3]
    assert plan.selection_sig("a1b2c3d4e5f6", ["c01"], []) != plan.selection_sig("000000000000", ["c01"], [])
    blob = '{"answers":["c02=删","c05=留"],"cards":["c01","c03"],"seal":"a1b2c3d4e5f6"}'
    import hashlib
    assert hashlib.sha256(blob.encode()).hexdigest()[:12] == a


def test_correct_sel_passes_dry_run_and_real_run(env):
    digest, dirs = setup_three(env)
    sel = plan.selection_sig(digest, ["c01", "c03"], {})
    assert run(env, digest, "c01,c03", sel=sel, dry=True) == 0
    assert_nothing_happened(env, dirs)                               # 预演不删
    assert run(env, digest, "c03,c01", sel=sel.upper()) == 0         # 顺序、大小写不影响
    assert not dirs["c01"].exists() and not dirs["c03"].exists() and dirs["c02"].exists()


def test_typo_card_number_is_rejected(env, capsys):
    digest, dirs = setup_three(env)
    sel = plan.selection_sig(digest, ["c01", "c03"], {})
    assert run(env, digest, "c01,c02", sel=sel) == 2                 # 抄错：c03 写成了 c02
    assert "不一致" in capsys.readouterr().err
    assert_nothing_happened(env, dirs)
    assert run(env, digest, "c01,c04", sel=sel) == 2                 # 抄成不存在的卡号
    assert_nothing_happened(env, dirs)


def test_extra_card_is_rejected(env):
    digest, dirs = setup_three(env)
    sel = plan.selection_sig(digest, ["c01"], {})
    assert run(env, digest, "c01,c03", sel=sel) == 2                 # 多加了 c03
    assert_nothing_happened(env, dirs)


def test_changed_answer_is_rejected_and_delete_answer_counts_as_card(env):
    digest, dirs = setup_three(env)
    sel = plan.selection_sig(digest, ["c01", "c02"], {"c02": "删"})  # 看板：清理 c01、c02(删)
    assert run(env, digest, "c01", ["c02=留"], sel=sel) == 2          # 改回答
    assert run(env, digest, "c01", sel=sel) == 2                      # 漏掉回答
    assert run(env, digest, "c01", ["c02=删", "c03=留"], sel=sel) == 2   # 多一条回答
    assert_nothing_happened(env, dirs)
    assert run(env, digest, "c01", ["c02=删"], sel=sel, dry=True) == 0   # c02 只出现在答复里也算选中
    assert_nothing_happened(env, dirs)
    assert run(env, digest, "c01", ["c02=删"], sel=sel) == 0
    assert not dirs["c01"].exists() and not dirs["c02"].exists() and dirs["c03"].exists()


def test_sel_bound_to_seal_digest(env):
    digest, dirs = setup_three(env)
    assert run(env, digest, "c01", sel=plan.selection_sig("ffffffffffff", ["c01"], {})) == 2
    assert_nothing_happened(env, dirs)


def test_sel_is_optional_but_noted(env, capsys):
    digest, dirs = setup_three(env)
    assert run(env, digest, "c01", dry=True) == 0
    assert "未提供 --sel" in capsys.readouterr().err


# ------------------------------------------------------------ JS / Python 一致性
def extract_js():
    html = DASHBOARD.read_text(encoding="utf-8")
    m = re.search(r"/\*SEL-BEGIN\*/(.*?)/\*SEL-END\*/", html, re.S)
    assert m, "dashboard.html 里找不到 SEL-BEGIN/SEL-END 标记"
    return m.group(1)


def test_fixed_vectors_python():
    for seal, cards, answers, want in FIXED:
        got = plan.selection_sig(seal, cards, answers)
        if want:
            assert got == want
        assert re.fullmatch(r"[0-9a-f]{12}", got)


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，改由 test_fixed_vectors_python 的硬编码期望值对照")
def test_dashboard_js_matches_python(tmp_path):
    js = extract_js()
    cases = [(s, c, a) for s, c, a, _ in FIXED] + [
        ("abc123def456", ["c10", "c02", "c09"], ["c09=删", "c02=留", "c10=删"]),
        ("abc123def456", ["c01"], ['c01=带"引号\\反斜杠', "c02=留中"]),
    ]
    prog = js + "\n(async()=>{const cases=" + json.dumps(cases, ensure_ascii=False) + ";\n" \
        "console.log(JSON.stringify(await Promise.all(cases.map(([s,c,a])=>selSig(s,c,a)))));})();"
    f = tmp_path / "sel.js"
    f.write_text(prog, encoding="utf-8")
    out = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30, check=True).stdout
    js_vals = json.loads(out)
    py_vals = [plan.selection_sig(s, c, a) for s, c, a in cases]
    assert js_vals == py_vals
    assert js_vals[0] == FIXED[0][3]
