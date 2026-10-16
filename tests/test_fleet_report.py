"""Tests for the Fleet CI report action engine (actions/fleet-report/fleet_report.py)."""

import sys
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions" / "fleet-report"))
import fleet_report as dashboard  # noqa: E402

TODAY = date(2026, 10, 14)


def _day(merged: int, ok: int, bad: int, other: int) -> dict:
    return {"merged": merged, "runs": {"total": ok + bad + other, "success": ok, "failure": bad,
                                       "cancelled": other, "skipped": 0}}


def _data(days: int = 30) -> dict:
    return {
        "repo": "owner/repo",
        "updated": "2026-10-14T18:00Z",
        "open_prs": 9,
        "days": {(TODAY - timedelta(days=i)).isoformat(): _day(30 + i, 1500, 40, 300) for i in range(days)},
    }


def test_days_to_fetch_backfills_the_window_then_only_refreshes_recent_days():
    assert len(dashboard.days_to_fetch({}, TODAY, 30)) == 30
    have = _data()["days"]
    assert dashboard.days_to_fetch(have, TODAY, 30) == ["2026-10-12", "2026-10-13", "2026-10-14"]


def test_render_is_valid_svg_with_one_bar_per_day_and_real_totals():
    svg = dashboard.render(_data(), 30, TODAY)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    titles = [t.text for t in root.iter("{http://www.w3.org/2000/svg}title")]
    assert sum("merged" in t for t in titles) == 30
    assert sum("runs" in t for t in titles) == 30
    merged_total = sum(30 + i for i in range(30))
    assert f"{merged_total:,}" in svg
    assert "97.4%" in svg  # 1500 / (1500 + 40)


def test_render_ignores_history_outside_the_window():
    data = _data(40)
    svg = dashboard.render(data, 30, TODAY)
    assert "last 30 days" in svg


def test_render_adds_the_ci_cost_row_and_wait_chart_when_detail_exists():
    data = _data()
    for i, d in enumerate(sorted(data["days"])):
        data["days"][d]["detail"] = {"bot_runs": 100, "ci_changes": [{"pr": 5000 + i, "title": "t"}] if i % 10 == 0 else [],
                                     "wait_median": 1.0, "wait_p90": 4.0, "runner_min_est": 600, "sampled_runs": 24}
    svg = dashboard.render(data, 30, TODAY)
    ET.fromstring(svg)
    assert "Job queue wait (p90)" in svg and "4.0 min" in svg
    assert "Runner time per merged PR" in svg
    assert svg.count("CI change(s) merged") == 3
    # without detail the extra row and chart stay out
    assert "Job queue wait" not in dashboard.render(_data(), 30, TODAY)


CFG = dashboard.Config(repo="owner/repo", agent_prefixes=["churn/", "claude/"])


def test_classify_pr_by_branch_body_marker_and_bot_author():
    assert dashboard.classify_pr({"headRefName": "churn/5164-x"}, CFG) == "agent"
    assert dashboard.classify_pr({"headRefName": "fix/x", "bodyText": "...\nGenerated with Claude Code"}, CFG) == "agent"
    assert dashboard.classify_pr({"headRefName": "fix/x", "bodyText": "agy-authored, Sonnet-checked"}, CFG) == "agent"
    assert dashboard.classify_pr({"headRefName": "dependabot/pip/x", "author": {"login": "dependabot[bot]"}}, CFG) == "automation"
    assert dashboard.classify_pr({"headRefName": "fix/x", "bodyText": "by hand", "author": {"login": "joe"}}, CFG) == "other"


def _detailed():
    data = _data()
    for i, d in enumerate(sorted(data["days"])):
        data["days"][d]["merged_by_class"] = {"agent": 20, "other": 8, "automation": 2 + i}
        data["days"][d]["merged"] = 30 + i
        data["days"][d]["detail"] = {
            "bot_runs": 100, "ci_changes": [{"pr": 5000 + i, "title": "CI: x | y"}] if i % 10 == 0 else [],
            "wait_median": 1.0, "wait_p90": 4.0 + (10 if i > 22 else 0), "runner_min_est": 600, "sampled_runs": 24,
            "sampled_min_by_workflow": {"Smoke Tests": 60.0, "Lint": 20.0},
            "sampled_min_by_origin": {"agent": 70.0, "main": 10.0}}
    return data


