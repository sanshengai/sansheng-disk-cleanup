"""Verify a closed registration phase's duplicate payloads; never delete anything.

This checkpoint does not close the parent project. Age, references, processes,
protected paths and execution authorization remain mandatory caller checks.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from datetime import datetime

KINDS = ("source", "gold", "evaluation", "approval")
SNAPSHOT = "个人网站/web/career-api/career_api/data/short-answer/manifest.json"
PHASE = "candidate-accumulated-not-derived-not-published"


def _path(value, directory=False):
    if not isinstance(value, str) or not value:
        raise ValueError("缺少绝对路径")
    p = Path(value)
    if not p.is_absolute() or p.resolve() != p:
        raise ValueError("不接受相对路径或符号链接")
    s = p.lstat()
    if directory:
        if not stat.S_ISDIR(s.st_mode):
            raise ValueError("要求普通目录")
    elif not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise ValueError("要求无硬链接的普通文件")
    return p


def _identity(p):
    s = p.lstat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _bytes(p, identities):
    p = _path(str(p))
    before = _identity(p)
    data = p.read_bytes()
    if _identity(p) != before:
        raise ValueError("检查期间文件变化")
    identities[p] = before
    return data


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("必须绑定完整 SHA256")
    return value


def _checked(p, expected, identities):
    data = _bytes(p, identities)
    if hashlib.sha256(data).hexdigest() != _digest(expected):
        raise ValueError("文件 SHA256 已变化: " + str(p))
    return data


def _git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          env=env, check=True, timeout=60).stdout


def _inventory(manifest):
    items = manifest["items"]
    if not isinstance(items, list) or not items:
        raise ValueError("manifest 必须包含非空条目")
    result, uids = {}, set()
    for entry in items:
        uid = entry["item_uid"]
        if not isinstance(uid, str) or not uid or uid in uids:
            raise ValueError("重复或空 UID")
        uids.add(uid)
        parents = set()
        for kind in KINDS:
            rel = entry[kind + "_relpath"]
            if (not isinstance(rel, str) or len(rel.split("/")) != 2
                    or any(x in ("", ".", "..") for x in rel.split("/"))
                    or Path(rel).is_absolute() or "\x00" in rel
                    or not (Path(rel).name == kind + ".json"
                            or Path(rel).name.endswith("-" + kind + ".json")) or rel in result):
                raise ValueError("无效或重复的四类文件路径")
            parents.add(str(Path(rel).parent))
            result[rel] = _digest(entry[kind + "_sha256"])
        if len(parents) != 1:
            raise ValueError("同一 UID 的四类文件必须同目录")
    return result


def _proof_paths(proof):
    retained = proof.get("retained_proofs")
    retained = retained if isinstance(retained, list) else []
    return [proof.get(k) for k in ("owner_repo", "formal_root", "stage_manifest",
                                  "formal_manifest", "receipt", "adoption")] + [
        p.get("path") for p in retained if isinstance(p, dict)]


def dependencies(item):
    """Protect formal data, all admission evidence and the retained stage manifest."""
    if not isinstance(item, dict):
        return []
    proof = item.get("checkpoint") or {}
    if not isinstance(proof, dict):
        return []
    paths = set()
    for value in _proof_paths(proof):
        if isinstance(value, str) and value:
            try:
                paths.add(str(Path(value).resolve()))
            except (OSError, ValueError, RuntimeError):
                pass
    # The caller also compares scope=files selections by exact path. A formal
    # root directory alone would not protect its individual payload files.
    # Derive those paths from this group's hash-bound manifest, never files[]
    # (which describes the disposable copies), and never protect owner subtrees.
    try:
        stage = _path(item["path"], True)
        formal = _path(proof["formal_root"], True)
        manifest = stage / "manifest.json"
        if (stage.name != "stage" or stage.parent.name != "registration"
                or proof["stage_manifest"] != str(manifest)
                or formal == stage or stage in formal.parents):
            raise ValueError("依赖路径未绑定登记 stage 与独立正式库")
        document = json.loads(_checked(manifest, proof["stage_manifest_sha256"], {}))
        paths.update(str((formal / rel).resolve()) for rel in _inventory(document))
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RuntimeError):
        # A malformed checkpoint is rejected by verify; dependency enumeration
        # stays total so it cannot turn the caller's rejection into an exception.
        pass
    return sorted(paths)


def verify(item):
    try:
        _verify(item)
        return True, "登记阶段已完成；所选过程副本与正式四类文件逐 SHA256 一致"
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError,
            subprocess.SubprocessError) as exc:
        return False, str(exc)


def _verify(item):
    proof = item["checkpoint"]
    if (proof.get("kind") != "registered_stage_copy" or item.get("method") != "delete"
            or item.get("scope") != "files"):
        raise ValueError("只接受登记 stage 的显式文件删除清单")
    stage = _path(item["path"], True)
    owner = _path(proof["owner_repo"], True)
    formal = _path(proof["formal_root"], True)
    if stage.name != "stage" or stage.parent.name != "registration":
        raise ValueError("只接受精确 registration/stage")
    if (owner not in stage.parents or owner not in formal.parents or formal == stage
            or formal in stage.parents or stage in formal.parents
            or _git(stage, "rev-parse", "--show-toplevel").decode().strip() != str(owner)
            or _git(formal, "rev-parse", "--show-toplevel").decode().strip() != str(owner)):
        raise ValueError("所属仓库或正式库边界不符")
    for flag in ("--git-dir", "--git-common-dir"):
        admin = Path(_git(owner, "rev-parse", "--path-format=absolute", flag).decode().strip()).resolve()
        if stage == admin or admin in stage.parents:
            raise ValueError("不得处理 Git 管理目录")
    if _git(owner, "ls-files", "-z", "--", ":(literal)" + str(stage)):
        raise ValueError("stage 含已跟踪内容")
    _git(owner, "check-ignore", "-q", "--", str(stage))
    expected_paths = {"stage_manifest": stage / "manifest.json",
                      "formal_manifest": formal / "manifest.json",
                      "receipt": stage.parent / "receipt.json"}
    for key, path in expected_paths.items():
        if proof[key] != str(path):
            raise ValueError("证据路径未绑定本组: " + key)
    identities = {}
    docs = {key: json.loads(_checked(_path(proof[key]), proof[key + "_sha256"], identities))
            for key in (*expected_paths, "adoption")}
    adoption_path = _path(proof["adoption"])
    if owner not in adoption_path.parents or stage in adoption_path.parents:
        raise ValueError("adoption 必须保留在所属仓库内、删除目录外")
    old = _inventory(docs["stage_manifest"])
    current = _inventory(docs["formal_manifest"])
    if any(current.get(rel) != sha for rel, sha in old.items()):
        raise ValueError("正式 manifest 不再包含相同四类文件")
    receipt, adoption = docs["receipt"], docs["adoption"]
    count = len(docs["stage_manifest"]["items"])
    if (receipt.get("status") != "PASS" or receipt["after"]["manifest"] != proof["stage_manifest_sha256"]
            or any(type(receipt.get(k)) is not int or receipt[k] != count for k in
                   ("registeredCurrent", "stagedReady", "formalReadinessPASS", "untrustedRejected"))
            or type(receipt.get("registeredBefore")) is not int or type(receipt.get("added")) is not int
            or receipt["registeredBefore"] < 0 or receipt["added"] < 0
            or receipt["registeredBefore"] + receipt["added"] != count):
        raise ValueError("registration PASS/计数/manifest 证明不一致")
    if (adoption.get("state") != "succeeded" or adoption.get("phase") != PHASE
            or type(adoption.get("registeredCurrent")) is not int
            or adoption.get("registeredCurrent") != count):
        raise ValueError("缺少已完成的登记后源快照阶段")
    finished = datetime.fromisoformat(adoption["finishedAt"].replace("Z", "+00:00"))
    if finished.tzinfo is None:
        raise ValueError("阶段完成时间须带时区")
    candidate = adoption["candidate"]
    if not isinstance(candidate, str) or not re.fullmatch(r"[0-9a-f]{40}", candidate):
        raise ValueError("adoption 缺少候选提交")
    snapshot = _git(owner, "show", candidate + ":" + SNAPSHOT)
    if hashlib.sha256(snapshot).hexdigest() != proof["stage_manifest_sha256"]:
        raise ValueError("adoption 候选未绑定本组 manifest")
    retained = [{"path": receipt["reviewPath"], "sha256": receipt["reviewSHA256"]}] + [
        {"path": c["path"], "sha256": c["sha256"]} for c in receipt["consumers"]]
    if len(retained) < 3 or sorted(retained, key=lambda x: x["path"]) != sorted(
            proof["retained_proofs"], key=lambda x: x["path"]):
        raise ValueError("必须完整保留 admission 和消费者回执")
    for entry in retained:
        p = _path(entry["path"])
        if stage.parent.parent not in p.parents or stage in p.parents:
            raise ValueError("admission/consumer 回执不属于本组或位于删除树内")
        _checked(p, entry["sha256"], identities)
    files = item.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("files 不能为空")
    selected = [f["path"] if isinstance(f, dict) else f for f in files]
    if not all(isinstance(p, str) for p in selected) or len(set(selected)) != len(selected):
        raise ValueError("files 必须是互异文件路径")
    allowed = {str(stage / rel) for rel in old}
    if not set(selected) <= allowed:
        raise ValueError("files 含 manifest、越界或未登记文件")
    # Complete inventory catches new files, links and missing copy members even
    # when only a subset is selected. Manifests are always retained.
    found = set()
    dirs = {str(Path(rel).parent) for rel in old}
    for base, subdirs, names in os.walk(stage, followlinks=False):
        for name in subdirs:
            p = _path(str(Path(base) / name), True)
            if p.relative_to(stage).as_posix() not in dirs:
                raise ValueError("stage 新增或未知目录")
        for name in names:
            p = _path(str(Path(base) / name))
            found.add(p.relative_to(stage).as_posix())
    if found != set(old) | {"manifest.json"}:
        raise ValueError("stage 有缺失或额外文件")
    for rel, digest in old.items():
        _checked(stage / rel, digest, identities)
        _checked(formal / rel, digest, identities)
    if any(_identity(p) != before for p, before in identities.items()):
        raise ValueError("检查期间证据或副本变化")
