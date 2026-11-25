"""quick_act.py：已授权清单执行器。重点是 2026-11-03 误删事故里漏掉的几道检查都会拦住。"""
import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import protect  # noqa: E402
import quick_act as qa  # noqa: E402
import triage  # noqa: E402

OLD = time.time() - 30 * 86400


def old(p):
    for d, ds, fs in os.walk(p):
        for n in fs:
            os.utime(os.path.join(d, n), (OLD, OLD))
    if os.path.isfile(p):
        os.utime(p, (OLD, OLD))


def mk(path, text="x" * 4096):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(text)
    os.utime(path, (OLD, OLD))
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "notes").mkdir(parents=True)
    talk = tmp_path / "talk"
    talk.mkdir()
    monkeypatch.setattr(triage, "COWORK", str(work))
    monkeypatch.setattr(triage, "PRODUCTION_GLOBS", (str(work / "成品"),))
    monkeypatch.setattr(triage, "LEDGER_GLOBS", ())
    monkeypatch.setattr(triage, "CLEANUP_LOG", str(tmp_path / "cleanup-log.md"))
    monkeypatch.setattr(triage, "LOCAL", dict(triage.LOCAL, offdisk_backup=None))
    monkeypatch.setattr(triage, "_REF_CACHE", {})
    monkeypatch.setattr(qa, "TRANSCRIPT_GLOBS", (str(talk / "*.jsonl"),))
    monkeypatch.setattr(qa, "open_paths", lambda: [])
    monkeypatch.setattr(triage, "running_apps", lambda: [])
    monkeypatch.setattr(qa, "known_regenerable", lambda p: "/regen/" in p)
    real = time.time
    monkeypatch.setattr(qa.time, "time", lambda: real() + 2 * 86400)   # ctime 改不了：把时钟往后拨两天，夹具文件就“旧”了
    # 测试常在 Agent 会话里跑，临时目录可能落在该会话的 scratchpad 下（路径含会话号）；屏蔽真实会话号，
    # 免得保护名单把夹具文件认成“当前会话正在用的目录”而整批拒绝（2026-11-04 实测 12 项假失败）。
    for var in protect.SESSION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return {"tmp": tmp_path, "work": work, "talk": talk, "run": str(tmp_path / "run")}


def plan(w, items, text="低风险的直接删，证据充分的全部删", **kw):
    f = w["tmp"] / "items.json"
    f.write_text(json.dumps(items, ensure_ascii=False))
    argv = ["plan", "--items", str(f), "--user-text", text, "--run", w["run"]]
    for k, v in kw.items():
        argv += ["--" + k.replace("_", "-")] + ([] if v is True else [str(v)])
    qa.main(argv)
    return json.load(open(os.path.join(w["run"], "quick-plan.json"), encoding="utf-8"))


def run(w, seal, *extra):
    qa.main(["run", "--run", w["run"], "--seal", seal, *extra])
    return [json.loads(l) for l in open(os.path.join(w["run"], "quick-receipt.jsonl"), encoding="utf-8")]


def item(path, **kw):
    d = {"id": os.path.basename(path), "path": str(path), "scope": "all", "kind": "regenerable", "reason": "测试"}
    d.update(kw)
    return d


def test_validate_rejects_bad_items():
    with pytest.raises(ValueError):
        qa.validate([])
    with pytest.raises(ValueError):
        qa.validate([{"id": "a", "path": "rel/x", "scope": "all", "kind": "regenerable", "reason": "r"}])
    with pytest.raises(ValueError):   # 没备份的过程件不能按文件删
        qa.validate([{"id": "a", "path": "/x/y", "scope": "untracked", "kind": "process", "reason": "r"}])
    with pytest.raises(ValueError):
        qa.validate([{"id": "a", "path": "/x/y", "scope": "files", "files": ["/z/q"], "kind": "backed_up", "reason": "r"}])


def test_regenerable_children_deleted_with_log_and_receipt(world):
    c = world["work"] / "regen" / "cache-dir"
    mk(str(c / "a" / "1.bin")); mk(str(c / "b.bin"))
    p = plan(world, [item(c, scope="children")])
    assert p["results"][0]["status"] == "ok"
    rec = run(world, p["seal"])
    done = [r for r in rec if r["status"] == "deleted"]
    assert len(done) == 2 and {r["status"] for r in rec} <= {"deleted", "intent", "run_started", "run_finished"}
    assert c.is_dir() and not os.listdir(c)          # children：目录本身保留
    log = open(world["tmp"] / "cleanup-log.md", encoding="utf-8").read()
    assert "准备删除" in log and "已删除（2 成功" in log and "发现文件不见了" in log


