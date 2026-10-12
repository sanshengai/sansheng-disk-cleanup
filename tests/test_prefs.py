"""越用越懂你：初次设置、本机规则、三次同向才问一次、会话保留天数。夹具全在 tmp_path 内。"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, age, card, delete_item, mkfile, node, plan  # noqa: E402
import prefs  # noqa: E402
import protect  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def profile(env):
    p = env.home / ".sansheng" / "disk-cleanup" / "profile.json"
    return json.loads(p.read_text()) if p.exists() else {}


def setup(env, spec=(("c01", "clean", None), ("c02", "decide", None), ("c03", "decide", "all"))):
    dirs, nodes, cards = {}, [], []
    for i, (cid, verdict, preset) in enumerate(spec, 1):
        d = env.work / f"d{i}"
        mkfile(d / "a.bin")
        age(d)
        dirs[cid] = d
        nodes.append(node(d, id=f"n{i}"))
        c = card(cid, [delete_item(d)], verdict=verdict, question="删吗？" if verdict == "decide" else None)
        c["cards_used"] = [f"tool{i}"]
        if preset:
            c["preset"] = preset
        cards.append(c)
    env.write(nodes, cards)
    return env.seal(), dirs


def propose(env):
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    return json.loads((env.run / "proposal.json").read_text())


# ---------------------------------------------------------------- 初次设置

def test_apply_settings_writes_profile(env):
    (env.home / "Movies").mkdir()
    (env.home / "Documents" / "成品").mkdir()
    text = "叁笙清理选择\n设置: 保留天数=7; 别碰=~/Movies; 成品=~/Documents/成品; 默认=safe\n别再列: other/tool2/recommended\n备注: 无"
    done, errs = prefs.apply_settings(text)
    assert not errs and done
    p = profile(env)
    assert p["retention_days"] == 7 and "~/Movies" in p["protect"] and "~/Documents/成品" in p["deliverable_roots"]
    assert p["default_choice"] == "safe" and p["rules"]["other/tool2/recommended"]["action"] == "keep" and p["onboarded_at"]
    assert "~/Documents/成品" in p["reference_roots"]      # 成品目录同时作为引用检查目录


@pytest.mark.parametrize("bad", ["别碰=/etc", "别碰=~", "别碰=~/不存在", "成品=~/../x", "保留天数=9", "默认=hard"])
def test_apply_settings_rejects_bad_values(env, bad):
    done, errs = prefs.apply_settings(f"设置: {bad}")
    assert errs
    p = profile(env)
    assert "/etc" not in json.dumps(p) and p.get("retention_days") is None and p.get("default_choice") is None
    assert not p.get("protect") and not p.get("deliverable_roots")


def test_profile_cli_reads_board_text(env, monkeypatch, capsys):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("设置: 保留天数=15; 别碰=无; 成品=无; 默认=recommended\n"))
    assert plan.main(["profile", "--apply", "-"]) == 0
    assert profile(env)["retention_days"] == 15


# ---------------------------------------------------------------- 规则进提案

def test_first_time_then_chat(env):
    setup(env)
    assert propose(env)["mode"] == "board"
    prefs.apply_settings("设置: 保留天数=0; 别碰=无; 成品=无; 默认=recommended")
    assert propose(env)["mode"] == "chat"


def test_rules_change_options(env):
    setup(env)
    prefs.apply_settings("设置: 默认=recommended")
    prefs.answer("other/tool3/all", "delete")     # 用户认可：这一类不用再问
    prefs.answer("other/tool2/recommended", "keep")     # 这一类一律留着
    p = propose(env)
    o = p["options"]
    assert "c03" not in o["safe"]["cards"] and "c03" in o["all"]["cards"]   # 规则不把卡塞进比它自己低的档
    assert all("c02" not in v["cards"] for v in o.values())
    assert p["attention"] == [] and "按你的规则留着" in p["message"]


def test_default_choice_drives_good(env):
    setup(env)
    prefs.apply_settings("设置: 默认=safe")
    p = propose(env)
    assert p["default"] == "safe" and "按稳妥删" in p["message"]


# ---------------------------------------------------------------- 飞轮

def run_once(env, choice):
    propose(env)
    assert act.main(["go", "--run", str(env.run), "--choice", choice]) == 0


def fresh_scan(env, n, deleted_keys=()):
    """模拟下一次扫描：新 scan_id、同样的三张卡。"""
    sid = f"scan{n}"
    for f in env.run.glob("*.json"):
        f.unlink()
    nodes, cards = [], []
    for i, (cid, verdict, preset) in enumerate((("c01", "clean", None), ("c02", "decide", None), ("c03", "decide", "all")), 1):
        d = env.work / f"d{i}"
        mkfile(d / "a.bin")
        age(d)
        nodes.append(node(d, id=f"n{i}"))
        c = card(cid, [delete_item(d)], verdict=verdict, question="删吗？" if verdict == "decide" else None)
        c["cards_used"] = [f"tool{i}"]
        if preset:
            c["preset"] = preset
        cards.append(c)
    env.write(nodes, cards, scan_id=sid)
    env.seal()


def test_three_same_direction_runs_ask_once(env):
    prefs.apply_settings("设置: 默认=recommended")
    for n in (1, 2):
        fresh_scan(env, n)
        run_once(env, "recommended")
        assert propose(env)["questions"] == [] or n == 3
    fresh_scan(env, 3)
    run_once(env, "recommended")
    fresh_scan(env, 4)
    q = {x["key"]: x["action"] for x in propose(env)["questions"]}
    assert q.get("other/tool2/recommended") == "delete"      # 三次都删了推荐档里的 c02
    assert q.get("other/tool3/all") == "keep"                # 三次都没删慎重档的 c03
    prefs.answer("other/tool3/all", "never")                 # “不用”：冷却期内不再问
    q = {x["key"] for x in propose(env)["questions"]}
    assert "other/tool3/all" not in q and "other/tool2/recommended" in q
    prefs.answer("other/tool2/recommended", "delete")
    prefs.answer("other/tool1/safe", "once")                 # 只这次：不记规则，下次还会问
    assert [x["key"] for x in propose(env)["questions"]] == ["other/tool1/safe"]
    prefs.answer("other/tool1/safe", "delete")
    assert propose(env)["questions"] == []


def test_two_runs_or_mixed_do_not_ask(env):
    prefs.apply_settings("设置: 默认=recommended")
    for n, choice in ((1, "recommended"), (2, "all"), (3, "recommended")):
        fresh_scan(env, n)
        run_once(env, choice)
    fresh_scan(env, 4)
    keys = {x["key"] for x in propose(env)["questions"]}
    assert "other/tool3/all" not in keys                     # 删过一次又没删：不同向，不问


def test_cooldown_expires(env, monkeypatch):
    prefs.answer("tool9/x", "never")
    assert prefs._cooling(prefs.load(), "tool9/x")
    assert not prefs._cooling(prefs.load(), "tool9/x", now=time.time() + 61 * 86400)


# ---------------------------------------------------------------- 会话保留天数

def test_retention_keeps_recent_interactive_session(env):
    sess = env.home / ".claude" / "projects" / "p" / "s1.jsonl"
    mkfile(sess)
    age(sess.parent)
    n = node(sess, id="n1")
    n["newest_mtime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 86400))
    c = card("c01", [delete_item(sess, **{"class": "interactive", "session": "s1"})], verdict="decide", question="删吗？")
    env.write([n], [c])
    r = env.check()
    assert next(x for x in r["cards"] if x["id"] == "c01")["verdict_out"] == "decide"
    prefs.apply_settings("设置: 保留天数=7")
    r = env.check()
    out = next(x for x in r["cards"] if x["id"] == "c01")
    assert out["verdict_out"] == "keep" and any("7 天" in e for e in out.get("errors", []))
    prefs.apply_settings("设置: 保留天数=0")
    assert next(x for x in env.check()["cards"] if x["id"] == "c01")["verdict_out"] == "decide"


# ---------------------------------------------------------------- 0.4 发布前评审回归

def test_attention_cards_listed_with_question_in_chat(env):
    """评审 2：对话模式下，回“好”会删的待定卡必须逐条列出标题、体积和问题。"""
    setup(env)
    prefs.apply_settings("设置: 默认=recommended")
    p = propose(env)
    assert p["mode"] == "chat" and p["attention"] == ["c02", "c03"]
    assert "卡片 c02" in p["message"] and "回“好”会删" in p["message"] and "删吗？" in p["message"]
    assert "回“好”不删" in p["message"]                 # c03 在慎重档，推荐不含它


def test_delete_rule_skips_downgraded_cards(env):
    """评审 3：“以后都删”不适用于降过级的卡（例如 clean 缺结束证据被降为 decide）。"""
    sealed, _ = setup(env, spec=(("c01", "clean", None),))
    seal = json.loads((env.run / "seal.json").read_text())
    seal["cards"]["c01"]["review"] = True
    import plan as planmod
    seal["digest"] = planmod.digest_of(seal)
    (env.run / "seal.json").write_text(json.dumps(seal), encoding="utf-8")
    prefs.apply_settings("设置: 默认=recommended")
    prefs.answer(seal["cards"]["c01"]["rule_key"], "delete")
    p = propose(env)
    assert "按你定的规则" not in p["message"]


def test_plan_edited_after_seal_does_not_move_tiers(env):
    """评审 4：封存后把 plan.json 的档位改成 safe、体积改小，提案仍按封存的档位与实测体积。"""
    setup(env)
    prefs.apply_settings("设置: 默认=recommended")
    pj = env.run / "plan.json"
    data = json.loads(pj.read_text())
    for c in data["cards"]:
        if c["id"] == "c03":
            c["preset"], c["bytes"] = "safe", 1
    pj.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    o = propose(env)["options"]
    assert "c03" not in o["safe"]["cards"] and "c03" in o["all"]["cards"]


def test_default_all_still_warns_unrecoverable(env):
    """评审 7：默认档设为“全部可删”时，删了找不回的那几项仍要在文案里点名。"""
    setup(env, spec=(("c01", "clean", None), ("c02", "clean", "all")))
    prefs.apply_settings("设置: 默认=all")
    p = propose(env)
    assert "回“好”也会删掉这些找不回的" in p["message"] and "卡片 c02" in p["message"]


def test_rule_key_ignores_custom_and_needs_card():
    assert prefs.rule_key({"rule_key": "ai-images/codex/safe", "cards_used": ["x"]}, "all", "other") == "other/x/all"
    assert prefs.rule_key({}, "recommended", "other") is None


# ---------------------------------------------------------------- 0.5：设置行改用 JSON，目录名可以含 | 和 ;

def test_settings_line_json_survives_pipe_and_semicolon_in_dir_names(env):
    odd = env.home / "a|b;c"
    odd.mkdir()
    (env.home / "Movies").mkdir()
    line = "设置: " + json.dumps({"保留天数": 15, "别碰": ["~/a|b;c", "~/Movies"], "成品": [], "默认": "safe"}, ensure_ascii=False)
    text = f"叁笙清理选择\n{line}\n别再列: other/tool2/recommended\n备注: 无"
    done, errs = prefs.apply_settings(text)
    assert not errs and done
    p = profile(env)
    assert p["protect"] == ["~/a|b;c", "~/Movies"]                  # 旧格式会被拆成 ~/a、b、c 三段
    assert p["retention_days"] == 15 and p["default_choice"] == "safe" and not p.get("deliverable_roots")
    assert p["rules"]["other/tool2/recommended"]["action"] == "keep"


def test_settings_line_json_rejects_bad_values_like_legacy(env):
    (env.home / "x").mkdir()
    for bad in ('{"别碰": ["/etc"]}', '{"别碰": ["~"]}', '{"别碰": ["~/不存在"]}', '{"保留天数": 9}', '{"保留天数": null}',
                '{"默认": "hard"}', '["not","an","object"]', '{bad json'):
        done, errs = prefs.apply_settings(f"设置: {bad}")
        assert errs, bad
        p = profile(env)
        assert "/etc" not in json.dumps(p) and p.get("retention_days") is None and p.get("default_choice") is None, bad
        assert not p.get("protect"), bad


# ---------------------------------------------------------------- 发布前评审 低9：成品目录拒绝太宽泛的位置

BROAD = ["~/Library", "~/Library/Caches", "~/Downloads", "~/Desktop", "~/Documents", "~/Applications", "~/.cache"]


@pytest.mark.parametrize("path", BROAD)
def test_broad_deliverable_dir_not_saved_via_settings(env, path):
    full = Path(path.replace("~", str(env.home)))
    full.mkdir(parents=True, exist_ok=True)
    done, errs = prefs.apply_settings(f"叁笙清理选择\n设置: 成品={path}\n备注: 无")
    assert any("太宽泛" in e and path in e for e in errs), errs
    p = profile(env)
    assert path not in (p.get("deliverable_roots") or []) and path not in (p.get("reference_roots") or [])


@pytest.mark.parametrize("path", BROAD)
def test_broad_deliverable_dir_not_saved_via_ledger(env, path):
    full = Path(path.replace("~", str(env.home)))
    full.mkdir(parents=True, exist_ok=True)
    proj = env.home / "proj"
    proj.mkdir()
    text = "叁笙清理 · 项目台账\nscan_id: s\n台账: " + json.dumps({"项目": {str(proj): {"状态": "做完了", "成品": [path, "~/Documents/具体成品"]}}}, ensure_ascii=False)
    (env.home / "Documents" / "具体成品").mkdir(parents=True, exist_ok=True)
    done, errs = prefs.apply_settings(text)
    assert any("太宽泛" in e for e in errs), errs
    entry = profile(env)["projects"][os.path.realpath(proj)]
    assert entry["deliverables"] == ["~/Documents/具体成品"]           # 宽泛的不存，具体的照存


def test_plan_profile_cli_reports_not_saved_for_broad_dir(env, tmp_path, capsys):
    f = tmp_path / "x.txt"
    f.write_text("叁笙清理选择\n设置: 成品=~/Downloads\n备注: 无", encoding="utf-8")
    (env.home / "Downloads").mkdir(exist_ok=True)
    assert plan.main(["profile", "--apply", str(f)]) == 2
    assert "没保存：太宽泛" in capsys.readouterr().err


def test_hand_edited_broad_deliverable_not_folded_into_effective_profile(env):
    """手改 profile 塞进宽泛成品目录：有效 profile（折叠台账之后）不把它当成品目录 / 引用检查目录。"""
    (env.home / "Downloads").mkdir(exist_ok=True)
    proj = env.home / "proj"
    proj.mkdir()
    pf = Path(protect.PROFILE)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"projects": {str(proj): {"status": "done", "deliverables": ["~/Downloads", "~/proj/out"]}}}), encoding="utf-8")
    eff = protect.load_profile(protect.PROFILE)
    assert "~/Downloads" not in eff["deliverable_roots"] and "~/Downloads" not in eff["reference_roots"]
    assert "~/proj/out" in eff["deliverable_roots"]


def test_profile_apply_missing_file_is_a_clean_chinese_error(env, tmp_path, capsys):
    assert plan.main(["profile", "--apply", str(tmp_path / "nope.txt")]) == 2
    err = capsys.readouterr().err
    assert "读不了" in err and "nope.txt" in err and "Traceback" not in err


# ---------------------------------------------------------------- 0.6：保留天数扩展、归档 / 待确认规则行

D12 = "0123456789ab"
BOARD = f"叁笙清理选择\nscan_id: s1\nseal: {D12}\n清理: c01, c03\n回答: c02=删 / c05=归档\nsel: {D12}\n"


def test_retention_choices_include_60_and_90(env):
    assert prefs.RETENTION_CHOICES == (0, 7, 15, 30, 60, 90)
    for d in (60, 90):
        done, errs = prefs.apply_settings(f"设置: 保留天数={d}")
        assert not errs and profile(env)["retention_days"] == d
    done, errs = prefs.apply_settings("设置: 保留天数=45")
    assert errs and "60" in errs[0] and profile(env)["retention_days"] == 90


def test_parse_board_export_archive_and_rule_lines():
    ex, errs = prefs.parse_board_export(BOARD + "归档到: /Volumes/备份盘/叁笙归档\n确认规则: p1, p2\n不要规则: p3\n备注: x")
    assert not errs and ex["answers"] == ["c02=删", "c05=归档"] and ex["cards"] == ["c01", "c03"]
    assert ex["archive_to"] == "/Volumes/备份盘/叁笙归档" and ex["confirm_rules"] == ["p1", "p2"] and ex["reject_rules"] == ["p3"]
    ex, errs = prefs.parse_board_export(BOARD)                                  # 三行都可缺
    assert not errs and ex["archive_to"] is None and ex["confirm_rules"] == [] and ex["reject_rules"] == []
    ex, errs = prefs.parse_board_export(BOARD + "确认规则: 无\n不要规则: 无\n归档到: 无")
    assert not errs and ex["confirm_rules"] == [] and ex["archive_to"] is None


@pytest.mark.parametrize("extra, frag", [
    ("归档到: /Volumes/a\n归档到: /Volumes/b\n", "多次"), ("确认规则: p1\n不要规则: p1\n", "既确认又不要"),
    ("确认规则: p 1;x\n", "不合法"), ("确认规则: p1\n确认规则: p2\n", "多次")])
def test_parse_board_export_rejects_bad_new_lines(extra, frag):
    ex, errs = prefs.parse_board_export(BOARD + extra)
    assert ex is None and any(frag in e for e in errs), errs


def test_setup_export_is_not_a_board_export():
    ex, errs = prefs.parse_board_export("叁笙清理 · 初始设置\n设置版本: 1\n答案: {}")
    assert ex is None and any("首行" in e for e in errs)


def test_apply_settings_hands_rule_decisions_to_memory(env, monkeypatch):
    import types
    calls = []
    fake = types.SimpleNamespace(confirm=lambda ids: (calls.append(("confirm", list(ids))) or [f"已确认 {i}" for i in ids], []),
                                 reject=lambda ids: (calls.append(("reject", list(ids))) or [f"已丢掉 {i}" for i in ids], ["p9 不存在"]))
    monkeypatch.setitem(sys.modules, "memory", fake)
    done, errs = prefs.apply_settings(BOARD + "设置: 默认=safe\n确认规则: p1,p2\n不要规则: p3\n")
    assert calls == [("confirm", ["p1", "p2"]), ("reject", ["p3"])]
    assert "已确认 p1" in done and "已丢掉 p3" in done and errs == ["p9 不存在"]
    assert profile(env)["default_choice"] == "safe"                          # 同一份文本里的设置照存，且先于规则存好
    calls.clear()
    done, errs = prefs.apply_settings("确认规则: p1\n")                            # 只有规则行也算有内容
    assert calls == [("confirm", ["p1"])] and not errs


def test_apply_settings_survives_missing_or_broken_memory_module(env, monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "memory", None)                        # import memory → ImportError
    done, errs = prefs.apply_settings("设置: 默认=all\n确认规则: p1\n")
    assert profile(env)["default_choice"] == "all" and any("没保存" in e and "memory" in e for e in errs)
    boom = types.SimpleNamespace(confirm=lambda ids: 1 / 0, reject=lambda ids: ([], []))
    monkeypatch.setitem(sys.modules, "memory", boom)
    done, errs = prefs.apply_settings("确认规则: p1\n")
    assert not done and any("没保存" in e for e in errs)
    monkeypatch.setitem(sys.modules, "memory", types.SimpleNamespace())      # 模块在但没有函数
    done, errs = prefs.apply_settings("不要规则: p1\n")
    assert any("没保存" in e for e in errs)


def test_apply_settings_still_errors_when_nothing_to_apply(env):
    done, errs = prefs.apply_settings("叁笙清理选择\n备注: x")
    assert not done and "确认规则" in errs[0]
