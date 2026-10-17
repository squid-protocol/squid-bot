#!/usr/bin/env python3
"""Fleet CI report: what an agent fleet costs and congests in GitHub Actions, per unit of output.

Counted exactly (GitHub API):
  merged PRs per day      GraphQL search, each PR classed agent / automation / other (see classify_pr)
  Actions runs per day     actions/runs?created=DAY, total and per conclusion
  bot workflow runs        per configured bot workflow (an orchestrator's own bookkeeping), if any
  CI changes merged        PR merges to the default branch that touched .github/workflows (chart markers)
  open PRs right now       search API
Sampled (about --sample-runs runs a day, spread over the day; labelled "sampled" / "est." wherever shown):
  job queue wait           job created -> picked up by a runner, median and p90
  runner minutes           mean job minutes per run x the day's runs, split by workflow and by origin
                           (agent PR / other PR / automation PR / default branch and schedules)

History accumulates in <out>/metrics.json: a finished day is fetched once, the last REFRESH_DAYS days are
re-fetched on every run, and older days get the sampled detail a few per run (--detail-backfill).
Writes <out>/dashboard.svg (light/dark) and, with --summary, a Markdown report (the Action points it at
$GITHUB_STEP_SUMMARY). Needs `gh`, authenticated or with GH_TOKEN set.

Usage:
    python fleet_report.py --repo owner/repo [--out dashboard] [--summary report.md]
    python fleet_report.py --render-only --out dashboard     # redraw from metrics.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

REFRESH_DAYS = 3  # today, yesterday and the day before can still change
OUTCOMES = ("success", "failure", "cancelled", "skipped")
DETAIL_BACKFILL = 4  # older days missing the sampled detail, filled a few per run (API budget)
DEFAULT_AGENT_PREFIXES = "claude/,codex/,copilot/,cursor/,devin/"
DEFAULT_AGENT_MARKERS = (r"Generated with \[?Claude Code|Co-Authored-By: Claude|Co-authored-by: Copilot|"
                         r"agy-authored|Devin AI|Cursor Agent|OpenAI Codex|Gemini CLI")
CLASSES = ("bot", "agent", "other", "automation")
SHOW_BEFORE = 14  # days before the bot started kept on the charts and used as its "before" baseline
MAX_DAYS = 90


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _events(s: str) -> list[tuple[str, str]]:
    """'2026-10-10: shepherd on; 2026-10-12: paused' -> [(day, label), ...]"""
    out = []
    for item in s.split(";"):
        day, _, label = item.partition(":")
        if day.strip() and label.strip():
            out.append((day.strip(), label.strip()))
    return out


@dataclass
class Config:
    repo: str
    title: str = ""
    bot_workflows: list[str] = field(default_factory=list)
    agent_prefixes: list[str] = field(default_factory=lambda: _csv(DEFAULT_AGENT_PREFIXES))
    agent_markers: str = DEFAULT_AGENT_MARKERS
    sample_runs: int = 24
    bot_prefixes: list[str] = field(default_factory=list)
    bot_name: str = "bot"
    bot_start: str = ""
    events: list[tuple[str, str]] = field(default_factory=list)

    @property
    def heading(self) -> str:
        return self.title or f"{self.repo}: CI activity"


# ---------------------------------------------------------------- GitHub API

def _gh(cmd: list[str], tries: int = 3) -> str:
    """Runs a gh call, retrying transient failures (a 5xx or a dropped connection) with a short backoff."""
    for attempt in range(tries):
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode == 0:
            return out.stdout
        if attempt < tries - 1:
            time.sleep(2 * 3 ** attempt)
    raise RuntimeError(f"{' '.join(cmd[:3])} {cmd[-1][:120]} failed: {out.stderr.strip()[:200]}")


def gh_json(path: str):
    return json.loads(_gh(["gh", "api", "-H", "Accept: application/vnd.github+json", path]))


def gh_count(path: str) -> int:
    return int(gh_json(path)["total_count"])


def gh_graphql(query: str, **variables: str) -> dict:
    cmd = ["gh", "api", "graphql"]
    for k, v in variables.items():
        cmd += ["-f", f"{k}={v}"]
    return json.loads(_gh(cmd + ["-f", f"query={query}"]))["data"]


def _iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


# ---------------------------------------------------------------- classification

def classify_pr(pr: dict, cfg: Config) -> str:
    """bot: one of the bot's own branch prefixes (the orchestrator this dashboard follows); agent: an agent branch
    prefix or an agent marker in the body; automation: any other bot account (dependabot, renovate, release bots);
    other: everything else (people, or agents that leave no trace)."""
    branch = pr.get("headRefName") or ""
    if any(branch.startswith(p) for p in cfg.bot_prefixes):
        return "bot"
    if any(branch.startswith(p) for p in cfg.agent_prefixes):
        return "agent"
    if cfg.agent_markers and re.search(cfg.agent_markers, pr.get("bodyText") or "", re.I):
        return "agent"
    author = pr.get("author") or {}
    if author.get("__typename") == "Bot" or str(author.get("login", "")).endswith("[bot]"):
        return "automation"
    return "other"


SEARCH_PRS = """query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { number headRefName bodyText author { login __typename } } }
  }
}"""


def merged_prs(repo: str, day: str) -> list[dict]:
    prs, after = [], None
    while True:
        vars_ = {"q": f"repo:{repo} is:pr is:merged merged:{day}"}
        if after:
            vars_["after"] = after
        s = gh_graphql(SEARCH_PRS, **vars_)["search"]
        prs += [n for n in s["nodes"] if n]
        if not s["pageInfo"]["hasNextPage"]:
            return prs
        after = s["pageInfo"]["endCursor"]


def merged_counts(repo: str, day: str, cfg: Config) -> dict:
    c = Counter(classify_pr(p, cfg) for p in merged_prs(repo, day))
    return {"merged": sum(c.values()), "merged_by_class": {k: c.get(k, 0) for k in CLASSES}}


class RunOrigin:
    """Which kind of PR a workflow run worked for: one PR lookup per PR number, cached for the whole collect."""

    def __init__(self, repo: str, cfg: Config):
        self.repo, self.cfg, self.cache = repo, cfg, {}
        self.owner = repo.split("/")[0]
        self.default_branch = gh_json(f"repos/{repo}")["default_branch"]

    def _classify(self, p: dict) -> str:
        return classify_pr({"headRefName": p["head"]["ref"], "bodyText": p.get("body") or "",
                            "author": {"login": p["user"]["login"],
                                       "__typename": "Bot" if p["user"]["type"] == "Bot" else "User"}}, self.cfg)

    def __call__(self, run: dict) -> str:
        branch = run.get("head_branch") or ""
        if branch == self.default_branch or run.get("event") in ("schedule", "dynamic") or branch.startswith("gh-pages"):
            return "main"
        prs = run.get("pull_requests") or []
        # GitHub often leaves pull_requests empty even on PR runs: then find the PR by its head branch
        key = f"#{prs[0]['number']}" if prs else f"branch:{branch}"
        if key not in self.cache:
            if prs:
                found = [gh_json(f"repos/{self.repo}/pulls/{prs[0]['number']}")]
            else:
                found = gh_json(f"repos/{self.repo}/pulls?head={self.owner}:{branch}&state=all&per_page=1")
            self.cache[key] = self._classify(found[0]) if found else classify_pr({"headRefName": branch}, self.cfg)
        return self.cache[key]


# ---------------------------------------------------------------- fetching

def bot_workflow_ids(repo: str, cfg: Config) -> dict[str, int]:
    if not cfg.bot_workflows:
        return {}
    wfs = gh_json(f"repos/{repo}/actions/workflows?per_page=100")["workflows"]
    return {w["name"]: w["id"] for w in wfs if w["name"] in cfg.bot_workflows}


def rerun_counts(repo: str, day: str) -> dict:
    """Exact: the day's runs that were rerun (run_attempt > 1) and how their latest attempt ended, per workflow.
    A rerun that failed again usually retried a real failure; one that passed was a flake or an outage."""
    by_wf: dict[str, list[int]] = {}
    attempts = 0
    for h in range(0, 24, 2):  # a listing stops at 1,000 runs: 2-hour windows stay under it on a busy day
        span = f"{day}T{h:02d}:00:00Z..{day}T{h + 1:02d}:59:59Z"
        for page in range(1, 11):
            runs = gh_json(f"repos/{repo}/actions/runs?created={span}&per_page=100&page={page}")["workflow_runs"]
            for r in runs:
                if (r.get("run_attempt") or 1) > 1 and r.get("status") == "completed":
                    attempts += r["run_attempt"] - 1
                    c = by_wf.setdefault(r["name"], [0, 0, 0])
                    c[{"success": 0, "failure": 1}.get(r.get("conclusion"), 2)] += 1
            if len(runs) < 100:
                break
    return {"runs": sum(sum(c) for c in by_wf.values()), "attempts": attempts,
            "passed": sum(c[0] for c in by_wf.values()), "failed_again": sum(c[1] for c in by_wf.values()),
            "by_workflow": by_wf}


def fetch_detail(repo: str, day: str, total_runs: int, bots: dict[str, int], origin, cfg: Config) -> dict:
    """Bot-run count and CI-change PRs (exact); job queue wait and runner minutes (sampled)."""
    bot_runs = sum(gh_count(f"repos/{repo}/actions/workflows/{i}/runs?created={day}&per_page=1") for i in bots.values())
    changes: dict[int, str] = {}
    for c in gh_json(f"repos/{repo}/commits?path=.github/workflows&since={day}T00:00:00Z&until={day}T23:59:59Z&per_page=100"):
        title = c["commit"]["message"].splitlines()[0]
        m = re.search(r"\(#(\d+)\)\s*$", title)
        if m:  # squash merges of PRs; direct bot commits carry no PR number
            changes[int(m.group(1))] = title
    waits: list[float] = []
    run_min: list[float] = []
    by_wf: Counter[str] = Counter()
    by_origin: Counter[str] = Counter()
    per_window = max(1, cfg.sample_runs // 4)
    for h in (0, 6, 12, 18):
        page = gh_json(f"repos/{repo}/actions/runs?created={day}T{h:02d}:00:00Z..{day}T{h + 5:02d}:59:59Z&per_page=100")
        runs = [r for r in page["workflow_runs"] if r.get("conclusion") != "skipped" and r.get("status") == "completed"]
        step = max(1, len(runs) // per_window)
        for r in runs[::step][:per_window]:
            try:
                jobs = gh_json(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100")["jobs"]
            except RuntimeError as e:  # one unreadable run shrinks the sample, it doesn't stop the report
                print(f"::notice::skipped run {r['id']} in the sample: {e}", file=sys.stderr)
                continue
            minutes = 0.0
            for j in jobs:
                if j.get("started_at") and j.get("completed_at") and j.get("conclusion") != "skipped":
                    m = (_iso(j["completed_at"]) - _iso(j["started_at"])).total_seconds() / 60
                    if m > 0.05:  # a job that never really ran held no runner
                        minutes += m
                        waits.append(max(0.0, (_iso(j["started_at"]) - _iso(j["created_at"])).total_seconds() / 60))
            run_min.append(minutes)
            by_wf[r["name"]] += minutes
            by_origin[origin(r)] += minutes
    return {
        "bot_runs": bot_runs,
        "ci_changes": [{"pr": n, "title": t} for n, t in sorted(changes.items())],
        "wait_median": round(_pct(waits, 0.5), 2),
        "wait_p90": round(_pct(waits, 0.9), 2),
        "runner_min_est": round(sum(run_min) / len(run_min) * total_runs) if run_min else None,
        "sampled_runs": len(run_min),
        "sampled_min_by_workflow": {k: round(v, 1) for k, v in by_wf.items()},
        "sampled_min_by_origin": {k: round(v, 1) for k, v in by_origin.items()},
        "reruns": rerun_counts(repo, day),
    }


def fetch_day(repo: str, day: str, cfg: Config) -> dict:
    # total_count tops out at 2,500 per query, a busy day passes that: count it in 6-hour windows
    runs = {"total": sum(
        gh_count(f"repos/{repo}/actions/runs?created={day}T{h:02d}:00:00Z..{day}T{h + 5:02d}:59:59Z&per_page=1")
        for h in (0, 6, 12, 18)
    )}
    for outcome in OUTCOMES:
        runs[outcome] = gh_count(f"repos/{repo}/actions/runs?created={day}&status={outcome}&per_page=1")
    return {**merged_counts(repo, day, cfg), "runs": runs}


def days_to_fetch(have: dict, today: date, days: int) -> list[str]:
    window = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    fresh = {(today - timedelta(days=i)).isoformat() for i in range(REFRESH_DAYS)}
    return [d for d in window if d not in have or d in fresh]


def collect(cfg: Config, days: int, data: dict, today: date, backfill: int = DETAIL_BACKFILL) -> dict:
    repo = cfg.repo
    if data.get("repo") != repo:
        data = {"repo": repo, "days": {}}
    days = shown_days(data, cfg, days, today)
    fresh = days_to_fetch(data["days"], today, days)
    for d in fresh:
        data["days"][d] = fetch_day(repo, d, cfg)
    window = [(today - timedelta(days=i)).isoformat() for i in range(days)]
    for d in window:  # history from before PR classing (or before the bot class): one GraphQL query a day fills it in
        classes = data["days"].get(d, {}).get("merged_by_class")
        if d in data["days"] and (classes is None or (cfg.bot_prefixes and "bot" not in classes)):
            data["days"][d].update(merged_counts(repo, d, cfg))
    bots = bot_workflow_ids(repo, cfg)
    for d in window:  # the bot workflows' own cancelled runs (a newer bookkeeping run replaced them): kept out of chart 4
        if bots and d in data["days"] and "bot_cancelled" not in data["days"][d]:
            data["days"][d]["bot_cancelled"] = sum(
                gh_count(f"repos/{repo}/actions/workflows/{i}/runs?created={d}&status=cancelled&per_page=1") for i in bots.values())
    origin = RunOrigin(repo, cfg)
    missing = [d for d in window if d not in fresh and d in data["days"]
               and not {"sampled_min_by_origin", "reruns"} <= data["days"][d].get("detail", {}).keys()]
    for d in fresh + missing[:backfill]:
        r = data["days"][d]["runs"]
        try:
            data["days"][d]["detail"] = fetch_detail(repo, d, r["total"] - r["skipped"], bots, origin, cfg)
        except RuntimeError as e:  # keep the day's previous detail (if any); the next run tries again
            print(f"::warning::sampled detail for {d} not refreshed: {e}", file=sys.stderr)
    data["open_prs"] = gh_count(f"search/issues?q=repo:{repo}+is:pr+is:open&per_page=1")
    data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return data


# ---------------------------------------------------------------- aggregation

def bot_start(data: dict, cfg: Config) -> str | None:
    """The day the bot went live: configured, else the first day with a bot-made merge."""
    if cfg.bot_start:
        return cfg.bot_start
    live = sorted(d for d, r in data.get("days", {}).items() if (r.get("merged_by_class") or {}).get("bot"))
    return live[0] if live else None


def shown_days(data: dict, cfg: Config, days: int, today: date) -> int:
    """Days on the charts: the configured window, stretched so the bot's start and SHOW_BEFORE days before it stay in view."""
    start = bot_start(data, cfg)
    if not start or start > today.isoformat():
        return days
    return min(MAX_DAYS, max(days, (today - date.fromisoformat(start)).days + SHOW_BEFORE + 1))


