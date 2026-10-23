#!/usr/bin/env python3
"""大目录下钻：所有子目录均计入；仅元信息，不跟随链接、不读取正文。"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import time


def census(root, now=None):
    root = Path(root).resolve()
    now = time.time() if now is None else now
    groups, extensions, errors = {}, defaultdict(int), []
    seen = set()
    def error(exc):
        errors.append({"path": exc.filename, "error": str(exc)})
    for dp, dns, fns in os.walk(root, followlinks=False, onerror=error):
        dns[:] = [n for n in dns if not Path(dp, n).is_symlink()]
        for name in fns:
            p = Path(dp, name)
            try:
                st = p.lstat()
                if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) in seen:
                    continue
                seen.add((st.st_dev, st.st_ino))
                rel = p.relative_to(root)
                key = rel.parts[0] if len(rel.parts) > 1 else "(root files)"
                g = groups.setdefault(key, {"name": key, "bytes": 0, "logical_bytes": 0,
                    "files": 0, "recent_24h": 0, "oldest_mtime": st.st_mtime,
                    "newest_mtime": st.st_mtime, "oldest_birthtime": None})
                allocated = getattr(st, "st_blocks", 0) * 512
                g["bytes"] += allocated
                g["logical_bytes"] += st.st_size
                g["files"] += 1
                g["recent_24h"] += st.st_mtime > now - 86400
                g["oldest_mtime"] = min(g["oldest_mtime"], st.st_mtime)
                g["newest_mtime"] = max(g["newest_mtime"], st.st_mtime)
                birth = getattr(st, "st_birthtime", None)
                if birth is not None:
                    g["oldest_birthtime"] = min(g["oldest_birthtime"] or birth, birth)
                extensions[p.suffix.lower() or "(none)"] += allocated
            except OSError as exc:
                error(exc)
    return {"schema": "deep-inventory/1", "root": str(root),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "groups": sorted(groups.values(), key=lambda g: -g["bytes"]),
            "extensions": dict(sorted(extensions.items(), key=lambda x: -x[1])),
            "errors": errors, "note": "元信息不是删除依据；APFS 共享块不能据此相加为可释放空间"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = census(args.root)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"groups": len(result["groups"]), "errors": len(result["errors"]), "out": args.out}))
