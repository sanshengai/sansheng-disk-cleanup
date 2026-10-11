"""scan.py 的测试。夹具全部在 tmp_path，不在 skill 目录里造任何带 SKILL.md 的目录。"""
import json
import os
import shutil
import stat
import subprocess
import sys
import time

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)
import scan  # noqa: E402

MB = 1024 * 1024
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def write_file(path, size, fill=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(fill * size)


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, env=env)


def run_scan(tmp_path, roots, extra=None, profile_extra=None, refs=False, threshold="1MB", tag="r"):
    prof = {"roots": [str(r) for r in roots], "extra_roots": [], "reference_roots": [], "protect": [], "notes": {}}
    prof.update(profile_extra or {})
    pf = tmp_path / ("profile-%s.json" % tag)
    pf.write_text(json.dumps(prof))
    argv = ["scan", "--profile", str(pf), "--runs-dir", str(tmp_path / "runs"), "--threshold", threshold,
            "--no-lsof"]
    if not refs:
        argv.append("--no-refs")
    argv += extra or []
    opts = scan.build_parser().parse_args(argv)
    run_dir, facts = scan.do_scan(opts)
    assert os.path.isfile(os.path.join(run_dir, "facts.json"))
    return facts


def node_map(facts):
    return {n["path"]: n for n in facts["nodes"]}


def test_frontier_and_remainder(tmp_path):
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "a" / "big.bin"), 2 * MB)                 # 前沿
    write_file(str(root / "b" / "small.bin"), 10 * 1024)             # 不到阈值，不应出现
    write_file(str(root / "c" / "c1" / "f.bin"), 2 * MB)             # c 有两个大子目录 -> 非前沿
    write_file(str(root / "c" / "c2" / "f.bin"), 2 * MB)
    write_file(str(root / "c" / "loose.bin"), 300 * 1024)            # c 自己的零散文件 -> remainder
    write_file(str(root / "proj" / "node_modules" / "pkg" / "deep" / "x.bin"), 2 * MB)  # 原子目录
    facts = run_scan(tmp_path, [root])
    nodes = node_map(facts)
    assert str(root / "a") in nodes and nodes[str(root / "a")]["frontier"]
    assert str(root / "b") not in nodes
    c = nodes[str(root / "c")]
    assert not c["frontier"] and c["children_over_threshold"] == 2
    assert 300 * 1024 <= c["remainder_bytes"] < 1 * MB
    assert nodes[str(root / "c" / "c1")]["frontier"]
    nm = nodes[str(root / "proj" / "node_modules")]
    assert nm["frontier"], "node_modules 是原子目录，自身即前沿"
    assert not any(p.startswith(str(root / "proj" / "node_modules") + "/") for p in nodes), "不应下钻原子目录"
    assert nodes[str(root / "proj")]["children_over_threshold"] == 1
    assert nodes[str(root)]["children_over_threshold"] == 3
    assert facts["coverage"]["frontier_bytes"] <= facts["coverage"]["walked_bytes"]
    assert facts["coverage"]["incomplete"] is False
    assert facts["schema"] == "facts/1" and facts["threshold"] == 1 * MB
    ids = [n["id"] for n in facts["nodes"]]
    assert len(ids) == len(set(ids))


def test_max_depth_makes_frontier(tmp_path):
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "d1" / "d2" / "d3" / "f.bin"), 2 * MB)
    facts = run_scan(tmp_path, [root], extra=["--max-depth", "2"])
    nodes = node_map(facts)
    assert str(root / "d1" / "d2") in nodes and nodes[str(root / "d1" / "d2")]["frontier"]
    assert str(root / "d1" / "d2" / "d3") not in nodes


def test_hardlink_counted_once(tmp_path):
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "one" / "data.bin"), 2 * MB)
    os.makedirs(root / "two")
    os.link(root / "one" / "data.bin", root / "two" / "link.bin")
    facts = run_scan(tmp_path, [root], threshold="1MB")
    n = node_map(facts)[str(root)]
    assert 2 * MB <= n["bytes"] < 3 * MB, "硬链接只能计一次: %d" % n["bytes"]
    assert n["files"] == 1


def test_symlink_not_followed(tmp_path):
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "real" / "data.bin"), 2 * MB)
    os.symlink(root / "real", root / "link")
    outside = tmp_path.resolve() / "outside"
    write_file(str(outside / "huge.bin"), 3 * MB)
    os.symlink(outside, root / "to_outside")
    facts = run_scan(tmp_path, [root])
    nodes = node_map(facts)
    assert str(root / "link") not in nodes and str(root / "to_outside") not in nodes
    assert 2 * MB <= nodes[str(root)]["bytes"] < 3 * MB


def test_timeout_marks_incomplete(tmp_path):
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "a" / "big.bin"), 2 * MB)
    facts = run_scan(tmp_path, [root], extra=["--max-seconds", "0"])
    assert facts["coverage"]["incomplete"] is True
    assert any(s["reason"] == "timeout" for s in facts["skipped"])


@needs_git
def test_git_roles_and_worktree_facts(tmp_path):
    root = tmp_path.resolve() / "tree"
    repo = root / "repo"
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main")
    write_file(str(repo / "tracked_dir" / "big.bin"), 2 * MB, os.urandom(1))
    write_file(str(repo / "mixed_dir" / "tracked.bin"), 2 * MB)
    (repo / ".gitignore").write_text("ignored_dir/\n.venv/\n")
    git(repo, "add", ".gitignore", "tracked_dir", "mixed_dir")
    git(repo, "commit", "-q", "-m", "init")
    write_file(str(repo / "mixed_dir" / "extra.bin"), 2 * MB)       # 未跟踪，混在跟踪目录里
    write_file(str(repo / "ignored_dir" / "sub" / "x.bin"), 2 * MB)  # 被忽略
    write_file(str(repo / "untracked_dir" / "x.bin"), 2 * MB)        # 未跟踪也未忽略
    write_file(str(repo / ".venv" / "lib" / "x.bin"), 2 * MB)        # 被忽略的原子目录
    write_file(str(repo / ".git" / "bulk.pack"), 2 * MB, os.urandom(1))  # 让 .git 过阈值
    facts = run_scan(tmp_path, [root], threshold="1MB")
    nodes = node_map(facts)
    role = lambda p: nodes[str(repo / p)]["git"]["role"]  # noqa: E731
    assert role("tracked_dir") == "tracked"
    assert nodes[str(repo / "tracked_dir")]["git"]["tracked_files"] == 1
    assert role("mixed_dir") == "mixed"
    assert role("ignored_dir") == "ignored", "忽略目录下的 sub 也应判 ignored，而不是 untracked"
    assert role("untracked_dir") == "untracked"
    assert role(".venv") == "ignored"
    assert nodes[str(root)]["git"]["role"] == "none" and nodes[str(root)]["git"]["repo"] is None
    top = nodes[str(repo)]
    assert top["git"]["worktree"] == str(repo) and top["git"]["repo"] == str(repo)
    assert top["git"]["branch"] == "main"
    assert top["git"]["dirty_files"] == 2            # untracked_dir/ 与 mixed_dir/extra.bin（忽略项不计）
    assert top["git"]["merged_into_origin_main"] is None  # 没有 origin/main
    gitnode = nodes[str(repo / ".git")]
    assert gitnode["protected"] is True and gitnode["git"]["role"] == "none"


@needs_git
def test_refs_found_in_other_repo_and_self_excluded(tmp_path):
    root = tmp_path.resolve() / "tree"
    repo = root / "repo"
    other = tmp_path.resolve() / "other"
    os.makedirs(repo)
    os.makedirs(other)
    for d in (repo, other):
        git(d, "init", "-q", "-b", "main")
    target = repo / "heavy_dir"
    quiet = repo / "quiet_dir"
    write_file(str(target / "data.bin"), 2 * MB)
    write_file(str(quiet / "data.bin"), 2 * MB)
    (quiet / "self.md").write_text("see %s for details\n" % quiet)     # 节点自身内部的跟踪文件，不算引用
    git(repo, "add", "quiet_dir")
    git(repo, "commit", "-q", "-m", "q")
    (other / "docs.md").write_text("line1\nline2\ncache lives at %s now\n" % target)
    (other / "unrelated.md").write_text("nothing here\n")
    git(other, "add", ".")
    git(other, "commit", "-q", "-m", "d")
    facts = run_scan(tmp_path, [root], refs=True, profile_extra={"reference_roots": [str(other)]})
    nodes = node_map(facts)
    t = nodes[str(target)]["refs"]
    assert t["checked"] is True and t["count"] == 1
    assert t["samples"] == ["docs.md:3"]
    q = nodes[str(quiet)]["refs"]
    assert q["checked"] is True and q["count"] == 0, "节点自身内部文件不能算引用: %r" % (q,)
    # 非前沿 / 受保护节点不查引用，且明确标 checked=false
    assert nodes[str(root)]["refs"]["checked"] is False


@pytest.mark.parametrize("use_rg", [True, False])
def test_refs_in_plain_directory(tmp_path, monkeypatch, use_rg):
    if use_rg and not scan.RG:
        pytest.skip("没有 rg")
    monkeypatch.setattr(scan, "RG", scan.RG if use_rg else None)
    root = tmp_path.resolve() / "tree"
    plain = tmp_path.resolve() / "plain"
    target = root / "cache_dir"
    write_file(str(target / "x.bin"), 2 * MB)
    os.makedirs(plain)
    (plain / "notes.txt").write_text("a\nuses ~%s\nmentions %s here\n" % ("/no/such", target))
    (plain / "binary.dat").write_bytes(b"\0\1\2" + str(target).encode())   # 二进制文件不扫
    facts = run_scan(tmp_path, [root], refs=True, profile_extra={"reference_roots": [str(plain)]})
    r = node_map(facts)[str(target)]["refs"]
    assert r["checked"] is True and r["count"] == 1 and r["samples"] == ["notes.txt:3"], r


def test_refs_generic_pattern_is_lower_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "LOCATE_MAX", 5)
    root = tmp_path.resolve() / "tree"
    plain = tmp_path.resolve() / "plain"
    common = root / "sample-project-cache"      # 基名 >= 12 且带 -，会被当成引用模式
    precise = root / "other_dir"
    write_file(str(common / "x.bin"), 2 * MB)
    write_file(str(precise / "x.bin"), 2 * MB)
    os.makedirs(plain)
    for i in range(40):                          # 到处都提到基名 -> 泛用模式
        (plain / ("f%02d.txt" % i)).write_text("see sample-project-cache here\n")
    (plain / "exact.txt").write_text("path: %s\n" % precise)
    facts = run_scan(tmp_path, [root], refs=True, profile_extra={"reference_roots": [str(plain)]})
    nodes = node_map(facts)
    g = nodes[str(common)]["refs"]
    assert g["checked"] is True and g["count"] > 0 and g["count_is_lower_bound"] is True
    assert g["generic_patterns"] == ["sample-project-cache"] and g["samples"]
    e = nodes[str(precise)]["refs"]
    assert e["checked"] is True and e["count"] == 1 and e["samples"] == ["exact.txt:1"]
    assert "count_is_lower_bound" not in e


