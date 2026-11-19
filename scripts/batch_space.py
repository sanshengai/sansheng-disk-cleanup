"""Sample the target filesystem around a cleanup batch; retain signed observations."""
import os
import plistlib
import subprocess


def sample(path):
    row = {"path": path, "device": None, "available_bytes": None,
           "container": None, "container_free_bytes": None}
    try:
        row["device"] = os.stat(path).st_dev
        stat = os.statvfs(path)
        row["available_bytes"] = stat.f_bavail * stat.f_frsize
        # On macOS a firmlinked home can sit on the Data volume even when its
        # lexical mount root appears to be /. df returns the actual device.
        device = subprocess.run(["/bin/df", "-kP", path], capture_output=True,
                                text=True, check=True, timeout=5).stdout.splitlines()[-1].split()[0]
        raw = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", device], capture_output=True,
                             check=True, timeout=5).stdout
        info = plistlib.loads(raw)
        row.update(container=info.get("APFSContainerReference"), container_free_bytes=info.get("APFSContainerFree"))
        if row["container"] is None or row["container_free_bytes"] is None:
            row["reason"] = "未取得 APFS 容器余量"
    except (OSError, ValueError, IndexError, subprocess.SubprocessError, plistlib.InvalidFileException) as e:
        row["reason"] = str(e)[:240]
    return row


def changes(before, after):
    result = {"before": before, "after": after, "volume_delta_bytes": None,
              "container_delta_bytes": None, "attributable_release_bytes": None}
    for identity, field, out in (("device", "available_bytes", "volume_delta_bytes"),
                                 ("container", "container_free_bytes", "container_delta_bytes")):
        if before[identity] is not None and before[identity] == after[identity] and all(
            isinstance(s[field], int) for s in (before, after)
        ):
            result[out] = after[field] - before[field]
    return result
