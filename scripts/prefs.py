"""叁笙清理：本机偏好与成长飞轮（越用越懂你）。

本机偏好存在 ~/.sansheng/disk-cleanup/profile.json（不上传、不进发行包），选择记录存在同目录 choices.jsonl。
飞轮规则：脚本自动记录，不靠自觉；同一类东西在 3 次不同的清理里做了同样的选择，
才问一次“以后都这样吗”；答案三选一（设为规则 / 只这次 / 不用），“不用”冷却 60 天；只存字段与路径，不存对话原文。

“同一类东西”= 规则键：看板类别/第一张知识卡/档位（如 ai-images/codex/recommended = Codex 有副本的出图）；没写知识卡的卡不参与规则。
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

import protect

SPEC_VERSION = 1
SAME_DIRECTION_RUNS = 3
COOLDOWN_DAYS = 60
RETENTION_CHOICES = (0, 7, 15, 30, 60, 90)  # 0 = 不按天数限制（只看任务是否结束）
# 规则键可带项目前缀 project:<项目键哈希 8 位>/（0.5）：记录“这个项目里的这一类”的选择，原有键格式不变
_KEY = re.compile(r"^(project:[0-9a-f]{8}/)?[a-z0-9][a-z0-9._-]*(/[a-z0-9._-]+)*$")


def _state(name):
    return os.path.join(os.path.dirname(protect.PROFILE), name)


def load():
    """读原始 profile（要改写时用）。有效 profile（折进台账的保护与成品目录）用 protect.load_profile / plan.load_profile。"""
    return protect.load_profile(protect.PROFILE, raw=True)


def save(profile):
    path = protect.PROFILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _note(p, kind, text, ref=None, scan_id=None):
    """把一次改动记进 memory_log（成果页“这次我记住了”读它）。只写进传入的 profile 字典，由调用方存盘；
    记忆模块出任何问题都不影响设置本身保存。memory 延迟导入：它在加载时就依赖本模块。"""
    try:
        import memory
        memory.log_change(kind, text, ref=ref, scan_id=scan_id, profile=p)
    except Exception:       # noqa: BLE001
        pass


def rule_key(card, preset, category):
    """规则键 = 看板类别/第一张知识卡/档位。卡片不能自定义（0.4 评审：自定义键可以撞到别的类别的规则）；
    没写知识卡的卡不参与规则（返回 None），免得所有“未归类目录”共用一条规则。"""
    used = card.get("cards_used") if isinstance(card.get("cards_used"), list) else []
    base = used[0] if used and isinstance(used[0], str) else ""
    key = f"{category}/{base}/{preset or 'none'}"
    return key if base and _KEY.match(key) else None


def project_rule_key(project_key, rule_key):
    """带项目前缀的规则键：project:<sha256(项目键)[:8]>/<原规则键>。原规则键不合法时返回 None。"""
    if not rule_key or not _KEY.match(rule_key) or rule_key.startswith("project:"):
        return None
    h = hashlib.sha256(str(project_key).encode("utf-8")).hexdigest()[:8]
    return f"project:{h}/{rule_key}"


def rule_for(profile, key):
    r = (profile.get("rules") or {}).get(key)
    return r.get("action") if isinstance(r, dict) and r.get("action") in ("delete", "keep") else None


# ------------------------------------------------------------ 选择记录

def record_choices(scan_id, offered, chosen_ids):
    """offered：[{id, key, bytes, title}] 本次可执行的卡；chosen_ids：正式执行选中的卡。
    每个规则键记一行：这一类在本次清理里是否被删（这一类里的卡全被选中才算删）。"""
    by_key = {}
    for c in offered:
        # 除原规则键外，卡片属于哪个项目（c["projects"]，项目键列表）就再多记一行带项目前缀的键
        keys = [c["key"]] + [pk for pk in (project_rule_key(p, c["key"]) for p in c.get("projects") or []) if pk]
        for k in keys:
            e = by_key.setdefault(k, {"scan_id": scan_id, "at": now_iso(), "key": k, "chosen": True,
                                      "bytes": 0, "title": c.get("title") or ""})
            e["bytes"] += int(c.get("bytes") or 0)
            e["chosen"] = e["chosen"] and c["id"] in chosen_ids
    path = _state("choices.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for e in by_key.values():
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return list(by_key.values())


def history():
    path = _state("choices.jsonl")
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and isinstance(e.get("key"), str) and isinstance(e.get("chosen"), bool):
                    out.append(e)
    except OSError:
        pass
    return out


def _cooling(profile, key, now=None):
    until = (profile.get("cooldown") or {}).get(key)
    return isinstance(until, (int, float)) and (now or time.time()) < until


def pending_questions(profile, keys):
    """对本次出现的规则键：还没有规则、不在冷却期、最近 3 次不同清理同向 → 问一次。返回 [{key, action, runs, title}]。"""
    by_key = {}
    for e in history():
        runs = by_key.setdefault(e["key"], {})
        runs[e.get("scan_id")] = e                      # 同一次清理多次记录只算一次（后写的为准）
    out = []
    for key in dict.fromkeys(keys):
        if rule_for(profile, key) or _cooling(profile, key):
            continue
        runs = list((by_key.get(key) or {}).values())[-SAME_DIRECTION_RUNS:]
        if len(runs) == SAME_DIRECTION_RUNS and len({r["chosen"] for r in runs}) == 1:
            out.append({"key": key, "action": "delete" if runs[0]["chosen"] else "keep",
                        "runs": SAME_DIRECTION_RUNS, "title": runs[-1].get("title") or key})
    return out


def answer(key, reply, scan_id=None):
    """reply：delete / keep 设为规则；once 只这次（不记）；never 不用（冷却 60 天）。返回一句说明。
    设成规则时写一条记忆日志（kind rule，ref {key, action, old}），“忘掉”会把规则恢复成 old。"""
    if not _KEY.match(key or ""):
        raise ValueError(f"规则键不合法：{key!r}")
    p = load()
    if reply in ("delete", "keep"):
        rules = p.setdefault("rules", {})
        old = rules.get(key) if isinstance(rules.get(key), dict) else None
        rules[key] = {"action": reply, "at": now_iso(), "spec": SPEC_VERSION}
        (p.get("cooldown") or {}).pop(key, None)
        msg = f"以后“{key}”这一类{'直接按规则清' if reply == 'delete' else '一律留着，不再列出'}"
        _note(p, "rule", msg, {"key": key, "action": reply, "old": old}, scan_id)
        save(p)
        return "已记下：" + msg
    if reply == "never":
        p.setdefault("cooldown", {})[key] = time.time() + COOLDOWN_DAYS * 86400
        save(p)
        return f"好的，{COOLDOWN_DAYS} 天内不再问“{key}”"
    if reply == "once":
        return "只这次，不改规则"
    if reply == "forget":
        (p.get("rules") or {}).pop(key, None)
        (p.get("cooldown") or {}).pop(key, None)
        save(p)
        return f"已忘掉“{key}”的规则"
    raise ValueError("回复只能是 delete / keep / once / never / forget")


# ------------------------------------------------------------ 项目台账（导出文本里的“台账:”行）

LEDGER_TITLE = "叁笙清理 · 项目台账"
_LEDGER_STATUS_KEYS = ("状态", "status")
_LEDGER_DELIV_KEYS = ("成品", "deliverables")
_LEDGER_NAME_KEYS = ("名称", "name")


def is_ledger_export(text):
    """文本抬头是“叁笙清理 · 项目台账”：这是保存项目状态的导出，不是清理清单（没有清理授权）。"""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip().startswith(LEDGER_TITLE)
    return False


def parse_ledger(text):
    """从导出文本取“台账:”行。返回 (projects 或 None, 错误)。一行 JSON，形如
    台账: {"项目":{"/Users/x/proj":{"状态":"做完了","成品":["~/成品"],"名称":"proj"}}}
    状态接受中文（进行中/做完了/归档/别碰）与英文（active/done/archived/protected）。
    整行是 JSON，路径里有 | ; 之类字符都不会被拆。没有这一行返回 (None, [])；有但不合法返回 (None, [错误])。"""
    found = None
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("台账:") or line.startswith("台账："):
            found = line[len("台账:"):].strip()
    if found is None:
        return None, []
    try:
        data = json.loads(found)
    except ValueError as exc:
        return None, [f"“台账:”行不是合法的 JSON：{exc}"]
    if not isinstance(data, dict):
        return None, ["“台账:”行的 JSON 必须是对象"]
    projects = data.get("项目") if "项目" in data else data.get("projects")
    if projects is None:
        projects = {}
    if not isinstance(projects, dict):
        return None, ["“台账:”里的“项目”必须是对象（键是项目路径）"]
    return projects, []


def normalize_project_key(key):
    """(规范化的项目键, 错误)。路径键必须是家目录下已存在的目录（不含家目录本身）；env: 键必须是已知环境项目。"""
    if not isinstance(key, str) or not key.strip():
        return None, "项目键为空"
    key = key.strip()
    if key.startswith("env:"):
        return (key, None) if key in protect.ENV_PROJECT_ROOTS else (None, f"不认识的环境项目：{key}")
    full = protect.real(key)
    home = protect.lexical("~")
    if full == home or not full.startswith(home.rstrip("/") + "/"):
        return None, f"只接受家目录下的项目目录：{key}"
    if not os.path.isdir(full):
        return None, f"项目目录不存在：{key}"
    return full, None


def _status_of(v):
    if v in protect.PROJECT_STATUSES:
        return v
    return protect.PROJECT_STATUS_FROM_ZH.get(v)


def apply_ledger(p, projects, scan_id=None):
    """把台账行写进 profile 字典 p（不存盘）。返回 (说明列表, 错误列表)。非法项不写，其余照写；
    写了任何一项（含全默认），调用方就要写 onboarded_at。
    状态有变化的项目写一条记忆日志（kind project，ref {key, old_status, status}）：原来就有、状态变了的，
    以及新进台账就不是“进行中”的；新进台账又是默认“进行中”的不记，免得第一次设置时刷屏。"""
    done, errs = [], []
    store = p.setdefault("projects", {})
    if not isinstance(store, dict):
        store = p["projects"] = {}
    counts = {}
    for raw_key, entry in projects.items():
        key, err = normalize_project_key(raw_key)
        if err:
            errs.append(err)
            continue
        if not isinstance(entry, dict):
            errs.append(f"项目 {raw_key} 的内容必须是对象")
            continue
        status = None
        for k in _LEDGER_STATUS_KEYS:
            if k in entry:
                status = _status_of(entry[k])
                if status is None:
                    errs.append(f"项目 {raw_key} 的状态只能是 进行中 / 做完了 / 归档 / 别碰，收到 {entry[k]!r}")
                break
        old = store.get(key) if isinstance(store.get(key), dict) else {}
        status = status or (old.get("status") if old.get("status") in protect.PROJECT_STATUSES else None)
        if status is None:
            if not any(k in entry for k in _LEDGER_STATUS_KEYS):
                errs.append(f"项目 {raw_key} 缺少状态")
            continue
        deliv = old.get("deliverables") or []
        for k in _LEDGER_DELIV_KEYS:
            if k in entry:
                deliv, e = _home_paths(entry[k] if isinstance(entry[k], list) else [], deliverable=True)
                errs += e
                break
        name = next((entry[k] for k in _LEDGER_NAME_KEYS if isinstance(entry.get(k), str) and entry[k].strip()), None)
        name = (name or old.get("name") or protect.ENV_PROJECT_NAMES.get(key) or os.path.basename(key.rstrip("/")) or key)[:80]
        old_status = old.get("status") if old.get("status") in protect.PROJECT_STATUSES else None
        store[key] = {"name": name, "status": status, "deliverables": list(dict.fromkeys(deliv)),
                      "updated_at": now_iso(), "source": "board"}
        if status != old_status and (old_status is not None or status != "active"):
            _note(p, "project", f"项目“{name}”标为{protect.PROJECT_STATUS_ZH.get(status, status)}",
                  {"key": key, "old_status": old_status, "status": status}, scan_id)
        counts[status] = counts.get(status, 0) + 1
    if counts:
        done.append("项目台账：" + "、".join(f"{protect.PROJECT_STATUS_ZH[s]} {n} 个" for s, n in counts.items()
                                         if s in protect.PROJECT_STATUS_ZH))
    return done, errs, bool(counts) or not projects


def set_project(key, status, deliverables=None, scan_id=None):
    """对话里改单个项目的状态（如回答“X 做完了吗”）。返回一句说明；不合法抛 ValueError。"""
    st = _status_of(status)
    if st is None:
        raise ValueError("状态只能是 active / done / archived / protected（进行中 / 做完了 / 归档 / 别碰）")
    p = load()
    done, errs, _ = apply_ledger(p, {key: {"status": st, **({"deliverables": deliverables} if deliverables is not None else {})}},
                                 scan_id)
    if not done:
        raise ValueError("；".join(errs) or "没有保存")
    p["onboarded_at"] = p.get("onboarded_at") or now_iso()
    save(p)
    return done[0] + ("（" + "；".join(errs) + "）" if errs else "")


# ------------------------------------------------------------ 看板清单（② 清理看板“复制清单，发给 Agent”的导出文本）

BOARD_TITLE = "叁笙清理选择"
_BOARD_FIELDS = ("scan_id", "seal", "sel", "清理", "回答")
_CARD_ID = re.compile(r"^[A-Za-z0-9_\-]+$")
_RULE_ID = re.compile(r"^[A-Za-z0-9_\-]+$")
_HEX12 = re.compile(r"^[0-9a-f]{12}$")
_ANSWER = re.compile(r"^([A-Za-z0-9_\-]+)=([^\s,;/，；、=]+)$")


def _rule_ids(raw, label, errs):
    """“确认规则:”“不要规则:”后面的规则号列表（逗号 / 顿号 / 空白分隔；无 = 空）。"""
    out = []
    if raw in (None, "", "无"):
        return out
    for r in re.split(r"[,，、\s]+", raw):
        if not r:
            continue
        if not _RULE_ID.match(r):
            errs.append(f"“{label}:”里的规则号不合法：{r!r}")
        elif r not in out:
            out.append(r)
    return out


def parse_board_export(text):
    """解析 ② 清理看板的导出文本，返回 (字段字典, 错误列表)。只认抬头“叁笙清理选择”和下面几行，其余行（设置/台账/别再列/备注）忽略：
        叁笙清理选择 / scan_id: … / seal: <12 位> / 清理: c01, c03 或 无 / 回答: c02=删 / c05=留 / c06=归档 或 无 / sel: <12 位>
    可选三行（0.6）：归档到: /Volumes/盘名/… / 确认规则: p1, p2 / 不要规则: p3。
    字段字典：scan_id、seal、sel、cards（卡号列表）、answers（["c02=删", …]）、archive_to（路径或 None）、
    confirm_rules / reject_rules（待确认规则的编号列表）。同一字段出现两次、缺字段、格式不对都算错误；
    本函数只解析，不核对 sel 与封存（act.py 用 selection_sig 核对），归档目标是否合格也由 act.py 复核。
    台账导出（抬头“叁笙清理 · 项目台账”）和初始设置导出（“叁笙清理 · 初始设置”）不是清单，这里同样报错。"""
    errs = []
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None, ["清单是空的"]
    if not lines[0].startswith(BOARD_TITLE):
        return None, [f"首行不是“{BOARD_TITLE}”（看板“复制清单”的导出文本）"]
    raw = {}
    for ln in lines[1:]:
        m = re.match(r"^(scan_id|seal|sel|清理|回答|归档到|确认规则|不要规则)\s*[:：]\s*(.*)$", ln)
        if not m:
            continue
        if m.group(1) in raw:
            errs.append(f"“{m.group(1)}:”行出现了多次")
        raw[m.group(1)] = m.group(2).strip()
    for k in _BOARD_FIELDS:
        if k not in raw:
            errs.append(f"缺少“{k}:”行")
    if errs:
        return None, errs
    if not raw["scan_id"] or not re.match(r"^\S+$", raw["scan_id"]):
        errs.append("scan_id 为空或含空白")
    for k in ("seal", "sel"):
        if not _HEX12.match(raw[k]):
            errs.append(f"{k} 必须是 12 位小写十六进制，收到 {raw[k]!r}")
    cards, answers = [], []
    if raw["清理"] != "无":
        for c in re.split(r"[,，、\s]+", raw["清理"]):
            if not c:
                continue
            if not _CARD_ID.match(c):
                errs.append(f"“清理:”里的卡号不合法：{c!r}")
            elif c not in cards:
                cards.append(c)
    if raw["回答"] != "无":
        for a in re.split(r"\s*/\s*", raw["回答"]):
            if not a:
                continue
            if not _ANSWER.match(a):
                errs.append(f"“回答:”里的条目格式应为 卡号=回答，收到 {a!r}")
            else:
                answers.append(a)
    archive_to = raw.get("归档到") or None
    if archive_to in ("无",):
        archive_to = None
    confirm = _rule_ids(raw.get("确认规则"), "确认规则", errs)
    reject = _rule_ids(raw.get("不要规则"), "不要规则", errs)
    both = sorted(set(confirm) & set(reject))
    if both:
        errs.append("同一条规则既确认又不要：" + "、".join(both))
    if errs:
        return None, errs
    return {"scan_id": raw["scan_id"], "seal": raw["seal"], "sel": raw["sel"], "cards": cards, "answers": answers,
            "archive_to": archive_to, "confirm_rules": confirm, "reject_rules": reject}, []


# ------------------------------------------------------------ 初次设置（看板导出文本里的“设置:”“别再列:”行）

def parse_settings(text):
    """从看板导出文本取设置。返回 (settings, hide_keys, errors)。只认这两行，其余行忽略。
    “设置:”后面是一行 JSON（0.5 起：目录名里可以有 | 或 ;），如
    设置: {"保留天数":15,"别碰":["~/a"],"成品":[],"默认":"safe"}；仍兼容旧的 `保留天数=15; 别碰=a|b; 成品=无; 默认=safe`。"""
    settings, hide, errs = {}, [], []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("设置:"):
            body = line[len("设置:"):].strip()
            if body.startswith("{"):
                try:
                    data = json.loads(body)
                except ValueError as exc:
                    errs.append(f"“设置:”行不是合法的 JSON：{exc}")
                    continue
                if not isinstance(data, dict):
                    errs.append("“设置:”行的 JSON 必须是对象")
                    continue
                settings.update(data)
                continue
            for part in body.split(";"):
                if "=" not in part:
                    continue
                k, v = (x.strip() for x in part.split("=", 1))
                settings[k] = v
        elif line.startswith("别再列:"):
            hide += [k.strip() for k in line[len("别再列:"):].split(",") if k.strip() and k.strip() != "无"]
    for k in hide:
        if not _KEY.match(k):
            errs.append(f"别再列里的规则键不合法：{k!r}")
    return settings, [k for k in hide if _KEY.match(k)], errs


def _home_paths(raw, deliverable=False):
    """deliverable=True：成品目录，拒绝太宽泛的位置（~ 本身、~/Library、~/Downloads、~/Documents 根等）。"""
    out, errs = [], []
    if isinstance(raw, list):                       # JSON 格式：数组里每项就是一个目录，里面的 | ; 都是名字的一部分
        cands = [x.strip() for x in raw if isinstance(x, str) and x.strip()]
    else:                                           # 旧格式：用 | 分隔，“无”表示空
        cands = [x.strip() for x in (raw or "").split("|") if x.strip() and x.strip() != "无"]
    for p in cands:
        full = protect.lexical(p)
        home = protect.lexical("~")
        if not (full == home or full.startswith(home.rstrip("/") + "/")) or full == home:
            errs.append(f"只接受家目录下的子目录：{p}")
            continue
        if deliverable and protect.is_broad_deliverable(full):
            errs.append(f"太宽泛，不能当成品目录（要具体到某个项目的成品文件夹）：{p}")
            continue
        if not os.path.isdir(full):
            errs.append(f"目录不存在：{p}")
            continue
        out.append(p if p.startswith("~") else "~" + full[len(home):])
    return out, errs


def parse_rule_decisions(text):
    """看板导出里对“待确认规则”的回答：返回 (确认的规则号, 不要的规则号, 错误)。只认“确认规则:”“不要规则:”两行。"""
    raw, errs = {}, []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(确认规则|不要规则)\s*[:：]\s*(.*?)\s*$", line)
        if m:
            raw[m.group(1)] = m.group(2)
    confirm = _rule_ids(raw.get("确认规则"), "确认规则", errs)
    reject = _rule_ids(raw.get("不要规则"), "不要规则", errs)
    both = sorted(set(confirm) & set(reject))
    if both:
        errs.append("同一条规则既确认又不要：" + "、".join(both))
    return confirm, reject, errs


def apply_rule_decisions(confirm, reject):
    """把对待确认规则的回答交给记忆模块（scripts/memory.py 的 confirm / reject）。返回 (说明列表, 错误列表)。
    记忆模块不存在或调用出错时报“没保存”，不崩溃、不影响同一份文本里别的设置。"""
    done, errs = [], []
    if not confirm and not reject:
        return done, errs
    try:
        import memory                                   # 延迟导入：它读写同一个 profile，不能在本模块加载时就依赖
        for name, ids, fn in (("确认", confirm, getattr(memory, "confirm", None)),
                              ("不要", reject, getattr(memory, "reject", None))):
            if not ids:
                continue
            if fn is None:
                errs.append(f"记忆模块没有提供 {name}规则 的功能，规则 {'、'.join(ids)} 没保存")
                continue
            d, e = fn(list(ids))
            done += list(d or [])
            errs += list(e or [])
    except ImportError:
        errs.append("记忆模块（scripts/memory.py）还不存在，“确认规则”“不要规则”没保存")
    except Exception as exc:    # noqa: BLE001  记忆模块出错不能拖垮同一份文本里别的设置
        errs.append(f"规则没保存（{type(exc).__name__}: {exc}）")
    return done, errs


def apply_settings(text, scan_id=None):
    """把初次设置、项目台账写进 profile。返回 (说明列表, 错误列表)。路径错误的项不写，其余照写。
    “台账:”行见 parse_ledger；成功应用过一次台账（哪怕全是默认状态）就写 onboarded_at。
    0.6：看板导出里的“确认规则: p1,p2”“不要规则: p3”交给 memory.confirm / memory.reject（先存好别的设置再调，它们自己读写 profile）。
    抬头为“叁笙清理 · 项目台账”的导出只保存设置，不含清理授权：这里不读取也不产生任何清理选择。
    每个真正改动的设置、别再列的规则、状态变化的项目都写一条记忆日志（scan_id 由调用方传入，拿不到就靠时间认）。"""
    settings, hide, errs = parse_settings(text)
    ledger, lerrs = parse_ledger(text)
    errs += lerrs
    confirm, reject, rerrs = parse_rule_decisions(text)
    errs += rerrs
    if not settings and not hide and ledger is None and not confirm and not reject:
        return [], errs or ["没有找到“设置:”“别再列:”“台账:”“确认规则:”或“不要规则:”行"]
    p = load()
    done = []
    ledger_ok = False
    if ledger is not None:
        d, e, ledger_ok = apply_ledger(p, ledger, scan_id)
        done += d
        errs += e
        if not ledger:
            done.append("项目台账：全部按默认状态，已记下第一次设置完成")
    if "保留天数" in settings:
        try:
            d = int(settings["保留天数"])
            if d not in RETENTION_CHOICES:
                raise ValueError
            old = p.get("retention_days")
            p["retention_days"] = d
            done.append("AI 对话：" + (f"最近 {d} 天内用过的一律留着" if d else "不按天数限制，只看任务是否结束"))
            if old != d:
                _note(p, "setting", done[-1], {"field": "retention_days", "old": old, "new": p.get("retention_days")}, scan_id)
        except (ValueError, TypeError):
            errs.append(f"保留天数只能是 {'/'.join(map(str, RETENTION_CHOICES))}")
    if "别碰" in settings:
        paths, e = _home_paths(settings["别碰"])
        errs += e
        if paths:
            added = [x for x in paths if x not in (p.get("protect") or [])]
            p["protect"] = list(dict.fromkeys(list(p.get("protect") or []) + paths))
            done.append("永不触碰：" + "、".join(paths))
            if added:
                _note(p, "protect", "永不触碰：" + "、".join(added), {"paths": added}, scan_id)
    if "成品" in settings:
        paths, e = _home_paths(settings["成品"], deliverable=True)
        errs += e
        if paths:
            old_d, old_r = p.get("deliverable_roots"), p.get("reference_roots")
            p["deliverable_roots"] = list(dict.fromkeys(list(p.get("deliverable_roots") or []) + paths))
            # 成品目录也是引用检查目录：删之前在这里查有没有文章、文档引用了要删的东西（0.4 评审 1）
            p["reference_roots"] = list(dict.fromkeys(list(p.get("reference_roots") or []) + paths))
            done.append("成品目录（也在这里查引用）：" + "、".join(paths))
            if p["deliverable_roots"] != (old_d or []) or p["reference_roots"] != (old_r or []):
                _note(p, "setting", done[-1], {"field": "deliverable_roots", "old": old_d,
                                               "new": p.get("deliverable_roots"),
                                               "also": {"reference_roots": old_r},
                                               "also_new": {"reference_roots": p.get("reference_roots")}}, scan_id)
    if "默认" in settings:
        if settings["默认"] in ("safe", "recommended", "all"):
            old = p.get("default_choice")
            p["default_choice"] = settings["默认"]
            done.append("默认档位：" + {"safe": "稳妥", "recommended": "推荐", "all": "全部可删"}[settings["默认"]])
            if old != settings["默认"]:
                _note(p, "setting", done[-1], {"field": "default_choice", "old": old, "new": p.get("default_choice")}, scan_id)
        else:
            errs.append("默认档位只能是 safe / recommended / all")
    for k in hide:
        rules = p.setdefault("rules", {})
        old = rules.get(k) if isinstance(rules.get(k), dict) else None
        rules[k] = {"action": "keep", "at": now_iso(), "spec": SPEC_VERSION, "source": "board"}
        if not (old and old.get("action") == "keep"):
            _note(p, "rule", f"以后“{k}”这一类一律留着，不再列出", {"key": k, "action": "keep", "old": old}, scan_id)
    if hide:
        done.append("以后不再列出：" + "、".join(hide))
    if settings or hide or ledger_ok:      # 只有成功应用过设置或台账才算完成第一次设置（台账全是默认也算）
        p["onboarded_at"] = p.get("onboarded_at") or now_iso()
    save(p)
    d, e = apply_rule_decisions(confirm, reject)       # 放在 save 之后：memory 自己 load / save，免得互相覆盖
    return done + d, errs + e
