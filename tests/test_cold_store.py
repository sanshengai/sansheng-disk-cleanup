"""冷存出口：候选只选整棵子树都冷的目录、Git 已跟踪的不计体积、不选第一层目录；offload 原样转交冷存工具。"""
import json, os, subprocess, sys, time

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)
import cold_store as C  # noqa: E402

OLD = time.time() - 30 * 86400


def mk(p, n, old=True):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f: f.write(b"x" * n)
    if old: os.utime(p, (OLD, OLD))


def test_pick_highest_cold_subtree_not_first_level(tmp_path):
    r = str(tmp_path)
    mk(f"{r}/视频/季A/ep1.mp4", 3000); mk(f"{r}/视频/季A/ep2.mp4", 3000)
    mk(f"{r}/视频/季B/ep1.mp4", 3000); mk(f"{r}/视频/季B/新.mp4", 10, old=False)   # 季B 有新改动
    rows = C.pick(C.scan(r), 14, 1000)
    paths = [x["path"] for x in rows]
    assert "视频/季A" in paths and "视频" not in paths
    assert "视频/季B" not in paths


def test_tracked_files_do_not_count(tmp_path):
    r = str(tmp_path)
    subprocess.run(["git", "init", "-q", r], check=True)
    mk(f"{r}/资料/甲/跟踪.bin", 5000); mk(f"{r}/资料/乙/未跟踪.bin", 5000)
    subprocess.run(["git", "-C", r, "add", "资料/甲/跟踪.bin"], check=True)
    rows = C.pick(C.scan(r), 14, 1000)
    assert [x["path"] for x in rows] == ["资料/乙"]


def test_offload_passes_through_to_cli(tmp_path, monkeypatch):
    log = tmp_path / "calls.json"
    fake = tmp_path / "fake.py"
    fake.write_text(f"import json,sys;json.dump(sys.argv[1:],open({str(log)!r},'w'))")
    cfg = tmp_path / "t.json"; cfg.write_text(json.dumps({"cold_storage": {"cli": [sys.executable, str(fake)]}}))
    monkeypatch.setenv("SANSHENG_DISK_CLEANUP_TRIAGE", str(cfg))
    d = tmp_path / "目录"; d.mkdir()
    monkeypatch.setattr(sys, "argv", ["cold_store.py", "offload", str(d), "--user-text", "同意冷存 目录"])
    with pytest.raises(SystemExit) as ex: C.main()
    assert ex.value.code == 0
    assert json.loads(log.read_text()) == ["cold", "offload", str(d), "--user-text", "同意冷存 目录", "--min-idle-days", "14"]


def test_offload_refuses_without_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SANSHENG_DISK_CLEANUP_TRIAGE", str(tmp_path / "none.json"))
    monkeypatch.setattr(sys, "argv", ["cold_store.py", "offload", str(tmp_path), "--user-text", "同意冷存这里"])
    with pytest.raises(SystemExit) as ex: C.main()
    assert "未配置冷存工具" in str(ex.value.code)


def _triage(tmp_path, monkeypatch, entries):
    tj = tmp_path / "triage.json"; tj.write_text(json.dumps(entries, ensure_ascii=False))
    cfg = {"work_root": str(tmp_path / "w")}
    return argparse_ns(from_triage=str(tj), min_idle_days=14, min_gib=1000 / C.GIB, json=None), cfg


def argparse_ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


def test_from_triage_drills_into_protected_dirs(tmp_path, monkeypatch):
    w = tmp_path / "w"
    mk(f"{w}/视频/试播/旧批次/a.mp4", 3000); mk(f"{w}/视频/试播/新批次/a.mp4", 3000, old=False)
    mk(f"{w}/视频/试播/旧批次/tmp/x.bin", 10)
    a, cfg = _triage(tmp_path, monkeypatch, {"protected_production": [
        {"path": f"{w}/视频/试播", "bytes": 6000, "red_line": "生产目录默认保护"},
        {"path": f"{w}/视频/试播/旧批次", "bytes": 3000}]})          # 嵌套的下层不重复下钻
    rows = C.from_triage(a, cfg)
    assert [r["path"] for r in rows] == ["视频/试播/旧批次"]
    assert rows[0]["guard"] == "生产目录默认保护" and rows[0]["source"] == "protected_production"


