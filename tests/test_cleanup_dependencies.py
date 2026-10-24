import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import plan


def context(items):
    targets = plan.cleanup_targets([{'verdict': 'clean', 'items': items}])
    return {'cleanup_files': targets['files'], 'cleanup_paths': targets['trees']}


def test_exact_selection_preserves_sibling_receipt(tmp_path):
    root = str(tmp_path)
    copy, receipt = str(tmp_path / 'copy'), str(tmp_path / 'manifest.json')
    for selected in ([copy], [{'path': copy, 'size': 1, 'mtime_ns': 1}]):
        ctx = context([{'method': 'delete', 'scope': 'files', 'path': root, 'files': selected}])
        assert not plan.dependency_selected(receipt, ctx)
        assert plan.dependency_selected(copy, ctx)
    ctx = context([{'method': 'delete', 'scope': 'all', 'path': root}])
    assert plan.dependency_selected(receipt, ctx)


def test_other_card_selecting_receipt_still_blocks(tmp_path):
    receipt = str(tmp_path / 'receipt.json')
    ctx = context([
        {'method': 'delete', 'scope': 'files', 'path': str(tmp_path), 'files': [str(tmp_path / 'copy')]},
        {'method': 'delete', 'scope': 'files', 'path': str(tmp_path), 'files': [receipt]},
    ])
    assert plan.dependency_selected(receipt, ctx)
    assert not plan.dependency_selected(receipt + '.other', ctx)


def test_empty_input_and_keep_cards_do_not_select(tmp_path):
    assert plan.cleanup_targets([]) == {'files': set(), 'trees': []}
    assert not plan.dependency_selected(str(tmp_path / 'receipt'), context([]))
    assert plan.cleanup_targets([{'verdict': 'keep', 'items': [{'path': str(tmp_path)}]}]) == {'files': set(), 'trees': []}


def test_malformed_input_does_not_crash_discovery():
    for items in (None, 12, "bad"):
        assert plan.cleanup_targets([{"verdict": "clean", "items": items}]) == {"files": set(), "trees": []}
    for files in (None, 12, "bad"):
        assert plan.cleanup_targets([{"verdict": "clean", "items": [{"method": "delete", "scope": "files", "files": files}]}]) == {"files": set(), "trees": []}


def test_checkpoint_gate_rejects_selected_receipt(tmp_path, monkeypatch):
    from test_copy_checkpoint import fixture
    repo, target, receipt, item, git = fixture(tmp_path)
    monkeypatch.setattr(plan.protect, 'protected_reason', lambda *a: None)
    monkeypatch.setattr(plan.protect, 'under_roots', lambda *a: True)
    ctx = dict(context([{'method': 'delete', 'scope': 'files', 'path': str(target.parent), 'files': [str(target)]}]), profile={}, nodes=[])
    errors, record = plan.check_item(item, ctx)
    assert 'checkpoint' in record
    blocked = dict(ctx, cleanup_files={str(receipt)})
    errors, record = plan.check_item(item, blocked)
    assert any('保留仓库或任务回执' in e for e in errors)
    assert 'checkpoint' not in record


def test_malformed_checkpoint_dependency_dispatch_is_total():
    import artifact_checkpoint as ac
    for value in (None, 7, "bad", {}, {"checkpoint": []}, {"checkpoint": "bad"}, {"checkpoint": {"task": "bad"}}):
        assert ac.dependencies(value) == []
        assert not ac.verify(value)[0]
