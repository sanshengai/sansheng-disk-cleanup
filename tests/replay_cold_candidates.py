"""Replay two committed cold_store CLIs on one metadata-only temporary tree.

Input JSON contains home, work_root, rows (cold candidate export), labels
(original absolute path -> eligible/unsuitable/other_route), and avds (Android probe rows).
Both versions receive identical logical file sizes, ages, AVDs and roots.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


def materialize(base, dataset):
    def mapped(path):
        parts = Path(path).parts
        if not parts or parts[0] != "/" or ".." in parts:
            raise ValueError(f"Invalid fixture path {path!r}")
        return base.joinpath(*parts[1:])

    home = mapped(dataset["home"])
    work = mapped(dataset["work_root"])
    triage = {}
    reverse = {}
    for row in dataset["rows"]:
        folder = mapped(row["abs"])
        folder.mkdir(parents=True, exist_ok=True)
        leaf = folder / "sample.bin"
        with leaf.open("xb") as stream:
            stream.truncate(row["bytes"])
        modified = time.time() - row["idle_days"] * 86400
        os.utime(leaf, (modified, modified))
        reverse[str(folder)] = row["abs"]
        triage.setdefault(row["source"], []).append({
            "path": str(folder), "bytes": row["bytes"], "red_line": row.get("guard"),
            "open": row.get("open")})
    for avd in dataset["avds"]:
        ini = mapped(avd["path"]) / "config.ini"
        ini.parent.mkdir(parents=True, exist_ok=True)
        ini.write_text("\n".join(f"image.sysdir.{n}={s}" for n, s in enumerate(avd["image_sysdirs"], 1)))
    (base / "triage.json").write_text(json.dumps(triage))
    config = base / "config.json"
    scopes = [{"source": str(mapped(s["source"])), "roots": [str(mapped(r)) for r in s["roots"]]}
              for s in dataset.get("consumer_scopes", [])]
    for edge in dataset.get("links", []):
        link = mapped(edge["path"])
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(mapped(edge["target"]) / "sample.bin")
    storage = {"consumer_scopes": scopes}
    if dataset.get("backup_batches"):
        ledger = home / ".sansheng/netdisk/cold-ledger.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        batches = {}
        for batch in dataset["backup_batches"]:
            local = mapped(batch["local"])
            marker = local / dataset["placeholder"]
            marker.write_text(f"<!-- netdisk-cold batch={batch['id']} remote_root={batch['remote_root']} -->")
            leaf = local / "sample.bin"
            if leaf.exists():
                # The original aggregate already included its marker.
                with leaf.open("r+b") as f:
                    f.truncate(leaf.stat().st_size - marker.stat().st_size)
                stamp = next(row["idle_days"] for row in dataset["rows"] if row["abs"] == batch["local"])
                mtime = time.time() - stamp * 86400
                os.utime(leaf, (mtime, mtime))
                os.utime(marker, (mtime, mtime))
            batches[batch["id"]] = dict(batch, local=str(local))
        ledger.write_text(json.dumps({"schema": "netdisk-cold/1", "batches": batches}))
        storage["placeholder"] = dataset["placeholder"]
    for bundle in dataset.get("session_bundles", []):
        folder = mapped(bundle["path"])
        folder.with_suffix(".jsonl").touch()
        for child in bundle["subdirs"]:
            if child not in ("subagents", "workflows", "tool-results"):
                raise ValueError("Unknown session component")
            (folder / child).mkdir(exist_ok=True)
    config.write_text(json.dumps({"work_root": str(work), "cold_storage": storage}))
    return reverse, dict(os.environ, HOME=str(home), SANSHENG_DISK_CLEANUP_TRIAGE=str(config),
                         PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")


def snapshot(repo, ref, dest):
    sha = subprocess.check_output(["git", "-C", repo, "rev-parse", "--verify", f"{ref}^{{commit}}"], text=True).strip()
    paths = subprocess.check_output(["git", "-C", repo, "ls-tree", "-r", "--name-only", sha, "scripts"], text=True)
    for name in paths.splitlines():
        if Path(name).suffix not in {".py", ".json"}:
            continue
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(["git", "-C", repo, "show", f"{sha}:{name}"]))
    return sha


# The production protection module intentionally uses pwd, ignoring HOME.
# Redirect just that test dependency before importing either actual CLI.
ENTRY = """
import os, pwd, runpy, sys
from types import SimpleNamespace
pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir=os.environ["HOME"])
sys.path.insert(0, os.path.dirname(sys.argv[1]))
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def run(args):
    raw = args.fixture.read_bytes()
    dataset = json.loads(raw)
    result = {"fixture_sha256": hashlib.sha256(raw).hexdigest(), "versions": {}}
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch).resolve()
        reverse, env = materialize(root, dataset)
        for name, ref in (("before", args.before), ("after", args.after)):
            sha = snapshot(args.repo, ref, root / name)
            result["versions"][name] = {"sha": sha, "seconds": []}
        for trial in range(args.repeats):
            order = ("before", "after") if trial % 2 == 0 else ("after", "before")
            for name in order:
                summary = result["versions"][name]
                output = root / f"{name}-{trial}.json"
                cmd = [sys.executable, "-I", "-B", "-c", ENTRY,
                       str(root / name / "scripts/cold_store.py"), "candidates",
                       "--from-triage", str(root / "triage.json"), "--min-idle-days", "7",
                       "--min-gib", "1", "--json", str(output)]
                start = time.perf_counter()
                subprocess.run(cmd, env=env, cwd=root, check=True, stdout=subprocess.DEVNULL)
                summary["seconds"].append(round(time.perf_counter() - start, 6))
                rows = json.loads(output.read_text())
                found = sorted(reverse[row["abs"]] for row in rows)
                if trial and found != summary["paths"]:
                    raise RuntimeError("Candidate sets changed between repeats")
                summary["paths"] = found
                summary["logical_bytes"] = sum(row["bytes"] for row in rows)
        labels = dataset["labels"]
        if (not set(labels).issubset(reverse.values()) or not labels
                or not set(labels.values()) <= {"eligible", "unsuitable", "other_route"}):
            raise ValueError("Labels must refer to fixture rows and use a known verdict")
        for summary in result["versions"].values():
            found = set(summary["paths"])
            summary["median_seconds"] = statistics.median(summary["seconds"])
            for label in ("eligible", "unsuitable", "other_route"):
                expected = {p for p, v in labels.items() if v == label}
                summary[label] = {"total": len(expected), "returned": len(found & expected)}
            summary["unlabelled_returned"] = len(found - labels.keys())
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    print(json.dumps(run(args), ensure_ascii=False, indent=2))
