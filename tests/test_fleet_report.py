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
    assert "runner hours per merged PR" in svg
    assert svg.count("CI change(s) merged") == 3
    # without detail the extra row and chart stay out
    assert "job queue wait" not in dashboard.render(_data(), 30, TODAY)


def test_a_cost_axis_cap_draws_bigger_days_at_the_top_and_marks_them():
    data = _data()
    for i, d in enumerate(sorted(data["days"])):
        data["days"][d]["detail"] = {"bot_runs": 0, "ci_changes": [], "wait_median": 1.0, "wait_p90": 4.0,
                                     "runner_min_est": 60 * 30 * 25 if i == 5 else 600, "sampled_runs": 24}
        if i == 5:
            data["days"][d]["merged"] = 30
    capped = dashboard.render(data, 30, TODAY, dashboard.Config(repo="owner/repo", cost_max=10))
    ET.fromstring(capped)
    assert "▲ 25 h" in capped and "(above the 10 h axis)" in capped  # 750 h over the day's 30 merges, drawn at 10
    uncapped = dashboard.render(data, 30, TODAY)
    assert "▲ 25 h" not in uncapped and "above the" not in uncapped  # no cap: the per-PR axis fits the data
    # CI hours: the 750 h day is over 3x the 10 h median, so its bar is drawn at the top and marked
    assert "2 · CI hours — runner hours per day (est., sampled)" in uncapped and "▲ 750 h" in uncapped


def test_ci_hours_bars_split_by_origin_in_the_tip():
    data = _data()
    for d in sorted(data["days"]):
        data["days"][d]["detail"] = {"bot_runs": 0, "ci_changes": [], "wait_median": 1.0, "wait_p90": 4.0,
                                     "runner_min_est": 1200, "sampled_runs": 24,
                                     "sampled_min_by_origin": {"bot": 30.0, "main": 10.0}}
    svg = dashboard.render(data, 30, TODAY)
    ET.fromstring(svg)
    assert "Oct 13 · 20 runner hours (est.) · 1,840 runs · bot 75%, main 25%" in svg
    assert "▲" not in svg and "runner hours (est.) (today so far)" in svg  # today drawn faded, like every chart


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
    # no shaded band: a start line (and the event lines) through every panel, five without factory.json
    assert 'class="band"' not in svg and svg.count('class="start"') == 5 and svg.count('class="mark"') == 5
    assert "175 of the 280 PRs merged since then were its own" in svg
    # the cards: the last finished day (Oct 13) against the day before, not period averages
    assert "Cards: Oct 13, the last finished UTC day, vs Oct 12." in svg
    assert "PROMOTER-BOT-MADE SHARE" in svg and "62.5%" in svg and "25 PRs · all agents 75.0%" in svg
    assert ">MERGED PRs</text>" in svg and ">40</text>" in svg and "Oct 12: 40 · +0%" in svg
    assert "before promoter-bot:" not in svg and "with promoter-bot (" not in svg  # no period-average cards
    # runner hours per PR without the before/with average lines
    assert "4 · CI load" in svg and 'class="avg"' not in svg and "h / PR with" not in svg
    assert "5 · Efficiency" in svg and "Spend" not in svg  # no factory.json: no dollar panels


def test_the_cards_follow_the_last_finished_day():
    data = _with_bot()
    data["days"]["2026-10-13"]["merged"] = 80  # yesterday doubled: the card says so, not a period average
    svg = dashboard.render(data, 30, TODAY, BOT)
    assert ">80</text>" in svg and "Oct 12: 40 · +100%" in svg
    data["days"]["2026-10-14"] = dict(data["days"]["2026-10-13"], merged=5)  # today so far is not a finished day
    assert "Cards: Oct 13, the last finished UTC day" in dashboard.render(data, 30, TODAY, BOT)


def test_dollar_panels_add_up_from_the_bots_start():
    data = _with_bot()
    data["factory"] = {"days": {"2026-10-07": {"usd": 50.0, "churn_merged": 1},
                                "2026-10-08": {"usd": 30.0, "churn_merged": 10}, "2026-10-09": {"usd": 20.0, "churn_merged": 15}}}
    svg = dashboard.render(data, 30, TODAY, BOT)
    ET.fromstring(svg)
    assert "2 · CI hours" in svg and "Repo clones" not in svg
    # 5 · dollars spent since the start, and 6 · per merged PR with the overall average ($50 / 25 PRs)
    assert "5 · Spend — promoter-bot cost in dollars, cumulative since Oct 8" in svg and ">$50 total</text>" in svg
    assert "25 merged promoter-bot PRs for $50" in svg
    assert "6 · Cost per PR" in svg and "$3.00 per merged PR ($30.00 for 10)" in svg and "avg $2.00 per PR since Oct 8" in svg
    assert "7 · Efficiency" in svg


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


