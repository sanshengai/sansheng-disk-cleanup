"""0.5 体积单位统一：全部十进制（1 GB = 1e9 字节，与访达一致），一位小数；act / plan / report / scan / 两个网页 JS 同值。"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, age, card, delete_item, mkfile, node, plan  # noqa: E402
import report  # noqa: E402
import scan  # noqa: E402
import units  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# 字节数 → 期望文案（含分档边界）
VECTORS = [
    (0, "0 B"), (999, "999 B"), (1000, "1.0 KB"), (1536, "1.5 KB"), (999_949, "999.9 KB"),
    (999_950, "1.0 MB"), (512_300_000, "512.3 MB"), (999_949_999, "999.9 MB"), (999_950_000, "1.0 GB"),
    (1_000_000_000, "1.0 GB"), (1_073_741_824, "1.1 GB"),          # 1 GiB 在十进制下是 1.1 GB，不再是 1.0
    (9_740_000_000, "9.7 GB"), (12_000_000_000, "12.0 GB"), (1_530_000_000, "1.5 GB"),
    (999_949_000_000, "999.9 GB"), (999_950_000_000, "1.0 TB"), (2_000_000_000_000, "2.0 TB"), (-1_500_000_000, "-1.5 GB"),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


@pytest.mark.parametrize("n,text", VECTORS)
def test_fmt_bytes_decimal_one_decimal(n, text):
    assert units.fmt_bytes(n) == text


def test_every_module_uses_the_same_formatter():
    assert act.fmt_bytes is units.fmt_bytes and plan._gb is units.fmt_bytes and scan.human is units.fmt_bytes
    for n, text in VECTORS:
        assert act.fmt_bytes(n) == plan._gb(n) == scan.human(n) == text
    assert report._gb1(1_530_000_000) == "1.5" and report._gb1(-5) == "0.0"


def test_not_binary():
    """回归：旧实现按 1024 进位且标 GB（1 GiB 显示成 1.0GB，成果页却是 1.1 GB）。"""
    assert units.fmt_bytes(1024 ** 3) != "1.0 GB"
    assert units.fmt_bytes(10 ** 9) == "1.0 GB"


def test_act_print_and_report_print_agree(env, capsys):
    """同一次清理：act 打印的“按条目核算释放”与成果页命令行打印的“腾出”是同一个字符串。"""
    d = env.work / "cache"
    mkfile(d / "a.bin", b"x" * 2_000_000)
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    assert env.act("c01") == 0
    out = capsys.readouterr().out
    freed = env.receipt()["summary"]["freed_by_items"]
    assert freed >= 2_000_000
    text = units.fmt_bytes(freed)
    assert f"按条目核算释放 {text}" in out
    assert report.main(["--run", str(env.run)]) == 0
    assert f"腾出 {text}" in capsys.readouterr().out


def _js_fn(html_name, name):
    html = (ASSETS / html_name).read_text(encoding="utf-8")
    m = re.search(r"const %s=n=>\{.*?\}\}; //" % name, html, re.S) or re.search(r"const %s=n=>\{.*?\}\};" % name, html, re.S)
    assert m, f"{html_name} 里找不到 {name}"
    return m.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
@pytest.mark.parametrize("html,name", [("dashboard.html", "fmtBytes"), ("onboard.html", "fmtBytes"), ("report.html", "gb")])
def test_js_formatter_matches_python(tmp_path, html, name):
    js = _js_fn(html, name)
    cases = [n for n, _ in VECTORS] + [1, 4096, 8_192, 123_456_789, 55_555_555_555]
    prog = js + "\nconsole.log(JSON.stringify(%s.map(%s)));" % (json.dumps(cases), name)
    f = tmp_path / "fmt.js"
    f.write_text(prog, encoding="utf-8")
    out = json.loads(subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30, check=True).stdout)
    assert out == [units.fmt_bytes(n) for n in cases]


def test_dashboard_gb_wrapper_keeps_special_values():
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    assert "const gb=n=>{if(n==null)return'未统计';if(n<=0)return'0';return fmtBytes(n)};" in html
    assert "maximumFractionDigits:n>=1e10" not in html          # 旧的 9.74 / 12 / 1.53 混用写法已去掉
