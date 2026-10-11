"""0.5 项目台账：probe projects、profile.projects、check / propose 接入、封存守卫、成长问题与首次判定。

夹具全在 tmp_path 内（沿用 test_plan_act 的 Env：HOME / protect.HOME / profile / runs 都指向临时目录）。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import END_EVIDENCE, Env, act, age, auto_sel, card, delete_item, mkfile, node, plan, protect, with_gate  # noqa: E402

import prefs  # noqa: E402
import scan  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_profile(env, **kw):
    p = Path(protect.PROFILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")


def read_profile():
    return json.loads(Path(protect.PROFILE).read_text(encoding="utf-8"))


def ledger(proj, status, name=None, deliverables=None):
    return {os.path.realpath(proj): {"name": name or Path(proj).name, "status": status,
                                     "deliverables": deliverables or [], "updated_at": "2026-10-11T00:00:00Z", "source": "board"}}


def make_proj(env, name="proj"):
    """项目目录里放一个已 age 的 build 缓存目录，返回 (项目目录, build 目录)。"""
    proj = env.home / name
    build = proj / "build"
    mkfile(build / "a.bin")
    age(build)
    return proj, build


def one_card_env(env, status, name="proj", verdict="clean", evidence=True, **card_kw):
    proj, build = make_proj(env, name)
    write_profile(env, projects=ledger(proj, status))
    c = card("c01", [delete_item(build)], verdict=verdict, question="删吗？" if verdict == "decide" else None)
    c.update(card_kw)
    if not evidence:
        c["evidence"] = []
    env.write([node(build)], [c])
    return proj, build


def run_argv(env, digest, *flags):
    return auto_sel(["run", "--run", str(env.run), "--seal", digest, "--cards", "c01", *flags])


def card_out(chk, cid="c01"):
    return next(c for c in chk["cards"] if c["id"] == cid)


# ------------------------------------------------------------ 进行中 / 别碰 / 做完了 / 归档

def test_active_project_gets_no_delete_card(env):
    """进行中的项目：项目里的东西整卡降为 keep，不进封存。"""
    proj, build = one_card_env(env, "active")
    chk = env.check()
    c = card_out(chk)
    assert c["verdict_out"] == "keep" and c["downgraded"]
    assert any("项目「proj」还在进行中" in e for e in c["errors"])
    assert c["projects"][0]["status"] == "active"
    assert c["judgment_refs"] and "进行中的项目" in c["judgment_refs"][0]["quote"]
    env.seal()
    assert json.loads((env.run / "seal.json").read_text())["cards"] == {}
    assert (build / "a.bin").exists()


def test_protected_project_card_kept_and_execution_blocked(env):
    """别碰：check 整卡保留；就算绕过 check 伪造封存，act 的保护名单（有效 profile 里并入的项目根）也拦下。"""
    proj, build = one_card_env(env, "protected")
    chk = env.check()
    assert card_out(chk)["verdict_out"] == "keep"
    assert any("别碰" in e for e in card_out(chk)["errors"])
    eff = plan.load_profile()
    assert os.path.realpath(proj) in [os.path.realpath(x) for x in eff["protect"]]
    assert os.path.realpath(proj) not in [os.path.realpath(x) for x in prefs.load()["protect"]]   # 只在内存里，不写回文件
    assert protect.protected_reason(str(build), eff)
    digest = env.forge({"c01": ("clean", [delete_item(build)])})
    act.main(with_gate(["run", "--run", str(env.run), "--seal", digest, "--cards", "c01"]))
    assert (build / "a.bin").exists()
    rc = json.loads((env.run / "receipt.json").read_text())
    statuses = [it["status"] for c in rc["cards"] for it in c["items"]]
    assert statuses and set(statuses) == {"skipped"}


def test_done_project_is_not_clean_without_end_evidence(env):
    """做完了 + user_stated：不改变放行条件，缺结束证据的 clean 卡仍降为 decide。"""
    proj, build = one_card_env(env, "done", evidence=False)
    c = json.loads((env.run / "plan.json").read_text())
    c["cards"][0]["evidence"] = [{"kind": "user_stated", "text": "这个项目做完了"}]
    (env.run / "plan.json").write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    chk = env.check()
    out = card_out(chk)
    assert out["verdict_out"] == "decide" and out["downgraded"]
    assert any("任务已结束" in e or "结束" in e for e in out["errors"])
    # 对照：有真实的结束证据，做完了的项目照常放行 clean
    c["cards"][0]["evidence"] = [END_EVIDENCE]
    (env.run / "plan.json").write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    assert card_out(env.check())["verdict_out"] == "clean"


def test_done_project_decide_card_prefilled_delete(env):
    """做完了的项目：decide 卡的问题预填“删”（只是预填，档位不变）；clean 卡没有预填。"""
    one_card_env(env, "done", verdict="decide")
    env.check()
    digest = env.seal()
    seal = json.loads((env.run / "seal.json").read_text())
    assert seal["cards"]["c01"]["prefill"] == "删" and seal["cards"]["c01"]["preset"] == "recommended"
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    prop = json.loads((env.run / "proposal.json").read_text())
    assert prop["prefill"] == {"c01": "删"} and prop["projects_by_card"]["c01"][0]["status"] == "done"
    assert prop["seal"] == digest
    bp = plan.board_plan(json.loads((env.run / "plan.json").read_text()), env.check())
    assert bp["cards"][0]["prefill"] == "删" and bp["cards"][0]["projects"][0]["name"] == "proj"


def test_done_project_clean_card_has_no_prefill(env):
    one_card_env(env, "done", verdict="clean")
    env.seal()
    seal = json.loads((env.run / "seal.json").read_text())
    assert "prefill" not in seal["cards"]["c01"] and seal["cards"]["c01"]["projects"][0]["status"] == "done"


def test_archived_moves_all_tier_card_into_recommended(env):
    """归档：Agent 标成“删了找不回”(all) 的卡进推荐档；做完了的同样的卡仍是 all；风险区不受影响。"""
    one_card_env(env, "archived", verdict="decide", preset="all")
    env.check()
    env.seal()
    assert json.loads((env.run / "seal.json").read_text())["cards"]["c01"]["preset"] == "recommended"
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    prop = json.loads((env.run / "proposal.json").read_text())
    assert prop["options"]["recommended"]["cards"] == ["c01"] and prop["options"]["safe"]["cards"] == []


def test_done_project_keeps_all_tier(env):
    one_card_env(env, "done", verdict="decide", preset="all")
    env.seal()
    assert json.loads((env.run / "seal.json").read_text())["cards"]["c01"]["preset"] == "all"


def test_item_outside_projects_unaffected(env):
    """卡里有一个条目不属于任何项目：不预填、不降档。"""
    proj, build = make_proj(env)
    other = env.work / "cache"
    mkfile(other / "b.bin")
    age(other)
    write_profile(env, projects=ledger(proj, "archived"))
    c = card("c01", [delete_item(build), delete_item(other)], verdict="decide", question="q")
    c["preset"] = "all"
    env.write([node(build), node(other, id="n2")], [c])
    env.check()
    env.seal()
    rec = json.loads((env.run / "seal.json").read_text())["cards"]["c01"]
    assert rec["preset"] == "all" and "prefill" not in rec


# ------------------------------------------------------------ 会话归属

UUID = "11111111-2222-3333-4444-555555555555"


def test_codex_session_of_active_project_kept_via_projects_json(env):
    """Codex 会话不在项目目录里，靠 projects.json 的 session_ids 归属：进行中项目的会话同样整卡保留。"""
    proj = env.home / "proj"
    proj.mkdir()
    rollout = env.home / ".codex" / "sessions" / "2026" / "10" / f"rollout-2026-10-01T00-00-00-{UUID}.jsonl"
    mkfile(rollout)
    age(rollout.parent)
    write_profile(env, projects=ledger(proj, "active"))
    (env.run / "projects.json").write_text(json.dumps({
        "schema": "projects/1", "scan_id": "scan1",
        "projects": [{"key": os.path.realpath(proj), "name": "proj", "session_ids": [UUID], "claude_dirs": [], "roots": []}]}))
    env.write([node(rollout)], [card("c01", [delete_item(rollout, session=UUID)])])
    out = card_out(env.check())
    assert out["verdict_out"] == "keep" and any("项目「proj」还在进行中" in e for e in out["errors"])
    # 换了一次扫描（scan_id 对不上）就不认这份 projects.json
    (env.run / "projects.json").write_text(json.dumps({"schema": "projects/1", "scan_id": "other", "projects": [
        {"key": os.path.realpath(proj), "session_ids": [UUID]}]}))
    out = card_out(env.check())
    assert not any("项目「" in e for e in out["errors"])


def test_claude_project_dir_belongs_to_project(env):
    proj = env.home / "proj"
    proj.mkdir()
    cdir = env.home / ".claude" / "projects" / "-x-proj"
    f = cdir / "a.jsonl"
    mkfile(f)
    age(cdir)
    write_profile(env, projects=ledger(proj, "active"))
    (env.run / "projects.json").write_text(json.dumps({
        "schema": "projects/1", "scan_id": "scan1",
        "projects": [{"key": os.path.realpath(proj), "name": "proj", "claude_dirs": ["-x-proj"], "roots": []}]}))
    env.write([node(f)], [card("c01", [delete_item(f)])])
    out = card_out(env.check())
    assert any("项目「proj」还在进行中" in e for e in out["errors"])


def test_active_project_contained_in_deleted_parent(env):
    """条目把整个进行中的项目包在里面（删上级目录）：同样整卡保留。"""
    parent = env.work / "all"
    proj = parent / "proj"
    mkfile(proj / "src.txt")
    age(parent)
    write_profile(env, projects=ledger(proj, "active"))
    env.write([node(parent)], [card("c01", [delete_item(parent)])])
    assert card_out(env.check())["verdict_out"] == "keep"


# ------------------------------------------------------------ 成品目录并入 / 封存守卫

def test_project_deliverables_join_effective_profile_only(env):
    proj, _ = make_proj(env)
    out = env.home / "out"
    out.mkdir()
    write_profile(env, projects=ledger(proj, "done", deliverables=["~/out"]))
    eff = plan.load_profile()
    assert "~/out" in eff["deliverable_roots"] and "~/out" in eff["reference_roots"]
    raw = prefs.load()
    assert "~/out" not in raw.get("deliverable_roots", []) and "~/out" not in raw.get("reference_roots", [])
    prefs.apply_settings('设置: {"默认":"safe"}')               # 改写 profile 不会把折进去的内容写回文件
    assert "~/out" not in read_profile().get("deliverable_roots", [])


def test_env_project_protected_adds_its_places(env):
    write_profile(env, projects={"env:android": {"name": "安卓开发环境", "status": "protected", "deliverables": []}})
    eff = plan.load_profile()
    assert any(p.endswith("Library/Android") for p in eff["protect"])
    assert protect.protected_reason(str(env.home / ".android" / "avd" / "x.avd"), eff)


def test_status_change_after_seal_blocks_execution(env, capsys):
    """封存后改台账状态（做完了 → 归档）：预演 / 试点 / 正式执行都拒绝，要求重新 check / seal。"""
    proj, build = one_card_env(env, "done")
    digest = env.seal()
    assert act.main(run_argv(env, digest, "--dry-run")) == 0
    write_profile(env, projects=ledger(proj, "archived"))
    capsys.readouterr()
    for flags in (["--dry-run"], ["--pilot"], ["--skip-pilot", "--reason", "x"]):
        assert act.main(run_argv(env, digest, *flags)) == 2, flags
        assert "项目台账" in capsys.readouterr().err
    assert (build / "a.bin").exists()
    new = env.seal()                                 # 重新封存：项目状态记在封存里（影响档位 / 预填），digest 随之变，用户要重新确认
    assert new != digest and act.main(run_argv(env, new, "--dry-run")) == 0
    assert act.main(run_argv(env, digest, "--dry-run")) == 2


def test_deliverables_change_after_seal_blocks_execution(env):
    proj, build = one_card_env(env, "done")
    digest = env.seal()
    (env.home / "out").mkdir()
    write_profile(env, projects=ledger(proj, "done", deliverables=["~/out"]))
    assert act.main(run_argv(env, digest, "--dry-run")) == 2


def test_project_name_change_does_not_stale_seal(env):
    """名字、更新时间只是显示用，不影响判断，不让封存过期。"""
    proj, build = one_card_env(env, "done")
    digest = env.seal()
    write_profile(env, projects=ledger(proj, "done", name="改了个名字"))
    assert act.main(run_argv(env, digest, "--dry-run")) == 0


def test_guard_digest_unchanged_without_projects():
    """没有台账时摘要与 0.5 之前一致（不因为新增字段让旧封存全部失效）。"""
    g = protect.profile_guard({"protect": ["~/a"]})
    assert "projects" not in g["fields"]
    g2 = protect.profile_guard({"protect": ["~/a"], "projects": {"/x": {"status": "active"}}})
    assert g2["digest"] != g["digest"] and g2["fields"]["projects"] == {"/x": {"status": "active", "deliverables": []}}


# ------------------------------------------------------------ 台账导出行 / 首次判定

def test_ledger_apply_writes_onboarded_and_projects(env, capsys):
    proj, _ = make_proj(env)
    (env.home / "成品").mkdir()
    key = os.path.realpath(proj)
    f = env.run / "ledger.txt"
    f.write_text("叁笙清理 · 项目台账\n"
                 + "台账: " + json.dumps({"项目": {key: {"状态": "做完了", "成品": ["~/成品"], "名称": "我的项目"},
                                               "env:android": {"status": "archived"}}}, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    assert not read_profile_or_empty().get("onboarded_at")
    assert plan.main(["profile", "--apply", str(f)]) == 0
    p = read_profile()
    assert p["onboarded_at"]
    assert p["projects"][key]["status"] == "done" and p["projects"][key]["deliverables"] == ["~/成品"]
    assert p["projects"][key]["name"] == "我的项目" and p["projects"][key]["source"] == "board"
    assert p["projects"]["env:android"]["status"] == "archived"
    assert "项目台账" in capsys.readouterr().out
    assert prefs.is_ledger_export(f.read_text(encoding="utf-8"))
    assert not prefs.is_ledger_export("叁笙清理选择\nscan_id: x\n")


def read_profile_or_empty():
    try:
        return read_profile()
    except OSError:
        return {}


def test_empty_ledger_still_marks_onboarded(env):
    """全默认（没有一行需要改）也算做完第一次设置。"""
    f = env.run / "ledger.txt"
    f.write_text('叁笙清理 · 项目台账\n台账: {"项目":{}}\n', encoding="utf-8")
    assert plan.main(["profile", "--apply", str(f)]) == 0
    assert read_profile()["onboarded_at"]


def test_invalid_ledger_does_not_onboard(env, capsys):
    outside = env.home.parent / "elsewhere"
    outside.mkdir()
    f = env.run / "ledger.txt"
    f.write_text("台账: " + json.dumps({"项目": {str(outside): {"状态": "做完了"}, "env:nope": {"状态": "别碰"},
                                                os.path.realpath(env.work): {"状态": "随便"}}}, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    assert plan.main(["profile", "--apply", str(f)]) == 2
    err = capsys.readouterr().err
    assert "只接受家目录下" in err and "不认识的环境项目" in err and "状态只能是" in err
    assert not read_profile_or_empty().get("onboarded_at") and not read_profile_or_empty().get("projects")
    f.write_text("台账: {坏的\n", encoding="utf-8")
    assert plan.main(["profile", "--apply", str(f)]) == 2


def test_ledger_paths_with_pipe_and_semicolon_not_split(env):
    odd = env.home / "a|b;c 项目"
    odd.mkdir()
    deliv = env.home / "成品|终稿;x"
    deliv.mkdir()
    key = os.path.realpath(odd)
    f = env.run / "ledger.txt"
    f.write_text("台账: " + json.dumps({"项目": {key: {"状态": "进行中", "成品": ["~/成品|终稿;x"]}}}, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    assert plan.main(["profile", "--apply", str(f)]) == 0
    p = read_profile()
    assert list(p["projects"]) == [key]
    assert p["projects"][key]["deliverables"] == ["~/成品|终稿;x"]


def test_ledger_export_is_not_a_cleanup_authorization(env):
    """台账导出只改 profile：同一段文本里就算混进“清理:”行，也不会产生任何清理选择，封存与回执都不动。"""
    proj, build = one_card_env(env, "done")
    digest = env.seal()
    f = env.run / "ledger.txt"
    f.write_text(f"叁笙清理 · 项目台账\nscan_id: scan1\nseal: {digest}\n清理: c01\nsel: abcdef012345\n"
                 '台账: {"项目":{}}\n', encoding="utf-8")
    before = (env.run / "seal.json").read_text()
    assert plan.main(["profile", "--apply", str(f)]) == 0
    assert (env.run / "seal.json").read_text() == before
    assert (build / "a.bin").exists() and not list(env.run.glob("receipt*.json"))


def test_stage_onboarding_then_cleanup(env):
    one_card_env(env, "done")
    env.seal()
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    prop = json.loads((env.run / "proposal.json").read_text())
    assert prop["stage"] == "onboarding" and "台账" in prop["onboarding_hint"]
    f = env.run / "ledger.txt"
    f.write_text('台账: {"项目":{}}\n', encoding="utf-8")
    assert plan.main(["profile", "--apply", str(f)]) == 0
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    prop = json.loads((env.run / "proposal.json").read_text())
    assert prop["stage"] == "cleanup" and "onboarding_hint" not in prop


def test_project_command_updates_status_and_warns(env, capsys):
    proj, _ = make_proj(env)
    key = os.path.realpath(proj)
    assert plan.main(["project", "--key", key, "--status", "做完了"]) == 0
    assert read_profile()["projects"][key]["status"] == "done"
    assert plan.main(["project", "--key", key, "--status", "wrong"]) == 2


# ------------------------------------------------------------ 成长：新项目 / 久未活动

def write_projects_file(env, projects, scan_id="scan1"):
    (env.run / "projects.json").write_text(json.dumps({"schema": "projects/1", "scan_id": scan_id, "projects": projects}),
                                           encoding="utf-8")


def test_new_project_and_stale_active_questions(env, capsys):
    proj, _ = make_proj(env)
    old = env.home / "old"
    old.mkdir()
    write_profile(env, onboarded_at="2026-10-01T00:00:00Z", projects=ledger(old, "active", name="老项目"))
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    env.seal()
    stale = iso(datetime.now(timezone.utc) - timedelta(days=60))
    write_projects_file(env, [
        {"key": os.path.realpath(old), "name": "老项目", "shown": True, "last_active": stale, "bytes": 5},
        {"key": os.path.realpath(proj), "name": "新项目甲", "shown": True, "bytes": 9, "default_status": "unknown"},
        {"key": str(env.home / "hidden"), "name": "折叠的", "shown": False, "bytes": 1}])
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    prop = json.loads((env.run / "proposal.json").read_text())
    qs = {q["type"]: q for q in prop["project_questions"]}
    assert qs["new_projects"]["count"] == 1 and qs["new_projects"]["projects"][0]["name"] == "新项目甲"
    assert qs["stale_active"]["name"] == "老项目" and qs["stale_active"]["idle_days"] >= 60
    assert "新发现 1 个项目" in prop["message"] and "老项目" in prop["message"] and "做完了吗" in prop["message"]
    out = capsys.readouterr().out
    assert "阶段：cleanup" in out


def test_recent_active_project_is_not_asked_and_known_not_new(env):
    proj, _ = make_proj(env)
    write_profile(env, onboarded_at="2026-10-01T00:00:00Z", projects=ledger(proj, "active"))
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    env.seal()
    write_projects_file(env, [{"key": os.path.realpath(proj), "name": "proj", "shown": True,
                               "last_active": iso(datetime.now(timezone.utc) - timedelta(days=3))}])
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    assert json.loads((env.run / "proposal.json").read_text())["project_questions"] == []


def test_no_projects_json_no_questions(env):
    write_profile(env, onboarded_at="2026-10-01T00:00:00Z")
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    env.seal()
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    assert json.loads((env.run / "proposal.json").read_text())["project_questions"] == []


# ------------------------------------------------------------ 飞轮：带项目的规则键

def test_record_choices_adds_project_prefixed_key(env):
    prefs.record_choices("s1", [{"id": "c01", "key": "ai-images/codex/recommended", "bytes": 10, "title": "t",
                                 "projects": ["/x/proj"]}], {"c01"})
    keys = [e["key"] for e in prefs.history()]
    pk = prefs.project_rule_key("/x/proj", "ai-images/codex/recommended")
    assert keys == ["ai-images/codex/recommended", pk]
    assert pk.startswith("project:") and prefs._KEY.match(pk)
    assert prefs.project_rule_key("/x/proj", "bad key") is None
    # 原键格式不变；老调用方（没有 projects）只记一行
    prefs.record_choices("s2", [{"id": "c01", "key": "a/b/safe", "bytes": 1, "title": "t"}], {"c01"})
    assert [e["key"] for e in prefs.history()][-1] == "a/b/safe"


def test_cardkeys_carry_project_keys(env):
    proj, build = one_card_env(env, "done", cards_used=["codex"])
    env.seal()
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    keys = json.loads((env.run / "cardkeys.json").read_text())
    assert keys["c01"]["projects"] == [os.path.realpath(proj)]


# ------------------------------------------------------------ probe projects

def test_decode_claude_project_dir(tmp_path):
    root = tmp_path.resolve() / "我的 家" / "my.proj_x" / "sub-dir"
    root.mkdir(parents=True)
    enc = scan._enc_claude(str(root))
    assert enc.startswith("-")
    assert scan.decode_claude_project_dir(enc) == str(root)
    assert scan.decode_claude_project_dir(enc + "-nope") is None
    assert scan.decode_claude_project_dir("C--Users-x") is None


def git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)


def test_probe_projects_end_to_end(env, capsys):
    home = env.home
    env.mp.setattr(scan, "HOME", str(home))
    now = datetime.now(timezone.utc)
    hot, cold, chat = home / "Hot", home / "Cold", home / "Chat"
    for r in (hot, cold):
        git_init(r)
    (hot / "成品").mkdir()
    chat.mkdir()
    mkfile(chat / "x.bin", b"y" * 3000)
    (home / "视频成品").mkdir()
    mkfile(home / "Library" / "Android" / "sdk" / "img" / "f.bin")
    cdir = home / ".claude" / "projects" / scan._enc_claude(str(chat))
    mkfile(cdir / "s1.jsonl")
    mkfile(cdir / "s2.jsonl")
    nodes = [
        node(hot / "build", id="n1", bytes=3 * 10 ** 9, newest_mtime=iso(now - timedelta(days=1))),
        node(cold / "build", id="n2", bytes=2 * 10 ** 9, newest_mtime=iso(now - timedelta(days=100))),
        node(home / "Library" / "Android" / "sdk" / "img", id="n3", bytes=10 ** 9, newest_mtime=iso(now - timedelta(days=10))),
    ]
    nodes[0]["git"] = {"repo": str(hot), "worktree": str(hot), "role": "ignored", "tracked_files": 0}
    nodes[1]["git"] = {"repo": str(cold), "worktree": str(cold), "role": "ignored", "tracked_files": 0}
    env.write(nodes, [], scan_id="scanP")
    old = time.time() - 100 * 86400
    for p in (cold, cold / ".git", cold / ".git" / "HEAD", cold / ".git" / "index"):
        if p.exists():
            os.utime(p, (old, old))
    assert scan.main(["probe", "projects", "--run", str(env.run)]) == 0
    out = json.loads(capsys.readouterr().out)
    by = {p["name"]: p for p in out["projects"]}
    assert set(by) >= {"Hot", "Cold", "Chat", "视频成品", "安卓开发环境"}
    assert by["Hot"]["key"] == str(hot) and by["Hot"]["kind"] == "git" and by["Hot"]["bytes"] == 3 * 10 ** 9
    assert by["Hot"]["default_status"] == "active" and by["Hot"]["deliverables_guess"] == ["~/Hot/成品"]
    assert by["Cold"]["default_status"] == "done"
    assert by["Chat"]["kind"] == "ai-session" and by["Chat"]["sessions"] == 2
    assert by["Chat"]["claude_dirs"] == [cdir.name] and "session_ids" not in by["Chat"]
    assert by["安卓开发环境"]["key"] == "env:android" and by["安卓开发环境"]["kind"] == "env"
    assert by["视频成品"]["kind"] == "deliverable-dir" and by["视频成品"]["deliverables_guess"] == ["~/视频成品"]
    sizes = [p["bytes"] for p in out["projects"]]
    assert sizes == sorted(sizes, reverse=True) and all(p["shown"] for p in out["projects"])
    assert any(s["source"] == "codex" for s in out["skipped"])           # 没有 Codex 会话：跳过并说明
    saved = json.loads((env.run / "projects.json").read_text())
    assert saved["scan_id"] == "scanP" and saved["schema"] == "projects/1"
    assert next(p for p in saved["projects"] if p["name"] == "Chat")["claude_dirs"] == [cdir.name]


def test_probe_projects_shown_limit_and_no_facts(env, capsys):
    env.mp.setattr(scan, "HOME", str(env.home))
    for i in range(17):
        git_init(env.home / f"P{i:02d}")
        mkfile(env.home / f"P{i:02d}" / "f.bin", b"z" * (1000 + i * 4096))
    run = env.home / "norun"
    run.mkdir()
    assert scan.main(["probe", "projects", "--run", str(run)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["projects"]) == 17 and sum(1 for p in out["projects"] if p["shown"]) == 15
    assert [p["shown"] for p in out["projects"]] == [True] * 15 + [False] * 2
    assert any(s["source"] == "facts" for s in out["skipped"])
    assert not (run / "projects.json").exists()                          # 没有 facts 就不写给 check 用的文件


def test_probe_projects_folds_linked_worktree(env, capsys):
    env.mp.setattr(scan, "HOME", str(env.home))
    main = env.home / "Main"
    git_init(main)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "i"],
                   check=True, capture_output=True)
    wt = env.home / "wt-feature"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt), "-b", "feat"], check=True, capture_output=True)
    env.write([], [], scan_id="scanW")
    assert scan.main(["probe", "projects", "--run", str(env.run)]) == 0
    out = json.loads(capsys.readouterr().out)
    names = [p["name"] for p in out["projects"]]
    assert "Main" in names and "wt-feature" not in names
    assert str(wt) in next(p for p in out["projects"] if p["name"] == "Main")["roots"]


# ------------------------------------------------------------ 发布前评审 中3：台账有进行中 / 别碰项目就必须有本次扫描的项目识别结果

def _unrelated_cache_env(env, status):
    proj = env.home / "proj"
    proj.mkdir()
    d = env.work / "cache"                 # 与项目目录无关的条目：只靠路径认不出归属，会话号 / Claude 目录要 projects.json
    mkfile(d / "a.bin")
    age(d)
    write_profile(env, projects=ledger(proj, status))
    env.write([node(d)], [card("c01", [delete_item(d)])])
    return proj


@pytest.mark.parametrize("status", ["active", "protected"])
def test_active_or_protected_ledger_without_projects_json_blocks_release(env, status):
    _unrelated_cache_env(env, status)
    c = card_out(env.check())
    assert c["verdict_out"] == "keep"
    assert any("项目识别结果" in e and "scan.py probe projects --run" in e for e in c["errors"]), c["errors"]
    assert env.seal() and "c01" not in json.loads((env.run / "seal.json").read_text())["cards"]


def test_projects_json_from_other_scan_does_not_count(env):
    proj = _unrelated_cache_env(env, "active")
    write_projects_file(env, [{"key": os.path.realpath(proj), "name": "proj"}], scan_id="other-scan")
    assert card_out(env.check())["verdict_out"] == "keep"


def test_matching_projects_json_releases_unrelated_item(env):
    proj = _unrelated_cache_env(env, "active")
    write_projects_file(env, [{"key": os.path.realpath(proj), "name": "proj"}])
    c = card_out(env.check())
    assert c["verdict_out"] == "clean" and not c["errors"]


@pytest.mark.parametrize("status", ["done", "archived"])
def test_done_or_archived_ledger_does_not_need_projects_json(env, status):
    _unrelated_cache_env(env, status)
    assert card_out(env.check())["verdict_out"] == "clean"


# ------------------------------------------------------------ 发布前评审 低8：归档降档后仍提醒“删了找不回”

def test_archived_capped_card_keeps_irrecoverable_warning_in_message_and_board(env):
    one_card_env(env, "archived", verdict="clean", preset="all")
    env.check()
    env.seal()
    sealed = json.loads((env.run / "seal.json").read_text())["cards"]["c01"]
    assert sealed["preset"] == "recommended" and sealed["preset_raw"] == "all"      # 降档了，但原本的风险记着
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    prop = json.loads((env.run / "proposal.json").read_text())
    assert prop["options"]["recommended"]["cards"] == ["c01"]                       # 回“好”会删它
    assert "回“好”也会删掉这些找不回的" in prop["message"] and "卡片 c01" in prop["message"]
    # 看板：卡片带 preset_raw，页面据此标出“删了找不回”
    chk = plan.check_plan(str(env.run))[0]
    bc = plan.board_plan(json.loads((env.run / "plan.json").read_text()), chk)["cards"][0]
    assert bc["preset"] == "recommended" and bc["preset_raw"] == "all"
    html = (Path(__file__).resolve().parent.parent / "assets" / "dashboard.html").read_text(encoding="utf-8")
    assert "c.preset_raw==='all'&&c.preset!=='all'" in html and "删了找不回" in html


def test_not_capped_card_has_no_preset_raw_on_board(env):
    one_card_env(env, "done", verdict="clean", preset="all")
    chk = plan.check_plan(str(env.run))[0]
    bc = plan.board_plan(json.loads((env.run / "plan.json").read_text()), chk)["cards"][0]
    assert bc["preset"] == "all" and "preset_raw" not in bc