def test_recent_write_skips_target(world):
    c = world["work"] / "regen" / "busy"
    mk(str(c / "old.bin"))
    open(c / "new.bin", "w").write("fresh")
    p = plan(world, [item(c, quiet_hours=72)])
    r = p["results"][0]
    assert r["status"] == "nothing" and "小时内有写入" in r["targets"][0]["skip"]


def test_code_reference_blocks_unless_user_names_dir(world):
    d = world["work"] / "regen" / "旧季-长效实施-20261019"
    mk(str(d / "c9e03" / "07-v1.mp4"))
    mk(str(world["work"] / "gate.py"), "SRC = '%s/c9e03/07-v1.mp4'\n" % d)
    p = plan(world, [item(d, kind="process")])
    r = p["results"][0]
    assert r["status"] == "refused" and any("代码引用" in x and "需用户点名" in x for x in r["refusals"])
    p = plan(world, [item(d, kind="process")], text="把 旧季-长效实施-20261019 删掉")
    assert p["results"][0]["status"] == "ok" and any("点名" in x for x in p["results"][0]["notes"])


def test_regenerable_cache_referenced_by_code_is_tool_state_even_if_named(world, monkeypatch):
    """项目脚本按路径引用的缓存需要保留，即使请求点名该缓存。"""
    monkeypatch.setattr(triage, "HARD_KEEP", ("/.git",))      # 不受本机配置里的保护词影响
    d = world["work"] / "regen" / "npm" / "_npx"
    mk(str(d / "0123456789abcdef" / "node_modules" / "hyperframes" / "package.json"))
    mk(str(world["work"] / "mix.py"), "engines = sorted((Path.home()/'%s').glob('*/node_modules/hyperframes'))\n" % d)
    p = plan(world, [item(d)], text="同意删除：%s" % d)
    r = p["results"][0]
    assert r["status"] == "refused" and any("工具状态" in x for x in r["refusals"])
    os.remove(world["work"] / "mix.py")                     # 反例：没有代码引用时照常可删
    p = plan(world, [item(d)], text="同意删除：%s" % d)
    assert p["results"][0]["status"] == "ok"


def test_other_agent_transcript_blocks_but_own_transcript_does_not(world):
    d = world["work"] / "proc" / "旧季过程件-20261020"
    mk(str(d / "x.bin"))
    text = "战役树的旧过程文件全部删除掉"
    (world["talk"] / "mine.jsonl").write_text(json.dumps({"text": text + " " + str(d)}, ensure_ascii=False))
    p = plan(world, [item(d, kind="process")], text=text)
    assert p["results"][0]["status"] == "ok"                     # 只有本会话提到：不挡
    (world["talk"] / "other.jsonl").write_text(json.dumps({"text": "继续用 " + str(d)}, ensure_ascii=False))
    p = plan(world, [item(d, kind="process")], text=text)
    r = p["results"][0]
    assert r["status"] == "refused" and any("其他 Agent" in x for x in r["refusals"])
    # 声明成“可再生”也照样查他人活动（评审第 3 条）
    p = plan(world, [item(d, kind="regenerable")], text=text)
    assert p["results"][0]["status"] == "refused"


def test_production_dir_needs_user_naming(world):
    d = world["work"] / "成品" / "第一季"
    mk(str(d / "a.mp4"))
    r = plan(world, [item(d, kind="process")])["results"][0]
    assert r["status"] == "refused" and any("生产目录" in x for x in r["refusals"])


def test_authorized_scope_can_isolate_with_transcript_mentions(world):
    process = world["work"] / "proc" / "old-pastes"
    cache = world["work"] / "regen" / "old-cache"
    mk(str(process / "a.bin"))
    mk(str(cache / "a.bin"))
    text = "把上面列出的剩余项清理掉"
    (world["talk"] / "mine.jsonl").write_text(json.dumps({"text": text}, ensure_ascii=False))
    (world["talk"] / "other.jsonl").write_text(f"See {process} and {cache}")
    items = [item(process, kind="process"), item(cache)]

    plan_result = plan(world, items, text=text, isolate_authorized=True)
    process_result, cache_result = plan_result["results"]
    assert process_result["status"] == "ok" and process_result["method"] == "quarantine"
    assert cache_result["status"] == "refused" and any("其他 Agent" in x for x in cache_result["refusals"])
    assert qa.seal_of(dict(plan_result, isolate_authorized=False)) != plan_result["seal"]
    receipts = run(world, plan_result["seal"])
    assert any(r["status"] == "quarantined" for r in receipts)
    assert not process.exists() and cache.is_dir()

    mk(str(process / "a.bin"))
    consumer = world["work"] / "consumer.py"
    mk(str(consumer), f"PATH = {str(process)!r}")
    result = plan(world, items[:1], text=text, isolate_authorized=True)["results"][0]
    assert result["status"] == "refused" and any("代码引用" in x for x in result["refusals"])


