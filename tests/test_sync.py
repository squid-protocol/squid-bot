"""Unit tests for the GitGalaxy <-> squid-bot synchronization bridge."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sync


def test_mappings_defined():
    assert len(sync.MAPPINGS) >= 10
    dest_paths = [dest for _, dest in sync.MAPPINGS]
    assert "src/squid_bot/ci_shepherd.py" in dest_paths
    assert "src/squid_bot/box_status.py" in dest_paths
    assert "scripts/heavy-run.sh" in dest_paths


def test_transform_for_squid_bot_replaces_repo_slug():
    content = 'REPO_SLUG = "squid-protocol/gitgalaxy"\n'
    res = sync.transform_for_squid_bot(content, "src/squid_bot/pr_check.py")
    assert "get_repo_slug" in res


def test_transform_for_squid_bot_replaces_heavy_run_locks():
    content = 'dir=${GG_LOCK_DIR:-/tmp/gitgalaxy-scratch/locks}\nexport GG_JOB_CORES\n'
    res = sync.transform_for_squid_bot(content, "scripts/heavy-run.sh")
    assert "SQUID_BOT_LOCK_DIR" in res
    assert "SQUID_BOT_CORES" in res
