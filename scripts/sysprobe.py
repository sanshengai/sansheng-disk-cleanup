"""probe system：只读的系统层探针（FDA、本地快照、三口径容量、System Data 未归因粗估、系统级大项）。
所有外部命令只读、带超时；不用 sudo。由 scan.py 稍后注册为子命令。"""
import glob
import json
import os
import plistlib
import subprocess
from datetime import datetime, timezone

import protect

CMD_TIMEOUT = 15
DU_TIMEOUT = 60
SWIFT_SCRIPT = ('import Foundation; let v = try URL(fileURLWithPath:"/").resourceValues('
                'forKeys:[.volumeAvailableCapacityForImportantUsageKey]); '
                'print(v.volumeAvailableCapacityForImportantUsage ?? -1)')


def run_cmd(args, timeout=CMD_TIMEOUT):
    """返回 (stdout, reason)；失败 stdout 为 None 并给 reason。"""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, text=True)
    except subprocess.TimeoutExpired:
        return None, "超时（%ds）" % timeout
    except (OSError, ValueError) as e:
        return None, "无法执行：%s" % e
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        return None, "退出码 %d：%s" % (r.returncode, msg[0][:200] if msg else "")
    return r.stdout, None


# ───────── 1. 完全磁盘访问 ─────────

def fda_probe_paths():
    h = protect.HOME
    return [os.path.join(h, "Library/Mail"), os.path.join(h, "Library/Safari"),
            os.path.join(h, "Library/Containers/com.apple.Safari"),
            "/Library/Application Support/com.apple.TCC"]


def probe_full_disk_access():
    import errno
    probes = []
    for p in fda_probe_paths():
        try:
            os.listdir(p)
            probes.append({"path": p, "result": "ok", "errno": None})
        except PermissionError as e:
            probes.append({"path": p, "result": "denied", "errno": e.errno or errno.EPERM})
        except FileNotFoundError as e:
            probes.append({"path": p, "result": "missing", "errno": e.errno or errno.ENOENT})
        except OSError as e:
            probes.append({"path": p, "result": "error", "errno": e.errno})
    results = [x["result"] for x in probes]
    if "ok" in results:
        state = "granted"
    elif "denied" in results:
        state = "denied"
    else:
        state = "inconclusive"
    return {"state": state, "probes": probes}


# ───────── 2. 本地快照 ─────────

def classify_snapshot(name):
    if "com.apple.os.update-" in name:
        return "os_update"
    if "com.apple.TimeMachine." in name:
        return "time_machine"
    return "other"


def parse_snapshots(text):
    names = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("com.apple.") or "snapshot" in line.lower() and "." in line and " " not in line:
            names.append(line)
    return names


def probe_local_snapshots():
    out = {"note": "tmutil 不提供单个快照占用大小；快照体积只能从容量差额粗估", "snapshots": [],
           "count_by_kind": {"os_update": 0, "time_machine": 0, "other": 0},
           "os_update_note": "os_update 快照由系统更新管理，tmutil 删不掉",
           "list_reason": None, "latest_backup": None, "latest_backup_reason": None,
           "destinations": None, "destinations_reason": None}
    txt, why = run_cmd(["tmutil", "listlocalsnapshots", "/"])
    if txt is None:
        out["list_reason"] = why
        out["snapshots"] = None
        out["count_by_kind"] = None
    else:
        for n in parse_snapshots(txt):
            k = classify_snapshot(n)
            out["snapshots"].append({"name": n, "kind": k})
            out["count_by_kind"][k] += 1
    txt, why = run_cmd(["tmutil", "latestbackup"])
    if txt is None:
        out["latest_backup_reason"] = why
    else:
        v = txt.strip()
        if v:
            out["latest_backup"] = v
        else:
            out["latest_backup_reason"] = "无输出（无备份）"
    txt, why = run_cmd(["tmutil", "destinationinfo"])
    if txt is None:
        # destinationinfo 无目的地时常以非零退出并提示 No destinations configured
        if why and "No destinations" in why:
            out["destinations"] = False
        else:
            out["destinations_reason"] = why
    else:
        out["destinations"] = ("Name" in txt or "ID" in txt) and "No destinations" not in txt
    return out


# ───────── 3. 容量三口径 ─────────

def probe_capacity():
    cap = {"statvfs_free": None, "statvfs_total": None, "apfs_container_free": None,
           "apfs_container_size": None, "finder_available": None, "finder_reason": None,
           "diskutil_reason": None, "purgeable_estimate": None, "purgeable_reason": None}
    try:
        st = os.statvfs("/System/Volumes/Data")
        cap["statvfs_free"] = st.f_bavail * st.f_frsize
        cap["statvfs_total"] = st.f_blocks * st.f_frsize
    except OSError as e:
        cap["statvfs_reason"] = str(e)
    txt, why = run_cmd(["diskutil", "info", "-plist", "/"])
    if txt is None:
        cap["diskutil_reason"] = why
    else:
        try:
            d = plistlib.loads(txt.encode("utf-8"))
            cap["apfs_container_free"] = d.get("APFSContainerFree")
            cap["apfs_container_size"] = d.get("APFSContainerSize")
        except Exception as e:  # noqa: BLE001
            cap["diskutil_reason"] = "plist 解析失败：%s" % e
    txt, why = run_cmd(["swift", "-e", SWIFT_SCRIPT], timeout=15)
    if txt is None:
        cap["finder_reason"] = why
    else:
        try:
            v = int(float(txt.strip().splitlines()[-1]))
            if v < 0:
                cap["finder_reason"] = "系统未返回该容量键"
            else:
                cap["finder_available"] = v
        except (ValueError, IndexError):
            cap["finder_reason"] = "输出无法解析：%r" % txt[:100]
    fa, sf = cap["finder_available"], cap["statvfs_free"]
    if fa is not None and sf is not None:
        if fa - sf >= 0:
            cap["purgeable_estimate"] = fa - sf
        else:
            cap["purgeable_reason"] = "Finder 口径小于 statvfs，差值为负，不采信"
    else:
        cap["purgeable_reason"] = "缺少 Finder 或 statvfs 口径"
    return cap


