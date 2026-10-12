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


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_claude_tmp_root(tmp_path_factory, monkeypatch):
    """0.6：scan 默认会把 /private/tmp/claude-<uid> 加进扫描根。测试一律指向不存在的目录，
    免得扫描夹具时顺带扫到本机真实的会话临时目录（慢，且结果不确定）。要测这条规则的用例自己再覆盖 TMP_BASE。"""
    try:
        import protect
    except ImportError:
        yield
        return
    monkeypatch.setattr(protect, "TMP_BASE", os.path.join(str(tmp_path_factory.getbasetemp()), "no-such-ptmp"))
    yield


@pytest.fixture(autouse=True)
def _no_real_extra_app_roots(monkeypatch):
    """leftovers 会额外并入 /System/Applications/Utilities；测试夹具的“已装清单”不能混进本机真实系统 App。
    要测这条合并的用例自己传 roots。"""
    try:
        import leftovers
    except ImportError:
        yield
        return
    monkeypatch.setattr(leftovers, "EXTRA_APP_ROOTS", ())
    yield