def compare(data: dict, cfg: Config, days: int, today: date) -> dict:
    """The two periods the cards and the report compare: with the bot vs the SHOW_BEFORE days before it, or (no bot)
    the last 7 finished days vs the 7 before."""
    start = bot_start(data, cfg)
    if start and start <= today.isoformat():
        rows = sorted(data["days"].items())
        first = (date.fromisoformat(start) - timedelta(days=SHOW_BEFORE)).isoformat()
        before = [(d, r) for d, r in rows if first <= d < start]
        with_bot = [(d, r) for d, r in rows if start <= d < today.isoformat()]
        if before or with_bot:
            return {"start": start, "cur": week_stats([r for _, r in with_bot]), "prev": week_stats([r for _, r in before]),
                    "cur_days": [d for d, _ in with_bot], "prev_days": [d for d, _ in before],
                    "cur_label": f"with {cfg.bot_name}", "prev_label": f"before {cfg.bot_name}"}
    done = [(d, r) for d, r in window_rows(data, days, today) if d != today.isoformat()]
    return {"start": None, "cur": week_stats([r for _, r in done[-7:]]), "prev": week_stats([r for _, r in done[-14:-7]]),
            "cur_days": [d for d, _ in done[-7:]], "prev_days": [d for d, _ in done[-14:-7]],
            "cur_label": "last 7 days", "prev_label": "previous 7 days"}


