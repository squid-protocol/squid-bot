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
    assert sync.package_imports("import det_sweep_plan\nimport json\n") == "import det_sweep_plan\nimport json\n"


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


def test_the_cause_classifier_is_synced_as_a_package_member():
    """2026-11-13: flake_history's causes come from ci_rerun_stats, which was not synced, so three of its tests failed
    on every synced tree."""
    assert ("tests/tools/ci_rerun_stats.py", "src/squid_bot/ci_rerun_stats.py") in sync.MAPPINGS
    fh = sync.transform_for_squid_bot(
        "try:\n    import ci_rerun_stats  # the classifier\nexcept ImportError:\n    ci_rerun_stats = None\n",
        "src/squid_bot/flake_history.py")
    assert fh.startswith("try:\n    from squid_bot import ci_rerun_stats  # the classifier\nexcept ImportError:\n")
    stats = sync.transform_for_squid_bot('import json\n\nREPO = "squid-protocol/gitgalaxy"\n', "src/squid_bot/ci_rerun_stats.py")
    assert "REPO = get_repo_slug()" in stats and 'REPO = "squid-protocol/gitgalaxy"' in stats  # the fallback stays


def test_shepherd_workflow_tests_find_the_templates_however_they_spell_the_path():
    src = ('REPO = Path(__file__).resolve().parents[2]\n\n\ndef _workflow(name):\n    yaml = pytest.importorskip("yaml")\n'
           '    doc = yaml.safe_load((REPO / ".github" / "workflows" / name).read_text(encoding="utf-8"))\n    return doc\n\n\n'
           'def test_x():\n    root = REPO / ".github" / "workflows"\n    return root\n')
    res = sync.transform_for_squid_bot(src, "tests/test_shepherd_gh.py")
    assert 'def _wf_path(name):\n    here = REPO / "templates" / "workflows" / name\n' in res
    assert '        pytest.skip(f"{name} is GitGalaxy-specific")\n    doc = yaml.safe_load(_wf_path(name).read_text(' in res
    assert '    root = REPO / "templates" / "workflows"\n' in res and res.count("def _wf_path") == 1
