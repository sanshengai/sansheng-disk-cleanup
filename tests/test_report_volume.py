"""成果页的条目核算、HOME 卷观测和分享口径。"""
import json
from urllib.parse import urlsplit

import pytest

from test_report_v06 import env, finish, report


def sample(free, used, device=14):
    return {"device": device, "total": 1000, "used": used, "free": free}


def receipt(start, end, before, after):
    return {"started_at": start, "finished_at": end, "summary": {
        "home_volume_before": before, "home_volume_after": after}}


def test_noncontiguous_receipts_and_finalize_use_outer_boundaries():
    first = receipt("01", "02", sample(390, 590), sample(500, 480))
    last = receipt("03", "04", sample(370, 610), sample(360, 620))
    volume = report.volume_window([first, last])
    assert (volume["free_before"], volume["free_after"], volume["freed_measured"]) == (390, 360, -30)
    assert (volume["used_before"], volume["used_after"]) == (590, 620)
    journal = dict(started_at="05", finished_at="06", home_volume_after=sample(0, 980))
    assert report.volume_window([last, first], journal)["freed_measured"] == -390
    journal["finished_at"] = None
    assert report.volume_window([first, last], journal)["free_after"] is None


@pytest.mark.parametrize("before,after", [
    (None, None), (sample(300, 400), None), (sample(300, 400), sample(100, 400, device=22)),
    (sample(-1, 400), sample(100, 400)), (sample(999, 400), sample(100, 400)),
])
def test_missing_or_incompatible_observations_stay_unknown(before, after):
    rc = receipt("01", "03", before, after)
    rc["summary"].update(free_before=500, free_after=700, freed_measured=200)
    assert all(x is None for x in report.volume_window([rc]).values())


def test_report_from_legacy_receipt_and_share_privacy(env):
    finish(env)
    rc = env.receipt()
    for side in ["before", "after"]:
        rc["summary"].pop("home_volume_" + side)
    (env.run / "receipt.json").write_text(json.dumps(rc))
    for p in env.run.glob("receipt-2*.json"):
        p.unlink()
    result = report.build_report(str(env.run))
    assert result["freed"] > 0 and result["freed_measured"] is None
    assert result["used_after"] is None and result["free_after"] is None
    assert not urlsplit(result["share"]["url"]).query
