#!/usr/bin/env python3
"""Bi-directional synchronization bridge between GitGalaxy and squid-bot.

Allows development and experimentation to happen live in GitGalaxy, while
maintaining a clean, generalized, installable version in squid-bot.

Usage:
    python scripts/sync.py status
    python scripts/sync.py diff
    python scripts/sync.py pull [--dry-run]
    python scripts/sync.py push [--dry-run]
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from pathlib import Path

# Paths
SQUID_BOT_ROOT = Path(__file__).resolve().parent.parent

# Detect GitGalaxy root from environment or known local paths
DEFAULT_GG_CANDIDATES = [
    Path("/srv/storage_16tb/projects/gitgalaxy-worktrees/main-check"),
    Path("/home/joe/Projects/gitgalaxy"),
    Path("/srv/storage_16tb/projects/gitgalaxy"),
]


def find_gitgalaxy_root() -> Path:
    env = os.environ.get("GITGALAXY_ROOT")
    if env:
        p = Path(env).resolve()
        if p.exists():
            return p
    for cand in DEFAULT_GG_CANDIDATES:
        if cand.exists() and (cand / ".git").exists():
            return cand
    for cand in DEFAULT_GG_CANDIDATES:
        if cand.exists():
            return cand
    raise FileNotFoundError(
        "Could not find GitGalaxy root. Set GITGALAXY_ROOT environment variable."
    )


# Declarative file mapping: (GitGalaxy relative path, squid-bot relative path)
MAPPINGS: list[tuple[str, str]] = [
    ("tests/tools/box/ci_shepherd.py", "src/squid_bot/ci_shepherd.py"),
    ("tests/tools/shepherd_gh.py", "src/squid_bot/shepherd_gh.py"),
    ("tests/tools/box/box_status.py", "src/squid_bot/box_status.py"),
    ("tests/tools/ci_digest.py", "src/squid_bot/ci_digest.py"),
    ("tests/tools/pr_check.py", "src/squid_bot/pr_check.py"),
    ("tests/tools/box/heavy-run.sh", "scripts/heavy-run.sh"),
    ("tests/tools/box/golden-lock.sh", "scripts/exclusive-lock.sh"),
    ("tests/tools/box/kill-by-cwd.sh", "scripts/kill-by-cwd.sh"),
    ("tests/tools/box/wait-pids.sh", "scripts/wait-pids.sh"),
    (".github/workflows/shepherd-event.yml", "templates/workflows/shepherd-event.yml"),
    (
        ".github/workflows/shepherd-reconcile.yml",
        "templates/workflows/shepherd-reconcile.yml",
    ),
    (".claude/skills/box-capacity/SKILL.md", "templates/skills/box-capacity/SKILL.md"),
    (
        ".claude/skills/parallel-pr-agents/SKILL.md",
        "templates/skills/parallel-agents/SKILL.md",
    ),
    ("tests/tools/test_shepherd_gh.py", "tests/test_shepherd_gh.py"),
    ("tests/tools/test_ci_shepherd.py", "tests/test_ci_shepherd.py"),
    ("tests/tools/flake_history.py", "src/squid_bot/flake_history.py"),
    ("tests/tools/test_flake_history.py", "tests/test_flake_history.py"),
]


def transform_for_squid_bot(content: str, rel_dest: str) -> str:
    """Transform GitGalaxy-specific code into generalized squid_bot code."""
    # 1. Adapt Python imports in src/squid_bot
    if rel_dest.startswith("src/squid_bot/"):
        # Handle sys.path hacking in shepherd_gh.py
        content = re.sub(
            r'sys\.path\.insert\(0,\s*str\(Path\(__file__\)\.resolve\(\)\.parent\)\)\nimport ci_digest\s+# noqa: E402\nimport pr_check\s+# noqa: E402',
            'try:\n    from squid_bot import ci_digest, pr_check\nexcept ImportError:\n    import ci_digest  # noqa: E402\n    import pr_check  # noqa: E402',
            content,
        )
        # Handle sys.path hacking in ci_digest.py
        content = re.sub(
            r'sys\.path\.insert\(0,\s*str\(Path\(__file__\)\.resolve\(\)\.parent\)\)\nimport pr_check\s+# noqa: E402',
            'try:\n    from squid_bot import pr_check\nexcept ImportError:\n    import pr_check  # noqa: E402',
            content,
        )
        # Handle sys.path hacking in ci_shepherd.py
        content = re.sub(
            r'TOOLS = Path\(__file__\)\.resolve\(\)\.parents\[1\]\nsys\.path\.insert\(0,\s*str\(TOOLS\)\)\nimport ci_digest\s+# noqa: E402\n(import flake_history\s+# noqa: E402\n)?import pr_check\s+# noqa: E402',
            lambda m: (
                f"try:\n    from squid_bot import ci_digest, {'flake_history, ' if m.group(1) else ''}pr_check\nexcept ImportError:\n"
                "    TOOLS = Path(__file__).resolve().parents[1]\n    sys.path.insert(0, str(TOOLS))\n    import ci_digest  # noqa: E402\n"
                + ("    import flake_history  # noqa: E402\n" if m.group(1) else "")
                + "    import pr_check  # noqa: E402"
            ),
            content,
        )

        # Handle hardcoded repo slug definitions
        content = re.sub(
            r'REPO_SLUG = "squid-protocol/gitgalaxy"',
            'try:\n    from squid_bot.config import get_repo_slug\n    REPO_SLUG = get_repo_slug()\nexcept Exception:\n    REPO_SLUG = "squid-protocol/gitgalaxy"',
            content,
        )

        # Handle lock dir in box_status.py
        if "box_status.py" in rel_dest:
            content = re.sub(
                r'LOCK_DIR = Path\(os\.environ\.get\("GG_LOCK_DIR", "/tmp/gitgalaxy-scratch/locks"\)\)',
                'try:\n    from squid_bot.config import get_lock_dir\n    LOCK_DIR = get_lock_dir()\nexcept Exception:\n    LOCK_DIR = Path(os.environ.get("SQUID_BOT_LOCK_DIR") or os.environ.get("GG_LOCK_DIR", "/tmp/squid-bot/locks"))',
                content,
            )

        # Handle state path in ci_shepherd.py
        if "ci_shepherd.py" in rel_dest:
            content = re.sub(
                r'STATE = Path\(os\.environ\.get\("CI_SHEPHERD_STATE", .*?\)\)',
                'try:\n    from squid_bot.config import get_state_path\n    STATE = get_state_path()\nexcept Exception:\n    STATE = Path(os.environ.get("SQUID_BOT_STATE") or os.environ.get("CI_SHEPHERD_STATE", "/tmp/squid-bot/state/shepherd.json"))',
                content,
            )

    # 2. Adapt tests
    if rel_dest.startswith("tests/"):
        content = re.sub(
            r'TOOLS = Path\(__file__\)\.resolve\(\)\.parent\nsys\.path\.insert\(0,\s*str\(TOOLS\)\)\nimport det_sweep_plan as dsp\s+# noqa: E402\nimport shepherd_gh as sg\s+# noqa: E402',
            'from squid_bot import shepherd_gh as sg\ntry:\n    import det_sweep_plan as dsp\nexcept ImportError:\n    dsp = None',
            content,
        )
        content = re.sub(
            r'sys\.path\.insert\(0,\s*str\(Path\(__file__\)\.resolve\(\)\.parent / "box"\)\)\nimport ci_shepherd as cs\s+# noqa: E402',
            'from squid_bot import ci_shepherd as cs',
            content,
        )
        # Guard dsp references and REPO path in test_shepherd_gh.py
        if "test_shepherd_gh.py" in rel_dest:
            content = re.sub(
                r'REPO = Path\(__file__\)\.resolve\(\)\.parents\[2\]',
                'REPO = Path(__file__).resolve().parents[1]',
                content,
            )
            content = re.sub(
                r'Path\(__file__\)\.resolve\(\)\.parents\[2\]',
                'REPO',
                content,
            )
            content = re.sub(
                r'"head": \{"sha": SHA, "ref": "fix/x", "repo": \{"full_name": "squid-protocol/gitgalaxy"\}\}',
                '"head": {"sha": SHA, "ref": "fix/x", "repo": {"full_name": sg.REPO_SLUG}}',
                content,
            )
            content = re.sub(
                r'assert f"repos/squid-protocol/gitgalaxy/collaborators/joe/permission" in gh\.calls',
                'assert f"repos/{sg.REPO_SLUG}/collaborators/joe/permission" in gh.calls',
                content,
            )
            content = re.sub(
                r'def test_shepherd_full_label_plans_a_full_det_sweep\(\):',
                'def test_shepherd_full_label_plans_a_full_det_sweep():\n    if dsp is None:\n        pytest.skip("det_sweep_plan is GitGalaxy-specific")',
                content,
            )
            content = re.sub(
                r'def test_labels_from_event_reads_the_pull_request_labels\(tmp_path\):',
                'def test_labels_from_event_reads_the_pull_request_labels(tmp_path):\n    if dsp is None:\n        pytest.skip("det_sweep_plan is GitGalaxy-specific")',
                content,
            )
            content = re.sub(
                r'def test_det_sweep_plan_reads_label\(tmp_path\):',
                'def test_det_sweep_plan_reads_label(tmp_path):\n    if dsp is None:\n        pytest.skip("det_sweep_plan is GitGalaxy-specific")',
                content,
            )
            content = re.sub(
                r'def test_plan_labels_option_overrides_the_event\(tmp_path, monkeypatch, capsys\):',
                'def test_plan_labels_option_overrides_the_event(tmp_path, monkeypatch, capsys):\n    if dsp is None:\n        pytest.skip("det_sweep_plan is GitGalaxy-specific")',
                content,
            )
            content = re.sub(
                r'def test_det_sweep_plan_entrypoint\(\):',
                'def test_det_sweep_plan_entrypoint():\n    if dsp is None:\n        pytest.skip("det_sweep_plan is GitGalaxy-specific")',
                content,
            )
            content = re.sub(
                r'def test_det_sweep_does_not_run_on_label_events_and_reads_labels_live\(\):',
                'def test_det_sweep_does_not_run_on_label_events_and_reads_labels_live():\n    if not (REPO / ".github/workflows/det-sweep.yml").exists():\n        pytest.skip("det-sweep.yml is GitGalaxy-specific")',
                content,
            )
            content = re.sub(
                r'def test_reconcile_triggers_on_the_required_checks_workflows_and_the_matrix_only\(\):',
                'def test_reconcile_triggers_on_the_required_checks_workflows_and_the_matrix_only():\n    if not (REPO / ".github/workflows/full-suite-gate.yml").exists():\n        pytest.skip("GitGalaxy full-suite workflows not present in standalone repo")',
                content,
            )

        # the shepherd workflows ship as templates here, not as this repo's own workflows
        if "test_shepherd_gh.py" in rel_dest:
            content = content.replace('REPO / ".github" / "workflows" / n for n in ("shepherd-event.yml"',
                                      'REPO / "templates" / "workflows" / n for n in ("shepherd-event.yml"')
            content = content.replace('REPO / ".github/workflows/shepherd-event.yml"',
                                      'REPO / "templates/workflows/shepherd-event.yml"')
            content = content.replace('REPO / ".github" / "workflows" / "shepherd-reconcile.yml"',
                                      'REPO / "templates" / "workflows" / "shepherd-reconcile.yml"')

    # 3. Adapt scripts
    if rel_dest.endswith("heavy-run.sh"):
        content = re.sub(
            r'dir=\$\{GG_LOCK_DIR:-/tmp/gitgalaxy-scratch/locks\}',
            'dir="${SQUID_BOT_LOCK_DIR:-${GG_LOCK_DIR:-/tmp/squid-bot/locks}}"',
            content,
        )
        content = re.sub(
            r'export GG_JOB_CORES',
            'export GG_JOB_CORES\n      SQUID_BOT_CORES=$GG_JOB_CORES\n      export SQUID_BOT_CORES',
            content,
        )

    if rel_dest.endswith("exclusive-lock.sh"):
        content = content.replace(
            "dir=${GG_LOCK_DIR:-/tmp/gitgalaxy-scratch/locks}",
            'dir="${SQUID_BOT_LOCK_DIR:-${GG_LOCK_DIR:-/tmp/squid-bot/locks}}"',
        ).replace("${GG_LOCK_DIR:-/tmp/gitgalaxy-scratch/locks}/golden.lock", "${SQUID_BOT_LOCK_DIR:-/tmp/squid-bot/locks}/golden.lock")

    return content


def transform_for_gitgalaxy(content: str, rel_src: str) -> str:
    """Reverse squid_bot adaptations back to GitGalaxy conventions."""
    # If pushing back to GitGalaxy, convert package imports back to relative imports
    content = re.sub(
        r"from squid_bot import ci_digest, pr_check\nfrom squid_bot\.config import get_repo_slug, get_lock_dir, get_state_path",
        "sys.path.insert(0, str(Path(__file__).resolve().parent))\nimport ci_digest  # noqa: E402\nimport pr_check  # noqa: E402",
        content,
    )
    content = re.sub(
        r"from squid_bot import pr_check, shepherd_gh\nfrom squid_bot\.config import get_repo_slug, get_lock_dir, get_state_path",
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\nimport pr_check  # noqa: E402\nimport shepherd_gh  # noqa: E402",
        content,
    )
    return content


def do_diff(gg_root: Path) -> None:
    diffs = 0
    for gg_rel, sb_rel in MAPPINGS:
        gg_file = gg_root / gg_rel
        sb_file = SQUID_BOT_ROOT / sb_rel
        if not gg_file.exists():
            print(f"[MISSING IN GITGALAXY] {gg_rel}")
            continue
        if not sb_file.exists():
            print(f"[MISSING IN SQUID-BOT]  {sb_rel}")
            diffs += 1
            continue

        gg_raw = gg_file.read_text(encoding="utf-8")
        gg_trans = transform_for_squid_bot(gg_raw, sb_rel)
        sb_raw = sb_file.read_text(encoding="utf-8")

        if gg_trans != sb_raw:
            diffs += 1
            print(f"--- GitGalaxy: {gg_rel}")
            print(f"+++ squid-bot: {sb_rel}")
            diff = difflib.unified_diff(
                sb_raw.splitlines(keepends=True),
                gg_trans.splitlines(keepends=True),
                fromfile=f"squid-bot/{sb_rel}",
                tofile=f"gitgalaxy/{gg_rel} (transformed)",
            )
            sys.stdout.writelines(diff)
            print()

    if diffs == 0:
        print("✓ All mapped files are in sync!")


def do_pull(gg_root: Path, dry_run: bool = False) -> None:
    print(f"Pulling from GitGalaxy: {gg_root}")
    count = 0
    for gg_rel, sb_rel in MAPPINGS:
        gg_file = gg_root / gg_rel
        sb_file = SQUID_BOT_ROOT / sb_rel
        if not gg_file.exists():
            print(f"  [SKIP] Missing in GitGalaxy: {gg_rel}")
            continue

        gg_raw = gg_file.read_text(encoding="utf-8")
        transformed = transform_for_squid_bot(gg_raw, sb_rel)

        sb_file.parent.mkdir(parents=True, exist_ok=True)
        if dry_run:
            print(f"  [WOULD WRITE] {sb_rel}")
        else:
            sb_file.write_text(transformed, encoding="utf-8")
            if sb_rel.endswith(".sh"):
                sb_file.chmod(0o755)
            print(f"  [SYNCED] {gg_rel} -> {sb_rel}")
            count += 1
    print(f"Pull complete ({count} files synchronized).")


def do_push(gg_root: Path, dry_run: bool = False) -> None:
    print(f"Pushing to GitGalaxy: {gg_root}")
    count = 0
    for gg_rel, sb_rel in MAPPINGS:
        gg_file = gg_root / gg_rel
        sb_file = SQUID_BOT_ROOT / sb_rel
        if not sb_file.exists():
            print(f"  [SKIP] Missing in squid-bot: {sb_rel}")
            continue

        sb_raw = sb_file.read_text(encoding="utf-8")
        transformed = transform_for_gitgalaxy(sb_raw, gg_rel)

        if dry_run:
            print(f"  [WOULD WRITE] {gg_rel}")
        else:
            gg_file.parent.mkdir(parents=True, exist_ok=True)
            gg_file.write_text(transformed, encoding="utf-8")
            if gg_rel.endswith(".sh"):
                gg_file.chmod(0o755)
            print(f"  [SYNCED] {sb_rel} -> {gg_rel}")
            count += 1
    print(f"Push complete ({count} files synchronized).")


def do_status(gg_root: Path) -> None:
    print(f"GitGalaxy Root : {gg_root}")
    print(f"squid-bot Root : {SQUID_BOT_ROOT}")
    print("\nMapped Files Status:")
    for gg_rel, sb_rel in MAPPINGS:
        gg_file = gg_root / gg_rel
        sb_file = SQUID_BOT_ROOT / sb_rel
        gg_stat = "OK" if gg_file.exists() else "MISSING"
        sb_stat = "OK" if sb_file.exists() else "MISSING"
        state = "SYNCED"
        if not gg_file.exists() or not sb_file.exists():
            state = "UNLINKED"
        else:
            transformed = transform_for_squid_bot(
                gg_file.read_text(encoding="utf-8"), sb_rel
            )
            if transformed != sb_file.read_text(encoding="utf-8"):
                state = "DRIFTED"
        print(f"  {state:<8} | GG: {gg_rel:<42} | SB: {sb_rel}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show sync status of all mapped files")
    sub.add_parser("diff", help="Show unified diff between GitGalaxy and squid-bot")

    pull_p = sub.add_parser(
        "pull", help="Pull latest files from GitGalaxy into squid-bot"
    )
    pull_p.add_argument("--dry-run", action="store_true")

    push_p = sub.add_parser(
        "push", help="Push fixes from squid-bot back into GitGalaxy"
    )
    push_p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    gg_root = find_gitgalaxy_root()

    if args.command == "status":
        do_status(gg_root)
    elif args.command == "diff":
        do_diff(gg_root)
    elif args.command == "pull":
        do_pull(gg_root, dry_run=args.dry_run)
    elif args.command == "push":
        do_push(gg_root, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