def test_process_is_quarantined_then_purged(world):
    d = world["work"] / "proc" / "试做-20261001"
    mk(str(d / "a.bin"))
    p = plan(world, [item(d, kind="process")])
    assert p["results"][0]["method"] == "quarantine"
    rec = run(world, p["seal"])
    q = [r for r in rec if r["status"] == "quarantined"][-1]
    assert not d.exists()
    moved = q["moved_to"]
    assert os.path.isdir(moved) and qa.QUARANTINE_TAG in moved and "mv -n" in q["restore"]
    qa.main(["purge", "--run", world["run"], "--days", "7"])
    assert os.path.isdir(moved)                                   # 未满 7 天不删
    qa.main(["purge", "--run", world["run"], "--days", "0"])
    assert not os.path.exists(moved)


def test_backed_up_requires_adapter_and_all_files_verified(world, monkeypatch):
    d = world["work"] / "proc" / "已备份-20261001"
    mk(str(d / "a.bin")); mk(str(d / "b.bin"))
    r = plan(world, [item(d, kind="backed_up")])["results"][0]
    assert r["status"] == "refused" and any("未配置盘外备份适配器" in x for x in r["refusals"])
    ad = world["tmp"] / "adapter.py"
    mode = world["tmp"] / "mode"
    ad.write_text(
        "import json,sys\nps=json.load(open(sys.argv[sys.argv.index('--paths-file')+1]))\n"
        "m=open(%r).read()\n"
        "if sys.argv[1]=='upload': print(json.dumps({'uploaded':ps,'failed':[]})); open(%r,'w').write('ok'); sys.exit()\n"
        "ok=ps if m=='ok' else ps[1:]\n"
        "print(json.dumps({'ok':ok,'missing':[p for p in ps if p not in ok],'changed':[],"
        "'sample':[{'path':p,'ok':True} for p in ok[:1]],'restore_hint':'网盘按原路径取回'}))\n" % (str(mode), str(mode)))
    monkeypatch.setattr(triage, "LOCAL", dict(triage.LOCAL, offdisk_backup={"label": "测试盘", "adapter": [sys.executable, str(ad)]}))
    mode.write_text("partial")
    r = plan(world, [item(d, kind="backed_up")])["results"][0]
    assert r["status"] == "refused" and any("没有确认的盘外备份" in x for x in r["refusals"])
    p = plan(world, [item(d, kind="backed_up")], upload_missing=True)   # 先补传再核对
    r = p["results"][0]
    assert r["status"] == "ok" and r["restore"] == "网盘按原路径取回"
    rec = run(world, p["seal"])
    assert any(r["status"] == "deleted" for r in rec) and not d.exists()


def test_seal_mismatch_and_replaced_target(world):
    d = world["work"] / "regen" / "c2"
    mk(str(d / "a" / "1.bin"))
    p = plan(world, [item(d, scope="children")])
    with pytest.raises(SystemExit):
        run(world, "0000")
    os.rename(d / "a", d / "a-old")
    mk(str(d / "a" / "1.bin"))                                    # 同名但换了 inode
    rec = run(world, p["seal"])
    assert not any(r.get("status") == "deleted" and r["target"].endswith("/a") for r in rec)
    assert (d / "a").exists()


def test_git_tracked_refused_and_untracked_scope(world):
    repo = world["tmp"] / "regen" / "repo"
    sub = repo / "line"
    mk(str(sub / "kept.txt"))
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "line/kept.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init",
                    "--date=2020-01-01T00:00:00"], check=True,
                   env=dict(os.environ, GIT_COMMITTER_DATE="2020-01-01T00:00:00"))
    mk(str(sub / "junk.bin"))
    r = plan(world, [item(sub)])["results"][0]
    assert r["status"] == "refused" and any("Git 跟踪文件" in x for x in r["refusals"])
    p = plan(world, [item(sub, scope="untracked", kind="regenerable")])
    assert p["results"][0]["status"] == "ok"
    run(world, p["seal"])
    assert (sub / "kept.txt").exists() and not (sub / "junk.bin").exists()
    r = plan(world, [item(repo, scope="children")])["results"][0]   # 仓库根不整体处理
    assert any("仓库根" in x for x in r["refusals"])


