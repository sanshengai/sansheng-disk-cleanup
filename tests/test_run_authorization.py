import copy
import json
import os
from pathlib import Path
import sys
import time

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import act
import plan
import run_authorization as auth
from test_plan_act import node


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = (tmp_path / 'outputs').resolve(); root.mkdir()
    f = root / 'temp.bin'; f.write_bytes(b'completed')
    run = tmp_path / 'run'; run.mkdir(); (run / 'facts.json').write_text('{"scan_id":"test"}')
    item = {'path': str(root), 'method': 'delete', 'scope': 'files', 'files': [str(f)]}
    item['run_authorization'] = auth.create(run, [item], '用户允许永久删除指定过程文件，包括24h内文件', allow_recent=True, retire_completed_outputs=True, audit=False)
    profile = {'roots': [str(root)], 'protect': [], 'protect_globs': []}
    monkeypatch.setattr(act, 'open_files', lambda p: [])
    ctx = {'profile': profile, 'nodes': [node(root, recent_24h=1)], 'reviews': [], 'matched': set(), 'legacy': False,
           'run': str(run), 'cleanup_paths': [], 'cleanup_files': {str(f)}, 'soft': []}
    return root, f, run, item, profile, ctx


def test_real_plan_and_act_recent_only(setup):
    root, f, run, item, profile, ctx = setup
    errors, sealed = plan.check_item(item, ctx)
    assert not errors, errors
    assert sealed['run_authorization'] == item['run_authorization']
    result = act._execute_item(0, sealed, profile, True, time.time(), str(run))
    assert result['status'] == 'planned', result
    assert f.exists()
    del item['run_authorization']
    assert any('24 小时' in e for e in plan.check_item(item, ctx)[0])


@pytest.mark.parametrize('change', ['content','selection','scan','document','symlink','hardlink','empty'])
def test_changes_rejected(setup, change):
    root, f, run, item, _, _ = setup
    if change == 'content': f.write_bytes(b'changed!!')
    elif change == 'selection': item['files'] += [str(root / 'new')]
    elif change == 'scan': (run / 'facts.json').write_text('{"scan_id":"other"}')
    elif change == 'document': (run / 'selection-authorization.json').write_text('{}')
    elif change == 'symlink': f.rename(root / 'retained'); f.symlink_to(root / 'retained')
    elif change == 'hardlink': os.link(f, root / 'hardlink')
    elif change == 'empty': item['files'] = []
    assert not auth.verify(item, run)[0]


def test_single_file_all_and_cross_run(setup):
    root, f, run, _, _, _ = setup
    other = run.parent / 'other'; other.mkdir(); (other / 'facts.json').write_text('{"scan_id":"test"}')
    item = {'path': str(f), 'method': 'delete', 'scope': 'all'}
    item['run_authorization'] = auth.create(other, [item], '允许近期文件', allow_recent=True, audit=False)
    assert auth.verify(item, other, recent=True)[0]
    assert not auth.verify(item, run)[0]


def test_open_protected_refs_not_waived(setup, monkeypatch):
    _, f, run, item, profile, ctx = setup
    ctx['nodes'][0]['open_by'] = [{'command': 'busy'}]
    assert any('正被' in e for e in plan.check_item(item, ctx)[0])
    ctx['nodes'][0]['open_by'] = []
    errors, sealed = plan.check_item(item, ctx); assert not errors
    monkeypatch.setattr(act, 'open_files', lambda p: [str(f)])
    result = act._execute_item(0, sealed, profile, True, time.time(), str(run))
    assert result['status'] == 'skipped' and f.exists()
    ctx['nodes'][0]['protected'] = True
    assert any('受保护' in e for e in plan.check_item(item, ctx)[0])
    ctx['nodes'][0]['protected'] = False; ctx['nodes'][0]['refs']['checked'] = False
    assert any('引用检查' in e for e in plan.check_item(item, ctx)[0])


def test_authorization_cannot_be_other_card_target(setup):
    *_, item, profile, ctx = setup
    ctx['cleanup_files'].add(item['run_authorization']['path'])
    assert any('授权证据' in e for e in plan.check_item(item, ctx)[0])


def test_delete_instant_hash_change_rejected(setup):
    _, f, _, item, _, _ = setup
    _, row = auth.read_document(item)
    before = f.stat(); f.write_bytes(b'bad value'); os.utime(f, ns=(before.st_atime_ns,before.st_mtime_ns))
    res = act.ItemResult(0, item)
    act.delete_files([(str(f),before)],res,{},authorized_files={str(f):row['files'][0]})
    assert f.exists() and res.d['removed']['files'] == 0


def test_active_process_and_tracked_file_still_block(setup, monkeypatch):
    _, f, run, item, profile, ctx = setup
    errors, sealed = plan.check_item(item, ctx); assert not errors
    monkeypatch.setattr(act, 'blocking_processes', lambda i: (['producer'], []))
    result = act.execute_item(0, sealed, profile, False, time.time(), str(run))
    assert result['status'] == 'skipped' and f.exists()
    monkeypatch.setattr(act, 'list_tracked', lambda p: {str(f)})
    result = act._execute_item(0, sealed, profile, True, time.time(), str(run))
    assert result['removed']['files'] == 0 and f.exists()


def test_single_file_all_through_plan_and_act(setup):
    root,f,run,_,profile,ctx=setup
    run2=run.parent/'single';run2.mkdir();(run2/'facts.json').write_text('{"scan_id":"test"}')
    item={'path':str(f),'scope':'all','method':'delete'}
    item['run_authorization']=auth.create(run2,[item],'允许本轮单文件近期清理',allow_recent=True,audit=False)
    ctx.update(run=str(run2),nodes=[node(f,recent_24h=1)])
    errors,sealed=plan.check_item(item,ctx);assert not errors,errors
    result=act._execute_item(0,sealed,profile,True,time.time(),str(run2))
    assert result['status']=='planned',result


def test_new_unselected_file_is_not_adopted(setup):
    root,f,run,item,profile,ctx=setup
    other=root/'new-output.bin';other.write_bytes(b'retain')
    errors,sealed=plan.check_item(item,ctx);assert not errors
    result=act._execute_item(0,sealed,profile,True,time.time(),str(run))
    assert result['removed']['files']==1 and other.exists() and f.exists()
