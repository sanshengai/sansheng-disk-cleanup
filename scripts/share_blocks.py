#!/usr/bin/env python3
"""对未跟踪的旧媒体做 APFS 写时复制：保留路径、内容及受验证属性，文件 inode 会改变。"""
import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile

import act
import artifact_checkpoint as ac
import compact_review as cr
import protect

MEDIA = {'.mp4', '.mov', '.wav', '.mp3', '.m4a', '.png', '.jpg', '.jpeg', '.webp', '.otf', '.ttf'}


def properties(p):
    s = p.stat()
    return [cr.metadata_signature(p), s.st_mtime_ns, s.st_birthtime]


def eligible(p, profile):
    p = cr.safe(p, profile, old=True)
    blocked = {'.photoslibrary', '.fcpbundle', '.logicx', '.app', '.bundle', '.framework'}
    if any(q.suffix.lower() in blocked for q in p.parents):
        raise ValueError('不处理软件包或资源库')
    if any(q.name in {'CloudStorage', 'Mobile Documents', 'Fonts'} for q in p.parents):
        raise ValueError('不处理同步目录或安装字体')
    if p.stat().st_flags or p.stat().st_uid != os.getuid():
        raise ValueError('仅处理当前用户拥有且无特殊标记的文件')
    return p


def writable_target(p, profile):
    p = eligible(p, profile)
    if p.suffix.lower() not in MEDIA or p.stat().st_flags:
        raise ValueError('仅支持无特殊标记的普通媒体或字体')
    try:
        if act.list_tracked(str(p)):
            raise ValueError('保留 Git 跟踪文件')
    except act.NotRepo:
        pass
    return p


def make_entry(source, target, sha, profile, run):
    source = eligible(source, profile)
    target = writable_target(target, profile)
    if source == target or source.stat().st_dev != target.stat().st_dev:
        raise ValueError('需要同卷不同普通文件')
    identities = [ac.fingerprint(p) for p in (source, target)]
    for p in (source, target):
        cr.readable(p, run)
        if ac.sha256_file(p) != sha:
            raise ValueError('完整内容哈希不一致')
    if identities != [ac.fingerprint(p) for p in (source, target)]:
        raise ValueError('读取期间文件变化')
    return {'source':str(source), 'path':str(target), 'sha256':sha,
            'source_identity':identities[0], 'identity':identities[1],
            'properties':properties(target), 'bytes':target.stat().st_blocks*512}


def unopened(paths):
    result = subprocess.run(['lsof', '-n', '-P', '-F', 'n', '--', *map(str, paths)],
                            capture_output=True, text=True, timeout=20)
    if result.returncode not in (0, 1) or act.parse_lsof(result.stdout) or result.stderr.strip():
        raise ValueError('文件打开中或占用检查不完整')


class AttrList(ctypes.Structure):
    _fields_ = [('bitmapcount', ctypes.c_uint16), ('reserved', ctypes.c_uint16),
                ('commonattr', ctypes.c_uint32), ('volattr', ctypes.c_uint32),
                ('dirattr', ctypes.c_uint32), ('fileattr', ctypes.c_uint32),
                ('forkattr', ctypes.c_uint32)]


