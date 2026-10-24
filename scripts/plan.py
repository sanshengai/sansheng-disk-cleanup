#!/usr/bin/env python3
"""计划校验、封存与看板渲染（契约第 4–7 节）。

用法：
  plan.py check --run DIR
  plan.py seal  --run DIR
  plan.py board --run DIR [--out board.html]

只用标准库。plan.json 由 Agent 撰写，这里不改它；校验结果写 plan.check.json，
封存指纹写 seal.json，act.py 只信 seal.json。
"""
import argparse
import artifact_checkpoint
import run_authorization
import base64
import contextlib
import glob
import hashlib
import json
import os
import re
import stat
import subprocess
import shlex
import sys
import posixpath
import time
import unicodedata
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import prefs  # noqa: E402
import protect  # noqa: E402
import codex_state  # noqa: E402
import setup_q  # noqa: E402
import units  # noqa: E402

SKILL_ROOT = os.path.dirname(HERE)
TEMPLATE = os.path.join(SKILL_ROOT, "assets", "dashboard.html")
LOGO = os.path.join(SKILL_ROOT, "assets", "logo.png")

VERDICTS = ("clean", "decide", "keep", "govern")
ACTIONABLE = ("clean", "decide")
SCOPES = ("all", "ignored-only", "files")
EVIDENCE_PREFIXES = ("引用已核对:", "引用已核对：")   # 旧写法：只给 warning，不再放行（见 refs_reviewed）
REFS_VERDICTS = ("history_only", "depends")
RECENT_SECONDS = 24 * 3600


# ----------------------------------------------------------------- 通用小工具

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise ValueError(f"找不到文件：{path}")
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 {path}：{exc}")


def write_json(path, data):
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def within(path, base):
    base = base.rstrip("/") or "/"
    return path == base or path.startswith(base.rstrip("/") + "/")


def canon_nofollow(path):
    """父目录取 realpath，最后一级不跟随符号链接。"""
    parent, name = os.path.split(os.path.abspath(path))
    return os.path.join(os.path.realpath(parent), name)


def entry_protected(path, profile):
    """目录树内单个条目的保护检查：符号链接本身可以删，但绝不跟随到目标。"""
    parent, name = os.path.split(os.path.abspath(path))
    parent_real = os.path.realpath(parent)
    full = os.path.join(parent_real, name)
    if name == ".git":
        return "git 内部数据"
    if os.path.islink(full):
        # protected_reason 会 realpath 跟随链接；对链接只检查它所在的位置
        return protect.protected_reason(os.path.join(parent_real, "__sansheng_probe__"), profile)
    return protect.protected_reason(full, profile)


def digest_of(seal):
    # profile_guard 不进 digest：它由 act.py 单独与当前设置比对；改了设置重新封存但卡片没变时，用户的选择清单仍有效
    body = {k: v for k, v in seal.items() if k not in ("digest", "created_at", "profile_guard")}
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def norm_archive_to(archive_to):
    """归档目标写进指纹前的规范化：去首尾空白、去末尾斜杠。assets/dashboard.html 的 archiveLine 做同样的事。"""
    return str(archive_to or "").strip().rstrip("/")


def selection_sig(seal_digest, cards, answers, archive_to=None):
    """看板选择指纹（看板导出的 `sel:` 行 / `act.py run --sel`）。

    sha256(规范化 JSON)[:12]；JSON 为 {"answers":[排序的"卡号=回答"],"cards":[排序的卡号],"seal":"<seal digest>"}，
    键序固定、无空白、不转义非 ASCII。assets/dashboard.html 里的 selPayload/selSig 须逐字节算出同一值。
    cards：本次选中的卡号（可乱序、可重复）；answers：{卡号: 回答} 或 "卡号=回答" 列表。
    archive_to（0.6 起）：归档目录。非空时多一个键 "archive_to"（规范化后的路径，字典序夹在 answers 与 cards 之间）；
    为空/None 时不出现这个键，签名与 0.5 完全一致（旧看板、旧回执继续可用）。"""
    if isinstance(answers, dict):
        answers = [f"{k}={v}" for k, v in answers.items()]
    body = {"seal": seal_digest, "cards": sorted(set(cards)), "answers": sorted(answers)}
    at = norm_archive_to(archive_to)
    if at:
        body["archive_to"] = at
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def load_profile():
    return protect.load_profile(protect.PROFILE)


# ----------------------------------------------------------------- 校验

def related_nodes(rp, nodes):
    """返回 (primary, descendants)：primary 为同路径节点，没有则取最深的祖先节点。"""
    primary = None
    best = -1
    desc = []
    for n in nodes:
        np_ = os.path.normpath(str(n.get("path", "")))
        if not np_ or np_ == ".":
            continue
        if np_ == rp:
            primary, best = n, 10 ** 9
        elif within(rp, np_):
            if len(np_) > best:
                primary, best = n, len(np_)
        elif within(np_, rp):
            desc.append(n)
    return primary, desc


def _node_errors(node, label, rescue=None, allow_recent=False):
    errs = []
    refs = node.get("refs")
    if not isinstance(refs, dict) or refs.get("checked") is not True:
        # 非前沿节点不做路径引用检查（大块子目录已各自检查）；其余量由 Agent 核对并写明。
        # 超时、出错等其他原因的未检查不可补救。
        if not (isinstance(refs, dict) and refs.get("reason") == "non-frontier" and rescue is not None and rescue()):
            if isinstance(refs, dict) and refs.get("reason") == "no-search-scope":
                errs.append(f"{label}：引用检查未完成（扫描时没有可搜的引用检查目录，引用未查）；"
                            "在设置或台账里补上成品所在目录后，运行 scan.py probe item-refs 补查")
            else:
                errs.append(f"{label}：引用检查未完成（refs.checked 不为 true）")
    if node.get("protected"):
        errs.append(f"{label}：事实中标记为受保护")
    r24 = node.get("recent_24h")
    if not isinstance(r24, int) or isinstance(r24, bool):
        errs.append(f"{label}：缺少 recent_24h，无法确认最近是否被修改")
    elif r24 > 0 and not allow_recent:
        errs.append(f"{label}：24 小时内有 {r24} 个文件被修改")
    open_by = node.get("open_by")
    if open_by == "unknown" or open_by is None:
        errs.append(f"{label}：无法确认是否被进程打开（open_by={open_by!r}）")
    elif open_by:
        names = ",".join(str(o.get("command", "?")) for o in open_by if isinstance(o, dict))
        errs.append(f"{label}：正被进程打开（{names}）")
    return errs


def norm_sample(s):
    """refs.samples 形如 "相对路径:命中数"。比较前规范化：NFC、去首尾空白、反斜杠转斜杠、去 "./"、
    折叠重复斜杠、normpath、去掉末尾 ":数字"。无法规范化返回 None。"""
    if not isinstance(s, str):
        return None
    t = unicodedata.normalize("NFC", s).strip().replace("\\", "/")
    m = re.match(r"^(.*?):\s*\d+$", t)
    if m:
        t = m.group(1).strip()
    t = re.sub(r"/+", "/", t)
    t = posixpath.normpath(t) if t else ""
    return t if t and t != "." else None


def parse_refs_review(ev):
    """解析 {"kind":"refs_reviewed","samples":[…],"verdict":"history_only"|"depends"}。
    返回 {"verdict","samples":规范化集合,"err":结构问题或 None}。"""
    verdict = ev.get("verdict")
    raw = ev.get("samples")
    err = None
    samples = set()
    if verdict not in REFS_VERDICTS:
        err = f"verdict 必须是 {'/'.join(REFS_VERDICTS)}，收到 {verdict!r}"
    if not isinstance(raw, list) or not raw:
        err = err or "samples 必须是非空数组"
    else:
        for x in raw:
            n = norm_sample(x)
            if n is None:
                err = err or f"samples 含无效项 {x!r}"
            else:
                samples.add(n)
    return {"verdict": verdict, "samples": samples, "err": err}


def _node_sample_set(node):
    refs = node.get("refs") if isinstance(node.get("refs"), dict) else {}
    if refs.get("checked") is not True or not isinstance(refs.get("samples"), list):
        return set()
    return {n for n in (norm_sample(x) for x in refs["samples"]) if n}


def refs_covered(node, ctx):
    """已检查的节点（checked=true）的引用，是否被一条合法的 history_only 复核覆盖：
    复核样本必须与该节点 refs.samples 有交集，没有交集的复核等于伪造，不起作用。"""
    have = _node_sample_set(node)
    hit = False
    for r in ctx.get("reviews", ()):
        if r["verdict"] == "history_only" and not r["err"] and r["samples"] & have:
            ctx.setdefault("matched", set()).add(r["idx"])
            hit = True
    return hit


def refs_rescue(ctx, label=None):
    """未检查但可补救的节点（非前沿、仅因含受保护路径而受保护的祖先）没有 refs.samples 可比对。
    P5.R M1：一条结构合法的 history_only 复核只能把它软降级成 decide（问题写“这批内容的引用没有被核实”），
    不再算已核对：这批内容的引用从没被搜索过，样本又无从比对，任意样本都能照抄。
    返回 True 表示“可补救（不是硬错误）”；没有任何合法 history_only 复核时返回 False（仍是硬错误）。"""
    ok = False
    for r in ctx.get("reviews", ()):
        if r["verdict"] == "history_only" and not r["err"]:
            ctx.setdefault("rescued", set()).add(r["idx"])      # 不进 matched：它没有和任何节点样本对上
            ok = True
            break           # 只用掉第一条：同卡其余复核仍要跟节点样本比对，对不上照样报“视为伪造”，不被一并吞掉
    if ok:
        ctx.setdefault("soft", []).append(f"{label or '所在节点'}：{UNVERIFIED_REFS_REASON}（所在节点没有引用样本可比对，只能由你确认）")
    return ok


def _refs_hint(ctx):
    return "（旧前缀「引用已核对:」写法已不放行）" if ctx.get("legacy") else ""


def _ir_usable(ir, ctx):
    """probe item-refs 对这一条的结果能不能当引用核对用：查过（checked）；
    且扫描之后若有新增的成品目录（ctx["stale_roots"]），它搜过的目录必须覆盖当前全部引用检查目录。"""
    if not (isinstance(ir, dict) and ir.get("checked") is True):
        return False
    if not ctx.get("stale_roots"):
        return True
    return not protect.roots_uncovered(ctx.get("cur_roots") or [], ir.get("roots"))


def _stale_msg(label, ctx):
    return (f"{label}：台账或设置新增了成品目录，扫描时没搜过，需要补查引用：运行 "
            f"scan.py probe item-refs --run {ctx.get('run') or '<本次扫描目录>'}")


def _alloc(path):
    try:
        return os.lstat(path).st_blocks * 512
    except OSError:
        return 0


def _live_item_errors(rp, label, allow_recent=False):
    """节点内部条目的现场核对：24 小时内改动；返回 (错误列表, 实际分配字节数)。只用 lstat，不跟随链接。"""
    cutoff = datetime.now(timezone.utc).timestamp() - RECENT_SECONDS
    total, recent, first = 0, 0, None
    stack = [rp]
    while stack:
        p = stack.pop()
        try:
            st = os.lstat(p)
        except OSError as exc:
            return [f"{label}：现场核对时无法读取 {p}（{exc.strerror or exc}）"], None
        total += getattr(st, "st_blocks", 0) * 512
        if st.st_mtime > cutoff and not stat.S_ISDIR(st.st_mode):
            recent += 1
            first = first or p
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            try:
                stack.extend(os.path.join(p, c) for c in os.listdir(p))
            except OSError as exc:
                return [f"{label}：现场核对时无法列出 {p}（{exc.strerror or exc}）"], None
    if recent and not allow_recent:
        return [f"{label}：24 小时内有 {recent} 个文件被修改（如 {first}）"], total
    return [], total


def _codex_target(argv):
    """argv 是 codex 命令时返回线程 UUID（形状不对返回 False）；不是 codex 返回 None。"""
    if argv[0] != "codex":
        return None
    if len(argv) == 4 and argv[1:3] == ["delete", "--force"] and UUID_RE.fullmatch(argv[3]):
        return argv[3].lower()
    return False


def _native_target_mismatch(argv, path):
    """native 条目的形状 + path 对应校验（plan check 与 act 执行前复核共用，规则全在 protect.SHAPES）。
    返回错误说明或 None。命令必须逐词落在已登记形状里，槽位对象（线程/包/模型…）必须就是 path 指向的那个，
    防止核对的是 A、删掉的是 B；对应不了对象的形状要求 path 落在该工具自己的数据目录内。
    codex 仍是 ["codex","delete","--force",<完整UUID>]，path 须是 ~/.codex/sessions 或 archived_sessions 下的会话文件。"""
    return protect.native_check(argv, path)


def _codex_tree_errors(items):
    """删父会话会连带删除全部后代：每个仍存在的后代都必须作为 codex delete 条目列在同一张卡、且排在父会话前面。"""
    order = {}
    for i, it in enumerate(items):
        if isinstance(it, dict) and isinstance(it.get("argv"), list) and it["argv"]:
            tid = _codex_target(it["argv"])
            if tid:
                order.setdefault(tid, i)
    if not order:
        return []
    rel = codex_state.relations(protect.HOME)
    errs = []
    for tid, i in order.items():
        for d in codex_state.descendants(tid, rel["children"]):
            path = rel["rollout"].get(d)
            if not path or not os.path.lexists(path):
                continue
            j = order.get(d)
            if j is None:
                errs.append(f"删除线程 {tid} 会连带删除子会话 {d}，它必须作为条目列在同一张卡里（排在父会话前面）")
            elif j > i:
                errs.append(f"子会话 {d} 必须排在父会话 {tid} 之前")
    return errs


# ----------------------------------------------------------------- 结束证据（evidence 对象形式）
#
# evidence 数组里的元素：字符串（旧写法，照常接受，只作说明，不算结束证据），或对象：
#   {"kind":"merged","repo":"/abs","ref":"分支或提交","target":"origin/main"}   分支已并入目标（默认 origin/main）
#   {"kind":"delivered","glob":"/abs/成片*","expect":16}                         成品数量对得上
#   {"kind":"copy_verified","src":"/abs/a.png","dst":"/abs/b.png"}               副本大小与 sha256 相同，且副本在受跟踪目录或 profile.deliverable_roots 内
#   {"kind":"user_stated","text":"…"}                                           用户口述：接受，但不算结束证据
# merged / delivered / copy_verified 由脚本现场复核，复核通过才算「结束证据」；外部命令一律带超时，失败按未通过。

EVIDENCE_END_KINDS = ("merged", "delivered", "copy_verified")
EVIDENCE_KINDS = EVIDENCE_END_KINDS + ("user_stated",)
GIT_TIMEOUT = 20
EVIDENCE_SECONDS = 120          # 单条证据的总时限（含大文件哈希、glob 展开）
BAD_PATH_WORDS = ("rejected", "tmp", "temp", "scratch")
# 按会话/任务划界的原生命令：删的是某个任务的产物，需要结束证据；其余登记的原生命令都是缓存清理
TASK_SHAPES = ("codex-delete", "grok-sessions-delete")
# 条目 class（探针 --emit 写入）：interactive 交互会话 / batch 批量出图 / unknown 判不出；缺失按 unknown，兼容旧条目文件
ITEM_CLASSES = ("interactive", "batch", "unknown")
INSTALLER_EXTS = (".dmg", ".pkg", ".mpkg", ".zip", ".xip", ".iso")
INSTALLER_POLICY_EXTS = (".dmg", ".pkg", ".mpkg", ".xip", ".iso")      # installer_policy 只认这些（不含 .zip）
ARCHIVE_RE = re.compile(r"/Xcode/Archives(/|$)|\.xcarchive(/|$)", re.I)


def _bad_segment(parts):
    """路径段（按非字母数字拆词）含 rejected/tmp/temp/scratch 之一就是废稿或临时目录；返回命中的段，没有返回 None。"""
    for seg in parts:
        for tok in re.split(r"[^0-9a-z]+", seg.lower()):
            if tok in ("tmp", "temp") or tok.startswith(("rejected", "scratch")):
                return seg
    return None


def _rel_parts(path, base):
    rel = os.path.relpath(path, base)
    return [] if rel == "." else rel.split(os.sep)


def _git(repo, *args):
    """返回 (退出码, 输出)；超时、找不到 git 等一律 (None, 说明)。"""
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                           timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except subprocess.TimeoutExpired:
        return None, f"git 超过 {GIT_TIMEOUT} 秒未返回"
    except (OSError, ValueError) as exc:
        return None, f"git 无法运行（{exc}）"
    return p.returncode, (p.stdout or p.stderr or "").strip()


def _abs_arg(ev, key):
    v = ev.get(key)
    if not isinstance(v, str) or not v.strip():
        return None
    v = protect.expand(v.strip())
    return v if os.path.isabs(v) else None


def _common_dir(path):
    """path（目录或文件）所在仓库的 git-common-dir（realpath）；不在仓库里返回 None。"""
    d = path
    while d and not os.path.isdir(d):
        nd = os.path.dirname(d)
        if nd == d:
            return None
        d = nd
    rc, out = _git(d, "rev-parse", "--git-common-dir")
    if rc != 0 or not out:
        return None
    return os.path.realpath(out if os.path.isabs(out) else os.path.join(d, out))


