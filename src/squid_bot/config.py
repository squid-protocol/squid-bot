"""Configuration management for squid-bot."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

# Default configuration values. 2026-11-13: the defaults were GitGalaxy's (its repository slug, its five required
# checks) and the merge queue's state lived under /tmp, where a reboot loses it. There is now no default repository
# (get_repo_slug returns "" and the commands that need one say how to set it), required checks come from the
# repository's own rules on main, and state lives under the user's state directory.
DEFAULT_REPO_SLUG = ""
DEFAULT_LOCK_DIR = "/tmp/squid-bot/locks"  # flock files only: nothing here needs to survive a reboot
LEGACY_STATE_PATH = "/tmp/squid-bot/state/shepherd.json"
DEFAULT_REQUIRED_CHECKS: list[str] = []
# The merge bot was renamed from "shepherd" to integrase-bot. New names first; the old ones still work.
SECTIONS = ("integrase", "shepherd")


def default_state_path() -> Path:
    """<state dir>/squid-bot/integrase.json, where the state dir is $XDG_STATE_HOME or ~/.local/state. An install
    that already has state at the old /tmp default and none at the new place keeps using the old file."""
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    new = base / "squid-bot" / "integrase.json"
    legacy = Path(LEGACY_STATE_PATH)
    return legacy if legacy.exists() and not new.exists() else new


def _section(cfg: dict[str, Any]) -> dict[str, Any]:
    """The merge bot's config section: [integrase], else the older [shepherd]."""
    for name in SECTIONS:
        if isinstance(cfg.get(name), dict):
            return cfg[name]
    return {}


def _find_config_file() -> Path | None:
    """Search current directory and ancestors for squid-bot.toml."""
    cur = Path.cwd().resolve()
    for parent in [cur, *cur.parents]:
        p = parent / "squid-bot.toml"
        if p.is_file():
            return p
    return None


def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file safely across Python 3.10 and 3.11+."""
    try:
        import tomllib

        with open(path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        try:
            import tomli

            with open(path, "rb") as f:
                return tomli.load(f)
        except ImportError:
            # Minimal fallback parser for simple key-value TOML
            res: dict[str, Any] = {}
            section = ""
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].strip()
                    res.setdefault(section, {})
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if section:
                        res[section][k] = v
                    else:
                        res[k] = v
            return res


def get_config() -> dict[str, Any]:
    """Get the merged configuration dict from file and defaults."""
    cfg_file = _find_config_file()
    if cfg_file:
        return _load_toml(cfg_file)
    return {}


def get_repo_slug() -> str:
    """Resolve the target GitHub repository slug ('owner/repo'); "" when none is configured or detectable."""
    # 1. Environment variable override
    env = os.environ.get("SQUID_BOT_REPO") or os.environ.get("GITHUB_REPOSITORY")
    if env:
        return env

    # 2. squid-bot.toml config
    cfg = get_config()
    repo_cfg = cfg.get("repository", {})
    if isinstance(repo_cfg, dict) and "slug" in repo_cfg:
        return str(repo_cfg["slug"])

    # 3. Detect via gh cli if in a git repo
    try:
        proc = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass

    # 4. None: the caller says how to set one (see squid_bot.cli.need_repo)
    return DEFAULT_REPO_SLUG


def get_lock_dir() -> Path:
    """Resolve the directory used for locks."""
    p = os.environ.get("SQUID_BOT_LOCK_DIR") or os.environ.get("GG_LOCK_DIR")
    if p:
        return Path(p)

    cfg = get_config()
    sup_cfg = cfg.get("supervisor", {})
    if isinstance(sup_cfg, dict) and "lock_dir" in sup_cfg:
        return Path(sup_cfg["lock_dir"])

    return Path(DEFAULT_LOCK_DIR)


def get_state_path() -> Path:
    """Resolve the path to the merge bot's state file."""
    p = os.environ.get("SQUID_BOT_STATE") or os.environ.get("CI_SHEPHERD_STATE")
    if p:
        return Path(p)

    shp_cfg = _section(get_config())
    if "state_path" in shp_cfg:
        return Path(str(shp_cfg["state_path"])).expanduser()

    return default_state_path()


def get_required_checks() -> list[str]:
    """Resolve the list of required check names for auto-merge."""
    shp_cfg = _section(get_config())
    if "required_checks" in shp_cfg:
        val = shp_cfg["required_checks"]
        if isinstance(val, list):
            return [str(x) for x in val]
    return list(DEFAULT_REQUIRED_CHECKS)
