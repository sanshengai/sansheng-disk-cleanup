"""会话文件已不在、但 Codex 状态库与历史库里还有记录的线程（identity.kind=codex_thread_without_rollout）。

全部在临时 HOME 里：两个 sqlite 库（state_5 / thread_history_1）是现造的，codex 是一个假的可执行脚本
（只改临时 HOME 下的库），不碰真实的 ~/.codex、~/.sansheng，不运行真实的 codex delete。
"""
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, age, card, mkfile, node, plan, rollout, act, codex_state, protect  # noqa: E402

import scan  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore")

OLD_TS = int(time.time()) - 10 * 86400
R1 = "00000000-0000-7000-8000-0000000000a1"       # 交互根线程（cli）
C1 = "00000000-0000-7000-8000-0000000000a2"       # R1 的子任务
R2 = "00000000-0000-7000-8000-0000000000b1"       # 交互根线程（vscode）
E1 = "00000000-0000-7000-8000-0000000000c1"       # exec 脚本会话
E2 = "00000000-0000-7000-8000-0000000000c2"
E3 = "00000000-0000-7000-8000-0000000000c3"


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(scan, "HOME", str(e.home))
    monkeypatch.setattr(act, "_ROLLOUT_SCAN", {})
    codex_state._cache.clear()
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    e.bindir = bindir
    # 假 codex：只认 delete --force <UUID>，按 mode 文件决定删多少；只改 $HOME/.codex 下的两个库
    script = bindir / "codex"
    script.write_text(f"""#!{sys.executable}
import os, sqlite3, sys
here = os.path.dirname(os.path.abspath(__file__))
mode = open(os.path.join(here, "mode")).read().strip() if os.path.exists(os.path.join(here, "mode")) else "normal"
with open(os.path.join(here, "calls.log"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
assert sys.argv[1:3] == ["delete", "--force"], sys.argv
tid = sys.argv[3]
base = os.path.join(os.environ["HOME"], ".codex")
st = sqlite3.connect(os.path.join(base, "state_5.sqlite"))
kids = {{}}
for p, c in st.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"):
    kids.setdefault(p, []).append(c)
tree, stack = [], [tid]
while stack:
    t = stack.pop()
    tree.append(t)
    stack.extend(kids.get(t, []))
if mode == "noop":
    sys.exit(0)
if mode == "root_only":                    # 只删根线程自己的记录，后代的记录原样留着
    tree = [tid]
if mode != "history_only":                 # history_only：只删历史库，状态库原样留着
    for t in tree:
        st.execute("DELETE FROM thread_spawn_edges WHERE parent_thread_id=? OR child_thread_id=?", (t, t))
        st.execute("DELETE FROM threads WHERE id=?", (t,))
    st.commit()
st.close()
if mode != "state_only":                   # state_only：只删状态库，历史库原样留着
    h = sqlite3.connect(os.path.join(base, "thread_history_1.sqlite"))
    for t in tree:
        h.execute("DELETE FROM thread_items WHERE thread_id=?", (t,))
    h.commit()
    h.close()
if mode == "images":                       # 连带删原图目录（真实 codex delete 会不会这么做没有验证过）
    import shutil
    for t in tree:
        shutil.rmtree(os.path.join(base, "generated_images", t), ignore_errors=True)
""", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(protect, "NATIVE_FIXED_PATH", ())          # 不让 /opt/homebrew/bin 里真实的 codex 被解析到
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    return e


def set_mode(env, mode):
    (env.bindir / "mode").write_text(mode)


def calls(env):
    p = env.bindir / "calls.log"
    return p.read_text().splitlines() if p.exists() else []


def row(tid, source="cli", rollout_path="", updated=OLD_TS, title=None, cwd="/work/x", created=None):
    return {"id": tid, "source": source, "rollout_path": rollout_path, "updated_at": updated,
            "title": title or f"线程 {tid[-2:]}", "cwd": cwd, "created_at": created or updated - 3600}


def make_dbs(env, threads, edges=(), history=None, payload=2000):
    """造 ~/.codex/state_5.sqlite 与 thread_history_1.sqlite（WAL + incremental，与真实库同形）。"""
    base = env.home / ".codex"
    base.mkdir(parents=True, exist_ok=True)
    st = sqlite3.connect(base / "state_5.sqlite")
    st.execute("PRAGMA journal_mode=WAL")
    st.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, cwd TEXT, title TEXT, "
               "created_at INTEGER, updated_at INTEGER, archived INTEGER DEFAULT 0, first_user_message TEXT)")
    st.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT)")
    for t in threads:
        st.execute("INSERT INTO threads (id, rollout_path, source, cwd, title, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                   (t["id"], t["rollout_path"], t["source"], t["cwd"], t["title"], t["created_at"], t["updated_at"]))
    for p, c in edges:
        st.execute("INSERT INTO thread_spawn_edges VALUES (?,?,?)", (p, c, "closed"))
    st.commit()
    st.close()
    h = sqlite3.connect(base / "thread_history_1.sqlite")
    h.execute("PRAGMA auto_vacuum=INCREMENTAL")
    h.execute("VACUUM")
    h.execute("PRAGMA journal_mode=WAL")
    h.execute("CREATE TABLE thread_items (thread_id TEXT, item_json TEXT)")
    h.execute("CREATE INDEX idx_items_thread ON thread_items(thread_id)")
    for tid, n in (history or {}).items():
        h.executemany("INSERT INTO thread_items VALUES (?, ?)", [(tid, json.dumps("x" * (payload - 2)))] * n)
    h.commit()
    h.close()
    codex_state._cache.clear()


def db_has(env, tid):
    st = sqlite3.connect(env.home / ".codex" / "state_5.sqlite")
    h = sqlite3.connect(env.home / ".codex" / "thread_history_1.sqlite")
    try:
        return (st.execute("SELECT COUNT(*) FROM threads WHERE id=?", (tid,)).fetchone()[0],
                h.execute("SELECT COUNT(*) FROM thread_items WHERE thread_id=?", (tid,)).fetchone()[0])
    finally:
        st.close()
        h.close()


def history_rows(env, tid):
    h = sqlite3.connect(env.home / ".codex" / "thread_history_1.sqlite")
    try:
        return h.execute("SELECT COUNT(*) FROM thread_items WHERE thread_id=?", (tid,)).fetchone()[0]
    finally:
        h.close()


def add_history(env, tid, n):
    h = sqlite3.connect(env.home / ".codex" / "thread_history_1.sqlite")
    h.executemany("INSERT INTO thread_items VALUES (?, ?)", [(tid, json.dumps("y" * 98))] * n)
    h.commit()
    h.close()


def item_for(env, tid, cls=None):
    """用探针自己的函数造条目（identity 取库里当前的值）。"""
    rows, _ = codex_state.read_threads(str(env.home))
    hist, errs = codex_state.history_counts(str(env.home), [tid], with_bytes=True)
    assert not errs
    return scan.history_item(rows[tid][0], hist, cls or "interactive")


def dcard(cid, items):
    return card(cid, items, verdict="decide", question="这个对话不再需要了吗？")


def verdicts(env):
    return {c["id"]: c for c in env.check()["cards"]}


def base_interactive(env):
    """R1（cli，有一个子任务 C1，都没有会话文件）+ 历史库里各有若干条目。"""
    rp = str(env.home / ".codex" / "sessions" / "2026" / "10" / "03" / f"rollout-2026-10-03T00-00-00-{R1}.jsonl")
    make_dbs(env, [row(R1, "cli", rp), row(C1, '{"subagent":{"thread_spawn":{"parent_thread_id":"%s"}}}' % R1)],
             edges=[(R1, C1)], history={R1: 30, C1: 10})
    return rp


# ---------------------------------------------------------------- 正常删除

def test_normal_delete_removes_both_dbs_and_accounts_as_db_records(env):
    base_interactive(env)
    before = (env.home / ".codex" / "thread_history_1.sqlite").stat().st_size
    env.write([], [dcard("c01", [item_for(env, R1)])])
    res = verdicts(env)["c01"]
    assert res["verdict_out"] == "decide" and not res["errors"]
    assert env.act("c01", answers=["c01=删"]) == 0
    rc = env.receipt()
    it = rc["cards"][0]["items"][0]
    assert it["status"] == "removed", it
    assert calls(env) == [f"delete --force {R1}"]
    assert db_has(env, R1) == (0, 0) and db_has(env, C1) == (0, 0)
    s = rc["summary"]
    # 库内条目单列，不算直接删除的文件
    assert s["db_threads"] == 2 and s["db_history_items"] == 30
    assert s["db_history_bytes"] == 30 * 2000
    assert s["bytes_by_files"] == 0 and s["bytes_native"] == 0 and s["bytes_freed_now"] == 0
    assert it["removed"]["bytes"] == 0 and it["removed"]["files"] == 0
    # 有这类条目成功就触发历史库回收；真正腾出的空间由前后文件大小实测
    [rec] = rc["history_reclaim"]
    assert rec["size_before"] == before and rec["reclaimed"] > 0 and s["history_reclaimed"] == rec["reclaimed"]
    assert s["freed_by_items"] == rec["reclaimed"]


def test_dry_run_changes_nothing(env):
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    assert env.act("c01", answers=["c01=删"], dry=True) == 0
    it = json.loads((env.run / "receipt-dry-run.json").read_text())["cards"][0]["items"][0] \
        if (env.run / "receipt-dry-run.json").exists() else None
    assert calls(env) == [] and db_has(env, R1) == (1, 30)
    if it:
        assert it["status"] == "planned"


# ---------------------------------------------------------------- 执行前复核

def test_file_reappears_after_seal_is_skipped(env):
    rp = base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    digest = env.seal()
    mkfile(rp, b"{}")                             # 封存后会话文件又出现了
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "skipped" and "会话文件又出现了" in it["reason"]
    assert calls(env) == [] and db_has(env, R1) == (1, 30)


def test_file_reappears_elsewhere_by_uuid_is_skipped(env):
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    digest = env.seal()
    other = rollout(env, R1, day="05")            # 同一线程号的会话文件出现在别的日期目录（不是库里记录的路径）
    assert str(other) != item_for(env, R1)["identity"]["rollout_path"]
    assert not os.path.lexists(item_for(env, R1)["identity"]["rollout_path"])
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "skipped" and "会话文件又出现了" in it["reason"]
    assert calls(env) == [] and db_has(env, R1) == (1, 30)


def test_history_grew_after_seal_is_skipped(env):
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    digest = env.seal()
    add_history(env, R1, 5)                       # 封存后历史库里多了 5 条
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "skipped" and "历史条目比封存时多" in it["reason"]
    assert calls(env) == [] and db_has(env, R1) == (1, 35)


def test_state_updated_after_seal_is_skipped(env):
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    digest = env.seal()
    st = sqlite3.connect(env.home / ".codex" / "state_5.sqlite")
    st.execute("UPDATE threads SET updated_at=updated_at+5 WHERE id=?", (R1,))
    st.commit()
    st.close()
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "skipped" and "updated_at" in it["reason"] and calls(env) == []


def test_live_descendant_appearing_after_seal_blocks_parent(env):
    """封存时后代的会话文件都不在；封存后子会话的文件出现了：act 现场再查一次，不删父线程（删父会连带删子）。"""
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    digest = env.seal()
    rollout(env, C1, parent=R1)
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "skipped" and "子会话" in it["reason"] and C1 in it["reason"]
    assert calls(env) == [] and db_has(env, R1)[0] == 1


# ---------------------------------------------------------------- 执行后复核

def test_state_only_delete_is_reported_as_incomplete(env):
    base_interactive(env)
    set_mode(env, "state_only")                   # 假 codex 只删了 state，历史库里还有行
    env.write([], [dcard("c01", [item_for(env, R1)])])
    assert env.act("c01", answers=["c01=删"]) == 0
    rc = env.receipt()
    it = rc["cards"][0]["items"][0]
    assert it["status"] == "failed" and "删除不完整" in it["failed"][0]["error"]
    assert "历史库" in it["failed"][0]["error"]
    assert db_has(env, R1) == (0, 30)
    assert rc["summary"]["failed_items"] == 1 and rc["summary"]["db_threads"] == 0
    assert "history_reclaim" not in rc            # 没成功的不触发回收


def test_command_exit_zero_but_nothing_deleted_is_failure(env):
    base_interactive(env)
    set_mode(env, "noop")
    env.write([], [dcard("c01", [item_for(env, R1)])])
    assert env.act("c01", answers=["c01=删"]) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "failed" and "删除不完整" in it["failed"][0]["error"]


# ---------------------------------------------------------------- check 的拒绝

def test_check_rejects_argv_thread_not_matching_identity(env):
    base_interactive(env)
    it = item_for(env, R1)
    bad = dict(it, argv=["codex", "delete", "--force", C1])
    env.write([], [dcard("c01", [bad]), dcard("c02", [item_for(env, R1)])])
    res = verdicts(env)
    assert res["c01"]["verdict_out"] == "keep" and any("identity.thread_id" in e for e in res["c01"]["errors"])
    assert res["c02"]["verdict_out"] == "decide"


def test_check_rejects_forged_or_unknown_identity(env):
    base_interactive(env)
    good = item_for(env, R1)
    extra = dict(good, identity=dict(good["identity"], note="x"))
    other_kind = dict(good, identity=dict(good["identity"], kind="whatever"))
    short = dict(good, identity={k: v for k, v in good["identity"].items() if k != "history_items"})
    wrong_path = dict(good, path=str(env.home / ".codex" / "sessions" / "elsewhere"))
    not_native = dict(good, method="delete", scope="all")
    env.write([], [dcard("c01", [extra]), dcard("c02", [other_kind]), dcard("c03", [short]),
                   dcard("c04", [wrong_path]), dcard("c05", [not_native])])
    res = verdicts(env)
    assert {k: v["verdict_out"] for k, v in res.items()} == {f"c0{i}": "keep" for i in range(1, 6)}


def test_check_rejects_state_mismatches(env):
    rp = base_interactive(env)
    good = item_for(env, R1)
    missing = item_for(env, R1)
    missing["identity"] = dict(missing["identity"], thread_id=R2)
    missing["argv"] = ["codex", "delete", "--force", R2]
    missing["path"] = codex_state.expected_item_path(str(env.home), R2, "")
    moved = dict(good, identity=dict(good["identity"], rollout_path=rp + ".moved"))
    moved["path"] = codex_state.expected_item_path(str(env.home), R1, rp + ".moved")
    stale = dict(good, identity=dict(good["identity"], state_updated_at=OLD_TS - 1))
    env.write([], [dcard("c01", [missing]), dcard("c02", [moved]), dcard("c03", [stale]), dcard("c04", [good])])
    res = verdicts(env)
    assert res["c01"]["verdict_out"] == "keep" and any("状态库里没有" in e for e in res["c01"]["errors"])
    assert res["c02"]["verdict_out"] == "keep" and any("rollout_path" in e for e in res["c02"]["errors"])
    assert res["c03"]["verdict_out"] == "keep" and any("updated_at" in e for e in res["c03"]["errors"])
    assert res["c04"]["verdict_out"] == "decide"


def test_check_rejects_when_file_exists_or_recently_updated(env):
    rp = base_interactive(env)
    good = item_for(env, R1)
    mkfile(rp, b"{}")
    env.write([], [dcard("c01", [good])])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and any("会话文件现在存在" in e for e in r["errors"])
    os.remove(rp)
    st = sqlite3.connect(env.home / ".codex" / "state_5.sqlite")
    st.execute("UPDATE threads SET updated_at=? WHERE id=?", (int(time.time()) - 600, C1))   # 子任务刚被续聊过
    st.commit()
    st.close()
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and any("24 小时" in e for e in r["errors"])


def test_check_rejects_child_thread_item(env):
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, C1)])])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and any("子任务" in e for e in r["errors"])


