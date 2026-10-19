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


def test_transform_for_squid_bot_replaces_exclusive_lock_dir():
    content = "# Lock: ${GG_LOCK_DIR:-/tmp/gitgalaxy-scratch/locks}/golden.lock.\ndir=${GG_LOCK_DIR:-/tmp/gitgalaxy-scratch/locks}\n"
    res = sync.transform_for_squid_bot(content, "scripts/exclusive-lock.sh")
    assert "gitgalaxy-scratch" not in res
    assert "SQUID_BOT_LOCK_DIR" in res


def test_transform_for_squid_bot_keeps_ci_shepherd_imports_with_and_without_flake_history():
    head = "TOOLS = Path(__file__).resolve().parents[1]\nsys.path.insert(0, str(TOOLS))\nimport ci_digest  # noqa: E402\n"
    for extra, want in (("", "from squid_bot import ci_digest, pr_check"),
                        ("import flake_history  # noqa: E402\n", "from squid_bot import ci_digest, flake_history, pr_check")):
        res = sync.transform_for_squid_bot(head + extra + "import pr_check  # noqa: E402\n", "src/squid_bot/ci_shepherd.py")
        assert res.startswith("try:\n    " + want + "\nexcept ImportError:\n")
        assert ("    import flake_history  # noqa: E402" in res) == bool(extra)


def test_package_imports_covers_any_sibling_run_and_aliases():
    src = ("import sys\n\nsys.path.insert(0, str(Path(__file__).resolve().parent))\nimport ci_digest  # noqa: E402\n"
           "import flake_history  # noqa: E402\nimport pr_check  # noqa: E402\n\nX = 1\n")
    res = sync.package_imports(src)
    assert "try:\n    from squid_bot import ci_digest, flake_history, pr_check\nexcept ImportError:\n" in res
    assert "    sys.path.insert(0, str(Path(__file__).resolve().parent))\n    import ci_digest  # noqa: E402\n" in res
    assert res.startswith("import sys\n") and res.endswith("\nX = 1\n")  # other imports and code untouched
    assert sync.package_imports("import flake_history as fh\n") == (
        "try:\n    from squid_bot import flake_history as fh\nexcept ImportError:\n    import flake_history as fh\n")
    assert sync.package_imports("import ci_rerun_stats\nimport json\n") == "import ci_rerun_stats\nimport json\n"


def test_package_imports_wraps_function_local_imports_unless_import_guarded():
    loose = "def f():\n    try:\n        import box_status\n        return 1\n    except Exception:\n        return 0\n"
    res = sync.package_imports(loose)
    assert "        try:\n            from squid_bot import box_status\n        except ImportError:\n            import box_status\n" in res
    guarded = "def f():\n    try:\n        import box_status\n    except ImportError:\n        box_status = None\n"
    assert sync.package_imports(guarded) == guarded
    once = sync.package_imports("TOOLS = x\nsys.path.insert(0, str(TOOLS))\nimport ci_digest  # noqa: E402\n" + loose)
    assert sync.package_imports(once) == once  # idempotent


def test_gitgalaxy_only_guard_test_is_skipped_here():
    src = "def test_no_squid_bot_reference_in_the_shepherd_tools():\n    from pathlib import Path\n"
    res = sync.transform_for_squid_bot(src, "tests/test_flake_history.py")
    assert 'pytest.skip("GitGalaxy-only' in res
