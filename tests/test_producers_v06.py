"""0.6：producers.json 新增三类路径的归属（复用 scan.py 的加载与匹配，不另写一套）。

覆盖：Claude Code 会话临时目录 /private/tmp/claude-*、Codex 临时目录 ~/.codex/.tmp、
编译器模块缓存 ~/Library/Caches/clang；以及 /private/tmp 下非 claude-* 的路径不该归到 claude-code。
"""
import json
import os
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)
import scan  # noqa: E402

PRODUCERS = os.path.join(SCRIPTS, "producers.json")


def card_of(path):
    """走真实规则文件、纯 glob 匹配（apps=None），返回命中的卡片名。"""
    return scan.Producers(PRODUCERS).match(path)["card"]


def test_rules_file_is_valid():
    p = scan.Producers(PRODUCERS)
    assert p.schema_error is None
    assert p.rules, "producers.json 没有加载到任何规则"


def test_claude_tmp_dir_matches_claude_code():
    uid = os.getuid()
    root = "/private/tmp/claude-%d" % uid
    assert card_of(root) == "claude-code"
    # 规则默认连子路径一起匹配
    assert card_of(root + "/01J8x/session/tmp.bin") == "claude-code"


def test_other_private_tmp_not_claude_code():
    # /private/tmp 下不是 claude-* 的东西不能套 claude-code 卡
    for path in ("/private/tmp/foo", "/private/tmp/claude", "/private/tmp/other-claude-1", "/private/tmp/Claude-501"):
        assert card_of(path) != "claude-code", path


def test_codex_tmp_matches_codex():
    assert card_of(os.path.expanduser("~/.codex/.tmp")) == "codex"
    assert card_of(os.path.expanduser("~/.codex/.tmp/upload-123")) == "codex"


def test_clang_cache_matches_package_managers():
    path = os.path.expanduser("~/Library/Caches/clang")
    assert card_of(path) == "package-managers"
    # 不能被更靠后的通用 ~/Library/Caches（macos-system）抢走
    assert card_of(path) != "macos-system"
    assert card_of(path + "/ModuleCache.noindex/ABC.pcm") == "package-managers"
