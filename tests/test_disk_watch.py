"""disk_watch.py：只读空间巡检的阈值、基准选择与本机配置回流。"""
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import disk_watch as dw  # noqa: E402

G = 1024 ** 3
W = {"min_free_gib": 50, "min_free_pct": 10, "max_drop_gib_7d": 60}


def r(at, free, total=1000 * G):
    return {"at": at, "free": free, "total": total}


def test_assess_thresholds():
    assert dw.assess(r(0, 400 * G), None, W) == ([], None)
    alerts, _ = dw.assess(r(0, 40 * G), None, W)
    assert any("GiB" in a for a in alerts) and any("%" in a for a in alerts)
    alerts, drop = dw.assess(r(7 * 86400, 300 * G), r(0, 400 * G), W)
    assert drop == 100 and any("少了" in a for a in alerts)
    alerts, _ = dw.assess(r(7 * 86400, 350 * G), r(0, 400 * G), W)
    assert alerts == []


def test_baseline_picks_closest_week_and_same_disk():
    now = r(10 * 86400, 1)
    hist = [r(0, 1), r(3 * 86400, 1), r(9.5 * 86400, 1), r(3 * 86400, 1, total=5 * G)]
    assert dw.baseline(hist, now)["at"] == 3 * 86400         # 最接近 7 天前；20 小时内的不算；换盘的不算
    assert dw.baseline([r(9.9 * 86400, 1)], now) is None


def test_main_writes_history_and_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(dw, "thresholds", lambda local=None: dict(W, min_free_gib=10 ** 9))
    rc = dw.main(["--out", str(tmp_path)])
    assert rc == 10
    rows = [json.loads(l) for l in open(tmp_path / "watch.jsonl")]
    assert len(rows) == 1 and rows[0]["alerts"]
    assert "异常" in open(tmp_path / "latest.md", encoding="utf-8").read()


def test_local_config_extends_tier1_and_tier3(tmp_path, monkeypatch):
    cfg = tmp_path / "local.json"
    cfg.write_text(json.dumps({"extra_tier1": [{"category": "某工具缓存", "globs": ["~/.某工具/cache"], "quiet_hours": 12}],
                               "extra_tier3": [{"category": "某 Agent 会话", "glob": "~/.某agent/sessions", "note": "问用户"}],
                               "watch": {"min_free_gib": 80}}, ensure_ascii=False))
    import triage
    monkeypatch.setenv("SANSHENG_DISK_CLEANUP_TRIAGE", str(cfg))
    try:
        importlib.reload(triage)
        assert ("某工具缓存", [os.path.expanduser("~/.某工具/cache")], "本机配置登记的可再生缓存", 12) in triage.TIER1
        assert any(c == "某 Agent 会话" for c, _, _ in triage.TIER3_GLOBS)
        assert dw.thresholds(triage.LOCAL)["min_free_gib"] == 80
    finally:
        monkeypatch.delenv("SANSHENG_DISK_CLEANUP_TRIAGE")
        importlib.reload(triage)