def test_from_triage_whole_root_but_not_first_level(tmp_path, monkeypatch):
    w = tmp_path / "w"
    mk(f"{w}/资料/甲/a.bin", 3000); mk(f"{w}/顶层/b.bin", 3000)
    a, cfg = _triage(tmp_path, monkeypatch, {"unclassified": [
        {"path": f"{w}/资料/甲", "bytes": 3000}, {"path": f"{w}/顶层", "bytes": 3000}]})
    paths = [r["path"] for r in C.from_triage(a, cfg)]
    assert "资料/甲" in paths and "顶层" not in paths


def test_process_like_flag(tmp_path, monkeypatch):
    w = tmp_path / "w"
    mk(f"{w}/工作/tmp/渲染/a.bin", 3000)
    a, cfg = _triage(tmp_path, monkeypatch, {"referenced_ask": [{"path": f"{w}/工作/tmp", "bytes": 3000}]})
    rows = C.from_triage(a, cfg)
    assert rows and rows[0]["process_like"] is True


def test_cold_store_filters_protected_and_external_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    mk(f"{home}/Documents/task1/sub1/big.bin", 3000)
    mk(f"{home}/Library/Containers/docker/Data/big.bin", 3000)
    mk(f"{home}/custom-work/archive/sub1/big.bin", 3000)
    ext = tmp_path / "opt" / "homebrew"
    mk(f"{ext}/bin/big.bin", 3000)
    monkeypatch.setattr(C.protect, "HOME", str(home))
    monkeypatch.setattr(C, "HOME", str(home))
    a, cfg = _triage(tmp_path, monkeypatch, {
        "tier3": [
            {"path": f"{home}/Documents/task1", "bytes": 3000},
            {"path": f"{home}/Library/Containers/docker/Data", "bytes": 3000},
            {"path": f"{home}/custom-work/archive", "bytes": 3000},
            {"path": str(ext), "bytes": 3000},
        ]
    })
    cfg["work_root"] = str(home / "custom-work")
    paths = [r["abs"] for r in C.from_triage(a, cfg)]
    assert f"{home}/custom-work/archive/sub1" in paths
    assert f"{home}/Documents/task1" not in paths
    assert f"{home}/Documents/task1/sub1" not in paths
    assert f"{home}/Library/Containers/docker/Data" not in paths
    assert str(ext) not in paths


def test_offload_optional_flags_passthrough(tmp_path, monkeypatch):
    log = tmp_path / "calls.json"
    fake = tmp_path / "fake.py"
    fake.write_text(f"import json,sys;json.dump(sys.argv[1:],open({str(log)!r},'w'))")
    cfg = tmp_path / "t.json"; cfg.write_text(json.dumps({"cold_storage": {"cli": [sys.executable, str(fake)]}}))
    monkeypatch.setenv("SANSHENG_DISK_CLEANUP_TRIAGE", str(cfg))
    d = tmp_path / "目录"; d.mkdir()
    monkeypatch.setattr(sys, "argv", [
        "cold_store.py", "offload", str(d), "--user-text", "同意冷存 目录",
        "--min-idle-days", "3", "--keep", "key.txt", "--quiet-hours", "12", "--self-transcript", "/tmp/t.jsonl",
    ])
    with pytest.raises(SystemExit) as ex: C.main()
    assert ex.value.code == 0
    assert json.loads(log.read_text()) == [
        "cold", "offload", str(d), "--user-text", "同意冷存 目录",
        "--min-idle-days", "3", "--keep", "key.txt", "--quiet-hours", "12", "--self-transcript", "/tmp/t.jsonl",
    ]


