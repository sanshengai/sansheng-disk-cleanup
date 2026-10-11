"""正式执行闸门：同一 seal + 同一 sel 的预演回执与成功的试点回执都在，才允许正式 run。

夹具全在 tmp_path 内（沿用 test_plan_act 的 Env），被拒绝的用例断言文件原封不动、没有正式回执。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, age, auto_sel, card, delete_item, mkfile, node, plan  # noqa: E402,F401


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def setup(env, n=2):
    """n 张 clean 卡，每张一个目录。返回 (digest, [目录])。"""
    dirs, cards, nodes = [], [], []
    for i in range(1, n + 1):
        d = env.work / f"cache{i}"
        mkfile(d / "a.bin")
        age(d)
        dirs.append(d)
        nodes.append(node(d, id=f"n{i}"))
        cards.append(card(f"c0{i}", [delete_item(d)]))
    env.write(nodes, cards)
    return env.seal(), dirs


def go(env, digest, cards, *flags):
    # 试点与正式执行必须带 --sel（P5.R H4）：没给 --sel 时按本次选择现算一个补上
    argv = ["run", "--run", str(env.run), "--seal", digest, "--cards", cards, *flags]
    if "--dry-run" not in argv:
        argv = auto_sel(argv)
    return act.main(argv)


def full_receipt(env):
    return json.loads((env.run / "receipt.json").read_text())


def untouched(env, dirs):
    assert all((d / "a.bin").exists() for d in dirs)
    assert not (env.run / "receipt.json").exists()


def test_missing_dry_run_is_rejected(env, capsys):
    digest, dirs = setup(env)
    assert go(env, digest, "c01", "--pilot") == 0           # 只有试点，没有预演
    assert not dirs[0].exists()
    mkfile(dirs[0] / "a.bin")
    age(dirs[0])
    capsys.readouterr()
    assert go(env, digest, "c01") == 2
    err = capsys.readouterr().err
    assert "预演" in err and "--dry-run" in err and "scripts/act.py run" in err
    assert (dirs[0] / "a.bin").exists() and not (env.run / "receipt.json").exists()


def test_missing_pilot_is_rejected(env, capsys):
    digest, dirs = setup(env)
    assert go(env, digest, "c01", "--dry-run") == 0
    capsys.readouterr()
    assert go(env, digest, "c01") == 2
    err = capsys.readouterr().err
    assert "试点" in err and "--pilot" in err and "--skip-pilot" in err
    untouched(env, dirs)


def test_failed_pilot_does_not_count(env):
    digest, dirs = setup(env, 1)
    assert go(env, digest, "c01", "--dry-run") == 0
    # 试点前把目录弄成"最近修改"：试点那一条被跳过，什么都没删，不算成功
    mkfile(dirs[0] / "new.bin")
    assert go(env, digest, "c01", "--pilot") == 0
    pilots = list(env.run.glob("receipt-pilot-*.json"))
    assert len(pilots) == 1 and not json.loads(pilots[0].read_text()).get("pilot_done")
    assert go(env, digest, "c01") == 2
    untouched(env, dirs)


def test_pilot_item_failing_does_not_count(env):
    digest, dirs = setup(env, 1)
    assert go(env, digest, "c01", "--dry-run") == 0
    assert go(env, digest, "c01", "--pilot") == 0
    path = next(env.run.glob("receipt-pilot-*.json"))
    rc = json.loads(path.read_text())
    assert rc["pilot_done"] and act.pilot_succeeded(rc)          # 对照：真试点成功
    rc["cards"][0]["items"][0]["status"] = "failed"
    assert not act.pilot_succeeded(rc)
    rc["cards"][0]["items"][0]["status"] = "partial"
    assert not act.pilot_succeeded(rc)
    rc["cards"][0]["items"][0]["status"] = "removed"
    rc["cards"][0]["items"][0]["failed_count"] = 1
    assert not act.pilot_succeeded(rc)


def test_failed_pilot_receipt_on_disk_blocks_full_run(env):
    digest, dirs = setup(env, 1)
    mkfile(dirs[0] / "b.bin")
    age(dirs[0])
    assert go(env, digest, "c01", "--dry-run") == 0
    assert go(env, digest, "c01", "--pilot") == 0
    path = next(env.run.glob("receipt-pilot-*.json"))
    rc = json.loads(path.read_text())
    mkfile(dirs[0] / "a.bin")
    rc["cards"][0]["items"][0]["status"] = "failed"                # 把试点那条改成失败
    path.write_text(json.dumps(rc), encoding="utf-8")
    assert go(env, digest, "c01") == 2
    assert (dirs[0] / "a.bin").exists() and not (env.run / "receipt.json").exists()


def test_different_selection_is_rejected(env):
    digest, dirs = setup(env)
    assert go(env, digest, "c01", "--dry-run") == 0
    assert go(env, digest, "c01", "--pilot") == 0
    mkfile(dirs[0] / "a.bin")
    age(dirs[0])
    assert go(env, digest, "c01,c02") == 2                          # 多选了一张卡：sel 变了
    untouched(env, dirs)
    assert go(env, digest, "c02") == 2                              # 换了一张卡
    untouched(env, dirs)


def test_changed_answers_change_sel(env):
    digest, dirs = setup(env, 1)
    assert go(env, digest, "c01", "--dry-run") == 0
    assert go(env, digest, "c01", "--pilot") == 0
    mkfile(dirs[0] / "a.bin")
    age(dirs[0])
    assert act.main(["run", "--run", str(env.run), "--seal", digest, "--cards", "c01",
                     "--answers", "c09=留"]) == 2                    # 多了一条答复：sel 变了
    untouched(env, dirs)


def test_receipts_of_another_plan_do_not_satisfy(env, tmp_path):
    digest, dirs = setup(env)
    assert go(env, digest, "c01", "--dry-run") == 0
    assert go(env, digest, "c01", "--pilot") == 0
    mkfile(dirs[0] / "a.bin")
    age(dirs[0])
    # 把回执里的 seal 改成别的计划：不能被当成本计划的预演/试点
    for p in env.run.glob("receipt-*.json"):
        rc = json.loads(p.read_text())
        rc["seal"] = "0" * 12
        p.write_text(json.dumps(rc), encoding="utf-8")
    assert go(env, digest, "c01") == 2
    untouched(env, dirs)


def test_receipts_without_sel_do_not_satisfy(env):
    digest, dirs = setup(env, 1)
    assert go(env, digest, "c01", "--dry-run") == 0
    assert go(env, digest, "c01", "--pilot") == 0
    mkfile(dirs[0] / "a.bin")
    age(dirs[0])
    for p in env.run.glob("receipt-*.json"):
        rc = json.loads(p.read_text())
        del rc["sel"]
        p.write_text(json.dumps(rc), encoding="utf-8")
    assert go(env, digest, "c01") == 2
    untouched(env, dirs)


def test_skip_pilot_with_reason_passes_and_is_recorded(env):
    digest, dirs = setup(env, 1)
    assert go(env, digest, "c01", "--dry-run") == 0
    assert go(env, digest, "c01", "--skip-pilot", "--reason", "只有一条缓存，已人工核对") == 0
    assert not dirs[0].exists()
    rc = full_receipt(env)
    assert rc["mode"] == "full" and rc["skip_pilot"] is True and rc["skip_reason"] == "只有一条缓存，已人工核对"
    assert rc["gate"]["skip_pilot"] and rc["gate"]["pilot_receipt"] is None and rc["gate"]["dry_run_receipt"]


def test_skip_pilot_still_requires_dry_run(env, capsys):
    digest, dirs = setup(env, 1)
    assert go(env, digest, "c01", "--skip-pilot", "--reason", "赶时间") == 2
    assert "预演" in capsys.readouterr().err
    untouched(env, dirs)


@pytest.mark.parametrize("flags", [["--skip-pilot"], ["--skip-pilot", "--reason", ""], ["--skip-pilot", "--reason", "  "]])
def test_skip_pilot_without_reason_is_an_error(env, flags, capsys):
    digest, dirs = setup(env, 1)
    assert go(env, digest, "c01", "--dry-run") == 0
    assert go(env, digest, "c01", *flags) == 2
    assert "--reason" in capsys.readouterr().err
    untouched(env, dirs)


def test_reason_without_skip_pilot_and_skip_with_dry_run_are_errors(env):
    digest, dirs = setup(env, 1)
    assert go(env, digest, "c01", "--dry-run", "--reason", "x") == 2
    assert go(env, digest, "c01", "--dry-run", "--skip-pilot", "--reason", "x") == 2
    assert go(env, digest, "c01", "--pilot", "--skip-pilot", "--reason", "x") == 2
    untouched(env, dirs)


def test_full_sequence_passes_and_receipts_carry_mode_seal_sel(env):
    digest, dirs = setup(env)
    assert go(env, digest, "c01,c02", "--dry-run") == 0
    assert go(env, digest, "c01,c02", "--pilot") == 0
    assert not dirs[0].exists() and dirs[1].exists()
    assert go(env, digest, "c01,c02") == 0
    assert not dirs[1].exists()
    sel = plan.selection_sig(digest, ["c01", "c02"], {})
    dry = json.loads(next(env.run.glob("receipt-dryrun-*.json")).read_text())
    pilot = json.loads(next(env.run.glob("receipt-pilot-*.json")).read_text())
    full = full_receipt(env)
    for rc, mode in ((dry, "dry_run"), (pilot, "pilot"), (full, "full")):
        assert rc["mode"] == mode and rc["seal"] == digest and rc["sel"] == sel
    assert full["gate"]["skip_pilot"] is False and full["gate"]["pilot_receipt"] and "skip_reason" not in full
    assert "skip_pilot" not in dry


def test_sel_is_recorded_even_without_sel_argument_and_matches_dashboard_value(env):
    digest, dirs = setup(env)
    assert go(env, digest, "c01", "--dry-run") == 0
    sel = plan.selection_sig(digest, ["c01"], {})
    assert json.loads(next(env.run.glob("receipt-dryrun-*.json")).read_text())["sel"] == sel
    assert go(env, digest, "c01", "--sel", sel, "--dry-run") == 0   # 带 --sel 与不带算出同一个值
    assert (dirs[0] / "a.bin").exists()                              # 预演什么都没删


def test_status_ignores_dry_run_receipts(env, capsys):
    digest, dirs = setup(env, 1)
    assert go(env, digest, "c01", "--dry-run") == 0
    capsys.readouterr()
    assert act.main(["status", "--run", str(env.run)]) == 1
    assert "还没有执行回执" in capsys.readouterr().out
