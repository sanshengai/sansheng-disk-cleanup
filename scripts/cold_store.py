#!/usr/bin/env python3
"""冷存出口：把“暂时不用、但要留着”的大目录交给本机配置的冷存工具（网盘、NAS 等）搬走，用时按原路径取回。

清理 Skill 只负责判断“哪些目录冷”；搬运、校验、登记、取回由冷存工具负责。没有配置冷存工具时，
candidates 照常给出候选，offload / where / restore 直接说明未配置。

  cold_store.py candidates [--root DIR | --from-triage triage.json] [--min-idle-days 14] [--min-gib 1] [--json OUT]
  cold_store.py offload <目录> --user-text '<用户原话>' [--min-idle-days 14]
  cold_store.py where <路径>
  cold_store.py restore <路径>

本机配置 triage-local.json 的 cold_storage（示例见 references/triage-local.example.json）：
  "cold_storage": {"label": "网盘冷存", "cli": ["python3", "~/tools/cold.py"], "placeholder": "已移到网盘.md"}
冷存工具须提供：<cli> cold offload <目录> --user-text T --min-idle-days N（上传、逐个核对后经
quick_act.py 复核删除，原目录留 placeholder 说明）、<cli> where <路径>（不在冷存里退出码 1）、<cli> restore <路径>。
"""
import argparse, json, os, re, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cold_consumers import ConsumerChecks
from cold_review import ReviewRoutes

try:
    import protect  # noqa: E402
    import triage   # noqa: E402
except Exception:  # noqa: BLE001
    protect = None
    triage = None

HOME = protect.HOME if protect else os.path.expanduser("~")
SKIP_DIRS = {".git", "node_modules", ".pnpm-store", "__pycache__", ".venv", "venv"}
GIB = 1 << 30


def _under(path, base):
    if not path or not base:
        return False
    p = os.path.realpath(os.path.abspath(os.path.expanduser(path))).rstrip("/")
    b = os.path.realpath(os.path.abspath(os.path.expanduser(base))).rstrip("/")
    return p == b or p.startswith(b + "/")


def _allow_cold_candidate(abs_p, work_root=None, profile=None):
    """排除 /opt/homebrew 等工作区与家目录外路径、受 protect.py 保护的路径、Containers、netdisk/cold 与在用 Android 镜像。"""
    home = protect.HOME if protect else HOME
    in_home = _under(abs_p, home)
    in_work = bool(work_root) and _under(abs_p, work_root)
    if not in_home and not in_work:
        return False
    if triage and any(token and token in abs_p for token in triage.HARD_KEEP):
        return False
    if in_home:
        if protect:
            prof = profile if profile is not None else protect.load_profile()
            if protect.protected_reason(abs_p, prof):
                return False
        for excluded in (
            os.path.join(home, "Library", "Containers"),
            os.path.join(home, ".sansheng", "netdisk", "cold"),
        ):
            if _under(abs_p, excluded) or _under(excluded, abs_p):
                return False
        if triage:
            for img in triage.active_android_system_images(home):
                if _under(abs_p, img) or _under(img, abs_p):
                    return False
    return True


def load_config():
    path = os.environ.get("SANSHENG_DISK_CLEANUP_TRIAGE") or os.path.join(HOME, ".sansheng/disk-cleanup/triage-local.json")
    try:
        cfg = json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    return cfg


def cold_cfg(cfg):
    c = cfg.get("cold_storage") or {}
    if c.get("cli"):
        c = dict(c, cli=[os.path.expanduser(x) for x in c["cli"]])
    return c


def tracked_files(root):
    """root 是 Git 仓库时返回已跟踪文件的相对路径集合（这些文件冷存时留在原处，不算可腾出体积）。"""
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if out.returncode != 0:
        return set()
    return set(x.decode("utf-8", "surrogateescape") for x in out.stdout.split(b"\0") if x)


