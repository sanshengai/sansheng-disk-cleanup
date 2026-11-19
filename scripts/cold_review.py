"""Evidence for routing a cold candidate into a more specific review."""
import json
import os
from pathlib import Path
import re
from uuid import UUID


class ReviewRoutes:
    def __init__(self, config, home):
        self.home = Path(home)
        self.marker = config.get("placeholder")
        self.ledger = {}
        if not self.marker or self.marker != os.path.basename(self.marker):
            return
        default = self.home / ".sansheng/netdisk/cold-ledger.json"
        try:
            ledger = json.loads(Path(config.get("ledger", default)).expanduser().read_text())
            if isinstance(ledger, dict) and ledger.get("schema") == "netdisk-cold/1" and isinstance(ledger.get("batches"), dict):
                self.ledger = ledger["batches"]
        except (OSError, ValueError):
            pass

    def evidence(self, candidate):
        path = Path(candidate).resolve()
        if self.ledger:
            for root in (path, *path.parents):
                marker = root / self.marker
                if not marker.is_file() or marker.is_symlink():
                    continue
                try:
                    with marker.open(encoding="utf-8") as f:
                        comment = re.search(r"<!-- netdisk-cold batch=(\S+) remote_root=(.*?) -->", f.read(16384))
                except (OSError, UnicodeError):
                    continue
                if comment:
                    bid, remote = comment.groups()
                    batch = self.ledger.get(bid, {})
                    if (isinstance(batch, dict) and batch.get("id") == bid and batch.get("status") in {"partial", "offloaded"}
                            and batch.get("remote_root") == remote and batch.get("local") == str(root)
                            and batch.get("offloaded") and re.fullmatch("[0-9a-fA-F]{64}", str(batch.get("manifest_sha256")))):
                        return {"route": "verify_existing_backup", "batch": bid, "marker": str(marker),
                                "action": "先核对原批次逐文件覆盖、哈希和本机消费者，再评估本地副本"}
        projects = self.home / ".claude/projects"
        if path.parent.parent != projects:
            return None
        try:
            if str(UUID(path.name)) != path.name:
                return None
        except ValueError:
            return None
        transcript = path.with_suffix(".jsonl")
        if (transcript.is_file() and not transcript.is_symlink()
                and any((path / n).is_dir() for n in ("subagents", "workflows", "tool-results"))):
            return {"route": "session_review", "transcript": str(transcript),
                    "action": "保留本机，连同主会话复核附属记录的用途和保留设置"}
        return None
