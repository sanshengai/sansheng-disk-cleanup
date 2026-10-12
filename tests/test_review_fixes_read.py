"""0.6 发版评审第 3、4 条：peek 永不读名单补全（按“词”匹配凭证名），leftovers 的 Group Containers / LaunchAgents 误报。

peek 部分全在 tmp_path 里分类（只分类，不读内容）；leftovers 部分用 monkeypatch 替换 subprocess.run（codesign / launchctl）。
"""
import os
import plistlib
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)
import leftovers  # noqa: E402
import peek  # noqa: E402
import test_leftovers  # noqa: E402

# 在任何 Env 夹具替换它们之前先留住真函数（Env 默认用假的 codesign / launchctl）
REAL_READ_APP_GROUPS = leftovers._read_app_groups
REAL_LAUNCHCTL_LOADED = leftovers._launchctl_loaded


# ───────────────────────── 第 3 条：peek 永不读名单 ─────────────────────────

NEVER_PATHS = [
    ".claude.json", ".claude.json.backup", ".codex/config.toml", ".codex/auth.json", ".npmrc", ".git-credentials",
    ".pypirc", ".cargo/credentials", ".cargo/credentials.toml", ".docker/config.json", ".kube/config",
    ".kube/cache/x.json", ".config/gcloud/credentials.db", ".config/gcloud/configurations/config_default",
    ".gemini/oauth_creds.json", ".netrc", ".aws/credentials", ".aws/config",
    ".zshrc", ".bashrc", ".zprofile", ".bash_profile", ".profile", ".zshenv", ".zsh_history", ".bash_history",
    ".claude/settings.json", ".claude/settings.local.json", ".codex/hooks.toml", ".cursor/mcp.json",
    ".config/opencode/config.json", ".config/fish/config.fish", ".config/gh/hosts.yml",
    "Documents/auth.json", "Documents/my-token.txt", "Documents/client_secret_123.json", "Downloads/OAuthClient.json",
    "Downloads/access.token", "Documents/credentials.csv", "Documents/API-Secrets.md",
]
BASIC_PATHS = [
    "Documents/author.md", "Documents/authority.txt", "Documents/authors-note.md", "Documents/tokenizer.json",
    "Documents/secretary.docx", "Documents/authentic.md", "Cowork/README.md", "Cowork/AGENTS.md",
    "Cowork/notes/plan.md", ".config/notes.txt", ".profiles/readme.txt", ".claude/projects/notes.md",
]


@pytest.fixture
def home(tmp_path):
    h = (tmp_path / "home").resolve()
    h.mkdir()
    return str(h)


@pytest.mark.parametrize("rel", NEVER_PATHS)
def test_never_list_covers_review_paths(home, rel):
    assert peek.classify(os.path.join(home, rel), home=home) == "never", rel


@pytest.mark.parametrize("rel", BASIC_PATHS)
def test_ordinary_files_with_lookalike_names_stay_basic(home, rel):
    assert peek.classify(os.path.join(home, rel), home=home) == "basic", rel


def test_credential_words_match_whole_words_not_substrings():
    # 规则：名字（含扩展名）按非字母数字、驼峰边界、字母与数字边界切词，整词命中 credential(s)/secret(s)/token(s)/oauth/auth 才算
    assert peek.name_tokens("OAuthClient.json") == ["o", "auth", "client", "json"]
    assert peek.name_tokens("client_secret_123.json") == ["client", "secret", "123", "json"]
    for hit in ("auth.json", "AUTH.TXT", "my-token.txt", "oauth2.json", "access.token", "GitHubToken.md",
                "tokens.md", "credentials", "x.secret"):
        assert peek.has_credential_word(hit), hit
    for miss in ("author.md", "Author.md", "authority.txt", "authors.md", "authentic.md", "tokenizer.json",
                 "TokenizerConfig.md", "secretary.docx", "secretion.txt", "oauthor.md", "", "readme"):
        assert not peek.has_credential_word(miss), miss


