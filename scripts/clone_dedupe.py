#!/usr/bin/env python3
"""重复副本 → APFS 克隆（原地替换，不删任何路径）。

为什么有它（2026-11-03 第三轮对比实验）：同一批成片在 成品库 / 交付 / 分发暂存 / 工作树 各存一份，
≥20MB 的重复副本合计 42G，Skill 从未检测。删副本会破坏交付目录和发布流水线的路径；改成 APFS 克隆后
每个路径照常可读可写，磁盘只占一份（任一副本被改写时 APFS 自动分离，互不影响）。

  python3 scripts/clone_dedupe.py plan --root ~/Projects [--root …] --out plan.json [--min-mb 20]
  python3 scripts/clone_dedupe.py run  --plan plan.json --receipt receipt.jsonl [--dry-run]

plan：按大小分组 → 全量 sha256 → 每组按优先级定保留端（见 keeper_rank），其余为待克隆副本。只读。
run ：逐个复核（保留端与副本的 inode/大小/mtime 与计划一致、未被进程打开、6 小时内未改动、不在保护名单），
      cp -c -p 到同目录临时名 → 校验大小 → 恢复权限与时间 → 原子 rename 覆盖。已共享 inode 的跳过。
      名义重复量 ≠ 实际释放：已经是克隆的副本再克隆不腾空间，回执与最终报告以磁盘实测为准。
"""
import argparse, collections, hashlib, json, os, re, subprocess, sys, time

SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__"}
# 不处理：同步/系统库目录（改 inode 会让同步客户端重传或损坏库）
UNSAFE_RX = re.compile(r"/Library/|\.photoslibrary/|\.app/|/Mobile Documents/|/CloudStorage/|\.sparsebundle/")
# 保留端优先级：越靠前越该留（成品库 > 一般项目目录 > 交付/分发暂存 > 工作树/临时）
LOW_RX = [re.compile(r) for r in (r"/_workspace/worktrees/|/worktrees/", r"/(tmp|temp|cache|_workspace)/", r"publish|distribut|分发|-xhs-|-bilibili-|-youtube-",
                                  r"交付|deliver")]
HIGH_RX = re.compile(r"成品库|/final/|/成品/")


def keeper_rank(p):
    """数值越小越优先保留：成品库(0) < 一般目录(1) < 交付(2) < 分发暂存(3) < 临时/_workspace(4) < 工作树(5)；同档取路径短的。
    落在“低优先”位置的副本即使名字含“成品库”也不当保留端（工作树里的成品库副本不该胜过主仓那份）。"""
    low = next((i for i, rx in enumerate(LOW_RX) if rx.search(p)), None)
    tier = (2 + len(LOW_RX) - 1 - low) if low is not None else (0 if HIGH_RX.search(p) else 1)
    return (tier, len(p), p)


def sha256(p, bufsize=8 << 20):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(bufsize), b""):
            h.update(b)
    return h.hexdigest()


def collect(roots, min_bytes):
    by = collections.defaultdict(list)
    for root in roots:
        for d, ds, fs in os.walk(root):
            ds[:] = [x for x in ds if x not in SKIP_DIRS and not os.path.islink(os.path.join(d, x))]
            for f in fs:
                p = os.path.join(d, f)
                if UNSAFE_RX.search(p):
                    continue
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not os.path.isfile(p) or os.path.islink(p) or st.st_size < min_bytes:
                    continue
                by[st.st_size].append(p)
    return by


def ident(p):
    st = os.stat(p)
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]


