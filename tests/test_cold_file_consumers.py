from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cold_consumers import ConsumerChecks


@pytest.mark.parametrize("edge", ["retained", "deleting", "parent", "empty", "outside", "partial"])
def test_file_selection_guards(tmp_path, edge):
    batch = tmp_path / "batch"
    root = tmp_path / "refs"
    batch.mkdir()
    root.mkdir()
    for name in ("raw", "discard"):
        (batch / name).touch()
    selected = [str(batch/"discard")]
    link = {"retained": batch/"raw", "deleting": batch/"discard", "parent": batch}.get(edge)
    if link:
        (root/"current").symlink_to(link)
    if edge == "empty":
        selected = []
    if edge == "outside":
        selected.append(str(tmp_path/"unrelated"))
    roots = [str(root)]
    if edge == "partial":
        roots.append(str(tmp_path/"absent"))
    policy = ConsumerChecks({"consumer_scopes": [{"source": str(batch), "roots": roots}]})
    assert policy.permits({"abs": str(batch)}, selected) is (edge == "retained")


def test_quick_act_run_rechecks_exact_subset_after_plan(world, monkeypatch):
    import quick_act
    import triage
    from test_quick_act import item, mk, plan, run

    batch = world["work"] / "assets" / "aigc-staged"
    root = world["tmp"] / "site"
    root.mkdir()
    raw = Path(mk(str(batch/"original.bin")))
    draft = Path(mk(str(batch/"old-draft.bin")))
    (root/"cover").symlink_to(raw)
    monkeypatch.setitem(triage.LOCAL, "cold_storage", {"consumer_scopes": [{"source": str(batch), "roots": [str(root)]}]})
    monkeypatch.setattr(quick_act, "backup_verify", lambda *a: (True, "fixture verified", None))
    chosen = item(batch, scope="files", files=[str(draft)], kind="backed_up", cold_batch="fixture")
    candidate = plan(world, [chosen], text=f"同意冷存 {batch}")
    assert candidate["results"][0]["status"] == "ok"
    (root/"draft-used").symlink_to(draft)
    rows = run(world, candidate["seal"])
    assert any(r["status"] == "refused" and "消费者" in str(r["why"]) for r in rows)
    assert draft.exists() and raw.exists()
    (root/"draft-used").unlink()
    run(world, candidate["seal"])
    assert not draft.exists() and raw.exists()


from test_quick_act import world  # the isolated on-disk fixture used by the CLI test