def window_rows(data: dict, days: int, today: date) -> list[tuple[str, dict]]:
    window = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    return [(d, data["days"][d]) for d in window if d in data["days"]]


def finished(rows: list[tuple[str, dict]], today: date) -> list[dict]:
    return [r for d, r in rows if d != today.isoformat()]


def week_stats(rows: list[dict]) -> dict:
    """Aggregates over finished days; None where a figure has no data."""
    n = len(rows)
    if not n:
        return {}
    merged = sum(r["merged"] for r in rows)
    classed = [r for r in rows if "merged_by_class" in r]
    agent = sum(r["merged_by_class"].get("agent", 0) + r["merged_by_class"].get("bot", 0) for r in classed)
    bot = sum(r["merged_by_class"].get("bot", 0) for r in classed)
    runs = sum(r["runs"]["total"] for r in rows)
    ok = sum(r["runs"]["success"] for r in rows)
    bad = sum(r["runs"]["failure"] for r in rows)
    det = [r["detail"] for r in rows if "detail" in r]
    est = [(r["detail"]["runner_min_est"], r["merged"]) for r in rows if r.get("detail", {}).get("runner_min_est") is not None]
    return {
        "days": n,
        "merged_per_day": merged / n,
        "agent_share": agent / max(1, sum(r["merged"] for r in classed)) if classed else None,
        "bot_merged": bot,
        "bot_merge_share": bot / max(1, sum(r["merged"] for r in classed)) if classed else None,
        "runs_per_day": runs / n,
        "pass_rate": ok / (ok + bad) if ok + bad else None,
        "wait_median": sum(x["wait_median"] for x in det) / len(det) if det else None,
        "wait_p90": sum(x["wait_p90"] for x in det) / len(det) if det else None,
        "runner_h_per_pr": sum(m for m, _ in est) / max(1, sum(k for _, k in est)) / 60 if est else None,
        "bot_share": sum(x["bot_runs"] for x in det) / max(1, sum(r["runs"]["total"] for r in rows if "detail" in r)) if det else None,
        "ci_changes": [c for x in det for c in x["ci_changes"]],
        **rerun_stats(det),
    }


def rerun_stats(det: list[dict]) -> dict:
    """Reruns per day and the share whose latest attempt passed, over the days that counted them."""
    rr = [x["reruns"] for x in det if "reruns" in x]
    if not rr:
        return {"reruns_per_day": None, "rerun_pass_rate": None}
    passed, failed = sum(x["passed"] for x in rr), sum(x["failed_again"] for x in rr)
    return {"reruns_per_day": sum(x["runs"] for x in rr) / len(rr),
            "rerun_pass_rate": passed / (passed + failed) if passed + failed else None}


def rerun_split(rows: list[dict]) -> list[tuple[str, int, int, int]]:
    """(workflow, reruns, passed, failed again) over the rows, most rerun first."""
    c: dict[str, list[int]] = {}
    for r in rows:
        for k, v in r.get("detail", {}).get("reruns", {}).get("by_workflow", {}).items():
            s = c.setdefault(k, [0, 0, 0])
            for i in range(3):
                s[i] += v[i]
    return sorted(((k, sum(v), v[0], v[1]) for k, v in c.items()), key=lambda t: (-t[1], t[0]))


def factory_rows(data: dict, today: date, n: int = 7) -> list[tuple[str, dict]]:
    """The last n finished days of the agent factory's own numbers (factory.json, published beside metrics.json by an
    orchestrator such as churn): worker-hours by kind, slot use, fix loops, pauses, idle ticks."""
    days = (data.get("factory") or {}).get("days") or {}
    return [(d, days[d]) for d in sorted(days)
            if d < today.isoformat() and sum((days[d].get("worker_hours") or {}).values())][-n:]  # no runs: no data yet