def test_check_rejects_protected_path(env):
    base_interactive(env)
    p = Path(protect.PROFILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"protect": [str(env.home / ".codex" / "sessions")]}), encoding="utf-8")
    env.write([], [dcard("c01", [item_for(env, R1)])])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and any("保护" in e for e in r["errors"])


def test_act_rejects_forged_seal_protected_path(env):
    """绕过 check 伪造封存：act 端自己也要拦保护路径。"""
    base_interactive(env)
    p = Path(protect.PROFILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"protect": [str(env.home / ".codex" / "sessions")]}), encoding="utf-8")
    it = item_for(env, R1)
    rec = dict(it, realpath=os.path.realpath(it["path"]), prog=protect.resolve_prog("codex"), bytes=1)
    seal = {"schema": "seal/1", "scan_id": "scan1", "created_at": "t",
            "cards": {"c01": {"title": "c01", "verdict": "decide", "items": [dict(rec, index=0)]}},
            "profile_guard": protect.profile_guard(plan.load_profile())}
    seal["digest"] = plan.digest_of(seal)
    (env.run / "seal.json").write_text(json.dumps(seal, ensure_ascii=False))
    assert env.act("c01", digest=seal["digest"], answers=["c01=删"]) == 0
    r = env.receipt()["cards"][0]["items"][0]
    assert r["status"] == "skipped" and "保护" in r["reason"] and calls(env) == []


