import copy
import hashlib
import json
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import act
import artifact_checkpoint
import plan
import protect
import retired_test_copy as rtc
import run_authorization as auth
from test_plan_act import node


def ref(p):
    return {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}


@pytest.fixture
def case(tmp_path, monkeypatch):
    base = (tmp_path / "system-tmp").resolve(); base.mkdir()
    monkeypatch.setattr(protect, "TMP_BASE", str(base))
    root = base / "owned-one-off-test"; root.mkdir()
    source = root / "fixture"; source.mkdir()
    src = source / "a.json"; src.write_text('{"a":1}\n')
    retained = tmp_path.resolve() / "retained"; retained.mkdir()
    dst = retained / "a.json"; dst.write_bytes(src.read_bytes())
    code = retained / "candidate.ts"; code.write_text("test('source', () => {});\n")
    log = retained / "original-test.log"
    log.write_text(f"\n RUN  v4.1.11 {source}\n\n Test Files  1 passed (1)\n      Tests  46 passed (46)\n")
    run = tmp_path.resolve() / "run"; run.mkdir()
    (run / "facts.json").write_text('{"scan_id":"test"}')
    proof = {"schema": rtc.SCHEMA, "role": "test_copy", "status": "retired",
             "retirement_reason": "User closes this exact completed test stage",
             "task_root": str(root), "retained_evidence": [ref(code), ref(log)],
             "test_receipt": dict(ref(log), format="vitest"),
             "files": [{"path": str(src), "retained_copy": str(dst), "sha256": ref(src)["sha256"]}]}
    pf = run / "retirement-proof.json"; pf.write_text(json.dumps(proof))
    item = {"path": str(source), "method": "delete", "scope": "files", "files": [str(src)],
            "checkpoint": dict(ref(pf), kind=rtc.KIND)}
    item["run_authorization"] = auth.create(run, [item], "Remove exact retired test copies",
        allow_recent=True, retire_completed_outputs=True, audit=False)
    profile = {"roots": [str(retained)], "reference_roots": [str(retained)],
               "deliverable_roots": [str(retained)], "protect": []}
    monkeypatch.setattr(act, "open_files", lambda p: [])
    n = node(source, recent_24h=1, protected=True)
    ctx = {"profile": profile, "nodes": [n], "reviews": [], "matched": set(), "legacy": False,
           "run": str(run), "cleanup_paths": [], "cleanup_files": {str(src)}, "soft": []}
    return dict(root=root, src=src, dst=dst, code=code, log=log, run=run,
                pf=pf, proof=proof, item=item, profile=profile, ctx=ctx)


def repin(c):
    c["pf"].write_text(json.dumps(c["proof"]))
    c["item"]["checkpoint"] = dict(ref(c["pf"]), kind=rtc.KIND)


def test_original_plan_and_act_share_verified_scope(case):
    c = case
    assert protect.protected_reason(str(c["src"]), c["profile"]) == rtc.TEMP_REASON
    assert not protect.under_roots(str(c["src"]), c["profile"])
    errors, sealed = plan.check_item(c["item"], c["ctx"])
    assert not errors, errors
    result = act._execute_item(0, sealed, c["profile"], True, time.time(), str(c["run"]))
    assert result["status"] == "planned", result
    assert c["src"].exists() and c["dst"].exists()
    assert c["dst"].as_posix() in artifact_checkpoint.dependencies(c["item"])


@pytest.mark.parametrize("change", ["missing-retained", "retained-drift", "source-drift", "empty",
    "different-root", "bad-log", "empty-code", "proof-drift", "no-retirement-auth", "user-protect",
    "evidence-selected", "symlink", "temp-root"])
