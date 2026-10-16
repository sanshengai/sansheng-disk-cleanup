"""永久删除仅接受本轮封存的原文件；其他废纸篓内容保持完整。"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import finalize_trash as f
import report


@pytest.fixture
def case(tmp_path, monkeypatch):
    home = tmp_path.resolve() / 'home'
    home.mkdir()
    monkeypatch.setattr(f.protect, 'HOME', str(home))
    monkeypatch.setattr(report, 'make', lambda run: None)
    monkeypatch.setattr(f.act, 'open_files', lambda path: [])
    run = tmp_path.resolve() / 'run'
    run.mkdir()
    src = home / 'Downloads' / 'old.bin'
    src.parent.mkdir()
    src.write_bytes(b'old installer')
    st = src.stat()
    seal = {'schema': 'seal/1', 'scan_id': 'sample', 'cards': {'c01': {'items': [
        {'path': str(src), 'dev': st.st_dev, 'ino': st.st_ino, 'scope': 'all', 'method': 'delete'}]}}}
    seal['digest'] = f.plan.digest_of(seal)
    f.plan.write_json(run / 'seal.json', seal)
    root = home / '.Trash' / '叁笙清理-sample-20261016T000000Z'
    dest = root / 'Downloads' / 'old.bin'
    dest.parent.mkdir(parents=True)
    src.rename(dest)
    other = home / '.Trash' / 'unrelated.bin'
    other.write_bytes(b'keep me')
    rc = {'scan_id': 'sample', 'seal': seal['digest'], 'mode': 'run', 'started_at': '1',
          'finished_at': '2', 'cards': [{'id': 'c01', 'items': [{'path': str(src),
          'status': 'removed', 'method': 'delete', 'removed': {'bytes': st.st_blocks * 512},
          'disposal': {'mode': 'trash', 'dir': str(root)}}]}]}
    f.plan.write_json(run / 'receipt.json', rc)
    return run, seal, dest, other


def args(case):
    run, seal, _, _ = case
    return ['--run', str(run), '--seal', seal['digest']]


def draft(case):
    f.main(args(case))
    return json.loads((case[0] / 'finalize-trash-dryrun.json').read_text())['digest']


def test_dryrun_execute_and_report_accounting(case):
    digest = draft(case)
    assert case[2].exists()
    assert f.main(args(case) + ['--execute', digest, '--reason', '永久删除本轮文件']) == 0
    assert not case[2].exists()
    assert case[3].read_bytes() == b'keep me'
    _, receipts = report.load_receipts(str(case[0]))
    assert report.trashed_total(receipts) == 0
    assert sum(report.freed_by_card(receipts).values()) > 0
    with pytest.raises(ValueError, match='已有永久删除回执'):
        f.main(args(case) + ['--execute', digest, '--reason', '再做一次'])


def test_requires_explicit_reason(case):
    digest = draft(case)
    with pytest.raises(ValueError, match='原话'):
        f.main(args(case) + ['--execute', digest])
    assert case[2].exists()


def test_changed_content_rejected(case):
    digest = draft(case)
    case[2].write_bytes(b'changed data')
    with pytest.raises(ValueError, match='变化'):
        f.main(args(case) + ['--execute', digest, '--reason', '删除'])
    assert case[2].exists()


def test_replaced_inode_rejected(case):
    digest = draft(case)
    replacement = case[2].with_suffix('.new')
    replacement.write_bytes(case[2].read_bytes())
    replacement.replace(case[2])
    with pytest.raises(ValueError, match='原文件'):
        f.main(args(case) + ['--execute', digest, '--reason', '删除'])
    assert case[2].exists()


@pytest.mark.parametrize('parent', [False, True])
def test_symlink_rejected(case, parent):
    draft(case)
    target = case[2].parent if parent else case[2]
    renamed = target.with_name(target.name + '-saved')
    target.rename(renamed)
    target.symlink_to(renamed, target_is_directory=parent)
    with pytest.raises(OSError):
        f.main(args(case))
    assert renamed.exists()


def test_empty_receipt_rejected(case):
    rc = json.loads((case[0] / 'receipt.json').read_text())
    rc['cards'] = []
    f.plan.write_json(case[0] / 'receipt.json', rc)
    with pytest.raises(ValueError, match='没有可核实'):
        f.main(args(case))


def test_outside_trash_rejected(case):
    rc = json.loads((case[0] / 'receipt.json').read_text())
    rc['cards'][0]['items'][0]['disposal']['dir'] = str(case[3].parent)
    f.plan.write_json(case[0] / 'receipt.json', rc)
    with pytest.raises(ValueError, match='本轮专用'):
        f.main(args(case))
    assert case[3].exists()


def test_unsealed_source_rejected(case):
    rc = json.loads((case[0] / 'receipt.json').read_text())
    rc['cards'][0]['items'][0]['path'] += '-unsealed'
    f.plan.write_json(case[0] / 'receipt.json', rc)
    with pytest.raises(ValueError, match='封存'):
        f.main(args(case))


def test_hardlink_rejected(case):
    os.link(case[2], case[2].with_suffix('.link'))
    with pytest.raises(ValueError, match='硬链接'):
        f.main(args(case))


def test_bad_seal_and_missing_preview_rejected(case):
    with pytest.raises(ValueError):
        f.main(['--run', str(case[0]), '--seal', 'wrong'])
    with pytest.raises((ValueError, OSError)):
        f.main(args(case) + ['--execute', 'wrong', '--reason', '删除'])
    assert case[2].exists()


@pytest.mark.parametrize('busy', [None, ['opened-file']])
def test_busy_or_unknown_rejected(case, monkeypatch, busy):
    monkeypatch.setattr(f.act, 'open_files', lambda path: busy)
    with pytest.raises(ValueError, match='占用'):
        f.main(args(case))
    assert case[2].exists()


def test_inode_guard_mutation_is_detected(case):
    """去掉真实被测路径上的 inode 闸门，替换文件的反例就会被放行。"""
    import inspect
    replacement = case[2].with_suffix('.new')
    replacement.write_bytes(case[2].read_bytes())
    replacement.replace(case[2])
    with pytest.raises(ValueError, match='原文件'):
        f.candidates(str(case[0]), case[1])
    source = inspect.getsource(f.candidates)
    gate = "                if (fp['st_dev'], fp['st_ino']) != (original.get('dev'), original.get('ino')):\n                    raise ValueError('废纸篓文件不是封存时的原文件')\n"
    assert gate in source
    namespace = dict(vars(f))
    exec(source.replace(gate, ''), namespace)
    assert len(namespace['candidates'](str(case[0]), case[1])) == 1
