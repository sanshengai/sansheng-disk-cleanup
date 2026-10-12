#!/usr/bin/env python3
"""叁笙清理：读取授权闸门（peek.py）。

Agent 要读用户的文件，一律先走这里，做三件事：
1) 分类：never（凭证、密钥、登录信息，永远不读）/ deep（聊天与邮件正文、图片内容）/
   extended（聊天收到的文件、邮件附件、照片图库、截图）/ basic（其余）。
2) 授权：按 profile 的 read_scope 决定给内容还是只给元信息；--meta 一律只给元信息；
   never 一律拒（退出码 3）。设置只决定“进哪一档”，不能把 never 放行（安全不变式里唯一的更严方向）。
3) 留痕：每次调用往 <run>/reads.jsonl 追加一行，成果页据此告诉用户“这次读了哪些文件”。

    python3 scripts/peek.py --run <运行目录> <路径> [--lines 200] [--bytes 65536] [--meta]
    python3 scripts/peek.py --summary --run <运行目录>
"""
import argparse
import fnmatch
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import protect  # noqa: E402

SCOPES = ("basic", "extended", "deep")
CHILD_CAP = 50            # 目录只列前 50 个子项名（不数总数、不算总大小）
SUMMARY_PATH_CAP = 50     # 成果页只展示前 50 条读过内容的路径
HIDDEN_CHILD = "（凭证类，已隐去）"


# ---------------------------------------------------------------- 分类规则（常量表，按 never > extended > deep > basic 匹配）

# 凭证、密钥、登录信息：任何设置都不能读。目录规则管目录本身及其下全部。
NEVER_DIRS = ("Library/Keychains", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".config/gcloud", ".config/gh",
              ".config/op", ".config/github-copilot")
NEVER_FILES = (".netrc", ".npmrc", ".pypirc", ".git-credentials", ".docker/config.json",
               ".codex/auth.json", ".claude/.credentials.json", ".gemini/oauth_creds.json")
# 家目录相对路径通配（小写；fnmatch 的 * 可跨 /）：Agent 配置里常有 API key / token，shell 配置里常有 export 的密钥，
# shell 历史里常有手敲的密钥，一律挡掉（宁可多挡）。
NEVER_REL_GLOBS = (
    ".claude.json*", ".claude/settings*.json", ".codex/*.toml", ".cursor/mcp.json", ".gemini/settings*.json",
    ".cargo/credentials*", ".config/*/config", ".config/*/config.*",
    ".zshrc", ".zshrc.*", ".bashrc", ".bashrc.*", ".zprofile", ".zprofile.*", ".bash_profile", ".bash_profile.*",
    ".profile", ".profile.*", ".zshenv", ".zshenv.*", ".zlogin", ".zlogout", ".bash_login", ".bash_logout",
    ".zsh_history*", ".bash_history*",
    ".android/adbkey*", "library/application support/claude/claude_desktop_config.json",
    ".m2/settings.xml", ".gradle/gradle.properties",
)
NEVER_NAME_GLOBS = ("*.keychain*", "*.pem", "*.key", "*.p12", "*.kdbx",
                    "Login Data*", "Cookies*",
                    "Web Data", "key4.db", "logins.json",
                    "*.ppk", "*.pfx", "*.jks", "*.keystore",                 # 私钥 / 证书库 / Java 密钥库
                    ".npmrc", ".envrc", ".mcp.json", "*mcp*.json",           # 任意目录的项目级令牌与 MCP 配置
                    "local state", "state.vscdb*", "wallet.dat",             # 浏览器 / 编辑器 / 钱包的登录态
                    ".pgpass", ".my.cnf", ".s3cfg", ".boto", ".terraformrc", "*.tfstate*",
                    "service-account*.json", "service_account*.json")
