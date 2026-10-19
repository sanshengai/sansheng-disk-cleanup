"""记录点比较的行为测试；所有文件都在 pytest 的扫描树外临时目录。"""
import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import space_history as history

HOME = "/sample/home"
FIRST = "20260101T000000Z-aaaaaa"
SECOND = "20260102T000000Z-bbbbbb"
NOW = "20260103T000000Z-cccccc"


def facts(scan_id=FIRST, size=100, free=800):
    return {"schema": "facts/1", "scan_id": scan_id,
            "created_at": scan_id[:8][:4] + "-" + scan_id[4:6] + "-" + scan_id[6:8] + "T00:00:00Z",
            "host": {"platform": "darwin", "home": HOME},
            "disk": {"total": 1000, "free": free, "used": 1000-free, "device": 12},
            "threshold": 1, "coverage": {"roots": [HOME, "/Applications"], "max_depth": 10,
            "incomplete": False, "scan_status": "complete", "walked_bytes": size},
            "nodes": [{"path": HOME + "/project", "bytes": size, "bytes_lower_bound": False}], "skipped": []}


def put_run(tmp_path, f):
    run = tmp_path / "runs" / f["scan_id"]
    run.mkdir(parents=True, exist_ok=True)
    (run / "facts.json").write_text(json.dumps(f))
    return run


def test_empty_history_and_first_record(tmp_path):
    assert history.load_points(tmp_path / "runs") == []
    run = put_run(tmp_path, facts())
    result = history.make(run)
    assert result["status"] == "first" and result["baseline"] is None
    saved = tmp_path / "history" / (FIRST + ".json.gz")
    assert saved.exists() and saved.stat().st_mode & 0o777 == 0o600
    with gzip.open(saved, "rt") as f:
        point = json.load(f)
    assert point["nodes"][0]["bytes"] == 100
    assert not any(key in point for key in ("contents", "hashes", "references"))
    # 即使原运行目录被移走，精简记录仍可作基准。
    (run / "facts.json").unlink()
    current = put_run(tmp_path, facts(NOW, 160, 730))
    result = history.make(current)
    assert result["baseline"]["scan_id"] == FIRST
    assert result["known_delta"] == 60 and result["unattributed_delta"] == 10


def test_latest_local_scan_is_rejected_by_real_compare_entry(tmp_path):
    put_run(tmp_path, facts())
    local = facts(SECOND)
    local["coverage"]["roots"] = [HOME + "/images", "/private/tmp/claude-501"]
    put_run(tmp_path, local)
    current = facts(NOW, 160)
    current["coverage"]["roots"].append("/private/tmp/claude-501")
    run = put_run(tmp_path, current)
    assert history.main(["compare", "--run", str(run)]) == 0
    result = json.loads((run / "growth.json").read_text())
    assert result["baseline"]["scan_id"] == FIRST
    assert result["skipped_baselines"][0]["scan_id"] == SECOND
    # 指定错误基准也必须拒绝，不悄悄更换范围。
    assert history.main(["compare", "--run", str(run), "--baseline", SECOND]) == 2


@pytest.mark.parametrize("change", [
    lambda p: p["host"].update(home="/different/home"),
    lambda p: p["host"].update(platform="linux"),
    lambda p: p["disk"].update(device=99),
    lambda p: p["disk"].update(total=2000),
    lambda p: p["scope"].update(max_depth=3),
    lambda p: p["scope"].update(threshold=50),
    lambda p: p["coverage"].update(incomplete=True),
    lambda p: p.update(created_at="2099-01-01T00:00:00Z"),
])
def test_incompatible_or_future_records_are_not_a_baseline(change):
    current = history.compact(facts(NOW))
    old = history.compact(facts())
    change(old)
    assert history.select_baseline(current, [old])[0] is None
    with pytest.raises(ValueError):
        history.select_baseline(current, [old], FIRST)


def test_corrupt_and_foreign_run_identity_ignored(tmp_path):
    run = put_run(tmp_path, facts())
    (run / "facts.json").write_text("{broken")
    foreign = tmp_path / "runs" / "fixture-copy"
    foreign.mkdir()
    (foreign / "facts.json").write_text(json.dumps(facts(SECOND)))
    mismatch = put_run(tmp_path, facts(NOW))
    (mismatch / "facts.json").write_text(json.dumps(facts(SECOND)))
    assert history.load_points(tmp_path / "runs") == []