def test_basename_pattern_rule():
    pats = scan.build_patterns("/work/tree/orchestrator", None)
    assert pats == ["/work/tree/orchestrator"], "纯字母长单词不是独特基名"
    assert "snapshot-20261010" in scan.build_patterns("/work/tree/snapshot-20261010", None)
    assert "sample-project" in scan.build_patterns("/work/tree/sample-project", None)
    assert "dist" not in scan.build_patterns("/work/repo/dist", "/work/repo")


def test_refs_timeout_marks_unchecked(tmp_path):
    root = tmp_path.resolve() / "tree"
    plain = tmp_path.resolve() / "plain"
    write_file(str(root / "cache_dir" / "x.bin"), 2 * MB)
    write_file(str(plain / "n.txt"), 10)
    facts = run_scan(tmp_path, [root], refs=True, profile_extra={"reference_roots": [str(plain)]},
                     extra=["--refs-seconds", "0"])
    r = node_map(facts)[str(root / "cache_dir")]["refs"]
    assert r["checked"] is False and r["count"] == 0


def test_protected_flag_from_profile(tmp_path):
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "keep_me" / "x.bin"), 2 * MB)
    write_file(str(root / "other" / "x.bin"), 2 * MB)
    facts = run_scan(tmp_path, [root], profile_extra={"protect": [str(root / "keep_me")],
                                                      "notes": {str(root / "other"): "一句说明"}})
    nodes = node_map(facts)
    assert nodes[str(root / "keep_me")]["protected"] is True
    assert nodes[str(root / "other")]["protected"] is False
    assert nodes[str(root / "other")]["note"] == "一句说明"
    assert nodes[str(root / "keep_me")]["refs"]["checked"] is False   # 受保护节点不查引用


def test_growth_against_previous_facts(tmp_path):
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "grow" / "a.bin"), 2 * MB)
    first = run_scan(tmp_path, [root], tag="1")
    n1 = node_map(first)[str(root / "grow")]
    assert n1["growth"] == {"prev_scan_id": None, "prev_bytes": None, "delta_bytes": None}
    write_file(str(root / "grow" / "b.bin"), 1 * MB)
    second = run_scan(tmp_path, [root], tag="2")
    n2 = node_map(second)[str(root / "grow")]
    assert n2["growth"]["prev_scan_id"] == first["scan_id"]
    assert n2["growth"]["prev_bytes"] == n1["bytes"]
    assert abs(n2["growth"]["delta_bytes"] - 1 * MB) < 64 * 1024


@pytest.mark.skipif(shutil.which("lsof") is None, reason="需要 lsof")
def test_open_by_from_lsof(tmp_path):
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "busy" / "a.bin"), 2 * MB)
    prof = tmp_path / "p.json"
    prof.write_text(json.dumps({"roots": [str(root)]}))
    with open(root / "busy" / "a.bin", "rb"):
        opts = scan.build_parser().parse_args(["scan", "--profile", str(prof), "--runs-dir", str(tmp_path / "runs"),
                                               "--threshold", "1MB", "--no-refs"])
        _, facts = scan.do_scan(opts)
    n = node_map(facts)[str(root / "busy")]
    assert any(p["pid"] == os.getpid() for p in n["open_by"]), n["open_by"]
    idle = node_map(facts)[str(root)]
    assert isinstance(idle["open_by"], list)


def test_open_by_unknown_when_lsof_skipped(tmp_path):
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "a" / "a.bin"), 2 * MB)
    facts = run_scan(tmp_path, [root])
    assert node_map(facts)[str(root / "a")]["open_by"] == "unknown"


def test_producer_rules_and_cards(tmp_path):
    p = scan.Producers()
    home = scan.HOME
    assert p.match(home + "/.codex/sessions")["card"] == "codex"
    assert p.match(home + "/.codex")["card"] == "codex"
    assert p.match(home + "/.claude/projects/x")["card"] == "claude-code"
    assert p.match(home + "/Library/Caches/ms-playwright")["card"] == "model-caches"
    assert p.match(home + "/Library/Caches/com.foo.bar")["card"] == "macos-system"
    assert p.match(home + "/Library/Application Support/BaiduNetdisk_mac")["card"] == "chat-netdisk"
    assert p.match(home + "/Library/Developer/Xcode/DerivedData")["card"] == "xcode"
    assert p.match("/data/some/android-sdk")["card"] == "android"
    assert p.match("/work/app/node_modules")["card"] == "git-worktree-build"
    assert p.match("/work/app/dist/assets")["card"] == "git-worktree-build"
    assert p.match(home + "/nothing/here") == {"tool": None, "card": None, "hint": None}
    cards_dir = os.path.join(os.path.dirname(SCRIPTS), "references", "cards")
    cards = {f[:-3] for f in os.listdir(cards_dir) if f.endswith(".md")}   # 规则引用的卡必须真有卡文件
    assert {"browsers", "app-updaters-installers", "creative-media", "chat-netdisk", "macos-system"} <= cards
    rules = json.load(open(os.path.join(SCRIPTS, "producers.json"), encoding="utf-8"))["rules"]
    assert {r["card"] for r in rules} <= cards
    required = ["~/.codex/**", "~/.claude/**", "~/.local/share/claude", "~/.grok", "~/.gemini",
                "~/Library/Application Support/Cursor", "~/.cursor", "~/.cache/huggingface", "~/.ollama",
                "~/.lmstudio", "~/Library/Caches/ms-playwright", "~/.npm", "~/.pnpm-store", "~/Library/pnpm",
                "~/.yarn", "~/.bun", "~/.cache/uv", "~/.local/share/uv", "~/Library/Caches/pip",
                "~/Library/Caches/Homebrew", "~/.gradle", "~/.android", "~/Library/Android/sdk", "**/android-sdk",
                "~/Library/Developer/**", "~/Library/Containers/com.docker.docker", "**/node_modules", "**/dist",
                "**/build", "**/.next", "**/target", "**/.venv", "~/Library/Caches", "~/.Trash",
                "~/Library/Containers/com.tencent.xinWeChat", "~/Library/Containers/com.tencent.WeWorkMac",
                "~/Library/Application Support/QuarkCloudDrive", "~/Library/Application Support/BaiduNetdisk*",
                "~/Library/Containers/com.kingsoft.wpsoffice.mac"]
    globs = {r["glob"] for r in rules}
    assert [g for g in required if g not in globs] == []


def test_parse_size():
    assert scan.parse_size("500MB") == 500 * MB
    assert scan.parse_size("1GB") == 1024 * MB
    assert scan.parse_size("1048576") == MB
    with pytest.raises(Exception):
        scan.parse_size("abc")


def test_rollout_head_streaming_skips_injected_and_truncates(tmp_path):
    p = tmp_path / "rollout-2026-10-01T00-00-00-00000000-0000-7000-8000-000000000001.jsonl"
    meta = {"type": "session_meta", "payload": {"id": "sid-1", "cwd": "/x/y", "source": {"subagent": {
        "thread_spawn": {"parent_thread_id": "parent-1"}}}, "base_instructions": {"text": "z" * 300000}}}
    inject = {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                    "content": [{"type": "input_text", "text": "# AGENTS.md instructions\nxxx"}]}}
    real = {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text", "text": "帮我" + "很长" * 200}]}}
    with open(p, "w", encoding="utf-8") as f:
        for d in (meta, inject, real):
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
        f.write("x" * (50 * MB) + "\n")  # 后面的大垃圾行不应被读取
    m, first = scan.read_rollout_head(str(p))
    assert m["id"] == "sid-1" and m["cwd"] == "/x/y"
    assert scan._source_info(m) == ("subagent", "parent-1")
    assert first.startswith("帮我") and len(first) == 120


@needs_git
def test_probe_worktrees(tmp_path):
    repo = tmp_path.resolve() / "repo"
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("junk/\n")
    (repo / "a.txt").write_text("a")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "i")
    wt = tmp_path.resolve() / "wt1"
    git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    write_file(str(wt / "junk" / "x.bin"), 1 * MB)
    (wt / "dirty.txt").write_text("d")
    opts = scan.build_parser().parse_args(["probe", "worktrees", "--repo", str(repo)])
    out = scan.probe_worktrees(opts)
    by = {w["path"]: w for w in out["worktrees"]}
    assert set(by) == {str(repo), str(wt)}
    w = by[str(wt)]
    assert w["branch"] == "feat" and w["dirty_files"] == 1
    assert w["ignored"]["bytes"] >= 1 * MB and w["ignored"]["top"][0]["path"] == "junk/"
    assert by[str(repo)]["branch"] == "main" and by[str(repo)]["ignored"]["bytes"] == 0


def test_no_skill_md_fixture_in_skill_dir():
    skill_dir = os.path.dirname(SCRIPTS)
    found = []
    for dirpath, _, files in os.walk(skill_dir):
        if "SKILL.md" in files and os.path.abspath(dirpath) != os.path.abspath(skill_dir):
            found.append(dirpath)
    assert found == []


# ---------------------------------------------------------------- probe codex-groups

