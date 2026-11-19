import base64
import hashlib
import json
import sys
import sqlite3
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import session_materials as sm
import quick_act as qa
from test_quick_act import item, plan, run, world
from test_codex_threadless import R1, base_interactive, calls, dcard, item_for, node
from test_codex_threadless import env

DATA = b"synthetic private attachment"
ENC = base64.b64encode(DATA).decode()
EMBED = {"message": {"content": [{"type": "image", "source": {"type": "base64", "data": ENC}}]}}


def proof(tmp):
    retained = tmp / "retained.bin"
    retained.write_bytes(DATA)
    path = tmp / "materials.json"
    path.write_text(json.dumps({"copies": {hashlib.sha256(DATA).hexdigest(): str(retained)}}))
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.mark.parametrize("failure", ["missing", "changed", "proof_changed", "valid"])
def test_quick_real_file_scope(world, monkeypatch, failure):
    home = world["work"]
    monkeypatch.setattr(qa, "HOME", str(home))
    d = home / ".claude/projects/fake"
    d.mkdir(parents=True)
    source = d / "old.jsonl"
    source.write_text(json.dumps(EMBED))
    entry = item(d, scope="files", files=[str(source)], kind="verified_obsolete")
    if failure != "missing":
        entry["session_materials"] = proof(world["tmp"])
    p = plan(world, [entry], text=f"直接永久删除 {d}")
    if failure == "changed":
        (world["tmp"] / "retained.bin").write_bytes(b"changed")
    if failure == "proof_changed":
        (world["tmp"] / "materials.json").write_text('{"copies":{}}')
    run(world, p["seal"])
    assert source.exists() == (failure != "valid")


def test_cross_item_copy_is_protected(world):
    home = world["work"]
    entry = item(home / "item1", session_materials=proof(world["tmp"]))
    other = item(world["tmp"] / "retained.bin")
    with pytest.raises(ValueError, match="同批|本轮"):
        qa.validate([entry, other])


def test_roots_limits_and_purge_mapping(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / ".claude/projects"
    source = root / "project/session.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(EMBED))
    assert sm.path_error(root, home)
    q = root.with_name("projects.待删-20260101")
    root.rename(q)
    assert sm.path_error(q, home, original=root)
    assert sm.path_error(q, home) is None
    (q / "project/session.jsonl").write_text("not-json")
    assert sm.path_error(q, home, original=root)
    (q / "project/session.jsonl").write_text('{"text":"ordinary"}')
    assert sm.path_error(q, home, original=root) is None
    monkeypatch.setattr(sm, "MAX_RECORD", 4)
    assert sm.path_error(q, home, original=root)


@pytest.mark.parametrize("row", [
    {"type":"imageGeneration","result":ENC},
    {"type":"function_call_output", "output":json.dumps(EMBED)},
    {"type":"input_audio","input_audio":{"data":ENC,"format":"wav"}},
    {"type":"mcpToolCall","result":{"content":[{"type":"image","data":ENC}]}},
    {"type":"userMessage","content":[{"type":"input_image","image_url":"data:image/png;base64,"+ENC}]},
    {"type":"dynamicToolCall","contentItems":[{"type":"resource","resource":{"blob":ENC}}]},
])
def test_native_sqlite_only_real_entry(env, tmp_path, row):
    base_interactive(env)
    with sqlite3.connect(env.home / ".codex/thread_history_1.sqlite") as db:
        db.execute("INSERT INTO thread_items VALUES (?,?)", (R1,json.dumps(row)))
    def invoke(spec=None):
        entry=item_for(env,R1)
        if spec:entry["session_materials"]=spec
        env.write([node(env.home / ".codex")], [dcard("c01",[entry])])
        env.act("c01", answers=["c01=删"])
    invoke()
    assert not calls(env)
    invoke(proof(tmp_path))
    assert calls(env)


def test_compressed_raw_material(tmp_path):
    zstd = pytest.importorskip("compression.zstd")
    root = tmp_path / ".codex/archived_sessions"
    root.mkdir(parents=True)
    f = root / "rollout-old.jsonl.zst"
    f.write_bytes(zstd.compress(json.dumps(EMBED).encode()))
    assert sm.path_error(f,tmp_path)
    assert sm.path_error(f,tmp_path,proof(tmp_path)) is None
    f.write_bytes(b"broken")
    assert sm.path_error(f,tmp_path)


def test_batch_callback_needs_material_proof(env, tmp_path):
    from test_codex_batch import C1, act, populate, seal
    import time

    entry = populate(env)
    with sqlite3.connect(env.home / ".codex/thread_history_1.sqlite") as db:
        db.execute("UPDATE thread_items SET item_json=? WHERE thread_id=?", (json.dumps(EMBED), C1))
    entry["identity"] = item_for(env, C1)["identity"]
    def invoke():
        return act.run_native_threadless(entry, {"roots": [str(env.home)]}, act.ItemResult(0, entry),
                                         False, time.time(), str(env.run), batch=seal(tmp_path, entry))
    assert invoke()["status"] == "skipped"
    assert not calls(env)
    entry["session_materials"] = proof(tmp_path)
    assert invoke()["status"] == "removed"
    assert len(calls(env)) == 1


def test_native_realtime_attachment_and_raw_database(env, tmp_path):
    base_interactive(env)
    base = env.home / ".codex"
    history = base / "thread_history_1.sqlite"
    assert sm.path_error(history, env.home)
    assert sm.path_error(tmp_path / history.name, env.home) is None
    assert sm.native_error(["/"], env.home)
    with sqlite3.connect(history) as db:
        db.execute("CREATE TABLE thread_realtime_items (thread_id TEXT, item_json TEXT)")
        db.execute("INSERT INTO thread_realtime_items VALUES (?,?)", (R1, json.dumps(EMBED)))
    assert sm.native_error([R1], env.home)
    spec = proof(tmp_path)
    assert sm.native_error([R1], env.home, spec=spec) is None
    with sqlite3.connect(base / "state_5.sqlite") as db:
        db.execute("CREATE TABLE thread_attachments (thread_id TEXT, payload TEXT)")
        db.execute("INSERT INTO thread_attachments VALUES (?,?)", (R1, '{"unknown":"attachment"}'))
    assert sm.native_error([R1], env.home, spec=spec)


def test_pasted_contents(tmp_path):
    source = tmp_path / ".claude/history.jsonl"
    source.parent.mkdir()
    source.write_text(json.dumps({"pastedContents": {"1": {"type": "text", "content": DATA.decode()}}}))
    assert sm.path_error(source, tmp_path)
    assert sm.path_error(source, tmp_path, proof(tmp_path)) is None