def test_classify_uses_original_case_for_camel_words_and_blocks_outside_home(home, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert peek.classify(str(outside / "AuthToken.json"), home=home) == "never"    # 家目录外也按文件名
    assert peek.classify(str(outside / "author.md"), home=home) == "basic"


def test_never_is_refused_by_peek_cli_even_with_all_scopes(home, tmp_path, monkeypatch, capsys):
    import protect
    monkeypatch.setattr(protect, "HOME", home)
    monkeypatch.setattr(protect, "STATE_DIR", os.path.join(home, ".sansheng", "disk-cleanup"))
    run = tmp_path / "run"
    run.mkdir()
    target = os.path.join(home, ".zshrc")
    with open(target, "w") as f:
        f.write("export SECRET_KEY=abc\n")
    assert peek.main(["--run", str(run), target]) == 3
    assert "abc" not in capsys.readouterr().out


# ───────────────────────── 第 4 条：_read_app_groups ─────────────────────────

def _cp(stdout=b"", stderr=b"", rc=0):
    return subprocess.CompletedProcess(["x"], rc, stdout, stderr)


def _ent(groups):
    return plistlib.dumps({"com.apple.security.application-groups": groups, "other": True})


@pytest.fixture
def clean_cache():
    leftovers._APP_GROUP_CACHE.clear()
    yield
    leftovers._APP_GROUP_CACHE.clear()


def test_read_app_groups_parses_codesign_xml_with_timeout_and_cache(monkeypatch, clean_cache):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return _cp(_ent(["group.is.workflow.shortcuts"]))
    monkeypatch.setattr(leftovers.subprocess, "run", fake_run)
    assert REAL_READ_APP_GROUPS("/System/Applications/Shortcuts.app") == ["group.is.workflow.shortcuts"]
    assert REAL_READ_APP_GROUPS("/System/Applications/Shortcuts.app") == ["group.is.workflow.shortcuts"]
    assert len(calls) == 1                                           # 第二次走缓存
    cmd, kw = calls[0]
    assert cmd == ["codesign", "-d", "--entitlements", ":-", "/System/Applications/Shortcuts.app"]
    assert kw["timeout"] == 5 and kw.get("shell") is False


def test_read_app_groups_failures_return_none_but_unsigned_or_empty_is_readable(monkeypatch, clean_cache):
    def run_with(result):
        def fake_run(cmd, **kw):
            if isinstance(result, Exception):
                raise result
            return result
        return fake_run
    cases = [
        (subprocess.TimeoutExpired("codesign", 5), None),                       # 超时
        (FileNotFoundError("codesign"), None),                                  # 命令缺失
        (_cp(b"not a plist at all", rc=0), None),                               # 解析失败
        (_cp(b"", b"/x: code object is not signed at all\n", rc=1), []),        # 未签名 = 没有 app-group，读得到
        (_cp(b"", b"/x: some other error\n", rc=1), None),                      # 其它报错 = 读不到
        (_cp(b"", rc=0), []),                                                   # 无权限声明
        (_cp(plistlib.dumps({"a": 1})), []),                                    # 有权限声明但没有 app-group
        (_cp(b"junk header\n" + _ent(["g1", 7, ""])), ["g1"]),                   # 前面带杂字节也能解析，非字符串丢弃
    ]
    for i, (res, want) in enumerate(cases):
        leftovers._APP_GROUP_CACHE.clear()
        monkeypatch.setattr(leftovers.subprocess, "run", run_with(res))
        assert REAL_READ_APP_GROUPS("/Applications/X%d.app" % i) == want, i


# ───────────────────────── 第 4 条：Group Containers ─────────────────────────

@pytest.fixture
def genv(tmp_path, monkeypatch, clean_cache):
    """真的 _read_app_groups，subprocess.run 用 env.codesign 字典假装：路径 -> 结果（bytes / 异常）。"""
    env = test_leftovers.Env(tmp_path, monkeypatch)
    env.tmp = tmp_path
    monkeypatch.setattr(leftovers, "_read_app_groups", REAL_READ_APP_GROUPS)
    env.codesign = {}
    env.codesign_calls = []

    def fake_run(cmd, **kw):
        assert cmd[0] == "codesign"
        env.codesign_calls.append(cmd[-1])
        res = env.codesign.get(cmd[-1], _cp(b""))
        if isinstance(res, Exception):
            raise res
        return res
    monkeypatch.setattr(leftovers.subprocess, "run", fake_run)
    return env


def _shortcuts(env):
    env.shortcuts = str(env.tmp / "System" / "Applications" / "Shortcuts.app")   # 夹具路径，不碰真实系统 App
    env.install_raw([{"path": env.shortcuts, "bundle_id": "com.apple.shortcuts",
                      "name": "Shortcuts", "version": "1", "short_version": "1"}])


def test_system_app_group_owns_group_container(genv):
    _shortcuts(genv)
    genv.codesign[genv.shortcuts] = _cp(_ent(["group.is.workflow.shortcuts"]))
    genv.lib("Group Containers", "group.is.workflow.shortcuts")
    res = genv.probe()
    assert res["candidates"] == [] and res["skipped"] == {"app_installed:app_group": 1}
    assert res["group_containers_evaluated"] is True
    assert genv.codesign_calls == [genv.shortcuts]


def test_group_container_with_no_owner_is_listed_when_all_signatures_readable(genv):
    _shortcuts(genv)
    genv.codesign[genv.shortcuts] = _cp(_ent(["group.is.workflow.shortcuts"]))
    genv.lib("Group Containers", "group.com.gone.other")
    res = genv.probe()
    assert names(res) == ["com.gone.other"] and res["group_containers_evaluated"] is True


def names(res):
    return sorted(c["name"] for c in res["candidates"])


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("codesign", 5), FileNotFoundError("codesign"),
                                     _cp(b"", b"boom", rc=1)])
