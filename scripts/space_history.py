#!/usr/bin/env python3
"""空间记录点与增长调查。只复用扫描元信息，不遍历用户目录，不产生删除授权。"""
import argparse
import gzip
import html
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import protect
import units

SCHEMA = "space-checkpoint/1"
RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
AUX_ROOT = re.compile(r"^/(?:private/)?tmp/claude-\d+$")


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".space-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def compact(facts):
    cov = facts.get("coverage") or {}
    return {
        "schema": SCHEMA, "kind": "scan", "id": facts["scan_id"],
        "scan_id": facts["scan_id"], "created_at": facts["created_at"],
        "host": facts.get("host") or {}, "disk": facts.get("disk") or {},
        "scope": {"roots": cov.get("roots") or [], "max_depth": cov.get("max_depth"),
                  "threshold": facts.get("threshold")},
        "coverage": {k: cov.get(k) for k in
                     ("walked_bytes", "incomplete", "scan_status", "partial_count", "inaccessible_count")},
        "nodes": [{"path": n["path"], "bytes": n["bytes"],
                   "lower_bound": bool(n.get("bytes_lower_bound")),
                   "producer": n.get("producer"), "newest_mtime": n.get("newest_mtime")}
                  for n in facts.get("nodes", [])],
        "skipped": [{"path": s.get("path"), "reason": s.get("reason")} for s in facts.get("skipped", [])],
    }


def valid_scan(point):
    return (isinstance(point, dict) and point.get("schema") == SCHEMA
            and point.get("kind") == "scan" and RUN_ID.fullmatch(point.get("scan_id", ""))
            and point.get("id") == point.get("scan_id")
            and valid_common(point) and isinstance(point.get("nodes"), list)
            and all(isinstance(n.get("path"), str) and n["path"].startswith("/")
                    and isinstance(n.get("bytes"), int) and n["bytes"] >= 0 for n in point["nodes"]))


def valid_common(point):
    try:
        stamp = datetime.fromisoformat(point["created_at"].replace("Z", "+00:00"))
        return (stamp.tzinfo is not None and all(isinstance(point["disk"].get(k), int)
                and point["disk"][k] >= 0 for k in ("total", "used", "free"))
                and bool(point.get("host", {}).get("home")) and bool(roots(point, True)))
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def history_dir(runs_dir):
    return Path(runs_dir).parent / "history"


def save_point(runs_dir, point):
    path = history_dir(runs_dir) / (point["id"] + ".json.gz")
    atomic_write(path, gzip.compress(json_bytes(point), mtime=0))
    return path