def factory_stats(rows: list[tuple[str, dict]]) -> dict:
    """Averages per day over the rows (worker-hour shares weighted by hours)."""
    if not rows:
        return {}
    wh = Counter()
    for _, r in rows:
        wh.update(r.get("worker_hours") or {})
    worked = sum(wh.values())
    loops, merged = sum(r.get("loop_runs") or 0 for _, r in rows), sum(r.get("churn_merged") or 0 for _, r in rows)
    avg = lambda k: sum(r.get(k) or 0 for _, r in rows) / len(rows)
    slots = [r["slot_use"] for _, r in rows if r.get("slot_use") is not None]
    return {
        "days": len(rows), "new_work_share": wh["new_work"] / worked if worked else None,
        "fix_h": (wh["pr_fix"]) / len(rows), "sendback_h": (wh["send_back"] + wh["precheck_retry"]) / len(rows),
        "slot_use": sum(slots) / len(slots) if slots else None, "max_workers": rows[-1][1].get("max_workers"),
        "loops_per_pr": loops / merged if merged else None, "paused_h": avg("paused_hours"), "ci_busy_h": avg("ci_busy_hours"),
        "idle_h": avg("idle_ticks") * 10 / 60,  # one gate tick every ~10 min
        "precheck_sent_back": sum((r.get("precheck") or {}).get("sent_back", 0) for _, r in rows),
        "precheck_failed_again": sum((r.get("precheck") or {}).get("failed_again", 0) for _, r in rows),
    }


def runner_split(rows: list[dict], key: str) -> list[tuple[str, float]]:
    """Share of sampled runner minutes by workflow or by origin, largest first."""
    c: Counter[str] = Counter()
    for r in rows:
        for k, v in r.get("detail", {}).get(key, {}).items():
            c[k] += v
    tot = sum(c.values())
    return [(k, v / tot) for k, v in c.most_common()] if tot else []


# ---------------------------------------------------------------- Markdown summary and alerts

def _fmt(v, kind: str) -> str:
    if v is None:
        return "–"
    return {"pct": f"{100 * v:.1f}%", "min": f"{v:.1f} min", "h": f"{v:.1f} h", "n": f"{v:,.1f}"}[kind]


def _md(s: str) -> str:
    return s.replace("|", "\\|")


def summary_md(data: dict, cfg: Config, days: int, today: date, alerts: list[str]) -> str:
    days = shown_days(data, cfg, days, today)
    done = finished(window_rows(data, days, today), today)
    cmp = compare(data, cfg, days, today)
    cur, prev = cmp["cur"], cmp["prev"]
    span = lambda ds: f"{ds[0]} to {ds[-1]}, {len(ds)} days" if ds else "no days"  # noqa: E731
    period = (f"{cfg.bot_name} live since {cmp['start']}: {cmp['cur_label']} ({span(cmp['cur_days'])}) vs "
              f"{cmp['prev_label']} ({span(cmp['prev_days'])})." if cmp["start"] else "Last 7 finished days vs the 7 before.")
    out = [f"## {cfg.heading}", "",
           f"Updated {data['updated'].replace('T', ' ').replace('Z', ' UTC')}. {period} "
           f"Days are UTC; queue wait and runner time are sampled from about {cfg.sample_runs} runs a day.", ""]
    if alerts:
        out += ["> [!WARNING]", *[f"> {a}" for a in alerts], ""]
    out += [f"| Measure | {cmp['cur_label'].capitalize()} | {cmp['prev_label'].capitalize()} |", "|---|---|---|"]
    lines = [("Delivery · merged PRs / day", "merged_per_day", "n"),
             *([(f"Delivery · {cfg.bot_name}-made share", "bot_merge_share", "pct")] if cmp["start"] else []),
             ("Delivery · agent-classified share", "agent_share", "pct"),
             ("Proof · CI workflow-run pass rate", "pass_rate", "pct"),
             ("Reruns / day", "reruns_per_day", "n"),
             ("Reruns whose latest attempt passed", "rerun_pass_rate", "pct"),
             ("Capacity · queue wait median (sampled)", "wait_median", "min"),
             ("Capacity · queue wait p90 (sampled)", "wait_p90", "min"),
             ("Capacity · runner hours per merged PR (est.)", "runner_h_per_pr", "h")]
    if cfg.bot_workflows:
        lines.append(("Context · configured bot workflow share", "bot_share", "pct"))
    for label, k, kind in lines:
        out.append(f"| {label} | {_fmt(cur.get(k), kind)} | {_fmt(prev.get(k), kind)} |")
    out.append(f"| Context · CI changes merged | {len(cur.get('ci_changes', []))} | {len(prev.get('ci_changes', []))} |")
    if cfg.bot_workflows:
        out += ["", f"Configured bot workflow share of runs (last 7 days): {_fmt(cur.get('bot_share'), 'pct')}."]
    wf = runner_split(done[-7:], "sampled_min_by_workflow")
    if wf:
        out += ["", "### Runner time by workflow (sampled, last 7 days)", "", "| Workflow | Share |", "|---|---|"]
        out += [f"| {_md(k)} | {100 * v:.1f}% |" for k, v in wf[:12]]
    rr = rerun_split(done[-7:])
    if rr:
        out += ["", "### Reruns by workflow (last 7 days)", "",
                "A rerun that failed again usually retried a real failure: runner time spent learning nothing.", "",
                "| Workflow | Reruns | Passed | Failed again |", "|---|---|---|---|"]
        out += [f"| {_md(k)} | {n} | {p} | {f} |" for k, n, p, f in rr[:12]]
    fr = factory_rows(data, today)
    if fr:
        out += ["", "### Agent factory (from the orchestrator's own logs)", "",
                "Where the agents' time went. New work makes PRs; PR fixes and send-backs are loops. Idle = a free worker "
                "slot with nothing launchable.", "",
                "| Day | Worker-h | New work | Fix loops / merged PR | Slots used | Paused h | CI-busy h | Idle h |",
                "|---|---|---|---|---|---|---|---|"]
        for d, r in fr:
            w = r.get("worker_hours") or {}
            out.append(f"| {d} | {sum(w.values()):.1f} | {_fmt(r.get('new_work_share'), 'pct')} | "
                       f"{_fmt(r.get('loops_per_merged_pr'), 'n')} | {_fmt(r.get('slot_use'), 'pct')} | "
                       f"{r.get('paused_hours', 0):.1f} | {r.get('ci_busy_hours', 0):.1f} | {(r.get('idle_ticks') or 0) / 6:.1f} |")
    org = runner_split(done[-7:], "sampled_min_by_origin")
    if org:
        names = {"bot": f"{cfg.bot_name} PRs", "agent": "Agent PRs", "other": "Other PRs", "automation": "Automation PRs",
                 "main": "Default branch & schedules"}
        out += ["", "### Runner time by origin (sampled, last 7 days)", "", "| Origin | Share |", "|---|---|"]
        out += [f"| {names.get(k, k)} | {100 * v:.1f}% |" for k, v in org]
    if cur.get("ci_changes"):
        out += ["", "### CI changes merged (last 7 days)", ""]
        out += [f"- #{c['pr']}: {c['title']}" for c in cur["ci_changes"][-25:]]
        if len(cur["ci_changes"]) > 25:
            out.append(f"- …and {len(cur['ci_changes']) - 25} earlier in the week")
    out += ["", "<sub>Exact: merged PRs, run counts and outcomes, bot runs, CI changes, reruns. Sampled: queue wait and runner "
            "time. Agent PRs (bot PRs included): an agent branch prefix or an agent marker in the PR body.</sub>", ""]
    return "\n".join(out)


