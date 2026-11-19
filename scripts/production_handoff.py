"""Read project-local production handoffs as evidence for ordinary cleanup review."""
import glob
import hashlib
import json
import os
from pathlib import Path


def discover(work, patterns, explicit):
    paths = {os.path.abspath(os.path.expanduser(p)) for p in explicit}
    for pattern in patterns:
        paths.update(glob.glob(os.path.join(work, pattern), recursive=True))
    return sorted(paths)


def inside(path, root):
    return path == root or root in path.parents


def readable_evidence(path):
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            return bool(f.read(1))
    except OSError:
        return False


def read(filename):
    if Path(filename).is_symlink():
        raise ValueError(f"交付清单不能为符号链接 {filename}")
    manifest = Path(filename).resolve(strict=True)
    root = manifest.parent
    raw = manifest.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema") != "production-handoff/1":
        raise ValueError(f"{manifest} 生产清单格式错误")
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{manifest} 缺少非空 entries")
    rows = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("生产条目必须是对象")
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or Path(rel).as_posix() != rel or ".." in Path(rel).parts:
            raise ValueError(f"生产条目路径无效 {rel!r}")
        path = (root / rel).resolve()
        if not inside(path, root) or path in (root, manifest) or path != root / rel or rel in rows:
            raise ValueError(f"生产条目越界、重复或路径歧义 {rel}")
        role = entry.get("role")
        reason = entry.get("reason")
        if role not in ("source", "delivery", "evidence", "intermediate") or not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"生产条目缺角色或依据 {rel}")
        rows[rel] = dict(entry, absolute=str(path), decision="keep")
    deliveries = [r for r in rows.values() if r["role"] == "delivery"]
    retained = [r for r in rows.values() if r["role"] in ("delivery", "source")]
    incomplete = not deliveries or any(not Path(r["absolute"]).exists() for r in retained)
    for row in rows.values():
        if row["role"] != "intermediate":
            continue
        evidence = row.get("evidence")
        if incomplete:
            row["hold"] = "缺少交付物或已登记的原料、交付路径不在"
        elif row.get("consumers") != []:
            row["hold"] = "仍有消费者或消费者未核实"
        elif not isinstance(evidence, list) or not evidence or not all(
            isinstance(key, str) and key in rows and rows[key]["role"] == "evidence"
            and readable_evidence(rows[key]["absolute"]) for key in evidence
        ):
            row["hold"] = "缺少可读的阶段证据文件"
        else:
            row["decision"] = "review"
    return {"manifest": str(manifest), "root": str(root), "sha256": hashlib.sha256(raw).hexdigest(),
            "entries": list(rows.values())}


def protection(path, reports, *, declared_only=False):
    target = Path(path).resolve()
    for report in reports:
        root = Path(report["root"])
        if not (inside(target, root) or inside(root, target)):
            continue
        if inside(Path(report["manifest"]), target):
            return "包含生产交付清单"
        matched = False
        for row in report["entries"]:
            entry = Path(row["absolute"])
            if inside(target, entry) or inside(entry, target):
                if row["decision"] != "review":
                    return "生产清单保留 %s（%s）" % (entry, row.get("hold", row["reason"]))
                matched |= inside(target, entry)
        if not matched and not declared_only:
            return "任务目录中的路径未列为无消费者过程件"
    return None