def _codex_fixture(tmp_path, monkeypatch):
    """假 ~/.codex：一个出图项目（4 个 exec 会话）+ 一个交互会话带一个子 Agent。"""
    import sqlite3
    home = (tmp_path / "home").resolve()
    codex = home / ".codex"
    proj = home / "proj"
    out = proj / "out"
    out.mkdir(parents=True)
    monkeypatch.setattr(scan, "HOME", str(home))
    monkeypatch.setattr(scan.protect, "HOME", str(home))
    monkeypatch.setattr(scan, "HOME", str(home))   # _glob_regex 读的是 scan.HOME
    monkeypatch.setattr(scan, "run_lsof", lambda timeout=120: [])
    ids = {k: "%08d-aaaa-bbbb-cccc-%012d" % (i, i) for i, k in enumerate(
        ["copied", "mentioned", "empty", "orphan", "parent", "child", "reject", "later", "ignored",
         "exparent", "exchild"], start=1)}
    old = 1_700_000_000

    def rollout(tid, cwd, source, originator, extra=None):
        p = codex / "sessions" / "2026" / "10" / "02" / f"rollout-2026-10-02T00-00-00-{tid}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        meta = {"id": tid, "cwd": str(cwd), "source": source, "originator": originator}
        meta.update(extra or {})
        p.write_text(json.dumps({"type": "session_meta", "payload": meta}) + "\n" + "x" * 5000 + "\n")
        os.utime(p, (old, old))
        return p

    def image(tid, data):
        p = codex / "generated_images" / tid / "img.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    scratch = proj / "scratch"
    scratch.mkdir()
    for k in ("copied", "mentioned", "empty", "orphan", "reject", "ignored"):
        rollout(ids[k], scratch, "exec", "codex_exec")
    image(ids["copied"], b"A" * 3000)
    (proj / "assets").mkdir()
    (proj / "assets" / "copied.png").write_bytes(b"A" * 3000)   # 逐字节相同、且被 git 跟踪的副本
    image(ids["reject"], b"R" * 3000)
    (out / "rejected").mkdir()
    (out / "rejected" / "r.png").write_bytes(b"R" * 3000)      # 副本在淘汰目录
    rollout(ids["exparent"], scratch, "exec", "codex_exec")      # 自己没出图……
    rollout(ids["exchild"], scratch, {"subagent": {"thread_spawn": {"parent_thread_id": ids["exparent"]}}},
            "codex_exec")                                          # ……子会话出了图；父子关系只记在 session_meta 里
    image(ids["exchild"], b"C" * 3000)
    image(ids["ignored"], b"I" * 3000)
    (out / "final").mkdir()
    (out / "final" / "i.png").write_bytes(b"I" * 3000)         # 副本在被 git 忽略的普通目录
    image(ids["mentioned"], b"M" * 3000)
    (out / "log.json").write_text(json.dumps({"source": ids["mentioned"]}))   # 只被提到
    image(ids["orphan"], b"O" * 3000)                          # 有图、无副本、无人提到
    (proj / ".gitignore").write_text("out/\n")                 # 回执在被忽略的目录里也要搜到
    git(proj, "init", "-q")
    git(proj, "add", "assets/copied.png", ".gitignore")
    git(proj, "commit", "-q", "-m", "init")
    rollout(ids["parent"], home / "work", "vscode", "codex-tui")
    later = rollout(ids["later"], home / "other", "vscode", "codex-tui")
    later.write_text(later.read_text() + json.dumps({"msg": "接着会话 " + ids["parent"] + " 做"}) + "\n")
    os.utime(later, (old, old))
    rollout(ids["child"], home / "work", {"subagent": {"thread_spawn": {"parent_thread_id": ids["parent"]}}},
            "codex-tui")
    con = sqlite3.connect(codex / "state_5.sqlite")
    con.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)")
    con.execute("INSERT INTO thread_spawn_edges VALUES (?, ?)", (ids["parent"], ids["child"]))
    con.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT)")
    con.commit()
    con.close()
    con = sqlite3.connect(codex / "thread_history_1.sqlite")
    con.execute("CREATE TABLE thread_items (thread_id TEXT, item_json TEXT)")
    con.execute("INSERT INTO thread_items VALUES (?, ?)", (ids["copied"], "b" * 7000))
    con.commit()
    con.close()
    prof = tmp_path / "profile.json"
    prof.write_text(json.dumps({"reference_roots": []}))
    return home, proj, ids, prof


def _probe(argv):
    return scan.probe_codex_groups(scan.build_parser().parse_args(["probe", "codex-groups"] + argv))


def test_codex_groups_groups_small_sessions_and_attaches_subagents(tmp_path, monkeypatch):
    home, proj, ids, prof = _codex_fixture(tmp_path, monkeypatch)
    r = _probe(["--min-bytes", "0", "--profile", str(prof)])
    by_cwd = {g["cwd"]: g for g in r["groups"]}
    batch = by_cwd[str(proj / "scratch")]
    assert batch["threads"] == 8 and batch["source"] == "exec"   # 每个会话都很小，也要合成一组
    assert batch["history_bytes"] == 7000 and batch["image_dirs"] == 6
    inter = by_cwd[str(home / "work")]
    assert inter["threads"] == 2 and inter["subagents"] == 1     # 子 Agent 归到父会话的组
    assert len(r["groups"]) == 3


def test_codex_groups_trace_classifies_and_emits_items(tmp_path, monkeypatch):
    home, proj, ids, prof = _codex_fixture(tmp_path, monkeypatch)
    groups = _probe(["--min-bytes", "0", "--profile", str(prof)])["groups"]
    idx = next(g["index"] for g in groups if g["source"] == "exec")
    emit = tmp_path / "items"
    r = _probe(["--min-bytes", "0", "--profile", str(prof), "--trace", str(idx), "--emit", str(emit)])
    cls = {x["id"]: x["class"] for x in r["trace"]["threads"]}
    assert cls[ids["copied"]] == "copied"
    assert cls[ids["mentioned"]] == "mentioned"      # log.json 在被 git 忽略的 out/ 里
    assert cls[ids["empty"]] == "empty"
    assert cls[ids["orphan"]] == "unmentioned"
    assert cls[ids["reject"]] == "copied_elsewhere"   # 有副本，但在 rejected/ 里
    rec = {x["id"]: x for x in r["trace"]["threads"]}
    assert rec[ids["copied"]]["copy_where"] == ["tracked"] and rec[ids["reject"]]["copy_where"] == ["rejected"]
    assert rec[ids["ignored"]]["class"] == "copied_elsewhere" and rec[ids["ignored"]]["copy_where"] == ["untracked"]
    assert len(r["trace"]["threads"]) == 7 and "copy_sample" in rec[ids["copied"]]   # exchild 随父线程，不单列
    assert rec[ids["exparent"]]["class"] == "unmentioned"     # 子会话的图算进父线程，不能落进 empty
    assert [d["id"] for d in rec[ids["exparent"]]["descendants"]] == [ids["exchild"]]   # 带 --emit 也不截断
    items = json.loads(open(r["trace"]["emitted"]["copied"]["file"]).read())
    assert items[0]["argv"] == ["codex", "delete", "--force", ids["copied"]]
    assert ids["copied"] in items[0]["path"]
    assert items[-1] == {"path": str(home / ".codex" / "generated_images" / ids["copied"]),
                         "method": "delete", "scope": "all", "class": "batch", "session": ids["copied"]}


def test_codex_groups_emits_descendants_before_parent(tmp_path, monkeypatch):
    home, proj, ids, prof = _codex_fixture(tmp_path, monkeypatch)
    groups = _probe(["--min-bytes", "0", "--profile", str(prof)])["groups"]
    idx = next(g["index"] for g in groups if g["source"] == "vscode")
    emit = tmp_path / "items"
    r = _probe(["--min-bytes", "0", "--profile", str(prof), "--trace", str(idx), "--emit", str(emit)])
    tag = r["trace"]["group"]["tag"]
    files = sorted(f.name for f in emit.glob("codex-*.json"))
    assert files == [f"codex-{tag}-manifest.json", f"codex-{tag}-thread-{ids['parent']}.json"]   # 交互对话逐个会话出文件
    argvs = [it["argv"][-1] for it in json.loads((emit / files[1]).read_text())]
    assert argvs == [ids["child"], ids["parent"]]   # 子会话在前；子会话不单独再出现一次
    emitted = json.loads((emit / files[1]).read_text())
    assert {it["class"] for it in emitted} == {"interactive"}                 # 交互对话组的条目一律 interactive
    assert {it["session"] for it in emitted} == {ids["parent"]}               # 后代随顶层会话，同属一个 session
    [rec] = r["trace"]["threads"]                   # 子会话不单独分类，体积不重复
    assert rec["class"] == "conversation"           # 没出图的交互对话不能落进 empty
    assert rec["mentioned_by_sessions"] == [ids["later"]]   # 另一个会话写了"接着这个会话做"


def test_codex_groups_busy_descendant_protects_parent(tmp_path, monkeypatch):
    home, proj, ids, prof = _codex_fixture(tmp_path, monkeypatch)
    child = next((home / ".codex" / "sessions").rglob(f"*{ids['child']}.jsonl"))
    os.utime(child, None)                            # 子会话 24 小时内有改动
    groups = _probe(["--min-bytes", "0", "--profile", str(prof)])["groups"]
    idx = next(g["index"] for g in groups if g["source"] == "vscode")
    r = _probe(["--min-bytes", "0", "--profile", str(prof), "--trace", str(idx), "--emit", str(tmp_path / "it")])
    cls = {x["id"]: x["class"] for x in r["trace"]["threads"]}
    assert cls[ids["parent"]] == "active"
    assert not list((tmp_path / "it").glob("*.json")) or all(
        ids["parent"] not in f.read_text() for f in (tmp_path / "it").glob("*.json"))


def test_reference_search_sees_git_ignored_files(tmp_path):
    root = (tmp_path / "repo").resolve()
    (root / "build").mkdir(parents=True)
    git(root, "init", "-q")
    (root / ".gitignore").write_text("build/\n")
    (root / "build" / "manifest.json").write_text('{"src": "needle-0123"}')
    hits = scan._grep_scope(str(root), ["needle-0123"], __import__("time").monotonic() + 30)
    assert hits == {str(root / "build" / "manifest.json")}


def test_descendants_deepest_first():
    children = {"p": ["c1", "c2"], "c1": ["g1"], "g1": ["gg"]}
    order = scan._descendants("p", children)
    assert order.index("gg") < order.index("g1") < order.index("c1")
    assert set(order) == {"c1", "c2", "g1", "gg"}
    assert scan._descendants("x", {"x": ["y"], "y": ["x"]}) == ["y"]   # 有环不死循环，也不把自己算成后代


def test_session_mentions_ignore_inventory_sessions(tmp_path):
    if not scan.RG:
        pytest.skip("需要 rg")
    root = tmp_path / "sessions"
    root.mkdir()
    ids = ["0000%04d-aaaa-bbbb-cccc-000000000000" % i for i in range(15)]
    inv, cont = "11111111-aaaa-bbbb-cccc-000000000000", "22222222-aaaa-bbbb-cccc-000000000000"
    (root / f"rollout-x-{inv}.jsonl").write_text(" ".join(ids))            # 盘点：一次列出 15 个线程
    (root / f"rollout-x-{cont}.jsonl").write_text("接着 " + ids[3] + " 做")  # 续接：只提一个
    found, done, inventory, listed_live = scan.session_mentions(ids, [str(root)], {}, __import__("time").monotonic() + 30)
    assert done and inventory == [{"id": inv, "threads": 15, "live": False}] and listed_live == {}
    assert found == {ids[3]: [cont]}


def _batch_index(prof):
    return next(g["index"] for g in _probe(["--min-bytes", "0", "--profile", str(prof)])["groups"]
                if g["source"] == "exec")


def test_codex_trace_unverified_when_search_incomplete(tmp_path, monkeypatch):
    home, proj, ids, prof = _codex_fixture(tmp_path, monkeypatch)
    idx = _batch_index(prof)
    monkeypatch.setattr(scan, "search_references", lambda items, roots, deadline: (
        {it["key"]: {"checked": False, "files": {}, "scopes": []} for it in items}, []))
    r = _probe(["--min-bytes", "0", "--profile", str(prof), "--trace", str(idx), "--emit", str(tmp_path / "it")])
    cls = {x["id"]: x["class"] for x in r["trace"]["threads"]}
    assert cls[ids["orphan"]] == "unverified" and cls[ids["mentioned"]] == "unverified"
    assert cls[ids["copied"]] == "copied" and cls[ids["empty"]] == "empty"   # 正向证据与"没图"不受影响
    assert "unverified" not in r["trace"]["emitted"] and "unmentioned" not in r["trace"]["emitted"]


