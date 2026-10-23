import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import artifact_checkpoint as ac
import plan
import act


def fixture(tmp_path):
    repo = tmp_path.resolve() / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=test',
            '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false', *args]).decode().strip()
    git('init', '-q')
    (repo / '.gitignore').write_text('scratch/\n')
    (repo / 'source.json').write_text('{"data": "retained"}\n')
    git('add', '.'); git('commit', '-qm', 'delivery')
    commit = git('rev-parse', 'HEAD'); ref = git('symbolic-ref', 'HEAD')
    root = repo / 'scratch'; root.mkdir()
    target = root / 'copy.json'; target.write_bytes((repo / 'source.json').read_bytes())
    receipt = root / 'receipt.json'; receipt.write_text(json.dumps({'candidate': commit}))
    item = {'path': str(target), 'method': 'delete', 'scope': 'all', 'checkpoint': {
        'kind': 'git_blob_copy', 'owner_repo': str(repo), 'repo': str(repo),
        'commit': commit, 'retain_ref': ref, 'git_path': 'source.json',
        'sha256': ac.sha256_file(target), 'task': {'root': str(root), 'role': 'test_copy',
            'repo': str(repo), 'commit': commit, 'retain_ref': ref, 'receipt': str(receipt),
            'receipt_sha256': ac.sha256_file(receipt), 'commit_pointer': ['candidate']}}}
    return repo, target, receipt, item, git


def test_restore_proof_and_active_exception(tmp_path):
    repo, target, receipt, item, git = fixture(tmp_path)
    assert ac.verify(item)[0], ac.verify(item)
    assert not plan.project_item_errors(str(target), [{'status':'active','name':'parent'}], ac.verify(item)[0])
    assert plan.project_item_errors(str(target), [{'status':'protected','name':'parent'}], ac.verify(item)[0])
    assert not ac.verify({})[0]
    assert not ac.verify(dict(item, scope='files'))[0]
    assert not ac.verify(dict(item, path=str(repo / 'source.json')))[0]


def test_ref_content_receipt_and_path_mutations(tmp_path):
    repo, target, receipt, item, git = fixture(tmp_path)
    for field, value in [('retain_ref','refs/heads/absent'),('sha256','0'*64),
                         ('git_path','../source.json'),('git_path','*'),('git_path','')]:
        bad=copy.deepcopy(item);bad['checkpoint'][field]=value
        assert not ac.verify(bad)[0], field
    for field,value in [('root',str(repo)),('role','original'),('commit_pointer',[]),
                        ('receipt_sha256','0'*64),('receipt',str(target))]:
        bad=copy.deepcopy(item);bad['checkpoint']['task'][field]=value
        assert not ac.verify(bad)[0], field
    target.write_text('changed')
    # Updating the planned hash must not permit data absent from retained Git.
    item['checkpoint']['sha256']=ac.sha256_file(target)
    assert not ac.verify(item)[0]


def test_unmerged_task_and_hardlinks(tmp_path):
    repo,target,receipt,item,git=fixture(tmp_path)
    git('checkout','-qb','unfinished')
    (repo/'source.json').write_text('new');git('add','.');git('commit','-qm','unfinished')
    commit=git('rev-parse','HEAD');receipt.write_text(json.dumps({'candidate':commit}))
    item['checkpoint']['task'].update(commit=commit,receipt_sha256=ac.sha256_file(receipt))
    assert not ac.verify(item)[0]
    os.link(target,target.with_suffix('.hard'))
    assert not ac.verify(item)[0]


def test_execute_rechecks_after_sealing(tmp_path,monkeypatch):
    repo,target,receipt,item,git=fixture(tmp_path)
    assert ac.verify(item)[0]
    receipt.write_text('{}')
    monkeypatch.setattr(act,'verify_identity',lambda *args:(str(target),None))
    monkeypatch.setattr(act,'blocking_processes',lambda *args:([],[]))
    result=act.execute_item(0,item,{},False,time.time())
    assert result['status']=='skipped' and target.exists()


def test_plan_rejects_deleting_restore_dependency(tmp_path, monkeypatch):
    repo,target,receipt,item,git=fixture(tmp_path)
    monkeypatch.setattr(plan.protect, 'protected_reason', lambda *a: None)
    monkeypatch.setattr(plan.protect, 'under_roots', lambda *a: True)
    ctx={'profile':{},'nodes':[], 'cleanup_paths':[]}
    errors,record=plan.check_item(item,ctx)
    assert 'checkpoint' in record
    for dependency in (receipt, repo):
        errors,record=plan.check_item(item,dict(ctx,cleanup_paths=[str(dependency)]))
        assert any('保留仓库或任务回执' in e for e in errors)
        assert 'checkpoint' not in record
    errors,record=plan.check_item(dict(item,checkpoint={}),ctx)
    assert errors and 'checkpoint' not in record
