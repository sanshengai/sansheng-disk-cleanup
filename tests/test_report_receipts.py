"""0.5 成果页汇总同一 seal 下全部正式回执：中断后重跑、分几次执行都会有多份，只看最后一份会少算；预演不计，同一路径不重复计。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, age, auto_sel, card, delete_item, mkfile, node, plan  # noqa: E402
import report  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def setup(env):
    dirs = {}
    nodes, cards = [], []
    for i, size in enumerate((3_000_000, 5_000_000, 7_000_000), 1):
        d = env.work / f"d{i}"
        mkfile(d / "a.bin", b"x" * size)
        age(d)
        dirs[f"c0{i}"] = d
        nodes.append(node(d, id=f"n{i}"))
        cards.append(card(f"c0{i}", [delete_item(d)]))
    env.write(nodes, cards)
    return env.seal(), dirs


def removed_bytes(rc):
    return sum(it["removed"]["bytes"] for c in rc["cards"] for it in c["items"] if it["status"] in ("removed", "partial"))


def stamped(env, prefix):
    return sorted(p for p in env.run.glob(f"receipt-{prefix}*.json"))


def test_report_sums_all_formal_receipts_of_same_seal(env):
    digest, dirs = setup(env)
    assert env.act("c01") == 0                                   # 第一次：只清 c01（预演 + 正式）
    first = env.receipt()
    assert not dirs["c01"].exists()
    # 第二次（例如第一次被打断后重跑，选了别的卡）：预演 → 试点 → 正式
    base = ["run", "--run", str(env.run), "--seal", digest, "--cards", "c02,c03"]
    assert act.main(auto_sel(base + ["--dry-run"])) == 0
    assert act.main(auto_sel(base + ["--pilot"])) == 0
    assert act.main(auto_sel(base)) == 0
    last = env.receipt()
    assert last["sel"] != first["sel"] and not dirs["c02"].exists() and not dirs["c03"].exists()
    pilot = json.loads(Path(stamped(env, "pilot-")[-1]).read_text())
    want = removed_bytes(first) + removed_bytes(pilot) + removed_bytes(last)
    assert removed_bytes(first) > 0 and removed_bytes(pilot) > 0
    assert report.build_report(str(env.run))["freed"] == want
    # 旧实现只算“最后一份正式回执 + 它闸门里的试点”：少了第一次那份
    assert want > removed_bytes(pilot) + removed_bytes(last)


def test_dryrun_and_duplicate_receipts_not_counted(env):
    digest, dirs = setup(env)
    assert env.act("c01,c02") == 0
    want = removed_bytes(env.receipt())
    assert want > 0
    # 预演回执：即便里面有“删除字节”也不计
    dry = sorted(env.run.glob("receipt-dryrun-*.json"))[-1]
    rc = json.loads(dry.read_text())
    rc["cards"][0]["items"][0]["status"] = "removed"
    rc["cards"][0]["items"][0]["removed"]["bytes"] = 10 ** 9
    dry.write_text(json.dumps(rc))
    # 同一份正式回执的另一个文件名（内容一致）：只算一次
    stamped_full = [p for p in env.run.glob("receipt-2*.json")]
    assert stamped_full
    (env.run / "receipt-29990101T000000Z.json").write_text(stamped_full[0].read_text())
    assert report.build_report(str(env.run))["freed"] == want


def test_same_path_removed_in_two_receipts_counted_once(env):
    digest, dirs = setup(env)
    assert env.act("c01") == 0
    want = removed_bytes(env.receipt())
    twin = json.loads((env.run / "receipt.json").read_text())      # 同一路径又记了一次“已删除”（如重复落盘的回执）
    twin["started_at"], twin["finished_at"] = "2099-01-01T00:00:00Z", "2099-01-01T00:00:05Z"
    (env.run / "receipt-29990101T000000Z.json").write_text(json.dumps(twin))
    rp = report.build_report(str(env.run))
    assert rp["freed"] == want
    assert rp["cards"][0]["bytes"] == want


def test_other_seal_receipts_ignored(env):
    digest, dirs = setup(env)
    assert env.act("c01") == 0
    want = removed_bytes(env.receipt())
    other = json.loads((env.run / "receipt.json").read_text())
    other["seal"], other["started_at"] = "ffffffffffff", "2098-01-01T00:00:00Z"
    other["cards"][0]["items"][0]["removed"]["bytes"] = 10 ** 10
    (env.run / "receipt-29980101T000000Z.json").write_text(json.dumps(other))
    assert report.build_report(str(env.run))["freed"] == want
