"""0.6 成果页：处置方式口径（废纸篓不算已腾出）、这次读了哪些文件、这次我记住了、从源头改好的。
数据文件不存在时对应区块不显示。"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, age, card, delete_item, mkfile, node, plan, protect, with_gate  # noqa: E402
import report  # noqa: E402
from test_act_disposal import vol, run_full, set_profile  # noqa: E402,F401


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def setup(env, n=3, size=100_000):
    nodes, cards, dirs = [], [], {}
    for i in range(1, n + 1):
        d = env.work / f"d{i}"
        mkfile(d / "a.bin", b"x" * size * i)
        age(d)
        dirs[f"c0{i}"] = d
        nodes.append(node(d, id=f"n{i}"))
        cards.append(card(f"c0{i}", [delete_item(d)], verdict="decide", question="处理吗？"))
    env.write(nodes, cards)
    return env.seal(), dirs


def removed_bytes(rc, cid):
    return sum(it["removed"]["bytes"] for c in rc["cards"] if c["id"] == cid for it in c["items"])


def share_f(rp):
    return float(parse_qs(urlparse(rp["share"]["url"]).query)["f"][0])


# ───────────────────────── 处置方式口径 ─────────────────────────

def test_trash_bytes_are_not_counted_as_freed(env):
    digest, dirs = setup(env, 1)
    assert run_full(env, digest, "c01", "--trash", answers=["c01=删"]) == 0
    rc = env.receipt()
    assert removed_bytes(rc, "c01") > 0                           # 确实处理了
    rp = report.build_report(str(env.run))
    assert rp["freed"] == 0 and rp["used_after"] == rp["used_before"]
    assert rp["disposal"]["freed"] == 0 and rp["disposal"]["trash"] == removed_bytes(rc, "c01")
    assert rp["disposal"]["trash_dirs"] and rp["disposal"]["trash_dirs"][0].startswith("叁笙清理-scan1-")
    assert rp["cards"] == [] and rp["categories"] == []
    assert share_f(rp) == 0.0                                    # 分享链接用“已腾出”口径


def test_three_modes_across_runs_split_into_freed_trash_and_archived(env, vol):
    digest, dirs = setup(env, 3)
    assert run_full(env, digest, "c01", "--no-trash", answers=["c01=删"]) == 0
    c01 = removed_bytes(env.receipt(), "c01")
    assert run_full(env, digest, "c02", "--trash", answers=["c02=删"]) == 0
    c02 = removed_bytes(env.receipt(), "c02")
    assert run_full(env, digest, "c03", "--no-trash", "--archive-to", str(vol), answers=["c03=归档"]) == 0
    c03 = removed_bytes(env.receipt(), "c03")
    rp = report.build_report(str(env.run))
    assert min(c01, c02, c03) > 0
    assert rp["freed"] == c01 + c03               # 归档且原文件已删：空间腾出了；进废纸篓的不算
    assert rp["disposal"]["trash"] == c02 and rp["disposal"]["freed"] == c01 + c03
    assert rp["disposal"]["archived"] == [{"volume": "备份盘", "source": "delete", "bytes": c03}]


def test_trash_and_delete_in_one_report(env):
    digest, dirs = setup(env, 2)
    assert run_full(env, digest, "c01", "--no-trash", answers=["c01=删"]) == 0
    c01 = removed_bytes(env.receipt(), "c01")
    assert run_full(env, digest, "c02", "--trash", answers=["c02=删"]) == 0
    c02 = removed_bytes(env.receipt(), "c02")
    rp = report.build_report(str(env.run))
    assert rp["freed"] == c01 and rp["disposal"]["trash"] == c02 and rp["disposal"]["freed"] == c01
    assert [c["bytes"] for c in rp["cards"]] == [c01]
    assert share_f(rp) == round(c01 / 1e9, 1)


def test_archive_with_trash_source_counts_as_trash_not_freed(env, vol):
    set_profile(env, delete_mode="trash")
    digest, dirs = setup(env, 1)
    assert run_full(env, digest, "c01", "--archive-to", str(vol), answers=["c01=归档"]) == 0
    b = removed_bytes(env.receipt(), "c01")
    rp = report.build_report(str(env.run))
    assert rp["freed"] == 0 and rp["disposal"]["trash"] == b
    assert rp["disposal"]["archived"] == [{"volume": "备份盘", "source": "trash", "bytes": b}]


def test_old_receipts_without_disposal_count_as_freed(env):
    """0.5 的回执没有 disposal 字段：按“已删除”算，行为不变。"""
    digest, dirs = setup(env, 1)
    assert run_full(env, digest, "c01", "--no-trash", answers=["c01=删"]) == 0
    rc = env.receipt()
    for c in rc["cards"]:
        for it in c["items"]:
            it.pop("disposal")
    (env.run / "receipt.json").write_text(json.dumps(rc))
    for p in env.run.glob("receipt-2*.json"):
        p.unlink()
    rp = report.build_report(str(env.run))
    assert rp["freed"] == removed_bytes(rc, "c01") and rp["disposal"]["trash"] == 0


# ───────────────────────── 三个可选区块 ─────────────────────────

def finish(env):
    digest, dirs = setup(env, 1)
    assert run_full(env, digest, "c01", "--no-trash", answers=["c01=删"]) == 0
    return report.build_report(str(env.run))


def test_optional_blocks_absent_when_files_missing(env):
    rp = finish(env)
    assert rp["reads"] is None and rp["remembered"] is None and rp["fixed"] is None


def test_optional_blocks_bad_files_are_ignored(env):
    (env.run / "remembered.json").write_text("{坏的")
    (env.run / "governance-check.json").write_text('{"not": "a list"}')
    rp = finish(env)
    assert rp["remembered"] is None and rp["fixed"] is None


def test_reads_block_counts_and_first_20_paths(env):
    lines = [{"at": "t", "path": f"~/Documents/f{i}.txt", "class": "basic", "mode": "content"} for i in range(25)]
    lines += [{"at": "t", "path": "~/x.png", "class": "extended", "mode": "meta"},
              {"at": "t", "path": "~/.ssh/id_rsa", "class": "never", "mode": "refused"},
              {"at": "t", "path": "~/Documents/f0.txt", "class": "basic", "mode": "content"}]
    (env.run / "reads.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n碎行\n")
    rp = finish(env)
    r = rp["reads"]
    assert (r["content"], r["meta"], r["refused"]) == (26, 1, 1)
    assert len(r["paths"]) == 20 and r["paths"][0] == "~/Documents/f0.txt" and r["more"] == 5
    assert "~/.ssh/id_rsa" not in r["paths"]                   # 被拒的不算读过


def test_reads_block_empty_file_still_shown_with_zero_counts(env):
    (env.run / "reads.jsonl").write_text("")
    r = finish(env)["reads"]
    assert r == {"content": 0, "meta": 0, "refused": 0, "paths": [], "more": 0}


def test_remembered_block_filters_forgotten_and_bad_ids(env):
    (env.run / "remembered.json").write_text(json.dumps([
        {"id": "m1", "kind": "rule", "text": "下载里的视频都留着"},
        {"id": "m2", "kind": "identity", "text": "~/Movies/成片 是交付成品", "forgotten": True},
        {"id": "m 3,x", "kind": "rule", "text": "id 里有逗号和空格，复制成“忘掉:”会串"},
        {"id": "m4", "text": "没有 kind"}], ensure_ascii=False))
    rows = finish(env)["remembered"]
    assert [r["id"] for r in rows] == ["m1", "m4"] and rows[0]["text"] == "下载里的视频都留着"


def test_fixed_block_verdicts_and_home_path_shortened(env):
    (env.run / "governance-check.json").write_text(json.dumps([
        {"path": str(env.home / ".cache" / "foo"), "change": "把 foo 的缓存上限调小", "baseline_bytes": 5_000_000_000,
         "now_bytes": 5_010_000_000, "delta": 10_000_000, "verdict": "stopped"},
        {"path": "/opt/x", "change": "给 x 加了定时清理", "baseline_bytes": 1, "now_bytes": 9, "delta": 8, "verdict": "growing"},
        {"path": "/opt/y", "change": "y", "verdict": "乱写的"}], ensure_ascii=False))
    rows = finish(env)["fixed"]
    assert [r["verdict"] for r in rows] == ["stopped", "growing", "unknown"]
    assert rows[0]["path"] == "~/.cache/foo" and rows[1]["delta"] == 8


# ───────────────────────── 页面 ─────────────────────────

def test_rendered_page_embeds_new_blocks_and_script_parses(env):
    (env.run / "remembered.json").write_text(json.dumps([{"id": "m1", "kind": "rule", "text": "视频都留着"}], ensure_ascii=False))
    (env.run / "reads.jsonl").write_text(json.dumps({"at": "t", "path": "~/a.txt", "class": "basic", "mode": "content"}) + "\n")
    finish(env)
    path, rp = report.make(str(env.run))
    html = Path(path).read_text(encoding="utf-8")
    for sec in ("disp-sec", "fixed-sec", "mem-sec", "reads-sec"):
        assert f'id="{sec}" hidden' in html                       # 默认隐藏，有数据时脚本才显示
    assert "这些内容发给了你的 AI 服务商" in html and "忘掉: " in html and "彻底删除本轮文件" in html
    assert "已移入废纸篓，等待彻底删除" in html and "实际释放仍为 0 B" in html
    assert "__REPORT_JSON__" not in html
    data = json.loads(re.search(r"const R=(\{.*?\});\nconst \$=", html, re.S).group(1))
    assert data["remembered"][0]["id"] == "m1" and data["reads"]["content"] == 1 and data["disposal"]["trash"] == 0
    node = shutil.which("node")
    if node:                                                      # 页面脚本语法没坏
        script = re.search(r"<script>(.*)</script>", html, re.S).group(1)
        js = Path(env.run / "syntax-check.js")
        js.write_text("new Function(require('fs').readFileSync(0,'utf8'))", encoding="utf-8")
        cp = subprocess.run([node, str(js)], input=script, capture_output=True, text=True)
        assert cp.returncode == 0, cp.stderr[:300]