# id_* 私钥（id_ecdsa / id_dsa / 任意 id_xxx）；*.pub 公钥不算
NEVER_NAME_PREFIX_EXCEPT = (("id_", ".pub"),)
# 路径里任一段命中就是登录态存储（Local Storage / Session Storage / IndexedDB / Telegram tdata）
NEVER_PATH_SEGS = frozenset(("local storage", "session storage", "indexeddb", "tdata"))
# 切词后相邻词对：api_key.txt / API-KEY.txt / private_key.json 等
NEVER_TOKEN_PAIRS = frozenset((("api", "key"), ("private", "key"), ("access", "key"), ("secret", "key")))
NEVER_NAME_WORDS = ("password", "密码", "凭证", "密钥")        # 文件名里出现就按凭证处理（宁可多挡；中文无词界，按子串）
# 文件名按「词」匹配：把名字（含扩展名）按非字母数字、驼峰边界、字母与数字边界切成词，词在下面这组里就按凭证处理。
# 例：auth.json、my-token.txt、OAuthClient.json、client_secret_123.json、access.token 命中；
# author.md、authority.txt、tokenizer.json、secretary.docx、authentic.md 不命中（整词才算，不是任意子串）。
NEVER_NAME_TOKENS = frozenset(("credential", "credentials", "secret", "secrets", "token", "tokens",
                               "oauth", "auth", "passwd", "apikey"))
NEVER_PATH_WORDS = ("1password", "bitwarden", "keepass")      # 密码管理器的数据目录
_ENV_NAME = re.compile(r"^\.env(\..+)?$")                     # .env / .env.local；~/.envelope 不算
_CAMEL_A = re.compile(r"([a-z0-9])([A-Z])")                   # fooBar -> foo Bar
_CAMEL_B = re.compile(r"([A-Z]+)([A-Z][a-z])")                # 连续大写后接驼峰：XMLHttp -> XML Http
_WORD = re.compile(r"[a-z]+|[0-9]+")


def name_tokens(name):
    """文件名切词（小写）。传入要保留原始大小写，驼峰才切得开：'OAuthClient.json' -> oauth/client/json。"""
    spaced = _CAMEL_B.sub(r"\1 \2", _CAMEL_A.sub(r"\1 \2", name or ""))
    return _WORD.findall(spaced.lower())


def has_credential_word(name):
    """文件名里有没有整词的 credential/secret/token/oauth/auth。"""
    toks = name_tokens(name)
    if any(t in NEVER_NAME_TOKENS for t in toks):
        return True
    return any(pair in NEVER_TOKEN_PAIRS for pair in zip(toks, toks[1:]))


# 聊天软件收到的文件、邮件附件、照片图库、截图：能看到文件名与大小就够了。
EXTENDED_MAIL_DIR = "library/mail"                            # Mail 下任一层叫 Attachments 的目录
EXTENDED_MAIL_SEG = "attachments"
EXTENDED_SEG_PAIRS = (("msg", "file"), ("msg", "video"))      # 微信 msg/file、msg/video
EXTENDED_SEG_SUFFIX = (".photoslibrary",)                     # 照片图库（任一层目录名以它结尾）
EXTENDED_SHOT_DIRS = ("desktop", "pictures/screenshots")      # 截图：桌面与 ~/Pictures/Screenshots
EXTENDED_SHOT_PREFIX = ("截屏", "screenshot")

# 聊天与邮件正文、图片内容。
DEEP_DIRS = ("library/messages",)                             # iMessage / 短信库
DEEP_MAIL_DIR = "library/mail"
DEEP_MAIL_SUFFIX = (".emlx",)
DEEP_SEG_PAIRS = (("com.tencent.xinwechat", "message"),        # 微信正文
                  ("com.tencent.xinwechat", "db_storage"),     # 微信数据库
                  ("nt_qq_", "nt_msg"),                        # QQ 正文
                  ("postbox", "db"))                           # Telegram
IMAGE_EXTS = ("png", "jpg", "jpeg", "heic", "webp")            # 图片内容按 deep 管

