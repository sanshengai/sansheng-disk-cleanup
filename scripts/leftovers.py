#!/usr/bin/env python3
"""已卸载应用遗留的低风险缓存：只读探针（不删除、不联网、不用 sudo）。

依据：清理开源拆解附录 C §2.6–2.8、§5.3、§7 第 3–5/11 条。四条底线都在代码里：
  1. 只看 Caches / Logs / Saved Application State / HTTPStorages / WebKit 下的第一层目录；
     Preferences / Application Support / Containers / Group Containers 不评估（见 NOT_EVALUATED）。
  2. 已装清单不完整（installed_apps()["complete"] 为 False）整个探针拒绝，不输出候选。
  3. 只认 bundle id 形式的名字，用 scan.reverse_lookup 判断（精确 / 点分血缘 / group / 容器元数据 /
     共享组件），不做名称包含匹配。
  4. 任何查询失败（反查异常、stat 失败、Spotlight 不可用、遍历超限）都往“应用还在”方向：跳过不列。
  另有：目录内最新 mtime 距今 >= min_age_days；Spotlight 找到同 bundle id 的 .app 视为还在；
  brew_zap=True 时，已装 cask 的 zap 路径若覆盖候选，视为还在（该 cask 仍安装）。
"""
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protect  # noqa: E402
import scan  # noqa: E402

# (Library 下的目录, 名字后缀)
LOW_RISK_LOCATIONS = (("Caches", ""), ("Logs", ""), ("Saved Application State", ".savedState"),
                      ("HTTPStorages", ""), ("WebKit", ""))
HIGHER_RISK_LOCATIONS = ("HTTPStorages", "WebKit")
NOT_EVALUATED = (
    ("Preferences", "含用户设置，可能是用户唯一的配置备份，只列不动"),
    ("Application Support", "含用户数据与登录态，误删不可恢复，只列不动"),
    ("Containers", "沙盒容器含用户文档，归属需读容器元数据，风险高，不自动评估"),
    ("Group Containers", "多个应用共享的数据，team id/group 前缀归属不可靠，不自动评估"),
)
STRICT_BID = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2,}$")   # 至少两个点，仅字母数字点横线下划线
DEFAULT_MIN_AGE_DAYS = 30
WALK_MAX_ENTRIES = 20000
WALK_MAX_SECONDS = 5.0
MDFIND_TIMEOUT = 10
BREW_TIMEOUT = 60
SPOTLIGHT_SANITY_BID = "com.apple.finder"   # 任何正常的 Spotlight 索引都该找到它


def _is_strict_bundle_id(name):
    return bool(STRICT_BID.match(name))


def _walk_stats(top, deadline_s=WALK_MAX_SECONDS, max_entries=WALK_MAX_ENTRIES):
    """只读 lstat 遍历（不跟随符号链接）。返回 (最新 mtime, 字节数 st_blocks*512, 条目数)；
    超过条目上限或时间上限、或任何 stat/listdir 失败抛 OSError/TimeoutError（调用方据此跳过）。"""
    t0 = time.monotonic()
    newest = 0.0
    total = 0
    count = 0
    seen = set()
    stack = [top]
    while stack:
        d = stack.pop()
        st = os.lstat(d)
        count += 1
        newest = max(newest, st.st_mtime)
        if (st.st_dev, st.st_ino) not in seen:
            seen.add((st.st_dev, st.st_ino))
            total += st.st_blocks * 512
        with os.scandir(d) as it:
            for e in it:
                count += 1
                if count > max_entries:
                    raise TimeoutError("entries>%d" % max_entries)
                if time.monotonic() - t0 > deadline_s:
                    raise TimeoutError("time>%ss" % deadline_s)
                est = e.stat(follow_symlinks=False)
                newest = max(newest, est.st_mtime)
                key = (est.st_dev, est.st_ino)
                if key not in seen:
                    seen.add(key)
                    total += est.st_blocks * 512
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
    return newest, total, count


