import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import retired_worktree as rw


class FakeHost(rw.Host):
    def __init__(self, home):
        super().__init__(home)
        self.boot_seconds = int(time.time()) + 120
        self.active = False

    def boot(self):
        return {"platform": "fixture", "boot_seconds": self.boot_seconds, "host": "fixture"}

    def unopened(self, root):
        rw.require(not self.active, "active_or_unknown_open_files")


def g(repo, *args):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
               GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    result = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout.decode().strip()


@pytest.fixture
def fixture(tmp_path):
    # pytest's temporary root is outside all enabled Skill scanning trees.
    repo = tmp_path / "source"
    repo.mkdir()
    g(repo, "init")
    (repo / "input.txt").write_bytes(b"original input\n")
    (repo / "converted.json").write_bytes(b'{\n  "fixture": true\n}\n')
    (repo / "partial.mp3").write_bytes(b"fixture audio")
    g(repo, "add", "input.txt", "converted.json", "partial.mp3")
    g(repo, "-c", "commit.gpgsign=false", "commit", "-m", "fixture source")
    home = tmp_path / "home"
    root = home / ".sansheng/build-tmp/sansheng-site-build-stable-v1"
    root.parent.mkdir(parents=True)
    g(repo, "worktree", "add", "--detach", str(root), "HEAD")
    gitdir = Path(g(root, "rev-parse", "--absolute-git-dir"))
    g(repo, "worktree", "lock", "--reason", "initializing", str(root))
    (gitdir / "index").unlink()
    (gitdir / "index.lock").touch()
    (root / "converted.json").write_bytes(b'{\r\n  "fixture": true\r\n}\r\n')
    (root / "partial.mp3").write_bytes(b"")
    ref = "refs/cleanup-retired-worktree/fixture"
    g(repo, "update-ref", ref, "HEAD")
    return {"repo": repo, "root": root, "gitdir": gitdir,
            "ref": ref, "host": FakeHost(home)}


def plan(f):
    return rw.make_plan(f["repo"], f["root"], f["ref"],
                        "User authorized review of retired task files", f["host"])


def test_exact_only_and_preserved_registration(fixture):
    f = fixture
    before = {p.name: p.read_bytes() for p in f["gitdir"].iterdir() if p.is_file()}
    p = plan(f)
    assert [x["relative"] for x in p["matched_files"]] == ["input.txt"]
    assert {x["relative"] for x in p["keep_files"]} == {"converted.json", "partial.mp3"}
    assert p["matched_logical_bytes"] == len(b"original input\n")
    assert rw.check_plan(p, rw.digest(p), f["host"]) == p
    after = {x.name: x.read_bytes() for x in f["gitdir"].iterdir() if x.is_file()}
    assert before == after
    assert not (f["gitdir"] / "index").exists()
    assert (f["root"] / ".git").exists()
    assert (f["root"] / "input.txt").exists()


def test_active_and_unknown_lsof_rejected(fixture, monkeypatch):
    fixture["host"].active = True
    with pytest.raises(rw.Refused, match="active"):
        plan(fixture)
    class Result:
        returncode = 1
        stdout = ""
        stderr = "permission denied"
    monkeypatch.setattr(rw.subprocess, "run", lambda *a, **k: Result())
    with pytest.raises(rw.Refused, match="unknown"):
        rw.Host().unopened(fixture["root"])


@pytest.mark.parametrize("change", ["content", "same_content_replaced", "new_file", "new_empty_dir"])
def test_drift_rejected(fixture, change):
    f = fixture
    p = plan(f)
    if change == "content":
        (f["root"] / "input.txt").write_bytes(b"changed")
    elif change == "same_content_replaced":
        file = f["root"] / "input.txt"
        file.unlink()
        file.write_bytes(b"original input\n")
    elif change == "new_file":
        (f["root"] / "new.txt").write_bytes(b"unique")
    else:
        (f["root"] / "new_empty").mkdir()
    with pytest.raises(rw.Refused, match="drift|empty_matched"):
        rw.check_plan(p, rw.digest(p), f["host"])


@pytest.mark.parametrize("change", ["wrong_name", "outside", "linked_root", "linked_file", "linked_dir"])
def test_path_and_link_rejections(fixture, change):
    f = fixture
    if change == "wrong_name":
        f["root"] = f["root"].parent / "normal-product-tree"
    elif change == "outside":
        f["root"] = f["repo"]
    elif change == "linked_root":
        link = f["root"].parent / "sansheng-site-build-stable-v1-slot2"
        link.symlink_to(f["root"], target_is_directory=True)
        f["root"] = link
    elif change == "linked_file":
        (f["root"] / "link").symlink_to(f["repo"] / "input.txt")
    else:
        (f["root"] / "linkdir").symlink_to(f["repo"], target_is_directory=True)
    with pytest.raises(rw.Refused):
        plan(f)


