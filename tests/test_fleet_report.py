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
    assert "2.6% of runs failed (40 of 1,540" in svg  # 40 / (1500 + 40)


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
    assert "job queue wait (p90)" in svg and "4.0 min" in svg
    assert "runner time per merged PR" in svg
    assert svg.count("CI change(s) merged") == 3
    # without detail the extra row and chart stay out
    assert "job queue wait" not in dashboard.render(_data(), 30, TODAY)


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


def test_an_orchestrators_factory_json_adds_an_agent_row_and_a_summary_table():
    data = _data()
    for d in sorted(data["days"]):
        data["days"][d]["detail"] = {"bot_runs": 100, "ci_changes": [], "wait_median": 1.0, "wait_p90": 4.0,
                                     "runner_min_est": 600, "sampled_runs": 24}
    day = {"worker_hours": {"new_work": 14.0, "pr_fix": 4.0, "send_back": 2.0, "precheck_retry": 0.0},
           "new_work_share": 0.7, "slot_use": 0.5, "max_workers": 2, "loops_per_merged_pr": 1.2, "loop_runs": 12,
           "churn_merged": 10, "precheck": {"sent_back": 1, "passed": 3, "failed_again": 0}, "paused_hours": 1.0,
           "ci_busy_hours": 2.0, "idle_ticks": 30}
    data["factory"] = {"days": {(TODAY - timedelta(days=i)).isoformat(): day for i in range(0, 9)}}
    svg = dashboard.render(data, 30, TODAY)
    ET.fromstring(svg)
    assert "Agent time on new work (7d)" in svg and "70.0%" in svg and "Idle with a free slot" in svg and "5.0h/day" in svg
    md = dashboard.summary_md(data, CFG, 30, TODAY, [])
    assert "### Agent factory" in md and md.count("| 2026-10-") >= 7 and str(TODAY) not in md.split("### Agent factory")[1]
    assert "Agent time" not in dashboard.render(_data(), 30, TODAY)  # no factory.json: no row


def test_charts_show_each_days_own_numbers_not_a_trailing_average():
    data = _data()
    data["days"][TODAY.isoformat()] = _day(71, 900, 100, 0)  # a spike today: 71 merged, 90% pass
    svg = dashboard.render(data, 30, TODAY)
    ET.fromstring(svg)
    assert "Oct 14 · 71 merged PRs (today so far)" in svg
    assert "Oct 14: 10.0% of runs failed (100 of 1,000" in svg
    assert "Oct 13 · 31 merged PRs ·" in svg
    assert "7-day average" not in svg and "trailing" not in svg


BOT = dashboard.Config(repo="owner/repo", agent_prefixes=["claude/"], bot_prefixes=["churn/"], bot_name="promoter-bot",
                       events=[("2026-10-06", "shepherd on")])


def _with_bot(start: str = "2026-10-08") -> dict:
    data = _data()
    for d in sorted(data["days"]):
        bot = 25 if d >= start else 0
        data["days"][d]["merged_by_class"] = {"bot": bot, "agent": 5, "other": 10, "automation": 0}
        data["days"][d]["merged"] = bot + 15
        data["days"][d]["detail"] = {"bot_runs": 100, "ci_changes": [], "wait_median": 1.0, "sampled_runs": 24,
                                     "wait_p90": 2.0 if d >= start else 8.0, "runner_min_est": 600}
    return data


def test_bot_branches_are_their_own_class_ahead_of_agent_prefixes():
    assert dashboard.classify_pr({"headRefName": "churn/5164-x"}, BOT) == "bot"
    assert dashboard.classify_pr({"headRefName": "claude/x"}, BOT) == "agent"


def test_the_bot_start_is_found_from_its_first_merge_and_kept_in_view():
    data = _with_bot()
    assert dashboard.bot_start(data, BOT) == "2026-10-08"
    assert dashboard.bot_start(data, dashboard.Config(repo="owner/repo", bot_start="2026-10-01")) == "2026-10-01"
    assert dashboard.shown_days(data, BOT, 30, TODAY) == 30
    assert dashboard.shown_days(data, dashboard.Config(repo="o/r", bot_start="2026-09-01"), 30, TODAY) == 58