def test_codex_trace_unverified_without_search_roots(tmp_path, monkeypatch):
    home, proj, ids, prof = _codex_fixture(tmp_path, monkeypatch)
    idx = _batch_index(prof)
    monkeypatch.setattr(scan, "_project_root", lambda cwd: None)
    r = _probe(["--min-bytes", "0", "--profile", str(prof), "--trace", str(idx)])
    cls = {x["id"]: x["class"] for x in r["trace"]["threads"]}
    assert cls[ids["orphan"]] == "unverified" and "warning" in r["trace"]


def test_codex_emit_clears_stale_files_of_same_group(tmp_path, monkeypatch):
    home, proj, ids, prof = _codex_fixture(tmp_path, monkeypatch)
    idx = _batch_index(prof)
    emit = tmp_path / "items"
    r = _probe(["--min-bytes", "0", "--profile", str(prof), "--trace", str(idx), "--emit", str(emit)])
    stale = emit / f"codex-{r['trace']['group']['tag']}-partial.json"
    stale.write_text("[]")
    other = emit / "codex-deadbeef-copied.json"
    other.write_text("[]")
    _probe(["--min-bytes", "0", "--profile", str(prof), "--trace", str(idx), "--emit", str(emit)])
    assert not stale.exists() and other.exists()
    man = json.loads((emit / f"codex-{r['trace']['group']['tag']}-manifest.json").read_text())
    assert man["group_key"].startswith("exec|codex_exec|")


def test_session_mentions_flag_live_inventory_sessions(tmp_path):
    if not scan.RG:
        pytest.skip("需要 rg")
    root = tmp_path / "sessions"
    root.mkdir()
    ids = ["0000%04d-aaaa-bbbb-cccc-000000000000" % i for i in range(15)]
    drv = "33333333-aaaa-bbbb-cccc-000000000000"
    (root / f"rollout-x-{drv}.jsonl").write_text(" ".join(ids))   # 仍在运行、列了很多线程号的会话
    threads = {drv: {"open": False, "recent": True}}
    found, done, inventory, listed_live = scan.session_mentions(ids, [str(root)], threads,
                                                               __import__("time").monotonic() + 30)
    assert done and found == {}                                    # 不当作逐个依赖
    assert inventory == [{"id": drv, "threads": 15, "live": True}]
    assert listed_live[ids[0]] == [drv]                            # 但逐个线程标出来，交给 Agent 核对


def test_conversation_mentioned_by_live_session_is_not_emitted(tmp_path, monkeypatch):
    home, proj, ids, prof = _codex_fixture(tmp_path, monkeypatch)
    later = next((home / ".codex" / "sessions").rglob(f"*{ids['later']}.jsonl"))
    os.utime(later, None)            # 提到 parent 的那个会话现在仍在运行
    groups = _probe(["--min-bytes", "0", "--profile", str(prof)])["groups"]
    idx = next(g["index"] for g in groups if g["cwd"] == str(home / "work"))
    emit = tmp_path / "it"
    r = _probe(["--min-bytes", "0", "--profile", str(prof), "--trace", str(idx), "--emit", str(emit)])
    assert r["trace"]["held_by_live_sessions"] == [ids["parent"]]
    assert not [f for f in emit.glob("*.json") if "manifest" not in f.name]


# ---- dataless / 云盘：读内容前不触发下载 ----

DATALESS_FLAG = getattr(stat, "SF_DATALESS", 0x40000000)


def test_sf_dataless_constant_is_macos_value():
    assert scan.SF_DATALESS == 0x40000000
    assert scan.SF_DATALESS == DATALESS_FLAG


def _fake_dataless(monkeypatch, names):
    real = os.lstat

    class St:
        def __init__(self, st):
            self._st = st
            # 字面量而不是 scan.SF_DATALESS：常量写错时测试不能跟着一起错（尺子不能是被测变量）
            self.st_flags = getattr(st, "st_flags", 0) | DATALESS_FLAG

        def __getattr__(self, k):
            return getattr(self._st, k)

    def fake(p, *a, **kw):
        st = real(p, *a, **kw)
        return St(st) if os.path.basename(str(p)) in names else st
    monkeypatch.setattr(os, "lstat", fake)


def _count_opens(monkeypatch):
    import builtins
    opened = []
    real = builtins.open

    def spy(p, *a, **kw):
        opened.append(str(p))
        return real(p, *a, **kw)
    monkeypatch.setattr(builtins, "open", spy)
    return opened


@pytest.fixture(autouse=False)
def clean_read_state():
    scan.READ_SKIPS.clear()
    scan.READ_WARNINGS.clear()
    yield
    scan.READ_SKIPS.clear()
    scan.READ_WARNINGS.clear()


def test_py_scan_root_skips_dataless_file_and_dir(tmp_path, monkeypatch, clean_read_state):
    import re
    write_file(str(tmp_path / "ok.txt"), 1, b"NEEDLE")
    write_file(str(tmp_path / "cloud.txt"), 1, b"NEEDLE")
    write_file(str(tmp_path / "gone" / "inner.txt"), 1, b"NEEDLE")
    _fake_dataless(monkeypatch, {"cloud.txt", "gone"})
    opened = _count_opens(monkeypatch)
    hits = scan._py_scan_root(str(tmp_path), re.compile(b"NEEDLE"), 1e18)
    assert hits == {str(tmp_path / "ok.txt")}
    assert not [o for o in opened if o.endswith("cloud.txt") or o.endswith("inner.txt")]
    assert {s["path"] for s in scan.READ_SKIPS} == {str(tmp_path / "cloud.txt"), str(tmp_path / "gone")}
    assert all(s["reason"] == "dataless" for s in scan.READ_SKIPS)


def test_read_capped_and_sha256_refuse_dataless(tmp_path, monkeypatch, clean_read_state):
    write_file(str(tmp_path / "a.bin"), 10)
    _fake_dataless(monkeypatch, {"a.bin"})
    opened = _count_opens(monkeypatch)
    with pytest.raises(OSError):
        scan._read_capped(str(tmp_path / "a.bin"))
    with pytest.raises(OSError):
        scan._sha256(str(tmp_path / "a.bin"))
    assert not opened
    assert len(scan.READ_SKIPS) == 2


def test_find_copies_does_not_hash_dataless_candidate(tmp_path, monkeypatch, clean_read_state):
    src = tmp_path / "src" / "img.png"
    cp = tmp_path / "pool" / "copy.png"
    write_file(str(src), 50, b"a")
    write_file(str(cp), 50, b"a")
    _fake_dataless(monkeypatch, {"copy.png"})
    opened = _count_opens(monkeypatch)
    found, _ = scan.find_copies([str(src)], [str(tmp_path / "pool")], 1e18)
    assert found == {}
    assert not [o for o in opened if o.endswith("copy.png")]
    assert any(s["path"].endswith("copy.png") for s in scan.READ_SKIPS)


def test_grep_scope_rg_argv_excludes_cloud_dirs(tmp_path, monkeypatch, clean_read_state):
    seen = []
    monkeypatch.setattr(scan, "RG", "/usr/bin/rg")
    monkeypatch.setattr(scan, "run", lambda cmd, **kw: (seen.append(cmd), (1, b"", b""))[1])
    assert scan._grep_scope(str(tmp_path), ["x"], 1e18) == set()
    cmd = seen[0]
    assert "!**/Library/Mobile Documents" in cmd and "!**/Library/CloudStorage" in cmd


def test_grep_scope_git_fallback_excludes_cloud_dirs(tmp_path, monkeypatch, clean_read_state):
    (tmp_path / ".git").mkdir()
    seen = []
    monkeypatch.setattr(scan, "RG", None)
    monkeypatch.setattr(scan, "git", lambda args, wt, **kw: (seen.append(args), (1, b"", b""))[1])
    scan._grep_scope(str(tmp_path), ["x"], 1e18)
    assert ":(exclude,glob,icase)**/Library/Mobile Documents/**" in seen[0]
    assert ":(exclude,glob,icase)**/Library/CloudStorage/**" in seen[0]
    assert ":(exclude)Library/Mobile Documents" not in seen[0]      # 旧的以搜索根为锚的写法


def test_grep_scope_skips_root_inside_cloud_with_warning(tmp_path, monkeypatch, clean_read_state):
    home = tmp_path / "home"
    root = home / "Library" / "CloudStorage" / "GoogleDrive"
    write_file(str(root / "a.txt"), 1, b"NEEDLE")
    monkeypatch.setattr(scan, "HOME", str(home))
    calls = []
    monkeypatch.setattr(scan, "run", lambda *a, **kw: calls.append(a) or (0, b"", b""))
    issues = []
    # 跳过 != 搜过没命中：返回 None（调用方据此标未核实），不是空集
    assert scan._grep_scope(str(root), ["NEEDLE"], 1e18, issues) is None
    assert issues == ["cloud_root_skipped"]
    assert scan._grep_scope(str(root), ["NEEDLE"], 1e18) is None
    assert not calls
    assert scan.READ_WARNINGS and "云盘" in scan.READ_WARNINGS[0]


@pytest.mark.skipif(shutil.which("rg") is None, reason="需要 rg")
def test_real_rg_skips_cloud_dirs_with_spaces(tmp_path, monkeypatch, clean_read_state):
    home = tmp_path / "home"
    write_file(str(home / "Library" / "CloudStorage" / "x" / "a.txt"), 1, b"NEEDLE")
    write_file(str(home / "Library" / "Mobile Documents" / "com~apple" / "b.txt"), 1, b"NEEDLE")
    write_file(str(home / "work" / "c.txt"), 1, b"NEEDLE")
    monkeypatch.setattr(scan, "RG", shutil.which("rg"))
    got = scan._grep_scope(str(home), ["NEEDLE"], scan.time.monotonic() + 60)
    assert got == {str(home / "work" / "c.txt")}


# ---- H1/M5/L5/L6：引用检查被跳过或没查完 != “已核实、0 引用” ----

def _search(tmp_path, root, key="k", target="/no/such/target/dir"):
    items = [{"key": key, "patterns": [target], "path": None, "scopes": []}]
    res, stats = scan.search_references(items, [str(root)], scan.time.monotonic() + 60)
    return res[key], stats


def _home_with_icloud_dd(tmp_path, monkeypatch, enabled=True):
    home = tmp_path / "home"
    os.makedirs(home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / ("Desktop" if enabled else "Other"))
    monkeypatch.setattr(scan, "HOME", str(home))
    scan.reset_read_state()
    return home


def test_search_cloud_root_is_unchecked_not_zero_refs(tmp_path, monkeypatch, clean_read_state):
    home = tmp_path / "home"
    root = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "notes"
    write_file(str(root / "plan.md"), 1, b"/no/such/target/dir")
    monkeypatch.setattr(scan, "HOME", str(home))
    r, stats = _search(tmp_path, root)
    assert r["checked"] is False and r["reason"] == "cloud_root_skipped"
    assert stats[0]["ok"] is False


