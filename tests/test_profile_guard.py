"""0.5 设置时序闸门：profile.json 在 seal 之后才改（保留天数、保护路径、成品目录等），本次执行不能继续沿用封存时的判断。

seal 记录影响判断的设置摘要（protect.profile_guard），act.py run（预演/试点/正式）与 go 发现现在的设置与封存时不同就拒绝，退出码 2。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_act import Env, act, age, auto_sel, card, delete_item, mkfile, node, plan, protect  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def write_profile(env, **kw):
    p = Path(protect.PROFILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(kw), encoding="utf-8")


def setup(env):
    d = env.work / "cache"
    mkfile(d / "a.bin")
    age(d)
    env.write([node(d)], [card("c01", [delete_item(d)])])
    return d, env.seal()


def run_argv(env, digest, *flags):
    return auto_sel(["run", "--run", str(env.run), "--seal", digest, "--cards", "c01", *flags])


def test_seal_records_profile_guard(env):
    write_profile(env, retention_days=7, protect=["~/b", "~/a"])
    _, digest = setup(env)
    seal = json.loads((env.run / "seal.json").read_text())
    g = seal["profile_guard"]
    assert g["fields"]["retention_days"] == 7 and g["fields"]["protect"] == ["~/a", "~/b"]
    assert g["digest"] == protect.profile_guard({"retention_days": 7, "protect": ["~/a", "~/b"]})["digest"]
    assert seal["digest"] == digest


def test_retention_changed_after_seal_blocks_every_mode(env, capsys):
    """例：封存时没设保留天数，之后改成 15 天——预演、试点、正式执行都拒绝，什么都不删。"""
    d, digest = setup(env)
    write_profile(env, retention_days=15)
    for flags in (["--dry-run"], ["--pilot"], ["--skip-pilot", "--reason", "x"]):
        assert act.main(run_argv(env, digest, *flags)) == 2, flags
        err = capsys.readouterr().err
        assert "设置在封存后改过" in err and "AI 对话保留天数" in err and "check / seal" in err
    assert (d / "a.bin").exists()
    assert not list(env.run.glob("receipt*.json"))


def test_go_blocked_when_settings_changed_after_seal(env, capsys):
    d, _ = setup(env)
    write_profile(env, onboarded_at="2026-10-11T00:00:00Z")        # 对话确认方式的提案（第一次使用阶段 go --choice 不收）
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    write_profile(env, protect=["~/elsewhere"])
    assert act.main(["go", "--run", str(env.run), "--choice", "safe"]) == 2
    assert "设置在封存后改过" in capsys.readouterr().err
    assert (d / "a.bin").exists()


def test_unchanged_settings_run_normally(env):
    write_profile(env, retention_days=15, protect=["~/x"])
    d, digest = setup(env)
    write_profile(env, protect=["~/x"], retention_days=15, onboarded_at="2026-10-11T00:00:00Z",
                  default_choice="safe", rules={"a/b/safe": {"action": "delete"}})   # 不影响判断的字段随便改
    assert env.act("c01", digest=digest) == 0
    assert not d.exists()


def test_reseal_after_change_runs_and_keeps_digest(env):
    """改设置后重新 check / seal：卡片没变时 digest 不变（用户的选择清单仍有效），按新设置放行。"""
    d, digest = setup(env)
    write_profile(env, retention_days=30)
    assert act.main(run_argv(env, digest, "--dry-run")) == 2
    assert env.seal() == digest
    assert env.act("c01", digest=digest) == 0
    assert not d.exists()


def test_old_seal_without_guard_is_refused(env, capsys):
    d, digest = setup(env)
    seal = json.loads((env.run / "seal.json").read_text())
    seal.pop("profile_guard")
    (env.run / "seal.json").write_text(json.dumps(seal), encoding="utf-8")
    assert act.main(run_argv(env, digest, "--dry-run")) == 2
    assert "没有记录当时的设置" in capsys.readouterr().err
    assert (d / "a.bin").exists()


def test_guard_ignores_defaults_and_order():
    a = protect.profile_guard({})
    b = protect.profile_guard({"roots": ["~"], "extra_roots": [], "protect": [], "retention_days": 0, "notes": {"x": "y"}})
    assert a["digest"] == b["digest"]
    c = protect.profile_guard({"protect": ["~/b", "~/a"]})
    d = protect.profile_guard({"protect": ["~/a", "~/b", "~/a"]})
    assert c["digest"] == d["digest"] != a["digest"]
    for key, val in (("retention_days", 7), ("deliverable_roots", ["~/out"]), ("reference_roots", ["~/r"]),
                     ("roots", ["~/w"]), ("extra_roots", ["/Applications"])):
        assert protect.profile_guard({key: val})["digest"] != a["digest"], key


def test_profile_apply_and_propose_remind_about_stale_seal(env, capsys, monkeypatch):
    """改了影响判断的设置：profile --apply 与 propose 都提醒要重新 check / seal；只改规则/默认档则不提醒 profile --apply。"""
    d, digest = setup(env)
    f = env.run / "settings.txt"
    f.write_text("设置: 默认=safe\n", encoding="utf-8")
    assert plan.main(["profile", "--apply", str(f)]) == 0
    assert "check 和 plan.py seal" not in capsys.readouterr().err
    f.write_text('设置: {"保留天数": 7}\n', encoding="utf-8")
    assert plan.main(["profile", "--apply", str(f)]) == 0
    assert "plan.py check 和 plan.py seal" in capsys.readouterr().err
    assert plan.main(["propose", "--run", str(env.run)]) == 0
    assert "设置在封存后改过" in capsys.readouterr().err


# ---------------------------------------------------------------- 0.6：初始设置里会改变 check 结果的项也进闸门

GUARDED_SETTINGS = {
    "tools": {"codex": "unused"},
    "conversation_policy": "library",
    "rejects_policy": "keep",
    "worktree_policy": "whole",
    "big_file_age_days": 30,
    "done_project_policy": "archive",
    "installer_policy": "auto",
}


@pytest.mark.parametrize("key", sorted(GUARDED_SETTINGS))
def test_new_setting_changed_after_seal_blocks_run(env, capsys, key):
    """封存后改了任一会影响 check 的新设置：预演/正式执行都拒绝，什么都不删。"""
    d, digest = setup(env)
    write_profile(env, **{key: GUARDED_SETTINGS[key]})
    assert act.main(run_argv(env, digest, "--dry-run")) == 2
    assert "设置在封存后改过" in capsys.readouterr().err
    assert act.main(run_argv(env, digest, "--skip-pilot", "--reason", "x")) == 2
    capsys.readouterr()
    assert (d / "a.bin").exists()
    assert not list(env.run.glob("receipt*.json"))


@pytest.mark.parametrize("key", sorted(GUARDED_SETTINGS))
def test_new_setting_unchanged_runs(env, key):
    write_profile(env, **{key: GUARDED_SETTINGS[key]})
    d, digest = setup(env)
    write_profile(env, **{key: GUARDED_SETTINGS[key]})
    assert env.act("c01", digest=digest) == 0
    assert not d.exists()


def test_non_judging_settings_do_not_block(env):
    """read_scope / delete_mode / governance_mode / confirm_mode / cloud_policy 与“常用 / 偶尔用”不影响放行，改了照常执行。"""
    d, digest = setup(env)
    write_profile(env, read_scope=["basic", "deep"], delete_mode="delete", governance_mode="tell",
                  confirm_mode="board", cloud_policy="offline", default_choice="all",
                  tools={"codex": "often", "claude-code": "sometimes"}, conversation_policy="sometimes",
                  rejects_policy="until_done", installer_policy="ask", done_project_policy="intermediate")
    assert env.act("c01", digest=digest) == 0
    assert not d.exists()


def test_guard_new_fields_default_equivalence_and_old_digest():
    """没设 = 设成“等于没设”的值（含非法值）；0.5 的旧封存（没有这些字段）摘要不变。"""
    import hashlib
    base = protect.profile_guard({})
    equal = protect.profile_guard({
        "tools": {"codex": "often", "grok": "sometimes", "nope": "unused"}, "conversation_policy": "sometimes",
        "rejects_policy": "until_done", "done_project_policy": "intermediate", "installer_policy": "ask",
        "big_file_age_days": 45, "worktree_policy": "bogus", "read_scope": ["deep"], "delete_mode": "delete"})
    assert equal["digest"] == base["digest"] and equal["fields"] == base["fields"]
    # 0.5 旧封存摘要：只有旧七个字段
    old_blob = json.dumps({"roots": ["~"], "extra_roots": [], "reference_roots": [], "protect": [],
                           "deliverable_roots": [], "retention_days": 0},
                          sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert base["digest"] == hashlib.sha256(old_blob.encode("utf-8")).hexdigest()[:12]
    for key, val in GUARDED_SETTINGS.items():
        assert protect.profile_guard({key: val})["digest"] != base["digest"], key
    # 取值不同也算不同；tools 只看“不用了”，与顺序无关
    assert protect.profile_guard({"tools": {"a": "x"}})["digest"] == base["digest"]
    assert protect.profile_guard({"tools": {"codex": "unused", "grok": "unused"}})["digest"] == \
        protect.profile_guard({"tools": {"grok": "unused", "codex": "unused"}})["digest"]
    assert protect.profile_guard({"conversation_policy": "library"})["digest"] != \
        protect.profile_guard({"conversation_policy": "done"})["digest"]
    assert protect.profile_guard({"big_file_age_days": 0})["digest"] != protect.profile_guard({"big_file_age_days": 90})["digest"]


def test_guard_values_match_setup_q():
    """闸门里的允许值与题库一致：题库新增会影响判断的取值时，这里必须同步。"""
    import setup_q
    assert set(protect.GUARD_TOOL_KEYS) == set(setup_q.TOOL_KEYS)
    assert tuple(protect.GUARD_BIG_DAYS) == tuple(setup_q.BIG_FILE_DAYS)
    ids = {"conversation_policy": "conversations", "rejects_policy": "rejects", "worktree_policy": "worktrees",
           "done_project_policy": "done_projects", "installer_policy": "installers"}
    for field, qid in ids.items():
        assert set(protect.GUARD_EFFECTIVE[field]) <= set(setup_q.SINGLE[qid][1]), field
        assert setup_q.SINGLE[qid][0] == field
    # 所有进闸门的新字段名都是题库的真字段
    new = {k for k, _ in protect.PROFILE_GUARD_FIELDS} - {"roots", "extra_roots", "reference_roots", "protect",
                                                           "deliverable_roots", "retention_days", "projects"}
    assert new == set(ids) | {"tools", "big_file_age_days"}