def check_alerts(data: dict, days: int, today: date, max_wait_p90: float | None, max_h_per_pr: float | None) -> list[str]:
    cur = week_stats(finished(window_rows(data, days, today), today)[-7:])
    out = []
    if max_wait_p90 is not None and cur.get("wait_p90") is not None and cur["wait_p90"] > max_wait_p90:
        out.append(f"Job queue wait p90 averaged {cur['wait_p90']:.1f} min over the last 7 days (limit {max_wait_p90:g}).")
    if max_h_per_pr is not None and cur.get("runner_h_per_pr") is not None and cur["runner_h_per_pr"] > max_h_per_pr:
        out.append(f"Runner time per merged PR was {cur['runner_h_per_pr']:.1f} h over the last 7 days (limit {max_h_per_pr:g}).")
    return out


# ---------------------------------------------------------------- SVG rendering

STYLE = """
  .root { --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781; --grid:#e1e0d9;
          --axis:#c3c2b7; --border:rgba(11,11,11,0.10); --tile:#f4f3f0; --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
          --neutral:#c3c2b7; --band:rgba(42,120,214,0.07); --failed:#d03b3b; }
  @media (prefers-color-scheme: dark) {
    .root { --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781; --grid:#2c2c2a;
            --axis:#383835; --border:rgba(255,255,255,0.10); --tile:#232322; --s1:#3987e5; --s2:#d95926; --s3:#199e70;
            --neutral:#5c5c58; --band:rgba(57,135,229,0.12); }
  }
  text { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; fill: var(--ink); }
  .bg { fill: var(--surface); stroke: var(--border); }
  .tile { fill: var(--tile); }
  .h1 { font-size: 16px; font-weight: 650; }
  .lead { font-size: 12.5px; fill: var(--ink2); }
  .h2 { font-size: 12.5px; font-weight: 600; }
  .meta, .tick, .lbl { font-size: 10.5px; fill: var(--muted); }
  .tick { font-variant-numeric: tabular-nums; }
  .k { font-size: 10.5px; font-weight: 600; letter-spacing: 0.04em; fill: var(--ink2); }
  .v { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; }
  .sub { font-size: 10.5px; fill: var(--ink2); }
  .grid { stroke: var(--grid); stroke-width: 1; }
  .base { stroke: var(--axis); stroke-width: 1; }
  .band { fill: var(--band); }
  .start { stroke: var(--s1); stroke-width: 1.5; }
  .startl { font-size: 11px; font-weight: 650; fill: var(--ink); }
  .mark { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 2 3; } .markd { fill: var(--muted); }
  .evl { font-size: 10.5px; fill: var(--ink2); }
  .seg { stroke: var(--surface); stroke-width: 1; }
  .bot { fill: var(--s1); } .agent { fill: var(--s2); } .other { fill: var(--s3); } .automation, .unknown { fill: var(--neutral); }
  .failed { fill: var(--failed); } .early { fill: var(--neutral); }
  .partial { opacity: 0.5; }
  .line { fill: none; stroke: var(--ink2); stroke-width: 2; } .dot { fill: var(--ink2); stroke: var(--surface); stroke-width: 2; }
"""


def _nice_axis(v: float) -> tuple[float, float]:
    """(top, step) of the tightest axis with a round step (1, 2, 2.5 or 5 x 10^k) and at most 5 intervals: 26 -> (30, 10)."""
    v = max(v, 1e-9)
    for k in range(-2, 7):
        for m in (1, 2, 2.5, 5):
            step = m * 10 ** k
            if math.ceil(v / step - 1e-9) <= 5:
                return math.ceil(v / step - 1e-9) * step, step
    return v, v / 4


def _bar_path(x: float, y: float, w: float, h: float, r: float) -> str:
    """Bar with rounded top corners only (the data end), square at the baseline."""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")


def _short(name: str, n: int = 22) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def _fmt_day(d: str) -> str:
    return date.fromisoformat(d).strftime("%b %-d")


def _delta(cur: float | None, prev: float | None) -> str:
    if cur is None or prev is None or not prev:
        return ""
    return f" · {100 * (cur - prev) / prev:+.0f}%"


def _origin_parts(row: dict) -> dict[str, float]:
    counts = row.get("merged_by_class")
    if counts is None:
        return {"bot": 0, "agent": 0, "other": 0, "automation": 0, "unknown": row["merged"]}
    parts = {kind: counts.get(kind, 0) for kind in CLASSES}
    parts["unknown"] = max(0, row["merged"] - sum(parts.values()))
    return parts