def load_points(runs_dir):
    """兼容已有 facts；永久记录优先于可能被改写/移走的运行目录。"""
    points = {}
    for path in sorted(history_dir(runs_dir).glob("*.json.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                p = json.load(f)
            if valid_scan(p) or (p.get("schema") == SCHEMA and p.get("kind") == "cleanup"
                                  and RUN_ID.fullmatch(p.get("scan_id", "")) and valid_common(p)):
                points[p["id"]] = p
        except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError):
            continue
    for path in sorted(Path(runs_dir).glob("*/facts.json")):
        if not RUN_ID.fullmatch(path.parent.name) or path.parent.name in points:
            continue
        try:
            f = read_json(path)
            if f.get("schema") != "facts/1" or f.get("scan_id") != path.parent.name:
                continue
            p = compact(f)
            if valid_scan(p):
                points[p["id"]] = p
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return sorted(points.values(), key=lambda p: (p.get("created_at", ""), p["id"]))


def roots(point, primary=False):
    rs = sorted({os.path.normpath(r) for r in point.get("scope", {}).get("roots", []) if isinstance(r, str)})
    if primary:
        rs = [r for r in rs if not AUX_ROOT.fullmatch(r)]
    return [r for r in rs if not any(r != q and r.startswith(q + "/") for q in rs)]


def incompatible(current, old):
    for field in ("platform", "home"):
        a, b = current.get("host", {}).get(field), old.get("host", {}).get(field)
        if not a or a != b:
            return "电脑或用户目录不同"
    a, b = current.get("disk", {}), old.get("disk", {})
    if a.get("device") is not None and b.get("device") is not None and a["device"] != b["device"]:
        return "磁盘设备不同"
    if a.get("total") and b.get("total") and a["total"] != b["total"]:
        return "磁盘容量不同"
    if not roots(current, True) or roots(current, True) != roots(old, True):
        return "扫描范围不同（局部扫描不能作整机基准）"
    for field in ("max_depth", "threshold"):
        if current.get("scope", {}).get(field) != old.get("scope", {}).get(field):
            return "扫描深度或目录记录门槛不同"
    if old.get("coverage", {}).get("incomplete"):
        return "上次扫描中途停止"
    return None


def select_baseline(current, points, baseline=None):
    rejected = []
    for p in sorted(points, key=lambda p: (p.get("created_at", ""), p["id"]), reverse=True):
        if p.get("kind") != "scan" or p["id"] == current["id"]:
            continue
        if baseline and p["id"] != baseline:
            continue
        why = ("记录时间晚于本次扫描" if p["created_at"] > current["created_at"] else incompatible(current, p))
        if why:
            rejected.append({"scan_id": p["id"], "reason": why})
            if baseline:
                raise ValueError("这个记录不能作基准：" + why)
            continue
        return p, rejected
    if baseline:
        raise ValueError("找不到指定的扫描记录：" + baseline)
    return None, rejected


def under(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def compare(current, old, rejected=(), notes=None):
    result = {"schema": "space-growth/1", "scan_id": current["scan_id"],
              "created_at": current["created_at"], "current": brief(current),
              "baseline": brief(old) if old else None, "skipped_baselines": list(rejected),
              "rows": [], "areas": [], "unmeasured": [],
              "notice": "增长表示占用变化，不代表垃圾或删除建议。父子目录不能相加；未记录、漏扫和不同测量时刻的差额单列。"}
    if not old:
        result["status"] = "first"
        return result
    result["status"] = "compared"
    prev = {n["path"]: n for n in old["nodes"]}
    rows = []
    for n in current["nodes"]:
        p = prev.get(n["path"])
        if not p or p.get("lower_bound") or n.get("lower_bound"):
            result["unmeasured"].append({"path": n["path"], "bytes": n["bytes"],
                                        "reason": "上次未单列，不能按零计算" if not p else "扫描存在缺口，只读到占用下限"})
            continue
        row = {"path": n["path"], "before": p["bytes"], "after": n["bytes"],
               "delta": n["bytes"] - p["bytes"], "producer": n.get("producer")}
        if notes and isinstance(notes.get(n["path"]), str):
            row["explanation"] = notes[n["path"]]
        rows.append(row)
    # 只把互不包含的目录作为归因桶；下钻明细保留，但不重复加入总量。
    selected = []
    for r in sorted(rows, key=lambda x: (x["path"].count("/"), x["path"])):
        if r["path"] in roots(current):
            continue
        if not any(under(r["path"], p) for p in selected):
            selected.append(r["path"])
    result["rows"] = sorted(rows, key=lambda r: (-abs(r["delta"]), r["path"]))
    result["areas"] = [r for r in result["rows"] if r["path"] in selected]
    result["known_delta"] = sum(r["delta"] for r in result["areas"])
    result["disk_used_delta"] = current["disk"]["used"] - old["disk"]["used"]
    result["unattributed_delta"] = result["disk_used_delta"] - result["known_delta"]
    result["scope_changed"] = roots(current) != roots(old)
    result["partial"] = any(p.get("coverage", {}).get("scan_status") != "complete" for p in (current, old))
    result["legacy_volume_identity"] = any(p.get("disk", {}).get("device") is None for p in (current, old))
    return result


def brief(point):
    return {k: point.get(k) for k in ("id", "scan_id", "created_at", "kind", "host", "disk", "scope", "coverage")}


def attach_growth(facts, comparison):
    by_path = {r["path"]: r for r in comparison["rows"]}
    baseline = comparison.get("baseline")
    for n in facts.get("nodes", []):
        r = by_path.get(n["path"])
        n["growth"] = {"prev_scan_id": baseline["scan_id"] if baseline else None,
                       "prev_bytes": r["before"] if r else None, "delta_bytes": r["delta"] if r else None}


def local_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8))).strftime("%m 月 %d 日 %H:%M")
    except (ValueError, AttributeError):
        return str(value or "")


def signed(value):
    return ("+" if value > 0 else "−" if value < 0 else "") + units.fmt_bytes(abs(value))


