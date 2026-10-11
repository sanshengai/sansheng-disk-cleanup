"""行为评测：evals.json 里每条“情境 + 期望行为”按 check 自动造夹具、跑 plan.py / act.py 并断言。

夹具全部造在 tmp_path 下（HOME、protect.HOME、profile、runs 都指向临时目录，沿用 tests/test_plan_act.py 的 Env），
不触碰真实家目录。绝对路径（如 /Library/Caches）只作为 plan.json 里的字符串交给保护判定，从不创建、不删除。

check 描述（对象或对象列表）：
  type=plan_check：fixture 造 facts+plan，跑 plan.py check，对第一张卡（或 expect.card）断言字段。
  type=act_run：fixture 同上，封存后按 preflight 依次跑 act.py，再跑最后一次 run，断言退出码与文件状态。
fixture：
  files   相对 ~ 的文件（"~/a/b.bin"），造出并把 mtime 调到 10 天前
  nodes   事实节点（path 用 ~/ 或绝对路径；其余字段并入 node()）
  cards   计划卡；items 里 {"codex_session":{"tid":"A","class":"interactive"}} 会造假会话文件并展开
          end_evidence=false 去掉结束证据；evidence 为字符串/对象列表
act_run 的 running：模拟正在运行的进程名列表（替换 act.process_running，不依赖本机真有该进程；默认空）。
  expect 另可有 item_reason_contains / item_blocked_by（对第一个条目）、files_intact（文件一个没少，不要求没有回执）。
known_gap：期望行为与当前代码有差异时的说明（目前没有未补的缺口）；check 断言当前真实行为，补上闸门后改成期望值。
"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from test_plan_act import (END_EVIDENCE, TID, TID2, Env, act, age, card, codex_item,  # noqa: E402
                           mkfile, node, plan, rollout)

pytestmark = pytest.mark.filterwarnings("ignore")

EVALS = json.loads((HERE / "evals.json").read_text(encoding="utf-8"))
TIDS = {"A": TID, "B": TID2}


def test_evals_file_is_wellformed():
    ids = [e["id"] for e in EVALS]
    assert 8 <= len(ids) <= 12 and len(set(ids)) == len(ids)
    for e in EVALS:
        assert {"id", "situation", "expected_behavior", "check"} <= set(e)
        assert all(isinstance(e[k], str) and e[k].strip() for k in ("situation", "expected_behavior"))


def _p(env, s):
    return str(env.home / s[2:]) if s.startswith("~/") else s


def _build(env, fx):
    for f in fx.get("files", []):
        mkfile(env.home / f[2:])
    age(env.home)
    nodes = []
    for i, n in enumerate(fx.get("nodes", []), 1):
        n = dict(n)
        path = _p(env, n.pop("path"))
        nodes.append(node(path, id=f"n{i}", **n))
    cards = []
    for c in fx.get("cards", []):
        items = []
        for it in c["items"]:
            if "codex_session" in it:
                cs = it["codex_session"]
                tid = TIDS[cs["tid"]]
                ci = codex_item(rollout(env, tid), tid)
                ci["session"] = tid
                if cs.get("class"):
                    ci["class"] = cs["class"]
                items.append(ci)
            else:
                it = dict(it)
                it["path"] = _p(env, it["path"])
                items.append(it)
        built = card(c["id"], items, verdict=c.get("verdict", "clean"), evidence=c.get("evidence"),
                     question=c.get("question"))
        if "default_select" in c:
            built["default_select"] = c["default_select"]
        if c.get("end_evidence", True) is False:
            built["evidence"].remove(END_EVIDENCE)
        cards.append(built)
    age(env.home)
    env.write(nodes, cards)


def _contains(haystack, needles, what):
    for n in needles:
        assert any(n in h for h in haystack), f"{what} 里应含「{n}」，实际：{haystack}"


def _plan_check(env, ck):
    _build(env, ck["fixture"])
    res = env.check()
    want = ck["expect"]
    c = next(x for x in res["cards"] if x["id"] == want.get("card", "c01"))
    for k in ("verdict_out", "downgraded", "default_select"):
        if k in want:
            assert c[k] == want[k], f"{k}: 期望 {want[k]!r}，实际 {c[k]!r}（errors={c['errors']}）"
    _contains(c["errors"], want.get("errors_contain", []), "errors")
    _contains(c.get("warnings", []), want.get("warnings_contain", []), "warnings")
    if "question_contains" in want:
        assert want["question_contains"] in c.get("question", "")
    if "evidence_checks_ok" in want:
        assert [x["ok"] for x in c.get("evidence_checks", [])] == want["evidence_checks_ok"]
    if "sealable" in want:
        assert (plan.main(["seal", "--run", str(env.run)]) == 0
                and c["id"] in json.loads((env.run / "seal.json").read_text())["cards"]) == want["sealable"]


FLAGS = {"skip_pilot": ["--skip-pilot", "--reason", "evals"], "pilot": ["--pilot"], "dry_run": ["--dry-run"]}


def _act_run(env, ck, capsys):
    _build(env, ck["fixture"])
    digest = env.seal()
    cards = ck["cards"]
    good = plan.selection_sig(digest, cards.split(","), {})
    base = ["run", "--run", str(env.run), "--seal", digest, "--cards", cards]
    env.mp.setattr(act, "open_files", lambda p: ck.get("open_files", {}).get(p.replace(str(env.home), "~"), []))
    running = set(ck.get("running", []))
    env.mp.setattr(act, "process_running", lambda n: n in running)
    for step in ck.get("preflight", []):
        assert act.main(base + ["--sel", good] + FLAGS[step]) == 0, step
    before = {p for p in env.home.rglob("*") if p.is_file() and ".sansheng" not in p.parts}
    sel = {"ok": good, "wrong": plan.selection_sig(digest, ["c99"], {}), "absent": None}[ck.get("sel", "ok")]
    argv = base + (["--sel", sel] if sel else []) + [x for f in ck.get("final_flags", []) for x in FLAGS[f]]
    capsys.readouterr()
    code = act.main(argv)
    err = capsys.readouterr().err
    want = ck["expect"]
    assert code == want["exit_code"], f"退出码 {code}，stderr={err[:300]}"
    _contains([err], want.get("stderr_contains", []), "stderr")
    after = {p for p in env.home.rglob("*") if p.is_file() and ".sansheng" not in p.parts}
    if want.get("nothing_deleted"):
        assert before == after, f"被删：{sorted(before - after)}"
        assert not (env.run / "receipt.json").exists()
    if want.get("files_intact"):
        assert before == after, f"被删：{sorted(before - after)}"
    if {"item_status", "item_reason_contains", "item_blocked_by"} & set(want):
        rc_path = next(iter(sorted(env.run.glob("receipt-dryrun-*.json"))[-1:]), None) if "--dry-run" in argv \
            else env.run / "receipt.json"
        it = json.loads(rc_path.read_text())["cards"][0]["items"][0]
        if "item_status" in want:
            assert it["status"] == want["item_status"], it["status"]
        if "item_reason_contains" in want:
            assert want["item_reason_contains"] in it["reason"], it["reason"]
        if "item_blocked_by" in want:
            assert it.get("blocked_by") == want["item_blocked_by"], it


@pytest.mark.parametrize("ev", EVALS, ids=[e["id"] for e in EVALS])
def test_eval(ev, tmp_path, monkeypatch, capsys):
    cks = ev["check"] if isinstance(ev["check"], list) else [ev["check"]]
    for ck in cks:
        env = _fresh_env(tmp_path, monkeypatch)
        {"plan_check": lambda: _plan_check(env, ck), "act_run": lambda: _act_run(env, ck, capsys)}[ck["type"]]()


_n = [0]


def _fresh_env(tmp_path, monkeypatch):
    _n[0] += 1
    d = tmp_path / f"case{_n[0]}"
    d.mkdir()
    return Env(d, monkeypatch)