def test_render_splits_merged_prs_by_origin_and_shows_the_agent_share():
    svg = dashboard.render(_detailed(), 30, TODAY, CFG)
    ET.fromstring(svg)
    assert "Merged PRs per day, by origin" in svg and "Agent share of merged PRs" in svg
    assert "owner/repo: CI activity" in svg  # generic heading by default


def test_summary_compares_weeks_and_breaks_runner_time_down():
    md = dashboard.summary_md(_detailed(), CFG, 30, TODAY, [])
    assert "| Measure | Last 7 days | Previous 7 days |" in md
    assert "| Smoke Tests | 75.0% |" in md and "| Agent PRs | 87.5% |" in md
    assert "### CI changes merged" not in md  # none in the last 7 finished days of this fixture
    data = _detailed()
    data["days"][(TODAY - dashboard.timedelta(days=1)).isoformat()]["detail"]["ci_changes"] = [{"pr": 7, "title": "CI: a | b"}]
    assert "- #7: CI: a | b" in dashboard.summary_md(data, CFG, 30, TODAY, [])


def test_alerts_fire_only_past_the_limit():
    data = _detailed()
    assert dashboard.check_alerts(data, 30, TODAY, 20.0, None) == []
    assert "p90" in dashboard.check_alerts(data, 30, TODAY, 10.0, None)[0]
    assert dashboard.check_alerts(data, 30, TODAY, None, 100.0) == []


def test_run_origin_finds_the_pr_by_branch_when_github_omits_it(monkeypatch):
    calls = []

    def fake(path):
        calls.append(path)
        if path == "repos/owner/repo":
            return {"default_branch": "main"}
        if "pulls?head=owner:ci/x" in path:
            return [{"head": {"ref": "ci/x"}, "body": "Generated with [Claude Code](https://claude.com)",
                     "user": {"login": "joe", "type": "User"}}]
        if "pulls?head=" in path:
            return []
        raise AssertionError(path)

    monkeypatch.setattr(dashboard, "gh_json", fake)
    origin = dashboard.RunOrigin("owner/repo", CFG)
    assert origin({"head_branch": "main", "event": "push"}) == "main"
    assert origin({"head_branch": "ci/x", "event": "pull_request", "pull_requests": []}) == "agent"
    assert origin({"head_branch": "ci/x", "event": "pull_request", "pull_requests": []}) == "agent"
    assert sum("pulls?head=owner:ci/x" in c for c in calls) == 1  # cached per branch
    assert origin({"head_branch": "feature/y", "event": "pull_request"}) == "other"


def test_reruns_show_in_the_summary_and_a_tile_row(monkeypatch):
    data = _data()
    for d in sorted(data["days"]):
        data["days"][d]["detail"] = {"bot_runs": 100, "ci_changes": [], "wait_median": 1.0, "wait_p90": 4.0,
                                     "runner_min_est": 600, "sampled_runs": 24,
                                     "reruns": {"runs": 3, "attempts": 4, "passed": 1, "failed_again": 2,
                                                "by_workflow": {"GitGalaxy Smoke Tests": [0, 2, 0], "Det Sweep": [1, 0, 0]}}}
    svg = dashboard.render(data, 30, TODAY)
    ET.fromstring(svg)
    assert "Reruns that passed" in svg and "33.3%" in svg and "GitGalaxy Smoke Tests" in svg
    md = dashboard.summary_md(data, CFG, 30, TODAY, [])
    assert "| Reruns / day | 3.0 |" in md and "| GitGalaxy Smoke Tests | 14 | 0 | 14 |" in md


def test_rerun_counts_pages_two_hour_windows_and_splits_by_outcome(monkeypatch):
    calls = []

    def fake(path):
        calls.append(path)
        if "T10:00:00Z" in path:
            return {"workflow_runs": [{"name": "Smoke", "run_attempt": 3, "status": "completed", "conclusion": "failure"},
                                      {"name": "Det", "run_attempt": 2, "status": "completed", "conclusion": "success"},
                                      {"name": "Det", "run_attempt": 1, "status": "completed", "conclusion": "failure"}]}
        return {"workflow_runs": []}

    monkeypatch.setattr(dashboard, "gh_json", fake)
    r = dashboard.rerun_counts("o/r", "2026-10-15")
    assert len(calls) == 12
    assert r == {"runs": 2, "attempts": 3, "passed": 1, "failed_again": 1,
                 "by_workflow": {"Smoke": [0, 1, 0], "Det": [1, 0, 0]}}
