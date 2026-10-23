#!/usr/bin/env python3
"""将旧本地验收页中重复内嵌的视频改为同盘文件引用；可逐字节还原原页。"""
import argparse
import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from urllib.parse import quote

import act
import artifact_checkpoint as ac
import peek
import protect

BLOCK = re.compile(rb'<script type="application/octet-stream" id="(movie-[A-Za-z0-9_-]+)">([A-Za-z0-9+/=]+)</script>')
PREFIX = b"function loadMovie(id){const node=document.getElementById('movie-'+id);if(!node)return;"
ADDITION = b"if(node.dataset.localSrc){document.getElementById(id).src=node.dataset.localSrc;node.remove();return;}"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def readable(path, run):
    cls = peek.classify(str(path))
    mode = peek.decide(cls, peek.read_scope())
    peek.record(str(run), str(path), cls, mode)
    if mode != 'content':
        raise ValueError('读取范围未授权')


def metadata_signature(path):
    attrs = subprocess.run(['/usr/bin/xattr', '-lx', str(path)], capture_output=True,
                           check=True, timeout=10).stdout
    acl = subprocess.run(['/bin/ls', '-lde', str(path)], capture_output=True,
                         check=True, timeout=10).stdout
    acl_lines = b'\n'.join(line for line in acl.splitlines() if re.match(rb'^\s*\d+:', line))
    st = path.stat()
    return digest(attrs + acl_lines + repr((st.st_mode, st.st_uid, st.st_gid, getattr(st, 'st_flags', 0))).encode())


def safe(path, profile, old=False):
    p = ac.ordinary_path(str(path))
    reason = protect.protected_reason(str(p), profile)
    if reason or not protect.under_roots(str(p), profile):
        raise ValueError(reason or '路径不在授权范围')
    st = p.stat()
    if old and max(st.st_mtime, st.st_ctime) > time.time() - 86400:
        raise ValueError('24 小时内变化，保留')
    return p


def plan_page(path, media_by_size, run, profile, hash_cache=None):
    p = safe(path, profile, old=True)
    if p.suffix.lower() != '.html' or act.list_tracked(str(p)):
        raise ValueError('只处理未跟踪的本地 HTML')
    if getattr(p.stat(), 'st_flags', 0):
        raise ValueError('含特殊文件标记，保留')
    readable(p, run)
    before = ac.fingerprint(p)
    data = p.read_bytes()
    blocks = list(BLOCK.finditer(data))
    if not blocks or data.count(PREFIX) != 1 or ADDITION in data:
        raise ValueError('不是受支持的内嵌视频验收页')
    cache = {} if hash_cache is None else hash_cache
    parts, videos, compact = [], [], []
    pos = 0
    for block in blocks:
        payload = block.group(2)
        decoded = base64.b64decode(payload, validate=True)
        if base64.b64encode(decoded) != payload:
            raise ValueError('非规范 base64，不改写')
        sha = digest(decoded)
        matches = []
        for candidate in media_by_size.get(len(decoded), []):
            candidate = safe(candidate, profile)
            if p.parent != candidate.parent and p.parent not in candidate.parents:
                continue  # 仅同目录树内的现存媒体；不把页面绑到别的临时项目。
            readable(candidate, run)
            identity = ac.fingerprint(candidate)
            key = (str(candidate), identity)
            if key not in cache:
                cache[key] = ac.sha256_file(candidate)
            if ac.fingerprint(candidate) != identity:
                raise ValueError('媒体读取期间变化')
            if cache[key] == sha:
                matches.append(candidate)
        if not matches:
            raise ValueError('存在没有逐字节匹配本地 MP4 的内嵌视频')
        source = min(matches, key=lambda q: (len(q.parts), str(q)))
        url = quote(os.path.relpath(source, p.parent), safe='/')
        before_payload = data[pos:block.start(2)]
        parts.append(before_payload.decode('utf-8'))
        videos.append({'path': str(source), 'sha256': sha, 'size': len(decoded),
                       'id': block.group(1).decode(), 'url': url})
        compact.append(data[pos:block.start()])
        compact.append(b'<script type="application/octet-stream" id="' + block.group(1)
                       + b'" data-local-src="' + html.escape(url, quote=True).encode() + b'"></script>')
        pos = block.end()
        # Restore shell retains closing tag separately, unlike compact replacement.
        parts[-1] = data[(0 if len(parts) == 1 else blocks[len(parts)-2].end(2)):block.start(2)].decode('utf-8')
    parts.append(data[blocks[-1].end(2):].decode('utf-8'))
    compact.append(data[pos:])
    new = b''.join(compact).replace(PREFIX, PREFIX + ADDITION, 1)
    if ac.fingerprint(p) != before:
        raise ValueError('页面读取期间变化')
    return {'path': str(p), 'identity': before, 'original_sha256': digest(data),
            'original_size': len(data), 'metadata_sha256': metadata_signature(p), 'compacted_sha256': digest(new),
            'compacted_size': len(new), 'compacted': new.decode('utf-8'),
            'restore_parts': parts, 'media': videos}


def restored_chunks(entry):
    parts = entry['restore_parts']
    if len(parts) != len(entry['media']) + 1:
        raise ValueError('恢复壳不完整')
    for i, media in enumerate(entry['media']):
        p = ac.ordinary_path(media['path'])
        before = ac.fingerprint(p)
        data = p.read_bytes()
        if digest(data) != media['sha256'] or len(data) != media['size'] or ac.fingerprint(p) != before:
            raise ValueError('保留 MP4 变化，停止')
        yield parts[i].encode('utf-8')
        yield base64.b64encode(data)
    yield parts[-1].encode('utf-8')


