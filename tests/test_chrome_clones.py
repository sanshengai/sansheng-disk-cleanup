import copy
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import chrome_clones as c
import protect


def test_path_scope_and_symlink(tmp_path, monkeypatch):
    root = tmp_path / 'X' / 'com.google.Chrome.code_sign_clone'
    root.mkdir(parents=True)
    p = root / 'code_sign_clone.abc123'; p.mkdir()
    monkeypatch.setattr(protect, 'chrome_clone_root', lambda: root)
    assert protect.chrome_clone_path(p, {}) == p
    for bad in [root, root/'code_sign_clone.*', tmp_path/'code_sign_clone.abc123']:
        with pytest.raises(ValueError): protect.chrome_clone_path(bad, {})
    q = root/'code_sign_clone.def456'; q.symlink_to(p)
    with pytest.raises(ValueError): protect.chrome_clone_path(q, {})
    with pytest.raises(ValueError): protect.chrome_clone_path(p, {'protect': [str(p)]})


def test_empty_tampered_and_changed_profile():
    profile = {}
    plan = {'schema': 'chrome-clones/1', 'entries': [{'path': 'example'}],
            'profile_guard': protect.profile_guard(profile)}
    c.check_plan(plan, c.digest(plan), 'existing authorization', profile)
    with pytest.raises(ValueError): c.check_plan(plan, c.digest(plan), '', profile)
    with pytest.raises(ValueError): c.check_plan(plan, 'wrong', 'authorized', profile)
    with pytest.raises(ValueError): c.check_plan(plan, c.digest(plan), 'authorized', {'protect': ['/a']})
    empty = copy.deepcopy(plan); empty['entries'] = []
    with pytest.raises(ValueError): c.check_plan(empty, c.digest(empty), 'authorized', profile)


def test_open_or_incomplete_probe_rejected(monkeypatch):
    for rc, out, err in [(0, 'Google Chrome open', ''), (1, '', 'permission denied'), (2, '', ''), (0, '', '')]:
        monkeypatch.setattr(c.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=rc, stdout=out, stderr=err))
        with pytest.raises(ValueError): c.unopened(Path('/fixture'))
    monkeypatch.setattr(c.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr=''))
    c.unopened(Path('/fixture'))


def test_current_version_rejected_before_native(tmp_path, monkeypatch):
    p = tmp_path/'clone'; p.mkdir(); (p/c.BUNDLE).mkdir()
    monkeypatch.setattr(protect, 'chrome_clone_path', lambda *a: p)
    # Macs supply birthtime; use a stand-in to keep the guard test portable.
    original = Path.stat
    def st(self, **kw):
        if self == p: return SimpleNamespace(st_birthtime=time.time()-200000)
        return original(self, **kw)
    monkeypatch.setattr(Path, 'stat', st)
    monkeypatch.setattr(c, 'version', lambda *a: '155.0.1.1')
    with pytest.raises(ValueError, match='当前'): c.entry(p, tmp_path, {})


def test_extra_data_rejected(tmp_path, monkeypatch):
    p = tmp_path/'clone'; p.mkdir(); (p/c.BUNDLE).mkdir(); (p/'user-data').mkdir()
    monkeypatch.setattr(protect, 'chrome_clone_path', lambda *a: p)
    with pytest.raises(ValueError, match='额外'): c.entry(p, tmp_path, {})


def test_inventory_link_escape(tmp_path):
    p = tmp_path/'clone'; p.mkdir(); (p/'outside').symlink_to(tmp_path)
    with pytest.raises(ValueError, match='越界'): c.inventory(p, {})


def test_native_failure_stops_batch_and_returns_nonzero(tmp_path, monkeypatch):
    import json
    first = tmp_path/'first'; first.mkdir()
    second = tmp_path/'second'; second.mkdir()
    entries = [{'path': str(first)}, {'path': str(second)}]
    plan = {'schema': 'chrome-clones/1', 'entries': entries,
            'profile_guard': protect.profile_guard({})}
    (tmp_path/'chrome-clones-plan.json').write_text(json.dumps(plan))
    monkeypatch.setattr(sys, 'argv', ['chrome_clones.py', 'apply', '--run', str(tmp_path),
                                     '--seal', c.digest(plan), '--reason', 'authorized'])
    monkeypatch.setattr(protect, 'load_profile', lambda: {})
    monkeypatch.setattr(c, 'helper', lambda r: Path('/fake/helper'))
    monkeypatch.setattr(c, 'entry', lambda p, *a: {'path': str(p)})
    called = []
    def fail(p, helper):
        called.append(p)
        raise ValueError('native failure')
    monkeypatch.setattr(c, 'native_cleanup', fail)
    with pytest.raises(SystemExit) as e: c.main()
    assert e.value.code == 1
    receipt = json.loads((tmp_path/'chrome-clones-execution.json').read_text())
    assert receipt['overall_status'] == 'stopped'
    assert called == [first]
    assert second.exists()


def test_native_helper_uses_only_validated_suffix(tmp_path, monkeypatch):
    p = tmp_path/'code_sign_clone.abc123'
    seen = []
    def run(argv, **kw):
        seen.append(argv)
        return SimpleNamespace(stdout='12345\n')
    monkeypatch.setattr(c.subprocess, 'run', run)
    assert c.native_cleanup(p, Path('/fixture/helper')) == 12345
    argv = seen[0]
    assert argv[-3:] == ['/fixture/helper', '--type=code-sign-clone-cleanup', '--unique-temp-dir-suffix=abc123']
    assert not any('no-wait-for-parent' in arg for arg in argv)