# ---------------------------------------------------------------- 交互线程只能逐个确认

def test_interactive_thread_cannot_pose_as_batch_or_share_a_card(env):
    rp1 = base_interactive(env)
    h = sqlite3.connect(env.home / ".codex" / "thread_history_1.sqlite")
    h.close()
    st = sqlite3.connect(env.home / ".codex" / "state_5.sqlite")
    st.execute("INSERT INTO threads (id, rollout_path, source, cwd, title, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
               (R2, "", "vscode", "/w", "另一个对话", OLD_TS, OLD_TS))
    st.commit()
    st.close()
    posing = item_for(env, R1, cls="batch")                       # 交互线程冒充批量
    items_dir = env.run / "items"
    items_dir.mkdir()
    (items_dir / "bulk.json").write_text(json.dumps([posing]))
    two = [item_for(env, R1), item_for(env, R2)]                  # 两个交互对话塞进同一张卡
    clean = card("c03", [item_for(env, R1)])                       # 交互线程写成 clean 批量卡
    bulk = card("c01", [])
    bulk["items_file"] = ["items/bulk.json"]
    env.write([], [bulk, dcard("c02", two), clean])
    res = verdicts(env)
    assert res["c01"]["verdict_out"] == "keep" and any("交互对话" in e and "batch" in e for e in res["c01"]["errors"])
    assert res["c02"]["verdict_out"] == "keep" and any("最多一个交互会话" in e for e in res["c02"]["errors"])
    assert res["c03"]["verdict_out"] == "decide" and res["c03"]["downgraded"]       # clean 被降为 decide，逐个确认
    seal = json.loads(env.run.joinpath("plan.check.json").read_text())
    assert seal["cards"][2]["default_select"] is False
    digest = env.seal()
    sealed = json.loads((env.run / "seal.json").read_text())
    assert sealed["cards"]["c03"]["interactive"] is True and "c01" not in sealed["cards"] and "c02" not in sealed["cards"]
    assert digest


# ---------------------------------------------------------------- 探针与批量组

def test_probe_groups_exec_together_and_interactive_one_by_one_and_emits(env, tmp_path):
    make_dbs(env, [row(E1, "exec"), row(E2, "exec", cwd="/work/y"), row(E3, "exec"),
                   row(R1, "cli", title="修一个 bug"), row(C1, "subagent"), row(R2, "vscode", title=""),
                   row("00000000-0000-7000-8000-0000000000d1", "cli")],
             edges=[(R1, C1)], history={E1: 4, E2: 6, E3: 2, R1: 8, C1: 3, R2: 1})
    # R2 没有 title：取 first_user_message 开头（本库没有这个值时为空）；d1 的会话文件还在，所以不在候选里
    alive = rollout(env, "00000000-0000-7000-8000-0000000000d1")
    st = sqlite3.connect(env.home / ".codex" / "state_5.sqlite")
    st.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(alive), "00000000-0000-7000-8000-0000000000d1"))
    st.commit()
    st.close()
    out_dir = tmp_path / "emit"
    class O:
        emit = str(out_dir)
    r = scan.probe_codex_history(O)
    kinds = sorted(g["kind"] for g in r["groups"])
    assert kinds == ["batch", "interactive", "interactive"]
    batch = next(g for g in r["groups"] if g["kind"] == "batch")
    assert batch["roots"] == 3 and batch["history_items"] == 12 and batch["history_bytes"] == 12 * 2000
    inter = {g["thread_id"]: g for g in r["groups"] if g["kind"] == "interactive"}
    assert set(inter) == {R1, R2}
    assert inter[R1]["title"] == "修一个 bug" and inter[R1]["subtasks"] == 1
    assert inter[R1]["history_bytes"] == 8 * 2000 and inter[R1]["tree_history_bytes"] == 11 * 2000
    assert inter[R1]["cwd"] == "/work/x" and inter[R1]["created_at"] and inter[R1]["updated_at"]
    assert r["summary"]["files_missing"] == 6            # 7 个线程里只有 d1 的会话文件在
    files = sorted(os.listdir(out_dir))
    assert files == ["codex-history-batch.json", "codex-history-manifest.json",
                     f"codex-history-thread-{R1}.json", f"codex-history-thread-{R2}.json"]
    batch_items = json.loads((out_dir / "codex-history-batch.json").read_text())
    assert [i["identity"]["thread_id"] for i in batch_items] == sorted([E1, E2, E3])
    assert all(i["class"] == "batch" and i["argv"] == ["codex", "delete", "--force", i["identity"]["thread_id"]]
               for i in batch_items)
    # 再次 emit 先清掉旧文件
    (out_dir / "codex-history-thread-stale.json").write_text("[]")
    scan.probe_codex_history(O)
    assert "codex-history-thread-stale.json" not in os.listdir(out_dir)

    # 批量组条目能被 items_file 引用并通过 check（clean），执行后三个线程的记录都清掉
    items_dir = env.run / "items"
    items_dir.mkdir()
    (items_dir / "codex-history-batch.json").write_text((out_dir / "codex-history-batch.json").read_text())
    bulk = card("c01", [])
    bulk["items_file"] = ["items/codex-history-batch.json"]
    env.write([], [bulk])
    res = verdicts(env)["c01"]
    assert res["verdict_out"] == "clean" and not res["errors"], res
    assert env.act("c01") == 0
    rc = env.receipt()
    assert [i["status"] for i in rc["cards"][0]["items"]] == ["removed"] * 3
    assert all(db_has(env, t) == (0, 0) for t in (E1, E2, E3))
    assert db_has(env, R1) == (1, 8) and rc["summary"]["db_threads"] == 3 and rc["summary"]["bytes_by_files"] == 0
    assert "history_reclaim" in rc


def test_probe_holds_roots_with_live_descendants_or_recent_updates(env):
    make_dbs(env, [row(R1, "cli"), row(C1, "subagent"), row(R2, "cli", updated=int(time.time()) - 60)],
             edges=[(R1, C1)], history={R1: 2})
    alive = rollout(env, C1, parent=R1)
    st = sqlite3.connect(env.home / ".codex" / "state_5.sqlite")
    st.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(alive), C1))
    st.commit()
    st.close()
    class O:
        emit = None
    r = scan.probe_codex_history(O)
    assert r["groups"] == []
    held = {h["id"]: h["reasons"] for h in r["held"]}
    assert set(held) == {R1, R2}
    assert "后代" in held[R1][0] and "24 小时" in held[R2][0]


