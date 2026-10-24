import copy
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import node_compile_cache as n


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path / 'node-compile-cache'
    family = root / ('v24.20.0-arm64-964aae3f-' + str(os.getuid()))
    family.mkdir(parents=True)
    source = family / '1234abcd'
    source.write_bytes(b'Node compiled cache fixture')
    run = tmp_path / 'evidence'
    run.mkdir()
    monkeypatch.setattr(n, 'discover_root', lambda: root)
    monkeypatch.setattr(n.protect, 'load_profile', lambda: {})
    monkeypatch.setattr(n, 'readable', lambda *a: None)
    monkeypatch.setattr(n, 'opened', lambda *a: set())
    # Move the clock, never the age guard. ctime cannot be set via utime.
    now = time.time()
    monkeypatch.setattr(n.time, 'time', lambda: now + 172800)
    monkeypatch.setattr(n.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr=''))
    plan = n.create_plan(run, 'direct', {})
    return SimpleNamespace(root=root, family=family, source=source, run=run, plan=plan, seal=n.digest(plan))


def preview(s, method='direct'):
    return n.execute(s.plan, s.seal, s.run, method, '用户批准本批夹具永久删除', dry_run=True)


def test_plan_and_sealed_preview_preserve_all_files(scene):
    s = scene
    assert len(s.plan['entries']) == 1
    e = s.plan['entries'][0]
    assert {'dev', 'inode', 'size', 'mtime_ns', 'ctime_ns', 'sha256', 'path'} <= e.keys()
    receipt, output = preview(s)
    assert s.source.exists() and s.family.exists()
    assert receipt['overall_status'] == 'validated'
    assert receipt['allocated_removed'] == 0
    assert json.loads(output.read_text())['entries'][0]['status'] == 'validated'


def test_no_authorization_refuses_even_dry_run(scene):
    s = scene
    with pytest.raises(ValueError, match='授权'):
        n.execute(s.plan, s.seal, s.run, 'direct', '', dry_run=True)
    assert s.source.exists()