def render(data: dict, days: int, today: date, cfg: Config | None = None) -> str:
    """The dashboard: one UTC day per mark. When a bot is configured (or found), a band marks the days it ran, and the
    cards compare its averages with the days before it."""
    cfg = cfg or Config(repo=data["repo"])
    rows = window_rows(data, shown_days(data, cfg, days, today), today)
    if not rows:
        raise SystemExit("no data to render")
    today_s = today.isoformat()
    width, pad = 920, 24
    left, right = pad + 42, width - pad
    slot = (right - left) / len(rows)
    bar_w = max(2.0, slot - 2)
    x_left = lambda i: left + i * slot
    x_mid = lambda i: left + i * slot + slot / 2
    index = {day: i for i, (day, _) in enumerate(rows)}
    cmp = compare(data, cfg, days, today)
    cur, prev = cmp["cur"], cmp["prev"]
    start = cmp["start"]
    start_i = next((i for i, (day, _) in enumerate(rows) if day >= start), None) if start else None
    out: list[str] = []

    # Heading and the one-sentence story
    out.append(f'<text class="h1" x="{pad}" y="34">{escape(cfg.heading)}</text>')
    out.append(f'<text class="meta" x="{width - pad}" y="34" text-anchor="end">last {len(rows)} days · '
               f'updated {escape(data["updated"].replace("T", " ").replace("Z", " UTC"))}</text>')
    today_row = data["days"].get(today_s)
    today_note = f' Today so far: {today_row["merged"]} merged.' if today_row else ""
    if start:
        since = [r for d, r in data["days"].items() if d >= start]
        merged_since = sum(r["merged"] for r in since)
        bot_since = sum((r.get("merged_by_class") or {}).get("bot", 0) for r in since)
        share = f" ({100 * bot_since / merged_since:.0f}%)" if merged_since else ""
        lead = (f"{cfg.bot_name} has run since {_fmt_day(start)}: {bot_since:,} of the {merged_since:,} PRs merged since "
                f"then were its own{share}.{today_note}")
    else:
        lead = f"Each mark is one UTC day; the cards compare the last 7 days with the 7 before.{today_note}"
    out.append(f'<text class="lead" x="{pad}" y="54">{escape(lead)}</text>')

    # Cards: the period with the bot vs the period before it
    factory = factory_stats(factory_rows(data, today))
    factory_detail = []
    if factory:
        factory_detail = [
            f"Agent time on new work ({factory['days']}d): {_fmt(factory.get('new_work_share'), 'pct')}",
            f"Fixes {factory['fix_h']:.1f}h/day; send-backs {factory['sendback_h']:.1f}h/day",
            f"Worker slots used: {_fmt(factory.get('slot_use'), 'pct')} of {factory.get('max_workers') or '?'}",
            f"Fix loops per merged PR: {_fmt(factory.get('loops_per_pr'), 'n')}",
            f"Idle with a free slot: {factory['idle_h']:.1f}h/day",
        ]
    was = f'{cmp["prev_label"]}:'
    if start:
        share_card = (f"{cfg.bot_name.upper()}-MADE SHARE", _fmt(cur.get("bot_merge_share"), "pct"),
                      f'{cur.get("bot_merged", 0):,} PRs · all agents {_fmt(cur.get("agent_share"), "pct")}')
    else:
        share_card = ("AGENT-MADE SHARE", _fmt(cur.get("agent_share"), "pct"),
                      f'{was} {_fmt(prev.get("agent_share"), "pct")}')
    cards = [
        ("MERGED PRs / DAY", _fmt(cur.get("merged_per_day"), "n"),
         f'{was} {_fmt(prev.get("merged_per_day"), "n")}{_delta(cur.get("merged_per_day"), prev.get("merged_per_day"))}', []),
        (*share_card, factory_detail),
        ("RUNNER HOURS / MERGED PR", _fmt(cur.get("runner_h_per_pr"), "h"),
         f'{was} {_fmt(prev.get("runner_h_per_pr"), "h")}{_delta(cur.get("runner_h_per_pr"), prev.get("runner_h_per_pr"))}', []),
        ("QUEUE WAIT P90", _fmt(cur.get("wait_p90"), "min"),
         f'{was} {_fmt(prev.get("wait_p90"), "min")}{_delta(cur.get("wait_p90"), prev.get("wait_p90"))}', []),
    ]
    card_y, card_h, card_gap = 68, 74, 12
    card_w = (width - 2 * pad - 3 * card_gap) / 4
    for i, (label, value, sub, detail) in enumerate(cards):
        x = pad + i * (card_w + card_gap)
        out.append("<g>" + (f"<title>{escape('. '.join(detail))}</title>" if detail else ""))
        if i == 1:
            out.append("<desc>Merged PRs per day, by origin. Agent share of merged PRs</desc>")
        out.append(f'<rect class="tile" x="{x:.1f}" y="{card_y}" width="{card_w:.1f}" height="{card_h}" rx="8"/>')
        out.append(f'<text class="k" x="{x + 14:.1f}" y="{card_y + 20}">{escape(label)}</text>')
        out.append(f'<text class="v" x="{x + 14:.1f}" y="{card_y + 46}">{escape(value)}</text>')
        out.append(f'<text class="sub" x="{x + 14:.1f}" y="{card_y + 63}">{escape(sub)}</text>')
        out.append("</g>")
    span = lambda ds: f"{_fmt_day(ds[0])}–{_fmt_day(ds[-1])}" if ds else "no days yet"
    out.append(f'<text class="meta" x="{pad}" y="{card_y + card_h + 18}">Cards: {escape(cmp["cur_label"])} '
               f'({span(cmp["cur_days"])}, finished days) vs {escape(cmp["prev_label"])} ({span(cmp["prev_days"])}).</text>')

    # Shared furniture for every panel
    events = [(d, label) for d, label in cfg.events if d in index]

    def frame(top: int, chart_h: int, axis: tuple[float, float], title: str, formatter, title_detail: str = "") -> None:
        maximum, step = axis
        intervals = round(maximum / step)
        if start_i is not None:
            out.append(f'<rect class="band" x="{x_left(start_i):.1f}" y="{top}" width="{right - x_left(start_i):.1f}" '
                       f'height="{chart_h}"/>')
        tip = f"<title>{escape(title_detail)}</title>" if title_detail else ""
        out.append(f'<g>{tip}<text class="h2" x="{pad}" y="{top - 30}">{escape(title)}</text></g>')
        for tick in range(intervals + 1):
            value = step * tick
            y = top + chart_h - chart_h * tick / intervals
            cls = "base" if tick == 0 else "grid"
            out.append(f'<line class="{cls}" x1="{left}" x2="{right}" y1="{y:.1f}" y2="{y:.1f}"/>')
            out.append(f'<text class="tick" x="{left - 8}" y="{y + 3.5:.1f}" text-anchor="end">{formatter(value)}</text>')
        for idx, (day, _) in enumerate(rows):
            if (len(rows) - 1 - idx) % 7 == 0:
                out.append(f'<text class="tick" x="{x_mid(idx):.1f}" y="{top + chart_h + 16}" '
                           f'text-anchor="middle">{_fmt_day(day)}</text>')
        for day, label in events:
            x = x_left(index[day])
            out.append(f'<g><title>{_fmt_day(day)}: {escape(label)}</title><line class="mark" x1="{x:.1f}" x2="{x:.1f}" '
                       f'y1="{top - 4}" y2="{top + chart_h}"/></g>')
        if start_i is not None:
            x = x_left(start_i)
            out.append(f'<line class="start" x1="{x:.1f}" x2="{x:.1f}" y1="{top - 4}" y2="{top + chart_h}"/>')

    def annotate(top: int) -> None:
        """Labels for the bot's start and the events, in the lane above the first chart (two rows, no overlaps)."""
        labels = [(x_left(index[d]), f"{_fmt_day(d)} · {label}", d < (start or "9999")) for d, label in events]
        if start_i is not None:
            labels.append((x_left(start_i), f"{cfg.bot_name} live · {_fmt_day(start)}", False))
        taken: list[list[tuple[float, float]]] = [[], []]
        for x, text, before in sorted(labels, key=lambda item: item[2]):
            w = 6.2 * len(text)
            x0, x1 = (x - 5 - w, x - 5) if before else (x + 5, x + 5 + w)
            if x1 > right:
                x0, x1 = x - 5 - w, x - 5
                before = True
            row = next((r for r in (0, 1) if all(x1 < a or x0 > b for a, b in taken[r])), 1)
            taken[row].append((x0, x1))
            cls = "startl" if text.startswith(f"{cfg.bot_name} live") else "evl"
            anchor = "end" if before else "start"
            out.append(f'<text class="{cls}" x="{(x - 5 if before else x + 5):.1f}" y="{top - 8 - 13 * row}" '
                       f'text-anchor="{anchor}">{escape(text)}</text>')

    def line_series(top: int, chart_h: int, values: list[float | None], maximum: float, formatter, detail: str) -> None:
        y_at = lambda value: top + chart_h - chart_h * value / maximum
        segment: list[str] = []
        dots = []
        for i, ((day, _), value) in enumerate(zip(rows, values)):
            if value is None:
                if len(segment) > 1:
                    out.append(f'<polyline class="line" points="{" ".join(segment)}"/>')
                segment = []
                continue
            segment.append(f"{x_mid(i):.1f},{y_at(value):.1f}")
            partial = day == today_s
            dots.append(f'<circle class="dot{" partial" if partial else ""}" cx="{x_mid(i):.1f}" cy="{y_at(value):.1f}" '
                        f'r="4"><title>{_fmt_day(day)}: {formatter(value)} {detail}'
                        f'{" (today so far)" if partial else ""}</title></circle>')
        if len(segment) > 1:
            out.append(f'<polyline class="line" points="{" ".join(segment)}"/>')
        out.extend(dots)

    def legend(y: int, items: list[tuple[str, str]]) -> None:
        x = right
        for label, css in reversed(items):
            x -= 6.2 * len(label) + 22
            out.append(f'<rect class="{css}" x="{x:.1f}" y="{y - 8}" width="9" height="9" rx="2"/>')
            out.append(f'<text class="lbl" x="{x + 13:.1f}" y="{y}">{escape(label)}</text>')

    # 1 · Delivery: merged PRs each day, by origin
    y = card_y + card_h + 18 + 62
    top, chart_h = y, 195
    delivery = [_origin_parts(row) for _, row in rows]
    delivery_axis = _nice_axis(max(1.0, max(sum(parts.values()) for parts in delivery)))
    delivery_max = delivery_axis[0]
    frame(top, chart_h, delivery_axis, "1 · Delivery — merged PRs per day, by origin", lambda v: f"{v:g}")
    origins = [("bot", cfg.bot_name), ("agent", "other agents"), ("other", "other"), ("automation", "automation"),
               ("unknown", "unclassified")]
    if not start and not any(parts["bot"] for parts in delivery):
        origins = origins[1:]
    for i, ((day, _), parts) in enumerate(zip(rows, delivery)):
        x, base = x_left(i) + (slot - bar_w) / 2, top + chart_h
        shown = [(kind, parts[kind]) for kind, _ in origins if parts[kind] > 0]
        elements = []
        for n, (kind, value) in enumerate(shown):
            h = chart_h * value / delivery_max
            if n == len(shown) - 1:
                elements.append(f'<path class="{kind} seg" d="{_bar_path(x, base - h, bar_w, h, 4)}"/>')
            else:
                elements.append(f'<rect class="{kind} seg" x="{x:.1f}" y="{base - h:.1f}" width="{bar_w:.1f}" height="{h:.1f}"/>')
            base -= h
        total = sum(parts.values())
        tip = escape(f'{_fmt_day(day)} · {total:.0f} merged PRs{" (today so far)" if day == today_s else ""} · '
                     + ", ".join(f"{label} {parts[kind]:.0f}" for kind, label in origins))
        fade = ' class="partial"' if day == today_s else ""
        out.append(f'<g{fade}><title>{tip}</title>{"".join(elements)}</g>')
    annotate(top)
    legend(top - 30, [(label, kind) for kind, label in origins if kind != "unknown" or any(p["unknown"] for p in delivery)])
    y = top + chart_h + 70

    # 2 · Queue wait and 3 · CI cost (sampled)
    details = [row.get("detail", {}) for _, row in rows]
    wait_values = [detail.get("wait_p90") for detail in details]
    runner_values = [detail["runner_min_est"] / row["merged"] / 60 if detail.get("runner_min_est") is not None
                     and row["merged"] else None for (_, row), detail in zip(rows, details)]
    if any(v is not None for v in wait_values + runner_values):
        top, chart_h = y, 120
        wait_axis = _nice_axis(max((v for v in wait_values if v is not None), default=1.0))
        wait_max = wait_axis[0]
        frame(top, chart_h, wait_axis, "2 · Speed — job queue wait (p90) per day, sampled (minutes)", lambda v: f"{v:g}")
        for i, (day, row) in enumerate(rows):
            changes = row.get("detail", {}).get("ci_changes")
            if changes:
                prs = ", ".join(f'#{change["pr"]}' for change in changes[:6])
                out.append(f'<g><title>{_fmt_day(day)}: CI change(s) merged {escape(prs)}</title>'
                           f'<path class="markd" d="M{x_mid(i):.1f},{top - 9} l3,3 l-3,3 l-3,-3 Z"/></g>')
        line_series(top, chart_h, wait_values, wait_max, lambda v: f"{v:.1f}",
                    f"minutes · about {cfg.sample_runs} runs sampled per day")
        legend(top - 30, [("CI change merged", "markd")])
        y = top + chart_h + 70
        top = y
        runner_axis = _nice_axis(max((v for v in runner_values if v is not None), default=1.0))
        runner_max = runner_axis[0]
        frame(top, chart_h, runner_axis, "3 · Cost — runner time per merged PR, estimated hours (sampled)", lambda v: f"{v:g}")
        line_series(top, chart_h, runner_values, runner_max, lambda v: f"{v:.2f}", "runner hours / merged PR")
        y = top + chart_h + 70
    else:
        out.append(f'<text class="h2" x="{pad}" y="{y - 30}">2 · Speed and 3 · Cost</text>')
        out.append(f'<text class="meta" x="{pad}" y="{y - 12}">No sampled CI capacity history yet.</text>')
        y += 20

    # 4 · Outcomes: failed runs (waste) and runs cancelled early (a newer push replaced them, so their time was cut short)
    proof_detail = ["CI workflow-run outcomes; not a per-PR quality score"]
    reruns = rerun_split(finished(rows, today)[-7:])
    if reruns:
        total, passed, failed = sum(item[1] for item in reruns), sum(item[2] for item in reruns), sum(item[3] for item in reruns)
        proof_detail.append(f"Reruns that passed: {_fmt(passed / (passed + failed) if passed + failed else None, 'pct')} "
                            f"({passed} passed, {failed} failed again; {total} total)")
        proof_detail.extend(f"{name}: {count} reruns, {ok} passed, {again} failed again" for name, count, ok, again in reruns[:12])
    early = [max(0, row["runs"]["cancelled"] - row.get("bot_cancelled", 0)) for _, row in rows]
    top, chart_h = y, 120
    outcome_axis = _nice_axis(max(1.0, max(row["runs"]["failure"] + e for (_, row), e in zip(rows, early))))
    frame(top, chart_h, outcome_axis, "4 · Outcomes — CI runs that failed, and runs cancelled early, per day", lambda v: f"{v:g}",
          ". ".join(proof_detail))
    for i, ((day, row), cut) in enumerate(zip(rows, early)):
        r = row["runs"]
        x, base = x_left(i) + (slot - bar_w) / 2, top + chart_h
        elements = []
        shown = [(css, value) for css, value in (("failed", r["failure"]), ("early", cut)) if value > 0]
        for n, (css, value) in enumerate(shown):
            h = chart_h * value / outcome_axis[0]
            if n == len(shown) - 1:
                elements.append(f'<path class="{css} seg" d="{_bar_path(x, base - h, bar_w, h, 4)}"/>')
            else:
                elements.append(f'<rect class="{css} seg" x="{x:.1f}" y="{base - h:.1f}" width="{bar_w:.1f}" height="{h:.1f}"/>')
            base -= h
        judged = r["success"] + r["failure"]
        rate = f'{100 * r["success"] / judged:.1f}%' if judged else "–"
        bookkeeping = f' (+{row["bot_cancelled"]} bot bookkeeping runs replaced by newer ones, not shown)' if row.get("bot_cancelled") else ""
        fade = ' class="partial"' if day == today_s else ""
        tip = escape(f'{_fmt_day(day)}: {r["total"]:,} CI runs · {r["failure"]} failed · {cut} cancelled early{bookkeeping} · '
                     f'pass rate {rate} (success / success + failure){" (today so far)" if day == today_s else ""}')
        out.append(f'<g{fade}><title>{tip}</title>{"".join(elements)}</g>')
    legend(top - 30, [("failed", "failed"), ("cancelled early (newer push)", "early")])
    y = top + chart_h + 44

    prefixes = ", ".join(cfg.bot_prefixes)
    out.append(f'<text class="meta" x="{pad}" y="{y}">Source: GitHub API. Days are UTC; faded marks are today so far. '
               f'Queue wait and runner time are sampled from about {cfg.sample_runs} runs a day.</text>')
    out.append(f'<text class="meta" x="{pad}" y="{y + 15}">'
               + escape((f"{cfg.bot_name} = PRs from {prefixes} branches. " if prefixes else "")
                        + "Other agents = agent branch prefixes or PR-body markers. Pass rate is per workflow run, "
                          "not per PR.") + '</text>')
    height = y + 34
    body = "\n  ".join(out)
    merged_total = sum(row["merged"] for _, row in rows)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'role="img" aria-label="{escape(cfg.heading)}: delivery, speed, cost and CI waste per day'
            f'{f", before and with {escape(cfg.bot_name)}" if start else ""}; '
            f'{merged_total:,} merged PRs in the selected period">\n'
            f'<style>{STYLE}</style>\n<g class="root">\n  <rect class="bg" x="0.5" y="0.5" width="{width - 1}" '
            f'height="{height - 1}" rx="10"/>\n  {body}\n</g>\n</svg>\n')