# ---------------------------------------------------------------- sampling (2026-10-24 rework)

import json
import math
import random

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "gitgalaxy_runs_2026-10-23.json").read_text())


def _population(seed: int = 0) -> tuple[list[dict], dict[str, float]]:
    """A day the size of gitgalaxy's 2026-10-23 (7,628 completed runs) drawn from that day's 297 measured runs: the
    real heavy tail and workflow mix, with a known true total."""
    rng = random.Random(seed)
    pop, truth = [], {}
    for i in range(FIXTURE["population"]):
        r = rng.choice(FIXTURE["runs"])
        pop.append({"id": 10_000 + i, "name": r["name"], "wall": r["wall"]})
        truth[str(10_000 + i)] = r["minutes"]
    return pop, truth


def _measure(pop, truth, cfg, seed):
    plan = dashboard.sample_plan(pop, set(), cfg, seed=seed)
    measured = {str(r["id"]): {"m": truth[str(r["id"])]} for r in plan}
    return dashboard.ratio_estimate(pop, measured), len(measured)


def test_old_fixed_24_run_estimate_was_mostly_noise_on_a_real_day():
    """The case the owner caught (564 h on 2026-10-23, pool ceiling 480 h): 24 runs of a heavy-tailed day."""
    pop, truth = _population()
    total = sum(truth.values())
    rng = random.Random(1)
    ids = [str(r["id"]) for r in pop]
    errs = []
    for _ in range(400):
        pick = rng.sample(ids, 24)
        errs.append(abs(sum(truth[i] for i in pick) / 24 * len(pop) - total) / total)
    assert sum(e > 0.25 for e in errs) / len(errs) > 0.5  # most days off by more than 25%


