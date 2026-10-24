#!/usr/bin/env python3
"""Read-only proof for files in an interrupted, pre-boot build checkout.

This is NOT a worktree remover. It never unlocks, writes Git metadata, creates
refs, copies files or deletes anything. Export ``matched_files`` to the normal
sealed scope=files workflow; keep .git, the registration/locks and all exceptions.
The seal binds evidence, not permission to bypass protect.py or act.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys

SCHEMA = "retired-build-worktree-proof/1"
BUILD_NAME = re.compile(r"sansheng-site-build-(?:stable-v1(?:-slot[23])?|[0-9a-f]{32})\Z")


class Refused(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise Refused(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def git(repo, *args, input_bytes=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
    result = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
                            env=env, input=input_bytes, capture_output=True)
    require(result.returncode == 0, "git_read_failed: " + " ".join(args[:2]))
    return result.stdout


def no_links(path):
    path = Path(path)
    require(path.is_absolute(), "absolute_path_required")
    require(".." not in path.parts, "parent_path_component")
    for part in reversed([path, *path.parents]):
        require(not part.is_symlink(), "linked_path: " + str(part))
    return path


def fingerprint(st):
    return {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
            "mode": st.st_mode, "uid": st.st_uid, "nlink": st.st_nlink}


def file_proof(path):
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode), "non_regular_file: " + str(path))
    require(before.st_nlink == 1, "hardlinked_file: " + str(path))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    sha = hashlib.sha256()
    blob = hashlib.sha1(b"blob " + str(before.st_size).encode() + b"\0")
    try:
        require(fingerprint(os.fstat(fd)) == fingerprint(before), "changed_before_read")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while block := stream.read(1024 * 1024):
                sha.update(block)
                blob.update(block)
        require(fingerprint(os.fstat(fd)) == fingerprint(before), "changed_during_read")
    finally:
        os.close(fd)
    require(fingerprint(path.lstat()) == fingerprint(before), "changed_after_read")
    return {"fingerprint": fingerprint(before), "sha256": sha.hexdigest(),
            "blob": blob.hexdigest(),
            "git_mode": "100755" if before.st_mode & stat.S_IXUSR else "100644"}


class Host:
    """Real probes are read-only. Tests substitute this boundary, never CLI flags."""
    def __init__(self, home=None):
        # Match protect.py: production boundaries come from passwd, not HOME.
        self.home = Path(pwd.getpwuid(os.getuid()).pw_dir) if home is None else Path(home)

    def boot(self):
        require(sys.platform == "darwin", "macos_required")
        result = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"],
                                capture_output=True, text=True)
        match = re.search(r"\bsec\s*=\s*(\d+)", result.stdout)
        require(result.returncode == 0 and match is not None, "boot_unknown")
        return {"platform": sys.platform, "boot_seconds": int(match[1]),
                "host": os.uname().nodename}

    def unopened(self, root):
        result = subprocess.run(["/usr/sbin/lsof", "-nP", "+D", str(root), "-F", "pcn"],
                                capture_output=True, text=True)
        require(result.returncode == 1 and not result.stdout.strip() and
                not result.stderr.strip(), "active_or_unknown_open_files")


def registration(repo, root):
    records = git(repo, "worktree", "list", "--porcelain").decode().strip().split("\n\n")
    found = []
    for record in records:
        lines = record.splitlines()
        if lines and lines[0] == "worktree " + str(root):
            found.append(lines)
    require(len(found) == 1, "registration_missing_or_ambiguous")
    lines = found[0]
    require("detached" in lines and "locked initializing" in lines,
            "not_detached_initializing_checkout")
    heads = [line[5:] for line in lines if line.startswith("HEAD ")]
    require(len(heads) == 1 and re.fullmatch(r"[0-9a-f]{40}", heads[0]), "head_unknown")
    return heads[0]


def validate_metadata(repo, root, recovery_ref, host):
    repo, root = no_links(repo), no_links(root)
    managed = no_links(host.home / ".sansheng/build-tmp")
    require(root.parent == managed and BUILD_NAME.fullmatch(root.name), "outside_build_namespace")
    require(root.is_dir() and root.stat().st_uid == os.getuid(), "root_missing_or_wrong_owner")
    require(repo.is_dir(), "repository_missing")
    head = registration(repo, root)
    require(recovery_ref.startswith("refs/cleanup-retired-worktree/"), "recovery_ref_namespace")
    git(repo, "check-ref-format", recovery_ref)
    require(git(repo, "rev-parse", "--verify", recovery_ref).decode().strip() == head,
            "recovery_ref_wrong_target")
    gitdir = no_links(Path(git(root, "rev-parse", "--absolute-git-dir").decode().strip()))
    common = Path(git(repo, "rev-parse", "--git-common-dir").decode().strip())
    if not common.is_absolute():
        common = repo / common
    common = no_links(common)
    require(gitdir.parent == common / "worktrees", "foreign_worktree_registration")
    require(not os.path.lexists(gitdir / "index"), "index_exists")
    lock, index_lock = gitdir / "locked", gitdir / "index.lock"
    lock_proof, index_proof = file_proof(lock), file_proof(index_lock)
    require(lock.read_bytes() in (b"initializing", b"initializing\n"), "lock_reason_changed")
    require(index_proof["fingerprint"]["size"] == 0, "index_lock_not_empty")
    boot = host.boot()
    require(boot["boot_seconds"] > 0, "boot_unknown")
    require(lock.stat().st_mtime < boot["boot_seconds"] and
            index_lock.stat().st_mtime < boot["boot_seconds"], "lock_not_before_boot")
    if hasattr(lock.stat(), "st_birthtime"):
        require(lock.stat().st_birthtime < boot["boot_seconds"], "lock_created_this_boot")
    # Existing metadata, even released/old metadata, means this is not the
    # legacy absent-lease case. Do not interpret unknown lease formats as idle.
    lease_root = no_links(managed / ".sansheng-build-leases")
    lease_key = hashlib.sha256(str(root).upper().encode()).hexdigest()
    require(not os.path.lexists(lease_root / (lease_key + ".json")), "existing_or_unknown_lease")
    if lease_root.exists():
        require(not any(lease_root.glob(lease_key + ".json.pending-*")), "pending_lease")
    host.unopened(root)
    host.unopened(gitdir)
    return {"head": head, "gitdir": str(gitdir), "boot": boot,
            "root_git": file_proof(root / ".git"), "locked": lock_proof,
            "index_lock": index_proof, "lease_state": "absent"}


def source_tree(repo, head):
    result = {}
    for entry in git(repo, "ls-tree", "-r", "-z", head).split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        result[os.fsdecode(path)] = (mode, kind, blob)
    require(result, "empty_source_tree")
    return result


def make_plan(repo, root, recovery_ref, authorization, host=None):
    host = host or Host()
    require(isinstance(authorization, str) and authorization.strip(), "authorization_required")
    repo, root = Path(repo), Path(root)
    before = validate_metadata(repo, root, recovery_ref, host)
    sources = source_tree(repo, before["head"])
    matched, keep, directories = [], [], []
    def traversal_error(error):
        raise Refused("incomplete_file_inventory: " + str(error))
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=traversal_error):
        dirs.sort()
        files.sort()
        parent = Path(directory)
        directories.append({"path": str(parent.relative_to(root)),
                            "fingerprint": fingerprint(parent.lstat())})
        for name in dirs:
            child = parent / name
            require(name != ".git" and not child.is_symlink(), "nested_git_or_linked_directory")
        for name in files:
            path = parent / name
            relative = str(path.relative_to(root))
            if relative == ".git":
                continue
            require(name != ".git", "nested_git_file")
            proof = file_proof(path)
            item = {"path": str(path), "relative": relative, **proof}
            source = sources.get(relative)
            if source and source == (proof["git_mode"], "blob", proof["blob"]):
                item["source_ref"] = recovery_ref
                matched.append(item)
            else:
                item["reason"] = "not_exact_blob_and_mode" if source else "extra_file"
                keep.append(item)
    require(matched, "empty_matched_file_list")
    # Changes to empty directories and root metadata are included in the seal.
    for directory in directories:
        require(fingerprint((root / directory["path"]).lstat()) == directory["fingerprint"],
                "directory_changed_during_scan")
    after = validate_metadata(repo, root, recovery_ref, host)
    require(before == after, "metadata_changed_during_scan")
    return {"schema": SCHEMA, "read_only": True, "scope": "files", "repo": str(repo),
            "root": str(root), "authorization": authorization, "recovery_ref": recovery_ref,
            "metadata": before, "directories": directories, "matched_files": matched,
            "keep_files": keep, "preserve": [str(root / ".git"), before["gitdir"]],
            "matched_logical_bytes": sum(x["fingerprint"]["size"] for x in matched),
            "keep_logical_bytes": sum(x["fingerprint"]["size"] for x in keep)}


def check_plan(plan, seal, host=None):
    require(isinstance(plan, dict), "invalid_proof_document")
    require(plan.get("schema") == SCHEMA and plan.get("read_only") is True,
            "wrong_proof_schema")
    require(isinstance(plan.get("matched_files"), list) and plan["matched_files"],
            "empty_matched_file_list")
    require(digest(plan) == seal, "proof_seal_mismatch")
    current = make_plan(plan["repo"], plan["root"], plan["recovery_ref"],
                        plan["authorization"], host=host)
    require(current == plan, "proof_drift")
    return current


def load_proof(checkpoint):
    """Read a sealed envelope without following links or accepting partial reads."""
    require(isinstance(checkpoint, dict) and checkpoint.get("kind") == "retired_build_copy",
            "wrong_checkpoint_kind")
    require(isinstance(checkpoint.get("proof"), str), "proof_path_required")
    path = no_links(checkpoint["proof"])
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "invalid_proof_file")
    require(0 < before.st_size <= 128 * 1024 * 1024, "proof_size_invalid")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        require(fingerprint(os.fstat(fd)) == fingerprint(before), "proof_changed_before_read")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(128 * 1024 * 1024 + 1)
        require(len(raw) == before.st_size and
                fingerprint(os.fstat(fd)) == fingerprint(before), "proof_changed_during_read")
    finally:
        os.close(fd)
    require(fingerprint(path.lstat()) == fingerprint(before), "proof_changed_after_read")
    envelope = json.loads(raw)
    require(isinstance(envelope, dict), "invalid_proof_envelope")
    plan = envelope.get("plan")
    require(isinstance(plan, dict) and plan.get("schema") == SCHEMA and
            plan.get("read_only") is True and plan.get("scope") == "files", "wrong_proof_schema")
    require(isinstance(checkpoint.get("seal"), str) and
            envelope.get("seal") == checkpoint["seal"] == digest(plan), "proof_seal_mismatch")
    require(isinstance(plan.get("authorization"), str) and plan["authorization"].strip(),
            "authorization_required")
    require(isinstance(plan.get("matched_files"), list) and plan["matched_files"],
            "empty_matched_file_list")
    return plan, path, fingerprint(before)


def verify_item(item, host=None):
    """Prove selected redundant leaves; never require prior batches to still exist.

    This gate grants no tree removal and makes no writes. The normal actor must
    still enforce its protections and recheck each leaf immediately before use.
    """
    try:
        require(isinstance(item, dict) and item.get("method") == "delete" and
                item.get("scope") == "files", "delete_files_only")
        plan, proof_path, proof_identity = load_proof(item.get("checkpoint"))
        require(item.get("path") == plan.get("root"), "root_mismatch")
        root, repo = no_links(plan["root"]), no_links(plan["repo"])
        require(str(root) == plan["root"] and str(repo) == plan["repo"], "noncanonical_root_or_repo")
        host = host or Host()
        before = validate_metadata(repo, root, plan["recovery_ref"], host)
        require(before == plan["metadata"], "metadata_drift")
        matched = {entry["path"]: entry for entry in plan["matched_files"]}
        require(len(matched) == len(plan["matched_files"]), "duplicate_proof_path")
        keep = {entry["path"] for entry in plan["keep_files"]}
        files = item.get("files")
        require(isinstance(files, list) and files, "empty_selected_files")
        sources = source_tree(repo, plan["recovery_ref"])
        selected, objects = {}, {}
        for entry in files:
            path_text = entry.get("path") if isinstance(entry, dict) else entry
            require(isinstance(path_text, str) and path_text not in selected,
                    "invalid_or_duplicate_selected_path")
            require(path_text in matched and path_text not in keep, "selected_not_matched")
            saved = matched[path_text]
            relative = Path(saved["relative"])
            require(not relative.is_absolute() and ".." not in relative.parts and
                    ".git" not in relative.parts and str(relative) == saved["relative"] and
                    str(root / relative) == path_text, "selected_path_outside_proof_root")
            path = no_links(path_text)
            current = file_proof(path)
            require(current == {key: saved[key] for key in current}, "selected_file_drift")
            require(saved["source_ref"] == plan["recovery_ref"] and
                    sources.get(str(relative)) == (current["git_mode"], "blob", current["blob"]),
                    "selected_source_blob_or_mode_mismatch")
            selected[path_text] = current["fingerprint"]
            objects[current["blob"]] = current["fingerprint"]["size"]
        # ls-tree alone does not prove the addressed objects still exist locally.
        oids = sorted(objects)
        lines = git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                    input_bytes=("\n".join(oids) + "\n").encode()).decode().splitlines()
        require(lines == [f"{oid} blob {objects[oid]}" for oid in oids],
                "source_blob_missing_or_wrong_size")
        after = validate_metadata(repo, root, plan["recovery_ref"], host)
        require(before == after, "metadata_changed_during_verification")
        for path_text, identity in selected.items():
            require(fingerprint(no_links(path_text).lstat()) == identity, "selected_file_drift")
        require(fingerprint(no_links(proof_path).lstat()) == proof_identity, "proof_file_drift")
        return True, "retired_build_copy_exact_selected_files"
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return False, str(error)


def dependencies(item):
    """Keep the proof and its recovery repository out of the deletion set."""
    checkpoint = item.get("checkpoint") if isinstance(item, dict) else None
    paths = []
    if isinstance(checkpoint, dict) and isinstance(checkpoint.get("proof"), str):
        paths.append(checkpoint["proof"])
    try:
        plan, _, _ = load_proof(checkpoint)
        require(isinstance(plan.get("repo"), str), "repository_missing")
        paths.append(str(no_links(plan["repo"])))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass  # verify_item rejects this case; dependencies must be total on bad input.
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    make = commands.add_parser("plan", help="print read-only proof and its seal as JSON")
    for option in ("repo", "root", "recovery-ref", "authorization"):
        make.add_argument("--" + option, required=True)
    check = commands.add_parser("check", help="rehash and verify a saved plan envelope")
    check.add_argument("--plan", required=True)
    check.add_argument("--seal", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            plan = make_plan(args.repo, args.root, args.recovery_ref, args.authorization)
            print(json.dumps({"seal": digest(plan), "plan": plan}, ensure_ascii=False))
        else:
            envelope = json.loads(Path(args.plan).read_text())
            require(isinstance(envelope, dict), "invalid_proof_envelope")
            require(envelope.get("seal") == args.seal, "envelope_seal_mismatch")
            plan = check_plan(envelope["plan"], args.seal)
            print(json.dumps({"status": "verified_read_only", "seal": args.seal,
                              "matched_files": len(plan["matched_files"]),
                              "keep_files": len(plan["keep_files"]),
                              "matched_logical_bytes": plan["matched_logical_bytes"]}))
    except (Refused, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "refused", "reason": str(error)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
