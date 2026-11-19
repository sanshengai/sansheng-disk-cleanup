"""Native retirement regression fixtures, exclusively in temporary HOME directories."""
import hashlib
import json
import os
import pwd
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from test_codex_threadless import C1, R1, E1, OLD_TS, act, calls, env, item_for, make_dbs, protect, row


def seal(tmp_path, item, guard=lambda t, cutoff: []):
    path = tmp_path / "batch.json"
    data = json.dumps(dict(schema=1, authorization="Remove these synthetic sessions", cutoff=time.time()-4*86400,
                           items={item['argv'][-1]: item})).encode()
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest(), guard


def populate(env):
    make_dbs(env, [row(R1), row(C1)], [(R1, C1)])
    with sqlite3.connect(env.home / ".codex/state_5.sqlite") as con:
        con.execute("alter table threads add column attached_thread_parent_id text")
    with sqlite3.connect(env.home / ".codex/thread_history_1.sqlite") as con:
        con.execute("create table thread_turns (thread_id,status,started_at,completed_at,rollout_ordinal)")
        con.execute("insert into thread_turns values (?,?,?,?,1)", (C1,"completed",OLD_TS-100,OLD_TS))
        con.execute("create trigger remove_turn after delete on thread_items begin delete from thread_turns where thread_id=old.thread_id; end")
        con.execute("insert into thread_items values (?, '{}')", (C1,))
    item = item_for(env, C1)
    item.update(prog=protect.resolve_prog("codex"), realpath=os.path.realpath(item["path"]))
    return item


@pytest.mark.parametrize("mutation", ["ok","default","scope","digest","recent","running","child","compressed_child","attached","guard","remaining_turn"])
def test_exact_leaf(env, tmp_path, mutation):
    item = populate(env)
    batch = seal(tmp_path, item, lambda t, cut: ["unprotected material"] if mutation=="guard" else [])
    if mutation == "compressed_child":
        zstd = pytest.importorskip("compression.zstd")
        child = env.home / ".codex/sessions" / f"rollout-{E1}.jsonl.zst"
        child.parent.mkdir(exist_ok=True)
        record = {"type":"session_meta","payload":{"id":E1,"source":{"subagent":{"thread_spawn":{"parent_thread_id":C1}}}}}
        child.write_bytes(zstd.compress((json.dumps(record)+"\n").encode()))
    if mutation=="digest":
        batch[0].write_bytes(batch[0].read_bytes()+b" ")
    if mutation=="scope":
        item["other"] = "unsealed"
    with sqlite3.connect(env.home / ".codex/state_5.sqlite") as con:
        if mutation=="attached":
            con.execute("update threads set attached_thread_parent_id=? where id=?", (R1,C1))
        if mutation=="child":
            con.execute("insert into thread_spawn_edges values (?,?,'closed')", (C1,E1))
            con.execute("insert into threads(id) values (?)", (E1,))
    with sqlite3.connect(env.home / ".codex/thread_history_1.sqlite") as con:
        if mutation=="running": con.execute("update thread_turns set status='running'")
        if mutation=="recent": con.execute("update thread_turns set completed_at=?", (time.time(),))
        if mutation=="remaining_turn": con.execute("drop trigger remove_turn")
    result = act.run_native_threadless(item, {"roots":[str(env.home)]}, act.ItemResult(0,item), False,
                                       time.time(), str(env.run), batch=None if mutation=="default" else batch)
    expected = {"ok":"removed","remaining_turn":"failed"}.get(mutation,"skipped")
    assert result["status"] == expected, result
    assert len(calls(env)) == int(mutation in ("ok","remaining_turn"))