def test_unreadable_app_group_makes_whole_location_not_evaluated(genv, failure):
    _shortcuts(genv)
    good = str(genv.tmp / "Good.app")
    genv.install_raw([
        {"path": good, "bundle_id": "org.example.good", "name": "Good", "version": "1", "short_version": "1"},
        {"path": genv.shortcuts, "bundle_id": "com.apple.shortcuts", "name": "Shortcuts",
         "version": "1", "short_version": "1"}])
    genv.codesign[good] = _cp(_ent(["group.com.good"]))
    genv.codesign[genv.shortcuts] = failure
    genv.lib("Group Containers", "group.is.workflow.shortcuts")
    genv.lib("Group Containers", "group.com.gone.other")
    genv.lib("Caches", "com.gone.cache")                            # 其它位置照常评估
    res = genv.probe()
    assert names(res) == ["com.gone.cache"]
    assert res["skipped"] == {"group_containers_not_evaluated": 2}
    assert res["group_containers_evaluated"] is False
    gc = [x for x in res["not_evaluated_locations"] if x["location"] == "~/Library/Group Containers"]
    assert len(gc) == 1 and "app-group" in gc[0]["reason"]
    assert any(e.get("stage") == "app_groups" for e in res["errors"])


def test_extension_app_group_counts(genv, tmp_path):
    app = tmp_path / "Ext.app"
    appex = app / "Contents" / "PlugIns" / "Widget.appex"
    appex.mkdir(parents=True)
    genv.install_raw([{"path": str(app), "bundle_id": "org.example.ext", "name": "Ext", "version": "1",
                       "short_version": "1"}])
    genv.codesign[str(app)] = _cp(b"")
    genv.codesign[str(appex)] = _cp(_ent(["group.com.gone.widgetdata"]))
    genv.lib("Group Containers", "group.com.gone.widgetdata")
    res = genv.probe()
    assert res["candidates"] == [] and res["skipped"] == {"app_installed:app_group": 1}


def test_no_group_container_dirs_means_no_codesign_calls(genv):
    _shortcuts(genv)
    genv.lib("Caches", "com.gone.cache")
    res = genv.probe()
    assert names(res) == ["com.gone.cache"] and genv.codesign_calls == []
    assert res["group_containers_evaluated"] is None


