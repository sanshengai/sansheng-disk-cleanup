"""Exact retired test-fixture copies; no general permission to clean temp dirs.

The receipt is the retained original Vitest output, not a new completion claim.
Location exceptions require a run-local retirement authorization and the same
proof, selected leaves and independent retained bytes at check and execution.
"""
import hashlib
import json
import os
from pathlib import Path
import re

import artifact_checkpoint as ac
import protect
import run_authorization as auth

KIND = "retired_test_copy"
SCHEMA = "retired-test-copy/1"
TEMP_REASON = "临时目录里不属于 Claude 会话的内容"


def is_item(item):
    return (isinstance(item, dict) and isinstance(item.get("checkpoint"), dict)
            and item["checkpoint"].get("kind") == KIND)


def temp_bases():
    # TMPDIR is caller-controlled. macOS's canonical /private/tmp is supplied
    # by the existing protection module; do not discover roots from env vars.
    return {Path(protect.TMP_BASE).resolve()}


def path(value, directory=False):
    if not isinstance(value, str) or not value:
        raise ValueError("test-copy: absolute path required")
    p = Path(value)
    if not p.is_absolute() or p.resolve() != p:
        raise ValueError("test-copy: linked/noncanonical path")
    st = p.lstat()
    if st.st_uid != os.getuid():
        raise ValueError("test-copy: file belongs to another user")
    if directory:
        if not p.is_dir():
            raise ValueError("test-copy: directory required")
    else:
        ac.ordinary_path(str(p))
    return p


def checked(ref, identities):
    p = path(ref["path"])
    before = ac.fingerprint(p)
    data = p.read_bytes()
    if (not re.fullmatch(r"[0-9a-f]{64}", ref.get("sha256", ""))
            or hashlib.sha256(data).hexdigest() != ref["sha256"]
            or ac.fingerprint(p) != before):
        raise ValueError("test-copy: evidence content/identity changed: " + str(p))
    identities[p] = before
    return data


class Scope:
    def __init__(self, root, item_root, pairs, identities, profile):
        self.root, self.item_root = root, item_root
        self.pairs, self.identities, self.profile = pairs, identities, profile

    def permits(self, value):
        p = Path(value)
        return (p == self.item_root or str(p) in self.pairs
                or (self.item_root in p.parents and any(p in Path(s).parents for s in self.pairs)))

    def reason(self, value):
        # Only the single temporary-location rule is exempted. User protects,
        # .git, sessions, system rules and every other reason remain effective.
        if not self.permits(value):
            return "test-copy: outside the exact selected scope"
        return next((why for candidate in protect._candidates(str(value))
                     for _, why in protect._reasons(candidate, self.profile)
                     if why != TEMP_REASON), None)

    def verify_leaf(self, value):
        for p, identity in self.identities.items():
            if ac.fingerprint(p) != identity:
                raise ValueError("test-copy: evidence changed before deletion")
        row = self.pairs[str(value)]
        source, retained = path(str(value)), path(row["retained_copy"])
        why = self.reason(str(source))
        if why:
            raise ValueError(why)
        if os.path.samefile(source, retained):
            raise ValueError("test-copy: retained copy is not independent")
        for p in (source, retained):
            before = ac.fingerprint(p)
            if ac.sha256_file(p) != row["sha256"] or ac.fingerprint(p) != before:
                raise ValueError("test-copy: selected/retained content drift")


def load_scope(item, profile, run=None):
    if not is_item(item):
        return None
    if item.get("method") != "delete" or item.get("scope") != "files" or item.get("keep"):
        raise ValueError("test-copy: explicit files only")
    identities = {}
    ref = item["checkpoint"]
    document = json.loads(checked(ref, identities))
    if document.get("schema") != SCHEMA or document.get("role") != "test_copy":
        raise ValueError("test-copy: invalid proof schema/role")
    if document.get("status") != "retired" or not str(document.get("retirement_reason", "")).strip():
        raise ValueError("test-copy: explicit phase retirement required")
    root, item_root = path(document["task_root"], True), path(item["path"], True)
    if (root.parent not in temp_bases() or root == Path(protect.claude_tmp_root())
            or root not in item_root.parents):
        raise ValueError("test-copy: one owned task directory below system temp required")
    selected = set(auth.selection(item)[2])
    doc, _ = auth.read_document(item, run)
    # A user's general cleanup request alone cannot retire a live phase.
    auth.validate(item, run or doc["run"], retirement=True)
    ledger = path(item["run_authorization"]["path"])
    identities[ledger] = ac.fingerprint(ledger)
    evidence = document.get("retained_evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("test-copy: original source/log evidence required")
    retained_data = {}
    for entry in evidence:
        p = path(entry["path"])
        if root == p or root in p.parents:
            raise ValueError("test-copy: original evidence must be durably retained outside task temp")
        retained_data[str(p)] = checked(entry, identities)
    if not any(Path(p).suffix in (".py", ".js", ".ts", ".mjs", ".tsx") for p in retained_data):
        raise ValueError("test-copy: retained test/source code missing")
    receipt = document["test_receipt"]
    raw = checked(receipt, identities).decode("utf-8")
    if receipt["path"] not in retained_data or receipt.get("format") != "vitest":
        raise ValueError("test-copy: original retained Vitest receipt required")
    # Match the actual RUN cwd and full passing test counts, not a user-written
    # exit-code stub or an arbitrary occurrence of 'passed'.
    cwd = re.findall(r"^\s*RUN\s+v\S+\s+(.+?)\s*$", raw, re.M)
    totals = re.findall(r"^\s*Tests\s+(\d+) passed\s+\((\d+)\)\s*$", raw, re.M)
    if (len(cwd) != 1 or not (Path(cwd[0]) == root or root in Path(cwd[0]).parents)
            or len(totals) != 1 or int(totals[0][0]) < 1 or totals[0][0] != totals[0][1]
            or re.search(r"\b(?:failed|FAIL|ERROR)\b", raw)):
        raise ValueError("test-copy: receipt does not prove this test stage passed")
    rows = document.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("test-copy: empty proof inventory")
    pairs = {}
    for row in rows:
        source, retained = str(path(row["path"])), path(row["retained_copy"])
        if source in pairs or source not in selected or item_root not in Path(source).parents:
            raise ValueError("test-copy: duplicate/cross-root/unselected source")
        if (root == retained or root in retained.parents or str(retained) in selected
                or not any(protect._within(str(retained), protect.real(d))
                           for d in profile.get("deliverable_roots", []))):
            raise ValueError("test-copy: retained leaf outside protected deliverable collection")
        if not re.fullmatch(r"[0-9a-f]{64}", row.get("sha256", "")):
            raise ValueError("test-copy: full SHA256 required")
        pairs[source] = row
    if set(pairs) != set(selected):
        raise ValueError("test-copy: selection must exactly equal frozen proof")
    if any(str(p) in selected for p in identities):
        raise ValueError("test-copy: selected its own evidence")
    scope = Scope(root, item_root, pairs, identities, profile)
    for source in pairs:
        scope.verify_leaf(source)
    return scope


def verify(item):
    try:
        load_scope(item, protect.load_profile())
        return True, "retired test copies and retained source/log evidence verified"
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        return False, str(exc)


def dependencies(item):
    ref = item.get("checkpoint") or {}
    result = [ref.get("path")]
    try:
        data = json.loads(Path(ref["path"]).read_text())
        result += [r.get("retained_copy") for r in data.get("files", [])]
        result += [r.get("path") for r in data.get("retained_evidence", [])]
        result.append(data.get("test_receipt", {}).get("path"))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return [p for p in result if isinstance(p, str) and p]
