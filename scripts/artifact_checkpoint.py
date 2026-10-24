"""只读验证有恢复依据的阶段过程件；不修改原始数据。"""
import hashlib
import json
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
        if not isinstance(item, dict):
            return False, "证明条目必须为对象"
        proof = item.get("checkpoint")
        if isinstance(proof, dict) and proof.get("kind") == "retired_test_copy":
            import retired_test_copy
            return retired_test_copy.verify(item)
        if isinstance(proof, dict) and proof.get("kind") == "retired_build_copy":
            import retired_worktree
            return retired_worktree.verify_item(item)
        if isinstance(proof, dict) and proof.get("kind") == "registered_stage_copy":
            import registered_stage_checkpoint
            return registered_stage_checkpoint.verify(item)
        if isinstance(proof, dict) and proof.get("kind") == "completed_output_stage":
            import completed_output_checkpoint
            return completed_output_checkpoint.verify(item)
        if isinstance(proof, dict) and proof.get("kind") == "git_blob_copy":
            return verify_blob_copy(item)
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


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def ordinary_path(value):
    p = Path(value)
    if not p.is_absolute() or p.resolve() != p:
        raise ValueError("要求无符号链接的绝对路径")
    st = p.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise ValueError("要求无硬链接的普通文件")
    return p


def retained_commit(repo, commit, ref):
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("要求完整提交号")
    if not isinstance(ref, str) or not ref.startswith(("refs/heads/", "refs/remotes/")):
        raise ValueError("要求明确保留的分支")
    root = Path(repo)
    if not root.is_absolute() or root.resolve() != root or git(root, "rev-parse", "--show-toplevel") != str(root):
        raise ValueError("保留仓库身份不符")
    git(root, "merge-base", "--is-ancestor", commit, ref)
    return root


def verify_blob_copy(item):
    """旧子任务的逐文件复制件。内容与任务完成分别证明；其余安全门照常运行。"""
    try:
        proof = item["checkpoint"]
        if item.get("method") != "delete" or item.get("scope") != "all" or item.get("keep"):
            raise ValueError("复制件证明只适用于单个完整文件")
        p = ordinary_path(item["path"])
        before = fingerprint(p)
        owner = Path(proof["owner_repo"])
        if (not owner.is_absolute() or owner.resolve() != owner or owner not in p.parents
                or git(p.parent, "rev-parse", "--show-toplevel") != str(owner)):
            raise ValueError("复制件所属仓库不符")
        for flag in ("--git-dir", "--git-common-dir"):
            admin = Path(git(owner, "rev-parse", "--path-format=absolute", flag)).resolve()
            if p == admin or admin in p.parents:
                raise ValueError("不得清理 Git 管理目录")
        if git(owner, "ls-files", "--", str(p)):
            raise ValueError("不得清理已跟踪文件")
        git(owner, "check-ignore", "-q", "--", str(p))
        repo = retained_commit(proof["repo"], proof["commit"], proof["retain_ref"])
        rel = proof["git_path"]
        if (not isinstance(rel, str) or not rel or Path(rel).is_absolute()
                or any(x in ("", ".", "..") for x in rel.split("/")) or "\x00" in rel):
            raise ValueError("无效的 Git 相对路径")
        # literal pathspec，不能把带通配符的路径解释成一批文件。
        entry = git(repo, "ls-tree", proof["commit"], "--", ":(literal)" + rel)
        header, found = entry.split("\t", 1)
        mode, typ, blob = header.split()
        # -z 避免中文路径被 core.quotepath 转义。
        entry0 = git(repo, "-c", "core.quotepath=false", "ls-tree", "-z", proof["commit"], "--", ":(literal)" + rel)
        if mode not in ("100644", "100755") or typ != "blob" or entry0.split("\t", 1)[1].rstrip("\0") != rel:
            raise ValueError("保留对象不是同一路径的普通文件")
        digest = sha256_file(p)
        if proof.get("sha256") != digest:
            raise ValueError("复制件内容指纹变化")
        if git(owner, "hash-object", "--no-filters", str(p)) != blob:
            raise ValueError("复制件与保留提交的 blob 不同")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
        restored = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", blob],
                                  capture_output=True, timeout=60, env=env, check=True).stdout
        if hashlib.sha256(restored).hexdigest() != digest:
            raise ValueError("保留对象恢复校验失败")
        task = proof["task"]
        root = Path(task["root"])
        if (not root.is_absolute() or root.resolve() != root or root == owner
                or owner not in root.parents or root not in p.parents):
            raise ValueError("必须指定文件所属的具体子任务目录")
        if task.get("role") not in ("test_copy", "release_candidate"):
            raise ValueError("仅允许验证副本或发布候选复制件")
        retained_commit(task["repo"], task["commit"], task["retain_ref"])
        receipt = ordinary_path(task["receipt"])
        if receipt == p or owner not in receipt.parents:
            raise ValueError("任务回执须留在所属仓库且不能是待删文件")
        receipt_before = fingerprint(receipt)
        if sha256_file(receipt) != task.get("receipt_sha256"):
            raise ValueError("任务回执指纹变化")
        data = json.loads(receipt.read_text())
        pointer = task["commit_pointer"]
        if not isinstance(pointer, list) or not pointer or not all(isinstance(x, str) and x for x in pointer):
            raise ValueError("必须指定回执中的提交字段")
        for key in pointer:
            data = data[key]
        if data != task["commit"]:
            raise ValueError("完成回执没有绑定该合入提交")
        if fingerprint(p) != before or fingerprint(receipt) != receipt_before:
            raise ValueError("检查期间文件或回执变化")
        return True, "旧子任务提交已合入，复制件可从保留提交逐字节恢复"
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def dependencies(item):
    if not isinstance(item, dict):
        return []
    proof = item.get("checkpoint") or {}
    if not isinstance(proof, dict):
        return []
    if proof.get("kind") == "retired_test_copy":
        import retired_test_copy
        return retired_test_copy.dependencies(item)
    if proof.get("kind") == "retired_build_copy":
        import retired_worktree
        return retired_worktree.dependencies(item)
    if proof.get("kind") == "registered_stage_copy":
        import registered_stage_checkpoint
        return registered_stage_checkpoint.dependencies(item)
    if proof.get("kind") == "completed_output_stage":
        import completed_output_checkpoint
        return completed_output_checkpoint.dependencies(item)
    task = proof.get("task") or {}
    if not isinstance(task, dict):
        task = {}
    paths = [proof.get("repo"), task.get("repo"), task.get("receipt")]
    return [str(Path(p).resolve()) for p in paths if isinstance(p, str) and p]
