"""A user's exact, run-local retirement/recency authorization, never a profile override."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
from datetime import datetime, timezone

SCHEMA = "cleanup-selection-authorization/1"


def require(ok, why):
    if not ok:
        raise ValueError(why)


def ordinary(value):
    p = Path(value)
    require(p.is_absolute() and p.resolve() == p, "authorization: absolute unlinked path required")
    st = p.lstat()
    require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1, "authorization: ordinary unlinked file required")
    return p


def fingerprint(st):
    return {k: getattr(st, "st_" + k) for k in ("dev", "ino", "size", "mtime_ns", "ctime_ns")}


def snapshot(value):
    p = ordinary(value)
    before = fingerprint(p.stat())
    h = hashlib.sha256()
    with p.open("rb") as f:
        require(fingerprint(os.fstat(f.fileno())) == before, "authorization: changed before read")
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
        require(fingerprint(os.fstat(f.fileno())) == before, "authorization: changed during read")
    require(fingerprint(p.stat()) == before, "authorization: changed after read")
    return {"path": str(p), **before, "sha256": h.hexdigest()}


def selection(item):
    require(isinstance(item, dict) and item.get("method") == "delete", "authorization: delete only")
    require(not item.get("keep"), "authorization: explicit selection only; no keep patterns")
    root = Path(item["path"])
    require(root.is_absolute() and root.resolve() == root, "authorization: invalid item root")
    scope = item.get("scope")
    if scope == "all":
        ordinary(str(root))
        paths = [str(root)]
    else:
        require(scope == "files", "authorization: files or single-file all only")
        require(root.is_dir(), "authorization: files root must be a directory")
        rows = item.get("files")
        require(isinstance(rows, list) and rows, "authorization: empty selection")
        paths = [r.get("path") if isinstance(r, dict) else r for r in rows]
        require(all(isinstance(p, str) for p in paths), "authorization: invalid file selection")
        require(len(set(paths)) == len(paths), "authorization: duplicate selection")
        for p in paths:
            q = Path(p)
            require(q.is_absolute() and root in q.parents and q.resolve() == q,
                    "authorization: selection outside root or linked")
    return str(root), scope, sorted(paths)


def binding(item):
    root, scope, paths = selection(item)
    data = json.dumps([root, scope, paths], ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def read_document(item, run=None, scan_id=None):
    ref = item.get("run_authorization")
    require(isinstance(ref, dict), "authorization: missing run_authorization")
    p = ordinary(ref["path"])
    raw = p.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == ref.get("sha256"), "authorization: document drift")
    doc = json.loads(raw)
    require(doc.get("schema") == SCHEMA, "authorization: unsupported schema")
    require(doc.get("run") == str(p.parent) and p.name == "selection-authorization.json",
            "authorization: document must live in its bound run")
    require(isinstance(doc.get("scan_id"), str) and doc["scan_id"], "authorization: missing scan id")
    if run is not None:
        require(str(Path(run).resolve()) == doc["run"], "authorization: different run")
        facts = json.loads((Path(run) / "facts.json").read_text())
        require(facts.get("scan_id") == doc["scan_id"], "authorization: different scan")
    if scan_id is not None:
        require(scan_id == doc["scan_id"], "authorization: different scan")
    require(isinstance(doc.get("user_text"), str) and doc["user_text"].strip(),
            "authorization: user instruction required")
    entries = doc.get("items")
    require(isinstance(entries, dict), "authorization: missing exact items")
    row = entries.get(binding(item))
    require(isinstance(row, dict), "authorization: selection not authorized")
    require([r["path"] for r in row["files"]] == selection(item)[2], "authorization: selection drift")
    return doc, row


def validate(item, run=None, scan_id=None, *, retirement=False, recent=False):
    doc, row = read_document(item, run, scan_id)
    if retirement:
        require(doc.get("retire_completed_outputs") is True, "authorization: retirement not authorized")
    if recent:
        require(doc.get("allow_recent") is True, "authorization: recent files not authorized")
    for expected in row["files"]:
        require(snapshot(expected["path"]) == expected, "authorization: file identity/content drift: " + expected["path"])
    return doc


def verify(item, run=None, scan_id=None, **flags):
    try:
        validate(item, run, scan_id, **flags)
        return True, "exact run-local authorization verified"
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return False, str(exc)


def dependencies(item):
    ref = item.get("run_authorization") if isinstance(item, dict) else None
    return [ref["path"]] if isinstance(ref, dict) and isinstance(ref.get("path"), str) else []


def create(run, items, user_text, *, allow_recent=False, retire_completed_outputs=False, audit=True):
    run = Path(run).resolve()
    facts = json.loads((run / "facts.json").read_text())
    require(isinstance(facts.get("scan_id"), str) and facts["scan_id"], "facts.scan_id required")
    require(isinstance(items, list) and items, "nonempty items required")
    require(isinstance(user_text, str) and user_text.strip(), "actual user instruction required")
    doc = {"schema": SCHEMA, "run": str(run), "scan_id": facts["scan_id"],
           "created_at": datetime.now(timezone.utc).isoformat(), "user_text": user_text,
           "allow_recent": bool(allow_recent), "retire_completed_outputs": bool(retire_completed_outputs), "items": {}}
    for item in items:
        paths = selection(item)[2]
        if audit:
            from compact_review import readable
            for p in paths:
                readable(Path(p), run)
        key = binding(item)
        require(key not in doc["items"], "duplicate item")
        doc["items"][key] = {"files": [snapshot(p) for p in paths]}
    target = run / "selection-authorization.json"
    with target.open("x") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return {"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--items", required=True, help="JSON array of exact delete items")
    parser.add_argument("--user-text", required=True, help="actual user authorization, not an inferred done flag")
    parser.add_argument("--allow-recent", action="store_true")
    parser.add_argument("--retire-completed-outputs", action="store_true")
    args = parser.parse_args()
    ref = create(args.run, json.loads(Path(args.items).read_text()), args.user_text,
                 allow_recent=args.allow_recent, retire_completed_outputs=args.retire_completed_outputs)
    print(json.dumps({"run_authorization": ref}, ensure_ascii=False))


if __name__ == "__main__":
    main()
