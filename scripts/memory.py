#!/usr/bin/env python3
"""叁笙清理：记忆。

profile 里有四块“记忆”，都由这里读写（不改 prefs.py，只用它的 load/save/now_iso）：
- identities：认下来的目录（谁产出的、属于哪个任务）；
- governance：从源头改过的设置，以及怎么撤销；
- pending_rules：用户提过、还没确认的一句话（设置页“补充”里来的）；
- memory_log：这次到底记住了什么，给成果页“这次我记住了”和“忘掉”用。

原则：记忆只改变“问不问、预填什么”，不改变“能不能放行”；forget 必须真的撤销（删规则、删身份、
移出保护名单、恢复旧设置、删治理记录），只在日志里标 forgotten，不物理删日志。

    python3 scripts/memory.py identity set --path ~/x --title 课程视频 --scan-id <run>
    python3 scripts/memory.py govern record --path ~/x --change "…" --undo "…" --bytes 12345
    python3 scripts/memory.py govern check --run <run目录>
    python3 scripts/memory.py pending add --text "…" --kind protect --value '["~/x"]'
    python3 scripts/memory.py remembered --run <run目录>
    python3 scripts/memory.py forget --ids m1,m2      # 或 forget --from-text <文件>
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import prefs  # noqa: E402
import protect  # noqa: E402
import units  # noqa: E402

PENDING_KINDS = ("protect", "rule", "note")
RULE_ACTIONS = ("delete", "keep")


# ------------------------------------------------------------ 小工具

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise ValueError(f"找不到文件：{path}")
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 {path}：{exc}")


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _short(path):
    """把家目录下的路径写成 ~/…（给人看）。保护名单里存的是 "~/…"，身份/治理键是绝对路径，两种都要认。"""
    if not isinstance(path, str) or not path:
        return path
    home = os.path.realpath(protect.HOME).rstrip("/")
    full = protect.real(path)
    if full == home:
        return "~"
    if full.startswith(home + "/"):
        return "~" + full[len(home):]
    return path


def _id_list(ids):
    """接受列表或 "m1,m2" 这种字符串。"""
    if isinstance(ids, str):
        ids = re.split(r"[,，、\s]+", ids)
    return [str(x).strip() for x in (ids or []) if str(x).strip()]


def _max_num(items, prefix):
    top = 0
    for x in items:
        if isinstance(x, dict) and isinstance(x.get("id"), str):
            m = re.match(r"^%s(\d+)$" % re.escape(prefix), x["id"])
            if m:
                top = max(top, int(m.group(1)))
    return top


def _check_path(path):
    """记住的路径必须是存在的家目录下路径，或 /private/tmp/claude-<uid> 下的路径。返回 (realpath, 错误)。"""
    if not isinstance(path, str) or not path.strip():
        return None, "路径不能为空"
    full = protect.real(path)
    home = protect.real("~")
    croot = protect.real(protect.claude_tmp_root())
    in_home = full != home and full.startswith(home.rstrip("/") + "/")
    in_tmp = full == croot or full.startswith(croot.rstrip("/") + "/")
    if not (in_home or in_tmp):
        return None, f"只记住家目录下或 Claude 会话临时目录下的路径：{path}"
    if not os.path.exists(full):
        return None, f"路径不存在：{path}"
    return full, None


# ------------------------------------------------------------ 记忆日志

def _log(profile, kind, text, ref=None, scan_id=None):
    """往 memory_log 追加一条，id = m<递增数字>。返回这条记录。"""
    log = profile.get("memory_log")
    if not isinstance(log, list):
        log = profile["memory_log"] = []
    entry = {"id": "m%d" % (_max_num(log, "m") + 1), "kind": kind, "text": text, "at": prefs.now_iso()}
    if ref is not None:
        entry["ref"] = ref
    if scan_id:
        entry["scan_id"] = scan_id
    log.append(entry)
    return entry


def log_change(kind, text, ref=None, scan_id=None, profile=None):
    """给别的模块用的公开入口：把一次“记住了什么”写进 memory_log（成果页“这次我记住了”读它，forget 能撤销）。
    kind：rule（以后都删 / 别再列）/ setting（设置项，ref {field, old}）/ project（项目状态，ref {key, old_status}）等。
    传 profile 时只在这个内存字典里追加（调用方自己存盘，免得和同一次保存互相覆盖）；不传就自己读写 profile。
    scan_id 拿不到就不带，remembered 还能靠时间（--since）认出来。返回这条记录。"""
    if profile is not None:
        return _log(profile, kind, text, ref=ref, scan_id=scan_id)
    p = prefs.load()
    entry = _log(p, kind, text, ref=ref, scan_id=scan_id)
    prefs.save(p)
    return entry


def _new_pending(profile, text, kind, value):
    pend = profile.get("pending_rules")
    if not isinstance(pend, list):
        pend = profile["pending_rules"] = []
    entry = {"id": "p%d" % (_max_num(pend, "p") + 1), "text": text, "kind": kind, "value": value,
             "at": prefs.now_iso()}
    pend.append(entry)
    return entry


def _undo(profile, entry):
    """撤销一条记忆造成的改动（不存盘）。"""
    kind = entry.get("kind")
    ref = entry.get("ref") if isinstance(entry.get("ref"), dict) else {}
    if kind == "rule":
        key, rules = ref.get("key"), profile.get("rules")
        if isinstance(rules, dict):
            cur = rules.get(key)
            # 只撤“这条记忆写的”规则：别的来源（或后来又被改过）不动。
            # 记忆模块自己写的带 source=memory；回复“以后都删 / 别再列”（prefs）写的日志带 old（原来的规则或 None）
            if isinstance(cur, dict) and cur.get("action") == ref.get("action") \
                    and (cur.get("source") == "memory" or "old" in ref):
                if isinstance(ref.get("old"), dict):
                    rules[key] = ref["old"]
                else:
                    rules.pop(key, None)
    elif kind == "identity":
        ids = profile.get("identities")
        if isinstance(ids, dict):
            ids.pop(ref.get("path"), None)
    elif kind == "protect":
        paths = ref.get("paths") if isinstance(ref.get("paths"), list) else ([ref["path"]] if ref.get("path") else [])
        prot = [x for x in (profile.get("protect") or []) if x not in paths]
        profile["protect"] = prot
    elif kind == "setting":
        fields = {ref.get("field"): ref.get("old")}
        if isinstance(ref.get("also"), dict):            # 一次回答连带改了的别的字段（如旧对话的保留天数）
            fields.update(ref["also"])
        for field, old in fields.items():
            if isinstance(field, str) and field:
                if old is not None:
                    profile[field] = old
                else:
                    profile.pop(field, None)
    elif kind == "project":
        store = profile.get("projects")
        cur = store.get(ref.get("key")) if isinstance(store, dict) else None
        # 后来又被改成别的状态就不动它（同规则的撤销口径）
        if isinstance(cur, dict) and (ref.get("status") is None or cur.get("status") == ref["status"]):
            if ref.get("old_status") in protect.PROJECT_STATUSES:
                cur["status"] = ref["old_status"]
            else:                                         # 原来台账里没有这个项目
                store.pop(ref["key"], None)
    elif kind == "governance":
        gov = profile.get("governance")
        if isinstance(gov, dict):
            gov.pop(ref.get("path"), None)
    elif kind == "note":
        notes = profile.get("notes")
        if isinstance(notes, dict):
            notes.pop(ref.get("path"), None)


# ------------------------------------------------------------ identity

def _identity_text(full, title, producer, task):
    text = f"{_short(full)}：{title}"
    if producer:
        text += f"（产自 {producer}）"
    if task:
        text += f"（任务：{task}）"
    return text


def identity_set(path, title, producer=None, task=None, scan_id=None):
    """认下一个目录。返回 (说明列表, 错误列表)。"""
    if not isinstance(title, str) or not title.strip():
        return [], ["要写个名字告诉我是做什么的（--title）"]
    full, err = _check_path(path)
    if err:
        return [], [err]
    p = prefs.load()
    ids = p.get("identities")
    if not isinstance(ids, dict):
        ids = p["identities"] = {}
    title = title.strip()
    ids[full] = {"title": title, "producer": producer, "task": task, "at": prefs.now_iso(), "scan_id": scan_id}
    text = _identity_text(full, title, producer, task)
    _log(p, "identity", text, ref={"path": full}, scan_id=scan_id)
    prefs.save(p)
    return [f"已记住：{text}"], []


def identity_forget(path):
    """忘掉一个认过的目录（对应记忆日志一并标 forgotten）。"""
    if not isinstance(path, str) or not path.strip():
        return [], ["路径不能为空"]
    full = protect.real(path)
    p = prefs.load()
    ids = p.get("identities") if isinstance(p.get("identities"), dict) else {}
    if full not in ids:
        return [], [f"没有记过这个目录：{path}"]
    ids.pop(full, None)
    for e in p.get("memory_log") or []:
        if isinstance(e, dict) and e.get("kind") == "identity" and (e.get("ref") or {}).get("path") == full:
            e["forgotten"] = True
    prefs.save(p)
    return [f"已忘掉：{_short(full)}"], []


def identity_list():
    ids = prefs.load().get("identities") or {}
    if not isinstance(ids, dict) or not ids:
        return ["还没有认下任何目录"], []
    lines = []
    for path in sorted(ids):
        rec = ids[path] if isinstance(ids[path], dict) else {}
        extra = []
        if rec.get("producer"):
            extra.append(f"产自 {rec['producer']}")
        if rec.get("task"):
            extra.append(f"任务：{rec['task']}")
        lines.append(f"{_short(path)}：{rec.get('title') or '（没写名字）'}" + (f"（{'，'.join(extra)}）" if extra else ""))
    return lines, []


# ------------------------------------------------------------ governance

def govern_record(path, change, undo, bytes_, scan_id=None):
    """记下“从源头改好的”一处改动，下次扫描用 govern check 复查。"""
    if not isinstance(change, str) or not change.strip():
        return [], ["要写清楚改了什么（--change）"]
    if not isinstance(undo, str) or not undo.strip():
        return [], ["要写清楚怎么撤销（--undo）"]
    try:
        n = int(bytes_)
    except (TypeError, ValueError):
        return [], ["字节数只能是整数（--bytes）"]
    if n < 0:
        return [], ["字节数不能是负数"]
    full, err = _check_path(path)
    if err:
        return [], [err]
    p = prefs.load()
    gov = p.get("governance")
    if not isinstance(gov, dict):
        gov = p["governance"] = {}
    gov[full] = {"change": change.strip(), "undo": undo.strip(), "at": prefs.now_iso(),
                 "baseline_bytes": n, "scan_id": scan_id}
    _log(p, "governance", change.strip(), ref={"path": full, "undo": undo.strip()}, scan_id=scan_id)
    prefs.save(p)
    return [f"已记下：{change.strip()}（基准 {units.fmt_bytes(n)}）"], []


def _norm(path):
    return os.path.normpath(path) if isinstance(path, str) else ""


def _node_bytes(nodes, target):
    """目标路径在 facts 节点里的现值：同路径优先；没有同路径就取它之下最上层节点的字节合计（父节点已含子节点，
    只取最上层，免得重复计）；都没有返回 None。"""
    target = _norm(target)
    if not target:
        return None
    exact = [n for n in nodes if _norm(n.get("path")) == target]
    if exact:
        return int(exact[0].get("bytes") or 0)
    under = {_norm(n.get("path")): int(n.get("bytes") or 0) for n in nodes}
    children = [p for p in under if p.startswith(target.rstrip("/") + "/")]
    if not children:
        return None
    top = [p for p in children if not any(o != p and p.startswith(o.rstrip("/") + "/") for o in children)]
    return sum(under[p] for p in top)


def govern_check(run):
    """对每条 governance，在 <run>/facts.json 的节点里找回现值，写 <run>/governance-check.json。"""
    run = os.path.realpath(run)
    facts_path = os.path.join(run, "facts.json")
    if not os.path.isfile(facts_path):
        raise ValueError(f"找不到扫描结果：{facts_path}")
    facts = _read_json(facts_path)
    nodes = [n for n in ((facts or {}).get("nodes") or []) if isinstance(n, dict)]
    gov = prefs.load().get("governance") or {}
    out = []
    for path, rec in gov.items():
        rec = rec if isinstance(rec, dict) else {}
        baseline = int(rec.get("baseline_bytes") or 0)
        now = _node_bytes(nodes, path)
        if now is None:
            out.append({"path": path, "change": rec.get("change") or "", "baseline_bytes": baseline,
                        "now_bytes": None, "delta": None, "verdict": "unknown"})
            continue
        delta = now - baseline
        verdict = "stopped" if delta <= baseline * 0.01 else "growing"
        out.append({"path": path, "change": rec.get("change") or "", "baseline_bytes": baseline,
                    "now_bytes": now, "delta": delta, "verdict": verdict})
    out.sort(key=lambda x: x["path"])
    _write_json(os.path.join(run, "governance-check.json"), out)
    return out


# ------------------------------------------------------------ pending（待确认的一句话）

def _normalize_pending_value(kind, value):
    """把 --value 规范化成可存的形态。返回 (值, 错误)。"""
    if kind == "protect":
        raws = value if isinstance(value, list) else ([value] if isinstance(value, str) else None)
        if not raws:
            return None, "“别动”要给出目录（--value 里写路径）"
        paths, errs = prefs._home_paths([x for x in raws if isinstance(x, str) and x.strip()])
        if not paths:
            return None, "；".join(errs) or "没有可用的目录"
        return {"paths": list(dict.fromkeys(paths))}, None
    if kind == "note":
        path = value.get("path") if isinstance(value, dict) else (value if isinstance(value, str) else None)
        if not path:
            return None, "“备注”要给出记在哪个目录（--value 里写 path）"
        paths, errs = prefs._home_paths([path])
        if not paths:
            return None, "；".join(errs)
        return {"path": paths[0]}, None
    if not isinstance(value, dict) or not isinstance(value.get("key"), str) or not value.get("action"):
        return None, '“规则”要给出规则键和动作（--value \'{"key":"…","action":"delete"}\'）'
    key, action = value["key"], value["action"]
    if not prefs._KEY.match(key or ""):
        return None, f"规则键不合法：{key!r}"
    if action not in RULE_ACTIONS:
        return None, "动作只能是 delete（以后都清）或 keep（以后都留）"
    return {"key": key, "action": action}, None


def _parse_value(raw):
    """--value 是 JSON；protect / note 也接受直接写一个路径（路径通常不是合法 JSON）。"""
    if raw is None:
        return None
    raw = raw.strip()
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def pending_add(text, kind, value=None):
    """记一句“用户提过、还没确认”的话，等看板“对 / 不对”或设置页确认。"""
    if kind not in PENDING_KINDS:
        return [], [f"类型只能是 {'/'.join(PENDING_KINDS)}"]
    if not isinstance(text, str) or not text.strip():
        return [], ["要记住的话不能为空（--text）"]
    value, err = _normalize_pending_value(kind, value)
    if err:
        return [], [err]
    p = prefs.load()
    entry = _new_pending(p, text.strip(), kind, value)
    prefs.save(p)
    return [f"已记下待确认（{entry['id']}）：{text.strip()}"], []


def pending_list():
    pend = prefs.load().get("pending_rules") or []
    pend = [x for x in pend if isinstance(x, dict)]
    if not pend:
        return ["没有待确认的话"], []
    return [f"{x.get('id')}［{x.get('kind') or '?'}］{x.get('text') or ''}" for x in pend], []


def _apply_pending(profile, item):
    """确认一条待确认的话，写进 profile（不存盘）。返回 (说明, 错误)。"""
    kind = item.get("kind")
    value = item.get("value") if isinstance(item.get("value"), dict) else {}
    text = item.get("text") or ""
    scan_id = item.get("scan_id")
    if kind == "protect":
        raws = value.get("paths") if isinstance(value.get("paths"), list) else []
        paths, errs = prefs._home_paths([x for x in raws if isinstance(x, str) and x.strip()])
        if not paths:
            return [], ["“" + text + "”里的目录现在不能当保护目录：" + ("；".join(errs) or "没有可用目录")]
        profile["protect"] = list(dict.fromkeys(list(profile.get("protect") or []) + paths))
        short = "、".join(paths)
        _log(profile, "protect", text or f"永不触碰：{short}", ref={"paths": paths}, scan_id=scan_id)
        return [f"已记下：永不触碰 {short}"], []
    if kind == "rule":
        value2, err = _normalize_pending_value("rule", value)
        if err:
            return [], [err]
        key, action = value2["key"], value2["action"]
        profile.setdefault("rules", {})[key] = {"action": action, "at": prefs.now_iso(), "source": "memory"}
        _log(profile, "rule", text or key, ref={"key": key, "action": action}, scan_id=scan_id)
        return [f"已记下：{key} 这一类{'以后都清' if action == 'delete' else '以后都留'}"], []
    if kind == "note":
        value2, err = _normalize_pending_value("note", value)
        if err:
            return [], [err]
        profile.setdefault("notes", {})[value2["path"]] = text
        _log(profile, "note", text, ref={"path": value2["path"]}, scan_id=scan_id)
        return [f"已记下备注：{text}"], []
    return [], [f"不认识这条待确认的类型：{kind}"]


def confirm(ids):
    """确认待确认的话（prefs.apply / 看板“确认规则:”调用）。返回 (说明列表, 错误列表)。"""
    p = prefs.load()
    items = {x.get("id"): x for x in (p.get("pending_rules") or []) if isinstance(x, dict)}
    done, errs, ok = [], [], set()
    for pid in _id_list(ids):
        if pid in ok:
            continue
        item = items.get(pid)
        if item is None:
            errs.append(f"没有这条待确认：{pid}")
            continue
        d, e = _apply_pending(p, item)
        done += d
        errs += e
        if not e:
            ok.add(pid)
    if ok:
        p["pending_rules"] = [x for x in (p.get("pending_rules") or [])
                              if not (isinstance(x, dict) and x.get("id") in ok)]
        prefs.save(p)
    return done, errs


def reject(ids):
    """不要这些待确认的话（看板“不要规则:”调用）。直接移除，不写记忆日志。"""
    p = prefs.load()
    items = {x.get("id"): x for x in (p.get("pending_rules") or []) if isinstance(x, dict)}
    done, errs, ok = [], [], set()
    for pid in _id_list(ids):
        if pid in ok:
            continue
        item = items.get(pid)
        if item is None:
            errs.append(f"没有这条待确认：{pid}")
            continue
        ok.add(pid)
        done.append(f"已不要：{item.get('text') or pid}")
    if ok:
        p["pending_rules"] = [x for x in (p.get("pending_rules") or [])
                              if not (isinstance(x, dict) and x.get("id") in ok)]
        prefs.save(p)
    return done, errs


# ------------------------------------------------------------ remembered / forget

def remembered(run, since=None):
    """汇总本次记住的条目（scan_id 与本次扫描一致，或 at ≥ since），写 <run>/remembered.json。"""
    run = os.path.realpath(run)
    if not os.path.isdir(run):
        raise ValueError(f"运行目录不存在：{run}")
    scan_id = None
    facts_path = os.path.join(run, "facts.json")
    if os.path.isfile(facts_path):
        try:
            facts = _read_json(facts_path)
        except ValueError:
            facts = {}
        scan_id = facts.get("scan_id") if isinstance(facts, dict) else None
    out = []
    for e in prefs.load().get("memory_log") or []:
        if not isinstance(e, dict) or e.get("forgotten"):
            continue
        matched = (bool(scan_id) and e.get("scan_id") == scan_id) or (
            bool(since) and isinstance(e.get("at"), str) and e["at"] >= since)
        if not matched:
            continue
        out.append({"id": e.get("id"), "kind": e.get("kind") or "", "text": e.get("text") or ""})
    _write_json(os.path.join(run, "remembered.json"), out)
    return out


def forget(ids):
    """忘掉这些记忆：撤销对应的改动，并在日志里标 forgotten（不物理删日志）。"""
    p = prefs.load()
    log = p.get("memory_log") if isinstance(p.get("memory_log"), list) else []
    by_id = {e.get("id"): e for e in log if isinstance(e, dict)}
    done, errs, ok = [], [], set()
    for mid in _id_list(ids):
        if mid in ok:
            continue
        e = by_id.get(mid)
        if e is None:
            errs.append(f"没有这条记忆：{mid}")
            continue
        ok.add(mid)
        if e.get("forgotten"):
            done.append(f"已经忘过了：{e.get('text') or mid}")
            continue
        _undo(p, e)
        e["forgotten"] = True
        done.append(f"已忘掉：{e.get('text') or mid}")
    if ok:
        prefs.save(p)
    return done, errs


def parse_forget_text(text):
    """从成果页复制来的文本里取出要忘掉的编号（“忘掉: m1,m2”可多行）。"""
    ids = []
    for line in (text or "").splitlines():
        line = line.strip()
        for head in ("忘掉:", "忘掉："):
            if line.startswith(head):
                ids += _id_list(line[len(head):])
                break
    return list(dict.fromkeys(ids))


# ------------------------------------------------------------ CLI

def _cmd_identity(args):
    if args.action == "set":
        return identity_set(args.path, args.title, args.producer, args.task, args.scan_id)
    if args.action == "forget":
        return identity_forget(args.path)
    return identity_list()


def _cmd_govern(args):
    if args.action == "record":
        return govern_record(args.path, args.change, args.undo, args.bytes, args.scan_id)
    rows = govern_check(args.run)
    lines = [f"治理复查：{os.path.join(os.path.realpath(args.run), 'governance-check.json')}"]
    for r in rows:
        short = _short(r["path"])
        if r["verdict"] == "stopped":
            lines.append(f"已停止增长：{short}")
        elif r["verdict"] == "growing":
            lines.append(f"还在涨（{units.fmt_bytes(r['delta'])}）：{short}")
        else:
            lines.append(f"这次没找到：{short}")
    return lines, []


def _cmd_pending(args):
    if args.action == "add":
        return pending_add(args.text, args.kind, _parse_value(args.value))
    return pending_list()


def _cmd_remembered(args):
    rows = remembered(args.run, args.since)
    lines = [f"这次我记住了 {len(rows)} 条：{os.path.join(os.path.realpath(args.run), 'remembered.json')}"]
    lines += [f"{r['id']}［{r['kind']}］{r['text']}" for r in rows]
    return lines, []


def _cmd_forget(args):
    ids = _id_list(args.ids) if args.ids else []
    if args.from_text:
        try:
            with open(args.from_text, encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            return [], [f"读不了文件 {args.from_text}：{exc}"]
        ids += parse_forget_text(text)
    if not ids:
        return [], ["要给出要忘掉的编号（--ids m1,m2 或 --from-text 文件）"]
    return forget(ids)


def _parser():
    ap = argparse.ArgumentParser(description="叁笙清理：记忆")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ident = sub.add_parser("identity", help="认下的目录")
    ia = ident.add_subparsers(dest="action", required=True)
    s = ia.add_parser("set", help="认下一个目录")
    s.add_argument("--path", required=True, help="目录（家目录下或 Claude 会话临时目录下）")
    s.add_argument("--title", required=True, help="它是什么")
    s.add_argument("--producer", help="谁产出的")
    s.add_argument("--task", help="属于哪个任务")
    s.add_argument("--scan-id", dest="scan_id", help="这次扫描的编号")
    f = ia.add_parser("forget", help="忘掉一个认过的目录")
    f.add_argument("--path", required=True)
    ia.add_parser("list", help="列出认下的目录")

    gov = sub.add_parser("govern", help="从源头改好的")
    ga = gov.add_subparsers(dest="action", required=True)
    r = ga.add_parser("record", help="记下一处改动")
    r.add_argument("--path", required=True)
    r.add_argument("--change", required=True, help="改了什么")
    r.add_argument("--undo", required=True, help="怎么撤销")
    r.add_argument("--bytes", required=True, help="改动时的字节数（基准）")
    r.add_argument("--scan-id", dest="scan_id")
    c = ga.add_parser("check", help="下次扫描时复查")
    c.add_argument("--run", required=True, help="运行目录（含 facts.json）")

    pend = sub.add_parser("pending", help="待确认的一句话")
    pa = pend.add_subparsers(dest="action", required=True)
    a = pa.add_parser("add")
    a.add_argument("--text", required=True)
    a.add_argument("--kind", required=True, choices=list(PENDING_KINDS))
    a.add_argument("--value", help="JSON：protect 写路径（或数组），rule 写 {key,action}，note 写 {path}")
    pa.add_parser("list")

    rem = sub.add_parser("remembered", help="汇总这次记住的条目")
    rem.add_argument("--run", required=True)
    rem.add_argument("--since", help="ISO 时间：这之后的记忆也算进本次")

    gf = sub.add_parser("forget", help="忘掉这些记忆")
    gf.add_argument("--ids", help="形如 m1,m2")
    gf.add_argument("--from-text", dest="from_text", help="成果页复制来的文本文件（“忘掉: m1,m2”）")
    return ap


def main(argv=None):
    args = _parser().parse_args(argv)
    handlers = {"identity": _cmd_identity, "govern": _cmd_govern, "pending": _cmd_pending,
                "remembered": _cmd_remembered, "forget": _cmd_forget}
    try:
        lines, errs = handlers[args.cmd](args)
    except ValueError as exc:
        lines, errs = [], [str(exc)]
    for line in lines:
        print(line)
    for err in errs:
        print(f"错误：{err}", file=sys.stderr)
    return 2 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