def validate_restore(entry):
    h = hashlib.sha256(); size = 0
    for chunk in restored_chunks(entry):
        h.update(chunk); size += len(chunk)
    if h.hexdigest() != entry['original_sha256'] or size != entry['original_size']:
        raise ValueError('恢复演练不能逐字节还原原页')


def apply_entry(entry, profile, restore=False):
    p = safe(entry['path'], profile, old=not restore)
    if act.list_tracked(str(p)) or metadata_signature(p) != entry['metadata_sha256']:
        raise ValueError('已被跟踪或文件属性变化')
    before = ac.fingerprint(p)
    expected = entry['compacted_sha256'] if restore else entry['original_sha256']
    if (not restore and list(before) != list(entry['identity'])) or ac.sha256_file(p) != expected:
        raise ValueError('页面封存后变化')
    if act.open_files(str(p)) != []:
        raise ValueError('页面打开中或不能确认，保留')
    validate_restore(entry)
    data = entry['compacted'].encode('utf-8')
    if digest(data) != entry['compacted_sha256']:
        raise ValueError('精简页面指纹不符')
    old_blocks = p.stat().st_blocks * 512
    fd, name = tempfile.mkstemp(prefix='.review-compact-', dir=p.parent)
    try:
        os.close(fd)
        subprocess.run(['/usr/bin/ditto', '--rsrc', '--extattr', '--acl', '--qtn', str(p), name],
                       check=True, capture_output=True, timeout=60)
        with open(name, 'wb') as out:
            if restore:
                for chunk in restored_chunks(entry): out.write(chunk)
            else:
                out.write(data)
            out.flush(); os.fsync(out.fileno())
        if metadata_signature(Path(name)) != entry['metadata_sha256']:
            raise ValueError('文件元信息未完整保留')
        if ac.fingerprint(p) != before:
            raise ValueError('写入前页面变化')
        os.replace(name, p)
    finally:
        if os.path.exists(name): os.unlink(name)
    return {'path': str(p), 'before_bytes': old_blocks,
            'after_bytes': p.stat().st_blocks * 512, 'status': 'restored' if restore else 'compacted'}


def seal(plan):
    return digest(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan'); p.add_argument('--root', required=True); p.add_argument('--run', required=True); p.add_argument('--out', required=True)
    p = sub.add_parser('apply'); p.add_argument('--plan', required=True); p.add_argument('--seal', required=True); p.add_argument('--reason', required=True); p.add_argument('--pilot', action='store_true'); p.add_argument('--restore', action='store_true')
    args = ap.parse_args(); profile = protect.load_profile(protect.PROFILE)
    if args.command == 'plan':
        root = Path(args.root)
        if not root.is_absolute() or root.resolve() != root: raise ValueError('要求真实绝对目录')
        media = {}; pages = []; errors = []; entries = []; cache = {}
        for dp, ds, fs in os.walk(root, followlinks=False):
            ds[:] = [d for d in ds if not Path(dp,d).is_symlink() and d not in ('.git','node_modules')]
            for f in fs:
                q = Path(dp,f)
                if q.is_symlink(): continue
                if q.suffix.lower() == '.mp4': media.setdefault(q.stat().st_size, []).append(q)
                elif q.suffix.lower() == '.html' and q.stat().st_size > 10*1024*1024: pages.append(q)
        for q in pages:
            try:
                entry = plan_page(q, media, args.run, profile, cache); validate_restore(entry); entries.append(entry)
            except (OSError, ValueError, act.GitUnknown, subprocess.SubprocessError) as exc: errors.append({'path':str(q),'reason':str(exc)})
        plan = {'schema':'compact-review/1','entries':entries,'skipped':errors}
        Path(args.out).write_text(json.dumps(plan, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps({'pages':len(entries),'skipped':len(errors),'logical_savings':sum(e['original_size']-e['compacted_size'] for e in entries),'seal':seal(plan),'plan':args.out}))
    else:
        plan = json.loads(Path(args.plan).read_text())
        if plan.get('schema') != 'compact-review/1' or seal(plan) != args.seal: raise ValueError('清单封存摘要不符')
        if not args.reason.strip(): raise ValueError('需要本轮用户授权依据')
        entries = plan['entries'][:1] if args.pilot else plan['entries']
        receipts = []
        for entry in entries:
            try: receipts.append(apply_entry(entry, profile, args.restore))
            except (OSError, ValueError, act.GitUnknown, subprocess.SubprocessError) as exc: receipts.append({'path':entry['path'],'status':'skipped','reason':str(exc)})
        out = {'authorization':args.reason,'seal':args.seal,'pilot':args.pilot,'restore':args.restore,'entries':receipts}
        dest = Path(args.plan).with_name('compact-'+('restore' if args.restore else 'pilot' if args.pilot else 'execution')+'.json')
        dest.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'compacted':sum(e['status']=='compacted' for e in receipts),'skipped':sum(e['status']=='skipped' for e in receipts),'allocated_savings':sum(e.get('before_bytes',0)-e.get('after_bytes',0) for e in receipts),'receipt':str(dest)}))


if __name__ == '__main__':
    main()
