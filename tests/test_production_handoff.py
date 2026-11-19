import json
from pathlib import Path

import pytest
from test_quick_act import item, mk, plan, run, world
import batch_space
import triage


def fixture_manifest(world):
    project = world["work"] / "regen" / "pilot-project"
    candidate = project / "drafts" / "unused"
    mk(str(candidate / "part.bin"))
    for name in ("raw.mov", "movie.mp4", "receipt.json"):
        mk(str(project / name))
    manifest = project / "production-handoff.json"
    entries = [{"path": name, "role": role, "reason": "生产者交付记录"}
               for name, role in (("raw.mov", "source"), ("movie.mp4", "delivery"), ("receipt.json", "evidence"))]
    entries += [{"path": "drafts/unused", "role": "intermediate", "reason": "阶段验收已完成",
                 "consumers": [], "evidence": ["receipt.json"]}]
    manifest.write_text(json.dumps({"schema": "production-handoff/1", "entries": entries}))
    return manifest, candidate


def test_cli_default_discovery_keeps_materials_and_measures_only_executed(world, monkeypatch):
    manifest, candidate = fixture_manifest(world)
    monkeypatch.setitem(triage.LOCAL, "production_manifest_globs", ["regen/*/production-handoff.json"])
    source = manifest.parent / "raw.mov"
    result = plan(world, [item(source), item(candidate)])
    assert [r["status"] for r in result["results"]] == ["nothing", "ok"]
    assert "生产清单保留" in result["results"][0]["targets"][0]["skip"]
    rec = run(world, result["seal"])
    assert not candidate.exists() and source.read_bytes()
    assert any("生产清单保留" in str(r.get("why")) for r in rec)
    batches = [json.loads(line) for line in (Path(world["run"]) / "quick-space.jsonl").read_text().splitlines()]
    completed = [b for b in batches if b["status"] == "batch_space"]
    assert len(completed) == 1 and completed[0]["id"] == "unused"
    assert completed[0]["entry_estimate_bytes"] == result["results"][1]["bytes"]
    obs = completed[0]["observations"][0]
    assert obs["before"]["path"] == str(candidate.parent)
    assert obs["volume_delta_bytes"] == obs["after"]["available_bytes"] - obs["before"]["available_bytes"]
    assert obs["attributable_release_bytes"] is None


@pytest.mark.parametrize("mutation", ["evidence_missing", "evidence_empty", "delivery_missing", "consumers", "escape", "empty", "new_file"])
def test_run_rechecks_manifest_and_evidence(world, mutation):
    manifest, candidate = fixture_manifest(world)
    result = plan(world, [item(candidate)], production_manifest=manifest)
    assert result["results"][0]["status"] == "ok"
    if mutation == "new_file":
        mk(str(candidate / "new.bin"))
    elif mutation == "evidence_missing":
        (manifest.parent / "receipt.json").unlink()
    elif mutation == "evidence_empty":
        (manifest.parent / "receipt.json").write_bytes(b"")
    elif mutation == "delivery_missing":
        (manifest.parent / "movie.mp4").unlink()
    else:
        data = json.loads(manifest.read_text())
        if mutation == "consumers":
            data["entries"][-1]["consumers"] = ["another stage"]
        elif mutation == "escape":
            data["entries"][-1]["path"] = "../outside"
        else:
            data["entries"] = []
        manifest.write_text(json.dumps(data))
        with pytest.raises((SystemExit, ValueError)):
            run(world, result["seal"])
        assert candidate.is_dir()
        return
    rows = run(world, result["seal"])
    assert candidate.is_dir() and any(r.get("status") == "refused" for r in rows)


def test_signed_and_unavailable_observations():
    before = {"device": 1, "available_bytes": 1000, "container": "a", "container_free_bytes": 1600}
    after = dict(before, available_bytes=950, container_free_bytes=1350)
    obs = batch_space.changes(before, after)
    assert (obs["volume_delta_bytes"], obs["container_delta_bytes"]) == (-50, -250)
    unknown = batch_space.changes(before, dict(after, device=2, container=None))
    assert unknown["volume_delta_bytes"] is None and unknown["container_delta_bytes"] is None


def test_triage_cli_writes_protection_and_review_in_report(world, monkeypatch):
    import sys

    manifest, _ = fixture_manifest(world)
    raw = str(manifest.parent / "raw.mov")
    out = world["tmp"] / "scan"
    monkeypatch.setattr(sys, "argv", ["triage.py", "--out", str(out), "--no-video", "--no-dups",
                                     "--no-wt-breakdown", "--production-manifest", str(manifest)])
    monkeypatch.setattr(triage.Ctx, "snapshot_lsof", lambda ctx: setattr(ctx, "lsof_ok", True))
    monkeypatch.setattr(triage, "breadth_first", lambda ctx: setattr(ctx, "du_timeouts", []))
    monkeypatch.setattr(triage, "tier1", lambda ctx: ([{"path": raw, "bytes": 20}], [], [], []))
    monkeypatch.setattr(triage, "worktrees", lambda ctx: ([], [], []))
    monkeypatch.setattr(triage, "apply_red_lines", lambda rows: (rows, [], []))
    for name in ("tier3", "rendering_dirs", "unclassified", "archive_check", "backup_failures"):
        monkeypatch.setattr(triage, name, lambda *a, **kw: [])
    for name in ("quarantine_summary", "cold_ledger_summary", "backup_state"):
        monkeypatch.setattr(triage, name, lambda: {})
    monkeypatch.setattr(triage, "df_both", lambda p: {"used": 50, "avail": 40, "device": "fixture"})
    triage.main()
    result = json.loads((out / "triage.json").read_text())
    assert result["tier1"] == []
    assert result["protected_production"][0]["path"] == raw
    report = (out / "triage.md").read_text()
    assert "待复核 · intermediate" in report and "保留 · delivery" in report
