"""quick_act.py：已授权清单执行器。重点是 2026-11-03 误删事故里漏掉的几道检查都会拦住。"""
import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
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
    p = plan(world, [item(d, kind="regenerable")])
    r = p["results"][0]
    assert r["status"] == "refused" and any("代码引用" in x and "需用户点名" in x for x in r["refusals"])
    p = plan(world, [item(d, kind="regenerable")], text="把 旧季-长效实施-20261019 删掉")
    assert p["results"][0]["status"] == "ok" and any("点名" in x for x in p["results"][0]["notes"])


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
