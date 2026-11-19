"""Bounded reverse symlink checks in explicitly configured consumer trees."""
import os
import time


def contains(parent, child):
    return os.path.commonpath((parent, child)) == parent


def inventory(root, limit=200000, timeout=15):
    """Scan entries once, never follow directory symlinks."""
    pending, edges, count = [root], [], 0
    deadline = time.monotonic() + timeout
    try:
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    count += 1
                    if count > limit or time.monotonic() > deadline:
                        return edges, "扫描预算已用完"
                    if entry.is_symlink():
                        edges.append((entry.path, os.path.realpath(entry.path, strict=os.path.ALLOW_MISSING)
                                      if hasattr(os.path, "ALLOW_MISSING") else os.path.realpath(entry.path)))
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
    except OSError as exc:
        return edges, str(exc)
    return edges, None


class ConsumerChecks:
    def __init__(self, config):
        self.scopes = config.get("consumer_scopes", [])
        self.cache = {}

    def inspect(self, path, files=None):
        path = os.path.realpath(path)
        affected = None
        if files is not None:
            if not isinstance(files, list) or not files:
                return {"status": "unknown", "configured": True, "reason": "待删文件清单为空或格式无效"}
            affected = set()
            for name in files:
                if (not isinstance(name, str) or not os.path.isabs(name) or not contains(path, os.path.realpath(os.path.dirname(name)))
                        or (os.path.isdir(name) and not os.path.islink(name))):
                    return {"status": "unknown", "configured": True, "reason": "待删清单含目录或越界路径"}
                # An external link to any ancestor directory consumes this leaf.
                node = os.path.realpath(name)
                while node not in affected:
                    affected.add(node)
                    parent = os.path.dirname(node)
                    if parent == node:
                        break
                    node = parent
        roots = set()
        matched = False
        for scope in self.scopes:
            source = os.path.realpath(os.path.expanduser(scope["source"]))
            if contains(source, path) or contains(path, source):
                matched = True
                roots.update(os.path.realpath(os.path.expanduser(r)) for r in scope["roots"])
        if not roots:
            return {"status": "unknown", "configured": matched, "reason": "未配置此目录的消费者搜索范围"}
        errors, links, targets = [], set(), set()
        for root in sorted(roots):
            if root not in self.cache:
                self.cache[root] = inventory(root)
            edges, error = self.cache[root]
            if error:
                errors.append(error)
            for link, target in edges:
                if affected is None and contains(path, link):
                    continue
                matched_target = target in affected if affected is not None else (contains(path, target) or contains(target, path))
                if matched_target:
                    links.add(link)
                    targets.add(target)
        return {"status": "referenced" if links else "unknown" if errors else "none_in_scope",
                "configured": True, "complete": not errors, "scope": sorted(roots), "errors": errors,
                "links": len(links), "distinct_targets": len(targets), "examples": sorted(links)[:3]}

    def permits(self, row, files=None):
        report = self.inspect(row["abs"], files)
        row["reverse_symlinks"] = report
        if report["status"] == "referenced":
            row["local_action"] = "保留本机，先迁移软链接消费者"
        elif report["configured"] and report["status"] == "unknown":
            row["local_action"] = "保留本机，先补齐消费者检查"
        return "local_action" not in row
