"""No GitGalaxy defaults, state that survives a reboot, and the merge bot's new names beside the old ones."""

from __future__ import annotations

import re

import pytest

from squid_bot import cli, config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in ("XDG_STATE_HOME", "SQUID_BOT_STATE", "CI_SHEPHERD_STATE", "SQUID_BOT_REPO", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(config, "LEGACY_STATE_PATH", str(tmp_path / "old-tmp" / "shepherd.json"))
    return tmp_path


def test_there_is_no_default_repository_and_the_commands_say_how_to_set_one(home, monkeypatch):
    monkeypatch.setattr(config.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no gh")))
    assert config.get_repo_slug() == "" and config.DEFAULT_REQUIRED_CHECKS == []
    with pytest.raises(SystemExit) as e:
        cli.need_repo()
    assert "squid-bot.toml" in str(e.value) and "SQUID_BOT_REPO" in str(e.value)
    (home / "squid-bot.toml").write_text('[repository]\nslug = "me/mine"\n')
    assert cli.need_repo() == "me/mine"


def test_state_lives_under_the_users_state_directory_not_tmp(home, monkeypatch):
    assert config.get_state_path() == home / ".local" / "state" / "squid-bot" / "integrase.json"
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "xdg"))
    assert config.get_state_path() == home / "xdg" / "squid-bot" / "integrase.json"
    # an install that already has state at the old /tmp default keeps it until the new file exists
    legacy = home / "old-tmp" / "shepherd.json"
    legacy.parent.mkdir()
    legacy.write_text("{}")
    assert config.get_state_path() == legacy
    new = home / "xdg" / "squid-bot" / "integrase.json"
    new.parent.mkdir(parents=True)
    new.write_text("{}")
    assert config.get_state_path() == new


def test_the_integrase_section_is_read_and_the_older_shepherd_one_still_works(home):
    (home / "squid-bot.toml").write_text('[shepherd]\nstate_path = "/data/old.json"\nrequired_checks = ["a"]\n')
    assert str(config.get_state_path()) == "/data/old.json" and config.get_required_checks() == ["a"]
    (home / "squid-bot.toml").write_text('[integrase]\nstate_path = "~/q.json"\n[shepherd]\nstate_path = "/data/old.json"\n')
    assert config.get_state_path() == home / "q.json"  # the new section wins, and ~ is expanded


def test_the_new_environment_names_reach_the_module_under_the_names_it_reads():
    env = {"INTEGRASE_FIXER": "fix.sh", "INTEGRASE_MAX_IN_FLIGHT": "2", "CI_SHEPHERD_MAX_IN_FLIGHT": "9",
           "CI_SHEPHERD_MAX_FIXERS": "3"}
    cli.env_aliases(env)
    assert env["CI_SHEPHERD_FIXER"] == "fix.sh" and env["CI_SHEPHERD_MAX_IN_FLIGHT"] == "2"  # the new name wins
    assert env["CI_SHEPHERD_MAX_FIXERS"] == "3" and "INTEGRASE_MAX_FIXERS" not in env  # an old-only setting stays


def test_local_repro_commands_come_from_the_config_never_from_gitgalaxy(home):
    rules, no_local = cli.repro_rules({})
    assert rules == [(r".", "python -m pytest -q {tests}")] and not no_local.match("CodeQL")
    rules, no_local = cli.repro_rules({"repro": {"default": "make test", "no_local": ["CodeQL", "Analyze (py)"],
                                                 "rules": {"^lint$": "ruff check .", "^unit": "pytest {tests}"}}})
    assert rules == [("^lint$", "ruff check ."), ("^unit", "pytest {tests}"), (r".", "make test")]
    assert no_local.match("codeql / analyze") and no_local.match("Analyze (py)") and not no_local.match("lint")
    # applied to the digest module, a failed check is answered from these rules
    (home / "squid-bot.toml").write_text('[repro]\ndefault = "make test"\n[repro.rules]\n"^lint$" = "ruff check ."\n')
    from squid_bot import ci_digest
    before = (ci_digest.REPRO, ci_digest.NO_LOCAL)
    try:
        dg = cli._digest_module()
        ex = {"tests": ["tests/t.py::test_x"], "cases": []}
        assert dg.repro("lint", ex) == "ruff check ." and dg.repro("anything else", ex) == "make test"
        assert all("pr_gates" not in cmd and "gitgalaxy" not in cmd for _, cmd in dg.REPRO)
    finally:
        ci_digest.REPRO, ci_digest.NO_LOCAL = before  # the module is shared with the other tests


def test_every_installed_command_goes_through_the_wrappers():
    text = open(config.__file__.replace("src/squid_bot/config.py", "pyproject.toml")).read()
    scripts = dict(re.findall(r'^([\w-]+) = "(squid_bot\.[\w.]+:\w+)"$', text, re.M))
    assert scripts["integrase-bot"] == scripts["squid-bot"] == scripts["shepherd"] == "squid_bot.cli:integrase"
    assert scripts["ci-digest"] == "squid_bot.cli:ci_digest" and scripts["flake-history"] == "squid_bot.cli:flake_history"
    assert scripts["box-status"] == "squid_bot.box_status:main"  # needs no repository
