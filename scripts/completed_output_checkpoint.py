"""Completed producer receipts plus explicit exact retirement; not a parent-project done flag."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

import run_authorization as authorization
from artifact_checkpoint import git

require = authorization.require


def pinned(ref):
    require(isinstance(ref, dict), "missing pinned evidence")
    p = authorization.ordinary(ref["path"])
    require(authorization.snapshot(p)["sha256"] == ref.get("sha256"), "evidence drift: " + str(p))
    return p


def document(ref):
    return json.loads(pinned(ref).read_text())


def commit_retained(repo, commit, ref):
    require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit), "missing full producer commit")
    require(isinstance(ref, str) and ref.startswith(("refs/heads/", "refs/remotes/")), "missing retained ref")
    git(repo, "merge-base", "--is-ancestor", commit, ref)


def validate(item):
    proof = item["checkpoint"]
    require(proof.get("kind") == "completed_output_stage", "unsupported checkpoint")
    root, scope, selected = authorization.selection(item)
    root = Path(root)
    require(scope == "files", "completed outputs require exact files")
    authorization.validate(item, retirement=True)
    repo = Path(proof["owner_repo"])
    require(repo.is_absolute() and repo.resolve() == repo and repo in root.parents,
            "output must be inside its owner repository")
    require(git(repo, "rev-parse", "--show-toplevel") == str(repo), "owner is not Git root")
    for flag in ("--git-dir", "--git-common-dir"):
        admin = Path(git(repo, "rev-parse", "--path-format=absolute", flag)).resolve()
        require(root != admin and admin not in root.parents, "Git administration is not output")
    # Never promote a tracked file or a source directory merely because it has a receipt.
    tracked = set(git(repo, "ls-files", "-z", "--", str(root)).split("\0"))
    require(not any(str(Path(p).relative_to(repo)) in tracked for p in selected), "tracked output")
    ignored = set(git(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--", str(root)).split("\0"))
    require(all(str(Path(p).relative_to(repo)) in ignored for p in selected), "output must be Git ignored")
    consumers = proof.get("consumer_files")
    require(isinstance(consumers, list) and consumers, "missing reviewed consumer code")
    for entry in consumers:
        p = pinned(entry)
        require(repo in p.parents and p.suffix in (".py", ".ps1", ".mjs", ".sh"), "invalid consumer source")
        require(git(repo, "ls-files", "--", str(p)), "consumer source must be tracked")
    retained_paths = dependencies(item)
    require(not set(selected).intersection(retained_paths), "selected retained evidence")
    evidence_identity = {p: authorization.fingerprint(authorization.ordinary(p).stat()) for p in retained_paths}
    if proof.get("variant") == "web_assembly":
        web = Path(proof["web_root"])
        require(web.resolve() == web and repo in web.parents, "invalid web root")
        require(root in (web / "dist", web / ".i18n-build"), "not a supported assembly output")
        require(proof["manifest"]["path"] == str(web / ".i18n-build-manifest.json"), "wrong manifest location")
        require(proof["receipt"]["path"] == str(web / "dist/i18n-build-receipt.json"), "wrong receipt location")
        manifest, receipt = document(proof["manifest"]), document(proof["receipt"])
        require(manifest.get("schema_version") == "i18n-build-manifest-v1", "unsupported manifest")
        require(receipt.get("schema_version") == "i18n-build-receipt-v2", "unsupported receipt")
        require(receipt.get("source_commit") == manifest.get("source_commit"), "source commit mismatch")
        commit_retained(repo, manifest.get("source_commit"), proof.get("retain_ref"))
        files = manifest.get("files")
        require(isinstance(files, dict) and files, "empty manifest")
        for name, digest in files.items():
            rel = PurePosixPath(name)
            require(not rel.is_absolute() and ".." not in rel.parts and str(rel) == name,
                    "invalid manifest path")
            require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest), "invalid manifest hash")
        assembly = {"files": len(files), "sha256": hashlib.sha256(
            "\n".join(f"{name}\0{digest}" for name, digest in files.items()).encode()).hexdigest()}
        require(manifest.get("assembly") == assembly == receipt.get("assembly"), "assembly digest mismatch")
        for p in selected:
            rel = Path(p).relative_to(root)
            if root.name == ".i18n-build":
                require(rel.parts[0] == "zh-Hans" and len(rel.parts) > 1, "unsupported stage locale")
                rel = Path(*rel.parts[1:])
            require(rel.name not in ("i18n-build-receipt.json", "i18n-effective-availability.json"), "retained build metadata")
            require(authorization.snapshot(p)["sha256"] == files.get(rel.as_posix()), "output does not match completed assembly: " + p)
    elif proof.get("variant") == "apk_build":
        packages, retained = proof.get("packages"), proof.get("retained_apks")
        require(isinstance(packages, list) and packages and isinstance(retained, list) and retained, "missing selected/retained APKs")
        require(sorted(e["path"] for e in packages) == selected, "package selection mismatch")
        require(len({e["path"] for e in retained}) == len(retained), "duplicate retained APK")
        require(not set(selected).intersection(e["path"] for e in retained), "retained APK selected")
        seen_commits = set()
        for entry in packages + retained:
            p = authorization.ordinary(entry["path"])
            require(root in p.parents and p.suffix == ".apk", "APK outside owner output")
            require(entry["receipt"]["path"] == str(p.parent / "version.json"), "APK receipt must be adjacent")
            receipt = document(entry["receipt"])
            match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(receipt.get("version")))
            require(match and int(receipt.get("versionCode", -1)) == sum(int(n) * m for n, m in zip(match.groups(), (10000, 100, 1))), "invalid APK version")
            require(receipt["version"] in p.name and isinstance(receipt.get("appId"), str) and receipt["appId"], "APK identity missing")
            # Early producer receipts predate this optional debug-mode annotation;
            # version, source commit, APK size and SHA remain mandatory proof.
            require("inspectable" not in receipt or isinstance(receipt["inspectable"], bool), "invalid APK build mode")
            snap = authorization.snapshot(p)
            require(snap["sha256"] == receipt.get("apkSha256") and snap["size"] == receipt.get("sizeBytes"), "APK differs from build receipt")
            commit = receipt.get("builtFrom")
            if commit not in seen_commits:
                commit_retained(repo, commit, proof.get("retain_ref"))
                seen_commits.add(commit)
    else:
        raise ValueError("unsupported completed output variant")
    for p, identity in evidence_identity.items():
        require(authorization.fingerprint(authorization.ordinary(p).stat()) == identity,
                "retained evidence changed during verification: " + p)
    return True


def verify(item):
    try:
        validate(item)
        return True, "producer-completed exact outputs explicitly retired; retained evidence verified"
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def dependencies(item):
    """Total for malformed input; exact files avoid protecting the stage's whole owner repo."""
    if not isinstance(item, dict) or not isinstance(item.get("checkpoint"), dict):
        return []
    proof = item["checkpoint"]
    refs = [proof.get("manifest"), proof.get("receipt")]
    consumers = proof.get("consumer_files")
    if isinstance(consumers, list):
        refs.extend(consumers)
    paths = authorization.dependencies(item)
    for key in ("packages", "retained_apks"):
        entries = proof.get(key)
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict):
                refs.append(entry.get("receipt"))
                if key == "retained_apks" and isinstance(entry.get("path"), str):
                    paths.append(entry["path"])
    paths.extend(r["path"] for r in refs if isinstance(r, dict) and isinstance(r.get("path"), str))
    return sorted(set(paths))