def test_empty_list_empty_source_and_bad_seal(fixture):
    p = plan(fixture)
    empty = copy.deepcopy(p)
    empty["matched_files"] = []
    with pytest.raises(rw.Refused, match="empty_matched"):
        rw.check_plan(empty, rw.digest(empty), fixture["host"])
    with pytest.raises(rw.Refused, match="seal"):
        rw.check_plan(p, "wrong", fixture["host"])
    (fixture["root"] / "input.txt").unlink()
    with pytest.raises(rw.Refused, match="empty_matched"):
        plan(fixture)


@pytest.mark.parametrize("change", ["missing", "wrong_target", "wrong_namespace"])
def test_recovery_ref_rejected(fixture, change):
    f = fixture
    if change == "missing":
        g(f["repo"], "update-ref", "-d", f["ref"])
    elif change == "wrong_target":
        (f["repo"] / "another").write_text("fixture")
        g(f["repo"], "add", "another")
        g(f["repo"], "-c", "commit.gpgsign=false", "commit", "-m", "another")
        g(f["repo"], "update-ref", f["ref"], "HEAD")
    else:
        f["ref"] = "refs/heads/main"
    with pytest.raises(rw.Refused):
        plan(f)


@pytest.mark.parametrize("change", ["reason", "missing_lock", "nonempty_index_lock", "index_present", "this_boot"])
def test_initialization_guard_rejected(fixture, change):
    f = fixture
    if change == "reason":
        (f["gitdir"] / "locked").write_text("user keeps this worktree\n")
    elif change == "missing_lock":
        (f["gitdir"] / "locked").unlink()
    elif change == "nonempty_index_lock":
        (f["gitdir"] / "index.lock").write_bytes(b"writing")
    elif change == "index_present":
        (f["gitdir"] / "index").touch()
    else:
        f["host"].boot_seconds = 1
    with pytest.raises(rw.Refused):
        plan(f)


def test_unknown_lease_and_hardlink_rejected(fixture):
    f = fixture
    leases = f["root"].parent / ".sansheng-build-leases"
    leases.mkdir()
    key = hashlib.sha256(str(f["root"]).upper().encode()).hexdigest()
    lease = leases / (key + ".json")
    lease.write_text("unknown")
    with pytest.raises(rw.Refused, match="lease"):
        plan(f)
    lease.unlink()
    os.link(f["root"] / "input.txt", f["root"] / "hardlink")
    with pytest.raises(rw.Refused, match="hardlinked"):
        plan(f)


def test_mutation_during_read_and_no_authorization(fixture, monkeypatch):
    f = fixture
    real = rw.file_proof
    def mutate(path):
        result = real(path)
        if path.name == "input.txt":
            (path.parent / "new_during_read").write_text("unique")
        return result
    monkeypatch.setattr(rw, "file_proof", mutate)
    with pytest.raises(rw.Refused, match="directory_changed"):
        plan(f)
    with pytest.raises(rw.Refused, match="authorization"):
        rw.make_plan(f["repo"], f["root"], f["ref"], "", f["host"])


def test_no_execute_or_pilot_command():
    for command in ("execute", "pilot", "unlock"):
        with pytest.raises(SystemExit) as exc:
            rw.main([command])
        assert exc.value.code == 2


def checkpoint_item(f, tmp_path):
    p = plan(f)
    proof = tmp_path / "proof.json"
    seal = rw.digest(p)
    proof.write_text(json.dumps({"plan": p, "seal": seal}))
    return {"path": str(f["root"]), "method": "delete", "scope": "files",
            "files": [x["path"] for x in p["matched_files"]],
            "checkpoint": {"kind": "retired_build_copy", "proof": str(proof), "seal": seal}}


def test_item_strings_and_actor_dicts(fixture, tmp_path, monkeypatch):
    item = checkpoint_item(fixture, tmp_path)
    monkeypatch.setattr(rw, "Host", lambda: fixture["host"])
    assert rw.verify_item(item)[0]
    assert set(rw.dependencies(item)) == {item["checkpoint"]["proof"], str(fixture["repo"])}
    item["files"] = [{"path": p, "size": Path(p).stat().st_size,
                      "mtime_ns": Path(p).stat().st_mtime_ns} for p in item["files"]]
    assert rw.verify_item(item)[0]


@pytest.mark.parametrize("change", ["seal", "wrong_ref", "changed", "replaced", "mode",
                                   "unknown", "keep", "git", "empty", "link", "active",
                                   "new_index", "root", "scope", "method", "duplicate",
                                   "lease", "lock", "proof_link"])