def test_render_tells_the_before_and_with_bot_story():
    svg = dashboard.render(_with_bot(), 30, TODAY, BOT)
    ET.fromstring(svg)
    assert "promoter-bot live · Oct 8" in svg and "Oct 6 · shepherd on" in svg
    assert 'class="band"' in svg and svg.count('class="start"') == 4  # the band and start line run through every panel
    assert "PROMOTER-BOT-MADE SHARE" in svg and "62.5%" in svg  # 25 of 40 merges a day since it started
    assert "before promoter-bot: 15.0 · +167%" in svg  # merged / day: 40 with vs 15 before
    assert "avg " not in svg and 'class="avg"' not in svg  # averages live in the cards, not on the charts
    assert "175 of the 280 PRs merged since then were its own" in svg
    md = dashboard.summary_md(_with_bot(), BOT, 30, TODAY, [])
    assert "| Measure | With promoter-bot | Before promoter-bot |" in md
    assert "| Delivery · promoter-bot-made share | 62.5% | 0.0% |" in md


def test_axes_use_the_tightest_round_step():
    assert dashboard._nice_axis(26.05) == (30, 10)
    assert dashboard._nice_axis(102) == (125, 25)
    assert dashboard._nice_axis(790) == (800, 200)
    assert dashboard._nice_axis(0.4) == (0.4, 0.1)


def _run(name, conclusion, minutes):
    return {"name": name, "status": "completed", "conclusion": conclusion,
            "run_started_at": "2026-10-14T10:00:00Z", "updated_at": f"2026-10-14T10:{minutes:02d}:00Z"}


def test_minutes_saved_prices_a_cancelled_run_at_its_workflows_median_success():
    runs = [_run("Smoke", "success", 20), _run("Smoke", "success", 30), _run("Smoke", "success", 40),
            _run("Smoke", "cancelled", 5),        # would have run 30: saved 25
            _run("Lint", "cancelled", 1),         # no successful Lint run to price it
            _run("Shepherd", "cancelled", 0), _run("Shepherd", "success", 1),  # bookkeeping: skipped
            {**_run("Smoke", "skipped", 0)}]
    c = dashboard.minutes_saved(runs, {"Shepherd"})
    assert c == {"wall_min": 96, "saved_min_est": 25, "cancelled": 2, "unpriced": 1}
    assert round(dashboard.saved_share(c), 3) == round(25 / 121, 3)


def test_the_efficiency_chart_plots_minutes_saved_and_failed_runs_as_percentages():
    data = _data()
    data["days"][TODAY.isoformat()]["detail"] = {"bot_runs": 0, "ci_changes": [], "wait_median": 1.0, "wait_p90": 4.0,
                                                 "runner_min_est": 600, "sampled_runs": 24,
                                                 "cancel_savings": {"wall_min": 900, "saved_min_est": 100,
                                                                    "cancelled": 12, "unpriced": 0}}
    svg = dashboard.render(data, 30, TODAY)
    ET.fromstring(svg)
    assert "10.0% of run minutes saved by early cancellation (est. 100 of 1,000 min; 12 runs cancelled)" in svg
    assert "minutes saved (est.)" in svg and "runs failed" in svg
    md = dashboard.summary_md(data, CFG, 30, TODAY, [])
    assert "| Proof · failed runs |" in md


def test_fetch_day_counts_each_outcome_in_windows_so_a_busy_day_passes_the_2500_cap(monkeypatch):
    def fake(path):  # 1,000 successes and 10 failures per 6-hour window; GitHub stops counting at 2,500
        n = 1000 if "status=success" in path else 10
        return min(2500, n if ".." in path else 4 * n)
    monkeypatch.setattr(dashboard, "gh_count", fake)
    monkeypatch.setattr(dashboard, "merged_counts", lambda repo, day, cfg: {"merged": 0})
    runs = dashboard.fetch_day("owner/repo", "2026-10-18", CFG)["runs"]
    assert runs["success"] == 4000
    assert runs["failure"] == 40