def _related_worktrees(repo, item_paths):
    """P5.R H2：merged 的 repo 必须与某个待删条目同仓。返回与条目相关的工作树 [(工作树路径, 它当前的 HEAD 提交)]：
    条目落在该工作树内、或条目包含该工作树（条目是工作树本身或其父目录）；没有相关工作树（不是同一个仓库）返回空表。
    条目不在任何工作树内、但与该仓 git-common-dir 相同时，退回该仓全部工作树。"""
    rc, out = _git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        return []
    wts, cur = [], None
    for ln in out.splitlines():
        if ln.startswith("worktree "):
            cur = [os.path.realpath(ln[len("worktree "):]), None]
            wts.append(cur)
        elif ln.startswith("HEAD ") and cur is not None:
            cur[1] = ln[len("HEAD "):].strip()
    related = [(w, h) for w, h in wts if h and any(within(p, w) or within(w, p) for p in item_paths)]
    if related:
        return related
    common = _common_dir(repo)
    if common is not None and any(_common_dir(p) == common for p in item_paths):
        return [(w, h) for w, h in wts if h]
    return []


def _verify_merged(ev, ctx):
    repo = _abs_arg(ev, "repo")
    if not repo or not os.path.isdir(repo):
        return False, "merged：repo 必须是存在的仓库绝对路径"
    ref = ev.get("ref")
    target = ev.get("target", "origin/main")
    for name, val in (("ref", ref), ("target", target)):
        if not isinstance(val, str) or not val.strip() or val.startswith("-") or any(c.isspace() for c in val):
            return False, f"merged：{name} 必须是分支名或提交号"
    related = _related_worktrees(repo, ctx.get("item_paths") or [])
    if not related:
        return False, "merged：repo 与待清理条目不是同一个仓库（条目必须在该仓的某个工作树内）"
    rc_t, full = _git(repo, "rev-parse", "--symbolic-full-name", target)
    if rc_t != 0 or not full.startswith("refs/remotes/"):
        return False, f"merged：target 必须是远端跟踪分支（如 origin/main），{target!r} 不是"
    rc_r, c_ref = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    rc_c, c_tgt = _git(repo, "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}")
    if rc_r != 0 or rc_c != 0:
        return False, f"merged：无法解析 {ref if rc_r != 0 else target} 对应的提交"
    if c_ref not in {h for _, h in related}:
        return False, f"merged：ref {ref!r} 不是待清理条目所在工作树当前检出的提交，它证明不了这批东西已被并入"
    if c_ref == c_tgt:
        return False, f"merged：{ref} 与 {target} 是同一个提交，这不能证明它被并入了别的地方"
    rc, out = _git(repo, "merge-base", "--is-ancestor", ref, target)
    if rc == 0:
        return True, f"{ref} 已并入 {target}"
    if rc == 1:
        return False, f"merged：{ref} 没有并入 {target}"
    return False, f"merged：无法复核（{out or rc}）"


def _broad_roots():
    return protect.broad_roots()


def _delivered_root_ok(root, ctx):
    """P5.R H2：delivered 的 glob 根目录（去掉通配后的目录）必须不是宽泛位置，且落在 reference_roots / deliverable_roots
    或某个 git 工作树内。返回 (是否合格, 原因)。"""
    real = os.path.realpath(root)
    if real in _broad_roots():
        return False, f"glob 根目录 {real} 太宽泛，不能当成品位置"
    for key in ("reference_roots", "deliverable_roots"):
        for r in ctx["profile"].get(key) or []:
            if isinstance(r, str) and r.strip():
                rr = protect.real(r.strip())
                broad = rr in _broad_roots() or (key == "deliverable_roots" and protect.is_broad_deliverable(rr))
                if not broad and within(real, rr):
                    return True, f"在 profile.{key} 内"
    d = real
    while d and not os.path.isdir(d):
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    rc, top = _git(d, "rev-parse", "--show-toplevel")
    if rc == 0 and top and os.path.realpath(top) not in _broad_roots():
        return True, "在 git 工作树内"
    return False, f"glob 根目录 {real} 不在 profile 的 reference_roots / deliverable_roots 内，也不在任何 git 工作树内"


def _verify_delivered(ev, ctx):
    pat = _abs_arg(ev, "glob")
    expect = ev.get("expect")
    if not pat:
        return False, "delivered：glob 必须是绝对路径模式"
    if not isinstance(expect, int) or isinstance(expect, bool) or expect < 1:
        return False, "delivered：expect 必须是 ≥1 的整数"
    cut = min([i for i in (pat.find(c) for c in "*?[") if i >= 0] or [len(pat)])
    base = os.path.dirname(pat[:cut]) if cut < len(pat) else os.path.dirname(pat)
    ok_root, why_root = _delivered_root_ok(base or "/", ctx)
    if not ok_root:
        return False, f"delivered：{why_root}"
    root_real = os.path.realpath(base)
    deadline = time.monotonic() + EVIDENCE_SECONDS
    count = 0
    for m in glob.iglob(pat, recursive=True):
        if time.monotonic() > deadline:
            return False, f"delivered：展开 {pat} 超过 {EVIDENCE_SECONDS} 秒"
        if not os.path.exists(m) or _bad_segment(_rel_parts(m, base)):
            continue        # os.path.exists 跟随链接：悬空符号链接不计数
        real_m = os.path.realpath(m)
        if not within(real_m, root_real):
            continue        # 链接指到根目录之外的不计
        if any(within(real_m, p) for p in ctx["item_paths"]):
            continue    # 在待清理路径里的不算：清完就没了
        count += 1
        if count >= expect:
            return True, f"{pat} 实际 ≥{expect} 个"
    return False, f"delivered：{pat} 只有 {count} 个，少于期望的 {expect}"


def _sha256_same(a, b, deadline):
    ha, hb = hashlib.sha256(), hashlib.sha256()
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            if time.monotonic() > deadline:
                return None
            ca, cb = fa.read(1 << 20), fb.read(1 << 20)
            ha.update(ca)
            hb.update(cb)
            if not ca and not cb:
                break
    return ha.hexdigest() == hb.hexdigest()


def _dst_home(dst_real, ctx):
    """副本所在的合格位置：返回 (位置根, 说明) 或 (None, 原因)。受跟踪文件优先，其次 profile.deliverable_roots。"""
    parent = os.path.dirname(dst_real)
    rc, top = _git(parent, "rev-parse", "--show-toplevel")
    if rc == 0 and top:
        top = os.path.realpath(top)
        rc2, _ = _git(top, "ls-files", "--error-unmatch", "--", os.path.relpath(dst_real, top))
        if rc2 == 0:
            return top, "副本被 git 跟踪"
    for r in ctx["profile"].get("deliverable_roots") or []:
        if isinstance(r, str) and r.strip():
            root = protect.real(r.strip())
            if protect.is_broad_deliverable(root):      # 手改 profile 塞进来的宽泛位置（~、~/Downloads 等）不当副本的家
                continue
            if within(dst_real, root) and dst_real != root:
                return root, "副本在成品目录"
    return None, "副本既不被 git 跟踪，也不在 profile.deliverable_roots 内"


def _verify_copy(ev, ctx):
    src, dst = _abs_arg(ev, "src"), _abs_arg(ev, "dst")
    if not src or not dst:
        return False, "copy_verified：src 与 dst 必须是绝对路径"
    src_real = os.path.join(os.path.realpath(os.path.dirname(src)), os.path.basename(src))
    if not any(within(src_real, p) for p in ctx["item_paths"]):
        return False, "copy_verified：src 不在任何待清理条目之内，这个副本证明不了被删的东西有备份"
    try:
        s_st, d_st = os.lstat(src), os.lstat(dst)
    except OSError as exc:
        return False, f"copy_verified：文件不存在（{exc.strerror or exc}）"
    if stat.S_ISLNK(d_st.st_mode) or not stat.S_ISREG(d_st.st_mode) or not stat.S_ISREG(s_st.st_mode):
        return False, "copy_verified：src 与 dst 都必须是普通文件（dst 不能是符号链接）"
    if (s_st.st_dev, s_st.st_ino) == (d_st.st_dev, d_st.st_ino):
        return False, "copy_verified：dst 与 src 是同一个文件（硬链接不是副本）"
    if s_st.st_size != d_st.st_size:
        return False, f"copy_verified：大小不同（src {s_st.st_size}，dst {d_st.st_size}）"
    dst_real = os.path.join(os.path.realpath(os.path.dirname(dst)), os.path.basename(dst))
    if any(within(dst_real, p) for p in ctx["item_paths"]):
        return False, "copy_verified：dst 在待清理路径之内，清理后副本也没了"
    root, why = _dst_home(dst_real, ctx)
    if root is None:
        return False, f"copy_verified：{why}"
    bad = _bad_segment(_rel_parts(dst_real, root))
    if bad:
        return False, f"copy_verified：副本路径含废稿/临时目录「{bad}」"
    try:
        same = _sha256_same(src, dst, time.monotonic() + EVIDENCE_SECONDS)
    except OSError as exc:
        return False, f"copy_verified：读取失败（{exc.strerror or exc}）"
    if same is None:
        return False, f"copy_verified：哈希计算超过 {EVIDENCE_SECONDS} 秒"
    if not same:
        return False, "copy_verified：sha256 不同"
    return True, f"{why}，大小与 sha256 一致"


_VERIFIERS = {"merged": _verify_merged, "delivered": _verify_delivered, "copy_verified": _verify_copy}


def verify_evidence(ev, ctx):
    """复核一条 evidence，返回 {"kind","ok","end","msg"}。end=True 表示它是通过复核的结束证据。
    字符串（旧写法）和 user_stated 只作说明；未知 kind、缺字段、复核失败都是 ok=False。"""
    if isinstance(ev, str):
        return {"kind": "text", "ok": True, "end": False, "msg": "字符串证据，不算结束证据"}
    if not isinstance(ev, dict):
        return {"kind": "invalid", "ok": False, "end": False, "msg": f"evidence 元素必须是字符串或对象，收到 {type(ev).__name__}"}
    kind = ev.get("kind")
    if kind == "user_stated":
        return {"kind": kind, "ok": True, "end": False, "msg": "用户口述，不算结束证据"}
    if kind == "refs_reviewed":
        # 只做结构检查；样本是否取自 facts 在 check_item 里按节点比对。它不是「已结束」证据
        r = parse_refs_review(ev)
        return {"kind": kind, "ok": r["err"] is None, "end": False,
                "msg": "引用核对，不算结束证据" if r["err"] is None else f"refs_reviewed：{r['err']}"}
    fn = _VERIFIERS.get(kind)
    if fn is None:
        return {"kind": str(kind), "ok": False, "end": False,
                "msg": f"evidence.kind 必须是 {'/'.join(EVIDENCE_KINDS)}，收到 {kind!r}"}
    try:
        ok, msg = fn(ev, ctx)
    except Exception as exc:    # 复核本身出错一律按未通过，不让一条坏证据拖垮整个 check
        ok, msg = False, f"{kind}：复核出错（{type(exc).__name__}: {exc}）"
    return {"kind": kind, "ok": ok, "end": ok, "msg": msg}


_PROD_ENGINE = {}


@contextlib.contextmanager
def _scan_home(scan):
    old = scan.HOME
    scan.HOME = protect.HOME
    try:
        yield
    finally:
        scan.HOME = old


def _match_own(rp):
    """对条目 realpath 本身跑一次 producer 规则（与 scan 给节点打 producer 同一套规则与 bundle id 反查）。
    出错一律返回空 producer。"""
    try:
        import scan
        key = (protect.HOME, os.stat(scan.PRODUCERS_PATH).st_mtime_ns)
        with _scan_home(scan):
            if _PROD_ENGINE.get("key") != key:
                try:
                    apps = scan.installed_apps()
                except Exception:       # noqa: BLE001  应用枚举失败就退回纯 glob 匹配
                    apps = None
                _PROD_ENGINE.update(key=key, engine=scan.Producers(), apps=apps)
            return _PROD_ENGINE["engine"].match(rp, _PROD_ENGINE["apps"])
    except Exception:                   # noqa: BLE001
        return {}


def _identified(p):
    return isinstance(p, dict) and bool(p.get("card") or p.get("tool") or p.get("safety"))


def merged_producer(rp, nodes, related=None):
    """P5.R H1：条目自己的风险画像 = 三方合并，不再只看「最近的事实节点」：
    ① primary 节点（同路径或最深祖先节点）的 producer；② 对条目 realpath 本身现跑的 producer 规则
    （scan 只给 ≥ 阈值的目录建节点，小目录会落在祖先节点的 producer 下）；③ 每个后代节点（条目是它们的父目录）的 producer。
    返回 {"chat","risky","blockers","cards","updater","safe","default_select","prods"}：
    任一方 risky 即 risky，任一方 chat-netdisk+risky 即 chat；blockers 取并集；
    safe（H3：免结束证据）要求条目自己的规则（内部条目时优先于祖先节点）与全部后代都是 safety=="safe"，且没有任何一方 risky；
    default_select 要求同一组都明写 true。check_item / _item_needs_end_evidence / compute_default_select 共用本函数。"""
    primary, desc = related if related is not None else related_nodes(rp, nodes)
    pnode = (primary or {}).get("producer") if isinstance(primary, dict) else None
    pnode = pnode if isinstance(pnode, dict) else {}
    own = _match_own(rp)
    dprods = [d.get("producer") if isinstance(d.get("producer"), dict) else {} for d in desc]
    exact = isinstance(primary, dict) and os.path.normpath(str(primary.get("path", ""))) == rp
    if exact:
        base = [x for x in (pnode, own) if _identified(x)]
    else:
        base = [own] if _identified(own) else ([pnode] if _identified(pnode) else [])
    everyone = [pnode, own] + dprods
    blockers = []
    for pr in everyone:
        b = pr.get("blocked_by_processes")
        for x in (b if isinstance(b, list) else []):
            if isinstance(x, str) and x.strip() and x not in blockers:
                blockers.append(x)
    cards = {pr.get("card") for pr in everyone if pr.get("card")}
    risky = any(pr.get("safety") == "risky" for pr in everyone)
    chat = any(pr.get("safety") == "risky" and pr.get("card") == "chat-netdisk" for pr in everyone)
    safe = not risky and bool(base) and all(x.get("safety") == "safe" for x in base + dprods)
    dsel = bool(base) and all(x.get("default_select") is True for x in base + dprods)
    return {"chat": chat, "risky": risky, "blockers": blockers, "cards": cards,
            "updater": "app-updaters-installers" in cards, "safe": safe, "default_select": dsel,
            "prods": everyone}


def _item_needs_end_evidence(item, nodes):
    """这个条目的 clean 是否要「任务已结束」的正向证据（判断准则第 3 问）。
    P5.R H3：免证据改成正向条件——只有缓存清理类原生命令，或合并后的 producer（见 merged_producer）safety 为 "safe"
    的路径才免；moderate、缺 safety、归不进任何规则的未知目录一律要证据。不再按「整张知识卡」豁免。
    要：codex/grok 会话删除，以及其余一切。"""
    if not isinstance(item, dict):
        return False
    if item.get("method") == "native" and isinstance(item.get("argv"), list) and item["argv"]:
        hit = protect.native_match([a for a in item["argv"] if isinstance(a, str)])
        if hit is not None and hit[0].sid not in TASK_SHAPES:
            return False
        return True
    path = item.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        return False
    return not merged_producer(os.path.realpath(path), nodes)["safe"]


def evidence_review(card, items, nodes, profile, validated_checkpoints=None):
    """复核卡片 evidence，返回 (逐条结果, 是否有通过复核的结束证据, 是否需要结束证据)。"""
    ev_list = card.get("evidence")
    ev_list = ev_list if isinstance(ev_list, list) else []
    paths = [os.path.realpath(i["path"]) for i in (items if isinstance(items, list) else [])
             if isinstance(i, dict) and isinstance(i.get("path"), str) and os.path.isabs(i["path"])]
    ctx = {"profile": profile, "item_paths": paths}
    checks = []
    for e in ev_list:
        if not isinstance(e, str):
            c = verify_evidence(e, ctx)
            c["evidence"] = e           # 带原文进看板：通过复核的证据让用户看见“已结束”依据的是什么
            checks.append(c)
    if items and all(isinstance(i, dict) and i.get("checkpoint") for i in items):
        verified = [(i["path"] in validated_checkpoints, "sealed checkpoint") for i in items] if validated_checkpoints is not None else [artifact_checkpoint.verify(i) for i in items]
        checks.append({"kind": "artifact_checkpoint", "ok": all(v[0] for v in verified),
                       "end": all(v[0] for v in verified),
                       "msg": "仅这些过程件的阶段与保留副本通过证明；不代表整个项目结束"})
    has_end = any(c["end"] for c in checks)
    needs = any(_item_needs_end_evidence(i, nodes) for i in (items if isinstance(items, list) else []))
    return checks, has_end, needs


def _inferred_interactive(item):
    """P5.R M2：按命令形状与路径判断它是不是交互会话（class 缺失、unknown 时用）：codex delete / grok sessions delete，
    ~/.claude/projects/**/*.jsonl，~/.codex/sessions/** 与 ~/.codex/archived_sessions/** 下的条目。"""
    if not isinstance(item, dict):
        return False
    argv = item.get("argv")
    if item.get("method") == "native" and isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv):
        hit = protect.native_match(argv)
        if hit is not None and hit[0].sid in TASK_SHAPES:
            return True
    path = item.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        return False
    rp = os.path.realpath(path)
    home = os.path.realpath(protect.HOME)
    if within(rp, os.path.join(home, ".claude", "projects")) and rp.endswith(".jsonl"):
        return True
    return any(within(rp, os.path.join(home, ".codex", d)) for d in ("sessions", "archived_sessions"))


