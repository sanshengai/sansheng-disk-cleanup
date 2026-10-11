"""sysprobe 的测试：伪造命令输出；夹具只用 tmp_path。"""
import json
import os
import plistlib
import subprocess
import sys
import types

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)
import sysprobe  # noqa: E402


def fake_run(table):
    """table: {命令首两词: (returncode, stdout, stderr) 或 Exception}"""
    def _run(args, **kw):
        key = " ".join(args[:2])
        v = table.get(key)
        if v is None:
            return subprocess.CompletedProcess(args, 1, "", "unknown")
        if isinstance(v, Exception):
            raise v
        return subprocess.CompletedProcess(args, v[0], v[1], v[2])
    return _run


SNAPS = ("Snapshots for disk /:\ncom.apple.TimeMachine.2026-10-10-120000.local\n"
         "com.apple.os.update-ABCDEF0123\nsomething.else.snapshot\n")


def test_snapshot_kinds(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run({
        "tmutil listlocalsnapshots": (0, SNAPS, ""),
        "tmutil latestbackup": (0, "/Volumes/TM/Backups/x\n", ""),
        "tmutil destinationinfo": (0, "Name : TM\nID : 1\n", "")}))
    r = sysprobe.probe_local_snapshots()
    assert r["count_by_kind"] == {"os_update": 1, "time_machine": 1, "other": 1}
    assert r["latest_backup"] == "/Volumes/TM/Backups/x"
    assert r["destinations"] is True
    assert "单个快照" in r["note"]


def test_tmutil_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run({
        "tmutil listlocalsnapshots": (1, "", "boom"),
        "tmutil latestbackup": (1, "", "No backups"),
        "tmutil destinationinfo": (1, "", "No destinations configured")}))
    r = sysprobe.probe_local_snapshots()
    assert r["snapshots"] is None and r["list_reason"]
    assert r["latest_backup"] is None and r["latest_backup_reason"]
    assert r["destinations"] is False


def cap_table(swift):
    plist = plistlib.dumps({"APFSContainerFree": 100, "APFSContainerSize": 1000}).decode()
    return {"diskutil info": (0, plist, ""), "swift -e": swift}


def test_diskutil_and_purgeable(monkeypatch):
    monkeypatch.setattr(os, "statvfs", lambda p: types.SimpleNamespace(f_bavail=10, f_frsize=1, f_blocks=500))
    monkeypatch.setattr(subprocess, "run", fake_run(cap_table((0, "35\n", ""))))
    c = sysprobe.probe_capacity()
    assert c["apfs_container_free"] == 100 and c["apfs_container_size"] == 1000
    assert c["statvfs_free"] == 10 and c["finder_available"] == 35
    assert c["purgeable_estimate"] == 25


def test_purgeable_negative_is_null(monkeypatch):
    monkeypatch.setattr(os, "statvfs", lambda p: types.SimpleNamespace(f_bavail=50, f_frsize=1, f_blocks=500))
    monkeypatch.setattr(subprocess, "run", fake_run(cap_table((0, "35\n", ""))))
    c = sysprobe.probe_capacity()
    assert c["purgeable_estimate"] is None and c["purgeable_reason"]


def test_swift_timeout_null(monkeypatch):
    monkeypatch.setattr(os, "statvfs", lambda p: types.SimpleNamespace(f_bavail=10, f_frsize=1, f_blocks=500))
    monkeypatch.setattr(subprocess, "run",
                        fake_run(cap_table(subprocess.TimeoutExpired("swift", 15))))
    c = sysprobe.probe_capacity()
    assert c["finder_available"] is None and "超时" in c["finder_reason"]
    assert c["purgeable_estimate"] is None


def fda(monkeypatch, effects):
    paths = sysprobe.fda_probe_paths()
    m = dict(zip(paths, effects))

    def fake_listdir(p):
        e = m[p]
        if isinstance(e, Exception):
            raise e
        return []
    monkeypatch.setattr(os, "listdir", fake_listdir)
    return sysprobe.probe_full_disk_access()


def test_fda_states(monkeypatch):
    P, N = PermissionError(1, "x"), FileNotFoundError(2, "x")
    assert fda(monkeypatch, [P, P, N, P])["state"] == "denied"
    assert fda(monkeypatch, [P, [], N, P])["state"] == "granted"
    assert fda(monkeypatch, [N, N, N, N])["state"] == "inconclusive"
    assert fda(monkeypatch, [OSError(5, "io"), N, N, N])["probes"][0]["result"] == "error"


def test_system_data(tmp_path):
    (tmp_path / "facts.json").write_text(json.dumps({"coverage": {"walked_bytes": 300}}))
    r = sysprobe.probe_system_data(str(tmp_path), {"apfs_container_size": 1000, "apfs_container_free": 100})
    assert r["unattributed"] == 600 and "粗估" in r["caveat"]
    assert sysprobe.probe_system_data(None, {})["unattributed"] is None
