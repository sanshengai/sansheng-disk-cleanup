"""act.py go --from-board：看板清单一条命令走完 校验 → 保存设置/台账 → 必要时重新封存 → 预演 → 试点 → 正式执行 → 成果页。

夹具全在 tmp_path 内（沿用 test_plan_act 的 Env）。关键断言：
- 台账导出（抬头“叁笙清理 · 项目台账”）被拒，且报的是“这是项目台账”，不是泛泛的格式错误（变异：去掉专门的拦截，消息就变）；
- 设置/台账改变了判断结果（封存 digest 变了）时退出码 3、什么都不删、打印重新生成看板的命令（变异：去掉重新封存后的 digest 比较）。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, age, card, delete_item, mkfile, node, plan  # noqa: E402
import prefs  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def profile(env):
    p = env.home / ".sansheng" / "disk-cleanup" / "profile.json"
    return json.loads(p.read_text()) if p.exists() else {}


def setup(env, spec=(("c01", "clean"), ("c02", "decide"))):
    dirs, nodes, cards = {}, [], []
    for i, (cid, verdict) in enumerate(spec, 1):
        d = env.work / f"d{i}"
        mkfile(d / "a.bin")
        age(d)
        dirs[cid] = d
        nodes.append(node(d, id=f"n{i}"))
        cards.append(card(cid, [delete_item(d)], verdict=verdict, question="删吗？" if verdict == "decide" else None))
    env.write(nodes, cards)
    return env.seal(), dirs


def export_text(digest, cards, answers=(), extra=(), scan_id="scan1", title="叁笙清理选择", sel=None):
    """按看板“复制清单”的格式拼导出文本；sel 缺省时按 cards + 答复「删」的卡现算。"""
    ids = sorted(set(cards) | {a.split("=")[0] for a in answers if a.endswith("=删")})
    sel = sel or plan.selection_sig(digest, ids, list(answers))
    return "\n".join([title, f"scan_id: {scan_id}", f"seal: {digest}",
                      "清理: " + (", ".join(cards) or "无"), "回答: " + (" / ".join(answers) or "无"),
                      f"sel: {sel}", *extra, "备注: 无"])


def write(env, text):
    f = env.run.parent / "board.txt"
    f.write_text(text, encoding="utf-8")
    return str(f)


def go(env, text):
    return act.main(["go", "--run", str(env.run), "--from-board", write(env, text)])


def no_receipts(env):
    return not list(env.run.glob("receipt*.json"))


# ---------------------------------------------------------------- 正常路径

def test_from_board_runs_three_steps_and_report(env, capsys):
    digest, dirs = setup(env)
    assert go(env, export_text(digest, ["c01", "c02"], ["c02=删"])) == 0
    out = capsys.readouterr().out
    assert not dirs["c01"].exists() and not dirs["c02"].exists()
    assert list(env.run.glob("receipt-dryrun-*.json")) and list(env.run.glob("receipt-pilot-*.json"))
    assert (env.run / "receipt.json").exists() and (env.run / "report.html").exists()
    assert "【go】预演开始" in out and "【go】试点开始" in out and "【go】正式执行开始" in out
    assert "【go】完成" in out and "成果页" in out


def test_from_board_only_selected_cards_are_deleted(env):
    digest, dirs = setup(env)
    assert go(env, export_text(digest, ["c01"])) == 0           # c02 没勾：不动
    assert not dirs["c01"].exists() and (dirs["c02"] / "a.bin").exists()


def test_from_board_and_choice_are_exclusive(env, capsys):
    digest, dirs = setup(env)
    f = write(env, export_text(digest, ["c01"]))
    assert act.main(["go", "--run", str(env.run), "--from-board", f, "--choice", "safe"]) == 2
    assert act.main(["go", "--run", str(env.run)]) == 2
    assert all((d / "a.bin").exists() for d in dirs.values()) and no_receipts(env)


# ---------------------------------------------------------------- 拒绝

def test_ledger_export_is_rejected_and_nothing_changes(env, capsys):
    """台账导出即使混进了合法的清理行，也只能保存设置、不能当清理授权：被拒、什么都不删、台账也没被它保存。"""
    digest, dirs = setup(env)
    (env.home / "proj").mkdir()
    ledger = '台账: {"项目":{"%s":{"状态":"做完了"}}}' % (env.home / "proj")
    text = export_text(digest, ["c01", "c02"], ["c02=删"], extra=[ledger], title="叁笙清理 · 项目台账")
    assert go(env, text) == 2
    err = capsys.readouterr().err
    assert "项目台账" in err and "profile --apply" in err        # 专门的拦截，不是泛泛的“格式不对”
    assert all((d / "a.bin").exists() for d in dirs.values()) and no_receipts(env)
    assert not profile(env).get("projects")


def test_scan_id_mismatch_is_rejected(env, capsys):
    digest, dirs = setup(env)
    assert go(env, export_text(digest, ["c01"], scan_id="other-scan")) == 2
    assert "scan_id" in capsys.readouterr().err
    assert all((d / "a.bin").exists() for d in dirs.values()) and no_receipts(env)


def test_seal_mismatch_is_rejected(env):
    digest, dirs = setup(env)
    assert go(env, export_text("0" * 12, ["c01"])) == 2
    assert all((d / "a.bin").exists() for d in dirs.values()) and no_receipts(env)


def test_tampered_selection_is_rejected_before_saving_settings(env, capsys):
    """清单里的卡号被多加了一张、sel 没变：对不上，闸门拒绝，一张都不删，清单里的设置也不保存。"""
    digest, dirs = setup(env)
    good = export_text(digest, ["c01"], extra=['设置: {"保留天数":7}'])
    bad = good.replace("清理: c01", "清理: c01, c02")
    assert go(env, bad) == 2
    assert "sel" in capsys.readouterr().err
    assert all((d / "a.bin").exists() for d in dirs.values()) and no_receipts(env)
    assert profile(env).get("retention_days") is None
    tampered_answer = export_text(digest, ["c01"], ["c02=删"]).replace("c02=删", "c02=删 / c01=删")
    assert go(env, tampered_answer) == 2
    assert all((d / "a.bin").exists() for d in dirs.values()) and no_receipts(env)


def test_tampered_sel_value_is_rejected(env):
    digest, dirs = setup(env)
    assert go(env, export_text(digest, ["c01"], sel="a" * 12)) == 2
    assert all((d / "a.bin").exists() for d in dirs.values()) and no_receipts(env)


def test_empty_selection_and_bad_files(env, capsys):
    digest, dirs = setup(env)
    assert go(env, export_text(digest, [])) == 2
    assert act.main(["go", "--run", str(env.run), "--from-board", str(env.run.parent / "no-such.txt")]) == 2
    assert go(env, "随便一段话") == 2
    assert go(env, export_text(digest, ["c01"], extra=["设置: {坏的"])) == 2       # 设置行不合法：什么都不做
    assert all((d / "a.bin").exists() for d in dirs.values()) and no_receipts(env)


def test_gates_still_apply_after_board(env):
    """闸门不弱化：试点那一条没删成功就停（退出码 3），正式执行不开始。"""
    digest, dirs = setup(env)
    env.mp.setattr(act, "pilot_order", lambda seal, selection: [])
    assert go(env, export_text(digest, ["c01", "c02"], ["c02=删"])) == 3
    assert all((d / "a.bin").exists() for d in dirs.values()) and not (env.run / "receipt.json").exists()


# ---------------------------------------------------------------- 设置 / 台账行

def interactive_setup(env):
    """c01 = 3 天前用过的交互会话（decide），c02 = 普通 clean 卡。保留天数设为 7 后，c01 整卡降为 keep。"""
    sess = env.home / ".claude" / "projects" / "p" / "s1.jsonl"
    mkfile(sess)
    age(sess.parent)
    n1 = node(sess, id="n1")
    n1["newest_mtime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 86400))
    d2 = env.work / "d2"
    mkfile(d2 / "a.bin")
    age(d2)
    c1 = card("c01", [delete_item(sess, **{"class": "interactive", "session": "s1"})], verdict="decide", question="删吗？")
    env.write([n1, node(d2, id="n2")], [c1, card("c02", [delete_item(d2)])])
    return env.seal(), sess, d2


def test_settings_change_judgement_stops_with_exit_3(env, capsys):
    digest, sess, d2 = interactive_setup(env)
    assert "c01" in json.loads((env.run / "seal.json").read_text())["cards"]
    text = export_text(digest, ["c01", "c02"], ["c01=删"], extra=['设置: {"保留天数":7,"别碰":[],"成品":[],"默认":"recommended"}'])
    assert go(env, text) == 3
    io = capsys.readouterr()
    assert "判断结果" in io.err and "重新确认" in io.err and "plan.py board" in io.err
    assert profile(env)["retention_days"] == 7                            # 设置已保存
    assert sess.exists() and (d2 / "a.bin").exists() and no_receipts(env)  # 什么都没删
    new = json.loads((env.run / "seal.json").read_text())
    assert new["digest"] != digest and "c01" not in new["cards"]           # 已按新设置重新封存
    assert go(env, text) == 2                                              # 旧清单对不上新封存：仍然不能执行


def test_settings_not_affecting_cards_reseals_and_continues(env, capsys):
    digest, dirs = setup(env)
    (env.home / "elsewhere").mkdir()
    old_guard = json.loads((env.run / "seal.json").read_text())["profile_guard"]["digest"]
    text = export_text(digest, ["c01", "c02"], ["c02=删"], extra=['设置: {"别碰":["~/elsewhere"]}'])
    assert go(env, text) == 0
    out = capsys.readouterr().out
    assert "digest 不变" in out
    assert not dirs["c01"].exists() and not dirs["c02"].exists()
    seal = json.loads((env.run / "seal.json").read_text())
    assert seal["digest"] == digest and seal["profile_guard"]["digest"] != old_guard
    assert "~/elsewhere" in profile(env)["protect"]


def test_unsaved_settings_stop_before_deleting(env, capsys):
    digest, dirs = setup(env)
    assert go(env, export_text(digest, ["c01"], extra=['设置: {"别碰":["~/不存在的目录"]}'])) == 3
    assert "没有全部保存" in capsys.readouterr().err
    assert all((d / "a.bin").exists() for d in dirs.values()) and no_receipts(env)


def test_ledger_line_in_cleanup_list_is_saved_and_reseals(env, capsys):
    """② 看板的清单可以带“台账:”行：保存后项目状态变了就重新封存；这里标「进行中」的项目包住了 c01，卡片变了 → 退出码 3。"""
    digest, dirs = setup(env)
    ledger = '台账: {"项目":{"%s":{"状态":"进行中"}}}' % dirs["c01"]
    assert go(env, export_text(digest, ["c01", "c02"], ["c02=删"], extra=[ledger])) == 3
    assert profile(env)["projects"][str(dirs["c01"])]["status"] == "active"
    assert (dirs["c01"] / "a.bin").exists() and (dirs["c02"] / "a.bin").exists() and no_receipts(env)
    assert "c01" not in json.loads((env.run / "seal.json").read_text())["cards"]


def test_stale_seal_without_settings_lines_is_resealed(env):
    """上一次在重新封存前就中断了：设置已改、封存已过期，清单里没有设置行，也自动重新封存（digest 不变则继续）。"""
    digest, dirs = setup(env)
    (env.home / "elsewhere").mkdir()
    prefs.apply_settings('设置: {"别碰":["~/elsewhere"]}')
    assert go(env, export_text(digest, ["c01"])) == 0
    assert not dirs["c01"].exists() and (dirs["c02"] / "a.bin").exists()


# ---------------------------------------------------------------- 解析

def test_parse_board_export():
    d = "0123456789ab"
    ok, errs = prefs.parse_board_export(f"叁笙清理选择\nscan_id: s1\nseal: {d}\n清理: c01, c03\n回答: c02=删 / c05=留\nsel: {d}\n备注: 清理: c99")
    assert not errs and ok["cards"] == ["c01", "c03"] and ok["answers"] == ["c02=删", "c05=留"] and ok["sel"] == d
    none, _ = prefs.parse_board_export(f"叁笙清理选择\nscan_id: s1\nseal: {d}\n清理: 无\n回答: 无\nsel: {d}")
    assert none["cards"] == [] and none["answers"] == []
    for bad in ("", "叁笙清理 · 项目台账\nscan_id: s1",
                f"叁笙清理选择\nscan_id: s1\nseal: {d}\n清理: c01\nsel: {d}",                                  # 缺回答行
                f"叁笙清理选择\nscan_id: s1\nseal: {d}\n清理: c01\n清理: c02\n回答: 无\nsel: {d}",            # 重复
                f"叁笙清理选择\nscan_id: s1\nseal: XYZ\n清理: c01\n回答: 无\nsel: {d}",                       # seal 格式
                f"叁笙清理选择\nscan_id: s1\nseal: {d}\n清理: c01;rm\n回答: 无\nsel: {d}",                    # 卡号含非法字符
                f"叁笙清理选择\nscan_id: s1\nseal: {d}\n清理: c01\n回答: 删掉\nsel: {d}"):
        assert prefs.parse_board_export(bad)[0] is None, bad


# ---------------------------------------------------------------- 发布前评审 低10：重新封存 digest 变了，要说清是判断变了还是目录内容在变

def test_seal_drift_classifies_changed_vs_content_drift():
    item = {"path": "/a/x", "method": "files", "scope": "all", "ino": 1, "dev": 2, "bytes": 10}
    base = {"c01": {"verdict": "clean", "preset": "safe", "items": [item]}, "c02": {"verdict": "clean", "preset": "safe", "items": [item]}}
    new = {"c01": {"verdict": "clean", "preset": "safe", "items": [dict(item, bytes=99)]},          # 只有体积变
           "c02": {"verdict": "decide", "preset": "safe", "items": [item]}}                         # 判定变
    assert act._seal_drift({"cards": base}, {"cards": new}) == (["c02"], ["c01"])
    assert act._seal_drift({"cards": base}, {"cards": {"c01": base["c01"]}}) == (["c02"], [])     # 卡片消失算判断变了


def test_reseal_explains_content_drift_when_only_bytes_changed(env, capsys):
    """设置变了要重新封存，而卡里的目录内容在封存之后也变了：digest 变，停下，并说清是“目录内容在变化”而不是判断变了。"""
    parent = env.work / "bucket"
    child = parent / "run-2026-10-01"
    mkfile(child / "a.bin")
    age(parent)
    env.write([node(parent, id="n1")], [card("c01", [delete_item(child)])])
    digest = env.seal()
    (env.home / "elsewhere").mkdir()
    mkfile(child / "more.bin", b"y" * 20000)               # 封存之后目录里又多了文件
    age(parent)
    text = export_text(digest, ["c01"], extra=['设置: {"别碰":["~/elsewhere"]}'])
    assert go(env, text) == 3
    err = capsys.readouterr().err
    assert "目录内容在封存之后又变了" in err and "c01" in err and "重新确认" in err
    assert child.exists() and no_receipts(env)
