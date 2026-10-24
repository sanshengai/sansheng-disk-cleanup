import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import artifact_checkpoint
import completed_output_checkpoint as checkpoint
import run_authorization as auth


def dump(p, data):
    p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(data))


def ref(p): return {'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}


def git(root, *args):
    return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True,text=True).stdout.strip()


@pytest.fixture
def fixture(tmp_path):
    repo = tmp_path.resolve(); git(repo,'init','-q'); git(repo,'config','user.name','test'); git(repo,'config','user.email','test@invalid')
    (repo/'.gitignore').write_text('web/dist/\nweb/.i18n-build/\napks/\nrun/\n')
    consumer=repo/'consumer.mjs'; consumer.write_text('export const output = "isolated-build";')
    git(repo,'add','.gitignore','consumer.mjs'); git(repo,'-c','commit.gpgsign=false','commit','-qm','source')
    commit=git(repo,'rev-parse','HEAD'); branch=git(repo,'symbolic-ref','HEAD')
    run=repo/'run';dump(run/'facts.json',{'scan_id':'scan'})
    proof={'kind':'completed_output_stage','owner_repo':str(repo),'retain_ref':branch,'consumer_files':[ref(consumer)]}
    return repo,run,proof,commit


def authorize(run,item,retire=True):
    item['run_authorization']=auth.create(run,[item],'永久删除已完成输出，保留明确成品',allow_recent=True,retire_completed_outputs=retire,audit=False)
    return item


def web(fixture, stage=False):
    repo,run,proof,commit=fixture;webroot=repo/'web';root=webroot/('.i18n-build' if stage else 'dist')
    file=root/('zh-Hans/page.html' if stage else 'page.html');file.parent.mkdir(parents=True);file.write_text('done')
    files={'page.html':ref(file)['sha256']};assembly={'files':1,'sha256':hashlib.sha256(('page.html\0'+files['page.html']).encode()).hexdigest()}
    manifest=webroot/'.i18n-build-manifest.json';receipt=webroot/'dist/i18n-build-receipt.json'
    dump(manifest,{'schema_version':'i18n-build-manifest-v1','source_commit':commit,'assembly':assembly,'files':files})
    dump(receipt,{'schema_version':'i18n-build-receipt-v2','source_commit':commit,'assembly':assembly})
    proof.update(variant='web_assembly',web_root=str(webroot),manifest=ref(manifest),receipt=ref(receipt))
    return authorize(run,{'path':str(root),'method':'delete','scope':'files','files':[str(file)],'checkpoint':proof})


def apk(fixture):
    repo,run,proof,commit=fixture;root=repo/'apks'
    def package(version,code):
        p=root/version/('test-'+version+'.apk');p.parent.mkdir(parents=True);p.write_bytes(version.encode())
        receipt=p.parent/'version.json';dump(receipt,{'version':version,'versionCode':code,'appId':'test.app','inspectable':False,'builtFrom':commit,'apkSha256':ref(p)['sha256'],'sizeBytes':p.stat().st_size})
        return {'path':str(p),'receipt':ref(receipt)}
    old,new=package('0.1.0',100),package('1.0.0',10000)
    proof.update(variant='apk_build',packages=[old],retained_apks=[new])
    return authorize(run,{'path':str(root),'method':'delete','scope':'files','files':[old['path']],'checkpoint':proof})


@pytest.mark.parametrize('stage',[False,True])
def test_web_real_git_receipt_and_both_caller_forms(fixture,stage):
    item=web(fixture,stage);assert artifact_checkpoint.verify(item)[0],checkpoint.verify(item)
    item['files']=[{'path':p,'size':Path(p).stat().st_size,'mtime_ns':Path(p).stat().st_mtime_ns} for p in item['files']]
    assert checkpoint.verify(item)[0]
    deps=artifact_checkpoint.dependencies(item)
    assert item['checkpoint']['receipt']['path'] in deps
    assert item['files'][0]['path'] not in deps
    assert item['checkpoint']['owner_repo'] not in deps


