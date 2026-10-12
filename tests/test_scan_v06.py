"""0.6 扫描侧：probe projects 的 tools / deliverable_candidates / setup_facts、目录身份、claude 临时根、done-evidence。

夹具全在临时 HOME 内；time_machine 与 tmutil 一律 monkeypatch，不碰真实系统。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, mkfile, node, protect  # noqa: E402

import scan  # noqa: E402

REAL_TM = scan.time_machine_last          # fixture 会把它换成固定值，这里留一份原函数


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(scan, "HOME", str(e.home))
    monkeypatch.setattr(scan, "time_machine_last", lambda: "2026-10-01T00:00:00Z")
    return e


def with_card(n, card):
    n["producer"] = {"tool": None, "card": card, "hint": None}
    return n


def probe(env, capsys, nodes=()):
    env.write(list(nodes), [], scan_id="scanV6")
    assert scan.main(["probe", "projects", "--run", str(env.run)]) == 0
    return json.loads(capsys.readouterr().out)


# ───────── tools ─────────

def test_tools_summed_from_card_nodes_topmost_only(env, capsys):
    a = env.home / ".android"
    sdk = a / "sdk"
    codex = env.home / ".codex"
    nodes = [
        with_card(node(a, id="n1", bytes=4 * 10 ** 9, newest_mtime="2026-10-01T00:00:00Z"), "android"),
        with_card(node(sdk, id="n2", bytes=3 * 10 ** 9, newest_mtime="2026-10-09T00:00:00Z"), "android"),   # 在 n1 里面，不重复计
        with_card(node(codex, id="n3", bytes=7 * 10 ** 9, newest_mtime="2026-10-05T00:00:00Z"), "codex"),
        with_card(node(env.home / ".cache" / "x", id="n4", bytes=1), "codex"),
    ]
    for n in nodes:
        Path(n["path"]).mkdir(parents=True, exist_ok=True)
    out = probe(env, capsys, nodes)
    tools = {t["key"]: t for t in out["tools"]}
    assert tools["android"]["bytes"] == 4 * 10 ** 9 and tools["android"]["name"] == "Android 模拟器与开发包"
    assert tools["android"]["paths"] == ["~/.android"]
    assert tools["codex"]["bytes"] == 7 * 10 ** 9 + 1
    assert tools["codex"]["last_used"] == "2026-10-05T00:00:00Z"
    assert [t["bytes"] for t in out["tools"]] == sorted((t["bytes"] for t in out["tools"]), reverse=True)


def test_tools_fall_back_to_known_paths_when_no_nodes(env, capsys):
    mkfile(env.home / ".ollama" / "models" / "m.bin", b"z" * 10000)
    out = probe(env, capsys)
    t = {x["key"]: x for x in out["tools"]}
    assert t["model-caches"]["bytes"] > 0 and t["model-caches"]["paths"] == ["~/.ollama"]
    assert "android" not in t


# ───────── deliverable_candidates ─────────

def test_deliverable_candidates_by_name_and_media(env, capsys):
    for i in range(3):
        mkfile(env.home / "Movies" / "成片" / ("a%d.mp4" % i))
    for i in range(35):
        mkfile(env.home / "素材堆" / ("p%d.png" % i), b"p")
    mkfile(env.home / "闲杂" / "a.txt")
    mkfile(env.home / "Library" / "export" / "x.mp4")           # Library 不扫
    mkfile(env.home / ".hidden" / "final" / "x.mp4")             # 隐藏目录不扫
    out = probe(env, capsys)
    c = {x["path"]: x for x in out["deliverable_candidates"]}
    assert "~/Movies/成片" in c and c["~/Movies/成片"]["files"] == 3 and "成片" in c["~/Movies/成片"]["why"]
    assert "~/素材堆" in c and c["~/素材堆"]["files"] == 35
    assert not any("闲杂" in p or "Library" in p or ".hidden" in p for p in c)


def test_deliverable_candidates_capped_at_20(env):
    for i in range(30):
        mkfile(env.home / ("final%02d" % i) / "a.mp4")
    got = scan.collect_deliverable_candidates()
    assert len(got) == 20


def test_deliverable_candidates_skip_git_repos(env):
    repo = env.home / "proj"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    mkfile(repo / "output" / "a.mp4")
    assert scan.collect_deliverable_candidates() == []


# ───────── setup_facts ─────────

def test_setup_facts_all_items(env, capsys):
    h = env.home
    mkfile(h / ".codex" / "sessions" / "s.jsonl", b"s" * 8192)
    mkfile(h / ".claude" / "projects" / "p" / "a.jsonl", b"s" * 8192)
    mkfile(h / ".codex" / "generated_images" / "i.png", b"i" * 8192)
    mkfile(h / "Downloads" / "App.dmg", b"d" * 8192)
    mkfile(h / "Downloads" / "notes.txt", b"n" * 8192)
    mkfile(h / "Desktop" / "屏幕录制 1.mov", b"v" * 8192)
    mkfile(h / "Movies" / "clip.mp4", b"v" * 8192)
    mkfile(h / "Library" / "Mobile Documents" / "x" / "doc.bin", b"c" * 8192)
    mkfile(h / "Library" / "CloudStorage" / "Drive" / "y.bin", b"c" * 8192)
    mkfile(h / "Big" / "f.bin", b"b" * 40000)
    out = probe(env, capsys)
    f = out["setup_facts"]
    assert f["ai_sessions_bytes"] >= 16384
    assert f["ai_images_bytes"] >= 8192
    assert 8192 <= f["installers_bytes"] < 16384        # 只有 dmg，txt 不计
    assert f["recordings_bytes"] >= 16384
    assert f["cloud_local_bytes"] >= 16384
    assert f["time_machine_last"] == "2026-10-01T00:00:00Z"
    assert f["top_dirs"][0]["path"] == "~/Big" and len(f["top_dirs"]) <= 12
    assert f["worktrees"] == 0


def test_setup_facts_cloud_folders_and_pipelines(env, capsys):
    """第 11 题的数据：iCloud 云盘与网盘根下第二层文件夹的本机体积，按提供方归类；pipelines 卡的节点是否存在。"""
    h = env.home
    mkfile(h / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "视频素材" / "a.mov", b"v" * 60000)
    mkfile(h / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "视频素材" / "b.mov", b"v" * 60000)
    mkfile(h / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "小文件" / "t.txt", b"t" * 100)
    mkfile(h / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "笔记.md", b"n" * 20000)
    mkfile(h / "Library" / "CloudStorage" / "Dropbox-团队" / "项目" / "x.bin", b"d" * 50000)
    mkfile(h / "Library" / "CloudStorage" / "OneDrive-个人" / "照片" / "y.bin", b"o" * 40000)
    mkfile(h / "Library" / "CloudStorage" / "GoogleDrive-a@b.com" / "My Drive" / "课程" / "z.bin", b"g" * 30000)
    mkfile(h / "Library" / "CloudStorage" / "坚果云-x" / "同步" / "w.bin", b"j" * 10000)
    out = probe(env, capsys)
    f = out["setup_facts"]
    rows = {r["path"]: r for r in f["cloud_folders"]}
    assert set(rows) == {"~/Library/Mobile Documents/com~apple~CloudDocs/视频素材",
                         "~/Library/Mobile Documents/com~apple~CloudDocs/小文件",
                         "~/Library/Mobile Documents/iCloud~md~obsidian",
                         "~/Library/CloudStorage/Dropbox-团队/项目", "~/Library/CloudStorage/OneDrive-个人/照片",
                         "~/Library/CloudStorage/GoogleDrive-a@b.com/My Drive/课程", "~/Library/CloudStorage/坚果云-x/同步"}
    prov = {p.split("/")[-1]: r["provider"] for p, r in rows.items()}
    assert prov == {"视频素材": "iCloud Drive", "小文件": "iCloud Drive", "iCloud~md~obsidian": "iCloud Drive", "项目": "Dropbox",
                    "照片": "OneDrive", "课程": "Google Drive", "同步": "其他"}
    big = rows["~/Library/Mobile Documents/com~apple~CloudDocs/视频素材"]
    assert big["files"] == 2 and big["local_bytes"] >= 120000
    sizes = [r["local_bytes"] for r in f["cloud_folders"]]
    assert sizes == sorted(sizes, reverse=True) and f["cloud_folders"][0]["path"].endswith("视频素材")
    assert f["pipelines"] is False
    n = with_card(node(h / "Work" / "out", id="n9", bytes=5), "pipelines")
    Path(n["path"]).mkdir(parents=True, exist_ok=True)
    assert probe(env, capsys, [n])["setup_facts"]["pipelines"] is True


def test_cloud_folders_empty_without_cloud_dirs(env):
    assert scan._cloud_folders() == []


def test_cloud_folders_capped_and_never_reads_contents(env, monkeypatch):
    """反例：只 stat、不读内容（读了会触发云盘下载）；报告条数有上限。"""
    h = env.home
    for i in range(20):
        mkfile(h / "Library" / "CloudStorage" / "Dropbox-x" / ("d%02d" % i) / "f.bin", b"x" * ((i + 1) * 8192))
    real_open = open

    def no_content_reads(path, *a, **k):
        assert "CloudStorage" not in str(path), "不该打开云盘里的文件"
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", no_content_reads)
    got = scan._cloud_folders(limit=5)
    assert len(got) == 5 and got[0]["path"].endswith("d19")


@pytest.mark.skipif(not hasattr(os, "chflags"), reason="需要 os.chflags")
def test_cloud_folders_skip_placeholder_files_and_folders(env, monkeypatch):
    """占位文件（SF_DATALESS）不计字节、不计数；占位文件夹不进去。真实系统标志位设不了，这里用用户可设的 UF_HIDDEN 顶替判据。"""
    import stat as st
    monkeypatch.setattr(scan, "SF_DATALESS", st.UF_HIDDEN)
    base = env.home / "Library" / "CloudStorage" / "Dropbox-x"
    mkfile(base / "real" / "a.bin", b"r" * 30000)
    mkfile(base / "mixed" / "local.bin", b"l" * 20000)
    mkfile(base / "mixed" / "cloud.bin", b"c" * 90000)
    mkfile(base / "ghost" / "inner.bin", b"g" * 90000)
    os.chflags(str(base / "mixed" / "cloud.bin"), st.UF_HIDDEN)
    os.chflags(str(base / "ghost"), st.UF_HIDDEN)
    try:
        got = {r["path"].split("/")[-1]: r for r in scan._cloud_folders()}
    finally:
        os.chflags(str(base / "mixed" / "cloud.bin"), 0)
        os.chflags(str(base / "ghost"), 0)
    assert set(got) == {"real", "mixed"}                     # ghost 整个不进
    assert got["mixed"]["files"] == 1 and got["mixed"]["local_bytes"] < 90000     # cloud.bin 不计


def test_setup_facts_counts_linked_worktrees(env, capsys):
    repo = env.home / "Repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    wt = env.home / "RepoWt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "b2", str(wt)], check=True)
    out = probe(env, capsys)
    assert out["setup_facts"]["worktrees"] == 1


def test_setup_facts_failure_isolated(env, capsys):
    def boom():
        raise RuntimeError("tm 坏了")
    env.mp.setattr(scan, "time_machine_last", boom)
    out = probe(env, capsys)
    assert out["setup_facts"]["time_machine_last"] is None
    assert "ai_sessions_bytes" in out["setup_facts"]


def test_time_machine_last_parses_and_fails_to_none(monkeypatch):
    # tmutil 给出备份目录名：2026-10-01-123456（本地时间）
    monkeypatch.setattr(scan, "run", lambda cmd, timeout=60, **k: (0, b"/Volumes/X/Backups.backupdb/Mac/2026-10-01-123456\n", b""))
    got = REAL_TM()
    assert got and got.startswith("2026-10-0") and got.endswith("Z")
    # tmutil 读不到 → 退回偏好文件里的最大日期
    def fake(cmd, timeout=60, **k):
        if cmd[0] == "tmutil":
            return 1, b"", b"No backups"
        return 0, b'SnapshotDates = ("2026-09-01 01:00:00 +0000", "2026-09-20 02:03:04 +0000");', b""
    monkeypatch.setattr(scan, "run", fake)
    assert REAL_TM() == "2026-09-20T02:03:04Z"
    # 都失败 → None
    monkeypatch.setattr(scan, "run", lambda cmd, timeout=60, **k: (None, b"", b"timeout"))
    assert REAL_TM() is None


# ───────── identity ─────────

def test_identity_attached_by_nearest_ancestor(env):
    deep = env.home / "mystery" / "cache" / "sub"
    deep.mkdir(parents=True)
    mkfile(deep / "a.bin", b"x" * 3000)
    ident_root = os.path.realpath(env.home / "mystery")
    Path(protect.PROFILE).parent.mkdir(parents=True, exist_ok=True)
    Path(protect.PROFILE).write_text(json.dumps({
        "roots": [str(env.home)],
        "identities": {ident_root: {"title": "旧项目缓存", "producer": "某工具", "task": "导出", "at": "x", "scan_id": "s"}}}),
        encoding="utf-8")
    env.mp.setattr(scan, "run_lsof", lambda timeout=120: [])
    runs = env.home.parent / "runs2"
    rc = scan.main(["scan", "--profile", protect.PROFILE, "--runs-dir", str(runs), "--threshold", "1KB", "--no-refs", "--no-lsof"])
    assert rc == 0
    facts = json.loads(next(runs.glob("*/facts.json")).read_text())
    by = {n["path"]: n for n in facts["nodes"]}
    assert by[os.path.realpath(deep)]["identity"] == {"title": "旧项目缓存", "producer": "某工具", "task": "导出"}
    assert by[ident_root]["identity"]["title"] == "旧项目缓存"
    assert "identity" not in by.get(str(env.home), {"x": 1}) or by[str(env.home)].get("identity") is None


# ───────── /private/tmp/claude-uid 根 ─────────

@pytest.fixture
def ptmp(monkeypatch):
    """假的 /private/tmp：放在真实家目录之外（/var/folders），免得被“他人用户目录”规则抢先命中。"""
    import shutil
    import tempfile
    d = os.path.realpath(tempfile.mkdtemp(prefix="v06tmp-"))
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def test_claude_tmp_root_added_and_protected(env, ptmp, monkeypatch):
    base = ptmp / "ptmp"
    root = base / ("claude-%d" % os.getuid())
    sess = root / "-Users-x" / "sess1"
    sess.mkdir(parents=True)
    other = base / "other"
    other.mkdir()
    monkeypatch.setattr(protect, "TMP_BASE", str(base))
    roots = scan.pick_roots(protect.load_profile())
    assert os.path.realpath(root) in roots
    assert protect.protected_reason(str(root)) is not None                 # 根本身不可删
    assert protect.protected_reason(str(base)) is not None                 # /private/tmp 本身
    assert protect.protected_reason(str(other)) is not None                # 其他内容
    assert protect.protected_reason(str(sess)) is None                     # 会话目录可清
    assert protect.protected_reason(str(sess / "f.txt")) is None


def test_claude_tmp_root_absent_not_added(env, ptmp, monkeypatch):
    monkeypatch.setattr(protect, "TMP_BASE", str(ptmp / "none"))
    assert all("claude-" not in r for r in scan.pick_roots(protect.load_profile()))


def test_current_session_id_dir_protected(env, ptmp, monkeypatch):
    base = ptmp / "ptmp"
    root = base / ("claude-%d" % os.getuid())
    cur = root / "proj" / "11112222-3333-4444-5555-666677778888" / "scratchpad"
    old = root / "proj" / "aaaabbbb-cccc-dddd-eeee-ffff00001111"
    cur.mkdir(parents=True)
    old.mkdir(parents=True)
    monkeypatch.setattr(protect, "TMP_BASE", str(base))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "11112222-3333-4444-5555-666677778888")
    assert protect.protected_reason(str(cur)) is not None
    assert protect.protected_reason(str(old)) is None
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ab")           # 太短不当 id
    assert protect.protected_reason(str(cur)) is None


# ───────── done-evidence ─────────

def run_de(capsys, path, **extra):
    argv = ["probe", "done-evidence", "--path", str(path)]
    assert scan.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_done_evidence_structure(env, capsys):
    proj = env.home / "Alpha"
    subprocess.run(["git", "init", "-q", "-b", "main", str(proj)], check=True)
    (proj / "README.md").write_text("# A\n项目已完成\n其他\nStatus: done\n已上线\n还有完成字样\n", encoding="utf-8")
    mkfile(proj / "final-report.pdf")
    subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(proj), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i"], check=True)
    (proj / "dirty.txt").write_text("x")
    deliver = env.home / "成品"
    mkfile(deliver / "Alpha-v1.mp4")
    mkfile(deliver / "Beta.mp4")
    Path(protect.PROFILE).parent.mkdir(parents=True, exist_ok=True)
    Path(protect.PROFILE).write_text(json.dumps({"deliverable_roots": [str(deliver)]}), encoding="utf-8")
    out = run_de(capsys, proj)
    assert out["git"]["is_repo"] and out["git"]["branch"] == "main"
    assert out["git"]["merged_into_main"] is True and out["git"]["dirty_files"] == 1
    assert out["git"]["last_commit_at"]
    assert out["deliverables"]["count"] == 1
    assert out["last_modified"]
    assert "final-report.pdf" in out["final_named_files"]
    hits = {h["file"]: h["lines"] for h in out["doc_hits"]}
    assert len(hits["README.md"]) == 3                        # 每文件最多 3 行


def test_done_evidence_non_repo_and_big_doc_skipped(env, capsys):
    d = env.home / "Plain"
    d.mkdir()
    (d / "README.md").write_bytes(b"done\n" + b"x" * (2 * 1024 * 1024 + 10))
    (d / "password-README.md").write_text("done")
    out = run_de(capsys, d)
    assert out["git"] == {"is_repo": False}
    assert out["doc_hits"] == []
    assert run_de(capsys, env.home / "nope")["exists"] is False
