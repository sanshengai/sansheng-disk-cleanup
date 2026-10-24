#!/usr/bin/env python3
"""封存并复核已退出旧版本 Chrome 的启动克隆，只调用 Chrome 原生清理助手。"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import time

import peek
import protect

APP = Path('/Applications/Google Chrome.app')
BUNDLE = 'Google Chrome.app.bundle'
REQ = '=anchor apple generic and identifier "com.google.Chrome" and certificate leaf[subject.OU] = "EQHXZ8M8AV"'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_plist(p, run):
    cls = peek.classify(str(p))
    if peek.decide(cls, peek.read_scope(), False) != 'content':
        raise ValueError('读取授权不足')
    peek.record(str(run), str(p), cls, 'content')
    with p.open('rb') as f:
        return plistlib.load(f)


def verify_signature(bundle):
    result = subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '-R', REQ, str(bundle)],
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError('Google Chrome 签名验证失败：' + result.stderr[:200])


def version(bundle, run):
    d = read_plist(bundle / 'Contents/Info.plist', run)
    if d.get('CFBundleIdentifier') != 'com.google.Chrome':
        raise ValueError('不是 Chrome 应用包')
    v = d.get('CFBundleShortVersionString', '')
    if not v or not all(x.isdigit() for x in v.split('.')) or len(v.split('.')) != 4:
        raise ValueError('版本不可确认')
    return v


def unopened(p):
    # +D 按整个目录内的文件 inode 查找，包含主程序的硬链接；不以 ps 路径代替。
    a = subprocess.run(['/usr/sbin/lsof', '-nP', '+D', str(p)], capture_output=True, text=True, timeout=120)
    if a.returncode != 1 or a.stdout.strip() or a.stderr.strip():
        raise ValueError('仍有打开文件或占用检查不完整')


def inventory(p, profile):
    rows = []
    for parent, dirs, files in os.walk(p, followlinks=False):
        for name in dirs + files:
            q = Path(parent) / name
            s = q.lstat()
            if s.st_uid != os.getuid() or protect.protected_reason(str(q), profile):
                raise ValueError('子项归属或保护检查拒绝')
            if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode) or stat.S_ISLNK(s.st_mode)):
                raise ValueError('包含特殊文件')
            link = os.readlink(q) if q.is_symlink() else None
            if link is not None and not q.resolve().is_relative_to(p):
                raise ValueError('应用包链接越界')
            rows.append([str(q.relative_to(p)), s.st_dev, s.st_ino, s.st_mode,
                         s.st_size, s.st_mtime_ns, link])
    return digest(sorted(rows)), len(rows)


def entry(p, run, profile):
    p = protect.chrome_clone_path(p, profile)
    if sorted(x.name for x in p.iterdir()) != [BUNDLE] or (p / BUNDLE).is_symlink():
        raise ValueError('克隆目录包含额外内容或链接')
    if time.time() - p.stat().st_birthtime < 86400:
        raise ValueError('创建不足一天')
    current = version(APP, run)
    old = version(p / BUNDLE, run)
    if tuple(map(int, old.split('.'))) >= tuple(map(int, current.split('.'))):
        raise ValueError('保留当前或更新版本')
    before, count = inventory(p, profile)
    verify_signature(p / BUNDLE)
    unopened(p)
    after, _ = inventory(p, profile)
    if before != after:
        raise ValueError('核验期间目录变化')
    st = p.stat()
    return {'path': str(p), 'version': old, 'installed_version': current,
            'identity': [st.st_dev, st.st_ino, st.st_mtime_ns], 'inventory': after, 'files': count}


def helper(run):
    verify_signature(APP)
    v = version(APP, run)
    p = APP / f'Contents/Frameworks/Google Chrome Framework.framework/Versions/{v}/Helpers/Google Chrome Helper.app/Contents/MacOS/Google Chrome Helper'
    if not p.is_file() or not p.resolve().is_relative_to(APP):
        raise ValueError('无法确认原生清理助手')
    return p


def native_cleanup(p, exe):
    # 启动者退出后助手自然变成孤儿进程；不使用跳过等待的测试参数。
    code = 'import subprocess,sys; p=subprocess.Popen(sys.argv[1:],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True); print(p.pid)'
    a = subprocess.run([sys.executable, '-c', code, str(exe), '--type=code-sign-clone-cleanup',
                        '--unique-temp-dir-suffix=' + p.name.split('.', 1)[1]],
                       capture_output=True, text=True, check=True, timeout=10)
    pid = int(a.stdout.strip())
    deadline = time.monotonic() + 20
    while p.exists() and time.monotonic() < deadline:
        time.sleep(.2)
    if p.exists():
        raise ValueError(f'原生助手未完成，停止；PID={pid}')
    return pid


def free():
    s = os.statvfs('/Users')
    return s.f_bavail * s.f_frsize


def dump(p, d):
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n')


def check_plan(plan, seal, reason, profile):
    if plan.get('schema') != 'chrome-clones/1' or not plan.get('entries'):
        raise ValueError('清单为空或 schema 不匹配')
    if digest(plan) != seal or not reason.strip():
        raise ValueError('封存摘要或既有授权缺失')
    if plan.get('profile_guard') != protect.profile_guard(profile):
        raise ValueError('保护配置已变化')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action', choices=['plan', 'apply'])
    ap.add_argument('--run', required=True, type=Path)
    ap.add_argument('--seal')
    ap.add_argument('--reason', default='')
    ap.add_argument('--pilot', action='store_true')
    a = ap.parse_args()
    if not a.run.is_dir():
        raise ValueError('运行目录不存在')
    profile = protect.load_profile()
    plan_path = a.run / 'chrome-clones-plan.json'
    if a.action == 'plan':
        entries, skipped = [], []
        root = protect.chrome_clone_root()
        for p in sorted(root.iterdir()):
            try:
                entries.append(entry(p, a.run, profile))
            except (ValueError, OSError, subprocess.SubprocessError) as e:
                skipped.append({'path': str(p), 'reason': str(e)})
        plan = {'schema': 'chrome-clones/1', 'profile_guard': protect.profile_guard(profile),
                'entries': entries, 'skipped': skipped}
        dump(plan_path, plan)
        print(json.dumps({'entries': len(entries), 'skipped': len(skipped), 'seal': digest(plan)}))
        return
    with (a.run / 'chrome-clones.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = json.loads(plan_path.read_text())
        check_plan(plan, a.seal, a.reason, profile)
        exe = helper(a.run)
        receipt = {'seal': a.seal, 'authorization': a.reason, 'free_before': free(), 'entries': []}
        out = a.run / ('chrome-clones-pilot.json' if a.pilot else 'chrome-clones-execution.json')
        if out.exists():
            raise ValueError('已有回执，不覆盖；另建运行目录重做清单')
        dump(out, receipt)
        selected = plan['entries'][:1] if a.pilot else plan['entries']
        for e in selected:
            p = Path(e['path'])
            try:
                if not p.exists():
                    result = {'path': str(p), 'status': 'already_absent'}
                else:
                    if entry(p, a.run, protect.load_profile()) != e:
                        raise ValueError('封存后目录或版本变化')
                    result = {'path': str(p), 'status': 'removed', 'helper_pid': native_cleanup(p, exe)}
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                result = {'path': str(p), 'status': 'stopped', 'reason': str(exc)}
            receipt['entries'].append(result)
            receipt['free_after'] = free()
            dump(out, receipt)
            if result['status'] == 'stopped':
                break
        counts = {status: sum(x['status'] == status for x in receipt['entries'])
                  for status in ('removed', 'already_absent', 'stopped')}
        receipt['overall_status'] = 'stopped' if counts['stopped'] else 'completed'
        dump(out, receipt)
        print(json.dumps({'receipt': str(out), 'overall_status': receipt['overall_status'], **counts,
                          'free_delta': receipt.get('free_after', receipt['free_before']) - receipt['free_before']}))
        if counts['stopped']:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
