#!/usr/bin/env python3
"""空间巡检：一秒读完磁盘余量，和一周前比，余量过低或涨得太快才提醒。只读，不删任何东西。

  python3 scripts/disk_watch.py [--notify] [--triage] [--out ~/.sansheng/disk-cleanup/watch]

本 Skill 不安装定时任务；想每天看一次，把这条命令挂到你已有的定时任务里（例如每日体检脚本、cron、launchd）。
阈值写在本机配置 triage-local.json 的 "watch"：
  {"min_free_gib": 50, "min_free_pct": 10, "max_drop_gib_7d": 60}
  余量低于 min_free_gib 或 min_free_pct%，或比约一周前少了 max_drop_gib_7d 以上，就算异常。
异常时：--notify 弹一条 macOS 通知；--triage 顺带跑一次快速分档体检（约 2 分钟），把报告路径写进 latest.md。
每次的读数追加到 <out>/watch.jsonl（每天一行左右，体积可忽略），latest.md 是给人看的一页。
退出码：0 正常；10 异常（调用方可据此决定要不要提醒）。
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
GiB = 1024 ** 3
DEFAULTS = {"min_free_gib": 50, "min_free_pct": 10, "max_drop_gib_7d": 60}


def thresholds(local=None):
    if local is None:
        sys.path.insert(0, HERE)
        import triage
        local = triage.LOCAL
    w = dict(DEFAULTS)
    w.update({k: v for k, v in (local.get("watch") or {}).items() if k in DEFAULTS and v is not None})
    return w


def reading(path=HOME):
    s = os.statvfs(path)
    total, free = s.f_blocks * s.f_frsize, s.f_bavail * s.f_frsize
    return {"at": time.time(), "total": total, "free": free}


def history(f):
    out = []
    try:
        for ln in open(f, encoding="utf-8"):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def baseline(hist, now, days=7, min_hours=20):
    """取最接近 days 天前、且至少 min_hours 小时前的一条（磁盘容量相同才可比）。"""
    want = now["at"] - days * 86400
    cand = [h for h in hist if h.get("total") == now["total"] and now["at"] - h["at"] >= min_hours * 3600]
    return min(cand, key=lambda h: abs(h["at"] - want)) if cand else None


def assess(now, base, w):
    alerts = []
    free_gib, pct = now["free"] / GiB, 100.0 * now["free"] / now["total"]
    if free_gib < w["min_free_gib"]:
        alerts.append("可用只剩 %.0f GiB（低于 %g GiB）" % (free_gib, w["min_free_gib"]))
    if pct < w["min_free_pct"]:
        alerts.append("可用只剩 %.1f%%（低于 %g%%）" % (pct, w["min_free_pct"]))
    drop = None
    if base:
        drop = (base["free"] - now["free"]) / GiB
        if drop > w["max_drop_gib_7d"]:
            days = (now["at"] - base["at"]) / 86400
            alerts.append("%.1f 天里可用少了 %.0f GiB（超过 %g GiB）" % (days, drop, w["max_drop_gib_7d"]))
    return alerts, drop


def bjt(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def render(now, base, alerts, drop, triage_dir=None):
    lines = ["# 空间巡检 %s（北京时间）" % bjt(now["at"]), "",
             "- 可用 %.1f GiB / 共 %.0f GiB（%.1f%%）" % (now["free"] / GiB, now["total"] / GiB, 100.0 * now["free"] / now["total"])]
    if base:
        lines.append("- 对比 %s：可用%s %.1f GiB" % (bjt(base["at"]), "减少" if drop >= 0 else "增加", abs(drop)))
    else:
        lines.append("- 还没有约一周前的读数可比（至少要隔 20 小时）")
    lines += ["", "**异常**：" + "；".join(alerts) if alerts else "状态正常。"]
    if alerts:
        lines += ["", "下一步：让 Agent 用清理 Skill 做一次分档体检，看是谁在涨（不会自动删除任何东西）。"]
    if triage_dir:
        lines += ["", "已跑快速分档体检：`%s/triage.md`" % triage_dir]
    return "\n".join(lines) + "\n"


def notify(alerts):
    body = "；".join(alerts)[:180].replace('"', "'")
    subprocess.run(["osascript", "-e", 'display notification "%s" with title "磁盘空间提醒" '
                    'subtitle "详情 ~/.sansheng/disk-cleanup/watch/latest.md"' % body], check=False, capture_output=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=HOME + "/.sansheng/disk-cleanup/watch")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--triage", action="store_true")
    ap.add_argument("--path", default=HOME)
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    w = thresholds()
    hf = os.path.join(a.out, "watch.jsonl")
    hist = history(hf)
    now = reading(a.path)
    base = baseline(hist, now)
    alerts, drop = assess(now, base, w)
    with open(hf, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(now, alerts=alerts), ensure_ascii=False) + "\n")
    tdir = None
    if alerts and a.triage:
        tdir = os.path.join("/tmp", "sansheng-triage", "watch-" + dt.datetime.now().strftime("%Y%m%d-%H%M"))
        subprocess.run([sys.executable, os.path.join(HERE, "triage.py"), "--out", tdir],
                       capture_output=True, timeout=1800, check=False)
    text = render(now, base, alerts, drop, tdir)
    open(os.path.join(a.out, "latest.md"), "w", encoding="utf-8").write(text)
    print(("异常：" + "；".join(alerts)) if alerts else "正常：可用 %.0f GiB" % (now["free"] / GiB))
    if alerts and a.notify:
        notify(alerts)
    return 10 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