@pytest.mark.parametrize('change',['manifest','receipt','consumer','output','empty','wrongroot','wrongcommit','assembly','tracked','no_retirement'])
def test_web_refuses_drift_or_false_completion(fixture,change):
    item=web(fixture);proof=item['checkpoint'];repo,run,_,_=fixture
    if change in ('manifest','receipt','consumer'):
        entry=proof['consumer_files'][0] if change=='consumer' else proof[change];Path(entry['path']).write_text('{}')
    elif change=='output':Path(item['files'][0]).write_text('bad!')
    elif change=='empty':item['files']=[]
    elif change=='wrongroot':proof['web_root']=str(repo)
    elif change in ('wrongcommit','assembly'):
        p=Path(proof['receipt']['path']);d=json.loads(p.read_text());d['source_commit' if change=='wrongcommit' else 'assembly']='wrong';dump(p,d);proof['receipt']=ref(p)
    elif change=='tracked':git(repo,'add','-f',item['files'][0])
    else:
        p=run/'selection-authorization.json';d=json.loads(p.read_text());d['retire_completed_outputs']=False;dump(p,d);item['run_authorization']=ref(p)
    assert not checkpoint.verify(item)[0],change


def test_apk_real_receipts_and_retained_files(fixture):
    item=apk(fixture);assert checkpoint.verify(item)[0],checkpoint.verify(item)
    deps=checkpoint.dependencies(item)
    assert item['checkpoint']['retained_apks'][0]['path'] in deps
    assert item['files'][0] not in deps


@pytest.mark.parametrize('mode', ['missing', 'wrong_type'])
def test_apk_legacy_optional_build_mode(fixture, mode):
    item = apk(fixture)
    entry = item['checkpoint']['packages'][0]
    path = Path(entry['receipt']['path'])
    receipt = json.loads(path.read_text())
    if mode == 'missing':
        receipt.pop('inspectable')
    else:
        receipt['inspectable'] = 'false'
    dump(path, receipt)
    entry['receipt'] = ref(path)
    ok, reason = checkpoint.verify(item)
    assert ok == (mode == 'missing'), reason


@pytest.mark.parametrize('change',['retained','missing','content','receipt_hash','size','commit','version','mapping','same_retained'])
def test_apk_negative(fixture,change):
    item=apk(fixture);proof=item['checkpoint'];entry=proof['packages'][0]
    if change=='retained':proof['retained_apks']=[]
    elif change=='missing':Path(proof['retained_apks'][0]['path']).unlink()
    elif change=='content':Path(entry['path']).write_text('bad')
    elif change=='receipt_hash':entry['receipt']['sha256']='0'*64
    elif change=='mapping':entry['path']=proof['retained_apks'][0]['path']
    elif change=='same_retained':proof['retained_apks']=[copy.deepcopy(entry)]
    else:
        p=Path(entry['receipt']['path']);d=json.loads(p.read_text());d[{'size':'sizeBytes','commit':'builtFrom','version':'versionCode'}[change]]=0;dump(p,d);entry['receipt']=ref(p)
    assert not checkpoint.verify(item)[0],change


def test_malformed_total():
    for item in (None,{}, {'checkpoint':{'packages':[None],'consumer_files':None}}, {'checkpoint':5}):
        assert not checkpoint.verify(item)[0]
        assert isinstance(checkpoint.dependencies(item),list)


def test_real_plan_checkpoint_allows_only_completed_phase_of_active_parent(fixture):
    import plan
    from test_plan_act import node
    item=web(fixture);repo,run,_,_=fixture
    ctx={'profile':{'roots':[str(repo)],'protect':[],'protect_globs':[]},
         'nodes':[node(Path(item['path']),recent_24h=1)],'reviews':[],'matched':set(),
         'legacy':False,'run':str(run),'cleanup_paths':[],'cleanup_files':set(item['files']),'soft':[]}
    errors,sealed=plan.check_item(item,ctx);assert not errors,errors
    active=[{'path':str(repo),'name':'parent','status':'active'}]
    assert plan.project_item_errors(item['path'],active,False)
    assert not plan.project_item_errors(item['path'],active,bool(sealed.get('checkpoint')))
    Path(item['checkpoint']['receipt']['path']).write_text('{}')
    errors,sealed=plan.check_item(item,ctx)
    assert errors and 'checkpoint' not in (sealed or {})


def test_evidence_changed_while_verifying_files_is_rejected(fixture,monkeypatch):
    item=web(fixture);receipt=Path(item['checkpoint']['receipt']['path'])
    original=auth.snapshot;target=item['files'][0];calls=0
    def changing(value):
        nonlocal calls
        result=original(value)
        if str(value)==target:
            calls+=1
            if calls==2: receipt.write_text('{}')  # after initial authorization hash, during manifest comparison
        return result
    monkeypatch.setattr(auth,'snapshot',changing)
    assert not checkpoint.verify(item)[0]
