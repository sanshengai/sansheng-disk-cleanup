"""只验证独立的临时 Git 索引；不修改源索引、对象库或工作区。"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


def git(repo, *args, extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    env.update(extra or {})
    p = subprocess.run(["git", "-C", str(repo), *args], env=env,
                       capture_output=True, timeout=60)
    if p.returncode:
        raise ValueError("Git 检查失败: " + p.stderr.decode(errors="replace")[:200])
    return p.stdout.decode().strip()


def fingerprint(path):
    st = os.lstat(path)
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def index_tree(path, repo):
    """在临时对象库和索引副本上计算树；原 Git 数据只读。"""
    path, repo = Path(path), Path(repo)
    if not path.is_absolute() or not repo.is_absolute():
        raise ValueError("索引与仓库必须是绝对路径")
    if path.resolve() != path or repo.resolve() != repo:
        raise ValueError("不接受符号链接路径")
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or path.suffix != ".index":
        raise ValueError("只接受独立普通 .index 文件")
    if git(repo, "rev-parse", "--show-toplevel") != str(repo) or repo not in path.parents:
        raise ValueError("索引不在指定工作树内")
    for flag in ("--git-dir", "--git-common-dir"):
        admin = Path(git(repo, "rev-parse", "--path-format=absolute", flag)).resolve()
        if path == admin or admin in path.parents:
            raise ValueError("不接受 Git 管理目录里的索引")
    if git(repo, "ls-files", "--", str(path)):
        raise ValueError("不接受已跟踪的文件")
    git(repo, "check-ignore", "-q", "--", str(path))
    before = fingerprint(path)
    with path.open("rb") as f:
        if f.read(4) != b"DIRC":
            raise ValueError("不是 Git 索引")
    objects = git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    with tempfile.TemporaryDirectory(prefix="cleanup-index-proof-") as tmp:
        idx, obj = Path(tmp) / "index", Path(tmp) / "objects"
        obj.mkdir()
        shutil.copyfile(path, idx)
        digest = hashlib.sha256(idx.read_bytes()).hexdigest()
        tree = git(repo, "write-tree", extra={"GIT_INDEX_FILE": str(idx),
                   "GIT_OBJECT_DIRECTORY": str(obj), "GIT_ALTERNATE_OBJECT_DIRECTORIES": objects})
    if fingerprint(path) != before:
        raise ValueError("检查期间索引发生变化")
    return tree, digest


def verify(item):
    """每次重新验证，不能缓存放行；返回 (ok, reason)。"""
    try:
        proof = item.get("checkpoint")
        if not isinstance(proof, dict) or proof.get("kind") != "git_index":
            raise ValueError("缺少 git_index 证明")
        if item.get("method") != "delete" or item.get("scope") != "all" or item.get("keep"):
            raise ValueError("证明仅用于单个完整索引文件")
        repo, commit, ref = proof.get("repo"), proof.get("commit"), proof.get("retain_ref")
        if not isinstance(repo, str) or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("缺少仓库或完整提交号")
        if not isinstance(ref, str) or not ref.startswith(("refs/heads/", "refs/remotes/")):
            raise ValueError("必须指定保留的分支引用")
        git(repo, "merge-base", "--is-ancestor", commit, ref)
        expected = git(repo, "rev-parse", commit + "^{tree}")
        actual, digest = index_tree(item["path"], repo)
        if actual != expected:
            raise ValueError("索引内容没有完整保存在指定提交中")
        if proof.get("sha256") != digest:
            raise ValueError("索引指纹不匹配")
        return True, "临时索引完整内容已保存在保留分支的提交中"
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        return False, str(exc)