def render(comparison, points):
    esc = lambda value: html.escape(str(value), quote=True)
    b, c = comparison.get("baseline"), comparison["current"]
    heading = "第一次空间记录已保存"
    sections = []
    if b:
        heading = local_time(b["created_at"]) + " → " + local_time(c["created_at"])
        sections.append('<div class="metrics">' + ''.join(
            '<div><span>' + esc(label) + '</span><strong>' + esc(signed(value)) + '</strong></div>'
            for label, value in (("磁盘已用变化", comparison["disk_used_delta"]),
                                 ("已定位目录的净变化", comparison["known_delta"]),
                                 ("尚未归因的差额", comparison["unattributed_delta"]))) + '</div>')
        for area in comparison["areas"]:
            children = [r for r in comparison["rows"] if r["path"] != area["path"] and under(r["path"], area["path"])]
            children = sorted(children, key=lambda r: -r["delta"])[:8]
            body = '<p>' + esc(area.get("explanation") or "下方目录是这个目录的组成部分，不能重复相加。") + '</p>'
            for r in children:
                body += '<p class="child"><code>' + esc(r["path"]) + '</code><b>' + esc(signed(r["delta"])) + '</b>'
                if r.get("explanation"):
                    body += '<small>' + esc(r["explanation"]) + '</small>'
                body += '</p>'
            sections.append('<details' + (' open' if area["delta"] > 0 else '') + '><summary><code>' + esc(area["path"]) + '</code><b>' + esc(signed(area["delta"])) + '</b></summary><p class="muted">' + esc(units.fmt_bytes(area["before"]) + ' → ' + units.fmt_bytes(area["after"])) + '</p>' + body + '</details>')
        sections.append('<p class="muted">尚未归因可能来自记录门槛以下的目录、权限或云盘缺口、扫描范围外，以及后台程序在不同测量时刻的写入。没有证据时不判为系统垃圾。</p>')
        if comparison.get("legacy_volume_identity"):
            sections.append('<p class="muted">旧记录没有磁盘设备身份，本次依据电脑、用户目录和磁盘容量匹配；未声称已经核验同一卷。</p>')
        if comparison["unmeasured"]:
            sections.append('<details><summary>上次没有可比实测的目录 · ' + str(len(comparison["unmeasured"])) + '</summary>' + ''.join('<p class="child"><code>' + esc(r["path"]) + '</code><small>' + esc(r["reason"]) + '</small></p>' for r in comparison["unmeasured"]) + '</details>')
    else:
        sections.append('<p>下次扫描时自动选取同一电脑、相同扫描范围的记录比较。现有局部扫描不会被当作整机基准。</p>')
    if comparison["skipped_baselines"]:
        sections.append('<details><summary>自动跳过的不可比记录</summary>' + ''.join('<p>' + esc(r["scan_id"] + ' · ' + r["reason"]) + '</p>' for r in comparison["skipped_baselines"]) + '</details>')
    sections.append('<h2>空间记录点</h2><p class="muted">清理后只记录回执里的磁盘实测余量，不冒充目录重新扫描。移入废纸篓也不等于释放。</p><div class="timeline">' + ''.join(
        '<p><time>' + esc(local_time(p["created_at"])) + '</time><span>' + ('清理后实测' if p["kind"] == 'cleanup' else '扫描') + '</span><b>剩余 ' + esc(units.fmt_bytes(p["disk"].get("free", 0))) + '</b><small>' + esc(p["id"]) + '</small></p>'
        for p in reversed(points) if p["created_at"] <= c["created_at"]
        and p.get("host", {}).get("home") == c.get("host", {}).get("home")
        and p.get("host", {}).get("platform") == c.get("host", {}).get("platform")
        and p.get("disk", {}).get("total") == c.get("disk", {}).get("total")
        ) + '</div>')
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>叁笙清理 · 空间增长</title><style>' + """
    :root{color-scheme:light}*{box-sizing:border-box}body{overflow-wrap:anywhere;margin:0;background:#f7f5ef;color:#27382b;font:15px/1.7 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}main{max-width:1040px;margin:auto;padding:40px 24px 80px}header{border-bottom:2px solid #819276;margin-bottom:24px}h1{font-size:32px;letter-spacing:-1px;line-height:1.25}h2{margin-top:40px}.eyebrow,.muted,small{color:#687064}code{font-size:12px;overflow-wrap:anywhere}details{border-top:1px solid #d5dbcf;padding:18px 0}summary{cursor:pointer;display:flex;gap:16px;justify-content:space-between}summary b,.child b{white-space:nowrap}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin:28px 0}.metrics span{display:block;color:#687064}.metrics strong{font-size:30px}.child{display:flex;gap:10px;justify-content:space-between;flex-wrap:wrap;margin-left:18px}.child small{width:100%}.timeline p{display:grid;grid-template-columns:145px 100px 1fr;gap:8px;border-bottom:1px solid #d5dbcf;padding:12px 0}.timeline small{grid-column:1/-1}a{color:#436b45}@media(max-width:620px){main{padding:24px 18px}h1{font-size:26px}.metrics{grid-template-columns:1fr;gap:12px}.metrics div{display:flex;justify-content:space-between;align-items:center}.metrics strong{font-size:24px}.timeline p{grid-template-columns:1fr 1fr}.timeline b{grid-column:1/-1}summary{align-items:start}.child{margin-left:8px}}
    """ + '</style><main><header><div class="eyebrow">叁笙清理 · 空间增长调查</div><h1>' + esc(heading) + '</h1><p>' + esc(comparison["notice"]) + '</p></header>' + ''.join(sections) + '</main></html>'


def make(run, facts=None, baseline=None):
    run = Path(run)
    facts = facts or read_json(run / "facts.json")
    current = compact(facts)
    if not valid_scan(current) or current["scan_id"] != run.name:
        raise ValueError("扫描记录的身份不正确")
    points = load_points(run.parent)
    old, rejected = select_baseline(current, points, baseline)
    notes_path = run / "growth-notes.json"
    notes = read_json(notes_path) if notes_path.exists() else {}
    comparison = compare(current, old, rejected, notes)
    attach_growth(facts, comparison)
    save_point(run.parent, current)
    atomic_write(run / "growth.json", json_bytes(comparison))
    points = [p for p in points if p["id"] != current["id"]] + [current]
    points.sort(key=lambda p: (p["created_at"], p["id"]))
    atomic_write(run / "growth.html", render(comparison, points).encode("utf-8"))
    return comparison


def record_cleanup(run, receipt):
    """仅使用正式回执实测；不读取 report.free_after（那是按条目核算值）。"""
    if receipt.get("schema") == "finalize-trash/1":
        receipt = {"schema": "receipt/1", "scan_id": receipt.get("scan_id"),
                   "mode": "finalize_trash", "dry_run": False,
                   "finished_at": receipt.get("finished_at"),
                   "summary": {"free_after": receipt.get("free_after")}}
    if (receipt.get("schema") != "receipt/1" or receipt.get("dry_run") is not False
            or receipt.get("mode") not in ("full", "pilot", "finalize_trash") or not receipt.get("finished_at")):
        return None
    summary = receipt.get("summary") or {}
    if not isinstance(summary.get("free_after"), int):
        return None
    facts = read_json(Path(run) / "facts.json")
    if not RUN_ID.fullmatch(facts.get("scan_id", "")):
        return None
    if receipt.get("scan_id") != facts.get("scan_id"):
        raise ValueError("清理回执与扫描记录身份不一致")
    point = compact(facts)
    datetime.fromisoformat(receipt["finished_at"].replace("Z", "+00:00"))
    stamp = receipt["finished_at"].replace("-", "").replace(":", "")
    if not re.fullmatch(r"[0-9TZ.+]+", stamp):
        raise ValueError("清理回执时间格式不正确")
    point.update(kind="cleanup", id=point["id"] + "-cleanup-" + stamp,
                 created_at=receipt["finished_at"])
    point.pop("nodes")
    point["disk"] = dict(point["disk"], free=summary["free_after"],
                         used=point["disk"]["total"] - summary["free_after"])
    point["measurement"] = "receipt.summary.free_after"
    return save_point(Path(run).parent, point)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "compare", "import"))
    parser.add_argument("--run")
    parser.add_argument("--runs-dir", default=str(Path(protect.STATE_DIR) / "runs"))
    parser.add_argument("--baseline", help="指定同范围的 scan_id；默认最近一次可比扫描")
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            if not args.run:
                parser.error("compare 需要 --run")
            c = make(args.run, baseline=args.baseline)
            print(str(Path(args.run) / "growth.html"))
            if c.get("baseline"):
                print("基准：" + c["baseline"]["scan_id"] + "；磁盘已用变化 " + signed(c["disk_used_delta"]))
        else:
            points = load_points(args.runs_dir)
            if args.command == "import":
                for p in points:
                    save_point(args.runs_dir, p)
                receipts = list(Path(args.runs_dir).glob("*/receipt*.json"))
                receipts += list(Path(args.runs_dir).glob("*/finalize-trash.json"))
                for r in receipts:
                    if RUN_ID.fullmatch(r.parent.name):
                        try:
                            record_cleanup(r.parent, read_json(r))
                        except (OSError, ValueError, KeyError):
                            continue
                points = load_points(args.runs_dir)
            for p in points:
                print(p["id"], local_time(p["created_at"]), p["kind"], "剩余", units.fmt_bytes(p["disk"].get("free", 0)))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("空间记录：" + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