def test_real_native_compressed(env, tmp_path, monkeypatch):
    zstd = pytest.importorskip("compression.zstd")
    binary = next((Path(d)/"codex" for d in ("/opt/homebrew/bin","/usr/local/bin") if (Path(d)/"codex").is_file()),None)
    if binary is None:
        pytest.skip("real Codex CLI unavailable")
    base = env.home / ".codex"
    base.mkdir(exist_ok=True)
    isolated = {"HOME":str(env.home),"CODEX_HOME":str(base),"PATH":str(binary.parent)+":/usr/bin:/bin"}
    # Initialize the installed CLI's own schema in the disposable profile.
    subprocess.run([binary,"delete","--force",E1], env=isolated, cwd=tmp_path,capture_output=True, timeout=30)
    raw = base/"sessions"/f"rollout-2026-01-01T00-00-00-{C1}.jsonl"
    raw.parent.mkdir(exist_ok=True)
    compressed = Path(str(raw)+".zst")
    def header(t):
        timestamp = "2026-01-01T00:00:00.000Z"
        payload = dict(id=t, session_id=t, timestamp=timestamp, cwd=str(env.home),
                       originator="codex_cli_rs", cli_version="0.170.0", source="cli",
                       model_provider="openai", history_mode="paginated", base_instructions={"text":"fixture"})
        record = dict(timestamp=timestamp, type="session_meta", payload=payload)
        compressed.write_bytes(zstd.compress((json.dumps(record)+"\n").encode()))
        os.utime(compressed,(OLD_TS,OLD_TS))
    with sqlite3.connect(base/"state_5.sqlite") as con:
        con.execute("insert into threads(id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode) "
                    "values(?,?,?,?,?,?,?,?,?,?)",(C1,str(raw),OLD_TS,OLD_TS,"cli","synthetic",str(env.home),"synthetic",'{"type":"read-only"}',"never"))
        con.execute("update threads set history_mode='paginated', created_at_ms=?, updated_at_ms=?, recency_at=?, recency_at_ms=?",(OLD_TS*1000,OLD_TS*1000,OLD_TS,OLD_TS*1000))
    with sqlite3.connect(base/"thread_history_1.sqlite") as con:
        # Read only the installed schema and SQLx migration checksums, never session records.
        template = Path(pwd.getpwuid(os.getuid()).pw_dir)/".codex/thread_history_1.sqlite"
        if not template.is_file():
            pytest.skip("native history schema unavailable")
        with sqlite3.connect(template.as_uri()+"?mode=ro", uri=True) as src:
            for (ddl,) in src.execute("select sql from sqlite_master where sql is not null and name not like 'sqlite_%' order by rowid"):
                con.execute(ddl)
            for rec in src.execute("select * from _sqlx_migrations"):
                con.execute("insert into _sqlx_migrations values ("+",".join("?" for _ in rec)+")",rec)
        con.execute("insert into thread_turns(thread_id,status,started_at,completed_at,rollout_ordinal,turn_id) values (?,?,?,?,1,?)",
                    (C1,"completed",OLD_TS,OLD_TS,R1))
    monkeypatch.setattr(protect,"NATIVE_FIXED_PATH",(str(binary.parent),"/usr/bin","/bin"))
    monkeypatch.setenv("PATH",isolated["PATH"])
    p=str(compressed)
    item=dict(path=p,realpath=p,prog=str(binary),argv=["codex","delete","--force",C1])
    batch=seal(tmp_path,item)
    def execute(dry):
        return act.run_native(item,p,act.ItemResult(0,item),dry,time.time(),str(env.run),batch=batch)
    header(R1)
    assert execute(True)["status"]=="skipped"
    header(C1)
    assert execute(True)["status"]=="planned"
    res=execute(False)
    assert res["returncode"] == 0, res
    with sqlite3.connect(base/"state_5.sqlite") as con:
        assert not con.execute("select id from threads where id=?",(C1,)).fetchone()
    assert res["status"] == "removed", res
    assert res["remaining_records"] == {"threads":0,"thread_turns":0,"thread_items":0}
    assert not compressed.exists()
    assert res["removed"]["files"] == 1
    assert res["removed"]["bytes"] > 0


@pytest.mark.parametrize("mutation", ["leftover", "wrong_path", "tampered", "unsealed", "media"])
def test_compressed_residual_accounting(env, tmp_path, mutation):
    zstd = pytest.importorskip("compression.zstd")
    populate(env)
    raw = env.home / ".codex/sessions" / f"rollout-{C1}.jsonl"
    raw.parent.mkdir(exist_ok=True)
    path = Path(str(raw) + ".zst")
    path.write_bytes(zstd.compress((json.dumps({"type":"session_meta","payload":{"id":C1}})+"\n").encode()))
    os.utime(path, (OLD_TS, OLD_TS))
    with sqlite3.connect(env.home / ".codex/state_5.sqlite") as con:
        con.execute("update threads set rollout_path=? where id=?", (str(raw), C1))
    item = dict(path=str(path),realpath=str(path),prog=protect.resolve_prog("codex"),
                argv=["codex","delete","--force",C1])
    batch = seal(tmp_path, item, lambda *_: ["未独立保留的素材"] if mutation == "media" else [])
    if mutation == "wrong_path":
        with sqlite3.connect(env.home / ".codex/state_5.sqlite") as con:
            con.execute("update threads set rollout_path='different' where id=?", (C1,))
    if mutation == "tampered":
        path.write_bytes(b"not a zstd stream")
    result = act.run_native(item, str(path), act.ItemResult(0, item), False, time.time(),
                            str(env.run), batch=None if mutation=="unsealed" else batch)
    assert path.is_file()
    if mutation == "leftover":
        assert result["status"] == "removed" and result["rollout_remaining"] == str(path)
        assert result["remaining_records"] == {"threads":0,"thread_turns":0,"thread_items":0}
        assert result["removed"]["files"] == result["removed"]["bytes"] == 0
    else:
        assert result["status"] == "skipped"
        assert calls(env) == []
