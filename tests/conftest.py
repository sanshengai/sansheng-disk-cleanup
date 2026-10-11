"""测试环境卫生：pytest 默认临时目录在 /private/var/folders 下，而 producers.json 把 /private/var/folders/** 标为 risky。
plan.py 现在对条目路径本身跑 producer 规则（P5.R H1），夹具若落在那里会被判成“风险区”，污染所有结论。
没有显式 --basetemp 时，把临时根挪到 ~/.cache 下（路径里不含 tmp/temp 等字样，scan 的“废稿路径”判断会把它们当临时目录）一个按进程区分的目录（并发的测试进程互不清理），结束时删除。"""
import os
import shutil
import tempfile

_OWN = []


def pytest_configure(config):
    if getattr(config.option, "basetemp", None):
        return
    if not os.path.realpath(tempfile.gettempdir()).startswith("/private/var/folders"):
        return
    base = os.path.expanduser(f"~/.cache/sansheng-cleanup-pytest-{os.getpid()}")
    config.option.basetemp = base
    _OWN.append(base)


def pytest_unconfigure(config):
    for d in _OWN:
        shutil.rmtree(d, ignore_errors=True)
