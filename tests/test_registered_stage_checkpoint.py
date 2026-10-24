import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import registered_stage_checkpoint as checkpoint


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, sort_keys=True))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RegisteredStageCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stage-checkpoint-test-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / ".gitignore").write_text(".local/\n")
        self.stage = self.repo / ".local/task-a/registration/stage"
        self.formal = self.repo / ".local/formal"
        entry = {"item_uid": "uid-a"}
        for kind in checkpoint.KINDS:
            rel = "uid-a/" + kind + ".json"
            for root in (self.stage, self.formal):
                dump(root / rel, {"kind": kind, "value": "original"})
            entry[kind + "_relpath"] = rel
            entry[kind + "_sha256"] = digest(self.stage / rel)
        manifest = {"items": [entry]}
        dump(self.stage / "manifest.json", manifest)
        dump(self.formal / "manifest.json", manifest)
        dump(self.repo / checkpoint.SNAPSHOT, manifest)
        self.git("add", ".gitignore", checkpoint.SNAPSHOT)
        self.git("commit", "-qm", "candidate")
        candidate = self.git("rev-parse", "HEAD").strip()
        self.review = self.stage.parent.parent / "ADMISSION.json"
        self.consumers = [self.stage.parent.parent / name for name in ("BACKEND.json", "FRONTEND.json")]
        for p in [self.review] + self.consumers:
            dump(p, {"status": "PASS"})
        self.receipt = self.stage.parent / "receipt.json"
        dump(self.receipt, {"status": "PASS", "registeredBefore": 0, "added": 1,
                           "registeredCurrent": 1, "stagedReady": 1, "formalReadinessPASS": 1,
                           "untrustedRejected": 1, "after": {"manifest": digest(self.stage / "manifest.json")},
                           "reviewPath": str(self.review), "reviewSHA256": digest(self.review),
                           "consumers": [{"path": str(p), "sha256": digest(p)} for p in self.consumers]})
        self.adoption = self.repo / ".local/batch-a/ADOPTION-PROCESS.json"
        dump(self.adoption, {"state": "succeeded", "phase": checkpoint.PHASE,
                            "registeredCurrent": 1, "finishedAt": "2026-01-01T00:00:00+00:00",
                            "candidate": candidate})
        proof = {"kind": "registered_stage_copy", "owner_repo": str(self.repo),
                 "formal_root": str(self.formal), "retained_proofs": [
                     {"path": str(p), "sha256": digest(p)} for p in [self.review] + self.consumers]}
        for k, p in {"stage_manifest": self.stage / "manifest.json", "formal_manifest": self.formal / "manifest.json",
                     "receipt": self.receipt, "adoption": self.adoption}.items():
            proof[k], proof[k + "_sha256"] = str(p), digest(p)
        self.item = {"path": str(self.stage), "scope": "files", "method": "delete",
                     "files": [str(self.stage / ("uid-a/" + k + ".json")) for k in checkpoint.KINDS],
                     "checkpoint": proof}

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True,
                              check=True, text=True).stdout

    def assertDenied(self, item=None):
        ok, reason = checkpoint.verify(item or self.item)
        self.assertFalse(ok, reason)
        self.assertTrue(reason)

    def rewrite_proof(self, key, updates):
        p = Path(self.item["checkpoint"][key])
        data = json.loads(p.read_text()); data.update(updates); dump(p, data)
        self.item["checkpoint"][key + "_sha256"] = digest(p)

    def test_real_git_and_both_caller_shapes(self):
        self.assertTrue(checkpoint.verify(self.item)[0])
        sealed = copy.deepcopy(self.item)
        sealed["files"] = [{"path": p, "size": Path(p).stat().st_size,
                            "mtime_ns": Path(p).stat().st_mtime_ns} for p in sealed["files"]]
        self.assertTrue(checkpoint.verify(sealed)[0])
        sealed["files"] = sealed["files"][:1]
        self.assertTrue(checkpoint.verify(sealed)[0])
        deps = checkpoint.dependencies(self.item)
        for p in (self.formal, self.adoption, self.receipt, self.stage / "manifest.json", self.review):
            self.assertIn(str(p), deps)
        for kind in checkpoint.KINDS:
            self.assertIn(str(self.formal / ("uid-a/" + kind + ".json")), deps)
            self.assertNotIn(str(self.stage / ("uid-a/" + kind + ".json")), deps)
        self.assertNotIn(str(self.stage), deps)

    def test_dependencies_cover_full_group_even_for_selected_subset(self):
        self.item["files"] = self.item["files"][:1]
        deps = checkpoint.dependencies(self.item)
        for kind in checkpoint.KINDS:
            self.assertIn(str(self.formal / ("uid-a/" + kind + ".json")), deps)

    def test_dependencies_invalid_input_is_total_and_verifier_denies(self):
        cases = [None, [], "bad", {}, {"checkpoint": []}, {"checkpoint": "bad"},
                 {"checkpoint": {"retained_proofs": None}},
                 {"checkpoint": {"formal_root": "\x00"}}]
        for item in cases:
            with self.subTest(item=item):
                self.assertIsInstance(checkpoint.dependencies(item), list)
                self.assertFalse(checkpoint.verify(item)[0])

    def test_dependencies_do_not_trust_changed_manifest(self):
        dump(self.stage / "manifest.json", {"items": []})
        deps = checkpoint.dependencies(self.item)
        self.assertNotIn(str(self.formal / "uid-a/gold.json"), deps)
        self.assertDenied()

    def test_unfinished_phase_even_with_fresh_hash(self):
        self.rewrite_proof("adoption", {"state": "running"}); self.assertDenied()

    def test_manifest_accepts_existing_batch_naming(self):
        entry = {"item_uid": "batch-uid"}
        for kind in checkpoint.KINDS:
            entry[kind + "_relpath"] = "batch/uid-" + kind + ".json"
            entry[kind + "_sha256"] = "a" * 64
        self.assertEqual(len(checkpoint._inventory({"items": [entry]})), 4)

    def test_manifest_rejects_path_traversal(self):
        entry = json.loads((self.stage / "manifest.json").read_text())["items"][0]
        entry["gold_relpath"] = "../gold.json"
        with self.assertRaises(ValueError):
            checkpoint._inventory({"items": [entry]})

    def test_wrong_phase(self):
        self.rewrite_proof("adoption", {"phase": "still-registering"}); self.assertDenied()

    def test_formal_payload_changed(self):
        dump(self.formal / "uid-a/gold.json", {"changed": True}); self.assertDenied()

    def test_formal_payload_missing(self):
        (self.formal / "uid-a/gold.json").unlink(); self.assertDenied()

    def test_stage_payload_changed(self):
        dump(self.stage / "uid-a/gold.json", {"changed": True}); self.assertDenied()

    def test_stage_payload_missing(self):
        (self.stage / "uid-a/gold.json").unlink(); self.assertDenied()

    def test_stage_manifest_changed(self):
        dump(self.stage / "manifest.json", {"items": []}); self.assertDenied()

    def test_formal_manifest_changed(self):
        dump(self.formal / "manifest.json", {"items": []}); self.assertDenied()

    def test_matching_files_but_formal_manifest_dropped_old_path(self):
        doc = json.loads((self.formal / "manifest.json").read_text())
        doc["items"][0]["gold_relpath"] = "uid-a/renamed-gold.json"
        self.rewrite_proof("formal_manifest", doc)
        self.assertDenied()

    def test_formal_manifest_can_have_newer_items(self):
        doc = json.loads((self.formal / "manifest.json").read_text())
        newer = copy.deepcopy(doc["items"][0]); newer["item_uid"] = "uid-b"
        for kind in checkpoint.KINDS:
            rel = "uid-b/" + kind + ".json"
            dump(self.formal / rel, {"kind": kind, "newer": True})
            newer[kind + "_relpath"] = rel
            newer[kind + "_sha256"] = digest(self.formal / rel)
        doc["items"].append(newer); self.rewrite_proof("formal_manifest", doc)
        self.assertTrue(checkpoint.verify(self.item)[0])

    def test_receipt_changed(self):
        self.receipt.write_text(self.receipt.read_text() + " "); self.assertDenied()

    def test_false_pass_count(self):
        self.rewrite_proof("receipt", {"formalReadinessPASS": 0}); self.assertDenied()

    def test_empty_inventory_even_with_refreshed_hash(self):
        self.rewrite_proof("stage_manifest", {"items": []}); self.assertDenied()

    def test_empty_files(self):
        self.item["files"] = []; self.assertDenied()

    def test_manifest_in_delete_list(self):
        self.item["files"].append(str(self.stage / "manifest.json")); self.assertDenied()

    def test_outside_delete_list(self):
        self.item["files"].append(str(self.review)); self.assertDenied()

    def test_duplicate_delete_list(self):
        self.item["files"].append(self.item["files"][0]); self.assertDenied()

    def test_symlink_payload(self):
        p = self.stage / "uid-a/gold.json"; p.unlink(); p.symlink_to(self.formal / "uid-a/gold.json")
        self.assertDenied()

    def test_symlink_directory(self):
        (self.stage / "extra").symlink_to(self.formal, target_is_directory=True); self.assertDenied()

    def test_hardlink_payload(self):
        p = self.stage / "uid-a/gold.json"; p.unlink(); os.link(self.formal / "uid-a/gold.json", p)
        self.assertDenied()

    def test_new_extra_file(self):
        dump(self.stage / "extra.json", {}); self.assertDenied()

    def test_new_empty_directory(self):
        (self.stage / "extra").mkdir(); self.assertDenied()

    def test_missing_admission_proof(self):
        self.item["checkpoint"]["retained_proofs"].pop(); self.assertDenied()

    def test_admission_proof_changed(self):
        dump(self.review, {"status": "FAIL"}); self.assertDenied()

    def test_wrong_group_receipt(self):
        p = self.repo / ".local/task-b/registration/receipt.json"; dump(p, json.loads(self.receipt.read_text()))
        self.item["checkpoint"]["receipt"] = str(p); self.assertDenied()

    def test_cross_group_adoption(self):
        dump(self.repo / checkpoint.SNAPSHOT, {"items": [{"different": "group"}]})
        self.git("add", checkpoint.SNAPSHOT); self.git("commit", "-qm", "different group")
        self.rewrite_proof("adoption", {"candidate": self.git("rev-parse", "HEAD").strip()}); self.assertDenied()

    def test_wrong_owner(self):
        self.item["checkpoint"]["owner_repo"] = str(self.repo / ".local"); self.assertDenied()

    def test_formal_inside_delete_tree(self):
        self.item["checkpoint"]["formal_root"] = str(self.stage); self.assertDenied()

    def test_tracked_stage_denied(self):
        self.git("add", "-f", str(self.stage / "uid-a/gold.json")); self.assertDenied()

    def test_unignored_stage_denied(self):
        (self.repo / ".gitignore").write_text(""); self.assertDenied()

    def test_drift_during_validation(self):
        original = checkpoint._checked
        def mutate(p, expected, identities):
            data = original(p, expected, identities)
            if p == self.formal / "uid-a/approval.json":
                self.receipt.write_text(self.receipt.read_text() + " ")
            return data
        with patch.object(checkpoint, "_checked", side_effect=mutate):
            self.assertDenied()

    def test_unsafe_mode(self):
        self.item["scope"] = "all"; self.assertDenied()


if __name__ == "__main__":
    unittest.main()