def test_search_cloud_root_case_insensitive(tmp_path, monkeypatch, clean_read_state):
    home = tmp_path / "home"
    os.makedirs(home / "Library" / "CloudStorage" / "Drive")
    monkeypatch.setattr(scan, "HOME", str(home))
    assert scan._in_cloud(str(home / "library" / "cloudstorage" / "Drive"))
    assert scan._in_cloud(str(home / "LIBRARY" / "MOBILE DOCUMENTS" / "x"))
    assert not scan._in_cloud(str(home / "Library" / "CloudStorageX"))
    r, _ = _search(tmp_path, home / "library" / "cloudstorage" / "Drive")
    assert r["checked"] is False and r["reason"] == "cloud_root_skipped"


@pytest.mark.parametrize("use_rg", [False, True])
def test_search_dataless_reference_file_is_unchecked(tmp_path, monkeypatch, clean_read_state, use_rg):
    if use_rg and not scan.RG:
        pytest.skip("需要 rg")
    monkeypatch.setattr(scan, "RG", scan.RG if use_rg else None)
    root = tmp_path / "refs"
    write_file(str(root / "ref.md"), 0)
    (root / "ref.md").write_text("see /no/such/target/dir\n")
    _fake_dataless(monkeypatch, {"ref.md"})
    r, _ = _search(tmp_path, root)
    assert r["checked"] is False and r["reason"] == "dataless_skipped", r
    assert any(s["path"].endswith("ref.md") for s in scan.READ_SKIPS)


def test_search_rg_exit_2_is_unchecked(tmp_path, monkeypatch, clean_read_state):
    root = tmp_path / "refs"
    write_file(str(root / "a.md"), 1)
    monkeypatch.setattr(scan, "RG", "/usr/bin/rg")
    monkeypatch.setattr(scan, "run", lambda *a, **kw: (2, b"", b"permission denied"))
    r, _ = _search(tmp_path, root)
    assert r["checked"] is False and r["reason"] == "read_error", r
    assert scan._grep_scope(str(root), ["x"], 1e18) is None        # 没有 issues 通道时直接当失败


def test_search_rg_hit_unreadable_is_unchecked(tmp_path, monkeypatch, clean_read_state):
    root = tmp_path / "refs"
    write_file(str(root / "a.md"), 1)
    monkeypatch.setattr(scan, "RG", "/usr/bin/rg")
    monkeypatch.setattr(scan, "run", lambda *a, **kw: (0, (str(root / "gone.md") + "\0").encode(), b""))
    r, _ = _search(tmp_path, root)
    assert r["checked"] is False and r["reason"] == "read_error", r


def test_do_scan_node_refs_unchecked_for_cloud_reference_root(tmp_path, monkeypatch, clean_read_state):
    home = tmp_path.resolve() / "home"
    cloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "notes"
    write_file(str(cloud / "plan.md"), 1)
    root = tmp_path.resolve() / "tree"
    target = root / "cache_dir"
    write_file(str(target / "x.bin"), 2 * MB)
    monkeypatch.setattr(scan, "HOME", str(home))
    facts = run_scan(tmp_path, [root], refs=True, profile_extra={"reference_roots": [str(cloud)]})
    r = node_map(facts)[str(target)]["refs"]
    assert r["checked"] is False and r["reason"] == "cloud_root_skipped" and r["count"] == 0, r
    assert facts["coverage"]["icloud_desktop_documents"] is False


@pytest.mark.parametrize("use_rg", [False, True])
def test_do_scan_normal_refs_stay_checked(tmp_path, monkeypatch, clean_read_state, use_rg):
    """防退化：没有云盘、没有 dataless 时，引用检查仍是 checked=True，0 引用也照旧。"""
    if use_rg and not scan.RG:
        pytest.skip("需要 rg")
    monkeypatch.setattr(scan, "RG", scan.RG if use_rg else None)
    monkeypatch.setattr(scan, "HOME", str(tmp_path.resolve() / "home"))
    root = tmp_path.resolve() / "tree"
    plain = tmp_path.resolve() / "plain"
    target = root / "cache_dir"
    write_file(str(target / "x.bin"), 2 * MB)
    write_file(str(plain / "notes.txt"), 1, b"nothing here")
    facts = run_scan(tmp_path, [root], refs=True, profile_extra={"reference_roots": [str(plain)]})
    r = node_map(facts)[str(target)]["refs"]
    assert r["checked"] is True and r["count"] == 0 and not r.get("reason"), r
    assert not any(s["reason"].startswith(("dataless", "cloud")) for s in facts["skipped"])


def test_icloud_desktop_documents_detection_and_coverage(tmp_path, monkeypatch, clean_read_state):
    _home_with_icloud_dd(tmp_path, monkeypatch, enabled=False)
    assert scan.icloud_desktop_documents() is False
    assert not scan._in_cloud(str(tmp_path / "home" / "Desktop" / "x"))
    home = _home_with_icloud_dd(tmp_path, monkeypatch, enabled=True)
    assert scan.icloud_desktop_documents() is True
    assert scan._in_cloud(str(home / "Desktop" / "x")) and scan._in_cloud(str(home / "documents"))
    assert not scan._in_cloud(str(home / "Downloads"))
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "a" / "x.bin"), 2 * MB)
    facts = run_scan(tmp_path, [root])
    assert facts["coverage"]["icloud_desktop_documents"] is True


def test_icloud_dd_excludes_desktop_for_root_and_ancestor(tmp_path, monkeypatch, clean_read_state):
    home = _home_with_icloud_dd(tmp_path, monkeypatch, enabled=True)
    write_file(str(home / "Desktop" / "a.txt"), 1, b"NEEDLE")
    write_file(str(home / "Documents" / "b.txt"), 1, b"NEEDLE")
    write_file(str(home / "work" / "c.txt"), 1, b"NEEDLE")
    # 搜索根就在 Desktop 里：按云盘根处理
    r, _ = _search(tmp_path, home / "Desktop", target="NEEDLE")
    assert r["checked"] is False and r["reason"] == "cloud_root_skipped"
    r, _ = _search(tmp_path, home / "documents", target="NEEDLE")
    assert r["checked"] is False and r["reason"] == "cloud_root_skipped"
    # 搜索根是 ~：Desktop/Documents 被排除，所以不能报“已核实”
    r, _ = _search(tmp_path, home, target="NEEDLE")
    assert r["checked"] is False and r["reason"] == "icloud_desktop_documents_excluded", r
    # 同步关闭：同一个 ~ 照常搜索并核实
    off = tmp_path / "off"
    write_file(str(off / "Desktop" / "a.txt"), 1, b"NEEDLE")
    monkeypatch.setattr(scan, "HOME", str(off))
    scan.reset_read_state()
    r, _ = _search(tmp_path, off, target="NEEDLE")
    assert r["checked"] is True and list(r["files"]) == [str(off / "Desktop" / "a.txt")]


@pytest.mark.skipif(shutil.which("rg") is None, reason="需要 rg")
def test_real_rg_icloud_dd_excluded_from_any_cwd(tmp_path, monkeypatch, clean_read_state):
    home = _home_with_icloud_dd(tmp_path, monkeypatch, enabled=True)
    write_file(str(home / "Desktop" / "a.txt"), 1, b"NEEDLE")
    write_file(str(home / "Documents" / "b.txt"), 1, b"NEEDLE")
    write_file(str(home / "work" / "c.txt"), 1, b"NEEDLE")
    write_file(str(home / "Library" / "Mobile Documents" / "x" / "d.txt"), 1, b"NEEDLE")
    monkeypatch.setattr(scan, "RG", shutil.which("rg"))
    want = {str(home / "work" / "c.txt")}
    for root in (home, home.parent):
        got = scan._grep_scope(str(root), ["NEEDLE"], scan.time.monotonic() + 60, [])
        assert {f for f in got if f.startswith(str(home))} == want, root


@needs_git
def test_real_git_grep_excludes_cloud_dirs_not_anchored_to_root(tmp_path, monkeypatch, clean_read_state):
    # 根不是家目录，云盘目录出现在仓库深处；大小写不同也要排除
    repo = tmp_path / "proj"
    for rel in ("keep/a.txt", "sub/Library/Mobile Documents/b.txt", "Library/CloudStorage/c.txt",
                "x/library/mobile documents/d.txt", "x/LIBRARY/CLOUDSTORAGE/e.txt"):
        write_file(str(repo / rel), 1, b"NEEDLE")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    monkeypatch.setattr(scan, "RG", None)
    monkeypatch.setattr(scan, "HOME", str(tmp_path / "home"))
    got = scan._grep_scope(str(repo), ["NEEDLE"], scan.time.monotonic() + 60, [])
    assert got == {str(repo / "keep" / "a.txt")}


@needs_git
def test_real_git_grep_excludes_icloud_dd_when_root_is_home(tmp_path, monkeypatch, clean_read_state):
    home = _home_with_icloud_dd(tmp_path, monkeypatch, enabled=True)
    for rel in ("Desktop/a.txt", "Documents/b.txt", "work/c.txt"):
        write_file(str(home / rel), 1, b"NEEDLE")
    git(home, "init", "-q")
    git(home, "add", "-A")
    monkeypatch.setattr(scan, "RG", None)
    issues = []
    got = scan._grep_scope(str(home), ["NEEDLE"], scan.time.monotonic() + 60, issues)
    assert got == {str(home / "work" / "c.txt")}
    assert "icloud_desktop_documents_excluded" in issues


def test_walker_does_not_enter_cloud_or_dataless_dirs(tmp_path, monkeypatch, clean_read_state):
    home = tmp_path.resolve() / "home"
    write_file(str(home / "Library" / "Mobile Documents" / "x" / "big.bin"), 2 * MB)
    write_file(str(home / "Library" / "CloudStorage" / "y" / "big.bin"), 2 * MB)
    write_file(str(home / "gone" / "big.bin"), 2 * MB)
    write_file(str(home / "real" / "big.bin"), 2 * MB)
    monkeypatch.setattr(scan, "HOME", str(home))
    real_is_dataless = scan.is_dataless
    monkeypatch.setattr(scan, "is_dataless",
                        lambda p, st=None: os.path.basename(str(p)) == "gone" or real_is_dataless(p, st))
    entered = []
    real_scandir = os.scandir
    monkeypatch.setattr(os, "scandir", lambda p=".": (entered.append(str(p)), real_scandir(p))[1])
    facts = run_scan(tmp_path, [home])
    paths = set(node_map(facts))
    assert not [p for p in paths if "Mobile Documents" in p or "CloudStorage" in p or p.endswith("/gone")]
    assert any(p.endswith("/real") for p in paths)
    assert not [e for e in entered if "Mobile Documents" in e or "CloudStorage" in e or e.endswith("/gone")]
    reasons = {os.path.basename(s["path"]): s["reason"] for s in facts["skipped"]}
    assert reasons["Mobile Documents"] == "cloud-not-entered" and reasons["CloudStorage"] == "cloud-not-entered"
    assert reasons["gone"] == "dataless-dir-not-entered"