def item_class(item):
    """条目的有效 class：显式 interactive 就是 interactive；显式 batch 与推断冲突时，来自探针 items_file 的信 batch，
    手写的按 interactive；缺失或 unknown 按推断（会话形状 → interactive），推断不出才是 unknown。"""
    if not isinstance(item, dict):
        return "unknown"
    cls = item.get("class")
    if cls == "interactive":
        return "interactive"
    if cls == "batch":
        return "interactive" if (_inferred_interactive(item) and not item.get("_emit")) else "batch"
    return "interactive" if _inferred_interactive(item) else "unknown"


def class_conflict(item):
    """手写 batch 与会话推断冲突（且没有探针 emit 来源）：返回说明，否则 None。"""
    if (isinstance(item, dict) and item.get("class") == "batch" and not item.get("_emit")
            and _inferred_interactive(item)):
        return (f"{item.get('path')}：手写 class=batch 与路径/命令推断（交互会话）冲突，没有探针 emit 来源，"
                "按 interactive 处理（会话一卡一个、不得 clean）；若确是批量出图请用探针 --emit 生成条目")
    return None


def interactive_sessions(items):
    """卡里交互会话的个数（按 session 去重：一个会话的后代线程与原图目录同属一个会话，不重复计）。
    没有 session 字段的 interactive 条目各算一个（宁可多计、不漏计）。class 缺失时按路径/命令推断（M2）。"""
    out = set()
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and item_class(it) == "interactive":
            sess = it.get("session")
            out.add(sess if isinstance(sess, str) and sess else "path:" + str(it.get("path")))
    return out


def _installer_or_archive(item):
    paths = [item.get("path")] + (item.get("files") if isinstance(item.get("files"), list) else [])
    for p in paths:
        if isinstance(p, str) and (p.lower().endswith(INSTALLER_EXTS) or ARCHIVE_RE.search(p)):
            return True
    return False


def compute_default_select(card, items, verdict_out, nodes):
    """看板是否默认勾选这张卡，只由这里算，plan.json 里写的值不作数。保守：任何一条不满足就不勾。
    false：不是 clean、含交互会话、安装包/归档、模型缓存卡、更新暂存（除非 updater_verdict=superseded）。
    true：其余 clean 卡，且每个条目要么是纯缓存原生命令，要么所在节点 producer 的 default_select 为 true。"""
    if verdict_out != "clean" or not isinstance(items, list) or not items:
        return False
    if interactive_sessions(items):
        return False
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("path"), str) or not os.path.isabs(it["path"]):
            return False
        if _installer_or_archive(it):
            return False
        m = merged_producer(os.path.realpath(it["path"]), nodes)
        if "model-caches" in m["cards"]:
            return False
        if m["updater"] and card.get("updater_verdict") != "superseded":
            return False
        if it.get("method") == "native" and isinstance(it.get("argv"), list) and it["argv"]:
            hit = protect.native_match([a for a in it["argv"] if isinstance(a, str)])
            if hit is not None and hit[0].sid not in TASK_SHAPES:
                continue                    # 纯缓存原生命令
            return False
        if not m["default_select"]:
            return False
    return True


def _item_newest_ts(it, nodes):
    """条目最近一次修改的时间戳：节点自己的 newest_mtime（路径逐字相同）优先，其次现场 lstat；都拿不到返回 None。"""
    rp = protect.lexical(it["path"])
    primary, _ = related_nodes(rp, nodes)
    mt = primary.get("newest_mtime") if primary and os.path.normpath(str(primary.get("path"))) == rp else None
    if isinstance(mt, str):
        try:
            mt = datetime.fromisoformat(mt.replace("Z", "+00:00")).timestamp()
        except ValueError:
            mt = None
    if not isinstance(mt, (int, float)):
        try:
            mt = os.lstat(rp).st_mtime
        except OSError:
            return None
    return mt


def _retention_hit(items, nodes, profile):
    """用户在初次设置里定了“最近 N 天内用过的 AI 对话一律留着”：交互会话条目最近修改在 N 天内 → 整卡保留。"""
    days = profile.get("retention_days") if isinstance(profile, dict) else None
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        return None
    newest = 0
    for it in items if isinstance(items, list) else []:
        if not (isinstance(it, dict) and item_class(it) == "interactive" and isinstance(it.get("path"), str)):
            continue
        mt = _item_newest_ts(it, nodes)
        if mt is not None:
            newest = max(newest, mt)
    age = (time.time() - newest) / 86400 if newest else None
    if age is not None and age < days:
        return f"你设定最近 {days} 天内用过的 AI 对话一律留着，这个会话 {age:.0f} 天前还有改动"
    return None


# ----------------------------------------------------------------- 初始设置对判断的影响（0.6）
# 原则：设置只改变“问不问、预填什么、进哪一档”，只会让卡变得更保守（降 keep / 降 decide / 抬档位 / 预填），
# 不会让任何卡从 decide 升为 clean，也不会绕过证据复核、引用核对、进程与 24 小时检查。

LIBRARY_REASON = "你说过旧对话当资料库，都留着"
REJECTS_KEEP_REASON = "你说过没挑中的图都留着，而且这批图没有通过复核的成品副本或交付证据"
BIG_OFF_REASON = "你说过录屏和大视频别问"
WT_BUILD_ONLY_REASON = "你说过只清构建产物，整棵工作树先留着：这里要删整棵工作树，改为让你确认"
WT_BUILD_ONLY_QUESTION = "只清构建产物，整棵工作树先留着？"
SLOW_REBUILD_CARDS = ("model-caches",)


def settings_of(profile):
    """check / propose 用到的设置，已校验：不合法的值当作没设。"""
    p = profile if isinstance(profile, dict) else {}

    def one(key, allowed):
        return p.get(key) if p.get(key) in allowed else None
    tools = p.get("tools") if isinstance(p.get("tools"), dict) else {}
    big = p.get("big_file_age_days")
    return {
        "tools_unused": {k for k, v in tools.items() if v == "unused" and k in setup_q.TOOL_KEYS},
        "conv": one("conversation_policy", setup_q.SINGLE["conversations"][1]),
        "rejects": one("rejects_policy", setup_q.SINGLE["rejects"][1]),
        "worktree": one("worktree_policy", setup_q.SINGLE["worktrees"][1]),
        "installer": one("installer_policy", setup_q.SINGLE["installers"][1]),
        "big_days": big if isinstance(big, int) and not isinstance(big, bool) and big in setup_q.BIG_FILE_DAYS else None,
        "done_project": one("done_project_policy", setup_q.SINGLE["done_projects"][1]),
    }


def _first_card(card):
    used = card.get("cards_used") if isinstance(card.get("cards_used"), list) else []
    return used[0] if used and isinstance(used[0], str) else ""


def _is_worktree_root(rp, nodes):
    """条目路径正好是某个 git 工作树的根目录（节点 git.role == worktree，或节点自己就是它所在工作树的根）。"""
    for n in nodes if isinstance(nodes, list) else []:
        if not isinstance(n, dict) or os.path.realpath(str(n.get("path") or "")) != rp:
            continue
        g = n.get("git") if isinstance(n.get("git"), dict) else {}
        if g.get("role") == "worktree":
            return True
        wt = g.get("worktree")
        if isinstance(wt, str) and os.path.realpath(wt) == rp:
            return True
    return False


def _installer_card(card, items):
    """“安装包直接清”只认真正的安装包（.dmg/.pkg/.mpkg/.xip/.iso），且整张卡的条目全是安装包。
    不看卡片 id、不认 .zip（zip 常是文档、素材、源码包）；混有文档或 .zip 的卡不受 installer_policy 影响（评审 8）。"""
    if not isinstance(items, list) or not items:
        return False
    for it in items:
        if not isinstance(it, dict):
            return False
        files = it.get("files") if isinstance(it.get("files"), list) else []
        paths = [x for x in files if x] if files else [it.get("path")]
        if not paths or not all(isinstance(x, str) and x.lower().endswith(INSTALLER_POLICY_EXTS) for x in paths):
            return False
    return True


def _floor_preset(card, preset, wt_ask):
    """重建很慢的缓存（卡片写了 rebuild: slow，或模型缓存）不进稳妥档；工作树选了“每次问我”也至少是推荐档。"""
    if preset == "safe" and (card.get("rebuild") == "slow" or _first_card(card) in SLOW_REBUILD_CARDS or wt_ask):
        return "recommended"
    return preset


