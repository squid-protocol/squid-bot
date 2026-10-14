"""Configuration management for squid-bot."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

# Default configuration values
DEFAULT_REPO_SLUG = "squid-protocol/gitgalaxy"
DEFAULT_LOCK_DIR = "/tmp/squid-bot/locks"
DEFAULT_STATE_PATH = "/tmp/squid-bot/state/shepherd.json"
DEFAULT_REQUIRED_CHECKS = [
    "det",
    "full-suite",
    "ground-truth",
    "mypy-audit",
    "ruff-audit",
]


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
    """Resolve the target GitHub repository slug ('owner/repo')."""
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

    # 4. Default fallback
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
    """Resolve the path to the CI shepherd state file."""
    p = os.environ.get("SQUID_BOT_STATE") or os.environ.get("CI_SHEPHERD_STATE")
    if p:
        return Path(p)

    cfg = get_config()
    shp_cfg = cfg.get("shepherd", {})
    if isinstance(shp_cfg, dict) and "state_path" in shp_cfg:
        return Path(shp_cfg["state_path"])

    return Path(DEFAULT_STATE_PATH)


def get_required_checks() -> list[str]:
    """Resolve the list of required check names for auto-merge."""
    cfg = get_config()
    shp_cfg = cfg.get("shepherd", {})
    if isinstance(shp_cfg, dict) and "required_checks" in shp_cfg:
        val = shp_cfg["required_checks"]
        if isinstance(val, list):
            return [str(x) for x in val]
    return list(DEFAULT_REQUIRED_CHECKS)