# ---------------------------------------------------------------- CLI

def _opt_float(s: str | None) -> float | None:
    return float(s) if s not in (None, "") else None


def main(argv: list[str] | None = None) -> int:
    env = os.environ.get
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", default=env("FLEET_REPORT_REPO") or env("GITHUB_REPOSITORY"))
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--out", type=Path, default=Path("dashboard"))
    p.add_argument("--title", default="")
    p.add_argument("--bot-workflows", default="", help="comma-separated names of an orchestrator's own bookkeeping workflows")
    p.add_argument("--agent-branch-prefixes", default=DEFAULT_AGENT_PREFIXES)
    p.add_argument("--agent-markers", default=DEFAULT_AGENT_MARKERS, help="regex matched against PR bodies")
    p.add_argument("--bot-branch-prefixes", default="", help="head-branch prefixes of the bot this dashboard follows")
    p.add_argument("--bot-name", default="bot")
    p.add_argument("--bot-start", default="", help="YYYY-MM-DD the bot went live (default: its first merged PR)")
    p.add_argument("--events", default="", help="'YYYY-MM-DD: label; ...' marked on the charts")
    p.add_argument("--sample-runs", type=int, default=24)
    p.add_argument("--detail-backfill", type=int, default=DETAIL_BACKFILL,
                   help="older days to fill with the sampled detail this run (each costs ~30 API calls)")
    p.add_argument("--summary", type=Path, help="append the Markdown report here (the Action passes $GITHUB_STEP_SUMMARY)")
    p.add_argument("--max-wait-p90", default="", help="alert when the 7-day p90 job queue wait (min) exceeds this")
    p.add_argument("--max-runner-hours-per-pr", default="", help="alert when 7-day runner hours per merged PR exceed this")
    p.add_argument("--fail-on-alert", action="store_true")
    p.add_argument("--render-only", action="store_true", help="redraw from metrics.json without fetching")
    a = p.parse_args(argv)
    if not a.repo:
        p.error("--repo is required (or GITHUB_REPOSITORY)")
    cfg = Config(repo=a.repo, title=a.title, bot_workflows=_csv(a.bot_workflows),
                 agent_prefixes=_csv(a.agent_branch_prefixes), agent_markers=a.agent_markers, sample_runs=a.sample_runs,
                 bot_prefixes=_csv(a.bot_branch_prefixes), bot_name=a.bot_name or "bot", bot_start=a.bot_start,
                 events=_events(a.events))
    a.out.mkdir(parents=True, exist_ok=True)
    data_file = a.out / "metrics.json"
    data = json.loads(data_file.read_text()) if data_file.exists() else {}
    today = datetime.now(timezone.utc).date()
    if not a.render_only:
        data = collect(cfg, a.days, data, today, a.detail_backfill)
        data_file.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    factory_file = a.out / "factory.json"  # an orchestrator's own numbers, published to the same branch (optional)
    if factory_file.exists():
        try:
            data = {**data, "factory": json.loads(factory_file.read_text())}
        except ValueError as e:
            print(f"::warning title=Fleet CI report::factory.json unreadable: {e}", file=sys.stderr)
    (a.out / "dashboard.svg").write_text(render(data, a.days, today, cfg), encoding="utf-8")
    alerts = check_alerts(data, a.days, today, _opt_float(a.max_wait_p90), _opt_float(a.max_runner_hours_per_pr))
    for msg in alerts:
        print(f"::warning title=Fleet CI report::{msg}")
    if a.summary:
        with a.summary.open("a", encoding="utf-8") as f:
            f.write(summary_md(data, cfg, a.days, today, alerts))
    print(f"wrote {a.out}/dashboard.svg and metrics.json ({len(data['days'])} days of history)", file=sys.stderr)
    return 1 if alerts and a.fail_on_alert else 0


if __name__ == "__main__":
    raise SystemExit(main())