def cleanup_targets(cards):
    """Exact file selections do not include their retained sibling receipts."""
    files, trees = set(), []
    for card in cards:
        if not isinstance(card, dict) or card.get("verdict") not in ("clean", "decide"):
            continue
        for item in card.get("items", []) if isinstance(card.get("items", []), list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("method") == "delete" and item.get("scope") == "files":
                for entry in item.get("files", []) if isinstance(item.get("files"), list) else []:
                    path = entry.get("path") if isinstance(entry, dict) else entry
                    if isinstance(path, str):
                        files.add(os.path.realpath(path))
            elif isinstance(item.get("path"), str):
                trees.append(os.path.realpath(item["path"]))
    return {"files": files, "trees": trees}


def dependency_selected(dependency, ctx):
    dep = os.path.realpath(dependency)
    return dep in ctx.get("cleanup_files", set()) or any(
        within(dep, target) for target in ctx.get("cleanup_paths", []))


def check_item(item, ctx):
    """返回 (错误列表, 封存记录或 None)。ctx: profile / nodes / reviews（refs_reviewed 解析结果）/ legacy。"""
    errs = []
    if not isinstance(item, dict):
        return ["item 不是对象"], None
    path, method = item.get("path"), item.get("method")
    if not isinstance(path, str) or not os.path.isabs(path):
        return [f"path 必须是绝对路径：{path!r}"], None
    if method not in ("delete", "native"):
        return [f"{path}：method 必须是 delete 或 native，收到 {method!r}"], None
    try:
        st = os.lstat(path)
    except OSError as exc:
        return [f"{path}：路径不存在或无法读取（{exc.strerror or exc}）"], None
    if stat.S_ISLNK(st.st_mode):
        return [f"{path}：item 路径本身是符号链接，不接受（请直接给出真实路径）"], None
    rp = os.path.realpath(path)
    reason = protect.protected_reason(rp, ctx["profile"])
    if reason:
        errs.append(f"{path}：属于保护路径（{reason}）")
    if not protect.under_roots(rp, ctx["profile"]):
        errs.append(f"{path}：不在 profile.roots 之内")

    if item.get("class") is not None and item.get("class") not in ITEM_CLASSES:
        errs.append(f"{path}：class 必须是 {'/'.join(ITEM_CLASSES)}，收到 {item.get('class')!r}")

    keep = item.get("keep", [])
    if not isinstance(keep, list) or not all(isinstance(k, str) and k for k in keep):
        errs.append(f"{path}：keep 必须是非空字符串数组")
        keep = []

    rec = {"path": path, "realpath": rp, "ino": st.st_ino, "dev": st.st_dev, "method": method}
    allow_recent = False
    if "run_authorization" in item:
        try:
            if not ctx.get("run"):
                raise ValueError("authorization: current run required")
            auth = run_authorization.validate(item, ctx["run"])
            if any(dependency_selected(dep, ctx) for dep in run_authorization.dependencies(item)):
                raise ValueError("清理范围包含本轮授权证据")
            allow_recent = auth.get("allow_recent") is True
            rec["run_authorization"] = dict(item["run_authorization"])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errs.append(f"{path}：{exc}")
    if "checkpoint" in item:
        ok, why = artifact_checkpoint.verify(item)
        dependencies = artifact_checkpoint.dependencies(item)
        if any(dependency_selected(dep, ctx) for dep in dependencies):
            ok, why = False, "清理范围包含复制件的保留仓库或任务回执"
        if not ok:
            errs.append(f"{path}：{why}")
        else:
            rec["checkpoint"] = dict(item["checkpoint"])
    if method == "native":
        argv = item.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            errs.append(f"{path}：native 的 argv 必须是非空字符串数组")
        else:
            mismatch = _native_target_mismatch(argv, path)
            if mismatch:
                errs.append(f"{path}：{mismatch}")
            else:
                rec["argv"] = list(argv)
                # 程序的绝对路径（找不到为 None）与强制环境变量记入封存；act 执行前用同一解析规则复核，路径变了就不执行
                rec["prog"] = protect.resolve_prog(argv[0])
                shape = protect.native_match(argv)[0]
                rec["env_forced"] = shape.forced_env(protect.HOME)
    else:
        scope = item.get("scope")
        if scope not in SCOPES:
            errs.append(f"{path}：scope 必须是 {'/'.join(SCOPES)}，收到 {scope!r}")
        else:
            rec["scope"] = scope
            rec["keep"] = keep
        if scope == "files":
            files = item.get("files")
            if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
                errs.append(f"{path}：scope=files 必须给出非空的 files 绝对路径数组")
            else:
                recs = []
                for f in files:
                    if not os.path.isabs(f):
                        errs.append(f"{path}：files 含非绝对路径 {f!r}")
                        continue
                    cf = canon_nofollow(f)
                    if not within(cf, rp):
                        errs.append(f"{f}：不在 item 路径 {rp} 之内")
                        continue
                    try:
                        fst = os.lstat(cf)
                    except OSError as exc:
                        errs.append(f"{f}：文件不存在（{exc.strerror or exc}）")
                        continue
                    if stat.S_ISDIR(fst.st_mode):
                        errs.append(f"{f}：files 只能列文件，不能列目录")
                        continue
                    pr = entry_protected(cf, ctx["profile"])
                    if pr:
                        errs.append(f"{f}：属于保护路径（{pr}）")
                        continue
                    recs.append({"path": cf, "size": fst.st_size, "mtime_ns": fst.st_mtime_ns})
                rec["files"] = recs

    # 事实节点核对
    primary, desc = related_nodes(rp, ctx["nodes"])
    # P5.R H1：风险画像三方合并（primary 节点 + 条目自己的规则 + 后代节点），不再只看最近的事实节点
    mp = merged_producer(rp, ctx["nodes"], (primary, desc))
    if mp["blockers"]:
        rec["blocked_by_processes"] = mp["blockers"]      # 封存进 seal，act 执行前按进程名复核
    if mp["chat"]:
        errs.append(f"{path}：{CHAT_NETDISK_REASON}，不可清理，整卡降为 keep")
    elif mp["risky"]:
        ctx.setdefault("soft", []).append(f"{path}：{RISKY_ZONE_REASON}")
    if mp["updater"]:
        uv = updater_verdict_for(rp, ctx)
        if uv != "superseded":
            ctx.setdefault("soft", []).append(f"{path}：{UPDATER_REASON}（探针结论 {uv}）")
    exact = primary is not None and os.path.normpath(primary.get("path", "")) == rp
    group = ([primary] if primary else []) + desc
    if not group:
        errs.append(f"{path}：facts 中没有覆盖该路径的节点，无法核对引用与占用")
    if primary is not None and not exact:
        # 条目在节点内部：祖先的占用/改动/受保护标记说的是兄弟项，不套到本条；
        # 改为现场核对本条自身（保护已由 protected_reason 直接判过，占用由 act 逐项 lsof）
        live_errs, live_bytes = _live_item_errors(rp, path, allow_recent)
        errs.extend(live_errs)
    for n in group:
        label = f"{path}（节点 {n.get('id', '?')} {n.get('path')}）"
        ir_item = (ctx.get("item_refs") or {}).get(rp)
        ir = ir_item if n is primary and not exact else None
        if _ir_usable(ir, ctx):
            # 定向引用检查（scan.py probe item-refs）按这一条自己的路径查过：零引用即已核实；有引用照常要复核样本
            if (ir.get("count") or 0) > 0 and not refs_covered({"refs": ir}, ctx):
                errs.append(f"{label}：定向引用检查发现 {ir.get('count')} 处引用，但卡片 evidence 中没有样本取自 item_refs.json 的 refs_reviewed（history_only）")
        elif n is primary and not exact:
            refs = n.get("refs") if isinstance(n.get("refs"), dict) else {}
            if refs.get("checked") is not True:
                # 节点仅因"包含受保护路径"（如 Application Support 含 MobileSync）被标 protected、因此没查引用：
                # 只拒绝整删这个节点本身，里面不受保护的条目按未做引用检查的普通节点处理（须有引用核对说明）
                ancestor_only = (refs.get("reason") == "protected" and n.get("protected")
                                 and protect.protection_kind(str(n.get("path", "")), ctx["profile"]) == "ancestor")
                if refs.get("reason") == "no-search-scope":
                    errs.append(f"{label}：所在节点扫描时没有可搜的引用检查目录（引用未查）；"
                                "在设置或台账里补上成品所在目录后，运行 scan.py probe item-refs 补查")
                elif refs.get("reason") != "non-frontier" and not ancestor_only:
                    errs.append(f"{label}：所在节点引用检查未完成（{refs.get('reason')}），不可补救")
                elif not refs_rescue(ctx, label):
                    errs.append(f"{label}：所在节点未做引用检查，卡片 evidence 中须有合法的 refs_reviewed（history_only）{_refs_hint(ctx)}")
            elif ctx.get("stale_roots"):
                errs.append(_stale_msg(label, ctx))
            elif (refs.get("count") or 0) > 0 and not refs_covered(n, ctx):
                errs.append(f"{label}：所在节点存在 {refs.get('count')} 处引用，但卡片 evidence 中没有样本取自该节点 refs.samples 的 refs_reviewed（history_only）{_refs_hint(ctx)}")
        else:
            nn = n
            node_refs = n.get("refs") if isinstance(n.get("refs"), dict) else {}
            ir_exact = ir_item if _ir_usable(ir_item, ctx) else None
            node_unsearched = (node_refs.get("checked") is not True
                               and node_refs.get("reason") in ("non-frontier", "no-search-scope"))
            node_stale = node_refs.get("checked") is True and bool(ctx.get("stale_roots"))
            if ir_exact is not None and ((n is primary and exact and node_unsearched) or node_stale):
                # 条目正好是一个非前沿节点本身（扫描时没查它自己的引用）、或扫描时没有可搜的目录 / 扫描后新增了成品目录：
                # 用 probe item-refs 按条目路径查的结果代替；占用、24 小时改动、受保护等节点级核对照旧
                nn = dict(n, refs=ir_exact)
                if (ir_exact.get("count") or 0) > 0 and not refs_covered({"refs": ir_exact}, ctx):
                    errs.append(f"{label}：定向引用检查发现 {ir_exact.get('count')} 处引用，但卡片 evidence 中没有样本取自 item_refs.json 的 refs_reviewed（history_only）")
            elif node_stale:
                errs.append(_stale_msg(label, ctx))
            errs.extend(_node_errors(nn, label, lambda: refs_rescue(ctx, label), allow_recent))
        refs = n.get("refs") if isinstance(n.get("refs"), dict) else {}
        count = refs.get("count")
        if refs.get("checked") is True:
            if not isinstance(count, int) or isinstance(count, bool):
                errs.append(f"{label}：refs.count 缺失")
            elif count > 0 and not refs_covered(n, ctx):
                errs.append(f"{label}：存在 {count} 处引用，但卡片 evidence 中没有样本取自该节点 refs.samples 的 refs_reviewed（history_only）{_refs_hint(ctx)}")
        if rec.get("scope") == "all" and (n is not primary or os.path.normpath(n.get("path", "")) == rp):
            tracked = (n.get("git") or {}).get("tracked_files")
            if isinstance(tracked, int) and tracked > 0:
                errs.append(f"{label}：含 {tracked} 个被 git 跟踪的文件，scope=all 不可用，请改用 ignored-only")

    if rec.get("scope") == "files" and isinstance(rec.get("files"), list):
        rec["bytes"] = sum(_alloc(f["path"]) for f in rec["files"])
    elif exact:
        rec["bytes"] = primary.get("bytes")
    elif primary is not None:
        rec["bytes"] = live_bytes
    elif desc:
        rec["bytes"] = sum(int(d.get("bytes") or 0) for d in desc)
    else:
        rec["bytes"] = None
    return errs, rec


ITEMS_FILE_MAX = 50000
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def load_plan(run):
    """读 plan.json，并把卡片的 items_file（run 目录内、由探针 --emit 生成的条目数组）展开进 items。
    返回 (plan, {card_id: [展开错误]})。"""
    plan = read_json(os.path.join(run, "plan.json"))
    errors = {}
    if not isinstance(plan, dict) or not isinstance(plan.get("cards"), list):
        return plan, errors
    for card in plan["cards"]:
        if not isinstance(card, dict):
            continue
        if isinstance(card.get("items"), list):       # 手写条目不得自带 _emit 标记（它只表示来自探针 items_file）
            card["items"] = [{k: v for k, v in it.items() if k != "_emit"} if isinstance(it, dict) else it
                             for it in card["items"]]
        if "items_file" not in card:
            continue
        cid = card.get("id")
        files = card["items_file"] if isinstance(card["items_file"], list) else [card["items_file"]]
        items = list(card.get("items") or [])
        for rel in files:
            if not isinstance(rel, str) or os.path.isabs(rel):
                errors.setdefault(cid, []).append(f"items_file 必须是 run 目录内的相对路径：{rel!r}")
                continue
            fp = os.path.realpath(os.path.join(run, rel))
            if not within(fp, run):
                errors.setdefault(cid, []).append(f"items_file 越出 run 目录：{rel}")
                continue
            try:
                data = read_json(fp)
            except ValueError as exc:
                errors.setdefault(cid, []).append(f"items_file 读不出：{rel}（{exc}）")
                continue
            if not isinstance(data, list) or len(data) > ITEMS_FILE_MAX:
                errors.setdefault(cid, []).append(f"items_file 必须是不超过 {ITEMS_FILE_MAX} 条的数组：{rel}")
                continue
            items.extend(dict(d, _emit=True) if isinstance(d, dict) else d for d in data)    # 探针 --emit 来源标记
        card["items"] = items
    return plan, errors


def _unreadable(msg, scan_id=None):
    return {"schema": "plan.check/1", "scan_id": scan_id, "checked_at": now_iso(), "ok": False,
            "plan_errors": [msg], "cards": [], "errors": [], "counts": {}}, {}


# ---- 降级原因 -> judgment.md 小节引用 ----
# producer.safety=risky 的两种降级原因（check_item 写入，DOWNGRADE_PATTERNS 靠这两句识别）
UNVERIFIED_REFS_REASON = "这批内容的引用没有被核实"
CHAT_NETDISK_REASON = "聊天记录与网盘本地文件只走软件自身入口"
RISKY_ZONE_REASON = "该位置属于用户资产或高风险区，需用户明确选择"
UPDATER_REASON = "更新暂存只有 probe updaters 现场判为 superseded（已装同版或更新）才可直接清，否则降为 decide"


def updater_verdict_for(rp, ctx):
    """现场跑一次 probe updaters（每次 check 只跑一次），返回与 rp 重叠的候选的结论：
    全部 superseded 才是 superseded；没有重叠候选为 not_found；探针出错为 probe_error。卡上手写的 updater_verdict 不作数。"""
    if "updaters" not in ctx:
        try:
            import updaters
            ctx["updaters"] = [(os.path.normpath(protect.real(i["path"])), i.get("verdict"))
                               for i in updaters.probe_updaters(None).get("items", []) if i.get("path")]
        except Exception:                 # noqa: BLE001  探针失败按未核实处理
            ctx["updaters"] = None
    if ctx["updaters"] is None:
        return "probe_error"
    hits = [v for cp, v in ctx["updaters"]
            if cp == rp or cp.startswith(rp.rstrip("/") + "/") or rp.startswith(cp.rstrip("/") + "/")]
    if not hits:
        return "not_found"
    return "superseded" if all(v == "superseded" for v in hits) else next(v for v in hits if v != "superseded")
JUDGMENT_MD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "references", "judgment.md")
# (原因里的特征子串, 类型)，按顺序匹配，先到先得
DOWNGRADE_PATTERNS = [
    ("项目「", "project_status"),
    ("项目识别结果", "project_status"),
    (CHAT_NETDISK_REASON, "chat_netdisk"),
    (RISKY_ZONE_REASON, "risky_zone"),
    (UPDATER_REASON, "updater_unverified"),
    ("最多一个交互会话", "multi_session"),
    ("含交互会话条目", "interactive_session"),
    ("任务已结束", "end_evidence"),
    ("depends", "refs_depends"),
    ("class 必须是", "class_invalid"),
    ("引用", "refs_unreviewed"),
    ("refs.count", "refs_unreviewed"),
    ("属于保护路径", "protected"),
    ("受保护", "protected"),
    ("git 跟踪", "tracked"),
    ("24 小时", "in_use"),
    ("recent_24h", "in_use"),
    ("进程打开", "in_use"),
]
# 类型 -> (judgment.md 小节标题, 用于在该小节内定位那一句的关键字)；原文运行时读取，不在此硬编码
JUDGMENT_MAP = {
    "end_evidence": ("每个大节点都要回答的四个问题", "结束要有正向证据"),
    "interactive_session": ("每个大节点都要回答的四个问题", "缺一项，就写 `decide`"),
    "multi_session": ("每个大节点都要回答的四个问题", "现在谁还在读它"),
    "refs_depends": ("硬规则", "看完发现确有依赖就写"),
    "refs_unreviewed": ("硬规则", "先读 `samples`"),
    "protected": ("硬规则", "保护名单内的路径"),
    "tracked": ("硬规则", "仓库里的中间稿只用"),
    "in_use": ("硬规则", "recent_24h > 0"),
    "class_invalid": ("写卡片", "`items`：优先用原生命令"),
    # P5.2 缺口 2：producer.safety=risky 的节点。judgment.md 第 7 节“浏览器与聊天软件的分层原则”的第 3、2 层
    "chat_netdisk": ("浏览器与聊天软件的分层原则", "账号与聊天内容层"),
    "risky_zone": ("浏览器与聊天软件的分层原则", "用户数据层"),
    "updater_unverified": ("每个大节点都要回答的四个问题", "缺一项，就写 `decide`"),
    # 0.5：项目台账。进行中 / 别碰的项目不出删除卡
    "project_status": ("项目状态怎么用", "进行中的项目"),
}


def judgment_ref(kind, text=None):
    """返回 {"section","title","quote"}；小节或句子找不到时 quote 写明未找到。"""
    title, kw = JUDGMENT_MAP[kind]
    try:
        text = open(JUDGMENT_MD, encoding="utf-8").read() if text is None else text
    except OSError:
        text = ""
    section, body, cur = None, [], None
    for line in text.splitlines():
        m = re.match(r"##\s+(\d+)\.\s+(.*\S)\s*$", line)
        if m:
            cur = m.group(1) if m.group(2) == title else None
            if cur:
                section = cur
            continue
        if cur and section:
            body.append(line)
    miss = {"section": None, "title": title, "quote": f"judgment.md 未找到对应小节：{title}"}
    if section is None:
        return miss
    for line in body:
        if kw in line:
            line = re.sub(r"^\s*(?:- |\d+\.\s)", "", line).strip()
            pos = line.index(kw)
            start = line.rfind("。", 0, pos) + 1
            end = line.find("。", pos)
            return {"section": section, "title": title,
                    "quote": line[start:end + 1 if end >= 0 else len(line)].strip()}
    return {"section": section, "title": title, "quote": f"judgment.md 未找到对应小节：{title}（定位句：{kw}）"}


def judgment_refs_for(messages):
    kinds = []
    for m in messages:
        for pat, kind in DOWNGRADE_PATTERNS:
            if pat in m:
                if kind not in kinds:
                    kinds.append(kind)
                break
    return [judgment_ref(k) for k in kinds]


# ----------------------------------------------------------------- 项目台账（0.5）