def test_empty_and_tampered_seal(scene):
    s = scene
    empty = copy.deepcopy(s.plan)
    empty['entries'] = []
    with pytest.raises(ValueError, match='为空'):
        n.preflight(empty, n.digest(empty), s.run, 'direct')
    with pytest.raises(ValueError, match='摘要'):
        n.preflight(s.plan, 'bad', s.run, 'direct')
    altered = copy.deepcopy(s.plan)
    altered['entries'][0]['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='摘要'):
        n.preflight(altered, s.seal, s.run, 'direct')


def test_path_escape_even_with_resealed_plan(scene):
    s = scene
    bad = copy.deepcopy(s.plan)
    bad['entries'][0]['path'] = str(s.run / '1234abcd')
    with pytest.raises(ValueError, match='路径逃逸|版本目录身份清单'):
        n.preflight(bad, n.digest(bad), s.run, 'direct')


def test_unrelated_new_file_retained_and_reported(scene):
    s = scene
    new = s.family / 'abcd1234'
    new.write_bytes(b'new cache')
    receipt, _ = preview(s)
    assert str(new) in receipt['unselected_before']['new_files_retained']
    assert s.source.exists() and new.exists()


def test_open_file_rejects_before_mutation(scene, monkeypatch):
    s = scene
    monkeypatch.setattr(n, 'opened', lambda *a: {str(s.source)})
    with pytest.raises(ValueError, match='打开'):
        preview(s)
    assert s.source.exists()


def test_content_change_rejected_by_sha_when_metadata_probe_is_forged(scene, monkeypatch):
    s = scene
    # Exercise the sealed SHA gate, not only the metadata guard.
    s.source.write_bytes(b'Other compiled blob fixture!')
    original_fp = n.fingerprint
    def forged_meta(*args):
        actual = original_fp(*args)
        return {**s.plan['entries'][0], 'sha256': actual['sha256']}
    monkeypatch.setattr(n, 'inventory', lambda root: s.plan['inventory'])
    monkeypatch.setattr(n, 'fingerprint', forged_meta)
    with pytest.raises(ValueError, match='内容'):
        preview(s)
    assert s.source.exists()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink'])
def test_links_never_selected(scene, kind):
    s = scene
    other = s.family / 'abcdef12'
    if kind == 'symlink':
        other.symlink_to(s.source)
    else:
        os.link(s.source, other)
    plan = n.create_plan(s.run, 'direct', {})
    assert str(other) not in {e['path'] for e in plan['entries']}
    if kind == 'hardlink':
        assert not plan['entries']


def test_recent_ctime_and_mtime_each_protect(scene, monkeypatch):
    s = scene
    monkeypatch.setattr(n.time, 'time', lambda: s.source.stat().st_ctime + 12)
    assert not n.create_plan(s.run, 'direct', {})['entries']
    now = s.source.stat().st_ctime + 172800
    monkeypatch.setattr(n.time, 'time', lambda: now)
    os.utime(s.source, (now, now))
    assert not n.create_plan(s.run, 'direct', {})['entries']


def test_profile_protection_and_guard_not_bypassed(scene, monkeypatch):
    s = scene
    profile = {'protect': [str(s.source)]}
    assert not n.create_plan(s.run, 'direct', profile)['entries']
    monkeypatch.setattr(n.protect, 'load_profile', lambda: profile)
    with pytest.raises(ValueError, match='保护配置'):
        preview(s)


def test_method_mismatch_and_missing_preview_rejected(scene):
    s = scene
    with pytest.raises(ValueError, match='方式'):
        n.execute(s.plan, s.seal, s.run, 'trash', '已授权', dry_run=True)
    with pytest.raises(ValueError, match='预演'):
        n.execute(s.plan, s.seal, s.run, 'direct', '已授权', pilot=True)
    assert s.source.exists()


def test_direct_only_removes_sealed_leaf_and_retains_version_directory(scene):
    s = scene
    preview(s)
    receipt, _ = n.execute(s.plan, s.seal, s.run, 'direct', '夹具永久删除授权', pilot=True)
    assert not s.source.exists()
    assert s.family.is_dir() and s.root.is_dir()
    assert receipt['allocated_removed'] == s.plan['entries'][0]['allocated']
    assert receipt['allocated_trashed'] == 0
    assert 'free_delta' in receipt


def test_trash_keeps_bytes_and_receipt_destination(scene, monkeypatch):
    s = scene
    home = s.root.parent
    (home / '.Trash').mkdir()
    monkeypatch.setattr(n.protect, 'HOME', str(home))
    s.plan = n.create_plan(s.run, 'trash', {})
    s.seal = n.digest(s.plan)
    preview(s, method='trash')
    receipt, _ = n.execute(s.plan, s.seal, s.run, 'trash', '夹具移入废纸篓授权', pilot=True)
    target = Path(receipt['entries'][0]['destination'])
    assert target.read_bytes() == b'Node compiled cache fixture'
    assert not s.source.exists() and s.family.exists()
    assert receipt['allocated_removed'] == 0 and receipt['allocated_trashed'] > 0


def test_second_occupancy_check_stops_execution(scene, monkeypatch):
    s = scene
    preview(s)
    monkeypatch.setattr(n.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='999999', stderr=''))
    receipt, _ = n.execute(s.plan, s.seal, s.run, 'direct', '已授权', pilot=True)
    assert receipt['overall_status'] == 'stopped'
    assert s.source.exists()


def test_discovered_root_changed_rejects(scene, monkeypatch):
    s = scene
    root2 = s.run / 'node-compile-cache'
    root2.mkdir()
    monkeypatch.setattr(n, 'discover_root', lambda: root2)
    with pytest.raises(ValueError, match='缓存根'):
        preview(s)


def test_cli_has_no_arbitrary_target_path_option(scene):
    s = scene
    with pytest.raises(SystemExit):
        n.main(['plan', '--run', str(s.run), '--method', 'direct', '--path', str(s.source)])
    assert s.source.exists()


def test_pilot_then_same_seal_run_does_not_double_count(scene):
    s = scene
    second = s.family / 'abcdef12'
    second.write_bytes(b'second cache')
    s.plan = n.create_plan(s.run, 'direct', {})
    s.seal = n.digest(s.plan)
    preview(s)
    pilot, _ = n.execute(s.plan, s.seal, s.run, 'direct', '已授权', pilot=True)
    assert pilot['overall_status'] == 'completed'
    receipt, _ = n.execute(s.plan, s.seal, s.run, 'direct', '已授权')
    assert receipt['overall_status'] == 'completed'
    assert len(receipt['entries']) == 1
    assert receipt['allocated_removed'] + pilot['allocated_removed'] == sum(e['allocated'] for e in s.plan['entries'])
    assert s.family.exists() and not list(s.family.iterdir())


def test_new_file_between_preview_and_pilot_preserved(scene):
    s = scene
    preview(s)
    other = s.family / 'aaaabbbb'
    other.write_bytes(b'created after preview')
    receipt, _ = n.execute(s.plan, s.seal, s.run, 'direct', '已授权', pilot=True)
    assert other.exists() and not s.source.exists()
    assert str(other) in receipt['unselected_after']['new_files_retained']