def _spotlight_apps(bundle_id):
    """mdfind 查 bundle id，返回路径列表；任何失败（超时、非零退出、命令缺失）抛异常。"""
    r = subprocess.run(["mdfind", "kMDItemCFBundleIdentifier == '%s'" % bundle_id],
                       capture_output=True, text=True, timeout=MDFIND_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError("mdfind 退出码 %s" % r.returncode)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _brew_executable():
    return shutil.which("brew") or next((p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
                                         if os.path.exists(p)), None)


def _brew_installed_casks():
    """brew info --cask --json=v2 --installed -> casks 列表；失败抛异常（调用方忽略）。只含已安装 cask。"""
    brew = _brew_executable()
    if not brew:
        raise RuntimeError("找不到 brew")
    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_ANALYTICS="1")
    r = subprocess.run([brew, "info", "--cask", "--json=v2", "--installed"], capture_output=True, text=True,
                       timeout=BREW_TIMEOUT, env=env)
    if r.returncode != 0:
        raise RuntimeError("brew 退出码 %s" % r.returncode)
    return json.loads(r.stdout).get("casks") or []


def _zap_paths(cask):
    """cask 的 zap 里 trash/delete 的路径（展开 ~）。"""
    out = []
    for art in cask.get("artifacts") or []:
        if not isinstance(art, dict):
            continue
        for block in art.get("zap") or []:
            if not isinstance(block, dict):
                continue
            for key in ("trash", "delete"):
                v = block.get(key)
                for p in ([v] if isinstance(v, str) else (v or [])):
                    if isinstance(p, str) and p:
                        out.append(protect.HOME + p[1:] if p.startswith("~") else p)
    return out


def _zap_owner(path, zap_index):
    """候选路径被某已装 cask 的 zap 覆盖（相等、通配符命中、或 zap 路径在候选目录之内）时返回 cask token。"""
    p = path.rstrip("/")
    for token, paths in zap_index:
        for z in paths:
            z = z.rstrip("/")
            if z == p or z.startswith(p + "/"):
                return token
            if any(c in z for c in "*?[") and fnmatch.fnmatchcase(p, z):
                return token
    return None