KIND_EXTS = (
    ("文本", ("txt", "md", "markdown", "json", "jsonl", "csv", "tsv", "log", "conf", "cfg", "ini",
              "xml", "yml", "yaml", "toml", "sql", "srt", "vtt",
              "py", "js", "mjs", "ts", "tsx", "jsx", "html", "htm", "css", "scss", "sh", "bash", "zsh",
              "rs", "go", "java", "kt", "c", "h", "cpp", "swift", "rb", "php", "pl", "lua", "tex")),
    ("图片", ("png", "jpg", "jpeg", "heic", "webp", "gif", "tif", "tiff", "bmp", "svg")),
    ("视频", ("mp4", "mov", "m4v", "avi", "mkv", "webm", "flv", "wmv")),
    ("音频", ("mp3", "m4a", "wav", "aac", "flac", "ogg", "aiff")),
    ("压缩包", ("zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar")),
    ("安装包", ("dmg", "pkg", "mpkg", "exe", "msi", "apk", "deb", "rpm", "iso")),
    ("文档", ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "pages", "numbers", "key", "epub")),
)


def _under(rel, sub):
    """rel 是 sub 本身或在其下（都已是小写的家目录相对路径）。"""
    return rel == sub or rel.startswith(sub + "/")


def _has_pair(segs, a, b):
    """路径段里先出现以 a 开头的一段，后面再出现以 b 开头的一段。"""
    for i, s in enumerate(segs):
        if s.startswith(a) and any(t.startswith(b) for t in segs[i + 1:]):
            return True
    return False


def _home_rel(path, home):
    """家目录相对路径（小写，无前后斜杠）；不在家目录下返回 None。"""
    home = home.rstrip("/") or "/"
    if path == home:
        return ""
    if path.startswith(home + "/"):
        return path[len(home) + 1:].lower()
    return None


def _never(rel, segs, name, orig_name=None):
    if rel is not None:
        if any(_under(rel, d.lower()) for d in NEVER_DIRS) or rel in NEVER_FILES:
            return True
        if any(fnmatch.fnmatchcase(rel, g.lower()) for g in NEVER_REL_GLOBS):
            return True
    if has_credential_word(orig_name if orig_name is not None else name):
        return True
    if _ENV_NAME.match(name):
        return True
    if any(name.startswith(pre) and not name.endswith(exc) for pre, exc in NEVER_NAME_PREFIX_EXCEPT):
        return True
    if any(sg in NEVER_PATH_SEGS for sg in segs):
        return True
    if any(fnmatch.fnmatchcase(name, g.lower()) for g in NEVER_NAME_GLOBS):
        return True
    if any(w in name for w in NEVER_NAME_WORDS):
        return True
    return any(w in s for s in segs for w in NEVER_PATH_WORDS)


def _extended(rel, segs, name):
    if rel is None:
        return False
    if _under(rel, EXTENDED_MAIL_DIR) and EXTENDED_MAIL_SEG in segs:
        return True
    if any(_has_pair(segs, a, b) for a, b in EXTENDED_SEG_PAIRS):
        return True
    if any(s.endswith(EXTENDED_SEG_SUFFIX) for s in segs):
        return True
    return any(_under(rel, d) for d in EXTENDED_SHOT_DIRS) and name.startswith(EXTENDED_SHOT_PREFIX)


def _deep(rel, segs, name):
    if rel is not None:
        if any(_under(rel, d) for d in DEEP_DIRS):
            return True
        if _under(rel, DEEP_MAIL_DIR) and name.endswith(DEEP_MAIL_SUFFIX):
            return True
    if any(_has_pair(segs, a, b) for a, b in DEEP_SEG_PAIRS):
        return True
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return ext in IMAGE_EXTS


def classify(path, home=None):
    """给一个路径定档：never / deep / extended / basic。先取真实路径，再按家目录相对路径与文件名匹配（不区分大小写）。
    家目录外的路径也照文件名规则走（图片、密码管理器这类不看家目录）。"""
    home = os.path.realpath(home or protect.HOME).lower()
    real_orig = os.path.realpath(os.path.expanduser(str(path)))
    real = real_orig.lower()
    rel = _home_rel(real, home)
    segs = [s for s in real.strip("/").split("/") if s]
    name = os.path.basename(real)
    if _never(rel, segs, name, os.path.basename(real_orig)):
        return "never"
    if _extended(rel, segs, name):
        return "extended"
    if _deep(rel, segs, name):
        return "deep"
    return "basic"