def test_original_consumers_reject(case, change):
    c = case
    if change == "missing-retained": c["dst"].unlink()
    elif change == "retained-drift": c["dst"].write_text("other")
    elif change == "source-drift": c["src"].write_text("other")
    elif change == "empty": c["item"]["files"] = []
    elif change == "different-root":
        c["proof"]["task_root"] = str(c["root"].parent / "another-test")
        (c["root"].parent / "another-test").mkdir(); repin(c)
    elif change == "bad-log":
        c["log"].write_text("Tests 46 passed (46)\n")
        c["proof"]["retained_evidence"][1] = ref(c["log"])
        c["proof"]["test_receipt"] = dict(ref(c["log"]), format="vitest"); repin(c)
    elif change == "empty-code":
        c["proof"]["retained_evidence"] = [ref(c["log"])]; repin(c)
    elif change == "proof-drift": c["pf"].write_text("{}")
    elif change == "no-retirement-auth":
        del c["item"]["run_authorization"]
    elif change == "user-protect": c["profile"]["protect"] = [str(c["root"])]
    elif change == "evidence-selected":
        c["ctx"]["cleanup_files"].add(str(c["log"]))
    elif change == "symlink":
        c["dst"].unlink(); c["dst"].symlink_to(c["src"])
    elif change == "temp-root":
        c["proof"]["task_root"] = str(c["root"].parent); repin(c)
    errors, sealed = plan.check_item(c["item"], c["ctx"])
    assert errors
    if change != "evidence-selected":
        # Cross-card dependency conflicts belong to plan, while every direct
        # input mutation must also fail the actual execution entry point.
        raw = dict(c["item"], realpath=str(Path(c["item"]["path"]).resolve()),
                   ino=Path(c["item"]["path"]).stat().st_ino, dev=Path(c["item"]["path"]).stat().st_dev)
        assert act._execute_item(0, raw, c["profile"], True, time.time(), str(c["run"]))["status"] == "skipped"


def test_other_tmp_and_non_checkpoint_stay_protected(case):
    c = case
    original = copy.deepcopy(c["item"]); del c["item"]["checkpoint"]
    assert plan.check_item(c["item"], c["ctx"])[0]
    scope = rtc.load_scope(original, c["profile"], str(c["run"]))
    other = c["root"].parent / "other"; other.mkdir()
    assert scope.reason(str(other))
    assert protect.protected_reason(str(other), c["profile"]) == rtc.TEMP_REASON


def test_open_and_process_guards_are_not_waived(case, monkeypatch):
    c = case
    c["ctx"]["nodes"][0]["open_by"] = [{"command": "busy"}]
    assert any("正被" in x for x in plan.check_item(c["item"], c["ctx"])[0])
    c["ctx"]["nodes"][0]["open_by"] = []
    errors, sealed = plan.check_item(c["item"], c["ctx"]); assert not errors
    monkeypatch.setattr(act, "open_files", lambda p: [str(c["src"])])
    assert act._execute_item(0, sealed, c["profile"], True, time.time(), str(c["run"]))["status"] == "skipped"
    monkeypatch.setattr(act, "blocking_processes", lambda i: (["producer"], []))
    assert act.execute_item(0, sealed, c["profile"], False, time.time(), str(c["run"]))["status"] == "skipped"


def test_delete_instant_retained_drift_blocks_disposer(case):
    c = case
    scope = rtc.load_scope(c["item"], c["profile"], str(c["run"]))
    st = c["src"].stat(); c["dst"].write_text("changed")
    class NeverDispose:
        def dispose(self, p): raise AssertionError("must not delete")
    result = act.ItemResult(0, c["item"])
    act.delete_files([(str(c["src"]), st)], result, {}, NeverDispose(), leaf_guard=scope.verify_leaf)
    assert result.d["skipped_count"] == 1 and c["src"].exists()


def test_source_and_retained_evidence_not_deletable(case):
    c = case
    c["ctx"]["cleanup_files"].add(str(c["dst"]))
    assert any("保留仓库或任务回执" in e for e in plan.check_item(c["item"], c["ctx"])[0])


def test_original_act_really_deletes_only_selected_leaf(case):
    c = case
    unselected = c["src"].parent / "different-retained-test-input.json"
    unselected.write_text("keep unique input")
    errors, sealed = plan.check_item(c["item"], c["ctx"])
    assert not errors, errors
    result = act.execute_item(0, sealed, c["profile"], False, time.time(),
                              str(c["run"]), act.Disposer(mode="delete"))
    assert result["status"] == "removed", result
    assert result["removed"]["files"] == 1
    assert not c["src"].exists()
    assert all(p.exists() for p in (c["dst"], c["code"], c["log"], c["pf"], unselected))


def test_original_no_refs_scan_requires_actual_targeted_probe(case):
    c = case
    c["ctx"]["nodes"][0]["refs"] = {"checked": False, "reason": "--no-refs"}
    assert plan.check_item(c["item"], c["ctx"])[0]
    c["ctx"]["item_refs"] = {c["item"]["path"]: {"checked": True, "count": 0, "samples": []}}
    errors, _ = plan.check_item(c["item"], c["ctx"])
    assert not errors, errors