def load_projects_file(run, scan_id):
    """scan.py probe projects 的结果（<run>/projects.json）；scan_id 对不上（换了扫描）就不认，返回 None。"""
    try:
        data = read_json(os.path.join(run, "projects.json"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != "projects/1" or data.get("scan_id") != scan_id:
        return None
    return data if isinstance(data.get("projects"), list) else None


class ProjectIndex:
    """台账（profile.projects）+ 本次扫描认出的项目（projects.json）：判断一个条目属于哪个项目。

    归属有三种：① 条目路径与项目目录重叠（在里面；或条目把整个“进行中 / 别碰”项目包含在内）；
    ② Codex 会话 / 生成图条目的会话号属于该项目（projects.json 的 session_ids）；
    ③ 路径在 ~/.claude/projects/<目录> 下，该目录属于该项目（projects.json 的 claude_dirs）。
    没有 projects.json 时只能做 ①（台账里的路径本身）。"""

    def __init__(self, profile, pfile=None):
        self.entries = []
        ledger = profile.get("projects") if isinstance(profile, dict) else None
        scanned = {p.get("key"): p for p in (pfile or {}).get("projects", []) if isinstance(p, dict)}
        for key, e in (ledger.items() if isinstance(ledger, dict) else []):
            if not (isinstance(e, dict) and e.get("status") in protect.PROJECT_STATUSES):
                continue
            sc = scanned.get(key) or {}
            roots = [os.path.realpath(r) for r in protect.project_roots(key)]
            roots += [os.path.realpath(r) for r in (sc.get("roots") or []) if isinstance(r, str)]
            self.entries.append({
                "key": key, "status": e["status"], "name": e.get("name") or sc.get("name") or os.path.basename(key) or key,
                "roots": list(dict.fromkeys(roots)),
                "sessions": {x.lower() for x in (sc.get("session_ids") or []) if isinstance(x, str)},
                "claude_dirs": {x for x in (sc.get("claude_dirs") or []) if isinstance(x, str)}})

    def match(self, item, rp):
        """条目属于哪些项目：返回台账条目列表（路径归属取最深的一个项目 + 所有被整个包含的进行中/别碰项目 + 会话归属）。"""
        if not self.entries:
            return []
        hits, best = {}, None
        for e in self.entries:
            for root in e["roots"]:
                if within(rp, root):
                    if best is None or len(root) > best[0]:
                        best = (len(root), e)
                elif within(root, rp) and e["status"] in ("active", "protected"):
                    hits[e["key"]] = e
        if best:
            hits[best[1]["key"]] = best[1]
        cp = os.path.join(protect.HOME, ".claude", "projects") + "/"
        if rp.startswith(cp):
            top = rp[len(cp):].split("/", 1)[0]
            for e in self.entries:
                if top in e["claude_dirs"]:
                    hits[e["key"]] = e
        ids = set()
        for cand in (item.get("session"), os.path.basename(rp),
                     (item.get("argv") or [None])[-1] if isinstance(item.get("argv"), list) and item.get("argv")
                     and item["argv"][0] == "codex" else None):
            for m in UUID_RE.findall(str(cand or "")):
                ids.add(m.lower())
        if ids:
            for e in self.entries:
                if ids & e["sessions"]:
                    hits[e["key"]] = e
        return list(hits.values())


PROJECTS_MISSING_REASON = "台账里有进行中或别碰的项目，但这次扫描还没有项目识别结果（projects.json），无法确认这批东西不属于它们"
PROJECT_ACTIVE_REASON = "还在进行中（台账里你标的状态），项目里的东西不出删除卡"
PROJECT_PROTECTED_REASON = "你标了“别碰”，整张卡保留"


def project_item_errors(path, entries, checkpoint_ok=False):
    """进行中 / 别碰项目里的条目：返回 [错误]。做完了、归档不在这里挡（它们不改变放行条件，只影响提案）。"""
    out = []
    for e in entries:
        if e["status"] == "protected":
            out.append(f"{path}：项目「{e['name']}」{PROJECT_PROTECTED_REASON}")
        elif e["status"] == "active" and not checkpoint_ok:
            out.append(f"{path}：项目「{e['name']}」{PROJECT_ACTIVE_REASON}")
    return out


def load_item_refs(run, scan_id):
    """scan.py probe item-refs 的结果；scan_id 对不上（换了扫描）就不认。"""
    try:
        data = read_json(os.path.join(run, "item_refs.json"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema") != "item-refs/1" or data.get("scan_id") != scan_id:
        return {}
    items = data.get("items")
    return items if isinstance(items, dict) else {}


_MSG_PATH = re.compile(r"^(?P<path>[/~][^：]*?)(?:（节点 [^）]*）)?：(?P<reason>.+)$", re.S)
_MSG_PREFIXES = ("风险提示：",)
_PATH_IN_REASON = re.compile(r"[/~][^\s，、；：（）()]+")
SUMMARY_EXAMPLES = 3


def split_check_message(msg):
    """“路径（节点 …）：原因” → (路径, 原因)；没有路径前缀的卡片级消息返回 (None, 原文)。“风险提示：”前缀留在原因里。"""
    msg = str(msg)
    prefix = next((p for p in _MSG_PREFIXES if msg.startswith(p)), "")
    m = _MSG_PATH.match(msg[len(prefix):])
    if m:
        return m.group("path"), prefix + m.group("reason")
    return None, msg


def summarize_messages(msgs, examples=SUMMARY_EXAMPLES):
    """按原因汇总逐条目的消息：[{reason, count, examples（最多 3 个路径）, paths（全部路径）}]，条数多的在前。
    原因里的数字和路径先归一化再比较（“存在 3 处引用”“存在 5 处引用”算同一种），显示第一条的原文。"""
    groups = {}
    for msg in msgs or []:
        path, reason = split_check_message(msg)
        key = re.sub(r"\d+", "#", _PATH_IN_REASON.sub("<路径>", reason))
        g = groups.setdefault(key, {"reason": reason, "count": 0, "examples": [], "paths": []})
        g["count"] += 1
        if path is not None and path not in g["paths"]:
            g["paths"].append(path)
    out = sorted(groups.values(), key=lambda g: -g["count"])
    for g in out:
        g["examples"] = g["paths"][:examples]
    return out


_ASCII_PAREN = re.compile(r"（[\x00-\x7f]+）")
_NODE_PAREN = re.compile(r"（节点 [^）]*）")
_PROJECT_NAME = re.compile(r"「([^」]*)」")
DOWN_REASON_LIMIT = 3
DOWN_REASON_MAXLEN = 80


def plain_reason(reason):
    """降级原因 → 给人看的一句话：去掉括号里的英文系统错误、节点标注与路径（路径在详情里看）。"""
    t = _NODE_PAREN.sub("", str(reason))
    t = _ASCII_PAREN.sub("", t)
    t = _PATH_IN_REASON.sub("相关文件", t)
    t = re.sub(r"(相关文件[、，\s]*)+(?=相关文件)", "", t)
    t = t.replace("风险提示：", "").strip(" ；;，,：:")
    return t if len(t) <= DOWN_REASON_MAXLEN else t[:DOWN_REASON_MAXLEN - 1] + "…"


def down_reasons(summary, limit=DOWN_REASON_LIMIT):
    """check 的 error_summary → 看板卡片上的原因短句：按人话去重（同一原因不按条目、不按项目重复），最多 limit 条。
    返回 (短句列表, 被省略的原因种数)。"""
    groups, order = {}, []
    for g in summary or []:
        text = plain_reason(g.get("reason", ""))
        if not text:
            continue
        names = _PROJECT_NAME.findall(text)
        key = re.sub(r"\d+", "#", _PROJECT_NAME.sub("「#」", text))
        if key not in groups:
            groups[key] = {"text": text, "names": []}
            order.append(key)
        for n in names:
            if n not in groups[key]["names"]:
                groups[key]["names"].append(n)
    out = []
    for key in order:
        g = groups[key]
        text = g["text"]
        if len(g["names"]) > 1:
            text = text.replace(f"项目「{g['names'][0]}」", f"{len(g['names'])} 个项目（如「{g['names'][0]}」）", 1)
        out.append(text)
    return out[:limit], max(0, len(out) - limit)


def bytes_hint(items, nodes):
    """被降级卡没有封存条目字节：用扫描时量过的节点体积（路径逐字相同）、其次条目自带的 bytes 估一个；都没有返回 None。"""
    by_path = {n.get("path"): n.get("bytes") for n in nodes if isinstance(n, dict) and isinstance(n.get("bytes"), int)}
    total, seen, found = 0, set(), False
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not isinstance(it.get("path"), str) or it["path"] in seen:
            continue
        seen.add(it["path"])
        b = by_path.get(it["path"])
        if b is None and isinstance(it.get("bytes"), int) and not isinstance(it.get("bytes"), bool):
            b = it["bytes"]
        if b is not None:
            total, found = total + b, True
    return total if found else None


def check_plan(run):
    """返回 (check 结果, {card_id: 可封存记录})。"""
    bad_home = protect.home_problem()
    if bad_home:
        return _unreadable(bad_home)
    try:
        facts = read_json(os.path.join(run, "facts.json"))
        plan, file_errors = load_plan(run)
    except ValueError as exc:
        return _unreadable(str(exc))
    scan_id = facts.get("scan_id") if isinstance(facts, dict) else None
    plan_errors = []
    if not isinstance(plan, dict) or plan.get("schema") != "plan/1":
        plan_errors.append(f"plan.json 的 schema 必须是 plan/1，收到 {plan.get('schema') if isinstance(plan, dict) else type(plan).__name__}")
    elif plan.get("scan_id") != scan_id:
        plan_errors.append(f"scan_id 不一致：plan={plan.get('scan_id')!r} facts={scan_id!r}")
    cards = plan.get("cards") if isinstance(plan, dict) else None
    if not isinstance(cards, list):
        plan_errors.append("plan.cards 必须是数组")
    nodes = facts.get("nodes") if isinstance(facts, dict) else None
    if not isinstance(nodes, list):
        plan_errors.append("facts.nodes 必须是数组")
    if plan_errors:
        res, rec = _unreadable("; ".join(plan_errors), scan_id)
        res["plan_errors"] = plan_errors
        return res, rec

    targets = cleanup_targets(cards)
    profile = load_profile()
    item_refs = load_item_refs(run, scan_id)
    # 扫描时搜过哪些引用检查目录（facts.coverage.refs_search）；现在的有效设置里多出来的就是“扫描后新增的成品目录”。
    # 旧格式 facts 没有这项记录，无从比较，不判（scan.py 产出的 facts 一定有）。
    rs = (facts.get("coverage") or {}).get("refs_search") if isinstance(facts, dict) else None
    cur_roots = protect.ref_roots_real(profile)
    stale_roots = protect.roots_uncovered(cur_roots, rs.get("roots")) if isinstance(rs, dict) else []
    pfile = load_projects_file(run, scan_id)
    pindex = ProjectIndex(profile, pfile)
    # 台账里有“进行中 / 别碰”的项目，而这次扫描没有（或对不上）项目识别结果：只靠台账里写的路径认不全归属
    # （会话号、Claude 项目目录要 projects.json 才认得出），不能放行任何删除卡
    projects_missing = pfile is None and any(e["status"] in ("active", "protected") for e in pindex.entries)
    S = settings_of(profile)
    out_cards, flat_errors, flat_warnings, records, seen = [], [], [], {}, set()
    counts = {}
    for idx, card in enumerate(cards):
        errs = []
        cid = card.get("id") if isinstance(card, dict) else None
        if not isinstance(card, dict):
            card = {}
        if not isinstance(cid, str) or not cid:
            errs.append(f"第 {idx + 1} 张卡片缺少 id")
            cid = f"#{idx + 1}"
        elif cid in seen:
            errs.append(f"卡片 id 重复：{cid}")
        seen.add(cid)
        errs.extend(file_errors.get(cid, []))
        verdict = card.get("verdict")
        if verdict not in VERDICTS:
            errs.append(f"verdict 必须是 {'/'.join(VERDICTS)}，收到 {verdict!r}")
        items = card.get("items") or []
        recs = []
        ev_checks, soft, warns, risk_notes = [], [], [], []
        card_projects, project_cap, prefill = [], None, None
        setting_prefill, setting_q, wt_ask, proj_done = None, None, False, False
        if "default_select" in card:
            warns.append("default_select 由 plan.py 按规则计算，plan.json 里写的值被忽略")
        isess = interactive_sessions(items)
        if verdict in ACTIONABLE:
            if not isinstance(items, list) or not items:
                errs.append("clean/decide 卡片必须带非空 items")
            if verdict == "decide" and not (isinstance(card.get("question"), str) and card["question"].strip()):
                errs.append("decide 卡片必须有 question")
            evidence = card.get("evidence") if isinstance(card.get("evidence"), list) else []
            reviews = []
            for ei, e in enumerate(evidence):
                if isinstance(e, str) and e.startswith(EVIDENCE_PREFIXES):
                    warns.append("evidence 用了旧前缀「引用已核对:」，不再放行，按未核对处理；"
                                 "请改用 {\"kind\":\"refs_reviewed\",\"samples\":[取自 facts 的 refs.samples],\"verdict\":\"history_only|depends\"}")
                elif isinstance(e, dict) and e.get("kind") == "refs_reviewed":
                    r = parse_refs_review(e)
                    r["idx"] = ei
                    reviews.append(r)
                    if r["err"]:
                        warns.append(f"refs_reviewed：{r['err']}，按未核对处理")
            ctx = {"profile": profile, "nodes": nodes, "reviews": reviews, "matched": set(), "item_refs": item_refs,
                   "cur_roots": cur_roots, "stale_roots": stale_roots, "run": run,
                   "legacy": any(isinstance(e, str) and e.startswith(EVIDENCE_PREFIXES) for e in evidence),
                   "soft": [], "cleanup_paths": targets["trees"], "cleanup_files": targets["files"]}
            if any(r["verdict"] == "depends" for r in reviews):
                errs.append("refs_reviewed 结论为 depends：引用对象仍依赖这批东西，整卡降为 keep")
            young = _retention_hit(items, nodes, profile) if isess else None
            if young:
                errs.append(young)
            if len(isess) > 1:
                errs.append(f"一张卡最多一个交互会话条目，这里有 {len(isess)} 个会话（{', '.join(sorted(isess))[:200]}）；"
                            "交互对话逐个会话出卡、逐个问")
            if isinstance(items, list):
                errs.extend(_codex_tree_errors(items))
            for i, it in enumerate(items if isinstance(items, list) else []):
                ie, rec = check_item(it, ctx)
                errs.extend(ie)
                if rec is not None:
                    rec["index"] = i
                    recs.append(rec)
            if projects_missing:
                errs.append(PROJECTS_MISSING_REASON + f"：先运行 scan.py probe projects --run {run}，再重新 check")
            # 项目台账：进行中 / 别碰项目里的条目整卡保留；做完了 / 归档只记录归属，供提案使用（不改变放行条件）
            if pindex.entries and isinstance(items, list):
                seen_proj, per_item = {}, []
                for it in items:
                    if not (isinstance(it, dict) and isinstance(it.get("path"), str) and os.path.isabs(it["path"])):
                        per_item.append([])
                        continue
                    hit = pindex.match(it, os.path.realpath(it["path"]))
                    per_item.append(hit)
                    checkpoint_ok = any(r.get("path") == it["path"] and r.get("checkpoint") for r in recs)
                    errs.extend(project_item_errors(it["path"], hit, checkpoint_ok))
                    for e in hit:
                        seen_proj.setdefault(e["key"], {"key": e["key"], "name": e["name"], "status": e["status"]})
                card_projects = sorted(seen_proj.values(), key=lambda x: x["key"])
                if per_item and all(per_item):
                    sts = {e["status"] for hit in per_item for e in hit}
                    if sts == {"archived"}:
                        project_cap = "recommended"
                    if sts <= {"done", "archived"}:
                        proj_done = True
                        # 做完的项目素材怎么处理：删中间稿（预填“删”）/ 整个搬走（预填“归档”）/ 先都留着（不预填）
                        prefill = {"archive": "归档", "keep": None}.get(S["done_project"], "删")
            for it in items if isinstance(items, list) else []:
                cw = class_conflict(it)
                if cw and cw not in warns:
                    warns.append(cw)
            for r in reviews:
                if (r["verdict"] == "history_only" and not r["err"] and r["idx"] not in ctx["matched"]
                        and r["idx"] not in ctx.get("rescued", ())):
                    warns.append("refs_reviewed 的 samples 与 facts 里相关节点的 refs.samples 无交集，视为伪造，按未核对处理")
            notes = list(dict.fromkeys(ctx["soft"]))
            if verdict == "clean":
                soft.extend(notes)
            else:
                # P5.R M3：Agent 直接写成 decide 的卡也要让用户看见风险（风险区、引用没核实、更新暂存未确认）
                risk_notes = notes
                warns.extend(f"风险提示：{n}" for n in notes)
            ev_checks, has_end, needs_end = evidence_review(card, items, nodes, profile, {r["path"] for r in recs if r.get("checkpoint")})
            if verdict == "clean" and isess:
                soft.append("含交互会话条目（class=interactive）：人还可能回来续聊，不得直接判 clean，降为 decide 由你确认")
            if verdict == "clean" and needs_end and not has_end:
                # 判断准则第 3 问：结束要有正向证据。字符串/user_stated 不算，对象证据复核没过也不算
                soft.append("clean 需要「任务已结束」的复核证据（merged / delivered / copy_verified），"
                            "现有证据没有一条通过复核（字符串与 user_stated 不算），降为 decide")
                soft.extend(c["msg"] for c in ev_checks if not c["ok"])
            # ---- 初始设置（0.6）：只会让卡更保守或预填，见上面 settings_of 的说明
            used0, cat = _first_card(card), card_category(card)[0]
            real_items = [i for i in items if isinstance(i, dict) and isinstance(i.get("path"), str)] if isinstance(items, list) else []
            if isess and S["conv"] == "library":
                errs.append(LIBRARY_REASON)
            if used0 in S["tools_unused"]:
                setting_prefill = "删"                       # 软件标了“不用了”：整套环境进入判断，仍由你逐项确认
            if cat == "ai-images":
                if S["rejects"] == "drop":
                    setting_prefill = "删"
                elif S["rejects"] == "keep" and not any(c.get("end") and c.get("kind") in ("copy_verified", "delivered")
                                                        for c in ev_checks):
                    errs.append(REJECTS_KEEP_REASON)
            if used0 == "large-old-files":
                if S["big_days"] == 0:
                    errs.append(BIG_OFF_REASON)
                elif S["big_days"]:
                    for it in real_items:
                        mt = _item_newest_ts(it, nodes)
                        if mt is not None and (time.time() - mt) / 86400 < S["big_days"]:
                            errs.append(f"{it['path']}：不到你定的 {S['big_days']} 天（最近 {(time.time() - mt) / 86400:.0f} 天内还有改动）")
            if used0 == "git-worktree-build":
                if S["worktree"] == "whole":
                    setting_prefill = "删"
                elif S["worktree"] == "ask":
                    wt_ask = True
                elif S["worktree"] == "build_only" and verdict == "clean" \
                        and any(_is_worktree_root(os.path.realpath(i["path"]), nodes)
                                and not (i.get("method") == "delete" and i.get("scope") == "files")
                                for i in real_items):
                    soft.append(WT_BUILD_ONLY_REASON)
                    setting_q = WT_BUILD_ONLY_QUESTION
        elif verdict == "govern" and items:
            errs.append("govern 卡片只放进 governance，不应带 items")
        out = verdict
        downgraded = False
        if errs:
            if verdict in ACTIONABLE or verdict == "govern" or verdict not in VERDICTS:
                out, downgraded = "keep", True
            recs = []
        elif soft:
            out, downgraded, errs = "decide", True, soft
            records[cid] = {"title": card.get("title"), "verdict": out, "items": recs}
        elif verdict in ACTIONABLE:
            records[cid] = {"title": card.get("title"), "verdict": verdict, "items": recs}
        if cid in records:
            # 0.4 评审：提案的档位、规则键、要不要逐个问都从封存推导，封存后改 plan.json 改不动执行范围
            preset = _floor_preset(card, card_preset(card, records[cid]["verdict"], risk_notes, cap=project_cap), wt_ask)
            records[cid]["preset"] = preset
            # 台账“归档”会把 all 档的卡降到推荐档；“删了找不回”的提醒按卡原本的风险判定，不按降档后的档位
            records[cid]["preset_raw"] = _floor_preset(card, card_preset(card, records[cid]["verdict"], risk_notes), wt_ask)
            if card_projects:
                records[cid]["projects"] = card_projects
            # 预填只是预填，仍要用户确认：项目做完了（按“做完的项目怎么处理”预填删或归档；含交互对话的卡永不预填）；其次是初始设置里的“不用了的软件 / 废稿不留 / 整棵工作树删”
            pf = None
            if prefill and not isess:          # 含交互对话的卡永不预填（逐个确认），conversation_policy=done 也一样
                pf = prefill
            if pf is None and setting_prefill and not isess:
                pf = setting_prefill
            if pf and records[cid]["verdict"] == "decide" and not risk_notes:
                records[cid]["prefill"] = pf
            records[cid]["rule_key"] = prefs.rule_key(card, preset, card_category(card)[0])
            # 降过级或带风险：本机“以后都删”规则不适用；工作树选了“每次问我”也不适用
            records[cid]["review"] = bool(downgraded or risk_notes or wt_ask)
            if _installer_card(card, items):
                records[cid]["installer"] = True          # 提案里“安装包直接清”据此按规则处理
            records[cid]["interactive"] = bool(isess)                     # 含交互会话：只在看板里逐个确认
        counts[out] = counts.get(out, 0) + 1
        for e in errs:
            flat_errors.append({"card": cid, "message": e})
        oc = {"id": cid, "title": card.get("title"), "verdict_in": verdict,
              "verdict_out": out, "downgraded": downgraded, "errors": errs,
              "default_select": compute_default_select(card, items, out, nodes)}
        if cid in records:
            # 实测体积（封存条目字节之和）：看板与提案都用它，不用 Agent 在 plan.json 里写的 bytes（试用发现：没写就全是 0）
            oc["bytes_checked"] = sum(int(r.get("bytes") or 0) for r in records[cid]["items"])
        if ev_checks:
            oc["evidence_checks"] = ev_checks
        if card_projects:
            oc["projects"] = card_projects
        if cid in records:
            oc["preset"], oc["preset_raw"] = records[cid]["preset"], records[cid]["preset_raw"]
            if project_cap:
                oc["project_cap"] = project_cap
            if records[cid].get("prefill"):
                oc["prefill"] = records[cid]["prefill"]
        if downgraded:
            oc["judgment_refs"] = judgment_refs_for(errs)
            if cid not in records:
                hint = bytes_hint(items, nodes)
                if hint is not None:
                    oc["bytes_hint"] = hint
        if errs:
            oc["error_summary"] = summarize_messages(errs)        # 按原因汇总；完整明细在 errors 与各组的 paths
        warns = list(dict.fromkeys(warns))                       # 同一张卡里完全相同的提示只留一条
        if warns:
            oc["warnings"] = warns
            oc["warning_summary"] = summarize_messages(warns)
            flat_warnings.extend({"card": cid, "message": w} for w in warns)
        if risk_notes:
            oc["risk_notes"] = risk_notes
        if soft or risk_notes:
            q = card.get("question")
            q = q.strip() if isinstance(q, str) and q.strip() else ""
            allm = soft + risk_notes
            risk_q = ("这里面有你的账号数据或用户资产，你明确要清理它们吗？" if any(RISKY_ZONE_REASON in m for m in allm)
                      else "这批内容的引用没有被核实，你确认没有别的项目还在用它们吗？" if any(UNVERIFIED_REFS_REASON in m for m in allm)
                      else "")
            if risk_notes and verdict == "decide":
                oc["question"] = (risk_q + " " + q).strip() if risk_q else (q or "请决定是否清理")
            else:
                default_q = ("这个对话会话你确认不再需要、不会回来续聊了吗？" if isess
                             else risk_q or "这批东西对应的任务确实已经结束、不会再续跑了吗？")
                oc["question"] = setting_q or q or default_q
        out_cards.append(oc)
    result = {"schema": "plan.check/1", "scan_id": scan_id, "checked_at": now_iso(), "ok": True,
              "plan_errors": [], "cards": out_cards, "errors": flat_errors, "warnings": flat_warnings, "counts": counts}
    return result, records


_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _group_lines(groups, indent="      "):
    lines = []
    for i, g in enumerate(groups):
        mark = _CIRCLED[i] if i < len(_CIRCLED) else f"{i + 1}."
        line = f"{indent}{mark} {g['reason']}（{g['count']} 条）"
        if g["examples"]:
            more = "等" if len(g["paths"]) > len(g["examples"]) else ""
            line += "  例：" + "、".join(g["examples"]) + more
        lines.append(line)
    return lines


def _print_check(result):
    if result["plan_errors"]:
        print("计划不合法：")
        for e in result["plan_errors"]:
            print(f"  - {e}")
        return
    c = result["counts"]
    print(f"校验完成：共 {len(result['cards'])} 张卡片；"
          + "，".join(f"{k}={v}" for k, v in sorted(c.items())))
    detail = False
    for card in result["cards"]:
        if card["downgraded"]:
            groups = card.get("error_summary") or summarize_messages(card["errors"])
            total = sum(g["count"] for g in groups)
            print(f"  [降级为 {card['verdict_out']}] {card['id']} {card['title']}：{total} 条，原因 {len(groups)} 种")
            for line in _group_lines(groups):
                print(line)
            detail = detail or any(g["count"] > len(g["examples"]) for g in groups)
            for r in card.get("judgment_refs", []):
                sec = f"judgment.md §{r['section']}" if r.get("section") else "judgment.md"
                print(f"      依据 {sec}「{r['title']}」：{r['quote']}")
    for card in result["cards"]:
        groups = card.get("warning_summary") or summarize_messages(card.get("warnings") or [])
        if groups:
            print(f"  [警告] {card['id']}：{len(groups)} 种")
            for line in _group_lines(groups):
                print(line)
            detail = detail or any(g["count"] > len(g["examples"]) for g in groups)
    if detail:
        print("  （每种原因只列前 3 个示例；完整明细在 plan.check.json 各卡的 errors / error_summary[].paths）")


def cmd_check(args):
    run = os.path.realpath(args.run)
    result, _ = check_plan(run)
    write_json(os.path.join(run, "plan.check.json"), result)
    _print_check(result)
    print(f"结果：{os.path.join(run, 'plan.check.json')}")
    return 0 if result["ok"] else 1


def cmd_seal(args):
    run = os.path.realpath(args.run)
    result, records = check_plan(run)
    write_json(os.path.join(run, "plan.check.json"), result)
    _print_check(result)
    if not result["ok"]:
        return 1
    seal = {"schema": "seal/1", "scan_id": result["scan_id"], "created_at": now_iso(), "cards": records,
            "profile_guard": protect.profile_guard(load_profile())}
    seal["digest"] = digest_of(seal)
    write_json(os.path.join(run, "seal.json"), seal)
    n_items = sum(len(c["items"]) for c in records.values())
    print(f"已封存 {len(records)} 张卡片、{n_items} 个条目")
    if not records:
        print("警告：没有任何卡片通过校验，封存内容为空")
    print(f"seal: {seal['digest']}")
    return 0


# ----------------------------------------------------------------- 看板

COMPOSITION_NOTE_SYSTEM = "系统区域没有单独量，算在“其他”里"
_USER_DIRS = ("Documents", "Desktop", "Movies", "Pictures", "Music")


def composition_of(facts):
    """整盘构成（看板总览）：你的文件与软件数据 / 应用程序 / 系统 / 其他（含可清除空间）。
    home = 扫描根里落在家目录下的根节点合计；apps = /Applications 下的根节点合计（没扫就是 None）；
    系统区域扫描没有单独量过，不编数字：bytes 为 None 并标注，剩下的差额全部算“其他”，并标 estimated。"""
    disk = facts.get("disk") if isinstance(facts.get("disk"), dict) else {}
    used = disk.get("used") if isinstance(disk.get("used"), int) and not isinstance(disk.get("used"), bool) else 0
    home_dir = os.path.realpath(str((facts.get("host") or {}).get("home") or ""))
    roots = [n for n in facts.get("nodes", []) if isinstance(n, dict) and n.get("depth") == 0
             and isinstance(n.get("bytes"), int)]
    home_nodes = [n for n in roots if home_dir and within(os.path.realpath(str(n.get("path"))), home_dir)]
    app_nodes = [n for n in roots if within(os.path.realpath(str(n.get("path"))), "/Applications")]
    home = sum(n["bytes"] for n in home_nodes) if home_nodes else None
    apps = sum(n["bytes"] for n in app_nodes) if app_nodes else None
    other = max(0, used - (home or 0) - (apps or 0)) if used and home is not None else None
    return [
        {"id": "home", "name": "你的文件与软件数据", "bytes": home,
         "estimated": any(n.get("bytes_lower_bound") for n in home_nodes)},
        {"id": "apps", "name": "应用程序", "bytes": apps, "estimated": apps is None or any(n.get("bytes_lower_bound") for n in app_nodes),
         **({} if apps is not None else {"note": "这次扫描没有量 /Applications"})},
        {"id": "system", "name": "系统", "bytes": None, "estimated": True, "note": COMPOSITION_NOTE_SYSTEM},
        {"id": "other", "name": "其他（含可清除空间）", "bytes": other, "estimated": True},
    ]


def purgeable_bytes(facts):
    """访达口径的可清除空间估算（capacity.purgeable_estimate = Finder 可用 − statvfs 可用）；取不到是 None，不等于 0。"""
    cap = facts.get("capacity") if isinstance(facts.get("capacity"), dict) else {}
    v = cap.get("purgeable_estimate")
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _plan_item_categories(plan):
    out = {}
    for c in (plan or {}).get("cards") or []:
        if not isinstance(c, dict):
            continue
        cat = card_category(c)[0]
        for it in c.get("items") if isinstance(c.get("items"), list) else []:
            if isinstance(it, dict) and isinstance(it.get("path"), str):
                out.setdefault(os.path.normpath(it["path"]), cat)
    return out


def node_category(n, home, item_cats):
    """前沿节点属于哪个看板类别：先看计划里包含它（或就是它）的条目所属卡片，再看识别出的工具，最后按位置猜。"""
    path = os.path.normpath(str(n.get("path") or ""))
    probe = path
    while probe and probe != "/":
        if probe in item_cats:
            return item_cats[probe]
        probe = os.path.dirname(probe)
    card = (n.get("producer") or {}).get("card")
    if card in USER_SOURCES:
        return USER_SOURCES[card][0]
    if home and within(path, home):
        rel = path[len(home.rstrip("/")) + 1:].split("/", 1)
        if rel[0] in _USER_DIRS:
            return "my-files"
        if rel[0] == "Downloads":
            return "old-files"
        if rel[0] == "Library":
            return "system"
    return "other"


def facts_summary(facts, plan=None):
    nodes = [n for n in facts.get("nodes", []) if isinstance(n, dict)]
    frontier = [n for n in nodes if not n.get("children_over_threshold")]
    frontier.sort(key=lambda n: n.get("bytes") or 0, reverse=True)
    home = os.path.realpath(str((facts.get("host") or {}).get("home") or "")) or None
    item_cats = _plan_item_categories(plan)
    top = []
    for n in frontier:
        top.append({
            "path": n.get("path"),
            "bytes": n.get("bytes"),
            "category": node_category(n, home, item_cats),
            "producer": {"card": (n.get("producer") or {}).get("card")},
            "refs": {"count": (n.get("refs") or {}).get("count")},
            "recent_24h": n.get("recent_24h"),
        })
    # 看板总览“没逐项分析的部分在哪”：扫描根下第一层目录（整目录大小，含已查过的部分）
    areas = sorted((n for n in nodes if n.get("depth") == 1), key=lambda n: n.get("bytes") or 0, reverse=True)
    areas = [{"path": n.get("path"), "bytes": n.get("bytes")} for n in areas]
    return {"scan_id": facts.get("scan_id"), "created_at": facts.get("created_at"),
            "disk": facts.get("disk"), "coverage": facts.get("coverage"), "frontier": top, "areas": areas,
            "composition": composition_of(facts), "purgeable_bytes": purgeable_bytes(facts)}


def _json_for_html(obj):
    s = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


BOARD_ITEMS = 20


# 看板左侧目录：(id, 名称, 所在分区, 一句说明)。顺序即目录顺序：先放本 Skill 的特长（AI 工作），
# 再放常见清理软件都有的常规类别，最后是只报告不清理的个人文件。
BOARD_CATEGORIES = [
    {"id": "ai-chats", "name": "AI 对话记录", "section": "AI 工作",
     "desc": "Claude Code、Codex、Gemini 等在对话时保存的记录，包括子 Agent 的工作过程和长段工具输出。"},
    {"id": "ai-images", "name": "AI 生成的图片", "section": "AI 工作",
     "desc": "批量出图时留下的原图和出图记录。已存进仓库的有副本，没存的删了就没了。"},
    {"id": "ai-models", "name": "AI 模型", "section": "AI 工作",
     "desc": "下载到本机的语音、图像、语言模型。删了不影响别的，下次用时会重新下载。"},
    {"id": "project-builds", "name": "项目中间产物", "section": "AI 工作",
     "desc": "项目工作区里生成的草稿、中间稿、构建产物和发布包。"},
    {"id": "dev-tools", "name": "开发工具", "section": "常规清理",
     "desc": "模拟器、Xcode、Docker，以及 Homebrew、npm 等安装工具的缓存。"},
    {"id": "updates", "name": "更新包与安装包", "section": "常规清理",
     "desc": "软件自动下载的更新文件和用过的安装包。已经装好的，留着没用。"},
    {"id": "old-files", "name": "下载与旧文件", "section": "常规清理",
     "desc": "下载、桌面、文稿、影片里很久没动过的大文件：用过的安装包、压缩包、录屏和视频。是你自己放的，删前要你确认。"},
    {"id": "browsers", "name": "浏览器", "section": "常规清理",
     "desc": "浏览器缓存。浏览器开着时清了很快会长回来。"},
    {"id": "chat-netdisk", "name": "聊天与网盘", "section": "常规清理",
     "desc": "微信、网盘等软件。聊天记录和账号数据不碰，只报告占用。"},
    {"id": "system", "name": "系统与临时文件", "section": "常规清理",
     "desc": "macOS 和各软件的缓存、日志、临时文件，多数由系统自己回收。"},
    {"id": "my-files", "name": "成品与工作文件", "section": "你的文件",
     "desc": "你的文稿、视频、项目等工作成果，只报告占用，不清理。"},
    {"id": "other", "name": "其他", "section": "你的文件", "desc": "暂时没有归类的占用。"},
]
_CATEGORY_IDS = {c["id"] for c in BOARD_CATEGORIES}
# producer 卡名 → (默认目录 id, 来源说明)。卡片 cards_used 第一项决定；卡片可用 `category` 字段改归类（如 Codex 出图归 ai-images）。
USER_SOURCES = {
    "codex": ("ai-chats", "Codex"),
    "claude-code": ("ai-chats", "Claude Code"),
    "gemini-antigravity": ("ai-chats", "Gemini / Antigravity"),
    "cursor": ("ai-chats", "Cursor"),
    "grok": ("ai-chats", "Grok"),
    "android": ("dev-tools", "Android 模拟器"),
    "xcode": ("dev-tools", "Xcode 与 iOS 模拟器"),
    "docker": ("dev-tools", "Docker"),
    "package-managers": ("dev-tools", "Homebrew、npm 等安装工具"),
    "model-caches": ("ai-models", "本机 AI 模型"),
    "git-worktree-build": ("project-builds", "项目工作区"),
    "pipelines": ("project-builds", "发布流程与工作目录"),
    "creative-media": ("project-builds", "剪辑与创作软件"),
    "app-updaters-installers": ("updates", "软件自动更新"),
    "large-old-files": ("old-files", "你下载或保存的文件"),
    "browsers": ("browsers", "浏览器"),
    "chat-netdisk": ("chat-netdisk", "聊天与网盘软件"),
    "macos-system": ("system", "macOS 系统"),
}
_RISK_PREFIX = re.compile(r"^.*?）：")
_RISK_PLAIN = {UNVERIFIED_REFS_REASON: "没能自动确认别处还有没有在用它，需要你来确认",
               RISKY_ZONE_REASON: "里面有账号数据或用户资产"}


# 看板“一键选择”三档：稳妥（删了不丢东西）⊂ 推荐 ⊂ 全部可删。卡片可写 `preset` 标最低档；
# 没写时 clean 归稳妥、decide 归推荐；碰到账号数据或用户资产（RISKY_ZONE_REASON）的一律只进“全部可删”。
PRESETS = ("safe", "recommended", "all")
_RANK_PRESET = {p: i for i, p in enumerate(PRESETS)}


def card_preset(card, verdict, notes, cap=None):
    """cap：项目台账给的档位上限（卡里的东西全属于“归档”项目时为 recommended：用户已说这些项目不用了，
    Agent 标成“找不回”的卡也进推荐档）。风险区（账号数据、用户资产）仍一律 all，台账改变不了。"""
    if verdict not in ("clean", "decide"):
        return None
    p = card.get("preset") if card.get("preset") in PRESETS else ("safe" if verdict == "clean" else "recommended")
    if cap in PRESETS and _RANK_PRESET[p] > _RANK_PRESET[cap]:
        p = cap
    if any(RISKY_ZONE_REASON in str(n) for n in notes or []):
        p = "all"
    return p


def risk_summary(notes):
    """逐条目的风险提示（“路径（节点 …）：原因”）去掉路径前缀后合并计数，看板每种原因只显示一行。"""
    out = {}
    for n in notes or []:
        text = _RISK_PREFIX.sub("", str(n), count=1).strip()
        for key, plain in _RISK_PLAIN.items():
            if key in text:
                text = plain
                break
        out[text] = out.get(text, 0) + 1
    return [{"text": t, "count": k} for t, k in out.items()]


def card_category(c):
    """(看板类别 id, 来源说明)：cards_used 第一项映射，卡片写了合法的 category 就以它为准。"""
    used = c.get("cards_used") if isinstance(c.get("cards_used"), list) else []
    cat, source = USER_SOURCES.get(used[0] if used else "", ("other", c.get("group") or "其他"))
    if c.get("category") in _CATEGORY_IDS:
        cat = c["category"]
    return cat, source

def board_plan(plan, result):
    """看板用的计划：判定取校验后的结果（降级卡显示为保留并附原因），路径清单只带前 BOARD_ITEMS 条。"""
    eff = {c.get("id"): c for c in result.get("cards", [])}
    cards = []
    for c in plan.get("cards") or []:
        if not isinstance(c, dict):
            continue
        bc = dict(c)
        bc.pop("default_select", None)         # 用户写的不作数；只认 check 算出来的
        r = eff.get(c.get("id"))
        bc["default_select"] = bool(r and r.get("default_select"))
        bc["category"], bc["source"] = card_category(c)
        q = c.get("question")
        if isinstance(q, str) and q.strip():
            bc["ask"] = q.strip()                   # Agent 写的问题本身；风险前缀单独放在 risk_summary
        if r is not None:
            bc["verdict"] = r.get("verdict_out")
            if r.get("question"):
                bc["question"] = r["question"]          # 校验后的问题（含风险提示），不用 Agent 原写的
            if r.get("risk_notes"):
                bc["risk_notes"] = list(r["risk_notes"])
                bc["risk_summary"] = risk_summary(r["risk_notes"])
            # H2：通过现场复核的结束证据原文，看板里让用户看到“已结束”依据的是什么
            ver = [{"evidence": ck.get("evidence"), "msg": ck.get("msg")} for ck in r.get("evidence_checks", []) if ck.get("end")]
            if ver:
                bc["evidence_verified"] = ver
            if r.get("downgraded"):
                errs = list(r.get("errors") or [])
                bc["downgraded"] = True
                bc["check_errors"] = errs[:3] + ([f"……另有 {len(errs) - 3} 条"] if len(errs) > 3 else [])
                summ = r.get("error_summary") or summarize_messages(errs)
                bc["down_reasons"], bc["down_more"] = down_reasons(summ)
                # 详情里的原文：按原因分组，每组最多 3 个示例路径（含系统原话），不再逐条目重复
                bc["check_groups"] = [{"reason": g["reason"], "count": g["count"], "examples": g["examples"]} for g in summ[:10]]
                bc["check_groups_more"] = max(0, len(summ) - 10)
        if r is not None and isinstance(r.get("bytes_checked"), int):
            bc["bytes"] = r["bytes_checked"]
        elif r is not None and r.get("downgraded"):
            bc["bytes"] = r.get("bytes_hint")            # 没有实测就是 None：页面显示“未统计”，不拿 Agent 在计划里写的数
        # 档位以 check 算出来的为准（含初始设置带来的调整：重建慢的缓存不进稳妥档等）；降级成 keep 的卡没有档位
        if r is not None and r.get("preset") in PRESETS:
            bc["preset"] = r["preset"]
            raw = r.get("preset_raw") if r.get("preset_raw") in PRESETS else r["preset"]
        else:
            bc["preset"] = card_preset(c, bc.get("verdict"), (r or {}).get("risk_notes") or [], cap=(r or {}).get("project_cap"))
            raw = card_preset(c, bc.get("verdict"), (r or {}).get("risk_notes") or [])
        if raw != bc["preset"]:
            bc["preset_raw"] = raw                         # 被台账降档的卡：看板仍标出它原本是“删了找不回”
        if r is not None and r.get("projects"):
            bc["projects"] = r["projects"]                 # 这张卡里的东西属于哪些项目（key / name / status）
        if r is not None and r.get("prefill"):
            bc["prefill"] = r["prefill"]                   # 项目做完了：问题预填“删”（用户仍要确认）
        bc["rule_key"] = prefs.rule_key(c, bc["preset"], bc["category"])
        items = c.get("items") if isinstance(c.get("items"), list) else []
        bc["items_total"] = len(items)
        # 含软件自带清理命令（native）的卡不能归档：看板只列前 BOARD_ITEMS 条，要按全部条目判断后明示
        bc["archivable"] = not any(isinstance(it, dict) and it.get("method") == "native" for it in items)
        bc["items"] = [{k: v for k, v in it.items() if k != "files" and not k.startswith("_")}
                       | ({"files_total": len(it["files"])} if isinstance(it.get("files"), list) else {})
                       for it in items[:BOARD_ITEMS] if isinstance(it, dict)]
        cards.append(bc)
    return dict(plan, cards=cards, categories=BOARD_CATEGORIES)


SETUP_TEMPLATE = os.path.join(SKILL_ROOT, "assets", "setup.html")
# 项目行的取法在 setup_q.py（设置页的单一真源）；这里保留旧名字，免得外部脚本断掉
PROJECT_ROW_FIELDS = setup_q.PROJECT_ROW_FIELDS
_project_row = setup_q.project_row
onboard_projects = setup_q.onboard_projects


def proposal_for_board(run, scan_id):
    """proposal.json 里看板要用的展示字段。没有、读不了或 scan_id 对不上（换过扫描）就不给。"""
    try:
        pr = read_json(os.path.join(run, "proposal.json"))
    except (OSError, ValueError):
        return None
    if not isinstance(pr, dict) or (scan_id and pr.get("scan_id") != scan_id):
        return None
    return {"stage": pr.get("stage"), "project_questions": pr.get("project_questions") or [], "prefill": pr.get("prefill") or {},
            "cloud_suggestions": pr.get("cloud_suggestions") or [],
            "review_questions": pr.get("review_questions") or [],
            "delete_mode": pr.get("delete_mode"), "governance_mode": pr.get("governance_mode")}


def pending_means(kind, value):
    """由 value（确认后真正生效的内容）生成中文释义；text 是 Agent 写的，可能与 value 不一致，用户该看到实际效果。"""
    value = value if isinstance(value, dict) else {}
    if kind == "protect":
        paths = [x for x in (value.get("paths") if isinstance(value.get("paths"), list) else []) if isinstance(x, str) and x.strip()]
        return "以后永不触碰：" + "、".join(paths) if paths else "（没有可用目录，确认后不会生效）"
    if kind == "rule":
        key, action = value.get("key"), value.get("action")
        if isinstance(key, str) and key and action in ("delete", "keep"):
            return f"以后“{key}”这一类" + ("都清" if action == "delete" else "都留着、不再列出")
        return "（规则不完整，确认后不会生效）"
    if kind == "note":
        path = value.get("path")
        return f"给 {path} 记一条备注，只用来提醒，不改变清理判断" if isinstance(path, str) and path else "（备注缺少目录，确认后不会生效）"
    return "（不认识的类型，确认后不会生效）"


def pending_rules_of(profile):
    """用户在设置页“补充”里说的话，Agent 翻译后待确认的规则：[{id, text, means}]。看板每条两按钮“对 / 不对”。
    means = 由 value 生成的中文释义（实际生效的内容），看板与 text 并排显示。"""
    out = []
    for r in profile.get("pending_rules") if isinstance(profile.get("pending_rules"), list) else []:
        if isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("text"), str) and r["text"].strip():
            out.append({"id": r["id"], "text": r["text"], "means": pending_means(r.get("kind"), r.get("value"))})
    return out


def _render_template(template, logo_path, placeholder, payload, out_path):
    with open(template, encoding="utf-8") as f:
        html = f.read()
    if placeholder not in html:
        raise ValueError(f"模板里没有 {placeholder} 占位符")
    with open(logo_path, "rb") as f:
        logo = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    dark_path = os.path.join(os.path.dirname(logo_path), "logo-dark.png")
    if os.path.isfile(dark_path):
        with open(dark_path, "rb") as f:
            dark_logo = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    else:
        dark_logo = logo
    html = html.replace("__LOGO_DARK_DATA__", dark_logo)
    html = html.replace("__LOGO_DATA__", logo).replace(placeholder, _json_for_html(payload))
    tmp = f"{out_path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, out_path)


def cmd_board(args):
    run = os.path.realpath(args.run)
    if not os.path.isfile(TEMPLATE):
        print(f"错误：看板模板不存在：{TEMPLATE}", file=sys.stderr)
        return 2
    if not os.path.isfile(LOGO):
        print(f"错误：logo 不存在：{LOGO}", file=sys.stderr)
        return 2
    try:
        facts = read_json(os.path.join(run, "facts.json"))
        plan, _ = load_plan(run)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    result, _ = check_plan(run)
    write_json(os.path.join(run, "plan.check.json"), result)
    digest = None
    seal_path = os.path.join(run, "seal.json")
    if os.path.isfile(seal_path):
        try:
            seal = read_json(seal_path)
            if seal.get("digest") == digest_of(seal):
                digest = seal["digest"]
            else:
                print("警告：seal.json 的 digest 与内容不符，不予采用", file=sys.stderr)
        except ValueError as exc:
            print(f"警告：{exc}", file=sys.stderr)
    bplan = board_plan(plan, result)
    if digest:
        write_cardkeys(run, bplan["cards"], seal.get("cards") or {})
    profile = prefs.load()
    scan_id = facts.get("scan_id") or plan.get("scan_id")
    proposal = proposal_for_board(run, scan_id)
    # 0.6：台账表和全局设置不再放在清理看板里（改设置在对话里说“改设置”，打开设置页）。看板只带：
    # 按需展开的设置详情、新项目 / 久没动项目的单题卡、待确认规则、归档开关
    payload = {"facts": facts_summary(facts, plan), "plan": bplan, "check": result,
               "seal": {"digest": digest}, "seal_digest": digest,
               "setup_summary": setup_q.summary_lines(profile),
               "delete_mode": profile.get("delete_mode") if profile.get("delete_mode") in ("delete", "trash") else "delete",
               "project_questions": (proposal or {}).get("project_questions") or [],
               "pending_rules": pending_rules_of(profile), "archive_enabled": True,
               "cloud_suggestions": (proposal or {}).get("cloud_suggestions") or [],
               "proposal": proposal}
    growth_path = os.path.join(run, "growth.json")
    if os.path.isfile(growth_path):
        try:
            growth = read_json(growth_path)
            if growth.get("schema") == "space-growth/1" and growth.get("scan_id") == scan_id:
                payload["growth"] = growth
        except (OSError, ValueError, AttributeError):
            pass
    review_path = os.path.join(run, "review-status.json")
    if os.path.isfile(review_path):
        try:
            review = read_json(review_path)
            if review.get("schema") == "review-status/1" and review.get("scan_id") == scan_id:
                payload["review_status"] = review
        except (OSError, ValueError, AttributeError):
            pass
    out = args.out or "board.html"
    if not os.path.isabs(out):
        out = os.path.join(run, out)
    try:
        _render_template(TEMPLATE, LOGO, "__PLAN_JSON__", payload, out)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(f"看板已生成：{out}")
    if digest is None:
        print("提示：还没有封存（seal.json），看板里的 seal 为空；先运行 plan.py seal")
    else:
        print(f"seal: {digest}")
    return 0


def cmd_setup(args):
    """「认识你的电脑」初始设置页：读 <run>/projects.json（scan.py probe projects 的结果），写 <run>/setup.html。
    --mode first（第一次，出全部适用的题）/ upgrade（题库更新了，只补新题，不出项目表）/ edit（改设置，预填现值；
    导出时只带改动过的题）；不写按 profile 自动判断。页面只保存设置（导出抬头“叁笙清理 · 初始设置”），没有任何清理选项。
    plan.py onboard 是它的别名。"""
    run = os.path.realpath(args.run)
    for path in (SETUP_TEMPLATE, LOGO):
        if not os.path.isfile(path):
            print(f"错误：文件不存在：{path}", file=sys.stderr)
            return 2
    ppath = os.path.join(run, "projects.json")
    if not os.path.isfile(ppath):
        print(f"错误：运行目录里没有 projects.json：先运行 scan.py probe projects --run {shlex.quote(run)}", file=sys.stderr)
        return 2
    try:
        pfile = read_json(ppath)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if not isinstance(pfile, dict) or pfile.get("schema") != "projects/1" or not isinstance(pfile.get("projects"), list):
        print("错误：projects.json 格式不对（应为 schema projects/1），重新运行 scan.py probe projects", file=sys.stderr)
        return 2
    try:
        facts_id = read_json(os.path.join(run, "facts.json")).get("scan_id")
    except (OSError, ValueError, AttributeError):
        facts_id = None
    if facts_id and pfile.get("scan_id") != facts_id:
        print("错误：projects.json 的 scan_id 与 facts.json 对不上（换过扫描），重新运行 scan.py probe projects", file=sys.stderr)
        return 2
    profile = prefs.load()
    mode = getattr(args, "mode", None) or setup_q.auto_mode(profile)
    payload = setup_q.build_payload(profile, pfile, mode)
    if not payload["groups"]:
        print("没有需要问的题：这台电脑上没有新增的情况。想改已有设置，用 --mode edit")
        return 0
    out = args.out or "setup.html"
    if not os.path.isabs(out):
        out = os.path.join(run, out)
    try:
        _render_template(SETUP_TEMPLATE, LOGO, "__SETUP_JSON__", payload, out)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    n = sum(len(g["questions"]) for g in payload["groups"])
    print(f"设置页已生成（{ {'first': '第一次', 'upgrade': '只补新题', 'edit': '改设置'}[mode] }，共 {n} 题）：{out}")
    return 0


# ----------------------------------------------------------------- 对话确认（提案）

# 三档选项：回复词 → 档位。“好”= 推荐档（稳妥 + 推荐），“都删”= 全部可删。
CHOICES = (("safe", "稳妥", "只删不丢东西的"), ("recommended", "推荐", "按推荐删"), ("all", "都删", "连删了找不回的一起删"))
_RANK = {p: i for i, p in enumerate(PRESETS)}
BOARD_ATTENTION_MAX = 3      # 要你看的项超过这个数就发看板


_gb = units.fmt_bytes         # 十进制 GB，一位小数；小于 1 GB 用 MB（见 units.py）


def needs_board(profile, attention):
    """返回发看板的原因列表；空列表 = 在对话里一句话确认即可。"""
    reasons = []
    if setup_q.stage_of(profile) == "onboarding":
        reasons.append("第一次使用：要核对一次你的使用习惯")
    if isinstance(profile, dict) and profile.get("confirm_mode") == "board":
        reasons.append("你选了每次都发看板")
    if len(attention) > BOARD_ATTENTION_MAX:
        reasons.append(f"有 {len(attention)} 项要你看，超过 {BOARD_ATTENTION_MAX} 项，在网页里看更清楚")
    return reasons


def write_cardkeys(run, cards, sealed):
    """可执行卡的规则键，正式执行完 act.py 据此记录这次的选择（飞轮）。"""
    keys = {c["id"]: {"key": c["rule_key"], "bytes": c.get("bytes") or 0, "title": c.get("title") or ""}
            for c in cards if c.get("id") in sealed and c.get("preset") and c.get("rule_key")}
    for cid, k in keys.items():
        pj = [p.get("key") for p in (sealed.get(cid) or {}).get("projects") or [] if isinstance(p, dict) and p.get("key")]
        if pj:                    # 卡片属于哪些项目：act.py 记选择时多记一行带项目前缀的键（prefs.record_choices）
            k["projects"] = pj
    write_json(os.path.join(run, "cardkeys.json"), keys)


_WORDS = {"safe": "稳妥", "recommended": "推荐", "all": "全部可删"}
PROJECT_STALE_DAYS = 30          # 台账里“进行中”的项目超过这么多天没动，问一句“做完了吗”
PROJECT_ASK_NEW_MAX = 15         # 新发现的项目只问 shown 的（projects.json 里体积前 15）


def _parse_iso(v):
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def project_questions(profile, pfile, now=None):
    """成长：返回 (问题列表, 文案句子列表)。① 本次扫描新认出、台账里还没有的项目（只列 shown 的）；
    ② 台账里标“进行中”、最后活动超过 30 天的项目，问一句“做完了吗”。没有 projects.json 就不问。"""
    if not pfile:
        return [], []
    now = now or datetime.now(timezone.utc)
    ledger = profile.get("projects") if isinstance(profile.get("projects"), dict) else {}
    questions, lines = [], []
    new = [p for p in pfile.get("projects", []) if isinstance(p, dict) and p.get("shown") and p.get("key") not in ledger]
    if new:
        questions.append({"type": "new_projects", "count": len(new),
                          "projects": [{"key": p["key"], "name": p.get("name"), "bytes": p.get("bytes"),
                                        "default_status": p.get("default_status")} for p in new[:PROJECT_ASK_NEW_MAX]]})
        names = "、".join(f"「{p.get('name')}」" for p in new[:3]) + ("等" if len(new) > 3 else "")
        lines.append(f"另外，这次新发现 {len(new)} 个项目（{names}），还没标状态；标一下以后就不用再问（进行中 / 做完了 / 归档 / 别碰）。")
    by_key = {p.get("key"): p for p in pfile.get("projects", []) if isinstance(p, dict)}
    for key, e in ledger.items():
        if not (isinstance(e, dict) and e.get("status") == "active" and key in by_key):
            continue
        last = _parse_iso(by_key[key].get("last_active"))
        if last and (now - last).days >= PROJECT_STALE_DAYS:
            days = (now - last).days
            name = e.get("name") or by_key[key].get("name") or key
            questions.append({"type": "stale_active", "key": key, "name": name, "idle_days": days})
            lines.append(f"另外，你标成“进行中”的「{name}」已经 {days} 天没动了，做完了吗？"
                         f"回复“做完了”我就改成做完了（plan.py project --key {shlex.quote(key)} --status done）。")
    return questions, lines


# ----------------------------------------------------------------- 云盘建议（第 11 题）
# 只读情报 + 官方入口：不执行任何释放动作，也不调 brctl evict（非官方文档承诺、没上传完的文件会不会丢没有依据）。
# 调研见 references/ 与 0.6 云盘调研；链接都是各家官方帮助页。
CLOUD_MIN_BYTES = 10 ** 9           # 本机占不到 1 GB 的文件夹不值得提
CLOUD_MAX_SUGGESTIONS = 8
_CLOUD_CAVEAT = "先在访达里确认这个文件夹里没有“正在上传”的文件；别直接删除，也别把文件拖出云盘，那样会连云端的一起删掉。"
CLOUD_HOWTO = {
    "iCloud Drive": ("在访达的“iCloud 云盘”里，按住 Control 点这个文件夹，选“移除下载”：文件还留在云上，用到时再下载。" + _CLOUD_CAVEAT,
                     "https://support.apple.com/guide/mac-help/mchl1a02d711/mac"),
    "Dropbox": ("在访达里右键这个文件夹，选“设为仅在线”（英文版是 Make online-only）：文件还留在 Dropbox 里，用到时再下载。" + _CLOUD_CAVEAT,
                "https://help.dropbox.com/sync/make-files-online-only"),
    "OneDrive": ("在访达里右键这个文件夹，选“释放空间”（Free up space）：文件还留在 OneDrive 里，用到时再下载。" + _CLOUD_CAVEAT,
                 "https://support.microsoft.com/en-us/onedrive/save-disk-space-with-onedrive-files-on-demand-for-mac"),
    "Google Drive": ("在 Google Drive 桌面版的设置里，把“我的云端硬盘”改成“串流文件”（Stream files）：文件留在云上，本机只留很少的缓存。" + _CLOUD_CAVEAT,
                     "https://support.google.com/drive/answer/13401938"),
    "其他": ("我没法判断这个网盘里哪些能只留云端。请在它自己的设置里找“仅在线 / 取消同步到本地”这类选项；同步型网盘直接删除可能连云端一起删，先别删。", None),
}


def cloud_suggestions_of(profile, pfile):
    """第 11 题的建议：设置里回答“可以”或“不确定”，且 projects.json 里有本机占 ≥ 1 GB 的云盘文件夹时，
    列出最大的几个，每个写明官方怎么做。回答“不行，我要离线用”或没回答时为空。只读：这里不动任何文件。"""
    if (profile or {}).get("cloud_policy") not in ("ok", "unsure"):
        return []
    sf = (pfile or {}).get("setup_facts") if isinstance((pfile or {}).get("setup_facts"), dict) else {}
    rows = []
    for f in sf.get("cloud_folders") if isinstance(sf.get("cloud_folders"), list) else []:
        if not (isinstance(f, dict) and isinstance(f.get("path"), str) and f["path"]):
            continue
        b = f.get("local_bytes")
        if not isinstance(b, int) or isinstance(b, bool) or b < CLOUD_MIN_BYTES:
            continue
        prov = f.get("provider") if f.get("provider") in CLOUD_HOWTO else "其他"
        how, link = CLOUD_HOWTO[prov]
        rows.append({"path": f["path"], "provider": prov, "local_bytes": b, "how": how, "link": link})
    rows.sort(key=lambda r: (-r["local_bytes"], r["path"]))
    return rows[:CLOUD_MAX_SUGGESTIONS]


def build_proposal(run):
    """按封存的计划生成三档选项（每档：卡号、答复、sel、体积）与对话文案。没封存或封存失效时返回 (None, 错误)。
    档位、规则键、体积、要不要逐个问都取自封存（0.4 评审：封存后改 plan.json 不能改变执行范围）；plan.json 只用来显示类别。
    本机规则：规则为“留”的类别不进任何一档；规则为“删”的类别不再算“要你看”，但仍只进它自己那一档及以上，
    且对降过级、带风险或含交互会话的卡不适用。
    对话模式下，要你看的每一项都在文案里逐条列出标题、体积和问题；含交互会话的卡一律发看板逐个确认。"""
    seal_path = os.path.join(run, "seal.json")
    if not os.path.isfile(seal_path):
        return None, "还没有封存，先运行 plan.py seal"
    seal = read_json(seal_path)
    if seal.get("digest") != digest_of(seal):
        return None, "seal.json 的 digest 与内容不符"
    sealed = seal.get("cards") or {}
    if any(not isinstance(r, dict) or "preset" not in r for r in sealed.values()):
        return None, "封存是旧格式（没有档位），重新运行 plan.py seal"
    plan, _ = load_plan(run)
    result, _ = check_plan(run)
    profile = prefs.load()
    S = settings_of(profile)
    shown = {c.get("id"): c for c in board_plan(plan, result)["cards"]}
    cards = []
    for cid, r in sealed.items():
        if r.get("preset") not in PRESETS:
            continue
        c = {"id": cid, "title": r.get("title") or cid, "verdict": r.get("verdict"), "preset": r["preset"],
             "preset_raw": r.get("preset_raw") if r.get("preset_raw") in PRESETS else r["preset"],
             "rule_key": r.get("rule_key"), "review": bool(r.get("review")), "interactive": bool(r.get("interactive")),
             "bytes": sum(int(it.get("bytes") or 0) for it in r.get("items") or [] if isinstance(it, dict)),
             "category": (shown.get(cid) or {}).get("category"), "ask": (shown.get(cid) or {}).get("ask")}
        rule = prefs.rule_for(profile, c["rule_key"]) if c["rule_key"] else None
        if rule == "delete" and (c["review"] or c["interactive"]):
            rule = None
        # 设置“安装包直接清”：和一条“以后都删”的规则一样处理（仍不适用于降过级、带风险或含交互对话的卡；用户明确的“留”规则优先）
        if rule is None and S["installer"] == "auto" and r.get("installer") and not c["review"] and not c["interactive"]:
            rule = "delete"
        c["rule"] = rule
        cards.append(c)
    write_cardkeys(run, cards, sealed)
    live = [c for c in cards if c["rule"] != "keep"]
    cats = {c["id"]: c["name"] for c in BOARD_CATEGORIES}
    options = {}
    for preset, word, label in CHOICES:
        # 含交互会话的卡只能在看板里逐个确认（对话里的一句“好”不是对它的确认），任何一档都不含
        cs = [c for c in live if _RANK[c["preset"]] <= _RANK[preset] and not c["interactive"]]
        ids = sorted(c["id"] for c in cs)
        answers = sorted(f"{c['id']}=删" for c in cs if c.get("verdict") == "decide")
        by_cat = {}
        for c in cs:
            k = cats.get(c.get("category"), "其他")
            by_cat[k] = by_cat.get(k, 0) + c["bytes"]
        options[preset] = {"word": word, "label": label, "cards": ids, "answers": answers,
                           "sel": selection_sig(seal["digest"], ids, answers),
                           "bytes": sum(c["bytes"] for c in cs),
                           "by_category": sorted(by_cat.items(), key=lambda kv: -kv[1])}
    default = profile.get("default_choice") if profile.get("default_choice") in PRESETS else "recommended"
    # 要你看的 = 待你决定、且没有被本机规则覆盖的卡
    attention = [c for c in live if c.get("verdict") == "decide" and not c["rule"]]
    reasons = needs_board(profile, attention)
    review_questions = [{"id": cid, "title": c.get("title") or cid, "question": c["ask"],
                         "bytes": c.get("bytes") or 0}
                        for cid, c in shown.items() if c.get("verdict") == "keep" and c.get("ask")]
    if review_questions:
        reasons.append(f"有 {len(review_questions)} 项暂不能删除，需要你选择是否继续核查")
    if not live:
        reasons.append("本轮没有可执行清理项，仍须交付占用与保留依据看板")
    inter = [c for c in live if c["interactive"]]
    if inter:
        reasons.append(f"有 {len(inter)} 项含 AI 交互对话，人可能还会回来续聊，要在看板里逐个确认")
    questions = prefs.pending_questions(profile, [c["rule_key"] for c in live if c["rule_key"] and not c["rule"]])
    in_default = set(options[default]["cards"])
    # “删了找不回”的提醒按卡原本的风险（preset_raw）判定：归档项目把 all 档卡降到推荐档后，回“好”照样会删它，更要提醒
    careful = [c for c in live if c["preset_raw"] == "all" and c["id"] not in {a["id"] for a in attention}]
    ruled = [c for c in live if c["rule"] == "delete" and c["id"] in in_default]
    kept = [c for c in cards if c["rule"] == "keep"]
    d = options[default]
    lines = []
    if d["cards"]:
        top = "、".join(f"{k} {_gb(v)}" for k, v in d["by_category"][:4])
        lines.append(f"按{_WORDS[default]}可以腾出 {_gb(d['bytes'])}（{top}）。")
    if ruled:
        lines.append("其中按你定的规则直接清的：" + "；".join(f"{c['title']} {_gb(c['bytes'])}" for c in ruled) + "。")
    if attention:
        parts = []
        for i, c in enumerate(attention, 1):
            mark = "回“好”会删" if c["id"] in in_default else "回“好”不删"
            parts.append(f"{i}. {c['title']} {_gb(c['bytes'])}（{mark}）" + (f"：{c['ask']}" if c.get("ask") else ""))
        lines.append("要你拿主意的：" + "；".join(parts) + "。")
    for group, lead in (([c for c in careful if c["id"] in in_default], "回“好”也会删掉这些找不回的："),
                        ([c for c in careful if c["id"] not in in_default], "另有删了就找不回的：")):
        if group:
            lines.append(lead + "；".join(f"{c['title']} {_gb(c['bytes'])}" for c in group) + "。")
    reply = [f"回复“好”按{_WORDS[default]}删"] if d["cards"] else []
    for preset, word, label in CHOICES:
        o = options[preset]
        if preset != default and o["cards"] and o["cards"] != d["cards"]:
            reply.append(f"“{word}”{label}，共 {_gb(o['bytes'])}")
    if reply:
        lines.append("；".join(reply) + "；想看细节我发看板。")
    elif not live:
        lines.append("本轮没有通过校验的可执行清理项，不代表没有可清理内容。请查看看板中的占用、保留依据和待核查问题；本轮尚未删除文件。")
    else:
        lines.append("推荐档没有可执行项，请在看板中逐项决定。")
    if review_questions:
        lines.append(f"另有 {len(review_questions)} 项需要你选择是否继续核查；这些答复不是删除授权。")
    if kept:
        lines.append(f"按你的规则留着、没有列出的有 {len(kept)} 项。")
    for q in questions:
        if q["action"] == "delete":
            lines.append(f"另外，最近 {q['runs']} 次清理你都删了“{q['title']}”这一类，以后直接按规则清吗？"
                         "回复“以后都删”记成规则；“不用”{0} 天内不再问。".format(prefs.COOLDOWN_DAYS))
        else:
            lines.append(f"另外，最近 {q['runs']} 次清理你都没删“{q['title']}”这一类，以后就不再列出来了吗？"
                         "回复“以后别列”记成规则；“不用”{0} 天内不再问。".format(prefs.COOLDOWN_DAYS))
    pfile = load_projects_file(run, seal.get("scan_id"))
    pquestions, plines = project_questions(profile, pfile)
    lines.extend(plines)
    cloud = cloud_suggestions_of(profile, pfile)
    if cloud:
        lines.append("另外，iCloud 或网盘里有 %d 个文件夹在本机占了 %s，可以只留云端（看板上写了怎么做，我不会替你动）。"
                     % (len(cloud), _gb(sum(c["local_bytes"] for c in cloud))))
    prefill = {cid: r["prefill"] for cid, r in sealed.items() if isinstance(r, dict) and r.get("prefill")}
    projects_by_card = {cid: r["projects"] for cid, r in sealed.items() if isinstance(r, dict) and r.get("projects")}
    stage = setup_q.stage_of(profile)
    rs = profile.get("read_scope")
    proposal = {"schema": "proposal/1", "scan_id": seal.get("scan_id"), "seal": seal["digest"],
                "created_at": now_iso(), "stage": stage, "mode": "board" if reasons else "chat", "board_reasons": reasons,
                "default": default, "attention": [c["id"] for c in attention], "questions": questions,
                "project_questions": pquestions, "review_questions": review_questions,
                "prefill": prefill, "projects_by_card": projects_by_card,
                "options": options, "message": "".join(lines),
                # 0.6 初始设置：治本建议怎么写、删除用废纸篓还是直接删（没设就是直接删，和 0.5 一致）、第二轮能读哪些文件
                "governance_mode": profile.get("governance_mode") if profile.get("governance_mode") in setup_q.SINGLE["governance"][1] else "guide",
                "delete_mode": profile.get("delete_mode") if profile.get("delete_mode") in setup_q.SINGLE["delete_mode"][1] else "delete",
                "read_scope": [x for x in rs if x in setup_q.READ_SCOPES] if isinstance(rs, list) else ["basic"],
                "setup_upgrade": setup_q.setup_upgrade(profile),
                "cloud_suggestions": cloud}
    if stage == "onboarding":
        proposal["onboarding_hint"] = ("第一次使用：先发「认识你的电脑」设置页（plan.py setup --run <目录>，数据来自 scan.py probe projects），"
                                       "用户答完、plan.py profile --apply 保存后再发清理清单；设置页只保存设置，不含清理授权")
    if proposal["setup_upgrade"]:
        proposal["setup_hint"] = ("这个版本多了几个设置问题（之前的设置都保留）：先用 plan.py setup --mode upgrade 发一份只补新题的设置页，"
                                  "保存后再发清理看板")
    return proposal, None


def cmd_propose(args):
    run = os.path.realpath(args.run)
    try:
        proposal, err = build_proposal(run)
    except ValueError as exc:
        proposal, err = None, str(exc)
    if err:
        print(f"错误：{err}", file=sys.stderr)
        return 2
    write_json(os.path.join(run, "proposal.json"), proposal)
    try:
        stale = protect.profile_guard_problem(read_json(os.path.join(run, "seal.json")), load_profile())
    except ValueError:
        stale = None
    if stale:                                  # 只提醒：真正的拒绝在 act.py run / go
        print(f"提醒：{stale}。现在执行会被拒绝", file=sys.stderr)
    print(f"阶段：{proposal['stage']}" + ("（第一次使用）" if proposal["stage"] == "onboarding" else ""))
    if proposal.get("onboarding_hint"):
        print("提示：" + proposal["onboarding_hint"])
    if proposal.get("setup_hint"):
        print("提示：" + proposal["setup_hint"])
    if proposal["mode"] == "board":
        print("方式：发看板（" + "；".join(proposal["board_reasons"]) + "）。先运行 plan.py board，再把看板交给用户")
    else:
        print("方式：在对话里确认。把下面这段话发给用户：")
    print(proposal["message"])
    for preset, o in proposal["options"].items():
        if o["cards"] and proposal["mode"] == "chat" and proposal["stage"] != "onboarding":    # 发看板的提案不收 go --choice
            word = "好 / " + o["word"] if preset == proposal["default"] else o["word"]
            print(f"  回复“{word}” → python3 scripts/act.py go --run {shlex.quote(run)} --choice {preset}"
                  f"    （{len(o['cards'])} 项，{_gb(o['bytes'])}）")
    for q in proposal["questions"]:
        yes = "以后都删" if q["action"] == "delete" else "以后别列"
        print(f"  回复“{yes}” → python3 scripts/plan.py rule --key {q['key']} --reply {q['action']}"
              f"；“不用” → --reply never")
    return 0


def cmd_rule(args):
    try:
        print(prefs.answer(args.key, args.reply, getattr(args, "scan_id", None)))
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


def cmd_profile(args):
    try:
        text = sys.stdin.read() if args.apply == "-" else open(args.apply, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        print(f"错误：读不了 --apply 指定的文件 {args.apply}（{reason}）；请确认路径对不对，或用 - 从标准输入读", file=sys.stderr)
        return 2
    before = protect.profile_guard(load_profile())["digest"]
    note = None
    if setup_q.is_setup_export(text):
        # 初始设置导出：只保存设置，不含清理授权（act.py go --from-board 遇到这个抬头一律拒绝）
        done, errs, note = setup_q.apply_export(text, getattr(args, "scan_id", None))
    else:
        done, errs = prefs.apply_settings(text, getattr(args, "scan_id", None))
    for d in done:
        print("已保存：" + d)
    for e in errs:
        print("没保存：" + e, file=sys.stderr)
    if note:
        # 用户在“还有什么想让我知道的”里写的原话：不存。请 Agent 翻译成待确认规则，在第一张清理看板上请用户确认后才生效
        print("补充（用户原话，不存档）：" + json.dumps(note, ensure_ascii=False))
        print("请把它翻译成规则或备注：python3 scripts/memory.py pending add --text \"<一句话规则>\" --kind protect|rule|note [--value JSON]；"
              "清理看板上会请用户确认，确认前不生效")
    if done and protect.profile_guard(load_profile())["digest"] != before:
        print("提醒：影响判断的本机设置变了，已封存的计划里按旧设置做的判断就过期了；执行前先重新运行 plan.py check 和 plan.py seal", file=sys.stderr)
    return 0 if done else 2


def cmd_project(args):
    before = protect.profile_guard(load_profile())["digest"]
    try:
        print("已保存：" + prefs.set_project(args.key, args.status, scan_id=getattr(args, "scan_id", None)))
    except ValueError as exc:
        print(f"没保存：{exc}", file=sys.stderr)
        return 2
    if protect.profile_guard(load_profile())["digest"] != before:
        print("提醒：项目台账变了，已封存的计划里按旧状态做的判断就过期了；执行前先重新运行 plan.py check 和 plan.py seal", file=sys.stderr)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="叁笙清理：计划校验 / 封存 / 看板")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "seal", "board", "propose", "setup", "onboard"):
        sp = sub.add_parser(name, help={"setup": "「认识你的电脑」初始设置页（读 projects.json，写 setup.html）",
                                        "onboard": "setup 的别名"}.get(name))
        sp.add_argument("--run", required=True, help="运行目录（含 facts.json、plan.json；setup 需要 projects.json）")
        if name in ("board", "setup", "onboard"):
            sp.add_argument("--out", default=None, help=f"输出文件，相对路径落在运行目录内，默认 {'board' if name == 'board' else 'setup'}.html")
        if name in ("setup", "onboard"):
            sp.add_argument("--mode", choices=("first", "upgrade", "edit"), default=None,
                            help="first 第一次 / upgrade 只补新题 / edit 改设置（预填现值）；缺省按 profile 自动判断")
    rp = sub.add_parser("rule", help="回答“以后都这样吗”：delete/keep 记成规则，never 冷却 60 天，once 只这次，forget 忘掉")
    rp.add_argument("--key", required=True)
    rp.add_argument("--reply", required=True, choices=("delete", "keep", "never", "once", "forget"))
    rp.add_argument("--scan-id", dest="scan_id", default=None, help="这次扫描的编号（写进“这次我记住了”，可省）")
    pp = sub.add_parser("profile", help="保存看板“设置:”“别再列:”行、台账导出、或初始设置导出到本机偏好")
    pp.add_argument("--apply", required=True, help="含看板导出文本的文件，- 表示标准输入")
    pp.add_argument("--scan-id", dest="scan_id", default=None, help="这次扫描的编号（写进“这次我记住了”，可省）")
    pj = sub.add_parser("project", help="改单个项目的台账状态（如回答“X 做完了吗”）")
    pj.add_argument("--scan-id", dest="scan_id", default=None, help="这次扫描的编号（写进“这次我记住了”，可省）")
    pj.add_argument("--key", required=True, help="项目键：项目目录的绝对路径，或 env:android 这类环境项目")
    pj.add_argument("--status", required=True, help="active / done / archived / protected，或 进行中 / 做完了 / 归档 / 别碰")
    args = ap.parse_args(argv)
    return {"check": cmd_check, "seal": cmd_seal, "board": cmd_board, "setup": cmd_setup, "onboard": cmd_setup, "propose": cmd_propose,
            "rule": cmd_rule, "profile": cmd_profile, "project": cmd_project}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