def test_find_copies_and_session_mentions_report_incomplete_when_root_skipped(tmp_path, monkeypatch, clean_read_state):
    home = tmp_path / "home"
    cloud = home / "Library" / "CloudStorage" / "d"
    ok = tmp_path / "ok"
    write_file(str(cloud / "c.png"), 5, b"a")
    write_file(str(ok / "x.txt"), 1)
    src = tmp_path / "src.png"
    write_file(str(src), 5, b"a")
    monkeypatch.setattr(scan, "HOME", str(home))
    found, complete = scan.find_copies([str(src)], [str(cloud), str(ok)], 1e18)
    assert found == {} and complete is False
    _, complete = scan.find_copies([str(src)], [str(ok)], 1e18)
    assert complete is True
    if scan.RG:
        sess = tmp_path / "sess"
        sess.mkdir()
        (sess / "rollout-x-00000000-aaaa-bbbb-cccc-000000000000.jsonl").write_text("{}")
        _, done, _, _ = scan.session_mentions(["11111111-aaaa-bbbb-cccc-000000000000"], [str(sess), str(cloud)], {}, scan.time.monotonic() + 30)
        assert done is False


def test_scan_refuses_when_env_home_differs(monkeypatch, capsys):
    monkeypatch.setenv("HOME", "/")
    assert scan.main(["scan"]) == 2
    assert "HOME" in capsys.readouterr().err


# ───────────── 应用清单与 bundle id 反查（P3.2）─────────────

import plistlib  # noqa: E402


def make_app(root, rel, bundle_id, name=None, short="1.0", build="100"):
    app = os.path.join(str(root), rel)
    os.makedirs(os.path.join(app, "Contents"), exist_ok=True)
    info = {"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": short, "CFBundleVersion": build}
    if name:
        info["CFBundleName"] = name
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(info, f)
    return app


@pytest.fixture
def fx(tmp_path, monkeypatch):
    """家目录与应用根都指到夹具；清单缓存隔离。"""
    home = tmp_path / "home"
    apps_root = tmp_path / "Applications"
    home.mkdir()
    apps_root.mkdir()
    monkeypatch.setattr(scan.protect, "HOME", str(home))
    monkeypatch.setattr(scan, "HOME", str(home))   # _glob_regex 读的是 scan.HOME
    monkeypatch.setattr(scan, "APP_ROOTS", [(str(apps_root), 1), ("~/Applications", 0),
                                            (str(tmp_path / "Caskroom"), 3)])
    scan._APPS_CACHE.clear()
    make_app(apps_root, "Google Chrome.app", "com.google.Chrome", "Chrome", "120.0")
    make_app(apps_root, "WeChat.app", "com.tencent.xinWeChat", "WeChat", "3.8")
    make_app(apps_root, "QQ.app", "com.tencent.qq", "QQ", "6.9")
    make_app(apps_root, "Adobe/Photoshop.app", "com.adobe.Photoshop", "Photoshop")
    make_app(apps_root, "Safari.app", "com.apple.Safari", "Safari")
    make_app(apps_root, "Mixed.app", "Org.Example.MixedCase", "Mixed")
    yield type("Fx", (), {"home": str(home), "apps_root": apps_root, "tmp": tmp_path})
    scan._APPS_CACHE.clear()


def lib(fx, *parts):
    return os.path.join(fx.home, "Library", *parts)


def lookup(fx, *parts):
    return scan.reverse_lookup(lib(fx, *parts))


def test_installed_apps_inventory(fx):
    inv = scan.installed_apps()
    assert inv["complete"] is True and inv["errors"] == []
    assert len(inv["apps"]) == 6                              # 含 Adobe 子文件夹里一层
    rec = inv["by_id"]["com.google.chrome"][0]                # key 小写
    assert rec["bundle_id"] == "com.google.Chrome" and rec["short_version"] == "120.0"
    assert rec["version"] == "100" and rec["name"] == "Chrome"
    assert scan.installed_apps() is inv                       # 缓存：同一次扫描只算一次


def test_installed_apps_cask_depth_and_home_apps(fx):
    make_app(fx.tmp / "Caskroom", "foo/1.2/Foo.app", "com.foo.app")
    make_app(fx.tmp / "Caskroom", "a/b/c/d/Deep.app", "com.too.deep")   # 超过 4 层，不收
    make_app(fx.home + "/Applications", "Mine.app", "com.me.mine")
    scan._APPS_CACHE.clear()
    inv = scan.installed_apps()
    assert "com.foo.app" in inv["by_id"] and "com.me.mine" in inv["by_id"]
    assert "com.too.deep" not in inv["by_id"]


def test_installed_apps_incomplete_on_unreadable_root(fx):
    if os.geteuid() == 0:
        pytest.skip("root 不受权限位限制")
    os.makedirs(str(fx.apps_root / "Locked"), exist_ok=True)
    os.chmod(str(fx.apps_root / "Locked"), 0)
    try:
        scan._APPS_CACHE.clear()
        inv = scan.installed_apps()
    finally:
        os.chmod(str(fx.apps_root / "Locked"), 0o755)
    assert inv["complete"] is False and inv["errors"]


def test_installed_apps_incomplete_on_bad_plist(fx):
    app = make_app(fx.apps_root, "Broken.app", "com.broken.x")
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        f.write(b"not a plist")
    scan._APPS_CACHE.clear()
    inv = scan.installed_apps()
    assert inv["complete"] is False and any(e["path"] == app for e in inv["errors"])


def test_reverse_exact_all_locations(fx):
    cases = [("Caches", "com.google.Chrome"), ("Containers", "com.google.Chrome"),
             ("Group Containers", "com.google.Chrome"), ("Application Support", "com.google.Chrome"),
             ("HTTPStorages", "com.google.Chrome"), ("WebKit", "com.google.Chrome"),
             ("Saved Application State", "com.google.Chrome.savedState"), ("Logs", "com.google.Chrome"),
             ("Preferences", "com.google.Chrome.plist")]
    for loc, name in cases:
        r = lookup(fx, loc, name)
        assert r["app_match"] == "exact", (loc, r)
        assert r["app"]["bundle_id"] == "com.google.Chrome" and r["app"]["name"] == "Chrome"
        assert r["app"]["version"] == "120.0" and r["app"]["path"].endswith("Google Chrome.app")
        assert r["app_installed"] is True
    # 取第一层名字：更深的节点同样归属；大小写不敏感
    assert scan.reverse_lookup(lib(fx, "Caches", "com.google.Chrome", "sub", "x"))["app_match"] == "exact"
    assert lookup(fx, "Caches", "ORG.example.mixedcase")["app"]["bundle_id"] == "Org.Example.MixedCase"


def test_reverse_outside_known_locations_is_null(fx):
    for p in (lib(fx, "Caches"), lib(fx, "Fonts", "com.google.Chrome"), fx.home + "/proj/com.google.Chrome",
              lib(fx, "Preferences", "com.google.Chrome")):   # 缺 .plist 后缀不算
        assert scan.reverse_lookup(p) == {"app": None, "app_match": None, "app_installed": None}


def test_reverse_group_container_prefix(fx):
    r = lookup(fx, "Group Containers", "UBF8T346G9.com.tencent.xinWeChat")
    assert r["app_match"] == "group_prefix" and r["app"]["bundle_id"] == "com.tencent.xinWeChat"
    r = lookup(fx, "Group Containers", "group.com.adobe.Photoshop")
    assert r["app_match"] == "group_prefix" and r["app_installed"] is True
    # 前缀不是 group./TeamID 形态时不剥
    assert lookup(fx, "Group Containers", "xx.com.tencent.xinWeChat")["app_match"] != "group_prefix"


def test_reverse_container_metadata(fx):
    d = lib(fx, "Containers", "8F2A-UUID-LIKE")
    os.makedirs(d)
    with open(os.path.join(d, scan.CONTAINER_META), "wb") as f:
        plistlib.dump({"MCMMetadataIdentifier": "com.adobe.Photoshop", "other": 1}, f)
    r = scan.reverse_lookup(d)
    assert r["app_match"] == "container_metadata" and r["app"]["bundle_id"] == "com.adobe.Photoshop"
    assert r["app_installed"] is True
    # 元数据指向未安装的软件 / 没有元数据 -> 不归属
    d2 = lib(fx, "Containers", "NO-META")
    os.makedirs(d2)
    assert scan.reverse_lookup(d2)["app"] is None


def test_reverse_lineage_descendant_and_ancestor(fx):
    r = lookup(fx, "Caches", "com.google.Chrome.helper")              # 名字是已装 id 的后代
    assert r["app_match"] == "lineage" and r["app"]["bundle_id"] == "com.google.Chrome"
    assert r["app_installed"] is True
    r = lookup(fx, "Application Support", "com.adobe.Photoshop")      # 先确认 exact 优先
    assert r["app_match"] == "exact"
    make_app(fx.apps_root, "Canary.app", "com.google.Chrome.canary", "Canary")
    scan._APPS_CACHE.clear()
    r = lookup(fx, "Caches", "com.google.Chrome")                     # exact 仍优先于血缘
    assert r["app_match"] == "exact" and r["app"]["bundle_id"] == "com.google.Chrome"
    r = lookup(fx, "Caches", "com.tencent.qq.helper.deep")
    assert r["app_match"] == "lineage" and r["app"]["bundle_id"] == "com.tencent.qq"
    # 名字是已装 id 的祖先（已装的是 Chrome.beta，没有 Chrome 本体）
    make_app(fx.apps_root, "Beta.app", "com.vendor.Tool.beta", "Beta")
    scan._APPS_CACHE.clear()
    r = lookup(fx, "Caches", "com.vendor.Tool")
    assert r["app_match"] == "lineage" and r["app"]["bundle_id"] == "com.vendor.Tool.beta"


def test_reverse_lookalike_is_not_lineage(fx):
    # 名称相似、点分段不同，不能互相归属
    r = lookup(fx, "Caches", "com.tencent.xinWeChatExt")
    assert r["app"] is None and r["app_match"] is None
    r = lookup(fx, "Caches", "com.tencent.xin")
    assert r["app"] is None
    r = lookup(fx, "Caches", "com.tencent.qqmusic")                   # 是 com.tencent.qq 的字符串前缀，不是血缘
    assert r["app"] is None and r["app_installed"] is False
    r = lookup(fx, "Caches", "com.tencent.xinWeChat")
    assert r["app"]["bundle_id"] == "com.tencent.xinWeChat" and r["app"]["bundle_id"] != "com.tencent.qq"
    # 厂商级前缀（不足 3 段）不把整家算作祖先
    r = lookup(fx, "Caches", "com.tencent")
    assert r["app"] is None


def test_reverse_ambiguous_ancestor_not_attributed(fx):
    make_app(fx.apps_root, "A.app", "com.multi.Suite.one")
    make_app(fx.apps_root, "B.app", "com.multi.Suite.two")
    scan._APPS_CACHE.clear()
    assert lookup(fx, "Caches", "com.multi.Suite")["app"] is None