def test_probe_reports_unreadable_state_db(env):
    base = env.home / ".codex"
    base.mkdir(parents=True)
    (base / "state_5.sqlite").write_bytes(b"not a database" * 100)
    class O:
        emit = None
    r = scan.probe_codex_history(O)
    assert r["groups"] == [] and r["errors"]


# ---------------------------------------------------------------- 原有带文件的条目不受影响

def test_file_backed_native_item_unaffected_by_identity_branch(env):
    f = rollout(env, R1)
    age(env.home)
    it = {"path": str(f), "method": "native", "argv": ["codex", "delete", "--force", R1], "session": R1}
    make_dbs(env, [row(R1, "cli", str(f))], history={R1: 3})
    env.write([node(env.home / ".codex")], [dcard("c01", [it])])
    res = verdicts(env)["c01"]
    assert res["verdict_out"] == "decide" and not res["errors"]
    sealed_item = None
    env.seal()
    sealed_item = json.loads((env.run / "seal.json").read_text())["cards"]["c01"]["items"][0]
    assert "identity" not in sealed_item and "ino" in sealed_item


# ---------------------------------------------------------------- 试点

def test_pilot_claim_is_checked_against_the_database(env):
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    digest = env.seal()
    seal = json.loads((env.run / "seal.json").read_text())
    assert act.main(plan_auto_sel(["run", "--run", str(env.run), "--seal", digest, "--cards", "c01",
                                   "--answers", "c01=删", "--pilot"])) == 0
    [rcp] = list(env.run.glob("receipt-pilot-*.json"))
    rc = json.loads(rcp.read_text())
    assert rc["cards"][0]["items"][0]["status"] == "removed" and db_has(env, R1) == (0, 0)
    assert act.pilot_receipt_verified(rc, seal) is True
    # 回执声称已删，但库里这个线程其实还在（手写的回执）：不认
    make_dbs_again(env)
    assert act.pilot_receipt_verified(rc, seal) is False


def plan_auto_sel(argv):
    from test_plan_act import auto_sel
    return auto_sel(argv)


def make_dbs_again(env):
    for n in ("state_5.sqlite", "thread_history_1.sqlite"):
        for suffix in ("", "-wal", "-shm"):
            f = env.home / ".codex" / (n + suffix)
            if f.exists():
                f.unlink()
    base_interactive(env)


# ================================================================ 评审补充：执行侧每项检查各有一个直接守着它的用例
# 下面这些用例都用“伪造封存”或“封存后改库”绕过 plan check，直接测 act.py 自己的复核；
# 变异验证见汇报：把 verify_threadless / run_native_threadless 里对应的一项改坏，至少一个用例失败。

def forge_seal(env, item, **over):
    """绕过 plan check 伪造封存（带 identity 的条目）。over 覆盖封存记录里的字段。返回 digest。"""
    rec = dict(item, realpath=os.path.realpath(item["path"]), prog=protect.resolve_prog("codex"), bytes=1, index=0)
    rec.update(over)
    seal = {"schema": "seal/1", "scan_id": "scan1", "created_at": "t",
            "cards": {"c01": {"title": "c01", "verdict": "decide", "items": [rec]}},
            "profile_guard": protect.profile_guard(plan.load_profile())}
    seal["digest"] = plan.digest_of(seal)
    (env.run / "seal.json").write_text(json.dumps(seal, ensure_ascii=False))
    return seal["digest"]


def run_forged(env, item, **over):
    digest = forge_seal(env, item, **over)
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    return env.receipt()["cards"][0]["items"][0]


def sql(env, db, stmt, args=()):
    con = sqlite3.connect(env.home / ".codex" / db)
    try:
        con.execute(stmt, args)
        con.commit()
    finally:
        con.close()


def garble(env, db):
    """把一个库换成不是数据库的内容（连同 -wal / -shm 一起清掉），模拟读取失败。"""
    for suffix in ("", "-wal", "-shm"):
        f = env.home / ".codex" / (db + suffix)
        if f.exists():
            f.unlink()
    (env.home / ".codex" / db).write_bytes(b"not a database" * 200)
    codex_state._cache.clear()


def assert_skipped(env, it, *words):
    assert it["status"] == "skipped", it
    for w in words:
        assert w in it["reason"], it["reason"]
    assert calls(env) == [], "不该执行任何 codex 命令"
    assert db_has(env, R1)[0] in (0, 1)


def test_act_rejects_argv_thread_not_matching_identity(env):
    base_interactive(env)
    it = item_for(env, R1)
    it["argv"] = ["codex", "delete", "--force", C1]        # 命令要删 C1，identity 说的是 R1
    r = run_forged(env, it)
    assert_skipped(env, r, "不一致")
    assert db_has(env, R1) == (1, 30) and db_has(env, C1) == (1, 10)


def test_act_rejects_path_not_derived_from_identity(env):
    base_interactive(env)
    it = item_for(env, R1)
    it["path"] = str(env.home / ".codex" / "sessions" / ".no-rollout" / "someone-else")
    assert_skipped(env, run_forged(env, it), "路径与封存身份不符")


def test_act_rejects_realpath_changed(env):
    base_interactive(env)
    r = run_forged(env, item_for(env, R1), realpath=str(env.home / "somewhere-else"))
    assert_skipped(env, r, "路径解析结果已变化")


def test_act_rejects_untrusted_prog(env):
    base_interactive(env)
    assert_skipped(env, run_forged(env, item_for(env, R1), prog="/usr/bin/false"), "程序路径")


def test_act_rejects_malformed_identity(env):
    base_interactive(env)
    it = item_for(env, R1)
    it["identity"] = dict(it["identity"], note="夹带")
    assert_skipped(env, run_forged(env, it), "identity")


def test_act_rejects_child_thread_even_with_forged_seal(env):
    """伪造封存把子任务当根线程：act 自己再查父子边，不单独删子任务。"""
    base_interactive(env)
    r = run_forged(env, item_for(env, C1))
    assert_skipped(env, r, "子任务")
    assert db_has(env, C1) == (1, 10)


def test_act_rejects_root_updated_within_24h_with_forged_seal(env):
    """根线程自己 24 小时内有更新（封存里的 updated_at 与库一致，所以别的检查都过）。"""
    base_interactive(env)
    sql(env, "state_5.sqlite", "UPDATE threads SET updated_at=? WHERE id=?", (int(time.time()) - 600, R1))
    r = run_forged(env, item_for(env, R1))
    assert_skipped(env, r, "24 小时", R1)
    assert db_has(env, R1) == (1, 30)


def test_act_rejects_descendant_updated_within_24h_after_seal(env):
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    digest = env.seal()
    sql(env, "state_5.sqlite", "UPDATE threads SET updated_at=? WHERE id=?", (int(time.time()) - 600, C1))
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    assert_skipped(env, env.receipt()["cards"][0]["items"][0], "24 小时", C1)
    assert db_has(env, R1) == (1, 30)


def test_act_rejects_descendant_with_unreadable_updated_at(env):
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    digest = env.seal()
    sql(env, "state_5.sqlite", "UPDATE threads SET updated_at=? WHERE id=?", ("不是时间", C1))
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    assert_skipped(env, env.receipt()["cards"][0]["items"][0], "无法判断", C1)


def test_act_rejects_rollout_path_changed_after_seal(env):
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    digest = env.seal()
    sql(env, "state_5.sqlite", "UPDATE threads SET rollout_path=? WHERE id=?", ("/nonexistent/moved.jsonl", R1))
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    assert_skipped(env, env.receipt()["cards"][0]["items"][0], "rollout_path")
    assert db_has(env, R1) == (1, 30)