def scan(root, placeholder=None, max_depth=6):
    """逐目录汇总：可搬走的字节（排除 Git 已跟踪）、文件数、最新修改时间。返回 {相对目录: [bytes, files, newest]}。"""
    tracked = tracked_files(root)
    agg = {}
    for dp, dns, fns in os.walk(root):
        rel_d = os.path.relpath(dp, root)
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not (rel_d == "_workspace" and d == "worktrees")]
        if placeholder and placeholder in fns:
            pass   # 已冷存目录里的残余（Git 跟踪文件等）照常统计，但不会再满足体积门槛
        parts = [] if rel_d == "." else rel_d.split(os.sep)
        for f in fns:
            p = os.path.join(dp, f)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not os.path.isfile(p) or os.path.islink(p):
                continue
            rel = os.path.relpath(p, root)
            size = 0 if rel in tracked else st.st_size
            for i in range(0, min(len(parts), max_depth) + 1):
                k = os.sep.join(parts[:i])            # "" 是 root 本身
                a = agg.setdefault(k, [0, 0, 0.0])
                a[0] += size; a[1] += 1; a[2] = max(a[2], st.st_mtime)
    return agg


def depth(k):
    return 0 if k == "" else k.count(os.sep) + 1


def pick(agg, min_idle_days, min_bytes, now=None, min_depth=2):
    """选“整棵子树都冷”的最高一层目录；depth 小于 min_depth 的不选（工作区根下默认不选第一层）；下层已被选中的上层不再重复。"""
    now = now or time.time()
    cold = {k: v for k, v in agg.items()
            if depth(k) >= min_depth and v[0] >= min_bytes and (now - v[2]) / 86400 >= min_idle_days}
    out = []
    for k in sorted(cold, key=lambda x: (depth(x), x)):
        if any(o == "" or k.startswith(o + os.sep) for o in out):
            continue
        out.append(k)
    return sorted(({"path": k, "bytes": cold[k][0], "files": cold[k][1], "idle_days": round((now - cold[k][2]) / 86400, 1)}
                   for k in out), key=lambda r: -r["bytes"])


# 体检结果里需要往下钻的几类：受保护的生产目录、要问人的、认不出的、第 3 档
TRIAGE_KEYS = ("protected_production", "referenced_ask", "unclassified", "tier3", "tier3_protected_ref")
PROCESS_RX = re.compile(r"(^|/)(tmp|temp|cache|caches|scratch|frames?|候选|自审|中间|过程|渲染缓存|node_modules|live-state|gate-lab|"
                        r"[^/]*-release|[^/]*-night|[^/]*试做[^/]*)(/|$)", re.I)


def triage_roots(triage, min_bytes):
    seen, out = set(), []
    for key in TRIAGE_KEYS:
        for it in triage.get(key) or []:
            p, b = it.get("path"), it.get("bytes") or 0
            if not p or b < min_bytes or p in seen or not os.path.isdir(p):
                continue
            seen.add(p)
            out.append({"root": p, "from": key, "category": it.get("category"),
                        "guard": it.get("red_line") or it.get("keep_hint") or None})
    return out


