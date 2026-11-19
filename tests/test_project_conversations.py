"""进行中项目里的 AI 对话“逐个问”（台账 conversations=ask）。

放开的只有“项目还在进行中”这一条规则，而且只对按会话归属到项目的对话记录（会话号 / ~/.claude/projects 目录 /
状态库工作目录）。路径落在项目目录里的文件、“别碰”的项目、没设 ask 的项目照旧整卡保留；保留天数等其余检查不变。
全部在临时 HOME 里，不碰真实的 ~/.sansheng。
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, age, card, delete_item, mkfile, node, plan, protect  # noqa: E402

import prefs  # noqa: E402

UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def write_profile(**kw):
    p = Path(protect.PROFILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")


def read_profile():
    return json.loads(Path(protect.PROFILE).read_text(encoding="utf-8"))


def ledger(proj, status, conversations=None):
    e = {"name": Path(proj).name, "status": status, "deliverables": [], "updated_at": "2026-10-11T00:00:00Z",
         "source": "board"}
    if conversations:
        e["conversations"] = conversations
    return {os.path.realpath(proj): e}


def projects_json(env, proj, session_ids=(), claude_dirs=()):
    (env.run / "projects.json").write_text(json.dumps({
        "schema": "projects/1", "scan_id": "scan1",
        "projects": [{"key": os.path.realpath(proj), "name": Path(proj).name, "session_ids": list(session_ids),
                      "claude_dirs": list(claude_dirs), "roots": []}]}), encoding="utf-8")


def make_proj(env):
    proj = env.home / "proj"
    proj.mkdir()
    return proj


def session_card(env, uuid=UUID):
    """~/.codex/sessions 下一个会话文件（不在项目目录里），只能按会话号归属到项目。"""
    rollout = env.home / ".codex" / "sessions" / "2026" / "10" / f"rollout-2026-10-01T00-00-00-{uuid}.jsonl"
    mkfile(rollout)
    age(rollout.parent)
    env.write([node(rollout)], [card("c01", [delete_item(rollout, session=uuid)], verdict="decide", question="删吗？")])
    return rollout


def out(env, cid="c01"):
    return next(c for c in env.check()["cards"] if c["id"] == cid)


def project_errors(r):
    return [e for e in r["errors"] if "项目「" in e]


# ---------------------------------------------------------------- 放开的情形

def test_ask_releases_codex_session_attributed_by_session_id(env):
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, session_ids=[UUID])
    session_card(env)
    r = out(env)
    assert not project_errors(r), r["errors"]
    assert r["verdict_out"] == "decide" and r.get("conversation_ask") == ["proj"]
    assert r["default_select"] is False                      # 看板不预选：仍要用户逐张勾
    rec = json.loads((env.run / "plan.check.json").read_text())["cards"]
    assert next(c for c in rec if c["id"] == "c01").get("conversation_ask") == ["proj"]


def test_ask_releases_claude_project_dir(env):
    proj = make_proj(env)
    cdir = env.home / ".claude" / "projects" / "-x-proj"
    f = mkfile(cdir / f"{UUID}.jsonl")                   # Claude Code 的会话记录按会话号命名
    age(cdir)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, claude_dirs=["-x-proj"])
    env.write([node(f)], [card("c01", [delete_item(f)], verdict="decide", question="删吗？")])
    r = out(env)
    assert not project_errors(r), r["errors"]
    assert r["verdict_out"] == "decide" and r.get("conversation_ask") == ["proj"]


# ---------------------------------------------------------------- 照旧挡住的情形

def test_without_ask_session_is_still_kept(env):
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active"))
    projects_json(env, proj, session_ids=[UUID])
    session_card(env)
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r))
    assert "conversation_ask" not in r


def test_explicit_protect_policy_is_the_same_as_default(env):
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active", "protect"))
    projects_json(env, proj, session_ids=[UUID])
    session_card(env)
    assert out(env)["verdict_out"] == "keep"


def test_ask_does_not_release_protected_project(env):
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "protected", "ask"))
    projects_json(env, proj, session_ids=[UUID])
    session_card(env)
    r = out(env)
    assert r["verdict_out"] == "keep" and any("别碰" in e for e in project_errors(r)), r["errors"]


def test_ask_keeps_files_inside_project_dir(env):
    """项目目录里的文件（路径归属）照旧保护：ask 只管对话记录。"""
    proj = make_proj(env)
    build = proj / "build"
    mkfile(build / "a.bin")
    age(build)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, session_ids=[UUID])
    env.write([node(build)], [card("c01", [delete_item(build)], verdict="decide", question="删吗？")])
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r))


def test_ask_keeps_item_attributed_by_both_path_and_session(env):
    """同一条目既在项目目录里、文件名里的会话号又属于该项目：按路径的保护优先。"""
    proj = make_proj(env)
    f = mkfile(proj / "logs" / f"rollout-{UUID}.jsonl")
    age(proj / "logs")
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, session_ids=[UUID])
    env.write([node(f)], [card("c01", [delete_item(f, session=UUID)], verdict="decide", question="删吗？")])
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r))


def test_ask_keeps_item_that_contains_the_whole_project(env):
    """条目把整个进行中项目包在里面（contains）：照旧保护。"""
    parent = env.home / "all"
    proj = parent / "proj"
    mkfile(proj / "src.txt")
    age(parent)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, session_ids=[UUID])
    env.write([node(parent)], [card("c01", [delete_item(parent, session=UUID)], verdict="decide", question="删吗？")])
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r))


def test_ask_does_not_skip_retention_days(env):
    """逐个问不跳过“最近 N 天内用过的对话一律留着”。"""
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active", "ask"), retention_days=30)
    projects_json(env, proj, session_ids=[UUID])
    rollout = session_card(env)
    now = time.time() - 2 * 86400
    os.utime(rollout, (now, now))
    env.write([node(rollout, newest_mtime=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)))],
              [card("c01", [delete_item(rollout, session=UUID, **{"class": "interactive"})], verdict="decide", question="删吗？")])
    r = out(env)
    assert not project_errors(r) and any("30 天内用过的 AI 对话一律留着" in e for e in r["errors"]), r["errors"]
    assert r["verdict_out"] == "keep"


# ---------------------------------------------------------------- 设置指纹与台账

def test_guard_digest_changes_only_when_ask_is_set(tmp_path):
    proj = tmp_path / "proj"
    base = protect.profile_guard({"projects": ledger(proj, "active")})["digest"]
    assert protect.profile_guard({"projects": ledger(proj, "active", "protect")})["digest"] == base
    assert protect.profile_guard({"projects": ledger(proj, "active", "ask")})["digest"] != base


def test_cli_sets_conversations_and_keeps_status(env):
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active"))
    key = os.path.realpath(proj)
    assert plan.main(["project", "--key", key, "--conversations", "ask"]) == 0
    e = read_profile()["projects"][key]
    assert e["status"] == "active" and e["conversations"] == "ask"
    # 不认识的取值：不保存
    assert plan.main(["project", "--key", key, "--conversations", "maybe"]) == 2
    assert read_profile()["projects"][key]["conversations"] == "ask"
    # 改回跟项目一起留着：字段去掉（与缺省等价）
    assert plan.main(["project", "--key", key, "--conversations", "protect"]) == 0
    assert "conversations" not in read_profile()["projects"][key]


def test_cli_conversations_needs_known_project_or_status(env):
    proj = make_proj(env)
    write_profile(projects={})
    assert plan.main(["project", "--key", os.path.realpath(proj), "--conversations", "ask"]) == 2
    assert plan.main(["project", "--key", os.path.realpath(proj)]) == 2


def test_board_ledger_line_without_field_keeps_ask(env):
    """看板导出的台账行不带对话处理方式：沿用原来的，不能把“逐个问”冲掉。"""
    proj = make_proj(env)
    key = os.path.realpath(proj)
    write_profile(projects=ledger(proj, "active", "ask"))
    p = read_profile()
    done, errs, _ = prefs.apply_ledger(p, {key: {"状态": "进行中"}})
    assert not errs and p["projects"][key]["conversations"] == "ask"
    done, errs, _ = prefs.apply_ledger(p, {key: {"状态": "别碰"}})
    assert p["projects"][key]["conversations"] == "ask" and p["projects"][key]["status"] == "protected"
    done, errs, _ = prefs.apply_ledger(p, {key: {"状态": "进行中", "对话": "逐个问"}})
    assert not errs and p["projects"][key]["conversations"] == "ask"
    done, errs, _ = prefs.apply_ledger(p, {key: {"状态": "进行中", "对话": "随便"}})
    assert errs and p["projects"][key]["conversations"] == "ask"


# ---------------------------------------------------------------- 评审补充：放开范围只限对话记录本身

def projects_json_multi(env, entries):
    """entries：[(项目目录, 会话号列表)]"""
    (env.run / "projects.json").write_text(json.dumps({
        "schema": "projects/1", "scan_id": "scan1",
        "projects": [{"key": os.path.realpath(pr), "name": Path(pr).name, "session_ids": list(ids),
                      "claude_dirs": [], "roots": []} for pr, ids in entries]}), encoding="utf-8")


def batch_conv_card(env, category=None, cards_used=()):
    """项目目录外的一个会话文件，经 items_file 以批量条目进卡（与会话探针 --emit 的产物同一条路径）：
    是对话记录、但不是交互对话，原本会进对话档位。"""
    rollout = env.home / ".codex" / "sessions" / "2026" / "10" / f"rollout-2026-10-01T00-00-00-{UUID}.jsonl"
    mkfile(rollout)
    age(rollout.parent)
    d = env.run / "items"
    d.mkdir(exist_ok=True)
    (d / "b.json").write_text(json.dumps([delete_item(rollout, session=UUID, **{"class": "batch"})]), encoding="utf-8")
    c = card("c01", [], verdict="decide", question="删吗？")
    c["items_file"] = ["items/b.json"]
    c["preset"] = "recommended"
    if category:
        c["category"] = category
    if cards_used:
        c["cards_used"] = list(cards_used)
    env.write([node(rollout)], [c])
    return rollout


def test_ask_keeps_generated_images_attributed_by_session(env):
    """会话的生成图目录（~/.codex/generated_images/<线程号>）不是对话记录，可能是项目成品：设了 ask 也照旧保护，
    “废稿不留”也不能把它预填成删。"""
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active", "ask"), rejects_policy="drop", onboarded_at="2026-10-11T00:00:00Z")
    projects_json(env, proj, session_ids=[UUID])
    gi = env.home / ".codex" / "generated_images" / UUID
    mkfile(gi / "a.png")
    age(gi)
    c = card("c01", [delete_item(gi)], verdict="decide", question="删吗？")
    c["category"] = "ai-images"
    env.write([node(gi)], [c])
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]
    assert "conversation_ask" not in r and "prefill" not in r


def test_ask_keeps_non_conversation_file_with_session_field(env):
    """项目目录外的普通文件，条目自带会话号：不是对话记录，照旧保护。"""
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, session_ids=[UUID])
    outd = env.work / "output"
    mkfile(outd / "result.bin")
    age(outd)
    env.write([node(outd)], [card("c01", [delete_item(outd, session=UUID)], verdict="decide", question="删吗？")])
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]


def test_ask_keeps_nested_done_subproject_file_named_with_session(env):
    """进行中项目 a（设了 ask）里嵌着做完的子项目 sub，文件名里带 a 的会话号：仍在 a 的目录里，照旧保护。"""
    a = env.home / "a"
    sub = a / "sub"
    f = mkfile(sub / "notes" / f"draft-{UUID}.md")
    age(a)
    write_profile(projects={**ledger(a, "active", "ask"), **ledger(sub, "done")})
    projects_json_multi(env, [(a, [UUID]), (sub, [])])
    env.write([node(f)], [card("c01", [delete_item(f, session=UUID)], verdict="decide", question="删吗？")])
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「a」还在进行中" in e for e in project_errors(r)), r["errors"]


def test_ask_keeps_conversation_record_lying_inside_the_project_dir(env):
    """防御：对话记录本身落在设了 ask 的项目目录里（路径归属被更深的做完子项目拿走，a 只剩会话归属）：照旧保护。"""
    a = env.home / ".codex"
    sub = a / "sessions"
    rollout = sub / "2026" / "10" / f"rollout-2026-10-01T00-00-00-{UUID}.jsonl"
    mkfile(rollout)
    age(rollout.parent)
    write_profile(projects={**ledger(a, "active", "ask"), **ledger(sub, "done")})
    projects_json_multi(env, [(a, [UUID]), (sub, [])])
    env.write([node(rollout)], [card("c01", [delete_item(rollout, session=UUID)], verdict="decide", question="删吗？")])
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「.codex」还在进行中" in e for e in project_errors(r)), r["errors"]


@pytest.mark.parametrize("status,word", [("active", "还在进行中"), ("protected", "别碰")])
def test_ask_does_not_release_session_that_also_belongs_to_another_held_project(env, status, word):
    """同一个会话号也属于另一个没设 ask 的进行中 / 别碰项目：那个项目照旧挡住，报错指向它。"""
    a, b = env.home / "a", env.home / "b"
    a.mkdir()
    b.mkdir()
    write_profile(projects={**ledger(a, "active", "ask"), **ledger(b, status)})
    projects_json_multi(env, [(a, [UUID]), (b, [UUID])])
    session_card(env)
    r = out(env)
    errs = project_errors(r)
    assert r["verdict_out"] == "keep" and any(f"项目「b」{word}" in e or ("「b」" in e and word in e) for e in errs), r["errors"]
    assert not any("「a」" in e for e in errs), r["errors"]


def test_released_batch_conversation_card_is_board_only_and_rules_do_not_apply(env):
    """因“对话逐个问”放开的批量对话卡：不预填、不进对话档位（回“好”不删它）、“以后都删”规则不替你删，提案改发看板。"""
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active", "ask"), rejects_policy="drop", onboarded_at="2026-10-11T00:00:00Z")
    projects_json(env, proj, session_ids=[UUID])
    batch_conv_card(env, category="ai-images", cards_used=["codex"])
    r = out(env)
    assert not project_errors(r) and r["verdict_out"] == "decide" and r.get("conversation_ask") == ["proj"], r["errors"]
    assert "prefill" not in r                                   # “废稿不留”也不预填
    env.seal()
    sealed = json.loads((env.run / "seal.json").read_text())["cards"]["c01"]
    assert sealed["review"] is True and not sealed.get("interactive")
    key = sealed["rule_key"]
    assert key, sealed
    prof = read_profile()
    prof["rules"] = {key: {"action": "delete", "source": "board", "at": "2026-10-11T00:00:00Z"}}
    Path(protect.PROFILE).write_text(json.dumps(prof, ensure_ascii=False), encoding="utf-8")
    env.seal()                                                  # 设置变了（规则不进摘要也照样重封，保持与真实流程一致）
    proposal, err = plan.build_proposal(str(env.run))
    assert err is None, err
    assert all("c01" not in o["cards"] for o in proposal["options"].values()), proposal["options"]
    assert "c01" in proposal["attention"]                       # 规则没替你拿主意
    assert proposal["mode"] == "board" and any("逐个问" in x for x in proposal["board_reasons"]), proposal["board_reasons"]
    bp = plan.board_plan(plan.load_plan(str(env.run))[0], plan.check_plan(str(env.run))[0])
    assert next(c for c in bp["cards"] if c["id"] == "c01").get("conversation_ask") == ["proj"]


def test_board_ignores_conversation_ask_written_in_plan(env):
    """看板上的“逐个问”说明只认校验算出来的，计划里手写的不作数。"""
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active"))
    projects_json(env, proj, session_ids=[UUID])
    session_card(env)
    pl = json.loads((env.run / "plan.json").read_text())
    pl["cards"][0]["conversation_ask"] = ["proj"]
    (env.run / "plan.json").write_text(json.dumps(pl, ensure_ascii=False), encoding="utf-8")
    bp = plan.board_plan(plan.load_plan(str(env.run))[0], plan.check_plan(str(env.run))[0])
    assert "conversation_ask" not in next(c for c in bp["cards"] if c["id"] == "c01")


def test_changing_conversations_invalidates_the_old_seal(env):
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active"))
    projects_json(env, proj, session_ids=[UUID])
    session_card(env)
    env.check()
    env.seal()
    seal = json.loads((env.run / "seal.json").read_text())
    assert protect.profile_guard_problem(seal, plan.load_profile()) is None
    assert plan.main(["project", "--key", os.path.realpath(proj), "--conversations", "ask"]) == 0
    assert protect.profile_guard_problem(seal, plan.load_profile())


# ---------------------------------------------------------------- 评审补充：撤销记忆不能删掉项目

def test_forget_conversations_note_restores_only_the_policy(env):
    import memory
    proj = make_proj(env)
    key = os.path.realpath(proj)
    write_profile(projects=ledger(proj, "protected"))
    assert plan.main(["project", "--key", key, "--conversations", "ask"]) == 0
    logs = [e for e in read_profile()["memory_log"] if e.get("kind") == "project_conversations"]
    assert len(logs) == 1 and logs[0]["ref"]["old_conversations"] == "protect"
    memory.forget([logs[0]["id"]])
    e = read_profile()["projects"][key]
    assert e["status"] == "protected" and "conversations" not in e


def test_forget_restores_policy_when_current_value_is_what_the_note_wrote(env):
    import memory
    proj = make_proj(env)
    key = os.path.realpath(proj)
    write_profile(projects=ledger(proj, "active"))
    assert plan.main(["project", "--key", key, "--conversations", "ask"]) == 0
    mid = [e for e in read_profile()["memory_log"] if e.get("kind") == "project_conversations"][-1]["id"]
    assert plan.main(["project", "--key", key, "--conversations", "protect"]) == 0
    assert plan.main(["project", "--key", key, "--conversations", "ask"]) == 0
    done, errs = memory.forget([mid])      # 这条记的是 protect→ask；现在仍是 ask：恢复成 protect
    assert not errs and "之后改过" not in "".join(done)
    e = read_profile()["projects"][key]
    assert "conversations" not in e and e["status"] == "active"


def test_forget_reports_policy_changed_later_and_leaves_it(env):
    import memory
    proj = make_proj(env)
    key = os.path.realpath(proj)
    write_profile(projects=ledger(proj, "active"))
    assert plan.main(["project", "--key", key, "--conversations", "ask"]) == 0
    mid = [e for e in read_profile()["memory_log"] if e.get("kind") == "project_conversations"][-1]["id"]
    assert plan.main(["project", "--key", key, "--conversations", "protect"]) == 0
    done, errs = memory.forget([mid])      # 之后已经改回 protect：不动，并说明
    assert not errs and "之后改过" in "".join(done), done
    e = read_profile()["projects"][key]
    assert "conversations" not in e and e["status"] == "active"


def test_forget_project_note_without_status_leaves_ledger_alone(env):
    """旧格式（kind=project、ref 只有对话处理方式）的记录：不认识就不动，不把项目从台账里删掉。"""
    import memory
    proj = make_proj(env)
    key = os.path.realpath(proj)
    prof = {"projects": ledger(proj, "protected", "ask"),
            "memory_log": [{"id": "m1", "kind": "project", "text": "x", "at": "2026-10-11T00:00:00Z",
                            "ref": {"key": key, "old_conversations": "protect", "conversations": "ask"}}]}
    write_profile(**prof)
    memory.forget(["m1"])
    e = read_profile()["projects"][key]
    assert e["status"] == "protected" and e["conversations"] == "ask"


def test_ask_keeps_claude_session_companion_dir(env):
    """~/.claude/projects/<目录>/<会话号>/（子 Agent 记录、工具输出）是目录：不受保留天数约束，也可能混着别的东西
    （如 <会话号>/memory），“逐个问”不放开它，照旧跟项目一起留着。"""
    proj = make_proj(env)
    sdir = env.home / ".claude" / "projects" / "-x-proj" / UUID
    mkfile(sdir / "tool-results" / "a.txt")
    age(sdir)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, claude_dirs=["-x-proj"])
    env.write([node(sdir)], [card("c01", [delete_item(sdir)], verdict="decide", question="删吗？")])
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]


def test_ask_keeps_non_session_dir_under_claude_projects(env):
    """~/.claude/projects/<目录>/ 下不是会话号命名的目录（例如自己放的笔记）：不算对话记录，照旧保护。"""
    proj = make_proj(env)
    ndir = env.home / ".claude" / "projects" / "-x-proj" / "notes"
    mkfile(ndir / "a.txt")
    age(ndir)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, claude_dirs=["-x-proj"])
    env.write([node(ndir)], [card("c01", [delete_item(ndir)], verdict="decide", question="删吗？")])
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]


# ---------------------------------------------------------------- 第二轮评审：只放开单个对话文件，带图的对话不放开

def two_projects_with_sessions(env, ua, ub, b_status="active"):
    a, b = env.home / "a", env.home / "b"
    a.mkdir()
    b.mkdir()
    write_profile(projects={**ledger(a, "active", "ask"), **ledger(b, b_status)})
    projects_json_multi(env, [(a, [ua]), (b, [ub])])
    return a, b


UB = "66666666-7777-8888-9999-000000000000"


@pytest.mark.parametrize("which", ["month", "root"])
def test_ask_keeps_session_directory_even_with_session_field(env, which):
    """~/.codex/sessions 下的目录条目写了 session=A：目录里还有别的项目 B 的对话，不算单个对话记录，照旧保护。"""
    two_projects_with_sessions(env, UUID, UB)
    base = env.home / ".codex" / "sessions"
    for u in (UUID, UB):
        mkfile(base / "2026" / "10" / f"rollout-2026-10-01T00-00-00-{u}.jsonl")
    age(base)
    target = base / "2026" / "10" if which == "month" else base
    env.write([node(target)], [card("c01", [delete_item(target, session=UUID)], verdict="decide", question="删吗？")])
    r = out(env)
    assert r["verdict_out"] == "keep" and "conversation_ask" not in r, r["errors"]
    assert any("项目「a」还在进行中" in e for e in project_errors(r)), r["errors"]


def test_ask_keeps_rollout_whose_session_field_does_not_match_its_name(env):
    """会话文件名里是 B 的会话号，条目却写 session=A：对不上就不算 A 的对话记录。"""
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, session_ids=[UUID])
    rollout = env.home / ".codex" / "sessions" / "2026" / "10" / f"rollout-2026-10-01T00-00-00-{UB}.jsonl"
    mkfile(rollout)
    age(rollout.parent)
    env.write([node(rollout)], [card("c01", [delete_item(rollout, session=UUID)], verdict="decide", question="删吗？")])
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]


def test_ask_keeps_conversation_that_has_generated_images(env):
    """对话还有 Codex 生成的图（~/.codex/generated_images/<会话号>）：可能是项目成品，带图的对话照旧保护，并写明原因。"""
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, session_ids=[UUID])
    gi = env.home / ".codex" / "generated_images" / UUID
    mkfile(gi / "a.png")
    age(gi)
    session_card(env)
    r = out(env)
    assert r["verdict_out"] == "keep" and "conversation_ask" not in r, r["errors"]
    assert any("生成的图" in e for e in r["errors"]) and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]


def test_board_carries_interactive_only_from_check(env):
    """看板上的“交互对话”标记只认校验结果：计划里手写的不作数，校验认出的会带上（看板据此不让批量按钮勾它）。"""
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, session_ids=[UUID])
    rollout = env.home / ".codex" / "sessions" / "2026" / "10" / f"rollout-2026-10-01T00-00-00-{UUID}.jsonl"
    mkfile(rollout)
    other = env.work / "build"
    mkfile(other / "a.bin")
    age(rollout.parent)
    age(other)
    c2 = card("c02", [delete_item(other)], verdict="decide", question="删吗？")
    c2["interactive"] = True
    env.write([node(rollout), dict(node(other), id="n2")],
              [card("c01", [delete_item(rollout, session=UUID, **{"class": "interactive"})], verdict="decide", question="删吗？"), c2])
    res = env.check()
    by = {c["id"]: c for c in res["cards"]}
    assert by["c01"]["verdict_out"] == "decide" and by["c01"].get("interactive") is True, by["c01"]["errors"]
    bp = {c["id"]: c for c in plan.board_plan(plan.load_plan(str(env.run))[0], res)["cards"]}
    assert bp["c01"].get("interactive") is True and bp["c01"].get("conversation_ask") == ["proj"]
    assert "interactive" not in bp["c02"]


def test_ask_keeps_claude_jsonl_not_named_by_session(env):
    """~/.claude/projects/<目录>/ 下不是按会话号命名的 jsonl：不是 Claude Code 的会话记录，照旧保护。"""
    proj = make_proj(env)
    cdir = env.home / ".claude" / "projects" / "-x-proj"
    f = mkfile(cdir / "notes.jsonl")
    age(cdir)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, claude_dirs=["-x-proj"])
    env.write([node(f)], [card("c01", [delete_item(f)], verdict="decide", question="删吗？")])
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]


def native_session_card(env, name_uuid, argv_uuid, session=None):
    """~/.codex/sessions 下一个会话文件，用 codex 删除命令删（与 probe codex-groups --emit 的条目同形）。"""
    rollout = env.home / ".codex" / "sessions" / "2026" / "10" / f"rollout-2026-10-01T00-00-00-{name_uuid}.jsonl"
    mkfile(rollout)
    age(rollout.parent)
    it = {"path": str(rollout), "method": "native", "argv": ["codex", "delete", "--force", argv_uuid]}
    if session:
        it["session"] = session
    env.write([node(rollout)], [card("c01", [it], verdict="decide", question="删吗？")])
    return rollout


def test_ask_keeps_codex_delete_whose_thread_does_not_match_the_file(env):
    """codex 删除命令里的线程号（A）和会话文件名（B）对不上：不算 A 的对话记录，照旧保护。"""
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, session_ids=[UUID])
    native_session_card(env, UB, UUID)
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]


def test_ask_holds_codex_delete_when_state_db_is_unreadable(env):
    """用 codex 删除命令删、状态库又读不了（这里根本没有状态库）：没法核对整棵对话树有没有生成图，先不放开。"""
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, session_ids=[UUID])
    native_session_card(env, UUID, UUID, session=UUID)
    r = out(env)
    assert r["verdict_out"] == "keep" and "conversation_ask" not in r, r["errors"]
    assert any("状态库读不了" in e for e in r["errors"]) and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]


def test_ask_keeps_directory_named_like_a_session_file(env):
    """目录哪怕名字像会话文件也不算对话记录（里面可能混着别的东西）。"""
    proj = make_proj(env)
    write_profile(projects=ledger(proj, "active", "ask"))
    projects_json(env, proj, session_ids=[UUID])
    d = env.home / ".codex" / "sessions" / "2026" / "10" / f"rollout-2026-10-01T00-00-00-{UUID}.jsonl"
    mkfile(d / "inner.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d, session=UUID)], verdict="decide", question="删吗？")])
    r = out(env)
    assert r["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in project_errors(r)), r["errors"]