def test_act_rejects_thread_in_two_state_dbs(env):
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    digest = env.seal()
    con = sqlite3.connect(env.home / ".codex" / "state_6.sqlite")
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
    con.execute("INSERT INTO threads VALUES (?, '', ?)", (R1, OLD_TS))
    con.commit()
    con.close()
    codex_state._cache.clear()
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    assert_skipped(env, env.receipt()["cards"][0]["items"][0], "2 个状态库")


def test_act_rejects_when_thread_gone_from_state(env):
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    digest = env.seal()
    sql(env, "state_5.sqlite", "DELETE FROM threads WHERE id=?", (R1,))
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    r = env.receipt()["cards"][0]["items"][0]
    assert r["status"] == "skipped" and "已经没有这个线程" in r["reason"] and calls(env) == []


def test_act_rejects_unreadable_state_db(env):
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    digest = env.seal()
    garble(env, "state_5.sqlite")
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    r = env.receipt()["cards"][0]["items"][0]
    assert r["status"] == "skipped" and "状态库读取失败" in r["reason"] and calls(env) == []


def test_act_rejects_unreadable_history_db(env):
    base_interactive(env)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    digest = env.seal()
    garble(env, "thread_history_1.sqlite")
    assert env.act("c01", digest=digest, answers=["c01=删"]) == 0
    r = env.receipt()["cards"][0]["items"][0]
    assert r["status"] == "skipped" and "历史库读取失败" in r["reason"] and calls(env) == []


# ---- 执行后复核：状态库、历史库、整棵树各自单独守着

def test_history_only_delete_is_incomplete_on_state_side_alone(env):
    """假 codex 只删历史库、不删状态库：只有“状态库里还有”这一项复核能抓到。"""
    base_interactive(env)
    set_mode(env, "history_only")
    env.write([], [dcard("c01", [item_for(env, R1)])])
    assert env.act("c01", answers=["c01=删"]) == 0
    it = env.receipt()["cards"][0]["items"][0]
    err = it["failed"][0]["error"]
    assert it["status"] == "failed" and "删除不完整" in err
    assert "状态库里还有" in err and "历史库里还有" not in err
    assert db_has(env, R1) == (1, 0)


def test_state_only_delete_reports_history_side_alone(env):
    base_interactive(env)
    set_mode(env, "state_only")
    env.write([], [dcard("c01", [item_for(env, R1)])])
    assert env.act("c01", answers=["c01=删"]) == 0
    err = env.receipt()["cards"][0]["items"][0]["failed"][0]["error"]
    assert "历史库里还有" in err and "状态库里还有" not in err


def test_delete_that_leaves_descendants_is_incomplete(env):
    """假 codex 只删了根线程，后代的记录还在：复核要覆盖整棵树，不只看根。"""
    base_interactive(env)
    set_mode(env, "root_only")
    env.write([], [dcard("c01", [item_for(env, R1)])])
    assert env.act("c01", answers=["c01=删"]) == 0
    it = env.receipt()["cards"][0]["items"][0]
    assert it["status"] == "failed" and "删除不完整" in it["failed"][0]["error"]
    assert C1 in it["failed"][0]["error"]
    assert db_has(env, R1) == (0, 0) and db_has(env, C1) == (1, 10)


# ---- plan check 补充：库内条目数现场核对、后代会话文件还在

def test_check_does_not_trust_history_numbers_in_identity(env):
    base_interactive(env)
    good = item_for(env, R1)
    zero_items = dict(good, identity=dict(good["identity"], history_items=0, history_bytes=0))
    wrong_bytes = dict(good, identity=dict(good["identity"], history_bytes=good["identity"]["history_bytes"] + 1))
    fewer = dict(good, identity=dict(good["identity"], history_items=good["identity"]["history_items"] - 1))
    env.write([], [dcard("c01", [zero_items]), dcard("c02", [wrong_bytes]), dcard("c03", [fewer]), dcard("c04", [good])])
    res = verdicts(env)
    for cid in ("c01", "c02", "c03"):
        assert res[cid]["verdict_out"] == "keep" and any("库内条目" in e for e in res[cid]["errors"]), res[cid]
    assert res["c04"]["verdict_out"] == "decide"


def test_check_fails_when_history_db_unreadable(env):
    base_interactive(env)
    good = item_for(env, R1)
    garble(env, "thread_history_1.sqlite")
    env.write([], [dcard("c01", [good])])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and any("历史库读取失败" in e for e in r["errors"])


def test_check_rejects_live_descendant_file_even_if_listed_in_same_card(env):
    """后代的会话文件还在：整卡降级（删根会连带删掉它），哪怕把它也列进同一张卡里。"""
    base_interactive(env)
    child_file = rollout(env, C1, parent=R1)
    age(env.home)
    child_item = {"path": str(child_file), "method": "native", "argv": ["codex", "delete", "--force", C1], "session": R1}
    env.write([node(env.home / ".codex")], [dcard("c01", [item_for(env, R1)]),
                                           dcard("c02", [child_item, item_for(env, R1)])])
    res = verdicts(env)
    for cid in ("c01", "c02"):
        assert res[cid]["verdict_out"] == "keep" and any("子会话" in e and C1 in e for e in res[cid]["errors"]), res[cid]


def test_check_finds_file_by_uuid_in_other_directory(env):
    base_interactive(env)
    good = item_for(env, R1)
    rollout(env, R1, day="05")
    env.write([], [dcard("c01", [good])])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and any("按线程号" in e for e in r["errors"])


# ---- source 精确匹配：只有 "exec" 进批量组

@pytest.mark.parametrize("source", ["exec-xxx", "exec ", "Exec", 'cli_exec_runner',
                                    '{"subagent":{"thread_spawn":{"agent_path":"exec/worker","parent_thread_id":"x"}}}'])
def test_exec_source_is_exact(source):
    assert codex_state.is_exec_source("exec") is True
    assert codex_state.is_exec_source(source) is False
    assert codex_state.is_exec_source(None) is False and codex_state.is_exec_source(5) is False


def test_check_rejects_fake_exec_sources_posing_as_batch(env):
    sub = '{"subagent":{"thread_spawn":{"agent_path":"exec/worker","parent_thread_id":"%s"}}}' % R1
    make_dbs(env, [row(R1, "exec-xxx"), row(R2, sub)], history={R1: 3, R2: 2})
    items_dir = env.run / "items"
    items_dir.mkdir()
    cards = []
    for n, tid in enumerate((R1, R2), 1):
        (items_dir / f"b{n}.json").write_text(json.dumps([item_for(env, tid, cls="batch")]))
        c = card(f"c0{n}", [])
        c["items_file"] = [f"items/b{n}.json"]
        cards.append(c)
    env.write([], cards)
    res = verdicts(env)
    for cid in ("c01", "c02"):
        assert res[cid]["verdict_out"] == "keep" and any("不是 exec" in e and "batch" in e for e in res[cid]["errors"]), res[cid]


def test_probe_keeps_fake_exec_sources_out_of_batch_group(env):
    sub = '{"subagent":{"thread_spawn":{"agent_path":"exec/worker"}}}'
    make_dbs(env, [row(E1, "exec"), row(E2, "exec-xxx"), row(E3, sub)], history={E1: 1, E2: 2, E3: 3})

    class O:
        emit = None
    r = scan.probe_codex_history(O)
    batch = next(g for g in r["groups"] if g["kind"] == "batch")
    assert batch["roots"] == 1                                       # 只有 source 精确等于 exec 的 E1
    assert [g["thread_id"] for g in r["groups"] if g["kind"] == "interactive"] == [E2]   # exec-xxx 当交互对话
    held = {h["id"]: h["reasons"] for h in r["held"]}
    assert set(held) == {E3} and "JSON" in held[E3][0]               # 子任务 JSON 不进批量、也不单独列出


# ---- 探针：历史库按线程号短查询、读失败不出条目、北京时间

