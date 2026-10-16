#!/usr/bin/env python3
"""只永久删除本轮回执证明已移入废纸篓的单个文件；不清空整个废纸篓。"""
import argparse
import copy
import fcntl
import hashlib
import os
import stat
from contextlib import contextmanager

import act
import plan
import protect


@contextmanager
def opened(path):
    """逐层打开目录，不跟随任何符号链接；unlink 使用已打开的父目录。"""
    parts = path.split(os.sep)
    if not os.path.isabs(path) or any(p in ('.', '..') for p in parts):
        raise ValueError('路径必须是无跳转的绝对路径')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        filefd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            yield fd, parts[-1], filefd
        finally:
            os.close(filefd)
    finally:
        os.close(fd)


def fingerprint(fd):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError('只接受无硬链接的普通文件')
    os.lseek(fd, 0, os.SEEK_SET)
    h = hashlib.sha256()
    for chunk in iter(lambda: os.read(fd, 1 << 20), b''):
        h.update(chunk)
    after = os.fstat(fd)
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_blocks')
    if any(getattr(before, k) != getattr(after, k) for k in fields):
        raise ValueError('读取期间文件有变化')
    return {k: getattr(after, k) for k in fields} | {'sha256': h.hexdigest()}


def candidates(run, seal):
    import report
    _, receipts = report.load_receipts(run)
    out = {}
    for rc in receipts:
        if rc.get('dry_run') or rc.get('seal') != seal['digest'] or rc.get('scan_id') != seal['scan_id']:
            raise ValueError('回执与封存不一致')
        for card in rc.get('cards', []):
            sealed = {i['path']: i for i in seal['cards'].get(card['id'], {}).get('items', [])}
            for item in card.get('items', []):
                d = item.get('disposal') or {}
                if item.get('status') != 'removed' or d.get('mode') != 'trash':
                    continue
                src = item['path']
                original = sealed.get(src)
                if not original or original.get('scope') != 'all' or original.get('method') != 'delete':
                    raise ValueError('只接受封存中的完整单文件删除条目')
                root = d.get('dir', '')
                expected = '叁笙清理-' + seal['scan_id'] + '-'
                if os.path.dirname(root) != os.path.join(protect.HOME, '.Trash') or not os.path.basename(root).startswith(expected):
                    raise ValueError('不是本轮专用废纸篓目录')
                if os.path.commonpath([src, protect.HOME]) != protect.HOME or src == protect.HOME:
                    raise ValueError('源文件不在用户目录内')
                dest = os.path.join(root, os.path.relpath(src, protect.HOME))
                with opened(dest) as (_, _, fd):
                    fp = fingerprint(fd)
                if (fp['st_dev'], fp['st_ino']) != (original.get('dev'), original.get('ino')):
                    raise ValueError('废纸篓文件不是封存时的原文件')
                out[dest] = {'path': dest, 'source': src, 'card': card['id'], 'dir': root,
                             'bytes': int(item['removed']['bytes']), 'fingerprint': fp}
    if not out:
        raise ValueError('没有可核实的本轮废纸篓文件')
    for root in {i['dir'] for i in out.values()}:
        busy = act.open_files(root)
        if busy is None or busy:
            raise ValueError('本轮废纸篓目录存在占用，或无法确认占用状态')
    return sorted(out.values(), key=lambda x: x['path'])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', required=True)
    p.add_argument('--seal', required=True)
    p.add_argument('--execute', metavar='预演摘要')
    p.add_argument('--reason', help='用户同意永久删除的原话；执行时必填')
    a = p.parse_args(argv)
    run = os.path.abspath(a.run)
    with open(os.path.join(run, 'finalize-trash.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        seal, err = act.load_seal(run, a.seal)
        if err:
            raise ValueError(err)
        journal_path = os.path.join(run, 'finalize-trash.json')
        if os.path.exists(journal_path):
            raise ValueError('本轮已有永久删除回执，请先检查结果，不重复执行')
        items = candidates(run, seal)
        snapshot = {'schema': 'finalize-trash/1', 'scan_id': seal['scan_id'], 'seal': a.seal, 'items': items}
        digest = plan.digest_of(snapshot)
        draft = os.path.join(run, 'finalize-trash-dryrun.json')
        if not a.execute:
            snapshot['digest'] = digest
            plan.write_json(draft, snapshot)
            print(f"仅预演：{len(items)} 个文件，{sum(i['bytes'] for i in items)} bytes；摘要 {digest}")
            return 0
        if not a.reason or not a.reason.strip():
            raise ValueError('需要记录用户同意永久删除的原话')
        old = plan.read_json(draft)
        if a.execute != digest or old.get('digest') != digest or plan.digest_of(old) != digest:
            raise ValueError('文件或预演摘要有变化，拒绝执行；请重新预演')
        journal = copy.deepcopy(snapshot)
        journal.update({'reason': a.reason, 'started_at': plan.now_iso(), 'removed': [], 'bytes': 0,
                        'free_before': act.free_bytes(), 'finished_at': None})
        plan.write_json(journal_path, journal)
        for item in items:
            with opened(item['path']) as (parent, name, fd):
                fp = fingerprint(fd)
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if fp != item['fingerprint'] or (current.st_dev, current.st_ino) != (fp['st_dev'], fp['st_ino']):
                    raise ValueError('执行时文件已变化，停止；已完成部分见回执')
                os.unlink(name, dir_fd=parent)
            journal['removed'].append(item)
            journal['bytes'] += item['bytes']
            plan.write_json(journal_path, journal)
        journal.update({'finished_at': plan.now_iso(), 'free_after': act.free_bytes()})
        plan.write_json(journal_path, journal)
        import report
        report.make(run)
        print(f"永久删除完成：{len(items)} 个文件，按条目核算 {journal['bytes']} bytes；磁盘余量变化 {journal['free_after'] - journal['free_before']} bytes")
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        raise SystemExit('拒绝或停止：' + str(exc))
