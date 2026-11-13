"""Entry points for the installed commands.

The modules behind them (ci_shepherd, ci_digest, pr_check, flake_history) are generalized copies of GitGalaxy's tools,
rewritten on every sync, so what makes them fit another repository lives here instead:

- the merge bot's newer environment names (INTEGRASE_*) are passed on under the names the module reads (CI_SHEPHERD_*);
- a command that needs a repository says how to set one when none is configured (there is no default repository);
- the local repro commands `ci-digest` prints come from `[repro]` in squid-bot.toml. Without that section a failed
  check with named tests gets `python -m pytest -q <tests>`; GitGalaxy's own commands are not assumed.
"""

from __future__ import annotations

import os
import re
import sys

from squid_bot import config

# new name -> the name the synced module reads
ENV_ALIASES = {
    "INTEGRASE_FIXER": "CI_SHEPHERD_FIXER",
    "INTEGRASE_MAX_FIXERS": "CI_SHEPHERD_MAX_FIXERS",
    "INTEGRASE_MAX_IN_FLIGHT": "CI_SHEPHERD_MAX_IN_FLIGHT",
    "INTEGRASE_STATE": "CI_SHEPHERD_STATE",
    "INTEGRASE_TRUSTED_BOTS": "SHEPHERD_TRUSTED_BOTS",
    "INTEGRASE_FLAKE_CACHE": "SHEPHERD_FLAKE_CACHE",
}
DEFAULT_REPRO = "python -m pytest -q {tests}"
NO_REPO = ("squid-bot: no repository to work on. Set one of:\n"
           '  - [repository] slug = "owner/repo" in squid-bot.toml (searched from the current directory upward)\n'
           "  - the SQUID_BOT_REPO environment variable\n"
           "or run the command inside a checkout that `gh repo view` recognises.")


def env_aliases(env: dict | None = None) -> None:
    """Copy each new-name variable to the old name its module reads. The new name wins when both are set."""
    env = os.environ if env is None else env
    for new, old in ENV_ALIASES.items():
        if env.get(new):
            env[old] = env[new]


def need_repo() -> str:
    slug = config.get_repo_slug()
    if not slug:
        raise SystemExit(NO_REPO)
    return slug


def repro_rules(cfg: dict | None = None) -> tuple[list[tuple[str, str]], re.Pattern]:
    """(REPRO rules, the NO_LOCAL pattern) for ci_digest, from `[repro]`:

        [repro]
        default = "make test"                       # any failed check no rule matches
        no_local = ["CodeQL", "Analyze"]            # checks with no local equivalent (matched at the start)
        [repro.rules]                               # check-name regex = command; {tests} and {cases} are filled in
        "^lint$" = "ruff check ."
    """
    r = (config.get_config() if cfg is None else cfg).get("repro") or {}
    r = r if isinstance(r, dict) else {}
    rules = [(str(k), str(v)) for k, v in (r.get("rules") or {}).items()] if isinstance(r.get("rules"), dict) else []
    rules.append((r".", str(r.get("default") or DEFAULT_REPRO)))  # last: every other check
    names = [re.escape(str(x)) for x in r.get("no_local") or []] if isinstance(r.get("no_local"), list) else []
    return rules, re.compile(r"^(?:" + "|".join(names) + r")" if names else r"(?!)", re.I)


def _digest_module():
    from squid_bot import ci_digest

    ci_digest.REPRO, ci_digest.NO_LOCAL = repro_rules()
    return ci_digest


def integrase(argv: list[str] | None = None) -> int:
    """`integrase-bot` (also installed as `squid-bot` and, for older setups, `shepherd`)."""
    env_aliases()
    need_repo()
    _digest_module()
    from squid_bot import ci_shepherd

    return ci_shepherd.main(*([argv] if argv is not None else []))


def ci_digest(argv: list[str] | None = None) -> int:
    need_repo()
    return _digest_module().main(*([argv] if argv is not None else []))


def pr_check(argv: list[str] | None = None) -> int:
    need_repo()
    from squid_bot import pr_check as mod

    return mod.main(*([argv] if argv is not None else []))


def flake_history(argv: list[str] | None = None) -> int:
    env_aliases()
    need_repo()
    from squid_bot import flake_history as mod

    return mod.main(*([argv] if argv is not None else []))
