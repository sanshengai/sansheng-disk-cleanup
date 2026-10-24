import copy
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import pytest
import artifact_checkpoint as ac
import share_blocks as sb

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="APFS native tests require macOS")


def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(sb.cr,'safe',lambda p,*a,**kw:ac.ordinary_path(str(p)))
    monkeypatch.setattr(sb.cr,'readable',lambda *a:None)
    monkeypatch.setattr(sb,'unopened',lambda *a:None)
    a,b=tmp_path/'source.mp4',tmp_path/'copy.mp4'
    data=os.urandom(10000);a.write_bytes(data);b.write_bytes(data)
    os.chmod(b,0o640);os.utime(b,(1000000000,1000000000))
    e=sb.make_entry(a,b,ac.sha256_file(a),{},tmp_path)
    return a,b,e,data


def test_clone_preserves_paths_content_metadata_and_independent_writes(tmp_path,monkeypatch):
    a,b,e,data=setup(tmp_path,monkeypatch)
    before=sb.properties(b);old_inode=b.stat().st_ino
    assert sb.apply_entry(e,{})['status']=='shared'
    assert a.exists() and b.exists() and a.read_bytes()==b.read_bytes()==data
    assert sb.properties(b)==before and b.stat().st_ino!=old_inode
    assert a.stat().st_ino!=b.stat().st_ino and b.stat().st_nlink==1
    b.write_bytes(b'new edit');assert a.read_bytes()==data


def test_mutations_fail_without_replacing_target(tmp_path,monkeypatch):
    a,b,e,data=setup(tmp_path,monkeypatch);inode=b.stat().st_ino
    bad=copy.deepcopy(e);bad['sha256']='0'*64
    with pytest.raises(ValueError):sb.apply_entry(bad,{})
    assert b.stat().st_ino==inode and b.read_bytes()==data
    a.write_bytes(b'changed')
    with pytest.raises(ValueError):sb.apply_entry(e,{})
    assert b.stat().st_ino==inode and b.read_bytes()==data


def test_protected_shapes_and_tracked_files_rejected(tmp_path,monkeypatch):
    a,b,e,data=setup(tmp_path,monkeypatch)
    with pytest.raises(ValueError):sb.make_entry(a,a,e['sha256'],{},tmp_path)
    monkeypatch.setattr(sb.act,'list_tracked',lambda p:[p])
    with pytest.raises(ValueError):sb.apply_entry(e,{})
    monkeypatch.setattr(sb.act,'list_tracked',lambda p:[])
    os.link(b,tmp_path/'hard.mp4')
    with pytest.raises(ValueError):sb.apply_entry(e,{})
    assert a.read_bytes()==b.read_bytes()==data


def test_open_or_unknown_never_replaces(tmp_path,monkeypatch):
    a,b,e,data=setup(tmp_path,monkeypatch);inode=b.stat().st_ino
    def blocked(*a):raise ValueError('open')
    monkeypatch.setattr(sb,'unopened',blocked)
    with pytest.raises(ValueError):sb.apply_entry(e,{})
    assert b.stat().st_ino==inode


def test_empty_plan_writes_zero_receipt(tmp_path,monkeypatch,capsys):
    import json
    plan={'schema':'share-blocks/1','entries':[],'skipped':[]}
    path=tmp_path/'plan.json';path.write_text(json.dumps(plan))
    monkeypatch.setattr(sb.protect,'load_profile',lambda *a:{})
    monkeypatch.setattr(sys,'argv',['share_blocks.py','apply','--plan',str(path),
                                  '--seal',sb.cr.seal(plan),'--reason','test'])
    sb.main()
    result=json.loads(capsys.readouterr().out)
    assert result['shared']==0 and result['free_delta']==0
    assert json.loads(Path(result['receipt']).read_text())['entries']==[]


def test_bundle_and_source_flags_rejected(tmp_path,monkeypatch):
    a,b,e,data=setup(tmp_path,monkeypatch)
    bundle=tmp_path/'asset.photoslibrary';bundle.mkdir()
    q=bundle/'copy.mp4';q.write_bytes(data)
    with pytest.raises(ValueError):sb.make_entry(a,q,e['sha256'],{},tmp_path)
    os.chflags(a,0x8000)
    try:
        with pytest.raises(ValueError):sb.make_entry(a,b,e['sha256'],{},tmp_path)
    finally:os.chflags(a,0)

def test_open_at_final_check_aborts(tmp_path,monkeypatch):
    a,b,e,data=setup(tmp_path,monkeypatch);inode=b.stat().st_ino
    checks=[]
    def check(*args):
        checks.append(1)
        if len(checks)==2:raise ValueError('opened during clone')
    monkeypatch.setattr(sb,'unopened',check)
    with pytest.raises(ValueError):sb.apply_entry(e,{})
    assert b.stat().st_ino==inode and b.read_bytes()==data


def test_real_different_creation_time_preserved(tmp_path,monkeypatch):
    import time
    a,b,e,data=setup(tmp_path,monkeypatch)
    # 原目标创建时间与来源、mtime 都不同，覆盖真实媒体命中的情况。
    b.unlink();time.sleep(0.02);b.write_bytes(data)
    os.utime(b,ns=(1792443069187848537,1792443069187848537))
    e=sb.make_entry(a,b,ac.sha256_file(a),{},tmp_path)
    before=sb.properties(b)
    assert sb.apply_entry(e,{})['status']=='shared'
    assert sb.properties(b)==before
