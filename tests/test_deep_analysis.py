import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import artifact_checkpoint as ac
import codex_state
import deep_inventory
import plan
import scan
import act


def fixture_index(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args, env=None):
        return subprocess.check_output(["git", "-C", str(repo), "-c", "user.name=test",
            "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args], env=env).decode().strip()
    git("init", "-q")
    (repo / ".gitignore").write_text("*.index\n")
    (repo / "data").write_text("retained\n")
    git("add", ".")
    git("commit", "-qm", "retained")
    ref, commit = git("symbolic-ref", "HEAD"), git("rev-parse", "HEAD")
    idx = repo / "old.index"
    env = dict(os.environ, GIT_INDEX_FILE=str(idx))
    git("read-tree", commit, env=env)
    before = idx.read_bytes()
    tree, digest = ac.index_tree(idx, repo)
    assert idx.read_bytes() == before
    assert tree == git("rev-parse", "HEAD^{tree}")
    item = {"path": str(idx), "method": "delete", "scope": "all", "checkpoint": {
        "kind": "git_index", "repo": str(repo), "commit": commit, "retain_ref": ref, "sha256": digest}}
    return repo, idx, item, git, env


def test_checkpoint_fail_closed_and_mutation(tmp_path):
    repo, idx, item, git, env = fixture_index(tmp_path)
    assert ac.verify(item)[0]
    assert not ac.verify({})[0]
    assert not ac.verify(dict(item, scope="files"))[0]
    bad = dict(item, checkpoint=dict(item["checkpoint"], retain_ref="refs/heads/missing"))
    assert not ac.verify(bad)[0]
    bad = dict(item, checkpoint=dict(item["checkpoint"], sha256="0" * 64))
    assert not ac.verify(bad)[0]
    (repo / "data").write_text("not committed\n")
    git("add", "data", env=env)
    item["checkpoint"]["sha256"] = hashlib.sha256(idx.read_bytes()).hexdigest()
    assert not ac.verify(item)[0]  # 即使更新指纹，未保存在提交里的暂存修改仍被拒绝。


def test_checkpoint_cannot_delete_real_index_or_symlink(tmp_path):
    repo, idx, item, git, env = fixture_index(tmp_path)
    link = repo / "link.index"
    link.symlink_to(idx)
    assert not ac.verify(dict(item, path=str(link)))[0]
    real = repo / ".git" / "danger.index"
    real.write_bytes(idx.read_bytes())
    assert not ac.verify(dict(item, path=str(real)))[0]
    git("add", "-f", str(idx))
    assert not ac.verify(item)[0]


def test_active_exception_keeps_protected_and_execute_rechecks(tmp_path, monkeypatch):
    repo, idx, item, git, env = fixture_index(tmp_path)
    active = [{"name": "test", "status": "active"}]
    assert plan.project_item_errors(str(idx), active)
    assert not plan.project_item_errors(str(idx), active, ac.verify(item)[0])
    assert plan.project_item_errors(str(idx), [{"name": "test", "status": "protected"}], True)
    monkeypatch.setattr(act, "verify_identity", lambda *args: (str(idx), None))
    monkeypatch.setattr(act, "blocking_processes", lambda *args: ([], []))
    item["checkpoint"]["sha256"] = "0" * 64
    result = act.execute_item(0, item, {}, False, time.time())
    assert result["status"] == "skipped"
    assert idx.exists()


def test_compressed_rollout_is_counted_and_read_from_db(tmp_path):
    base = tmp_path / ".codex"
    folder = base / "sessions" / "today"
    folder.mkdir(parents=True)
    tid = "01234567-1234-1234-1234-0123456789ab"
    p = folder / ("rollout-test-" + tid + ".jsonl.zst")
    p.write_bytes(b"compressed bytes, deliberately not JSON")
    con = sqlite3.connect(base / "state_5.sqlite")
    con.execute("CREATE TABLE threads(id TEXT, cwd TEXT, source TEXT, archived INT)")
    con.execute("INSERT INTO threads VALUES(?,?,?,?)", (tid, "/project", '"cli"', 0))
    con.commit(); con.close()
    assert codex_state.scan_rollouts(str(base))[tid] == str(p)
    meta, first = scan.read_rollout_head(str(p))
    assert meta["cwd"] == "/project" and meta["archived"] == 0 and first is None
    rows, _ = scan.collect_codex_threads(str(base), with_lsof=False)
    assert tid in rows and rows[tid]["cwd"] == "/project"


def test_deep_census_keeps_small_groups_and_no_symlinks(tmp_path):
    for n in range(45):
        d = tmp_path / str(n)
        d.mkdir(); (d / "tiny.txt").write_text("x")
    (tmp_path / "linked").symlink_to(tmp_path / "1", target_is_directory=True)
    d = deep_inventory.census(tmp_path)
    assert len(d["groups"]) == 45 and not d["errors"]
    facts = {"nodes": [{"path": str(tmp_path / str(n)), "bytes": 10, "depth": 1} for n in range(45)]}
    summary = plan.facts_summary(facts)
    assert len(summary["frontier"]) == 45 and len(summary["areas"]) == 45


def test_last_turn_is_not_task_completion_and_db_errors_visible(tmp_path):
    con = sqlite3.connect(tmp_path / "thread_history_1.sqlite")
    con.execute("CREATE TABLE thread_turns(thread_id TEXT,status TEXT,started_at INT,completed_at INT)")
    con.executemany("INSERT INTO thread_turns VALUES(?,?,?,?)", [("thread", "completed", 99, 100), ("thread", "inProgress", 101, None)])
    con.commit(); con.close()
    rows, errors = codex_state.latest_turns(tmp_path)
    assert rows["thread"]["status"] == "inProgress" and not errors
    (tmp_path / "state_5.sqlite").write_bytes(b"corrupt")
    rows, errors = codex_state.thread_metadata(tmp_path)
    assert rows == {} and errors