def plan(roots, min_bytes, out):
    groups = []
    for size, ps in sorted(collect(roots, min_bytes).items(), key=lambda x: -x[0]):
        if len(ps) < 2:
            continue
        byh = collections.defaultdict(list)
        for p in ps:
            try:
                byh[sha256(p)].append(p)
            except OSError:
                pass
        for h, grp in byh.items():
            if len(grp) < 2:
                continue
            grp.sort(key=keeper_rank)
            keep = grp[0]
            kid = ident(keep)
            dups = [{"path": q, "ident": ident(q)} for q in grp[1:] if ident(q)[1] != kid[1]]
            if dups:
                groups.append({"sha256": h, "size": size, "keep": keep, "keep_ident": kid, "dups": dups})
    nominal = sum(g["size"] * len(g["dups"]) for g in groups)
    doc = {"schema": "clone-dedupe/1", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "roots": roots,
           "min_bytes": min_bytes, "groups": groups, "nominal_bytes": nominal}
    json.dump(doc, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return doc


def open_paths():
    r = subprocess.run(["lsof", "-nP", "-w", "-Fn"], capture_output=True, text=True, timeout=120)
    return {l[1:] for l in r.stdout.splitlines() if l.startswith("n/")}


def protected(p):
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import protect
        return protect.protected_reason(p, protect.load_profile())
    except Exception:  # noqa: BLE001
        return None


def clone_replace(keep, dup):
    st = os.stat(dup)
    tmp = dup + ".clone-dedupe-tmp"
    r = subprocess.run(["/bin/cp", "-c", "-p", keep, tmp], capture_output=True, text=True)
    if r.returncode:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise OSError("cp -c 失败（非 APFS 或跨卷？）：" + r.stderr.strip()[:160])
    if os.path.getsize(tmp) != st.st_size:
        os.unlink(tmp)
        raise OSError("克隆后大小不符")
    os.chmod(tmp, st.st_mode & 0o7777)
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, dup)


def run(plan_file, receipt, dry=False, quiet_hours=6):
    doc = json.load(open(plan_file, encoding="utf-8"))
    opened = open_paths()
    now = time.time()
    done = skipped = 0
    nominal = 0
    with open(receipt, "a", encoding="utf-8") as rf:
        for g in doc["groups"]:
            keep = g["keep"]
            try:
                if ident(keep) != g["keep_ident"]:
                    raise ValueError("保留端在计划后有变化")
            except (OSError, ValueError) as e:
                for d in g["dups"]:
                    rf.write(json.dumps({"path": d["path"], "status": "skipped", "why": str(e)}, ensure_ascii=False) + "\n")
                    skipped += 1
                continue
            for d in g["dups"]:
                p, why = d["path"], None
                try:
                    cur = ident(p)
                except OSError as e:
                    cur, why = None, "副本已不存在：%s" % e
                if not why and cur != d["ident"]:
                    why = "副本在计划后有变化"
                elif not why and p in opened:
                    why = "被进程打开"
                elif not why and now - cur[3] / 1e9 < quiet_hours * 3600:
                    why = "%g 小时内改过" % quiet_hours
                elif not why and protected(p):
                    why = "保护名单：%s" % protected(p)
                if why:
                    skipped += 1
                    rf.write(json.dumps({"path": p, "status": "skipped", "why": why}, ensure_ascii=False) + "\n")
                    continue
                if not dry:
                    try:
                        clone_replace(keep, p)
                    except OSError as e:
                        skipped += 1
                        rf.write(json.dumps({"path": p, "status": "failed", "why": str(e)}, ensure_ascii=False) + "\n")
                        continue
                done += 1
                nominal += g["size"]
                rf.write(json.dumps({"path": p, "keep": keep, "bytes": g["size"], "sha256": g["sha256"],
                                     "status": "dry-run" if dry else "cloned"}, ensure_ascii=False) + "\n")
    return {"cloned": done, "skipped": skipped, "nominal_bytes": nominal}


def free_bytes(path="/"):
    s = os.statvfs(path)
    return s.f_bavail * s.f_frsize


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("plan")
    a.add_argument("--root", action="append", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--min-mb", type=float, default=20)
    b = sp.add_parser("run")
    b.add_argument("--plan", required=True)
    b.add_argument("--receipt", required=True)
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--quiet-hours", type=float, default=6)
    x = ap.parse_args()
    if x.cmd == "plan":
        roots = [os.path.realpath(os.path.expanduser(r)) for r in x.root]
        doc = plan(roots, int(x.min_mb * 2**20), x.out)
        print("计划 %s：%d 组，名义可共享 %.2f GiB（已是克隆的不会再腾空间，以执行后磁盘实测为准）" % (
            x.out, len(doc["groups"]), doc["nominal_bytes"] / 2**30))
    else:
        before = free_bytes()
        r = run(x.plan, x.receipt, x.dry_run, x.quiet_hours)
        after = free_bytes()
        print("%s %d 个副本（名义 %.2f GiB），跳过/失败 %d；磁盘可用 %+.2f GiB（实测；并发写盘会抵消）" % (
            "预演" if x.dry_run else "已克隆", r["cloned"], r["nominal_bytes"] / 2**30, r["skipped"], (after - before) / 2**30))


if __name__ == "__main__":
    main()
