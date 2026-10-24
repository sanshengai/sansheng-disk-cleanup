#!/usr/bin/env python3
"""当前 UID 的 macOS Node 编译缓存窄入口；plan/check/run，不接受清理目标路径。"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

import protect
from compact_review import readable

SCHEMA = 'node-compile-cache/1'
PLAN = 'node-compile-cache-plan.json'
FAMILY = re.compile(r'v\d+\.\d+\.\d+-(?:arm64|x64)-[0-9a-f]{8}-\d+')
LEAF = re.compile(r'[0-9a-f]{8}')
AGE = 86400


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def discover_root():
    # No TMPDIR/HOME environment override and no CLI path override.
    raw = subprocess.check_output(['/usr/bin/getconf', 'DARWIN_USER_TEMP_DIR'], text=True).strip()
    temp = Path(raw).resolve(strict=True)
    if not re.fullmatch(r'/private/var/folders/[^/]{2}/[^/]+/T', str(temp)):
        raise ValueError('无法确认当前用户系统临时目录')
    if temp.stat().st_uid != os.getuid():
        raise ValueError('系统临时目录不属于当前 UID')
    root = temp / 'node-compile-cache'
    directory(root)
    return root


def directory(path):
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or path.resolve() != path or st.st_uid != os.getuid():
        raise ValueError('目录归属、链接或类型不合法')
    return [st.st_dev, st.st_ino, st.st_uid, st.st_mode]


def identity(st):
    return {'dev': st.st_dev, 'inode': st.st_ino, 'uid': st.st_uid,
            'mode': st.st_mode, 'links': st.st_nlink, 'size': st.st_size,
            'mtime_ns': st.st_mtime_ns, 'ctime_ns': st.st_ctime_ns,
            'allocated': st.st_blocks * 512, 'flags': getattr(st, 'st_flags', 0)}


def safe_path(path, root, profile):
    p = Path(path)
    if p.parent.parent != root or not FAMILY.fullmatch(p.parent.name) or not LEAF.fullmatch(p.name):
        raise ValueError('路径逃逸或不是已支持的 Node 编译缓存族')
    if not p.parent.name.endswith('-' + str(os.getuid())):
        raise ValueError('缓存族 UID 不匹配')
    directory(root)
    directory(p.parent)
    reason = protect.protected_reason(str(p), profile)
    if reason:
        raise ValueError(reason)
    st = p.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
        raise ValueError('非普通单链接本用户文件')
    if getattr(st, 'st_flags', 0):
        raise ValueError('特殊文件标记，保留')
    return p


def old(st):
    if max(st.st_mtime, st.st_ctime) >= time.time() - AGE:
        raise ValueError('mtime 或 ctime 在 24 小时内，保留')


def fd_sha(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    h = hashlib.sha256()
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            return h.hexdigest()
        h.update(block)


def fingerprint(path, root, profile, run):
    p = safe_path(path, root, profile)
    readable(p, run)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = identity(os.fstat(fd))
        old(os.fstat(fd))
        sha = fd_sha(fd)
        if identity(os.fstat(fd)) != before or identity(p.lstat()) != before:
            raise ValueError('读取期间文件变化')
        return {'path': str(p), **before, 'sha256': sha}
    finally:
        os.close(fd)


def inventory(root):
    """Metadata only, including rejected/unrecognized children; never follow links."""
    result = []
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            p = Path(parent) / name
            st = p.lstat()
            result.append({'path': str(p), **identity(st)})
    return sorted(result, key=lambda row: row['path'])


def opened(root):
    probe = subprocess.run(['/usr/sbin/lsof', '-nP', '-F', 'n', '+D', str(root)],
                           capture_output=True, text=True, timeout=120)
    if probe.returncode not in (0, 1) or probe.stderr.strip():
        raise ValueError('打开文件检查不完整')
    if probe.returncode == 1 and probe.stdout.strip():
        raise ValueError('打开文件检查返回矛盾')
    paths = {line[1:] for line in probe.stdout.splitlines() if line.startswith('n/')}
    if probe.returncode == 0 and not paths:
        raise ValueError('打开文件检查没有可解析的结果')
    return paths


def unopened_batch(paths):
    probe = subprocess.run(['/usr/sbin/lsof', '-nP', '-F', 'n', '--', *paths],
                           capture_output=True, text=True, timeout=60)
    if probe.returncode != 1 or probe.stdout.strip() or probe.stderr.strip():
        raise ValueError('本批文件被打开或占用检查不完整，保留')


def inventory_delta(plan, root, completed):
    expected = {e['path']: e for e in plan['inventory'] if e['path'] not in completed}
    actual = {e['path']: e for e in inventory(root)}
    return {'new_files_retained': sorted(set(actual) - set(expected)),
            'missing_unselected': sorted(set(expected) - set(actual)),
            'changed_unselected_retained': sorted(p for p in set(actual) & set(expected)
                                                  if actual[p] != expected[p] and not stat.S_ISDIR(actual[p]['mode']))}


def create_plan(run, method, profile):
    root = discover_root()
    before = inventory(root)
    busy = opened(root)
    entries, skipped = [], []
    for row in before:
        if stat.S_ISDIR(row['mode']):
            continue  # All version directories and parents are retained.
        try:
            if row['path'] in busy:
                raise ValueError('文件正在打开，保留')
            entries.append(fingerprint(row['path'], root, profile, run))
        except (ValueError, OSError) as exc:
            skipped.append({'path': row['path'], 'reason': str(exc)})
    return {'schema': SCHEMA, 'root': str(root), 'root_identity': directory(root),
            'uid': os.getuid(), 'method': method, 'created_at': time.time(),
            'profile_guard': protect.profile_guard(profile), 'inventory': before,
            'families': {str(Path(e['path']).parent): directory(Path(e['path']).parent) for e in entries},
            'entries': entries, 'skipped': skipped}


def validate(plan, seal, root, profile, method, completed=frozenset()):
    if plan.get('schema') != SCHEMA or not plan.get('entries'):
        raise ValueError('清单为空或 schema 不匹配')
    if digest(plan) != seal:
        raise ValueError('封存摘要不匹配')
    if plan.get('root') != str(root) or plan.get('root_identity') != directory(root):
        raise ValueError('系统发现的缓存根已变化')
    if plan.get('uid') != os.getuid() or plan.get('method') != method:
        raise ValueError('UID 或处置方式与封存不一致')
    if plan.get('profile_guard') != protect.profile_guard(profile):
        raise ValueError('保护配置已变化')
    for path, expected in plan.get('families', {}).items():
        if directory(Path(path)) != expected:
            raise ValueError('版本目录身份变化')
    paths = [entry['path'] for entry in plan['entries']]
    if len(paths) != len(set(paths)):
        raise ValueError('清单路径重复')
    for path in paths:
        if path not in completed:
            safe_path(path, root, profile)


def preflight(plan, seal, run, method, completed=frozenset()):
    root, profile = discover_root(), protect.load_profile()
    validate(plan, seal, root, profile, method, completed)
    busy = opened(root)
    for e in plan['entries']:
        if e['path'] in completed:
            continue
        if e['path'] in busy:
            raise ValueError('封存文件正在打开，拒绝执行')
        if fingerprint(e['path'], root, profile, run) != e:
            raise ValueError('封存文件内容或身份已变化')
    return root


def free(root):
    s = os.statvfs(root)
    return s.f_bavail * s.f_frsize


def dispose(entry, root, run, method, trash, profile, families):
    """Anchor the parent fd; never rmtree. Last-minute identity/hash check before unlink/rename."""
    p = safe_path(entry['path'], root, profile)
    if directory(p.parent) != families[str(p.parent)]:
        raise ValueError('版本目录身份变化')
    readable(p, run)
    parent = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        fd = os.open(p.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        st = os.fstat(fd)
        old(st)
        actual = {'path': str(p), **identity(st), 'sha256': fd_sha(fd)}
        if actual != entry or identity(os.stat(p.name, dir_fd=parent, follow_symlinks=False)) != identity(st):
            raise ValueError('处置前身份或内容变化，保留')
        if identity(os.fstat(fd)) != identity(st) or identity(p.lstat()) != identity(st):
            raise ValueError('处置前文件变化，保留')
        if method == 'direct':
            os.unlink(p.name, dir_fd=parent)
            return {'path': str(p), 'status': 'removed', 'allocated': entry['allocated']}
        target = trash / (p.parent.name + '-' + p.name)
        if target.exists() or target.is_symlink():
            raise ValueError('废纸篓目标已存在')
        os.rename(p.name, target, src_dir_fd=parent)
        return {'path': str(p), 'status': 'trashed', 'allocated': entry['allocated'], 'destination': str(target)}
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def execute(plan, seal, run, method, authorization, dry_run=False, pilot=False):
    if not authorization.strip():
        raise ValueError('缺少本批明确授权 --authorize（用户原话）')
    completed = set()
    pilot_path = run / 'node-compile-cache-pilot.json'
    if not dry_run and not pilot and not pilot_path.exists():
        raise ValueError('正式执行前必须完成同封存计划的试点 --pilot')
    if not dry_run and not pilot and pilot_path.exists():
        previous = json.loads(pilot_path.read_text())
        expected_status = 'removed' if method == 'direct' else 'trashed'
        if (previous.get('seal') != seal or previous.get('method') != method
                or previous.get('overall_status') != 'completed'
                or len(previous.get('entries', [])) != 1
                or previous['entries'][0].get('path') != plan['entries'][0]['path']
                or previous['entries'][0].get('status') != expected_status):
            raise ValueError('试点回执不完整或与本批封存不一致')
        completed.add(previous['entries'][0]['path'])
    root = preflight(plan, seal, run, method, completed)
    label = 'dry-run' if dry_run else 'pilot' if pilot else 'execution'
    out = run / ('node-compile-cache-' + label + '.json')
    if out.exists():
        raise ValueError('回执已存在；不覆盖，另建运行目录重做计划')
    if not dry_run:
        proof_path = run / 'node-compile-cache-dry-run.json'
        proof = json.loads(proof_path.read_text()) if proof_path.exists() else {}
        if proof.get('seal') != seal or proof.get('overall_status') != 'validated':
            raise ValueError('缺少同封存计划的成功预演')
    receipt = {'seal': seal, 'authorization': authorization, 'method': method,
               'dry_run': dry_run, 'pilot': pilot, 'started_at': time.time(),
               'free_before': free(root), 'entries': [], 'prior_pilot_paths': sorted(completed),
               'unselected_before': inventory_delta(plan, root, completed),
               'allocated_removed': 0, 'allocated_trashed': 0,
               'note': 'allocated 是条目名义占用；free_delta 是整卷同时活动下的实测变化，废纸篓不算释放。'}
    dump(out, receipt)
    trash = None
    if method == 'trash' and not dry_run:
        base = Path(protect.HOME) / '.Trash'
        directory(base)
        if base.stat().st_dev != root.stat().st_dev:
            raise ValueError('废纸篓非同卷，拒绝复制替代')
        trash = base / ('Node编译缓存-' + uuid.uuid4().hex)
        trash.mkdir(mode=0o700)
    selected = [e for e in plan['entries'] if e['path'] not in completed]
    if pilot:
        selected = selected[:1]
    removed_now = set()
    batch_profile = None
    # Append evidence instead of rewriting an ever-growing receipt per file (quadratic I/O).
    with out.with_suffix('.jsonl').open('x') as events:
        for index, entry in enumerate(selected):
            try:
                if not dry_run and index % 128 == 0:
                    batch_profile = protect.load_profile()
                    if protect.profile_guard(batch_profile) != plan['profile_guard']:
                        raise ValueError('保护配置已变化')
                    if discover_root() != root or directory(root) != plan['root_identity']:
                        raise ValueError('缓存根身份已变化')
                    unopened_batch([e['path'] for e in selected[index:index + 128]])
                result = ({'path': entry['path'], 'status': 'validated', 'allocated': entry['allocated']}
                          if dry_run else dispose(entry, root, run, method, trash, batch_profile, plan['families']))
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                result = {'path': entry['path'], 'status': 'stopped', 'reason': str(exc)}
            receipt['entries'].append(result)
            if result['status'] == 'removed':
                receipt['allocated_removed'] += result['allocated']
                removed_now.add(entry['path'])
            if result['status'] == 'trashed':
                receipt['allocated_trashed'] += result['allocated']
                removed_now.add(entry['path'])
            events.write(json.dumps(result, ensure_ascii=False) + '\n')
            events.flush()
            if index % 128 == 0 or result['status'] == 'stopped':
                os.fsync(events.fileno())
            if result['status'] == 'stopped':
                break
        os.fsync(events.fileno())
    receipt['free_after'] = free(root)
    receipt['free_delta'] = receipt['free_after'] - receipt['free_before']
    receipt['overall_status'] = ('stopped' if any(e['status'] == 'stopped' for e in receipt['entries'])
                                 else 'validated' if dry_run else 'completed')
    receipt['unselected_after'] = inventory_delta(plan, root, completed | removed_now)
    receipt['finished_at'] = time.time()
    dump(out, receipt)
    return receipt, out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action', choices=['plan', 'check', 'run'])
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--method', choices=['direct', 'trash'], required=True)
    ap.add_argument('--seal')
    ap.add_argument('--authorize', default='')
    group = ap.add_mutually_exclusive_group()
    group.add_argument('--dry-run', action='store_true')
    group.add_argument('--pilot', action='store_true')
    a = ap.parse_args(argv)
    if not a.run.is_dir() or a.run.resolve() != a.run or a.run.is_relative_to(discover_root()):
        raise ValueError('证据目录必须已存在、无链接且在缓存目录之外')
    with (a.run / 'node-compile-cache.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = a.run / PLAN
        if a.action == 'plan':
            if path.exists():
                raise ValueError('清单已存在；不覆盖')
            plan = create_plan(a.run, a.method, protect.load_profile())
            dump(path, plan)
            print(json.dumps({'plan': str(path), 'seal': digest(plan), 'entries': len(plan['entries']),
                              'skipped': len(plan['skipped']), 'allocated': sum(e['allocated'] for e in plan['entries'])}))
        else:
            plan = json.loads(path.read_text())
            if a.action == 'check':
                preflight(plan, a.seal, a.run, a.method)
                print(json.dumps({'status': 'validated', 'seal': a.seal}))
            else:
                receipt, out = execute(plan, a.seal, a.run, a.method, a.authorize, a.dry_run, a.pilot)
                print(json.dumps({'receipt': str(out), 'status': receipt['overall_status'],
                                  'allocated_removed': receipt['allocated_removed'], 'free_delta': receipt['free_delta']}))
                return 1 if receipt['overall_status'] == 'stopped' else 0
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'refused', 'reason': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