def test_busy_unknown_and_recorded_hard_guard_exclude_only_their_branch(tmp_path, monkeypatch):
    work = tmp_path / "w"
    root = work / "media" / "season"
    for child in ("runtime", "model", "archive"):
        mk(str(root / child / "sample.bin"), 3000)
    a, cfg = _triage(tmp_path, monkeypatch, {
        "referenced_ask": [{"path": str(root), "bytes": 9000}],
        "unclassified": [{"path": str(root / "runtime"), "bytes": 1, "open": 13}],
        "tier3_protected_ref": [{"path": str(root / "model"), "bytes": 1,
                                "keep_hint": "硬保护：本机模型"}],
    })
    assert [r["abs"] for r in C.from_triage(a, cfg)] == [str(root / "archive")]
    monkeypatch.setattr(C.triage, "HARD_KEEP", ("season/archive",))
    assert C.from_triage(a, cfg) == []


def test_consumer_scope_keeps_linked_sources_and_incomplete_checks_local(tmp_path):
    root = tmp_path / "shared"
    source = root / "originals"
    source.mkdir(parents=True)
    (source / "a").touch()
    (source / "self").symlink_to("a")
    sibling = root / "index"
    sibling.mkdir()
    (sibling / "external").symlink_to("../originals/a")
    cfg = {"consumer_scopes": [{"source": str(source), "roots": [str(root)]}]}
    c = C.ConsumerChecks(cfg)
    row = {"abs": str(source)}
    assert not c.permits(row)
    assert row["reverse_symlinks"]["links"] == row["reverse_symlinks"]["distinct_targets"] == 1
    assert c.inspect(str(sibling))["status"] == "unknown"
    (sibling / "external").unlink()
    assert C.ConsumerChecks(cfg).inspect(str(source))["status"] == "none_in_scope"
    cfg["consumer_scopes"][0]["roots"].append(str(root / "missing"))
    assert not C.ConsumerChecks(cfg).permits({"abs": str(source)})


def test_offload_stops_before_adapter_when_external_directory_link_exists(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    source = root / "originals"
    source.mkdir(parents=True)
    (root / "consumer").symlink_to("originals", target_is_directory=True)
    cfg = {"cold_storage": {"consumer_scopes": [{"source": str(source), "roots": [str(root)]}]}}
    monkeypatch.setattr(C, "load_config", lambda: cfg)
    monkeypatch.setattr(C, "run_cli", lambda *args: pytest.fail("forwarded unsafe offload"))
    monkeypatch.setattr(sys, "argv", ["cold_store.py", "offload", str(source), "--user-text", "同意冷存此目录"])
    with pytest.raises(SystemExit, match="保留本机"):
        C.main()


def test_evidence_routes_require_matched_receipt_or_confirmed_session_structure(tmp_path):
    from cold_review import ReviewRoutes
    root = tmp_path / "scratch" / "prior"
    root.mkdir(parents=True)
    ledger = tmp_path / "ledger.json"
    batch = {"id": "b", "status": "partial", "remote_root": "/cold/b", "local": str(root),
             "offloaded": "2025-01-01", "manifest_sha256": "a" * 64}
    marker = root / "receipt.md"
    marker.write_text("<!-- netdisk-cold batch=b remote_root=/cold/b -->")
    config = {"placeholder": marker.name, "ledger": str(ledger)}
    def routes():
        ledger.write_text(json.dumps({"schema": "netdisk-cold/1", "batches": {"b": batch}}))
        return ReviewRoutes(config, tmp_path)
    assert routes().evidence(root / "nested")["route"] == "verify_existing_backup"
    batch["status"] = "planned"
    assert routes().evidence(root) is None
    batch["status"] = "partial"
    batch["local"] = str(root.parent)
    assert routes().evidence(root) is None
    session = tmp_path / ".claude/projects/demo/12345678-1234-1234-1234-123456789012"
    (session / "subagents").mkdir(parents=True)
    assert routes().evidence(session) is None
    session.with_suffix(".jsonl").touch()
    assert routes().evidence(session)["route"] == "session_review"
    assert routes().evidence(root.parent / "cache") is None