def test_probe_queries_history_per_thread_with_short_connections(env, tmp_path, monkeypatch):
    make_dbs(env, [row(E1, "exec"), row(E2, "exec"), row(R1, "cli")], history={E1: 4, E2: 6, R1: 8})
    seen, state = [], {"open": 0, "max": 0}

    class Spy(sqlite3.Connection):
        def close(self):
            state["open"] -= 1
            super().close()

    def spy_connect(db):
        con = sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True, timeout=5, factory=Spy)
        state["open"] += 1
        state["max"] = max(state["max"], state["open"])
        con.set_trace_callback(seen.append)
        return con

    monkeypatch.setattr(codex_state, "_ro_connect", spy_connect)

    class O:
        emit = None
    r = scan.probe_codex_history(O)
    assert next(g for g in r["groups"] if g["kind"] == "batch")["history_items"] == 10
    item_sql = [q for q in seen if "thread_items" in q and "PRAGMA" not in q]
    assert item_sql and all("WHERE thread_id IN" in q for q in item_sql), item_sql      # 没有整库扫描
    assert not any("GROUP BY" in q for q in seen)
    assert state["open"] == 0 and state["max"] == 1                                       # 连接用完即关，同时只开一个


def test_history_counts_batches_open_a_fresh_connection_each(env, monkeypatch):
    tids = [f"00000000-0000-7000-8000-{i:012x}" for i in range(7)]
    make_dbs(env, [row(t, "exec") for t in tids], history={t: 2 for t in tids})
    opened = []
    real = codex_state._ro_connect
    monkeypatch.setattr(codex_state, "_ro_connect", lambda db: (opened.append(db), real(db))[1])
    out, errs = codex_state.history_counts(str(env.home), tids, with_bytes=True, batch=3)
    assert not errs and all(v == (2, 2 * 2000) for v in out.values())
    assert len(opened) == 1 + 3                                   # 1 次看表结构 + ceil(7/3)=3 批


@pytest.mark.parametrize("broken", ["thread_history_1.sqlite", "state_5.sqlite"])
def test_probe_writes_no_items_when_a_db_is_unreadable(env, tmp_path, broken):
    make_dbs(env, [row(E1, "exec"), row(R1, "cli")], history={E1: 4, R1: 8})
    garble(env, broken)
    out_dir = tmp_path / "emit"

    class O:
        emit = str(out_dir)
    r = scan.probe_codex_history(O)
    assert r["errors"] and r["groups"] == [] and "emitted" not in r
    assert not out_dir.exists() or os.listdir(out_dir) == []        # 不输出 history_items 为 0 的条目


def test_beijing_time_for_humans_utc_for_storage(env, tmp_path, monkeypatch):
    assert codex_state.beijing_of(0) == "1970-01-01 08:00:00 北京时间"
    assert codex_state.beijing_of(None) is None
    # 没有 zoneinfo / 时区库时用固定 UTC+8
    monkeypatch.setitem(sys.modules, "zoneinfo", None)
    assert codex_state.beijing_of(0) == "1970-01-01 08:00:00 北京时间"
    monkeypatch.undo()
    ts = 1_790_000_000
    make_dbs(env, [row(R1, "cli", updated=ts, created=ts - 7200)], history={R1: 1})
    out_dir = tmp_path / "emit"
    monkeypatch.setattr(scan, "HOME", str(env.home))

    class O:
        emit = str(out_dir)
    r = scan.probe_codex_history(O)
    g = r["groups"][0]
    assert g["updated_at"].endswith("北京时间") and g["created_at"].endswith("北京时间")
    assert g["updated_at_utc"].endswith("Z") and g["updated_at_utc"] == codex_state.iso_of(ts)
    assert g["updated_at"] != g["updated_at_utc"]
    it = json.loads((out_dir / f"codex-history-thread-{R1}.json").read_text())[0]
    assert it["identity"]["state_updated_at"] == ts                 # 存储字段保持原值


# ---- 原图目录

def make_images(env, tid, name="a.png"):
    d = env.home / ".codex" / "generated_images" / tid
    mkfile(d / name, b"i" * 3000)
    return d


def emit_and_load(env, tmp_path):
    out_dir = tmp_path / "emit"

    class O:
        emit = str(out_dir)
    r = scan.probe_codex_history(O)
    items_dir = env.run / "items"
    items_dir.mkdir(exist_ok=True)
    for f in os.listdir(out_dir):
        (items_dir / f).write_text((out_dir / f).read_text())
    return r, out_dir


def image_card(tid):
    c = dcard("c01", [])
    c["items_file"] = [f"items/codex-history-thread-{tid}.json"]
    return c


def test_probe_lists_generated_images_of_whole_tree_after_thread_items(env, tmp_path):
    base_interactive(env)
    d1, d2 = make_images(env, R1), make_images(env, C1, "b.png")
    make_images(env, R2)                                              # 别的线程的原图目录不带上
    r, out_dir = emit_and_load(env, tmp_path)
    items = json.loads((out_dir / f"codex-history-thread-{R1}.json").read_text())
    assert [i["method"] for i in items] == ["native", "delete", "delete"]
    assert [i["path"] for i in items[1:]] == [str(d1), str(d2)]
    assert all(i["scope"] == "all" and i["class"] == "interactive" and i["session"] == R1 for i in items[1:])
    assert items[0]["session"] == R1 and items[0]["class"] == "interactive"
    g = r["groups"][0]
    assert g["image_dirs"] == 2 and g["image_bytes"] > 0
    assert g["card_bytes"] == g["tree_history_bytes"] + g["image_bytes"]          # 卡片体积计入原图
    assert r["summary"]["image_dirs_listed"] == 2


def test_batch_group_puts_all_thread_items_before_image_dirs(env, tmp_path):
    make_dbs(env, [row(E1, "exec"), row(E2, "exec")], history={E1: 2, E2: 2})
    d1, d2 = make_images(env, E1), make_images(env, E2)
    r, out_dir = emit_and_load(env, tmp_path)
    items = json.loads((out_dir / "codex-history-batch.json").read_text())
    assert [i["method"] for i in items] == ["native", "native", "delete", "delete"]
    assert all(i["class"] == "batch" for i in items) and items[2]["session"] == E1 and items[3]["session"] == E2
    assert r["groups"][0]["image_dirs"] == 2