# ---------------------------------------------------------------- 读授权与输出

def read_scope(profile=None):
    """profile.read_scope（只读，不写）。没这一项或缺省给 ["basic"]；给了空列表 = 只读文件名，不给内容。"""
    if profile is None:
        profile = protect.load_profile(protect.PROFILE)
    raw = profile.get("read_scope") if isinstance(profile, dict) else None
    if not isinstance(raw, list):
        return ["basic"]
    return [x for x in raw if x in SCOPES]


def decide(cls, scope, meta_only=False):
    """never 一律拒；extended 授权“扩展”只给名字、大小、类型、日期（计划第 2 节：扩展档不看内容），授权“深度”才给内容，都没授权连名字都不给；
    deep 授权了才给内容，没授权只给元信息；basic 在 scope 里给内容，否则只给元信息。"""
    if cls == "never":
        return "refused"
    if cls == "extended":                # 授权“深度”才看内容（判断截图、重复照片要看图）
        if "deep" in scope and not meta_only:
            return "content"
        return "meta" if ("extended" in scope or "deep" in scope) else "refused"
    if meta_only or cls not in scope:
        return "meta"
    return "content"


def display_path(path, home=None):
    """给人看与记进 reads.jsonl 的路径：家目录下写成 ~/…。"""
    home = os.path.realpath(home or protect.HOME).rstrip("/") or "/"
    path = str(path)
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def kind_of(path):
    """只按扩展名给一个普通用户能懂的类型（meta 模式不读内容，不能靠内容判断）。"""
    name = os.path.basename(str(path)).lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    for k, exts in KIND_EXTS:
        if ext in exts:
            return k
    return "其他"


def children(path, home=None, cap=CHILD_CAP):
    """前 cap 个子项名；never 类的子项连名字也不给（名字本身也是凭证线索）。"""
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return []
    return [HIDDEN_CHILD if classify(os.path.join(path, n), home=home) == "never" else n
            for n in names[:cap]]


def build_meta(path, home=None):
    st = os.stat(path)
    if os.path.isdir(path):
        return {"name": os.path.basename(str(path).rstrip("/")) or str(path), "bytes": int(st.st_size),
                "kind": "目录", "mtime": _iso(st.st_mtime), "children": children(path, home=home)}
    return {"name": os.path.basename(str(path)), "bytes": int(st.st_size), "kind": kind_of(path),
            "mtime": _iso(st.st_mtime)}


def _head_lines(text, lines):
    """按行数截断（只认 \n，别的换行符不算行）。返回 (文本, 是否截断)。"""
    if lines is None or lines < 0:
        return text, False
    out, count, i = [], 0, 0
    while i < len(text) and count < lines:
        j = text.find("\n", i)
        if j < 0:
            out.append(text[i:])
            i = len(text)
        else:
            out.append(text[i:j + 1])
            i = j + 1
        count += 1
    return "".join(out), i < len(text)


def _decode(raw):
    """严格 UTF-8 解码；按字节上限截断时可能切掉半个多字节字符，去掉尾部最多 3 字节再试一次。"""
    try:
        return raw.decode("utf-8"), True
    except UnicodeDecodeError:
        for cut in (1, 2, 3):
            if len(raw) > cut:
                try:
                    return raw[:-cut].decode("utf-8"), True
                except UnicodeDecodeError:
                    pass
        return "", False


def read_head(path, lines, nbytes):
    """读文件开头，返回 (文本, 截断提示列表)。文本是 None = 二进制，只该给元信息与类型。"""
    limit = max(0, int(nbytes or 0))
    with open(path, "rb") as f:
        raw = f.read(limit) if limit else b""
    if b"\x00" in raw:
        return None, []
    text, ok = _decode(raw)
    if not ok:
        return None, []
    text, cut = _head_lines(text, lines)
    notes = []
    if limit and len(raw) >= limit:
        notes.append(f"只给前 {limit} 字节。")
    if cut:
        notes.append(f"只给前 {lines} 行。")
    return text, notes