def test_tracked_scripts_in_dir_block_untracked_cleanup(world):
    repo = world["tmp"] / "repo2"
    sub = repo / "season"
    mk(str(sub / "gate.py"), "print(1)\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "season/gate.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    mk(str(sub / "frames" / "00.png"))
    r = plan(world, [item(sub, scope="untracked", kind="regenerable")])["results"][0]
    assert r["status"] == "refused" and any("已跟踪脚本" in x for x in r["refusals"])


def test_dot_git_and_shallow_paths_refused(world):
    g = world["work"] / "proj" / ".git"
    mk(str(g / "HEAD"))
    r = plan(world, [item(g)])["results"][0]
    assert r["status"] == "refused"
    r = plan(world, [item(world["work"] / "notes")])["results"][0]
    assert any("第一层目录" in x for x in r["refusals"])


def test_unregistered_regenerable_is_quarantined_not_deleted(world):
    d = world["work"] / "proc" / "自称可重建-20261001"
    mk(str(d / "a.bin"))
    r = plan(world, [item(d, kind="regenerable")])["results"][0]
    assert r["kind"] == "process" and r["method"] == "quarantine" and any("extra_tier1" in x for x in r["notes"])
    r = plan(world, [item(d, scope="untracked", kind="regenerable")])["results"][0]
    assert r["status"] == "refused"


def test_tampered_plan_is_rejected(world):
    d = world["work"] / "proc" / "旧过程件-20261001"
    mk(str(d / "x.bin"))
    mk(str(world["work"] / "gate.py"), "P = '%s'\n" % d)
    p = plan(world, [item(d, kind="process")])
    assert p["results"][0]["status"] == "refused"
    f = os.path.join(world["run"], "quick-plan.json")
    j = json.load(open(f, encoding="utf-8"))
    j["user_text"] += " 旧过程件-20261001"           # 事后往原话里补目录名
    j["results"][0]["status"] = "ok"
    json.dump(j, open(f, "w", encoding="utf-8"), ensure_ascii=False)
    with pytest.raises(SystemExit):
        run(world, p["seal"])
    assert d.exists()


def test_naming_needs_path_or_long_exact_name(world):
    top = None
    it = item(world["work"] / "proc" / "原片")
    assert not qa.named(it, top, "原片相关的缓存帧删了")          # 短名不算点名
    it = item(world["work"] / "proc" / "out")
    assert not qa.named(it, top, "without the cache")
    it = item(world["work"] / "proc" / "旧季-长效实施-20261019")
    assert qa.named(it, top, "把 旧季-长效实施-20261019 删掉")
    assert not qa.named(it, top, "把 旧季-长效实施-20261019b 删掉")
    assert qa.named(it, top, "删 proc/旧季-长效实施-20261019")


def test_quarantined_items_never_targeted_again(world):
    parent = world["work"] / "regen" / "p"
    sub = parent / "sub"
    mk(str(sub / "a.bin"))
    os.rename(sub, str(sub) + ".待删-20261103")
    mk(str(parent / "other.bin"))
    p = plan(world, [item(parent, scope="children")])
    assert [os.path.basename(t["path"]) for t in p["results"][0]["targets"]] == ["other.bin"]
    r = plan(world, [item(parent)])["results"][0]
    assert r["status"] == "nothing" and "隔离" in r["targets"][0]["skip"]


def test_code_refs_ignore_gitignore_and_longer_key_not_swallowed(world):
    (world["work"] / ".gitignore").write_text("/_ws/\n")
    subprocess.run(["git", "init", "-q", str(world["work"])], check=True)
    a = world["work"] / "proc" / "out"
    b = world["work"] / "proc" / "out2"
    mk(str(a / "x.bin")); mk(str(b / "y.bin"))
    mk(str(world["work"] / "_ws" / "tree" / "gate.py"), "SRC = '%s/y.bin'\n" % b)   # 被 .gitignore 忽略的地方
    p = plan(world, [item(a, kind="process"), item(b, kind="process")])
    ra, rb = p["results"]
    assert rb["status"] == "refused" and any("代码引用" in x for x in rb["refusals"])
    assert not any("代码引用" in x for x in ra.get("refusals", []))


def test_adapter_must_list_every_file_as_ok(world, monkeypatch):
    d = world["work"] / "proc" / "已备份-20261002"
    mk(str(d / "a.bin")); mk(str(d / "yarn.lock"))
    ad = world["tmp"] / "adapter2.py"
    ad.write_text("import json\nprint(json.dumps({}))\n")      # 什么都不报：不能当成全部通过
    monkeypatch.setattr(triage, "LOCAL", dict(triage.LOCAL, offdisk_backup={"adapter": [sys.executable, str(ad)]}))
    r = plan(world, [item(d, kind="backed_up")], sample=0)["results"][0]
    assert r["status"] == "refused" and any("没有确认的盘外备份" in x for x in r["refusals"])


def test_lsof_physical_path_matches(world, monkeypatch):
    d = world["work"] / "regen" / "held"
    mk(str(d / "a.bin"))
    real = os.path.realpath(str(d / "a.bin"))
    monkeypatch.setattr(qa, "open_paths", lambda: sorted({real}))
    link = world["tmp"] / "lnk"
    os.symlink(world["work"], link)
    r = plan(world, [item(link / "regen" / "held")])["results"][0]
    assert r["status"] == "nothing" and "被进程打开" in r["targets"][0]["skip"]


def test_self_transcript_ambiguous_or_unverified(world):
    d = world["work"] / "proc" / "旧过程件-20261005"
    mk(str(d / "x.bin"))
    text = "战役树的旧过程文件全部删除掉吧"
    for n in ("s1.jsonl", "s2.jsonl"):
        (world["talk"] / n).write_text(json.dumps({"text": text + " " + str(d)}, ensure_ascii=False))
    r = plan(world, [item(d, kind="process")], text=text)["results"][0]
    assert r["status"] == "refused" and any("无法判断哪个是本会话" in x for x in r["refusals"])
    fake = world["tmp"] / "fake.jsonl"
    fake.write_text("nothing")
    r = plan(world, [item(d, kind="process")], text=text, self_transcript=str(fake))["results"][0]
    assert any("找不到用户原话" in x for x in r["refusals"])
    r = plan(world, [item(d, kind="process")], text=text, self_transcript=str(world["talk"] / "s1.jsonl"))["results"][0]
    assert any("其他 Agent" in x for x in r["refusals"])   # s2 仍算他人


# ---------- 本会话派过子 Agent（回归：run 曾把子 Agent 记录当成手工指定的记录去核对原话，整批拒绝） ----------
SELF_TEXT = "其他不需要的过程文件你都可以全部删掉"


def note(path, text):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    open(str(path), "w", encoding="utf-8").write(json.dumps({"text": text}, ensure_ascii=False))


def with_subagents(world, monkeypatch):
    talk = world["talk"]
    monkeypatch.setattr(qa, "TRANSCRIPT_GLOBS", (str(talk / "*.jsonl"), str(talk / "*" / "subagents" / "**" / "*.jsonl")))
    return talk


def test_run_self_inputs_only_drops_subagents_of_listed_mains():
    main, own = "/p/proj/s1.jsonl", "/p/proj/s1/subagents/agent-a.jsonl"
    wf, stray = "/p/proj/s1/subagents/workflows/wf_1/journal.jsonl", "/p/proj/s2/subagents/agent-b.jsonl"
    assert qa.run_self_inputs({"self_transcripts": [main, own, wf, stray]}) == [main, stray]
    assert qa.run_self_inputs({"self_transcripts": [own]}) == [own]      # 列表里没有它的主记录：照旧核对，不放行
    assert qa.run_self_inputs({}) == [] and qa.run_self_inputs({"self_transcripts": None}) == []


def test_run_counts_own_subagents_as_self(world, monkeypatch):
    talk = with_subagents(world, monkeypatch)
    c = world["work"] / "regen" / "验收包-20261104"
    mk(str(c / "a.bin")); mk(str(c / "b.bin"))
    note(talk / "sess-mine.jsonl", SELF_TEXT)
    sub = talk / "sess-mine" / "subagents" / "agent-a1.jsonl"
    note(sub, "复核 %s" % c)                                   # 子 Agent 提到了这个目录，但它的记录里没有用户原话
    p = plan(world, [item(c, scope="children")], text=SELF_TEXT)
    assert p["results"][0]["status"] == "ok" and str(sub) in p["self_transcripts"]
    note(talk / "sess-mine" / "subagents" / "workflows" / "wf_1" / "agent-b2.jsonl", "再看一眼 %s" % c)   # plan 之后新派的
    rec = run(world, p["seal"])
    assert not [r for r in rec if r["status"] == "refused"], rec
    assert len([r for r in rec if r["status"] == "deleted"]) == 2 and not os.listdir(c)


def test_run_still_refuses_when_other_sessions_subagent_uses_it(world, monkeypatch):
    """反例：只有本会话名下的子 Agent 算自己；别的会话的子 Agent 在 plan 之后提到同一目录，run 照样拒绝。"""
    talk = with_subagents(world, monkeypatch)
    c = world["work"] / "regen" / "验收包-20261105"
    mk(str(c / "a.bin"))
    note(talk / "sess-mine.jsonl", SELF_TEXT)
    note(talk / "sess-mine" / "subagents" / "agent-a1.jsonl", "复核 %s" % c)
    p = plan(world, [item(c, scope="children")], text=SELF_TEXT)
    assert p["results"][0]["status"] == "ok"
    note(talk / "sess-other" / "subagents" / "agent-c3.jsonl", "还在用 %s" % c)
    rec = run(world, p["seal"])
    refused = [r for r in rec if r["status"] == "refused"]
    assert refused and any("其他 Agent" in w for w in refused[0]["why"]), rec
    assert not [r for r in rec if r["status"] == "deleted"] and os.listdir(c)


def _cold_world(world, monkeypatch, extra=("视频成品",)):
    ad = world["tmp"] / "cold_adapter.py"
    ad.write_text("import json,sys\nps=json.load(open(sys.argv[sys.argv.index('--paths-file')+1]))\n"
                  "print(json.dumps({'ok':ps,'missing':[],'changed':[],'sample':[{'path':p,'ok':True} for p in ps[:1]],'restore_hint':'取回'}))\n")
    monkeypatch.setattr(triage, "LOCAL", dict(triage.LOCAL, offdisk_backup={"label": "冷存", "adapter": [sys.executable, str(ad)]},
                                              hard_keep_extra=list(extra)))
    monkeypatch.setattr(triage, "HARD_KEEP", ("cleanup-archive", "-slot", "/.git") + tuple(extra))


def test_cold_batch_named_passes_user_hard_keep(world, monkeypatch):
    _cold_world(world, monkeypatch)
    d = world["work"] / "视频成品" / "旧季-20261001"
    mk(str(d / "ep1.mp4"))
    it = item(d, kind="backed_up", cold_batch="b1")
    r = plan(world, [it], text="同意冷存并删除本地：视频成品/旧季-20261001")["results"][0]
    assert not any("硬保护" in x for x in r["refusals"]), r["refusals"]
    assert any("已由用户点名放行" in x for x in r["notes"])


def test_cold_hard_keep_refused_without_naming_or_batch(world, monkeypatch):
    _cold_world(world, monkeypatch)
    d = world["work"] / "视频成品" / "旧季-20261001"
    mk(str(d / "ep1.mp4"))
    r = plan(world, [item(d, kind="backed_up", cold_batch="b1")], text="低风险的直接删，证据充分的全部删")["results"][0]
    assert any("硬保护" in x for x in r["refusals"])                    # 没点名
    r = plan(world, [item(d, kind="backed_up")], text="同意冷存并删除本地：视频成品/旧季-20261001")["results"][0]
    assert any("硬保护" in x for x in r["refusals"])                    # 不是冷存批次


def test_cold_batch_never_passes_builtin_hard_keep(world, monkeypatch):
    _cold_world(world, monkeypatch)
    d = world["work"] / "proj" / "site-slot" / "old"
    mk(str(d / "a.bin"))
    r = plan(world, [item(d, kind="backed_up", cold_batch="b1")], text="同意冷存并删除本地：proj/site-slot/old")["results"][0]
    assert any("硬保护" in x for x in r["refusals"])


def test_normalize_and_named_terminal_quirks_with_negatives(world):
    # 正例：折行、花括号、吞掉的 ~、两层前缀通配、同父上下文短目录名
    # 用 world["work"] 模拟 示例工作区，测试花括号、折行、两层通配与严格反例
    w = world["work"]
    u = ("直接永久删除 "
         f"{w}/web/{{dist,.local}} 与 "
         f"{w}/tmp-\n  work/sub-task-alpha 与 "
         f"{w}/batches/math-* 与 "
         f"{w}/历史/子目录 并保留其他")
    assert qa.named(item(w / "web" / "dist"), None, u)
    assert qa.named(item(w / "web" / ".local"), None, u)
    assert qa.named(item(w / "tmp-work" / "sub-task-alpha"), None, u)
    assert qa.named(item(w / "batches" / "math-20261105"), None, u)
    assert qa.named(item(w / "历史" / "子目录"), None, f"直接永久删除 {w}/历史 下的 子目录")

    # stripped-tilde (/... under HOME)
    rel_item = item(qa.HOME + "/.sansheng/video-study/dl")
    assert qa.named(rel_item, None, "清理 /.sansheng/video-\n  study/dl")

    # 反例：空输入、前缀碰撞、子路径不得放大到父目录或兄弟目录、括号内“保留/避开…”、模糊表述、裸通配符
    assert not qa.named(item(w / "web" / "dist"), None, "")
    assert not qa.named(item(w / "proc" / "out"), None, f"直接永久删除 {w}/proc/out2")
    assert not qa.named(item(w / "a" / "b"), None, f"直接永久删除 {w}/a/b/c")
    assert not qa.named(item(w / "a" / "b" / "d"), None, f"直接永久删除 {w}/a/b/c")
    assert not qa.named(item(w / "_workspace" / "sample-build-20260101"), None, "直接永久删除（保留sample-build-20260101）")
    assert not qa.named(item(w / "视频成品" / "生产线改造验收-20261015"), None, "直接永久删除 10月独立试片")
    assert not qa.named(item(w / "web" / "dist"), None, "直接永久删除 *")


def test_verified_obsolete_gates_and_execution(world, monkeypatch):
    d = world["work"] / "proc" / "旧过程目录-20261101"
    mk(str(d / "a.bin"))
    # 反例 1：缺明确直删意图词 -> 拒绝
    r1 = plan(world, [item(d, kind="verified_obsolete")], text=f"整理一下 {d}")["results"][0]
    assert r1["status"] == "refused" and any("直删意图" in x for x in r1["refusals"])
    # 反例 2：未点名路径 -> 拒绝
    r2 = plan(world, [item(d, kind="verified_obsolete")], text="直接永久删除不需要的文件")["results"][0]
    assert r2["status"] == "refused" and any("明确写出路径" in x for x in r2["refusals"])
    # 反例 3：即使点名且含直删意图，命中 HOME_RULES 的 Documents 也必须拒绝
    doc_dir = qa.HOME + "/Documents/Codex"
    monkeypatch.setattr(os.path, "lexists", lambda p, orig=os.path.lexists: True if p == doc_dir else orig(p))
    r3 = plan(world, [item(doc_dir, kind="verified_obsolete")], text=f"直接永久删除 {doc_dir}")["results"][0]
    assert r3["status"] == "refused" and any("保护名单" in x for x in r3["refusals"])
    # 正例：意图 + 点名 -> 直接永久删除（method=delete）并真删
    p = plan(world, [item(d, kind="verified_obsolete")], text=f"直接永久删除 {d}")
    assert p["results"][0]["status"] == "ok" and p["results"][0]["method"] == "delete"
    rec = run(world, p["seal"])
    assert any(r.get("status") == "deleted" for r in rec) and not d.exists()


def test_ephemeral_run_dir_rejected(world, monkeypatch):
    assert qa.ephemeral_run_dir_problem("") is not None
    assert qa.ephemeral_run_dir_problem("/tmp/foo-run") is not None
    assert qa.ephemeral_run_dir_problem("/private/tmp/foo-run") is not None
    assert qa.ephemeral_run_dir_problem(world["run"]) is None   # pytest-of- 允许
    f = world["tmp"] / "it.json"
    d = world["work"] / "regen" / "x"
    mk(str(d / "1.bin"))
    f.write_text(json.dumps([item(d)]))
    with pytest.raises(SystemExit):
        qa.main(["plan", "--items", str(f), "--user-text", f"直接永久删除 {d}", "--run", "/tmp/bad-run-dir"])


def test_keep_names_and_purge_orphan_and_all(world, monkeypatch):
    d = world["work"] / "regen" / "keep-test"
    mk(str(d / "drop.bin")); mk(str(d / "aligned" / "keep.txt"))
    p = plan(world, [item(d, scope="children", keep_names=["aligned"])])
    assert [os.path.basename(t["path"]) for t in p["results"][0]["targets"]] == ["drop.bin"]
    run(world, p["seal"])
    assert not (d / "drop.bin").exists() and (d / "aligned" / "keep.txt").exists()

    # 孤儿 .待删-YYYYMMDD：原文件还在时不删；无授权提前删拒；原文件消失 + 授权后删掉
    odir = world["work"] / "orphans"
    live_mp4 = mk(str(odir / "c01.mp4"))
    q_mp4 = mk(str(odir / "c01.mp4.待删-20261110"))
    with pytest.raises(SystemExit):
        qa.main(["purge", "--orphan-scan", str(odir), "--days", "0"])
    qa.main(["purge", "--orphan-scan", str(odir), "--days", "0", "--user-text", "清理 .待删-"])
    assert os.path.exists(q_mp4)   # 原路径 c01.mp4 还在，不能删
    os.remove(live_mp4)
    qa.main(["purge", "--orphan-scan", str(odir), "--days", "0", "--user-text", "清理 .待删-"])
    assert not os.path.exists(q_mp4)


def test_antigravity_session_grouping_and_subagent(world, monkeypatch):
    ag = world["tmp"] / ".gemini" / "antigravity-cli"
    cid = "11111111-2222-3333-4444-555555555555"
    sub_cid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    tr = ag / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
    db = ag / "conversations" / f"{cid}.db"
    sub_tr = ag / "brain" / sub_cid / ".system_generated" / "logs" / "transcript.jsonl"
    monkeypatch.setattr(qa, "TRANSCRIPT_GLOBS", (
        str(ag / "conversations" / "*.db"),
        str(ag / "brain" / "*" / ".system_generated" / "logs" / "transcript*.jsonl"),
    ))
    txt = "请把 /x/y/旧会话目录20261118 清理掉"
    tr.parent.mkdir(parents=True, exist_ok=True)
    tr.write_text(json.dumps({"user": txt, "conversationId": sub_cid}, ensure_ascii=False) + "\n", encoding="utf-8")
    note(db, txt)
    note(sub_tr, "子 agent 只读复核 " + str(world["work"] / "regen" / "ag-dir"))
    files = qa.recent_transcripts(72)
    mine, err = qa.self_transcripts(files, txt, [])
    assert err is None and str(tr) in mine and str(db) in mine and str(sub_tr) in mine


def test_cold_batch_rechecks_consumers_added_after_seal(world, monkeypatch):
    _cold_world(world, monkeypatch)
    root = world["work"] / "library"
    d = root / "originals"
    mk(str(d / "sample.bin"))
    triage.LOCAL["cold_storage"] = {"consumer_scopes": [{"source": str(d), "roots": [str(root)]}]}
    p = plan(world, [item(d, kind="backed_up", cold_batch="b1")], text="同意冷存并删除本地：library/originals")
    assert p["results"][0]["status"] == "ok"
    (root / "reader").symlink_to("originals/sample.bin")
    rec = run(world, p["seal"])
    assert any(r["status"] == "refused" and "软链接" in str(r["why"]) for r in rec)
    assert (d / "sample.bin").is_file()


def test_regenerable_cache_ignores_cleanup_tool_self_refs_but_keeps_real_code_ref(world, monkeypatch):
    """清理工具自身的分类清单/测试提及可再生缓存时不应误拒，但真实业务脚本引用仍须按工具状态拦截。"""
    monkeypatch.setattr(triage, "HARD_KEEP", ("/.git",))
    d = world["work"] / "regen" / "gradle" / "caches"
    mk(str(d / "modules-2" / "pkg.jar"))
    # 工具自身分类清单与测试提到该缓存路径 → 放行（与 triage.tool_state_refs 口径一致）
    mk(str(world["work"] / "skills" / "sansheng-disk-cleanup" / "scripts" / "producers.json"),
       json.dumps({"path": str(d)}))
    mk(str(world["work"] / "skills" / "sansheng-disk-cleanup" / "tests" / "test_scan.py"),
       f'SAMPLE = "{d}"\n')
    p = plan(world, [item(d)], text=f"同意删除：{d}")
    assert p["results"][0]["status"] == "ok", p["results"][0]
    # 反例：一旦有真实业务脚本按路径引用，点名也必须按工具状态拒绝
    real_script = mk(str(world["work"] / "scripts" / "use_gradle.py"), f'CACHE = "{d}"\n')
    p2 = plan(world, [item(d)], text=f"同意删除：{d}")
    assert p2["results"][0]["status"] == "refused" and any("工具状态" in x for x in p2["results"][0]["refusals"])
    os.remove(real_script)