def from_triage(a, cfg):
    t = json.load(open(a.from_triage, encoding="utf-8"))
    c = cold_cfg(cfg)
    work = os.path.expanduser(cfg.get("work_root") or "")
    max_depth = getattr(a, "max_depth", 6)
    profile = protect.load_profile() if protect else {}
    # A parent aggregate may contain a protected child which isn't itself large
    # enough to appear in roots. Reject both containing and contained rows.
    blocked = set()
    for key in TRIAGE_KEYS:
        for item in t.get(key) or []:
            hints = (item.get("red_line") or "", item.get("keep_hint") or "")
            hard = any(h.startswith(("硬保护：", "profile 保护：")) for h in hints)
            busy_unknown = key == "unclassified" and (item.get("open") or 0) > 0
            if item.get("path") and (hard or busy_unknown):
                blocked.add(os.path.normpath(item["path"]))

    def intersects(p):
        return any(p == b or p.startswith(b + os.sep) or b.startswith(p + os.sep) for b in blocked)

    rows = []
    roots = triage_roots(t, a.min_gib * GIB)
    for r in roots:
        if any(r["root"].startswith(x["root"] + os.sep) for x in roots):
            continue                                   # 上层已在列表里，下钻时会覆盖到
        if any(r["root"] == b or r["root"].startswith(b + os.sep) for b in blocked):
            continue
        if not _allow_cold_candidate(r["root"], work, profile):
            continue
        parent = os.path.dirname(r["root"])
        top_level = parent in (work, HOME) if work else parent == HOME
        agg = scan(r["root"], c.get("placeholder"), max_depth=max_depth)
        nested = any(b.startswith(r["root"] + os.sep) for b in blocked)
        if nested:
            agg = {k: v for k, v in agg.items()
                   if not intersects(os.path.normpath(os.path.join(r["root"], k)))}
        for row in pick(agg, a.min_idle_days, a.min_gib * GIB, min_depth=1 if top_level else 0):
            row["abs"] = os.path.join(r["root"], row["path"]) if row["path"] else r["root"]
            if not _allow_cold_candidate(row["abs"], work, profile):
                continue
            row.update(path=os.path.relpath(row["abs"], work) if work and row["abs"].startswith(work + os.sep) else row["abs"],
                       source=r["from"], guard=r["guard"], process_like=bool(PROCESS_RX.search(row["abs"])))
            rows.append(row)
    return sorted(rows, key=lambda x: -x["bytes"])


def review_consumers(rows, cfg, a):
    config = cold_cfg(cfg)
    checks = ConsumerChecks(config)
    routes = ReviewRoutes(config, HOME)
    available, held = [], []
    for row in rows:
        if checks.permits(row):
            evidence = routes.evidence(row["abs"])
            if evidence is None:
                available.append(row)
                continue
            row["review_route"] = evidence
            row["local_action"] = evidence["action"]
        held.append(row)
    for row in held:
        print(f"{row['local_action']}：{row['abs']}", file=sys.stderr)
    if getattr(a, "held_json", None):
        with open(a.held_json, "w", encoding="utf-8") as f:
            json.dump(held, f, ensure_ascii=False, indent=2)
    return available


