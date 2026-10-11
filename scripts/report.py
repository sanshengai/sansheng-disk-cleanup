#!/usr/bin/env python3
"""叁笙清理：清理完成后的成果页（report.html）与分享数据。

    python3 scripts/report.py --run <run目录>

数字一律来自回执的实测值：同一 seal 下全部正式执行回执（中断后重跑会有多份）加上试点回执
（试点先删掉的那一条，在正式执行里记为“路径已不存在”跳过，不加回来就会少算）；预演不计，同一路径不重复计。
分享链接只带几个汇总数字（GB，保留一位小数），不带任何路径、用户名或文件信息。
"""
import argparse
import base64
import json
import os
import re
import sys
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import plan  # noqa: E402
import prefs  # noqa: E402
import units  # noqa: E402
from vendor.qrcodegen import QrCode  # noqa: E402

TEMPLATE = os.path.join(os.path.dirname(HERE), "assets", "report.html")
# 扫码落地页：官网，随 Skill 公开一起上线；页面在浏览器端读这几个数字渲染
SHARE_BASE = "https://sanshengai.top/tools/disk-cleanup/"
AI_SECTION = "AI 工作"
REPO_URL = "https://github.com/sanshengai/sansheng-disk-cleanup"      # 首次成果页的 Star 提示链接


_gb1 = units.gb_number


_STAMPED = re.compile(r"^receipt-(pilot-)?\d{8}T\d{6}Z\.json$")      # 预演回执 receipt-dryrun-*.json 不在其内


def _identity(rc):
    return (rc.get("started_at"), rc.get("finished_at"), rc.get("mode"), rc.get("sel"))


def load_receipts(run):
    """返回 (最后一次正式回执, 同一 seal 下全部真正删过东西的回执，按开始时间排序)。

    中断后重跑会有多份正式回执，各自只记自己那一次删掉的；试点删掉的那一条在后面的正式回执里是“已不存在”跳过。
    所以这里取 run 目录里与最后一次正式回执同 scan_id、同 seal 的全部正式回执和试点回执（预演不计）；
    receipt.json 与带时间戳的同一份回执只算一次，被闸门引用又被目录扫到的试点回执也只算一次。"""
    full = plan.read_json(os.path.join(run, "receipt.json"))
    names = ["receipt.json"] + sorted(n for n in os.listdir(run) if _STAMPED.match(n))
    gate_pilot = (full.get("gate") or {}).get("pilot_receipt")
    if gate_pilot:
        names.append(os.path.basename(gate_pilot))
    seen, found = set(), []
    for name in names:
        try:
            rc = plan.read_json(os.path.join(run, name))
        except ValueError:
            continue
        if (not isinstance(rc, dict) or rc.get("dry_run") or rc.get("mode") == "dry_run"
                or rc.get("seal") != full.get("seal") or rc.get("scan_id") != full.get("scan_id")):
            continue
        key = _identity(rc)
        if key in seen:
            continue
        seen.add(key)
        found.append(rc)
    if _identity(full) not in seen:
        found.append(full)
    found.sort(key=lambda r: (r.get("started_at") or "", 0 if r.get("pilot") else 1))
    return full, found


def freed_by_card(receipts):
    """每张卡释放的字节。同一路径的整体删除（delete 方法、状态 removed）最多只可能发生一次，同一路径在多份回执里
    都记为已删除时只取最大的那份，不重复计；部分删除（partial）和原生命令（每次清的是不同的内容）逐份累加。"""
    whole, rest = {}, {}
    for rc in receipts:
        for c in rc.get("cards", []):
            for it in c.get("items", []):
                if it.get("status") not in ("removed", "partial"):
                    continue
                b = int((it.get("removed") or {}).get("bytes") or 0)
                if it.get("status") == "removed" and it.get("method") == "delete":
                    k = (c["id"], it.get("path"))
                    whole[k] = max(whole.get(k, 0), b)
                else:
                    rest[c["id"]] = rest.get(c["id"], 0) + b
    out = dict(rest)
    for (cid, _), b in whole.items():
        out[cid] = out.get(cid, 0) + b
    return out


def share_url(freed, used_before, used_after, ai_freed):
    q = {"v": 1, "f": _gb1(freed), "b": _gb1(used_before), "a": _gb1(used_after), "ai": _gb1(ai_freed)}
    return SHARE_BASE + "?" + urlencode(q)


def qr_rows(text):
    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    n = qr.get_size()
    return ["".join("1" if qr.get_module(x, y) else "0" for x in range(n)) for y in range(n)]