def copy_birthtime(lib, target, clone):
    # sys/attr.h: ATTR_CMN_CRTIME; getattrlist 返回 4 字节长度 + timespec。
    attrs = AttrList(5, 0, 0x200, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(24)
    if lib.getattrlist(os.fsencode(target), ctypes.byref(attrs), buf, 24, 0):
        raise OSError(ctypes.get_errno(), '读取创建时间失败')
    stamp = ctypes.create_string_buffer(buf.raw[4:20], 16)
    if lib.setattrlist(os.fsencode(clone), ctypes.byref(attrs), stamp, 16, 0):
        raise OSError(ctypes.get_errno(), '复制创建时间失败')


def apply_entry(e, profile):
    source = eligible(e['source'], profile)
    target = writable_target(e['path'], profile)
    if source == target or source.stat().st_dev != target.stat().st_dev:
        raise ValueError('路径或文件系统变化')
    for p, key in ((source, 'source_identity'), (target, 'identity')):
        if list(ac.fingerprint(p)) != list(e[key]) or ac.sha256_file(p) != e['sha256']:
            raise ValueError('封存后文件变化')
    if properties(target) != e['properties']:
        raise ValueError('元信息变化')
    unopened([source, target])
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    lib.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    lib.copyfile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32]
    free_before = act.free_bytes()
    # 同目录独占临时目录，目的路径原先不存在；clonefile 不允许回退到普通复制。
    with tempfile.TemporaryDirectory(prefix='.share-blocks-', dir=target.parent) as tmp:
        clone = Path(tmp) / 'clone'
        if lib.clonefile(os.fsencode(source), os.fsencode(clone), 0x0001 | 0x0008):
            raise OSError(ctypes.get_errno(), 'clonefile 失败，原文件未变')
        # 从目标恢复属性，不用来源的属性替代目标；不复制数据。
        if lib.copyfile(os.fsencode(target), os.fsencode(clone), None, 1 | 2 | 4):
            raise OSError(ctypes.get_errno(), '复制目标元信息失败')
        copy_birthtime(lib, target, clone)
        if ac.sha256_file(clone) != e['sha256'] or properties(clone) != e['properties']:
            raise ValueError('克隆内容或元信息不一致')
        unopened([source, target])
        if (list(ac.fingerprint(source)) != list(e['source_identity'])
                or list(ac.fingerprint(target)) != list(e['identity'])):
            raise ValueError('替换前文件变化')
        os.replace(clone, target)
    return {'path':str(target), 'source':str(source), 'status':'shared',
            'logical_bytes_unchanged':target.stat().st_size,
            'old_identity':e['identity'], 'new_identity':ac.fingerprint(target),
            'birthtime':target.stat().st_birthtime, 'parent_mtime_changed':True,
            'free_delta':act.free_bytes()-free_before, 'sha256':e['sha256']}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--duplicates', required=True); p.add_argument('--run', required=True)
    p.add_argument('--out', required=True)
    p = sub.add_parser('apply')
    p.add_argument('--plan', required=True); p.add_argument('--seal', required=True)
    p.add_argument('--reason', required=True); p.add_argument('--pilot', action='store_true')
    p.add_argument('--limit', type=int, default=0)
    args = ap.parse_args(); profile = protect.load_profile(protect.PROFILE)
    if args.command == 'plan':
        data = json.loads(Path(args.duplicates).read_text()); entries = []; skipped = []
        for group in data['groups']:
            candidates = []
            for row in group['items']:
                try:
                    p = eligible(row['path'], profile)
                    if p.suffix.lower() in MEDIA: candidates.append(p)
                except (OSError, ValueError): pass
            if len(candidates) < 2: continue
            # 固定最浅路径作为来源；该组来源永远不进入改写清单。
            candidates.sort(key=lambda p:(len(p.parts), str(p)))
            source = candidates[0]
            for target in candidates[1:]:
                try: entries.append(make_entry(source,target,group['sha256'],profile,args.run))
                except (OSError,ValueError,act.GitUnknown,subprocess.SubprocessError) as exc:
                    skipped.append({'path':str(target),'reason':str(exc)})
        entries.sort(key=lambda e:-e['bytes'])
        plan={'schema':'share-blocks/1','entries':entries,'skipped':skipped}
        Path(args.out).write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'entries':len(entries),'skipped':len(skipped),'seal':cr.seal(plan),
                          'upper_bound_bytes':sum(e['bytes']for e in entries)}))
    else:
        lock=open(str(args.plan)+'.lock','a')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        plan=json.loads(Path(args.plan).read_text())
        if args.limit < 0: raise ValueError('limit 不能为负数')
        if plan.get('schema')!='share-blocks/1' or cr.seal(plan)!=args.seal or not args.reason.strip():
            raise ValueError('清单摘要或授权缺失')
        receipt={'seal':args.seal,'authorization':args.reason,'free_before':act.free_bytes(),'entries':[]}
        receipt['free_after']=receipt['free_before']
        out=Path(args.plan).with_name('share-blocks-'+('pilot'if args.pilot else ('batch-'+str(args.limit) if args.limit else 'execution'))+'.json')
        out.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
        selected=plan['entries'][:1] if args.pilot else (plan['entries'][:args.limit] if args.limit else plan['entries'])
        receipt['planned_count']=len(selected)
        for e in selected:
            try: result=apply_entry(e,profile)
            except (OSError,ValueError,act.GitUnknown,subprocess.SubprocessError) as exc:
                result={'path':e['path'],'status':'skipped','reason':str(exc)}
            receipt['entries'].append(result)
            receipt['free_after']=act.free_bytes()
            out.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
            if len(receipt['entries']) % 100 == 0:
                print(json.dumps({'processed':len(receipt['entries']),'total':len(selected)}),flush=True)
            if len(receipt['entries']) >= 10 and sum(x['status']=='skipped' for x in receipt['entries']) > len(receipt['entries'])/2:
                receipt['stopped_reason']='超过一半条目复核失败，停止放量'
                out.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
                break
        print(json.dumps({'shared':sum(e['status']=='shared'for e in receipt['entries']),
                          'skipped':sum(e['status']=='skipped'for e in receipt['entries']),
                          'free_delta':receipt['free_after']-receipt['free_before'],'receipt':str(out)}))


if __name__=='__main__':main()