def test_new_file_during_execution_is_retained_and_reported(scene, monkeypatch):
    s = scene
    preview(s)
    original = n.dispose
    new = s.family / 'aaaabbbb'
    def concurrent(*args):
        result = original(*args)
        new.write_bytes(b'new writer')
        return result
    monkeypatch.setattr(n, 'dispose', concurrent)
    receipt, _ = n.execute(s.plan, s.seal, s.run, 'direct', '已授权', pilot=True)
    assert receipt['overall_status'] == 'completed'
    assert str(new) in receipt['unselected_after']['new_files_retained']
    assert new.exists()


def test_mutation_between_preflight_and_disposal_is_preserved(scene, monkeypatch):
    s = scene
    preview(s)
    def change_before_batch(paths):
        s.source.write_bytes(b'fresh content')
    monkeypatch.setattr(n, 'unopened_batch', change_before_batch)
    receipt, _ = n.execute(s.plan, s.seal, s.run, 'direct', '已授权', pilot=True)
    assert receipt['overall_status'] == 'stopped'
    assert s.source.read_bytes() == b'fresh content'


@pytest.mark.parametrize('rc,out,err', [(0, '', ''), (1, '', 'denied'), (2, '', ''), (0, 'p123\nn/x', '')])
def test_batch_open_probe_fail_closed(scene, monkeypatch, rc, out, err):
    s = scene
    monkeypatch.setattr(n.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=rc, stdout=out, stderr=err))
    with pytest.raises(ValueError, match='打开|完整'):
        n.unopened_batch([str(s.source)])


def test_current_uid_family_and_regular_leaf_only(scene):
    s = scene
    alien = s.root / 'v24.20.0-arm64-964aae3f-999999'
    alien.mkdir()
    (alien / '1234abcd').write_bytes(b'other uid family')
    (s.family / 'user-data.txt').write_bytes(b'not a Node cache leaf')
    plan = n.create_plan(s.run, 'direct', {})
    assert [e['path'] for e in plan['entries']] == [str(s.source)]
    assert len(plan['skipped']) == 2


def test_formal_run_requires_successful_pilot(scene):
    s = scene
    preview(s)
    with pytest.raises(ValueError, match='试点'):
        n.execute(s.plan, s.seal, s.run, 'direct', '已授权')
    assert s.source.exists()


def test_missing_family_identity_rejected_before_mutation(scene):
    s = scene
    bad = copy.deepcopy(s.plan)
    bad['families'] = {}
    with pytest.raises(ValueError, match='版本目录身份清单'):
        n.preflight(bad, n.digest(bad), s.run, 'direct')
    assert s.source.exists()


def test_leaf_replaced_with_symlink_after_preflight_refused(scene, monkeypatch):
    s = scene
    preview(s)
    target = s.run / 'keep'
    target.write_bytes(b'never remove')
    def switch(paths):
        s.source.unlink()
        s.source.symlink_to(target)
    monkeypatch.setattr(n, 'unopened_batch', switch)
    receipt, _ = n.execute(s.plan, s.seal, s.run, 'direct', '已授权', pilot=True)
    assert receipt['overall_status'] == 'stopped'
    assert target.read_bytes() == b'never remove' and s.source.is_symlink()


def test_full_inventory_and_profile_loads_are_not_per_leaf(scene, monkeypatch):
    s = scene
    for i in range(130):
        (s.family / f'{i:08x}').write_bytes(b'cache ' + str(i).encode())
    s.plan = n.create_plan(s.run, 'direct', {})
    s.seal = n.digest(s.plan)
    preview(s)
    n.execute(s.plan, s.seal, s.run, 'direct', '已授权', pilot=True)
    counts = {'inventory': 0, 'profile': 0, 'dump': 0}
    original_inventory, original_dump = n.inventory, n.dump
    def inventory(root):
        counts['inventory'] += 1
        return original_inventory(root)
    def profile():
        counts['profile'] += 1
        return {}
    def dump(path, data):
        counts['dump'] += 1
        return original_dump(path, data)
    monkeypatch.setattr(n, 'inventory', inventory)
    monkeypatch.setattr(n.protect, 'load_profile', profile)
    monkeypatch.setattr(n, 'dump', dump)
    receipt, out = n.execute(s.plan, s.seal, s.run, 'direct', '已授权')
    assert receipt['overall_status'] == 'completed'
    assert len(receipt['entries']) == 130
    assert counts == {'inventory': 2, 'profile': 3, 'dump': 2}
    assert len(out.with_suffix('.jsonl').read_text().splitlines()) == 130