def record(run, path, cls, mode, home=None):
    """每次调用都记一行（含 refused），成果页与用户核对的就是这一份。"""
    line = {"at": _now_iso(), "path": display_path(path, home), "class": cls, "mode": mode}
    with open(os.path.join(run, "reads.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def summary(run, cap=SUMMARY_PATH_CAP):
    """汇总这次读过哪些文件：计数按每次调用算，路径去重后给前 cap 条（口径同成果页）。"""
    counts = {"content": 0, "meta": 0, "refused": 0}
    paths = []
    try:
        handle = open(os.path.join(run, "reads.jsonl"), encoding="utf-8")
    except OSError:
        return {**counts, "paths": paths}
    with handle:
        for line in handle:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("mode") not in counts:
                continue
            counts[e["mode"]] += 1
            p = e.get("path")
            if e["mode"] == "content" and isinstance(p, str) and p not in paths:
                paths.append(p)
    return {**counts, "paths": paths[:cap]}


def _fail(msg, code=2):
    print(msg, file=sys.stderr)
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(prog="peek.py", description="叁笙清理：读取授权闸门")
    ap.add_argument("--run", required=True, help="运行目录（reads.jsonl 记在这里）")
    ap.add_argument("path", nargs="?", help="要读的文件或目录")
    ap.add_argument("--lines", type=int, default=200, help="最多给多少行（默认 200）")
    ap.add_argument("--bytes", type=int, default=65536, help="最多读多少字节（默认 65536）")
    ap.add_argument("--meta", action="store_true", help="只要元信息，不要内容")
    ap.add_argument("--summary", action="store_true", help="汇总这次读过哪些文件")
    args = ap.parse_args(argv)

    run = os.path.realpath(args.run)
    if not os.path.isdir(run):
        return _fail(f"错误：运行目录不存在：{args.run}。先跑 scan.py 建好这次的运行目录。")
    if args.summary:
        if args.path:
            return _fail("错误：--summary 只做汇总，不用给路径。")
        print(json.dumps(summary(run), ensure_ascii=False, indent=2))
        return 0
    if not args.path:
        return _fail("错误：要说清读哪个文件。用法：peek.py --run <运行目录> <路径>")

    real = os.path.realpath(os.path.expanduser(args.path))
    cls = classify(real)
    mode = decide(cls, read_scope(), args.meta)
    if mode == "refused":
        record(run, real, cls, "refused")
        if cls == "extended":
            return _fail(f"拒绝：{display_path(real)} 是聊天收到的文件、邮件附件、照片或截图，用户没有授权查看这一类"
                         "（初始设置第 1 题“扩展”档）。", 3)
        return _fail(f"拒绝：{display_path(real)} 属于“绝不读”的文件（凭证、密钥或登录信息），任何设置都不能读。", 3)
    if not os.path.exists(real):
        return _fail(f"错误：找不到 {display_path(real)}")

    if os.path.isdir(real):
        print(json.dumps(build_meta(real), ensure_ascii=False, indent=2))
        record(run, real, cls, "meta")
        return 0
    if mode == "meta":
        print(json.dumps(build_meta(real), ensure_ascii=False, indent=2))
        record(run, real, cls, "meta")
        return 0

    try:
        text, notes = read_head(real, args.lines, getattr(args, "bytes"))
    except OSError as exc:
        return _fail(f"错误：读不了 {display_path(real)}（{exc.strerror or exc}）")
    if text is None:                    # 二进制：内容不上屏，只给元信息与类型
        print(json.dumps(build_meta(real), ensure_ascii=False, indent=2))
        if cls == "deep" and kind_of(real) == "图片":
            print(f"提示：用户授权了“深度”档，可以用你自己的看图工具打开 {real} 判断内容（已记入读取清单）。", file=sys.stderr)
    else:
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")
        for note in notes:
            print(f"提示：{note}", file=sys.stderr)
    record(run, real, cls, "content")
    return 0


if __name__ == "__main__":
    sys.exit(main())
