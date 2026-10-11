"""体积的统一写法：十进制（1 GB = 1e9 字节，与访达一致），一位小数。

act.py 的回执打印、plan.py 的检查/提案/看板、report.py 的成果页、scan.py 的扫描摘要都用 fmt_bytes；
assets/dashboard.html 与 assets/report.html 里的 JS 端 fmtBytes 必须和它逐值一致（tests/test_units.py 在 node 里对照）。
分档：小于 1000 字节写 B（整数），之后 KB / MB / GB / TB 各一位小数；小于 1 GB 用 MB（如 512.3 MB），不写成 0.5 GB。
"""

_TIERS = (("KB", 1e3), ("MB", 1e6), ("GB", 1e9), ("TB", 1e12))


def fmt_bytes(n):
    """字节数 → “1.5 GB”“512.3 MB”“8.0 KB”“999 B”。负数带负号（用于差异）；None 按 0。"""
    n = float(n or 0)
    a = abs(n)
    if a < 1000:
        return f"{int(n)} B"
    sign = "-" if n < 0 else ""
    for unit, div in _TIERS:
        v = a / div
        if float(f"{v:.1f}") < 1000 or unit == "TB":      # 999.96 MB 四舍五入成 1000.0 时进位到 GB
            return f"{sign}{v:.1f} {unit}"


def gb_number(n):
    """只要数字、不带单位的 GB（分享链接参数用）：一位小数，负数按 0。"""
    return f"{max(0, n or 0) / 1e9:.1f}"