def run_with_images(env, tmp_path, mode):
    base_interactive(env)
    d1, d2 = make_images(env, R1), make_images(env, C1, "b.png")
    age(env.home)
    r, out_dir = emit_and_load(env, tmp_path)
    # This case exercises the native command's accounting with an independently retained original.
    import hashlib
    retained = tmp_path / "retained.png"
    retained.write_bytes((d1 / "a.png").read_bytes())
    digest = hashlib.sha256(retained.read_bytes()).hexdigest()
    manifest = tmp_path / "retained.json"
    manifest.write_text(json.dumps({"copies": {digest: str(retained)}}))
    items_path = env.run / "items" / f"codex-history-thread-{R1}.json"
    items = json.loads(items_path.read_text())
    items[0]["session_materials"] = {"path": str(manifest), "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}
    items_path.write_text(json.dumps(items))
    set_mode(env, mode)
    env.write([node(env.home / ".codex")], [image_card(R1)])
    res = verdicts(env)["c01"]
    assert res["verdict_out"] == "decide" and not res["errors"], res            # 目录条目照常通过 plan check
    assert env.act("c01", answers=["c01=删"]) == 0
    return env.receipt()["cards"][0]["items"], (d1, d2)


def test_image_dirs_deleted_by_their_own_items_when_codex_leaves_them(env, tmp_path):
    its, (d1, d2) = run_with_images(env, tmp_path, "normal")
    assert [i["status"] for i in its] == ["removed", "removed", "removed"], its
    assert "generated_images_removed_by_codex" not in its[0] and "没有被 codex delete 删掉" in its[0]["reason"]
    assert not d1.exists() and not d2.exists()


def test_receipt_records_image_dirs_removed_by_codex_delete(env, tmp_path):
    its, (d1, d2) = run_with_images(env, tmp_path, "images")
    gone = its[0]["generated_images_removed_by_codex"]
    assert sorted(x["path"] for x in gone) == sorted([str(d1), str(d2)])
    assert all(x["bytes"] > 0 and x["files"] == 1 for x in gone)
    assert its[0]["status"] == "removed" and "连带删掉了 2 个原图目录" in its[0]["reason"]
    assert its[0]["removed"]["bytes"] == sum(x["bytes"] for x in gone)               # 真实删掉的字节如实计入
    assert [i["status"] for i in its[1:]] == ["skipped", "skipped"]                  # 目录条目：路径已不存在
    assert not d1.exists() and not d2.exists()


# ---------------------------------------------------------------- 项目归属：按状态库里的工作目录
# 会话文件已不在的线程，probe projects 收不到它的会话号（会话号只从现存会话文件里读），
# 只能按状态库记录的 cwd 归到项目；否则“进行中 / 别碰”的项目挡不住它们（2026-11-04 实测 15 个旧对话、465 条脚本残留漏挡）。

def write_ledger(env, proj, status, conversations=None):
    p = Path(protect.PROFILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    key = os.path.realpath(proj)
    entry = {"name": Path(proj).name, "status": status, "deliverables": [],
             "updated_at": "2026-10-11T00:00:00Z", "source": "board"}
    if conversations:
        entry["conversations"] = conversations
    p.write_text(json.dumps({"projects": {key: entry}}, ensure_ascii=False), encoding="utf-8")
    (env.run / "projects.json").write_text(json.dumps({
        "schema": "projects/1", "scan_id": "scan1",
        "projects": [{"key": key, "name": Path(proj).name, "session_ids": [], "claude_dirs": [], "roots": []}]}),
        encoding="utf-8")


def threadless_tree(env, cwd_root, cwd_child=None):
    rp = str(env.home / ".codex" / "sessions" / "2026" / "10" / "03" / f"rollout-2026-10-03T00-00-00-{R1}.jsonl")
    make_dbs(env, [row(R1, "cli", rp, cwd=cwd_root),
                   row(C1, '{"subagent":{"thread_spawn":{"parent_thread_id":"%s"}}}' % R1, cwd=cwd_child or cwd_root)],
             edges=[(R1, C1)], history={R1: 30, C1: 10})


def project_errors(r):
    return [e for e in r["errors"] if "项目「" in e]


def batch_card(env, items, name="b.json"):
    """批量条目经 items_file 进卡（与 probe codex-history --emit 的产物同一条路径）：手写进 items 的 batch 条目按交互对话计。"""
    d = env.run / "items"
    d.mkdir(exist_ok=True)
    (d / name).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    c = dcard("c01", [])
    c["items_file"] = [f"items/{name}"]
    return c


@pytest.mark.parametrize("status,word", [("active", "还在进行中"), ("protected", "别碰")])
def test_threadless_thread_with_cwd_in_held_project_is_kept(env, status, word):
    proj = env.home / "proj"
    (proj / "sub").mkdir(parents=True)
    write_ledger(env, proj, status)
    threadless_tree(env, str(proj / "sub"))
    env.write([], [dcard("c01", [item_for(env, R1)])])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and any("项目「proj」" in e and word in e for e in project_errors(r)), r["errors"]


def test_threadless_subtask_cwd_counts_for_the_whole_tree(env):
    """根线程的工作目录在项目外、子任务在项目里：codex delete 会连带删子任务，整卡同样保留。"""
    proj = env.home / "proj"
    proj.mkdir()
    write_ledger(env, proj, "active")
    threadless_tree(env, str(env.home / "elsewhere"), cwd_child=str(proj))
    env.write([], [dcard("c01", [item_for(env, R1)])])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]


@pytest.mark.parametrize("where", ["elsewhere", "home"])
def test_threadless_cwd_outside_projects_or_at_home_is_not_attributed(env, where):
    """工作目录在项目外不归属；家目录本身包含项目，但会话的工作目录是家目录不等于属于其下所有项目。"""
    proj = env.home / "proj"
    proj.mkdir()
    write_ledger(env, proj, "active")
    threadless_tree(env, str(env.home / "elsewhere") if where == "elsewhere" else str(env.home))
    env.write([], [dcard("c01", [item_for(env, R1)])])
    r = verdicts(env)["c01"]
    assert not project_errors(r) and r["verdict_out"] == "decide", r["errors"]


def test_threadless_cwd_in_done_project_is_not_blocked(env):
    """做完了的项目不挡（与带文件的条目同一规则：只影响提案，不改变放行条件）。"""
    proj = env.home / "proj"
    proj.mkdir()
    write_ledger(env, proj, "done")
    threadless_tree(env, str(proj))
    env.write([], [dcard("c01", [item_for(env, R1)])])
    r = verdicts(env)["c01"]
    assert not project_errors(r) and r["verdict_out"] == "decide", r["errors"]


def test_threadless_exec_batch_items_in_active_project_are_named(env):
    """批量卡里只要有一条脚本残留的工作目录在进行中项目里，整卡保留，并指名是哪一条。"""
    proj = env.home / "proj"
    proj.mkdir()
    write_ledger(env, proj, "active")
    make_dbs(env, [row(E1, "exec", cwd=str(proj / "batch")), row(E2, "exec", cwd=str(env.home / "tmpwork")),
                   row(E3, "exec", cwd=str(env.home / "tmpwork2"))],
             history={E1: 5, E2: 5, E3: 5})
    env.write([], [batch_card(env, [item_for(env, E1, "batch"), item_for(env, E2, "batch")])])
    r = verdicts(env)["c01"]
    msgs = project_errors(r)
    assert r["verdict_out"] == "keep" and len(msgs) == 1 and E1 in msgs[0] and E2 not in msgs[0], r["errors"]
    assert r["errors"] == msgs, r["errors"]                 # 只因项目被挡，没有别的原因
    # 只剩项目外的条目：整卡放行
    env.write([], [batch_card(env, [item_for(env, E2, "batch"), item_for(env, E3, "batch")])])
    r = verdicts(env)["c01"]
    assert not r["errors"] and r["verdict_out"] == "decide", r["errors"]


# 进行中项目设了“AI 对话逐个问”：按状态库工作目录归属的旧对话不再整卡保留；“别碰”的项目照旧保留
def test_threadless_cwd_in_active_project_with_ask_is_released(env):
    proj = env.home / "proj"
    proj.mkdir()
    write_ledger(env, proj, "active", "ask")
    threadless_tree(env, str(proj))
    env.write([], [dcard("c01", [item_for(env, R1)])])
    r = verdicts(env)["c01"]
    assert not project_errors(r) and r["verdict_out"] == "decide" and r.get("conversation_ask") == ["proj"], r["errors"]


def test_threadless_cwd_in_protected_project_with_ask_is_kept(env):
    proj = env.home / "proj"
    proj.mkdir()
    write_ledger(env, proj, "protected", "ask")
    threadless_tree(env, str(proj))
    env.write([], [dcard("c01", [item_for(env, R1)])])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and any("别碰" in e for e in project_errors(r)), r["errors"]


def test_threadless_batch_in_active_project_with_ask_is_released(env):
    proj = env.home / "proj"
    proj.mkdir()
    write_ledger(env, proj, "active", "ask")
    make_dbs(env, [row(E1, "exec", cwd=str(proj / "batch")), row(E2, "exec", cwd=str(proj))], history={E1: 5, E2: 5})
    c = batch_card(env, [item_for(env, E1, "batch"), item_for(env, E2, "batch")])
    env.write([], [c])
    r = verdicts(env)["c01"]
    assert not project_errors(r) and r["verdict_out"] == "decide", r["errors"]


def test_threadless_child_cwd_in_other_active_project_keeps_card(env):
    """线程根的工作目录在设了 ask 的项目 a 里，后代线程的工作目录在另一个没设 ask 的进行中项目 b 里：b 照旧挡住。"""
    a, b = env.home / "a", env.home / "b"
    a.mkdir()
    b.mkdir()
    p = Path(protect.PROFILE)
    p.parent.mkdir(parents=True, exist_ok=True)

    def ent(proj, status, conv=None):
        e = {"name": proj.name, "status": status, "deliverables": [], "updated_at": "2026-10-11T00:00:00Z", "source": "board"}
        if conv:
            e["conversations"] = conv
        return e
    p.write_text(json.dumps({"projects": {os.path.realpath(a): ent(a, "active", "ask"),
                                          os.path.realpath(b): ent(b, "active")}}, ensure_ascii=False), encoding="utf-8")
    (env.run / "projects.json").write_text(json.dumps({
        "schema": "projects/1", "scan_id": "scan1",
        "projects": [{"key": os.path.realpath(x), "name": x.name, "session_ids": [], "claude_dirs": [], "roots": []}
                     for x in (a, b)]}), encoding="utf-8")
    threadless_tree(env, str(a), str(b))
    env.write([], [dcard("c01", [item_for(env, R1)])])
    r = verdicts(env)["c01"]
    errs = project_errors(r)
    assert r["verdict_out"] == "keep" and any("项目「b」还在进行中" in e for e in errs), r["errors"]
    assert not any("「a」" in e for e in errs), r["errors"]


# ---------------------------------------------------------------- 第二轮评审：原图目录随线程树归属，带图的对话不放开

def image_dirs(env, *tids):
    out = []
    for t in tids:
        d = env.home / ".codex" / "generated_images" / t
        mkfile(d / "a.png")
        age(d)
        out.append(d)
    return out


def test_threadless_emit_card_with_images_listed_is_kept_under_ask(env):
    """probe codex-history --emit 的产物：线程条目 + 整棵树的原图目录（session 写根线程号）。
    设了 ask 也照旧保留：原图目录按线程树的工作目录归到项目，不是对话记录；线程本身带图也不放开。"""
    proj = env.home / "proj"
    proj.mkdir()
    write_ledger(env, proj, "active", "ask")
    threadless_tree(env, str(proj))
    imgs = image_dirs(env, R1, C1)
    its = [item_for(env, R1)] + [{"path": str(d), "method": "delete", "scope": "all", "class": "interactive", "session": R1}
                                 for d in imgs]
    env.write([node(d) for d in imgs], [dcard("c01", its)])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and "conversation_ask" not in r, r["errors"]
    assert any("generated_images" in e and "项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]
    assert any("生成的图" in e for e in r["errors"]), r["errors"]


def test_threadless_thread_with_unlisted_images_is_kept_under_ask(env):
    """卡里只有线程条目，原图目录没列出来：codex delete 会不会连带删它没验证过，照旧保留并写明原因。"""
    proj = env.home / "proj"
    proj.mkdir()
    write_ledger(env, proj, "active", "ask")
    threadless_tree(env, str(proj))
    image_dirs(env, C1)                    # 只有子任务有图：要按整棵树查
    env.write([], [dcard("c01", [item_for(env, R1)])])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and "conversation_ask" not in r, r["errors"]
    assert any("生成的图" in e for e in r["errors"]) and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]


def test_threadless_thread_with_symlinked_images_dir_is_kept_under_ask(env, tmp_path):
    """原图目录是符号链接（指到别处的成品）也算带图：“逐个问”不放开。"""
    proj = env.home / "proj"
    proj.mkdir()
    write_ledger(env, proj, "active", "ask")
    threadless_tree(env, str(proj))
    real = tmp_path / "elsewhere"
    mkfile(real / "a.png")
    link = env.home / ".codex" / "generated_images" / C1
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(real, link)
    env.write([], [dcard("c01", [item_for(env, R1)])])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and "conversation_ask" not in r, r["errors"]
    assert any("生成的图" in e for e in r["errors"]), r["errors"]


def test_images_dir_of_gone_thread_belongs_to_project_by_tree_cwd(env):
    """会话文件已不在的线程，它的原图目录单独成卡（不设 ask）：按状态库里线程树的工作目录归到进行中项目，照旧保留。"""
    proj = env.home / "proj"
    proj.mkdir()
    write_ledger(env, proj, "active")
    threadless_tree(env, str(proj))
    (d,) = image_dirs(env, C1)
    env.write([node(d)], [dcard("c01", [{"path": str(d), "method": "delete", "scope": "all", "session": R1}])])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]