def test_reverse_shared_components_never_attributed(fx):
    for name in ("com.apple.Safari", "com.apple.Safari.helper", "org.chromium.Chromium", "com.google.Keystone",
                 "com.google.Keystone.Agent", "com.google.GoogleUpdater", "com.google.SoftwareUpdate",
                 "org.sparkle-project.Sparkle", "com.github.Electron", "com.electron.foo", "org.qt-project.Qt",
                 "io.qt.x", "com.google.Chrome.crashpad", "Crashpad", "com.foo.Bar.crashpad_handler"):
        r = lookup(fx, "Caches", name)
        assert r == {"app": None, "app_match": None, "app_installed": None, "shared_component": True}, name
    # 即使 com.apple.Safari 确实已装也不归属；Group Container 剥前缀后同样判共享
    r = lookup(fx, "Group Containers", "group.com.apple.notes")
    assert r["shared_component"] is True and r["app"] is None
    assert "shared_component" not in lookup(fx, "Caches", "com.google.Chrome")


def test_reverse_installed_false_null_and_incomplete(fx):
    r = lookup(fx, "Caches", "com.removed.OldApp")
    assert r["app"] is None and r["app_installed"] is False           # 像 bundle id 且清单完整、找不到
    assert lookup(fx, "Caches", "Google")["app_installed"] is None    # 不像 bundle id
    assert lookup(fx, "Caches", "Some Tool.v2")["app_installed"] is None
    assert lookup(fx, "Caches", "org.only.one")["app_installed"] is False   # 恰好两个点
    assert lookup(fx, "Caches", "foo.bar")["app_installed"] is None   # 只有一个点
    # 清单不完整：一律 null，不下结论（命中的也不报 True）
    inv = dict(scan.installed_apps(), complete=False)
    r = scan.reverse_lookup(lib(fx, "Caches", "com.removed.OldApp"), inv)
    assert r["app_installed"] is None
    r = scan.reverse_lookup(lib(fx, "Caches", "com.google.Chrome"), inv)
    assert r["app"]["bundle_id"] == "com.google.Chrome" and r["app_installed"] is None


def write_producers(tmp_path, schema, rules):
    p = tmp_path / ("producers-%s.json" % str(schema).replace("/", "_"))
    data = {"rules": rules}
    if schema is not None:
        data["schema"] = schema
    p.write_text(json.dumps(data))
    return str(p)


def test_producer_rule_matches_by_bundle_ids(fx):
    rules = [{"glob": "~/Library/Containers/com.nomatch.nothing", "tool": "chrome", "card": "chat-netdisk",
              "hint": "h", "bundle_ids": ["COM.GOOGLE.chrome"], "safety": "moderate",
              "blocked_by_processes": ["Google Chrome"], "default_select": False},
             {"glob": "~/Library/Containers", "tool": "generic", "card": "macos-system", "hint": "g"}]
    pr = scan.Producers(write_producers(fx.tmp, "producers/2", rules))
    inv = scan.installed_apps()
    d = lib(fx, "Containers", "RANDOM-SANDBOX-ID")
    os.makedirs(d)
    with open(os.path.join(d, scan.CONTAINER_META), "wb") as f:
        plistlib.dump({"MCMMetadataIdentifier": "com.google.Chrome"}, f)
    m = pr.match(d, inv)                                   # glob 不命中，靠反查到的 bundle id 命中第一条
    assert m["tool"] == "chrome" and m["card"] == "chat-netdisk"
    assert m["safety"] == "moderate" and m["blocked_by_processes"] == ["Google Chrome"]
    assert m["default_select"] is False and m["bundle_ids"] == ["COM.GOOGLE.chrome"]
    assert m["app"]["bundle_id"] == "com.google.Chrome" and m["app_match"] == "container_metadata"
    # 未反查到该 bundle id 的容器落到后面的通用规则；通用规则没有新字段
    other = lib(fx, "Containers", "com.removed.OldApp")
    m2 = pr.match(other, inv)
    assert m2["tool"] == "generic" and "safety" not in m2 and "bundle_ids" not in m2
    # 不传 apps = 纯 glob 匹配，没有 app 字段，bundle_ids 不起作用
    m3 = pr.match(d)
    assert m3["tool"] == "generic" and "app" not in m3


def test_producer_invalid_optional_fields_ignored(fx):
    rules = [{"glob": "~/x", "tool": "t", "card": "c", "hint": "h", "safety": "nuclear",
              "blocked_by_processes": "notalist", "default_select": "yes", "bundle_ids": "notalist"}]
    m = scan.Producers(write_producers(fx.tmp, "producers/2", rules)).match(fx.home + "/x")
    assert m == {"tool": "t", "card": "c", "hint": "h"}


def test_producers_v1_file_still_loads(fx):
    rules = [{"glob": "~/.foo", "tool": "foo", "card": "codex", "hint": "old"}]
    pr = scan.Producers(write_producers(fx.tmp, "producers/1", rules))
    assert pr.schema_error is None
    assert pr.match(fx.home + "/.foo/x") == {"tool": "foo", "card": "codex", "hint": "old"}
    assert scan.Producers(write_producers(fx.tmp, None, rules)).match(fx.home + "/.foo")["tool"] == "foo"
    # 未知 schema 不加载规则并给出原因
    bad = scan.Producers(write_producers(fx.tmp, "producers/9", rules))
    assert bad.rules == [] and bad.schema_error
    # 随包的 producers.json 是 v2 且示范新字段
    shipped = json.load(open(os.path.join(SCRIPTS, "producers.json"), encoding="utf-8"))
    assert shipped["schema"] == "producers/2"
    assert any(r.get("bundle_ids") for r in shipped["rules"])


def test_scan_writes_app_facts_into_producer(fx):
    root = fx.tmp / "tree"
    write_file(str(root / "a" / "a.bin"), 2 * MB)
    facts = run_scan(fx.tmp, [root])
    prod = node_map(facts)[str(root / "a")]["producer"]
    assert prod["app"] is None and prod["app_match"] is None and prod["app_installed"] is None


def test_probe_system_is_registered(monkeypatch, capsys):
    import sysprobe
    monkeypatch.setattr(sysprobe, "probe_system", lambda opts: {"ok": True, "run": opts.run})
    assert scan.main(["probe", "system", "--run", "/x"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "run": "/x"}


# ───────── P2.1 容量口径 / 覆盖率语义 / firmlink ─────────

FAKE_CAP = {"statvfs_free": 100, "statvfs_total": 1000, "apfs_container_free": 120, "apfs_container_size": 2000,
            "finder_available": 150, "purgeable_estimate": 50, "finder_reason": None, "diskutil_reason": None,
            "purgeable_reason": None}


@pytest.fixture(autouse=True)
def _fast_capacity(monkeypatch):
    """默认不真跑 swift/diskutil，避免每个 do_scan 测试多等数秒。"""
    monkeypatch.setattr(scan.sysprobe, "probe_capacity", lambda: dict(FAKE_CAP))


def test_capacity_three_views_in_facts(tmp_path):
    root = tmp_path.resolve() / "t"
    write_file(str(root / "a" / "f.bin"), 2 * MB)
    cap = run_scan(tmp_path, [root])["capacity"]
    assert cap["statvfs_free"] == 100 and cap["container_free"] == 120 and cap["container_size"] == 2000
    assert cap["finder_available"] == 150 and cap["purgeable_estimate"] == 50


def test_capacity_null_when_swift_times_out(tmp_path, monkeypatch):
    cap_in = dict(FAKE_CAP, finder_available=None, purgeable_estimate=None, finder_reason="超时",
                  purgeable_reason="缺少 Finder 或 statvfs 口径")
    monkeypatch.setattr(scan.sysprobe, "probe_capacity", lambda: cap_in)
    root = tmp_path.resolve() / "t"
    write_file(str(root / "a" / "f.bin"), 2 * MB)
    facts = run_scan(tmp_path, [root])
    assert facts["capacity"]["finder_available"] is None and facts["capacity"]["purgeable_estimate"] is None
    assert facts["capacity"]["statvfs_free"] == 100
    assert "a" in node_map(facts)[str(root / "a")]["path"]


def test_capacity_probe_exception_does_not_fail_scan(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("swift 挂了")
    monkeypatch.setattr(scan.sysprobe, "probe_capacity", boom)
    root = tmp_path.resolve() / "t"
    write_file(str(root / "a" / "f.bin"), 2 * MB)
    cap = run_scan(tmp_path, [root])["capacity"]
    assert cap["statvfs_free"] is None and cap["finder_available"] is None and "probe" in cap["reasons"]


def test_summary_mentions_capacity_and_null(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scan.sysprobe, "probe_capacity", lambda: dict(FAKE_CAP, finder_available=None, purgeable_estimate=None))
    root = tmp_path.resolve() / "t"
    write_file(str(root / "a" / "f.bin"), 2 * MB)
    facts = run_scan(tmp_path, [root])
    scan.print_summary(str(tmp_path), facts)
    out = capsys.readouterr().out
    assert "容量三口径" in out and "估算" in out and "测不到" in out


def test_complete_scan_status_on_clean_fixture(tmp_path):
    root = tmp_path.resolve() / "t"
    write_file(str(root / "a" / "f.bin"), 2 * MB)
    facts = run_scan(tmp_path, [root])
    assert facts["coverage"]["scan_status"] == "complete"
    assert facts["coverage"]["inaccessible_count"] == 0
    assert all(n["bytes_lower_bound"] is False for n in facts["nodes"])


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 不受 chmod 000 限制")
def test_permission_denied_marks_lower_bound_and_partial(tmp_path, capsys):
    root = tmp_path.resolve() / "t"
    write_file(str(root / "a" / "f.bin"), 2 * MB)
    write_file(str(root / "b" / "f.bin"), 2 * MB)
    locked = root / "a" / "locked"
    locked.mkdir()
    (locked / "hidden.bin").write_bytes(b"x")
    os.chmod(locked, 0)
    try:
        facts = run_scan(tmp_path, [root])
        nm = node_map(facts)
        assert facts["coverage"]["scan_status"] == "partial"
        assert facts["coverage"]["inaccessible_count"] >= 1
        assert nm[str(root / "a")]["bytes_lower_bound"] is True
        assert nm[str(root / "b")]["bytes_lower_bound"] is False
        scan.print_summary(str(tmp_path), facts)
        assert "体积是下限，不是 0" in capsys.readouterr().out
    finally:
        os.chmod(locked, 0o700)


def test_firmlink_data_volume_skipped_when_root_covers(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "t"
    write_file(str(root / "System" / "Volumes" / "Data" / "f.bin"), 2 * MB)
    write_file(str(root / "keep" / "f.bin"), 2 * MB)
    dup = str(root / "System" / "Volumes" / "Data")
    monkeypatch.setattr(scan, "is_firmlink_dup", lambda p: p == dup)
    monkeypatch.setattr(scan, "roots_cover_data_volume", lambda roots: True)
    facts = run_scan(tmp_path, [root])
    assert dup not in node_map(facts)
    assert any(s["reason"] == "firmlink-duplicate" and s["path"] == dup for s in facts["skipped"])
    assert facts["coverage"]["scan_status"] == "complete"   # 去重跳过不算未计完


def test_firmlink_not_skipped_without_cover(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "t"
    write_file(str(root / "System" / "Volumes" / "Data" / "f.bin"), 2 * MB)
    dup = str(root / "System" / "Volumes" / "Data")
    monkeypatch.setattr(scan, "is_firmlink_dup", lambda p: p == dup)
    facts = run_scan(tmp_path, [root])
    assert dup in node_map(facts)


def test_roots_cover_data_volume_helper():
    assert scan.roots_cover_data_volume(["/", "/x"]) and scan.roots_cover_data_volume(["/System/Volumes/Data"])
    assert not scan.roots_cover_data_volume(["/Users/x"])
    assert scan.is_firmlink_dup("/System/Volumes/Data") and not scan.is_firmlink_dup("/Users")


def test_tree_size_does_not_cross_device(tmp_path, monkeypatch):
    d = tmp_path / "t"
    write_file(str(d / "a" / "f.bin"), 1 * MB)
    write_file(str(d / "m" / "f.bin"), 1 * MB)
    real_lstat = os.lstat
    full, _ = scan.tree_size(str(d))
    st_dev = real_lstat(str(d)).st_dev
    real_scandir = os.scandir

    class E:
        def __init__(self, e):
            self._e, self.path, self.name = e, e.path, e.name
        def is_dir(self, follow_symlinks=False):
            return self._e.is_dir(follow_symlinks=follow_symlinks)
        def stat(self, follow_symlinks=False):
            st = self._e.stat(follow_symlinks=follow_symlinks)
            if self.path.endswith("/m"):
                return os.stat_result(tuple(st)[:2] + (st_dev + 1,) + tuple(st)[3:])
            return st
        def __getattr__(self, k):
            return getattr(self._e, k)

    class It:
        def __init__(self, path):
            self._it = real_scandir(path)
        def __enter__(self):
            return self
        def __iter__(self):
            return (E(e) for e in self._it)
        def __exit__(self, *a):
            return self._it.__exit__(*a)

    monkeypatch.setattr(scan.os, "scandir", lambda p: It(p))
    cut, _ = scan.tree_size(str(d))
    assert cut < full


@pytest.mark.parametrize("kind,mod,fname,extra", [
    ("updaters", "updaters", "probe_updaters", []),
    ("leftovers", "leftovers", "probe_leftovers", ["--min-age-days", "7", "--brew-zap"]),
])
def test_probe_new_modules_registered(monkeypatch, capsys, kind, mod, fname, extra):
    m = __import__(mod)
    monkeypatch.setattr(m, fname, lambda opts: {"kind": kind, "opts": {k: v for k, v in vars(opts).items() if k != "cmd"}})
    assert scan.main(["probe", kind] + extra) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == kind
    if kind == "leftovers":
        assert out["opts"]["min_age_days"] == 7 and out["opts"]["brew_zap"] is True


def test_project_marker_present(tmp_path):
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "proj" / "Cargo.toml"), 10)
    write_file(str(root / "proj" / "target" / "a.bin"), 2 * MB)
    n = node_map(run_scan(tmp_path, [root]))[str(root / "proj" / "target")]
    assert n["project_marker"] == {"marker": "Cargo.toml", "ecosystem": "rust"}


def test_project_marker_missing_is_null(tmp_path):
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "x" / "target" / "a.bin"), 2 * MB)
    n = node_map(run_scan(tmp_path, [root]))[str(root / "x" / "target")]
    assert n["project_marker"] is None
    assert n["project_marker_checked"] is True


