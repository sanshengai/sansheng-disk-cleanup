"""读取授权闸门（peek.py）：分类、按 read_scope 给内容还是元信息、never 一律拒、reads.jsonl 留痕。

夹具全在 tmp_path 内：HOME、protect.HOME、profile、运行目录都指向临时目录，不触碰真实家目录。
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import peek  # noqa: E402
import protect  # noqa: E402


class Env:
    def __init__(self, home, run):
        self.home = Path(home)
        self.run = Path(run)

    def path(self, rel):
        return self.home / rel

    def scope(self, *values):
        f = self.home / ".sansheng" / "disk-cleanup" / "profile.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"read_scope": list(values)}), encoding="utf-8")

    def read(self, *argv):
        return peek.main(["--run", str(self.run), *argv])

    def records(self):
        f = self.run / "reads.jsonl"
        if not f.exists():
            return []
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    (home / "Documents").mkdir()
    run = (tmp_path / "runs" / "scan1").resolve()
    run.mkdir(parents=True)
    monkeypatch.setattr(protect, "HOME", str(home))
    monkeypatch.setattr(protect, "STATE_DIR", str(home / ".sansheng" / "disk-cleanup"))
    monkeypatch.setattr(protect, "PROFILE", str(home / ".sansheng" / "disk-cleanup" / "profile.json"))
    return Env(home, run)


def mkfile(env, rel, data=b"x\n"):
    p = env.path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def cls(env, rel):
    return peek.classify(str(env.path(rel)), home=str(env.home))


# ---------------------------------------------------------------- 分类：never

@pytest.mark.parametrize("rel", [
    ".ssh/id_rsa",                                              # SSH 私钥
    ".ssh/id_ed25519",
    ".gnupg/private-keys-v1.d/abc.key",
    "Library/Keychains/login.keychain-db",                      # 钥匙串
    "backups/old.keychain",
    "proj/.env",                                                # 环境变量里的令牌
    "proj/.env.local",
    "proj/server.pem",
    "dev/app.p12",
    "Library/Application Support/Google/Chrome/Default/Login Data",
    "Library/Application Support/Firefox/Profiles/x/logins.json",
    ".netrc",
    ".aws/credentials",
    ".config/gh/hosts.yml",
    ".codex/auth.json",
    ".claude/.credentials.json",
    "Library/Application Support/Bitwarden/data.json",           # 密码管理器数据目录
    "Library/Application Support/1Password/token",
    "Documents/我的密码.txt",
])
def test_never_examples(env, rel):
    assert cls(env, rel) == "never"


def test_never_by_file_name_is_deliberately_conservative(env):
    # 文件名里带 password 就算凭证：哪怕它看上去只是一篇评测稿，也按更严的方向挡（有意的保守）
    assert cls(env, "notes/password-manager-review.md") == "never"
    assert cls(env, "Documents/密码策略.md") == "never"
    assert cls(env, "src/passwordless-login.md") == "never"


def test_never_boundaries_that_are_not_credentials(env):
    assert cls(env, ".envelope") == "basic"          # 不是 .env，别误伤
    assert cls(env, ".envrc") == "never"             # direnv 的 .envrc 常 export 密钥：2026-10 复核起按凭证挡（原先判 basic）
    assert cls(env, "Documents/envoy-notes.md") == "basic"
    assert cls(env, "Library/Keychains2/readme.txt") == "basic"    # 只是名字前缀像
    assert cls(env, "Documents/monkey.txt") == "basic"


def test_never_beats_other_classes(env):
    # 照片图库按 extended、png 按 deep，但里面躺着一个 .key 就得按 never 挡
    assert cls(env, "Pictures/Photos Library.photoslibrary/originals/0/vault.key") == "never"
    assert cls(env, "Library/Application Support/1Password/photo.png") == "never"


# ---------------------------------------------------------------- 分类：deep

@pytest.mark.parametrize("rel", [
    "Library/Containers/com.tencent.xinWeChat/Data/Documents/2.0b4.0.9/abc/Message/msg_0.db",
    "Library/Containers/com.tencent.xinWeChat/Data/Documents/2.0b4.0.9/abc/db_storage/session.db",
    "Library/Messages/chat.db",                                   # iMessage / 短信
    "Library/Mail/V10/ABC-DEF/Data/Messages/1.emlx",              # 邮件正文
    "Library/Application Support/Telegram/postbox/db/db_sqlite",   # （Telegram Desktop 的 tdata 目录已升为 never，见 test_review2_fixes）
    "Library/Containers/com.tencent.qq/Data/Documents/nt_qq_1234/nt_msg.db",
    "Downloads/photo.png",                                        # 图片内容
    "Downloads/scan.heic",
])
def test_deep_examples(env, rel):
    assert cls(env, rel) == "deep"


# ---------------------------------------------------------------- 分类：extended

@pytest.mark.parametrize("rel", [
    "Library/Mail/V10/ABC/Data/Attachments/1/a/photo.jpg",        # 邮件附件
    "Library/Containers/com.tencent.xinWeChat/Data/Documents/2.0b4.0.9/abc/msg/file/报告.pdf",
    "Library/Containers/com.tencent.xinWeChat/Data/Documents/2.0b4.0.9/abc/msg/video/a.mp4",
    "Pictures/Photos Library.photoslibrary/originals/0/abc.heic",  # 照片图库
    "Desktop/截屏2026-10-12 13.00.00.png",                          # 截图
    "Pictures/Screenshots/Screenshot 2026-01-01 at 10.00.png",
])
def test_extended_examples(env, rel):
    assert cls(env, rel) == "extended"


# ---------------------------------------------------------------- 分类：basic 与边界

@pytest.mark.parametrize("rel", ["Downloads/report.csv", "Documents/notes.md", "work/app.py"])
def test_basic_examples(env, rel):
    assert cls(env, rel) == "basic"


def test_screenshot_beats_generic_image_rule(env):
    # 截图即使也是图片，也按 extended（能看到文件名就够了），不升级成 deep
    assert cls(env, "Desktop/Screenshot 2026-01-01.png") == "extended"


def test_chat_file_beats_generic_image_rule(env):
    assert cls(env, "Library/Containers/com.tencent.xinWeChat/Data/Documents/x/msg/file/a.png") == "extended"


def test_messages_directory_is_all_deep(env):
    assert cls(env, "Library/Messages/Attachments/1/x.jpg") == "deep"


def test_mail_directory_without_attachments_is_basic(env):
    assert cls(env, "Library/Mail/V10/ABC/Data/notes.txt") == "basic"


def test_name_like_chat_is_not_chat(env):
    assert cls(env, "Downloads/message.txt") == "basic"
    assert cls(env, "Downloads/db_storage.txt") == "basic"


def test_outside_home(env):
    outside = env.home.parent / "outside"
    assert peek.classify(str(outside / "photo.jpg"), home=str(env.home)) == "deep"     # 图片内容一样按 deep
    assert peek.classify(str(outside / "notes.md"), home=str(env.home)) == "basic"
    assert peek.classify(str(outside / "passwords.txt"), home=str(env.home)) == "never"  # 文件名规则不看家目录


def test_case_insensitive(env):
    assert cls(env, ".SSH/ID_RSA") == "never"
    assert cls(env, "library/messages/chat.db") == "deep"
    assert cls(env, "desktop/screenshot 1.PNG") == "extended"


# ---------------------------------------------------------------- 内容与元信息

def test_content_default_lines_cap_and_byte_cap(env, capsys):
    p = mkfile(env, "Documents/notes.md", "".join(f"line{i:03d}\n" for i in range(1, 301)).encode())
    assert env.read(str(p)) == 0
    out = capsys.readouterr()
    assert len(out.out.splitlines()) == 200 and out.out.startswith("line001\n")
    assert "只给前 200 行" in out.err

    assert env.read(str(p), "--lines", "5") == 0
    out = capsys.readouterr()
    assert out.out == "line001\nline002\nline003\nline004\nline005\n"
    assert "只给前 5 行" in out.err

    assert env.read(str(p), "--bytes", "12") == 0
    out = capsys.readouterr()
    assert out.out == "line001\nline\n" and "只给前 12 字节" in out.err   # 截断后结尾补一个换行


def test_meta_mode_gives_json_only(env, capsys):
    p = mkfile(env, "Documents/notes.md", b"# hello\n")
    assert env.read(str(p), "--meta") == 0
    out = capsys.readouterr()
    meta = json.loads(out.out)
    assert set(meta) == {"name", "bytes", "kind", "mtime"}
    assert meta["name"] == "notes.md" and meta["bytes"] == 8 and meta["kind"] == "文本"
    want = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert meta["mtime"] == want and "hello" not in out.out


def test_binary_with_nul_gives_meta_only(env, capsys):
    p = mkfile(env, "Documents/blob.bin", b"\x00\x01\x02\x03")
    assert env.read(str(p), "--bytes", "65536") == 0
    out = capsys.readouterr()
    meta = json.loads(out.out)
    assert meta["name"] == "blob.bin" and meta["kind"] == "其他"
    assert env.records()[-1]["mode"] == "content"        # 授权范围内确实读了内容，只是二进制不上屏


def test_non_utf8_gives_meta_only(env, capsys):
    p = mkfile(env, "Documents/old.dat", b"\x80\x81\x82\x83\xfe\xff")
    assert env.read(str(p)) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["bytes"] == 6
    assert env.records()[-1]["mode"] == "content"


def test_directory_gives_meta_with_children_and_hides_credentials(env, capsys):
    d = env.path("Documents/项目")
    mkfile(env, "Documents/项目/notes.txt")
    mkfile(env, "Documents/项目/.env")
    mkfile(env, "Documents/项目/sub/passwords.txt")
    env.scope("basic", "extended", "deep")
    assert env.read(str(d)) == 0
    meta = json.loads(capsys.readouterr().out)
    assert meta["kind"] == "目录"
    assert ".env" not in meta["children"] and "passwords.txt" not in "".join(meta["children"])
    assert peek.HIDDEN_CHILD in meta["children"] and "notes.txt" in meta["children"]
    assert env.records()[-1]["mode"] == "meta"           # 目录只给元信息


def test_directory_children_capped_at_50(env, capsys):
    d = env.path("Documents/many")
    d.mkdir(parents=True)
    for i in range(60):
        (d / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
    assert env.read(str(d)) == 0
    assert len(json.loads(capsys.readouterr().out)["children"]) == 50


# ---------------------------------------------------------------- read_scope 三档

def test_read_scope_combinations(env):
    txt = mkfile(env, "Documents/notes.md", b"hello\n")           # basic
    shot = mkfile(env, "Desktop/截屏.png", b"\x89PNG\r\n")         # extended
    img = mkfile(env, "Downloads/photo.png", b"\x89PNG\r\n")       # deep
    assert [peek.classify(str(x)) for x in (txt, shot, img)] == ["basic", "extended", "deep"]

    env.scope("basic")
    for f, want in ((txt, "content"), (img, "meta")):
        assert env.read(str(f)) == 0
        assert env.records()[-1]["mode"] == want
    # 没授权“扩展”：截图、聊天收到的文件连名字都不给（退出码 3，仍记一行 refused）
    assert env.read(str(shot)) == 3
    assert env.records()[-1]["mode"] == "refused"

    # 授权“扩展”：只看名字、大小、类型、日期，不看内容
    env.scope("basic", "extended")
    for f, want in ((txt, "content"), (shot, "meta"), (img, "meta")):
        assert env.read(str(f)) == 0
        assert env.records()[-1]["mode"] == want

    env.scope("basic", "extended", "deep")
    for f in (txt, shot, img):
        assert env.read(str(f)) == 0
        assert env.records()[-1]["mode"] == "content"


def test_missing_profile_defaults_to_basic(env):
    txt = mkfile(env, "Documents/notes.md", b"hello\n")
    img = mkfile(env, "Downloads/photo.png", b"\x89PNG\r\n")
    assert peek.read_scope() == ["basic"]
    assert env.read(str(txt)) == 0 and env.records()[-1]["mode"] == "content"
    assert env.read(str(img)) == 0 and env.records()[-1]["mode"] == "meta"


def test_empty_scope_gives_metadata_only(env):
    txt = mkfile(env, "Documents/notes.md", b"hello\n")
    env.scope()
    assert peek.read_scope() == []
    assert env.read(str(txt)) == 0
    assert env.records()[-1]["mode"] == "meta"


def test_unknown_scope_values_ignored(env):
    env.scope("basic", "全部", "deep")
    assert peek.read_scope() == ["basic", "deep"]


def test_meta_flag_wins_over_scope(env):
    img = mkfile(env, "Downloads/photo.png", b"\x89PNG\r\n")
    env.scope("basic", "extended", "deep")
    assert env.read(str(img), "--meta") == 0
    assert env.records()[-1]["mode"] == "meta"


# ---------------------------------------------------------------- never 拒绝

def test_never_refused_exit_3_and_recorded(env, capsys):
    p = mkfile(env, "proj/.env", b"TOKEN=abc\n")
    assert env.read(str(p)) == 3
    out = capsys.readouterr()
    assert out.out == "" and "拒绝" in out.err and "绝不读" in out.err
    rows = env.records()
    assert len(rows) == 1
    assert rows[0] == {"at": rows[0]["at"], "path": "~/proj/.env", "class": "never", "mode": "refused"}


def test_never_refused_even_with_meta(env, capsys):
    p = mkfile(env, ".ssh/id_rsa", b"PRIVATE KEY\n")
    env.scope("basic", "extended", "deep")
    assert env.read(str(p), "--meta") == 3 and "拒绝" in capsys.readouterr().err


def test_never_symlink_is_caught(env, capsys):
    target = mkfile(env, ".ssh/id_rsa", b"PRIVATE KEY\n")
    link = env.path("Documents/innocent.txt")
    link.symlink_to(target)
    assert env.read(str(link)) == 3 and "拒绝" in capsys.readouterr().err


# ---------------------------------------------------------------- reads.jsonl 与 --summary

def test_reads_jsonl_records_home_relative_path(env):
    p = mkfile(env, "Documents/notes.md", b"hello\n")
    assert env.read(str(p)) == 0
    assert env.read(str(p), "--meta") == 0
    rows = env.records()
    assert [set(r) for r in rows] == [{"at", "path", "class", "mode"}] * 2
    assert rows[0] == {"at": rows[0]["at"], "path": "~/Documents/notes.md", "class": "basic", "mode": "content"}
    assert rows[1]["mode"] == "meta" and rows[1]["path"] == "~/Documents/notes.md"
    assert rows[0]["at"].endswith("Z") and len(rows[0]["at"]) == 20


def test_reads_jsonl_keeps_absolute_path_outside_home(env):
    outside = env.home.parent / "outside.txt"
    outside.write_text("hi\n", encoding="utf-8")
    assert env.read(str(outside)) == 0
    assert env.records()[0]["path"] == str(outside)


def test_summary_counts_and_paths(env, capsys):
    p = mkfile(env, "Documents/notes.md", b"hello\n")
    env.scope("basic")
    assert env.read(str(p)) == 0 and env.read(str(p)) == 0
    assert env.read(str(p), "--meta") == 0
    assert env.read(str(env.path("proj/.env"))) == 3
    capsys.readouterr()
    assert peek.main(["--summary", "--run", str(env.run)]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got == {"content": 2, "meta": 1, "refused": 1, "paths": ["~/Documents/notes.md"]}


def test_summary_without_reads_file(env, capsys):
    assert peek.main(["--summary", "--run", str(env.run)]) == 0
    assert json.loads(capsys.readouterr().out) == {"content": 0, "meta": 0, "refused": 0, "paths": []}


def test_summary_caps_paths_at_50(env, capsys):
    for i in range(55):
        mkfile(env, f"Documents/f{i:02d}.txt", b"x\n")
    for i in range(55):
        assert env.read(str(env.path(f"Documents/f{i:02d}.txt"))) == 0
    capsys.readouterr()
    assert peek.main(["--summary", "--run", str(env.run)]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["content"] == 55 and len(got["paths"]) == 50


# ---------------------------------------------------------------- 参数与错误

def test_missing_run_dir_exit_2(env, capsys):
    p = mkfile(env, "Documents/notes.md", b"hello\n")
    assert peek.main(["--run", str(env.run / "nope"), str(p)]) == 2
    assert "运行目录" in capsys.readouterr().err


def test_missing_file_exit_2(env, capsys):
    assert env.read(str(env.path("Documents/nope.md"))) == 2
    assert "找不到" in capsys.readouterr().err
    assert env.records() == []


def test_path_required_exit_2(env, capsys):
    assert env.read() == 2 and "读哪个文件" in capsys.readouterr().err


def test_summary_rejects_path(env, capsys):
    assert env.read("--summary", str(env.path("Documents"))) == 2
    assert "不用给路径" in capsys.readouterr().err


def test_read_does_not_modify_profile(env):
    txt = mkfile(env, "Documents/notes.md", b"hello\n")
    mkfile(env, "proj/.env", b"TOKEN=1\n")
    env.scope("basic")
    f = env.home / ".sansheng" / "disk-cleanup" / "profile.json"
    before = f.read_bytes()
    assert env.read(str(txt)) == 0 and env.read(str(env.path("proj/.env"))) == 3
    assert f.read_bytes() == before