def test_parent_child_not_added_twice_and_missing_is_not_zero():
    old = facts()
    old["nodes"] += [{"path": HOME + "/project/tests", "bytes": 60},
                     {"path": HOME + "/uncertain", "bytes": 40, "bytes_lower_bound": True}]
    current = facts(NOW, 160, 720)
    current["nodes"] += [{"path": HOME + "/project/tests", "bytes": 110},
                         {"path": HOME + "/uncertain", "bytes": 80},
                         {"path": HOME + "/discovered", "bytes": 500}]
    result = history.compare(history.compact(current), history.compact(old))
    assert result["known_delta"] == 60  # 不把子目录 +50 再加一次。
    assert result["disk_used_delta"] == 80 and result["unattributed_delta"] == 20
    assert {r["path"] for r in result["unmeasured"]} == {HOME + "/uncertain", HOME + "/discovered"}
    history.attach_growth(current, result)
    assert current["nodes"][-1]["growth"]["delta_bytes"] is None


def test_cleanup_uses_actual_receipt_and_never_dry_run(tmp_path):
    f = facts()
    run = put_run(tmp_path, f)
    receipt = {"schema": "receipt/1", "scan_id": FIRST, "mode": "dry_run", "dry_run": True,
               "finished_at": "2026-01-01T01:00:00Z", "summary": {"free_after": 900}}
    assert history.record_cleanup(run, receipt) is None
    assert not (tmp_path / "history").exists()
    receipt.update(mode="full", dry_run=False)
    # 废纸篓可处理了 50 bytes，但磁盘实际少了 20；必须保存真实余量。
    receipt["summary"].update(free_after=780, bytes_trash=50, freed_by_items=0)
    path = history.record_cleanup(run, receipt)
    with gzip.open(path, "rt") as f:
        point = json.load(f)
    assert point["disk"]["free"] == 780 and point["kind"] == "cleanup"
    assert "nodes" not in point
    history.record_cleanup(run, receipt)
    assert len(list((tmp_path / "history").glob("*.gz"))) == 1
    receipt["scan_id"] = SECOND
    with pytest.raises(ValueError):
        history.record_cleanup(run, receipt)


def test_finalize_records_measured_point_only_after_completion(tmp_path):
    run = put_run(tmp_path, facts())
    journal = {"schema": "finalize-trash/1", "scan_id": FIRST, "finished_at": None}
    assert history.record_cleanup(run, journal) is None
    journal.update(finished_at="2026-01-01T01:00:00Z", free_after=900)
    path = history.record_cleanup(run, journal)
    with gzip.open(path, "rt") as f:
        point = json.load(f)
    assert point["disk"]["free"] == 900 and point["kind"] == "cleanup"


def test_report_escapes_notes_paths_and_uses_beijing_time():
    a, b = history.compact(facts()), history.compact(facts(NOW, 200))
    evil = '<script>alert("x")</script>'
    b["nodes"][0]["path"] += evil
    a["nodes"][0]["path"] += evil
    result = history.compare(b, a, notes={b["nodes"][0]["path"]: evil})
    page = history.render(result, [a, b])
    assert evil not in page and "&lt;script&gt;" in page
    assert "01 月 01 日 08:00" in page
    assert "父子目录不能相加" in page


def test_scan_entry_writes_history_and_growth(tmp_path):
    from test_scan import run_scan, write_file
    root = tmp_path / "tree"
    write_file(str(root / "data" / "a"), 2 * 1024 * 1024)
    first = run_scan(tmp_path, [root], tag="history-first")
    write_file(str(root / "data" / "b"), 1024 * 1024)
    current = run_scan(tmp_path, [root], tag="history-next")
    run = tmp_path / "runs" / current["scan_id"]
    growth = json.loads((run / "growth.json").read_text())
    assert growth["baseline"]["scan_id"] == first["scan_id"]
    assert (run / "growth.html").exists()
    assert len(list((tmp_path / "history").glob("*.json.gz"))) == 2