def test_stratified_ratio_sample_is_accurate_and_its_interval_covers_the_truth():
    pop, truth = _population()
    total = sum(truth.values())
    cfg = dashboard.Config(repo="o/r", sample_fraction=0.05, sample_min=30)
    covered, errs = 0, []
    for seed in range(40):
        est, n = _measure(pop, truth, cfg, seed=f"day-{seed}")
        assert n >= math.ceil(0.05 * len(pop))
        errs.append(abs(est["total"] - total) / total)
        covered += abs(est["total"] - total) <= est["half_width"]
    assert sorted(errs)[len(errs) // 2] < 0.12  # median error at 5%
    assert covered >= 32  # the 95% interval covers the truth in >= 80% of 40 days
    est, _ = _measure(pop, truth, cfg, seed="x")
    measured = {k for k in est["weights"]}
    assert abs(sum(truth[k] * w for k, w in est["weights"].items()) - est["total"]) < 1e-6 * est["total"]
    assert abs(sum(est["by_workflow"].values()) - est["total"]) < 1e-6 * est["total"]  # splits add up
    assert measured


def test_sample_plan_scales_with_volume_covers_every_workflow_and_is_stable():
    pop, _ = _population()
    cfg = dashboard.Config(repo="o/r", sample_fraction=0.05, sample_min=30)
    plan = dashboard.sample_plan(pop, set(), cfg, seed="2026-10-23")
    assert len(plan) >= 0.05 * len(pop)
    assert {r["name"] for r in plan} == {r["name"] for r in pop}  # at least one per workflow
    assert {r["name"] for r in plan[:len({r['name'] for r in pop})]} == {r["name"] for r in pop}  # round-robin first
    assert [r["id"] for r in plan] == [r["id"] for r in dashboard.sample_plan(pop, set(), cfg, seed="2026-10-23")]
    half = {str(r["id"]) for r in plan[: len(plan) // 2]}
    rest = dashboard.sample_plan(pop, half, cfg, seed="2026-10-23")
    assert not half & {str(r["id"]) for r in rest} and len(rest) == len(plan) - len(half)  # a re-run tops up only
    small = dashboard.sample_plan(pop[:200], set(), cfg, seed="s")
    assert len(small) >= 30  # the floor on a quiet day


def test_fetch_detail_measures_each_run_once_within_the_budget(monkeypatch):
    runs = [{"id": i, "name": "wf" + str(i % 3), "status": "completed", "conclusion": "success",
             "run_started_at": "2026-10-23T10:00:00Z", "updated_at": "2026-10-23T10:05:00Z", "event": "push",
             "head_branch": "main"} for i in range(1, 401)]
    looked = []
    monkeypatch.setattr(dashboard, "day_runs", lambda repo, day: runs)
    monkeypatch.setattr(dashboard, "gh_count", lambda path: 0)

    def gh(path):
        if "/jobs" in path:
            looked.append(path)
            return {"jobs": [{"started_at": "2026-10-23T10:01:00Z", "completed_at": "2026-10-23T10:04:00Z",
                              "created_at": "2026-10-23T10:00:00Z", "conclusion": "success"}]}
        return []

    monkeypatch.setattr(dashboard, "gh_json", gh)
    cfg = dashboard.Config(repo="o/r", sample_fraction=0.1, sample_min=5)
    budget = [25]
    d1 = dashboard.fetch_detail("o/r", "2026-10-23", {}, lambda r: "main", cfg, None, budget)
    assert budget == [0] and d1["sampled_runs"] == 25 and d1["sample_target"] == 40
    d2 = dashboard.fetch_detail("o/r", "2026-10-23", {}, lambda r: "main", cfg, d1, [100])
    assert d2["sampled_runs"] == 40 and len(looked) == 40  # topped up, nothing measured twice
    assert d2["runner_min_est"] == 3 * 400 and d2["runner_min_ci"] == 0  # every run 3 runner-min: exact
    assert d2["est_method"] == dashboard.EST_METHOD and d2["wait_median"] == 1.0


def test_occupancy_day_is_time_weighted_bounded_and_reports_coverage():
    day = "2026-10-23"
    full = [{"t": f"{day}T{h:02d}:{m:02d}:00Z", "running": 20} for h in range(24) for m in (0, 15, 30, 45)]
    occ = dashboard.occupancy_day(full, day)
    assert occ["coverage"] == 1.0 and occ["runner_min"] == 20 * 1440  # a full pool all day is exactly the ceiling
    gappy = [r for r in full if not r["t"][11:13] in ("03", "04", "05", "06")]  # four hours missing
    occ2 = dashboard.occupancy_day(gappy, day)
    assert 0.8 < occ2["coverage"] < 0.9 and occ2["runner_min"] <= 20 * 1440
    sparse = full[::12]  # every 3 h: mostly uncovered
    assert dashboard.occupancy_day(sparse, day)["coverage"] < dashboard.OCC_MIN_COVERAGE
    detail = {"runner_min_est": 33_840, "runner_min_ci": 9_000, "sampled_runs": 24, "population": 6667,
              "occupancy": dashboard.occupancy_day(sparse, day)}
    assert dashboard.runner_minutes(detail) == 33_840  # occupancy too thin: the sample's figure
    detail["occupancy"] = occ
    assert dashboard.runner_minutes(detail) == 28_800 and dashboard.runner_source(detail).startswith("occupancy")


def test_alert_flags_a_day_over_the_pools_ceiling():
    data = _data(10)
    base = {"wait_median": 1.0, "wait_p90": 3.0, "bot_runs": 0, "ci_changes": []}
    for d in data["days"].values():
        d["detail"] = {**base, "runner_min_est": 400 * 60}
    over = (TODAY - timedelta(days=2)).isoformat()
    data["days"][over]["detail"] = {**base, "runner_min_est": 564 * 60, "runner_min_ci": 300 * 60, "sampled_runs": 24,
                                    "population": 6667}
    alerts = dashboard.check_alerts(data, 10, TODAY, None, None, pool=20)
    assert len(alerts) == 1 and alerts[0].startswith(f"{over}: runner time 564 h is over the pool's 480 h")
    assert dashboard.check_alerts(data, 10, TODAY, None, None, pool=None) == []


def test_occupancy_reading_counts_every_repo_of_the_owner(monkeypatch):
    """Owner policy 2026-10-24: the runner pool is the owner's, shared by all its repos, so all of them are counted."""
    runs = {"o/gitgalaxy": [{"id": 1, "status": "in_progress"}, {"id": 2, "status": "queued"}],
            "o/crucible": [{"id": 3, "status": "in_progress"}], "o/quiet": [{"id": 4, "status": "completed"}]}
    jobs = {1: ["in_progress", "in_progress", "queued"], 3: ["in_progress"]}

    def gh(path):
        if path.startswith("orgs/o/repos"):
            return [{"full_name": n} for n in runs] + [{"full_name": "o/old", "archived": True}]
        if "/jobs" in path:
            return {"jobs": [{"status": s} for s in jobs[int(path.split("/runs/")[1].split("/")[0])]]}
        rs = runs[path.split("/actions")[0].removeprefix("repos/")]
        want = "in_progress" if "status=in_progress" in path else "queued"
        got = [x for x in rs if x["status"] == want]
        return {"workflow_runs": got, "total_count": len(got)}

    monkeypatch.setattr(dashboard, "gh_json", gh)
    r = dashboard.occupancy_reading("o/gitgalaxy", "org")
    assert (r["running"], r["queued"], r["queued_runs"], r["repos_read"]) == (3, 1, 1, 3)
    assert r["by_repo"] == {"o/gitgalaxy": {"running": 2, "queued": 1, "queued_runs": 1},
                            "o/crucible": {"running": 1, "queued": 0, "queued_runs": 0}}
    only = dashboard.occupancy_reading("o/gitgalaxy", "repo")
    assert only["running"] == 2 and list(only["by_repo"]) == ["o/gitgalaxy"]
    day = "2026-10-23"
    readings = [{**r, "t": f"{day}T{h:02d}:{m:02d}:00Z"} for h in range(24) for m in (0, 15, 30, 45)]
    assert dashboard.occupancy_day(readings, day)["runner_min"] == 3 * 1440  # the pool's load
    assert dashboard.occupancy_day(readings, day, repo="o/gitgalaxy")["runner_min"] == 2 * 1440  # this repo's share
    assert dashboard.occupancy_day(readings, day, repo="o/none")["runner_min"] == 0


def _factory_data():
    data = _data(10)
    days = sorted(data["days"])
    data["scorecard"] = {"open_now": 100, "days": {
        d: {"opened": 6, "closed": 4, "resolved": 4, "resolved_by_bot": 4, "not_planned": 0, "reverts": 1 if d == days[-3] else 0,
            "regression_fixes": 0, "opened_by_origin": {"owner": 2, "agent": 3, "automation": 1}} for d in days}}
    data["factory"] = {"days": {d: {"usd": 40.0, "first_pass": {"merged": 20, "first_pass": 15},
                                    "worker_hours": {"new_work": 1.0}} for d in days}}
    return data


def test_dashboard_draws_issue_arrows_first_pass_change_failure_and_cost_per_issue():
    """Owner 2026-10-24: issues opened as arrows up stacked by who opened them (people purple, agents blue), closed as a
    green arrow down, a pair per day; then first-pass, change failure and cost per resolved issue over time."""
    svg = dashboard.render(_factory_data(), 10, TODAY, dashboard.Config(repo="owner/repo"))
    ET.fromstring(svg)
    for needle in ("Issues — opened ▲ (by who) and closed ▼", 'class="people"', 'class="agents"', 'class="closed"',
                   "First-pass rate", 'class="band"', "Change failure", "Cost per resolved issue"):
        assert needle in svg, needle
    assert "opened 6 (people 2, agents 3, bots 1); closed 4 as completed" in svg
    assert "75% first pass (15 of 20" in svg  # exact from the factory's records, with its 95% band
    assert "$10.00 per issue, average of the last 7 day(s)" in svg  # $40 / 4 issues, paired day by day


def test_goals_sheet_renders_any_goal_in_the_generic_shape():
    data = _data(3)
    data["factory"] = {"goals": [{
        "id": "x", "title": "Services migrated", "unit": "services", "levels": ["S0", "S1", "S2"], "target": "S2",
        "group_label": "team", "measured": "2026-10-14T10:00Z",
        "history": {"2026-10-12": {"S0": 5, "S1": 3, "S2": 2}, "2026-10-14": {"S0": 2, "S1": 3, "S2": 5}},
        "groups": {"payments": {"S2": 3}, "search": {"S0": 2, "S1": 3, "S2": 2}},
        "blockers": [{"name": "svc/ledger.py", "group": "payments", "holes": 2, "note": "needs a schema", "issues": []}]}]}
    svg = dashboard.render_goals(data, TODAY, dashboard.Config(repo="owner/repo"))
    ET.fromstring(svg)
    assert "Services migrated" in svg and "5 of 10" in svg and "+3" in svg  # at target now, change since the first day
    assert "1 of 2" in svg and "3/3" in svg and "2/7" in svg  # teams done; per-team counts
    assert 'class="noissue"' in svg and "ledger.py" in svg  # a blocker nobody is on
    assert dashboard.render_goals(_data(3), TODAY, dashboard.Config(repo="owner/repo")) is None


def test_cost_per_issue_starts_at_the_first_bot_resolved_day_and_averages_only_days_it_has():
    """Owner 2026-10-24: no zero-filled days and no false trend: the bot's spend over the issues it did the work on,
    from the first day it resolved one; day 1 is one day's figure, day 2 the average of two."""
    data = _factory_data()
    days = sorted(data["days"])
    data["scorecard"]["days"][days[0]]["resolved_by_bot"] = 0  # the bot's first part-day: people resolved them
    data["factory"]["days"][days[1]]["usd"] = 20.0  # day 2: $20 / 4 = $5
    svg = dashboard.render(data, 10, TODAY, dashboard.Config(repo="owner/repo"))
    assert "$5.00 per issue, average of the last 1 day(s)" in svg  # the line starts on day 2, alone
    assert "$7.50 per issue, average of the last 2 day(s)" in svg  # ($20 + $40) / 8
    first_day = dashboard._fmt_day(days[0])
    assert f"{first_day}: $40 / 0 issues" not in svg and f"{first_day}: $" not in svg  # no dot for day 1 at all


def test_an_issue_counts_as_bot_resolved_only_if_bot_work_merged_before_it_closed():
    cfg = dashboard.Config(repo="o/r", bot_prefixes=["churn/"])
    ev = lambda branch, merged_at: {"source": {"merged": True, "headRefName": branch, "mergedAt": merged_at}}  # noqa: E731
    node = lambda *evs: {"closedAt": "2026-10-12T10:00:00Z", "timelineItems": {"nodes": list(evs)}}  # noqa: E731
    assert dashboard.bot_worked(node(ev("churn/1-x", "2026-10-12T09:00:00Z")), cfg)
    assert dashboard.bot_worked(node(ev("churn/1-x", "2026-10-12T10:30:00Z")), cfg)  # merge, then the close follows
    assert not dashboard.bot_worked(node(ev("churn/9-later", "2026-10-15T09:00:00Z")), cfg)  # a later slice only mentions it
    assert not dashboard.bot_worked(node(ev("feat/human", "2026-10-12T09:00:00Z")), cfg)
    assert dashboard.bot_worked(node({"closer": {"headRefName": "churn/2-y"}}), cfg)  # closed by a bot PR


def test_api_panel_shows_the_busiest_hour_against_the_limit_and_exhausted_days():
    data = _factory_data()
    days = sorted(data["days"])
    data["factory"]["days"][days[-2]]["api"] = {"peak_hour": 5000, "readings_at_zero": 3, "churn_calls": 900,
                                                "churn_by_caller": {"pr_snapshot": 400, "inflight_files": 300}}
    data["factory"]["days"][days[-3]]["api"] = {"peak_hour": 3100, "readings_at_zero": 0, "churn_calls": 500}
    svg = dashboard.render(data, 10, TODAY, dashboard.Config(repo="owner/repo"))
    ET.fromstring(svg)
    assert "GitHub API — calls in the busiest hour" in svg and "limit 5,000/h" in svg
    assert "3×0" in svg and "busiest hour 5,000 calls; 3 readings at 0; churn made 900 calls" in svg


def test_writes_are_counted_per_account_and_hour_and_drawn_against_the_guidance(monkeypatch):
    """Owner 2026-10-24: secondary limits count content-creating writes per account (~500/h): watch them."""
    day = "2026-10-13"
    comments = [{"user": {"login": "me"}, "created_at": f"{day}T03:0{i}:00Z"} for i in range(5)] + \
               [{"user": {"login": "shep[bot]"}, "created_at": f"{day}T04:00:00Z"}]
    events = [{"event": "labeled", "actor": {"login": "me"}, "created_at": f"{day}T03:30:00Z"},
              {"event": "subscribed", "actor": {"login": "me"}, "created_at": f"{day}T03:31:00Z"},  # not a write
              {"event": "merged", "actor": {"login": "shep[bot]"}, "created_at": f"{day}T04:10:00Z"},
              {"event": "labeled", "actor": {"login": "me"}, "created_at": "2026-10-12T23:00:00Z"}]  # the day before
    monkeypatch.setattr(dashboard, "gh_json", lambda path: comments if "comments" in path else events)
    monkeypatch.setattr(dashboard, "gh_graphql", lambda query, **v: {"search": {
        "nodes": [{"createdAt": f"{day}T03:45:00Z", "author": {"login": "me"}}] if "is:pr" in v["q"] else [],
        "pageInfo": {"hasNextPage": False}}})
    w = dashboard.fetch_writes_day("o/r", day)
    assert w["me"] == {"total": 7, "busiest_hour": 7, "hours": {"03": 7}}  # 5 comments, 1 label, 1 PR
    assert w["shep[bot]"]["total"] == 2
    data = _factory_data()
    for d in data["scorecard"]["days"]:
        data["scorecard"]["days"][d]["writes"] = w
    svg = dashboard.render(data, 10, TODAY, dashboard.Config(repo="owner/repo"))
    assert "GitHub writes — each account" in svg and "me: busiest hour 7 writes, 7 that day" in svg