def test_item_rejects_unsafe_selection(fixture, tmp_path, change):
    f = fixture
    item = checkpoint_item(f, tmp_path)
    target = Path(item["files"][0])
    if change == "seal":
        proof = Path(item["checkpoint"]["proof"])
        envelope = json.loads(proof.read_text())
        envelope["plan"]["authorization"] += "tampered"
        proof.write_text(json.dumps(envelope))
    elif change == "wrong_ref":
        g(f["repo"], "update-ref", f["ref"], g(f["repo"], "rev-parse", "HEAD:input.txt"))
    elif change == "changed":
        target.write_bytes(b"unique edited input")
    elif change == "replaced":
        content = target.read_bytes()
        target.unlink()
        target.write_bytes(content)
    elif change == "mode":
        target.chmod(0o755)
    elif change in ("unknown", "keep", "git"):
        item["files"] = [str(f["root"] / {"unknown": "new", "keep": "partial.mp3",
                                          "git": ".git"}[change])]
    elif change == "empty":
        item["files"] = []
    elif change == "link":
        target.unlink()
        target.symlink_to(f["repo"] / "input.txt")
    elif change == "active":
        f["host"].active = True
    elif change == "new_index":
        (f["gitdir"] / "index").touch()
    elif change == "root":
        item["path"] = str(f["repo"])
    elif change == "scope":
        item["scope"] = "tree"
    elif change == "method":
        item["method"] = "trash"
    elif change == "duplicate":
        item["files"].append(item["files"][0])
    elif change == "lease":
        leases = f["root"].parent / ".sansheng-build-leases"
        leases.mkdir()
        key = hashlib.sha256(str(f["root"]).upper().encode()).hexdigest()
        (leases / (key + ".json")).write_text("unknown")
    elif change == "lock":
        (f["gitdir"] / "locked").write_text("keep")
    elif change == "proof_link":
        linked = tmp_path / "linked-proof.json"
        linked.symlink_to(item["checkpoint"]["proof"])
        item["checkpoint"]["proof"] = str(linked)
    assert not rw.verify_item(item, f["host"])[0]


def test_remaining_batch_after_first_removed_and_new_files(fixture, tmp_path):
    f = fixture
    (f["root"] / "converted.json").write_bytes((f["repo"] / "converted.json").read_bytes())
    item = checkpoint_item(f, tmp_path)
    assert len(item["files"]) == 2
    first = Path(item["files"].pop(0))
    first.unlink()  # Only a disposable test fixture, never a real target.
    (f["root"] / "new-unselected-input").write_text("preserve new work")
    assert rw.verify_item(item, f["host"])[0]
    item["files"].append(str(f["root"] / "new-unselected-input"))
    assert not rw.verify_item(item, f["host"])[0]


@pytest.mark.parametrize("change", ["blob", "mode", "relative", "source_ref", "empty"])
def test_resealed_invalid_proof_still_rejected(fixture, tmp_path, change):
    f = fixture
    item = checkpoint_item(f, tmp_path)
    path = Path(item["checkpoint"]["proof"])
    envelope = json.loads(path.read_text())
    matched = envelope["plan"]["matched_files"][0]
    if change == "blob":
        matched["blob"] = "0" * 40
    elif change == "mode":
        matched["git_mode"] = "100755"
    elif change == "relative":
        matched["relative"] = "../source/input.txt"
    elif change == "source_ref":
        matched["source_ref"] = "refs/heads/main"
    elif change == "empty":
        envelope["plan"]["matched_files"] = []
    envelope["seal"] = rw.digest(envelope["plan"])
    item["checkpoint"]["seal"] = envelope["seal"]
    path.write_text(json.dumps(envelope))
    assert not rw.verify_item(item, f["host"])[0]


def test_item_checks_source_objects_and_final_identity(fixture, tmp_path, monkeypatch):
    item = checkpoint_item(fixture, tmp_path)
    real_git = rw.git
    def missing_object(repo, *args, **kwargs):
        if args[0] == "cat-file":
            return b"missing\n"
        return real_git(repo, *args, **kwargs)
    monkeypatch.setattr(rw, "git", missing_object)
    ok, reason = rw.verify_item(item, fixture["host"])
    assert not ok and "source_blob_missing" in reason
    monkeypatch.setattr(rw, "git", real_git)
    real_metadata = rw.validate_metadata
    calls = 0
    def mutate_after_read(*args):
        nonlocal calls
        result = real_metadata(*args)
        calls += 1
        if calls == 2:
            Path(item["files"][0]).write_text("changed after selected hashing")
        return result
    monkeypatch.setattr(rw, "validate_metadata", mutate_after_read)
    assert not rw.verify_item(item, fixture["host"])[0]


def test_malformed_item_and_dependencies_are_total():
    for item in (None, {}, {"checkpoint": []}, {"checkpoint": {"proof": "/missing"}}):
        assert not rw.verify_item(item)[0]
        assert isinstance(rw.dependencies(item), list)