def test_app_group_read_does_not_mutate_scan_inventory(genv):
    _shortcuts(genv)
    genv.codesign[genv.shortcuts] = _cp(_ent(["group.x"]))
    genv.lib("Group Containers", "group.com.gone.other")
    genv.probe()
    import scan
    assert "app_groups" not in scan.installed_apps()["apps"][0]


# ───────────────────────── 第 4 条：LaunchAgents ─────────────────────────

@pytest.fixture
def lenv(tmp_path, monkeypatch):
    env = test_leftovers.Env(tmp_path, monkeypatch)
    env.tmp = tmp_path
    return env


def test_launch_agent_whose_program_exists_is_in_use(lenv):
    exe = lenv.tmp / "panel"
    exe.write_text("#!/bin/sh\n")
    lenv.launch_agent("top.sansheng.deskdash.panel", args=[str(exe), "--x"])
    lenv.launch_agent("top.sansheng.deskdash.other", program=str(exe))        # Program 优先
    res = lenv.probe()
    assert res["candidates"] == []
    assert res["skipped"] == {"launch_agent_in_use:program_exists": 2}


def test_launch_agent_interpreter_uses_script_path(lenv):
    script = lenv.tmp / "run.sh"
    script.write_text("echo\n")
    lenv.launch_agent("com.gone.alive.agent", args=["/bin/bash", str(script)])
    lenv.launch_agent("com.gone.dead.agent", args=["/bin/bash", "/nonexistent/run.sh"])
    res = lenv.probe()
    assert names(res) == ["com.gone.dead.agent"]
    assert res["skipped"] == {"launch_agent_in_use:program_exists": 1}


def test_launch_agent_loaded_in_launchd_is_in_use_even_if_program_missing(lenv):
    lenv.launch_agent("com.gone.loaded.agent", program="/nonexistent/x", label="real.label.loaded")
    lenv.launchctl_loaded = {"real.label.loaded"}                  # 用 plist 里的 Label，不是文件名
    res = lenv.probe()
    assert res["candidates"] == [] and res["skipped"] == {"launch_agent_in_use:launchctl_loaded": 1}


def test_launch_agent_gone_and_not_loaded_is_listed(lenv):
    lenv.launch_agent("com.gone.dead.agent", program="/nonexistent/x")
    res = lenv.probe()
    assert names(res) == ["com.gone.dead.agent"] and res["candidates"][0]["location"] == "LaunchAgents"


def test_launch_agent_inconclusive_checks_lean_towards_in_use(lenv):
    lenv.launch_agent("com.gone.timeout.agent", program="/nonexistent/x")
    lenv.launchctl_fail = True
    res = lenv.probe()
    assert res["candidates"] == [] and res["skipped"] == {"launch_agent_in_use:launchctl_inconclusive": 1}
    lenv.libfile("LaunchAgents", "com.gone.garbage.agent.plist")     # 内容不是 plist
    res = lenv.probe()                                               # launchctl 仍超时，plist 坏的那个也不报
    assert res["candidates"] == [] and res["skipped"] == {"launch_agent_in_use:launchctl_inconclusive": 1,
                                                          "launch_agent_in_use:plist_unreadable": 1}


def test_launchctl_loaded_command_shape_and_exit_codes(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return _cp(rc=0 if cmd[-1].endswith("/yes") else 113)
    monkeypatch.setattr(leftovers.subprocess, "run", fake_run)
    assert REAL_LAUNCHCTL_LOADED("yes") is True
    assert REAL_LAUNCHCTL_LOADED("no") is False
    cmd, kw = calls[0]
    assert cmd == ["launchctl", "print", "gui/%d/yes" % os.getuid()] and kw["timeout"] == 5
    monkeypatch.setattr(leftovers.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("launchctl", 5)))
    with pytest.raises(subprocess.TimeoutExpired):
        REAL_LAUNCHCTL_LOADED("slow")