def cmd_candidates(a, cfg):
    if a.from_triage:
        rows = review_consumers(from_triage(a, cfg), cfg, a)
        if a.json:
            json.dump(rows, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        total = sum(r["bytes"] for r in rows)
        print(f"在体检列出的大目录里下钻：冷数据 {len(rows)} 处，约 {total / GIB:.1f} GiB（Git 已跟踪的留原处，不计入）")
        for r in rows:
            tag = "像过程件，先判断能否直接删" if r["process_like"] else "冷存候选"
            print(f"  {r['bytes'] / GIB:7.1f} GiB  闲置 {r['idle_days']:5.1f} 天  {tag:12}  {r['path']}" + (f"  〔{r['guard']}〕" if r["guard"] else ""))
        print("“像过程件”的按清理 Skill 的判断删（可再生、成品已交付的中间稿）；其余不可再生的才冷存。带〔〕的需要用户原话点名。")
        return
    root = os.path.expanduser(a.root or cfg.get("work_root") or "")
    if not root or not os.path.isdir(root):
        raise SystemExit("没有工作区根目录：用 --root 指定，或在 triage-local.json 写 work_root")
    c = cold_cfg(cfg)
    profile = protect.load_profile() if protect else {}
    raw_rows = pick(scan(root, c.get("placeholder"), max_depth=getattr(a, "max_depth", 6)), a.min_idle_days, a.min_gib * GIB)
    rows = []
    for r in raw_rows:
        r["abs"] = os.path.join(root, r["path"])
        if _allow_cold_candidate(r["abs"], root, profile):
            rows.append(r)
    rows = review_consumers(rows, cfg, a)
    if a.json:
        json.dump(rows, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    total = sum(r["bytes"] for r in rows)
    print(f"冷数据候选 {len(rows)} 个，可搬走约 {total / GIB:.1f} GiB（Git 已跟踪的文件留在原处，不计入）"
          + ("" if c.get("cli") else "；本机未配置冷存工具（cold_storage），只能列出，不能搬"))
    for r in rows:
        print(f"  {r['bytes'] / GIB:7.1f} GiB  闲置 {r['idle_days']:5.1f} 天  {r['files']:7d} 个文件  {r['path']}")
    print("这只是候选：删除前冷存工具会交给 quick_act.py 复核生产目录、引用、他人活动与占用；需要点名的照常要用户原话写出路径。")


def run_cli(cfg, args):
    c = cold_cfg(cfg)
    if not c.get("cli"):
        raise SystemExit("本机未配置冷存工具（triage-local.json 的 cold_storage.cli），不能冷存或取回")
    return subprocess.call(c["cli"] + args)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    x = sp.add_parser("candidates"); x.add_argument("--root"); x.add_argument("--from-triage", help="读 triage.py 的 triage.json，只在它列出的大目录里下钻（不再扫全盘）"); x.add_argument("--min-idle-days", type=float, default=14)
    x.add_argument("--min-gib", type=float, default=1); x.add_argument("--max-depth", type=int, default=6); x.add_argument("--json")
    x.add_argument("--held-json", help="写出应留本机的候选及反向软链接证据")
    x = sp.add_parser("consumers", help="只读核对给定目录的软链接消费者"); x.add_argument("path"); x.add_argument("--require-safe", action="store_true")
    x.add_argument("--files-json", help="限本目录内实际待删的精确绝对文件路径数组")
    x = sp.add_parser("offload"); x.add_argument("dir"); x.add_argument("--user-text", required=True)
    x.add_argument("--min-idle-days", type=float, default=14)
    x.add_argument("--keep", action="append", default=[], help="保留在本地的子目录或通配（透传给冷存工具）")
    x.add_argument("--quiet-hours", type=float, default=None, help="删本地要求静默小时数（仅显式传递时透传）")
    x.add_argument("--self-transcript", action="append", default=[], help="透传给冷存/清理执行器的本会话记录路径")
    x = sp.add_parser("where"); x.add_argument("path")
    x = sp.add_parser("restore"); x.add_argument("path")
    a = ap.parse_args(); cfg = load_config()
    if a.cmd == "candidates":
        cmd_candidates(a, cfg); return
    if a.cmd == "consumers":
        row = {"abs": os.path.abspath(a.path)}
        if a.files_json:
            with open(a.files_json, encoding="utf-8") as f:
                files = json.load(f)
            if files is None:
                raise SystemExit("待删文件清单不能为 null")
        else:
            files = None
        allowed = ConsumerChecks(cold_cfg(cfg)).permits(row, files)
        print(json.dumps(row["reverse_symlinks"], ensure_ascii=False, indent=2))
        if a.require_safe and not allowed:
            raise SystemExit(2)
        return
    if a.cmd == "offload":
        row = {"abs": os.path.abspath(a.dir)}
        if not ConsumerChecks(cold_cfg(cfg)).permits(row):
            raise SystemExit(row["local_action"])
        if len(a.user_text.strip()) < 4:
            raise SystemExit("必须给出用户的实际原话（--user-text）")
        cli_args = ["cold", "offload", os.path.abspath(a.dir), "--user-text", a.user_text,
                    "--min-idle-days", f"{a.min_idle_days:g}"]
        for k in a.keep:
            cli_args += ["--keep", k]
        if a.quiet_hours is not None:
            cli_args += ["--quiet-hours", f"{a.quiet_hours:g}"]
        for st in a.self_transcript:
            cli_args += ["--self-transcript", st]
        sys.exit(run_cli(cfg, cli_args))
    sys.exit(run_cli(cfg, [a.cmd, os.path.abspath(a.path)]))


if __name__ == "__main__":
    main()