# ───────── 4. System Data 粗估 ─────────

def probe_system_data(run, capacity):
    out = {"container_used": None, "metered": None, "unattributed": None, "reason": None,
           "caveat": "粗估：扫描范围外目录、权限盲区、本地快照、APFS 开销都会落进未归因，不等于 System Data 或可清理量"}
    if not run:
        out["reason"] = "未提供 --run"
        return out
    fp = os.path.join(run, "facts.json")
    try:
        with open(fp, encoding="utf-8") as f:
            facts = json.load(f)
    except (OSError, ValueError) as e:
        out["reason"] = "读不到 facts.json：%s" % e
        return out
    metered = (facts.get("coverage") or {}).get("walked_bytes")
    out["metered"] = metered
    size, free = capacity.get("apfs_container_size"), capacity.get("apfs_container_free")
    if size is not None and free is not None:
        out["container_used"] = size - free
    else:
        d = facts.get("disk") or {}
        if d.get("used"):
            out["container_used"] = d["used"]
            out["reason"] = "容器口径缺失，改用 facts.disk.used"
    if out["container_used"] is not None and metered is not None:
        out["unattributed"] = out["container_used"] - metered
    elif out["reason"] is None:
        out["reason"] = "facts 缺少 walked_bytes 或容量"
    return out


# ───────── 5. 系统级大项 ─────────

def du_item(path):
    item = {"path": path, "exists": os.path.lexists(path), "bytes": None, "status": None}
    if not item["exists"]:
        item["status"] = "missing"
        return item
    try:
        r = subprocess.run(["du", "-sk", path], capture_output=True, timeout=DU_TIMEOUT, text=True)
    except subprocess.TimeoutExpired:
        item["status"] = "timeout"
        return item
    except OSError as e:
        item["status"] = "error"
        item["reason"] = str(e)
        return item
    first = (r.stdout or "").split("\t")[0].strip()
    denied = "Operation not permitted" in (r.stderr or "") or "Permission denied" in (r.stderr or "")
    if first.isdigit():
        item["bytes"] = int(first) * 1024
        item["status"] = "partial_denied" if denied else "ok"
    else:
        item["status"] = "denied" if denied else "error"
        if not denied:
            item["reason"] = (r.stderr or "").strip()[:200]
    return item


def install_app_info(app):
    info = {"name": os.path.basename(app), "version": None, "bundle_id": None, "is_install_assistant": False}
    try:
        with open(os.path.join(app, "Contents/Info.plist"), "rb") as f:
            d = plistlib.load(f)
        info["version"] = d.get("CFBundleShortVersionString")
        info["bundle_id"] = d.get("CFBundleIdentifier")
        info["is_install_assistant"] = str(info["bundle_id"] or "").startswith("com.apple.InstallAssistant.")
    except Exception as e:  # noqa: BLE001
        info["reason"] = "Info.plist 读不到：%s" % e
    return info


def probe_big_system_items():
    h = protect.HOME
    items = []
    v = du_item("/private/var/folders")
    v["note"] = "只统计当前用户可读部分，其他用户/系统子目录读不到会少算"
    items.append(v)
    for app in sorted(glob.glob("/Applications/Install macOS*.app")):
        it = du_item(app)
        it.update(install_app_info(app))
        items.append(it)
    paths = sorted(glob.glob(os.path.join(h, "Library/iTunes/*Software Updates")))
    paths += [os.path.join(h, "Library/Logs/DiagnosticReports"), "/Library/Logs/DiagnosticReports",
              "/private/var/db/diagnostics", "/private/var/db/powerlog"]
    for p in paths:
        items.append(du_item(p))
    for p in ("/Library/Updates", "/macOS Install Data"):
        it = du_item(p)
        it["do_not_touch"] = True
        it["note"] = "系统自己管理，不要动"
        items.append(it)
    return items


# ───────── 入口 ─────────

def probe_system(opts):
    errors = []
    out = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "errors": errors}

    def safe(key, fn, *a):
        try:
            out[key] = fn(*a)
        except Exception as e:  # noqa: BLE001
            out[key] = None
            errors.append("%s: %s" % (key, e))
    safe("full_disk_access", probe_full_disk_access)
    safe("local_snapshots", probe_local_snapshots)
    safe("capacity", probe_capacity)
    safe("system_data", probe_system_data, getattr(opts, "run", None), out.get("capacity") or {})
    safe("big_system_items", probe_big_system_items)
    return out