def test_project_marker_absent_for_unlisted_name(tmp_path):
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "proj" / "Cargo.toml"), 10)
    write_file(str(root / "proj" / "other" / "a.bin"), 2 * MB)
    n = node_map(run_scan(tmp_path, [root]))[str(root / "proj" / "other")]
    assert "project_marker" not in n


def test_project_marker_glob(tmp_path):
    root = tmp_path.resolve() / "tree"
    write_file(str(root / "app" / "App.csproj"), 10)
    write_file(str(root / "app" / "bin" / "a.bin"), 2 * MB)
    n = node_map(run_scan(tmp_path, [root]))[str(root / "app" / "bin")]
    assert n["project_marker"] == {"marker": "App.csproj", "ecosystem": "dotnet"}


def test_summary_warns_when_reference_scopes_incomplete(tmp_path, capsys):
    cov = {"refs_scopes": [{"root": "/a", "ok": True}, {"root": "/b", "ok": False}], "refs_seconds": 180.0,
           "frontier_bytes": 10, "walked_bytes": 10, "dirs": 1, "files": 1, "seconds": 1, "walk_seconds": 1,
           "incomplete": False, "scan_status": "complete"}
    facts = {"scan_id": "s", "coverage": cov, "skipped": [], "disk": {"used": 1, "total": 2, "free": 1},
             "nodes": [{"id": "n1", "path": "/x", "bytes": 10, "frontier": True, "refs": {"checked": False}}]}
    try:
        scan.print_summary(str(tmp_path), facts)
    except (KeyError, TypeError):
        pass        # 节点列表部分依赖完整节点字段；这里只关心警告行在节点列表之前打印
    out = capsys.readouterr().out
    assert "引用检查有 1 个搜索根没查完" in out and "--refs-seconds 1800" in out
    cov["refs_scopes"][1]["ok"] = True
    try:
        scan.print_summary(str(tmp_path), facts)
    except (KeyError, TypeError):
        pass
    assert "没查完" not in capsys.readouterr().out


# ---------------------------------------------------------------- 0.5：遍历阶段的进度（只写 stderr）

def test_walker_reports_progress_periodically(tmp_path):
    root = tmp_path / "r"
    for i in range(5):
        write_file(str(root / ("d%d" % i) / "f.bin"), 2 * MB)
    seen = []
    clock = {"t": 1000.0}
    real_mono = scan.time.monotonic
    scan.time.monotonic = lambda: clock["t"]            # 每次调用前进 40 秒：超过 30 秒就该报一次
    try:
        def tick(w):
            seen.append((w.dirs, w.bytes_seen))
        w = scan.Walker(1 * MB, 10, 10 ** 9, time.time(), progress_every=30.0, progress=tick)
        orig_walk = w.walk

        def stepping(path, depth, reg):
            clock["t"] += 40
            return orig_walk(path, depth, reg)
        w.walk = stepping
        w.walk_root(str(root))
    finally:
        scan.time.monotonic = real_mono
    assert len(seen) >= 3
    assert [d for d, _ in seen] == sorted(d for d, _ in seen) and seen[-1][1] > 0     # 目录数、字节数随遍历增长


def test_progress_disabled_when_zero_and_not_reported_before_interval(tmp_path):
    root = tmp_path / "r"
    write_file(str(root / "a" / "f.bin"), 2 * MB)
    calls = []
    w = scan.Walker(1 * MB, 10, time.monotonic() + 600, time.time(), progress_every=0, progress=calls.append)
    w.walk_root(str(root))
    assert calls == []
    w = scan.Walker(1 * MB, 10, time.monotonic() + 600, time.time(), progress_every=3600, progress=calls.append)
    w.walk_root(str(root))
    assert calls == []                                    # 没到间隔不报


def test_scan_progress_goes_to_stderr_not_stdout(tmp_path, capsys):
    root = tmp_path / "r"
    for i in range(3):
        write_file(str(root / ("d%d" % i) / "f.bin"), 2 * MB)
    pf = tmp_path / "p.json"
    pf.write_text(json.dumps({"roots": [str(root)], "extra_roots": [], "reference_roots": [], "protect": [], "notes": {}}))
    assert scan.main(["scan", "--profile", str(pf), "--runs-dir", str(tmp_path / "runs"), "--threshold", "1MB",
                      "--no-lsof", "--no-refs", "--progress-seconds", "0.000001"]) == 0
    cap = capsys.readouterr()
    assert "[扫描进度] 已遍历" in cap.err and "个目录" in cap.err and "用时" in cap.err
    assert "[扫描进度]" not in cap.out


# ---------------------------------------------------------------- 0.5：large-old-files 只套给单个文件，不套整个子目录

def test_large_old_files_card_matches_only_files_not_subdirs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(scan, "HOME", str(home))
    movies = home / "Movies"
    write_file(str(movies / "录屏" / "rec.mov"), 1 * MB)                 # 子目录里的文件
    write_file(str(movies / "录屏" / "项目" / "deep.mov"), 1 * MB)
    write_file(str(movies / "top.mov"), 1 * MB)
    p = scan.Producers()
    for name in ("Movies", "Downloads", "Desktop"):
        write_file(str(home / name / "sub" / "x.bin"), 1 * MB)
        # 子目录：不套 large-old-files，也不带它的 hint
        d = p.match(str(home / name / "sub"))
        assert d["card"] is None and d["tool"] is None, name
        # 单个文件：套
        f = p.match(str(home / name / "sub" / "x.bin"))
        assert f["card"] == "large-old-files" and f["safety"] == "moderate" and f["default_select"] is False, name
        # 目录根本身仍是入口（提示去跑 probe bigfiles），但它的子目录不连带
        r = p.match(str(home / name))
        assert r["card"] == "large-old-files", name
    assert p.match(str(movies / "录屏"))["card"] is None
    assert p.match(str(movies / "录屏" / "项目"))["card"] is None
    assert p.match(str(movies / "录屏" / "rec.mov"))["card"] == "large-old-files"
    assert p.match(str(movies / "top.mov"))["card"] == "large-old-files"
    # 不存在的路径不是“已存在的文件”，不套
    assert p.match(str(movies / "ghost.mov"))["card"] is None
    # 符号链接指向目录：链接本身不算目录，但它在 Movies 下是个文件系统条目——按链接本身（非目录）处理
    os.symlink(str(movies / "录屏"), str(movies / "link"))
    assert p.match(str(movies / "link"))["card"] == "large-old-files"


def test_producer_rule_options_exact_and_only_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(scan, "HOME", str(home))
    write_file(str(home / "X" / "d" / "f.bin"), 1)
    rules = [{"glob": "~/X", "exact": True, "tool": "a", "card": "macos-system", "hint": "root"},
             {"glob": "~/X", "only": "file", "tool": "b", "card": "macos-system", "hint": "files"}]
    pf = tmp_path / "p.json"
    pf.write_text(json.dumps({"schema": "producers/2", "rules": rules}))
    p = scan.Producers(str(pf))
    assert p.match(str(home / "X"))["tool"] == "a"
    assert p.match(str(home / "X" / "d"))["tool"] is None
    assert p.match(str(home / "X" / "d" / "f.bin"))["tool"] == "b"
