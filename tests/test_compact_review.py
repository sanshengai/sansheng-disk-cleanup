import base64
import copy
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import compact_review as cr


def setup(tmp_path,monkeypatch):
    root=tmp_path.resolve();subprocess.run(['git','init','-q',str(root)],check=True)
    movie=root/'保留视频.mp4';movie.write_bytes(b'ftyp'+bytes(range(256))*20)
    page=root/'review.html'
    b64=base64.b64encode(movie.read_bytes())
    original=(b'<html><p>review notes</p><video id="a"></video>'
      b'<script type="application/octet-stream" id="movie-a">'+b64+b'</script>'
      b'<script type="application/octet-stream" id="movie-b">'+b64+b'</script>'
      b'<script>'+cr.PREFIX+b'const data=node.textContent.trim();}</script></html>')
    page.write_bytes(original)
    monkeypatch.setattr(cr,'safe',lambda p,profile,old=False:cr.ac.ordinary_path(str(p)))
    monkeypatch.setattr(cr,'readable',lambda *args:None)
    monkeypatch.setattr(cr.act,'open_files',lambda *args:[])
    return root,movie,page,original


def test_lossless_compact_and_restore(tmp_path,monkeypatch):
    root,movie,page,original=setup(tmp_path,monkeypatch)
    entry=cr.plan_page(page,{movie.stat().st_size:[movie]},root,{})
    cr.validate_restore(entry)
    assert b''.join(cr.restored_chunks(entry))==original
    assert entry['compacted_size']<entry['original_size']
    assert 'data-local-src="%E4%BF%9D' in entry['compacted']
    assert '<p>review notes</p>' in entry['compacted']
    cr.apply_entry(entry,{})
    assert page.read_bytes()==entry['compacted'].encode()
    cr.apply_entry(entry,{},restore=True)
    assert page.read_bytes()==original


def test_missing_changed_media_and_original_mutation(tmp_path,monkeypatch):
    root,movie,page,original=setup(tmp_path,monkeypatch)
    with pytest.raises(ValueError):cr.plan_page(page,{},root,{})
    entry=cr.plan_page(page,{movie.stat().st_size:[movie]},root,{})
    movie.write_bytes(b'changed')
    with pytest.raises(ValueError):cr.apply_entry(entry,{})
    assert page.read_bytes()==original
    movie.write_bytes(base64.b64decode(cr.BLOCK.search(original).group(2)))
    page.write_bytes(original+b'modified')
    with pytest.raises(ValueError):cr.apply_entry(entry,{})


def test_open_tracked_and_empty_pages_rejected(tmp_path,monkeypatch):
    root,movie,page,original=setup(tmp_path,monkeypatch)
    entry=cr.plan_page(page,{movie.stat().st_size:[movie]},root,{})
    monkeypatch.setattr(cr.act,'open_files',lambda *args:['busy'])
    with pytest.raises(ValueError):cr.apply_entry(entry,{})
    subprocess.run(['git','-C',str(root),'add','review.html'],check=True)
    with pytest.raises(ValueError):cr.plan_page(page,{movie.stat().st_size:[movie]},root,{})
    page2=root/'empty.html';page2.write_bytes(b'')
    with pytest.raises(ValueError):cr.plan_page(page2,{},root,{})


def test_manifest_mutation_cannot_pass_restore(tmp_path,monkeypatch):
    root,movie,page,original=setup(tmp_path,monkeypatch)
    entry=cr.plan_page(page,{movie.stat().st_size:[movie]},root,{})
    bad=copy.deepcopy(entry);bad['restore_parts'][0]='changed'
    with pytest.raises(ValueError):cr.validate_restore(bad)
    assert cr.seal({'entries':[entry]})!=cr.seal({'entries':[bad]})