def build_report(run):
    facts = plan.read_json(os.path.join(run, "facts.json"))
    pl, _ = plan.load_plan(run)
    full, receipts = load_receipts(run)
    by_card = freed_by_card(receipts)
    reclaimed = sum(int((r.get("summary") or {}).get("history_reclaimed") or 0) for r in receipts)
    freed = sum(by_card.values()) + reclaimed

    total = int((facts.get("disk") or {}).get("total") or 0)
    first = receipts[0].get("summary") or {}
    free_before = int(first.get("free_before") or (facts.get("disk") or {}).get("free") or 0)
    used_before = max(0, total - free_before)
    used_after = max(0, used_before - freed)

    cats = {c["id"]: c for c in plan.BOARD_CATEGORIES}
    cards_by_id = {c.get("id"): c for c in pl.get("cards") or [] if isinstance(c, dict)}
    cat_freed, card_rows = {}, []
    for cid, b in by_card.items():
        c = cards_by_id.get(cid) or {}
        cat, _ = plan.card_category(c)
        cat_freed[cat] = cat_freed.get(cat, 0) + b
        card_rows.append({"title": c.get("title") or cid, "category": cat, "bytes": b})
    if reclaimed:                                       # Codex 历史库回收算在 AI 对话记录
        cat_freed["ai-chats"] = cat_freed.get("ai-chats", 0) + reclaimed
    categories = [{"id": k, "name": cats.get(k, {}).get("name", "其他"), "section": cats.get(k, {}).get("section", ""),
                   "bytes": v} for k, v in cat_freed.items() if v > 0]
    order = [c["id"] for c in plan.BOARD_CATEGORIES]
    categories.sort(key=lambda x: (-x["bytes"], order.index(x["id"]) if x["id"] in order else 99))
    ai_freed = sum(x["bytes"] for x in categories if x["section"] == AI_SECTION)

    governance = [{"title": g.get("title"), "suggest": g.get("suggest"), "how": g.get("how"), "done": bool(g.get("done"))}
                  for g in pl.get("governance") or [] if isinstance(g, dict) and g.get("title")]
    url = share_url(freed, used_before, used_after, ai_freed)
    return {
        "schema": "report/1", "scan_id": full.get("scan_id"), "created_at": plan.now_iso(),
        "finished_at": full.get("finished_at"),
        "disk_total": total, "used_before": used_before, "used_after": used_after,
        "free_before": free_before, "free_after": free_before + freed,
        "freed": freed, "freed_measured": sum(int((r.get("summary") or {}).get("freed_measured") or 0) for r in receipts),
        "ai_freed": ai_freed, "categories": categories, "category_order": order,
        "cards": sorted(card_rows, key=lambda x: -x["bytes"]),
        "governance": governance, "share": {"url": url, "qr": qr_rows(url)},
    }


def render(run, report, out="report.html"):
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    with open(plan.LOGO, "rb") as f:
        logo = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    html = html.replace("__LOGO_DATA__", logo).replace("__REPORT_JSON__", plan._json_for_html(report))
    path = out if os.path.isabs(out) else os.path.join(run, out)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, path)
    return path


def _star_shown_before(run):
    """同一次清理里重新生成成果页时，上一份若已带着 Star 提示就继续带着（否则第一次的提示会被刷新冲掉）。"""
    try:
        old = plan.read_json(os.path.join(run, "report.json"))
    except (OSError, ValueError):
        return False
    return isinstance(old, dict) and (old.get("star") or {}).get("show") is True


def make(run):
    """正式生成成果页。第一次（profile 里没有 star_asked_at）在分享区下方带一句 Star 提示，生成后记下时间，之后的清理不再出现。
    只有这里写 profile；build_report 是纯计算，预览 / 测试里单独调用不会改任何设置。"""
    report = build_report(run)
    profile = prefs.load()
    first = not profile.get("star_asked_at")
    report["star"] = {"show": bool(first or _star_shown_before(run)), "url": REPO_URL}
    plan.write_json(os.path.join(run, "report.json"), report)
    path = render(run, report)
    if first:
        try:
            profile["star_asked_at"] = prefs.now_iso()
            prefs.save(profile)
        except OSError as exc:                  # 写不进去只是下次还会出现，不影响成果页
            print(f"提示：没能记下 Star 提示已出现（{exc}）", file=sys.stderr)
    return path, report


def main(argv=None):
    ap = argparse.ArgumentParser(description="叁笙清理：成果页")
    ap.add_argument("--run", required=True, help="运行目录（含 receipt.json）")
    args = ap.parse_args(argv)
    run = os.path.realpath(args.run)
    if not os.path.isfile(os.path.join(run, "receipt.json")):
        print("错误：还没有正式执行回执（receipt.json），成果页只在清理完成后生成", file=sys.stderr)
        return 2
    try:
        path, report = make(run)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(f"成果页：{path}")
    fb = units.fmt_bytes
    print(f"腾出 {fb(report['freed'])}（其中 AI 工作 {fb(report['ai_freed'])}）；"
          f"已用 {fb(report['used_before'])} → {fb(report['used_after'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