def test_ask_keeps_codex_delete_whose_thread_does_not_match_file_with_state_db(env):
    """状态库读得了（不被“读不了先不放开”挡住）时：codex 删除命令的线程号（R1，属于设了 ask 的项目）
    和会话文件名（R2）对不上，照旧保护。"""
    proj = env.home / "proj"
    proj.mkdir()
    write_ledger(env, proj, "active", "ask")
    (env.run / "projects.json").write_text(json.dumps({
        "schema": "projects/1", "scan_id": "scan1",
        "projects": [{"key": os.path.realpath(proj), "name": "proj", "session_ids": [R1], "claude_dirs": [], "roots": []}]}),
        encoding="utf-8")
    f = rollout(env, R2)
    make_dbs(env, [row(R1, "cli"), row(R2, "cli", str(f))], history={R1: 3, R2: 3})
    it = {"path": str(f), "method": "native", "argv": ["codex", "delete", "--force", R1]}
    env.write([node(f)], [dcard("c01", [it])])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and "conversation_ask" not in r, r["errors"]
    assert any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]


def _ask_proj_with_sessions(env, sids):
    proj = env.home / "proj"
    proj.mkdir()
    write_ledger(env, proj, "active", "ask")
    (env.run / "projects.json").write_text(json.dumps({
        "schema": "projects/1", "scan_id": "scan1",
        "projects": [{"key": os.path.realpath(proj), "name": "proj", "session_ids": list(sids), "claude_dirs": [], "roots": []}]}),
        encoding="utf-8")
    return proj


def test_ask_releases_subtask_rollout_that_carries_the_root_session(env):
    """探针给整棵树的条目都写根线程号（session=R1）：子任务 C1 的会话文件名是 C1，只要 C1 是 R1 的后代就算对话记录。"""
    _ask_proj_with_sessions(env, [R1, C1])
    f1, f2 = rollout(env, R1), rollout(env, C1, parent=R1)
    age(env.home)
    make_dbs(env, [row(R1, "cli", str(f1)), row(C1, '{"subagent":{"thread_spawn":{"parent_thread_id":"%s"}}}' % R1, str(f2))],
             edges=[(R1, C1)], history={R1: 3, C1: 3})
    its = [{"path": str(f), "method": "native", "argv": ["codex", "delete", "--force", t], "session": R1, "class": "interactive"}
           for f, t in ((f2, C1), (f1, R1))]
    env.write([node(env.home / ".codex")], [dcard("c01", its)])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "decide" and r.get("conversation_ask") == ["proj"] and not project_errors(r), r["errors"]


def test_ask_keeps_rollout_whose_session_is_an_unrelated_thread(env):
    """子任务 C1 的会话文件写 session=R2（不是它的祖先）：对不上，照旧保护。"""
    _ask_proj_with_sessions(env, [R2, C1])
    f1, f2 = rollout(env, R1), rollout(env, C1, parent=R1)
    age(env.home)
    make_dbs(env, [row(R1, "cli", str(f1)), row(C1, '{"subagent":{"thread_spawn":{"parent_thread_id":"%s"}}}' % R1, str(f2)),
                   row(R2, "cli")], edges=[(R1, C1)], history={R1: 3, C1: 3, R2: 3})
    it = {"path": str(f2), "method": "native", "argv": ["codex", "delete", "--force", C1], "session": R2, "class": "interactive"}
    env.write([node(env.home / ".codex")], [dcard("c01", [it])])
    r = verdicts(env)["c01"]
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]