def probe_leftovers(opts=None):
    min_age = getattr(opts, "min_age_days", None)
    min_age = DEFAULT_MIN_AGE_DAYS if min_age is None else min_age
    use_brew = bool(getattr(opts, "brew_zap", False))
    now = time.time()
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "inventory_complete": False, "min_age_days": min_age, "candidates": [], "skipped": {},
           "not_evaluated_locations": [{"location": "~/Library/" + n, "reason": why} for n, why in NOT_EVALUATED],
           "errors": []}
    skipped = out["skipped"]

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    # 底线 2：清单不完整（或枚举本身出错）就拒绝
    try:
        apps = scan.installed_apps()
        complete = bool(apps.get("complete"))
    except Exception as exc:  # noqa: BLE001
        return {"refused": True, "reason": "无法枚举已安装应用（%s: %s），不输出候选" % (type(exc).__name__, exc),
                "inventory_complete": False, "not_evaluated_locations": out["not_evaluated_locations"]}
    if not complete:
        return {"refused": True,
                "reason": "已安装应用清单不完整（%d 个读取错误），无法断言某个 bundle id 未安装，不输出候选"
                          % len(apps.get("errors") or []),
                "inventory_complete": False, "not_evaluated_locations": out["not_evaluated_locations"]}
    out["inventory_complete"] = True

    spotlight_ok = None   # 惰性做一次健全性检查：Spotlight 关闭时 mdfind 会静默返回空，不能当“没找到”
    zap_index = []
    if use_brew:
        try:
            zap_index = [(c.get("token"), _zap_paths(c)) for c in _brew_installed_casks() if c.get("token")]
        except Exception as exc:  # noqa: BLE001  zap 只是佐证，失败忽略
            out["errors"].append({"stage": "brew_zap", "error": "%s: %s" % (type(exc).__name__, exc)})

    lib = protect.HOME + "/Library"
    for loc, suffix in LOW_RISK_LOCATIONS:
        base = lib + "/" + loc
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name)
        except FileNotFoundError:
            continue
        except OSError as exc:
            out["errors"].append({"path": base, "error": "listdir失败: %s" % exc})
            skip("location_unreadable")
            continue
        for e in entries:
            try:
                if suffix:
                    if not e.name.endswith(suffix) or e.name == suffix:
                        skip("skipped_non_bundle_id")
                        continue
                    name = e.name[:-len(suffix)]
                else:
                    name = e.name
                if not _is_strict_bundle_id(name):
                    skip("skipped_non_bundle_id")
                    continue
                if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                    skip("not_plain_directory")
                    continue
                path = e.path
                try:
                    facts = scan.reverse_lookup(path, apps)
                except Exception:  # noqa: BLE001  反查异常 -> 当作应用还在
                    skip("lookup_error")
                    continue
                if facts.get("shared_component"):
                    skip("shared_component")
                    continue
                if facts.get("app") is not None:
                    skip("app_installed:%s" % (facts.get("app_match") or "unknown"))
                    continue
                if facts.get("app_installed") is not False:
                    skip("lookup_inconclusive")
                    continue
                try:
                    newest, size, _ = _walk_stats(path)
                except TimeoutError:
                    skip("walk_limit_exceeded")
                    continue
                except OSError:
                    skip("stat_error")
                    continue
                age_days = (now - newest) / 86400.0
                if age_days < min_age:
                    skip("too_recent")
                    continue
                # Spotlight：装在清单根目录之外（Setapp、自定义目录、iOS Wrapper）的 .app
                if spotlight_ok is None:
                    try:
                        spotlight_ok = any(p.endswith(".app") for p in _spotlight_apps(SPOTLIGHT_SANITY_BID))
                    except Exception as exc:  # noqa: BLE001
                        spotlight_ok = False
                        out["errors"].append({"stage": "spotlight", "error": "%s: %s" % (type(exc).__name__, exc)})
                    if not spotlight_ok:
                        out["errors"].append({"stage": "spotlight", "error": "Spotlight 不可用或索引为空，全部候选按“还在”处理"})
                if not spotlight_ok:
                    skip("mdfind_failed")
                    continue
                try:
                    found = _spotlight_apps(name)
                except Exception:  # noqa: BLE001
                    skip("mdfind_failed")
                    continue
                if any(".app" in p for p in found):
                    skip("found_by_spotlight")
                    continue
                owner = _zap_owner(path, zap_index) if zap_index else None
                if owner:
                    skip("owned_by_installed_cask")
                    continue
                cand = {"path": path, "name": name, "location": loc, "bytes": size,
                        "age_days": int(age_days), "reason": "no_installed_app"}
                if loc in HIGHER_RISK_LOCATIONS:
                    # WebKit / HTTPStorages 里是登录态、IndexedDB、本地存储，不只是缓存：降为 moderate、默认不勾
                    cand["safety"] = "moderate"
                    cand["default_select"] = False
                    cand["note"] = "含登录态或本地存储（WebKit/HTTPStorages），不只是缓存；默认不勾选，需用户确认"
                out["candidates"].append(cand)
            except Exception as exc:  # noqa: BLE001  任何意外都往“还在”方向
                skip("unexpected_error")
                out["errors"].append({"path": getattr(e, "path", "?"), "error": "%s: %s" % (type(exc).__name__, exc)})
    out["candidates"].sort(key=lambda c: (-c["bytes"], c["path"]))
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="已卸载应用遗留的低风险缓存（只读）")
    ap.add_argument("--min-age-days", dest="min_age_days", type=int, default=DEFAULT_MIN_AGE_DAYS)
    ap.add_argument("--brew-zap", dest="brew_zap", action="store_true")
    print(json.dumps(probe_leftovers(ap.parse_args()), ensure_ascii=False, indent=1))
